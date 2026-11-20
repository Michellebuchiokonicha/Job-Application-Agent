"""Phase 5: screening answers, cover-letter checks, packs and the review flow. Made-up data."""

import json
import re

import httpx
import pytest
from typer.testing import CliRunner

from jobagent import cli
from jobagent.apply import answers as answers_module
from jobagent.apply import browser as browser_module
from jobagent.apply import profile as profile_module
from jobagent.apply.answers import ASK_ME, QUESTIONS, AnswersError, load_answers
from jobagent.apply.fill import question_key
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
    save_answer,
    save_decision,
    save_score,
    saved_answers,
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
    # Removing it would leave only the opening: too thin to send, so it comes to you.
    wrong = letter("At Acme Foods I led an enterprise-scale Python platform.")
    chat = FakeChat(wrong, wrong, unsupported=("enterprise-scale",))
    out = LetterWriter(chat, "m").write("AI Engineer", "Beta Labs", AD, CV, "A")
    assert any("not backed by your CV" in p for p in out.problems)


def test_an_unbacked_sentence_left_after_redraft_is_removed_and_you_are_told():
    true = "I built RAG pipelines on MongoDB Vector Search with Python. It worked."
    false = "At Acme Foods I led an enterprise-scale Python platform."
    wrong = letter(f"{true}\n\n{false}")
    chat = FakeChat(wrong, wrong, unsupported=("enterprise-scale",))
    out = LetterWriter(chat, "m").write("AI Engineer", "Beta Labs", AD, CV, "A")
    assert out.problems == []  # nothing left to fix: ready to send
    assert "enterprise-scale" not in out.text and true in out.text
    assert out.text.endswith("Kind regards,\nAda Example")
    assert "\n\n\n" not in out.text  # the emptied paragraph doesn't leave a gap
    assert any("removed" in w and "enterprise-scale" in w for w in out.warnings)


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


# --- Reading the form in review (Lever) ----------------------------------------------------

HEARD = "How did you hear about this job?"
FORM = f"""
<ul>
  <li class="application-question"><label>
    <div class="application-label">Full name<span class="required">✱</span></div>
    <input type="text" name="name" required></label></li>
</ul>
<input type="hidden" name="cards[c1][baseTemplate]" value='{
    json.dumps(
        {
            "fields": [
                {
                    "type": "dropdown",
                    "required": True,
                    "text": HEARD,
                    "options": [{"text": "Job board"}, {"text": "Other"}],
                },
                {"type": "text", "required": True, "text": "Preferred name"},
            ]
        }
    )
}'>
<textarea name="comments"></textarea>
"""


@pytest.fixture
def lever_env(env, tmp_path, monkeypatch):
    conn, _ = env
    conn.execute("UPDATE jobs SET source = 'lever' WHERE external_id = '1'")
    profile = tmp_path / "profile.toml"
    profile.write_text('name = "Ada Example"\nemail = "ada@example.com"\n')
    monkeypatch.setattr(profile_module, "DEFAULT_PROFILE_PATH", profile)
    monkeypatch.setattr(cli, "_fetch_form_html", lambda url: FORM)
    CliRunner().invoke(cli.app, ["apply", "prepare"])
    return conn


def test_review_asks_what_the_form_needs_and_saves_it(lever_env):
    conn = lever_env
    # Approve job 1 (Lever, tier A): pick "Other" (2), type a preferred name, submit.
    out = (
        CliRunner()
        .invoke(cli.app, ["apply", "review"], input="a\n2\nAda\ny\nq\n")
        .output
    )
    flat = " ".join(out.split())  # Rich wraps long lines in an 80-column test terminal
    assert "1 answered from your profile" in flat and "2 need you" in flat
    assert saved_answers(conn) == {
        question_key(HEARD): "Other",
        question_key("Preferred name"): "Ada",
    }
    sent = conn.execute(
        "SELECT answers_json FROM application_packs WHERE status = 'sent'"
    ).fetchone()
    assert json.loads(sent["answers_json"])[HEARD] == "Other"  # the record of what went


def test_the_next_form_with_the_same_question_fills_itself(lever_env):
    conn = lever_env
    save_answer(conn, question_key(HEARD), HEARD, "Job board")
    save_answer(conn, question_key("Preferred name"), "Preferred name", "Ada")
    out = CliRunner().invoke(cli.app, ["apply", "review"], input="a\ny\nq\n").output
    flat = " ".join(out.split())
    assert "3 answered" in flat and "0 need you" in flat


