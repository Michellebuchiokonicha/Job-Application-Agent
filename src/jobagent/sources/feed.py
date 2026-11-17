"""Run every search, filter, and store: the body of `jobagent jobs fetch`."""

import sqlite3
from collections import Counter
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass, field
from datetime import date
from typing import TYPE_CHECKING

import httpx

from jobagent.core.store import (
    job_id,
    same_job_stored,
    save_full_description,
    upsert_job,
)
from jobagent.match.filters import SEARCH_TERMS, check
from jobagent.sources.base import Job

if TYPE_CHECKING:
    from jobagent.sources.boards import BoardsClient, Company

# (search term, max results) -> jobs. ReedClient.search fits this shape, and so will
# the other search sources, so this loop doesn't change when we add them.
Search = Callable[[str, int], Iterable[Job]]


@dataclass
class FeedStats:
    seen: int = 0  # results returned by the API, duplicates included
    unique: int = 0  # distinct jobs (the same job often matches several searches)
    kept: int = 0
    new: int = 0  # kept and never stored before
    dropped: Counter = field(default_factory=Counter)  # reason -> count


def _unique(jobs: Iterable[Job], stats: FeedStats) -> Iterator[Job]:
    seen: set[tuple[str, str]] = set()
    for job in jobs:
        stats.seen += 1
        key = (job.source, job.external_id)
        if key not in seen:
            seen.add(key)
            yield job


def store_jobs(
    conn: sqlite3.Connection,
    jobs: Iterable[Job],
    today: date,
    max_age_days: int | None = 14,
    *,
    worldwide: bool = False,
    full_text: bool = False,
    stats: FeedStats | None = None,
) -> FeedStats:
    """Filter, dedupe and store. `full_text`: the descriptions are whole ads (boards),
    so they're saved as the full description and scoring never has to fetch them."""
    stats = stats or FeedStats()
    for job in _unique(jobs, stats):
        stats.unique += 1
        verdict = check(job, today, max_age_days, worldwide=worldwide)
        if not verdict.keep:
            stats.dropped[verdict.reason] += 1
            continue
        if same_job_stored(conn, job):
            stats.dropped["same job posted again"] += 1
            continue
        stats.kept += 1
        if upsert_job(conn, job, verdict.region):
            stats.new += 1
            if full_text:
                save_full_description(conn, job_id(conn, job), job.description)
    return stats


def fetch_jobs(
    conn: sqlite3.Connection,
    search: Search,
    today: date,
    max_per_term: int = 200,
    max_age_days: int = 14,
    terms: Iterable[str] = SEARCH_TERMS,
) -> FeedStats:
    """Search sources (Reed): run every search term, then filter and store."""
    found = (job for term in terms for job in search(term, max_per_term))
    return store_jobs(conn, found, today, max_age_days)


@dataclass
class BoardStats(FeedStats):
    companies: int = 0
    failed: list[str] = field(default_factory=list)  # "Name (ats/slug): reason"


def fetch_boards(
    conn: sqlite3.Connection,
    client: "BoardsClient",
    companies: Iterable["Company"],
    today: date,
) -> BoardStats:
    """Every open job at every company, filtered to target roles in England/UK-remote.

    One company failing (renamed slug, API down) is reported by name, never silent, and
    doesn't stop the others.
    """
    stats = BoardStats()
    for company in companies:
        try:
            jobs = list(client.jobs(company))
        except (httpx.HTTPError, ValueError) as e:  # ValueError: a non-JSON reply
            detail = (
                f"HTTP {e.response.status_code}"
                if isinstance(e, httpx.HTTPStatusError)
                else type(e).__name__
            )
            stats.failed.append(
                f"{company.name} ({company.ats}/{company.slug}): {detail}"
            )
            continue
        stats.companies += 1
        store_jobs(conn, jobs, today, None, worldwide=True, full_text=True, stats=stats)
    return stats
