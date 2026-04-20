"""Session reference resolution.

Resolves session references (#N, N, UUID, prefix) to UUIDs.
Extracted from LocalSessionManager.resolve_session_reference()
as part of the Strangler Fig decomposition.
"""

from __future__ import annotations

import uuid

from gobby.storage.database import DatabaseProtocol


def resolve_session_reference(db: DatabaseProtocol, ref: str, project_id: str | None = None) -> str:
    """
    Resolve a session reference to a UUID.

    Supports:
    - #N: Project-scoped Sequence Number (e.g., #1) - requires project_id
    - N: Integer string treated as #N (e.g., "1")
    - UUID: Full UUID — matches on sessions.id, then sessions.external_id
    - Prefix: UUID prefix — matches on sessions.id, then sessions.external_id

    For UUID and prefix refs the id column is authoritative: primary-key matches
    always win. If there is no id match we fall back to external_id. When a
    project_id is supplied we scope the external_id lookup to that project;
    without it we scan across projects.

    A single external_id can legally belong to multiple rows (the UNIQUE index
    is ``(external_id, machine_id, source, project_id)``). When that happens we
    raise ``ValueError`` so callers must disambiguate with the primary-key UUID —
    silent nondeterminism here is what drove the original bug.

    Args:
        db: Database connection.
        ref: Session reference string.
        project_id: Project ID. Required for #N lookups. Optional for UUID and
            prefix refs; when provided, scopes the external_id / prefix fallback
            to that project.

    Returns:
        Resolved Session UUID (always the primary-key ``id``, never external_id).

    Raises:
        ValueError: If not found, or if an external_id / prefix match is
            ambiguous (caller must pass the primary-key UUID to disambiguate).
    """
    if not ref:
        raise ValueError("Empty session reference")

    # #N or N format: seq_num lookup
    seq_num_ref = ref
    if ref.startswith("#"):
        seq_num_ref = ref[1:]

    if seq_num_ref.isdigit():
        seq_num = int(seq_num_ref)
        if not project_id:
            raise ValueError(
                f"Cannot resolve session #{seq_num}: project context is required "
                f"for #N lookups. Pass a full UUID or ensure project context is set."
            )
        row = db.fetchone(
            "SELECT id FROM sessions WHERE project_id = ? AND seq_num = ?",
            (project_id, seq_num),
        )
        if not row:
            raise ValueError(f"Session #{seq_num} not found in project")
        return str(row["id"])

    # Full UUID check
    try:
        uuid_obj = uuid.UUID(ref)
        is_valid_uuid = True
    except ValueError:
        is_valid_uuid = False

    if is_valid_uuid:
        canonical = str(uuid_obj)
        # Primary-key lookup wins.
        row = db.fetchone("SELECT id FROM sessions WHERE id = ?", (canonical,))
        if row:
            return str(row["id"])

        # Fall back to external_id. A single external_id can legally map to
        # multiple rows (same external_id across machine_id/source in one
        # project, or across projects when no scope is supplied) — force the
        # caller to disambiguate rather than picking one silently.
        if project_id:
            ext_rows = db.fetchall(
                "SELECT id FROM sessions WHERE external_id = ? AND project_id = ? LIMIT 5",
                (canonical, project_id),
            )
        else:
            ext_rows = db.fetchall(
                "SELECT id FROM sessions WHERE external_id = ? LIMIT 5",
                (canonical,),
            )
        if not ext_rows:
            raise ValueError(f"Session '{ref}' not found")
        if len(ext_rows) > 1:
            matches = [str(r["id"]) for r in ext_rows]
            scope = f"project {project_id}" if project_id else "database"
            suffix = "..." if len(ext_rows) > 3 else ""
            raise ValueError(
                f"Ambiguous session reference '{ref}' — external_id matches "
                f"{len(ext_rows)} sessions in {scope} "
                f"(ids: {', '.join(matches[:3])}{suffix}). "
                "Pass the primary-key UUID to disambiguate."
            )
        return str(ext_rows[0]["id"])

    # Prefix matching — id first, then external_id.
    rows = db.fetchall("SELECT id FROM sessions WHERE id LIKE ? LIMIT 5", (f"{ref}%",))
    if rows:
        if len(rows) > 1:
            matches = [str(r["id"]) for r in rows]
            raise ValueError(f"Ambiguous session '{ref}' matches: {', '.join(matches[:3])}...")
        return str(rows[0]["id"])

    if project_id:
        ext_rows = db.fetchall(
            "SELECT id FROM sessions WHERE external_id LIKE ? AND project_id = ? LIMIT 5",
            (f"{ref}%", project_id),
        )
    else:
        ext_rows = db.fetchall(
            "SELECT id FROM sessions WHERE external_id LIKE ? LIMIT 5",
            (f"{ref}%",),
        )
    if not ext_rows:
        raise ValueError(f"Session '{ref}' not found")
    if len(ext_rows) > 1:
        matches = [str(r["id"]) for r in ext_rows]
        scope = f"project {project_id}" if project_id else "database"
        suffix = "..." if len(ext_rows) > 3 else ""
        raise ValueError(
            f"Ambiguous session prefix '{ref}' — external_id prefix matches "
            f"{len(ext_rows)} sessions in {scope} "
            f"(ids: {', '.join(matches[:3])}{suffix}). "
            "Pass the primary-key UUID to disambiguate."
        )
    return str(ext_rows[0]["id"])
