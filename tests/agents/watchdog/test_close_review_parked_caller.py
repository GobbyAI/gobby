"""A caller parked in ``wait_for_agent`` on its close review survives the watchdogs (#20713).

End-to-end over the real seams: ``launch_close_review`` persists the review and
spawns the validator run; the caller parks through the real ``wait_for_agent``
tool; the idle and stuck watchdogs leave the parked caller alone; the validator
submits its verdict through ``submit_close_review``; terminal delivery resolves
the durable review payload and wakes the caller; the watchdogs resume, and the
caller can retry ``close_task`` after an invalid verdict.
"""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from gobby.agents.lifecycle_monitor import AgentLifecycleMonitor
from gobby.agents.terminal_delivery import deliver_and_cleanup_terminal_run
from gobby.autonomous.stuck_detector import StuckDetectionResult
from gobby.config.tmux import TmuxConfig
from gobby.events.completion_registry import CompletionEventRegistry
from gobby.mcp_proxy.tools.agents import create_agents_registry
from gobby.mcp_proxy.tools.tasks._context import RegistryContext
from gobby.mcp_proxy.tools.tasks._lifecycle_close_orchestration import (
    launch_close_review,
    submit_close_review,
)
from gobby.mcp_proxy.tools.tasks._lifecycle_close_preview import CloseEvaluation
from gobby.storage.agents import LocalAgentRunManager
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.sessions import SessionManager
from gobby.storage.task_close_reviews import TaskCloseReviewStore
from gobby.storage.tasks import Task
from gobby.tasks.agentic_close_review import TASK_CLOSE_VALIDATOR_AGENT
from gobby.utils.session_context import (
    reset_current_agent_run_id,
    session_context_for_test,
    set_current_agent_run_id,
)
from tests.agents.test_lifecycle_monitor import (
    DETECTION_REGISTRY,
    TerminalWakeRecorder,
    _local_machine_identity,  # noqa: F401  # autouse fixture re-export
    _make_terminal_run,
    _rid,
    agent_run_manager,  # noqa: F401  # fixture re-export
    sample_session,  # noqa: F401  # fixture re-export
)

pytestmark = pytest.mark.unit

PROMPT_PANE = "❯ \n"
STAGNANT = StuckDetectionResult(
    is_stuck=True,
    reason="No progress events for 634 seconds",
    layer="progress_stagnation",
    suggested_action="stop",
)
LIVE = StuckDetectionResult(is_stuck=False, reason="", layer=None, suggested_action="continue")


