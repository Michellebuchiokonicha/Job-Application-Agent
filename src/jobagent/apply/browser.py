"""Filling an application form in a real, visible browser, then stopping for you.

It never clicks Submit. It fills what it has approved answers for, attaches your CV, and
hands over: you check the page and submit it yourself. A field it can't fill (a changed
form, a custom widget) is named, not skipped silently, and left for you.
"""

from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from jobagent.apply.fill import MULTI_SEPARATOR
from jobagent.apply.forms import Question

FIELD_TIMEOUT_MS = 5_000  # a field that isn't there fails fast, instead of hanging


@dataclass
class FillResult:
    filled: list[str] = field(default_factory=list)  # question labels
    failed: list[str] = field(default_factory=list)  # left for you, with the reason


def _css(value: str) -> str:
    """A value made safe inside a CSS attribute selector: [value="..."]."""
    return value.replace("\\", "\\\\").replace('"', '\\"')


def fill_lever_form(
    page, questions: list[Question], answers: dict[str, str], cv_path: Path | None
) -> FillResult:
    """Put each answer in its field on an open Lever form. `answers` is by field name.

    `page` is a Playwright Page (tests pass a fake with the same methods).
    """
    result = FillResult()
    if cv_path is not None:
        try:
            page.set_input_files('input[name="resume"]', str(cv_path))
            result.filled.append("Resume/CV")
        except Exception as e:  # noqa: BLE001 (any failure: named, left for you)
            result.failed.append(f"Resume/CV ({type(e).__name__})")

    for q in questions:
        answer = answers.get(q.field)
        if not answer or q.kind == "file":
            continue
        name = _css(q.field)
        try:
            if q.kind == "dropdown":
                page.select_option(f'select[name="{name}"]', value=answer)
            elif q.kind == "multiple-choice":
                page.check(f'input[name="{name}"][value="{_css(answer)}"]')
            elif q.kind == "multiple-select":
                for part in answer.split(MULTI_SEPARATOR):
                    page.check(f'input[name="{name}"][value="{_css(part)}"]')
            else:  # text, email, textarea, and the cover letter box
                page.fill(f'[name="{name}"]', answer)
            result.filled.append(q.label)
        except Exception as e:  # noqa: BLE001
            result.failed.append(f"{q.label} ({type(e).__name__})")
    return result


def open_and_fill(
    url: str,
    questions: list[Question],
    answers: dict[str, str],
    cv_path: Path | None,
    hand_over: Callable[[FillResult], bool],
) -> bool:
    """Open the form in Chrome, fill it, then hand over to you. The browser stays open
    until `hand_over` returns (it asks whether you submitted); returns its answer."""
    from playwright.sync_api import sync_playwright

    with sync_playwright() as pw:
        # Your installed Chrome, visible: you watch it fill, then check and submit.
        browser = pw.chromium.launch(channel="chrome", headless=False)
        try:
            page = browser.new_page()
            page.goto(url, wait_until="domcontentloaded")
            page.set_default_timeout(FIELD_TIMEOUT_MS)
            result = fill_lever_form(page, questions, answers, cv_path)
            return hand_over(result)
        finally:
            browser.close()
