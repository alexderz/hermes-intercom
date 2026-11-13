"""Replies: the post_llm_call hook (steps 1-7 in replies.py), the span finder, and the in-process ask."""

from __future__ import annotations

import contextvars
import json
import os
import shutil
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

import record
import replies
import route

OWNER = "123456789"
GROUP = "-1001234567890"
OTHER_GROUP = "-1009999999999"
MARK = "\U0001f4e1"  # 📡

_probe = contextvars.ContextVar("intercom_test_probe", default=None)


class FakeCtx:
    def __init__(self, **settings):
        self.settings = settings
        self.hooks = []

    def get_config(self, key, default=None):
        return self.settings.get(key, default)

    def register_hook(self, name, fn):
        self.hooks.append((name, fn))


class FakeOwner:
    """``client.deliver_sync`` stand-in: records each call, with its thread and context probe."""

    def __init__(self):
        self.calls = []
        self.gate = None
        self.lock = threading.Lock()

    def __call__(self, target, text, delivery, submission_id):
        if self.gate is not None:
            self.gate.wait(10)
        with self.lock:
            self.calls.append({"target": target, "text": text, "delivery": delivery,
                               "message_id": submission_id.removeprefix("intercom-"),
                               "thread": threading.current_thread(), "probe": _probe.get()})
        return {"ok": True, "applied": "steer", "applied_detail": None, "admission_id": None}


class RepliesTestBase(unittest.TestCase):
    def setUp(self):
        self.home = Path(tempfile.mkdtemp(prefix="intercom-replies-"))
        self.addCleanup(shutil.rmtree, self.home, ignore_errors=True)
        env = mock.patch.dict(os.environ, {"HERMES_HOME": str(self.home)})
        env.start()
        self.addCleanup(env.stop)
        (self.home / "config.yaml").write_text(
            "plugins:\n  enabled: [intercom]\n  entries:\n    intercom:\n"
            "      allow_gateway_injection: true\n      settings: {}\n", encoding="utf-8")
        self.ctx = FakeCtx(user_name="Sam", echo_chats=[f"telegram:{GROUP}"],
                           owner_user_ids=[f"telegram:{OWNER}"])
        self.owner = FakeOwner()
        for name, value in (("deliver_sync", self.owner), ("list_sessions", self.lister)):
            patch = mock.patch.object(route.client, name, value)
            patch.start()
            self.addCleanup(patch.stop)
        ctx_patch = mock.patch.dict(replies._ctx, {"ctx": self.ctx})
        ctx_patch.start()
        self.addCleanup(ctx_patch.stop)
        self.addCleanup(replies._waiters.clear)

        from hermes_state import SessionDB

        self.db = SessionDB()
        self.addCleanup(self.db.close)
        self.started = 1.0
        self.add("local-sender", "Sender Chat", "cli")
        self.add("local-target", "Reviewer", "tui")

    @staticmethod
    def lister(scope="live", picker=False):
        return {"ok": True, "sessions": []}

    def add(self, session_id, title, source, *, origin=None, parent=None):
        kwargs = {"parent_session_id": parent} if parent else {}
        self.db.create_session(session_id, source=source, **kwargs)
        self.db._write_sql("UPDATE sessions SET started_at = ? WHERE id = ?", (self.started, session_id))
        self.started += 1
        if title:
            self.db.set_session_title(session_id, title)
        if origin is not None:
            self.db._write_sql("UPDATE sessions SET origin_json = ? WHERE id = ?",
                               (json.dumps(origin), session_id))

    def send(self, reply_mode, *, sender="local-sender", to="local-target", message="Is 22:00 still ok?",
             **kwargs):
        """A real framed message through route.send. Returns ``(message_id, submitted text)``."""
        before = len(self.owner.calls)
        receipt = route.send(sender, to, message, ctx=self.ctx, reply_mode=reply_mode,
                             lister=self.lister, **kwargs)
        self.assertTrue(receipt["ok"], receipt)
        self.assertEqual(len(self.owner.calls), before + 1)
        return receipt["message_id"], self.owner.calls[-1]["text"]

    def turn(self, answer, *, prompt="", steers=(), session_id="local-target", history_before=()):
        """Run the hook for one finished turn of *session_id*, then wait for reply threads."""
        history = [*history_before, {"role": "user", "content": prompt}]
        for steer in steers:
            history.append({"role": "assistant", "content": "", "tool_calls": []})
            history.append({"role": "user", "content": f"[steer]\n{steer}\n[/steer]",
                            "display_kind": "steer"})
        history.append({"role": "assistant", "content": answer})
        before = len(self.owner.calls)
        replies.on_post_llm_call(session_id=session_id, user_message=prompt,
                                 assistant_response=answer, conversation_history=history,
                                 model="m", platform="tui")
        self.join_replies()
        return self.owner.calls[before:]

    @staticmethod
    def join_replies():
        for thread in threading.enumerate():
            if thread.name.startswith("intercom-reply-"):
                thread.join(10)

    def reply_header(self, call):
        return call["text"].splitlines()[0]


