"""Addressing: resolve rules, project addressing, and list."""

from __future__ import annotations

import hashlib
import importlib.util
import os
import shutil
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import address

PACKAGE_DIR = Path(__file__).resolve().parents[1]

UNAVAILABLE = (
    "Project addressing is not available on this Hermes. Use "
    "list (filter by project) and address the session by id."
)
NO_OWNER = "Project @{slug} has no owner session set. Address a session by id (from list)."
PLANNER = "Reviewer \u2014 Planner"


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class AddressTests(unittest.TestCase):
    def setUp(self):
        self._old_home = os.environ.get("HERMES_HOME")
        self.home = Path(tempfile.mkdtemp(prefix="intercom-address-"))
        os.environ["HERMES_HOME"] = str(self.home)
        self.db = None
        address.registered_project_addressing = None

    def tearDown(self):
        try:
            if self.db is not None:
                self.db.close()
        finally:
            self.db = None
            if self._old_home is None:
                os.environ.pop("HERMES_HOME", None)
            else:
                os.environ["HERMES_HOME"] = self._old_home
            shutil.rmtree(self.home, ignore_errors=True)
            address.registered_project_addressing = None

    def write_config(self, *, enabled=None, plugin="projects"):
        lines = ["plugins:"]
        if enabled is None:
            lines.append("  enabled: []")
        else:
            lines.append("  enabled:")
            for item in enabled:
                lines.append(f"    - {item}")
        if plugin is not None:
            lines.extend([
                "  entries:",
                "    intercom:",
                "      settings:",
                f"        project_plugin: {plugin}",
            ])
        (self.home / "config.yaml").write_text("\n".join(lines) + "\n", encoding="utf-8")

    def enable_projects(self):
        self.write_config(enabled=["projects"], plugin="projects")

    def make_projects(self, rows, *, owner_column=True, folders=()):
        path = self.home / "projects.db"
        conn = sqlite3.connect(path)
        try:
            columns = "id TEXT PRIMARY KEY, slug TEXT NOT NULL UNIQUE, name TEXT, archived INTEGER NOT NULL DEFAULT 0"
            if owner_column:
                columns += ", owner_session_id TEXT"
            conn.execute(f"CREATE TABLE projects ({columns})")
            conn.execute(
                "CREATE TABLE project_folders ("
                "project_id TEXT NOT NULL, path TEXT NOT NULL, PRIMARY KEY (project_id, path))"
            )
            for row in rows:
                if owner_column:
                    conn.execute(
                        "INSERT INTO projects (id, slug, name, archived, owner_session_id) "
                        "VALUES (?, ?, ?, ?, ?)",
                        (
                            row.get("id") or row["slug"],
                            row["slug"],
                            row.get("name", row["slug"]),
                            1 if row.get("archived") else 0,
                            row.get("owner"),
                        ),
                    )
                else:
                    conn.execute(
                        "INSERT INTO projects (id, slug, name, archived) VALUES (?, ?, ?, ?)",
                        (
                            row.get("id") or row["slug"],
                            row["slug"],
                            row.get("name", row["slug"]),
                            1 if row.get("archived") else 0,
                        ),
                    )
            for project_id, folder in folders:
                conn.execute(
                    "INSERT INTO project_folders (project_id, path) VALUES (?, ?)",
                    (project_id, folder),
                )
            conn.commit()
        finally:
            conn.close()
        return path

    def open_writer(self):
        from hermes_state import SessionDB

        self.db = SessionDB()
        return self.db

    def add_session(self, sid, title=None, source="cli", parent=None, started_at=None, end=None):
        kwargs = {}
        if parent:
            kwargs["parent_session_id"] = parent
        self.db.create_session(sid, source=source, **kwargs)
        if end:
            self.db.end_session(sid, end)
        if title:
            self.db.set_session_title(sid, title)
        if started_at is not None:
            self.db._write_sql(
                "UPDATE sessions SET started_at = ? WHERE id = ?", (started_at, sid),
            )

    def close_writer(self):
        self.db.close()
        self.db = None

    def test_home_and_exact_title_are_case_sensitive(self):
        self.open_writer()
        self.add_session("local-home", title="Home", started_at=10)
        self.add_session("local-review", title="Reviewer", started_at=11)
        self.close_writer()
        home = address.resolve("Home", lister=_raising_lister)
        self.assertEqual(home, {"ok": True, "session_id": "local-home", "title": "Home"})
        padded = address.resolve("  Home  ", lister=_raising_lister)
        self.assertEqual(padded["session_id"], "local-home")
        titled = address.resolve("Reviewer", lister=_raising_lister)
        self.assertEqual(titled["session_id"], "local-review")
        wrong_case = address.resolve("home", lister=_quiet_lister())
        self.assertEqual(wrong_case["error"], "unknown")
        self.assertFalse(wrong_case["ok"])
        self.assertNotIn("session_id", wrong_case)

    def test_exact_title_beats_id_prefix(self):
        self.open_writer()
        self.add_session("local-title-win", title="local-shared", started_at=2)
        self.add_session("local-shared-999", title="Other", started_at=3)
        self.close_writer()
        result = address.resolve("local-shared", lister=_raising_lister)
        self.assertTrue(result["ok"])
        self.assertEqual(result["session_id"], "local-title-win")

    def test_unique_local_and_v022_prefixes(self):
        self.open_writer()
        self.add_session("local-shared-900", title="Shared", started_at=1)
        self.add_session("local-other", title="Other", started_at=2)
        self.add_session("20261111_130000_bbbb", title="Legacy", started_at=3)
        self.close_writer()
        local = address.resolve("local-shared-9", lister=_raising_lister)
        self.assertEqual(local["session_id"], "local-shared-900")
        self.assertEqual(local["title"], "Shared")
        legacy = address.resolve("20261111_130000_bb", lister=_raising_lister)
        self.assertEqual(legacy["session_id"], "20261111_130000_bbbb")
        exact = address.resolve("local-shared-900", lister=_raising_lister)
        self.assertEqual(exact["session_id"], "local-shared-900")

    def test_prefix_wildcards_are_literal(self):
        self.open_writer()
        self.add_session("100%abc", title="Percent", started_at=5)
        self.add_session("100Xabc", title="Plain", started_at=4)
        self.add_session("100_abc", title="Under", started_at=3)
        self.add_session("20261111_120000_aaaa", title="A", started_at=20)
        self.add_session("20261111_120000_aaab", title="B", started_at=10)
        self.add_session("20261111X120000_aaaa", title="Not a prefix", started_at=30)
        self.close_writer()
        percent = address.resolve("100%", lister=_raising_lister)
        self.assertEqual(percent["session_id"], "100%abc")
        under = address.resolve("100_", lister=_raising_lister)
        self.assertEqual(under["session_id"], "100_abc")
        listed = _quiet_lister()
        ambiguous = address.resolve("20261111_120000_aaa", lister=listed)
        self.assertEqual(ambiguous["error"], "ambiguous")
        ids = [item["id"] for item in ambiguous["candidates"]]
        self.assertEqual(ids, ["20261111_120000_aaaa", "20261111_120000_aaab"])
        self.assertNotIn("20261111X120000_aaaa", ids)

    def test_ambiguous_prefix_caps_candidates_and_availability(self):
        self.open_writer()
        ids = [f"local-amb-{index}" for index in range(6)]
        for index, sid in enumerate(ids):
            self.add_session(sid, title=f"Ambiguous {index}", started_at=600 - index)
        self.close_writer()
        stored = [
            {"id": ids[0], "title": "Ambiguous 0", "last_active_at": 111.0, "running": True},
            {"id": ids[1], "title": "Ambiguous 1", "last_active_at": 50.0, "running": False},
            {"id": ids[2], "title": "Ambiguous 2", "last_active_at": 40.0},
            {"id": ids[3], "title": "Ambiguous 3", "last_active_at": 30.0},
            {"id": ids[4], "title": "Ambiguous 4", "last_active_at": 20.0},
            {"id": ids[5], "title": "Ambiguous 5", "last_active_at": 10.0, "running": True},
        ]
        live = [{"id": ids[0], "running": True, "last_active_at": 999.0}]
        calls = []
        result = address.resolve("local-amb-", lister=_recording_lister(stored, live, calls))
        self.assertEqual(result["error"], "ambiguous")
        self.assertEqual(result["message"], "That address matches more than one session.")
        self.assertEqual([item["id"] for item in result["candidates"]], ids[:5])
        self.assertNotIn(ids[5], [item["id"] for item in result["candidates"]])
        first, second, third = result["candidates"][:3]
        self.assertEqual(first["availability"], "open")
        self.assertEqual(first["last_active"], 111.0)
        self.assertEqual(second["availability"], "not open")
        self.assertEqual(third["availability"], "not open")
        self.assertEqual(calls, [("stored", True), ("live", False)])
        for item in result["candidates"]:
            self.assertEqual(set(item), {"title", "id", "last_active", "availability"})

    def test_lister_failure_still_returns_the_refusal(self):
        self.open_writer()
        self.add_session("local-amb-a", title="A", started_at=2)
        self.add_session("local-amb-b", title="B", started_at=1)
        self.close_writer()
        failed = address.resolve("local-amb-", lister=_raising_lister)
        self.assertEqual(failed["error"], "ambiguous")
        self.assertIsNone(failed["candidates"][0]["last_active"])
        self.assertEqual(failed["candidates"][0]["availability"], "not open")

    def test_compression_tip_on_title_id_and_prefix(self):
        self.open_writer()
        self.add_session("local-reports-parent", title="Reports", started_at=1)
        self.db.end_session("local-reports-parent", "compression")
        self.add_session(
            "local-reports-child", source="cli", parent="local-reports-parent", started_at=2,
        )
        self.close_writer()
        for text in ("Reports", "local-reports-parent", "local-reports-p"):
            result = address.resolve(text, lister=_raising_lister)
            self.assertTrue(result["ok"], text)
            self.assertEqual(result["session_id"], "local-reports-child", text)
            self.assertEqual(result["title"], "Reports", text)

    def test_prefix_rows_that_share_a_tip_are_one_session(self):
        self.open_writer()
        self.add_session("local-chain-aaaa", title="Chain", started_at=1)
        self.db.end_session("local-chain-aaaa", "compression")
        self.add_session("local-chain-aaab", parent="local-chain-aaaa", started_at=2)
        self.db.end_session("local-chain-aaab", "compression")
        self.add_session("local-chain-aaac", parent="local-chain-aaab", started_at=3)
        self.close_writer()
        result = address.resolve("local-chain-aaa", lister=_raising_lister)
        self.assertEqual(result, {"ok": True, "session_id": "local-chain-aaac", "title": "Chain"})

    def test_unknown_title_lists_closest_or_none(self):
        self.open_writer()
        self.add_session("local-orch", title=PLANNER, started_at=5)
        self.add_session("local-home", title="Home", started_at=4)
        self.close_writer()
        stored = [{"id": "local-orch", "title": PLANNER, "last_active_at": 8.0}]
        typo = address.resolve("Reviewerr \u2014 Planner", lister=_quiet_lister(stored, []))
        self.assertEqual(typo["error"], "unknown")
        self.assertEqual(typo["message"], "No session matches that address.")
        self.assertEqual(typo["candidates"][0]["id"], "local-orch")
        self.assertEqual(typo["candidates"][0]["title"], PLANNER)
        self.assertEqual(typo["candidates"][0]["last_active"], 8.0)
        self.assertEqual(typo["candidates"][0]["availability"], "not open")
        partial = address.resolve("Planner", lister=_quiet_lister(stored, [{"id": "local-orch"}]))
        self.assertEqual(partial["error"], "unknown")
        self.assertEqual(partial["candidates"][0]["id"], "local-orch")
        self.assertEqual(partial["candidates"][0]["availability"], "open")
        none = address.resolve("zzzzzzzz", lister=_raising_lister)
        self.assertEqual(none["candidates"], [])
        empty = address.resolve("   ", lister=_raising_lister)
        self.assertEqual(empty["error"], "unknown")
        self.assertEqual(address.resolve(None, lister=_raising_lister)["error"], "unknown")
        self.assertEqual(address.resolve(12, lister=_raising_lister)["error"], "unknown")

    def test_closest_titles_page_the_published_listing(self):
        self.open_writer()
        for index in range(address._PREFIX_PAGE):
            self.add_session(f"local-fill-{index}", title=f"xxxx {index}", started_at=1000 + index)
        self.add_session("local-old", title="Reviewer notes", started_at=1)
        self.add_session("local-hidden", title="Reviewer hidden", started_at=2)
        self.db.set_session_hidden("local-hidden", True)
        self.add_session("local-archived", title="Reviewer archived", started_at=3)
        self.db.set_session_archived("local-archived", True, notify=False)
        self.add_session("local-cron", title="Reviewer cron", source="cron", started_at=4)
        self.close_writer()
        result = address.resolve("Reviewer", lister=_quiet_lister())
        self.assertEqual(result["error"], "unknown")
        self.assertEqual(
            [item["id"] for item in result["candidates"]],
            ["local-cron", "local-old", "local-hidden", "local-archived"],
        )

    def test_closest_titles_cap_at_five(self):
        self.open_writer()
        for index in range(6):
            self.add_session(f"local-near-{index}", title=f"Planner {index}", started_at=index)
        self.close_writer()
        result = address.resolve("Planner", lister=_quiet_lister())
        self.assertEqual(result["error"], "unknown")
        self.assertEqual(len(result["candidates"]), 5)

    def test_project_slug_resolves_to_owner_and_tip(self):
        self.enable_projects()
        self.open_writer()
        self.add_session("local-owner-parent", title="Owner", started_at=1)
        self.db.end_session("local-owner-parent", "compression")
        self.add_session("local-owner-child", title="Reports", parent="local-owner-parent", started_at=2)
        self.add_session("local-orch", title=PLANNER, started_at=3)
        self.close_writer()
        path = self.make_projects([
            {"slug": "reviewer", "name": "Reviewer", "owner": "local-owner-parent"},
            {"slug": "other", "name": "Other", "owner": "local-orch"},
        ])
        before = _sha256(path)
        with _assert_projects_readonly():
            result = address.resolve("@reviewer", lister=_raising_lister)
        self.assertEqual(result["session_id"], "local-owner-child")
        self.assertEqual(result["title"], "Reports")
        self.assertEqual(_sha256(path), before)
        self.assertFalse(path.with_name(path.name + "-wal").exists())
        self.assertFalse(path.with_name(path.name + "-journal").exists())

    def test_at_title_beats_slug_only_when_feature_is_on(self):
        self.enable_projects()
        self.open_writer()
        self.add_session("local-at-title", title="@reviewer", started_at=2)
        self.add_session("local-owner", title="Owner", started_at=1)
        self.close_writer()
        self.make_projects([{"slug": "reviewer", "owner": "local-owner"}])
        titled = address.resolve("@reviewer", lister=_raising_lister)
        self.assertEqual(titled["session_id"], "local-at-title")
        self.write_config(enabled=[], plugin="projects")
        refused = address.resolve("@reviewer", lister=_raising_lister)
        self.assertEqual(refused["error"], "project-addressing-unavailable")
        self.assertNotIn("local-at-title", repr(refused))

    def test_no_owner_set_uses_stored_slug_and_guesses_nothing(self):
        self.enable_projects()
        self.open_writer()
        self.add_session("local-orch", title=PLANNER, started_at=1)
        self.close_writer()
        for owner in (None, "", "   "):
            with self.subTest(owner=owner):
                path = self.home / "projects.db"
                if path.exists():
                    path.unlink()
                self.make_projects([{"slug": "reviewer", "name": "Reviewer", "owner": owner}])
                result = address.resolve("@reviewer", lister=_raising_lister)
                self.assertEqual(result["error"], "no-owner-set")
                self.assertEqual(result["message"], NO_OWNER.format(slug="reviewer"))
                self.assertEqual(result["candidates"], [])
                self.assertNotIn("local-orch", repr(result))
                self.assertNotIn("Nothing was sent", result["message"])
        other_case = address.resolve("@Reviewer", lister=_raising_lister)
        self.assertEqual(other_case["error"], "unknown")
        self.assertNotIn("local-orch", repr(other_case))

    def test_unknown_slug_lists_closest_slugs_only(self):
        self.enable_projects()
        self.open_writer()
        self.add_session("local-orch", title=PLANNER, started_at=1)
        self.close_writer()
        slugs = [
            "reviewer", "review-a", "review-b", "review-c", "review-d", "review-e",
            "review-platform-extra",
        ]
        self.make_projects(
            [{"slug": slug, "owner": "local-orch"} for slug in slugs]
            + [{"slug": "review-archived", "owner": "local-orch", "archived": True}]
        )
        result = address.resolve("@reviewerr", lister=_raising_lister)
        self.assertEqual(result["error"], "unknown")
        self.assertEqual(result["message"], "No project matches that slug.")
        found = [item["slug"] for item in result["candidates"]]
        self.assertIn("reviewer", found)
        self.assertLessEqual(len(found), 5)
        self.assertNotIn("review-archived", found)
        self.assertNotIn("local-orch", repr(result))
        for item in result["candidates"]:
            self.assertEqual(set(item), {"slug"})
        many = address.resolve("@review-", lister=_raising_lister)
        many_slugs = [item["slug"] for item in many["candidates"]]
        self.assertEqual(len(many_slugs), 5)
        self.assertEqual(many_slugs[0], "review-a")
        self.assertNotIn("review-platform-extra", many_slugs)
        self.assertNotIn("review-archived", many_slugs)
        self.assertNotIn("reviewer", many_slugs)
        archived = address.resolve("@review-archived", lister=_raising_lister)
        self.assertEqual(archived["error"], "unknown")
        self.assertNotIn("local-orch", repr(archived))
        self.assertNotEqual(archived.get("session_id"), "local-orch")

    def test_ambiguous_owner_prefix(self):
        self.enable_projects()
        self.open_writer()
        self.add_session("local-own-aaa", title="One", started_at=2)
        self.add_session("local-own-aab", title="Two", started_at=1)
        self.close_writer()
        self.make_projects([{"slug": "reviewer", "owner": "local-own-aa"}])
        result = address.resolve("@reviewer", lister=_quiet_lister())
        self.assertEqual(result["error"], "ambiguous")
        self.assertEqual(
            [item["id"] for item in result["candidates"]],
            ["local-own-aaa", "local-own-aab"],
        )

    def test_missing_owner_session_is_unknown(self):
        self.enable_projects()
        self.open_writer()
        self.add_session("local-other", title="Other", started_at=1)
        self.close_writer()
        self.make_projects([{"slug": "reviewer", "owner": "local-missing"}])
        result = address.resolve("@reviewer", lister=_raising_lister)
        self.assertEqual(result["error"], "unknown")
        self.assertEqual(result["message"], "No session matches that address.")
        self.assertEqual(result["candidates"], [])

    def test_feature_absent_never_picks_planner_title(self):
        self.open_writer()
        self.add_session("local-orch", title=PLANNER, started_at=1)
        self.add_session("local-at", title="@reviewer", started_at=2)
        self.close_writer()
        column_path = self.home / "projects.db"
        # The three absence cases share the sessions above; rebuild the db per case.
        builders = {
            "plugin-off": lambda: (
                self.write_config(enabled=[], plugin="projects"),
                self.make_projects([{"slug": "reviewer", "owner": "local-orch"}]),
            ),
            "column-missing": lambda: (
                self.write_config(enabled=["projects"], plugin="projects"),
                self.make_projects([{"slug": "reviewer"}], owner_column=False),
            ),
            "no-file": lambda: self.write_config(enabled=["projects"], plugin="projects"),
        }
        # Drop the db left by the preview setup so "no-file" starts clean.
        if column_path.exists():
            column_path.unlink()
        for name, build in builders.items():
            with self.subTest(case=name):
                if column_path.exists():
                    column_path.unlink()
                build()
                before = _sha256(column_path) if column_path.exists() else None
                result = address.resolve("@reviewer", lister=_raising_lister)
                self.assertEqual(result["error"], "project-addressing-unavailable")
                self.assertEqual(result["message"], UNAVAILABLE)
                self.assertEqual(result["candidates"], [])
                self.assertNotIn("local-orch", repr(result))
                self.assertNotIn("local-at", repr(result))
                self.assertNotIn("Nothing was sent", result["message"])
                if before is not None:
                    self.assertEqual(_sha256(column_path), before)
        self.assertFalse(address.check_project_addressing())

    def test_corrupt_projects_db_is_not_available_and_unchanged(self):
        self.enable_projects()
        path = self.home / "projects.db"
        path.write_bytes(b"not a sqlite database\n")
        before = path.read_bytes()
        self.assertFalse(address.check_project_addressing())
        result = address.resolve("@reviewer", lister=_raising_lister)
        self.assertEqual(result["error"], "project-addressing-unavailable")
        self.assertEqual(path.read_bytes(), before)

    def test_plugin_id_must_be_an_enabled_string(self):
        self.write_config(enabled=["other"], plugin="projects")
        self.make_projects([{"slug": "reviewer", "owner": "local-x"}])
        self.assertFalse(address.check_project_addressing())
        self.write_config(enabled=["projects"], plugin="3")
        # Non-string settings are written as YAML ints when unquoted; rewrite by hand.
        (self.home / "config.yaml").write_text(
            "plugins:\n  enabled:\n    - projects\n  entries:\n    intercom:\n"
            "      settings:\n        project_plugin: 3\n",
            encoding="utf-8",
        )
        self.assertFalse(address.check_project_addressing())

    def test_readonly_open_on_every_resolve_and_never_migrates(self):
        self.enable_projects()
        self.open_writer()
        self.add_session("local-home", title="Home", started_at=1)
        self.close_writer()
        path = self.make_projects([{"slug": "reviewer", "owner": "local-home"}])
        path.chmod(0o444)
        before = _sha256(path)
        with mock.patch("hermes_cli.projects_db.connect", side_effect=AssertionError("migrating connect")):
            with _assert_projects_readonly():
                result = address.resolve("Home", lister=_raising_lister)
        self.assertEqual(result["session_id"], "local-home")
        self.assertEqual(_sha256(path), before)
        self.assertTrue(address.check_project_addressing())

    def test_slug_read_error_is_unavailable(self):
        self.enable_projects()
        self.make_projects([{"slug": "reviewer", "owner": "local-x"}])
        with mock.patch.object(address, "_unarchived_project_rows", side_effect=sqlite3.OperationalError("boom")):
            result = address.resolve("@reviewer", lister=_raising_lister)
        self.assertEqual(result["error"], "project-addressing-unavailable")

    def test_register_snapshot_does_not_stick_for_resolve(self):
        self.enable_projects()
        self.make_projects([{"slug": "reviewer", "owner": None}])
        self.open_writer()
        self.add_session("local-orch", title=PLANNER, started_at=1)
        self.close_writer()
        plugin = _load_plugin()
        try:
            ctx = _Ctx()
            plugin.register(ctx)
            # The reply hook and the echo hooks: with inbound "never" no legacy drain is registered.
            self.assertEqual([hook[0] for hook in ctx.hooks],
                             ["post_llm_call", "pre_llm_call", "pre_api_request"])
            echo = sys.modules[plugin.__name__ + ".echo"]
            self.assertEqual([hook[1] for hook in ctx.hooks[1:]],
                             [echo.on_pre_llm_call, echo.on_pre_api_request])
            self.assertIn(
                "@<project-slug> (that project's owner session)",
                ctx.tools[0]["schema"]["description"],
            )
            addr = sys.modules[plugin.__name__ + ".address"]
            self.assertTrue(addr.registered_project_addressing)
            self.write_config(enabled=[], plugin="projects")
            result = addr.resolve("@reviewer", lister=_raising_lister)
            self.assertEqual(result["error"], "project-addressing-unavailable")
            self.assertEqual(result["message"], UNAVAILABLE)
            self.assertNotIn("local-orch", repr(result))
            self.assertTrue(addr.registered_project_addressing)
            self.assertFalse(addr.check_project_addressing())
        finally:
            name = plugin.__name__
            for key in list(sys.modules):
                if key == name or key.startswith(name + "."):
                    sys.modules.pop(key, None)

    def test_list_filters_runs_query_limit_and_states(self):
        rows = []
        for index in range(31):
            rows.append({
                "id": f"local-row-{index:02d}",
                "title": f"Notes {index}",
                "source": "cli",
                "running": False,
                "last_active_at": float(index),
                "workspace": "",
            })
        rows.append({"id": "local-cron", "title": "Nightly", "source": " cron ", "running": False, "last_active_at": 100})
        rows.append({"id": "local-sub", "title": "Child", "source": "subagent", "running": False, "last_active_at": 101})
        rows.append({"id": "local-once", "title": "Once", "source": "oneshot", "running": False, "last_active_at": 102})
        rows.append({"id": "local-cronjob", "title": "Keep", "source": "cronjob", "running": False, "last_active_at": 39})
        rows.append({
            "id": "local-live-run", "title": "Busy", "source": "cli", "running": False,
            "last_active_at": 40, "workspace": "",
        })
        rows.append({
            "id": "local-stored-run", "title": "Flag", "source": "cli", "running": True,
            "last_active_at": 41, "workspace": "",
        })
        rows.append({
            "id": "local-closed", "title": "Done", "source": "cli", "running": "true",
            "last_active_at": 42, "workspace": "",
        })
        rows.append({
            "id": "local-undated", "title": "Old", "source": "cli", "running": False,
            "last_active_at": None, "workspace": "",
        })
        live = [{"id": "local-live-run", "running": True, "last_active_at": 1}]
        calls = []
        listed = address.list_sessions(lister=_recording_lister(rows, live, calls))
        self.assertTrue(listed["ok"])
        self.assertEqual(calls, [("stored", True), ("live", True)])
        found = [item["id"] for item in listed["sessions"]]
        self.assertEqual(len(found), 30)
        self.assertEqual(found[0], "local-closed")
        self.assertNotIn("local-row-00", found)
        self.assertNotIn("local-cron", found)
        self.assertNotIn("local-sub", found)
        self.assertNotIn("local-once", found)
        self.assertNotIn("local-undated", found)
        by_id = {item["id"]: item for item in listed["sessions"]}
        self.assertEqual(by_id["local-live-run"]["state"], "running")
        self.assertEqual(by_id["local-stored-run"]["state"], "running")
        self.assertEqual(by_id["local-closed"]["state"], "closed")
        idle = address.list_sessions(
            lister=_quiet_lister(
                [{"id": "local-idle", "title": "Quiet", "source": "cli", "running": False, "last_active_at": 1}],
                [{"id": "local-idle", "running": False}],
            ),
        )
        self.assertEqual(idle["sessions"][0]["state"], "idle")
        self.assertNotIn("mark", idle["sessions"][0])
        with_runs = address.list_sessions(include_runs=True, lister=_quiet_lister(rows, live))
        run_ids = {item["id"] for item in with_runs["sessions"]}
        self.assertIn("local-cron", run_ids)
        self.assertIn("local-sub", run_ids)
        self.assertIn("local-once", run_ids)
        self.assertIn("local-cronjob", {item["id"] for item in listed["sessions"]})
        limited = address.list_sessions(limit=2, lister=_quiet_lister(rows, live))
        self.assertEqual([item["id"] for item in limited["sessions"]], ["local-closed", "local-stored-run"])
        as_bool = address.list_sessions(limit=True, lister=_quiet_lister(rows, live))
        self.assertEqual(len(as_bool["sessions"]), 30)
        self.assertEqual(address.list_sessions(limit=0, lister=_quiet_lister(rows, live))["sessions"], [])
        negative = address.list_sessions(limit=-3, lister=_quiet_lister(rows, live))
        self.assertEqual(len(negative["sessions"]), 30)
        blank = address.list_sessions(query="   ", lister=_quiet_lister(rows, live))
        self.assertEqual(len(blank["sessions"]), 30)

    def test_list_query_matches_title_or_project_not_id(self):
        work = self.home / "work"
        review = work / "review"
        rows = [
            {"id": "review-session-id", "title": "Notes", "source": "cli", "running": False,
             "last_active_at": 2, "workspace": str(work / "other" / "app")},
            {"id": "local-notes", "title": "Plain", "source": "cli", "running": False,
             "last_active_at": 3, "workspace": str(review)},
        ]
        self.make_projects(
            [{"slug": "reviewer", "name": "Reviewer", "id": "p1"}],
            owner_column=False,
            folders=[("p1", str(review))],
        )
        self.write_config(enabled=[], plugin=None)
        listed = address.list_sessions(query="REVIEW", lister=_quiet_lister(rows, []))
        self.assertEqual([item["id"] for item in listed["sessions"]], ["local-notes"])
        self.assertEqual(listed["sessions"][0]["project"], "Reviewer")
        missed = address.list_sessions(query="review-session", lister=_quiet_lister(rows, []))
        self.assertEqual(missed["sessions"], [])
        titled = address.list_sessions(query="notes", lister=_quiet_lister(rows, []))
        self.assertEqual([item["id"] for item in titled["sessions"]], ["review-session-id"])

    def test_list_project_from_folders_without_the_plugin(self):
        work = self.home / "work"
        review = work / "review"
        web = review / "web"
        slug_only = work / "slug-only"
        archived = work / "archived-root"
        self.make_projects(
            [
                {"id": "p-review", "slug": "reviewer", "name": "Reviewer"},
                {"id": "p-web", "slug": "review-web", "name": "Review Web"},
                {"id": "p-slug", "slug": "slug-name", "name": "  "},
                {"id": "p-old", "slug": "old", "name": "Old Name", "archived": True},
            ],
            owner_column=False,
            folders=[
                ("p-review", str(review)),
                ("p-web", str(web)),
                ("p-slug", str(slug_only)),
                ("p-old", str(archived)),
            ],
        )
        self.write_config(enabled=[], plugin="projects")
        rows = [
            {"id": "s-web", "title": "Web", "source": "cli", "workspace": str(web / "src"), "last_active_at": 5},
            {"id": "s-review", "title": "Root", "source": "cli", "workspace": str(review / "src"), "last_active_at": 4},
            {"id": "s-near", "title": "Near", "source": "cli", "workspace": str(work / "reviewer"), "last_active_at": 3},
            {"id": "s-app", "title": "App", "source": "cli", "workspace": str(work / "other" / "app"), "last_active_at": 2},
            {"id": "s-empty", "title": "Empty", "source": "cli", "workspace": "  ", "last_active_at": 1},
            {"id": "s-slug", "title": "Slug", "source": "cli", "workspace": str(slug_only), "last_active_at": 6},
            {"id": "s-old", "title": "Old", "source": "cli", "workspace": str(archived / "src"), "last_active_at": 7},
        ]
        listed = address.list_sessions(lister=_quiet_lister(rows, []))
        by_id = {item["id"]: item for item in listed["sessions"]}
        self.assertEqual(by_id["s-web"]["project"], "Review Web")
        self.assertEqual(by_id["s-review"]["project"], "Reviewer")
        self.assertEqual(by_id["s-near"]["project"], "reviewer")
        self.assertEqual(by_id["s-app"]["project"], "app")
        self.assertEqual(by_id["s-empty"]["project"], "")
        self.assertEqual(by_id["s-slug"]["project"], "slug-name")
        self.assertEqual(by_id["s-old"]["project"], "Old Name")
        self.assertFalse(address.check_project_addressing())

    def test_list_project_read_error_uses_the_folder_name(self):
        path = self.home / "projects.db"
        path.write_bytes(b"not a sqlite database\n")
        rows = [{
            "id": "s-app", "title": "App", "source": "cli", "running": False,
            "last_active_at": 1, "workspace": str(self.home / "other" / "app"),
        }]
        listed = address.list_sessions(lister=_quiet_lister(rows, []))
        self.assertEqual(listed["sessions"][0]["project"], "app")

    def test_list_id_is_the_compression_tip_and_marks_sender(self):
        self.open_writer()
        self.add_session("local-parent", title="Parent", started_at=1)
        self.db.end_session("local-parent", "compression")
        self.add_session("local-child", title="Child title", parent="local-parent", started_at=2)
        self.close_writer()
        rows = [
            {"id": "local-parent", "title": "Parent", "source": "cli", "running": False,
             "last_active_at": 9, "workspace": ""},
            {"id": "local-other", "title": "Other", "source": "cli", "running": False,
             "last_active_at": 8, "workspace": ""},
        ]
        listed = address.list_sessions(sender="local-parent", lister=_quiet_lister(rows, []))
        by_id = {item["id"]: item for item in listed["sessions"]}
        self.assertIn("local-child", by_id)
        self.assertNotIn("local-parent", by_id)
        self.assertEqual(by_id["local-child"]["mark"], "(this session)")
        self.assertEqual(by_id["local-child"]["title"], "Parent")
        self.assertNotIn("(this session)", by_id["local-child"]["title"])
        self.assertNotIn("mark", by_id["local-other"])
        marked_child = address.list_sessions(sender=" local-child ", lister=_quiet_lister(rows, []))
        self.assertEqual(marked_child["sessions"][0]["mark"], "(this session)")

    def test_list_errors_come_from_stored_then_live(self):
        def stored_fails(scope, picker=False):
            if scope == "live":
                raise AssertionError("live was called")
            return {"ok": False, "error": "no-owner"}

        self.assertEqual(address.list_sessions(lister=stored_fails), {"ok": False, "error": "no-owner"})

        def live_fails(scope, picker=False):
            if scope == "stored":
                return {"ok": True, "sessions": []}
            return {"ok": False, "error": "not-reachable"}

        self.assertEqual(
            address.list_sessions(lister=live_fails),
            {"ok": False, "error": "not-reachable"},
        )

    def test_list_dedupes_a_shared_tip_and_ors_state(self):
        self.open_writer()
        self.add_session("local-root", started_at=1)
        self.db.end_session("local-root", "compression")
        self.add_session("local-tip", parent="local-root", started_at=2)
        self.close_writer()
        rows = [
            {"id": "local-root", "title": "Older", "source": "cli", "running": False, "last_active_at": 1},
            {"id": "local-tip", "title": "Newer", "source": "cli", "running": True, "last_active_at": 5},
        ]
        listed = address.list_sessions(lister=_quiet_lister(rows, []))
        self.assertEqual(len(listed["sessions"]), 1)
        item = listed["sessions"][0]
        self.assertEqual(item["id"], "local-tip")
        self.assertEqual(item["title"], "Newer")
        self.assertEqual(item["last_active"], 5)
        self.assertEqual(item["state"], "running")

    def test_list_keeps_live_sessions_missing_from_the_stored_page(self):
        # The owner's stored list is one page of recent sessions; busy cron runs can push
        # an older, still-open chat off it. The live list must still bring it in.
        stored = [
            {"id": "local-cron-new", "title": "Check-in", "source": "cron", "running": False, "last_active_at": 9},
            {"id": "local-paged", "title": "Paged", "source": "cli", "running": False, "last_active_at": 8},
        ]
        live = [
            {"id": "local-old-chat", "session_id": "local-old-chat", "title": "Old chat", "source": "tui",
             "running": True, "last_active_at": 7, "workspace": ""},
            {"id": "local-old-idle", "title": "Open idle", "source": "cli", "running": False},
            {"id": "local-old-run", "title": "Build run", "source": "oneshot", "running": True},
            {"id": "local-paged", "title": "Paged", "source": "cli", "running": True},
        ]
        listed = address.list_sessions(lister=_quiet_lister(stored, live))
        by_id = {item["id"]: item for item in listed["sessions"]}
        self.assertEqual(by_id["local-old-chat"]["state"], "running")
        self.assertEqual(by_id["local-old-chat"]["title"], "Old chat")
        self.assertEqual(by_id["local-old-chat"]["source"], "tui")
        self.assertEqual(by_id["local-old-idle"]["state"], "idle")
        self.assertEqual(by_id["local-paged"]["state"], "running")
        self.assertNotIn("local-old-run", by_id)
        self.assertNotIn("local-cron-new", by_id)
        self.assertEqual([item["id"] for item in listed["sessions"]].count("local-paged"), 1)
        # With its last activity from the live row, it sorts by recency and survives the limit.
        top = address.list_sessions(limit=2, lister=_quiet_lister(stored, live))
        self.assertEqual([item["id"] for item in top["sessions"]], ["local-paged", "local-old-chat"])
        with_runs = address.list_sessions(include_runs=True, lister=_quiet_lister(stored, live))
        run_ids = {item["id"]: item["state"] for item in with_runs["sessions"]}
        self.assertEqual(run_ids["local-old-run"], "running")
        self.assertIn("local-cron-new", run_ids)

    def test_list_keeps_a_running_live_only_chat_past_the_default_limit(self):
        stored = [
            {"id": f"local-stored-{index:02d}", "title": f"Stored {index}", "source": "cli",
             "running": False, "last_active_at": float(index), "workspace": ""}
            for index in range(40)
        ]
        # Shaped like the owner's live picker row: last_active_at comes from picker fields.
        live = [{"session_id": "local-busy", "id": "local-busy", "title": "Busy", "source": "tui",
                 "started_at": 1.0, "message_count": 9, "model": "m", "running": True,
                 "last_active_at": 50.0, "workspace": ""}]
        listed = address.list_sessions(lister=_quiet_lister(stored, live))
        self.assertEqual(len(listed["sessions"]), 30)
        self.assertEqual(listed["sessions"][0]["id"], "local-busy")
        self.assertEqual(listed["sessions"][0]["state"], "running")

    def test_default_lister_calls_client_list_sessions(self):
        import client

        seen = []

        def fake(scope="live", picker=False):
            seen.append((scope, picker))
            return {"ok": True, "sessions": []}

        with mock.patch.object(client, "list_sessions", fake):
            result = address.list_sessions()
        self.assertEqual(result, {"ok": True, "sessions": []})
        self.assertEqual(seen, [("stored", True), ("live", True)])

    def test_live_tip_matches_a_stored_root(self):
        self.open_writer()
        self.add_session("local-root", title="Root", started_at=1)
        self.db.end_session("local-root", "compression")
        self.add_session("local-tip", parent="local-root", started_at=2)
        self.close_writer()
        rows = [{
            "id": "local-root", "title": "Root", "source": "cli", "running": False, "last_active_at": 4,
        }]
        live = [{"id": "local-tip", "running": False}]
        listed = address.list_sessions(lister=_quiet_lister(rows, live))
        self.assertEqual(listed["sessions"][0]["id"], "local-tip")
        self.assertEqual(listed["sessions"][0]["state"], "idle")

    def test_none_last_active_sorts_last(self):
        rows = [
            {"id": "s-none", "title": "None", "source": "cli", "last_active_at": None},
            {"id": "s-new", "title": "New", "source": "cli", "last_active_at": 2},
            {"id": "s-old", "title": "Old", "source": "cli", "last_active_at": 1},
        ]
        listed = address.list_sessions(lister=_quiet_lister(rows, []))
        self.assertEqual([item["id"] for item in listed["sessions"]], ["s-new", "s-old", "s-none"])


