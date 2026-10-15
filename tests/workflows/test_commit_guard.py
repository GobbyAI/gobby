"""Cross-session commit ownership guard tests."""

from __future__ import annotations

import shlex
import shutil
import subprocess
from collections.abc import Collection, Iterator, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

import psycopg
import pytest
from psycopg_pool import PoolTimeout

from gobby.hooks.events import HookEvent, HookEventType, SessionSource
from gobby.storage.definitions.rules import RuleDefinitionManager
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.projects import Project
from gobby.storage.session_models import Session
from gobby.storage.sessions import SessionManager
from gobby.storage.tasks import LocalTaskManager, Task
from gobby.utils.daemon_git import GitResult, GitTimeout, daemon_git
from gobby.workflows.commit_guard import (
    DirtyEditOwnershipInspectionError,
    active_owned_dirty_paths,
    foreign_dirty_edit_conflict,
    foreign_owned_dirty_paths,
    foreign_staged_commit_conflict,
)
from gobby.workflows.commit_guard_conflicts import (
    ForeignPathOwner,
    format_dirty_edit_reason,
    format_ref,
)
from gobby.workflows.definitions import RuleDefinitionBody
from gobby.workflows.engine.core import RuleEngine
from gobby.workflows.git_commit_parsing import parse_git_commit_invocations
from gobby.workflows.hooks import WorkflowHookHandler
from gobby.workflows.state_manager import SessionVariableManager
from gobby.workflows.sync_rules import get_bundled_rules_path, sync_bundled_rules
from tests.fixtures.isolated_checkout import IsolatedCheckoutFactory

pytestmark = pytest.mark.unit

RULE_NAME = "block-cross-session-foreign-staged-commit"
DIRTY_EDIT_RULE_NAME = "block-cross-session-foreign-dirty-edit"

LOCAL_MACHINE_ID = "21000000-0000-4000-8000-000000000002"


@pytest.mark.asyncio
async def test_rule3_refuses_dirty_first_touch_naming_owner(guard_harness: GuardHarness) -> None:
    h = guard_harness
    (h.repo / "unowned.txt").write_text("external edit\n", encoding="utf-8")
    result = await h.handler._evaluate_rules(h.edit_event("unowned.txt"))
    assert result.decision == "block"
    assert result.reason is not None
    assert "no live session owns it" in result.reason
    variables = SessionVariableManager(h.db)
    variables.merge_variables(
        h.foreign_session.id,
        {
            "session_dirty_files": ["unowned.txt"],
            "session_dirty_file_checkouts": {str(h.repo): ["unowned.txt"]},
            "task_edited_file_checkouts": {h.foreign_task.id: {str(h.repo): ["unowned.txt"]}},
        },
    )
    result = await h.handler._evaluate_rules(h.edit_event("unowned.txt"))
    assert result.decision == "block"
    assert result.reason is not None
    assert h.foreign_session.ref in result.reason
    assert f"task #{h.foreign_task.seq_num}" in result.reason
    variables.merge_variables(h.foreign_session.id, {"claimed_tasks": {}})
    result = await h.handler._evaluate_rules(h.edit_event("unowned.txt"))
    assert result.reason is not None
    assert "no claimed task" in result.reason


@pytest.mark.asyncio
async def test_rule3_names_live_owner_from_another_project(
    guard_harness: GuardHarness,
    isolated_checkout_factory: IsolatedCheckoutFactory,
) -> None:
    h = guard_harness
    other = isolated_checkout_factory(h.db, "other-owner-project").project
    owner = h.session_manager.register(
        external_id="other-project-owner",
        machine_id=LOCAL_MACHINE_ID,
        source="claude",
        project_id=other.id,
    )
    SessionVariableManager(h.db).merge_variables(
        owner.id,
        {
            "session_dirty_files": ["cross-project.txt"],
            "session_dirty_file_checkouts": {str(h.repo): ["cross-project.txt"]},
        },
    )
    (h.repo / "cross-project.txt").write_text("other session work\n", encoding="utf-8")
    response = await h.handler._evaluate_rules(h.edit_event("cross-project.txt"))
    assert response.decision == "block"
    assert response.reason is not None
    assert owner.ref in response.reason
    assert "cross-project.txt" in response.reason
    assert "no claimed task" in response.reason


@pytest.mark.asyncio
async def test_rule3_allows_owned_and_clean_paths_and_releases_stale_holder(
    guard_harness: GuardHarness,
) -> None:
    h = guard_harness
    (h.repo / "owned.txt").write_text("own dirt\n", encoding="utf-8")
    with patch("gobby.workflows.commit_guard.task_dirty_paths_async") as status:
        result = await h.handler._evaluate_rules(h.edit_event("owned.txt"))
    assert result.decision == "allow"
    status.assert_not_called()
    result = await h.handler._evaluate_rules(h.edit_event("foreign.txt"))
    assert result.decision == "allow"
    foreign = SessionVariableManager(h.db).get_variables(h.foreign_session.id)
    assert "foreign.txt" not in foreign["session_dirty_files"]
    assert foreign["task_edited_file_checkouts"][h.foreign_task.id][str(h.repo)] == ["foreign.txt"]


@pytest.mark.asyncio
async def test_rule3_checks_write_paths_of_write_kind_calls_only(
    guard_harness: GuardHarness,
) -> None:
    h = guard_harness
    (h.repo / "foreign.txt").write_text("dirty source\n", encoding="utf-8")
    for command in (
        "git add foreign.txt",
        "git restore --staged foreign.txt",
        "git restore -S -- foreign.txt",
        "git switch --detach HEAD",
        "cp foreign.txt clean.txt",
    ):
        result = await h.handler._evaluate_rules(h.event(command))
        assert result.decision == "allow", (command, result.reason)
    with patch(
        "gobby.workflows.commit_guard.task_dirty_paths_async", new=AsyncMock(return_value=None)
    ):
        result = await h.handler._evaluate_rules(h.edit_event("unknown.txt"))
    assert result.decision == "block"
    assert result.reason is not None
    assert "could not verify" in result.reason


@pytest.mark.asyncio
async def test_rule2_blocks_only_other_live_sessions_dirty_paths(
    guard_harness: GuardHarness,
) -> None:
    h = guard_harness
    variables = SessionVariableManager(h.db)
    for path in ("owned.txt", "unowned.txt", "foreign.txt"):
        (h.repo / path).write_text("dirt\n", encoding="utf-8")
        _git(h.repo, "add", path)
    for path in ("owned.txt", "unowned.txt"):
        result = await h.handler._evaluate_rules(h.event(f"git commit --only -m own -- {path}"))
        assert result.decision == "allow"
    variables.merge_variables(h.foreign_session.id, {"claimed_tasks": {}})
    result = await h.handler._evaluate_rules(h.event("git commit -m all"))
    assert result.decision == "block"
    assert result.reason is not None
    assert "rule 2" in result.reason
    assert "no claimed task" in result.reason
    assert "release_task_paths" not in result.reason


@pytest.mark.asyncio
async def test_rule2_own_ledger_read_failure_blocks(guard_harness: GuardHarness) -> None:
    h = guard_harness
    (h.repo / "foreign.txt").write_text("foreign change\n", encoding="utf-8")
    _git(h.repo, "add", "--", "foreign.txt")
    original = SessionVariableManager.get_variables

    def read(manager: SessionVariableManager, session_id: str) -> dict[str, Any]:
        if session_id == h.current_session.id:
            raise PoolTimeout("own ledger unavailable")
        return original(manager, session_id)

    with patch.object(SessionVariableManager, "get_variables", read):
        reason = await foreign_staged_commit_conflict(
            h.db,
            h.event("git commit -m x"),
            session_id=h.current_session.id,
            project_id=h.project.id,
            project_path=str(h.repo),
        )
    assert "could not verify staged-path ownership" in reason
    assert "own ledger unavailable" in reason


def _start_rename_merge(h: GuardHarness) -> None:
    _git(h.repo, "checkout", "-b", "incoming")
    _git(h.repo, "mv", "foreign.txt", "renamed.txt")
    _git(h.repo, "commit", "-m", "rename")
    _git(h.repo, "checkout", "-")
    _git(h.repo, "merge", "--no-ff", "--no-commit", "incoming")


@pytest.mark.asyncio
async def test_rule3_exempts_merge_set_paths_during_merge(guard_harness: GuardHarness) -> None:
    h = guard_harness
    _start_rename_merge(h)
    for path in ("foreign.txt", "renamed.txt"):
        result = await h.handler._evaluate_rules(h.edit_event(path))
        assert result.decision == "allow"
    (h.repo / "outside.txt").write_text("outside dirt\n", encoding="utf-8")
    result = await h.handler._evaluate_rules(h.edit_event("outside.txt"))
    assert result.decision == "block"
    _git(h.repo, "merge", "--abort")
    (h.repo / "renamed.txt").write_text("after merge dirt\n", encoding="utf-8")
    result = await h.handler._evaluate_rules(h.edit_event("renamed.txt"))
    assert result.decision == "block"


@pytest.mark.asyncio
async def test_rule2_landing_merge_passes_for_merge_set_paths(guard_harness: GuardHarness) -> None:
    h = guard_harness
    _start_rename_merge(h)
    variables = SessionVariableManager(h.db)
    variables.merge_variables(
        h.foreign_session.id,
        {
            "session_dirty_files": ["foreign.txt", "renamed.txt", "outside.txt"],
            "session_dirty_file_checkouts": {
                str(h.repo): ["foreign.txt", "renamed.txt", "outside.txt"]
            },
        },
    )
    result = await h.handler._evaluate_rules(h.event("git commit -m merge"))
    assert result.decision == "allow"
    (h.repo / "outside.txt").write_text("outside dirt\n", encoding="utf-8")
    _git(h.repo, "add", "outside.txt")
    result = await h.handler._evaluate_rules(h.event("git commit -m merge"))
    assert result.decision == "block"
    assert result.reason is not None
    assert "outside.txt" in result.reason
    assert "renamed.txt" not in result.reason


