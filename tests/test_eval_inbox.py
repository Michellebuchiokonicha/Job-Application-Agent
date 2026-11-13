import json

import pytest
from gmail_fakes import message, part

from jobagent.core.categories import Category
from jobagent.core.store import connect, init_db, insert_raw_email
from jobagent.eval.inbox import always_other, keyword, run_eval
from jobagent.gmail.parse import parse_message


@pytest.fixture
def conn():
    c = connect(":memory:")
    init_db(c)
    yield c
    c.close()


def add(conn, mid, subject, body):
    raw = message(part("text/plain", body), msg_id=mid, subject=subject)
    insert_raw_email(conn, parse_message(raw), json.dumps(raw))


def rec(label, split="dev"):
    return {"label": label, "split": split}


def test_baselines():
    assert always_other("Interview", "") == Category.OTHER
    assert keyword("Interview invitation", "") == Category.INTERVIEW_INVITE
    assert keyword("Weekly digest", "") == Category.OTHER


def test_run_eval_uses_only_requested_split(conn):
    add(conn, "a", "Interview invitation", "pick a time")
    add(conn, "b", "Newsletter", "news")
    labels = {
        "a": rec("interview_invite"),
        "b": rec("other", split="test"),
    }
    run = run_eval(conn, labels, keyword, "dev")
    assert run.y_true == ["interview_invite"]
    assert run.y_pred == ["interview_invite"]
    assert len(run.latencies_s) == 1


def test_run_eval_counts_missing_emails(conn):
    run = run_eval(conn, {"gone": rec("other")}, keyword, "dev")
    assert run.missing == 1
    assert run.y_true == []


def test_eval_command_prints_scores_not_email_text(tmp_path, monkeypatch):
    from typer.testing import CliRunner

    from jobagent.cli import app
    from jobagent.eval.labels import append_label, split_for

    db = tmp_path / "t.db"
    c = connect(db)
    init_db(c)
    ids = [f"m{i}" for i in range(40)]
    dev = [m for m in ids if split_for(m) == "dev"]
    for m in dev:
        add(c, m, "Interview invitation", "SECRET-BODY pick a time")
    c.close()
    labels = tmp_path / "l.jsonl"
    for m in dev:
        append_label(labels, m, Category.INTERVIEW_INVITE)
    monkeypatch.setenv("DB_PATH", str(db))

    out = CliRunner().invoke(app, ["eval", "inbox", "--labels", str(labels)]).output
    assert "interview+assessment recall" in out
    assert "1.00" in out
    assert "SECRET-BODY" not in out


def test_disagreements():
    from jobagent.eval.inbox import EvalRun, disagreements

    run = EvalRun(
        ids=["a", "b"], y_true=["other", "rejection"], y_pred=["other", "other"]
    )
    assert disagreements(run) == [("b", "rejection", "other")]


@pytest.fixture
def review_env(tmp_path, monkeypatch):
    """Two dev emails: keyword gets one right, one wrong."""
    from jobagent.eval.labels import append_label, split_for

    db = tmp_path / "r.db"
    c = connect(db)
    init_db(c)
    dev = [m for m in (f"m{i}" for i in range(40)) if split_for(m) == "dev"][:2]
    add(c, dev[0], "Interview invitation", "pick a time")  # keyword: interview ✓
    add(c, dev[1], "Hello", "no keywords here")  # keyword: other ✗
    c.close()
    labels = tmp_path / "l.jsonl"
    append_label(labels, dev[0], Category.INTERVIEW_INVITE)
    append_label(labels, dev[1], Category.REJECTION)
    monkeypatch.setenv("DB_PATH", str(db))
    return labels, dev


def review(labels, keys, split="dev"):
    from typer.testing import CliRunner

    from jobagent.cli import app

    args = ["eval", "inbox", "--labels", str(labels), "--split", split, "--show-errors"]
    return CliRunner().invoke(app, args, input=keys)


def test_show_errors_refuses_test_split(review_env):
    labels, _ = review_env
    result = review(labels, "", split="test")
    assert result.exit_code == 2
    assert "test stays unseen" in result.output


def test_show_errors_lists_only_disagreements(review_env):
    labels, _ = review_env
    result = review(labels, " q")  # any key to start, then quit
    assert "1 disagreements" in result.output
    assert "You: rejection" in result.output
    assert "keyword: other" in result.output


def test_show_errors_can_correct_your_label(review_env):
    from jobagent.eval.labels import load_labels

    labels, dev = review_env
    review(labels, " xq")  # start, relabel as other, quit
    assert load_labels(labels)[dev[1]]["label"] == "other"


def test_save_run(tmp_path):
    from jobagent.eval.inbox import save_run

    path = save_run(
        tmp_path / "runs", {"classifier": "keyword", "split": "dev", "n": 3}
    )
    record = json.loads(path.read_text())
    assert record["n"] == 3
    assert "timestamp" in record
    assert path.name.endswith("-keyword-dev.json")
