"""Echo: hook selection, the once-only claim, the post decision and the missing-escaper warning."""

from __future__ import annotations

import contextvars
import json
import os
import shutil
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import echo
import frame
import record
import route

OWNER = "123456789"
GROUP = "-1001234567890"
OTHER_GROUP = "-1009999999999"
ROOM = "!room:example.org"
MARK = "\U0001f4e1"  # 📡

_probe = contextvars.ContextVar("intercom_echo_probe", default=None)


class FakeCtx:
    def __init__(self, **settings):
        self.settings = settings
        self.hooks = []
        self.posts = []
        self.lock = threading.Lock()
        self.gate = None
        self.result = SimpleNamespace(ok=True, error=None)
        self.raises = None

    def get_config(self, key, default=None):
        return self.settings.get(key, default)

    def register_hook(self, name, fn):
        self.hooks.append((name, fn))

    def post_message(self, text):
        if self.gate is not None:
            self.gate.wait(10)
        with self.lock:
            self.posts.append({"text": text, "thread": threading.current_thread(),
                               "probe": _probe.get()})
        if self.raises is not None:
            raise self.raises
        return self.result


class EchoTestBase(unittest.TestCase):
    def setUp(self):
        self.home = Path(tempfile.mkdtemp(prefix="intercom-echo-"))
        self.addCleanup(shutil.rmtree, self.home, ignore_errors=True)
        env = mock.patch.dict(os.environ, {"HERMES_HOME": str(self.home)})
        env.start()
        self.addCleanup(env.stop)
        (self.home / "config.yaml").write_text(
            "plugins:\n  enabled: [intercom]\n  entries:\n    intercom:\n"
            "      allow_gateway_injection: true\n      settings: {}\n", encoding="utf-8")
        self.ctx = FakeCtx(user_name="Sam", echo_chats=[f"telegram:{GROUP}", f"matrix:{ROOM}"],
                           owner_user_ids=[f"telegram:{OWNER}"])
        patch = mock.patch.object(route.client, "deliver_sync", self.deliver)
        patch.start()
        self.addCleanup(patch.stop)
        ctx_patch = mock.patch.dict(echo._ctx, {"ctx": self.ctx})
        ctx_patch.start()
        self.addCleanup(ctx_patch.stop)
        self.submitted = []
        self.echoes = {}

        from hermes_state import SessionDB

        self.db = SessionDB()
        self.addCleanup(self.db.close)
        self.started = 1.0
        self.add("local-sender", "Sender Chat", "cli")
        self.add("local-target", "Reviewer", "tui")
        self.add("tg-private", "Sam DM", "telegram",
                 origin={"platform": "telegram", "chat_id": OWNER, "chat_type": "dm", "user_id": OWNER})
        self.add("tg-shared", "Team group", "telegram",
                 origin={"platform": "telegram", "chat_id": OTHER_GROUP, "chat_type": "group"})
        self.add("mx-private", "Matrix room", "matrix",
                 origin={"platform": "matrix", "chat_id": ROOM, "chat_type": "group"})

    def deliver(self, target, text, delivery, submission_id):
        self.submitted.append(text)
        return {"ok": True, "applied": delivery, "applied_detail": None, "admission_id": None}

    @staticmethod
    def lister(scope="live", picker=False):
        return {"ok": True, "sessions": []}

    def add(self, session_id, title, source, *, origin=None):
        self.db.create_session(session_id, source=source)
        self.db._write_sql("UPDATE sessions SET started_at = ? WHERE id = ?", (self.started, session_id))
        self.started += 1
        if title:
            self.db.set_session_title(session_id, title)
        if origin is not None:
            self.db._write_sql("UPDATE sessions SET origin_json = ? WHERE id = ?",
                               (json.dumps(origin), session_id))

    def send(self, to="tg-private", message="Is 22:00 still ok?", **kwargs):
        """A real framed message through route.send. Returns ``(message_id, submitted text)``.

        The raw echo ``frame.build`` returned is kept in ``self.echoes``.
        """
        build = frame.build
        built = {}

        def capture(*args, **kw):
            built["out"] = build(*args, **kw)
            return built["out"]

        with mock.patch.object(route.frame, "build", side_effect=capture):
            receipt = route.send("local-sender", to, message, ctx=self.ctx, reply_mode="none",
                                 lister=self.lister, **kwargs)
        self.assertTrue(receipt["ok"], receipt)
        self.echoes[receipt["message_id"]] = built["out"][1]
        return receipt["message_id"], self.submitted[-1]

    def join_posts(self):
        for thread in threading.enumerate():
            if thread.name.startswith("intercom-echo-"):
                thread.join(10)

    def pre_llm(self, prompt, platform="telegram"):
        result = echo.on_pre_llm_call(session_id="tg-private", user_message=prompt,
                                      conversation_history=[{"role": "user", "content": prompt}],
                                      platform=platform, model="m")
        self.join_posts()
        return result

    def pre_api(self, request_messages, platform="telegram", retry_count=0):
        result = echo.on_pre_api_request(session_id="tg-private", user_message="Sam's task",
                                         request_messages=request_messages, platform=platform,
                                         retry_count=retry_count, model="m")
        self.join_posts()
        return result

    @staticmethod
    def steered(steer_text, *, then_tool=False):
        rows = [
            {"role": "system", "content": "system"},
            {"role": "user", "content": "Sam's task"},
            {"role": "assistant", "content": "", "tool_calls": [{"id": "t1"}]},
            {"role": "tool", "tool_call_id": "t1", "content": "result"},
            {"role": "user", "content": f"[steer]\n{steer_text}\n[/steer]", "display_kind": "steer"},
        ]
        if then_tool:
            rows += [{"role": "assistant", "content": "", "tool_calls": [{"id": "t2"}]},
                     {"role": "tool", "tool_call_id": "t2", "content": "result"}]
        return rows


