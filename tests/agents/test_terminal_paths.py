from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

from gobby.mcp_proxy.tools.agent_messaging import add_messaging_tools
from gobby.mcp_proxy.tools.internal import InternalToolRegistry
from gobby.mcp_proxy.tools.spawn_agent._health import _deferred_tmux_health_check
from gobby.plans.review_evidence import PlanReviewEvidenceService
from gobby.plans.review_evidence_models import ReviewEvidenceError
from gobby.plans.review_requirements import (
    REQUEST_ANCHOR_VARIABLE,
    build_request_anchor,
)
from gobby.plans.review_telemetry import (
    deterministic_review_message_id,
    enrich_round_result,
    persist_delivered_round_result,
    persist_enriched_round_result,
)
from gobby.plans.review_terminal import PlanReviewTerminalOutcome, terminalize_plan_review_run
from gobby.plans.review_verdict_effects import apply_staged_verdict_effects
from gobby.storage.agents import LocalAgentRunManager
from gobby.storage.build_history import BuildHistoryStorage
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.inter_session_messages import InterSessionMessageManager
from gobby.storage.projects import LocalProjectManager
from gobby.storage.session_tasks import SessionTaskManager
from gobby.storage.sessions import SessionManager
from gobby.workflows.state_manager import SessionVariableManager
from tests.review_coverage_helpers import coverage_attestation
from tests.review_telemetry_helpers import delivered_telemetry
from tests.storage.test_stage_review_findings import (
    StageReviewSetup,
    _prepare_bound,
)
from tests.storage.test_stage_review_findings import (
    stage_review_setup as _stage_review_setup,  # noqa: F401 - pytest fixture re-export
)


@dataclass(frozen=True)
class BoundReview:
    evidence_id: str
    run_id: str
    parent_session_id: str
    child_session_id: str


def _bound_review(
    temp_db: HubDatabase,
    tmp_path: Path,
    *,
    suffix: str = "",
) -> BoundReview:
    project = LocalProjectManager(temp_db).create(
        name=f"terminal-review{suffix}",
        repo_path=str(tmp_path),
    )
    sessions = SessionManager(temp_db)
    parent = sessions.register(
        external_id=f"terminal-parent{suffix}",
        machine_id="test-machine",
        source="codex",
        project_id=project.id,
    )
    child = sessions.register(
        external_id=f"terminal-child{suffix}",
        machine_id="test-machine",
        source="codex",
        project_id=project.id,
        parent_session_id=parent.id,
        agent_depth=1,
    )
    plan_path = tmp_path / f"terminal-review{suffix}.md"
    plan_path.write_text(
        "\n".join(
            [
                "# Terminal Review",
                "**Plan ID:** terminal-review",
                "",
                "## P1 Phase",
                "`kind: framing`",
                "",
                "### 1.1 Work",
                "`kind: deliverable`",
                "",
                "Target: `src/example.py`",
                "",
                "**Acceptance:**",
                "- 1.1.1 — Exists. test: `tests/test_example.py`",
                "",
                "## Task Mapping",
                "`kind: framing`",
                "",
                "Pending.",
                "",
                "## V1 Plan Changelog",
                "`kind: verification`",
                "",
                "No rounds.",
                "",
                "## M1 Task Manifest",
                "`kind: manifest`",
                "",
                "```yaml",
                "[]",
                "```",
            ]
        ),
        encoding="utf-8",
    )
    SessionVariableManager(temp_db).merge_variables(
        parent.id,
        {
            REQUEST_ANCHOR_VARIABLE: build_request_anchor(
                f"terminal-review-request{suffix}",
                "Review the terminal plan",
            )
        },
    )
    evidence_service = PlanReviewEvidenceService(temp_db)
    prepared = evidence_service.prepare_plan_review_round(
        project_id=project.id,
        plan_path=plan_path,
        round_number=1,
        session_id=parent.id,
    )
    run = LocalAgentRunManager(temp_db).create(
        parent_session_id=parent.id,
        child_session_id=child.id,
        provider="codex",
        prompt="Review the plan.",
    )
    evidence_service.bind_evidence_run(prepared.evidence_id, run.id)
    return BoundReview(
        evidence_id=prepared.evidence_id,
        run_id=run.id,
        parent_session_id=parent.id,
        child_session_id=child.id,
    )


