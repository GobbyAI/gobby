from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

from gobby.agents.sync import sync_bundled_agents
from gobby.dispatch.actions import SpawnAgentAction
from gobby.dispatch.spawn import DispatchSpawnFailed, spawn_agent
from gobby.plans.review_evidence import PlanReviewEvidenceService
from gobby.plans.review_evidence_models import ReviewEvidenceError
from gobby.storage.agents import LocalAgentRunManager
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.projects import LocalProjectManager
from gobby.storage.sessions import SessionManager
from gobby.storage.tasks import LocalTaskManager
from gobby.storage.tasks._artifacts import TaskArtifactManager
from gobby.storage.tasks._dispatch_mutex import TaskDispatchMutexManager
from tests.review_coverage_helpers import coverage_attestation
from tests.storage.tasks._stage_test_helpers import (
    lifecycle_events,
    set_stage_state,
    stage_row,
)


@dataclass(frozen=True)
class StageReviewSetup:
    db: HubDatabase
    manager: LocalTaskManager
    evidence: PlanReviewEvidenceService
    runs: LocalAgentRunManager
    sessions: SessionManager
    project_id: str
    task_id: str
    plan_path: Path
    plan_relative_path: str
    parent_session_id: str


@pytest.fixture
def stage_review_setup(temp_db: HubDatabase, tmp_path: Path) -> StageReviewSetup:
    project = LocalProjectManager(temp_db).create(
        name="stage-review-findings",
        repo_path=str(tmp_path),
    )
    sessions = SessionManager(temp_db)
    parent = sessions.register(
        external_id="stage-review-launcher",
        machine_id="test-machine",
        source="codex",
        project_id=project.id,
    )
    plan_path = tmp_path / ".gobby" / "plans" / "review.md"
    plan_path.parent.mkdir(parents=True)
    plan_path.write_text(
        "\n".join(
            [
                "# Review",
                "**Plan ID:** review",
                "",
                "## P1 Foundation",
                "`kind: framing`",
                "",
                "### 1.1 Implement",
                "`kind: deliverable`",
                "",
                "Target: `src/example.py`",
                "",
                "**Acceptance:**",
                "- 1.1.1 — Implemented. test: `tests/test_example.py`",
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
                "- title: Implement",
                "  source_section: '1.1'",
                "  covers: [1.1.1]",
                "  category: code",
                "  implementation_domain: backend",
                "  priority: 2",
                "  task_type: feature",
                "  tdd: false",
                "  labels: [covers:review:1.1:1.1.1]",
                "  description: Implement.",
                "  validation_criteria: Tested.",
                "```",
                "",
            ]
        ),
        encoding="utf-8",
    )
    manager = LocalTaskManager(temp_db)
    task = manager.create_task(
        project.id,
        "Plan review anchor",
        task_type="review_anchor",
        category="planning",
        isolation="none",
        validation_criteria="Test task completion is observable.",
    )
    manager.initialize_task_manifest(task.id, stage_names=["planning"])
    set_stage_state(temp_db, task.id, "planning", "needs_review")
    TaskArtifactManager(temp_db).set_artifacts_atomic(
        task.id,
        plan_file_path=str(plan_path),
    )
    return StageReviewSetup(
        db=temp_db,
        manager=manager,
        evidence=PlanReviewEvidenceService(temp_db),
        runs=LocalAgentRunManager(temp_db),
        sessions=sessions,
        project_id=project.id,
        task_id=task.id,
        plan_path=plan_path,
        plan_relative_path=".gobby/plans/review.md",
        parent_session_id=parent.id,
    )


