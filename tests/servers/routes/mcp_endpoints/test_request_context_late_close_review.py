"""HTTP identity checks for a successful validator's late verdict submission."""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from gobby.mcp_proxy.tools.tasks import _lifecycle_close_orchestration as orchestration
from gobby.mcp_proxy.tools.tasks._lifecycle_close_preview import CloseEvaluation
from gobby.servers.routes.dependencies import get_server
from gobby.servers.routes.mcp.endpoints import execution, request_context
from gobby.storage.agents import AgentRun, AgentRunStatus
from gobby.storage.task_close_reviews import (
    VALIDATOR_RUN_ENDED_SUCCESS_ERROR,
    TaskCloseReview,
    TaskCloseReviewStatus,
    TaskCloseReviewStore,
)
from gobby.tasks.agentic_close_review import TASK_CLOSE_VALIDATOR_AGENT
from gobby.utils.session_context import (
    AGENT_RUN_ID_HEADER,
    SeededContextTokens,
    SessionContext,
    get_current_agent_run_id,
    get_current_session_id,
    set_session_context,
)

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]
_SUBMIT_PATH = "/api/mcp/gobby-tasks/tools/submit_close_review"


@dataclass
class ReviewClient:
    client: AsyncClient
    run: AgentRun
    review: TaskCloseReview
    manager: MagicMock
    store: MagicMock
    dispatch: AsyncMock
    headers: dict[str, str]


