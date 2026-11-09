"""Fetch emails from Gmail into the local raw_emails table. Idempotent.

The Gmail service is passed in (dependency injection), so tests can use a fake one
instead of the real API.
"""

import json
import sqlite3
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime

from jobagent.core.store import insert_raw_email, raw_email_exists
from jobagent.gmail.parse import parse_message


@dataclass
class FetchStats:
    listed: int = 0
    already_stored: int = 0
    stored: int = 0


def build_query(base_query: str, since: datetime) -> str:
    # Epoch seconds rather than "after:2026/10/01": a date is read in Gmail's
    # timezone (ambiguous); a timestamp is exact.
    return f"{base_query} after:{int(since.timestamp())}".strip()


def list_message_ids(service, query: str) -> Iterator[str]:
    """Yield every matching message id, following pagination."""
    page_token = None
    while True:
        resp = (
            service.users()
            .messages()
            .list(userId="me", q=query, pageToken=page_token, maxResults=500)
            .execute()
        )
        for m in resp.get("messages", []):  # key is absent when there are no results
            yield m["id"]
        page_token = resp.get("nextPageToken")
        if not page_token:
            return


def fetch_new(
    service, conn: sqlite3.Connection, base_query: str, since: datetime
) -> FetchStats:
    stats = FetchStats()
    for msg_id in list_message_ids(service, build_query(base_query, since)):
        stats.listed += 1
        if raw_email_exists(conn, msg_id):
            stats.already_stored += 1
            continue
        message = (
            service.users()
            .messages()
            .get(userId="me", id=msg_id, format="full")
            .execute()
        )
        insert_raw_email(conn, parse_message(message), json.dumps(message))
        stats.stored += 1
    return stats
