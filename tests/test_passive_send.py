"""Passive send path and record (G16a): usage errors, migration, keyed replacement, receipts."""

from __future__ import annotations

import io
import json
import os
import shutil
import sqlite3
import tempfile
import threading
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

import cli
import record
import replies
import route
from tests.test_cli import FakeCtx as PluginCtx
from tests.test_cli import _load_plugin

# The messages table as it was before G16 (no note_key, offered_turn, sender_profile).
OLD_SCHEMA = """\
CREATE TABLE messages (
  id TEXT PRIMARY KEY,
  kind TEXT NOT NULL,
  in_reply_to TEXT,
  reply_message_id TEXT,
  request_id TEXT,
  sender_session_id TEXT,
  sender_label TEXT NOT NULL,
  sender_source TEXT NOT NULL,
  target_session_id TEXT,
  target_title TEXT,
  address TEXT NOT NULL,
  delivery_path TEXT,
  admission_id TEXT,
  body TEXT NOT NULL,
  header_sha TEXT,
  sender_chat_scope TEXT,
  target_chat_scope TEXT,
  reply_mode TEXT NOT NULL CHECK (reply_mode IN ('none','requested','waiting','optional')),
  requested TEXT NOT NULL,
  applied TEXT,
  applied_detail TEXT,
  result TEXT,
  detail TEXT,
  reply TEXT,
  reply_state TEXT,
  created_at REAL NOT NULL,
  delivered_at REAL,
  replied_at REAL,
  echoed_at REAL
)
"""


class FakeCtx:
    def __init__(self, **settings):
        self.settings = settings

    def get_config(self, key, default=None):
        return self.settings.get(key, default)


class FakeOwner:
    def __init__(self):
        self.calls = []
        self.answer = {"ok": True, "applied": "queue", "applied_detail": None, "admission_id": "adm-1"}

    def __call__(self, target, text, delivery, submission_id):
        self.calls.append({"target": target, "delivery": delivery})
        return dict(self.answer)


class PassiveTestBase(unittest.TestCase):
    def setUp(self):
        self.home = Path(tempfile.mkdtemp(prefix="intercom-passive-"))
        self.addCleanup(shutil.rmtree, self.home, ignore_errors=True)
        env = mock.patch.dict(os.environ, {"HERMES_HOME": str(self.home)})
        env.start()
        self.addCleanup(env.stop)
        self.write_config(consent=True)
        self.ctx = FakeCtx(user_name="Sam")
        self.owner = FakeOwner()
        patch = mock.patch.object(route.client, "deliver_sync", self.owner)
        patch.start()
        self.addCleanup(patch.stop)
        from hermes_state import SessionDB

        self.db = SessionDB()
        self.addCleanup(self.db.close)
        self.started = 1_700_000_000
        self.add("local-sender", "Sender Chat", "cli")
        self.add("local-target", "Reviewer", "tui")
        self.add("local-other", "Other", "tui")

    def write_config(self, *, consent):
        lines = ["plugins:", "  enabled: [intercom]", "  entries:", "    intercom:",
                 f"      allow_gateway_injection: {consent}", "      settings: {}"]
        (self.home / "config.yaml").write_text("\n".join(lines) + "\n", encoding="utf-8")

    def add(self, session_id, title, source, *, parent=None):
        kwargs = {"parent_session_id": parent} if parent else {}
        self.db.create_session(session_id, source=source, **kwargs)
        self.db._write_sql("UPDATE sessions SET started_at = ? WHERE id = ?", (self.started, session_id))
        self.started += 1
        if title:
            self.db.set_session_title(session_id, title)

    def lister(self, scope="live", picker=False):
        return {"ok": True, "sessions": []}

    def send(self, to="local-target", message="build state: verifying", **kwargs):
        kwargs.setdefault("reply_mode", "none")
        kwargs.setdefault("delivery", "passive")
        kwargs.setdefault("ctx", self.ctx)
        kwargs.setdefault("lister", self.lister)
        sender = kwargs.pop("sender", "local-sender")
        return route.send(sender, to, message, **kwargs)

    def record_path(self):
        return self.home / "plugin-data" / "intercom" / "record.db"

    def rows(self):
        path = self.record_path()
        if not path.exists():
            return []
        conn = sqlite3.connect(path)
        conn.row_factory = sqlite3.Row
        try:
            return [dict(r) for r in conn.execute("SELECT * FROM messages ORDER BY created_at, id")]
        except sqlite3.OperationalError:
            return []
        finally:
            conn.close()

    def invoke(self, argv):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = cli.main(argv)
        return code, out.getvalue(), err.getvalue()


