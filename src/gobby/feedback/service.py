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
import re
import subprocess
from collections import Counter
from collections.abc import Callable
from datetime import datetime
from difflib import SequenceMatcher
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Any, Protocol

from gobby.feedback.storage import FeedbackReviewStore, FeedbackRow
from gobby.prompts.loader import PromptLoader
from gobby.sessions.handoff import FEEDBACK_TASK_REF_RE
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.project_checkouts import require_root
from gobby.tasks.state_semantics import (
    AWAITING_HUMAN_REVIEW_LABEL,
    get_claimed_session_id,
    is_task_closed,
)
from gobby.utils.json_helpers import json_dumps
from gobby.utils.machine_id import require_machine_id

if TYPE_CHECKING:
    from gobby.config.sessions import FeedbackReviewConfig

logger = logging.getLogger(__name__)

FEEDBACK_TASK_LABEL = "feedback-review"
LLM_REVIEWED_LABEL = "llm-reviewed"
POSSIBLY_FIXED_LABEL = "possibly-fixed"
UNVERIFIED_PREMISE_LABEL = "unverified-premise"
FINDINGS_EPIC_TITLE = "[Gobby Feedback - Reviewed Findings]"
GOBBY_PROJECT_NAME = "gobby"
_RESOLVED_DISPOSITIONS = ("filed-task", "fixed")
DISTILL_TOTAL_DEADLINE_SECONDS = 900.0
_DEDUP_LOOKUP_PAGE_SIZE = 200
_RECENT_CLOSED_TASK_LIMIT = 100
_THEME_SIMILARITY_THRESHOLD = 0.72
_CLASSIFICATIONS = ("defect", "guidance-gap", "noise", "praise")
_OBSERVATION_LINE_RE = re.compile(
    r"(?:Observations|Additional observations) \(session_feedback\.id\):\s*([^\n]+)",
    re.IGNORECASE,
)
_THEME_LINE_RE = re.compile(r"^Theme:\s*(.+)$", re.IGNORECASE | re.MULTILINE)

