"""Shared helpers for runner tests."""

from typing import Any
from unittest.mock import DEFAULT, AsyncMock, MagicMock, patch
from uuid import uuid4

from gobby.config.logging import LoggingSettings

RUNNER_INIT_SESSION_MANAGER_PATCH = "gobby.runner_init.storage.SessionManager"


def normalize_mcp_manager(mock_mcp_manager: Any | None) -> MagicMock:
    """Match the production manager's synchronous and asynchronous method contracts."""
    manager = mock_mcp_manager or MagicMock()

    for method_name in ("connect_all", "disconnect_all"):
        method = manager.__dict__.get(method_name)
        if not isinstance(method, AsyncMock):
            setattr(manager, method_name, AsyncMock())

    get_server_config = manager.__dict__.get("get_server_config")
    if isinstance(get_server_config, AsyncMock):
        sync_get_server_config = MagicMock(side_effect=get_server_config.side_effect)
        sync_get_server_config.return_value = (
            None
            if get_server_config._mock_return_value is DEFAULT
            else get_server_config._mock_return_value
        )
        manager.get_server_config = sync_get_server_config
    elif not isinstance(get_server_config, MagicMock):
        manager.get_server_config = MagicMock(return_value=None)

    return manager


def set_mock_default(obj: MagicMock, name: str, default: Any) -> None:
    """Assign a default only when a MagicMock placeholder has not been made concrete."""
    value = getattr(obj, name, None)
    if isinstance(value, AsyncMock):
        return
    if isinstance(value, MagicMock):
        setattr(obj, name, default)
    elif value is None:
        setattr(obj, name, default)


def apply_safe_runner_config_defaults(config: MagicMock) -> MagicMock:
    """Populate runner tests with scalar defaults so background tasks stay deterministic."""
    config.bind_host = "localhost"
    config.hub_backend = "postgres"
    config.database_url = "postgresql://gobby:secret@localhost:60891/gobby"

    if getattr(config, "websocket", None) is None:
        config.websocket = None

    config.telemetry = getattr(config, "telemetry", MagicMock())
    config.telemetry.traces_enabled = False
    config.logging = LoggingSettings(dir=f"/tmp/gobby-test-logs-{uuid4().hex}")

    config.session_lifecycle = getattr(config, "session_lifecycle", MagicMock())
    config.message_tracking = getattr(config, "message_tracking", None)
    config.memory_backup = getattr(config, "memory_backup", MagicMock())
    set_mock_default(config.memory_backup, "enabled", False)

    config.ui = getattr(config, "ui", MagicMock())
    set_mock_default(config.ui, "enabled", False)
    set_mock_default(config.ui, "mode", "prod")
    set_mock_default(config.ui, "host", "localhost")
    set_mock_default(config.ui, "port", 5173)

    config.embeddings = getattr(config, "embeddings", MagicMock())
    set_mock_default(config.embeddings, "api_base", "http://127.0.0.1:11434/v1")
    set_mock_default(config.embeddings, "model", "text-embedding-3-small")
    set_mock_default(config.embeddings, "api_key", None)
    set_mock_default(config.embeddings, "dim", 1536)

    config.databases = getattr(config, "databases", MagicMock())
    config.databases.qdrant = getattr(config.databases, "qdrant", MagicMock())
    set_mock_default(config.databases.qdrant, "url", "")
    set_mock_default(config.databases.qdrant, "collection_prefix", "test_")
    config.databases.falkordb = getattr(config.databases, "falkordb", MagicMock())
    set_mock_default(config.databases.falkordb, "host", "127.0.0.1")
    set_mock_default(config.databases.falkordb, "port", 16379)
    set_mock_default(config.databases.falkordb, "password", None)
    set_mock_default(config.databases.falkordb, "graph_name", "gobby_kg")
    set_mock_default(config.databases.falkordb, "graph_search", True)
    set_mock_default(config.databases.falkordb, "graph_min_score", 0.5)
    set_mock_default(config.databases.falkordb, "rrf_k", 60)

    config.code_index = getattr(config, "code_index", MagicMock())
    set_mock_default(config.code_index, "enabled", False)
    set_mock_default(config.code_index, "embedding_enabled", False)
    set_mock_default(config.code_index, "graph_enabled", False)
    config.code_index.symbol_summary = getattr(config.code_index, "symbol_summary", MagicMock())
    set_mock_default(config.code_index.symbol_summary, "enabled", False)
    set_mock_default(config.code_index.symbol_summary, "batch_size", 20)
    set_mock_default(config.code_index, "maintenance_interval_seconds", 3600)
    set_mock_default(config.code_index, "maintenance_index_timeout_seconds", 900)
    set_mock_default(config.code_index, "nightly_full_reindex_enabled", True)
    set_mock_default(config.code_index, "nightly_full_reindex_cron", "0 2 * * *")
    set_mock_default(config.code_index, "nightly_full_reindex_timezone", None)
    set_mock_default(config.code_index, "nightly_full_reindex_timeout_seconds", 8 * 60 * 60)
    set_mock_default(config.code_index, "nightly_full_reindex_concurrency", 1)
    set_mock_default(
        config.code_index,
        "maintenance_log_file",
        f"/tmp/gobby-test-code-index-maintenance-{uuid4().hex}.log",
    )
    set_mock_default(config.code_index, "sync_worker_interval_seconds", 5)
    set_mock_default(config.code_index, "sync_worker_batch_size", 50)

    return config


