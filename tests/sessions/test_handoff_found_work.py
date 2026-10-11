"""Found-work entries on set_handoff and the gate their consumption re-arms."""

from __future__ import annotations

import re
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock, patch
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
from gobby.storage.agents import LocalAgentRunManager
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.projects import LocalProjectManager
from gobby.storage.session_models import Session
from gobby.storage.session_tasks import SessionTaskManager
from gobby.storage.sessions import SessionManager
from gobby.storage.tasks import LocalTaskManager, Task
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
@pytest.mark.parametrize("tool_name", ["set_handoff", "feedback"])
@pytest.mark.parametrize(
    "owner,action,accepted",
    [
        pytest.param("caller", "claimed", True, id="former-caller-claim"),
        pytest.param("descendant", "claimed", True, id="former-descendant-claim"),
        pytest.param("unrelated", "claimed", False, id="unrelated-claim"),
        pytest.param("caller", "worked_on", False, id="worked-on-only"),
        pytest.param("caller", "created", False, id="created-only"),
    ],
)
async def test_fixed_disposition_uses_historical_claims_after_creator_closes(
    temp_db: HubDatabase,
    session_manager: SessionManager,
    tool_name: str,
    owner: str,
    action: str,
    accepted: bool,
) -> None:
    project_id = _registered_session(session_manager).project_id
    assert project_id is not None
    caller = session_manager.register(
        external_id="historical-claim-caller",
        machine_id=MACHINE_ID,
        source="codex",
        project_id=project_id,
        session_type="web_chat",
    )
    creator = session_manager.register(
        external_id="historical-claim-creator",
        machine_id=MACHINE_ID,
        source="codex",
        project_id=project_id,
    )
    claimant = caller
    if owner != "caller":
        claimant = session_manager.register(
            external_id=f"historical-claim-{owner}",
            machine_id=MACHINE_ID,
            source="codex",
            project_id=project_id,
        )
    if owner == "descendant":
        LocalAgentRunManager(temp_db).create(
            parent_session_id=caller.id,
            child_session_id=claimant.id,
            provider="codex",
            prompt="Fix the discovered bug.",
        )
    task_manager = LocalTaskManager(temp_db)
    task = task_manager.create_task(
        project_id=project_id,
        title="Fixed finding",
        created_in_session_id=creator.id,
        validation_criteria="The finding is fixed and its regression test passes.",
    )
    if action == "claimed":
        task_manager.claim_task(task.id, claimant.id)
        task_manager.release_task_claim(task.id)
    else:
        SessionTaskManager(temp_db).link_task(claimant.id, task.id, action=action)
    closed = task_manager.close_task(task.id, closed_in_session_id=creator.id)
    assert closed.claimed_by_session_id is None
    assert closed.closed_in_session_id == creator.id

    compact = AsyncMock(return_value={"compacted": True})
    chat_registry = SimpleNamespace(
        find_session=lambda _session_id: (None, None), compact_session=compact
    )
    registry = create_session_messages_registry(
        session_manager=session_manager,
        task_manager=task_manager,
        db=temp_db,
        web_chat_session_registry=chat_registry,
    )
    task_ref = f"#{task.seq_num}"
    with session_context_for_test(caller.id):
        if tool_name == "set_handoff":
            result = await registry.call(
                tool_name,
                {
                    "current_state": "Fixed the finding.",
                    "next_steps": ["Continue."],
                    "found_work": [dict(FIXED, ref=task_ref)],
                },
            )
        else:
            result = await registry.call(
                tool_name,
                {
                    "observations": [
                        {
                            "source": "gobby-sessions:set_handoff",
                            "kind": "bug",
                            "evidence": f"Fixed the finding in {task_ref}.",
                            "impact": "Handoff can continue.",
                            "frequency": "once",
                            "disposition": "fixed",
                        }
                    ]
                },
            )
    staged = temp_db.fetchone(
        "SELECT rendered_markdown FROM session_handoffs WHERE session_id = %s", (caller.id,)
    )
    if not accepted:
        assert result["success"] is False
        assert result["error_code"] == (
            "invalid_handoff" if tool_name == "set_handoff" else "invalid_feedback"
        )
        assert "'fixed' requires a task claimed or closed" in result["error"]
        assert staged is None
        compact.assert_not_awaited()
    elif tool_name == "set_handoff":
        assert result.get("compacted") is True, result
        assert result["handoff_staged"] is True
        assert staged is not None
        assert task_ref in staged["rendered_markdown"]
        compact.assert_awaited_once()
    else:
        assert result["success"] is True, result
        assert result["created"] == 1


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
        delivery_mode="in_process",
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
