"""intercom deadline — wake the target of a held note whose deadline passed (passive.md P7).

A per-target passive note sent with ``within`` has ``wake_at``. ``sweep()`` wakes the target of
each such note still held once that time has passed, once, through Hermes's queue; the wake's
turn carries the note (``passive.py``). A note delivered earlier causes no wake.

Who runs it: the gateway, from the published ``on_kanban_dispatch_tick`` hook
(``on_dispatch_tick``). The handler ignores ``dry_run`` ticks, starts one daemon thread for the
sweep and returns at once, so the dispatcher's tick is never blocked; while a sweep runs, later
ticks start none (a process-wide non-blocking lock). The hook reaches the launch profile's
plugin only, so the sweep visits every profile itself. ``hermes intercom wake-due`` runs the same
sweep in the foreground. Nothing is scheduled or installed.

For each profile (the root, then every live ``<root>/profiles/<name>`` whose config enables
intercom), inside ``profiles.in_profile(name)``:

1. discover that profile's owner once (``client._discover``); with none, skip the profile;
2. the due notes (``record.due_notes``): passive, ``pending``, not delivered, ``wake_at`` passed,
   wake never consumed, and no live wake (a marker naming the discovered owner instance). A
   marker of another instance is dead: that wake went with the owner's in-memory queue;
3. per note, oldest first: claim it (``record.claim_wake``, its own ``BEGIN IMMEDIATE``, only
   while still due); with ``allow_gateway_injection`` off, release the claim and leave it held;
   else frame the wake line (``reply_mode='none'``, ``header_sha`` on the note's row) to the
   target's compression tip and submit it with ``client.deliver_sync(..., "queue", ...)``;
   record the admission id (``record.wake_fired``). A failed delivery releases the claim and
   logs one line. The sweep never raises.
"""

from __future__ import annotations

import logging
import secrets
import threading
import time

try:
    from . import address, client, profiles, record, route
except ImportError:  # loaded as top-level modules (the unittest layout)
    import address
    import client
    import profiles
    import record
    import route

logger = logging.getLogger(__name__)

WAKE_LINE = "a held note from `{sender}` is due; it follows."

# Held while a sweep runs: a tick that cannot take it returns without starting another.
_running = threading.Lock()


def register(ctx) -> None:
    """Register the dispatcher-tick hook."""
    ctx.register_hook("on_kanban_dispatch_tick", on_dispatch_tick)


def on_dispatch_tick(dry_run=False, **_kw) -> None:
    """Start one sweep on a daemon thread and return; nothing for a dry run or while one runs."""
    if dry_run is True or not _running.acquire(blocking=False):
        return
    try:
        threading.Thread(target=_sweep_then_release, name="intercom-deadline", daemon=True).start()
    except Exception as exc:  # health: allow BLE001 -- a tick hook never raises into the dispatcher
        _running.release()
        logger.warning("intercom: deadline sweep did not start (%s)", type(exc).__name__)


def sweep(now: float | None = None) -> bool:
    """Wake every due deadline note of every profile once. False only when the profiles could
    not be listed; a failing profile or note is logged and skipped. Never raises."""
    try:
        names = _profiles()
    except Exception as exc:  # health: allow BLE001 -- the sweep never raises
        logger.warning("intercom: deadline sweep could not list profiles (%s)", type(exc).__name__)
        return False
    for name in names:
        try:
            with profiles.in_profile(name):
                if profiles.enabled():
                    _sweep_profile(now)
        except Exception as exc:  # health: allow BLE001 -- one profile's failure skips that profile
            logger.warning("intercom: deadline sweep of profile %s failed (%s)", name,
                           type(exc).__name__)
    return True


# ── internals ────────────────────────────────────────────────────────────────


def _sweep_then_release() -> None:
    try:
        sweep()
    finally:
        _running.release()


def _profiles() -> list[str]:
    """``default`` (the root), then each live named profile, by name."""
    from hermes_constants import get_default_hermes_root

    names = [profiles.DEFAULT]
    base = get_default_hermes_root() / "profiles"
    if base.is_dir():
        names += sorted(entry.name for entry in base.iterdir()
                        if entry.name != profiles.DEFAULT and profiles.home_for(entry.name) is not None)
    return names


def _sweep_profile(now) -> None:
    """The active profile's due notes, with its owner discovered once."""
    endpoint, failure = client._discover()
    instance = getattr(endpoint, "instance_id", None) if failure is None else None
    if not isinstance(instance, str) or not instance:
        return
    for row in record.due_notes(instance, now):
        try:
            _fire(row, instance, now)
        except Exception as exc:  # health: allow BLE001 -- log the type only; a note body can be in the traceback
            logger.warning("intercom: deadline wake of message %s failed (%s)", row["id"],
                           type(exc).__name__)


def _fire(row: dict, instance: str, now) -> None:
    message_id = row["id"]
    wake = client.submission_id(f"{message_id}-{secrets.token_hex(2)}")
    if not record.claim_wake(message_id, instance_id=instance, wake_admission=wake,
                             now=time.time() if now is None else now):
        return  # another sweep has it, or it stopped being due
    if not route.injection_allowed():
        record.release_wake(message_id, wake)
        return
    try:
        tip = _tip(row["target_session_id"])
        line = WAKE_LINE.format(sender=row.get("sender_label") or "")
        text = route.frame_stored({**row, "target_session_id": tip}, profiles.Settings(),
                                  body=line, reply_mode="none")
        record.update(message_id, header_sha=route.header_sha(text, message_id))
        outcome = client.deliver_sync(tip, text, "queue", wake)
    except Exception as exc:  # health: allow BLE001 -- log the type only; the frame carries the sender
        record.release_wake(message_id, wake)
        logger.warning("intercom: deadline wake of message %s not delivered (%s)", message_id,
                       type(exc).__name__)
        return
    if outcome.get("ok") is True:
        record.wake_fired(message_id, wake, outcome.get("admission_id"), outcome.get("instance_id"))
        return
    record.release_wake(message_id, wake)
    logger.warning("intercom: deadline wake of message %s not delivered (%s)", message_id,
                   outcome.get("error") or "delivery-unknown")


def _tip(session_id: str) -> str:
    """The target's compression tip now (``address._tip``)."""
    db = route._open_state()
    try:
        return address._tip(db, session_id)
    finally:
        if db is not None:
            db.close()
