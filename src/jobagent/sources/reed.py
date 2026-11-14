"""Reed Jobseeker API (free key from reed.co.uk/developers).

Auth is HTTP Basic: the API key is the username, the password is empty.
Search returns at most 100 results per request; we page with resultsToSkip.
"""

import re
from collections.abc import Iterator
from datetime import date, datetime

import httpx
from bs4 import BeautifulSoup

from jobagent.sources.base import USER_AGENT, Job

BASE_URL = "https://www.reed.co.uk/api/1.0"
PAGE_SIZE = 100  # Reed's maximum


def _parse_date(value: str | None) -> date | None:
    """Reed dates look like 13/11/2026 (day first)."""
    if not value:
        return None
    try:
        return datetime.strptime(value, "%d/%m/%Y").date()
    except ValueError:
        return None  # an odd date shouldn't lose the job


def parse_job(item: dict) -> Job:
    return Job(
        source="reed",
        external_id=str(item["jobId"]),
        title=(item.get("jobTitle") or "").strip(),
        company=(item.get("employerName") or "").strip(),
        location=(item.get("locationName") or "").strip(),
        url=item.get("jobUrl") or f"https://www.reed.co.uk/jobs/{item['jobId']}",
        description=item.get("jobDescription") or "",
        salary_min=item.get("minimumSalary"),
        salary_max=item.get("maximumSalary"),
        posted_at=_parse_date(item.get("date")),
    )


class ReedClient:
    def __init__(self, api_key: str, http: httpx.Client | None = None):
        # Injected so tests can pass a fake transport; no network in tests.
        self.http = http or httpx.Client(timeout=30)
        self.auth = (api_key, "")
        self.requests = 0  # how many calls we made: be a polite API user

    def _get(self, path: str, params: dict | None = None) -> dict:
        self.requests += 1
        response = self.http.get(
            f"{BASE_URL}{path}",
            params=params,
            auth=self.auth,
            headers={"User-Agent": USER_AGENT},
        )
        response.raise_for_status()  # fail loudly: a bad key must not look like "0 jobs"
        return response.json()

    def search(self, keywords: str, max_results: int = 200) -> Iterator[Job]:
        """All UK jobs matching `keywords`, newest first, up to `max_results`."""
        skip = 0
        while skip < max_results:
            take = min(PAGE_SIZE, max_results - skip)
            data = self._get(
                "/search",
                {"keywords": keywords, "resultsToTake": take, "resultsToSkip": skip},
            )
            results = data.get("results") or []
            yield from (parse_job(item) for item in results)
            if len(results) < take:  # last page
                return
            skip += take

    def full_description(self, job_id: str) -> str:
        """Search only returns a snippet; the details endpoint has the full ad (HTML)."""
        html = self._get(f"/jobs/{job_id}").get("jobDescription") or ""
        return html_to_text(html)


def html_to_text(html: str) -> str:
    """Job ads arrive as HTML. Keep line breaks between blocks so lists stay readable."""
    text = BeautifulSoup(html, "html.parser").get_text("\n")
    return re.sub(r"\n\s*\n+", "\n", text).strip()
