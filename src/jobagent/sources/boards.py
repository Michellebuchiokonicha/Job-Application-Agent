"""Company job boards on Greenhouse, Lever and Ashby: public APIs, no key, no login.

Each returns *every* open job at one company, with the full ad. Most tech and AI companies
host their careers page on one of these, so a list of companies (companies.toml) covers
"company sites" without scraping anything.

Docs:
- Greenhouse: https://developers.greenhouse.io/job-board.html
- Lever: https://github.com/lever/postings-api
- Ashby: https://developers.ashbyhq.com/docs/public-job-posting-api
"""

import html
import tomllib
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path

import httpx

from jobagent.sources.base import USER_AGENT, Job
from jobagent.sources.reed import html_to_text

COMPANIES_FILE = Path(__file__).with_name("companies.toml")
ATS_TYPES = ("greenhouse", "lever", "ashby")


@dataclass(frozen=True)
class Company:
    name: str
    ats: str  # "greenhouse", "lever" or "ashby"
    slug: str  # the company's ID on that ATS, e.g. boards.greenhouse.io/<slug>
    eu: bool = False  # Lever only: hosted on api.eu.lever.co


def load_companies(path: Path = COMPANIES_FILE) -> list[Company]:
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    companies = [Company(**c) for c in data["company"]]
    for c in companies:
        if c.ats not in ATS_TYPES:
            raise ValueError(f"{c.name}: unknown ATS {c.ats!r} (use {ATS_TYPES})")
    return companies


def _date_from_iso(value: str | None) -> date | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value).date()  # handles "Z" since Python 3.11
    except ValueError:
        return None


def _date_from_ms(value: int | None) -> date | None:
    return datetime.fromtimestamp(value / 1000, UTC).date() if value else None


# ---- Parsers: one API response item -> Job --------------------------------------------------


def parse_greenhouse(item: dict, company: Company) -> Job:
    # `content` is HTML, escaped once more ("&lt;p&gt;"), so unescape before parsing.
    return Job(
        source="greenhouse",
        external_id=f"{company.slug}:{item['id']}",
        title=(item.get("title") or "").strip(),
        company=company.name,
        location=((item.get("location") or {}).get("name") or "").strip(),
        url=item.get("absolute_url") or "",
        description=html_to_text(html.unescape(item.get("content") or "")),
        posted_at=_date_from_iso(item.get("first_published") or item.get("updated_at")),
    )


def parse_lever(item: dict, company: Company) -> Job:
    parts = [item.get("descriptionPlain") or ""]
    for section in item.get("lists") or []:  # "What you'll do", "Requirements"...
        parts.append(section.get("text") or "")
        parts.append(html_to_text(section.get("content") or ""))
    parts.append(item.get("additionalPlain") or "")
    categories = item.get("categories") or {}
    locations = categories.get("allLocations") or [categories.get("location") or ""]
    return Job(
        source="lever",
        external_id=f"{company.slug}:{item['id']}",
        title=(item.get("text") or "").strip(),
        company=company.name,
        location="; ".join(loc for loc in locations if loc),
        url=item.get("hostedUrl") or "",
        description="\n".join(p.strip() for p in parts if p and p.strip()),
        posted_at=_date_from_ms(item.get("createdAt")),
    )


def parse_ashby(item: dict, company: Company) -> Job:
    locations = [item.get("location") or ""] + [
        loc.get("location") or "" for loc in item.get("secondaryLocations") or []
    ]
    description = item.get("descriptionPlain") or html_to_text(
        item.get("descriptionHtml") or ""
    )
    return Job(
        source="ashby",
        external_id=f"{company.slug}:{item['id']}",
        title=(item.get("title") or "").strip(),
        company=company.name,
        location="; ".join(loc for loc in locations if loc),
        url=item.get("jobUrl") or "",
        description=description.strip(),
        posted_at=_date_from_iso(item.get("publishedAt")),
    )


# ---- Client ---------------------------------------------------------------------------------


class BoardsClient:
    def __init__(self, http: httpx.Client | None = None):
        # Injected so tests can pass a fake transport; no network in tests.
        self.http = http or httpx.Client(
            timeout=30, headers={"User-Agent": USER_AGENT}, follow_redirects=True
        )
        self.requests = 0

    def _get(self, url: str, params: dict | None = None):
        self.requests += 1
        response = self.http.get(url, params=params)
        response.raise_for_status()  # a wrong slug must fail loudly, not look like 0 jobs
        return response.json()

    def jobs(self, company: Company) -> Iterator[Job]:
        """Every open job at this company, as Jobs (filtering happens later)."""
        if company.ats == "greenhouse":
            data = self._get(
                f"https://boards-api.greenhouse.io/v1/boards/{company.slug}/jobs",
                {"content": "true"},
            )
            yield from (parse_greenhouse(j, company) for j in data.get("jobs") or [])
        elif company.ats == "lever":
            host = "api.eu.lever.co" if company.eu else "api.lever.co"
            data = self._get(
                f"https://{host}/v0/postings/{company.slug}", {"mode": "json"}
            )
            yield from (parse_lever(j, company) for j in data or [])
        elif company.ats == "ashby":
            data = self._get(
                f"https://api.ashbyhq.com/posting-api/job-board/{company.slug}"
            )
            yield from (parse_ashby(j, company) for j in data.get("jobs") or [])