class SpanFinderTests(RepliesTestBase):
    def test_finds_the_span_and_checks_the_stored_hash(self):
        message_id, text = self.send("requested")
        spans = replies.find_spans(text)
        self.assertEqual([s.message_id for s in spans], [message_id])
        self.assertEqual(spans[0].sha, record.get(message_id)["header_sha"])
        self.assertTrue(spans[0].text.startswith(f"{MARK} Not Sam"))
        self.assertTrue(spans[0].text.endswith(f"{MARK} End of message {message_id}"))
        found = replies.find_messages([text])
        self.assertEqual([row["id"] for row, _span in found], [message_id])

    def test_span_inside_a_steer_wrapper_and_other_text(self):
        message_id, text = self.send("requested")
        wrapped = f"Sam says: also look at this.\n{text}\ntrailing words"
        self.assertEqual([r["id"] for r, _ in replies.find_messages([wrapped])], [message_id])

    def test_changed_span_or_typed_lookalike_has_no_row(self):
        _message_id, text = self.send("requested")
        changed = text.replace("22:00", "23:00")
        self.assertEqual(replies.find_messages([changed]), [])
        lookalike = (f"{MARK} Not Sam · reply requested · hermes@Sender Chat says "
                     f"(session local-sender · message deadbeef):\nhi\n{MARK} End of message deadbeef")
        self.assertEqual(len(replies.find_spans(lookalike)), 1)
        self.assertEqual(replies.find_messages([lookalike]), [])

    def test_header_without_closing_line_is_no_span(self):
        _message_id, text = self.send("requested")
        self.assertEqual(replies.find_spans(text.rsplit("\n", 1)[0]), [])

    def test_each_message_found_once(self):
        message_id, text = self.send("requested")
        self.assertEqual(len(replies.find_messages([text, text])), 1)
        self.assertEqual(replies.find_messages([text])[0][0]["id"], message_id)

    def test_turn_rows_are_the_prompt_and_the_steers_after_it(self):
        history = [
            {"role": "user", "content": "old steer", "display_kind": "steer"},
            {"role": "user", "content": "the prompt"},
            {"role": "assistant", "content": ""},
            {"role": "user", "content": [{"type": "text", "text": "steer one"}], "display_kind": "steer"},
            {"role": "tool", "content": "result"},
            {"role": "user", "content": "steer two", "display_kind": "steer"},
            {"role": "assistant", "content": "answer"},
        ]
        self.assertEqual(replies.turn_rows("the prompt", history), ["the prompt", "steer one", "steer two"])


