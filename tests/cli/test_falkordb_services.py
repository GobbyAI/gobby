"""Tests for FalkorDB service lifecycle helpers."""

from __future__ import annotations

from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock, patch

import pytest

from gobby.storage.config_mutations import ConfigMutations, ConfigPatch, SecretUpdate
from gobby.storage.hub.protocol import HubDatabase

pytestmark = pytest.mark.unit


class _NonClosingDb:
    dialect = "postgres"

    def __init__(self, db: HubDatabase) -> None:
        self._db = db
        self.close_count = 0

    def __setattr__(self, name: str, value: object) -> None:
        if name in {"_db", "close_count"}:
            object.__setattr__(self, name, value)
            return
        setattr(self._db, name, value)

    def __getattr__(self, name: str) -> object:
        return getattr(self._db, name)

    def close(self) -> None:
        self.close_count += 1


def _seed_falkordb_config(db: HubDatabase) -> None:
    ConfigMutations(db).patch_internal(
        expected_revision=0,
        patch=ConfigPatch(
            values={
                "databases.falkordb.host": "localhost",
                "databases.falkordb.port": 6379,
            },
            secrets={"databases.falkordb.password": SecretUpdate("secret")},
        ),
        source="test",
    )


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
    def test_uses_injected_database(self, hub_db: HubDatabase) -> None:
        import gobby.cli.services as services

        _seed_falkordb_config(hub_db)
        proxy = _NonClosingDb(hub_db)
        assert services.is_falkordb_installed(db=cast(HubDatabase, proxy)) is True

        assert proxy.close_count == 0

    def test_does_not_close_injected_database(self, hub_db: HubDatabase) -> None:
        import gobby.cli.services as services

        _seed_falkordb_config(hub_db)
        proxy = _NonClosingDb(hub_db)
        assert services.is_falkordb_installed(db=cast(HubDatabase, proxy)) is True

        assert proxy.close_count == 0

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
        hub_db: HubDatabase,
    ) -> None:
        from gobby.cli.services import get_falkordb_status

        _seed_falkordb_config(hub_db)
        proxy = _NonClosingDb(hub_db)
        with (
            patch("gobby.cli.services.is_falkordb_healthy", new=AsyncMock(return_value=True)),
        ):
            status = await get_falkordb_status(db=cast(HubDatabase, proxy))

        assert status == {
            "installed": True,
            "healthy": True,
            "url": "redis://localhost:6379",
        }
