"""Reading application forms. A made-up Lever page in Lever's real HTML shape; no network."""

import html
import json

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