def test_a_choice_is_picked_by_number_so_it_is_always_a_real_option(lever_env):
    out = (
        CliRunner()
        .invoke(cli.app, ["apply", "review"], input="a\nLinkedIn\n9\n1\nAda\ny\nq\n")
        .output
    )
    assert out.count("Type number from the list.") == 2  # text and 9 both refused
    assert saved_answers(lever_env)[question_key(HEARD)] == "Job board"


def test_no_profile_falls_back_to_the_page(lever_env, tmp_path, monkeypatch):
    monkeypatch.setattr(profile_module, "DEFAULT_PROFILE_PATH", tmp_path / "none.toml")
    out = CliRunner().invoke(cli.app, ["apply", "review"], input="a\ny\nq\n").output
    assert "No profile yet" in out and "Sent 1" in out


def test_approve_fills_the_form_in_the_browser_and_records_it(lever_env, monkeypatch):
    conn = lever_env
    seen = {}

    def fake_open_and_fill(
        url, questions, answers, cv_path, hand_over, ats, screenshot
    ):
        seen.update(url=url, answers=answers, cv=cv_path.name, ats=ats)
        return hand_over(browser_module.FillResult(filled=["Full name"]))

    monkeypatch.setattr(browser_module, "open_and_fill", fake_open_and_fill)
    out = (
        CliRunner()
        .invoke(cli.app, ["apply", "review"], input="a\n2\nAda\ny\nq\n")
        .output
    )
    assert seen["url"].endswith("/apply") and seen["cv"] == "cv_ai_engineer.pdf"
    assert seen["ats"] == "lever"
    assert seen["answers"]["name"] == "Ada Example"
    assert seen["answers"]["comments"].startswith("Dear Hiring Manager")  # the letter
    assert "click Submit yourself" in out and "Sent 1" in out
    sent = conn.execute(
        "SELECT answers_json FROM application_packs WHERE status = 'sent'"
    ).fetchone()
    assert json.loads(sent["answers_json"])["Full name"] == "Ada Example"


def test_no_browser_falls_back_to_copying_by_hand(lever_env):
    # conftest's guard makes the browser fail, as it would without Chrome.
    out = (
        CliRunner()
        .invoke(cli.app, ["apply", "review"], input="a\n2\nAda\ny\nq\n")
        .output
    )
    assert "Couldn't open the browser" in out and "Answers for this form" in out
    assert "Sent 1" in out


GREENHOUSE_API = {
    "questions": [
        {
            "label": "First Name",
            "required": True,
            "fields": [{"name": "first_name", "type": "input_text"}],
        },
        {
            "label": "Last Name",
            "required": True,
            "fields": [{"name": "last_name", "type": "input_text"}],
        },
        {
            "label": "Resume/CV",
            "required": True,
            "fields": [
                {"name": "resume", "type": "input_file"},
                {"name": "resume_text", "type": "textarea"},
            ],
        },
        {
            "label": "LinkedIn Profile",
            "required": False,
            "fields": [{"name": "question_2", "type": "input_text"}],
        },
        {
            "label": HEARD,
            "required": True,
            "fields": [
                {
                    "name": "question_1",
                    "type": "multi_value_single_select",
                    "values": [
                        {"label": "Job board", "value": 1},
                        {"label": "Other", "value": 2},
                    ],
                }
            ],
        },
    ]
}


def test_greenhouse_form_is_read_from_the_api_and_filled(env, tmp_path, monkeypatch):
    conn, _ = env
    conn.execute(
        "UPDATE jobs SET source = 'greenhouse', external_id = 'acme:77' "
        "WHERE external_id = '1'"
    )
    profile = tmp_path / "profile.toml"
    profile.write_text(
        'name = "Ada Example"\nlinkedin = "https://linkedin.com/in/ada"\n'
    )
    monkeypatch.setattr(profile_module, "DEFAULT_PROFILE_PATH", profile)
    asked = []
    monkeypatch.setattr(
        cli, "_fetch_form_json", lambda url: asked.append(url) or GREENHOUSE_API
    )
    seen = {}

    def fake_open_and_fill(
        url, questions, answers, cv_path, hand_over, ats, screenshot
    ):
        seen.update(url=url, answers=answers, ats=ats)
        return hand_over(browser_module.FillResult())

    monkeypatch.setattr(browser_module, "open_and_fill", fake_open_and_fill)
    CliRunner().invoke(cli.app, ["apply", "prepare"])
    out = CliRunner().invoke(cli.app, ["apply", "review"], input="a\n1\ny\nq\n").output
    assert asked == [
        "https://boards-api.greenhouse.io/v1/boards/acme/jobs/77?questions=true"
    ]
    assert seen["url"] == "https://job-boards.greenhouse.io/acme/jobs/77"
    assert seen["ats"] == "greenhouse"
    assert seen["answers"] == {
        "first_name": "Ada",
        "last_name": "Example",
        "question_2": "https://linkedin.com/in/ada",  # profile link, found by its label
        "question_1": "Job board",  # asked once, picked by number
    }
    assert "Sent 1" in out


