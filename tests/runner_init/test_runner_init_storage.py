"""Focused tests for runner storage/config initialization."""

from __future__ import annotations

import logging
from types import SimpleNamespace
from typing import cast

import pytest

from gobby.config.app import DaemonConfig
from gobby.config.bootstrap import BootstrapConfig
from gobby.runner_init.storage import (
    _warn_missing_terminal_dependency,
    bootstrap_overlaid_config,
)
from gobby.storage.hub.protocol import HubDatabase

pytestmark = pytest.mark.integration


def test_disabled_tmux_skips_availability_warning(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    config = cast(DaemonConfig, SimpleNamespace(tmux=SimpleNamespace(enabled=False)))

    def unexpected_which(_command: str) -> str | None:
        raise AssertionError("disabled tmux must not probe the host")

    monkeypatch.setattr("gobby.runner_init.storage.shutil.which", unexpected_which)

    with caplog.at_level(logging.WARNING, logger="gobby.runner_init.storage"):
        _warn_missing_terminal_dependency(config)

    assert caplog.records == []


def test_enabled_tmux_warns_when_unavailable(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    config = cast(DaemonConfig, SimpleNamespace(tmux=SimpleNamespace(enabled=True)))
    monkeypatch.setattr("gobby.agents.tmux.wsl_compat.needs_wsl", lambda: False)
    monkeypatch.setattr("gobby.runner_init.storage.shutil.which", lambda _command: None)

    with caplog.at_level(logging.WARNING, logger="gobby.runner_init.storage"):
        _warn_missing_terminal_dependency(config)

    assert "tmux is not installed. Agent spawning in terminal mode will not work." in caplog.text


def test_bootstrap_overlay_wins_for_bootstrap_owned_fields() -> None:
    bootstrap = BootstrapConfig(
        daemon_port=61111,
        bind_host="0.0.0.0",
        websocket_port=61112,
        ui_port=61113,
        datastore_mode="remote",
        database_url="postgresql://gobby:pw@db.example:5432/hub",
    )

    merged = bootstrap_overlaid_config(DaemonConfig(), bootstrap)

    assert merged.daemon_port == 61111
    assert merged.bind_host == "0.0.0.0"
    assert merged.websocket.port == 61112
    assert merged.ui.port == 61113
    assert merged.datastore_mode == "remote"
    assert merged.database_url == "postgresql://gobby:pw@db.example:5432/hub"


def test_bootstrap_overlay_preserves_stored_projection_values() -> None:
    candidate = DaemonConfig.model_validate(
        {
            "voice": {"enabled": True},
            "websocket": {"ping_interval": 45},
        }
    )
    bootstrap = BootstrapConfig(daemon_port=61111, websocket_port=61112)

    merged = bootstrap_overlaid_config(candidate, bootstrap)

    assert merged.voice.enabled is True
    # The nested overlay merges deep: the bootstrap port lands without
    # clobbering sibling websocket settings from the stored projection.
    assert merged.websocket.port == 61112
    assert merged.websocket.ping_interval == 45


def test_real_runtime_candidate_through_overlay_matches_daemon_startup(
    temp_db: HubDatabase,
) -> None:
    """The daemon startup path: stored overrides flow through the real
    runtime_candidate, and bootstrap-owned facts land only via the overlay."""
    from gobby.storage.config_mutations import ConfigMutations, ConfigPatch
    from gobby.storage.config_repository import ConfigRepository

    repository = ConfigRepository(temp_db)
    repository.reconcile_registry()
    ConfigMutations(temp_db).patch(
        expected_revision=repository.current_revision(),
        patch=ConfigPatch(values={"voice.enabled": True}),
    )
    stored = repository.read()

    candidate = repository.runtime_candidate(dict(stored.overrides), stored.secret_bindings)

    # runtime_candidate does not overlay bootstrap fields by design; they stay
    # at their defaults until bootstrap_overlaid_config runs.
    assert candidate.daemon_port == DaemonConfig().daemon_port
    assert candidate.voice.enabled is True

    bootstrap = BootstrapConfig(
        daemon_port=61111,
        websocket_port=61112,
        database_url="postgresql://gobby:pw@db.example:5432/hub",
    )
    merged = bootstrap_overlaid_config(candidate, bootstrap)

    assert merged.daemon_port == 61111
    assert merged.websocket.port == 61112
    assert merged.database_url == "postgresql://gobby:pw@db.example:5432/hub"
    assert merged.voice.enabled is True
