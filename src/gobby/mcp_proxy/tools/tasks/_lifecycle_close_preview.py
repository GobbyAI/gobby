"""Structured close evaluation and commit-set helpers."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

from gobby.mcp_proxy.tools.tasks._close_evaluation_support import (
    CloseEvaluationFingerprint,
    format_git_since,
)
from gobby.storage.tasks import LocalTaskManager, Task
from gobby.tasks.close_checklist import CloseGateResult


@dataclass
class CloseEvaluation:
    """Reusable result of the read-mostly close evaluation phase."""

    requested_task_id: str
    response_detail: Literal["concise", "diagnostic"] = "concise"
    task: Task | None = None
    task_id: str | None = None
    repo_path: str | None = None
    resolved_session_id: str | None = None
    edit_session_id: str | None = None
    claim_started_at: str | None = None
    commit_shas: list[str] = field(default_factory=list)
    edited_paths: set[str] = field(default_factory=set)
    had_attributed_edits: bool = False
    scope_snapshot: tuple[tuple[str, ...], tuple[str, ...], tuple[str, ...]] | None = None
    scope_justification: str | None = None
    fingerprint: CloseEvaluationFingerprint | None = None
    skip_leaf_checks: bool = False
    is_epic: bool = False
    gates: list[CloseGateResult] = field(default_factory=list)
    transcript_evidence: dict[str, Any] = field(default_factory=dict)
    validation_status: str | None = None
    validation_feedback: str | None = None
    validation_reset_reason: str | None = None
    verdict: dict[str, Any] | None = None
    error: str | None = None
    message: str | None = None
    action: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def ready(self) -> bool:
        return self.error is None and all(gate.passed for gate in self.gates)

    def pass_gate(
        self,
        item: int,
        name: str,
        message: str,
        *,
        details: dict[str, Any] | None = None,
        skipped: bool = False,
    ) -> None:
        self.gates.append(
            CloseGateResult(
                item=item,
                name=name,
                status="skipped" if skipped else "passed",
                message=message,
                details=details or {},
            )
        )

    def fail(
        self,
        item: int,
        name: str,
        error: str,
        message: str,
        *,
        action: str | None = None,
        details: dict[str, Any] | None = None,
        extra: dict[str, Any] | None = None,
    ) -> CloseEvaluation:
        self.gates.append(
            CloseGateResult(
                item=item,
                name=name,
                status="failed",
                message=message,
                details=details or {},
            )
        )
        self.error = error
        self.message = message
        self.action = action or message
        if extra:
            self.extra.update(extra)
        return self

    def response(self, *, preview: bool, closed: bool = False) -> dict[str, Any]:
        response: dict[str, Any] = {
            "success": closed,
            "preview": preview,
            "can_close": self.ready,
            "closed": closed,
            "task_id": self.task_id or self.requested_task_id,
            "commit_shas": list(self.commit_shas),
        }
        if self.error:
            response.update(
                {
                    "error": self.error,
                    "message": self.message,
                    "blocking_reasons": [self.message] if self.message else [],
                    "required_actions": [self.action] if self.action else [],
                }
            )
        if self.validation_status:
            response["validation_status"] = self.validation_status
        if self.verdict:
            response["verdict"] = self.verdict
        response.update(self.extra)
        if self.response_detail == "diagnostic":
            response.update(
                {
                    "checklist": [gate.to_dict() for gate in self.gates],
                    "transcript_evidence": dict(self.transcript_evidence),
                    "validation_feedback": self.validation_feedback,
                }
            )
        return response


def resolve_close_commit_shas(
    task_manager: LocalTaskManager,
    *,
    task: Task,
    task_id: str,
    claim_started_at: str | None,
    commit_sha: str | None,
    cwd: str | None,
    project_name: str | None,
) -> tuple[list[str], dict[str, Any] | None]:
    """Resolve the exact prospective commit set without writing task state."""
    resolved = list(dict.fromkeys(task.commits or []))
    if claim_started_at:
        if cwd is None:
            return resolved, _repo_path_error()
        try:
            from gobby.tasks.commits import resolve_task_tagged_commits

            tagged = resolve_task_tagged_commits(
                task_manager,
                task_id=task_id,
                since=claim_started_at,
                cwd=cwd,
                project_name=project_name,
                project_id=task.project_id,
            )
        except Exception:
            return resolved, {
                "error": "claim_window_autolink_failed",
                "message": (
                    "close_task could not resolve task-tagged commits from the claim window. "
                    "Fix commit resolution and retry."
                ),
            }
        resolved.extend(sha for sha in tagged if sha not in resolved)
    if commit_sha:
        if cwd is None:
            return resolved, _repo_path_error()
        from gobby.utils.git import normalize_commit_sha

        normalized = normalize_commit_sha(commit_sha, cwd=cwd)
        if normalized is None:
            return resolved, {
                "error": "invalid_commit_sha",
                "message": f"Commit {commit_sha!r} could not be resolved in the task repository.",
            }
        if normalized not in resolved:
            resolved.append(normalized)
    return resolved, None


def unlinked_tagged_commits(
    task_manager: LocalTaskManager,
    *,
    task: Task,
    task_id: str,
    commit_shas: list[str],
    cwd: str | None,
    project_name: str | None,
) -> tuple[tuple[list[str], list[str]], dict[str, Any] | None]:
    """Find task-tagged commits since the task's creation that the close would not judge."""
    if cwd is None:
        return ([], []), _repo_path_error()
    try:
        from gobby.tasks.commits import unlinked_task_tagged_commits

        divergence = unlinked_task_tagged_commits(
            task_manager,
            task_id=task_id,
            since=format_git_since(task.created_at),
            cwd=cwd,
            project_name=project_name,
            project_id=task.project_id,
            linked=commit_shas,
        )
    except Exception:
        return ([], []), {
            "error": "tagged_commit_scan_failed",
            "message": (
                "close_task could not scan git for commits tagged with this task. "
                "Fix commit resolution and retry."
            ),
        }
    return divergence, None


def link_close_commit_shas(
    task_manager: LocalTaskManager,
    *,
    task: Task,
    commit_shas: list[str],
    cwd: str | None,
) -> tuple[Task, dict[str, Any] | None]:
    """Link the evaluated commit set and return the refreshed task lock."""
    existing = set(task.commits or [])
    for commit_sha in commit_shas:
        if commit_sha in existing:
            continue
        if cwd is None:
            return task, _repo_path_error()
        try:
            task_manager.link_commit(task.id, commit_sha, cwd=cwd)
        except ValueError as exc:
            return task, {"error": "commit_link_failed", "message": str(exc)}
        existing.add(commit_sha)
    refreshed = task_manager.get_task(task.id)
    if refreshed is None:
        return task, {
            "error": "task_missing_after_commit_link",
            "message": f"Task {task.id} disappeared while commits were being linked.",
        }
    return refreshed, None


def _repo_path_error() -> dict[str, Any]:
    return {
        "error": "repository_path_unavailable",
        "message": "A registered repository path is required for commit resolution.",
    }


__all__ = [
    "CloseEvaluation",
    "link_close_commit_shas",
    "resolve_close_commit_shas",
]
