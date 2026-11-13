"""Classify one email into a Category with a local LLM (Ollama).

The prompt mirrors evals/LABELING_GUIDE.md: the model gets the same rules you used
when labelling. Output is forced into a Pydantic schema, and the evidence quote is
checked against the email so the model can't justify itself with invented text.
"""

import re
from dataclasses import dataclass
from typing import Any, Protocol

from pydantic import BaseModel, Field, ValidationError

from jobagent.core.categories import Category

PROMPT_VERSION = "v2"  # bump when the prompt changes, so eval runs are comparable
MAX_BODY_CHARS = 4000  # the start of an email carries its meaning

SYSTEM_PROMPT = """\
You sort a job seeker's emails. Classify the email into exactly one category.

Categories:
- offer: a job offer, verbal or written.
- interview_invite: they invite the candidate to talk: ask for availability, send a \
booking link, propose a time, or confirm/remind/reschedule an interview.
- assessment: they send a test to complete: coding challenge, take-home, online \
test, one-way video interview, aptitude/personality test, or a reminder about one.
- rejection: they are not moving forward with the candidate, at any stage.
- recruiter_message: a real person writes personally about a job, and it is none \
of the above (e.g. agency outreach, a question, a follow-up).
- confirmation: an automated acknowledgement that the candidate's application was \
received, e.g. "we received your application", "thank you for applying", \
"thank you for your interest in the role". Nothing is decided and no action is needed.
- other: everything else: job alerts and "job match" emails (even for one job, even \
if listings mention interviews or assessments), newsletters, marketing, community \
events, account/security emails, personal mail.

Rules:
- If several fit, pick the first in this order: offer, interview_invite, \
assessment, rejection, recruiter_message, confirmation, other.
- "After interviewing many candidates, unfortunately..." is a rejection.
- A rejection that says "we'll keep your CV on file" is still a rejection.
- A confirmation saying a test *may* follow later is a confirmation.
- "Thanks for completing the assessment, we'll review your responses" is a \
confirmation: the test is done and nothing is decided yet.
- A rejection must be about the candidate's own job application. Declining any \
other request (e.g. "we can't share that information") is other.
- In a reply thread ("Re: ..."), classify by what the thread is about now.
- Template emails that could go to 1,000 people unchanged except the name are \
never recruiter_message.

Return the category and evidence_quote: a short phrase (under 20 words) copied \
exactly from the email that best supports the category."""


class EmailLabel(BaseModel):
    category: Category
    evidence_quote: str = Field(max_length=300)


@dataclass
class Prediction:
    category: Category
    evidence_quote: str
    evidence_found: bool  # False = the quote is not in the email (model invented it)


class ChatClient(Protocol):
    """The slice of ollama.Client we use; lets tests pass a fake."""

    def chat(self, *args: Any, **kwargs: Any) -> Any: ...


def _normalise(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip().lower()


def evidence_in_email(quote: str, subject: str, body: str) -> bool:
    """Whitespace- and case-insensitive substring check."""
    q = _normalise(quote)
    return bool(q) and q in _normalise(f"{subject}\n{body}")


def build_user_message(subject: str, body: str) -> str:
    return f"Subject: {subject}\n\n{body[:MAX_BODY_CHARS]}"


class LLMClassifier:
    """Callable as (subject, body) -> Category, so the eval harness can use it."""

    def __init__(self, client: ChatClient, model: str, retries: int = 1):
        self.client = client
        self.model = model
        self.retries = retries
        self.errors = 0  # emails where the model never returned valid output
        self.unsupported = 0  # predictions whose evidence quote wasn't in the email

    def predict(self, subject: str, body: str) -> Prediction | None:
        messages = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": build_user_message(subject, body)},
        ]
        for _ in range(1 + self.retries):
            response = self.client.chat(
                model=self.model,
                messages=messages,
                format=EmailLabel.model_json_schema(),  # constrained decoding
                options={"temperature": 0},
                think=False,  # qwen3's reasoning mode: slower, not needed here
            )
            try:
                label = EmailLabel.model_validate_json(response["message"]["content"])
            except ValidationError:
                continue
            found = evidence_in_email(label.evidence_quote, subject, body)
            return Prediction(label.category, label.evidence_quote, found)
        return None

    def __call__(self, subject: str, body: str) -> Category:
        prediction = self.predict(subject, body)
        if prediction is None:
            self.errors += 1
            # In the eval, a failure counts as "other": it's a miss, never hidden.
            return Category.OTHER
        if not prediction.evidence_found:
            self.unsupported += 1
        return prediction.category
