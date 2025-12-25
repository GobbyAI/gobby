"""Machine ID utility.

Provides stable machine identification stored in config.yaml.
Uses py-machineid for hardware-based IDs with UUID fallback.
"""

import threading
import uuid

# Thread-safe cache
_cache_lock = threading.Lock()
_cached_machine_id: str | None = None


def get_machine_id() -> str | None:
    """Get stable machine ID from config.yaml.

    Strategy:
    1. Return cached ID if available
    2. Check config.yaml for machine_id
    3. If not present, generate ID and save to config.yaml

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

    # Try config.yaml first, then fall back to legacy file
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
    """Get or create machine ID from config.yaml.

    Strategy:
    1. Read from config.yaml if present
    2. Generate new ID and save to config.yaml

    Returns:
        Machine ID string

    Raises:
        OSError: If file operations fail
    """
    from gobby.config.app import load_config, save_config

    # Load config (creates default if doesn't exist)
    config = load_config(create_default=True)

    # If config has machine_id, return it
    if config.machine_id:
        return config.machine_id  # type: ignore[no-any-return]

    # Config doesn't have it - generate new ID
    new_id: str

    # Try to use hardware-based ID from library
    try:
        import machineid

        new_id = machineid.id()
    except (ImportError, Exception):
        # Fallback to UUID4 if library not available
        new_id = str(uuid.uuid4())

    # Save to config.yaml
    config.machine_id = new_id
    save_config(config)

    return new_id


def clear_cache() -> None:
    """Clear the cached machine ID.

    Useful for testing or when machine ID needs to be refreshed.
    """
    global _cached_machine_id
    with _cache_lock:
        _cached_machine_id = None