class UsageErrorTests(PassiveTestBase):
    """Refused before a message id exists: nothing recorded, nothing delivered, exit 2."""

    CASES = [
        ("key with steer", dict(delivery="steer", key="k"), "steer hands the message over at once"),
        ("key with interrupt", dict(delivery="interrupt", key="k"),
         "interrupt hands the message over at once"),
        ("all without passive", dict(to="all", delivery="queue", key="k"), "to: all needs delivery: passive"),
        ("all without a key", dict(to="all"), "to: all needs delivery: passive and a key"),
        ("all with a blank key", dict(to="all", key="   "), "and a key"),
        ("all with reply requested", dict(to="all", key="k", reply_mode="requested"),
         "to: all needs reply: none"),
        ("passive with waiting", dict(reply_mode="waiting"), "passive cannot use reply: waiting"),
        ("key with ask", dict(kind="ask", delivery="steer", key="k", reply_mode="waiting"),
         "ask cannot take a key"),
        ("passive with ask", dict(kind="ask", reply_mode="waiting"), "ask cannot be passive"),
    ]

    def test_each_usage_error_is_refused_with_its_reason_and_records_nothing(self):
        for name, kwargs, reason in self.CASES:
            with self.subTest(name):
                receipt = self.send(**kwargs)
                self.assertFalse(receipt["ok"])
                self.assertEqual(receipt["receipt"], "refused")
                self.assertEqual(receipt["error"], "usage")
                self.assertEqual(receipt["exit"], 2)
                self.assertIsNone(receipt["message_id"])
                self.assertTrue(receipt["message"].startswith("Nothing was sent:"))
                self.assertIn(reason, receipt["message"])
        self.assertEqual(self.rows(), [])
        self.assertEqual(self.owner.calls, [])

    def test_ask_with_passive_or_key_through_the_ask_action(self):
        for args, reason in (({"delivery": "passive"}, "ask cannot be passive"),
                             ({"key": "k"}, "ask cannot take a key")):
            with self.subTest(args=args):
                answer = replies.ask({"to": "local-target", "message": "Still ok?", **args},
                                     session_id="local-sender", ctx=self.ctx, lister=self.lister)
                self.assertEqual(answer["error"], "usage")
                self.assertIn(reason, answer["message"])
        self.assertEqual(self.rows(), [])

    def test_cli_usage_errors_exit_2(self):
        argvs = [
            ["--to", "local-target", "--delivery", "steer", "--key", "k"],
            ["--to", "local-target", "--delivery", "interrupt", "--key", "k"],
            ["--to", "all", "--delivery", "passive"],
            ["--to", "all", "--delivery", "queue", "--key", "k"],
            ["--to", "all", "--delivery", "steer"],
            ["--to", "all", "--delivery", "passive", "--key", "k", "--reply", "requested"],
            ["--to", "local-target", "--delivery", "passive", "--reply", "waiting"],
        ]
        for argv in argvs:
            with self.subTest(argv=argv):
                code, out, err = self.invoke(["send", *argv, "--message", "status"])
                self.assertEqual(code, 2)
                self.assertEqual(out, "")
                self.assertTrue(err.startswith("Nothing was sent:"), err)
        self.assertEqual(self.rows(), [])
        self.assertEqual(self.owner.calls, [])

    def test_key_with_queue_is_plain_queue_with_the_key_recorded(self):
        receipt = self.send(delivery="queue", key="build-state")
        self.assertTrue(receipt["ok"], receipt)
        self.assertEqual(len(self.owner.calls), 1)
        self.assertEqual(self.owner.calls[0]["delivery"], "queue")
        second = self.send(delivery="queue", key="build-state", message="build state: landed")
        self.assertTrue(second["ok"])
        rows = self.rows()
        self.assertEqual([r["note_key"] for r in rows], ["build-state", "build-state"])
        self.assertEqual([r["result"] for r in rows], ["held", "held"])
        self.assertEqual([r["requested"] for r in rows], ["queue", "queue"])

    def test_an_unknown_delivery_is_still_a_value_error(self):
        with self.assertRaises(ValueError):
            self.send(delivery="whisper")

    def test_a_session_titled_all_is_not_reached_by_title(self):
        self.add("local-all", "all", "tui")
        receipt = self.send(to="all", delivery="steer")
        self.assertEqual(receipt["error"], "usage")
        self.assertEqual(self.owner.calls, [])
        by_id = self.send(to="local-all", delivery="steer")
        self.assertTrue(by_id["ok"])
        self.assertEqual(by_id["target_session"], "local-all")


