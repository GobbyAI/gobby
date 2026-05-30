"""Tests for embedding availability helpers.

Covers both the cheap config check (``is_embedding_configured``) and the
real endpoint probe (``is_embedding_reachable``), including its cache.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from gobby.search.embeddings import (
    _REACHABILITY_CACHE_MAX_SIZE,
    _clear_reachability_cache,
    _reachability_cache,
    is_embedding_configured,
    is_embedding_reachable,
)

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _clean_reachability_cache() -> Any:
    _clear_reachability_cache()
    yield
    _clear_reachability_cache()


class TestIsEmbeddingConfigured:
    """Pure-configuration checks — never touch the network."""

    def test_nothing_configured(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        assert is_embedding_configured() is False

    def test_api_base_only(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        assert is_embedding_configured(api_base="http://localhost:11434/v1") is True

    def test_explicit_key(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        assert is_embedding_configured(model="text-embedding-3-small", api_key="sk-test") is True

    def test_env_key_does_not_configure_default_local_model(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("OPENAI_API_KEY", "sk-env")
        assert is_embedding_configured() is False

    def test_empty_explicit_key_does_not_fall_back_to_env(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("OPENAI_API_KEY", "sk-env")
        assert is_embedding_configured(model="text-embedding-3-small", api_key="") is False

    def test_empty_key_no_env_is_false(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        assert is_embedding_configured(api_key="") is False


def _mock_httpx_client(
    *,
    status: int | None = None,
    raise_exc: BaseException | None = None,
) -> Any:
    """Build a patch target that stands in for ``httpx.AsyncClient(...)``.

    The returned MagicMock is used as the new value of
    ``gobby.search.embeddings.httpx.AsyncClient`` — calling it returns an
    async context manager whose ``get`` either raises or yields a
    response with the given status.
    """
    response = MagicMock()
    if status is not None:
        response.status_code = status

    client_instance = MagicMock()
    if raise_exc is not None:
        client_instance.get = AsyncMock(side_effect=raise_exc)
    else:
        client_instance.get = AsyncMock(return_value=response)

    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=client_instance)
    cm.__aexit__ = AsyncMock(return_value=None)

    factory = MagicMock(return_value=cm)
    return factory, client_instance


class TestIsEmbeddingReachable:
    """Probe behavior — real network is mocked."""

    @pytest.mark.asyncio
    async def test_probe_uses_shared_lock_for_cache_read_and_write(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        class TrackingLock:
            def __init__(self) -> None:
                self.enter_count = 0

            def __enter__(self) -> TrackingLock:
                self.enter_count += 1
                return self

            def __exit__(self, exc_type, exc, tb) -> bool:
                return False

        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        lock = TrackingLock()
        factory, _client = _mock_httpx_client(status=200)

        with (
            patch("gobby.search.embeddings._get_lock", return_value=lock),
            patch("gobby.search.embeddings.httpx.AsyncClient", factory),
        ):
            assert await is_embedding_reachable(api_base="http://localhost:11434/v1") is True

        assert lock.enter_count == 2

    def test_clear_reachability_cache_uses_shared_lock(self) -> None:
        class TrackingLock:
            def __init__(self) -> None:
                self.enter_count = 0

            def __enter__(self) -> TrackingLock:
                self.enter_count += 1
                return self

            def __exit__(self, exc_type, exc, tb) -> bool:
                return False

        lock = TrackingLock()
        _reachability_cache[("http://localhost:11434/v1", False)] = MagicMock()

        with patch("gobby.search.embeddings._get_lock", return_value=lock):
            _clear_reachability_cache()

        assert lock.enter_count == 1
        assert _reachability_cache == {}

    @pytest.mark.asyncio
    async def test_not_configured_short_circuits(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        factory, client = _mock_httpx_client(status=200)
        with patch("gobby.search.embeddings.httpx.AsyncClient", factory):
            assert await is_embedding_reachable() is False
        assert client.get.await_count == 0

    @pytest.mark.asyncio
    async def test_reachable_200(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        factory, client = _mock_httpx_client(status=200)
        with patch("gobby.search.embeddings.httpx.AsyncClient", factory):
            assert await is_embedding_reachable(api_base="http://localhost:11434/v1") is True
        client.get.assert_awaited_once()
        called_url = client.get.await_args.args[0]
        assert called_url == "http://localhost:11434/v1/models"

    @pytest.mark.asyncio
    async def test_trailing_slash_in_api_base_is_stripped(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        factory, client = _mock_httpx_client(status=200)
        with patch("gobby.search.embeddings.httpx.AsyncClient", factory):
            await is_embedding_reachable(api_base="http://localhost:11434/v1/")
        assert client.get.await_args.args[0] == "http://localhost:11434/v1/models"

    @pytest.mark.asyncio
    async def test_non_2xx_is_unreachable(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        factory, _ = _mock_httpx_client(status=500)
        with patch("gobby.search.embeddings.httpx.AsyncClient", factory):
            assert await is_embedding_reachable(api_base="http://localhost:11434/v1") is False

    @pytest.mark.asyncio
    async def test_connect_error_is_unreachable(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        factory, _ = _mock_httpx_client(raise_exc=httpx.ConnectError("refused"))
        with patch("gobby.search.embeddings.httpx.AsyncClient", factory):
            assert await is_embedding_reachable(api_base="http://127.0.0.1:1") is False

    @pytest.mark.asyncio
    async def test_timeout_is_unreachable(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        factory, _ = _mock_httpx_client(raise_exc=httpx.ReadTimeout("slow"))
        with patch("gobby.search.embeddings.httpx.AsyncClient", factory):
            assert await is_embedding_reachable(api_base="http://localhost:11434/v1") is False

    @pytest.mark.asyncio
    async def test_api_key_sent_as_bearer(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        factory, client = _mock_httpx_client(status=200)
        with patch("gobby.search.embeddings.httpx.AsyncClient", factory):
            await is_embedding_reachable(
                model="text-embedding-3-small",
                api_key="sk-secret",
            )
        headers = client.get.await_args.kwargs["headers"]
        assert headers["Authorization"] == "Bearer sk-secret"

    @pytest.mark.asyncio
    async def test_openai_cloud_default_base(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """No api_base + key present → probes OpenAI cloud."""
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        factory, client = _mock_httpx_client(status=200)
        with patch("gobby.search.embeddings.httpx.AsyncClient", factory):
            await is_embedding_reachable(
                model="text-embedding-3-small",
                api_key="sk-test",
            )
        assert client.get.await_args.args[0] == "https://api.openai.com/v1/models"

    @pytest.mark.asyncio
    async def test_cache_hit_skips_probe(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        factory, client = _mock_httpx_client(status=200)
        with patch("gobby.search.embeddings.httpx.AsyncClient", factory):
            a = await is_embedding_reachable(api_base="http://localhost:11434/v1")
            b = await is_embedding_reachable(api_base="http://localhost:11434/v1")
        assert a is True and b is True
        # Second call served from cache → only one HTTP request fired.
        assert client.get.await_count == 1

    @pytest.mark.asyncio
    async def test_cache_expires_after_ttl(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        factory, client = _mock_httpx_client(status=200)
        with patch("gobby.search.embeddings.httpx.AsyncClient", factory):
            await is_embedding_reachable(api_base="http://localhost:11434/v1", cache_ttl=0.0)
            await is_embedding_reachable(api_base="http://localhost:11434/v1", cache_ttl=0.0)
        # cache_ttl=0 forces re-probe each time.
        assert client.get.await_count == 2

    @pytest.mark.asyncio
    async def test_cache_key_distinguishes_endpoints(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        factory, client = _mock_httpx_client(status=200)
        with patch("gobby.search.embeddings.httpx.AsyncClient", factory):
            await is_embedding_reachable(api_base="http://a:1234/v1")
            await is_embedding_reachable(api_base="http://b:1234/v1")
        assert client.get.await_count == 2

    @pytest.mark.asyncio
    async def test_failure_is_cached_too(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A negative probe shouldn't be re-fired within the TTL."""
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        factory, client = _mock_httpx_client(raise_exc=httpx.ConnectError("refused"))
        with patch("gobby.search.embeddings.httpx.AsyncClient", factory):
            a = await is_embedding_reachable(api_base="http://127.0.0.1:1")
            b = await is_embedding_reachable(api_base="http://127.0.0.1:1")
        assert a is False and b is False
        assert client.get.await_count == 1

    @pytest.mark.asyncio
    async def test_reachability_cache_prunes_oldest_entries(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        factory, _client = _mock_httpx_client(status=200)

        with patch("gobby.search.embeddings.httpx.AsyncClient", factory):
            for index in range(_REACHABILITY_CACHE_MAX_SIZE + 5):
                await is_embedding_reachable(api_base=f"http://host-{index}:11434/v1")

        assert len(_reachability_cache) == _REACHABILITY_CACHE_MAX_SIZE
        assert ("http://host-0:11434/v1", False) not in _reachability_cache
        assert (
            f"http://host-{_REACHABILITY_CACHE_MAX_SIZE + 4}:11434/v1",
            False,
        ) in _reachability_cache
