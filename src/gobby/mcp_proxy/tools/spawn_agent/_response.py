"""Backend-neutral response helpers for spawn_agent."""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class ReasoningPayload(Protocol):
    def to_dict(self) -> dict[str, Any]: ...


def _tmux_runtime_metadata(terminal: Any | None) -> tuple[str | None, str | None]:
    """Return optional tmux socket diagnostics from a terminal row."""
    if terminal is None or getattr(terminal, "backend", None) != "tmux":
        return None, None
    locator = getattr(terminal, "locator", None)
    if not isinstance(locator, dict):
        return None, None
    socket_name = locator.get("socket_name")
    socket_path = locator.get("socket_path")
    return (
        socket_name if isinstance(socket_name, str) else None,
        socket_path if isinstance(socket_path, str) else None,
    )


def build_spawn_response(
    *,
    run_id: str,
    spawn_result: Any,
    effective_isolation: str,
    isolation_ctx: Any,
    base_commit_sha: Any,
    code_index_preflight_warning: dict[str, str] | None,
    reasoning: Any | None,
    terminal: Any | None = None,
) -> dict[str, Any]:
    """Build the MCP response around backend-neutral terminal identity."""
    terminal_id = getattr(terminal, "id", None) or getattr(spawn_result, "terminal_id", None)
    backend = getattr(terminal, "backend", None) or getattr(spawn_result, "backend", None)
    response = {
        "success": True,
        "run_id": run_id,
        "agent_run_id": run_id,
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
        "terminal_id": terminal_id,
        "backend": backend,
        "error": getattr(spawn_result, "error", None),
        "error_detail": getattr(spawn_result, "error_detail", None),
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
