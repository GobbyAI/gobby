"""Tests for service lifecycle utilities."""

import logging
import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from gobby.cli.services import (
    ensure_local_embedding_service_ready,
    get_falkordb_status,
    get_local_embedding_service_failure_reason,
    is_falkordb_healthy,
    is_falkordb_installed,
    is_qdrant_healthy,
    try_autoload_embedding_model,
)
from gobby.storage.config_store import ConfigStore
from gobby.storage.database import LocalDatabase
from gobby.storage.migrations import run_migrations

pytestmark = pytest.mark.unit


def _seed_falkordb_config(db_path: Path, *, host: str = "localhost", port: int = 16379) -> None:
    db = LocalDatabase(db_path)
    try:
        run_migrations(db)
        store = ConfigStore(db)
        store.set("databases.falkordb.host", host, source="test")
        store.set("databases.falkordb.port", port, source="test")
    finally:
        db.close()


class _FakeRedisClient:
    def __init__(
        self,
        *,
        ping_result: bool = True,
        ping_error: Exception | None = None,
        close_error: Exception | None = None,
    ) -> None:
        self.ping_result = ping_result
        self.ping_error = ping_error
        self.close_error = close_error
        self.ping_count = 0
        self.close_count = 0

    async def ping(self) -> bool:
        self.ping_count += 1
        if self.ping_error is not None:
            raise self.ping_error
        return self.ping_result

    async def aclose(self) -> None:
        self.close_count += 1
        if self.close_error is not None:
            raise self.close_error


class _RedisFactory:
    def __init__(self, client: _FakeRedisClient) -> None:
        self.client = client
        self.calls: list[dict[str, object]] = []

    def __call__(self, **kwargs: object) -> _FakeRedisClient:
        self.calls.append(kwargs)
        return self.client


class TestIsFalkorDBInstalled:
    """Tests for is_falkordb_installed()."""

    def test_installed_when_config_has_host_and_port(self, tmp_path: Path) -> None:
        _seed_falkordb_config(tmp_path / "gobby-hub.db")

        assert is_falkordb_installed(gobby_home=tmp_path) is True

    def test_not_installed_when_config_missing(self, tmp_path: Path) -> None:
        assert is_falkordb_installed(gobby_home=tmp_path) is False

    def test_no_bootstrap_uses_tmp_home_db(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _seed_falkordb_config(tmp_path / "gobby-hub.db")

        import gobby.cli.services as services
        from gobby.cli.installers import falkor

        resolved_paths: list[Path] = []
        original_resolver = falkor._resolve_falkordb_db_path

        def track_resolver(home: Path) -> Path:
            path = original_resolver(home)
            resolved_paths.append(path)
            return path

        monkeypatch.setattr(falkor, "_resolve_falkordb_db_path", track_resolver)
        monkeypatch.setattr(services, "get_gobby_home", lambda: Path.home() / ".gobby")

        assert services.is_falkordb_installed(gobby_home=tmp_path) is True
        assert resolved_paths == [tmp_path / "gobby-hub.db"]


@pytest.fixture
def mock_async_client() -> AsyncMock:
    """Create a reusable async HTTP client mock with context-manager support."""
    client = AsyncMock()
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=False)
    return client


def _completed_process(
    args: list[str],
    *,
    returncode: int = 0,
    stdout: str = "",
    stderr: str = "",
) -> subprocess.CompletedProcess[str]:
    """Build a completed subprocess result for CLI mocks."""
    return subprocess.CompletedProcess(
        args=args, returncode=returncode, stdout=stdout, stderr=stderr
    )


async def _run_inline(func, *args, **kwargs):
    """Execute asyncio.to_thread call sites synchronously in tests."""
    return func(*args, **kwargs)


