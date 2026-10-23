"""Real Git regressions for target-attributed close cleanliness."""

from __future__ import annotations

import subprocess
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

import gobby.mcp_proxy.tools.tasks._lifecycle_close as lifecycle
import gobby.mcp_proxy.tools.tasks._lifecycle_close_finalization as close_finalization
from gobby.mcp_proxy.tools.internal import InternalToolRegistry
from gobby.mcp_proxy.tools.tasks._context import RegistryContext
from gobby.mcp_proxy.tools.tasks._lifecycle_close_tool import register_close_task
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.session_models import Session
from gobby.storage.sessions import SessionManager
from gobby.storage.tasks import LocalTaskManager, Task
from gobby.tasks.close_checklist import CloseChecklist
from gobby.tasks.close_invariants import evaluate_close_invariants
from gobby.utils.daemon_git import GitFailed, GitOk
from gobby.utils.daemon_git import daemon_git as daemon_git_client
from gobby.utils.machine_id import require_machine_id
from gobby.utils.session_context import session_context_for_test
from gobby.workflows.state_manager import SessionVariableManager

pytestmark = pytest.mark.unit


@dataclass(frozen=True, slots=True)
class CloseHarness:
    manager: LocalTaskManager
    session_manager: SessionManager
    variable_manager: SessionVariableManager
    registry: InternalToolRegistry
    context: RegistryContext
    task: Task
    session_id: str
    repo_path: Path
    commit_sha: str


def _git(repo_path: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=repo_path,
        check=True,
        capture_output=True,
        text=True,
        timeout=10,
    )
    return result.stdout.strip()


def _commit_fixture_paths(repo_path: Path) -> str:
    for name in ("foreign.py", "expired.py"):
        (repo_path / name).write_text(f'VALUE = "{name} baseline"\n', encoding="utf-8")
    _git(repo_path, "add", "foreign.py", "expired.py")
    _git(
        repo_path,
        "-c",
        "user.name=Gobby Tests",
        "-c",
        "user.email=gobby-tests@example.com",
        "commit",
        "--no-gpg-sign",
        "-q",
        "-m",
        "foreign fixture baselines",
    )
    (repo_path / "target.py").write_text('VALUE = "target baseline"\n', encoding="utf-8")
    _git(repo_path, "add", "target.py")
    _git(
        repo_path,
        "-c",
        "user.name=Gobby Tests",
        "-c",
        "user.email=gobby-tests@example.com",
        "commit",
        "--no-gpg-sign",
        "-q",
        "-m",
        "target fixture",
    )
    return _git(repo_path, "rev-parse", "HEAD")


@pytest.fixture
def close_harness(
    temp_db: HubDatabase,
    sample_git_project: dict[str, Any],
) -> CloseHarness:
    repo_path = Path(sample_git_project["repo_path"])
    commit_sha = _commit_fixture_paths(repo_path)
    manager = LocalTaskManager(temp_db)
    session_manager = SessionManager(temp_db)
    variable_manager = SessionVariableManager(temp_db)
    session = session_manager.register(
        external_id="target-close-cleanliness",
        machine_id=require_machine_id(),
        source="codex",
        project_id=sample_git_project["id"],
    )
    task = manager.create_task(
        project_id=sample_git_project["id"],
        title="Target attributed close cleanliness",
        category="code",
        task_type="task",
        implementation_domain="backend",
        validation_criteria="Focused tests pass.",
    )
    task = manager.claim_task(task.id, session.id)
    task = manager.link_commit(task.id, commit_sha)
    context = RegistryContext(
        task_manager=manager,
    )
    registry = InternalToolRegistry("gobby-tasks")
    register_close_task(registry, context)
    return CloseHarness(
        manager=manager,
        session_manager=session_manager,
        variable_manager=variable_manager,
        registry=registry,
        context=context,
        task=task,
        session_id=session.id,
        repo_path=repo_path,
        commit_sha=commit_sha,
    )


def _attribute_path(
    harness: CloseHarness,
    *,
    session_id: str,
    task: Task,
    path: str,
) -> None:
    harness.variable_manager.merge_variables(
        session_id,
        {
            "task_claimed": True,
            "claimed_tasks": {task.id: f"#{task.seq_num}"},
            "active_task_id": task.id,
            "task_edited_files": {task.id: [path]},
            "task_edited_file_checkouts": {
                task.id: {str(harness.repo_path): [path]},
            },
        },
    )


def _hold_session_dirt(harness: CloseHarness, *, session_id: str, path: str) -> None:
    """Record ``path`` as live session dirt, the ledger edit ownership judges."""
    harness.variable_manager.merge_variables(
        session_id,
        {
            "session_dirty_files": [path],
            "session_dirty_file_checkouts": {str(harness.repo_path): [path]},
        },
    )


