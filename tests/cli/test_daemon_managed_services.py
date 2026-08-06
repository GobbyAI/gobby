from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

from gobby.cli import daemon
from gobby.cli.installers.compose_env import (
    MANAGED_SERVICE_PROFILES,
    ComposeEnvironmentError,
    ComposeRuntime,
)

pytestmark = pytest.mark.unit


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
    _write_compose(tmp_path, include_falkordb=False)

    result = daemon._services_start(tmp_path)

    assert result.outcome == "failed"
    assert "falkordb" in result.detail


def test_compose_starts_all_profiles_and_waits_for_health(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(shutil, "which", lambda _name: "/usr/bin/docker")
    _write_compose(tmp_path)
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

    monkeypatch.setattr(daemon.subprocess, "run", _run)

    result = daemon._services_start(tmp_path)

    assert result.outcome == "success"
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

    monkeypatch.setattr(daemon.subprocess, "run", _run)

    result = daemon._services_start(tmp_path)

    assert result.outcome == "failed"
    if failure == "timeout":
        assert "timed out" in result.detail
    else:
        assert "unhealthy" in result.detail


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
    monkeypatch.setattr(daemon.subprocess, "run", _run)

    result = daemon._services_start(tmp_path)

    assert result.outcome == "failed"
    assert "FalkorDB credentials are missing" in result.detail
    assert len(calls) == 1
