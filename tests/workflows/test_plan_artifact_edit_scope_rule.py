"""Tests for block-writes-outside-plan-artifact rule."""

from dataclasses import dataclass
from datetime import UTC, datetime

import pytest

from gobby.hooks.events import HookEvent, HookEventType, SessionSource
from gobby.storage.database import LocalDatabase
from gobby.storage.migrations import run_migrations
from gobby.storage.workflow_definitions import LocalWorkflowDefinitionManager
from gobby.workflows.definitions import RuleDefinitionBody
from gobby.workflows.rule_engine import RuleEngine
from gobby.workflows.safe_evaluator import SafeExpressionEvaluator
from gobby.workflows.sync import sync_bundled_rules

pytestmark = pytest.mark.unit


@dataclass
class FakeTask:
    id: str
    status: str


class FakeTaskManager:
    def __init__(self, tasks: dict[str, FakeTask]):
        self._tasks = tasks

    def get_task(self, task_id: str) -> FakeTask | None:
        return self._tasks.get(task_id)


@pytest.fixture
def db(tmp_path) -> LocalDatabase:
    db_path = tmp_path / "test_plan_artifact_scope.db"
    database = LocalDatabase(db_path)
    run_migrations(database)
    return database


def _sync_bundled(db: LocalDatabase) -> RuleDefinitionBody:
    from gobby.workflows.sync import get_bundled_rules_path

    sync_bundled_rules(db, get_bundled_rules_path())
    manager = LocalWorkflowDefinitionManager(db)
    row = manager.get_by_name("block-writes-outside-plan-artifact")
    assert row is not None
    return RuleDefinitionBody.model_validate_json(row.definition_json)


def _make_write_event(file_path: str) -> HookEvent:
    return HookEvent(
        event_type=HookEventType.BEFORE_TOOL,
        session_id="test-session",
        source=SessionSource.CLAUDE,
        timestamp=datetime.now(UTC),
        data={"tool_name": "Write", "tool_input": {"file_path": file_path}},
        metadata={},
    )


def _evaluate_when(
    engine: RuleEngine,
    when: str,
    *,
    file_path: str,
    variables: dict[str, object],
) -> bool:
    event = _make_write_event(file_path)
    ctx = engine._build_eval_context(event, variables)
    funcs = engine._build_allowed_funcs(ctx)
    return SafeExpressionEvaluator(ctx, funcs).evaluate(when)


def test_rule_syncs_and_uses_helper_wiring(db: LocalDatabase) -> None:
    body = _sync_bundled(db)
    assert body.event.value == "before_tool"
    assert body.effects[0].type == "block"
    assert "is_current_plan_artifact" in (body.when or "")
    assert "task_status_in" in (body.when or "")


def test_delegated_path_blocks_non_artifact_and_allows_artifact(
    db: LocalDatabase, tmp_path
) -> None:
    body = _sync_bundled(db)
    engine = RuleEngine(db)
    artifact_rel = ".gobby/plans/task-42-plan.md"
    artifact_abs = str(tmp_path / ".gobby" / "plans" / "task-42-plan.md")
    other_abs = str(tmp_path / "notes.md")
    variables: dict[str, object] = {
        "project": {"path": str(tmp_path)},
        "plan_review_mode": "delegated",
        "interactive_lock_label": "interactive:planning-in-progress:sess-1",
        "artifact_path": artifact_rel,
    }

    assert _evaluate_when(engine, body.when or "", file_path=other_abs, variables=variables) is True
    assert (
        _evaluate_when(engine, body.when or "", file_path=artifact_abs, variables=variables)
        is False
    )


def test_planner_path_blocks_until_task_reaches_terminal_status(
    db: LocalDatabase, tmp_path
) -> None:
    body = _sync_bundled(db)
    task_manager = FakeTaskManager({"task-1": FakeTask(id="task-1", status="open")})
    engine = RuleEngine(db, task_manager=task_manager)
    variables: dict[str, object] = {
        "project": {"path": str(tmp_path)},
        "_agent_type": "planner",
        "assigned_task_id": "task-1",
        "artifact_path": ".gobby/plans/task-42-plan.md",
    }

    assert (
        _evaluate_when(
            engine,
            body.when or "",
            file_path=str(tmp_path / "src" / "app.py"),
            variables=variables,
        )
        is True
    )

    task_manager._tasks["task-1"].status = "review_approved"
    assert (
        _evaluate_when(
            engine,
            body.when or "",
            file_path=str(tmp_path / "src" / "app.py"),
            variables=variables,
        )
        is False
    )


def test_missing_artifact_path_fails_closed_for_delegated_mode(db: LocalDatabase, tmp_path) -> None:
    body = _sync_bundled(db)
    engine = RuleEngine(db)
    variables: dict[str, object] = {
        "project": {"path": str(tmp_path)},
        "plan_review_mode": "delegated",
        "interactive_lock_label": "interactive:planning-in-progress:sess-1",
    }

    assert (
        _evaluate_when(
            engine,
            body.when or "",
            file_path=str(tmp_path / "anywhere.txt"),
            variables=variables,
        )
        is True
    )