def _create_foreign_attribution(
    harness: CloseHarness,
    *,
    external_id: str,
    path: str,
    expired: bool,
) -> tuple[Session, Task]:
    session = harness.session_manager.register(
        external_id=external_id,
        machine_id=require_machine_id(),
        source="codex",
        project_id=harness.task.project_id,
    )
    task = harness.manager.create_task(
        project_id=harness.task.project_id,
        title=f"Foreign owner for {path}",
        category="code",
        task_type="task",
        implementation_domain="backend",
        validation_criteria="Foreign task fixture.",
    )
    task = harness.manager.claim_task(task.id, session.id)
    _attribute_path(harness, session_id=session.id, task=task, path=path)
    _hold_session_dirt(harness, session_id=session.id, path=path)
    if expired:
        harness.session_manager.update_status(session.id, "expired")
    return session, task


@asynccontextmanager
async def _recorded_clean_proof_paths() -> AsyncIterator[list[frozenset[str]]]:
    """Record every checkout's attributed path set passed to the invariants."""
    proof_sets: list[frozenset[str]] = []

    async def spy_evaluate(**kwargs: Any) -> CloseChecklist:
        proof_sets.extend(frozenset(paths) for paths in kwargs["attributed_checkouts"].values())
        return await evaluate_close_invariants(**kwargs)

    with patch.object(lifecycle, "evaluate_close_invariants", spy_evaluate):
        yield proof_sets


async def _close_task(
    harness: CloseHarness, *, before_finalize: Callable[[], None] | None = None
) -> dict[str, Any]:
    with session_context_for_test(harness.session_id):
        evaluation = await lifecycle._evaluate_close(
            harness.context,
            task_id=harness.task.id,
            reason="completed",
            commit_sha=harness.commit_sha,
            project_path=None,
            response_detail="diagnostic",
        )
        if not evaluation.ready:
            return evaluation.response(preview=False)
        if before_finalize is not None:
            before_finalize()
        return await close_finalization.commit_close(
            harness.context,
            evaluation,
            reason="completed",
            changes_summary="Verified task-attributed cleanliness.",
            commit_sha=harness.commit_sha,
        )


def _clean_gate(result: dict[str, Any]) -> dict[str, Any]:
    return next(gate for gate in result["gates"] if gate["name"] == "uncommitted_task_edits")


@pytest.mark.asyncio
async def test_clean_linked_target_closes_amid_unowned_foreign_and_expired_dirt(
    close_harness: CloseHarness,
) -> None:
    _create_foreign_attribution(
        close_harness,
        external_id="active-foreign-owner",
        path="foreign.py",
        expired=False,
    )
    _create_foreign_attribution(
        close_harness,
        external_id="expired-foreign-owner",
        path="expired.py",
        expired=True,
    )
    (close_harness.repo_path / "foreign.py").write_text("ACTIVE = True\n", encoding="utf-8")
    (close_harness.repo_path / "expired.py").write_text("EXPIRED = True\n", encoding="utf-8")
    (close_harness.repo_path / "unowned.py").write_text("UNOWNED = True\n", encoding="utf-8")

    result = await _close_task(close_harness)

    assert result["closed"] is True
    assert _clean_gate(result)["status"] == "passed"
    assert close_harness.manager.get_task(close_harness.task.id).closed_at is not None


def _commit_target_path(harness: CloseHarness, path: str) -> None:
    """Commit ``path`` as the target task's work, keeping its attribution history."""
    (harness.repo_path / path).write_text("TARGET = True\n", encoding="utf-8")
    _attribute_path(harness, session_id=harness.session_id, task=harness.task, path=path)
    _git(harness.repo_path, "add", path)
    _git(
        harness.repo_path,
        "-c",
        "user.name=Gobby Tests",
        "-c",
        "user.email=gobby-tests@example.com",
        "commit",
        "--no-gpg-sign",
        "-q",
        "-m",
        "target task commit",
    )
    harness.manager.link_commit(harness.task.id, _git(harness.repo_path, "rev-parse", "HEAD"))


@pytest.mark.asyncio
async def test_retained_target_path_reedited_by_foreign_task_does_not_block_close(
    close_harness: CloseHarness,
) -> None:
    """Committed target work a live foreign session re-dirties is not the target's dirt."""
    path = "shared.py"
    _commit_target_path(close_harness, path)
    _create_foreign_attribution(
        close_harness,
        external_id="later-foreign-owner",
        path=path,
        expired=False,
    )
    (close_harness.repo_path / path).write_text("FOREIGN = True\n", encoding="utf-8")

    async with _recorded_clean_proof_paths() as proof_sets:
        result = await _close_task(close_harness)

    assert result["closed"] is True
    assert _clean_gate(result)["status"] == "passed"
    assert all(path not in proof for proof in proof_sets)
    assert close_harness.manager.get_task(close_harness.task.id).closed_at is not None


