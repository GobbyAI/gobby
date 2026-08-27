"""Cross-session commit ownership guard tests."""

from __future__ import annotations

import subprocess
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

import pytest

from gobby.hooks.events import HookEvent, HookEventType, SessionSource
from gobby.mcp_proxy.tools.tasks import create_task_registry
from gobby.storage.definitions.rules import RuleDefinitionManager
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.projects import LocalProjectManager, Project
from gobby.storage.session_models import Session
from gobby.storage.sessions import SessionManager
from gobby.storage.tasks import LocalTaskManager, Task
from gobby.utils.session_context import session_context_for_test
from gobby.workflows.commit_guard import (
    DirtyEditOwnershipInspectionError,
    ForeignPathOwner,
    _format_dirty_edit_reason,
    _format_ref,
    parse_git_commit_invocations,
)
from gobby.workflows.definitions import RuleDefinitionBody
from gobby.workflows.engine.core import RuleEngine
from gobby.workflows.hooks import WorkflowHookHandler
from gobby.workflows.state_manager import SessionVariableManager
from gobby.workflows.sync_rules import get_bundled_rules_path, sync_bundled_rules

pytestmark = pytest.mark.unit

RULE_NAME = "block-cross-session-foreign-staged-commit"
DIRTY_EDIT_RULE_NAME = "block-cross-session-foreign-dirty-edit"

LOCAL_MACHINE_ID = "21000000-0000-4000-8000-000000000002"


@pytest.fixture(autouse=True)
def _local_machine_identity() -> Iterator[None]:
    with patch("gobby.utils.machine_id._cached_machine_id", LOCAL_MACHINE_ID):
        yield


def test_format_ref_preserves_full_uuid_fallback() -> None:
    task_id = "12345678-1234-5678-9abc-123456789abc"

    assert _format_ref(None, task_id) == task_id


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
    db: HubDatabase
    handler: WorkflowHookHandler
    session_manager: SessionManager
    project: Project
    current_session: Session
    foreign_session: Session
    current_task: Task
    foreign_task: Task
    repo: Path

    def event(
        self,
        command: str,
        *,
        session: Session | None = None,
        checkout: Path | None = None,
    ) -> HookEvent:
        selected_session = session or self.current_session
        selected_checkout = checkout or self.repo
        return HookEvent(
            event_type=HookEventType.BEFORE_TOOL,
            session_id=selected_session.external_id,
            source=(
                SessionSource.CODEX
                if selected_session.id == self.current_session.id
                else SessionSource.CLAUDE
            ),
            timestamp=datetime.now(UTC),
            cwd=str(selected_checkout),
            project_id=self.project.id,
            data={
                "tool_name": "functions.exec_command",
                "tool_input": {"cmd": command},
            },
            metadata={
                "_platform_session_id": selected_session.id,
                "project_path": str(selected_checkout),
            },
        )

    def edit_event(
        self,
        file_path: str,
        *,
        checkout: Path | None = None,
        cwd: Path | None = None,
    ) -> HookEvent:
        selected_checkout = checkout or self.repo
        return HookEvent(
            event_type=HookEventType.BEFORE_TOOL,
            session_id=self.current_session.external_id,
            source=SessionSource.CODEX,
            timestamp=datetime.now(UTC),
            cwd=str(cwd or selected_checkout),
            project_id=self.project.id,
            data={
                "tool_name": "Edit",
                "tool_input": {"file_path": file_path},
            },
            metadata={
                "_platform_session_id": self.current_session.id,
                "project_path": str(selected_checkout),
            },
        )

    def foreign_close_event(self) -> HookEvent:
        return HookEvent(
            event_type=HookEventType.BEFORE_TOOL,
            session_id=self.foreign_session.external_id,
            source=SessionSource.CLAUDE,
            timestamp=datetime.now(UTC),
            cwd=str(self.repo),
            project_id=self.project.id,
            data={
                "tool_name": "mcp__gobby__call_tool",
                "tool_input": {
                    "server_name": "gobby-tasks",
                    "tool_name": "close_task",
                    "arguments": {
                        "task_id": f"#{self.foreign_task.seq_num}",
                        "commit_sha": "abc123",
                        "preview": True,
                    },
                },
            },
            metadata={
                "_platform_session_id": self.foreign_session.id,
                "project_path": str(self.repo),
            },
        )


