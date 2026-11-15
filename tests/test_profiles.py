"""Other profiles, explicitly: ``profile`` on send and list (passive.md P6).

Two temp homes: a root (the ``default`` profile) and ``<root>/profiles/spike``, each with its own
config, ``state.db`` and record. ``client.deliver_sync`` and ``client.list_sessions`` are fakes
that note the Hermes home active when they are called.
"""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

import passive
import profiles
import record
import replies
import route

PACKAGE_DIR = Path(__file__).resolve().parents[1]
GROUP = "-1001234567890"
NAMED = "spike"


def _home():
    from hermes_constants import get_hermes_home

    return get_hermes_home()


def _override():
    from hermes_constants import get_hermes_home_override

    return get_hermes_home_override()


class FakeCtx:
    def __init__(self, **settings):
        self.settings = settings
        self.hooks = []

    def get_config(self, key, default=None):
        return self.settings.get(key, default)

    def register_hook(self, name, fn):
        self.hooks.append((name, fn))

    def register_tool(self, **kwargs):
        self.tool = kwargs

    def register_cli_command(self, *args, **kwargs):
        return None


class FakeOwner:
    """``client.deliver_sync`` stand-in: notes the active home and the row it sees there."""

    def __init__(self):
        self.calls = []
        self.lock = threading.Lock()
        self.error = None

    def __call__(self, target, text, delivery, submission_id):
        message_id = submission_id.removeprefix("intercom-")
        with self.lock:
            self.calls.append({"target": target, "text": text, "delivery": delivery,
                               "message_id": message_id, "home": _home(),
                               "row": record.get(message_id)})
        if self.error is not None:
            raise self.error
        return {"ok": True, "applied": "steer", "applied_detail": None, "admission_id": None}


