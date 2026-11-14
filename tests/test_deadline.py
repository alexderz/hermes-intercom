"""Deadline on a passive note (G16g, passive.md P7): ``within``, the sweep and the wake turn.

A temp root home (the ``default`` profile) and ``<root>/profiles/spike``, each with its own
config, ``state.db`` and record. ``client.deliver_sync`` and ``client._discover`` are fakes; the
fake delivery notes the Hermes home active when it is called.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import shutil
import sqlite3
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import cli
import deadline
import echo
import frame
import passive
import record
import replies
import route

OWNER = "123456789"
MARK = "\U0001f4e1"  # 📡
NAMED = "spike"
PROMPT = "Sam's task"
WITHIN = 10


def _home():
    from hermes_constants import get_hermes_home

    return get_hermes_home()


class FakeCtx:
    def __init__(self, **settings):
        self.settings = settings
        self.hooks = []
        self.posts = []
        self.lock = threading.Lock()

    def get_config(self, key, default=None):
        return self.settings.get(key, default)

    def register_hook(self, name, fn):
        self.hooks.append((name, fn))

    def post_message(self, text):
        with self.lock:
            self.posts.append(text)
        return SimpleNamespace(ok=True, error=None)


class FakeOwner:
    """``client.deliver_sync`` and ``client._discover``: one owner instance per profile home."""

    def __init__(self):
        self.calls = []
        self.instance = "inst-1"
        self.present = True
        self.failure = None
        self.block = None
        self.lock = threading.Lock()

    def __call__(self, target, text, delivery, submission_id):
        with self.lock:
            self.calls.append({"target": target, "text": text, "delivery": delivery,
                               "submission_id": submission_id, "home": _home()})
        if self.block is not None:
            self.block.wait(30)
        if self.failure is not None:
            return {"ok": False, "error": self.failure, "code": None, "exit": 5, "message": "down"}
        applied = "start" if delivery == "queue" else delivery
        return {"ok": True, "applied": applied, "applied_detail": None, "admission_id": "adm-1",
                "instance_id": self.instance}

    def discover(self):
        if not self.present:
            return None, {"ok": False, "error": "no-owner"}
        return SimpleNamespace(instance_id=self.instance), None

    def wakes(self, target="local-target"):
        return [c for c in self.calls if c["target"] == target and c["delivery"] == "queue"]

    def replies(self, target="local-sender"):
        return [c for c in self.calls if c["target"] == target and c["delivery"] == "steer"]


class DeadlineTestBase(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="intercom-deadline-"))
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.named = self.root / "profiles" / NAMED
        self.named.mkdir(parents=True)
        env = mock.patch.dict(os.environ, {"HERMES_HOME": str(self.root)})
        env.start()
        self.addCleanup(env.stop)
        self.write_config(self.root)
        self.write_config(self.named)
        self.ctx = FakeCtx(user_name="Sam", owner_user_ids=[f"telegram:{OWNER}"])
        self.owner = FakeOwner()
        for name, value in (("deliver_sync", self.owner), ("_discover", self.owner.discover),
                            ("owner_instance", lambda: self.owner.instance)):
            patch = mock.patch.object(route.client, name, value)
            patch.start()
            self.addCleanup(patch.stop)
        for module in (passive, echo, replies):
            ctx_patch = mock.patch.dict(module._ctx, {"ctx": self.ctx})
            ctx_patch.start()
            self.addCleanup(ctx_patch.stop)
        for state in (passive._offered, passive._instance):
            patch = mock.patch.dict(state, {}, clear=True)
            patch.start()
            self.addCleanup(patch.stop)
        passive._instance["id"] = None

        from hermes_state import SessionDB

        self.dbs = {}
        for home in (self.root, self.named):
            db = SessionDB(db_path=home / "state.db")
            self.addCleanup(db.close)
            self.dbs[home] = db
        self.db = self.dbs[self.root]
        self.started = 1.0
        self.turns = 0
        self.add("local-sender", "Sender Chat", "cli")
        self.add("local-target", "Reviewer", "tui")
        self.add("s-target", "Spike Chat", "tui", home=self.named)

    def write_config(self, home, *, consent=True, enabled=("intercom",)):
        config = {"plugins": {"enabled": list(enabled), "disabled": [],
                              "entries": {"intercom": {"allow_gateway_injection": consent,
                                                       "settings": {}}}}}
        (home / "config.yaml").write_text(json.dumps(config) + "\n", encoding="utf-8")

    def add(self, session_id, title, source, *, parent=None, origin=None, home=None):
        db = self.dbs[home or self.root]
        kwargs = {"parent_session_id": parent} if parent else {}
        db.create_session(session_id, source=source, **kwargs)
        db._write_sql("UPDATE sessions SET started_at = ? WHERE id = ?", (self.started, session_id))
        self.started += 1
        if title:
            db.set_session_title(session_id, title)
        if origin is not None:
            db._write_sql("UPDATE sessions SET origin_json = ? WHERE id = ?",
                          (json.dumps(origin), session_id))

    def compact(self, session_id, tip):
        self.db.end_session(session_id, "compression")
        self.add(tip, None, "tui", parent=session_id)

    @staticmethod
    def lister(scope="live", picker=False):
        return {"ok": True, "sessions": []}

    def send(self, message="build state: verifying", *, to="local-target", within=WITHIN,
             sender="status-script", script=True, delivery="passive", **kwargs):
        kwargs.setdefault("reply_mode", "none")
        return route.send(sender, to, message, ctx=self.ctx, lister=self.lister, script=script,
                          delivery=delivery, within=within, **kwargs)

    def held(self, *args, **kwargs):
        receipt = self.send(*args, **kwargs)
        self.assertTrue(receipt["ok"], receipt)
        self.assertEqual(receipt["receipt"], "held")
        return receipt

    def after(self, receipt, minutes=WITHIN, slack=1.0):
        """A clock *minutes* after *receipt*'s row was created, plus *slack* seconds."""
        return record.get(receipt["message_id"])["created_at"] + minutes * 60 + slack

    def sweep(self, receipt, minutes=WITHIN, slack=1.0):
        self.assertTrue(deadline.sweep(now=self.after(receipt, minutes, slack)))

    def restart(self, instance="inst-2"):
        """A new owner instance: Hermes's in-memory queue is gone, the record is not."""
        self.owner.instance = instance
        passive._instance["id"] = None
        passive._offered.clear()

    def record_path(self, home=None):
        return (home or self.root) / "plugin-data" / "intercom" / "record.db"

    def rows(self, home=None):
        path = self.record_path(home)
        if not path.exists():
            return []
        conn = sqlite3.connect(path)
        try:
            return conn.execute("SELECT id FROM messages").fetchall()
        finally:
            conn.close()

    def turn(self, prompt=PROMPT, session_id="local-target", *, request=True, answer=None,
             platform="tui"):
        """One turn: ``pre_llm_call`` (held notes, then the echo), its first model request
        (``request``) and, with ``answer``, ``post_llm_call``. Returns the held-note context."""
        self.turns += 1
        turn_id = f"turn-{self.turns}"
        result = passive.on_pre_llm_call(session_id=session_id, turn_id=turn_id, user_message=prompt,
                                         conversation_history=[], platform=platform, model="m")
        context = None if result is None else result["context"]
        echo.on_pre_llm_call(session_id=session_id, user_message=prompt, platform=platform)
        user_message = prompt if context is None else f"{prompt}\n\n{context}"
        if request:
            for _ in range(2):  # a second request in the same turn changes nothing
                rows = [{"role": "user", "content": user_message}]
                passive.on_pre_api_request(session_id=session_id, turn_id=turn_id, platform=platform,
                                           request_messages=rows)
                echo.on_pre_api_request(session_id=session_id, turn_id=turn_id, platform=platform,
                                        request_messages=rows)
        if answer is not None:
            replies.on_post_llm_call(session_id=session_id, turn_id=turn_id, user_message=user_message,
                                     assistant_response=answer,
                                     conversation_history=[{"role": "user", "content": user_message},
                                                           {"role": "assistant", "content": answer}],
                                     platform=platform, model="m")
        for thread in threading.enumerate():
            if thread.name.startswith(("intercom-reply-", "intercom-echo-")):
                thread.join(10)
        self.last_turn = turn_id
        return context

    def wake_turn(self, number=0, target="local-target", **kwargs):
        """The turn Hermes runs for the *number*-th queued wake to *target*."""
        return self.turn(self.owner.wakes(target)[number]["text"], **kwargs)


