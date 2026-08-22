"""Machine ID utility.

Provides stable UUID identification stored in the active Gobby home.
"""

import threading
import uuid
from pathlib import Path

from gobby.paths import get_gobby_home
from gobby.utils.durable_file import durable_replace_text, exclusive_file_lock

__all__ = [
    "clear_cache",
    "get_machine_id",
    "get_machine_id_file",
    "require_machine_id",
]

# Thread-safe cache
_cache_lock = threading.Lock()
_cached_machine_id: str | None = None


def get_machine_id_file() -> Path:
    """Return the identity file in the active Gobby home."""
    return get_gobby_home() / "machine_id"


def get_machine_id() -> str | None:
    """Get the stable machine ID from the active Gobby home.

    Strategy:
    1. Return cached ID if available
    2. Check the active Gobby home's machine_id file
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
    """Get or create the machine ID in the active Gobby home.

    Strategy:
    1. Read from file if present
    2. Generate new ID and save to file (legacy config.yaml migration removed)
    3. Generate new ID and save to file

    Returns:
        Machine ID string

    Raises:
        OSError: If file operations fail
    """
    machine_id_file = get_machine_id_file()
    existing = _read_machine_id(machine_id_file)
    if existing is not None:
        return existing
    with exclusive_file_lock(machine_id_file):
        existing = _read_machine_id(machine_id_file)
        if existing is not None:
            return existing

        # Generate new ID and save with atomic permissions
        new_id = _generate_machine_id()
        durable_replace_text(machine_id_file, new_id)

        return new_id


def _read_machine_id(machine_id_file: Path) -> str | None:
    """Return the present identity without taking the creation lock.

    ``durable_replace_text`` publishes the file atomically, so a lock-free read
    sees either nothing or a complete identity. Managed executions run in
    sandboxes that deny writes under the Gobby home, which rules out the lock
    sidecar and the permission repair while the identity itself stays readable.
    """
    try:
        content = machine_id_file.read_text().strip()
    except FileNotFoundError:
        return None
    if not content:
        return None
    try:
        machine_id_file.chmod(0o600)
    except PermissionError:
        pass
    return content


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
