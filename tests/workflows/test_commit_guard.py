"""Cross-session commit ownership guard tests."""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import pytest

from gobby.hooks.events import HookEvent, HookEventType, SessionSource
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.projects import LocalProjectManager, Project
from gobby.storage.session_models import Session
from gobby.storage.sessions import SessionManager
from gobby.storage.tasks import LocalTaskManager, Task
from gobby.storage.workflow_definitions import LocalWorkflowDefinitionManager
from gobby.workflows.definitions import RuleDefinitionBody
from gobby.workflows.engine.core import RuleEngine
from gobby.workflows.hooks import WorkflowHookHandler
from gobby.workflows.state_manager import SessionVariableManager
from gobby.workflows.sync_rules import get_bundled_rules_path, sync_bundled_rules

pytestmark = pytest.mark.unit

RULE_NAME = "block-cross-session-foreign-staged-commit"


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    repo_path = tmp_path / "repo"
    repo_path.mkdir()
    _git(repo_path, "init", "-q")
    _git(repo_path, "config", "user.email", "tests@gobby.local")
    _git(repo_path, "config", "user.name", "Gobby Tests")
    (repo_path / "owned.txt").write_text("base owned\n", encoding="utf-8")
    (repo_path / "foreign.txt").write_text("base foreign\n", encoding="utf-8")
    _git(repo_path, "add", "--", "owned.txt", "foreign.txt")
    _git(repo_path, "commit", "-q", "-m", "base")
    return repo_path


@dataclass(frozen=True)
class GuardHarness:
    handler: WorkflowHookHandler
    session_manager: SessionManager
    project: Project
    current_session: Session
    foreign_session: Session
    current_task: Task
    foreign_task: Task
    repo: Path

    def event(self, command: str) -> HookEvent:
        return HookEvent(
            event_type=HookEventType.BEFORE_TOOL,
            session_id=self.current_session.external_id,
            source=SessionSource.CODEX,
            timestamp=datetime.now(UTC),
            cwd=str(self.repo),
            project_id=self.project.id,
            data={
                "tool_name": "functions.exec_command",
                "tool_input": {"cmd": command},
            },
            metadata={
                "_platform_session_id": self.current_session.id,
                "project_path": str(self.repo),
            },
        )


@pytest.fixture
def guard_harness(temp_db: HubDatabase, repo: Path) -> GuardHarness:
    project = LocalProjectManager(temp_db).create("commit-guard-test", repo_path=str(repo))
    session_manager = SessionManager(temp_db)
    current_session = session_manager.register(
        external_id="current-external",
        machine_id="test-machine",
        source="codex",
        project_id=project.id,
    )
    foreign_session = session_manager.register(
        external_id="foreign-external",
        machine_id="test-machine",
        source="claude",
        project_id=project.id,
    )

    task_manager = LocalTaskManager(temp_db)
    current_task = task_manager.create_task(
        project_id=project.id,
        title="Current task",
        task_type="bug",
        category="code",
        implementation_domain="backend",
        validation_criteria="Current task paths commit.",
        claimed_by_session_id=current_session.id,
    )
    foreign_task = task_manager.create_task(
        project_id=project.id,
        title="Foreign task",
        task_type="bug",
        category="code",
        implementation_domain="backend",
        validation_criteria="Foreign task paths remain isolated.",
        claimed_by_session_id=foreign_session.id,
    )

    variables = SessionVariableManager(temp_db)
    variables.merge_variables(
        current_session.id,
        {
            "claimed_tasks": {current_task.id: f"#{current_task.seq_num}"},
            "active_task_id": current_task.id,
            "task_edited_files": {current_task.id: ["owned.txt"]},
        },
    )
    variables.merge_variables(
        foreign_session.id,
        {
            "claimed_tasks": {foreign_task.id: f"#{foreign_task.seq_num}"},
            "active_task_id": foreign_task.id,
            "task_edited_files": {foreign_task.id: ["foreign.txt"]},
        },
    )

    result = sync_bundled_rules(temp_db, get_bundled_rules_path())
    assert result["errors"] == []
    temp_db.execute(
        "UPDATE workflow_definitions SET source = 'installed' WHERE source = 'template'"
    )
    temp_db.execute(
        "UPDATE workflow_definitions SET enabled = (name = %s) WHERE workflow_type = 'rule'",
        (RULE_NAME,),
    )

    engine = RuleEngine(temp_db, task_manager=task_manager)
    handler = WorkflowHookHandler(
        rule_engine=engine,
        task_manager=task_manager,
        session_manager=session_manager,
    )
    return GuardHarness(
        handler=handler,
        session_manager=session_manager,
        project=project,
        current_session=current_session,
        foreign_session=foreign_session,
        current_task=current_task,
        foreign_task=foreign_task,
        repo=repo,
    )


