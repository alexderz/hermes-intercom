"""intercom send path — one message from a session or a script to one resolved target.

``send(...)`` runs these steps in order:

- inputs: a ``reply_mode`` outside ``none`` / ``requested`` / ``waiting`` is ``reply-missing``
  and records nothing. ``optional`` is only for a reply (``reply_to`` set, from the reply hook);
- 0 an empty or whitespace-only message is ``empty``;
- 1 ``address.resolve(to)``: ``ambiguous`` / ``unknown`` with candidates, the project-addressing refusals,
  and ``own-session`` when the target is the sender's own session;
- 2 the chat scopes of the target and of a session sender (``scope.chat_scope``);
- 3 consent for **every** delivery: ``plugins.entries.intercom.allow_gateway_injection`` must
  be ``true``, read fresh from the profile config, else ``injection-off``;
- 4 ``record.insert`` before delivery;
- 5 ``frame.build``, ``sanitize_user_prompt_text`` once, then ``header_sha`` over the span from
  header line 1 to the closing line, stored in the row;
- 6 ``client.deliver_sync``;
- 7 ``record.update`` and the receipt.

Refusals of steps 0-3 are recorded as ``delivery_path='refused'`` rows; ``reply-missing`` is not.

Operator power, deliberately: a stored messaging chat is delivered whatever its
platform's allowlist says now. Its scope is ``shared`` unless listed, so the frame carries the
shared-chat line. Sending is the operator's choice; the frame tells the target who else reads.
"""

from __future__ import annotations

import hashlib
import json
import logging
import secrets
import time

logger = logging.getLogger(__name__)

try:
    from . import address, client, frame, record, scope
except ImportError:  # loaded as top-level modules (the unittest layout)
    import address
    import client
    import frame
    import record
    import scope

REPLY_MODES = ("none", "requested", "waiting")
DEFAULT_USER_NAME = "the user"
SCRIPT_SOURCE = "script"

# Readable receipt sentences (README, "The CLI"). Refusals not listed here use address's sentence.
REPLY_MISSING = "Nothing was sent: choose reply: none, requested or waiting."
NOTHING_SENT = "Nothing was sent."
EMPTY_REASON = "The message is empty."
OWN_SESSION_REASON = "You cannot message your own session."
INJECTION_OFF = ("Not delivered: intercom may not deliver messages in this Hermes profile "
                 "(allow_gateway_injection is off). Nothing was sent.")
_ERROR_SENTENCES = {
    "no-owner": "Not delivered: the gateway for this profile is not running. You may send again later.",
    "owner-restarting": "Not delivered: the gateway is restarting. You may send again in a minute.",
    "not-reachable": ("Not delivered: the gateway did not answer before the message was handed "
                      "over. You may send again later."),
}
# address's own sentences, kept word for word with no "Nothing was sent." prefix.
_VERBATIM_REFUSALS = frozenset({"project-addressing-unavailable", "no-owner-set"})
_UNWAKEABLE = {"cron": "cron run", "oneshot": "oneshot", "subagent": "subagent"}


def not_addressable_sentence(title: str) -> str:
    return f'"{title}" is open in the admin dashboard, which intercom cannot reach. Nothing was sent.'


def cold_start_sentence(title: str, message_id: str) -> str:
    return (f'"{title}" was not open, so the gateway reopened it and message {message_id} '
            "started a turn there.")


