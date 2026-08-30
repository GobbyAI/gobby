"""Webhook-first GitHub issue triage service."""

from __future__ import annotations

import asyncio
import hmac
import json
import logging
import math
import time
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass, field
from hashlib import sha256
from typing import TYPE_CHECKING, Any, Literal, Protocol, TypedDict

import psycopg

from gobby.build import BuildOptions, build
from gobby.github_triage.delivery import (
    PROCESSABLE_ISSUE_ACTIONS,
    DeliveryProcessor,
    TransientDeliveryError,
    payload_issue_number,
    payload_repo,
)
from gobby.github_triage.issue_index import (
    GitHubIssueIndexer,
    IssueDuplicate,
    IssueSnapshot,
    build_issue_content,
    content_hash,
)
from gobby.github_triage.task_description import build_task_description
from gobby.integrations.github_helper import parse_github_repo
from gobby.storage.github_triage import (
    GitHubTriageStore,
    TriageVerdict,
)
from gobby.storage.hub.protocol import GitHubIssueTriageMutation
from gobby.storage.projects import LocalProjectManager
from gobby.storage.tasks import LocalTaskManager, Task
from gobby.sync.github_issue_sync import GitHubIssueDeliveryHandler

if TYPE_CHECKING:
    from gobby.storage.hub.protocol import HubDatabase

logger = logging.getLogger(__name__)

TRIAGE_ACCEPTED_LABEL = "gobby:accepted"
TRIAGE_SKIPPED_LABEL = "gobby:skipped"
TRIAGE_DUPLICATE_LABEL = "gobby:duplicate"
TRIAGE_ESCALATED_LABEL = "gobby:needs-triage"
TRIAGE_RESOLVED_LABEL = "gobby:resolved"
AUTO_CLOSE_DUPLICATE_SCORE = 0.97

BuildFunc = Callable[[str, BuildOptions], Awaitable[Any]]


class TriageError(ValueError): ...


class TriageWebhookError(TriageError):
    """Webhook validation failed."""


class WebhookAuthenticationError(TriageWebhookError):
    """Webhook authentication failed without exposing project state."""

    detail = "GitHub webhook authentication failed"

    def __init__(self) -> None:
        super().__init__(self.detail)


class TriageDisabledError(TriageError):
    """Triage is disabled for the project."""


class GitHubMCPError(RuntimeError):
    """Safe typed failure returned by the GitHub MCP server."""

    def __init__(
        self,
        *,
        tool_name: str | None = None,
        status_code: int | None = None,
        retry_after_seconds: float | None = None,
        rate_limit_remaining: int | None = None,
        rate_limit_reset: float | None = None,
    ) -> None:
        self.tool_name = tool_name
        self.status_code = status_code
        self.retry_after_seconds = retry_after_seconds
        self.rate_limit_remaining = rate_limit_remaining
        self.rate_limit_reset = rate_limit_reset
        tool = f" tool {tool_name}" if tool_name else ""
        status = f" (status={status_code})" if status_code is not None else ""
        super().__init__(f"GitHub MCP{tool} failed{status}")

    @property
    def rate_limit_metadata(self) -> dict[str, int | float]:
        """Return only the allowlisted rate-limit fields safe for logs and metrics."""
        metadata: dict[str, int | float] = {}
        if self.status_code is not None:
            metadata["status_code"] = self.status_code
        if self.retry_after_seconds is not None:
            metadata["retry_after_seconds"] = self.retry_after_seconds
        if self.rate_limit_remaining is not None:
            metadata["rate_limit_remaining"] = self.rate_limit_remaining
        if self.rate_limit_reset is not None:
            metadata["rate_limit_reset"] = self.rate_limit_reset
        return metadata

    @property
    def is_rate_limited(self) -> bool:
        return bool(
            self.status_code == 429
            or self.retry_after_seconds is not None
            or self.rate_limit_reset is not None
            or (self.status_code == 403 and self.rate_limit_remaining == 0)
        )

    def retry_delay(self, *, now: float, maximum: float) -> float:
        """Choose a server-provided delay, bounded for cron and test safety."""
        if self.retry_after_seconds is not None:
            delay = self.retry_after_seconds
        elif self.rate_limit_reset is not None:
            delay = max(0.0, self.rate_limit_reset - now)
        else:
            delay = 1.0
        return min(maximum, max(0.0, delay))


