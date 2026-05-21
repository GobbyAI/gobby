"""Gobby Daemon Runner.

GobbyRunner is the main entry point for the daemon. Initialization and
lifecycle logic are extracted into runner_init.py and runner_lifecycle.py
to keep this module focused on the public API.

Related modules:
- runner_init.py — component wiring, dependency injection, service setup
- runner_lifecycle.py — event loop, startup sequence, shutdown sequence
- runner_broadcasting.py — WebSocket event broadcasting
- runner_maintenance.py — background maintenance loops, signal handling
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any

from gobby.shutdown_intent import ShutdownIntent

if TYPE_CHECKING:
    from gobby.agents.lifecycle_monitor import AgentLifecycleMonitor
    from gobby.agents.runner import AgentRunner
    from gobby.config.app import DaemonConfig
    from gobby.events.completion_registry import CompletionEventRegistry
    from gobby.events.wake import WakeDispatcher
    from gobby.llm import LLMService
    from gobby.mcp_proxy.manager import MCPClientManager
    from gobby.mcp_proxy.metrics import ToolMetricsManager
    from gobby.mcp_proxy.metrics_events import MetricsEventStore
    from gobby.memory.manager import MemoryManager
    from gobby.memory.vectorstore import VectorStore
    from gobby.scheduler.scheduler import CronScheduler
    from gobby.servers.http import HTTPServer
    from gobby.servers.websocket.server import WebSocketServer
    from gobby.sessions.lifecycle import SessionLifecycleManager
    from gobby.sessions.processor import SessionMessageProcessor
    from gobby.storage.clones import LocalCloneManager
    from gobby.storage.config_store import ConfigStore
    from gobby.storage.cron import CronJobStorage
    from gobby.storage.cron_models import CronJob
    from gobby.storage.executor import DatabaseExecutor
    from gobby.storage.hub.protocol import HubDatabase
    from gobby.storage.mcp import LocalMCPManager
    from gobby.storage.pipelines import LocalPipelineExecutionManager
    from gobby.storage.prompts import LocalPromptManager
    from gobby.storage.secrets import SecretStore
    from gobby.storage.session_tasks import SessionTaskManager
    from gobby.storage.sessions import SessionManager
    from gobby.storage.skills import LocalSkillManager
    from gobby.storage.spans import SpanStorage
    from gobby.storage.tasks import LocalTaskManager
    from gobby.storage.worktrees import LocalWorktreeManager
    from gobby.sync.memories import MemorySyncManager
    from gobby.sync.tasks import TaskSyncManager
    from gobby.tasks.validation import TaskValidator
    from gobby.workflows.loader import WorkflowLoader
    from gobby.workflows.pipeline_executor import PipelineExecutor
    from gobby.worktrees.git import WorktreeGitManager

os.environ["TOKENIZERS_PARALLELISM"] = "false"

# Strip Claude Code session marker so SDK subprocess calls don't fail with
# "cannot be launched inside another Claude Code session" when the daemon
# was started/restarted from within a Claude Code session.
os.environ.pop("CLAUDECODE", None)

# Silence noisy third-party HTTP loggers (httpx logs every Neo4j query at INFO)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)

logger = logging.getLogger(__name__)

DISPATCHER_CRON_JOB_NAME = "gobby:dispatcher"
DISPATCHER_CRON_DESCRIPTION = "System dispatcher heartbeat for task lifecycle automation"
DISPATCHER_CRON_HANDLER = "dispatch.tick"
DISPATCHER_CRON_INTERVAL_SECONDS = 60
DISPATCHER_CRON_ACTION_CONFIG: dict[str, Any] = {"handler": DISPATCHER_CRON_HANDLER}


def _ensure_dispatcher_project_row(db: HubDatabase, project_id: str) -> None:
    db.execute(
        """
        INSERT INTO projects (id, name, created_at, updated_at)
        VALUES (?, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
        ON CONFLICT (id) DO NOTHING
        """,
        (project_id, f"project:{project_id}"),
    )


def install_dispatcher_cron_row(db: HubDatabase, *, project_id: str) -> CronJob:
    """Install or reconcile the bundled dispatcher cron row."""
    from gobby.storage.cron import CronJobStorage

    _ensure_dispatcher_project_row(db, project_id)
    storage = CronJobStorage(db)
    existing = storage.get_job_by_name(DISPATCHER_CRON_JOB_NAME)
    if existing is None:
        return storage.create_job(
            project_id=project_id,
            name=DISPATCHER_CRON_JOB_NAME,
            description=DISPATCHER_CRON_DESCRIPTION,
            schedule_type="interval",
            interval_seconds=DISPATCHER_CRON_INTERVAL_SECONDS,
            action_type="handler",
            action_config=DISPATCHER_CRON_ACTION_CONFIG,
            enabled=True,
            is_system=True,
        )

    if not existing.is_system:
        storage.mark_as_system_job(existing.id)

    reconciled = storage.reconcile_system_job_definition(
        existing.id,
        description=DISPATCHER_CRON_DESCRIPTION,
        schedule_type="interval",
        cron_expr=None,
        interval_seconds=DISPATCHER_CRON_INTERVAL_SECONDS,
        run_at=None,
        timezone="UTC",
        action_type="handler",
        action_config=DISPATCHER_CRON_ACTION_CONFIG,
    )
    if reconciled is None:
        raise RuntimeError(f"Dispatcher cron row disappeared during install: {existing.id}")
    return reconciled


class GobbyRunner:
    """Runner for Gobby daemon.

    Attributes are set by the phase functions in runner_init.py.
    Declared here so mypy can see them.
    """

    # Phase 1: storage & config (init_storage_and_config)
    _config_file: str | None
    config: DaemonConfig
    verbose: bool
    machine_id: str | None
    _shutdown_requested: bool
    _shutdown_intent: ShutdownIntent
    _metrics_cleanup_task: asyncio.Task[None] | None
    _vector_rebuild_task: asyncio.Task[None] | None
    _zombie_messages_task: asyncio.Task[None] | None
    _comms_messages_task: asyncio.Task[None] | None
    _chat_attachments_cleanup_task: asyncio.Task[None] | None
    _span_cleanup_task: asyncio.Task[None] | None
    _metrics_archive_task: asyncio.Task[None] | None
    _metric_snapshot_task: asyncio.Task[None] | None
    _hook_inbox_task: asyncio.Task[None] | None
    _bin_freshness_task: asyncio.Task[None] | None
    _code_index_task: asyncio.Task[None] | None
    _code_index_shutdown: asyncio.Event | None
    _sync_worker_task: asyncio.Task[None] | None
    _sync_worker_shutdown: asyncio.Event | None
    _websocket_task: asyncio.Task[None] | None
    _subsystem_init_task: asyncio.Task[None] | None
    _provider_model_refresh_task: asyncio.Task[dict[str, dict[str, Any]]] | None
    _pending_tasks: set[asyncio.Task[Any]]

    _memory_reconcile_task: asyncio.Task[None] | None
    _approval_timeout_task: asyncio.Task[None] | None
    _expired_isolation_task: asyncio.Task[None] | None
    database: HubDatabase
    db_executor: DatabaseExecutor
    secret_store: SecretStore
    config_store: ConfigStore
    session_manager: SessionManager
    task_manager: LocalTaskManager
    session_task_manager: SessionTaskManager
    span_storage: SpanStorage
    _dev_mode: bool
    prompt_manager: LocalPromptManager
    skill_manager: LocalSkillManager
    hub_manager: Any | None

    # Phase 2: services (init_services)
    llm_service: LLMService | None
    vector_store: VectorStore | None
    memory_manager: MemoryManager | None
    code_indexer: Any | None
    mcp_db_manager: LocalMCPManager
    metrics_event_store: MetricsEventStore
    metrics_manager: ToolMetricsManager
    mcp_proxy: MCPClientManager
    task_sync_manager: TaskSyncManager
    memory_sync_manager: MemorySyncManager | None
    message_processor: SessionMessageProcessor | None
    task_validator: TaskValidator | None
    worktree_storage: LocalWorktreeManager
    clone_storage: LocalCloneManager
    git_manager: WorktreeGitManager | None
    project_id: str | None

    # Phase 3: orchestration (init_orchestration)
    wake_dispatcher: WakeDispatcher
    completion_registry: CompletionEventRegistry
    workflow_loader: WorkflowLoader | None
    pipeline_execution_manager: LocalPipelineExecutionManager | None
    pipeline_executor: PipelineExecutor | None
    agent_runner: AgentRunner | None
    agent_lifecycle_monitor: AgentLifecycleMonitor | None
    lifecycle_manager: SessionLifecycleManager
    cron_storage: CronJobStorage | None
    cron_scheduler: CronScheduler | None
    communications_manager: Any | None

    # Phase 4: servers (init_servers)
    http_server: HTTPServer
    websocket_server: WebSocketServer | None

    def __init__(self, config_path: Path | None = None, verbose: bool = False):
        from gobby.runner_init import (
            init_orchestration,
            init_servers,
            init_services,
            init_storage_and_config,
        )

        init_storage_and_config(self, config_path, verbose)
        init_services(self)
        init_orchestration(self)
        init_servers(self)

    async def run(self) -> None:
        from gobby.runner_lifecycle import run_daemon

        await run_daemon(self)

    def request_shutdown(self, intent: ShutdownIntent | None = None) -> None:
        """Request daemon shutdown and optionally set the semantic intent."""
        if intent is not None:
            self._shutdown_intent = intent
        self._shutdown_requested = True


async def run_gobby(config_path: Path | None = None, verbose: bool = False) -> None:
    runner = GobbyRunner(config_path=config_path, verbose=verbose)
    await runner.run()


def _healthy_daemon_running(port: int, host: str = "localhost") -> bool:
    """Quick check whether a healthy Gobby daemon is already listening."""
    import urllib.parse
    import urllib.request

    # Normalize wildcard addresses to localhost for health check
    if host in ("0.0.0.0", "::", ""):
        host = "localhost"
    elif ":" in host and not host.startswith("["):
        host = f"[{host}]"

    try:
        url = f"http://{host}:{port}/api/admin/health"
        parsed = urllib.parse.urlparse(url)
        if parsed.scheme not in ("http", "https"):
            return False
        req = urllib.request.Request(url, method="GET")
        with urllib.request.urlopen(req, timeout=2) as resp:  # nosec B310
            return bool(resp.status == 200)
    except Exception:
        return False


def _raise_fd_limit(target: int = 10240) -> None:
    """Raise the soft file-descriptor limit for the daemon process.

    macOS/Linux set low default soft limits which are far too low for a daemon
    managing WebSocket connections, MCP subprocess transports, database pools, and HTTP
    clients.  We raise the soft limit to *target* (or hard limit, whichever is
    smaller).  No-op on platforms without the resource module (e.g. Windows).
    """
    try:
        import resource
    except ImportError:
        logger.debug("resource module unavailable (Windows?) — skipping fd limit raise")
        return

    soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
    if soft >= target:
        return
    new_soft = min(target, hard) if hard != resource.RLIM_INFINITY else target
    try:
        resource.setrlimit(resource.RLIMIT_NOFILE, (new_soft, hard))
        logger.info(f"Raised fd limit: {soft} -> {new_soft} (hard={hard})")
    except (ValueError, OSError) as e:
        logger.warning(f"Could not raise fd limit from {soft}: {e}")


def main(config_path: Path | None = None, verbose: bool = False) -> None:
    _raise_fd_limit()

    # Fast guard: if a healthy daemon is already serving on our port, exit
    # cleanly so launchd (KeepAlive.SuccessfulExit=false) won't respawn us.
    from gobby.config.bootstrap import load_bootstrap

    bootstrap = load_bootstrap(str(config_path) if config_path else None)
    if _healthy_daemon_running(bootstrap.daemon_port, bootstrap.bind_host):
        print(
            f"Gobby daemon already healthy on port {bootstrap.daemon_port}, exiting.",
            file=sys.stderr,
        )
        sys.exit(0)

    try:
        asyncio.run(run_gobby(config_path=config_path, verbose=verbose))
    except KeyboardInterrupt:
        sys.exit(0)
    except Exception as e:
        logger.error(f"Fatal error: {e}", exc_info=True)
        sys.exit(1)


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Run Gobby daemon")
    parser.add_argument("--verbose", action="store_true", help="Enable verbose logging")
    parser.add_argument("--config", type=Path, help="Path to config file")

    args = parser.parse_args()
    main(config_path=args.config, verbose=args.verbose)
