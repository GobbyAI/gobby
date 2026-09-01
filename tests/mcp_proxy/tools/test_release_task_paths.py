"""Owner-controlled task path release tool tests."""

from __future__ import annotations

import logging
import subprocess
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from gobby.mcp_proxy.tools.internal import InternalToolRegistry
from gobby.mcp_proxy.tools.tasks import create_task_registry
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.projects import Project
from gobby.storage.sessions import SessionManager
from gobby.storage.tasks import LocalTaskManager
from gobby.utils.session_context import session_context_for_test
from gobby.workflows.commit_guard import DirtyEditOwnershipInspectionError
from gobby.workflows.state_manager import SessionVariableManager
from gobby.workflows.task_claim_state import normalize_task_checkout_root
from tests.fixtures.isolated_checkout import install_isolated_checkout_project

pytestmark = pytest.mark.unit

LOCAL_MACHINE_ID = "21000000-0000-4000-8000-000000000002"


@pytest.fixture(autouse=True)
def _local_machine_identity() -> Iterator[None]:
    with patch("gobby.utils.machine_id._cached_machine_id", LOCAL_MACHINE_ID):
        yield


def _checkout_project(temp_db: HubDatabase, repo: Path, name: str) -> Project:
    """Register `repo` as the local checkout of a new project and commit its marker."""
    isolated = install_isolated_checkout_project(
        temp_db, repo, name=name, machine_id=LOCAL_MACHINE_ID
    )
    subprocess.run(["git", "add", ".gobby/project.json"], cwd=repo, check=True)
    subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "marker"],
        cwd=repo,
        check=True,
    )
    return isolated.project


@pytest.mark.asyncio
async def test_release_task_paths_is_owner_only_and_clears_commit_guard_attribution(
    temp_db: HubDatabase,
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    project = _checkout_project(temp_db, repo, "release-task-paths-test")
    sessions = SessionManager(temp_db)
    owner = sessions.register(
        external_id="release-owner",
        machine_id="21000000-0000-4000-8000-000000000002",
        source="codex",
        project_id=project.id,
    )
    foreign = sessions.register(
        external_id="release-foreign",
        machine_id="21000000-0000-4000-8000-000000000002",
        source="claude",
        project_id=project.id,
    )
    tasks = LocalTaskManager(temp_db)
    task = tasks.create_task(
        project_id=project.id,
        title="Release committed task path",
        task_type="bug",
        category="code",
        implementation_domain="backend",
        validation_criteria="The owner can release stale task path attribution.",
        claimed_by_session_id=owner.id,
    )
    variables = SessionVariableManager(temp_db)
    variables.merge_variables(
        owner.id,
        {
            "claimed_tasks": {task.id: f"#{task.seq_num}"},
            "active_task_id": task.id,
            "task_edited_files": {task.id: ["src/committed.py", "src/in-flight.py"]},
        },
    )
    registry = create_task_registry(tasks)

    with session_context_for_test(foreign.id):
        rejected = await registry.call(
            "release_task_paths",
            {"task_id": task.id, "paths": ["src/committed.py"]},
        )

    assert rejected == {
        "success": False,
        "status": "error",
        "error": "Only the task's owning session can release attributed paths",
        "error_code": "TASK_CLAIM_CONFLICT",
        "task_id": task.id,
        "owner_session_id": owner.id,
        "session_id": foreign.id,
    }
    assert variables.get_variables(owner.id)["task_edited_files"][task.id] == [
        "src/committed.py",
        "src/in-flight.py",
    ]

    with session_context_for_test(owner.id):
        invalid = await registry.call(
            "release_task_paths",
            {"task_id": task.id, "paths": ["../outside.py"]},
        )

    assert invalid == {
        "success": False,
        "status": "error",
        "error": "Invalid repository-relative path: '../outside.py'",
        "error_code": "TASK_INVALID_STATUS",
    }

    with session_context_for_test(owner.id):
        released = await registry.call(
            "release_task_paths",
            {"task_id": task.id, "paths": ["./src/committed.py"]},
        )

    assert released == {
        "success": True,
        "task_id": task.id,
        "released_paths": ["src/committed.py"],
        "remaining_paths": ["src/in-flight.py"],
    }
    assert variables.get_variables(owner.id)["task_edited_files"][task.id] == ["src/in-flight.py"]

    assert "release_task_paths" in {item["name"] for item in registry.list_tools()}


SHARED_PATH = "src/shared.py"
RENAMED_PATH = "src/shared_v2.py"


def _committed_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    (repo / "src").mkdir()
    (repo / SHARED_PATH).write_text("committed = True\n", encoding="utf-8")
    subprocess.run(["git", "add", SHARED_PATH], cwd=repo, check=True)
    subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "seed"],
        cwd=repo,
        check=True,
    )
    return repo


