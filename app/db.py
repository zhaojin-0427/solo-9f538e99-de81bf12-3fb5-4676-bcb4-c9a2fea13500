"""SQLite access layer.

All state lives in one SQLite database plus the immutable object store on
disk.  Connections are opened with autocommit (``isolation_level=None``) and
every multi-statement write is wrapped in an explicit ``BEGIN IMMEDIATE``
transaction, which takes the database reserved lock up-front and makes the
register/revoke sequences serialisable across threads and processes.
"""

from __future__ import annotations

import os
import sqlite3

from flask import current_app, g


SCHEMA = """
PRAGMA journal_mode=WAL;

CREATE TABLE IF NOT EXISTS files (
    id            TEXT PRIMARY KEY,
    kind          TEXT NOT NULL CHECK (kind IN ('raw', 'cleaned', 'summary')),
    filename      TEXT NOT NULL,
    content_sha256 TEXT NOT NULL,
    size_bytes    INTEGER NOT NULL,
    stored_name   TEXT NOT NULL UNIQUE,
    uploaded_at   TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    revoked       INTEGER NOT NULL DEFAULT 0 CHECK (revoked IN (0, 1)),
    revoked_at    TEXT,
    revoke_reason TEXT
);

CREATE TABLE IF NOT EXISTS dependencies (
    child_id  TEXT NOT NULL REFERENCES files(id),
    parent_id TEXT NOT NULL REFERENCES files(id),
    position  INTEGER NOT NULL,
    PRIMARY KEY (child_id, parent_id),
    CHECK (child_id <> parent_id)
);

CREATE TABLE IF NOT EXISTS id_seq (
    kind TEXT PRIMARY KEY,
    next_value INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS audit_events (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    ts         TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    file_id    TEXT,
    action     TEXT NOT NULL,
    detail     TEXT
);

-- Three-way merge drafts.  The merge state derived from the three source
-- CSVs is frozen into state_json at creation; user conflict choices and the
-- optimistic-concurrency revision are updated in place until confirmation
-- locks the draft and links the registered result file.
CREATE TABLE IF NOT EXISTS merge_drafts (
    id           TEXT PRIMARY KEY,
    base_id      TEXT NOT NULL REFERENCES files(id),
    left_id      TEXT NOT NULL REFERENCES files(id),
    right_id     TEXT NOT NULL REFERENCES files(id),
    key_column   TEXT NOT NULL,
    state_json   TEXT NOT NULL,
    choices_json TEXT NOT NULL DEFAULT '{}',
    revision     INTEGER NOT NULL DEFAULT 0 CHECK (revision >= 0),
    status       TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'completed')),
    result_id    TEXT REFERENCES files(id),
    created_at   TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    updated_at   TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    completed_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_dep_parent ON dependencies(parent_id);
CREATE INDEX IF NOT EXISTS idx_dep_child  ON dependencies(child_id);
CREATE INDEX IF NOT EXISTS idx_merge_drafts_status ON merge_drafts(status);
"""


def get_db() -> sqlite3.Connection:
    if "db" not in g:
        conn = sqlite3.connect(
            current_app.config["DATABASE"],
            isolation_level=None,  # autocommit; explicit BEGIN only
            timeout=30,
        )
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=30000")
        g.db = conn
    return g.db


def close_db(_exc=None) -> None:
    conn = g.pop("db", None)
    if conn is not None:
        conn.close()


def init_db() -> None:
    os.makedirs(os.path.dirname(current_app.config["DATABASE"]), exist_ok=True)
    conn = sqlite3.connect(current_app.config["DATABASE"], isolation_level=None)
    try:
        conn.execute("PRAGMA foreign_keys=ON")
        conn.executescript(SCHEMA)
    finally:
        conn.close()


def init_app(app) -> None:
    app.teardown_appcontext(close_db)
    with app.app_context():
        init_db()


# ---------------------------------------------------------------------------
# ID generation
# ---------------------------------------------------------------------------

_PREFIX = {"raw": "RAW", "cleaned": "CLN", "summary": "SUM", "merge": "MRG"}


def next_file_id(conn: sqlite3.Connection, kind: str) -> str:
    """Allocate the next human-readable, process-safe file ID."""
    prefix = _PREFIX[kind]
    cur = conn.execute(
        """
        INSERT INTO id_seq(kind, next_value) VALUES (?, 2)
        ON CONFLICT(kind) DO UPDATE SET next_value = next_value + 1
        RETURNING next_value
        """,
        (kind,),
    )
    value = cur.fetchone()[0] - 1
    return f"{prefix}-{value:04d}"


def next_merge_id(conn: sqlite3.Connection) -> str:
    """Allocate the next merge-draft ID (MRG-0001, ...)."""
    return next_file_id(conn, "merge")


# ---------------------------------------------------------------------------
# Availability / lineage queries
#
# Availability is derived on every read from the immutable registration data
# plus the revoke flags.  Therefore a restart cannot change the answer and
# re-registering a repaired file under a new ID never revives old descendants.
# ---------------------------------------------------------------------------

# Recursive walk from each file up to every ancestor, carrying the path.
_ANCESTORS_SQL = """
WITH RECURSIVE walk(child_id, ancestor_id, depth, path) AS (
    SELECT d.child_id, d.parent_id, 1,
           d.child_id || ' <- ' || d.parent_id
    FROM dependencies d
    WHERE d.child_id = ?
    UNION ALL
    SELECT w.child_id, d.parent_id, w.depth + 1,
           w.path || ' <- ' || d.parent_id
    FROM walk w
    JOIN dependencies d ON d.child_id = w.ancestor_id
)
SELECT w.ancestor_id, w.depth, w.path,
       f.filename AS ancestor_filename, f.revoked AS ancestor_revoked
FROM walk w
JOIN files f ON f.id = w.ancestor_id
ORDER BY w.depth, w.ancestor_id
"""

