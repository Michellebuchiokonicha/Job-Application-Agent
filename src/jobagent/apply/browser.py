"""Filling an application form in a real, visible browser, then stopping for you.

It never clicks Submit. It fills what it has approved answers for, attaches your CV, and
hands over: you check the page and submit it yourself. A field it can't fill (a changed
form, a custom widget) is named, not skipped silently, and left for you.
"""

import re
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
    submitted: bool = False  # clicked Submit and the site confirmed it
    screenshot: str = ""  # the filled form, saved just before submitting


def _css(value: str) -> str:
    """A value made safe inside a CSS attribute selector: [value="..."]."""
    return value.replace("\\", "\\\\").replace('"', '\\"')


def _upload_cv(page, selector: str, cv_path: Path | None, result: FillResult) -> None:
    if cv_path is None:
        return
    try:
        page.set_input_files(selector, str(cv_path))
        result.filled.append("Resume/CV")
    except Exception as e:  # noqa: BLE001 (any failure: named, left for you)
        result.failed.append(f"Resume/CV ({type(e).__name__})")


def fill_lever_form(
    page, questions: list[Question], answers: dict[str, str], cv_path: Path | None
) -> FillResult:
    """Put each answer in its field on an open Lever form. `answers` is by field name.

    Lever uses plain HTML fields, found by their name attribute. `page` is a Playwright
    Page (tests pass a fake with the same methods).
    """
    result = FillResult()
    _upload_cv(page, 'input[name="resume"]', cv_path, result)
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


def fill_greenhouse_form(
    page, questions: list[Question], answers: dict[str, str], cv_path: Path | None
) -> FillResult:
    """Put each answer in its field on an open Greenhouse form. `answers` is by field name.

    Each field's id is its API name. Choice questions are searchable dropdowns: click,
    type the answer, then click the option whose text is exactly the answer.
    """
    result = FillResult()
    _upload_cv(page, 'input[id="resume"]', cv_path, result)
    for q in questions:
        answer = answers.get(q.field)
        if not answer or q.kind == "file":
            continue
        selector = f'[id="{_css(q.field)}"]'
        try:
            if q.kind in ("dropdown", "multiple-select"):
                for part in answer.split(MULTI_SEPARATOR):
                    page.click(selector)
                    page.fill(selector, part)
                    page.get_by_role("option", name=part, exact=True).first.click()
            else:
                page.fill(selector, answer)
            result.filled.append(q.label)
        except Exception as e:  # noqa: BLE001
            result.failed.append(f"{q.label} ({type(e).__name__})")
    return result


def fill_ashby_form(
    page, questions: list[Question], answers: dict[str, str], cv_path: Path | None
) -> FillResult:
    """Put each answer in its field on an open Ashby form. `answers` is by field path.

    Every question sits in a container marked data-field-path="<path>", so each answer
    is placed inside its own question: two "Yes" buttons on one form can't be confused.
    """
    result = FillResult()
    _upload_cv(
        page,
        '[data-field-path="_systemfield_resume"] input[type="file"]',
        cv_path,
        result,
    )
    for q in questions:
        box = f'[data-field-path="{_css(q.field)}"]'
        answer = answers.get(q.field)
        if q.kind in ("file", "unsupported"):
            if q.required and q.field != "_systemfield_resume":
                result.failed.append(f"{q.label} (do this one on the page)")
            continue
        if not answer:
            continue
        try:
            if q.kind == "multiple-choice" and set(q.options) == {"Yes", "No"}:
                page.locator(box).get_by_role("button", name=answer, exact=True).click()
            elif q.kind in ("multiple-choice", "dropdown", "multiple-select"):
                for part in answer.split(MULTI_SEPARATOR):
                    page.locator(box).get_by_label(part, exact=True).check()
            elif q.kind == "location":
                # A search box: type your location, take the first suggestion, and say
                # which one it took (the only place a "nearest" match is allowed: it's
                # your own location, and you see it before anything is sent).
                page.locator(f"{box} input").fill(answer)
                option = page.get_by_role("option").first
                result.filled.append(f"{q.label}: {option.inner_text().strip()}")
                option.click()
                continue
            else:
                page.locator(f"{box} input, {box} textarea").first.fill(answer)
            result.filled.append(q.label)
        except Exception as e:  # noqa: BLE001
            result.failed.append(f"{q.label} ({type(e).__name__})")
    return result


FILLERS = {
    "lever": fill_lever_form,
    "greenhouse": fill_greenhouse_form,
    "ashby": fill_ashby_form,
}


# The site's own confirmation after Submit. Counted before and after the click, so text
# already on the page ("Thank you for your interest" in the ad) can't fake a success.
_CONFIRMED = (
    r"thank you for (applying|your application)|thanks for applying|"
    r"application (was |has been )?(successfully )?(submitted|received)|"
    r"we('ve| have) received your application"
)
_COUNT_JS = "p => (document.body.innerText.match(new RegExp(p, 'gi')) || []).length"
_SUBMIT_BUTTON = re.compile(r"^\s*submit( your)?( application)?\s*$", re.IGNORECASE)
SUBMIT_TIMEOUT_MS = 20_000

# Wait for these before filling: Ashby and Greenhouse build their forms in the browser.
_READY = {
    "lever": 'input[name="name"]',
    "greenhouse": '[id="first_name"]',
    "ashby": "[data-field-path]",
}


def submit_and_confirm(page) -> bool:
    """Click Submit, then wait for the site to confirm. False means it didn't (a
    CAPTCHA, a validation error, a slow site): the application may not have gone."""
    before = page.evaluate(_COUNT_JS, _CONFIRMED)
    page.get_by_role("button", name=_SUBMIT_BUTTON).first.click()
    try:
        page.wait_for_function(
            "([p, n]) => (document.body.innerText.match(new RegExp(p, 'gi')) || [])"
            ".length > n",
            arg=[_CONFIRMED, before],
            timeout=SUBMIT_TIMEOUT_MS,
        )
        return True
    except Exception:  # noqa: BLE001 (no confirmation: treated as not sent)
        return False


def open_and_fill(
    url: str,
    questions: list[Question],
    answers: dict[str, str],
    cv_path: Path | None,
    hand_over: Callable[[FillResult], bool],
    ats: str = "lever",
    screenshot: Path | None = None,
) -> bool:
    """Open the form in Chrome, fill it, then hand over to you; returns whether it was
    sent. With `screenshot` (submit mode) and nothing left for you, it also clicks
    Submit itself, after saving the filled form there as your record."""
    from playwright.sync_api import sync_playwright

    with sync_playwright() as pw:
        # Your installed Chrome, visible: you can watch, and take over if it stops.
        browser = pw.chromium.launch(channel="chrome", headless=False)
        try:
            page = browser.new_page()
            page.goto(url, wait_until="domcontentloaded")
            page.set_default_timeout(FIELD_TIMEOUT_MS)
            page.wait_for_selector(_READY[ats], timeout=SUBMIT_TIMEOUT_MS)
            result = FILLERS[ats](page, questions, answers, cv_path)
            if screenshot is not None and not result.failed:
                screenshot.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                page.screenshot(path=str(screenshot), full_page=True)
                result.screenshot = str(screenshot)
                result.submitted = submit_and_confirm(page)
                if not result.submitted:
                    result.failed.append(
                        "no confirmation after Submit (a CAPTCHA or an error on the "
                        "page?): check it, finish it yourself if needed"
                    )
            return hand_over(result)
        finally:
            browser.close()
