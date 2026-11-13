"""intercom inbox — per-session UDS server + peer registry.

Each opted-in interactive CLI session:
- writes ~/.hermes/intercom/sessions/<session_id>.json  {name, pid, cwd, session_id, socket, started_at}
- binds    ~/.hermes/intercom/sessions/<session_id>.sock (0600) served on a daemon thread
- removes both at exit (atexit)

Envelope protocol (JSON lines over the socket):
  -> {"type": "send", "from_name": ..., "from_cwd": ..., "message": ...}
  <- {"ok": true} | {"ok": false, "error": ...}

Delivery on the receiving side:
  busy  -> agent.steer(wrapped_text)
  idle  -> pending queue; drained at next turn start by the tool's turn hook
"""

from __future__ import annotations

import atexit
import json
import logging
import os
import socket
import struct
import sys
import threading
import time
import uuid
from pathlib import Path

logger = logging.getLogger(__name__)

_DIR_NAME = "intercom"
_REGISTRY = "sessions"
# Message length, rate and dedupe caps are gone (README, "What changed from 0.2").
# The pending cap stays only on this legacy inbox, which a build with the gateway
# owner client never starts.
_PENDING_CAP = 10

_lock = threading.Lock()
_state: dict = {}          # session-scoped singleton state
_pending_by_peer: dict[str, list[dict]] = {}   # peer_name -> [envelopes]


def _human_view(header: str, body: str) -> str:
    """Reasoning-style box: top/bottom rules with side corners, no side rails.

    The title line never exceeds the box width: if the header is long the
    dashes shrink to the minimum and the ┐ corner still lands at column W.
    """
    import shutil

    # Full terminal width (minus 1 col so the ┐/┘ corners don't trigger
    # a wrap on terminals that auto-wrap at exactly cols).
    term_w = shutil.get_terminal_size((100, 20)).columns
    w = max(40, term_w - 1)
    def dlen(s: str) -> int:
        """Display width: emoji/CJK count 2 columns."""
        return len(s) + sum(1 for ch in s if ord(ch) > 0x1F000)

    top_label = f"┌─ 📡 INTERCOM · {header} "
    top_dlen = dlen(top_label)
    if top_dlen + 2 <= w:
        out = [top_label + "─" * (w - top_dlen - 1) + "┐"]
    else:
        # header too long: truncate it so the corner still fits on row 1
        keep = max(10, w - 4 - (dlen(top_label) - len(top_label)))
        out = [top_label[:keep] + "…┐"]
    import textwrap

    for l in body.split("\n"):
        if not l:
            out.append("")
            continue
        out.extend(textwrap.wrap(l, width=w, replace_whitespace=False,
                                 drop_whitespace=False) or [""])
    out.append("└" + "─" * (w - 2) + "┘")
    return "\n".join(out)


def _display_banner(human_text: str) -> None:
    """Print an incoming message straight onto the session's screen.

    Shows only the compact human view; the full model wrapper is NOT
    printed here (it enters context via steer/pending drain), so the
    user sees the message once, not twice.
    """
    try:
        out = sys.stdout
        if out is None:
            return
        out.write("\n" + human_text + "\n\n")
        out.flush()
    except Exception:  # health: allow BLE001 -- a render failure must not crash delivery
        logger.debug("intercom banner was not printed")


# ---------------------------------------------------------------------------
# paths / registry helpers
# ---------------------------------------------------------------------------

def _base_dir() -> Path:
    from hermes_constants import get_hermes_home
    return Path(get_hermes_home()) / _DIR_NAME


def _registry_dir() -> Path:
    return _base_dir() / _REGISTRY


def _default_name() -> str:
    """Stable per-profile name with a live-session suffix.

    Profile identity comes from the HERMES_HOME path (…/profiles/<name>/
    vs the default home), so two sessions of the same profile share a base
    name and disambiguate by pid — mirroring Walkie Talkie's
    agent_id + session_id split, simplified.
    """
    from hermes_constants import get_hermes_home

    home = Path(get_hermes_home())
    profile = home.name if home.parent.name == "profiles" else "default"
    cwd = Path.cwd().name or "session"
    return f"{profile}-{cwd[:20]}-{os.getpid() % 1000:03d}"