# Recursive walk downward: every (root, descendant) pair reachable from root,
# carrying the path.  A descendant appears once per distinct dependency path.
_DESCENDANTS_SQL = """
WITH RECURSIVE walk(root_id, descendant_id, depth, path) AS (
    SELECT ?, ?, 0, ?
    UNION ALL
    SELECT w.root_id, d.child_id, w.depth + 1,
           w.path || ' -> ' || d.child_id
    FROM walk w
    JOIN dependencies d ON d.parent_id = w.descendant_id
)
SELECT w.descendant_id, w.depth, w.path,
       f.filename, f.revoked
FROM walk w
JOIN files f ON f.id = w.descendant_id
ORDER BY w.depth, w.descendant_id
"""


def get_file(conn: sqlite3.Connection, file_id: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM files WHERE id = ?", (file_id,)).fetchone()


def list_parents(conn: sqlite3.Connection, child_id: str) -> list[sqlite3.Row]:
    return conn.execute(
        """
        SELECT f.* FROM dependencies d
        JOIN files f ON f.id = d.parent_id
        WHERE d.child_id = ?
        ORDER BY d.position, d.parent_id
        """,
        (child_id,),
    ).fetchall()


def list_children(conn: sqlite3.Connection, parent_id: str) -> list[sqlite3.Row]:
    return conn.execute(
        """
        SELECT f.* FROM dependencies d
        JOIN files f ON f.id = d.child_id
        WHERE d.parent_id = ?
        ORDER BY f.id
        """,
        (parent_id,),
    ).fetchall()


def find_blocking_ancestor(
    conn: sqlite3.Connection, file_id: str
) -> tuple[str | None, str | None, str | None]:
    """Return (blocking_ancestor_id, filename, path) or (None, None, None).

    A file is blocked when any transitive ancestor is revoked.  The shallowest
    revoked ancestor wins; ties are broken by ID for deterministic output.
    """
    rows = conn.execute(_ANCESTORS_SQL, (file_id,)).fetchall()
    best = None
    for row in rows:
        if row["ancestor_revoked"]:
            if best is None or (row["depth"], row["ancestor_id"]) < (
                best["depth"],
                best["ancestor_id"],
            ):
                best = row
    if best is None:
        return None, None, None
    return best["ancestor_id"], best["ancestor_filename"], best["path"]


def is_available(conn: sqlite3.Connection, file_id: str) -> bool:
    """A file is downloadable iff it is not revoked itself and no transitive
    ancestor has been revoked."""
    row = get_file(conn, file_id)
    if row is None or row["revoked"]:
        return False
    blocker, _, _ = find_blocking_ancestor(conn, file_id)
    return blocker is None


def blocked_by_map(conn: sqlite3.Connection) -> dict[str, str]:
    """Map every blocked file id to the id of its nearest revoked ancestor."""
    rows = conn.execute(
        """
        WITH RECURSIVE walk(child_id, ancestor_id, depth) AS (
            SELECT d.child_id, d.parent_id, 1
            FROM dependencies d
            UNION ALL
            SELECT w.child_id, d.parent_id, w.depth + 1
            FROM walk w
            JOIN dependencies d ON d.child_id = w.ancestor_id
        )
        SELECT w.child_id, w.ancestor_id, MIN(w.depth) AS depth
        FROM walk w
        JOIN files f ON f.id = w.ancestor_id
        WHERE f.revoked = 1
        GROUP BY w.child_id, w.ancestor_id
        """
    ).fetchall()
    best: dict[str, tuple[int, str]] = {}
    for row in rows:
        cur = best.get(row["child_id"])
        if cur is None or (row["depth"], row["ancestor_id"]) < cur:
            best[row["child_id"]] = (row["depth"], row["ancestor_id"])
    return {child: pair[1] for child, pair in best.items()}


def descendant_chains(
    conn: sqlite3.Connection, root_id: str
) -> list[sqlite3.Row]:
    """All paths from root down through its transitive descendants."""
    return conn.execute(
        _DESCENDANTS_SQL, (root_id, root_id, root_id)
    ).fetchall()


def list_all_files(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM files ORDER BY uploaded_at, id"
    ).fetchall()


def recent_events(conn: sqlite3.Connection, limit: int = 20) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM audit_events ORDER BY id DESC LIMIT ?", (limit,)
    ).fetchall()


# ---------------------------------------------------------------------------
# Merge drafts
# ---------------------------------------------------------------------------

def get_merge_draft(conn: sqlite3.Connection, draft_id: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM merge_drafts WHERE id = ?", (draft_id,)
    ).fetchone()


def list_merge_drafts(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM merge_drafts ORDER BY created_at, id"
    ).fetchall()


def insert_merge_draft(
    conn: sqlite3.Connection,
    draft_id: str,
    base_id: str,
    left_id: str,
    right_id: str,
    key_column: str,
    state_json: str,
) -> None:
    conn.execute(
        """
        INSERT INTO merge_drafts
            (id, base_id, left_id, right_id, key_column, state_json)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (draft_id, base_id, left_id, right_id, key_column, state_json),
    )
