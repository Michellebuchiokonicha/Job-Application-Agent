from gmail_fakes import message, part

from jobagent.gmail.parse import parse_message


def test_simple_plain_text_and_headers():
    msg = message(part("text/plain", "Thanks for applying to Acme."))
    p = parse_message(msg)

    assert p.gmail_message_id == "m1"
    assert p.thread_id == "t1"
    assert p.from_name == "Talent Team"
    assert p.from_addr == "no-reply@greenhouse.io"
    assert p.subject == "Thank you for applying"
    assert p.body_text == "Thanks for applying to Acme."
    assert p.received_at.endswith("+00:00")  # stored in UTC


def test_prefers_plain_over_html():
    msg = message(
        part(
            "multipart/alternative",
            parts=[
                part("text/plain", "plain version"),
                part("text/html", "<p>html version</p>"),
            ],
        )
    )
    assert parse_message(msg).body_text == "plain version"


def test_html_only_is_converted_to_text():
    html = (
        "<html><head><style>p{color:red}</style></head>"
        "<body><p>We'd like to invite you</p><p>to an interview.</p></body></html>"
    )
    body = parse_message(message(part("text/html", html))).body_text
    assert "We'd like to invite you" in body
    assert "interview" in body
    assert "color" not in body  # <style> content dropped
    assert "<p>" not in body


def test_nested_parts_and_attachments_skipped():
    msg = message(
        part(
            "multipart/mixed",
            parts=[
                part(
                    "multipart/alternative",
                    parts=[part("text/plain", "Your assessment link is below.")],
                ),
                part("text/plain", "SECRET CV TEXT", filename="cv.txt"),
            ],
        )
    )
    body = parse_message(msg).body_text
    assert body == "Your assessment link is below."


def test_non_utf8_charset():
    msg = message(part("text/plain", "Café interview", charset="latin-1"))
    assert parse_message(msg).body_text == "Café interview"


def test_missing_subject_and_empty_body():
    msg = message({"mimeType": "multipart/mixed", "parts": []}, subject="")
    p = parse_message(msg)
    assert p.subject == ""
    assert p.body_text == ""


def test_whitespace_is_tidied():
    msg = message(part("text/plain", "Hello\r\n\r\n\r\n\r\nWorld   and\xa0more"))
    assert parse_message(msg).body_text == "Hello\n\nWorld and more"
