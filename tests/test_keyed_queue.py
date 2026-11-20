"""Keyed queue (G16c): Hermes's queue times the turn, the record supplies the newest version."""

from __future__ import annotations

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
import echo
import frame
import passive
import record
import replies
import route
from tests.test_cli import FakeCtx as PluginCtx
from tests.test_cli import _load_plugin

OWNER = "123456789"
MARK = "\U0001f4e1"  # 📡
PROMPT = "Sam's task"


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
    """``client.deliver_sync``: ``busy`` targets queue, idle ones start; the answer names *instance*."""

    def __init__(self):
        self.calls = []
        self.busy = True
        self.instance = "inst-1"
        self.failure = None
        self.lock = threading.Lock()

    def __call__(self, target, text, delivery, submission_id):
        with self.lock:
            self.calls.append({"target": target, "text": text, "delivery": delivery,
                               "submission_id": submission_id})
        if self.failure is not None:
            return {"ok": False, "error": self.failure, "code": None, "exit": 5, "message": "down"}
        applied = delivery if delivery == "steer" or self.busy else "start"
        return {"ok": True, "applied": applied, "applied_detail": None, "admission_id": "adm",
                "instance_id": self.instance}

    def wakes(self, target="local-target"):
        return [c for c in self.calls if c["target"] == target and c["delivery"] == "queue"]

    def replies(self, target="local-sender"):
        return [c for c in self.calls if c["target"] == target and c["delivery"] == "steer"]


class KeyedQueueTestBase(unittest.TestCase):
    def setUp(self):
        self.home = Path(tempfile.mkdtemp(prefix="intercom-keyed-queue-"))
        self.addCleanup(shutil.rmtree, self.home, ignore_errors=True)
        env = mock.patch.dict(os.environ, {"HERMES_HOME": str(self.home)})
        env.start()
        self.addCleanup(env.stop)
        (self.home / "config.yaml").write_text(
            "plugins:\n  enabled: [intercom]\n  entries:\n    intercom:\n"
            "      allow_gateway_injection: true\n      settings: {}\n", encoding="utf-8")
        self.ctx = FakeCtx(user_name="Sam", owner_user_ids=[f"telegram:{OWNER}"])
        self.owner = FakeOwner()
        for target, name, value in ((route.client, "deliver_sync", self.owner),
                                    (route.client, "owner_instance", lambda: self.owner.instance)):
            patch = mock.patch.object(target, name, value)
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

        self.db = SessionDB()
        self.addCleanup(self.db.close)
        self.started = 1.0
        self.turns = 0
        self.clock = 1_700_000_000.0
        self.add("local-sender", "Sender Chat", "cli")
        self.add("local-target", "Reviewer", "tui")

    def add(self, session_id, title, source, *, origin=None):
        self.db.create_session(session_id, source=source)
        self.db._write_sql("UPDATE sessions SET started_at = ? WHERE id = ?", (self.started, session_id))
        self.started += 1
        if title:
            self.db.set_session_title(session_id, title)
        if origin is not None:
            self.db._write_sql("UPDATE sessions SET origin_json = ? WHERE id = ?",
                               (json.dumps(origin), session_id))

    @staticmethod
    def lister(scope="live", picker=False):
        return {"ok": True, "sessions": [{"id": "local-target"}]}

    def send(self, message="build state: verifying G3", *, to="local-target", key="build-state",
             sender="status-script", script=True, delivery="queue", **kwargs):
        """A keyed queued send. Each row gets a later ``created_at`` than the one before."""
        kwargs.setdefault("reply_mode", "none")
        receipt = route.send(sender, to, message, ctx=self.ctx, lister=self.lister, script=script,
                             delivery=delivery, key=key, **kwargs)
        self.assertTrue(receipt["ok"], receipt)
        self.clock += 60
        self.set_row(receipt["message_id"], created_at=self.clock)
        return receipt

    def record_path(self):
        return self.home / "plugin-data" / "intercom" / "record.db"

    def set_row(self, message_id, **fields):
        conn = sqlite3.connect(self.record_path())
        try:
            for name, value in fields.items():
                conn.execute(f"UPDATE messages SET {name} = ? WHERE id = ?", (value, message_id))
            conn.commit()
        finally:
            conn.close()

    def restart(self, instance="inst-2"):
        """A new owner instance: Hermes's in-memory queue is gone, the record is not."""
        self.owner.instance = instance
        passive._instance["id"] = None
        passive._offered.clear()

    def turn(self, prompt=PROMPT, session_id="local-target", *, request=True, answer=None,
             platform="tui"):
        """One turn whose prompt is *prompt* (a wake line, or the user's text): ``pre_llm_call``
        (held notes, then the echo), its first model request (``request``) and, with ``answer``,
        ``post_llm_call``. Returns the held-note context, or None."""
        self.turns += 1
        turn_id = f"turn-{self.turns}"
        result = passive.on_pre_llm_call(session_id=session_id, turn_id=turn_id, user_message=prompt,
                                         conversation_history=[], platform=platform, model="m")
        context = None if result is None else result["context"]
        echo.on_pre_llm_call(session_id=session_id, user_message=prompt, platform=platform)
        # Core puts hook text after the prompt, in the turn's user message.
        user_message = prompt if context is None else f"{prompt}\n\n{context}"
        if request:
            for _ in range(2):  # a second request in the same turn changes nothing
                passive.on_pre_api_request(session_id=session_id, turn_id=turn_id, platform=platform,
                                           request_messages=[{"role": "user", "content": user_message}])
                echo.on_pre_api_request(session_id=session_id, turn_id=turn_id, platform=platform,
                                        request_messages=[{"role": "user", "content": user_message}])
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

    def wake_turn(self, number=0, **kwargs):
        """The turn Hermes runs for the *number*-th queued wake."""
        return self.turn(self.owner.wakes()[number]["text"], **kwargs)


