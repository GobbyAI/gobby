"""CLI hub openers never migrate a hub that a running daemon serves (#23313)."""

from __future__ import annotations

import subprocess
import sys
from collections.abc import Callable, Iterator
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

from gobby.runner_pid_file import (
    ProbeState,
    SingletonOpenError,
    claim_pid_file,
    held_singleton_claim,
    probe_daemon_lock,
)
from gobby.storage.hub.postgres import PostgresHubDatabase
from gobby.storage.hub.runtime import runtime_hub_database
from tests.fixtures.fake_hub import FAKE_DATABASE_URL

pytestmark = pytest.mark.unit

_HOLD_LOCK = """
import sys
from pathlib import Path
from gobby.runner_pid_file import claim_pid_file
claim = claim_pid_file(Path(sys.argv[1]), role=sys.argv[2])
assert claim is not None
print("ready", flush=True)
sys.stdin.read()
"""

_TRY_DAEMON_CLAIM = """
import sys
from pathlib import Path
from gobby.runner_pid_file import claim_pid_file
claim = claim_pid_file(Path(sys.argv[1]), role="daemon")
print("refused" if claim is None else "claimed", flush=True)
"""


class _Hub:
    """Hub whose migration step is the real PostgresHubDatabase delegation."""

    _conninfo = FAKE_DATABASE_URL
    apply_migrations = PostgresHubDatabase.apply_migrations

    def close(self) -> None:
        return None


def _hold_lock(pid_file: Path, role: str) -> subprocess.Popen[str]:
    holder = subprocess.Popen(
        [sys.executable, "-c", _HOLD_LOCK, str(pid_file), role],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
    )
    assert holder.stdout is not None
    assert holder.stdout.readline().strip() == "ready"
    return holder


def _daemon_claim_from_another_process(pid_file: Path) -> str:
    """Attempt a daemon start's singleton claim from a separate process."""
    completed = subprocess.run(
        [sys.executable, "-c", _TRY_DAEMON_CLAIM, str(pid_file)],
        capture_output=True,
        text=True,
        check=True,
        timeout=60,
    )
    return completed.stdout.strip()


@pytest.fixture
def gobby_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("GOBBY_HOME", str(tmp_path))
    monkeypatch.delenv("GOBBY_MANAGED_EXECUTION_BOOTSTRAP", raising=False)
    return tmp_path


@pytest.fixture
def foreign_daemon(gobby_home: Path) -> Iterator[None]:
    """Hold this home's daemon singleton from another process, as a live daemon does."""
    pid_file = gobby_home / "gobby.pid"
    holder = _hold_lock(pid_file, "daemon")
    try:
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


def _open_default_runtime() -> object:
    return _open_runtime(None)


def _open_init_local_storage() -> object:
    from gobby.cli.utils_config import init_local_storage

    db = init_local_storage()
    db.close()
    return db


_OPENERS: list[Callable[[], object]] = [_open_default_runtime, _open_init_local_storage]


_BOOTSTRAP_LOCATIONS = ["default", "in_home", "outside_home"]


def _config_file(
    location: str, gobby_home: Path, tmp_path_factory: pytest.TempPathFactory
) -> str | None:
    """Bootstrap path as `gobby --config` passes it; GOBBY_HOME stays the daemon home."""
    if location == "default":
        return None
    if location == "in_home":
        return str(gobby_home / "bootstrap.yaml")
    return str(tmp_path_factory.mktemp("elsewhere") / "bootstrap.yaml")


@pytest.mark.parametrize("bootstrap_location", _BOOTSTRAP_LOCATIONS)
def test_runtime_opener_skips_migrations_under_live_daemon(
    gobby_home: Path,
    foreign_daemon: None,
    gdaemon_apply: Mock,
    bootstrap_location: str,
    tmp_path_factory: pytest.TempPathFactory,
) -> None:
    config_file = _config_file(bootstrap_location, gobby_home, tmp_path_factory)

    opened = _open_runtime(config_file)

    assert isinstance(opened, _Hub)
    assert probe_daemon_lock(gobby_home / "gobby.pid").state is ProbeState.DAEMON
    gdaemon_apply.assert_not_called()


