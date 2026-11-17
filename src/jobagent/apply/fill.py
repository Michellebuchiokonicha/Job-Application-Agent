"""Answering a form's questions from what you've already approved. Never guessing.

Three sources, all yours: your profile (name, email, links), your screening answers
(answers.toml), and later, answers you've given to earlier forms. A question none of them
covers gets None: it comes to you.
"""

import re
from collections.abc import Mapping
from dataclasses import dataclass, field

from jobagent.apply.answers import ASK_ME, Answers
from jobagent.apply.forms import Question

# Lever's standard fields, by their name in the form -> the key in your profile.
PROFILE_FIELDS = {
    "name": "name",
    "email": "email",
    "phone": "phone",
    "location": "location",
    "org": "current_company",
    "urls[LinkedIn]": "linkedin",
    "urls[GitHub]": "github",
    "urls[Portfolio]": "portfolio",
}

# Screening questions, by words in the label -> the key in answers.toml.
# Order matters: the first match wins, so specific wording comes before general wording
# ("now or in the future ... sponsorship" also contains "sponsorship").
SCREENING = [
    (("now or in the future", "in the future"), "sponsorship_now_or_future"),
    (("sponsorship", "sponsor"), "sponsorship_to_start"),
    (("right to work", "legally authorized", "legally authorised"), "right_to_work"),
    (("notice period",), "notice_period"),
    (("salary",), "salary"),
    (("start date", "earliest start", "when can you start"), "earliest_start"),
]


def question_key(label: str) -> str:
    """A question's wording reduced to lowercase words, so the same question matches
    across forms despite case, punctuation or spacing: "How did you hear about us?" and
    "how did you hear about us" are one question. Different wording is a different one."""
    return " ".join(re.findall(r"[a-z0-9]+", label.lower()))


MULTI_SEPARATOR = "; "  # a multiple-select answer is stored as "English; French"


def fits(q: Question, answer: str) -> bool:
    """Whether an answer can go in this field: any text for a text box; for a choice,
    exactly one of its options (each of them, for a multiple-select). Never the nearest."""
    if not q.options:
        return True
    parts = answer.split(MULTI_SEPARATOR) if q.kind == "multiple-select" else [answer]
    return all(part in q.options for part in parts)


def known_answer(
    q: Question,
    profile: dict[str, str],
    answers: Answers,
    saved: Mapping[str, str] | None = None,
) -> str | None:
    """The approved answer to this question, or None if it has to come to you.

    Sources, in order: your profile, your screening answers, then answers you gave to
    the same question on an earlier form (`saved`, by question_key).
    """
    if q.field in PROFILE_FIELDS:
        return profile.get(PROFILE_FIELDS[q.field]) or None

    label = q.label.lower()
    candidates = []
    for words, key in SCREENING:
        if any(w in label for w in words):
            if answers.values.get(key, "").strip() == ASK_ME:
                return None  # "ask me" means every time, never a saved answer
            candidates.append(answers.get(key))
            break
    # An earlier answer to this exact question: used when there's no screening answer,
    # or when the screening answer doesn't fit this form's options (a sentence about
    # your visa can't go in a Yes/No box, but your Yes/No from last time can).
    candidates.append((saved or {}).get(question_key(q.label)))
    return next((a for a in candidates if a is not None and fits(q, a)), None)


@dataclass
class FormPlan:
    filled: dict[str, str] = field(default_factory=dict)  # form field -> answer
    needs_you: list[Question] = field(default_factory=list)  # required, no answer
    left_empty: list[Question] = field(default_factory=list)  # optional, no answer


def plan_form(
    questions: list[Question],
    profile: dict[str, str],
    answers: Answers,
    saved: Mapping[str, str],
    letter: str,
) -> FormPlan:
    """Sort a form into what's filled, what needs you, and optional fields left empty.
    The CV upload is handled by the browser step; the letter goes in the comments box."""
    plan = FormPlan()
    for q in questions:
        if q.kind == "file":
            continue
        answer = (
            letter
            if q.field == "comments"
            else known_answer(q, profile, answers, saved)
        )
        if answer:
            plan.filled[q.field] = answer
        elif q.required:
            plan.needs_you.append(q)
        else:
            plan.left_empty.append(q)
    return plan
