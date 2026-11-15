"""intercom send path — one message from a session or a script to one resolved target.

``send(...)`` runs these steps in order:

- inputs: a ``reply_mode`` outside ``none`` / ``requested`` / ``waiting`` is ``reply-missing``
  and records nothing. ``optional`` is only for a reply (``reply_to`` set, from the reply hook);
- usage: ``passive`` is checked before the live-delivery enum, then the combinations of
  ``key``, ``to: all``, ``reply`` and ``ask`` that cannot work are ``usage`` refusals: no
  message id, nothing recorded (CLI exit 2);
- 0 an empty or whitespace-only message is ``empty``;
- 1 ``all`` is matched first, like ``Home`` (a session titled "all" is reachable only by id);
  otherwise ``address.resolve(to)``: ``ambiguous`` / ``unknown`` with candidates, the project-addressing refusals,
  and ``own-session`` when the target is the sender's own session;
- 2 the chat scopes of the target and of a session sender (``scope.chat_scope``);
- 3 consent for **every** delivery: ``plugins.entries.intercom.allow_gateway_injection`` must
  be ``true``, read fresh from the profile config, else ``injection-off``;
- 4 ``record.insert`` before delivery. A passive note stops here: ``record.insert_note``
  stores it ``pending`` (replacing the held note with its key and mode in the target's
  lineage, or the pending ``all`` row) and the receipt is ``held``; nothing is delivered.
  A keyed queued message is stored ``pending`` too (``record.insert_keyed_queue``); it rides
  the outstanding wake of the row it replaces, else its send frames a wake line
  (``reply_mode='none'``, ``header_sha`` on its row) and queues it through steps 5-6. The
  target's next turn carries the newest version (``passive.py``). Receipt: ``started`` (idle
  target) or ``queued`` (replaceable, with the key); ``held`` when the owner did not take the
  wake, so the row rides the target's next turn;
- 5 ``frame.build``, ``sanitize_user_prompt_text`` once, then ``header_sha`` over the span from
  header line 1 to the closing line, stored in the row;
- 6 ``client.deliver_sync``;
- 7 ``record.update`` and the receipt.

Another profile (``profile``, passive.md P6): the usage check also refuses ``profile`` with
``to: all``, ``ask`` or ``@<project>``; an unknown or deleted profile, and one where intercom is
not enabled, are refused unrecorded. The sender's facts (its ``_Who`` and chat scope) are read
first, in its own profile; then steps 1-7 run inside ``profiles.in_profile(profile)``, so the
row lands in the target's record (``sender_profile`` set) and the target's config governs
consent, chat scope and the user name. The sender label reads ``<profile>/<title>`` and the
receipt names the target ``<profile>/<title>``. The sender's own profile is no ``profile``.

A deadline (``within``, passive.md P7): whole minutes, at least 5, on a per-target passive note
only; anything else is a usage refusal. The row gets ``wake_at = created_at + within * 60``; the
receipt says when it wakes. ``deadline.py`` fires it.

Refusals of steps 0-3 are recorded as ``delivery_path='refused'`` rows; ``reply-missing`` and the
usage refusals are not.

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
    from . import address, client, frame, profiles, record, scope
except ImportError:  # loaded as top-level modules (the unittest layout)
    import address
    import client
    import frame
    import profiles
    import record
    import scope

REPLY_MODES = ("none", "requested", "waiting")
PASSIVE = record.PASSIVE
ALL = record.ALL
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

# Usage refusals: each names the reason in plain words (passive.md, section 2).
KEY_WITH_LIVE = ("Nothing was sent: key works only with passive or queue. {delivery} hands the "
                 "message over at once, so there is no held message for a newer one to replace.")
KEY_WITH_ASK = ("Nothing was sent: ask cannot take a key. The asker waits on that exact message, "
                "so a replaced ask would leave it waiting until it times out.")
ASK_PASSIVE = ("Nothing was sent: ask cannot be passive. An ask waits at most an hour and a "
               "held note may wait days, so it would always time out. Use steer.")
ALL_NEEDS_PASSIVE_KEY = ("Nothing was sent: to: all needs delivery: passive and a key. An unkeyed "
                         "or waking message to every session is a general notification, which "
                         "session mail does not send.")
ALL_NEEDS_REPLY_NONE = ("Nothing was sent: to: all needs reply: none. One note would ask every "
                        "session to answer you.")
PROFILE_WITH_ALL = ("Nothing was sent: to: all cannot take a profile. all reaches the sessions of "
                    "your own profile only.")
PROFILE_WITH_ASK = ("Nothing was sent: ask cannot take a profile. The asker waits in its own "
                    "process, and an answer from another profile would never find it. Use send "
                    "with reply: waiting.")
PROFILE_WITH_PROJECT = ("Nothing was sent: @<project> cannot take a profile. Address a session in "
                        "another profile by id, exact title or Home.")
UNKNOWN_PROFILE = "Nothing was sent: there is no Hermes profile named {name} on this machine."
PROFILE_NOT_ENABLED = ("Nothing was sent: session mail is not enabled in profile {name}, so its "
                       "replies, echo and framing protection would not happen there.")
INJECTION_OFF_IN = ("Not delivered: intercom may not deliver messages in Hermes profile {name} "
                    "(allow_gateway_injection is off). Nothing was sent.")
WITHIN_WITH_ALL = ("Nothing was sent: to: all cannot take within. A deadline on a note to all "
                   "would wake every session.")
WITHIN_WITH_LIVE = ("Nothing was sent: within works only with passive. {delivery} hands the "
                    "message over at once, so there is nothing to wake the target for later.")
WITHIN_INVALID = ("Nothing was sent: within is a whole number of minutes, at least {minimum}.")
MIN_WITHIN = 5
PASSIVE_WAITING = ("Nothing was sent: passive cannot use reply: waiting. waiting asks the target "
                   "to answer first, but a held note is dealt with after what its turn is for. "
                   "Use reply: requested, or steer.")


def not_addressable_sentence(title: str) -> str:
    return f'"{title}" is open in the admin dashboard, which intercom cannot reach. Nothing was sent.'


def cold_start_sentence(title: str, message_id: str) -> str:
    return (f'"{title}" was not open, so the gateway reopened it and message {message_id} '
            "started a turn there.")


def send(sender, to, message, *, ctx, delivery="steer", reply_mode=None, kind="send",
         reply_to=None, script=False, lister=None, on_inserted=None, key=None,
         profile=None, within=None) -> dict:
    """Send *message* from *sender* to the one session *to* resolves to. Returns the receipt.

    *sender* is the calling session's id, or with ``script=True`` a script label (the CLI, which
    always frames as a script and never names a session as sender). *ctx* reads the plugin's
    settings. *lister* is ``session.list`` (tests pass their own); it decorates candidates and
    tells a cold start from an idle one. *on_inserted* is called with the message id and the
    target session once the row exists, before delivery (the ``ask`` waiter). *key* (with
    ``passive`` or ``queue``) lets a newer message with the same key replace a held one.
    *profile* names another Hermes profile on this machine to deliver into. *within* (minutes,
    passive only) is the note's deadline: the sweep in ``deadline.py`` wakes the target then if
    the note is still held.
    """
    passive = delivery == PASSIVE
    if not passive and delivery not in client.DELIVERIES:
        raise ValueError(f"delivery must be one of {', '.join((*client.DELIVERIES, PASSIVE))}")
    reply_to = reply_to or None
    key = key.strip() if isinstance(key, str) else None
    key = key or None
    to_all = isinstance(to, str) and to.strip() == ALL
    receipt = _Receipt(to=to, requested=delivery, reply=reply_mode)
    if not (reply_mode in REPLY_MODES or (reply_mode == "optional" and reply_to)):
        return receipt.refused("reply-missing", REPLY_MISSING, reply=None)
    profile = profiles.other(profile)
    usage = _usage_error(delivery, key, to_all, reply_mode, kind, profile=profile, to=to,
                         within=within)
    if usage:
        refused = receipt.refused("usage", usage)
        refused["exit"] = 2
        return refused
    if profile is not None and profiles.home_for(profile) is None:
        return receipt.refused("unknown-profile",
                               UNKNOWN_PROFILE.format(name=frame.sanitize_title(profile)))

    message_id = secrets.token_hex(4)
    receipt.message_id = message_id
    body = message if isinstance(message, str) else ""
    who = _Who.script(sender) if script else _Who.session(sender)
    row = dict(message_id=message_id, kind=kind, sender_label=who.label, sender_source=who.source,
               sender_session_id=who.session_id, address=to if isinstance(to, str) else "",
               body=body, reply_mode=reply_mode, requested=delivery, in_reply_to=reply_to,
               note_key=key)
    if within is not None:
        row["created_at"] = time.time()
        row["wake_at"] = row["created_at"] + _minutes(within) * 60
    steps = dict(sender=sender, to=to, ctx=ctx, delivery=delivery, reply_mode=reply_mode,
                 kind=kind, reply_to=reply_to, script=script, lister=lister,
                 on_inserted=on_inserted)
    if profile is None:
        return _steps(receipt, row, who, **steps)

    # Another profile: the sender's facts first, in its own profile; then the whole target side.
    sender_scope = None if script else scope.chat_scope(sender, ctx)
    own = profiles.current() or profiles.DEFAULT
    who = who.in_profile(own)
    row.update(sender_label=who.label, sender_profile=own)
    with profiles.in_profile(profile):
        if not profiles.enabled():
            receipt.message_id = None
            return receipt.refused("profile-not-enabled",
                                   PROFILE_NOT_ENABLED.format(name=profile))
        steps["ctx"] = profiles.Settings()
        return _steps(receipt, row, who, profile=profile, sender_scope=sender_scope, **steps)


_UNREAD = object()


def _steps(receipt, row, who, *, sender, to, ctx, delivery, reply_mode, kind, reply_to, script,
           lister, on_inserted, profile=None, sender_scope=_UNREAD) -> dict:
    """Steps 0-7 for one message, in the active profile (the target's, under ``in_profile``).

    *profile* is the target profile when it is not the sender's; *sender_scope* is then
    already read, in the sender's profile.
    """
    passive = delivery == PASSIVE
    key = row["note_key"]
    to_all = row["address"].strip() == ALL
    message_id, body = row["message_id"], row["body"]
    injection_off = INJECTION_OFF if profile is None else INJECTION_OFF_IN.format(name=profile)

    # 0. empty
    if not body.strip():
        return _refuse(receipt, row, "empty", f"{NOTHING_SENT} {EMPTY_REASON}")

    # 1. all (before addressing, like Home), resolve, then own-session
    if to_all:
        row.update(address=ALL)
        receipt.target_title = ALL
        if not injection_allowed():
            return _refuse(receipt, row, "injection-off", injection_off, result="error")
        return _hold(receipt, row, who, lineage=())
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
    # The receipt names a session of another profile as <profile>/<title>.
    shown = title if profile is None else f"{profile}/{title}"
    receipt.target_session, receipt.target_title = target, shown
    row.update(target_session_id=target, target_title=title)
    if not script and profile is None and target in _lineage_tips(sender):
        return _refuse(receipt, row, "own-session", f"{NOTHING_SENT} {OWN_SESSION_REASON}")

    # 2. scopes
    target_scope = scope.chat_scope(target, ctx)
    if sender_scope is _UNREAD:
        sender_scope = None if script else scope.chat_scope(sender, ctx)
    receipt.shared_chat = target_scope == scope.SHARED
    row.update(target_chat_scope=target_scope, sender_chat_scope=sender_scope)

    # 3. consent, for every delivery and every target kind
    if not injection_allowed():
        return _refuse(receipt, row, "injection-off", injection_off, result="error")

    # 4. the row before delivery; a passive note is held here
    if passive:
        return _hold(receipt, row, who, lineage=_target_lineage(target))
    if delivery == "queue" and key:
        return _queue_keyed(receipt, row, who, ctx=ctx, lister=lister, shown=shown,
                            scopes=(sender_scope, target, target_scope, title))
    record.insert(**row)
    if on_inserted is not None:
        on_inserted(message_id, target)

    # 5. frame, sanitise once, hash the span
    text = _framed(message_id, who, body, ctx=ctx, reply_mode=reply_mode,
                   interrupt=delivery == "interrupt", kind=kind, reply_to=reply_to,
                   scopes=(sender_scope, target, target_scope, title))
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
        sentence = _delivered_sentence(applied, applied_detail, shown, message_id, was_open)
        return receipt.done(result, " ".join(
            part for part in (sentence, _weaker(delivery, applied),
                              _answer_sentence(reply_mode, who, message_id),
                              _shared_sentence(receipt.shared_chat)) if part))
    detail = outcome.get("error") or "delivery-unknown"
    record.update(message_id, delivery_path="owner", result="error", detail=detail)
    return receipt.error(detail, _error_sentence(detail, outcome.get("code"), shown, message_id))


def frame_stored(row: dict, ctx, *, body=None, reply_mode=None) -> str:
    """The live frame of a stored row, as its send would have framed it (sanitised).

    For a keyed queued message handed over in its wake's turn: that turn exists for it. With
    *body* and *reply_mode*, another text framed as this row (a deadline note's wake line).
    """
    if row.get("sender_source") == SCRIPT_SOURCE or not row.get("sender_session_id"):
        who = _Who.script(row.get("sender_label"))
    elif row.get("sender_profile"):
        who = _Who.of_profile(row["sender_session_id"], row["sender_profile"], row)
    else:
        who = _Who.session(row["sender_session_id"])
    sender_scope = None if who.kind == "script" else row.get("sender_chat_scope")
    text = (row.get("body") or "") if body is None else body
    return _framed(row["id"], who, text, ctx=ctx, reply_mode=reply_mode or row["reply_mode"],
                   scopes=(sender_scope, row.get("target_session_id"), row.get("target_chat_scope"),
                           row.get("target_title") or row.get("target_session_id")))


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
    """SHA-256 of the framed span: header line 1 through ``📡 End of message <id>``.

    The notice line before the header (``frame.NOTICE``) is not in the span.
    """
    closing = f"📡 End of message {message_id}"
    start = text.index("\n") + 1 if text.startswith(frame.NOTICE) else 0
    end = text.rindex(closing) + len(closing)
    return hashlib.sha256(text[start:end].encode("utf-8")).hexdigest()


# ── internals ────────────────────────────────────────────────────────────────


def _usage_error(delivery, key, to_all, reply_mode, kind, *, profile=None, to=None,
                 within=None) -> str | None:
    """The reason a combination cannot work, or None. Checked before a message id exists."""
    passive = delivery == PASSIVE
    if within is not None:
        if to_all:
            return WITHIN_WITH_ALL
        if not passive:
            return WITHIN_WITH_LIVE.format(delivery=delivery)
        minutes = _minutes(within)
        if minutes is None or minutes < MIN_WITHIN:
            return WITHIN_INVALID.format(minimum=MIN_WITHIN)
    if profile is not None:
        if to_all:
            return PROFILE_WITH_ALL
        if kind == "ask":
            return PROFILE_WITH_ASK
        if isinstance(to, str) and to.strip().startswith("@"):
            return PROFILE_WITH_PROJECT
    if kind == "ask" and key:
        return KEY_WITH_ASK
    if kind == "ask" and passive:
        return ASK_PASSIVE
    if key and delivery in ("steer", "interrupt"):
        return KEY_WITH_LIVE.format(delivery=delivery)
    if to_all and not (passive and key):
        return ALL_NEEDS_PASSIVE_KEY
    if to_all and reply_mode != "none":
        return ALL_NEEDS_REPLY_NONE
    if passive and reply_mode == "waiting":
        return PASSIVE_WAITING
    return None


def _minutes(within) -> int | None:
    """*within* as whole minutes (an integer, or a string of digits), else None."""
    if isinstance(within, bool):
        return None
    if isinstance(within, int):
        return within
    if isinstance(within, float) and within.is_integer():
        return int(within)
    if isinstance(within, str) and within.strip().isdigit():
        return int(within.strip())
    return None


def wake_clock(stamp) -> str:
    """``HH:MM`` local time of a deadline, as the receipt and status show it."""
    return time.strftime("%H:%M", time.localtime(stamp))


def _framed(message_id, who, body, *, ctx, reply_mode, scopes, interrupt=False, kind="send",
            reply_to=None) -> str:
    """Step 5's frame, sanitised once. *scopes* is (sender scope, target, target scope, title)."""
    sender_scope, target, target_scope, title = scopes
    target_facts = _facts(target)
    user_name = ctx.get_config("user_name", default=None) or DEFAULT_USER_NAME
    text, _echo = frame.build(
        message_id, who.frame_sender(), body, reply_mode=reply_mode,
        requested_interrupt=interrupt, kind=kind, reply_to=reply_to, user_name=user_name,
        sender_scope=None if who.kind == "script" else frame.Scope(sender_scope, who.platform,
                                                                    who.chat_type, who.title),
        target_scope=frame.Scope(target_scope, target_facts["platform"], target_facts["chat_type"], title),
    )
    from hermes_cli.input_sanitize import sanitize_user_prompt_text

    return sanitize_user_prompt_text(text)


def _queue_keyed(receipt, row, who, *, ctx, lister, scopes, shown) -> dict:
    """A keyed queued message: stored ``pending``; it rides an outstanding wake or queues one."""
    message_id = row.pop("message_id")
    target = scopes[1]
    title = shown
    key = row["note_key"]
    wake = client.submission_id(message_id)
    replaced, submit = record.insert_keyed_queue(
        message_id, lineage=_target_lineage(target), wake_admission=wake,
        instance_id=client.owner_instance(), **row)
    queued = (f'"{title}" is busy. Message {message_id} is queued (replaceable, key {key}): a newer '
              "message with this key replaces it until its turn starts.")
    result, applied = "queued", "queue"
    if not submit:
        sentence = f"{queued} It rides the wake already queued there."
    else:
        line = f"keyed update `{key}` from `{who.label}`; the current version follows."
        text = _framed(message_id, who, line, ctx=ctx, reply_mode="none", scopes=scopes)
        record.update(message_id, header_sha=header_sha(text, message_id))
        was_open = _is_live(target, lister)
        outcome = client.deliver_sync(target, text, "queue", wake)
        if outcome.get("ok") is True:
            applied = outcome.get("applied")
            record.update(message_id, delivery_path="owner", admission_id=outcome.get("admission_id"),
                          applied=applied, applied_detail=outcome.get("applied_detail"))
            record.wake_submitted(wake, outcome.get("instance_id"))
            if applied == "queue":
                sentence = queued
            else:
                result = "started"
                sentence = _delivered_sentence(applied, None, title, message_id, was_open)
        else:
            record.wake_failed(wake)
            result, applied = "held", None
            sentence = (f'Message {message_id} is held for "{title}" until its next turn: the gateway '
                        f"did not take its wake ({outcome.get('error') or 'delivery-unknown'}).")
    receipt.applied = applied
    parts = [sentence]
    if replaced:
        parts.append(f"It replaces queued message {', '.join(replaced)} (key {key}).")
    parts += [_answer_sentence(row["reply_mode"], who, message_id), _shared_sentence(receipt.shared_chat)]
    fields = receipt.done(result, " ".join(part for part in parts if part))
    fields["key"] = key
    fields["replaced"] = replaced
    return fields


def _hold(receipt, row, who, *, lineage) -> dict:
    """Store a passive note ``pending``: nothing is framed or delivered until its next turn."""
    replaced = record.insert_note(row.pop("message_id"), lineage=lineage, **row)
    message_id = receipt.message_id
    if row.get("address") == ALL:
        sentence = (f"Message {message_id} is held for all sessions of this profile; each gets it "
                    "at its next turn. It wakes none of them.")
    else:
        sentence = (f'Message {message_id} is held for "{receipt.target_title}" until its next '
                    "turn. It does not wake it.")
        if row.get("wake_at") is not None:
            sentence = (f'Message {message_id} is held for "{receipt.target_title}" until its next '
                        f"turn. If that has not run by {wake_clock(row['wake_at'])}, it is woken "
                        "then.")
    parts = [sentence]
    if replaced:
        parts.append(f"It replaces held message {', '.join(replaced)} (key {row['note_key']}).")
    elif row.get("note_key"):
        parts.append(f"A newer message with key {row['note_key']} will replace it while it is held.")
    parts.append(_answer_sentence(row["reply_mode"], who, message_id))
    fields = receipt.done("held", " ".join(part for part in parts if part))
    fields["key"] = row.get("note_key")
    fields["replaced"] = replaced
    fields["wake_at"] = row.get("wake_at")
    return fields


def _target_lineage(target) -> list:
    """The target and its compression lineage: "same target" for keyed replacement."""
    try:
        from . import replies
    except ImportError:  # loaded as top-level modules (the unittest layout)
        import replies
    return sorted(replies._lineage(target))


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

    def __init__(self, *, kind, label, source, session_id=None, title="", platform="", chat_type="",
                 profile=""):
        self.kind, self.label, self.source, self.session_id = kind, label, source, session_id
        self.title, self.platform, self.chat_type = title, platform, chat_type
        self.profile = profile

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
                   platform=facts["platform"], chat_type=facts["chat_type"],
                   profile=profiles.current() or profiles.DEFAULT)

    def in_profile(self, profile):
        """This sender as another profile sees it: ``<profile>/<title>``."""
        label = frame.sanitize_title(f"{profile}/{self.title or self.label}")
        return _Who(kind=self.kind, label=label, source=self.source, session_id=self.session_id,
                    title=label, platform=self.platform, chat_type=self.chat_type, profile=profile)

    @classmethod
    def of_profile(cls, session_id, profile, row):
        """A sender of *row* in another profile: its facts read there, else the row's label."""
        try:
            with profiles.in_profile(profile):
                return cls.session(session_id).in_profile(profile)
        except LookupError:  # that profile is gone: the stored label still names the sender
            source = row.get("sender_source") or "unknown"
            label = frame.sanitize_title(row.get("sender_label"))
            return cls(kind="session" if source in scope.LOCAL_SOURCES else "messaging",
                       label=label, source=source, session_id=session_id, title=label,
                       profile=profile)

    def frame_sender(self):
        if self.kind == "script":
            return frame.Sender(kind="script", label=self.label)
        return frame.Sender(kind=self.kind, title=self.title, session_id=self.session_id,
                            platform=self.platform, chat_type=self.chat_type, source=self.source,
                            profile=self.profile)


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
