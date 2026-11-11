from datetime import UTC, datetime

import httplib2
import pytest
from gmail_fakes import FakeGmail, message, part
from googleapiclient.errors import HttpError

from jobagent.core.store import connect, count_raw_emails, init_db
from jobagent.gmail.fetch import (
    FetchStats,
    build_query,
    execute_with_retry,
    fetch_new,
)

SINCE = datetime(2026, 10, 1, tzinfo=UTC)


@pytest.fixture
def conn():
    c = connect(":memory:")
    init_db(c)
    yield c
    c.close()


def five_messages():
    return [
        message(part("text/plain", f"email {i}"), msg_id=f"m{i}", thread_id=f"t{i}")
        for i in range(5)
    ]


def test_query_uses_epoch_seconds():
    assert build_query("label:jobs", SINCE) == "label:jobs after:1790812800"


def test_fetches_all_pages(conn):
    gmail = FakeGmail(five_messages(), page_size=2)  # 3 pages: 2 + 2 + 1
    stats = fetch_new(gmail, conn, "", SINCE)

    assert stats.listed == 5
    assert stats.stored == 5
    assert count_raw_emails(conn) == 5


def test_second_run_fetches_nothing_new(conn):
    gmail = FakeGmail(five_messages())
    fetch_new(gmail, conn, "", SINCE)
    calls_after_first_run = gmail.get_calls

    stats = fetch_new(gmail, conn, "", SINCE)

    assert stats.stored == 0
    assert stats.already_stored == 5
    assert gmail.get_calls == calls_after_first_run  # no wasted downloads
    assert count_raw_emails(conn) == 5


def test_no_results(conn):
    stats = fetch_new(FakeGmail([]), conn, "", SINCE)
    assert stats.listed == 0


# --- Rate limiting ---

RATE_LIMITED = b'{"error": {"errors": [{"reason": "rateLimitExceeded"}]}}'


def http_error(status: int, content: bytes = b"") -> HttpError:
    return HttpError(httplib2.Response({"status": status}), content)


class FlakyRequest:
    """Fails with the given errors first, then succeeds."""

    def __init__(self, errors, result="ok"):
        self.errors = list(errors)
        self.result = result
        self.calls = 0

    def execute(self):
        self.calls += 1
        if self.errors:
            raise self.errors.pop(0)
        return self.result


def test_retries_rate_limit_then_succeeds():
    sleeps = []
    req = FlakyRequest([http_error(403, RATE_LIMITED), http_error(429)])
    stats = FetchStats()

    assert execute_with_retry(req, stats, sleep=sleeps.append) == "ok"
    assert req.calls == 3
    assert stats.retries == 2
    assert len(sleeps) == 2
    # Exponential backoff with jitter: ~2s then ~4s (each scaled by 0.5-1.0).
    assert 1.0 <= sleeps[0] <= 2.0
    assert 2.0 <= sleeps[1] <= 4.0


def test_non_retryable_error_raises_immediately():
    # A 403 that is NOT a rate limit (e.g. permission denied) must not be retried.
    req = FlakyRequest(
        [http_error(403, b'{"error": {"errors": [{"reason": "forbidden"}]}}')]
    )
    with pytest.raises(HttpError):
        execute_with_retry(req, FetchStats(), sleep=lambda s: None)
    assert req.calls == 1


def test_gives_up_after_max_attempts():
    req = FlakyRequest([http_error(429)] * 20)
    with pytest.raises(HttpError):
        execute_with_retry(req, FetchStats(), sleep=lambda s: None)
    assert req.calls == 8


def test_progress_callback(conn):
    seen = []
    fetch_new(
        FakeGmail(five_messages()),
        conn,
        "",
        SINCE,
        on_progress=lambda d, t: seen.append((d, t)),
    )
    assert seen[-1] == (5, 5)
