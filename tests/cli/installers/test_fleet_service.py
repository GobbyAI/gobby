"""Isolated contracts for supervising the native fleet sampler."""

import json
import plistlib
import subprocess
from pathlib import Path
from unittest.mock import Mock

import pytest

from gobby.cli.installers import fleet_service, service_common


@pytest.mark.parametrize("system", ["Darwin", "Linux"])
@pytest.mark.parametrize("existing", [True, False])
def test_install_loads_single_service_and_waits_for_new_sample(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, system: str, existing: bool
) -> None:
    home = tmp_path / "home & spaces"
    state = home / ".gobby" / "state" / "fleet-health.json"
    state.parent.mkdir(parents=True)
    if existing:
        state.write_text(json.dumps({"sampled_at_unix": 50, "watcher_pid": 12}))
    calls: list[list[str]] = []

    def run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(command)
        if "bootstrap" in command or "restart" in command:
            state.write_text(json.dumps({"sampled_at_unix": 100, "watcher_pid": 42}))
        output = "pid = 42\n" if "print" in command else "42\n"
        return subprocess.CompletedProcess(command, 0, output, "")

    monkeypatch.setattr(Path, "home", lambda: home)
    monkeypatch.setenv("GOBBY_HOME", str(home / ".gobby"))
    monkeypatch.setattr("platform.system", lambda: system)
    monkeypatch.setattr("subprocess.run", run)
    monkeypatch.setattr("time.time", lambda: 100.5)
    context = {
        "gdaemon_executable": str(home / ".gobby/bin/gdaemon"),
        "home_dir": str(home),
        "gobby_home": str(home / ".gobby"),
        "path_env": "/usr/bin:/bin",
        "runtime_log_file": str(home / "custom logs" / "daemon.log"),
    }
    monkeypatch.setattr(service_common, "_resolve_install_context", lambda **kw: context)
    project = tmp_path / "project"
    (project / ".gobby").mkdir(parents=True)
    (project / ".gobby/project.json").write_text(
        json.dumps({"id": "12345678-1234-5678-1234-567812345678"})
    )

    assert fleet_service.install_fleet_service(project) is True
    assert fleet_service.install_fleet_service(project) is True
    fleet_service.reload_fleet_service()
    if system == "Darwin":
        files = list((home / "Library/LaunchAgents").glob("*fleet*.plist"))
        assert len(files) == 1
        plist = plistlib.loads(files[0].read_bytes())
        assert plist["KeepAlive"] is True
        assert plist["ProgramArguments"] == [
            context["gdaemon_executable"],
            "fleet-watch",
            "--project-id",
            "12345678-1234-5678-1234-567812345678",
            "--logs-dir",
            str(home / "custom logs"),
        ]
        assert plist["EnvironmentVariables"]["GOBBY_HOME"] == context["gobby_home"]
        assert plist["StandardErrorPath"] == str(home / "custom logs" / "fleet-watch.log")
        assert sum("bootstrap" in cmd for cmd in calls) == 3
    else:
        files = list((home / ".config/systemd/user").glob("*fleet*.service"))
        assert len(files) == 1
        content = files[0].read_text()
        assert "Restart=always" in content
        assert "fleet-watch" in content
        assert "12345678-1234-5678-1234-567812345678" in content
        assert "--logs-dir" in content
        assert "custom logs" in content
        assert sum("restart" in cmd for cmd in calls) == 3


def test_windows_does_not_install_a_unix_only_command(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("platform.system", lambda: "Windows")
    run = Mock()
    monkeypatch.setattr("subprocess.run", run)
    assert fleet_service.install_fleet_service(tmp_path) is False
    run.assert_not_called()


@pytest.mark.parametrize("document", [None, "broken", "[]", '{"sampled_at_unix": 1}'])
def test_loading_requires_a_new_fresh_sample(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, document: str | None
) -> None:
    monkeypatch.setenv("GOBBY_HOME", str(tmp_path))
    monkeypatch.setattr("platform.system", lambda: "Linux")
    state = tmp_path / "state/fleet-health.json"
    state.parent.mkdir()
    if document is not None:
        state.write_text(document)
    calls: list[list[str]] = []

    def run(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0)

    monkeypatch.setattr("subprocess.run", run)
    clock = iter([0.0, 0.0, 61.0])
    monkeypatch.setattr("time.monotonic", lambda: next(clock))
    monkeypatch.setattr("time.sleep", lambda seconds: None)
    with pytest.raises(RuntimeError, match="did not publish a fresh sample"):
        fleet_service._load_and_wait(tmp_path / "watcher.service")
    assert calls[-1] == ["systemctl", "--user", "restart", "gobby-fleet-watch.service"]
    assert (state.read_text() if state.exists() else None) == document


def test_service_load_failure_is_fatal(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("GOBBY_HOME", str(tmp_path))
    monkeypatch.setattr("platform.system", lambda: "Linux")
    monkeypatch.setattr(
        "subprocess.run",
        lambda cmd, **kw: subprocess.CompletedProcess(cmd, 1, "", "permission denied"),
    )
    with pytest.raises(RuntimeError, match="permission denied"):
        fleet_service._load_and_wait(tmp_path / "watcher.service")


def test_sample_must_come_from_the_supervised_process(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("GOBBY_HOME", str(tmp_path))
    monkeypatch.setattr("platform.system", lambda: "Linux")
    state = tmp_path / "state/fleet-health.json"
    state.parent.mkdir()
    calls: list[list[str]] = []

    def run(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(cmd)
        if "restart" in cmd:
            state.write_text(json.dumps({"sampled_at_unix": 100, "watcher_pid": 111}))
        return subprocess.CompletedProcess(cmd, 0, "42\n", "")

    monkeypatch.setattr("subprocess.run", run)
    monkeypatch.setattr("time.time", lambda: 100.5)
    clock = iter([0.0, 0.0, 61.0])
    monkeypatch.setattr("time.monotonic", lambda: next(clock))
    monkeypatch.setattr("time.sleep", lambda seconds: None)
    with pytest.raises(RuntimeError, match="did not publish a fresh sample"):
        fleet_service._load_and_wait(tmp_path / "watcher.service")
    assert json.loads(state.read_text())["watcher_pid"] == 111
    assert calls[-1] == [
        "systemctl",
        "--user",
        "show",
        "gobby-fleet-watch.service",
        "--property=MainPID",
        "--value",
    ]
