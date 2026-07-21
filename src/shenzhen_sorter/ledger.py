"""
Durable SQLite ledger - the machine source of truth (spec section 21.1).

Receipts are human summaries generated FROM this ledger; the ledger is
never generated from a receipt. If the two ever disagree, the ledger wins.

Design notes:
- WAL journal mode + busy_timeout so a brief overlap between the periodic
  reconciliation scan and an active writer doesn't raise "database is locked".
  This is a concurrency *smoother*, not a substitute for the single-writer
  lock in lock.py - only one process should ever be the active writer.
- Every status change is a separate UPDATE with its own timestamp column
  populated (discovery_time, stability_time, copy_time, verify_time,
  commit_time) so a partial/crashed run can be reconciled by asking
  "which timestamps are set vs. NULL" rather than trusting a single status
  string alone.
"""

import sqlite3
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

from config import settings

# Appendix B state names, kept as plain strings (not an enum) so the raw
# SQLite rows stay human-readable when someone opens the db in a viewer.
STATES = (
    "DISCOVERED",
    "WAITING_FOR_STABILITY",
    "STABLE",
    "METADATA_READ",
    "ASSET_GROUPED",
    "DESTINATION_PLANNED",
    "COPYING",
    "COPIED",
    "VERIFYING",
    "VERIFIED",
    "COMMITTED",
    "QUARANTINED",
    "ERROR",
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    receipt_id      TEXT PRIMARY KEY,
    started_at      REAL NOT NULL,
    completed_at    REAL,
    status          TEXT NOT NULL DEFAULT 'IN_PROGRESS'
);

CREATE TABLE IF NOT EXISTS dump_folders (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    receipt_id      TEXT NOT NULL REFERENCES sessions(receipt_id),
    folder_name     TEXT NOT NULL,
    source_path     TEXT NOT NULL,
    shipped_at      REAL,
    UNIQUE(receipt_id, folder_name)
);