@pytest.fixture
def guard_harness(temp_db: HubDatabase, repo: Path) -> GuardHarness:
    project = LocalProjectManager(temp_db).create("commit-guard-test", repo_path=str(repo))
    session_manager = SessionManager(temp_db)
    current_session = session_manager.register(
        external_id="current-external",
        machine_id="21000000-0000-4000-8000-000000000002",
        source="codex",
        project_id=project.id,
    )
    foreign_session = session_manager.register(
        external_id="foreign-external",
        machine_id="21000000-0000-4000-8000-000000000002",
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
            "task_edited_file_checkouts": {
                current_task.id: {str(repo): ["owned.txt"]},
            },
            "baseline_dirty_files": [],
        },
    )
    variables.merge_variables(
        foreign_session.id,
        {
            "require_commit_before_status": True,
            "loaded_skills": ["tasks"],
            "task_claimed": True,
            "claimed_tasks": {foreign_task.id: f"#{foreign_task.seq_num}"},
            "active_task_id": foreign_task.id,
            "task_edited_files": {foreign_task.id: ["foreign.txt"]},
            "task_edited_file_checkouts": {
                foreign_task.id: {str(repo): ["foreign.txt"]},
            },
            "task_has_commits": True,
        },
    )

    result = sync_bundled_rules(temp_db, get_bundled_rules_path())
    assert result["errors"] == []
    temp_db.execute("UPDATE rule_definitions SET source = 'installed' WHERE source = 'template'")
    temp_db.execute(
        "UPDATE rule_definitions SET enabled = (name IN (%s, %s)) ",
        (RULE_NAME, DIRTY_EDIT_RULE_NAME),
    )

    engine = RuleEngine(temp_db, task_manager=task_manager)
    handler = WorkflowHookHandler(
        rule_engine=engine,
        task_manager=task_manager,
        session_manager=session_manager,
    )
    return GuardHarness(
        db=temp_db,
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
async def test_dirty_foreign_edit_blocks_with_recovery_guidance(
    guard_harness: GuardHarness,
) -> None:
    (guard_harness.repo / "foreign.txt").write_text("foreign change\n", encoding="utf-8")

    response = await guard_harness.handler._evaluate_rules(guard_harness.edit_event("foreign.txt"))

    assert response.decision == "block"
    assert response.reason is not None
    assert "foreign.txt" in response.reason
    assert guard_harness.foreign_session.ref in response.reason
    assert f"#{guard_harness.foreign_task.seq_num}" in response.reason
    assert "gobby-agents.send_message" in response.reason
    assert "buildable WIP commit" in response.reason
    assert "gobby-worktrees" in response.reason
    assert "claim_task" in response.reason
    assert "force=true" in response.reason


def test_dirty_edit_reason_lists_one_reclaim_hint_per_task() -> None:
    reason = _format_dirty_edit_reason(
        {
            ForeignPathOwner(path="docs/a.md", session_ref="#2", task_ref="#7"),
            ForeignPathOwner(path="src/b.py", session_ref="#2", task_ref="#7"),
            ForeignPathOwner(path="src/c.py", session_ref="#3", task_ref="#9"),
        }
    )

    assert "- docs/a.md — session #2, task #7" in reason
    assert "- src/b.py — session #2, task #7" in reason
    assert "- src/c.py — session #3, task #9" in reason
    assert reason.count('claim_task(task_id="#7", force=true)') == 1
    assert reason.count('claim_task(task_id="#9", force=true)') == 1


@pytest.mark.asyncio
async def test_normalized_bash_dirty_foreign_edit_blocks(guard_harness: GuardHarness) -> None:
    (guard_harness.repo / "foreign.txt").write_text("foreign change\n", encoding="utf-8")
    event = guard_harness.event("printf changed > foreign.txt")
    event.data = {
        "tool_name": "Bash",
        "tool_input": {"command": "printf changed > foreign.txt"},
    }

    response = await guard_harness.handler._evaluate_rules(event)

    assert response.decision == "block"
    assert response.reason is not None
    assert "foreign.txt" in response.reason


@pytest.mark.asyncio
async def test_clean_foreign_edit_is_allowed(guard_harness: GuardHarness) -> None:
    response = await guard_harness.handler._evaluate_rules(guard_harness.edit_event("foreign.txt"))

    assert response.decision == "allow"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("path", "contents"),
    [("owned.txt", "owned change\n"), ("unattributed.txt", "new change\n")],
)
async def test_dirty_nonforeign_edit_is_allowed(
    guard_harness: GuardHarness,
    path: str,
    contents: str,
) -> None:
    (guard_harness.repo / path).write_text(contents, encoding="utf-8")

    response = await guard_harness.handler._evaluate_rules(guard_harness.edit_event(path))

    assert response.decision == "allow"


