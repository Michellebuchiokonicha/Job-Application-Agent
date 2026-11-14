"""Stage 2 of the funnel: the local model compares one job ad with one CV.

v1-v2 asked the model for a score directly. It was far too generous: it filed nearly every
requirement as "met", citing unrelated CV lines ("integrate with ERP/CRM" -> an edtech app),
and scored every job 85-95. So from v3:

- the model only makes small, checkable judgements: one requirement at a time, met or not,
  each with an exact quote from the ad AND an exact quote from the CV;
- code checks both quotes are real, and a "met" without a real CV quote counts as missing;
- code computes the score from the verified results.

Small models are better at narrow yes/no judgements than at one holistic number.
"""

import re
from dataclasses import dataclass
from datetime import date

from pydantic import BaseModel, Field, ValidationError

from jobagent.inbox.classify import ChatClient

PROMPT_VERSION = "v4"  # v4: second pass checks each CV quote really proves the claim
MAX_JOB_CHARS = 8000
# Ollama's default context window can be as small as 2-4k tokens; CV + ad + answer need more.
# Without this, the start of the prompt is silently cut off.
NUM_CTX = 16384

SYSTEM_PROMPT = """\
You are a strict, careful recruiter. Check one job ad against the candidate's CV.

1. List the job's requirements: skills, experience, qualifications, tools. One per item, \
copied from the ad (job_quote, exact words). Mark essential = false for "nice to have", \
"ideally", "bonus", "desirable".
   Do NOT list location, hybrid/remote, contract type, salary or benefits as requirements.
2. For each requirement decide met = true only if the CV shows THE SAME skill or \
experience. A related but different skill does not count (e.g. building an edtech app is \
not "integration with ERP/CRM systems"). If unsure, met = false.
   When met = true, cv_quote must be copied exactly from the CV and directly show it. \
When met = false, cv_quote is "".
3. dealbreakers: only if the ad states them: security clearance required, a PhD stated as \
essential, far more years of experience than CANDIDATE FACTS, a fee to join.
4. concerns: practical issues worth checking, not skills: e.g. contract (not permanent), \
immediate start (the candidate is available from the date in CANDIDATE FACTS), on-site \
outside London, heavy travel.
5. Years of experience: use CANDIDATE FACTS (computed from the CV's dates). Do not count \
years yourself.

summary: at most two short sentences."""


class Requirement(BaseModel):
    requirement: str = Field(max_length=200)
    job_quote: str = Field(max_length=400)
    essential: bool
    met: bool
    cv_quote: str = Field(max_length=400)


# Second pass: one narrow question per "met" claim. The first pass checks quotes exist; this
# checks they prove the point. ("Built FastAPI services" is real, but not ERP/CRM integration.)
VERIFY_PROMPT = """\
You check claims on a job application. For each numbered pair, answer whether the CV line, \
on its own, directly shows the candidate has THAT specific requirement. Be strict:
- Specific named things (e.g. ERP, CRM, infrastructure as code, Power Automate, Kubernetes, \
a named cloud or tool) must actually appear in, or be clearly done in, the CV line.
- General engineering work does not prove a specific tool, domain or practice.
- Giving talks or teaching does not prove hands-on use of something.
- If the CV line is about something else, the answer is false.
Answer for every number."""


class Judgement(BaseModel):
    number: int
    directly_shows: bool


class Verification(BaseModel):
    judgements: list[Judgement]


class FitAssessment(BaseModel):
    # Limits are generous: constrained decoding cuts text off mid-word at the limit.
    summary: str = Field(max_length=800)
    requirements: list[Requirement]
    dealbreakers: list[str]
    concerns: list[str]


@dataclass
class CheckedRequirement:
    requirement: str
    essential: bool
    met: bool  # after verification: claimed met AND the CV quote is real
    cv_quote: str
    claimed_but_unverified: bool  # model said met, but its CV quote isn't in the CV
    not_proven: bool = (
        False  # real CV quote, but the verifier says it doesn't prove this
    )


@dataclass
class Fit:
    score: int
    verdict: str
    summary: str
    requirements: list[CheckedRequirement]
    dealbreakers: list[str]
    concerns: list[str]
    unverified_job_quotes: int  # requirement quotes not found in the ad
    unverified_cv_quotes: int  # "met" claims whose CV quote isn't in the CV
    not_proven: int = 0  # "met" claims the verifier rejected as not proving the point
    verifier_failed: bool = False  # second pass gave no valid answer: claims left as-is


