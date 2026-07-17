"""Prompt file management for agent spawning.

Handles creation, cleanup, and tracking of temporary prompt files
used to pass long prompts to spawned CLI agents.
"""

from __future__ import annotations

import atexit
import logging
import os
import re
import tempfile
import threading
import uuid
from pathlib import Path

logger = logging.getLogger(__name__)

# Maximum prompt length to pass via environment variable
# Longer prompts will be written to a temp file
MAX_ENV_PROMPT_LENGTH = 4096

# Module-level set for tracking prompt files to clean up on exit
# This avoids registering a new atexit handler for each prompt file
_prompt_files_to_cleanup: set[Path] = set()
_atexit_registered = False
_atexit_lock = threading.Lock()


def cleanup_all_prompt_files() -> None:
    """Clean up all tracked prompt files on process exit."""
    with _atexit_lock:
        files_to_cleanup = list(_prompt_files_to_cleanup)
        _prompt_files_to_cleanup.clear()
        for prompt_path in files_to_cleanup:
            try:
                if prompt_path.exists():
                    prompt_path.unlink()
            except OSError:
                pass


def create_prompt_file(prompt: str, session_id: str) -> str:
    """
    Create a prompt file with secure permissions.

    The file is created in the system temp directory with restrictive
    permissions (owner read/write only) and tracked for cleanup on exit.

    Args:
        prompt: The prompt content to write
        session_id: Session ID for naming the file

    Returns:
        Path to the created temp file
    """
    global _atexit_registered

    # Create temp directory with restrictive permissions
    temp_dir = Path(tempfile.gettempdir()) / "gobby-prompts"
    temp_dir.mkdir(parents=True, exist_ok=True, mode=0o700)

    # Sanitize session_id to prevent path traversal attacks
    # Strip path separators and limit to alphanumeric, hyphens, underscores
    safe_session_id = re.sub(r"[^a-zA-Z0-9_-]", "", session_id)
    if not safe_session_id or len(safe_session_id) > 128:
        safe_session_id = str(uuid.uuid4())

    # Create the prompt file path
    prompt_path = temp_dir / f"prompt-{safe_session_id}.txt"

    # Write with secure permissions atomically - create with mode 0o600 from the start
    # This avoids the TOCTOU window between write_text and chmod
    fd = os.open(str(prompt_path), os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(prompt)
            f.flush()
            os.fsync(f.fileno())
    except Exception:
        # fd is closed by fdopen, but if fdopen fails we need to close it
        try:
            os.close(fd)
        except OSError:
            pass
        raise

    # Track for cleanup and register handler (thread-safe)
    with _atexit_lock:
        _prompt_files_to_cleanup.add(prompt_path)
        if not _atexit_registered:
            atexit.register(cleanup_all_prompt_files)
            _atexit_registered = True

    logger.debug("Created secure prompt file: %s", prompt_path)
    return str(prompt_path)
