"""Tests for the bundled Droid hooks template."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from gobby.adapters.droid_contract import DROID_PASCAL_HOOK_NAMES
from gobby.cli.installers.hook_commands import rewrite_hook_template_commands

pytestmark = pytest.mark.unit


def test_droid_template_matches_contract_and_rewrites_commands() -> None:
    template_path = Path("src/gobby/install/droid/hooks-template.json")
    template = json.loads(template_path.read_text())

    hooks = template["hooks"]
    assert tuple(hooks) == DROID_PASCAL_HOOK_NAMES

    rewrite_hook_template_commands(
        template,
        cli_name="droid",
        hooks_dir=Path("/tmp/hooks"),
        ghook_bin="/Users/test/.gobby/bin/ghook",
    )

    for hook_type, hook_configs in hooks.items():
        assert isinstance(hook_configs, list)
        assert len(hook_configs) == 1
        hook_config = hook_configs[0]
        if hook_type in {"PreToolUse", "PostToolUse"}:
            assert hook_config["matcher"] == "*"
        else:
            assert "matcher" not in hook_config

        command = hook_config["hooks"][0]["command"]
        base = f"/Users/test/.gobby/bin/ghook --gobby-owned --cli=droid --type={hook_type}"
        if hook_type == "Stop":
            # Stop hooks run through ghook_guard.py for stable shutdown.
            assert command == f"/tmp/hooks/ghook_guard.py -- {base}"
        else:
            assert command == base


def test_default_agent_template_lists_droid_as_supported_source() -> None:
    template_path = Path("src/gobby/install/shared/workflows/agents/default.yaml")
    template = yaml.safe_load(template_path.read_text())

    assert "droid" in template["sources"]
