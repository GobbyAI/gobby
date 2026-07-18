"""Tests for block-writes-outside-plan-artifact rule."""

from datetime import UTC, datetime
from pathlib import Path

import pytest

from gobby.hooks.events import HookEvent, HookEventType, SessionSource
from gobby.hooks.normalization import normalize_tool_fields
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.projects import LocalProjectManager
from gobby.storage.sessions import SessionManager
from gobby.storage.tasks import LocalTaskManager
from gobby.storage.workflow_definitions import LocalWorkflowDefinitionManager
from gobby.workflows.definitions import RuleDefinitionBody
from gobby.workflows.engine.core import RuleEngine
from gobby.workflows.safe_evaluator import SafeExpressionEvaluator
from gobby.workflows.sync_rules import sync_bundled_rules

pytestmark = pytest.mark.unit


@pytest.fixture
def db(temp_db: HubDatabase) -> HubDatabase:
    database = temp_db
    return database


def _sync_bundled(
    db: HubDatabase,
    rule_name: str = "block-writes-outside-plan-artifact",
) -> RuleDefinitionBody:
    from gobby.workflows.sync_rules import get_bundled_rules_path

    sync_bundled_rules(db, get_bundled_rules_path())
    manager = LocalWorkflowDefinitionManager(db)
    row = manager.get_by_name(rule_name)
    assert row is not None
    return RuleDefinitionBody.model_validate_json(row.definition_json)


def _make_write_event(
    file_path: str,
    metadata: dict[str, object] | None = None,
    *,
    source: SessionSource = SessionSource.CLAUDE,
    tool_input: dict[str, object] | None = None,
) -> HookEvent:
    data: dict[str, object] = {
        "tool_name": "Write",
        "tool_input": tool_input or {"file_path": file_path},
    }
    normalize_tool_fields(data)
    return HookEvent(
        event_type=HookEventType.BEFORE_TOOL,
        session_id="11111111-1111-4111-8111-111111111111",
        source=source,
        timestamp=datetime.now(UTC),
        data=data,
        metadata=metadata or {},
    )


def _evaluate_when(
    engine: RuleEngine,
    when: str,
    *,
    file_path: str,
    variables: dict[str, object],
    metadata: dict[str, object] | None = None,
    source: SessionSource = SessionSource.CLAUDE,
    tool_input: dict[str, object] | None = None,
) -> bool:
    event = _make_write_event(file_path, metadata, source=source, tool_input=tool_input)
    ctx = engine._build_eval_context(event, variables)
    funcs = engine._build_allowed_funcs(ctx)
    return SafeExpressionEvaluator(ctx, funcs).evaluate(when)


def test_rule_syncs_and_uses_helper_wiring(db: HubDatabase) -> None:
    body = _sync_bundled(db)
    assert body.event.value == "before_tool"
    assert body.effects[0].type == "block"
    assert "plan_write_paths_allowed" in (body.when or "")
    assert "task_state_in" in (body.when or "")


def test_delegated_path_blocks_non_artifact_and_allows_artifact(db: HubDatabase, tmp_path) -> None:
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