@pytest.mark.asyncio
async def test_dirty_foreign_path_in_other_checkout_is_allowed(
    guard_harness: GuardHarness,
    tmp_path: Path,
) -> None:
    other_checkout = tmp_path / "other-checkout"
    _git(
        guard_harness.repo,
        "worktree",
        "add",
        "-q",
        "-b",
        "dirty-edit-other-checkout",
        str(other_checkout),
    )
    (other_checkout / "foreign.txt").write_text("other checkout\n", encoding="utf-8")

    response = await guard_harness.handler._evaluate_rules(
        guard_harness.edit_event("foreign.txt", checkout=other_checkout)
    )

    assert response.decision == "allow"


@pytest.mark.asyncio
async def test_completed_foreign_session_allows_dirty_edit(guard_harness: GuardHarness) -> None:
    guard_harness.session_manager.update_status(guard_harness.foreign_session.id, "completed")
    (guard_harness.repo / "foreign.txt").write_text("foreign change\n", encoding="utf-8")

    response = await guard_harness.handler._evaluate_rules(guard_harness.edit_event("foreign.txt"))

    assert response.decision == "allow"


@pytest.mark.asyncio
async def test_tool_cwd_relative_dirty_foreign_edit_blocks(guard_harness: GuardHarness) -> None:
    nested = guard_harness.repo / "nested"
    nested.mkdir()
    nested_file = nested / "foreign.txt"
    nested_file.write_text("base\n", encoding="utf-8")
    _git(guard_harness.repo, "add", "--", "nested/foreign.txt")
    _git(guard_harness.repo, "commit", "-q", "-m", "add nested file")
    SessionVariableManager(guard_harness.db).merge_variables(
        guard_harness.foreign_session.id,
        {
            "task_edited_file_checkouts": {
                guard_harness.foreign_task.id: {
                    str(guard_harness.repo): ["nested/foreign.txt"],
                }
            }
        },
    )
    nested_file.write_text("dirty\n", encoding="utf-8")

    response = await guard_harness.handler._evaluate_rules(
        guard_harness.edit_event("foreign.txt", cwd=nested)
    )

    assert response.decision == "block"
    assert response.reason is not None
    assert "nested/foreign.txt" in response.reason


@pytest.mark.asyncio
@pytest.mark.parametrize("event_kind", ["noncanonical", "outside-checkout"])
async def test_skipped_mutations_do_not_inspect_dirty_files(
    guard_harness: GuardHarness,
    tmp_path: Path,
    event_kind: str,
) -> None:
    event = (
        guard_harness.event("gcode search foreign")
        if event_kind == "noncanonical"
        else guard_harness.edit_event(str(tmp_path / "outside.txt"))
    )

    with patch(
        "gobby.workflows.git_utils.get_dirty_files_categorized",
        side_effect=AssertionError("dirty status inspection must be skipped"),
    ):
        response = await guard_harness.handler._evaluate_rules(event)

    assert response.decision == "allow"


