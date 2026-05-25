"""Initialization and configuration tests for GobbyRunner."""

import json
import logging
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gobby.runner import GobbyRunner
from gobby.runner_init import resolve_embedding_api_key
from tests.runner_helpers import create_base_patches, set_mock_default

pytestmark = [pytest.mark.unit, pytest.mark.usefixtures("fast_stop_hook_grace_window")]


def _set_config_value(db: Any, key: str, value: Any, *, is_secret: bool = False) -> None:
    db.execute(
        """
        INSERT INTO config_store (key, value, source, is_secret, updated_at)
        VALUES (?, ?, 'test', ?, CURRENT_TIMESTAMP)
        """,
        (key, json.dumps(value), is_secret),
    )


def _config_value(db: Any, key: str) -> Any | None:
    row = db.fetchone("SELECT value FROM config_store WHERE key = ?", (key,))
    if row is None:
        return None
    return json.loads(row["value"])


class TestGobbyRunnerInit:
    """Tests for GobbyRunner initialization."""

    def test_init_creates_components(self, tmp_path, mock_config_with_websocket) -> None:
        """Test that init creates all required components."""
        patches = create_base_patches(mock_config=mock_config_with_websocket)

        config_file = tmp_path / "config.yaml"
        config_file.write_text("")

        with ExitStack() as stack:
            mocks = [stack.enter_context(p) for p in patches]
            mock_http_cls = mocks[-4]
            mock_ws_cls = mocks[-1]

            runner = GobbyRunner(config_path=config_file, verbose=True)

            assert runner.config == mock_config_with_websocket
            assert runner.verbose is True
            assert runner.machine_id == "test-machine"
            assert runner._shutdown_requested is False
            mock_http_cls.assert_called_once()
            mock_ws_cls.assert_called_once()


class TestStaleNeo4jConfigStartup:
    """Startup cleanup for stale Neo4j config rows."""

    def test_init_storage_warns_and_cleans_stale_neo4j_config_before_final_load(
        self,
        tmp_path: Path,
        temp_db: Any,
        caplog: pytest.LogCaptureFixture,
        enable_log_propagation: None,
    ) -> None:
        """Stale Neo4j config is warned, cleaned, and migrated before services read config."""
        from gobby.runner_init.storage import init_storage_and_config

        _set_config_value(temp_db, "databases.neo4j.rrf_k", 80)
        _set_config_value(temp_db, "databases.neo4j.auth", "$secret:auth", is_secret=True)
        _set_config_value(temp_db, "mock.test.auth", "$secret:auth", is_secret=True)
        _set_config_value(temp_db, "databases.falkordb.host", "127.0.0.1")
        _set_config_value(temp_db, "databases.falkordb.port", 16379)
        _set_config_value(temp_db, "databases.falkordb.requirepass", "safe-pass")
        temp_db.execute(
            """
            INSERT INTO secrets (id, name, encrypted_value, category, description, created_at, updated_at)
            VALUES ('secret-auth', 'auth', 'encrypted', 'general', 'shared auth', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
            """
        )

        class FakeSecretStore:
            def __init__(self, db: Any) -> None:
                self.db = db

            def get(self, name: str) -> str | None:
                return "shared-pass" if name == "auth" else None

        class FakeModelCostStore:
            def __init__(self, db: Any) -> None:
                self.db = db

            def populate(self) -> None:
                return None

        config_file = tmp_path / "config.yaml"
        config_file.write_text("{}\n")
        runner = SimpleNamespace()

        with (
            patch("gobby.runner_init.storage.init_telemetry"),
            patch("gobby.runner_init.storage.get_machine_id", return_value="test-machine"),
            patch("gobby.runner_init.storage.init_hub_database", return_value=temp_db),
            patch("gobby.storage.secrets.SecretStore", FakeSecretStore),
            patch("gobby.storage.model_costs.ModelCostStore", FakeModelCostStore),
            patch("gobby.utils.dev.is_dev_mode", return_value=False),
        ):
            with caplog.at_level(logging.WARNING, logger="gobby"):
                init_storage_and_config(runner, config_file, verbose=False)

        assert "Detected stale Neo4j config keys" in caplog.text
        assert "databases.neo4j.rrf_k" in caplog.text
        assert "databases.neo4j.auth" in caplog.text
        assert "Cleaning them up now" in caplog.text

        assert runner.config.databases.falkordb.rrf_k == 80
        assert _config_value(temp_db, "databases.falkordb.rrf_k") == 80
        assert _config_value(temp_db, "databases.neo4j.rrf_k") is None
        assert _config_value(temp_db, "databases.neo4j.auth") is None
        assert _config_value(temp_db, "mock.test.auth") == "$secret:auth"
        assert temp_db.fetchone("SELECT name FROM secrets WHERE name = ?", ("auth",)) is not None


