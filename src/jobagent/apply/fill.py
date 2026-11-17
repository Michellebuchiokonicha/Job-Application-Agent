"""Answering a form's questions from what you've already approved. Never guessing.

Three sources, all yours: your profile (name, email, links), your screening answers
(answers.toml), and later, answers you've given to earlier forms. A question none of them
covers gets None: it comes to you.
"""

from jobagent.apply.answers import Answers
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


def known_answer(q: Question, profile: dict[str, str], answers: Answers) -> str | None:
    """The approved answer to this question, or None if it has to come to you."""
    if q.field in PROFILE_FIELDS:
        return profile.get(PROFILE_FIELDS[q.field]) or None

    label = q.label.lower()
    answer = None
    for words, key in SCREENING:
        if any(w in label for w in words):
            answer = answers.get(key)  # None for a missing or "ask_me" answer
            break
    if answer is None:
        return None  # not a question you've answered: never guessed

    # A choice question takes exactly one of its options. Never the nearest one.
    if q.options and answer not in q.options:
        return None
    return answer
