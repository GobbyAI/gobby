"""Found-work entries on set_handoff and the gate their consumption re-arms."""

from __future__ import annotations

import re
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import cast
from unittest.mock import patch
from uuid import uuid4

import pytest

from gobby.mcp_proxy.tools.sessions import create_session_messages_registry
from gobby.sessions.clear_continuation import refresh_clear_attempt_content, stage_clear_attempt
from gobby.sessions.handoff import (
    FOUND_WORK_VARIABLE,
    HANDOFF_PULL_PENDING_VARIABLE,
    consume_pending_handoff,
    normalize_found_work,
    restore_handoff_attempt,
    stage_handoff_attempt,
)
from gobby.sessions.handoff_records import (
    FoundWorkEntry,
    HandoffPayload,
    build_handoff_payload,
    latest_delivered_clear_handoff,
    record_handoff_delivery,
)
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.projects import LocalProjectManager
from gobby.storage.session_models import Session
from gobby.storage.sessions import SessionManager
from gobby.storage.tasks import Task
from gobby.utils.session_context import session_context_for_test
from gobby.workflows.found_work_gate import (
    FOUND_WORK_GATE_ARMED_AT_VARIABLE,
    found_work_gate_armed_at,
)
from gobby.workflows.state_manager import SessionVariableManager
from tests.fixtures.isolated_checkout import write_project_marker

pytestmark = pytest.mark.unit

MACHINE_ID = "20000000-0000-4000-8000-000000000002"
SESSION_ID = "session-current"
ESCALATED: dict[str, str] = {
    "finding": "terminal_list blocks the event loop",
    "disposition": "escalated",
    "ref": "gobby#13822",
}
FIXED: dict[str, str] = {
    "finding": "the clear dialog's Cancel button did nothing",
    "disposition": "fixed",
    "ref": "#22543",
}


@pytest.fixture(autouse=True)
def _local_machine_identity() -> Iterator[None]:
    with patch("gobby.utils.machine_id._cached_machine_id", MACHINE_ID):
        yield


@pytest.fixture
def session_manager(temp_db: HubDatabase, tmp_path: Path) -> SessionManager:
    checkout = tmp_path / "found-work-test"
    checkout.mkdir()
    project_id = str(uuid4())
    write_project_marker(checkout, project_id=project_id, name="found-work-test")
    project = LocalProjectManager(temp_db).create(
        name="found-work-test", repo_path=str(checkout), project_id=project_id
    )
    manager = SessionManager(temp_db)
    manager.register_session(
        external_id="found-work-session",
        machine_id=MACHINE_ID,
        source="codex",
        project_id=project.id,
    )
    return manager


def _registered_session(manager: SessionManager) -> Session:
    row = manager.db.fetchone(
        "SELECT id FROM sessions WHERE external_id = %s",
        ("found-work-session",),
    )
    assert row is not None
    session = manager.get(str(row["id"]))
    assert session is not None
    return session


def _ladder_task(**overrides: object) -> Task:
    values: dict[str, object] = {
        "created_in_session_id": SESSION_ID,
        "claimed_by_session_id": None,
        "closed_in_session_id": None,
        "closed_at": None,
        "labels": [],
    }
    values.update(overrides)
    return cast(Task, SimpleNamespace(**values))


def _payload(*entries: dict[str, str], notes: list[str] | None = None) -> HandoffPayload:
    return build_handoff_payload(
        current_state="Ready.",
        next_steps=["Continue."],
        notes=notes or (),
        found_work=[FoundWorkEntry(**entry) for entry in entries],
    )


def _register_clear_successor(manager: SessionManager, predecessor: Session) -> str:
    return manager.register_session(
        external_id="found-work-successor",
        machine_id=MACHINE_ID,
        source="codex",
        project_id=predecessor.project_id,
        parent_session_id=predecessor.id,
    )


@pytest.mark.asyncio
async def test_set_handoff_rejects_an_undispositioned_finding(
    temp_db: HubDatabase,
    session_manager: SessionManager,
) -> None:
    session = _registered_session(session_manager)
    registry = create_session_messages_registry(session_manager=session_manager, db=temp_db)

    with session_context_for_test(session.id):
        result = await registry.call(
            "set_handoff",
            {
                "current_state": "Ready.",
                "next_steps": ["Continue."],
                "found_work": [{"finding": "capture_output drops the last line", "ref": "#1"}],
            },
        )

    assert result["success"] is False
    assert result["error_code"] == "invalid_handoff"
    assert (
        "found_work[0].disposition must be one of fixed, escalated, filed-task" in (result["error"])
    )
    staged = temp_db.fetchone("SELECT 1 FROM session_handoffs WHERE session_id = %s", (session.id,))
    assert staged is None