def send(sender, to, message, *, ctx, delivery="steer", reply_mode=None, kind="send",
         reply_to=None, script=False, lister=None, on_inserted=None) -> dict:
    """Send *message* from *sender* to the one session *to* resolves to. Returns the receipt.

    *sender* is the calling session's id, or with ``script=True`` a script label (the CLI, which
    always frames as a script and never names a session as sender). *ctx* reads the plugin's
    settings. *lister* is ``session.list`` (tests pass their own); it decorates candidates and
    tells a cold start from an idle one. *on_inserted* is called with the message id and the
    target session once the row exists, before delivery (the ``ask`` waiter).
    """
    if delivery not in client.DELIVERIES:
        raise ValueError(f"delivery must be one of {', '.join(client.DELIVERIES)}")
    reply_to = reply_to or None
    receipt = _Receipt(to=to, requested=delivery, reply=reply_mode)
    if not (reply_mode in REPLY_MODES or (reply_mode == "optional" and reply_to)):
        return receipt.refused("reply-missing", REPLY_MISSING, reply=None)

    message_id = secrets.token_hex(4)
    receipt.message_id = message_id
    body = message if isinstance(message, str) else ""
    who = _Who.script(sender) if script else _Who.session(sender)
    row = dict(message_id=message_id, kind=kind, sender_label=who.label, sender_source=who.source,
               sender_session_id=who.session_id, address=to if isinstance(to, str) else "",
               body=body, reply_mode=reply_mode, requested=delivery, in_reply_to=reply_to)

    # 0. empty
    if not body.strip():
        return _refuse(receipt, row, "empty", f"{NOTHING_SENT} {EMPTY_REASON}")

    # 1. resolve, then own-session
    resolved = address.resolve(to, lister=lister)
    if resolved.get("ok") is not True:
        detail = resolved.get("error") or "unknown"
        reason = resolved.get("message") or address.UNKNOWN_MESSAGE
        text = reason if detail in _VERBATIM_REFUSALS else f"{NOTHING_SENT} {reason}"
        candidates = list(resolved.get("candidates") or [])
        if candidates:
            text = "\n".join([text, *(_candidate_line(c) for c in candidates)])
        return _refuse(receipt, row, detail, text, candidates=candidates)
    target = resolved["session_id"]
    title = frame.sanitize_title(resolved.get("title") or target)
    receipt.target_session, receipt.target_title = target, title
    row.update(target_session_id=target, target_title=title)
    if not script and target in _lineage_tips(sender):
        return _refuse(receipt, row, "own-session", f"{NOTHING_SENT} {OWN_SESSION_REASON}")

    # 2. scopes
    target_facts = _facts(target)
    target_scope = scope.chat_scope(target, ctx)
    sender_scope = None if script else scope.chat_scope(sender, ctx)
    receipt.shared_chat = target_scope == scope.SHARED
    row.update(target_chat_scope=target_scope, sender_chat_scope=sender_scope)

    # 3. consent, for every delivery and every target kind
    if not injection_allowed():
        return _refuse(receipt, row, "injection-off", INJECTION_OFF, result="error")

    # 4. the row before delivery
    record.insert(**row)
    if on_inserted is not None:
        on_inserted(message_id, target)

    # 5. frame, sanitise once, hash the span
    user_name = ctx.get_config("user_name", default=None) or DEFAULT_USER_NAME
    text, _echo = frame.build(
        message_id, who.frame_sender(), body, reply_mode=reply_mode,
        requested_interrupt=delivery == "interrupt", kind=kind, reply_to=reply_to,
        user_name=user_name,
        sender_scope=None if script else frame.Scope(sender_scope, who.platform, who.chat_type, who.title),
        target_scope=frame.Scope(target_scope, target_facts["platform"], target_facts["chat_type"], title),
    )
    from hermes_cli.input_sanitize import sanitize_user_prompt_text

    text = sanitize_user_prompt_text(text)
    record.update(message_id, header_sha=header_sha(text, message_id))

    # 6. deliver through the owner
    was_open = _is_live(target, lister)
    outcome = client.deliver_sync(target, text, delivery, client.submission_id(message_id))

    # 7. record and receipt
    if outcome.get("ok") is True:
        applied, applied_detail = outcome.get("applied"), outcome.get("applied_detail")
        result = "held" if applied == "queue" else "delivered"
        record.update(message_id, delivery_path="owner", admission_id=outcome.get("admission_id"),
                      applied=applied, applied_detail=applied_detail, result=result,
                      delivered_at=time.time())
        receipt.applied, receipt.applied_detail = applied, applied_detail
        sentence = _delivered_sentence(applied, applied_detail, title, message_id, was_open)
        return receipt.done(result, " ".join(
            part for part in (sentence, _weaker(delivery, applied),
                              _answer_sentence(reply_mode, who, message_id),
                              _shared_sentence(receipt.shared_chat)) if part))
    detail = outcome.get("error") or "delivery-unknown"
    record.update(message_id, delivery_path="owner", result="error", detail=detail)
    return receipt.error(detail, _error_sentence(detail, outcome.get("code"), title, message_id))


def injection_allowed() -> bool:
    """``plugins.entries.intercom.allow_gateway_injection`` is ``true``, read from the config now."""
    try:
        from hermes_cli.config import load_config_readonly

        config = load_config_readonly()
    except Exception:  # health: allow BLE001 -- a config failure leaves injection off
        return False
    plugins = config.get("plugins") if isinstance(config, dict) else None
    entries = plugins.get("entries") if isinstance(plugins, dict) else None
    entry = entries.get("intercom") if isinstance(entries, dict) else None
    return isinstance(entry, dict) and entry.get("allow_gateway_injection") is True