@pytest.mark.asyncio
async def test_unscoped_commit_blocks_foreign_staged_path_with_owner_diagnostic(
    guard_harness: GuardHarness,
) -> None:
    (guard_harness.repo / "owned.txt").write_text("current change\n", encoding="utf-8")
    (guard_harness.repo / "foreign.txt").write_text("foreign change\n", encoding="utf-8")
    _git(guard_harness.repo, "add", "--", "owned.txt", "foreign.txt")

    response = await guard_harness.handler._evaluate_rules(
        guard_harness.event("git commit -m 'unsafe'")
    )

    assert response.decision == "block"
    assert response.reason is not None
    assert "foreign.txt" in response.reason
    assert guard_harness.foreign_session.ref in response.reason
    assert f"#{guard_harness.foreign_task.seq_num}" in response.reason
    assert "gobby-agents.send_message" in response.reason
    assert "git commit --only --" in response.reason
    assert set(_git(guard_harness.repo, "diff", "--cached", "--name-only").splitlines()) == {
        "foreign.txt",
        "owned.txt",
    }


@pytest.mark.asyncio
async def test_owned_path_only_commit_succeeds_and_preserves_foreign_index_entry(
    guard_harness: GuardHarness,
) -> None:
    (guard_harness.repo / "owned.txt").write_text("current change\n", encoding="utf-8")
    (guard_harness.repo / "foreign.txt").write_text("foreign change\n", encoding="utf-8")
    _git(guard_harness.repo, "add", "--", "owned.txt", "foreign.txt")
    command = "git commit --only -m 'scoped' -- owned.txt"

    response = await guard_harness.handler._evaluate_rules(guard_harness.event(command))
    assert response.decision == "allow"

    _git(guard_harness.repo, "commit", "--only", "-m", "scoped", "--", "owned.txt")

    assert _git(guard_harness.repo, "show", "HEAD:owned.txt") == "current change"
    assert _git(guard_harness.repo, "show", "HEAD:foreign.txt") == "base foreign"
    assert _git(guard_harness.repo, "diff", "--cached", "--name-only") == "foreign.txt"


@pytest.mark.asyncio
async def test_foreign_path_only_commit_is_blocked(guard_harness: GuardHarness) -> None:
    (guard_harness.repo / "foreign.txt").write_text("foreign change\n", encoding="utf-8")
    _git(guard_harness.repo, "add", "--", "foreign.txt")

    response = await guard_harness.handler._evaluate_rules(
        guard_harness.event("git commit --only -m 'wrong owner' -- foreign.txt")
    )

    assert response.decision == "block"
    assert response.reason is not None
    assert "foreign.txt" in response.reason
    assert _git(guard_harness.repo, "diff", "--cached", "--name-only") == "foreign.txt"


@pytest.mark.asyncio
async def test_completed_foreign_session_does_not_block_commit(
    guard_harness: GuardHarness,
) -> None:
    guard_harness.session_manager.update_status(
        guard_harness.foreign_session.id,
        "completed",
    )
    (guard_harness.repo / "foreign.txt").write_text("foreign change\n", encoding="utf-8")
    _git(guard_harness.repo, "add", "--", "foreign.txt")

    response = await guard_harness.handler._evaluate_rules(
        guard_harness.event("git commit -m 'active owners only'")
    )

    assert response.decision == "allow"


def test_commit_guard_rule_syncs_and_validates(temp_db: HubDatabase) -> None:
    result = sync_bundled_rules(temp_db, get_bundled_rules_path())
    assert result["errors"] == []
    temp_db.execute(
        "UPDATE workflow_definitions SET source = 'installed' WHERE source = 'template'"
    )

    row = LocalWorkflowDefinitionManager(temp_db).get_by_name(RULE_NAME)

    assert row is not None
    body = RuleDefinitionBody.model_validate_json(row.definition_json)
    assert body.event.value == "before_tool"
    assert body.group == "task-enforcement"
    assert body.when == "foreign_staged_commit_conflict"
    assert [effect.type for effect in body.resolved_effects] == ["block"]