FEEDBACK_FINDINGS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "clusters": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "observation_ids": {"type": "array", "items": {"type": "string"}},
                    "cited_paths": {"type": "array", "items": {"type": "string"}},
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
                "required": [
                    "observation_ids",
                    "cited_paths",
                    "theme",
                    "classification",
                    "digest_note",
                ],
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
        label: str | None = ...,
        limit: int = ...,
        offset: int = ...,
        sort_by: str = ...,
        sort_order: str = ...,
    ) -> list[Any]: ...

    def get_task(self, task_id: str, project_id: str | None = None) -> Any | None: ...

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
        parent_task_id: str | None = ...,
        task_type: str = ...,
    ) -> Any: ...

    def update_task(self, task_id: str, *, description: str) -> Any: ...


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
            actions = await self._apply_actions(findings, rows, dry_run=dry_run)
            if not dry_run:
                actions["rows_marked_reviewed"] = self.store.mark_reviewed(
                    [row.id for row in rows], run_id
                )
            digest = _render_digest(
                rows,
                findings,
                actions,
                dry_run=dry_run,
                resolve_task=self._resolve_feedback_task,
            )
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

    async def _apply_actions(
        self,
        findings: dict[str, Any],
        rows: list[FeedbackRow],
        *,
        dry_run: bool,
    ) -> dict[str, Any]:
        """File deduplicated tasks for actionable clusters; the LLM never writes."""
        actionable = [
            cluster
            for cluster in findings["clusters"]
            if cluster.get("classification") in ("defect", "guidance-gap")
            and isinstance(cluster.get("proposed_task"), dict)
        ]
        actions: dict[str, Any] = {
            "filed": [],
            "deduplicated": 0,
            "suppressed": [],
            "skipped": [],
        }
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
        epic_id = await asyncio.to_thread(self._findings_epic_id, project_id)
        actions["epic_task_id"] = epic_id
        open_tasks = await asyncio.to_thread(self._open_tasks, project_id)
        recent_closed_tasks = await asyncio.to_thread(self._recent_closed_tasks, project_id)
        rows_by_id = {row.id: row for row in rows}
        repo_root: Path | None = None

        for cluster in actionable[: self.config.max_tasks_per_run]:
            proposed = cluster["proposed_task"]
            title = str(proposed.get("title") or "").strip()
            if not title:
                continue
            observation_ids = _cluster_observation_ids(cluster)
            observed_rows = [rows_by_id[value] for value in observation_ids if value in rows_by_id]
            resolved = _resolved_feedback_disposition(observed_rows, self._resolve_feedback_task)
            if resolved is not None:
                disposition, observation_id, task_ref = resolved
                suppression = {
                    "title": title,
                    "observation_ids": observation_ids,
                    "disposition": disposition,
                    "reason": f"observation {observation_id} has resolved {disposition} disposition",
                }
                if task_ref is not None:
                    suppression["matched_task_ref"] = task_ref
                actions["suppressed"].append(suppression)
                continue

            duplicate = _find_duplicate(open_tasks, cluster, title)
            if duplicate is not None:
                await asyncio.to_thread(
                    self._append_observation_ids,
                    duplicate,
                    observation_ids,
                )
                actions["deduplicated"] += 1
                actions["suppressed"].append(
                    {
                        "title": title,
                        "observation_ids": observation_ids,
                        "matched_task_ref": _task_ref(duplicate),
                        "reason": "matched open task",
                    }
                )
                continue

            if recent_closed_tasks:
                if repo_root is None:
                    repo_root = await asyncio.to_thread(self._gobby_repo_root, project_id)
                closed_duplicate = await asyncio.to_thread(
                    _find_reachable_closed_duplicate,
                    recent_closed_tasks,
                    cluster,
                    title,
                    repo_root,
                )
            else:
                closed_duplicate = None
            if closed_duplicate is not None:
                actions["deduplicated"] += 1
                actions["suppressed"].append(
                    {
                        "title": title,
                        "observation_ids": observation_ids,
                        "matched_task_ref": _task_ref(closed_duplicate),
                        "matched_commits": list(getattr(closed_duplicate, "commits", ()) or ()),
                        "reason": (
                            "matched recently closed valid task whose linked commits "
                            "are reachable from HEAD"
                        ),
                    }
                )
                continue

            labels = [FEEDBACK_TASK_LABEL, LLM_REVIEWED_LABEL, AWAITING_HUMAN_REVIEW_LABEL]
            if cluster.get("classification") == "guidance-gap":
                labels.append("needs-decision")
            proposed_priority = proposed.get("priority")
            priority = int(proposed_priority) if isinstance(proposed_priority, int) else 2
            cited_paths = _cluster_cited_paths(cluster)
            missing_paths: list[str] = []
            newest_touching_commit: str | None = None
            if cited_paths:
                if repo_root is None:
                    repo_root = await asyncio.to_thread(self._gobby_repo_root, project_id)
                missing_paths = await asyncio.to_thread(
                    _missing_paths_at_head,
                    repo_root,
                    cited_paths,
                )
                if missing_paths:
                    labels.append(UNVERIFIED_PREMISE_LABEL)
                    priority = 3

                newest_observation_at = _newest_observation_at(observation_ids, rows_by_id)
                newest_touch = await asyncio.to_thread(
                    _newest_touching_commit,
                    repo_root,
                    cited_paths,
                )
                if (
                    newest_touch is not None
                    and newest_observation_at is not None
                    and newest_touch[1] > newest_observation_at
                ):
                    newest_touching_commit = newest_touch[0]
                    labels.append(POSSIBLY_FIXED_LABEL)

            task = await asyncio.to_thread(
                self._create_task,
                project_id,
                epic_id,
                title,
                cluster,
                proposed,
                labels,
                priority,
                missing_paths,
                newest_touching_commit,
            )
            open_tasks.append(task)
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

    def _resolve_feedback_task(self, row: FeedbackRow, task_ref: str) -> Any | None:
        if self.task_manager is None:
            return None
        session = self.db.fetchone(
            "SELECT project_id FROM sessions WHERE id = %s",
            (row.session_id,),
        )
        if session is None or not session["project_id"]:
            return None
        try:
            return self.task_manager.get_task(task_ref, str(session["project_id"]))
        except ValueError:
            return None

    def _findings_epic_id(self, project_id: str) -> str:
        """Find the reviewed-findings epic by exact title, creating it when missing."""
        assert self.task_manager is not None
        candidates = self.task_manager.list_tasks(
            project_id=project_id,
            closed=False,
            title_like=FINDINGS_EPIC_TITLE,
            limit=_DEDUP_LOOKUP_PAGE_SIZE,
        )
        title_key = FINDINGS_EPIC_TITLE.casefold()
        for candidate in candidates:
            if (
                str(getattr(candidate, "title", "")).strip().casefold() == title_key
                and getattr(candidate, "task_type", "task") == "epic"
            ):
                return str(candidate.id)
        epic = self.task_manager.create_task(
            project_id,
            FINDINGS_EPIC_TITLE,
            "Findings filed by the nightly session-feedback review loop. The loop "
            "locates this epic by its exact title and recreates it when missing, "
            "so keep the title unchanged. A human verifier triages the children.",
            task_type="epic",
            labels=[FEEDBACK_TASK_LABEL],
        )
        return str(epic.id)

    def _open_tasks(self, project_id: str) -> list[Any]:
        assert self.task_manager is not None
        tasks: list[Any] = []
        offset = 0
        while True:
            page = self.task_manager.list_tasks(
                project_id=project_id,
                closed=False,
                limit=_DEDUP_LOOKUP_PAGE_SIZE,
                offset=offset,
            )
            tasks.extend(page)
            if len(page) < _DEDUP_LOOKUP_PAGE_SIZE:
                return tasks
            offset += len(page)

    def _recent_closed_tasks(self, project_id: str) -> list[Any]:
        assert self.task_manager is not None
        return self.task_manager.list_tasks(
            project_id=project_id,
            closed=True,
            limit=_RECENT_CLOSED_TASK_LIMIT,
            sort_by="updated_at",
            sort_order="desc",
        )

    def _gobby_repo_root(self, project_id: str) -> Path:
        return Path(require_root(self.db, project_id, require_machine_id()))

    def _append_observation_ids(self, task: Any, observation_ids: list[str]) -> None:
        assert self.task_manager is not None
        description = str(getattr(task, "description", "") or "")
        attached = _description_observation_ids(description)
        new_ids = [
            observation_id for observation_id in observation_ids if observation_id not in attached
        ]
        if not new_ids:
            return
        suffix = f"Additional observations (session_feedback.id): {', '.join(new_ids)}"
        updated_description = f"{description.rstrip()}\n{suffix}".strip()
        self.task_manager.update_task(str(task.id), description=updated_description)
        try:
            task.description = updated_description
        except (AttributeError, TypeError):
            pass

    def _create_task(
        self,
        project_id: str,
        epic_id: str,
        title: str,
        cluster: dict[str, Any],
        proposed: dict[str, Any],
        labels: list[str],
        priority: int,
        missing_paths: list[str],
        newest_touching_commit: str | None,
    ) -> Any:
        assert self.task_manager is not None
        return self.task_manager.create_task(
            project_id,
            title,
            _task_description(
                cluster,
                proposed,
                missing_paths=missing_paths,
                newest_touching_commit=newest_touching_commit,
            ),
            priority=priority,
            labels=labels,
            category="research",
            parent_task_id=epic_id,
            validation_criteria=(
                "The recurring feedback theme is resolved or explicitly declined: the "
                "referenced observations no longer reproduce, or the decline reason is "
                "recorded on this task."
            ),
        )


