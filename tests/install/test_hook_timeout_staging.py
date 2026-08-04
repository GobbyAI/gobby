from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from gobby.adapters.agy_contract import AGY_HOOK_TIMEOUT_SECONDS
from gobby.config.tasks import DEFAULT_WORKFLOW_TIMEOUT_SECONDS
from gobby.servers.routes.mcp.hooks import NON_CRITICAL_HOOK_TIMEOUT_SECONDS


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
    assert DEFAULT_WORKFLOW_TIMEOUT_SECONDS == 30
    assert NON_CRITICAL_HOOK_TIMEOUT_SECONDS == 35
    assert AGY_HOOK_TIMEOUT_SECONDS == 45


@pytest.mark.parametrize(
    ("provider", "expected_timeout"),
    [("agy", 45), ("grok", 45), ("qwen", 45_000)],
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
