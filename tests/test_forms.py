"""Reading application forms. A made-up Lever page in Lever's real HTML shape; no network."""

import html
import json
from pathlib import Path

from jobagent.apply.browser import fill_lever_form
from jobagent.apply.forms import Question, lever_apply_url, parse_lever_form


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
