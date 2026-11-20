"""Reading application forms. A made-up Lever page in Lever's real HTML shape; no network."""

import html
import json
from pathlib import Path

import pytest

from jobagent.apply.browser import (
    fill_ashby_form,
    fill_greenhouse_form,
    fill_lever_form,
    submit_and_confirm,
)
from jobagent.apply.forms import (
    Question,
    lever_apply_url,
    parse_ashby_questions,
    parse_greenhouse_questions,
    parse_lever_form,
)


def _card(card_id: str, fields: list[dict]) -> str:
    """One custom-question block, as Lever embeds it: JSON in a hidden input."""
    value = html.escape(json.dumps({"text": "Questions", "fields": fields}))
    return (
        f'<input type="hidden" name="cards[{card_id}][baseTemplate]" value="{value}">'
        f'<li class="application-question"><input name="cards[{card_id}][field0]"></li>'
    )


LEVER_FORM = f"""
<form>
<ul>
  <li class="application-question resume"><label>
    <div class="application-label">Resume/CV<span class="required">✱</span></div>
    <div class="application-field"><input type="file" name="resume"></div>
  </label></li>
  <li class="application-question"><label>
    <div class="application-label">Full name<span class="required">✱</span></div>
    <div class="application-field"><input type="text" name="name" required></div>
  </label></li>
  <li class="application-question"><label>
    <div class="application-label">Email<span class="required">✱</span></div>
    <div class="application-field"><input type="email" name="email" required></div>
  </label></li>
  <li class="application-question"><label>
    <div class="application-label">LinkedIn URL</div>
    <div class="application-field"><input type="text" name="urls[LinkedIn]"></div>
  </label></li>
</ul>
{
    _card(
        "abc",
        [
            {
                "type": "multiple-choice",
                "required": True,
                "text": "Will you now or in the future require sponsorship?",
                "options": [{"text": "Yes"}, {"text": "No"}],
            },
            {"type": "textarea", "required": True, "text": "Why Acme AI?"},
        ],
    )
}
<textarea name="comments"></textarea>
<input type="checkbox" name="consent[marketing]">
</form>
"""


def test_reads_standard_fields_with_labels_and_required():
    qs = parse_lever_form(LEVER_FORM)
    assert qs[:4] == [
        Question("resume", "Resume/CV", "file", True),
        Question("name", "Full name", "text", True),
        Question("email", "Email", "email", True),
        Question("urls[LinkedIn]", "LinkedIn URL", "text", False),
    ]


def test_reads_custom_questions_from_their_json():
    qs = {q.field: q for q in parse_lever_form(LEVER_FORM)}
    sponsorship = qs["cards[abc][field0]"]
    assert sponsorship.kind == "multiple-choice" and sponsorship.required
    assert sponsorship.options == ("Yes", "No")
    assert qs["cards[abc][field1]"].label == "Why Acme AI?"


def test_cover_letter_box_is_found_and_consent_is_left_alone():
    fields = [q.field for q in parse_lever_form(LEVER_FORM)]
    assert fields[-1] == "comments"
    assert not any(f.startswith("consent") for f in fields)  # never ticked for you
    assert len(fields) == len(set(fields))  # each question once


def test_apply_url():
    job = "https://jobs.lever.co/acmeai/u-1"
    assert lever_apply_url(job) == job + "/apply"
    assert lever_apply_url(job + "/apply") == job + "/apply"


# --- Filling the page (a fake Playwright page: no browser) ---------------------------------


class FakePage:
    def __init__(self, missing=()):
        self.calls, self.missing = [], set(missing)

    def _do(self, action, selector, value=None):
        if any(m in selector for m in self.missing):
            raise TimeoutError(selector)
        self.calls.append((action, selector, value))

    def fill(self, selector, value):
        self._do("fill", selector, value)

    def select_option(self, selector, value):
        self._do("select", selector, value)

    def check(self, selector):
        self._do("check", selector)

    def set_input_files(self, selector, path):
        self._do("upload", selector, path)

    def click(self, selector):
        self._do("click", selector)

    def get_by_role(self, role, name, exact):
        page = self

        class Option:
            @property
            def first(self):
                return self

            def click(self):
                page._do("pick", f"{role}={name}", exact)

        return Option()