@pytest.mark.asyncio
async def test_get_handoff_rearms_the_gate_for_open_findings(
    temp_db: HubDatabase,
    session_manager: SessionManager,
) -> None:
    session = _registered_session(session_manager)
    stage_handoff_attempt(
        temp_db,
        session.id,
        attempt_id="a" * 32,
        handoff=_payload(ESCALATED),
        clear_session=False,
    )
    sv_mgr = SessionVariableManager(temp_db)
    assert FOUND_WORK_GATE_ARMED_AT_VARIABLE not in sv_mgr.get_variables(session.id)
    registry = create_session_messages_registry(session_manager=session_manager, db=temp_db)

    with session_context_for_test(session.id):
        result = await registry.call("get_handoff", {})

    assert result["found"] is True
    assert result["found_work"] == [ESCALATED]
    assert result["found_work_gate_armed"] is True
    assert (
        "- Found work: terminal_list blocks the event loop (escalated gobby#13822)"
        in result["handoff"]
    )
    variables = sv_mgr.get_variables(session.id)
    assert found_work_gate_armed_at(variables.get(FOUND_WORK_GATE_ARMED_AT_VARIABLE)) is not None
    assert FOUND_WORK_VARIABLE not in variables


def test_clear_successor_is_armed_by_open_findings(
    temp_db: HubDatabase,
    session_manager: SessionManager,
) -> None:
    predecessor = _registered_session(session_manager)
    state = stage_handoff_attempt(
        temp_db,
        predecessor.id,
        attempt_id="b" * 32,
        handoff=_payload(FIXED, ESCALATED),
        clear_session=True,
    )
    successor_id = _register_clear_successor(session_manager, predecessor)
    record_handoff_delivery(
        temp_db,
        handoff_id=state.handoff_record_id,
        attempt_id="b" * 32,
        boundary_kind="clear",
        continuation_session_id=successor_id,
    )
    sv_mgr = SessionVariableManager(temp_db)
    sv_mgr.merge_variables(successor_id, {HANDOFF_PULL_PENDING_VARIABLE: True})

    consumed = consume_pending_handoff(temp_db, successor_id)

    assert consumed is not None
    assert consumed.open_found_work == (FoundWorkEntry(**ESCALATED),)
    successor_variables = sv_mgr.get_variables(successor_id)
    assert HANDOFF_PULL_PENDING_VARIABLE not in successor_variables
    armed_at = successor_variables.get(FOUND_WORK_GATE_ARMED_AT_VARIABLE)
    assert found_work_gate_armed_at(armed_at) is not None
    assert FOUND_WORK_GATE_ARMED_AT_VARIABLE not in sv_mgr.get_variables(predecessor.id)


def test_fixed_findings_do_not_arm_the_gate(
    temp_db: HubDatabase,
    session_manager: SessionManager,
) -> None:
    session = _registered_session(session_manager)
    stage_handoff_attempt(
        temp_db,
        session.id,
        attempt_id="c" * 32,
        handoff=_payload(FIXED),
        clear_session=False,
    )

    consumed = consume_pending_handoff(temp_db, session.id)

    assert consumed is not None
    assert consumed.found_work == (FoundWorkEntry(**FIXED),)
    assert consumed.open_found_work == ()
    variables = SessionVariableManager(temp_db).get_variables(session.id)
    assert FOUND_WORK_GATE_ARMED_AT_VARIABLE not in variables
    assert FOUND_WORK_VARIABLE not in variables


def test_failed_attempt_restore_drops_the_found_work_marker(
    temp_db: HubDatabase,
    session_manager: SessionManager,
) -> None:
    session = _registered_session(session_manager)
    state = stage_handoff_attempt(
        temp_db,
        session.id,
        attempt_id="d" * 32,
        handoff=_payload(ESCALATED),
        clear_session=False,
    )
    sv_mgr = SessionVariableManager(temp_db)
    assert sv_mgr.get_variables(session.id)[FOUND_WORK_VARIABLE] == [ESCALATED]

    assert restore_handoff_attempt(temp_db, state) is True

    assert FOUND_WORK_VARIABLE not in sv_mgr.get_variables(session.id)
    assert consume_pending_handoff(temp_db, session.id) is None


def test_refreshed_clear_attempt_replaces_the_found_work_marker(
    temp_db: HubDatabase,
    session_manager: SessionManager,
) -> None:
    session = _registered_session(session_manager)
    stage_clear_attempt(
        temp_db,
        session.id,
        attempt_id="e" * 32,
        handoff=_payload(ESCALATED),
        terminal_context=None,
        chat_context=None,
    )
    sv_mgr = SessionVariableManager(temp_db)
    assert sv_mgr.get_variables(session.id)[FOUND_WORK_VARIABLE] == [ESCALATED]

    refreshed = refresh_clear_attempt_content(
        temp_db, session.id, attempt_id="e" * 32, handoff=_payload(FIXED)
    )

    assert refreshed is True
    assert sv_mgr.get_variables(session.id)[FOUND_WORK_VARIABLE] == [FIXED]


