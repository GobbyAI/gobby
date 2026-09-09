"""Reporter retries and restart recovery stay separate from source execution."""

from pathlib import Path
from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock, Mock

import pytest

from gobby.agents.runner import AgentRunner
from gobby.config.app import DaemonConfig
from gobby.events.completion_registry import CompletionEventRegistry
from gobby.feedback.storage import FeedbackReviewStore
from gobby.memory.dream.cron import reconcile_interrupted_dream_runs
from gobby.memory.dream.protocols import MemoryDreamManagerProtocol
from gobby.memory.dream.storage import MemoryDreamStore
from gobby.reports.service import SynthesisReporter
from gobby.reports.storage import ReportStore
from gobby.storage.agents import LocalAgentRunManager
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.projects import PERSONAL_PROJECT_ID
from gobby.storage.sessions import SessionManager
from gobby.storage.tasks import LocalTaskManager
from gobby.storage.worktrees import LocalWorktreeManager
from gobby.worktrees.git import WorktreeGitManager
from tests.reports.test_publication import _source

pytestmark = pytest.mark.integration


def _reporter(db: HubDatabase, root: Path) -> SynthesisReporter:
    return SynthesisReporter(
        db=db,
        runner=cast(AgentRunner, Mock(spec=AgentRunner)),
        session_manager=SessionManager(db),
        completion_registry=CompletionEventRegistry(),
        task_manager=LocalTaskManager(db),
        worktree_storage=LocalWorktreeManager(db),
        git_manager=cast(WorktreeGitManager, SimpleNamespace(repo_path=root)),
        daemon_config=cast(DaemonConfig, Mock(spec=DaemonConfig)),
        project_id=PERSONAL_PROJECT_ID,
    )


@pytest.mark.parametrize(
    "spawn_error",
    [
        "socket unavailable",
        "Failed to prepare reused worktree: Timed out rebasing reused worktree onto 0.5.0: "
        "Command '['git', 'rebase', '0.5.0']' timed out after 120 seconds; "
        "rebase abort failed: fatal: no rebase in progress",
        "Failed to prepare environment: Git command timed out after 60s",
        "Failed to prepare environment: Unable to verify local-vs-remote divergence for "
        "branch '0.5.0': Command '['git', 'rev-list', '--count', 'origin/0.5.0..0.5.0']' "
        "timed out after 5 seconds. Refusing to select a remote worktree base; "
        "retry after Git is responsive.",
    ],
)
async def test_infrastructure_retry_reuses_task_and_never_replays_source(
    temp_db: HubDatabase, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, spawn_error: str
) -> None:
    run_id = _source(temp_db, "feedback")
    reporter = _reporter(temp_db, tmp_path)
    reporter.store.request("feedback", run_id, PERSONAL_PROJECT_ID)
    source_before = FeedbackReviewStore(temp_db).get_run(run_id)
    monkeypatch.setattr("gobby.reports.service.resolve_agent", Mock(return_value=object()))
    monkeypatch.setattr(
        "gobby.reports.service.get_or_create_launcher_session", Mock(return_value="launcher")
    )
    spawn = AsyncMock(return_value={"success": False, "error": spawn_error})
    monkeypatch.setattr("gobby.reports.service.spawn_agent_impl", spawn)
    assert await reporter.run_pending() == 1
    first = reporter.store.get("feedback", run_id)
    assert first["status"] == "pending" and first["auto_retries"] == 1
    assert await reporter.run_pending() == 1
    assert await reporter.run_pending() == 0
    failed = reporter.store.get("feedback", run_id)
    assert failed["status"] == "failed" and failed["task_id"] == first["task_id"]
    attempts = reporter.store.attempts("feedback", run_id)["attempts"]
    assert len(attempts) == 2 and all(item["error"] == spawn_error for item in attempts)
    assert spawn.await_count == 2
    assert spawn.await_args_list[0].kwargs["task_id"] == spawn.await_args_list[1].kwargs["task_id"]
    source_after = FeedbackReviewStore(temp_db).get_run(run_id)
    assert source_before is not None and source_after is not None
    assert (source_after.status, source_after.actions, source_after.completed_at) == (
        source_before.status,
        source_before.actions,
        source_before.completed_at,
    )