@pytest.mark.asyncio
@pytest.mark.parametrize("commit_fallback", [False, True])
async def test_target_path_redirtied_by_owner_blocks_close_despite_foreign_holder(
    close_harness: CloseHarness,
    commit_fallback: bool,
) -> None:
    """The owner's own live dirt stays in the proof even when a foreign session holds it.

    With ``commit_fallback`` the volatile attribution is gone (fresh claim or
    recovery), so the path reaches the proof only through the linked commit.
    """
    path = "shared.py"
    _commit_target_path(close_harness, path)
    if commit_fallback:
        close_harness.variable_manager.set_variable(
            close_harness.session_id, "task_edited_files", {}
        )
        close_harness.variable_manager.set_variable(
            close_harness.session_id, "task_edited_file_checkouts", {}
        )
    _create_foreign_attribution(
        close_harness,
        external_id="concurrent-foreign-owner",
        path=path,
        expired=False,
    )
    (close_harness.repo_path / path).write_text("OWNER_AGAIN = True\n", encoding="utf-8")
    _hold_session_dirt(close_harness, session_id=close_harness.session_id, path=path)

    result = await _close_task(close_harness)

    assert result["closed"] is False
    assert result["error"] == "uncommitted_task_edits"
    assert _clean_gate(result)["details"]["dirty_checkouts"] == {
        str(close_harness.repo_path): [path]
    }
    assert close_harness.manager.get_task(close_harness.task.id).closed_at is None


@pytest.mark.asyncio
async def test_stale_clean_foreign_entry_is_checked_without_mutating_its_owner(
    close_harness: CloseHarness,
) -> None:
    """A clean foreign-named linked path is checked without releasing another session's ledger."""
    foreign_session, foreign_task = _create_foreign_attribution(
        close_harness,
        external_id="stale-clean-foreign-owner",
        path="target.py",
        expired=False,
    )
    async with _recorded_clean_proof_paths() as proof_sets:
        result = await _close_task(close_harness)

    assert result["closed"] is True
    assert _clean_gate(result)["status"] == "passed"
    assert frozenset({"target.py"}) in proof_sets
    foreign_vars = close_harness.variable_manager.get_variables(foreign_session.id)
    assert "target.py" in foreign_vars["session_dirty_files"]
    assert foreign_vars["task_edited_files"][foreign_task.id] == ["target.py"]


@pytest.mark.asyncio
async def test_dirty_foreign_entry_stays_dropped_from_clean_proof(
    close_harness: CloseHarness,
) -> None:
    """Real uncommitted foreign dirt keeps its entry and stays out of the proof."""
    foreign_session, foreign_task = _create_foreign_attribution(
        close_harness,
        external_id="dirty-foreign-owner",
        path="target.py",
        expired=False,
    )
    (close_harness.repo_path / "target.py").write_text('VALUE = "foreign dirt"\n', encoding="utf-8")
    async with _recorded_clean_proof_paths() as proof_sets:
        result = await _close_task(close_harness)

    assert result["closed"] is True
    assert _clean_gate(result)["status"] == "passed"
    assert frozenset({"target.py"}) not in proof_sets
    foreign_vars = close_harness.variable_manager.get_variables(foreign_session.id)
    assert foreign_vars["task_edited_files"][foreign_task.id] == ["target.py"]


@pytest.mark.asyncio
async def test_target_dirt_blocks_close_evaluation(close_harness: CloseHarness) -> None:
    (close_harness.repo_path / "target.py").write_text("DIRTY = True\n", encoding="utf-8")

    result = await _close_task(close_harness)

    assert result["closed"] is False
    assert result["error"] == "uncommitted_task_edits"
    assert _clean_gate(result)["details"]["dirty_checkouts"] == {
        str(close_harness.repo_path): ["target.py"]
    }
    assert close_harness.manager.get_task(close_harness.task.id).closed_at is None


