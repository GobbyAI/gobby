"""Tests for FalkorDB service lifecycle helpers."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from gobby.storage.config_store import ConfigStore
from gobby.storage.database import LocalDatabase
from gobby.storage.migrations import run_migrations

pytestmark = pytest.mark.unit


def _seed_falkordb_config(db_path: Path) -> None:
    db = LocalDatabase(db_path)
    try:
        run_migrations(db)
        store = ConfigStore(db)
        store.set("databases.falkordb.host", "localhost", source="test")
        store.set("databases.falkordb.port", 6379, source="test")
    finally:
        db.close()


class TestIsFalkorDBInstalled:
    def test_uses_tmp_home_db_when_bootstrap_is_missing(
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

    def test_uses_bootstrap_database_path_when_present(self, tmp_path: Path) -> None:
        configured_db = tmp_path / "configured-hub.db"
        _seed_falkordb_config(configured_db)
        bootstrap = tmp_path / "bootstrap.yaml"
        bootstrap.write_text(f"database_path: {configured_db}\n", encoding="utf-8")
        bootstrap.chmod(0o600)

        from gobby.cli.services import is_falkordb_installed

        assert is_falkordb_installed(gobby_home=tmp_path) is True
        assert not (tmp_path / "gobby-hub.db").exists()

    def test_services_module_does_not_define_default_db_path_helper(self) -> None:
        import gobby.cli.services as services

        assert not hasattr(services, "_default_db_path")


class TestFalkorDBHealthAndStatus:
    @pytest.mark.asyncio
    async def test_health_uses_redis_ping_with_auth_when_password_present(self) -> None:
        from gobby.cli.services import is_falkordb_healthy

        completed = object()
        with patch("gobby.cli.services.asyncio.to_thread", return_value=completed) as to_thread:
            assert (
                await is_falkordb_healthy(
                    host="localhost",
                    port=6379,
                    password="secret",
                )
                is True
            )

        call_args = to_thread.call_args.args
        assert call_args[0].__name__ == "run"
        assert call_args[1] == [
            "redis-cli",
            "-h",
            "localhost",
            "-p",
            "6379",
            "-a",
            "secret",
            "PING",
        ]

    @pytest.mark.asyncio
    async def test_status_reports_installed_health_and_endpoint(self, tmp_path: Path) -> None:
        from gobby.cli.services import get_falkordb_status

        _seed_falkordb_config(tmp_path / "gobby-hub.db")

        with patch("gobby.cli.services.is_falkordb_healthy", new=AsyncMock(return_value=True)):
            status = await get_falkordb_status(gobby_home=tmp_path)

        assert status == {
            "installed": True,
            "healthy": True,
            "host": "localhost",
            "port": 6379,
        }
