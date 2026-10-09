"""TerminalHostConfig is host-specific and reads in-doubt from TerminalConfig."""

from __future__ import annotations

import inspect

import pytest
from pydantic import ValidationError

from gobby.config.app import DaemonConfig
from gobby.config.terminal_host import TerminalHostConfig
from gobby.config.terminals import TerminalConfig
from gobby.config.tmux import TmuxConfig

pytestmark = pytest.mark.unit


@pytest.mark.parametrize("drain", [True, False])
def test_ordinary_config_cannot_request_host_drain(drain: bool) -> None:
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        TerminalConfig.model_validate({"stop_host_on_shutdown": drain})
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        DaemonConfig.model_validate({"terminals": {"stop_host_on_shutdown": drain}})
    assert "stop_host_on_shutdown" not in TerminalConfig.model_fields


def test_terminal_host_config_defaults_and_shared_keys() -> None:
    host = TerminalHostConfig()
    assert host.enabled is True
    assert host.socket_dir == "~/.gobby"
    assert host.binary_path is None
    assert host.health_interval_seconds > 0
    assert host.shutdown_grace_seconds > 0
    assert host.commit_deadline_ms == 30_000
    fields = set(TerminalHostConfig.model_fields)
    assert "spawn_in_doubt_seconds" not in fields
    assert "default_backend" not in fields

    daemon = DaemonConfig()
    assert type(daemon.terminal_host) is TerminalHostConfig
    assert type(daemon.tmux) is TmuxConfig
    assert type(daemon.terminals) is TerminalConfig
    assert daemon.terminal_host.socket_dir == host.socket_dir
    assert daemon.terminals.spawn_in_doubt_seconds > 0
    source = inspect.getsource(TerminalHostConfig)
    assert "spawn_in_doubt_seconds" not in source
    assert "default_backend" not in source


def test_terminal_host_commit_deadline_bounds() -> None:
    assert TerminalHostConfig(commit_deadline_ms=1_000).commit_deadline_ms == 1_000
    with pytest.raises(ValidationError):
        TerminalHostConfig(commit_deadline_ms=999)
