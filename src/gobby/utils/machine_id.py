"""Machine ID utility.

Provides stable machine identification stored in ~/.gobby/machine_id.
Uses py-machineid for hardware-based IDs with UUID fallback.
"""

import fcntl
import logging
import os
import threading
import uuid
from pathlib import Path

logger = logging.getLogger(__name__)

LEGACY_MISSING_MACHINE_ID_PREFIX = "legacy-missing:"

__all__ = [
    "LEGACY_MISSING_MACHINE_ID_PREFIX",
    "clear_cache",
    "get_machine_id",
    "is_legacy_missing_machine_id",
    "new_legacy_missing_machine_id",
]

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


def new_legacy_missing_machine_id() -> str:
    """Return a unique legacy session-only machine id for missing client identity."""
    return f"{LEGACY_MISSING_MACHINE_ID_PREFIX}{uuid.uuid4()}"


def is_legacy_missing_machine_id(machine_id: str | None) -> bool:
    """Return True when machine_id is a per-registration missing-client sentinel."""
    return (machine_id or "").strip().lower().startswith(LEGACY_MISSING_MACHINE_ID_PREFIX)


def _get_or_create_machine_id() -> str:
    """Get or create machine ID from ~/.gobby/machine_id.

    Strategy:
    1. Read from file if present
    2. Generate new ID and save to file (legacy config.yaml migration removed)
    3. Generate new ID and save to file

    Returns:
        Machine ID string

    Raises:
        OSError: If file operations fail
    """
    # Ensure directory exists
    MACHINE_ID_FILE.parent.mkdir(parents=True, exist_ok=True)

    lock_path = MACHINE_ID_FILE.with_name(f".{MACHINE_ID_FILE.name}.lock")
    lock_fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        os.fchmod(lock_fd, 0o600)
        fcntl.flock(lock_fd, fcntl.LOCK_EX)

        # Check if file exists and has content
        if MACHINE_ID_FILE.exists():
            MACHINE_ID_FILE.chmod(0o600)
            content = MACHINE_ID_FILE.read_text().strip()
            if content:
                return content

        # Generate new ID and save with atomic permissions
        new_id = _generate_machine_id()
        _write_file_secure(MACHINE_ID_FILE, new_id)

        return new_id
    finally:
        os.close(lock_fd)


def _write_file_secure(path: Path, content: str) -> None:
    """Write content to file with restrictive permissions atomically.

    Writes and syncs an owner-only temporary file in the destination directory,
    then atomically replaces the destination.

    Args:
        path: File path to write to
        content: Content to write

    Raises:
        OSError: If file operations fail
    """
    temp_path = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    fd = os.open(temp_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        try:
            os.fchmod(fd, 0o600)
            remaining = memoryview(content.encode())
            while remaining:
                written = os.write(fd, remaining)
                if written <= 0:
                    raise OSError("Failed to write machine ID")
                remaining = remaining[written:]
            os.fsync(fd)
        finally:
            os.close(fd)

        os.replace(temp_path, path)
    finally:
        temp_path.unlink(missing_ok=True)


def _generate_machine_id() -> str:
    """Generate a new machine ID.

    Uses py-machineid for an app-scoped hardware ID hash, falls back to UUID4.

    Returns:
        Generated machine ID string
    """
    try:
        import machineid

        return str(machineid.hashed_id("gobby"))
    except ImportError:
        # Library not available, use UUID fallback
        return str(uuid.uuid4())
    except Exception as e:
        # machineid library failed (hardware access issues, etc.)
        logger.debug(f"machineid.hashed_id() failed, using UUID fallback: {e}")
        return str(uuid.uuid4())


def clear_cache() -> None:
    """Clear the cached machine ID.

    Useful for testing or when machine ID needs to be refreshed.
    """
    global _cached_machine_id
    with _cache_lock:
        _cached_machine_id = None
