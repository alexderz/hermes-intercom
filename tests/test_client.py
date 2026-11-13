from __future__ import annotations

import asyncio
import contextvars
import importlib.util
import logging
import os
import sys
import threading
import unittest
from contextlib import asynccontextmanager
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest import mock


PACKAGE_DIR = Path(__file__).resolve().parents[1]
MODULE_NAME = "hermes_intercom_client_test"

# Planted in every handle field the client must not read, and in every exception text.
LEAK = "LEAKMARK"
ENDPOINT = SimpleNamespace(api_origin=f"http://127.0.0.1:9/{LEAK}", instance_id=LEAK)
HOME = contextvars.ContextVar("fake_hermes_home", default="/nonexistent/default-home")


class GatewayClientError(ValueError):
    pass


class GatewayRPCError(GatewayClientError):
    """The real class's shape: str(exc) is the owner's bounded reason code."""

    def __init__(self, reason, code=None, data=None):
        super().__init__(reason)
        self.code = code
        self.data = data or {}


def handle(running, generation=7):
    """A full resume answer: everything but ``running`` / ``execution_generation`` carries LEAK."""
    return {
        "session_id": "s1", "stored_session_id": LEAK, "messages": [{"role": "user", "content": LEAK}],
        "messages_omitted": True, "message_count": 3, "running": running, "authority_epoch": 1,
        "replay_epoch": LEAK, "last_sequence": 9, "subscription_id": LEAK, "revision": 2,
        "execution_generation": generation, "pending": [{"admission_id": LEAK, "text": LEAK}],
        "prompts": [{"text": LEAK}], "info": {"title": LEAK, "cwd": f"/home/{LEAK}"},
        "todo_state": {"items": [LEAK]}, "open_requests": [{"id": LEAK}],
    }


def submitted(admission_id="adm-1"):
    return {"admission_id": admission_id, "ref": {"profile_id": LEAK, "session_id": "s1"}, "sequence": 1,
            "status": "queued", "outcome": None, "authority_epoch": 1, "execution_generation": None}


def ack(status, generation=7):
    return {"status": status, "text": LEAK, "execution_generation": generation, "authority_epoch": 1}


class FakeOwner:
    """The owner's verbs: each method answers from a queue of results or exceptions."""

    def __init__(self, **script):
        self.script = {method.replace("_", "."): list(answers) for method, answers in script.items()}
        self.calls: list[tuple[str, dict]] = []
        self.endpoints: list = []
        self.connect_error: BaseException | None = None
        self.seen: list[dict] = []

    async def rpc(self, method, **params):
        self.calls.append((method, params))
        self.seen.append({"thread": threading.get_ident(), "home": HOME.get(),
                          "loop": asyncio.get_running_loop()})
        answers = self.script.get(method)
        if not answers:
            raise AssertionError(f"unexpected call {method}")
        answer = answers.pop(0)
        if isinstance(answer, BaseException):
            raise answer
        return answer

    @asynccontextmanager
    async def connect(self, endpoint=None, subject=None, **_kw):
        self.endpoints.append(endpoint)
        if self.connect_error is not None:
            raise self.connect_error
        yield self

    def methods(self):
        return [method for method, _ in self.calls]


