"""Hand labels for the inbox eval.

The label file holds IDs and labels only, never email text: the text stays in the
local database, so this file reveals nothing on its own.
"""

import hashlib
import json
import random
import re
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from jobagent.core.categories import Category

LABELS_PATH = Path("evals/inbox_labels.jsonl")
TEST_FRACTION_TENTHS = 3  # 3 in 10 emails go to the test split
HINTED_SHARE = 0.6  # about 60% of the queue comes from keyword-hinted emails


def split_for(gmail_message_id: str) -> str:
    """'dev' or 'test', decided by the ID alone, so it never changes between runs."""
    digest = hashlib.sha256(gmail_message_id.encode()).hexdigest()
    return "test" if int(digest, 16) % 10 < TEST_FRACTION_TENTHS else "dev"


def load_labels(path: Path = LABELS_PATH) -> dict[str, dict]:
    """Current label per email. Later lines win; a null label means 'undone'."""
    labels: dict[str, dict] = {}
    if not path.exists():
        return labels
    with path.open(encoding="utf-8") as f:
        for line_no, line in enumerate(f, start=1):
            if not line.strip():
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError as e:
                raise ValueError(f"{path}:{line_no} is not valid JSON") from e
            labels[record["gmail_message_id"]] = record
    return {mid: r for mid, r in labels.items() if r["label"] is not None}


def append_label(
    path: Path,
    gmail_message_id: str,
    label: Category | None,
    suggested: Category | None = None,
) -> None:
    """Record a label (or None to undo one). Appends only; never rewrites the file.

    `suggested` = what the model proposed, if you labelled with assistance. Kept so
    we can measure how often suggestions were accepted (and spot anchoring).
    """
    record = {
        "gmail_message_id": gmail_message_id,
        "label": Category(label).value if label is not None else None,
        "split": split_for(gmail_message_id),
        "labelled_at": datetime.now(UTC).isoformat(timespec="seconds"),
    }
    if suggested is not None:
        record["suggested"] = Category(suggested).value
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record) + "\n")


# Rough patterns, checked in priority order. Only used to ORDER the labelling
# queue so rare categories show up often enough; never used as a label.
_HINTS: list[tuple[Category, re.Pattern[str]]] = [
    (
        Category.OFFER,
        re.compile(r"pleased to offer|offer of employment|job offer|offer letter"),
    ),
    (
        Category.INTERVIEW_INVITE,
        re.compile(
            r"\binterview|phone screen|schedule a (call|chat)|calendly|your availability"
        ),
    ),
    (
        Category.ASSESSMENT,
        re.compile(
            r"assessment|coding (challenge|test)|take[- ]home|hackerrank|codility"
            r"|codesignal|online test"
        ),
    ),
    (
        Category.REJECTION,
        re.compile(
            r"unfortunately|not (to )?(be )?(moving|progress)|unsuccessful"
            r"|not been selected|other candidates"
        ),
    ),
    (
        Category.CONFIRMATION,
        re.compile(
            r"thank(s| you) for (applying|your application)"
            r"|received your application|application (has been )?received"
        ),
    ),
]


def keyword_hint(subject: str, body: str) -> Category | None:
    text = f"{subject}\n{body}".lower()
    for category, pattern in _HINTS:
        if pattern.search(text):
            return category
    return None


# Confirmations are common, so they don't count as "interesting" for sampling.
_RARE = {
    Category.OFFER,
    Category.INTERVIEW_INVITE,
    Category.ASSESSMENT,
    Category.REJECTION,
}


def build_queue(
    conn: sqlite3.Connection, labelled: set[str], seed: int = 42
) -> list[str]:
    """IDs to label next: ~60% likely-rare emails, ~40% everything else, mixed.

    Seeded, so the order is the same on every run (given the same labelled set).
    """
    hinted: list[str] = []
    rest: list[str] = []
    rows = conn.execute(
        "SELECT gmail_message_id, subject, body_text FROM raw_emails "
        "ORDER BY gmail_message_id"  # fixed input order, so the shuffle is reproducible
    )
    for row in rows:
        mid = row["gmail_message_id"]
        if mid in labelled:
            continue
        hint = keyword_hint(row["subject"], row["body_text"])
        (hinted if hint in _RARE else rest).append(mid)

    rng = random.Random(seed)
    rng.shuffle(hinted)
    rng.shuffle(rest)

    queue: list[str] = []
    while hinted or rest:
        take_hinted = hinted and (not rest or rng.random() < HINTED_SHARE)
        queue.append((hinted if take_hinted else rest).pop())
    return queue
