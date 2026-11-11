"""Email categories: what a single email *is* (see docs/state-machine.md, section 1)."""

from enum import StrEnum


class Category(StrEnum):
    OFFER = "offer"
    INTERVIEW_INVITE = "interview_invite"
    ASSESSMENT = "assessment"
    REJECTION = "rejection"
    RECRUITER_MESSAGE = "recruiter_message"
    CONFIRMATION = "confirmation"
    OTHER = "other"


# Most expensive to miss first. Used when an email fits several categories.
PRIORITY: tuple[Category, ...] = tuple(Category)
