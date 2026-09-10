from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import signal
import socket
import sys
import tempfile
import time
from collections.abc import Mapping
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from urllib.parse import parse_qs, urlsplit

import pytest

from gobby.ask.runtime_validation import (
    ASK_NATIVE_PROBE_EXPECTATIONS,
    ASK_SRT_POLICY_SCHEMA_VERSION,
    AskRuntimeValidationArtifact,
    ask_runtime_control_digest,
    load_ask_runtime_validation,
    load_ask_runtime_validation_artifacts,
)
from tests.ask import native_probe_harness as harness

pytestmark = pytest.mark.unit

_TEST_DATABASE_URL = "postgresql://gobby_test:gobby_test@127.0.0.1:60892/gobby_test"
_SCHEMA = "gobby_test_askprobe_abcdef"
_SCOPED_TEST_DATABASE_URL = _TEST_DATABASE_URL + "?options=-csearch_path%3D" + _SCHEMA


def _canonical_hash(value: object) -> str:
    payload = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(payload).hexdigest()


def _provider(path: Path) -> Path:
    path.write_text("#!/bin/sh\nprintf '2.1.265\\n'\n", encoding="utf-8")
    path.chmod(0o755)
    return path


def _policy_fixture(tmp_path: Path) -> tuple[dict[str, object], dict[str, str]]:
    source_root = str((tmp_path / "source").resolve())
    scratch_root = str((tmp_path / "scratch").resolve())
    run_root = (tmp_path / "run").resolve()
    policy_path = str(run_root / "assets" / "settings.json")
    run_tmp_root = str(Path(tempfile.gettempdir()).resolve() / "gobby-12345678")
    policy: dict[str, object] = {
        "network": {
            "allowedDomains": [],
            "deniedDomains": ["*"],
            "strictAllowlist": True,
            "allowUnixSockets": [run_tmp_root],
            "allowAllUnixSockets": False,
            "allowLocalBinding": False,
        },
        "filesystem": {
            "denyRead": [str((tmp_path / "private").resolve())],
            "allowRead": [source_root, str(run_root / "assets")],
            "allowWrite": [scratch_root, run_tmp_root],
            "denyWrite": [source_root],
            "allowGitConfig": False,
        },
        "allowPty": False,
        "enableWeakerNestedSandbox": False,
        "enableWeakerNetworkIsolation": False,
        "allowAppleEvents": False,
    }
    return policy, {
        "source_root": source_root,
        "scratch_root": scratch_root,
        "policy_path": policy_path,
        "run_tmp_root": run_tmp_root,
    }


def _observations(executable: Path, tmp_path: Path) -> list[dict[str, Any]]:
    resolved = str(executable.resolve())
    executable_sha256 = hashlib.sha256(executable.read_bytes()).hexdigest()
    control_digest = ask_runtime_control_digest("claude", "claude.ai")
    policy, paths = _policy_fixture(tmp_path)
    policy_hash = _canonical_hash(policy)
    observations: list[dict[str, Any]] = []
    for phase in ("fresh", "resumed"):
        agent_run_id = f"{phase}-agent-run"
        for case, expected in ASK_NATIVE_PROBE_EXPECTATIONS.items():
            record: dict[str, Any] = {
                "phase": phase,
                "case": case,
                "observed": expected,
                "agent_run_id": agent_run_id,
                "session_id": f"{phase}-session",
                "terminal_id": f"{phase}-terminal",
                "process_id": 1001 if phase == "fresh" else 1002,
                "provider_executable": resolved,
                "provider_executable_sha256": executable_sha256,
                "provider_version": "2.1.265",
                "auth_mode": "claude.ai",
                "control_digest": control_digest,
                "policy_hash": policy_hash,
                "policy": policy,
                "srt_runtime_version": "0.0.66",
                "srt_policy_schema_version": ASK_SRT_POLICY_SCHEMA_VERSION,
                **paths,
            }
            if phase == "resumed":
                record["resumed_from_agent_run_id"] = "interrupted-agent-run"
            observations.append(
                {
                    "phase": phase,
                    "case": case,
                    "observed": expected,
                    "receipt": {
                        "source": (
                            "native_runtime"
                            if case in {"native_shell", "native_edit", "unrestricted_read", "web"}
                            else "mcp_response"
                        ),
                        "record": record,
                        "sha256": _canonical_hash(record),
                    },
                }
            )
    return observations


def test_parser_exposes_self_contained_driver_without_external_daemon_controls(
    tmp_path: Path,
) -> None:
    arguments = harness._parser().parse_args(
        [
            "contained-drive",
            "--project-root",
            str(tmp_path),
            "--output-dir",
            str(tmp_path / "evidence"),
        ]
    )

    assert arguments.command == "contained-drive"
    assert arguments.project_root == tmp_path
    assert not hasattr(arguments, "daemon_pid")
    assert not hasattr(arguments, "restart_command_json")


