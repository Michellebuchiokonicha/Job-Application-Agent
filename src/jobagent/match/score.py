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
from dataclasses import dataclass, field, replace
from datetime import date

import httpx
from pydantic import BaseModel, Field, ValidationError

from jobagent.inbox.classify import ChatClient
from jobagent.match.filters import based_abroad

PROMPT_VERSION = "v7"  # v7: second look for proof in experience and projects
MAX_JOB_CHARS = 8000
# Ollama's default context window can be as small as 2-4k tokens; CV + ad + answer need more.
# Without this, the start of the prompt is silently cut off.
NUM_CTX = 16384
# Guards against a looping model (it once ran 5 hours on one job). A real answer is well
# under 2k tokens, so hitting the output cap means a loop; the timeout catches the rest.
NUM_PREDICT = 4096
MODEL_TIMEOUT = 300  # seconds per call


class ModelStuck(Exception):
    """The model looped or went silent: skip this job, say why, never wait forever."""


SYSTEM_PROMPT = """\
You are a strict, careful recruiter. Check one job ad against the candidate's CV.

1. List the job's requirements: skills, experience, qualifications, tools. ONE skill or \
tool per item: split lists, so "CI/CD, observability and infrastructure as code" is three \
requirements. job_quote: the shortest exact words from the ad that name that one thing \
(e.g. "infrastructure as code"). Mark essential = false for "nice to have", "ideally", \
"bonus", "desirable".
   Do NOT list location, hybrid/remote, contract type, salary or benefits as requirements.
2. For each requirement decide met = true only if the CV shows THE SAME skill or \
experience. A related but different skill does not count (e.g. building an edtech app is \
not "integration with ERP/CRM systems"). If unsure, met = false.
   When met = true, cv_quote must be copied exactly from the CANDIDATE CV section (never \
from the job ad) and directly show it. When met = false, cv_quote is "".
3. dealbreakers: only if the ad states them: security clearance required, a PhD stated as \
essential, far more years of experience than CANDIDATE FACTS, a fee to join.
4. concerns: practical issues worth checking, not skills: e.g. contract (not permanent), \
immediate start (the candidate is available from the date in CANDIDATE FACTS), on-site \
outside London, heavy travel.
5. Years of experience: use CANDIDATE FACTS (computed from the CV's dates). Do not count \
years yourself."""


class Requirement(BaseModel):
    requirement: str = Field(max_length=200)
    job_quote: str = Field(max_length=400)
    essential: bool
    met: bool
    cv_quote: str = Field(max_length=400)


# Second pass: one narrow question per "met" claim. The first pass checks quotes exist; this
# checks they prove the point. ("Built FastAPI services" is real, but not ERP/CRM integration.)
VERIFY_PROMPT = """\
You check claims on a job application. For each numbered item, answer whether the CV line, \
on its own, directly shows the candidate has that requirement (the ad's wording is given \
for context). Be strict about specific things, fair about general ones:
- If the requirement names a specific tool, system, domain or practice (e.g. ERP, CRM, \
infrastructure as code, Power Automate, Kubernetes, a named cloud), the CV line must show \
that same thing.
- If the requirement is general (e.g. "AI applications", "work with stakeholders"), one \
clear, concrete example in the CV line is enough.
- General engineering work does not prove a specific tool, domain or practice.
- Giving talks or teaching does not prove hands-on use of something.
- If the CV line is about something else, the answer is false.
Answer for every number."""


# Second look, for claims the verifier rejected. The first pass often cites a skills-list
# line ("Python, SQL, TypeScript…"), which the verifier rightly rejects; but the proof was
# in a bullet it never looked at. This call only sees experience and projects.
EVIDENCE_PROMPT = """\
Each numbered requirement below was claimed for a candidate, but the CV line given does \
not prove it. Look through the candidate's experience and projects for ONE line that \
directly shows hands-on work with that requirement. Copy that line exactly as cv_quote. \
Do not reuse the rejected line. If no line shows it, cv_quote is "". Answer for every \
number."""


class Evidence(BaseModel):
    number: int
    cv_quote: str = Field(max_length=400)


class EvidenceSearch(BaseModel):
    items: list[Evidence]


class Judgement(BaseModel):
    number: int
    directly_shows: bool


class Verification(BaseModel):
    judgements: list[Judgement]