def create_base_patches(
    mock_config: Any = None,
    mock_mcp_manager: Any = None,
    mock_http: Any = None,
    mock_ws_server: Any = None,
) -> list[Any]:
    """Create all standard patches needed for GobbyRunner tests."""
    if mock_config is not None:
        apply_safe_runner_config_defaults(mock_config)

    mock_mcp_manager = normalize_mcp_manager(mock_mcp_manager)

    if mock_http is None:
        mock_http = MagicMock()
        mock_http.app = MagicMock()
        mock_http.port = 60887
    set_mock_default(mock_http, "_terminate_streamable_http_sessions", AsyncMock())

    mock_agent_monitor = AsyncMock()
    mock_agent_monitor.recover_or_cleanup_agents.return_value = (0, 0)

    patches = [
        patch("gobby.runner_init.storage.init_telemetry"),
        patch("gobby.runner_init.storage.setup_file_logging"),
        patch("gobby.runner_init.storage.get_machine_id", return_value="test-machine"),
        patch("gobby.storage.hub.postgres.PostgresHubDatabase"),
        patch(RUNNER_INIT_SESSION_MANAGER_PATCH),
        patch("gobby.runner_init.storage.LocalTaskManager"),
        patch("gobby.runner_init.storage.SessionTaskManager"),
        patch("gobby.runner_init.services.MCPClientManager", return_value=mock_mcp_manager),
        patch("gobby.runner_init.services.MemoryBackupManager"),
        patch("gobby.runner_init.services.SessionMessageProcessor", return_value=AsyncMock()),
        patch("gobby.runner_init.services.TaskValidator"),
        patch("gobby.runner_init.orchestration.SessionLifecycleManager", return_value=AsyncMock()),
        patch("gobby.runner_init.services.create_llm_service", return_value=None),
        patch("gobby.runner_init.services.MemoryManager", return_value=None),
        patch("gobby.runner_init.servers.HTTPServer", return_value=mock_http),
        patch("gobby.storage.secrets.SecretStore"),
        patch("gobby.storage.config_store.ConfigStore"),
        patch("gobby.runner_init.storage.ensure_local_api_token"),
        patch(
            "gobby.runner_service_readiness.require_managed_services_ready",
            new=AsyncMock(),
        ),
        patch(
            "gobby.runner_init.orchestration.AgentLifecycleMonitor", return_value=mock_agent_monitor
        ),
    ]

    if mock_config is not None:
        patches.insert(1, patch("gobby.runner_init.storage.load_config", return_value=mock_config))
    else:
        patches.insert(1, patch("gobby.runner_init.storage.load_config"))

    if mock_ws_server is not None:
        patches.append(
            patch("gobby.runner_init.servers.WebSocketServer", return_value=mock_ws_server)
        )
    else:
        patches.append(patch("gobby.runner_init.servers.WebSocketServer"))

    return patches