def _findings() -> list[dict[str, object]]:
    return [
        {
            "finding_id": "F1",
            "section_id": "1.1",
            "check_key": "failure-atomicity",
            "severity": "blocking",
            "category": "unhandled-edge",
            "location": "§ 1.1",
            "description": "The failure path can leave partial state.",
            "minimal_repair": "Specify rollback before retry.",
            "root_cause": "Only the successful write was modeled.",
            "prevention": "Walk every write failure boundary.",
            "participating_section_ids": ["1.1"],
            "failure_trace": {
                "preconditions": "The first durable write succeeds.",
                "action": "The second durable write fails.",
                "wrong_outcome": "The first write remains visible.",
                "violated_obligation": "The operation must commit atomically.",
                "citation": [{"path": "plan.md", "sha256": "0" * 64}],
            },
        },
        {
            "finding_id": "F2",
            "section_id": "1.1",
            "check_key": "cross-round-causality",
            "severity": "blocking",
            "category": "bad-sequencing",
            "location": "§ 1.1",
            "description": "The prior fix created a conflicting order.",
            "minimal_repair": "Restore the prerequisite before the new write.",
            "principle": "Causal fixes preserve established prerequisites.",
            "prevention": "Recheck all sections changed by a causal fix.",
            "introduced_in_round": 1,
            "causal_finding_id": "F1",
            "causal_section_ids": ["1.1"],
            "failure_trace": {
                "preconditions": "The prior repair is present.",
                "action": "The new write executes before its prerequisite.",
                "wrong_outcome": "The prerequisite is observed too late.",
                "violated_obligation": "Causal repairs must preserve prerequisite order.",
                "citation": [{"path": "plan.md", "sha256": "0" * 64}],
            },
        },
    ]


def _repair_submission() -> dict[str, object]:
    attestations = []
    for finding in _findings():
        attestations.append(
            {
                "prior_finding_id": finding["finding_id"],
                "check_key": finding["check_key"],
                "changed_section_ids": ["1.1"],
                "accepted_resolution": finding["minimal_repair"],
                "deviation_from_minimal_repair": None,
                "changed_symbols": ["gobby.example.rollback"],
                "consumer_sites_swept": ["src/example.py:consumer"],
                "adjacent_variants_swept": ["src/example.py:retry"],
                "validation_evidence": ["pytest tests/test_example.py"],
                "deferred_sites": [],
            }
        )
    return {
        "round_number": 2,
        "prior_finding_resolutions": [
            {"prior_finding_id": finding["finding_id"], "decision": "repair"}
            for finding in _findings()
        ],
        "repair_attestations": attestations,
    }


def _apply_round_one_repairs(setup: StageReviewSetup) -> dict[str, object]:
    setup.plan_path.write_text(
        setup.plan_path.read_text(encoding="utf-8").replace(
            "Implemented.",
            "Implemented with rollback before retry.",
        ),
        encoding="utf-8",
    )
    return _repair_submission()


def _prepare_bound(
    setup: StageReviewSetup,
    *,
    round_number: int = 1,
    task_id: str | None = None,
    plan_path: Path | None = None,
) -> tuple[str, str]:
    target_task_id = task_id or setup.task_id
    prepared = setup.evidence.prepare_plan_review_round(
        project_id=setup.project_id,
        plan_path=plan_path or setup.plan_path,
        round_number=round_number,
        task_id=target_task_id,
        stage="planning",
    )
    run = setup.runs.create(
        parent_session_id=setup.parent_session_id,
        provider="codex",
        prompt="review",
        task_id=target_task_id,
    )
    setup.evidence.bind_evidence_run(prepared.evidence_id, run.id)
    _hold_dispatch_mutex(setup, task_id=target_task_id, run_id=run.id)
    return prepared.evidence_id, run.id


def _hold_dispatch_mutex(
    setup: StageReviewSetup,
    *,
    task_id: str,
    run_id: str,
) -> None:
    mutexes = TaskDispatchMutexManager(setup.db)
    mutexes.ensure_table()
    assert mutexes.acquire_mutex(
        task_id,
        holder=f"test:{run_id}",
        kind="spawn_agent",
        ttl_seconds=600,
        run_id=run_id,
    )


def _fence(description: str) -> dict[str, object]:
    matches = re.findall(r"```json\n(.+?)\n```", description, flags=re.DOTALL)
    assert len(matches) == 1
    payload = json.loads(matches[0])
    assert isinstance(payload, dict)
    return payload