@pytest.mark.asyncio
async def test_rule3_resolves_relative_paths_at_tool_workdir(
    guard_harness: GuardHarness, tmp_path: Path
) -> None:
    from tests.fixtures.isolated_checkout import write_project_marker

    h = guard_harness
    other = tmp_path / "other"
    _git(h.repo, "worktree", "add", "-b", "other", str(other))
    write_project_marker(other, project_id=h.project.id, name=h.project.name)
    (other / "p.py").write_text("other dirt\n", encoding="utf-8")
    result = await h.handler._evaluate_rules(h.edit_event("p.py", cwd=h.repo, workdir=other))
    assert result.decision == "block"
    (other / "p.py").unlink()
    (h.repo / "p.py").write_text("main dirt\n", encoding="utf-8")
    result = await h.handler._evaluate_rules(h.edit_event("p.py", cwd=h.repo, workdir=other))
    assert result.decision == "allow"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "operation",
    [
        "restore -- .",
        "checkout -- .",
        "checkout -f",
        "checkout --force HEAD",
        "switch -f --detach HEAD",
        "switch --force --detach HEAD",
        "switch --discard-changes --detach HEAD",
        "restore -S --workt -- foreign.txt",
        "restore -S --w -- foreign.txt",
        "switch --discard --detach HEAD",
        "checkout --for HEAD",
        "checkout --forc HEAD",
        "restore -S --gobby-unproven -- foreign.txt",
        "switch --gobby-unproven --detach HEAD",
        "checkout --gobby-unproven HEAD",
    ],
)
@pytest.mark.parametrize("nested", [False, True])
@pytest.mark.parametrize("owner", ["foreign", "unowned", "requester", "clean"])
async def test_rule3_root_writes_check_dirty_descendants(
    guard_harness: GuardHarness, operation: str, nested: bool, owner: str
) -> None:
    from gobby.workflows.task_claim_state import resolve_edit_pair

    h = guard_harness
    _git(h.repo, "add", ".gobby/project.json")
    _git(h.repo, "commit", "-m", "project marker")
    root = h.repo
    if nested:
        root = h.repo / "nested-checkout"
        _git(h.repo, "worktree", "add", "--detach", str(root), "HEAD")
    variables = SessionVariableManager(h.db)
    for session, path in ((h.current_session, "owned.txt"), (h.foreign_session, "foreign.txt")):
        variables.merge_variables(
            session.id,
            {"session_dirty_file_checkouts": {str(root): [path]}},
        )
    path = "owned.txt" if owner == "requester" else "foreign.txt"
    if owner == "unowned":
        path = "unowned.txt"
        (root / path).write_text("base\n", encoding="utf-8")
        _git(root, "add", path)
        _git(root, "commit", "-m", "unowned base")
    if owner != "clean":
        (root / path).write_text("dirty descendant\n", encoding="utf-8")
    assert resolve_edit_pair(
        str(root),
        str(root),
        project_id=h.project.id,
        db=h.db,
        variables=variables.get_variables(h.current_session.id),
    ) == (str(root), ".")
    event = h.event(f"git {operation}", checkout=root)
    response = await h.handler._evaluate_rules(event)
    assert event.data["canonical_tool_kind"] == "write"
    assert event.data["canonical_file_paths"] == [str(root)]
    assert event.data.get("canonical_write_file_paths", [str(root)]) == [str(root)]
    assert response.decision == ("block" if owner in {"foreign", "unowned"} else "allow"), (
        response.reason
    )
    if owner in {"foreign", "unowned"}:
        assert path in (response.reason or "")
        expected = h.foreign_session.ref if owner == "foreign" else "no live session owns it"
        assert expected in (response.reason or "")
    if owner == "clean":
        foreign_variables = variables.get_variables(h.foreign_session.id)
        assert "foreign.txt" not in foreign_variables["session_dirty_files"]
        assert foreign_variables["task_edited_files"][h.foreign_task.id] == ["foreign.txt"]


@pytest.mark.asyncio
async def test_rule3_directory_write_allows_requester_owned_child(
    guard_harness: GuardHarness,
) -> None:
    h = guard_harness
    (h.repo / "sub").mkdir()
    (h.repo / "sub/owned.txt").write_text("base\n", encoding="utf-8")
    _git(h.repo, "add", "sub/owned.txt")
    _git(h.repo, "commit", "-m", "owned subdirectory base")
    (h.repo / "sub/owned.txt").write_text("requester dirt\n", encoding="utf-8")
    SessionVariableManager(h.db).merge_variables(
        h.current_session.id,
        {
            "session_dirty_files": ["sub/owned.txt"],
            "session_dirty_file_checkouts": {str(h.repo): ["sub/owned.txt"]},
        },
    )
    response = await h.handler._evaluate_rules(h.event("git restore -- sub"))
    assert response.decision == "allow", response.reason


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "options",
    [
        "-sSTABLE -- foreign.txt",
        "-s STABLE -- foreign.txt",
        "--source=STABLE -- foreign.txt",
        "--source STABLE -- foreign.txt",
        "--pathspec-from-file -S",
        "--pathspec-from-file=-S",
        "-S --worktree -- foreign.txt",
    ],
)
async def test_rule3_restore_option_values_do_not_disable_write_guard(
    guard_harness: GuardHarness, options: str
) -> None:
    h = guard_harness
    _git(h.repo, "branch", "STABLE")
    (h.repo / "-S").write_text("foreign.txt\n", encoding="utf-8")
    _git(h.repo, "add", "--", "-S", ".gobby/project.json")
    _git(h.repo, "commit", "-m", "pathspec input")
    (h.repo / "foreign.txt").write_text("foreign dirt\n", encoding="utf-8")
    event = h.event(f"git restore {options}")
    response = await h.handler._evaluate_rules(event)
    assert event.data["canonical_tool_kind"] == "write"
    assert response.decision == "block"
    assert "foreign.txt" in (response.reason or "")
    assert h.foreign_session.ref in (response.reason or "")


@pytest.fixture(autouse=True)
def _local_machine_identity() -> Iterator[None]:
    with patch("gobby.utils.machine_id._cached_machine_id", LOCAL_MACHINE_ID):
        yield


def test_format_ref_preserves_full_uuid_fallback() -> None:
    task_id = "12345678-1234-5678-9abc-123456789abc"

    assert format_ref(None, task_id) == task_id


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
        workdir: Path | None = None,
    ) -> HookEvent:
        selected_session = session or self.current_session
        selected_checkout = checkout or self.repo
        tool_input = {"cmd": command}
        if workdir is not None:
            tool_input["workdir"] = str(workdir)
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
                "tool_input": tool_input,
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
        workdir: Path | None = None,
    ) -> HookEvent:
        selected_checkout = checkout or self.repo
        tool_input = {"file_path": file_path}
        if workdir is not None:
            tool_input["workdir"] = str(workdir)
        return HookEvent(
            event_type=HookEventType.BEFORE_TOOL,
            session_id=self.current_session.external_id,
            source=SessionSource.CODEX,
            timestamp=datetime.now(UTC),
            cwd=str(cwd or selected_checkout),
            project_id=self.project.id,
            data={
                "tool_name": "Edit",
                "tool_input": tool_input,
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
def guard_harness(
    isolated_checkout_factory: IsolatedCheckoutFactory, temp_db: HubDatabase, repo: Path
) -> GuardHarness:
    project = isolated_checkout_factory(temp_db, "commit-guard-test", root=repo).project
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
            "session_dirty_files": ["owned.txt"],
            "session_dirty_file_checkouts": {str(repo): ["owned.txt"]},
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
            "session_dirty_files": ["foreign.txt"],
            "session_dirty_file_checkouts": {str(repo): ["foreign.txt"]},
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


def test_active_owner_lookup_includes_the_current_session(guard_harness: GuardHarness) -> None:
    paths = {"owned.txt", "foreign.txt"}

    foreign_only = foreign_owned_dirty_paths(
        guard_harness.db,
        session_id=guard_harness.current_session.id,
        project_id=guard_harness.project.id,
        checkout_root=str(guard_harness.repo),
        paths=paths,
    )
    all_active = active_owned_dirty_paths(
        guard_harness.db,
        project_id=guard_harness.project.id,
        checkout_root=str(guard_harness.repo),
        paths=paths,
    )

    assert set(foreign_only) == {"foreign.txt"}
    assert set(all_active) == paths
    assert all_active["owned.txt"][0].session_ref == guard_harness.current_session.ref


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
    assert "Ask each owner" in response.reason
    assert "ask the user about unowned paths" in response.reason


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "ledger_path, requested", [(".", "foreign.txt"), ("sub", "sub/a.txt"), ("sub/a.txt", "sub")]
)
async def test_dirty_directory_owner_blocks_overlapping_edit(
    guard_harness: GuardHarness, ledger_path: str, requested: str
) -> None:
    harness = guard_harness
    manager = SessionVariableManager(harness.db)
    manager.merge_variables(
        harness.foreign_session.id,
        {
            "task_edited_files": {harness.foreign_task.id: [ledger_path]},
            "task_edited_file_checkouts": {
                harness.foreign_task.id: {str(harness.repo): [ledger_path]}
            },
            "session_dirty_files": [ledger_path],
            "session_dirty_file_checkouts": {str(harness.repo): [ledger_path]},
        },
    )
    dirty_path = "foreign.txt" if ledger_path == "." else "sub/a.txt"
    target = harness.repo / dirty_path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("foreign change\n")
    response = await harness.handler._evaluate_rules(harness.edit_event(requested))
    assert response.decision == "block", response.reason
    assert f"#{harness.foreign_task.seq_num}" in (response.reason or "")
    assert manager.get_variables(harness.foreign_session.id)["task_edited_files"] == {
        harness.foreign_task.id: [ledger_path],
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["edit_owned", "commit_only", "commit_staged", "edit_clean"])
async def test_foreign_root_owner_allows_requester_owned_or_clean_child(
    guard_harness: GuardHarness, action: str
) -> None:
    harness = guard_harness
    SessionVariableManager(harness.db).merge_variables(
        harness.foreign_session.id,
        {
            "task_edited_files": {harness.foreign_task.id: ["."]},
            "task_edited_file_checkouts": {harness.foreign_task.id: {str(harness.repo): ["."]}},
            "session_dirty_files": ["."],
            "session_dirty_file_checkouts": {str(harness.repo): ["."]},
        },
    )
    (harness.repo / "foreign.txt").write_text("foreign change\n")
    (harness.repo / "owned.txt").write_text("owned change\n")
    if action == "commit_only":
        event = harness.event("git commit --only -m mine -- owned.txt")
    elif action == "commit_staged":
        _git(harness.repo, "add", "owned.txt")
        event = harness.event("git commit -m mine")
    else:
        event = harness.edit_event("new.txt" if action == "edit_clean" else "owned.txt")
    response = await harness.handler._evaluate_rules(event)
    assert response.decision == "allow", response.reason
    ledger = SessionVariableManager(harness.db).get_variables(harness.foreign_session.id)
    assert ledger["task_edited_file_checkouts"][harness.foreign_task.id][str(harness.repo)] == ["."]


@pytest.mark.asyncio
async def test_root_directory_owner_blocks_staged_child_commit(guard_harness: GuardHarness) -> None:
    harness = guard_harness
    SessionVariableManager(harness.db).merge_variables(
        harness.foreign_session.id,
        {
            "task_edited_files": {harness.foreign_task.id: ["."]},
            "task_edited_file_checkouts": {harness.foreign_task.id: {str(harness.repo): ["."]}},
            "session_dirty_files": ["."],
            "session_dirty_file_checkouts": {str(harness.repo): ["."]},
        },
    )
    (harness.repo / "foreign.txt").write_text("foreign change\n")
    _git(harness.repo, "add", "foreign.txt")
    response = await harness.handler._evaluate_rules(harness.event("git commit -m test"))
    assert response.decision == "block", response.reason
    assert f"#{harness.foreign_task.seq_num}" in (response.reason or "")


def test_dirty_edit_reason_lists_every_owner_with_commit_guidance() -> None:
    reason = format_dirty_edit_reason(
        {
            ForeignPathOwner(
                path="docs/a.md",
                session_ref="#2",
                task_ref="#7",
                owner_session_id="session-2",
                owner_task_id="task-7",
            ),
            ForeignPathOwner(
                path="src/b.py",
                session_ref="#2",
                task_ref="#7",
                owner_session_id="session-2",
                owner_task_id="task-7",
            ),
            ForeignPathOwner(
                path="src/c.py",
                session_ref="#3",
                task_ref="#9",
                owner_session_id="session-3",
                owner_task_id="task-9",
            ),
        }
    )

    assert "- docs/a.md — session #2, task #7" in reason
    assert "- src/b.py — session #2, task #7" in reason
    assert "- src/c.py — session #3, task #9" in reason
    assert reason.count("Ask each owner with `gobby-agents.send_message`") == 1
    assert "commit its work" in reason


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "command",
    [
        "printf changed > foreign.txt",
        "git checkout HEAD~0; touch foreign.txt",
        "touch foreign.txt; git checkout HEAD~0",
        'git checkout HEAD~0; for f in foreign.txt; do touch "$f"; done',
        "bash -c 'git checkout HEAD~0; touch foreign.txt'",
    ],
)
async def test_normalized_bash_dirty_foreign_edit_blocks(
    guard_harness: GuardHarness, command: str
) -> None:
    (guard_harness.repo / "foreign.txt").write_text("foreign change\n", encoding="utf-8")
    event = guard_harness.event(command)
    event.data = {
        "tool_name": "Bash",
        "tool_input": {"command": command},
    }

    response = await guard_harness.handler._evaluate_rules(event)

    assert response.decision == "block"
    assert response.reason is not None
    assert "foreign.txt" in response.reason


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "operation",
    ["checkout -f HEAD", "switch -f --detach HEAD", "switch --discard-changes --detach HEAD"],
)
@pytest.mark.parametrize("directory_form", ["git_option", "cd"])
@pytest.mark.parametrize("location", ["foreign.txt", "sibling/foreign.txt", "sub/foreign.txt"])
@pytest.mark.parametrize("state", ["dirty", "untracked", "clean"])
async def test_forced_branch_change_checks_dirty_ownership(
    guard_harness: GuardHarness,
    tmp_path: Path,
    operation: str,
    directory_form: str,
    location: str,
    state: str,
) -> None:
    harness = guard_harness
    _git(harness.repo, "add", ".gobby/project.json")
    _git(harness.repo, "commit", "-m", "project marker")
    target = tmp_path / "other"
    _git(harness.repo, "worktree", "add", "--detach", str(target), "HEAD")
    (target / "sub").mkdir()
    if state == "untracked":
        location = str(Path(location).with_name("untracked.txt"))
    path = target / location
    path.parent.mkdir(parents=True, exist_ok=True)
    if state != "untracked":
        path.write_text("base\n", encoding="utf-8")
        _git(target, "add", location)
        _git(target, "commit", "-q", "-m", "foreign base")
    if state != "clean":
        path.write_text("foreign change\n", encoding="utf-8")
    SessionVariableManager(harness.db).merge_variables(
        harness.foreign_session.id,
        {
            "task_edited_files": {harness.foreign_task.id: [location]},
            "task_edited_file_checkouts": {harness.foreign_task.id: {str(target): [location]}},
            "session_dirty_files": [location],
            "session_dirty_file_checkouts": {str(target): [location]},
        },
    )
    directory = target / "sub"
    prefix = f"git -C {directory}" if directory_form == "git_option" else f"cd {directory} && git"
    event = harness.event(f"{prefix} {operation}")
    response = await harness.handler._evaluate_rules(event)
    assert event.data["canonical_file_paths"] == [str(target)]
    assert event.data["canonical_tool_kind"] == "write"
    assert response.decision == ("allow" if state == "clean" else "block")
    if state != "clean":
        assert location in (response.reason or "")
        assert harness.foreign_session.ref in (response.reason or "")


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["checkout HEAD", "switch --detach HEAD", "switch -c new"])
async def test_nonforce_branch_change_keeps_git_conflict_handling(
    guard_harness: GuardHarness, operation: str
) -> None:
    (guard_harness.repo / "foreign.txt").write_text("foreign change\n", encoding="utf-8")
    response = await guard_harness.handler._evaluate_rules(guard_harness.event(f"git {operation}"))
    assert response.decision == "allow"


