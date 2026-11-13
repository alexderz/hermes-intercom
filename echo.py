"""intercom echo — a message to a private chat is also posted into that chat.

See the README, "Trust model", for why private chats get an echo and shared ones do not.

``ctx.post_message`` posts into the session bound to the calling context, so the echo is posted
from the target's own turn, by a hook that runs for that turn and sees the message:

- queued or started messages: ``pre_llm_call`` of the turn whose prompt carries the header
  (``on_pre_llm_call``). A steer that lands after the last request becomes a later turn, and is
  echoed here too;
- steered messages: the first ``pre_api_request`` whose ``request_messages`` carry the steer row
  (``on_pre_api_request``): a steer joins a running turn, so no new prompt carries it. It fires before every request, the final answer's included.

Both hooks read only the user rows after the newest assistant row, enough to find a ``📡`` span
and check its hash (``replies.find_messages``, the reply hook's span finder). Nothing from the turn is kept or logged.
A message is echoed only when the row's ``target_chat_scope`` is ``private``. The post is claimed
once (``record.claim_echo``), so retries and later requests never post twice. The text is escaped
for the platform's renderer (``frame.escape_echo``); a platform missing from
``frame.ECHO_ESCAPERS`` gets no post and one warning: unescaped text could ping or format in that chat. The hook schedules
``ctx.post_message`` on a thread and returns: a failed or refused post is logged only, and
delivery is never affected. A shared chat never gets a post: other people read it.
"""

from __future__ import annotations

import contextvars
import logging
import threading

try:
    from . import frame, record, replies, route, scope
except ImportError:  # loaded as top-level modules (the unittest layout)
    import frame
    import record
    import replies
    import route
    import scope

logger = logging.getLogger(__name__)

_ctx = {"ctx": None}


def register(ctx) -> None:
    """Keep *ctx* for the post, and register both hooks."""
    _ctx["ctx"] = ctx
    ctx.register_hook("pre_llm_call", on_pre_llm_call)
    ctx.register_hook("pre_api_request", on_pre_api_request)


def on_pre_llm_call(user_message=None, platform=None, **_kw) -> None:
    """Queued or started messages, and late steers: the turn's prompt."""
    _echo([replies._text(user_message)], platform)


def on_pre_api_request(request_messages=None, platform=None, **_kw) -> None:
    """Steered messages: the user rows after the newest assistant row of this request."""
    _echo(new_user_rows(request_messages), platform)


def new_user_rows(request_messages) -> list[str]:
    """The text of the user rows after the newest assistant row."""
    rows = []
    for message in reversed(request_messages or []):
        if not isinstance(message, dict):
            continue
        role = message.get("role")
        if role == "assistant":
            break
        if role == "user":
            rows.append(replies._text(message.get("content")))
    rows.reverse()
    return rows


def echo_text(row: dict, span, user_name: str) -> str:
    """The echo: header line 1, a blank line and the relabelled body, as ``frame.build`` made it."""
    header = span.text.split("\n", 1)[0]
    body = frame.relabel(frame._strip_echo_headers(row.get("body") or ""), frame.sanitize_title(user_name))
    return header if not body else f"{header}\n\n{body}"


# ── internals ────────────────────────────────────────────────────────────────


def _echo(texts, platform) -> None:
    try:
        if not any("📡" in text for text in texts):
            return
        for row, span in replies.find_messages(texts):
            if row.get("target_chat_scope") != scope.PRIVATE:
                continue
            if not record.claim_echo(row["id"]):
                continue
            text = frame.escape_echo(echo_text(row, span, _user_name()), platform)
            if text is None:
                logger.warning("intercom: no echo escaper for %s; message %s not echoed",
                               platform or "unknown", row["id"])
                continue
            _schedule(row["id"], text)
    except Exception as exc:  # health: allow BLE001 -- log the type only; the message can be in the traceback
        logger.warning("intercom: echo hook failed (%s)", type(exc).__name__)


def _schedule(message_id: str, text: str) -> None:
    # The copied context keeps the turn's bound session, which post_message reads.
    run = contextvars.copy_context().run
    threading.Thread(target=run, args=(_post, message_id, text), daemon=True,
                     name=f"intercom-echo-{message_id}").start()


def _post(message_id: str, text: str) -> None:
    try:
        handle = _ctx["ctx"].post_message(text)
    except Exception as exc:  # health: allow BLE001 -- log the type only; the post error can carry the body
        logger.warning("intercom: echo of message %s failed (%s)", message_id, type(exc).__name__)
        return
    if not getattr(handle, "ok", False):
        logger.warning("intercom: echo of message %s refused (%s)", message_id,
                       getattr(handle, "error", None) or "unknown")


def _user_name() -> str:
    ctx = _ctx["ctx"]
    try:
        return (ctx.get_config("user_name", default=None) if ctx else None) or route.DEFAULT_USER_NAME
    except Exception:  # health: allow BLE001 -- a config failure uses the default user name
        return route.DEFAULT_USER_NAME