def _task_description(
    cluster: dict[str, Any],
    proposed: dict[str, Any],
    *,
    missing_paths: list[str] | None = None,
    newest_touching_commit: str | None = None,
) -> str:
    observation_ids = ", ".join(str(oid) for oid in cluster.get("observation_ids", []))
    cited_paths = ", ".join(_cluster_cited_paths(cluster))
    description = (
        f"{proposed.get('description', '')}\n\n"
        f"Filed by the session-feedback review loop.\n"
        f"Theme: {cluster.get('theme', '')}\n"
        f"Classification: {cluster.get('classification', '')}\n"
        f"Observations (session_feedback.id): {observation_ids}"
    )
    if cited_paths:
        description += f"\nCited repository paths: {cited_paths}"
    if missing_paths:
        description += f"\nPremise verification: missing at HEAD: {', '.join(missing_paths)}"
    if newest_touching_commit:
        description += (
            "\nRecency check: possibly fixed by newest commit touching cited paths: "
            f"{newest_touching_commit}"
        )
    return description.strip()


def _cluster_observation_ids(cluster: dict[str, Any]) -> list[str]:
    raw_ids = cluster.get("observation_ids")
    if not isinstance(raw_ids, list):
        return []
    return list(dict.fromkeys(str(value).strip() for value in raw_ids if str(value).strip()))


def _cluster_cited_paths(cluster: dict[str, Any]) -> list[str]:
    raw_paths = cluster.get("cited_paths")
    if not isinstance(raw_paths, list):
        return []
    return list(dict.fromkeys(str(value).strip() for value in raw_paths if str(value).strip()))


def _description_observation_ids(description: str) -> set[str]:
    attached: set[str] = set()
    for match in _OBSERVATION_LINE_RE.finditer(description):
        attached.update(value.strip() for value in match.group(1).split(",") if value.strip())
    return attached


def _description_theme(description: str) -> str:
    match = _THEME_LINE_RE.search(description)
    return match.group(1).strip() if match else ""


