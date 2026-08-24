"""Claim-state hydration across session lifecycle boundaries."""

from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

import pytest

from gobby.hooks.events import HookEvent, HookEventType, SessionSource
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.projects import LocalProjectManager
from gobby.storage.sessions import SessionManager
from gobby.storage.tasks import LocalTaskManager
from gobby.workflows.engine.core import RuleEngine
from gobby.workflows.hooks import WorkflowHookHandler
from gobby.workflows.state_manager import SessionVariableManager
from gobby.workflows.sync_rules import get_bundled_rules_path, sync_bundled_rules

LOCAL_MACHINE_ID = "21000000-0000-4000-8000-000000000012"


@pytest.fixture(autouse=True)
def _local_machine_identity() -> Iterator[None]:
    with patch("gobby.utils.machine_id._cached_machine_id", LOCAL_MACHINE_ID):
        yield


@dataclass(frozen=True)
class ClaimContext:
    """Isolated persisted claim and its workflow dependencies."""

    db: HubDatabase
    project_id: str
    session_id: str
    task_id: str
    task_ref: str
    repo_path: Path
    task_manager: LocalTaskManager
    session_manager: SessionManager
    variable_manager: SessionVariableManager


@pytest.fixture
def claim_context(temp_db: HubDatabase, tmp_path: Path) -> ClaimContext:
    """Create a real claim while leaving the session-side claim view reset."""
    project = LocalProjectManager(temp_db).create(
        name="claim-reconciliation",
        repo_path=str(tmp_path),
    )
    session_manager = SessionManager(temp_db)
    session_id = session_manager.register_session(
        external_id="claim-reconciliation-external",
        machine_id="21000000-0000-4000-8000-000000000012",
        source=SessionSource.CLAUDE.value,
        project_id=project.id,
        project_path=str(tmp_path),
    )
    assert session_id

    task_manager = LocalTaskManager(temp_db)
    task = task_manager.create_task(
        project_id=project.id,
        title="Exercise claim reconciliation",
        claimed_by_session_id=session_id,
        category="code",
        implementation_domain="backend",
        validation_criteria="Claim hydration tests pass.",
    )

    sync_bundled_rules(temp_db, get_bundled_rules_path())
    temp_db.execute("UPDATE rule_definitions SET source = 'installed' WHERE source = 'template'")
    temp_db.execute(
        """
        UPDATE rule_definitions
        SET enabled = CASE WHEN name = 'require-task-before-edit' THEN TRUE ELSE FALSE END
        """
    )

    variable_manager = SessionVariableManager(temp_db)
    variable_manager.merge_variables(
        session_id,
        {
            "task_claimed": False,
            "claimed_tasks": {},
            "require_task_before_edit": True,
            "plan_mode": False,
        },
    )
    return ClaimContext(
        db=temp_db,
        project_id=project.id,
        session_id=session_id,
        task_id=task.id,
        task_ref=f"#{task.seq_num}",
        repo_path=tmp_path,
        task_manager=task_manager,
        session_manager=session_manager,
        variable_manager=variable_manager,
    )


def _handler(context: ClaimContext) -> WorkflowHookHandler:
    return WorkflowHookHandler(
        rule_engine=RuleEngine(context.db),
        task_manager=context.task_manager,
        session_manager=context.session_manager,
    )


def _event(
    context: ClaimContext,
    event_type: HookEventType,
    *,
    tool_name: str | None = None,
    tool_input: dict[str, str] | None = None,
) -> HookEvent:
    data: dict[str, object] = {}
    if tool_name is not None:
        data = {"tool_name": tool_name, "tool_input": tool_input or {}}
    return HookEvent(
        event_type=event_type,
        session_id="claim-reconciliation-external",
        source=SessionSource.CLAUDE,
        timestamp=datetime.now(UTC),
        cwd=str(context.repo_path),
        data=data,
        project_id=context.project_id,
        metadata={
            "_platform_session_id": context.session_id,
            "project_path": str(context.repo_path),
        },
    )


@pytest.mark.asyncio
async def test_session_start_rehydrates_reset_claim_state_from_database(
    claim_context: ClaimContext,
) -> None:
    """Compaction SessionStart restores both session-side claim variables."""
    response = await _handler(claim_context)._evaluate_rules(
        _event(claim_context, HookEventType.SESSION_START)
    )

    variables = claim_context.variable_manager.get_variables(claim_context.session_id)
    assert response.decision == "allow"
    assert variables["task_claimed"] is True
    assert variables["claimed_tasks"] == {
        claim_context.task_id: claim_context.task_ref,
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("tool_name", "tool_input"),
    [
        (
            "Edit",
            {
                "file_path": "src/module.py",
                "old_string": "old",
                "new_string": "new",
            },
        ),
        (
            "Bash",
            {
                "command": (
                    'python3 -c "from pathlib import Path; '
                    "Path('src/module.py').write_text('new')\""
                )
            },
        ),
    ],
)
async def test_live_database_claim_allows_native_and_inline_write_routes(
    claim_context: ClaimContext,
    tool_name: str,
    tool_input: dict[str, str],
) -> None:
    """Both write routes consume the claim hydrated at SessionStart."""
    handler = _handler(claim_context)
    await handler._evaluate_rules(_event(claim_context, HookEventType.SESSION_START))

    response = await handler._evaluate_rules(
        _event(
            claim_context,
            HookEventType.BEFORE_TOOL,
            tool_name=tool_name,
            tool_input=tool_input,
        )
    )

    assert response.decision == "allow"
