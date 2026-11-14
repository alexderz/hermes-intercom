"""intercom record — one owner-only SQLite file for every message (README, "Trust model").

``plugins.plugin_storage.plugin_db("intercom", "record.db")``: WAL, ``busy_timeout``
5000 ms. ``plugin-data/intercom/`` is ``0700`` on every open. Only the first open
runs under ``umask 077``: it creates ``record.db`` ``O_EXCL`` ``0600`` and the
schema write that makes ``-wal`` and ``-shm``. That set/restore is serialised,
because the umask is process-wide. Later opens do not change it; they ``chmod``
the files, and SQLite gives the sidecars the database file's mode.

In the ``messages`` table, ``reply_mode`` checks ``none``, ``requested``,
``waiting`` and ``optional``. There is no ``expect_reply`` column.
``reply_state`` is ``NULL``, ``sent``, ``silent``, ``stored`` or ``no-block``.

A passive note is a ``messages`` row with ``requested='passive'`` and result
``pending`` (later ``delivered`` or ``replaced``, ``detail='replaced by <id>'``).
``note_key`` is its key, ``offered_turn`` the turn that was offered it. A note to
``all`` is one row with ``address='all'`` and no target. ``note_seen`` keeps, per
lineage root and key, which ``all`` version a session was offered and when that
was confirmed. ``sender_profile`` is NULL for the sender's own profile.

A keyed queued message is a ``pending`` row with ``requested='queue'`` and a
wake marker: ``wake_admission``, the submission id of the wake line its send (or
the send it inherited from) put in Hermes's queue, and ``wake_instance``, the
owner instance that took it. A wake is outstanding while its instance is the
current owner's and no turn has been offered the row; a marker of another
instance is dead.

A passive note with a deadline has ``wake_at`` (``created_at + within`` minutes).
Once it is due, the deadline sweep (``deadline.py``) ``claim_wake``s it: the same
marker, written with ``wake_claimed_at``, then ``wake_fired`` (the owner's
``admission_id``) or ``release_wake``. The wake's turn consumes it
(``wake_turn``, ``woken_at``) in ``offer_notes``. A consumed wake is never sent
again. Older files get these columns from the guarded migration in
``connect()``.

Two writers share the file. The sender ``insert``s its row with an empty result,
then ``update``s ``header_sha`` and the delivery result; a passive sender calls
``insert_note``, which marks the held note it replaces and inserts its own row in
one transaction; a keyed queued sender calls ``insert_keyed_queue`` the same way
(inheriting an outstanding wake), then ``wake_submitted`` or ``wake_failed``. The
owner's reply hook ``update``s the reply fields and ``insert``s the new reply row.
``claim_echo`` sets ``echoed_at`` once. At turn start the passive hook
``offer_notes`` (``offered_turn``, or a ``note_seen`` row for an ``all`` note) and
at the turn's first model request ``confirm_notes`` (``delivered_at``). Lookups are ``get`` (id), ``by_span_hash``,
``by_target_lineage``, ``confirmed_in_turn`` and ``due_notes``. Nothing is pruned, retried or
walked through a status ladder.
"""

from __future__ import annotations

import errno
import os
import sqlite3
import stat
import threading
import time
from collections.abc import Sequence

DB_FILENAME = "record.db"
PLUGIN = "intercom"

# The umask is process-wide. The first open's set/restore holds this lock so
# no other record call can save and restore the umask in the middle of it.
_open_lock = threading.Lock()

REPLY_MODES = ("none", "requested", "waiting", "optional")
# NULL is the unset state. These four are the only values a writer may store.
REPLY_STATES = frozenset({None, "sent", "silent", "stored", "no-block"})

# What the sender may record after insert, and what the reply hook may record.
# echoed_at is only the echo claim. There is no retry count and no status column.
_UPDATE_FIELDS = frozenset({
    "header_sha", "delivery_path", "admission_id",
    "applied", "applied_detail", "result", "detail", "delivered_at",
    "reply", "reply_state", "replied_at", "reply_message_id",
    "offered_turn",
})

QUEUE = "queue"

PASSIVE = "passive"
ALL = "all"

