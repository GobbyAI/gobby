"""Regression tests for task-manager-backed rule evaluation."""

from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest

from gobby.hooks.events import HookEvent, HookEventType, SessionSource
from gobby.storage.database import LocalDatabase
from gobby.storage.migrations import run_migrations
from gobby.storage.projects import LocalProjectManager
from gobby.storage.tasks import LocalTaskManager
from gobby.workflows.engine.core import RuleEngine
from gobby.workflows.sync_rules import sync_bundled_rules

pytestmark = pytest.mark.unit


@pytest.fixture
def db(tmp_path: Path) -> Iterator[LocalDatabase]:
    db_path = tmp_path / "test_rule_engine_task_wiring.db"
    database = LocalDatabase(db_path)
    run_migrations(database)
    yield database
    database.close()


def _sync_bundled(db: LocalDatabase) -> None:
    from gobby.workflows.sync_rules import get_bundled_rules_path

    sync_bundled_rules(db, get_bundled_rules_path())


def _make_event() -> HookEvent:
    return HookEvent(
        event_type=HookEventType.STOP,
        session_id="test-session",
        source=SessionSource.CLAUDE,
        timestamp=datetime.now(UTC),
        data={},
        metadata={},
    )


@pytest.mark.asyncio
async def test_require_epic_tree_close_uses_real_task_manager(db: LocalDatabase) -> None:
    _sync_bundled(db)
    task_manager = LocalTaskManager(db)
    project = LocalProjectManager(db).create(name="task-helper-wiring", repo_path=None)
    parent = task_manager.create_task(
        project_id=project.id,
        title="Parent epic",
        task_type="epic",
        category="planning",
    )
    task_manager.create_task(
        project_id=project.id,
        title="Open child",
        parent_task_id=parent.id,
        category="code",
    )

    engine = RuleEngine(db, task_manager=task_manager)
    response = await engine.evaluate(
        _make_event(),
        session_id="sess-1",
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
