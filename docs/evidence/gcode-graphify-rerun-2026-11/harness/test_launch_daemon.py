"""The isolated daemon launch goes through the SRT runner with a policy confined to R."""

from __future__ import annotations

import json
import socket
import subprocess
from pathlib import Path

import pytest
from launch_daemon import SrtRunner, daemon_argv, launch, srt_policy, srt_wrapped_argv

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


def test_daemon_argv_runs_the_venv_gobby_runner_on_the_bootstrap_config() -> None:
    assert daemon_argv(ROOT, BOOTSTRAP) == [
        "/runtime/root/tools/gobby/venv/bin/python",
        "-m",
        "gobby.runner",
        "--config",
        "/runtime/root/gobby-home/bootstrap.yaml",
    ]


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
        daemon_argv(ROOT, BOOTSTRAP),
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
        "/runtime/root/tools/gobby/venv/bin/python",
        "-m",
        "gobby.runner",
        "--config",
        "/runtime/root/gobby-home/bootstrap.yaml",
    ]


class _FakeProcess:
    pid = 4242


class _FakeSocket:
    def __enter__(self) -> _FakeSocket:
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def bind(self, address: tuple[str, int]) -> None:
        return None


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
    assert argv[7:] == daemon_argv(staged_root, staged_root / "gobby-home/bootstrap.yaml")
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
