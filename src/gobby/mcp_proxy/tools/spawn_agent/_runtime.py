"""Runtime helpers for spawn_agent implementation."""

from __future__ import annotations

import logging
from collections.abc import Iterable
from typing import Any, Protocol, runtime_checkable

logger = logging.getLogger(__name__)


@runtime_checkable
class ReasoningPayload(Protocol):
    def to_dict(self) -> dict[str, Any]: ...


class SpawnRunStorage(Protocol):
    def update_child_session(self, run_id: str, child_session_id: str) -> object: ...

    def update_runtime(
        self,
        run_id: str,
        *,
        pid: int | None = None,
        terminal_id: str | None = None,
        tmux_session_name: str | None = None,
        worktree_id: str | None = None,
        clone_id: str | None = None,
    ) -> None: ...


class SpawnRuntimeRunner(Protocol):
    @property
    def run_storage(self) -> SpawnRunStorage: ...


def _parent_session_ref(session_manager: Any | None, parent_session_id: str) -> str:
    """Return the coordinator's ``#N`` ref so a leaf can address it by either form."""
    if session_manager is None:
        return parent_session_id
    try:
        parent_session = session_manager.get(parent_session_id)
    except Exception:
        logger.debug("Failed to load parent session %s", parent_session_id, exc_info=True)
        return parent_session_id
    seq_num = getattr(parent_session, "seq_num", None)
    return f"#{seq_num}" if seq_num else parent_session_id


def _normalize_string_list(value: Any) -> list[str]:
    if not isinstance(value, Iterable) or isinstance(value, str | bytes | dict):
        return []
    return [item for item in value if isinstance(item, str)]


def _normalize_optional_model(value: str | None) -> str | None:
    if value is None:
        return None
    value = value.strip()
    return value if value and value.lower() != "inherit" else None


def _persist_spawn_runtime(
    runner: SpawnRuntimeRunner,
    run_id: str,
    spawn_result: Any,
    *,
    tmux_session_name: str | None,
    worktree_id: str | None,
    clone_id: str | None,
    terminal_id: str | None = None,
) -> None:
    child_session_id = getattr(spawn_result, "child_session_id", None)
    if child_session_id is not None:
        try:
            runner.run_storage.update_child_session(run_id, child_session_id)
        except Exception as e:
            logger.warning("Failed to update child_session_id for %s: %s", run_id, e)

    try:
        runner.run_storage.update_runtime(
            run_id,
            pid=getattr(spawn_result, "pid", None),
            terminal_id=terminal_id or getattr(spawn_result, "terminal_id", None),
            worktree_id=worktree_id,
            clone_id=clone_id,
        )
    except Exception as e:
        logger.warning("Failed to persist runtime state for %s: %s", run_id, e)


def _tmux_runtime_metadata(spawn_result: Any) -> tuple[str | None, str | None, str | None]:
    tmux_session_name = getattr(spawn_result, "tmux_session_name", None)
    if not isinstance(tmux_session_name, str):
        terminal_id = getattr(spawn_result, "terminal_id", None)
        tmux_session_name = terminal_id if isinstance(terminal_id, str) else None
    tmux_socket_name = getattr(spawn_result, "tmux_socket_name", None)
    if not isinstance(tmux_socket_name, str):
        tmux_socket_name = None
    tmux_socket_path = getattr(spawn_result, "tmux_socket_path", None)
    if not isinstance(tmux_socket_path, str):
        tmux_socket_path = None
    return tmux_session_name, tmux_socket_name, tmux_socket_path


def _build_spawn_success_response(
    *,
    run_id: str,
    spawn_result: Any,
    effective_isolation: str,
    isolation_ctx: Any,
    base_commit_sha: Any,
    tmux_socket_name: str | None,
    tmux_socket_path: str | None,
    code_index_preflight_warning: dict[str, str] | None,
    reasoning: Any | None,
    terminal_id: str | None = None,
    tmux_session_name: str | None = None,
) -> dict[str, Any]:
    response = {
        "success": True,
        "run_id": run_id,
        "child_session_id": spawn_result.child_session_id,
        "status": spawn_result.status,
        "isolation": effective_isolation,
        "branch_name": isolation_ctx.branch_name,
        "worktree_id": isolation_ctx.worktree_id,
        "worktree_path": str(isolation_ctx.cwd) if effective_isolation == "worktree" else None,
        "clone_id": isolation_ctx.clone_id,
        "clone_path": str(isolation_ctx.cwd) if effective_isolation == "clone" else None,
        "base_commit_sha": base_commit_sha if isinstance(base_commit_sha, str) else None,
        "pid": spawn_result.pid,
        "terminal_id": terminal_id or getattr(spawn_result, "terminal_id", None),
        "tmux_session_name": tmux_session_name,
        "tmux_socket_name": tmux_socket_name,
        "tmux_socket_path": tmux_socket_path,
        "message": spawn_result.message,
    }
    isolation_extra = isolation_ctx.extra
    if "reused_worktree_rebase_conflict" in isolation_extra:
        response.update(
            {
                "reuse_outcome": "fresh_after_conflict",
                "reused_worktree_rebase_conflict": isolation_extra[
                    "reused_worktree_rebase_conflict"
                ],
                "reused_worktree_id": isolation_extra.get("reused_worktree_id"),
                "reused_worktree_path": isolation_extra.get("reused_worktree_path"),
            }
        )
    elif isolation_extra.get("reused_worktree") is True:
        response.update({"reuse_outcome": "reused", "reused_worktree": True})
    else:
        response["reuse_outcome"] = "fresh"
    if reasoning is not None:
        if not isinstance(reasoning, ReasoningPayload):
            raise TypeError(
                f"spawn reasoning payload must implement to_dict(); got {type(reasoning).__name__}"
            )
        response["reasoning"] = reasoning.to_dict()
    if code_index_preflight_warning is not None:
        response["warnings"] = [code_index_preflight_warning]
    return response
