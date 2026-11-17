"""Phase 5: screening answers, cover-letter checks, packs and the review flow. Made-up data."""

import json
import re

import httpx
import pytest
from typer.testing import CliRunner

from jobagent import cli
from jobagent.apply import answers as answers_module
from jobagent.apply.answers import ASK_ME, QUESTIONS, AnswersError, load_answers
from jobagent.apply.letter import (
    Letter,
    LetterWriter,
    check_letter,
    drop_heading,
    letter_sentences,
    tier_for,
)
from jobagent.core.store import (
    connect,
    get_application,
    init_db,
    jobs_to_prepare,
    pack_counts,
    save_decision,
    save_score,
    upsert_job,
)
from jobagent.match import cv as cv_module
from jobagent.sources.base import Job

CV = """Ada Example
AI Engineer
PROFESSIONAL EXPERIENCE
Built RAG pipelines on MongoDB Vector Search, improving retrieval precision by 22%.
Served models through async Python FastAPI services in Docker for Acme Foods.
"""
AD = "We need Python, RAG and Kubernetes. Join our team of 12 at Beta Labs."


OPENING = "I'm applying for the AI Engineer role at Beta Labs."


def letter(body: str) -> str:
    return f"Dear Hiring Manager,\n\n{OPENING} {body}\n\nKind regards,\nAda Example"


def check(text: str, max_words: int = 250):
    return check_letter(text, CV, AD, "AI Engineer", "Beta Labs", max_words)


# --- Tiers ------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("score", "dealbreakers", "decision", "tier"),
    [
        (85, [], None, "A"),
        (70, [], None, "A"),
        (69, [], None, "B"),
        (50, [], None, "B"),
        (49, [], None, "B"),
        (5, [], None, "B"),
        (90, ["Based outside the UK"], None, None),
        (90, [], "rejected", None),
        (60, [], "accepted", "B"),
        (None, [], None, None),
    ],
)
def test_tier_for(score, dealbreakers, decision, tier):
    assert tier_for(score, dealbreakers, decision) == tier


# --- Letter checks ------------------------------------------------------------------------


def test_a_letter_built_from_the_cv_passes():
    text = letter(
        "I built RAG pipelines on MongoDB Vector Search, improving retrieval precision "
        "by 22%. At Acme Foods I served models through Python FastAPI services."
    )
    assert check(text) == ([], [])


def test_em_dash_is_a_problem_but_a_range_is_fine():
    problems, _ = check(letter("I build RAG systems — fast ones."))
    assert any("dash" in p for p in problems)
    problems, _ = check(letter("I spent 2021–2023 building RAG systems."))
    assert not any("dash" in p for p in problems)


def test_spaced_en_dash_is_a_problem():
    problems, _ = check(letter("I build RAG systems – fast ones."))
    assert any("dash" in p for p in problems)


def test_an_invented_number_is_caught():
    problems, _ = check(letter("I cut latency by 60% with Python."))
    assert any("60%" in p for p in problems)


def test_numbers_from_the_ad_are_allowed():
    problems, _ = check(letter("I'd enjoy joining a team of 12."))
    assert problems == []


def test_an_invented_employer_or_tool_is_caught():
    problems, _ = check(letter("At Google I deployed models on Kafka with Python."))
    assert any("Google" in p and "Kafka" in p for p in problems)


def test_a_name_only_in_the_ad_is_a_warning():
    problems, warnings = check(letter("I'm keen to grow my Kubernetes skills."))
    assert problems == [] and any("Kubernetes" in w for w in warnings)


def test_whole_word_matching():
    # "CI" mustn't count as found just because "precision" contains "ci".
    problems, _ = check(letter("I set up CI for every service."))
    assert any("CI" in p for p in problems)


def test_placeholders_and_length():
    problems, _ = check(letter("I worked at [Company] on Python."))
    assert any("placeholder" in p for p in problems)
    problems, _ = check(letter("Python " * 200), max_words=150)
    assert any("too long" in p for p in problems)


# --- Writer -----------------------------------------------------------------------------


class FakeChat:
    """Plays the model. Letter calls get `replies` in turn (None = cut off at the cap, an
    exception is raised). Sentence checks pass everything except sentences containing a
    phrase in `unsupported`."""

    def __init__(self, *replies, unsupported=()):
        self.replies = list(replies)
        self.unsupported = unsupported
        self.calls = []

    def chat(self, **kwargs):
        self.calls.append(kwargs)
        if (kwargs.get("format") or {}).get("title") == "SupportCheck":
            listed = re.findall(
                r"^(\d+)\. (.*)$", kwargs["messages"][1]["content"], re.MULTILINE
            )
            checks = [
                {
                    "number": int(n),
                    "supported": not any(u in t for u in self.unsupported),
                }
                for n, t in listed
            ]
            return {"message": {"content": json.dumps({"sentences": checks})}}
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        if reply is None:
            return {"message": {"content": "..."}, "done_reason": "length"}
        return {"message": {"content": reply}}