QS = [
    Question("name", "Full name", "text", True),
    Question("cards[a][field0]", "Heard?", "dropdown", True, ("Job board", "Other")),
    Question("cards[a][field1]", "Sponsor?", "multiple-choice", True, ("Yes", "No")),
    Question("cards[a][field2]", "Languages", "multiple-select", True, ("EN", "FR")),
    Question("comments", "Cover letter", "textarea", False),
    Question("cards[a][field3]", "Not answered", "text", False),
]
ANSWERS = {
    "name": "Ada Example",
    "cards[a][field0]": "Other",
    "cards[a][field1]": "No",
    "cards[a][field2]": "EN; FR",
    "comments": "Dear Hiring Manager, ...",
}


def test_fills_each_kind_of_field_the_right_way():
    page = FakePage()
    result = fill_lever_form(page, QS, ANSWERS, Path("/cv/ai.pdf"))
    assert page.calls == [
        ("upload", 'input[name="resume"]', "/cv/ai.pdf"),
        ("fill", '[name="name"]', "Ada Example"),
        ("select", 'select[name="cards[a][field0]"]', "Other"),
        ("check", 'input[name="cards[a][field1]"][value="No"]', None),
        ("check", 'input[name="cards[a][field2]"][value="EN"]', None),
        ("check", 'input[name="cards[a][field2]"][value="FR"]', None),
        ("fill", '[name="comments"]', "Dear Hiring Manager, ..."),
    ]
    assert result.failed == [] and "Not answered" not in result.filled


def test_a_field_it_cannot_fill_is_named_and_the_rest_still_filled():
    page = FakePage(missing={"field0"})
    result = fill_lever_form(page, QS, ANSWERS, None)
    assert result.failed == ["Heard? (TimeoutError)"]
    assert "Cover letter" in result.filled


def test_it_never_clicks_anything_but_answers():
    page = FakePage()
    fill_lever_form(page, QS, ANSWERS, None)
    assert not any("submit" in selector.lower() for _, selector, _ in page.calls)


def test_quotes_in_an_answer_cannot_break_the_selector():
    q = Question("cards[b][field0]", "Pick", "multiple-choice", True, ('Say "yes"',))
    page = FakePage()
    fill_lever_form(page, [q], {q.field: 'Say "yes"'}, None)
    assert page.calls == [
        ("check", 'input[name="cards[b][field0]"][value="Say \\"yes\\""]', None)
    ]


def test_greenhouse_dropdowns_are_searched_then_the_exact_option_clicked():
    qs = [
        Question("first_name", "First Name", "text", True),
        Question("question_9", "Heard?", "dropdown", True, ("Job board", "Other")),
    ]
    page = FakePage()
    answers = {"first_name": "Ada", "question_9": "Other"}
    result = fill_greenhouse_form(page, qs, answers, Path("/cv/ai.pdf"))
    assert page.calls == [
        ("upload", 'input[id="resume"]', "/cv/ai.pdf"),
        ("fill", '[id="first_name"]', "Ada"),
        ("click", '[id="question_9"]', None),
        ("fill", '[id="question_9"]', "Other"),
        ("pick", "option=Other", True),  # exact text, never the nearest
    ]
    assert result.failed == []


def test_greenhouse_questions_from_the_api():
    data = {
        "questions": [
            {
                "label": "Resume/CV",
                "required": True,
                "fields": [
                    {"name": "resume", "type": "input_file"},
                    {"name": "resume_text", "type": "textarea"},
                ],
            },
            {
                "label": "Sponsorship?",
                "required": True,
                "fields": [
                    {
                        "name": "question_1",
                        "type": "multi_value_single_select",
                        "values": [{"label": "Yes"}, {"label": "No"}],
                    }
                ],
            },
        ]
    }
    assert parse_greenhouse_questions(data) == [
        Question("resume", "Resume/CV", "file", True),  # the typed CV box is skipped
        Question("question_1", "Sponsorship?", "dropdown", True, ("Yes", "No")),
    ]


# --- Ashby ------------------------------------------------------------------------------


class FakeAshbyPage:
    """Records actions as (action, where, what). Ashby places each answer inside its
    question's container, so `where` is the container or a selector within it."""

    def __init__(self, suggestion="London, England, United Kingdom"):
        self.calls, self.suggestion = [], suggestion

    def set_input_files(self, selector, path):
        self.calls.append(("upload", selector, path))

    def locator(self, selector):
        page = self

        class Loc:
            first = property(lambda self: self)

            def fill(self, value):
                page.calls.append(("fill", selector, value))

            def get_by_role(self, role, name, exact):
                class Btn:
                    def click(self):
                        page.calls.append(("press", selector, name))

                return Btn()

            def get_by_label(self, label, exact):
                class Box:
                    def check(self):
                        page.calls.append(("tick", selector, label))

                return Box()

        return Loc()

    def get_by_role(self, role):
        page = self

        class Option:
            first = property(lambda self: self)

            def inner_text(self):
                return page.suggestion

            def click(self):
                page.calls.append(("pick", "suggestion", page.suggestion))

        return Option()


