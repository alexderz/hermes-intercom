"""Addressing: resolve a ``to`` string to one session, and list sessions (README, "The tool").

The first rule that matches wins: an exact title (including ``Home``), then a session id
or id prefix, then ``@<slug>`` when project addressing is available. A successful id is
then the compression tip. Anything else is a refusal, with at most five candidates.
``list_sessions`` is the discovery side: id, title, project, last active and state.
"""

from __future__ import annotations

import difflib
import os
import sqlite3
from pathlib import Path

# Fixed sentences: tests and receipts compare them word for word. Refusals do not add a "Nothing was sent." prefix; that is the route.
PROJECT_UNAVAILABLE = (
    "Project addressing is not available on this Hermes. Use "
    "list (filter by project) and address the session by id."
)
UNKNOWN_MESSAGE = "No session matches that address."
UNKNOWN_SLUG_MESSAGE = "No project matches that slug."
AMBIGUOUS_MESSAGE = "That address matches more than one session."

MAX_CANDIDATES = 5
DEFAULT_LIMIT = 30
FUZZY_CUTOFF = 0.6
RUN_SOURCES = frozenset({"cron", "subagent", "oneshot"})
_PREFIX_PAGE = 200

# Snapshot taken at register() for the tool text, which is fixed for the process.
# resolve() does not read it: it checks again on every call.
registered_project_addressing: bool | None = None


def no_owner_message(slug: str) -> str:
    return (
        f"Project @{slug} has no owner session set. "
        "Address a session by id (from list)."
    )


def check_project_addressing() -> bool:
    """True only when the configured project plugin is enabled and ``owner_session_id`` exists.

    Never raises. A missing ``projects.db`` or any read error means not available. The
    config and the database are read fresh on every call.
    """
    try:
        plugin_id, enabled = _plugin_membership()
        if not plugin_id or plugin_id not in enabled:
            return False
        return _projects_have_owner_column()
    except Exception:  # health: allow BLE001 -- any read error means project addressing is unavailable
        return False


def note_registration() -> bool:
    """Record the check at ``register()``. Later resolves re-check and ignore the snapshot."""
    global registered_project_addressing
    registered_project_addressing = check_project_addressing()
    return registered_project_addressing


def resolve(to, *, lister=None) -> dict:
    """Resolve ``to`` to one session, or a refusal with candidates.

    Success is ``{"ok": True, "session_id", "title"}`` where ``session_id`` is the
    compression tip. A refusal is ``{"ok": False, "error", "message", "candidates"}``.
    The lister (``session.list``) is used only to decorate session candidates.
    """
    text = to.strip() if isinstance(to, str) else ""
    # Re-read on every resolve, including addresses that are not project slugs.
    available = check_project_addressing()
    # Without the feature, "@…" is never tried as a title or an id.
    if text.startswith("@") and not available:
        return _refusal("project-addressing-unavailable", PROJECT_UNAVAILABLE)
    if not text:
        return _refusal("unknown", UNKNOWN_MESSAGE)

    db = _open_state()
    try:
        if db is not None:
            titled = db.get_session_by_title(text)
            if titled and titled.get("id"):
                return _ok(db, titled["id"], titled.get("title") or text)
            matched = _match_id(db, text, lister)
            if matched is not None:
                return matched
        if text.startswith("@") and available:
            return _resolve_slug(db, text[1:].strip(), lister)
        return _unknown_titles(db, text, lister)
    finally:
        if db is not None:
            db.close()