def test_bootstrap_loader_only_bypasses_prior_artifact_in_protected_test_runtime(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = _provider(tmp_path / "claude")
    marker_path = tmp_path / "bootstrap.json"
    artifact = harness._write_bootstrap_marker(marker_path)
    identity = harness.BootstrapPolicyIdentity(
        runtime_version="0.0.66",
        schema_version=ASK_SRT_POLICY_SCHEMA_VERSION,
        policy_digest="d" * 64,
    )

    monkeypatch.delenv("GOBBY_TEST_PROTECT", raising=False)
    monkeypatch.setenv("DATABASE_URL", _TEST_DATABASE_URL)
    with pytest.raises(ValueError, match="loopback test database"):
        harness._load_bootstrap_runtime_validation(
            artifact,
            provider_executable=provider,
            policy_identity=identity,
        )

    monkeypatch.setenv("GOBBY_TEST_PROTECT", "1")
    with pytest.raises(ValueError, match="unique test schema"):
        harness._load_bootstrap_runtime_validation(
            artifact,
            provider_executable=provider,
            policy_identity=identity,
        )

    monkeypatch.setenv("DATABASE_URL", _SCOPED_TEST_DATABASE_URL)
    validation = harness._load_bootstrap_runtime_validation(
        artifact,
        provider_executable=provider,
        policy_identity=identity,
    )

    assert validation.provider == "claude"
    assert validation.provider_executable == str(provider.resolve())
    assert validation.provider_version == "2.1.265"
    assert validation.srt_runtime_version == "0.0.66"
    assert validation.srt_policy_schema_version == ASK_SRT_POLICY_SCHEMA_VERSION
    assert validation.srt_policy_digest == "d" * 64
    with pytest.raises(ValueError, match="schema is unsupported"):
        load_ask_runtime_validation(
            AskRuntimeValidationArtifact(path=marker_path, sha256=artifact.sha256),
            provider_executable=provider,
        )


@pytest.mark.asyncio
async def test_bootstrap_policy_uses_the_production_ask_scratch_workspace(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from gobby.agents.srt_runtime import SandboxLaunch
    from gobby.ask.runtime_validation import ask_sandbox_config

    project_root = tmp_path / "project"
    scratch_root = tmp_path / "scratch"
    project_root.mkdir()
    policy_path = tmp_path / "settings.json"
    policy_path.write_text('{"sandbox": {"enabled": true}}', encoding="utf-8")
    captured: dict[str, object] = {}

    async def prepare_sandbox_launch(**kwargs: object) -> SandboxLaunch:
        captured.update(kwargs)
        return SandboxLaunch(
            backend="srt",
            enforced=True,
            runtime_version="1.2.3",
            policy_schema_version=7,
            policy_path=str(policy_path),
            provider_env={"CLAUDE_CODE_TMPDIR": str(tmp_path / "run-tmp")},
        )

    monkeypatch.setattr(
        "gobby.agents.srt_runtime.prepare_sandbox_launch",
        prepare_sandbox_launch,
    )
    monkeypatch.setattr(
        harness,
        "normalized_ask_srt_policy_digest",
        lambda *_args, **_kwargs: "d",
    )

    identity = await harness._bootstrap_policy_identity(
        project_root=project_root,
        scratch_root=scratch_root,
        daemon_port=60887,
        websocket_port=60888,
    )

    assert captured["workspace_path"] == str(scratch_root)
    assert captured["config"] == ask_sandbox_config(str(project_root), str(scratch_root))
    assert identity.policy_digest == "d"


def test_probe_database_scope_and_worker_environment_are_owned(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GOBBY_TEST_PROTECT", "1")
    with pytest.raises(ValueError, match="loopback test database"):
        harness._protected_database_url(
            "postgresql://gobby_test:gobby_test@example.com:60892/gobby_test",
            require_unique_schema=False,
        )

    scoped = harness._scoped_database_url(_TEST_DATABASE_URL, _SCHEMA)
    assert parse_qs(urlsplit(scoped).query)["options"] == [f"-csearch_path={_SCHEMA}"]
    assert harness._protected_database_url(scoped, require_unique_schema=True) == _SCHEMA
    with pytest.raises(ValueError, match="unscoped"):
        harness._protected_database_url(scoped, require_unique_schema=False)

    monkeypatch.setenv("HOME", str(tmp_path / "real-home"))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "secret")
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "oauth")
    monkeypatch.setenv("GOBBY_SESSION_ID", "spoofed")
    monkeypatch.setenv("GOBBY_MACHINE_ID", "spoofed-machine")
    environment = harness._worker_environment(scoped, tmp_path / "gobby", "machine-id")

    assert environment["HOME"] == str(tmp_path / "real-home")
    assert environment["GOBBY_MACHINE_ID"] == "machine-id"
    assert environment["DATABASE_URL"] == scoped
    assert environment["GOBBY_TEST_PROTECT"] == "1"
    assert "ANTHROPIC_API_KEY" not in environment
    assert "CLAUDE_CODE_OAUTH_TOKEN" not in environment
    assert "GOBBY_SESSION_ID" not in environment


def test_contained_config_pins_gterm_host_to_owned_runtime(tmp_path: Path) -> None:
    gobby_home = tmp_path / "runtime" / "gobby"

    config_path = harness._write_contained_config(
        gobby_home,
        database_url=_SCOPED_TEST_DATABASE_URL,
        daemon_port=19001,
        websocket_port=19002,
    )

    config = config_path.read_text(encoding="utf-8")
    assert "terminals:\n  stop_host_on_shutdown: true" in config
    assert "terminal_host:\n  enabled: true" in config
    assert f'  socket_dir: "{(gobby_home.parent / "gterm-host").resolve()}"' in config


def test_owned_runtime_root_requires_its_exact_marker_for_removal(tmp_path: Path) -> None:
    runtime_root = harness._create_owned_runtime_root(parent=tmp_path)
    marker = json.loads((runtime_root / ".native-ask-probe-root.json").read_bytes())
    sibling = tmp_path / "gobby-ap-not-owned"
    sibling.mkdir(mode=0o700)

    assert marker["root"] == str(runtime_root)
    assert marker["purpose"] == "native-ask-runtime-probe-root"
    if sys.platform == "darwin":
        assert harness._owned_runtime_parent() == Path("/tmp").resolve(strict=True)
    with pytest.raises(RuntimeError, match="unowned Ask probe runtime root"):
        harness._remove_owned_runtime_root(sibling)
    harness._remove_owned_runtime_root(runtime_root)

    assert not runtime_root.exists()
    assert sibling.is_dir()


def test_runtime_identity_requires_branch_local_gcode_and_records_gterm(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_root = tmp_path / "project"
    native_bin_dir = project_root / "target" / "debug"
    fixture = project_root / "tests" / "ask" / "fixtures" / "native_ask_probe_hostile.txt"
    native_bin_dir.mkdir(parents=True)
    fixture.parent.mkdir(parents=True)
    fixture.write_text(f"{harness._HOSTILE_FIXTURE_MARKER}\n", encoding="utf-8")
    binaries = {name: native_bin_dir / name for name in ("gcode", "gterm")}
    for binary in binaries.values():
        binary.write_bytes(f"{binary.name}-binary".encode())
        binary.chmod(0o700)

    monkeypatch.setenv("GOBBY_NATIVE_BIN_DIR", str(native_bin_dir))
    monkeypatch.setattr(
        "gobby.utils.native_bin.resolve_native_bin",
        lambda name: str(binaries[name]),
    )
    monkeypatch.setattr(
        "gobby.utils.git.run_git_command",
        lambda *_args, **_kwargs: "f" * 40,
    )
    monkeypatch.setattr(
        harness,
        "_provider_identity",
        lambda path: (str(path.resolve()), hashlib.sha256(path.read_bytes()).hexdigest(), "1.7.0"),
    )

    identity = harness._capture_runtime_identity(project_root)
    gcode_identity = cast(dict[str, Any], identity["gcode"])
    gterm_identity = cast(dict[str, Any], identity["gterm"])

    assert identity["source_head"] == "f" * 40
    assert gcode_identity["path"] == str(binaries["gcode"].resolve())
    assert gcode_identity["version"] == "1.7.0"
    assert gterm_identity["path"] == str(binaries["gterm"].resolve())
    assert gterm_identity["version"] is None

    foreign_gcode = tmp_path / "foreign-gcode"
    foreign_gcode.write_bytes(b"foreign")
    foreign_gcode.chmod(0o700)
    monkeypatch.setattr(
        "gobby.utils.native_bin.resolve_native_bin",
        lambda name: str(foreign_gcode) if name == "gcode" else str(binaries[name]),
    )
    with pytest.raises(RuntimeError, match="branch-local gcode"):
        harness._capture_runtime_identity(project_root)


def test_terminal_host_launch_snapshot_binds_owned_socket_and_process_group(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime_root = Path(tempfile.mkdtemp(prefix="ga-")).resolve()
    socket_dir = runtime_root
    control_path = socket_dir / "gterm-control.sock"
    frames_path = socket_dir / "gterm-frames.sock"
    pidfile = socket_dir / "gterm.pid"
    pidfile.write_text("4321", encoding="utf-8")
    control_socket = socket.socket(socket.AF_UNIX)
    frames_socket = socket.socket(socket.AF_UNIX)
    control_socket.bind(str(control_path))
    frames_socket.bind(str(frames_path))
    manager = SimpleNamespace(
        socket_dir=socket_dir,
        host_pid=4321,
        host_epoch="host-epoch",
        native_available=True,
        running=True,
        spawned_this_construction=True,
        adopted=False,
    )
    runner = SimpleNamespace(terminal_host_manager=manager)
    monkeypatch.setattr(harness, "_process_start_identity", lambda _pid: "host-start")
    monkeypatch.setattr("tests.ask.native_probe_harness.os.getpgid", lambda pid: pid)
    monkeypatch.setattr(
        harness,
        "_process_group_snapshot",
        lambda _pgid: [
            {
                "pid": 4321,
                "ppid": 1,
                "pgid": 4321,
                "start_identity": "host-start",
            }
        ],
        raising=False,
    )

    try:
        snapshot = harness._terminal_host_launch_snapshot(
            runner,
            phase="fresh",
            runtime_root=runtime_root,
        )
    finally:
        control_socket.close()
        frames_socket.close()
        shutil.rmtree(runtime_root)

    assert snapshot["workers"] == []
    assert snapshot["agents"] == []
    hosts = cast(list[dict[str, Any]], snapshot["hosts"])
    assert len(hosts) == 1
    host = hosts[0]
    assert host["socket_dir"] == str(socket_dir.resolve())
    assert host["control_socket"] == str(control_path.resolve())
    assert host["frames_socket"] == str(frames_path.resolve())
    assert host["pidfile"] == str(pidfile.resolve())
    assert host["host_pid"] == 4321
    assert host["start_identity"] == "host-start"
    assert host["pgid"] == 4321
    assert host["spawned_this_construction"] is True
    assert host["adopted"] is False
    assert host["process_group"] == [
        {
            "pid": 4321,
            "ppid": 1,
            "pgid": 4321,
            "start_identity": "host-start",
        }
    ]


def test_terminal_host_recovery_accepts_adoption_or_proven_dead_predecessor() -> None:
    predecessor = {
        "workers": [],
        "agents": [],
        "hosts": [
            {
                "phase": "resumed",
                "socket_dir": "/owned/gterm-host",
                "host_pid": 4321,
                "start_identity": "host-start",
                "host_epoch": "host-epoch",
                "spawned_this_construction": True,
                "adopted": False,
            }
        ],
    }
    surviving = {
        "workers": [],
        "agents": [],
        "hosts": [
            {
                "phase": "resumed",
                "socket_dir": "/owned/gterm-host",
                "host_pid": 4321,
                "start_identity": "host-start",
                "host_epoch": "host-epoch",
                "live": True,
                "control_socket_exists": True,
                "frames_socket_exists": True,
                "process_group": [{"pid": 4321}],
            }
        ],
    }
    adopted = {
        "workers": [],
        "agents": [],
        "hosts": [
            {
                "phase": "recover",
                "socket_dir": "/owned/gterm-host",
                "host_pid": 4321,
                "start_identity": "host-start",
                "host_epoch": "host-epoch",
                "spawned_this_construction": False,
                "adopted": True,
            }
        ],
    }

    assert harness._assert_terminal_host_recovery(predecessor, surviving, adopted) == "adopted"

    absent = {
        "workers": [],
        "agents": [],
        "hosts": [
            {
                "phase": "resumed",
                "socket_dir": "/owned/gterm-host",
                "host_pid": 4321,
                "start_identity": "host-start",
                "host_epoch": "host-epoch",
                "live": False,
                "control_socket_exists": False,
                "frames_socket_exists": False,
                "process_group": [],
            }
        ],
    }
    restarted = {
        "workers": [],
        "agents": [],
        "hosts": [
            {
                "phase": "recover",
                "socket_dir": "/owned/gterm-host",
                "host_pid": 5000,
                "start_identity": "new-host-start",
                "host_epoch": "new-host-epoch",
                "spawned_this_construction": True,
                "adopted": False,
            }
        ],
    }

    assert harness._assert_terminal_host_recovery(predecessor, absent, restarted) == "restarted"
    unknown = json.loads(json.dumps(absent))
    cast(list[dict[str, Any]], unknown["hosts"])[0]["live"] = None
    with pytest.raises(RuntimeError, match="predecessor absence is unproven"):
        harness._assert_terminal_host_recovery(predecessor, unknown, restarted)


def test_terminal_host_cleanup_kills_descendants_and_removes_owned_sockets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime_root = Path(tempfile.gettempdir()).resolve()
    socket_dir = Path(tempfile.mkdtemp(prefix="h-", dir=runtime_root)).resolve()
    control_path = socket_dir / "gterm-control.sock"
    frames_path = socket_dir / "gterm-frames.sock"
    pidfile = socket_dir / "gterm.pid"
    control_socket = socket.socket(socket.AF_UNIX)
    frames_socket = socket.socket(socket.AF_UNIX)
    control_socket.bind(str(control_path))
    frames_socket.bind(str(frames_path))
    pidfile.write_text("4321", encoding="utf-8")
    launch_group = [
        {"pid": 4321, "ppid": 1, "pgid": 4321, "start_identity": "host-start"},
        {"pid": 4322, "ppid": 4321, "pgid": 4321, "start_identity": "child-start"},
    ]
    snapshot = {
        "workers": [],
        "agents": [],
        "hosts": [
            {
                "phase": "resumed",
                "socket_dir": str(socket_dir),
                "control_socket": str(control_path),
                "frames_socket": str(frames_path),
                "pidfile": str(pidfile),
                "host_pid": 4321,
                "start_identity": "host-start",
                "pgid": 4321,
                "host_epoch": "host-epoch",
                "spawned_this_construction": True,
                "adopted": False,
                "process_group": launch_group,
            }
        ],
    }
    group_snapshots = iter(
        [
            launch_group,
            [
                {
                    "pid": 4322,
                    "ppid": 1,
                    "pgid": 4321,
                    "start_identity": "child-start",
                }
            ],
            [],
            [],
        ]
    )
    signals: list[tuple[int, int]] = []
    identities = {4321: "host-start", 4322: "child-start"}
    monkeypatch.setattr(harness, "_process_group_snapshot", lambda _pgid: next(group_snapshots))
    monkeypatch.setattr(
        harness,
        "_process_start_identity",
        lambda pid: identities.get(pid),
    )
    monkeypatch.setattr("tests.ask.native_probe_harness.os.getpgid", lambda _pid: 4321)

    def kill_group(pgid: int, process_signal: int) -> None:
        signals.append((pgid, process_signal))
        if process_signal == signal.SIGKILL:
            identities.clear()

    monkeypatch.setattr(
        "tests.ask.native_probe_harness.os.killpg",
        kill_group,
    )

    try:
        result = harness._terminate_owned_host_process(
            snapshot,
            deadline_monotonic=time.monotonic(),
            runtime_root=runtime_root,
        )
    finally:
        control_socket.close()
        frames_socket.close()
        shutil.rmtree(socket_dir)

    assert result["status"] == "terminated"
    assert cast(list[dict[str, object]], result["group_before"])[1]["pid"] == 4322
    assert result["group_after"] == []
    assert result["removed_sockets"] == [str(control_path), str(frames_path)]
    assert signals == [(4321, signal.SIGTERM), (4321, signal.SIGKILL)]


def test_terminal_host_cleanup_rejects_stale_receipt_for_live_successor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime_root = Path(tempfile.gettempdir()).resolve()
    socket_dir = Path(tempfile.mkdtemp(prefix="h-", dir=runtime_root)).resolve()
    control_path = socket_dir / "gterm-control.sock"
    frames_path = socket_dir / "gterm-frames.sock"
    pidfile = socket_dir / "gterm.pid"
    control_socket = socket.socket(socket.AF_UNIX)
    frames_socket = socket.socket(socket.AF_UNIX)
    control_socket.bind(str(control_path))
    frames_socket.bind(str(frames_path))
    pidfile.write_text("5000", encoding="utf-8")
    snapshot = {
        "workers": [],
        "agents": [],
        "hosts": [
            {
                "phase": "resumed",
                "socket_dir": str(socket_dir),
                "control_socket": str(control_path),
                "frames_socket": str(frames_path),
                "pidfile": str(pidfile),
                "host_pid": 4321,
                "start_identity": "old-host-start",
                "pgid": 4321,
                "host_epoch": "old-host-epoch",
                "spawned_this_construction": True,
                "adopted": False,
                "process_group": [
                    {
                        "pid": 4321,
                        "ppid": 1,
                        "pgid": 4321,
                        "start_identity": "old-host-start",
                    }
                ],
            }
        ],
    }
    signals: list[tuple[int, int]] = []
    monkeypatch.setattr(harness, "_process_group_snapshot", lambda _pgid: [])
    monkeypatch.setattr(
        harness,
        "_process_start_identity",
        lambda pid: "successor-start" if pid == 5000 else None,
    )
    monkeypatch.setattr(
        "tests.ask.native_probe_harness.os.killpg",
        lambda pgid, process_signal: signals.append((pgid, process_signal)),
    )

    try:
        with pytest.raises(RuntimeError, match="stale terminal host receipt"):
            harness._terminate_owned_host_process(
                snapshot,
                deadline_monotonic=time.monotonic(),
                runtime_root=runtime_root,
            )

        assert control_path.exists()
        assert frames_path.exists()
        assert pidfile.read_text(encoding="utf-8") == "5000"
        assert signals == []
    finally:
        control_socket.close()
        frames_socket.close()
        shutil.rmtree(socket_dir)


def test_wait_for_first_agent_requires_a_running_native_process(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed: list[list[str]] = [[], ["not-live"], ["not-live", "live"]]

    def run_ids(_database_url: str, _ask_run_id: str) -> list[str]:
        return observed.pop(0)

    monkeypatch.setattr(harness, "_ask_run_ids", run_ids)
    monkeypatch.setattr(
        harness,
        "_agent_is_live",
        lambda _database_url, agent_run_id: agent_run_id == "live",
    )
    monkeypatch.setattr(harness, "_ask_execution_status", lambda *_args: "running")
    monkeypatch.setattr("tests.ask.native_probe_harness.time.sleep", lambda _seconds: None)

    assert (
        harness._wait_for_first_agent(
            _SCOPED_TEST_DATABASE_URL,
            "ask-run",
            deadline_monotonic=time.monotonic() + 1.0,
        )
        == "live"
    )
    assert observed == []


def test_wait_for_first_agent_fails_if_run_finishes_before_fault(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(harness, "_ask_run_ids", lambda *_args: [])
    monkeypatch.setattr(harness, "_ask_execution_status", lambda *_args: "completed")

    with pytest.raises(RuntimeError, match="completed before fault injection"):
        harness._wait_for_first_agent(
            _SCOPED_TEST_DATABASE_URL,
            "ask-run",
            deadline_monotonic=time.monotonic() + 1.0,
        )


def _execution_snapshot(
    *,
    run_id: str,
    deadline: str,
    status: str,
    current_agent: str,
    superseded: list[str],
    include_publish: bool,
) -> dict[str, object]:
    steps: list[dict[str, object]] = [
        {
            "id": "step-prepare",
            "step_id": "prepare",
            "status": "completed",
            "output_json": {"snapshot": "stable"},
        },
        {
            "id": "step-investigate",
            "step_id": "investigate",
            "status": "completed" if include_publish else "running",
            "output_json": {"draft": "accepted"} if include_publish else None,
        },
    ]
    if include_publish:
        steps.append(
            {
                "id": "step-publish",
                "step_id": "publish",
                "status": "completed",
                "output_json": {"status": "complete"},
            }
        )
    return {
        "execution": {
            "id": run_id,
            "project_id": "project-id",
            "pipeline_name": "native-ask",
            "status": status,
            "definition_json": {"name": "native-ask", "version": 1},
            "inputs_json": {
                "run_id": run_id,
                "project_id": "project-id",
                "caller_session_id": "caller-id",
                "ask": {
                    "binding": {"deadline_at": deadline},
                    "execution_context": {
                        "run_id": run_id,
                        "project_id": "project-id",
                        "caller_session_id": "caller-id",
                        "project_root": "/source",
                    },
                    "runtime": {
                        "authorities": {
                            "investigate": {
                                "lifecycle": {
                                    "current_agent_run_id": current_agent,
                                    "superseded_agent_run_ids": superseded,
                                }
                            }
                        }
                    },
                },
            },
        },
        "steps": steps,
        "agent_run_ids": [*superseded, current_agent],
    }


def test_recovery_invariants_preserve_deadline_and_completed_checkpoints() -> None:
    before = _execution_snapshot(
        run_id="ask-run",
        deadline="2026-09-10T12:10:00Z",
        status="running",
        current_agent="interrupted-agent",
        superseded=[],
        include_publish=False,
    )
    after = _execution_snapshot(
        run_id="ask-run",
        deadline="2026-09-10T12:10:00Z",
        status="completed",
        current_agent="successor-agent",
        superseded=["interrupted-agent"],
        include_publish=True,
    )

    harness._assert_recovery_invariants(
        before,
        after,
        interrupted_agent_run_id="interrupted-agent",
    )

    changed_deadline = json.loads(json.dumps(after))
    changed_deadline["execution"]["inputs_json"]["ask"]["binding"]["deadline_at"] = (
        "2026-09-10T12:20:00Z"
    )
    with pytest.raises(RuntimeError, match="deadline"):
        harness._assert_recovery_invariants(
            before,
            changed_deadline,
            interrupted_agent_run_id="interrupted-agent",
        )


def test_recovery_invariants_allow_one_live_child_to_reattach() -> None:
    before = _execution_snapshot(
        run_id="ask-run",
        deadline="2026-09-10T12:10:00Z",
        status="running",
        current_agent="reattached-agent",
        superseded=[],
        include_publish=False,
    )
    after = _execution_snapshot(
        run_id="ask-run",
        deadline="2026-09-10T12:10:00Z",
        status="completed",
        current_agent="reattached-agent",
        superseded=[],
        include_publish=True,
    )

    harness._assert_recovery_invariants(
        before,
        after,
        interrupted_agent_run_id="reattached-agent",
    )


class _Cursor:
    def __init__(self, rows: list[dict[str, object]]) -> None:
        self._rows = rows

    def fetchone(self) -> dict[str, object] | None:
        return self._rows[0] if self._rows else None

    def fetchall(self) -> list[dict[str, object]]:
        return self._rows


class _Connection:
    def __init__(
        self,
        snapshots: Mapping[str, dict[str, object]],
        agent_rows: list[dict[str, object]],
    ) -> None:
        self.snapshots = snapshots
        self.agent_rows = agent_rows

    def __enter__(self) -> _Connection:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def execute(self, query: str, parameters: tuple[object, ...]) -> _Cursor:
        run_id = str(parameters[0])
        if "pipeline_executions AS execution" in query:
            execution = self.snapshots[run_id]["execution"]
            assert isinstance(execution, dict)
            return _Cursor([{"record": execution}])
        if "step_executions AS step" in query:
            steps = self.snapshots[run_id]["steps"]
            assert isinstance(steps, list)
            return _Cursor([{"record": step} for step in steps])
        if "agent_runs AS agent" in query:
            return _Cursor(self.agent_rows)
        raise AssertionError(query)


@pytest.mark.parametrize(
    "receipt_state",
    [
        "complete",
        "missing_transcript",
        "missing_policy",
        "absent_violations",
        "wrong_machine",
    ],
)
def test_raw_export_hashes_owned_receipt_bytes_and_strips_secret_fields(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    receipt_state: str,
) -> None:
    runtime_root = tmp_path / "runtime"
    transcript = tmp_path / "provider-home" / "agent.jsonl"
    policy = runtime_root / "control" / "launch-artifacts" / "agent-run" / "settings.json"
    violations = runtime_root / "gobby" / "logs" / "sandbox-violations" / "agent-run.jsonl"
    transcript.parent.mkdir(parents=True)
    policy.parent.mkdir(parents=True)
    violations.parent.mkdir(parents=True)
    transcript.write_bytes(b'{"mcp_response":"denied"}\n')
    policy.write_bytes(b'{"policy":"exact"}\n')
    violations.write_bytes(b'{"violation":"shell"}\n')
    launch_receipts = runtime_root / "control" / "launch-receipts"
    launch_receipts.mkdir(parents=True)
    (launch_receipts / "agent-run.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "agent_run_id": "agent-run",
                "ask_run_id": "ask-run",
                "project_id": "project-id",
                "provider": "claude",
                "child_session_id": "session-id",
                "pid": 123,
                "terminal_id": "terminal-id",
                "start_identity": "os-start",
                "policy": {"captured_path": str(policy)},
                "violation": {"retained_path": str(violations)},
            }
        ),
        encoding="utf-8",
    )
    snapshot = _execution_snapshot(
        run_id="ask-run",
        deadline="2026-09-10T12:10:00Z",
        status="completed",
        current_agent="agent-run",
        superseded=[],
        include_publish=True,
    )
    agent_rows = [
        {
            "agent": {
                "id": "agent-run",
                "workflow_name": "native-ask",
                "machine_id": "owned-machine",
                "pid": 123,
                "terminal_id": "terminal-id",
                "child_session_id": "session-id",
                "resume_metadata_json": {
                    "env": {"ANTHROPIC_API_KEY": "must-not-export", "SAFE": "ok"},
                    "provider": "claude",
                    "project_id": "project-id",
                    "workflow": "native-ask",
                    "provider_native_session_id": "claude-native-id",
                    "initial_variables": {"ask_run_id": "ask-run"},
                    "sandbox": {
                        "policy_path": str(runtime_root / "reaped" / "settings.json"),
                        "violation_path": str(runtime_root / "reaped" / "violations.jsonl"),
                    },
                },
            },
            "session": {
                "id": "session-id",
                "machine_id": "other-machine"
                if receipt_state == "wrong_machine"
                else "owned-machine",
                "source": "claude",
                "project_id": "project-id",
                "external_id": "claude-native-id",
                "transcript_path": str(transcript),
            },
        }
    ]
    connection = _Connection({"ask-run": snapshot}, agent_rows)
    monkeypatch.setattr(
        "tests.ask.native_probe_harness.psycopg.connect",
        lambda *_args, **_kwargs: connection,
    )
    output_dir = tmp_path / "evidence"
    if receipt_state == "missing_transcript":
        transcript.unlink()
    elif receipt_state == "missing_policy":
        policy.unlink()
    elif receipt_state == "absent_violations":
        violations.unlink()

    result = harness._export_raw(
        _SCOPED_TEST_DATABASE_URL,
        ["ask-run"],
        output_dir,
        runtime_root=runtime_root,
        process_sets={"after": {"workers": [], "agents": []}},
        runtime_identity={"source_head": "f" * 40},
    )

    exported = json.loads((output_dir / "raw-probe.json").read_bytes())
    encoded = (output_dir / "raw-probe.json").read_bytes()
    assert result["sha256"] == hashlib.sha256(encoded).hexdigest()
    assert (output_dir / "raw-probe.sha256").read_text(encoding="utf-8").strip() == result["sha256"]
    expected_count = (
        3 if receipt_state == "complete" else (0 if receipt_state == "wrong_machine" else 2)
    )
    assert len(exported["receipts"]) == expected_count
    assert result["complete"] is (receipt_state in {"complete", "absent_violations"})
    assert all(Path(receipt["output_path"]).is_file() for receipt in exported["receipts"])
    assert "must-not-export" not in encoded.decode()
    assert exported["runtime_identity"] == {"source_head": "f" * 40}
    assert exported["agent_runs"][0]["agent"]["resume_metadata_json"]["env"] == {"SAFE": "ok"}


def test_launch_receipt_captures_policy_before_runtime_reap(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime_root = tmp_path / "runtime"
    control_dir = runtime_root / "control"
    policy = runtime_root / "srt" / "assets" / "settings.json"
    violation = runtime_root / "srt" / "logs" / "violations.jsonl"
    policy.parent.mkdir(parents=True)
    violation.parent.mkdir(parents=True)
    policy.write_bytes(b'{"policy":"pre-reap"}\n')
    violation.write_bytes(b"")
    row = {
        "agent": {
            "id": "agent-run",
            "workflow_name": "native-ask",
            "status": "running",
            "pid": 4321,
            "terminal_id": "terminal-id",
            "child_session_id": "session-id",
            "resume_metadata_json": {
                "provider": "claude",
                "project_id": "project-id",
                "workflow": "native-ask",
                "initial_variables": {"ask_run_id": "ask-run"},
                "sandbox": {
                    "policy_path": str(policy),
                    "violation_path": str(violation),
                },
            },
        },
        "session": {
            "id": "session-id",
            "source": "claude",
            "project_id": "project-id",
        },
        "terminal": {"id": "terminal-id", "process": {"pgid": 4321, "start_time": 99}},
    }
    monkeypatch.setattr(harness, "_agent_receipt_row", lambda *_args: row)
    monkeypatch.setattr(harness, "_process_start_identity", lambda _pid: "os-start")
    monkeypatch.setattr("tests.ask.native_probe_harness.os.getpgid", lambda pid: pid)
    monkeypatch.setattr(
        harness,
        "_process_group_snapshot",
        lambda _pgid: [{"pid": 4321, "ppid": 1, "pgid": 4321, "start_identity": "os-start"}],
    )

    harness._capture_agent_launch_receipt(
        _SCOPED_TEST_DATABASE_URL,
        "agent-run",
        ask_run_id="ask-run",
        project_id="project-id",
        control_dir=control_dir,
    )
    shutil.rmtree(runtime_root / "srt")

    manifest = json.loads((control_dir / "launch-receipts" / "agent-run.json").read_bytes())
    captured_policy = Path(manifest["policy"]["captured_path"])
    assert captured_policy.read_bytes() == b'{"policy":"pre-reap"}\n'
    assert manifest["agent_run_id"] == "agent-run"
    assert manifest["ask_run_id"] == "ask-run"
    assert manifest["start_identity"] == "os-start"
    assert manifest["pgid"] == 4321
    assert manifest["process_group"][0]["pid"] == 4321
    assert manifest["violation"]["retained_path"].endswith(
        "/gobby/logs/sandbox-violations/agent-run.jsonl"
    )


def test_external_transcript_receipt_rejects_symlink(tmp_path: Path) -> None:
    source = tmp_path / "provider-home" / "actual.jsonl"
    source.parent.mkdir()
    source.write_bytes(b'{"private":"provider-memory"}\n')
    claimed_transcript = tmp_path / "claimed.jsonl"
    claimed_transcript.symlink_to(source)

    receipt = harness._copy_receipt(
        str(claimed_transcript),
        runtime_root=None,
        destination=tmp_path / "evidence" / "transcript.jsonl",
        kind="provider-transcript-and-mcp-responses",
    )

    assert receipt is None
    assert not (tmp_path / "evidence" / "transcript.jsonl").exists()


def test_process_snapshot_uses_os_start_identity_independent_of_terminal_status(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    row = {
        "id": "agent-run",
        "status": "completed",
        "pid": 4321,
        "terminal_id": "terminal-id",
        "child_session_id": "session-id",
        "terminal_process": {"pgid": 4321, "start_time": 99},
    }

    class Connection:
        def __enter__(self) -> Connection:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def execute(self, _query: str, _parameters: tuple[object, ...]) -> _Cursor:
            return _Cursor([row])

    monkeypatch.setattr(harness, "_ask_run_ids", lambda *_args: ["agent-run"])
    monkeypatch.setattr(
        "tests.ask.native_probe_harness.psycopg.connect",
        lambda *_args, **_kwargs: Connection(),
    )
    monkeypatch.setattr(harness, "_process_start_identity", lambda _pid: "os-start")
    start_identities: dict[str, str] = {}

    unknown = harness._process_snapshot(
        _SCOPED_TEST_DATABASE_URL,
        {},
        ["ask-run"],
        start_identities=start_identities,
    )
    unknown_agents = cast(list[dict[str, Any]], unknown["agents"])
    assert unknown_agents[0]["live"] is None
    assert unknown_agents[0]["start_identity"] is None
    assert unknown_agents[0]["observed_start_identity"] == "os-start"
    assert start_identities == {}

    start_identities["agent:agent-run:4321"] = "os-start"
    live = harness._process_snapshot(
        _SCOPED_TEST_DATABASE_URL,
        {},
        ["ask-run"],
        start_identities=start_identities,
    )
    live_agents = cast(list[dict[str, Any]], live["agents"])
    terminal_process = cast(dict[str, Any], live_agents[0]["terminal_process"])
    assert live_agents[0]["live"] is True
    assert live_agents[0]["start_identity"] == "os-start"
    assert terminal_process["start_time"] == 99

    monkeypatch.setattr(harness, "_process_start_identity", lambda _pid: None)
    after_cleanup = harness._process_snapshot(
        _SCOPED_TEST_DATABASE_URL,
        {},
        ["ask-run"],
        start_identities=start_identities,
    )
    final_agents = cast(list[dict[str, Any]], after_cleanup["agents"])
    assert final_agents[0]["live"] is False
    assert final_agents[0]["start_identity"] == "os-start"


def test_agent_cleanup_signals_only_the_matching_owned_process_group(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    group_snapshots = iter(
        [
            [
                {"pid": 4321, "ppid": 1, "pgid": 4321, "start_identity": "os-start"},
                {
                    "pid": 4322,
                    "ppid": 4321,
                    "pgid": 4321,
                    "start_identity": "child-start",
                },
            ],
            [
                {
                    "pid": 4322,
                    "ppid": 1,
                    "pgid": 4321,
                    "start_identity": "child-start",
                }
            ],
            [],
        ]
    )
    signals: list[tuple[int, int]] = []
    monkeypatch.setattr(
        harness,
        "_process_start_identity",
        lambda pid: {4321: "os-start", 4322: "child-start"}[pid],
    )
    monkeypatch.setattr(
        harness,
        "_process_group_snapshot",
        lambda _pgid: next(group_snapshots),
        raising=False,
    )
    monkeypatch.setattr("tests.ask.native_probe_harness.os.getpgid", lambda _pid: 4321)
    monkeypatch.setattr(
        "tests.ask.native_probe_harness.os.killpg",
        lambda pid, process_signal: signals.append((pid, process_signal)),
    )

    result = harness._terminate_owned_agent_process(
        {
            "id": "agent-run",
            "pid": 4321,
            "live": True,
            "start_identity": "os-start",
            "terminal_process": {"pgid": 4321, "start_time": 99},
        },
        deadline_monotonic=time.monotonic(),
    )

    assert result["agent_run_id"] == "agent-run"
    assert result["pid"] == 4321
    assert result["status"] == "terminated"
    assert cast(list[dict[str, object]], result["group_before"])[1]["pid"] == 4322
    assert result["group_after"] == []
    assert signals == [(4321, signal.SIGTERM), (4321, signal.SIGKILL)]


def test_agent_cleanup_uses_launch_group_when_leader_already_exited(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    launch_group = [
        {"pid": 4321, "ppid": 1, "pgid": 4321, "start_identity": "os-start"},
        {"pid": 4322, "ppid": 4321, "pgid": 4321, "start_identity": "child-start"},
    ]
    group_snapshots = iter(
        [
            [
                {
                    "pid": 4322,
                    "ppid": 1,
                    "pgid": 4321,
                    "start_identity": "child-start",
                }
            ],
            [],
        ]
    )
    signals: list[tuple[int, int]] = []
    monkeypatch.setattr(harness, "_process_group_snapshot", lambda _pgid: next(group_snapshots))
    monkeypatch.setattr(
        harness,
        "_process_start_identity",
        lambda pid: "child-start" if pid == 4322 else None,
    )
    monkeypatch.setattr("tests.ask.native_probe_harness.os.getpgid", lambda _pid: 4321)
    monkeypatch.setattr(
        "tests.ask.native_probe_harness.os.killpg",
        lambda pgid, process_signal: signals.append((pgid, process_signal)),
    )

    result = harness._terminate_owned_agent_process(
        {
            "id": "agent-run",
            "pid": 4321,
            "live": False,
            "start_identity": "os-start",
            "terminal_process": {"pgid": 4321, "start_time": 99},
            "process_group": launch_group,
        },
        deadline_monotonic=time.monotonic(),
    )

    assert result["status"] == "terminated"
    assert result["group_after"] == []
    assert signals == [(4321, signal.SIGTERM)]


def test_probe_question_correlates_hostile_read_with_evidence_tools() -> None:
    project_root = Path(__file__).parents[2]

    question = harness._probe_question(project_root)

    assert "tests/ask/fixtures/native_ask_probe_hostile.txt" in question
    assert "ASK_NATIVE_PROBE_EVIDENCE_MARKER=leaf-22018-hostile-repository-evidence" in question
    assert "native Read" in question
    assert "with query_evidence" in question
    assert "with read_evidence" in question
    assert "evidence_query" in question
    assert "evidence_read" in question
    assert "exactly once" not in question


def test_raw_export_retains_partial_evidence_when_one_run_snapshot_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    snapshot = _execution_snapshot(
        run_id="ask-run",
        deadline="2026-09-10T12:10:00Z",
        status="completed",
        current_agent="agent-run",
        superseded=[],
        include_publish=True,
    )

    def pipeline_snapshot(_database_url: str, run_id: str) -> dict[str, object]:
        if run_id == "broken-run":
            raise RuntimeError("incomplete execution row")
        return snapshot

    connection = _Connection({"ask-run": snapshot}, [])
    monkeypatch.setattr(harness, "_pipeline_snapshot", pipeline_snapshot)
    monkeypatch.setattr(
        "tests.ask.native_probe_harness.psycopg.connect",
        lambda *_args, **_kwargs: connection,
    )

    result = harness._export_raw(
        _SCOPED_TEST_DATABASE_URL,
        ["broken-run", "ask-run"],
        tmp_path / "evidence",
        runtime_root=tmp_path,
        process_sets={"failure": {"workers": [], "agents": []}},
    )

    exported = json.loads((tmp_path / "evidence" / "raw-probe.json").read_bytes())
    assert result["complete"] is False
    assert len(exported["ask_runs"]) == 1
    assert exported["ask_runs"][0]["execution"]["id"] == "ask-run"
    assert exported["capture_errors"] == [
        {
            "kind": "pipeline-snapshot",
            "run_id": "broken-run",
            "error_type": "RuntimeError",
            "message": "incomplete execution row",
        }
    ]


def test_failure_finalizer_exports_discovered_runs_before_exact_cleanup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime_root = harness._create_owned_runtime_root(parent=Path(tempfile.gettempdir()))
    output_dir = tmp_path / "evidence"
    output_dir.mkdir()
    calls: list[tuple[object, ...]] = []
    process_sets: dict[str, object] = {}

    monkeypatch.setattr(
        harness,
        "_native_ask_execution_ids",
        lambda *_args: ["partial-run"],
    )
    monkeypatch.setattr(
        harness,
        "_process_snapshot",
        lambda *_args, **_kwargs: {"workers": [], "agents": []},
    )

    def export_raw(
        _database_url: str,
        run_ids: list[str],
        _output_dir: Path,
        *,
        runtime_root: Path,
        process_sets: Mapping[str, object],
        runtime_identity: Mapping[str, object] | None = None,
    ) -> dict[str, object]:
        assert runtime_identity is None
        calls.append(("export", list(run_ids), runtime_root, dict(process_sets)))
        return {"path": "raw-probe.json", "sha256": "a" * 64, "complete": True}

    def drop_schema(_database_url: str, schema_name: str) -> None:
        calls.append(("drop", schema_name))

    monkeypatch.setattr(harness, "_export_raw", export_raw)
    monkeypatch.setattr(harness, "_drop_owned_schema", drop_schema)

    cleanup_errors = harness._finalize_contained_probe(
        base_database_url=_TEST_DATABASE_URL,
        scoped_database_url=_SCOPED_TEST_DATABASE_URL,
        schema_name=_SCHEMA,
        schema_created=True,
        project_id="project-id",
        output_dir=output_dir,
        runtime_root=runtime_root,
        workers={},
        ask_run_ids=[],
        process_sets=process_sets,
        failure={"error_type": "RuntimeError", "message": "fresh startup failed"},
    )

    assert cleanup_errors == []
    assert calls[0][0:2] == ("export", ["partial-run"])
    assert calls[1] == ("drop", _SCHEMA)
    assert not runtime_root.exists()
    cleanup = json.loads((output_dir / "cleanup.json").read_bytes())
    assert cleanup["raw_export"]["sha256"] == "a" * 64
    assert cleanup["schema"]["status"] == "dropped"
    assert cleanup["runtime_root"]["status"] == "removed"
    assert process_sets["after_cleanup"] == {"workers": [], "agents": [], "hosts": []}


def test_failure_finalizer_uses_launch_identity_when_before_snapshot_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    request: pytest.FixtureRequest,
) -> None:
    runtime_root = harness._create_owned_runtime_root(parent=Path(tempfile.gettempdir()))
    request.addfinalizer(lambda: shutil.rmtree(runtime_root) if runtime_root.exists() else None)
    output_dir = tmp_path / "evidence"
    output_dir.mkdir()
    launch_manifest: dict[str, Any] = {
        "agent_run_id": "agent-1",
        "captured_at_unix": 1.0,
        "pid": 4321,
        "terminal_id": "terminal-1",
        "child_session_id": "session-1",
        "status": "running",
        "start_identity": "stable-start",
        "terminal_process": {"pgid": 4321},
    }
    snapshots = iter(
        [
            RuntimeError("snapshot unavailable"),
            {
                "workers": [],
                "agents": [
                    {
                        "id": "agent-1",
                        "pid": 4321,
                        "terminal_id": "terminal-1",
                        "start_identity": "stable-start",
                        "live": False,
                    }
                ],
            },
        ]
    )
    terminated: list[Mapping[str, object]] = []

    def process_snapshot(*_args: object, **_kwargs: object) -> dict[str, object]:
        snapshot = next(snapshots)
        if isinstance(snapshot, Exception):
            raise snapshot
        return cast(dict[str, object], snapshot)

    def terminate_agent(
        agent: Mapping[str, object],
        *,
        deadline_monotonic: float,
    ) -> dict[str, object]:
        assert deadline_monotonic > time.monotonic()
        terminated.append(agent)
        return {
            "agent_run_id": agent["id"],
            "status": "terminated",
            "group_after": [],
            "group_before": [],
        }

    monkeypatch.setattr(harness, "_native_ask_execution_ids", lambda *_args: ["run-1"])
    monkeypatch.setattr(
        harness,
        "_load_launch_receipts",
        lambda _runtime_root: ({"agent-1": launch_manifest}, []),
    )
    monkeypatch.setattr(harness, "_process_snapshot", process_snapshot)
    monkeypatch.setattr(harness, "_terminate_owned_agent_process", terminate_agent)
    monkeypatch.setattr(
        harness,
        "_export_raw",
        lambda *_args, **_kwargs: {"path": "raw-probe.json", "sha256": "a" * 64, "complete": True},
    )
    monkeypatch.setattr(harness, "_drop_owned_schema", lambda *_args: None)

    cleanup_errors = harness._finalize_contained_probe(
        base_database_url=_TEST_DATABASE_URL,
        scoped_database_url=_SCOPED_TEST_DATABASE_URL,
        schema_name=_SCHEMA,
        schema_created=True,
        project_id="project-id",
        output_dir=output_dir,
        runtime_root=runtime_root,
        workers={},
        ask_run_ids=["run-1"],
        process_sets={},
        failure={"error_type": "RuntimeError", "message": "worker failed"},
    )

    assert [error["kind"] for error in cleanup_errors] == ["before-cleanup-process-snapshot"]
    assert len(terminated) == 1
    assert terminated[0]["id"] == "agent-1"
    assert terminated[0]["start_identity"] == "stable-start"


def test_failure_finalizer_retains_owned_storage_when_raw_export_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    request: pytest.FixtureRequest,
) -> None:
    runtime_root = harness._create_owned_runtime_root(parent=Path(tempfile.gettempdir()))
    request.addfinalizer(lambda: shutil.rmtree(runtime_root) if runtime_root.exists() else None)
    output_dir = tmp_path / "evidence"
    output_dir.mkdir()
    calls: list[object] = []

    monkeypatch.setattr(harness, "_native_ask_execution_ids", lambda *_args: [])
    monkeypatch.setattr(
        harness,
        "_process_snapshot",
        lambda *_args, **_kwargs: {"workers": [], "agents": []},
    )

    def export_raw(*_args: object, **_kwargs: object) -> dict[str, object]:
        calls.append("export")
        raise RuntimeError("raw export failed")

    def drop_schema(_database_url: str, schema_name: str) -> None:
        calls.append(("drop", schema_name))

    monkeypatch.setattr(harness, "_export_raw", export_raw)
    monkeypatch.setattr(harness, "_drop_owned_schema", drop_schema)

    cleanup_errors = harness._finalize_contained_probe(
        base_database_url=_TEST_DATABASE_URL,
        scoped_database_url=_SCOPED_TEST_DATABASE_URL,
        schema_name=_SCHEMA,
        schema_created=True,
        project_id="project-id",
        output_dir=output_dir,
        runtime_root=runtime_root,
        workers={},
        ask_run_ids=["partial-run"],
        process_sets={},
        failure={"error_type": "RuntimeError", "message": "probe failed"},
    )

    assert calls == ["export"]
    assert runtime_root.exists()
    cleanup = json.loads((output_dir / "cleanup.json").read_bytes())
    assert cleanup["raw_export"] is None
    assert cleanup["schema"]["status"] == "retained"
    assert cleanup["runtime_root"] == {"status": "retained", "path": str(runtime_root)}
    assert cleanup_errors == [
        {
            "kind": "raw-export",
            "phase": "cleanup",
            "error_type": "RuntimeError",
            "message": "raw export failed",
        }
    ]


def test_contained_drive_preserves_startup_failure_before_owned_teardown(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from gobby.storage import schema_contract

    project_root = tmp_path / "project"
    project_root.mkdir()
    output_dir = tmp_path / "evidence"
    runtime_roots: list[Path] = []
    original_create_runtime_root = harness._create_owned_runtime_root

    def create_owned_runtime_root() -> Path:
        runtime_root = original_create_runtime_root(parent=Path(tempfile.gettempdir()))
        runtime_roots.append(runtime_root)
        return runtime_root

    def fail_schema_startup(_database_url: str, *, schema: str) -> None:
        raise RuntimeError(f"schema startup failed: {schema}")

    def export_raw(
        _database_url: str,
        run_ids: list[str],
        _output_dir: Path,
        *,
        runtime_root: Path,
        process_sets: Mapping[str, object],
        runtime_identity: Mapping[str, object] | None = None,
    ) -> dict[str, object]:
        assert run_ids == []
        assert process_sets["after_cleanup"] == {"unavailable": "schema-not-created"}
        assert runtime_identity == {"source_head": "f" * 40}
        assert runtime_root.exists()
        return {"path": "raw-probe.json", "sha256": "b" * 64, "complete": True}

    monkeypatch.setenv("DATABASE_URL", _TEST_DATABASE_URL)
    monkeypatch.setattr(harness, "_read_project_identity", lambda _root: ("project-id", "p"))
    monkeypatch.setattr(
        harness,
        "_capture_runtime_identity",
        lambda _root: {"source_head": "f" * 40},
    )
    monkeypatch.setattr(harness, "_create_owned_runtime_root", create_owned_runtime_root)
    monkeypatch.setattr(schema_contract, "apply_schema", fail_schema_startup)
    monkeypatch.setattr(harness, "_export_raw", export_raw)

    with pytest.raises(RuntimeError, match="schema startup failed"):
        harness._contained_drive(
            argparse.Namespace(
                project_root=project_root,
                output_dir=output_dir,
                timeout_seconds=60.0,
            )
        )

    assert len(runtime_roots) == 1
    assert not runtime_roots[0].exists()
    failure = json.loads((output_dir / "failure.json").read_bytes())
    assert failure["error_type"] == "RuntimeError"
    cleanup = json.loads((output_dir / "cleanup.json").read_bytes())
    assert cleanup["failure"] == failure
    assert cleanup["raw_export"]["sha256"] == "b" * 64
    assert cleanup["schema"]["status"] == "not-created"
    assert cleanup["runtime_root"]["status"] == "removed"


def test_seal_writes_schema_v2_manifest_accepted_by_production_loader(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from tests.ask.test_native_probe_provenance import _raw_probe_fixture

    provider = _provider(tmp_path / "claude")
    observations_path = tmp_path / "observations.json"
    observations = _observations(provider, tmp_path)
    raw_path = _raw_probe_fixture(tmp_path, observations)
    observations_path.write_text(
        json.dumps(observations),
        encoding="utf-8",
    )
    _policy, paths = _policy_fixture(tmp_path)
    monkeypatch.setattr(
        "gobby.ask.runtime_validation.registered_run_tmp",
        lambda _run_root: Path(paths["run_tmp_root"]),
    )
    artifact_path = tmp_path / "validation" / "claude.json"
    manifest_path = tmp_path / "validation" / "manifest.json"
    arguments = argparse.Namespace(
        provider="claude",
        provider_executable=provider,
        auth_mode="claude.ai",
        observations=observations_path,
        output=artifact_path,
        manifest=manifest_path,
        profile=["ask-investigator", "ask-reviewer"],
    )

    assert harness._seal(arguments) == 0

    manifest = json.loads(manifest_path.read_bytes())
    assert manifest["schema_version"] == 2
    artifacts = load_ask_runtime_validation_artifacts(manifest_path)
    assert set(artifacts) == {"ask-investigator", "ask-reviewer"}
    for artifact in artifacts.values():
        validation = load_ask_runtime_validation(
            artifact,
            provider_executable=provider,
        )
        assert validation.verified_artifact is True
    admission = json.loads((manifest_path.parent / "admission.json").read_bytes())
    assert admission["provider"] == "claude"
    assert admission["raw_probe_sha256"] == hashlib.sha256(raw_path.read_bytes()).hexdigest()
    assert admission["profiles"] == ["ask-investigator", "ask-reviewer"]
