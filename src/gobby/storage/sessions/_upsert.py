"""Session registration update helpers."""

from __future__ import annotations

import json
from collections.abc import Sequence
from datetime import datetime
from typing import Protocol

from gobby.storage.session_models import Session
from gobby.storage.workspace_machine_scope import MachineOwnershipMismatchError
from gobby.terminal_context import (
    CODEX_CLEAR_ORIGIN_KEY,
    CODEX_CLEAR_ORIGIN_PENDING,
    parse_terminal_context_value,
)

from ._update_sentinel import UNSET, UnsetType, is_set


class _SessionGetter(Protocol):
    def get(self, session_id: str) -> Session | None: ...


class _TransactionConnection(Protocol):
    def execute(self, sql: str, params: Sequence[object] = ()) -> object: ...


_SESSION_UNIQUE_CONSTRAINT = "idx_sessions_unique"


def is_session_unique_conflict(exc: BaseException) -> bool:
    messages = [str(arg) for arg in exc.args if isinstance(arg, str)]
    messages.append(str(exc))
    for message in messages:
        if _SESSION_UNIQUE_CONSTRAINT in message:
            return True
    return False


def update_existing_session(
    manager: _SessionGetter,
    conn: _TransactionConnection,
    existing: Session,
    *,
    machine_id: str,
    transcript_path: str | None | UnsetType,
    git_branch: str | None | UnsetType,
    parent_session_id: str | None | UnsetType,
    terminal_context_json: str | None,
    workflow_name: str | None,
    is_local: bool | None,
    sandbox_enabled: bool | None,
    sandbox_policy_hash: str | None,
    now: datetime,
    workspace_path: str | None | UnsetType = UNSET,
) -> Session:
    if existing.machine_id != machine_id:
        raise MachineOwnershipMismatchError(
            resource_kind="session",
            resource_id=existing.id,
            owner_machine_id=existing.machine_id,
            current_machine_id=machine_id,
        )
    if existing.session_type == "terminal" and existing.status == "expired":
        return existing

    incoming_terminal_context = parse_terminal_context_value(terminal_context_json)
    clear_origin = (incoming_terminal_context or {}).get(CODEX_CLEAR_ORIGIN_KEY)
    terminal_context_update_json = (
        json.dumps(
            {key: value for key, value in incoming_terminal_context.items() if value is not None}
        )
        if incoming_terminal_context is not None
        else None
    )

    conn.execute(
        """
        UPDATE sessions SET
            machine_id = %s,
            transcript_path = CASE WHEN %s THEN %s ELSE transcript_path END,
            git_branch = CASE WHEN %s THEN %s ELSE git_branch END,
            parent_session_id = CASE WHEN %s THEN %s ELSE parent_session_id END,
            terminal_context = CASE
                WHEN %s::jsonb IS NULL THEN terminal_context
                -- A clear-start proposes its origin with the seat it found. It applies
                -- only over no origin, or a pending one on a row with no recorded
                -- process; against a settled origin it writes nothing at all.
                WHEN %s::text IS NOT NULL
                    AND terminal_context ->> %s::text IS NOT NULL
                    AND NOT (
                        terminal_context @> %s::jsonb
                        AND terminal_context ->> 'parent_pid' IS NULL
                    )
                THEN terminal_context
                ELSE COALESCE(terminal_context, '{}'::jsonb) || %s::jsonb
            END,
            workflow_name = COALESCE(%s, workflow_name),
            is_local = CASE
                WHEN %s THEN %s
                ELSE is_local
            END,
            sandbox_enabled = COALESCE(%s, sandbox_enabled),
            sandbox_policy_hash = COALESCE(%s, sandbox_policy_hash),
            workspace_generation = CASE
                WHEN %s AND workspace_path IS DISTINCT FROM %s THEN workspace_generation + 1
                ELSE workspace_generation
            END,
            workspace_path = CASE WHEN %s THEN %s ELSE workspace_path END,
            transcript_processed = CASE
                WHEN status = 'expired' AND session_type = 'terminal' THEN FALSE
                ELSE transcript_processed
            END,
            status = CASE
                WHEN status = 'deleted' THEN status
                WHEN status = 'expired' AND session_type = 'terminal' THEN 'active'
                ELSE 'active'
            END,
            updated_at = %s,
            last_activity = %s
        WHERE id = %s
        """,
        (
            machine_id,
            is_set(transcript_path),
            transcript_path if is_set(transcript_path) else None,
            is_set(git_branch),
            git_branch if is_set(git_branch) else None,
            is_set(parent_session_id),
            parent_session_id if is_set(parent_session_id) else None,
            terminal_context_update_json,
            None if clear_origin is None else str(clear_origin),
            CODEX_CLEAR_ORIGIN_KEY,
            json.dumps({CODEX_CLEAR_ORIGIN_KEY: CODEX_CLEAR_ORIGIN_PENDING}),
            terminal_context_update_json,
            workflow_name,
            is_local is not None,
            bool(is_local) if is_local is not None else False,
            sandbox_enabled,
            sandbox_policy_hash,
            is_set(workspace_path),
            workspace_path if is_set(workspace_path) else None,
            is_set(workspace_path),
            workspace_path if is_set(workspace_path) else None,
            now,
            now,
            existing.id,
        ),
    )
    updated = manager.get(existing.id)
    if updated is None:
        raise RuntimeError(f"Session {existing.id} disappeared during update")
    return updated
