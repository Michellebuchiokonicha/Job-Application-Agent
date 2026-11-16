"""Your screening answers: the only place the applier may copy an answer from.

They live outside the repo (they're personal), in a TOML file you write and approve. The
applier never composes its own answer to a screening question: a question with no answer
here, or one set to "ask_me", comes to you.
"""

import tomllib
from dataclasses import dataclass
from pathlib import Path

DEFAULT_ANSWERS_PATH = Path.home() / ".local" / "share" / "jobagent" / "answers.toml"
ASK_ME = "ask_me"

# Every key the file must have, with the question it answers (in display order).
QUESTIONS = {
    "right_to_work": "Do you have the right to work in the UK?",
    "sponsorship_to_start": "Will you need visa sponsorship to start?",
    "sponsorship_now_or_future": "Will you now or in the future need sponsorship?",
    "sponsorship_details": "Sponsorship / visa details (free text)",
    "notice_period": "Notice period",
    "earliest_start": "Earliest start date",
    "salary": "Salary expectation",
    "location": "Location / willing to relocate",
}


class AnswersError(Exception):
    """The answers file is missing or incomplete. The message says how to fix it."""


@dataclass(frozen=True)
class Answers:
    values: dict[str, str]

    def get(self, key: str) -> str | None:
        """The approved answer, or None if it must come to you (missing or ask_me)."""
        value = self.values.get(key, "").strip()
        return None if not value or value == ASK_ME else value


def load_answers(path: Path | None = None) -> Answers:
    # Default looked up at call time, not definition time, so it can be overridden.
    path = path or DEFAULT_ANSWERS_PATH
    if not path.exists():
        raise AnswersError(
            f"No screening answers yet. Create {path} with these keys: "
            + ", ".join(QUESTIONS)
        )
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as e:
        raise AnswersError(f"{path} isn't valid TOML: {e}") from None
    missing = [k for k in QUESTIONS if k not in data]
    if missing:
        raise AnswersError(f"{path} is missing: {', '.join(missing)}")
    return Answers({k: str(v) for k, v in data.items()})
