"""Coverage for shared sandbox runner infrastructure."""

from __future__ import annotations

import pytest

from .runner import ALL_SANDBOX_SPECS, CODEX_SPEC, SandboxRunner, load_diagnose_schema

pytestmark = [pytest.mark.integration, pytest.mark.slow]


# Keep this assertion in sync with ALL_SANDBOX_SPECS when a new CLI/provider
# is added so the runner contract coverage remains explicit.
def test_runner_specs_cover_supported_clis() -> None:
    cli_names = {spec.cli_name for spec in ALL_SANDBOX_SPECS}
    assert cli_names == {"claude", "codex", "gemini", "qwen"}


def test_load_diagnose_schema_points_at_mirrored_contract() -> None:
    schema = load_diagnose_schema()
    assert schema["$id"].endswith("diagnose-output.v1.schema.json")
    assert schema["properties"]["schema_version"]["const"] == 1


def test_build_diagnose_command_swaps_owned_marker() -> None:
    runner = SandboxRunner(CODEX_SPEC)
    runner.ghook_binary_path = "/usr/bin/ghook"

    command = runner.build_diagnose_command()

    assert "--diagnose" in command
    assert "--gobby-owned" not in command
    assert "--cli=codex" in command
