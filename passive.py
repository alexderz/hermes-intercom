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

A keyed queued message (``route.send(delivery="queue", key=...)``) is a ``pending`` row whose
send queued a framed wake line in Hermes's queue, or rides one already queued. The wake's turn
carries the newest version: ``on_pre_llm_call`` finds the wake span in ``user_message`` with the
reply hook's span finder and offers the newest pending row for that key, framed as live mail
(the turn exists for it). Outside its wake turn such a row is offered only when its wake is no
longer outstanding: a turn was already offered it, or its marker names another owner instance
(the owner restarted and Hermes's in-memory queue went with it). Then it rides as a held note.
The owner's instance id is discovered once per process.

A passive note with a deadline (``within``) is woken by ``deadline.sweep`` once it is due: a framed
wake line in Hermes's queue, its span hash on the note's row. The wake's turn finds that span the
same way, consumes the wake (``wake_turn``) and is offered the target's held notes, that one
included, framed as held notes. While its wake is live (claimed by the current owner instance and
not consumed) the note is offered to no other turn; a dead wake leaves it an ordinary held note.

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

# The owner instance this process runs in, discovered once (``client.owner_instance``).
_instance: dict[str, str | None] = {"id": None}


def register(ctx) -> None:
    """Keep *ctx* for settings and the echo, and register both hooks."""
    _ctx["ctx"] = ctx
    ctx.register_hook("pre_llm_call", on_pre_llm_call)
    ctx.register_hook("pre_api_request", on_pre_api_request)


def on_pre_llm_call(session_id=None, turn_id=None, user_message=None, **_kw):
    """Offer this turn its held notes. Returns ``{"context": <frame>}``, or None when none."""
    if not _usable(session_id) or not _usable(turn_id):
        return None
    try:
        if not route.injection_allowed():
            return None
        lineage, root = _lineage_and_root(session_id)
        facts = route._facts(session_id)
        found = replies.find_messages([replies._text(user_message)])
        keys = _wake_keys(found, lineage)
        notes = record.offer_notes(turn_id, lineage=sorted(lineage), root_id=root,
                                   include_all=facts["source"] not in address.RUN_SOURCES,
                                   wake_keys=sorted(keys), instance_id=_owner_instance(),
                                   woken=sorted(_deadline_wakes(found, lineage)))
        if not notes:
            return None
        ctx = _ctx["ctx"]
        woken = [n for n in notes if n["requested"] == record.QUEUE and n["note_key"] in keys]
        woken_ids = {n["id"] for n in woken}
        held = [n for n in notes if n["id"] not in woken_ids]
        parts = [route.frame_stored(n, ctx) for n in woken]
        if held:
            parts.append(frame.held_notes(held, replies._user_name(ctx),
                                          shared_line=_shared_line(held, session_id, facts, ctx)))
        text = "\n\n".join(parts)
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


def _wake_keys(found, lineage) -> set:
    """The keys of the keyed-queue wakes framed in this turn's prompt (*found*), for this lineage."""
    return {row["note_key"] for row, _span in found
            if row.get("wake_admission") and row.get("requested") == record.QUEUE
            and row.get("note_key") and row.get("target_session_id") in lineage}


def _deadline_wakes(found, lineage) -> set:
    """The deadline notes whose wake line this turn's prompt (*found*) carries, for this lineage."""
    return {row["id"] for row, _span in found
            if row.get("wake_admission") and row.get("requested") == record.PASSIVE
            and row.get("wake_at") is not None and row.get("target_session_id") in lineage}


def _owner_instance():
    """The owner's instance id, discovered once per process; None (every marker dead) until found."""
    if _instance["id"] is None:
        _instance["id"] = route.client.owner_instance()
    return _instance["id"]


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
