"""Drive `jobagent label` with fake key presses against a synthetic database."""

import json

import pytest
from gmail_fakes import message, part
from typer.testing import CliRunner

from jobagent.cli import app
from jobagent.core.store import connect, init_db, insert_raw_email
from jobagent.eval.labels import build_queue, load_labels
from jobagent.gmail.parse import parse_message

runner = CliRunner()


@pytest.fixture
def env(tmp_path, monkeypatch):
    """A DB with 3 synthetic emails, wired in via DB_PATH. Returns (db, labels)."""
    db = tmp_path / "test.db"
    conn = connect(db)
    init_db(conn)
    for i in range(3):
        raw = message(part("text/plain", f"body {i}"), msg_id=f"m{i}", subject=f"S{i}")
        insert_raw_email(conn, parse_message(raw), json.dumps(raw))
    conn.close()
    monkeypatch.setenv("DB_PATH", str(db))
    return db, tmp_path / "labels.jsonl"


def run(labels, keys):
    return runner.invoke(app, ["label", "--labels", str(labels)], input=keys)


def queue_order(db):
    conn = connect(db)
    order = build_queue(conn, set())
    conn.close()
    return order


def test_labels_in_queue_order_then_quits(env):
    db, labels = env
    first, second, _ = queue_order(db)

    result = run(labels, "ir")  # input runs out -> treated as quit
    assert result.exit_code == 0, result.output

    saved = load_labels(labels)
    assert saved[first]["label"] == "interview_invite"
    assert saved[second]["label"] == "rejection"
    assert len(saved) == 2


def test_resumes_where_it_left_off(env):
    db, labels = env
    order = queue_order(db)
    run(labels, "xq")
    run(labels, "cq")  # second session starts at the next email

    saved = load_labels(labels)
    assert saved[order[0]]["label"] == "other"
    assert saved[order[1]]["label"] == "confirmation"


def test_undo_goes_back_and_relabels(env):
    db, labels = env
    first = queue_order(db)[0]

    run(labels, "xuaq")  # other, undo, then assessment for the same email
    saved = load_labels(labels)
    assert saved[first]["label"] == "assessment"
    assert len(saved) == 1


def test_skip_and_unknown_keys_save_nothing(env):
    _, labels = env
    run(labels, "z?sq")
    assert load_labels(labels) == {}


def test_email_text_is_not_treated_as_markup(env, tmp_path, monkeypatch):
    db = tmp_path / "markup.db"
    conn = connect(db)
    init_db(conn)
    raw = message(part("text/plain", "costs [bold]£5[/bold]"), msg_id="mk")
    insert_raw_email(conn, parse_message(raw), json.dumps(raw))
    conn.close()
    monkeypatch.setenv("DB_PATH", str(db))

    result = run(tmp_path / "l.jsonl", "q")
    assert "[bold]£5[/bold]" in result.output  # shown literally, not styled


def test_undo_reaches_previous_session(env):
    db, labels = env
    first = queue_order(db)[0]
    run(labels, "tq")  # session 1: mislabel as recruiter

    run(labels, "uxq")  # session 2: undo it, relabel as other
    saved = load_labels(labels)
    assert saved[first]["label"] == "other"
    assert len(saved) == 1


def test_feedback_after_each_key(env):
    _, labels = env
    result = run(labels, "xzq")
    assert "Saved previous email as other" in result.output
    assert "Unknown key 'z'" in result.output


def test_undo_with_nothing_to_undo(env):
    _, labels = env
    result = run(labels, "uq")
    assert "Nothing to undo" in result.output
    assert load_labels(labels) == {}


def test_split_option_shows_only_that_split(tmp_path, monkeypatch):
    from jobagent.eval.labels import split_for

    db = tmp_path / "s.db"
    conn = connect(db)
    init_db(conn)
    for i in range(30):
        raw = message(part("text/plain", "b"), msg_id=f"m{i}", subject="S")
        insert_raw_email(conn, parse_message(raw), json.dumps(raw))
    conn.close()
    monkeypatch.setenv("DB_PATH", str(db))
    labels = tmp_path / "l.jsonl"

    runner.invoke(
        app, ["label", "--labels", str(labels), "--split", "test"], input="x" * 30
    )
    saved = load_labels(labels)
    assert saved  # some test-split emails exist among 30
    assert all(split_for(mid) == "test" for mid in saved)
