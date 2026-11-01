"""Shared result for close and its read-only preview."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

from pydantic import JsonValue

from gobby.storage.tasks import Task
from gobby.tasks.closure_policy import ClosureDecision


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
    decision: ClosureDecision | None = None
    facts: dict[str, JsonValue] = field(default_factory=dict)
    error: str | None = None
    message: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def ready(self) -> bool:
        return self.error is None and (
            self.already_closed or (self.decision is not None and self.decision.allowed)
        )

    def refuse(self, error: str, message: str) -> CloseEvaluation:
        self.error, self.message = error, message
        return self

    def response(self, *, preview: bool, closed: bool = False) -> dict[str, Any]:
        blockers = list(self.decision.blockers) if self.decision else []
        result: dict[str, Any] = {
            "success": self.ready,
            "task_id": self.task_id,
            "preview": preview,
            "can_close": self.ready,
            "closed": closed or (self.already_closed and not preview),
            "blocking_reasons": blockers,
            "policy_revision": self.decision.policy_revision if self.decision else None,
            "snapshot_revision": self.decision.snapshot_revision if self.decision else None,
            "commit_shas": self.commit_shas,
            "candidate_commit_sha": self.candidate_commit_sha,
            **self.extra,
        }
        if self.error or blockers:
            result["error"] = self.error or "closure_policy"
            result["message"] = self.message or "; ".join(blockers)
        if self.response_detail == "diagnostic":
            result["facts"] = self.facts
        return result