_SCHEMA = """\
CREATE TABLE IF NOT EXISTS messages (
  id TEXT PRIMARY KEY,
  kind TEXT NOT NULL,
  in_reply_to TEXT,
  reply_message_id TEXT,
  request_id TEXT,
  sender_session_id TEXT,
  sender_label TEXT NOT NULL,
  sender_source TEXT NOT NULL,
  target_session_id TEXT,
  target_title TEXT,
  address TEXT NOT NULL,
  delivery_path TEXT,
  admission_id TEXT,
  body TEXT NOT NULL,
  header_sha TEXT,
  sender_chat_scope TEXT,
  target_chat_scope TEXT,
  reply_mode TEXT NOT NULL CHECK (reply_mode IN ('none','requested','waiting','optional')),
  requested TEXT NOT NULL,
  applied TEXT,
  applied_detail TEXT,
  result TEXT,
  detail TEXT,
  reply TEXT,
  reply_state TEXT,
  created_at REAL NOT NULL,
  delivered_at REAL,
  replied_at REAL,
  echoed_at REAL,
  note_key TEXT,
  offered_turn TEXT,
  sender_profile TEXT,
  wake_admission TEXT,
  wake_instance TEXT,
  wake_at REAL,
  wake_claimed_at REAL,
  wake_turn TEXT,
  woken_at REAL
)
"""

# Columns added after the first release. connect() adds any an older file lacks.
_ADDED_COLUMNS = (
    ("note_key", "TEXT"),
    ("offered_turn", "TEXT"),
    ("sender_profile", "TEXT"),
    ("wake_admission", "TEXT"),
    ("wake_instance", "TEXT"),
    ("wake_at", "REAL"),
    ("wake_claimed_at", "REAL"),
    ("wake_turn", "TEXT"),
    ("woken_at", "REAL"),
)

# Its own statement: conn.execute runs one statement only.
_NOTE_SEEN_SCHEMA = """\
CREATE TABLE IF NOT EXISTS note_seen (
  root_id TEXT NOT NULL,
  note_key TEXT NOT NULL,
  message_id TEXT NOT NULL,
  turn_id TEXT,
  delivered_at REAL,
  PRIMARY KEY (root_id, note_key)
)
"""


def connect() -> sqlite3.Connection:
    """Open the record. The caller closes it.

    The directory is ``0700`` on every open. Only the first open changes the
    process umask (see ``_open_lock``). Later opens ``chmod`` the files.
    """
    from plugins.plugin_storage import plugin_data_dir, plugin_db

    with _open_lock:
        directory = plugin_data_dir(PLUGIN)
        os.chmod(directory, 0o700)
        db_path = directory / DB_FILENAME
        # Absent inode: this is the first open. A symlink is not absent; it is
        # refused by _create_exclusive without touching the umask.
        creating = _db_missing(db_path)
        if creating:
            old_umask = os.umask(0o077)
        conn = None
        try:
            _create_exclusive(db_path)
            conn = plugin_db(PLUGIN, DB_FILENAME)
            conn.isolation_level = None  # this module's own BEGIN/COMMIT
            conn.row_factory = sqlite3.Row
            # The schema statement is the write that creates -wal and -shm.
            conn.execute(_SCHEMA)
            _migrate(conn)
            conn.execute(_NOTE_SEEN_SCHEMA)
        except BaseException:
            if conn is not None:
                conn.close()
            raise
        finally:
            if creating:
                os.umask(old_umask)
        _tighten(db_path)
        return conn


def insert(
    message_id: str,
    *,
    kind: str,
    sender_label: str,
    sender_source: str,
    address: str,
    body: str,
    reply_mode: str,
    requested: str,
    sender_session_id: str | None = None,
    target_session_id: str | None = None,
    target_title: str | None = None,
    in_reply_to: str | None = None,
    request_id: str | None = None,
    sender_chat_scope: str | None = None,
    target_chat_scope: str | None = None,
    created_at: float | None = None,
    note_key: str | None = None,
    sender_profile: str | None = None,
    wake_at: float | None = None,
) -> None:
    """Insert the row before delivery. ``result`` and ``header_sha`` stay empty.

    A ``reply_mode`` outside the four values fails the table check
    (``sqlite3.IntegrityError``).
    """
    values = _row_values(locals())

    def write(conn: sqlite3.Connection) -> None:
        _insert_row(conn, values, None)

    _run_write(write)