GOOD = letter("I built RAG pipelines on MongoDB Vector Search with Python.")
DASHED = letter("I built RAG pipelines — with Python.")


def test_writer_returns_a_clean_first_draft():
    chat = FakeChat(GOOD)
    out = LetterWriter(chat, "m").write("AI Engineer", "Beta Labs", AD, CV, "A")
    assert out.text == GOOD and out.problems == []
    assert len(chat.calls) == 2  # the draft, then the sentence-by-sentence check
    prompt = chat.calls[0]["messages"][0]["content"]
    assert "Ada Example" in prompt and "250 words" in prompt  # name from the CV


def test_writer_redrafts_once_with_the_problems_spelled_out():
    chat = FakeChat(DASHED, GOOD)
    out = LetterWriter(chat, "m").write("AI Engineer", "Beta Labs", AD, CV, "B")
    assert out.text == GOOD and out.problems == []
    feedback = chat.calls[2]["messages"][-1]["content"]  # calls: draft, check, redraft
    assert "dash" in feedback


def test_writer_keeps_flagged_letter_after_two_tries():
    chat = FakeChat(DASHED, DASHED)
    out = LetterWriter(chat, "m").write("AI Engineer", "Beta Labs", AD, CV, "B")
    assert out.text == DASHED and any("dash" in p for p in out.problems)


def test_writer_reports_a_stuck_model():
    chat = FakeChat(None, httpx.ReadTimeout("slow"))
    out = LetterWriter(chat, "m").write("AI Engineer", "Beta Labs", AD, CV, "A")
    assert out.text == "" and "looped" in out.problems[0]


# --- Answers -----------------------------------------------------------------------------


def write_answers(path, **overrides):
    values = {k: f"answer to {k}" for k in QUESTIONS} | overrides
    path.write_text("\n".join(f'{k} = "{v}"' for k, v in values.items()))
    return path


def test_answers_load_and_ask_me(tmp_path):
    answers = load_answers(write_answers(tmp_path / "a.toml", salary=ASK_ME))
    assert answers.get("notice_period") == "answer to notice_period"
    assert answers.get("salary") is None  # comes to you, never guessed


def test_missing_answers_file_says_what_to_do(tmp_path):
    with pytest.raises(AnswersError, match="right_to_work"):
        load_answers(tmp_path / "nope.toml")


def test_incomplete_answers_file_names_the_gaps(tmp_path):
    path = tmp_path / "a.toml"
    path.write_text('right_to_work = "Yes"')
    with pytest.raises(AnswersError, match="notice_period"):
        load_answers(path)


# --- Packs and the CLI ----------------------------------------------------------------------


def seed(conn):
    jobs = [
        ("1", "AI Engineer", 85, []),
        ("2", "ML Engineer", 60, []),
        ("3", "Data Scientist", 40, []),
        ("4", "AI Engineer US", 90, ["Based outside the UK"]),
        ("5", "MLOps Engineer", 75, []),
    ]
    for ext, title, score, dealbreakers in jobs:
        upsert_job(
            conn,
            Job("reed", ext, title, "Beta Labs", "London", f"https://x/{ext}", AD),
            0,
        )
        job_id = conn.execute(
            "SELECT id FROM jobs WHERE external_id = ?", (ext,)
        ).fetchone()["id"]
        save_score(
            conn,
            job_id,
            score=score,
            verdict="v",
            cv="ai_engineer",
            assessment_json=json.dumps({"dealbreakers": dealbreakers}),
            unverified=0,
            model="m",
            prompt_version="v7",
        )


class FakeWriter:
    def __init__(self, text=GOOD, problems=()):
        self.text, self.problems = text, list(problems)
        self.calls = []

    def write(self, title, company, ad, cv, tier):
        self.calls.append((title, tier))
        return Letter(self.text, self.problems, [])


@pytest.fixture
def env(tmp_path, monkeypatch):
    db = tmp_path / "test.db"
    monkeypatch.setenv("DB_PATH", str(db))
    conn = connect(db)
    init_db(conn)
    seed(conn)
    # Reject job 5 in review: it must never get a pack.
    row = conn.execute("SELECT * FROM jobs WHERE external_id = '5'").fetchone()
    save_decision(conn, {**dict(row), "score": 75, "prompt_version": "v7"}, "rejected")
    writer = FakeWriter()
    monkeypatch.setattr(cli, "_make_letter_writer", lambda s: writer)
    monkeypatch.setattr(cv_module, "load_cv", lambda key: CV)
    monkeypatch.setattr(
        answers_module, "DEFAULT_ANSWERS_PATH", write_answers(tmp_path / "answers.toml")
    )
    monkeypatch.setattr(cli, "_copy_to_clipboard", lambda text: True)
    monkeypatch.setattr(cli.typer, "launch", lambda url: 0)
    return conn, writer


