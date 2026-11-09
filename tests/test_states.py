import pytest

from jobagent.core.states import (
    ALLOWED,
    BOOTSTRAP_STATUSES,
    TERMINAL,
    InvalidTransition,
    Status,
    check_transition,
)

ALLOWED_PAIRS = [(cur, new) for cur, nxts in ALLOWED.items() for new in nxts]
FORBIDDEN_PAIRS = [
    (cur, new) for cur in Status for new in Status if new not in ALLOWED[cur]
]


def test_every_status_has_rules():
    # Guards against adding a Status and forgetting to put it in the table.
    assert set(ALLOWED) == set(Status)


@pytest.mark.parametrize(("current", "new"), ALLOWED_PAIRS)
def test_allowed_transitions_pass(current, new):
    check_transition(current, new)  # must not raise


@pytest.mark.parametrize(("current", "new"), FORBIDDEN_PAIRS)
def test_forbidden_transitions_raise(current, new):
    with pytest.raises(InvalidTransition) as exc:
        check_transition(current, new)
    assert exc.value.current == current
    assert exc.value.new == new


def test_terminal_statuses():
    assert TERMINAL == {
        Status.REJECTED,
        Status.WITHDRAWN,
        Status.ACCEPTED,
        Status.DECLINED,
        Status.SKIPPED,
    }


@pytest.mark.parametrize("status", sorted(TERMINAL))
def test_nothing_leaves_a_terminal_status(status):
    for new in Status:
        with pytest.raises(InvalidTransition):
            check_transition(status, new)


def test_bootstrap_statuses_are_reachable_real_states():
    # You can't be created at a pipeline-only or "you decided" status.
    assert Status.WITHDRAWN not in BOOTSTRAP_STATUSES
    assert Status.DISCOVERED not in BOOTSTRAP_STATUSES
    assert Status.ACKNOWLEDGED in BOOTSTRAP_STATUSES


def test_no_going_backwards_from_interview():
    with pytest.raises(InvalidTransition):
        check_transition(Status.INTERVIEW, Status.ACKNOWLEDGED)


def test_status_is_a_plain_string():
    # StrEnum: values compare equal to str, so they store and load cleanly in SQLite.
    assert Status.INTERVIEW == "interview"
    assert Status("interview") is Status.INTERVIEW
