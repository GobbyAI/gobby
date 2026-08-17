"""Typed agent-step runtime instances with immutable definition snapshots."""

from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, Field, ValidationError

from gobby.storage.hub.protocol import AgentStepInstanceMutation, HubDatabase, Transaction
from gobby.storage.session_resolution import is_session_uuid
from gobby.utils.datetime import (
    parse_stored_datetime,
    require_stored_datetime,
    to_aware_utc,
    utc_now,
)
from gobby.workflows.agent_models import AgentDefinitionBody, AgentStepWorkflowBody

logger = logging.getLogger(__name__)


class StaleStepInstanceWriteError(Exception):
    """Raised when save is rejected as a stale identity or CAS write."""


class CorruptStepSnapshotError(ValueError):
    """Raised when a stored step snapshot is not a valid workflow body."""


class AgentStepInstance(BaseModel):
    """One agent-step execution bound to a session, with an immutable snapshot."""

    id: str
    session_id: str
    agent_name: str
    agent_step_workflow_id: str | None = None
    snapshot: AgentStepWorkflowBody
    enabled: bool = True
    current_step: str | None = None
    step_entered_at: datetime | None = None
    step_action_count: int = 0
    total_action_count: int = 0
    variables: dict[str, Any] = Field(default_factory=dict)
    context_injected: bool = False
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


def build_step_instance(
    agent_body: AgentDefinitionBody,
    *,
    session_id: str,
    step_workflow_id: str | None,
    variables: dict[str, Any] | None = None,
    current_step: str | None = None,
) -> AgentStepInstance:
    """Build a new instance from an agent body. Snapshot is a detached copy."""
    snapshot = agent_body.step_workflow
    if snapshot is None:
        raise ValueError(f"agent {agent_body.name!r} has no step_workflow")
    detached = AgentStepWorkflowBody.model_validate(snapshot.model_dump())
    now = utc_now()
    return AgentStepInstance(
        id=str(uuid4()),
        session_id=session_id,
        agent_name=agent_body.name,
        agent_step_workflow_id=step_workflow_id,
        snapshot=detached,
        current_step=detached.steps[0].name if current_step is None else current_step,
        variables=dict(detached.variables) if variables is None else dict(variables),
        created_at=now,
        updated_at=now,
    )


def _decode_variables(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str | bytes | bytearray) and value:
        try:
            loaded = json.loads(value)
        except (json.JSONDecodeError, TypeError, ValueError) as exc:
            logger.warning("Failed to decode step-instance variables: %s", exc)
            return {}
        if isinstance(loaded, dict):
            return loaded
    return {}


def _encode_json(value: Any) -> str:
    return json.dumps(value)


def _decode_snapshot(value: Any, *, session_id: str | None = None) -> AgentStepWorkflowBody:
    try:
        payload: Any = value
        if isinstance(value, str | bytes | bytearray):
            payload = json.loads(value)
        return AgentStepWorkflowBody.model_validate(payload)
    except (json.JSONDecodeError, ValidationError, TypeError, ValueError) as exc:
        if isinstance(exc, CorruptStepSnapshotError):
            raise
        context = f" session_id={session_id}" if session_id else ""
        raise CorruptStepSnapshotError(f"Malformed agent step snapshot{context}: {exc}") from exc


def _row_to_instance(row: Any) -> AgentStepInstance:
    session_id = str(row["session_id"])
    return AgentStepInstance(
        id=row["id"],
        session_id=row["session_id"],
        agent_name=row["agent_name"],
        agent_step_workflow_id=row["agent_step_workflow_id"],
        snapshot=_decode_snapshot(row["snapshot_json"], session_id=session_id),
        enabled=bool(row["enabled"]),
        current_step=row["current_step"],
        step_entered_at=parse_stored_datetime(row["step_entered_at"]),
        step_action_count=row["step_action_count"],
        total_action_count=row["total_action_count"],
        variables=_decode_variables(row["variables"]),
        context_injected=bool(row["context_injected"]),
        created_at=require_stored_datetime(row["created_at"], "created_at"),
        updated_at=require_stored_datetime(row["updated_at"], "updated_at"),
    )


def _stored_instance(row: Any) -> AgentStepInstance | None:
    """Decode a stored row, treating a corrupt snapshot as absent."""
    try:
        return _row_to_instance(row)
    except CorruptStepSnapshotError as exc:
        logger.warning("%s", exc)
        return None


def _cas_matches(
    existing: AgentStepInstance,
    if_match: tuple[str, datetime],
) -> bool:
    expected_id, expected_updated = if_match
    return str(existing.id) == str(expected_id) and to_aware_utc(
        existing.updated_at
    ) == to_aware_utc(expected_updated)


