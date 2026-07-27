"""Planning and response helpers for conditional task closure."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal

from gobby.tasks.verification_receipt_packet import VerificationReceiptPacket

if TYPE_CHECKING:
    from gobby.storage.tasks import LocalTaskManager, Task


@dataclass
class CloseEvaluationReport:
    """Structured state accumulated by the canonical close evaluator."""

    task_id: str
    response_detail: Literal["concise", "diagnostic"] = "concise"
    commit_shas: list[str] = field(default_factory=list)
    mechanical_gates: list[dict[str, Any]] = field(default_factory=list)
    selected_evidence: dict[str, list[str]] = field(
        default_factory=lambda: {
            "detailed_receipt_ids": [],
            "catalogued_receipt_ids": [],
        }
    )
    evidence_completeness: dict[str, Any] = field(default_factory=dict)
    unassigned_count: int = 0
    validation_status: str | None = None
    validation_feedback: str | None = None

    def pass_gate(self, name: str) -> None:
        self.mechanical_gates.append({"name": name, "passed": True})

    def set_receipt_packet(
        self,
        packet: VerificationReceiptPacket,
        *,
        unassigned_count: int,
    ) -> None:
        self.selected_evidence = {
            "detailed_receipt_ids": list(packet.detailed_receipt_ids),
            "catalogued_receipt_ids": list(packet.catalogued_receipt_ids),
        }
        self.evidence_completeness = packet.disclosure.to_dict()
        self.unassigned_count = unassigned_count

    def preview_response(
        self,
        *,
        can_close: bool,
        error: str | None = None,
        blocking_reasons: list[str] | None = None,
        required_actions: list[str] | None = None,
        extra: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        reasons = blocking_reasons or []
        actions = required_actions or []
        gates = list(self.mechanical_gates)
        if error:
            gates.append(
                {"name": error, "passed": False, "message": reasons[0] if reasons else error}
            )
        response: dict[str, Any] = {
            "success": True,
            "preview": True,
            "can_close": can_close,
            "closed": False,
            "task_id": self.task_id,
            "commit_shas": list(self.commit_shas),
        }
        if reasons:
            response["blocking_reasons"] = reasons
        if actions:
            response["required_actions"] = actions
        if self.response_detail == "diagnostic":
            response.update(
                {
                    "mechanical_gates": gates,
                    "selected_evidence": dict(self.selected_evidence),
                    "evidence_completeness": dict(self.evidence_completeness),
                    "unassigned_receipts": _unassigned_receipt_diagnostics(
                        self.unassigned_count,
                        task_id=self.task_id,
                    ),
                    "blocking_reasons": reasons,
                    "required_actions": actions,
                }
            )
        if error:
            response["error"] = error
        if self.validation_status:
            response["validation_status"] = self.validation_status
        if self.validation_feedback and self.response_detail == "diagnostic":
            response["validation_feedback"] = self.validation_feedback
        if extra:
            if self.response_detail == "diagnostic":
                response.update(extra)
            else:
                diagnostic_keys = {
                    "diagnostics",
                    "evidence_completeness",
                    "mechanical_gates",
                    "selected_evidence",
                    "unassigned_receipts",
                    "validation_feedback",
                }
                response.update(
                    {key: value for key, value in extra.items() if key not in diagnostic_keys}
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
    """Resolve the exact commit set a real close would link, without writes."""
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
                "success": False,
                "error": "claim_window_autolink_failed",
                "message": (
                    "close_task could not resolve task-tagged commits from the claim window. "
                    "Validation would be incomplete; retry after fixing commit resolution."
                ),
            }
        for sha in tagged:
            if sha not in resolved:
                resolved.append(sha)

    if commit_sha:
        if cwd is None:
            return resolved, _repo_path_error()
        from gobby.utils.git import normalize_commit_sha

        normalized = normalize_commit_sha(commit_sha, cwd=cwd)
        if normalized is None:
            return resolved, {
                "success": False,
                "error": "invalid_commit_sha",
                "message": f"Commit {commit_sha!r} could not be resolved in the task repository.",
            }
        if normalized not in resolved:
            resolved.append(normalized)
    return resolved, None


def link_close_commit_shas(
    task_manager: LocalTaskManager,
    *,
    task: Task,
    commit_shas: list[str],
    cwd: str | None,
) -> tuple[Task, dict[str, Any] | None]:
    """Apply a previously resolved close commit set during real closure."""
    existing = set(task.commits or [])
    for commit_sha in commit_shas:
        if commit_sha in existing:
            continue
        if cwd is None:
            return task, _repo_path_error()
        try:
            task_manager.link_commit(task.id, commit_sha, cwd=cwd)
        except ValueError as exc:
            return task, {"success": False, "error": str(exc), "message": str(exc)}
        existing.add(commit_sha)
    refreshed = task_manager.get_task(task.id)
    if refreshed is None:
        return task, {
            "success": False,
            "error": "task_missing_after_commit_link",
            "message": f"Task {task.id} was not found after linking commits.",
        }
    return refreshed, None


def _unassigned_receipt_diagnostics(count: int, *, task_id: str) -> dict[str, Any]:
    diagnostics: dict[str, Any] = {"count": count}
    if count:
        diagnostics["repair_action"] = (
            "Inspect with list_task_verification_receipts(scope='unassigned'), then call "
            f"assign_verification_receipts(task_id='{task_id}', receipt_ids=[...])."
        )
    return diagnostics


def _repo_path_error() -> dict[str, Any]:
    return {
        "success": False,
        "error": "task_repo_path_unavailable",
        "message": (
            "close_task requires a resolvable task repository path for commit operations. "
            "Configure the task project's repo_path or pass project_path."
        ),
    }