def list_sessions(*, include_runs=False, query=None, limit=None, sender=None, lister=None) -> dict:
    """Discovery listing: id (compression tip), title, project, last active, state.

    Rows come from the lister: stored ``session.list`` with ``picker`` true, then live
    ``session.list``. ``cron``, ``subagent`` and ``oneshot`` are left out unless
    ``include_runs`` is truthy. ``query`` is a case-insensitive substring of title or
    project. Newest ``last_active`` first; ``limit`` defaults to 30. The sender's own
    session is kept and marked ``(this session)``.
    """
    if lister is None:
        lister = _owner_lister
    stored = lister("stored", picker=True)
    if not isinstance(stored, dict) or stored.get("ok") is not True:
        return stored if isinstance(stored, dict) else {"ok": False, "error": "list-failed"}
    live = lister("live", picker=False)
    if not isinstance(live, dict) or live.get("ok") is not True:
        return live if isinstance(live, dict) else {"ok": False, "error": "list-failed"}

    db = _open_state()
    try:
        projects = _project_index()
        live_index = _live_index(_rows_of(live), db)
        merged: dict[str, dict] = {}
        for row in _rows_of(stored):
            source = row.get("source")
            source = source.strip() if isinstance(source, str) else ""
            if source in RUN_SOURCES and not include_runs:
                continue
            raw = _primary_id(row)
            if not raw:
                continue
            tip = _tip(db, raw)
            item = _list_item(row, tip, db, live_index, projects)
            previous = merged.get(tip)
            if previous is None:
                merged[tip] = item
                continue
            if _is_newer(item["last_active"], previous["last_active"]):
                item["state"] = _merge_state(item["state"], previous["state"])
                merged[tip] = item
            else:
                previous["state"] = _merge_state(previous["state"], item["state"])
        sessions = list(merged.values())
        needle = query.strip().casefold() if isinstance(query, str) else ""
        if needle:
            sessions = [
                item for item in sessions
                if needle in item["title"].casefold() or needle in item["project"].casefold()
            ]
        sessions.sort(key=_list_sort_key)
        sessions = sessions[:_coerce_limit(limit)]
        _mark_sender(sessions, sender, db)
        return {"ok": True, "sessions": sessions}
    finally:
        if db is not None:
            db.close()


# ── internals ────────────────────────────────────────────────────────────────


def _refusal(error: str, message: str, candidates: list | None = None) -> dict:
    return {
        "ok": False,
        "error": error,
        "message": message,
        "candidates": list(candidates or []),
    }


def _ok(db, session_id: str, fallback_title) -> dict:
    """Rule 4: a resolved id is the compression tip, not another search."""
    tip = _tip(db, session_id)
    title = None
    if db is not None:
        title = db.get_session_title(tip)
    if not title:
        title = fallback_title
    return {"ok": True, "session_id": tip, "title": title}


def _tip(db, session_id: str) -> str:
    if db is None or not session_id:
        return session_id
    tip = db.get_compression_tip(session_id)
    return tip or session_id


def _match_id(db, text: str, lister):
    """Exact id, else one prefix, else ambiguous when several tips match.

    ``resolve_session_id`` returns an id for an exact match or a unique prefix, and
    ``None`` for both zero and several matches. An exact id wins even when it is a
    prefix of other ids. Several rows that compress to one tip are one session.
    """
    found = db.resolve_session_id(text)
    if found:
        return _ok(db, found, db.get_session_title(found))
    if not text:
        return None
    rows = _prefix_matches(db, text)
    if not rows:
        return None
    order: list[str] = []
    titles: dict[str, str] = {}
    for row in rows:
        tip = _tip(db, row["id"])
        title = db.get_session_title(tip) or row.get("title") or ""
        # Several prefix rows can compress to one tip. Keep a title from the chain
        # when the tip itself has none (the title often stays on the parent).
        if tip in titles:
            if not titles[tip] and title:
                titles[tip] = title
            continue
        titles[tip] = title
        order.append(tip)
    if len(order) == 1:
        return _ok(db, order[0], titles[order[0]])
    candidates = [{"id": tip, "title": titles[tip]} for tip in order[:MAX_CANDIDATES]]
    return _refusal("ambiguous", AMBIGUOUS_MESSAGE, _decorate(db, candidates, lister))


