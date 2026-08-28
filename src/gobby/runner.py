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
from typing import TYPE_CHECKING, Any, Self

from gobby.shutdown_intent import ShutdownIntent, write_shutdown_intent
from gobby.utils.git import disable_optional_git_locks

if TYPE_CHECKING:
    from gobby.adapters.codex_impl.client import CodexAppServerClient
    from gobby.agents.attention_metadata import AttentionMetadataStore
    from gobby.agents.detection.registry import DetectionManifestRegistry
    from gobby.agents.lifecycle_monitor import AgentLifecycleMonitor
    from gobby.agents.runner import AgentRunner
    from gobby.ai import TextGenerationService, ToolChatService
    from gobby.code_index.nightly_repair import CodeIndexNightlyRepairer
    from gobby.code_index.prune import CodeIndexPruner
    from gobby.config.app import DaemonConfig
    from gobby.config.bootstrap import BootstrapConfig
    from gobby.config.runtime import ConfigRuntime
    from gobby.config.terminal_host import TerminalHostConfig
    from gobby.config.terminals import TerminalConfig
    from gobby.daemon_lease import ActiveDaemonLease
    from gobby.events.completion_registry import CompletionEventRegistry
    from gobby.events.wake import WakeDispatcher
    from gobby.llm import LLMService
    from gobby.mcp_proxy.manager import MCPClientManager
    from gobby.mcp_proxy.metrics import ToolMetricsManager
    from gobby.mcp_proxy.metrics_events import MetricsEventStore
    from gobby.memory.dream.coordinator import MemoryDreamCoordinator
    from gobby.memory.manager import MemoryManager
    from gobby.memory.vectorstore import VectorStore
    from gobby.runner_pid_file import PidOwnershipResolution
    from gobby.scheduler.scheduler import CronScheduler
    from gobby.servers.http import HTTPServer
    from gobby.servers.websocket.server import WebSocketServer
    from gobby.sessions.lifecycle import SessionLifecycleManager
    from gobby.sessions.processor import SessionMessageProcessor
    from gobby.storage.attention import AttentionStateManager
    from gobby.storage.clones import LocalCloneManager
    from gobby.storage.concurrency import CoverageExecutor, DatabaseConcurrencyResolution
    from gobby.storage.concurrency_watchdog import DatabaseSaturationWatchdog
    from gobby.storage.cron import CronJobStorage
    from gobby.storage.definitions.notifications import DefinitionRevisionListener
    from gobby.storage.executor import DatabaseExecutor
    from gobby.storage.hub.protocol import HubDatabase
    from gobby.storage.managed_credentials import ManagedCredentialManager
    from gobby.storage.mcp import LocalMCPManager
    from gobby.storage.pipelines import LocalPipelineExecutionManager
    from gobby.storage.prompts import LocalPromptManager
    from gobby.storage.secrets import SecretStore
    from gobby.storage.session_tasks import SessionTaskManager
    from gobby.storage.sessions import SessionManager
    from gobby.storage.skills import LocalSkillManager
    from gobby.storage.spans import SpanStorage
    from gobby.storage.tasks import LocalTaskManager
    from gobby.storage.terminals import TerminalManager
    from gobby.storage.worktrees import LocalWorktreeManager
    from gobby.sync.memories import MemoryBackupManager
    from gobby.tasks.validation import TaskValidator
    from gobby.terminals import TerminalRuntimeRegistry
    from gobby.terminals.host_manager import TerminalHostManager
    from gobby.terminals.services import TerminalServices
    from gobby.wiki.watcher import WikiWatcher
    from gobby.workflows.pipeline_executor import PipelineExecutor
    from gobby.workflows.pipeline_loader import PipelineLoader
    from gobby.worktrees.executor import WorktreeDeleteExecutor
    from gobby.worktrees.git import WorktreeGitManager

os.environ["TOKENIZERS_PARALLELISM"] = "false"

# Daemon git reads must never take the optional index lock: a `git status`
# killed on timeout mid index-refresh leaves `.git/index.lock` behind and
# blocks every commit in the shared checkout (#21055).
disable_optional_git_locks()

# Strip Claude Code session marker so SDK subprocess calls don't fail with
# "cannot be launched inside another Claude Code session" when the daemon
# was started/restarted from within a Claude Code session.
os.environ.pop("CLAUDECODE", None)

# Silence noisy third-party HTTP loggers.
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)

logger = logging.getLogger(__name__)


