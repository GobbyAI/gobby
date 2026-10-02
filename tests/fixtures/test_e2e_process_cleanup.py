"""Process cleanup regressions kept outside the e2e autouse-fixture subtree."""

import inspect
import os
from collections.abc import Callable, Generator
from dataclasses import dataclass, field
from pathlib import Path
from typing import cast

import psutil
import pytest

from tests.e2e import conftest as e2e_fixtures


@dataclass
class FakeRunner:
    pid: int = 901
    argv: list[str] = field(
        default_factory=lambda: [
            "python",
            "-m",
            "gobby.runner",
            "--config",
            "/tmp/gobby_e2e_foreign/config.yaml",
        ]
    )
    env: dict[str, str] = field(
        default_factory=lambda: {
            "GOBBY_E2E_OWNER_PID": "123",
            "GOBBY_E2E_OWNER_CREATE_TIME": "120.0",
        }
    )
    alive: bool = True
    termination_requested: bool = False
    killed: bool = False
    termination_timeout: bool = False
    environ_error: psutil.Error | None = None
    environment_reads: int = 0

    def cmdline(self) -> list[str]:
        return self.argv

    def environ(self) -> dict[str, str]:
        self.environment_reads += 1
        if self.environ_error is not None:
            raise self.environ_error
        return self.env

    def terminate(self) -> None:
        self.termination_requested = True

    def wait(self, timeout: float) -> int:
        if self.termination_timeout:
            raise psutil.TimeoutExpired(timeout, pid=self.pid)
        self.alive = False
        return 0

    def kill(self) -> None:
        self.killed = True
        self.alive = False


@dataclass
class FakeOwner:
    pid: int = 123
    created: float = 120.0
    running: bool = True
    state: str = psutil.STATUS_RUNNING

    def create_time(self) -> float:
        return self.created

    def is_running(self) -> bool:
        return self.running

    def status(self) -> str:
        return self.state


@pytest.fixture
def runner(monkeypatch: pytest.MonkeyPatch) -> FakeRunner:
    process = FakeRunner()
    monkeypatch.setattr(psutil, "process_iter", lambda _attrs: iter([process]))
    return process


def _stub_owner(monkeypatch: pytest.MonkeyPatch, owner: FakeOwner | Exception) -> list[int]:
    lookups: list[int] = []

    def lookup(pid: int) -> FakeOwner:
        lookups.append(pid)
        if isinstance(owner, Exception):
            raise owner
        return owner

    monkeypatch.setattr(psutil, "Process", lookup)
    return lookups


def test_live_foreign_owner_survives_cleanup_fixture_setup_and_teardown(
    monkeypatch: pytest.MonkeyPatch, runner: FakeRunner
) -> None:
    _stub_owner(monkeypatch, FakeOwner())
    cleanup = cast(
        Callable[[], Generator[None]], inspect.unwrap(e2e_fixtures.cleanup_orphan_processes)
    )()

    next(cleanup)
    assert runner.alive
    with pytest.raises(StopIteration):
        next(cleanup)

    assert runner.alive
    assert not runner.termination_requested
    assert not runner.killed


def test_runner_with_exited_test_owner_is_reaped(
    monkeypatch: pytest.MonkeyPatch, runner: FakeRunner
) -> None:
    _stub_owner(monkeypatch, psutil.NoSuchProcess(123))

    e2e_fixtures._cleanup_orphan_gobby_processes()

    assert runner.termination_requested
    assert not runner.alive
    assert not runner.killed


def test_runner_without_e2e_marker_is_never_touched(
    monkeypatch: pytest.MonkeyPatch, runner: FakeRunner
) -> None:
    runner.argv = ["python", "-m", "gobby.runner", "--config", "/home/config"]
    lookups = _stub_owner(monkeypatch, psutil.NoSuchProcess(123))

    e2e_fixtures._cleanup_orphan_gobby_processes()

    assert runner.alive
    assert not runner.termination_requested
    assert not runner.killed
    assert runner.environment_reads == 0
    assert lookups == []


