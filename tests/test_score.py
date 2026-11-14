"""Fit scoring: CV choice, the scorer's guards, and the score/show commands. Made-up data."""

import json
from datetime import date

import pytest
from typer.testing import CliRunner

from jobagent import cli
from jobagent.match import cv as cv_module
from jobagent.match.cv import cv_for
from jobagent.match.score import (
    FitScorer,
    build_prompt,
    cv_supports,
    experience_years,
    is_optional,
    quote_in,
    verdict_for,
)
from jobagent.sources.base import Job
from jobagent.sources.reed import html_to_text

AD = "We need strong Python and experience with RAG pipelines. Kubernetes is a bonus."
CV = "Skills: Python, FastAPI. Architected RAG pipelines using MongoDB Vector Search."


def req(requirement, job_quote, met, cv_quote="", essential=True):
    return {
        "requirement": requirement,
        "job_quote": job_quote,
        "essential": essential,
        "met": met,
        "cv_quote": cv_quote,
    }


def assessment(**overrides):
    base = {
        "summary": "Good match on LLM work.",
        "requirements": [
            req("Python", "strong Python", True, "Python, FastAPI"),
            req(
                "RAG",
                "experience with RAG pipelines",
                True,
                "Architected RAG pipelines",
            ),
            req("Kubernetes", "Kubernetes is a bonus", False, essential=False),
        ],
        "dealbreakers": [],
        "concerns": [],
    }
    return {**base, **overrides}


class FakeChat:
    """Plays the model. First-pass calls get `replies` in turn. Second-pass (verifier)
    calls approve every claim, except the numbers in `reject`."""

    def __init__(self, *replies, reject=()):
        self.replies = list(replies)
        self.reject = set(reject)
        self.calls = []

    def chat(self, **kwargs):
        self.calls.append(kwargs)
        if kwargs["format"].get("title") == "Verification":
            claims = kwargs["messages"][1]["content"].count("Requirement:")
            judgements = [
                {"number": n, "directly_shows": n not in self.reject}
                for n in range(1, claims + 1)
            ]
            return {"message": {"content": json.dumps({"judgements": judgements})}}
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


def score_with(reply, cv=CV, reject=()):
    chat = FakeChat(reply, reject=reject)
    return FitScorer(chat, "m").score("ML Engineer", "Acme", AD, cv)


def test_quote_check_ignores_case_and_spacing():
    assert quote_in("STRONG   python", AD)
    assert not quote_in("10 years of Rust", AD)
    assert not quote_in("", AD)


def test_score_is_computed_in_code():
    fit = score_with(assessment())
    # both essentials met (80) + 0 of 1 nice-to-have (0)
    assert (fit.score, fit.verdict) == (80, "strong")
    assert fit.unverified_job_quotes == fit.unverified_cv_quotes == 0


def test_met_claim_without_a_real_cv_quote_counts_as_missing():
    reqs = [
        req("Python", "strong Python", True, "Python, FastAPI"),
        req("ERP integration", "experience with RAG pipelines", True, "Built Velarius"),
    ]
    fit = score_with(assessment(requirements=reqs))
    erp = fit.requirements[1]
    assert not erp.met and erp.claimed_but_unverified
    assert fit.unverified_cv_quotes == 1
    assert fit.score == 50  # 1 of 2, no nice-to-haves: all 100 points on the one group


def test_invented_ad_quote_is_counted():
    reqs = [req("Rust", "10 years of Rust", False)]
    assert score_with(assessment(requirements=reqs)).unverified_job_quotes == 1


def test_dealbreaker_caps_score():
    fit = score_with(assessment(dealbreakers=["Security clearance required"]))
    assert (fit.score, fit.verdict) == (30, "poor fit")


def test_no_requirements_scores_zero():
    assert score_with(assessment(requirements=[])).score == 0


@pytest.mark.parametrize(
    ("score", "verdict"),
    [
        (95, "strong"),
        (80, "strong"),
        (65, "worth applying"),
        (45, "stretch"),
        (10, "poor fit"),
    ],
)
def test_verdict_bands(score, verdict):
    assert verdict_for(score) == verdict


def test_settings_sent_to_the_model():
    chat = FakeChat(assessment())
    FitScorer(chat, "m").score("t", "c", AD, "my cv")
    call = chat.calls[0]
    assert call["options"]["temperature"] == 0
    assert call["options"]["num_ctx"] >= 8192  # long CV + ad must fit
    assert "my cv" in call["messages"][1]["content"]


