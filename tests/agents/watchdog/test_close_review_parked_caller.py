"""A caller parked in ``wait_for_agent`` on its close review survives lifecycle sweeps.

End-to-end over the real seams: ``launch_close_review`` persists the review and
spawns the validator run; the caller parks through the real ``wait_for_agent``
tool; the idle and stuck watchdogs leave the parked caller alone; the validator
submits its verdict through ``submit_close_review``; terminal delivery resolves
the durable review payload and wakes the caller; a valid verdict leaves time for
cooperative ``end_agent_run`` while abandoned callers retain a bounded fallback.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gobby.agents.completion_subscribers import subscribe_agent_completion
from gobby.agents.lifecycle_monitor import AgentLifecycleMonitor
from gobby.agents.terminal_delivery import deliver_and_cleanup_terminal_run
from gobby.autonomous.progress_tracker import ProgressType
from gobby.autonomous.stuck_detector import StuckDetectionResult
from gobby.config.tmux import TmuxConfig
from gobby.events.completion_registry import CompletionEventRegistry
from gobby.hooks.events import HookEvent, HookEventType, SessionSource
from gobby.mcp_proxy.tools.agents import create_agents_registry
from gobby.mcp_proxy.tools.tasks._context import RegistryContext
from gobby.mcp_proxy.tools.tasks._lifecycle_close_orchestration import (
    launch_close_review,
    submit_close_review,
)
from gobby.mcp_proxy.tools.tasks._lifecycle_close_preview import CloseEvaluation
from gobby.sessions.handoff_records import get_agent_end_handoff
from gobby.storage.agents import LocalAgentRunManager
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.pipeline_subscribers import CompletionSubscriberManager
from gobby.storage.sessions import SessionManager
from gobby.storage.task_close_reviews import TaskCloseReviewStore
from gobby.storage.tasks import LocalTaskManager
from gobby.tasks.agentic_close_review import TASK_CLOSE_VALIDATOR_AGENT
from gobby.utils.session_context import (
    reset_current_agent_run_id,
    session_context_for_test,
    set_current_agent_run_id,
)
from gobby.workflows.engine.core import RuleEngine
from gobby.workflows.sync_rules import get_bundled_rules_path, sync_bundled_rules
from tests.agents.test_lifecycle_monitor import (
    DETECTION_REGISTRY,
    _fake_terminal_services,
    _make_terminal_run,
    _pane_text,
    _rid,
    _runtime_of,
)

pytestmark = pytest.mark.unit

PROMPT_PANE = "❯ \n"
STAGNANT = StuckDetectionResult(
    is_stuck=True,
    reason="No progress events for 634 seconds",
    layer="progress_stagnation",
    suggested_action="stop",
)
PASSIVE_WAIT = StuckDetectionResult(
    is_stuck=True,
    reason="Repeated passive wait",
    layer="tool_loop",
    details={"passive_wait": True},
    suggested_action="change_approach",
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
        self.parent_session = str(session["id"])
        self.project_id = str(sample_project["id"])
        self.caller_session = str(
            session_manager.register(
                external_id="task-close-caller-session",
                machine_id=str(session["machine_id"]),
                source="claude",
                project_id=self.project_id,
                parent_session_id=self.parent_session,
                agent_depth=1,
            ).id
        )
        self.validator_session = str(
            session_manager.register(
                external_id="task-close-validator-session",
                machine_id=str(session["machine_id"]),
                source="claude",
                project_id=self.project_id,
                parent_session_id=self.caller_session,
                agent_depth=2,
            ).id
        )
        self.task_manager = LocalTaskManager(temp_db)
        self.task = self.task_manager.create_task(
            project_id=self.project_id,
            title="Oversized close",
            validation_criteria="Criterion.",
        )
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
            task_manager=self.task_manager,
            tmux_config=TmuxConfig(
                idle_check_enabled=True, idle_timeout_seconds=10, max_reprompt_attempts=2
            ),
            terminal_services=_fake_terminal_services(temp_db),
        )
        self.caller_run = _make_terminal_run(
            runs,
            session,
            run_id=_rid("run-close-review-caller"),
            terminal_id="gobby-close-review-caller",
            child_session_id=self.caller_session,
            task_id=self.task.id,
        )
        self.spawned: list[str] = []
        self.ctx = cast(
            RegistryContext,
            SimpleNamespace(
                task_manager=self.task_manager,
                agent_registry=SimpleNamespace(call=self._spawn_validator),
                validation_config=None,
            ),
        )
        runner = MagicMock()
        runner.run_storage = runs
        runner.get_run.side_effect = runs.get
        runner.complete_run.side_effect = runs.complete
        runner.terminal_runtime_registry = self.monitor._terminal_services.registry
        self.runner = runner
        self.agents = create_agents_registry(
            runner,
            db=temp_db,
            completion_registry=self.completion_registry,
            session_manager=session_manager,
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
        await asyncio.to_thread(
            subscribe_agent_completion,
            completion_registry=self.completion_registry,
            run_id=run.id,
            subscriber_session_id=str(arguments["parent_session_id"]),
            db=self.db,
            strict=True,
        )
        return {"success": True, "run_id": run.id}

    def _evaluation(self, *, ready: bool) -> CloseEvaluation:
        evaluation = CloseEvaluation(f"#{self.task.seq_num}")
        evaluation.task = self.task
        evaluation.task_id = self.task.id
        evaluation.resolved_session_id = self.caller_session
        evaluation.repo_path = "/repo"
        evaluation.commit_shas = ["abc"]
        evaluation.extra.update(
            {
                "review_fingerprint": "close",
                "deterministic_evidence_fingerprint": "evidence",
                "diff_sha": "a" * 64,
                "test_bodies_sha": "b" * 64,
                "stable_facts": {"commit_shas": ["abc"]},
                "criterion_count": 1,
            }
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
                "preview": False,
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

        async def commit_close(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
            self.task_manager.close_task(
                self.task.id,
                reason="Done",
                closed_commit_sha="abc",
            )
            return {
                "success": True,
                "closed": True,
                "task_id": self.task.id,
                "commit_shas": ["abc"],
            }

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
                    commit_close=commit_close,
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

    async def cooperative_end(self) -> dict[str, Any]:
        """Persist the caller's structured report through the real lifecycle tool."""
        token = set_current_agent_run_id(self.caller_run.id)
        try:
            with session_context_for_test(self.caller_session):
                result = await self.agents._tools["end_agent_run"].func(
                    current_state="The async close verdict was received and the task is closed.",
                    next_steps=["Review and land the committed task branch."],
                    what_was_accomplished=[
                        "Validated the async close and preserved cooperative handoff delivery."
                    ],
                    references=[f"#{self.task.seq_num}"],
                )
        finally:
            reset_current_agent_run_id(token)
        return cast(dict[str, Any], result)

    def age_close_review_delivery(self) -> None:
        """Move the delivered verdict beyond the existing stagnation boundary."""
        self.db.execute(
            """
            UPDATE loop_progress
            SET recorded_at = %s
            WHERE session_id = %s AND progress_type = %s
            """,
            (
                (datetime.now(UTC) - timedelta(seconds=601)).isoformat(),
                self.caller_session,
                ProgressType.TASK_CLOSE_REVIEW_COMPLETED.value,
            ),
        )

    def age_idle_state(self) -> None:
        self.monitor._idle_detector.get_state(self.caller_run.id).first_idle_at = (
            time.monotonic() - 360
        )

    async def watchdogs_tick(self) -> tuple[int, int, list[str], int]:
        """Run both watchdogs against a bare prompt; return (idle, stuck, keys, cleanups)."""
        self.age_idle_state()
        runtime = _runtime_of(self.monitor)
        runtime.write_log.clear()
        with (
            _pane_text(self.monitor, PROMPT_PANE),
            patch.object(
                self.monitor._cleanup_handler, "cleanup_agent", new_callable=AsyncMock
            ) as cleanup_agent,
        ):
            idle = await self.monitor.check_idle_agents()
            stuck = await self.monitor.check_autonomous_stuck_agents()
        return (
            idle,
            stuck,
            [payload for _kind, payload in runtime.write_log if isinstance(payload, str)],
            cleanup_agent.await_count,
        )