def header_sha(text: str, message_id: str) -> str:
    """SHA-256 of the framed span: header line 1 through ``📡 End of message <id>``."""
    closing = f"📡 End of message {message_id}"
    end = text.rindex(closing) + len(closing)
    return hashlib.sha256(text[:end].encode("utf-8")).hexdigest()


# ── internals ────────────────────────────────────────────────────────────────


class _Receipt:
    """The receipt fields, filled in as the steps run."""

    def __init__(self, *, to, requested, reply):
        self.to, self.requested, self.reply = to, requested, reply
        self.message_id = None
        self.target_title = None
        self.target_session = None
        self.applied = None
        self.applied_detail = None
        self.shared_chat = False

    def _fields(self, ok, receipt, message, error):
        line = message
        if self.target_session:
            line = _end_with_session(message, self.target_session)
        return {"ok": ok, "receipt": receipt, "to": self.to, "message_id": self.message_id,
                "target_title": self.target_title, "target_session": self.target_session,
                "reply": self.reply, "requested": self.requested, "applied": self.applied,
                "applied_detail": self.applied_detail, "shared_chat": self.shared_chat,
                "message": line, "error": error}

    def done(self, result, message):
        return self._fields(True, result, message, None)

    def error(self, detail, message):
        return self._fields(False, "refused", message, detail)

    def refused(self, detail, message, *, candidates=None, **override):
        for key, value in override.items():
            setattr(self, key, value)
        fields = self._fields(False, "refused", message, detail)
        if detail in ("ambiguous", "unknown"):
            fields["candidates"] = list(candidates or [])
        return fields


def _refuse(receipt, row, detail, text, *, candidates=None, result="refused"):
    """A refusal of steps 0-3: one ``refused`` row, nothing delivered."""
    record.insert(**row)
    record.update(row["message_id"], delivery_path="refused", result=result, detail=detail)
    return receipt.refused(detail, text, candidates=candidates)


def _end_with_session(message: str, session_id: str) -> str:
    """The readable line ends with ``(session <id>)``; candidate lines stay below it."""
    first, sep, rest = message.partition("\n")
    return f"{first} (session {session_id}){sep}{rest}"


class _Who:
    """The sender, for the record and the frame."""

    def __init__(self, *, kind, label, source, session_id=None, title="", platform="", chat_type=""):
        self.kind, self.label, self.source, self.session_id = kind, label, source, session_id
        self.title, self.platform, self.chat_type = title, platform, chat_type

    @classmethod
    def script(cls, label):
        label = frame.sanitize_title(label)
        return cls(kind="script", label=label, source=SCRIPT_SOURCE)

    @classmethod
    def session(cls, session_id):
        facts = _facts(session_id)
        source = facts["source"] or "unknown"
        kind = "session" if facts["local"] else "messaging"
        title = facts["title"] or session_id
        return cls(kind=kind, label=title, source=source, session_id=session_id, title=title,
                   platform=facts["platform"], chat_type=facts["chat_type"])

    def frame_sender(self):
        if self.kind == "script":
            return frame.Sender(kind="script", label=self.label)
        return frame.Sender(kind=self.kind, title=self.title, session_id=self.session_id,
                            platform=self.platform, chat_type=self.chat_type, source=self.source)


def _open_state():
    try:
        from hermes_state import SessionDB

        return SessionDB(read_only=True)
    except Exception:  # health: allow BLE001 -- state unreadable means the sender stays unknown
        return None


def _facts(session_id) -> dict:
    """Title, source, and for a messaging session its platform and chat type, from the row."""
    facts = {"title": "", "source": "", "local": True, "platform": "", "chat_type": ""}
    db = _open_state()
    if db is None:
        return facts
    try:
        row = db.get_session(session_id) or {}
    except Exception:  # health: allow BLE001 -- a session-row miss leaves the sender unknown
        row = {}
    finally:
        db.close()
    source = row.get("source") if isinstance(row.get("source"), str) else ""
    facts["title"] = row.get("title") if isinstance(row.get("title"), str) else ""
    facts["source"] = source
    facts["local"] = source in scope.LOCAL_SOURCES
    if not facts["local"]:
        origin = {}
        try:
            parsed = json.loads(row.get("origin_json") or "{}")
            origin = parsed if isinstance(parsed, dict) else {}
        except (TypeError, ValueError):
            pass
        platform = origin.get("platform") or source
        chat_type = origin.get("chat_type") or row.get("chat_type")
        facts["platform"] = platform if isinstance(platform, str) else ""
        facts["chat_type"] = chat_type if isinstance(chat_type, str) else ""
    return facts


