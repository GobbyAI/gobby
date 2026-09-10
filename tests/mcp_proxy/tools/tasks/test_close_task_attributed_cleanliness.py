"""Real Git regressions for target-attributed close cleanliness."""

from __future__ import annotations

import subprocess
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast
from unittest.mock import AsyncMock, patch

import pytest

import gobby.mcp_proxy.tools.tasks._lifecycle_close as lifecycle
import gobby.mcp_proxy.tools.tasks._lifecycle_close_finalization as close_finalization
from gobby.config.app import DaemonConfig
from gobby.mcp_proxy.tools.internal import InternalToolRegistry
from gobby.mcp_proxy.tools.tasks._context import RegistryContext
from gobby.mcp_proxy.tools.tasks._lifecycle_close import register_close_task
from gobby.mcp_proxy.tools.tasks._lifecycle_validation import ValidationResult
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.sessions import SessionManager
from gobby.storage.tasks import LocalTaskManager, Task
from gobby.tasks.acceptance_artifacts import AcceptanceArtifactResult
from gobby.tasks.transcript_evidence import TranscriptEvidence, TranscriptValidationRun
from gobby.tasks.validation import TaskValidator
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


def _successful_transcript(task: Task) -> TranscriptEvidence:
    now = datetime(2026, 9, 9, 12, 0, tzinfo=UTC)
    return TranscriptEvidence(
        validation_runs=(
            TranscriptValidationRun(
                session_id=task.claimed_by_session_id or "",
                source="codex",
                command=(
                    "uv run pytest "
                    "tests/mcp_proxy/tools/tasks/test_close_task_attributed_cleanliness.py -q"
                ),
                categories=("test",),
                matcher_id="pytest",
                label="pytest",
                outcome="success",
                started_at=now,
                completed_at=now,
                order=1,
                exit_code=0,
            ),
        ),
        sessions=(task.claimed_by_session_id or "",),
    )


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
        task_validator_resolver=lambda: cast(TaskValidator, object()),
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


def _create_foreign_attribution(
    harness: CloseHarness,
    *,
    external_id: str,
    path: str,
    expired: bool,
) -> None:
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
    if expired:
        harness.session_manager.update_status(session.id, "expired")


def _valid_review() -> ValidationResult:
    return ValidationResult(
        can_close=True,
        validation_status="valid",
        validation_feedback="Criteria satisfied.",
        reset_reason="llm_valid",
        extra={"verdict": {"status": "valid"}},
    )


@asynccontextmanager
async def _close_evidence(
    harness: CloseHarness,
    *,
    review: AsyncMock | None = None,
) -> AsyncIterator[AsyncMock]:
    review_mock = review or AsyncMock(return_value=_valid_review())
    transcript = _successful_transcript(harness.task)
    acceptance = AcceptanceArtifactResult(
        passed=True,
        tests=(),
        findings=(),
        evidence_files=(),
    )
    with (
        patch.object(lifecycle, "check_linked_committed_bundled_manifest", return_value=None),
        patch.object(lifecycle, "unlinked_tagged_commits", return_value=(([], []), None)),
        patch.object(
            close_finalization,
            "unlinked_tagged_commits",
            return_value=(([], []), None),
        ),
        patch.object(
            lifecycle,
            "_derive_close_transcript_evidence",
            AsyncMock(return_value=transcript),
        ),
        patch.object(lifecycle, "evaluate_acceptance_artifacts", return_value=acceptance),
        patch.object(lifecycle, "evaluate_criteria_review", review_mock),
    ):
        yield review_mock


async def _close_task(
    harness: CloseHarness,
    *,
    review: AsyncMock | None = None,
) -> dict[str, Any]:
    async with _close_evidence(harness, review=review):
        with session_context_for_test(harness.session_id):
            return cast(
                dict[str, Any],
                await harness.registry.call(
                    "close_task",
                    {
                        "task_id": harness.task.id,
                        "changes_summary": "Implemented and exercised target-scoped close proof.",
                        "commit_sha": harness.commit_sha,
                        "preview": True,
                    },
                ),
            )


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
    assert result["clean_proof"] == {"status": "clean"}
    assert close_harness.manager.get_task(close_harness.task.id).closed_at is not None