@pytest.mark.parametrize(
    "owner_env",
    [
        {},
        {"GOBBY_E2E_OWNER_PID": "123"},
        {"GOBBY_E2E_OWNER_PID": "bad", "GOBBY_E2E_OWNER_CREATE_TIME": "120.0"},
        {"GOBBY_E2E_OWNER_PID": "0", "GOBBY_E2E_OWNER_CREATE_TIME": "120.0"},
        {"GOBBY_E2E_OWNER_PID": "123", "GOBBY_E2E_OWNER_CREATE_TIME": "bad"},
        {"GOBBY_E2E_OWNER_PID": "123", "GOBBY_E2E_OWNER_CREATE_TIME": "0"},
        {"GOBBY_E2E_OWNER_PID": "123", "GOBBY_E2E_OWNER_CREATE_TIME": "nan"},
        {"GOBBY_E2E_OWNER_PID": "123", "GOBBY_E2E_OWNER_CREATE_TIME": "inf"},
    ],
)
def test_runner_with_unknown_owner_identity_is_preserved(
    monkeypatch: pytest.MonkeyPatch, runner: FakeRunner, owner_env: dict[str, str]
) -> None:
    runner.env = owner_env
    _stub_owner(monkeypatch, psutil.NoSuchProcess(123))

    e2e_fixtures._cleanup_orphan_gobby_processes()

    assert runner.alive
    assert not runner.termination_requested
    assert not runner.killed


@pytest.mark.parametrize("denied_at", ["runner", "owner"])
def test_inaccessible_owner_identity_is_preserved(
    monkeypatch: pytest.MonkeyPatch, runner: FakeRunner, denied_at: str
) -> None:
    _stub_owner(monkeypatch, psutil.AccessDenied(123))
    if denied_at == "runner":
        runner.environ_error = psutil.AccessDenied(runner.pid)

    e2e_fixtures._cleanup_orphan_gobby_processes()

    assert runner.alive
    assert not runner.termination_requested
    assert not runner.killed


@pytest.mark.parametrize("created", [118.0, 119.0, 121.0, 122.0])
def test_live_owner_with_create_time_drift_is_preserved(
    monkeypatch: pytest.MonkeyPatch, runner: FakeRunner, created: float
) -> None:
    _stub_owner(monkeypatch, FakeOwner(created=created))

    e2e_fixtures._cleanup_orphan_gobby_processes()

    assert runner.alive
    assert not runner.termination_requested
    assert not runner.killed


def test_reused_owner_pid_does_not_hide_an_orphan(
    monkeypatch: pytest.MonkeyPatch, runner: FakeRunner
) -> None:
    _stub_owner(monkeypatch, FakeOwner(created=130.0))

    e2e_fixtures._cleanup_orphan_gobby_processes()

    assert not runner.alive
    assert runner.termination_requested


@pytest.mark.parametrize(
    "running,status",
    [(False, psutil.STATUS_RUNNING), (True, psutil.STATUS_ZOMBIE), (True, psutil.STATUS_DEAD)],
)
def test_exited_owner_with_process_entry_is_reaped(
    monkeypatch: pytest.MonkeyPatch, runner: FakeRunner, running: bool, status: str
) -> None:
    _stub_owner(monkeypatch, FakeOwner(running=running, state=status))

    e2e_fixtures._cleanup_orphan_gobby_processes()

    assert not runner.alive
    assert runner.termination_requested


def test_orphan_that_ignores_termination_is_killed(
    monkeypatch: pytest.MonkeyPatch, runner: FakeRunner
) -> None:
    _stub_owner(monkeypatch, psutil.NoSuchProcess(123))
    runner.termination_timeout = True

    e2e_fixtures._cleanup_orphan_gobby_processes()

    assert not runner.alive
    assert runner.termination_requested
    assert runner.killed


def test_prepare_daemon_env_records_current_test_owner(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, runner: FakeRunner
) -> None:
    lookups = _stub_owner(monkeypatch, FakeOwner(pid=os.getpid()))
    monkeypatch.setattr("tests.fixtures.gdaemon_binary.select_test_gdaemon", lambda *_args: None)

    env = e2e_fixtures.prepare_daemon_env(
        {"GOBBY_E2E_OWNER_PID": "456", "GOBBY_E2E_OWNER_CREATE_TIME": "100.0"},
        home_dir=tmp_path,
    )

    assert env["GOBBY_E2E_OWNER_PID"] == str(os.getpid())
    assert env["GOBBY_E2E_OWNER_CREATE_TIME"] == "120.0"
    assert lookups == [os.getpid()]
    assert runner.alive
    assert not runner.termination_requested
    assert not runner.killed
