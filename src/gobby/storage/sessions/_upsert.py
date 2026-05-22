"""Session registration update helpers."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Final, Protocol

from gobby.storage.session_models import Session


class _SessionGetter(Protocol):
    def get(self, session_id: str) -> Session | None: ...


class _TransactionConnection(Protocol):
    def execute(self, sql: str, params: Sequence[object] = ()) -> object: ...


_SESSION_UNIQUE_CONSTRAINT = "idx_sessions_unique"
_SQLITE_SESSION_UNIQUE_PREFIX = "UNIQUE constraint failed:"
_SESSION_UNIQUE_REQUIRED_COLUMNS = (
    "sessions.external_id",
    "sessions.machine_id",
    "sessions.source",
    "sessions.project_id",
)
PRESERVE_IS_LOCAL: Final = -1


def is_session_unique_conflict(exc: BaseException) -> bool:
    messages = [str(arg) for arg in exc.args if isinstance(arg, str)]
    messages.append(str(exc))
    for message in messages:
        if _SESSION_UNIQUE_CONSTRAINT in message:
            return True
        if _SQLITE_SESSION_UNIQUE_PREFIX in message and all(
            column in message for column in _SESSION_UNIQUE_REQUIRED_COLUMNS
        ):
            return True
    return False


def update_existing_session(
    manager: _SessionGetter,
    conn: _TransactionConnection,
    existing: Session,
    *,
    title: str | None,
    transcript_path: str | None,
    git_branch: str | None,
    parent_session_id: str | None,
    terminal_context_json: str | None,
    workflow_name: str | None,
    is_local: bool | None,
    sandbox_enabled: bool | None,
    sandbox_policy_hash: str | None,
    now: str,
) -> Session:
    conn.execute(
        """
        UPDATE sessions SET
            title = COALESCE(?, title),
            transcript_path = COALESCE(?, transcript_path),
            git_branch = COALESCE(?, git_branch),
            parent_session_id = COALESCE(?, parent_session_id),
            terminal_context = COALESCE(?, terminal_context),
            workflow_name = COALESCE(?, workflow_name),
            is_local = CASE
                WHEN ? = -1 THEN is_local
                WHEN ? THEN TRUE
                ELSE FALSE
            END,
            sandbox_enabled = COALESCE(?, sandbox_enabled),
            sandbox_policy_hash = COALESCE(?, sandbox_policy_hash),
            status = 'active',
            updated_at = ?
        WHERE id = ?
        """,
        (
            title,
            transcript_path,
            git_branch,
            parent_session_id,
            terminal_context_json,
            workflow_name,
            PRESERVE_IS_LOCAL if is_local is None else int(is_local),
            0 if is_local is None else int(is_local),
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