class _RateLimitMetadata(TypedDict):
    status_code: int | None
    retry_after_seconds: float | None
    rate_limit_remaining: int | None
    rate_limit_reset: float | None


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


@dataclass(frozen=True)
class _Judgment:
    outcome: TriageOutcome
    build_approved: bool = False


class GitHubIssueTriageService:
    """Coordinates GitHub issue intake, dedup, task creation, and GitHub updates."""

    def __init__(
        self,
        *,
        db: HubDatabase,
        mcp_manager: Any | None = None,
        task_manager: LocalTaskManager | None = None,
        project_manager: LocalProjectManager | None = None,
        memory_manager: Any | None = None,
        secret_store: Any | None = None,
        judge: TriageJudge | None = None,
        build_func: BuildFunc | None = None,
        sleep_func: Callable[[float], Awaitable[None]] = asyncio.sleep,
        time_func: Callable[[], float] = time.time,
        max_rate_limit_delay: float = 60.0,
    ) -> None:
        if max_rate_limit_delay < 0 or not math.isfinite(max_rate_limit_delay):
            raise ValueError("max_rate_limit_delay must be a finite non-negative number")
        self.db = db
        self.mcp_manager = mcp_manager
        self.task_manager = task_manager or LocalTaskManager(db)
        self.project_manager = project_manager or LocalProjectManager(db)
        self.memory_manager = memory_manager
        self.secret_store = secret_store
        self.judge = judge
        self.store = GitHubTriageStore(db)
        self._build_func = build_func
        self._sleep_func = sleep_func
        self._time_func = time_func
        self._max_rate_limit_delay = max_rate_limit_delay

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
        project = self.project_manager.get(project_id)
        if not project or project.deleted_at:
            try:
                self._validate_signature(_UNVERIFIABLE_HMAC_KEY, normalized_headers, raw_body)
            except TriageWebhookError:
                pass
            raise WebhookAuthenticationError

        config = self.store.get_config(project_id, fallback_repo=project.github_repo)
        try:
            self._validate_signature(config.webhook_secret_ref, normalized_headers, raw_body)
        except TriageWebhookError:
            raise WebhookAuthenticationError from None
        if not (config.sync_enabled or config.triage_enabled) or not config.webhook_enabled:
            raise WebhookAuthenticationError

        event = _required_header(normalized_headers, "x-github-event")
        delivery_id = _required_header(normalized_headers, "x-github-delivery")
        payload = _loads_payload(raw_body)
        action = payload.get("action")
        repo = payload_repo(payload)
        issue_number = payload_issue_number(payload)
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
        return await self._delivery_processor().process(project_id, delivery_id)

    async def recover_deliveries(self, project_id: str) -> dict[str, int]:
        """Recover due retries and expired processing leases for one project."""
        return await self._delivery_processor().recover(project_id)

    def _delivery_processor(self) -> DeliveryProcessor:
        return DeliveryProcessor(self.store, GitHubIssueDeliveryHandler(self), TriageWebhookError)

    async def reconcile_project_repos(self, project_id: str) -> dict[str, int]:
        """Reconcile all configured repositories as a webhook recovery path."""
        project = self.project_manager.get(project_id)
        if not project or project.deleted_at:
            raise ValueError(f"Unknown project: {project_id}")
        config = self.store.get_config(project_id, fallback_repo=project.github_repo)
        if not config.triage_enabled:
            return {"scanned": 0, "triaged": 0, "errors": 0}

        scanned = triaged = errors = 0
        for repo in config.repositories_with_fallback(project.github_repo):
            owner, repo_name = parse_github_repo(repo)
            page = 1
            while True:
                try:
                    issues = await self._github_call(
                        "list_issues",
                        {
                            "owner": owner,
                            "repo": repo_name,
                            "state": "open",
                            "per_page": 100,
                            "page": page,
                        },
                        project_id=project_id,
                    )
                except Exception as exc:
                    logger.warning(
                        "Failed to list GitHub issues for %s page %s (%s)",
                        repo,
                        page,
                        type(exc).__name__,
                    )
                    errors += 1
                    break
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
                    except Exception as exc:
                        logger.warning(
                            "Failed to reconcile GitHub issue %s#%s (%s)",
                            repo,
                            issue_number,
                            type(exc).__name__,
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
        """Serialize one issue across webhook and reconciliation processors."""
        lock = GitHubIssueTriageMutation(
            project_id=project_id,
            repo=repo,
            issue_number=issue_number,
        )
        async with self.db.advisory_lock(lock):
            return await self._triage_issue_locked(
                project_id, repo, issue_number, source, issue_data=issue_data
            )

    async def _triage_issue_locked(
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
        if not config.triage_enabled:
            raise TriageDisabledError("GitHub issue triage is disabled")
        issue_data = issue_data or await self._fetch_issue(
            repo, issue_number, project_id=project_id
        )
        if issue_data.get("pull_request"):
            outcome = TriageOutcome("skip", "GitHub pull requests are not triaged as issues")
            issue = IssueSnapshot.from_github(project_id=project_id, repo=repo, issue=issue_data)
            return await self.apply_triage_outcome(project_id, issue, outcome, source)

        issue = IssueSnapshot.from_github(project_id=project_id, repo=repo, issue=issue_data)
        existing = self.store.get_issue_record(project_id, repo, issue_number)
        current_hash = content_hash(issue)
        existing_task_id = existing.task_id if existing else None
        if existing_task_id is None:
            linked_task = self.db.fetchone(
                "SELECT id FROM tasks WHERE project_id = %s AND github_repo = %s "
                "AND github_issue_number = %s LIMIT 1",
                (project_id, issue.repo, issue.issue_number),
            )
            existing_task_id = str(linked_task["id"]) if linked_task else None
        indexer = self._indexer()
        duplicates = await indexer.find_duplicates(issue)
        judgment = await self._judge(issue, duplicates)
        outcome = judgment.outcome
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
        build_dispatched = self.store.has_build_dispatch(project_id, repo, issue_number)
        if outcome.verdict == "implement" and judgment.build_approved and not build_dispatched:
            task = self._create_or_update_task(project_id, issue)
            await self._run_build(task)
            self.store.record_build_dispatch(project_id, repo, issue_number, task.id)
        result = await self.apply_triage_outcome(
            project_id,
            issue,
            outcome,
            source,
            build_approved=judgment.build_approved,
            dispatch_build=False,
            defer_comment=True,
        )
        deferred_comment = result.pop("_deferred_comment", None)
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
            source_text=build_issue_content(issue),
        )
        if isinstance(deferred_comment, str):
            try:
                await self._comment(issue, deferred_comment)
            except BaseException:
                self.store.rollback_issue_record(
                    project_id,
                    repo,
                    issue_number,
                    content_hash=current_hash,
                    previous=existing,
                )
                raise
        result["content_hash"] = current_hash
        result["vector_point_id"] = point_id
        return result

    async def apply_triage_outcome(
        self,
        project_id: str,
        issue: IssueSnapshot,
        outcome: TriageOutcome,
        source: str,
        *,
        build_approved: bool = False,
        dispatch_build: bool = True,
        defer_comment: bool = False,
    ) -> dict[str, Any]:
        """Apply deterministic side effects for a triage outcome."""
        if outcome.verdict == "implement" and not build_approved:
            outcome = TriageOutcome(
                "escalate",
                "Implementation requires explicit approval from a configured triage judge",
            )
        task: Task | None = None
        if outcome.verdict == "implement":
            task = self._create_or_update_task(project_id, issue)
            if dispatch_build:
                await self._run_build(task)
            comment = (
                outcome.comment or f"Accepted for implementation as Gobby task #{task.seq_num}."
            )
            await self._apply_labels(issue, [TRIAGE_ACCEPTED_LABEL, *outcome.labels])
        elif outcome.verdict == "dedup":
            duplicate = outcome.duplicate
            duplicate_text = (
                f"Duplicate of {duplicate.issue_key}" if duplicate else "Duplicate issue"
            )
            comment = outcome.comment or duplicate_text
            await self._apply_labels(issue, [TRIAGE_DUPLICATE_LABEL, *outcome.labels])
            if outcome.close_issue:
                await self._close_issue(issue)
        elif outcome.verdict == "skip":
            comment = outcome.comment or f"Skipped by Gobby triage: {outcome.reason}"
            await self._apply_labels(issue, [TRIAGE_SKIPPED_LABEL, *outcome.labels])
            if outcome.close_issue:
                await self._close_issue(issue)
        else:
            comment = outcome.comment or f"Gobby needs human triage: {outcome.reason}"
            await self._apply_labels(issue, [TRIAGE_ESCALATED_LABEL, *outcome.labels])

        result: dict[str, Any] = {
            "project_id": project_id,
            "repo": issue.repo,
            "issue_number": issue.issue_number,
            "source": source,
            "verdict": outcome.verdict,
            "task_id": task.id if task else None,
        }
        if defer_comment:
            result["_deferred_comment"] = comment
        else:
            await self._comment(issue, comment)
        return result

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
        await self._apply_labels(issue, [TRIAGE_RESOLVED_LABEL])
        await self._close_issue(issue)
        await self._comment(issue, f"Resolved by merged Gobby task #{task.seq_num}{suffix}.")
        return True

    def _create_or_update_task(self, project_id: str, issue: IssueSnapshot) -> Task:
        description = build_task_description(issue)
        title = f"Implement externally reported GitHub issue {issue.repo}#{issue.issue_number}"
        labels = sorted(set(issue.labels) | {"github"})
        validation_criteria = (
            f"GitHub issue {issue.repo}#{issue.issue_number} is implemented and linked."
        )
        existing = self.db.fetchone(
            "SELECT id FROM tasks WHERE project_id = %s AND github_repo = %s "
            "AND github_issue_number = %s LIMIT 1",
            (project_id, issue.repo, issue.issue_number),
        )
        if existing:
            return self.task_manager.update_task(
                existing["id"],
                title=title,
                description=description,
                labels=labels,
                validation_criteria=validation_criteria,
            )
        try:
            return self.task_manager.create_task(
                project_id=project_id,
                title=title,
                description=description,
                labels=labels,
                category="code",
                task_type="feature",
                validation_criteria=validation_criteria,
                github_issue_number=issue.issue_number,
                github_repo=issue.repo,
            )
        except psycopg.IntegrityError:
            existing = self.db.fetchone(
                "SELECT id FROM tasks WHERE project_id = %s AND github_repo = %s "
                "AND github_issue_number = %s LIMIT 1",
                (project_id, issue.repo, issue.issue_number),
            )
            if existing is None:
                raise
            return self.task_manager.update_task(
                existing["id"],
                title=title,
                description=description,
                labels=labels,
                validation_criteria=validation_criteria,
            )

    async def _fetch_issue(
        self, repo: str, issue_number: int, *, project_id: str
    ) -> dict[str, Any]:
        owner, repo_name = parse_github_repo(repo)
        result = await self._github_call(
            "get_issue",
            {"owner": owner, "repo": repo_name, "issue_number": issue_number},
            project_id=project_id,
        )
        if not isinstance(result, dict):
            raise RuntimeError(f"GitHub get_issue returned {type(result).__name__}")
        return result

    async def _apply_labels(self, issue: IssueSnapshot, labels: list[str]) -> None:
        owner, repo_name = parse_github_repo(issue.repo)
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
                project_id=issue.project_id,
            )

    async def _comment(self, issue: IssueSnapshot, comment: str) -> None:
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
            project_id=issue.project_id,
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
            project_id=issue.project_id,
        )

    async def _run_build(self, task: Task) -> None:
        options = BuildOptions(
            skip_stages=[],
            isolation="worktree",
            isolation_explicit=True,
        )
        build_func = self._build_func
        if build_func is None:

            async def _default_build(input_ref: str, opts: BuildOptions) -> Any:
                return await build(input_ref, opts, db=self.db, project_id=task.project_id)

            build_func = _default_build
        try:
            await build_func(f"#{task.seq_num}", options)
        except Exception as exc:
            logger.warning(
                "Build dispatch failed for triaged issue task %s (%s)",
                task.id,
                type(exc).__name__,
            )
            raise TransientDeliveryError("GitHub triage build dispatch failed") from None

    async def _github_call(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        *,
        required: bool = True,
        project_id: str | None = None,
    ) -> Any:
        from gobby.github_triage.mcp_call import github_call
        from gobby.mcp_proxy.services.server_resolution import as_project_id, resolved_server_id

        if self.mcp_manager is None:
            if required:
                raise RuntimeError("GitHub MCP manager is not configured")
            return None
        scope = as_project_id(
            project_id,
            default=as_project_id(getattr(self.mcp_manager, "project_id", None)),
        )
        server_id = resolved_server_id(self.mcp_manager, "github", project_id=scope)
        if server_id is None:
            if required:
                raise RuntimeError(f"GitHub MCP server not found in project {scope}")
            return None
        return await github_call(
            self.mcp_manager,
            server_id,
            tool_name,
            arguments,
            required=required,
            time_func=self._time_func,
            sleep_func=self._sleep_func,
            max_rate_limit_delay=self._max_rate_limit_delay,
        )

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
            resolved = self.secret_store.resolve(secret_ref)
            if not isinstance(resolved, str) or not resolved or resolved == secret_ref:
                raise TriageWebhookError("Webhook secret could not be resolved")
            return resolved
        return secret_ref

    async def _judge(self, issue: IssueSnapshot, duplicates: list[IssueDuplicate]) -> _Judgment:
        if duplicates:
            duplicate = duplicates[0]
            if duplicate.score < AUTO_CLOSE_DUPLICATE_SCORE:
                return _Judgment(
                    TriageOutcome(
                        "escalate",
                        f"Potential duplicate of {duplicate.issue_key} "
                        f"(similarity {duplicate.score:.2f})",
                        close_issue=False,
                        duplicate=duplicate,
                    )
                )
            return _Judgment(
                TriageOutcome(
                    "dedup",
                    f"Similar to {duplicate.issue_key}",
                    close_issue=True,
                    duplicate=duplicate,
                )
            )
        if "gobby:ignore" in issue.labels:
            return _Judgment(
                TriageOutcome("skip", "Issue has gobby:ignore label", close_issue=False)
            )
        if self.judge is None:
            return _Judgment(
                TriageOutcome("escalate", "No triage judge is configured; human review required")
            )
        try:
            outcome = await self.judge(issue, duplicates)
        except Exception as exc:
            logger.warning("GitHub triage judge failed; escalating (%s)", type(exc).__name__)
            return _Judgment(
                TriageOutcome("escalate", "Triage judge failed; human review required")
            )
        if (
            not isinstance(outcome, TriageOutcome)
            or outcome.verdict not in {"implement", "dedup", "skip", "escalate"}
            or not isinstance(outcome.reason, str)
            or (outcome.comment is not None and not isinstance(outcome.comment, str))
            or not isinstance(outcome.labels, tuple)
            or not all(isinstance(label, str) for label in outcome.labels)
            or not isinstance(outcome.close_issue, bool)
            or (outcome.duplicate is not None and not isinstance(outcome.duplicate, IssueDuplicate))
            or not isinstance(outcome.metadata, dict)
        ):
            logger.warning(
                "GitHub triage judge returned a malformed decision; escalating (%s)",
                type(outcome).__name__,
            )
            return _Judgment(
                TriageOutcome(
                    "escalate", "Triage judge response was invalid; human review required"
                )
            )
        return _Judgment(outcome, build_approved=outcome.verdict == "implement")

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


