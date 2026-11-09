"""Interrupt the xdist controller while detached fixture setup is blocked."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import tempfile
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Protocol
from uuid import uuid4

import psutil
import pytest

from gobby.terminals.host_protocol import control_socket_path, control_token_path
from tests.e2e.conftest import prepare_daemon_env
from tests.fixtures.e2e_run_cleanup import RUN_ID_ENV, cleanup_run
from tests.native_binary_selection import select_native_binary
from tests.workflows.placed_runbook_live_support import fixture_host, host_socket_dir

pytestmark = pytest.mark.integration
_ROOT = Path(__file__).resolve().parents[2]

_RUNNER = """
import json
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

# Imported before daemon-ready: _stop_runner gives the shutdown POST 3s. Measured 17-24s for this
# import under host load (load average 30-36), so an import inside the handler always exceeds it.
from tests.workflows.placed_runbook_live_support import start_fixture_host

home = Path(os.environ["GOBBY_HOME"])
socket_dir = Path(os.environ["PROBE_LATE_SOCKET"])
binary = Path(os.environ["PROBE_GTERM"])

class Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        if (self.path != "/api/admin/shutdown?terminals=true"
                or self.headers.get("Authorization") != "Bearer probe-key"):
            self.send_error(403)
            return
        # Written before the host starts and before any terminating signal, so the record
        # exists even if the POST times out and the stub is terminated.
        started = time.monotonic()
        print(f"shutdown POST received at {time.time():.3f}", file=sys.stderr, flush=True)
        (home / "shutdown").write_text(self.path)
        host = start_fixture_host(binary, socket_dir, os.environ)
        print(f"host ready after {time.monotonic() - started:.2f}s", file=sys.stderr, flush=True)
        (home / "late.json").write_text(json.dumps({"pid": host.pid}))
        self.send_response(200)
        self.end_headers()
        threading.Thread(target=server.shutdown).start()

server = HTTPServer(("127.0.0.1", 0), Handler)
(home / "bootstrap.yaml").write_text(
    f"daemon_port: {server.server_port}\\napi_key: probe-key\\n")
(home / "daemon-ready").write_text(str(os.getpid()))
server.serve_forever()
server.server_close()
"""

_TEST = """
import json
import os
import subprocess
import sys
import time
from pathlib import Path
import pytest
from gobby.terminals.host_protocol import control_token_path
from tests.e2e.conftest import prepare_daemon_env
from tests.fixtures.e2e_run_cleanup import RUN_ID_ENV
from tests.workflows.placed_runbook_live_support import start_fixture_host

@pytest.fixture
def pending(request):
    worker = request.config.workerinput["workerid"]
    root = Path(os.environ["PROBE_ROOT"])
    home = root / worker
    home.mkdir()
    (home / "run-id").write_text(os.environ[RUN_ID_ENV])
    sockets = Path(os.environ["PROBE_SOCKETS"])
    initial, late = sockets / worker, sockets / (worker + "-late")
    for directory in (initial, late):
        directory.mkdir()
        control_token_path(directory).write_text("probe-host-key")
    env = prepare_daemon_env(home_dir=home)
    env.update(GOBBY_HOME=str(home), GOBBY_CONFIG=str(home / "config.yaml"),
               PROBE_LATE_SOCKET=str(late))
    (home / "config.yaml").write_text("test_mode: true\\n")
    host = start_fixture_host(Path(env["PROBE_GTERM"]), initial, env)
    with (home / "stub.stderr").open("wb") as stub_output:
        daemon = subprocess.Popen(
            [sys.executable, str(root / "readiness_bootstrap.py"), "gobby.runner"],
            env=env, start_new_session=True, stdout=stub_output, stderr=stub_output)
    # Measured 2026-11-07. Two concurrent module-level starts reached daemon-ready in 21.70 s
    # and 26.59 s (load average 32-36), 11.35 s and 12.10 s, and 13.97 s and 14.21 s. One start
    # alone took 40.36 s at load average 44. 120 s is 3x that worst observed start.
    deadline = time.monotonic() + 120
    while not (home / "daemon-ready").exists():
        assert daemon.poll() is None
        assert time.monotonic() < deadline
        time.sleep(0.05)
    (home / "ready.json").write_text(json.dumps({
        "run_id": env[RUN_ID_ENV], "host": host.pid, "daemon": daemon.pid}))
    # Setup never yields. xdist must terminate the worker, bypassing fixture teardown.
    while True:
        time.sleep(0.1)
    yield