def insert_note(
    message_id: str,
    *,
    lineage: Sequence[str] = (),
    **row,
) -> list[str]:
    """Insert a held note (result ``pending``), replacing the held one with its key.

    One ``BEGIN IMMEDIATE``: a keyed note first marks the ``pending`` row with the
    same ``note_key`` and the same ``requested`` mode ``replaced`` (``detail``
    ``replaced by <message_id>``). For a per-target note that row's target is in
    *lineage* (the target's compression lineage); for an ``all`` note it is the
    pending ``all`` row. Then the new row is inserted. Returns the replaced ids.
    """
    values = _row_values({"message_id": message_id, **row})

    def write(conn: sqlite3.Connection) -> list[str]:
        replaced = [r["id"] for r in _held_with_key(conn, row, lineage)]
        _replace(conn, replaced, message_id)
        _insert_row(conn, values, "pending")
        return replaced

    return _run_write(write)


def insert_keyed_queue(
    message_id: str,
    *,
    lineage: Sequence[str],
    wake_admission: str,
    instance_id: str | None,
    **row,
) -> tuple[list[str], bool]:
    """Insert a keyed queued message (result ``pending``). Returns ``(replaced ids, submit)``.

    One ``BEGIN IMMEDIATE``, as ``insert_note`` (same key, ``requested='queue'``, target in
    *lineage*). When a replaced row's wake is outstanding (its ``wake_instance`` is
    *instance_id*, the owner the send discovered, and no turn has been offered it), the new row
    inherits that marker and *submit* is False. Otherwise the new row's marker is
    (*wake_admission*, *instance_id*) and *submit* is True: the caller submits the wake after
    commit.
    """
    values = list(_row_values({"message_id": message_id, **row}))
    marker = _INSERT_COLUMNS.index("wake_admission")

    def write(conn: sqlite3.Connection) -> tuple[list[str], bool]:
        held = _held_with_key(conn, {**row, "requested": QUEUE}, lineage)
        live = [r for r in held if _wake_outstanding(r, instance_id)]
        if live:
            values[marker:marker + 2] = [live[-1]["wake_admission"], live[-1]["wake_instance"]]
        else:
            values[marker:marker + 2] = [wake_admission, instance_id]
        replaced = [r["id"] for r in held]
        _replace(conn, replaced, message_id)
        _insert_row(conn, tuple(values), "pending")
        return replaced, not live

    return _run_write(write)


def wake_submitted(wake_admission: str, instance_id) -> None:
    """The owner took the wake: its marker names the instance that answered."""
    if not isinstance(instance_id, str) or not instance_id:
        return
    _run_write(lambda conn: conn.execute(
        "UPDATE messages SET wake_instance = ? WHERE wake_admission = ?", (instance_id, wake_admission)))


def wake_failed(wake_admission: str) -> None:
    """The wake was not taken: its marker is dead, so its pending row rides the next turn."""
    _run_write(lambda conn: conn.execute(
        "UPDATE messages SET wake_instance = NULL WHERE wake_admission = ?", (wake_admission,)))


def due_notes(instance_id: str, now: float | None = None) -> list[dict]:
    """The deadline notes due now, oldest first: see ``_DUE``."""
    when = time.time() if now is None else now

    def read(conn: sqlite3.Connection) -> list[dict]:
        return [_as_dict(r) for r in conn.execute(
            f"SELECT * FROM messages WHERE {_DUE} ORDER BY created_at, id",
            (PASSIVE, when, instance_id))]

    return _run_read(read)


def claim_wake(message_id: str, *, instance_id: str, wake_admission: str,
               now: float | None = None) -> bool:
    """Write the wake marker on a deadline note only while it is still due. True when claimed.

    One ``BEGIN IMMEDIATE``: the marker is (*wake_admission*, *instance_id*) with
    ``wake_claimed_at``, so a second sweep finds the wake live and leaves the row alone.
    """
    when = time.time() if now is None else now

    def write(conn: sqlite3.Connection) -> bool:
        cur = conn.execute(
            f"UPDATE messages SET wake_admission = ?, wake_instance = ?, wake_claimed_at = ? "
            f"WHERE id = ? AND {_DUE}",
            (wake_admission, instance_id, when, message_id, PASSIVE, when, instance_id))
        return cur.rowcount == 1

    return _run_write(write)


