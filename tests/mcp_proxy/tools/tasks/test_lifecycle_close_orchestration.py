"""Automated task-close review orchestration tests."""

from __future__ import annotations

import asyncio
import json
import os
import pty
import select
import subprocess
import time
from collections.abc import AsyncIterator, Callable, Mapping
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
import uvicorn
from fastapi import FastAPI

import gobby.mcp_proxy.tools.tasks._lifecycle_close_orchestration as orchestration
import gobby.mcp_proxy.tools.tasks._lifecycle_close_tool as close_tool
from gobby.config.tasks import TaskValidationConfig
from gobby.mcp_proxy.tools.internal import InternalRegistryManager, InternalToolRegistry
from gobby.mcp_proxy.tools.tasks._context import RegistryContext
from gobby.mcp_proxy.tools.tasks._factory import create_task_registry
from gobby.mcp_proxy.tools.tasks._lifecycle_close_orchestration import (
    launch_close_review,
    submit_close_review,
)
from gobby.mcp_proxy.tools.tasks._lifecycle_close_preview import CloseEvaluation
from gobby.mcp_proxy.tools.tasks._lifecycle_review_gate import SubmittedCloseReview
from gobby.servers.routes.dependencies import get_metrics_manager, get_server
from gobby.servers.routes.mcp.tools import create_mcp_router
from gobby.sessions.transcript_reader import TranscriptReader
from gobby.storage.agents import LocalAgentRunManager
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.sessions import SessionManager
from gobby.storage.task_close_reviews import (
    REVIEWER_RUN_ENDED_SUCCESS_ERROR,
    QueuedAgentRunSpec,
    TaskCloseReview,
    TaskCloseReviewStatus,
    TaskCloseReviewStore,
)
from gobby.storage.tasks import LocalTaskManager, Task
from gobby.tasks import agentic_close_review as agentic_close_review_module
from gobby.tasks.close_review_delivery import terminal_review_delivery
from gobby.utils.machine_id import require_machine_id
from tests._timing import wait_for_awaitable_or_background_task

pytestmark = pytest.mark.unit

_PERSISTED_SESSION_ID = "00000000-0000-4000-8000-000000002609"
_FIRST_REVIEW_RUN_ID = "00000000-0000-4000-8000-000000002610"
_SECOND_REVIEW_RUN_ID = "00000000-0000-4000-8000-000000002611"
_REQUIRED_EVIDENCE = "Run the real close adapter and capture its MCP response receipt."
_OVERSIZED_TRANSCRIPT_BYTES = 10 * 1024 * 1024
_SUBMIT_DEADLINE_SECONDS = 10.0
_STDIO_DEFAULT_PREFLIGHT_PATH = "/api/health"


@pytest.mark.asyncio
async def test_close_persists_and_launches_one_taskless_reviewer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _Store(_review(status="queued", run_id=None))
    registry = SimpleNamespace(
        call=AsyncMock(return_value={"success": True, "run_id": _FIRST_REVIEW_RUN_ID})
    )
    ctx = _ctx(
        registry=registry,
        validation_config=TaskValidationConfig(
            candidates=["codex/gpt-5.6-terra"],
            close_review_total_timeout_seconds=17,
            close_review_validator_timeout_seconds=900,
        ),
    )
    _patch_store(monkeypatch, store)
    evaluation = _evaluation()
    evaluation.extra["coordinator_owned_pending"] = True
    arguments = _arguments()

    result = await launch_close_review(
        ctx,
        evaluation=evaluation,
        close_arguments=arguments,
        evaluate_close=_revalidate(evaluation),
    )

    assert store.created_arguments == {
        **arguments,
        "_review_timeout_seconds": 900,
        "_criterion_count": 3,
        "_manifest_count": None,
        "_excerpt_chars": None,
        "_review_provider": "codex",
        "_review_model": "gpt-5.6-terra",
    }
    assert "_review_deadline_at" not in arguments
    assert evaluation.task is not None
    assert store.expected_task_updated_at == evaluation.task.updated_at
    registry.call.assert_awaited_once()
    launch_args = registry.call.call_args.args[1]
    assert launch_args["agent"] == "task-close-reviewer"
    assert launch_args["task_id"] is None
    assert launch_args["isolation"] == "none"
    from gobby.mcp_proxy.tools.spawn_agent import create_spawn_agent_registry

    # Validate the real public tool boundary, not just the mocked launch result.
    create_spawn_agent_registry(MagicMock())._prepare_call("spawn_agent", launch_args)
    assert launch_args["project_path"] == evaluation.repo_path
    assert launch_args["provider"] == "codex"
    assert launch_args["model"] == "gpt-5.6-terra"
    # An unpinned candidate inherits the profile default, which is always `auto`.
    assert launch_args["reasoning_effort"] == "auto"
    assert launch_args["timeout"] == 900
    assert "close caller is a spawned agent" in launch_args["prompt"]
    assert "state `pending_external`" in launch_args["prompt"]
    assert result["success"] is True
    assert result["closed"] is False
    assert result["can_close"] is False
    assert result["error"] == "close_review_required"
    assert result["reviewer_run_id"] == _FIRST_REVIEW_RUN_ID
    assert result["review_status"] == "running"
    assert "run_id" not in result
    assert "spawn_request" not in result
    assert "review_run_id" not in result
    assert "Do not poll agent runs or re-call close_task." in result["message"]
    assert "Oversized" not in result["message"]
    assert result["close_review_duration_ms"] == 4.25
    # The reviewer judges the normalized criteria, so the launch names them.
    assert result["criterion_count"] == 3
    assert result["criterion_indexes"] == [1, 2, 3]
    assert "criterion_count=3" in launch_args["prompt"]
    assert "criterion_indexes=[1, 2, 3]" in launch_args["prompt"]