def _lineage_tips(session_id) -> set:
    """The session id and its compression tip: either is the sender's own session."""
    ids = {session_id}
    db = _open_state()
    if db is None:
        return ids
    try:
        tip = db.get_compression_tip(session_id)
        if tip:
            ids.add(tip)
    except Exception:  # health: allow BLE001 -- a lineage miss still treats this session as its own
        logger.debug("intercom compression tip was not read")
    finally:
        db.close()
    return ids


def _is_live(target, lister) -> bool:
    """Whether the owner had *target* open before delivery (only for the started sentence)."""
    try:
        listing = (lister or client.list_sessions)("live", picker=False)
    except Exception:  # health: allow BLE001 -- an unknown listing still uses the started sentence
        return True
    if not isinstance(listing, dict) or listing.get("ok") is not True:
        return True
    ids = {row.get(field) for row in listing.get("sessions") or [] if isinstance(row, dict)
           for field in ("id", "session_id", "resolved_id")}
    ids = {value for value in ids if isinstance(value, str) and value}
    if target in ids:
        return True
    db = _open_state()
    if db is None:
        return False
    try:
        return any(db.get_compression_tip(value) == target for value in ids)
    except Exception:  # health: allow BLE001 -- a tip lookup miss is not counted as live
        return False
    finally:
        db.close()


def _candidate_line(candidate) -> str:
    if not isinstance(candidate, dict):
        return f"- {candidate}"
    parts = [f'"{candidate.get("title") or ""}"', str(candidate.get("id") or "")]
    last = candidate.get("last_active")
    if isinstance(last, (int, float)) and not isinstance(last, bool):
        parts.append("last active " + time.strftime("%Y-%m-%d %H:%M", time.localtime(last)))
    if candidate.get("availability"):
        parts.append(str(candidate["availability"]))
    return "- " + ", ".join(parts)


def _delivered_sentence(applied, applied_detail, title, message_id, was_open) -> str:
    if applied == "steer":
        return f'Message {message_id} was steered into the running turn of "{title}".'
    if applied == "queue":
        return f'"{title}" is busy. Message {message_id} is queued and runs after its current turn.'
    if applied == "interrupt" and applied_detail == "redirect":
        return f'Message {message_id} redirected the running turn of "{title}" and runs now.'
    if applied == "interrupt":
        return (f'Message {message_id} stopped the running turn of "{title}"; it runs as that '
                "session's next turn.")
    if not was_open:
        return cold_start_sentence(title, message_id)
    return f'"{title}" was idle. Message {message_id} started a turn there.'


def _weaker(requested, applied) -> str:
    if applied == "queue" and requested != "queue":
        return f"You asked for {requested}; this session could not take it now, so queue was used."
    return ""


def _answer_sentence(reply_mode, who, message_id) -> str:
    if reply_mode == "none":
        return "No answer was asked for, and none will come back."
    if reply_mode == "optional":
        return ""
    if who.kind == "script":
        return (f'Its answer will be stored in the record; read it with '
                f'"hermes intercom status {message_id}".')
    if who.source in _UNWAKEABLE:
        return ("Its answer will be stored in the record, not sent to you, because this session "
                f"is a {_UNWAKEABLE[who.source]}.")
    if reply_mode == "waiting":
        return ("You marked it as waiting: your call returns now, and the answer will come back "
                "as a new message.")
    return "Its answer will come back to you as a new message."


def _shared_sentence(shared) -> str:
    return "Its chat is shared with other people; the body is now in that chat's context." if shared else ""


def _error_sentence(detail, code, title, message_id) -> str:
    if detail == "not-addressable":
        return not_addressable_sentence(title)
    if detail == "refused-by-owner":
        return f"Not delivered: the gateway refused it ({code or 'refused'}). Nothing was sent."
    if detail in _ERROR_SENTENCES:
        return _ERROR_SENTENCES[detail]
    return (f"Delivery of message {message_id} is unknown: the gateway stopped answering after the "
            'message was handed over, so it may already have arrived. Run "hermes intercom status '
            f'{message_id}" and follow what it says: if it says "found", do not send again; if it '
            'says "not found (yet)", wait as it tells you and run it again; send again only when it '
            "says sending again will not duplicate it.")