class TestIsFalkorDBHealthy:
    """Tests for is_falkordb_healthy()."""

    @pytest.mark.asyncio
    async def test_healthy_when_ping_succeeds(self) -> None:
        client = _FakeRedisClient(ping_result=True)
        redis_factory = _RedisFactory(client)
        redis_module = SimpleNamespace(Redis=redis_factory)

        with patch("gobby.cli.services.importlib.import_module", return_value=redis_module):
            assert await is_falkordb_healthy("localhost", 16379, "secret") is True

        assert redis_factory.calls == [
            {"host": "localhost", "port": 16379, "password": "secret", "socket_timeout": 5}
        ]
        assert client.ping_count == 1
        assert client.close_count == 1

    @pytest.mark.asyncio
    async def test_unhealthy_when_ping_fails(self) -> None:
        client = _FakeRedisClient(ping_error=ConnectionError("refused"))
        redis_factory = _RedisFactory(client)
        redis_module = SimpleNamespace(Redis=redis_factory)

        with patch("gobby.cli.services.importlib.import_module", return_value=redis_module):
            assert await is_falkordb_healthy("localhost", 16379, "secret") is False

        assert client.ping_count == 1
        assert client.close_count == 1

    @pytest.mark.asyncio
    async def test_unhealthy_when_close_fails(self) -> None:
        client = _FakeRedisClient(ping_result=True, close_error=RuntimeError("close failed"))
        redis_factory = _RedisFactory(client)
        redis_module = SimpleNamespace(Redis=redis_factory)

        with patch("gobby.cli.services.importlib.import_module", return_value=redis_module):
            assert await is_falkordb_healthy("localhost", 16379, "secret") is True

        assert client.ping_count == 1
        assert client.close_count == 1

    @pytest.mark.asyncio
    async def test_unhealthy_when_host_or_port_missing(self) -> None:
        assert await is_falkordb_healthy(None, 16379, "secret") is False
        assert await is_falkordb_healthy("localhost", None, "secret") is False

    @pytest.mark.asyncio
    async def test_unreachable_probe_does_not_log_warning(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        client = _FakeRedisClient(ping_error=ConnectionError("refused"))
        redis_module = SimpleNamespace(Redis=_RedisFactory(client))
        caplog.set_level(logging.DEBUG, logger="gobby.cli.services")

        with patch("gobby.cli.services.importlib.import_module", return_value=redis_module):
            assert await is_falkordb_healthy("localhost", 16379, "secret") is False

        assert any(
            record.levelno == logging.DEBUG
            and "FalkorDB health check failed: localhost:16379 unreachable: ConnectionError: refused"
            in record.getMessage()
            for record in caplog.records
        )
        assert not any(record.levelno >= logging.WARNING for record in caplog.records)


class TestIsQdrantHealthy:
    """Tests for is_qdrant_healthy()."""

    @pytest.mark.asyncio
    async def test_unreachable_probe_does_not_log_warning(
        self, mock_async_client: AsyncMock, caplog: pytest.LogCaptureFixture
    ) -> None:
        mock_async_client.get = AsyncMock(side_effect=httpx.ConnectError("refused"))
        caplog.set_level(logging.DEBUG, logger="gobby.cli.services")

        with patch("gobby.cli.services.httpx.AsyncClient", return_value=mock_async_client):
            assert await is_qdrant_healthy("http://localhost:6333") is False

        assert any(
            record.levelno == logging.DEBUG
            and "Qdrant health check failed: http://localhost:6333/healthz unreachable: ConnectError: refused"
            in record.getMessage()
            for record in caplog.records
        )
        assert not any(record.levelno >= logging.WARNING for record in caplog.records)

    @pytest.mark.asyncio
    async def test_non_200_response_logs_debug_without_warning(
        self, mock_async_client: AsyncMock, caplog: pytest.LogCaptureFixture
    ) -> None:
        mock_async_client.get = AsyncMock(return_value=httpx.Response(503))
        caplog.set_level(logging.DEBUG, logger="gobby.cli.services")

        with patch("gobby.cli.services.httpx.AsyncClient", return_value=mock_async_client):
            assert await is_qdrant_healthy("http://localhost:6333") is False

        assert any(
            record.levelno == logging.DEBUG
            and "Qdrant health check failed: http://localhost:6333/healthz returned 503"
            in record.getMessage()
            for record in caplog.records
        )
        assert not any(record.levelno >= logging.WARNING for record in caplog.records)


class TestGetFalkorDBStatus:
    """Tests for get_falkordb_status()."""

    @pytest.mark.asyncio
    async def test_status_installed_and_healthy(self, tmp_path: Path) -> None:
        _seed_falkordb_config(tmp_path / "gobby-hub.db")

        with patch("gobby.cli.services.is_falkordb_healthy", new=AsyncMock(return_value=True)):
            status = await get_falkordb_status(gobby_home=tmp_path, password="secret")

        assert status["installed"] is True
        assert status["healthy"] is True
        assert status["url"] == "redis://localhost:16379"

    @pytest.mark.asyncio
    async def test_status_not_installed(self, tmp_path: Path) -> None:
        status = await get_falkordb_status(gobby_home=tmp_path)
        assert status["installed"] is False
        assert status["healthy"] is False
        assert status["url"] is None

    @pytest.mark.asyncio
    async def test_status_installed_but_unhealthy(self, tmp_path: Path) -> None:
        _seed_falkordb_config(tmp_path / "gobby-hub.db")

        with patch("gobby.cli.services.is_falkordb_healthy", new=AsyncMock(return_value=False)):
            status = await get_falkordb_status(gobby_home=tmp_path, password="secret")

        assert status["installed"] is True
        assert status["healthy"] is False


class TestEnsureLocalEmbeddingServiceReady:
    """Tests for local embedding readiness recovery."""

    @pytest.mark.asyncio
    async def test_starts_lmstudio_server_when_status_reports_down(
        self,
        mock_async_client: AsyncMock,
    ) -> None:
        mock_async_client.get = AsyncMock(
            side_effect=[httpx.ConnectError("refused"), httpx.Response(200)]
        )

        with (
            patch("gobby.cli.services.shutil.which", return_value="/usr/bin/lms"),
            patch("gobby.cli.services.asyncio.to_thread", side_effect=_run_inline),
            patch(
                "gobby.cli.services.subprocess.run",
                side_effect=[
                    _completed_process(["lms", "server", "status"], returncode=1, stderr="stopped"),
                    _completed_process(["lms", "server", "start"], stdout="started"),
                ],
            ) as mock_run,
            patch("gobby.cli.services.httpx.AsyncClient", return_value=mock_async_client),
            patch("gobby.cli.services.is_embedding_healthy", new=AsyncMock(return_value=True)),
        ):
            ready = await ensure_local_embedding_service_ready(
                model="nomic-embed-text",
                api_base="http://localhost:1234/v1",
            )

        assert ready is True
        assert [call.args[0] for call in mock_run.call_args_list] == [
            ["lms", "server", "status"],
            ["lms", "server", "start"],
        ]

    @pytest.mark.asyncio
    async def test_loads_lmstudio_model_only_after_failed_health_check(
        self,
        mock_async_client: AsyncMock,
    ) -> None:
        mock_async_client.get = AsyncMock(return_value=httpx.Response(200))
        mock_health = AsyncMock(side_effect=[False, True])

        with (
            patch("gobby.cli.services.shutil.which", return_value="/usr/bin/lms"),
            patch("gobby.cli.services.asyncio.to_thread", side_effect=_run_inline),
            patch(
                "gobby.cli.services.subprocess.run",
                side_effect=[
                    _completed_process(["lms", "server", "status"], stdout="running"),
                    _completed_process(["lms", "ps"], stdout=""),
                    _completed_process(["lms", "load", "model"], stdout="loaded"),
                ],
            ) as mock_run,
            patch("gobby.cli.services.httpx.AsyncClient", return_value=mock_async_client),
            patch("gobby.cli.services.is_embedding_healthy", new=mock_health),
        ):
            ready = await ensure_local_embedding_service_ready(
                model="text-embedding-nomic-embed-text-v1.5@f16",
                api_base="http://localhost:1234/v1",
            )

        assert ready is True
        assert [call.args[0] for call in mock_run.call_args_list] == [
            ["lms", "server", "status"],
            ["lms", "ps"],
            ["lms", "load", "text-embedding-nomic-embed-text-v1.5@f16", "-y"],
        ]
        assert mock_health.await_count == 2

    @pytest.mark.asyncio
    async def test_autoload_skips_lmstudio_load_when_model_is_already_loaded(self) -> None:
        with (
            patch("gobby.cli.services.shutil.which", return_value="/usr/bin/lms"),
            patch("gobby.cli.services.asyncio.to_thread", side_effect=_run_inline),
            patch(
                "gobby.cli.services.subprocess.run",
                return_value=_completed_process(
                    ["lms", "ps"],
                    stdout="text-embedding-nomic-embed-text-v1.5@f16",
                ),
            ) as mock_run,
        ):
            loaded = await try_autoload_embedding_model(
                model="text-embedding-nomic-embed-text-v1.5@f16",
                api_base="http://localhost:1234/v1",
            )

        assert loaded is True
        assert [call.args[0] for call in mock_run.call_args_list] == [["lms", "ps"]]

    @pytest.mark.asyncio
    async def test_returns_failure_on_server_start_error(self) -> None:
        with (
            patch("gobby.cli.services.shutil.which", return_value="/usr/bin/lms"),
            patch("gobby.cli.services.asyncio.to_thread", side_effect=_run_inline),
            patch(
                "gobby.cli.services.subprocess.run",
                side_effect=[
                    _completed_process(["lms", "server", "status"], returncode=1, stderr="stopped"),
                    _completed_process(["lms", "server", "start"], returncode=1, stderr="boom"),
                ],
            ),
        ):
            ready = await ensure_local_embedding_service_ready(
                model="nomic-embed-text",
                api_base="http://localhost:1234/v1",
            )

        assert ready is False
        assert get_local_embedding_service_failure_reason() == "LM Studio server start failed: boom"

    @pytest.mark.asyncio
    async def test_fails_when_models_never_become_ready(
        self,
        mock_async_client: AsyncMock,
    ) -> None:
        mock_async_client.get = AsyncMock(side_effect=httpx.ConnectError("refused"))

        with (
            patch("gobby.cli.services.shutil.which", return_value="/usr/bin/lms"),
            patch("gobby.cli.services.asyncio.to_thread", side_effect=_run_inline),
            patch(
                "gobby.cli.services.subprocess.run",
                return_value=_completed_process(
                    ["lms", "server", "status"],
                    stdout="running",
                ),
            ),
            patch("gobby.cli.services.httpx.AsyncClient", return_value=mock_async_client),
            patch("gobby.cli.services._LM_STUDIO_READINESS_TIMEOUT", 0.0),
        ):
            ready = await ensure_local_embedding_service_ready(
                model="nomic-embed-text",
                api_base="http://localhost:1234/v1",
            )

        assert ready is False
        assert (
            get_local_embedding_service_failure_reason()
            == "LM Studio readiness timed out at http://localhost:1234/v1"
        )

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "api_base",
        [
            "http://remote.example:1234/v1",
            "https://api.openai.com/v1",
        ],
    )
    async def test_skips_local_start_behavior_for_non_local_or_non_lmstudio_endpoints(
        self,
        api_base: str,
    ) -> None:
        mock_health = AsyncMock(return_value=True)

        with (
            patch("gobby.cli.services.is_embedding_healthy", new=mock_health),
            patch("gobby.cli.services.subprocess.run") as mock_run,
        ):
            ready = await ensure_local_embedding_service_ready(
                model="text-embedding-3-small",
                api_base=api_base,
                api_key="secret",
                expected_dim=1536,
            )

        assert ready is True
        mock_run.assert_not_called()
        mock_health.assert_awaited_once_with(
            model="text-embedding-3-small",
            api_base=api_base,
            api_key="secret",
            expected_dim=1536,
        )
