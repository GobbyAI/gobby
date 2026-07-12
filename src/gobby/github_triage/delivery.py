"""Durable GitHub webhook delivery leases and retry transitions."""

from __future__ import annotations

import json
import logging
from typing import Any, Protocol

from gobby.storage.github_triage import GitHubTriageStore

logger = logging.getLogger(__name__)

PROCESSABLE_ISSUE_ACTIONS = frozenset({"opened", "edited", "reopened"})
DELIVERY_LEASE_SECONDS = 900
DELIVERY_MAX_ATTEMPTS = 3
DELIVERY_MAX_RETRY_SECONDS = 60.0


class TransientDeliveryError(RuntimeError):
    """A delivery failure that is safe to retry within the attempt bound."""


class TriageIssueCallback(Protocol):
    async def __call__(
        self,
        project_id: str,
        repo: str,
        issue_number: int,
        source: str,
        *,
        issue_data: dict[str, Any] | None = None,
    ) -> dict[str, Any]: ...


class DeliveryProcessor:
    """Own the durable delivery state machine.

    Cancellation deliberately leaves a claimed row in ``processing``. The lease
    timeout makes that state recoverable without racing a processor still alive.
    """

    def __init__(
        self,
        store: GitHubTriageStore,
        triage_issue: TriageIssueCallback,
        terminal_error_type: type[Exception] = ValueError,
    ) -> None:
        self.store = store
        self.triage_issue = triage_issue
        self.terminal_error_type = terminal_error_type

    async def process(self, project_id: str, delivery_id: str) -> dict[str, Any]:
        delivery = self.store.get_delivery(project_id, delivery_id)
        if delivery is None:
            raise self.terminal_error_type(f"Unknown GitHub delivery: {delivery_id}")
        if delivery.status in {"processed", "ignored", "duplicate", "error"}:
            return {"status": delivery.status}

        claimed = self.store.claim_delivery_for_processing(
            project_id,
            delivery_id,
            lease_timeout_seconds=DELIVERY_LEASE_SECONDS,
            max_attempts=DELIVERY_MAX_ATTEMPTS,
        )
        if claimed is None:
            current = self.store.get_delivery(project_id, delivery_id)
            return {"status": current.status if current else "missing"}

        try:
            payload = _load_payload(claimed.raw_body)
            repo = payload_repo(payload)
            issue_number = payload_issue_number(payload)
            if claimed.event != "issues" or claimed.action not in PROCESSABLE_ISSUE_ACTIONS:
                self.store.update_delivery_status(
                    project_id, delivery_id, "ignored", processed=True
                )
                return {"status": "ignored"}
            if repo is None or issue_number is None:
                raise self.terminal_error_type("Issue webhook missing repository or issue number")
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
            if _is_transient(exc) and claimed.attempt_count < DELIVERY_MAX_ATTEMPTS:
                self.store.update_delivery_status(
                    project_id,
                    delivery_id,
                    "pending",
                    error=type(exc).__name__,
                    retry_after_seconds=_retry_delay(claimed.attempt_count),
                )
                return {"status": "retry", "attempt_count": claimed.attempt_count}
            self.store.update_delivery_status(
                project_id,
                delivery_id,
                "error",
                error=type(exc).__name__,
                processed=True,
            )
            raise

    async def recover(self, project_id: str) -> dict[str, int]:
        recovered = retried = errors = 0
        delivery_ids = self.store.list_recoverable_delivery_ids(
            project_id,
            lease_timeout_seconds=DELIVERY_LEASE_SECONDS,
            max_attempts=DELIVERY_MAX_ATTEMPTS,
        )
        for delivery_id in delivery_ids:
            try:
                result = await self.process(project_id, delivery_id)
                if result.get("status") == "retry":
                    retried += 1
                else:
                    recovered += 1
            except Exception as exc:
                logger.warning(
                    "GitHub delivery recovery failed for project %s (%s)",
                    project_id,
                    type(exc).__name__,
                )
                errors += 1
        return {"recovered": recovered, "retried": retried, "errors": errors}


def payload_repo(payload: dict[str, Any]) -> str | None:
    repository = payload.get("repository")
    value = repository.get("full_name") if isinstance(repository, dict) else None
    return str(value) if value else None


def payload_issue_number(payload: dict[str, Any]) -> int | None:
    issue = payload.get("issue")
    value = issue.get("number") if isinstance(issue, dict) else None
    return int(value) if isinstance(value, int | str) and str(value).isdigit() else None


def _load_payload(raw_body: str) -> dict[str, Any]:
    payload = json.loads(raw_body)
    if not isinstance(payload, dict):
        raise ValueError("GitHub webhook payload must be a JSON object")
    return payload


def _is_transient(exc: Exception) -> bool:
    if isinstance(exc, (TransientDeliveryError, TimeoutError, ConnectionError)):
        return True
    status_code = getattr(exc, "status_code", None)
    return bool(
        getattr(exc, "is_rate_limited", False)
        or (isinstance(status_code, int) and 500 <= status_code <= 599)
    )


def _retry_delay(attempt_count: int) -> float:
    return min(DELIVERY_MAX_RETRY_SECONDS, float(2 ** max(0, attempt_count - 1)))