class PrivateImportTests(unittest.TestCase):
    def test_plugin_imports_only_published_names(self):
        from hermes_cli.plugin_validate_private_imports import private_import_findings

        self.assertEqual(private_import_findings(PACKAGE_DIR), [])


class _Ctx:
    def __init__(self):
        self.tools = []
        self.hooks = []

    def get_config(self, key, default=None):
        if key == "inbound":
            return "never"
        return default

    def register_tool(self, **kwargs):
        self.tools.append(kwargs)

    def register_hook(self, *args, **kwargs):
        self.hooks.append(args)

    def register_cli_command(self, *args, **kwargs):
        return None


def _load_plugin():
    name = "hermes_intercom_test_address"
    spec = importlib.util.spec_from_file_location(
        name,
        PACKAGE_DIR / "__init__.py",
        submodule_search_locations=[str(PACKAGE_DIR)],
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _raising_lister(scope, picker=False):
    raise AssertionError(f"lister called for {scope}")


def _quiet_lister(stored=None, live=None):
    stored = [] if stored is None else stored
    live = [] if live is None else live

    def lister(scope, picker=False):
        if scope == "stored":
            return {"ok": True, "sessions": stored}
        if scope == "live":
            return {"ok": True, "sessions": live}
        raise AssertionError(scope)

    return lister


def _recording_lister(stored, live, calls):
    def lister(scope, picker=False):
        calls.append((scope, picker))
        if scope == "stored":
            return {"ok": True, "sessions": stored}
        if scope == "live":
            return {"ok": True, "sessions": live}
        raise AssertionError(scope)

    return lister


class _assert_projects_readonly:
    def __enter__(self):
        self.seen = []
        self._real = sqlite3.connect

        def wrapped(*args, **kwargs):
            target = args[0] if args else kwargs.get("database")
            if isinstance(target, str) and "projects.db" in target:
                if "mode=ro" not in target or kwargs.get("uri") is not True:
                    raise AssertionError(f"projects.db opened without mode=ro: {target!r} {kwargs!r}")
                self.seen.append(target)
            return self._real(*args, **kwargs)

        self._patch = mock.patch("sqlite3.connect", wrapped)
        self._patch.start()
        return self.seen

    def __exit__(self, exc_type, exc, tb):
        self._patch.stop()
        if exc_type is None and not self.seen:
            raise AssertionError("projects.db was not opened")
        return False


if __name__ == "__main__":
    unittest.main()