class ProfilesTestBase(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="intercom-profiles-"))
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.named = self.root / "profiles" / NAMED
        self.named.mkdir(parents=True)
        self.in_home(self.root)
        # Each profile's own settings: the target's are the ones that must govern.
        self.write_config(self.root, user_name="Sam")
        self.write_config(self.named, user_name="Robin", echo_chats=[f"telegram:{GROUP}"])
        self.ctx = FakeCtx(user_name="Sam")
        self.owner = FakeOwner()
        self.listed = []
        for name, value in (("deliver_sync", self.owner), ("list_sessions", self.list_sessions)):
            patch = mock.patch.object(route.client, name, value)
            patch.start()
            self.addCleanup(patch.stop)
        for module in (passive, replies):
            ctx_patch = mock.patch.dict(module._ctx, {"ctx": self.ctx})
            ctx_patch.start()
            self.addCleanup(ctx_patch.stop)
        offered = mock.patch.dict(passive._offered, {}, clear=True)
        offered.start()
        self.addCleanup(offered.stop)

        from hermes_state import SessionDB

        self.dbs = {}
        for home in (self.root, self.named):
            db = SessionDB(db_path=home / "state.db")
            self.addCleanup(db.close)
            self.dbs[home] = db
        self.add(self.root, "d-sender", "Sender Chat", "cli")
        self.add(self.root, "d-reviewer", "Reviewer", "tui")
        self.add(self.root, "d-bot", "Bot Chat", "tui")  # same title in the sender's own profile
        self.add(self.named, "s-sender", "Spike Chat", "tui")
        self.add(self.named, "s-bot", "Bot Chat", "telegram",
                 origin={"platform": "telegram", "chat_id": GROUP, "chat_type": "group"})

    def in_home(self, home):
        """Run as a process of *home*'s profile (``HERMES_HOME``)."""
        env = mock.patch.dict(os.environ, {"HERMES_HOME": str(home)})
        env.start()
        self.addCleanup(env.stop)

    def write_config(self, home, *, consent=True, enabled=("intercom",), disabled=(), **settings):
        config = {"plugins": {"enabled": list(enabled), "disabled": list(disabled),
                              "entries": {"intercom": {"allow_gateway_injection": consent,
                                                       "settings": settings}}}}
        (home / "config.yaml").write_text(json.dumps(config) + "\n", encoding="utf-8")

    def add(self, home, session_id, title, source, *, origin=None):
        db = self.dbs[home]
        db.create_session(session_id, source=source)
        db.set_session_title(session_id, title)
        if origin is not None:
            db._write_sql("UPDATE sessions SET origin_json = ? WHERE id = ?",
                          (json.dumps(origin), session_id))

    def list_sessions(self, scope="live", picker=False):
        home = _home()
        self.listed.append(home)
        if scope != "stored":
            return {"ok": True, "sessions": []}
        db = self.dbs[home]
        rows = db._conn.execute("SELECT id, title, source FROM sessions").fetchall()
        return {"ok": True, "sessions": [{"id": r[0], "title": r[1], "source": r[2]} for r in rows]}

    def send(self, to="Bot Chat", message="build is green", *, sender="d-sender", **kwargs):
        kwargs.setdefault("reply_mode", "none")
        kwargs.setdefault("profile", NAMED)
        return route.send(sender, to, message, ctx=self.ctx, lister=self.list_sessions, **kwargs)

    def rows(self, home):
        path = home / "plugin-data" / "intercom" / "record.db"
        if not path.exists():
            return []
        conn = sqlite3.connect(path)
        conn.row_factory = sqlite3.Row
        try:
            return [dict(r) for r in conn.execute("SELECT * FROM messages ORDER BY created_at")]
        except sqlite3.OperationalError:
            return []
        finally:
            conn.close()

    def turn(self, session_id, *, prompt="", answer="", turn_id="turn-1", steers=()):
        """One finished turn of *session_id* in the active profile, through the plugin's hooks."""
        history = [{"role": "user", "content": prompt}]
        for steer in steers:
            history.append({"role": "assistant", "content": "", "tool_calls": []})
            history.append({"role": "user", "content": steer, "display_kind": "steer"})
        history.append({"role": "assistant", "content": answer})
        replies.on_post_llm_call(session_id=session_id, user_message=prompt, turn_id=turn_id,
                                 assistant_response=answer, conversation_history=history,
                                 model="m", platform="tui")
        for thread in threading.enumerate():
            if thread.name.startswith("intercom-reply-"):
                thread.join(10)

    def assert_nothing_sent(self, receipt, error):
        self.assertFalse(receipt["ok"], receipt)
        self.assertEqual(receipt["error"], error)
        self.assertEqual(self.owner.calls, [])
        self.assertEqual(self.rows(self.root), [])
        self.assertEqual(self.rows(self.named), [])
        self.assertIsNone(_override())


