"""Daemon-owned task filing, deduplication, and suppression of feedback findings."""

from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import Callable
from datetime import datetime
from difflib import SequenceMatcher
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Any, Protocol

from gobby.feedback.storage import FeedbackRow
from gobby.sessions.handoff import FEEDBACK_TASK_REF_RE
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.project_checkouts import require_root
from gobby.tasks.state_semantics import (
    AWAITING_HUMAN_REVIEW_LABEL,
    get_claimed_session_id,
    is_task_closed,
)
from gobby.utils.daemon_git import GitFailed, GitOk, GitResult, GitTimeout, daemon_git
from gobby.utils.machine_id import require_machine_id

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from gobby.config.sessions import FeedbackReviewConfig

FEEDBACK_TASK_LABEL = "feedback-review"
LLM_REVIEWED_LABEL = "llm-reviewed"
POSSIBLY_FIXED_LABEL = "possibly-fixed"
UNVERIFIED_PREMISE_LABEL = "unverified-premise"
FINDINGS_EPIC_TITLE = "[Gobby Feedback - Reviewed Findings]"
GOBBY_PROJECT_NAME = "gobby"
_RESOLVED_DISPOSITIONS = ("filed-task", "fixed")
_DEDUP_LOOKUP_PAGE_SIZE = 200
_RECENT_CLOSED_TASK_LIMIT = 100
_THEME_SIMILARITY_THRESHOLD = 0.72
_OBSERVATION_LINE_RE = re.compile(
    r"(?:Observations|Additional observations) \(session_feedback\.id\):\s*([^\n]+)",
    re.IGNORECASE,
)
_THEME_LINE_RE = re.compile(r"^Theme:\s*(.+)$", re.IGNORECASE | re.MULTILINE)


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


