"""Foreign managed-branch recognition for review freshness."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from gobby.hooks.events import HookEvent, HookEventType, SessionSource
from gobby.storage.clones import LocalCloneManager
from gobby.storage.definitions.rules import RuleDefinitionManager
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.session_models import Session
from gobby.storage.sessions import SessionManager
from gobby.storage.worktrees import LocalWorktreeManager
from gobby.workflows.code_review_freshness import (
    is_foreign_landing_merge,
    merge_branch_names,
)
from gobby.workflows.engine.core import RuleEngine
from gobby.workflows.hooks import WorkflowHookHandler
from gobby.workflows.state_manager import SessionVariableManager
from gobby.workflows.sync_rules import get_bundled_rules_path, sync_bundled_rules
from tests.fixtures.isolated_checkout import patch_local_machine_id

pytestmark = pytest.mark.unit

MACHINE_ID = "22000000-0000-4000-8000-000000000002"


@pytest.fixture(autouse=True)
def _local_machine_identity(monkeypatch: pytest.MonkeyPatch) -> None:
    patch_local_machine_id(monkeypatch, MACHINE_ID)


@pytest.fixture
def sessions(
    temp_db: HubDatabase,
    sample_project: dict[str, object],
) -> tuple[Session, Session]:
    manager = SessionManager(temp_db)
    project_id = str(sample_project["id"])
    current = manager.register(
        external_id="review-freshness-current",
        machine_id=MACHINE_ID,
        source="codex",
        project_id=project_id,
    )
    foreign = manager.register(
        external_id="review-freshness-foreign",
        machine_id=MACHINE_ID,
        source="claude",
        project_id=project_id,
    )
    return current, foreign


@pytest.mark.parametrize(
    ("command", "expected"),
    [
        ("git merge --no-ff task-1", ("task-1",)),
        ('GIT_AUTHOR_NAME=x git --no-pager merge -m "land" -- task-1', ("task-1",)),
        ("git merge refs/heads/task-1", ("refs/heads/task-1",)),
        ("git merge --abort", ()),
        ("git merge task-1 task-2", ()),
        ("git merge task-1 && git commit -m followup", ()),
        ("git merge-base HEAD main", ()),
        ("git cherry-pick abc123", ()),
    ],
)
def test_merge_branch_names(command: str, expected: tuple[str, ...]) -> None:
    assert merge_branch_names(command) == expected


@pytest.mark.parametrize("workspace_kind", ["worktree", "clone"])
def test_foreign_managed_branch_is_a_landing_merge(
    workspace_kind: str,
    temp_db: HubDatabase,
    sample_project: dict[str, object],
    sessions: tuple[Session, Session],
    tmp_path: Path,
) -> None:
    current, foreign = sessions
    project_id = str(sample_project["id"])
    branch = f"task-foreign-{workspace_kind}"
    if workspace_kind == "worktree":
        LocalWorktreeManager(temp_db).create(
            project_id=project_id,
            branch_name=branch,
            worktree_path=str(tmp_path / workspace_kind),
            agent_session_id=foreign.id,
        )
    else:
        LocalCloneManager(temp_db).create(
            project_id=project_id,
            branch_name=branch,
            clone_path=str(tmp_path / workspace_kind),
            agent_session_id=foreign.id,
        )

    assert (
        is_foreign_landing_merge(
            temp_db,
            f"git merge --no-ff {branch}",
            session_id=current.id,
            project_id=project_id,
        )
        is True
    )


def test_owned_or_unmanaged_merge_still_spends_freshness(
    temp_db: HubDatabase,
    sample_project: dict[str, object],
    sessions: tuple[Session, Session],
    tmp_path: Path,
) -> None:
    current, _foreign = sessions
    project_id = str(sample_project["id"])
    LocalWorktreeManager(temp_db).create(
        project_id=project_id,
        branch_name="task-owned",
        worktree_path=str(tmp_path / "owned"),
        agent_session_id=current.id,
    )

    assert (
        is_foreign_landing_merge(
            temp_db,
            "git merge --no-ff task-owned",
            session_id=current.id,
            project_id=project_id,
        )
        is False
    )
    assert (
        is_foreign_landing_merge(
            temp_db,
            "git merge --no-ff ordinary-branch",
            session_id=current.id,
            project_id=project_id,
        )
        is False
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("workspace_kind", ["worktree", "clone"])
async def test_foreign_landing_does_not_spend_installed_rule_freshness(
    workspace_kind: str,
    temp_db: HubDatabase,
    sample_project: dict[str, object],
    sessions: tuple[Session, Session],
    tmp_path: Path,
) -> None:
    current, foreign = sessions
    project_id = str(sample_project["id"])
    branch = f"task-hook-{workspace_kind}"
    if workspace_kind == "worktree":
        LocalWorktreeManager(temp_db).create(
            project_id=project_id,
            branch_name=branch,
            worktree_path=str(tmp_path / workspace_kind),
            agent_session_id=foreign.id,
        )
    else:
        LocalCloneManager(temp_db).create(
            project_id=project_id,
            branch_name=branch,
            clone_path=str(tmp_path / workspace_kind),
            agent_session_id=foreign.id,
        )

    result = sync_bundled_rules(temp_db, get_bundled_rules_path())
    assert result["errors"] == []
    temp_db.execute("UPDATE rule_definitions SET source = 'installed' WHERE source = 'template'")
    temp_db.execute(
        "UPDATE rule_definitions SET enabled = (name = %s)",
        ("clear-ocr-review-freshness",),
    )
    installed = RuleDefinitionManager(temp_db).get_by_name("clear-ocr-review-freshness")
    assert installed is not None
    assert installed.source == "installed"

    variables = SessionVariableManager(temp_db)
    variables.merge_variables(
        current.id,
        {"_variable_defaults_loaded": True, "code_review_fresh": True},
    )
    event = HookEvent(
        event_type=HookEventType.AFTER_TOOL,
        session_id=current.external_id,
        source=SessionSource.CODEX,
        timestamp=datetime.now(UTC),
        project_id=project_id,
        cwd=str(tmp_path),
        metadata={"_platform_session_id": current.id, "is_failure": False},
        data={
            "tool_name": "Bash",
            "tool_input": {"command": f"git merge --no-ff {branch}"},
            "is_error": False,
        },
    )

    await WorkflowHookHandler(
        rule_engine=RuleEngine(temp_db),
        session_manager=SessionManager(temp_db),
    )._evaluate_rules(event)

    assert variables.get_variables(current.id)["code_review_fresh"] is True
