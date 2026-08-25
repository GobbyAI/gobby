"""Shared scope, conflict, codec, and revision helpers for definition managers."""

from __future__ import annotations

import hashlib
import json
import logging
from collections.abc import Mapping, Sequence
from typing import Any, Literal
from uuid import uuid4

from gobby.storage.definitions.revisions import (
    DefinitionDomain,
    advance_persistent_revision,
    bump_definitions_revision,
)
from gobby.storage.hub.protocol import HubDatabase, Transaction
from gobby.storage.sql_dialect import older_than_now_expr
from gobby.utils.datetime import utc_now
from gobby.utils.sql import render_internal_sql
from gobby.utils.uuid_validation import parse_uuid_reference

logger = logging.getLogger(__name__)

DefinitionSource = Literal["installed", "custom", "project"]
_KNOWN_TABLES = frozenset(
    {
        "rule_definitions",
        "session_variable_defaults",
        "pipeline_definitions",
        "agent_definitions",
    }
)


class DefinitionNameConflictError(Exception):
    """A live definition already uses this name in the target scope."""


class DefinitionNotFoundError(Exception):
    """No matching definition row exists for the requested lookup."""


def compute_definition_hash(definition_json: str) -> str:
    """Compute a SHA-256 hash of a canonical definition JSON string.

    Used for cheap drift detection between installed definitions
    and their on-disk template files.
    """
    canonical_json = json.dumps(
        json.loads(definition_json),
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical_json.encode()).hexdigest()


def encode_json_list(value: list[str] | None) -> str | None:
    if value is None:
        return None
    return json.dumps(value)


def decode_json_list(value: Any) -> list[str] | None:
    if value is None:
        return None
    if isinstance(value, list):
        return [str(item) for item in value]
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return None
        if isinstance(parsed, list):
            return [str(item) for item in parsed]
    return None


