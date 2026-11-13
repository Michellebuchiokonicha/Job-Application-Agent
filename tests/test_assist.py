"""Assisted labelling: background suggestions and the --assist CLI flow, fake model."""

import json
import threading

import pytest
from gmail_fakes import message, part
from typer.testing import CliRunner

from jobagent import cli
from jobagent.core.categories import Category
from jobagent.core.store import connect, init_db, insert_raw_email
from jobagent.eval.assist import Suggester
from jobagent.eval.labels import load_labels, split_for
from jobagent.gmail.parse import parse_message
from jobagent.inbox.classify import Prediction

# --- Suggester ---


def test_prefetch_then_get():
    s = Suggester(lambda subj, body: Prediction(Category.REJECTION, body, True))
    s.prefetch("m1", "S", "no thanks")
    assert s.get("m1").category == Category.REJECTION
    s.close()


def test_prefetch_runs_in_background_and_only_once():
    calls = []
    started = threading.Event()

    def predict(subject, body):
        calls.append(subject)
        started.set()
        return Prediction(Category.OTHER, "x", True)

    s = Suggester(predict)
    s.prefetch("m1", "S", "B")
    s.prefetch("m1", "S", "B")  # duplicate request: ignored
    assert started.wait(timeout=2)  # ran on the worker thread without get()
    s.get("m1")
    assert calls == ["S"]
    s.close()


def test_unknown_id_returns_none():
    s = Suggester(lambda a, b: None)
    assert s.get("never-requested") is None
    s.close()


def test_model_failure_is_recorded_not_raised():
    def broken(subject, body):
        raise ConnectionError("ollama not running")

    s = Suggester(broken)
    s.prefetch("m1", "S", "B")
    assert s.get("m1") is None
    assert "ConnectionError" in s.last_error
    s.close()


# --- CLI ---

runner = CliRunner()


@pytest.fixture
def dev_env(tmp_path, monkeypatch):
    """DB with synthetic emails; a fake model that always suggests 'rejection'."""
    db = tmp_path / "a.db"
    conn = connect(db)
    init_db(conn)
    for i in range(30):
        raw = message(part("text/plain", f"body {i}"), msg_id=f"m{i}", subject="S")
        insert_raw_email(conn, parse_message(raw), json.dumps(raw))
    conn.close()
    monkeypatch.setenv("DB_PATH", str(db))
    monkeypatch.setattr(
        cli,
        "_make_predictor",
        lambda settings: lambda s, b: Prediction(Category.REJECTION, "body", True),
    )
    return tmp_path / "labels.jsonl"


def app_args(labels, *args):
    return ["label", "--labels", str(labels), *args]


def invoke(labels, keys, *args):
    return runner.invoke(cli.app, app_args(labels, *args), input=keys)


def test_assist_requires_dev_split(dev_env):
    result = invoke(dev_env, "q", "--assist", "--split", "test")
    assert result.exit_code == 2
    assert "test stays blind" in result.output
    assert invoke(dev_env, "q", "--assist").exit_code == 2  # no split given


def test_enter_accepts_suggestion_and_records_it(dev_env):
    result = invoke(dev_env, "\r" + "q", "--assist", "--split", "dev")
    assert result.exit_code == 0, result.output
    (record,) = load_labels(dev_env).values()
    assert record["label"] == "rejection"
    assert record["suggested"] == "rejection"
    assert split_for(record["gmail_message_id"]) == "dev"
    assert "accepted with Enter: 1 of 1" in result.output


def test_override_records_both(dev_env):
    invoke(dev_env, "xq", "--assist", "--split", "dev")
    (record,) = load_labels(dev_env).values()
    assert record["label"] == "other"  # your answer wins
    assert record["suggested"] == "rejection"  # what the model said is kept


def test_suggestion_is_shown(dev_env):
    result = invoke(dev_env, "q", "--assist", "--split", "dev")
    assert "Model suggests:" in result.output
    assert "rejection" in result.output


def test_enter_without_assist_is_explained(dev_env):
    result = invoke(dev_env, "\rq", "--split", "dev")
    assert "No suggestion to accept" in result.output
    assert load_labels(dev_env) == {}
