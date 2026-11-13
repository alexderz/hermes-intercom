"""Reboots: reboot-notice and resume-after-reboot.

The owner is faked the way test_cli.py fakes it (FakeOwner, patched
client.list_sessions) and test_route.py fakes delivery (patched
client.deliver_sync). Sessions and transcripts are a real SessionDB in the
temp HERMES_HOME, as test_address.py and test_cli.py do.
"""

from __future__ import annotations

import json
import os
import sqlite3
import time
import unittest
from pathlib import Path
from unittest import mock

from tests.test_cli import CliTestBase

BOOT = 1_700_000_000
TOOL = '[{"id": "t1"}]'
NOTICE = (
    "This machine will reboot in about {minutes} minutes ({reason}). "
    "Your current turn and any processes you started (background jobs, "
    "systemd-run units, tmux windows) will stop. If it matters, write down "
    "where you are now. After the reboot you will get a message telling you "
    "to continue."
)
RESUME_TAIL = (
    "and your turn was cut off. Continue where you left off. "
    "Processes you started before the reboot (background jobs, systemd-run units, "
    "tmux windows) did not survive: check them and restart what you still need."
)


def _clock(boot):
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(boot))


def _planned(boot, reason):
    return f"This machine rebooted at {_clock(boot)} ({reason}) {RESUME_TAIL}"


def _unplanned(boot):
    return f"This machine restarted unexpectedly at {_clock(boot)} {RESUME_TAIL}"