@pytest.mark.asyncio
@pytest.mark.parametrize("directory_form", ["workdir", "git_option"])
@pytest.mark.parametrize("foreign_worktree", [False, True])
@pytest.mark.parametrize(
    "operation",
    [
        "checkout foreign.txt",
        "checkout HEAD foreign.txt",
        "checkout .",
        "checkout HEAD .",
        "checkout --ours foreign.txt",
    ],
)
@pytest.mark.parametrize("deleted", [False, True])
async def test_checkout_positional_paths_check_foreign_ownership(
    guard_harness: GuardHarness,
    directory_form: str,
    operation: str,
    deleted: bool,
    foreign_worktree: bool,
) -> None:
    harness = guard_harness
    target = harness.repo
    if foreign_worktree:
        # Keep the worktree inside the known project so the unresolved-project
        # external-path fallback cannot disable repository ownership enforcement.
        target = harness.repo / "other"
        _git(harness.repo, "worktree", "add", "--detach", str(target), "HEAD")
        (target / ".gobby").mkdir()
        shutil.copyfile(harness.repo / ".gobby/project.json", target / ".gobby/project.json")
        SessionVariableManager(harness.db).merge_variables(
            harness.foreign_session.id,
            {
                "task_edited_file_checkouts": {
                    harness.foreign_task.id: {str(target): ["foreign.txt"]}
                },
                "session_dirty_file_checkouts": {str(target): ["foreign.txt"]},
            },
        )
    path = target / "foreign.txt"
    if deleted:
        path.unlink()
    else:
        path.write_text("foreign change\n", encoding="utf-8")
    prefix = f"git -C {target}" if directory_form == "git_option" else "git"
    event = harness.event(f"{prefix} {operation}", checkout=target, workdir=target)
    response = await harness.handler._evaluate_rules(event)
    assert response.decision == "block", response.reason
    assert response.reason is not None
    assert "foreign.txt" in response.reason
    assert event.data["canonical_edit_file_paths"] == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "operation",
    [
        "checkout -m HEAD",
        "checkout --merge HEAD",
        "checkout -qm HEAD",
        "switch -m --detach HEAD",
        "switch --merge --detach HEAD",
        "switch -qm --detach HEAD",
    ],
)
@pytest.mark.parametrize("dirty", [False, True])
async def test_merge_branch_change_checks_foreign_ownership(
    guard_harness: GuardHarness, operation: str, dirty: bool
) -> None:
    harness = guard_harness
    _git(harness.repo, "add", ".gobby/project.json")
    _git(harness.repo, "commit", "-m", "project marker")
    if dirty:
        (harness.repo / "foreign.txt").write_text("foreign change\n", encoding="utf-8")
    event = harness.event(f"git {operation}", workdir=harness.repo)
    response = await harness.handler._evaluate_rules(event)
    assert event.data["canonical_tool_kind"] == "write"
    assert event.data["canonical_file_paths"] == [str(harness.repo)]
    assert response.decision == ("block" if dirty else "allow"), response.reason
    if dirty:
        assert harness.foreign_session.ref in (response.reason or "")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "operation", ["checkout -f HEAD", "checkout foreign.txt", "checkout -m HEAD"]
)
async def test_checkout_unverified_cwd_fails_closed(
    guard_harness: GuardHarness, operation: str
) -> None:
    harness = guard_harness
    harness.db.execute(
        "UPDATE rule_definitions SET enabled = (name IN (%s, %s, %s))",
        (RULE_NAME, DIRTY_EDIT_RULE_NAME, "block-unresolved-scope-shell-write"),
    )
    (harness.repo / "foreign.txt").write_text("foreign change\n", encoding="utf-8")
    event = harness.event(f"git {operation}")
    event.cwd = None
    response = await harness.handler._evaluate_rules(event)
    assert event.data["canonical_repo_mutation_scope_unknown"] is True
    assert response.decision == "block", response.reason
    assert response.reason is not None
    assert "cannot resolve its target scope" in response.reason


@pytest.mark.asyncio
async def test_nonforce_head_checkout_allows_unverified_cwd(guard_harness: GuardHarness) -> None:
    harness = guard_harness
    harness.db.execute(
        "UPDATE rule_definitions SET enabled = (name IN (%s, %s, %s))",
        (RULE_NAME, DIRTY_EDIT_RULE_NAME, "block-unresolved-scope-shell-write"),
    )
    (harness.repo / "foreign.txt").write_text("foreign change\n", encoding="utf-8")
    response = await harness.handler._evaluate_rules(harness.event("git checkout HEAD"))
    assert response.decision == "allow", response.reason


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "command",
    [
        "git --icase-pathspecs checkout FOREIGN.TXT",
        "git -C {repo} --icase-pathspecs checkout HEAD FOREIGN.TXT",
        "GIT_ICASE_PATHSPECS=1 git checkout FOREIGN.TXT",
        "env GIT_ICASE_PATHSPECS=1 git checkout FOREIGN.TXT",
        "git checkout {foreign,owned}.txt",
        "git checkout ~+/foreign.txt",
        "git --icase-pathspecs checkout -- FOREIGN.TXT",
        "git --icase-pathspecs restore FOREIGN.TXT",
        "GIT_ICASE_PATHSPECS=1 git restore FOREIGN.TXT",
        "GIT_ICASE_PATHSPECS+=1 git checkout FOREIGN.TXT",
        "GIT_ICASE_PATHSPECS+=1 git restore FOREIGN.TXT",
        "git checkout -- 'f*.txt'",
        "git checkout HEAD -- 'f*'",
        "git restore 'f*.txt'",
        "git restore '[f]oreign.txt'",
        "git restore -- 'f?reign.txt'",
        "git rm -f 'f*.txt'",
    ],
)
async def test_checkout_opaque_operands_block_foreign_restore(
    guard_harness: GuardHarness, command: str
) -> None:
    harness = guard_harness
    harness.db.execute(
        "UPDATE rule_definitions SET enabled = (name IN (%s, %s, %s))",
        (RULE_NAME, DIRTY_EDIT_RULE_NAME, "block-unresolved-scope-shell-write"),
    )
    (harness.repo / "foreign.txt").write_text("foreign change\n", encoding="utf-8")
    event = harness.event(command.replace("{repo}", str(harness.repo)), workdir=harness.repo)
    response = await harness.handler._evaluate_rules(event)
    assert response.decision == "block", response.reason
    assert event.data["canonical_repo_mutation_scope_unknown"] is True


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "operation",
    [
        "checkout owned.txt",
        "checkout HEAD~0",
        "checkout -",
        "checkout -- owned.txt",
        "restore owned.txt",
        "restore --source=HEAD owned.txt",
        "--icase-pathspecs add owned.txt",
    ],
)
async def test_checkout_literal_operand_controls_allow(
    guard_harness: GuardHarness, operation: str
) -> None:
    harness = guard_harness
    harness.db.execute(
        "UPDATE rule_definitions SET enabled = (name IN (%s, %s, %s))",
        (RULE_NAME, DIRTY_EDIT_RULE_NAME, "block-unresolved-scope-shell-write"),
    )
    (harness.repo / "foreign.txt").write_text("foreign change\n", encoding="utf-8")
    response = await harness.handler._evaluate_rules(
        harness.event(f"git {operation}", workdir=harness.repo)
    )
    assert response.decision == "allow", response.reason


