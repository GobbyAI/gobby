from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest
import yaml

from gobby.agents.runner import AgentRunner
from gobby.config.app import DaemonConfig
from gobby.events.completion_registry import (
    CompletionEventRegistry,
    CompletionResultEvictedError,
)
from gobby.feedback import agent as agent_module
from gobby.feedback.agent import (
    FEEDBACK_REVIEWER_AGENT_NAME,
    FeedbackReviewerAgent,
    FeedbackReviewerLaunchError,
    FeedbackReviewerResultError,
    FeedbackReviewerRunError,
    FeedbackReviewerTimeoutError,
)
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.sessions import SessionManager

pytestmark = pytest.mark.unit

AGENT_PATH = (
    Path(__file__).parents[2] / "src/gobby/install/shared/workflows/agents/feedback-reviewer.yaml"
)


class _FakeRegistry:
    def __init__(self, error: Exception | None = None) -> None:
        self.error = error
        self.waits: list[tuple[str, float | None]] = []

    async def wait(self, run_id: str, timeout: float | None = None) -> dict[str, Any]:
        self.waits.append((run_id, timeout))
        if self.error is not None:
            raise self.error
        return {"status": "success"}


class _FakeRunner:
    def __init__(self, run: Any) -> None:
        self.run = run
        self.requested_run_ids: list[str] = []

    def get_run(self, run_id: str) -> Any:
        self.requested_run_ids.append(run_id)
        return self.run


def _reviewer(
    monkeypatch: pytest.MonkeyPatch,
    *,
    run_status: str = "success",
    run_error: str | None = None,
    current_state: str = '{"clusters": []}',
    spawn_result: dict[str, Any] | None = None,
    wait_error: Exception | None = None,
) -> tuple[FeedbackReviewerAgent, _FakeRegistry, dict[str, Any]]:
    captured: dict[str, Any] = {}

    def fake_resolve_agent(
        name: str,
        _db: HubDatabase,
        cli_source: str | None = None,
        project_id: str | None = None,
    ) -> Any:
        captured["resolved"] = (name, cli_source, project_id)
        return SimpleNamespace(name=name)

    async def fake_spawn_agent_impl(*args: Any, **kwargs: Any) -> dict[str, Any]:
        captured["spawn_args"] = args
        captured["spawn_kwargs"] = kwargs
        return spawn_result or {"success": True, "run_id": "agent-run-1"}

    monkeypatch.setattr(agent_module, "resolve_agent", fake_resolve_agent)
    monkeypatch.setattr(
        agent_module,
        "get_or_create_launcher_session",
        lambda *_args: "launcher-session-1",
    )
    monkeypatch.setattr(agent_module, "spawn_agent_impl", fake_spawn_agent_impl)
    monkeypatch.setattr(
        agent_module,
        "get_agent_end_handoff",
        lambda *_args: SimpleNamespace(
            payload=SimpleNamespace(current_state=current_state),
        ),
    )

    registry = _FakeRegistry(wait_error)
    runner = _FakeRunner(
        SimpleNamespace(status=run_status, error=run_error, result=None, started_at="started"),
    )
    reviewer = FeedbackReviewerAgent(
        db=cast(HubDatabase, object()),
        runner=cast(AgentRunner, runner),
        session_manager=cast(SessionManager, object()),
        completion_registry=cast(CompletionEventRegistry, registry),
        project_id="project-1",
        project_path="/tmp/gobby-project",
        git_manager=None,
        daemon_config=cast(DaemonConfig, object()),
    )
    return reviewer, registry, captured


@pytest.mark.asyncio
async def test_named_reviewer_launches_waits_and_reads_agent_end_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    findings = {
        "clusters": [
            {
                "observation_ids": ["feedback-1"],
                "cited_paths": [],
                "theme": "close retry friction",
                "classification": "defect",
                "proposed_task": None,
                "digest_note": "The issue is already resolved.",
            }
        ]
    }
    reviewer, registry, captured = _reviewer(
        monkeypatch,
        current_state=json.dumps(findings),
    )

    result = await reviewer.review("rendered observations", timeout_seconds=900.0)

    assert result.agent_run_id == "agent-run-1"
    assert result.findings == findings
    assert captured["resolved"] == (FEEDBACK_REVIEWER_AGENT_NAME, None, "project-1")
    spawn_args = captured["spawn_args"]
    spawn_kwargs = captured["spawn_kwargs"]
    assert spawn_args[0] == "rendered observations"
    assert spawn_kwargs["agent_lookup_name"] == FEEDBACK_REVIEWER_AGENT_NAME
    assert spawn_kwargs["parent_session_id"] == "launcher-session-1"
    assert spawn_kwargs["notify_parent_on_completion"] is True
    assert spawn_kwargs["isolation"] == "none"
    assert spawn_kwargs["timeout"] == 900.0
    assert registry.waits == [("agent-run-1", 930.0)]