def _delivered_result(evidence_id: str) -> dict[str, object]:
    return {
        "verdict": "needs_requirements",
        "evidence_id": evidence_id,
        "reason": {
            "reason_code": "missing_requirements",
            "questions": ["Which source is authoritative?"],
        },
        "convergence_telemetry": delivered_telemetry(),
    }


def _settled_run() -> SimpleNamespace:
    started_at = datetime(2026, 7, 28, 12, 0, tzinfo=UTC)
    return SimpleNamespace(
        started_at=started_at,
        created_at=started_at,
        completed_at=started_at + timedelta(seconds=10),
        tool_calls_count=8,
        turns_used=3,
    )


def test_result_state_is_monotonic(
    temp_db: HubDatabase,
    tmp_path: Path,
) -> None:
    bound = _bound_review(temp_db, tmp_path)
    delivered = _delivered_result(bound.evidence_id)

    first = persist_delivered_round_result(
        temp_db,
        run_id=bound.run_id,
        round_result=delivered,
    )
    assert (
        persist_delivered_round_result(
            temp_db,
            run_id=bound.run_id,
            round_result=delivered,
        )
        == first
    )

    enriched = enrich_round_result(
        delivered,
        run=_settled_run(),
        terminal_status="success",
    )
    assert (
        persist_enriched_round_result(
            temp_db,
            run_id=bound.run_id,
            round_result=enriched,
        )
        == enriched
    )
    with pytest.raises(ReviewEvidenceError, match="cannot regress"):
        persist_delivered_round_result(
            temp_db,
            run_id=bound.run_id,
            round_result=delivered,
        )

    row = temp_db.fetchone("SELECT result FROM agent_runs WHERE id = %s", (bound.run_id,))
    assert row is not None
    assert json.loads(row["result"]) == enriched


