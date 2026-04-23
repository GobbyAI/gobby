"""Coverage for shared sandbox runner infrastructure."""

from __future__ import annotations

import pytest

from . import runner as sandbox_runner_module
from .runner import (
    ALL_SANDBOX_SPECS,
    CODEX_SPEC,
    SandboxRunner,
    load_diagnose_schema,
    validate_diagnose_payload,
)

pytestmark = [pytest.mark.integration, pytest.mark.slow]


# Keep this assertion in sync with ALL_SANDBOX_SPECS when a new CLI/provider
# is added so the runner contract coverage remains explicit.
def test_runner_specs_cover_supported_clis() -> None:
    cli_names = {spec.cli_name for spec in ALL_SANDBOX_SPECS}
    assert cli_names == {"claude", "codex", "gemini", "qwen"}


def test_load_diagnose_schema_points_at_mirrored_contract() -> None:
    schema = load_diagnose_schema()
    assert schema["$id"].endswith("diagnose-output.v2.schema.json")
    assert schema["properties"]["schema_version"]["const"] == 2
    assert schema["properties"]["install_method"]["type"] == ["string", "null"]
    assert schema["properties"]["install_source_url"]["type"] == ["string", "null"]


def test_diagnose_schema_accepts_nullable_install_provenance() -> None:
    validate_diagnose_payload(
        {
            "schema_version": 2,
            "ghook_version": "0.2.1",
            "cli": "codex",
            "hook_type": "stop",
            "source": None,
            "critical": True,
            "terminal_context_enabled": True,
            "daemon_url": "http://127.0.0.1:60887",
            "daemon_host": "127.0.0.1",
            "daemon_port": 60887,
            "project_root": None,
            "project_id": None,
            "terminal_context_preview": None,
            "cli_recognized": True,
            "install_method": None,
            "install_source_url": None,
        }
    )


def test_build_diagnose_command_swaps_owned_marker(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sandbox_runner_module, "resolve_native_bin", lambda _name: "/usr/bin/ghook")
    runner = SandboxRunner(CODEX_SPEC)

    command = runner.build_diagnose_command()

    assert "--diagnose" in command
    assert "--gobby-owned" not in command
    assert "--cli=codex" in command