class WakeTests(KeyedQueueTestBase):
    def test_two_sends_while_busy_make_one_wake_and_its_turn_carries_the_newer(self):
        first = self.send("build state: verifying G3")
        second = self.send("build state: G3 landed")
        self.assertEqual(len(self.owner.wakes()), 1)
        self.assertEqual((first["receipt"], second["receipt"]), ("queued", "queued"))
        self.assertEqual(second["replaced"], [first["message_id"]])
        self.assertIn("rides the wake already queued", second["message"])
        wake = self.owner.wakes()[0]
        self.assertEqual(wake["submission_id"], f"intercom-{first['message_id']}")
        self.assertIn("keyed update `build-state` from `status-script`; the current version follows.",
                      wake["text"])
        # The notice line shows the version current when the wake went out (accepted trade-off);
        # the wake body itself carries no version, the turn gets the newest one.
        body = "\n".join(wake["text"].splitlines()[1:])
        self.assertNotIn("verifying", body)
        self.assertNotIn("landed", body)
        first_line = wake["text"].splitlines()[0]
        self.assertIn("verifying G3", first_line)
        self.assertNotIn("landed", first_line)
        context = self.wake_turn()
        self.assertIn("build state: G3 landed", context)
        self.assertNotIn("verifying G3", context)
        # Framed as live mail: the turn exists for it.
        self.assertIn(f"{MARK} End of message {second['message_id']}", context)
        self.assertNotIn("Held note", context)
        self.assertEqual(record.get(second["message_id"])["result"], "delivered")
        self.assertEqual(record.get(first["message_id"])["result"], "replaced")
        self.assertIsNone(self.turn())
        self.assertEqual(len(self.owner.wakes()), 1)

    def test_a_wake_shows_this_versions_gist_not_the_wake_line(self):
        receipt = self.send("build state: verifying G3", gist="The build is ready to merge.")
        wake = self.owner.wakes()[0]["text"]
        self.assertEqual(wake.splitlines()[0],
                         f"Message from {MARK} status-script: \u201cThe build is ready to merge.\u201d")
        # The wake body still carries the wake line; the current version follows at its turn.
        self.assertIn(
            "keyed update `build-state` from `status-script`; the current version follows.",
            "\n".join(wake.splitlines()[1:]),
        )
        row = record.get(receipt["message_id"])
        self.assertEqual(row["gist"], "The build is ready to merge.")
        stored = route.frame_stored(row, self.ctx)
        self.assertTrue(stored.splitlines()[0].endswith("\u201cThe build is ready to merge.\u201d"))
        self.assertIn("build state: verifying G3", stored)

    def test_a_wake_for_a_body_with_no_readable_line_uses_the_wake_line(self):
        self.send("\U0001f4e1 Not Sam \u00b7 echo only")
        first_line = self.owner.wakes()[0]["text"].splitlines()[0]
        self.assertIn("keyed update build-state", first_line)
        self.assertNotIn("\u201c\u201d", first_line)

    def test_a_wake_without_a_gist_shows_the_versions_first_sentence(self):
        self.send("Build state: verifying G3. Details follow.")
        wake = self.owner.wakes()[0]["text"]
        self.assertEqual(wake.splitlines()[0],
                         f"Message from {MARK} status-script: \u201cBuild state: verifying G3.\u201d")
        self.assertNotIn("keyed update", wake.splitlines()[0])

    def test_the_wake_records_its_span_hash_and_marker_on_the_submitting_row(self):
        first = self.send()
        second = self.send("newer")
        wake = self.owner.wakes()[0]["text"]
        row = record.get(first["message_id"])
        self.assertEqual(row["header_sha"], route.header_sha(wake, first["message_id"]))
        self.assertIn("no reply needed", wake.splitlines()[1])
        self.assertEqual((row["wake_admission"], row["wake_instance"]),
                         (f"intercom-{first['message_id']}", "inst-1"))
        newer = record.get(second["message_id"])
        self.assertEqual((newer["wake_admission"], newer["wake_instance"], newer["header_sha"]),
                         (row["wake_admission"], "inst-1", None))
        self.assertEqual((newer["result"], newer["requested"], newer["note_key"]),
                         ("pending", "queue", "build-state"))

    def test_idle_target_starts_at_once(self):
        self.owner.busy = False
        receipt = self.send("build state: G3 landed")
        self.assertEqual(receipt["receipt"], "started")
        self.assertEqual(receipt["applied"], "start")
        self.assertIn("started a turn there", receipt["message"])
        self.assertEqual(len(self.owner.wakes()), 1)
        self.assertIn("build state: G3 landed", self.wake_turn())
        self.assertEqual(record.get(receipt["message_id"])["result"], "delivered")

    def test_receipt_words(self):
        busy = self.send()
        self.assertEqual(cli._applied_phrase(busy), "queued (replaceable, key build-state)")
        self.assertEqual(cli._applied_phrase(self.send("v2")), "queued (replaceable, key build-state)")
        self.owner.busy = False
        idle = self.send(key="other")
        self.assertTrue(cli._applied_phrase(idle).startswith("started"))

    def test_a_row_with_a_live_wake_waits_for_its_wake_turn(self):
        receipt = self.send()
        self.assertIsNone(self.turn())  # the user's own turn, before the queued wake runs
        self.assertIsNone(record.get(receipt["message_id"])["offered_turn"])
        self.assertIn("build state", self.wake_turn())

    def test_plain_unkeyed_queue_is_unchanged(self):
        receipt = self.send("plain", key=None)
        self.assertEqual(receipt["receipt"], "held")
        row = record.get(receipt["message_id"])
        self.assertEqual((row["result"], row["wake_admission"], row["note_key"]), ("held", None, None))
        call = self.owner.wakes()[0]
        self.assertIn("plain", call["text"])
        self.assertIsNone(self.turn(call["text"]))
        self.assertEqual(record.get(receipt["message_id"])["result"], "held")

    def test_a_wake_the_owner_did_not_take_leaves_the_row_for_the_next_turn(self):
        self.owner.failure = "no-owner"
        receipt = self.send()
        self.assertEqual(receipt["receipt"], "held")
        self.assertIn("did not take its wake (no-owner)", receipt["message"])
        self.assertTrue(cli._applied_phrase(receipt).startswith("held"))
        self.assertIsNone(record.get(receipt["message_id"])["wake_instance"])
        self.assertIn("build state", self.turn())
        self.assertEqual(record.get(receipt["message_id"])["result"], "delivered")

    def test_key_with_ask_is_still_refused(self):
        receipt = route.send("local-sender", "local-target", "q?", ctx=self.ctx, lister=self.lister,
                             delivery="queue", reply_mode="waiting", kind="ask", key="k")
        self.assertEqual((receipt["ok"], receipt["error"], receipt["exit"]), (False, "usage", 2))
        self.assertEqual(receipt["message"], route.KEY_WITH_ASK)
        self.assertEqual(self.owner.calls, [])

    def test_passive_and_queued_with_the_same_key_do_not_replace_each_other(self):
        held = self.send("passive status", delivery="passive")
        queued = self.send("queued status")
        self.assertEqual(record.get(held["message_id"])["result"], "pending")
        self.assertEqual(record.get(queued["message_id"])["result"], "pending")
        context = self.wake_turn()
        self.assertIn("queued status", context)
        self.assertIn(f"{MARK} Held note (status-script", context)
        self.assertIn("passive status", context)
        self.assertEqual(record.get(held["message_id"])["result"], "delivered")
        self.assertEqual(record.get(queued["message_id"])["result"], "delivered")

    def test_status_and_log_words(self):
        first = self.send()
        second = self.send("v2")
        rows = {r["id"]: r for r in (record.get(first["message_id"]), record.get(second["message_id"]))}
        self.assertEqual(cli._delivery_words(rows[second["message_id"]]), "queued (replaceable)")
        self.assertEqual(cli._log_delivery(rows[first["message_id"]]),
                         f"queue: replaced by {second['message_id']} · key build-state")
        self.wake_turn()
        self.assertTrue(cli._delivery_words(record.get(second["message_id"])).startswith("delivered "))