@pytest.mark.asyncio
async def test_no_terminal_route_bypasses_guard(
    temp_db: HubDatabase,
    tmp_path: Path,
) -> None:
    manager = LocalAgentRunManager(temp_db)
    undelivered = _bound_review(temp_db, tmp_path, suffix="-missing")

    failed = terminalize_plan_review_run(
        manager,
        run_id=undelivered.run_id,
        action="complete",
        tool_calls_count=3,
        turns_used=2,
    )

    assert failed.handled is True
    assert failed.run is not None
    assert failed.run.status == "error"
    assert failed.parent_session_id == undelivered.parent_session_id
    assert (
        PlanReviewEvidenceService(temp_db).get_evidence(undelivered.evidence_id).expired_at
        is not None
    )

    delivered = _bound_review(temp_db, tmp_path, suffix="-delivered")
    persist_delivered_round_result(
        temp_db,
        run_id=delivered.run_id,
        round_result=_delivered_result(delivered.evidence_id),
    )
    completed = terminalize_plan_review_run(
        manager,
        run_id=delivered.run_id,
        action="complete",
        tool_calls_count=9,
        turns_used=4,
    )

    assert completed.handled is True
    assert completed.run is not None
    assert completed.run.status == "success"
    assert completed.run.result is not None
    stored_result = json.loads(completed.run.result)
    assert stored_result["convergence_telemetry"]["state"] == "enriched"
    assert stored_result["convergence_telemetry"]["daemon"]["tool_calls"] == 9
    assert PlanReviewEvidenceService(temp_db).get_evidence(delivered.evidence_id).is_live

    unbound_run = manager.create(
        parent_session_id=delivered.parent_session_id,
        provider="codex",
        prompt="Ordinary helper.",
    )
    unaffected = terminalize_plan_review_run(
        manager,
        run_id=unbound_run.id,
        action="complete",
    )
    assert unaffected.handled is False
    stored_unbound = manager.get(unbound_run.id)
    assert stored_unbound is not None
    assert stored_unbound.status == "pending"

    from gobby.agents.run_completion import complete_and_notify_agent_run
    from gobby.agents.runner import AgentRunner
    from gobby.agents.runner_queries import cancel_run
    from gobby.events.completion_registry import CompletionEventRegistry
    from gobby.hooks.session_coordinator import SessionCoordinator
    from gobby.hooks.session_types import HookSessionManager
    from gobby.mcp_proxy.tools.agent_cancellation import (
        terminalize_cancelled_agent_run,
        terminalize_killed_agent_run,
    )
    from gobby.workflows.engine.enforcement_completion import EnforcementCompletionMixin

    routes = ("session_end", "workflow", "fallback", "kill_error", "cancel")
    for delivered_state in (False, True):
        for route in routes:
            route_bound = _bound_review(
                temp_db,
                tmp_path,
                suffix=f"-{route}-{'delivered' if delivered_state else 'missing'}",
            )
            if delivered_state:
                persist_delivered_round_result(
                    temp_db,
                    run_id=route_bound.run_id,
                    round_result=_delivered_result(route_bound.evidence_id),
                )

            notifications: list[str] = []
            notification_completed = asyncio.Event()

            class RecordingRegistry:
                async def notify(
                    self,
                    target_run_id: str,
                    *,
                    result: dict[str, object],
                    message: str,
                    expected_run_id: str = route_bound.run_id,
                    expected_delivery: bool = delivered_state,
                    recorded_notifications: list[str] = notifications,
                    completed_event: asyncio.Event = notification_completed,
                ) -> dict[str, bool]:
                    del result, message
                    assert target_run_id == expected_run_id
                    stored = manager.get(target_run_id)
                    assert stored is not None
                    if expected_delivery:
                        assert stored.result is not None
                        telemetry = json.loads(stored.result)["convergence_telemetry"]
                        assert telemetry["state"] == "enriched"
                    recorded_notifications.append(target_run_id)
                    completed_event.set()
                    return {}

                def cleanup(
                    self,
                    target_run_id: str,
                    expected_run_id: str = route_bound.run_id,
                ) -> None:
                    assert target_run_id == expected_run_id

            registry = RecordingRegistry()
            runner = SimpleNamespace(
                _run_storage=manager,
                run_storage=manager,
                get_run=manager.get,
                get_run_id_by_session=lambda _session_id,
                expected_run_id=route_bound.run_id: expected_run_id,
                _session_manager=SessionManager(temp_db),
                logger=SimpleNamespace(
                    debug=lambda *_args: None,
                    info=lambda *_args: None,
                ),
            )
            runner.cancel_run = lambda target_run_id, current_runner=runner: cancel_run(
                current_runner,
                target_run_id,
            )
            runner.complete_run = lambda target_run_id, result=None: manager.complete(
                target_run_id,
                result=result,
            )
            typed_runner = cast(AgentRunner, runner)
            typed_registry = cast(CompletionEventRegistry, registry)

            if route == "session_end":
                coordinator = SessionCoordinator(
                    session_storage=cast(HookSessionManager, SessionManager(temp_db)),
                    agent_run_manager=manager,
                )
                coordinator.set_completion_registry(typed_registry)
                coordinator.complete_agent_run(
                    SimpleNamespace(
                        id=route_bound.child_session_id,
                        agent_run_id=route_bound.run_id,
                        summary_markdown="provider exit",
                        last_assistant_content="",
                        tool_call_count=3,
                        turn_count=2,
                    )
                )
                await notification_completed.wait()
            elif route == "workflow":

                async def lifecycle_terminalize(
                    target_run_id: str,
                    *,
                    notify_result: dict[str, object],
                    message: str,
                    current_runner: AgentRunner = typed_runner,
                    current_registry: CompletionEventRegistry = typed_registry,
                ) -> bool:
                    return await complete_and_notify_agent_run(
                        current_runner,
                        target_run_id,
                        completion_registry=current_registry,
                        notify_result=notify_result,
                        message=message,
                    )

                runner.agent_lifecycle_monitor = SimpleNamespace(
                    terminalize_successful_run=lifecycle_terminalize,
                )
                engine = SimpleNamespace(
                    _runner=typed_runner,
                    db=temp_db,
                    _completion_registry=typed_registry,
                )
                typed_engine = cast(EnforcementCompletionMixin, engine)
                await EnforcementCompletionMixin._complete_agent_workflow_run(
                    typed_engine,
                    route_bound.child_session_id,
                    "plan-adversary",
                )
            elif route == "fallback":
                await complete_and_notify_agent_run(
                    typed_runner,
                    route_bound.run_id,
                    completion_registry=typed_registry,
                )
            elif route == "kill_error":
                await terminalize_killed_agent_run(
                    runner=typed_runner,
                    run_id=route_bound.run_id,
                    effective_status="error",
                    lifecycle_monitor=None,
                    completion_registry=typed_registry,
                    task_manager=None,
                )
            else:
                await terminalize_cancelled_agent_run(
                    runner=typed_runner,
                    run_id=route_bound.run_id,
                    terminal_reason="user_cancelled",
                    lifecycle_monitor=None,
                    completion_registry=typed_registry,
                    task_manager=None,
                )

            stored = manager.get(route_bound.run_id)
            assert stored is not None
            expected_status = (
                "error"
                if not delivered_state or route == "kill_error"
                else "cancelled"
                if route == "cancel"
                else "success"
            )
            assert stored.status == expected_status
            route_evidence = PlanReviewEvidenceService(temp_db).get_evidence(
                route_bound.evidence_id
            )
            assert (route_evidence.expired_at is not None) is not delivered_state
            assert notifications == [route_bound.run_id], route