def test_prepare_writes_letters_for_every_eligible_job(env):
    conn, writer = env
    out = CliRunner().invoke(cli.app, ["apply", "prepare"]).output
    assert sorted(writer.calls) == [
        ("AI Engineer", "A"),
        ("Data Scientist", "B"),
        ("ML Engineer", "B"),
    ]
    assert "Prepared 3: tier A 1" in out
    assert "dealbreaker" in out and "rejected it: 1" in out
    assert "scored under 50" not in out  # a low score no longer blocks a letter
    assert pack_counts(conn) == {"prepared": 3}
    # Idempotent: a second run has nothing new to do.
    again = CliRunner().invoke(cli.app, ["apply", "prepare"]).output
    assert "Nothing to prepare" in again and len(writer.calls) == 3


def test_review_approve_and_confirm_records_exactly_what_was_sent(env):
    conn, _ = env
    CliRunner().invoke(cli.app, ["apply", "prepare"])
    # Tier A first: approve it and confirm it was submitted; quit at the next one.
    out = CliRunner().invoke(cli.app, ["apply", "review"], input="a\ny\nq\n").output
    assert "Sent 1" in out, out
    pack = conn.execute(
        "SELECT * FROM application_packs WHERE status = 'sent'"
    ).fetchone()
    assert pack["external_id"] == "1" and pack["letter"] == GOOD
    assert (
        json.loads(pack["answers_json"])["notice_period"] == "answer to notice_period"
    )
    app = get_application(conn, pack["application_id"])
    assert app["source"] == "own_agent" and app["status"] == "submitted"
    assert app["cv_version"] == "cv_ai_engineer.pdf" and app["role"] == "AI Engineer"


def test_review_not_submitted_keeps_it_for_later(env):
    conn, _ = env
    CliRunner().invoke(cli.app, ["apply", "prepare"])
    out = CliRunner().invoke(cli.app, ["apply", "review"], input="a\nn\nq\n").output
    assert "Kept for later" in out
    assert pack_counts(conn) == {"prepared": 3}


def test_review_reject_also_records_the_decision(env):
    conn, _ = env
    CliRunner().invoke(cli.app, ["apply", "prepare"])
    CliRunner().invoke(cli.app, ["apply", "review"], input="r\nq\n")
    assert pack_counts(conn) == {"prepared": 2, "rejected": 1}
    decision = conn.execute(
        "SELECT decision FROM job_decisions WHERE external_id = '1'"
    ).fetchone()
    assert decision["decision"] == "rejected"


def test_flagged_letter_needs_confirmation_to_send(env):
    _, writer = env
    writer.text, writer.problems = DASHED, ["uses a dash"]
    CliRunner().invoke(cli.app, ["apply", "prepare"])
    # Approve, decline the override, then quit: nothing sent.
    out = CliRunner().invoke(cli.app, ["apply", "review"], input="a\nn\nq\n").output
    assert "Send it anyway?" in out and "Sent 0" in out


def test_edit_rechecks_the_letter(env, monkeypatch):
    conn, writer = env
    writer.text, writer.problems = DASHED, ["uses a dash"]
    CliRunner().invoke(cli.app, ["apply", "prepare"])
    monkeypatch.setattr(cli, "_edit_text", lambda text: GOOD)
    out = CliRunner().invoke(cli.app, ["apply", "review"], input="e\na\ny\nq\n").output
    assert "Sent 1" in out, out
    sent = conn.execute(
        "SELECT letter, problems_json FROM application_packs WHERE status = 'sent'"
    ).fetchone()
    assert sent["letter"] == GOOD and json.loads(sent["problems_json"]) == []


def test_review_without_answers_file_says_how_to_fix(env, monkeypatch, tmp_path):
    monkeypatch.setattr(answers_module, "DEFAULT_ANSWERS_PATH", tmp_path / "none.toml")
    result = CliRunner().invoke(cli.app, ["apply", "review"])
    assert result.exit_code == 1 and "No screening answers yet" in result.output


def test_jobs_to_prepare_skips_jobs_that_have_a_pack(env):
    conn, _ = env
    before = len(jobs_to_prepare(conn))
    CliRunner().invoke(cli.app, ["apply", "prepare"])
    assert len(jobs_to_prepare(conn)) == before - 3


# --- Stricter letters: filler, openings, and the sentence-by-sentence check ---------------


def test_filler_phrases_are_problems():
    problems, _ = check(letter("My Python work aligns with your needs."))
    assert any("aligns with" in p for p in problems)