class RebootTests(CliTestBase):
    def setUp(self):
        super().setUp()
        self.write_config(consent=True, enabled=[])
        self.open_db()
        self.live_ids = []
        self.stored = []
        self.owner_error = None
        self.live_script = []

    def use_owner(self):
        self.close_db()

        def list_sessions(scope="live", picker=False):
            self.list_calls.append((scope, picker))
            if self.owner_error is not None:
                return {
                    "ok": False,
                    "error": self.owner_error,
                    "message": self.owner_error,
                }
            if scope == "live" and self.live_script:
                return self.live_script.pop(0)
            if scope == "live":
                sessions = [{"id": sid} for sid in self.live_ids]
                return {"ok": True, "sessions": sessions}
            return {"ok": True, "sessions": list(self.stored)}

        deliver = mock.patch.object(self.plugin.client, "deliver_sync", self.owner)
        listing = mock.patch.object(self.plugin.client, "list_sessions", list_sessions)
        deliver.start()
        listing.start()
        self.addCleanup(deliver.stop)
        self.addCleanup(listing.stop)

    def stored_row(self, sid, title, source="cli", running=False, last=0):
        return {
            "id": sid,
            "title": title,
            "source": source,
            "running": running,
            "last_active_at": last,
            "workspace": "",
        }

    def add_message(self, sid, role, stamp, tool_calls="[]", active=1):
        self.db._write_sql(
            "INSERT INTO messages "
            "(session_id, role, content, tool_calls, timestamp, active, compacted) "
            "VALUES (?, ?, ?, ?, ?, ?, 0)",
            (sid, role, "work", tool_calls, float(stamp), active),
        )

    def pending_path(self):
        return self.home / "plugin-data" / "intercom" / "reboot-pending.json"

    def done_path(self):
        return self.home / "plugin-data" / "intercom" / "reboot-pending.done.json"

    def write_pending(self, sessions, *, written, reason="kernel", minutes=5):
        path = self.pending_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        body = {
            "written": written,
            "minutes": minutes,
            "reason": reason,
            "sessions": sessions,
        }
        path.write_text(json.dumps(body), encoding="utf-8")
        return path

    def mode_of(self, path):
        return path.stat().st_mode & 0o777

    def message_rows(self):
        path = self.home / "state.db"
        uri = path.resolve().as_uri() + "?mode=ro"
        conn = sqlite3.connect(uri, uri=True)
        try:
            return conn.execute(
                "SELECT session_id, role, tool_calls, timestamp, active "
                "FROM messages ORDER BY id"
            ).fetchall()
        finally:
            conn.close()

    def snapshot(self):
        found = []
        for path in sorted(self.home.rglob("*")):
            key = path.relative_to(self.home).as_posix()
            if path.is_symlink() or not path.is_file():
                found.append((key, None))
            else:
                found.append((key, path.read_bytes()))
        return found

    def queue_ok(self):
        self.owner.answer = {
            "ok": True,
            "applied": "queue",
            "applied_detail": None,
            "admission_id": None,
        }

    def test_help_and_usage(self):
        code, out, err = self.invoke(["reboot-notice", "--help"])
        self.assertEqual(code, 0, err)
        self.assertIn("--minutes", out)
        self.assertIn("--reason", out)
        code, out, err = self.invoke(["resume-after-reboot", "--help"])
        self.assertEqual(code, 0, err)
        self.assertIn("--max-age", out)
        self.assertNotIn("--boot-time", out)
        code, _out, err = self.invoke(["reboot-notice"])
        self.assertEqual(code, 2, err)
        code, _out, err = self.invoke(["resume-after-reboot", "--max-age", "week"])
        self.assertEqual(code, 2, err)
        self.assertIn("invalid --max-age", err)

    def test_notice_warns_running_sessions_and_records_them(self):
        specs = [
            ("local-tui", "Desk", "tui", True, 500),
            ("local-busy", "Reviewer", "cli", True, 400),
            ("local-once", "Once", "oneshot", True, 300),
            ("local-cronjob", "Keep", "cronjob", True, 200),
            ("local-cron", "Nightly", "cron", True, 900),
            ("local-sub", "Child", "subagent", True, 800),
            ("local-spaced", "Spaced", " cron ", True, 700),
            ("local-idle", "Quiet", "cli", False, 600),
            ("local-closed", "Done", "cli", False, 100),
            ("local-once-idle", "Wait", "oneshot", False, 50),
        ]
        for sid, title, source, _running, _last in specs:
            self.add_session(sid, title, source=source.strip() or "cli")
        self.stored = [
            self.stored_row(sid, title, source, running, last)
            for sid, title, source, running, last in specs
        ]
        self.live_ids = ["local-tui", "local-busy"]
        self.use_owner()
        seen = []
        real = self.plugin.address.list_sessions

        def wrapped(**kwargs):
            seen.append(kwargs)
            return real(**kwargs)

        with mock.patch.object(self.plugin.address, "list_sessions", wrapped):
            code, out, err = self.invoke([
                "reboot-notice", "--minutes", "12", "--reason", "kernel",
            ])
        self.assertEqual(code, 0, err)
        self.assertEqual(err, "")
        self.assertTrue(seen)
        self.assertIs(seen[0]["include_runs"], True)
        self.assertEqual(seen[0]["limit"], 10000)
        wanted = ["local-tui", "local-busy", "local-once", "local-cronjob"]
        payload = json.loads(self.pending_path().read_text(encoding="utf-8"))
        self.assertEqual([item["id"] for item in payload["sessions"]], wanted)
        self.assertEqual(
            [item["title"] for item in payload["sessions"]],
            ["Desk", "Reviewer", "Once", "Keep"],
        )
        self.assertEqual(payload["minutes"], 12)
        self.assertEqual(payload["reason"], "kernel")
        self.assertLess(abs(payload["written"] - time.time()), 60)
        self.assertEqual(self.mode_of(self.pending_path()), 0o600)
        self.assertEqual(self.mode_of(self.pending_path().parent), 0o700)
        body = NOTICE.format(minutes=12, reason="kernel")
        self.assertEqual([call["target"] for call in self.owner.calls], wanted)
        for call in self.owner.calls:
            self.assertEqual(call["delivery"], "steer")
            self.assertIn(body, call["text"])
            self.assertIn("hermes@script reboot-notice", call["text"])
            self.assertIn("no reply needed", call["text"])
        lines = out.splitlines()
        self.assertEqual([line.split(" ", 1)[0] for line in lines], wanted)
        for line in lines:
            self.assertIn("steered (into its running turn)", line)
        rows = self.record_rows()
        self.assertEqual(len(rows), len(wanted))
        for row in rows:
            self.assertEqual(row["sender_source"], "script")
            self.assertEqual(row["sender_label"], "reboot-notice")
            self.assertEqual(row["reply_mode"], "none")
            self.assertEqual(row["requested"], "steer")
            self.assertEqual(row["body"], body)
        listed = real(include_runs=True, limit=10000)
        by_id = {item["id"]: item["source"] for item in listed["sessions"]}
        self.assertEqual(by_id["local-cron"], "cron")
        self.assertEqual(by_id["local-spaced"], "cron")
        self.assertEqual(by_id["local-once"], "oneshot")
        self.assertEqual(by_id["local-cronjob"], "cronjob")

    def test_notice_id_is_the_compression_tip(self):
        self.add_session("local-parent", "Parent")
        self.db.end_session("local-parent", "compression")
        self.add_session("local-child", "Child title", parent="local-parent")
        self.stored = [
            self.stored_row("local-parent", "Parent", running=True, last=5),
        ]
        self.use_owner()
        code, _out, err = self.invoke([
            "reboot-notice", "--minutes", "2", "--reason", "kernel",
        ])
        self.assertEqual(code, 0, err)
        payload = json.loads(self.pending_path().read_text(encoding="utf-8"))
        self.assertEqual(payload["sessions"], [{"id": "local-child", "title": "Parent"}])
        self.assertEqual(self.owner.calls[0]["target"], "local-child")

    def test_empty_notice_writes_an_empty_file_and_exits_0(self):
        for sid, source in (
            ("local-cron", "cron"),
            ("local-sub", "subagent"),
            ("local-idle", "cli"),
            ("local-once", "oneshot"),
        ):
            self.add_session(sid, sid, source=source)
        self.stored = [
            self.stored_row("local-cron", "Nightly", "cron", running=True, last=4),
            self.stored_row("local-sub", "Child", "subagent", running=True, last=3),
            self.stored_row("local-idle", "Quiet", "cli", running=False, last=2),
            self.stored_row("local-once", "Once", "oneshot", running=False, last=1),
        ]
        self.use_owner()
        previous = os.umask(0)
        self.addCleanup(os.umask, previous)
        code, out, err = self.invoke(["reboot-notice", "--minutes", "5"])
        current = os.umask(0)
        self.assertEqual(current, 0)
        self.assertEqual(code, 0, err)
        self.assertEqual(err, "")
        self.assertEqual(out, "no running sessions\n")
        self.assertEqual(self.owner.calls, [])
        payload = json.loads(self.pending_path().read_text(encoding="utf-8"))
        self.assertEqual(payload["sessions"], [])
        self.assertEqual(payload["minutes"], 5)
        self.assertEqual(payload["reason"], "package updates")
        self.assertEqual(self.mode_of(self.pending_path()), 0o600)
        self.assertEqual(self.mode_of(self.pending_path().parent), 0o700)

    def test_failed_notice_delivery_still_exits_0(self):
        self.add_session("local-ok", "Ok")
        self.add_session("local-bad", "Bad")
        self.stored = [
            self.stored_row("local-ok", "Ok", running=True, last=2),
            self.stored_row("local-bad", "Bad", running=True, last=1),
        ]
        self.use_owner()

        def deliver(target, text, delivery, submission_id):
            self.owner.calls.append({
                "target": target,
                "text": text,
                "delivery": delivery,
                "submission_id": submission_id,
            })
            if target == "local-bad":
                return {
                    "ok": False,
                    "error": "not-reachable",
                    "exit": 5,
                    "message": "down",
                }
            return {
                "ok": True,
                "applied": "steer",
                "applied_detail": None,
                "admission_id": None,
            }

        with mock.patch.object(self.plugin.client, "deliver_sync", deliver):
            code, out, err = self.invoke([
                "reboot-notice", "--minutes", "3", "--reason", "kernel",
            ])
        self.assertEqual(code, 0, err)
        self.assertEqual(
            [call["target"] for call in self.owner.calls],
            ["local-ok", "local-bad"],
        )
        self.assertEqual([line.split(" ", 1)[0] for line in out.splitlines()],
                         ["local-ok", "local-bad"])
        self.assertIn("Not delivered", out)
        self.assertIn("local-bad", out)
        payload = json.loads(self.pending_path().read_text(encoding="utf-8"))
        self.assertEqual(
            [item["id"] for item in payload["sessions"]],
            ["local-ok", "local-bad"],
        )

    def test_notice_without_an_owner_writes_nothing_and_exits_0(self):
        self.add_session("local-busy", "Reviewer")
        self.stored = [self.stored_row("local-busy", "Reviewer", running=True, last=1)]
        for error in ("no-owner", "owner-restarting"):
            with self.subTest(error=error):
                self.owner_error = error
                self.owner.calls.clear()
                self.use_owner()
                with mock.patch.object(self.plugin.cli.time, "sleep",
                                       side_effect=AssertionError("slept")):
                    code, out, err = self.invoke([
                        "reboot-notice", "--minutes", "5",
                    ])
                self.assertEqual(code, 0, err)
                self.assertEqual(out, "")
                self.assertIn(error, err)
                self.assertFalse(self.pending_path().exists())
                self.assertEqual(self.owner.calls, [])

    def test_resume_from_a_fresh_file_queues_and_a_second_run_is_quiet(self):
        self.add_session("local-target", "Reviewer")
        self.add_session("local-other", "Other")
        self.add_message("local-target", "assistant", BOOT - 20, "[]")
        self.add_message("local-other", "assistant", BOOT - 30, TOOL)
        self.stored = [
            self.stored_row("local-target", "Reviewer", running=True, last=5),
            self.stored_row("local-other", "Other", running=False, last=4),
        ]
        self.use_owner()
        code, _out, err = self.invoke([
            "reboot-notice", "--minutes", "5", "--reason", "kernel",
        ])
        self.assertEqual(code, 0, err)
        self.assertEqual([call["target"] for call in self.owner.calls], ["local-target"])
        self.owner.calls.clear()
        self.queue_ok()
        before = self.message_rows()
        code, out, err = self.invoke([
            "resume-after-reboot", "--boot-time", str(BOOT),
        ])
        self.assertEqual(code, 0, err)
        self.assertEqual(err, "")
        self.assertEqual([call["target"] for call in self.owner.calls], ["local-target"])
        call = self.owner.calls[0]
        self.assertEqual(call["delivery"], "queue")
        self.assertIn(_planned(BOOT, "kernel"), call["text"])
        self.assertNotIn("unexpectedly", call["text"])
        self.assertIn("hermes@script resume-after-reboot", call["text"])
        self.assertIn("no reply needed", call["text"])
        self.assertIn("queued (runs after its current turn)", out)
        row = self.record_rows()[-1]
        self.assertEqual(row["sender_label"], "resume-after-reboot")
        self.assertEqual(row["sender_source"], "script")
        self.assertEqual(row["reply_mode"], "none")
        self.assertEqual(row["requested"], "queue")
        self.assertEqual(row["body"], _planned(BOOT, "kernel"))
        self.assertFalse(self.pending_path().exists())
        self.assertEqual(
            json.loads(self.done_path().read_text(encoding="utf-8")),
            {"boot_time": BOOT},
        )
        self.assertEqual(self.message_rows(), before)
        listed = len(self.list_calls)
        sent = len(self.owner.calls)
        code, out, err = self.invoke([
            "resume-after-reboot", "--boot-time", str(BOOT),
        ])
        self.assertEqual(code, 0, err)
        self.assertEqual(out.strip(), "already resumed for this boot")
        self.assertEqual(len(self.owner.calls), sent)
        self.assertEqual(len(self.list_calls), listed)

    def test_resume_skips_a_session_with_a_message_after_boot(self):
        self.add_session("local-later", "Later")
        self.add_session("local-kept", "Kept")
        self.add_session("local-edge", "Edge")
        self.add_message("local-later", "user", BOOT + 5, None)
        self.add_message("local-kept", "user", BOOT + 5, None, active=0)
        self.add_message("local-edge", "user", BOOT, None)
        self.write_pending([
            {"id": "local-later", "title": "Later"},
            {"id": "local-kept", "title": "Kept"},
            {"id": "local-edge", "title": "Edge"},
        ], written=time.time(), reason="kernel")
        self.use_owner()
        self.queue_ok()
        code, out, err = self.invoke([
            "resume-after-reboot", "--boot-time", str(BOOT),
        ])
        self.assertEqual(code, 0, err)
        self.assertEqual(
            [call["target"] for call in self.owner.calls],
            ["local-kept", "local-edge"],
        )
        self.assertIn("local-later skipped: a message was stored after boot", out)
        for call in self.owner.calls:
            self.assertEqual(call["delivery"], "queue")
            self.assertIn(_planned(BOOT, "kernel"), call["text"])

    def test_stale_file_uses_the_unplanned_path_and_is_renamed(self):
        self.add_session("local-planned", "Planned")
        self.add_session("local-open", "Open")
        self.add_message("local-planned", "assistant", BOOT - 20, "[]")
        self.add_message("local-open", "assistant", BOOT - 40, TOOL)
        self.stored = [
            self.stored_row("local-planned", "Planned", running=False, last=2),
            self.stored_row("local-open", "Open", running=False, last=1),
        ]
        self.write_pending(
            [{"id": "local-planned", "title": "Planned"}],
            written=time.time() - 7 * 3600,
            reason="kernel",
        )
        self.use_owner()
        self.queue_ok()
        code, out, err = self.invoke([
            "resume-after-reboot", "--boot-time", str(BOOT),
        ])
        self.assertEqual(code, 0, err)
        self.assertEqual([call["target"] for call in self.owner.calls], ["local-open"])
        self.assertIn(_unplanned(BOOT), self.owner.calls[0]["text"])
        self.assertNotIn("kernel", self.owner.calls[0]["text"])
        self.assertNotIn("local-planned", out)
        self.assertFalse(self.pending_path().exists())
        self.assertEqual(
            json.loads(self.done_path().read_text(encoding="utf-8")),
            {"boot_time": BOOT},
        )

    def test_unplanned_selection_skips_final_answers_and_runs(self):
        cases = [
            ("local-tool", "cli", "assistant", BOOT - 100, TOOL, 1, True),
            ("local-user", "cli", "user", BOOT - 80, None, 1, True),
            ("local-result", "cli", "tool", BOOT - 70, None, 1, True),
            ("local-at", "tui", "assistant", BOOT, TOOL, 1, True),
            ("local-early", "cli", "user", BOOT - 900, None, 1, True),
            ("local-once", "oneshot", "user", BOOT - 30, None, 1, True),
            ("local-final", "cli", "assistant", BOOT - 20, "[]", 1, False),
            ("local-final-null", "cli", "assistant", BOOT - 20, None, 1, False),
            ("local-final-word", "cli", "assistant", BOOT - 20, "null", 1, False),
            ("local-blank", "cli", "assistant", BOOT - 20, "", 1, False),
            ("local-old", "cli", "user", BOOT - 901, None, 1, False),
            ("local-future", "cli", "user", BOOT + 1, None, 1, False),
            ("local-cron", "cron", "assistant", BOOT - 10, TOOL, 1, False),
            ("local-sub", "subagent", "user", BOOT - 10, None, 1, False),
            ("local-spaced", " cron ", "user", BOOT - 10, None, 1, False),
        ]
        expected = []
        last = 1000
        for sid, source, role, stamp, calls, active, send in cases:
            self.add_session(sid, sid, source=source.strip() or "cli")
            self.add_message(sid, role, stamp, calls, active)
            self.stored.append(self.stored_row(sid, sid, source, last=last))
            last -= 1
            if send:
                expected.append(sid)
        self.add_session("local-quiet-after", "Quiet after")
        self.add_message("local-quiet-after", "assistant", BOOT - 15, TOOL)
        self.add_message("local-quiet-after", "user", BOOT + 20, None, active=0)
        self.stored.insert(
            0, self.stored_row("local-quiet-after", "Quiet after", last=2000),
        )
        expected.insert(0, "local-quiet-after")
        self.add_session("local-finished", "Finished")
        self.add_message("local-finished", "assistant", BOOT - 300, TOOL)
        self.add_message("local-finished", "assistant", BOOT - 40, "[]")
        self.stored.append(self.stored_row("local-finished", "Finished", last=0))
        self.use_owner()
        self.queue_ok()
        before = self.message_rows()
        with mock.patch(
            "hermes_state.SessionDB.get_messages",
            side_effect=AssertionError("get_messages"),
        ):
            code, out, err = self.invoke([
                "resume-after-reboot", "--boot-time", str(BOOT),
            ])
        self.assertEqual(code, 0, err)
        self.assertEqual(err, "")
        self.assertEqual([call["target"] for call in self.owner.calls], expected)
        for call in self.owner.calls:
            self.assertEqual(call["delivery"], "queue")
            self.assertIn(_unplanned(BOOT), call["text"])
            self.assertIn("no reply needed", call["text"])
            self.assertNotIn("package updates", call["text"])
        self.assertNotIn("local-final", out)
        self.assertNotIn("local-cron", out)
        self.assertNotIn("local-future", out)
        self.assertEqual(self.message_rows(), before)
        self.assertFalse(self.pending_path().exists())
        self.assertEqual(
            json.loads(self.done_path().read_text(encoding="utf-8")),
            {"boot_time": BOOT},
        )
        sent = len(self.owner.calls)
        code, out, err = self.invoke([
            "resume-after-reboot", "--boot-time", str(BOOT),
        ])
        self.assertEqual(code, 0, err)
        self.assertEqual(out.strip(), "already resumed for this boot")
        self.assertEqual(len(self.owner.calls), sent)

    def test_a_failed_unplanned_send_is_not_repeated(self):
        self.add_session("local-open", "Open")
        self.add_message("local-open", "assistant", BOOT - 20, TOOL)
        self.stored = [self.stored_row("local-open", "Open", last=1)]
        self.use_owner()
        self.owner.answer = {
            "ok": False,
            "error": "not-reachable",
            "exit": 5,
            "message": "down",
        }
        before = self.message_rows()
        code, out, err = self.invoke([
            "resume-after-reboot", "--boot-time", str(BOOT),
        ])
        self.assertEqual(code, 0, err)
        self.assertEqual(len(self.owner.calls), 1)
        self.assertEqual(self.owner.calls[0]["delivery"], "queue")
        self.assertIn("local-open", out)
        self.assertIn("Not delivered", out)
        self.assertEqual(self.message_rows(), before)
        self.assertEqual(
            json.loads(self.done_path().read_text(encoding="utf-8")),
            {"boot_time": BOOT},
        )
        self.owner.calls.clear()
        self.queue_ok()
        code, out, err = self.invoke([
            "resume-after-reboot", "--boot-time", str(BOOT),
        ])
        self.assertEqual(code, 0, err)
        self.assertEqual(self.owner.calls, [])
        self.assertEqual(out.strip(), "already resumed for this boot")
        self.assertEqual(self.message_rows(), before)

    def test_a_fresh_empty_file_does_not_take_the_unplanned_path(self):
        self.add_session("local-open", "Open")
        self.add_message("local-open", "user", BOOT - 20, None)
        self.stored = [self.stored_row("local-open", "Open", last=1)]
        self.write_pending([], written=time.time(), reason="kernel")
        self.use_owner()
        self.queue_ok()
        code, out, err = self.invoke([
            "resume-after-reboot", "--boot-time", str(BOOT),
        ])
        self.assertEqual(code, 0, err)
        self.assertEqual(self.owner.calls, [])
        self.assertNotIn("unexpectedly", out)
        self.assertEqual(out.strip(), "no sessions to resume")
        self.assertFalse(self.pending_path().exists())
        self.assertTrue(self.done_path().is_file())

    def test_resume_waits_for_the_owner_and_sends_nothing_until_then(self):
        self.add_session("local-target", "Reviewer")
        self.write_pending(
            [{"id": "local-target", "title": "Reviewer"}],
            written=time.time(),
            reason="kernel",
        )
        self.live_script = [
            {"ok": False, "error": "no-owner", "message": "no-owner"},
            {"ok": False, "error": "owner-restarting", "message": "owner-restarting"},
        ]
        self.use_owner()
        self.queue_ok()
        with mock.patch.object(self.plugin.cli.time, "sleep") as slept:
            code, _out, err = self.invoke([
                "resume-after-reboot", "--boot-time", str(BOOT),
            ])
        self.assertEqual(code, 0, err)
        self.assertEqual(slept.call_count, 2)
        self.assertEqual([call["target"] for call in self.owner.calls], ["local-target"])
        self.assertEqual(
            [call[0] for call in self.list_calls[:2]],
            ["live", "live"],
        )

    def test_no_owner_exits_5_and_changes_nothing(self):
        self.add_session("local-target", "Reviewer")
        self.add_message("local-target", "user", BOOT - 10, None)
        self.stored = [self.stored_row("local-target", "Reviewer", running=True, last=1)]
        self.owner_error = "no-owner"
        self.use_owner()
        before = self.snapshot()
        tries = self.plugin.cli._OWNER_WAIT_S // self.plugin.cli._OWNER_POLL_S + 1
        with mock.patch.object(self.plugin.cli.time, "sleep") as slept:
            code, out, err = self.invoke([
                "resume-after-reboot", "--boot-time", str(BOOT),
            ])
        self.assertEqual(code, 5, err)
        self.assertEqual(out, "")
        self.assertIn("no-owner", err)
        self.assertEqual(slept.call_count, tries - 1)
        self.assertEqual(len(self.list_calls), tries)
        self.assertEqual(self.owner.calls, [])
        self.assertEqual(self.snapshot(), before)
        self.assertFalse(self.pending_path().exists())
        self.assertFalse(self.done_path().exists())

    def test_a_non_waiting_owner_error_returns_immediately(self):
        self.owner_error = "not-reachable"
        self.use_owner()
        before = self.snapshot()
        with mock.patch.object(self.plugin.cli.time, "sleep",
                               side_effect=AssertionError("slept")):
            code, out, err = self.invoke([
                "resume-after-reboot", "--boot-time", str(BOOT),
            ])
        self.assertEqual(code, 5, err)
        self.assertEqual(out, "")
        self.assertIn("not-reachable", err)
        self.assertEqual(len(self.list_calls), 1)
        self.assertEqual(self.owner.calls, [])
        self.assertEqual(self.snapshot(), before)

    def test_resume_does_not_connect_without_a_discovered_endpoint(self):
        import hermes_cli.gateway_client as gateway_client

        self.add_session("local-open", "Open")
        self.add_message("local-open", "user", BOOT - 10, None)
        self.stored = [self.stored_row("local-open", "Open", last=1)]
        self.use_owner()
        self.queue_ok()
        with mock.patch.object(
            gateway_client, "connect_gateway",
            side_effect=AssertionError("connect_gateway"),
        ):
            code, _out, err = self.invoke([
                "resume-after-reboot", "--boot-time", str(BOOT),
            ])
        self.assertEqual(code, 0, err)
        self.assertEqual(len(self.owner.calls), 1)

    def test_missing_btime_exits_2_and_sends_nothing(self):
        cases = {
            "empty": self.home / "empty-stat",
            "missing": self.home / "missing-stat",
            "directory": self.home / "stat-dir",
            "no-btime": self.home / "cpu-only",
        }
        cases["empty"].write_text("", encoding="utf-8")
        cases["directory"].mkdir()
        cases["no-btime"].write_text("cpu 1 2 3\nintr 4\n", encoding="utf-8")
        for name, path in cases.items():
            with self.subTest(name=name):
                with mock.patch.object(self.plugin.cli, "_PROC_STAT", path), \
                     mock.patch.object(
                         self.plugin.client, "list_sessions",
                         side_effect=AssertionError("listed"),
                     ), \
                     mock.patch.object(
                         self.plugin.client, "deliver_sync",
                         side_effect=AssertionError("sent"),
                     ), \
                     mock.patch.object(
                         self.plugin.cli.time, "sleep",
                         side_effect=AssertionError("slept"),
                     ):
                    code, out, err = self.invoke(["resume-after-reboot"])
                self.assertEqual(code, 2, err)
                self.assertEqual(out, "")
                self.assertIn("btime", err)
                self.assertFalse(self.pending_path().exists())
                self.assertFalse(self.done_path().exists())

    def test_btime_is_read_from_proc_and_boot_time_overrides_it(self):
        proc = self.home / "proc-stat"
        proc.write_text("cpu 1\nbtime 1700000000\n", encoding="utf-8")
        self.use_owner()
        with mock.patch.object(self.plugin.cli, "_PROC_STAT", proc):
            code, out, err = self.invoke(["resume-after-reboot"])
        self.assertEqual(code, 0, err)
        self.assertEqual(out.strip(), "no sessions to resume")
        self.assertEqual(self.owner.calls, [])
        self.assertEqual(
            json.loads(self.done_path().read_text(encoding="utf-8")),
            {"boot_time": 1700000000},
        )
        # A second boot time in the same process test uses another home file.
        self.done_path().unlink()
        with mock.patch.object(self.plugin.cli, "_PROC_STAT", proc):
            code, _out, err = self.invoke([
                "resume-after-reboot", "--boot-time", "0",
            ])
        self.assertEqual(code, 0, err)
        self.assertEqual(
            json.loads(self.done_path().read_text(encoding="utf-8")),
            {"boot_time": 0},
        )

    def test_bad_max_age_is_a_usage_error(self):
        for text in ("week", "6", "6H", ""):
            with self.subTest(text=text):
                with mock.patch.object(
                    self.plugin.client, "list_sessions",
                    side_effect=AssertionError("listed"),
                ), mock.patch.object(
                    self.plugin.cli.time, "sleep",
                    side_effect=AssertionError("slept"),
                ):
                    code, out, err = self.invoke([
                        "resume-after-reboot", "--max-age", text, "--boot-time", "1",
                    ])
                self.assertEqual(code, 2, err)
                self.assertEqual(out, "")
                self.assertIn("invalid --max-age", err)
                self.assertFalse(self.done_path().exists())

    def test_max_age_units_choose_the_planned_record(self):
        self.add_session("local-target", "Reviewer")
        self.add_session("local-open", "Open")
        self.add_message("local-open", "user", BOOT - 20, None)
        self.stored = [
            self.stored_row("local-target", "Reviewer", last=2),
            self.stored_row("local-open", "Open", last=1),
        ]
        self.use_owner()
        self.queue_ok()
        # 5 days is older than the 6h default and younger than 7d.
        self.write_pending(
            [{"id": "local-target", "title": "Reviewer"}],
            written=time.time() - 5 * 86400,
            reason="kernel",
        )
        code, _out, err = self.invoke([
            "resume-after-reboot", "--max-age", "7d", "--boot-time", str(BOOT),
        ])
        self.assertEqual(code, 0, err)
        self.assertEqual([call["target"] for call in self.owner.calls], ["local-target"])
        self.assertIn(_planned(BOOT, "kernel"), self.owner.calls[0]["text"])
        self.done_path().unlink()
        self.owner.calls.clear()

        # 30 minutes is inside the 6h default and outside 15m.
        self.write_pending(
            [{"id": "local-target", "title": "Reviewer"}],
            written=time.time() - 30 * 60,
            reason="kernel",
        )
        code, _out, err = self.invoke([
            "resume-after-reboot", "--max-age", "15m", "--boot-time", str(BOOT),
        ])
        self.assertEqual(code, 0, err)
        self.assertEqual([call["target"] for call in self.owner.calls], ["local-open"])
        self.assertIn(_unplanned(BOOT), self.owner.calls[0]["text"])
        self.done_path().unlink()
        self.owner.calls.clear()

        # 45s is inside 6h and outside 30s, so the record is not used.
        self.write_pending(
            [{"id": "local-target", "title": "Reviewer"}],
            written=time.time() - 45,
            reason="kernel",
        )
        code, _out, err = self.invoke([
            "resume-after-reboot", "--max-age", "30s", "--boot-time", str(BOOT),
        ])
        self.assertEqual(code, 0, err)
        self.assertEqual([call["target"] for call in self.owner.calls], ["local-open"])
        self.assertIn(_unplanned(BOOT), self.owner.calls[0]["text"])

    def test_custom_state_file_round_trip(self):
        self.add_session("local-target", "Reviewer")
        self.stored = [self.stored_row("local-target", "Reviewer", running=True, last=1)]
        self.use_owner()
        path = self.home / "var" / "reboot-pending.json"
        code, _out, err = self.invoke([
            "reboot-notice", "--minutes", "4", "--reason", "kernel",
            "--state-file", str(path),
        ])
        self.assertEqual(code, 0, err)
        self.assertTrue(path.is_file())
        self.assertEqual(self.mode_of(path), 0o600)
        self.assertFalse(self.pending_path().exists())
        self.owner.calls.clear()
        self.queue_ok()
        code, _out, err = self.invoke([
            "resume-after-reboot", "--state-file", str(path),
            "--boot-time", str(BOOT),
        ])
        self.assertEqual(code, 0, err)
        self.assertFalse(path.exists())
        done = self.home / "var" / "reboot-pending.done.json"
        self.assertEqual(json.loads(done.read_text(encoding="utf-8")), {"boot_time": BOOT})
        self.assertEqual(self.owner.calls[0]["target"], "local-target")
        self.assertEqual(self.owner.calls[0]["delivery"], "queue")


if __name__ == "__main__":
    unittest.main()