@pytest.mark.parametrize("case", [0, 1])
def test_pending(pending, case):
    assert case >= 0
"""


# gterm refuses a control socket path longer than the AF_UNIX sun_path limit (103 usable bytes here).
_SUN_PATH_LIMIT = 103
_SUN_PATH_HEADROOM = 8


def _assert_socket_headroom(sockets: Path) -> None:
    """Fail before launch if a worker's control socket path is within the headroom of the limit."""
    names = [f"gw{index}{suffix}" for index in range(2) for suffix in ("", "-late")]
    paths = [control_socket_path(sockets / name) for name in names]
    longest = max(paths, key=lambda path: len(os.fsencode(path)))
    length = len(os.fsencode(longest))
    assert length <= _SUN_PATH_LIMIT - _SUN_PATH_HEADROOM, (
        f"control socket path {longest} is {length} bytes; the AF_UNIX limit is "
        f"{_SUN_PATH_LIMIT} bytes and this test needs {_SUN_PATH_HEADROOM} bytes of headroom. "
        "Shorten the socket root."
    )


# Measured in the solo run at host load ~50: workers reached the fixture 85 s after the parent
# started, and daemon-ready followed 38-43 s later (ready at 123 s and 128 s). A 120 s deadline
# from the parent start expired with both workers healthy. 300 s is about twice that worst case.
_READY_TIMEOUT_SECONDS = 300
_LOG_TAIL_CHARS = 2000


class _Pollable(Protocol):
    def poll(self) -> int | None: ...


def _tail(path: Path) -> str:
    if not path.exists():
        return "<missing>"
    return path.read_text(errors="replace")[-_LOG_TAIL_CHARS:]


def _await_ready(
    root: Path, process: _Pollable, timeout: float = _READY_TIMEOUT_SECONDS
) -> list[Path]:
    # One wall clock from the parent start: worker collection, host start and daemon-stub start
    # run in sequence, so no single stage gets its own budget here.
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        ready = sorted(root.glob("gw*/ready.json"))
        if len(ready) == 2:
            return ready
        assert process.poll() is None, "Nested pytest exited before both workers started"
        time.sleep(0.05)
    workers = "".join(
        f"\n--- {worker.name}/stub.stderr ---\n{_tail(worker / 'stub.stderr')}"
        for worker in sorted(root.glob("gw*"))
        if worker.is_dir()
    )
    raise AssertionError(
        "Both xdist workers did not reach blocked fixture setup within "
        f"{timeout:g}s ({len(list(root.glob('gw*/ready.json')))} of 2 workers ready).\n"
        f"--- pytest.log tail ---\n{_tail(root / 'pytest.log')}{workers}"
    )


class _NestedRun:
    """Stands in for the nested pytest process: alive, optionally publishing both workers on poll."""

    def __init__(self, root: Path, *, publish_on_poll: bool) -> None:
        self._root = root
        self._publish_on_poll = publish_on_poll

    def poll(self) -> int | None:
        if self._publish_on_poll:
            self._publish_on_poll = False
            for name in ("gw0", "gw1"):
                (self._root / name).mkdir()
                (self._root / name / "ready.json").write_text("{}")
        return None


def test_await_ready_returns_once_both_workers_publish_ready(tmp_path: Path) -> None:
    ready = _await_ready(tmp_path, _NestedRun(tmp_path, publish_on_poll=True), timeout=10)
    assert [path.parent.name for path in ready] == ["gw0", "gw1"]


def test_await_ready_timeout_reports_worker_state_and_log_tail(tmp_path: Path) -> None:
    (tmp_path / "pytest.log").write_text("collected 2 items\nworker gw0 stalled in import\n")
    (tmp_path / "gw0").mkdir()
    (tmp_path / "gw0" / "stub.stderr").write_text("shutdown POST received at 1.0\n")
    with pytest.raises(AssertionError) as raised:
        _await_ready(tmp_path, _NestedRun(tmp_path, publish_on_poll=False), timeout=0.3)
    message = str(raised.value)
    assert "Both xdist workers did not reach blocked fixture setup" in message
    assert "0 of 2 workers ready" in message
    assert "worker gw0 stalled in import" in message
    assert "shutdown POST received at 1.0" in message


