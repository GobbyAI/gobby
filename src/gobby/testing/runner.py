"""Test runner for gobby-tests MCP server.

Executes test/lint/typecheck commands via async subprocess, captures output to disk,
and produces concise summaries (LLM-based on failure, brief on success).
"""

from __future__ import annotations

import asyncio
import logging
import shlex
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from gobby.config.features import TestSummarizerConfig
    from gobby.llm.service import LLMService
    from gobby.storage.test_runs import TestRunStorage

from gobby.storage.test_run_models import TestRun

logger = logging.getLogger(__name__)

_DEFAULT_OUTPUT_DIR = Path.home() / ".gobby" / "test_runs"

_SUMMARIZE_PROMPT = """Extract ONLY the errors/failures from this {category} output.
List each with file:line and message. Be concise.
Do not include passing tests or irrelevant output.
If there are no clear errors, say "No specific errors found" and include the last few lines.

Output:
{output}"""


class TestRunner:
    """Runs verification commands and produces token-efficient summaries."""

    def __init__(
        self,
        storage: TestRunStorage,
        llm_service: LLMService | None = None,
        config: TestSummarizerConfig | None = None,
        output_dir: Path | None = None,
    ):
        self.storage = storage
        self.llm_service = llm_service
        self.config = config
        self.output_dir = output_dir or _DEFAULT_OUTPUT_DIR
        self.output_dir.mkdir(parents=True, exist_ok=True)

    async def run_check(
        self,
        category: str,
        command: str,
        cwd: str | None = None,
        paths: str | None = None,
        extra_args: str | None = None,
        timeout: int = 300,
        session_id: str | None = None,
        project_id: str | None = None,
    ) -> TestRun:
        """Run a verification command and return a TestRun with summary.

        Args:
            category: Verification category name (e.g., 'unit_tests', 'lint')
            command: The command to execute
            cwd: Working directory for the command
            paths: Override paths in command (replaces path args)
            extra_args: Extra arguments to append
            timeout: Command timeout in seconds
            session_id: Session that triggered the run
            project_id: Project the run belongs to

        Returns:
            TestRun with status, exit_code, and summary populated
        """
        # Build final command
        cmd = command
        if paths:
            # Replace existing path-like positional args with the override paths.
            # Keeps command words (uv, run, pytest, ruff, check, etc.) and flags.
            parts = shlex.split(cmd)
            cmds = []
            flags = []
            for part in parts:
                if part.startswith("-"):
                    flags.append(part)
                elif "/" not in part:
                    cmds.append(part)
                # else: drop path-like positional args (e.g. "tests/", "src/")
            cmd = shlex.join(cmds + shlex.split(paths) + flags)
        if extra_args:
            cmd = f"{cmd} {extra_args}"

        # Create run record
        run = self.storage.create_run(
            category=category,
            command=cmd,
            session_id=session_id,
            project_id=project_id,
        )

        try:
            run = await self._execute(run, cmd, cwd=cwd, timeout=timeout)
        except Exception as e:
            logger.exception("Test run %s failed unexpectedly", run.id)
            now = datetime.now(UTC).isoformat()
            run = (
                self.storage.update_run(
                    run.id,
                    status="failed",
                    exit_code=-1,
                    summary=f"Internal error: {e}",
                    completed_at=now,
                )
                or run
            )

        return run

    async def _execute(
        self,
        run: TestRun,
        cmd: str,
        cwd: str | None = None,
        timeout: int = 300,
    ) -> TestRun:
        """Execute command via async subprocess and update run record."""
        process = await asyncio.create_subprocess_shell(
            cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            cwd=cwd,
        )

        try:
            stdout_bytes, _ = await asyncio.wait_for(process.communicate(), timeout=timeout)
        except TimeoutError:
            process.kill()
            await process.wait()
            now = datetime.now(UTC).isoformat()
            return (
                self.storage.update_run(
                    run.id,
                    status="timeout",
                    exit_code=-1,
                    summary=f"Command timed out after {timeout}s",
                    completed_at=now,
                )
                or run
            )

        output = stdout_bytes.decode("utf-8", errors="replace") if stdout_bytes else ""
        exit_code = process.returncode or 0

        # Write full output to disk
        output_file = self.output_dir / f"{run.id}.log"
        output_file.write_text(output, encoding="utf-8")

        # Generate summary
        if exit_code == 0:
            summary = self._brief_success(output)
            status = "completed"
        else:
            summary = await self._summarize_failure(output, run.category)
            status = "failed"

        now = datetime.now(UTC).isoformat()
        return (
            self.storage.update_run(
                run.id,
                status=status,
                exit_code=exit_code,
                summary=summary,
                output_file=str(output_file),
                completed_at=now,
            )
            or run
        )

    def _brief_success(self, output: str) -> str:
        """Extract a brief success summary from output (last few meaningful lines)."""
        lines = output.strip().splitlines()
        if not lines:
            return "Passed (no output)"

        # Take the last 5 non-empty lines — usually contains the summary
        tail = [line for line in lines[-10:] if line.strip()][-5:]
        return "\n".join(tail)

    async def _summarize_failure(self, output: str, category: str) -> str:
        """Use LLM to extract only errors from failure output.

        Falls back to last 50 lines if LLM is unavailable or times out.
        """
        max_lines = 200
        if self.config:
            max_lines = self.config.max_output_lines

        lines = output.strip().splitlines()
        truncated = "\n".join(lines[-max_lines:])

        # Try LLM summarization with timeout (same pattern as title synthesis)
        if self.llm_service and self.config and self.config.enabled:
            try:
                provider = self.llm_service.get_provider(self.config.provider)
                if provider:
                    prompt = _SUMMARIZE_PROMPT.format(category=category, output=truncated)
                    llm_timeout = self.config.timeout
                    summary = await asyncio.wait_for(
                        provider.generate_text(
                            prompt=prompt,
                            model=self.config.model,
                            max_tokens=1024,
                        ),
                        timeout=llm_timeout,
                    )
                    if summary:
                        return summary.strip()
            except TimeoutError:
                logger.warning(
                    "LLM summarization timed out after %ss for %s, falling back to raw tail",
                    self.config.timeout,
                    category,
                )
            except Exception as e:
                logger.warning("LLM summarization failed, falling back to raw tail: %s", e)

        # Fallback: last 50 lines
        fallback_lines = lines[-50:]
        return "\n".join(fallback_lines)

    def get_output(
        self,
        run: TestRun,
        offset: int = 0,
        limit: int = 50,
    ) -> dict[str, object]:
        """Get paginated raw output from a run's log file.

        Args:
            run: TestRun to get output for
            offset: Line offset to start from
            limit: Number of lines to return

        Returns:
            Dict with lines, total_lines, offset, limit, has_more
        """
        if not run.output_file:
            return {
                "lines": [],
                "total_lines": 0,
                "offset": offset,
                "limit": limit,
                "has_more": False,
            }

        output_path = Path(run.output_file)
        if not output_path.exists():
            return {
                "lines": [],
                "total_lines": 0,
                "offset": offset,
                "limit": limit,
                "has_more": False,
            }

        all_lines = output_path.read_text(encoding="utf-8").splitlines()
        total = len(all_lines)
        page = all_lines[offset : offset + limit]

        return {
            "lines": page,
            "total_lines": total,
            "offset": offset,
            "limit": limit,
            "has_more": (offset + limit) < total,
        }