# --- Letter fixes: prompt echo, the fixed opening, dashes in titles, flagged-only redo ---


def test_explaining_how_work_matches_the_job_is_filler():
    problems, _ = check(letter("My Python work matches the requirement for RAG."))
    assert any("the requirement" in p for p in problems)


def test_a_dash_in_the_job_title_is_not_the_letter_writing():
    text = (
        "Dear Hiring Manager,\n\nI'm applying for the AI Engineer – Vision role at "
        "Beta Labs. I built RAG pipelines.\n\nKind regards,\nAda Example"
    )
    problems, _ = check_letter(text, CV, AD, "AI Engineer – Vision", "Beta Labs", 250)
    assert not any("dash" in p for p in problems)


def test_the_fixed_opening_is_never_sent_to_the_model_check():
    seen = []

    class Client:
        def chat(self, model, messages, **kw):
            seen.append(messages[-1]["content"])
            return {"message": {"content": '{"sentences": []}'}, "done_reason": "stop"}

    LetterWriter(Client(), "m").unsupported(letter("I built RAG pipelines."), CV)
    assert "applying for the" not in seen[0] and "I built RAG pipelines." in seen[0]


def test_redo_flagged_redrafts_only_the_flagged_letters(env):
    conn, writer = env
    CliRunner().invoke(cli.app, ["apply", "prepare"])
    conn.execute(
        """UPDATE application_packs SET problems_json = '["x"]'
           WHERE external_id = '2'"""
    )
    writer.calls.clear()
    out = CliRunner().invoke(cli.app, ["apply", "prepare", "--redo-flagged"]).output
    assert "Redrafting 1 unsent letter(s)" in out
    assert writer.calls == [("ML Engineer", "B")]


# --- Upgrade words: checked exactly, because the sentence checker misses some ----------


def test_an_upgrade_word_the_cv_never_uses_is_a_problem():
    problems, _ = check(letter("I led the RAG pipelines on MongoDB Vector Search."))
    assert 'says "led", which your CV never does' in problems


def test_upgrade_word_is_fine_when_the_cv_uses_it():
    cv = CV + "Led the RAG rollout across two teams.\n"
    problems, _ = check_letter(
        letter("I led the RAG rollout across two teams."),
        cv,
        AD,
        "AI Engineer",
        "Beta Labs",
        250,
    )
    assert not any("which your CV never does" in p for p in problems)


def test_led_to_and_lead_in_the_job_title_are_not_upgrades():
    text = (
        "Dear Hiring Manager,\n\nI'm applying for the Lead AI Engineer role at Beta "
        "Labs. Better retrieval led to fewer support tickets.\n\nKind regards,\n"
        "Ada Example"
    )
    problems, _ = check_letter(text, CV, AD, "Lead AI Engineer", "Beta Labs", 250)
    assert not any("which your CV never does" in p for p in problems)


def test_recheck_flags_letters_a_new_check_would_catch(env):
    conn, writer = env
    writer.text = letter("I led the RAG pipelines on MongoDB Vector Search.")
    # Prepared before the check existed: saved as clean.
    CliRunner().invoke(cli.app, ["apply", "prepare"])
    conn.execute("UPDATE application_packs SET problems_json = '[]'")
    out = CliRunner().invoke(cli.app, ["apply", "recheck"]).output
    assert "Newly flagged: 3" in out and 'says "led"' in out
    flagged = conn.execute(
        "SELECT COUNT(*) FROM application_packs WHERE problems_json != '[]'"
    ).fetchone()[0]
    assert flagged == 3


ASHBY_FORM = {
    "data": {
        "jobPosting": {
            "applicationForm": {
                "sections": [
                    {
                        "fieldEntries": [
                            {
                                "isRequired": True,
                                "field": {
                                    "path": "_systemfield_name",
                                    "title": "Name",
                                    "type": "String",
                                },
                            },
                            {
                                "isRequired": True,
                                "field": {
                                    "path": "p1",
                                    "title": "Will you now or in the future require "
                                    "visa sponsorship?",
                                    "type": "Boolean",
                                },
                            },
                        ]
                    }
                ]
            }
        }
    }
}