@pytest.mark.asyncio
async def test_dirty_edit_ownership_inspection_failure_allows(
    guard_harness: GuardHarness,
) -> None:
    (guard_harness.repo / "foreign.txt").write_text("foreign change\n", encoding="utf-8")

    with patch(
        "gobby.workflows.commit_guard._active_foreign_path_owners",
        side_effect=DirtyEditOwnershipInspectionError("database unavailable"),
    ):
        response = await guard_harness.handler._evaluate_rules(
            guard_harness.edit_event("foreign.txt")
        )

    assert response.decision == "allow"


@pytest.mark.asyncio
async def test_unexpected_dirty_edit_ownership_inspection_failure_propagates(
    guard_harness: GuardHarness,
) -> None:
    (guard_harness.repo / "foreign.txt").write_text("foreign change\n", encoding="utf-8")

    with (
        patch(
            "gobby.workflows.commit_guard._active_foreign_path_owners",
            side_effect=RuntimeError("programming error"),
        ),
        pytest.raises(RuntimeError, match="programming error"),
    ):
        await guard_harness.handler._evaluate_rules(guard_harness.edit_event("foreign.txt"))


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
    assert (
        "gobby-tasks.release_task_paths("
        f'task_id="#{guard_harness.foreign_task.seq_num}", paths=["foreign.txt"])'
        in response.reason
    )
    assert "git commit --only --" in response.reason
    assert set(_git(guard_harness.repo, "diff", "--cached", "--name-only").splitlines()) == {
        "foreign.txt",
        "owned.txt",
    }


@pytest.mark.asyncio
async def test_unscoped_commit_blocks_handoff_ready_owner(
    guard_harness: GuardHarness,
) -> None:
    guard_harness.session_manager.update_status(
        guard_harness.foreign_session.id,
        "handoff_ready",
    )
    (guard_harness.repo / "foreign.txt").write_text("foreign change\n", encoding="utf-8")
    _git(guard_harness.repo, "add", "--", "foreign.txt")

    response = await guard_harness.handler._evaluate_rules(
        guard_harness.event("git commit -m 'unsafe during handoff'")
    )

    assert response.decision == "block"
    assert response.reason is not None
    assert "foreign.txt" in response.reason
    assert guard_harness.foreign_session.ref in response.reason
    assert f"#{guard_harness.foreign_task.seq_num}" in response.reason


@pytest.mark.asyncio
async def test_unscoped_commit_reports_every_foreign_staged_path(
    guard_harness: GuardHarness,
) -> None:
    foreign_paths = [f"foreign-{index}.txt" for index in range(6)]
    for path in foreign_paths:
        (guard_harness.repo / path).write_text(f"foreign change for {path}\n", encoding="utf-8")
    _git(guard_harness.repo, "add", "--", *foreign_paths)
    SessionVariableManager(guard_harness.db).merge_variables(
        guard_harness.foreign_session.id,
        {
            "task_edited_files": {guard_harness.foreign_task.id: foreign_paths},
            "task_edited_file_checkouts": {
                guard_harness.foreign_task.id: {
                    str(guard_harness.repo): foreign_paths,
                }
            },
        },
    )

    response = await guard_harness.handler._evaluate_rules(
        guard_harness.event("git commit -m 'unsafe shared-index commit'")
    )

    assert response.decision == "block"
    assert response.reason is not None
    assert all(path in response.reason for path in foreign_paths)
    assert guard_harness.foreign_session.ref in response.reason
    assert f"#{guard_harness.foreign_task.seq_num}" in response.reason
    assert set(_git(guard_harness.repo, "diff", "--cached", "--name-only").splitlines()) == set(
        foreign_paths
    )


