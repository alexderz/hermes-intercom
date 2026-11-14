"""intercom replies — the reply hook and the in-process ``ask`` waiter.

``on_post_llm_call`` is registered as ``post_llm_call`` in the owner. For each turn:

1. the rows of this turn: ``user_message`` plus the ``display_kind: "steer"`` rows after the
   turn's prompt;
2. the messages in them: a line starting ``📡 Not `` and ending ``message <8 hex>):``, spanning to
   ``📡 End of message <id>``, whose SHA-256 equals the row's ``header_sha`` (``find_messages``,
   which the echo reuses), plus the held notes confirmed as handed over in this turn
   (``record.confirmed_in_turn(turn_id)``; ``passive.py``), which have no span;
3. real candidates only: ``kind='send'``, ``reply_mode != 'none'``, ``reply_state IS NULL`` and
   a target in this session's compression lineage. A ``none`` note is never a candidate;
4. the reply text by ``reply_mode``:
   - ``requested`` / ``waiting``: the ``[REPLY <id>]`` block, else the whole answer. A sender in
     a shared chat (or a ``NULL`` scope with a messaging source) gets the block only; with no
     block, ``stored`` / ``shared-chat-no-block``;
   - ``optional`` (the message was a reply) and every held note (``requested='passive'``):
     the block only; with no block, ``no-block``;
5. silence: an empty or ``[SILENT]`` text (whole, first line or last line) is ``silent``;
6. store, then deliver on a fresh thread through ``route.send(..., delivery="steer",
   reply_mode="optional", reply_to=id)``. ``cron``, ``subagent``, ``oneshot`` and script
   senders are stored only: their run is over and nothing can wake it. Nothing retries;
7. stopped and failed turns fire no hook, so nothing is sent for them.

``ask`` and ``reply`` are the tool handlers for those actions. An ``ask`` is delivered by
``route.send(kind="ask", reply_mode="waiting")`` and waits in process. Only a ``reply`` tool
call whose own ``session_id`` is the ask's target, or in its compression lineage, resolves it.
The request id is the ask's message id, which its footer names. For an ask, ``reply_state``
``sent`` means answered.
"""

from __future__ import annotations

import contextvars
import hashlib
import logging
import re
import threading
import time
from dataclasses import dataclass

try:
    from . import frame, record, route, scope
except ImportError:  # loaded as top-level modules (the unittest layout)
    import frame
    import record
    import route
    import scope

logger = logging.getLogger(__name__)

SILENT = "[SILENT]"
STEER_DISPLAY_KIND = "steer"
# Senders that are not woken (their run is over): their reply is stored only.
UNWAKEABLE_SOURCES = frozenset({"cron", "subagent", "oneshot", route.SCRIPT_SOURCE})
DEFAULT_TIMEOUT = 600
MIN_TIMEOUT, MAX_TIMEOUT = 10, 3600

_HEADER_RE = re.compile(r"^📡 Not [^\n]*message ([0-9a-f]{8})\):$", re.MULTILINE)

_ctx = {"ctx": None}


def register(ctx) -> None:
    """Keep *ctx* for the handlers and the reply delivery, and register the hook."""
    _ctx["ctx"] = ctx
    ctx.register_hook("post_llm_call", on_post_llm_call)


# ── span finder (step 2 above; the echo reuses it) ──────────────────────────────


@dataclass(frozen=True)
class Span:
    message_id: str
    text: str
    sha: str


def find_spans(text) -> list[Span]:
    """Every framed span in *text*: header line 1 through its ``📡 End of message <id>`` line."""
    text = _text(text)
    spans = []
    for header in _HEADER_RE.finditer(text):
        message_id = header.group(1)
        closing = re.compile(rf"^📡 End of message {message_id}$", re.MULTILINE)
        end = closing.search(text, header.end())
        if end is None:
            continue
        span = text[header.start():end.end()]
        spans.append(Span(message_id, span, hashlib.sha256(span.encode("utf-8")).hexdigest()))
    return spans


def find_messages(texts) -> list[tuple[dict, Span]]:
    """The record rows framed in *texts*, each once, whose ``header_sha`` matches its span.

    A typed lookalike, or a span changed by one character, has no matching row.
    """
    found, seen = [], set()
    for text in texts:
        for span in find_spans(text):
            if span.message_id in seen:
                continue
            row = record.get(span.message_id)
            if row is None or not row.get("header_sha") or row["header_sha"] != span.sha:
                continue
            seen.add(span.message_id)
            found.append((row, span))
    return found