def _pid_alive(pid: int) -> bool:
    """True when *pid* is still running.

    ``psutil.pid_exists`` instead of ``os.kill(pid, 0)``, which signals the
    process on Windows. If psutil cannot be imported, the entry stays.
    """
    try:
        import psutil
    except ImportError:
        return True
    return bool(psutil.pid_exists(pid))


def sweep_stale() -> list[str]:
    """Remove registry entries whose PID is dead. Returns surviving names."""
    alive: dict[str, dict] = {}
    for path in sorted(_registry_dir().glob("*.json")):
        try:
            meta = json.loads(path.read_text(encoding="utf-8-sig"))
            if not _pid_alive(int(meta["pid"])):
                raise ProcessLookupError(meta["pid"])
            alive[meta["name"]] = meta
        except (OSError, ValueError, KeyError):
            try:
                meta0 = json.loads(path.read_text(encoding="utf-8-sig"))
            except (OSError, ValueError):
                meta0 = {}
            sock = meta0.get("socket", "")
            if sock and os.path.exists(sock):
                os.unlink(sock)
            path.unlink(missing_ok=True)
    return sorted(alive.keys())


# ---------------------------------------------------------------------------
# delivery
# ---------------------------------------------------------------------------

def _sanitize_boundary(text: str) -> str:
    """Neutralise boundary-marker injection in untrusted message parts.

    Sender names and message bodies travel inside the INTERCOM wrapper; a
    malicious peer could embed ``[/INTERCOM MESSAGE]`` to break out of the
    frame and have the trailing text read as ordinary context (the
    Walkie Talkie plugin defends against the same thing). Insert zero-width spaces into any
    boundary-like tag so it no longer parses as one.
    """
    # Header forgery: a body line mimicking our new header format gets broken
    # by a zero-width space right after the emoji, so only the true first
    # line (added by _wrap) parses as the header.
    text = text.replace("📡 INTERCOM", "📡\u200b INTERCOM")
    return text


def _sanitize_name(name: str) -> str:
    """Strip newlines/brackets from sender names before framing them."""
    cleaned = "".join(ch for ch in str(name) if ch not in "\r\n[]")
    return cleaned.strip() or "unknown"


def _frame_parts(from_name: str, from_cwd: str, message: str) -> tuple[str, str]:
    """Return (header_line, sanitized_body)."""
    from_name = _sanitize_name(from_name)
    body = _sanitize_boundary(str(message))
    header = f"📡 hermes@{from_name} says:"
    return header, body


def _wrap(from_name: str, from_cwd: str, message: str) -> str:
    """Full model-facing frame: identity header + body."""
    header, body = _frame_parts(from_name, from_cwd, message)
    return f"{header}\n{body}"


def _agent_busy(agent) -> bool:
    """Heuristic: agent is mid-turn when a conversation loop flag is set."""
    for attr in ("_is_processing", "_busy", "_in_turn"):
        v = getattr(agent, attr, None)
        if isinstance(v, bool):
            return v
    # Fallback: steer() returns False only on empty text; there is no public
    # busy probe, so treat "steer accepted" as authoritative — see deliver().
    return True


def _find_cli_instance(cli_mod):
    """Locate the running HermesCLI instance.

    ``cli`` is a local in ``main()`` (cli.py:21256), not module state, so we
    find the live object via GC — there is exactly one per interactive
    session process.
    """
    import gc

    for o in gc.get_objects():
        if type(o).__name__ == "HermesCLI" and hasattr(o, "_pending_input"):
            return o
    return None


