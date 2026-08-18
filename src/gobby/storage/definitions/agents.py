"""Typed storage manager for agent_definitions and agent_step_workflows."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from gobby.storage.definitions._shared import (
    DefinitionNameConflictError,
    DefinitionNotFoundError,
    DefinitionSource,
    apply_definition_update,
    assert_live_name_free,
    decode_json_list,
    decode_json_object,
    encode_json_list,
    encode_json_value,
    hard_delete_definition,
    insert_definition_row,
    move_definition_scope,
    new_definition_id,
    prepare_sync_values,
    purge_deleted_definitions,
    require_definition_id,
    restore_definition,
    soft_delete_definition,
    touch_revision,
)
from gobby.storage.hub.protocol import HubDatabase, Transaction
from gobby.storage.sql_dialect import older_than_now_expr
from gobby.utils.datetime import normalize_datetime_model, utc_now
from gobby.utils.json_helpers import json_dumps

_TABLE = "agent_definitions"
_WHAT = "Agent definition"
_STEP_BODY_KEYS = ("steps", "step_variables", "exit_condition", "step_workflow")
_UPDATE_FIELDS = frozenset({"name", "description", "enabled", "definition_json", "tags"})
_SYNC_FIELDS = _UPDATE_FIELDS
_SELECT_HYDRATED = """
SELECT
    a.id, a.project_id, a.name, a.description, a.enabled, a.enabled_pinned,
    a.definition_json, a.source, a.tags, a.deleted_at, a.created_at, a.updated_at,
    w.id AS step_workflow_id,
    w.steps_json AS child_steps_json,
    w.variables_json AS child_variables_json,
    w.exit_condition AS child_exit_condition