class ClaimRaceTests(KeyedQueueTestBase):
    def test_a_send_committed_before_the_wake_turn_claims_rides_this_turn(self):
        self.send("v1")
        newer = self.send("v2")
        self.assertIn("v2", self.wake_turn())
        self.assertEqual(record.get(newer["message_id"])["result"], "delivered")
        self.assertEqual(len(self.owner.wakes()), 1)

    def test_a_send_committed_after_the_wake_turn_claims_queues_a_second_wake(self):
        first = self.send("v1")
        self.assertIn("v1", self.wake_turn(request=False))
        newer = self.send("v2")
        self.assertEqual(newer["receipt"], "queued")
        self.assertEqual(len(self.owner.wakes()), 2)
        passive.on_pre_api_request(session_id="local-target", turn_id=self.last_turn, platform="tui")
        row = record.get(first["message_id"])
        self.assertEqual((row["result"], row["offered_turn"]), ("replaced", self.last_turn))
        self.assertIsNotNone(row["delivered_at"])
        self.assertIn("v2", self.wake_turn(1))
        self.assertEqual(record.get(newer["message_id"])["result"], "delivered")

    def test_a_send_committed_exactly_as_the_wake_turn_claims_is_never_lost(self):
        for attempt in range(4):
            with self.subTest(attempt=attempt):
                self.owner.calls.clear()
                key = f"race-{attempt}"
                self.send("v1", key=key)
                wake = self.owner.wakes()[0]["text"]
                holder = sqlite3.connect(self.record_path(), isolation_level=None, timeout=30)
                holder.execute("BEGIN IMMEDIATE")  # both writers now wait on the record
                out = {}
                turn_id = f"race-turn-{attempt}"
                send = threading.Thread(target=lambda: out.update(send=route.send(
                    "status-script", "local-target", "v2", ctx=self.ctx, lister=self.lister,
                    script=True, delivery="queue", key=key, reply_mode="none")))
                claim = threading.Thread(target=lambda: out.update(turn=passive.on_pre_llm_call(
                    session_id="local-target", turn_id=turn_id, user_message=wake)))
                for thread in ((send, claim) if attempt % 2 else (claim, send)):
                    thread.start()
                time.sleep(0.3)
                holder.execute("COMMIT")
                holder.close()
                send.join(30)
                claim.join(30)
                passive.on_pre_api_request(session_id="local-target", turn_id=turn_id, platform="tui")
                context = (out.get("turn") or {}).get("context") or ""
                newer = out["send"]["message_id"]
                if "v2" in context:
                    self.assertEqual(len(self.owner.wakes()), 1)
                else:
                    self.assertIn("v1", context)
                    self.assertEqual(len(self.owner.wakes()), 2)
                    self.assertIn("v2", self.turn(self.owner.wakes()[1]["text"]))
                self.assertEqual(record.get(newer)["result"], "delivered")


