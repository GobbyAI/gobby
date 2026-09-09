"""Daemon-owned reporter launch and publication recovery."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import TYPE_CHECKING, Any

from gobby.agents.launcher_session import get_or_create_launcher_session
from gobby.events.completion_registry import CompletionResultEvictedError
from gobby.mcp_proxy.tools.spawn_agent._implementation import spawn_agent_impl
from gobby.reports.publication import verify_publication
from gobby.reports.storage import ReportStore, transient_publication_error
from gobby.workflows.agent_resolver import resolve_agent

if TYPE_CHECKING:
    from gobby.agents.runner import AgentRunner
    from gobby.config.app import DaemonConfig
    from gobby.events.completion_registry import CompletionEventRegistry
    from gobby.storage.hub.protocol import HubDatabase
    from gobby.storage.sessions import SessionManager
    from gobby.storage.tasks import LocalTaskManager
    from gobby.storage.worktrees import LocalWorktreeManager
    from gobby.worktrees.git import WorktreeGitManager


class SynthesisReporter:
    def __init__(
        self,
        *,
        db: HubDatabase,
        runner: AgentRunner,
        session_manager: SessionManager,
        completion_registry: CompletionEventRegistry,
        task_manager: LocalTaskManager,
        worktree_storage: LocalWorktreeManager,
        git_manager: WorktreeGitManager,
        daemon_config: DaemonConfig,
        project_id: str,
    ) -> None:
        self.store = ReportStore(db)
        self.runner = runner
        self.session_manager = session_manager
        self.completion_registry = completion_registry
        self.task_manager = task_manager
        self.worktree_storage = worktree_storage
        self.git_manager = git_manager
        self.daemon_config = daemon_config
        self.project_id = project_id
        self._lock = asyncio.Lock()

    async def run_pending(self) -> int:
        async with self._lock:
            jobs = await asyncio.to_thread(self.store.pending)
            for job in jobs:
                await self.publish(str(job["source_kind"]), str(job["source_run_id"]))
            return len(jobs)

    async def publish(self, kind: str, run_id: str) -> None:
        if not await asyncio.to_thread(self.store.source_ready, kind, run_id):
            return
        active = await asyncio.to_thread(self.store.active_attempt, kind, run_id)
        attempt_id: str | None
        if active:
            attempt_id = str(active["id"])
        else:
            attempt_id = await asyncio.to_thread(self.store.begin, kind, run_id)
        if attempt_id is None:
            return
        agent_run_id = (
            str(active["agent_run_id"]) if active and active.get("agent_run_id") else None
        )
        try:
            report = await asyncio.to_thread(self.store.prepare_task, kind, run_id)
            task = await asyncio.to_thread(self.task_manager.get_task, str(report["task_id"]))
            if task.closed_at is None:
                if agent_run_id is None:
                    agent_run_id = await self._spawn(report, attempt_id)
                # Re-register a lost notification after restart, then check the
                # durable state to close the registration/completion race.
                self.completion_registry.register(agent_run_id, subscribers=[])
                agent = await asyncio.to_thread(self.runner.get_run, agent_run_id)
                if agent is not None and agent.status in {"pending", "running"}:
                    try:
                        await self.completion_registry.wait(agent_run_id, timeout=930)
                    except (KeyError, CompletionResultEvictedError, TimeoutError):
                        pass
                    agent = await asyncio.to_thread(self.runner.get_run, agent_run_id)
                    if agent is not None and agent.status in {"pending", "running"}:
                        return  # The lifecycle monitor owns this live writer's timeout.
                if agent is None or agent.status != "success":
                    if (
                        agent is not None
                        and agent.started_at is None
                        and agent.error
                        and transient_publication_error(agent.error)
                    ):
                        await asyncio.to_thread(self.store.phase, attempt_id, "launch")
                        raise OSError(agent.error)
                    raise RuntimeError(
                        agent.error
                        if agent is not None and agent.error
                        else "Reporter did not complete successfully"
                    )
            report = await asyncio.to_thread(self.store.get, kind, run_id)
            await asyncio.to_thread(self.store.phase, attempt_id, "verification")
            await asyncio.to_thread(
                verify_publication, self.store, report, Path(self.git_manager.repo_path), attempt_id
            )
        except asyncio.CancelledError:
            # The child may still be running. Preserve its attempt and reattach
            # on restart instead of launching a second writer in its worktree.
            await asyncio.to_thread(self.store.interrupted_coordinator, attempt_id)
            raise
        except Exception as exc:
            await asyncio.to_thread(
                self.store.fail,
                attempt_id,
                str(exc),
                transient=isinstance(exc, OSError),
                agent_run_id=agent_run_id,
                error_type=type(exc).__name__,
            )

    async def _spawn(self, report: dict[str, Any], attempt_id: str) -> str:
        body = await asyncio.to_thread(
            resolve_agent, "synthesis-reporter", self.store.db, project_id=self.project_id
        )
        if body is None:
            raise ValueError("Installed synthesis-reporter definition is unavailable")
        parent = await asyncio.to_thread(
            get_or_create_launcher_session,
            self.session_manager,
            self.project_id,
            "synthesis-report",
        )
        await asyncio.to_thread(self.store.phase, attempt_id, "launch")
        result = await spawn_agent_impl(
            f"Publish the {report['source_kind']} synthesis report for source run {report['source_run_id']}. Read gobby-reports:get_report first; resume its persisted draft if present.",
            self.runner,
            agent_body=body,
            agent_lookup_name="synthesis-reporter",
            task_id=str(report["task_id"]),
            task_manager=self.task_manager,
            isolation="worktree",
            branch_name=str(report["branch_name"]),
            worktree_id=str(report["worktree_id"]) if report.get("worktree_id") else None,
            worktree_storage=self.worktree_storage,
            git_manager=self.git_manager,
            timeout=900,
            parent_session_id=parent,
            caller_session_id=parent,
            project_path=str(self.git_manager.repo_path),
            target_project_id=self.project_id,
            session_manager=self.session_manager,
            db=self.store.db,
            completion_registry=self.completion_registry,
            notify_parent_on_completion=True,
            daemon_config=self.daemon_config,
            cleanup_isolation_on_failure=False,
        )
        agent_run_id = str(result["run_id"]) if result.get("run_id") else None
        worktree_id = str(result["worktree_id"]) if result.get("worktree_id") else None
        if agent_run_id is not None or worktree_id is not None:
            await asyncio.to_thread(self.store.attach_agent, attempt_id, agent_run_id, worktree_id)
        if not result.get("success") or agent_run_id is None or worktree_id is None:
            error = str(
                result.get("error") or "Reporter spawn returned incomplete isolation evidence"
            )
            if transient_publication_error(error):
                raise OSError(error)
            raise ValueError(error)
        await asyncio.to_thread(
            self.store.phase, attempt_id, "publication" if report.get("content") else "synthesis"
        )
        return agent_run_id
