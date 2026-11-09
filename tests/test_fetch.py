from datetime import UTC, datetime

import pytest
from gmail_fakes import FakeGmail, message, part

from jobagent.core.store import connect, count_raw_emails, init_db
from jobagent.gmail.fetch import build_query, fetch_new

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