@pytest.fixture
def harness(
    agent_run_manager: LocalAgentRunManager,
    temp_db: HubDatabase,
    session_manager: SessionManager,
    sample_project: dict[str, Any],
    sample_session: dict[str, Any],
) -> _Harness:
    return _Harness(
        runs=agent_run_manager,
        temp_db=temp_db,
        session_manager=session_manager,
        sample_project=sample_project,
        session=sample_session,
    )


async def test_closed_task_waits_for_verdict_and_cooperative_structured_handoff(
    harness: _Harness,
) -> None:
    launched = await harness.close_task()
    assert launched["error"] == "agentic_review_required"
    validator_run_id = launched["validator_run_id"]
    assert validator_run_id == harness.spawned[0]
    subscribers = CompletionSubscriberManager(harness.db)
    assert subscribers.get_completion_subscribers(validator_run_id) == [harness.caller_session]

    waited = await harness.wait_for_agent(validator_run_id)
    assert waited["success"] is True
    assert waited["completed"] is False
    assert waited["notification_registered"] is True
    assert harness.completion_registry.is_awaiting(harness.caller_session) is True
    assert subscribers.get_completion_subscribers(validator_run_id) == [harness.caller_session]

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
    assert await harness.monitor.check_completed_task_agents() == 0
    caller = harness.runs.get(harness.caller_run.id)
    assert caller is not None and caller.status == "running"

    delivery = await harness.validator_run_ends(validator_run_id)
    assert delivery == {harness.caller_session: True}
    [(woken_session, _message, delivered)] = harness.wakes
    assert woken_session == harness.caller_session
    assert delivered["event"] == "task_close_review_completed"
    assert (delivered["status"], delivered["closed"]) == ("closed", True)
    assert delivered["run_id"] == validator_run_id
    assert harness.completion_registry.is_awaiting(harness.caller_session) is False
    assert await harness.monitor.check_completed_task_agents() == 0
    caller = harness.runs.get(harness.caller_run.id)
    assert caller is not None and caller.status == "running"

    # The verdict was consumed and the caller gets a cooperative completion turn.
    resolved = await harness.wait_for_agent(validator_run_id)
    assert resolved["completed"] is True
    assert resolved["notification_registered"] is False

    ended = await harness.cooperative_end()
    assert ended["success"] is True
    assert ended["run_id"] == harness.caller_run.id
    handoff = get_agent_end_handoff(harness.db, harness.caller_run.id)
    assert handoff is not None
    assert "async close verdict was received" in handoff.payload.rendered_markdown

    with session_context_for_test(harness.parent_session):
        result = await harness.agents._tools["get_agent_result"].func(harness.caller_run.id)
        waited = await harness.agents._tools["wait_for_agent"].func(harness.caller_run.id)
    assert result["result"] == handoff.payload.rendered_markdown
    assert waited["completed"] is True
    assert waited["result"] == handoff.payload.rendered_markdown


