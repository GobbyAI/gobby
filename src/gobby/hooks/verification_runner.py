"""Verification runner for git hooks.

Executes configured verification commands (lint, typecheck, tests, etc.) for git hook stages.
"""

import logging
import os
import subprocess  # nosec B404 # subprocess needed for verification commands
import time
from dataclasses import dataclass, field
from pathlib import Path

from gobby.config.features import HooksConfig, HookStageConfig, ProjectVerificationConfig
from gobby.utils.project_context import get_hooks_config, get_verification_config

logger = logging.getLogger(__name__)

# Default timeout for verification commands (5 minutes)
DEFAULT_TIMEOUT = 300


@dataclass
class VerificationResult:
    """Result of running a single verification command."""

    name: str
    command: str
    success: bool
    exit_code: int | None = None
    stdout: str = ""
    stderr: str = ""
    duration_ms: int = 0
    skipped: bool = False
    skip_reason: str | None = None
    error: str | None = None


@dataclass
class StageResult:
    """Result of running all verification commands for a hook stage."""

    stage: str
    success: bool
    results: list[VerificationResult] = field(default_factory=list)
    skipped: bool = False
    skip_reason: str | None = None

    @property
    def failed_count(self) -> int:
        """Number of failed verifications."""
        return sum(1 for r in self.results if not r.success and not r.skipped)

    @property
    def passed_count(self) -> int:
        """Number of passed verifications."""
        return sum(1 for r in self.results if r.success)

    @property
    def skipped_count(self) -> int:
        """Number of skipped verifications."""
        return sum(1 for r in self.results if r.skipped)


def run_command(
    name: str,
    command: str,
    cwd: Path,
    timeout: int = DEFAULT_TIMEOUT,
) -> VerificationResult:
    """Execute a verification command and return the result.

    Args:
        name: Name of the verification (e.g., 'lint', 'unit_tests').
        command: The command to execute.
        cwd: Working directory for the command.
        timeout: Maximum execution time in seconds.

    Returns:
        VerificationResult with command output and status.
    """
    start_time = time.time()

    # Clear VIRTUAL_ENV to avoid Python warnings when cwd differs from current directory
    # (e.g., when running in a worktree). Let Python auto-detect the correct environment.
    env = os.environ.copy()
    env.pop("VIRTUAL_ENV", None)
    env.pop("VIRTUAL_ENV_PROMPT", None)

    try:
        result = subprocess.run(
            command,
            shell=True,  # nosec B602 # user-configured verification commands require shell features
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=cwd,
            env=env,
        )

        duration_ms = int((time.time() - start_time) * 1000)

        return VerificationResult(
            name=name,
            command=command,
            success=result.returncode == 0,
            exit_code=result.returncode,
            stdout=result.stdout,
            stderr=result.stderr,
            duration_ms=duration_ms,
        )

    except subprocess.TimeoutExpired:
        duration_ms = int((time.time() - start_time) * 1000)
        return VerificationResult(
            name=name,
            command=command,
            success=False,
            duration_ms=duration_ms,
            error=f"Command timed out after {timeout} seconds",
        )

    except Exception as e:
        duration_ms = int((time.time() - start_time) * 1000)
        return VerificationResult(
            name=name,
            command=command,
            success=False,
            duration_ms=duration_ms,
            error=str(e),
        )


class VerificationRunner:
    """Runs verification commands for git hooks.

    Reads configuration from project.json and executes the appropriate
    commands for each hook stage (pre-commit, pre-push, pre-merge).
    """

    def __init__(
        self,
        verification_config: ProjectVerificationConfig | None = None,
        hooks_config: HooksConfig | None = None,
        cwd: Path | None = None,
    ):
        """Initialize VerificationRunner.

        Args:
            verification_config: Verification commands configuration.
            hooks_config: Git hooks configuration.
            cwd: Working directory (auto-detected if None).
        """
        self.cwd = cwd or Path.cwd()
        self.verification_config = verification_config
        self.hooks_config = hooks_config

    @classmethod
    def from_project(cls, cwd: Path | None = None) -> "VerificationRunner":
        """Create a VerificationRunner from project configuration.

        Args:
            cwd: Working directory to search for project config.

        Returns:
            VerificationRunner instance with loaded configuration.
        """
        cwd = cwd or Path.cwd()
        verification_config = get_verification_config(cwd)
        hooks_config = get_hooks_config(cwd)
        return cls(
            verification_config=verification_config,
            hooks_config=hooks_config,
            cwd=cwd,
        )

    def run_stage(self, stage: str) -> StageResult:
        """Run all verification commands for a hook stage.

        Args:
            stage: Hook stage name (e.g., 'pre-commit', 'pre-push', 'pre-merge').

        Returns:
            StageResult with all verification results.
        """
        # Check if hooks are configured
        if not self.hooks_config:
            return StageResult(
                stage=stage,
                success=True,
                skipped=True,
                skip_reason="No hooks configured in project.json",
            )

        # Get stage configuration
        stage_config = self.hooks_config.get_stage(stage)

        # Check if stage is enabled
        if not stage_config.enabled:
            return StageResult(
                stage=stage,
                success=True,
                skipped=True,
                skip_reason=f"Hook stage '{stage}' is disabled",
            )

        # Check if any commands are configured for this stage
        if not stage_config.run:
            return StageResult(
                stage=stage,
                success=True,
                skipped=True,
                skip_reason=f"No commands configured for '{stage}'",
            )

        # Check if verification config exists
        if not self.verification_config:
            command_names = stage_config.run[:1] if stage_config.fail_fast else stage_config.run
            return StageResult(
                stage=stage,
                success=False,
                results=[
                    VerificationResult(
                        name=cmd_name,
                        command="",
                        success=False,
                        error=f"Command '{cmd_name}' not defined in verification config",
                    )
                    for cmd_name in command_names
                ],
            )

        # Run each command
        results: list[VerificationResult] = []
        overall_success = True

        for cmd_name in stage_config.run:
            command = self.verification_config.get_command(cmd_name)

            if not command:
                error = f"Command '{cmd_name}' not defined in verification config"
                results.append(
                    VerificationResult(
                        name=cmd_name,
                        command="",
                        success=False,
                        error=error,
                    )
                )
                overall_success = False
                if stage_config.fail_fast:
                    break
                continue

            # Run the command
            result = run_command(
                name=cmd_name,
                command=command,
                cwd=self.cwd,
                timeout=stage_config.timeout,
            )
            results.append(result)

            if not result.success:
                overall_success = False
                if stage_config.fail_fast:
                    # Stop on first failure
                    break

        return StageResult(
            stage=stage,
            success=overall_success,
            results=results,
        )

    def get_stage_config(self, stage: str) -> HookStageConfig | None:
        """Get configuration for a hook stage.

        Args:
            stage: Hook stage name.

        Returns:
            HookStageConfig if configured, None otherwise.
        """
        if not self.hooks_config:
            return None
        return self.hooks_config.get_stage(stage)
