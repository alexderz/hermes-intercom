"""Record: schema, file modes, and the two writers on one file."""

from __future__ import annotations

import errno
import importlib.util
import os
import shutil
import sqlite3
import stat
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock


PACKAGE_DIR = Path(__file__).resolve().parents[1]
MODULE_NAME = "hermes_intercom_record_test"

COLUMNS = (
    "id", "kind", "in_reply_to", "reply_message_id", "request_id",
    "sender_session_id", "sender_label", "sender_source",
    "target_session_id", "target_title", "address",
    "delivery_path", "admission_id", "body", "header_sha",
    "sender_chat_scope", "target_chat_scope",
    "reply_mode", "requested", "applied", "applied_detail",
    "result", "detail", "reply", "reply_state",
    "created_at", "delivered_at", "replied_at", "echoed_at",
    "note_key", "offered_turn", "sender_profile",
)

REPLY_CHECK = "CHECK (reply_mode IN ('none','requested','waiting','optional'))"


def load_record():
    spec = importlib.util.spec_from_file_location(MODULE_NAME, PACKAGE_DIR / "record.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def mode_of(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


class RecordTestBase(unittest.TestCase):
    def setUp(self):
        self.home = Path(tempfile.mkdtemp(prefix="intercom-record-"))
        self.addCleanup(shutil.rmtree, self.home, ignore_errors=True)
        env = mock.patch.dict(os.environ, {"HERMES_HOME": str(self.home)})
        env.start()
        self.addCleanup(env.stop)
        # A loose umask: files we forget to tighten would show up 0644/0755.
        self._umask = os.umask(0o022)
        self.addCleanup(os.umask, self._umask)
        self.record = load_record()

    def directory(self) -> Path:
        return self.home / "plugin-data" / "intercom"

    def send(self, message_id="m1", **overrides):
        fields = dict(
            kind="send",
            sender_label="Orch",
            sender_source="local",
            address="Home",
            body="hello",
            reply_mode="requested",
            requested="steer",
            sender_session_id="local-sender",
            target_session_id="local-target",
            target_title="Home",
            sender_chat_scope=None,
            target_chat_scope="private",
            created_at=1.0,
        )
        fields.update(overrides)
        self.record.insert(message_id, **fields)


class SchemaTests(RecordTestBase):
    def test_schema_is_the_b7_messages_table(self):
        conn = self.record.connect()
        try:
            self.assertEqual(conn.execute("PRAGMA journal_mode").fetchone()[0], "wal")
            self.assertEqual(conn.execute("PRAGMA busy_timeout").fetchone()[0], 5000)
            info = conn.execute("PRAGMA table_info(messages)").fetchall()
            sql = conn.execute(
                "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'messages'"
            ).fetchone()[0]
            tables = [row[0] for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name")]
        finally:
            conn.close()

        names = tuple(row[1] for row in info)
        self.assertEqual(names, COLUMNS)
        self.assertNotIn("expect_reply", names)
        self.assertNotIn("expect_reply", sql)
        self.assertNotIn("retry_count", names)
        self.assertNotIn("status", names)
        self.assertEqual(tables, ["messages", "note_seen"])
        self.assertIn(REPLY_CHECK, sql)
        for column in ("delivery_path", "admission_id", "header_sha", "echoed_at", "reply_state"):
            self.assertIn(column, names)

        notnull = {row[1]: row[3] for row in info}
        for column in ("kind", "sender_label", "sender_source", "address", "body",
                       "reply_mode", "requested", "created_at"):
            self.assertEqual(notnull[column], 1, column)
        self.assertEqual(notnull["id"], 0)  # PRIMARY KEY, declared without NOT NULL
        self.assertEqual(info[0][5], 1)  # id is the primary key
        for column in ("reply_state", "header_sha", "delivery_path", "admission_id", "echoed_at"):
            self.assertEqual(notnull[column], 0, column)

        with self.assertRaises(sqlite3.OperationalError):
            stray = self.record.connect()
            try:
                stray.execute("SELECT expect_reply FROM messages")
            finally:
                stray.close()

    def test_reply_mode_rejects_anything_outside_the_four(self):
        for mode in ("none", "requested", "waiting", "optional"):
            self.send(f"ok-{mode}", reply_mode=mode)
            self.assertEqual(self.record.get(f"ok-{mode}")["reply_mode"], mode)
        for mode in ("expect_reply", "always", "yes", "NONE", ""):
            with self.assertRaises(sqlite3.IntegrityError):
                self.send(f"bad-{mode or 'empty'}", reply_mode=mode)

        conn = self.record.connect()
        try:
            with self.assertRaises(sqlite3.IntegrityError):
                conn.execute(
                    "INSERT INTO messages (id, kind, sender_label, sender_source, address, body,"
                    " reply_mode, requested, created_at) VALUES"
                    " ('raw', 'send', 'l', 'local', 'Home', 'body', 'bogus', 'steer', 1)"
                )
        finally:
            conn.close()

    def test_reply_state_is_null_or_the_four_outcomes(self):
        self.send("m1")
        self.assertIsNone(self.record.get("m1")["reply_state"])
        for state in ("sent", "silent", "stored", "no-block"):
            self.assertTrue(self.record.update("m1", reply_state=state))
            self.assertEqual(self.record.get("m1")["reply_state"], state)
        self.assertTrue(self.record.update("m1", reply_state=None))
        self.assertIsNone(self.record.get("m1")["reply_state"])
        for state in ("pending", "retry", "held", "delivered", ""):
            with self.assertRaises(ValueError):
                self.record.update("m1", reply_state=state)
        self.assertIsNone(self.record.get("m1")["reply_state"])

    def test_insert_leaves_the_result_and_hash_empty(self):
        self.send("m1", in_reply_to=None, request_id="req-1")
        row = self.record.get("m1")
        self.assertEqual(row["id"], "m1")
        self.assertEqual(row["body"], "hello")
        self.assertEqual(row["request_id"], "req-1")
        self.assertEqual(row["requested"], "steer")
        for column in ("result", "header_sha", "delivery_path", "admission_id", "applied",
                       "applied_detail", "detail", "reply", "reply_message_id", "delivered_at",
                       "replied_at", "echoed_at"):
            self.assertIsNone(row[column], column)

    def test_no_retry_count_status_ladder_or_prune(self):
        self.send("m1")
        for name in ("retry_count", "status", "attempts", "expect_reply", "echoed_at"):
            with self.assertRaises(ValueError):
                self.record.update("m1", **{name: 1})
        self.assertFalse(hasattr(self.record, "prune"))
        for i in range(20):
            self.send(f"p{i}", created_at=float(i))
        self.assertEqual(len(self.record.by_target_lineage(["local-target"])), 21)
        self.assertIsNotNone(self.record.get("p0"))
        self.assertIsNotNone(self.record.get("p19"))


class ModeTests(RecordTestBase):
    def test_modes_on_the_directory_and_all_three_files(self):
        # umask 0 would leave SQLite sidecars 0666 if the open did not tighten them.
        previous = os.umask(0)
        held = None
        try:
            held = self.record.connect()
            self.send("m1")
            directory = self.directory()
            self.assertEqual(mode_of(directory), 0o700)
            for name in ("record.db", "record.db-wal", "record.db-shm"):
                path = directory / name
                self.assertTrue(path.is_file(), name)
                self.assertEqual(mode_of(path), 0o600, name)
                self.assertFalse(stat.S_ISLNK(path.lstat().st_mode), name)
        finally:
            if held is not None:
                held.close()
            os.umask(previous)

    def test_record_db_is_created_exclusive_0600(self):
        calls = []
        real_open = os.open

        def wrapped(path, flags, mode=0o777, *args, **kwargs):
            calls.append((os.fspath(path), flags, mode))
            return real_open(path, flags, mode, *args, **kwargs)

        with mock.patch("os.open", side_effect=wrapped):
            self.send("m1")

        created = [
            (flags, mode) for path, flags, mode in calls
            if path.endswith(f"{os.sep}record.db") and flags & os.O_EXCL and flags & os.O_CREAT
        ]
        self.assertTrue(created)
        flags, mode = created[0]
        self.assertEqual(mode, 0o600)
        if hasattr(os, "O_NOFOLLOW"):
            self.assertTrue(flags & os.O_NOFOLLOW)
        # A second open finds the file; O_EXCL must not fail the writer.
        self.record.update("m1", result="delivered")
        self.assertEqual(self.record.get("m1")["result"], "delivered")

    def test_directory_is_tightened_on_every_open(self):
        directory = self.directory()
        directory.mkdir(parents=True)
        os.chmod(directory, 0o755)
        self.assertEqual(mode_of(directory), 0o755)
        conn = self.record.connect()
        try:
            self.assertEqual(mode_of(directory), 0o700)
        finally:
            conn.close()
        os.chmod(directory, 0o755)
        self.assertIsNone(self.record.get("missing"))
        self.assertEqual(mode_of(directory), 0o700)

    def test_sidecars_are_tightened_after_a_write(self):
        held = self.record.connect()
        try:
            self.send("m1")
            directory = self.directory()
            for name in ("record.db-wal", "record.db-shm"):
                os.chmod(directory / name, 0o644)
            os.chmod(directory, 0o755)
            self.assertTrue(self.record.update("m1", header_sha="abc", result="delivered"))
            self.assertEqual(mode_of(directory), 0o700)
            for name in ("record.db", "record.db-wal", "record.db-shm"):
                self.assertEqual(mode_of(directory / name), 0o600, name)
        finally:
            held.close()

    def test_umask_is_only_the_first_open_and_is_restored(self):
        seen = []
        real_umask = os.umask

        def wrapped(mask):
            seen.append(mask)
            return real_umask(mask)

        with mock.patch("os.umask", side_effect=wrapped):
            self.record.connect().close()
            first = list(seen)
            seen.clear()
            self.record.connect().close()
            second = list(seen)
        self.assertIn(0o077, first)
        self.assertNotIn(0o077, second)
        current = os.umask(0o022)
        self.assertEqual(current, 0o022)

    def test_concurrent_calls_leave_the_process_umask_unchanged(self):
        # The verifier's repro: several threads open the record together,
        # starting from umask 022. The process umask must still be 022.
        errors = []

        def reads(index):
            try:
                for n in range(40):
                    self.record.get(f"missing-{index}-{n}")
            except Exception as exc:  # reported from the main thread
                errors.append(exc)

        def writes():
            try:
                self.send("w1", created_at=1.0)
                self.record.update("w1", header_sha="h", result="delivered")
                self.record.claim_echo("w1", 3.0)
            except Exception as exc:  # reported from the main thread
                errors.append(exc)

        threads = [threading.Thread(target=reads, args=(i,)) for i in range(8)]
        threads.append(threading.Thread(target=writes))
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)
            self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(os.umask(0o022), 0o022)
        held = self.record.connect()
        try:
            directory = self.directory()
            self.assertEqual(mode_of(directory), 0o700)
            for name in ("record.db", "record.db-wal", "record.db-shm"):
                self.assertEqual(mode_of(directory / name), 0o600, name)
        finally:
            held.close()

    def test_a_symlink_is_not_the_record(self):
        directory = self.directory()
        directory.mkdir(parents=True)
        outside = self.home / "outside.db"
        outside.write_bytes(b"not the record")
        os.symlink(outside, directory / "record.db")
        with self.assertRaises(OSError) as caught:
            self.record.connect()
        self.assertEqual(caught.exception.errno, errno.ELOOP)
        self.assertEqual(outside.read_bytes(), b"not the record")
        self.assertTrue((directory / "record.db").is_symlink())


class WriterTests(RecordTestBase):
    def test_echo_claim_is_once(self):
        self.send("m1")
        self.assertTrue(self.record.claim_echo("m1", 5.0))
        self.assertFalse(self.record.claim_echo("m1", 9.0))
        self.assertEqual(self.record.get("m1")["echoed_at"], 5.0)
        self.assertFalse(self.record.claim_echo("missing", 1.0))
        self.assertIsNone(self.record.get("missing"))

        self.send("m2", created_at=2.0)
        start = threading.Barrier(2)
        results = []
        errors = []
        lock = threading.Lock()

        def claim():
            try:
                start.wait(timeout=5)
                won = self.record.claim_echo("m2", 7.0)
                with lock:
                    results.append(won)
            except Exception as exc:  # fail the test from the main thread
                errors.append(exc)

        threads = [threading.Thread(target=claim), threading.Thread(target=claim)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)
            self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(sorted(results), [False, True])
        self.assertEqual(self.record.get("m2")["echoed_at"], 7.0)

    def test_lookups_by_id_span_hash_and_target_lineage(self):
        self.send("old", target_session_id="old-id", created_at=1.0, body="first")
        self.send("tip", target_session_id="tip-id", created_at=2.0, body="second")
        self.send("other", target_session_id="elsewhere", created_at=3.0, body="third")
        self.record.update("old", header_sha="hash-old")
        self.record.update("tip", header_sha="hash-tip")

        self.assertEqual(self.record.get("tip")["body"], "second")
        self.assertIsNone(self.record.get("nope"))
        self.assertEqual(self.record.by_span_hash("hash-tip")["id"], "tip")
        self.assertEqual(self.record.by_span_hash("hash-old")["target_session_id"], "old-id")
        self.assertIsNone(self.record.by_span_hash("missing"))
        self.assertIsNone(self.record.by_span_hash(""))

        rows = self.record.by_target_lineage(["tip-id", "old-id"])
        self.assertEqual([row["id"] for row in rows], ["old", "tip"])
        self.assertEqual(self.record.by_target_lineage(["elsewhere"])[0]["id"], "other")
        self.assertEqual(self.record.by_target_lineage(["missing"]), [])
        self.assertEqual(self.record.by_target_lineage([]), [])
        self.assertFalse(self.record.update("missing", result="delivered"))

    def test_two_writers_share_one_file(self):
        self.send("m1", body="the question", created_at=1.0)
        start = threading.Barrier(2)
        errors = []

        def run(fn):
            try:
                start.wait(timeout=5)
                fn()
            except Exception as exc:  # fail the test from the main thread
                errors.append(exc)

        def sender():
            wrote = self.record.update(
                "m1",
                header_sha="span-hash",
                result="delivered",
                delivery_path="owner",
                admission_id="adm-1",
                applied="steer",
                applied_detail=None,
                delivered_at=10.0,
            )
            if not wrote:
                raise AssertionError("sender update missed the row")

        def reply_hook():
            wrote = self.record.update(
                "m1",
                reply="the answer",
                reply_state="sent",
                replied_at=11.0,
                reply_message_id="m2",
            )
            if not wrote:
                raise AssertionError("reply update missed the row")
            self.send(
                "m2",
                kind="send",
                in_reply_to="m1",
                reply_mode="optional",
                body="the answer",
                sender_session_id="local-target",
                sender_label="Home",
                target_session_id="local-sender",
                target_title="Orch",
                address="local-sender",
                requested="steer",
                created_at=12.0,
            )

        threads = [threading.Thread(target=run, args=(sender,)),
                   threading.Thread(target=run, args=(reply_hook,))]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)
            self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])

        database = self.directory() / "record.db"
        self.assertTrue(database.is_file())
        row = self.record.get("m1")
        self.assertEqual(row["header_sha"], "span-hash")
        self.assertEqual(row["result"], "delivered")
        self.assertEqual(row["delivery_path"], "owner")
        self.assertEqual(row["admission_id"], "adm-1")
        self.assertEqual(row["applied"], "steer")
        self.assertEqual(row["delivered_at"], 10.0)
        self.assertEqual(row["reply"], "the answer")
        self.assertEqual(row["reply_state"], "sent")
        self.assertEqual(row["replied_at"], 11.0)
        self.assertEqual(row["reply_message_id"], "m2")

        reply = self.record.get("m2")
        self.assertEqual(reply["in_reply_to"], "m1")
        self.assertEqual(reply["reply_mode"], "optional")
        self.assertEqual(reply["kind"], "send")
        self.assertEqual(reply["body"], "the answer")
        self.assertEqual(reply["target_session_id"], "local-sender")
        self.assertIsNone(reply["result"])
        self.assertIsNone(reply["header_sha"])
        self.assertEqual(self.record.by_span_hash("span-hash")["id"], "m1")
        self.assertEqual(
            [item["id"] for item in self.record.by_target_lineage(["local-sender"])],
            ["m2"],
        )
