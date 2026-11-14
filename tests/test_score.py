"""Fit scoring: CV choice, the scorer's guards, and the score/show commands. Made-up data."""

import json
from datetime import date

import pytest
from typer.testing import CliRunner

from jobagent import cli
from jobagent.match import cv as cv_module
from jobagent.match.cv import cv_for
from jobagent.match.score import FitScorer, build_prompt, experience_years, quote_in
from jobagent.sources.base import Job
from jobagent.sources.reed import html_to_text

AD = "We need strong Python and experience with RAG pipelines. Security clearance required."


def assessment(**overrides):
    base = {
        "score": 75,
        "verdict": "worth applying",
        "summary": "Good match on LLM work.",
        "met": [
            {
                "requirement": "Python",
                "job_quote": "strong Python",
                "cv_evidence": "FastAPI services",
            }
        ],
        "missing": [{"requirement": "Kubernetes", "essential": False}],
        "dealbreakers": [],
    }
    return {**base, **overrides}


class FakeChat:
    """Replies with each of `replies` in turn; records every call."""

    def __init__(self, *replies):
        self.replies = list(replies)
        self.calls = []

    def chat(self, **kwargs):
        self.calls.append(kwargs)
        reply = self.replies.pop(0)
        content = reply if isinstance(reply, str) else json.dumps(reply)
        return {"message": {"content": content}}


# --- CV choice ---------------------------------------------------------------


@pytest.mark.parametrize(
    ("title", "key"),
    [
        ("Forward Deployed Engineer", "forward_deployed"),
        ("AI Solutions Architect", "forward_deployed"),
        ("Senior AI Solutions Engineer", "forward_deployed"),
        ("Machine Learning Engineer", "ai_engineer"),
        ("Data Scientist", "ai_engineer"),
    ],
)
def test_cv_for(title, key):
    assert cv_for(title) == key


# --- Scorer ------------------------------------------------------------------


def test_quote_check_ignores_case_and_spacing():
    assert quote_in("STRONG   python", AD)
    assert not quote_in("10 years of Rust", AD)
    assert not quote_in("", AD)


def test_valid_answer_is_returned_with_settings():
    chat = FakeChat(assessment())
    fit = FitScorer(chat, "m").score("ML Engineer", "Acme", AD, "my cv")
    assert fit.assessment.score == 75 and fit.unverified_quotes == 0
    call = chat.calls[0]
    assert call["options"]["temperature"] == 0
    assert call["options"]["num_ctx"] >= 8192  # long CV + ad must fit
    assert "my cv" in call["messages"][1]["content"]


def test_invented_quote_is_counted():
    met = [{"requirement": "Rust", "job_quote": "10 years of Rust", "cv_evidence": "x"}]
    fit = FitScorer(FakeChat(assessment(met=met)), "m").score("t", "c", AD, "cv")
    assert fit.unverified_quotes == 1


def test_dealbreaker_caps_score_in_code():
    reply = assessment(score=90, verdict="strong", dealbreakers=["Security clearance"])
    fit = FitScorer(FakeChat(reply), "m").score("t", "c", AD, "cv")
    assert fit.assessment.score == 30 and fit.assessment.verdict == "poor fit"


def test_retries_then_gives_up_without_inventing_a_score():
    scorer = FitScorer(FakeChat("not json", assessment()), "m", retries=1)
    assert scorer.score("t", "c", AD, "cv").assessment.score == 75
    assert (
        FitScorer(FakeChat("bad", "{}"), "m", retries=1).score("t", "c", AD, "cv")
        is None
    )


def test_html_to_text_keeps_list_items_apart():
    text = html_to_text("<p>About us</p><ul><li>Python</li><li>RAG</li></ul>")
    assert text.splitlines() == ["About us", "Python", "RAG"]


# --- CLI -----------------------------------------------------------------------


@pytest.fixture
def scored_env(tmp_path, monkeypatch):
    monkeypatch.setenv("DB_PATH", str(tmp_path / "test.db"))

    class FakeReed:
        def search(self, term, n):
            return [
                Job(
                    "reed", "1", "ML Engineer", "Acme", "London", "https://x/1", "snip"
                ),
                Job(
                    "reed", "2", "AI Engineer", "Beta", "London", "https://x/2", "snip"
                ),
            ]

        def full_description(self, job_id):
            return AD

    class FakeScorer:
        def score(self, title, company, description, cv):
            from jobagent.match.score import Fit, FitAssessment

            score = 82 if title == "AI Engineer" else 45
            return Fit(FitAssessment(**assessment(score=score)), 0)

    monkeypatch.setattr(cli, "_make_reed", lambda s: FakeReed())
    monkeypatch.setattr(cli, "_make_scorer", lambda s: FakeScorer())
    monkeypatch.setattr(cv_module, "load_cv", lambda key: "cv text")
    CliRunner().invoke(cli.app, ["jobs", "fetch"])


def test_score_then_list_puts_best_fit_first(scored_env):
    result = CliRunner().invoke(cli.app, ["jobs", "score"])
    assert result.exit_code == 0, result.output
    assert "Scored 2 of 2" in result.output

    listing = CliRunner().invoke(cli.app, ["jobs", "list"]).output
    assert listing.index("82") < listing.index("45")

    again = CliRunner().invoke(cli.app, ["jobs", "score"])
    assert "Nothing to score" in again.output  # already scored: not repeated


def test_show_explains_the_score(scored_env):
    CliRunner().invoke(cli.app, ["jobs", "score"])
    out = CliRunner().invoke(cli.app, ["jobs", "show", "2"]).output
    assert "Python" in out and "Kubernetes" in out and "https://x/2" in out


def test_show_unknown_job(scored_env):
    result = CliRunner().invoke(cli.app, ["jobs", "show", "999"])
    assert result.exit_code == 1 and "No job with ID 999" in result.output


def test_experience_years_from_cv_dates():
    cv = "Engineer | X   Mar 2021 - Nov 2021\nAI Engineer | Y   Jan 2024 - Present"
    assert experience_years(cv, date(2026, 3, 1)) == 5.0
    assert experience_years("no dates here", date(2026, 3, 1)) is None


def test_prompt_states_years_computed_in_code():
    prompt = build_prompt("t", "c", AD, "Dev | Z  Jan 2020 – Present", date(2026, 1, 1))
    assert "about 6.0 years" in prompt


def test_requirement_both_met_and_missing_counts_as_met():
    missing = [
        {"requirement": "Strong Python", "essential": True},
        {"requirement": "Kubernetes", "essential": False},
    ]
    fit = FitScorer(FakeChat(assessment(missing=missing)), "m").score(
        "t", "c", AD, "cv"
    )
    assert [m.requirement for m in fit.assessment.missing] == ["Kubernetes"]
