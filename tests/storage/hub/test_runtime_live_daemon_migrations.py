"""CLI hub openers never migrate a hub that a running daemon serves (#23313)."""

from __future__ import annotations

import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

from gobby.runner_pid_file import ProbeState, claim_pid_file, probe_daemon_lock
from gobby.storage.hub.postgres import PostgresHubDatabase
from gobby.storage.hub.runtime import runtime_hub_database
from tests.fixtures.fake_hub import FAKE_DATABASE_URL

pytestmark = pytest.mark.unit

_HOLD_DAEMON_LOCK = """
import sys
from pathlib import Path
from gobby.runner_pid_file import claim_pid_file
claim = claim_pid_file(Path(sys.argv[1]), role="daemon")
assert claim is not None
print("ready", flush=True)
sys.stdin.read()
"""


class _Hub:
    """Hub whose migration step is the real PostgresHubDatabase delegation."""

    _conninfo = FAKE_DATABASE_URL
    apply_migrations = PostgresHubDatabase.apply_migrations

    def close(self) -> None:
        return None


@pytest.fixture
def gobby_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("GOBBY_HOME", str(tmp_path))
    monkeypatch.delenv("GOBBY_MANAGED_EXECUTION_BOOTSTRAP", raising=False)
    return tmp_path


@pytest.fixture
def foreign_daemon(gobby_home: Path) -> Iterator[None]:
    """Hold this home's daemon singleton from another process, as a live daemon does."""
    pid_file = gobby_home / "gobby.pid"
    holder = subprocess.Popen(
        [sys.executable, "-c", _HOLD_DAEMON_LOCK, str(pid_file)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert holder.stdout is not None
        assert holder.stdout.readline().strip() == "ready"
        assert probe_daemon_lock(pid_file).state is ProbeState.DAEMON
        yield
    finally:
        holder.kill()
        holder.wait(timeout=10)


@pytest.fixture
def gdaemon_apply() -> Iterator[Mock]:
    """Stand in for an installed gdaemon whose pinned schema is newer than the hub."""
    config = SimpleNamespace(
        database_url=FAKE_DATABASE_URL,
        postgres_pool=None,
        datastore_mode="local",
    )
    with (
        patch("gobby.storage.schema_contract.apply_schema") as apply_schema,
        patch("gobby.config.bootstrap.load_bootstrap", return_value=config),
        patch("gobby.storage.hub.runtime.load_bootstrap", return_value=config),
        patch(
            "gobby.storage.hub.runtime.admitted_database_url",
            return_value=FAKE_DATABASE_URL,
        ),
        patch(
            "gobby.storage.hub.postgres.PostgresHubDatabase",
            side_effect=lambda *_args, **_kwargs: _Hub(),
        ),
        patch("gobby.storage.projects.ensure_personal_project"),
    ):
        yield apply_schema


def _open_runtime(config_file: str | None) -> object:
    with runtime_hub_database(config_file, apply_migrations=True) as db:
        return db


@pytest.mark.parametrize("explicit_bootstrap", [False, True])
def test_runtime_opener_skips_migrations_under_live_daemon(
    gobby_home: Path, foreign_daemon: None, gdaemon_apply: Mock, explicit_bootstrap: bool
) -> None:
    config_file = str(gobby_home / "bootstrap.yaml") if explicit_bootstrap else None

    opened = _open_runtime(config_file)

    assert isinstance(opened, _Hub)
    assert probe_daemon_lock(gobby_home / "gobby.pid").state is ProbeState.DAEMON
    gdaemon_apply.assert_not_called()


def test_init_local_storage_skips_migrations_under_live_daemon(
    gobby_home: Path, foreign_daemon: None, gdaemon_apply: Mock
) -> None:
    from gobby.cli.utils_config import init_local_storage

    opened = init_local_storage()
    opened.close()

    assert isinstance(opened, _Hub)
    assert probe_daemon_lock(gobby_home / "gobby.pid").state is ProbeState.DAEMON
    gdaemon_apply.assert_not_called()


@pytest.mark.parametrize("explicit_bootstrap", [False, True])
def test_runtime_opener_migrates_without_daemon(
    gobby_home: Path, gdaemon_apply: Mock, explicit_bootstrap: bool
) -> None:
    config_file = str(gobby_home / "bootstrap.yaml") if explicit_bootstrap else None

    _open_runtime(config_file)

    assert gdaemon_apply.call_args_list == [((FAKE_DATABASE_URL,), {})]


def test_init_local_storage_migrates_without_daemon(gobby_home: Path, gdaemon_apply: Mock) -> None:
    from gobby.cli.utils_config import init_local_storage

    init_local_storage().close()

    assert gdaemon_apply.call_args_list == [((FAKE_DATABASE_URL,), {})]


def test_start_path_migrates_under_its_own_daemon_claim(
    gobby_home: Path, gdaemon_apply: Mock
) -> None:
    """`gobby start` holds the daemon claim in-process before it migrates."""
    from gobby.cli.utils_config import init_local_storage

    claim = claim_pid_file(gobby_home / "gobby.pid", role="daemon")
    assert claim is not None
    try:
        init_local_storage().close()
    finally:
        claim.release()

    assert gdaemon_apply.call_args_list == [((FAKE_DATABASE_URL,), {})]
