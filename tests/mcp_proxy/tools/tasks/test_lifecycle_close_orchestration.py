"""Automated task-close review orchestration tests."""

from __future__ import annotations

import asyncio
import json
import os
import pty
import select
import subprocess
import time
from collections.abc import AsyncIterator, Callable
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

import gobby.mcp_proxy.tools.tasks._lifecycle_close as close_module
import gobby.mcp_proxy.tools.tasks._lifecycle_close_orchestration as orchestration
from gobby.config.tasks import TaskValidationConfig
from gobby.mcp_proxy.tools.internal import InternalRegistryManager
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
    VALIDATOR_RUN_ENDED_SUCCESS_ERROR,
    TaskCloseReview,
    TaskCloseReviewStatus,
    TaskCloseReviewStore,
)
from gobby.storage.tasks import LocalTaskManager, Task
from gobby.tasks import agentic_close_review as agentic_close_review_module
from gobby.tasks.close_review_delivery import terminal_review_delivery
from gobby.utils.machine_id import require_machine_id

pytestmark = pytest.mark.unit

_PERSISTED_TASK_ID = "00000000-0000-4000-8000-000000002608"
_PERSISTED_SESSION_ID = "00000000-0000-4000-8000-000000002609"
_FIRST_REVIEW_RUN_ID = "00000000-0000-4000-8000-000000002610"
_SECOND_REVIEW_RUN_ID = "00000000-0000-4000-8000-000000002611"
_REQUIRED_EVIDENCE = "Run the real close adapter and capture its MCP response receipt."
_OVERSIZED_TRANSCRIPT_BYTES = 10 * 1024 * 1024
_SUBMIT_DEADLINE_SECONDS = 10.0
_STDIO_DEFAULT_PREFLIGHT_PATH = "/api/health"