class FitAssessment(BaseModel):
    # No summary from the model: it was written before verification and could contradict
    # the final result ("meets all essentials" next to six missing). Code writes it instead.
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
    job_quote: str = ""  # the ad's wording, so the verifier knows what was asked
    ad_line: int | None = None  # which line of the ad it came from (for weighting)
    second_look: bool = False  # proof found on a second look, in experience or projects


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
    title_gaps: list[str] = field(
        default_factory=list
    )  # missing skills named in the title


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


# A heading line: "PROFESSIONAL EXPERIENCE", "Work Experience", "EXPERIENCE". Only short,
# capitalised lines, so a wrapped sentence ending "…years of experience" doesn't count.
_EXPERIENCE_HEADING = re.compile(
    r"^[ \t]*(?:[A-Z][A-Za-z&]* ){0,3}(?:EXPERIENCE|Experience)[ \t]*$", re.MULTILINE
)


def evidence_section(cv: str) -> str:
    """The CV from its experience heading on: jobs, projects, education.

    Leaves out the profile and skills list, which name skills without showing them. If
    there's no such heading, the whole CV (better a weaker second look than none).
    """
    m = _EXPERIENCE_HEADING.search(cv)
    return cv[m.start() :] if m else cv


_OPTIONAL = re.compile(
    r"\b(nice to have|nice-to-have|bonus|desirable|ideally|preferred|a plus|advantageous|"
    r"beneficial|not essential|would be great)\b"
)


def ad_line(job_quote: str, ad: str) -> int | None:
    """Which line of the ad a quote is on; None if it isn't found word for word."""
    q = _normalise(job_quote)
    flat = re.sub(r"[ \t]+", " ", ad.lower())
    at = flat.find(q) if q else -1
    return None if at == -1 else flat.count("\n", 0, at)


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


def _line_scores(reqs: list[CheckedRequirement]) -> list[float]:
    """Each line of the ad counts once: the fraction of its requirements that are met.

    Requirements are judged one skill at a time, but weighted per ad line, so "ERP, CRM,
    finance and document platforms" (one bullet, four skills) weighs the same as "RAG".
    A requirement whose line can't be found (a light paraphrase) counts as its own line.
    """
    lines: dict[object, list[bool]] = {}
    for i, r in enumerate(reqs):
        key = r.ad_line if r.ad_line is not None else ("own", i)
        lines.setdefault(key, []).append(r.met)
    return [sum(met) / len(met) for met in lines.values()]


def compute_score(reqs: list[CheckedRequirement], dealbreakers: list[str]) -> int:
    """80 points for essential lines of the ad, 20 for nice-to-have lines.

    If the ad marks nothing as optional (or nothing as essential), that one group carries
    all 100 points. Any dealbreaker caps the score at 30.
    """
    if not reqs:
        return 0
    essential = _line_scores([r for r in reqs if r.essential])
    optional = _line_scores([r for r in reqs if not r.essential])
    if not essential or not optional:
        both = essential + optional
        score = 100 * sum(both) / len(both)
    else:
        score = 80 * sum(essential) / len(essential)
        score += 20 * sum(optional) / len(optional)
    score = round(score)
    return min(score, 30) if dealbreakers else score


def summarise(reqs: list[CheckedRequirement], dealbreakers: list[str]) -> str:
    """One line built from the checked results, so it always agrees with the score."""
    essential = [r for r in reqs if r.essential]
    optional = [r for r in reqs if not r.essential]
    parts = [f"Meets {sum(r.met for r in essential)} of {len(essential)} essentials"]
    if optional:
        parts.append(f"{sum(r.met for r in optional)} of {len(optional)} nice-to-haves")
    text = " and ".join(parts) + "."
    if gaps := [r.requirement for r in essential if not r.met]:
        text += " Main gaps: " + "; ".join(gaps[:3]) + ("…" if len(gaps) > 3 else ".")
    if dealbreakers:
        text += " Dealbreaker: " + "; ".join(dealbreakers) + "."
    return text


# Missing a skill the job title names ("AI Automation Engineer (Python, R, Shiny)") means
# missing the core of the job, however many other lines are met: cap at "stretch".
TITLE_GAP_CAP = 55
ABROAD = "Based outside the UK: you can't take it from here"