def _deliver_local(text: str, *, display: bool = False) -> str:
    import cli as cli_mod

    cli_obj = getattr(cli_mod, "_cli_instance", None) or _find_cli_instance(cli_mod)

    agent = getattr(cli_mod, "_active_agent_ref", None)
    if agent is None and cli_obj is not None:
        agent = getattr(cli_obj, "agent", None)

    # Wake-the-input-loop path: the interactive CLI drains `_pending_input`
    # in its process_loop thread (0.1s poll). Putting the message there makes
    # the receiving session START A TURN automatically — Claude Code parity.
    pq = getattr(cli_obj, "_pending_input", None) if cli_obj is not None else None
    proc_busy = bool(getattr(cli_obj, "_agent_running", False)) or _is_turn_running(agent)
    reg_busy = self_registry_turn_active()
    if pq is not None and not proc_busy and not reg_busy:
        try:
            pq.put(text)
            return "submitted_as_turn"
        except Exception:  # health: allow BLE001 -- a queue rejection falls through to park or steer
            logger.debug("intercom pending-input put failed")

    if agent is None:
        # No reachable agent in THIS process (e.g. the peer is a TUI/desktop
        # session whose agent lives in tui_gateway.entry). Park the message
        # anyway — the plugin's pre_llm_call hook drains it into the next
        # turn of whichever process received it.
        spill = False
        with _lock:
            pend = _state.setdefault("pending_for_self", [])
            if len(pend) >= _PENDING_CAP:
                spill = True
                status = "held_spilled"
            else:
                pend.append(text)
                spill = True
                status = "parked_no_agent"
        if spill:
            _spool_append(text)   # outside the lock — self_meta() takes it
        return status

    # Busy path: steer drains between tool calls of the running turn.
    # Registry says busy even when process-local probes lag — steer anyway.
    if _is_turn_running(agent) or reg_busy:
        if hasattr(agent, "steer") and agent.steer(text):
            return "steered"
        return "held_steer_rejected"
    # Idle path: park it; the next user prompt sees it via take_pending().
    with _lock:
        pend = _state.setdefault("pending_for_self", [])
        if len(pend) >= _PENDING_CAP:
            return "rejected_pending_cap"
        pend.append(text)
    return "parked_idle"


def _is_turn_running(agent) -> bool:
    """Best-effort detection that the agent loop is between API calls.

    Hermes sets `_conversation_active`/`_processing` in different versions;
    we probe the known flags and default to False (idle → parked), which is
    the safe choice: a steered message into an idle session would otherwise
    surface mid-next-turn unexpectedly.
    """
    for attr in (
        "_conversation_active",
        "_is_conversation_active",
        "_processing",
        "_turn_running",
    ):
        v = getattr(agent, attr, None)
        if v is True:
            return True
    # A tool count means run_conversation is executing.
    return bool(getattr(agent, "_current_tool_count", None))


def set_turn_active(active: bool) -> None:
    """Publish this session's busy/idle state into its registry entry.

    Written by the pre_llm_call hook (turn starts) and a post-turn hook so
    senders (and humans) can see who is mid-turn before messaging.
    """
    try:
        with _lock:
            meta = dict(_state.get("meta") or {})
        if not meta:
            return
        path = _registry_dir() / f"{meta.get('session_id')}.json"
        if not path.exists():
            return
        data = json.loads(path.read_text(encoding="utf-8-sig"))
        data["turn_active"] = bool(active)
        data["turn_ts"] = time.time()
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data), encoding="utf-8")
        tmp.replace(path)
    except Exception:  # health: allow BLE001 -- registry bookkeeping must not break a turn
        logger.debug("intercom turn flag was not published")


def list_peers() -> tuple[list[dict], dict]:
    """Live peers with busy state + self meta. For the tool's action=list."""
    me = self_meta()
    out = []
    for name, meta in _load_peers().items():
        if name == me.get("name"):
            continue
        out.append({
            "name": name,
            "cwd": meta.get("cwd"),
            "busy": bool(meta.get("turn_active")),
        })
    return out, me


def self_registry_turn_active() -> bool:
    """Read this session's own published turn_active flag (registry JSON)."""
    try:
        with _lock:
            sid = (_state.get("meta") or {}).get("session_id")
        if not sid:
            return False
        p = _registry_dir() / f"{sid}.json"
        return bool(json.loads(p.read_text(encoding="utf-8-sig")).get("turn_active"))
    except (OSError, ValueError, TypeError, KeyError, ImportError, AttributeError):
        return False


def _load_peers() -> dict:
    """Read live peer metadata from the registry, keyed by name."""
    peers = {}
    for path in sorted(_registry_dir().glob("*.json")):
        try:
            meta = json.loads(path.read_text(encoding="utf-8-sig"))
            if not _pid_alive(int(meta["pid"])):
                continue
            peers.setdefault(meta["name"], meta)
        except (OSError, ValueError, TypeError, KeyError, AttributeError):
            continue
    return peers