def test_retries_then_gives_up_without_inventing_a_score():
    scorer = FitScorer(FakeChat("not json", assessment()), "m", retries=1)
    assert scorer.score("t", "c", AD, CV).score == 80
    assert (
        FitScorer(FakeChat("bad", "{}"), "m", retries=1).score("t", "c", AD, CV) is None
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
            from jobagent.match.score import FitAssessment, check

            reqs = assessment()["requirements"]
            if title != "AI Engineer":  # make the ML job a weaker fit
                reqs = [{**r, "met": False} for r in reqs]
            return check(FitAssessment(**assessment(requirements=reqs)), AD, CV)

    monkeypatch.setattr(cli, "_make_reed", lambda s: FakeReed())
    monkeypatch.setattr(cli, "_make_scorer", lambda s: FakeScorer())
    monkeypatch.setattr(cv_module, "load_cv", lambda key: "cv text")
    CliRunner().invoke(cli.app, ["jobs", "fetch"])


def test_score_then_list_puts_best_fit_first(scored_env):
    result = CliRunner().invoke(cli.app, ["jobs", "score"])
    assert result.exit_code == 0, result.output
    assert "Scored 2 of 2" in result.output

    listing = CliRunner().invoke(cli.app, ["jobs", "list"]).output
    assert listing.index("80") < listing.index(" 0 ")

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


def test_prompt_states_facts_computed_in_code():
    cv = "Dev | Z  Jan 2020 – Present. Available to start in January 2027."
    prompt = build_prompt("t", "c", AD, cv, date(2026, 1, 1))
    assert "about 6.0 years" in prompt
    assert "available to start from January 2027" in prompt


def test_cv_supports_exact_and_light_paraphrase_only():
    assert cv_supports("Architected RAG pipelines", CV)
    assert cv_supports("RAG pipelines using MongoDB", CV)
    assert cv_supports("architected rag pipelines with mongodb vector search", CV)
    assert not cv_supports("Integrated ERP and CRM systems", CV)
    assert not cv_supports("", CV)


def test_optional_comes_from_the_ads_wording():
    ad = "Essential:\nStrong Python\nScala and Spark\nNice to have: Kubernetes, AWS"
    assert is_optional("Strong Python", ad) is False
    assert is_optional("Scala and Spark", ad) is False
    assert is_optional("Kubernetes", ad) is True
    assert is_optional("not in the ad", ad) is None


def test_ad_wording_overrides_the_models_essential_flag():
    reqs = [
        req("Python", "strong Python", True, "Python, FastAPI", essential=False),
        req("Kubernetes", "Kubernetes is a bonus", False, essential=True),
    ]
    fit = score_with(assessment(requirements=reqs))
    assert [r.essential for r in fit.requirements] == [True, False]


def test_verifier_rejects_a_real_quote_that_proves_nothing():
    fit = score_with(assessment(), reject={2})  # 2nd met claim: RAG
    rag = fit.requirements[1]
    assert not rag.met and rag.not_proven and fit.not_proven == 1
    assert fit.score == 40  # 1 of 2 essentials (40) + 0 of 1 nice-to-have


def test_summary_agrees_with_the_final_score():
    # job 16 said "meets all essential requirements" next to six missing ones
    fit = score_with(assessment(), reject={2})
    assert fit.summary == (
        "Meets 1 of 2 essentials and 0 of 1 nice-to-haves. Main gaps: RAG."
    )


def test_verifier_failure_is_reported_not_hidden():
    class BrokenVerifier(FakeChat):
        def chat(self, **kwargs):
            if kwargs["format"].get("title") == "Verification":
                self.calls.append(kwargs)
                return {"message": {"content": "nonsense"}}
            return super().chat(**kwargs)

    fit = FitScorer(BrokenVerifier(assessment()), "m").score("t", "c", AD, CV)
    assert fit.verifier_failed and fit.score == 80  # claims kept, but flagged


def test_no_verifier_call_when_nothing_is_claimed():
    reqs = [req("Kubernetes", "Kubernetes is a bonus", False, essential=False)]
    chat = FakeChat(assessment(requirements=reqs))
    FitScorer(chat, "m").score("t", "c", AD, CV)
    assert len(chat.calls) == 1
