"""Distill unreviewed session feedback into filed tasks and a digest.

The LLM proposes; deterministic code disposes. One distill pass clusters the
batch, then this service files deduplicated tasks into the project named
``gobby``, marks the batch reviewed, and renders a Markdown digest onto the
run row. Rows stay unreviewed until the actions land, so a failed run is
safely re-picked by the next one.
"""

from __future__ import annotations

import asyncio
import logging
from collections import Counter
from typing import TYPE_CHECKING, Any, Protocol

from gobby.feedback.storage import FeedbackReviewStore, FeedbackRow
from gobby.prompts.loader import PromptLoader
from gobby.storage.hub.protocol import HubDatabase
from gobby.utils.json_helpers import json_dumps

if TYPE_CHECKING:
    from gobby.config.sessions import FeedbackReviewConfig

logger = logging.getLogger(__name__)

FEEDBACK_TASK_LABEL = "feedback-review"
GOBBY_PROJECT_NAME = "gobby"
DISTILL_TOTAL_DEADLINE_SECONDS = 900.0
_DEDUP_LOOKUP_LIMIT = 20
_CLASSIFICATIONS = ("defect", "guidance-gap", "noise", "praise")

FEEDBACK_FINDINGS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "clusters": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "observation_ids": {"type": "array", "items": {"type": "string"}},
                    "theme": {"type": "string"},
                    "classification": {"type": "string", "enum": list(_CLASSIFICATIONS)},
                    "proposed_task": {
                        "type": ["object", "null"],
                        "properties": {
                            "title": {"type": "string"},
                            "description": {"type": "string"},
                            "labels": {"type": "array", "items": {"type": "string"}},
                            "priority": {"type": "integer", "minimum": 1, "maximum": 4},
                        },
                        "required": ["title", "description"],
                        "additionalProperties": False,
                    },
                    "digest_note": {"type": "string"},
                },
                "required": ["observation_ids", "theme", "classification", "digest_note"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["clusters"],
    "additionalProperties": False,
}


class JSONFeatureProvider(Protocol):
    """The slice of LLMService the distill pass needs."""

    async def call_json_feature(
        self,
        feature_config: Any,
        prompt: str,
        system_prompt: str | None = None,
        *,
        json_schema: dict[str, Any],
        max_tokens: int | None = None,
        caller: str | None = None,
        total_timeout_seconds: float | None = None,
    ) -> dict[str, Any]: ...


class ReviewTaskManagerProtocol(Protocol):
    """The slice of LocalTaskManager the action layer needs."""

    def list_tasks(
        self,
        *,
        project_id: str | None = ...,
        closed: bool | None = ...,
        title_like: str | None = ...,
        limit: int = ...,
    ) -> list[Any]: ...

    def create_task(
        self,
        project_id: str,
        title: str,
        description: str | None = None,
        *,
        priority: int = ...,
        labels: list[str] | None = ...,
        category: str | None = ...,
        validation_criteria: str | None = ...,
    ) -> Any: ...