def _last_commit_epoch(repo: Path, path: str) -> int:
    output = subprocess.run(
        ["git", "log", "-1", "--format=%ct", "--", path],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    assert output, f"{path} has no commit"
    return int(output)


def _stamp_owner_edits(harness: _Harness, edited_at: dict[str, float]) -> None:
    """Record when the owner session last edited each path under its task."""
    harness.variables.merge_variables(
        harness.owner.id,
        {"task_edited_file_times": {harness.task.id: edited_at}},
    )


@dataclass
class _Harness:
    tasks: LocalTaskManager
    sessions: SessionManager
    variables: SessionVariableManager
    registry: InternalToolRegistry
    project_id: str
    owner: Any
    task: Any
    repo: Path


def _harness(temp_db: HubDatabase, repo: Path) -> _Harness:
    """Owner session with stale (committed) attribution on SHARED_PATH."""
    project = _checkout_project(temp_db, repo, "release-dirty-paths-test")
    sessions = SessionManager(temp_db)
    owner = sessions.register(
        external_id="dirty-release-owner",
        machine_id=LOCAL_MACHINE_ID,
        source="codex",
        project_id=project.id,
    )
    tasks = LocalTaskManager(temp_db)
    task = tasks.create_task(
        project_id=project.id,
        title="Release stale attribution on a foreign-dirty path",
        task_type="bug",
        category="code",
        implementation_domain="backend",
        validation_criteria="Stale attribution releases when foreign dirt accounts for the path.",
        claimed_by_session_id=owner.id,
    )
    variables = SessionVariableManager(temp_db)
    variables.merge_variables(
        owner.id,
        {
            "claimed_tasks": {task.id: f"#{task.seq_num}"},
            "active_task_id": task.id,
            "task_edited_files": {task.id: [SHARED_PATH]},
        },
    )
    return _Harness(
        tasks=tasks,
        sessions=sessions,
        variables=variables,
        registry=create_task_registry(tasks),
        project_id=project.id,
        owner=owner,
        task=task,
        repo=repo,
    )


def _foreign_claimant(harness: _Harness, path: str = SHARED_PATH) -> tuple[Any, Any]:
    """Second active session whose open claimed task holds attribution on ``path``."""
    foreign = harness.sessions.register(
        external_id="dirty-release-foreign",
        machine_id=LOCAL_MACHINE_ID,
        source="claude",
        project_id=harness.project_id,
    )
    other = harness.tasks.create_task(
        project_id=harness.project_id,
        title="Foreign in-flight work on the shared path",
        task_type="bug",
        category="code",
        implementation_domain="backend",
        validation_criteria="The foreign session's dirt stays attributed to it.",
        claimed_by_session_id=foreign.id,
    )
    root = normalize_task_checkout_root(str(harness.repo))
    assert root is not None
    harness.variables.merge_variables(
        foreign.id,
        {
            "claimed_tasks": {other.id: f"#{other.seq_num}"},
            "active_task_id": other.id,
            "task_edited_files": {other.id: [path]},
            "task_edited_file_checkouts": {other.id: {root: [path]}},
        },
    )
    return foreign, other


@pytest.mark.asyncio
async def test_release_succeeds_when_the_dirt_belongs_to_another_sessions_open_task(
    temp_db: HubDatabase,
    tmp_path: Path,
) -> None:
    """The #20818 deadlock shape: stale attribution over another session's live dirt."""
    harness = _harness(temp_db, _committed_repo(tmp_path))
    foreign, other = _foreign_claimant(harness)
    _stamp_owner_edits(harness, {SHARED_PATH: _last_commit_epoch(harness.repo, SHARED_PATH) - 100})
    (harness.repo / SHARED_PATH).write_text(
        "committed = True\nforeign_dirt = True\n", encoding="utf-8"
    )

    with session_context_for_test(harness.owner.id):
        released = await harness.registry.call(
            "release_task_paths",
            {"task_id": harness.task.id, "paths": [SHARED_PATH]},
        )

    assert released == {
        "success": True,
        "task_id": harness.task.id,
        "released_paths": [SHARED_PATH],
        "remaining_paths": [],
        "foreign_dirty_paths": {
            SHARED_PATH: [{"task": f"#{other.seq_num}", "session": f"#{foreign.seq_num}"}]
        },
    }
    owner_variables = harness.variables.get_variables(harness.owner.id)
    assert harness.task.id not in owner_variables.get("task_edited_files", {})
    assert harness.task.id not in owner_variables.get("task_edited_file_times", {})
    foreign_files = harness.variables.get_variables(foreign.id)["task_edited_files"]
    assert foreign_files[other.id] == [SHARED_PATH]


@pytest.mark.asyncio
async def test_release_refuses_own_edit_newer_than_the_last_commit_on_a_co_claimed_path(
    temp_db: HubDatabase,
    tmp_path: Path,
) -> None:
    """The reverse of #20818: another task holding attribution proves nothing about
    whose dirt it is, so the owner's live edit stays attributed and committable."""
    harness = _harness(temp_db, _committed_repo(tmp_path))
    foreign, other = _foreign_claimant(harness)
    _stamp_owner_edits(harness, {SHARED_PATH: _last_commit_epoch(harness.repo, SHARED_PATH) + 100})
    (harness.repo / SHARED_PATH).write_text("committed = True\nown_dirt = True\n", encoding="utf-8")

    with session_context_for_test(harness.owner.id):
        refused = await harness.registry.call(
            "release_task_paths",
            {"task_id": harness.task.id, "paths": [SHARED_PATH]},
        )

    assert refused["success"] is False
    assert refused["error_code"] == "TASK_INVALID_STATUS"
    assert "commit or revert" in refused["error"]
    assert "git stash is blocked" in refused["error"]
    assert refused["dirty_paths"] == [SHARED_PATH]
    owner_files = harness.variables.get_variables(harness.owner.id)["task_edited_files"]
    assert owner_files[harness.task.id] == [SHARED_PATH]
    foreign_files = harness.variables.get_variables(foreign.id)["task_edited_files"]
    assert foreign_files[other.id] == [SHARED_PATH]


@pytest.mark.asyncio
async def test_release_refuses_dirt_with_no_recorded_edit(
    temp_db: HubDatabase,
    tmp_path: Path,
) -> None:
    """Without a stamp the ledger cannot clear the dirt, foreign owner or not."""
    harness = _harness(temp_db, _committed_repo(tmp_path))
    (harness.repo / SHARED_PATH).write_text(
        "committed = True\nunaccounted = True\n", encoding="utf-8"
    )

    with session_context_for_test(harness.owner.id):
        refused = await harness.registry.call(
            "release_task_paths",
            {"task_id": harness.task.id, "paths": [SHARED_PATH]},
        )

    assert refused["success"] is False
    assert refused["error_code"] == "TASK_INVALID_STATUS"
    assert "commit or revert" in refused["error"]
    assert "git stash is blocked" in refused["error"], (
        "the refusal must not leave stashing as the implied exit"
    )
    assert refused["dirty_paths"] == [SHARED_PATH]
    owner_files = harness.variables.get_variables(harness.owner.id)["task_edited_files"]
    assert owner_files[harness.task.id] == [SHARED_PATH]


@pytest.mark.asyncio
async def test_release_omits_the_foreign_report_when_ownership_inspection_errors(
    temp_db: HubDatabase,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Ownership only feeds the report, so an inspection failure does not block a
    release the edit ledger already cleared."""
    harness = _harness(temp_db, _committed_repo(tmp_path))
    _foreign_claimant(harness)
    _stamp_owner_edits(harness, {SHARED_PATH: _last_commit_epoch(harness.repo, SHARED_PATH) - 100})
    (harness.repo / SHARED_PATH).write_text(
        "committed = True\nforeign_dirt = True\n", encoding="utf-8"
    )

    with (
        patch(
            "gobby.mcp_proxy.tools.tasks._lifecycle_paths.foreign_owned_dirty_paths",
            side_effect=DirtyEditOwnershipInspectionError("db down"),
        ),
        session_context_for_test(harness.owner.id),
        caplog.at_level(logging.WARNING, logger="gobby.mcp_proxy.tools.tasks._lifecycle_paths"),
    ):
        released = await harness.registry.call(
            "release_task_paths",
            {"task_id": harness.task.id, "paths": [SHARED_PATH]},
        )

    assert released == {
        "success": True,
        "task_id": harness.task.id,
        "released_paths": [SHARED_PATH],
        "remaining_paths": [],
    }
    assert any("ownership inspection failed" in record.message for record in caplog.records)
    assert harness.task.id not in harness.variables.get_variables(harness.owner.id).get(
        "task_edited_files", {}
    )


@pytest.mark.asyncio
async def test_release_clears_a_path_another_session_renamed(
    temp_db: HubDatabase,
    tmp_path: Path,
) -> None:
    """A foreign staged rename shows the requested path as deleted (git reports a
    rename only when both names are in the pathspec); the stale attribution still
    releases under the name the owner's ledger holds, while the foreign ledger's new
    name stays out of the report."""
    harness = _harness(temp_db, _committed_repo(tmp_path))
    _foreign_claimant(harness, path=RENAMED_PATH)
    _stamp_owner_edits(harness, {SHARED_PATH: _last_commit_epoch(harness.repo, SHARED_PATH) - 100})
    subprocess.run(["git", "mv", SHARED_PATH, RENAMED_PATH], cwd=harness.repo, check=True)

    with session_context_for_test(harness.owner.id):
        released = await harness.registry.call(
            "release_task_paths",
            {"task_id": harness.task.id, "paths": [SHARED_PATH]},
        )

    assert released == {
        "success": True,
        "task_id": harness.task.id,
        "released_paths": [SHARED_PATH],
        "remaining_paths": [],
    }
    assert harness.task.id not in harness.variables.get_variables(harness.owner.id).get(
        "task_edited_files", {}
    )


@pytest.mark.asyncio
async def test_release_refuses_the_owners_own_uncommitted_rename(
    temp_db: HubDatabase,
    tmp_path: Path,
) -> None:
    harness = _harness(temp_db, _committed_repo(tmp_path))
    subprocess.run(["git", "mv", SHARED_PATH, RENAMED_PATH], cwd=harness.repo, check=True)
    harness.variables.merge_variables(
        harness.owner.id,
        {"task_edited_files": {harness.task.id: [SHARED_PATH, RENAMED_PATH]}},
    )
    seed_epoch = _last_commit_epoch(harness.repo, SHARED_PATH)
    _stamp_owner_edits(harness, {SHARED_PATH: seed_epoch - 100, RENAMED_PATH: seed_epoch + 100})

    with session_context_for_test(harness.owner.id):
        refused = await harness.registry.call(
            "release_task_paths",
            {"task_id": harness.task.id, "paths": [SHARED_PATH, RENAMED_PATH]},
        )

    assert refused["success"] is False
    assert refused["error_code"] == "TASK_INVALID_STATUS"
    assert refused["dirty_paths"] == [RENAMED_PATH]
    owner_files = harness.variables.get_variables(harness.owner.id)["task_edited_files"]
    assert owner_files[harness.task.id] == [SHARED_PATH, RENAMED_PATH]


@pytest.mark.asyncio
async def test_inspect_task_path_ownership_reports_staged_dirty_and_unowned_paths(
    temp_db: HubDatabase,
    tmp_path: Path,
) -> None:
    harness = _harness(temp_db, _committed_repo(tmp_path))
    foreign_path = "src/foreign.py"
    unowned_path = "src/unowned.py"
    foreign, other = _foreign_claimant(harness, foreign_path)
    checkout_root = normalize_task_checkout_root(str(harness.repo))
    assert checkout_root is not None
    harness.variables.merge_variables(
        harness.owner.id,
        {
            "task_edited_file_checkouts": {
                harness.task.id: {checkout_root: [SHARED_PATH]},
            }
        },
    )

    (harness.repo / SHARED_PATH).write_text("owner staged\n", encoding="utf-8")
    subprocess.run(["git", "add", "--", SHARED_PATH], cwd=harness.repo, check=True)
    (harness.repo / foreign_path).write_text("foreign dirty\n", encoding="utf-8")
    (harness.repo / unowned_path).write_text("unowned dirty\n", encoding="utf-8")

    with session_context_for_test(harness.owner.id):
        inspected = await harness.registry.call("inspect_task_path_ownership", {})

    assert inspected["success"] is True
    assert inspected["checkout_root"] == checkout_root
    entries = {entry["path"]: entry for entry in inspected["paths"]}
    assert entries[SHARED_PATH] == {
        "path": SHARED_PATH,
        "dirty": True,
        "staged": True,
        "owners": [{"task": f"#{harness.task.seq_num}", "session": f"#{harness.owner.seq_num}"}],
        "unowned": False,
    }
    assert entries[foreign_path] == {
        "path": foreign_path,
        "dirty": True,
        "staged": False,
        "owners": [{"task": f"#{other.seq_num}", "session": f"#{foreign.seq_num}"}],
        "unowned": False,
    }
    assert entries[unowned_path] == {
        "path": unowned_path,
        "dirty": True,
        "staged": False,
        "owners": [],
        "unowned": True,
    }
    assert inspected["unowned_paths"] == [unowned_path]
    assert "inspect_task_path_ownership" in {item["name"] for item in harness.registry.list_tools()}
