"""LLM classifier logic with a fake chat client: no model, no network, no real email."""

import json

from jobagent.core.categories import Category
from jobagent.inbox.classify import (
    MAX_BODY_CHARS,
    LLMClassifier,
    build_user_message,
    evidence_in_email,
)


class FakeChat:
    """Returns the queued replies in order; records what it was asked."""

    def __init__(self, *replies: str):
        self.replies = list(replies)
        self.calls = []

    def chat(self, **kwargs):
        self.calls.append(kwargs)
        return {"message": {"content": self.replies.pop(0)}}


def reply(category: str, quote: str) -> str:
    return json.dumps({"category": category, "evidence_quote": quote})


SUBJECT = "Next steps"
BODY = "Hi, we'd love to   schedule a call with you next week."


def test_valid_prediction_with_supported_evidence():
    clf = LLMClassifier(FakeChat(reply("interview_invite", "Schedule a call")), "m")
    assert clf(SUBJECT, BODY) == Category.INTERVIEW_INVITE
    assert clf.errors == 0
    assert clf.unsupported == 0


def test_invented_evidence_is_counted():
    clf = LLMClassifier(FakeChat(reply("offer", "pleased to offer you")), "m")
    assert clf(SUBJECT, BODY) == Category.OFFER
    assert clf.unsupported == 1


def test_invalid_output_retried_then_succeeds():
    fake = FakeChat("not json", reply("rejection", "schedule a call"))
    clf = LLMClassifier(fake, "m")
    assert clf(SUBJECT, BODY) == Category.REJECTION
    assert len(fake.calls) == 2
    assert clf.errors == 0


def test_unknown_category_is_invalid():
    fake = FakeChat(reply("maybe", "x"), reply("maybe", "x"))
    clf = LLMClassifier(fake, "m")
    assert clf(SUBJECT, BODY) == Category.OTHER  # counted as a miss...
    assert clf.errors == 1  # ...and reported


def test_request_is_deterministic_and_schema_constrained():
    fake = FakeChat(reply("other", "next week"))
    LLMClassifier(fake, "qwen3:8b")(SUBJECT, BODY)
    call = fake.calls[0]
    assert call["model"] == "qwen3:8b"
    assert call["options"]["temperature"] == 0
    assert call["format"]["properties"]["category"]  # JSON schema sent
    assert call["messages"][0]["role"] == "system"


def test_body_truncated():
    msg = build_user_message("S", "x" * (MAX_BODY_CHARS + 500))
    assert msg.count("x") == MAX_BODY_CHARS


def test_evidence_check_ignores_case_and_spacing():
    assert evidence_in_email("SCHEDULE a\ncall", SUBJECT, BODY)
    assert evidence_in_email("next steps", SUBJECT, BODY)  # subject counts too
    assert not evidence_in_email("", SUBJECT, BODY)
    assert not evidence_in_email("unfortunately", SUBJECT, BODY)
