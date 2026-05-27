"""Task ID generation and resolution utilities.

This module provides:
- generate_task_id(): Generate unique task UUIDs
- resolve_task_reference(): Resolve various reference formats to UUIDs
"""

import uuid

from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.tasks._models import TaskNotFoundError


def generate_task_id(project_id: str, salt: str = "") -> str:
    """
    Generate a UUID-based task ID.

    Returns a UUID4 string which provides:
    - Guaranteed uniqueness (128-bit random)
    - Standard format (RFC 4122)
    - Human-friendly reference via seq_num field

    Args:
        project_id: Project ID (included for API compatibility, not used in UUID generation)
        salt: Salt value (included for API compatibility, not used in UUID generation)

    Returns:
        UUID4 string in standard format (xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx)
    """
    # Note: project_id and salt params kept for backward API compatibility
    # UUID4 is random and doesn't need external entropy
    _ = project_id, salt  # Silence unused parameter warnings
    return str(uuid.uuid4())


def resolve_task_reference(db: HubDatabase, ref: str, project_id: str) -> str:
    """Resolve a task reference to its UUID.

    Accepts multiple reference formats:
      - N: Plain seq_num (e.g., 47)
      - #N: Project-scoped seq_num (e.g., #47)
      - 1.2.3: Path cache format
      - UUID: Direct UUID (validated to exist)

    Args:
        db: Database protocol instance
        ref: Task reference in any supported format
        project_id: Project ID for scoped lookups

    Returns:
        The task's UUID

    Raises:
        TaskNotFoundError: If the reference cannot be resolved
    """
    if not ref:
        raise TaskNotFoundError("Empty task reference")

    # #N or plain N format: seq_num lookup
    seq_num: int | None = None
    if ref.startswith("#"):
        try:
            seq_num = int(ref[1:])
        except ValueError:
            raise TaskNotFoundError(f"Invalid seq_num format: {ref}") from None
    elif ref.isdigit():
        seq_num = int(ref)

    if seq_num is not None:
        if seq_num <= 0:
            raise TaskNotFoundError(f"Invalid seq_num: {ref} (must be positive)")

        row = db.fetchone(
            "SELECT id FROM tasks WHERE project_id = %s AND seq_num = %s",
            (project_id, seq_num),
        )
        if not row:
            raise TaskNotFoundError(f"Task {ref} not found in project")
        return str(row["id"])

    # Path format: 1.2.3 (dots with all digits)
    if "." in ref and all(part.isdigit() for part in ref.split(".")):
        row = db.fetchone(
            "SELECT id FROM tasks WHERE project_id = %s AND path_cache = %s",
            (project_id, ref),
        )
        if not row:
            raise TaskNotFoundError(f"Task with path '{ref}' not found in project")
        return str(row["id"])

    # UUID format: validate it exists
    # UUIDs have 5 parts separated by hyphens
    parts = ref.split("-")
    if len(parts) == 5:
        row = db.fetchone(
            "SELECT id FROM tasks WHERE id = %s",
            (ref,),
        )
        if not row:
            raise TaskNotFoundError(f"Task with UUID '{ref}' not found")
        return str(row["id"])

    # Unknown format
    raise TaskNotFoundError(f"Unknown task reference format: {ref}")