@pytest.mark.asyncio
@pytest.mark.parametrize("shell", ["bash", "zsh"])
async def test_append_icase_prefix_blocks_foreign_restore(
    guard_harness: GuardHarness, shell: str
) -> None:
    if shutil.which(shell) is None:
        pytest.skip(f"{shell} is unavailable")
    harness = guard_harness
    harness.db.execute(
        "UPDATE rule_definitions SET enabled = (name IN (%s, %s, %s))",
        (RULE_NAME, DIRTY_EDIT_RULE_NAME, "block-unresolved-scope-shell-write"),
    )
    (harness.repo / "foreign.txt").write_text("foreign change\n", encoding="utf-8")
    result = subprocess.run(
        [shell, "-c", "GIT_ICASE_PATHSPECS+=1 git ls-files FOREIGN.TXT"],
        cwd=harness.repo,
        capture_output=True,
        text=True,
        check=True,
    )
    assert result.stdout.split() == ["foreign.txt"]
    event = harness.event("GIT_ICASE_PATHSPECS+=1 git checkout FOREIGN.TXT", workdir=harness.repo)
    response = await harness.handler._evaluate_rules(event)
    assert response.decision == "block", response.reason
    assert event.data["canonical_repo_mutation_scope_unknown"] is True


@pytest.mark.asyncio
async def test_clean_foreign_edit_is_allowed(guard_harness: GuardHarness) -> None:
    response = await guard_harness.handler._evaluate_rules(guard_harness.edit_event("foreign.txt"))

    assert response.decision == "allow"


@pytest.mark.parametrize(
    "command",
    [
        "touch d.py",
        "case x in x) :;; esac; touch d.py",
        "if true; then echo hi; fi; touch d.py",
        "for f in a b; do echo $f; done; git add d.py",
        "if [ ! -d build ]; then mkdir build; fi; cp a.py build/",
        "if grep -q x a.py; then :; else touch d.py; fi",
    ],
)
@pytest.mark.asyncio
async def test_directory_preserving_branches_allow_claimed_shell_write(
    guard_harness: GuardHarness, command: str
) -> None:
    guard_harness.db.execute(
        "UPDATE rule_definitions SET enabled = (name = %s)",
        ("block-unresolved-scope-shell-write",),
    )
    response = await guard_harness.handler._evaluate_rules(guard_harness.event(command))
    assert response.decision == "allow"


@pytest.mark.asyncio
async def test_clean_foreign_edit_releases_stale_entry_for_reedit(
    guard_harness: GuardHarness,
) -> None:
    """A first edit to a clean foreign-named path releases the stale entry (#22471)."""
    first = await guard_harness.handler._evaluate_rules(guard_harness.edit_event("foreign.txt"))
    assert first.decision == "allow"

    variables = SessionVariableManager(guard_harness.db)
    foreign_vars = variables.get_variables(guard_harness.foreign_session.id)
    assert foreign_vars["task_edited_files"][guard_harness.foreign_task.id] == ["foreign.txt"]
    assert foreign_vars["task_edited_file_checkouts"][guard_harness.foreign_task.id] == {
        str(guard_harness.repo): ["foreign.txt"]
    }
    assert not foreign_vars.get("session_dirty_files")

    # The allowed edit lands. The worker's own dirt must not re-arm the released
    # stale entry against the worker's next edit.
    (guard_harness.repo / "foreign.txt").write_text("worker change\n", encoding="utf-8")
    variables.record_edited_files(
        guard_harness.current_session.id,
        ["foreign.txt"],
        checkout_root=str(guard_harness.repo),
    )
    second = await guard_harness.handler._evaluate_rules(guard_harness.edit_event("foreign.txt"))
    assert second.decision == "allow"


@pytest.mark.asyncio
async def test_foreign_entry_committed_then_modified_again_still_blocks_edit(
    guard_harness: GuardHarness,
) -> None:
    """Committing a path does not license the next edit while newer dirt sits on it."""
    (guard_harness.repo / "foreign.txt").write_text("foreign work\n", encoding="utf-8")
    _git(guard_harness.repo, "add", "--", "foreign.txt")
    _git(guard_harness.repo, "commit", "-q", "-m", "foreign work")
    (guard_harness.repo / "foreign.txt").write_text("more foreign work\n", encoding="utf-8")

    response = await guard_harness.handler._evaluate_rules(guard_harness.edit_event("foreign.txt"))

    assert response.decision == "block"
    assert response.reason is not None
    assert "foreign.txt" in response.reason
    variables = SessionVariableManager(guard_harness.db)
    foreign_vars = variables.get_variables(guard_harness.foreign_session.id)
    assert foreign_vars["task_edited_files"][guard_harness.foreign_task.id] == ["foreign.txt"]
    assert foreign_vars["task_edited_file_checkouts"][guard_harness.foreign_task.id] == {
        str(guard_harness.repo): ["foreign.txt"]
    }


@pytest.mark.asyncio
async def test_stale_release_keeps_the_same_path_in_another_checkout(
    guard_harness: GuardHarness,
    tmp_path: Path,
) -> None:
    """A clean path here says nothing about the same path in the owner's other worktree."""
    foreign_checkout = tmp_path / "foreign-checkout"
    _git(
        guard_harness.repo,
        "worktree",
        "add",
        "-q",
        "-b",
        "foreign-worktree",
        str(foreign_checkout),
    )
    variables = SessionVariableManager(guard_harness.db)
    variables.merge_variables(
        guard_harness.foreign_session.id,
        {
            "task_edited_file_checkouts": {
                guard_harness.foreign_task.id: {
                    str(guard_harness.repo): ["foreign.txt"],
                    str(foreign_checkout): ["foreign.txt"],
                }
            },
            "session_dirty_file_checkouts": {
                str(guard_harness.repo): ["foreign.txt"],
                str(foreign_checkout): ["foreign.txt"],
            },
        },
    )
    (foreign_checkout / "foreign.txt").write_text("foreign worktree work\n", encoding="utf-8")

    response = await guard_harness.handler._evaluate_rules(guard_harness.edit_event("foreign.txt"))

    assert response.decision == "allow"
    foreign_vars = variables.get_variables(guard_harness.foreign_session.id)
    assert foreign_vars["task_edited_file_checkouts"][guard_harness.foreign_task.id] == {
        str(guard_harness.repo): ["foreign.txt"],
        str(foreign_checkout): ["foreign.txt"],
    }
    assert foreign_vars["session_dirty_file_checkouts"] == {str(foreign_checkout): ["foreign.txt"]}
    assert foreign_vars["task_edited_files"][guard_harness.foreign_task.id] == ["foreign.txt"]


@pytest.mark.asyncio
async def test_stale_entry_release_failure_is_skipped_and_edit_allows(
    guard_harness: GuardHarness,
) -> None:
    """The release is ledger cleanup: an infra failure leaves the entry and the edit allowed."""

    def failing_release(
        manager: SessionVariableManager,
        session_id: str,
        repo_relative_paths: list[str],
        *,
        checkout_root: str | None = None,
    ) -> list[str]:
        raise psycopg.OperationalError("ledger unavailable")

    with patch.object(SessionVariableManager, "release_session_dirty_files", failing_release):
        response = await guard_harness.handler._evaluate_rules(
            guard_harness.edit_event("foreign.txt")
        )

    assert response.decision == "allow"
    variables = SessionVariableManager(guard_harness.db)
    foreign_vars = variables.get_variables(guard_harness.foreign_session.id)
    assert foreign_vars["task_edited_files"][guard_harness.foreign_task.id] == ["foreign.txt"]


