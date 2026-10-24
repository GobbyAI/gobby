"""Shared result for close and its read-only preview."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

from gobby.storage.tasks import Task
from gobby.tasks.close_checklist import CloseChecklist
from gobby.tasks.close_invariants import CLOSE_GATE_ORDER as CLOSE_GATE_ORDER


@dataclass
class CloseEvaluation:
    task_id: str
    response_detail: Literal["concise", "diagnostic"] = "concise"
    task: Task | None = None
    repo_path: str | None = None
    resolved_session_id: str | None = None
    edit_session_id: str | None = None
    commit_shas: list[str] = field(default_factory=list)
    candidate_commit_sha: str | None = None
    is_epic: bool = False
    already_closed: bool = False
    checklist: CloseChecklist = field(default_factory=lambda: CloseChecklist(()))
    error: str | None = None
    message: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def ready(self) -> bool:
        evaluated = self.already_closed or len(self.checklist.gates) == len(CLOSE_GATE_ORDER)
        return self.error is None and evaluated and self.checklist.ready

    def refuse(self, error: str, message: str) -> CloseEvaluation:
        self.error, self.message = error, message
        return self

    def response(self, *, preview: bool, closed: bool = False) -> dict[str, Any]:
        failure = self.checklist.first_failure
        result: dict[str, Any] = {
            "success": self.ready,
            "task_id": self.task_id,
            "preview": preview,
            "can_close": self.ready,
            "closed": closed or (self.already_closed and not preview),
            "gates": self.checklist.summary(),
            "commit_shas": self.commit_shas,
            "candidate_commit_sha": self.candidate_commit_sha,
            **self.extra,
        }
        if self.error or failure:
            result["error"] = self.error or (failure.name if failure else None)
            result["message"] = self.message or (failure.message if failure else None)
            result["blocking_reasons"] = [gate.to_dict() for gate in self.checklist.all_failures]
        if self.response_detail == "diagnostic":
            result["gates"] = self.checklist.to_dict()["gates"]
        return result