async def test_restart_reattaches_run_created_before_attempt_link(
    temp_db: HubDatabase, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    machine_id = "21000000-0000-4000-8000-000000000001"
    monkeypatch.setattr("gobby.utils.machine_id._cached_machine_id", machine_id)
    source_id = _source(temp_db, "feedback")
    store = ReportStore(temp_db)
    store.request("feedback", source_id, PERSONAL_PROJECT_ID)
    attempt_id = store.begin("feedback", source_id)
    assert attempt_id is not None
    report = store.prepare_task("feedback", source_id)
    worktree = LocalWorktreeManager(temp_db).create(
        project_id=PERSONAL_PROJECT_ID,
        branch_name=report["branch_name"],
        worktree_path=str(tmp_path),
        task_id=str(report["task_id"]),
    )
    session = SessionManager(temp_db).register(
        external_id="report-recovery",
        machine_id=machine_id,
        source="codex",
        project_id=PERSONAL_PROJECT_ID,
    )
    agent = LocalAgentRunManager(temp_db).create(
        parent_session_id=session.id,
        provider="codex",
        prompt="report",
        agent_name="synthesis-reporter",
        task_id=str(report["task_id"]),
        worktree_id=worktree.id,
    )
    store.recover()
    recovered = store.active_attempt("feedback", source_id)
    assert recovered is not None and recovered["id"] == attempt_id
    assert recovered["agent_run_id"] == agent.id
    reporter = _reporter(temp_db, tmp_path)
    monkeypatch.setattr(
        reporter.runner, "get_run", Mock(return_value=SimpleNamespace(status="success", error=None))
    )
    spawn = AsyncMock()
    monkeypatch.setattr("gobby.reports.service.spawn_agent_impl", spawn)
    await reporter.run_pending()
    spawn.assert_not_awaited()
    attempts = store.attempts("feedback", source_id)["attempts"]
    assert len(attempts) == 1 and attempts[0]["id"] == attempt_id
    assert attempts[0]["phase"] == "verification"
    assert "validated draft" in attempts[0]["error"]


def test_enqueue_outage_preserves_terminal_source_and_recovers_request(
    temp_db: HubDatabase, monkeypatch: pytest.MonkeyPatch
) -> None:
    feedback = FeedbackReviewStore(temp_db, report_project_id=PERSONAL_PROJECT_ID)
    run_id = feedback.create_run(
        dry_run=False, window_start=None, window_end=None, rows_considered=0
    )
    with monkeypatch.context() as scoped:
        scoped.setattr(ReportStore, "request", Mock(side_effect=OSError("queue unavailable")))
        feedback.finalize_run(run_id, status="completed", actions={"filed": [{"task_ref": "#42"}]})
    run = feedback.get_run(run_id)
    assert run is not None and run.status == "completed"
    assert run.actions is not None and run.actions["filed"] == [{"task_ref": "#42"}]
    store = ReportStore(temp_db)
    assert store.get("feedback", run_id)["status"] == "not_requested"
    assert store.pending() == [{"source_kind": "feedback", "source_run_id": run_id}]
    assert store.get("feedback", run_id)["status"] == "pending"


@pytest.mark.parametrize("kind", ["feedback", "dream"])
def test_orphaned_source_retains_report_request(temp_db: HubDatabase, kind: str) -> None:
    run_id = _source(temp_db, kind, "running")
    if kind == "feedback":
        assert (
            FeedbackReviewStore(
                temp_db, report_project_id=PERSONAL_PROJECT_ID
            ).mark_running_interrupted()
            == 1
        )
    else:
        assert reconcile_interrupted_dream_runs(
            cast(MemoryDreamManagerProtocol, SimpleNamespace(db=temp_db)),
            report_project_id=PERSONAL_PROJECT_ID,
        ) == [run_id]
    store = ReportStore(temp_db)
    assert store.pending() == [{"source_kind": kind, "source_run_id": run_id}]
    assert store.get(kind, run_id)["status"] == "pending"


async def test_report_waits_for_resumable_dream_to_finish(
    temp_db: HubDatabase, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_id = _source(temp_db, "dream", "interrupted")
    dream = MemoryDreamStore(temp_db)
    dream.update_run(run_id, options={"_restart_resume_pending": True})
    reporter = _reporter(temp_db, tmp_path)
    reporter.store.request("dream", run_id, PERSONAL_PROJECT_ID)
    spawn = AsyncMock(side_effect=ValueError("explicit launch failure"))
    monkeypatch.setattr(reporter, "_spawn", spawn)
    await reporter.publish("dream", run_id)
    dream.update_run(run_id, status="running", options={})
    await reporter.publish("dream", run_id)
    assert reporter.store.attempts("dream", run_id)["attempts"] == []
    spawn.assert_not_called()
    dream.update_run(run_id, status="completed")
    await reporter.publish("dream", run_id)
    spawn.assert_awaited_once()
    assert reporter.store.get("dream", run_id)["status"] == "failed"


@pytest.mark.parametrize("started", [False, True])
@pytest.mark.parametrize("restart", [False, True])
@pytest.mark.parametrize(
    "agent_error",
    ["socket unavailable", "Git command timed out after 60s", "Agent exceeded 900.0s timeout"],
)
async def test_background_transport_failure_retries_only_before_agent_started(
    temp_db: HubDatabase,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    started: bool,
    restart: bool,
    agent_error: str,
) -> None:
    machine_id = "21000000-0000-4000-8000-000000000001"
    monkeypatch.setattr("gobby.utils.machine_id._cached_machine_id", machine_id)
    run_id = _source(temp_db, "feedback")
    reporter = _reporter(temp_db, tmp_path)
    store = reporter.store
    store.request("feedback", run_id, PERSONAL_PROJECT_ID)
    attempt = store.begin("feedback", run_id)
    assert attempt is not None
    report = store.prepare_task("feedback", run_id)
    session = SessionManager(temp_db).register(
        external_id="background-launch",
        machine_id=machine_id,
        source="codex",
        project_id=PERSONAL_PROJECT_ID,
    )
    agents = LocalAgentRunManager(temp_db)
    agent = agents.create(
        parent_session_id=session.id,
        provider="codex",
        prompt="report",
        agent_name="synthesis-reporter",
        task_id=str(report["task_id"]),
    )
    if started:
        agents.start(agent.id)
    agents.fail(agent.id, agent_error)
    store.attach_agent(attempt, agent.id, None)
    monkeypatch.setattr(reporter.runner, "get_run", agents.get)
    spawn = AsyncMock()
    monkeypatch.setattr(reporter, "_spawn", spawn)
    if restart:
        store.recover()
    else:
        await reporter.publish("feedback", run_id)
    result = store.get("feedback", run_id)
    failure_status = "interrupted" if restart else "failed"
    can_retry = not started and agent_error != "Agent exceeded 900.0s timeout"
    assert result["status"] == ("pending" if can_retry else failure_status)
    assert result["auto_retries"] == int(can_retry)
    history = store.attempts("feedback", run_id)["attempts"]
    assert history[0]["agent_run_id"] == agent.id
    assert history[0]["error"] == agent_error
    spawn.assert_not_called()