class HookSelectionTests(EchoTestBase):
    def test_register_adds_pre_llm_call_and_pre_api_request(self):
        ctx = FakeCtx()
        with mock.patch.dict(echo._ctx, {"ctx": None}):
            echo.register(ctx)
            self.assertIs(echo._ctx["ctx"], ctx)
        self.assertEqual(ctx.hooks, [("pre_llm_call", echo.on_pre_llm_call),
                                     ("pre_api_request", echo.on_pre_api_request)])

    def test_queued_or_started_message_is_echoed_by_pre_llm_call(self):
        message_id, text = self.send(delivery="queue")
        self.assertIsNone(self.pre_llm(text))
        self.assertEqual([p["text"] for p in self.ctx.posts],
                         [frame.escape_echo(self.echoes[message_id], "telegram")])

    def test_steered_message_is_echoed_by_pre_api_request(self):
        message_id, text = self.send()
        self.assertIsNone(self.pre_api(self.steered(text)))
        self.assertEqual([p["text"] for p in self.ctx.posts],
                         [frame.escape_echo(self.echoes[message_id], "telegram")])

    def test_steer_before_the_final_answer_request_is_echoed(self):
        # The post-tool drain: the steer row is the last row of the request that writes the answer.
        message_id, text = self.send()
        rows = self.steered(text)
        rows.insert(4, {"role": "tool", "tool_call_id": "t0", "content": "another result"})
        self.pre_api(rows)
        self.assertEqual(len(self.ctx.posts), 1)
        self.assertIsNotNone(record.get(message_id)["echoed_at"])

    def test_late_steer_is_echoed_by_the_later_turn(self):
        message_id, text = self.send()
        # The steer missed the last request: no request ever carried it in this turn.
        self.pre_api([{"role": "user", "content": "Sam's task"}])
        self.assertEqual(self.ctx.posts, [])
        # The finalizer hands it back; it becomes the next turn's prompt.
        self.pre_llm(f"[steer]\n{text}\n[/steer]")
        self.assertEqual([p["text"] for p in self.ctx.posts],
                         [frame.escape_echo(self.echoes[message_id], "telegram")])

    def test_only_user_rows_after_the_newest_assistant_row_are_read(self):
        _message_id, text = self.send()
        rows = self.steered(text, then_tool=False)
        rows.append({"role": "assistant", "content": "answer so far"})
        rows.append({"role": "user", "content": "Sam's next words"})
        self.pre_api(rows)
        self.assertEqual(self.ctx.posts, [])

    def test_tool_and_system_rows_are_not_read(self):
        _message_id, text = self.send()
        rows = [{"role": "system", "content": text},
                {"role": "assistant", "content": ""},
                {"role": "tool", "tool_call_id": "t1", "content": text}]
        self.pre_api(rows)
        self.assertEqual(self.ctx.posts, [])

    def test_new_user_rows_reads_list_content(self):
        rows = [{"role": "user", "content": "old"},
                {"role": "assistant", "content": "a"},
                {"role": "user", "content": [{"type": "input_text", "text": "one"}]},
                {"role": "tool", "content": "skip"},
                {"role": "user", "content": "two"}]
        self.assertEqual(echo.new_user_rows(rows), ["one", "two"])
        self.assertEqual(echo.new_user_rows(None), [])