FROM agent_definitions a
LEFT JOIN agent_step_workflows w ON w.agent_definition_id = a.id
"""


def _lock_live_row(txn: Transaction, definition_id: str) -> AgentDefinitionRow:
    row = txn.execute(
        _SELECT_HYDRATED + " WHERE a.id = %s AND a.deleted_at IS NULL FOR UPDATE OF a",
        (definition_id,),
    ).fetchone()
    if row is None:
        raise DefinitionNotFoundError(f"{_WHAT} {definition_id} not found")
    return AgentDefinitionRow.from_row(row)


def _decode_json_array(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return list(value)
    if isinstance(value, str):
        parsed = json.loads(value)
        if isinstance(parsed, list):
            return parsed
    return []


def parent_body(body_json: Mapping[str, Any] | str) -> dict[str, Any]:
    if isinstance(body_json, str):
        parsed = json.loads(body_json)
        if not isinstance(parsed, dict):
            raise ValueError("Agent definition body must be a JSON object")
        body = parsed
    else:
        body = dict(body_json)
    for key in _STEP_BODY_KEYS:
        body.pop(key, None)
    return body


_parent_body = parent_body


def _child_columns(
    step_workflow: Mapping[str, Any],
) -> tuple[list[Any], dict[str, Any], str | None]:
    steps = step_workflow.get("steps", [])
    if not isinstance(steps, list):
        raise ValueError("step_workflow.steps must be a list")
    variables = step_workflow.get("variables") or {}
    if not isinstance(variables, dict):
        raise ValueError("step_workflow.variables must be an object")
    exit_condition = step_workflow.get("exit_condition")
    if exit_condition is not None:
        exit_condition = str(exit_condition)
    return steps, dict(variables), exit_condition


def _find_live(txn: Transaction, name: str, project_id: str | None) -> Mapping[str, Any] | None:
    if project_id is None:
        return txn.execute(
            "SELECT id, enabled_pinned FROM agent_definitions "
            "WHERE name = %s AND project_id IS NULL AND deleted_at IS NULL FOR UPDATE",
            (name,),
        ).fetchone()
    return txn.execute(
        "SELECT id, enabled_pinned FROM agent_definitions "
        "WHERE name = %s AND project_id = %s AND deleted_at IS NULL FOR UPDATE",
        (name, project_id),
    ).fetchone()


def _write_child(
    txn: Transaction,
    agent_definition_id: str,
    step_workflow: Mapping[str, Any] | None,
) -> bool:
    existing = txn.execute(
        "SELECT id FROM agent_step_workflows WHERE agent_definition_id = %s",
        (agent_definition_id,),
    ).fetchone()
    if step_workflow is None:
        if existing is None:
            return False
        txn.execute(
            "DELETE FROM agent_step_workflows WHERE agent_definition_id = %s",
            (agent_definition_id,),
        )
        return True
    now = utc_now()
    steps, variables, exit_condition = _child_columns(step_workflow)
    if existing is None:
        txn.execute(
            """
            INSERT INTO agent_step_workflows (
                id, agent_definition_id, steps_json, variables_json,
                exit_condition
            ) VALUES (%s, %s, %s, %s, %s)
            """,
            (
                new_definition_id(),
                agent_definition_id,
                encode_json_value(steps),
                encode_json_value(variables),
                exit_condition,
            ),
        )
        return True
    txn.execute(
        """
        UPDATE agent_step_workflows
        SET steps_json = %s, variables_json = %s, exit_condition = %s, updated_at = %s
        WHERE agent_definition_id = %s
        """,
        (
            encode_json_value(steps),
            encode_json_value(variables),
            exit_condition,
            now,
            agent_definition_id,
        ),
    )
    return True


@normalize_datetime_model(required=("created_at", "updated_at"), optional=("deleted_at",))
@dataclass
class AgentDefinitionRow:
    id: str
    name: str
    enabled: bool
    enabled_pinned: bool
    definition_json: dict[str, Any]
    source: DefinitionSource
    created_at: datetime
    updated_at: datetime
    project_id: str | None = None
    description: str | None = None
    tags: list[str] | None = None
    step_workflow_id: str | None = None
    deleted_at: datetime | None = None

    @classmethod
    def from_row(cls, row: Mapping[str, Any]) -> AgentDefinitionRow:
        definition = decode_json_object(row["definition_json"]) or {}
        for key in _STEP_BODY_KEYS:
            definition.pop(key, None)
        raw_child_id = row["step_workflow_id"] if "step_workflow_id" in row.keys() else None
        step_workflow_id = str(raw_child_id) if raw_child_id is not None else None
        if step_workflow_id is not None:
            definition["step_workflow"] = {
                "variables": decode_json_object(row["child_variables_json"]) or {},
                "exit_condition": row["child_exit_condition"],
                "steps": _decode_json_array(row["child_steps_json"]),
            }
        return cls(
            id=str(row["id"]),
            project_id=row["project_id"],
            name=row["name"],
            description=row["description"],
            enabled=bool(row["enabled"]),
            enabled_pinned=bool(row["enabled_pinned"]),
            definition_json=definition,
            source=row["source"] or "installed",
            tags=decode_json_list(row["tags"]),
            step_workflow_id=step_workflow_id,
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            deleted_at=row["deleted_at"] if "deleted_at" in row.keys() else None,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "project_id": self.project_id,
            "name": self.name,
            "description": self.description,
            "enabled": self.enabled,
            "enabled_pinned": self.enabled_pinned,
            "definition_json": json_dumps(self.definition_json),
            "source": self.source,
            "tags": self.tags,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "deleted_at": self.deleted_at,
            "step_workflow_id": self.step_workflow_id,
        }


@normalize_datetime_model(required=("created_at", "updated_at"))
@dataclass
class AgentStepWorkflowRow:
    id: str
    agent_definition_id: str
    steps_json: list[Any]
    variables_json: dict[str, Any]
    created_at: datetime
    updated_at: datetime
    exit_condition: str | None = None

    @classmethod
    def from_row(cls, row: Mapping[str, Any]) -> AgentStepWorkflowRow:
        return cls(
            id=str(row["id"]),
            agent_definition_id=str(row["agent_definition_id"]),
            steps_json=_decode_json_array(row["steps_json"]),
            variables_json=decode_json_object(row["variables_json"]) or {},
            exit_condition=row["exit_condition"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )


class AgentDefinitionManager:
    """Manages agent_definitions plus the optional step-workflow child."""

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
        tags: list[str] | None = None,
        source: DefinitionSource = "installed",
    ) -> AgentDefinitionRow:
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
                    "definition_json",
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
                    encode_json_value(_parent_body(definition_json)),
                    source,
                    encode_json_list(tags),
                    now,
                    now,
                ),
            )
            touch_revision(txn, "agents")
        return self.get(definition_id)

    def get(self, definition_id: str, include_deleted: bool = False) -> AgentDefinitionRow:
        require_definition_id(definition_id, what=_WHAT)
        query = _SELECT_HYDRATED + " WHERE a.id = %s"
        params: list[Any] = [definition_id]
        if not include_deleted:
            query += " AND a.deleted_at IS NULL"
        row = self.db.fetchone(query, tuple(params))
        if row is None:
            raise DefinitionNotFoundError(f"{_WHAT} {definition_id} not found")
        return AgentDefinitionRow.from_row(row)

    def get_by_name(
        self,
        name: str,
        project_id: str | None = None,
        include_deleted: bool = False,
    ) -> AgentDefinitionRow | None:
        deleted = "" if include_deleted else " AND a.deleted_at IS NULL"
        order = " ORDER BY a.deleted_at NULLS FIRST" if include_deleted else ""
        if project_id is not None:
            row = self.db.fetchone(
                f"{_SELECT_HYDRATED} WHERE a.name = %s AND a.project_id = %s{deleted}{order}",
                (name, project_id),
            )
            if row is not None:
                return AgentDefinitionRow.from_row(row)
        row = self.db.fetchone(
            f"{_SELECT_HYDRATED} WHERE a.name = %s AND a.project_id IS NULL{deleted}{order}",
            (name,),
        )
        return AgentDefinitionRow.from_row(row) if row is not None else None

    def update(self, definition_id: str, **fields: Any) -> AgentDefinitionRow:
        if "enabled" in fields:
            fields = {**fields, "enabled_pinned": True}
        return self._write_update(
            definition_id, fields, allowed=_UPDATE_FIELDS | {"enabled_pinned"}
        )

    def update_from_sync(self, definition_id: str, **fields: Any) -> AgentDefinitionRow:
        return self._write_update(definition_id, fields, allowed=_SYNC_FIELDS, from_sync=True)

    def _write_update(
        self,
        definition_id: str,
        fields: Mapping[str, Any],
        *,
        allowed: frozenset[str],
        from_sync: bool = False,
    ) -> AgentDefinitionRow:
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
                elif key == "definition_json":
                    values[key] = encode_json_value(_parent_body(value))
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
            touch_revision(txn, "agents")
        return self.get(definition_id)

    def toggle_enabled(self, definition_id: str) -> AgentDefinitionRow:
        now = utc_now()
        with self.db.transaction() as txn:
            row = txn.execute(
                """
                UPDATE agent_definitions
                SET enabled = NOT enabled, enabled_pinned = TRUE, updated_at = %s
                WHERE id = %s AND deleted_at IS NULL
                RETURNING id
                """,
                (now, definition_id),
            ).fetchone()
            if row is None:
                raise DefinitionNotFoundError(f"{_WHAT} {definition_id} not found")
            touch_revision(txn, "agents")
        return self.get(definition_id)

    def delete(self, definition_id: str) -> bool:
        with self.db.transaction() as txn:
            deleted = soft_delete_definition(txn, _TABLE, definition_id)
            if deleted:
                touch_revision(txn, "agents")
        return deleted

    def hard_delete(self, definition_id: str) -> bool:
        with self.db.transaction() as txn:
            child = txn.execute(
                "SELECT 1 FROM agent_step_workflows WHERE agent_definition_id = %s",
                (definition_id,),
            ).fetchone()
            deleted = hard_delete_definition(txn, _TABLE, definition_id)
            if deleted:
                touch_revision(txn, "agents")
                if child is not None:
                    touch_revision(txn, "agent_step_workflows")
        return deleted

    def restore(self, definition_id: str) -> AgentDefinitionRow:
        with self.db.transaction() as txn:
            restore_definition(txn, _TABLE, definition_id, what=_WHAT)
            touch_revision(txn, "agents")
        return self.get(definition_id)

    def purge_deleted(self, older_than_days: int = 30) -> int:
        with self.db.transaction() as txn:
            deleted_before_sql = older_than_now_expr(self.db, "a.deleted_at", "%s", "day")
            child = txn.execute(
                f"""
                SELECT 1
                FROM agent_step_workflows w
                JOIN agent_definitions a ON a.id = w.agent_definition_id
                WHERE a.deleted_at IS NOT NULL
                  AND {deleted_before_sql}
                LIMIT 1
                """,
                (older_than_days,),
            ).fetchone()
            count = purge_deleted_definitions(txn, _TABLE, self.db, older_than_days)
            if count:
                touch_revision(txn, "agents")
                if child is not None:
                    touch_revision(txn, "agent_step_workflows")
        return count

    def list_all(
        self,
        project_id: str | None = None,
        enabled: bool | None = None,
        include_deleted: bool = False,
    ) -> list[AgentDefinitionRow]:
        conditions: list[str] = []
        params: list[Any] = []
        if not include_deleted:
            conditions.append("a.deleted_at IS NULL")
        if project_id is not None:
            conditions.append("(a.project_id = %s OR a.project_id IS NULL)")
            params.append(project_id)
        if enabled is not None:
            conditions.append("a.enabled = %s")
            params.append(enabled)
        where = f" WHERE {' AND '.join(conditions)}" if conditions else ""
        return [
            AgentDefinitionRow.from_row(row)
            for row in self.db.fetchall(
                f"{_SELECT_HYDRATED}{where} ORDER BY a.name",
                tuple(params),
            )
        ]

    def move_to_project(self, definition_id: str, project_id: str) -> AgentDefinitionRow:
        with self.db.transaction() as txn:
            move_definition_scope(
                txn,
                _TABLE,
                definition_id,
                source="project",
                project_id=project_id,
                what=_WHAT,
            )
            touch_revision(txn, "agents")
        return self.get(definition_id)

    def move_to_global(self, definition_id: str) -> AgentDefinitionRow:
        with self.db.transaction() as txn:
            move_definition_scope(
                txn,
                _TABLE,
                definition_id,
                source="installed",
                project_id=None,
                what=_WHAT,
            )
            touch_revision(txn, "agents")
        return self.get(definition_id)

    def duplicate(self, definition_id: str, new_name: str) -> AgentDefinitionRow:
        original = self.get(definition_id)
        body = dict(original.definition_json)
        step_workflow = body.pop("step_workflow", None)
        return self.upsert_with_steps(
            new_name,
            body,
            step_workflow,
            project_id=original.project_id,
            description=original.description,
            enabled=original.enabled,
            tags=original.tags,
            source="custom",
        )

    def upsert_with_steps(
        self,
        name: str,
        body_json: Mapping[str, Any] | str,
        step_workflow: Mapping[str, Any] | None,
        *,
        source: DefinitionSource = "installed",
        project_id: str | None = None,
        enabled: bool = True,
        tags: list[str] | None = None,
        description: str | None = None,
        create_only: bool = False,
    ) -> AgentDefinitionRow:
        parent_body = _parent_body(body_json)
        now = utc_now()
        with self.db.transaction() as txn:
            existing = _find_live(txn, name, project_id)
            if existing is not None and create_only:
                scope = "global" if project_id is None else f"project {project_id}"
                raise DefinitionNameConflictError(f"{_WHAT} {name!r} already exists in {scope}")
            if existing is None:
                definition_id = new_definition_id()
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
                        "definition_json",
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
                        encode_json_value(parent_body),
                        source,
                        encode_json_list(tags),
                        now,
                        now,
                    ),
                )
            else:
                definition_id = str(existing["id"])
                apply_definition_update(
                    txn,
                    _TABLE,
                    definition_id,
                    {
                        "description": description,
                        "enabled": bool(enabled),
                        "definition_json": encode_json_value(parent_body),
                        "source": source,
                        "tags": encode_json_list(tags),
                        "updated_at": now,
                    },
                    what=_WHAT,
                )
            child_written = _write_child(txn, definition_id, step_workflow)
            touch_revision(txn, "agents")
            if child_written:
                touch_revision(txn, "agent_step_workflows")
        return self.get(definition_id)

    def upsert_from_sync(
        self,
        name: str,
        body_json: Mapping[str, Any] | str,
        step_workflow: Mapping[str, Any] | None,
        *,
        source: DefinitionSource = "installed",
        project_id: str | None = None,
        enabled: bool = True,
        tags: list[str] | None = None,
        description: str | None = None,
        restore: bool = False,
    ) -> AgentDefinitionRow:
        parent_body = _parent_body(body_json)
        now = utc_now()
        with self.db.transaction() as txn:
            existing = _find_live(txn, name, project_id)
            if existing is None and restore:
                if project_id is None:
                    existing = txn.execute(
                        "SELECT id, enabled_pinned FROM agent_definitions "
                        "WHERE name = %s AND project_id IS NULL AND deleted_at IS NOT NULL "
                        "ORDER BY deleted_at DESC LIMIT 1",
                        (name,),
                    ).fetchone()
                else:
                    existing = txn.execute(
                        "SELECT id, enabled_pinned FROM agent_definitions "
                        "WHERE name = %s AND project_id = %s AND deleted_at IS NOT NULL "
                        "ORDER BY deleted_at DESC LIMIT 1",
                        (name, project_id),
                    ).fetchone()
                if existing is not None:
                    restore_definition(txn, _TABLE, str(existing["id"]), what=_WHAT)
            if existing is None:
                definition_id = new_definition_id()
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
                        "definition_json",
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
                        encode_json_value(parent_body),
                        source,
                        encode_json_list(tags),
                        now,
                        now,
                    ),
                )
            else:
                definition_id = str(existing["id"])
                values = prepare_sync_values(
                    {"enabled_pinned": bool(existing["enabled_pinned"])},
                    {
                        "description": description,
                        "enabled": bool(enabled),
                        "definition_json": encode_json_value(parent_body),
                        "tags": encode_json_list(tags),
                    },
                    allowed=_SYNC_FIELDS,
                )
                if values:
                    values["updated_at"] = now
                    apply_definition_update(txn, _TABLE, definition_id, values, what=_WHAT)
            child_written = _write_child(txn, definition_id, step_workflow)
            touch_revision(txn, "agents")
            if child_written:
                touch_revision(txn, "agent_step_workflows")
        return self.get(definition_id)

    def set_step_workflow(
        self,
        agent_definition_id: str,
        step_workflow: Mapping[str, Any] | None,
    ) -> AgentDefinitionRow:
        require_definition_id(agent_definition_id, what=_WHAT)
        with self.db.transaction() as txn:
            parent = txn.execute(
                "SELECT id FROM agent_definitions WHERE id = %s AND deleted_at IS NULL",
                (agent_definition_id,),
            ).fetchone()
            if parent is None:
                raise DefinitionNotFoundError(f"{_WHAT} {agent_definition_id} not found")
            child_written = _write_child(txn, agent_definition_id, step_workflow)
            if child_written:
                touch_revision(txn, "agents")
                touch_revision(txn, "agent_step_workflows")
        return self.get(agent_definition_id)

    def get_step_workflow(self, agent_definition_id: str) -> AgentStepWorkflowRow | None:
        require_definition_id(agent_definition_id, what=_WHAT)
        row = self.db.fetchone(
            "SELECT * FROM agent_step_workflows WHERE agent_definition_id = %s",
            (agent_definition_id,),
        )
        return AgentStepWorkflowRow.from_row(row) if row is not None else None
