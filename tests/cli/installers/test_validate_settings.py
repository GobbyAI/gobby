"""Tests for the deployed settings validator."""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

from gobby.adapters.claude_contract import CLAUDE_PASCAL_HOOK_NAMES
from gobby.adapters.droid_contract import DROID_PASCAL_HOOK_NAMES
from gobby.install.shared.hooks import validate_settings


def test_deployed_validator_runs_without_gobby_importable(tmp_path: Path) -> None:
    project_dir = tmp_path / "project"
    hooks_dir = project_dir / ".gobby" / "hooks"
    hooks_dir.mkdir(parents=True)
    deployed_script = hooks_dir / "validate_settings.py"
    shutil.copy2(Path(validate_settings.__file__), deployed_script)

    config = validate_settings.CLI_VALIDATION_CONFIGS["grok"]
    settings_file = project_dir / config.settings_dir / config.settings_file
    settings_file.parent.mkdir(parents=True)
    hook_config = [{"hooks": [{"command": "ghook --gobby-owned"}]}]
    settings_file.write_text(
        json.dumps({"hooks": dict.fromkeys(config.required_hooks, hook_config)})
    )

    result = subprocess.run(
        [sys.executable, "-I", "-S", str(deployed_script), "--cli=grok"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert "All validations passed! (Grok CLI)" in result.stdout


def test_inlined_hook_names_match_adapter_contracts() -> None:
    assert (
        validate_settings.CLI_VALIDATION_CONFIGS["claude"].required_hooks
        == CLAUDE_PASCAL_HOOK_NAMES
    )
    assert (
        validate_settings.CLI_VALIDATION_CONFIGS["droid"].required_hooks == DROID_PASCAL_HOOK_NAMES
    )
