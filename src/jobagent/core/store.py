"""SQLite storage: schema, connection, and the only code allowed to change a status.

Rules (states.py) decide *whether* a move is allowed. This module *performs* it:
update the status and append to the history, atomically, in one transaction.
"""

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path

from jobagent.core.states import INITIAL_STATUSES, Status, check_transition


class Source(StrEnum):
    """How an application was made. Lets us compare channels later."""

    SORCE = "sorce"
    OWN_AGENT = "own_agent"
    MANUAL = "manual"
    REFERRAL = "referral"
    UNKNOWN = "unknown"  # bootstrapped from an email: we can't tell how you applied


class ApplicationNotFound(Exception):
    pass


def _sql_in(enum: type[StrEnum]) -> str:
    """Render enum values as a SQL list, e.g. ('a', 'b'), for CHECK constraints."""
    return "(" + ", ".join(f"'{m.value}'" for m in enum) + ")"


# CHECK constraints are generated from the enums, so Python and the DB can never disagree.
SCHEMA = f"""
CREATE TABLE IF NOT EXISTS applications (
    id                  INTEGER PRIMARY KEY,
    company             TEXT NOT NULL,
    role                TEXT NOT NULL,
    status              TEXT NOT NULL CHECK (status IN {_sql_in(Status)}),
    source              TEXT NOT NULL CHECK (source IN {_sql_in(Source)}),
    -- Feedback-loop fields (docs/state-machine.md §6). Mostly NULL for Sorce apps.
    seniority           TEXT,
    location            TEXT,
    work_mode           TEXT,
    sponsorship_needed  INTEGER,          -- 0/1/NULL (SQLite has no boolean type)
    salary_band         TEXT,
    ats                 TEXT,
    cv_version          TEXT,
    job_url             TEXT,
    job_description     TEXT,
    applied_at          TEXT,
    created_at          TEXT NOT NULL,    -- all timestamps: ISO 8601, UTC
    updated_at          TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS status_changes (
    id              INTEGER PRIMARY KEY,
    application_id  INTEGER NOT NULL REFERENCES applications(id),
    from_status     TEXT,                 -- NULL = the application was created
    to_status       TEXT NOT NULL CHECK (to_status IN {_sql_in(Status)}),
    reason          TEXT NOT NULL,
    event_id        TEXT,                 -- what caused it, e.g. a Gmail message id
    is_override     INTEGER NOT NULL DEFAULT 0,
    at              TEXT NOT NULL
);

-- Idempotency: one event can move a given application at most once...
CREATE UNIQUE INDEX IF NOT EXISTS uq_change_per_event
    ON status_changes (application_id, event_id) WHERE event_id IS NOT NULL;
-- ...and can create at most one application.
CREATE UNIQUE INDEX IF NOT EXISTS uq_creation_per_event
    ON status_changes (event_id) WHERE event_id IS NOT NULL AND from_status IS NULL;

CREATE INDEX IF NOT EXISTS ix_status_changes_app ON status_changes (application_id);
"""


def _now() -> str:
    return datetime.now(UTC).isoformat()


def connect(db_path: str | Path = ":memory:") -> sqlite3.Connection:
    # isolation_level=None: no hidden transactions; we open them explicitly in _tx().
    conn = sqlite3.connect(db_path, isolation_level=None)
    conn.row_factory = sqlite3.Row  # rows behave like dicts: row["status"]
    conn.execute("PRAGMA foreign_keys = ON")  # SQLite ignores REFERENCES unless enabled
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)


@contextmanager
def _tx(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """All-or-nothing block. IMMEDIATE takes the write lock up front, so the status
    we read can't change before we write."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")


def _find_creation(conn: sqlite3.Connection, event_id: str) -> int | None:
    row = conn.execute(
        "SELECT application_id FROM status_changes "
        "WHERE event_id = ? AND from_status IS NULL",
        (event_id,),
    ).fetchone()
    return row["application_id"] if row else None


def create_application(
    conn: sqlite3.Connection,
    *,
    company: str,
    role: str,
    status: Status,
    reason: str,
    source: Source = Source.UNKNOWN,
    event_id: str | None = None,
    **fields: object,
) -> int:
    """Create an application at an initial status and log it. Returns its id.

    Idempotent: if this event_id already created an application, that id is returned.
    Extra keyword args fill optional columns (location=..., cv_version=..., ...).
    """
    if status not in INITIAL_STATUSES:
        raise ValueError(f"Cannot create an application at status '{status}'")

    with _tx(conn):
        if event_id is not None and (existing := _find_creation(conn, event_id)):
            return existing

        now = _now()
        columns = {
            "company": company,
            "role": role,
            "status": status,
            "source": source,
            **fields,
            "created_at": now,
            "updated_at": now,
        }
        # Column names come from code, never user input, so building them is safe.
        # Values always go through ? placeholders (prevents SQL injection).
        names = ", ".join(columns)
        marks = ", ".join("?" for _ in columns)
        cur = conn.execute(
            f"INSERT INTO applications ({names}) VALUES ({marks})",
            tuple(columns.values()),
        )
        app_id = cur.lastrowid
        conn.execute(
            "INSERT INTO status_changes "
            "(application_id, from_status, to_status, reason, event_id, at) "
            "VALUES (?, NULL, ?, ?, ?, ?)",
            (app_id, status, reason, event_id, now),
        )
        return app_id


def transition(
    conn: sqlite3.Connection,
    application_id: int,
    new_status: Status,
    reason: str,
    *,
    event_id: str | None = None,
    override: bool = False,
) -> bool:
    """Move an application to new_status. Returns True if it moved, False if this
    event was already applied (safe to call twice).

    Raises InvalidTransition if the move isn't allowed (unless override=True, which is
    for manual decisions from the review queue and is logged as such), and
    ApplicationNotFound for an unknown id. On any error, nothing is written.
    """
    with _tx(conn):
        row = conn.execute(
            "SELECT status FROM applications WHERE id = ?", (application_id,)
        ).fetchone()
        if row is None:
            raise ApplicationNotFound(application_id)

        if event_id is not None:
            already = conn.execute(
                "SELECT 1 FROM status_changes WHERE application_id = ? AND event_id = ?",
                (application_id, event_id),
            ).fetchone()
            if already:
                return False

        current = Status(row["status"])
        if not override:
            check_transition(current, new_status)

        now = _now()
        conn.execute(
            "UPDATE applications SET status = ?, updated_at = ? WHERE id = ?",
            (new_status, now, application_id),
        )
        conn.execute(
            "INSERT INTO status_changes "
            "(application_id, from_status, to_status, reason, event_id, is_override, at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (application_id, current, new_status, reason, event_id, int(override), now),
        )
        return True


def get_application(conn: sqlite3.Connection, application_id: int) -> sqlite3.Row:
    row = conn.execute(
        "SELECT * FROM applications WHERE id = ?", (application_id,)
    ).fetchone()
    if row is None:
        raise ApplicationNotFound(application_id)
    return row


def list_applications(
    conn: sqlite3.Connection, status: Status | None = None
) -> list[sqlite3.Row]:
    if status is None:
        return conn.execute(
            "SELECT * FROM applications ORDER BY updated_at DESC"
        ).fetchall()
    return conn.execute(
        "SELECT * FROM applications WHERE status = ? ORDER BY updated_at DESC",
        (status,),
    ).fetchall()


def history(conn: sqlite3.Connection, application_id: int) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM status_changes WHERE application_id = ? ORDER BY id",
        (application_id,),
    ).fetchall()