class WakeTurnOutcomeTests(KeyedQueueTestBase):
    def test_interrupted_during_its_api_call_or_a_non_retryable_error_delivers_once(self):
        receipt = self.send()
        self.assertIn("build state", self.wake_turn(request=True))  # first request built; no post_llm_call
        row = record.get(receipt["message_id"])
        self.assertEqual((row["result"], row["offered_turn"]), ("delivered", self.last_turn))
        self.assertIsNone(self.turn())
        self.assertEqual(len(self.owner.wakes()), 1)

    def test_a_wake_turn_that_stops_before_its_first_request_leaves_it_for_the_next_turn(self):
        receipt = self.send()
        self.wake_turn(request=False)
        self.assertEqual(record.get(receipt["message_id"])["result"], "pending")
        context = self.turn()
        self.assertIn(f"{MARK} Held note (status-script", context)
        self.assertIn("build state", context)
        self.assertEqual(record.get(receipt["message_id"])["result"], "delivered")
        self.assertIsNone(self.turn())
        self.assertEqual(len(self.owner.wakes()), 1)


class RestartTests(KeyedQueueTestBase):
    def test_a_marker_from_an_earlier_owner_instance_is_dead_and_the_row_rides_the_next_turn(self):
        receipt = self.send()
        self.assertIsNone(self.turn())  # live wake: waits
        self.restart()
        context = self.turn()
        self.assertIn("build state", context)
        self.assertIn("Held note", context)
        self.assertEqual(record.get(receipt["message_id"])["result"], "delivered")
        self.assertEqual(len(self.owner.wakes()), 1)

    def test_a_send_seeing_a_different_owner_instance_queues_a_new_wake(self):
        first = self.send("v1")
        self.restart()
        second = self.send("v2")
        self.assertEqual(second["replaced"], [first["message_id"]])
        self.assertEqual(len(self.owner.wakes()), 2)
        self.assertEqual(record.get(second["message_id"])["wake_instance"], "inst-2")
        self.assertIn("v2", self.wake_turn(1))
        self.assertEqual(record.get(second["message_id"])["result"], "delivered")

    def test_the_marker_names_the_instance_that_took_the_wake(self):
        self.owner.instance = "inst-3"  # the owner answering delivery, not the one found before it
        with mock.patch.object(route.client, "owner_instance", return_value="inst-2"):
            receipt = self.send()
        self.assertEqual(record.get(receipt["message_id"])["wake_instance"], "inst-3")

    def test_the_owner_instance_is_discovered_once_per_process(self):
        self.send()
        with mock.patch.object(route.client, "owner_instance", return_value="inst-1") as found:
            self.turn()
            self.turn()
        self.assertEqual(found.call_count, 1)

    def test_a_cli_register_between_two_sends_does_not_cause_a_second_wake(self):
        self.send("v1")
        plugin = _load_plugin(f"hermes_intercom_keyed_{self._testMethodName}")
        with mock.patch.object(plugin.inbox, "start", side_effect=AssertionError("inbox.start")):
            plugin.register(PluginCtx(user_name="Sam"))
        second = self.send("v2")
        self.assertEqual(len(self.owner.wakes()), 1)
        self.assertEqual(record.get(second["message_id"])["wake_instance"], "inst-1")
        self.assertIn("v2", self.wake_turn())