class FeedbackReviewService:
    """Run the nightly (or on-demand) session-feedback review loop."""

    def __init__(
        self,
        db: HubDatabase,
        llm_service: JSONFeatureProvider,
        config: FeedbackReviewConfig,
        task_manager: ReviewTaskManagerProtocol | None,
    ) -> None:
        self.db = db
        self.store = FeedbackReviewStore(db)
        self.llm_service = llm_service
        self.config = config
        self.task_manager = task_manager

    async def run_review(self, *, dry_run: bool = False) -> dict[str, Any]:
        """Distill one batch, file tasks, mark rows reviewed, render the digest."""
        rows = self.store.list_unreviewed(self.config.max_rows_per_run)
        if not rows:
            return {"status": "no_rows", "run_id": None, "rows_considered": 0}

        run_id = self.store.create_run(
            dry_run=dry_run,
            window_start=rows[0].created_at,
            window_end=rows[-1].created_at,
            rows_considered=len(rows),
        )
        try:
            findings = await self._distill(rows)
            actions = await self._apply_actions(findings, dry_run=dry_run)
            if not dry_run:
                actions["rows_marked_reviewed"] = self.store.mark_reviewed(
                    [row.id for row in rows], run_id
                )
            digest = _render_digest(rows, findings, actions, dry_run=dry_run)
            self.store.finalize_run(
                run_id,
                status="completed",
                findings=findings,
                actions=actions,
                digest_md=digest,
            )
        except Exception as exc:
            self.store.finalize_run(run_id, status="failed", error=str(exc))
            raise
        return {
            "status": "completed",
            "run_id": run_id,
            "dry_run": dry_run,
            "rows_considered": len(rows),
            "tasks_filed": len(actions.get("filed", [])),
            "deduplicated": actions.get("deduplicated", 0),
        }

    async def _distill(self, rows: list[FeedbackRow]) -> dict[str, Any]:
        loader = PromptLoader(db=self.db)
        prompt = loader.render(
            self.config.prompt_path,
            {
                "observations": _render_rows_json(rows),
                "max_tasks": self.config.max_tasks_per_run,
            },
        )
        response = await self.llm_service.call_json_feature(
            self.config,
            prompt,
            json_schema=FEEDBACK_FINDINGS_SCHEMA,
            max_tokens=self.config.max_tokens,
            caller="feedback.review",
            total_timeout_seconds=DISTILL_TOTAL_DEADLINE_SECONDS,
        )
        if not isinstance(response, dict):
            raise TypeError(f"feedback.review expected dict, got {type(response).__name__}")
        clusters = response.get("clusters")
        if not isinstance(clusters, list):
            raise ValueError("feedback.review response missing 'clusters' list")
        return {"clusters": [cluster for cluster in clusters if isinstance(cluster, dict)]}

    async def _apply_actions(self, findings: dict[str, Any], *, dry_run: bool) -> dict[str, Any]:
        """File deduplicated tasks for actionable clusters; the LLM never writes."""
        actionable = [
            cluster
            for cluster in findings["clusters"]
            if cluster.get("classification") in ("defect", "guidance-gap")
            and isinstance(cluster.get("proposed_task"), dict)
        ]
        actions: dict[str, Any] = {"filed": [], "deduplicated": 0, "skipped": []}
        if not actionable:
            return actions
        if dry_run:
            actions["skipped"].append("dry_run: no tasks filed")
            return actions
        if self.task_manager is None:
            actions["skipped"].append("task manager unavailable; digest only")
            return actions
        project_id = await asyncio.to_thread(self._gobby_project_id)
        if project_id is None:
            actions["skipped"].append(f"no project named {GOBBY_PROJECT_NAME!r}; digest only")
            return actions

        seen_titles: set[str] = set()
        for cluster in actionable[: self.config.max_tasks_per_run]:
            proposed = cluster["proposed_task"]
            title = str(proposed.get("title") or "").strip()
            if not title:
                continue
            title_key = title.casefold()
            if title_key in seen_titles or await asyncio.to_thread(
                self._has_open_task_titled, project_id, title
            ):
                actions["deduplicated"] += 1
                continue
            seen_titles.add(title_key)
            labels = [FEEDBACK_TASK_LABEL]
            if cluster.get("classification") == "guidance-gap":
                labels.append("needs-decision")
            task = await asyncio.to_thread(
                self._create_task, project_id, title, cluster, proposed, labels
            )
            actions["filed"].append(
                {"task_id": str(getattr(task, "id", "")), "title": title, "labels": labels}
            )
        overflow = len(actionable) - self.config.max_tasks_per_run
        if overflow > 0:
            actions["skipped"].append(f"task cap reached; {overflow} proposal(s) deferred")
        return actions

    def _gobby_project_id(self) -> str | None:
        row = self.db.fetchone(
            "SELECT id FROM projects WHERE name = %s LIMIT 1",
            (GOBBY_PROJECT_NAME,),
        )
        return str(row["id"]) if row else None

    def _has_open_task_titled(self, project_id: str, title: str) -> bool:
        assert self.task_manager is not None
        candidates = self.task_manager.list_tasks(
            project_id=project_id,
            closed=False,
            title_like=title,
            limit=_DEDUP_LOOKUP_LIMIT,
        )
        title_key = title.casefold()
        return any(
            str(getattr(candidate, "title", "")).strip().casefold() == title_key
            for candidate in candidates
        )

    def _create_task(
        self,
        project_id: str,
        title: str,
        cluster: dict[str, Any],
        proposed: dict[str, Any],
        labels: list[str],
    ) -> Any:
        assert self.task_manager is not None
        priority = proposed.get("priority")
        return self.task_manager.create_task(
            project_id,
            title,
            _task_description(cluster, proposed),
            priority=int(priority) if isinstance(priority, int) else 2,
            labels=labels,
            category="research",
            validation_criteria=(
                "The recurring feedback theme is resolved or explicitly declined: the "
                "referenced observations no longer reproduce, or the decline reason is "
                "recorded on this task."
            ),
        )


