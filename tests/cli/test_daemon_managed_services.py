from __future__ import annotations

import shutil
import signal
import subprocess
import sys
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, call

import pytest

from gobby.cli import _daemon_services as daemon_services
from gobby.cli import daemon
from gobby.cli.installers import postgres as postgres_installer
from gobby.cli.installers.compose_env import (
    MANAGED_SERVICE_PROFILES,
    ComposeEnvironmentError,
    ComposeRuntime,
)

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _hub_schema_apply(monkeypatch: pytest.MonkeyPatch) -> list[Path]:
    """Stub the hub schema apply; record the homes it was invoked for."""
    applied: list[Path] = []
    monkeypatch.setattr(
        daemon_services,
        "_apply_hub_schema_contract",
        lambda gobby_home: applied.append(gobby_home),
    )
    return applied


def _write_compose(home: Path, *, include_falkordb: bool = True) -> Path:
    services = home / "services"
    services.mkdir(parents=True)
    falkordb = "  falkordb:\n    profiles: [falkordb]\n" if include_falkordb else ""
    compose = services / "docker-compose.yml"
    compose.write_text(
        (
            "services:\n"
            "  postgres:\n    profiles: [postgres]\n"
            "  qdrant:\n    profiles: [qdrant]\n" + falkordb
        ),
        encoding="utf-8",
    )
    return compose


