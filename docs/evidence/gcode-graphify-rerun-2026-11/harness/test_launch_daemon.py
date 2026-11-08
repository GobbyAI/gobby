"""The isolated daemon launch goes through the SRT runner with a policy confined to R."""

from __future__ import annotations

import email.message
import errno
import hashlib
import io
import json
import os
import signal
import socket
import subprocess
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

import launch_daemon
import provision_environment
import pytest
import runtime_gobby
from launch_daemon import (
    SrtRunner,
    backend_environment,
    daemon_argv,
    daemon_health_failure,
    launch,
    srt_policy,
    srt_wrapped_argv,
)
from runtime_boundary import EvidenceError, file_record, verify_file_record

ROOT = Path("/runtime/root")
HOME = Path("/operator/home")
BOOTSTRAP = ROOT / "gobby-home/bootstrap.yaml"
SRT = SrtRunner(
    node=Path("/opt/node/bin/node"),
    runner=Path("/opt/srt/0.0.79/runner.mjs"),
    monitor_socket=None,
)
OPERATOR_DENY_READS = [
    "/operator/home/.gobby/bootstrap.yaml",
    "/operator/home/.gobby/.secret_kek",
    "/operator/home/.gobby/.secret_salt",
    "/operator/home/.gobby/local_cli_token",
    "/operator/home/.codex/auth.json",
]


def test_daemon_argv_is_the_supervised_gdaemon_serve_under_the_runtime_home() -> None:
    assert daemon_argv(ROOT) == ["/runtime/root/gobby-home/bin/gdaemon", "serve"]


def test_gdaemon_runs_the_venv_gobby_runner_as_its_backend_on_the_bootstrap_config() -> None:
    environment = backend_environment(ROOT, BOOTSTRAP)

    assert json.loads(environment["GOBBY_BACKEND_COMMAND"]) == [
        "/runtime/root/tools/gobby/venv/bin/python",
        "-m",
        "gobby.runner",
        "--config",
        "/runtime/root/gobby-home/bootstrap.yaml",
    ]


