"""Webhook-first GitHub issue triage service."""

from __future__ import annotations

import hmac
import inspect
import json
import logging
import sqlite3
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass, field
from hashlib import sha256
from typing import TYPE_CHECKING, Any, Literal, Protocol

from gobby.build import BuildOptions, build
from gobby.github_triage.issue_index import (
    GitHubIssueIndexer,
    IssueDuplicate,
    IssueSnapshot,
    content_hash,
)
from gobby.integrations.github_helper import parse_github_repo
from gobby.storage.github_triage import (
    GitHubTriageStore,
    TriageVerdict,
)
from gobby.storage.projects import LocalProjectManager
from gobby.storage.tasks import LocalTaskManager, Task

if TYPE_CHECKING:
    from gobby.storage.cron_models import CronJob
    from gobby.storage.database import DatabaseProtocol

logger = logging.getLogger(__name__)

PROCESSABLE_ISSUE_ACTIONS = frozenset({"opened", "edited", "reopened"})
TRIAGE_ACCEPTED_LABEL = "gobby:accepted"
TRIAGE_SKIPPED_LABEL = "gobby:skipped"
TRIAGE_DUPLICATE_LABEL = "gobby:duplicate"
TRIAGE_ESCALATED_LABEL = "gobby:needs-triage"
TRIAGE_RESOLVED_LABEL = "gobby:resolved"

BuildFunc = Callable[[str, BuildOptions], Awaitable[Any]]


class TriageError(ValueError):
    """Base error for rejected triage intake."""


class TriageWebhookError(TriageError):
    """Webhook validation failed."""


class TriageDisabledError(TriageError):
    """Triage is disabled for the project."""


class TriageJudge(Protocol):
    async def __call__(
        self,
        issue: IssueSnapshot,
        duplicates: list[IssueDuplicate],
    ) -> TriageOutcome:
        """Return a structured triage outcome."""


@dataclass(frozen=True)
class WebhookAcceptance:
    """Result of webhook validation and delivery persistence."""

    delivery_id: str
    event: str
    action: str | None
    status: str
    duplicate: bool = False


@dataclass(frozen=True)
class TriageOutcome:
    """Structured decision returned by the triage judgment layer."""

    verdict: TriageVerdict
    reason: str
    comment: str | None = None
    labels: tuple[str, ...] = ()
    close_issue: bool = False
    duplicate: IssueDuplicate | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        duplicate = self.duplicate
        data["duplicate"] = asdict(duplicate) if duplicate else None
        data["labels"] = list(self.labels)
        return data