def _as_ashby(conn, tmp_path, monkeypatch, url):
    conn.execute(
        "UPDATE jobs SET source = 'ashby', external_id = 'acme:9', url = ? "
        "WHERE external_id = '1'",
        (url,),
    )
    profile = tmp_path / "profile.toml"
    profile.write_text('name = "Ada Example"\n')
    monkeypatch.setattr(profile_module, "DEFAULT_PROFILE_PATH", profile)
    monkeypatch.setattr(cli, "_post_form_json", lambda url, body: ASHBY_FORM)
    CliRunner().invoke(cli.app, ["apply", "prepare"])


def test_ashby_form_is_read_and_filled(env, tmp_path, monkeypatch):
    conn, _ = env
    _as_ashby(conn, tmp_path, monkeypatch, "https://jobs.ashbyhq.com/acme/9")
    seen = {}

    def fake_open_and_fill(
        url, questions, answers, cv_path, hand_over, ats, screenshot
    ):
        seen.update(url=url, answers=answers, ats=ats)
        return hand_over(browser_module.FillResult())

    monkeypatch.setattr(browser_module, "open_and_fill", fake_open_and_fill)
    # The test answers file has text, not Yes/No, for sponsorship: so it's asked (2 = No).
    out = CliRunner().invoke(cli.app, ["apply", "review"], input="a\n2\ny\nq\n").output
    assert seen["ats"] == "ashby"
    assert seen["url"] == "https://jobs.ashbyhq.com/acme/9/application"
    assert seen["answers"] == {"_systemfield_name": "Ada Example", "p1": "No"}
    assert "Sent 1" in out


def test_ashby_job_on_a_company_site_falls_back_to_the_page(env, tmp_path, monkeypatch):
    conn, _ = env
    _as_ashby(conn, tmp_path, monkeypatch, "https://acme.ai/careers/ai-engineer")
    out = CliRunner().invoke(cli.app, ["apply", "review"], input="a\ny\nq\n").output
    assert "Form:" not in out and "Sent 1" in out  # no form read: you fill the page


# --- Submit mode -------------------------------------------------------------------------


def _review_with_fake_browser(lever_env, monkeypatch, args, submitted):
    seen = {}

    def fake(url, questions, answers, cv_path, hand_over, ats, screenshot):
        seen["screenshot"] = screenshot
        result = browser_module.FillResult(filled=["Full name"])
        if screenshot is not None:  # submit mode: the agent clicks Submit
            result.submitted, result.screenshot = submitted, str(screenshot)
            if not submitted:
                result.failed.append("no confirmation after Submit")
        return hand_over(result)

    monkeypatch.setattr(browser_module, "open_and_fill", fake)
    out = (
        CliRunner()
        .invoke(cli.app, ["apply", "review", *args], input="a\n2\nAda\ny\nq\n")
        .output
    )
    return seen, out


def test_submit_mode_sends_without_asking_once_the_site_confirms(
    lever_env, monkeypatch
):
    seen, out = _review_with_fake_browser(lever_env, monkeypatch, ["--submit"], True)
    assert seen["screenshot"].name == "lever-1.png"  # the filled form, kept as a record
    assert "Submitted ✓" in out and "Did you submit" not in out
    assert "Sent 1" in out


def test_submit_mode_without_confirmation_asks_you(lever_env, monkeypatch):
    _, out = _review_with_fake_browser(lever_env, monkeypatch, ["--submit"], False)
    assert "no confirmation after Submit" in out and "Did you submit" in out


def test_without_submit_flag_the_agent_never_submits(lever_env, monkeypatch):
    seen, _ = _review_with_fake_browser(lever_env, monkeypatch, [], True)
    assert seen["screenshot"] is None


def test_daily_cap_switches_submit_off(lever_env, monkeypatch):
    seen, out = _review_with_fake_browser(
        lever_env, monkeypatch, ["--submit", "--max-submits", "0"], True
    )
    assert seen["screenshot"] is None and "Daily cap of 0 reached" in out


def test_daily_runs_fetch_score_and_letters_in_order(monkeypatch):
    calls = []
    monkeypatch.setattr(cli, "jobs_fetch", lambda **kw: calls.append("fetch"))
    monkeypatch.setattr(cli, "jobs_score", lambda **kw: calls.append("score"))
    monkeypatch.setattr(cli, "apply_prepare", lambda **kw: calls.append("letters"))
    out = CliRunner().invoke(cli.app, ["daily"]).output
    assert calls == ["fetch", "score", "letters"]
    assert "apply review --submit" in out
