"""Under test, connections to the live hub's coordinates are refused before any dial."""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import psycopg
import pytest

from gobby.storage import schema_contract
from gobby.utils import spawn
from tests.fixtures import postgres
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


@pytest.mark.parametrize(
    "operator_url,test_url,exit_code",
    [
        (
            "host=localhost port=60951 dbname=operator",
            "host=127.0.0.1 port=60951 dbname=operator",
            4,
        ),
        (
            "host=hub.example.invalid port=5432 dbname=operator",
            "host=hub.example.invalid port=5432 dbname=operator",
            4,
        ),
        ("", _LIVE_URLS[0], 4),
        (
            "host=localhost port=60951 dbname=operator",
            "host=127.0.0.1 port=60892 dbname=gobby_test",
            0,
        ),
    ],
    ids=["custom-loopback", "remote", "default", "isolated-test-hub"],
)
def test_pytest_configure_guards_operator_identity_before_collection(
    tmp_path: Path, operator_url: str, test_url: str, exit_code: int
) -> None:
    home = tmp_path / "operator"
    bootstrap = home / ".gobby" / "bootstrap.yaml"
    bootstrap.parent.mkdir(parents=True)
    bootstrap.write_text("unreadable: [\n", encoding="utf-8")
    env = os.environ.copy()
    env.update(
        HOME=str(home),
        GOBBY_HOME=str(bootstrap.parent),
        DATABASE_URL=test_url,
        GOBBY_TEST_OPERATOR_DATABASE_URL=operator_url,
        GOBBY_TEST_PROTECT="1",
        PYTEST_DISABLE_PLUGIN_AUTOLOAD="1",
    )
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-p",
            "xdist.plugin",
            "-p",
            "pytest_asyncio.plugin",
            "tests/fixtures/test_live_hub_guard.py::test_fake_database_url_is_off_the_live_hub",
            "tests/fixtures/test_live_hub_guard.py::test_supplied_operator_identity_stays_guarded",
            "-q",
        ],
        cwd=Path(__file__).resolve().parents[2],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == exit_code, result.stdout + result.stderr
    if exit_code:
        assert "DATABASE_URL points at the live Gobby hub" in result.stderr
    else:
        assert "2 passed" in result.stdout
    assert bootstrap.read_text(encoding="utf-8") == "unreadable: [\n"


def test_supplied_operator_identity_stays_guarded(monkeypatch: pytest.MonkeyPatch) -> None:
    operator_url = os.environ.get("GOBBY_TEST_OPERATOR_DATABASE_URL")
    if not operator_url:
        pytest.skip("exercised in the configured child pytest")
    monkeypatch.setenv("GOBBY_TEST_OPERATOR_DATABASE_URL", FAKE_DATABASE_URL)
    assert postgres._live_hub_identity() == _dsn_identity(operator_url)
    with pytest.raises(LiveHubConnectionRefused, match="live Gobby hub"):
        postgres._refuse_live_hub(operator_url, {})


def test_invalid_supplied_operator_identity_aborts(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(postgres, "_OPERATOR_HUB_DATABASE_URL", "invalid conninfo")
    with pytest.raises(pytest.UsageError, match="must be valid PostgreSQL conninfo"):
        postgres._live_hub_identity()


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
