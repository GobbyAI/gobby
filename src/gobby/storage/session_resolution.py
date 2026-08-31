"""Session reference resolution.

Resolves session references (#N, <project>-S#N, N, UUID, prefix) to UUIDs.
Extracted from SessionManager.resolve_session_reference()
as part of the Strangler Fig decomposition.
"""

from __future__ import annotations

import re

from gobby.storage.hub.protocol import HubDatabase
from gobby.utils.uuid_validation import parse_uuid_reference

# '<project>-S#<seq>': project name or UUID, then that project's seq_num.
_PROJECT_QUALIFIED_SEQ_NUM_RE = re.compile(r"^(?P<project>.+)-S#(?P<seq>\d+)$")


def is_project_qualified_session_ref(value: str | None) -> bool:
    """Return whether a value uses the '<project>-S#N' cross-project session form."""
    return isinstance(value, str) and _PROJECT_QUALIFIED_SEQ_NUM_RE.match(value) is not None


def _resolve_project_id(db: HubDatabase, project_ref: str) -> str | None:
    """Resolve an active project by UUID, then by name."""
    row = None
    if parse_uuid_reference(project_ref) is not None:
        row = db.fetchone(
            "SELECT id FROM projects WHERE id = %s AND deleted_at IS NULL", (project_ref,)
        )
    if row is None:
        row = db.fetchone(
            "SELECT id FROM projects WHERE name = %s AND deleted_at IS NULL", (project_ref,)
        )
    return str(row["id"]) if row else None


def _escape_like_prefix(prefix: str) -> str:
    """Escape SQL LIKE wildcard characters in a session prefix."""
    return prefix.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def is_session_uuid(value: str | None) -> bool:
    """Return whether a value can be safely used against session UUID columns."""
    return parse_uuid_reference(value) is not None


def resolve_session_reference(db: HubDatabase, ref: str, project_id: str | None = None) -> str:
    """
    Resolve a session reference to a UUID.

    Supports:
    - #N: Project-scoped Sequence Number (e.g., #1) - requires project_id
    - <project>-S#N: Sequence Number inside a named project (e.g., gobby-S#11265);
      the project part is an active project name or UUID and ignores project_id
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

    qualified = _PROJECT_QUALIFIED_SEQ_NUM_RE.match(ref)
    if qualified is not None:
        project_ref = qualified.group("project")
        seq_num = int(qualified.group("seq"))
        qualified_project_id = _resolve_project_id(db, project_ref)
        if qualified_project_id is None:
            raise ValueError(f"Cannot resolve session '{ref}': project '{project_ref}' not found")
        row = db.fetchone(
            "SELECT id FROM sessions WHERE project_id = %s AND seq_num = %s",
            (qualified_project_id, seq_num),
        )
        if not row:
            raise ValueError(f"Session #{seq_num} not found in project '{project_ref}'")
        return str(row["id"])

    # #N is always a seq_num lookup. Plain numeric refs are treated as seq_num
    # only when they do not uniquely identify a session UUID prefix.
    seq_num_ref = ref[1:] if ref.startswith("#") else ref

    if ref.startswith("#"):
        is_seq_num_lookup = seq_num_ref.isdigit()
    else:
        is_seq_num_lookup = seq_num_ref.isdigit()
        if is_seq_num_lookup and len(seq_num_ref) >= 8:
            prefix_rows = db.fetchall(
                "SELECT id FROM sessions WHERE id::text LIKE %s ESCAPE '\\' LIMIT 2",
                (f"{_escape_like_prefix(seq_num_ref)}%",),
            )
            if len(prefix_rows) == 1:
                return str(prefix_rows[0]["id"])

    if is_seq_num_lookup:
        seq_num = int(seq_num_ref)
        if not project_id:
            raise ValueError(
                f"Cannot resolve session #{seq_num}: project context is required "
                f"for #N lookups. Pass a full UUID or ensure project context is set."
            )
        row = db.fetchone(
            "SELECT id FROM sessions WHERE project_id = %s AND seq_num = %s",
            (project_id, seq_num),
        )
        if not row:
            raise ValueError(
                f"Session #{seq_num} not found in project. #N refs are project-scoped; "
                "address a session in another project as '<project>-S#N' "
                "(project name or UUID) or by its full session UUID."
            )
        return str(row["id"])

    # Full UUID check
    uuid_obj = parse_uuid_reference(ref)
    if uuid_obj is not None:
        canonical = str(uuid_obj)
        # Primary-key lookup wins.
        row = db.fetchone("SELECT id FROM sessions WHERE id = %s", (canonical,))
        if row:
            return str(row["id"])

        # Fall back to external_id. A single external_id can legally map to
        # multiple rows (same external_id across machine_id/source in one
        # project, or across projects when no scope is supplied) — force the
        # caller to disambiguate rather than picking one silently.
        if project_id:
            ext_rows = db.fetchall(
                "SELECT id FROM sessions WHERE external_id = %s AND project_id = %s LIMIT 5",
                (canonical, project_id),
            )
        else:
            ext_rows = db.fetchall(
                "SELECT id FROM sessions WHERE external_id = %s LIMIT 5",
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
    escaped_prefix = f"{_escape_like_prefix(ref)}%"
    rows = db.fetchall(
        "SELECT id FROM sessions WHERE id::text LIKE %s ESCAPE '\\' LIMIT 5",
        (escaped_prefix,),
    )
    if rows:
        if len(rows) > 1:
            matches = [str(r["id"]) for r in rows]
            raise ValueError(f"Ambiguous session '{ref}' matches: {', '.join(matches[:3])}...")
        return str(rows[0]["id"])

    if project_id:
        ext_rows = db.fetchall(
            "SELECT id FROM sessions "
            "WHERE external_id LIKE %s ESCAPE '\\' AND project_id = %s LIMIT 5",
            (escaped_prefix, project_id),
        )
    else:
        ext_rows = db.fetchall(
            "SELECT id FROM sessions WHERE external_id LIKE %s ESCAPE '\\' LIMIT 5",
            (escaped_prefix,),
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
