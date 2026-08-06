"""Helper functions and constants for task tools.

Pure functions with no external dependencies that provide utility
for task operations.
"""

import uuid

from gobby.tasks.validation import NO_WORK_CLOSE_REASONS


def _is_uuid(value: str) -> bool:
    """Check if a string is a valid UUID (not a ref like #123)."""
    try:
        uuid.UUID(value)
        return True
    except (ValueError, TypeError):
        return False


# Reasons for which commit linking and validation are skipped when closing tasks
SKIP_REASONS: frozenset[str] = NO_WORK_CLOSE_REASONS


def _is_path_format(ref: str) -> bool:
    """Check if a reference is in path format (e.g., 1.2.3)."""
    if "." not in ref:
        return False
    parts = ref.split(".")
    return all(part.isdigit() for part in parts)