class _Harness:
    """One caller session with its terminal run, watchdog, and close-review context."""

    def __init__(
        self,
        *,
        runs: LocalAgentRunManager,
        temp_db: HubDatabase,
        session_manager: SessionManager,
        sample_project: dict[str, Any],
        session: dict[str, Any],
    ) -> None:
        self.db = temp_db
        self.runs = runs
        self.caller_session = str(session["id"])
        self.validator_session = str(
            session_manager.register(
                external_id="task-close-validator-session",
                machine_id=str(session["machine_id"]),
                source="claude",
                project_id=str(sample_project["id"]),
            ).id
        )
        self.project_id = str(sample_project["id"])
        self.wakes: list[tuple[str, str, dict[str, Any]]] = []
        self.completion_registry = CompletionEventRegistry(wake_callback=self._wake)
        self.stuck_detector = MagicMock()
        self.stuck_detector.is_stuck.side_effect = (
            lambda session_id: STAGNANT if session_id == self.caller_session else LIVE
        )
        self.monitor = AgentLifecycleMonitor(
            detection_registry=DETECTION_REGISTRY,
            agent_run_manager=runs,
            db=temp_db,
            stuck_detector=self.stuck_detector,
            completion_registry=self.completion_registry,
            tmux_config=TmuxConfig(
                idle_check_enabled=True, idle_timeout_seconds=10, max_reprompt_attempts=2
            ),
        )
        self.caller_run = _make_terminal_run(
            runs,
            session,
            run_id=_rid("run-close-review-caller"),
            terminal_id="gobby-close-review-caller",
            child_session_id=self.caller_session,
        )
        self.spawned: list[str] = []
        self.ctx = cast(
            RegistryContext,
            SimpleNamespace(
                task_manager=SimpleNamespace(db=temp_db),
                agent_registry=SimpleNamespace(call=self._spawn_validator),
                validation_config=None,
            ),
        )
        runner = MagicMock()
        runner.get_run.side_effect = runs.get
        self.agents = create_agents_registry(
            runner, db=temp_db, completion_registry=self.completion_registry
        )
        self.task = Task(
            id=str(uuid4()),
            project_id=self.project_id,
            title="Oversized close",
            priority=2,
            task_type="task",
            created_at=datetime(2026, 8, 22, tzinfo=UTC),
            updated_at=datetime(2026, 8, 22, tzinfo=UTC),
            seq_num=4242,
            validation_criteria="Criterion.",
        )

    async def _wake(self, session_id: str, message: str, result: dict[str, Any]) -> dict[str, Any]:
        """The daemon's wake dispatch: the payload the parked caller receives."""
        self.wakes.append((session_id, message, result))
        return {"ism_persisted": True}

    async def _spawn_validator(self, tool: str, arguments: dict[str, Any]) -> dict[str, Any]:
        assert tool == "spawn_agent"
        assert arguments["agent"] == TASK_CLOSE_VALIDATOR_AGENT
        assert arguments["notify_parent_on_completion"] is True
        run = self.runs.create(
            parent_session_id=arguments["parent_session_id"],
            provider="claude",
            prompt=arguments["prompt"],
            agent_name=arguments["agent"],
            child_session_id=self.validator_session,
            run_id=_rid(f"validator-run-{len(self.spawned)}"),
        )
        self.runs.start(run.id)
        self.spawned.append(run.id)
        return {"success": True, "run_id": run.id}

    def _evaluation(self, *, ready: bool) -> CloseEvaluation:
        evaluation = CloseEvaluation(f"#{self.task.seq_num}")
        evaluation.task = self.task
        evaluation.task_id = self.task.id
        evaluation.resolved_session_id = self.caller_session
        evaluation.repo_path = "/repo"
        evaluation.commit_shas = ["abc"]
        evaluation.extra.update(
            {"review_fingerprint": "close", "deterministic_evidence_fingerprint": "evidence"}
        )
        if ready:
            evaluation.pass_gate(14, "criteria_review", "valid")
        else:
            evaluation.error = "agentic_review_required"
        return evaluation

    def _reviewed(self, status: str) -> CloseEvaluation:
        """The close gates re-run with the validator's verdict consumed."""
        if status == "valid":
            return self._evaluation(ready=True)
        evaluation = self._evaluation(ready=False)
        evaluation.error = "validation_failed"
        evaluation.message = "Criterion 1 is unmet."
        evaluation.validation_status = "invalid"
        evaluation.extra["blocking_reasons"] = ["Criterion 1 is unmet."]
        return evaluation

    async def close_task(self) -> dict[str, Any]:
        """The caller's close_task once the deterministic gates report oversized evidence."""
        return await launch_close_review(
            self.ctx,
            evaluation=self._evaluation(ready=False),
            close_arguments={
                "task_id": f"#{self.task.seq_num}",
                "reason": "completed",
                "changes_summary": "Implemented.",
                "commit_sha": "abc",
                "project_path": "/repo",
                "preview": True,
            },
        )

    async def wait_for_agent(self, run_id: str) -> dict[str, Any]:
        with session_context_for_test(self.caller_session):
            result = await self.agents._tools["wait_for_agent"].func(run_id)
        return cast(dict[str, Any], result)

    async def validator_submits(self, run_id: str, review_id: str, status: str) -> dict[str, Any]:
        """The validator's submit_close_review under its own run and session identity."""
        evaluate: Callable[..., Awaitable[CloseEvaluation]] = AsyncMock(
            return_value=self._reviewed(status)
        )
        commit = AsyncMock(
            return_value={
                "success": True,
                "closed": True,
                "task_id": self.task.id,
                "commit_shas": ["abc"],
            }
        )
        token = set_current_agent_run_id(run_id)
        try:
            with session_context_for_test(self.validator_session):
                return await submit_close_review(
                    self.ctx,
                    review_id=review_id,
                    verdict={
                        "status": status,
                        "criteria": [{"index": 1, "satisfied": status == "valid", "gap": None}],
                        "feedback": status,
                    },
                    evaluate_close=evaluate,
                    commit_close=commit,
                )
        finally:
            reset_current_agent_run_id(token)

    async def validator_run_ends(self, run_id: str) -> dict[str, bool] | None:
        """The runner's terminal path: resolve the durable review payload and notify."""
        self.runs.complete(run_id, result="verdict submitted")

        async def run_db(fn: Callable[..., Any], *args: Any) -> Any:
            return fn(*args)

        return await deliver_and_cleanup_terminal_run(
            db=self.db,
            completion_registry=self.completion_registry,
            run_id=run_id,
            result=None,
            message="",
            run_db=run_db,
        )

    def age_idle_state(self) -> None:
        self.monitor._idle_detector.get_state(self.caller_run.id).first_idle_at = (
            time.monotonic() - 360
        )

    async def watchdogs_tick(self) -> tuple[int, int, list[str], int]:
        """Run both watchdogs against a bare prompt; return (idle, stuck, keys, cleanups)."""
        self.age_idle_state()
        with (
            patch.object(
                self.monitor._tmux,
                "capture_pane",
                new_callable=AsyncMock,
                return_value=PROMPT_PANE,
            ),
            patch.object(self.monitor._tmux, "send_keys", new=TerminalWakeRecorder()) as wake,
            patch.object(
                self.monitor._cleanup_handler, "cleanup_agent", new_callable=AsyncMock
            ) as cleanup_agent,
        ):
            idle = await self.monitor.check_idle_agents()
            stuck = await self.monitor.check_autonomous_stuck_agents()
        return (
            idle,
            stuck,
            [keys for _session, keys, _literal in wake.calls],
            cleanup_agent.await_count,
        )