class EchoTests(KeyedQueueTestBase):
    def test_a_private_chat_gets_one_echo_of_the_newest_and_never_the_wake_line(self):
        self.add("tg-private", "Sam DM", "telegram",
                 origin={"platform": "telegram", "chat_id": OWNER, "chat_type": "dm", "user_id": OWNER})
        self.send("v1", to="tg-private")
        newest = self.send("v2", to="tg-private")
        self.turn(self.owner.wakes("tg-private")[0]["text"], "tg-private", platform="telegram")
        self.turn(session_id="tg-private", platform="telegram")
        stamp = time.strftime("%H:%M", time.localtime(record.get(newest["message_id"])["created_at"]))
        self.assertEqual(self.ctx.posts, [
            frame.escape_echo(f"{MARK} Held note · not from Sam · hermes@script status-script · {stamp}"
                              "\n\nv2", "telegram"),
        ])
        self.assertNotIn("keyed update", "".join(self.ctx.posts))


class AnswerTests(KeyedQueueTestBase):
    def send_requested(self, message):
        return self.send(message, sender="local-sender", script=False, reply_mode="requested")

    def test_a_requested_keyed_queued_message_gets_exactly_one_reply(self):
        receipt = self.send_requested("Is the build green?")
        self.wake_turn(answer="Yes, green on main.")
        replies_sent = self.owner.replies()
        self.assertEqual(len(replies_sent), 1)
        self.assertIn("Yes, green on main.", replies_sent[0]["text"])
        self.assertEqual(record.get(receipt["message_id"])["reply_state"], "sent")

    def test_only_the_newest_version_is_answered(self):
        older = self.send_requested("Is G3 verified?")
        newer = self.send_requested("Did G3 land?")
        self.wake_turn(answer="It landed.")
        replies_sent = self.owner.replies()
        self.assertEqual(len(replies_sent), 1)
        self.assertIn(f"message {newer['message_id']}", replies_sent[0]["text"])
        self.assertIsNone(record.get(older["message_id"])["reply_state"])
        self.assertEqual(record.get(newer["message_id"])["reply_state"], "sent")

    def test_riding_another_turn_it_is_answered_only_by_its_block(self):
        receipt = self.send_requested("Is the build green?")
        self.restart()
        self.turn(answer="Here is what Sam asked for.")
        self.assertEqual(self.owner.replies(), [])
        self.assertEqual(record.get(receipt["message_id"])["reply_state"], "no-block")


class MigrationTests(KeyedQueueTestBase):
    def test_a_record_without_wake_columns_gains_them(self):
        directory = self.record_path().parent
        directory.mkdir(parents=True, exist_ok=True)
        os.chmod(directory, 0o700)
        schema = record._SCHEMA[:record._SCHEMA.index(",\n  wake_admission TEXT")] + "\n)\n"
        self.assertNotIn("wake_", schema)
        conn = sqlite3.connect(self.record_path())
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute(schema)
            conn.commit()
        finally:
            conn.close()
        os.chmod(self.record_path(), 0o600)
        first, second = record.connect(), record.connect()
        try:
            for conn in (first, second):
                names = [row[1] for row in conn.execute("PRAGMA table_info(messages)")]
                self.assertEqual([names.count("wake_admission"), names.count("wake_instance")], [1, 1])
        finally:
            first.close()
            second.close()
        receipt = self.send()
        self.assertEqual(record.get(receipt["message_id"])["wake_instance"], "inst-1")


if __name__ == "__main__":
    unittest.main()
