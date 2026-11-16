"""Fit scoring: CV choice, the scorer's guards, and the score/show commands. Made-up data."""

import json
import re
from dataclasses import asdict
from datetime import date

import httpx
import pytest
from typer.testing import CliRunner

from jobagent import cli
from jobagent.match import cv as cv_module
from jobagent.match.cv import cv_for
from jobagent.match.score import (
    NUM_PREDICT,
    FitScorer,
    build_prompt,
    cv_supports,
    evidence_section,
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
    """Plays the model. First-pass calls get `replies` in turn. The first verifier call
    approves every claim except the numbers in `reject`. A second look offers the lines in
    `evidence` ({requirement: CV line}); later verifier calls approve unless the CV line
    contains a phrase in `reject_lines`."""

    def __init__(self, *replies, reject=(), evidence=None, reject_lines=()):
        self.replies = list(replies)
        self.reject = set(reject)
        self.evidence = evidence or {}
        self.reject_lines = reject_lines
        self.calls = []
        self.verifications = 0

    def chat(self, **kwargs):
        self.calls.append(kwargs)
        title = kwargs["format"].get("title")
        content = kwargs["messages"][1]["content"]
        if title == "Verification":
            self.verifications += 1
            lines = re.findall(r"CV line: (.*)", content)
            judgements = [
                {
                    "number": n,
                    "directly_shows": (
                        n not in self.reject
                        if self.verifications == 1
                        else not any(p in line for p in self.reject_lines)
                    ),
                }
                for n, line in enumerate(lines, start=1)
            ]
            return {"message": {"content": json.dumps({"judgements": judgements})}}
        if title == "EvidenceSearch":
            asked = re.findall(r"^(\d+)\. Requirement: (.*)$", content, re.MULTILINE)
            items = [
                {"number": int(n), "cv_quote": self.evidence.get(name, "")}
                for n, name in asked
            ]
            return {"message": {"content": json.dumps({"items": items})}}
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


def test_invented_ad_quote_is_counted_and_dropped():
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


def test_failure_reason_for_invalid_answers():
    scorer = FitScorer(FakeChat("bad", "{}"), "m", retries=1)
    assert scorer.score("t", "c", AD, CV) is None
    assert scorer.last_failure == "invalid answer 2 times"


def test_every_call_caps_the_answer_length():
    chat = FakeChat(assessment())
    FitScorer(chat, "m").score("t", "c", AD, CV)
    assert all(c["options"]["num_predict"] == NUM_PREDICT for c in chat.calls)
    assert len(chat.calls) == 2  # first pass + verifier


class LoopingChat:
    """A model stuck in a loop: Ollama stops it at the output cap."""

    def __init__(self):
        self.calls = 0

    def chat(self, **kwargs):
        self.calls += 1
        return {"message": {"content": '{"requirements": [  '}, "done_reason": "length"}


def test_a_looping_model_is_skipped_not_retried():
    chat = LoopingChat()
    scorer = FitScorer(chat, "m", retries=1)
    assert scorer.score("t", "c", AD, CV) is None
    assert "cut off" in scorer.last_failure
    assert chat.calls == 1  # temperature 0 would loop the same way again


def test_a_silent_model_times_out():
    class SilentChat:
        def chat(self, **kwargs):
            raise httpx.ReadTimeout("timed out")

    scorer = FitScorer(SilentChat(), "m")
    assert scorer.score("t", "c", AD, CV) is None
    assert "no answer within" in scorer.last_failure


def test_a_looping_verifier_flags_the_job_instead_of_hanging():
    class LoopingVerifier(FakeChat):
        def chat(self, **kwargs):
            if kwargs["format"].get("title") == "Verification":
                return {"message": {"content": ""}, "done_reason": "length"}
            return super().chat(**kwargs)

    fit = FitScorer(LoopingVerifier(assessment()), "m").score("t", "c", AD, CV)
    assert fit.verifier_failed and fit.score == 80


def test_score_names_the_jobs_it_could_not_score(scored_env, monkeypatch):
    class StuckScorer:
        last_failure = "answer cut off at 4096 tokens (model looping)"

        def score(self, *args):
            return None

    monkeypatch.setattr(cli, "_make_scorer", lambda s: StuckScorer())
    out = CliRunner().invoke(cli.app, ["jobs", "score"]).output
    assert "Scored 0 of 2" in out and "Not scored this run (2)" in out
    assert "ML Engineer: answer cut off" in out


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


# --- Second look: rejected claims get one more search, in experience and projects ----

SECTIONED_CV = """\
PROFILE
AI engineer who ships LLM products.
TECHNICAL SKILLS
Python, FastAPI, Docker
PROFESSIONAL EXPERIENCE
Served models through async Python FastAPI services in Docker.
Architected RAG pipelines using MongoDB Vector Search.
"""
SKILLS_LINE = "Python, FastAPI, Docker"
BULLET = "Served models through async Python FastAPI services in Docker."


def sectioned_assessment():
    # The first pass cites the skills list for Python: real, but it proves nothing.
    return assessment(
        requirements=[
            req("Python", "strong Python", True, SKILLS_LINE),
            req(
                "RAG",
                "experience with RAG pipelines",
                True,
                "Architected RAG pipelines",
            ),
            req("Kubernetes", "Kubernetes is a bonus", False, essential=False),
        ]
    )


def second_look(**fake):
    chat = FakeChat(sectioned_assessment(), reject={1}, **fake)
    fit = FitScorer(chat, "m").score("ML Engineer", "Acme", AD, SECTIONED_CV)
    return fit, chat


def test_evidence_section_leaves_out_profile_and_skills():
    section = evidence_section(SECTIONED_CV)
    assert section.startswith("PROFESSIONAL EXPERIENCE")
    assert SKILLS_LINE not in section and BULLET in section


def test_evidence_section_needs_a_real_heading():
    cv = "Profile: engineer with five years of\nexperience\nSkills: Python"
    assert evidence_section(cv) == cv  # a wrapped sentence isn't a heading: whole CV
    assert evidence_section("Skills\nWork Experience\nBuilt X").startswith("Work")


def test_second_look_finds_the_bullet_that_proves_it():
    fit, _ = second_look(evidence={"Python": BULLET})
    python = fit.requirements[0]
    assert python.met and python.second_look and not python.not_proven
    assert python.cv_quote == BULLET
    assert fit.not_proven == 0 and fit.score == 80


def test_second_look_cannot_use_the_skills_list():
    # Offering the skills line again: code refuses it (not in experience), no new judging.
    fit, chat = second_look(evidence={"Python": SKILLS_LINE})
    assert not fit.requirements[0].met and fit.not_proven == 1
    assert chat.verifications == 1


def test_second_look_cannot_invent_a_line():
    fit, _ = second_look(evidence={"Python": "Led Python platform team at Google"})
    assert not fit.requirements[0].met and fit.not_proven == 1


def test_second_look_line_is_judged_like_any_other():
    fit, chat = second_look(evidence={"Python": BULLET}, reject_lines=("Docker",))
    assert not fit.requirements[0].met and fit.requirements[0].not_proven
    assert chat.verifications == 2


def test_no_second_look_when_nothing_was_rejected():
    chat = FakeChat(sectioned_assessment())
    FitScorer(chat, "m").score("t", "c", AD, SECTIONED_CV)
    titles = [c["format"].get("title") for c in chat.calls]
    assert "EvidenceSearch" not in titles


def test_a_failed_second_look_leaves_the_rejection_standing():
    class LoopingSecondLook(FakeChat):
        def chat(self, **kwargs):
            if kwargs["format"].get("title") == "EvidenceSearch":
                return {"message": {"content": ""}, "done_reason": "length"}
            return super().chat(**kwargs)

    chat = LoopingSecondLook(sectioned_assessment(), reject={1})
    fit = FitScorer(chat, "m").score("t", "c", AD, SECTIONED_CV)
    assert not fit.requirements[0].met and fit.not_proven == 1
    assert not fit.verifier_failed  # the main check worked; only the bonus step didn't


def test_summary_agrees_with_the_final_score():
    # job 16 said "meets all essential requirements" next to six missing ones
    fit = score_with(assessment(), reject={2})
    assert fit.summary == (
        "Meets 1 of 2 essentials and 0 of 1 nice-to-haves. Main gaps: RAG."
    )


def test_verifier_sees_the_ads_wording():
    chat = FakeChat(assessment())
    FitScorer(chat, "m").score("t", "c", AD, CV)
    assert (
        "Ad says: experience with RAG pipelines"
        in chat.calls[1]["messages"][1]["content"]
    )


def test_show_says_why_a_claim_was_rejected(tmp_path, monkeypatch):
    monkeypatch.setenv("DB_PATH", str(tmp_path / "t.db"))
    from jobagent.core.store import connect, init_db, save_score, upsert_job

    conn = connect(tmp_path / "t.db")
    init_db(conn)
    upsert_job(conn, Job("reed", "1", "ML Engineer", "Acme", "London", "u", "d"), 0)
    fit = score_with(assessment(), reject={2})
    save_score(
        conn,
        1,
        score=fit.score,
        verdict=fit.verdict,
        cv="ai_engineer",
        assessment_json=json.dumps(asdict(fit)),
        unverified=0,
        model="m",
        prompt_version="v6",
    )
    conn.commit()
    out = CliRunner().invoke(cli.app, ["jobs", "show", "1"]).output
    assert "doesn't show this" in out and "Architected RAG pipelines" in out


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


def test_requirements_not_in_the_ad_are_dropped():
    # the model once listed CV skills as "requirements", all met
    reqs = [
        req("Python", "strong Python", True, "Python, FastAPI"),
        req("MongoDB", "MongoDB Vector Search", True, "MongoDB Vector Search"),
        req("Python", "strong Python", True, "Python, FastAPI"),  # listed twice
    ]
    fit = score_with(assessment(requirements=reqs))
    assert [r.requirement for r in fit.requirements] == ["Python"]
    assert fit.unverified_job_quotes == 1


def test_each_ad_line_weighs_the_same():
    ad = "Build RAG systems\nIntegrate with ERP, CRM, finance and document platforms"
    reqs = [
        req("RAG", "Build RAG systems", True, "Architected RAG pipelines"),
        req("ERP", "ERP", False),
        req("CRM", "CRM", False),
        req("Finance", "finance", False),
        req("Documents", "document platforms", False),
    ]
    fit = FitScorer(FakeChat(assessment(requirements=reqs)), "m").score(
        "t", "c", ad, CV
    )
    # per skill this would be 1 of 5 = 20; per ad line it's 1 of 2 lines = 50
    assert fit.score == 50


def test_partly_met_line_gets_partial_credit():
    ad = "Experience with RAG pipelines and strong Python"
    reqs = [
        req("RAG", "RAG pipelines", True, "Architected RAG pipelines"),
        req("Python", "strong Python", True, "Python, FastAPI"),
        req("Kotlin", "RAG pipelines and", False),  # same line, not met
    ]
    fit = FitScorer(FakeChat(assessment(requirements=reqs)), "m").score(
        "t", "c", ad, CV
    )
    assert fit.score == 67  # one line, 2 of 3 met


def test_missing_a_skill_named_in_the_title_caps_the_score():
    reqs = [
        req("Python", "strong Python", True, "Python, FastAPI"),
        req("RAG", "experience with RAG pipelines", True, "Architected RAG pipelines"),
        req("R/Shiny", "Kubernetes is a bonus", False),  # quote just needs to be in AD
    ]
    chat = FakeChat(assessment(requirements=reqs))
    fit = FitScorer(chat, "m").score("AI Engineer (Python, R, Shiny)", "c", AD, CV)
    assert fit.title_gaps == ["R/Shiny"]
    assert fit.score == 55 and fit.verdict == "stretch"
    assert "title names R/Shiny" in fit.summary


def test_job_based_in_the_us_is_a_dealbreaker():
    ad = AD + "\nUSA based (Fully Remote). $180,000 to $200,000 base."
    fit = FitScorer(FakeChat(assessment()), "m").score("ML Engineer", "c", ad, CV)
    assert fit.score <= 30 and any("outside the UK" in d for d in fit.dealbreakers)


def test_recount_applies_new_rules_without_the_model(scored_env):
    CliRunner().invoke(cli.app, ["jobs", "score"])
    out = CliRunner().invoke(cli.app, ["jobs", "recount"]).output
    assert "Recounted with current rules: 0 score(s) changed." in out


def test_recount_keeps_judgements_and_applies_new_rules():
    from jobagent.match.score import recount

    saved = asdict(score_with(assessment()))
    assert saved["score"] == 80
    fit = recount(saved, AD + "\nUSA based (Fully Remote)", "ML Engineer")
    assert fit.score == 30 and [r.met for r in fit.requirements] == [True, True, False]
    assert recount(asdict(fit), AD, "ML Engineer").score == 80  # rule no longer applies


def test_role_words_in_the_title_are_not_a_skill_gap():
    from jobagent.match.score import CheckedRequirement, title_gaps

    def missing(name):
        return CheckedRequirement(name, True, False, "", False)

    reqs = [missing("Machine Learning"), missing("TypeScript")]
    title = "Senior Machine Learning Engineer (TypeScript)"
    assert title_gaps(reqs, title) == ["TypeScript"]


# --- Review ------------------------------------------------------------------------


def test_review_saves_decisions_and_hides_rejected(scored_env):
    CliRunner().invoke(cli.app, ["jobs", "score"])
    # best first: AI Engineer (80) then ML Engineer (0). Accept the first, reject the second
    result = CliRunner().invoke(
        cli.app, ["jobs", "review"], input="a\ngreat fit\nr\nnot my field\n"
    )
    assert result.exit_code == 0, result.output
    assert "Accepted 1 · rejected 1 · skipped 0." in result.output

    listing = CliRunner().invoke(cli.app, ["jobs", "list"]).output
    assert "2 ✓" in listing and "ML" not in listing  # rejected job hidden
    assert "Nothing to review" in CliRunner().invoke(cli.app, ["jobs", "review"]).output

    shortlist = CliRunner().invoke(cli.app, ["jobs", "list", "--accepted"]).output
    assert "Beta" in shortlist and "Acme" not in shortlist


def test_review_skip_and_quit_save_nothing(scored_env):
    CliRunner().invoke(cli.app, ["jobs", "score"])
    out = CliRunner().invoke(cli.app, ["jobs", "review"], input="s\nq\n").output
    assert "Accepted 0 · rejected 0 · skipped 1." in out
    again = CliRunner().invoke(cli.app, ["jobs", "review"]).output  # EOF ends it
    assert "2 job(s) to review" in again


def test_decisions_survive_clearing_jobs(scored_env):
    CliRunner().invoke(cli.app, ["jobs", "score"])
    CliRunner().invoke(cli.app, ["jobs", "review"], input="r\n\nq\n")
    CliRunner().invoke(cli.app, ["jobs", "clear", "--yes"])
    CliRunner().invoke(cli.app, ["jobs", "fetch"])  # same jobs come back
    CliRunner().invoke(cli.app, ["jobs", "score"])
    out = CliRunner().invoke(cli.app, ["jobs", "review"], input="q\n").output
    assert "1 job(s) to review" in out  # the rejected one stays decided