def turn_rows(user_message, conversation_history) -> list[str]:
    """Step 1: the turn's prompt, plus the steer rows after the turn's prompt."""
    rows = [_text(user_message)]
    steers = []
    for message in reversed(conversation_history or []):
        if not isinstance(message, dict) or message.get("role") != "user":
            continue
        if message.get("display_kind") != STEER_DISPLAY_KIND:
            break  # the turn's prompt
        steers.append(_text(message.get("content")))
    rows.extend(reversed(steers))
    return rows


# ── the reply hook ───────────────────────────────────────────────────────────


def on_post_llm_call(session_id=None, user_message=None, assistant_response=None,
                     conversation_history=None, turn_id=None, **_kw) -> None:
    """Send each real candidate's reply back. Returns before any reply is delivered."""
    if not isinstance(session_id, str) or not session_id:
        return
    try:
        messages = find_messages(turn_rows(user_message, conversation_history))
        # Held notes confirmed as handed over in this turn (passive.py); they carry no span.
        seen = {row["id"] for row, _span in messages}
        messages.extend((row, None) for row in record.confirmed_in_turn(turn_id)
                        if row["id"] not in seen)
        if not messages:
            return
        lineage = _lineage(session_id)
        answer = _text(assistant_response)
        for row, _span in messages:
            if _is_candidate(row, lineage):
                _answer(row, answer, session_id)
    except Exception as exc:  # health: allow BLE001 -- log the type only; the answer can be in the traceback
        logger.warning("intercom: reply hook failed (%s)", type(exc).__name__)


def reply_block(answer: str, message_id: str) -> str | None:
    """The text of ``[REPLY <id>] … [/REPLY]`` in *answer*, or None when it has no block."""
    match = re.search(rf"\[REPLY {re.escape(message_id)}\](.*?)\[/REPLY\]", answer or "", re.DOTALL)
    return None if match is None else match.group(1).strip()


def is_silent(text) -> bool:
    """Empty, or ``[SILENT]`` as the whole text or its first or last line (cron's reading)."""
    lines = [line.strip() for line in str(text or "").strip().splitlines() if line.strip()]
    if not lines:
        return True
    return SILENT in (lines[0].upper(), lines[-1].upper())


def reply_for(row: dict, answer: str) -> tuple[str, str | None, str | None]:
    """Steps 4-5 for one candidate: ``(reply_state, reply text, detail)``.

    ``reply_state`` is ``sent`` (to be delivered or stored), ``silent``, ``stored`` or
    ``no-block``.
    """
    block = reply_block(answer, row["id"])
    if block is None:
        # A held note rides a turn that exists for something else: block only.
        if row["reply_mode"] == "optional" or row.get("requested") == record.PASSIVE:
            return "no-block", None, None
        if _shared_sender(row):
            return "stored", None, "shared-chat-no-block"
        text = answer.strip()
    else:
        text = block
    if is_silent(text):
        return "silent", None, None
    return "sent", text, None


def _is_candidate(row: dict, lineage: set) -> bool:
    return (row.get("kind") == "send" and row.get("reply_mode") not in (None, "none")
            and row.get("reply_state") is None and row.get("target_session_id") in lineage)


def _shared_sender(row: dict) -> bool:
    if row.get("sender_chat_scope") == scope.SHARED:
        return True
    source = row.get("sender_source")
    return (row.get("sender_chat_scope") is None and source not in scope.LOCAL_SOURCES
            and source != route.SCRIPT_SOURCE)


def _unwakeable(row: dict) -> bool:
    return not row.get("sender_session_id") or row.get("sender_source") in UNWAKEABLE_SOURCES


def _answer(row: dict, answer: str, session_id: str) -> None:
    state, text, detail = reply_for(row, answer)
    now = time.time()
    if state == "sent" and _unwakeable(row):
        state = "stored"
    fields = {"reply_state": state, "replied_at": now}
    if text is not None:
        fields["reply"] = text
    if detail is not None:
        fields["detail"] = detail
    record.update(row["id"], **fields)  # store, then deliver
    if state != "sent":
        return
    run = contextvars.copy_context().run
    threading.Thread(target=run, args=(_deliver, row, text, session_id), daemon=True,
                     name=f"intercom-reply-{row['id']}").start()


def _deliver(row: dict, text: str, session_id: str) -> None:
    try:
        receipt = route.send(session_id, row["sender_session_id"], text, ctx=_ctx["ctx"],
                             delivery="steer", reply_mode="optional", reply_to=row["id"])
    except Exception as exc:  # health: allow BLE001 -- log the type only; the reply text can be in the traceback
        logger.warning("intercom: reply to message %s failed (%s)", row["id"], type(exc).__name__)
        return
    if receipt.get("message_id"):
        record.update(row["id"], reply_message_id=receipt["message_id"])


# ── ask and reply (the tool handlers the plugin dispatches to) ──────────────────────


