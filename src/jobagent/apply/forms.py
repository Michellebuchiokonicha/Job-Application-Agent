"""Application forms: read a job's questions before any browser opens.

Reading the questions ahead of time means answers can be prepared overnight, and the ones
only you can answer are asked once, in review, not discovered halfway through a form.

Lever's apply page is public HTML. Standard fields (name, email, CV...) are plain inputs.
Each block of custom questions carries its own definition as JSON (question text, type,
options) in a hidden "baseTemplate" input, so nothing needs to run in a browser to read it.

Greenhouse is simpler still: its public job API returns the questions as JSON
(`?questions=true`), and the rendered form uses each field's name as the element's id.
"""

import json
from dataclasses import dataclass

from bs4 import BeautifulSoup


@dataclass(frozen=True)
class Question:
    field: str  # the form's name for it: "email", "cards[<id>][field0]"
    label: str  # the question as the form shows it
    kind: str  # text, email, file, textarea, dropdown, multiple-choice, multiple-select
    required: bool
    options: tuple[str, ...] = ()  # the choices, for dropdowns and multiple choice


def lever_apply_url(job_url: str) -> str:
    """Lever's form lives at the job's URL plus /apply."""
    url = job_url.rstrip("/")
    return url if url.endswith("/apply") else url + "/apply"


def parse_lever_form(html: str) -> list[Question]:
    """Every question on a Lever apply page, in page order: standard fields first."""
    soup = BeautifulSoup(html, "html.parser")
    questions: list[Question] = []

    # Standard fields: one <li class="application-question"> each, label beside the input.
    for li in soup.select("li.application-question"):
        el = li.find(["input", "textarea", "select"], attrs={"name": True})
        if el is None or el["name"].startswith(("cards[", "consent")):
            continue  # custom questions are read from their JSON below; consent stays yours
        label_el = li.select_one(".application-label")
        label = label_el.get_text(" ", strip=True).replace("✱", "").strip()
        kind = el.get("type", "text") if el.name == "input" else el.name
        required = li.select_one(".required") is not None or el.has_attr("required")
        questions.append(Question(el["name"], label, kind, required))

    # Custom questions: the JSON definition of each card. Field names follow Lever's
    # pattern, cards[<card id>][field<n>], which is what the browser step fills in.
    for hidden in soup.select('input[type="hidden"][name$="[baseTemplate]"]'):
        card_id = hidden["name"].removeprefix("cards[").removesuffix("][baseTemplate]")
        card = json.loads(hidden["value"])
        for i, f in enumerate(card.get("fields", [])):
            questions.append(
                Question(
                    field=f"cards[{card_id}][field{i}]",
                    label=f.get("text", "").strip(),
                    kind=f.get("type", "text"),
                    required=bool(f.get("required")),
                    options=tuple(o.get("text", "") for o in f.get("options", [])),
                )
            )

    # The free-text box at the end: where a cover letter goes.
    if soup.find("textarea", attrs={"name": "comments"}):
        questions.append(
            Question(
                "comments", "Additional information (cover letter)", "textarea", False
            )
        )
    return questions


# ---- Greenhouse ------------------------------------------------------------------------

# Greenhouse's field types -> ours. Its choice questions render as searchable dropdowns.
_GREENHOUSE_KINDS = {
    "input_text": "text",
    "textarea": "textarea",
    "input_file": "file",
    "input_hidden": "hidden",
    "multi_value_single_select": "dropdown",
    "multi_value_multi_select": "multiple-select",
}
# Typed alternatives to an upload: the CV is always uploaded, and the letter goes in
# cover_letter_text where a form offers it (see fill.LETTER_FIELDS).
_TYPED_ALTERNATIVES = {"resume_text"}


def greenhouse_urls(external_id: str) -> tuple[str, str]:
    """(questions API, form page) for a Greenhouse job stored as "<slug>:<id>". The form
    page is Greenhouse's own, not the company's site, where it may sit in an iframe."""
    slug, job_id = external_id.split(":", 1)
    return (
        f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs/{job_id}?questions=true",
        f"https://job-boards.greenhouse.io/{slug}/jobs/{job_id}",
    )


def parse_greenhouse_questions(data: dict) -> list[Question]:
    """Every question in a Greenhouse job's API response, in form order."""
    questions: list[Question] = []
    for q in data.get("questions") or []:
        for f in q.get("fields") or []:
            kind = _GREENHOUSE_KINDS.get(f.get("type", ""), "text")
            if kind == "hidden" or f.get("name") in _TYPED_ALTERNATIVES:
                continue
            questions.append(
                Question(
                    field=f["name"],
                    label=(q.get("label") or "").strip(),
                    kind=kind,
                    required=bool(q.get("required")),
                    options=tuple(v.get("label", "") for v in f.get("values") or []),
                )
            )
    return questions