def test_fence_round_trip(stage_review_setup: StageReviewSetup) -> None:
    evidence_id, run_id = _prepare_bound(stage_review_setup)
    findings = _findings()
    invalid = [dict(finding) for finding in findings]
    invalid[0]["section_id"] = "missing"
    with pytest.raises(ReviewEvidenceError, match="absent from the evidence manifest"):
        stage_review_setup.manager.reject_review(
            stage_review_setup.task_id,
            "planning",
            findings=invalid,
            coverage_attestation=coverage_attestation(
                evidence_id=evidence_id,
                shadow_valid=False,
            ),
            evidence_id=evidence_id,
            round_number=1,
            dispatch_run_id=run_id,
        )
    assert stage_review_setup.evidence.get_evidence(evidence_id).finalized_at is None

    updated = stage_review_setup.manager.reject_review(
        stage_review_setup.task_id,
        "planning",
        findings=findings,
        coverage_attestation=coverage_attestation(
            evidence_id=evidence_id,
            shadow_valid=False,
        ),
        evidence_id=evidence_id,
        round_number=1,
        dispatch_run_id=run_id,
    )

    payload = _fence(updated.description or "")
    assert payload["findings"] == findings
    assert payload["evidence_id"] == evidence_id
    assert "principle" not in findings[0]
    assert findings[1]["causal_finding_id"] == "F1"
    evidence = stage_review_setup.evidence.get_evidence(evidence_id)
    assert evidence.round_result == {
        "verdict": "needs_review",
        "findings": findings,
        "coverage_attestation": coverage_attestation(
            evidence_id=evidence_id,
            shadow_valid=False,
        ),
    }


def test_server_side_evidence_resolution(stage_review_setup: StageReviewSetup) -> None:
    original_snapshot = stage_review_setup.plan_path.read_bytes()
    evidence_id, run_id = _prepare_bound(stage_review_setup)
    stored = stage_review_setup.evidence.get_evidence(evidence_id)
    stage_review_setup.plan_path.write_text("# Mutated live plan\n", encoding="utf-8")

    updated = stage_review_setup.manager.reject_review(
        stage_review_setup.task_id,
        "planning",
        findings=_findings(),
        coverage_attestation=coverage_attestation(
            evidence_id=evidence_id,
            shadow_valid=False,
        ),
        evidence_id=evidence_id,
        round_number=1,
        dispatch_run_id=run_id,
    )

    payload = _fence(updated.description or "")
    assert payload["plan_hash"] == hashlib.sha256(original_snapshot).hexdigest()
    assert payload["plan_hash"] == stored.plan_hash
    assert (
        payload["plan_hash"]
        != hashlib.sha256(stage_review_setup.plan_path.read_bytes()).hexdigest()
    )
    assert payload["section_manifest"] == [section.to_dict() for section in stored.section_manifest]
    with pytest.raises(ReviewEvidenceError, match="current review attempt"):
        stage_review_setup.manager.reject_review(
            stage_review_setup.task_id,
            "planning",
            findings=_findings(),
            coverage_attestation=coverage_attestation(
                evidence_id=evidence_id,
                shadow_valid=False,
            ),
            evidence_id=evidence_id,
            round_number=2,
            dispatch_run_id=run_id,
        )

    other_plan = stage_review_setup.plan_path.with_name("other.md")
    other_plan.write_text(original_snapshot.decode("utf-8"), encoding="utf-8")
    other_task = stage_review_setup.manager.create_task(
        stage_review_setup.project_id,
        "Other plan",
        task_type="review_anchor",
        category="planning",
        validation_criteria="Test task completion is observable.",
    )
    stage_review_setup.manager.initialize_task_manifest(other_task.id, stage_names=["planning"])
    set_stage_state(stage_review_setup.db, other_task.id, "planning", "needs_review")
    TaskArtifactManager(stage_review_setup.db).set_artifacts_atomic(
        other_task.id,
        plan_file_path=str(other_plan),
    )
    with pytest.raises(ReviewEvidenceError, match="current review attempt"):
        stage_review_setup.manager.reject_review(
            other_task.id,
            "planning",
            findings=_findings(),
            coverage_attestation=coverage_attestation(
                evidence_id=evidence_id,
                shadow_valid=False,
            ),
            evidence_id=evidence_id,
            round_number=1,
            dispatch_run_id=run_id,
        )


def test_free_text_rejection_fallback(stage_review_setup: StageReviewSetup) -> None:
    updated = stage_review_setup.manager.reject_review(
        stage_review_setup.task_id,
        "planning",
        rejection_notes="The fallback remains available.",
        round_number=1,
    )

    assert (
        stage_row(stage_review_setup.db, stage_review_setup.task_id, "planning")["state"] == "ready"
    )
    assert "The fallback remains available." in (updated.description or "")
    assert "```json" not in (updated.description or "")