def test_failed_launch_keeps_its_records_when_the_daemon_never_becomes_healthy(
    staged_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    process = _FakeProcess()
    _patch_launch(monkeypatch, process)
    monkeypatch.setattr(launch_daemon, "daemon_health_failure", lambda url: "no answer")

    with pytest.raises(EvidenceError, match="not healthy") as caught:
        launch(staged_root, "attempt-1", SRT, home=tmp_path / "home")

    assert process.group_signals
    assert (staged_root / "receipts/daemon-launch-attempt-1.json").exists()
    assert (staged_root / "receipts/active-launches.json").exists()
    assert any("cleanup unproven" in note for note in caught.value.__notes__)


def test_failed_launch_keeps_its_records_when_the_wrapper_exits_before_the_daemon_is_healthy(
    staged_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    process = _FakeProcess()
    process.returncode = 1
    _patch_launch(monkeypatch, process)
    monkeypatch.setattr(launch_daemon, "daemon_health_failure", lambda url: "no answer")

    with pytest.raises(EvidenceError, match="exited before"):
        launch(staged_root, "attempt-1", SRT, home=tmp_path / "home")

    assert (staged_root / "receipts/active-launches.json").exists()


def test_a_daemon_that_stays_degraded_is_refused_and_names_its_components(
    staged_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_launch(monkeypatch, _FakeProcess())
    monkeypatch.setattr(
        launch_daemon,
        "daemon_health_failure",
        lambda url: "status 'degraded'; degraded components: llm_service",
    )

    with pytest.raises(EvidenceError, match="llm_service"):
        launch(staged_root, "attempt-1", SRT, home=tmp_path / "home")


def test_launch_probes_the_health_route_on_the_bootstrap_daemon_port(
    staged_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    urls: list[str] = []
    _patch_launch(monkeypatch, _FakeProcess())

    def healthy(url: str) -> None:
        urls.append(url)

    monkeypatch.setattr(launch_daemon, "daemon_health_failure", healthy)

    launch(staged_root, "attempt-1", SRT, home=tmp_path / "home")

    assert urls == ["http://127.0.0.1:59480/api/health"]


HEALTH_URL = "http://127.0.0.1:59480/api/health"


class _HealthAnswer:
    def __init__(self, status: int, body: bytes) -> None:
        self.status = status
        self._body = body

    def __enter__(self) -> _HealthAnswer:
        return self

    def __exit__(self, *_exc: object) -> None:
        return None

    def read(self) -> bytes:
        return self._body


def _answer_health(monkeypatch: pytest.MonkeyPatch, status: int, body: bytes) -> None:
    class _Opener:
        def open(self, url: str, timeout: float) -> _HealthAnswer:
            return _HealthAnswer(status, body)

    monkeypatch.setattr(urllib.request, "build_opener", lambda *_handlers: _Opener())


def test_a_health_answer_passes_only_when_its_status_is_ok(monkeypatch: pytest.MonkeyPatch) -> None:
    _answer_health(monkeypatch, 200, b'{"status": "ok", "degraded_services": []}')

    assert daemon_health_failure(HEALTH_URL) is None


def test_a_degraded_200_fails_and_names_its_degraded_services(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    body = {
        "status": "degraded",
        "degraded_services": ["llm_service", "memory_embeddings"],
        "install_dir": "/operator/checkout",
    }
    _answer_health(monkeypatch, 200, json.dumps(body).encode())

    failure = daemon_health_failure(HEALTH_URL)

    assert failure is not None
    assert "llm_service" in failure
    assert "memory_embeddings" in failure
    assert "/operator/checkout" not in failure


def test_an_incompatible_hook_runtime_fails_and_names_its_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    body = {
        "status": "degraded",
        "degraded_services": [],
        "hook_runtime": {"state": "stale_version", "compatible": False},
    }
    _answer_health(monkeypatch, 200, json.dumps(body).encode())

    failure = daemon_health_failure(HEALTH_URL)

    assert failure is not None
    assert "stale_version" in failure


def test_a_200_whose_body_is_not_json_is_not_healthy(monkeypatch: pytest.MonkeyPatch) -> None:
    _answer_health(monkeypatch, 200, b"<html>not the health route</html>")

    assert daemon_health_failure(HEALTH_URL) is not None


def test_an_http_error_status_is_not_healthy(monkeypatch: pytest.MonkeyPatch) -> None:
    class _Refusing:
        def open(self, url: str, timeout: float) -> _HealthAnswer:
            raise urllib.error.HTTPError(
                url, 503, "Service Unavailable", email.message.Message(), io.BytesIO()
            )

    monkeypatch.setattr(urllib.request, "build_opener", lambda *_handlers: _Refusing())

    failure = daemon_health_failure(HEALTH_URL)

    assert failure is not None
    assert "503" in failure


def test_srt_policy_allows_writes_only_under_the_runtime_root() -> None:
    filesystem = srt_policy(ROOT, HOME)["filesystem"]

    assert filesystem["allowWrite"] == ["/runtime/root"]
    assert filesystem["allowRead"] == ["/runtime/root"]
    assert filesystem["denyWrite"] == []


def test_srt_policy_denies_reads_of_the_five_operator_paths() -> None:
    filesystem = srt_policy(ROOT, HOME)["filesystem"]

    assert filesystem["denyRead"] == OPERATOR_DENY_READS


def test_srt_policy_permits_loopback_network_only() -> None:
    network = srt_policy(ROOT, HOME)["network"]

    assert network["allowedDomains"] == ["localhost", "127.0.0.1"]
    assert network["deniedDomains"] == []
    assert network["strictAllowlist"] is True
    assert network["allowLocalBinding"] is True
    assert network["allowUnixSockets"] == []
    assert network["allowAllUnixSockets"] is False


def test_srt_wrapped_argv_follows_the_runtime_wrap_shape() -> None:
    wrapped = srt_wrapped_argv(
        daemon_argv(ROOT),
        policy=ROOT / "srt/settings.json",
        violations=ROOT / "logs/violations.jsonl",
        srt=SRT,
    )

    assert wrapped == [
        "/opt/node/bin/node",
        "/opt/srt/0.0.79/runner.mjs",
        "--settings",
        "/runtime/root/srt/settings.json",
        "--violations",
        "/runtime/root/logs/violations.jsonl",
        "--",
        "/runtime/root/gobby-home/bin/gdaemon",
        "serve",
    ]


class _FakeProcess:
    """A srt wrapper (pid 4242) and one same-group descendant, as killpg and the wrapper see them.

    Each process exits when it receives a signal in its set. The group id is the wrapper's pid.
    """

    pid = 4242

    def __init__(
        self,
        *,
        wrapper_exits_on: frozenset[int] = frozenset({signal.SIGTERM}),
        descendant_exits_on: frozenset[int] = frozenset({signal.SIGTERM}),
        group_errors: dict[int, OSError] | None = None,
    ) -> None:
        self.wrapper_exits_on = wrapper_exits_on
        self.descendant_exits_on = descendant_exits_on
        self.group_errors: dict[int, OSError] = group_errors or {}
        self.returncode: int | None = None
        self.descendant_alive = True
        self.group_signals: list[tuple[int, int]] = []
        self.pid_signals: list[int] = []

    def killpg(self, pgid: int, number: int) -> None:
        if number == 0:
            if self.returncode is not None and not self.descendant_alive:
                raise ProcessLookupError(3, "No such process")
            return
        self.group_signals.append((pgid, number))
        error = self.group_errors.get(number)
        if error is not None:
            if isinstance(error, ProcessLookupError):
                self._vanish()
            raise error
        if number in self.wrapper_exits_on:
            self._exit_wrapper(number)
        if number in self.descendant_exits_on:
            self.descendant_alive = False

    def send_signal(self, number: int) -> None:
        self.pid_signals.append(number)
        if number in self.wrapper_exits_on:
            self._exit_wrapper(number)

    def kill(self) -> None:
        self.send_signal(signal.SIGKILL)

    def poll(self) -> int | None:
        return self.returncode

    def wait(self, timeout: float | None = None) -> int:
        if self.returncode is None:
            if timeout is not None:
                raise subprocess.TimeoutExpired(cmd="daemon", timeout=timeout)
            self._exit_wrapper(signal.SIGKILL)
        assert self.returncode is not None
        return self.returncode

    def _exit_wrapper(self, number: int) -> None:
        if self.returncode is None:
            self.returncode = -number

    def _vanish(self) -> None:
        self.descendant_alive = False
        self._exit_wrapper(signal.SIGKILL)


def _patch_launch(monkeypatch: pytest.MonkeyPatch, process: _FakeProcess) -> list[list[str]]:
    """Patch Popen, killpg and the probe socket. Returns the argv of every spawn."""
    spawns: list[list[str]] = []

    def popen(argv: list[str], **kwargs: object) -> _FakeProcess:
        spawns.append(argv)
        return process

    monkeypatch.setattr(subprocess, "Popen", popen)
    monkeypatch.setattr(os, "killpg", process.killpg)
    monkeypatch.setattr(socket, "socket", _FakeSocket)
    return spawns


BACKEND_GROUP = 5151


def _patch_backend_group(monkeypatch: pytest.MonkeyPatch, process: _FakeProcess) -> list[int]:
    """Give gdaemon's backend a group of its own, alive apart from the wrapper group.

    Returns the signal numbers that reached the backend group, probes included.
    """
    backend_signals: list[int] = []
    wrapper_killpg = process.killpg

    def killpg(pgid: int, number: int) -> None:
        if pgid == BACKEND_GROUP:
            backend_signals.append(number)
            return
        wrapper_killpg(pgid, number)

    monkeypatch.setattr(os, "killpg", killpg)
    return backend_signals


def _fail_before_writing(monkeypatch: pytest.MonkeyPatch, record: str) -> None:
    """Fail as a disk error before the named record reaches disk; other records are written."""
    real_write = provision_environment._write_json

    def fail_then_write(path: Path, value: Any, *, mode: int = 0o644) -> None:
        if path.name == record:
            raise OSError(f"disk full before {record} was written")
        real_write(path, value, mode=mode)

    monkeypatch.setattr(launch_daemon, "_write_json", fail_then_write)


def _fail_after_writing(monkeypatch: pytest.MonkeyPatch, record: str) -> None:
    """Write the named record in full, then fail as a disk error after the write would."""
    real_write = provision_environment._write_json

    def write_then_fail(path: Path, value: Any, *, mode: int = 0o644) -> None:
        real_write(path, value, mode=mode)
        if path.name == record:
            raise OSError(f"disk full after {record} was written")

    monkeypatch.setattr(launch_daemon, "_write_json", write_then_fail)


@pytest.fixture(autouse=True)
def wrapper_leads_its_group(monkeypatch: pytest.MonkeyPatch) -> None:
    """Launches lead their own group and answer healthy unless a test says otherwise."""
    monkeypatch.setattr(os, "getpgid", lambda pid: pid)
    monkeypatch.setattr(launch_daemon, "_pause", lambda seconds: None)
    monkeypatch.setattr(launch_daemon, "daemon_health_failure", lambda url: None)


class _FakeSocket:
    def __enter__(self) -> _FakeSocket:
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def bind(self, address: tuple[str, int]) -> None:
        return None


class _OccupiedSocket(_FakeSocket):
    """A probe socket for a port that already has a listener."""

    def bind(self, address: tuple[str, int]) -> None:
        raise OSError(errno.EADDRINUSE, "Address already in use")


@pytest.fixture
def staged_root(tmp_path: Path) -> Path:
    root = tmp_path / "runtime"
    (root / "gobby-home").mkdir(parents=True)
    (root / "gobby-home/bootstrap.yaml").write_text(
        "bind_host: 127.0.0.1\ndaemon_port: 59480\nwebsocket_port: 59481\n"
    )
    (root / "logs/install").mkdir(parents=True)
    (root / "receipts").mkdir()
    return root


@pytest.fixture
def spawned(monkeypatch: pytest.MonkeyPatch) -> list[list[str]]:
    argvs: list[list[str]] = []

    def fake_popen(argv: list[str], **kwargs: object) -> _FakeProcess:
        argvs.append(argv)
        return _FakeProcess()

    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    monkeypatch.setattr(socket, "socket", _FakeSocket)
    return argvs


def test_launch_runs_the_daemon_wrapped_by_srt_and_records_that_argv(
    staged_root: Path, tmp_path: Path, spawned: list[list[str]]
) -> None:
    launch(staged_root, "attempt-1", SRT, home=tmp_path / "home")

    argv = spawned[0]
    assert argv[:7] == [
        "/opt/node/bin/node",
        "/opt/srt/0.0.79/runner.mjs",
        "--settings",
        str(staged_root / "srt/settings-attempt-1.json"),
        "--violations",
        str(staged_root / "logs/violations-attempt-1.jsonl"),
        "--",
    ]
    assert argv[7:] == daemon_argv(staged_root)
    receipt = json.loads((staged_root / "receipts/daemon-launch-attempt-1.json").read_text())
    assert receipt["argv"] == argv
    assert receipt["daemon_argv"] == argv[7:]


def test_launch_hands_the_daemon_the_shared_monitor_socket_on_darwin(
    staged_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    envs: list[dict[str, str]] = []

    def fake_popen(argv: list[str], *, env: dict[str, str], **kwargs: object) -> _FakeProcess:
        envs.append(env)
        return _FakeProcess()

    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    monkeypatch.setattr(socket, "socket", _FakeSocket)
    monitored = SrtRunner(
        node=SRT.node,
        runner=SRT.runner,
        monitor_socket="/private/tmp/srt-monitor/v-abc",
    )

    launch(staged_root, "attempt-1", monitored, home=tmp_path / "home")

    assert envs[0]["GOBBY_SRT_MONITOR_SOCKET"] == "/private/tmp/srt-monitor/v-abc"
    assert envs[0]["GOBBY_SRT_RUN_KEY"] == "attempt-1"


def test_launch_receipt_names_the_monitor_socket_env_and_path(
    staged_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(subprocess, "Popen", lambda argv, **kwargs: _FakeProcess())
    monkeypatch.setattr(socket, "socket", _FakeSocket)
    monitored = SrtRunner(
        node=SRT.node, runner=SRT.runner, monitor_socket="/private/tmp/srt-monitor/v-abc"
    )

    launch(staged_root, "attempt-1", monitored, home=tmp_path / "home")

    sandbox = json.loads((staged_root / "receipts/daemon-launch-attempt-1.json").read_text())[
        "sandbox"
    ]
    assert sandbox["monitor_socket_env"] == "GOBBY_SRT_MONITOR_SOCKET"
    assert sandbox["monitor_socket"] == "/private/tmp/srt-monitor/v-abc"
    assert sandbox["run_key"] == "attempt-1"


def test_launch_indexes_the_daemon_receipt_as_the_active_launch(
    staged_root: Path, tmp_path: Path, spawned: list[list[str]]
) -> None:
    launch(staged_root, "attempt-1", SRT, home=tmp_path / "home")

    index = json.loads((staged_root / "receipts/active-launches.json").read_text())
    assert set(index) == {"daemon"}
    assert index["daemon"] == file_record(staged_root, "receipts/daemon-launch-attempt-1.json")
    verify_file_record(staged_root, index["daemon"])


def test_launch_refuses_a_root_with_an_active_launch_before_starting_a_daemon(
    staged_root: Path, tmp_path: Path, spawned: list[list[str]]
) -> None:
    launch(staged_root, "attempt-1", SRT, home=tmp_path / "home")

    with pytest.raises(EvidenceError) as caught:
        launch(staged_root, "attempt-2", SRT, home=tmp_path / "home")

    active = staged_root / "receipts/active-launches.json"
    receipt = staged_root / "receipts/daemon-launch-attempt-1.json"
    message = str(caught.value)
    assert str(active) in message
    assert str(receipt) in message
    assert "process group 4242" in message
    assert f"rm -- {active} {receipt}" in message
    assert len(spawned) == 1


def test_a_relaunch_is_refused_while_a_receipt_survives_without_an_active_index(
    staged_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    process = _FakeProcess()
    spawns = _patch_launch(monkeypatch, process)
    _patch_backend_group(monkeypatch, process)
    _fail_after_writing(monkeypatch, "daemon-launch-attempt-1.json")
    with pytest.raises(OSError, match="disk full"):
        launch(staged_root, "attempt-1", SRT, home=tmp_path / "home")
    monkeypatch.setattr(launch_daemon, "_write_json", provision_environment._write_json)

    with pytest.raises(EvidenceError, match="still on record") as caught:
        launch(staged_root, "attempt-2", SRT, home=tmp_path / "home")

    assert not (staged_root / "receipts/active-launches.json").exists()
    assert str(staged_root / "receipts/daemon-launch-attempt-1.json") in str(caught.value)
    assert "recorded process group 4242" in str(caught.value)
    assert len(spawns) == 1


def test_a_relaunch_is_refused_when_the_active_index_write_fails_before_its_file_exists(
    staged_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    process = _FakeProcess()
    spawns = _patch_launch(monkeypatch, process)
    _patch_backend_group(monkeypatch, process)
    _fail_before_writing(monkeypatch, "active-launches.json")
    with pytest.raises(OSError, match="disk full"):
        launch(staged_root, "attempt-1", SRT, home=tmp_path / "home")
    monkeypatch.setattr(launch_daemon, "_write_json", provision_environment._write_json)

    with pytest.raises(EvidenceError, match="still on record") as caught:
        launch(staged_root, "attempt-2", SRT, home=tmp_path / "home")

    assert not (staged_root / "receipts/active-launches.json").exists()
    assert str(staged_root / "receipts/daemon-launch-attempt-1.json") in str(caught.value)
    assert len(spawns) == 1


def test_a_relaunch_is_refused_when_the_receipt_write_fails_before_its_file_exists(
    staged_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    process = _FakeProcess()
    spawns = _patch_launch(monkeypatch, process)
    _patch_backend_group(monkeypatch, process)
    _fail_before_writing(monkeypatch, "daemon-launch-attempt-1.json")
    with pytest.raises(OSError, match="disk full"):
        launch(staged_root, "attempt-1", SRT, home=tmp_path / "home")
    monkeypatch.setattr(launch_daemon, "_write_json", provision_environment._write_json)

    with pytest.raises(EvidenceError, match="still on record") as caught:
        launch(staged_root, "attempt-2", SRT, home=tmp_path / "home")

    log = staged_root / "logs/install/daemon-attempt-1.log"
    assert not (staged_root / "receipts/daemon-launch-attempt-1.json").exists()
    assert str(log) in str(caught.value)
    assert "no receipt records a process group" in str(caught.value)
    assert len(spawns) == 1


def test_an_active_record_is_named_before_the_daemon_ports_are_probed(
    staged_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spawns = _patch_launch(monkeypatch, _FakeProcess())
    launch(staged_root, "attempt-1", SRT, home=tmp_path / "home")
    monkeypatch.setattr(socket, "socket", _OccupiedSocket)

    with pytest.raises(EvidenceError, match="still on record"):
        launch(staged_root, "attempt-2", SRT, home=tmp_path / "home")

    assert len(spawns) == 1


def test_launch_writes_a_root_confined_policy_and_an_empty_violation_log(
    staged_root: Path, tmp_path: Path, spawned: list[list[str]]
) -> None:
    launch(staged_root, "attempt-1", SRT, home=tmp_path / "home")

    policy_path = staged_root / "srt/settings-attempt-1.json"
    policy = json.loads(policy_path.read_text())
    assert policy == srt_policy(staged_root, tmp_path / "home")
    assert policy["filesystem"]["allowWrite"] == [str(staged_root)]
    violations_path = staged_root / "logs/violations-attempt-1.jsonl"
    assert violations_path.read_text() == ""
    assert policy_path.stat().st_mode & 0o777 == 0o600
    assert violations_path.stat().st_mode & 0o777 == 0o600


GOBBY_MODULE = "gobby.utils.native_bin"


def _module_copy(path: Path, body: bytes) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(body)
    return path


def test_matching_module_copies_pass_and_record_both_hashes(tmp_path: Path) -> None:
    epic = _module_copy(tmp_path / "epic/native_bin.py", b"same bytes")
    venv = _module_copy(tmp_path / "venv/native_bin.py", b"same bytes")

    record = launch_daemon.compare_module_copies(
        [GOBBY_MODULE], {GOBBY_MODULE: epic}, {GOBBY_MODULE: venv}
    )

    digest = hashlib.sha256(b"same bytes").hexdigest()
    assert record == {
        GOBBY_MODULE: {
            "epic_tree": {"path": str(epic), "sha256": digest},
            "runtime_venv": {"path": str(venv), "sha256": digest},
        }
    }


def test_mismatched_module_copy_stops_and_names_the_module(tmp_path: Path) -> None:
    epic = _module_copy(tmp_path / "epic/native_bin.py", b"epic bytes")
    venv = _module_copy(tmp_path / "venv/native_bin.py", b"runtime bytes")

    with pytest.raises(EvidenceError, match=r"gobby\.utils\.native_bin"):
        launch_daemon.compare_module_copies(
            [GOBBY_MODULE], {GOBBY_MODULE: epic}, {GOBBY_MODULE: venv}
        )


def test_missing_module_copy_stops_and_names_the_module(tmp_path: Path) -> None:
    epic = _module_copy(tmp_path / "epic/native_bin.py", b"same bytes")

    with pytest.raises(EvidenceError, match=r"gobby\.utils\.native_bin"):
        launch_daemon.compare_module_copies(
            [GOBBY_MODULE], {GOBBY_MODULE: epic}, {GOBBY_MODULE: None}
        )
    with pytest.raises(EvidenceError, match=r"gobby\.utils\.native_bin"):
        launch_daemon.compare_module_copies(
            [GOBBY_MODULE],
            {GOBBY_MODULE: epic},
            {GOBBY_MODULE: tmp_path / "venv/absent.py"},
        )


def test_provisioning_table_lists_exactly_the_step_module_set() -> None:
    doc = (Path(__file__).parents[1] / "provisioning.md").read_text()
    listed = {line.split("`")[1] for line in doc.splitlines() if line.startswith("| `gobby.")}

    assert listed == set(launch_daemon.GOBBY_STEP_MODULES)


def _origin_path(name: str) -> str:
    return "gobby/__init__.py" if name == "gobby" else name.replace(".", "/") + ".py"


def _package_tree(base: Path) -> Path:
    """A gobby package: its init, the four step modules, one module outside the step set, and the
    schema identity file that step 8 reads from the package."""
    for name in [*launch_daemon.GOBBY_STEP_MODULES, "gobby", "gobby.utils.text"]:
        _module_copy(base / _origin_path(name), b"same bytes")
    _module_copy(base / "gobby/storage/schema_expected_identity.json", b"same bytes")
    return base


def _patch_copies(monkeypatch: pytest.MonkeyPatch, epic: Path, venv: Path) -> None:
    monkeypatch.setattr(
        launch_daemon,
        "epic_origins",
        lambda names: {name: epic / _origin_path(name) for name in names},
    )

    def runtime(
        _root: Path, _env: dict[str, str], operation: str, payload: dict[str, Any]
    ) -> dict[str, Any]:
        if operation == "package-files":
            return {"files": runtime_gobby.hash_package_files(venv / "gobby")}
        return {"origins": {name: str(venv / _origin_path(name)) for name in payload["modules"]}}

    monkeypatch.setattr(launch_daemon, "run_runtime_gobby", runtime)


def test_module_identity_receipt_records_both_hashes_and_the_package_for_the_attempt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_copies(monkeypatch, _package_tree(tmp_path / "epic"), _package_tree(tmp_path / "venv"))
    root = tmp_path / "root"
    (root / "receipts").mkdir(parents=True)

    launch_daemon.verify_gobby_modules(root, "attempt-1", {})

    receipt = json.loads((root / "receipts/gobby-module-identity-attempt-1.json").read_text())
    assert set(receipt) == {"modules", "package"}
    for name in launch_daemon.GOBBY_STEP_MODULES:
        module = receipt["modules"][name]
        assert module["epic_tree"]["sha256"] == module["runtime_venv"]["sha256"]
    assert receipt["package"]["files"] == 7
    assert receipt["package"]["epic_tree_digest"] == receipt["package"]["runtime_venv_digest"]


def test_one_changed_byte_in_a_non_direct_module_fails_the_aggregate_check(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    epic = _package_tree(tmp_path / "epic")
    venv = _package_tree(tmp_path / "venv")
    (venv / "gobby/utils/text.py").write_bytes(b"same bytez")
    _patch_copies(monkeypatch, epic, venv)
    root = tmp_path / "root"
    (root / "receipts").mkdir(parents=True)

    with pytest.raises(EvidenceError, match=r"^gobby/utils/text\.py: epic-tree and tools"):
        launch_daemon.verify_gobby_modules(root, "attempt-1", {})
    assert not (root / "receipts/gobby-module-identity-attempt-1.json").exists()


def test_a_package_file_on_one_side_only_stops_and_names_the_first_such_path() -> None:
    with pytest.raises(EvidenceError, match=r"^gobby/b\.py: only in the epic-tree"):
        launch_daemon.compare_package_copies(
            {"gobby/a.py": "1", "gobby/b.py": "2"}, {"gobby/a.py": "1", "gobby/c.py": "3"}
        )


def test_matching_package_copies_record_both_digests_and_the_file_count() -> None:
    files = {"gobby/__init__.py": "aa", "gobby/utils/text.py": "bb"}

    record = launch_daemon.compare_package_copies(files, dict(files))

    assert record == {
        "files": 2,
        "epic_tree_digest": runtime_gobby.package_digest(files),
        "runtime_venv_digest": runtime_gobby.package_digest(files),
    }


def test_one_changed_byte_in_the_schema_identity_file_fails_the_aggregate_check(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    epic = _package_tree(tmp_path / "epic")
    venv = _package_tree(tmp_path / "venv")
    (venv / "gobby/storage/schema_expected_identity.json").write_bytes(b"same bytez")
    _patch_copies(monkeypatch, epic, venv)
    root = tmp_path / "root"
    (root / "receipts").mkdir(parents=True)

    with pytest.raises(
        EvidenceError,
        match=r"^gobby/storage/schema_expected_identity\.json: epic-tree and tools",
    ):
        launch_daemon.verify_gobby_modules(root, "attempt-1", {})
    assert not (root / "receipts/gobby-module-identity-attempt-1.json").exists()


def test_a_non_python_package_file_on_one_side_only_fails_naming_it(tmp_path: Path) -> None:
    epic = _package_tree(tmp_path / "epic")
    venv = _package_tree(tmp_path / "venv")
    _module_copy(epic / "gobby/install/shared/rule.yaml", b"rule")

    with pytest.raises(
        EvidenceError, match=r"^gobby/install/shared/rule\.yaml: only in the epic-tree"
    ):
        launch_daemon.compare_package_copies(
            runtime_gobby.hash_package_files(epic / "gobby"),
            runtime_gobby.hash_package_files(venv / "gobby"),
        )


@pytest.mark.parametrize("failing_record", ["daemon-launch-attempt-1.json", "active-launches.json"])
def test_launch_stops_an_unrecorded_daemon_and_keeps_its_partial_records(
    staged_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failing_record: str
) -> None:
    process = _FakeProcess()
    _patch_launch(monkeypatch, process)
    _fail_after_writing(monkeypatch, failing_record)

    with pytest.raises(OSError, match="disk full") as caught:
        launch(staged_root, "attempt-1", SRT, home=tmp_path / "home")

    assert process.group_signals == [(4242, signal.SIGTERM)]
    assert (staged_root / "receipts" / failing_record).exists()
    assert any("cleanup unproven" in note for note in caught.value.__notes__)


def test_launch_kills_a_daemon_that_ignores_sigterm(
    staged_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    process = _FakeProcess(
        wrapper_exits_on=frozenset({signal.SIGKILL}),
        descendant_exits_on=frozenset({signal.SIGKILL}),
    )
    _patch_launch(monkeypatch, process)
    _fail_after_writing(monkeypatch, "daemon-launch-attempt-1.json")

    with pytest.raises(OSError, match="disk full"):
        launch(staged_root, "attempt-1", SRT, home=tmp_path / "home")

    assert process.group_signals == [(4242, signal.SIGTERM), (4242, signal.SIGKILL)]
    assert (staged_root / "receipts/daemon-launch-attempt-1.json").exists()


def test_launch_sigkills_a_group_member_that_outlives_its_wrapper(
    staged_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    process = _FakeProcess(descendant_exits_on=frozenset({signal.SIGKILL}))
    _patch_launch(monkeypatch, process)
    _fail_after_writing(monkeypatch, "active-launches.json")

    with pytest.raises(OSError, match="disk full"):
        launch(staged_root, "attempt-1", SRT, home=tmp_path / "home")

    assert process.group_signals == [(4242, signal.SIGTERM), (4242, signal.SIGKILL)]
    assert (staged_root / "receipts/daemon-launch-attempt-1.json").exists()
    assert (staged_root / "receipts/active-launches.json").exists()


def test_a_sigkill_that_finds_the_group_gone_keeps_the_original_error(
    staged_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    process = _FakeProcess(
        wrapper_exits_on=frozenset({signal.SIGKILL}),
        descendant_exits_on=frozenset({signal.SIGKILL}),
        group_errors={signal.SIGKILL: ProcessLookupError(3, "No such process")},
    )
    _patch_launch(monkeypatch, process)
    _fail_after_writing(monkeypatch, "daemon-launch-attempt-1.json")

    with pytest.raises(OSError, match="disk full"):
        launch(staged_root, "attempt-1", SRT, home=tmp_path / "home")

    assert process.group_signals == [(4242, signal.SIGTERM), (4242, signal.SIGKILL)]
    assert (staged_root / "receipts/daemon-launch-attempt-1.json").exists()


def test_a_refused_group_signal_keeps_the_records_and_names_both_failures(
    staged_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    process = _FakeProcess(
        group_errors={signal.SIGTERM: PermissionError(1, "Operation not permitted")}
    )
    _patch_launch(monkeypatch, process)
    _fail_after_writing(monkeypatch, "active-launches.json")

    with pytest.raises(EvidenceError) as caught:
        launch(staged_root, "attempt-1", SRT, home=tmp_path / "home")

    assert "disk full" in str(caught.value)
    assert "Operation not permitted" in str(caught.value)
    assert (staged_root / "receipts/daemon-launch-attempt-1.json").exists()
    assert (staged_root / "receipts/active-launches.json").exists()


def test_a_group_that_survives_sigkill_is_not_reported_as_cleaned_up(
    staged_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    process = _FakeProcess(wrapper_exits_on=frozenset(), descendant_exits_on=frozenset())
    _patch_launch(monkeypatch, process)
    _fail_after_writing(monkeypatch, "active-launches.json")

    with pytest.raises(EvidenceError, match="survived SIGKILL"):
        launch(staged_root, "attempt-1", SRT, home=tmp_path / "home")

    assert (staged_root / "receipts/daemon-launch-attempt-1.json").exists()


def test_launch_keeps_its_records_when_the_backend_group_outlives_the_wrapper_group(
    staged_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    process = _FakeProcess()
    _patch_launch(monkeypatch, process)
    backend_signals = _patch_backend_group(monkeypatch, process)
    _fail_after_writing(monkeypatch, "active-launches.json")

    with pytest.raises(OSError, match="disk full") as caught:
        launch(staged_root, "attempt-1", SRT, home=tmp_path / "home")

    assert process.group_signals == [(4242, signal.SIGTERM)]
    assert not process.descendant_alive
    assert backend_signals == []
    assert (staged_root / "receipts/daemon-launch-attempt-1.json").exists()
    assert (staged_root / "receipts/active-launches.json").exists()
    assert any("cleanup unproven" in note for note in caught.value.__notes__)


def test_launch_refuses_a_wrapper_that_does_not_lead_its_own_group(
    staged_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    process = _FakeProcess()
    _patch_launch(monkeypatch, process)
    monkeypatch.setattr(os, "getpgid", lambda pid: 999)

    with pytest.raises(EvidenceError, match="does not lead"):
        launch(staged_root, "attempt-1", SRT, home=tmp_path / "home")

    assert process.group_signals == []
    assert process.pid_signals == [signal.SIGTERM]
    assert not (staged_root / "receipts/daemon-launch-attempt-1.json").exists()
    assert not (staged_root / "receipts/active-launches.json").exists()


def test_launch_receipt_records_the_observed_group_and_that_the_wrapper_leads_it(
    staged_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_launch(monkeypatch, _FakeProcess())

    launch(staged_root, "attempt-1", SRT, home=tmp_path / "home")

    receipt = json.loads((staged_root / "receipts/daemon-launch-attempt-1.json").read_text())
    assert receipt["process_group"] == {
        "wrapper_pid": 4242,
        "observed_pgid": 4242,
        "leads_own_group": True,
    }
