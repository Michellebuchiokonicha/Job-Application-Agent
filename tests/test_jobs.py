"""Phase 4 step 1: Reed source, filters, storage, CLI. All data here is made up."""

from datetime import date

import httpx
import pytest
from typer.testing import CliRunner

from jobagent import cli
from jobagent.core.store import connect, init_db, list_jobs
from jobagent.match.filters import Region, check, matches_role, region_for
from jobagent.sources.base import Job
from jobagent.sources.feed import fetch_jobs
from jobagent.sources.reed import ReedClient, parse_job

TODAY = date(2026, 11, 14)


def reed_item(
    job_id, title="Machine Learning Engineer", location="London", day="12/11/2026"
):
    return {
        "jobId": job_id,
        "jobTitle": title,
        "employerName": "Acme AI",
        "locationName": location,
        "minimumSalary": 60000.0,
        "maximumSalary": 80000.0,
        "date": day,
        "jobDescription": "Build ML systems...",
        "jobUrl": f"https://www.reed.co.uk/jobs/x/{job_id}",
    }


def job(title="ML Engineer", location="London", posted=TODAY, external_id="1"):
    return Job(
        "reed",
        external_id,
        title,
        "Acme",
        location,
        "https://x",
        "desc",
        posted_at=posted,
    )


# --- Reed client -----------------------------------------------------------


def test_parse_job_reads_reed_fields():
    j = parse_job(reed_item(42))
    assert (j.source, j.external_id, j.company) == ("reed", "42", "Acme AI")
    assert j.posted_at == date(2026, 11, 12)  # day first
    assert j.salary_min == 60000.0


def test_parse_job_survives_bad_date_and_missing_fields():
    j = parse_job({"jobId": 7, "date": "not a date"})
    assert j.posted_at is None and j.title == "" and j.url.endswith("/7")


def fake_reed(pages, calls):
    """A Reed API stand-in: serves `pages` in order and records each request."""

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, json={"results": pages[len(calls) - 1]})

    return ReedClient("KEY", httpx.Client(transport=httpx.MockTransport(handler)))


def test_search_pages_until_a_short_page():
    calls = []
    full = [reed_item(i) for i in range(100)]
    client = fake_reed([full, [reed_item(100)]], calls)
    jobs = list(client.search("ml engineer", max_results=500))
    assert len(jobs) == 101
    assert [c.url.params["resultsToSkip"] for c in calls] == ["0", "100"]


def test_search_stops_at_max_results():
    calls = []
    client = fake_reed([[reed_item(i) for i in range(50)]], calls)
    assert len(list(client.search("x", max_results=50))) == 50
    assert calls[0].url.params["resultsToTake"] == "50"


def test_search_sends_key_as_basic_auth_and_user_agent():
    calls = []
    list(fake_reed([[]], calls).search("x"))
    assert calls[0].headers["authorization"].startswith("Basic ")
    assert "jobagent" in calls[0].headers["user-agent"]


def test_bad_key_fails_loudly():
    client = ReedClient(
        "BAD",
        httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(401))),
    )
    with pytest.raises(httpx.HTTPStatusError):
        list(client.search("x"))


# --- Filters ---------------------------------------------------------------


@pytest.mark.parametrize(
    "title",
    [
        "Machine Learning Engineer",
        "Senior ML Engineer",
        "MLOps Engineer",
        "AI Engineer - LLMs",
        "Applied Scientist",
        "Forward Deployed Engineer",
        "Forward-Deployed Software Engineer",
        "AI Solutions Architect",
        "GenAI Developer",
        "Data Scientist",
        "Lead AI Data Scientist",
        "Automation Engineer",
        "AI Automation Engineer",
        "Intelligent Automation Developer",
        "AI Test Automation Engineer",  # off-topic word, but AI rescues it
    ],
)
def test_target_roles_match(title):
    assert matches_role(title)


@pytest.mark.parametrize(
    "title",
    [
        "Solutions Architect",
        "Mail Engineer",
        "AI Trainer",
        "Sales Executive",
        "Test Automation Engineer",  # software testing, a different career
        "PLC Automation Engineer",  # factory automation
    ],
)
def test_other_roles_do_not_match(title):
    assert not matches_role(title)


@pytest.mark.parametrize(
    ("location", "region"),
    [
        ("London", Region.LONDON),
        ("Canary Wharf, London", Region.LONDON),
        ("Reading, Berkshire", Region.SOUTH),
        ("Cambridge", Region.SOUTH),
        ("Remote", Region.REMOTE),
        ("Manchester", Region.ENGLAND),
        ("Edinburgh", Region.EXCLUDED),
        ("Cardiff", Region.EXCLUDED),
        ("Belfast", Region.EXCLUDED),
        ("Londonderry", Region.EXCLUDED),  # contains "london", must not count as London
        ("Cork", Region.EXCLUDED),  # Republic of Ireland
        ("CM201WG", Region.SOUTH),  # postcode only: CM = Chelmsford area
        ("SW1A 1AA", Region.LONDON),
        ("EH1 1YZ", Region.EXCLUDED),  # Edinburgh
        ("CV11 5AA", Region.ENGLAND),  # Coventry area
        ("Bath", Region.SOUTH),  # a town name, not a postcode
        ("Aberystwyth", Region.EXCLUDED),  # Wales
        ("Llandudno", Region.EXCLUDED),  # any Welsh "Llan..." town
        ("Welwyn Garden City", Region.SOUTH),
        ("Abingdon", Region.SOUTH),
    ],
)
def test_region(location, region):
    assert region_for(location) is region


