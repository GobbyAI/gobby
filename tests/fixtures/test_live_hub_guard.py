"""Under test, connections to the live hub's coordinates are refused before any dial."""

from __future__ import annotations

import asyncio
import subprocess
from typing import Any

import psycopg
import pytest

from gobby.storage import schema_contract
from gobby.utils import spawn
from tests.fixtures.fake_hub import FAKE_DATABASE_URL, fake_database_url
from tests.fixtures.postgres import LiveHubConnectionRefused, _dsn_identity

pytestmark = pytest.mark.unit

_LIVE_URLS = (
    "postgresql://gobby:secret@localhost:60891/gobby",
    "postgresql://other@127.0.0.1:60891/gobby?connect_timeout=1",
)


def test_fake_database_url_is_off_the_live_hub() -> None:
    assert _dsn_identity(FAKE_DATABASE_URL) == ("localhost", "1", "gobby_fixture")
    assert fake_database_url("****") == "postgresql://gobby:****@localhost:1/gobby_fixture"


@pytest.mark.parametrize("url", _LIVE_URLS)
def test_sync_connect_refuses_the_live_hub(url: str) -> None:
    with pytest.raises(LiveHubConnectionRefused, match="live Gobby hub"):
        psycopg.connect(url)
    # Pools dial through the class method rather than the module alias.
    with pytest.raises(LiveHubConnectionRefused, match="live Gobby hub"):
        psycopg.Connection.connect(url, autocommit=True)


def test_sync_connect_refuses_the_live_hub_named_by_keywords() -> None:
    with pytest.raises(LiveHubConnectionRefused, match="live Gobby hub"):
        psycopg.connect(host="127.0.0.1", port=60891, dbname="gobby", connect_timeout=1)


def test_sync_connect_refuses_the_live_hub_named_by_libpq_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PGHOST", "127.0.0.1")
    monkeypatch.setenv("PGPORT", "60891")
    with pytest.raises(LiveHubConnectionRefused, match="live Gobby hub"):
        psycopg.connect("dbname=gobby connect_timeout=1")


@pytest.mark.parametrize("url", _LIVE_URLS)
def test_async_connect_refuses_the_live_hub(url: str) -> None:
    with pytest.raises(LiveHubConnectionRefused, match="live Gobby hub"):
        asyncio.run(psycopg.AsyncConnection.connect(url))


def test_connect_passes_other_databases_through() -> None:
    with pytest.raises(psycopg.OperationalError):
        psycopg.connect(FAKE_DATABASE_URL, connect_timeout=1)
    with pytest.raises(psycopg.OperationalError):
        asyncio.run(psycopg.AsyncConnection.connect(FAKE_DATABASE_URL, connect_timeout=1))


def test_isolated_test_hub_connects_through_the_guard(postgres_database_url: str) -> None:
    with psycopg.connect(postgres_database_url, connect_timeout=5) as conn:
        assert conn.execute("SELECT 1").fetchone() == (1,)


def _record_gdaemon_runs(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    dialed: list[str] = []

    def run(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        dialed.append(kwargs["env"][schema_contract.DATABASE_URL_ENV])
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    monkeypatch.setattr(schema_contract, "resolve_native_bin", lambda _name: "/bin/gdaemon")
    monkeypatch.setattr(spawn, "run", run)
    return dialed


def test_gdaemon_schema_runs_refuse_the_live_hub(monkeypatch: pytest.MonkeyPatch) -> None:
    dialed = _record_gdaemon_runs(monkeypatch)

    with pytest.raises(LiveHubConnectionRefused, match="live Gobby hub"):
        schema_contract.sweep_test_schemas(_LIVE_URLS[0], age_hours=1)

    schema_contract.sweep_test_schemas(FAKE_DATABASE_URL, age_hours=1)
    assert dialed == [FAKE_DATABASE_URL]
