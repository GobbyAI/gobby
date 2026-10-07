"""Runner-owned gdaemon front door child: spawn, descriptors, reuse, respawn, and stop."""

from __future__ import annotations

import asyncio
import errno
import json
import logging
import os
import signal
import socket
import subprocess
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from gobby import runner as runner_module
from gobby import runner_front_door
from gobby.cli.utils_process import is_port_available
from gobby.config.bootstrap import BootstrapConfig, FrontDoorConfig
from gobby.runner_front_door import FrontDoorChild, FrontDoorStartupError
from gobby.runner_pid_file import SERVICE_LAUNCH_ENV, PidOwnershipResolution, claim_pid_file

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="POSIX liveness pipe")


@pytest.mark.asyncio
@pytest.mark.parametrize("creation_fails", [False, True])
async def test_run_gobby_creates_break_glass_before_front_door(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, creation_fails: bool
) -> None:
    from starlette.requests import Request

    from gobby.servers.auth_service import AuthService
    from gobby.storage.hub.protocol import HubDatabase
    from gobby.utils import break_glass, local_token

    home = tmp_path / "home"
    outside = tmp_path / "outside"
    home.mkdir()
    outside.mkdir()
    config = outside / "bootstrap.yaml"
    monkeypatch.setenv("GOBBY_HOME", str(home))
    monkeypatch.setattr(local_token, "_daemon_bootstrap", None)
    bootstrap = BootstrapConfig(database_url="postgresql://test.invalid/gobby_test")
    monkeypatch.setattr("gobby.config.bootstrap.load_bootstrap", lambda *_a, **_kw: bootstrap)
    monkeypatch.setattr("gobby.utils.machine_id.require_machine_id", lambda: "machine-a")
    monkeypatch.setattr("gobby.storage.schema_contract.verify_schema", lambda _url: None)
    monkeypatch.setattr("gobby.daemon_lease.ActiveDaemonLease", lambda *_a, **_kw: FakeLease())
    monkeypatch.setattr("gobby.providers.version_gate.probe_and_publish_agy_support", AsyncMock())
    monkeypatch.setattr("gobby.runner_init.servers._bind_runtime_grants", lambda *_a: None)
    monkeypatch.setattr("gobby.daemon_lease_control.monitor_active_lease", AsyncMock())
    runner = SimpleNamespace(
        http_server=SimpleNamespace(effect_fence=None),
        run=AsyncMock(),
        request_shutdown=MagicMock(),
    )
    monkeypatch.setattr(runner_module.GobbyRunner, "create", AsyncMock(return_value=runner))
    observed: list[Path] = []

    def broken_database() -> HubDatabase:
        raise RuntimeError("database unavailable")

    def start_child() -> None:
        observed.append(local_token.daemon_bootstrap_path())
        path = outside / "break_glass"
        if creation_fails:
            assert not path.exists()
        else:
            assert path.is_file()
            request = Request(
                {
                    "type": "http",
                    "method": "GET",
                    "path": "/api/projects",
                    "headers": [(b"x-gobby-break-glass", path.read_bytes())],
                    "client": ("127.0.0.1", 50000),
                    "query_string": b"",
                }
            )
            assert AuthService(broken_database).authenticate(request).allowed

    child = MagicMock(spec=FrontDoorChild)
    child.start.side_effect = start_child

    def from_bootstrap(_bootstrap: BootstrapConfig, child_home: Path) -> FrontDoorChild:
        assert child_home == outside
        return child

    monkeypatch.setattr(FrontDoorChild, "from_bootstrap", from_bootstrap)
    if creation_fails:

        def fail_creation(_home: Path) -> None:
            raise OSError("isolated creation failure")

        monkeypatch.setattr(break_glass, "ensure_break_glass_credential", fail_creation)
    claim = claim_pid_file(home / "gobby.pid")
    assert claim is not None
    await runner_module.run_gobby(config, ownership_resolution=claim)
    assert observed == [config]
    runner.run.assert_awaited_once()
    child.stop.assert_called_once()
    assert not (home / "break_glass").exists()