def wake_fired(message_id: str, wake_admission: str, admission_id, instance_id) -> None:
    """The owner took the deadline wake: the marker gets its admission id and answering instance."""
    def write(conn: sqlite3.Connection) -> None:
        conn.execute("UPDATE messages SET admission_id = ? WHERE id = ? AND wake_admission = ?",
                     (admission_id if isinstance(admission_id, str) else None, message_id,
                      wake_admission))
        if isinstance(instance_id, str) and instance_id:
            conn.execute("UPDATE messages SET wake_instance = ? WHERE id = ? AND wake_admission = ? "
                         "AND wake_turn IS NULL", (instance_id, message_id, wake_admission))

    _run_write(write)


def release_wake(message_id: str, wake_admission: str) -> None:
    """Release an unconsumed claim: the marker is dead, so the note is held and due again.

    ``wake_admission`` stays: a wake line that did arrive is still a wake, never a message.
    """
    _run_write(lambda conn: conn.execute(
        "UPDATE messages SET wake_instance = NULL WHERE id = ? AND wake_admission = ? "
        "AND wake_turn IS NULL", (message_id, wake_admission)))


def update(message_id: str, **fields) -> bool:
    """Update one existing row. True when that row was written.

    Sender fields: ``header_sha``, ``delivery_path``, ``admission_id``,
    ``applied``, ``applied_detail``, ``result``, ``detail``, ``delivered_at``.
    Reply fields: ``reply``, ``reply_state``, ``replied_at``, ``reply_message_id``
    (and ``detail``). ``reply_state`` is ``NULL`` or ``sent``, ``silent``,
    ``stored``, ``no-block``. ``echoed_at`` is not a field of this call.
    """
    if not fields:
        raise ValueError("update needs at least one field")
    unknown = set(fields) - _UPDATE_FIELDS
    if unknown:
        raise ValueError(f"cannot update {', '.join(sorted(unknown))}")
    if "reply_state" in fields and fields["reply_state"] not in REPLY_STATES:
        raise ValueError("reply_state must be NULL, sent, silent, stored or no-block")
    names = list(fields)
    assignments = ", ".join(f"{name} = ?" for name in names)
    values = [fields[name] for name in names]

    def write(conn: sqlite3.Connection) -> bool:
        # names come from _UPDATE_FIELDS, never from outside that set.
        cur = conn.execute(
            f"UPDATE messages SET {assignments} WHERE id = ?",
            (*values, message_id),
        )
        return cur.rowcount == 1

    return _run_write(write)


def claim_echo(message_id: str, echoed_at: float | None = None) -> bool:
    """Set ``echoed_at`` once. True only when this call claimed the row.

    ``UPDATE messages SET echoed_at = ? WHERE id = ? AND echoed_at IS NULL``.
    """
    when = time.time() if echoed_at is None else echoed_at

    def write(conn: sqlite3.Connection) -> bool:
        cur = conn.execute(
            "UPDATE messages SET echoed_at = ? WHERE id = ? AND echoed_at IS NULL",
            (when, message_id),
        )
        return cur.rowcount == 1

    return _run_write(write)


def get(message_id: str) -> dict | None:
    """The row with this id, or None."""
    def read(conn: sqlite3.Connection) -> dict | None:
        row = conn.execute("SELECT * FROM messages WHERE id = ?", (message_id,)).fetchone()
        return _as_dict(row)

    return _run_read(read)


def by_span_hash(header_sha: str) -> dict | None:
    """The row whose ``header_sha`` is this span hash, or None.

    The sender is the only writer of the hash. The earliest row wins if two
    rows ever share one.
    """
    if not isinstance(header_sha, str) or header_sha == "":
        return None

    def read(conn: sqlite3.Connection) -> dict | None:
        row = conn.execute(
            "SELECT * FROM messages WHERE header_sha = ? ORDER BY created_at, id",
            (header_sha,),
        ).fetchone()
        return _as_dict(row)

    return _run_read(read)