def test_check_reasons():
    assert check(job(), TODAY).keep
    assert (
        check(job(title="Sales Executive"), TODAY).reason == "title not a target role"
    )
    assert check(job(location="Glasgow"), TODAY).reason == "outside England"
    old = job(posted=date(2026, 10, 1))
    assert check(old, TODAY, max_age_days=14).reason == "older than 14 days"
    assert check(job(posted=None), TODAY).keep  # unknown date: keep, don't guess


# --- Feed + storage --------------------------------------------------------


@pytest.fixture
def conn():
    c = connect()
    init_db(c)
    return c


def test_fetch_dedupes_filters_and_counts(conn):
    results = {
        "a": [job(external_id="1"), job(external_id="2", location="Glasgow")],
        "b": [job(external_id="1"), job(external_id="3", title="Sales Executive")],
    }
    stats = fetch_jobs(conn, lambda term, n: results[term], TODAY, terms=["a", "b"])
    assert (stats.seen, stats.unique, stats.kept, stats.new) == (4, 3, 1, 1)
    assert stats.dropped == {"outside England": 1, "title not a target role": 1}
    assert len(list_jobs(conn)) == 1


def test_fetch_twice_is_idempotent(conn):
    search = lambda term, n: [job(external_id="1")]
    fetch_jobs(conn, search, TODAY, terms=["a"])
    stats = fetch_jobs(conn, search, TODAY, terms=["a"])
    assert (stats.kept, stats.new) == (1, 0)
    assert len(list_jobs(conn)) == 1


def test_list_jobs_orders_by_region_then_newest(conn):
    jobs = [
        job(
            external_id="1",
            title="MLOps Engineer",
            location="Manchester",
            posted=date(2026, 11, 13),
        ),
        job(external_id="2", location="London", posted=date(2026, 11, 10)),
        job(
            external_id="3",
            title="AI Engineer",  # a different job, so not a repeat posting
            location="London",
            posted=date(2026, 11, 12),
        ),
        job(
            external_id="4",
            title="Applied Scientist",
            location="Reading",
            posted=date(2026, 11, 13),
        ),
    ]
    fetch_jobs(conn, lambda t, n: jobs, TODAY, terms=["a"])
    assert [r["external_id"] for r in list_jobs(conn)] == ["3", "2", "4", "1"]


# --- CLI -------------------------------------------------------------------


@pytest.fixture
def cli_env(tmp_path, monkeypatch):
    monkeypatch.setenv("DB_PATH", str(tmp_path / "test.db"))
    monkeypatch.setenv("REED_API_KEY", "")


def test_fetch_without_key_explains(cli_env):
    result = CliRunner().invoke(cli.app, ["jobs", "fetch"])
    assert result.exit_code == 2
    assert "reed.co.uk/developers" in result.output


def test_fetch_and_list(cli_env, monkeypatch):
    class FakeReed:
        def search(self, term, n):
            return [job(external_id="1", title="Forward Deployed Engineer")]

    monkeypatch.setattr(cli, "_make_reed", lambda settings: FakeReed())
    result = CliRunner().invoke(cli.app, ["jobs", "fetch"])
    assert result.exit_code == 0, result.output
    assert "1 match" in result.output and "(1 new)" in result.output

    listing = CliRunner().invoke(cli.app, ["jobs", "list"])
    # The test terminal is narrow, so Rich wraps long cells: check single words.
    assert "Forward" in listing.output
    assert "London" in listing.output


def test_same_job_posted_twice_is_dropped(conn):
    jobs = [job(external_id="1"), job(external_id="2")]  # same title/company/place
    stats = fetch_jobs(conn, lambda t, n: jobs, TODAY, terms=["a"])
    assert (stats.kept, stats.dropped["same job posted again"]) == (1, 1)
    # Re-fetching the original isn't a duplicate of itself.
    again = fetch_jobs(conn, lambda t, n: jobs[:1], TODAY, terms=["a"])
    assert again.kept == 1


@pytest.mark.parametrize(
    ("low", "high", "text"),
    [
        (60000, 80000, "£60k–80k"),
        (500, 600, "£500–600/day"),
        (None, None, ""),
        (70000, None, "£70k–70k"),
    ],
)
def test_short_salary(low, high, text):
    assert cli._short_salary(low, high) == text


def test_clear(cli_env, monkeypatch):
    class FakeReed:
        def search(self, term, n):
            return [job(external_id="1")]

    monkeypatch.setattr(cli, "_make_reed", lambda settings: FakeReed())
    CliRunner().invoke(cli.app, ["jobs", "fetch"])
    result = CliRunner().invoke(cli.app, ["jobs", "clear", "--yes"])
    assert "Deleted 1 jobs" in result.output
    assert "No jobs yet" in CliRunner().invoke(cli.app, ["jobs", "list"]).output


def test_same_ad_in_many_postcodes_is_one_job(conn):
    jobs = [
        job(external_id="1", location="E1 6RP"),
        job(external_id="2", location="RG1 1QH"),
        job(external_id="3", location="LS1 3AD"),
    ]
    stats = fetch_jobs(conn, lambda t, n: jobs, TODAY, terms=["a"])
    assert (stats.kept, stats.dropped["same job posted again"]) == (1, 2)