def load_client():
    spec = importlib.util.spec_from_file_location(MODULE_NAME, PACKAGE_DIR / "client.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class RecordingEnviron(dict):
    """``os.environ`` stand-in that records every key read."""

    def __init__(self, *args):
        super().__init__(*args)
        self.read: set[str] = set()

    def __getitem__(self, key):
        self.read.add(key)
        return super().__getitem__(key)

    def get(self, key, default=None):
        self.read.add(key)
        return super().get(key, default)

    def __contains__(self, key):
        self.read.add(key)
        return super().__contains__(key)


class ClientTests(unittest.TestCase):
    def setUp(self):
        self.owner = FakeOwner()
        self.discovery = SimpleNamespace(state="ready", endpoint=ENDPOINT)
        self.discovered: list[tuple] = []

        def discover(home, *, timeout=2.0):
            self.discovered.append((home, timeout))
            if isinstance(self.discovery, BaseException):
                raise self.discovery
            return self.discovery

        constants = ModuleType("hermes_constants")
        constants.get_hermes_home = lambda: HOME.get()
        runtime = ModuleType("hermes_cli.gateway_runtime")
        runtime.discover_gateway_endpoint = discover
        gateway_client = ModuleType("hermes_cli.gateway_client")
        gateway_client.GatewayClientError = GatewayClientError
        gateway_client.GatewayRPCError = GatewayRPCError
        gateway_client.connect_gateway = lambda *a, **kw: self.owner.connect(*a, **kw)
        package = ModuleType("hermes_cli")
        package.__path__ = []
        modules = mock.patch.dict(sys.modules, {
            "hermes_constants": constants, "hermes_cli": package,
            "hermes_cli.gateway_runtime": runtime, "hermes_cli.gateway_client": gateway_client,
        })
        modules.start()
        self.addCleanup(modules.stop)
        self.client = load_client()

    def script(self, **answers):
        self.owner.script = {method.replace("_", "."): list(a) for method, a in answers.items()}

    def deliver(self, delivery="steer", text="📡 hello", sub="intercom-m1"):
        return self.client.deliver_sync("s1", text, delivery, sub)

    def assert_clean(self, result):
        self.assertNotIn(LEAK, repr(result))
        self.assertNotIn("127.0.0.1", repr(result))
        self.assertNotIn("ticket", repr(result).lower())

    # ── mode table ──

    def test_idle_target_starts_a_turn_in_every_mode(self):
        for delivery in ("interrupt", "steer", "queue"):
            with self.subTest(delivery=delivery):
                self.owner = FakeOwner(session_resume=[handle(False)], prompt_submit=[submitted("adm-start")])
                result = self.deliver(delivery)
                self.assertEqual(result, {"ok": True, "applied": "start", "applied_detail": None,
                                          "admission_id": "adm-start"})
                self.assertEqual(self.owner.calls, [
                    ("session.resume", {"session_id": "s1", "omit_messages": True}),
                    ("prompt.submit", {"session_id": "s1", "text": "📡 hello", "submission_id": "intercom-m1"}),
                ])

    def test_queue_on_running_target_is_a_queued_submit(self):
        self.script(session_resume=[handle(True)], prompt_submit=[submitted("adm-q")])
        result = self.deliver("queue")
        self.assertEqual(result, {"ok": True, "applied": "queue", "applied_detail": None, "admission_id": "adm-q"})
        self.assertEqual(self.owner.calls[1], ("prompt.submit", {
            "session_id": "s1", "text": "📡 hello", "submission_id": "intercom-m1", "queued": True}))

    def test_steer_taken_by_session_steer(self):
        self.script(session_resume=[handle(True, 11)], session_steer=[ack("queued", 11)])
        result = self.deliver("steer")
        self.assertEqual(result, {"ok": True, "applied": "steer", "applied_detail": None, "admission_id": None})
        self.assertEqual(self.owner.calls[1], ("session.steer", {
            "session_id": "s1", "text": "📡 hello", "execution_generation": 11}))
        self.assertEqual(self.owner.methods(), ["session.resume", "session.steer"])

    def test_steer_rejected_falls_back_to_queued_submit(self):
        self.script(session_resume=[handle(True)], session_steer=[ack("rejected")], prompt_submit=[submitted("adm-sq")])
        result = self.deliver("steer")
        self.assertEqual(result, {"ok": True, "applied": "queue", "applied_detail": None, "admission_id": "adm-sq"})
        self.assertEqual(self.owner.methods(), ["session.resume", "session.steer", "prompt.submit"])
        self.assertIs(self.owner.calls[2][1]["queued"], True)

    def test_interrupt_taken_by_redirect(self):
        self.script(session_resume=[handle(True, 4)], session_redirect=[ack("redirected", 4)])
        result = self.deliver("interrupt")
        self.assertEqual(result, {"ok": True, "applied": "interrupt", "applied_detail": "redirect",
                                  "admission_id": None})
        self.assertEqual(self.owner.calls[1], ("session.redirect", {
            "session_id": "s1", "text": "📡 hello", "execution_generation": 4}))

    def test_interrupt_redirect_rejected_is_hard_interrupt_then_queued_submit(self):
        self.script(session_resume=[handle(True, 4)], session_redirect=[ack("rejected", 4)],
                    session_interrupt=[{"execution_state": "idle", "ref": {"session_id": LEAK}}],
                    prompt_submit=[submitted("adm-hard")])
        result = self.deliver("interrupt")
        self.assertEqual(result, {"ok": True, "applied": "interrupt", "applied_detail": "hard-interrupt",
                                  "admission_id": "adm-hard"})
        self.assertEqual(self.owner.calls[1:], [
            ("session.redirect", {"session_id": "s1", "text": "📡 hello", "execution_generation": 4}),
            ("session.interrupt", {"session_id": "s1", "execution_generation": 4}),
            ("prompt.submit", {"session_id": "s1", "text": "📡 hello", "submission_id": "intercom-m1",
                               "queued": True}),
        ])

    def test_stale_generation_resumes_once_more_then_idle_row(self):
        for delivery, verb in (("steer", "session_steer"), ("interrupt", "session_redirect")):
            with self.subTest(delivery=delivery):
                self.owner = FakeOwner(session_resume=[handle(True, 4), handle(False, 5)],
                                       prompt_submit=[submitted("adm-stale")],
                                       **{verb: [GatewayRPCError("stale_generation")]})
                result = self.deliver(delivery)
                self.assertEqual(result, {"ok": True, "applied": "start", "applied_detail": None,
                                          "admission_id": "adm-stale"})
                self.assertEqual(self.owner.methods(),
                                 ["session.resume", verb.replace("_", "."), "session.resume", "prompt.submit"])
                self.assertEqual(self.owner.calls[2][1], {"session_id": "s1", "omit_messages": True})
                self.assertNotIn("queued", self.owner.calls[3][1])

    def test_stale_generation_on_hard_interrupt_takes_the_idle_row(self):
        self.script(session_resume=[handle(True), handle(False)], session_redirect=[ack("rejected")],
                    session_interrupt=[GatewayRPCError("stale_generation")], prompt_submit=[submitted("adm-x")])
        result = self.deliver("interrupt")
        self.assertEqual(result["applied"], "start")
        self.assertEqual(result["admission_id"], "adm-x")
        self.assertEqual(self.owner.methods(), ["session.resume", "session.redirect", "session.interrupt",
                                                "session.resume", "prompt.submit"])

    def test_unknown_delivery_is_a_caller_error(self):
        with self.assertRaises(ValueError):
            self.deliver("whisper")
        self.assertEqual(self.owner.calls, [])
        self.assertEqual(self.discovered, [])

    # ── idempotence ──

    def test_submission_id_is_per_message_and_passed_unchanged(self):
        self.assertEqual(self.client.submission_id("3f2a9c"), "intercom-3f2a9c")
        self.script(session_resume=[handle(False)], prompt_submit=[submitted()])
        self.deliver("queue", sub=self.client.submission_id("3f2a9c"))
        self.assertEqual(self.owner.calls[-1][1]["submission_id"], "intercom-3f2a9c")

    def test_one_submit_per_delivery_and_nothing_resent_after_a_failure(self):
        for answer in (GatewayClientError("gone"), TimeoutError(), GatewayRPCError("session_busy")):
            with self.subTest(answer=type(answer).__name__):
                self.owner = FakeOwner(session_resume=[handle(True)], session_steer=[ack("rejected")],
                                       prompt_submit=[answer, submitted()])
                result = self.deliver("steer")
                self.assertFalse(result["ok"])
                self.assertEqual(self.owner.methods(), ["session.resume", "session.steer", "prompt.submit"])
                self.assertEqual(len(self.owner.endpoints), 1)

    # ── discovery and the endpoint ──

    def test_discovery_uses_the_calling_profile_home_with_two_seconds(self):
        self.script(session_resume=[handle(False)], prompt_submit=[submitted()])
        token = HOME.set("/profiles/caller-home")
        try:
            self.deliver()
        finally:
            HOME.reset(token)
        self.assertEqual(self.discovered, [("/profiles/caller-home", 2.0)])

    def test_no_endpoint_is_no_owner_and_never_connects(self):
        for state in ("absent", "inaccessible", "conflict", "ready"):
            with self.subTest(state=state):
                self.owner = FakeOwner()
                self.discovery = SimpleNamespace(state=state, endpoint=None)
                result = self.deliver()
                self.assertEqual((result["error"], result["exit"]), ("no-owner", 5))
                self.assertEqual(self.owner.endpoints, [])

    def test_starting_or_draining_is_owner_restarting_and_never_connects(self):
        for state, endpoint in (("starting", None), ("draining", None), ("draining", ENDPOINT)):
            with self.subTest(state=state, endpoint=endpoint is not None):
                self.owner = FakeOwner()
                self.discovery = SimpleNamespace(state=state, endpoint=endpoint, reason_code="restart_wait")
                result = self.deliver()
                self.assertEqual((result["error"], result["exit"]), ("owner-restarting", 5))
                self.assertEqual(self.owner.endpoints, [])

    def test_discovery_failure_is_not_reachable(self):
        self.discovery = OSError(f"ws://127.0.0.1:9/{LEAK}")
        result = self.deliver()
        self.assertEqual(result["error"], "not-reachable")
        self.assertEqual(self.owner.endpoints, [])
        self.assert_clean(result)

    def test_no_failure_selects_the_legacy_path(self):
        outcomes = []
        for discovery in (SimpleNamespace(state="absent", endpoint=None),
                          SimpleNamespace(state="starting", endpoint=None),
                          SimpleNamespace(state="draining", endpoint=None)):
            self.discovery = discovery
            outcomes.append(self.deliver())
        self.assertEqual([o["error"] for o in outcomes], ["no-owner", "owner-restarting", "owner-restarting"])
        source = (PACKAGE_DIR / "client.py").read_text(encoding="utf-8-sig")
        self.assertNotIn("inbox", source)

    def test_connect_gateway_only_ever_gets_the_discovered_endpoint(self):
        runs = [
            dict(session_resume=[handle(False)], prompt_submit=[submitted()]),
            dict(session_resume=[handle(True)], session_steer=[ack("rejected")], prompt_submit=[submitted()]),
            dict(session_resume=[handle(True)], session_redirect=[ack("rejected")], session_interrupt=[{}],
                 prompt_submit=[submitted()]),
            dict(session_resume=[GatewayRPCError("permission_denied")]),
        ]
        endpoints = []
        for script in runs:
            self.owner = FakeOwner(**script)
            self.deliver("interrupt" if "session_redirect" in script else "steer")
            endpoints += self.owner.endpoints
        self.owner = FakeOwner(session_list=[{"sessions": []}], prompt_receipt=[submitted()])
        self.client.list_sessions()
        self.client.receipt("adm-1", session_id="s1")
        endpoints += self.owner.endpoints
        self.assertEqual(len(endpoints), 6)
        for endpoint in endpoints:
            self.assertIs(endpoint, ENDPOINT)

    def test_hermes_tui_gateway_url_is_never_read(self):
        environ = RecordingEnviron(os.environ)
        environ["HERMES_TUI_GATEWAY_URL"] = f"ws://example.invalid/{LEAK}"
        self.script(session_resume=[handle(True)], session_steer=[ack("queued")])
        with mock.patch("os.environ", environ):
            self.deliver()
            self.discovery = SimpleNamespace(state="absent", endpoint=None)
            self.deliver()
        self.assertNotIn("HERMES_TUI_GATEWAY_URL", environ.read)
        source = (PACKAGE_DIR / "client.py").read_text(encoding="utf-8-sig")
        code = "\n".join(line for line in source.splitlines() if not line.lstrip().startswith(("#", "-", "``")))
        self.assertNotIn("environ", code)
        self.assertNotIn("getenv", code)

    # ── thread ──

    def test_runs_asyncio_on_a_fresh_thread_with_the_callers_context(self):
        self.script(session_resume=[handle(False)], prompt_submit=[submitted()])
        token = HOME.set("/profiles/bound")
        try:
            self.deliver()
        finally:
            HOME.reset(token)
        self.assertEqual({s["home"] for s in self.owner.seen}, {"/profiles/bound"})
        self.assertNotIn(threading.get_ident(), {s["thread"] for s in self.owner.seen})

    def test_callable_from_inside_a_running_event_loop(self):
        self.script(session_resume=[handle(False)], prompt_submit=[submitted("adm-loop")])
        outer = {}

        async def caller():
            outer["loop"] = asyncio.get_running_loop()
            return self.deliver()

        result = asyncio.run(caller())
        self.assertEqual(result["admission_id"], "adm-loop")
        self.assertIsNot(self.owner.seen[0]["loop"], outer["loop"])

    def test_uses_copy_context_run_on_a_new_thread(self):
        self.script(session_resume=[handle(False)], prompt_submit=[submitted()])
        started = []
        real_thread = threading.Thread

        def spy(*args, **kwargs):
            started.append(kwargs.get("target"))
            return real_thread(*args, **kwargs)

        with mock.patch.object(self.client.threading, "Thread", side_effect=spy):
            self.deliver()
        self.assertEqual(len(started), 1)
        self.assertEqual(getattr(started[0], "__name__", None), "run")
        self.assertIsInstance(getattr(started[0], "__self__", None), contextvars.Context)

    # ── resume: only running and execution_generation are kept ──

    def test_resume_omits_messages_and_no_handle_field_escapes(self):
        cases = [
            ("steer", dict(session_resume=[handle(True)], session_steer=[ack("queued")])),
            ("steer", dict(session_resume=[handle(True)], session_steer=[ack("rejected")],
                           prompt_submit=[submitted()])),
            ("interrupt", dict(session_resume=[handle(True)], session_redirect=[ack("redirected")])),
            ("interrupt", dict(session_resume=[handle(True)], session_redirect=[ack("rejected")],
                               session_interrupt=[handle(False)], prompt_submit=[submitted()])),
            ("queue", dict(session_resume=[handle(False)], prompt_submit=[submitted()])),
            ("steer", dict(session_resume=[handle(True)], session_steer=[ack("odd")])),
            ("steer", dict(session_resume=[handle(True)], session_steer=[GatewayClientError(LEAK)])),
            ("queue", dict(session_resume=[handle(True)], prompt_submit=[GatewayRPCError("invalid_params",
                                                                                      data={"x": LEAK})])),
        ]
        records = []

        class Capture(logging.Handler):
            def emit(self, record):
                records.append(record.getMessage())

        root = logging.getLogger()
        capture = Capture(level=logging.DEBUG)
        old_level = root.level
        root.addHandler(capture)
        root.setLevel(logging.DEBUG)
        try:
            for delivery, script in cases:
                with self.subTest(delivery=delivery, script=sorted(script)):
                    self.owner = FakeOwner(**script)
                    result = self.deliver(delivery)
                    self.assert_clean(result)
                    resumes = [params for method, params in self.owner.calls if method == "session.resume"]
                    self.assertEqual(resumes, [{"session_id": "s1", "omit_messages": True}])
        finally:
            root.removeHandler(capture)
            root.setLevel(old_level)
        self.assertFalse([r for r in records if LEAK in r])

    def test_every_delivery_starts_with_resume(self):
        for delivery in ("interrupt", "steer", "queue"):
            for running in (False, True):
                with self.subTest(delivery=delivery, running=running):
                    self.owner = FakeOwner(session_resume=[handle(running)], prompt_submit=[submitted()],
                                           session_steer=[ack("queued")], session_redirect=[ack("redirected")])
                    self.deliver(delivery)
                    self.assertEqual(self.owner.methods()[0], "session.resume")

    # ── error mapping ──

    def test_runtime_coordination_required_is_not_addressable(self):
        for step in ("session_resume", "prompt_submit"):
            with self.subTest(step=step):
                script = dict(session_resume=[handle(False)], prompt_submit=[submitted()])
                script[step] = [GatewayRPCError("runtime_coordination_required")]
                self.owner = FakeOwner(**script)
                result = self.deliver()
                self.assertEqual(result, {"ok": False, "error": "not-addressable",
                                          "code": "runtime_coordination_required", "exit": 4,
                                          "message": ERROR_SENTENCES["not-addressable"] + " (GatewayRPCError)"})

    def test_owner_refusals_are_refused_by_owner_with_the_code_kept(self):
        for code in ("permission_denied", "unsupported_control", "invalid_params"):
            for delivery, script in (
                ("steer", dict(session_resume=[GatewayRPCError(code)])),
                ("steer", dict(session_resume=[handle(True)], session_steer=[GatewayRPCError(code)])),
                ("interrupt", dict(session_resume=[handle(True)], session_redirect=[GatewayRPCError(code)])),
                ("queue", dict(session_resume=[handle(True)], prompt_submit=[GatewayRPCError(code)])),
            ):
                with self.subTest(code=code, step=list(script)[-1]):
                    self.owner = FakeOwner(**script)
                    result = self.deliver(delivery)
                    self.assertEqual((result["ok"], result["error"], result["code"], result["exit"]),
                                     (False, "refused-by-owner", code, 4))
                    self.assertEqual(result["message"], "The gateway refused the call. (GatewayRPCError)")

    def test_not_sent_is_not_reachable(self):
        connect_failure = GatewayClientError(f"Gateway connection failed ws://127.0.0.1:9/api/ws ticket={LEAK}")
        cases = [
            ("connect", None, connect_failure),
            ("resume", dict(session_resume=[GatewayClientError(f"ws://127.0.0.1:9 {LEAK}")]), None),
            ("resume timeout", dict(session_resume=[TimeoutError(LEAK)]), None),
            ("after rejected redirect, hard interrupt lost",
             dict(session_resume=[handle(True)], session_redirect=[ack("rejected")],
                  session_interrupt=[GatewayClientError(LEAK)]), None),
            ("after stale redirect, second resume lost",
             dict(session_resume=[handle(True), GatewayClientError(LEAK)],
                  session_redirect=[GatewayRPCError("stale_generation")]), None),
        ]
        for name, script, connect_error in cases:
            with self.subTest(name):
                self.owner = FakeOwner(**(script or {}))
                self.owner.connect_error = connect_error
                result = self.deliver("interrupt")
                self.assertEqual((result["error"], result["exit"]), ("not-reachable", 5))
                self.assertIsNone(result["code"])
                self.assertRegex(result["message"], r"^The gateway did not answer before the message was handed "
                                                    r"over\. \((GatewayClientError|TimeoutError)\)$")
                self.assert_clean(result)
                self.assertNotIn("prompt.submit", self.owner.methods())

    def test_request_the_gateway_client_never_wrote_is_not_reachable(self):
        cases = [
            ("submit", "queue", dict(session_resume=[handle(False)], prompt_submit=[GatewayClientError(NOT_SENT)])),
            ("steer", "steer", dict(session_resume=[handle(True)], session_steer=[GatewayClientError(NOT_SENT)])),
            ("redirect", "interrupt",
             dict(session_resume=[handle(True)], session_redirect=[GatewayClientError(NOT_SENT)])),
            ("hard-interrupt submit", "interrupt",
             dict(session_resume=[handle(True)], session_redirect=[ack("rejected")], session_interrupt=[{}],
                  prompt_submit=[GatewayClientError(NOT_SENT)])),
        ]
        for name, delivery, script in cases:
            with self.subTest(name):
                self.owner = FakeOwner(**script)
                result = self.deliver(delivery)
                self.assertEqual((result["error"], result["exit"]), ("not-reachable", 5))
                self.assertEqual(result["message"], ERROR_SENTENCES["not-reachable"] + " (GatewayClientError)")
                self.assertNotIn("nothing was sent", result["message"])

    def test_sent_without_answer_is_delivery_unknown(self):
        lost = GatewayClientError(f"Gateway disconnected; turn outcome is unknown. {LEAK}")
        cases = [
            ("submit lost", "queue", dict(session_resume=[handle(False)], prompt_submit=[lost])),
            ("steer timeout", "steer", dict(session_resume=[handle(True)], session_steer=[TimeoutError()])),
            ("redirect lost", "interrupt", dict(session_resume=[handle(True)], session_redirect=[lost])),
            ("fallback submit lost", "steer",
             dict(session_resume=[handle(True)], session_steer=[ack("rejected")], prompt_submit=[lost])),
            ("hard-interrupt submit lost", "interrupt",
             dict(session_resume=[handle(True)], session_redirect=[ack("rejected")], session_interrupt=[{}],
                  prompt_submit=[OSError(LEAK)])),
        ]
        for name, delivery, script in cases:
            with self.subTest(name):
                self.owner = FakeOwner(**script)
                result = self.deliver(delivery)
                self.assertEqual((result["error"], result["exit"]), ("delivery-unknown", 6))
                self.assertTrue(result["message"].startswith(ERROR_SENTENCES["delivery-unknown"] + " ("))
                self.assert_clean(result)

    def test_unreadable_control_answer_is_delivery_unknown(self):
        self.script(session_resume=[handle(True)], session_steer=[{"voice_stopped": True}])
        result = self.deliver("steer")
        self.assertEqual(result["error"], "delivery-unknown")
        self.assertEqual(self.owner.methods(), ["session.resume", "session.steer"])

    def test_error_texts_are_fixed_sentences_without_url_or_ticket(self):
        for detail, (exit_code, sentence) in self.client.ERRORS.items():
            with self.subTest(detail=detail):
                self.assertEqual(sentence, ERROR_SENTENCES[detail])
                self.assertIn(exit_code, (4, 5, 6))
                self.assertNotRegex(sentence, r"(?i)https?:|wss?:|ticket|127\.0\.0\.1|/api/")

    # ── reads ──

    def test_list_sessions_wraps_session_list_read_only(self):
        rows = [{"session_id": "s1", "id": "s1", "title": "Home", "running": False}]
        self.script(session_list=[{"sessions": rows, "scope": "stored"}])
        result = self.client.list_sessions("stored", True)
        self.assertEqual(result, {"ok": True, "sessions": rows})
        self.assertEqual(self.owner.calls, [("session.list", {"scope": "stored", "picker": True})])

    def test_list_sessions_defaults_and_errors(self):
        self.script(session_list=[{"sessions": [], "scope": "live"}])
        self.assertEqual(self.client.list_sessions(), {"ok": True, "sessions": []})
        self.assertEqual(self.owner.calls, [("session.list", {"scope": "live", "picker": False})])
        self.owner = FakeOwner(session_list=[GatewayRPCError("permission_denied")])
        self.assertEqual(self.client.list_sessions()["error"], "refused-by-owner")
        self.owner = FakeOwner(session_list=[GatewayClientError(LEAK)])
        failure = self.client.list_sessions()
        self.assertEqual(failure["error"], "not-reachable")
        self.assert_clean(failure)
        self.discovery = SimpleNamespace(state="starting", endpoint=None)
        self.assertEqual(self.client.list_sessions()["error"], "owner-restarting")

    def test_receipt_wraps_prompt_receipt_read_only(self):
        for status in ("queued", "started", "terminal"):
            with self.subTest(status=status):
                answer = dict(submitted("adm-9"), status=status, outcome="completed" if status == "terminal" else None)
                self.owner = FakeOwner(prompt_receipt=[answer])
                result = self.client.receipt("adm-9", session_id="s1")
                self.assertEqual(result, {"ok": True, "status": status, "outcome": answer["outcome"]})
                self.assertEqual(self.owner.calls, [("prompt.receipt", {"session_id": "s1", "admission_id": "adm-9"})])
                self.assert_clean(result)

    def test_reads_never_resume_or_write(self):
        self.script(session_list=[{"sessions": []}], prompt_receipt=[submitted()])
        self.client.list_sessions()
        self.client.receipt("adm-1", session_id="s1")
        self.assertEqual(self.owner.methods(), ["session.list", "prompt.receipt"])
        self.owner = FakeOwner(prompt_receipt=[GatewayClientError(LEAK)])
        self.assertEqual(self.client.receipt("adm-1", session_id="s1")["error"], "not-reachable")


# The real gateway client's not-sent refusal text (hermes_cli.gateway_client.NOT_SENT).
NOT_SENT = "Gateway disconnected before the request was sent; nothing was sent."

ERROR_SENTENCES = {
    "not-addressable": "The session is held by another runtime that intercom cannot reach.",
    "refused-by-owner": "The gateway refused the call.",
    "no-owner": "The gateway for this profile is not running.",
    "owner-restarting": "The gateway is restarting.",
    "not-reachable": "The gateway did not answer before the message was handed over.",
    "delivery-unknown": "The gateway stopped answering after the message was handed over.",
}


class PublishedNamesTests(unittest.TestCase):
    """The fake mirrors the real names; check them where a Hermes build is importable."""

    def setUp(self):
        try:
            from hermes_cli import gateway_client, gateway_runtime
        except ImportError:
            self.skipTest("no Hermes build importable")
        self.gateway_client, self.gateway_runtime = gateway_client, gateway_runtime

    def test_names_and_shapes(self):
        import inspect

        gc, gr = self.gateway_client, self.gateway_runtime
        self.assertTrue(issubclass(gc.GatewayRPCError, gc.GatewayClientError))
        self.assertEqual(str(gc.GatewayRPCError("stale_generation")), "stale_generation")
        self.assertIn("timeout", inspect.signature(gr.discover_gateway_endpoint).parameters)
        self.assertTrue(hasattr(gc.connect_gateway(ENDPOINT), "__aenter__"))

    def test_not_sent_text_matches_the_real_refusal(self):
        """client.py matches this text to tell a request never written from a lost answer."""
        client = load_client()
        self.assertEqual(client._NOT_SENT, self.gateway_client.NOT_SENT)
        self.assertEqual(NOT_SENT, self.gateway_client.NOT_SENT)
        self.assertNotEqual(self.gateway_client.DISCONNECTED, client._NOT_SENT)


if __name__ == "__main__":
    unittest.main()
