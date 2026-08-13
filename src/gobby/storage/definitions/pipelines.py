"""Typed storage manager for pipeline_definitions."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from gobby.storage.definitions._shared import (
    DefinitionNotFoundError,
    DefinitionSource,
    apply_definition_update,
    assert_live_name_free,
    decode_json_list,
    decode_json_object,
    encode_json_list,
    encode_json_value,
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
from gobby.storage.hub.protocol import HubDatabase
from gobby.utils.datetime import normalize_datetime_model, utc_now

_TABLE = "pipeline_definitions"
_WHAT = "Pipeline definition"
_UPDATE_FIELDS = frozenset(
    {
        "name",
        "description",
        "enabled",
        "version",
        "definition_json",
        "canvas_json",
        "tags",
    }
)
_SYNC_FIELDS = _UPDATE_FIELDS


@normalize_datetime_model(required=("created_at", "updated_at"), optional=("deleted_at",))
@dataclass
class PipelineDefinitionRow:
    id: str
    name: str
    enabled: bool
    enabled_pinned: bool
    version: str
    definition_json: dict[str, Any]
    source: DefinitionSource
    created_at: datetime
    updated_at: datetime
    project_id: str | None = None
    description: str | None = None
    canvas_json: dict[str, Any] | None = None
    tags: list[str] | None = None
    deleted_at: datetime | None = None

    @classmethod
    def from_row(cls, row: Mapping[str, Any]) -> PipelineDefinitionRow:
        definition = decode_json_object(row["definition_json"]) or {}
        return cls(
            id=str(row["id"]),
            project_id=row["project_id"],
            name=row["name"],
            description=row["description"],
            enabled=bool(row["enabled"]),
            enabled_pinned=bool(row["enabled_pinned"]),
            version=row["version"] if row["version"] is not None else "1.0",
            definition_json=definition,
            canvas_json=decode_json_object(row["canvas_json"]),
            source=row["source"] or "installed",
            tags=decode_json_list(row["tags"]),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            deleted_at=row["deleted_at"] if "deleted_at" in row.keys() else None,
        )


class PipelineDefinitionManager:
    """Manages pipeline_definitions with commit-visible revision bumps."""

    def __init__(self, db: HubDatabase) -> None:
        self.db = db

    def create(
        self,
        name: str,
        definition_json: Mapping[str, Any] | str,
        *,
        project_id: str | None = None,
        description: str | None = None,
        enabled: bool = True,
        version: str = "1.0",
        canvas_json: Mapping[str, Any] | str | None = None,
        tags: list[str] | None = None,
        source: DefinitionSource = "installed",
    ) -> PipelineDefinitionRow:
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
                    "version",
                    "definition_json",
                    "canvas_json",
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
                    version,
                    encode_json_value(definition_json),
                    encode_json_value(canvas_json),
                    source,
                    encode_json_list(tags),
                    now,
                    now,
                ),
            )
            touch_revision(txn, "pipelines")
        return self.get(definition_id)

    def get(self, definition_id: str, include_deleted: bool = False) -> PipelineDefinitionRow:
        return PipelineDefinitionRow.from_row(
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
    ) -> PipelineDefinitionRow | None:
        row = fetch_definition_by_name(
            self.db,
            _TABLE,
            name,
            project_id,
            include_deleted=include_deleted,
        )
        return PipelineDefinitionRow.from_row(row) if row is not None else None

    def update(self, definition_id: str, **fields: Any) -> PipelineDefinitionRow:
        if "enabled" in fields:
            fields = {**fields, "enabled_pinned": True}
        return self._write_update(
            definition_id, fields, allowed=_UPDATE_FIELDS | {"enabled_pinned"}
        )

    def update_from_sync(self, definition_id: str, **fields: Any) -> PipelineDefinitionRow:
        current = self.get(definition_id)
        values = prepare_sync_values(current.__dict__, fields, allowed=_SYNC_FIELDS)
        if not values:
            return current
        return self._write_update(definition_id, values, allowed=_SYNC_FIELDS)

    def _write_update(
        self,
        definition_id: str,
        fields: Mapping[str, Any],
        *,
        allowed: frozenset[str],
    ) -> PipelineDefinitionRow:
        unknown = set(fields) - allowed
        if unknown:
            raise ValueError(f"Unknown definition field(s): {', '.join(sorted(unknown))}")
        values: dict[str, Any] = {}
        for key, value in fields.items():
            if key == "tags":
                values[key] = encode_json_list(value)
            elif key in {"definition_json", "canvas_json"}:
                values[key] = encode_json_value(value)
            else:
                values[key] = value
        if not values:
            return self.get(definition_id)
        values["updated_at"] = utc_now()
        with self.db.transaction() as txn:
            if "name" in values:
                current = self.get(definition_id)
                assert_live_name_free(
                    txn,
                    _TABLE,
                    str(values["name"]),
                    current.project_id,
                    exclude_id=definition_id,
                    what=_WHAT,
                )
            apply_definition_update(txn, _TABLE, definition_id, values, what=_WHAT)
            touch_revision(txn, "pipelines")
        return self.get(definition_id)

    def toggle_enabled(self, definition_id: str) -> PipelineDefinitionRow:
        now = utc_now()
        with self.db.transaction() as txn:
            row = txn.execute(
                """
                UPDATE pipeline_definitions
                SET enabled = NOT enabled, enabled_pinned = TRUE, updated_at = %s
                WHERE id = %s AND deleted_at IS NULL
                RETURNING *
                """,
                (now, definition_id),
            ).fetchone()
            if row is None:
                raise DefinitionNotFoundError(f"{_WHAT} {definition_id} not found")
            touch_revision(txn, "pipelines")
        return PipelineDefinitionRow.from_row(row)

    def delete(self, definition_id: str) -> bool:
        with self.db.transaction() as txn:
            deleted = soft_delete_definition(txn, _TABLE, definition_id)
            if deleted:
                touch_revision(txn, "pipelines")
        return deleted

    def hard_delete(self, definition_id: str) -> bool:
        with self.db.transaction() as txn:
            deleted = hard_delete_definition(txn, _TABLE, definition_id)
            if deleted:
                touch_revision(txn, "pipelines")
        return deleted

    def restore(self, definition_id: str) -> PipelineDefinitionRow:
        with self.db.transaction() as txn:
            row = restore_definition(txn, _TABLE, definition_id, what=_WHAT)
            touch_revision(txn, "pipelines")
        return PipelineDefinitionRow.from_row(row)

    def purge_deleted(self, older_than_days: int = 30) -> int:
        with self.db.transaction() as txn:
            count = purge_deleted_definitions(txn, _TABLE, self.db, older_than_days)
            if count:
                touch_revision(txn, "pipelines")
        return count

    def list_all(
        self,
        project_id: str | None = None,
        enabled: bool | None = None,
        include_deleted: bool = False,
    ) -> list[PipelineDefinitionRow]:
        return [
            PipelineDefinitionRow.from_row(row)
            for row in list_definition_rows(
                self.db,
                _TABLE,
                project_id=project_id,
                enabled=enabled,
                include_deleted=include_deleted,
            )
        ]

    def move_to_project(self, definition_id: str, project_id: str) -> PipelineDefinitionRow:
        with self.db.transaction() as txn:
            row = move_definition_scope(
                txn,
                _TABLE,
                definition_id,
                source="project",
                project_id=project_id,
                what=_WHAT,
            )
            touch_revision(txn, "pipelines")
        return PipelineDefinitionRow.from_row(row)

    def move_to_global(self, definition_id: str) -> PipelineDefinitionRow:
        with self.db.transaction() as txn:
            row = move_definition_scope(
                txn,
                _TABLE,
                definition_id,
                source="installed",
                project_id=None,
                what=_WHAT,
            )
            touch_revision(txn, "pipelines")
        return PipelineDefinitionRow.from_row(row)

    def duplicate(self, definition_id: str, new_name: str) -> PipelineDefinitionRow:
        original = self.get(definition_id)
        return self.create(
            name=new_name,
            definition_json=original.definition_json,
            project_id=original.project_id,
            description=original.description,
            enabled=original.enabled,
            version=original.version,
            canvas_json=original.canvas_json,
            tags=original.tags,
            source="custom",
        )
