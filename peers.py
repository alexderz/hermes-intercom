"""intercom peers — the remote agents of the active profile (peers.md, P2).

``plugin-data/intercom/peers.json`` is written by house-infra, never by the plugin. It is a JSON
list with one object per remote agent:

- ``name``: ``^[a-z0-9-]{1,63}$``; the same string is the certificate CN, the webhook route
  (``peer-<name>``) and the address (``peer:<name>``);
- ``url``: the agent's webhook. For ``auth: url`` this is the same URL with the secret part
  replaced by ``…``, and it is what ``list`` and receipts show. Only an ``https://`` URL is a
  peer; anything else is not loaded;
- ``auth``: ``url`` (the secret is the full URL, in the secret file) or ``hmac-v2``;
- ``secret_file``: where the secret is kept.

``load()`` reads it fresh on every call. A missing, unreadable or malformed file, or any bad
or repeated entry, means no peers: nothing is half-trusted.

``send()`` is the one outbound post. ``route.send`` calls it when ``to`` starts with ``peer:``,
so the tool, the CLI and the reply hook all pass its check. The check refuses, and records and
posts nothing, unless the sender is the default profile's canonical Bot Chat. The POST then runs
on the caller's thread.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import re
import secrets
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

PLUGIN = "intercom"
FILENAME = "peers.json"
AUTH_MODES = ("url", "hmac-v2")
NAME_RE = re.compile(r"^[a-z0-9-]{1,63}$")
PREFIX = "peer:"
FROM = "hermes"
# Fixed. urllib applies this to each blocking socket call, not the whole request,
# so a peer that sends a byte now and then can hold the thread longer. A timeout
# is not a retry and not a cap.
TIMEOUT_SECONDS = 30
ONLY_BOT_CHAT = "only Bot Chat talks to remote agents; message Bot Chat"
WAITING = "a remote agent cannot hold your turn; use requested"
SECRET_UNREADABLE = "Not delivered: the remote agent's secret could not be read."


@dataclass(frozen=True)
class Peer:
    name: str
    url: str
    auth: str
    secret_file: str


def load() -> dict[str, Peer]:
    """The peers of the active profile by name; empty when the file is absent or bad."""
    try:
        data = json.loads(path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(data, list):
        return {}
    found: dict[str, Peer] = {}
    for entry in data:
        peer = _peer(entry)
        if peer is None or peer.name in found:
            return {}
        found[peer.name] = peer
    return found


def path():
    """``<home>/plugin-data/intercom/peers.json`` of the active profile. Reading it creates nothing."""
    from hermes_constants import get_hermes_home

    return get_hermes_home() / "plugin-data" / PLUGIN / FILENAME


def for_bot_chat(sender) -> list[dict] | None:
    """Configured peers as ``peer:<name>`` when *sender* is the default profile's Bot Chat.

    ``None`` for every other caller, so ``list`` can omit the key. Each item's ``url`` is the
    entry's ``url`` (already redacted, for ``auth: url``). The secret file is never read here.
    """
    if not _on_default_profile() or not _is_bot_chat(sender):
        return None
    found = load()
    return [{"to": f"{PREFIX}{name}", "url": found[name].url} for name in sorted(found)]


def send(sender, to, message, *, delivery="steer", reply_mode=None, kind="send", reply_to=None,
         script=False, key=None, profile=None, within=None) -> dict:
    """POST *message* to the remote agent *to* (``peer:<name>``). The receipt is the return value.

    The first step refuses, with nothing recorded or posted, unless every one of these holds:
    the active profile is default and no ``profile`` was passed; this is not a script send or an
    ``ask``; delivery is not ``passive`` and there is no ``key`` or ``within``; *sender* is the
    canonical Bot Chat's compression tip; the name is a peer whose URL is ``https://``.

    ``reply: none`` posts ``wants_reply: false``, ``requested`` posts ``true``, and ``waiting``
    is refused. A reply (``optional``, with ``reply_to``) posts ``wants_reply: false``. The row
    is recorded before the POST. TLS is verified, redirects are not followed, and the secret is
    never put in the receipt or the log.
    """
    text = to.strip() if isinstance(to, str) else ""
    reply_to = reply_to or None
    reason, peer = _refusal(sender, text, delivery=delivery, kind=kind, script=script, key=key,
                            profile=profile, within=within)
    if reason is not None:
        return _refused(text, delivery, reply_mode, reason, ONLY_BOT_CHAT)
    if reply_mode == "waiting":
        return _refused(text, delivery, reply_mode, "waiting", WAITING)

    body = message if isinstance(message, str) else ""
    wants_reply = reply_mode == "requested"
    if not body.strip():
        return _empty(sender, peer, body, delivery=delivery, reply_mode=reply_mode, reply_to=reply_to)

    target = _target(peer)
    if target is None:
        return _refused(text, delivery, reply_mode, "unknown-peer", ONLY_BOT_CHAT)
    if target == "secret":
        return _refused(text, delivery, reply_mode, "secret-unreadable", SECRET_UNREADABLE, exit_code=None)
    url, signing_key = target

    message_id = secrets.token_hex(4)
    raw = _payload(message_id, body, wants_reply=wants_reply, reply_to=reply_to)
    _record(sender, peer, message_id, body, delivery=delivery, reply_mode=reply_mode, reply_to=reply_to)
    headers = {"Content-Type": "application/json"}
    if signing_key is not None:
        stamp = str(int(time.time()))
        headers["X-Webhook-Timestamp"] = stamp
        headers["X-Webhook-Signature-V2"] = hmac.new(
            signing_key.encode("utf-8"), stamp.encode("utf-8") + b"." + raw, hashlib.sha256).hexdigest()
    status, problem = _exchange(url, raw, headers)
    return _finish(text, peer, message_id, delivery=delivery, reply_mode=reply_mode,
                   status=status, problem=problem)


def _peer(entry) -> Peer | None:
    if not isinstance(entry, dict):
        return None
    name, url, auth, secret_file = (entry.get(k) for k in ("name", "url", "auth", "secret_file"))
    if not (isinstance(name, str) and NAME_RE.match(name)):
        return None
    if not _is_https(url) or auth not in AUTH_MODES:
        return None
    if not (isinstance(secret_file, str) and secret_file):
        return None
    return Peer(name=name, url=url, auth=auth, secret_file=secret_file)


def _is_https(url) -> bool:
    """True when *url* is an ``https`` URL with a host. Anything else is not a peer."""
    if not isinstance(url, str):
        return False
    parts = urllib.parse.urlsplit(url.strip())
    return parts.scheme == "https" and bool(parts.hostname)


def _refusal(sender, to, *, delivery, kind, script, key, profile, within) -> tuple[str | None, Peer | None]:
    """``(reason, None)`` when the send stops here, else ``(None, peer)``.

    Nothing is recorded for a reason. The peer comes from this one ``load()``, so the
    send does not read ``peers.json`` again.
    """
    try:
        from . import profiles, record
    except ImportError:  # loaded as top-level modules (the unittest layout)
        import profiles
        import record

    if profiles.current() != profiles.DEFAULT:
        return "not-default", None
    if isinstance(profile, str) and profile.strip():
        return "profile", None
    if script:
        return "script", None
    if kind == "ask":
        return "ask", None
    if delivery == record.PASSIVE:
        return "passive", None
    if key:
        return "key", None
    if within is not None:
        return "within", None
    if not _is_bot_chat(sender):
        return "not-bot-chat", None
    name = to[len(PREFIX):] if to.startswith(PREFIX) else ""
    if not NAME_RE.match(name):
        return "unknown-peer", None
    peer = load().get(name)
    if peer is None:
        return "unknown-peer", None
    return None, peer


def _on_default_profile() -> bool:
    try:
        from . import profiles
    except ImportError:
        import profiles
    return profiles.current() == profiles.DEFAULT


def _is_bot_chat(sender) -> bool:
    """*sender* is the compression tip of the session titled exactly ``Bot Chat``."""
    if not isinstance(sender, str) or not sender.strip():
        return False
    tip = _bot_chat_tip()
    return tip is not None and sender.strip() == tip


def _bot_chat_tip() -> str | None:
    """The tip of ``get_session_by_title(CANONICAL_BOT_CHAT_TITLE)``, as ``address.py`` reads it."""
    try:
        from hermes_state import SessionDB

        db = SessionDB(read_only=True)
    except Exception:  # health: allow BLE001 -- state unreadable means this sender is not Bot Chat
        return None
    try:
        titled = db.get_session_by_title(db.CANONICAL_BOT_CHAT_TITLE)
        if not isinstance(titled, dict):
            return None
        raw = titled.get("id")
        if not isinstance(raw, str) or not raw.strip():
            return None
        raw = raw.strip()
        tip = db.get_compression_tip(raw)
        return tip or raw
    except Exception:  # health: allow BLE001 -- a lineage miss is not Bot Chat
        return None
    finally:
        db.close()


def _who(sender: str):
    try:
        from . import route
    except ImportError:
        import route
    return route._Who.session(sender)


def _empty(sender, peer: Peer, body: str, *, delivery, reply_mode, reply_to) -> dict:
    """An empty message is refused like any other send: one refused row, nothing posted."""
    try:
        from . import record, route
    except ImportError:
        import record
        import route

    message_id = secrets.token_hex(4)
    _record(sender, peer, message_id, body, delivery=delivery, reply_mode=reply_mode, reply_to=reply_to)
    record.update(message_id, delivery_path="refused", result="refused", detail="empty")
    fields = _fields(ok=False, receipt="refused", to=f"{PREFIX}{peer.name}", message_id=message_id,
                     title=peer.name, delivery=delivery, reply_mode=reply_mode,
                     message=f"{route.NOTHING_SENT} {route.EMPTY_REASON}", error="empty")
    return fields


def _target(peer: Peer):
    """``(post url, hmac secret or None)``, ``"secret"`` when the file cannot be read, else None.

    ``None`` means this entry is not a peer (the URL that would be posted is not ``https://``).
    The secret itself is not logged and not returned anywhere but the HMAC key.
    """
    try:
        secret = Path(peer.secret_file).read_text(encoding="utf-8").strip()
    except (OSError, UnicodeError):
        return "secret"
    if not secret:
        return "secret"
    if peer.auth == "url":
        url = secret if _is_https(secret) else ""
        return (url, None) if url else None
    # load() already dropped a peer whose url is not https.
    return peer.url.strip(), secret


def _payload(message_id: str, text: str, *, wants_reply: bool, reply_to) -> bytes:
    payload = {"from": FROM, "message_id": message_id}
    if reply_to:
        payload["reply_to"] = reply_to
    payload["wants_reply"] = wants_reply
    payload["text"] = text
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def _record(sender, peer: Peer, message_id: str, body: str, *, delivery, reply_mode, reply_to) -> None:
    """The row, with ``delivery_path='peer'``, before the POST."""
    try:
        from . import record
    except ImportError:
        import record

    who = _who(sender)
    record.insert(message_id, kind="send", sender_label=who.label, sender_source=who.source,
                  sender_session_id=who.session_id, address=f"{PREFIX}{peer.name}", body=body,
                  reply_mode=reply_mode, requested=delivery, target_title=peer.name,
                  in_reply_to=reply_to)
    record.update(message_id, delivery_path="peer")


def _finish(to: str, peer: Peer, message_id: str, *, delivery, reply_mode, status, problem) -> dict:
    try:
        from . import record
    except ImportError:
        import record

    if status is not None and 200 <= status < 300:
        record.update(message_id, result="delivered", detail=f"HTTP {status}", delivered_at=time.time())
        return _fields(ok=True, receipt="delivered", to=to, message_id=message_id, title=peer.name,
                       delivery=delivery, reply_mode=reply_mode, message=f"delivered (HTTP {status})",
                       error=None, url=peer.url)
    if status is not None:
        detail = f"HTTP {status}"
    else:
        detail = problem or "delivery-unknown"
    record.update(message_id, result="error", detail=detail)
    logger.warning("intercom: peer post to %s failed (%s)", peer.name, detail)
    message = "delivery-unknown" if detail == "delivery-unknown" else f"not delivered ({detail})"
    return _fields(ok=False, receipt="refused", to=to, message_id=message_id, title=peer.name,
                   delivery=delivery, reply_mode=reply_mode, message=message, error=detail, url=peer.url)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """A 3xx is an error. The redirect is not followed, and the URL is not kept on the error."""

    def http_error_302(self, req, fp, code, msg, headers):
        raise urllib.error.HTTPError("https://peer.invalid/", code, msg, headers, fp)

    http_error_301 = http_error_303 = http_error_307 = http_error_308 = http_error_302


class _StatusOnly(urllib.request.HTTPDefaultErrorHandler):
    """Raise with the status and a stand-in URL, so the request URL (it may be the secret) is dropped."""

    def http_error_default(self, req, fp, code, msg, hdrs):
        raise urllib.error.HTTPError("https://peer.invalid/", code, msg, hdrs, fp)


def _exchange(url: str, data: bytes, headers: dict) -> tuple[int | None, str | None]:
    """POST *data* to *url*. ``(status, None)`` or ``(None, problem)``.

    TLS is the default context, so verification stays on. No proxy: the URL can carry the
    secret. Any failure is the exception type (or ``delivery-unknown``); ``str(exc)`` is
    never used, because it can contain the secret URL.
    """
    if not _is_https(url):
        return None, "not-https"
    request = urllib.request.Request(url, data=data, headers=headers, method="POST")
    opener = urllib.request.build_opener(
        urllib.request.ProxyHandler({}),
        _NoRedirect(),
        _StatusOnly(),
        urllib.request.HTTPSHandler(context=ssl.create_default_context()),
    )
    try:
        with opener.open(request, timeout=TIMEOUT_SECONDS) as response:
            response.read()
            return response.status, None
    except urllib.error.HTTPError as exc:
        status = exc.code
        try:
            exc.read()
        except Exception:  # health: allow BLE001 -- a short body must not replace the HTTP code
            pass
        finally:
            exc.close()
        return status, None
    except (TimeoutError, urllib.error.URLError, ssl.SSLError, OSError) as exc:
        if _timed_out(exc):
            return None, "delivery-unknown"
        return None, type(exc).__name__
    except Exception as exc:  # health: allow BLE001 -- type name only; str(exc) can quote the secret URL
        return None, type(exc).__name__


def _timed_out(exc: BaseException) -> bool:
    if isinstance(exc, TimeoutError):
        return True
    reason = getattr(exc, "reason", None)
    return isinstance(reason, BaseException) and reason is not exc and _timed_out(reason)


def _refused(to, delivery, reply_mode, reason: str, message: str, *, exit_code=2) -> dict:
    fields = _fields(ok=False, receipt="refused", to=to, message_id=None, title=None,
                     delivery=delivery, reply_mode=reply_mode, message=message, error=reason)
    if exit_code is not None:
        fields["exit"] = exit_code
    return fields


def _fields(*, ok, receipt, to, message_id, title, delivery, reply_mode, message, error,
            url=None) -> dict:
    fields = {
        "ok": ok,
        "receipt": receipt,
        "to": to,
        "message_id": message_id,
        "target_title": title,
        "target_session": None,
        "reply": reply_mode,
        "requested": delivery,
        "applied": None,
        "applied_detail": None,
        "shared_chat": False,
        "message": message,
        "error": error,
    }
    if url is not None:
        fields["url"] = url
    return fields