async def test_abandoned_closed_task_uses_stagnation_fallback(harness: _Harness) -> None:
    launched = await harness.close_task()
    [validator_run_id] = harness.spawned
    await harness.wait_for_agent(validator_run_id)
    submitted = await harness.validator_submits(
        validator_run_id,
        launched["review_id"],
        "valid",
    )
    assert submitted["review_status"] == "closed"
    await harness.validator_run_ends(validator_run_id)
    assert await harness.monitor.check_completed_task_agents() == 0

    harness.age_close_review_delivery()
    with (
        patch.object(
            harness.monitor._cleanup_handler,
            "_run_capture_policy",
            new=AsyncMock(return_value=(False, None)),
        ),
        patch.object(
            harness.monitor._cleanup_handler,
            "post_terminal_cleanup",
            new=AsyncMock(),
        ),
    ):
        handled = await harness.monitor.check_completed_task_agents()

    completed = harness.runs.get(harness.caller_run.id)
    assert handled == 1
    assert completed is not None
    assert completed.status == "success"
    assert completed.terminal_reason == "task_completed"
    assert "Task completion:" in (completed.result or "")
    assert get_agent_end_handoff(harness.db, harness.caller_run.id) is None


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


@pytest.mark.asyncio
async def test_passive_wait_exemption_requires_live_subscription_including_observers(
    harness: _Harness,
) -> None:
    launched = await harness.close_task()
    run_id = cast(str, launched["validator_run_id"])
    await harness.wait_for_agent(run_id)

    assert await harness.monitor._parked_on_completion(harness.caller_session, PASSIVE_WAIT)

    subscribers = CompletionSubscriberManager(harness.db)
    subscribers.remove_completion_subscribers(run_id)
    assert not await harness.monitor._parked_on_completion(harness.caller_session, PASSIVE_WAIT)

    foreign = harness.runs.create(
        parent_session_id=harness.parent_session,
        provider="claude",
        prompt="foreign wait",
        agent_name="foreign-agent",
        run_id=_rid("foreign-run"),
    )
    harness.runs.start(foreign.id)
    waiting = await harness.wait_for_agent(foreign.id)
    assert waiting["notification_registered"] is True
    assert await harness.monitor._parked_on_completion(harness.caller_session, PASSIVE_WAIT)

    harness.runs.complete(foreign.id, result="done")
    assert not await harness.monitor._parked_on_completion(harness.caller_session, PASSIVE_WAIT)

    subscribers.add_completion_subscriber(run_id, harness.caller_session)
    harness.runs.complete(run_id, result="done")
    assert not await harness.monitor._parked_on_completion(harness.caller_session, PASSIVE_WAIT)


