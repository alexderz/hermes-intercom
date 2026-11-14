"""intercom passive — held notes handed over at the start of the target's next turn.

A passive note is a ``pending`` record row (``route.send(delivery="passive")``). Two hooks
hand it over:

- ``pre_llm_call`` (``on_pre_llm_call``), once at the start of every turn: with
  ``allow_gateway_injection`` on, it offers the turn the session lineage's per-target notes and,
  for a conversation session (source not in ``address.RUN_SOURCES``), the newest unseen ``all``
  note per key that another lineage sent (``record.offer_notes``), and returns them, oldest
  first, framed by ``frame.held_notes`` as the turn's context;
- ``pre_api_request`` (``on_pre_api_request``), on the first model request of that turn:
  confirms the offer (``record.confirm_notes``), and echoes each per-target note into a private
  chat once (``record.claim_echo``). ``all`` notes are never echoed.

A turn that makes no model request (vetoed, or a runtime that does not carry plugin text) leaves
its notes held; the next turn offers them again. Once the first request is built the notes are
in the saved user message, so a later failure still counts them delivered. A steer starts no
turn, so it hands nothing over. The reply hook answers a ``reply requested`` note only from its
``[REPLY <id>]`` block (``replies.py``).
"""

from __future__ import annotations

import logging
import threading

try:
    from . import address, echo, frame, record, replies, route, scope
except ImportError:  # loaded as top-level modules (the unittest layout)
    import address
    import echo
    import frame
    import record
    import replies
    import route
    import scope

logger = logging.getLogger(__name__)

_ctx = {"ctx": None}

# turn id -> lineage root, for turns offered notes and not yet confirmed. Only those turns'
# requests touch the record.
_lock = threading.Lock()
_offered: dict[str, str] = {}


def register(ctx) -> None:
    """Keep *ctx* for settings and the echo, and register both hooks."""
    _ctx["ctx"] = ctx
    ctx.register_hook("pre_llm_call", on_pre_llm_call)
    ctx.register_hook("pre_api_request", on_pre_api_request)


def on_pre_llm_call(session_id=None, turn_id=None, **_kw):
    """Offer this turn its held notes. Returns ``{"context": <frame>}``, or None when none."""
    if not _usable(session_id) or not _usable(turn_id):
        return None
    try:
        if not route.injection_allowed():
            return None
        lineage, root = _lineage_and_root(session_id)
        facts = route._facts(session_id)
        notes = record.offer_notes(turn_id, lineage=sorted(lineage), root_id=root,
                                   include_all=facts["source"] not in address.RUN_SOURCES)
        if not notes:
            return None
        ctx = _ctx["ctx"]
        text = frame.held_notes(notes, replies._user_name(ctx),
                                shared_line=_shared_line(notes, session_id, facts, ctx))
        # Registered only once the frame exists: a turn is never confirmed for notes it did not get.
        with _lock:
            # An earlier turn of this lineage that never made a request no longer counts.
            for stale in [t for t, r in _offered.items() if r == root]:
                del _offered[stale]
            _offered[turn_id] = root
        return {"context": text}
    except Exception as exc:  # health: allow BLE001 -- log the type only; a note body can be in the traceback
        logger.warning("intercom: held-note handover failed (%s)", type(exc).__name__)
        return None


def on_pre_api_request(session_id=None, turn_id=None, platform=None, **_kw) -> None:
    """Confirm the notes offered to *turn_id* at its first model request, then echo them."""
    if not _usable(turn_id):
        return
    with _lock:
        root = _offered.pop(turn_id, None)
    if root is None:
        return
    try:
        confirmed = record.confirm_notes(turn_id, root_id=root)
        _echo(confirmed, platform)
    except Exception as exc:  # health: allow BLE001 -- log the type only; a note body can be in the traceback
        logger.warning("intercom: held-note confirm failed (%s)", type(exc).__name__)


# ── internals ────────────────────────────────────────────────────────────────


def _usable(value) -> bool:
    return isinstance(value, str) and value != ""


def _lineage_and_root(session_id: str) -> tuple[set, str]:
    """The session, its compression ancestors and its tip (as ``replies._lineage``), and the
    lineage root, from one read. The root is the session id when it has no lineage row."""
    ids = {session_id}
    try:
        from hermes_state import SessionDB

        db = SessionDB(read_only=True)
    except Exception:  # health: allow BLE001 -- lineage and root fall back to this session id
        return ids, session_id
    chain = []
    try:
        chain = db.get_compression_lineage(session_id) or []
        ids.update(chain)
        tip = db.get_compression_tip(session_id)
        if tip:
            ids.add(tip)
    except Exception:  # health: allow BLE001 -- a lineage miss still hands over this session's notes
        logger.debug("intercom compression lineage was not read")
    finally:
        db.close()
    return ids, (chain[0] if chain else session_id)


def _shared_line(notes, session_id: str, facts: dict, ctx) -> str:
    """This session's shared-chat line when a note lands in a shared chat, else ``""``.

    Per-target notes use the scope stored at send; ``all`` notes this session's scope now.
    A session with no readable row is ``shared`` (``scope.chat_scope`` fails closed), so it gets
    the line without chat details. That is intended.
    """
    shared = any(n.get("target_session_id") and n.get("target_chat_scope") == scope.SHARED
                 for n in notes)
    if not shared and any(not n.get("target_session_id") for n in notes):
        shared = scope.chat_scope(session_id, ctx) == scope.SHARED
    if not shared:
        return ""
    return frame._shared_chat_line(facts["platform"], facts["chat_type"], facts["title"])


def _echo(rows, platform) -> None:
    """Per-target notes to a private chat: one echo each, claimed once."""
    for row in rows:
        if not row.get("target_session_id") or row.get("target_chat_scope") != scope.PRIVATE:
            continue
        if not record.claim_echo(row["id"]):
            continue
        text = frame.escape_echo(frame.held_echo(row, replies._user_name(_ctx["ctx"])), platform)
        if text is None:
            logger.warning("intercom: no echo escaper for %s; message %s not echoed",
                           platform or "unknown", row["id"])
            continue
        echo._schedule(row["id"], text)