def _reply_waiter_socket(request_id: str) -> str:
    """Bind a one-shot reply socket for this process, keyed by request_id."""
    reg = _registry_dir()
    path = str(reg / f"reply-{request_id}.sock")
    try:
        os.unlink(path)
    except OSError:
        pass
    return path


def ask_to(
    target: str,
    message: str,
    from_name: str = "",
    from_cwd: str = "",
    timeout: int = 600,
) -> dict:
    """Send `message` to peer and block until their reply or timeout.

    The reply arrives on our own inbox as type='reply' addressed to
    request_id; the inbox thread hands it to the waiter event.
    """
    resolved_target = resolve_target(target)
    if not resolved_target.get("ok"):
        return {**resolved_target, "action": "ask"}
    expected_peer = _sanitize_name(resolved_target["name"])
    request_id = uuid.uuid4().hex
    evt = threading.Event()
    with _lock:
        _state.setdefault("waiters", {})[request_id] = {
            "evt": evt,
            "reply": None,
            "expected_peer": expected_peer,
        }

    payload_extra = {
        "type": "ask",
        "request_id": request_id,
        "ask_timeout": timeout,
    }
    res = send_to(
        resolved_target["name"],
        message,
        from_name,
        from_cwd,
        payload_extra=payload_extra,
    )
    if not res.get("ok"):
        with _lock:
            _state.get("waiters", {}).pop(request_id, None)
        return {**res, "action": "ask"}

    # Ask accepted: wait for the peer's reply envelope on our inbox.
    if not evt.wait(timeout=min(timeout, 3600)):
        with _lock:
            _state.get("waiters", {}).pop(request_id, None)
        return {"ok": False, "error": "timeout waiting for reply",
                "request_id": request_id, "action": "ask"}
    with _lock:
        entry = _state.get("waiters", {}).pop(request_id, None)
    reply = (entry or {}).get("reply")
    return {"ok": reply is not None, "reply": reply,
            "from": target, "action": "ask"}


def resolve_ask(request_id: str, from_name: str, reply_text: str) -> bool:
    """Deliver a reply for a pending ask. True if a local waiter took it."""
    with _lock:
        w = _state.get("waiters", {}).get(request_id)
        if w is None or w.get("expected_peer") != from_name:
            return False
        w["reply"] = reply_text
    w["evt"].set()
    return True


def reply_to(
    target: str,
    request_id: str,
    message: str,
    from_name: str = "",
    from_cwd: str = "",
) -> dict:
    """Reply to a peer's pending ask using its original request id."""
    return send_to(
        target,
        message,
        from_name,
        from_cwd,
        payload_extra={"type": "reply", "request_id": request_id},
        dedupe=False,
    )


def resolve_target(target: str) -> dict:
    """Resolve a unique live peer name from an exact name or prefix."""
    peers = _load_peers()
    if target in peers:
        return {"ok": True, "name": target, "meta": peers[target]}
    matches = [name for name in peers if name == target or name.startswith(target)]
    if not matches:
        sweep_stale()
        return {
            "ok": False,
            "error": f"no live peer named '{target}'",
            "peers": sweep_stale(),
        }
    if len(matches) > 1:
        return {"ok": False, "error": f"ambiguous name; matches: {matches}"}
    return {"ok": True, "name": matches[0], "meta": peers[matches[0]]}


def _exchange(meta: dict, payload: dict) -> dict:
    """Exchange one authenticated-local envelope with a registered peer."""
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
            connection.settimeout(5)
            connection.connect(meta["socket"])
            connection.sendall((json.dumps(payload) + "\n").encode())
            data = b""
            while b"\n" not in data:
                chunk = connection.recv(4096)
                if not chunk:
                    break
                data += chunk
        return json.loads(data.decode().strip() or "{}")
    except (OSError, json.JSONDecodeError) as exc:
        return {"ok": False, "error": f"delivery failed: {exc}"}