class FeedbackActions:
    """Apply review findings through the daemon's task manager."""

    def __init__(
        self,
        db: HubDatabase,
        config: FeedbackReviewConfig,
        task_manager: ReviewTaskManagerProtocol | None,
    ) -> None:
        self.db = db
        self.config = config
        self.task_manager = task_manager

    async def apply(
        self,
        findings: dict[str, Any],
        rows: list[FeedbackRow],
        *,
        dry_run: bool,
        checkpoint: Callable[[dict[str, Any]], None] | None = None,
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
            "deferred": [],
            "failed": [],
            "retained": [
                {
                    "observation_ids": _cluster_observation_ids(cluster),
                    "classification": cluster["classification"],
                    "reason": cluster.get("digest_note", "No task proposed"),
                }
                for cluster in findings["clusters"]
                if cluster not in actionable
            ],
        }
        if not actionable:
            return actions
        if dry_run:
            actions["skipped"].append("dry_run: no tasks filed")
            return actions
        if self.task_manager is None:
            actions["failed"].extend(
                {
                    "observation_ids": _cluster_observation_ids(cluster),
                    "proposal": cluster.get("proposed_task"),
                    "error": "task manager unavailable",
                }
                for cluster in actionable
            )
            return actions
        project_id = await asyncio.to_thread(self._gobby_project_id)
        if project_id is None:
            actions["failed"].extend(
                {
                    "observation_ids": _cluster_observation_ids(cluster),
                    "proposal": cluster.get("proposed_task"),
                    "error": f"no project named {GOBBY_PROJECT_NAME!r}",
                }
                for cluster in actionable
            )
            return actions
        epic_id = await asyncio.to_thread(self._findings_epic_id, project_id)
        actions["epic_task_id"] = epic_id
        open_tasks = await asyncio.to_thread(self._open_tasks, project_id)
        recent_closed_tasks = await asyncio.to_thread(self._recent_closed_tasks, project_id)
        rows_by_id = {row.id: row for row in rows}
        repo_root: Path | None = None

        for cluster in actionable[: self.config.max_tasks_per_run]:
            try:
                proposed = cluster["proposed_task"]
                title = str(proposed.get("title") or "").strip()
                if not title:
                    raise ValueError("actionable proposal has no task title")
                observation_ids = _cluster_observation_ids(cluster)
                observed_rows = [
                    rows_by_id[value] for value in observation_ids if value in rows_by_id
                ]
                resolved = _resolved_feedback_disposition(
                    observed_rows, self._resolve_feedback_task
                )
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
                    closed_duplicate = await _find_reachable_closed_duplicate(
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
                    missing_paths = await _missing_paths_at_head(
                        repo_root,
                        cited_paths,
                    )
                    if missing_paths:
                        labels.append(UNVERIFIED_PREMISE_LABEL)
                        priority = 3

                    newest_observation_at = _newest_observation_at(observation_ids, rows_by_id)
                    newest_touch = await _newest_touching_commit(
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
                    {
                        "task_id": str(task.id),
                        "task_ref": _task_ref(task),
                        "title": title,
                        "labels": labels,
                        "observation_ids": observation_ids,
                    }
                )
            except Exception as exc:
                actions["failed"].append(
                    {
                        "observation_ids": _cluster_observation_ids(cluster),
                        "proposal": cluster.get("proposed_task"),
                        "error": str(exc),
                    }
                )
            finally:
                if checkpoint is not None:
                    await asyncio.to_thread(checkpoint, actions)
        overflow = len(actionable) - self.config.max_tasks_per_run
        if overflow > 0:
            actions["skipped"].append(f"task cap reached; {overflow} proposal(s) deferred")
        actions["deferred"].extend(
            {
                "observation_ids": _cluster_observation_ids(cluster),
                "proposal": cluster.get("proposed_task"),
                "reason": "task cap reached",
            }
            for cluster in actionable[self.config.max_tasks_per_run :]
        )
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


async def _find_reachable_closed_duplicate(
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
        if await _task_commits_reachable_from_head(repo_root, candidate):
            return candidate
    return None


async def _task_commits_reachable_from_head(repo_root: Path, task: Any) -> bool:
    raw_commits = getattr(task, "commits", None)
    if not isinstance(raw_commits, (list, tuple)) or not raw_commits:
        return False
    commits = [str(commit).strip() for commit in raw_commits]
    if any(not commit for commit in commits):
        return False
    for commit in commits:
        result = await _run_git(repo_root, "merge-base", "--is-ancestor", commit, "HEAD")
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


async def _run_git(repo_root: Path, *args: str) -> GitResult:
    return await daemon_git.run(args, cwd=repo_root, timeout=30)


async def _require_git_head(repo_root: Path) -> None:
    result = await _run_git(repo_root, "rev-parse", "--verify", "HEAD")
    if not isinstance(result, GitOk):
        detail = result.stderr.strip() or result.stdout.strip() or "unknown git error"
        raise RuntimeError(f"unable to verify feedback paths at HEAD: {detail}")


async def _missing_paths_at_head(repo_root: Path, cited_paths: list[str]) -> list[str]:
    await _require_git_head(repo_root)
    missing: list[str] = []
    for cited_path in cited_paths:
        repo_path = _repo_relative_path(cited_path)
        if repo_path is None:
            missing.append(cited_path)
            continue
        result = await _run_git(repo_root, "cat-file", "-e", f"HEAD:{repo_path}")
        if isinstance(result, GitTimeout) or (
            isinstance(result, GitFailed) and result.returncode is None
        ):
            raise RuntimeError("unable to verify feedback paths at HEAD: git unavailable")
        if not isinstance(result, GitOk):
            missing.append(cited_path)
    return missing


async def _newest_touching_commit(
    repo_root: Path,
    cited_paths: list[str],
) -> tuple[str, datetime] | None:
    repo_paths = [path for value in cited_paths if (path := _repo_relative_path(value))]
    if not repo_paths:
        return None
    result = await _run_git(
        repo_root,
        "log",
        "-1",
        "--format=%H%x00%cI",
        "--",
        *repo_paths,
    )
    if not isinstance(result, GitOk):
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
