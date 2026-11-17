"""SQLite storage: schema, connection, and the only code allowed to change a status.

Rules (states.py) decide *whether* a move is allowed. This module *performs* it:
update the status and append to the history, atomically, in one transaction.
"""

import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import asdict
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING

from jobagent.core.states import INITIAL_STATUSES, Status, check_transition

if (
    TYPE_CHECKING
):  # import only for type hints, avoiding a core -> gmail runtime dependency
    from jobagent.gmail.parse import ParsedEmail
    from jobagent.sources.base import Job


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

-- Emails exactly as fetched, stored once. Classification results will live in a
-- separate table, so we can re-classify without re-fetching.
CREATE TABLE IF NOT EXISTS raw_emails (
    gmail_message_id  TEXT PRIMARY KEY,
    thread_id         TEXT NOT NULL,
    from_name         TEXT NOT NULL,
    from_addr         TEXT NOT NULL,
    to_addr           TEXT NOT NULL,
    subject           TEXT NOT NULL,
    received_at       TEXT NOT NULL,
    body_text         TEXT NOT NULL,
    raw_json          TEXT NOT NULL,      -- full Gmail API response, for re-parsing
    fetched_at        TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS ix_raw_emails_thread ON raw_emails (thread_id);
CREATE INDEX IF NOT EXISTS ix_raw_emails_received ON raw_emails (received_at);

-- Jobs found by the sources (Phase 4). Only jobs that passed the filters are stored.
CREATE TABLE IF NOT EXISTS jobs (
    id              INTEGER PRIMARY KEY,
    source          TEXT NOT NULL,        -- "reed", "greenhouse", ...
    external_id     TEXT NOT NULL,        -- the source's own ID
    title           TEXT NOT NULL,
    company         TEXT NOT NULL,
    location        TEXT NOT NULL,
    region          INTEGER NOT NULL,     -- match.filters.Region: 0 London ... 3 England
    url             TEXT NOT NULL,
    description     TEXT NOT NULL,
    salary_min      REAL,
    salary_max      REAL,
    posted_at       TEXT,                 -- ISO date
    first_seen_at   TEXT NOT NULL,
    last_seen_at    TEXT NOT NULL,
    UNIQUE (source, external_id)          -- idempotent: fetching twice stores once
);

CREATE INDEX IF NOT EXISTS ix_jobs_rank ON jobs (region, posted_at);

-- The full ad, fetched once per job (search results only carry a snippet).
CREATE TABLE IF NOT EXISTS job_details (
    job_id            INTEGER PRIMARY KEY REFERENCES jobs(id) ON DELETE CASCADE,
    full_description  TEXT NOT NULL,
    fetched_at        TEXT NOT NULL
);

-- The local model's fit assessment. One row per job; rescoring replaces it.
CREATE TABLE IF NOT EXISTS job_scores (
    job_id           INTEGER PRIMARY KEY REFERENCES jobs(id) ON DELETE CASCADE,
    score            INTEGER NOT NULL,
    verdict          TEXT NOT NULL,
    cv               TEXT NOT NULL,      -- which CV it was compared with
    assessment_json  TEXT NOT NULL,      -- met / missing / dealbreakers, in full
    unverified       INTEGER NOT NULL,   -- quotes not found in the ad
    model            TEXT NOT NULL,
    prompt_version   TEXT NOT NULL,
    scored_at        TEXT NOT NULL
);

-- Your accept/reject decisions: the labels for the matcher's eval. Keyed by the source's
-- own job ID (not jobs.id) and holding a copy of title, company and score, so they survive
-- rescoring and `jobs clear`.
CREATE TABLE IF NOT EXISTS job_decisions (
    source           TEXT NOT NULL,
    external_id      TEXT NOT NULL,
    decision         TEXT NOT NULL CHECK (decision IN ('accepted', 'rejected')),
    reason           TEXT NOT NULL DEFAULT '',
    title            TEXT NOT NULL,
    company          TEXT NOT NULL,
    score            INTEGER,            -- the fit score when you decided
    prompt_version   TEXT,
    decided_at       TEXT NOT NULL,
    PRIMARY KEY (source, external_id)
);

-- Phase 5: one application pack per job: the CV, the cover letter and (once sent) the
-- screening answers, exactly as sent. Keyed like job_decisions, so it outlives `jobs clear`.
CREATE TABLE IF NOT EXISTS application_packs (
    source           TEXT NOT NULL,
    external_id      TEXT NOT NULL,
    tier             TEXT NOT NULL CHECK (tier IN ('A', 'B')),
    cv               TEXT NOT NULL,      -- which CV to attach (match.cv.CV_FILES key)
    letter           TEXT NOT NULL,
    problems_json    TEXT NOT NULL,      -- code's checks: must fix before sending
    warnings_json    TEXT NOT NULL,      -- worth a look
    status           TEXT NOT NULL CHECK (status IN ('prepared', 'sent', 'rejected')),
    answers_json     TEXT,               -- the screening answers, copied when sent
    application_id   INTEGER REFERENCES applications(id),
    model            TEXT NOT NULL,
    created_at       TEXT NOT NULL,
    updated_at       TEXT NOT NULL,
    PRIMARY KEY (source, external_id)
);

-- Answers you've given to form questions, reused when another form asks the same thing.
-- Keyed by the question's normalised wording (apply.fill.question_key).
CREATE TABLE IF NOT EXISTS saved_answers (
    question_key  TEXT PRIMARY KEY,
    label         TEXT NOT NULL,     -- the question as you first saw it
    answer        TEXT NOT NULL,
    updated_at    TEXT NOT NULL
);
"""


def _now() -> str:
    return datetime.now(UTC).isoformat()


def connect(db_path: str | Path = ":memory:") -> sqlite3.Connection:
    on_disk = str(db_path) != ":memory:"
    if on_disk:
        Path(db_path).parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    # isolation_level=None: no hidden transactions; we open them explicitly in _tx().
    conn = sqlite3.connect(db_path, isolation_level=None)
    if on_disk:
        Path(db_path).chmod(0o600)  # it holds your email: readable by you only
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


# --- Raw emails ---


def raw_email_exists(conn: sqlite3.Connection, gmail_message_id: str) -> bool:
    return (
        conn.execute(
            "SELECT 1 FROM raw_emails WHERE gmail_message_id = ?", (gmail_message_id,)
        ).fetchone()
        is not None
    )


def insert_raw_email(
    conn: sqlite3.Connection, email: "ParsedEmail", raw_json: str
) -> bool:
    """Store an email. Returns False if it was already stored (INSERT OR IGNORE)."""
    row = {**asdict(email), "raw_json": raw_json, "fetched_at": _now()}
    names = ", ".join(row)
    marks = ", ".join("?" for _ in row)
    cur = conn.execute(
        f"INSERT OR IGNORE INTO raw_emails ({names}) VALUES ({marks})",
        tuple(row.values()),
    )
    return cur.rowcount == 1


def get_raw_email(conn: sqlite3.Connection, gmail_message_id: str) -> sqlite3.Row:
    """One stored email, without the bulky raw_json column."""
    row = conn.execute(
        "SELECT gmail_message_id, thread_id, from_name, from_addr, subject, "
        "received_at, body_text FROM raw_emails WHERE gmail_message_id = ?",
        (gmail_message_id,),
    ).fetchone()
    if row is None:
        raise KeyError(gmail_message_id)
    return row


def count_raw_emails(conn: sqlite3.Connection) -> int:
    return conn.execute("SELECT COUNT(*) FROM raw_emails").fetchone()[0]


def upsert_job(conn: sqlite3.Connection, job: "Job", region: int) -> bool:
    """Store a job, or refresh last_seen_at if we already have it. Returns True if new.

    ON CONFLICT ... DO UPDATE is an "upsert": insert, or update the existing row.
    last_seen_at tells us later which jobs have been taken down.
    """
    now = _now()
    existed = conn.execute(
        "SELECT 1 FROM jobs WHERE source = ? AND external_id = ?",
        (job.source, job.external_id),
    ).fetchone()
    conn.execute(
        """
        INSERT INTO jobs (source, external_id, title, company, location, region, url,
                          description, salary_min, salary_max, posted_at,
                          first_seen_at, last_seen_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT (source, external_id) DO UPDATE SET last_seen_at = excluded.last_seen_at
        """,
        (
            job.source,
            job.external_id,
            job.title,
            job.company,
            job.location,
            int(region),
            job.url,
            job.description,
            job.salary_min,
            job.salary_max,
            job.posted_at.isoformat() if job.posted_at else None,
            now,
            now,
        ),
    )
    return existed is None


_DECISION_JOIN = """
    LEFT JOIN job_decisions jd
        ON jd.source = jobs.source AND jd.external_id = jobs.external_id
"""


def list_jobs(
    conn: sqlite3.Connection, limit: int = 50, include_rejected: bool = False
) -> list[sqlite3.Row]:
    """Best first: highest fit score (unscored last), then preferred region, then newest."""
    where = "" if include_rejected else "WHERE jd.decision IS NOT 'rejected'"
    return conn.execute(
        f"""
        SELECT jobs.*, s.score, s.verdict, jd.decision FROM jobs
        LEFT JOIN job_scores s ON s.job_id = jobs.id
        {_DECISION_JOIN}
        {where}
        ORDER BY s.score IS NULL, s.score DESC, region, posted_at DESC, id DESC
        LIMIT ?
        """,
        (limit,),
    ).fetchall()


def jobs_to_review(
    conn: sqlite3.Connection, min_score: int = 0, limit: int | None = None
) -> list[sqlite3.Row]:
    """Scored jobs you haven't decided on yet, best first. Same columns as get_job."""
    return conn.execute(
        f"""
        SELECT jobs.*, d.full_description, s.score, s.verdict, s.cv, s.assessment_json,
               s.unverified, s.model, s.prompt_version
        FROM jobs
        JOIN job_scores s ON s.job_id = jobs.id
        LEFT JOIN job_details d ON d.job_id = jobs.id
        {_DECISION_JOIN}
        WHERE jd.decision IS NULL AND s.score >= ?
        ORDER BY s.score DESC, region, posted_at DESC
        LIMIT ?
        """,
        (min_score, -1 if limit is None else limit),
    ).fetchall()


def save_decision(
    conn: sqlite3.Connection, job: sqlite3.Row, decision: str, reason: str = ""
) -> None:
    """Record accept/reject for a job (a row from get_job or jobs_to_review)."""
    conn.execute(
        """
        INSERT OR REPLACE INTO job_decisions
            (source, external_id, decision, reason, title, company, score,
             prompt_version, decided_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            job["source"],
            job["external_id"],
            decision,
            reason.strip(),
            job["title"],
            job["company"],
            job["score"],
            job["prompt_version"],
            _now(),
        ),
    )


def decision_counts(conn: sqlite3.Connection) -> dict[str, int]:
    rows = conn.execute(
        "SELECT decision, COUNT(*) AS n FROM job_decisions GROUP BY decision"
    ).fetchall()
    return {r["decision"]: r["n"] for r in rows}


def get_job(conn: sqlite3.Connection, job_id: int) -> sqlite3.Row:
    row = conn.execute(
        """
        SELECT jobs.*, d.full_description, s.score, s.verdict, s.cv, s.assessment_json,
               s.unverified, s.model, s.prompt_version
        FROM jobs
        LEFT JOIN job_details d ON d.job_id = jobs.id
        LEFT JOIN job_scores s ON s.job_id = jobs.id
        WHERE jobs.id = ?
        """,
        (job_id,),
    ).fetchone()
    if row is None:
        raise KeyError(job_id)
    return row


def jobs_to_score(
    conn: sqlite3.Connection, rescore: bool = False, limit: int | None = None
) -> list[sqlite3.Row]:
    """Jobs without a score (or all, with rescore), best region first."""
    where = "" if rescore else "WHERE s.job_id IS NULL"
    return conn.execute(
        f"""
        SELECT jobs.*, d.full_description FROM jobs
        LEFT JOIN job_details d ON d.job_id = jobs.id
        LEFT JOIN job_scores s ON s.job_id = jobs.id
        {where}
        ORDER BY region, posted_at DESC
        LIMIT ?
        """,
        (-1 if limit is None else limit,),  # LIMIT -1 = no limit in SQLite
    ).fetchall()


def scored_jobs(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Every scored job with what's needed to recount it: title, ad and saved judgements."""
    return conn.execute(
        """
        SELECT jobs.id, jobs.title, d.full_description, jobs.description, s.cv,
               s.score, s.assessment_json, s.unverified, s.model, s.prompt_version
        FROM jobs
        JOIN job_scores s ON s.job_id = jobs.id
        LEFT JOIN job_details d ON d.job_id = jobs.id
        """
    ).fetchall()


def save_full_description(conn: sqlite3.Connection, job_id: int, text: str) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO job_details (job_id, full_description, fetched_at) "
        "VALUES (?, ?, ?)",
        (job_id, text, _now()),
    )


def save_score(
    conn: sqlite3.Connection,
    job_id: int,
    *,
    score: int,
    verdict: str,
    cv: str,
    assessment_json: str,
    unverified: int,
    model: str,
    prompt_version: str,
) -> None:
    conn.execute(
        """
        INSERT OR REPLACE INTO job_scores
            (job_id, score, verdict, cv, assessment_json, unverified, model,
             prompt_version, scored_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            job_id,
            score,
            verdict,
            cv,
            assessment_json,
            unverified,
            model,
            prompt_version,
            _now(),
        ),
    )


def job_id(conn: sqlite3.Connection, job: "Job") -> int:
    """The stored row id of a job (by its source's own ID)."""
    return conn.execute(
        "SELECT id FROM jobs WHERE source = ? AND external_id = ?",
        (job.source, job.external_id),
    ).fetchone()["id"]


def same_job_stored(conn: sqlite3.Connection, job: "Job") -> bool:
    """Is this job already stored under a *different* ID?

    Agencies repost the same ad: sometimes twice, sometimes once per postcode (one ad
    appeared in 40+ towns). Same company + same title (ignoring case) = one job to
    apply for, wherever it's listed.
    """
    return (
        conn.execute(
            """
            SELECT 1 FROM jobs
            WHERE lower(company) = lower(?) AND lower(title) = lower(?)
              AND NOT (source = ? AND external_id = ?)
            """,
            (job.company, job.title, job.source, job.external_id),
        ).fetchone()
        is not None
    )


def clear_jobs(conn: sqlite3.Connection) -> int:
    """Delete every stored job (they're public ads, re-fetchable). Returns how many."""
    conn.execute("DELETE FROM job_scores")
    conn.execute("DELETE FROM job_details")
    return conn.execute("DELETE FROM jobs").rowcount


# ---- Phase 5: application packs -------------------------------------------------------------

_PACK_JOIN = """
    LEFT JOIN application_packs ap
        ON ap.source = jobs.source AND ap.external_id = jobs.external_id
"""


def jobs_to_prepare(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Scored jobs with no pack yet, best first, with your decision (if any)."""
    return conn.execute(
        f"""
        SELECT jobs.*, d.full_description, s.score, s.cv, s.assessment_json,
               jd.decision
        FROM jobs
        JOIN job_scores s ON s.job_id = jobs.id
        LEFT JOIN job_details d ON d.job_id = jobs.id
        {_DECISION_JOIN}
        {_PACK_JOIN}
        WHERE ap.source IS NULL
        ORDER BY s.score DESC
        """
    ).fetchall()


def save_pack(
    conn: sqlite3.Connection,
    job: sqlite3.Row,
    *,
    tier: str,
    cv: str,
    letter: str,
    problems: list[str],
    warnings: list[str],
    model: str,
) -> None:
    now = _now()
    conn.execute(
        """
        INSERT INTO application_packs
            (source, external_id, tier, cv, letter, problems_json, warnings_json,
             status, model, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, 'prepared', ?, ?, ?)
        """,
        (
            job["source"],
            job["external_id"],
            tier,
            cv,
            letter,
            json.dumps(problems),
            json.dumps(warnings),
            model,
            now,
            now,
        ),
    )


def packs_to_review(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Prepared packs, tier A first, best score first, with the job's details."""
    return conn.execute(
        f"""
        SELECT ap.*, jobs.id AS job_id, jobs.title, jobs.company, jobs.location,
               jobs.url, s.score, COALESCE(d.full_description, jobs.description) AS ad
        FROM application_packs ap
        JOIN jobs ON jobs.source = ap.source AND jobs.external_id = ap.external_id
        LEFT JOIN job_scores s ON s.job_id = jobs.id
        LEFT JOIN job_details d ON d.job_id = jobs.id
        {_DECISION_JOIN}
        WHERE ap.status = 'prepared' AND jd.decision IS NOT 'rejected'
        ORDER BY ap.tier, s.score DESC
        """
    ).fetchall()


def update_pack_letter(
    conn: sqlite3.Connection,
    pack: sqlite3.Row,
    letter: str,
    problems: list[str],
    warnings: list[str],
) -> None:
    conn.execute(
        """
        UPDATE application_packs
        SET letter = ?, problems_json = ?, warnings_json = ?, updated_at = ?
        WHERE source = ? AND external_id = ?
        """,
        (
            letter,
            json.dumps(problems),
            json.dumps(warnings),
            _now(),
            pack["source"],
            pack["external_id"],
        ),
    )


def reject_pack(conn: sqlite3.Connection, pack: sqlite3.Row) -> None:
    conn.execute(
        "UPDATE application_packs SET status = 'rejected', updated_at = ? "
        "WHERE source = ? AND external_id = ?",
        (_now(), pack["source"], pack["external_id"]),
    )


def mark_sent(
    conn: sqlite3.Connection,
    pack: sqlite3.Row,
    *,
    letter: str,
    answers: dict[str, str],
    cv_file: str,
) -> int:
    """Record a sent application: a normal applications row plus exactly what was sent.

    Idempotent per job: marking the same job sent twice returns the same application.
    """
    app_id = create_application(
        conn,
        company=pack["company"],
        role=pack["title"],
        status=Status.SUBMITTED,
        reason="sent after approval in `apply review`",
        source=Source.OWN_AGENT,
        event_id=f"apply:{pack['source']}:{pack['external_id']}",
        location=pack["location"],
        cv_version=cv_file,
        job_url=pack["url"],
        job_description=pack["ad"],
        applied_at=_now(),
    )
    conn.execute(
        """
        UPDATE application_packs
        SET status = 'sent', letter = ?, answers_json = ?, application_id = ?,
            updated_at = ?
        WHERE source = ? AND external_id = ?
        """,
        (
            letter,
            json.dumps(answers),
            app_id,
            _now(),
            pack["source"],
            pack["external_id"],
        ),
    )
    return app_id


def delete_prepared_packs(conn: sqlite3.Connection, flagged_only: bool = False) -> int:
    """Drop packs not yet sent, so they're drafted again. Sent ones are a record: kept.
    With flagged_only, just the letters whose checks found problems."""
    flagged = " AND problems_json != '[]'" if flagged_only else ""
    return conn.execute(
        f"DELETE FROM application_packs WHERE status = 'prepared'{flagged}"
    ).rowcount


def pack_counts(conn: sqlite3.Connection) -> dict[str, int]:
    rows = conn.execute(
        "SELECT status, COUNT(*) AS n FROM application_packs GROUP BY status"
    ).fetchall()
    return {r["status"]: r["n"] for r in rows}


# ---- Saved answers to form questions ----------------------------------------------------


def save_answer(conn: sqlite3.Connection, key: str, label: str, answer: str) -> None:
    """Remember your answer to a question, replacing any earlier one."""
    conn.execute(
        """
        INSERT INTO saved_answers (question_key, label, answer, updated_at)
        VALUES (?, ?, ?, ?)
        ON CONFLICT (question_key) DO UPDATE
        SET label = excluded.label, answer = excluded.answer,
            updated_at = excluded.updated_at
        """,
        (key, label, answer, _now()),
    )


def saved_answers(conn: sqlite3.Connection) -> dict[str, str]:
    """Every saved answer, by question key."""
    rows = conn.execute("SELECT question_key, answer FROM saved_answers").fetchall()
    return {r["question_key"]: r["answer"] for r in rows}
