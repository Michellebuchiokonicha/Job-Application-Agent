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