@pytest.mark.parametrize(
    ("entry", "task", "expected"),
    [
        pytest.param(
            {"finding": "x", "ref": "#1"},
            None,
            "found_work[0].disposition must be one of fixed, escalated, filed-task",
            id="missing-disposition",
        ),
        pytest.param(
            {"finding": "x", "disposition": "noted", "ref": "#1"},
            None,
            "found_work[0].disposition must be one of fixed, escalated, filed-task",
            id="feedback-only-disposition",
        ),
        pytest.param(
            {"finding": "x", "disposition": "fixed"},
            None,
            "found_work[0].ref must be a nonblank string",
            id="missing-ref",
        ),
        pytest.param(
            {"finding": " ", "disposition": "fixed", "ref": "#1"},
            None,
            "found_work[0].finding must be a nonblank string",
            id="blank-finding",
        ),
        pytest.param(
            {"finding": "x", "disposition": "fixed", "ref": "gobby#13822"},
            None,
            "'fixed' requires a #N task ref as ref",
            id="fixed-needs-task-ref",
        ),
        pytest.param(
            {"finding": "x", "disposition": "escalated", "ref": "the owner"},
            None,
            "'escalated' requires the active owner session ref (#N, UUID, or <project>#N) as ref",
            id="escalated-needs-session-ref",
        ),
        pytest.param(
            {"finding": "x", "disposition": "fixed", "ref": "#1"},
            None,
            "#1 could not be resolved",
            id="fixed-unresolved-task",
        ),
        pytest.param(
            {"finding": "x", "disposition": "fixed", "ref": "#1"},
            _ladder_task(claimed_by_session_id="session-other"),
            "'fixed' requires a task claimed or closed by this session",
            id="fixed-foreign-owner",
        ),
        pytest.param(
            {"finding": "x", "disposition": "filed-task", "ref": "#1"},
            _ladder_task(),
            "'filed-task' is rung 3 only",
            id="filed-task-unlabeled",
        ),
        pytest.param(
            {"finding": "x", "disposition": "filed-task", "ref": "#1"},
            _ladder_task(created_in_session_id="session-other", labels=["needs-decision"]),
            "'filed-task' is rung 3 only",
            id="filed-task-foreign-author",
        ),
    ],
)
def test_found_work_dispositions_follow_the_ladder(
    entry: dict[str, str], task: Task | None, expected: str
) -> None:
    with pytest.raises(ValueError, match=re.escape(expected)):
        normalize_found_work([entry], resolve_task=lambda _ref: task, session_id=SESSION_ID)


@pytest.mark.parametrize(
    ("entry", "task"),
    [
        pytest.param(FIXED, _ladder_task(claimed_by_session_id=SESSION_ID), id="fixed-claimed"),
        pytest.param(
            FIXED, _ladder_task(closed_in_session_id="session-child"), id="fixed-by-descendant"
        ),
        pytest.param(
            {"finding": "x", "disposition": "filed-task", "ref": "#7"},
            _ladder_task(labels=["clean-window"]),
            id="filed-task-rung-three",
        ),
        pytest.param(ESCALATED, None, id="escalated-owner-ref"),
    ],
)
def test_found_work_dispositions_accept_ladder_proof(
    entry: dict[str, str], task: Task | None
) -> None:
    [normalized] = normalize_found_work(
        [entry],
        resolve_task=lambda _ref: task,
        session_id=SESSION_ID,
        descendant_session_ids=("session-child",),
    )

    assert normalized == FoundWorkEntry(**entry)


def test_found_work_renders_as_notes_and_round_trips(
    temp_db: HubDatabase,
    session_manager: SessionManager,
) -> None:
    predecessor = _registered_session(session_manager)
    handoff = _payload(FIXED, ESCALATED, notes=["Daemon restarted at 03:45 UTC."])
    assert handoff.notes == (
        "Found work: the clear dialog's Cancel button did nothing (fixed #22543)",
        "Found work: terminal_list blocks the event loop (escalated gobby#13822)",
        "Daemon restarted at 03:45 UTC.",
    )
    state = stage_handoff_attempt(
        temp_db,
        predecessor.id,
        attempt_id="f" * 32,
        handoff=handoff,
        clear_session=True,
    )
    record_handoff_delivery(
        temp_db,
        handoff_id=state.handoff_record_id,
        attempt_id="f" * 32,
        boundary_kind="clear",
        continuation_session_id=_register_clear_successor(session_manager, predecessor),
    )

    delivered = latest_delivered_clear_handoff(temp_db, predecessor.id)

    assert delivered is not None
    assert delivered.payload.rendered_markdown == handoff.rendered_markdown
    assert delivered.payload.notes == handoff.notes
    assert delivered.payload.found_work == ()
