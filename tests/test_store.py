import sqlite3

import pytest

from jobagent.core.states import InvalidTransition, Status
from jobagent.core.store import (
    ApplicationNotFound,
    Source,
    connect,
    create_application,
    get_application,
    history,
    init_db,
    list_applications,
    transition,
)


@pytest.fixture
def conn():
    # A fresh in-memory database per test: fast, isolated, nothing left on disk.
    c = connect(":memory:")
    init_db(c)
    yield c
    c.close()


def make_app(conn, status=Status.ACKNOWLEDGED, **kw):
    return create_application(
        conn, company="Acme", role="ML Engineer", status=status, reason="test", **kw
    )


def test_create_logs_creation_as_change_from_null(conn):
    app_id = make_app(conn, event_id="msg-1", location="London")

    app = get_application(conn, app_id)
    assert app["status"] == Status.ACKNOWLEDGED
    assert app["source"] == Source.UNKNOWN
    assert app["location"] == "London"

    [change] = history(conn, app_id)
    assert change["from_status"] is None
    assert change["to_status"] == Status.ACKNOWLEDGED
    assert change["event_id"] == "msg-1"


def test_create_is_idempotent_per_event(conn):
    first = make_app(conn, event_id="msg-1")
    second = make_app(conn, event_id="msg-1")
    assert first == second
    assert len(list_applications(conn)) == 1


def test_cannot_create_at_non_initial_status(conn):
    with pytest.raises(ValueError):
        make_app(conn, status=Status.WITHDRAWN)


def test_valid_transition_updates_status_and_history(conn):
    app_id = make_app(conn)

    moved = transition(conn, app_id, Status.INTERVIEW, "invite", event_id="msg-2")

    assert moved is True
    assert get_application(conn, app_id)["status"] == Status.INTERVIEW
    last = history(conn, app_id)[-1]
    assert (last["from_status"], last["to_status"]) == (
        Status.ACKNOWLEDGED,
        Status.INTERVIEW,
    )


def test_invalid_transition_raises_and_changes_nothing(conn):
    app_id = make_app(conn, status=Status.REJECTED)

    with pytest.raises(InvalidTransition):
        transition(conn, app_id, Status.INTERVIEW, "invite", event_id="msg-2")

    assert get_application(conn, app_id)["status"] == Status.REJECTED
    assert len(history(conn, app_id)) == 1  # only the creation row


def test_same_event_twice_moves_once(conn):
    app_id = make_app(conn)

    assert transition(conn, app_id, Status.INTERVIEW, "invite", event_id="msg-2")
    assert not transition(conn, app_id, Status.INTERVIEW, "invite", event_id="msg-2")

    assert len(history(conn, app_id)) == 2


def test_repeat_rounds_with_different_events(conn):
    app_id = make_app(conn, status=Status.INTERVIEW)
    assert transition(conn, app_id, Status.INTERVIEW, "round 2", event_id="msg-3")
    assert len(history(conn, app_id)) == 2


def test_override_can_leave_terminal_and_is_logged(conn):
    app_id = make_app(conn, status=Status.REJECTED)

    transition(conn, app_id, Status.INTERVIEW, "manual reopen", override=True)

    assert get_application(conn, app_id)["status"] == Status.INTERVIEW
    assert history(conn, app_id)[-1]["is_override"] == 1


def test_unknown_application(conn):
    with pytest.raises(ApplicationNotFound):
        transition(conn, 999, Status.REJECTED, "x")


def test_db_rejects_bad_status_even_bypassing_python(conn):
    # Defence in depth: the CHECK constraint catches bugs that skip our functions.
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO applications (company, role, status, source, created_at, "
            "updated_at) VALUES ('A', 'B', 'interveiw', 'unknown', 'now', 'now')"
        )


def test_list_filters_by_status(conn):
    make_app(conn, status=Status.ACKNOWLEDGED)
    make_app(conn, status=Status.REJECTED)
    assert len(list_applications(conn, Status.REJECTED)) == 1
    assert len(list_applications(conn)) == 2