@pytest.mark.asyncio
async def test_launch_reviews_again_after_summary_only_change(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = {
        "event": "task_close_review_completed",
        "review_id": "rejected-review",
        "status": "invalid",
        "closed": False,
        "message": "The deterministic close evidence was rejected.",
    }
    rejected = replace(
        _review(status="invalid", run_id=_FIRST_REVIEW_RUN_ID),
        id="rejected-review",
        review_fingerprint="summary-before-rewording",
        result_payload=payload,
        completed_at=datetime(2026, 8, 22, 1, tzinfo=UTC),
        delivered_at=datetime(2026, 8, 22, 2, tzinfo=UTC),
    )
    store = _Store(
        _review(status="queued", run_id=None),
        reusable_rejection=rejected,
    )
    registry = SimpleNamespace(
        call=AsyncMock(return_value={"success": True, "run_id": _FIRST_REVIEW_RUN_ID})
    )
    _patch_store(monkeypatch, store)
    evaluation = _evaluation()
    evaluation.extra["review_fingerprint"] = "summary-after-rewording"
    arguments = {**_arguments(), "changes_summary": "Same work, reworded."}

    result = await launch_close_review(
        _ctx(registry=registry),
        evaluation=evaluation,
        close_arguments=arguments,
        evaluate_close=_revalidate(evaluation),
    )

    assert result["error"] == "close_review_required"
    assert store.reuse_lookup == ("task", "summary-after-rewording")
    assert store.created_arguments is not None
    registry.call.assert_awaited_once()


@pytest.mark.asyncio
async def test_launch_is_refused_without_a_normalized_criterion_count(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _Store(_review(status="queued", run_id=None))
    registry = SimpleNamespace(call=AsyncMock())
    _patch_store(monkeypatch, store)
    evaluation = _evaluation()
    del evaluation.extra["criterion_count"]

    result = await launch_close_review(
        _ctx(registry=registry),
        evaluation=evaluation,
        close_arguments=_arguments(),
        evaluate_close=_revalidate(evaluation),
    )

    registry.call.assert_not_awaited()
    assert result["error"] == "close_review_required"
    assert "reviewer_run_id" not in result


@pytest.mark.asyncio
async def test_launch_moves_down_the_candidate_list_after_a_provider_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A quota-exhausted reviewer never judged the evidence, and the error it
    # delivers tells the caller to close again. Relaunching onto the same
    # provider makes that instruction unfollowable, so the ordered candidate
    # list has to advance.
    store = _Store(_review(status="queued", run_id=None), unjudged_attempts=1)
    registry = SimpleNamespace(
        call=AsyncMock(return_value={"success": True, "run_id": _FIRST_REVIEW_RUN_ID})
    )
    ctx = _ctx(
        registry=registry,
        validation_config=TaskValidationConfig(
            candidates=["codex/gpt-5.6-terra", "claude/sonnet"],
        ),
    )
    _patch_store(monkeypatch, store)

    evaluation = _evaluation()
    result = await launch_close_review(
        ctx,
        evaluation=evaluation,
        close_arguments=_arguments(),
        evaluate_close=_revalidate(evaluation),
    )

    launch_args = registry.call.call_args.args[1]
    assert launch_args["provider"] == "claude"
    assert launch_args["model"] == "sonnet"
    assert result["reviewer_provider"] == "claude"
    assert result["reviewer_model"] == "sonnet"


@pytest.mark.asyncio
async def test_launch_wraps_to_the_head_once_every_candidate_has_failed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Running off the end is not a reason to stay on the tail. A quota lifts on
    # its own, so after every candidate has died the one that failed longest ago
    # is the next worth trying; stopping at the last entry would pin the task to
    # whichever provider stays down longest.
    store = _Store(_review(status="queued", run_id=None), unjudged_attempts=2)
    registry = SimpleNamespace(
        call=AsyncMock(return_value={"success": True, "run_id": _FIRST_REVIEW_RUN_ID})
    )
    ctx = _ctx(
        registry=registry,
        validation_config=TaskValidationConfig(
            candidates=["codex/gpt-5.6-terra", "claude/sonnet"],
        ),
    )
    _patch_store(monkeypatch, store)

    evaluation = _evaluation()
    result = await launch_close_review(
        ctx,
        evaluation=evaluation,
        close_arguments=_arguments(),
        evaluate_close=_revalidate(evaluation),
    )

    launch_args = registry.call.call_args.args[1]
    assert launch_args["provider"] == "codex"
    assert launch_args["model"] == "gpt-5.6-terra"
    assert result["reviewer_provider"] == "codex"
    assert result["reviewer_model"] == "gpt-5.6-terra"


@pytest.mark.asyncio
@pytest.mark.integration
async def test_task_update_before_review_launch_returns_stale_without_spawning(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
) -> None:
    manager = LocalTaskManager(temp_db)
    task = manager.create_task(
        sample_project["id"],
        "Concurrent close review",
        validation_criteria="The original criterion passes.",
    )
    session = SessionManager(temp_db).register(
        external_id=f"close-review-stale-{uuid4()}",
        machine_id=None,
        source="codex",
        project_id=sample_project["id"],
    )
    evaluation = _evaluation()
    evaluation.task = task
    evaluation.task_id = task.id
    evaluation.resolved_session_id = session.id
    manager.update_task(task.id, validation_criteria="The updated criterion passes.")
    registry = SimpleNamespace(call=AsyncMock())
    ctx = _ctx(registry=registry)
    ctx.task_manager = manager

    result = await launch_close_review(
        ctx,
        evaluation=evaluation,
        close_arguments=_arguments(),
        evaluate_close=_revalidate(evaluation),
    )

    assert result["success"] is False
    assert result["closed"] is False
    assert result["preview"] is False
    assert result["error"] == "stale_task_state"
    assert result["stale_state"] is True
    assert result["required_actions"] == [
        "Retry close_task; the existing evaluation will not be reused."
    ]
    assert "No reviewer was launched." in result["message"]
    registry.call.assert_not_awaited()
    assert TaskCloseReviewStore(temp_db).get_active_for_task(task.id) is None


@pytest.mark.asyncio
async def test_close_task_persists_commit_before_promoted_review_launch(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = LocalTaskManager(temp_db)
    task = manager.create_task(
        sample_project["id"],
        "Persist commit before promoted review",
        validation_criteria="Focused tests pass.",
    )
    session = SessionManager(temp_db).register(
        external_id=f"close-review-promoted-{uuid4()}",
        machine_id=require_machine_id(),
        source="codex",
        project_id=sample_project["id"],
    )
    commit_sha = "abc123"
    evaluation_count = 0

    async def evaluate(_ctx: RegistryContext, **kwargs: Any) -> CloseEvaluation:
        nonlocal evaluation_count
        evaluation_count += 1
        assert kwargs["commit_sha"] == commit_sha
        persisted = manager.get_task(task.id)
        assert persisted is not None
        evaluation = _evaluation()
        evaluation.task = persisted
        evaluation.task_id = task.id
        evaluation.resolved_session_id = session.id
        evaluation.commit_shas = [commit_sha]
        evaluation.extra.update({"diff_sha": "d" * 64, "test_bodies_sha": "e" * 64})
        return evaluation

    async def spawn_reviewer(tool: str, arguments: dict[str, Any]) -> dict[str, object]:
        assert tool == "spawn_agent"
        persisted = manager.get_task(task.id)
        assert persisted is not None
        assert persisted.commits == [commit_sha]
        return {"success": True, "run_id": arguments["reserved_run_id"]}

    agent_registry = SimpleNamespace(call=AsyncMock(side_effect=spawn_reviewer))
    ctx = _ctx(registry=agent_registry)
    ctx.task_manager = manager
    monkeypatch.setattr(close_tool, "_evaluate_close", evaluate)
    registry = InternalToolRegistry("tasks")
    close_tool.register_close_task(registry, ctx)

    result = await registry.call(
        "close_task",
        {
            "task_id": task.id,
            "changes_summary": "Implemented and tested.",
            "commit_sha": commit_sha,
            "project_path": "/repo",
            "preview": False,
        },
    )

    assert result["error"] == "close_review_required"
    assert result["commit_shas"] == [commit_sha]
    assert result["review_status"] == "running"
    assert evaluation_count == 2
    agent_registry.call.assert_awaited_once()


@pytest.mark.asyncio
async def test_promoted_review_refuses_unpersisted_evaluation_commit(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = LocalTaskManager(temp_db)
    task = manager.create_task(
        sample_project["id"],
        "Refuse review with invisible commit",
        validation_criteria="Focused tests pass.",
    )
    review = _review(status="launching", run_id=_FIRST_REVIEW_RUN_ID)
    store = _Store(review)
    registry = SimpleNamespace(call=AsyncMock())
    ctx = _ctx(registry=registry)
    ctx.task_manager = manager
    _patch_store(monkeypatch, store)
    evaluation = _evaluation()
    evaluation.task = task
    evaluation.task_id = task.id
    evaluation.commit_shas = ["abc123"]

    result = await orchestration._launch_promoted_review(ctx, review, evaluation)

    assert result["success"] is False
    assert result["error"] == "close_review_commit_links_missing"
    assert "abc123" in result["message"]
    registry.call.assert_not_awaited()
    persisted = manager.get_task(task.id)
    assert persisted is not None
    assert persisted.commits is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "reason", ["completed", "obsolete", "duplicate", "wont_fix", "out_of_repo"]
)
async def test_launch_preserves_closure_reason(
    monkeypatch: pytest.MonkeyPatch, reason: str
) -> None:
    store = _Store(_review(status="queued", run_id=None))
    registry = SimpleNamespace(
        call=AsyncMock(return_value={"success": True, "run_id": _FIRST_REVIEW_RUN_ID})
    )
    _patch_store(monkeypatch, store)
    arguments = _arguments()
    arguments["reason"] = reason

    result = await launch_close_review(
        _ctx(registry=registry),
        evaluation=(evaluation := _evaluation()),
        close_arguments=arguments,
        evaluate_close=_revalidate(evaluation),
    )

    assert result["review_status"] == "running"
    assert store.created_arguments is not None
    assert store.created_arguments["reason"] == reason
    prompt = registry.call.call_args.args[1]["prompt"]
    assert f'closure_reason="{reason}"' in prompt
    assert ("This is a no-work disposition review" in prompt) is (reason != "completed")


@pytest.mark.asyncio
async def test_launch_prompt_carries_gate10_validation_facts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The evaluation's gate-10 record reaches the reviewer through its launch prompt."""
    store = _Store(_review(status="queued", run_id=None))
    registry = SimpleNamespace(
        call=AsyncMock(return_value={"success": True, "run_id": _FIRST_REVIEW_RUN_ID})
    )
    ctx = _ctx(registry=registry)
    _patch_store(monkeypatch, store)
    evaluation = _evaluation()
    evaluation.extra["validation_commands"] = {
        "latest_outcomes": {"test": "success"},
        "latest_runs": [
            {
                "category": "test",
                "command": "uv run pytest tests/tasks/ -q",
                "core_command": "uv run pytest tests/tasks/ -q",
                "wrapped": False,
                "completed_at": "2026-09-03T05:10:00+00:00",
                "outcome": "success",
                "exit_code": 0,
            }
        ],
        "uncredited_runs": [
            {
                "command": "uv run pytest tests/tasks/ -q | tail -1",
                "reason": "wrapped",
                "wrapper_reason": "pipeline",
            }
        ],
    }

    arguments = {**_arguments(), "response_detail": "diagnostic"}
    result = await launch_close_review(
        ctx,
        evaluation=evaluation,
        close_arguments=arguments,
        evaluate_close=_revalidate(evaluation),
    )

    launch_prompt = registry.call.await_args.args[1]["prompt"]
    assert "validation_commands=" in launch_prompt
    assert "uv run pytest tests/tasks/ -q" in launch_prompt
    assert "gate 10's authoritative transcript record" in launch_prompt
    assert result["validation_commands"] == evaluation.extra["validation_commands"]
    assert result["validation_commands"]["uncredited_runs"][0]["reason"] == "wrapped"


@pytest.mark.asyncio
async def test_launch_refuses_actual_prompt_over_limit_without_spawning(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _Store(_review(status="queued", run_id=None))
    registry = SimpleNamespace(
        call=AsyncMock(return_value={"success": True, "run_id": _FIRST_REVIEW_RUN_ID})
    )
    config = TaskValidationConfig()
    ctx = _ctx(registry=registry, validation_config=config)
    _patch_store(monkeypatch, store)
    evaluation = _evaluation()
    evaluation.extra["validation_commands"] = {
        "latest_runs": ["x" * config.close_review_prompt_max_chars]
    }

    result = await launch_close_review(
        ctx,
        evaluation=evaluation,
        close_arguments=_arguments(),
        evaluate_close=_revalidate(evaluation),
    )

    registry.call.assert_not_awaited()
    assert store.finished_status is None
    assert result["closed"] is False
    assert result["error"] == "close_review_prompt_too_large"
    assert result["prompt_chars"] > result["prompt_limit"]
    assert "validation_commands" not in result


@pytest.mark.asyncio
async def test_pending_concise_response_keeps_commands_only_in_reviewer_prompt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _Store(_review(status="queued", run_id=None))
    registry = SimpleNamespace(
        call=AsyncMock(return_value={"success": True, "run_id": _FIRST_REVIEW_RUN_ID})
    )
    ctx = _ctx(registry=registry)
    _patch_store(monkeypatch, store)
    evaluation = _evaluation()
    evaluation.extra["validation_commands"] = {"latest_runs": [{"command": "npm ci"}]}

    result = await launch_close_review(
        ctx,
        evaluation=evaluation,
        close_arguments=_arguments(),
        evaluate_close=_revalidate(evaluation),
    )

    assert "validation_commands" not in result
    assert "npm ci" in registry.call.await_args.args[1]["prompt"]


@pytest.mark.asyncio
@pytest.mark.integration
async def test_launch_after_rejected_verdict_does_not_carry_cross_fingerprint_requirements(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = TaskCloseReviewStore(temp_db)
    task_manager = LocalTaskManager(temp_db)
    task = task_manager.create_task(
        project_id=sample_project["id"],
        title="Carry prior close evidence",
        priority=1,
        validation_criteria="Focused tests pass.",
    )
    caller = SessionManager(temp_db).register(
        external_id=f"close-review-caller-{uuid4()}",
        machine_id=require_machine_id(),
        source="codex",
        project_id=sample_project["id"],
    )
    evaluation = CloseEvaluation(f"#{task.seq_num}")
    evaluation.task = task
    evaluation.task_id = task.id
    evaluation.resolved_session_id = caller.id
    evaluation.repo_path = "/repo"
    evaluation.commit_shas = ["def"]
    evaluation.error = "close_review_required"
    evaluation.extra.update(
        {
            "review_fingerprint": "first-review",
            "deterministic_evidence_fingerprint": "first-evidence",
            "diff_sha": "a" * 64,
            "test_bodies_sha": "b" * 64,
            "stable_facts": {"commit_shas": ["def"]},
            "criterion_count": 1,
        }
    )
    registry = _successful_registry()
    ctx = cast(
        RegistryContext,
        SimpleNamespace(
            task_manager=task_manager,
            agent_registry=registry,
            validation_config=TaskValidationConfig(),
        ),
    )

    await launch_close_review(
        ctx,
        evaluation=evaluation,
        close_arguments=_arguments(),
        evaluate_close=_revalidate(evaluation),
    )

    first_launch_prompt = registry.call.await_args.args[1]["prompt"]
    assert "prior_requirements=" not in first_launch_prompt
    first_review = store.get_active_for_task(task.id)
    assert first_review is not None
    assert first_review.agent_run_id is not None

    monkeypatch.setattr(orchestration, "_authenticate_submission", lambda _ctx, _review: None)
    submitted_verdict: dict[str, object] = {
        "status": "invalid",
        "criteria": [
            {
                "index": 1,
                "satisfied": False,
                "gap": "The real close path was not exercised.",
                "required_evidence": _REQUIRED_EVIDENCE,
            }
        ],
        "feedback": "The close evidence is incomplete.",
    }
    submit_ctx = cast(
        RegistryContext,
        SimpleNamespace(
            task_manager=task_manager,
            agent_registry=None,
            validation_config=TaskValidationConfig(),
        ),
    )

    async def evaluate_close(
        _ctx: RegistryContext,
        **kwargs: Any,
    ) -> CloseEvaluation:
        submitted = kwargs["submitted_review"]
        assert isinstance(submitted, SubmittedCloseReview)
        evaluation = CloseEvaluation(f"#{task.seq_num}")
        evaluation.task_id = task.id
        evaluation.error = "validation_failed"
        evaluation.message = "The close evidence is incomplete."
        evaluation.validation_status = "invalid"
        evaluation.verdict = dict(submitted.verdict)
        return evaluation

    submit_result = await submit_close_review(
        submit_ctx,
        review_id=first_review.id,
        verdict=submitted_verdict,
        evaluate_close=evaluate_close,
        commit_close=AsyncMock(),
    )

    assert submit_result["review_status"] == "invalid"
    terminal = store.get(first_review.id)
    assert terminal is not None
    assert terminal.result_payload is not None
    assert terminal.result_payload["validation_status"] == "invalid"
    # The wake payload references the rejection; it does not re-carry the verdict.
    assert "verdict" not in terminal.result_payload

    registry = _successful_registry()
    evaluation.extra.update(
        {
            "review_fingerprint": "second-review",
            "deterministic_evidence_fingerprint": "second-evidence",
            "diff_sha": "c" * 64,
            "test_bodies_sha": "d" * 64,
            "stable_facts": {"commit_shas": ["def"]},
        }
    )
    ctx = cast(
        RegistryContext,
        SimpleNamespace(
            task_manager=task_manager,
            agent_registry=registry,
            validation_config=TaskValidationConfig(),
        ),
    )

    await launch_close_review(
        ctx,
        evaluation=evaluation,
        close_arguments=_arguments(),
        evaluate_close=_revalidate(evaluation),
    )

    launch_prompt = registry.call.await_args.args[1]["prompt"]
    assert _REQUIRED_EVIDENCE not in launch_prompt
    assert "prior_requirements=" not in launch_prompt
    assert 'changes_summary="Implemented."' in launch_prompt


@pytest.mark.asyncio
async def test_launch_omits_model_overrides_without_validation_config(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _Store(_review(status="queued", run_id=None))
    registry = SimpleNamespace(
        call=AsyncMock(return_value={"success": True, "run_id": _FIRST_REVIEW_RUN_ID})
    )
    ctx = cast(
        RegistryContext,
        SimpleNamespace(
            task_manager=SimpleNamespace(
                db=object(),
                get_task=lambda _task_id: replace(cast(Task, _evaluation().task), commits=["abc"]),
            ),
            agent_registry=registry,
            validation_config=None,
        ),
    )
    _patch_store(monkeypatch, store)

    evaluation = _evaluation()
    await launch_close_review(
        ctx,
        evaluation=evaluation,
        close_arguments=_arguments(),
        evaluate_close=_revalidate(evaluation),
    )

    launch_args = registry.call.call_args.args[1]
    assert "provider" not in launch_args
    assert "model" not in launch_args
    assert "reasoning_effort" not in launch_args
    assert launch_args["timeout"] == 1200.0


@pytest.mark.asyncio
async def test_failed_reviewer_launch_remains_unsuccessful(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _Store(_review(status="queued", run_id=None))
    registry = SimpleNamespace(
        call=AsyncMock(return_value={"success": False, "error": "reviewer unavailable"})
    )
    _patch_store(monkeypatch, store)
    evaluation = _evaluation()

    result = await launch_close_review(
        _ctx(registry=registry),
        evaluation=evaluation,
        close_arguments=_arguments(),
        evaluate_close=_revalidate(evaluation),
    )

    assert result["success"] is False
    assert result["closed"] is False
    assert result["can_close"] is False
    assert result["error"] == "agentic_review_launch_failed"
    assert result["review_status"] == "error"
    assert store.finished_status == "error"


@pytest.mark.asyncio
async def test_finish_launch_error_in_background_close_is_surfaced(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _Store(_review(status="queued", run_id=None))
    registry = SimpleNamespace(call=AsyncMock(side_effect=OSError("review launch failed")))
    _patch_store(monkeypatch, store)
    monkeypatch.setattr(
        orchestration,
        "_finish_launch_error",
        MagicMock(side_effect=RuntimeError("finish launch error failed")),
    )
    evaluation = _evaluation()
    spawn_started = asyncio.Event()
    close_task = asyncio.create_task(
        launch_close_review(
            _ctx(registry=registry),
            evaluation=evaluation,
            close_arguments=_arguments(),
            evaluate_close=_revalidate(evaluation),
        )
    )

    with pytest.raises(RuntimeError, match="finish launch error failed"):
        await wait_for_awaitable_or_background_task(
            spawn_started.wait(),
            close_task,
            timeout=1,
            description="task-close reviewer spawn",
        )

    assert close_task.done()
    assert close_task.exception() is not None


@pytest.mark.asyncio
async def test_missing_agent_registry_finishes_launch_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _Store(_review(status="queued", run_id=None))
    _patch_store(monkeypatch, store)
    evaluation = _evaluation()

    result = await launch_close_review(
        _ctx(registry=None),
        evaluation=evaluation,
        close_arguments=_arguments(),
        evaluate_close=_revalidate(evaluation),
    )

    assert result["error"] == "close_review_unavailable"
    assert result["closed"] is False
    assert store.finished_status is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "launch,expected_class",
    [
        pytest.param(
            OSError("provider executable missing"), "retryable_infrastructure", id="launch-os-error"
        ),
        pytest.param(
            TimeoutError("launch timed out"), "retryable_infrastructure", id="launch-timeout"
        ),
        pytest.param(RuntimeError("unknown"), "action_required", id="unknown-exception"),
        pytest.param({"success": True}, "action_required", id="missing-run-id"),
        pytest.param(
            {"success": True, "run_id": "bad"}, "action_required", id="invalid-success-id"
        ),
        pytest.param(["invalid"], "action_required", id="malformed-response"),
        pytest.param(
            {"success": False, "run_id": "invalid"}, "action_required", id="malformed-run-id"
        ),
        pytest.param(
            {"success": False, "error": "invalid prompt"}, "action_required", id="request-error"
        ),
    ],
)
async def test_incomplete_spawn_finishes_launch_error(
    monkeypatch: pytest.MonkeyPatch,
    launch: Exception | dict[str, object] | list[str],
    expected_class: str,
) -> None:
    store = _Store(_review(status="queued", run_id=None))
    call = (
        AsyncMock(side_effect=launch)
        if isinstance(launch, Exception)
        else AsyncMock(return_value=launch)
    )
    _patch_store(monkeypatch, store)
    evaluation = _evaluation()

    result = await launch_close_review(
        _ctx(registry=SimpleNamespace(call=call)),
        evaluation=evaluation,
        close_arguments=_arguments(),
        evaluate_close=_revalidate(evaluation),
    )

    assert result["error"] == "agentic_review_launch_failed"
    assert result["closed"] is False
    assert result["error_class"] == expected_class
    assert (result["retry_after"] is not None) == (expected_class == "retryable_infrastructure")
    assert store.review.result_payload is not None
    assert store.review.result_payload["error_class"] == expected_class
    assert store.finished_status == "error"


@pytest.mark.parametrize(
    ("candidates", "profile", "expected"),
    [
        (
            ["codex/gpt-5.6-terra"],
            "feature_mid",
            {"provider": "codex", "model": "gpt-5.6-terra", "reasoning_effort": "auto"},
        ),
        (
            [{"candidate": "codex/gpt-5.6-sol", "reasoning_effort": "xhigh"}, "claude/opus"],
            "feature_high",
            {"provider": "codex", "model": "gpt-5.6-sol", "reasoning_effort": "xhigh"},
        ),
        (
            ["claude/sonnet"],
            "feature_low",
            {"provider": "claude", "model": "sonnet", "reasoning_effort": "auto"},
        ),
    ],
)
def test_reviewer_spawn_overrides_follow_first_validation_candidate(
    candidates: list[object],
    profile: str,
    expected: dict[str, str | None],
) -> None:
    config = TaskValidationConfig(candidates=candidates, profile=profile)

    overrides = agentic_close_review_module.reviewer_spawn_overrides(config)

    assert overrides == expected


def test_reviewer_spawn_overrides_carry_the_reached_candidates_pinned_effort() -> None:
    # Each candidate brings its own reasoning pin; skipping to the second one
    # has to bring the second one's effort, not the head's.
    config = TaskValidationConfig(
        candidates=[
            {"candidate": "codex/gpt-5.6-sol", "reasoning_effort": "xhigh"},
            {"candidate": "claude/opus", "reasoning_effort": "high"},
        ],
        profile="feature_high",
    )

    overrides = agentic_close_review_module.reviewer_spawn_overrides(config, unjudged_attempts=1)

    assert overrides == {"provider": "claude", "model": "opus", "reasoning_effort": "high"}


def test_reviewer_spawn_overrides_are_empty_without_config() -> None:
    assert agentic_close_review_module.reviewer_spawn_overrides(None) == {}


@pytest.mark.asyncio
async def test_concurrent_close_reuses_active_review(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    existing = _review(status="running", run_id="same-run")
    store = _Store(existing, created=False)
    registry = SimpleNamespace(call=AsyncMock())
    _patch_store(monkeypatch, store)
    evaluation = _evaluation()

    result = await launch_close_review(
        _ctx(registry=registry),
        evaluation=evaluation,
        close_arguments=_arguments(),
        evaluate_close=_revalidate(evaluation),
    )

    assert result["success"] is True
    assert result["closed"] is False
    assert result["can_close"] is False
    assert result["error"] == "close_review_required"
    assert result["review_status"] == "running"
    assert "run_id" not in result
    assert "Do not poll agent runs or re-call close_task." in result["message"]
    registry.call.assert_not_awaited()


@pytest.mark.asyncio
async def test_authenticated_valid_submission_closes_and_persists_payload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _Store(_review(status="running", run_id="run"))
    _authenticate(monkeypatch, store.review)
    monkeypatch.setattr(orchestration, "TaskCloseReviewStore", lambda _db: store)
    evaluation = _evaluation(ready=True)
    evaluate = AsyncMock(return_value=evaluation)
    commit = AsyncMock(
        return_value={
            "success": True,
            "closed": True,
            "task_id": "task",
            "commit_shas": ["abc"],
        }
    )

    result = await submit_close_review(
        _ctx(),
        review_id="review",
        verdict=_verdict("valid"),
        evaluate_close=evaluate,
        commit_close=commit,
    )

    assert result["success"] is True
    assert result["review_status"] == "closed"
    assert result["terminal_payload"]["event"] == "task_close_review_completed"
    assert store.finished_status == "closed"
    assert evaluate.call_args.kwargs["closing_session_id"] == "parent"
    assert evaluate.call_args.kwargs["submitted_review"].review_fingerprint == "close"
    commit.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.integration
@pytest.mark.parametrize("reviewer_ended", [False, True], ids=["running", "late-success"])
async def test_submit_close_review_claims_before_heavy_work(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    reviewer_ended: bool,
) -> None:
    """Real stdio/HTTP accepts both active and late verdicts before heavy reads."""
    session_manager = SessionManager(temp_db)
    caller_transcript = tmp_path / "caller.jsonl"
    await asyncio.to_thread(_write_oversized_codex_transcript, caller_transcript)
    caller = session_manager.register(
        external_id=str(uuid4()),
        machine_id=require_machine_id(),
        source="codex",
        project_id=sample_project["id"],
        transcript_path=str(caller_transcript),
        is_local=True,
        workspace_path=str(tmp_path),
    )
    reviewer = session_manager.register(
        external_id=str(uuid4()),
        machine_id=require_machine_id(),
        source="codex",
        project_id=sample_project["id"],
        transcript_path=str(tmp_path / "reviewer.jsonl"),
        parent_session_id=caller.id,
        is_local=True,
        workspace_path=str(tmp_path),
    )
    run_manager = LocalAgentRunManager(temp_db)

    task = LocalTaskManager(temp_db).create_task(
        project_id=sample_project["id"],
        title="Review the oversized transcript",
        created_in_session_id=caller.id,
        validation_criteria=_REQUIRED_EVIDENCE,
    )
    store = TaskCloseReviewStore(temp_db)
    review, created = store.create_or_get_active(
        **{
            **_persisted_review_intent(task, caller_session_id=caller.id),
            "task_id": task.id,
            "task_ref": f"#{task.seq_num}",
            "close_arguments": {**_arguments(), "task_id": task.id},
            "caller_session_id": caller.id,
            "review_fingerprint": "oversized-transcript-review",
            "evidence_fingerprint": "oversized-transcript-evidence",
        }
    )
    assert created is True
    review = _promote_persisted(store, task, review)
    assert review.agent_run_id is not None
    activated = run_manager.activate_queued(
        review.agent_run_id,
        child_session_id=reviewer.id,
        provider="codex",
        prompt="Review the task close.",
        workflow_name="task-close-reviewer",
        agent_name=agentic_close_review_module.TASK_CLOSE_REVIEWER_AGENT,
        model="gpt-test",
        is_local=True,
        requested_reasoning_effort=None,
        effective_reasoning_effort=None,
        reasoning_required=False,
        reasoning_status="not_requested",
        reasoning_message=None,
        timeout_seconds=1200,
        resume_metadata_json=None,
        worktree_id=None,
        clone_id=None,
    )
    assert activated is not None
    run = run_manager.start(review.agent_run_id)
    assert run is not None
    assert store.bind_run(review.id, review.agent_run_id) is not None
    initial_status: TaskCloseReviewStatus = "running"
    if reviewer_ended:
        completed_run = run_manager.complete(run.id)
        assert completed_run is not None
        assert completed_run.status == "success"
        delivery = terminal_review_delivery(temp_db, run.id)
        assert delivery is not None
        assert delivery[1] == REVIEWER_RUN_ENDED_SUCCESS_ERROR
        abandoned = store.get(review.id)
        assert abandoned is not None
        assert abandoned.status == "error"
        assert abandoned.error == REVIEWER_RUN_ENDED_SUCCESS_ERROR
        initial_status = "error"
    transcript_reader = TranscriptReader(session_manager)
    default_preflight_observations: list[tuple[str, TaskCloseReviewStatus]] = []
    observed_finalizing: list[str] = []

    def observe_default_preflight() -> None:
        persisted = store.get(review.id)
        assert persisted is not None
        default_preflight_observations.append((_STDIO_DEFAULT_PREFLIGHT_PATH, persisted.status))

    async def evaluate_close(_ctx: RegistryContext, **kwargs: Any) -> CloseEvaluation:
        assert default_preflight_observations[-1] == (
            _STDIO_DEFAULT_PREFLIGHT_PATH,
            initial_status,
        )
        persisted = await asyncio.to_thread(store.get, review.id)
        assert persisted is not None
        assert persisted.status == "finalizing"
        observed_finalizing.append(persisted.status)
        assert caller_transcript.stat().st_size > _OVERSIZED_TRANSCRIPT_BYTES
        messages = await transcript_reader.get_messages(kwargs["closing_session_id"], limit=1)
        assert messages
        submitted = kwargs["submitted_review"]
        assert isinstance(submitted, SubmittedCloseReview)
        evaluation = CloseEvaluation(review.task_ref)
        evaluation.task_id = review.task_id
        evaluation.error = "validation_failed"
        evaluation.message = "The close evidence is incomplete."
        evaluation.validation_status = "invalid"
        evaluation.verdict = dict(submitted.verdict)
        return evaluation

    monkeypatch.setattr(close_tool, "_evaluate_close", evaluate_close)
    internal_manager = InternalRegistryManager()
    internal_manager.add_registry(
        create_task_registry(LocalTaskManager(temp_db), project_id=sample_project["id"])
    )

    async def run_db(function: Any, *args: Any, **kwargs: Any) -> Any:
        return await asyncio.to_thread(function, *args, **kwargs)

    http_server = SimpleNamespace(
        _internal_manager=internal_manager,
        tool_proxy=None,
        mcp_manager=None,
        session_manager=session_manager,
        services=SimpleNamespace(database=temp_db),
        config=SimpleNamespace(mcp_client_proxy=SimpleNamespace(tool_timeout=10.0)),
        run_db=run_db,
    )
    async with _live_mcp_http_server(
        http_server,
        health_observer=observe_default_preflight,
    ) as port:
        gobby_home = tmp_path / "gobby-home"
        _write_test_bootstrap(gobby_home, temp_db.conninfo, port)
        environment = os.environ.copy()
        for variable in (
            "GOBBY_CONFIG_FILE",
            "GOBBY_AGENT_API_TOKEN",
            "GOBBY_DAEMON_URL",
            "GOBBY_DAEMON_PORT",
            "GOBBY_PORT",
        ):
            environment.pop(variable, None)
        environment.update(
            {
                "GOBBY_HOME": str(gobby_home),
                "GOBBY_AGENT_RUN_ID": run.id,
                "GOBBY_SESSION_ID": reviewer.id,
                "GOBBY_PROJECT_ID": sample_project["id"],
            }
        )
        master_fd, slave_fd = pty.openpty()
        process = subprocess.Popen(
            [
                "uv",
                "run",
                "--project",
                str(Path(__file__).resolve().parents[4]),
                "gobby",
                "mcp-server",
            ],
            cwd=tmp_path,
            env=environment,
            stdin=slave_fd,
            stdout=slave_fd,
            stderr=slave_fd,
            start_new_session=True,
        )
        os.close(slave_fd)
        try:
            _write_pty_json(master_fd, _initialize_request())
            initialize = await asyncio.to_thread(_read_pty_response, master_fd, 1, 20.0)
            assert "result" in initialize
            _write_pty_json(
                master_fd,
                {"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}},
            )
            startup_health_count = len(default_preflight_observations)
            submit_request = _submit_review_request(review.id, sample_project["id"])
            encoded_submit = _encode_pty_json(submit_request)
            assert len(encoded_submit) < 1024
            assert b'"preflight_enabled"' not in encoded_submit
            started = time.monotonic()
            await asyncio.to_thread(_write_pty_bytes, master_fd, encoded_submit)
            response = await asyncio.to_thread(
                _read_pty_response,
                master_fd,
                2,
                _SUBMIT_DEADLINE_SECONDS,
            )
            elapsed = time.monotonic() - started
        finally:
            await asyncio.to_thread(_stop_stdio_process, process, master_fd)

    assert "result" in response
    structured = response["result"]["structuredContent"]
    assert structured["success"] is True
    assert elapsed < _SUBMIT_DEADLINE_SECONDS
    assert len(default_preflight_observations) == startup_health_count + 1
    assert default_preflight_observations[-1] == (
        _STDIO_DEFAULT_PREFLIGHT_PATH,
        initial_status,
    )
    assert observed_finalizing == ["finalizing"]
    finished = store.get(review.id)
    assert finished is not None
    assert finished.status == "invalid"
    assert finished.result_payload is not None
    assert finished.result_payload["status"] == "invalid"


@pytest.mark.asyncio
async def test_late_verdict_after_run_end_is_applied(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_end_message = "Task-close reviewer run ended with status success before finalization."
    prior_payload = {"status": "error", "message": run_end_message}
    store = _Store(
        replace(
            _review(status="error", run_id="run"),
            result_payload=prior_payload,
            error=run_end_message,
        )
    )
    _authenticate(monkeypatch, store.review)
    monkeypatch.setattr(orchestration, "TaskCloseReviewStore", lambda _db: store)
    commit = AsyncMock(
        return_value={
            "success": True,
            "closed": True,
            "task_id": "task",
            "commit_shas": ["abc"],
        }
    )

    result = await submit_close_review(
        _ctx(),
        review_id="review",
        verdict=_verdict("valid"),
        evaluate_close=AsyncMock(return_value=_evaluation(ready=True)),
        commit_close=commit,
    )

    assert store.claimed is True
    assert store.finished_status == "closed"
    assert result["review_status"] == "closed"
    assert result["terminal_payload"]["status"] == "closed"
    assert result["terminal_payload"] != prior_payload


@pytest.mark.asyncio
async def test_late_submission_yields_to_newer_active_review(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = TaskCloseReviewStore(temp_db)
    task = LocalTaskManager(temp_db).create_task(
        project_id=sample_project["id"],
        title="Yield an obsolete close review",
        validation_criteria="The obsolete verdict does not replace the active review.",
    )
    caller = SessionManager(temp_db).register(
        external_id=f"late-close-review-{uuid4()}",
        machine_id=require_machine_id(),
        source="codex",
        project_id=sample_project["id"],
    )
    old_review, _created = store.create_or_get_active(
        **_persisted_review_intent(task, caller_session_id=caller.id)
    )
    old_review = _promote_persisted(store, task, old_review)
    assert store.bind_run(old_review.id, old_review.agent_run_id or "") is not None
    prior_payload = {
        "event": "task_close_review_completed",
        "status": "error",
        "message": REVIEWER_RUN_ENDED_SUCCESS_ERROR,
    }
    assert (
        store.finish(
            old_review.id,
            status="error",
            result_payload=prior_payload,
            error=REVIEWER_RUN_ENDED_SUCCESS_ERROR,
        )
        is not None
    )
    newer, created = store.create_or_get_active(
        **{
            **_persisted_review_intent(task, caller_session_id=caller.id),
            "review_fingerprint": "newer-review",
            "evidence_fingerprint": "newer-evidence",
        }
    )
    assert created is True
    newer = _promote_persisted(store, task, newer)
    assert store.bind_run(newer.id, newer.agent_run_id or "") is not None
    monkeypatch.setattr(orchestration, "_authenticate_submission", lambda _ctx, _review: None)
    evaluate_close = AsyncMock(side_effect=AssertionError("newer review owns the active slot"))
    ctx = cast(
        RegistryContext,
        SimpleNamespace(task_manager=SimpleNamespace(db=temp_db)),
    )

    result = await submit_close_review(
        ctx,
        review_id=old_review.id,
        verdict=_verdict("invalid"),
        evaluate_close=evaluate_close,
        commit_close=AsyncMock(),
    )

    assert result == {
        "success": True,
        "review_id": old_review.id,
        "review_status": "error",
        "closed": False,
        "terminal_payload": prior_payload,
    }
    evaluate_close.assert_not_awaited()
    active = store.get_active_for_task(old_review.task_id)
    assert active is not None
    assert active.id == newer.id
    assert active.status == "running"


@pytest.mark.asyncio
async def test_invalid_and_stale_submissions_clear_active_lock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for validation_status, error, expected in [
        ("invalid", "validation_failed", "invalid"),
        (None, "agentic_review_stale", "stale"),
    ]:
        store = _Store(_review(status="running", run_id="run"))
        _authenticate(monkeypatch, store.review)
        monkeypatch.setattr(
            orchestration,
            "TaskCloseReviewStore",
            lambda _db, current=store: current,
        )
        evaluation = _evaluation()
        evaluation.error = error
        evaluation.message = "feedback"
        evaluation.validation_status = validation_status
        evaluation.extra["blocking_reasons"] = ["gap"]

        result = await submit_close_review(
            _ctx(),
            review_id="review",
            verdict=_verdict("invalid"),
            evaluate_close=AsyncMock(return_value=evaluation),
            commit_close=AsyncMock(),
        )

        assert result["review_status"] == expected
        assert store.finished_status == expected
        assert result["closed"] is False
        if expected == "invalid":
            assert result["terminal_payload"]["blocking_reasons"] == ["gap"]


@pytest.mark.asyncio
async def test_external_pending_submission_stays_open_and_names_live_criteria(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _Store(_review(status="running", run_id="run"))
    _authenticate(monkeypatch, store.review)
    monkeypatch.setattr(orchestration, "TaskCloseReviewStore", lambda _db: store)
    evaluation = _evaluation()
    evaluation.error = "external_pending"
    evaluation.message = "Coordinator verification remains."
    evaluation.validation_status = "pending"
    evaluation.extra.update(
        {
            "pending_external_criteria": ["Live: restart the daemon."],
            "blocking_reasons": [],
            "required_actions": ["End this agent run."],
        }
    )
    commit = AsyncMock()

    result = await submit_close_review(
        _ctx(),
        review_id="review",
        verdict=_verdict("valid"),
        evaluate_close=AsyncMock(return_value=evaluation),
        commit_close=commit,
    )

    assert result["review_status"] == "external_pending"
    assert result["closed"] is False
    assert result["terminal_payload"]["validation_status"] == "pending"
    assert result["terminal_payload"]["pending_external_criteria"] == ["Live: restart the daemon."]
    assert result["terminal_payload"]["blocking_reasons"] == []
    assert store.finished_status == "external_pending"
    commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_malformed_submission_returns_review_to_running(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _Store(_review(status="running", run_id="run"))
    _authenticate(monkeypatch, store.review)
    monkeypatch.setattr(orchestration, "TaskCloseReviewStore", lambda _db: store)
    evaluation = _evaluation()
    evaluation.error = "agentic_review_malformed"
    evaluation.message = "Fix criterion indexes."

    result = await submit_close_review(
        _ctx(),
        review_id="review",
        verdict={},
        evaluate_close=AsyncMock(return_value=evaluation),
        commit_close=AsyncMock(),
    )

    assert result["error"] == "agentic_review_malformed"
    assert result["review_status"] == "running"
    assert store.restored is True
    assert store.finished_status is None


@pytest.mark.asyncio
async def test_wrong_reviewer_run_is_rejected_without_transition(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _Store(_review(status="running", run_id="run"))
    monkeypatch.setattr(orchestration, "TaskCloseReviewStore", lambda _db: store)
    monkeypatch.setattr(orchestration, "get_current_agent_run_id", lambda: "other-run")
    monkeypatch.setattr(orchestration, "get_current_session_id", lambda: "child")

    result = await submit_close_review(
        _ctx(),
        review_id="review",
        verdict=_verdict("valid"),
        evaluate_close=AsyncMock(),
        commit_close=AsyncMock(),
    )

    assert result["error"] == "agentic_review_unauthorized"
    assert store.claimed is False


class _Store:
    def __init__(
        self,
        review: TaskCloseReview,
        *,
        created: bool = True,
        unjudged_attempts: int = 0,
        reusable_rejection: TaskCloseReview | None = None,
    ) -> None:
        self.db = object()
        self.review = review
        self.created = created
        self.created_arguments: dict[str, Any] | None = None
        self.expected_task_updated_at: datetime | None = None
        self.finished_status: str | None = None
        self.claimed = False
        self.claimed_verdict: dict[str, Any] | None = None
        self.restored = False
        self.unjudged_attempts = unjudged_attempts
        self.queue_claimed = False
        self.reusable_rejection = reusable_rejection
        self.reuse_lookup: tuple[str, str] | None = None

    def count_unjudged_attempts(self, _task_id: str) -> int:
        return self.unjudged_attempts

    def get_delivered_rejected_verdict(
        self,
        *,
        task_id: str,
        review_fingerprint: str,
        expected_task_updated_at: datetime,
    ) -> TaskCloseReview | None:
        assert expected_task_updated_at == datetime(2026, 8, 22, tzinfo=UTC)
        self.reuse_lookup = (task_id, review_fingerprint)
        if self.reusable_rejection is None:
            return None
        if self.reusable_rejection.review_fingerprint != review_fingerprint:
            return None
        return self.reusable_rejection

    def create_or_get_active(self, **kwargs: Any) -> tuple[TaskCloseReview, bool]:
        self.created_arguments = dict(kwargs["close_arguments"])
        self.expected_task_updated_at = kwargs["expected_task_updated_at"]
        if self.created:
            run = cast(QueuedAgentRunSpec, kwargs["run"])
            self.review = replace(
                self.review,
                id=str(kwargs["review_id"]),
                agent_run_id=run.id,
                close_arguments=dict(kwargs["close_arguments"]),
                review_fingerprint=kwargs["review_fingerprint"],
                evidence_fingerprint=kwargs["evidence_fingerprint"],
                status="queued",
            )
        return self.review, self.created

    def list_queued_project_ids(self) -> list[str]:
        return ["project"] if self.review.status == "queued" else []

    def claim_queued(self, *, project_id: str, max_concurrency: int) -> list[TaskCloseReview]:
        assert project_id == "project"
        assert max_concurrency > 0
        if self.review.status != "queued" or self.queue_claimed:
            return []
        self.queue_claimed = True
        self.review = replace(self.review, status="launching")
        return [self.review]

    def bind_run(self, _review_id: str, run_id: str) -> TaskCloseReview:
        self.review = replace(self.review, status="running", agent_run_id=run_id)
        return self.review

    def get(self, _review_id: str) -> TaskCloseReview:
        return self.review

    def claim_finalizing(
        self,
        _review_id: str,
        _run_id: str,
        *,
        verdict: Mapping[str, Any] | None = None,
    ) -> TaskCloseReview:
        self.claimed = True
        self.claimed_verdict = dict(verdict) if verdict is not None else None
        self.review = replace(self.review, status="finalizing")
        return self.review

    def restore_running(self, _review_id: str, _run_id: str, *, error: str) -> bool:
        del error
        self.restored = True
        self.review = replace(self.review, status="running")
        return True

    def finish(self, _review_id: str, *, status: str, **kwargs: Any) -> TaskCloseReview:
        self.finished_status = status
        self.review = replace(
            self.review,
            status=cast(TaskCloseReviewStatus, status),
            result_payload=dict(kwargs["result_payload"]),
        )
        return self.review


class _RunStore:
    def __init__(self, _db: object) -> None:
        pass

    def update_queued_prompt(self, run_id: str, prompt: str) -> SimpleNamespace:
        return SimpleNamespace(id=run_id, prompt=prompt, status="queued")

    def fail(self, run_id: str, error: str) -> SimpleNamespace:
        return SimpleNamespace(id=run_id, error=error, status="failed", terminal_reason=None)

    def get(self, _run_id: str) -> None:
        return None


def _patch_store(monkeypatch: pytest.MonkeyPatch, store: _Store) -> None:
    monkeypatch.setattr(orchestration, "TaskCloseReviewStore", lambda _db: store)
    monkeypatch.setattr(orchestration, "LocalAgentRunManager", _RunStore)


def _revalidate(evaluation: CloseEvaluation) -> Callable[..., Any]:
    async def evaluate(_ctx: RegistryContext, **_kwargs: Any) -> CloseEvaluation:
        return evaluation

    return evaluate


def _ctx(
    *,
    registry: object | None = None,
    validation_config: object | None = None,
) -> RegistryContext:
    if validation_config is None:
        validation_config = TaskValidationConfig(candidates=["codex/gpt-5.6-terra"])
    return cast(
        RegistryContext,
        SimpleNamespace(
            task_manager=SimpleNamespace(
                db=object(),
                get_task=lambda _task_id: replace(cast(Task, _evaluation().task), commits=["abc"]),
            ),
            agent_registry=registry,
            validation_config=validation_config,
        ),
    )


def _review(*, status: str, run_id: str | None) -> TaskCloseReview:
    now = datetime(2026, 8, 22, tzinfo=UTC)
    return TaskCloseReview(
        id="review",
        task_id="task",
        task_ref="#42",
        caller_session_id="parent",
        agent_run_id=run_id,
        close_arguments=_arguments(),
        review_fingerprint="close",
        evidence_fingerprint="evidence",
        status=cast(TaskCloseReviewStatus, status),
        result_payload=None,
        error=None,
        launched_at=now,
        completed_at=None,
        delivered_at=None,
        created_at=now,
        updated_at=now,
    )


def _evaluation(*, ready: bool = False) -> CloseEvaluation:
    task = Task(
        id="task",
        project_id="project",
        title="Task",
        priority=2,
        task_type="task",
        created_at=datetime(2026, 8, 22, tzinfo=UTC),
        updated_at=datetime(2026, 8, 22, tzinfo=UTC),
        seq_num=42,
    )
    evaluation = CloseEvaluation("#42")
    evaluation.task = task
    evaluation.task_id = task.id
    evaluation.resolved_session_id = "parent"
    evaluation.repo_path = "/repo"
    evaluation.commit_shas = ["abc"]
    evaluation.extra.update(
        {
            "review_fingerprint": "close",
            "deterministic_evidence_fingerprint": "evidence",
            "diff_sha": "diff",
            "test_bodies_sha": "tests",
            "stable_facts": {},
            "criterion_count": 3,
            "close_review_duration_ms": 4.25,
        }
    )
    if ready:
        evaluation.pass_gate(13, "close_review", "valid")
    else:
        evaluation.error = "close_review_required"
    return evaluation


def _arguments() -> dict[str, Any]:
    return {
        "task_id": "#42",
        "reason": "completed",
        "changes_summary": "Implemented.",
        "skip_validation": False,
        "override_justification": None,
        "scope_justification": None,
        "commit_sha": "abc",
        "project_path": "/repo",
        "preview": False,
        "response_detail": "concise",
    }


def _persisted_review_intent(
    task: Task,
    *,
    caller_session_id: str = _PERSISTED_SESSION_ID,
) -> dict[str, Any]:
    run_id = str(uuid4())
    return {
        "task_id": task.id,
        "task_ref": f"#{task.seq_num}",
        "caller_session_id": caller_session_id,
        "commit_shas": (),
        "close_arguments": _arguments(),
        "expected_task_updated_at": task.updated_at,
        "review_fingerprint": "review",
        "evidence_fingerprint": "evidence",
        "diff_sha": "a" * 64,
        "test_bodies_sha": "b" * 64,
        "stable_facts": {},
        "review_id": str(uuid4()),
        "run": QueuedAgentRunSpec(
            id=run_id,
            machine_id=require_machine_id(),
            provider="codex",
            model="gpt-test",
            agent_name="task-close-reviewer",
            prompt="Review task-close evidence.",
            timeout_seconds=1200,
            requested_reasoning_effort=None,
        ),
    }


def _promote_persisted(
    store: TaskCloseReviewStore,
    task: Task,
    review: TaskCloseReview,
) -> TaskCloseReview:
    promoted = store.claim_queued(project_id=str(task.project_id), max_concurrency=3)
    match = next((item for item in promoted if item.id == review.id), None)
    assert match is not None
    return match


def _successful_registry() -> SimpleNamespace:
    async def call(_tool: str, arguments: Mapping[str, Any]) -> dict[str, object]:
        return {"success": True, "run_id": arguments["reserved_run_id"]}

    return SimpleNamespace(call=AsyncMock(side_effect=call))


def _authenticate(monkeypatch: pytest.MonkeyPatch, review: TaskCloseReview) -> None:
    run = SimpleNamespace(
        agent_name="task-close-reviewer",
        task_id=None,
        parent_session_id=review.caller_session_id,
        child_session_id="child",
    )
    monkeypatch.setattr(orchestration, "get_current_agent_run_id", lambda: review.agent_run_id)
    monkeypatch.setattr(orchestration, "get_current_session_id", lambda: "child")
    monkeypatch.setattr(
        orchestration,
        "LocalAgentRunManager",
        lambda _db: SimpleNamespace(get=MagicMock(return_value=run)),
    )


def _verdict(status: str) -> dict[str, object]:
    return {
        "status": status,
        "criteria": [{"index": 1, "satisfied": status == "valid", "gap": None}],
        "feedback": status,
    }


def _write_oversized_codex_transcript(path: Path) -> None:
    record = (
        json.dumps(
            {
                "timestamp": "2026-09-05T04:00:00Z",
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_text", "text": "x" * 4000}],
                },
            },
            separators=(",", ":"),
        ).encode()
        + b"\n"
    )
    with path.open("wb") as transcript:
        while transcript.tell() <= _OVERSIZED_TRANSCRIPT_BYTES:
            transcript.write(record)


def _write_test_bootstrap(gobby_home: Path, database_url: str, port: int) -> None:
    files_home = gobby_home / "files"
    files_home.mkdir(parents=True)
    bootstrap = gobby_home / "bootstrap.yaml"
    bootstrap.write_text(
        "".join(
            (
                "datastore_mode: local\n",
                f"database_url: {json.dumps(database_url)}\n",
                f"daemon_port: {port}\n",
                "bind_host: 127.0.0.1\n",
                f"files_home: {json.dumps(str(files_home))}\n",
            )
        ),
        encoding="utf-8",
    )
    bootstrap.chmod(0o600)


@asynccontextmanager
async def _live_mcp_http_server(
    server: Any,
    *,
    health_observer: Callable[[], None] | None = None,
) -> AsyncIterator[int]:
    app = FastAPI()
    app.include_router(create_mcp_router())

    async def override_server() -> Any:
        return server

    async def health() -> dict[str, str]:
        if health_observer is not None:
            health_observer()
        return {"status": "ok"}

    app.dependency_overrides[get_server] = override_server
    app.dependency_overrides[get_metrics_manager] = lambda: None
    app.add_api_route("/api/health", health, methods=["GET"])
    config = uvicorn.Config(
        app,
        host="127.0.0.1",
        port=0,
        log_level="warning",
        access_log=False,
        lifespan="off",
        log_config=None,
        ws="none",
    )
    http = uvicorn.Server(config)
    task = asyncio.create_task(http.serve())

    async def wait_until_started() -> None:
        while not http.started:
            await asyncio.sleep(0)

    try:
        await wait_for_awaitable_or_background_task(
            wait_until_started(),
            task,
            timeout=5,
            description="live MCP HTTP server startup",
        )
        sockets = http.servers[0].sockets
        assert sockets is not None
        yield int(sockets[0].getsockname()[1])
    finally:
        http.should_exit = True
        await asyncio.wait_for(task, timeout=5)


def _initialize_request() -> dict[str, object]:
    return {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2025-06-18",
            "capabilities": {},
            "clientInfo": {"name": "close-review-boundary-test", "version": "1"},
        },
    }


def _submit_review_request(review_id: str, project_id: str) -> dict[str, object]:
    return {
        "jsonrpc": "2.0",
        "id": 2,
        "method": "tools/call",
        "params": {
            "name": "call_tool",
            "arguments": {
                "server_name": "gobby-tasks",
                "tool_name": "submit_close_review",
                "arguments": {"review_id": review_id, "verdict": _verdict("invalid")},
                "project_id": project_id,
            },
        },
    }


def _encode_pty_json(payload: dict[str, object]) -> bytes:
    return (json.dumps(payload, separators=(",", ":")) + "\n").encode()


def _write_pty_json(master_fd: int, payload: dict[str, object]) -> None:
    _write_pty_bytes(master_fd, _encode_pty_json(payload))


def _write_pty_bytes(master_fd: int, payload: bytes) -> None:
    view = memoryview(payload)
    while view:
        view = view[os.write(master_fd, view) :]


def _read_pty_response(master_fd: int, request_id: int, timeout: float) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    buffered = b""
    observed = bytearray()
    while time.monotonic() < deadline:
        remaining = max(0.0, deadline - time.monotonic())
        readable, _, _ = select.select([master_fd], [], [], remaining)
        if not readable:
            break
        try:
            chunk = os.read(master_fd, 65_536)
        except OSError as exc:
            raise AssertionError(
                f"stdio MCP process ended before response {request_id}: "
                f"{observed[-4000:].decode(errors='replace')!r}"
            ) from exc
        if not chunk:
            break
        observed.extend(chunk)
        buffered += chunk
        while b"\n" in buffered:
            raw_line, buffered = buffered.split(b"\n", 1)
            try:
                message = json.loads(raw_line.rstrip(b"\r"))
            except (json.JSONDecodeError, UnicodeDecodeError):
                continue
            if (
                isinstance(message, dict)
                and message.get("id") == request_id
                and ("result" in message or "error" in message)
            ):
                return cast(dict[str, Any], message)
    raise TimeoutError(
        f"stdio MCP response {request_id} exceeded {timeout:g}s; "
        f"output={observed[-4000:].decode(errors='replace')!r}"
    )


def _stop_stdio_process(process: subprocess.Popen[bytes], master_fd: int) -> None:
    os.close(master_fd)
    if process.poll() is None:
        process.terminate()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)


@pytest.mark.asyncio
async def test_close_retry_invalidates_wait_before_evidence_evaluation(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tasks = LocalTaskManager(temp_db)
    task = tasks.create_task(
        validation_criteria="Focused close retry regression passes",
        project_id=sample_project["id"],
        title="Retry before evidence",
    )
    caller = SessionManager(temp_db).register(
        external_id=f"close-retry-{uuid4()}",
        machine_id=require_machine_id(),
        source="codex",
        project_id=sample_project["id"],
    )
    store = TaskCloseReviewStore(temp_db)
    review, _ = store.create_or_get_active(
        **{
            **_persisted_review_intent(task, caller_session_id=caller.id),
            "close_arguments": {},
            "diff_sha": "d" * 64,
            "test_bodies_sha": "e" * 64,
        }
    )
    payload = agentic_close_review_module.build_terminal_review_payload(
        review,
        status="error",
        error_class="retryable_infrastructure",
    )
    store.finish(review.id, status="error", result_payload=payload)
    assert store.has_retry_wait(task.id, caller_session_id=caller.id) is True
    monkeypatch.setattr(orchestration, "get_current_session_id", lambda: caller.id)

    async def evaluate(_ctx: RegistryContext, **_kwargs: Any) -> CloseEvaluation:
        assert store.has_retry_wait(task.id, caller_session_id=caller.id) is False
        result = CloseEvaluation(task.id)
        result.error = "validation_failed"
        return result

    monkeypatch.setattr(close_tool, "_evaluate_close", evaluate)
    registry = InternalToolRegistry("tasks")
    close_tool.register_close_task(
        registry, cast(RegistryContext, SimpleNamespace(task_manager=tasks))
    )
    result = await registry.call("close_task", {"task_id": task.id})
    assert result["error"] == "validation_failed"
    assert (
        TaskCloseReviewStore(temp_db).has_retry_wait(task.id, caller_session_id=caller.id) is False
    )
    persisted = tasks.get_task(task.id)
    assert persisted is not None and persisted.closed_at is None
