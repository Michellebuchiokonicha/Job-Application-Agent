"""Turn a raw Gmail API message into clean fields. Pure: no network, easy to test.

Gmail returns emails as a tree of MIME parts, e.g.

    multipart/mixed
    ├── multipart/alternative
    │   ├── text/plain   <- what we want
    │   └── text/html    <- fallback if there is no plain text
    └── application/pdf  <- attachment, skipped

Each part's content is base64url-encoded in part["body"]["data"].
"""

import base64
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from email.message import Message
from email.utils import parseaddr

from bs4 import BeautifulSoup


@dataclass(frozen=True)
class ParsedEmail:
    gmail_message_id: str
    thread_id: str
    from_name: str
    from_addr: str
    to_addr: str
    subject: str
    received_at: str  # ISO 8601, UTC
    body_text: str


def _headers(payload: dict) -> dict[str, str]:
    # Header names are case-insensitive in email; normalise to lower case.
    return {h["name"].lower(): h["value"] for h in payload.get("headers", [])}


def _charset(part: dict) -> str:
    msg = Message()
    msg["Content-Type"] = _headers(part).get("content-type", "text/plain")
    return msg.get_content_charset() or "utf-8"


def _decode(data: str, charset: str) -> str:
    # base64url, and Gmail sometimes omits the "=" padding.
    raw = base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))
    try:
        return raw.decode(charset, errors="replace")
    except LookupError:  # unknown charset name in the header
        return raw.decode("utf-8", errors="replace")


def _collect(part: dict, found: dict[str, list[str]]) -> None:
    """Depth-first walk of the MIME tree, collecting text/plain and text/html bodies."""
    if part.get("filename"):  # attachments have a filename; skip them
        return
    mime = part.get("mimeType", "")
    data = part.get("body", {}).get("data")
    if mime in ("text/plain", "text/html") and data:
        found[mime].append(_decode(data, _charset(part)))
    for child in part.get("parts", []):
        _collect(child, found)


def html_to_text(html: str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "head"]):
        tag.decompose()
    return soup.get_text(separator="\n")


def _tidy(text: str) -> str:
    text = text.replace("\r\n", "\n").replace("\xa0", " ")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n\s*\n+", "\n\n", text)  # collapse runs of blank lines
    return text.strip()


def extract_body(payload: dict) -> str:
    found: dict[str, list[str]] = {"text/plain": [], "text/html": []}
    _collect(payload, found)
    if found["text/plain"]:
        return _tidy("\n\n".join(found["text/plain"]))
    if found["text/html"]:
        return _tidy("\n\n".join(html_to_text(h) for h in found["text/html"]))
    return ""


def parse_message(message: dict) -> ParsedEmail:
    payload = message.get("payload", {})
    h = _headers(payload)
    from_name, from_addr = parseaddr(h.get("from", ""))
    # internalDate (ms since epoch) is when Gmail received it: more reliable than
    # the Date header, which the sender sets and is sometimes wrong or missing.
    received = datetime.fromtimestamp(int(message["internalDate"]) / 1000, tz=UTC)
    return ParsedEmail(
        gmail_message_id=message["id"],
        thread_id=message["threadId"],
        from_name=from_name,
        from_addr=from_addr.lower(),
        to_addr=h.get("to", ""),
        subject=h.get("subject", ""),
        received_at=received.isoformat(),
        body_text=extract_body(payload),
    )
