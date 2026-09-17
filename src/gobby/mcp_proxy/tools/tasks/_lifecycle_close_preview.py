"""Structured close evaluation and commit-set helpers."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any, Literal

from gobby.mcp_proxy.tools.tasks._close_evaluation_support import (
    CloseEvaluationFingerprint,
    format_git_since,
)
from gobby.storage.tasks import LocalTaskManager, Task
from gobby.tasks.close_checklist import CloseGateResult
from gobby.utils.daemon_git import normalize_commit_sha

# Internal carriers, never part of a close response: ``validation_commands`` travels
# to the validator launch prompt and the checklist already owns gate 10's record,
# and ``stable_facts`` is the persisted review's fingerprint input, repeating
# ``commit_shas`` and the scope inventory the checklist already carries.
_INTERNAL_EXTRA_KEYS = frozenset({"validation_commands", "stable_facts"})


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
    scope_snapshot: tuple[tuple[str, ...], ...] | None = None
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
    blocking_reasons: list[str] = field(default_factory=list)
    required_actions: list[str] = field(default_factory=list)
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
        reasons: Sequence[str] | None = None,
        actions: Sequence[str] | None = None,
        details: dict[str, Any] | None = None,
        extra: dict[str, Any] | None = None,
    ) -> CloseEvaluation:
        self.collect_failure(
            item,
            name,
            error,
            message,
            action=action,
            reasons=reasons,
            actions=actions,
            details=details,
            extra=extra,
        )
        return self

    def record_gate_failure(
        self,
        gate: CloseGateResult,
        *,
        error: str,
        action: str | None = None,
    ) -> CloseEvaluation:
        """Record an already-evaluated failed gate as this evaluation's blocker."""
        self.gates.append(gate)
        self._register_failure(
            gate.name, error, gate.message, action=action, reasons=None, actions=None
        )
        return self

    def collect_failure(
        self,
        item: int,
        name: str,
        error: str,
        message: str,
        *,
        action: str | None = None,
        reasons: Sequence[str] | None = None,
        actions: Sequence[str] | None = None,
        details: dict[str, Any] | None = None,
        extra: dict[str, Any] | None = None,
    ) -> None:
        self.gates.append(
            CloseGateResult(
                item=item,
                name=name,
                status="failed",
                message=message,
                details=details or {},
            )
        )
        self._register_failure(
            name, error, message, action=action, reasons=reasons, actions=actions
        )
        if extra:
            self.extra.update(extra)

    def _register_failure(
        self,
        name: str,
        error: str,
        message: str,
        *,
        action: str | None,
        reasons: Sequence[str] | None,
        actions: Sequence[str] | None,
    ) -> None:
        """Contribute one concise gate-attributed sentence per blocker.

        ``blocking_reasons`` names the gate that blocks, so the same sentence is
        never repeated verbatim in ``message``. ``required_actions`` carries only an
        action the blocking sentences do not already state; a self-actioning gate
        message stands on its own. A caller that already computed its own reasons or
        actions passes them, including an explicitly empty list.
        """
        if self.error is None:
            self.error = error
            self.message = message
            self.action = action or message
        self.blocking_reasons.extend(
            f"{name}: {reason}" for reason in (reasons if reasons is not None else [message])
        )
        if actions is not None:
            self.required_actions.extend(actions)
        elif action and action != message:
            self.required_actions.append(action)

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
            blocking_reasons = self.blocking_reasons
            required_actions = self.required_actions
            if not any(not gate.passed for gate in self.gates):
                # A failure recorded outside the gate API states itself once. A gate
                # that recorded an explicitly empty list keeps it: external_pending
                # blocks on nothing the caller can fix.
                blocking_reasons = blocking_reasons or ([self.message] if self.message else [])
                required_actions = required_actions or ([self.action] if self.action else [])
            response.update(
                {
                    "error": self.error,
                    "message": self.message,
                    "blocking_reasons": blocking_reasons,
                    "required_actions": required_actions,
                }
            )
        if self.validation_status:
            response["validation_status"] = self.validation_status
        if self.verdict:
            response["verdict"] = self.verdict
        checklist = [gate.to_dict() for gate in self.gates] if self.diagnostic else None
        carried = self._checklist_details(checklist)
        response.update(
            {
                key: value
                for key, value in self.extra.items()
                if key not in _INTERNAL_EXTRA_KEYS
                and not (key in carried and carried[key] == value)
            }
        )
        if checklist is not None:
            response.update(
                {
                    "checklist": checklist,
                    "transcript_evidence": dict(self.transcript_evidence),
                    "validation_feedback": self.validation_feedback,
                }
            )
        return response

    @property
    def diagnostic(self) -> bool:
        return self.response_detail == "diagnostic"

    @staticmethod
    def _checklist_details(checklist: list[dict[str, Any]] | None) -> dict[str, Any]:
        """Collect the sections a serialized checklist already carries.

        Gate details are the single home for per-gate inventories such as the task
        scope paths. When the checklist ships, an ``extra`` entry holding the same
        key and value is that section a second time, so the response drops it.
        """
        if checklist is None:
            return {}
        carried: dict[str, Any] = {}
        for entry in checklist:
            details = entry.get("details")
            if isinstance(details, dict):
                carried.update(details)
        return carried


async def resolve_close_commit_shas(
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
            from gobby.tasks.commits import resolve_task_tagged_commits_async

            tagged = await resolve_task_tagged_commits_async(
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
        normalized = await normalize_commit_sha(commit_sha, cwd=cwd)
        if normalized is None:
            return resolved, {
                "error": "invalid_commit_sha",
                "message": f"Commit {commit_sha!r} could not be resolved in the task repository.",
            }
        if normalized not in resolved:
            resolved.append(normalized)
    return resolved, None


async def unlinked_tagged_commits(
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
        from gobby.tasks.commits import unlinked_task_tagged_commits_async

        divergence = await unlinked_task_tagged_commits_async(
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
            task_manager.link_commit(task.id, commit_sha)
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