def _normalise(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip().lower()


def quote_in(quote: str, text: str) -> bool:
    q = _normalise(quote)
    return bool(q) and q in _normalise(text)


# Words like "c++", "c#" and "node.js" stay whole; a full stop at a sentence end doesn't stick.
_WORD = re.compile(r"[a-z0-9+#]+(?:\.[a-z0-9]+)*")
# Glue words carry no evidence; a paraphrase may swap "using" for "with".
_STOP = frozenset(
    [
        "the",
        "and",
        "with",
        "for",
        "using",
        "from",
        "into",
        "via",
        "our",
        "was",
        "were",
        "has",
        "have",
        "its",
    ]
)


def cv_supports(quote: str, cv: str, threshold: float = 0.85) -> bool:
    """Is this "CV quote" really from the CV?

    Exact match first. Otherwise accept a light paraphrase: at least 85% of the quote's
    meaningful words (3+ letters, not glue words like "with") appear in the CV. Small models often reword slightly ("built RAG
    pipelines" for "Architected RAG pipelines"); invented claims use words that aren't
    there at all.
    """
    if quote_in(quote, cv):
        return True
    words = [w for w in _WORD.findall(quote.lower()) if len(w) >= 3 and w not in _STOP]
    if not words:
        return False
    cv_words = set(_WORD.findall(cv.lower()))
    return sum(w in cv_words for w in words) / len(words) >= threshold


_OPTIONAL = re.compile(
    r"\b(nice to have|nice-to-have|bonus|desirable|ideally|preferred|a plus|advantageous|"
    r"beneficial|not essential|would be great)\b"
)


def is_optional(job_quote: str, ad: str) -> bool | None:
    """Decide "nice to have" from the ad's own wording; None if the quote isn't found.

    Looks at the quote plus the text before it on the same line, so "Nice to have:
    Kubernetes, AWS" marks Kubernetes optional. The model's own flag proved unreliable.
    """
    q, text = _normalise(job_quote), ad.lower()
    if not q:
        return None
    flat = re.sub(r"[ \t]+", " ", text)
    at = flat.find(q)
    if at == -1:
        return None
    line_start = flat.rfind("\n", 0, at) + 1
    return bool(_OPTIONAL.search(flat[line_start : at + len(q)]))


# --- Facts computed in code ------------------------------------------------------

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
_AVAILABLE = re.compile(
    r"available to start (?:in |from )?((?:" + "|".join(_MONTHS) + r")[a-z]* \d{4})",
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


def candidate_facts(cv: str, today: date) -> str:
    years = experience_years(cv, today)
    facts = [
        f"about {years} years of professional experience in total"
        if years is not None
        else "years of experience unknown"
    ]
    if available := _AVAILABLE.search(cv):
        facts.append(f"available to start from {available.group(1)}")
    return "CANDIDATE FACTS: " + "; ".join(facts) + "."


def build_prompt(
    title: str, company: str, description: str, cv: str, today: date | None = None
) -> str:
    return (
        f"JOB: {title} at {company}\n\n{description[:MAX_JOB_CHARS]}\n\n"
        f"---\n\n{candidate_facts(cv, today or date.today())}\n\nCANDIDATE CV:\n\n{cv}"
    )


# --- The score, computed in code ----------------------------------------------------


def compute_score(reqs: list[CheckedRequirement], dealbreakers: list[str]) -> int:
    """80 points for essential requirements met, 20 for nice-to-haves.

    If the ad marks nothing as optional (or nothing as essential), that one group carries
    all 100 points. Any dealbreaker caps the score at 30.
    """
    if not reqs:
        return 0
    essential = [r for r in reqs if r.essential]
    optional = [r for r in reqs if not r.essential]
    if not essential or not optional:
        score = 100 * sum(r.met for r in reqs) / len(reqs)
    else:
        score = 80 * sum(r.met for r in essential) / len(essential)
        score += 20 * sum(r.met for r in optional) / len(optional)
    score = round(score)
    return min(score, 30) if dealbreakers else score


def verdict_for(score: int) -> str:
    if score >= 80:
        return "strong"
    if score >= 60:
        return "worth applying"
    if score >= 40:
        return "stretch"
    return "poor fit"


def check(assessment: FitAssessment, description: str, cv: str) -> Fit:
    """Verify every quote, then compute the score from what survived."""
    checked = []
    unverified_job = unverified_cv = 0
    for r in assessment.requirements:
        optional = is_optional(r.job_quote, description)
        if optional is None:
            unverified_job += 1
        essential = r.essential if optional is None else not optional
        cv_ok = cv_supports(r.cv_quote, cv)
        if r.met and not cv_ok:
            unverified_cv += 1
        checked.append(
            CheckedRequirement(
                requirement=r.requirement,
                essential=essential,
                met=r.met and cv_ok,
                cv_quote=r.cv_quote if cv_ok else "",
                claimed_but_unverified=r.met and not cv_ok,
            )
        )
    score = compute_score(checked, assessment.dealbreakers)
    return Fit(
        score=score,
        verdict=verdict_for(score),
        summary=assessment.summary,
        requirements=checked,
        dealbreakers=assessment.dealbreakers,
        concerns=assessment.concerns,
        unverified_job_quotes=unverified_job,
        unverified_cv_quotes=unverified_cv,
    )


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
                assessment = FitAssessment.model_validate_json(
                    response["message"]["content"]
                )
            except ValidationError:
                continue
            fit = check(assessment, description, cv)
            self._verify(fit)
            return fit
        return None  # never valid: the caller records a failure, never a fake score

    def _verify(self, fit: Fit) -> None:
        """Ask, claim by claim, whether each CV quote proves its requirement."""
        claims = [r for r in fit.requirements if r.met]
        if not claims:
            return
        pairs = "\n\n".join(
            f"{i}. Requirement: {r.requirement}\n   CV line: {r.cv_quote}"
            for i, r in enumerate(claims, start=1)
        )
        for _ in range(1 + self.retries):
            response = self.client.chat(
                model=self.model,
                messages=[
                    {"role": "system", "content": VERIFY_PROMPT},
                    {"role": "user", "content": pairs},
                ],
                format=Verification.model_json_schema(),
                options={"temperature": 0, "num_ctx": NUM_CTX},
                think=False,
            )
            try:
                verdicts = Verification.model_validate_json(
                    response["message"]["content"]
                )
            except ValidationError:
                continue
            rejected = {j.number for j in verdicts.judgements if not j.directly_shows}
            for i, r in enumerate(claims, start=1):
                if i in rejected:
                    r.met, r.not_proven = False, True
            fit.not_proven = len(rejected & set(range(1, len(claims) + 1)))
            fit.score = compute_score(fit.requirements, fit.dealbreakers)
            fit.verdict = verdict_for(fit.score)
            return
        fit.verifier_failed = True  # reported, so it's never silently trusted
