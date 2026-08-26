"""Initialization and configuration tests for GobbyRunner."""

import asyncio
import json
import os
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import ANY, AsyncMock, MagicMock, call, patch

import pytest

from gobby.config import DaemonConfig
from gobby.config.bootstrap import BootstrapConfig
from gobby.config.persistence import EmbeddingsConfig
from gobby.config.postgres_pool import PostgresPoolConfig
from gobby.config.tasks import GobbyTasksConfig, TaskExpansionConfig, TaskValidationConfig
from gobby.runner import GobbyRunner
from gobby.runner_init.orchestration import (
    RETIRED_SYSTEM_CRON_JOBS,
    _send_tmux_pane_wake,
    _send_tmux_session_wake,
)
from gobby.runner_lifecycle_subsystems import _start_system_automation_loop
from gobby.telemetry.span_store import GobbySpanExporter
from gobby.wiki.codewiki_dormant import CodewikiCronReconciliation
from tests.runner_helpers import (
    apply_safe_runner_config_defaults,
    create_base_patches,
    set_mock_default,
)

pytestmark = [pytest.mark.unit, pytest.mark.usefixtures("fast_stop_hook_grace_window")]


def test_daemon_process_disables_optional_git_locks() -> None:
    """Importing the runner marks every daemon git subprocess lock-free (#21055).

    A `git status` killed on timeout mid index-refresh would otherwise leave
    `.git/index.lock` behind in the shared checkout.
    """
    assert os.environ["GIT_OPTIONAL_LOCKS"] == "0"


def _set_config_value(db: Any, key: str, value: Any, *, is_secret: bool = False) -> None:
    db.execute(
        """
        INSERT INTO config_store (key, value, source, is_secret, updated_at)
        VALUES (%s, %s, 'test', %s, CURRENT_TIMESTAMP)
        """,
        (key, json.dumps(value), is_secret),
    )


def _config_value(db: Any, key: str) -> Any | None:
    row = db.fetchone("SELECT value FROM config_store WHERE key = %s", (key,))
    if row is None:
        return None
    return json.loads(row["value"])