@pytest.mark.asyncio
async def test_unexpected_stale_entry_release_failure_propagates(
    guard_harness: GuardHarness,
) -> None:
    def failing_release(
        manager: SessionVariableManager,
        session_id: str,
        repo_relative_paths: list[str],
        *,
        checkout_root: str | None = None,
    ) -> list[str]:
        raise RuntimeError("programming error")

    with (
        patch.object(SessionVariableManager, "release_session_dirty_files", failing_release),
        pytest.raises(RuntimeError, match="programming error"),
    ):
        await guard_harness.handler._evaluate_rules(guard_harness.edit_event("foreign.txt"))


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("path", "contents"),
    [("owned.txt", "owned change\n"), ("unattributed.txt", "new change\n")],
)
async def test_dirty_edit_allows_own_path_and_blocks_unattributed_path(
    guard_harness: GuardHarness,
    path: str,
    contents: str,
) -> None:
    (guard_harness.repo / path).write_text(contents, encoding="utf-8")

    response = await guard_harness.handler._evaluate_rules(guard_harness.edit_event(path))

    assert response.decision == ("allow" if path == "owned.txt" else "block")
    if path == "unattributed.txt":
        assert response.reason is not None
        assert "unattributed.txt — no live session owns it" in response.reason


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
async def test_completed_foreign_session_leaves_dirty_edit_unowned(
    guard_harness: GuardHarness,
) -> None:
    guard_harness.session_manager.update_status(guard_harness.foreign_session.id, "completed")
    (guard_harness.repo / "foreign.txt").write_text("foreign change\n", encoding="utf-8")

    response = await guard_harness.handler._evaluate_rules(guard_harness.edit_event("foreign.txt"))

    assert response.decision == "block"
    assert response.reason is not None
    assert "foreign.txt — no live session owns it" in response.reason


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
            },
            "session_dirty_file_checkouts": {
                str(guard_harness.repo): ["nested/foreign.txt"],
            },
        },
    )
    nested_file.write_text("dirty\n", encoding="utf-8")

    response = await guard_harness.handler._evaluate_rules(
        guard_harness.edit_event(
            "foreign.txt",
            cwd=guard_harness.repo,
            workdir=nested,
        )
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

    with patch.object(daemon_git, "status", new_callable=AsyncMock) as mock_status:
        response = await guard_harness.handler._evaluate_rules(event)

    assert response.decision == "allow"
    mock_status.assert_not_awaited()


@pytest.mark.asyncio
async def test_foreign_conflict_verifies_only_candidate_paths(
    guard_harness: GuardHarness,
) -> None:
    """Git is asked about the foreign-claimed path only, never the whole tree."""
    (guard_harness.repo / "foreign.txt").write_text("foreign change\n", encoding="utf-8")
    (guard_harness.repo / "owned.txt").write_text("owned change\n", encoding="utf-8")
    (guard_harness.repo / "unrelated.txt").write_text("noise\n", encoding="utf-8")
    real_status = daemon_git.status
    inspected: list[set[str]] = []

    async def spy_status(
        cwd: str | Path,
        paths: Collection[str] = (),
        *,
        timeout: float = 10.0,
        env: Mapping[str, str] | None = None,
    ) -> GitResult:
        inspected.append(set(paths))
        return await real_status(cwd, paths, timeout=timeout, env=env)

    with patch.object(daemon_git, "status", side_effect=spy_status):
        response = await guard_harness.handler._evaluate_rules(
            guard_harness.edit_event("foreign.txt")
        )

    assert response.decision == "block"
    assert inspected == [{"foreign.txt"}]


@pytest.mark.asyncio
async def test_unverifiable_foreign_candidate_blocks(guard_harness: GuardHarness) -> None:
    async def timed_out_status(
        cwd: str | Path,
        paths: Collection[str] = (),
        *,
        timeout: float = 10.0,
        env: Mapping[str, str] | None = None,
    ) -> GitTimeout:
        del cwd, paths, env
        return GitTimeout(status="timeout", argv=("git", "status"), timeout=timeout)

    with patch.object(daemon_git, "status", side_effect=timed_out_status):
        response = await guard_harness.handler._evaluate_rules(
            guard_harness.edit_event("foreign.txt")
        )

    assert response.decision == "block"
    assert response.reason is not None
    assert "could not verify" in response.reason
    assert "foreign.txt" in response.reason


@pytest.mark.asyncio
async def test_shell_git_status_after_external_commit_reconciles_attribution(
    guard_harness: GuardHarness,
) -> None:
    """A commit made outside the hooks is picked up by the next git command the session runs."""
    (guard_harness.repo / "owned.txt").write_text("committed elsewhere\n", encoding="utf-8")
    _git(guard_harness.repo, "add", "--", "owned.txt")
    _git(guard_harness.repo, "commit", "-q", "-m", "external commit", "--", "owned.txt")
    event = guard_harness.event("git status")
    event.event_type = HookEventType.AFTER_TOOL
    event.data["tool_output"] = "nothing to commit, working tree clean"
    event.metadata["is_failure"] = False

    response = await guard_harness.handler._evaluate_rules(event)

    assert response.decision == "allow"
    variables = SessionVariableManager(guard_harness.db).get_variables(
        guard_harness.current_session.id
    )
    assert guard_harness.current_task.id not in variables.get("task_edited_files", {})
    assert guard_harness.current_task.id not in variables.get("task_edited_file_checkouts", {})


@pytest.mark.asyncio
async def test_dirty_edit_ownership_inspection_failure_blocks(
    guard_harness: GuardHarness,
) -> None:
    (guard_harness.repo / "foreign.txt").write_text("foreign change\n", encoding="utf-8")

    with patch(
        "gobby.workflows.commit_guard._live_path_owners",
        side_effect=DirtyEditOwnershipInspectionError("database unavailable"),
    ):
        response = await guard_harness.handler._evaluate_rules(
            guard_harness.edit_event("foreign.txt")
        )

    assert response.decision == "block"
    assert response.reason is not None
    assert "could not verify dirty-path ownership" in response.reason
    assert "database unavailable" in response.reason
    assert "retry" in response.reason


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("failure", "cause"),
    [
        (PoolTimeout("connection pool timeout"), "connection pool timeout"),
        (
            DirtyEditOwnershipInspectionError(
                "git rev-parse --show-toplevel failed: permission denied"
            ),
            "git rev-parse --show-toplevel failed: permission denied",
        ),
    ],
    ids=["pool-timeout", "git-rev-parse-failure"],
)
async def test_dirty_edit_inspection_failure_names_cause_and_retry(
    guard_harness: GuardHarness,
    failure: Exception,
    cause: str,
) -> None:
    event = guard_harness.edit_event("foreign.txt")
    event.data.update(
        {
            "canonical_tool_kind": "write",
            "canonical_repo_mutation": True,
            "canonical_file_paths": ["foreign.txt"],
        }
    )
    with patch(
        "gobby.workflows.commit_guard._live_path_owners", side_effect=failure
    ) as inspect_owners:
        reason = await foreign_dirty_edit_conflict(
            guard_harness.db,
            event,
            session_id=guard_harness.current_session.id,
            project_id=guard_harness.project.id,
            project_path=str(guard_harness.repo),
        )

    inspect_owners.assert_called_once()
    assert "could not verify dirty-path ownership" in reason
    assert cause in reason
    assert "Fix that cause" in reason
    assert "retry once the daemon and repository are available" in reason


@pytest.mark.asyncio
async def test_unexpected_dirty_edit_ownership_inspection_failure_propagates(
    guard_harness: GuardHarness,
) -> None:
    (guard_harness.repo / "foreign.txt").write_text("foreign change\n", encoding="utf-8")

    with (
        patch(
            "gobby.workflows.commit_guard._live_path_owners",
            side_effect=RuntimeError("programming error"),
        ),
        pytest.raises(RuntimeError, match="programming error"),
    ):
        await guard_harness.handler._evaluate_rules(guard_harness.edit_event("foreign.txt"))


@pytest.mark.asyncio
@pytest.mark.parametrize("tool_name", ["functions.apply_patch", "Write"])
@pytest.mark.parametrize("placement", ["tool_input", "event"])
async def test_commit_text_in_non_shell_tool_data_is_allowed(
    guard_harness: GuardHarness, tool_name: str, placement: str
) -> None:
    command = "git " + "commit -m $(uv run pytest)"
    event = guard_harness.event(command)
    event.data["tool_name"] = tool_name
    if placement == "event":
        event.data["tool_input"] = {"content": command}
        event.data["command"] = command

    response = await guard_harness.handler._evaluate_rules(event)

    assert response.decision == "allow"


_ROLE_PROSE_PATCH = """*** Begin Patch
*** Add File: tests/test_role_prose.py
+ROLE_PROSE = "Use git commit --only -- <paths> for ordinary commits."
+
+
+def test_role_prose_names_the_scoped_commit() -> None:
+    # Run git commit -m 'wip' only after staging owned paths.
+    assert "git commit --only" in ROLE_PROSE
*** End Patch
"""


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("tool_name", "tool_input"),
    [
        ("functions.apply_patch", {"patch": _ROLE_PROSE_PATCH}),
        ("Write", {"patch": _ROLE_PROSE_PATCH, "file_path": "tests/test_role_prose.py"}),
    ],
    ids=["codex-apply-patch", "normalized-write"],
)
async def test_commit_prose_in_patch_content_allows_while_a_real_commit_blocks(
    guard_harness: GuardHarness, tool_name: str, tool_input: dict[str, str]
) -> None:
    (guard_harness.repo / "owned.txt").write_text("current change\n", encoding="utf-8")
    (guard_harness.repo / "foreign.txt").write_text("foreign change\n", encoding="utf-8")
    _git(guard_harness.repo, "add", "--", "owned.txt", "foreign.txt")
    patch_event = guard_harness.event("", workdir=guard_harness.repo)
    patch_event.data["tool_name"] = tool_name
    patch_event.data["tool_input"] = tool_input

    patch_response = await guard_harness.handler.evaluate_async(patch_event)
    commit_response = await guard_harness.handler.evaluate_async(
        guard_harness.event("git commit -m 'unsafe'", workdir=guard_harness.repo)
    )

    assert patch_response.decision == "allow"
    assert commit_response.decision == "block"
    assert commit_response.reason is not None
    assert "foreign.txt" in commit_response.reason


@pytest.mark.asyncio
async def test_unscoped_commit_blocks_foreign_staged_path_with_owner_diagnostic(
    guard_harness: GuardHarness,
) -> None:
    (guard_harness.repo / "owned.txt").write_text("current change\n", encoding="utf-8")
    (guard_harness.repo / "foreign.txt").write_text("foreign change\n", encoding="utf-8")
    _git(guard_harness.repo, "add", "--", "owned.txt", "foreign.txt")

    response = await guard_harness.handler._evaluate_rules(
        guard_harness.event("git commit -m 'unsafe'", workdir=guard_harness.repo)
    )

    assert response.decision == "block"
    assert response.reason is not None
    assert "foreign.txt" in response.reason
    assert guard_harness.foreign_session.ref in response.reason
    assert f"#{guard_harness.foreign_task.seq_num}" in response.reason
    assert "gobby-agents.send_message" in response.reason
    assert "ask each owner with `gobby-agents.send_message` to commit first" in response.reason
    assert "git commit --only --" in response.reason
    assert set(_git(guard_harness.repo, "diff", "--cached", "--name-only").splitlines()) == {
        "foreign.txt",
        "owned.txt",
    }


@pytest.mark.asyncio
async def test_commit_uses_tool_workdir_checkout(
    guard_harness: GuardHarness,
    tmp_path: Path,
) -> None:
    external_repo = tmp_path / "external-repo"
    external_repo.mkdir()
    _git(external_repo, "init", "-q")
    _git(external_repo, "config", "user.email", "tests@gobby.local")
    _git(external_repo, "config", "user.name", "Gobby Tests")
    (external_repo / "foreign.txt").write_text("external base\n", encoding="utf-8")
    _git(external_repo, "add", "--", "foreign.txt")
    _git(external_repo, "commit", "-q", "-m", "external base")
    (external_repo / "foreign.txt").write_text("external staged\n", encoding="utf-8")
    _git(external_repo, "add", "--", "foreign.txt")

    (guard_harness.repo / "foreign.txt").write_text("project staged\n", encoding="utf-8")
    _git(guard_harness.repo, "add", "--", "foreign.txt")

    response = await guard_harness.handler._evaluate_rules(
        guard_harness.event("git commit -m external", workdir=external_repo)
    )

    assert response.decision == "allow"
    assert _git(guard_harness.repo, "diff", "--cached", "--name-only") == "foreign.txt"


@pytest.mark.asyncio
async def test_commit_from_non_git_tool_workdir_falls_through(
    guard_harness: GuardHarness,
    tmp_path: Path,
) -> None:
    non_repo = tmp_path / "not-a-repository"
    non_repo.mkdir()
    (guard_harness.repo / "foreign.txt").write_text("project staged\n", encoding="utf-8")
    _git(guard_harness.repo, "add", "--", "foreign.txt")

    response = await guard_harness.handler._evaluate_rules(
        guard_harness.event("git commit -m invalid", workdir=non_repo)
    )

    assert response.decision == "allow"


@pytest.mark.asyncio
async def test_unverifiable_scoped_commit_names_the_git_failure(
    guard_harness: GuardHarness,
    tmp_path: Path,
) -> None:
    # `-F <file>` written after `--` is a pathspec, so a message file outside
    # the checkout makes the ownership query's `git ls-files` fail. The block
    # has to name that: "retry once the daemon is available" on its own sends
    # the caller round a loop no retry can break.
    outside_message = tmp_path / "outside-message.txt"
    outside_message.write_text("subject\n", encoding="utf-8")

    response = await guard_harness.handler._evaluate_rules(
        guard_harness.event(f"git commit --only -- owned.txt -F {outside_message}")
    )

    assert response.decision == "block"
    assert response.reason is not None
    assert "could not verify staged-path ownership" in response.reason
    assert "outside repository" in response.reason


