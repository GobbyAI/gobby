"""Regression tests for task-manager-backed rule evaluation."""

from collections.abc import Iterator
from datetime import UTC, datetime
from unittest.mock import patch

import pytest

from gobby.hooks.events import HookEvent, HookEventType, SessionSource
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.pipeline_subscribers import PipelineSubscriberStorageError
from gobby.storage.projects import LocalProjectManager
from gobby.storage.tasks import LocalTaskManager
from gobby.workflows.engine.core import RuleEngine
from gobby.workflows.observer_plan_mode import detect_plan_mode_from_context
from gobby.workflows.sync_rules import sync_bundled_rules

pytestmark = pytest.mark.unit


@pytest.fixture
def db(temp_db: HubDatabase) -> Iterator[HubDatabase]:
    database = temp_db
    yield database


def _sync_bundled(db: HubDatabase) -> None:
    from gobby.workflows.sync_rules import get_bundled_rules_path

    sync_bundled_rules(db, get_bundled_rules_path())


def _make_event(event_type: HookEventType = HookEventType.STOP) -> HookEvent:
    return HookEvent(
        event_type=event_type,
        session_id="11111111-1111-4111-8111-111111111111",
        source=SessionSource.CLAUDE,
        timestamp=datetime.now(UTC),
        data={},
        metadata={},
    )


@pytest.mark.asyncio
async def test_require_epic_tree_close_uses_real_task_manager(db: HubDatabase) -> None:
    _sync_bundled(db)
    task_manager = LocalTaskManager(db)
    project = LocalProjectManager(db).create(name="task-helper-wiring", repo_path=None)
    parent = task_manager.create_task(
        project_id=project.id,
        title="Parent epic",
        task_type="epic",
        category="planning",
        validation_criteria="Test task completion is observable.",
    )
    task_manager.create_task(
        project_id=project.id,
        title="Open child",
        parent_task_id=parent.id,
        category="code",
        validation_criteria="Test task completion is observable.",
    )

    engine = RuleEngine(db, task_manager=task_manager)
    response = await engine.evaluate(
        _make_event(),
        session_id="11111111-1111-4111-8111-111111111111",
        variables={
            "_agent_type": "default",
            "mode_level": 0,
            "task_claimed": True,
            "claimed_tasks": {parent.id: f"#{parent.seq_num}"},
            "stop_attempts": 0,
        },
    )

    assert response.decision == "block"
    assert "Epic tree not complete" in (response.reason or "")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("labels", "cached_labels", "expected_decision"),
    [
        pytest.param(["live-session"], [], "allow", id="database-label-enables-exemption"),
        pytest.param([], ["live-session"], "block", id="cached-label-cannot-fake-exemption"),
    ],
)
async def test_require_task_close_uses_database_labels_after_mode_reset(
    db: HubDatabase,
    labels: list[str],
    cached_labels: list[str],
    expected_decision: str,
) -> None:
    _sync_bundled(db)
    task_manager = LocalTaskManager(db)
    project = LocalProjectManager(db).create(name=f"label-gate-{expected_decision}", repo_path=None)
    task = task_manager.create_task(
        project_id=project.id,
        title="Claimed task",
        category="code",
        labels=labels,
        validation_criteria="Test task completion is observable.",
    )
    variables: dict[str, object] = {
        "_agent_type": "default",
        "mode_level": 0,
        "chat_mode": "bypass",
        "plan_mode": True,
        "plan_skill_loaded": True,
        "task_claimed": True,
        "claimed_tasks": {task.id: f"#{task.seq_num}"},
        "claimed_task_labels": cached_labels,
        "stop_attempts": 0,
    }

    detect_plan_mode_from_context("Continue implementing the task.", variables, task.id)
    response = await RuleEngine(db, task_manager=task_manager).evaluate(
        _make_event(),
        session_id="11111111-1111-4111-8111-111111111111",
        variables=variables,
    )

    assert variables["mode_level"] == 2
    assert response.decision == expected_decision


@pytest.mark.asyncio
async def test_require_task_close_rejects_mixed_live_and_ordinary_claims(
    db: HubDatabase,
) -> None:
    _sync_bundled(db)
    task_manager = LocalTaskManager(db)
    project = LocalProjectManager(db).create(name="mixed-label-gate", repo_path=None)
    live_task = task_manager.create_task(
        project_id=project.id,
        title="Live task",
        category="code",
        labels=["live-session"],
        validation_criteria="Test task completion is observable.",
    )
    ordinary_task = task_manager.create_task(
        project_id=project.id,
        title="Ordinary task",
        category="code",
        validation_criteria="Test task completion is observable.",
    )

    response = await RuleEngine(db, task_manager=task_manager).evaluate(
        _make_event(),
        session_id="11111111-1111-4111-8111-111111111111",
        variables={
            "_agent_type": "default",
            "mode_level": 2,
            "task_claimed": True,
            "claimed_tasks": {
                live_task.id: f"#{live_task.seq_num}",
                ordinary_task.id: f"#{ordinary_task.seq_num}",
            },
            "claimed_task_labels": ["live-session"],
            "stop_attempts": 0,
        },
    )

    assert response.decision == "block"
    assert "require-task-close" in (response.reason or "")