_UNVERIFIABLE_HMAC_KEY = sha256(b"gobby webhook authentication sentinel").hexdigest()


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


def _initial_delivery_status(
    event: str, action: str | None
) -> Literal["pending", "processed", "ignored"]:
    if event == "ping":
        return "processed"
    if event == "issues" and action in PROCESSABLE_ISSUE_ACTIONS:
        return "pending"
    return "ignored"


def _parse_mcp_result(result: Any, *, tool_name: str | None = None) -> Any:
    payload = _mcp_result_payload(result)
    if bool(_mcp_field(result, "isError", "is_error")):
        metadata = _safe_rate_limit_metadata(payload)
        raise GitHubMCPError(
            tool_name=tool_name,
            status_code=metadata["status_code"],
            retry_after_seconds=metadata["retry_after_seconds"],
            rate_limit_remaining=metadata["rate_limit_remaining"],
            rate_limit_reset=metadata["rate_limit_reset"],
        )
    return payload


def _mcp_result_payload(result: Any) -> Any:
    structured = _mcp_field(result, "structuredContent", "structured_content")
    if structured is not None:
        return structured
    content = _mcp_field(result, "content")
    if isinstance(content, list):
        for item in content:
            text = _mcp_field(item, "text")
            if isinstance(text, str):
                try:
                    return json.loads(text)
                except json.JSONDecodeError:
                    return text
    return result