@pytest.mark.asyncio
async def test_unscoped_commit_blocks_awaiting_handoff_owner(
    guard_harness: GuardHarness,
) -> None:
    guard_harness.session_manager.update_status(
        guard_harness.foreign_session.id,
        "awaiting_handoff",
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
            "session_dirty_files": foreign_paths,
            "session_dirty_file_checkouts": {
                str(guard_harness.repo): foreign_paths,
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
            "session_dirty_files": ["foreign.txt"],
            "session_dirty_file_checkouts": {
                str(guard_harness.repo): ["foreign.txt"],
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
            "session_dirty_files": ["foreign.txt"],
            "session_dirty_file_checkouts": {
                str(foreign_checkout): ["foreign.txt"],
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
async def test_clean_foreign_pathspec_commit_is_allowed_and_releases_stale_entry(
    guard_harness: GuardHarness,
) -> None:
    """A clean foreign-named path is not a pathspec commit conflict (#22471)."""
    response = await guard_harness.handler._evaluate_rules(
        guard_harness.event("git commit -m 'stale entry' -- foreign.txt")
    )

    assert response.decision == "allow"
    variables = SessionVariableManager(guard_harness.db)
    foreign_vars = variables.get_variables(guard_harness.foreign_session.id)
    assert foreign_vars["task_edited_files"][guard_harness.foreign_task.id] == ["foreign.txt"]
    assert foreign_vars["task_edited_file_checkouts"][guard_harness.foreign_task.id] == {
        str(guard_harness.repo): ["foreign.txt"]
    }
    assert not foreign_vars.get("session_dirty_files")


@pytest.mark.asyncio
async def test_dirty_foreign_pathspec_commit_still_blocked_and_entry_retained(
    guard_harness: GuardHarness,
) -> None:
    """Real uncommitted foreign dirt keeps its ledger entry and blocks the commit."""
    (guard_harness.repo / "foreign.txt").write_text("foreign change\n", encoding="utf-8")
    _git(guard_harness.repo, "add", "--", "foreign.txt")
    variables = SessionVariableManager(guard_harness.db)

    response = await guard_harness.handler._evaluate_rules(
        guard_harness.event("git commit --only -m 'foreign dirt' -- foreign.txt")
    )

    assert response.decision == "block"
    assert response.reason is not None
    assert "foreign.txt" in response.reason
    foreign_vars = variables.get_variables(guard_harness.foreign_session.id)
    assert foreign_vars["task_edited_files"][guard_harness.foreign_task.id] == ["foreign.txt"]
    assert foreign_vars["task_edited_file_checkouts"][guard_harness.foreign_task.id] == {
        str(guard_harness.repo): ["foreign.txt"]
    }


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


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "pathspec",
    [
        "'*.py'",
        "'[ab].py'",
        "':/'",
        "':(glob)**/*.py'",
        "*.py",
        "[ab].py",
        "{a,b}.py",
        "sub/{a,b}.py",
        "?.py",
        "'b\\.py'",
        "b.py",
    ],
)
@pytest.mark.parametrize("delimiter", ["-- ", ""])
async def test_pattern_pathspec_refuses_foreign_dirty_python_file(
    guard_harness: GuardHarness, pathspec: str, delimiter: str
) -> None:
    for relative in ("a.py", "b.py", "sub/a.py", "sub/b.py"):
        path = guard_harness.repo / relative
        path.parent.mkdir(exist_ok=True)
        path.write_text("base\n", encoding="utf-8")
    _git(guard_harness.repo, "add", "--", "a.py", "b.py", "sub")
    _git(guard_harness.repo, "commit", "-q", "-m", "python fixtures")
    variables = SessionVariableManager(guard_harness.db)
    variables.merge_variables(
        guard_harness.foreign_session.id,
        {
            "task_edited_files": {guard_harness.foreign_task.id: ["b.py", "sub/b.py"]},
            "task_edited_file_checkouts": {
                guard_harness.foreign_task.id: {str(guard_harness.repo): ["b.py", "sub/b.py"]}
            },
            "session_dirty_files": ["b.py", "sub/b.py"],
            "session_dirty_file_checkouts": {str(guard_harness.repo): ["b.py", "sub/b.py"]},
        },
    )
    (guard_harness.repo / "b.py").write_text("foreign change\n", encoding="utf-8")
    (guard_harness.repo / "sub/b.py").write_text("foreign change\n", encoding="utf-8")

    response = await guard_harness.handler._evaluate_rules(
        guard_harness.event(f"git commit --only -m pattern {delimiter}{pathspec}")
    )
    assert response.decision == "block"
    assert response.reason


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "command,stage_foreign",
    [
        ("git commit -a -m x", False),
        ("git commit -am x", False),
        ("git commit --include -m x -- owned.txt", True),
        ("git commit -im x owned.txt", True),
        ("git commit -m x --pathspec-from-file=files.txt", False),
        ("git --icase-pathspecs commit -m x -- FOREIGN.TXT", False),
        ("GIT_ICASE_PATHSPECS=1 git commit -m x -- FOREIGN.TXT", False),
    ],
)
async def test_commit_selection_modes_cannot_hide_foreign_dirty_paths(
    guard_harness: GuardHarness, command: str, stage_foreign: bool
) -> None:
    (guard_harness.repo / "foreign.txt").write_text("foreign change\n", encoding="utf-8")
    (guard_harness.repo / "owned.txt").write_text("owned change\n", encoding="utf-8")
    (guard_harness.repo / "files.txt").write_text("foreign.txt\n", encoding="utf-8")
    if stage_foreign:
        _git(guard_harness.repo, "add", "--", "foreign.txt")
    response = await guard_harness.handler._evaluate_rules(guard_harness.event(command))
    assert response.decision == "block"
    assert response.reason


@pytest.mark.asyncio
@pytest.mark.parametrize("message", ["*.py", "{a,b}"])
async def test_positional_owned_path_preserves_pattern_message_data(
    guard_harness: GuardHarness, message: str
) -> None:
    (guard_harness.repo / "owned.txt").write_text("owned change\n", encoding="utf-8")
    (guard_harness.repo / "foreign.txt").write_text("foreign change\n", encoding="utf-8")
    command = f"git commit -m '{message}' owned.txt"
    response = await guard_harness.handler._evaluate_rules(guard_harness.event(command))
    assert response.decision == "allow"
    invocations = parse_git_commit_invocations(command)
    assert len(invocations) == 1
    assert invocations[0].pathspecs == ("owned.txt",)
    assert not invocations[0].cwd_unverified


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("setup", "shell"),
    [
        ("export GIT_ICASE_PATHSPECS=1;", "bash"),
        ("export GIT_ICASE_PATHSPECS=1 &&", "bash"),
        ("set -a; GIT_ICASE_PATHSPECS=1;", "bash"),
        ("declare -x GIT_ICASE_PATHSPECS=1;", "bash"),
        ("! export GIT_ICASE_PATHSPECS=1;", "bash"),
        ("set -a; read GIT_ICASE_PATHSPECS <<< 1;", "bash"),
        ("set -a; printf -v GIT_ICASE_PATHSPECS 1;", "bash"),
        ("set -a; let GIT_ICASE_PATHSPECS=1;", "bash"),
        ('set -a; : "${GIT_ICASE_PATHSPECS:=1}";', "bash"),
        ("set -o allexport; read GIT_ICASE_PATHSPECS <<< 1;", "bash"),
        ("shopt -os allexport; read GIT_ICASE_PATHSPECS <<< 1;", "bash"),
        ("shopt -s -o allexport; read GIT_ICASE_PATHSPECS <<< 1;", "bash"),
        ("x=PATHSPECS; export GIT_ICASE_$x=1;", "bash"),
        ('n=GIT_ICASE_PATHSPECS; export "$n=1";', "bash"),
        ('n=GIT_ICASE_PATHSPECS; declare -x "$n"=1;', "bash"),
        ("export GIT_{ICASE,X}_PATHSPECS=1;", "bash"),
        ("export $(echo GIT_ICASE_PATHSPECS)=1;", "bash"),
        ("setopt allexport; read GIT_ICASE_PATHSPECS <<< 1;", "zsh"),
        ("setopt ALL_EXPORT; read GIT_ICASE_PATHSPECS <<< 1;", "zsh"),
        ("setopt -a; read GIT_ICASE_PATHSPECS <<< 1;", "zsh"),
        ("set -o ALL_EXPORT; read GIT_ICASE_PATHSPECS <<< 1;", "zsh"),
        ("set -o all_export; read GIT_ICASE_PATHSPECS <<< 1;", "zsh"),
        ("unsetopt noallexport; read GIT_ICASE_PATHSPECS <<< 1;", "zsh"),
        ("set +o noallexport; read GIT_ICASE_PATHSPECS <<< 1;", "zsh"),
        ("integer -x GIT_ICASE_PATHSPECS=1;", "zsh"),
        ("x=PATHSPECS; export GIT_ICASE_$x=1;", "zsh"),
        ('n=GIT_ICASE_PATHSPECS; export "$n=1";', "zsh"),
        ('n=GIT_ICASE_PATHSPECS; declare -x "$n"=1;', "zsh"),
        ("export GIT_{ICASE,X}_PATHSPECS=1;", "zsh"),
        ("export $(echo GIT_ICASE_PATHSPECS)=1;", "zsh"),
    ],
)
async def test_exported_pathspec_mode_refuses_foreign_capture(
    guard_harness: GuardHarness, setup: str, shell: str
) -> None:
    if shutil.which(shell) is None:
        pytest.skip(f"{shell} is unavailable")
    (guard_harness.repo / "foreign.txt").write_text("foreign change\n", encoding="utf-8")
    command = f"{setup} git commit --only -m x -- FOREIGN.TXT"
    response = await guard_harness.handler._evaluate_rules(guard_harness.event(command))
    subprocess.run([shell, "-c", command], cwd=guard_harness.repo, capture_output=True, check=True)
    assert _git(guard_harness.repo, "show", "--format=", "--name-only", "HEAD") == "foreign.txt"
    assert response.decision == "block"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "command",
    [
        "export GIT_EDITOR=true; git commit -m x -- owned.txt",
        "GIT_AUTHOR_DATE=2026-01-01T00:00:00 git commit -m x owned.txt",
        "! true; git commit -m x owned.txt",
        "set -e; git commit -m x owned.txt",
        "set -euo pipefail; git commit -m x owned.txt",
    ],
)
async def test_unrelated_git_environment_preserves_owned_only_commit(
    guard_harness: GuardHarness, command: str
) -> None:
    (guard_harness.repo / "foreign.txt").write_text("foreign change\n", encoding="utf-8")
    (guard_harness.repo / "owned.txt").write_text("owned change\n", encoding="utf-8")
    response = await guard_harness.handler._evaluate_rules(guard_harness.event(command))
    assert response.decision == "allow"
    subprocess.run(["bash", "-c", command], cwd=guard_harness.repo, capture_output=True, check=True)
    assert _git(guard_harness.repo, "show", "--format=", "--name-only", "HEAD") == "owned.txt"


