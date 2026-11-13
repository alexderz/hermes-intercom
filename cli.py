"""``hermes intercom`` — send, list, status, log and reboot hooks (README, "The CLI").

The CLI is an owner client. It runs ``route.send`` in this process with
``script=True``, starts no turn, and has no legacy-socket fallback. ``--reply``
defaults to ``none``. ``--no-reply`` is not a flag, so argparse reports it as
a usage error (exit 2).

``reboot-notice`` and ``resume-after-reboot`` (README, "Reboots") warn running sessions before a planned reboot and wake them after boot.
Delivery rules are unchanged: no caps, holds, retries or approval gates, and
one send per session per boot.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import sys
import time
from pathlib import Path

try:
    from . import address, client, replies, route
except ImportError:  # loaded as a top-level module (the unittest layout)
    import address
    import client
    import replies
    import route

# One sentence, word for word, in the tool text, the delivery parameter and --help,
# so a model and a person read the same rule.
INTERRUPT_SENTENCE = (
    "Only use interrupt when you intend to stop what the other session is doing right now "
    "(it is wrong, or the work is obsolete). Otherwise use steer (default) or queue."
)

_SINCE = re.compile(r"^(\d+)([smhd])$")
_SINCE_UNIT = {"s": 1, "m": 60, "h": 3600, "d": 86400}
_DEFAULT_SINCE = 24 * 3600

# detail -> process exit. 0 is a delivery; 1 is status's missing id; 2 is usage.
_EXIT = {
    "reply-missing": 3,
    "empty": 3,
    "ambiguous": 3,
    "unknown": 3,
    "own-session": 3,
    "project-addressing-unavailable": 3,
    "no-owner-set": 3,
    "not-addressable": 4,
    "injection-off": 4,
    "refused-by-owner": 4,
    "no-seam": 4,
    "no-owner": 5,
    "owner-restarting": 5,
    "not-reachable": 5,
    "delivery-unknown": 6,
}

_APPLIED_WORD = {
    "steer": "steered",
    "queue": "queued",
    "interrupt": "interrupted",
    "start": "started",
}

_state = {"ctx": None}


class _FallbackCtx:
    def get_config(self, key, default=None):
        return default


def register(ctx) -> None:
    """Register ``hermes intercom``. The host calls ``setup`` then the handler."""
    _state["ctx"] = ctx
    ctx.register_cli_command(
        "intercom",
        "Message another Hermes session",
        register_parser,
        intercom_command,
        description="Message another Hermes session; never send an empty message.",
    )


def _help_formatter(prog):
    # Wide enough that --delivery's help keeps the interrupt sentence on one line.
    return argparse.HelpFormatter(prog, width=240)


def register_parser(parser: argparse.ArgumentParser) -> None:
    """Arguments for ``hermes intercom <verb>``."""
    parser.formatter_class = _help_formatter
    parser.set_defaults(func=intercom_command)
    subs = parser.add_subparsers(dest="intercom_command", required=True)
    send = subs.add_parser("send", help="Send a message", formatter_class=_help_formatter)
    send.add_argument("--to", required=True, help="Session id, exact title, Home, or @<project>.")
    send.add_argument("--message", default=None, help="Message text.")
    send.add_argument("--file", default=None, help="Read the message from PATH, or - for stdin.")
    send.add_argument("--reply", choices=("none", "requested", "waiting"), default="none",
                      help="none (default), requested, or waiting.")
    send.add_argument("--delivery", choices=("interrupt", "steer", "queue"), default="steer",
                      help=INTERRUPT_SENTENCE)
    send.add_argument("--from", dest="from_label", default="cli",
                      help='Script label in the header (default: cli).')
    send.add_argument("--json", action="store_true", help="Print the receipt as JSON.")
    send.set_defaults(func=intercom_command)

    listing = subs.add_parser("list", help="List sessions", formatter_class=_help_formatter)
    listing.add_argument("--include-runs", action="store_true",
                         help="Include cron, subagent and oneshot runs.")
    listing.add_argument("--query", default=None, help="Filter on title or project.")
    listing.add_argument("--limit", type=int, default=None, help="How many rows (default 30).")
    listing.add_argument("--json", action="store_true", help="Print the rows as JSON.")
    listing.set_defaults(func=intercom_command)

    status = subs.add_parser("status", help="Show one message", formatter_class=_help_formatter)
    status.add_argument("message_id", metavar="MESSAGE_ID")
    status.add_argument("--json", action="store_true", help="Print the status as JSON.")
    status.set_defaults(func=intercom_command)

    log = subs.add_parser("log", help="Show recent messages", formatter_class=_help_formatter)
    log.add_argument("--to", default=None, help="Filter on the target title, id or address.")
    log.add_argument("--from", dest="from_label", default=None,
                     help="Filter on the sender label or id.")
    log.add_argument("--since", default=None, help="Window such as 30s, 15m, 24h or 7d. Default 24h.")
    log.add_argument("--json", action="store_true", help="Print the rows as JSON.")
    log.set_defaults(func=intercom_command)

    notice = subs.add_parser(
        "reboot-notice",
        help="Warn running sessions before a reboot",
        formatter_class=_help_formatter,
    )
    notice.add_argument(
        "--minutes", type=int, required=True,
        help="Minutes until the reboot (the N in shutdown -r +N).",
    )
    notice.add_argument(
        "--state-file", default=None,
        help="Record path. Default: plugin-data/intercom/reboot-pending.json.",
    )
    notice.add_argument(
        "--reason", default=None,
        help="Why the machine will reboot. Default: package updates.",
    )
    notice.set_defaults(func=intercom_command)

    resume = subs.add_parser(
        "resume-after-reboot",
        help="Wake sessions a reboot cut off",
        formatter_class=_help_formatter,
    )
    resume.add_argument(
        "--state-file", default=None,
        help="Record written by reboot-notice.",
    )
    resume.add_argument(
        "--max-age", default=None,
        help="How recent that record still counts: 30s, 15m, 6h, 7d. Default 6h.",
    )
    # Tests pin the boot. Operators take btime from /proc/stat.
    resume.add_argument("--boot-time", type=int, default=None, help=argparse.SUPPRESS)
    resume.set_defaults(func=intercom_command)


def main(argv=None) -> int:
    """Parse ``argv`` (without the ``intercom`` word) and return the exit code."""
    parser = argparse.ArgumentParser(prog="hermes intercom", formatter_class=_help_formatter)
    register_parser(parser)
    try:
        args = parser.parse_args(list(argv) if argv is not None else None)
    except SystemExit as exc:
        code = exc.code
        if code is None:
            return 0
        return code if isinstance(code, int) else 2
    return int(intercom_command(args))


def intercom_command(args: argparse.Namespace) -> int:
    verb = getattr(args, "intercom_command", None)
    if verb == "send":
        return _cmd_send(args)
    if verb == "list":
        return _cmd_list(args)
    if verb == "status":
        return _cmd_status(args)
    if verb == "log":
        return _cmd_log(args)
    if verb == "reboot-notice":
        return _cmd_reboot_notice(args)
    if verb == "resume-after-reboot":
        return _cmd_resume_after_reboot(args)
    print(
        "usage: hermes intercom "
        "{send,list,status,log,reboot-notice,resume-after-reboot}",
        file=sys.stderr,
    )
    return 2


def _ctx():
    return _state["ctx"] if _state["ctx"] is not None else _FallbackCtx()


def _exit_for(receipt: dict) -> int:
    if receipt.get("ok") is True:
        return 0
    error = receipt.get("error")
    if error in _EXIT:
        return _EXIT[error]
    code = receipt.get("exit")
    if isinstance(code, int) and not isinstance(code, bool):
        return code
    return 4


def _print_failure(receipt: dict) -> None:
    print(receipt.get("message") or receipt.get("error") or "refused", file=sys.stderr)


def _cmd_send(args) -> int:
    text, problem = _body(args)
    if problem:
        print(problem, file=sys.stderr)
        return 2
    receipt = route.send(
        args.from_label or "cli",
        args.to,
        text,
        ctx=_ctx(),
        delivery=args.delivery,
        reply_mode=args.reply,
        script=True,
    )
    if args.json:
        print(json.dumps(receipt, ensure_ascii=False))
        if receipt.get("ok") is not True:
            _print_failure(receipt)
        return _exit_for(receipt)
    if receipt.get("ok") is True:
        title = receipt.get("target_title") or ""
        print(f'message {receipt.get("message_id")} → "{title}": {_applied_phrase(receipt)}')
        return 0
    _print_failure(receipt)
    return _exit_for(receipt)


def _body(args):
    has_message = args.message is not None
    has_file = args.file is not None
    if has_message == has_file:
        return None, "usage: pass exactly one of --message or --file"
    if has_message:
        return args.message, None
    if args.file == "-":
        return sys.stdin.read(), None
    path = Path(args.file)
    try:
        return path.read_text(encoding="utf-8-sig"), None
    except OSError:
        return None, f"usage: cannot read --file {args.file}"


def _applied_phrase(receipt: dict) -> str:
    applied = receipt.get("applied")
    requested = receipt.get("requested")
    detail = receipt.get("applied_detail")
    message = receipt.get("message") or ""
    if applied == "steer":
        return "steered (into its running turn)"
    if applied == "queue" and requested and requested != "queue":
        return (f"queued (asked: {requested}; the {requested} was refused, "
                "runs after its current turn)")
    if applied == "queue":
        return "queued (runs after its current turn)"
    if applied == "interrupt" and detail == "redirect":
        return "interrupted (redirected its running turn)"
    if applied == "interrupt":
        return "interrupted (stopped its running turn; runs next)"
    if applied == "start" and "was not open" in message:
        return "started (was not open; the gateway reopened it and a turn started there)"
    if applied == "start":
        return "started (was idle; a turn started)"
    return str(applied or "delivered")


def _cmd_list(args) -> int:
    listed = address.list_sessions(
        include_runs=bool(args.include_runs),
        query=args.query,
        limit=args.limit,
    )
    if not isinstance(listed, dict) or listed.get("ok") is not True:
        receipt = listed if isinstance(listed, dict) else {"ok": False, "error": "list-failed"}
        if args.json:
            print(json.dumps(receipt, ensure_ascii=False))
        _print_failure(receipt)
        return _exit_for(receipt)
    rows = list(listed.get("sessions") or [])
    if args.json:
        print(json.dumps(rows, ensure_ascii=False))
        return 0
    for row in rows:
        print(_list_line(row))
    return 0


def _list_line(row: dict) -> str:
    project = row.get("project")
    if not isinstance(project, str) or not project.strip():
        project = "\u2014"
    return (f"{row.get('id') or ''}  {row.get('title') or ''}  {project}  "
            f"{_relative(row.get('last_active'))}  {row.get('state') or ''}")


def _relative(last) -> str:
    if isinstance(last, bool) or not isinstance(last, (int, float)):
        return "\u2014"
    delta = max(0.0, time.time() - float(last))
    if delta < 45:
        return "now"
    if delta < 3600:
        return f"{int(delta // 60) or 1} min ago"
    if delta < 86400:
        return f"{int(delta // 3600) or 1} h ago"
    return f"{int(delta // 86400) or 1} d ago"


def _cmd_status(args) -> int:
    message_id = args.message_id
    row = _record_row(message_id)
    if row is None:
        text = f"No message with id {message_id}."
        if args.json:
            print(json.dumps({"ok": False, "error": "no such id", "message": text},
                             ensure_ascii=False))
        print(text, file=sys.stderr)
        return 1
    check = _status_check(row)
    reply_line = _status_reply(row)
    rendered = _status_text(row, reply_line, check)
    if args.json:
        print(json.dumps({
            "ok": True,
            "id": row.get("id"),
            "target_session": row.get("target_session_id"),
            "from": _from_label(row),
            "to": _to_label(row),
            "sent": _sent(row),
            "delivery": _delivery_words(row),
            "reply": reply_line,
            "check": check,
            "text": rendered,
        }, ensure_ascii=False))
        return 0
    print(rendered)
    return 0


def _status_text(row: dict, reply_line: str, check) -> str:
    lines = [
        f"message {row.get('id')}",
        f"  from      {_from_label(row)}",
        f"  to        {_to_label(row)}",
        f"  sent      {_sent(row)}",
        f"  delivery  {_delivery_words(row)}",
        f"  reply     {reply_line}",
    ]
    parent = row.get("in_reply_to")
    if isinstance(parent, str) and parent:
        # Two spaces after "to" so the message id lines up with the other values.
        lines.append(f"  note      in reply to  message {parent}")
    if check:
        lines.append(f"  check     {check}")
    return "\n".join(lines)


def _from_label(row: dict) -> str:
    label = row.get("sender_label") or ""
    if row.get("sender_source") == "script" or not row.get("sender_session_id"):
        return f'script "{label}"'
    return f'session "{label}"'


def _to_label(row: dict) -> str:
    title = row.get("target_title") or ""
    session_id = row.get("target_session_id") or ""
    if session_id:
        return f'"{title}" ({session_id})'
    return f'"{title}"'


def _sent(row: dict) -> str:
    created = row.get("created_at")
    if not isinstance(created, (int, float)) or isinstance(created, bool):
        return ""
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(created))


def _clock(stamp) -> str:
    if not isinstance(stamp, (int, float)) or isinstance(stamp, bool):
        return ""
    return time.strftime("%H:%M:%S", time.localtime(stamp))


def _delivery_words(row: dict) -> str:
    result = row.get("result")
    detail = row.get("detail")
    if result is None:
        return "unknown"
    if result == "refused":
        return f"refused ({detail})" if detail else "refused"
    if result == "error":
        return f"error ({detail})" if detail else "error"
    applied = row.get("applied")
    word = _APPLIED_WORD.get(applied, applied or "unknown")
    requested = row.get("requested")
    if applied == "queue" and requested and requested != "queue":
        return f"{word} (asked: {requested})"
    return word


def _needs_check(row: dict) -> bool:
    """Extra read only when the result was never written, or delivery is unknown."""
    return row.get("result") is None or row.get("detail") == "delivery-unknown"


def _status_check(row: dict):
    if not _needs_check(row):
        return None
    admission = row.get("admission_id")
    applied = row.get("applied")
    if isinstance(admission, str) and admission and applied in ("queue", "start"):
        return _admitted_line(admission, row.get("target_session_id"))
    return _transcript_line(row)


def _admitted_line(admission_id: str, session_id) -> str:
    try:
        answer = client.receipt(admission_id, session_id=session_id or "")
    except Exception:  # health: allow BLE001 -- a receipt failure is reported as not admitted
        return "not admitted"
    if not isinstance(answer, dict) or answer.get("ok") is not True:
        return "not admitted"
    return {
        "queued": "admitted: queued",
        "started": "admitted: started",
        "terminal": "admitted: finished",
    }.get(answer.get("status"), "not admitted")


def _transcript_line(row: dict) -> str:
    found = _transcript_timestamp(row.get("target_session_id"), row.get("header_sha"))
    if found is not None:
        return f"found in the target's transcript ({_clock(found)})"
    created = row.get("created_at")
    base = "not found in the target's transcript (yet)"
    if isinstance(created, (int, float)) and not isinstance(created, bool) and time.time() - created < 60:
        return f"{base}: the send ended less than a minute ago, so run status again in a minute"
    return f"{base}: sending again will not duplicate it"


def _transcript_timestamp(session_id, header_sha):
    """Read-only hash check of the recorded target session. None when it is absent.

    Matches a ``role='user'`` row with ``active=1`` or ``compacted=1`` whose framed
    span hashes to ``header_sha``. Does not open the database for writing.
    """
    if not isinstance(session_id, str) or not session_id:
        return None
    if not isinstance(header_sha, str) or not header_sha:
        return None
    path = _state_db()
    if not path.is_file():
        return None
    try:
        conn = _ro(path)
    except sqlite3.Error:
        return None
    try:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT content, timestamp FROM messages "
            "WHERE session_id = ? AND role = 'user' AND (active = 1 OR compacted = 1)",
            (session_id,),
        ).fetchall()
    except sqlite3.Error:
        return None
    finally:
        conn.close()
    for row in rows:
        content = row["content"]
        if not isinstance(content, str):
            continue
        for span in replies.find_spans(content):
            if span.sha == header_sha:
                stamp = row["timestamp"]
                return stamp if isinstance(stamp, (int, float)) else 0
    return None


def _status_reply(row: dict) -> str:
    state = row.get("reply_state")
    when = _clock(row.get("replied_at"))
    text = row.get("reply") if isinstance(row.get("reply"), str) else ""
    extra = f" {text}" if text else ""
    if row.get("kind") == "ask":
        if state in ("sent", "silent"):
            return f"answered by reply ({when}){extra}"
        return "no reply recorded"
    if state is None:
        return "no reply recorded"
    if state == "silent":
        return f"silent ({when})"
    if state == "no-block":
        return "no reply recorded"
    child = _record_row(row.get("reply_message_id")) if row.get("reply_message_id") else None
    if state == "stored":
        reason = _undelivered(child)
        if reason and row.get("reply_message_id"):
            return (f"stored, not delivered ({when}): message {row['reply_message_id']}, "
                    f"{reason}{extra}")
        return f"stored ({when}){extra}"
    if state == "sent" and row.get("reply_message_id"):
        reason = _undelivered(child)
        if reason:
            return (f"stored, not delivered ({when}): message {row['reply_message_id']}, "
                    f"{reason}{extra}")
        applied = (child or {}).get("applied") or "steer"
        word = _APPLIED_WORD.get(applied, "steered")
        return f"sent as message {row['reply_message_id']}, {word} ({when}){extra}"
    return "no reply recorded"


def _undelivered(child) -> str | None:
    if not isinstance(child, dict):
        return None
    if child.get("result") in ("error", "refused"):
        return child.get("detail") or child.get("result")
    return None


def _cmd_log(args) -> int:
    window = _parse_since(args.since)
    if window is None:
        print(f"usage: invalid --since {args.since}", file=sys.stderr)
        return 2
    rows = _record_rows()
    if rows is None:
        if args.json:
            print("[]")
        return 0
    cutoff = time.time() - window
    kept = []
    for row in rows:
        created = row.get("created_at")
        if not isinstance(created, (int, float)) or isinstance(created, bool) or created < cutoff:
            continue
        if args.to and not _contains(args.to, row.get("target_title"), row.get("target_session_id"),
                                     row.get("address")):
            continue
        if args.from_label and not _contains(args.from_label, row.get("sender_label"),
                                             row.get("sender_session_id")):
            continue
        kept.append(row)
    kept.sort(key=lambda row: (row.get("created_at") or 0, row.get("id") or ""), reverse=True)
    by_id = {row.get("id"): row for row in rows}
    if args.json:
        print(json.dumps([_log_item(row, by_id) for row in kept], ensure_ascii=False))
        return 0
    for row in kept:
        print(_log_line(row, by_id))
    return 0


def _parse_since(text):
    if text is None or text == "":
        return _DEFAULT_SINCE
    if not isinstance(text, str):
        return None
    match = _SINCE.fullmatch(text.strip())
    if match is None:
        return None
    return int(match.group(1)) * _SINCE_UNIT[match.group(2)]


def _contains(needle, *values) -> bool:
    folded = needle.casefold()
    return any(isinstance(value, str) and folded in value.casefold() for value in values)


def _log_item(row: dict, by_id: dict) -> dict:
    return {
        "id": row.get("id"),
        "created_at": row.get("created_at"),
        "from": _from_label(row),
        "to": _to_label(row),
        "delivery": _log_delivery(row),
        "reply": _log_reply(row, by_id),
        "in_reply_to": row.get("in_reply_to"),
        "line": _log_line(row, by_id),
    }


def _log_line(row: dict, by_id: dict) -> str:
    created = row.get("created_at")
    clock = time.strftime("%H:%M", time.localtime(created)) if isinstance(created, (int, float)) else ""
    suffix = ""
    parent = row.get("in_reply_to")
    if isinstance(parent, str) and parent:
        suffix = f"  (reply to {parent})"
    target = f'session "{row.get("target_title") or row.get("target_session_id") or ""}"'
    return (f"{clock}  message {row.get('id')}  {_from_label(row)} → {target}  "
            f"{_log_delivery(row)}  {_log_reply(row, by_id)}{suffix}")


def _log_delivery(row: dict) -> str:
    result = row.get("result")
    detail = row.get("detail")
    if result == "refused":
        return f"refused ({detail})" if detail else "refused"
    if result == "error":
        return f"error ({detail})" if detail else "error"
    applied = row.get("applied")
    word = _APPLIED_WORD.get(applied, applied or (result or "unknown"))
    requested = row.get("requested")
    if applied == "queue" and requested and requested != "queue":
        return f"queued (asked {requested})"
    return word


def _log_reply(row: dict, by_id: dict) -> str:
    state = row.get("reply_state")
    detail = row.get("detail")
    if state == "stored" and detail == "shared-chat-no-block":
        return "reply: stored (shared chat, no [REPLY] block)"
    if row.get("reply_mode") == "none" and state is None:
        return "reply: none (not asked)"
    if state == "no-block":
        return "reply: none (no [REPLY] block)"
    if state == "silent":
        return "reply: silent"
    if row.get("kind") == "ask" and state in ("sent", "silent"):
        return "reply: answered"
    child_id = row.get("reply_message_id")
    child = by_id.get(child_id) if child_id else None
    reason = _undelivered(child)
    if child_id and reason:
        return f"reply: {child_id} not delivered ({reason})"
    if child_id:
        return f"reply: {child_id}"
    if state == "stored":
        return "reply: stored"
    return "reply: none recorded"


def _hermes_home() -> Path:
    from hermes_constants import get_hermes_home

    return Path(get_hermes_home())


def _record_path() -> Path:
    return _hermes_home() / "plugin-data" / "intercom" / "record.db"


def _state_db() -> Path:
    return _hermes_home() / "state.db"


def _ro(path: Path) -> sqlite3.Connection:
    return sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)


def _record_rows():
    """Every record row, or None when the file does not exist.

    A missing file is left missing: this never calls ``record.connect``.
    """
    path = _record_path()
    if not path.is_file():
        return None
    try:
        conn = _ro(path)
    except sqlite3.Error:
        return []
    try:
        conn.row_factory = sqlite3.Row
        found = conn.execute("SELECT * FROM messages").fetchall()
        return [dict(row) for row in found]
    except sqlite3.Error:
        return []
    finally:
        conn.close()


def _record_row(message_id):
    if not isinstance(message_id, str) or not message_id:
        return None
    rows = _record_rows()
    if not rows:
        return None
    for row in rows:
        if row.get("id") == message_id:
            return row
    return None


# ── reboot hooks ─────────────────────────────────────────────────────────────
#
# reboot-notice records running sessions and steers a warning. resume-after-reboot
# queues one wake per session per boot, from that record or, when the record is
# missing or stale, from sessions still mid-turn. A failed send is reported and
# not repeated. Neither command adds a cap, a hold or an approval gate.

_DEFAULT_REASON = "package updates"
_NOTICE_SENDER = "reboot-notice"
_RESUME_SENDER = "resume-after-reboot"
_PENDING_NAME = "reboot-pending.json"
# The owner's stored session.list is 200 rows. A limit of 30 would drop the rest.
_REBOOT_LIST_LIMIT = 10000
_EXCLUDED_RUNS = frozenset({"cron", "subagent"})
_OWNER_WAIT_S = 300
_OWNER_POLL_S = 5
_DEFAULT_MAX_AGE = 6 * 3600
_UNPLANNED_WINDOW_S = 900
_PROC_STAT = Path("/proc/stat")
_RESUME_TAIL = (
    "and your turn was cut off. Continue where you left off. "
    "Processes you started before the reboot (background jobs, systemd-run units, "
    "tmux windows) did not survive: check them and restart what you still need."
)
_LATEST_SQL = (
    "SELECT role, tool_calls, timestamp FROM messages "
    "WHERE session_id = ? AND active = 1 ORDER BY id DESC LIMIT 1"
)
_AFTER_BOOT_SQL = (
    "SELECT 1 FROM messages WHERE session_id = ? AND active = 1 AND timestamp > ? LIMIT 1"
)


def _cmd_reboot_notice(args) -> int:
    reason = _clean_reason(getattr(args, "reason", None))
    listed = address.list_sessions(include_runs=True, limit=_REBOOT_LIST_LIMIT)
    if not isinstance(listed, dict) or listed.get("ok") is not True:
        receipt = listed if isinstance(listed, dict) else {"ok": False, "error": "list-failed"}
        _print_failure(receipt)
        return 0
    running = _running_from(listed.get("sessions"))
    _atomic_json(_state_path(getattr(args, "state_file", None)), {
        "written": time.time(),
        "minutes": args.minutes,
        "reason": reason,
        "sessions": running,
    })
    if not running:
        print("no running sessions")
        return 0
    _send_notice(running, _notice_text(args.minutes, reason))
    return 0


def _cmd_resume_after_reboot(args) -> int:
    max_age = _parse_max_age(getattr(args, "max_age", None))
    if max_age is None:
        print(f"usage: invalid --max-age {args.max_age}", file=sys.stderr)
        return 2
    boot = _resolve_boot(getattr(args, "boot_time", None))
    if boot is None:
        print("usage: no boot time; /proc/stat has no btime (Linux only)", file=sys.stderr)
        return 2
    state = _state_path(getattr(args, "state_file", None))
    if _same_boot(_recorded_boot(state), boot):
        print("already resumed for this boot")
        return 0
    owner = _wait_for_owner()
    if not isinstance(owner, dict) or owner.get("ok") is not True:
        receipt = owner if isinstance(owner, dict) else {"ok": False, "error": "no-owner"}
        _print_failure(receipt)
        if receipt.get("error") in ("no-owner", "owner-restarting", None):
            return 5
        return _exit_for(receipt)
    kind, payload = _classify_pending(state, max_age)
    if kind == "fresh":
        targets = _session_rows(payload)
        text = _resume_text(_boot_clock(boot), _reason_of(payload), unplanned=False)
    else:
        targets = _unplanned_targets(boot)
        if targets is None:
            return 0
        text = _resume_text(_boot_clock(boot), None, unplanned=True)
    if targets:
        _send_resume(targets, text, boot)
    else:
        print("no sessions to resume")
    _write_done(state, boot, retire=kind in ("fresh", "stale"))
    return 0


def _notice_text(minutes, reason) -> str:
    return (
        f"This machine will reboot in about {minutes} minutes ({reason}). "
        "Your current turn and any processes you started (background jobs, "
        "systemd-run units, tmux windows) will stop. If it matters, write down "
        "where you are now. After the reboot you will get a message telling you "
        "to continue."
    )


def _resume_text(when, reason, *, unplanned: bool) -> str:
    if unplanned:
        head = f"This machine restarted unexpectedly at {when}"
    else:
        head = f"This machine rebooted at {when} ({reason})"
    return f"{head} {_RESUME_TAIL}"


def _boot_clock(boot: int) -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(boot))


def _clean_reason(text) -> str:
    if isinstance(text, str) and text.strip():
        return text.strip()
    return _DEFAULT_REASON


def _reason_of(payload) -> str:
    if not isinstance(payload, dict):
        return _DEFAULT_REASON
    return _clean_reason(payload.get("reason"))


def _parse_max_age(text):
    """Seconds, parsed like ``log --since``. ``None`` text is the 6h default.

    A blank or unrecognised value is ``None`` so the caller can exit 2.
    ``_parse_since`` treats a blank as 24h, which is the wrong default here.
    """
    if text is None:
        return _DEFAULT_MAX_AGE
    if not isinstance(text, str) or _SINCE.fullmatch(text.strip()) is None:
        return None
    return _parse_since(text.strip())


def _resolve_boot(override):
    """``--boot-time`` wins. Otherwise Linux ``btime``. ``None`` if neither works."""
    if override is not None:
        if isinstance(override, bool) or not isinstance(override, int):
            return None
        return override
    return _btime_from_proc()


def _btime_from_proc():
    try:
        text = _PROC_STAT.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    for line in text.splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[0] == "btime" and parts[1].isdigit():
            return int(parts[1])
    return None


def _state_path(arg) -> Path:
    if isinstance(arg, str) and arg.strip():
        return Path(arg)
    return _hermes_home() / "plugin-data" / "intercom" / _PENDING_NAME


def _done_path(state: Path) -> Path:
    name = state.name
    if name.endswith(".json"):
        done_name = name[: -len(".json")] + ".done.json"
    else:
        done_name = name + ".done.json"
    return state.with_name(done_name)


def _ensure_parent(path: Path) -> None:
    directory = path.parent
    directory.mkdir(parents=True, exist_ok=True)
    reboot_dir = _hermes_home() / "plugin-data" / "intercom"
    try:
        same = directory.resolve() == reboot_dir.resolve()
    except OSError:
        same = False
    if same:
        os.chmod(directory, 0o700)


def _atomic_json(path: Path, payload: dict) -> None:
    """Write ``path`` as 0600 via ``O_EXCL`` and ``os.replace``. The umask stays put.

    The open mode is 0600, then ``fchmod`` puts back any bit a umask cleared.
    ``plugin-data/intercom`` is 0700. Other parents are only created.
    """
    _ensure_parent(path)
    data = (json.dumps(payload, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{time.time_ns()}.tmp")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    fd = os.open(os.fspath(tmp), flags, 0o600)
    try:
        os.fchmod(fd, 0o600)
        view = memoryview(data)
        while view:
            written = os.write(fd, view)
            if written <= 0:
                raise OSError("short write")
            view = view[written:]
        os.fsync(fd)
    except BaseException:
        os.close(fd)
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    os.close(fd)
    try:
        os.replace(os.fspath(tmp), os.fspath(path))
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    os.chmod(path, 0o600)
    try:
        dir_fd = os.open(os.fspath(path.parent), os.O_RDONLY)
    except OSError:
        return
    try:
        try:
            os.fsync(dir_fd)
        except OSError:
            pass
    finally:
        os.close(dir_fd)


def _read_json(path: Path):
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return None


def _recorded_boot(state: Path):
    payload = _read_json(_done_path(state))
    if not isinstance(payload, dict):
        return None
    return payload.get("boot_time")


def _same_boot(recorded, boot: int) -> bool:
    if isinstance(recorded, bool) or not isinstance(recorded, (int, float)):
        return False
    return int(recorded) == int(boot)


def _classify_pending(path: Path, max_age: int):
    """``fresh``, ``stale``, ``absent`` or ``invalid``, and the parsed payload."""
    if not path.is_file():
        return "absent", None
    payload = _read_json(path)
    sessions = payload.get("sessions") if isinstance(payload, dict) else None
    if not isinstance(sessions, list):
        return "invalid", payload
    if _pending_is_fresh(path, payload, max_age):
        return "fresh", payload
    return "stale", payload


def _pending_is_fresh(path: Path, payload: dict, max_age: int) -> bool:
    """Younger than ``max_age``. The file's ``written`` time, else its mtime."""
    written = payload.get("written")
    if isinstance(written, bool) or not isinstance(written, (int, float)):
        try:
            written = path.stat().st_mtime
        except OSError:
            return False
    return time.time() - float(written) < max_age