CREATE TABLE IF NOT EXISTS transactions (
    txn_id                  TEXT PRIMARY KEY,
    receipt_id              TEXT NOT NULL REFERENCES sessions(receipt_id),
    dump_folder_id          INTEGER NOT NULL REFERENCES dump_folders(id),
    source_path             TEXT NOT NULL,
    original_filename       TEXT NOT NULL,
    source_size             INTEGER,
    source_sha256           TEXT,
    destination_path        TEXT,
    destination_size        INTEGER,
    destination_sha256      TEXT,
    discovery_time          REAL,
    stability_time          REAL,
    copy_time               REAL,
    verification_time       REAL,
    commit_time             REAL,
    detected_file_type      TEXT,
    capture_date            TEXT,
    camera_make             TEXT,
    camera_model            TEXT,
    sorting_category        TEXT,
    asset_group_id          TEXT,
    status                  TEXT NOT NULL DEFAULT 'DISCOVERED',
    warnings                TEXT,
    errors                  TEXT,
    retry_count             INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_txn_dump_folder ON transactions(dump_folder_id);
CREATE INDEX IF NOT EXISTS idx_txn_status ON transactions(status);

-- Stability-gate tracking (spec section 6). One row per observed source
-- file, updated on every reconciliation scan until the file is judged
-- stable and promoted into a transaction. Kept separate from `transactions`
-- because a file can be observed many times before it is ever eligible.
CREATE TABLE IF NOT EXISTS file_observations (
    source_path         TEXT PRIMARY KEY,
    first_seen_at       REAL NOT NULL,
    last_seen_at        REAL NOT NULL,
    unchanged_since      REAL NOT NULL,
    last_size           INTEGER NOT NULL,
    last_mtime          REAL NOT NULL,
    size_ever_shrank     INTEGER NOT NULL DEFAULT 0
);
"""


def _connect(db_path: Path = None) -> sqlite3.Connection:
    db_path = db_path if db_path is not None else settings.LEDGER_DB_PATH
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path), timeout=30, isolation_level=None)
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA busy_timeout=30000;")
    conn.execute("PRAGMA foreign_keys=ON;")
    conn.row_factory = sqlite3.Row
    return conn


@contextmanager
def connection(db_path: Path = None):
    conn = _connect(db_path)
    try:
        yield conn
    finally:
        conn.close()


def init_db(db_path: Path = None) -> None:
    with connection(db_path) as conn:
        conn.executescript(SCHEMA)


def new_receipt_id(prefix: str) -> str:
    # prefix e.g. "SZ" (Windows) matches spec receipt-ID examples like
    # SZ-20260714-231700-A7F3
    ts = time.strftime("%Y%m%d-%H%M%S")
    short = uuid.uuid4().hex[:4].upper()
    return f"{prefix}-{ts}-{short}"


def start_session(conn: sqlite3.Connection, receipt_id: str) -> None:
    conn.execute(
        "INSERT INTO sessions (receipt_id, started_at) VALUES (?, ?)",
        (receipt_id, time.time()),
    )


def complete_session(conn: sqlite3.Connection, receipt_id: str, status: str) -> None:
    conn.execute(
        "UPDATE sessions SET completed_at = ?, status = ? WHERE receipt_id = ?",
        (time.time(), status, receipt_id),
    )


def add_dump_folder(conn: sqlite3.Connection, receipt_id: str, folder_name: str, source_path: str) -> int:
    cur = conn.execute(
        "INSERT INTO dump_folders (receipt_id, folder_name, source_path) VALUES (?, ?, ?)",
        (receipt_id, folder_name, source_path),
    )
    return cur.lastrowid


def mark_dump_folder_shipped(conn: sqlite3.Connection, dump_folder_id: int) -> None:
    conn.execute(
        "UPDATE dump_folders SET shipped_at = ? WHERE id = ?",
        (time.time(), dump_folder_id),
    )


def create_transaction(
    conn: sqlite3.Connection,
    receipt_id: str,
    dump_folder_id: int,
    source_path: str,
    original_filename: str,
) -> str:
    txn_id = uuid.uuid4().hex
    conn.execute(
        """INSERT INTO transactions
           (txn_id, receipt_id, dump_folder_id, source_path, original_filename,
            discovery_time, status)
           VALUES (?, ?, ?, ?, ?, ?, 'DISCOVERED')""",
        (txn_id, receipt_id, dump_folder_id, source_path, original_filename, time.time()),
    )
    return txn_id


def set_status(conn: sqlite3.Connection, txn_id: str, status: str, **fields) -> None:
    assert status in STATES, f"unknown state {status!r}"
    columns = ["status = ?"]
    values = [status]
    time_column_for_status = {
        "STABLE": "stability_time",
        "COPIED": "copy_time",
        "VERIFIED": "verification_time",
        "COMMITTED": "commit_time",
    }.get(status)
    if time_column_for_status:
        columns.append(f"{time_column_for_status} = ?")
        values.append(time.time())
    # warnings accumulate across a transaction's lifetime (e.g. a low-confidence
    # grouping note at ASSET_GROUPED, then an unrelated date-fallback note at
    # DESTINATION_PLANNED) rather than each call overwriting the last one - same
    # append behavior as record_error(), just inline here since set_status is
    # the only place warnings get written.
    warnings_value = fields.pop("warnings", None)
    if warnings_value:
        columns.append("warnings = COALESCE(warnings || char(10), '') || ?")
        values.append(warnings_value)
    for key, value in fields.items():
        columns.append(f"{key} = ?")
        values.append(value)
    values.append(txn_id)
    conn.execute(f"UPDATE transactions SET {', '.join(columns)} WHERE txn_id = ?", values)


def record_error(conn: sqlite3.Connection, txn_id: str, message: str) -> None:
    conn.execute(
        """UPDATE transactions
           SET status = 'ERROR', errors = COALESCE(errors || char(10), '') || ?,
               retry_count = retry_count + 1
           WHERE txn_id = ?""",
        (message, txn_id),
    )


def find_committed_by_source_path(conn: sqlite3.Connection, source_path: str, exclude_txn_id: str = None):
    """Most recent COMMITTED transaction for this exact source path, if any -
    from ANY prior session. Used so a retry only touches unresolved work
    (spec section 7's locked rule) instead of re-copying/re-hashing files
    that already succeeded in an earlier session just because the dump
    folder as a whole wasn't fully reconciled yet."""
    query = "SELECT * FROM transactions WHERE source_path = ? AND status = 'COMMITTED'"
    params = [source_path]
    if exclude_txn_id:
        query += " AND txn_id != ?"
        params.append(exclude_txn_id)
    query += " ORDER BY commit_time DESC LIMIT 1"
    return conn.execute(query, params).fetchone()


def find_committed_by_destination_sha256(conn: sqlite3.Connection, sha256: str, exclude_txn_id: str = None):
    """Most recent COMMITTED transaction whose destination bytes hash to
    this value, regardless of filename. Spec section 8's "different
    filenames + same SHA-256" case: filename is never identity, so this
    catches a byte-identical duplicate even when it doesn't collide on
    name with anything already archived."""
    query = "SELECT * FROM transactions WHERE destination_sha256 = ? AND status = 'COMMITTED'"
    params = [sha256]
    if exclude_txn_id:
        query += " AND txn_id != ?"
        params.append(exclude_txn_id)
    query += " ORDER BY commit_time DESC LIMIT 1"
    return conn.execute(query, params).fetchone()


def committed_in_month(conn: sqlite3.Connection, year: int, month: int):
    """Every COMMITTED transaction whose resolved capture date falls in
    this Year/Month, regardless of which camera/category it landed under -
    the data source for the Browse Month view (browse.py)."""
    capture_date = f"{year:04d}-{month:02d}"
    return conn.execute(
        "SELECT * FROM transactions WHERE status = 'COMMITTED' AND capture_date = ? "
        "ORDER BY destination_path",
        (capture_date,),
    ).fetchall()


def unresolved_transactions(conn: sqlite3.Connection, dump_folder_id: int):
    """Everything not COMMITTED - what a retry/resume pass must target."""
    return conn.execute(
        "SELECT * FROM transactions WHERE dump_folder_id = ? AND status != 'COMMITTED'",
        (dump_folder_id,),
    ).fetchall()


def observe_file(conn: sqlite3.Connection, source_path: str, size: int, mtime: float) -> sqlite3.Row:
    """Record one sighting of a file during a scan. Returns the row as it
    stands *after* this observation, so the caller can evaluate gates
    immediately without a second query."""
    now = time.time()
    existing = conn.execute(
        "SELECT * FROM file_observations WHERE source_path = ?", (source_path,)
    ).fetchone()
    if existing is None:
        conn.execute(
            """INSERT INTO file_observations
               (source_path, first_seen_at, last_seen_at, unchanged_since,
                last_size, last_mtime, size_ever_shrank)
               VALUES (?, ?, ?, ?, ?, ?, 0)""",
            (source_path, now, now, now, size, mtime),
        )
    else:
        unchanged = (size == existing["last_size"] and mtime == existing["last_mtime"])
        unchanged_since = existing["unchanged_since"] if unchanged else now
        shrank = existing["size_ever_shrank"] or (1 if size < existing["last_size"] else 0)
        conn.execute(
            """UPDATE file_observations
               SET last_seen_at = ?, unchanged_since = ?, last_size = ?,
                   last_mtime = ?, size_ever_shrank = ?
               WHERE source_path = ?""",
            (now, unchanged_since, size, mtime, shrank, source_path),
        )
    return conn.execute(
        "SELECT * FROM file_observations WHERE source_path = ?", (source_path,)
    ).fetchone()


def forget_observation(conn: sqlite3.Connection, source_path: str) -> None:
    conn.execute("DELETE FROM file_observations WHERE source_path = ?", (source_path,))


def dump_folder_fully_reconciled(conn: sqlite3.Connection, dump_folder_id: int) -> bool:
    row = conn.execute(
        "SELECT COUNT(*) AS n FROM transactions WHERE dump_folder_id = ? AND status != 'COMMITTED'",
        (dump_folder_id,),
    ).fetchone()
    return row["n"] == 0
