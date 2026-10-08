"""Interrupt the xdist controller while detached fixture setup is blocked."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from uuid import uuid4

import psutil
import pytest

from gobby.terminals.host_protocol import control_token_path
from tests.e2e.conftest import prepare_daemon_env
from tests.fixtures.e2e_run_cleanup import RUN_ID_ENV, cleanup_run
from tests.native_binary_selection import select_native_binary
from tests.workflows.placed_runbook_live_support import fixture_host, host_socket_dir

pytestmark = pytest.mark.integration
_ROOT = Path(__file__).resolve().parents[2]

_RUNNER = """
import json
import os
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from tests.e2e.conftest import find_free_port
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
        host = start_fixture_host(binary, socket_dir, os.environ)
        (home / "late.json").write_text(json.dumps({"pid": host.pid}))
        (home / "shutdown").write_text(self.path)
        self.send_response(200)
        self.end_headers()
        threading.Thread(target=server.shutdown).start()

server = HTTPServer(("127.0.0.1", find_free_port()), Handler)
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
    daemon = subprocess.Popen(
        [sys.executable, str(root / "readiness_bootstrap.py"), "gobby.runner"],
        env=env, start_new_session=True)
    deadline = time.monotonic() + 30
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


def _await_ready(root: Path, process: subprocess.Popen[bytes]) -> list[Path]:
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        ready = sorted(root.glob("gw*/ready.json"))
        if len(ready) == 2:
            return ready
        assert process.poll() is None, "Nested pytest exited before both workers started"
        time.sleep(0.05)
    raise AssertionError("Both xdist workers did not reach blocked fixture setup")


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
    with host_socket_dir() as sockets, host_socket_dir() as foreign_socket:
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