# Words that name the role itself, not a skill. "Machine Learning" missing from a "Machine
# Learning Engineer" job is a model error (it once cited a CV heading as proof, which was
# rightly rejected), not a real gap: it must not trigger the cap.
_ROLE_WORDS = frozenset(
    [
        "senior",
        "junior",
        "lead",
        "principal",
        "staff",
        "head",
        "mid",
        "level",
        "machine",
        "learning",
        "ml",
        "ai",
        "artificial",
        "intelligence",
        "engineer",
        "engineering",
        "developer",
        "scientist",
        "science",
        "data",
        "applied",
        "forward",
        "deployed",
        "solutions",
        "solution",
        "architect",
        "automation",
        "mlops",
        "ops",
        "software",
        "generative",
        "genai",
        "agentic",
    ]
)


def title_gaps(reqs: list[CheckedRequirement], title: str) -> list[str]:
    """Unmet skills named in the job title (e.g. "R/Shiny"), ignoring the role's own words."""
    in_title = set(_WORD.findall(title.lower()))
    return [
        r.requirement
        for r in reqs
        if not r.met
        and (words := _WORD.findall(r.requirement.lower()))
        and all(w in in_title for w in words)
        and not all(w in _ROLE_WORDS for w in words)
    ]


def finalise(fit: Fit, title: str) -> None:
    """Score, verdict and summary from the final requirements. Run after every change."""
    fit.title_gaps = title_gaps(fit.requirements, title)
    score = compute_score(fit.requirements, fit.dealbreakers)
    if fit.title_gaps:
        score = min(score, TITLE_GAP_CAP)
    fit.score, fit.verdict = score, verdict_for(score)
    fit.summary = summarise(fit.requirements, fit.dealbreakers)
    if fit.title_gaps:
        fit.summary += " The job title names " + ", ".join(fit.title_gaps) + "."


def verdict_for(score: int) -> str:
    if score >= 80:
        return "strong"
    if score >= 60:
        return "worth applying"
    if score >= 40:
        return "stretch"
    return "poor fit"


def check(assessment: FitAssessment, description: str, cv: str, title: str = "") -> Fit:
    """Verify every quote, then compute the score from what survived."""
    checked = []
    seen = set()
    unverified_job = unverified_cv = 0
    for r in assessment.requirements:
        # A requirement must come from the ad. In testing, the model once listed ~70
        # "requirements" copied from the CV (every framework, "Google Developer Expert"),
        # all "met", inflating the score. Same light-paraphrase tolerance as CV quotes.
        if not cv_supports(r.job_quote, description):
            unverified_job += 1
            continue
        if (key := r.requirement.strip().lower()) in seen:
            continue  # listed twice: count once
        seen.add(key)
        optional = is_optional(r.job_quote, description)  # None: a light paraphrase
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
                job_quote=r.job_quote,
                ad_line=ad_line(r.job_quote, description),
            )
        )
    dealbreakers = list(assessment.dealbreakers)
    if based_abroad(description):  # checked in code: the model missed "USA based"
        dealbreakers.append(ABROAD)
    fit = Fit(
        score=0,
        verdict="",
        summary="",
        requirements=checked,
        dealbreakers=dealbreakers,
        concerns=assessment.concerns,
        unverified_job_quotes=unverified_job,
        unverified_cv_quotes=unverified_cv,
    )
    finalise(fit, title)
    return fit


def recount(saved: dict, description: str, title: str) -> Fit:
    """Re-apply today's scoring rules to judgements saved earlier. No model call.

    The model's yes/no answers are kept; only the counting is redone (weights, caps,
    dealbreakers found in code). Use after changing scoring rules, not the prompt.
    """
    reqs = [
        CheckedRequirement(
            **{**r, "ad_line": ad_line(r.get("job_quote", ""), description)}
        )
        for r in saved["requirements"]
    ]
    fit = Fit(**{**saved, "requirements": reqs})
    fit.dealbreakers = [d for d in fit.dealbreakers if d != ABROAD]
    if based_abroad(description):
        fit.dealbreakers.append(ABROAD)
    finalise(fit, title)
    return fit