def encode_json_value(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        return value
    return json.dumps(value)


def encode_json_scalar(value: Any) -> str | None:
    if value is None:
        return None
    return json.dumps(value)


def decode_json_object(value: Any) -> dict[str, Any] | None:
    if value is None:
        return None
    if isinstance(value, dict):
        return dict(value)
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return None
        if isinstance(parsed, dict):
            return parsed
    return None


def new_definition_id() -> str:
    return str(uuid4())


def require_definition_id(definition_id: str, *, what: str) -> str:
    if parse_uuid_reference(definition_id) is None:
        raise DefinitionNotFoundError(f"{what} {definition_id} not found") from None
    return definition_id


def touch_revision(txn: Transaction, domain: DefinitionDomain) -> None:
    advance_persistent_revision(txn, domain)
    txn.after_commit(lambda: bump_definitions_revision(domain))


def _validate_table(table: str) -> str:
    if table not in _KNOWN_TABLES:
        raise ValueError(f"Unknown definition table: {table}")
    return table


def live_name_taken(
    txn: Transaction,
    table: str,
    name: str,
    project_id: str | None,
    *,
    exclude_id: str | None = None,
) -> bool:
    table = _validate_table(table)
    query = render_internal_sql(
        "SELECT 1 FROM {table} WHERE name = %s AND deleted_at IS NULL AND ",
        table=table,
    )
    params: list[Any] = [name]
    if project_id is None:
        query += "project_id IS NULL"
    else:
        query += "project_id = %s"
        params.append(project_id)
    if exclude_id is not None:
        query += " AND id <> %s"
        params.append(exclude_id)
    return txn.execute(query, tuple(params)).fetchone() is not None


def assert_live_name_free(
    txn: Transaction,
    table: str,
    name: str,
    project_id: str | None,
    *,
    exclude_id: str | None = None,
    what: str,
) -> None:
    if live_name_taken(txn, table, name, project_id, exclude_id=exclude_id):
        scope = "global" if project_id is None else f"project {project_id}"
        raise DefinitionNameConflictError(f"{what} {name!r} already exists in {scope}")


def fetch_definition_row(
    db: HubDatabase,
    table: str,
    definition_id: str,
    *,
    include_deleted: bool,
    what: str,
) -> Mapping[str, Any]:
    table = _validate_table(table)
    require_definition_id(definition_id, what=what)
    query = render_internal_sql("SELECT * FROM {table} WHERE id = %s", table=table)
    params: tuple[Any, ...] = (definition_id,)
    if not include_deleted:
        query += " AND deleted_at IS NULL"
    row = db.fetchone(query, params)
    if row is None:
        raise DefinitionNotFoundError(f"{what} {definition_id} not found")
    return row


def fetch_definition_by_name(
    db: HubDatabase,
    table: str,
    name: str,
    project_id: str | None,
    *,
    include_deleted: bool,
) -> Mapping[str, Any] | None:
    table = _validate_table(table)
    deleted = "" if include_deleted else " AND deleted_at IS NULL"
    if project_id is not None:
        row = db.fetchone(
            render_internal_sql(
                "SELECT * FROM {table} WHERE name = %s AND project_id = %s{deleted}",
                table=table,
                deleted=deleted,
            ),
            (name, project_id),
        )
        if row is not None:
            return row
    return db.fetchone(
        render_internal_sql(
            "SELECT * FROM {table} WHERE name = %s AND project_id IS NULL{deleted}",
            table=table,
            deleted=deleted,
        ),
        (name,),
    )


def insert_definition_row(
    txn: Transaction,
    table: str,
    columns: Sequence[str],
    values: Sequence[Any],
) -> None:
    table = _validate_table(table)
    placeholders = ", ".join(["%s"] * len(columns))
    column_sql = ", ".join(columns)
    txn.execute(
        render_internal_sql(
            "INSERT INTO {table} ({columns}) VALUES ({placeholders})",
            table=table,
            columns=column_sql,
            placeholders=placeholders,
        ),
        tuple(values),
    )


def list_definition_rows(
    db: HubDatabase,
    table: str,
    *,
    project_id: str | None,
    enabled: bool | None,
    include_deleted: bool,
    extra_conditions: Sequence[str] = (),
    extra_params: Sequence[Any] = (),
    order_by: str = "name",
) -> list[Mapping[str, Any]]:
    table = _validate_table(table)
    if order_by not in {"name", "priority, name"}:
        raise ValueError(f"Unsupported definition order: {order_by}")
    conditions: list[str] = []
    params: list[Any] = []
    if not include_deleted:
        conditions.append("deleted_at IS NULL")
    if project_id is not None:
        conditions.append("(project_id = %s OR project_id IS NULL)")
        params.append(project_id)
    if enabled is not None:
        conditions.append("enabled = %s")
        params.append(enabled)
    conditions.extend(extra_conditions)
    params.extend(extra_params)
    where = f" WHERE {' AND '.join(conditions)}" if conditions else ""
    return list(
        db.fetchall(
            render_internal_sql(
                "SELECT * FROM {table}{where} ORDER BY {order_by}",
                table=table,
                where=where,
                order_by=order_by,
            ),
            tuple(params),
        )
    )


def apply_definition_update(
    txn: Transaction,
    table: str,
    definition_id: str,
    values: dict[str, Any],
    *,
    what: str,
) -> Mapping[str, Any]:
    table = _validate_table(table)
    assignments = ", ".join(f"{column} = %s" for column in values)
    params = [*values.values(), definition_id]
    row = txn.execute(
        render_internal_sql(
            "UPDATE {table} SET {assignments} WHERE id = %s AND deleted_at IS NULL RETURNING *",
            table=table,
            assignments=assignments,
        ),
        tuple(params),
    ).fetchone()
    if row is None:
        raise DefinitionNotFoundError(f"{what} {definition_id} not found")
    return row


def soft_delete_definition(
    txn: Transaction,
    table: str,
    definition_id: str,
) -> bool:
    table = _validate_table(table)
    now = utc_now()
    cursor = txn.execute(
        render_internal_sql(
            "UPDATE {table} SET deleted_at = %s, updated_at = %s "
            "WHERE id = %s AND deleted_at IS NULL",
            table=table,
        ),
        (now, now, definition_id),
    )
    return cursor.rowcount > 0


def hard_delete_definition(txn: Transaction, table: str, definition_id: str) -> bool:
    table = _validate_table(table)
    cursor = txn.execute(
        render_internal_sql("DELETE FROM {table} WHERE id = %s", table=table),
        (definition_id,),
    )
    return cursor.rowcount > 0


def restore_definition(
    txn: Transaction,
    table: str,
    definition_id: str,
    *,
    what: str,
) -> Mapping[str, Any]:
    table = _validate_table(table)
    require_definition_id(definition_id, what=what)
    current = txn.execute(
        render_internal_sql(
            "SELECT * FROM {table} WHERE id = %s AND deleted_at IS NOT NULL",
            table=table,
        ),
        (definition_id,),
    ).fetchone()
    if current is None:
        raise DefinitionNotFoundError(f"{what} {definition_id} not found or not deleted")
    assert_live_name_free(
        txn,
        table,
        str(current["name"]),
        current["project_id"],
        exclude_id=definition_id,
        what=what,
    )
    now = utc_now()
    row = txn.execute(
        render_internal_sql(
            "UPDATE {table} SET deleted_at = NULL, updated_at = %s WHERE id = %s RETURNING *",
            table=table,
        ),
        (now, definition_id),
    ).fetchone()
    if row is None:
        raise DefinitionNotFoundError(f"{what} {definition_id} not found or not deleted")
    return row


def purge_deleted_definitions(
    txn: Transaction,
    table: str,
    db: HubDatabase,
    older_than_days: int,
) -> int:
    table = _validate_table(table)
    if older_than_days < 1:
        raise ValueError(f"older_than_days must be >= 1, got {older_than_days}")
    deleted_before_sql = older_than_now_expr(db, "deleted_at", "%s", "day")
    cursor = txn.execute(
        render_internal_sql(
            """
            DELETE FROM {table}
            WHERE deleted_at IS NOT NULL
              AND {deleted_before}
            """,
            table=table,
            deleted_before=deleted_before_sql,
        ),
        (older_than_days,),
    )
    return cursor.rowcount


def move_definition_scope(
    txn: Transaction,
    table: str,
    definition_id: str,
    *,
    source: DefinitionSource,
    project_id: str | None,
    what: str,
) -> Mapping[str, Any]:
    table = _validate_table(table)
    require_definition_id(definition_id, what=what)
    current = txn.execute(
        render_internal_sql(
            "SELECT * FROM {table} WHERE id = %s AND deleted_at IS NULL",
            table=table,
        ),
        (definition_id,),
    ).fetchone()
    if current is None:
        raise DefinitionNotFoundError(f"{what} {definition_id} not found")
    assert_live_name_free(
        txn,
        table,
        str(current["name"]),
        project_id,
        exclude_id=definition_id,
        what=what,
    )
    now = utc_now()
    row = txn.execute(
        render_internal_sql(
            "UPDATE {table} SET source = %s, project_id = %s, updated_at = %s "
            "WHERE id = %s RETURNING *",
            table=table,
        ),
        (source, project_id, now, definition_id),
    ).fetchone()
    if row is None:
        raise DefinitionNotFoundError(f"{what} {definition_id} not found")
    return row


def prepare_sync_values(
    current: Mapping[str, Any],
    fields: Mapping[str, Any],
    *,
    allowed: frozenset[str],
) -> dict[str, Any]:
    unknown = set(fields) - allowed
    if unknown:
        names = ", ".join(sorted(unknown))
        raise ValueError(f"Unknown definition field(s): {names}")
    values = dict(fields)
    if "enabled" in values and bool(current.get("enabled_pinned")):
        values.pop("enabled")
    values.pop("enabled_pinned", None)
    return values


__all__ = [
    "DefinitionNameConflictError",
    "DefinitionNotFoundError",
    "DefinitionSource",
    "apply_definition_update",
    "assert_live_name_free",
    "compute_definition_hash",
    "decode_json_list",
    "decode_json_object",
    "encode_json_list",
    "encode_json_scalar",
    "encode_json_value",
    "fetch_definition_by_name",
    "fetch_definition_row",
    "hard_delete_definition",
    "insert_definition_row",
    "list_definition_rows",
    "live_name_taken",
    "move_definition_scope",
    "new_definition_id",
    "prepare_sync_values",
    "purge_deleted_definitions",
    "require_definition_id",
    "restore_definition",
    "soft_delete_definition",
    "touch_revision",
]