@pytest.mark.asyncio
async def test_pre_spawn_snapshot_transport(
    stage_review_setup: StageReviewSetup,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sync_bundled_agents(stage_review_setup.db)
    captured: dict[str, object] = {}
    run_id = "f57e4e3b-2ac4-53ad-91ed-29ee556fef12"

    async def fake_spawn_agent_impl(**kwargs: object) -> dict[str, object]:
        captured.update(kwargs)
        rows = stage_review_setup.evidence.store.list_for_path(
            project_id=stage_review_setup.project_id,
            plan_path=stage_review_setup.plan_relative_path,
        )
        prepared = rows[-1]
        with pytest.raises(ReviewEvidenceError) as pending:
            stage_review_setup.manager.reject_review(
                stage_review_setup.task_id,
                "planning",
                findings=_findings(),
                coverage_attestation=coverage_attestation(
                    evidence_id=prepared.evidence_id,
                    shadow_valid=False,
                ),
                evidence_id=prepared.evidence_id,
                round_number=1,
                dispatch_run_id=run_id,
            )
        assert pending.value.code == "binding_pending"
        assert pending.value.retryable is True
        stage_review_setup.runs.create(
            parent_session_id=str(kwargs["parent_session_id"]),
            provider="codex",
            prompt=str(kwargs["prompt"]),
            agent_name=str(kwargs["agent_lookup_name"]),
            task_id=stage_review_setup.task_id,
            run_id=run_id,
        )
        return {"success": True, "run_id": run_id, "isolation": "none"}

    monkeypatch.setattr(
        "gobby.mcp_proxy.tools.spawn_agent._implementation.spawn_agent_impl",
        fake_spawn_agent_impl,
    )
    action = SpawnAgentAction(
        task_id=stage_review_setup.task_id,
        task_ref="#1",
        agent_slug="plan-adversary",
        prompt="Review the prepared plan.",
        initial_variables={"stage_name": "planning", "stage_state": "needs_review"},
    )
    services = SimpleNamespace(
        database=stage_review_setup.db,
        task_manager=stage_review_setup.manager,
        session_manager=stage_review_setup.sessions,
        agent_runner=SimpleNamespace(),
    )

    result = await spawn_agent(
        action,
        db=stage_review_setup.db,
        services=services,
    )

    assert result == run_id
    prompt = str(captured["prompt"])
    transported = prompt.partition("<plan-review-snapshot>\n")[2].partition(
        "\n</plan-review-snapshot>"
    )[0]
    assert transported.encode("utf-8") == stage_review_setup.plan_path.read_bytes()
    rows = stage_review_setup.evidence.store.list_for_path(
        project_id=stage_review_setup.project_id,
        plan_path=stage_review_setup.plan_relative_path,
    )
    prepared = rows[-1]
    assert prepared.task_id == stage_review_setup.task_id
    assert prepared.stage == "planning"
    assert prepared.round_number == 1
    assert prepared.dispatch_run_id == run_id
    assert prepared.evidence_id in prompt

    _hold_dispatch_mutex(
        stage_review_setup,
        task_id=stage_review_setup.task_id,
        run_id=run_id,
    )
    stage_review_setup.manager.reject_review(
        stage_review_setup.task_id,
        "planning",
        findings=_findings(),
        coverage_attestation=coverage_attestation(
            evidence_id=prepared.evidence_id,
            shadow_valid=False,
        ),
        evidence_id=prepared.evidence_id,
        round_number=1,
        dispatch_run_id=run_id,
    )
    TaskDispatchMutexManager(stage_review_setup.db).clear_by_run_id(run_id)
    repair_submission = _apply_round_one_repairs(stage_review_setup)
    stage_review_setup.manager.stage_states.start_stage(
        stage_review_setup.task_id,
        "planning",
        by_session_id=None,
    )
    stage_review_setup.manager.submit_for_review(
        stage_review_setup.task_id,
        "planning",
        repair_submission=repair_submission,
    )

    async def failed_spawn(**_kwargs: object) -> dict[str, object]:
        raise DispatchSpawnFailed("provider_failed")

    monkeypatch.setattr(
        "gobby.mcp_proxy.tools.spawn_agent._implementation.spawn_agent_impl",
        failed_spawn,
    )
    with pytest.raises(DispatchSpawnFailed, match="provider_failed"):
        await spawn_agent(action, db=stage_review_setup.db, services=services)
    failed_row = stage_review_setup.evidence.store.list_for_path(
        project_id=stage_review_setup.project_id,
        plan_path=stage_review_setup.plan_relative_path,
    )[-1]
    assert failed_row.round_number == 2
    assert failed_row.expired_at is not None

    wrong_run_id = "dbdb2055-cd26-5eba-87f2-a0a3ffc859f8"

    async def wrong_lineage_spawn(**kwargs: object) -> dict[str, object]:
        stage_review_setup.runs.create(
            parent_session_id=str(kwargs["parent_session_id"]),
            provider="codex",
            prompt=str(kwargs["prompt"]),
            agent_name=str(kwargs["agent_lookup_name"]),
            task_id=None,
            run_id=wrong_run_id,
        )
        return {"success": True, "run_id": wrong_run_id, "isolation": "none"}

    monkeypatch.setattr(
        "gobby.mcp_proxy.tools.spawn_agent._implementation.spawn_agent_impl",
        wrong_lineage_spawn,
    )
    with pytest.raises(DispatchSpawnFailed, match="run_lineage_mismatch"):
        await spawn_agent(action, db=stage_review_setup.db, services=services)
    bind_failed = stage_review_setup.evidence.store.list_for_path(
        project_id=stage_review_setup.project_id,
        plan_path=stage_review_setup.plan_relative_path,
    )[-1]
    assert bind_failed.expired_at is not None
    cancelled = stage_review_setup.runs.get(wrong_run_id)
    assert cancelled is not None
    assert cancelled.status == "cancelled"


def test_rejection_finalizes_evidence(
    stage_review_setup: StageReviewSetup,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    evidence_id, run_id = _prepare_bound(stage_review_setup)
    original_finalize = PlanReviewEvidenceService.finalize_plan_review_evidence

    def crash_finalize(
        self: PlanReviewEvidenceService,
        target_evidence_id: str,
        round_result: dict[str, object],
    ) -> None:
        del self, target_evidence_id, round_result
        raise RuntimeError("crash between rejection writes")

    monkeypatch.setattr(
        PlanReviewEvidenceService,
        "finalize_plan_review_evidence",
        crash_finalize,
    )
    with pytest.raises(RuntimeError, match="crash between rejection writes"):
        stage_review_setup.manager.reject_review(
            stage_review_setup.task_id,
            "planning",
            findings=_findings(),
            coverage_attestation=coverage_attestation(
                evidence_id=evidence_id,
                shadow_valid=False,
            ),
            evidence_id=evidence_id,
            round_number=1,
            dispatch_run_id=run_id,
        )

    after_crash = stage_review_setup.manager.get_task(stage_review_setup.task_id)
    assert "```json" not in (after_crash.description or "")
    assert (
        stage_row(stage_review_setup.db, stage_review_setup.task_id, "planning")["state"]
        == "needs_review"
    )
    assert stage_review_setup.evidence.get_evidence(evidence_id).finalized_at is None

    monkeypatch.setattr(
        PlanReviewEvidenceService,
        "finalize_plan_review_evidence",
        original_finalize,
    )
    first = stage_review_setup.manager.reject_review(
        stage_review_setup.task_id,
        "planning",
        findings=_findings(),
        coverage_attestation=coverage_attestation(
            evidence_id=evidence_id,
            shadow_valid=False,
        ),
        evidence_id=evidence_id,
        round_number=1,
        dispatch_run_id=run_id,
    )
    finalized = stage_review_setup.evidence.get_evidence(evidence_id)
    assert finalized.finalized_at is not None
    assert (
        stage_row(stage_review_setup.db, stage_review_setup.task_id, "planning")["state"] == "ready"
    )

    event_count = len(lifecycle_events(stage_review_setup.db, stage_review_setup.task_id))
    replay = stage_review_setup.manager.reject_review(
        stage_review_setup.task_id,
        "planning",
        findings=_findings(),
        coverage_attestation=coverage_attestation(
            evidence_id=evidence_id,
            shadow_valid=False,
        ),
        evidence_id=evidence_id,
        round_number=1,
        dispatch_run_id=run_id,
    )
    assert replay.description == first.description
    assert len(lifecycle_events(stage_review_setup.db, stage_review_setup.task_id)) == event_count
    assert (
        stage_review_setup.evidence.get_evidence(evidence_id).finalized_at == finalized.finalized_at
    )

    with pytest.raises(ReviewEvidenceError, match="no longer live"):
        stage_review_setup.manager.reject_review(
            stage_review_setup.task_id,
            "planning",
            findings=_findings(),
            coverage_attestation=coverage_attestation(
                evidence_id=evidence_id,
                shadow_valid=False,
            ),
            evidence_id=evidence_id,
            round_number=1,
            dispatch_run_id="wrong-run",
        )

    repair_submission = _apply_round_one_repairs(stage_review_setup)
    next_round = stage_review_setup.evidence.prepare_plan_review_round(
        project_id=stage_review_setup.project_id,
        plan_path=stage_review_setup.plan_path,
        round_number=2,
        task_id=stage_review_setup.task_id,
        stage="planning",
        prior_finding_resolutions=cast(
            list[dict[str, object]],
            repair_submission["prior_finding_resolutions"],
        ),
        repair_attestations=cast(
            list[dict[str, object]],
            repair_submission["repair_attestations"],
        ),
    )
    assert next_round.evidence_id != evidence_id
    assert stage_review_setup.evidence.get_evidence(evidence_id).finalized_at is not None


def test_approval_ledger_is_server_derived(
    stage_review_setup: StageReviewSetup,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import inspect

    from gobby.plans.review_ledger import merge_quality_ledger
    from gobby.storage.tasks._stage_states import StageStatesManager

    for method in (
        stage_review_setup.manager.approve_review,
        stage_review_setup.manager.reject_review,
    ):
        assert "quality_ledger" not in inspect.signature(method).parameters

    evidence_id, run_id = _prepare_bound(stage_review_setup)
    derived = stage_review_setup.evidence.derive_plan_review_manifest(
        evidence_id,
        routing_decisions={},
    )
    manifest_entries = derived["manifest_entries"]
    assert isinstance(manifest_entries, list)
    major = dict(_findings()[0])
    major["severity"] = "major"
    major.pop("failure_trace")
    approval_attestation = coverage_attestation(
        evidence_id=evidence_id,
        manifest_entries=manifest_entries,
    )

    stage_mutated = False
    original_stage_approval = StageStatesManager.approve_review
    original_merge = merge_quality_ledger

    def mark_stage_mutation(
        self: StageStatesManager,
        task_id: str,
        stage_name: str,
        *,
        by_session_id: str | None,
        notes: str | None = None,
        dispatch_run_id: str | None = None,
    ) -> Any:
        nonlocal stage_mutated
        stage_mutated = True
        return original_stage_approval(
            self,
            task_id,
            stage_name,
            by_session_id=by_session_id,
            notes=notes,
            dispatch_run_id=dispatch_run_id,
        )

    def fail_derivation(**_kwargs: object) -> list[dict[str, object]]:
        raise ReviewEvidenceError("ledger_derivation_failed", "synthetic ledger failure")

    monkeypatch.setattr(StageStatesManager, "approve_review", mark_stage_mutation)
    monkeypatch.setattr(
        "gobby.plans.review_checkpoint_service.merge_quality_ledger",
        fail_derivation,
    )
    with pytest.raises(ReviewEvidenceError, match="synthetic ledger failure"):
        stage_review_setup.manager.approve_review(
            stage_review_setup.task_id,
            "planning",
            evidence_id=evidence_id,
            round_number=1,
            findings=[major],
            routing_decisions={},
            manifest_entries=manifest_entries,
            coverage_attestation=approval_attestation,
            dispatch_run_id=run_id,
        )
    assert stage_mutated is False
    assert (
        stage_row(stage_review_setup.db, stage_review_setup.task_id, "planning")["state"]
        == "needs_review"
    )

    monkeypatch.setattr(
        "gobby.plans.review_checkpoint_service.merge_quality_ledger",
        original_merge,
    )
    stage_review_setup.manager.approve_review(
        stage_review_setup.task_id,
        "planning",
        evidence_id=evidence_id,
        round_number=1,
        findings=[major],
        routing_decisions={},
        manifest_entries=manifest_entries,
        coverage_attestation=approval_attestation,
        dispatch_run_id=run_id,
    )

    finalized = stage_review_setup.evidence.get_evidence(evidence_id)
    assert finalized.approval_result is not None
    displayed_ledger = finalized.approval_result["quality_ledger"]
    assert displayed_ledger == finalized.quality_ledger
    assert isinstance(displayed_ledger, list)
    assert displayed_ledger[0]["check_key"] == "failure-atomicity"