class TestGobbyRunnerInit:
    def test_machine_registration_precedes_system_session_bootstrap(
        self, mock_config_with_websocket: MagicMock
    ) -> None:
        order: list[str] = []
        patches = create_base_patches(mock_config=mock_config_with_websocket)

        def register_machine(_database: object, machine_id: str) -> str:
            order.append("machine")
            return machine_id

        def bootstrap_system_session(_database: object) -> None:
            order.append("system_session")

        with ExitStack() as stack:
            for patch_context in patches:
                stack.enter_context(patch_context)
            stack.enter_context(
                patch(
                    "gobby.runner_init.storage.ensure_machine_identity",
                    side_effect=register_machine,
                )
            )
            stack.enter_context(
                patch(
                    "gobby.runner_init.storage.ensure_system_session",
                    side_effect=bootstrap_system_session,
                )
            )

            GobbyRunner()

        assert order == ["machine", "system_session"]

    """Tests for GobbyRunner initialization."""

    def test_init_creates_components(
        self,
        tmp_path: Path,
        mock_config_with_websocket: MagicMock,
    ) -> None:
        """Test that init creates all required components."""
        patches = create_base_patches(mock_config=mock_config_with_websocket)

        config_file = tmp_path / "config.yaml"
        config_file.write_text("")

        with ExitStack() as stack:
            mocks = [stack.enter_context(p) for p in patches]
            mocks_by_attribute = {
                patch_context.attribute: entered_mock
                for patch_context, entered_mock in zip(patches, mocks, strict=True)
            }
            mock_http_cls = mocks_by_attribute["HTTPServer"]
            mock_ws_cls = mocks_by_attribute["WebSocketServer"]

            runner = GobbyRunner(config_path=config_file, verbose=True)

            assert runner.startup_config == mock_config_with_websocket
            assert runner.verbose is True
            assert runner.machine_id == "00000000-0000-4000-8000-000000000001"
            assert runner._shutdown_requested is False
            assert runner.db_executor.max_workers >= 8
            assert runner.coverage_executor.max_concurrency >= 1
            cast(MagicMock, runner.database).resize_pool.assert_called_once_with(64)
            mock_http_cls.assert_called_once()
            mock_ws_cls.assert_called_once()

    def test_telemetry_uses_phase_two_config(self) -> None:
        runtime_config = apply_safe_runner_config_defaults(MagicMock())
        runtime_config.telemetry.exporter.otlp_endpoint = "https://collector.example/v1/traces"
        runtime_config.telemetry.exporter.otlp_headers = {"Authorization": "resolved-runtime-token"}
        patches = create_base_patches(mock_config=runtime_config)

        with ExitStack() as stack:
            mocks = [stack.enter_context(p) for p in patches]
            mocks_by_attribute = {
                patch_context.attribute: entered_mock
                for patch_context, entered_mock in zip(patches, mocks, strict=True)
            }
            runner = GobbyRunner()

            bootstrap_defaults = DaemonConfig.model_validate(BootstrapConfig().to_config_dict())
            mocks_by_attribute["setup_file_logging"].assert_called_once_with(
                bootstrap_defaults.logging,
                verbose=False,
            )
            mocks_by_attribute["init_telemetry"].assert_called_once_with(
                runtime_config.telemetry,
                runtime_config.logging,
                verbose=False,
            )
            assert runner.startup_config is runtime_config
            assert runner.startup_config.telemetry.exporter.otlp_headers == {
                "Authorization": "resolved-runtime-token"
            }
            repository = mocks_by_attribute["ConfigRepository"].return_value
            repository.runtime_candidate.assert_called_once_with({}, {})

    async def test_trace_export_broadcasts_from_worker_thread(
        self,
        mock_config_with_websocket: MagicMock,
    ) -> None:
        """Trace exports schedule WebSocket broadcasts on the daemon loop."""
        websocket_server = MagicMock()
        broadcast_complete = asyncio.Event()
        daemon_loop = asyncio.get_running_loop()

        async def broadcast_trace_event(event: dict[str, Any]) -> None:
            assert asyncio.get_running_loop() is daemon_loop
            assert event["type"] == "trace_event"
            broadcast_complete.set()

        websocket_server.broadcast_trace_event = AsyncMock(side_effect=broadcast_trace_event)
        patches = create_base_patches(
            mock_config=mock_config_with_websocket,
            mock_ws_server=websocket_server,
        )
        mock_config_with_websocket.telemetry.traces_enabled = True

        with ExitStack() as stack:
            for patch_context in patches:
                stack.enter_context(patch_context)
            add_exporter = stack.enter_context(
                patch("gobby.telemetry.providers.add_span_storage_exporter")
            )

            runner = GobbyRunner()
            callback = add_exporter.call_args.kwargs["broadcast_callback"]
            exporter = GobbySpanExporter(MagicMock(), broadcast_callback=callback)

            with patch.object(exporter, "_span_to_dict", return_value={"trace_id": "trace-1"}):
                await asyncio.to_thread(exporter.export, [MagicMock()])

            await asyncio.wait_for(broadcast_complete.wait(), timeout=1)

        websocket_server.broadcast_trace_event.assert_awaited_once()
        assert runner._pending_tasks == set()

    def test_secret_envelope_initialization_failure_aborts_startup(self) -> None:
        mock_db = MagicMock()
        mock_store = MagicMock()
        mock_store.ensure_ready.side_effect = RuntimeError("secret envelope initialization failed")
        mock_config_store = MagicMock()

        with (
            patch(
                "gobby.runner_init.storage.load_bootstrap",
                return_value=BootstrapConfig(database_url="postgresql://localhost/gobby"),
            ),
            patch("gobby.runner_init.storage.init_telemetry"),
            patch("gobby.runner_init.storage.setup_file_logging"),
            patch(
                "gobby.runner_init.storage.get_machine_id",
                return_value="00000000-0000-4000-8000-000000000001",
            ),
            patch(
                "gobby.runner_init.storage.ensure_machine_identity",
                return_value="00000000-0000-4000-8000-000000000001",
            ),
            patch("gobby.runner_init.storage.ensure_system_session"),
            patch("gobby.runner_init.storage.init_hub_database", return_value=mock_db),
            patch("gobby.storage.secrets.SecretStore", return_value=mock_store),
            patch("gobby.storage.config_store.ConfigStore", return_value=mock_config_store),
            pytest.raises(RuntimeError, match="secret envelope initialization failed"),
        ):
            GobbyRunner()

        assert mock_store.ensure_ready.call_count == 1
        assert mock_store.ensure_ready.call_args.args == ()
        assert mock_store.ensure_ready.call_args.kwargs == {}
        assert mock_config_store.method_calls == []

    def test_init_provisions_local_api_token_after_secret_envelope_setup(
        self,
        mock_config_with_websocket: DaemonConfig,
    ) -> None:
        patches = create_base_patches(mock_config=mock_config_with_websocket)

        with ExitStack() as stack:
            entered = [stack.enter_context(patch_context) for patch_context in patches]
            mocks = {
                patch_context.attribute: entered_mock
                for patch_context, entered_mock in zip(patches, entered, strict=True)
            }
            secret_store = mocks["SecretStore"].return_value
            auth_store = mocks["AuthStore"].return_value
            ensure_token = mocks["ensure_local_api_token"]
            ordering = MagicMock()
            ordering.attach_mock(secret_store.ensure_ready, "ensure_ready")
            ordering.attach_mock(ensure_token, "ensure_local_api_token")

            GobbyRunner()

        assert [call[0] for call in ordering.mock_calls[:2]] == [
            "ensure_ready",
            "ensure_local_api_token",
        ]
        ensure_token.assert_called_once_with(auth_store)

    def test_memory_stack_uses_embedding_secret_when_runtime_config_has_no_key(self) -> None:
        from gobby.runner_init import services

        runner = SimpleNamespace(
            startup_config=SimpleNamespace(
                memory=SimpleNamespace(),
                knowledge_graph_queue=SimpleNamespace(max_deterministic_attempts=3),
                embeddings=SimpleNamespace(
                    model="text-embedding-3-small",
                    api_key="",
                    api_base=None,
                    dim=1536,
                ),
                databases=SimpleNamespace(
                    qdrant=SimpleNamespace(
                        url="http://qdrant:6333",
                        api_key=None,
                        collection_prefix="test",
                    ),
                    falkordb=SimpleNamespace(password=None),
                ),
            ),
            database=MagicMock(),
            db_executor=SimpleNamespace(run=MagicMock()),
            llm_service=object(),
            secret_store=MagicMock(),
        )
        runner.secret_store.get.side_effect = (
            lambda name: "sk-secret" if name == "embeddings_api_key" else None
        )

        with (
            patch("gobby.runner_init.services.EmbeddingService") as mock_embedding_service,
            patch("gobby.runner_init.services.VectorStore") as mock_vector_store,
            patch("gobby.runner_init.services.MemoryManager") as mock_memory_manager,
        ):
            mock_embedding_service.return_value.is_configured.return_value = True
            services._init_memory_stack(cast(GobbyRunner, runner))

        mock_embedding_service.assert_any_call(
            model="text-embedding-3-small",
            api_base=None,
            api_key="sk-secret",
            dim=1536,
            query_prefix=None,
        )
        from gobby.storage.embedding_generation_state import EmbeddingGenerationState

        mock_vector_store.assert_called_once_with(
            url="http://qdrant:6333",
            api_key=None,
            embedding_dim=1536,
            generation_state=ANY,
        )
        generation_state = mock_vector_store.call_args.kwargs["generation_state"]
        assert isinstance(generation_state, EmbeddingGenerationState)
        runner.secret_store.get.assert_called_once_with("embeddings_api_key")
        from gobby.projects.fenced_vector_store import ProjectFencedVectorStore

        assert isinstance(runner.vector_store, ProjectFencedVectorStore)
        assert runner.vector_store._inner is mock_vector_store.return_value
        assert runner.memory_manager is mock_memory_manager.return_value
        mock_memory_manager.assert_called_once()
        embed_fn = mock_memory_manager.call_args.kwargs["embed_fn"]
        assert embed_fn is mock_embedding_service.return_value.generate_embedding

    def test_embedding_api_key_resolver_checks_legacy_secret_names(self) -> None:
        from gobby.runner_init.services import _resolve_embedding_api_key

        runner = SimpleNamespace(secret_store=MagicMock())
        runner.secret_store.get.side_effect = (
            lambda name: "sk-legacy" if name == "api_key" else None
        )
        emb_cfg = SimpleNamespace(api_key="")

        assert (
            _resolve_embedding_api_key(
                cast(GobbyRunner, runner),
                cast(EmbeddingsConfig, emb_cfg),
            )
            == "sk-legacy"
        )