class SendAcrossTests(ProfilesTestBase):
    def test_default_to_named_runs_the_target_side_in_the_named_profile(self):
        receipt = self.send(reply_mode="requested")
        self.assertTrue(receipt["ok"], receipt)
        self.assertEqual(receipt["receipt"], "delivered")
        # "Bot Chat" resolves by exact title in the target's state.db, not the sender's.
        self.assertEqual(receipt["target_session"], "s-bot")
        self.assertEqual(receipt["target_title"], f"{NAMED}/Bot Chat")
        self.assertIn(f'"{NAMED}/Bot Chat"', receipt["message"])
        [call] = self.owner.calls
        self.assertEqual(call["home"], self.named)
        self.assertEqual(call["target"], "s-bot")
        # The row is the target's, written before delivery, with the sender's profile on it.
        self.assertIsNotNone(call["row"])
        self.assertEqual(self.rows(self.root), [])
        [row] = self.rows(self.named)
        self.assertEqual(row["id"], receipt["message_id"])
        self.assertEqual(row["sender_profile"], "default")
        self.assertEqual(row["sender_label"], "default/Sender Chat")
        self.assertEqual(row["sender_session_id"], "d-sender")
        self.assertEqual(row["target_title"], "Bot Chat")
        self.assertEqual(row["header_sha"], route.header_sha(call["text"], row["id"]))
        # The target's config: its user name and its echo_chats (the group is private there).
        self.assertEqual(row["target_chat_scope"], "private")
        header = call["text"].splitlines()[1]
        self.assertTrue(header.startswith("📡 Not Robin · "), header)
        self.assertIn("hermes@default/Sender Chat says (session d-sender · message", header)
        self.assertIsNotNone(replies._HEADER_RE.match(header))
        self.assertEqual(call["text"].splitlines()[0], "Message from 📡 default/Sender Chat (@default):")
        self.assertIsNone(_override())

    def test_named_to_default(self):
        self.in_home(self.named)
        receipt = self.send(to="Reviewer", sender="s-sender", profile="default")
        self.assertTrue(receipt["ok"], receipt)
        self.assertEqual(receipt["target_title"], "default/Reviewer")
        [call] = self.owner.calls
        self.assertEqual(call["home"], self.root)
        self.assertEqual(call["target"], "d-reviewer")
        self.assertEqual(self.rows(self.named), [])
        [row] = self.rows(self.root)
        self.assertEqual(row["sender_profile"], NAMED)
        self.assertEqual(row["sender_label"], f"{NAMED}/Spike Chat")
        self.assertIn(f"📡 Not Sam · no reply needed · hermes@{NAMED}/Spike Chat says", call["text"])
        self.assertEqual(call["text"].splitlines()[0], f"Message from 📡 {NAMED}/Spike Chat (@{NAMED}):")

    def test_naming_the_own_profile_is_the_same_as_no_profile(self):
        receipt = self.send(to="Reviewer", profile="default")
        self.assertTrue(receipt["ok"], receipt)
        self.assertEqual(receipt["target_title"], "Reviewer")
        [call] = self.owner.calls
        self.assertEqual(call["home"], self.root)
        self.assertIn("hermes@Sender Chat says", call["text"])
        [row] = self.rows(self.root)
        self.assertIsNone(row["sender_profile"])
        self.assertEqual(row["sender_label"], "Sender Chat")

    def test_without_profile_nothing_changes(self):
        receipt = self.send(profile=None)
        self.assertTrue(receipt["ok"], receipt)
        self.assertEqual(receipt["target_session"], "d-bot")
        self.assertEqual(self.owner.calls[0]["home"], self.root)
        self.assertEqual(self.rows(self.named), [])

    def test_bot_is_reached_by_exact_title_only(self):
        receipt = self.send(to="Bot")
        self.assertFalse(receipt["ok"])
        self.assertEqual(receipt["error"], "unknown")
        self.assertEqual(self.owner.calls, [])
        # The refusal row is the target profile's: that is where the name was looked up.
        [row] = self.rows(self.named)
        self.assertEqual(row["delivery_path"], "refused")

    def test_sender_chat_scope_is_read_in_the_sender_profile(self):
        self.add(self.root, "d-group", "Group Chat", "telegram",
                 origin={"platform": "telegram", "chat_id": GROUP, "chat_type": "group"})
        # The named profile lists GROUP as private; the sender's own profile does not.
        receipt = self.send(sender="d-group")
        self.assertTrue(receipt["ok"], receipt)
        [row] = self.rows(self.named)
        self.assertEqual(row["sender_chat_scope"], "shared")

    def test_keyed_queue_wake_line_names_the_sender_with_its_profile(self):
        receipt = self.send(delivery="queue", key="build")
        self.assertTrue(receipt["ok"], receipt)
        [row] = self.rows(self.named)
        self.assertEqual(self.owner.calls[0]["home"], self.named)
        self.in_home(self.named)  # the target's gateway frames the version its wake turn gets
        text = route.frame_stored(record.get(row["id"]), self.ctx)
        self.assertIn("hermes@default/Sender Chat says (session d-sender", text)