class GobbyRunner:
    """Runner for Gobby daemon.

    Attributes are set by the phase functions in runner_init.py.
    Declared here so mypy can see them.
    """

    # Phase 1: storage & config (init_storage_and_config)
    _config_file: str | None
    startup_config: DaemonConfig
    bootstrap_config: BootstrapConfig
    verbose: bool
    machine_id: str | None
    _shutdown_requested: bool
    _shutdown_intent: ShutdownIntent
    _metrics_cleanup_task: asyncio.Task[None] | None
    _test_schema_sweep_task: asyncio.Task[None] | None
    _tool_results_cleanup_task: asyncio.Task[None] | None
    _workflow_audit_cleanup_task: asyncio.Task[None] | None
    _vector_rebuild_task: asyncio.Task[None] | None
    _zombie_messages_task: asyncio.Task[None] | None
    _comms_messages_task: asyncio.Task[None] | None
    _skill_purge_task: asyncio.Task[None] | None
    _chat_attachments_cleanup_task: asyncio.Task[None] | None
    _span_cleanup_task: asyncio.Task[None] | None
    _unmodeled_observations_cleanup_task: asyncio.Task[None] | None
    _loop_progress_cleanup_task: asyncio.Task[None] | None
    _metrics_archive_task: asyncio.Task[None] | None
    _metric_snapshot_task: asyncio.Task[None] | None
    _resource_monitor_task: asyncio.Task[None] | None
    _hook_inbox_task: asyncio.Task[None] | None
    _bin_freshness_task: asyncio.Task[None] | None
    _code_index_task: asyncio.Task[None] | None
    _code_index_shutdown: asyncio.Event | None
    _sync_worker_task: asyncio.Task[None] | None
    _sync_worker_shutdown: asyncio.Event | None
    _external_issue_sync_task: asyncio.Task[None] | None
    _external_issue_sync_shutdown: asyncio.Event | None
    external_issue_sync_coordinator: Any | None
    _websocket_task: asyncio.Task[None] | None
    _subsystem_init_task: asyncio.Task[None] | None
    _provider_capability_refresh_task: asyncio.Task[None] | None
    _generation_endpoint_health_task: asyncio.Task[None] | None
    _model_metadata_refresh_task: asyncio.Task[None] | None
    _pending_tasks: set[asyncio.Task[Any]]
    degraded_services: set[str]
    daemon_lease: ActiveDaemonLease

    _memory_reconcile_task: asyncio.Task[None] | None
    _recall_drift_task: asyncio.Task[None] | None
    _approval_timeout_task: asyncio.Task[None] | None
    _expired_isolation_task: asyncio.Task[None] | None
    _tmux_window_repair_task: asyncio.Task[None] | None
    _wiki_watcher_task: asyncio.Task[None] | None
    _wiki_watcher: WikiWatcher | None
    database: HubDatabase
    managed_credential_manager: ManagedCredentialManager
    db_executor: DatabaseExecutor
    worktree_delete_executor: WorktreeDeleteExecutor
    coverage_executor: CoverageExecutor
    database_concurrency: DatabaseConcurrencyResolution
    database_watchdog: DatabaseSaturationWatchdog
    secret_store: SecretStore
    config_runtime: ConfigRuntime
    definition_revision_listener: DefinitionRevisionListener
    session_manager: SessionManager
    task_manager: LocalTaskManager
    session_task_manager: SessionTaskManager
    span_storage: SpanStorage
    _dev_mode: bool
    prompt_manager: LocalPromptManager
    skill_manager: LocalSkillManager
    hub_manager: Any | None

    # Phase 2: services (init_services)
    text_generation_service: TextGenerationService | None
    tool_chat_service: ToolChatService | None
    llm_service: LLMService | None
    vector_store: VectorStore | None
    project_write_fence: Any
    project_purge_service: Any | None
    embedding_switch_coordinator: Any | None
    memory_manager: MemoryManager | None
    memory_dream_coordinator: MemoryDreamCoordinator | None
    code_indexer: Any | None
    code_index_pruner: CodeIndexPruner | None
    code_index_nightly_repairer: CodeIndexNightlyRepairer | None
    mcp_db_manager: LocalMCPManager
    metrics_event_store: MetricsEventStore
    metrics_manager: ToolMetricsManager
    mcp_proxy: MCPClientManager
    memory_backup_manager: MemoryBackupManager | None
    message_processor: SessionMessageProcessor | None
    task_validator: TaskValidator | None
    worktree_storage: LocalWorktreeManager
    clone_storage: LocalCloneManager
    git_manager: WorktreeGitManager | None
    project_id: str | None

    # Phase 3: orchestration (init_orchestration)
    wake_dispatcher: WakeDispatcher
    completion_registry: CompletionEventRegistry
    workflow_loader: PipelineLoader | None
    pipeline_execution_manager: LocalPipelineExecutionManager | None
    pipeline_executor: PipelineExecutor | None
    agent_runner: AgentRunner | None
    agent_lifecycle_monitor: AgentLifecycleMonitor | None
    detection_registry: DetectionManifestRegistry
    terminal_manager: TerminalManager
    terminal_runtime_registry: TerminalRuntimeRegistry
    terminal_config: TerminalConfig
    terminal_host_config: TerminalHostConfig
    terminal_host_manager: TerminalHostManager | None
    frame_client: Any
    write_coordinator: Any
    terminal_services: TerminalServices
    terminal_effect_bridge: Any
    attention_manager: AttentionStateManager
    attention_metadata_store: AttentionMetadataStore
    lifecycle_manager: SessionLifecycleManager
    cron_storage: CronJobStorage | None
    cron_scheduler: CronScheduler | None
    system_automation_loop: Any | None
    communications_manager: Any | None

    # Phase 4: servers (init_servers)
    codex_client: CodexAppServerClient | None
    http_server: HTTPServer
    websocket_server: WebSocketServer | None

    def __init__(self, config_path: Path | None = None, verbose: bool = False):
        self._prepare_base_state()
        try:
            self._initialize_storage(config_path, verbose)
            self._initialize_post_database_services()
        except BaseException:
            from gobby.runner_rollback import rollback_runner_resources

            rollback_runner_resources(self)
            raise

    @classmethod
    async def create(cls, config_path: Path | None = None, verbose: bool = False) -> Self:
        """Build the production runner after ConfigRuntime reaches its initial epoch."""
        self = cls.__new__(cls)
        self._prepare_base_state()
        try:
            self._initialize_storage(config_path, verbose)
            startup_snapshot = await self.config_runtime.start()
            from gobby.runner_init.storage import bootstrap_overlaid_config

            self.startup_config = bootstrap_overlaid_config(
                startup_snapshot.active, self.bootstrap_config
            )
            await self._initialize_runtime_services()
        except BaseException:
            runtime = getattr(self, "config_runtime", None)
            if runtime is not None:
                await runtime.close()
            from gobby.runner_rollback import rollback_runner_resources_async

            await rollback_runner_resources_async(self)
            raise
        return self

    def _prepare_base_state(self) -> None:
        self.degraded_services = set()
        # Captured by run_daemon once the daemon's long-lived loop is running;
        # dispatch uses it to keep fire-and-forget work off short-lived loops.
        self.main_loop: asyncio.AbstractEventLoop | None = None

    def _initialize_storage(self, config_path: Path | None, verbose: bool) -> None:
        from gobby.runner_init import init_storage_and_config

        init_storage_and_config(self, config_path, verbose)

    def _initialize_post_database_services(self) -> None:
        from gobby.runner_init import (
            init_orchestration,
            init_runtime_capacity,
            init_servers,
            init_services,
        )

        init_runtime_capacity(self)
        init_services(self)
        init_orchestration(self, self.startup_config)
        init_servers(self)

    async def _initialize_runtime_services(self) -> None:
        from gobby.runner_init import (
            init_orchestration,
            init_runtime_capacity,
            init_servers,
        )
        from gobby.runner_init.services import init_stateful_services

        init_runtime_capacity(self)
        await init_stateful_services(self)
        init_orchestration(self, self.startup_config)
        init_servers(self)

    async def run(self, *, ownership_resolution: PidOwnershipResolution) -> None:
        from gobby.runner_lifecycle import run_daemon

        await run_daemon(self, ownership_resolution=ownership_resolution)

    def request_shutdown(self, intent: ShutdownIntent | None = None) -> None:
        """Request daemon shutdown and optionally set the semantic intent."""
        restart_already_requested = (
            self._shutdown_requested and self._shutdown_intent is ShutdownIntent.RESTART
        )
        if intent is not None and not (restart_already_requested and intent is ShutdownIntent.STOP):
            self._shutdown_intent = intent
        self._shutdown_requested = True


