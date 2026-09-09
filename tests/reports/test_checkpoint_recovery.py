"""Real Git recovery preserves reporter evidence and resumes the saved draft."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, Mock

import pytest

from gobby.agents.launcher_session import get_or_create_launcher_session
from gobby.memory.dream.storage import MemoryDreamStore
from gobby.reports.service import SynthesisReporter
from gobby.storage.agents import LocalAgentRunManager
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.projects import PERSONAL_PROJECT_ID
from gobby.workflows.state_manager import SessionVariableManager
from gobby.workflows.task_claim_state import add_claimed_task, remove_claimed_task
from tests.reports.test_publication import _content, _git, _source
from tests.reports.test_service import _reporter

pytestmark = pytest.mark.integration


@dataclass
class _Recovery:
    reporter: SynthesisReporter
    report: dict[str, Any]
    worktree: Path
    child: str
    parent: str
    run_id: str
    first_attempt: str
    content: str


def _recovery(db: HubDatabase, tmp_path: Path, *, legacy: bool = False) -> _Recovery:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.name", "Report Test")
    _git(repo, "config", "user.email", "reports@example.invalid")
    _git(repo, "commit", "--allow-empty", "-m", "baseline")
    reporter = _reporter(db, repo)
    source_id = _source(db, "dream")
    store = reporter.store
    store.request("dream", source_id, PERSONAL_PROJECT_ID)
    first = store.begin("dream", source_id)
    assert first is not None
    report = store.prepare_task("dream", source_id)
    parent = get_or_create_launcher_session(
        reporter.session_manager, PERSONAL_PROJECT_ID, "synthesis-report"
    )
    child = reporter.session_manager.register(
        external_id="report-child", machine_id=None, source="codex", project_id=PERSONAL_PROJECT_ID
    ).id
    task = reporter.task_manager.claim_task(str(report["task_id"]), child)
    root = (tmp_path / "report-worktree").resolve()
    _git(repo, "worktree", "add", "-b", report["branch_name"], str(root), "main")
    worktree = reporter.worktree_storage.create(
        project_id=PERSONAL_PROJECT_ID,
        branch_name=report["branch_name"],
        worktree_path=str(root),
        task_id=task.id,
        agent_session_id=child,
        workspace_role="task",
    )
    runs = LocalAgentRunManager(db)
    run = runs.create(
        parent_session_id=parent,
        child_session_id=child,
        provider="codex",
        prompt="report",
        agent_name="synthesis-reporter",
        task_id=task.id,
        worktree_id=worktree.id,
    )
    runs.start(run.id)
    variables = SessionVariableManager(db)
    variables.merge_variables(child, add_claimed_task({}, task.id, f"#{task.seq_num}"))
    variables.record_edited_files(child, [report["report_path"]], checkout_root=str(root))
    path = root / report["report_path"]
    path.parent.mkdir(parents=True)
    path.write_text("# Earlier incomplete report\n", encoding="utf-8")
    runs.fail(run.id, error="reporter timed out")
    store.attach_agent(first, run.id, worktree.id)
    content = _content(source_id)
    store.save_draft("dream", source_id, content)
    store.fail(first, "reporter timed out", capture_id="preserved-capture")
    if legacy:
        reporter.task_manager.release_task_claim(task.id)
        variables.merge_existing_variables(
            child, remove_claimed_task(variables.get_variables(child), task.id)
        )
    store.retry("dream", source_id)
    return _Recovery(
        reporter, store.get("dream", source_id), root, child, parent, run.id, first, content
    )


@pytest.mark.parametrize("legacy", [False, True])
async def test_dirty_report_checkpoint_then_publication_retry_preserves_source_and_draft(
    temp_db: HubDatabase, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, legacy: bool
) -> None:
    recovery = _recovery(temp_db, tmp_path, legacy=legacy)
    reporter, report = recovery.reporter, recovery.report
    source_id = str(report["source_run_id"])
    source_before = MemoryDreamStore(temp_db).get_run(source_id)
    monkeypatch.setattr("gobby.reports.service.resolve_agent", Mock(return_value=object()))
    spawn = AsyncMock(side_effect=OSError("socket unavailable"))
    monkeypatch.setattr("gobby.reports.service.spawn_agent_impl", spawn)
    await reporter.publish("dream", source_id)
    attempts = reporter.store.attempts("dream", source_id)["attempts"]
    checkpoint = attempts[1]["diagnostics"]["checkpoint"]
    assert checkpoint["success"] is True and checkpoint["run_id"] == recovery.run_id
    assert checkpoint["included_paths"] == [report["report_path"]]
    checkpoint_sha = checkpoint["commit_sha"]
    assert _git(recovery.worktree, "status", "--porcelain") == ""
    assert _git(recovery.worktree, "show", f"{checkpoint_sha}:{report['report_path']}") == (
        "# Earlier incomplete report"
    )
    assert attempts[1]["error"] == "socket unavailable"
    assert reporter.store.get("dream", source_id)["content"] == recovery.content

    async def publish_saved_draft(*args: Any, **kwargs: Any) -> dict[str, Any]:
        assert kwargs["parent_session_id"] == recovery.parent
        assert kwargs["worktree_id"] == report["worktree_id"]
        assert kwargs["task_id"] == report["task_id"]
        assert _git(recovery.worktree, "rev-parse", "HEAD") == checkpoint_sha
        (recovery.worktree / report["report_path"]).write_text(recovery.content, encoding="utf-8")
        _git(recovery.worktree, "add", "--", report["report_path"])
        _git(recovery.worktree, "commit", "-m", "publish saved draft")
        sha = _git(recovery.worktree, "rev-parse", "HEAD")
        # Model the reporter's normal task lifecycle at the provider boundary.
        temp_db.execute(
            "UPDATE tasks SET closed_at = now(), commits = %s WHERE id = %s",
            (json.dumps([sha]), report["task_id"]),
        )
        runs = LocalAgentRunManager(temp_db)
        run = runs.create(
            parent_session_id=recovery.parent,
            child_session_id=recovery.child,
            provider="codex",
            prompt="publish saved draft",
            task_id=str(report["task_id"]),
            worktree_id=str(report["worktree_id"]),
        )
        runs.complete(run.id)
        return {"success": True, "run_id": run.id, "worktree_id": report["worktree_id"]}

    spawn.side_effect = publish_saved_draft
    monkeypatch.setattr(reporter.runner, "get_run", LocalAgentRunManager(temp_db).get)
    await reporter.publish("dream", source_id)
    completed = reporter.store.get("dream", source_id)
    assert completed["status"] == "completed"
    assert completed["content_hash"] == hashlib.sha256(recovery.content.encode()).hexdigest()
    assert completed["commit_sha"] == _git(recovery.worktree, "rev-parse", "HEAD")
    assert completed["task_id"] == report["task_id"]
    assert MemoryDreamStore(temp_db).get_run(source_id) == source_before
    final_attempts = reporter.store.attempts("dream", source_id)["attempts"]
    assert final_attempts[:2] == attempts
    assert final_attempts[2]["diagnostics"]["checkpoint"]["error_code"] == "worktree_clean"
    assert final_attempts[2]["status"] == "completed"
    assert spawn.await_count == 2


@pytest.mark.parametrize("boundary", ["extra_path", "active_run", "foreign_parent"])
async def test_report_recovery_rejects_unsafe_checkpoint_without_launch(
    temp_db: HubDatabase, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, boundary: str
) -> None:
    recovery = _recovery(temp_db, tmp_path)
    reporter, report = recovery.reporter, recovery.report
    if boundary == "extra_path":
        (recovery.worktree / "unrelated.txt").write_text("preserve me\n")
        SessionVariableManager(temp_db).record_edited_files(
            recovery.child, ["unrelated.txt"], checkout_root=str(recovery.worktree)
        )
        expected_error = "unexpected_checkpoint_paths"
    elif boundary == "active_run":
        LocalAgentRunManager(temp_db).create(
            parent_session_id=recovery.parent,
            provider="codex",
            prompt="active writer",
            task_id=str(report["task_id"]),
            worktree_id=str(report["worktree_id"]),
        )
        expected_error = "run_active"
    else:
        monkeypatch.setattr(
            "gobby.reports.service.get_or_create_launcher_session",
            Mock(return_value=recovery.child),
        )
        expected_error = "coordinator_required"
    before = _git(recovery.worktree, "rev-parse", "HEAD")
    dirty_before = _git(recovery.worktree, "status", "--porcelain")
    monkeypatch.setattr("gobby.reports.service.resolve_agent", Mock(return_value=object()))
    spawn = AsyncMock()
    monkeypatch.setattr("gobby.reports.service.spawn_agent_impl", spawn)
    await reporter.publish("dream", str(report["source_run_id"]))
    spawn.assert_not_awaited()
    failed = reporter.store.get("dream", str(report["source_run_id"]))
    assert failed["status"] == "failed" and failed["auto_retries"] == 0
    assert failed["content"] == recovery.content
    attempts = reporter.store.attempts("dream", str(report["source_run_id"]))["attempts"]
    assert attempts[1]["diagnostics"]["checkpoint"]["error_code"] == expected_error
    assert attempts[0]["id"] == recovery.first_attempt
    assert attempts[0]["diagnostics"]["capture_id"] == "preserved-capture"
    assert _git(recovery.worktree, "rev-parse", "HEAD") == before
    assert _git(recovery.worktree, "status", "--porcelain") == dirty_before
