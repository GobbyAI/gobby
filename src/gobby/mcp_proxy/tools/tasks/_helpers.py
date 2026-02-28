"""Helper functions and constants for task tools.

Pure functions with no external dependencies that provide utility
for task operations.
"""

# Reasons for which commit linking and validation are skipped when closing tasks
SKIP_REASONS: frozenset[str] = frozenset(
    {"duplicate", "already_implemented", "wont_fix", "obsolete", "out_of_repo"}
)


def _is_path_format(ref: str) -> bool:
    """Check if a reference is in path format (e.g., 1.2.3)."""
    if "." not in ref:
        return False
    parts = ref.split(".")
    return all(part.isdigit() for part in parts)