def _resolve_slug(db, slug_text: str, lister) -> dict:
    conn = _open_projects()
    if conn is None:
        return _refusal("project-addressing-unavailable", PROJECT_UNAVAILABLE)
    try:
        try:
            if not _has_owner_column(conn):
                return _refusal("project-addressing-unavailable", PROJECT_UNAVAILABLE)
            rows = _unarchived_project_rows(conn)
        except sqlite3.Error:
            return _refusal("project-addressing-unavailable", PROJECT_UNAVAILABLE)
    finally:
        conn.close()

    slugs = []
    chosen = None
    for row in rows:
        slug = row["slug"]
        if not isinstance(slug, str):
            continue
        slugs.append(slug)
        if slug == slug_text:
            chosen = row
    if chosen is None:
        closest = [{"slug": slug} for slug in _closest_strings(slug_text, slugs)]
        return _refusal("unknown", UNKNOWN_SLUG_MESSAGE, closest)

    owner = chosen["owner_session_id"]
    if not isinstance(owner, str) or not owner.strip():
        return _refusal("no-owner-set", no_owner_message(slug_text))
    if db is None:
        return _refusal("unknown", UNKNOWN_MESSAGE)
    matched = _match_id(db, owner.strip(), lister)
    if matched is None:
        return _refusal("unknown", UNKNOWN_MESSAGE)
    return matched


def _unknown_titles(db, text: str, lister) -> dict:
    if db is None or not text:
        return _refusal("unknown", UNKNOWN_MESSAGE)
    rows = _titled_rows(db)
    best: dict[str, dict] = {}
    for row in rows:
        title = row["title"]
        if not isinstance(title, str) or not title:
            continue
        score = _score_text(text, title)
        if score is None:
            continue
        tip = _tip(db, row["id"])
        started = row["started_at"] if isinstance(row["started_at"], (int, float)) else 0
        current = best.get(tip)
        candidate = {"id": tip, "title": db.get_session_title(tip) or title, "score": score, "started_at": started}
        if current is None or (score, -started) < (current["score"], -current["started_at"]):
            best[tip] = candidate
    if not best:
        return _refusal("unknown", UNKNOWN_MESSAGE)
    ranked = list(best.values())
    listings = _safe_index(db, lister)
    for item in ranked:
        hit = listings.get(item["id"])
        item["last_active"] = None if hit is None else hit.get("last_active")
    ranked.sort(key=_title_sort_key)
    bare = [{"id": item["id"], "title": item["title"]} for item in ranked[:MAX_CANDIDATES]]
    # Listings were already loaded; decorate from them so a second read is not required.
    return _refusal("unknown", UNKNOWN_MESSAGE, _decorate_from(bare, listings))


def _title_sort_key(item: dict):
    last = item.get("last_active")
    numeric = isinstance(last, (int, float)) and not isinstance(last, bool)
    return (
        item["score"][0],
        item["score"][1],
        not numeric,
        -(last if numeric else 0),
        -(item.get("started_at") or 0),
        item["id"],
    )


def _score_text(query: str, text: str):
    """Substring matches rank above a fuzzy ratio. ``None`` is not close."""
    folded_query = query.casefold()
    folded = text.casefold()
    if not folded_query:
        return None
    if folded_query in folded:
        return (0, abs(len(folded) - len(folded_query)))
    ratio = difflib.SequenceMatcher(None, folded_query, folded).ratio()
    if ratio >= FUZZY_CUTOFF:
        return (1, -ratio)
    return None


def _closest_strings(query: str, values: list[str]) -> list[str]:
    scored = []
    for value in values:
        score = _score_text(query, value)
        if score is None:
            continue
        scored.append((score[0], score[1], value.casefold(), value))
    scored.sort()
    return [item[3] for item in scored[:MAX_CANDIDATES]]


def _decorate(db, candidates: list[dict], lister) -> list[dict]:
    if not candidates:
        return []
    return _decorate_from(candidates, _safe_index(db, lister))


def _decorate_from(candidates: list[dict], listings: dict) -> list[dict]:
    decorated = []
    for candidate in candidates[:MAX_CANDIDATES]:
        hit = listings.get(candidate["id"])
        decorated.append({
            "title": candidate.get("title") or "",
            "id": candidate["id"],
            "last_active": None if hit is None else hit.get("last_active"),
            "availability": "not open" if hit is None else hit["availability"],
        })
    return decorated