@pytest.mark.asyncio
async def test_close_persists_and_launches_one_taskless_validator(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _Store(_review(status="launching", run_id=None))
    registry = SimpleNamespace(call=AsyncMock(return_value={"success": True, "run_id": "run"}))
    ctx = _ctx(registry=registry)
    monkeypatch.setattr(orchestration, "TaskCloseReviewStore", lambda _db: store)
    evaluation = _evaluation()
    evaluation.extra["coordinator_owned_pending"] = True
    arguments = _arguments()

    result = await launch_close_review(ctx, evaluation=evaluation, close_arguments=arguments)

    assert store.created_arguments == arguments
    registry.call.assert_awaited_once()
    launch_args = registry.call.call_args.args[1]
    assert launch_args["agent"] == "task-close-validator"
    assert launch_args["task_id"] is None
    assert launch_args["isolation"] == "none"
    assert launch_args["provider"] == "codex"
    assert launch_args["model"] == "gpt-5.6-terra"
    # An unpinned candidate inherits the profile default, which is always `auto`.
    assert launch_args["reasoning_effort"] == "auto"
    assert "close caller is a spawned agent" in launch_args["prompt"]
    assert "state `pending_external`" in launch_args["prompt"]
    assert result["success"] is True
    assert result["closed"] is False
    assert result["can_close"] is False
    assert result["error"] == "agentic_review_required"
    assert result["review_status"] == "running"
    assert "run_id" not in result
    assert "spawn_request" not in result
    assert "review_run_id" not in result
    assert "Do not poll agent runs or re-call close_task." in result["message"]
    assert "Oversized" not in result["message"]
    assert result["criteria_review_duration_ms"] == 4.25


@pytest.mark.asyncio
async def test_launch_prompt_carries_gate10_validation_facts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The evaluation's gate-10 record reaches the validator through its launch prompt."""
    store = _Store(_review(status="launching", run_id=None))
    registry = SimpleNamespace(call=AsyncMock(return_value={"success": True, "run_id": "run"}))
    ctx = _ctx(registry=registry)
    monkeypatch.setattr(orchestration, "TaskCloseReviewStore", lambda _db: store)
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

    result = await launch_close_review(ctx, evaluation=evaluation, close_arguments=_arguments())

    launch_prompt = registry.call.await_args.args[1]["prompt"]
    assert "validation_commands=" in launch_prompt
    assert "uv run pytest tests/tasks/ -q" in launch_prompt
    assert "gate 10's authoritative transcript record" in launch_prompt
    assert result["validation_commands"] == evaluation.extra["validation_commands"]
    assert result["validation_commands"]["uncredited_runs"][0]["reason"] == "wrapped"


@pytest.mark.asyncio
@pytest.mark.integration
async def test_launch_after_rejected_verdict_carries_required_evidence(
    temp_db: HubDatabase,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = TaskCloseReviewStore(temp_db)
    task_manager = SimpleNamespace(db=temp_db)
    task = Task(
        id=_PERSISTED_TASK_ID,
        project_id="00000000-0000-4000-8000-000000002612",
        title="Carry prior close evidence",
        priority=1,
        task_type="task",
        validation_criteria="Focused tests pass.",
        created_at=datetime(2026, 8, 22, tzinfo=UTC),
        updated_at=datetime(2026, 8, 22, tzinfo=UTC),
        seq_num=42,
    )
    evaluation = CloseEvaluation("#42")
    evaluation.task = task
    evaluation.task_id = task.id
    evaluation.resolved_session_id = _PERSISTED_SESSION_ID
    evaluation.repo_path = "/repo"
    evaluation.commit_shas = ["def"]
    evaluation.error = "agentic_review_required"
    evaluation.extra.update(
        {
            "review_fingerprint": "first-review",
            "deterministic_evidence_fingerprint": "first-evidence",
            "diff_sha": "a" * 64,
            "test_bodies_sha": "b" * 64,
            "stable_facts": {"commit_shas": ["def"]},
        }
    )
    registry = SimpleNamespace(
        call=AsyncMock(return_value={"success": True, "run_id": _FIRST_REVIEW_RUN_ID})
    )
    ctx = cast(
        RegistryContext,
        SimpleNamespace(
            task_manager=task_manager,
            agent_registry=registry,
            validation_config=TaskValidationConfig(),
        ),
    )

    await launch_close_review(ctx, evaluation=evaluation, close_arguments=_arguments())

    first_launch_prompt = registry.call.await_args.args[1]["prompt"]
    assert "prior_requirements=" not in first_launch_prompt
    first_review = store.get_active_for_task(_PERSISTED_TASK_ID)
    assert first_review is not None
    assert first_review.agent_run_id == _FIRST_REVIEW_RUN_ID

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
        evaluation = CloseEvaluation("#42")
        evaluation.task_id = _PERSISTED_TASK_ID
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
    assert terminal.result_payload["verdict"] == submitted_verdict

    registry = SimpleNamespace(
        call=AsyncMock(return_value={"success": True, "run_id": _SECOND_REVIEW_RUN_ID})
    )
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

    await launch_close_review(ctx, evaluation=evaluation, close_arguments=_arguments())

    launch_prompt = registry.call.await_args.args[1]["prompt"]
    assert f"Required evidence: {_REQUIRED_EVIDENCE}" in launch_prompt
    assert "prior_requirements=" in launch_prompt
    assert 'changes_summary="Implemented."' in launch_prompt


@pytest.mark.asyncio
async def test_launch_omits_model_overrides_without_validation_config(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _Store(_review(status="launching", run_id=None))
    registry = SimpleNamespace(call=AsyncMock(return_value={"success": True, "run_id": "run"}))
    ctx = cast(
        RegistryContext,
        SimpleNamespace(
            task_manager=SimpleNamespace(db=object()),
            agent_registry=registry,
            validation_config=None,
        ),
    )
    monkeypatch.setattr(orchestration, "TaskCloseReviewStore", lambda _db: store)

    await launch_close_review(ctx, evaluation=_evaluation(), close_arguments=_arguments())

    launch_args = registry.call.call_args.args[1]
    assert "provider" not in launch_args
    assert "model" not in launch_args
    assert "reasoning_effort" not in launch_args


@pytest.mark.asyncio
async def test_failed_validator_launch_remains_unsuccessful(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _Store(_review(status="launching", run_id=None))
    registry = SimpleNamespace(
        call=AsyncMock(return_value={"success": False, "error": "validator unavailable"})
    )
    monkeypatch.setattr(orchestration, "TaskCloseReviewStore", lambda _db: store)

    result = await launch_close_review(
        _ctx(registry=registry),
        evaluation=_evaluation(),
        close_arguments=_arguments(),
    )

    assert result["success"] is False
    assert result["closed"] is False
    assert result["can_close"] is False
    assert result["error"] == "agentic_review_launch_failed"
    assert result["review_status"] == "error"
    assert store.finished_status == "error"


@pytest.mark.asyncio
async def test_missing_agent_registry_finishes_launch_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _Store(_review(status="launching", run_id=None))
    monkeypatch.setattr(orchestration, "TaskCloseReviewStore", lambda _db: store)

    result = await launch_close_review(
        _ctx(registry=None),
        evaluation=_evaluation(),
        close_arguments=_arguments(),
    )

    assert result["error"] == "agentic_review_launch_failed"
    assert result["closed"] is False
    assert store.finished_status == "error"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "launch",
    [
        pytest.param(RuntimeError("spawn failed"), id="spawn-exception"),
        pytest.param({"success": True}, id="missing-run-id"),
    ],
)
async def test_incomplete_spawn_finishes_launch_error(
    monkeypatch: pytest.MonkeyPatch,
    launch: Exception | dict[str, object],
) -> None:
    store = _Store(_review(status="launching", run_id=None))
    call = (
        AsyncMock(side_effect=launch)
        if isinstance(launch, Exception)
        else AsyncMock(return_value=launch)
    )
    monkeypatch.setattr(orchestration, "TaskCloseReviewStore", lambda _db: store)

    result = await launch_close_review(
        _ctx(registry=SimpleNamespace(call=call)),
        evaluation=_evaluation(),
        close_arguments=_arguments(),
    )

    assert result["error"] == "agentic_review_launch_failed"
    assert result["closed"] is False
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
def test_validator_spawn_overrides_follow_first_validation_candidate(
    candidates: list[object],
    profile: str,
    expected: dict[str, str | None],
) -> None:
    config = TaskValidationConfig(candidates=candidates, profile=profile)

    overrides = agentic_close_review_module.validator_spawn_overrides(config)

    assert overrides == expected


def test_validator_spawn_overrides_are_empty_without_config() -> None:
    assert agentic_close_review_module.validator_spawn_overrides(None) == {}


@pytest.mark.asyncio
async def test_concurrent_close_reuses_active_review(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    existing = _review(status="running", run_id="same-run")
    store = _Store(existing, created=False)
    registry = SimpleNamespace(call=AsyncMock())
    monkeypatch.setattr(orchestration, "TaskCloseReviewStore", lambda _db: store)

    result = await launch_close_review(
        _ctx(registry=registry),
        evaluation=_evaluation(),
        close_arguments=_arguments(),
    )

    assert result["success"] is True
    assert result["closed"] is False
    assert result["can_close"] is False
    assert result["error"] == "agentic_review_pending"
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
@pytest.mark.parametrize("validator_ended", [False, True], ids=["running", "late-success"])
async def test_submit_close_review_claims_before_heavy_work(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    validator_ended: bool,
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
    validator = session_manager.register(
        external_id=str(uuid4()),
        machine_id=require_machine_id(),
        source="codex",
        project_id=sample_project["id"],
        transcript_path=str(tmp_path / "validator.jsonl"),
        parent_session_id=caller.id,
        is_local=True,
        workspace_path=str(tmp_path),
    )
    run_manager = LocalAgentRunManager(temp_db)
    run = run_manager.create(
        parent_session_id=caller.id,
        provider="codex",
        prompt="Validate the task close.",
        agent_name=agentic_close_review_module.TASK_CLOSE_VALIDATOR_AGENT,
        child_session_id=validator.id,
        run_id=str(uuid4()),
    )
    assert run_manager.start(run.id) is not None

    task = LocalTaskManager(temp_db).create_task(
        project_id=sample_project["id"],
        title="Review the oversized transcript",
        created_in_session_id=caller.id,
        validation_criteria=_REQUIRED_EVIDENCE,
    )
    store = TaskCloseReviewStore(temp_db)
    review, created = store.create_or_get_active(
        **{
            **_persisted_review_intent(),
            "task_id": task.id,
            "task_ref": f"#{task.seq_num}",
            "close_arguments": {**_arguments(), "task_id": task.id},
            "caller_session_id": caller.id,
            "review_fingerprint": "oversized-transcript-review",
            "evidence_fingerprint": "oversized-transcript-evidence",
        }
    )
    assert created is True
    assert store.bind_run(review.id, run.id) is not None
    initial_status: TaskCloseReviewStatus = "running"
    if validator_ended:
        completed_run = run_manager.complete(run.id)
        assert completed_run is not None
        assert completed_run.status == "success"
        delivery = terminal_review_delivery(temp_db, run.id)
        assert delivery is not None
        assert delivery[1] == VALIDATOR_RUN_ENDED_SUCCESS_ERROR
        abandoned = store.get(review.id)
        assert abandoned is not None
        assert abandoned.status == "error"
        assert abandoned.error == VALIDATOR_RUN_ENDED_SUCCESS_ERROR
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

    monkeypatch.setattr(close_module, "_evaluate_close", evaluate_close)
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
        environment.pop("GOBBY_CONFIG_FILE", None)
        environment.update(
            {
                "GOBBY_HOME": str(gobby_home),
                "GOBBY_AGENT_RUN_ID": run.id,
                "GOBBY_SESSION_ID": validator.id,
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
    run_end_message = "Task-close validator run ended with status success before finalization."
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
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = TaskCloseReviewStore(temp_db)
    old_review, _created = store.create_or_get_active(**_persisted_review_intent())
    assert store.bind_run(old_review.id, _FIRST_REVIEW_RUN_ID) is not None
    prior_payload = {
        "event": "task_close_review_completed",
        "status": "error",
        "message": VALIDATOR_RUN_ENDED_SUCCESS_ERROR,
    }
    assert (
        store.finish(
            old_review.id,
            status="error",
            result_payload=prior_payload,
            error=VALIDATOR_RUN_ENDED_SUCCESS_ERROR,
        )
        is not None
    )
    newer, created = store.create_or_get_active(
        **{
            **_persisted_review_intent(),
            "review_fingerprint": "newer-review",
            "evidence_fingerprint": "newer-evidence",
        }
    )
    assert created is True
    assert store.bind_run(newer.id, _SECOND_REVIEW_RUN_ID) is not None
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
async def test_wrong_validator_run_is_rejected_without_transition(
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
    def __init__(self, review: TaskCloseReview, *, created: bool = True) -> None:
        self.review = review
        self.created = created
        self.created_arguments: dict[str, Any] | None = None
        self.finished_status: str | None = None
        self.claimed = False
        self.restored = False

    def create_or_get_active(self, **kwargs: Any) -> tuple[TaskCloseReview, bool]:
        self.created_arguments = dict(kwargs["close_arguments"])
        return self.review, self.created

    def bind_run(self, _review_id: str, run_id: str) -> TaskCloseReview:
        self.review = replace(self.review, status="running", agent_run_id=run_id)
        return self.review

    def get(self, _review_id: str) -> TaskCloseReview:
        return self.review

    def claim_finalizing(self, _review_id: str, _run_id: str) -> TaskCloseReview:
        self.claimed = True
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
            task_manager=SimpleNamespace(db=object()),
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
            "criteria_review_duration_ms": 4.25,
        }
    )
    if ready:
        evaluation.pass_gate(13, "criteria_review", "valid")
    else:
        evaluation.error = "agentic_review_required"
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
        "preview": True,
        "response_detail": "concise",
    }


def _persisted_review_intent() -> dict[str, Any]:
    return {
        "task_id": _PERSISTED_TASK_ID,
        "task_ref": "#42",
        "caller_session_id": _PERSISTED_SESSION_ID,
        "close_arguments": _arguments(),
        "review_fingerprint": "review",
        "evidence_fingerprint": "evidence",
        "diff_sha": "a" * 64,
        "test_bodies_sha": "b" * 64,
        "stable_facts": {},
    }


def _authenticate(monkeypatch: pytest.MonkeyPatch, review: TaskCloseReview) -> None:
    run = SimpleNamespace(
        agent_name="task-close-validator",
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
    try:
        while not http.started:
            if task.done():
                await task
            await asyncio.sleep(0)
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
