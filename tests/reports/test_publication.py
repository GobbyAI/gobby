"""Publication uses durable drafts and Git evidence without replaying source operations."""

import hashlib
import json
import subprocess
from pathlib import Path

import pytest

from gobby.feedback.storage import FeedbackReviewStore
from gobby.memory.dream.storage import MemoryDreamStore
from gobby.reports.publication import verify_publication
from gobby.reports.storage import ReportStore
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.projects import PERSONAL_PROJECT_ID
from gobby.storage.worktrees import LocalWorktreeManager

pytestmark = pytest.mark.integration


def _source(db: HubDatabase, kind: str, status: str = "completed") -> str:
    if kind == "dream":
        dream = MemoryDreamStore(db)
        run_id = dream.create_run(project_id=PERSONAL_PROJECT_ID, dry_run=False, options={})
        dream.update_run(run_id, status=status)
    else:
        feedback = FeedbackReviewStore(db)
        run_id = feedback.create_run(
            dry_run=False, window_start=None, window_end=None, rows_considered=0
        )
        feedback.finalize_run(run_id, status=status)
    return run_id


def _content(run_id: str) -> str:
    return f"# Synthesis {run_id}\n\n## Findings\nNo observations.\n\n## Outcomes\nNo changes.\n\n## Limitations\nHistorical rationale is unavailable.\n"


def _git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(root), *args], check=True, capture_output=True, text=True, timeout=20
    ).stdout.strip()


@pytest.mark.parametrize("kind", ["feedback", "dream"])
@pytest.mark.parametrize("status", ["completed", "partial", "failed", "interrupted"])
def test_terminal_sources_can_publish_without_changing_source(
    temp_db: HubDatabase, kind: str, status: str
) -> None:
    run_id = _source(temp_db, kind, status)
    store = ReportStore(temp_db)
    report = store.request(kind, run_id, PERSONAL_PROJECT_ID)
    assert report["status"] == "pending"
    assert report["report_path"] == f"docs/reports/{kind}/{run_id}.md"
    store.request(kind, run_id, PERSONAL_PROJECT_ID)
    assert len(store.pending()) == 1
    if kind == "dream":
        source = MemoryDreamStore(temp_db).get_run(run_id)
        assert source is not None and source["status"] == status
    else:
        feedback = FeedbackReviewStore(temp_db).get_run(run_id)
        assert feedback is not None and feedback.status == status


def test_failed_attempts_and_draft_survive_bounded_retry_and_restart(temp_db: HubDatabase) -> None:
    run_id = _source(temp_db, "feedback")
    store = ReportStore(temp_db)
    store.request("feedback", run_id, PERSONAL_PROJECT_ID)
    first = store.begin("feedback", run_id)
    assert first is not None
    assert store.begin("feedback", run_id) is None
    task_id = store.prepare_task("feedback", run_id)["task_id"]
    content = _content(run_id)
    store.save_draft("feedback", run_id, content)
    store.fail(first, "Git lock unavailable", transient=True, command="git commit")
    assert store.get("feedback", run_id)["status"] == "pending"
    second = store.begin("feedback", run_id)
    assert second is not None and second != first
    assert store.prepare_task("feedback", run_id)["task_id"] == task_id
    store.recover()
    report = store.get("feedback", run_id)
    assert report["status"] == "interrupted"
    assert report["content"] == content
    assert report["auto_retries"] == 1
    page = store.attempts("feedback", run_id, limit=1)
    assert page["attempts"][0]["error"] == "Git lock unavailable"
    assert page["attempts"][0]["diagnostics"] == {"command": "git commit"}
    assert page["next_offset"] == 1
    assert store.attempts("feedback", run_id, offset=1)["attempts"][0]["status"] == "interrupted"
    store.retry("feedback", run_id)
    assert store.get("feedback", run_id)["status"] == "pending"
    assert store.prepare_task("feedback", run_id)["task_id"] == task_id


def test_validation_failure_stays_explicit(temp_db: HubDatabase) -> None:
    run_id = _source(temp_db, "dream")
    store = ReportStore(temp_db)
    store.request("dream", run_id, PERSONAL_PROJECT_ID)
    attempt = store.begin("dream", run_id)
    assert attempt is not None
    with pytest.raises(ValueError, match="Findings"):
        store.save_draft("dream", run_id, "unsupported assertions")
    store.fail(attempt, "Report lacks evidence sections")
    report = store.get("dream", run_id)
    assert report["status"] == "failed" and report["auto_retries"] == 0
    assert report["content"] is None


async def test_git_publication_verifies_hash_task_commit_and_retains_branch(
    temp_db: HubDatabase, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_id = _source(temp_db, "dream")
    store = ReportStore(temp_db)
    store.request("dream", run_id, PERSONAL_PROJECT_ID)
    attempt = store.begin("dream", run_id)
    assert attempt is not None
    report = store.prepare_task("dream", run_id)
    content = _content(run_id)
    store.save_draft("dream", run_id, content)
    _git(tmp_path, "init", "-b", "main")
    _git(tmp_path, "config", "user.email", "test@example.invalid")
    _git(tmp_path, "config", "user.name", "Report Test")
    _git(tmp_path, "commit", "--allow-empty", "-m", "baseline")
    _git(tmp_path, "checkout", "-b", report["branch_name"])
    path = tmp_path / report["report_path"]
    path.parent.mkdir(parents=True)
    path.write_text(content)
    _git(tmp_path, "add", "--", report["report_path"])
    _git(tmp_path, "commit", "-m", "report")
    sha = _git(tmp_path, "rev-parse", "HEAD")
    _git(tmp_path, "checkout", "main")
    worktree = LocalWorktreeManager(temp_db).create(
        project_id=PERSONAL_PROJECT_ID,
        branch_name=report["branch_name"],
        worktree_path=str(tmp_path),
        task_id=str(report["task_id"]),
    )
    temp_db.execute(
        "UPDATE synthesis_reports SET worktree_id = %s WHERE source_run_id = %s",
        (worktree.id, run_id),
    )
    report = store.get("dream", run_id)
    with pytest.raises(ValueError, match="closed documentation task"):
        verify_publication(store, report, tmp_path, attempt)
    temp_db.execute(
        "UPDATE tasks SET closed_at = now(), commits = %s WHERE id = %s",
        (json.dumps([sha]), report["task_id"]),
    )
    with pytest.raises(ValueError, match="draft hash"):
        verify_publication(store, {**report, "content_hash": "wrong"}, tmp_path, attempt)
    # A restart after commit/task close verifies the saved draft directly;
    # no second synthesis agent is needed to finish publication.
    from unittest.mock import AsyncMock

    from tests.reports.test_service import _reporter

    reporter = _reporter(temp_db, tmp_path)
    spawn = AsyncMock()
    monkeypatch.setattr(reporter, "_spawn", spawn)
    await reporter.publish("dream", run_id)
    spawn.assert_not_called()
    completed = store.get("dream", run_id)
    assert completed["status"] == "completed" and completed["commit_sha"] == sha
    assert completed["content_hash"] == hashlib.sha256(content.encode()).hexdigest()
    assert not path.exists()
    assert _git(tmp_path, "rev-parse", report["branch_name"]) == sha
    assert store.attempts("dream", run_id)["attempts"][0]["status"] == "completed"


def test_nonterminal_source_rejected(temp_db: HubDatabase) -> None:
    run_id = _source(temp_db, "dream", "running")
    with pytest.raises(ValueError, match="terminal"):
        ReportStore(temp_db).request("dream", run_id, PERSONAL_PROJECT_ID)