@pytest.mark.asyncio
@pytest.mark.parametrize("selection", ["inline", "exported", "options", "chdir", "negated"])
async def test_linked_worktree_selector_refuses_foreign_capture(
    guard_harness: GuardHarness, tmp_path: Path, selection: str
) -> None:
    linked = tmp_path / "linked"
    _git(guard_harness.repo, "worktree", "add", "--detach", str(linked))
    (guard_harness.repo / "foreign.txt").write_text("foreign change\n", encoding="utf-8")
    root = shlex.quote(str(guard_harness.repo))
    git_dir = shlex.quote(str(guard_harness.repo / ".git"))
    prefixes = {
        "inline": f"GIT_DIR={git_dir} GIT_WORK_TREE={root} git",
        "exported": f"export GIT_DIR={git_dir} GIT_WORK_TREE={root}; git",
        "negated": f"! export GIT_DIR={git_dir} GIT_WORK_TREE={root}; git",
        "options": f"git --git-dir={git_dir} --work-tree={root}",
        "chdir": f"git -C {root}",
    }
    command = f"{prefixes[selection]} commit -a -m x"
    response = await guard_harness.handler._evaluate_rules(
        guard_harness.event(command, workdir=linked)
    )
    subprocess.run(["bash", "-c", command], cwd=linked, capture_output=True, check=True)
    assert _git(guard_harness.repo, "show", "--format=", "--name-only", "HEAD") == "foreign.txt"
    assert response.decision == "block"


@pytest.mark.asyncio
async def test_git_c_worktree_commit_does_not_inspect_primary_index(
    guard_harness: GuardHarness,
    tmp_path: Path,
) -> None:
    worktree = tmp_path / "agent-worktree"
    _git(
        guard_harness.repo,
        "worktree",
        "add",
        "-q",
        "-b",
        "agent-worktree",
        str(worktree),
    )
    (guard_harness.repo / "foreign.txt").write_text("staged on primary\n", encoding="utf-8")
    _git(guard_harness.repo, "add", "--", "foreign.txt")

    response = await guard_harness.handler._evaluate_rules(
        guard_harness.event(f"git -C {worktree} commit -m 'worktree commit'")
    )

    assert response.decision == "allow"
    assert set(_git(guard_harness.repo, "diff", "--cached", "--name-only").splitlines()) == {
        "foreign.txt"
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("separator", [" && ", ";", "\n"])
@pytest.mark.parametrize("commit_foreign_checkout", [False, True])
async def test_shell_cd_commit_inspects_actual_checkout(
    guard_harness: GuardHarness,
    tmp_path: Path,
    separator: str,
    commit_foreign_checkout: bool,
) -> None:
    worktree = tmp_path / "agent worktree"
    _git(guard_harness.repo, "worktree", "add", "-q", "-b", "shell-cd", str(worktree))
    variables = SessionVariableManager(guard_harness.db)
    variables.merge_variables(
        guard_harness.current_session.id,
        {
            "task_edited_files": {guard_harness.current_task.id: ["foreign.txt"]},
            "task_edited_file_checkouts": {
                guard_harness.current_task.id: {str(worktree): ["foreign.txt"]},
            },
            "session_dirty_files": ["foreign.txt"],
            "session_dirty_file_checkouts": {str(worktree): ["foreign.txt"]},
        },
    )
    (guard_harness.repo / "foreign.txt").write_text("foreign shared content\n", encoding="utf-8")
    (worktree / "foreign.txt").write_text("owned isolated content\n", encoding="utf-8")
    _git(guard_harness.repo, "add", "--", "foreign.txt")
    _git(worktree, "add", "--", "foreign.txt")
    target = guard_harness.repo if commit_foreign_checkout else worktree
    start = worktree if commit_foreign_checkout else guard_harness.repo
    command = f"cd {shlex.quote(str(target))}{separator}git commit --only -m x -- foreign.txt"

    response = await guard_harness.handler._evaluate_rules(
        guard_harness.event(command, workdir=start)
    )

    assert response.decision == ("block" if commit_foreign_checkout else "allow")
    if commit_foreign_checkout:
        assert response.reason is not None
        assert "foreign.txt" in response.reason
    else:
        _git(worktree, "commit", "-q", "--only", "-m", "isolated", "--", "foreign.txt")
        assert _git(worktree, "show", "HEAD:foreign.txt") == "owned isolated content"
    assert _git(guard_harness.repo, "diff", "--cached", "--name-only") == "foreign.txt"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "command",
    [
        'cd "$W" && git commit --only -m x -- foreign.txt',
        'git -C "$W" commit --only -m x -- foreign.txt',
        'git --work-tree "$(pwd)" commit -m x',
    ],
)
async def test_dynamic_cwd_refuses_without_git_or_operational_warning(
    guard_harness: GuardHarness,
    caplog: pytest.LogCaptureFixture,
    command: str,
) -> None:
    from gobby.workflows.commit_guard import foreign_staged_commit_conflict

    git_run = AsyncMock(
        return_value=GitTimeout(status="timeout", argv=("git", "rev-parse"), timeout=10.0)
    )
    with caplog.at_level("WARNING", logger="gobby.workflows.commit_guard"):
        with patch.object(daemon_git, "run", git_run):
            refusal = await foreign_staged_commit_conflict(
                guard_harness.db,
                guard_harness.event(command),
                session_id=guard_harness.current_session.id,
                project_id=guard_harness.project.id,
                project_path=str(guard_harness.repo),
            )

    assert refusal.startswith("Commit blocked:")
    assert "working directory" in refusal
    git_run.assert_not_awaited()
    assert not caplog.records


@pytest.mark.parametrize("override", ["chdir", "work-tree"])
@pytest.mark.asyncio
async def test_git_directory_override_uses_resolved_checkout_ownership(
    guard_harness: GuardHarness,
    tmp_path: Path,
    override: str,
) -> None:
    worktree = tmp_path / "owned-worktree"
    _git(
        guard_harness.repo,
        "worktree",
        "add",
        "-q",
        "-b",
        f"owned-worktree-{override}",
        str(worktree),
    )
    SessionVariableManager(guard_harness.db).merge_variables(
        guard_harness.foreign_session.id,
        {
            "task_edited_file_checkouts": {
                guard_harness.foreign_task.id: {str(worktree): ["foreign.txt"]},
            },
            "session_dirty_file_checkouts": {str(worktree): ["foreign.txt"]},
        },
    )
    (worktree / "foreign.txt").write_text("foreign worktree change\n", encoding="utf-8")
    _git(worktree, "add", "--", "foreign.txt")
    if override == "chdir":
        command = f"git -C {worktree} commit -m unsafe"
    else:
        git_dir = _git(worktree, "rev-parse", "--absolute-git-dir")
        command = f"git --git-dir={git_dir} --work-tree={worktree} commit -m unsafe"

    response = await guard_harness.handler._evaluate_rules(guard_harness.event(command))

    assert response.decision == "block"
    assert response.reason is not None
    assert "foreign.txt" in response.reason


@pytest.mark.asyncio
@pytest.mark.parametrize("separator", [";", "\n"])
async def test_conditional_navigation_cannot_hide_foreign_staged_checkout(
    guard_harness: GuardHarness,
    tmp_path: Path,
    separator: str,
) -> None:
    worktree = tmp_path / "owned worktree"
    _git(guard_harness.repo, "worktree", "add", "-q", "-b", "conditional-cd", str(worktree))
    SessionVariableManager(guard_harness.db).merge_variables(
        guard_harness.current_session.id,
        {
            "task_edited_files": {guard_harness.current_task.id: ["foreign.txt"]},
            "task_edited_file_checkouts": {
                guard_harness.current_task.id: {str(worktree): ["foreign.txt"]},
            },
            "session_dirty_files": ["foreign.txt"],
            "session_dirty_file_checkouts": {str(worktree): ["foreign.txt"]},
        },
    )
    (guard_harness.repo / "foreign.txt").write_text("foreign shared bytes\n", encoding="utf-8")
    (worktree / "foreign.txt").write_text("owned isolated bytes\n", encoding="utf-8")
    _git(guard_harness.repo, "add", "--", "foreign.txt")
    _git(worktree, "add", "--", "foreign.txt")
    foreign_index = _git(guard_harness.repo, "write-tree")
    owned_index = _git(worktree, "write-tree")
    command = (
        f"false && cd {shlex.quote(str(worktree))}{separator}"
        "git commit --only -m unsafe -- foreign.txt"
    )

    response = await guard_harness.handler._evaluate_rules(
        guard_harness.event(command, workdir=guard_harness.repo)
    )

    assert response.decision == "block"
    assert response.reason is not None
    assert "working directory" in response.reason
    assert _git(guard_harness.repo, "write-tree") == foreign_index
    assert _git(worktree, "write-tree") == owned_index


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "command",
    [
        "cd ~ && git commit -m unsafe",
        "cd ~/owned && git commit -m unsafe",
        "cd ~/'owned' && git commit -m unsafe",
        "cd ~other/owned && git commit -m unsafe",
        "git -C ~/owned commit -m unsafe",
        "git --work-tree ~/owned commit -m unsafe",
        "git --git-dir ~/owned/.git commit -m unsafe",
    ],
)
async def test_tilde_navigation_refuses_before_git(
    guard_harness: GuardHarness,
    caplog: pytest.LogCaptureFixture,
    command: str,
) -> None:
    from gobby.workflows.commit_guard import foreign_staged_commit_conflict

    invocations = parse_git_commit_invocations(command)
    assert len(invocations) == 1
    assert invocations[0].cwd_unverified
    git_run = AsyncMock()
    with caplog.at_level("WARNING", logger="gobby.workflows.commit_guard"):
        with patch.object(daemon_git, "run", git_run):
            refusal = await foreign_staged_commit_conflict(
                guard_harness.db,
                guard_harness.event(command),
                session_id=guard_harness.current_session.id,
                project_id=guard_harness.project.id,
                project_path=str(guard_harness.repo),
            )

    assert refusal.startswith("Commit blocked:")
    assert "working directory" in refusal
    git_run.assert_not_awaited()
    assert not caplog.records


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "command",
    [
        "(cd /other && git commit -m x -- a.txt)",
        "cd /other && (git commit -m x -- a.txt)",
        "{ cd /other; git commit -m x -- a.txt; }",
        "if cd /other; then git commit -m x -- a.txt; fi",
        "if false; then\ncd /other\nfi\ngit commit -m x -- a.txt",
        "for d in x; do cd /other; done; git commit -m x -- a.txt",
        "case x in x) cd /other;; esac; git commit -m x -- a.txt",
        "f() { cd /other; }; f; git commit -m x -- a.txt",
        "pushd /other && git commit -m x -- a.txt",
        "builtin cd /other && git commit -m x -- a.txt",
        "env -C /other git commit -m x -- a.txt",
        "env --chdir=/other git commit -m x -- a.txt",
        "CDPATH=/repos cd other && git commit -m x -- a.txt",
        "export CDPATH=/repos; cd other && git commit -m x -- a.txt",
        "x=$(cd /other && git commit -m x -- a.txt)",
        "echo `cd /other && git commit -m x -- a.txt`",
        'echo "$(cd /other && git commit -m x -- a.txt)"',
        "bash -c 'cd /other && git commit -m x -- a.txt'",
        'sh -c "git commit -m x -- a.txt"',
        "eval 'cd /other && git commit -m x -- a.txt'",
        "command env -C /other git commit -m x -- a.txt",
        "FOO=x env -C /other git commit -m x -- a.txt",
        "env bash -c 'cd /other && git commit -m x -- a.txt'",
        "FOO=x bash -c 'cd /other && git commit -m x -- a.txt'",
        "timeout 5 sh -c 'git commit -m x -- a.txt'",
        "command eval 'git commit -m x -- a.txt'",
        "bash -Ec 'cd /other && git commit -m x -- a.txt'",
        "env bash -Cc 'cd /other && git commit -m x -- a.txt'",
    ],
)
async def test_unmodeled_navigation_refuses_before_git(
    guard_harness: GuardHarness,
    caplog: pytest.LogCaptureFixture,
    command: str,
) -> None:
    """Navigation the parser cannot follow fails closed instead of inspecting the tool cwd."""
    from gobby.workflows.commit_guard import foreign_staged_commit_conflict

    invocations = parse_git_commit_invocations(command)
    assert invocations
    assert all(invocation.cwd_unverified for invocation in invocations)
    git_run = AsyncMock()
    with caplog.at_level("WARNING", logger="gobby.workflows.commit_guard"):
        with patch.object(daemon_git, "run", git_run):
            refusal = await foreign_staged_commit_conflict(
                guard_harness.db,
                guard_harness.event(command),
                session_id=guard_harness.current_session.id,
                project_id=guard_harness.project.id,
                project_path=str(guard_harness.repo),
            )

    assert refusal.startswith("Commit blocked:")
    assert "working directory" in refusal
    git_run.assert_not_awaited()
    assert not caplog.records


