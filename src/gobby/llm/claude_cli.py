"""Claude CLI path management and multi-turn session launcher.

Functions for finding and verifying the Claude CLI binary, plus
ClaudeCLI/CLISession classes for subprocess-based multi-turn web chat.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import signal
from collections.abc import AsyncIterator
from urllib.parse import urlparse

from gobby.llm.stream_json_parser import StreamEvent, parse_stream

logger = logging.getLogger(__name__)


def find_cli_path() -> str | None:
    """
    Find Claude CLI path.

    DO NOT resolve symlinks - npm manages the symlink atomically during upgrades.
    Resolving causes race conditions when Claude Code is being reinstalled.
    """
    cli_path = shutil.which("claude")

    if cli_path:
        # Validate CLI exists and is executable
        if not os.path.exists(cli_path):
            logger.warning(f"Claude CLI not found: {cli_path}")
            return None
        elif not os.access(cli_path, os.X_OK):
            logger.warning(f"Claude CLI not executable: {cli_path}")
            return None
        else:
            logger.debug(f"Claude CLI found: {cli_path}")
            return cli_path
    else:
        logger.warning("Claude CLI not found in PATH - LLM features disabled")
        return None


async def verify_cli_path(cached_path: str | None) -> str | None:
    """
    Verify CLI path is still valid and retry if needed.

    Handles race condition when npm install updates Claude Code during hook execution.
    Uses exponential backoff retry with async sleep to avoid blocking the event loop.

    Args:
        cached_path: Previously cached CLI path to verify.

    Returns:
        Valid CLI path if found, None otherwise.
    """
    cli_path = cached_path

    # Validate cached path still exists
    # Retry with backoff if missing (may be in the middle of npm install)
    if cli_path and not os.path.exists(cli_path):
        logger.warning(f"Cached CLI path no longer exists (may have been reinstalled): {cli_path}")
        # Try to find CLI again with retry logic for npm install race condition
        max_retries = 3
        retry_delays = [0.5, 1.0, 2.0]  # Exponential backoff

        for attempt, delay in enumerate(retry_delays, 1):
            cli_path = shutil.which("claude")
            if cli_path and os.path.exists(cli_path):
                logger.debug(
                    f"Found Claude CLI at new location after {attempt} attempt(s): {cli_path}"
                )
                break

            if attempt < max_retries:
                logger.debug(
                    f"Claude CLI not found, waiting {delay}s before retry {attempt + 1}/{max_retries}"
                )
                await asyncio.sleep(delay)
            else:
                logger.warning(f"Claude CLI not found in PATH after {max_retries} retries")
                cli_path = None

    return cli_path


# ---------------------------------------------------------------------------
# Multi-turn CLI session classes
# ---------------------------------------------------------------------------


class ClaudeCLI:
    """Claude CLI subprocess manager for multi-turn web chat sessions."""

    # Security: only these env vars may be overridden
    ALLOWED_ENV_OVERRIDES = {"ANTHROPIC_BASE_URL", "ANTHROPIC_API_KEY", "CLAUDE_MODEL"}

    def __init__(self, cli_path: str | None = None) -> None:
        self._cli_path = cli_path

    async def _resolve_path(self) -> str:
        """Find and verify CLI path."""
        path = self._cli_path or find_cli_path()
        if not path:
            raise FileNotFoundError("Claude CLI not found")
        return path

    def session(
        self,
        session_id: str | None = None,
        model: str | None = None,
        env_overrides: dict[str, str] | None = None,
    ) -> CLISession:
        """Create a new multi-turn CLI session."""
        if env_overrides:
            disallowed = set(env_overrides) - self.ALLOWED_ENV_OVERRIDES
            if disallowed:
                raise ValueError(f"Disallowed env overrides: {disallowed}")
            # Validate ANTHROPIC_BASE_URL: must be localhost to prevent SSRF
            base_url = env_overrides.get("ANTHROPIC_BASE_URL", "")
            if base_url:
                parsed = urlparse(base_url)
                if parsed.hostname not in ("localhost", "127.0.0.1"):
                    raise ValueError(
                        f"ANTHROPIC_BASE_URL must be localhost, got: {parsed.hostname}"
                    )
        return CLISession(
            cli_path_resolver=self._resolve_path,
            session_id=session_id,
            model=model,
            env_overrides=env_overrides,
        )


class CLISession:
    """Multi-turn Claude CLI session via stream-json I/O."""

    def __init__(
        self,
        cli_path_resolver: object,
        session_id: str | None,
        model: str | None,
        env_overrides: dict[str, str] | None,
    ) -> None:
        self._resolve_path = cli_path_resolver
        self._session_id = session_id
        self._model = model
        self._env_overrides = env_overrides or {}
        self._process: asyncio.subprocess.Process | None = None

    async def start(self) -> None:
        """Spawn CLI subprocess with stream-json I/O."""
        path = await self._resolve_path()
        cmd = [
            path,
            "--output-format",
            "stream-json",
            "--verbose",
            "--input-format",
            "stream-json",
        ]
        if self._session_id:
            cmd.extend(["--session-id", self._session_id])
        if self._model:
            cmd.extend(["--model", self._model])

        env = {**os.environ, **self._env_overrides}
        self._process = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        )

    async def send(self, message: str) -> AsyncIterator[StreamEvent]:
        """Send a message and stream responses."""
        if not self._process or not self._process.stdin:
            raise RuntimeError("CLISession not started — call start() first")
        payload = json.dumps({"type": "user", "content": message}) + "\n"
        self._process.stdin.write(payload.encode())
        await self._process.stdin.drain()
        async for event in parse_stream(self._process.stdout):
            yield event

    async def interrupt(self) -> None:
        """Send interrupt signal to CLI process."""
        if self._process:
            self._process.send_signal(signal.SIGINT)

    async def stop(self) -> None:
        """Terminate CLI process."""
        if self._process:
            self._process.terminate()
            await self._process.wait()