@pytest.mark.asyncio
async def test_delivery_mailbox_and_result_are_one_identity(
    temp_db: HubDatabase,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bound = _bound_review(temp_db, tmp_path)
    result = _delivered_result(bound.evidence_id)
    content = json.dumps(result, sort_keys=True, separators=(",", ":"))
    message_id = deterministic_review_message_id(
        evidence_id=bound.evidence_id,
        run_id=bound.run_id,
        effect_kind="round_result",
        target_session_id=bound.parent_session_id,
    )
    messages = InterSessionMessageManager(temp_db)
    registry = InternalToolRegistry("gobby-agents")
    add_messaging_tools(
        registry=registry,
        message_manager=messages,
        session_manager=SessionManager(temp_db),
        db=temp_db,
    )

    from gobby.plans import review_telemetry

    persist = review_telemetry.persist_delivered_round_result

    def crash_after_mailbox(*_args: object, **_kwargs: object) -> dict[str, object]:
        raise ReviewEvidenceError(
            "injected_result_write_failure",
            "injected crash after mailbox insert",
        )

    monkeypatch.setattr(review_telemetry, "persist_delivered_round_result", crash_after_mailbox)
    first_attempt = await registry.call(
        "send_message",
        {
            "from_session": bound.child_session_id,
            "target": "session",
            "target_id": bound.parent_session_id,
            "content": content,
        },
    )
    assert first_attempt["success"] is False

    monkeypatch.setattr(review_telemetry, "persist_delivered_round_result", persist)
    replayed_after_mailbox_crash = await registry.call(
        "send_message",
        {
            "from_session": bound.child_session_id,
            "target": "session",
            "target_id": bound.parent_session_id,
            "content": content,
        },
    )
    assert replayed_after_mailbox_crash["success"] is True
    replayed_after_result_crash = await registry.call(
        "send_message",
        {
            "from_session": bound.child_session_id,
            "target": "session",
            "target_id": bound.parent_session_id,
            "content": content,
        },
    )
    assert replayed_after_result_crash["success"] is True

    row = temp_db.fetchone(
        "SELECT COUNT(*) AS count FROM inter_session_messages WHERE id = %s",
        (message_id,),
    )
    assert row is not None
    assert row["count"] == 1

    first = messages.get_message(message_id)
    assert first is not None
    with pytest.raises(ValueError, match="idempotency conflict"):
        messages.create_message(
            from_session=bound.child_session_id,
            to_session=bound.parent_session_id,
            content="different result",
            message_id=message_id,
        )


@pytest.mark.parametrize(
    "relative_path",
    [
        "src/gobby/agents/agent_cleanup.py",
        "src/gobby/agents/run_completion.py",
        "src/gobby/agents/runner_queries.py",
        "src/gobby/agents/resume_executor.py",
        "src/gobby/hooks/session_coordinator.py",
        "src/gobby/mcp_proxy/tools/agent_cancellation.py",
        "src/gobby/mcp_proxy/tools/spawn_agent/_health.py",
        "src/gobby/servers/routes/admin/_testing.py",
        "src/gobby/servers/routes/agents.py",
    ],
)
def test_all_terminalizing_call_sites_route_through_helper(relative_path: str) -> None:
    source = (Path(__file__).parents[2] / relative_path).read_text()

    assert "terminalize_plan_review_run" in source


@pytest.mark.parametrize(
    "crash_boundary",
    [
        None,
        "before_enrich",
        "after_enrich",
        "before_finalize",
        "after_finalize",
        "before_commit_stage",
        "after_commit_stage",
        "before_verdict_effects",
        "after_verdict_effects",
        "before_terminal",
        "after_terminal",
    ],
)
def test_staged_verdict_terminal_ordering(
    request: pytest.FixtureRequest,
    monkeypatch: pytest.MonkeyPatch,
    crash_boundary: str | None,
) -> None:
    stage_review_setup = cast(
        StageReviewSetup,
        request.getfixturevalue("_stage_review_setup"),
    )
    evidence_id, run_id = _prepare_bound(stage_review_setup)
    derived = stage_review_setup.evidence.derive_plan_review_manifest(
        evidence_id,
        routing_decisions={},
    )
    manifest_entries = derived["manifest_entries"]
    assert isinstance(manifest_entries, list)
    stage_review_setup.manager.approve_review(
        stage_review_setup.task_id,
        "planning",
        evidence_id=evidence_id,
        round_number=1,
        findings=[],
        routing_decisions={},
        manifest_entries=manifest_entries,
        coverage_attestation=coverage_attestation(
            evidence_id=evidence_id,
            manifest_entries=manifest_entries,
        ),
        convergence_telemetry=delivered_telemetry(),
        dispatch_run_id=run_id,
    )

    from gobby.plans import review_terminal
    from gobby.storage.tasks._stage_states import StageStatesManager

    order: list[str] = []
    original_enrich = persist_enriched_round_result
    original_finalize = PlanReviewEvidenceService.finalize_plan_review_evidence
    original_stage_commit = StageStatesManager.approve_review
    original_complete = LocalAgentRunManager.complete
    crashed = False

    def inject_crash(boundary: str, moment: str) -> None:
        nonlocal crashed
        if crash_boundary == f"{moment}_{boundary}" and not crashed:
            crashed = True
            raise RuntimeError(f"injected crash {moment} {boundary}")

    def record_enrich(*args: Any, **kwargs: Any) -> dict[str, object]:
        inject_crash("enrich", "before")
        result = original_enrich(*args, **kwargs)
        order.append("enrich")
        inject_crash("enrich", "after")
        return result

    def record_finalize(*args: Any, **kwargs: Any) -> object:
        inject_crash("finalize", "before")
        result = original_finalize(*args, **kwargs)
        order.append("finalize")
        inject_crash("finalize", "after")
        return result

    def record_stage_commit(*args: Any, **kwargs: Any) -> object:
        inject_crash("commit_stage", "before")
        result = original_stage_commit(*args, **kwargs)
        order.append("commit_stage")
        inject_crash("commit_stage", "after")
        return result

    def record_effects(*_args: Any, **_kwargs: Any) -> None:
        inject_crash("verdict_effects", "before")
        stored = stage_review_setup.runs.get(run_id)
        assert stored is not None
        assert stored.result is not None
        assert json.loads(stored.result)["convergence_telemetry"]["state"] == "enriched"
        order.append("verdict_effects")
        inject_crash("verdict_effects", "after")

    def record_complete(*args: Any, **kwargs: Any) -> object:
        inject_crash("terminal", "before")
        result = original_complete(*args, **kwargs)
        order.append("terminal")
        inject_crash("terminal", "after")
        return result

    monkeypatch.setattr(review_terminal, "persist_enriched_round_result", record_enrich)
    monkeypatch.setattr(
        PlanReviewEvidenceService,
        "finalize_plan_review_evidence",
        record_finalize,
    )
    monkeypatch.setattr(StageStatesManager, "approve_review", record_stage_commit)
    monkeypatch.setattr(review_terminal, "apply_staged_verdict_effects", record_effects)
    monkeypatch.setattr(LocalAgentRunManager, "complete", record_complete)

    def terminalize() -> PlanReviewTerminalOutcome:
        return terminalize_plan_review_run(
            stage_review_setup.runs,
            run_id=run_id,
            action="complete",
            tool_calls_count=9,
            turns_used=4,
        )

    if crash_boundary is not None:
        moment, boundary = crash_boundary.split("_", 1)
        with pytest.raises(RuntimeError, match=f"injected crash {moment} {boundary}"):
            terminalize()
        assert "wake" not in order

    outcome = terminalize()
    order.append("wake")

    assert outcome.handled is True
    run = stage_review_setup.runs.get(run_id)
    assert run is not None
    assert run.status == "success"
    finalized = stage_review_setup.evidence.get_evidence(evidence_id)
    assert finalized.finalized_at is not None
    current = stage_review_setup.manager.stage_states.current_stage(stage_review_setup.task_id)
    assert current is not None
    assert current.state == "review_approved"
    expected = [
        "enrich",
        "finalize",
        "commit_stage",
        "verdict_effects",
        "terminal",
        "wake",
    ]
    if crash_boundary is None:
        assert order == expected
    else:
        first_positions = [order.index(boundary) for boundary in expected]
        assert first_positions == sorted(first_positions)


def test_verdict_effects_idempotent_across_replay(
    request: pytest.FixtureRequest,
) -> None:
    stage_review_setup = cast(
        StageReviewSetup,
        request.getfixturevalue("_stage_review_setup"),
    )
    task = stage_review_setup.manager.get_task(stage_review_setup.task_id)
    reviewer = stage_review_setup.sessions.register(
        external_id="verdict-effect-reviewer",
        machine_id="test-machine",
        source="codex",
        project_id=task.project_id,
        parent_session_id=stage_review_setup.parent_session_id,
        agent_depth=1,
    )
    coordinator = stage_review_setup.sessions.register(
        external_id="verdict-effect-coordinator",
        machine_id="test-machine",
        source="codex",
        project_id=task.project_id,
    )
    BuildHistoryStorage(stage_review_setup.db).record_run(
        project_id=task.project_id,
        root_task_id=task.id,
        input_ref=f"#{task.seq_num}",
        action="build",
        summary={"coordinator_session_id": coordinator.id},
    )

    evidence_id, run_id = _prepare_bound(stage_review_setup)
    stage_review_setup.db.execute(
        "UPDATE agent_runs SET child_session_id = %s WHERE id = %s",
        (reviewer.id, run_id),
    )
    derived = stage_review_setup.evidence.derive_plan_review_manifest(
        evidence_id,
        routing_decisions={},
    )
    manifest_entries = derived["manifest_entries"]
    assert isinstance(manifest_entries, list)
    stage_review_setup.manager.approve_review(
        task.id,
        "planning",
        evidence_id=evidence_id,
        round_number=1,
        findings=[],
        routing_decisions={},
        manifest_entries=manifest_entries,
        coverage_attestation=coverage_attestation(
            evidence_id=evidence_id,
            manifest_entries=manifest_entries,
        ),
        convergence_telemetry=delivered_telemetry(),
        dispatch_run_id=run_id,
    )

    outcome = terminalize_plan_review_run(
        stage_review_setup.runs,
        run_id=run_id,
        action="complete",
        tool_calls_count=6,
        turns_used=2,
    )
    assert outcome.run is not None
    finalized = stage_review_setup.evidence.get_evidence(evidence_id)
    assert finalized.round_result is not None

    apply_staged_verdict_effects(
        stage_review_setup.db,
        evidence=finalized,
        run=outcome.run,
        result=finalized.round_result,
    )
    apply_staged_verdict_effects(
        stage_review_setup.db,
        evidence=finalized,
        run=outcome.run,
        result=finalized.round_result,
    )

    message_id = deterministic_review_message_id(
        evidence_id=evidence_id,
        run_id=run_id,
        effect_kind="signoff_relay",
        target_session_id=coordinator.id,
    )
    row = stage_review_setup.db.fetchone(
        "SELECT COUNT(*) AS count FROM inter_session_messages WHERE id = %s",
        (message_id,),
    )
    assert row is not None
    assert row["count"] == 1
    assert finalized.manifest_state == "applied"
    assert stage_review_setup.manager.get_task(task.id).claimed_by_session_id is None
    refreshed = stage_review_setup.evidence.get_evidence(evidence_id)
    assert refreshed.lesson_mint_status == "none"
    links = SessionTaskManager(stage_review_setup.db).get_task_sessions(task.id)
    assert (
        len(
            [
                link
                for link in links
                if link["session_id"] == reviewer.id and link["action"] == "review_approved"
            ]
        )
        == 1
    )


async def test_deferred_health_check_respects_evidence_bind(
    request: pytest.FixtureRequest,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stage_review_setup = cast(
        StageReviewSetup,
        request.getfixturevalue("_stage_review_setup"),
    )

    async def dead_session(*_args: object, **_kwargs: object) -> tuple[bool, None]:
        return False, None

    monkeypatch.setattr(
        "gobby.mcp_proxy.tools.spawn_agent._health._check_tmux_session_alive",
        dead_session,
    )
    unbound = stage_review_setup.runs.create(
        parent_session_id=stage_review_setup.parent_session_id,
        provider="codex",
        prompt="pre-bind health race",
    )
    runner = SimpleNamespace(run_storage=stage_review_setup.runs)

    await _deferred_tmux_health_check(
        runner,
        unbound.id,
        "pre-bind",
        None,
        None,
        0,
    )

    pre_bind_result = stage_review_setup.runs.get(unbound.id)
    assert pre_bind_result is not None
    assert pre_bind_result.status == "error"

    evidence_id, run_id = _prepare_bound(stage_review_setup)
    await _deferred_tmux_health_check(
        runner,
        run_id,
        "post-bind",
        None,
        None,
        0,
    )

    post_bind_result = stage_review_setup.runs.get(run_id)
    assert post_bind_result is not None
    assert post_bind_result.status == "error"
    evidence = stage_review_setup.evidence.get_evidence(evidence_id)
    assert evidence.expired_at is not None
    current = stage_review_setup.manager.stage_states.current_stage(stage_review_setup.task_id)
    assert current is not None
    assert current.state == "ready"