class ReplyAcrossTests(ProfilesTestBase):
    def test_reply_goes_back_to_the_sender_profile(self):
        receipt = self.send(reply_mode="requested")
        message_id = receipt["message_id"]
        text = self.owner.calls[0]["text"]
        # The named profile's gateway: its own home and its own plugin ctx.
        self.in_home(self.named)
        self.turn("s-bot", prompt=text, answer=f"[REPLY {message_id}]Green here too.[/REPLY]")
        self.assertEqual(len(self.owner.calls), 2)
        call = self.owner.calls[1]
        self.assertEqual(call["home"], self.root)
        self.assertEqual(call["target"], "d-sender")
        self.assertIn(f"hermes@{NAMED}/Bot Chat", call["text"])
        self.assertIn(f"reply to your message {message_id}", call["text"])
        self.assertIn("Green here too.", call["text"])
        [reply_row] = self.rows(self.root)
        self.assertEqual(reply_row["id"], call["message_id"])
        self.assertEqual(reply_row["in_reply_to"], message_id)
        self.assertEqual(reply_row["sender_profile"], NAMED)
        [original] = self.rows(self.named)
        self.assertEqual(original["reply_state"], "sent")
        self.assertEqual(original["reply_message_id"], call["message_id"])
        self.assertIsNone(_override())


class PassiveAcrossTests(ProfilesTestBase):
    def test_passive_note_lands_in_the_target_record_and_is_handed_over_there(self):
        receipt = self.send(delivery="passive")
        self.assertTrue(receipt["ok"], receipt)
        self.assertEqual(receipt["receipt"], "held")
        self.assertIn(f'"{NAMED}/Bot Chat"', receipt["message"])
        self.assertEqual(self.owner.calls, [])
        self.assertEqual(self.rows(self.root), [])
        [row] = self.rows(self.named)
        self.assertEqual(row["result"], "pending")
        self.assertEqual(row["sender_profile"], "default")
        self.in_home(self.named)
        result = passive.on_pre_llm_call(session_id="s-bot", turn_id="turn-1",
                                         user_message="task", conversation_history=[],
                                         platform="tui", model="m")
        self.assertIsNotNone(result)
        self.assertIn("default/Sender Chat", result["context"])
        self.assertIn("build is green", result["context"])


class RefusalTests(ProfilesTestBase):
    def test_target_injection_off_is_refused(self):
        self.write_config(self.named, consent=False)
        receipt = self.send()
        self.assertFalse(receipt["ok"])
        self.assertEqual(receipt["error"], "injection-off")
        self.assertIn(f"Hermes profile {NAMED}", receipt["message"])
        self.assertEqual(self.owner.calls, [])
        [row] = self.rows(self.named)
        self.assertEqual(row["delivery_path"], "refused")
        self.assertIsNone(_override())

    def test_sender_consent_does_not_matter_only_the_target_s(self):
        self.write_config(self.root, consent=False)
        self.assertTrue(self.send()["ok"])

    def test_intercom_not_enabled_in_target_is_refused(self):
        for enabled, disabled in (((), ()), (("intercom",), ("intercom",))):
            with self.subTest(enabled=enabled, disabled=disabled):
                self.write_config(self.named, enabled=enabled, disabled=disabled)
                receipt = self.send()
                self.assert_nothing_sent(receipt, "profile-not-enabled")
                self.assertIn(f"session mail is not enabled in profile {NAMED}", receipt["message"])

    def test_unknown_or_deleted_profile_is_refused(self):
        from hermes_constants import mark_named_profile_deleted

        gone = self.root / "profiles" / "gone"
        gone.mkdir()
        (gone / "config.yaml").write_text("{}\n", encoding="utf-8")
        mark_named_profile_deleted(gone)
        shell = self.root / "profiles" / "shell"  # a directory with no identity marker
        shell.mkdir()
        for name in ("nosuch", "gone", "shell", "Bad/Name", "../profiles/spike"):
            with self.subTest(name=name):
                receipt = self.send(profile=name)
                self.assert_nothing_sent(receipt, "unknown-profile")
                self.assertIn("there is no Hermes profile named", receipt["message"])

    def test_profile_with_all_ask_or_project_is_a_usage_refusal(self):
        cases = {
            "all": dict(to="all", delivery="passive", key="k"),
            "ask": dict(kind="ask", reply_mode="waiting"),
            "@project": dict(to="@site"),
        }
        for name, kwargs in cases.items():
            with self.subTest(name):
                receipt = self.send(**kwargs)
                self.assert_nothing_sent(receipt, "usage")
                self.assertEqual(receipt["exit"], 2)
                self.assertIn("profile", receipt["message"])

    def test_ask_tool_with_profile_is_refused(self):
        result = replies.ask({"to": "Bot Chat", "message": "ok?", "profile": NAMED},
                             session_id="d-sender", ctx=self.ctx, lister=self.list_sessions)
        self.assert_nothing_sent(result, "usage")
        self.assertEqual(result["message"], route.PROFILE_WITH_ASK)


