"""Focused tests for runner storage/config initialization."""

from __future__ import annotations

import logging
from types import SimpleNamespace
from typing import cast

import pytest

from gobby.config.app import DaemonConfig
from gobby.runner_init.storage import _warn_missing_terminal_dependency

pytestmark = pytest.mark.unit


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