@pytest.mark.asyncio
async def test_same_relative_path_commits_from_separate_worktrees(
    guard_harness: GuardHarness,
    tmp_path: Path,
) -> None:
    foreign_checkout = tmp_path / "foreign-checkout"
    _git(
        guard_harness.repo,
        "worktree",
        "add",
        "-q",
        "-b",
        "foreign-checkout",
        str(foreign_checkout),
    )
    variables = SessionVariableManager(guard_harness.db)
    variables.merge_variables(
        guard_harness.current_session.id,
        {
            "task_edited_files": {guard_harness.current_task.id: ["foreign.txt"]},
            "task_edited_file_checkouts": {
                guard_harness.current_task.id: {
                    str(guard_harness.repo): ["foreign.txt"],
                }
            },
        },
    )
    variables.merge_variables(
        guard_harness.foreign_session.id,
        {
            "task_edited_files": {guard_harness.foreign_task.id: ["foreign.txt"]},
            "task_edited_file_checkouts": {
                guard_harness.foreign_task.id: {
                    str(foreign_checkout): ["foreign.txt"],
                }
            },
        },
    )

    (guard_harness.repo / "foreign.txt").write_text("current checkout\n", encoding="utf-8")
    (foreign_checkout / "foreign.txt").write_text("foreign checkout\n", encoding="utf-8")
    _git(guard_harness.repo, "add", "--", "foreign.txt")
    _git(foreign_checkout, "add", "--", "foreign.txt")

    current_response = await guard_harness.handler._evaluate_rules(
        guard_harness.event("git commit -m 'current checkout'")
    )
    foreign_response = await guard_harness.handler._evaluate_rules(
        guard_harness.event(
            "git commit -m 'foreign checkout'",
            session=guard_harness.foreign_session,
            checkout=foreign_checkout,
        )
    )

    assert current_response.decision == "allow"
    assert foreign_response.decision == "allow"
    _git(guard_harness.repo, "commit", "-q", "-m", "current checkout")
    _git(foreign_checkout, "commit", "-q", "-m", "foreign checkout")
    assert _git(guard_harness.repo, "show", "HEAD:foreign.txt") == "current checkout"
    assert _git(foreign_checkout, "show", "HEAD:foreign.txt") == "foreign checkout"


@pytest.mark.asyncio
async def test_owned_path_only_commit_succeeds_and_preserves_foreign_index_entry(
    guard_harness: GuardHarness,
) -> None:
    (guard_harness.repo / "owned.txt").write_text("current change\n", encoding="utf-8")
    (guard_harness.repo / "foreign.txt").write_text("foreign change\n", encoding="utf-8")
    (guard_harness.repo / "unowned.txt").write_text("unowned change\n", encoding="utf-8")
    _git(guard_harness.repo, "add", "--", "owned.txt", "foreign.txt", "unowned.txt")
    command = "git commit --only -m 'scoped' -- owned.txt"

    response = await guard_harness.handler._evaluate_rules(guard_harness.event(command))
    assert response.decision == "allow"

    _git(guard_harness.repo, "commit", "--only", "-m", "scoped", "--", "owned.txt")

    assert _git(guard_harness.repo, "show", "HEAD:owned.txt") == "current change"
    assert _git(guard_harness.repo, "show", "HEAD:foreign.txt") == "base foreign"
    assert set(_git(guard_harness.repo, "diff", "--cached", "--name-only").splitlines()) == {
        "foreign.txt",
        "unowned.txt",
    }


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
async def test_owner_path_release_breaks_commit_and_close_cycle(
    guard_harness: GuardHarness,
) -> None:
    guard_harness.db.execute(
        "UPDATE rule_definitions SET enabled = (name IN (%s, %s)) ",
        (RULE_NAME, "require-clean-tree-before-status"),
    )
    (guard_harness.repo / "foreign.txt").write_text("current session change\n", encoding="utf-8")
    _git(guard_harness.repo, "add", "--", "foreign.txt")

    commit_response = await guard_harness.handler._evaluate_rules(
        guard_harness.event("git commit -m 'blocked by stale attribution'")
    )
    close_response = await guard_harness.handler._evaluate_rules(
        guard_harness.foreign_close_event()
    )

    assert commit_response.decision == "block"
    assert close_response.decision == "block"

    variables = SessionVariableManager(guard_harness.db)
    variables.merge_variables(
        guard_harness.foreign_session.id,
        {
            "task_edited_files": {
                guard_harness.foreign_task.id: ["foreign.txt"],
            }
        },
    )
    released, remaining = variables.release_task_edited_files(
        guard_harness.foreign_session.id,
        guard_harness.foreign_task.id,
        ["foreign.txt"],
        checkout_root=str(guard_harness.repo),
    )

    assert released == ["foreign.txt"]
    assert remaining == []
    commit_response = await guard_harness.handler._evaluate_rules(
        guard_harness.event("git commit -m 'released by owner'")
    )
    close_response = await guard_harness.handler._evaluate_rules(
        guard_harness.foreign_close_event()
    )
    assert commit_response.decision == "allow"
    assert close_response.decision == "allow"