def _mcp_field(value: Any, *names: str) -> Any:
    for name in names:
        if isinstance(value, dict) and name in value:
            return value[name]
        if hasattr(value, name):
            return getattr(value, name)
    return None


def _safe_rate_limit_metadata(payload: Any) -> _RateLimitMetadata:
    values: dict[str, Any] = {}
    for mapping in _nested_mappings(payload):
        for key, value in mapping.items():
            normalized = str(key).strip().lower().replace("_", "-")
            values.setdefault(normalized, value)

    status = _first_number(values, "status", "status-code", "statuscode", "http-status")
    retry_after = _first_number(values, "retry-after", "retryafter")
    remaining = _first_number(
        values,
        "x-ratelimit-remaining",
        "x-rate-limit-remaining",
        "rate-limit-remaining",
    )
    reset = _first_number(
        values,
        "x-ratelimit-reset",
        "x-rate-limit-reset",
        "rate-limit-reset",
    )
    status_code = (
        int(status) if status is not None and status.is_integer() and 100 <= status <= 599 else None
    )
    rate_limit_remaining = (
        int(remaining) if remaining is not None and remaining.is_integer() else None
    )
    return {
        "status_code": status_code,
        "retry_after_seconds": retry_after,
        "rate_limit_remaining": rate_limit_remaining,
        "rate_limit_reset": reset,
    }


def _nested_mappings(value: Any, *, depth: int = 0) -> list[dict[Any, Any]]:
    if depth > 4:
        return []
    if isinstance(value, dict):
        mappings = [value]
        for nested in value.values():
            mappings.extend(_nested_mappings(nested, depth=depth + 1))
        return mappings
    if isinstance(value, list):
        mappings = []
        for nested in value:
            mappings.extend(_nested_mappings(nested, depth=depth + 1))
        return mappings
    return []


def _first_number(values: dict[str, Any], *names: str) -> float | None:
    for name in names:
        value = values.get(name)
        if isinstance(value, bool) or value is None:
            continue
        try:
            number = float(value)
        except (TypeError, ValueError):
            continue
        if math.isfinite(number) and number >= 0:
            return number
    return None