def _safe_index(db, lister) -> dict:
    """Session-list rows keyed by id and compression tip. Failures become an empty index."""
    if lister is None:
        lister = _owner_lister
    try:
        stored = lister("stored", picker=True)
        live = lister("live", picker=False)
    except Exception:  # health: allow BLE001 -- a listing failure is an empty index, not an error for resolve
        return {}
    stored_rows = _ok_rows(stored)
    live_rows = _ok_rows(live)
    if stored_rows is None or live_rows is None:
        return {}
    buckets: dict[str, list] = {}

    def add(row: dict, is_live: bool) -> None:
        for key in _row_keys(row, db):
            buckets.setdefault(key, []).append((row, is_live))

    for row in stored_rows:
        add(row, False)
    for row in live_rows:
        add(row, True)
    return {key: _summarize_listing(pairs) for key, pairs in buckets.items()}


def _summarize_listing(pairs: list) -> dict:
    # A candidate is open when its id is in the live list, and not open otherwise.
    live_rows = [row for row, is_live in pairs if is_live]
    last = _first_last_active(row for row, is_live in pairs if not is_live)
    if last is None:
        last = _first_last_active(live_rows)
    return {"last_active": last, "availability": "open" if live_rows else "not open"}


def _first_last_active(rows) -> object:
    for row in rows:
        if row.get("last_active_at") is not None:
            return row.get("last_active_at")
    return None


def _ok_rows(payload) -> list | None:
    if not isinstance(payload, dict) or payload.get("ok") is not True:
        return None
    rows = payload.get("sessions")
    if rows is None:
        return []
    if not isinstance(rows, list):
        return None
    return [row for row in rows if isinstance(row, dict)]


def _rows_of(payload) -> list:
    rows = _ok_rows(payload)
    return rows or []


def _row_keys(row: dict, db) -> list[str]:
    keys: list[str] = []
    for field in ("id", "session_id", "resolved_id"):
        value = row.get(field)
        if not isinstance(value, str) or not value.strip():
            continue
        ident = value.strip()
        if ident not in keys:
            keys.append(ident)
        tip = _tip(db, ident)
        if tip and tip not in keys:
            keys.append(tip)
    return keys


def _primary_id(row: dict) -> str:
    for field in ("id", "session_id"):
        value = row.get(field)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _list_item(row: dict, tip: str, db, live_index: dict, projects) -> dict:
    matched = _matched_live(row, tip, db, live_index)
    running = row.get("running") is True or any(item.get("running") is True for item in matched)
    if running:
        state = "running"
    elif matched:
        state = "idle"
    else:
        state = "closed"
    last = row.get("last_active_at")
    if last is None:
        for item in matched:
            if item.get("last_active_at") is not None:
                last = item.get("last_active_at")
                break
    title = row.get("title")
    workspace = row.get("workspace")
    # reboot-notice keeps oneshot and drops cron and subagent. The stored row
    # has the source; the public list fields do not, so it has to travel here.
    source = row.get("source")
    source = source.strip() if isinstance(source, str) else ""
    return {
        "id": tip,
        "title": title.strip() if isinstance(title, str) else "",
        "project": _project_for(workspace if isinstance(workspace, str) else "", projects),
        "last_active": last,
        "state": state,
        "source": source,
    }


def _live_index(rows: list, db) -> dict:
    index: dict[str, list] = {}
    for row in rows:
        for key in _row_keys(row, db):
            index.setdefault(key, []).append(row)
    return index


def _matched_live(row: dict, tip: str, db, live_index: dict) -> list:
    seen: set[int] = set()
    matched = []
    keys = _row_keys(row, db)
    if tip and tip not in keys:
        keys.append(tip)
    for key in keys:
        for live in live_index.get(key, []):
            marker = id(live)
            if marker in seen:
                continue
            seen.add(marker)
            matched.append(live)
    return matched


