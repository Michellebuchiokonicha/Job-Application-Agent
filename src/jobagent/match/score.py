"""Stage 2 of the funnel: the local model compares one job ad with one CV.

Same habits as the inbox classifier: structured output (a Pydantic schema), temperature 0,
and grounding: every requirement the model claims you meet must quote the job ad, and
code checks the quote is really there.
"""

import re
from dataclasses import dataclass
from datetime import date
from typing import Literal

from pydantic import BaseModel, Field, ValidationError

from jobagent.inbox.classify import ChatClient

PROMPT_VERSION = "v2"  # v2: years computed in code, contradictions removed
MAX_JOB_CHARS = 8000
# Ollama's default context window can be as small as 2-4k tokens; CV + ad + answer need more.
# Without this, the start of the prompt is silently cut off.
NUM_CTX = 16384

SYSTEM_PROMPT = """\
You are a careful recruiter. Compare one job ad with the candidate's CV and judge how well \
the candidate fits. Be honest: an inflated score wastes the candidate's time.

Rules:
- Only count a skill or experience as met if the CV clearly shows it. Never assume.
- "met": requirements from the ad that the CV satisfies. For each, quote the ad's wording \
exactly (job_quote) and say briefly where the CV shows it (cv_evidence).
- "missing": requirements from the ad the CV does not show, including "nice to have" \
ones (essential = false). A requirement is either met or missing, never both.
- "dealbreakers": things that rule the candidate out regardless of skills, only if the ad \
states them: e.g. security clearance required, a PhD stated as essential, far more years of \
experience than the CV shows, a fee to join, a start date long before the candidate is available.
- Years of experience: use the CANDIDATE FACTS line, which is computed from the CV's \
dates. Do not count years yourself.

Score (0-100):
- 80-100: meets nearly all essential requirements; a strong application.
- 60-79: meets most essentials, a few gaps; worth applying.
- 40-59: meets some essentials; a stretch.
- 0-39: missing several essentials, or a dealbreaker.
Any dealbreaker caps the score at 30.

Write the summary in at most two short sentences."""


class MetRequirement(BaseModel):
    requirement: str = Field(max_length=200)
    job_quote: str = Field(max_length=300)
    cv_evidence: str = Field(max_length=300)


class MissingRequirement(BaseModel):
    requirement: str = Field(max_length=200)
    essential: bool


class FitAssessment(BaseModel):
    score: int = Field(ge=0, le=100)
    verdict: Literal["strong", "worth applying", "stretch", "poor fit"]
    # Generous limit: constrained decoding cuts text off mid-word at the limit.
    summary: str = Field(max_length=800)
    met: list[MetRequirement]
    missing: list[MissingRequirement]
    dealbreakers: list[str]


@dataclass
class Fit:
    assessment: FitAssessment
    unverified_quotes: int  # met requirements whose quote isn't in the ad


def _normalise(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip().lower()


def quote_in(quote: str, text: str) -> bool:
    q = _normalise(quote)
    return bool(q) and q in _normalise(text)


_MONTHS = [
    "jan",
    "feb",
    "mar",
    "apr",
    "may",
    "jun",
    "jul",
    "aug",
    "sep",
    "oct",
    "nov",
    "dec",
]
_RANGE = re.compile(
    r"\b(" + "|".join(_MONTHS) + r")[a-z]*\.? (\d{4})\s*[-–]\s*"
    r"(present|current|(?:" + "|".join(_MONTHS) + r")[a-z]*\.? \d{4})",
    re.IGNORECASE,
)


def experience_years(cv: str, today: date) -> float | None:
    """Years since the earliest job start date in the CV ("Mar 2021 - Nov 2021").

    Computed in code because small models miscount dates: in testing, an 8B model
    called five years of experience "under three".
    """
    starts = [
        date(int(year), _MONTHS.index(month.lower()[:3]) + 1, 1)
        for month, year, _ in _RANGE.findall(cv)
    ]
    if not starts:
        return None
    return round((today - min(starts)).days / 365.25, 1)


def build_prompt(
    title: str, company: str, description: str, cv: str, today: date | None = None
) -> str:
    years = experience_years(cv, today or date.today())
    facts = (
        f"CANDIDATE FACTS: about {years} years of professional experience in total."
        if years is not None
        else "CANDIDATE FACTS: years of experience unknown."
    )
    return (
        f"JOB: {title} at {company}\n\n{description[:MAX_JOB_CHARS]}\n\n"
        f"---\n\n{facts}\n\nCANDIDATE CV:\n\n{cv}"
    )


def _drop_contradictions(fit: FitAssessment) -> None:
    """A requirement listed as both met and missing is treated as met.

    Matches loosely: "strong Python" (missing) vs "Python" (met) is the same requirement.
    """
    met = [_normalise(m.requirement) for m in fit.met]

    def also_met(requirement: str) -> bool:
        r = _normalise(requirement)
        return any(m and (m in r or r in m) for m in met)

    fit.missing = [m for m in fit.missing if not also_met(m.requirement)]


class FitScorer:
    def __init__(self, client: ChatClient, model: str, retries: int = 1):
        self.client = client
        self.model = model
        self.retries = retries

    def score(self, title: str, company: str, description: str, cv: str) -> Fit | None:
        messages = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": build_prompt(title, company, description, cv)},
        ]
        for _ in range(1 + self.retries):
            response = self.client.chat(
                model=self.model,
                messages=messages,
                format=FitAssessment.model_json_schema(),
                options={"temperature": 0, "num_ctx": NUM_CTX},
                think=False,
            )
            try:
                fit = FitAssessment.model_validate_json(response["message"]["content"])
            except ValidationError:
                continue
            _drop_contradictions(fit)
            if fit.dealbreakers:  # enforce the rule in code; don't just trust the model
                fit.score = min(fit.score, 30)
                fit.verdict = "poor fit"
            unverified = sum(not quote_in(m.job_quote, description) for m in fit.met)
            return Fit(fit, unverified)
        return None  # never valid: the caller records a failure, never a fake score
