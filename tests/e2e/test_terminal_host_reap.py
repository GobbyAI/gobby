"""E2E socket-dir ownership: hosts orphaned by a dead pytest process are reaped."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import threading
import uuid
from collections.abc import Callable, Iterator
from pathlib import Path

import psutil
import pytest

from gobby.guard_set_g import socket_dir_from_cmdline
from gobby.terminals.host_protocol import (
    control_socket_path,
    control_token_path,
    pidfile_path,
    write_pidfile,
)
from tests._timing import wait_for_condition
from tests.e2e.conftest import (
    E2E_HOST_OWNER_FILE,
    create_host_socket_dir,
    reap_orphaned_terminal_hosts,
    stop_terminal_host,
)
from tests.e2e.test_external_terminal_attach import _gterm_bin_dir

pytestmark = pytest.mark.e2e

Spawn = Callable[[list[str]], subprocess.Popen[bytes]]


@pytest.fixture
def host_root() -> Iterator[Path]:
    # AF_UNIX paths are short; keep the root near the temp base like the e2e fixtures.
    base = os.environ.get("CLAUDE_CODE_TMPDIR") or tempfile.gettempdir()
    root = Path(tempfile.mkdtemp(prefix="gr-", dir=base)).resolve()
    yield root
    shutil.rmtree(root, ignore_errors=True)


@pytest.fixture
def spawn() -> Iterator[Spawn]:
    started: list[subprocess.Popen[bytes]] = []

    def start(argv: list[str]) -> subprocess.Popen[bytes]:
        process = subprocess.Popen(
            argv,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        started.append(process)
        return process

    yield start
    for process in started:
        if process.poll() is None:
            process.kill()
            process.wait()


def _start_host(spawn: Spawn, socket_dir: Path) -> subprocess.Popen[bytes]:
    # The daemon normally seeds this; the host refuses to start without it.
    token_path = control_token_path(socket_dir)
    token_path.write_text(uuid.uuid4().hex)
    token_path.chmod(0o600)
    host = spawn([str(_gterm_bin_dir() / "gterm"), "host", "--socket-dir", str(socket_dir)])
    write_pidfile(socket_dir, host.pid)
    wait_for_condition(
        lambda: control_socket_path(socket_dir).exists(),
        timeout=10.0,
        interval=0.05,
        description="gterm host control socket",
    )
    assert host.poll() is None
    return host


def _dead_pid() -> int:
    process = subprocess.Popen([sys.executable, "-c", "pass"])
    assert process.wait() == 0
    return process.pid


def _orphaned_dir(root: Path) -> Path:
    socket_dir = Path(tempfile.mkdtemp(prefix="gh-", dir=root)).resolve()
    (socket_dir / E2E_HOST_OWNER_FILE).write_text(str(_dead_pid()))
    return socket_dir


def _wait_for_exit(process: subprocess.Popen[bytes]) -> None:
    wait_for_condition(
        lambda: process.poll() is not None,
        timeout=5.0,
        interval=0.05,
        description=f"pid {process.pid} exit",
    )


def test_reap_stops_the_host_of_a_dead_owner(host_root: Path, spawn: Spawn) -> None:
    socket_dir = _orphaned_dir(host_root)
    host = _start_host(spawn, socket_dir)

    reap_orphaned_terminal_hosts(host_root)

    _wait_for_exit(host)
    assert host.returncode is not None
    assert not socket_dir.exists()


def test_reap_stops_a_dead_owners_host_that_has_no_pidfile_yet(
    host_root: Path, spawn: Spawn
) -> None:
    socket_dir = _orphaned_dir(host_root)
    host = _start_host(spawn, socket_dir)
    pidfile_path(socket_dir).unlink()

    reap_orphaned_terminal_hosts(host_root)

    _wait_for_exit(host)
    assert host.returncode is not None
    assert not socket_dir.exists()


def test_reap_leaves_the_host_of_a_live_owner(host_root: Path, spawn: Spawn) -> None:
    socket_dir = create_host_socket_dir(host_root)
    host = _start_host(spawn, socket_dir)

    reap_orphaned_terminal_hosts(host_root)

    assert host.poll() is None
    assert (socket_dir / E2E_HOST_OWNER_FILE).read_text() == str(os.getpid())


def test_reap_never_touches_an_unmarked_dir(host_root: Path, spawn: Spawn) -> None:
    socket_dir = Path(tempfile.mkdtemp(prefix="gh-", dir=host_root)).resolve()
    host = _start_host(spawn, socket_dir)

    reap_orphaned_terminal_hosts(host_root)

    assert host.poll() is None
    assert socket_dir.is_dir()


def test_reap_spares_a_non_host_pid_named_by_the_pidfile(host_root: Path, spawn: Spawn) -> None:
    socket_dir = _orphaned_dir(host_root)
    bystander = spawn([sys.executable, "-c", "import time; time.sleep(60)"])
    write_pidfile(socket_dir, bystander.pid)

    reap_orphaned_terminal_hosts(host_root)

    assert bystander.poll() is None


def test_reap_spares_a_host_serving_another_dir(host_root: Path, spawn: Spawn) -> None:
    other_host = _start_host(spawn, create_host_socket_dir(host_root))
    socket_dir = _orphaned_dir(host_root)
    write_pidfile(socket_dir, other_host.pid)

    reap_orphaned_terminal_hosts(host_root)

    assert other_host.poll() is None


def test_stop_terminal_host_stops_the_dirs_host(host_root: Path, spawn: Spawn) -> None:
    socket_dir = create_host_socket_dir(host_root)
    host = _start_host(spawn, socket_dir)

    stop_terminal_host(socket_dir)

    _wait_for_exit(host)
    assert host.returncode is not None


def test_stop_terminal_host_stops_a_host_that_starts_during_teardown(
    host_root: Path, spawn: Spawn
) -> None:
    # A daemon torn down mid-spawn can exec its host after teardown first looks.
    # Missing it lets the rmtree drop the owner marker, so no reap ever finds it.
    socket_dir = create_host_socket_dir(host_root)
    token_path = control_token_path(socket_dir)
    token_path.write_text(uuid.uuid4().hex)
    token_path.chmod(0o600)
    argv = [str(_gterm_bin_dir() / "gterm"), "host", "--socket-dir", str(socket_dir)]
    late = threading.Timer(0.3, lambda: spawn(argv))
    late.start()

    stop_terminal_host(socket_dir)
    late.join()

    assert not [
        process.pid
        for process in psutil.process_iter(["cmdline"])
        if socket_dir_from_cmdline(process.info["cmdline"]) == socket_dir
    ]
