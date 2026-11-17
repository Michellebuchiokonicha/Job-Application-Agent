"""known_answer: which approved answer fits which form question. Made-up profile."""

import pytest

from jobagent.apply.answers import Answers
from jobagent.apply.fill import known_answer, question_key
from jobagent.apply.forms import Question
from jobagent.apply.profile import ProfileError, load_profile
from jobagent.core.store import connect, init_db, save_answer, saved_answers

PROFILE = {
    "name": "Ada Example",
    "email": "ada@example.com",
    "phone": "+44 7000 000000",
    "location": "London, UK",
    "current_company": "Beta Labs",
    "linkedin": "https://linkedin.com/in/ada-example",
    "github": "https://github.com/ada-example",
}
ANSWERS = Answers(
    {
        "right_to_work": "Yes",
        "sponsorship_to_start": "No",
        "sponsorship_now_or_future": "No",
        "notice_period": "One month",
        "salary": "Open",
        "earliest_start": "ask_me",
    }
)


def ask(field="cards[x][field0]", label="", kind="text", options=()):
    q = Question(field, label, kind, True, tuple(options))
    return known_answer(q, PROFILE, ANSWERS)


# 1. Standard Lever fields are known by their field name, not their label.
def test_profile_fields_by_field_name():
    assert ask("name", "Full name") == "Ada Example"
    assert ask("email", "Email", "email") == "ada@example.com"
    assert ask("location", "Current location") == "London, UK"
    assert ask("org", "Current company") == "Beta Labs"
    assert (
        ask("urls[LinkedIn]", "LinkedIn URL") == "https://linkedin.com/in/ada-example"
    )
    assert ask("urls[GitHub]", "GitHub URL") == "https://github.com/ada-example"


# 2. A profile field you haven't filled in comes to you.
def test_missing_profile_field_is_none():
    assert ask("urls[Portfolio]", "Portfolio URL") is None


# 3. Screening questions are recognised by words in the label, whatever the wording.
def test_screening_questions_by_label():
    assert ask(label="Are you legally authorized to work in the UK?") == "Yes"
    assert ask(label="Do you have the right to work in the UK?") == "Yes"
    assert ask(label="What is your notice period?") == "One month"
    assert ask(label="What are your salary expectations?") == "Open"


# 4. "Now or in the future" and "to start" are different questions with their own answers.
def test_two_kinds_of_sponsorship_question():
    future = "Will you now or in the future require sponsorship for a visa?"
    start = "Will you require visa sponsorship to start?"
    assert ask(label=future) == "No"
    assert ask(label=start) == "No"
    q = Question("f", future, "text", True)
    other = Answers(ANSWERS.values | {"sponsorship_now_or_future": "Yes"})
    assert known_answer(q, PROFILE, other) == "Yes"  # it used the "future" key


# 5. Matching ignores upper/lower case.
def test_case_does_not_matter():
    assert ask(label="RIGHT TO WORK in the UK") == "Yes"


# 6. "ask_me" means it comes to you, even though the question is recognised.
def test_ask_me_comes_to_you():
    assert ask(label="What is your earliest start date?") is None


# 7. For a choice question, the answer must be exactly one of the options. Never pick
#    the nearest one: "Yes" is not "Yes, I have a valid UK visa".
def test_choice_answer_must_be_one_of_the_options():
    label = "Are you legally authorized to work in the UK?"
    assert ask(label=label, kind="multiple-choice", options=["Yes", "No"]) == "Yes"
    long_options = ["Yes, I am a UK citizen", "Yes, I have a visa", "No"]
    assert ask(label=label, kind="multiple-choice", options=long_options) is None


# 8. Anything else is never guessed.
def test_unknown_question_comes_to_you():
    assert ask(label="Give us three numbers that describe you") is None
    assert ask(label="Why do you want to work here?", kind="textarea") is None


# --- Saved answers: the same question on another form ------------------------------------


def test_question_key_ignores_case_punctuation_and_spacing():
    assert question_key("How did you hear about us?") == "how did you hear about us"
    assert question_key("  HOW did you hear about   us ") == "how did you hear about us"
    assert question_key("How did you find us?") != question_key("How did you hear?")


def test_saved_answer_is_reused_for_the_same_question():
    saved = {question_key("Which university did you last attend?"): "Acme University"}
    assert ask(label="Which university did you last attend?") is None  # not saved: you
    q = Question(
        "cards[y][field0]", "WHICH university did you last attend", "text", True
    )
    assert known_answer(q, PROFILE, ANSWERS, saved) == "Acme University"


def test_saved_choice_answer_still_has_to_be_an_option():
    saved = {question_key("How did you hear about us?"): "LinkedIn"}
    q = Question(
        "f", "How did you hear about us?", "dropdown", True, ("Job board", "Other")
    )
    assert (
        known_answer(q, PROFILE, ANSWERS, saved) is None
    )  # this form has no "LinkedIn"


def test_screening_answers_win_over_saved_ones():
    label = "Will you now or in the future require sponsorship?"
    saved = {question_key(label): "Yes"}  # an old answer must not override answers.toml
    assert (
        known_answer(Question("f", label, "text", True), PROFILE, ANSWERS, saved)
        == "No"
    )


def test_save_and_load_answers():
    conn = connect()
    init_db(conn)
    save_answer(conn, "k", "Question?", "first")
    save_answer(conn, "k", "Question?", "second")  # answering again replaces it
    assert saved_answers(conn) == {"k": "second"}


# --- Profile file ----------------------------------------------------------------------


def test_profile_missing_file_says_how_to_create_it(tmp_path):
    with pytest.raises(ProfileError, match="No profile yet"):
        load_profile(tmp_path / "profile.toml")


def test_profile_loads_and_rejects_typos(tmp_path):
    path = tmp_path / "profile.toml"
    path.write_text('name = "Ada Example"\nemail = "ada@example.com"\n')
    assert load_profile(path) == {"name": "Ada Example", "email": "ada@example.com"}
    path.write_text('emial = "ada@example.com"\n')
    with pytest.raises(ProfileError, match="unknown keys: emial"):
        load_profile(path)
