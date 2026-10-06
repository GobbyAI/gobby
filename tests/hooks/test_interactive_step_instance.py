"""Interactive definition activation owns the same step snapshots as spawned seats."""

import asyncio
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from threading import Event, Thread
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import patch

import pytest

from gobby.hooks.event_handlers import EventHandlers
from gobby.hooks.events import HookEvent, HookEventType, SessionSource
from gobby.hooks.session_activation import (
    ActivationReconciliationResult,
    _ensure_step_instance,
    reconcile_session_activation,
)
from gobby.hooks.session_types import HookSessionManager
from gobby.mcp_proxy.tools.apply_agent_definition import apply_agent_definition_impl
from gobby.storage.agents import LocalAgentRunManager
from gobby.storage.definitions import AgentDefinitionManager
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.sessions import SessionManager
from gobby.workflows.agent_resolver import resolve_agent_with_row
from gobby.workflows.state_manager import SessionVariableManager
from gobby.workflows.step_instances import AgentStepInstanceManager, build_step_instance
from tests.fixtures.isolated_checkout import IsolatedCheckoutFactory

pytestmark = pytest.mark.integration


@pytest.fixture(autouse=True)
def _local_machine_identity() -> Iterator[None]:
    with patch("gobby.utils.machine_id._cached_machine_id", "21000000-0000-4000-8000-000000000001"):
        yield


@pytest.fixture
def seat_id(
    temp_db: HubDatabase,
    tmp_path: Path,
    isolated_checkout_factory: IsolatedCheckoutFactory,
) -> str:
    project = isolated_checkout_factory(temp_db, "step-seats", root=tmp_path).project
    return SessionManager(temp_db).register_session(
        external_id="seat",
        machine_id="21000000-0000-4000-8000-000000000001",
        source="claude",
        project_id=project.id,
        project_path=str(tmp_path),
    )


def _define(db: HubDatabase, name: str, steps: list[str] | None) -> None:
    AgentDefinitionManager(db).upsert_with_steps(
        name,
        {
            "name": name,
            "surfaces": ["persona", "spawn"],
            "prompts": {"persona": "Seat.", "agent": "Work."},
            "workflows": {"rule_selectors": {"include": [], "exclude": []}},
        },
        {"steps": [{"name": step} for step in steps]} if steps is not None else None,
        source="custom",
    )


def _reconcile(db: HubDatabase, session_id: str, path: Path) -> ActivationReconciliationResult:
    return reconcile_session_activation(
        HookEvent(
            event_type=HookEventType.BEFORE_AGENT,
            session_id="seat",
            source=SessionSource.CLAUDE,
            timestamp=datetime.now(UTC),
            data={"cwd": str(path)},
            metadata={"_platform_session_id": session_id},
        ),
        EventHandlers(session_manager=cast(HookSessionManager, SessionManager(db))),
    )


async def test_interactive_seat_with_step_workflow_gets_instance_without_task(
    temp_db: HubDatabase,
    seat_id: str,
) -> None:
    _define(temp_db, "interactive-seat", ["claim", "implement"])
    result = await apply_agent_definition_impl(
        agent="interactive-seat", db=temp_db, session_id=seat_id
    )
    assert result["success"] is True
    assert result["step_workflow"]["current_step"] == "claim"
    variables = SessionVariableManager(temp_db).get_variables(seat_id)
    assert variables["step_workflow_complete"] is False
    assert not variables.get("assigned_task_id")
    assert not variables.get("active_task_id")
    instance = AgentStepInstanceManager(temp_db).get_for_session(seat_id)
    assert instance is not None
    assert instance.agent_name == "interactive-seat"
    assert instance.current_step == "claim"


@pytest.mark.parametrize("name", ["default", "plain-seat"])
async def test_seat_without_step_workflow_gets_none(
    temp_db: HubDatabase, seat_id: str, name: str
) -> None:
    _define(temp_db, name, None)
    result = await apply_agent_definition_impl(agent=name, db=temp_db, session_id=seat_id)
    assert result["success"] is True
    assert result["step_workflow"] is None
    assert AgentStepInstanceManager(temp_db).get_for_session(seat_id) is None
    assert "step_workflow_complete" not in SessionVariableManager(temp_db).get_variables(seat_id)