def _merge_state(left: str, right: str) -> str:
    if "running" in (left, right):
        return "running"
    if "idle" in (left, right):
        return "idle"
    return "closed"


def _is_newer(new, old) -> bool:
    if isinstance(new, bool) or isinstance(old, bool):
        return False
    if not isinstance(new, (int, float)):
        return False
    if not isinstance(old, (int, float)):
        return True
    return new > old


def _list_sort_key(item: dict):
    last = item.get("last_active")
    numeric = isinstance(last, (int, float)) and not isinstance(last, bool)
    return (not numeric, -(last if numeric else 0), item.get("id") or "")


def _coerce_limit(limit) -> int:
    # bool is an int subclass; True must not become a limit of 1.
    if isinstance(limit, bool) or not isinstance(limit, int) or limit < 0:
        return DEFAULT_LIMIT
    return limit


def _mark_sender(sessions: list, sender, db) -> None:
    if not isinstance(sender, str) or not sender.strip():
        return
    tip = _tip(db, sender.strip())
    for item in sessions:
        if item["id"] == tip:
            item["mark"] = "(this session)"


def _project_for(workspace: str, projects) -> str:
    text = workspace.strip() if isinstance(workspace, str) else ""
    if not text:
        return ""
    workspace_path = os.path.normpath(text)
    best = None
    if projects:
        for folder, label in projects:
            if not _path_holds(folder, workspace_path):
                continue
            length = len(folder)
            if best is None or length > best[0] or (length == best[0] and label < best[1]):
                best = (length, label)
    if best is not None and best[1]:
        return best[1]
    return Path(workspace_path).name


def _path_holds(folder: str, workspace: str) -> bool:
    """True when ``workspace`` is ``folder`` or a child of it, on a path boundary.

    ``/work/budget`` does not hold ``/work/budgeting``.
    """
    if not folder or folder == ".":
        return False
    try:
        Path(workspace).relative_to(folder)
    except ValueError:
        return False
    return True


def _project_index():
    """``(normpath, label)`` pairs from ``project_folders``, or None when the read fails.

    Discovery only: archived projects stay, and a missing owner column is fine.
    """
    conn = _open_projects()
    if conn is None:
        return None
    try:
        rows = conn.execute(
            "SELECT p.name AS name, p.slug AS slug, f.path AS path "
            "FROM project_folders f JOIN projects p ON p.id = f.project_id"
        ).fetchall()
    except sqlite3.Error:
        return None
    finally:
        conn.close()
    index = []
    for row in rows:
        path = row["path"]
        if not isinstance(path, str) or not path.strip():
            continue
        index.append((os.path.normpath(path.strip()), _project_label(row["name"], row["slug"])))
    return index


def _project_label(name, slug) -> str:
    if isinstance(name, str) and name.strip():
        return name.strip()
    if isinstance(slug, str) and slug.strip():
        return slug.strip()
    return ""


def _plugin_membership() -> tuple[str, set[str]]:
    """One config load. The id is ``plugins.entries.intercom.settings.project_plugin``."""
    from hermes_cli.config import load_config_readonly

    config = load_config_readonly()
    plugins = config.get("plugins") if isinstance(config, dict) else None
    if not isinstance(plugins, dict):
        return "", set()
    entries = plugins.get("entries") if isinstance(plugins.get("entries"), dict) else {}
    intercom = entries.get("intercom") if isinstance(entries.get("intercom"), dict) else {}
    settings = intercom.get("settings") if isinstance(intercom.get("settings"), dict) else {}
    plugin_id = settings.get("project_plugin")
    plugin_id = plugin_id.strip() if isinstance(plugin_id, str) else ""
    enabled_raw = plugins.get("enabled")
    enabled: set[str] = set()
    if isinstance(enabled_raw, list):
        enabled = {item.strip() for item in enabled_raw if isinstance(item, str) and item.strip()}
    return plugin_id, enabled


def _projects_have_owner_column() -> bool:
    conn = _open_projects()
    if conn is None:
        return False
    try:
        return _has_owner_column(conn)
    except sqlite3.Error:
        return False
    finally:
        conn.close()