class UsageTests(DeadlineTestBase):
    def assert_usage(self, receipt, message):
        self.assertEqual((receipt["ok"], receipt["error"], receipt["exit"]), (False, "usage", 2))
        self.assertIsNone(receipt["message_id"])
        self.assertEqual(receipt["message"], message)
        self.assertEqual(self.rows(), [])
        self.assertEqual(self.owner.calls, [])

    def test_within_with_to_all_is_refused(self):
        self.assert_usage(self.send(to="all", key="k"), route.WITHIN_WITH_ALL)
        self.assertIn("would wake every session", route.WITHIN_WITH_ALL)

    def test_within_with_a_live_delivery_is_refused(self):
        for delivery in ("steer", "queue", "interrupt"):
            with self.subTest(delivery=delivery):
                self.assert_usage(self.send(delivery=delivery),
                                  route.WITHIN_WITH_LIVE.format(delivery=delivery))
        self.assertIn("hands the message over at once", route.WITHIN_WITH_LIVE)

    def test_within_under_five_or_not_whole_is_refused(self):
        for within in (4, 0, -5, 7.5, "abc", "", True, "4"):
            with self.subTest(within=within):
                self.assert_usage(self.send(within=within), route.WITHIN_INVALID.format(minimum=5))

    def test_the_cli_refuses_with_exit_2_and_the_reason(self):
        for argv, reason in (
            (["--to", "all", "--key", "k", "--delivery", "passive", "--within", "10"],
             route.WITHIN_WITH_ALL),
            (["--to", "local-target", "--within", "10"], route.WITHIN_WITH_LIVE.format(delivery="steer")),
            (["--to", "local-target", "--delivery", "passive", "--within", "3"],
             route.WITHIN_INVALID.format(minimum=5)),
            (["--to", "local-target", "--delivery", "passive", "--within", "ten"],
             route.WITHIN_INVALID.format(minimum=5)),
        ):
            with self.subTest(argv=argv):
                err = io.StringIO()
                with contextlib.redirect_stderr(err), contextlib.redirect_stdout(io.StringIO()):
                    code = cli.main(["send", "--message", "x", *argv])
                self.assertEqual(code, 2)
                self.assertIn(reason, err.getvalue())
        self.assertEqual(self.rows(), [])

    def test_whole_minutes_are_accepted_as_a_number_or_digits(self):
        for within in (5, "15", 20.0):
            with self.subTest(within=within):
                receipt = self.held(within=within)
                row = record.get(receipt["message_id"])
                self.assertEqual(row["wake_at"], row["created_at"] + int(within) * 60)


