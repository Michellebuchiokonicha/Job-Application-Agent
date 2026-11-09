"""Application state machine: statuses and the rules for moving between them.

This module is pure: no database, no I/O. It only answers "is this move allowed?".
The code that actually writes a status change to the DB lives in store.py and
calls check_transition() first. See docs/state-machine.md for the design.
"""

from enum import StrEnum


class Status(StrEnum):
    # Phase 4: outbound pipeline (unused in Phase 1)
    DISCOVERED = "discovered"
    MATCHED = "matched"
    QUEUED_FOR_REVIEW = "queued_for_review"
    # Active
    SUBMITTED = "submitted"
    ACKNOWLEDGED = "acknowledged"
    ASSESSMENT = "assessment"
    INTERVIEW = "interview"
    OFFER = "offer"
    # Terminal
    REJECTED = "rejected"
    WITHDRAWN = "withdrawn"
    ACCEPTED = "accepted"
    DECLINED = "declined"
    SKIPPED = "skipped"


S = Status  # short alias to keep the table below readable

ALLOWED: dict[Status, frozenset[Status]] = {
    S.DISCOVERED: frozenset({S.MATCHED, S.SKIPPED}),
    S.MATCHED: frozenset({S.QUEUED_FOR_REVIEW, S.SKIPPED}),
    S.QUEUED_FOR_REVIEW: frozenset({S.SUBMITTED, S.SKIPPED}),
    S.SUBMITTED: frozenset(
        {S.ACKNOWLEDGED, S.ASSESSMENT, S.INTERVIEW, S.OFFER, S.REJECTED, S.WITHDRAWN}
    ),
    S.ACKNOWLEDGED: frozenset(
        {S.ASSESSMENT, S.INTERVIEW, S.OFFER, S.REJECTED, S.WITHDRAWN}
    ),
    S.ASSESSMENT: frozenset(
        {S.ASSESSMENT, S.INTERVIEW, S.OFFER, S.REJECTED, S.WITHDRAWN}
    ),
    S.INTERVIEW: frozenset(
        {S.INTERVIEW, S.ASSESSMENT, S.OFFER, S.REJECTED, S.WITHDRAWN}
    ),
    S.OFFER: frozenset({S.ACCEPTED, S.DECLINED, S.REJECTED}),
    # Terminal: no way out except a logged manual override (handled in store.py).
    S.REJECTED: frozenset(),
    S.WITHDRAWN: frozenset(),
    S.ACCEPTED: frozenset(),
    S.DECLINED: frozenset(),
    S.SKIPPED: frozenset(),
}

TERMINAL: frozenset[Status] = frozenset(s for s, nxt in ALLOWED.items() if not nxt)

# Statuses an application may be *created* at, from the first email we see for it
# (applications made outside this agent, e.g. via Sorce, have no record yet).
BOOTSTRAP_STATUSES: frozenset[Status] = frozenset(
    {S.ACKNOWLEDGED, S.ASSESSMENT, S.INTERVIEW, S.OFFER, S.REJECTED}
)

# Every status an application may be created at: email bootstrap, plus
# discovered (Phase 4 job feed) and submitted (our applier / manual entry).
INITIAL_STATUSES: frozenset[Status] = BOOTSTRAP_STATUSES | {S.DISCOVERED, S.SUBMITTED}


class InvalidTransition(Exception):
    """Raised when a status change is not allowed by ALLOWED."""

    def __init__(self, current: Status, new: Status) -> None:
        self.current = current
        self.new = new
        super().__init__(f"Cannot move application from '{current}' to '{new}'")


def can_transition(current: Status, new: Status) -> bool:
    return new in ALLOWED[current]


def check_transition(current: Status, new: Status) -> None:
    """Return None if the move is allowed; raise InvalidTransition otherwise."""
    if not can_transition(current, new):
        raise InvalidTransition(current, new)