@pytest.mark.asyncio
async def test_subshell_navigation_cannot_hide_foreign_staged_checkout(
    guard_harness: GuardHarness,
    tmp_path: Path,
) -> None:
    worktree = tmp_path / "owned worktree"
    _git(guard_harness.repo, "worktree", "add", "-q", "-b", "subshell-cd", str(worktree))
    SessionVariableManager(guard_harness.db).merge_variables(
        guard_harness.current_session.id,
        {
            "task_edited_files": {guard_harness.current_task.id: ["foreign.txt"]},
            "task_edited_file_checkouts": {
                guard_harness.current_task.id: {str(worktree): ["foreign.txt"]},
            },
            "session_dirty_files": ["foreign.txt"],
            "session_dirty_file_checkouts": {str(worktree): ["foreign.txt"]},
        },
    )
    (guard_harness.repo / "foreign.txt").write_text("foreign shared bytes\n", encoding="utf-8")
    (worktree / "foreign.txt").write_text("owned isolated bytes\n", encoding="utf-8")
    _git(guard_harness.repo, "add", "--", "foreign.txt")
    _git(worktree, "add", "--", "foreign.txt")
    foreign_index = _git(guard_harness.repo, "write-tree")
    owned_index = _git(worktree, "write-tree")
    command = (
        f"(cd {shlex.quote(str(guard_harness.repo))} && git commit --only -m unsafe -- foreign.txt)"
    )

    response = await guard_harness.handler._evaluate_rules(
        guard_harness.event(command, workdir=worktree)
    )

    assert response.decision == "block"
    assert response.reason is not None
    assert "working directory" in response.reason
    assert _git(guard_harness.repo, "write-tree") == foreign_index
    assert _git(worktree, "write-tree") == owned_index


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("command", "shell"),
    [
        ("set -k; git commit --only -m x -- FOREIGN.TXT GIT_ICASE_PATHSPECS=1", "bash"),
        ("set -o keyword; git commit --only -m x -- FOREIGN.TXT GIT_ICASE_PATHSPECS=1", "bash"),
        ('n=GIT_ICASE_PATHSPECS; env "$n=1" git commit --only -m x -- FOREIGN.TXT', "bash"),
        ('n=GIT_ICASE_PATHSPECS; env "$n=1" git commit --only -m x -- FOREIGN.TXT', "zsh"),
    ],
)
async def test_keyword_and_dynamic_env_refuse_foreign_capture(
    guard_harness: GuardHarness, command: str, shell: str
) -> None:
    if shutil.which(shell) is None:
        pytest.skip(f"{shell} is unavailable")
    (guard_harness.repo / "foreign.txt").write_text("foreign change\n", encoding="utf-8")
    response = await guard_harness.handler._evaluate_rules(guard_harness.event(command))
    subprocess.run([shell, "-c", command], cwd=guard_harness.repo, capture_output=True, check=True)
    assert _git(guard_harness.repo, "show", "--format=", "--name-only", "HEAD") == "foreign.txt"
    assert response.decision == "block"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("setup", "shell"),
    [
        ("set -x;", "bash"),
        ("set -o pipefail;", "bash"),
        ("shopt -s nullglob;", "bash"),
        ('export PATH="$PATH";', "bash"),
        ("declare -a arr=(a b);", "bash"),
        ("set -- -a;", "bash"),
        ("setopt extendedglob;", "zsh"),
    ],
)
async def test_selector_controls_preserve_owned_commit(
    guard_harness: GuardHarness, setup: str, shell: str
) -> None:
    if shutil.which(shell) is None:
        pytest.skip(f"{shell} is unavailable")
    (guard_harness.repo / "foreign.txt").write_text("foreign change\n", encoding="utf-8")
    (guard_harness.repo / "owned.txt").write_text("owned change\n", encoding="utf-8")
    command = f"{setup} git commit -m x -- owned.txt"
    response = await guard_harness.handler._evaluate_rules(guard_harness.event(command))
    subprocess.run([shell, "-c", command], cwd=guard_harness.repo, capture_output=True, check=True)
    assert _git(guard_harness.repo, "show", "--format=", "--name-only", "HEAD") == "owned.txt"
    assert response.decision == "allow"


@pytest.mark.asyncio
@pytest.mark.parametrize("git_word", [r"g$'\x69't", "$(printf gi)t", "`printf gi`t"])
async def test_malformed_later_line_cannot_hide_computed_git_name(
    guard_harness: GuardHarness, git_word: str
) -> None:
    (guard_harness.repo / "foreign.txt").write_text("foreign change\n", encoding="utf-8")
    _git(guard_harness.repo, "add", "foreign.txt")
    command = f"{git_word} commit -m x\necho 'unterminated"
    invocations = parse_git_commit_invocations(command)
    response = await guard_harness.handler._evaluate_rules(guard_harness.event(command))
    execution = subprocess.run(
        ["bash", "-c", command], cwd=guard_harness.repo, capture_output=True, text=True
    )
    assert execution.returncode != 0
    assert "unexpected EOF" in execution.stderr
    assert _git(guard_harness.repo, "show", "--format=", "--name-only", "HEAD") == "foreign.txt"
    assert response.decision == "block"
    assert invocations
    assert all(invocation.cwd_unverified for invocation in invocations)


@pytest.mark.asyncio
@pytest.mark.parametrize("selection", ["keyword", "zsh-allexport"])
async def test_additional_linked_selectors_refuse_foreign_capture(
    guard_harness: GuardHarness, tmp_path: Path, selection: str
) -> None:
    shell = "zsh" if selection == "zsh-allexport" else "bash"
    if shutil.which(shell) is None:
        pytest.skip(f"{shell} is unavailable")
    linked = tmp_path / "linked"
    _git(guard_harness.repo, "worktree", "add", "--detach", str(linked))
    (guard_harness.repo / "foreign.txt").write_text("foreign change\n", encoding="utf-8")
    root = shlex.quote(str(guard_harness.repo))
    git_dir = shlex.quote(str(guard_harness.repo / ".git"))
    commands = {
        "keyword": f"set -k; git commit -a -m x GIT_DIR={git_dir} GIT_WORK_TREE={root}",
        "zsh-allexport": (
            f"setopt allexport; read GIT_DIR <<< {git_dir}; "
            f"read GIT_WORK_TREE <<< {root}; git commit -a -m x"
        ),
    }
    command = commands[selection]
    response = await guard_harness.handler._evaluate_rules(
        guard_harness.event(command, workdir=linked)
    )
    subprocess.run([shell, "-c", command], cwd=linked, capture_output=True, check=True)
    assert _git(guard_harness.repo, "show", "--format=", "--name-only", "HEAD") == "foreign.txt"
    assert response.decision == "block"


@pytest.mark.asyncio
async def test_ownership_query_cancel_blocks_with_evaluation_timeout(
    guard_harness: GuardHarness,
) -> None:
    with patch(
        "gobby.workflows.commit_guard._live_path_owners",
        side_effect=psycopg.errors.QueryCanceled("canceling statement due to statement timeout"),
    ) as lookup:
        response = await guard_harness.handler._evaluate_rules(
            guard_harness.event("git commit -m x")
        )
    lookup.assert_called_once()
    assert response.decision == "block"
    assert "timeout" in (response.reason or "").lower()


async def test_git_path_probe_disables_fsmonitor(tmp_path: Path) -> None:
    from unittest.mock import AsyncMock, patch

    from gobby.utils.daemon_git import GitOk
    from gobby.workflows.commit_guard import _git_paths_async

    result = GitOk(status="ok", argv=("git",), stdout="a.py\0", stderr="")
    with patch(
        "gobby.workflows.commit_guard.daemon_git.run", new=AsyncMock(return_value=result)
    ) as run:
        paths = await _git_paths_async(str(tmp_path), "ls-files", "--cached")
    assert paths == {"a.py"}
    assert "core.fsmonitor=false" in run.call_args.args[0]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "command", ["git -C {repo} commit -m 'cwd gone'", "git commit -m 'cwd gone'"]
)
async def test_foreign_staged_commit_conflict_resolves_repo_when_cwd_vanished(
    guard_harness: GuardHarness,
    tmp_path: Path,
    command: str,
) -> None:
    vanished = tmp_path / "vanished-scratch"
    vanished.mkdir()
    vanished.rmdir()
    (guard_harness.repo / "foreign.txt").write_text("foreign change\n", encoding="utf-8")
    _git(guard_harness.repo, "add", "--", "foreign.txt")

    response = await guard_harness.handler._evaluate_rules(
        guard_harness.event(command.format(repo=guard_harness.repo), workdir=vanished)
    )

    assert response.decision == "block"
    assert response.reason is not None
    assert "foreign.txt" in response.reason
    assert f"#{guard_harness.foreign_task.seq_num}" in response.reason
    assert "could not verify staged-path ownership" not in response.reason


@pytest.mark.asyncio
@pytest.mark.parametrize("relative", [False, True])
async def test_foreign_staged_commit_conflict_names_vanished_cwd(
    guard_harness: GuardHarness,
    tmp_path: Path,
    relative: bool,
) -> None:
    vanished = tmp_path / "gitabbr"
    vanished.mkdir()
    vanished.rmdir()
    if relative:
        # A relative navigation from a vanished cwd has no resolvable target.
        event = guard_harness.event("cd sub && git commit -m x", workdir=vanished)
    else:
        event = guard_harness.event(f"cd {vanished} && git commit -m x")

    response = await guard_harness.handler._evaluate_rules(event)

    assert response.decision == "block"
    assert response.reason is not None
    assert f"working directory {vanished} does not exist" in response.reason
    assert "could not verify staged-path ownership" not in response.reason
