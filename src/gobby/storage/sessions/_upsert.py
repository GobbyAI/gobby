"""Session registration update helpers."""

from __future__ import annotations

import json
from collections.abc import Sequence
from datetime import datetime
from typing import Protocol

from gobby.storage.session_models import Session
from gobby.terminal_context import merge_terminal_context, parse_terminal_context_value


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
    title: str | None,
    title_source: str | None,
    transcript_path: str | None,
    git_branch: str | None,
    parent_session_id: str | None,
    terminal_context_json: str | None,
    workflow_name: str | None,
    is_local: bool | None,
    sandbox_enabled: bool | None,
    sandbox_policy_hash: str | None,
    now: datetime,
) -> Session:
    terminal_context_update_json = None
    incoming_terminal_context = parse_terminal_context_value(terminal_context_json)
    if incoming_terminal_context is not None:
        merged_terminal_context = merge_terminal_context(
            existing.terminal_context,
            incoming_terminal_context,
        )
        terminal_context_update_json = json.dumps(merged_terminal_context)

    conn.execute(
        """
        UPDATE sessions SET
            title = COALESCE(%s, title),
            title_source = COALESCE(%s, title_source),
            transcript_path = COALESCE(%s, transcript_path),
            git_branch = COALESCE(%s, git_branch),
            parent_session_id = COALESCE(%s, parent_session_id),
            terminal_context = COALESCE(%s, terminal_context),
            workflow_name = COALESCE(%s, workflow_name),
            is_local = CASE
                WHEN %s THEN %s
                ELSE is_local
            END,
            sandbox_enabled = COALESCE(%s, sandbox_enabled),
            sandbox_policy_hash = COALESCE(%s, sandbox_policy_hash),
            status = 'active',
            updated_at = %s
        WHERE id = %s
        """,
        (
            title,
            title_source,
            transcript_path,
            git_branch,
            parent_session_id,
            terminal_context_update_json,
            workflow_name,
            is_local is not None,
            bool(is_local) if is_local is not None else False,
            sandbox_enabled,
            sandbox_policy_hash,
            now,
            existing.id,
        ),
    )
    updated = manager.get(existing.id)
    if updated is None:
        raise RuntimeError(f"Session {existing.id} disappeared during update")
    return updated