def _normalized_theme(value: str) -> str:
    return " ".join(re.findall(r"[a-z0-9]+", value.casefold()))


def _themes_match(first: str, second: str) -> bool:
    normalized_first = _normalized_theme(first)
    normalized_second = _normalized_theme(second)
    if not normalized_first or not normalized_second:
        return False
    return (
        SequenceMatcher(None, normalized_first, normalized_second).ratio()
        >= _THEME_SIMILARITY_THRESHOLD
    )


def _find_duplicate(candidates: list[Any], cluster: dict[str, Any], title: str) -> Any | None:
    observation_ids = set(_cluster_observation_ids(cluster))
    theme = str(cluster.get("theme") or "")
    for candidate in candidates:
        if str(getattr(candidate, "task_type", "task")) == "epic":
            continue
        candidate_description = str(getattr(candidate, "description", "") or "")
        if observation_ids & _description_observation_ids(candidate_description):
            return candidate
        if _themes_match(theme, _description_theme(candidate_description)):
            return candidate
        if _themes_match(title, str(getattr(candidate, "title", "") or "")):
            return candidate
    return None


def _find_reachable_closed_duplicate(
    candidates: list[Any],
    cluster: dict[str, Any],
    title: str,
    repo_root: Path,
) -> Any | None:
    for candidate in candidates:
        if getattr(candidate, "validation_status", None) != "valid":
            continue
        if _find_duplicate([candidate], cluster, title) is None:
            continue
        if _task_commits_reachable_from_head(repo_root, candidate):
            return candidate
    return None


def _task_commits_reachable_from_head(repo_root: Path, task: Any) -> bool:
    raw_commits = getattr(task, "commits", None)
    if not isinstance(raw_commits, (list, tuple)) or not raw_commits:
        return False
    commits = [str(commit).strip() for commit in raw_commits]
    if any(not commit for commit in commits):
        return False
    for commit in commits:
        result = _run_git(repo_root, "merge-base", "--is-ancestor", commit, "HEAD")
        if result.returncode == 0:
            continue
        if result.returncode != 1:
            detail = result.stderr.strip() or result.stdout.strip() or "unknown git error"
            logger.warning("Could not verify feedback task commit %s at HEAD: %s", commit, detail)
        return False
    return True


def _task_ref(task: Any) -> str:
    seq_num = getattr(task, "seq_num", None)
    return f"#{seq_num}" if seq_num is not None else str(getattr(task, "id", ""))


def _resolved_feedback_disposition(
    rows: list[FeedbackRow],
    resolve_task: Callable[[FeedbackRow, str], Any | None] | None,
) -> tuple[str, str, str | None] | None:
    for row in rows:
        if row.disposition not in _RESOLVED_DISPOSITIONS:
            continue
        match = FEEDBACK_TASK_REF_RE.search(row.evidence)
        task_ref = match.group(0) if match is not None else None
        if row.disposition == "fixed":
            return row.disposition, row.id, task_ref
        task = resolve_task(row, task_ref) if resolve_task is not None and task_ref else None
        labels = set(getattr(task, "labels", ()) or ())
        if (
            is_task_closed(task)
            or get_claimed_session_id(task) is not None
            or {"needs-decision", "clean-window"}.intersection(labels)
        ):
            return row.disposition, row.id, task_ref
    return None


def _repo_relative_path(value: str) -> str | None:
    candidate = PurePosixPath(value.replace("\\", "/"))
    if candidate.is_absolute() or not candidate.parts or ".." in candidate.parts:
        return None
    normalized = candidate.as_posix()
    return normalized if normalized not in {"", "."} else None


def _run_git(repo_root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(repo_root), *args],
        capture_output=True,
        check=False,
        text=True,
        timeout=30,
    )


def _require_git_head(repo_root: Path) -> None:
    result = _run_git(repo_root, "rev-parse", "--verify", "HEAD")
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or "unknown git error"
        raise RuntimeError(f"unable to verify feedback paths at HEAD: {detail}")


def _missing_paths_at_head(repo_root: Path, cited_paths: list[str]) -> list[str]:
    _require_git_head(repo_root)
    missing: list[str] = []
    for cited_path in cited_paths:
        repo_path = _repo_relative_path(cited_path)
        if repo_path is None:
            missing.append(cited_path)
            continue
        result = _run_git(repo_root, "cat-file", "-e", f"HEAD:{repo_path}")
        if result.returncode != 0:
            missing.append(cited_path)
    return missing


