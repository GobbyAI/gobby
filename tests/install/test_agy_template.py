"""AGY install template regression tests."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from gobby.adapters.agy_contract import (
    AGY_FLAT_HOOK_NAMES,
    AGY_GOBBY_HOOK_NAME,
    AGY_GROUPED_HOOK_NAMES,
    AGY_HOOK_NAMES,
    AGY_HOOK_TIMEOUT_SECONDS,
)

pytestmark = pytest.mark.unit


def _template(repo_root: Path) -> dict[str, Any]:
    data = json.loads((repo_root / "src/gobby/install/agy/hooks-template.json").read_text())
    assert isinstance(data, dict)
    return data


def test_agy_template_is_keyed_by_hook_name_not_literal_hooks(repo_root: Path) -> None:
    """AGY reads each top-level key as a hook name.

    A literal "hooks" key made AGY reject the whole file with
    `invalid hook "hooks": command hook must specify 'command'`.
    """
    template = _template(repo_root)

    assert list(template) == [AGY_GOBBY_HOOK_NAME]
    assert "hooks" not in template


def test_agy_template_covers_every_agy_hook_event(repo_root: Path) -> None:
    gobby_hook = _template(repo_root)[AGY_GOBBY_HOOK_NAME]

    assert set(gobby_hook) == set(AGY_HOOK_NAMES)


@pytest.mark.parametrize("hook_type", AGY_FLAT_HOOK_NAMES)
def test_agy_template_flat_events_are_bare_handler_lists(repo_root: Path, hook_type: str) -> None:
    """PreInvocation/PostInvocation/Stop take handlers directly, with no wrapper."""
    entries = _template(repo_root)[AGY_GOBBY_HOOK_NAME][hook_type]

    assert len(entries) == 1
    handler = entries[0]
    assert "hooks" not in handler
    assert "matcher" not in handler
    assert handler["command"] == "__GOBBY_HOOK_COMMAND__"
    assert handler["type"] == "command"


@pytest.mark.parametrize("hook_type", AGY_GROUPED_HOOK_NAMES)
def test_agy_template_tool_events_use_matcher_groups(repo_root: Path, hook_type: str) -> None:
    entries = _template(repo_root)[AGY_GOBBY_HOOK_NAME][hook_type]

    assert len(entries) == 1
    assert entries[0]["matcher"] == "*"
    handler = entries[0]["hooks"][0]
    assert handler["command"] == "__GOBBY_HOOK_COMMAND__"
    assert handler["type"] == "command"


def test_agy_template_timeouts_are_seconds(repo_root: Path) -> None:
    """Template default is seconds, overwritten at install by hook_timeout_seconds."""
    gobby_hook = _template(repo_root)[AGY_GOBBY_HOOK_NAME]

    timeouts = []
    for hook_type in AGY_FLAT_HOOK_NAMES:
        timeouts.append(gobby_hook[hook_type][0]["timeout"])
    for hook_type in AGY_GROUPED_HOOK_NAMES:
        timeouts.append(gobby_hook[hook_type][0]["hooks"][0]["timeout"])

    assert timeouts == [AGY_HOOK_TIMEOUT_SECONDS] * len(AGY_HOOK_NAMES)


def test_agy_template_has_no_stale_vendor_directory(repo_root: Path) -> None:
    template_path = repo_root / "src/gobby/install/agy/hooks-template.json"

    assert ".antigravitycli" not in template_path.read_text()