def by_target_lineage(session_ids: Sequence[str]) -> list[dict]:
    """Rows whose ``target_session_id`` is in *session_ids*, oldest first.

    *session_ids* is a session and its compression lineage. The caller decides
    which of these rows are reply candidates.
    """
    ids = list(session_ids)
    if not ids:
        return []
    marks = ", ".join("?" for _ in ids)

    def read(conn: sqlite3.Connection) -> list[dict]:
        rows = conn.execute(
            f"""\
            SELECT * FROM messages
            WHERE target_session_id IN ({marks})
            ORDER BY created_at, id
            """,
            ids,
        ).fetchall()
        return [_as_dict(row) for row in rows]

    return _run_read(read)


def offer_notes(turn_id: str, *, lineage: Sequence[str], root_id: str,
                include_all: bool, wake_keys: Sequence[str] = (),
                instance_id: str | None = None, woken: Sequence[str] = ()) -> list[dict]:
    """Offer this turn the held notes of one target lineage, oldest first.

    One ``BEGIN IMMEDIATE``. *woken* are the deadline notes whose wake this turn carries: each
    wake is consumed (``wake_turn = turn_id``, ``woken_at``) unless already consumed.
    Per-target notes: the ``pending`` passive rows whose target is in *lineage*, except a
    deadline note whose wake is live for *instance_id* and not in *woken*; and the
    ``pending`` keyed queued rows whose key is in
    *wake_keys* (this turn carries their wake) or whose wake is not outstanding for
    *instance_id* (a turn was already offered them, or the marker is dead); each
    gets ``offered_turn = turn_id``. With
    *include_all*: per key, the newest ``pending`` ``all`` row, unless its sender
    is in *lineage* or ``note_seen(root_id, key)`` already holds that row
    confirmed; each upserts ``note_seen(root_id, key) = (id, turn_id, NULL)``.
    An offer that is never confirmed is offered again by the next call.
    """
    ids = list(lineage)
    marks = ", ".join("?" for _ in ids)

    def write(conn: sqlite3.Connection) -> list[dict]:
        notes = []
        consumed = set(woken)
        for message_id in consumed:
            conn.execute("UPDATE messages SET wake_turn = ?, woken_at = ? "
                         "WHERE id = ? AND wake_turn IS NULL", (turn_id, time.time(), message_id))
        if ids:
            notes = [r for r in map(_as_dict, conn.execute(
                f"SELECT * FROM messages WHERE requested = ? AND result = 'pending' "
                f"AND delivered_at IS NULL AND target_session_id IN ({marks})",
                (PASSIVE, *ids),
            )) if r["id"] in consumed or not _deadline_live(r, instance_id)]
            keys = set(wake_keys)
            notes.extend(r for r in map(_as_dict, conn.execute(
                f"SELECT * FROM messages WHERE requested = ? AND result = 'pending' "
                f"AND delivered_at IS NULL AND wake_admission IS NOT NULL "
                f"AND target_session_id IN ({marks})",
                (QUEUE, *ids),
            )) if r["note_key"] in keys or not _wake_outstanding(r, instance_id))
        for row in notes:
            conn.execute("UPDATE messages SET offered_turn = ? WHERE id = ?", (turn_id, row["id"]))
            row["offered_turn"] = turn_id
        if include_all:
            newest: dict[str, dict] = {}
            for r in conn.execute(
                "SELECT * FROM messages WHERE address = ? AND target_session_id IS NULL "
                "AND requested = ? AND result = 'pending' AND note_key IS NOT NULL "
                "ORDER BY created_at, id",
                (ALL, PASSIVE),
            ):
                newest[r["note_key"]] = _as_dict(r)
            for key, row in newest.items():
                if row.get("sender_session_id") in ids:
                    continue
                seen = conn.execute(
                    "SELECT message_id, delivered_at FROM note_seen WHERE root_id = ? AND note_key = ?",
                    (root_id, key),
                ).fetchone()
                if seen is not None and seen["message_id"] == row["id"] and seen["delivered_at"] is not None:
                    continue
                conn.execute(
                    "INSERT INTO note_seen (root_id, note_key, message_id, turn_id, delivered_at) "
                    "VALUES (?, ?, ?, ?, NULL) ON CONFLICT (root_id, note_key) DO UPDATE SET "
                    "message_id = excluded.message_id, turn_id = excluded.turn_id, delivered_at = NULL",
                    (root_id, key, row["id"], turn_id),
                )
                notes.append(row)
        notes.sort(key=lambda r: (r["created_at"], r["id"]))
        return notes

    return _run_write(write)