def test_planner_path_blocks_until_task_reaches_review_approved(db: HubDatabase, tmp_path) -> None:
    body = _sync_bundled(db)
    project = LocalProjectManager(db).create(name="planner-project", repo_path=str(tmp_path))
    task_manager = LocalTaskManager(db)
    task = task_manager.create_task(project_id=project.id, title="Planner task")
    engine = RuleEngine(db, task_manager=task_manager)
    variables: dict[str, object] = {
        "project": {"path": str(tmp_path)},
        "_agent_type": "planner",
        "assigned_task_id": task.id,
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

    task_manager.initialize_task_manifest(task.id)
    task_manager.stage_states.start_stage(task.id, "development", by_session_id=None)
    task_manager.submit_for_review(task.id)
    task_manager.approve_review(task.id)
    assert (
        _evaluate_when(
            engine,
            body.when or "",
            file_path=str(tmp_path / "src" / "app.py"),
            variables=variables,
        )
        is False
    )


def test_missing_artifact_path_fails_closed_for_delegated_mode(db: HubDatabase, tmp_path) -> None:
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


def test_missing_artifact_allows_only_active_agy_scratch_path(
    db: HubDatabase,
    tmp_path: Path,
) -> None:
    body = _sync_bundled(db)
    engine = RuleEngine(db)
    repo = tmp_path / "repo"
    repo.mkdir()
    variables: dict[str, object] = {
        "project": {"path": str(repo)},
        "plan_review_mode": "delegated",
        "interactive_lock_label": "interactive:planning-in-progress:sess-1",
    }

    assert (
        _evaluate_when(
            engine,
            body.when or "",
            file_path=str(Path.home() / ".gemini" / "scratch" / "state.json"),
            variables=variables,
            source=SessionSource.AGY,
        )
        is False
    )
    assert (
        _evaluate_when(
            engine,
            body.when or "",
            file_path=str(Path.home() / ".claude" / "scratch" / "state.json"),
            variables=variables,
            source=SessionSource.AGY,
        )
        is True
    )


def test_interactive_rule_requires_every_target_to_be_plan_or_scratch(
    db: HubDatabase,
    tmp_path: Path,
) -> None:
    body = _sync_bundled(db, "block-edits-plan-mode")
    engine = RuleEngine(db)
    repo = tmp_path / "repo"
    repo.mkdir()
    artifact = repo / ".gobby" / "plans" / "plan.md"
    scratch = Path.home() / ".claude" / "scratch" / "state.json"
    variables: dict[str, object] = {"project": {"path": str(repo)}, "plan_mode": True}

    assert (
        _evaluate_when(
            engine,
            body.when or "",
            file_path="",
            variables=variables,
            tool_input={"file_paths": [str(artifact), str(scratch)]},
        )
        is False
    )
    assert (
        _evaluate_when(
            engine,
            body.when or "",
            file_path="",
            variables=variables,
            tool_input={"file_paths": [str(scratch), str(repo / "src" / "unsafe.py")]},
        )
        is True
    )
    assert (
        _evaluate_when(
            engine,
            body.when or "",
            file_path=".claude/plans/project-local.md",
            variables=variables,
        )
        is True
    )


@pytest.mark.asyncio
async def test_claude_memory_hygiene_still_blocks_provider_scratch_write(
    db: HubDatabase,
    tmp_path: Path,
) -> None:
    _sync_bundled(db)
    repo = tmp_path / "repo"
    repo.mkdir()
    event = _make_write_event(str(Path.home() / ".claude" / "memory" / "project.md"))

    response = await RuleEngine(db).evaluate(
        event,
        session_id=event.session_id,
        variables={"project": {"path": str(repo)}, "plan_mode": True},
    )

    assert response.decision == "block"
    assert response.reason is not None
    assert "gobby-memory" in response.reason


def test_absolute_artifact_write_uses_platform_session_project_path(
    db: HubDatabase,
    tmp_path,
) -> None:
    body = _sync_bundled(db)
    project = LocalProjectManager(db).create(name="test-project", repo_path=str(tmp_path))
    platform_session_id = SessionManager(db).register_session(
        external_id="claude-external",
        machine_id="machine-1",
        source="claude",
        project_id=project.id,
        project_path=str(tmp_path),
    )
    task_manager = LocalTaskManager(db)
    task = task_manager.create_task(project_id=project.id, title="Planner task")
    engine = RuleEngine(db, task_manager=task_manager)
    variables: dict[str, object] = {
        "_agent_type": "planner",
        "assigned_task_id": task.id,
        "artifact_path": ".gobby/plans/task-42-plan.md",
    }

    assert (
        _evaluate_when(
            engine,
            body.when or "",
            file_path=str(tmp_path / ".gobby" / "plans" / "task-42-plan.md"),
            variables=variables,
            metadata={"_platform_session_id": platform_session_id},
        )
        is False
    )