ASHBY_API = {
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
                                    "path": "_systemfield_resume",
                                    "title": "Resume",
                                    "type": "File",
                                },
                            },
                            {
                                "isRequired": True,
                                "field": {
                                    "path": "p1",
                                    "title": "Sponsorship?",
                                    "type": "Boolean",
                                },
                            },
                            {
                                "isRequired": True,
                                "field": {
                                    "path": "p2",
                                    "title": "Where are you?",
                                    "type": "Location",
                                },
                            },
                            {
                                "isRequired": True,
                                "field": {
                                    "path": "p3",
                                    "title": "Heard?",
                                    "type": "ValueSelect",
                                    "selectableValues": [
                                        {"label": "Job board"},
                                        {"label": "Other"},
                                    ],
                                },
                            },
                            {
                                "isRequired": False,
                                "field": {
                                    "path": "p4",
                                    "title": "Start date",
                                    "type": "Date",
                                },
                            },
                            {
                                "isRequired": True,
                                "field": {
                                    "path": "p5",
                                    "title": "Portfolio PDF",
                                    "type": "File",
                                },
                            },
                        ]
                    }
                ]
            }
        }
    }
}


def test_ashby_questions_from_the_api():
    qs = parse_ashby_questions(ASHBY_API)
    kinds = {q.field: (q.kind, q.options) for q in qs}
    assert kinds["p1"] == ("multiple-choice", ("Yes", "No"))  # Yes/No question
    assert kinds["p2"] == ("location", ())
    assert kinds["p3"] == ("dropdown", ("Job board", "Other"))
    assert "p4" not in kinds  # an optional date picker: left out


def test_closed_ashby_job_is_an_error_not_an_empty_form():
    with pytest.raises(ValueError, match="closed"):
        parse_ashby_questions({"data": {"jobPosting": None}})


def test_ashby_answers_go_inside_their_own_question():
    qs = parse_ashby_questions(ASHBY_API)
    answers = {"_systemfield_name": "Ada", "p1": "No", "p2": "London", "p3": "Other"}
    page = FakeAshbyPage()
    result = fill_ashby_form(page, qs, answers, Path("/cv/ai.pdf"))
    assert page.calls == [
        (
            "upload",
            '[data-field-path="_systemfield_resume"] input[type="file"]',
            "/cv/ai.pdf",
        ),
        (
            "fill",
            '[data-field-path="_systemfield_name"] input, [data-field-path="_systemfield_name"] textarea',
            "Ada",
        ),
        ("press", '[data-field-path="p1"]', "No"),
        ("fill", '[data-field-path="p2"] input', "London"),
        ("pick", "suggestion", "London, England, United Kingdom"),
        ("tick", '[data-field-path="p3"]', "Other"),
    ]
    # The location it chose is shown to you; a required upload it can't do is named.
    assert "Where are you?: London, England, United Kingdom" in result.filled
    assert result.failed == ["Portfolio PDF (do this one on the page)"]


# --- Submitting: only a confirmation the site shows counts as sent ------------------------


class FakeSubmitPage:
    def __init__(self, confirms: bool):
        self.confirms, self.clicked, self.waited_for = confirms, False, None

    def evaluate(self, js, pattern):
        return 1  # e.g. "Thank you for your interest" already in the ad

    def get_by_role(self, role, name):
        page = self

        class Button:
            first = property(lambda self: self)

            def click(self):
                page.clicked = True

        return Button()

    def wait_for_function(self, js, arg, timeout):
        self.waited_for = arg
        if not self.confirms:
            raise TimeoutError("no confirmation")


def test_submit_counts_only_new_confirmation_text():
    page = FakeSubmitPage(confirms=True)
    assert submit_and_confirm(page) is True and page.clicked
    assert page.waited_for[1] == 1  # must exceed what was on the page before Submit


def test_no_confirmation_means_not_sent():
    assert submit_and_confirm(FakeSubmitPage(confirms=False)) is False