def confirm_notes(turn_id: str, *, root_id: str, delivered_at: float | None = None) -> list[dict]:
    """Mark the notes offered in *turn_id* handed over. Returns the per-target rows confirmed now.

    One ``BEGIN IMMEDIATE``: ``messages`` rows with ``offered_turn = turn_id`` and no
    ``delivered_at`` get it, and result ``delivered`` unless already ``replaced``;
    ``note_seen`` rows of *root_id* offered in *turn_id* get ``delivered_at``.
    """
    when = time.time() if delivered_at is None else delivered_at

    def write(conn: sqlite3.Connection) -> list[dict]:
        rows = [_as_dict(r) for r in conn.execute(
            "SELECT * FROM messages WHERE offered_turn = ? AND delivered_at IS NULL "
            "ORDER BY created_at, id",
            (turn_id,),
        )]
        conn.execute(
            "UPDATE messages SET delivered_at = ?, "
            "result = CASE WHEN result = 'replaced' THEN result ELSE 'delivered' END "
            "WHERE offered_turn = ? AND delivered_at IS NULL",
            (when, turn_id),
        )
        conn.execute(
            "UPDATE note_seen SET delivered_at = ? "
            "WHERE root_id = ? AND turn_id = ? AND delivered_at IS NULL",
            (when, root_id, turn_id),
        )
        return rows

    return _run_write(write)


def confirmed_in_turn(turn_id: str) -> list[dict]:
    """Per-target notes confirmed as handed over in *turn_id*, oldest first."""
    if not isinstance(turn_id, str) or turn_id == "":
        return []

    def read(conn: sqlite3.Connection) -> list[dict]:
        rows = conn.execute(
            "SELECT * FROM messages WHERE offered_turn = ? AND delivered_at IS NOT NULL "
            "ORDER BY created_at, id",
            (turn_id,),
        ).fetchall()
        return [_as_dict(row) for row in rows]

    return _run_read(read)


# ── internals ────────────────────────────────────────────────────────────────

_INSERT_COLUMNS = (
    "id", "kind", "in_reply_to", "sender_session_id", "sender_label", "sender_source",
    "target_session_id", "target_title", "address", "body", "sender_chat_scope",
    "target_chat_scope", "reply_mode", "requested", "request_id", "created_at",
    "note_key", "sender_profile", "wake_admission", "wake_instance", "wake_at",
)


def _held_with_key(conn: sqlite3.Connection, row: dict, lineage: Sequence[str]) -> list[dict]:
    """The ``pending`` rows a keyed *row* replaces: same ``note_key`` and ``requested`` mode, and
    for a per-target row a target in *lineage*, for an ``all`` row the pending ``all`` row."""
    key, ids = row.get("note_key"), list(lineage)
    if not key:
        return []
    if row.get("address") == ALL and not row.get("target_session_id"):
        where, args = "address = ? AND target_session_id IS NULL", [ALL]
    elif ids:
        where, args = f"target_session_id IN ({', '.join('?' for _ in ids)})", ids
    else:
        return []
    return [_as_dict(r) for r in conn.execute(
        f"SELECT * FROM messages WHERE result = 'pending' AND note_key = ? AND requested = ? "
        f"AND {where} ORDER BY created_at, id",
        (key, row.get("requested"), *args),
    )]


def _replace(conn: sqlite3.Connection, ids: Sequence[str], message_id: str) -> None:
    for old in ids:
        conn.execute("UPDATE messages SET result = 'replaced', detail = ? WHERE id = ?",
                     (f"replaced by {message_id}", old))


# A deadline note is due: held, its deadline passed, its wake never consumed and not live.
# Parameters: PASSIVE, now, the owner's instance id.
_DUE = ("requested = ? AND result = 'pending' AND delivered_at IS NULL "
        "AND target_session_id IS NOT NULL AND wake_at IS NOT NULL AND wake_at <= ? "
        "AND wake_turn IS NULL AND (wake_instance IS NULL OR wake_instance != ?)")


def _deadline_live(row: dict, instance_id) -> bool:
    """A deadline note's wake is live: claimed by the current owner and not yet consumed."""
    return (row.get("wake_at") is not None and bool(row.get("wake_admission"))
            and row.get("wake_turn") is None and isinstance(instance_id, str) and instance_id != ""
            and row.get("wake_instance") == instance_id)