@pytest.fixture
async def review_client(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[ReviewClient]:
    now = datetime.now(UTC)
    run = AgentRun(
        id=str(uuid4()),
        parent_session_id=str(uuid4()),
        child_session_id=str(uuid4()),
        machine_id="test-machine",
        provider="codex",
        prompt="Validate close evidence.",
        agent_name=TASK_CLOSE_VALIDATOR_AGENT,
        status="success",
        created_at=now,
        updated_at=now,
    )
    review = TaskCloseReview(
        id=str(uuid4()),
        task_id=str(uuid4()),
        task_ref="#42",
        caller_session_id=run.parent_session_id,
        agent_run_id=run.id,
        close_arguments={},
        review_fingerprint="review",
        evidence_fingerprint="evidence",
        status="error",
        result_payload=None,
        error=VALIDATOR_RUN_ENDED_SUCCESS_ERROR,
        launched_at=now,
        completed_at=now,
        delivered_at=now,
        created_at=now,
        updated_at=now,
    )
    manager = MagicMock()
    manager.get.return_value = run
    manager.get_by_session.return_value = None
    store = MagicMock()
    store.get.side_effect = lambda review_id: review if review_id == review.id else None
    store.get_active_for_task.return_value = None
    monkeypatch.setattr(request_context, "LocalAgentRunManager", lambda db: manager)
    # Storage is fake; the HTTP route and identity binder remain real.
    monkeypatch.setattr(
        "gobby.storage.task_close_reviews.TaskCloseReviewStore.get",
        lambda self, review_id: store.get(review_id),
    )
    monkeypatch.setattr(
        "gobby.storage.task_close_reviews.TaskCloseReviewStore.get_active_for_task",
        lambda self, task_id: store.get_active_for_task(task_id),
    )
    project_id = str(uuid4())

    async def seed_contexts(**kwargs: Any) -> SeededContextTokens:
        session_id = kwargs["session_ref"]
        return SeededContextTokens(
            resolved_session_id=session_id,
            resolved_project_id=project_id,
            session_token=set_session_context(
                SessionContext(session_id) if session_id is not None else None
            ),
        )

    monkeypatch.setattr(request_context, "resolve_and_seed_contexts", seed_contexts)
    monkeypatch.setattr(execution, "_http_request_scope", lambda *args: project_id)

    async def dispatch(*args: Any, **kwargs: Any) -> dict[str, Any]:
        return {
            "success": True,
            "run_id": get_current_agent_run_id(),
            "session_id": get_current_session_id(),
        }

    server = MagicMock()
    server.run_db = AsyncMock(side_effect=lambda function, *args: function(*args))
    server.tool_proxy.call_tool = AsyncMock(side_effect=dispatch)
    server.config.mcp_client_proxy.tool_timeout = 10.0
    app = FastAPI()
    app.dependency_overrides[get_server] = lambda: server
    app.add_api_route(
        "/api/mcp/{server_name}/tools/{tool_name}", execution.mcp_proxy, methods=["POST"]
    )
    headers = {
        AGENT_RUN_ID_HEADER: run.id,
        "x-gobby-session-id": str(run.child_session_id),
        "x-gobby-project-id": project_id,
    }
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        yield ReviewClient(
            client, run, review, manager, store, server.tool_proxy.call_tool, headers
        )


async def test_successful_bound_validator_can_submit_late_review(
    review_client: ReviewClient,
) -> None:
    case = review_client
    response = await case.client.post(
        _SUBMIT_PATH, headers=case.headers, json={"review_id": case.review.id, "verdict": {}}
    )
    assert response.status_code == 200
    assert case.dispatch.await_count == 1
    assert response.json()["run_id"] == case.run.id
    assert response.json()["session_id"] == case.run.child_session_id
    assert get_current_agent_run_id() is None
    assert get_current_session_id() is None


@pytest.mark.parametrize(
    "failure",
    [
        "other_tool",
        "other_server",
        "missing_review",
        "foreign_review",
        "foreign_run",
        "foreign_child",
        "missing_child",
        "missing_session",
        "foreign_parent",
        "non_validator",
        "task_bound",
        "stale_review",
        "other_error",
        "superseded_review",
        "missing_run",
        "invalid_header",
        "terminal_without_payload",
    ],
)
async def test_late_review_rejects_unrelated_identity_or_operation(
    review_client: ReviewClient, failure: str
) -> None:
    case = review_client
    path = _SUBMIT_PATH
    body: dict[str, Any] = {"review_id": case.review.id, "verdict": {}}
    headers = dict(case.headers)
    if failure == "other_tool":
        path = "/api/mcp/gobby-tasks/tools/close_task"
        body["tool_name"] = "submit_close_review"
    elif failure == "other_server":
        path = "/api/mcp/foreign/tools/submit_close_review"
    elif failure == "missing_review":
        body.pop("review_id")
    elif failure == "foreign_review":
        body["review_id"] = str(uuid4())
    elif failure == "foreign_run":
        case.store.get.side_effect = None
        case.store.get.return_value = replace(case.review, agent_run_id=str(uuid4()))
    elif failure == "foreign_child":
        headers["x-gobby-session-id"] = str(uuid4())
    elif failure == "missing_child":
        case.run.child_session_id = None
    elif failure == "missing_session":
        headers.pop("x-gobby-session-id")
    elif failure == "foreign_parent":
        case.store.get.side_effect = None
        case.store.get.return_value = replace(case.review, caller_session_id=str(uuid4()))
    elif failure == "non_validator":
        case.run.agent_name = "implementer"
    elif failure == "task_bound":
        case.run.task_id = case.review.task_id
    elif failure == "stale_review":
        case.store.get.side_effect = None
        case.store.get.return_value = replace(case.review, status="stale")
    elif failure == "other_error":
        case.store.get.side_effect = None
        case.store.get.return_value = replace(case.review, error="Validator failed.")
    elif failure == "superseded_review":
        case.store.get_active_for_task.return_value = replace(
            case.review, id=str(uuid4()), status="running"
        )
    elif failure == "missing_run":
        case.manager.get.return_value = None
    elif failure == "invalid_header":
        headers[AGENT_RUN_ID_HEADER] = "not-a-uuid"
    elif failure == "terminal_without_payload":
        case.store.get.side_effect = None
        case.store.get.return_value = replace(case.review, status="closed")
    response = await case.client.post(path, headers=headers, json=body)
    assert response.status_code == 403
    case.dispatch.assert_not_awaited()
    assert get_current_agent_run_id() is None
    assert get_current_session_id() is None


@pytest.mark.parametrize("status", ["error", "timeout", "cancelled"])
async def test_failed_run_cannot_use_success_tombstone(
    review_client: ReviewClient, status: AgentRunStatus
) -> None:
    case = review_client
    case.run.status = status
    response = await case.client.post(
        _SUBMIT_PATH, headers=case.headers, json={"review_id": case.review.id}
    )
    assert response.status_code == 403
    case.dispatch.assert_not_awaited()


@pytest.mark.parametrize("status", ["pending", "running"])
async def test_active_runs_retain_ordinary_tool_access(
    review_client: ReviewClient, status: AgentRunStatus
) -> None:
    case = review_client
    case.run.status = status
    response = await case.client.post(
        "/api/mcp/gobby-tasks/tools/get_task", headers=case.headers, json={"task_id": "#42"}
    )
    assert response.status_code == 200
    case.dispatch.assert_awaited_once()
    case.store.get.assert_not_called()


async def test_malformed_late_verdict_can_be_corrected_and_terminal_result_replayed(
    review_client: ReviewClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Run the actual submit/auth handler through HTTP across its state transitions."""
    case = review_client
    case.review = replace(case.review, close_arguments={"task_id": "#42", "reason": "Complete"})
    case.store.get.side_effect = lambda review_id: case.review
    case.store.get_active_for_task.side_effect = lambda task_id: (
        case.review if case.review.active else None
    )
    monkeypatch.setattr(orchestration, "LocalAgentRunManager", lambda db: case.manager)
    claims: list[str] = []

    def claim(store: TaskCloseReviewStore, review_id: str, run_id: str) -> TaskCloseReview | None:
        if case.review.terminal and case.review.status != "error":
            return None
        claims.append(case.review.status)
        case.review = replace(case.review, status="finalizing")
        return case.review

    def restore(store: TaskCloseReviewStore, review_id: str, run_id: str, *, error: str) -> bool:
        case.review = replace(case.review, status="running", error=error)
        return True

    def finish(
        store: TaskCloseReviewStore,
        review_id: str,
        *,
        status: TaskCloseReviewStatus,
        result_payload: dict[str, Any],
        error: str | None,
    ) -> bool:
        case.review = replace(
            case.review, status=status, result_payload=result_payload, error=error
        )
        return True

    monkeypatch.setattr(TaskCloseReviewStore, "claim_finalizing", claim)
    monkeypatch.setattr(TaskCloseReviewStore, "restore_running", restore)
    monkeypatch.setattr(TaskCloseReviewStore, "finish", finish)
    malformed = CloseEvaluation("#42")
    malformed.error = "agentic_review_malformed"
    malformed.message = "Correct the criterion state."
    corrected = CloseEvaluation("#42")
    corrected.error = "validation_failed"
    corrected.validation_status = "invalid"
    corrected.message = "Evidence has a gap."
    evaluate = AsyncMock(side_effect=[malformed, corrected])
    commit = AsyncMock()

    async def submit(server: str, tool: str, arguments: dict[str, Any], **kwargs: Any) -> Any:
        return await orchestration.submit_close_review(
            MagicMock(),
            review_id=arguments["review_id"],
            verdict=arguments["verdict"],
            evaluate_close=evaluate,
            commit_close=commit,
        )

    case.dispatch.side_effect = submit
    body = {"review_id": case.review.id, "verdict": {"status": "invalid"}}
    first = await case.client.post(_SUBMIT_PATH, headers=case.headers, json=body)
    assert first.status_code == 200
    assert first.json()["error"] == "agentic_review_malformed"
    restored = case.review
    assert restored.status == "running"
    second = await case.client.post(_SUBMIT_PATH, headers=case.headers, json=body)
    assert second.status_code == 200
    assert case.review.status == "invalid"
    persisted_payload = case.review.result_payload
    assert persisted_payload is not None
    replay = await case.client.post(_SUBMIT_PATH, headers=case.headers, json=body)
    assert replay.status_code == 200
    assert replay.json()["terminal_payload"] == persisted_payload
    assert case.review.result_payload is persisted_payload
    assert claims == ["error", "running"]
    assert evaluate.await_count == 2
    commit.assert_not_awaited()

    # No header must not synthesize an ended validator's identity from its session.
    headers = dict(case.headers)
    headers.pop(AGENT_RUN_ID_HEADER)
    unbound = await case.client.post(_SUBMIT_PATH, headers=headers, json=body)
    assert unbound.status_code == 200
    assert unbound.json()["error"] == "agentic_review_unauthorized"
    assert evaluate.await_count == 2


@pytest.mark.parametrize("status", ["finalizing", "closed", "external_pending"])
async def test_bound_late_review_states_allow_only_retrieval_or_existing_work(
    review_client: ReviewClient, status: TaskCloseReviewStatus
) -> None:
    case = review_client
    review = replace(case.review, status=status, result_payload={"status": status})
    case.store.get.side_effect = None
    case.store.get.return_value = review
    case.store.get_active_for_task.return_value = review if review.active else None
    response = await case.client.post(
        _SUBMIT_PATH, headers=case.headers, json={"review_id": case.review.id}
    )
    assert response.status_code == 200
    case.dispatch.assert_awaited_once()