async def run_gobby(
    config_path: Path | None = None,
    verbose: bool = False,
    ownership_resolution: PidOwnershipResolution | None = None,
) -> None:
    from gobby.cli.utils import get_gobby_home
    from gobby.config.bootstrap import load_bootstrap
    from gobby.daemon_lease import ActiveDaemonLease
    from gobby.daemon_lease_control import (
        LeaseLoss,
        StandbyLeaseControl,
        monitor_active_lease,
        serve_standby_until_promotion,
    )
    from gobby.deployment import deployment_token
    from gobby.runner_pid_file import (
        SERVICE_LAUNCH_ENV,
        ProbeState,
        adopt_inherited_claim,
        claim_pid_file,
        convert_or_acquire_service_claim,
        probe_daemon_lock,
    )
    from gobby.storage.schema_contract import verify_schema
    from gobby.utils.local_token import read_local_api_token
    from gobby.utils.machine_id import require_machine_id

    if ownership_resolution is None:
        pid_file = get_gobby_home() / "gobby.pid"
        if os.environ.get(SERVICE_LAUNCH_ENV) == "1":
            ownership_resolution = convert_or_acquire_service_claim(pid_file)
        else:
            inherited = adopt_inherited_claim(pid_file)
            if inherited is not None:
                ownership_resolution = inherited
            else:
                ownership_resolution = claim_pid_file(pid_file)
        if ownership_resolution is None:
            owner = probe_daemon_lock(pid_file)
            if owner.state is ProbeState.DAEMON:
                logger.info(
                    "PID file %s is owned by another live daemon (PID %s); exiting cleanly",
                    pid_file,
                    owner.pid or "unknown",
                )
            else:
                logger.info(
                    "PID file %s is held (%s, PID %s); exiting before initialization",
                    pid_file,
                    owner.state.value,
                    owner.pid or "unknown",
                )
            return

    bootstrap = load_bootstrap(
        str(config_path) if config_path is not None else None,
        resolve_database_url=True,
    )
    database_url = bootstrap.database_url
    if database_url is None:
        raise RuntimeError("bootstrap database_url is required for active-daemon ownership")

    lease = ActiveDaemonLease(
        database_url,
        machine_id=require_machine_id(),
        deployment_token=deployment_token(),
    )
    runner: GobbyRunner | None = None
    try:
        await asyncio.to_thread(verify_schema, database_url)
        if not await asyncio.to_thread(lease.try_acquire):
            promotion_requested = asyncio.Event()
            control = StandbyLeaseControl(
                lease=lease,
                database_url=database_url,
                local_token=read_local_api_token(),
                promotion_requested=promotion_requested,
                schema_verifier=verify_schema,
            )
            promoted = await serve_standby_until_promotion(
                control,
                host=bootstrap.bind_host,
                port=bootstrap.daemon_port,
            )
            if not promoted:
                return

        runner = await GobbyRunner.create(config_path=config_path, verbose=verbose)
        active = runner
        active.daemon_lease = lease
        from gobby.runner_init.servers import _bind_runtime_grants
        from gobby.servers.lease_fence import drain_effect_fence

        _bind_runtime_grants(active.http_server, active)

        def _on_lease_invalidation() -> None:
            drain_effect_fence(getattr(active.http_server, "effect_fence", None))

        def _on_lease_loss(loss: LeaseLoss) -> None:
            write_shutdown_intent(
                loss.reason,
                ShutdownIntent.STOP,
                details=loss.shutdown_details(),
            )
            active.request_shutdown(ShutdownIntent.STOP)

        lease_monitor_stop = asyncio.Event()
        async with asyncio.TaskGroup() as tasks:
            tasks.create_task(
                monitor_active_lease(
                    lease,
                    stop=lease_monitor_stop,
                    on_loss=_on_lease_loss,
                    on_invalidation=_on_lease_invalidation,
                ),
                name="active-daemon-lease-monitor",
            )
            try:
                await active.run(ownership_resolution=ownership_resolution)
            finally:
                lease_monitor_stop.set()
    finally:
        if runner is not None:
            from gobby.servers.lease_fence import drain_effect_fence

            drain_effect_fence(getattr(runner.http_server, "effect_fence", None))
        lease.release()
        ownership_resolution.release()


