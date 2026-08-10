"""Storage and configuration setup for GobbyRunner."""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
from concurrent.futures import CancelledError, Future
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast
from uuid import UUID

from gobby.config.app import DaemonConfig, load_config
from gobby.config.bootstrap import load_bootstrap
from gobby.paths import get_gobby_home
from gobby.runner_init.helpers import (
    _ensure_headless_settings,
    ensure_machine_identity,
    init_hub_database,
)
from gobby.shutdown_intent import ShutdownIntent
from gobby.storage.auth import ensure_local_api_token
from gobby.storage.concurrency import CoverageExecutor, resolve_database_concurrency
from gobby.storage.concurrency_watchdog import DatabaseSaturationWatchdog
from gobby.storage.executor import DatabaseExecutor
from gobby.storage.session_tasks import SessionTaskManager
from gobby.storage.sessions import SessionManager
from gobby.storage.sessions._constants import ensure_system_session
from gobby.storage.tasks import LocalTaskManager
from gobby.telemetry import init_telemetry
from gobby.telemetry.logging import setup_file_logging
from gobby.utils.machine_id import get_machine_id
from gobby.worktrees.executor import WorktreeDeleteExecutor

if TYPE_CHECKING:
    from gobby.runner import GobbyRunner

logger = logging.getLogger(__name__)


def _warn_missing_terminal_dependency(config: DaemonConfig) -> None:
    if not config.tmux.enabled:
        return

    from gobby.agents.tmux.wsl_compat import needs_wsl

    if needs_wsl():
        if not shutil.which("wsl"):
            logger.warning(
                "WSL is not installed. Agent spawning in terminal mode will not work. "
                "Install: wsl --install"
            )
    elif not shutil.which("tmux"):
        logger.warning(
            "tmux is not installed. Agent spawning in terminal mode will not work. "
            "Install: brew install tmux (macOS), apt install tmux (Linux)"
        )


def init_runtime_capacity(runner: GobbyRunner) -> None:
    """Size runtime capacity from the initial active configuration epoch."""
    from gobby.storage.hub.postgres import PostgresHubDatabase

    postgres_database = cast(PostgresHubDatabase, runner.database)
    database_capacity = postgres_database.server_capacity()
    runner.database_concurrency = resolve_database_concurrency(
        runner.config.database_concurrency,
        database_capacity,
        cpu_count=os.process_cpu_count() or 1,
    )
    postgres_database.resize_pool(runner.database_concurrency.pool_max_size)
    runner.db_executor = DatabaseExecutor(
        max_workers=runner.database_concurrency.executor_max_workers
    )
    runner.worktree_delete_executor = WorktreeDeleteExecutor(max_workers=4)
    runner.coverage_executor = CoverageExecutor(
        max_concurrency=runner.database_concurrency.coverage_max_concurrency
    )
    if runner.database_concurrency.hardware_warning is not None:
        logger.warning(runner.database_concurrency.hardware_warning)
    logger.info("Database concurrency: %s", runner.database_concurrency.as_dict())

    init_telemetry(runner.config.telemetry, runner.config.logging, verbose=runner.verbose)
    runner.database_watchdog = DatabaseSaturationWatchdog(
        postgres_database,
        runner.db_executor,
        runner.coverage_executor,
        runner.database_concurrency,
    )
    runner.database_watchdog.start()


