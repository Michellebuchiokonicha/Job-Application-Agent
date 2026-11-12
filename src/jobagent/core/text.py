"""Text cleanup shared by the labelling tool and (later) the classifier."""

import html
import re
from urllib.parse import urlsplit

# Brackets excluded: emails often wrap links as [https://...], and a stray "[" in
# the host makes urlsplit think it's an IPv6 address and raise.
_URL = re.compile(r"https?://[^\s<>()\[\]\"']+")
# Long tokens with no spaces (base64-ish tracking IDs that survive outside a URL).
_JUNK_TOKEN = re.compile(r"\S{80,}")


def _short_link(match: re.Match[str]) -> str:
    try:
        host = urlsplit(match.group()).hostname or "link"
    except ValueError:  # malformed URL in someone's email: show it, never crash
        return "[link]"
    return f"[link: {host.removeprefix('www.')}]"


def clean_for_reading(text: str) -> str:
    """Decode HTML entities and collapse URLs to their domain.

    For humans and models reading emails: keeps what the link points to (the domain
    is often a strong signal, e.g. hackerrank.com, calendly.com) and drops the
    tracking noise. Never use this for anything that needs the real URL.
    """
    text = html.unescape(text)  # &nbsp; &apos; &amp; -> real characters
    text = text.replace("\xa0", " ")  # non-breaking space from &nbsp;
    text = _URL.sub(_short_link, text)
    return _JUNK_TOKEN.sub("[…]", text)
