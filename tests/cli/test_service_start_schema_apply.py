"""A service-managed `gobby start` applies pending hub migrations itself (#23386).

These drive the real start command, singleton and managed-services chain: only
Docker, the installed gdaemon apply and the service manager are stand-ins.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, Mock, patch

import pytest
from click.testing import CliRunner

from gobby.cli import cli, daemon
from gobby.cli.installers.compose_env import MANAGED_SERVICE_PROFILES, ComposeRuntime
from gobby.runner_pid_file import (
    INHERITED_LOCK_FD_ENV,
    ProbeState,
    claim_pid_file,
    held_singleton_claim,
    probe_daemon_lock,
)
from gobby.storage.hub.postgres import PostgresHubDatabase
from gobby.storage.hub.runtime import runtime_hub_database
from tests.fixtures.fake_hub import FAKE_DATABASE_URL

pytestmark = pytest.mark.unit


class _Hub:
    """Hub whose migration step is the real PostgresHubDatabase delegation."""

    _conninfo = FAKE_DATABASE_URL
    apply_migrations = PostgresHubDatabase.apply_migrations

    def close(self) -> None:
        return None


@pytest.fixture
def events() -> list[str]:
    return []


@pytest.fixture
def gobby_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("GOBBY_HOME", str(tmp_path))
    monkeypatch.delenv("GOBBY_MANAGED_EXECUTION_BOOTSTRAP", raising=False)
    monkeypatch.delenv(INHERITED_LOCK_FD_ENV, raising=False)
    services = tmp_path / "services"
    services.mkdir()
    (services / "docker-compose.yml").write_text(
        "services:\n"
        "  postgres:\n    profiles: [postgres]\n"
        "  qdrant:\n    profiles: [qdrant]\n"
        "  falkordb:\n    profiles: [falkordb]\n",
        encoding="utf-8",
    )
    return tmp_path


@pytest.fixture
def gdaemon_apply(
    events: list[str], gobby_home: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[Mock]:
    """Stand in for an installed gdaemon whose pinned schema is newer than the hub."""
    # The CLI conftest stubs the opener; these tests exercise the real migration claim.
    monkeypatch.setattr("gobby.storage.hub.runtime.runtime_hub_database", runtime_hub_database)
    config = SimpleNamespace(
        database_url=FAKE_DATABASE_URL,
        postgres_pool=None,
        datastore_mode="local",
    )

    def _apply(_database_url: str) -> None:
        held = held_singleton_claim()
        events.append(f"apply:held={held is not None}")

    with (
        patch("gobby.storage.schema_contract.apply_schema", side_effect=_apply) as apply_schema,
        patch("gobby.storage.hub.runtime.load_bootstrap", return_value=config),
        patch(
            "gobby.storage.hub.runtime.admitted_database_url",
            return_value=FAKE_DATABASE_URL,
        ),
        patch(
            "gobby.storage.hub.postgres.PostgresHubDatabase",
            side_effect=lambda *_args, **_kwargs: _Hub(),
        ),
        patch("gobby.storage.projects.ensure_personal_project"),
    ):
        yield apply_schema


@pytest.fixture
def service_manager(
    events: list[str],
    gobby_home: Path,
    monkeypatch: pytest.MonkeyPatch,
    mock_daemon_config: MagicMock,
) -> Iterator[MagicMock]:
    """Installed OS service, Docker stand-ins, and the real managed-services chain."""

    def _resolve(
        _home: Path, *, profiles: tuple[str, ...] = MANAGED_SERVICE_PROFILES
    ) -> ComposeRuntime:
        return ComposeRuntime(environment={}, profiles=profiles)

    def _run(command: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        events.append("compose-up")
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    def _service_start(**_kwargs: object) -> dict[str, bool]:
        state = probe_daemon_lock(gobby_home / "gobby.pid").state
        events.append(f"service-start:{state.value}")
        return {"success": True}

    service_start = MagicMock(side_effect=_service_start)
    monkeypatch.setenv("GOBBY_TEST_PROTECT", "")
    monkeypatch.setattr(shutil, "which", lambda _name: "/usr/bin/docker")
    monkeypatch.setattr(subprocess, "run", _run)
    monkeypatch.setattr(daemon, "resolve_compose_runtime", _resolve)
    monkeypatch.setattr("gobby.cli.daemon_start._services_start", daemon._services_start)
    monkeypatch.setattr("gobby.cli.daemon_start._start_dependency_errors", lambda: [])
    monkeypatch.setattr("gobby.storage.schema_divergence.binary_set_apply_refusal", lambda: None)
    monkeypatch.setattr("gobby.cli.daemon_start.worktree_daemon_refusal", lambda: None)
    monkeypatch.setattr(
        "gobby.cli.daemon_start.get_service_status",
        lambda: {"installed": True, "platform": "macos"},
    )
    monkeypatch.setattr("gobby.cli.daemon_start.service_start", service_start)
    monkeypatch.setattr("gobby.cli.daemon_start._wait_for_daemon_health", lambda _port: 1.0)
    monkeypatch.setattr("gobby.cli.daemon_start._poll_startup_progress", lambda _port: True)
    monkeypatch.setattr("gobby.cli.daemon_start._reconcile_ui_exposure", lambda _port: None)
    monkeypatch.setattr(
        "gobby.cli.runtime.CliRuntime.read_only_operational_config",
        lambda _runtime: mock_daemon_config,
    )
    with (
        patch("gobby.runner_pid_record.current_boot_id", return_value="boot:test"),
        patch("gobby.runner_pid_file.current_boot_id", return_value="boot:test"),
    ):
        yield service_start


def _inherit_restart_claim(gobby_home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Hand `start` the singleton the way `gobby restart` does, by descriptor."""
    claim = claim_pid_file(gobby_home / "gobby.pid", role="daemon")
    assert claim is not None
    inherited_fd = os.dup(claim.fileno())
    claim.detach()
    monkeypatch.setenv(INHERITED_LOCK_FD_ENV, str(inherited_fd))


@pytest.mark.parametrize("entry", ["start", "restart_handoff"])
def test_service_start_applies_pending_migrations_before_the_launch_reservation(
    entry: str,
    gobby_home: Path,
    gdaemon_apply: Mock,
    service_manager: MagicMock,
    events: list[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The start holds the singleton while it migrates, then hands off to the service."""
    if entry == "restart_handoff":
        _inherit_restart_claim(gobby_home, monkeypatch)

    result = CliRunner().invoke(cli, ["start"])

    assert result.exit_code == 0, result.output
    assert gdaemon_apply.call_args_list == [((FAKE_DATABASE_URL,), {})]
    assert events == [
        "compose-up",
        "apply:held=True",
        "compose-up",
        f"service-start:{ProbeState.LIVE_RESERVATION.value}",
    ]
    service_manager.assert_called_once_with(reserved=True)


def test_service_start_fails_loudly_when_it_cannot_own_the_migration(
    gobby_home: Path,
    gdaemon_apply: Mock,
    service_manager: MagicMock,
) -> None:
    """A start that cannot take the migration claim refuses instead of skipping it."""
    with patch("gobby.storage.hub.runtime.held_singleton_claim", return_value=None):
        result = CliRunner().invoke(cli, ["start"])

    assert result.exit_code == 1
    assert "Could not apply the hub schema contract" in result.output
    assert "Hub schema apply needs the daemon singleton" in result.output
    assert "pending migrations were not applied" in result.output
    gdaemon_apply.assert_not_called()
    service_manager.assert_not_called()
    assert probe_daemon_lock(gobby_home / "gobby.pid").state is ProbeState.ABSENT
