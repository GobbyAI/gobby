"""Deterministic stop enforcement for unclaimed found-work tasks."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gobby.hooks.events import HookEvent, HookEventType, SessionSource
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.projects import LocalProjectManager
from gobby.storage.sessions import SessionManager
from gobby.storage.tasks import LocalTaskManager
from gobby.workflows.engine.core import RuleEngine
from gobby.workflows.found_work_gate import FoundWorkStopAnalyzer, FoundWorkStopFacts
from gobby.workflows.hooks import WorkflowHookHandler
from gobby.workflows.sync_rules import get_bundled_rules_path, sync_bundled_rules

pytestmark = pytest.mark.unit

MACHINE_ID = "20000000-0000-4000-8000-000000000012"


@pytest.fixture(autouse=True)
def _local_machine_identity() -> Iterator[None]:
    with patch("gobby.utils.machine_id._cached_machine_id", MACHINE_ID):
        yield


def _event(session_id: str) -> HookEvent:
    return HookEvent(
        event_type=HookEventType.STOP,
        session_id=session_id,
        source=SessionSource.CODEX,
        timestamp=datetime.now(UTC),
        data={},
        metadata={"_platform_session_id": session_id},
    )


def _analyzer(db: HubDatabase) -> FoundWorkStopAnalyzer:
    return FoundWorkStopAnalyzer(
        llm_service_resolver=lambda: None,
        config_resolver=lambda: None,
        session_manager=None,
        session_task_manager=None,
        db=db,
    )


@pytest.fixture
def task_context(temp_db: HubDatabase) -> tuple[LocalTaskManager, str, str, str]:
    project = LocalProjectManager(temp_db).create(name="unclaimed-gate", repo_path=None)
    sessions = SessionManager(temp_db)
    owner_id = sessions.register_session(
        external_id="unclaimed-owner",
        machine_id=MACHINE_ID,
        source="codex",
        project_id=project.id,
    )
    other_id = sessions.register_session(
        external_id="unclaimed-other",
        machine_id=MACHINE_ID,
        source="codex",
        project_id=project.id,
    )
    assert owner_id
    assert other_id
    return LocalTaskManager(temp_db), project.id, owner_id, other_id


@pytest.mark.asyncio
async def test_query_returns_only_unclaimed_unlabeled_tasks_created_by_session(
    temp_db: HubDatabase,
    task_context: tuple[LocalTaskManager, str, str, str],
) -> None:
    tasks, project_id, owner_id, other_id = task_context
    candidate = tasks.create_task(
        project_id=project_id,
        title="Candidate",
        created_in_session_id=owner_id,
        category="code",
        validation_criteria="Candidate is fixed.",
    )
    tasks.create_task(
        project_id=project_id,
        title="Needs decision",
        created_in_session_id=owner_id,
        labels=["needs-decision"],
        category="code",
        validation_criteria="Decision is recorded.",
    )
    tasks.create_task(
        project_id=project_id,
        title="Clean window",
        created_in_session_id=owner_id,
        labels=["clean-window"],
        category="code",
        validation_criteria="Clean-window work is completed.",
    )
    tasks.create_task(
        project_id=project_id,
        title="Claimed",
        created_in_session_id=owner_id,
        claimed_by_session_id=owner_id,
        category="code",
        validation_criteria="Claimed work is completed.",
    )
    tasks.create_task(
        project_id=project_id,
        title="Expanded child",
        created_in_session_id=owner_id,
        labels=["expansion-run:run-1"],
        category="code",
        validation_criteria="Expanded work is completed.",
    )
    tasks.create_task(
        project_id=project_id,
        title="Foreign session",
        created_in_session_id=other_id,
        category="code",
        validation_criteria="Foreign work is completed.",
    )

    refs = _analyzer(temp_db).unclaimed_found_work(owner_id)

    assert refs == (f"#{candidate.seq_num}",)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "prompt",
    [
        "Just file it for later.",
        "File them for later; do not implement them now.",
        "Please file these for later.",
    ],
)
async def test_explicit_user_filing_instruction_exempts_unclaimed_tasks(
    temp_db: HubDatabase,
    task_context: tuple[LocalTaskManager, str, str, str],
    prompt: str,
) -> None:
    tasks, project_id, owner_id, _other_id = task_context
    tasks.create_task(
        project_id=project_id,
        title="Explicitly deferred",
        created_in_session_id=owner_id,
        category="code",
        validation_criteria="Deferred work is completed.",
    )

    assert _analyzer(temp_db).unclaimed_found_work(owner_id, user_prompt=prompt) == ()


@pytest.mark.asyncio
@pytest.mark.parametrize("is_spawned_agent", [False, True])
async def test_bundled_rule_blocks_interactive_and_spawned_sessions(
    temp_db: HubDatabase,
    is_spawned_agent: bool,
) -> None:
    sync_bundled_rules(temp_db, get_bundled_rules_path())
    variables: dict[str, Any] = {
        "_memory_initial_stop_checked": True,
        "is_spawned_agent": is_spawned_agent,
        "stop_attempts": 0,
    }

    response = await RuleEngine(temp_db).evaluate(
        _event("11111111-1111-4111-8111-111111111111"),
        session_id="11111111-1111-4111-8111-111111111111",
        variables=variables,
        eval_context={
            "unclaimed_found_work": True,
            "unclaimed_found_work_tasks": ["#21484", "#21485"],
        },
    )

    assert response.decision == "block"
    assert "#21484, #21485" in (response.reason or "")
    assert "Filing is not a terminal state" in (response.reason or "")


@pytest.mark.asyncio
async def test_workflow_handler_feeds_unclaimed_fact_for_spawned_claimed_session(
    temp_db: HubDatabase,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sync_bundled_rules(temp_db, get_bundled_rules_path())
    handler = WorkflowHookHandler(rule_engine=RuleEngine(temp_db))
    variables = {
        "_memory_initial_stop_checked": True,
        "is_spawned_agent": True,
        "task_claimed": True,
        "stop_attempts": 0,
    }
    session_vars = MagicMock()
    session_vars.get_variables.return_value = variables
    handler._session_var_manager = session_vars
    analyze = AsyncMock(return_value=FoundWorkStopFacts())
    unclaimed = MagicMock(return_value=("#21484",))
    monkeypatch.setattr(handler._found_work_analyzer, "analyze", analyze)
    monkeypatch.setattr(handler._found_work_analyzer, "unclaimed_found_work", unclaimed)
    session_id = "11111111-1111-4111-8111-111111111111"
    event = _event(session_id)
    event.cwd = str(Path(__file__).resolve().parents[2])

    response = await handler.evaluate_async(event)

    assert response.decision == "block"
    assert "#21484" in (response.reason or "")
    analyze.assert_not_awaited()
    unclaimed.assert_called_once_with(session_id, user_prompt="")
