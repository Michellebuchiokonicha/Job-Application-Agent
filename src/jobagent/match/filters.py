"""Stage 1 of the funnel: instant, rule-based filters. No model, no network.

Each filter returns a reason when it rejects a job, so `jobs fetch` can report how many
jobs were dropped and why. Nothing disappears silently.
"""

import re
from dataclasses import dataclass
from datetime import date, timedelta
from enum import IntEnum

from jobagent.sources.base import Job

# Search terms sent to the job APIs (one search each).
SEARCH_TERMS = (
    "machine learning engineer",
    "AI engineer",
    "MLOps engineer",
    "applied scientist",
    "forward deployed engineer",
    "AI solutions architect",
    "data scientist",
    "automation engineer",
)

# A title must match one of these. Lower-case; \b = word boundary.
_ROLE_PATTERNS = [
    # an AI/ML word, then a role word: "Senior ML Engineer", "AI Solutions Architect"
    re.compile(
        r"\b(machine learning|ml|mlops|ai|llm|gen ?ai|generative ai|deep learning)\b"
        r".*\b(engineer|developer|scientist|architect)\b"
    ),
    re.compile(r"\bmlops\b"),
    re.compile(r"\bapplied (ai |ml )?scientist\b"),
    re.compile(r"\bforward[- ]deployed\b"),
    re.compile(r"\bdata scientist\b"),
    re.compile(r"\bautomation (engineer|developer|specialist)\b"),
]

# "Automation engineer" also means software *testing* and *factory* automation, which
# are different careers. Drop those unless the title also has an AI/ML word.
_OFF_TOPIC_AUTOMATION = re.compile(
    r"\b(test|testing|qa|quality|plc|scada|controls?|electrical|mechanical|industrial|"
    r"building|manufacturing|process control|instrumentation)\b"
)
_AI_WORD = re.compile(r"\b(ai|ml|machine learning|llm|gen ?ai|intelligent|agentic)\b")


def matches_role(title: str) -> bool:
    t = title.lower()
    if not any(p.search(t) for p in _ROLE_PATTERNS):
        return False
    off_topic = "automation" in t and _OFF_TOPIC_AUTOMATION.search(t)
    return not (off_topic and not _AI_WORD.search(t))


class Region(IntEnum):
    """Lower = preferred. Used to sort the list."""

    # Values must be distinct: two equal values in an Enum become one member (an alias).
    LONDON = 0
    SOUTH = 1
    REMOTE = 2  # UK-remote: no relocation needed
    ENGLAND = 3
    EXCLUDED = 9


_EXCLUDED = re.compile(
    r"\b(scotland|wales|northern ireland|edinburgh|glasgow|aberdeen|dundee|inverness|"
    r"stirling|cardiff|swansea|wrexham|belfast|londonderry|derry|lisburn|dublin|ireland|cork|galway|limerick|waterford|kilkenny)\b"
)
_LONDON = re.compile(
    r"\b(london|city of london|canary wharf|westminster|shoreditch|croydon|stratford|"
    r"islington|camden|southwark|hammersmith|kensington|wimbledon|richmond|kingston)\b"
)
_SOUTH = re.compile(
    r"\b(reading|oxford|cambridge|brighton|guildford|milton keynes|southampton|"
    r"portsmouth|bristol|bath|slough|maidenhead|woking|watford|st albans|crawley|"
    r"basingstoke|farnborough|bracknell|winchester|chelmsford|luton|hemel hempstead|"
    r"stevenage|exeter|swindon|cheltenham|gloucester|maidstone|canterbury|"
    r"tunbridge wells|surrey|berkshire|hampshire|kent|sussex|essex|oxfordshire|"
    r"buckinghamshire|hertfordshire|bedfordshire|wiltshire|south east|south west|"
    r"home counties)\b"
)
_REMOTE = re.compile(r"\b(remote|work from home|home[- ]based|fully remote)\b")

# Some ads give only a postcode ("CM20 1WG"). The letters at the start (the postcode
# *area*) say roughly where it is.
_POSTCODE = re.compile(r"^([A-Z]{1,2})\d[A-Z\d]?\s*(\d[A-Z]{2})?$")


def _codes(text: str) -> frozenset[str]:
    return frozenset(text.split())


_AREAS = {
    # Scotland, Wales, Northern Ireland
    Region.EXCLUDED: _codes(
        "AB DD DG EH FK G HS IV KA KW KY ML PA PH TD ZE CF LD LL NP SA BT"
    ),
    # inner London, then outer London boroughs
    Region.LONDON: _codes("E EC N NW SE SW W WC BR CR EN HA IG KT RM SM TW UB"),
    Region.SOUTH: _codes(
        "AL BA BH BN BS CB CM CO CT DA DT EX GL GU HP IP LU ME MK OX PL PO RG RH SG "
        "SL SN SO SP SS TA TN TQ TR WD"
    ),
}


def _region_from_postcode(location: str) -> Region | None:
    match = _POSTCODE.match(location.strip().upper())
    if not match:
        return None
    area = match.group(1)
    for region, areas in _AREAS.items():
        if area in areas:
            return region
    return Region.ENGLAND


def region_for(location: str) -> Region:
    """England only; London first, then the South (and UK-remote), then the rest.

    Excluded is checked first: "London" must not rescue "Londonderry", hence \\b.
    Unknown English towns fall to ENGLAND: Reed is a UK board, and the excluded list
    already catches Scotland, Wales and Northern Ireland.
    """
    if (from_postcode := _region_from_postcode(location)) is not None:
        return from_postcode
    loc = location.lower()
    if _EXCLUDED.search(loc):
        return Region.EXCLUDED
    if _LONDON.search(loc):
        return Region.LONDON
    if _SOUTH.search(loc):
        return Region.SOUTH
    if _REMOTE.search(loc):
        return Region.REMOTE
    return Region.ENGLAND


@dataclass(frozen=True)
class Verdict:
    keep: bool
    reason: str  # why it was dropped; "" when kept
    region: Region


def check(job: Job, today: date, max_age_days: int = 14) -> Verdict:
    region = region_for(job.location)
    if not matches_role(job.title):
        return Verdict(False, "title not a target role", region)
    if region is Region.EXCLUDED:
        return Verdict(False, "outside England", region)
    if job.posted_at and job.posted_at < today - timedelta(days=max_age_days):
        return Verdict(False, f"older than {max_age_days} days", region)
    return Verdict(True, "", region)