# Stands in for `gdaemon serve`: logs what it inherited, then binds, exits, or
# hangs per FAKE_GDAEMON_MODE, and serves until its parent pipe reaches EOF.
FAKE_GDAEMON = """
import json, os, socket, sys

def is_open(fd):
    try:
        os.fstat(fd)
    except OSError:
        return False
    return True

parent_fd = int(os.environ["GOBBY_PARENT_FD"])
probe_fds = [int(fd) for fd in os.environ.get("FAKE_GDAEMON_PROBE_FDS", "").split(",") if fd]
with open(os.environ["FAKE_GDAEMON_LOG"], "a") as log:
    log.write(json.dumps({
        "argv": sys.argv[1:],
        "home": os.environ["GOBBY_HOME"],
        "front_door_secret": os.environ.get("GOBBY_FRONT_DOOR_SECRET"),
        "parent_fd_open": is_open(parent_fd),
        "probe_fds_open": [fd for fd in probe_fds if is_open(fd)],
    }) + "\\n")
mode = os.environ["FAKE_GDAEMON_MODE"]
if mode == "exit":
    sys.exit(3)
listeners = []
if mode == "serve":
    for port in os.environ["FAKE_GDAEMON_PORTS"].split(","):
        sock = socket.socket()
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("127.0.0.1", int(port)))
        sock.listen()
        listeners.append(sock)
while os.read(parent_fd, 64):
    pass
"""


@dataclass
class FakeGdaemon:
    binary: Path
    log: Path
    ports: tuple[int, int]
    home: Path

    def spawns(self) -> list[dict[str, object]]:
        if not self.log.exists():
            return []
        return [json.loads(line) for line in self.log.read_text().splitlines()]

    def child(self) -> FrontDoorChild:
        return FrontDoorChild(
            binary=str(self.binary),
            bind_host="127.0.0.1",
            public_ports=self.ports,
            bootstrap_dir=self.home,
        )

    def serving(self) -> bool:
        return all(_accepts(port) for port in self.ports)

    def ports_free(self) -> bool:
        return all(is_port_available(port, "127.0.0.1") for port in self.ports)


def _accepts(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.5):
            return True
    except OSError:
        return False


def _free_port_pair() -> tuple[int, int]:
    # The backend pair is the public pair + 100, so both must stay <= 65435.
    while True:
        socks = [socket.socket(), socket.socket()]
        for sock in socks:
            sock.bind(("127.0.0.1", 0))
        http_port, ws_port = (sock.getsockname()[1] for sock in socks)
        for sock in socks:
            sock.close()
        if max(http_port, ws_port) <= 65435 and 60891 not in (
            http_port,
            ws_port,
            http_port + 100,
            ws_port + 100,
        ):
            return http_port, ws_port


def _monitor_task() -> asyncio.Task[object]:
    monitors = [
        task
        for task in asyncio.all_tasks()
        if task.get_name() == "gdaemon-front-door-monitor" and not task.done()
    ]
    assert len(monitors) == 1
    return monitors[0]