class FitScorer:
    def __init__(self, client: ChatClient, model: str, retries: int = 1):
        self.client = client
        self.model = model
        self.retries = retries
        self.last_failure = ""  # why score() last returned None (for the summary)

    def _chat(self, messages: list[dict], schema: dict) -> str:
        """One model call, returning the reply text. Raises ModelStuck on a loop or timeout.

        No retry for those: at temperature 0 the same prompt loops the same way again.
        """
        try:
            response = self.client.chat(
                model=self.model,
                messages=messages,
                format=schema,
                options={
                    "temperature": 0,
                    "num_ctx": NUM_CTX,
                    "num_predict": NUM_PREDICT,
                },
                think=False,
            )
        except httpx.TimeoutException:
            raise ModelStuck(f"no answer within {MODEL_TIMEOUT}s") from None
        if response.get("done_reason") == "length":
            raise ModelStuck(f"answer cut off at {NUM_PREDICT} tokens (model looping)")
        return response["message"]["content"]

    def score(self, title: str, company: str, description: str, cv: str) -> Fit | None:
        self.last_failure = ""
        messages = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": build_prompt(title, company, description, cv)},
        ]
        for _ in range(1 + self.retries):
            try:
                content = self._chat(messages, FitAssessment.model_json_schema())
            except ModelStuck as stuck:
                self.last_failure = str(stuck)
                return None
            try:
                assessment = FitAssessment.model_validate_json(content)
            except ValidationError:
                continue
            fit = check(assessment, description, cv, title)
            self._verify(fit, cv)
            finalise(fit, title)  # again: the verifier may have changed what's met
            return fit
        self.last_failure = f"invalid answer {1 + self.retries} times"
        return None  # never valid: the caller records a failure, never a fake score

    def _judge(self, claims: list[CheckedRequirement]) -> set[int] | None:
        """Ask, claim by claim, whether each CV quote proves its requirement.

        Returns the numbers (1-based) of the claims it rejects, or None if the model gave
        no usable answer.
        """
        pairs = "\n\n".join(
            f"{i}. Requirement: {r.requirement}\n   Ad says: {r.job_quote}\n"
            f"   CV line: {r.cv_quote}"
            for i, r in enumerate(claims, start=1)
        )
        messages = [
            {"role": "system", "content": VERIFY_PROMPT},
            {"role": "user", "content": pairs},
        ]
        for _ in range(1 + self.retries):
            try:
                content = self._chat(messages, Verification.model_json_schema())
            except ModelStuck:
                return None
            try:
                verdicts = Verification.model_validate_json(content)
            except ValidationError:
                continue
            numbers = set(range(1, len(claims) + 1))
            return {
                j.number for j in verdicts.judgements if not j.directly_shows
            } & numbers
        return None

    def _verify(self, fit: Fit, cv: str) -> None:
        """Check every "met" claim; give rejected ones a second look for better proof."""
        claims = [r for r in fit.requirements if r.met]
        if not claims:
            return
        rejected = self._judge(claims)
        if rejected is None:
            fit.verifier_failed = True  # reported, so it's never silently trusted
            return
        doubted = [r for i, r in enumerate(claims, start=1) if i in rejected]
        for r in doubted:
            r.met, r.not_proven = False, True
        self._second_look(doubted, cv)
        fit.not_proven = sum(r.not_proven for r in fit.requirements)

    def _second_look(self, doubted: list[CheckedRequirement], cv: str) -> None:
        """Look for a better line in experience and projects, then judge it the same way.

        A bonus step: if anything fails, the rejections simply stand.
        """
        if not doubted:
            return
        evidence = evidence_section(cv)
        listing = "\n\n".join(
            f"{i}. Requirement: {r.requirement}\n   Ad says: {r.job_quote}\n"
            f"   Rejected CV line: {r.cv_quote}"
            for i, r in enumerate(doubted, start=1)
        )
        messages = [
            {"role": "system", "content": EVIDENCE_PROMPT},
            {
                "role": "user",
                "content": f"{listing}\n\nCANDIDATE EXPERIENCE AND PROJECTS:\n{evidence}",
            },
        ]
        try:
            content = self._chat(messages, EvidenceSearch.model_json_schema())
            found = EvidenceSearch.model_validate_json(content)
        except (ModelStuck, ValidationError):
            return
        candidates: dict[int, CheckedRequirement] = {}  # index in doubted -> new claim
        for item in found.items:
            quote = item.cv_quote.strip()
            if not 1 <= item.number <= len(doubted) or item.number - 1 in candidates:
                continue
            old = doubted[item.number - 1]
            # Code checks the line really is in experience/projects, and is a new one.
            if (
                quote
                and cv_supports(quote, evidence)
                and not quote_in(quote, old.cv_quote)
            ):
                candidates[item.number - 1] = replace(old, cv_quote=quote)
        if not candidates:
            return
        order = list(candidates)
        rejected = self._judge([candidates[k] for k in order])
        if rejected is None:
            return
        for n, k in enumerate(order, start=1):
            if n not in rejected:
                r = doubted[k]
                r.cv_quote, r.met, r.not_proven, r.second_look = (
                    candidates[k].cv_quote,
                    True,
                    False,
                    True,
                )