def test_repeated_i_have_openings_are_a_problem():
    problems, _ = check(letter("I have built RAG pipelines. I have used Python."))
    assert any('"I have"' in p for p in problems)


def test_third_person_letter_is_a_problem_but_the_sign_off_is_fine():
    problems, _ = check(letter("Ada built RAG pipelines."))
    assert any("first person" in p for p in problems)
    # The name under "Kind regards," is fine.
    problems, _ = check(letter("I built RAG pipelines."))
    assert not any("first person" in p for p in problems)


def test_letter_must_name_the_role_or_company():
    text = (
        "Dear Hiring Manager,\n\nI built RAG pipelines.\n\nKind regards,\nAda Example"
    )
    problems, _ = check(text)
    assert "doesn't name the role or the company" in problems


def test_letter_sentences_drop_greeting_and_sign_off():
    assert letter_sentences(letter("I built RAG pipelines. It worked.")) == [
        OPENING,
        "I built RAG pipelines.",
        "It worked.",
    ]


def test_a_heading_line_is_not_glued_to_the_next_sentence():
    # The model sometimes adds "Role at Company" with no full stop. Joined to the next
    # line, it read as "AI Engineer at Beta Labs I built...": a false claim.
    text = (
        "Dear Hiring Manager,\nAI Engineer at Beta Labs\n\n"
        "I built RAG pipelines. It worked.\n\nKind regards,\nAda Example"
    )
    assert letter_sentences(text) == [
        "AI Engineer at Beta Labs",
        "I built RAG pipelines.",
        "It worked.",
    ]


def test_drop_heading_removes_the_title_line_and_keeps_the_rest():
    text = (
        "Dear Hiring Manager,\nAI Engineer at Beta Labs\n\n"
        "I'm applying for the AI Engineer role at Beta Labs. I built RAG pipelines."
        "\n\nKind regards,\nAda Example"
    )
    assert drop_heading(text, "AI Engineer") == (
        "Dear Hiring Manager,\n\n"
        "I'm applying for the AI Engineer role at Beta Labs. I built RAG pipelines."
        "\n\nKind regards,\nAda Example"
    )


def test_a_sentence_the_cv_doesnt_back_is_redrafted():
    # Real words, false claim: the kind of error the word-level checks can't see.
    wrong = letter("At Acme Foods I led an enterprise-scale Python platform.")
    chat = FakeChat(wrong, GOOD, unsupported=("enterprise-scale",))
    out = LetterWriter(chat, "m").write("AI Engineer", "Beta Labs", AD, CV, "A")
    assert out.text == GOOD and out.problems == []
    feedback = chat.calls[2]["messages"][-1]["content"]
    assert "not backed by your CV" in feedback and "enterprise-scale" in feedback


def test_an_unbacked_sentence_still_there_after_redraft_is_flagged():
    wrong = letter("At Acme Foods I led an enterprise-scale Python platform.")
    chat = FakeChat(wrong, wrong, unsupported=("enterprise-scale",))
    out = LetterWriter(chat, "m").write("AI Engineer", "Beta Labs", AD, CV, "A")
    assert any("not backed by your CV" in p for p in out.problems)


def test_a_failed_sentence_check_is_a_warning_not_silence():
    class BrokenCheck(FakeChat):
        def chat(self, **kwargs):
            if (kwargs.get("format") or {}).get("title") == "SupportCheck":
                return {"message": {"content": "nonsense"}}
            return super().chat(**kwargs)

    out = LetterWriter(BrokenCheck(GOOD), "m").write(
        "AI Engineer", "Beta Labs", AD, CV, "A"
    )
    assert out.problems == [] and any("couldn't run" in w for w in out.warnings)


def test_redo_redrafts_unsent_letters_but_keeps_sent_ones(env):
    conn, writer = env
    CliRunner().invoke(cli.app, ["apply", "prepare"])
    CliRunner().invoke(cli.app, ["apply", "review"], input="a\ny\nq\n")  # send tier A
    out = CliRunner().invoke(cli.app, ["apply", "prepare", "--redo"]).output
    assert "Redrafting 2 unsent letter(s)" in out
    assert pack_counts(conn) == {"sent": 1, "prepared": 2}
    assert writer.calls[-1] == ("Data Scientist", "B")


def test_model_sizes_are_not_names():
    # "8B" in "Llama 3.1 8B" was flagged as an unknown name "B".
    cv = CV + "Evaluated Llama 3.1 8B and Qwen2.5 7B.\n"
    problems, _ = check_letter(
        letter("I evaluated Llama 3.1 8B and Qwen2.5 7B with Python."),
        cv,
        AD,
        "AI Engineer",
        "Beta Labs",
        250,
    )
    assert problems == []
