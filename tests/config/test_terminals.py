"""Shared TerminalConfig lives with the contract, not the host (plan 2.2.9)."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest
from pydantic import ValidationError

from gobby.config.app import DaemonConfig
from gobby.config.terminals import TerminalConfig
from gobby.config.tmux import TmuxConfig

pytestmark = pytest.mark.unit

_SRC = Path(__file__).resolve().parents[2] / "src" / "gobby"
_P2_CONSUMERS = (
    _SRC / "terminals",
    _SRC / "config" / "terminals.py",
)


def test_shared_terminal_config_precedes_host_config() -> None:
    config = TerminalConfig()
    assert config.default_backend == "native"
    assert config.spawn_in_doubt_seconds > 0

    daemon = DaemonConfig()
    assert daemon.terminals.default_backend == "native"
    assert daemon.terminals.spawn_in_doubt_seconds == config.spawn_in_doubt_seconds
    assert type(daemon.terminals) is TerminalConfig
    assert type(daemon.tmux) is TmuxConfig

    native = DaemonConfig.model_validate(
        {"terminals": {"default_backend": "native", "spawn_in_doubt_seconds": 90.0}}
    )
    assert native.terminals.default_backend == "native"
    assert native.terminals.spawn_in_doubt_seconds == 90.0

    with pytest.raises(ValidationError):
        DaemonConfig.model_validate({"terminals": {"default_backend": "ssh"}})

    imported_host = False
    for root in _P2_CONSUMERS:
        paths = [root] if root.is_file() else sorted(root.rglob("*.py"))
        for path in paths:
            if path.name.startswith("host_"):
                continue
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom) and node.module is not None:
                    if "terminal_host" in node.module:
                        imported_host = True
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        if "terminal_host" in alias.name:
                            imported_host = True
    assert imported_host is False