def test_init_local_storage_skips_migrations_under_live_daemon(
    gobby_home: Path, foreign_daemon: None, gdaemon_apply: Mock
) -> None:
    opened = _open_init_local_storage()

    assert isinstance(opened, _Hub)
    assert probe_daemon_lock(gobby_home / "gobby.pid").state is ProbeState.DAEMON
    gdaemon_apply.assert_not_called()


@pytest.mark.parametrize("bootstrap_location", _BOOTSTRAP_LOCATIONS)
def test_runtime_opener_migrates_without_daemon(
    gobby_home: Path,
    gdaemon_apply: Mock,
    bootstrap_location: str,
    tmp_path_factory: pytest.TempPathFactory,
) -> None:
    config_file = _config_file(bootstrap_location, gobby_home, tmp_path_factory)

    _open_runtime(config_file)

    assert gdaemon_apply.call_args_list == [((FAKE_DATABASE_URL,), {})]


def test_init_local_storage_migrates_without_daemon(gobby_home: Path, gdaemon_apply: Mock) -> None:
    _open_init_local_storage()

    assert gdaemon_apply.call_args_list == [((FAKE_DATABASE_URL,), {})]


def test_start_path_migrates_under_its_own_daemon_claim(
    gobby_home: Path, gdaemon_apply: Mock
) -> None:
    """`gobby start` holds the daemon claim in-process before it migrates."""
    claim = claim_pid_file(gobby_home / "gobby.pid", role="daemon")
    assert claim is not None
    try:
        _open_init_local_storage()
        assert held_singleton_claim() is claim
    finally:
        claim.release()

    assert gdaemon_apply.call_args_list == [((FAKE_DATABASE_URL,), {})]


@pytest.mark.parametrize("opener", _OPENERS)
def test_daemon_cannot_start_while_cli_migrates(
    gobby_home: Path, gdaemon_apply: Mock, opener: Callable[[], object]
) -> None:
    """The singleton stays held from before the schema apply until it finishes."""
    pid_file = gobby_home / "gobby.pid"
    during_apply: list[str] = []
    gdaemon_apply.side_effect = lambda _conninfo: during_apply.append(
        _daemon_claim_from_another_process(pid_file)
    )

    opener()

    assert during_apply == ["refused"]
    assert held_singleton_claim() is None
    assert _daemon_claim_from_another_process(pid_file) == "claimed"


@pytest.mark.parametrize("opener", _OPENERS)
def test_failed_migration_releases_the_claim(
    gobby_home: Path, gdaemon_apply: Mock, opener: Callable[[], object]
) -> None:
    gdaemon_apply.side_effect = RuntimeError("schema apply failed")

    with pytest.raises(RuntimeError, match="schema apply failed"):
        opener()

    assert held_singleton_claim() is None
    assert probe_daemon_lock(gobby_home / "gobby.pid").state is ProbeState.ABSENT
    assert _daemon_claim_from_another_process(gobby_home / "gobby.pid") == "claimed"


def test_crashed_cli_claim_does_not_block_daemon_start(gobby_home: Path) -> None:
    """A CLI killed mid-migration leaves a stale record that a daemon start reclaims."""
    pid_file = gobby_home / "gobby.pid"
    holder = _hold_lock(pid_file, "maintenance")
    assert probe_daemon_lock(pid_file).state is ProbeState.MAINTENANCE
    holder.kill()
    holder.wait(timeout=10)

    assert not pid_file.exists()
    assert probe_daemon_lock(pid_file).state is ProbeState.ABSENT
    assert _daemon_claim_from_another_process(pid_file) == "claimed"


@pytest.mark.parametrize("opener", _OPENERS)
def test_unwritable_singleton_skips_migrations(
    gobby_home: Path, gdaemon_apply: Mock, opener: Callable[[], object]
) -> None:
    """A sandboxed CLI that cannot take the singleton opens the hub without migrating."""
    with patch(
        "gobby.runner_pid_file.claim_pid_file",
        side_effect=SingletonOpenError("sandbox denies writing gobby.pid.lock"),
    ):
        opened = opener()

    assert isinstance(opened, _Hub)
    gdaemon_apply.assert_not_called()