@pytest.mark.asyncio
async def test_21049_owner_release_allows_close_when_later_dirt_is_foreign(
    guard_harness: GuardHarness,
) -> None:
    guard_harness.db.execute(
        "UPDATE rule_definitions SET enabled = (name IN (%s, %s)) ",
        (RULE_NAME, "require-clean-tree-before-status"),
    )
    last_commit_epoch = int(
        _git(guard_harness.repo, "log", "-1", "--format=%ct", "--", "foreign.txt")
    )
    variables = SessionVariableManager(guard_harness.db)
    variables.merge_variables(
        guard_harness.foreign_session.id,
        {
            "task_edited_file_times": {
                guard_harness.foreign_task.id: {"foreign.txt": last_commit_epoch - 100},
            }
        },
    )
    variables.merge_variables(
        guard_harness.current_session.id,
        {
            "task_edited_files": {
                guard_harness.current_task.id: ["owned.txt", "foreign.txt"],
            },
            "task_edited_file_checkouts": {
                guard_harness.current_task.id: {
                    str(guard_harness.repo): ["owned.txt", "foreign.txt"],
                },
            },
        },
    )
    (guard_harness.repo / "foreign.txt").write_text(
        "later dirt from current session\n",
        encoding="utf-8",
    )

    blocked_close = await guard_harness.handler._evaluate_rules(guard_harness.foreign_close_event())
    assert blocked_close.decision == "block"

    registry = create_task_registry(LocalTaskManager(guard_harness.db))
    with session_context_for_test(guard_harness.foreign_session.id):
        released = await registry.call(
            "release_task_paths",
            {
                "task_id": guard_harness.foreign_task.id,
                "paths": ["foreign.txt"],
            },
        )

    assert released["success"] is True
    assert released["released_paths"] == ["foreign.txt"]
    assert released["foreign_dirty_paths"] == {
        "foreign.txt": [
            {
                "task": f"#{guard_harness.current_task.seq_num}",
                "session": f"#{guard_harness.current_session.seq_num}",
            }
        ]
    }
    assert guard_harness.foreign_task.id not in variables.get_variables(
        guard_harness.foreign_session.id
    ).get("task_edited_files", {})

    allowed_close = await guard_harness.handler._evaluate_rules(guard_harness.foreign_close_event())
    assert allowed_close.decision == "allow"


@pytest.mark.asyncio
async def test_successful_owner_commit_releases_clean_checkout_attribution(
    guard_harness: GuardHarness,
) -> None:
    (guard_harness.repo / "owned.txt").write_text("committed by owner\n", encoding="utf-8")
    _git(guard_harness.repo, "add", "--", "owned.txt")
    output = _git(
        guard_harness.repo,
        "commit",
        "--only",
        "-m",
        "owner commit",
        "--",
        "owned.txt",
    )
    event = guard_harness.event("git commit --only -m 'owner commit' -- owned.txt")
    event.event_type = HookEventType.AFTER_TOOL
    event.data["tool_output"] = output
    event.metadata["is_failure"] = False

    response = await guard_harness.handler._evaluate_rules(event)

    assert response.decision == "allow"
    variables = SessionVariableManager(guard_harness.db).get_variables(
        guard_harness.current_session.id
    )
    assert guard_harness.current_task.id not in variables.get("task_edited_files", {})
    assert guard_harness.current_task.id not in variables.get("task_edited_file_checkouts", {})


