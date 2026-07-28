from __future__ import annotations

import json
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

from gobby.plans.review_evidence import PlanReviewEvidenceService
from gobby.plans.review_evidence_io import ensure_checkpoint
from gobby.plans.review_evidence_models import ReviewEvidenceError
from gobby.plans.review_requirements import (
    REQUEST_ANCHOR_VARIABLE,
    build_request_anchor,
)
from gobby.plans.review_telemetry import (
    derive_convergence_comparison,
    derive_daemon_aggregates,
    enrich_round_result,
    persist_delivered_round_result,
    settle_review_result_before_wake,
    validate_convergence_telemetry,
)
from gobby.plans.review_terminal import terminalize_plan_review_run
from gobby.storage.agents import LocalAgentRunManager
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.projects import LocalProjectManager
from gobby.storage.sessions import SessionManager
from gobby.workflows.state_manager import SessionVariableManager
from tests.review_coverage_helpers import coverage_attestation
from tests.review_telemetry_helpers import delivered_telemetry
from tests.storage.test_stage_review_findings import (
    StageReviewSetup,
    _findings,
    _prepare_bound,
)
from tests.storage.test_stage_review_findings import (
    stage_review_setup as _stage_review_setup,  # noqa: F401 - pytest fixture re-export
)


def test_classification_provenance() -> None:
    telemetry = validate_convergence_telemetry(delivered_telemetry())

    comparison = derive_convergence_comparison([telemetry])

    assert comparison == {
        "reviewer_miss_count": 1,
        "fixer_induced_count": 0,
        "repeated_check_keys": ["terminal-path-totality"],
        "repeated_check_key_classes": ["terminal-path"],
        "ledger_entries_carried": 1,
        "artifact_growth": {
            "section_delta": 1,
            "target_delta": 2,
            "acceptance_delta": 3,
        },
    }

    missing_ids = cast(dict[str, Any], deepcopy(telemetry))
    classification = missing_ids["reviewer"]["reviewer_miss"]["classifications"][0]
    classification["finding_ids"] = []
    classification["ledger_ids"] = []
    with pytest.raises(ReviewEvidenceError, match="contributing finding or ledger"):
        validate_convergence_telemetry(missing_ids)

    missing_inputs = cast(dict[str, Any], deepcopy(telemetry))
    classification = missing_inputs["reviewer"]["reviewer_miss"]["classifications"][0]
    classification["classification_inputs"] = []
    with pytest.raises(ReviewEvidenceError, match="classification_inputs"):
        validate_convergence_telemetry(missing_inputs)


@pytest.mark.parametrize("terminal_status", ["success", "timeout", "error"])
def test_daemon_derived_aggregates_across_terminal_states(terminal_status: str) -> None:
    started_at = datetime(2026, 7, 28, 12, 0, tzinfo=UTC)
    completed_at = started_at + timedelta(seconds=25)
    run = SimpleNamespace(
        started_at=started_at,
        created_at=started_at - timedelta(seconds=5),
        completed_at=None,
        tool_calls_count=12,
        turns_used=4,
    )

    aggregates = derive_daemon_aggregates(
        run,
        terminal_status=terminal_status,
        finding_count=3,
        completed_at=completed_at,
    )

    assert aggregates["terminal_status"] == terminal_status
    assert aggregates["wall_time_seconds"] == 25
    assert aggregates["tool_calls"] == 12
    assert aggregates["turns"] == 4
    assert aggregates["calls_per_finding"] == {"value": 4.0}
    raw_lanes = aggregates["lanes"]
    assert isinstance(raw_lanes, list)
    lanes = cast(list[dict[str, object]], raw_lanes)
    assert [lane["lane_id"] for lane in lanes] == [
        "requirements",
        "failure-paths",
        "integration",
    ]
    assert all(
        lane["duration_seconds"] == {"unavailable": "native_lane_events_unavailable"}
        and lane["tool_calls"] == {"unavailable": "native_lane_events_unavailable"}
        for lane in lanes
    )


