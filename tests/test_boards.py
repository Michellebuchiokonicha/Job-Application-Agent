"""Company job boards (Greenhouse, Lever, Ashby): parsing, UK filtering, fetching.
Made-up API responses in each ATS's real shape; no network."""

from datetime import date

import httpx
import pytest
from typer.testing import CliRunner

from jobagent import cli
from jobagent.core.store import connect, get_job, init_db, list_jobs
from jobagent.match.filters import Region, board_region, check
from jobagent.sources.base import Job
from jobagent.sources.boards import (
    BoardsClient,
    Company,
    load_companies,
    parse_ashby,
    parse_greenhouse,
    parse_lever,
)
from jobagent.sources.feed import fetch_boards

TODAY = date(2026, 11, 16)
ACME_GH = Company("Acme AI", "greenhouse", "acmeai")
ACME_LV = Company("Acme AI", "lever", "acmeai")
ACME_AB = Company("Acme AI", "ashby", "acmeai")


# --- Parsers -------------------------------------------------------------------------------


def test_parse_greenhouse_unescapes_the_html():
    job = parse_greenhouse(
        {
            "id": 123,
            "title": " Machine Learning Engineer ",
            "location": {"name": "London, UK"},
            "absolute_url": "https://boards.greenhouse.io/acmeai/jobs/123",
            "content": "&lt;p&gt;Build &lt;b&gt;RAG&lt;/b&gt; systems.&lt;/p&gt;",
            "first_published": "2026-11-01T09:00:00-04:00",
        },
        ACME_GH,
    )
    assert job.source == "greenhouse" and job.external_id == "acmeai:123"
    assert job.title == "Machine Learning Engineer" and job.company == "Acme AI"
    assert job.description.split() == ["Build", "RAG", "systems."]  # tags gone
    assert job.posted_at == date(2026, 11, 1)


def test_parse_lever_joins_sections_and_locations():
    job = parse_lever(
        {
            "id": "abc",
            "text": "AI Engineer",
            "categories": {
                "location": "London",
                "allLocations": ["London", "Remote - UK"],
            },
            "hostedUrl": "https://jobs.lever.co/acmeai/abc",
            "descriptionPlain": "About the role.",
            "lists": [
                {"text": "Requirements", "content": "<li>Python</li><li>RAG</li>"}
            ],
            "additionalPlain": "Benefits.",
            "createdAt": 1762300800000,
        },
        ACME_LV,
    )
    assert job.location == "London; Remote - UK"
    assert "Requirements" in job.description and "Python" in job.description
    assert job.posted_at == date(2025, 11, 5)


def test_parse_ashby_uses_secondary_locations():
    job = parse_ashby(
        {
            "id": "u-1",
            "title": "Forward Deployed Engineer",
            "location": "New York",
            "secondaryLocations": [{"location": "London"}],
            "jobUrl": "https://jobs.ashbyhq.com/acmeai/u-1",
            "descriptionPlain": "Work with customers.",
            "publishedAt": "2026-11-10T12:00:00.000Z",
        },
        ACME_AB,
    )
    assert job.location == "New York; London" and job.posted_at == date(2026, 11, 10)
    assert board_region(job.location) is Region.LONDON


def test_companies_file_is_valid():
    companies = load_companies()
    assert len(companies) >= 50
    assert len({(c.ats, c.slug) for c in companies}) == len(companies)  # no duplicates


# --- Where is a board job? ------------------------------------------------------------------


@pytest.mark.parametrize(
    ("location", "region"),
    [
        ("London, UK", Region.LONDON),
        ("San Francisco, CA; London", Region.LONDON),
        ("Remote - UK", Region.REMOTE),
        ("Remote (United Kingdom)", Region.REMOTE),
        ("Manchester, England", Region.ENGLAND),
        ("Bristol", Region.SOUTH),
        ("UK", Region.ENGLAND),
        # Worldwide boards: no UK signal means not for you.
        ("Remote", Region.EXCLUDED),
        ("San Francisco", Region.EXCLUDED),
        ("New York", Region.EXCLUDED),  # "York" inside "New York"
        # English names, other countries.
        ("Cambridge, MA", Region.EXCLUDED),
        ("Richmond, VA", Region.EXCLUDED),
        ("London, ON", Region.EXCLUDED),
        ("Edinburgh, UK", Region.EXCLUDED),  # UK, but not England (your choice)
    ],
)
def test_board_region(location, region):
    assert board_region(location) is region