def _wake_outstanding(row: dict, instance_id) -> bool:
    """The row's wake is still in the current owner's queue: its instance, and no turn offered."""
    return (isinstance(instance_id, str) and instance_id != ""
            and row.get("wake_instance") == instance_id and row.get("offered_turn") is None)


def _row_values(fields: dict) -> tuple:
    values = dict(fields)
    values["id"] = values.pop("message_id")
    if values.get("created_at") is None:
        values["created_at"] = time.time()
    return tuple(values.get(name) for name in _INSERT_COLUMNS)


def _insert_row(conn: sqlite3.Connection, values: tuple, result) -> None:
    columns = ", ".join((*_INSERT_COLUMNS, "result"))
    marks = ", ".join("?" for _ in range(len(_INSERT_COLUMNS) + 1))
    conn.execute(f"INSERT INTO messages ({columns}) VALUES ({marks})", (*values, result))


def _columns(conn: sqlite3.Connection) -> set:
    return {row[1] for row in conn.execute("PRAGMA table_info(messages)")}


def _migrate(conn: sqlite3.Connection) -> None:
    """Add the columns an older ``messages`` table lacks.

    The gateway and the CLI both open the record, and ``_open_lock`` is per
    process, so the check and the ``ALTER TABLE`` run inside ``BEGIN IMMEDIATE``.
    "duplicate column name" also counts as success.
    """
    if all(name in _columns(conn) for name, _type in _ADDED_COLUMNS):
        return
    conn.execute("BEGIN IMMEDIATE")
    try:
        present = _columns(conn)
        for name, kind in _ADDED_COLUMNS:
            if name in present:
                continue
            try:
                conn.execute(f"ALTER TABLE messages ADD COLUMN {name} {kind}")
            except sqlite3.OperationalError as exc:
                if "duplicate column name" not in str(exc):
                    raise
    except BaseException:
        conn.rollback()
        raise
    conn.commit()


def _db_missing(path) -> bool:
    try:
        os.lstat(path)
    except FileNotFoundError:
        return True
    return False


def _create_exclusive(path) -> None:
    """Create a new ``record.db`` inode ``0600``, or leave a regular file in place.

    ``O_EXCL`` so only a new inode is opened here. A symlink is refused: SQLite
    would follow it and write message bodies somewhere else.
    """
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    try:
        fd = os.open(os.fspath(path), flags, 0o600)
    except FileExistsError:
        if _is_symlink(path):
            raise OSError(errno.ELOOP, "refusing a symlink at the record path", os.fspath(path))
        return
    except OSError as exc:
        if exc.errno == errno.ELOOP or _is_symlink(path):
            raise OSError(errno.ELOOP, "refusing a symlink at the record path", os.fspath(path))
        raise
    else:
        os.close(fd)


def _is_symlink(path) -> bool:
    try:
        return stat.S_ISLNK(os.lstat(path).st_mode)
    except FileNotFoundError:
        return False


def _tighten(db_path) -> None:
    """``chmod 0600`` the database and its sidecars when they are regular files.

    Sidecars appear on the first write and can be recreated on a later open.
    ``chmod`` on the path does not open the file, so it does not drop SQLite's
    locks. A symlink is left alone.
    """
    paths = (
        db_path,
        db_path.with_name(db_path.name + "-wal"),
        db_path.with_name(db_path.name + "-shm"),
    )
    for path in paths:
        try:
            st = os.lstat(path)
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(st.st_mode) or not stat.S_ISREG(st.st_mode):
            continue
        os.chmod(path, 0o600)


def _run_write(fn):
    # No umask change here. Sidecars created by a later write take the database
    # file's mode (SQLite's unix VFS); _tighten chmods them after the commit.
    conn = connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        try:
            result = fn(conn)
        except BaseException:
            conn.rollback()
            raise
        else:
            conn.commit()
            return result
    finally:
        conn.close()
        _tighten(_db_path())


def _run_read(fn):
    conn = connect()
    try:
        return fn(conn)
    finally:
        conn.close()


def _db_path():
    from plugins.plugin_storage import plugin_data_dir
    return plugin_data_dir(PLUGIN) / DB_FILENAME


def _as_dict(row):
    if row is None:
        return None
    return {key: row[key] for key in row.keys()}