@pytest.mark.asyncio
async def test_merge_precedes_parent_wake() -> None:
    started_at = datetime(2026, 7, 28, 12, 0, tzinfo=UTC)
    run = SimpleNamespace(
        started_at=started_at,
        created_at=started_at,
        completed_at=started_at + timedelta(seconds=10),
        tool_calls_count=8,
        turns_used=3,
    )
    result = {
        "verdict": "needs_requirements",
        "evidence_id": "evidence-1",
        "reason": {
            "reason_code": "missing_requirements",
            "questions": ["Which source is authoritative?"],
        },
        "convergence_telemetry": delivered_telemetry(),
    }
    events: list[str] = []
    durable: dict[str, Any] = {}

    async def persist(enriched: dict[str, object]) -> dict[str, object]:
        events.append("persist")
        durable.clear()
        durable.update(enriched)
        return enriched

    async def wake() -> None:
        assert durable["convergence_telemetry"]["state"] == "enriched"
        events.append("wake")

    first = await settle_review_result_before_wake(
        result,
        run=run,
        terminal_status="success",
        persist=persist,
        wake=wake,
    )
    second = await settle_review_result_before_wake(
        first,
        run=run,
        terminal_status="success",
        persist=persist,
        wake=wake,
    )

    assert second == first
    assert events == ["persist", "wake", "persist", "wake"]


def test_delivered_and_enriched_states(
    temp_db: HubDatabase,
    tmp_path: Path,
) -> None:
    delivered = delivered_telemetry()
    validate_convergence_telemetry(delivered, required_state="delivered")
    with pytest.raises(ReviewEvidenceError, match="state must be enriched"):
        validate_convergence_telemetry(delivered, required_state="enriched")

    started_at = datetime(2026, 7, 28, 12, 0, tzinfo=UTC)
    run = SimpleNamespace(
        started_at=started_at,
        created_at=started_at,
        completed_at=started_at + timedelta(seconds=10),
        tool_calls_count=8,
        turns_used=3,
    )
    result = {
        "verdict": "needs_requirements",
        "evidence_id": "evidence-1",
        "reason": {
            "reason_code": "missing_requirements",
            "questions": ["Which source is authoritative?"],
        },
        "convergence_telemetry": delivered,
    }
    enriched = enrich_round_result(result, run=run, terminal_status="success")

    enriched_telemetry_value = enriched["convergence_telemetry"]
    assert isinstance(enriched_telemetry_value, dict)
    assert enriched_telemetry_value["state"] == "enriched"
    validate_convergence_telemetry(
        enriched_telemetry_value,
        required_state="enriched",
    )

    project = LocalProjectManager(temp_db).create(
        name="review-telemetry",
        repo_path=str(tmp_path),
    )
    parent = SessionManager(temp_db).register(
        external_id="telemetry-parent",
        machine_id="test-machine",
        source="codex",
        project_id=project.id,
    )
    plan_path = tmp_path / "telemetry-plan.md"
    plan_path.write_text(
        "\n".join(
            [
                "# Telemetry Plan",
                "**Plan ID:** telemetry-plan",
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
                "telemetry-request",
                "Review telemetry convergence",
            )
        },
    )
    service = PlanReviewEvidenceService(temp_db)
    prepared = service.prepare_plan_review_round(
        project_id=project.id,
        plan_path=plan_path,
        round_number=1,
        session_id=parent.id,
    )
    agent_run = LocalAgentRunManager(temp_db).create(
        parent_session_id=parent.id,
        provider="codex",
        prompt="Review the plan.",
    )
    service.bind_evidence_run(prepared.evidence_id, agent_run.id)
    delivered_result = deepcopy(result)
    delivered_result["evidence_id"] = prepared.evidence_id
    with pytest.raises(ReviewEvidenceError, match="state must be enriched"):
        service.finalize_plan_review_evidence(prepared.evidence_id, delivered_result)

    enriched_result = enrich_round_result(
        delivered_result,
        run=run,
        terminal_status="success",
    )
    ensure_checkpoint(
        plan_path,
        service.render_v1_round_checkpoint(prepared.evidence_id, enriched_result),
    )
    finalized = service.finalize_plan_review_evidence(prepared.evidence_id, enriched_result)
    assert finalized.round_result == enriched_result