async def test_observer_wait_yields_task_and_epic_gates_until_run_completes(
    harness: _Harness,
) -> None:
    sync_bundled_rules(harness.db, get_bundled_rules_path())
    harness.task_manager.claim_task(harness.task.id, harness.caller_session)
    report_run = harness.runs.create(
        parent_session_id=harness.parent_session,
        provider="claude",
        prompt="publish the report",
        agent_name="synthesis-reporter",
    )
    harness.runs.start(report_run.id)
    engine = RuleEngine(harness.db, task_manager=harness.task_manager)
    event = HookEvent(
        event_type=HookEventType.STOP,
        session_id=harness.caller_session,
        source=SessionSource.CLAUDE,
        timestamp=datetime.now(UTC),
        data={},
        metadata={},
    )
    variables: dict[str, Any] = {
        "_agent_type": "default",
        "_memory_initial_stop_checked": True,
        "mode_level": 2,
        "task_claimed": True,
        "claimed_tasks": {harness.task.id: f"#{harness.task.seq_num}"},
        "stop_attempts": 0,
    }
    blocked = await engine.evaluate(event, session_id=harness.caller_session, variables=variables)
    assert blocked.decision == "block"
    assert "claimed tasks" in (blocked.reason or "")
    assert "Epic tree not complete" in (blocked.reason or "")

    waiting = await harness.wait_for_agent(report_run.id)
    assert waiting["notification_registered"] is True and waiting["completed"] is False
    attempts_before_wait = variables["stop_attempts"]
    allowed = await engine.evaluate(event, session_id=harness.caller_session, variables=variables)
    assert allowed.decision == "allow"
    assert variables["stop_attempts"] == attempts_before_wait
    assert harness.task_manager.get_task(harness.task.id).closed_at is None

    harness.runs.complete(report_run.id, result="published")
    blocked_again = await engine.evaluate(
        event, session_id=harness.caller_session, variables=variables
    )
    assert blocked_again.decision == "block"
    assert "[aggregated:2-gates]" in (blocked_again.reason or "")
    assert variables["stop_attempts"] == attempts_before_wait + 1


async def _sweep_after_caller_ends(harness: _Harness) -> int:
    SessionManager(harness.db).update_status(harness.caller_session, "expired")
    with (
        patch.object(
            harness.monitor._cleanup_handler,
            "_run_capture_policy",
            new=AsyncMock(return_value=(False, None)),
        ),
        patch.object(harness.monitor._cleanup_handler, "post_terminal_cleanup", new=AsyncMock()),
    ):
        return await harness.monitor.check_completed_task_agents()


async def test_ended_caller_fails_on_an_invalid_verdict_it_cannot_retry(
    harness: _Harness,
) -> None:
    launched = await harness.close_task()
    [validator_run_id] = harness.spawned
    submitted = await harness.validator_submits(validator_run_id, launched["review_id"], "invalid")
    assert submitted["review_status"] == "invalid"

    handled = await _sweep_after_caller_ends(harness)

    caller = harness.runs.get(harness.caller_run.id)
    assert handled == 1
    assert caller is not None
    assert caller.status == "error"
    assert "review_status=invalid" in (caller.error or "")


async def test_ended_caller_completes_on_a_valid_verdict_before_delivery(
    harness: _Harness,
) -> None:
    launched = await harness.close_task()
    [validator_run_id] = harness.spawned
    submitted = await harness.validator_submits(validator_run_id, launched["review_id"], "valid")
    assert submitted["review_status"] == "closed"

    handled = await _sweep_after_caller_ends(harness)

    caller = harness.runs.get(harness.caller_run.id)
    assert handled == 1
    assert caller is not None
    assert (caller.status, caller.terminal_reason) == ("success", "task_completed")
    assert harness.wakes == []