class ReplyModeTests(RepliesTestBase):
    def test_none_note_steered_into_a_busy_turn_sends_nothing_even_with_a_block(self):
        message_id, text = self.send("none")
        answer = f"Done for Sam.\n[REPLY {message_id}]Here is more.[/REPLY]"
        calls = self.turn(answer, prompt="Sam's own task", steers=[text])
        self.assertEqual(calls, [])
        row = record.get(message_id)
        self.assertIsNone(row["reply_state"])
        self.assertIsNone(row["reply"])

    def test_requested_block_is_delivered_as_an_optional_reply(self):
        message_id, text = self.send("requested")
        calls = self.turn(f"Report for Sam.\n[REPLY {message_id}]\nYes, 22:00.\n[/REPLY]", steers=[text])
        self.assertEqual(len(calls), 1)
        call = calls[0]
        self.assertEqual(call["target"], "local-sender")
        self.assertEqual(call["delivery"], "steer")
        header = self.reply_header(call)
        self.assertIn("reply optional", header)
        self.assertIn(f"reply to your message {message_id}", header)
        self.assertIn("hermes@Reviewer", header)
        self.assertIn("Yes, 22:00.", call["text"])
        self.assertNotIn("Report for Sam", call["text"])
        row = record.get(message_id)
        self.assertEqual(row["reply_state"], "sent")
        self.assertEqual(row["reply"], "Yes, 22:00.")
        self.assertEqual(row["reply_message_id"], call["message_id"])
        reply_row = record.get(call["message_id"])
        self.assertEqual(reply_row["reply_mode"], "optional")
        self.assertEqual(reply_row["in_reply_to"], message_id)
        self.assertEqual(reply_row["sender_session_id"], "local-target")

    def test_requested_and_waiting_fall_back_to_the_whole_answer(self):
        for mode in ("requested", "waiting"):
            with self.subTest(mode=mode):
                message_id, text = self.send(mode)
                calls = self.turn("Yes, 22:00 still holds.", prompt=text)
                self.assertEqual(len(calls), 1)
                self.assertIn("Yes, 22:00 still holds.", calls[0]["text"])
                self.assertEqual(record.get(message_id)["reply_state"], "sent")

    def test_a_reply_without_a_block_sends_nothing_back(self):
        message_id, text = self.send("optional", reply_to="abcd1234")
        calls = self.turn("Thanks, noted for Sam.", steers=[text])
        self.assertEqual(calls, [])
        row = record.get(message_id)
        self.assertEqual(row["reply_state"], "no-block")
        self.assertIsNone(row["reply"])

    def test_a_reply_with_a_block_is_delivered(self):
        message_id, text = self.send("optional", reply_to="abcd1234")
        calls = self.turn(f"For Sam.\n[REPLY {message_id}]I disagree: 23:00 is safer.[/REPLY]", steers=[text])
        self.assertEqual(len(calls), 1)
        self.assertIn("I disagree: 23:00 is safer.", calls[0]["text"])
        self.assertIn(f"reply to your message {message_id}", self.reply_header(calls[0]))
        self.assertEqual(record.get(message_id)["reply_state"], "sent")

    def test_two_messages_two_blocks_each_to_its_own_sender(self):
        self.add("local-other", "Other Chat", "cli")
        first, first_text = self.send("requested")
        second, second_text = self.send("requested", sender="local-other")
        answer = f"[REPLY {first}]to the first[/REPLY]\n[REPLY {second}]to the second[/REPLY]"
        calls = self.turn(answer, prompt=first_text, steers=[second_text])
        by_target = {c["target"]: c["text"] for c in calls}
        self.assertEqual(set(by_target), {"local-sender", "local-other"})
        self.assertIn("to the first", by_target["local-sender"])
        self.assertNotIn("to the second", by_target["local-sender"])
        self.assertIn("to the second", by_target["local-other"])
        self.assertNotIn("to the first", by_target["local-other"])

    def test_an_ask_gets_no_automatic_reply(self):
        receipt = route.send("local-sender", "local-target", "q?", ctx=self.ctx, reply_mode="waiting",
                             kind="ask", lister=self.lister)
        message_id, text = receipt["message_id"], self.owner.calls[-1]["text"]
        self.assertEqual(self.turn(f"[REPLY {message_id}]answer[/REPLY]", prompt=text), [])
        self.assertIsNone(record.get(message_id)["reply_state"])

    def test_steer_from_before_the_turn_prompt_is_not_this_turn(self):
        message_id, text = self.send("requested")
        old = [{"role": "user", "content": text, "display_kind": "steer"},
               {"role": "assistant", "content": "earlier"}]
        self.assertEqual(self.turn("an answer", prompt="Sam asks something", history_before=old), [])
        self.assertIsNone(record.get(message_id)["reply_state"])


