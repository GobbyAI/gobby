"""Machine ID utility.

Provides stable machine identification stored in ~/.gobby/machine_id.
Uses py-machineid for hardware-based IDs with UUID fallback.
"""

import logging
import os
import threading
import uuid
from pathlib import Path

logger = logging.getLogger(__name__)

# Thread-safe cache
_cache_lock = threading.Lock()
_cached_machine_id: str | None = None

# Default location for machine ID file
MACHINE_ID_FILE = Path("~/.gobby/machine_id").expanduser()


def get_machine_id() -> str | None:
    """Get stable machine ID from ~/.gobby/machine_id.

    Strategy:
    1. Return cached ID if available
    2. Check ~/.gobby/machine_id file
    3. If not present, generate ID and save to file

    Returns:
        Machine ID as string, or None if operations fail

    Raises:
        OSError: If file operations fail
    """
    global _cached_machine_id

    # Fast path: Return cached ID
    with _cache_lock:
        if _cached_machine_id is not None:
            return _cached_machine_id

    try:
        machine_id = _get_or_create_machine_id()
        if machine_id:
            with _cache_lock:
                _cached_machine_id = machine_id
            return machine_id
    except OSError as e:
        # Let OSError propagate for file system issues
        raise OSError(f"Failed to retrieve or create machine ID: {e}") from e

    return None


def _get_or_create_machine_id() -> str:
    """Get or create machine ID from ~/.gobby/machine_id.

    Strategy:
    1. Read from file if present
    2. Migrate from config.yaml if present there (one-time migration)
    3. Generate new ID and save to file

    Returns:
        Machine ID string

    Raises:
        OSError: If file operations fail
    """
    # Ensure directory exists
    MACHINE_ID_FILE.parent.mkdir(parents=True, exist_ok=True)

    # Check if file exists and has content
    if MACHINE_ID_FILE.exists():
        content = MACHINE_ID_FILE.read_text().strip()
        if content:
            return content

    # Generate new ID and save with atomic permissions
    new_id = _generate_machine_id()
    _write_file_secure(MACHINE_ID_FILE, new_id)

    return new_id


def _write_file_secure(path: Path, content: str) -> None:
    """Write content to file with restrictive permissions atomically.

    Uses os.open with O_CREAT to set permissions at creation time,
    avoiding TOCTOU race condition with write_text()/chmod() pattern.

    Args:
        path: File path to write to
        content: Content to write

    Raises:
        OSError: If file operations fail
    """
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, content.encode())
    finally:
        os.close(fd)


def _generate_machine_id() -> str:
    """Generate a new machine ID.

    Uses py-machineid for hardware-based ID, falls back to UUID4.

    Returns:
        Generated machine ID string
    """
    try:
        import machineid

        return machineid.id()
    except ImportError:
        # Library not available, use UUID fallback
        return str(uuid.uuid4())
    except Exception as e:
        # machineid library failed (hardware access issues, etc.)
        logger.debug(f"machineid.id() failed, using UUID fallback: {e}")
        return str(uuid.uuid4())


def clear_cache() -> None:
    """Clear the cached machine ID.

    Useful for testing or when machine ID needs to be refreshed.
    """
    global _cached_machine_id
    with _cache_lock:
        _cached_machine_id = None
