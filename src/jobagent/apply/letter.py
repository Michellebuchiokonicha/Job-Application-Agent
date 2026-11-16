"""Cover letters: drafted by the local model, checked by code, approved by you.

The model writes; code checks what it can check. Every number must come from your CV or
the ad, and every name (tool, employer, project) from your CV, or it's flagged. A name only
the ad uses is allowed but shown to you, in case the letter claims it as your experience.
"""

import re
from dataclasses import dataclass, field

import httpx
from pydantic import BaseModel, ValidationError

from jobagent.inbox.classify import ChatClient
from jobagent.match.score import MAX_JOB_CHARS, MODEL_TIMEOUT, NUM_CTX

TIER_A_MIN = 70
TIER_B_MIN = 50
WORD_LIMITS = {"A": 250, "B": 150}
LETTER_NUM_PREDICT = 1024  # a 250-word letter is ~350 tokens; more means a loop


def tier_for(
    score: int | None, dealbreakers: list[str], decision: str | None
) -> str | None:
    """A (70+), B (50-69) or None (don't apply). Rejected or blocked jobs never get one."""
    if score is None or decision == "rejected" or dealbreakers:
        return None
    if score >= TIER_A_MIN:
        return "A"
    if score >= TIER_B_MIN:
        return "B"
    return None


LETTER_PROMPT = """\
Write a cover letter from the candidate whose CV is below, for the job below.

Rules:
- Start with "Dear Hiring Manager," then a first sentence that names the role ({title}) \
and the company ({company}).
- {paragraphs} short paragraphs, at most {words} words in total. Plain text: no subject \
line, no addresses, no date. End with "Kind regards," and then {name} on its own line.
- Pick the {picks} most important requirements in the ad. For each, give ONE specific \
piece of work from the CV that matches it, and say where it happened: the employer or \
project the CV puts it under. Never move a fact from one job or project to another.
- Restate only what a CV bullet says, with its real numbers. Never upgrade it: no \
"enterprise-scale", "led", "mentored", "migrated" or any other claim the bullet doesn't \
make. A skill that's only in the CV's skills list is not hands-on experience.
- If the ad asks for something the CV doesn't show, leave it out completely. Never write \
that the candidate has it.
- Let the examples speak: don't say how the experience "aligns with" or "meets" the \
requirements. Vary sentence openings; don't start sentences with "I have".
- UK English, plain and confident. No clichés such as "I am writing to express my \
interest", "I am excited to apply" or "passionate about".
- Never use an em dash or an en dash between clauses. Use commas, colons or full stops.
- Don't mention visas, salary or notice periods.

JOB: {title} at {company}
{ad}

CANDIDATE CV:
{cv}"""


# The model's second job: check its own letter, one sentence at a time, against the CV.
# Code can catch invented names and numbers; only this catches real words in false claims
# ("collaborated with clients at PLATE" when the client work was at another job).
SUPPORT_PROMPT = """\
You check a cover letter against the candidate's CV. For each numbered sentence, answer \
supported = true only if the CV backs every factual claim in it exactly as stated: the \
same skill, the same result, and the same job or project it happened in.
- A claim of experience, scale or seniority the CV doesn't show: false.
- Work attributed to the wrong job or project: false.
- A skill that's only in the CV's skills list, written up as hands-on work: false.
- A sentence that only names the role, expresses interest, or says what the candidate \
would like to do: true.
Answer for every number."""


class SentenceCheck(BaseModel):
    number: int
    supported: bool


class SupportCheck(BaseModel):
    sentences: list[SentenceCheck]


# ---- Checks (code, not the model) --------------------------------------------------------