def send_to(target: str, message: str, from_name: str = "", from_cwd: str = "",
            payload_extra: dict | None = None, *, dedupe: bool = True) -> dict:
    """Send `message` to peer `target`. Returns status dict.

    ``dedupe`` is accepted so ``reply_to`` can still pass it. It does not
    filter: twenty identical messages in a minute all go out (README, "What changed from 0.2").
    """
    del dedupe
    resolved_target = resolve_target(target)
    if not resolved_target.get("ok"):
        return resolved_target
    name = resolved_target["name"]
    meta = resolved_target["meta"]
    if int(meta.get("pid", -1)) == os.getpid():
        return {"ok": False, "error": "target is the current session"}

    payload = {
        "type": "send",
        "from_name": from_name,
        "from_cwd": from_cwd,
        "message": message,
        **(payload_extra or {}),
    }
    reply = _exchange(meta, payload)
    if not reply.get("ok") and reply.get("error"):
        return {**reply, "receipt": "refused", "to": name}

    # Normalize receipt (Walkie Talkie-style taxonomy).
    status = reply.get("status")
    if status in ("submitted_as_turn",):
        receipt = "delivered"       # receiver will start a turn
    elif status in ("steered", "parked_idle", "parked_no_agent"):
        receipt = "held"            # accepted, surfaces at next boundary/turn
    elif status == "reply_resolved":
        receipt = "delivered"
    else:
        receipt = "refused"
    return {"ok": reply.get("ok", False), "receipt": receipt,
            "delivered_as": status, "to": name}


# ---------------------------------------------------------------------------
# inbox server thread
# ---------------------------------------------------------------------------

def _peer_uid(conn: socket.socket) -> int:
    """Return the effective UID authenticated by the local socket transport."""
    getpeereid = getattr(conn, "getpeereid", None)
    if getpeereid is not None:
        uid, _gid = getpeereid()
        return int(uid)
    local_peercred = getattr(socket, "LOCAL_PEERCRED", None)
    if local_peercred is not None:
        raw = conn.getsockopt(0, local_peercred, 12)
        _version, uid, _groups = struct.unpack("@iIh", raw[:10])
        return int(uid)
    so_peercred = getattr(socket, "SO_PEERCRED", None)
    if so_peercred is not None:
        raw = conn.getsockopt(socket.SOL_SOCKET, so_peercred, 12)
        _pid, uid, _gid = struct.unpack("3i", raw)
        return int(uid)
    raise PermissionError("peer uid authentication is unavailable on this platform")


def _server_uid() -> int:
    """This process's uid. PermissionError where the platform has none."""
    getuid = getattr(os, "getuid", None)
    if getuid is None:
        raise PermissionError("peer uid authentication is unavailable on this platform")
    return int(getuid())


def _serve(conn: socket.socket) -> None:
    try:
        peer_uid = _peer_uid(conn)
        server_uid = _server_uid()
        if peer_uid != server_uid:
            raise PermissionError(
                f"peer uid {peer_uid} does not match server uid {server_uid}"
            )
        data = b""
        conn.settimeout(10)
        while b"\n" not in data:
            chunk = conn.recv(65536)
            if not chunk:
                break
            data += chunk
        line = data.split(b"\n", 1)[0].decode(errors="replace").strip()
        req = json.loads(line) if line else {}
        req_type = req.get("type", "send")
        if req_type not in ("send", "ask", "reply"):
            raise ValueError(f"unsupported type {req_type!r}")
        if req_type == "reply":
            # Reply to one of OUR asks: hand it to the local waiter.
            ok = resolve_ask(
                str(req.get("request_id", "")),
                _sanitize_name(req.get("from_name") or "unknown"),
                str(req.get("message", "")),
            )
            resp = {"ok": ok, "status": "reply_resolved" if ok else "no_pending_ask"}
            conn.sendall((json.dumps(resp) + "\n").encode())
            conn.close()
            return
        sender = _sanitize_name(req.get("from_name") or "unknown")
        header, body = _frame_parts(sender, req.get("from_cwd") or "?",
                                    str(req.get("message", "")))
        text = f"{header}\n{body}"
        if req_type == "ask" and req.get("request_id"):
            # Tell the receiving agent this needs a reply, and how.
            rid = str(req["request_id"])
            asker = _sanitize_name(req.get("from_name") or "unknown")
            text = (
                f"{text}\n"
                f"[The sender expects a reply. Use intercom(action=\"reply\", "
                f"to=\"{asker}\", request_id=\"{rid}\", message=\"...\").]"
            )
        status = _deliver_local(text)
        resp = {"ok": status not in ("rejected_pending_cap",), "status": status}
    except Exception as exc:  # health: allow BLE001 -- the thread must stay up; the warning is the record
        logger.warning("intercom inbox error: %s", exc)
        resp = {"ok": False, "error": str(exc)}
    try:
        conn.sendall((json.dumps(resp) + "\n").encode())
    except OSError:
        pass
    finally:
        conn.close()


