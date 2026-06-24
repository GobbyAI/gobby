"""AGY install template regression tests."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from gobby.adapters.agy_contract import AGY_HOOK_NAMES

pytestmark = pytest.mark.unit


def test_agy_template_uses_current_vendor_hook_file_shape(repo_root: Path) -> None:
    template_path = repo_root / "src/gobby/install/agy/hooks-template.json"
    template = json.loads(template_path.read_text())

    assert tuple(template["hooks"]) == AGY_HOOK_NAMES
    assert ".antigravitycli" not in template_path.read_text()

    for hook_type in AGY_HOOK_NAMES:
        entries = template["hooks"][hook_type]
        assert len(entries) == 1
        assert entries[0]["hooks"][0]["command"] == "__GOBBY_HOOK_COMMAND__"


def test_agy_template_covers_agy_1_0_11_hook_events(repo_root: Path) -> None:
    hooks = json.loads((repo_root / "src/gobby/install/agy/hooks-template.json").read_text())[
        "hooks"
    ]

    assert set(hooks) == {
        "PreInvocation",
        "PostInvocation",
        "PreToolUse",
        "PostToolUse",
        "Stop",
    }
    assert hooks["PreToolUse"][0]["matcher"] == "*"
    assert hooks["PostToolUse"][0]["matcher"] == "*"