def _newest_touching_commit(
    repo_root: Path,
    cited_paths: list[str],
) -> tuple[str, datetime] | None:
    repo_paths = [path for value in cited_paths if (path := _repo_relative_path(value))]
    if not repo_paths:
        return None
    result = _run_git(
        repo_root,
        "log",
        "-1",
        "--format=%H%x00%cI",
        "--",
        *repo_paths,
    )
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or "unknown git error"
        raise RuntimeError(f"unable to check feedback path recency: {detail}")
    payload = result.stdout.strip()
    if not payload:
        return None
    commit_sha, separator, committed_at = payload.partition("\x00")
    if not separator:
        raise RuntimeError("unable to check feedback path recency: malformed git log output")
    return commit_sha, datetime.fromisoformat(committed_at)


def _newest_observation_at(
    observation_ids: list[str],
    rows_by_id: dict[str, FeedbackRow],
) -> datetime | None:
    observed_at = [rows_by_id[value].created_at for value in observation_ids if value in rows_by_id]
    return max(observed_at) if observed_at else None


def _render_rows_json(rows: list[FeedbackRow]) -> str:
    payload = [
        {
            "id": row.id,
            "session_id": row.session_id,
            "source": row.source,
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


def _shirked_cluster_lines(
    rows: list[FeedbackRow],
    findings: dict[str, Any],
    resolve_task: Callable[[FeedbackRow, str], Any | None] | None = None,
) -> list[str]:
    """One digest line per actionable cluster no observer filed or fixed in-line."""
    rows_by_id = {row.id: row for row in rows}
    lines: list[str] = []
    for cluster in findings["clusters"]:
        if cluster.get("classification") not in ("defect", "guidance-gap"):
            continue
        observed = [
            rows_by_id[obs_id]
            for obs_id in cluster.get("observation_ids", [])
            if obs_id in rows_by_id
        ]
        if not observed:
            continue
        resolved = False
        unclaimed_refs: list[str] = []
        for row in observed:
            if row.disposition not in _RESOLVED_DISPOSITIONS:
                continue
            if row.disposition == "fixed":
                resolved = True
                break
            match = FEEDBACK_TASK_REF_RE.search(row.evidence)
            task_ref = match.group(0) if match is not None else None
            task = resolve_task(row, task_ref) if resolve_task is not None and task_ref else None
            labels = set(getattr(task, "labels", ()) or ())
            if (
                is_task_closed(task)
                or get_claimed_session_id(task) is not None
                or {"needs-decision", "clean-window"}.intersection(labels)
            ):
                resolved = True
                break
            if task_ref is not None and task_ref not in unclaimed_refs:
                unclaimed_refs.append(task_ref)
        if resolved:
            continue
        dispositions = Counter(row.disposition or "none" for row in observed)
        breakdown = ", ".join(f"{name} {count}" for name, count in dispositions.most_common())
        unresolved = f"; {', '.join(unclaimed_refs)} filed unclaimed" if unclaimed_refs else ""
        lines.append(
            f"- **{cluster.get('theme', '(untitled)')}** "
            f"({len(observed)} obs; dispositions: {breakdown}{unresolved})"
        )
    return lines


def _render_digest(
    rows: list[FeedbackRow],
    findings: dict[str, Any],
    actions: dict[str, Any],
    *,
    dry_run: bool,
    resolve_task: Callable[[FeedbackRow, str], Any | None] | None = None,
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
    for suppressed in actions.get("suppressed", []):
        matched = suppressed.get("matched_task_ref")
        matched_suffix = f" ({matched})" if matched else ""
        lines.append(
            f"- Suppressed {suppressed.get('title', '(untitled)')}{matched_suffix}: "
            f"{suppressed.get('reason', 'resolved before filing')}"
        )
    if actions.get("deduplicated"):
        lines.append(f"- Deduplicated against existing tasks: {actions['deduplicated']}")
    for skipped in actions.get("skipped", []):
        lines.append(f"- Skipped: {skipped}")
    if (
        not actions.get("filed")
        and not actions.get("suppressed")
        and not actions.get("deduplicated")
        and not actions.get("skipped")
    ):
        lines.append("- None")
    lines.append("")

    lines.append("## Shirked found work")
    shirked = _shirked_cluster_lines(rows, findings, resolve_task)
    if shirked:
        lines.append(
            "Actionable clusters whose observations were deferred into the survey "
            "instead of being filed or fixed in-line per the found-work ladder:"
        )
        lines.extend(shirked)
    else:
        lines.append(
            "- None — every actionable cluster had a fixed disposition or a filed-task "
            "whose task was closed, claimed, or labeled needs-decision/clean-window."
        )
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