@pytest.mark.asyncio
async def test_target_dirt_added_after_preview_blocks_close_finalization(
    close_harness: CloseHarness,
) -> None:
    def dirty_target() -> None:
        (close_harness.repo_path / "target.py").write_text("DIRTY = True\n", encoding="utf-8")

    result = await _close_task(close_harness, before_finalize=dirty_target)
    assert result["closed"] is False
    assert result["error"] == "uncommitted_task_edits"
    assert _clean_gate(result)["details"]["dirty_checkouts"] == {
        str(close_harness.repo_path): ["target.py"]
    }
    assert close_harness.manager.get_task(close_harness.task.id).closed_at is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "git_result",
    [
        pytest.param(
            GitFailed(
                status="failed",
                argv=("status",),
                returncode=128,
                stdout="",
                stderr="Git unavailable",
            ),
            id="unavailable",
        ),
        pytest.param(
            GitOk(
                status="ok",
                argv=("status",),
                stdout="R  target.py\0",
                stderr="",
            ),
            id="malformed",
        ),
        pytest.param(
            GitOk(status="ok", argv=("status",), stdout="\0", stderr=""),
            id="nul-only",
        ),
    ],
)
async def test_unprovable_git_status_fails_close_evaluation(
    close_harness: CloseHarness,
    git_result: GitOk | GitFailed,
) -> None:
    status = AsyncMock(return_value=git_result)

    with patch.object(daemon_git_client, "status", status):
        result = await _close_task(close_harness)

    assert result["closed"] is False
    assert result["error"] == "uncommitted_task_edits"
    assert _clean_gate(result)["details"]["unavailable_checkouts"] == [str(close_harness.repo_path)]
    status.assert_awaited_once()
    assert close_harness.manager.get_task(close_harness.task.id).closed_at is None


@pytest.mark.asyncio
@pytest.mark.parametrize("malformed_stdout", ["\0", "R  target.py\0"])
async def test_malformed_git_status_fails_close_finalization(
    close_harness: CloseHarness,
    malformed_stdout: str,
) -> None:
    status = AsyncMock(
        side_effect=[
            GitOk(status="ok", argv=("status",), stdout="", stderr=""),
            GitOk(
                status="ok",
                argv=("status",),
                stdout=malformed_stdout,
                stderr="",
            ),
        ]
    )

    with patch.object(daemon_git_client, "status", status):
        result = await _close_task(close_harness)

    assert result["closed"] is False
    assert result["error"] == "uncommitted_task_edits"
    assert _clean_gate(result)["details"]["unavailable_checkouts"] == [str(close_harness.repo_path)]
    assert status.await_count == 2
    assert close_harness.manager.get_task(close_harness.task.id).closed_at is None


@pytest.mark.asyncio
async def test_clean_close_clears_attribution_without_validation_override(
    close_harness: CloseHarness,
) -> None:
    _attribute_path(
        close_harness,
        session_id=close_harness.session_id,
        task=close_harness.task,
        path="target.py",
    )
    result = await _close_task(close_harness)
    assert result["closed"] is True
    assert _clean_gate(result)["status"] == "passed"
    persisted = close_harness.manager.get_task(close_harness.task.id)
    assert persisted.closed_at is not None
    assert persisted.validation_override_reason is None
    variables = close_harness.variable_manager.get_variables(close_harness.session_id)
    assert close_harness.task.id not in variables.get("task_edited_files", {})
    assert close_harness.task.id not in variables.get("task_edited_file_checkouts", {})
    assert close_harness.task.id not in variables.get("claimed_tasks", {})


@pytest.mark.asyncio
async def test_later_live_claim_takes_an_uncommitted_path_from_the_earlier_claim(
    close_harness: CloseHarness,
) -> None:
    """The earlier claim's close stops listing a path that a later live claim of the
    same session edited after the earlier claim's interval reconciled clean."""
    repo = str(close_harness.repo_path)
    later = close_harness.manager.create_task(
        project_id=close_harness.task.project_id,
        title="Later live claim takes the path",
        category="code",
        task_type="task",
        implementation_domain="backend",
        validation_criteria="Focused tests pass.",
    )
    close_harness.manager.claim_task(later.id, close_harness.session_id)
    variables = close_harness.variable_manager
    variables.merge_variables(
        close_harness.session_id,
        {
            "claimed_tasks": {
                close_harness.task.id: f"#{close_harness.task.seq_num}",
                later.id: f"#{later.seq_num}",
            },
            "active_task_id": close_harness.task.id,
        },
    )
    variables.record_edited_files(close_harness.session_id, ["shared.py"], checkout_root=repo)
    variables.release_session_dirty_files(
        close_harness.session_id, ["shared.py"], checkout_root=repo
    )
    variables.merge_variables(close_harness.session_id, {"active_task_id": later.id})
    (close_harness.repo_path / "shared.py").write_text("LATER = True\n", encoding="utf-8")
    variables.record_edited_files(close_harness.session_id, ["shared.py"], checkout_root=repo)

    result = await _close_task(close_harness)

    assert _clean_gate(result)["status"] == "passed"
    assert result["closed"] is True
