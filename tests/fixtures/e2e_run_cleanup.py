"""Controller-owned cleanup for detached fixtures after xdist workers disappear."""

from __future__ import annotations

import os
import time
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

import httpx
import psutil
import pytest
import yaml
from xdist.workermanage import WorkerController

RUN_ID_ENV = "GOBBY_E2E_RUN_ID"
_RUN_ID = pytest.StashKey[str]()
_PREVIOUS_ID = pytest.StashKey[str | None]()
_RUNNER_MODULES = {"gobby.runner", "tests.workflows.placed_runbook_live_support"}


def pytest_configure(config: pytest.Config) -> None:
    worker_input = getattr(config, "workerinput", None)
    run_id = str(worker_input[RUN_ID_ENV]) if worker_input is not None else uuid4().hex
    config.stash[_RUN_ID] = run_id
    config.stash[_PREVIOUS_ID] = os.environ.get(RUN_ID_ENV)
    os.environ[RUN_ID_ENV] = run_id


@pytest.hookimpl(optionalhook=True)
def pytest_configure_node(node: WorkerController) -> None:
    node.workerinput[RUN_ID_ENV] = node.config.stash[_RUN_ID]


def pytest_unconfigure(config: pytest.Config) -> None:
    previous = config.stash.get(_PREVIOUS_ID, None)
    if previous is None:
        os.environ.pop(RUN_ID_ENV, None)
    else:
        os.environ[RUN_ID_ENV] = previous


@pytest.hookimpl(wrapper=True, tryfirst=True)
def pytest_sessionfinish(session: pytest.Session) -> Iterator[None]:
    try:
        yield
    finally:
        # xdist tears down its nodes inside this hook. Workers can exit without
        # running yield-fixture finalizers, so the controller must drain last.
        if not hasattr(session.config, "workerinput"):
            cleanup_run(session.config.stash[_RUN_ID])


@dataclass(frozen=True)
class Resource:
    process: psutil.Process
    env: dict[str, str]
    socket_dir: Path | None


def _socket_dir(command: list[str]) -> Path | None:
    if len(command) < 4 or Path(command[0]).name not in {"gterm", "gterm.exe"}:
        return None
    if command[1] != "host" or "--socket-dir" not in command:
        return None
    index = command.index("--socket-dir") + 1
    return Path(command[index]).resolve() if index < len(command) else None


def _is_runner(command: list[str]) -> bool:
    return (
        len(command) >= 3
        and command[2] in _RUNNER_MODULES
        and (command[1] == "-m" or Path(command[1]).name == "readiness_bootstrap.py")
    )


def _resources(run_id: str) -> list[Resource]:
    resources: list[Resource] = []
    for process in psutil.process_iter():
        try:
            command = process.cmdline()
            socket_dir = _socket_dir(command)
            if socket_dir is None and not _is_runner(command):
                continue
            env = process.environ()
            if env.get(RUN_ID_ENV) == run_id and process.status() != psutil.STATUS_ZOMBIE:
                resources.append(Resource(process, env, socket_dir))
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    return resources


def _stop_runner(resource: Resource) -> None:
    from tests.e2e.conftest import daemon_auth_headers

    process = resource.process
    if not process.is_running():
        return
    home = Path(resource.env.get("GOBBY_HOME", ""))
    config = Path(resource.env.get("GOBBY_CONFIG", ""))
    # Never resolve a missing fixture home to the user's default bootstrap.
    real_home = Path.home().resolve()
    if (
        home.is_absolute()
        and home.resolve() not in {real_home, real_home / ".gobby"}
        and config.parent == home
        and resource.env.get("HOME") == str(home)
    ):
        try:
            bootstrap = yaml.safe_load((home / "bootstrap.yaml").read_text())
            port = int(bootstrap["daemon_port"])
            if not 0 < port < 65536 or port == 60891:
                raise ValueError("Not an isolated daemon port")
            # Recheck identity before contacting its authenticated loopback
            # socket. psutil guards against PID reuse on signal fallback.
            if process.is_running():
                httpx.post(
                    f"http://127.0.0.1:{port}/api/admin/shutdown?terminals=true",
                    headers=daemon_auth_headers(home),
                    timeout=3.0,
                    trust_env=False,
                ).raise_for_status()
                process.wait(timeout=10)
                return
        except (
            OSError,
            ValueError,
            KeyError,
            TypeError,
            RuntimeError,
            httpx.HTTPError,
            yaml.YAMLError,
            psutil.TimeoutExpired,
        ):
            pass
        except psutil.NoSuchProcess:
            return
    try:
        process.terminate()
        process.wait(timeout=5)
    except psutil.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)
    except psutil.NoSuchProcess:
        pass


def cleanup_run(run_id: str) -> None:
    from gobby.terminals.host_protocol import read_pidfile, write_pidfile
    from tests.e2e.conftest import E2E_HOST_SETTLE_SECONDS, _drain_terminal_host, _hosts_serving

    if not run_id:
        raise ValueError("Fixture cleanup requires a nonempty pytest run ID")
    quiet_until = time.monotonic()
    while True:
        resources = _resources(run_id)
        if not resources:
            if time.monotonic() >= quiet_until:
                return
            time.sleep(0.05)
            continue
        # A shutting-down daemon can still spawn a host. Drain daemons first,
        # then rescan until this run has stayed quiet for the settle interval.
        for resource in resources:
            if resource.socket_dir is None:
                _stop_runner(resource)
        hosts = [resource for resource in _resources(run_id) if resource.socket_dir is not None]
        owned_pids = {resource.process.pid for resource in hosts}
        for socket_dir in {resource.socket_dir for resource in hosts}:
            assert socket_dir is not None
            serving = _hosts_serving(socket_dir)
            if set(serving) - owned_pids:
                raise RuntimeError(f"Another run owns a host at {socket_dir}; refusing to drain")
            if serving:
                if read_pidfile(socket_dir) not in serving:
                    write_pidfile(socket_dir, serving[0])
                _drain_terminal_host(socket_dir)
                if set(_hosts_serving(socket_dir)) & set(serving):
                    raise RuntimeError(f"Fixture host survived drain at {socket_dir}")
        quiet_until = time.monotonic() + E2E_HOST_SETTLE_SECONDS