@pytest.fixture
def harness(
    agent_run_manager: LocalAgentRunManager,  # noqa: F811
    temp_db: HubDatabase,
    session_manager: SessionManager,
    sample_project: dict[str, Any],
    sample_session: dict[str, Any],  # noqa: F811
) -> _Harness:
    return _Harness(
        runs=agent_run_manager,
        temp_db=temp_db,
        session_manager=session_manager,
        sample_project=sample_project,
        session=sample_session,
    )


async def test_caller_parked_on_its_close_review_survives_until_the_verdict_lands(
    harness: _Harness,
) -> None:
    launched = await harness.close_task()
    assert launched["error"] == "agentic_review_required"
    assert "run_id" not in launched, "the caller must not receive a pollable run handle"
    [validator_run_id] = harness.spawned

    waited = await harness.wait_for_agent(validator_run_id)
    assert waited["success"] is True
    assert waited["completed"] is False
    assert waited["notification_registered"] is True
    assert harness.completion_registry.is_awaiting(harness.caller_session) is True

    # Parked: the bare prompt reads idle and the detector reports stagnation, yet
    # neither watchdog touches the caller across repeated ticks.
    for _tick in range(2):
        assert await harness.watchdogs_tick() == (0, 0, [], 0)
    caller = harness.runs.get(harness.caller_run.id)
    assert caller is not None and caller.status == "running"

    submitted = await harness.validator_submits(validator_run_id, launched["review_id"], "valid")
    assert submitted["success"] is True
    assert submitted["review_status"] == "closed"
    assert submitted["terminal_payload"]["event"] == "task_close_review_completed"

    delivery = await harness.validator_run_ends(validator_run_id)
    assert delivery == {harness.caller_session: True}
    [(woken_session, _message, delivered)] = harness.wakes
    assert woken_session == harness.caller_session
    assert delivered["event"] == "task_close_review_completed"
    assert (delivered["status"], delivered["closed"]) == ("closed", True)
    assert delivered["run_id"] == validator_run_id
    assert harness.completion_registry.is_awaiting(harness.caller_session) is False

    # The wait resolved: the next wait_for_agent returns the finished run outright,
    # and ordinary watchdog handling resumes on the caller.
    resolved = await harness.wait_for_agent(validator_run_id)
    assert resolved["completed"] is True
    assert resolved["notification_registered"] is False
    idle, stuck, keys, cleanups = await harness.watchdogs_tick()
    assert (idle, keys[:1]) == (1, ["Escape"])
    assert (stuck, cleanups) == (1, 1)


async def test_caller_retries_close_task_after_an_invalid_verdict(harness: _Harness) -> None:
    first = await harness.close_task()
    assert "run_id" not in first
    [first_run_id] = harness.spawned
    await harness.wait_for_agent(first_run_id)
    assert await harness.watchdogs_tick() == (0, 0, [], 0)

    submitted = await harness.validator_submits(first_run_id, first["review_id"], "invalid")
    assert submitted["review_status"] == "invalid"
    await harness.validator_run_ends(first_run_id)
    [(woken_session, _message, delivered)] = harness.wakes
    assert woken_session == harness.caller_session
    assert (delivered["status"], delivered["closed"]) == ("invalid", False)
    assert delivered["blocking_reasons"] == ["Criterion 1 is unmet."]
    assert delivered["required_actions"] == [
        "Address every blocking reason, rerun focused validation, commit fixes, "
        "and call close_task again."
    ]
    assert harness.completion_registry.is_awaiting(harness.caller_session) is False

    # A retry while the first review is terminal launches a fresh validator, and the
    # caller parks again on the new run.
    retry = await harness.close_task()
    assert retry["error"] == "agentic_review_required"
    assert retry["review_id"] != first["review_id"]
    assert "run_id" not in retry
    retry_run_id = harness.spawned[-1]
    assert retry_run_id != first_run_id
    assert harness.spawned == [first_run_id, retry_run_id]
    store = TaskCloseReviewStore(harness.db)
    assert {
        r.status for r in (store.get(first["review_id"]), store.get(retry["review_id"])) if r
    } == {
        "invalid",
        "running",
    }

    waited = await harness.wait_for_agent(retry_run_id)
    assert waited["notification_registered"] is True
    assert await harness.watchdogs_tick() == (0, 0, [], 0)
