"""Claim-window derivation for the close-time commit autolink."""

from __future__ import annotations

import os
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

from gobby.mcp_proxy.tools.tasks._close_evaluation_support import claimed_session_window_start
from gobby.mcp_proxy.tools.tasks._context import RegistryContext
from gobby.mcp_proxy.tools.tasks._lifecycle_close_preview import resolve_close_commit_shas
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.session_tasks import SessionTaskManager
from gobby.storage.sessions import SessionManager
from gobby.storage.tasks import LocalTaskManager, Task
from gobby.utils import machine_id as machine_identity

pytestmark = pytest.mark.unit

TASK_ID = "00000000-0000-4000-8000-000000000101"
OWNER = "00000000-0000-4000-8000-000000000301"
EARLIER_SESSION = "00000000-0000-4000-8000-000000000302"


def _row(session_id: str, action: str, hour: int, minute: int = 0) -> dict[str, Any]:
    return {
        "session_id": session_id,
        "action": action,
        "created_at": datetime(2026, 9, 1, hour, minute, tzinfo=UTC),
    }


# Storage returns rows newest first.
ROWS = [
    _row(OWNER, "claimed", 13, 30),
    _row(OWNER, "claimed", 13),
    _row(EARLIER_SESSION, "worked_on", 12, 30),
    _row(EARLIER_SESSION, "claimed", 12),
    _row(EARLIER_SESSION, "created", 11),
]


def _ctx(rows: list[dict[str, Any]] | Exception) -> RegistryContext:
    def get_task_sessions(_task_id: str) -> list[dict[str, Any]]:
        if isinstance(rows, Exception):
            raise rows
        return rows

    return cast(
        RegistryContext,
        SimpleNamespace(session_task_manager=SimpleNamespace(get_task_sessions=get_task_sessions)),
    )


def _task(owner: str | None) -> Task:
    return Task(
        id=TASK_ID,
        project_id="00000000-0000-4000-8000-000000000201",
        title="Windowed leaf",
        category="code",
        priority=2,
        task_type="task",
        created_at=datetime(2026, 9, 1, 10, tzinfo=UTC),
        updated_at=datetime(2026, 9, 1, 10, tzinfo=UTC),
        claimed_by_session_id=owner,
    )


def test_owned_task_uses_the_owners_latest_claim() -> None:
    window = claimed_session_window_start(_ctx(ROWS), _task(OWNER), TASK_ID)

    assert window == "2026-09-01T13:30:00+00:00"


def test_unowned_task_falls_back_to_the_earliest_linked_window() -> None:
    # Escalation cleared the owner; the earlier claimant's window still bounds the scan,
    # and a bare "created" link does not count as evidence.
    window = claimed_session_window_start(_ctx(ROWS), _task(None), TASK_ID)

    assert window == "2026-09-01T12:00:00+00:00"


def test_unowned_task_without_evidence_links_has_no_window() -> None:
    window = claimed_session_window_start(_ctx([_row(OWNER, "created", 11)]), _task(None), TASK_ID)

    assert window is None


def test_unowned_task_with_unreadable_history_has_no_window() -> None:
    window = claimed_session_window_start(_ctx(RuntimeError("db down")), _task(None), TASK_ID)

    assert window is None


def _commit(repo: Path, message: str, *, committed_at: str | None = None) -> str:
    env = dict(os.environ)
    if committed_at:
        env["GIT_AUTHOR_DATE"] = committed_at
        env["GIT_COMMITTER_DATE"] = committed_at
    git = ["git", "-c", "user.name=Gobby Tests", "-c", "user.email=gobby-tests@example.com"]
    subprocess.run(
        [*git, "commit", "--allow-empty", "--no-gpg-sign", "-q", "-m", message],
        cwd=repo,
        check=True,
        timeout=10,
        env=env,
    )
    return subprocess.run(
        ["git", "rev-parse", "--short", "HEAD"],
        cwd=repo,
        check=True,
        timeout=10,
        capture_output=True,
        text=True,
    ).stdout.strip()


@pytest.mark.integration
def test_escalated_then_de_escalated_task_resolves_commits_from_the_prior_claimants_window(
    temp_db: HubDatabase, sample_git_project: dict[str, Any]
) -> None:
    """Escalation clears the owner; the earlier claimant's window still bounds the scan."""
    task_manager = LocalTaskManager(temp_db)
    session_tasks = SessionTaskManager(temp_db)
    project_id = sample_git_project["id"]
    project_name = sample_git_project["name"]
    repo = Path(sample_git_project["repo_path"])
    claimant = SessionManager(temp_db).register(
        external_id="prior-claimant",
        machine_id=machine_identity.require_machine_id(),
        source="cli",
        project_id=project_id,
        title="Prior claimant",
    )
    task = task_manager.create_task(
        project_id=project_id,
        title="Windowed leaf",
        validation_criteria="Tagged commits from the prior claimant are resolved.",
    )
    tag = f"[{project_name}-#{task.seq_num}]"
    before_claim = _commit(
        repo, f"{tag} committed before anyone claimed", committed_at="2026-01-01T00:00:00+00:00"
    )

    task_manager.claim_task(task.id, claimant.id)
    session_tasks.link_task(claimant.id, task.id, "claimed")
    in_window = _commit(repo, f"{tag} committed inside the prior claimant's window")
    task_manager.escalate_task(task.id, "three review failures")
    task_manager.de_escalate_task(task.id, "human review done")

    fresh = task_manager.get_task(task.id)
    assert fresh.claimed_by_session_id is None
    ctx = cast(RegistryContext, SimpleNamespace(session_task_manager=session_tasks))
    window = claimed_session_window_start(ctx, fresh, task.id)
    assert window is not None

    shas, error = resolve_close_commit_shas(
        task_manager,
        task=fresh,
        task_id=task.id,
        claim_started_at=window,
        commit_sha=None,
        cwd=str(repo),
        project_name=project_name,
    )

    assert error is None
    assert shas == [in_window]
    assert before_claim not in shas