class ClaimTests(EchoTestBase):
    def test_retries_and_later_requests_never_post_twice(self):
        message_id, text = self.send()
        rows = self.steered(text)
        self.pre_api(rows)
        self.pre_api(rows, retry_count=1)
        self.pre_api([*rows, {"role": "user", "content": "more"}])
        self.pre_llm(text)
        self.assertEqual(len(self.ctx.posts), 1)
        self.assertIsNotNone(record.get(message_id)["echoed_at"])

    def test_pre_llm_call_then_first_request_post_once(self):
        _message_id, text = self.send(delivery="queue")
        self.pre_llm(text)
        self.pre_api([{"role": "system", "content": "s"}, {"role": "user", "content": text}])
        self.assertEqual(len(self.ctx.posts), 1)

    def test_concurrent_hooks_claim_once(self):
        _message_id, text = self.send()
        rows = self.steered(text)
        barrier = threading.Barrier(4)

        def fire():
            barrier.wait(5)
            echo.on_pre_api_request(request_messages=rows, platform="telegram")

        threads = [threading.Thread(target=fire) for _ in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(10)
        self.join_posts()
        self.assertEqual(len(self.ctx.posts), 1)

    def test_two_messages_in_one_request_post_each_once(self):
        first, text1 = self.send(message="first")
        second, text2 = self.send(message="second")
        rows = [{"role": "assistant", "content": ""},
                {"role": "user", "content": text1}, {"role": "user", "content": text2}]
        self.pre_api(rows)
        self.pre_api(rows)
        self.assertEqual(sorted(p["text"] for p in self.ctx.posts),
                         sorted(frame.escape_echo(self.echoes[m], "telegram") for m in (first, second)))


class PostDecisionTests(EchoTestBase):
    def test_shared_chat_never_gets_a_post(self):
        message_id, text = self.send(to="tg-shared")
        self.assertEqual(record.get(message_id)["target_chat_scope"], "shared")
        self.pre_llm(text)
        self.pre_api(self.steered(text))
        self.assertEqual(self.ctx.posts, [])
        self.assertIsNone(record.get(message_id)["echoed_at"])

    def test_listed_group_is_private(self):
        self.add("tg-group", "Ops group", "telegram",
                 origin={"platform": "telegram", "chat_id": GROUP, "chat_type": "group"})
        message_id, text = self.send(to="tg-group")
        self.pre_llm(text)
        self.assertEqual([p["text"] for p in self.ctx.posts],
                         [frame.escape_echo(self.echoes[message_id], "telegram")])

    def test_local_target_never_gets_a_post(self):
        message_id, text = self.send(to="local-target")
        self.assertIsNone(record.get(message_id)["target_chat_scope"])
        self.pre_llm(text, platform="tui")
        self.assertEqual(self.ctx.posts, [])
        self.assertIsNone(record.get(message_id)["echoed_at"])

    def test_typed_lookalike_or_changed_span_never_posts(self):
        message_id, text = self.send()
        self.pre_llm(text.replace("22:00", "23:00"))
        lookalike = (f"{MARK} Not Sam · no reply needed · hermes@Sender Chat says "
                     f"(session local-sender · message {message_id}):\nhi\n{MARK} End of message {message_id}")
        self.pre_llm(lookalike)
        self.pre_api([{"role": "user", "content": lookalike}])
        self.assertEqual(self.ctx.posts, [])
        self.assertIsNone(record.get(message_id)["echoed_at"])

    def test_text_without_a_frame_reads_no_record(self):
        with mock.patch.object(echo.replies, "find_messages") as find:
            self.pre_llm("Sam's own words")
            self.pre_api([{"role": "user", "content": "plain"}])
        find.assert_not_called()
        self.assertEqual(self.ctx.posts, [])

    def test_the_echo_is_the_built_echo_escaped_for_the_renderer(self):
        body = "Ping @everyone and <@U123>, see [docs](https://example.org)."
        message_id, text = self.send(message=body)
        self.pre_llm(text)
        posted = self.ctx.posts[0]["text"]
        self.assertEqual(posted, frame.escape_echo(self.echoes[message_id], "telegram"))
        self.assertTrue(posted.startswith(f"{MARK} Not Sam"))
        self.assertNotIn("@everyone", posted)
        self.assertNotIn("<@U123>", posted)
        self.assertNotIn("](", posted)
        self.assertNotIn("How to answer", posted)
        self.assertNotIn(f"End of message {message_id}", posted)

    def test_renderer_is_the_hook_platform(self):
        message_id, text = self.send(message="<b>bold</b> @here")
        self.pre_llm(text, platform="discord")
        self.assertEqual([p["text"] for p in self.ctx.posts],
                         [frame.escape_echo(self.echoes[message_id], "discord")])


class MissingEscaperTests(EchoTestBase):
    def test_platform_missing_from_escapers_gets_no_post_and_one_warning(self):
        self.assertNotIn("matrix", frame.ECHO_ESCAPERS)
        message_id, text = self.send(to="mx-private")
        self.assertEqual(record.get(message_id)["target_chat_scope"], "private")
        with self.assertLogs(echo.logger, level="WARNING") as logs:
            self.pre_llm(text, platform="matrix")
            self.pre_api(self.steered(text), platform="matrix")
            self.pre_api(self.steered(text), platform="matrix", retry_count=1)
            echo.logger.warning("sentinel")
        warnings = [line for line in logs.output if "no echo escaper" in line]
        self.assertEqual(len(warnings), 1)
        self.assertIn("no echo escaper for matrix", warnings[0])
        self.assertEqual(self.ctx.posts, [])


class DeliveryNeverAffectedTests(EchoTestBase):
    def test_hook_returns_before_the_post_finishes(self):
        _message_id, text = self.send()
        self.ctx.gate = threading.Event()
        try:
            self.assertIsNone(echo.on_pre_api_request(request_messages=self.steered(text),
                                                      platform="telegram"))
            self.assertEqual(self.ctx.posts, [])
        finally:
            self.ctx.gate.set()
        self.join_posts()
        self.assertEqual(len(self.ctx.posts), 1)

    def test_post_runs_on_a_fresh_thread_with_the_turn_context(self):
        _message_id, text = self.send()
        token = _probe.set("turn-context")
        try:
            echo.on_pre_llm_call(user_message=text, platform="telegram")
        finally:
            _probe.reset(token)
        self.join_posts()
        post = self.ctx.posts[0]
        self.assertIsNot(post["thread"], threading.current_thread())
        self.assertEqual(post["probe"], "turn-context")

    def test_refused_post_is_logged_only(self):
        message_id, text = self.send(message="secret body words")
        self.ctx.result = SimpleNamespace(ok=False, error="no_conversation")
        with self.assertLogs(echo.logger, level="WARNING") as logs:
            self.assertIsNone(self.pre_llm(text))
        self.assertTrue(any("refused (no_conversation)" in line for line in logs.output))
        self.assertFalse(any("secret body words" in line for line in logs.output))
        self.assertIsNotNone(record.get(message_id)["echoed_at"])
        self.pre_llm(text)
        self.assertEqual(len(self.ctx.posts), 1)  # nothing retries

    def test_failed_post_is_logged_only(self):
        _message_id, text = self.send(message="secret body words")
        self.ctx.raises = RuntimeError("secret body words")
        with self.assertLogs(echo.logger, level="WARNING") as logs:
            self.assertIsNone(self.pre_llm(text))
        self.assertTrue(any("failed (RuntimeError)" in line for line in logs.output))
        self.assertFalse(any("secret body words" in line for line in logs.output))

    def test_record_error_never_raises_from_the_hook(self):
        _message_id, text = self.send()
        with mock.patch.object(echo.record, "claim_echo", side_effect=OSError("disk")), \
                self.assertLogs(echo.logger, level="WARNING"):
            self.assertIsNone(self.pre_llm(text))
        self.assertEqual(self.ctx.posts, [])


if __name__ == "__main__":
    unittest.main()
