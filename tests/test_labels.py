import json

import pytest
from gmail_fakes import message, part

from jobagent.core.categories import Category
from jobagent.core.store import connect, init_db, insert_raw_email
from jobagent.eval.labels import (
    append_label,
    build_queue,
    keyword_hint,
    load_labels,
    split_for,
)
from jobagent.gmail.parse import parse_message

# --- split_for ---


def test_split_is_stable():
    assert split_for("abc123") == split_for("abc123")


def test_split_is_roughly_thirty_percent_test():
    splits = [split_for(f"msg-{i}") for i in range(2000)]
    share = splits.count("test") / len(splits)
    assert 0.25 < share < 0.35
    assert set(splits) == {"dev", "test"}


# --- load_labels / append_label ---


def test_missing_file_means_no_labels(tmp_path):
    assert load_labels(tmp_path / "none.jsonl") == {}


def test_append_then_load(tmp_path):
    path = tmp_path / "evals" / "labels.jsonl"  # parent dir created on demand
    append_label(path, "m1", Category.REJECTION)

    labels = load_labels(path)
    assert labels["m1"]["label"] == "rejection"
    assert labels["m1"]["split"] == split_for("m1")


def test_last_line_wins(tmp_path):
    path = tmp_path / "labels.jsonl"
    append_label(path, "m1", Category.CONFIRMATION)
    append_label(path, "m1", Category.REJECTION)

    assert load_labels(path)["m1"]["label"] == "rejection"
    assert len(path.read_text().splitlines()) == 2  # nothing was rewritten


def test_undo_removes_label(tmp_path):
    path = tmp_path / "labels.jsonl"
    append_label(path, "m1", Category.OFFER)
    append_label(path, "m1", None)
    assert "m1" not in load_labels(path)


def test_file_contains_no_email_text(tmp_path):
    path = tmp_path / "labels.jsonl"
    append_label(path, "m1", Category.OTHER)
    record = json.loads(path.read_text())
    assert set(record) == {"gmail_message_id", "label", "split", "labelled_at"}


def test_invalid_label_rejected(tmp_path):
    with pytest.raises(ValueError):
        append_label(tmp_path / "labels.jsonl", "m1", "maybe")


def test_corrupt_line_reports_line_number(tmp_path):
    path = tmp_path / "labels.jsonl"
    append_label(path, "m1", Category.OTHER)
    with path.open("a") as f:
        f.write("{not json\n")
    with pytest.raises(ValueError, match=":2"):
        load_labels(path)


# --- keyword_hint ---


@pytest.mark.parametrize(
    ("subject", "body", "expected"),
    [
        ("Interview invitation", "", Category.INTERVIEW_INVITE),
        ("Next steps", "Please complete this HackerRank test", Category.ASSESSMENT),
        (
            "Your application",
            "Unfortunately we will not be moving forward",
            Category.REJECTION,
        ),
        ("Thank you for applying", "We'll be in touch", Category.CONFIRMATION),
        ("Great news", "We are pleased to offer you the role", Category.OFFER),
        ("Weekly digest", "Top stories this week", None),
    ],
)
def test_keyword_hint(subject, body, expected):
    assert keyword_hint(subject, body) == expected


def test_hint_uses_priority_order():
    # Mentions an interview AND a rejection phrase: interview wins on priority.
    assert (
        keyword_hint("Interview", "unfortunately the slot moved")
        == Category.INTERVIEW_INVITE
    )


# --- build_queue ---


@pytest.fixture
def conn():
    c = connect(":memory:")
    init_db(c)
    yield c
    c.close()


def add_email(conn, msg_id, subject, body="hello"):
    raw = message(part("text/plain", body), msg_id=msg_id, subject=subject)
    insert_raw_email(conn, parse_message(raw), json.dumps(raw))


def test_queue_skips_labelled_and_is_reproducible(conn):
    for i in range(10):
        add_email(conn, f"m{i}", "Interview invitation" if i % 2 else "Newsletter")

    q1 = build_queue(conn, labelled={"m0", "m1"})
    q2 = build_queue(conn, labelled={"m0", "m1"})

    assert q1 == q2  # same seed, same order
    assert set(q1) == {f"m{i}" for i in range(2, 10)}


def test_queue_favours_hinted_emails(conn):
    for i in range(100):
        add_email(conn, f"h{i}", "Interview invitation")
    for i in range(100):
        add_email(conn, f"o{i}", "Newsletter")

    first_50 = build_queue(conn, labelled=set())[:50]
    hinted_share = sum(mid.startswith("h") for mid in first_50) / 50
    assert hinted_share > 0.45  # ~60% expected; loose bound keeps the test stable