@pytest.mark.asyncio
async def test_release_refuses_dirty_paths(guard_harness: GuardHarness) -> None:
    dirty_paths = [":(glob)literal.py", "literal[abc]*.py"]
    for path in dirty_paths:
        (guard_harness.repo / path).write_text("uncommitted\n", encoding="utf-8")

    variables = SessionVariableManager(guard_harness.db)
    variables.merge_variables(
        guard_harness.foreign_session.id,
        {"task_edited_files": {guard_harness.foreign_task.id: dirty_paths}},
    )
    registry = create_task_registry(LocalTaskManager(guard_harness.db))

    with session_context_for_test(guard_harness.foreign_session.id):
        result = await registry.call(
            "release_task_paths",
            {
                "task_id": guard_harness.foreign_task.id,
                "paths": dirty_paths,
            },
        )

    assert result == {
        "success": False,
        "status": "error",
        "error": (
            "Cannot release paths whose uncommitted content may be this task's own "
            "work (no recorded edit, or the newest recorded edit is newer than the "
            "last commit touching the path); commit or revert it first "
            "(git stash is blocked for interactive sessions)"
        ),
        "error_code": "TASK_INVALID_STATUS",
        "dirty_paths": dirty_paths,
    }
    assert (
        variables.get_variables(guard_harness.foreign_session.id)["task_edited_files"][
            guard_harness.foreign_task.id
        ]
        == dirty_paths
    )


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
    temp_db.execute("UPDATE rule_definitions SET source = 'installed' WHERE source = 'template'")

    row = RuleDefinitionManager(temp_db).get_by_name(RULE_NAME)

    assert row is not None
    body = RuleDefinitionBody.model_validate(row.definition_json)
    assert body.event.value == "before_tool"
    assert body.group == "task-enforcement"
    assert body.when == "foreign_staged_commit_conflict"
    assert [effect.type for effect in body.resolved_effects] == ["block"]


@pytest.mark.parametrize(
    ("case", "command", "expected"),
    [
        (
            "heredoc-quoted-tag",
            "git commit -F - <<'MSG'\nfix: thing\n\nthe row -- abandonment alike\n"
            "audit / quality audit\nMSG\n",
            [()],
        ),
        (
            "heredoc-unquoted-tag",
            "git commit -F - <<MSG\nfix: thing\n\nthe row -- abandonment alike\n"
            "audit / quality audit\nMSG\n",
            [()],
        ),
        ("heredoc-dash-variant", "git commit -F - <<-MSG\n\tbody -- src/a.py\n\tMSG\n", [()]),
        (
            "heredoc-then-path-scoped-commit",
            "git commit -F - <<'MSG'\nbody -- x/y\nMSG\ngit commit -- src/a.py",
            [(), ("src/a.py",)],
        ),
    ],
    ids=["quoted-tag", "unquoted-tag", "dash-variant", "chained"],
)
def test_parse_ignores_heredoc_bodies(
    case: str, command: str, expected: list[tuple[str, ...]]
) -> None:
    """A heredoc body is unquoted text; `--` in a commit message is not a pathspec delimiter."""
    assert [i.pathspecs for i in parse_git_commit_invocations(command)] == expected


@pytest.mark.parametrize(
    ("command", "expected"),
    [
        ("git commit -m x\ngit commit -- src/a.py", [(), ("src/a.py",)]),
        ("git commit -m x ; git commit -- src/a.py", [(), ("src/a.py",)]),
        ("git commit -m x && git commit -- src/a.py", [(), ("src/a.py",)]),
        ("git commit -m x\ngit commit -m y", [(), ()]),
        ("git commit \\\n  -- src/a.py", [("src/a.py",)]),
    ],
    ids=["newline", "semicolon", "andand", "two-unscoped", "line-continuation"],
)
def test_parse_separates_newline_chained_commits(
    command: str, expected: list[tuple[str, ...]]
) -> None:
    """A merged parse would inherit the later pathspecs and skip the unscoped commit's check."""
    assert [i.pathspecs for i in parse_git_commit_invocations(command)] == expected


def test_parse_keeps_explicit_pathspecs_and_quoted_messages() -> None:
    assert [
        i.pathspecs for i in parse_git_commit_invocations("git commit -- src/a.py tests/b.py")
    ] == [("src/a.py", "tests/b.py")]
    assert [
        i.pathspecs for i in parse_git_commit_invocations("git commit -m 'row -- a/b and c/d'")
    ] == [()]
