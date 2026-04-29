"""Tests for service lifecycle utilities."""

import subprocess
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from gobby.cli.services import (
    ensure_local_embedding_service_ready,
    get_local_embedding_service_failure_reason,
    get_neo4j_status,
    is_neo4j_healthy,
    is_neo4j_installed,
)

pytestmark = pytest.mark.unit


class TestIsNeo4jInstalled:
    """Tests for is_neo4j_installed()."""

    def test_installed_when_dir_exists(self, tmp_path: Path) -> None:
        svc_dir = tmp_path / "services" / "neo4j"
        svc_dir.mkdir(parents=True)
        assert is_neo4j_installed(gobby_home=tmp_path) is True

    def test_not_installed_when_dir_missing(self, tmp_path: Path) -> None:
        assert is_neo4j_installed(gobby_home=tmp_path) is False


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


class TestIsNeo4jHealthy:
    """Tests for is_neo4j_healthy()."""

    @pytest.mark.asyncio
    async def test_healthy_when_reachable(self, mock_async_client: AsyncMock) -> None:
        mock_async_client.get = AsyncMock(return_value=httpx.Response(200))
        with patch("gobby.cli.services.httpx.AsyncClient", return_value=mock_async_client):
            assert await is_neo4j_healthy("http://localhost:8474") is True

    @pytest.mark.asyncio
    async def test_unhealthy_when_unreachable(self, mock_async_client: AsyncMock) -> None:
        mock_async_client.get = AsyncMock(side_effect=httpx.ConnectError("refused"))
        with patch("gobby.cli.services.httpx.AsyncClient", return_value=mock_async_client):
            assert await is_neo4j_healthy("http://localhost:8474") is False

    @pytest.mark.asyncio
    async def test_unhealthy_when_server_error(self, mock_async_client: AsyncMock) -> None:
        mock_async_client.get = AsyncMock(return_value=httpx.Response(500))
        with patch("gobby.cli.services.httpx.AsyncClient", return_value=mock_async_client):
            assert await is_neo4j_healthy("http://localhost:8474") is False

    @pytest.mark.asyncio
    async def test_unhealthy_when_no_url(self) -> None:
        assert await is_neo4j_healthy(None) is False


class TestGetNeo4jStatus:
    """Tests for get_neo4j_status()."""

    @pytest.mark.asyncio
    async def test_status_installed_and_healthy(
        self, tmp_path: Path, mock_async_client: AsyncMock
    ) -> None:
        svc_dir = tmp_path / "services" / "neo4j"
        svc_dir.mkdir(parents=True)
        mock_async_client.get = AsyncMock(return_value=httpx.Response(200))
        with patch("gobby.cli.services.httpx.AsyncClient", return_value=mock_async_client):
            status = await get_neo4j_status(gobby_home=tmp_path, neo4j_url="http://localhost:8474")
        assert status["installed"] is True
        assert status["healthy"] is True
        assert status["url"] == "http://localhost:8474"

    @pytest.mark.asyncio
    async def test_status_not_installed(self, tmp_path: Path) -> None:
        status = await get_neo4j_status(gobby_home=tmp_path, neo4j_url=None)
        assert status["installed"] is False
        assert status["healthy"] is False
        assert status["url"] is None

    @pytest.mark.asyncio
    async def test_status_installed_but_unhealthy(
        self, tmp_path: Path, mock_async_client: AsyncMock
    ) -> None:
        svc_dir = tmp_path / "services" / "neo4j"
        svc_dir.mkdir(parents=True)
        mock_async_client.get = AsyncMock(side_effect=httpx.ConnectError("refused"))
        with patch("gobby.cli.services.httpx.AsyncClient", return_value=mock_async_client):
            status = await get_neo4j_status(gobby_home=tmp_path, neo4j_url="http://localhost:8474")
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
                    _completed_process(["lms", "load", "model"], stdout="loaded"),
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
            ["lms", "load", "nomic-embed-text-v1.5", "-y"],
        ]

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