# Dashes between clauses read as machine-written. Ranges like 2017–2020 are fine.
_DASH = re.compile(r"—|\s–\s|\s--\s")
_PLACEHOLDER = re.compile(r"\[[^\]]*\]|\{[^}]*\}|<[^>]*>")
_NUMBER = re.compile(r"\d[\d,.]*%?")
# Hyphens, slashes and apostrophes split words: "LLM-powered", "CI/CD", "PLATE's".
_NAME = re.compile(r"[A-Za-z][A-Za-z0-9+#.&]*")
# Capitalised words that are fine without appearing in the CV or the ad.
_OK_WORDS = """
    i dear hiring manager team kind regards best thank thanks yours sincerely
    uk england english british london ai ml llm msc cv
    january february march april may june july august september october november december
    monday tuesday wednesday thursday friday
"""
_ALWAYS_OK = frozenset(_OK_WORDS.split())
# Filler that tells instead of shows, and reads as machine-written.
_CLICHES = (
    "aligns with",
    "align with",
    "aligned with",
    "meets the requirements",
    "meet the requirements",
    "i am writing to",
    "excited to apply",
    "great fit",
    "perfect fit",
    "proven track record",
    "passionate about",
)


def _sentence_starts(text: str) -> set[int]:
    """Positions of words that start a sentence or a line (capitalised anyway)."""
    starts = {0}
    for m in re.finditer(r"(?:[.!?:]\s+|\n\s*)", text):
        starts.add(m.end())
    return starts


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", text.lower())


def _has(word: str, text: str) -> bool:
    """Whole-word match, so "CI" doesn't match inside "specific". Plurals allowed."""
    for w in {word, word.removesuffix("s")}:
        if w and re.search(rf"(?<![a-z0-9]){re.escape(w)}(?![a-z0-9])", text):
            return True
    return False


def check_letter(
    letter: str, cv: str, ad: str, title: str, company: str, max_words: int
) -> tuple[list[str], list[str]]:
    """Problems (the letter mustn't go out as is) and warnings (worth a look)."""
    problems: list[str] = []
    warnings: list[str] = []
    if not letter.strip():
        return ["the letter is empty"], []
    if _DASH.search(letter):
        problems.append("uses a dash (— or –) between clauses: reads as AI-written")
    if m := _PLACEHOLDER.search(letter):
        problems.append(f"has a placeholder: {m.group()}")
    low_letter = _norm(letter)
    for phrase in _CLICHES:
        if phrase in low_letter:
            problems.append(f'uses filler: "{phrase}"')
    i_have = len(re.findall(r"(?:^|[.!?]\s+|\n\s*)I have\b", letter))
    if i_have > 1:
        problems.append(f'starts {i_have} sentences with "I have"')
    first_company_word = (re.findall(r"[A-Za-z0-9]+", company) or [""])[0].lower()
    if title.lower() not in low_letter and first_company_word not in low_letter:
        problems.append("doesn't name the role or the company")
    words = len(letter.split())
    if words > max_words * 1.2:
        problems.append(f"too long: {words} words (limit {max_words})")

    cv_n, ad_n, own_n = _norm(cv), _norm(ad), _norm(title + " " + company)
    cv_digits = re.sub(r"[,\s]", "", cv)
    for num in {n.rstrip(".,") for n in _NUMBER.findall(letter)}:
        bare = num.replace(",", "")
        if bare not in cv_digits and num not in f"{ad_n} {own_n}":
            problems.append(f"the number {num} isn't in your CV or the ad")

    starts = _sentence_starts(letter)
    not_found, ad_only = [], []
    for m in _NAME.finditer(letter):
        word = m.group().rstrip(".")
        if not any(c.isupper() for c in word) or m.start() in starts:
            continue  # ordinary word, or capitalised only because it starts a sentence
        low = word.lower()
        if low in _ALWAYS_OK or low.rstrip("s") in _ALWAYS_OK:
            continue
        if _has(low, cv_n) or _has(low, own_n):
            continue  # from your CV, or the role/company the letter is addressed to
        (ad_only if _has(low, ad_n) else not_found).append(word)
    if not_found:
        problems.append(
            "names things that aren't in your CV or the ad: "
            + ", ".join(dict.fromkeys(not_found))
        )
    if ad_only:
        warnings.append(
            "names things from the ad that aren't in your CV (check they aren't "
            "claimed as your experience): " + ", ".join(dict.fromkeys(ad_only))
        )
    return problems, warnings


# ---- Writer ------------------------------------------------------------------------------