def board_job(**kw):
    base = {
        "source": "ashby",
        "external_id": "acmeai:1",
        "title": "Machine Learning Engineer",
        "company": "Acme AI",
        "location": "London",
        "url": "https://jobs.ashbyhq.com/acmeai/1",
        "description": "Full ad text.",
        "posted_at": date(2026, 6, 1),
    }
    return Job(**(base | kw))


def test_board_jobs_skip_the_age_check():
    # Still on the company's own board = still open, however old the posting.
    assert check(board_job(), TODAY, None, worldwide=True).keep
    assert not check(board_job(), TODAY, 14).keep  # the Reed rule would drop it


# --- Fetching -------------------------------------------------------------------------------


class FakeBoards:
    def __init__(self, jobs_by_slug, failing=()):
        self.jobs_by_slug, self.failing = jobs_by_slug, set(failing)
        self.requests = 0

    def jobs(self, company):
        if company.slug in self.failing:
            request = httpx.Request("GET", "https://x")
            raise httpx.HTTPStatusError(
                "404", request=request, response=httpx.Response(404, request=request)
            )
        return iter(self.jobs_by_slug.get(company.slug, []))


@pytest.fixture
def conn():
    c = connect()
    init_db(c)
    return c


def test_fetch_boards_keeps_uk_target_roles_and_saves_the_full_ad(conn):
    jobs = [
        board_job(external_id="a:1"),
        board_job(external_id="a:2", title="Account Executive"),
        board_job(external_id="a:3", title="AI Engineer", location="San Francisco"),
    ]
    stats = fetch_boards(
        conn, FakeBoards({"a": jobs}), [Company("Acme AI", "ashby", "a")], TODAY
    )
    assert stats.companies == 1 and stats.kept == 1 and stats.new == 1
    assert stats.dropped == {
        "title not a target role": 1,
        "not in England or UK-remote": 1,
    }
    (row,) = list_jobs(conn)
    assert get_job(conn, row["id"])["full_description"] == "Full ad text."


def test_one_failing_board_is_named_and_the_rest_still_run(conn):
    companies = [
        Company("Gone Ltd", "greenhouse", "gone"),
        Company("Acme AI", "ashby", "a"),
    ]
    stats = fetch_boards(
        conn, FakeBoards({"a": [board_job()]}, failing={"gone"}), companies, TODAY
    )
    assert stats.failed == ["Gone Ltd (greenhouse/gone): HTTP 404"]
    assert stats.kept == 1


def test_client_builds_the_right_urls():
    seen = []

    def handler(request):
        seen.append(str(request.url))
        if "greenhouse" in request.url.host:
            return httpx.Response(200, json={"jobs": []})
        if "lever" in request.url.host:
            return httpx.Response(200, json=[])
        return httpx.Response(200, json={"jobs": []})

    client = BoardsClient(httpx.Client(transport=httpx.MockTransport(handler)))
    for company in (
        ACME_GH,
        ACME_LV,
        Company("Eu Co", "lever", "euco", eu=True),
        ACME_AB,
    ):
        list(client.jobs(company))
    assert seen == [
        "https://boards-api.greenhouse.io/v1/boards/acmeai/jobs?content=true",
        "https://api.lever.co/v0/postings/acmeai?mode=json",
        "https://api.eu.lever.co/v0/postings/euco?mode=json",
        "https://api.ashbyhq.com/posting-api/job-board/acmeai",
    ]


def test_fetch_command_reports_boards(tmp_path, monkeypatch):
    monkeypatch.setenv("DB_PATH", str(tmp_path / "t.db"))
    monkeypatch.setattr(
        cli,
        "_make_boards",
        lambda: FakeBoards({"anthropic": [board_job(source="greenhouse")]}),
    )
    out = CliRunner().invoke(cli.app, ["jobs", "fetch", "--source", "boards"]).output
    assert "Company boards:" in out and "1 match" in out and "1 new job(s)" in out


def test_ashby_job_links_to_the_careers_site_when_ashby_pages_are_off():
    company = Company("Acme AI", "ashby", "acmeai", careers="https://acme.ai/careers")
    item = {"id": "u-2", "title": "AI Engineer", "jobUrl": "https://jobs.ashbyhq.com/x"}
    assert parse_ashby(item, company).url == "https://acme.ai/careers"
    assert parse_ashby(item, ACME_AB).url == "https://jobs.ashbyhq.com/x"  # default


def test_careers_url_can_name_the_job_page_after_its_title():
    company = Company(
        "Acme", "ashby", "acme", careers="https://acme.ai/careers/{title}"
    )
    item = {"id": "u-3", "title": "Forward Deployed Engineer, Western Europe"}
    assert parse_ashby(item, company).url == (
        "https://acme.ai/careers/forward-deployed-engineer-western-europe"
    )
