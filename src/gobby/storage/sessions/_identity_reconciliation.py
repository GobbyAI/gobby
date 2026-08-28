"""Reconcile legacy session rows that differ only by machine attribution."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from gobby.storage.hub.protocol import Transaction


class AmbiguousSessionIdentityError(RuntimeError):
    """Raised when multiple canonical-identity rows contain owned history."""


@dataclass(frozen=True)
class SessionIdentityReconciliation:
    """Result of reconciling one canonical provider-session identity."""

    canonical_id: str | None
    deleted_ids: tuple[str, ...] = ()


_HISTORY_FIELDS = (
    "transcript_path",
    "summary_path",
    "summary_markdown",
    "handoff_markdown",
    "summary_revision_id",
    "summary_source_context_hash",
    "summary_generation_mode",
    "summary_generated_at",
    "parent_session_id",
    "transcript_processed",
    "agent_depth",
    "spawned_by_agent_id",
    "workflow_name",
    "agent_run_id",
    "context_injected",
    "original_prompt",
    "usage_input_tokens",
    "usage_output_tokens",
    "usage_cache_creation_tokens",
    "usage_cache_read_tokens",
    "context_window",
    "context_used_tokens",
    "context_usage_ratio",
    "context_usage_source",
    "context_usage_confidence",
    "context_usage_updated_at",
    "last_prompt_input_tokens",
    "last_prompt_uncached_input_tokens",
    "last_prompt_cache_read_tokens",
    "last_prompt_cache_creation_tokens",
    "last_completion_output_tokens",
    "model",
    "had_edits",
    "message_count",
    "turn_count",
    "tool_call_count",
    "last_assistant_content",
    "approved_tools_json",
)


def _has_inline_history(row: Mapping[str, Any]) -> bool:
    return any(row.get(field) not in (None, False, 0, "", {}, []) for field in _HISTORY_FIELDS)


def _session_reference_columns(conn: Transaction) -> list[tuple[str, str]]:
    rows = conn.execute(
        """
        SELECT
            quote_ident(child_ns.nspname) || '.' || quote_ident(child.relname) AS table_name,
            quote_ident(child_attr.attname) AS column_name
        FROM pg_constraint AS fk
        JOIN LATERAL unnest(fk.conkey) WITH ORDINALITY AS child_key(attnum, ord)
          ON TRUE
        JOIN LATERAL unnest(fk.confkey) WITH ORDINALITY AS parent_key(attnum, ord)
          ON parent_key.ord = child_key.ord
        JOIN pg_class AS child ON child.oid = fk.conrelid
        JOIN pg_namespace AS child_ns ON child_ns.oid = child.relnamespace
        JOIN pg_attribute AS child_attr
          ON child_attr.attrelid = child.oid AND child_attr.attnum = child_key.attnum
        JOIN pg_attribute AS parent_attr
          ON parent_attr.attrelid = fk.confrelid AND parent_attr.attnum = parent_key.attnum
        WHERE fk.contype = 'f'
          AND fk.confrelid = 'sessions'::regclass
          AND parent_attr.attname = 'id'
        ORDER BY table_name, column_name
        """
    ).fetchall()
    return [(str(row["table_name"]), str(row["column_name"])) for row in rows]


def _has_referenced_history(
    conn: Transaction,
    session_id: str,
    reference_columns: list[tuple[str, str]],
) -> bool:
    for table_name, column_name in reference_columns:
        # pg_catalog format(%I) produced both identifiers above.
        query = (
            f"SELECT EXISTS (SELECT 1 FROM {table_name} "  # nosec B608
            f"WHERE {column_name} = %s) AS has_reference"
        )
        row = conn.execute(query, (session_id,)).fetchone()
        if row and row["has_reference"]:
            return True
    return False


def reconcile_session_identity(
    conn: Transaction,
    *,
    external_id: str,
    source: str,
    project_id: str,
    session_type: str,
) -> SessionIdentityReconciliation:
    """Collapse unambiguous legacy duplicates for one canonical identity."""
    rows = conn.execute(
        """
        SELECT *
        FROM sessions
        WHERE external_id = %s
          AND source = %s
          AND project_id = %s
          AND session_type = %s
        ORDER BY created_at, id
        FOR UPDATE
        """,
        (external_id, source, project_id, session_type),
    ).fetchall()
    if len(rows) <= 1:
        return SessionIdentityReconciliation(
            canonical_id=str(rows[0]["id"]) if rows else None,
        )

    reference_columns = _session_reference_columns(conn)
    populated_rows = [
        row
        for row in rows
        if _has_inline_history(row)
        or _has_referenced_history(conn, str(row["id"]), reference_columns)
    ]
    if len(populated_rows) > 1:
        populated_ids = ", ".join(str(row["id"]) for row in populated_rows)
        raise AmbiguousSessionIdentityError(
            "Multiple sessions with canonical identity "
            f"{external_id!r}/{source!r}/{project_id!r}/{session_type!r} contain history: "
            f"{populated_ids}"
        )

    canonical = populated_rows[0] if populated_rows else rows[0]
    canonical_id = str(canonical["id"])
    duplicates = [row for row in rows if str(row["id"]) != canonical_id]
    deleted_ids = tuple(str(row["id"]) for row in duplicates)
    attributed_machine_id = next(
        (
            row["machine_id"]
            for row in sorted(
                rows,
                key=lambda item: (item["updated_at"], str(item["id"])),
                reverse=True,
            )
            if row["machine_id"] is not None
        ),
        None,
    )
    conn.execute("DELETE FROM sessions WHERE id = ANY(%s)", (list(deleted_ids),))
    if attributed_machine_id is not None and canonical["machine_id"] is None:
        conn.execute(
            "UPDATE sessions SET machine_id = %s WHERE id = %s",
            (attributed_machine_id, canonical_id),
        )
    return SessionIdentityReconciliation(canonical_id=canonical_id, deleted_ids=deleted_ids)
