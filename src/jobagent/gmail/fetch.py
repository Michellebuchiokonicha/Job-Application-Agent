"""Fetch emails from Gmail into the local raw_emails table. Idempotent and resumable.

The Gmail service is passed in (dependency injection), so tests can use a fake one
instead of the real API.
"""

import json
import random
import sqlite3
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import datetime

from googleapiclient.errors import HttpError

from jobagent.core.store import insert_raw_email, raw_email_exists
from jobagent.gmail.parse import parse_message

# Gmail enforces a per-user, per-minute quota. When we hit it, we wait and retry.
RATE_LIMIT_REASONS = (b"rateLimitExceeded", b"userRateLimitExceeded")
MAX_ATTEMPTS = 8
BASE_DELAY_S = 2.0
MAX_DELAY_S = 64.0  # the quota window is a minute, so waiting longer is pointless


@dataclass
class FetchStats:
    listed: int = 0
    already_stored: int = 0
    stored: int = 0
    retries: int = 0


def build_query(base_query: str, since: datetime) -> str:
    # Epoch seconds rather than "after:2026/10/01": a date is read in Gmail's
    # timezone (ambiguous); a timestamp is exact.
    return f"{base_query} after:{int(since.timestamp())}".strip()


def is_retryable(err: HttpError) -> bool:
    status = err.resp.status
    if status == 429 or status >= 500:  # too many requests / Google-side error
        return True
    return status == 403 and any(r in (err.content or b"") for r in RATE_LIMIT_REASONS)


def execute_with_retry(
    request,
    stats: FetchStats,
    sleep: Callable[[float], None] = time.sleep,
):
    """Run request.execute(), retrying rate limits with exponential backoff + jitter.

    Delays: ~2s, 4s, 8s, ... capped at 64s. Jitter (randomness) stops many clients
    from retrying at the exact same moment. Non-retryable errors raise immediately.
    """
    for attempt in range(MAX_ATTEMPTS):
        try:
            return request.execute()
        except HttpError as err:
            if not is_retryable(err) or attempt == MAX_ATTEMPTS - 1:
                raise
            delay = min(MAX_DELAY_S, BASE_DELAY_S * 2**attempt)
            stats.retries += 1
            sleep(delay * random.uniform(0.5, 1.0))
    raise AssertionError("unreachable")


def list_message_ids(service, query: str, stats: FetchStats) -> Iterator[str]:
    """Yield every matching message id, following pagination."""
    page_token = None
    while True:
        request = (
            service.users()
            .messages()
            .list(userId="me", q=query, pageToken=page_token, maxResults=500)
        )
        resp = execute_with_retry(request, stats)
        for m in resp.get("messages", []):  # key is absent when there are no results
            yield m["id"]
        page_token = resp.get("nextPageToken")
        if not page_token:
            return


def fetch_new(
    service,
    conn: sqlite3.Connection,
    base_query: str,
    since: datetime,
    on_progress: Callable[[int, int], None] | None = None,
) -> FetchStats:
    """List matching ids, then download only the ones not stored yet.

    Listing first is cheap (500 ids per call) and tells us the total, so the caller
    can show progress. If interrupted, re-running resumes: stored ids are skipped.
    """
    stats = FetchStats()
    ids = list(list_message_ids(service, build_query(base_query, since), stats))
    stats.listed = len(ids)
    new_ids = [i for i in ids if not raw_email_exists(conn, i)]
    stats.already_stored = stats.listed - len(new_ids)

    for done, msg_id in enumerate(new_ids, start=1):
        request = service.users().messages().get(userId="me", id=msg_id, format="full")
        message = execute_with_retry(request, stats)
        insert_raw_email(conn, parse_message(message), json.dumps(message))
        stats.stored += 1
        if on_progress:
            on_progress(done, len(new_ids))
    return stats
