"""intercom peer inbound — the ``intercom_peer`` platform (peers.md, P1).

A remote agent posts to a Hermes webhook route ``peer-<name>`` with ``deliver: intercom_peer``,
``deliver_only: true``, ``deliver_extra.chat_id: <name>`` and the prompt :data:`TEMPLATE`. The
webhook renders the prompt and hands it to this adapter's ``send``
(``_direct_deliver`` → ``_deliver_cross_platform`` → ``adapter.send``); ``success=False`` or an
exception is the post's 502.

``send`` runs :func:`_deliver_inbound` on a worker thread, never on the gateway loop. That
function, in the default profile:

- refuses a ``chat_id`` that is not a name in ``peers.json`` (``peers.load``);
- parses the rendered prompt; a key the payload lacked stays the literal ``{key}`` and counts as
  absent, and a missing or empty ``text`` is a failure;
- picks the reply mode: ``wants_reply`` true is ``requested``; a valid ``reply_to`` (an outbound
  row addressed ``peer:<name>``) without it is ``optional``; otherwise ``none``. An invalid
  ``reply_to`` is dropped;
- sends to the exact title ``Bot Chat`` with ``route.send(peer=<name>)``, steered. Nothing in the
  payload can name another target.

``validate_target_ref_fn`` refuses every target, so ``send_message``, the CLI and cron can never
deliver through this platform; only the webhook's direct path reaches ``send``.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Optional

from gateway.config import Platform
from gateway.platforms.base import BasePlatformAdapter, SendResult

try:
    from . import peers, profiles, record, route
except ImportError:  # loaded as top-level modules (the unittest layout)
    import peers
    import profiles
    import record
    import route

logger = logging.getLogger(__name__)

PLATFORM = "intercom_peer"
LABEL = "Intercom remote agents"
TARGET = "Bot Chat"
TEMPLATE = "reply_to={reply_to}\nwants_reply={wants_reply}\n{text}"
INBOUND_ONLY = "intercom_peer is inbound-only (webhook routes)"


def register(ctx) -> None:
    """Register the ``intercom_peer`` platform (``ctx.register_platform``)."""
    ctx.register_platform(
        name=PLATFORM,
        label=LABEL,
        adapter_factory=PeerAdapter,
        check_fn=_always,
        is_connected=_always,
        validate_target_ref_fn=_refuse_target,
        install_hint="Part of the intercom plugin; no extra packages.",
        emoji="📡",
        allow_update_command=False,
    )


class PeerAdapter(BasePlatformAdapter):
    """Inbound only: ``send`` delivers a remote agent's post into the canonical Bot Chat."""

    def __init__(self, config, **_kwargs):
        super().__init__(config=config, platform=Platform(PLATFORM))

    async def connect(self, *, is_reconnect: bool = False) -> bool:
        self._mark_connected()
        return True

    async def disconnect(self) -> None:
        self._mark_disconnected()

    async def send(self, chat_id: str, content: str, reply_to: Optional[str] = None,
                   metadata: Optional[dict[str, Any]] = None) -> SendResult:
        try:
            ok, reason = await asyncio.to_thread(_deliver_inbound, chat_id, content)
        except Exception as exc:  # health: allow BLE001 -- log the type only; the body may be in it
            ok, reason = False, type(exc).__name__
        if ok:
            return SendResult(success=True)
        logger.warning("intercom: remote agent post not delivered (%s)", reason)
        return SendResult(success=False, error=reason)

    async def get_chat_info(self, chat_id: str) -> dict[str, Any]:
        return {"name": str(chat_id), "type": "dm"}


def _always(*_args) -> bool:
    return True


def _refuse_target(_target_ref) -> str:
    return INBOUND_ONLY


def _deliver_inbound(chat_id, content) -> tuple[bool, str]:
    """Deliver one rendered post. ``(True, "")`` on a delivered receipt, else ``(False, reason)``.

    The reason names what failed, never the body or a secret.
    """
    with profiles.in_profile(profiles.DEFAULT):
        name = chat_id if isinstance(chat_id, str) else ""
        if name not in peers.load():
            return False, "unknown remote agent"
        parsed = parse(content)
        if parsed is None:
            return False, "malformed post"
        reply_to, wants_reply, text = parsed
        if reply_to is not None and not _is_outbound_to(reply_to, name):
            reply_to = None
        if wants_reply:
            reply_mode = "requested"
        elif reply_to is not None:
            reply_mode = "optional"
        else:
            reply_mode = "none"
        receipt = route.send(None, TARGET, text, ctx=profiles.Settings(), delivery="steer",
                             reply_mode=reply_mode, reply_to=reply_to, peer=name)
    if receipt.get("ok") is True:
        return True, ""
    return False, f"message {receipt.get('message_id') or '-'}: {receipt.get('error') or 'refused'}"


def parse(content) -> tuple[str | None, bool, str] | None:
    """``(reply_to, wants_reply, text)`` from a prompt rendered from :data:`TEMPLATE`, or None.

    A literal ``{reply_to}`` or ``{wants_reply}`` is an absent key; a literal ``{text}`` (no text in
    the payload) or an empty text is None.
    """
    if not isinstance(content, str):
        return None
    lines = content.split("\n", 2)
    if len(lines) != 3:
        return None
    first, second, text = lines
    if not (first.startswith("reply_to=") and second.startswith("wants_reply=")):
        return None
    reply_to = _present(first.removeprefix("reply_to="), "reply_to")
    wants = _present(second.removeprefix("wants_reply="), "wants_reply")
    if text.strip() in ("", "{text}"):
        return None
    return reply_to, (wants or "").strip().casefold() == "true", text


def _present(value: str, key: str) -> str | None:
    value = value.strip()
    return None if value in ("", f"{{{key}}}") else value


def _is_outbound_to(message_id: str, name: str) -> bool:
    """*message_id* is a row of this profile's record addressed to remote agent *name*."""
    row = record.get(message_id)
    return isinstance(row, dict) and row.get("address") == f"peer:{name}"