def test_compose_timeout_terminates_windows_process_tree(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    command = ["docker", "compose", "up"]
    process = MagicMock()
    process.__enter__.return_value = process
    process.pid = 42
    process.communicate.side_effect = subprocess.TimeoutExpired(command, 10)
    popen = MagicMock(return_value=process)
    taskkill = MagicMock(return_value=subprocess.CompletedProcess(command, 0))

    monkeypatch.setattr(daemon_services, "resolves_to_real_run", lambda _run: True)
    monkeypatch.setattr(subprocess, "Popen", popen)
    monkeypatch.setattr(subprocess, "run", taskkill)
    monkeypatch.setattr(sys, "platform", "win32")

    with pytest.raises(subprocess.TimeoutExpired):
        daemon_services._run_compose_command(command, timeout=10, env={}, cwd=".")

    assert popen.call_args.kwargs["start_new_session"] is False
    taskkill.assert_called_once_with(
        ["taskkill", "/PID", "42", "/T", "/F"],
        capture_output=True,
        check=False,
        text=True,
    )
    process.wait.assert_called_once_with(timeout=daemon_services._COMPOSE_TERMINATION_GRACE_SECONDS)


def test_compose_interrupt_terminates_process_group_and_reaps_child(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    command = ["docker", "compose", "up"]
    process = MagicMock()
    process.__enter__.return_value = process
    process.pid = 42
    process.communicate.side_effect = KeyboardInterrupt
    process.wait.side_effect = [
        subprocess.TimeoutExpired(command, daemon_services._COMPOSE_TERMINATION_GRACE_SECONDS),
        0,
    ]
    killpg = MagicMock()

    monkeypatch.setattr(daemon_services, "resolves_to_real_run", lambda _run: True)
    popen = MagicMock(return_value=process)
    monkeypatch.setattr(subprocess, "Popen", popen)
    monkeypatch.setattr(daemon_services.os, "killpg", killpg)
    monkeypatch.setattr(sys, "platform", "darwin")

    with pytest.raises(KeyboardInterrupt):
        daemon_services._run_compose_command(command, timeout=10, env={}, cwd=".")

    assert popen.call_args.kwargs["start_new_session"] is True
    assert killpg.call_args_list == [
        ((42, signal.SIGTERM),),
        ((42, signal.SIGKILL),),
    ]
    assert process.wait.call_args_list == [
        call(timeout=daemon_services._COMPOSE_TERMINATION_GRACE_SECONDS),
        call(timeout=daemon_services._COMPOSE_TERMINATION_GRACE_SECONDS),
    ]


def test_second_interrupt_during_compose_reap_still_kills_process_group(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    command = ["docker", "compose", "up"]
    process = MagicMock()
    process.__enter__.return_value = process
    process.pid = 42
    process.communicate.side_effect = KeyboardInterrupt
    process.wait.side_effect = [KeyboardInterrupt, 0]
    killpg = MagicMock()

    monkeypatch.setattr(daemon_services, "resolves_to_real_run", lambda _run: True)
    monkeypatch.setattr(subprocess, "Popen", MagicMock(return_value=process))
    monkeypatch.setattr(daemon_services.os, "killpg", killpg)
    monkeypatch.setattr(sys, "platform", "darwin")

    with pytest.raises(KeyboardInterrupt):
        daemon_services._run_compose_command(command, timeout=10, env={}, cwd=".")

    assert killpg.call_args_list == [
        ((42, signal.SIGTERM),),
        ((42, signal.SIGKILL),),
    ]
    assert process.wait.call_args_list == [
        call(timeout=daemon_services._COMPOSE_TERMINATION_GRACE_SECONDS),
        call(timeout=daemon_services._COMPOSE_TERMINATION_GRACE_SECONDS),
    ]


def test_missing_docker_is_fatal(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(shutil, "which", lambda _name: None)

    result = daemon._services_start(tmp_path)

    assert result.outcome == "failed"
    assert "Docker executable" in result.detail


def test_missing_compose_is_fatal(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(shutil, "which", lambda _name: "/usr/bin/docker")

    result = daemon._services_start(tmp_path)

    assert result.outcome == "failed"
    assert "Compose file is missing" in result.detail


def test_missing_required_compose_profile_is_fatal(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(shutil, "which", lambda _name: "/usr/bin/docker")
    compose_file = _write_compose(tmp_path, include_falkordb=False)
    monkeypatch.setattr(postgres_installer, "_COMPOSE_SRC", compose_file)

    result = daemon._services_start(tmp_path)

    assert result.outcome == "failed"
    assert "falkordb" in result.detail


def test_compose_starts_all_profiles_and_waits_for_health(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(shutil, "which", lambda _name: "/usr/bin/docker")
    compose_file = _write_compose(tmp_path)
    environment = {"PATH": "/usr/bin"}
    monkeypatch.setattr(
        daemon,
        "resolve_compose_runtime",
        lambda _home, profiles=MANAGED_SERVICE_PROFILES: ComposeRuntime(
            environment=environment,
            profiles=profiles,
        ),
    )
    calls: list[list[str]] = []

    def _run(command: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        calls.append(command)
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", _run)

    result = daemon._services_start(tmp_path)

    assert result.outcome == "success"
    assert compose_file.read_text(encoding="utf-8") == postgres_installer._COMPOSE_SRC.read_text(
        encoding="utf-8"
    )
    assert len(calls) == 2
    assert [profile for profile in MANAGED_SERVICE_PROFILES if profile in calls[0]] == ["postgres"]
    command = calls[1]
    for profile in MANAGED_SERVICE_PROFILES:
        assert any(
            command[index : index + 2] == ["--profile", profile]
            for index in range(len(command) - 1)
        )
    assert command[-4:] == ["up", "-d", "--remove-orphans", "--wait"]


@pytest.mark.parametrize("failure", ["nonzero", "timeout"])
def test_compose_failure_prevents_startup(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    failure: str,
) -> None:
    monkeypatch.setattr(shutil, "which", lambda _name: "/usr/bin/docker")
    _write_compose(tmp_path)
    monkeypatch.setattr(
        daemon,
        "resolve_compose_runtime",
        lambda _home, profiles=MANAGED_SERVICE_PROFILES: ComposeRuntime(
            environment={},
            profiles=profiles,
        ),
    )
    calls = 0

    def _run(command: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        nonlocal calls
        calls += 1
        if calls == 1:
            return subprocess.CompletedProcess(command, 0, stdout="", stderr="")
        if failure == "timeout":
            raise subprocess.TimeoutExpired(command, 120)
        return subprocess.CompletedProcess(command, 1, stdout="", stderr="unhealthy")

    monkeypatch.setattr(subprocess, "run", _run)

    result = daemon._services_start(tmp_path)

    assert result.outcome == "failed"
    if failure == "timeout":
        assert "timed out" in result.detail
    else:
        assert "unhealthy" in result.detail


def test_schema_contract_applies_after_postgres_up_and_before_full_resolve(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(shutil, "which", lambda _name: "/usr/bin/docker")
    _write_compose(tmp_path)
    events: list[str] = []

    def _resolve(
        _home: Path,
        *,
        profiles: tuple[str, ...] = MANAGED_SERVICE_PROFILES,
    ) -> ComposeRuntime:
        events.append(f"resolve:{','.join(profiles)}")
        return ComposeRuntime(environment={}, profiles=profiles)

    def _run(command: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        events.append("compose-up")
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    monkeypatch.setattr(
        daemon_services,
        "_apply_hub_schema_contract",
        lambda gobby_home: events.append("apply-schema"),
    )
    monkeypatch.setattr(daemon, "resolve_compose_runtime", _resolve)
    monkeypatch.setattr(subprocess, "run", _run)

    result = daemon._services_start(tmp_path)

    full_resolve = f"resolve:{','.join(MANAGED_SERVICE_PROFILES)}"
    assert result.outcome == "success"
    assert events.index("apply-schema") > events.index("compose-up")
    assert events.index("apply-schema") < events.index(full_resolve)


def test_schema_contract_failure_fails_start_before_full_resolve(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(shutil, "which", lambda _name: "/usr/bin/docker")
    _write_compose(tmp_path)
    resolved_profiles: list[tuple[str, ...]] = []

    def _resolve(
        _home: Path,
        *,
        profiles: tuple[str, ...] = MANAGED_SERVICE_PROFILES,
    ) -> ComposeRuntime:
        resolved_profiles.append(profiles)
        return ComposeRuntime(environment={}, profiles=profiles)

    compose_calls: list[list[str]] = []

    def _run(command: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        compose_calls.append(command)
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    def _apply(gobby_home: Path) -> None:
        raise RuntimeError("hub is unreachable")

    monkeypatch.setattr(daemon_services, "_apply_hub_schema_contract", _apply)
    monkeypatch.setattr(daemon, "resolve_compose_runtime", _resolve)
    monkeypatch.setattr(subprocess, "run", _run)

    result = daemon._services_start(tmp_path)

    assert result.outcome == "failed"
    assert "hub is unreachable" in result.detail
    assert resolved_profiles == [("postgres",)]
    assert len(compose_calls) == 1


def test_missing_service_config_after_postgres_bootstrap_is_fatal(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(shutil, "which", lambda _name: "/usr/bin/docker")
    _write_compose(tmp_path)

    def _resolve(
        _home: Path,
        *,
        profiles: tuple[str, ...] = MANAGED_SERVICE_PROFILES,
    ) -> ComposeRuntime:
        if profiles == ("postgres",):
            return ComposeRuntime(environment={}, profiles=profiles)
        raise ComposeEnvironmentError("FalkorDB credentials are missing")

    calls: list[list[str]] = []

    def _run(command: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        calls.append(command)
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    monkeypatch.setattr(daemon, "resolve_compose_runtime", _resolve)
    monkeypatch.setattr(subprocess, "run", _run)

    result = daemon._services_start(tmp_path)

    assert result.outcome == "failed"
    assert "FalkorDB credentials are missing" in result.detail
    assert len(calls) == 1
