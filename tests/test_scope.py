from __future__ import annotations

import importlib.util
import json
import os
import shutil
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock


PACKAGE_DIR = Path(__file__).resolve().parents[1]
MODULE_NAME = "hermes_intercom_scope_test"

OWNER = "123456789"
OTHER = "555000111"
GROUP = "-1001234567890"


def load_scope():
    spec = importlib.util.spec_from_file_location(MODULE_NAME, PACKAGE_DIR / "scope.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class FakeCtx:
    """``ctx.get_config`` over a settings dict the test may change between calls."""

    def __init__(self, **settings):
        self.settings = settings
        self.reads: list[str] = []

    def get_config(self, key, default=None):
        self.reads.append(key)
        return self.settings.get(key, default)


def origin(platform="telegram", chat_id=OWNER, chat_type="dm", user_id=OWNER):
    return json.dumps({"platform": platform, "chat_id": chat_id, "chat_type": chat_type,
                       "user_id": user_id})


class ScopeTestBase(unittest.TestCase):
    def setUp(self):
        self.home = Path(tempfile.mkdtemp(prefix="intercom-scope-"))
        self.addCleanup(shutil.rmtree, self.home, ignore_errors=True)
        env = mock.patch.dict(os.environ, {"HERMES_HOME": str(self.home)})
        env.start()
        self.addCleanup(env.stop)
        self.scope = load_scope()
        self.ctx = FakeCtx(echo_chats=[f"telegram:{GROUP}"], owner_user_ids=[f"telegram:{OWNER}"])

    def make_db(self):
        from hermes_state import SessionDB

        SessionDB().close()  # creates the schema in this home

    def add(self, session_id, source, *, user_id=None, chat_id=None, chat_type=None,
            origin_json=None):
        if not (self.home / "state.db").exists():
            self.make_db()
        with sqlite3.connect(self.home / "state.db") as conn:
            conn.execute(
                "INSERT INTO sessions (id, source, user_id, chat_id, chat_type, origin_json, started_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)",
                (session_id, source, user_id, chat_id, chat_type, origin_json, time.time()))
        conn.close()

    def scope_of(self, session_id):
        return self.scope.chat_scope(session_id, self.ctx)


class RuleTests(ScopeTestBase):
    def test_owner_dm_is_private(self):
        self.add("s1", "telegram", origin_json=origin())
        self.assertEqual(self.scope_of("s1"), "private")

    def test_non_owner_dm_is_shared(self):
        self.add("s1", "telegram", origin_json=origin(chat_id=OTHER, user_id=OTHER))
        self.assertEqual(self.scope_of("s1"), "shared")

    def test_unlisted_group_is_shared(self):
        self.add("s1", "telegram", origin_json=origin(chat_id="-100999", chat_type="group"))
        self.assertEqual(self.scope_of("s1"), "shared")

    def test_listed_group_is_private(self):
        self.add("s1", "telegram", origin_json=origin(chat_id=GROUP, chat_type="group", user_id=OTHER))
        self.assertEqual(self.scope_of("s1"), "private")

    def test_owner_in_an_unlisted_group_is_shared(self):
        self.add("s1", "telegram", origin_json=origin(chat_id="-100999", chat_type="group"))
        self.assertEqual(self.scope_of("s1"), "shared")

    def test_other_platform_prefix_is_shared(self):
        self.add("dm", "discord", origin_json=origin(platform="discord"))
        self.add("grp", "discord", origin_json=origin(platform="discord", chat_id=GROUP,
                                                      chat_type="group"))
        self.assertEqual(self.scope_of("dm"), "shared")
        self.assertEqual(self.scope_of("grp"), "shared")

    def test_columns_are_the_origin_without_origin_json(self):
        self.add("dm", "telegram", user_id=OWNER, chat_id=OWNER, chat_type="dm")
        self.add("grp", "telegram", user_id=OTHER, chat_id=GROUP, chat_type="group")
        self.add("other", "telegram", user_id=OTHER, chat_id=OTHER, chat_type="dm")
        self.assertEqual(self.scope_of("dm"), "private")
        self.assertEqual(self.scope_of("grp"), "private")
        self.assertEqual(self.scope_of("other"), "shared")

    def test_integer_ids_in_origin_json_match(self):
        self.add("s1", "telegram", origin_json=origin(chat_id=int(OWNER), user_id=int(OWNER)))
        self.assertEqual(self.scope_of("s1"), "private")

    def test_a_dm_needs_the_dm_chat_type(self):
        for i, chat_type in enumerate((None, "", "thread", "DM")):
            self.add(f"s{i}", "telegram", origin_json=origin(chat_type=chat_type))
            self.assertEqual(self.scope_of(f"s{i}"), "shared", chat_type)


class EntryTests(ScopeTestBase):
    def test_int_entry_is_ignored_with_one_warning(self):
        self.ctx.settings["owner_user_ids"] = [int(OWNER)]
        self.add("s1", "telegram", origin_json=origin())
        with self.assertLogs(self.scope.logger, "WARNING") as logs:
            self.assertEqual(self.scope_of("s1"), "shared")
            self.assertEqual(self.scope_of("s1"), "shared")
        self.assertEqual(len(logs.records), 1)
        self.assertIn("owner_user_ids", logs.output[0])

    def test_unprefixed_entries_are_ignored(self):
        self.ctx.settings = {"echo_chats": [GROUP, f":{GROUP}", "telegram:"],
                             "owner_user_ids": [OWNER, None, {"telegram": OWNER}]}
        self.add("dm", "telegram", origin_json=origin())
        self.add("grp", "telegram", origin_json=origin(chat_id=GROUP, chat_type="group"))
        with self.assertLogs(self.scope.logger, "WARNING") as logs:
            self.assertEqual(self.scope_of("dm"), "shared")
            self.assertEqual(self.scope_of("grp"), "shared")
        self.assertEqual(len(logs.records), 6)

    def test_valid_entries_still_count_beside_bad_ones(self):
        self.ctx.settings["owner_user_ids"] = [7, f"telegram:{OWNER}"]
        self.add("s1", "telegram", origin_json=origin())
        with self.assertLogs(self.scope.logger, "WARNING"):
            self.assertEqual(self.scope_of("s1"), "private")

    def test_a_setting_that_is_not_a_list_is_ignored(self):
        self.ctx.settings["owner_user_ids"] = f"telegram:{OWNER}"
        self.add("s1", "telegram", origin_json=origin())
        with self.assertLogs(self.scope.logger, "WARNING"):
            self.assertEqual(self.scope_of("s1"), "shared")

    def test_no_settings_is_shared(self):
        self.ctx.settings = {}
        self.add("s1", "telegram", origin_json=origin())
        self.assertEqual(self.scope_of("s1"), "shared")


class FreshReadTests(ScopeTestBase):
    def test_settings_are_read_on_every_call(self):
        self.add("s1", "telegram", origin_json=origin())
        self.assertEqual(self.scope_of("s1"), "private")
        self.ctx.settings["owner_user_ids"] = []
        self.assertEqual(self.scope_of("s1"), "shared")
        self.ctx.settings["echo_chats"] = [f"telegram:{OWNER}"]
        self.assertEqual(self.scope_of("s1"), "private")
        self.assertGreaterEqual(self.ctx.reads.count("echo_chats"), 3)

    def test_the_row_is_read_on_every_call(self):
        self.add("s1", "telegram", origin_json=origin(chat_id=OTHER, user_id=OTHER))
        self.assertEqual(self.scope_of("s1"), "shared")
        with sqlite3.connect(self.home / "state.db") as conn:
            conn.execute("UPDATE sessions SET origin_json = ? WHERE id = 's1'", (origin(),))
        conn.close()
        self.assertEqual(self.scope_of("s1"), "private")


class LocalTests(ScopeTestBase):
    def test_local_sources_are_none(self):
        for source in sorted(self.scope.LOCAL_SOURCES):
            self.add(f"s-{source}", source)
            self.assertIsNone(self.scope_of(f"s-{source}"), source)

    def test_local_source_with_a_messaging_origin_is_still_none(self):
        self.add("s1", "local", user_id=OWNER, chat_id=OWNER, chat_type="dm", origin_json="{bad")
        self.assertIsNone(self.scope_of("s1"))

    def test_local_set_is_the_b4_set(self):
        self.assertEqual(self.scope.LOCAL_SOURCES, {
            "cli", "tui", "desktop", "gui", "acp", "tool", "oneshot", "cron", "subagent", "local"})


class FailClosedTests(ScopeTestBase):
    def test_corrupt_origin_json_is_shared(self):
        for i, raw in enumerate(("{not json", "[1, 2]", '"telegram"', "null")):
            # The columns would say owner DM; a bad stored origin still fails closed.
            self.add(f"s{i}", "telegram", user_id=OWNER, chat_id=OWNER, chat_type="dm",
                     origin_json=raw)
            self.assertEqual(self.scope_of(f"s{i}"), "shared", raw)

    def test_missing_row_is_shared(self):
        self.make_db()
        self.assertEqual(self.scope_of("nope"), "shared")

    def test_missing_database_is_shared(self):
        self.assertFalse((self.home / "state.db").exists())
        with self.assertLogs(self.scope.logger, "WARNING"):
            self.assertEqual(self.scope_of("s1"), "shared")
        self.assertFalse((self.home / "state.db").exists(), "a read-only read must not create it")

    def test_read_error_is_shared_even_for_a_local_session(self):
        self.add("s1", "cli")
        import hermes_state

        with mock.patch.object(hermes_state.SessionDB, "get_session",
                               side_effect=sqlite3.OperationalError("database is locked")), \
                self.assertLogs(self.scope.logger, "WARNING"):
            self.assertEqual(self.scope_of("s1"), "shared")

    def test_messaging_source_with_no_origin_is_shared(self):
        self.add("bare", "telegram")
        self.add("no-chat", "telegram", origin_json=json.dumps({"platform": "telegram",
                                                                "user_id": OWNER, "chat_type": "dm"}))
        self.add("no-platform", "telegram", origin_json=json.dumps({"chat_id": OWNER,
                                                                    "user_id": OWNER, "chat_type": "dm"}))
        for session_id in ("bare", "no-chat", "no-platform"):
            self.assertEqual(self.scope_of(session_id), "shared", session_id)

    def test_settings_read_error_is_shared(self):
        self.add("s1", "telegram", origin_json=origin())
        self.ctx.get_config = mock.Mock(side_effect=ValueError("bad config"))
        with self.assertLogs(self.scope.logger, "WARNING"):
            self.assertEqual(self.scope_of("s1"), "shared")

    def test_opens_read_only_and_closes(self):
        self.add("s1", "telegram", origin_json=origin())
        import hermes_state

        opened = []
        real = hermes_state.SessionDB

        class Spy(real):
            def __init__(self, *args, **kwargs):
                opened.append(kwargs.get("read_only"))
                super().__init__(*args, **kwargs)

            def close(self):
                opened.append("closed")
                super().close()

        with mock.patch.object(hermes_state, "SessionDB", Spy):
            self.assertEqual(self.scope_of("s1"), "private")
        self.assertEqual(opened, [True, "closed"])


if __name__ == "__main__":
    unittest.main()
