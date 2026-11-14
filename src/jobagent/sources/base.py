"""The one shape every job source produces, so filters and storage never care where a
job came from."""

from dataclasses import dataclass
from datetime import date

# Honest user agent: says who we are, no personal contact details.
USER_AGENT = "jobagent/0.1 (personal job search tool)"


@dataclass(frozen=True)
class Job:
    source: str  # "reed", "greenhouse", ...
    external_id: str  # the source's own ID; (source, external_id) is unique
    title: str
    company: str
    location: str
    url: str
    # May be a snippet: some sources only give the full text on request.
    description: str
    salary_min: float | None = None
    salary_max: float | None = None
    posted_at: date | None = None