def start(name: str = "", session_id: str = "") -> None:
    """Start the inbox for this process. Idempotent."""
    with _lock:
        if _state.get("running"):
            return

    reg = _registry_dir()
    reg.mkdir(parents=True, exist_ok=True)
    sid = session_id or f"{os.getpid():d}-{int(time.time())}"
    sock_path = str(reg / f"{sid}.sock")
    try:
        os.unlink(sock_path)
    except OSError:
        pass

    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    srv.bind(sock_path)
    os.chmod(sock_path, 0o600)
    srv.listen(8)

    meta = {
        "name": name or _default_name(),
        "pid": os.getpid(),
        "cwd": str(Path.cwd()),
        "session_id": sid,
        "socket": sock_path,
        "started_at": time.time(),
    }
    (reg / f"{sid}.json").write_text(json.dumps(meta), encoding="utf-8")

    stop_evt = threading.Event()

    def _loop():
        srv.settimeout(1.0)
        while not stop_evt.is_set():
            try:
                conn, _ = srv.accept()
            except TimeoutError:
                continue
            except OSError:
                break
            threading.Thread(target=_serve, args=(conn,), daemon=True).start()

    t = threading.Thread(target=_loop, name="intercom-inbox", daemon=True)
    t.start()

    def _cleanup(*_a):
        stop_evt.set()
        try:
            srv.close()
        except OSError:
            pass
        for p in (sock_path, str(reg / f"{sid}.json")):
            try:
                os.unlink(p)
            except OSError:
                pass

    atexit.register(_cleanup)
    # NOTE: no custom signal handlers here — the host CLI owns SIGINT/SIGTERM
    # semantics; atexit covers our cleanup on normal and signal-driven exits.

    with _lock:
        _state.update(running=True, cleanup=_cleanup, meta=meta, thread=t)
    recovered = _spool_drain(session_id=sid)
    with _lock:
        if recovered:
            _state["pending_for_self"] = (
                _state.get("pending_for_self", []) + recovered
            )[-_PENDING_CAP * 2:]


def stop() -> None:
    with _lock:
        fn = _state.get("cleanup")
        _state["running"] = False
    if fn:
        fn()


def self_meta() -> dict:
    with _lock:
        return dict(_state.get("meta") or {})


def _spool_file(session_id: str = "") -> Path:
    """Owner-only on-disk spill for parked messages scoped to one endpoint id."""
    sid = session_id or self_meta().get("session_id") or str(os.getpid())
    return _base_dir() / "pending" / f"{sid}.jsonl"


def _spool_append(text: str) -> None:
    try:
        f = _spool_file()
        f.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(f, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        os.chmod(f, 0o600)
        with os.fdopen(fd, "a", encoding="utf-8") as fh:
            fh.write(json.dumps({"text": text, "ts": time.time()}) + "\n")
    except (OSError, TypeError, ValueError):
        pass


def _spool_drain(session_id: str = "") -> list[str]:
    """Load and remove spilled messages (called at inbox start)."""
    try:
        f = _spool_file(session_id)
        if not f.exists():
            return []
        out = []
        for line in f.read_text(encoding="utf-8-sig").splitlines():
            try:
                out.append(str(json.loads(line)["text"]))
            except (ValueError, TypeError, KeyError):
                continue
        f.unlink(missing_ok=True)
        return out
    except (OSError, ValueError):
        return []


def take_pending() -> list[str]:
    """Pop messages parked while idle (drained at next turn)."""
    with _lock:
        pend = _state.pop("pending_for_self", [])
    return pend


def is_running() -> bool:
    with _lock:
        return bool(_state.get("running"))