@dataclass
class Letter:
    text: str
    problems: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def letter_sentences(letter: str) -> list[str]:
    """The letter's sentences, without the greeting and sign-off lines."""
    lines = [ln.strip() for ln in letter.splitlines() if ln.strip()]
    body = [
        ln
        for ln in lines
        if not re.match(
            r"(dear|kind regards|best regards|regards|yours)\b", ln, re.IGNORECASE
        )
    ]
    if body and len(body[-1].split()) <= 4 and not body[-1].endswith("."):
        body = body[:-1]  # the name under the sign-off
    text = " ".join(body)
    return [t for t in re.split(r"(?<=[.!?])\s+", text) if t.strip()]


def candidate_name(cv: str) -> str:
    """Your name, as the first line of the CV (never hard-coded in the repo)."""
    return next((line.strip() for line in cv.splitlines() if line.strip()), "")


class LetterWriter:
    def __init__(self, client: ChatClient, model: str):
        self.client = client
        self.model = model

    def _chat(self, messages: list[dict], schema: dict | None = None) -> str | None:
        """The reply, or None if the model looped or went silent (same guards as scoring)."""
        try:
            response = self.client.chat(
                model=self.model,
                messages=messages,
                format=schema,
                options={
                    # A little variety for letters; none for the yes/no check.
                    "temperature": 0.3 if schema is None else 0,
                    "num_ctx": NUM_CTX,
                    "num_predict": LETTER_NUM_PREDICT,
                },
                think=False,
            )
        except httpx.TimeoutException:
            return None
        if response.get("done_reason") == "length":
            return None
        return response["message"]["content"].strip()

    def unsupported(self, letter: str, cv: str) -> list[str] | None:
        """Sentences the CV doesn't back, or None if the check couldn't run."""
        sentences = letter_sentences(letter)
        if not sentences:
            return []
        numbered = "\n".join(f"{i}. {t}" for i, t in enumerate(sentences, start=1))
        messages = [
            {"role": "system", "content": SUPPORT_PROMPT},
            {"role": "user", "content": f"CV:\n{cv}\n\nLETTER SENTENCES:\n{numbered}"},
        ]
        reply = self._chat(messages, schema=SupportCheck.model_json_schema())
        if reply is None:
            return None
        try:
            checks = SupportCheck.model_validate_json(reply)
        except ValidationError:
            return None
        bad = {c.number for c in checks.sentences if not c.supported}
        return [t for i, t in enumerate(sentences, start=1) if i in bad]

    def write(self, title: str, company: str, ad: str, cv: str, tier: str) -> Letter:
        """Draft, check (code, then sentence by sentence), redraft once if needed."""
        words = WORD_LIMITS[tier]
        prompt = LETTER_PROMPT.format(
            paragraphs="Three" if tier == "A" else "Two",
            words=words,
            picks="two or three" if tier == "A" else "two",
            name=candidate_name(cv),
            title=title,
            company=company,
            ad=ad[:MAX_JOB_CHARS],
            cv=cv,
        )
        messages = [{"role": "user", "content": prompt}]
        text = ""
        problems: list[str] = []
        warnings: list[str] = []
        for attempt in range(2):
            reply = self._chat(messages)
            if reply is None:
                if attempt == 0:
                    continue
                if not text:
                    stuck = (
                        f"the model looped or gave no answer within {MODEL_TIMEOUT}s"
                    )
                    return Letter("", [stuck])
                break
            text = reply
            problems, warnings = check_letter(text, cv, ad, title, company, words)
            unsupported = self.unsupported(text, cv)
            if unsupported is None:
                warnings.append("the sentence-by-sentence check couldn't run")
            else:
                problems += [
                    f'not backed by your CV as written: "{t}"' for t in unsupported
                ]
            if not problems:
                break
            messages += [
                {"role": "assistant", "content": text},
                {
                    "role": "user",
                    "content": "Rewrite the letter fixing these problems, keeping the "
                    "same rules. Remove or correct any sentence the CV doesn't back."
                    "\n- " + "\n- ".join(problems),
                },
            ]
        return Letter(text, problems, warnings)