def _has_owner_column(conn) -> bool:
    rows = conn.execute("PRAGMA table_info(projects)").fetchall()
    for row in rows:
        name = row["name"] if isinstance(row, sqlite3.Row) else row[1]
        if name == "owner_session_id":
            return True
    return False


def _unarchived_project_rows(conn) -> list:
    return list(conn.execute(
        "SELECT slug, owner_session_id FROM projects WHERE COALESCE(archived, 0) = 0"
    ).fetchall())


def _open_projects():
    """Read-only ``projects.db``. Never ``hermes_cli.projects_db.connect`` (that migrates)."""
    from hermes_constants import get_hermes_home

    path = Path(get_hermes_home()) / "projects.db"
    conn = None
    try:
        if not path.is_file():
            return None
        # file: URI so "?" and "#" in the home path stay encoded; mode=ro never writes.
        uri = path.resolve().as_uri() + "?mode=ro"
        conn = sqlite3.connect(uri, uri=True)
        conn.row_factory = sqlite3.Row
        return conn
    except (OSError, sqlite3.Error, ValueError):
        if conn is not None:
            try:
                conn.close()
            except (OSError, sqlite3.Error):
                pass
        return None


def _open_state():
    """Published read-only SessionDB. A missing ``state.db`` is no sessions, not an error."""
    try:
        from hermes_state import SessionDB

        return SessionDB(read_only=True)
    except Exception:  # health: allow BLE001 -- state unreadable means no sessions, not an error
        return None


def _titled_rows(db) -> list[dict]:
    """Every titled session, through the published listing.

    ``list_sessions_rich`` is one page and its search is a substring, so a typo
    is ranked here after the pages are walked.
    """
    offset = 0
    seen: set[str] = set()
    titled = []
    while True:
        page = db.list_sessions_rich(
            include_children=True,
            include_archived=True,
            include_hidden=True,
            include_subagents=True,
            compact_rows=True,
            limit=_PREFIX_PAGE,
            offset=offset,
        )
        if not page:
            break
        fresh = 0
        for row in page:
            sid = row.get("id")
            if not isinstance(sid, str) or sid in seen:
                continue
            seen.add(sid)
            fresh += 1
            title = row.get("title")
            if isinstance(title, str) and title:
                titled.append(row)
        if len(page) < _PREFIX_PAGE or fresh == 0:
            break
        offset += len(page)
    return titled


def _prefix_matches(db, text: str) -> list[dict]:
    """Sessions whose own id starts with *text*, newest ``started_at`` first.

    ``list_sessions_rich(id_query=)`` is a substring over the compression chain,
    one page at a time, so this keeps a real prefix and follows the offset.
    """
    offset = 0
    seen: set[str] = set()
    matched = []
    while True:
        rows = db.list_sessions_rich(
            id_query=text,
            include_children=True,
            include_archived=True,
            include_hidden=True,
            include_subagents=True,
            order_by_last_active=True,
            limit=_PREFIX_PAGE,
            offset=offset,
        )
        if not rows:
            break
        fresh = 0
        for row in rows:
            sid = row.get("id")
            if not isinstance(sid, str) or sid in seen:
                continue
            seen.add(sid)
            fresh += 1
            if sid.startswith(text):
                matched.append(row)
        if len(rows) < _PREFIX_PAGE or fresh == 0:
            break
        offset += len(rows)
    matched.sort(key=_started_desc)
    return matched


def _started_desc(row: dict):
    started = row.get("started_at")
    numeric = isinstance(started, (int, float)) and not isinstance(started, bool)
    return (not numeric, -(started if numeric else 0), row.get("id") or "")


def _owner_lister(scope: str = "live", picker: bool = False) -> dict:
    """Production lister: ``client.list_sessions``. Tests pass their own."""
    try:
        from . import client as owner_client
    except ImportError:
        import client as owner_client
    return owner_client.list_sessions(scope=scope, picker=picker)