class GitHubIssueTriageService:
    """Coordinates GitHub issue intake, dedup, task creation, and GitHub updates."""

    def __init__(
        self,
        *,
        db: DatabaseProtocol,
        mcp_manager: Any | None = None,
        task_manager: LocalTaskManager | None = None,
        project_manager: LocalProjectManager | None = None,
        memory_manager: Any | None = None,
        secret_store: Any | None = None,
        judge: TriageJudge | None = None,
        build_func: BuildFunc | None = None,
    ) -> None:
        self.db = db
        self.mcp_manager = mcp_manager
        self.task_manager = task_manager or LocalTaskManager(db)
        self.project_manager = project_manager or LocalProjectManager(db)
        self.memory_manager = memory_manager
        self.secret_store = secret_store
        self.judge = judge
        self.store = GitHubTriageStore(db)
        self._build_func = build_func

    async def handle_webhook_delivery(
        self, project_id: str, headers: dict[str, str], raw_body: bytes
    ) -> WebhookAcceptance:
        """Validate, persist, and process one GitHub webhook delivery."""
        accepted = self.accept_webhook_delivery(project_id, headers, raw_body)
        if accepted.status == "pending" and not accepted.duplicate:
            await self.process_delivery(project_id, accepted.delivery_id)
        return accepted

    def accept_webhook_delivery(
        self, project_id: str, headers: dict[str, str], raw_body: bytes
    ) -> WebhookAcceptance:
        """Validate HMAC and persist a webhook delivery for idempotent processing."""
        normalized_headers = _normalize_headers(headers)
        event = _required_header(normalized_headers, "x-github-event")
        delivery_id = _required_header(normalized_headers, "x-github-delivery")

        project = self.project_manager.get(project_id)
        if not project or project.deleted_at:
            raise TriageWebhookError(f"Unknown project: {project_id}")

        config = self.store.get_config(project_id, fallback_repo=project.github_repo)
        if not config.enabled or not config.webhook_enabled:
            raise TriageDisabledError("GitHub issue triage webhooks are disabled")

        self._validate_signature(config.webhook_secret_ref, normalized_headers, raw_body)
        payload = _loads_payload(raw_body)
        action = payload.get("action")
        repo = _payload_repo(payload)
        issue_number = _payload_issue_number(payload)
        allowed_repos = config.repositories_with_fallback(project.github_repo)
        if not allowed_repos:
            raise TriageWebhookError("No repositories are enabled for GitHub issue triage")
        if repo and repo not in allowed_repos:
            raise TriageWebhookError(f"Repository {repo!r} is not enabled for triage")

        status = _initial_delivery_status(event, action)
        delivery, inserted = self.store.record_delivery(
            project_id=project_id,
            delivery_id=delivery_id,
            event=event,
            action=action,
            repository=repo,
            issue_number=issue_number,
            headers=normalized_headers,
            raw_body=raw_body,
            status=status,
        )
        if not inserted:
            return WebhookAcceptance(delivery_id, event, action, "duplicate", duplicate=True)
        if status != "pending":
            self.store.update_delivery_status(
                project_id, delivery.delivery_id, status, processed=True
            )
        return WebhookAcceptance(delivery_id, event, action, status)

    async def process_delivery(self, project_id: str, delivery_id: str) -> dict[str, Any]:
        """Process a previously persisted delivery."""
        delivery = self.store.get_delivery(project_id, delivery_id)
        if delivery is None:
            raise TriageWebhookError(f"Unknown GitHub delivery: {delivery_id}")
        if delivery.status in {"processed", "ignored", "duplicate"}:
            return {"status": delivery.status}
        if delivery.status != "pending":
            return {"status": delivery.status}

        claimed_delivery = self.store.claim_delivery_for_processing(project_id, delivery_id)
        if claimed_delivery is None:
            current = self.store.get_delivery(project_id, delivery_id)
            return {"status": current.status if current else "missing"}
        delivery = claimed_delivery
        try:
            payload = json.loads(delivery.raw_body)
            repo = _payload_repo(payload)
            issue_number = _payload_issue_number(payload)
            if delivery.event != "issues" or delivery.action not in PROCESSABLE_ISSUE_ACTIONS:
                self.store.update_delivery_status(
                    project_id, delivery_id, "ignored", processed=True
                )
                return {"status": "ignored"}
            if repo is None or issue_number is None:
                raise TriageWebhookError("Issue webhook missing repository or issue number")
            result = await self.triage_issue(
                project_id,
                repo,
                issue_number,
                source="webhook",
                issue_data=payload.get("issue"),
            )
            self.store.update_delivery_status(project_id, delivery_id, "processed", processed=True)
            return result
        except Exception as exc:
            self.store.update_delivery_status(
                project_id,
                delivery_id,
                "error",
                error=str(exc),
                processed=True,
            )
            raise

    async def reconcile_project_repos(self, project_id: str) -> dict[str, int]:
        """Reconcile all configured repositories as a webhook recovery path."""
        project = self.project_manager.get(project_id)
        if not project or project.deleted_at:
            raise ValueError(f"Unknown project: {project_id}")
        config = self.store.get_config(project_id, fallback_repo=project.github_repo)
        if not config.enabled:
            return {"scanned": 0, "triaged": 0, "errors": 0}

        scanned = triaged = errors = 0
        for repo in config.repositories_with_fallback(project.github_repo):
            owner, repo_name = parse_github_repo(repo)
            page = 1
            while True:
                issues = await self._github_call(
                    "list_issues",
                    {
                        "owner": owner,
                        "repo": repo_name,
                        "state": "open",
                        "per_page": 100,
                        "page": page,
                    },
                )
                if isinstance(issues, dict):
                    issues = issues.get("issues", [])
                if not isinstance(issues, list) or not issues:
                    break
                for issue in issues:
                    if not isinstance(issue, dict) or issue.get("pull_request"):
                        continue
                    scanned += 1
                    issue_number = issue.get("number")
                    try:
                        if issue_number is None:
                            raise TriageWebhookError("Reconciled issue missing number")
                        await self.triage_issue(
                            project_id,
                            repo,
                            int(issue_number),
                            source="reconcile",
                            issue_data=issue,
                        )
                        triaged += 1
                    except Exception:
                        logger.warning(
                            "Failed to reconcile GitHub issue %s#%s",
                            repo,
                            issue_number,
                            exc_info=True,
                        )
                        errors += 1
                if len(issues) < 100:
                    break
                page += 1
        return {"scanned": scanned, "triaged": triaged, "errors": errors}

    async def triage_issue(
        self,
        project_id: str,
        repo: str,
        issue_number: int,
        source: str,
        *,
        issue_data: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Triage one GitHub issue and apply the resulting outcome."""
        project = self.project_manager.get(project_id)
        if not project or project.deleted_at:
            raise ValueError(f"Unknown project: {project_id}")
        config = self.store.get_config(project_id, fallback_repo=project.github_repo)
        if not config.enabled:
            raise TriageDisabledError("GitHub issue triage is disabled")

        issue_data = issue_data or await self._fetch_issue(repo, issue_number)
        if issue_data.get("pull_request"):
            outcome = TriageOutcome("skip", "GitHub pull requests are not triaged as issues")
            issue = IssueSnapshot.from_github(project_id=project_id, repo=repo, issue=issue_data)
            return await self.apply_triage_outcome(project_id, issue, outcome, source)

        issue = IssueSnapshot.from_github(project_id=project_id, repo=repo, issue=issue_data)
        existing = self.store.get_issue_record(project_id, repo, issue_number)
        current_hash = content_hash(issue)
        existing_task_id = existing.task_id if existing else None

        indexer = self._indexer()
        duplicates = await indexer.find_duplicates(issue)
        outcome = await self._judge(issue, duplicates)
        if (
            existing is not None
            and existing.content_hash == current_hash
            and existing.verdict == outcome.verdict
        ):
            return {
                "project_id": project_id,
                "repo": issue.repo,
                "issue_number": issue.issue_number,
                "source": source,
                "verdict": existing.verdict,
                "task_id": existing.task_id,
                "content_hash": current_hash,
                "vector_point_id": existing.vector_point_id,
            }
        result = await self.apply_triage_outcome(project_id, issue, outcome, source)
        task_id = (
            result.get("task_id") if isinstance(result.get("task_id"), str) else existing_task_id
        )
        point_id = await indexer.upsert(issue, task_id=task_id)

        self.store.upsert_issue_record(
            project_id=project_id,
            repo=repo,
            issue_number=issue_number,
            issue_url=issue.issue_url,
            issue_state=issue.state,
            labels=list(issue.labels),
            issue_updated_at=issue.updated_at,
            content_hash=current_hash,
            verdict=outcome.verdict,
            decision=outcome.to_dict(),
            task_id=task_id,
            vector_point_id=point_id,
            dedup_issue_key=outcome.duplicate.issue_key if outcome.duplicate else None,
            source=source,
        )
        result["content_hash"] = current_hash
        result["vector_point_id"] = point_id
        return result

    async def apply_triage_outcome(
        self,
        project_id: str,
        issue: IssueSnapshot,
        outcome: TriageOutcome,
        source: str,
    ) -> dict[str, Any]:
        """Apply deterministic side effects for a triage outcome."""
        task: Task | None = None
        if outcome.verdict == "implement":
            task = self._create_or_update_task(project_id, issue)
            await self._comment_and_label(
                issue,
                outcome.comment or f"Accepted for implementation as Gobby task #{task.seq_num}.",
                [TRIAGE_ACCEPTED_LABEL, *outcome.labels],
            )
            await self._run_build(task)
        elif outcome.verdict == "dedup":
            duplicate = outcome.duplicate
            duplicate_text = (
                f"Duplicate of {duplicate.issue_key}" if duplicate else "Duplicate issue"
            )
            await self._comment_and_label(
                issue,
                outcome.comment or duplicate_text,
                [TRIAGE_DUPLICATE_LABEL, *outcome.labels],
            )
            if outcome.close_issue:
                await self._close_issue(issue)
        elif outcome.verdict == "skip":
            await self._comment_and_label(
                issue,
                outcome.comment or f"Skipped by Gobby triage: {outcome.reason}",
                [TRIAGE_SKIPPED_LABEL, *outcome.labels],
            )
            if outcome.close_issue:
                await self._close_issue(issue)
        else:
            await self._comment_and_label(
                issue,
                outcome.comment or f"Gobby needs human triage: {outcome.reason}",
                [TRIAGE_ESCALATED_LABEL, *outcome.labels],
            )

        return {
            "project_id": project_id,
            "repo": issue.repo,
            "issue_number": issue.issue_number,
            "source": source,
            "verdict": outcome.verdict,
            "task_id": task.id if task else None,
        }

    async def close_linked_issue_after_merge(self, task_id: str, merge_sha: str | None) -> bool:
        """Comment, label, and close a task-linked GitHub issue after merge."""
        task = self.task_manager.get_task(task_id)
        if not task.github_repo or not task.github_issue_number:
            return False
        issue = IssueSnapshot(
            project_id=task.project_id,
            repo=task.github_repo,
            issue_number=task.github_issue_number,
            title=task.title,
            body=task.description or "",
            state="open",
            labels=tuple(task.labels or []),
            updated_at=None,
            issue_url=None,
        )
        suffix = f" in {merge_sha}" if merge_sha else ""
        await self._comment_and_label(
            issue,
            f"Resolved by merged Gobby task #{task.seq_num}{suffix}.",
            [TRIAGE_RESOLVED_LABEL],
        )
        await self._close_issue(issue)
        return True

    def _create_or_update_task(self, project_id: str, issue: IssueSnapshot) -> Task:
        description = _task_description(issue)
        labels = sorted(set(issue.labels) | {"github"})
        try:
            with self.db.transaction_immediate() as conn:
                existing = conn.execute(
                    "SELECT id FROM tasks WHERE project_id = ? AND github_repo = ? "
                    "AND github_issue_number = ? LIMIT 1",
                    (project_id, issue.repo, issue.issue_number),
                ).fetchone()
                if existing:
                    return self.task_manager.update_task(
                        existing["id"],
                        title=issue.title,
                        description=description,
                        labels=labels,
                    )
                return self.task_manager.create_task(
                    project_id=project_id,
                    title=issue.title,
                    description=description,
                    labels=labels,
                    category="code",
                    task_type="feature",
                    validation_criteria=(
                        f"GitHub issue {issue.repo}#{issue.issue_number} is implemented and linked."
                    ),
                    github_issue_number=issue.issue_number,
                    github_repo=issue.repo,
                )
        except sqlite3.IntegrityError:
            existing = self.db.fetchone(
                "SELECT id FROM tasks WHERE project_id = ? AND github_repo = ? "
                "AND github_issue_number = ? LIMIT 1",
                (project_id, issue.repo, issue.issue_number),
            )
            if existing is None:
                raise
            return self.task_manager.update_task(
                existing["id"],
                title=issue.title,
                description=description,
                labels=labels,
            )

    async def _fetch_issue(self, repo: str, issue_number: int) -> dict[str, Any]:
        owner, repo_name = parse_github_repo(repo)
        result = await self._github_call(
            "get_issue",
            {"owner": owner, "repo": repo_name, "issue_number": issue_number},
        )
        if not isinstance(result, dict):
            raise RuntimeError(f"GitHub get_issue returned {type(result).__name__}")
        return result

    async def _comment_and_label(
        self, issue: IssueSnapshot, comment: str, labels: list[str]
    ) -> None:
        owner, repo_name = parse_github_repo(issue.repo)
        await self._github_call(
            "add_issue_comment",
            {
                "owner": owner,
                "repo": repo_name,
                "issue_number": issue.issue_number,
                "body": comment,
            },
            required=False,
        )
        deduped_labels = sorted({label for label in labels if label})
        if deduped_labels:
            await self._github_call(
                "add_labels_to_issue",
                {
                    "owner": owner,
                    "repo": repo_name,
                    "issue_number": issue.issue_number,
                    "labels": deduped_labels,
                },
                required=False,
            )

    async def _close_issue(self, issue: IssueSnapshot) -> None:
        owner, repo_name = parse_github_repo(issue.repo)
        await self._github_call(
            "update_issue",
            {
                "owner": owner,
                "repo": repo_name,
                "issue_number": issue.issue_number,
                "state": "closed",
            },
            required=False,
        )

    async def _run_build(self, task: Task) -> None:
        options = BuildOptions(
            skip_stages=[],
            isolation="none",
        )
        build_func = self._build_func
        if build_func is None:

            async def _default_build(input_ref: str, opts: BuildOptions) -> Any:
                return await build(input_ref, opts, db=self.db, project_id=task.project_id)

            build_func = _default_build
        try:
            await build_func(f"#{task.seq_num}", options)
        except Exception:
            logger.warning(
                "Build dispatch failed for triaged issue task %s", task.id, exc_info=True
            )

    async def _github_call(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        *,
        required: bool = True,
    ) -> Any:
        if self.mcp_manager is None:
            if required:
                raise RuntimeError("GitHub MCP manager is not configured")
            return None

        if hasattr(self.mcp_manager, "call_tool"):
            result = self.mcp_manager.call_tool(
                server_name="github",
                tool_name=tool_name,
                arguments=arguments,
            )
            if inspect.isawaitable(result):
                result = await result
            return _parse_mcp_result(result)

        session = await self.mcp_manager.get_client_session("github")
        result = await session.call_tool(tool_name, arguments)
        return _parse_mcp_result(result)

    def _validate_signature(
        self, secret_ref: str | None, headers: dict[str, str], raw_body: bytes
    ) -> None:
        signature = headers.get("x-hub-signature-256")
        if not signature or not signature.startswith("sha256="):
            raise TriageWebhookError("Missing X-Hub-Signature-256")
        secret = self._resolve_secret(secret_ref)
        expected = "sha256=" + hmac.new(secret.encode(), raw_body, sha256).hexdigest()
        if not hmac.compare_digest(signature, expected):
            raise TriageWebhookError("Invalid GitHub webhook signature")

    def _resolve_secret(self, secret_ref: str | None) -> str:
        if not secret_ref:
            raise TriageWebhookError("GitHub triage webhook_secret_ref is not configured")
        if secret_ref.startswith("$secret:"):
            if self.secret_store is None:
                raise TriageWebhookError("Secret store is not available")
            resolved = str(self.secret_store.resolve(secret_ref))
            if resolved == secret_ref:
                raise TriageWebhookError(f"Webhook secret {secret_ref!r} was not found")
            return resolved
        return secret_ref

    async def _judge(self, issue: IssueSnapshot, duplicates: list[IssueDuplicate]) -> TriageOutcome:
        if duplicates:
            duplicate = duplicates[0]
            return TriageOutcome(
                "dedup",
                f"Similar to {duplicate.issue_key}",
                close_issue=True,
                duplicate=duplicate,
            )
        if "gobby:ignore" in issue.labels:
            return TriageOutcome("skip", "Issue has gobby:ignore label", close_issue=False)
        if self.judge is not None:
            return await self.judge(issue, duplicates)
        return TriageOutcome("implement", "Default triage judgment accepted the issue")

    def _indexer(self) -> GitHubIssueIndexer:
        vector_store = getattr(self.memory_manager, "vector_store", None)
        if (
            not callable(getattr(vector_store, "ensure_collection", None))
            or not callable(getattr(vector_store, "upsert", None))
            or not callable(getattr(vector_store, "search_with_payload", None))
        ):
            vector_store = None

        embed_fn = getattr(self.memory_manager, "embed_fn", None)
        if not callable(embed_fn):
            embed_fn = None
        return GitHubIssueIndexer(vector_store=vector_store, embed_fn=embed_fn)


def create_github_triage_handler(
    *,
    db: DatabaseProtocol,
    mcp_manager: Any | None,
    task_manager: LocalTaskManager,
    memory_manager: Any | None = None,
    secret_store: Any | None = None,
) -> Callable[[CronJob], Awaitable[str]]:
    """Create a cron handler for GitHub issue triage reconciliation."""

    async def _handler(job: CronJob) -> str:
        service = GitHubIssueTriageService(
            db=db,
            mcp_manager=mcp_manager,
            task_manager=task_manager,
            memory_manager=memory_manager,
            secret_store=secret_store,
        )
        result = await service.reconcile_project_repos(job.project_id)
        return (
            "GitHub triage reconciliation completed: "
            f"scanned={result['scanned']} triaged={result['triaged']} errors={result['errors']}"
        )

    return _handler


def _normalize_headers(headers: dict[str, str]) -> dict[str, str]:
    return {str(key).lower(): str(value) for key, value in headers.items()}


def _required_header(headers: dict[str, str], name: str) -> str:
    value = headers.get(name)
    if not value:
        raise TriageWebhookError(f"Missing required GitHub webhook header: {name}")
    return value


def _loads_payload(raw_body: bytes) -> dict[str, Any]:
    try:
        payload = json.loads(raw_body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise TriageWebhookError("Invalid GitHub webhook JSON payload") from exc
    if not isinstance(payload, dict):
        raise TriageWebhookError("GitHub webhook payload must be a JSON object")
    return payload


def _payload_repo(payload: dict[str, Any]) -> str | None:
    repo = payload.get("repository")
    if isinstance(repo, dict) and repo.get("full_name"):
        return str(repo["full_name"])
    return None


def _payload_issue_number(payload: dict[str, Any]) -> int | None:
    issue = payload.get("issue")
    if isinstance(issue, dict) and issue.get("number") is not None:
        return int(issue["number"])
    return None


def _initial_delivery_status(
    event: str, action: str | None
) -> Literal["pending", "processed", "ignored"]:
    if event == "ping":
        return "processed"
    if event == "issues" and action in PROCESSABLE_ISSUE_ACTIONS:
        return "pending"
    return "ignored"


def _parse_mcp_result(result: Any) -> Any:
    if hasattr(result, "content") and result.content:
        for item in result.content:
            if hasattr(item, "text"):
                try:
                    return json.loads(item.text)
                except (json.JSONDecodeError, TypeError):
                    return item.text
    return result


def _task_description(issue: IssueSnapshot) -> str:
    parts = [issue.body.strip()]
    if issue.issue_url:
        parts.append(f"GitHub issue: {issue.issue_url}")
    parts.append(f"Source: {issue.repo}#{issue.issue_number}")
    return "\n\n".join(part for part in parts if part)
