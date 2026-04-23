"""Shared helpers for runner tests."""

from unittest.mock import AsyncMock, MagicMock, patch

RUNNER_INIT_SESSION_MANAGER_PATCH = "gobby.runner_init.SessionManager"


def set_mock_default(obj: MagicMock, name: str, default):
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

    if getattr(config, "websocket", None) is None:
        config.websocket = None

    config.session_lifecycle = getattr(config, "session_lifecycle", MagicMock())
    config.message_tracking = getattr(config, "message_tracking", None)
    config.memory_sync = getattr(config, "memory_sync", MagicMock())
    set_mock_default(config.memory_sync, "enabled", False)

    config.ui = getattr(config, "ui", MagicMock())
    set_mock_default(config.ui, "enabled", False)
    set_mock_default(config.ui, "mode", "prod")
    set_mock_default(config.ui, "host", "localhost")
    set_mock_default(config.ui, "port", 5173)

    config.embeddings = getattr(config, "embeddings", MagicMock())
    set_mock_default(config.embeddings, "api_base", "")
    set_mock_default(config.embeddings, "model", "text-embedding-3-small")
    set_mock_default(config.embeddings, "api_key", None)
    set_mock_default(config.embeddings, "dim", 1536)

    config.databases = getattr(config, "databases", MagicMock())
    config.databases.qdrant = getattr(config.databases, "qdrant", MagicMock())
    set_mock_default(config.databases.qdrant, "url", "")
    set_mock_default(config.databases.qdrant, "collection_prefix", "test_")
    config.databases.neo4j = getattr(config.databases, "neo4j", MagicMock())
    set_mock_default(config.databases.neo4j, "url", "")
    set_mock_default(config.databases.neo4j, "auth", None)
    set_mock_default(config.databases.neo4j, "database", "neo4j")
    set_mock_default(config.databases.neo4j, "graph_search", True)
    set_mock_default(config.databases.neo4j, "graph_min_score", 0.5)
    set_mock_default(config.databases.neo4j, "rrf_k", 60)

    config.code_index = getattr(config, "code_index", MagicMock())
    set_mock_default(config.code_index, "enabled", False)
    set_mock_default(config.code_index, "embedding_enabled", False)
    set_mock_default(config.code_index, "graph_enabled", False)
    set_mock_default(config.code_index, "summary_enabled", False)
    set_mock_default(config.code_index, "summary_batch_size", 20)
    set_mock_default(config.code_index, "maintenance_interval_seconds", 300)
    set_mock_default(config.code_index, "sync_worker_interval_seconds", 5)
    set_mock_default(config.code_index, "sync_worker_batch_size", 50)

    return config


def create_base_patches(
    mock_config=None,
    mock_mcp_manager=None,
    mock_http=None,
    mock_ws_server=None,
):
    """Create all standard patches needed for GobbyRunner tests."""
    if mock_config is not None:
        apply_safe_runner_config_defaults(mock_config)

    if mock_mcp_manager is None:
        mock_mcp_manager = AsyncMock()
        mock_mcp_manager.connect_all = AsyncMock()
        mock_mcp_manager.disconnect_all = AsyncMock()

    if mock_http is None:
        mock_http = MagicMock()
        mock_http.app = MagicMock()
        mock_http.port = 60887
    set_mock_default(mock_http, "_terminate_streamable_http_sessions", AsyncMock())

    mock_agent_monitor = AsyncMock()
    mock_agent_monitor.recover_or_cleanup_agents.return_value = (0, 0)

    patches = [
        patch("gobby.runner_init.init_telemetry"),
        patch("gobby.runner_init.get_machine_id", return_value="test-machine"),
        patch("gobby.runner_init.LocalDatabase"),
        patch("gobby.runner_init.run_migrations"),
        patch(RUNNER_INIT_SESSION_MANAGER_PATCH),
        patch("gobby.runner_init.LocalTaskManager"),
        patch("gobby.runner_init.SessionTaskManager"),
        patch("gobby.runner_init.MCPClientManager", return_value=mock_mcp_manager),
        patch("gobby.runner_init.TaskSyncManager"),
        patch("gobby.runner_init.MemorySyncManager"),
        patch("gobby.runner_init.SessionMessageProcessor", return_value=AsyncMock()),
        patch("gobby.runner_init.TaskValidator"),
        patch("gobby.runner_init.SessionLifecycleManager", return_value=AsyncMock()),
        patch("gobby.runner_init.create_llm_service", return_value=None),
        patch("gobby.runner_init.MemoryManager", return_value=None),
        patch("gobby.runner_init.HTTPServer", return_value=mock_http),
        patch("gobby.storage.secrets.SecretStore"),
        patch("gobby.storage.config_store.ConfigStore"),
        patch("gobby.runner_init.AgentLifecycleMonitor", return_value=mock_agent_monitor),
    ]

    if mock_config is not None:
        patches.insert(1, patch("gobby.runner_init.load_config", return_value=mock_config))
    else:
        patches.insert(1, patch("gobby.runner_init.load_config"))

    if mock_ws_server is not None:
        patches.append(patch("gobby.runner_init.WebSocketServer", return_value=mock_ws_server))
    else:
        patches.append(patch("gobby.runner_init.WebSocketServer"))

    return patches
