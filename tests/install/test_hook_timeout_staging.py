from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from gobby.adapters.agy_contract import AGY_HOOK_TIMEOUT_SECONDS
from gobby.config.app import DaemonConfig
from gobby.config.hooks import HOOK_TRANSPORT_WINDOW_SECONDS
from gobby.config.tasks import DEFAULT_WORKFLOW_TIMEOUT_SECONDS


def _timeout_values(value: Any) -> list[int]:
    if isinstance(value, dict):
        values = [value["timeout"]] if isinstance(value.get("timeout"), int) else []
        for child in value.values():
            values.extend(_timeout_values(child))
        return values
    if isinstance(value, list):
        values = []
        for child in value:
            values.extend(_timeout_values(child))
        return values
    return []


def test_hook_timeout_layers_leave_ordered_cleanup_windows() -> None:
    config = DaemonConfig()

    assert DEFAULT_WORKFLOW_TIMEOUT_SECONDS == 24
    assert config.hooks.adapter_timeout == 26
    assert config.hooks.provider_timeout == 120
    assert AGY_HOOK_TIMEOUT_SECONDS == 45
    # The staged provider timeouts sit outside ghook's own window, which is the
    # deadline that actually decides whether the daemon's answer is used.
    assert config.hooks.adapter_timeout < HOOK_TRANSPORT_WINDOW_SECONDS
    assert HOOK_TRANSPORT_WINDOW_SECONDS < AGY_HOOK_TIMEOUT_SECONDS


@pytest.mark.parametrize(
    ("provider", "expected_timeout"),
    [("agy", 45), ("droid", 120), ("grok", 120), ("qwen", 120_000)],
)
def test_explicit_provider_hook_timeouts_use_outer_ceiling(
    provider: str,
    expected_timeout: int,
) -> None:
    template_path = Path("src/gobby/install") / provider / "hooks-template.json"
    template = json.loads(template_path.read_text())
    timeouts = _timeout_values(template)

    assert timeouts
    assert set(timeouts) == {expected_timeout}


def test_claude_template_caps_session_end_timeout() -> None:
    template = json.loads(Path("src/gobby/install/claude/hooks-template.json").read_text())

    assert set(_timeout_values(template["hooks"]["SessionEnd"])) == {60}
    assert set(_timeout_values(template)) == {60, 120}


def test_codex_template_keeps_session_end_enqueue_only() -> None:
    template = json.loads(Path("src/gobby/install/codex/hooks-template.json").read_text())
    session_end = template["hooks"]["SessionEnd"][0]["hooks"][0]

    assert session_end["timeout"] == 3
    assert "--enqueue-only" in session_end["command"]
    assert set(_timeout_values(template)) == {3, 120}