@pytest.mark.asyncio
async def test_require_epic_tree_close_skips_live_session_epic(db: HubDatabase) -> None:
    _sync_bundled(db)
    task_manager = LocalTaskManager(db)
    project = LocalProjectManager(db).create(name="live-epic-gate", repo_path=None)
    parent = task_manager.create_task(
        project_id=project.id,
        title="Live parent epic",
        task_type="epic",
        category="planning",
        labels=["live-session"],
        validation_criteria="Test task completion is observable.",
    )
    task_manager.create_task(
        project_id=project.id,
        title="Open child",
        parent_task_id=parent.id,
        category="code",
        validation_criteria="Test task completion is observable.",
    )

    response = await RuleEngine(db, task_manager=task_manager).evaluate(
        _make_event(),
        session_id="11111111-1111-4111-8111-111111111111",
        variables={
            "_agent_type": "default",
            "mode_level": 2,
            "task_claimed": True,
            "claimed_tasks": {parent.id: f"#{parent.seq_num}"},
            "stop_attempts": 0,
        },
    )

    assert response.decision == "allow"


@pytest.mark.asyncio
async def test_active_agent_wait_is_computed_once_and_yields_both_stop_gates(
    db: HubDatabase,
) -> None:
    _sync_bundled(db)
    variables: dict[str, object] = {
        "_agent_type": "default",
        "mode_level": 2,
        "task_claimed": True,
        "claimed_tasks": {"11111111-1111-4111-8111-111111111112": "#1"},
        "stop_attempts": 3,
    }

    with patch("gobby.workflows.engine.core.CompletionSubscriberManager") as manager_type:
        wait_lookup = manager_type.return_value.has_active_agent_wait
        wait_lookup.return_value = True
        response = await RuleEngine(db).evaluate(
            _make_event(),
            session_id="11111111-1111-4111-8111-111111111111",
            variables=variables,
        )

    assert response.decision == "allow"
    assert variables["stop_attempts"] == 3
    wait_lookup.assert_called_once_with("11111111-1111-4111-8111-111111111111")


@pytest.mark.asyncio
async def test_agent_wait_lookup_failure_warns_and_keeps_stop_gates_armed(
    db: HubDatabase,
    caplog: pytest.LogCaptureFixture,
) -> None:
    _sync_bundled(db)
    variables: dict[str, object] = {
        "_agent_type": "default",
        "mode_level": 2,
        "task_claimed": True,
        "claimed_tasks": {"11111111-1111-4111-8111-111111111112": "#1"},
        "stop_attempts": 0,
    }

    with (
        patch("gobby.workflows.engine.core.CompletionSubscriberManager") as manager_type,
        caplog.at_level("WARNING"),
    ):
        manager_type.return_value.has_active_agent_wait.side_effect = (
            PipelineSubscriberStorageError("lookup failed")
        )
        response = await RuleEngine(db).evaluate(
            _make_event(),
            session_id="11111111-1111-4111-8111-111111111111",
            variables=variables,
        )

    assert response.decision == "block"
    assert variables["stop_attempts"] == 1
    assert "Failed to determine active agent wait" in caplog.text


@pytest.mark.asyncio
async def test_human_wait_marker_yields_for_one_turn_and_can_be_reset(
    db: HubDatabase,
) -> None:
    _sync_bundled(db)
    session_id = "11111111-1111-4111-8111-111111111111"
    variables: dict[str, object] = {
        "_agent_type": "default",
        "mode_level": 2,
        "task_claimed": True,
        "claimed_tasks": {"11111111-1111-4111-8111-111111111112": "#1"},
        "stop_attempts": 4,
        "waiting_on_user_input": True,
    }
    engine = RuleEngine(db)

    first_wait = await engine.evaluate(_make_event(), session_id, variables)
    assert first_wait.decision == "allow"
    assert variables["stop_attempts"] == 4

    await engine.evaluate(_make_event(HookEventType.BEFORE_AGENT), session_id, variables)
    assert variables["waiting_on_user_input"] is False
    assert variables["stop_attempts"] == 0

    variables["waiting_on_user_input"] = True
    repeated_wait = await engine.evaluate(_make_event(), session_id, variables)
    assert repeated_wait.decision == "allow"
    assert variables["stop_attempts"] == 0

    await engine.evaluate(_make_event(HookEventType.BEFORE_AGENT), session_id, variables)
    rearmed = await engine.evaluate(_make_event(), session_id, variables)
    assert rearmed.decision == "block"
    assert variables["stop_attempts"] == 1