class SilenceTests(RepliesTestBase):
    def test_silent_answers_send_nothing(self):
        cases = {
            "silent block": lambda mid: f"For Sam.\n[REPLY {mid}] [SILENT] [/REPLY]",
            "empty block": lambda mid: f"For Sam.\n[REPLY {mid}]\n[/REPLY]",
            "whole answer": lambda mid: "[SILENT]",
            "first line": lambda mid: "[SILENT]\nnothing new here",
            "last line": lambda mid: "Checked it.\n[silent]",
            "empty answer": lambda mid: "   ",
        }
        for name, answer in cases.items():
            with self.subTest(case=name):
                message_id, text = self.send("requested")
                self.assertEqual(self.turn(answer(message_id), prompt=text), [])
                row = record.get(message_id)
                self.assertEqual(row["reply_state"], "silent")
                self.assertIsNone(row["reply"])

    def test_silent_mid_sentence_is_content(self):
        message_id, text = self.send("requested")
        calls = self.turn("The cron answered [SILENT] last night, so it is fine.", prompt=text)
        self.assertEqual(len(calls), 1)
        self.assertEqual(record.get(message_id)["reply_state"], "sent")


class SharedSenderTests(RepliesTestBase):
    def setUp(self):
        super().setUp()
        self.add("tg-shared", "Team group", "telegram",
                 origin={"platform": "telegram", "chat_id": OTHER_GROUP, "chat_type": "group"})

    def test_shared_sender_without_a_block_is_stored(self):
        message_id, text = self.send("requested", sender="tg-shared")
        self.assertEqual(record.get(message_id)["sender_chat_scope"], "shared")
        self.assertEqual(self.turn("Private notes for Sam.", prompt=text), [])
        row = record.get(message_id)
        self.assertEqual(row["reply_state"], "stored")
        self.assertEqual(row["detail"], "shared-chat-no-block")
        self.assertIsNone(row["reply"])

    def test_shared_sender_gets_the_block(self):
        message_id, text = self.send("waiting", sender="tg-shared")
        calls = self.turn(f"Private notes.\n[REPLY {message_id}]Yes.[/REPLY]", prompt=text)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["target"], "tg-shared")
        self.assertNotIn("Private notes", calls[0]["text"])

    def test_null_scope_with_a_messaging_source_is_shared(self):
        row = {"id": "abcd1234", "reply_mode": "requested", "sender_chat_scope": None,
               "sender_source": "telegram"}
        self.assertEqual(replies.reply_for(row, "whole answer"), ("stored", None, "shared-chat-no-block"))
        row["sender_source"] = "cli"
        self.assertEqual(replies.reply_for(row, "whole answer"), ("sent", "whole answer", None))

    def test_private_messaging_sender_gets_the_whole_answer(self):
        self.add("tg-private", "Sam DM", "telegram",
                 origin={"platform": "telegram", "chat_id": OWNER, "chat_type": "dm", "user_id": OWNER})
        message_id, text = self.send("requested", sender="tg-private")
        self.assertEqual(record.get(message_id)["sender_chat_scope"], "private")
        calls = self.turn("Yes, 22:00.", prompt=text)
        self.assertEqual(len(calls), 1)


class FinishedSenderTests(RepliesTestBase):
    def test_cron_subagent_and_oneshot_senders_are_stored_only(self):
        for source in ("cron", "subagent", "oneshot"):
            with self.subTest(source=source):
                sid = f"run-{source}"
                self.add(sid, f"A {source}", source)
                message_id, text = self.send("requested", sender=sid)
                self.assertEqual(self.turn("Yes, 22:00.", prompt=text), [])
                row = record.get(message_id)
                self.assertEqual(row["reply_state"], "stored")
                self.assertEqual(row["reply"], "Yes, 22:00.")
                self.assertIsNone(row["reply_message_id"])

    def test_script_sender_is_stored_only(self):
        message_id, text = self.send("requested", sender="nightly backup", script=True)
        self.assertEqual(self.turn(f"[REPLY {message_id}]All good.[/REPLY]", prompt=text), [])
        row = record.get(message_id)
        self.assertEqual(row["reply_state"], "stored")
        self.assertEqual(row["reply"], "All good.")


