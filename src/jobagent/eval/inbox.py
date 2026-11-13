"""Run an inbox classifier over the hand-labelled emails and collect predictions.

Scoring lives in metrics.py; this module only produces (y_true, y_pred) pairs.
"""

import json
import sqlite3
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from jobagent.core.categories import Category
from jobagent.core.store import get_raw_email
from jobagent.core.text import clean_for_reading
from jobagent.eval.labels import keyword_hint

# A classifier takes (subject, body) and returns a category.
Classifier = Callable[[str, str], Category]


def always_other(subject: str, body: str) -> Category:
    """Baseline: the majority class. High accuracy, zero recall on what matters."""
    return Category.OTHER


def keyword(subject: str, body: str) -> Category:
    """Baseline: the regex hints. The local model must beat this to be worth it."""
    return keyword_hint(subject, body) or Category.OTHER


CLASSIFIERS: dict[str, Classifier] = {
    "always-other": always_other,
    "keyword": keyword,
}


@dataclass
class EvalRun:
    ids: list[str] = field(default_factory=list)  # gmail_message_id per row
    y_true: list[str] = field(default_factory=list)
    y_pred: list[str] = field(default_factory=list)
    latencies_s: list[float] = field(default_factory=list)
    missing: int = 0  # labelled emails no longer in the database


def run_eval(
    conn: sqlite3.Connection,
    labels: dict[str, dict],
    classify: Classifier,
    split: str,
    on_progress: Callable[[int, int], None] | None = None,
) -> EvalRun:
    run = EvalRun()
    # sorted: stable order between runs
    todo = sorted((m, r) for m, r in labels.items() if r["split"] == split)
    for done, (mid, record) in enumerate(todo, start=1):
        if on_progress:
            on_progress(done - 1, len(todo))
        try:
            row = get_raw_email(conn, mid)
        except KeyError:
            run.missing += 1
            continue
        subject = clean_for_reading(row["subject"])
        body = clean_for_reading(row["body_text"])

        start = time.perf_counter()
        predicted = classify(subject, body)
        run.latencies_s.append(time.perf_counter() - start)

        run.ids.append(mid)
        run.y_true.append(record["label"])
        run.y_pred.append(Category(predicted).value)
    if on_progress:
        on_progress(len(todo), len(todo))
    return run


def disagreements(run: EvalRun) -> list[tuple[str, str, str]]:
    """(gmail_message_id, your label, predicted) for every row they differ."""
    return [
        (mid, t, p)
        for mid, t, p in zip(run.ids, run.y_true, run.y_pred, strict=True)
        if t != p
    ]


def save_run(runs_dir: Path, result: dict) -> Path:
    """Write one eval run to its own timestamped JSON file. Returns the path.

    One file per run (not appending to one file): runs are never overwritten, and
    each file is a self-contained record of what was measured, with what, and when.
    """
    now = datetime.now(UTC)
    record = {"timestamp": now.isoformat(timespec="seconds"), **result}
    runs_dir.mkdir(parents=True, exist_ok=True)
    name = f"{now:%Y%m%dT%H%M%S}-{result['classifier']}-{result['split']}.json"
    path = runs_dir / name
    path.write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
    return path