class TestSetMockDefault:
    """Tests for test helper default assignment behavior."""

    def test_preserves_asyncmock_overrides(self) -> None:
        """Existing AsyncMock attributes are not replaced by default values."""
        obj = MagicMock()
        existing = AsyncMock()
        obj.child = existing

        set_mock_default(obj, "child", False)

        assert obj.child is existing

    def test_init_without_websocket(self, mock_config: Any) -> None:
        """Test init when WebSocket is disabled."""
        mock_config.websocket = MagicMock()
        mock_config.websocket.enabled = False

        patches = create_base_patches(mock_config)

        with ExitStack() as stack:
            mocks = [stack.enter_context(p) for p in patches]
            mock_ws_cls = mocks[-1]

            runner = GobbyRunner()

            assert runner.websocket_server is None
            mock_ws_cls.assert_not_called()

    def test_init_websocket_none_config(self, mock_config: Any) -> None:
        """Test init when websocket config is None."""
        patches = create_base_patches(mock_config)

        with ExitStack() as stack:
            [stack.enter_context(p) for p in patches]

            runner = GobbyRunner()

            assert runner.websocket_server is None


class TestInitHubDatabase:
    """Tests for hub database initialization helpers."""

    def test_rejects_non_postgres_backend(self) -> None:
        """Non-PostgreSQL hub backends are rejected by the runtime."""
        from gobby.runner_init import helpers

        config = SimpleNamespace(
            hub_backend="local",
            database_url=None,
        )

        with pytest.raises(ValueError, match="postgres"):
            helpers.init_hub_database(config)

    def test_uses_postgres_backend_when_selected(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """PostgreSQL backend opens the configured DSN and applies migrations."""
        from gobby.runner_init import helpers

        with patch("gobby.storage.hub.postgres.PostgresHubDatabase") as postgres_database:
            db = MagicMock()
            postgres_database.return_value = db
            config = SimpleNamespace(
                hub_backend="postgres",
                database_url="postgresql://gobby:secret@localhost:60891/gobby",
            )

            result = helpers.init_hub_database(config)

        assert result is db
        postgres_database.assert_called_once_with("postgresql://gobby:secret@localhost:60891/gobby")
        db.apply_migrations.assert_called_once_with()

    def test_postgres_startup_retries_transient_connection_failure(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """PostgreSQL startup retries transient pool/connection failures."""
        import psycopg

        from gobby.runner_init import helpers

        sleeps: list[float] = []

        class FakePostgresDatabase:
            calls = 0

            def __init__(self, _dsn: str) -> None:
                pass

            def apply_migrations(self) -> None:
                self.calls += 1
                if self.calls < 3:
                    raise psycopg.OperationalError("database is starting")

        monkeypatch.setattr(
            "gobby.storage.hub.postgres.PostgresHubDatabase",
            FakePostgresDatabase,
        )
        monkeypatch.setattr(helpers.time, "sleep", sleeps.append)
        config = SimpleNamespace(
            hub_backend="postgres",
            database_url="postgresql://gobby:secret@localhost:60891/gobby",
        )

        result = helpers.init_hub_database(config)

        assert isinstance(result, FakePostgresDatabase)
        assert result.calls == 3
        assert sleeps == [0.25, 0.5]

    def test_postgres_backend_requires_database_url(self) -> None:
        """PostgreSQL backend requires a configured database_url."""
        from gobby.runner_init import helpers

        config = SimpleNamespace(
            hub_backend="postgres",
            database_url=None,
        )

        with pytest.raises(ValueError, match="database_url"):
            helpers.init_hub_database(config)


class TestGobbyRunnerInitialization:
    """Tests for component initialization during GobbyRunner.__init__."""

    def test_init_with_memory_manager(self) -> None:
        """Test that MemoryManager is initialized when memory config exists."""
        mock_config = MagicMock()
        mock_config.daemon_port = 60887
        mock_config.websocket = None
        mock_config.session_lifecycle = MagicMock()
        mock_config.message_tracking = None
        mock_config.memory_sync = MagicMock()
        mock_config.memory_sync.enabled = False
        mock_config.memory = MagicMock()

        mock_memory_manager = MagicMock()

        patches = create_base_patches(mock_config=mock_config)
        patches = [p for p in patches if "MemoryManager" not in str(p)]
        patches.append(
            patch("gobby.runner_init.services.MemoryManager", return_value=mock_memory_manager)
        )

        with ExitStack() as stack:
            [stack.enter_context(p) for p in patches]

            runner = GobbyRunner()

            assert runner.memory_manager == mock_memory_manager
            assert runner.memory_sync_manager is None

    def test_init_memory_manager_exception(self) -> None:
        """Test that MemoryManager initialization exception is handled."""
        mock_config = MagicMock()
        mock_config.daemon_port = 60887
        mock_config.websocket = None
        mock_config.session_lifecycle = MagicMock()
        mock_config.message_tracking = None
        mock_config.memory_sync = MagicMock()
        mock_config.memory_sync.enabled = False
        mock_config.memory = MagicMock()

        patches = create_base_patches(mock_config=mock_config)
        patches = [p for p in patches if "MemoryManager" not in str(p)]
        patches.append(
            patch(
                "gobby.runner_init.services.MemoryManager",
                side_effect=Exception("Memory init error"),
            )
        )

        with ExitStack() as stack:
            [stack.enter_context(p) for p in patches]

            runner = GobbyRunner()
            assert runner.memory_manager is None
            assert runner.memory_sync_manager is None

    def test_init_with_memory_sync_manager_does_not_import_jsonl(self) -> None:
        """Test MemorySyncManager initializes without automatic JSONL import."""
        mock_config = MagicMock()
        mock_config.daemon_port = 60887
        mock_config.websocket = None
        mock_config.session_lifecycle = MagicMock()
        mock_config.message_tracking = None
        mock_config.memory = MagicMock()
        mock_config.memory_sync = MagicMock()
        mock_config.memory_sync.enabled = True

        mock_memory_manager = MagicMock()
        mock_memory_manager.storage = MagicMock()
        mock_memory_sync_manager = MagicMock()
        mock_memory_sync_manager.import_sync.return_value = 0

        patches = create_base_patches(mock_config=mock_config)
        patches = [p for p in patches if "MemoryManager" not in str(p)]
        patches = [p for p in patches if "MemorySyncManager" not in str(p)]
        patches.append(
            patch("gobby.runner_init.services.MemoryManager", return_value=mock_memory_manager)
        )
        patches.append(
            patch(
                "gobby.runner_init.services.MemorySyncManager",
                return_value=mock_memory_sync_manager,
            )
        )

        with ExitStack() as stack:
            [stack.enter_context(p) for p in patches]

            runner = GobbyRunner()

            assert runner.memory_sync_manager == mock_memory_sync_manager
            assert runner.memory_manager == mock_memory_manager
            mock_memory_sync_manager.import_sync.assert_not_called()

    def test_init_task_sync_manager_does_not_import_jsonl(self) -> None:
        """Test TaskSyncManager initializes without automatic JSONL import."""
        mock_config = MagicMock()
        mock_config.daemon_port = 60887
        mock_config.websocket = None
        mock_config.session_lifecycle = MagicMock()
        mock_config.message_tracking = None
        mock_config.memory_sync = MagicMock()
        mock_config.memory_sync.enabled = False

        mock_task_sync_manager = MagicMock()

        patches = create_base_patches(mock_config=mock_config)
        patches = [p for p in patches if "TaskSyncManager" not in str(p)]
        patches.append(
            patch("gobby.runner_init.services.TaskSyncManager", return_value=mock_task_sync_manager)
        )

        with ExitStack() as stack:
            [stack.enter_context(p) for p in patches]

            runner = GobbyRunner()

            assert runner.task_sync_manager == mock_task_sync_manager
            assert runner.memory_sync_manager is None
            mock_task_sync_manager.import_from_jsonl.assert_not_called()

    def test_init_memory_sync_manager_exception(self) -> None:
        """Test MemorySyncManager initialization exception is handled."""
        mock_config = MagicMock()
        mock_config.daemon_port = 60887
        mock_config.websocket = None
        mock_config.session_lifecycle = MagicMock()
        mock_config.message_tracking = None
        mock_config.memory = MagicMock()
        mock_config.memory_sync = MagicMock()
        mock_config.memory_sync.enabled = True

        mock_memory_manager = MagicMock()
        mock_memory_manager.storage = MagicMock()

        patches = create_base_patches(mock_config=mock_config)
        patches = [p for p in patches if "MemoryManager" not in str(p)]
        patches = [p for p in patches if "MemorySyncManager" not in str(p)]
        patches.append(
            patch("gobby.runner_init.services.MemoryManager", return_value=mock_memory_manager)
        )
        patches.append(
            patch(
                "gobby.runner_init.services.MemorySyncManager",
                side_effect=Exception("Sync manager error"),
            )
        )

        with ExitStack() as stack:
            [stack.enter_context(p) for p in patches]

            runner = GobbyRunner()
            assert runner.memory_sync_manager is None
            assert runner.memory_manager == mock_memory_manager

    def test_init_with_message_processor(self) -> None:
        """Test SessionMessageProcessor initialization when message_tracking enabled."""
        mock_config = MagicMock()
        mock_config.daemon_port = 60887
        mock_config.websocket = None
        mock_config.session_lifecycle = MagicMock()
        mock_config.memory_sync = MagicMock()
        mock_config.memory_sync.enabled = False
        mock_config.message_tracking = MagicMock()
        mock_config.message_tracking.enabled = True
        mock_config.message_tracking.poll_interval = 5.0

        mock_message_processor = AsyncMock()

        patches = create_base_patches(mock_config=mock_config)
        patches = [p for p in patches if "SessionMessageProcessor" not in str(p)]
        patches.append(
            patch(
                "gobby.runner_init.services.SessionMessageProcessor",
                return_value=mock_message_processor,
            )
        )

        with ExitStack() as stack:
            [stack.enter_context(p) for p in patches]

            runner = GobbyRunner()

            assert runner.message_processor == mock_message_processor
            assert runner.lifecycle_manager is not None

    def test_init_with_task_validator(self) -> None:
        """Test TaskValidator initialization when LLM service and validation enabled."""
        mock_config = MagicMock()
        mock_config.daemon_port = 60887
        mock_config.websocket = None
        mock_config.session_lifecycle = MagicMock()
        mock_config.message_tracking = None
        mock_config.memory_sync = MagicMock()
        mock_config.memory_sync.enabled = False
        mock_config.gobby_tasks = MagicMock()
        mock_config.gobby_tasks.expansion = MagicMock()
        mock_config.gobby_tasks.expansion.enabled = False
        mock_config.gobby_tasks.validation = MagicMock()
        mock_config.gobby_tasks.validation.enabled = True

        mock_llm_service = MagicMock()
        mock_llm_service.enabled_providers = ["test"]
        mock_task_validator = MagicMock()

        patches = create_base_patches(mock_config=mock_config)
        patches = [p for p in patches if "create_llm_service" not in str(p)]
        patches = [p for p in patches if "TaskValidator" not in str(p)]
        patches.append(
            patch("gobby.runner_init.services.create_llm_service", return_value=mock_llm_service)
        )
        patches.append(
            patch("gobby.runner_init.services.TaskValidator", return_value=mock_task_validator)
        )

        with ExitStack() as stack:
            [stack.enter_context(p) for p in patches]

            runner = GobbyRunner()

            assert runner.task_validator == mock_task_validator
            assert runner.llm_service == mock_llm_service

    def test_init_task_validator_exception(self) -> None:
        """Test TaskValidator initialization exception is handled."""
        mock_config = MagicMock()
        mock_config.daemon_port = 60887
        mock_config.websocket = None
        mock_config.session_lifecycle = MagicMock()
        mock_config.message_tracking = None
        mock_config.memory_sync = MagicMock()
        mock_config.memory_sync.enabled = False
        mock_config.gobby_tasks = MagicMock()
        mock_config.gobby_tasks.expansion = MagicMock()
        mock_config.gobby_tasks.expansion.enabled = False
        mock_config.gobby_tasks.validation = MagicMock()
        mock_config.gobby_tasks.validation.enabled = True

        mock_llm_service = MagicMock()
        mock_llm_service.enabled_providers = ["test"]

        patches = create_base_patches(mock_config=mock_config)
        patches = [p for p in patches if "create_llm_service" not in str(p)]
        patches = [p for p in patches if "TaskValidator" not in str(p)]
        patches.append(
            patch("gobby.runner_init.services.create_llm_service", return_value=mock_llm_service)
        )
        patches.append(
            patch(
                "gobby.runner_init.services.TaskValidator", side_effect=Exception("Validator error")
            )
        )

        with ExitStack() as stack:
            [stack.enter_context(p) for p in patches]

            runner = GobbyRunner()
            assert runner.task_validator is None
            assert runner.llm_service == mock_llm_service

    def test_init_agent_runner_exception(self) -> None:
        """Test AgentRunner initialization exception is handled."""
        mock_config = MagicMock()
        mock_config.daemon_port = 60887
        mock_config.websocket = None
        mock_config.session_lifecycle = MagicMock()
        mock_config.message_tracking = None
        mock_config.memory_sync = MagicMock()
        mock_config.memory_sync.enabled = False

        patches = create_base_patches(mock_config=mock_config)
        patches.append(
            patch(
                "gobby.runner_init.orchestration.AgentRunner",
                side_effect=Exception("Agent runner error"),
            )
        )

        with ExitStack() as stack:
            [stack.enter_context(p) for p in patches]

            runner = GobbyRunner()
            assert runner.agent_runner is None
            assert runner.task_sync_manager is not None

    def test_init_llm_service_exception(self) -> None:
        """Test LLM service initialization exception is handled."""
        mock_config = MagicMock()
        mock_config.daemon_port = 60887
        mock_config.websocket = None
        mock_config.session_lifecycle = MagicMock()
        mock_config.message_tracking = None
        mock_config.memory_sync = MagicMock()
        mock_config.memory_sync.enabled = False

        patches = create_base_patches(mock_config=mock_config)
        patches = [p for p in patches if "create_llm_service" not in str(p)]
        patches.append(
            patch(
                "gobby.runner_init.services.create_llm_service",
                side_effect=Exception("LLM init error"),
            )
        )

        with ExitStack() as stack:
            [stack.enter_context(p) for p in patches]

            runner = GobbyRunner()
            assert runner.llm_service is None
            assert runner.task_validator is None


class TestResolveEmbeddingApiKey:
    """Tests for resolve_embedding_api_key (runner_init.py)."""

    @pytest.fixture
    def mock_secret_store(self):
        """Create a mock secret store."""
        store = MagicMock()
        store.get = MagicMock(
            side_effect=lambda name: {
                "openai_api_key": "sk-test-openai",
                "gemini_api_key": "gemini-test-key",
                "mistral_api_key": "mistral-test-key",
            }.get(name)
        )
        return store

    def test_default_model_resolves_openai(self, mock_secret_store) -> None:
        """text-embedding-3-small (no prefix) should resolve to openai_api_key."""
        key = resolve_embedding_api_key(mock_secret_store, "text-embedding-3-small")
        assert key == "sk-test-openai"
        mock_secret_store.get.assert_called_with("openai_api_key")

    def test_openai_prefix_resolves_openai(self, mock_secret_store) -> None:
        """openai/text-embedding-3-small should resolve to openai_api_key."""
        key = resolve_embedding_api_key(mock_secret_store, "openai/text-embedding-3-small")
        assert key == "sk-test-openai"
        mock_secret_store.get.assert_called_with("openai_api_key")

    def test_gemini_prefix_resolves_gemini(self, mock_secret_store) -> None:
        """gemini/ prefix should resolve to gemini_api_key."""
        key = resolve_embedding_api_key(mock_secret_store, "gemini/text-embedding-004")
        assert key == "gemini-test-key"
        mock_secret_store.get.assert_called_with("gemini_api_key")

    def test_mistral_prefix_resolves_mistral(self, mock_secret_store) -> None:
        """mistral/ prefix should resolve to mistral_api_key."""
        key = resolve_embedding_api_key(mock_secret_store, "mistral/mistral-embed")
        assert key == "mistral-test-key"
        mock_secret_store.get.assert_called_with("mistral_api_key")

    def test_local_returns_none(self, mock_secret_store) -> None:
        """local/ models don't need an API key."""
        key = resolve_embedding_api_key(mock_secret_store, "local/nomic-embed-text-v1.5")
        assert key is None
        mock_secret_store.get.assert_not_called()

    def test_ollama_returns_none(self, mock_secret_store) -> None:
        """ollama/ models don't need an API key."""
        key = resolve_embedding_api_key(mock_secret_store, "ollama/nomic-embed-text")
        assert key is None
        mock_secret_store.get.assert_not_called()

    def test_missing_secret_returns_none(self) -> None:
        """Returns None when the secret doesn't exist."""
        store = MagicMock()
        store.get = MagicMock(return_value=None)
        key = resolve_embedding_api_key(store, "text-embedding-3-small")
        assert key is None


class TestGobbyRunnerInitEdgeCases:
    """Edge case tests for GobbyRunner initialization."""

    def test_init_with_no_llm_service(self, mock_config) -> None:
        """Test init when LLM service creation returns None."""
        patches = create_base_patches(mock_config=mock_config)

        with ExitStack() as stack:
            [stack.enter_context(p) for p in patches]

            runner = GobbyRunner()

            assert runner.llm_service is None

    def test_init_llm_service_exception(self, mock_config) -> None:
        """Test init when LLM service creation raises."""
        patches = create_base_patches(mock_config=mock_config)
        patches = [p for p in patches if "create_llm_service" not in str(p)]
        patches.append(
            patch(
                "gobby.runner_init.services.create_llm_service",
                side_effect=Exception("LLM init error"),
            )
        )

        with ExitStack() as stack:
            [stack.enter_context(p) for p in patches]

            runner = GobbyRunner()

            assert runner.llm_service is None

    def test_init_with_verbose_false(self, mock_config) -> None:
        """Test init with verbose=False (default)."""
        patches = create_base_patches(mock_config=mock_config)

        with ExitStack() as stack:
            [stack.enter_context(p) for p in patches]

            runner = GobbyRunner()

            assert runner.verbose is False

    def test_shutdown_requested_initially_false(self, mock_config) -> None:
        """Test that _shutdown_requested is False on init."""
        patches = create_base_patches(mock_config=mock_config)

        with ExitStack() as stack:
            [stack.enter_context(p) for p in patches]

            runner = GobbyRunner()

            assert runner._shutdown_requested is False
