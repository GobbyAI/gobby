"""TerminalHostConfig is host-specific and reads in-doubt from TerminalConfig."""

from __future__ import annotations

import inspect

import pytest

from gobby.config.app import DaemonConfig
from gobby.config.terminal_host import TerminalHostConfig
from gobby.config.terminals import TerminalConfig
from gobby.config.tmux import TmuxConfig

pytestmark = pytest.mark.unit


def test_terminal_host_config_defaults_and_shared_keys() -> None:
    host = TerminalHostConfig()
    assert host.enabled is True
    assert host.socket_dir == "~/.gobby"
    assert host.binary_path is None
    assert host.health_interval_seconds > 0
    assert host.shutdown_grace_seconds > 0
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
