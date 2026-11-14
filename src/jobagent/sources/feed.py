"""Run every search, filter, and store: the body of `jobagent jobs fetch`."""

import sqlite3
from collections import Counter
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass, field
from datetime import date

from jobagent.core.store import same_job_stored, upsert_job
from jobagent.match.filters import SEARCH_TERMS, check
from jobagent.sources.base import Job

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


def fetch_jobs(
    conn: sqlite3.Connection,
    search: Search,
    today: date,
    max_per_term: int = 200,
    max_age_days: int = 14,
    terms: Iterable[str] = SEARCH_TERMS,
) -> FeedStats:
    stats = FeedStats()
    found = (job for term in terms for job in search(term, max_per_term))
    for job in _unique(found, stats):
        stats.unique += 1
        verdict = check(job, today, max_age_days)
        if not verdict.keep:
            stats.dropped[verdict.reason] += 1
            continue
        if same_job_stored(conn, job):
            stats.dropped["same job posted again"] += 1
            continue
        stats.kept += 1
        if upsert_job(conn, job, verdict.region):
            stats.new += 1
    return stats