def _task_description(cluster: dict[str, Any], proposed: dict[str, Any]) -> str:
    observation_ids = ", ".join(str(oid) for oid in cluster.get("observation_ids", []))
    return (
        f"{proposed.get('description', '')}\n\n"
        f"Filed by the session-feedback review loop.\n"
        f"Theme: {cluster.get('theme', '')}\n"
        f"Classification: {cluster.get('classification', '')}\n"
        f"Observations (session_feedback.id): {observation_ids}"
    ).strip()


def _render_rows_json(rows: list[FeedbackRow]) -> str:
    payload = [
        {
            "id": row.id,
            "kind": row.kind,
            "kind_other_label": row.kind_other_label,
            "evidence": row.evidence,
            "impact": row.impact,
            "frequency": row.frequency,
            "suggestion": row.suggestion,
            "disposition": row.disposition,
            "created_at": row.created_at.isoformat(),
        }
        for row in rows
    ]
    return json_dumps(payload, indent=2)


def _render_digest(
    rows: list[FeedbackRow],
    findings: dict[str, Any],
    actions: dict[str, Any],
    *,
    dry_run: bool,
) -> str:
    kind_counts = Counter(row.kind for row in rows)
    lines = ["# Session-feedback review digest", ""]
    if dry_run:
        lines.extend(["**Dry run** — no tasks filed, no rows marked reviewed.", ""])
    lines.append(f"Rows considered: {len(rows)}")
    lines.append(
        "Counts by kind: "
        + ", ".join(f"{kind} {count}" for kind, count in kind_counts.most_common())
    )
    lines.append("")

    lines.append("## Clusters")
    for cluster in findings["clusters"]:
        classification = cluster.get("classification", "?")
        size = len(cluster.get("observation_ids", []))
        lines.append(f"- **{cluster.get('theme', '(untitled)')}** [{classification}, {size} obs]")
        note = str(cluster.get("digest_note") or "").strip()
        if note:
            lines.append(f"  {note}")
    lines.append("")

    lines.append("## Actions")
    for filed in actions.get("filed", []):
        lines.append(f"- Filed #{filed.get('task_id', '?')}: {filed.get('title', '')}")
    if actions.get("deduplicated"):
        lines.append(f"- Deduplicated against open tasks: {actions['deduplicated']}")
    for skipped in actions.get("skipped", []):
        lines.append(f"- Skipped: {skipped}")
    if not actions.get("filed") and not actions.get("deduplicated") and not actions.get("skipped"):
        lines.append("- None")
    lines.append("")

    label_counts = Counter(row.kind_other_label for row in rows if row.kind_other_label is not None)
    lines.append("## Other-label audit")
    if not label_counts:
        lines.append("- No `other` observations in this batch.")
    else:
        for label, count in label_counts.most_common():
            suffix = " — recurring; consider promoting to the kind enum" if count >= 2 else ""
            lines.append(f"- `{label}`: {count}{suffix}")
    return "\n".join(lines).strip()