@pytest.mark.parametrize("verdict", ["approved", "needs_review"])
def test_staged_path_carries_telemetry(
    request: pytest.FixtureRequest,
    verdict: str,
) -> None:
    stage_review_setup = cast(
        StageReviewSetup,
        request.getfixturevalue("_stage_review_setup"),
    )
    evidence_id, run_id = _prepare_bound(stage_review_setup)
    telemetry = delivered_telemetry()

    if verdict == "approved":
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
            convergence_telemetry=telemetry,
            dispatch_run_id=run_id,
        )
    else:
        stage_review_setup.manager.reject_review(
            stage_review_setup.task_id,
            "planning",
            findings=_findings(),
            coverage_attestation=coverage_attestation(
                evidence_id=evidence_id,
                shadow_valid=False,
            ),
            convergence_telemetry=telemetry,
            evidence_id=evidence_id,
            round_number=1,
            dispatch_run_id=run_id,
        )

    run = stage_review_setup.runs.get(run_id)
    assert run is not None
    delivered = json.loads(run.result or "{}")
    assert delivered["convergence_telemetry"] == telemetry
    current = stage_review_setup.manager.stage_states.current_stage(stage_review_setup.task_id)
    assert current is not None
    assert current.state == "needs_review"

    outcome = terminalize_plan_review_run(
        stage_review_setup.runs,
        run_id=run_id,
        action="complete",
        tool_calls_count=7,
        turns_used=3,
    )

    assert outcome.handled is True
    finalized = stage_review_setup.evidence.get_evidence(evidence_id)
    assert finalized.finalized_at is not None
    assert finalized.round_result is not None
    telemetry_result = finalized.round_result["convergence_telemetry"]
    assert isinstance(telemetry_result, dict)
    assert telemetry_result["state"] == "enriched"


@pytest.mark.parametrize(
    "producer_path",
    [
        "src/gobby/install/shared/skills/plan-review/SKILL.md",
        "src/gobby/install/shared/workflows/agents/plan-adversary-taskless.yaml",
        "src/gobby/install/shared/workflows/agents/plan-adversary.yaml",
    ],
)
def test_producer_contract_survives_delivery(
    temp_db: HubDatabase,
    tmp_path: Path,
    producer_path: str,
) -> None:
    source = (Path(__file__).parents[2] / producer_path).read_text()
    for field in (
        "convergence_telemetry",
        "reviewer_miss",
        "fixer_induced",
        "repeated_check_keys",
        "remedy_scope",
        "ledger_entries_carried",
        "artifact_growth",
        "classification_inputs",
        "check_key_class",
        "delivered",
        "daemon",
        "absent",
        "zero",
    ):
        assert field in source

    project = LocalProjectManager(temp_db).create(
        name=f"producer-{Path(producer_path).stem}",
        repo_path=str(tmp_path),
    )
    parent = SessionManager(temp_db).register(
        external_id=f"producer-{Path(producer_path).stem}",
        machine_id="test-machine",
        source="codex",
        project_id=project.id,
    )
    plan_path = tmp_path / f"{Path(producer_path).stem}.md"
    plan_path.write_text(
        "# Producer\n**Plan ID:** producer\n\n## V1 Plan Changelog\n`kind: verification`\n",
        encoding="utf-8",
    )
    SessionVariableManager(temp_db).merge_variables(
        parent.id,
        {
            REQUEST_ANCHOR_VARIABLE: build_request_anchor(
                f"producer-{Path(producer_path).stem}-request",
                "Review the producer contract",
            )
        },
    )
    service = PlanReviewEvidenceService(temp_db)
    prepared = service.prepare_plan_review_round(
        project_id=project.id,
        plan_path=plan_path,
        round_number=1,
        session_id=parent.id,
    )
    runs = LocalAgentRunManager(temp_db)
    run = runs.create(
        parent_session_id=parent.id,
        provider="codex",
        prompt="Review producer contract.",
    )
    service.bind_evidence_run(prepared.evidence_id, run.id)
    delivered_result = {
        "verdict": "needs_requirements",
        "evidence_id": prepared.evidence_id,
        "reason": {
            "reason_code": "missing_requirements",
            "questions": ["Which requirement source is canonical?"],
        },
        "convergence_telemetry": delivered_telemetry(),
    }
    persist_delivered_round_result(
        temp_db,
        run_id=run.id,
        round_result=delivered_result,
    )
    outcome = terminalize_plan_review_run(
        runs,
        run_id=run.id,
        action="complete",
        tool_calls_count=5,
        turns_used=2,
    )
    assert outcome.result is not None
    ensure_checkpoint(
        plan_path,
        service.render_v1_round_checkpoint(prepared.evidence_id, outcome.result),
    )
    finalized = service.finalize_plan_review_evidence(
        prepared.evidence_id,
        outcome.result,
    )
    assert finalized.round_result is not None
    final_telemetry = finalized.round_result["convergence_telemetry"]
    assert isinstance(final_telemetry, dict)
    assert final_telemetry["state"] == "enriched"
