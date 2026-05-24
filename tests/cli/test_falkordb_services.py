"""Tests for FalkorDB service lifecycle helpers."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from gobby.storage.config_store import ConfigStore
from gobby.storage.hub.protocol import HubDatabase

pytestmark = pytest.mark.unit


class _NonClosingDb:
    dialect = "postgres"

    def __init__(self, db: HubDatabase) -> None:
        self._db = db

    def __getattr__(self, name: str) -> object:
        return getattr(self._db, name)

    def close(self) -> None:
        pass


def _seed_falkordb_config(db: HubDatabase) -> None:
    store = ConfigStore(db)
    store.set("databases.falkordb.host", "localhost", source="test")
    store.set("databases.falkordb.port", 6379, source="test")


class _FakeRedisClient:
    def __init__(self) -> None:
        self.ping_count = 0
        self.close_count = 0

    async def ping(self) -> bool:
        self.ping_count += 1
        return True

    async def aclose(self) -> None:
        self.close_count += 1


class _RedisFactory:
    def __init__(self, client: _FakeRedisClient) -> None:
        self.client = client
        self.calls: list[dict[str, object]] = []

    def __call__(self, **kwargs: object) -> _FakeRedisClient:
        self.calls.append(kwargs)
        return self.client


class TestIsFalkorDBInstalled:
    def test_uses_runtime_hub_for_requested_home(
        self,
        tmp_path: Path,
        hub_db: HubDatabase,
    ) -> None:
        import gobby.cli.services as services

        _seed_falkordb_config(hub_db)
        proxy = _NonClosingDb(hub_db)
        with patch("gobby.cli.services._open_falkordb_config_db", return_value=proxy) as open_db:
            assert services.is_falkordb_installed(gobby_home=tmp_path) is True

        open_db.assert_called_once_with(tmp_path)

    def test_uses_default_gobby_home_when_home_missing(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        hub_db: HubDatabase,
    ) -> None:
        import gobby.cli.services as services

        _seed_falkordb_config(hub_db)
        proxy = _NonClosingDb(hub_db)
        monkeypatch.setattr(services, "get_gobby_home", lambda: tmp_path)
        with patch("gobby.cli.services._open_falkordb_config_db", return_value=proxy) as open_db:
            assert services.is_falkordb_installed() is True

        open_db.assert_called_once_with(None)

    def test_services_module_does_not_define_default_db_path_helper(self) -> None:
        import gobby.cli.services as services

        assert not hasattr(services, "_default_db_path")


class TestFalkorDBHealthAndStatus:
    @pytest.mark.asyncio
    async def test_health_uses_redis_ping_with_auth_when_password_present(self) -> None:
        from gobby.cli.services import is_falkordb_healthy

        client = _FakeRedisClient()
        redis_factory = _RedisFactory(client)
        redis_module = SimpleNamespace(Redis=redis_factory)

        with patch("gobby.cli.services.importlib.import_module", return_value=redis_module):
            assert (
                await is_falkordb_healthy(
                    host="localhost",
                    port=6379,
                    password="secret",
                )
                is True
            )

        assert redis_factory.calls == [
            {"host": "localhost", "port": 6379, "password": "secret", "socket_timeout": 5}
        ]
        assert client.ping_count == 1
        assert client.close_count == 1

    @pytest.mark.asyncio
    async def test_status_reports_installed_health_and_endpoint(
        self,
        tmp_path: Path,
        hub_db: HubDatabase,
    ) -> None:
        from gobby.cli.services import get_falkordb_status

        _seed_falkordb_config(hub_db)
        proxy = _NonClosingDb(hub_db)
        with (
            patch("gobby.cli.services._open_falkordb_config_db", return_value=proxy),
            patch("gobby.cli.services.is_falkordb_healthy", new=AsyncMock(return_value=True)),
        ):
            status = await get_falkordb_status(gobby_home=tmp_path)

        assert status == {
            "installed": True,
            "healthy": True,
            "url": "redis://localhost:6379",
        }