class ReceiptTests(DeadlineTestBase):
    def test_receipt_says_when_it_wakes(self):
        receipt = self.held()
        row = record.get(receipt["message_id"])
        self.assertEqual(row["wake_at"], row["created_at"] + WITHIN * 60)
        hhmm = time.strftime("%H:%M", time.localtime(row["wake_at"]))
        self.assertEqual(receipt["wake_at"], row["wake_at"])
        self.assertEqual(cli._applied_phrase(receipt), f"held (wakes by {hhmm} if not run)")
        self.assertIn(f"If that has not run by {hhmm}, it is woken then.", receipt["message"])
        keyed = self.held(key="k")
        self.assertTrue(cli._applied_phrase(keyed).endswith("if not run, key k)"))

    def test_the_cli_prints_the_receipt(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = cli.main(["send", "--to", "local-target", "--message", "x", "--delivery", "passive",
                             "--within", "10"])
        self.assertEqual(code, 0)
        self.assertRegex(out.getvalue(), r"held \(wakes by \d\d:\d\d if not run\)")

    def test_a_note_without_within_is_unchanged(self):
        receipt = self.held(within=None)
        self.assertIsNone(record.get(receipt["message_id"])["wake_at"])
        self.assertEqual(cli._applied_phrase(receipt), "held (until its next turn; it does not wake it)")
        self.assertEqual(cli._delivery_words(record.get(receipt["message_id"])), "held")

    def test_status_shows_wakes_by_then_woken(self):
        receipt = self.held()
        row = record.get(receipt["message_id"])
        hhmm = time.strftime("%H:%M", time.localtime(row["wake_at"]))
        self.assertEqual(cli._delivery_words(row), f"held · wakes by {hhmm}")
        self.sweep(receipt)
        self.assertEqual(cli._delivery_words(record.get(receipt["message_id"])), f"held · wakes by {hhmm}")
        self.wake_turn(request=False)
        row = record.get(receipt["message_id"])
        self.assertEqual(cli._delivery_words(row),
                         f"held · woken {cli._stamp(row['woken_at'])} (turn {self.last_turn})")
        self.turn()
        words = cli._delivery_words(record.get(receipt["message_id"]))
        self.assertTrue(words.startswith("delivered "), words)
        self.assertIn(" · woken ", words)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(cli.main(["status", receipt["message_id"]]), 0)
        self.assertIn("woken", out.getvalue())

    def test_a_note_delivered_before_its_deadline_shows_no_wake(self):
        receipt = self.held()
        self.turn()
        words = cli._delivery_words(record.get(receipt["message_id"]))
        self.assertTrue(words.startswith("delivered "), words)
        self.assertNotIn("wake", words)


class SweepTests(DeadlineTestBase):
    def test_not_yet_due_makes_no_wake(self):
        receipt = self.held()
        self.sweep(receipt, slack=-1.0)
        self.assertEqual(self.owner.calls, [])
        self.assertIsNone(record.get(receipt["message_id"])["wake_admission"])

    def test_due_makes_exactly_one_wake_through_the_queue(self):
        receipt = self.held()
        self.sweep(receipt)
        self.sweep(receipt, slack=120)
        wakes = self.owner.wakes()
        self.assertEqual(len(wakes), 1)
        self.assertEqual(len(self.owner.calls), 1)
        wake = wakes[0]
        self.assertEqual(wake["delivery"], "queue")
        self.assertIn("a held note from `status-script` is due; it follows.", wake["text"])
        self.assertNotIn("build state", wake["text"])
        self.assertIn("no reply needed", wake["text"].splitlines()[0])
        row = record.get(receipt["message_id"])
        self.assertEqual(row["header_sha"], route.header_sha(wake["text"], receipt["message_id"]))
        self.assertEqual((row["wake_admission"], row["wake_instance"], row["admission_id"]),
                         (wake["submission_id"], "inst-1", "adm-1"))
        self.assertIsNotNone(row["wake_claimed_at"])
        self.assertEqual(row["result"], "pending")

    def test_reply_mode_none_on_the_wake_line(self):
        receipt = self.held(sender="local-sender", script=False, reply_mode="requested")
        with mock.patch.object(deadline.route.frame, "build", wraps=frame.build) as build:
            self.sweep(receipt)
        self.assertEqual(build.call_args.kwargs["reply_mode"], "none")
        self.assertIn("a held note from `Sender Chat` is due", self.owner.wakes()[0]["text"])

    def test_delivered_before_the_deadline_makes_no_wake(self):
        receipt = self.held()
        self.assertIn("build state", self.turn())
        self.sweep(receipt)
        self.assertEqual(self.owner.calls, [])

    def test_two_overlapping_sweeps_make_one_wake(self):
        receipt = self.held()
        now = self.after(receipt)
        holder = sqlite3.connect(self.record_path(), isolation_level=None, timeout=30)
        holder.execute("BEGIN IMMEDIATE")  # both sweeps now wait on the record to claim
        threads = [threading.Thread(target=deadline.sweep, kwargs={"now": now}) for _ in range(2)]
        for thread in threads:
            thread.start()
        time.sleep(0.3)
        holder.execute("COMMIT")
        holder.close()
        for thread in threads:
            thread.join(30)
        self.assertEqual(len(self.owner.wakes()), 1)

    def test_a_consumed_wake_is_not_sent_again(self):
        receipt = self.held()
        self.sweep(receipt)
        self.wake_turn(request=False)  # vetoed: no model request, nothing delivered
        row = record.get(receipt["message_id"])
        self.assertEqual((row["wake_turn"], row["result"]), (self.last_turn, "pending"))
        self.sweep(receipt, slack=60)
        self.restart()
        self.sweep(receipt, slack=120)
        self.assertEqual(len(self.owner.wakes()), 1)
        self.assertIn("build state", self.turn())  # an ordinary held note again

    def test_a_marker_of_another_owner_instance_never_consumed_fires_once_more(self):
        receipt = self.held()
        self.sweep(receipt)
        self.restart()  # the queued wake went with the old owner
        self.sweep(receipt, slack=60)
        self.sweep(receipt, slack=120)
        wakes = self.owner.wakes()
        self.assertEqual(len(wakes), 2)
        self.assertNotEqual(wakes[0]["submission_id"], wakes[1]["submission_id"])
        self.assertEqual(record.get(receipt["message_id"])["wake_instance"], "inst-2")
        self.assertIn("build state", self.wake_turn(1))
        self.assertEqual(record.get(receipt["message_id"])["wake_turn"], self.last_turn)
        self.sweep(receipt, slack=180)
        self.assertEqual(len(self.owner.wakes()), 2)

    def test_injection_off_at_fire_time_makes_no_wake_and_leaves_it_held(self):
        receipt = self.held()
        self.write_config(self.root, consent=False)
        self.sweep(receipt)
        self.assertEqual(self.owner.calls, [])
        row = record.get(receipt["message_id"])
        self.assertEqual((row["result"], row["wake_instance"], row["wake_turn"]), ("pending", None, None))
        self.write_config(self.root, consent=True)
        self.sweep(receipt, slack=60)
        self.assertEqual(len(self.owner.wakes()), 1)

    def test_a_failed_delivery_releases_the_claim_and_logs_one_line(self):
        receipt = self.held()
        self.owner.failure = "no-owner"
        with self.assertLogs(deadline.logger, "WARNING") as logs:
            self.sweep(receipt)
        self.assertEqual(len(logs.output), 1)
        self.assertIn(receipt["message_id"], logs.output[0])
        self.assertNotIn("build state", logs.output[0])
        self.assertIsNone(record.get(receipt["message_id"])["wake_instance"])
        self.owner.failure = None
        self.sweep(receipt, slack=60)
        self.assertEqual(len(self.owner.wakes()), 2)

    def test_the_sweep_never_raises(self):
        receipt = self.held()
        with mock.patch.object(route.client, "deliver_sync", side_effect=RuntimeError("boom")), \
             self.assertLogs(deadline.logger, "WARNING"):
            self.sweep(receipt)
        self.assertIsNone(record.get(receipt["message_id"])["wake_instance"])
        with mock.patch.object(deadline, "_profiles", side_effect=OSError("gone")), \
             self.assertLogs(deadline.logger, "WARNING"):
            self.assertFalse(deadline.sweep())

    def test_a_target_compacted_after_send_is_woken_at_its_new_tip(self):
        receipt = self.held()
        self.compact("local-target", "local-target-2")
        self.sweep(receipt)
        self.assertEqual(self.owner.wakes(), [])
        wakes = self.owner.wakes("local-target-2")
        self.assertEqual(len(wakes), 1)
        context = self.turn(wakes[0]["text"], "local-target-2")
        self.assertIn("build state", context)
        self.assertEqual(record.get(receipt["message_id"])["result"], "delivered")

    def test_no_owner_skips_the_profile(self):
        receipt = self.held()
        self.owner.present = False
        self.sweep(receipt)
        self.assertEqual(self.owner.calls, [])
        self.assertIsNone(record.get(receipt["message_id"])["wake_admission"])


class ReplacementTests(DeadlineTestBase):
    def test_a_keyed_replacement_takes_the_new_rows_within(self):
        first = self.held("v1", key="k", within=10)
        second = self.held("v2", key="k", within=30)
        self.assertEqual(second["replaced"], [first["message_id"]])
        self.sweep(first, minutes=11)
        self.assertEqual(self.owner.calls, [])
        self.sweep(second, minutes=30)
        self.assertEqual(len(self.owner.wakes()), 1)
        self.assertIn("v2", self.wake_turn())
        self.assertIsNone(record.get(first["message_id"])["wake_admission"])

    def test_a_replacement_without_within_leaves_nothing_due(self):
        first = self.held("v1", key="k", within=10)
        self.held("v2", key="k", within=None)
        self.sweep(first, minutes=60 * 24)
        self.assertEqual(self.owner.calls, [])


class ProfileTests(DeadlineTestBase):
    def test_within_and_profile_land_in_the_targets_record(self):
        receipt = self.held(to="s-target", sender="local-sender", script=False, profile=NAMED)
        self.assertEqual(self.rows(), [])
        from profiles import in_profile

        with in_profile(NAMED):
            row = record.get(receipt["message_id"])
        self.assertEqual(row["wake_at"], row["created_at"] + WITHIN * 60)
        self.assertEqual(row["sender_profile"], "default")

    def test_a_sweep_started_in_the_root_wakes_a_note_in_a_named_profile(self):
        receipt = self.held(to="s-target", sender="local-sender", script=False, profile=NAMED)
        from profiles import in_profile

        with in_profile(NAMED):
            now = self.after(receipt)
        self.assertTrue(deadline.sweep(now=now))
        wakes = self.owner.wakes("s-target")
        self.assertEqual(len(wakes), 1)
        self.assertEqual(wakes[0]["home"], self.named)
        self.assertIn("a held note from `default/Sender Chat` is due", wakes[0]["text"])

    def test_a_profile_with_intercom_disabled_is_skipped(self):
        receipt = self.held(to="s-target", sender="local-sender", script=False, profile=NAMED)
        from profiles import in_profile

        with in_profile(NAMED):
            now = self.after(receipt)
        self.write_config(self.named, enabled=())
        self.assertTrue(deadline.sweep(now=now))
        self.assertEqual(self.owner.calls, [])
        with in_profile(NAMED):
            self.assertIsNone(record.get(receipt["message_id"])["wake_admission"])

    def test_the_profiles_visited(self):
        (self.root / "profiles" / "Bad Name").mkdir()
        (self.root / "profiles" / "shell").mkdir()  # no identity marker
        self.assertEqual(deadline._profiles(), ["default", NAMED])


class TickTests(DeadlineTestBase):
    def deadline_threads(self):
        return [t for t in threading.enumerate() if t.name == "intercom-deadline"]

    def test_register_hooks_the_dispatcher_tick(self):
        ctx = FakeCtx()
        deadline.register(ctx)
        self.assertEqual(ctx.hooks, [("on_kanban_dispatch_tick", deadline.on_dispatch_tick)])

    def test_a_dry_run_tick_does_nothing(self):
        with mock.patch.object(deadline, "sweep") as sweep:
            self.assertIsNone(deadline.on_dispatch_tick(board="default", profile_name="default",
                                                        dry_run=True, outcome="idle", result=None))
        self.assertEqual(self.deadline_threads(), [])
        sweep.assert_not_called()

    def test_the_tick_returns_at_once_and_a_running_sweep_blocks_a_second(self):
        receipt = self.held()
        self.set_wake_at_past(receipt)
        self.owner.block = threading.Event()
        try:
            started = time.monotonic()
            deadline.on_dispatch_tick(board="default", profile_name="default", dry_run=False,
                                      outcome="idle", result=None)
            self.assertLess(time.monotonic() - started, 1.0)
            for _ in range(200):
                if self.owner.calls:
                    break
                time.sleep(0.05)
            self.assertEqual(len(self.owner.calls), 1)  # the sweep is inside deliver_sync
            with mock.patch.object(deadline, "sweep") as second:
                deadline.on_dispatch_tick(dry_run=False)
            second.assert_not_called()
            self.assertEqual(len(self.deadline_threads()), 1)
        finally:
            self.owner.block.set()
            for thread in self.deadline_threads():
                thread.join(30)
        self.assertFalse(deadline._running.locked())
        self.assertEqual(len(self.owner.wakes()), 1)

    def set_wake_at_past(self, receipt):
        conn = sqlite3.connect(self.record_path())
        try:
            conn.execute("UPDATE messages SET wake_at = ? WHERE id = ?",
                         (time.time() - 1, receipt["message_id"]))
            conn.commit()
        finally:
            conn.close()

    def test_wake_due_with_no_owner_exits_0_and_prints_nothing(self):
        receipt = self.held()
        self.set_wake_at_past(receipt)
        self.owner.present = False
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            self.assertEqual(cli.main(["wake-due"]), 0)
        self.assertEqual((out.getvalue(), err.getvalue()), ("", ""))
        self.assertEqual(self.owner.calls, [])

    def test_wake_due_fires_a_due_note_silently(self):
        receipt = self.held()
        self.set_wake_at_past(receipt)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(cli.main(["wake-due"]), 0)
        self.assertEqual(out.getvalue(), "")
        self.assertEqual(len(self.owner.wakes()), 1)


class WakeTurnTests(DeadlineTestBase):
    def test_the_wake_turn_hands_over_the_note_and_other_held_notes(self):
        other = self.held("unrelated note", within=None)
        receipt = self.held("build state: G3 landed")
        self.sweep(receipt)
        context = self.wake_turn()
        self.assertIn("build state: G3 landed", context)
        self.assertIn("unrelated note", context)
        self.assertIn(f"{MARK} Held note (status-script", context)
        self.assertNotIn("is due; it follows", context)
        for message_id in (other["message_id"], receipt["message_id"]):
            self.assertEqual(record.get(message_id)["result"], "delivered")
        self.assertEqual(record.get(receipt["message_id"])["wake_turn"], self.last_turn)

    def test_the_wake_span_is_not_a_reply_candidate(self):
        receipt = self.held("Is the build green?", sender="local-sender", script=False,
                            reply_mode="requested")
        self.sweep(receipt)
        self.wake_turn(answer="Working on Sam's task; nothing about the build.")
        self.assertEqual(self.owner.replies(), [])
        self.assertEqual(record.get(receipt["message_id"])["reply_state"], "no-block")

    def test_a_block_answers_the_note_once(self):
        receipt = self.held("Is the build green?", sender="local-sender", script=False,
                            reply_mode="requested")
        self.sweep(receipt)
        self.wake_turn(answer=f"[REPLY {receipt['message_id']}] Green. [/REPLY]")
        replies_sent = self.owner.replies()
        self.assertEqual(len(replies_sent), 1)
        self.assertIn("Green.", replies_sent[0]["text"])

    def test_a_private_chat_gets_one_echo_of_the_note_and_never_the_wake_line(self):
        self.add("tg-private", "Sam DM", "telegram",
                 origin={"platform": "telegram", "chat_id": OWNER, "chat_type": "dm", "user_id": OWNER})
        receipt = self.held("v2", to="tg-private")
        self.sweep(receipt)
        self.wake_turn(target="tg-private", session_id="tg-private", platform="telegram")
        self.turn(session_id="tg-private", platform="telegram")
        stamp = time.strftime("%H:%M", time.localtime(record.get(receipt["message_id"])["created_at"]))
        self.assertEqual(self.ctx.posts, [
            frame.escape_echo(f"{MARK} Held note · not from Sam · hermes@script status-script · {stamp}"
                              "\n\nv2", "telegram"),
        ])
        self.assertNotIn("is due", "".join(self.ctx.posts))

    def test_after_the_wake_is_submitted_another_turn_does_not_take_the_note(self):
        receipt = self.held()
        self.sweep(receipt)
        self.assertIsNone(self.turn())  # the user's own turn, before the queued wake runs
        self.assertIsNone(record.get(receipt["message_id"])["offered_turn"])
        self.assertIn("build state", self.wake_turn())

    def test_after_a_dead_wake_another_turn_takes_the_note(self):
        receipt = self.held()
        self.sweep(receipt)
        self.restart()
        context = self.turn()
        self.assertIn("build state", context)
        self.assertEqual(record.get(receipt["message_id"])["result"], "delivered")
        self.sweep(receipt, slack=60)
        self.assertEqual(len(self.owner.wakes()), 1)


class MigrationTests(DeadlineTestBase):
    def test_a_record_without_deadline_columns_gains_them(self):
        directory = self.record_path().parent
        directory.mkdir(parents=True, exist_ok=True)
        os.chmod(directory, 0o700)
        schema = record._SCHEMA[:record._SCHEMA.index(",\n  wake_at REAL")] + "\n)\n"
        conn = sqlite3.connect(self.record_path())
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute(schema)
            conn.commit()
        finally:
            conn.close()
        os.chmod(self.record_path(), 0o600)
        conn = record.connect()
        try:
            names = [row[1] for row in conn.execute("PRAGMA table_info(messages)")]
        finally:
            conn.close()
        for name in ("wake_at", "wake_claimed_at", "wake_turn", "woken_at"):
            self.assertEqual(names.count(name), 1)
        self.assertIsNotNone(record.get(self.held()["message_id"])["wake_at"])


if __name__ == "__main__":
    unittest.main()
