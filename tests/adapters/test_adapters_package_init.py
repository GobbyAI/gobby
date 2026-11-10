"""Importing one adapter submodule must not load the whole adapter and agent stack."""

import subprocess
import sys

from gobby import adapters


def _modules_loaded_by(statement: str) -> set[str]:
    script = "import sys\n" + statement + "\nprint('\\n'.join(sys.modules))\n"
    completed = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        timeout=120,
        check=True,
    )
    return set(completed.stdout.split())


def test_importing_an_acp_module_does_not_load_the_agent_stack() -> None:
    loaded = _modules_loaded_by("import gobby.adapters.acp_client")

    assert "gobby.ai" not in loaded
    assert "claude_agent_sdk" not in loaded


def test_package_level_adapter_names_still_resolve() -> None:
    assert adapters.GitAdapter.__name__ == "GitAdapter"
    assert adapters.ClaudeCodeAdapter.__name__ == "ClaudeCodeAdapter"