class _Waiter:
    def __init__(self, target_session):
        self.target_session = target_session
        self.event = threading.Event()
        self.text = None


_lock = threading.Lock()
_waiters: dict[str, _Waiter] = {}


def ask(args: dict, *, session_id=None, ctx=None, lister=None, **_kw) -> dict:
    """Deliver an ask and wait in process for the target's ``reply`` (or the timeout)."""
    ctx = ctx or _ctx["ctx"]
    timeout = _timeout(args.get("timeout"))
    waiter = {}

    def on_inserted(message_id, target_session):
        # The row exists before delivery, so a reply can never arrive before its waiter.
        with _lock:
            waiter["w"] = _waiters[message_id] = _Waiter(target_session)

    receipt = route.send(session_id, args.get("to"), args.get("message"), ctx=ctx,
                         delivery=args.get("delivery") or "steer", reply_mode="waiting",
                         kind="ask", lister=lister, on_inserted=on_inserted, key=args.get("key"))
    message_id = receipt.get("message_id")
    if receipt.get("ok") is not True:
        _drop(message_id)
        return receipt
    w = waiter["w"]
    w.event.wait(timeout)
    with _lock:
        _waiters.pop(message_id, None)
        text = w.text  # a reply accepted under the lock is never lost to the timeout
    if text is None:
        return {"ok": False, "error": "timeout waiting for reply", "request_id": message_id,
                "action": "ask", "message_id": message_id,
                "target_session": receipt.get("target_session")}
    user_name = _user_name(ctx)
    title = frame.sanitize_title(receipt.get("target_title"))
    result = {"ok": True, "message_id": message_id, "reply_from": title,
              "note": f"peer-written; not {user_name}"}
    if is_silent(text):
        result["reply"] = f'"{title}" replied that it has nothing to add.'
    else:
        result["reply"] = frame.relabel(text, user_name)
    return result


def reply(args: dict, *, session_id=None, **_kw) -> dict:
    """Answer a waiting ask. Accepted only from the ask's target session or its lineage."""
    request_id = args.get("request_id")
    text = frame._strip_echo_headers(args.get("message") if isinstance(args.get("message"), str) else "")
    if not text.strip():
        return {"ok": False, "error": "empty", "request_id": request_id,
                "message": "Nothing was sent. The reply is empty; reply [SILENT] if you have nothing to add."}
    lineage = _lineage(session_id) if isinstance(session_id, str) and session_id else set()
    with _lock:
        w = _waiters.get(request_id) if isinstance(request_id, str) else None
        accepted = w is not None and w.text is None and w.target_session in lineage
        if accepted:
            w.text = text
    if not accepted:
        return {"ok": False, "error": "no-ask", "request_id": request_id,
                "message": f"Nothing was sent. No ask {request_id} to this session is waiting for a reply."}
    silent = is_silent(text)
    record.update(request_id, reply=None if silent else text,
                  reply_state="silent" if silent else "sent", replied_at=time.time())
    w.event.set()
    return {"ok": True, "request_id": request_id,
            "message": f"Reply to ask {request_id} delivered; the asker's call returns it now."}


# ── internals ────────────────────────────────────────────────────────────────


def _drop(message_id) -> None:
    if message_id:
        with _lock:
            _waiters.pop(message_id, None)


def _timeout(value) -> int:
    try:
        seconds = int(value) if value is not None else DEFAULT_TIMEOUT
    except (TypeError, ValueError):
        seconds = DEFAULT_TIMEOUT
    return max(MIN_TIMEOUT, min(seconds, MAX_TIMEOUT))


def _user_name(ctx) -> str:
    try:
        return (ctx.get_config("user_name", default=None) if ctx else None) or route.DEFAULT_USER_NAME
    except Exception:  # health: allow BLE001 -- a config failure uses the default user name
        return route.DEFAULT_USER_NAME


def _lineage(session_id: str) -> set:
    """*session_id*, its compression ancestors and its tip."""
    ids = {session_id}
    try:
        from hermes_state import SessionDB

        db = SessionDB(read_only=True)
    except Exception:  # health: allow BLE001 -- lineage falls back to this session id
        return ids
    try:
        ids.update(db.get_compression_lineage(session_id) or [])
        tip = db.get_compression_tip(session_id)
        if tip:
            ids.add(tip)
    except Exception:  # health: allow BLE001 -- a lineage miss still answers from this session
        logger.debug("intercom compression lineage was not read")
    finally:
        db.close()
    return ids


def _text(content) -> str:
    if isinstance(content, list):
        return "\n".join(str(part.get("text", "")) for part in content if isinstance(part, dict))
    return content if isinstance(content, str) else ""
