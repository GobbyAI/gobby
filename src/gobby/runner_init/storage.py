"""Storage and configuration setup for GobbyRunner."""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
from pathlib import Path
from typing import TYPE_CHECKING, Any

from gobby.config.app import load_config
from gobby.runner_init.helpers import (
    _ensure_headless_settings,
    init_hub_database,
    resolve_embedding_api_key,
)
from gobby.shutdown_intent import ShutdownIntent
from gobby.storage.executor import DatabaseExecutor
from gobby.storage.session_tasks import SessionTaskManager
from gobby.storage.sessions import SessionManager
from gobby.storage.tasks import LocalTaskManager
from gobby.telemetry.logging import init_telemetry
from gobby.utils.machine_id import get_machine_id

if TYPE_CHECKING:
    from gobby.runner import GobbyRunner

logger = logging.getLogger(__name__)


def init_storage_and_config(runner: GobbyRunner, config_path: Path | None, verbose: bool) -> None:
    """Initialize config, telemetry, database, secrets, and core managers."""
    if config_path is not None and not config_path.exists():
        raise FileNotFoundError(
            f"Config file not found: {config_path}. "
            f"Use 'gobby install' to create bootstrap.yaml, "
            f"or omit --config to use the default path (~/.gobby/bootstrap.yaml)."
        )
    runner._config_file = str(config_path) if config_path else None
    runner.config = load_config(runner._config_file)
    runner.verbose = verbose

    init_telemetry(runner.config.telemetry, verbose=verbose)

    runner.machine_id = get_machine_id()

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
    _ensure_headless_settings()

    runner._shutdown_requested = False
    runner._shutdown_intent = ShutdownIntent.STOP
    runner._metrics_cleanup_task = None
    runner._vector_rebuild_task = None
    runner._zombie_messages_task = None
    runner._span_cleanup_task = None
    runner._metrics_archive_task = None
    runner._metric_snapshot_task = None
    runner._hook_inbox_task = None
    runner._bin_freshness_task = None
    runner._expired_isolation_task = None
    runner._pending_tasks = set()

    runner.database = init_hub_database(runner.config)
    try:
        db_max_workers = int(os.environ.get("RUNNER_MAX_WORKERS", "4"))
    except ValueError:
        db_max_workers = 4
    db_max_workers = db_max_workers if db_max_workers > 0 else 4
    runner.db_executor = DatabaseExecutor(max_workers=db_max_workers)

    from gobby.storage.config_store import ConfigStore
    from gobby.storage.secrets import SecretStore

    runner.secret_store = SecretStore(runner.database)
    runner.config_store = ConfigStore(runner.database)
    runner.config = load_config(
        config_file=runner._config_file,
        secret_resolver=runner.secret_store.get,
        config_store=runner.config_store,
    )

    if hasattr(runner.config, "embeddings") and not runner.config.embeddings.api_key:
        resolved_key = resolve_embedding_api_key(
            runner.secret_store, runner.config.embeddings.model
        )
        if resolved_key:
            runner.config.embeddings.api_key = resolved_key

    from gobby.storage.model_costs import ModelCostStore

    try:
        cost_store = ModelCostStore(runner.database)
        cost_store.populate()
    except Exception as e:
        logger.warning(f"Failed to populate model metadata: {e}")

    runner.session_manager = SessionManager(runner.database)
    runner.task_manager = LocalTaskManager(runner.database)
    runner.session_task_manager = SessionTaskManager(runner.database)

    from gobby.storage.spans import SpanStorage

    runner.span_storage = SpanStorage(runner.database)

    if runner.config.telemetry and runner.config.telemetry.traces_enabled:
        from gobby.telemetry.providers import add_span_storage_exporter

        def _broadcast_proxy(span: dict[str, Any]) -> None:
            """Proxy for trace event broadcasting via WebSocket."""
            if hasattr(runner, "websocket_server") and runner.websocket_server:
                try:
                    loop = asyncio.get_running_loop()
                    task = loop.create_task(runner.websocket_server.broadcast_trace_event(span))
                    runner._pending_tasks.add(task)
                    task.add_done_callback(runner._pending_tasks.discard)
                except RuntimeError as e:
                    logger.debug(f"Trace broadcast skipped (no running loop): {e}")

        add_span_storage_exporter(runner.span_storage, broadcast_callback=_broadcast_proxy)
        logger.debug("Local span storage exporter wired to OTel")

    from gobby.utils.dev import is_dev_mode

    runner._dev_mode = is_dev_mode(Path.cwd())

    if runner._dev_mode:
        from gobby.cli.installers.shared import sync_bundled_content_to_db

        sync_result = sync_bundled_content_to_db(runner.database)
        total = sync_result["total_synced"]
        if total > 0:
            logger.info(f"Dev mode: synced {total} bundled items on startup")

    from gobby.storage.tasks._stage_registry_loader import StageRegistryLoader

    stage_sync = StageRegistryLoader().sync(runner.database)
    if stage_sync.upserted > 0:
        logger.info(f"Synced {stage_sync.upserted} bundled stage registry rows")

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
            HubManager,
            SkillsMPProvider,
        )

        skills_config = runner.config.skills if hasattr(runner.config, "skills") else SkillsConfig()

        api_keys: dict[str, str] = {}
        for _hub_name, hub_config in skills_config.hubs.items():
            if hub_config.auth_key_name:
                value = os.environ.get(hub_config.auth_key_name)
                if value:
                    api_keys[hub_config.auth_key_name] = value

        runner.hub_manager = HubManager(configs=skills_config.hubs, api_keys=api_keys)
        runner.hub_manager.register_provider_factory("clawdhub", ClawdHubProvider)
        runner.hub_manager.register_provider_factory("skillsmp", SkillsMPProvider)
        runner.hub_manager.register_provider_factory("github-collection", GitHubCollectionProvider)
        runner.hub_manager.register_provider_factory("claude-plugins", ClaudePluginsProvider)
        runner.hub_manager._skill_description_config = (
            runner.config.skill_description if hasattr(runner.config, "skill_description") else None
        )
        logger.debug(f"HubManager initialized with {len(skills_config.hubs)} hubs")
    except Exception as e:
        logger.warning(f"Failed to initialize HubManager: {e}")