class AgentStepInstanceManager:
    """One agent-step instance per session, with immutable snapshot lineage."""

    def __init__(self, db: HubDatabase) -> None:
        self.db = db

    def get_for_session(self, session_id: str) -> AgentStepInstance | None:
        """Return the instance bound to ``session_id``, if any."""
        if not is_session_uuid(session_id):
            return None
        row = self.db.fetchone(
            "SELECT * FROM agent_step_instances WHERE session_id = %s",
            (session_id,),
        )
        if row is None:
            return None
        return _stored_instance(row)

    def save(
        self,
        instance: AgentStepInstance,
        *,
        if_match: tuple[str, datetime] | None = None,
    ) -> None:
        """Upsert mutable fields. Snapshot, lineage, created_at, and agent_name stay put."""
        if not is_session_uuid(instance.session_id):
            raise ValueError(f"invalid session_id: {instance.session_id}")
        lock = AgentStepInstanceMutation(session_id=instance.session_id)
        with self.db.transaction_immediate(lock) as txn:
            existing = self._fetch(txn, instance.session_id)
            if existing is None:
                if if_match is not None:
                    raise StaleStepInstanceWriteError(
                        f"stale save for session {instance.session_id}: instance is gone"
                    )
                persisted = self._insert(txn, instance)
            else:
                if existing.agent_name != instance.agent_name:
                    raise StaleStepInstanceWriteError(
                        f"stale identity write for session {instance.session_id}: "
                        f"stored agent {existing.agent_name!r} != {instance.agent_name!r}"
                    )
                if if_match is not None and not _cas_matches(existing, if_match):
                    raise StaleStepInstanceWriteError(
                        f"stale save for session {instance.session_id}: id or updated_at mismatch"
                    )
                persisted = self._update_mutable(txn, instance)
            instance.id = persisted.id
            instance.created_at = persisted.created_at
            instance.updated_at = persisted.updated_at
            instance.agent_step_workflow_id = persisted.agent_step_workflow_id
            instance.snapshot = persisted.snapshot
            instance.agent_name = persisted.agent_name

    def replace_for_session(self, instance: AgentStepInstance) -> None:
        """Delete + insert so snapshot, lineage, and agent identity change together."""
        if not is_session_uuid(instance.session_id):
            raise ValueError(f"invalid session_id: {instance.session_id}")
        lock = AgentStepInstanceMutation(session_id=instance.session_id)
        now = utc_now()
        with self.db.transaction_immediate(lock) as txn:
            txn.execute(
                "DELETE FROM agent_step_instances WHERE session_id = %s",
                (instance.session_id,),
            )
            instance.created_at = now
            instance.updated_at = now
            persisted = self._insert(txn, instance)
            instance.id = persisted.id
            instance.created_at = persisted.created_at
            instance.updated_at = persisted.updated_at

    def merge_variables(
        self,
        session_id: str,
        updates: dict[str, Any],
    ) -> AgentStepInstance | None:
        """Atomically merge variables without rewriting snapshot or step position."""
        if not updates or not is_session_uuid(session_id):
            return None
        lock = AgentStepInstanceMutation(session_id=session_id)
        now = utc_now()
        with self.db.transaction_immediate(lock) as txn:
            row = txn.execute(
                """
                UPDATE agent_step_instances
                SET variables = COALESCE(variables, '{}'::jsonb) || %s::jsonb,
                    updated_at = %s
                WHERE session_id = %s
                RETURNING *
                """,
                (_encode_json(updates), now, session_id),
            ).fetchone()
            if row is None:
                return None
            return _stored_instance(row)

    def delete_for_session(self, session_id: str) -> int:
        """Delete the instance for a session and return the deleted row count."""
        if not is_session_uuid(session_id):
            return 0
        lock = AgentStepInstanceMutation(session_id=session_id)
        with self.db.transaction_immediate(lock) as txn:
            cursor = txn.execute(
                "DELETE FROM agent_step_instances WHERE session_id = %s",
                (session_id,),
            )
            return cursor.rowcount

    def _fetch(self, txn: Transaction, session_id: str) -> AgentStepInstance | None:
        row = txn.execute(
            "SELECT * FROM agent_step_instances WHERE session_id = %s",
            (session_id,),
        ).fetchone()
        if row is None:
            return None
        return _row_to_instance(row)

    def _insert(self, txn: Transaction, instance: AgentStepInstance) -> AgentStepInstance:
        row = txn.execute(
            """
            INSERT INTO agent_step_instances (
                id, session_id, agent_step_workflow_id, agent_name, enabled,
                current_step, step_entered_at, step_action_count, total_action_count,
                variables, context_injected, snapshot_json, created_at, updated_at
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING *
            """,
            (
                instance.id,
                instance.session_id,
                instance.agent_step_workflow_id,
                instance.agent_name,
                instance.enabled,
                instance.current_step,
                instance.step_entered_at.isoformat() if instance.step_entered_at else None,
                instance.step_action_count,
                instance.total_action_count,
                _encode_json(instance.variables),
                instance.context_injected,
                _encode_json(instance.snapshot.model_dump()),
                instance.created_at.isoformat(),
                instance.updated_at.isoformat(),
            ),
        ).fetchone()
        if row is None:  # pragma: no cover - PostgreSQL RETURNING always yields a row.
            raise RuntimeError("agent_step_instances insert returned no row")
        return _row_to_instance(row)

    def _update_mutable(self, txn: Transaction, instance: AgentStepInstance) -> AgentStepInstance:
        now = utc_now()
        row = txn.execute(
            """
            UPDATE agent_step_instances
            SET enabled = %s,
                current_step = %s,
                step_entered_at = %s,
                step_action_count = %s,
                total_action_count = %s,
                variables = %s,
                context_injected = %s,
                updated_at = %s
            WHERE session_id = %s
            RETURNING *
            """,
            (
                instance.enabled,
                instance.current_step,
                instance.step_entered_at.isoformat() if instance.step_entered_at else None,
                instance.step_action_count,
                instance.total_action_count,
                _encode_json(instance.variables),
                instance.context_injected,
                now,
                instance.session_id,
            ),
        ).fetchone()
        if row is None:  # pragma: no cover - caller already observed the row.
            raise RuntimeError("agent_step_instances update returned no row")
        return _row_to_instance(row)