@pytest.mark.asyncio
async def test_released_target_path_reedited_by_foreign_task_does_not_block_close(
    close_harness: CloseHarness,
) -> None:
    path = "shared.py"
    (close_harness.repo_path / path).write_text("TARGET = True\n", encoding="utf-8")
    _attribute_path(
        close_harness,
        session_id=close_harness.session_id,
        task=close_harness.task,
        path=path,
    )
    _git(close_harness.repo_path, "add", path)
    _git(
        close_harness.repo_path,
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
    close_harness.manager.link_commit(
        close_harness.task.id,
        _git(close_harness.repo_path, "rev-parse", "HEAD"),
    )
    released, remaining = close_harness.variable_manager.release_task_edited_files(
        close_harness.session_id,
        close_harness.task.id,
        [path],
        checkout_root=str(close_harness.repo_path),
    )
    assert released == [path]
    assert remaining == []

    _create_foreign_attribution(
        close_harness,
        external_id="later-foreign-owner",
        path=path,
        expired=False,
    )
    (close_harness.repo_path / path).write_text("FOREIGN = True\n", encoding="utf-8")

    result = await _close_task(close_harness)

    assert result["closed"] is True
    assert result["clean_proof"] == {"status": "clean"}
    assert close_harness.manager.get_task(close_harness.task.id).closed_at is not None


@pytest.mark.asyncio
async def test_target_dirt_blocks_close_evaluation(close_harness: CloseHarness) -> None:
    (close_harness.repo_path / "target.py").write_text("DIRTY = True\n", encoding="utf-8")

    result = await _close_task(close_harness)

    assert result["closed"] is False
    assert result["error"] == "uncommitted_task_edits"
    assert result["clean_proof"] == {"status": "dirty", "dirty_paths": ["target.py"]}
    assert close_harness.manager.get_task(close_harness.task.id).closed_at is None


@pytest.mark.asyncio
async def test_target_dirt_added_during_review_blocks_close_finalization(
    close_harness: CloseHarness,
) -> None:
    async def dirty_target_during_review(**_kwargs: object) -> ValidationResult:
        (close_harness.repo_path / "target.py").write_text("DIRTY = True\n", encoding="utf-8")
        return _valid_review()

    review = AsyncMock(side_effect=dirty_target_during_review)

    result = await _close_task(close_harness, review=review)

    assert result["closed"] is False
    assert result["error"] == "stale_task_state"
    assert result["clean_proof"] == {"status": "dirty", "dirty_paths": ["target.py"]}
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
    assert result["error"] == "task_clean_proof_unavailable"
    assert result["clean_proof"] == {
        "status": "unavailable",
        "reason": "git_status_unavailable",
    }
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
    assert result["error"] == "task_clean_proof_unavailable"
    assert result["clean_proof"] == {
        "status": "unavailable",
        "reason": "git_status_unavailable",
    }
    assert status.await_count == 2
    assert close_harness.manager.get_task(close_harness.task.id).closed_at is None


@pytest.mark.asyncio
async def test_disabled_clean_proof_skips_status_closes_and_clears_attribution(
    close_harness: CloseHarness,
) -> None:
    close_harness.context.startup_config = DaemonConfig(
        gobby_tasks={
            "validation": {
                "require_clean_attributed_paths_on_close": False,
            }
        }
    )
    _attribute_path(
        close_harness,
        session_id=close_harness.session_id,
        task=close_harness.task,
        path="target.py",
    )
    (close_harness.repo_path / "target.py").write_text("DIRTY = True\n", encoding="utf-8")
    status = AsyncMock(side_effect=AssertionError("disabled proof called Git status"))

    with patch.object(daemon_git_client, "status", status):
        result = await _close_task(close_harness)

    assert result["closed"] is True
    assert result["clean_proof"] == {
        "status": "skipped",
        "reason": "disabled_by_configuration",
    }
    status.assert_not_awaited()
    persisted = close_harness.manager.get_task(close_harness.task.id)
    assert persisted.closed_at is not None
    assert persisted.validation_override_reason == "Task clean proof: disabled_by_configuration"
    variables = close_harness.variable_manager.get_variables(close_harness.session_id)
    assert close_harness.task.id not in variables.get("task_edited_files", {})
    assert close_harness.task.id not in variables.get("task_edited_file_checkouts", {})
    assert close_harness.task.id not in variables.get("claimed_tasks", {})
