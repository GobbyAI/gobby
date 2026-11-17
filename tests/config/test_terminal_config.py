"""Shared TerminalConfig lives with the contract, not the host (plan 2.2.9)."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest
from pydantic import ValidationError

from gobby.config.app import DaemonConfig
from gobby.config.terminals import TerminalConfig

pytestmark = pytest.mark.unit


def test_terminals_section_carries_folded_keys() -> None:
    config = TerminalConfig()
    expected = {
        "attach_history_lines": 500,
        "idle_check_enabled": True,
        "idle_timeout_seconds": 60,
        "idle_reprompt_delay_seconds": 300,
        "max_reprompt_attempts": 3,
        "reasoning_watchdog_interrupt_enabled": True,
        "reasoning_watchdog_settle_seconds": 0.2,
        "init_timeout_seconds": 120,
        "auto_enter_approval_prompts": True,
        "auto_enter_agent_terminals": True,
        "auto_enter_agent_interval_seconds": 30,
    }
    values = config.model_dump()
    assert {key: values.get(key) for key in expected} == expected


def test_folded_terminal_configuration_accepts_overrides() -> None:
    config = DaemonConfig.model_validate(
        {"terminals": {"attach_history_lines": 700, "idle_reprompt_delay_seconds": 420}}
    )
    assert config.terminals.attach_history_lines == 700
    assert config.terminals.idle_reprompt_delay_seconds == 420


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("attach_history_lines", -1),
        ("attach_history_lines", 2001),
        ("idle_timeout_seconds", 9),
        ("idle_reprompt_delay_seconds", 59),
        ("max_reprompt_attempts", 0),
        ("reasoning_watchdog_settle_seconds", -0.1),
        ("init_timeout_seconds", 29),
        ("auto_enter_agent_interval_seconds", 0),
    ],
)
def test_folded_key_bounds_reject_out_of_range(key: str, value: float) -> None:
    with pytest.raises(ValidationError, match=key):
        TerminalConfig.model_validate({key: value})


_SRC = Path(__file__).resolve().parents[2] / "src" / "gobby"
_P2_CONSUMERS = (
    _SRC / "terminals",
    _SRC / "config" / "terminals.py",
)


def test_shared_terminal_config_precedes_host_config() -> None:
    config = TerminalConfig()
    assert config.spawn_in_doubt_seconds > 0

    daemon = DaemonConfig()
    assert daemon.terminals.spawn_in_doubt_seconds == config.spawn_in_doubt_seconds
    assert type(daemon.terminals) is TerminalConfig

    overridden = DaemonConfig.model_validate({"terminals": {"spawn_in_doubt_seconds": 90.0}})
    assert overridden.terminals.spawn_in_doubt_seconds == 90.0

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