async def test_reconcile_repairs_missing_instance_after_failed_save(
    temp_db: HubDatabase, seat_id: str, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    _define(temp_db, "review-seat", ["review", "report"])
    with patch.object(AgentStepInstanceManager, "save", side_effect=RuntimeError("save failed")):
        result = await apply_agent_definition_impl(
            agent="review-seat", db=temp_db, session_id=seat_id
        )
    variables = SessionVariableManager(temp_db).get_variables(seat_id)
    assert result["success"] is True
    assert result["step_workflow_pending"] is True
    assert "save failed" in caplog.text
    assert variables["_agent_type"] == "review-seat"
    assert variables["step_workflow_complete"] is False
    assert AgentStepInstanceManager(temp_db).get_for_session(seat_id) is None

    repaired = _reconcile(temp_db, seat_id, tmp_path)
    instance = AgentStepInstanceManager(temp_db).get_for_session(seat_id)
    assert repaired.reason == "repaired"
    assert instance is not None
    assert instance.agent_name == "review-seat"
    assert instance.current_step == "review"


def test_spawned_step_instance_unchanged(
    temp_db: HubDatabase, seat_id: str, tmp_path: Path
) -> None:
    _define(temp_db, "spawn-seat", ["claim", "implement"])
    sessions = SessionManager(temp_db)
    parent = sessions.get(seat_id)
    assert parent is not None
    child = sessions.register_session(
        external_id="spawn-seat",
        machine_id="21000000-0000-4000-8000-000000000001",
        source="claude",
        project_id=parent.project_id,
        project_path=str(tmp_path),
        agent_depth=1,
    )
    LocalAgentRunManager(temp_db).create(
        parent_session_id=seat_id,
        provider="claude",
        prompt="Work.",
        agent_name="spawn-seat",
        child_session_id=child,
    )
    resolved = resolve_agent_with_row("spawn-seat", temp_db)
    assert resolved is not None
    body, row = resolved
    initial = build_step_instance(body, session_id=child, step_workflow_id=row.step_workflow_id)
    initial.current_step = "implement"
    manager = AgentStepInstanceManager(temp_db)
    manager.save(initial)
    SessionVariableManager(temp_db).merge_variables(child, {"_agent_type": "default"})

    _reconcile(temp_db, child, tmp_path)

    instance = manager.get_for_session(child)
    assert instance is not None
    assert instance.id == initial.id
    assert instance.snapshot == initial.snapshot
    assert instance.current_step == "implement"
    assert SessionVariableManager(temp_db).get_variables(child)["_agent_type"] == "spawn-seat"


@pytest.mark.parametrize("target,steps", [("seat-y", ["review"]), ("default", None)])
async def test_relaunch_switch_replaces_step_instance(
    temp_db: HubDatabase, seat_id: str, target: str, steps: list[str] | None
) -> None:
    _define(temp_db, "seat-x", ["claim"])
    _define(temp_db, target, steps)
    await apply_agent_definition_impl(agent="seat-x", db=temp_db, session_id=seat_id)
    previous = AgentStepInstanceManager(temp_db).get_for_session(seat_id)
    assert previous is not None
    SessionVariableManager(temp_db).set_variable(seat_id, "step_workflow_complete", True)

    result = await apply_agent_definition_impl(
        agent=target, db=temp_db, session_id=seat_id, relaunch=True
    )

    assert result["status"] == "applied"
    instance = AgentStepInstanceManager(temp_db).get_for_session(seat_id)
    if steps is None:
        assert instance is None
        assert (
            SessionVariableManager(temp_db).get_variables(seat_id).get("step_workflow_complete")
            is None
        )
    else:
        assert instance is not None
        assert instance.id != previous.id
        assert instance.agent_name == target
        assert instance.current_step == steps[0]
        assert result["step_workflow"]["current_step"] == steps[0]
        assert (
            SessionVariableManager(temp_db).get_variables(seat_id)["step_workflow_complete"]
            is False
        )


async def test_activation_over_configured_base_replaces_base_instance(
    temp_db: HubDatabase, seat_id: str
) -> None:
    _define(temp_db, "configured-base", ["start"])
    _define(temp_db, "seat-y", ["review"])
    await apply_agent_definition_impl(agent="configured-base", db=temp_db, session_id=seat_id)
    with patch(
        "gobby.storage.config_repository.ConfigRepository.read",
        return_value=SimpleNamespace(values={"default_agent": "configured-base"}),
    ):
        result = await apply_agent_definition_impl(agent="seat-y", db=temp_db, session_id=seat_id)
    assert result["status"] == "applied"
    instance = AgentStepInstanceManager(temp_db).get_for_session(seat_id)
    assert instance is not None
    assert instance.agent_name == "seat-y"
    assert instance.current_step == "review"


@pytest.mark.parametrize("scenario", ["failed_save", "concurrent_merge", "rollback"])
async def test_switch_serializes_with_stale_reconcile(
    temp_db: HubDatabase, seat_id: str, scenario: str
) -> None:
    _define(temp_db, "seat-x", ["claim"])
    _define(temp_db, "seat-y", ["review"])
    await apply_agent_definition_impl(agent="seat-x", db=temp_db, session_id=seat_id)
    variables = SessionVariableManager(temp_db)
    stale = variables.get_variables(seat_id)
    session = SessionManager(temp_db).get(seat_id)
    manager = AgentStepInstanceManager(temp_db)
    original = manager.get_for_session(seat_id)
    assert original is not None

    if scenario == "failed_save":
        with patch.object(
            AgentStepInstanceManager, "save", side_effect=RuntimeError("save failed")
        ):
            result = await apply_agent_definition_impl(
                agent="seat-y", db=temp_db, session_id=seat_id, relaunch=True
            )
        assert result["step_workflow_pending"] is True
        assert _ensure_step_instance(temp_db, seat_id, stale, session) is True
    elif scenario == "rollback":
        with patch.object(
            SessionVariableManager, "merge_variables", side_effect=RuntimeError("merge failed")
        ):
            with pytest.raises(RuntimeError, match="merge failed"):
                await apply_agent_definition_impl(
                    agent="seat-y", db=temp_db, session_id=seat_id, relaunch=True
                )
        assert variables.get_variables(seat_id) == stale
        remaining = manager.get_for_session(seat_id)
        assert remaining is not None
        assert remaining.id == original.id
        assert remaining.current_step == "claim"
        return
    else:
        merging, release, reconciling = Event(), Event(), Event()
        errors: list[BaseException] = []
        merge = SessionVariableManager.merge_variables

        def paused_merge(self: SessionVariableManager, sid: str, delta: dict[str, Any]) -> bool:
            if delta.get("_agent_type") == "seat-y":
                merging.set()
                if not release.wait(10):
                    raise RuntimeError("merge release timed out")
            return merge(self, sid, delta)

        def switch() -> None:
            try:
                asyncio.run(
                    apply_agent_definition_impl(
                        agent="seat-y", db=temp_db, session_id=seat_id, relaunch=True
                    )
                )
            except BaseException as exc:
                errors.append(exc)

        def reconcile() -> None:
            reconciling.set()
            try:
                _ensure_step_instance(temp_db, seat_id, stale, session)
            except BaseException as exc:
                errors.append(exc)

        writer, repair = Thread(target=switch), Thread(target=reconcile)
        with patch.object(SessionVariableManager, "merge_variables", paused_merge):
            writer.start()
            try:
                assert merging.wait(10)
                repair.start()
                assert reconciling.wait(10)
            finally:
                release.set()
                writer.join(10)
                if repair.ident is not None:
                    repair.join(10)
        assert not writer.is_alive()
        assert not repair.is_alive()
        assert errors == []

    instance = manager.get_for_session(seat_id)
    assert instance is not None
    assert instance.agent_name == "seat-y"
    assert instance.current_step == "review"
    assert variables.get_variables(seat_id)["_agent_type"] == "seat-y"
