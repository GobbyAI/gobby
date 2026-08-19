"""Session registration update helpers."""

from __future__ import annotations

import json
from collections.abc import Sequence
from datetime import datetime
from typing import Protocol, cast

from gobby.storage.hub.protocol import Transaction
from gobby.storage.session_models import Session
from gobby.storage.workspace_machine_scope import MachineOwnershipMismatchError
from gobby.terminal_context import parse_terminal_context_value

from ._title_update import apply_title_mutation
from ._update_sentinel import UnsetType, is_set


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
    title: str | None | UnsetType,
    title_source: str | None | UnsetType,
    transcript_path: str | None | UnsetType,
    git_branch: str | None | UnsetType,
    parent_session_id: str | None | UnsetType,
    terminal_context_json: str | None,
    workflow_name: str | None,
    is_local: bool | None,
    sandbox_enabled: bool | None,
    sandbox_policy_hash: str | None,
    now: datetime,
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
                ELSE COALESCE(terminal_context, '{}'::jsonb) || %s::jsonb
            END,
            workflow_name = COALESCE(%s, workflow_name),
            is_local = CASE
                WHEN %s THEN %s
                ELSE is_local
            END,
            sandbox_enabled = COALESCE(%s, sandbox_enabled),
            sandbox_policy_hash = COALESCE(%s, sandbox_policy_hash),
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
            terminal_context_update_json,
            workflow_name,
            is_local is not None,
            bool(is_local) if is_local is not None else False,
            sandbox_enabled,
            sandbox_policy_hash,
            now,
            now,
            existing.id,
        ),
    )
    apply_title_mutation(
        cast(Transaction, conn),
        existing.id,
        title_is_set=is_set(title),
        title=title if is_set(title) else None,
        title_source_is_set=is_set(title_source),
        title_source=title_source if is_set(title_source) else None,
        updated_at=now,
    )
    updated = manager.get(existing.id)
    if updated is None:
        raise RuntimeError(f"Session {existing.id} disappeared during update")
    return updated