class RecordCheckTests(RepliesTestBase):
    def test_a_message_is_answered_once(self):
        message_id, text = self.send("requested")
        self.assertEqual(len(self.turn("Yes.", prompt=text)), 1)
        self.assertEqual(self.turn("Yes again.", prompt=text), [])
        self.assertEqual(record.get(message_id)["reply"], "Yes.")

    def test_a_message_to_another_session_is_not_answered_here(self):
        self.add("local-third", "Third", "cli")
        message_id, text = self.send("requested")
        self.assertEqual(self.turn("Yes.", prompt=text, session_id="local-third"), [])
        self.assertIsNone(record.get(message_id)["reply_state"])

    def test_the_compression_tip_answers_for_its_ancestor(self):
        message_id, text = self.send("requested")
        self.db.end_session("local-target", "compression")
        self.add("local-target-2", None, "tui", parent="local-target")
        calls = self.turn("Yes, after compaction.", prompt=text, session_id="local-target-2")
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["target"], "local-sender")
        self.assertEqual(record.get(message_id)["reply_state"], "sent")

    def test_failed_turn_fires_no_hook_and_nothing_retries(self):
        message_id, _text = self.send("requested")
        # Stopped and failed turns never call the hook; the row simply stays unanswered.
        self.join_replies()
        self.assertEqual(len(self.owner.calls), 1)
        self.assertIsNone(record.get(message_id)["reply_state"])


class DeliveryTests(RepliesTestBase):
    def test_reply_is_delivered_on_a_fresh_thread_with_the_turn_context(self):
        message_id, text = self.send("requested")
        token = _probe.set("turn-context")
        self.addCleanup(_probe.reset, token)
        calls = self.turn(f"[REPLY {message_id}]Yes.[/REPLY]", prompt=text)
        self.assertEqual(len(calls), 1)
        self.assertIsNot(calls[0]["thread"], threading.current_thread())
        self.assertEqual(calls[0]["probe"], "turn-context")

    def test_hook_returns_before_the_reply_is_delivered(self):
        message_id, text = self.send("requested")
        self.owner.gate = threading.Event()
        before = len(self.owner.calls)
        replies.on_post_llm_call(session_id="local-target", user_message=text,
                                 assistant_response="Yes.", conversation_history=[])
        self.assertEqual(len(self.owner.calls), before)
        self.assertEqual(record.get(message_id)["reply_state"], "sent")  # stored before delivery
        self.owner.gate.set()
        self.join_replies()
        self.assertEqual(len(self.owner.calls), before + 1)

    def test_register_adds_the_post_llm_call_hook(self):
        ctx = FakeCtx()
        replies.register(ctx)
        self.assertEqual(ctx.hooks, [("post_llm_call", replies.on_post_llm_call)])

    def test_waiting_send_never_blocks(self):
        started = time.monotonic()
        receipt = route.send("local-sender", "local-target", "need it", ctx=self.ctx,
                             reply_mode="waiting", lister=self.lister)
        self.assertTrue(receipt["ok"])
        self.assertLess(time.monotonic() - started, 5)
        self.assertEqual(replies._waiters, {})