@pytest.mark.asyncio
async def test_named_reviewer_reports_launch_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    reviewer, registry, _captured = _reviewer(
        monkeypatch,
        spawn_result={"success": False, "run_id": "agent-run-1", "error": "tmux unavailable"},
    )

    with pytest.raises(FeedbackReviewerLaunchError, match="tmux unavailable") as error:
        await reviewer.review("observations", timeout_seconds=20.0)

    assert error.value.agent_run_id == "agent-run-1"
    assert registry.waits == []


@pytest.mark.parametrize(
    ("detail", "retryable"),
    [("tmux: message too long", True), ("invalid provider configuration", False)],
)
async def test_serialized_launch_error_preserves_retry_classification(
    monkeypatch: pytest.MonkeyPatch, detail: str, retryable: bool
) -> None:
    reviewer, registry, _captured = _reviewer(
        monkeypatch,
        spawn_result={"success": False, "run_id": "agent-run-1", "error": detail},
    )
    with pytest.raises(FeedbackReviewerLaunchError) as error:
        await reviewer.review("frozen run", timeout_seconds=20.0)
    assert error.value.transient is retryable
    assert error.value.agent_run_id == "agent-run-1"
    assert detail in str(error.value)
    assert registry.waits == []


async def test_background_boot_error_is_a_retryable_launch_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reviewer, _registry, _captured = _reviewer(
        monkeypatch, run_status="cancelled", run_error="tmux: message too long"
    )
    assert reviewer.runner is not None
    run = reviewer.runner.get_run("agent-run-1")
    assert run is not None
    run.started_at = None
    with pytest.raises(FeedbackReviewerLaunchError) as error:
        await reviewer.review("frozen run", timeout_seconds=20.0)
    assert error.value.transient
    assert error.value.agent_run_id == "agent-run-1"


@pytest.mark.asyncio
async def test_named_reviewer_reports_terminal_agent_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reviewer, _registry, _captured = _reviewer(
        monkeypatch,
        run_status="error",
        run_error="reviewer process exited",
    )

    with pytest.raises(FeedbackReviewerRunError, match="reviewer process exited") as error:
        await reviewer.review("observations", timeout_seconds=20.0)

    assert error.value.agent_run_id == "agent-run-1"


@pytest.mark.asyncio
async def test_named_reviewer_reports_completion_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    reviewer, registry, _captured = _reviewer(
        monkeypatch,
        run_status="running",
        wait_error=TimeoutError(),
    )

    with pytest.raises(FeedbackReviewerTimeoutError, match="timed out") as error:
        await reviewer.review("observations", timeout_seconds=20.0)

    assert error.value.agent_run_id == "agent-run-1"
    assert registry.waits == [("agent-run-1", 50.0)]


@pytest.mark.asyncio
async def test_named_reviewer_rejects_malformed_handoff_json(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reviewer, _registry, _captured = _reviewer(
        monkeypatch,
        current_state="not-json",
    )

    with pytest.raises(FeedbackReviewerResultError, match="returned invalid JSON") as error:
        await reviewer.review("observations", timeout_seconds=20.0)

    assert error.value.agent_run_id == "agent-run-1"


@pytest.mark.asyncio
async def test_named_reviewer_accepts_evicted_notification_after_durable_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reviewer, _registry, _captured = _reviewer(
        monkeypatch,
        wait_error=CompletionResultEvictedError("cleaned after notify"),
    )

    result = await reviewer.review("observations", timeout_seconds=20.0)

    assert result.findings == {"clusters": []}


def test_feedback_reviewer_agent_requires_both_methodology_skills() -> None:
    document = yaml.safe_load(AGENT_PATH.read_text(encoding="utf-8"))

    assert document["name"] == FEEDBACK_REVIEWER_AGENT_NAME
    assert document["surfaces"] == ["spawn"]
    workflow = document["step_workflow"]
    assert workflow["exit_condition"] == "vars.review_complete"
    load_step, review_step = workflow["steps"]
    tracked_skills = {
        hook["when"]
        for hook in load_step["on_mcp_success"]
        if hook["server"] == "gobby-skills" and hook["tool"] == "get_skill"
    }
    assert tracked_skills == {
        "tool_input.name == 'restraint'",
        "tool_input.name == 'proportionality'",
    }
    assert load_step["transitions"] == [
        {
            "to": "review",
            "when": "vars.restraint_loaded and vars.proportionality_loaded",
        }
    ]
    assert review_step["allowed_mcp_tools"] == [
        "gobby-feedback:get_review_observations",
        "gobby-feedback:get_review_results",
        "gobby-agents:end_agent_run",
    ]