def _write_done(state: Path, boot: int, *, retire: bool) -> None:
    done = _done_path(state)
    _atomic_json(done, {"boot_time": int(boot)})
    if not retire:
        return
    try:
        if state.resolve() == done.resolve():
            return
    except OSError:
        return
    try:
        os.unlink(state)
    except FileNotFoundError:
        return
    except OSError as exc:
        detail = exc.strerror or exc
        print(
            f"resume-after-reboot: could not retire {state.name}: {detail}",
            file=sys.stderr,
        )


def _wait_for_owner():
    """Poll until the gateway answers. Boot ordering, not a delivery retry.

    ``client.list_sessions`` discovers an endpoint before any connect, so this
    never calls ``connect_gateway`` itself. ``no-owner`` and ``owner-restarting``
    keep waiting. Any other result is returned at once. Nothing is sent here.
    """
    tries = (_OWNER_WAIT_S // _OWNER_POLL_S) + 1
    last = {"ok": False, "error": "no-owner"}
    for attempt in range(tries):
        found = client.list_sessions("live")
        if isinstance(found, dict) and found.get("ok") is True:
            return found
        last = found if isinstance(found, dict) else {"ok": False, "error": "no-owner"}
        if last.get("error") not in ("no-owner", "owner-restarting"):
            return last
        if attempt + 1 == tries:
            return last
        time.sleep(_OWNER_POLL_S)
    return last


def _running_from(rows) -> list:
    """Running sessions, cron and subagent dropped, oneshot kept."""
    chosen = []
    if not isinstance(rows, list):
        return chosen
    for row in rows:
        if not isinstance(row, dict) or row.get("state") != "running":
            continue
        source = row.get("source")
        source = source.strip() if isinstance(source, str) else ""
        if source in _EXCLUDED_RUNS:
            continue
        item = _id_title(row)
        if item is not None:
            chosen.append(item)
    return chosen


def _session_rows(payload) -> list:
    rows = payload.get("sessions") if isinstance(payload, dict) else None
    if not isinstance(rows, list):
        return []
    chosen = []
    for row in rows:
        if isinstance(row, dict):
            item = _id_title(row)
            if item is not None:
                chosen.append(item)
    return chosen


def _id_title(row: dict):
    sid = row.get("id")
    if not isinstance(sid, str) or not sid.strip():
        return None
    title = row.get("title")
    title = title.strip() if isinstance(title, str) else ""
    return {"id": sid.strip(), "title": title}


def _send_notice(targets, text) -> None:
    for item in targets:
        receipt = route.send(
            _NOTICE_SENDER, item["id"], text, ctx=_ctx(),
            delivery="steer", reply_mode="none", script=True,
        )
        print(_receipt_line(item["id"], receipt))


def _send_resume(targets, text, boot: int) -> None:
    conn = _open_state_ro()
    try:
        for item in targets:
            sid = item["id"]
            if conn is not None and _active_after(conn, sid, boot):
                print(f"{sid} skipped: a message was stored after boot")
                continue
            receipt = route.send(
                _RESUME_SENDER, sid, text, ctx=_ctx(),
                delivery="queue", reply_mode="none", script=True,
            )
            print(_receipt_line(sid, receipt))
    finally:
        if conn is not None:
            conn.close()


def _receipt_line(session_id, receipt) -> str:
    if isinstance(receipt, dict) and receipt.get("ok") is True:
        title = receipt.get("target_title") or ""
        mid = receipt.get("message_id")
        phrase = _applied_phrase(receipt)
        return f'{session_id} message {mid} → "{title}": {phrase}'
    if not isinstance(receipt, dict):
        return f"{session_id} refused"
    detail = receipt.get("message") or receipt.get("error") or "refused"
    if not isinstance(detail, str) or not detail:
        detail = "refused"
    return f"{session_id} {detail.splitlines()[0]}"


def _open_state_ro():
    """Read-only ``state.db``, or None when it is missing or cannot be opened."""
    path = _state_db()
    if not path.is_file():
        return None
    try:
        conn = _ro(path)
    except sqlite3.Error:
        return None
    conn.row_factory = sqlite3.Row
    return conn


def _latest_active(conn, session_id):
    try:
        return conn.execute(_LATEST_SQL, (session_id,)).fetchone()
    except sqlite3.Error:
        return None


def _active_after(conn, session_id, boot: int) -> bool:
    """True when any active row was stored after boot. A read error is not that."""
    try:
        row = conn.execute(_AFTER_BOOT_SQL, (session_id, boot)).fetchone()
    except sqlite3.Error:
        return False
    return row is not None


def _unplanned_targets(boot: int):
    """Sessions (never cron or subagent) mid-turn in the 15 minutes before boot.

    The newest active row's timestamp has to fall in ``[boot - 900, boot]``, and
    that row must not be a final assistant answer. Listing failure returns None
    and prints the error; nothing is sent and the done file is left untouched.
    """
    listed = address.list_sessions(include_runs=True, limit=_REBOOT_LIST_LIMIT)
    if not isinstance(listed, dict) or listed.get("ok") is not True:
        receipt = listed if isinstance(listed, dict) else {"ok": False, "error": "list-failed"}
        _print_failure(receipt)
        return None
    conn = _open_state_ro()
    try:
        chosen = []
        for row in listed.get("sessions") or []:
            if not isinstance(row, dict):
                continue
            source = row.get("source")
            source = source.strip() if isinstance(source, str) else ""
            if source in _EXCLUDED_RUNS:
                continue
            item = _id_title(row)
            if item is None or conn is None:
                continue
            latest = _latest_active(conn, item["id"])
            if latest is None or not _in_reboot_window(latest, boot):
                continue
            if _is_final_answer(latest["role"], latest["tool_calls"]):
                continue
            chosen.append(item)
        return chosen
    finally:
        if conn is not None:
            conn.close()


def _in_reboot_window(row, boot: int) -> bool:
    stamp = row["timestamp"]
    if isinstance(stamp, bool) or not isinstance(stamp, (int, float)):
        return False
    return (boot - _UNPLANNED_WINDOW_S) <= stamp <= boot


def _is_final_answer(role, tool_calls) -> bool:
    """An assistant turn that did not call a tool. Anything else is still open."""
    return role == "assistant" and _tool_calls_empty(tool_calls)


def _tool_calls_empty(value) -> bool:
    if value is None:
        return True
    if isinstance(value, (bytes, bytearray)):
        value = bytes(value).decode("utf-8", "replace")
    if isinstance(value, (list, tuple)):
        return len(value) == 0
    if not isinstance(value, str):
        return False
    text = value.strip()
    if text in ("", "[]", "null"):
        return True
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        return False
    return parsed is None or parsed == []