class OverrideTests(ProfilesTestBase):
    def test_override_is_reset_after_a_delivery_error(self):
        self.owner.error = RuntimeError("boom")
        with self.assertRaises(RuntimeError):
            self.send()
        self.assertEqual(self.owner.calls[0]["home"], self.named)
        self.assertIsNone(_override())
        self.assertEqual(_home(), self.root)

    def test_in_profile_resets_on_error_and_refuses_an_unknown_name(self):
        with self.assertRaises(ValueError):
            with profiles.in_profile(NAMED) as home:
                self.assertEqual(home, self.named)
                self.assertEqual(_home(), self.named)
                raise ValueError("inside")
        self.assertIsNone(_override())
        with self.assertRaises(LookupError):
            with profiles.in_profile("nosuch"):
                pass
        self.assertIsNone(_override())

    def test_names_to_homes(self):
        self.assertEqual(profiles.home_for("default"), self.root)
        self.assertEqual(profiles.home_for(NAMED), self.named)
        self.assertIsNone(profiles.home_for("nosuch"))
        self.assertEqual(profiles.current(), "default")
        self.in_home(self.named)
        self.assertEqual(profiles.current(), NAMED)
        self.assertEqual(profiles.home_for("default"), self.root)


def _load_plugin():
    name = "hermes_intercom_test_profiles"
    spec = importlib.util.spec_from_file_location(
        name, PACKAGE_DIR / "__init__.py", submodule_search_locations=[str(PACKAGE_DIR)])
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


class ToolTests(ProfilesTestBase):
    def setUp(self):
        super().setUp()
        self.plugin = _load_plugin()
        client = importlib.import_module(f"{self.plugin.__name__}.client")
        for name, value in (("deliver_sync", self.owner), ("list_sessions", self.list_sessions)):
            patch = mock.patch.object(client, name, value)
            patch.start()
            self.addCleanup(patch.stop)
        runtime = mock.patch.dict(self.plugin._runtime, {"ctx": self.ctx, "legacy": False})
        runtime.start()
        self.addCleanup(runtime.stop)

    def tool(self, **args):
        return json.loads(self.plugin.handle(args, session_id="d-sender"))

    def test_schema_and_description_name_profile(self):
        sentence = 'profile: another Hermes profile on this machine; reach its bot with to: "Bot Chat"'
        schema = self.plugin._schema(False)
        self.assertIn(sentence, schema["parameters"]["properties"]["profile"]["description"])
        self.assertIn(sentence, schema["description"])
        self.assertNotIn("profile", schema["parameters"]["required"])

    def test_list_with_profile_lists_that_profile(self):
        result = self.tool(action="list", profile=NAMED)
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["profile"], NAMED)
        self.assertEqual({s["id"] for s in result["sessions"]}, {"s-sender", "s-bot"})
        self.assertEqual(set(self.listed), {self.named})
        self.assertIsNone(_override())

    def test_list_without_profile_is_unchanged(self):
        result = self.tool(action="list")
        self.assertTrue(result["ok"], result)
        self.assertIn("d-bot", {s["id"] for s in result["sessions"]})
        self.assertEqual(set(self.listed), {self.root})

    def test_list_refusals(self):
        self.assertEqual(self.tool(action="list", profile="nosuch")["error"], "unknown-profile")
        self.write_config(self.named, enabled=())
        self.assertEqual(self.tool(action="list", profile=NAMED)["error"], "profile-not-enabled")
        self.assertEqual(self.listed, [])
        self.assertIsNone(_override())

    def test_send_with_profile_from_the_tool(self):
        result = self.tool(action="send", to="Bot Chat", message="hi", reply="none", profile=NAMED)
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["target_session"], "s-bot")
        self.assertEqual(self.owner.calls[0]["home"], self.named)


if __name__ == "__main__":
    unittest.main()
