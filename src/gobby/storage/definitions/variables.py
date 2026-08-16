"""Typed storage manager for session_variable_defaults."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from psycopg.types.json import Jsonb

from gobby.storage.definitions._shared import (
    DefinitionNotFoundError,
    DefinitionSource,
    apply_definition_update,
    assert_live_name_free,
    decode_json_list,
    encode_json_list,
    fetch_definition_by_name,
    fetch_definition_row,
    hard_delete_definition,
    insert_definition_row,
    list_definition_rows,
    move_definition_scope,
    new_definition_id,
    prepare_sync_values,
    purge_deleted_definitions,
    restore_definition,
    soft_delete_definition,
    touch_revision,
)
from gobby.storage.hub.protocol import HubDatabase, Transaction
from gobby.utils.datetime import normalize_datetime_model, utc_now

_TABLE = "session_variable_defaults"
_WHAT = "Session variable default"
_UPDATE_FIELDS = frozenset({"name", "description", "enabled", "default_value", "tags"})
_SYNC_FIELDS = _UPDATE_FIELDS


def _lock_live_row(txn: Transaction, definition_id: str) -> SessionVariableDefaultRow:
    row = txn.execute(
        """
        SELECT * FROM session_variable_defaults
        WHERE id = %s AND deleted_at IS NULL
        FOR UPDATE
        """,
        (definition_id,),
    ).fetchone()
    if row is None:
        raise DefinitionNotFoundError(f"{_WHAT} {definition_id} not found")
    return SessionVariableDefaultRow.from_row(row)


def _decode_default_value(value: Any) -> Any:
    """Undo hub row normalization that dumps JSON objects/arrays to text."""
    if not isinstance(value, str) or not value or value[0] not in "[{":
        return value
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return value


@normalize_datetime_model(required=("created_at", "updated_at"), optional=("deleted_at",))
@dataclass
class SessionVariableDefaultRow:
    id: str
    name: str
    enabled: bool
    enabled_pinned: bool
    default_value: Any
    source: DefinitionSource
    created_at: datetime
    updated_at: datetime
    project_id: str | None = None
    description: str | None = None
    tags: list[str] | None = None
    deleted_at: datetime | None = None

    @classmethod
    def from_row(cls, row: Mapping[str, Any]) -> SessionVariableDefaultRow:
        return cls(
            id=str(row["id"]),
            project_id=row["project_id"],
            name=row["name"],
            description=row["description"],
            enabled=bool(row["enabled"]),
            enabled_pinned=bool(row["enabled_pinned"]),
            default_value=_decode_default_value(row["default_value"]),
            source=row["source"] or "installed",
            tags=decode_json_list(row["tags"]),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            deleted_at=row["deleted_at"] if "deleted_at" in row.keys() else None,
        )


class SessionVariableDefaultManager:
    """Manages session_variable_defaults with commit-visible revision bumps."""

    def __init__(self, db: HubDatabase) -> None:
        self.db = db

    def create(
        self,
        name: str,
        default_value: Any,
        *,
        project_id: str | None = None,
        description: str | None = None,
        enabled: bool = True,
        tags: list[str] | None = None,
        source: DefinitionSource = "installed",
    ) -> SessionVariableDefaultRow:
        definition_id = new_definition_id()
        now = utc_now()
        with self.db.transaction() as txn:
            assert_live_name_free(txn, _TABLE, name, project_id, what=_WHAT)
            insert_definition_row(
                txn,
                _TABLE,
                (
                    "id",
                    "project_id",
                    "name",
                    "description",
                    "enabled",
                    "enabled_pinned",
                    "default_value",
                    "source",
                    "tags",
                    "created_at",
                    "updated_at",
                ),
                (
                    definition_id,
                    project_id,
                    name,
                    description,
                    bool(enabled),
                    False,
                    None if default_value is None else Jsonb(default_value),
                    source,
                    encode_json_list(tags),
                    now,
                    now,
                ),
            )
            touch_revision(txn, "variables")
        return self.get(definition_id)

    def get(self, definition_id: str, include_deleted: bool = False) -> SessionVariableDefaultRow:
        return SessionVariableDefaultRow.from_row(
            fetch_definition_row(
                self.db,
                _TABLE,
                definition_id,
                include_deleted=include_deleted,
                what=_WHAT,
            )
        )

    def get_by_name(
        self,
        name: str,
        project_id: str | None = None,
        include_deleted: bool = False,
    ) -> SessionVariableDefaultRow | None:
        row = fetch_definition_by_name(
            self.db,
            _TABLE,
            name,
            project_id,
            include_deleted=include_deleted,
        )
        return SessionVariableDefaultRow.from_row(row) if row is not None else None

    def update(self, definition_id: str, **fields: Any) -> SessionVariableDefaultRow:
        if "enabled" in fields:
            fields = {**fields, "enabled_pinned": True}
        return self._write_update(
            definition_id, fields, allowed=_UPDATE_FIELDS | {"enabled_pinned"}
        )

    def update_from_sync(self, definition_id: str, **fields: Any) -> SessionVariableDefaultRow:
        return self._write_update(definition_id, fields, allowed=_SYNC_FIELDS, from_sync=True)

    def _write_update(
        self,
        definition_id: str,
        fields: Mapping[str, Any],
        *,
        allowed: frozenset[str],
        from_sync: bool = False,
    ) -> SessionVariableDefaultRow:
        unknown = set(fields) - allowed
        if unknown:
            raise ValueError(f"Unknown definition field(s): {', '.join(sorted(unknown))}")
        with self.db.transaction() as txn:
            current = _lock_live_row(txn, definition_id)
            incoming: Mapping[str, Any] = fields
            if from_sync:
                incoming = prepare_sync_values(current.__dict__, fields, allowed=allowed)
                if not incoming:
                    return current
            values: dict[str, Any] = {}
            for key, value in incoming.items():
                if key == "tags":
                    values[key] = encode_json_list(value)
                elif key == "default_value":
                    values[key] = None if value is None else Jsonb(value)
                else:
                    values[key] = value
            if not values:
                return current
            values["updated_at"] = utc_now()
            if "name" in values:
                assert_live_name_free(
                    txn,
                    _TABLE,
                    str(values["name"]),
                    current.project_id,
                    exclude_id=definition_id,
                    what=_WHAT,
                )
            apply_definition_update(txn, _TABLE, definition_id, values, what=_WHAT)
            touch_revision(txn, "variables")
        return self.get(definition_id)

    def toggle_enabled(self, definition_id: str) -> SessionVariableDefaultRow:
        now = utc_now()
        with self.db.transaction() as txn:
            row = txn.execute(
                """
                UPDATE session_variable_defaults
                SET enabled = NOT enabled, enabled_pinned = TRUE, updated_at = %s
                WHERE id = %s AND deleted_at IS NULL
                RETURNING *
                """,
                (now, definition_id),
            ).fetchone()
            if row is None:
                raise DefinitionNotFoundError(f"{_WHAT} {definition_id} not found")
            touch_revision(txn, "variables")
        return SessionVariableDefaultRow.from_row(row)

    def delete(self, definition_id: str) -> bool:
        with self.db.transaction() as txn:
            deleted = soft_delete_definition(txn, _TABLE, definition_id)
            if deleted:
                touch_revision(txn, "variables")
        return deleted

    def hard_delete(self, definition_id: str) -> bool:
        with self.db.transaction() as txn:
            deleted = hard_delete_definition(txn, _TABLE, definition_id)
            if deleted:
                touch_revision(txn, "variables")
        return deleted

    def restore(self, definition_id: str) -> SessionVariableDefaultRow:
        with self.db.transaction() as txn:
            row = restore_definition(txn, _TABLE, definition_id, what=_WHAT)
            touch_revision(txn, "variables")
        return SessionVariableDefaultRow.from_row(row)

    def purge_deleted(self, older_than_days: int = 30) -> int:
        with self.db.transaction() as txn:
            count = purge_deleted_definitions(txn, _TABLE, self.db, older_than_days)
            if count:
                touch_revision(txn, "variables")
        return count

    def list_all(
        self,
        project_id: str | None = None,
        enabled: bool | None = None,
        include_deleted: bool = False,
    ) -> list[SessionVariableDefaultRow]:
        return [
            SessionVariableDefaultRow.from_row(row)
            for row in list_definition_rows(
                self.db,
                _TABLE,
                project_id=project_id,
                enabled=enabled,
                include_deleted=include_deleted,
            )
        ]

    def get_defaults_map(
        self,
        project_id: str | None = None,
        enabled_only: bool = True,
    ) -> dict[str, Any]:
        enabled = True if enabled_only else None
        rows = self.list_all(project_id=project_id, enabled=enabled)
        if project_id is None:
            rows = [row for row in rows if row.project_id is None]
        defaults: dict[str, Any] = {}
        for row in sorted(rows, key=lambda item: 0 if item.project_id is None else 1):
            defaults[row.name] = row.default_value
        return defaults

    def move_to_project(self, definition_id: str, project_id: str) -> SessionVariableDefaultRow:
        with self.db.transaction() as txn:
            row = move_definition_scope(
                txn,
                _TABLE,
                definition_id,
                source="project",
                project_id=project_id,
                what=_WHAT,
            )
            touch_revision(txn, "variables")
        return SessionVariableDefaultRow.from_row(row)

    def move_to_global(self, definition_id: str) -> SessionVariableDefaultRow:
        with self.db.transaction() as txn:
            row = move_definition_scope(
                txn,
                _TABLE,
                definition_id,
                source="installed",
                project_id=None,
                what=_WHAT,
            )
            touch_revision(txn, "variables")
        return SessionVariableDefaultRow.from_row(row)