def init_storage_and_config(runner: GobbyRunner, config_path: Path | None, verbose: bool) -> None:
    """Initialize config, telemetry, database, secrets, and core managers."""
    if config_path is not None and not config_path.exists():
        raise FileNotFoundError(
            f"Config file not found: {config_path}. "
            f"Use 'gobby install' to create bootstrap.yaml, "
            f"or omit --config to use the default path (~/.gobby/bootstrap.yaml)."
        )
    runner._config_file = str(config_path) if config_path else None
    runner.bootstrap_config = load_bootstrap(
        runner._config_file,
        resolve_database_url=True,
    )
    runner.config = load_config(runner._config_file, resolve_database_url=True)
    runner.verbose = verbose

    setup_file_logging(runner.config.logging, verbose=verbose)

    runner.machine_id = get_machine_id()

    _ensure_headless_settings()

    runner._shutdown_requested = False
    runner._shutdown_intent = ShutdownIntent.STOP
    runner._metrics_cleanup_task = None
    runner._tool_results_cleanup_task = None
    runner._workflow_audit_cleanup_task = None
    runner._vector_rebuild_task = None
    runner._zombie_messages_task = None
    runner._span_cleanup_task = None
    runner._unmodeled_observations_cleanup_task = None
    runner._metrics_archive_task = None
    runner._model_metadata_refresh_task = None
    runner._provider_capability_refresh_task = None
    runner._metric_snapshot_task = None
    runner._resource_monitor_task = None
    runner._hook_inbox_task = None
    runner._bin_freshness_task = None
    runner._expired_isolation_task = None
    runner._tmux_window_repair_task = None
    runner._pending_tasks = set()

    runner.database = init_hub_database(runner.config)
    if runner.machine_id is None:
        raise RuntimeError("local machine identity is unavailable")
    runner.machine_id = ensure_machine_identity(runner.database, runner.machine_id)
    ensure_system_session(runner.database)
    from gobby.storage.managed_credentials import ManagedCredentialManager

    runner.managed_credential_manager = ManagedCredentialManager(
        database=runner.database,
        machine_id=UUID(runner.machine_id),
        runtime_root=get_gobby_home() / "runtime" / "managed-executions",
    )
    setattr(  # noqa: B010 - runtime attachment avoids a hub protocol dependency cycle
        runner.database,
        "managed_credential_manager",
        runner.managed_credential_manager,
    )
    from gobby.storage.config_store import ConfigStore
    from gobby.storage.hub.postgres import PostgresHubDatabase
    from gobby.storage.secrets import SecretStore

    runner.secret_store = SecretStore(runner.database)
    runner.config_store = ConfigStore(runner.database, secret_store=runner.secret_store)
    runner.secret_store.ensure_ready()
    runner.config_store.initialize()
    from gobby.providers.capabilities.metadata_aliases import seed_model_metadata_aliases

    seed_model_metadata_aliases(runner.config_store)
    ensure_local_api_token(runner.config_store)
    runner.config = load_config(
        config_file=runner._config_file,
        secret_resolver=runner.secret_store.get,
        config_store=runner.config_store,
        resolve_database_url=True,
    )
    _warn_missing_terminal_dependency(runner.config)
    postgres_database = cast(PostgresHubDatabase, runner.database)
    from gobby.config.runtime import ConfigRuntime
    from gobby.storage.config_notifications import ConfigNotificationListener
    from gobby.storage.config_repository import ConfigRepository

    config_repository = ConfigRepository(
        runner.database,
        secret_store=runner.secret_store,
    )
    runner.config_runtime = ConfigRuntime(
        config_repository,
        notification_source=ConfigNotificationListener(
            postgres_database.open_runtime_async_connection,
        ),
    )
    from gobby.storage.model_metadata import ModelMetadataStore

    try:
        metadata_store = ModelMetadataStore(runner.database)
        metadata_store.populate()
    except Exception as e:
        logger.warning("Failed to populate model metadata: %s", e, exc_info=True)

    runner.session_manager = SessionManager(runner.database)
    runner.task_manager = LocalTaskManager(runner.database)
    runner.session_task_manager = SessionTaskManager(runner.database)

    from gobby.storage.spans import SpanStorage

    runner.span_storage = SpanStorage(runner.database)

    if runner.config.telemetry and runner.config.telemetry.traces_enabled:
        from gobby.telemetry.providers import add_span_storage_exporter

        broadcast_loop = asyncio.get_running_loop()

        def _broadcast_proxy(span: dict[str, Any]) -> None:
            """Proxy for trace event broadcasting via WebSocket."""
            if hasattr(runner, "websocket_server") and runner.websocket_server:
                broadcast = runner.websocket_server.broadcast_trace_event(span)
                try:
                    future = asyncio.run_coroutine_threadsafe(broadcast, broadcast_loop)
                except RuntimeError as e:
                    broadcast.close()
                    logger.debug("Trace broadcast skipped (daemon loop unavailable): %s", e)
                    return

                def _log_broadcast_result(done_future: Future[None]) -> None:
                    try:
                        done_future.result()
                    except CancelledError:
                        logger.debug("Trace broadcast task cancelled")
                    except Exception:
                        logger.exception("Trace broadcast task failed")

                future.add_done_callback(_log_broadcast_result)

        add_span_storage_exporter(runner.span_storage, broadcast_callback=_broadcast_proxy)
        logger.debug("Local span storage exporter wired to OTel")

    from gobby.utils.dev import is_dev_mode

    runner._dev_mode = is_dev_mode(Path.cwd())

    if runner._dev_mode:
        from gobby.cli.installers.shared import sync_bundled_content_to_db

        sync_result = sync_bundled_content_to_db(runner.database)
        total = sync_result["total_synced"]
        if total > 0:
            logger.info("Dev mode: synced %s bundled items on startup", total)

    from gobby.storage.tasks._stage_registry_loader import StageRegistryLoader

    stage_sync = StageRegistryLoader().sync(runner.database)
    if stage_sync.upserted > 0:
        logger.info("Synced %s bundled stage registry rows", stage_sync.upserted)

    from gobby.storage.build_profiles import BuildProfileLoader

    profile_sync = BuildProfileLoader().sync(runner.database)
    if profile_sync.upserted > 0:
        logger.info("Synced %s bundled build profile rows", profile_sync.upserted)

    from gobby.storage.prompts import LocalPromptManager

    runner.prompt_manager = LocalPromptManager(runner.database, dev_mode=runner._dev_mode)

    from gobby.storage.skills import LocalSkillManager

    runner.skill_manager = LocalSkillManager(runner.database)

    runner.hub_manager = None
    try:
        from gobby.config.skills import SkillsConfig
        from gobby.skills.hubs import (
            ClaudePluginsProvider,
            ClawdHubProvider,
            GitHubCollectionProvider,
            GitHubTopicProvider,
            HubManager,
            SkillsMPProvider,
        )
        from gobby.skills.hubs.manager import resolve_hub_api_keys

        skills_config = runner.config.skills if hasattr(runner.config, "skills") else SkillsConfig()
        api_keys = resolve_hub_api_keys(skills_config.hubs, runner.secret_store)

        runner.hub_manager = HubManager(configs=skills_config.hubs, api_keys=api_keys)
        runner.hub_manager.register_provider_factory("clawdhub", ClawdHubProvider)
        runner.hub_manager.register_provider_factory("skillsmp", SkillsMPProvider)
        runner.hub_manager.register_provider_factory("github-collection", GitHubCollectionProvider)
        runner.hub_manager.register_provider_factory("github-topic", GitHubTopicProvider)
        runner.hub_manager.register_provider_factory("claude-plugins", ClaudePluginsProvider)
        runner.hub_manager._skill_description_config = (
            runner.config.skill_description if hasattr(runner.config, "skill_description") else None
        )
        logger.debug("HubManager initialized with %s hubs", len(skills_config.hubs))
    except Exception as e:
        logger.warning("Failed to initialize HubManager: %s", e, exc_info=True)