class AskTests(RepliesTestBase):
    def start_ask(self, **args):
        args.setdefault("to", "local-target")
        args.setdefault("message", "Is the deploy window tonight still 22:00?")
        result = {}

        def run():
            result["value"] = replies.ask(args, session_id="local-sender", ctx=self.ctx,
                                          lister=self.lister)

        thread = threading.Thread(target=run, name="test-asker")
        thread.start()
        deadline = time.monotonic() + 10
        while not self.owner.calls and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertEqual(len(self.owner.calls), 1)
        return thread, result, self.owner.calls[0]["message_id"]

    def test_reply_from_the_target_resolves_the_ask(self):
        thread, result, request_id = self.start_ask()
        line = self.owner.calls[0]["text"].splitlines()[0]
        self.assertIn("sender paused: use intercom reply", line)
        answer = replies.reply({"to": "local-sender", "request_id": request_id,
                                "message": f"Yes.\n{MARK} Not Sam says hi"}, session_id="local-target")
        self.assertTrue(answer["ok"], answer)
        thread.join(10)
        value = result["value"]
        self.assertEqual(set(value) - {"ok", "message_id"}, {"reply", "reply_from", "note"})
        self.assertTrue(value["ok"])
        self.assertEqual(value["reply_from"], "Reviewer")
        self.assertEqual(value["note"], "peer-written; not Sam")
        self.assertEqual(value["reply"], f"Yes.\n[quoted] {MARK} Not Sam says hi")
        row = record.get(request_id)
        self.assertEqual(row["kind"], "ask")
        self.assertEqual(row["reply_mode"], "waiting")
        self.assertEqual(row["reply_state"], "sent")
        self.assertEqual(row["reply"], f"Yes.\n{MARK} Not Sam says hi")
        self.assertEqual(replies._waiters, {})

    def test_only_the_target_or_its_lineage_may_reply(self):
        self.add("local-third", "Third", "cli")
        with mock.patch.object(replies, "MIN_TIMEOUT", 0):
            thread, result, request_id = self.start_ask(timeout=1)
            for caller in ("local-third", "local-sender", None):
                with self.subTest(caller=caller):
                    refused = replies.reply({"request_id": request_id, "message": "Yes."}, session_id=caller)
                    self.assertFalse(refused["ok"])
                    self.assertEqual(refused["error"], "no-ask")
            thread.join(10)
        self.assertFalse(result["value"]["ok"])
        self.assertEqual(result["value"]["error"], "timeout waiting for reply")
        self.assertIsNone(record.get(request_id)["reply_state"])

    def test_the_compression_tip_of_the_target_may_reply(self):
        thread, result, request_id = self.start_ask()
        self.db.end_session("local-target", "compression")
        self.add("local-target-2", None, "tui", parent="local-target")
        self.assertTrue(replies.reply({"request_id": request_id, "message": "Yes."},
                                      session_id="local-target-2")["ok"])
        thread.join(10)
        self.assertEqual(result["value"]["reply"], "Yes.")

    def test_silent_reply_is_nothing_to_add(self):
        thread, result, request_id = self.start_ask()
        replies.reply({"request_id": request_id, "message": "[SILENT]"}, session_id="local-target")
        thread.join(10)
        self.assertEqual(result["value"]["reply"], '"Reviewer" replied that it has nothing to add.')
        self.assertEqual(record.get(request_id)["reply_state"], "silent")

    def test_second_reply_and_late_reply_are_refused(self):
        thread, _result, request_id = self.start_ask()
        self.assertTrue(replies.reply({"request_id": request_id, "message": "one"},
                                      session_id="local-target")["ok"])
        self.assertEqual(replies.reply({"request_id": request_id, "message": "two"},
                                       session_id="local-target")["error"], "no-ask")
        thread.join(10)
        self.assertEqual(replies.reply({"request_id": request_id, "message": "three"},
                                       session_id="local-target")["error"], "no-ask")
        self.assertEqual(record.get(request_id)["reply"], "one")

    def test_empty_reply_is_refused(self):
        thread, _result, request_id = self.start_ask()
        self.assertEqual(replies.reply({"request_id": request_id, "message": "  "},
                                       session_id="local-target")["error"], "empty")
        replies.reply({"request_id": request_id, "message": "[SILENT]"}, session_id="local-target")
        thread.join(10)

    def test_refused_ask_returns_the_receipt_and_leaves_no_waiter(self):
        value = replies.ask({"to": "Nobody", "message": "q?"}, session_id="local-sender",
                            ctx=self.ctx, lister=self.lister)
        self.assertFalse(value["ok"])
        self.assertEqual(value["error"], "unknown")
        self.assertEqual(replies._waiters, {})

    def test_timeout_is_clamped(self):
        self.assertEqual(replies._timeout(None), 600)
        self.assertEqual(replies._timeout(1), 10)
        self.assertEqual(replies._timeout(99999), 3600)
        self.assertEqual(replies._timeout("x"), 600)


if __name__ == "__main__":
    unittest.main()