def _healthy_daemon_running(port: int, host: str = "localhost") -> bool:
    """Quick check whether a healthy Gobby daemon is already listening."""
    import ipaddress
    import urllib.parse
    import urllib.request

    # Normalize wildcard addresses to localhost for health check
    # These wildcard strings are compared and normalized, never used as bind targets.
    wildcard_hosts = {str(ipaddress.IPv4Address(0)), str(ipaddress.IPv6Address(0)), ""}
    if host in wildcard_hosts:
        host = "localhost"
    elif ":" in host and not host.startswith("["):
        host = f"[{host}]"

    try:
        url = f"http://{host}:{port}/api/health"
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
        logger.info("Raised fd limit: %s -> %s (hard=%s)", soft, new_soft, hard)
    except (ValueError, OSError) as e:
        logger.warning("Could not raise fd limit from %s: %s", soft, e)


def _force_exit_after_expired_settlement() -> None:
    """Force process death when shutdown abandoned wedged settlement workers.

    A non-daemon executor worker wedged in a settlement transaction survives
    ``shutdown(cancel_futures=True)`` and blocks interpreter exit at the
    atexit thread join, so the standalone daemon would linger as a zombie
    process after pid release. Embedded hosts never call this — ``run_daemon``
    returns to them normally with workers leaked until gate severance.
    """
    from gobby.runner_lifecycle_shutdown import finalizer_expiry_backstop_required

    if not finalizer_expiry_backstop_required():
        return
    exc = sys.exception()
    if isinstance(exc, SystemExit):
        code = exc.code if isinstance(exc.code, int) else 0 if exc.code is None else 1
    elif exc is None or isinstance(exc, KeyboardInterrupt):
        code = 0
    else:
        code = 1
    logger.error(
        "Terminal-delivery settlement expired at shutdown; forcing process exit (code %s)",
        code,
    )
    logging.shutdown()
    os._exit(code)