class ConsentTests(PassiveTestBase):
    def test_passive_is_refused_when_injection_is_off(self):
        self.write_config(consent=False)
        for to, key in (("local-target", None), ("all", "k")):
            with self.subTest(to=to):
                receipt = self.send(to=to, key=key)
                self.assertFalse(receipt["ok"])
                self.assertEqual(receipt["error"], "injection-off")
        self.assertEqual([r["result"] for r in self.rows()], ["error", "error"])
        self.assertNotIn("pending", [r["result"] for r in self.rows()])
        self.assertEqual(self.owner.calls, [])


class MigrationTests(PassiveTestBase):
    def make_old_record(self):
        directory = self.record_path().parent
        directory.mkdir(parents=True, exist_ok=True)
        os.chmod(directory, 0o700)
        conn = sqlite3.connect(self.record_path())
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute(OLD_SCHEMA)
            conn.execute(
                "INSERT INTO messages (id, kind, sender_label, sender_source, address, body, "
                "reply_mode, requested, result, created_at) "
                "VALUES ('0ld00001', 'send', 'cli', 'script', 'Reviewer', 'old body', 'none', "
                "'steer', 'delivered', 1.0)")
            conn.commit()
        finally:
            conn.close()
        os.chmod(self.record_path(), 0o600)

    def columns(self, conn):
        return [row[1] for row in conn.execute("PRAGMA table_info(messages)")]

    def test_an_old_record_gains_the_columns_and_note_seen_from_two_connections(self):
        self.make_old_record()
        first = record.connect()
        second = record.connect()
        try:
            for conn in (first, second):
                names = self.columns(conn)
                for column in ("note_key", "offered_turn", "sender_profile"):
                    self.assertEqual(names.count(column), 1)
                tables = {row[0] for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'")}
                self.assertIn("note_seen", tables)
            seen = [row[1] for row in first.execute("PRAGMA table_info(note_seen)")]
            self.assertEqual(seen, ["root_id", "note_key", "message_id", "turn_id", "delivered_at"])
            pk = [row[1] for row in first.execute("PRAGMA table_info(note_seen)") if row[5]]
            self.assertEqual(pk, ["root_id", "note_key"])
        finally:
            first.close()
            second.close()
        old = record.get("0ld00001")
        self.assertEqual(old["body"], "old body")
        self.assertIsNone(old["note_key"])
        self.assertIsNone(old["sender_profile"])
        receipt = self.send(key="k")
        self.assertTrue(receipt["ok"], receipt)
        self.assertEqual(record.get(receipt["message_id"])["note_key"], "k")

    def test_two_threads_migrating_at_once_both_succeed(self):
        self.make_old_record()
        errors = []
        barrier = threading.Barrier(2)
        real_migrate = record._migrate

        def migrate_together(conn):
            barrier.wait(5)
            real_migrate(conn)

        def open_once():
            try:
                record.connect().close()
            except Exception as exc:  # noqa: BLE001 - collected and asserted below
                errors.append(exc)

        # _open_lock serialises one process; a second process is a raw connection here.
        with mock.patch.object(record, "_migrate", migrate_together):
            raw = sqlite3.connect(self.record_path(), isolation_level=None, timeout=5)
            raw.row_factory = sqlite3.Row
            thread = threading.Thread(target=open_once)
            thread.start()
            try:
                migrate_together(raw)
            finally:
                raw.close()
            thread.join(10)
        self.assertEqual(errors, [])
        conn = record.connect()
        try:
            names = self.columns(conn)
        finally:
            conn.close()
        for column in ("note_key", "offered_turn", "sender_profile"):
            self.assertEqual(names.count(column), 1)

    def test_a_duplicate_column_counts_as_success(self):
        self.make_old_record()
        record.connect().close()
        conn = sqlite3.connect(self.record_path(), isolation_level=None)
        try:
            stale = {"id", "kind"}
            with mock.patch.object(record, "_columns", return_value=stale):
                record._migrate(conn)
        finally:
            conn.close()

    def test_offered_turn_is_an_update_field(self):
        receipt = self.send()
        self.assertTrue(record.update(receipt["message_id"], offered_turn="turn-1"))
        self.assertEqual(record.get(receipt["message_id"])["offered_turn"], "turn-1")
        with self.assertRaises(ValueError):
            record.update(receipt["message_id"], note_key="other")


class RecordTests(PassiveTestBase):
    def test_a_passive_note_is_one_pending_row_and_nothing_is_delivered(self):
        receipt = self.send(key="build-state")
        self.assertTrue(receipt["ok"], receipt)
        self.assertEqual(self.owner.calls, [])
        [row] = self.rows()
        self.assertEqual(row["id"], receipt["message_id"])
        self.assertEqual(row["requested"], "passive")
        self.assertEqual(row["result"], "pending")
        self.assertEqual(row["note_key"], "build-state")
        self.assertEqual(row["target_session_id"], "local-target")
        self.assertEqual(row["target_title"], "Reviewer")
        self.assertIsNone(row["delivered_at"])
        self.assertIsNone(row["header_sha"])
        self.assertIsNone(row["sender_profile"])

    def test_the_key_is_trimmed_and_case_sensitive(self):
        first = self.send(key="  Build ")
        second = self.send(key="build")
        third = self.send(key="Build")
        by_id = {r["id"]: r for r in self.rows()}
        self.assertEqual(by_id[first["message_id"]]["note_key"], "Build")
        self.assertEqual(by_id[first["message_id"]]["result"], "replaced")
        self.assertEqual(by_id[second["message_id"]]["result"], "pending")
        self.assertEqual(by_id[third["message_id"]]["result"], "pending")

    def test_all_is_one_row_with_no_target(self):
        receipt = self.send(to="all", key="subscription-usage")
        self.assertTrue(receipt["ok"], receipt)
        [row] = self.rows()
        self.assertEqual(row["address"], "all")
        self.assertIsNone(row["target_session_id"])
        self.assertEqual(row["result"], "pending")
        self.assertEqual(row["requested"], "passive")
        self.assertEqual(self.owner.calls, [])

    def test_plain_queue_still_stores_held(self):
        receipt = self.send(delivery="queue")
        self.assertTrue(receipt["ok"])
        self.assertEqual(self.rows()[0]["result"], "held")


class ReplacementTests(PassiveTestBase):
    def test_a_newer_keyed_note_replaces_the_held_one_and_takes_the_newest_place(self):
        first = self.send(key="build-state", message="verifying G3")
        unkeyed = self.send(message="one-time update")
        second = self.send(key="build-state", message="G3 landed")
        by_id = {r["id"]: r for r in self.rows()}
        self.assertEqual(by_id[first["message_id"]]["result"], "replaced")
        self.assertEqual(by_id[first["message_id"]]["detail"], f"replaced by {second['message_id']}")
        self.assertEqual(by_id[unkeyed["message_id"]]["result"], "pending")
        self.assertEqual(by_id[second["message_id"]]["result"], "pending")
        pending = [r["id"] for r in self.rows() if r["result"] == "pending"]
        self.assertEqual(pending, [unkeyed["message_id"], second["message_id"]])
        self.assertEqual(second["replaced"], [first["message_id"]])
        self.assertIn(first["message_id"], second["message"])

    def test_unkeyed_notes_never_replace(self):
        self.send()
        self.send()
        self.assertEqual([r["result"] for r in self.rows()], ["pending", "pending"])

    def test_different_modes_never_replace_each_other(self):
        passive = self.send(key="build-state")
        queued = self.send(delivery="queue", key="build-state")
        by_id = {r["id"]: r for r in self.rows()}
        self.assertEqual(by_id[passive["message_id"]]["result"], "pending")
        self.assertEqual(by_id[queued["message_id"]]["result"], "held")
        # A pending row of another mode with the key is left alone too.
        record.insert_note("q0000001", kind="send", sender_label="cli", sender_source="script",
                           address="Reviewer", body="queued", reply_mode="none", requested="queue",
                           target_session_id="local-target", note_key="build-state",
                           lineage=["local-target"])
        newer = self.send(key="build-state")
        by_id = {r["id"]: r for r in self.rows()}
        self.assertEqual(by_id["q0000001"]["result"], "pending")
        self.assertEqual(by_id[passive["message_id"]]["result"], "replaced")
        self.assertEqual(by_id[newer["message_id"]]["result"], "pending")

    def test_other_targets_and_all_are_separate(self):
        mine = self.send(key="k")
        other = self.send(to="local-other", key="k")
        everyone = self.send(to="all", key="k")
        newer = self.send(key="k")
        by_id = {r["id"]: r for r in self.rows()}
        self.assertEqual(by_id[mine["message_id"]]["result"], "replaced")
        self.assertEqual(by_id[other["message_id"]]["result"], "pending")
        self.assertEqual(by_id[everyone["message_id"]]["result"], "pending")
        self.assertEqual(by_id[newer["message_id"]]["result"], "pending")

    def test_a_newer_all_note_replaces_the_pending_all_row(self):
        first = self.send(to="all", key="subscription-usage", sender="local-target")
        second = self.send(to="all", key="subscription-usage")
        other_key = self.send(to="all", key="deployed")
        by_id = {r["id"]: r for r in self.rows()}
        self.assertEqual(by_id[first["message_id"]]["result"], "replaced")
        self.assertEqual(by_id[first["message_id"]]["detail"], f"replaced by {second['message_id']}")
        self.assertEqual(by_id[second["message_id"]]["result"], "pending")
        self.assertEqual(by_id[other_key["message_id"]]["result"], "pending")

    def test_replacement_follows_the_target_lineage_across_a_compaction(self):
        before = self.send(key="build-state", message="verifying G3")
        self.db.end_session("local-target", "compression")
        self.add("local-target-2", None, "tui", parent="local-target")
        after = self.send(key="build-state", message="G3 landed")
        self.assertEqual(after["target_session"], "local-target-2")
        by_id = {r["id"]: r for r in self.rows()}
        self.assertEqual(by_id[before["message_id"]]["target_session_id"], "local-target")
        self.assertEqual(by_id[before["message_id"]]["result"], "replaced")
        self.assertEqual(by_id[before["message_id"]]["detail"], f"replaced by {after['message_id']}")
        self.assertEqual(by_id[after["message_id"]]["result"], "pending")
        # Addressed by the old id, the next one still replaces the tip's note.
        third = self.send(to="local-target", key="build-state", message="G4 started")
        self.assertEqual(record.get(after["message_id"])["result"], "replaced")
        self.assertEqual(record.get(third["message_id"])["result"], "pending")

    def test_replacement_and_insert_are_one_transaction(self):
        first = self.send(key="k")
        real_insert = record._insert_row

        def failing_insert(conn, values, result):
            raise sqlite3.OperationalError("disk is full")

        with mock.patch.object(record, "_insert_row", failing_insert):
            with self.assertRaises(sqlite3.OperationalError):
                self.send(key="k")
        self.assertIs(record._insert_row, real_insert)
        self.assertEqual(record.get(first["message_id"])["result"], "pending")
        self.assertEqual(len(self.rows()), 1)


class ReceiptTests(PassiveTestBase):
    def test_the_receipt_is_held_with_the_message_id(self):
        receipt = self.send(key="build-state")
        self.assertTrue(receipt["ok"])
        self.assertEqual(receipt["receipt"], "held")
        self.assertEqual(receipt["requested"], "passive")
        self.assertEqual(receipt["key"], "build-state")
        self.assertRegex(receipt["message_id"], r"^[0-9a-f]{8}$")
        self.assertIn(f"Message {receipt['message_id']} is held", receipt["message"])
        self.assertNotIn("deliver", receipt["message"].lower())
        self.assertIn("does not wake", receipt["message"])

    def test_all_receipt(self):
        receipt = self.send(to="all", key="subscription-usage")
        self.assertEqual(receipt["receipt"], "held")
        self.assertEqual(receipt["target_title"], "all")
        self.assertIsNone(receipt["target_session"])
        self.assertIn("held for all sessions", receipt["message"])
        self.assertNotIn("deliver", receipt["message"].lower())

    def test_cli_send_passive_prints_held(self):
        code, out, err = self.invoke(["send", "--to", "local-target", "--delivery", "passive",
                                      "--key", "build-state", "--from", "build-watch",
                                      "--message", "verifying G3"])
        self.assertEqual(code, 0, err)
        [row] = self.rows()
        self.assertEqual(out.strip(), f'message {row["id"]} → "Reviewer": held (until its next turn; '
                                      "it does not wake it, key build-state)")
        self.assertEqual(row["sender_label"], "build-watch")
        self.assertEqual(row["sender_source"], "script")

    def test_cli_send_to_all_json(self):
        code, out, _err = self.invoke(["send", "--to", "all", "--delivery", "passive", "--key",
                                       "subscription-usage", "--reply", "none", "--from",
                                       "subscription-monitor", "--message", "Subs: Claude 18%",
                                       "--json"])
        self.assertEqual(code, 0)
        receipt = json.loads(out)
        self.assertEqual(receipt["receipt"], "held")
        self.assertEqual(record.get(receipt["message_id"])["address"], "all")


class StatusTests(PassiveTestBase):
    def status(self, message_id):
        code, out, err = self.invoke(["status", message_id])
        self.assertEqual(code, 0, err)
        lines = {line.split()[0]: line.split(None, 1)[1] for line in out.splitlines()[1:]}
        return lines

    def test_status_of_a_held_note(self):
        receipt = self.send(key="build-state")
        lines = self.status(receipt["message_id"])
        self.assertEqual(lines["delivery"], "held")
        self.assertEqual(lines["key"], "build-state")
        self.assertEqual(lines["to"], '"Reviewer" (local-target)')

    def test_status_of_a_delivered_note(self):
        receipt = self.send()
        record.update(receipt["message_id"], result="delivered", delivered_at=1_700_000_000.0,
                      offered_turn="turn-7")
        lines = self.status(receipt["message_id"])
        self.assertEqual(lines["delivery"], f"delivered {cli._stamp(1_700_000_000.0)} (turn turn-7)")

    def test_status_of_a_replaced_note(self):
        first = self.send(key="k")
        second = self.send(key="k")
        self.assertEqual(self.status(first["message_id"])["delivery"],
                         f"replaced by {second['message_id']}")
        self.assertEqual(self.status(second["message_id"])["delivery"], "held")
        record.update(first["message_id"], offered_turn="turn-3", delivered_at=1_700_000_000.0)
        self.assertEqual(self.status(first["message_id"])["delivery"],
                         f"replaced by {second['message_id']} (handed over in turn turn-3)")

    def test_status_of_an_all_note(self):
        first = self.send(to="all", key="subscription-usage")
        lines = self.status(first["message_id"])
        self.assertEqual(lines["to"], "all")
        self.assertEqual(lines["delivery"], "held for all · handed to 0 sessions")
        conn = record.connect()
        try:
            conn.executemany(
                "INSERT INTO note_seen (root_id, note_key, message_id, turn_id, delivered_at) "
                "VALUES (?, 'subscription-usage', ?, ?, ?)",
                [("root-a", first["message_id"], "t1", 1.0),
                 ("root-b", first["message_id"], "t2", 2.0),
                 ("root-c", first["message_id"], "t3", None)])
        finally:
            conn.close()
        self.assertEqual(self.status(first["message_id"])["delivery"],
                         "held for all · handed to 2 sessions")
        second = self.send(to="all", key="subscription-usage")
        self.assertEqual(self.status(first["message_id"])["delivery"],
                         f"held for all · handed to 2 sessions · replaced by {second['message_id']}")

    def test_status_json_carries_the_key(self):
        receipt = self.send(key="build-state")
        code, out, _err = self.invoke(["status", receipt["message_id"], "--json"])
        self.assertEqual(code, 0)
        data = json.loads(out)
        self.assertEqual(data["key"], "build-state")
        self.assertEqual(data["delivery"], "held")
        self.assertIsNone(data["check"])


class LogTests(PassiveTestBase):
    def test_log_lists_passive_rows_with_state_and_key(self):
        first = self.send(key="build-state")
        second = self.send(key="build-state")
        plain = self.send()
        everyone = self.send(to="all", key="subscription-usage")
        code, out, err = self.invoke(["log"])
        self.assertEqual(code, 0, err)
        by_id = {line.split()[2]: line for line in out.splitlines()}
        self.assertIn(f"passive: replaced by {second['message_id']} · key build-state",
                      by_id[first["message_id"]])
        self.assertIn("passive: held · key build-state", by_id[second["message_id"]])
        self.assertIn("passive: held  reply", by_id[plain["message_id"]])
        self.assertIn("→ all  passive: held for all · handed to 0 sessions · key subscription-usage",
                      by_id[everyone["message_id"]])

    def test_log_shows_the_key_of_a_keyed_queue(self):
        queued = self.send(delivery="queue", key="build-state")
        code, out, _err = self.invoke(["log", "--json"])
        self.assertEqual(code, 0)
        [item] = json.loads(out)
        self.assertEqual(item["id"], queued["message_id"])
        self.assertEqual(item["delivery"], "queued · key build-state")


class ToolTests(unittest.TestCase):
    """The tool schema and the legacy path, through the loaded plugin."""

    def setUp(self):
        self.home = Path(tempfile.mkdtemp(prefix="intercom-passive-tool-"))
        self.addCleanup(shutil.rmtree, self.home, ignore_errors=True)
        env = mock.patch.dict(os.environ, {"HERMES_HOME": str(self.home)})
        env.start()
        self.addCleanup(env.stop)
        self.plugin = _load_plugin(f"hermes_intercom_passive_{self._testMethodName}")
        self.ctx = PluginCtx(user_name="Sam")

    def register(self):
        with mock.patch.object(self.plugin.inbox, "start", side_effect=AssertionError("inbox.start")):
            self.plugin.register(self.ctx)

    def test_schema_offers_passive_and_key(self):
        self.register()
        schema = self.ctx.tools[-1]["schema"]
        props = schema["parameters"]["properties"]
        self.assertEqual(props["delivery"]["enum"], ["interrupt", "steer", "queue", "passive"])
        self.assertEqual(props["key"]["type"], "string")
        self.assertNotIn("key", schema["parameters"]["required"])
        self.assertIn(
            "passive: held until the target's next turn, never wakes it; "
            "key: a newer message with the same key replaces a held one",
            schema["description"],
        )

    def test_the_tool_passes_the_key_to_route(self):
        self.register()
        seen = {}

        def fake_send(sender, to, message, **kwargs):
            seen.update(kwargs, to=to)
            return {"ok": True}

        with mock.patch.object(self.plugin.route, "send", fake_send):
            self.plugin.handle({"action": "send", "to": "all", "message": "m", "reply": "none",
                                "delivery": "passive", "key": "subscription-usage"},
                               session_id="local-sender")
        self.assertEqual(seen["delivery"], "passive")
        self.assertEqual(seen["key"], "subscription-usage")
        self.assertEqual(seen["to"], "all")

    def test_passive_on_the_legacy_path_is_no_seam(self):
        self.register()
        self.plugin._runtime["legacy"] = True
        self.plugin._runtime["inbox_up"] = True  # even with the socket inbox running
        with mock.patch.object(self.plugin.inbox, "send_to",
                               side_effect=AssertionError("legacy send")):
            raw = json.loads(self.plugin.handle({"action": "send", "to": "peer", "message": "hi",
                                                 "delivery": "passive", "key": "k"}))
        self.assertFalse(raw["ok"])
        self.assertEqual(raw["error"], "no-seam")
        self.assertIn("passive needs the gateway", raw["message"])
        self.assertFalse((self.home / "plugin-data" / "intercom" / "record.db").exists())


if __name__ == "__main__":
    unittest.main()
