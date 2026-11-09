"""Builders for fake Gmail API messages. Synthetic only: never put real emails in tests."""

import base64


def b64(text: str, charset: str = "utf-8", pad: bool = False) -> str:
    encoded = base64.urlsafe_b64encode(text.encode(charset)).decode()
    return encoded if pad else encoded.rstrip("=")  # Gmail often omits padding


def part(mime: str, text: str = "", *, charset="utf-8", filename="", parts=None):
    p = {
        "mimeType": mime,
        "filename": filename,
        "headers": [
            {"name": "Content-Type", "value": f"{mime}; charset={charset}"},
        ],
        "body": {"data": b64(text, charset)} if text else {"size": 0},
    }
    if parts is not None:
        p["parts"] = parts
    return p


def message(
    payload: dict,
    *,
    msg_id="m1",
    thread_id="t1",
    sender="Talent Team <no-reply@greenhouse.io>",
    subject="Thank you for applying",
    internal_date_ms=1_790_000_000_000,
) -> dict:
    payload = dict(payload)
    payload["headers"] = payload.get("headers", []) + [
        {"name": "From", "value": sender},
        {"name": "To", "value": "me@example.com"},
        {"name": "Subject", "value": subject},
    ]
    return {
        "id": msg_id,
        "threadId": thread_id,
        "internalDate": str(internal_date_ms),
        "payload": payload,
    }


class FakeGmail:
    """Mimics the chained client: service.users().messages().list(...).execute()."""

    def __init__(self, messages: list[dict], page_size: int = 2):
        self._messages = {m["id"]: m for m in messages}
        self._ids = [m["id"] for m in messages]
        self._page_size = page_size
        self.get_calls = 0
        self.last_query = None

    def users(self):
        return self

    def messages(self):
        return self

    def list(self, userId, q, pageToken=None, maxResults=500):
        self.last_query = q
        start = int(pageToken or 0)
        page = self._ids[start : start + self._page_size]
        resp = {"messages": [{"id": i} for i in page]} if page else {}
        if start + self._page_size < len(self._ids):
            resp["nextPageToken"] = str(start + self._page_size)
        return _Exec(resp)

    def get(self, userId, id, format):
        self.get_calls += 1
        return _Exec(self._messages[id])


class _Exec:
    def __init__(self, value):
        self._value = value

    def execute(self):
        return self._value
