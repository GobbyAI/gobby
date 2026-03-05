"""Claude CLI path management.

Functions for finding and verifying the Claude CLI binary.
Extracted from ClaudeLLMProvider as part of the Strangler Fig
decomposition.
"""

from __future__ import annotations

import asyncio
import logging
import os
import shutil

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
