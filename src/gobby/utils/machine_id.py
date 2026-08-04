"""Machine ID utility.

Provides stable UUID identification stored in ~/.gobby/machine_id.
"""

import threading
import uuid
from pathlib import Path

from gobby.utils.durable_file import durable_replace_text, exclusive_file_lock

__all__ = [
    "clear_cache",
    "get_machine_id",
    "require_machine_id",
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


def require_machine_id() -> str:
    """Return the local machine ID or fail closed when it cannot be resolved."""
    machine_id = get_machine_id()
    if machine_id is None:
        raise RuntimeError("Local machine ID is unavailable")
    return machine_id


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
    with exclusive_file_lock(MACHINE_ID_FILE):
        # Check if file exists and has content
        if MACHINE_ID_FILE.exists():
            MACHINE_ID_FILE.chmod(0o600)
            content = MACHINE_ID_FILE.read_text().strip()
            if content:
                return content

        # Generate new ID and save with atomic permissions
        new_id = _generate_machine_id()
        durable_replace_text(MACHINE_ID_FILE, new_id)

        return new_id


def _generate_machine_id() -> str:
    """Generate a new machine ID.

    Returns:
        Generated machine ID string
    """
    return str(uuid.uuid4())


def clear_cache() -> None:
    """Clear the cached machine ID.

    Useful for testing or when machine ID needs to be refreshed.
    """
    global _cached_machine_id
    with _cache_lock:
        _cached_machine_id = None