class TestWakeTmuxSenders:
    """Tests for runner-level tmux wake sender wiring."""

    @pytest.mark.asyncio
    async def test_session_wake_can_escape_before_submit(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        calls: list[tuple[str, str, bool]] = []

        class FakeTmuxManager:
            async def send_keys(
                self,
                target: str,
                keys: str,
                *,
                literal: bool = True,
            ) -> bool:
                calls.append((target, keys, literal))
                return True

        async def fake_sleep(_seconds: float) -> None:
            return None

        monkeypatch.setattr(
            "gobby.agents.tmux.get_tmux_session_manager",
            lambda: FakeTmuxManager(),
        )
        monkeypatch.setattr("gobby.runner_init.orchestration.asyncio.sleep", fake_sleep)

        await _send_tmux_session_wake(
            "gobby-agent-abc",
            "Message from Gobby daemon: New activity available.",
            submit=True,
            escape_before_submit=True,
        )

        assert calls == [
            ("gobby-agent-abc", "Escape", False),
            (
                "gobby-agent-abc",
                "Message from Gobby daemon: New activity available.",
                True,
            ),
            ("gobby-agent-abc", "Enter", False),
        ]

    @pytest.mark.asyncio
    async def test_pane_wake_forwards_escape_before_submit(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        calls: list[tuple[str, str, list[str], bool]] = []

        async def fake_submit_literal_text_to_tmux_target(
            pane_id: str,
            message: str,
            *,
            tmux_cmd: list[str],
            escape_before_submit: bool = False,
        ) -> None:
            calls.append((pane_id, message, tmux_cmd, escape_before_submit))

        monkeypatch.setattr(
            "gobby.agents.tmux.text_injection.submit_literal_text_to_tmux_target",
            fake_submit_literal_text_to_tmux_target,
        )

        await _send_tmux_pane_wake(
            "%12",
            "Message from Gobby daemon: New activity available.",
            "/tmp/tmux-501/gobby",
            submit=True,
            escape_before_submit=True,
        )

        assert calls == [
            (
                "%12",
                "Message from Gobby daemon: New activity available.",
                ["tmux", "-S", "/tmp/tmux-501/gobby"],
                True,
            )
        ]


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
        """PostgreSQL startup separates migration and served runtime pools."""
        from gobby.runner_init import helpers

        with (
            patch(
                "gobby.runner_init.helpers.admitted_database_url",
                side_effect=lambda database_url: database_url,
            ),
            patch("gobby.storage.hub.postgres.PostgresHubDatabase") as postgres_database,
        ):
            migration_db = MagicMock()
            runtime_db = MagicMock()
            runtime_db.verify_runtime_identity = MagicMock()
            postgres_database.side_effect = [migration_db, runtime_db]
            config = SimpleNamespace(
                hub_backend="postgres",
                database_url="postgresql://gobby:secret@localhost:60891/gobby",
                postgres_pool=PostgresPoolConfig(min_size=3, max_size=12),
            )

            result = helpers.init_hub_database(config)

        assert result is runtime_db
        assert postgres_database.call_args_list == [
            call(
                "postgresql://gobby:secret@localhost:60891/gobby",
                pool_config=PostgresPoolConfig(min_size=2, max_size=2),
            ),
            call(
                "postgresql://gobby:secret@localhost:60891/gobby",
                pool_config=PostgresPoolConfig(min_size=2, max_size=2),
                runtime_role="gobby_daemon_runtime",
            ),
        ]
        migration_db.apply_migrations.assert_called_once_with()
        migration_db.close.assert_called_once_with()
        runtime_db.verify_runtime_identity.assert_called_once_with()

    def test_postgres_startup_retries_transient_connection_failure(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """PostgreSQL startup retries transient pool/connection failures."""
        import psycopg

        from gobby.runner_init import helpers

        sleeps: list[float] = []

        class FakePostgresDatabase:
            attempts = 0
            instances: list["FakePostgresDatabase"] = []

            def __init__(
                self,
                _dsn: str,
                *,
                pool_config: object,
                runtime_role: str | None = None,
            ) -> None:
                self.closed = False
                self.runtime_role = runtime_role
                self.instances.append(self)

            def apply_migrations(self) -> None:
                type(self).attempts += 1
                if self.attempts < 3:
                    raise psycopg.OperationalError("database is starting")

            def close(self) -> None:
                self.closed = True

            def verify_runtime_identity(self) -> None:
                assert self.runtime_role == "gobby_daemon_runtime"

        monkeypatch.setattr(
            "gobby.storage.hub.postgres.PostgresHubDatabase",
            FakePostgresDatabase,
        )
        monkeypatch.setattr(
            "gobby.runner_init.helpers.admitted_database_url",
            lambda database_url: database_url,
        )
        monkeypatch.setattr("gobby.runner_init.helpers.time.sleep", sleeps.append)
        config = SimpleNamespace(
            hub_backend="postgres",
            database_url="postgresql://gobby:secret@localhost:60891/gobby",
            postgres_pool=PostgresPoolConfig(),
        )

        result = helpers.init_hub_database(config)

        assert isinstance(result, FakePostgresDatabase)
        assert result is FakePostgresDatabase.instances[3]
        assert [instance.closed for instance in FakePostgresDatabase.instances] == [
            True,
            True,
            True,
            False,
        ]
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
        mock_config.memory_backup = MagicMock()
        mock_config.memory_backup.enabled = False
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
            assert runner.memory_backup_manager is None

    def test_init_memory_manager_exception(self, caplog: pytest.LogCaptureFixture) -> None:
        """Test that MemoryManager initialization exception is handled."""
        mock_config = MagicMock()
        mock_config.daemon_port = 60887
        mock_config.websocket = None
        mock_config.session_lifecycle = MagicMock()
        mock_config.message_tracking = None
        mock_config.memory_backup = MagicMock()
        mock_config.memory_backup.enabled = True
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
            assert runner.memory_backup_manager is None
            assert {"memory_manager", "memory_backup_manager"} <= runner.degraded_services
            assert "Skipping MemoryBackupManager initialization" in caplog.text
            memory_error = next(
                record
                for record in caplog.records
                if record.message == "Failed to initialize MemoryManager"
            )
            assert memory_error.exc_info is not None

    def test_init_code_indexer_exception(self, caplog: pytest.LogCaptureFixture) -> None:
        """Test that code indexer initialization exceptions are handled."""
        mock_config = MagicMock()
        mock_config.code_index.enabled = True

        patches = create_base_patches(mock_config=mock_config)
        patches.append(
            patch(
                "gobby.code_index.storage.CodeIndexStorage",
                side_effect=ImportError("Code index storage unavailable"),
            )
        )

        with ExitStack() as stack:
            [stack.enter_context(p) for p in patches]

            runner = GobbyRunner()

            assert runner.code_indexer is None
            assert "code_indexer" in runner.degraded_services
            code_index_error = next(
                record
                for record in caplog.records
                if record.message == "Failed to initialize code indexer"
            )
            assert code_index_error.exc_info is not None

    def test_init_with_memory_backup_manager_does_not_restore_jsonl(self) -> None:
        """Test MemoryBackupManager initializes without automatic JSONL restore."""
        mock_config = MagicMock()
        mock_config.daemon_port = 60887
        mock_config.websocket = None
        mock_config.session_lifecycle = MagicMock()
        mock_config.message_tracking = None
        mock_config.memory = MagicMock()
        mock_config.memory_backup = MagicMock()
        mock_config.memory_backup.enabled = True

        mock_memory_manager = MagicMock()
        mock_memory_manager.storage = MagicMock()
        mock_memory_backup_manager = MagicMock()
        mock_memory_backup_manager.restore_sync.return_value = 0

        patches = create_base_patches(mock_config=mock_config)
        patches = [p for p in patches if "MemoryManager" not in str(p)]
        patches = [p for p in patches if "MemoryBackupManager" not in str(p)]
        patches.append(
            patch("gobby.runner_init.services.MemoryManager", return_value=mock_memory_manager)
        )
        patches.append(
            patch(
                "gobby.runner_init.services.MemoryBackupManager",
                return_value=mock_memory_backup_manager,
            )
        )

        with ExitStack() as stack:
            [stack.enter_context(p) for p in patches]

            runner = GobbyRunner()

            assert runner.memory_backup_manager == mock_memory_backup_manager
            assert runner.memory_manager == mock_memory_manager
            mock_memory_backup_manager.restore_sync.assert_not_called()

    def test_init_memory_backup_manager_exception(self) -> None:
        """Test MemoryBackupManager initialization exception is handled."""
        mock_config = MagicMock()
        mock_config.daemon_port = 60887
        mock_config.websocket = None
        mock_config.session_lifecycle = MagicMock()
        mock_config.message_tracking = None
        mock_config.memory = MagicMock()
        mock_config.memory_backup = MagicMock()
        mock_config.memory_backup.enabled = True

        mock_memory_manager = MagicMock()
        mock_memory_manager.storage = MagicMock()

        patches = create_base_patches(mock_config=mock_config)
        patches = [p for p in patches if "MemoryManager" not in str(p)]
        patches = [p for p in patches if "MemoryBackupManager" not in str(p)]
        patches.append(
            patch("gobby.runner_init.services.MemoryManager", return_value=mock_memory_manager)
        )
        patches.append(
            patch(
                "gobby.runner_init.services.MemoryBackupManager",
                side_effect=Exception("Sync manager error"),
            )
        )

        with ExitStack() as stack:
            [stack.enter_context(p) for p in patches]

            runner = GobbyRunner()
            assert runner.memory_backup_manager is None
            assert runner.memory_manager == mock_memory_manager

    def test_init_with_message_processor(self) -> None:
        """Test SessionMessageProcessor initialization when message_tracking enabled."""
        mock_config = MagicMock()
        mock_config.daemon_port = 60887
        mock_config.websocket = None
        mock_config.session_lifecycle = MagicMock()
        mock_config.memory_backup = MagicMock()
        mock_config.memory_backup.enabled = False
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
        mock_config = DaemonConfig(
            daemon_port=60887,
            gobby_tasks=GobbyTasksConfig(
                expansion=TaskExpansionConfig(enabled=False),
                validation=TaskValidationConfig(enabled=True),
            ),
        )

        mock_text_generation = MagicMock()
        mock_llm_service = MagicMock()
        mock_llm_service.enabled_providers = ["test"]
        mock_task_validator = MagicMock()
        task_validator_factory = MagicMock(return_value=mock_task_validator)

        patches = create_base_patches(mock_config=mock_config)
        patches = [p for p in patches if "create_llm_service" not in str(p)]
        patches = [p for p in patches if "TaskValidator" not in str(p)]
        patches.append(
            patch(
                "gobby.runner_init.services.build_daemon_text_generation_service",
                return_value=mock_text_generation,
            )
        )
        patches.append(
            patch("gobby.runner_init.services.create_llm_service", return_value=mock_llm_service)
        )
        patches.append(patch("gobby.runner_init.services.TaskValidator", task_validator_factory))

        with ExitStack() as stack:
            [stack.enter_context(p) for p in patches]

            runner = GobbyRunner()

            assert runner.task_validator == mock_task_validator
            assert runner.llm_service == mock_llm_service
            assert runner.text_generation_service == mock_text_generation
            validator_kwargs = task_validator_factory.call_args.kwargs
            assert validator_kwargs["llm_service"] is mock_llm_service
            assert validator_kwargs["config"] is mock_config.gobby_tasks.validation
            assert validator_kwargs["db"] is runner.database
            assert "tool_chat_service" not in validator_kwargs

    def test_init_task_validator_exception(self) -> None:
        """Test TaskValidator initialization exception is handled."""
        mock_config = DaemonConfig(
            daemon_port=60887,
            gobby_tasks=GobbyTasksConfig(
                expansion=TaskExpansionConfig(enabled=False),
                validation=TaskValidationConfig(enabled=True),
            ),
        )

        mock_text_generation = MagicMock()
        mock_llm_service = MagicMock()
        mock_llm_service.enabled_providers = ["test"]

        patches = create_base_patches(mock_config=mock_config)
        patches = [p for p in patches if "create_llm_service" not in str(p)]
        patches = [p for p in patches if "TaskValidator" not in str(p)]
        patches.append(
            patch(
                "gobby.runner_init.services.build_daemon_text_generation_service",
                return_value=mock_text_generation,
            )
        )
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
            assert runner.text_generation_service == mock_text_generation
            assert "task_validator" in runner.degraded_services

    def test_init_agent_runner_exception(self) -> None:
        """Test AgentRunner initialization exception is handled."""
        mock_config = MagicMock()
        mock_config.daemon_port = 60887
        mock_config.websocket = None
        mock_config.session_lifecycle = MagicMock()
        mock_config.message_tracking = None
        mock_config.memory_backup = MagicMock()
        mock_config.memory_backup.enabled = False

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
            assert runner.startup_config is mock_config
            assert runner.agent_runner is None
            assert runner.memory_backup_manager is None

    def test_init_llm_service_exception(self, caplog: pytest.LogCaptureFixture) -> None:
        """Test LLM service initialization exception is handled."""
        mock_config = MagicMock()
        mock_config.daemon_port = 60887
        mock_config.websocket = None
        mock_config.session_lifecycle = MagicMock()
        mock_config.message_tracking = None
        mock_config.memory_backup = MagicMock()
        mock_config.memory_backup.enabled = False

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
            assert {"llm_service", "task_validator"} <= runner.degraded_services
            assert "Skipping TaskValidator initialization" in caplog.text
            llm_error = next(
                record
                for record in caplog.records
                if record.message == "Failed to initialize LLM service"
            )
            assert llm_error.exc_info is not None


class TestCronInitializationFailures:
    def test_legacy_pipeline_heartbeat_job_is_retired(self) -> None:
        assert "gobby:pipeline-heartbeat" in RETIRED_SYSTEM_CRON_JOBS

    @pytest.mark.parametrize(
        "query_results",
        [
            [RuntimeError("initial prefix query failed")],
            [[], RuntimeError("residual prefix query failed")],
        ],
        ids=["initial-query", "residual-query"],
    )
    def test_reconciliation_query_failure_does_not_block_startup(
        self,
        query_results: list[object],
    ) -> None:
        mock_config = MagicMock()
        mock_config.code_index.enabled = False
        patches = create_base_patches(mock_config=mock_config)
        cron_storage = MagicMock()
        cron_storage.list_jobs_by_name_prefix.side_effect = query_results

        with ExitStack() as stack:
            for patch_context in patches:
                stack.enter_context(patch_context)
            stack.enter_context(
                patch("gobby.storage.cron.CronJobStorage", return_value=cron_storage)
            )
            scheduler = stack.enter_context(patch("gobby.scheduler.scheduler.CronScheduler"))

            runner = GobbyRunner()

        assert "codewiki_dormant_reconciliation" in runner.degraded_services
        assert runner.cron_storage is cron_storage
        assert runner.cron_scheduler is scheduler.return_value
        assert cron_storage.list_jobs_by_name_prefix.call_count == len(query_results)
        scheduler.assert_called_once()

    @pytest.mark.parametrize(
        "reconciliation",
        [
            CodewikiCronReconciliation(disabled=(), failed=("failed",), residual_enabled=()),
            CodewikiCronReconciliation(disabled=(), failed=(), residual_enabled=("residual",)),
        ],
        ids=["failed-update", "residual-enabled"],
    )
    def test_reconciliation_residue_marks_startup_degraded(
        self,
        reconciliation: CodewikiCronReconciliation,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        mock_config = MagicMock()
        mock_config.code_index.enabled = False
        patches = create_base_patches(mock_config=mock_config)

        with ExitStack() as stack:
            for patch_context in patches:
                stack.enter_context(patch_context)
            stack.enter_context(patch("gobby.storage.cron.CronJobStorage"))
            stack.enter_context(patch("gobby.scheduler.scheduler.CronScheduler"))
            stack.enter_context(
                patch(
                    "gobby.wiki.codewiki_dormant.reconcile_codewiki_crons_disabled",
                    return_value=reconciliation,
                )
            )

            runner = GobbyRunner()

        assert "codewiki_dormant_reconciliation" in runner.degraded_services
        assert any(
            "CodeWiki cron reconciliation left enabled rows" in record.getMessage()
            for record in caplog.records
        )

    @pytest.mark.parametrize("indexer_state", ["absent", "disabled", "failed"])
    def test_reconciliation_independent_of_code_indexer(self, indexer_state: str) -> None:
        mock_config = MagicMock()
        mock_config.code_index.enabled = indexer_state != "disabled"
        patches = create_base_patches(mock_config=mock_config)
        if indexer_state == "absent":
            patches.extend(
                [
                    patch("gobby.code_index.storage.CodeIndexStorage"),
                    patch("gobby.code_index.gcode_gateway.GcodeGateway"),
                    patch("gobby.code_index.context.CodeIndexContext", return_value=None),
                ]
            )
        elif indexer_state == "failed":
            patches.append(
                patch(
                    "gobby.code_index.storage.CodeIndexStorage",
                    side_effect=RuntimeError("code index unavailable"),
                )
            )

        with ExitStack() as stack:
            for patch_context in patches:
                stack.enter_context(patch_context)
            stack.enter_context(patch("gobby.storage.cron.CronJobStorage"))
            scheduler = stack.enter_context(patch("gobby.scheduler.scheduler.CronScheduler"))
            reconcile = stack.enter_context(
                patch(
                    "gobby.wiki.codewiki_dormant.reconcile_codewiki_crons_disabled",
                    return_value=CodewikiCronReconciliation((), (), ()),
                )
            )

            runner = GobbyRunner()

        assert runner.code_indexer is None
        assert runner.cron_storage is reconcile.call_args.args[0]
        assert runner.cron_scheduler is scheduler.return_value
        assert "codewiki_dormant_reconciliation" not in runner.degraded_services
        reconcile.assert_called_once_with(runner.cron_storage)

    @pytest.mark.parametrize(
        ("failed_target", "expected_message"),
        [
            ("gobby.storage.cron.CronJobStorage", "Failed to initialize CronJobStorage"),
            ("gobby.scheduler.executor.CronExecutor", "Failed to initialize CronExecutor"),
            (
                "gobby.system_automation.SystemAutomationLoop",
                "Failed to initialize SystemAutomationLoop",
            ),
            ("gobby.scheduler.scheduler.CronScheduler", "Failed to initialize CronScheduler"),
        ],
    )
    def test_each_component_logs_its_own_failure_with_traceback(
        self,
        failed_target: str,
        expected_message: str,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        mock_config = MagicMock()
        patches = create_base_patches(mock_config=mock_config)
        cron_targets = [
            "gobby.storage.cron.CronJobStorage",
            "gobby.scheduler.executor.CronExecutor",
            "gobby.system_automation.SystemAutomationLoop",
            "gobby.scheduler.scheduler.CronScheduler",
        ]

        with ExitStack() as stack:
            for patch_context in patches:
                stack.enter_context(patch_context)
            constructors = {target: stack.enter_context(patch(target)) for target in cron_targets}
            constructors[failed_target].side_effect = RuntimeError("component failed")

            GobbyRunner()

        matching_records = [
            record for record in caplog.records if record.getMessage() == expected_message
        ]
        assert len(matching_records) == 1
        assert matching_records[0].exc_info is not None

    @pytest.mark.asyncio
    async def test_scheduler_failure_prevents_automation_loop_start(self) -> None:
        mock_config = MagicMock()
        patches = create_base_patches(mock_config=mock_config)
        automation_loop = MagicMock()
        automation_loop.start = AsyncMock()

        with ExitStack() as stack:
            for patch_context in patches:
                stack.enter_context(patch_context)
            stack.enter_context(patch("gobby.storage.cron.CronJobStorage"))
            stack.enter_context(patch("gobby.scheduler.executor.CronExecutor"))
            stack.enter_context(
                patch(
                    "gobby.system_automation.SystemAutomationLoop",
                    return_value=automation_loop,
                )
            )
            stack.enter_context(
                patch(
                    "gobby.scheduler.scheduler.CronScheduler",
                    side_effect=RuntimeError("scheduler failed"),
                )
            )

            runner = GobbyRunner()
            await _start_system_automation_loop(runner, tracker=None)

        assert runner.cron_scheduler is None
        assert runner.system_automation_loop is None
        automation_loop.start.assert_not_awaited()


class TestGobbyRunnerInitEdgeCases:
    """Edge case tests for GobbyRunner initialization."""

    def test_init_with_no_llm_service(self, mock_config: MagicMock) -> None:
        """Test init when LLM service creation returns None."""
        patches = create_base_patches(mock_config=mock_config)

        with ExitStack() as stack:
            [stack.enter_context(p) for p in patches]

            runner = GobbyRunner()

            assert runner.llm_service is None

    def test_init_llm_service_exception(self, mock_config: MagicMock) -> None:
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

    def test_init_with_verbose_false(self, mock_config: MagicMock) -> None:
        """Test init with verbose=False (default)."""
        patches = create_base_patches(mock_config=mock_config)

        with ExitStack() as stack:
            [stack.enter_context(p) for p in patches]

            runner = GobbyRunner()

            assert runner.verbose is False

    def test_shutdown_requested_initially_false(self, mock_config: MagicMock) -> None:
        """Test that _shutdown_requested is False on init."""
        patches = create_base_patches(mock_config=mock_config)

        with ExitStack() as stack:
            [stack.enter_context(p) for p in patches]

            runner = GobbyRunner()

            assert runner._shutdown_requested is False


class TestDefinitionRevisionListenerLifecycle:
    def test_sync_construct_builds_listener_without_event_loop(
        self,
        mock_config_with_websocket: MagicMock,
    ) -> None:
        patches = create_base_patches(mock_config=mock_config_with_websocket)

        with ExitStack() as stack:
            for patch_context in patches:
                stack.enter_context(patch_context)
            runner = GobbyRunner()

        listener = runner.definition_revision_listener
        assert listener is not None
        assert listener.listen_task is None
        assert listener.poll_task is None

    def test_construction_failure_after_listener_rolls_back_listener(
        self,
        mock_config_with_websocket: MagicMock,
    ) -> None:
        from gobby.storage.definitions.notifications import DefinitionRevisionListener

        patches = create_base_patches(mock_config=mock_config_with_websocket)
        closed: list[str] = []
        real_close = DefinitionRevisionListener.close

        async def tracking_close(self: DefinitionRevisionListener) -> None:
            closed.append("close")
            await real_close(self)

        with ExitStack() as stack:
            for patch_context in patches:
                stack.enter_context(patch_context)
            stack.enter_context(
                patch(
                    "gobby.runner_init.init_runtime_capacity",
                    side_effect=RuntimeError("post-storage boom"),
                )
            )
            stack.enter_context(patch.object(DefinitionRevisionListener, "close", tracking_close))
            with pytest.raises(RuntimeError, match="post-storage boom"):
                GobbyRunner()

        assert closed == ["close"]