def main(config_path: Path | None = None, verbose: bool = False) -> None:
    # Must precede any torch import (torch is lazy, voice-only): PyTorch's
    # default MPS high watermark is 1.7x Metal's recommended working set,
    # which lets unified-memory allocations balloon past physical RAM.
    os.environ.setdefault("PYTORCH_MPS_HIGH_WATERMARK_RATIO", "0.8")
    _raise_fd_limit()

    # Refuse a linked-worktree source tree before any subsystem touches the
    # database: startup sync would publish this checkout's templates to every
    # session (#21031).
    from gobby.utils.dev import worktree_daemon_refusal

    if refusal := worktree_daemon_refusal():
        print(refusal, file=sys.stderr)
        sys.exit(1)

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

    # Claim the singleton lock before any subsystem init: a launchd respawn
    # racing a live daemon must lose here and exit 0 so
    # KeepAlive.SuccessfulExit=false never hot-loops it.
    from gobby.cli.utils import get_gobby_home
    from gobby.runner_pid_file import (
        SERVICE_LAUNCH_ENV,
        ProbeState,
        SingletonError,
        adopt_inherited_claim,
        claim_pid_file,
        convert_or_acquire_service_claim,
        probe_daemon_lock,
    )

    pid_file = get_gobby_home() / "gobby.pid"
    ownership_resolution: PidOwnershipResolution | None = None
    try:
        if os.environ.get(SERVICE_LAUNCH_ENV) == "1":
            ownership_resolution = convert_or_acquire_service_claim(pid_file)
        else:
            inherited = adopt_inherited_claim(pid_file)
            ownership_resolution = inherited or claim_pid_file(pid_file)
    except SingletonError as exc:
        print(f"Gobby daemon lock {pid_file} failed closed: {exc}", file=sys.stderr)
        sys.exit(1)
    if ownership_resolution is None:
        owner = probe_daemon_lock(pid_file)
        if owner.state is ProbeState.DAEMON:
            print(
                f"Gobby daemon lock {pid_file} is held by PID {owner.pid or 'unknown'}, exiting.",
                file=sys.stderr,
            )
        else:
            print(
                f"Gobby daemon lock {pid_file} is held ({owner.state.value}, "
                f"PID {owner.pid or 'unknown'}), exiting.",
                file=sys.stderr,
            )
        sys.exit(0)

    try:
        assert ownership_resolution is not None
        asyncio.run(
            run_gobby(
                config_path=config_path,
                verbose=verbose,
                ownership_resolution=ownership_resolution,
            )
        )
    except KeyboardInterrupt:
        sys.exit(0)
    except Exception as e:
        logger.exception("Fatal error: %s", e)
        sys.exit(1)
    finally:
        # run_daemon releases the claim during shutdown; release() is
        # idempotent, so this only matters when asyncio.run never ran it.
        if ownership_resolution is not None:
            ownership_resolution.release()
        _force_exit_after_expired_settlement()


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Run Gobby daemon")
    parser.add_argument("--verbose", action="store_true", help="Enable verbose logging")
    parser.add_argument("--config", type=Path, help="Path to config file")

    args = parser.parse_args()
    main(config_path=args.config, verbose=args.verbose)
