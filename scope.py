"""intercom chat scope — the one chat rule for messaging sessions (README, "Trust model").

``chat_scope(session_id, ctx)`` is ``"private"``, ``"shared"`` or ``None`` (a local session):

- origin: the session row, read with ``hermes_state.SessionDB(read_only=True)``. The stored
  ``origin_json`` when present, else the row's columns. Only the raw ``platform``, ``chat_id``,
  ``chat_type`` and ``user_id`` are used; there is no identity re-pin.
- local: ``sessions.source`` in ``LOCAL_SOURCES``. Anything else is a messaging platform.
- rule: ``private`` only when ``"<platform>:<chat_id>"`` is in ``settings.echo_chats``, or the chat
  type is ``dm`` and ``"<platform>:<user_id>"`` is in ``settings.owner_user_ids``. Every other
  messaging chat is ``shared``.
- entries are ``"<platform>:<id>"`` strings; a non-string or unprefixed entry is ignored with one
  warning, and never makes a chat private.
- settings are read with ``ctx.get_config`` on every call, so a removed chat takes effect at once.
- fail closed: a messaging session with no resolvable origin, any read error or a missing row is
  ``shared``. ``None`` only when the source was positively read as local.
"""

from __future__ import annotations

import json
import logging

logger = logging.getLogger(__name__)

PRIVATE = "private"
SHARED = "shared"

# Sources that never leave the machine. The owner's own local platform is ``local``.
LOCAL_SOURCES = frozenset(
    {"cli", "tui", "desktop", "gui", "acp", "tool", "oneshot", "cron", "subagent", "local"})

_warned: set[tuple[str, str]] = set()


def chat_scope(session_id: str, ctx) -> str | None:
    """The chat scope of stored session *session_id*, with *ctx*'s settings read now."""
    try:
        row = _read_row(session_id)
    except Exception as exc:  # health: allow BLE001 -- fail closed to shared; log the type only
        logger.warning("intercom: chat scope read failed (%s); treating the chat as shared",
                       type(exc).__name__)
        return SHARED
    if row is None:
        return SHARED
    source = row.get("source")
    if isinstance(source, str) and source in LOCAL_SOURCES:
        return None
    try:
        origin = _origin(row)
    except (ValueError, TypeError, KeyError, AttributeError):
        return SHARED  # a corrupt origin_json is no origin
    if origin is None:
        return SHARED
    try:
        return rule(*origin, ctx=ctx)
    except Exception as exc:  # health: allow BLE001 -- fail closed to shared; log the type only
        logger.warning("intercom: chat scope settings read failed (%s); treating the chat as shared",
                       type(exc).__name__)
        return SHARED


def rule(platform: str, chat_id: str, chat_type: str | None, user_id: str | None, *, ctx) -> str:
    """The chat rule for one messaging chat: ``private`` or ``shared``."""
    echo_chats = _entries(ctx, "echo_chats")
    if f"{platform}:{chat_id}" in echo_chats:
        return PRIVATE
    if chat_type == "dm" and user_id and f"{platform}:{user_id}" in _entries(ctx, "owner_user_ids"):
        return PRIVATE
    return SHARED


# ── internals ────────────────────────────────────────────────────────────────


def _read_row(session_id: str) -> dict | None:
    from hermes_state import SessionDB

    db = SessionDB(read_only=True)
    try:
        return db.get_session(session_id)
    finally:
        db.close()


def _origin(row: dict) -> tuple[str, str, str | None, str | None] | None:
    """``(platform, chat_id, chat_type, user_id)`` from the stored origin, or ``None``.

    Raises on an ``origin_json`` that is present but not a JSON object.
    """
    raw = row.get("origin_json")
    if raw:
        origin = json.loads(raw)
        if not isinstance(origin, dict):
            raise ValueError("origin_json is not an object")
        platform = origin.get("platform")
        fields = (origin.get("chat_id"), origin.get("chat_type"), origin.get("user_id"))
    else:
        platform = row.get("source")
        fields = (row.get("chat_id"), row.get("chat_type"), row.get("user_id"))
    chat_id, chat_type, user_id = (_text(v) for v in fields)
    platform = _text(platform)
    if not platform or not chat_id:
        return None
    return platform, chat_id, chat_type, user_id


def _text(value) -> str | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (str, int)):
        return str(value) or None
    return None


def _entries(ctx, key: str) -> frozenset[str]:
    """The valid ``"<platform>:<id>"`` entries of setting *key*, read fresh."""
    value = ctx.get_config(key, default=None)
    if value is None:
        return frozenset()
    if not isinstance(value, (list, tuple)):
        _warn(key, value)
        return frozenset()
    valid = set()
    for entry in value:
        if isinstance(entry, str) and _prefixed(entry):
            valid.add(entry)
        else:
            _warn(key, entry)
    return frozenset(valid)


def _prefixed(entry: str) -> bool:
    platform, sep, ident = entry.partition(":")
    return bool(sep and platform and ident)


def _warn(key: str, value) -> None:
    mark = (key, repr(value))
    if mark in _warned:
        return
    _warned.add(mark)
    logger.warning("intercom: ignoring settings.%s entry %r: not a \"<platform>:<id>\" string",
                   key, value)