def test_sigint_drains_daemons_and_late_hosts_preserving_another_run(tmp_path: Path) -> None:
    selected = select_native_binary("gterm", required=True)
    assert selected is not None
    binary = selected.path
    (tmp_path / "readiness_bootstrap.py").write_text(_RUNNER)
    (tmp_path / "test_pending.py").write_text(_TEST)
    (tmp_path / "pytest.ini").write_text("[pytest]\naddopts =\n")
    foreign_home = tmp_path / "foreign"
    foreign_home.mkdir()
    foreign_env = prepare_daemon_env(home_dir=foreign_home)
    foreign_env[RUN_ID_ENV] = uuid4().hex
    with (
        tempfile.TemporaryDirectory(prefix="gsig-", dir="/tmp", ignore_cleanup_errors=True) as root,
        host_socket_dir(Path(root)) as sockets,
        host_socket_dir(Path(root)) as foreign_socket,
    ):
        _assert_socket_headroom(sockets)
        control_token_path(foreign_socket).write_text("foreign-host-key")
        with fixture_host(binary, foreign_socket, foreign_env) as foreign:
            env = os.environ.copy()
            env.update(
                PYTHONPATH=os.pathsep.join([str(_ROOT), str(_ROOT / "src")]),
                PYTEST_DISABLE_PLUGIN_AUTOLOAD="1",
                PROBE_ROOT=str(tmp_path),
                PROBE_SOCKETS=str(sockets),
                PROBE_GTERM=str(binary),
            )
            env.pop("PYTEST_ADDOPTS", None)
            with (tmp_path / "pytest.log").open("wb") as log:
                process = subprocess.Popen(
                    [
                        sys.executable,
                        "-m",
                        "pytest",
                        "-p",
                        "xdist.plugin",
                        "-p",
                        "tests.fixtures.e2e_run_cleanup",
                        "-n",
                        "2",
                        "-q",
                        "test_pending.py",
                    ],
                    cwd=tmp_path,
                    env=env,
                    stdout=log,
                    stderr=log,
                    start_new_session=True,
                )
                try:
                    ready = _await_ready(tmp_path, process)
                    records = [json.loads(path.read_text()) for path in ready]
                    assert len({record["run_id"] for record in records}) == 1
                    process.send_signal(signal.SIGINT)
                    assert process.wait(timeout=60) == 2, (tmp_path / "pytest.log").read_text()
                    assert foreign.alive(), "Controller cleanup stopped another run's host"
                    for path, record in zip(ready, records, strict=True):
                        home = path.parent
                        assert (home / "shutdown").read_text() == (
                            "/api/admin/shutdown?terminals=true"
                        )
                        late = json.loads((home / "late.json").read_text())
                        for pid in (record["host"], record["daemon"], late["pid"]):
                            assert not psutil.pid_exists(pid) or (
                                psutil.Process(pid).status() == psutil.STATUS_ZOMBIE
                            ), f"Owned fixture process {pid} survived SIGINT"
                finally:
                    if process.poll() is None:
                        process.kill()
                        process.wait(timeout=10)
                    for path in tmp_path.glob("gw*/run-id"):
                        cleanup_run(path.read_text())


def test_prepare_env_stamps_current_run_even_with_explicit_base(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(RUN_ID_ENV, "current-run")
    assert prepare_daemon_env({RUN_ID_ENV: "old-run"})[RUN_ID_ENV] == "current-run"


def test_startup_without_a_ready_socket_is_reaped(tmp_path: Path) -> None:
    bootstrap = tmp_path / "readiness_bootstrap.py"
    bootstrap.write_text("import time\ntime.sleep(60)\n")
    env = prepare_daemon_env(home_dir=tmp_path)
    env[RUN_ID_ENV] = uuid4().hex
    env.update(GOBBY_HOME=str(tmp_path), GOBBY_CONFIG=str(tmp_path / "config.yaml"))
    process = subprocess.Popen(
        [sys.executable, str(bootstrap), "gobby.runner"], env=env, start_new_session=True
    )
    try:
        cleanup_run(env[RUN_ID_ENV])
        # psutil.wait reaps our child; Popen cannot recover its exit status.
        assert not psutil.pid_exists(process.pid)
        process.wait(timeout=5)
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)


def test_empty_run_returns_without_a_settle_wait(monkeypatch: pytest.MonkeyPatch) -> None:
    scans = 0

    def no_processes() -> Iterator[psutil.Process]:
        nonlocal scans
        scans += 1
        return iter(())

    def unexpected_wait(seconds: float) -> None:
        raise AssertionError(f"Empty run waited {seconds}s despite owning no fixtures")

    monkeypatch.setattr(psutil, "process_iter", no_processes)
    monkeypatch.setattr(time, "sleep", unexpected_wait)
    cleanup_run("empty-run")
    assert scans == 1
