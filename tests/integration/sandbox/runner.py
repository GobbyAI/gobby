"""Shared helpers for the opt-in sandbox compatibility runner suite."""

from __future__ import annotations

import json
import shlex
import shutil
import subprocess
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any, cast

import pytest
from jsonschema.validators import validator_for

from gobby.cli.installers.hook_commands import _GOBBY_OWNED_MARKER, build_hook_command
from gobby.utils.native_bin import resolve_native_bin

pytestmark = pytest.mark.integration

REPO_ROOT = Path(__file__).resolve().parents[3]
HOOKS_DIR = REPO_ROOT / "src" / "gobby" / "install" / "shared" / "hooks"
SCHEMA_PATH = REPO_ROOT / "schemas" / "diagnose-output.v2.schema.json"


@dataclass(frozen=True)
class SandboxRunnerSpec:
    """Configuration for one CLI-specific sandbox compatibility runner."""

    cli_name: str
    cli_binary_name: str
    hook_type: str
    sandbox_summary: str
    expected_critical: bool
    expects_terminal_context: bool


@dataclass(frozen=True)
class DiagnoseRunResult:
    """Structured result from invoking ``ghook --diagnose``."""

    spec: SandboxRunnerSpec
    command: tuple[str, ...]
    stdout: str
    stderr: str
    payload: dict[str, Any]


CODEX_SPEC = SandboxRunnerSpec(
    cli_name="codex",
    cli_binary_name="codex",
    hook_type="PreToolUse",
    sandbox_summary="Codex defaults to workspace-write for daemon-owned sandboxed tools.",
    expected_critical=False,
    expects_terminal_context=True,
)
CLAUDE_SPEC = SandboxRunnerSpec(
    cli_name="claude",
    cli_binary_name="claude",
    hook_type="session-start",
    sandbox_summary="Claude runs with its managed sandbox enabled and unsandboxed fallback disabled.",
    expected_critical=True,
    expects_terminal_context=True,
)
GEMINI_SPEC = SandboxRunnerSpec(
    cli_name="gemini",
    cli_binary_name="gemini",
    hook_type="SessionStart",
    sandbox_summary="Gemini runs with -s and the daemon-selected Seatbelt profile.",
    expected_critical=True,
    expects_terminal_context=True,
)
QWEN_SPEC = SandboxRunnerSpec(
    cli_name="qwen",
    cli_binary_name="qwen",
    hook_type="SessionStart",
    sandbox_summary="Qwen follows the same -s plus Seatbelt-profile contract as Gemini.",
    expected_critical=True,
    expects_terminal_context=True,
)

ALL_SANDBOX_SPECS = (
    CODEX_SPEC,
    CLAUDE_SPEC,
    GEMINI_SPEC,
    QWEN_SPEC,
)


@lru_cache(maxsize=1)
def load_diagnose_schema() -> dict[str, Any]:
    """Load the mirrored ``ghook --diagnose`` schema."""
    return cast(dict[str, Any], json.loads(SCHEMA_PATH.read_text(encoding="utf-8")))


@lru_cache(maxsize=1)
def _diagnose_validator() -> Any:
    """Compile the schema validator once per test session."""
    schema = load_diagnose_schema()
    validator_cls = validator_for(schema)
    validator_cls.check_schema(schema)
    return validator_cls(schema)


def validate_diagnose_payload(payload: dict[str, Any]) -> None:
    """Validate one diagnose payload against the mirrored JSON schema."""
    _diagnose_validator().validate(payload)


class SandboxRunner:
    """Execute the current registered ``ghook --diagnose`` branch for one CLI."""

    def __init__(self, spec: SandboxRunnerSpec) -> None:
        self.spec = spec

    @property
    def ghook_binary_path(self) -> str | None:
        """Resolve the locally installed ``ghook`` binary."""
        return resolve_native_bin("ghook")

    @property
    def cli_binary_path(self) -> str | None:
        """Resolve the local host CLI binary."""
        return shutil.which(self.spec.cli_binary_name)

    def build_registered_command(self) -> str:
        """Return the Gobby-managed hook command currently registered for this CLI."""
        return build_hook_command(
            self.spec.cli_name,
            self.spec.hook_type,
            HOOKS_DIR,
            ghook_bin=self.ghook_binary_path,
        )

    def build_diagnose_command(self) -> tuple[str, ...]:
        """Rewrite the registered hook command into the diagnose form."""
        command = self.build_registered_command()
        if _GOBBY_OWNED_MARKER not in command:
            raise RuntimeError(
                "sandbox runner requires the ghook branch; local ghook was not resolved"
            )
        diagnose_command = command.replace(_GOBBY_OWNED_MARKER, "--diagnose", 1)
        return tuple(shlex.split(diagnose_command))

    def run_diagnose(
        self,
        *,
        cwd: Path | None = None,
        timeout_seconds: float = 10.0,
    ) -> DiagnoseRunResult:
        """Invoke ``ghook --diagnose`` and return parsed structured output."""
        if self.ghook_binary_path is None:
            pytest.skip("sandbox runner requires a local ghook binary")
        if self.cli_binary_path is None:
            pytest.skip(f"sandbox runner requires local {self.spec.cli_binary_name}")

        command = self.build_diagnose_command()
        completed = subprocess.run(
            list(command),
            cwd=str(cwd or REPO_ROOT),
            capture_output=True,
            text=True,
            check=False,
            timeout=timeout_seconds,
        )
        assert completed.returncode == 0, completed.stderr or completed.stdout

        payload = json.loads(completed.stdout)
        validate_diagnose_payload(payload)
        return DiagnoseRunResult(
            spec=self.spec,
            command=command,
            stdout=completed.stdout,
            stderr=completed.stderr,
            payload=payload,
        )

    def assert_matches_spec(self, result: DiagnoseRunResult) -> None:
        """Assert the live diagnose payload matches the expected CLI contract."""
        payload = result.payload
        assert payload["schema_version"] == 2
        assert payload["cli"] == self.spec.cli_name
        assert payload["hook_type"] == self.spec.hook_type
        assert payload["source"] == self.spec.cli_name
        assert payload["critical"] is self.spec.expected_critical
        assert payload["terminal_context_enabled"] is self.spec.expects_terminal_context
        assert payload["cli_recognized"] is True
        assert payload["daemon_host"] in {"127.0.0.1", "localhost"}
        assert payload["daemon_port"] == 60887
        assert payload["project_root"] == str(REPO_ROOT)
        assert payload["project_id"]
        if self.spec.expects_terminal_context:
            assert payload["terminal_context_preview"] is not None


class BaseSandboxRunnerTests:
    """Shared live checks for each CLI-specific runner module."""

    __test__ = False
    runner: SandboxRunner

    def test_builds_gobby_managed_command(self) -> None:
        command = self.runner.build_registered_command()
        assert f"--cli={self.runner.spec.cli_name}" in command
        assert f"--type={self.runner.spec.hook_type}" in command
        if self.runner.ghook_binary_path is not None:
            assert _GOBBY_OWNED_MARKER in command

    def test_builds_diagnose_command_from_registered_command(self) -> None:
        if self.runner.ghook_binary_path is None:
            pytest.skip("sandbox runner requires a local ghook binary")
        command = self.runner.build_diagnose_command()
        assert "--diagnose" in command
        assert _GOBBY_OWNED_MARKER not in command

    def test_diagnose_output_matches_current_contract(self) -> None:
        result = self.runner.run_diagnose()
        self.runner.assert_matches_spec(result)