async def _eventually(predicate: Callable[[], bool], timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        assert time.monotonic() < deadline, "condition not reached in time"
        await asyncio.sleep(0.02)


def _open_fds() -> set[int]:
    # The listing includes the descriptor listdir itself held, closed by now.
    open_fds: set[int] = set()
    for fd in (int(name) for name in os.listdir("/dev/fd")):
        try:
            os.fstat(fd)
        except OSError:
            continue
        open_fds.add(fd)
    return open_fds


@pytest.fixture
def fake_gdaemon(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> FakeGdaemon:
    binary = tmp_path / "gdaemon"
    binary.write_text(f"#!{sys.executable}\n{FAKE_GDAEMON}")
    binary.chmod(0o755)
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("GOBBY_HOME", str(home))
    monkeypatch.setattr("gobby.utils.local_token._daemon_bootstrap", None)
    fake = FakeGdaemon(binary, tmp_path / "gdaemon.log", _free_port_pair(), home)
    monkeypatch.setattr(runner_front_door, "resolve_native_bin", lambda _name: str(binary))
    monkeypatch.setenv("FAKE_GDAEMON_LOG", str(fake.log))
    monkeypatch.setenv("FAKE_GDAEMON_PORTS", ",".join(str(port) for port in fake.ports))
    monkeypatch.setenv("FAKE_GDAEMON_MODE", "serve")
    monkeypatch.delenv("FAKE_GDAEMON_PROBE_FDS", raising=False)
    monkeypatch.setattr(runner_front_door, "STARTUP_WINDOW_SECONDS", 5.0)
    monkeypatch.setattr(runner_front_door, "PORT_REUSE_WAIT_SECONDS", 5.0)
    monkeypatch.setattr(runner_front_door, "STOP_GRACE_SECONDS", 2.0)
    monkeypatch.setattr(runner_front_door, "RESPAWN_INITIAL_BACKOFF_SECONDS", 0.05)
    monkeypatch.setattr(runner_front_door, "POLL_INTERVAL_SECONDS", 0.02)
    return fake


class FakeLease:
    def try_acquire(self) -> bool:
        return True

    def heartbeat(self) -> None:
        pass

    def release(self) -> None:
        pass


@pytest.mark.asyncio
@pytest.mark.parametrize("launch", ["service", "direct", "explicit-bootstrap", "disabled"])
async def test_runner_spawns_child_for_either_launch_path(
    launch: str, fake_gdaemon: FakeGdaemon, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    claims: list[str] = []
    observed: dict[str, object] = {}
    bootstrap = BootstrapConfig(
        database_url="postgresql://test.invalid/gobby_test",
        bind_host="127.0.0.1",
        daemon_port=fake_gdaemon.ports[0],
        websocket_port=fake_gdaemon.ports[1],
        front_door=FrontDoorConfig(enabled=launch != "disabled"),
    )

    def claim_as(path_name: str) -> Callable[[Path], PidOwnershipResolution | None]:
        def claim(pid_file: Path) -> PidOwnershipResolution | None:
            claims.append(path_name)
            return claim_pid_file(pid_file)

        return claim

    if launch == "service":
        monkeypatch.setenv(SERVICE_LAUNCH_ENV, "1")
    else:
        monkeypatch.delenv(SERVICE_LAUNCH_ENV, raising=False)
    monkeypatch.setattr("gobby.cli.utils.get_gobby_home", lambda: fake_gdaemon.home)
    monkeypatch.setattr(
        "gobby.runner_pid_file.convert_or_acquire_service_claim", claim_as("service")
    )
    monkeypatch.setattr("gobby.runner_pid_file.adopt_inherited_claim", lambda _path: None)
    monkeypatch.setattr("gobby.runner_pid_file.claim_pid_file", claim_as("direct"))
    monkeypatch.setattr("gobby.config.bootstrap.load_bootstrap", lambda *_a, **_kw: bootstrap)
    monkeypatch.setattr("gobby.utils.machine_id.require_machine_id", lambda: "machine-a")
    monkeypatch.setattr("gobby.utils.local_token.read_local_api_token", lambda: "token")
    monkeypatch.setattr("gobby.storage.schema_contract.verify_schema", lambda _url: None)
    monkeypatch.setattr("gobby.daemon_lease.ActiveDaemonLease", lambda *_a, **_kw: FakeLease())

    async def fake_probe() -> None:
        pass

    monkeypatch.setattr("gobby.providers.version_gate.probe_and_publish_agy_support", fake_probe)

    class FakeRunner:
        front_door_child: FrontDoorChild | None = None

        def __init__(self) -> None:
            self.http_server = type("HTTP", (), {"effect_fence": None})()

        @classmethod
        async def create(cls, *_args: object, **_kwargs: object) -> FakeRunner:
            return cls()

        async def run(self, *, ownership_resolution: object) -> None:
            child = self.front_door_child
            observed["pid"] = child.pid if child is not None else None
            observed["serving"] = fake_gdaemon.serving()
            if child is not None:
                observed["front_door_secret"] = child.secret

        def request_shutdown(self) -> None:
            pass

    monkeypatch.setattr(runner_module, "GobbyRunner", FakeRunner)

    # An explicit bootstrap path points gdaemon at that file's directory, not GOBBY_HOME.
    explicit_dir = tmp_path / "explicit"
    if launch == "explicit-bootstrap":
        await runner_module.run_gobby(explicit_dir / "bootstrap.yaml")
    else:
        await runner_module.run_gobby()

    assert claims == ["service" if launch == "service" else "direct"]
    if launch == "disabled":
        assert observed == {"pid": None, "serving": False}
        assert fake_gdaemon.spawns() == []
        return
    assert observed["pid"] is not None
    assert observed["serving"] is True
    child_home = explicit_dir if launch == "explicit-bootstrap" else fake_gdaemon.home
    assert fake_gdaemon.spawns() == [
        {
            "argv": ["serve"],
            "home": str(child_home),
            "parent_fd_open": True,
            "probe_fds_open": [],
            "front_door_secret": observed["front_door_secret"],
        }
    ]
    # run_gobby stops the child before it releases the claim.
    assert fake_gdaemon.ports_free()


@pytest.mark.asyncio
async def test_front_door_secret_survives_respawn(fake_gdaemon: FakeGdaemon) -> None:
    child = fake_gdaemon.child()
    assert child.secret
    assert len(child.secret) >= 43
    assert fake_gdaemon.child().secret != child.secret
    await asyncio.to_thread(child.start)
    child.arm_respawn()
    first_pid = child.pid
    assert first_pid is not None
    try:
        assert fake_gdaemon.spawns()[-1]["front_door_secret"] == child.secret
        os.kill(first_pid, signal.SIGKILL)
        await _eventually(lambda: child.pid not in (None, first_pid))
        await _eventually(fake_gdaemon.serving)
        assert fake_gdaemon.spawns()[-1]["front_door_secret"] == child.secret
    finally:
        child.stop()


@pytest.mark.asyncio
async def test_child_reuse_failure_and_respawn(
    fake_gdaemon: FakeGdaemon,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    # No pid-lock descriptor: every fd the claim opened stays out of the child.
    before = _open_fds()
    claim = claim_pid_file(fake_gdaemon.home / "gobby.pid")
    assert claim is not None
    lock_fds = sorted(_open_fds() - before)
    assert lock_fds
    for fd in lock_fds:
        os.set_inheritable(fd, True)
    monkeypatch.setenv("FAKE_GDAEMON_PROBE_FDS", ",".join(str(fd) for fd in lock_fds))
    first = fake_gdaemon.child()
    try:
        await asyncio.to_thread(first.start)
    finally:
        claim.release()
    assert fake_gdaemon.spawns()[-1]["parent_fd_open"] is True
    assert fake_gdaemon.spawns()[-1]["probe_fds_open"] == []
    monkeypatch.delenv("FAKE_GDAEMON_PROBE_FDS")

    # Reuse: a second owner times out naming the held port...
    monkeypatch.setattr(runner_front_door, "PORT_REUSE_WAIT_SECONDS", 0.3)
    with pytest.raises(FrontDoorStartupError, match=f"Port {fake_gdaemon.ports[0]} on 127.0.0.1"):
        fake_gdaemon.child().start()
    # ...and succeeds once the previous child frees the pair within the wait.
    monkeypatch.setattr(runner_front_door, "PORT_REUSE_WAIT_SECONDS", 5.0)
    second = fake_gdaemon.child()
    reuse = asyncio.create_task(asyncio.to_thread(second.start))
    early, _ = await asyncio.wait({reuse}, timeout=0.3)
    assert not early
    first.stop()
    await asyncio.wait_for(reuse, timeout=5.0)
    assert fake_gdaemon.serving()
    second.stop()

    # Startup failure: a child that exits, or never binds, fails start and is reaped.
    monkeypatch.setenv("FAKE_GDAEMON_MODE", "exit")
    with pytest.raises(FrontDoorStartupError, match="exited with code 3 before binding"):
        fake_gdaemon.child().start()
    monkeypatch.setenv("FAKE_GDAEMON_MODE", "hang")
    monkeypatch.setattr(runner_front_door, "STARTUP_WINDOW_SECONDS", 0.5)
    hung = fake_gdaemon.child()
    with pytest.raises(FrontDoorStartupError, match="did not bind the public ports"):
        hung.start()
    hung_pid = hung.pid
    assert hung_pid is not None
    with pytest.raises(ProcessLookupError):
        os.kill(hung_pid, 0)
    hung.stop()

    # Respawn: a crashed child comes back on the same public pair.
    monkeypatch.setenv("FAKE_GDAEMON_MODE", "serve")
    monkeypatch.setattr(runner_front_door, "STARTUP_WINDOW_SECONDS", 5.0)
    crashing = fake_gdaemon.child()
    await asyncio.to_thread(crashing.start)
    crashing.arm_respawn()
    crashed_pid = crashing.pid
    assert crashed_pid is not None
    try:
        os.kill(crashed_pid, signal.SIGKILL)
        await _eventually(lambda: crashing.pid not in (None, crashed_pid))
        await _eventually(fake_gdaemon.serving)
    finally:
        crashing.stop()
    assert fake_gdaemon.ports_free()

    # Backoff doubles to the cap while respawns keep failing; only serving resets it.
    monkeypatch.setattr(runner_front_door, "RESPAWN_MAX_BACKOFF_SECONDS", 0.2)
    caplog.set_level(logging.WARNING, logger=runner_front_door.__name__)
    caplog.clear()

    def backoffs() -> list[object]:
        return [
            record.args[2]
            for record in caplog.records
            if "respawning in" in record.getMessage() and isinstance(record.args, tuple)
        ]

    failing = fake_gdaemon.child()
    await asyncio.to_thread(failing.start)
    failing.arm_respawn()
    monkeypatch.setenv("FAKE_GDAEMON_MODE", "exit")
    failing_pid = failing.pid
    assert failing_pid is not None
    try:
        os.kill(failing_pid, signal.SIGKILL)
        await _eventually(lambda: len(backoffs()) >= 5)
    finally:
        failing.stop()
    assert backoffs()[:5] == [0.05, 0.1, 0.2, 0.2, 0.2]


@pytest.mark.asyncio
async def test_respawn_survives_a_transient_spawn_error(
    fake_gdaemon: FakeGdaemon, monkeypatch: pytest.MonkeyPatch
) -> None:
    child = fake_gdaemon.child()
    await asyncio.to_thread(child.start)
    child.arm_respawn()
    popen = child._popen
    failures = [OSError(errno.EAGAIN, "Resource temporarily unavailable")]

    def flaky_popen() -> tuple[subprocess.Popen[bytes], int]:
        if failures:
            raise failures.pop()
        return popen()

    monkeypatch.setattr(child, "_popen", flaky_popen)
    crashed_pid = child.pid
    assert crashed_pid is not None
    try:
        os.kill(crashed_pid, signal.SIGKILL)
        # The first respawn hits EAGAIN; the monitor must live to make the second.
        await _eventually(lambda: child.pid not in (None, crashed_pid))
        await _eventually(fake_gdaemon.serving)
        assert failures == []
        assert not _monitor_task().done()
    finally:
        child.stop()


@pytest.mark.asyncio
async def test_shutdown_disarms_respawn(
    fake_gdaemon: FakeGdaemon,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    # A child that exits after shutdown disarmed respawn stays down.
    exiting = fake_gdaemon.child()
    await asyncio.to_thread(exiting.start)
    exiting.arm_respawn()
    monitor = _monitor_task()
    exiting.disarm()
    await asyncio.wait({monitor}, timeout=5.0)
    assert monitor.done()
    exiting_pid = exiting.pid
    assert exiting_pid is not None
    os.kill(exiting_pid, signal.SIGKILL)
    assert exiting.pid == exiting_pid
    exiting.stop()
    assert len(fake_gdaemon.spawns()) == 1

    # A child waiting in backoff when shutdown begins is never respawned.
    monkeypatch.setattr(runner_front_door, "RESPAWN_INITIAL_BACKOFF_SECONDS", 0.5)
    caplog.set_level(logging.WARNING, logger=runner_front_door.__name__)
    backing_off = fake_gdaemon.child()
    await asyncio.to_thread(backing_off.start)
    backing_off.arm_respawn()
    monitor = _monitor_task()
    backing_off_pid = backing_off.pid
    assert backing_off_pid is not None
    os.kill(backing_off_pid, signal.SIGKILL)
    await _eventually(lambda: "respawning in" in caplog.text)
    backing_off.stop()
    # Stopped mid-backoff: the monitor ends, so nothing is left to respawn the child.
    await asyncio.wait({monitor}, timeout=5.0)
    assert monitor.done()
    assert backing_off.pid is None
    assert len(fake_gdaemon.spawns()) == 2
    assert fake_gdaemon.ports_free()


def test_missing_gdaemon_guidance_requires_front_door(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(runner_front_door, "resolve_native_bin", lambda _name: None)
    bootstrap = BootstrapConfig(front_door=FrontDoorConfig(enabled=True))
    with pytest.raises(runner_front_door.FrontDoorStartupError, match="gobby install") as error:
        runner_front_door.FrontDoorChild.from_bootstrap(bootstrap, tmp_path)
    assert "front_door.enabled: false" not in str(error.value)
    assert "API-key" in str(error.value)
