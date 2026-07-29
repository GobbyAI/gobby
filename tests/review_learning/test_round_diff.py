from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal, cast

import pytest

from gobby.plans.consumer_sweep import CandidateSite, CandidateSiteInventory
from gobby.plans.review_evidence import PlanReviewEvidenceService
from gobby.plans.review_evidence_models import PlanReviewEvidence, SectionHash
from gobby.plans.review_sweep_scope import SweepRequirement, SweepScope
from gobby.review_learning.recorders import mint_plan_review_lessons
from gobby.review_learning.round_diff import (
    PlanReviewLessonCandidate,
    classify_plan_review_rounds,
    select_plan_review_candidates,
)
from gobby.storage.agents import LocalAgentRunManager
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.projects import LocalProjectManager
from gobby.storage.sessions import SessionManager
from gobby.storage.tasks import LocalTaskManager
from tests.review_coverage_helpers import (
    StageReviewSetup,
    coverage_attestation,
    prepare_bound_review,
)
from tests.review_coverage_helpers import (
    stage_review_setup as stage_review_setup_fixture,  # noqa: F401 - pytest fixture
)
from tests.review_telemetry_helpers import enriched_telemetry

PLAN_PATH = ".gobby/plans/review.md"
TASK_ID = "task-lineage"
STAGE = "planning"
_SWEEP_SCOPE_DIGEST = "a" * 64


def _finding(
    finding_id: str,
    *,
    severity: str = "blocking",
    participating: list[str] | None = None,
    causal: list[str] | None = None,
    causal_finding_id: str | None = None,
    introduced_in_round: int | None = None,
    principle: str | None = "Review the complete invariant.",
) -> dict[str, object]:
    finding: dict[str, object] = {
        "finding_id": finding_id,
        "section_id": "A",
        "check_key": f"check-{finding_id.lower()}",
        "severity": severity,
        "category": "unhandled-edge",
        "location": "§ A",
        "description": f"Finding {finding_id}",
        "minimal_repair": f"Fix {finding_id}",
        "repair_scope": "existing_sections",
        "prevention": f"Prevent {finding_id}",
    }
    if severity == "blocking":
        finding["failure_trace"] = {
            "preconditions": "The original plan is otherwise unchanged.",
            "action": f"The reviewer exercises {finding_id}.",
            "wrong_outcome": f"Finding {finding_id} remains reachable.",
            "violated_obligation": "The reviewed plan must close blocking failure paths.",
            "citation": [{"path": "plan.md", "sha256": "0" * 64}],
        }
    if principle is not None:
        finding["principle"] = principle
    if participating is not None:
        finding["participating_section_ids"] = participating
    if causal is not None:
        finding["causal_section_ids"] = causal
    if causal_finding_id is not None:
        finding["causal_finding_id"] = causal_finding_id
    if introduced_in_round is not None:
        finding["introduced_in_round"] = introduced_in_round
    return finding


def test_findings_use_canonical_remedy_field() -> None:
    finding = _finding("F1")

    assert finding["minimal_repair"] == "Fix F1"
    assert "fix" not in finding
    assert "failure_trace" in finding


def _row(
    round_number: int,
    hashes: dict[str, str],
    findings: list[dict[str, object]],
    *,
    task_id: str = TASK_ID,
    stage: str = STAGE,
    project_id: str = "project",
    plan_path: str = PLAN_PATH,
    finalized: bool = True,
    verdict: str = "needs_review",
    evidence_id: str | None = None,
) -> PlanReviewEvidence:
    created_at = datetime(2026, 1, 1, tzinfo=UTC) + timedelta(minutes=round_number)
    round_result: dict[str, object] = {
        "verdict": verdict,
        "findings": findings,
        "coverage_attestation": coverage_attestation(
            evidence_id=evidence_id or f"evidence-{round_number}-{task_id}",
            shadow_valid=verdict == "approved",
            manifest_entries=[{"source_section": "A"}] if verdict == "approved" else None,
        ),
        "convergence_telemetry": enriched_telemetry(),
    }
    if verdict == "approved":
        round_result["manifest_entries"] = [{"source_section": "A"}]
        round_result["routing_decisions"] = {}
    return PlanReviewEvidence(
        evidence_id=evidence_id or f"evidence-{round_number}-{task_id}",
        project_id=project_id,
        plan_path=plan_path,
        plan_hash=f"plan-{round_number}",
        section_manifest=tuple(
            SectionHash(section_id=section_id, section_hash=section_hash)
            for section_id, section_hash in hashes.items()
        ),
        snapshot=f"snapshot-{round_number}".encode(),
        round_number=round_number,
        session_id=None,
        task_id=task_id,
        stage=stage,
        dispatch_run_id=f"run-{round_number}",
        lease_expires_at=None,
        finalized_at=created_at if finalized else None,
        expired_at=None,
        round_result=round_result,
        approval_result=round_result if verdict == "approved" and finalized else None,
        approved_at=created_at if verdict == "approved" and finalized else None,
        lesson_mint_status="pending" if verdict == "approved" and finalized else None,
        lesson_mint_detail=None,
        manifest_digest=None,
        manifest_payload=None,
        manifest_state=None,
        manifest_result=None,
        manifest_applied_at=None,
        quality_ledger=None,
        repair_attestations=None,
        prior_round_context=None,
        created_at=created_at,
    )


def _classes(rows: list[PlanReviewEvidence], finding_id: str) -> set[str]:
    return {
        candidate.lesson_type
        for candidate in classify_plan_review_rounds(rows, task_id=TASK_ID, stage=STAGE)
        if candidate.finding["finding_id"] == finding_id
    }


@pytest.mark.unit
def test_classification_matrix() -> None:
    base = _row(1, {"A": "a1", "B": "b1", "C": "c1"}, [_finding("BASE")])
    reviewer = _row(
        2,
        {"A": "a1", "B": "b1", "C": "c1"},
        [_finding("REVIEWER", participating=["A", "B"])],
    )
    assert _classes([base, reviewer], "REVIEWER") == {"reviewer-miss"}

    fixer = _row(
        2,
        {"A": "a2", "B": "b1", "C": "c1"},
        [
            _finding(
                "FIXER",
                participating=["A", "B"],
                causal=["A"],
                causal_finding_id="BASE",
                introduced_in_round=1,
            )
        ],
    )
    assert _classes([base, fixer], "FIXER") == {"fixer-induced-defect"}

    dual = _row(
        2,
        {"A": "a2", "B": "b1", "C": "c1"},
        [
            _finding(
                "DUAL",
                participating=["B", "C"],
                causal=["A"],
                causal_finding_id="BASE",
                introduced_in_round=1,
            )
        ],
    )
    assert _classes([base, dual], "DUAL") == {
        "reviewer-miss",
        "fixer-induced-defect",
    }

    unattested = _row(
        2,
        {"A": "a1", "B": "b1", "C": "c1"},
        [_finding("UNATTESTED", participating=["A"], principle=None)],
    )
    assert _classes([base, unattested], "UNATTESTED") == set()
    nit = _row(
        2,
        {"A": "a1", "B": "b1", "C": "c1"},
        [_finding("NIT", severity="nit", participating=["A"])],
    )
    assert _classes([base, nit], "NIT") == set()

    multi_reviewer = _row(
        2,
        {"A": "a1", "B": "b1", "C": "c1"},
        [_finding("MULTI-REVIEWER", participating=["A", "B", "C"])],
    )
    assert _classes([base, multi_reviewer], "MULTI-REVIEWER") == {"reviewer-miss"}
    changed_participant = _row(
        2,
        {"A": "a2", "B": "b1", "C": "c1"},
        [_finding("CHANGED-PARTICIPANT", participating=["A", "B"])],
    )
    assert _classes([base, changed_participant], "CHANGED-PARTICIPANT") == set()

    multi_causal = _row(
        2,
        {"A": "a2", "B": "b2", "C": "c1"},
        [
            _finding(
                "MULTI-CAUSAL",
                causal=["A", "B"],
                causal_finding_id="BASE",
                introduced_in_round=1,
            )
        ],
    )
    assert _classes([base, multi_causal], "MULTI-CAUSAL") == {"fixer-induced-defect"}
    unchanged_causal = _row(
        2,
        {"A": "a2", "B": "b1", "C": "c1"},
        [
            _finding(
                "UNCHANGED-CAUSAL",
                causal=["A", "B"],
                causal_finding_id="BASE",
                introduced_in_round=1,
            )
        ],
    )
    assert _classes([base, unchanged_causal], "UNCHANGED-CAUSAL") == set()


def _candidate(
    lesson_type: Literal["reviewer-miss", "fixer-induced-defect"],
    finding_id: str,
    metric: int,
) -> PlanReviewLessonCandidate:
    return PlanReviewLessonCandidate(
        lesson_type=lesson_type,
        evidence_id=f"evidence-{finding_id}",
        round_number=metric + 1,
        finding=_finding(finding_id),
        proof={"metric": metric},
        metric=metric,
    )


@pytest.mark.unit
def test_class_aware_cap_selection() -> None:
    candidates = [
        _candidate("reviewer-miss", "R3", 3),
        _candidate("fixer-induced-defect", "F1", 1),
        _candidate("reviewer-miss", "R2", 2),
        _candidate("fixer-induced-defect", "F4", 4),
        _candidate("reviewer-miss", "R1", 1),
        _candidate("fixer-induced-defect", "F2", 2),
        _candidate("reviewer-miss", "R4", 4),
    ]
    selected = select_plan_review_candidates(list(reversed(candidates)), limit=5)

    assert len(selected) == 5
    assert {candidate.lesson_type for candidate in selected} == {
        "reviewer-miss",
        "fixer-induced-defect",
    }
    assert [(candidate.lesson_type, candidate.finding["finding_id"]) for candidate in selected] == [
        ("reviewer-miss", "R4"),
        ("fixer-induced-defect", "F4"),
        ("reviewer-miss", "R3"),
        ("reviewer-miss", "R2"),
        ("fixer-induced-defect", "F2"),
    ]
    assert select_plan_review_candidates(candidates, limit=5) == selected


@pytest.mark.unit
def test_class_aware_cap_tracks_duplicate_candidates_by_position() -> None:
    duplicate = _candidate("reviewer-miss", "duplicate", 3)
    fixer = _candidate("fixer-induced-defect", "fixer", 3)

    selected = select_plan_review_candidates([duplicate, duplicate, fixer], limit=3)

    assert selected == [duplicate, fixer, duplicate]


@pytest.mark.unit
def test_invalid_round_payload_is_logged(caplog: pytest.LogCaptureFixture) -> None:
    row = _row(1, {"A": "hash"}, [])
    invalid = replace(row, round_result={"verdict": "needs_review", "findings": "invalid"})

    with caplog.at_level("DEBUG", logger="gobby.review_learning.round_diff"):
        assert classify_plan_review_rounds([invalid], task_id=TASK_ID, stage=STAGE) == []

    assert invalid.evidence_id in caplog.text
    assert "Skipping invalid findings" in caplog.text


@pytest.mark.unit
def test_historical_evidence_lineage() -> None:
    base = _row(1, {"A": "a1"}, [_finding("BASE")])
    current = _row(2, {"A": "a1"}, [_finding("CURRENT", participating=["A"])])
    cross_task = _row(
        3,
        {"A": "a1"},
        [_finding("CROSS-TASK", participating=["A"])],
        task_id="another-task",
    )
    cross_plan = _row(
        3,
        {"A": "a1"},
        [_finding("CROSS-PLAN", participating=["A"])],
        plan_path=".gobby/plans/other.md",
    )
    unfinalized = _row(
        3,
        {"A": "a1"},
        [_finding("UNFINALIZED", participating=["A"])],
        finalized=False,
    )

    candidates = classify_plan_review_rounds(
        [cross_task, unfinalized, current, cross_plan, base],
        task_id=TASK_ID,
        stage=STAGE,
    )
    assert [
        (candidate.finding["finding_id"], candidate.lesson_type) for candidate in candidates
    ] == [("CURRENT", "reviewer-miss")]


@dataclass
class DurableLineage:
    db: HubDatabase
    manager: LocalTaskManager
    service: PlanReviewEvidenceService
    task_id: str
    stage: str
    session_id: str
    plan_path: Path
    approval_evidence_id: str


class StubReviewLearningService:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.calls: list[dict[str, Any]] = []

    async def record(self, **kwargs: Any) -> dict[str, object]:
        self.calls.append(kwargs)
        if self.fail:
            raise RuntimeError("recorder unavailable")
        return {"lesson_id": "lesson-1"}


def _plan_text(body: str = "Stable requirement.") -> str:
    return "\n".join(
        [
            "# Review",
            "**Plan ID:** review",
            "",
            "## P1 Foundation",
            "`kind: framing`",
            "",
            "### 1.1 Work",
            "`kind: deliverable`",
            "",
            body,
            "",
            "**Acceptance:**",
            "- 1.1.1 — Works. test: `tests/test_work.py`",
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
            "- title: Work",
            "  source_section: '1.1'",
            "  covers: [1.1.1]",
            "  category: code",
            "  implementation_domain: backend",
            "  priority: 2",
            "  task_type: feature",
            "  tdd: false",
            "  labels: [covers:review:1.1:1.1.1]",
            "  description: Work.",
            "  validation_criteria: Tested.",
            "```",
            "",
        ]
    )


def _persist_round(
    lineage: DurableLineage,
    *,
    round_number: int,
    findings: list[dict[str, object]],
    verdict: str = "needs_review",
    prior_finding_resolutions: list[dict[str, object]] | None = None,
    repair_attestations: list[dict[str, object]] | None = None,
) -> str:
    repair_ids = [
        str(resolution["prior_finding_id"])
        for resolution in prior_finding_resolutions or []
        if resolution["decision"] == "repair"
    ]
    scope: SweepScope | None = None
    if repair_ids:
        project_id = lineage.manager.get_task(lineage.task_id).project_id
        prior = lineage.service.store.list_for_path(
            project_id=project_id,
            plan_path=PLAN_PATH,
        )[-1]
        _inventory, scope = _settled_repair_inputs(
            prior_evidence=prior,
            repair_finding_ids=repair_ids,
        )
        for attestation in repair_attestations or []:
            if "sweep_scope_digest" in attestation:
                attestation["sweep_scope_digest"] = scope.digest
    prepared = lineage.service.prepare_plan_review_round(
        project_id=lineage.manager.get_task(lineage.task_id).project_id,
        plan_path=lineage.plan_path,
        round_number=round_number,
        task_id=lineage.task_id,
        stage=lineage.stage,
        prior_finding_resolutions=prior_finding_resolutions,
        repair_attestations=repair_attestations,
        sweep_scope=scope.to_dict() if scope is not None else None,
        sweep_scope_digest=scope.digest if scope is not None else None,
    )
    run = LocalAgentRunManager(lineage.db).create(
        parent_session_id=lineage.session_id,
        provider="codex",
        prompt=f"round {round_number}",
        task_id=lineage.task_id,
    )
    lineage.service.bind_evidence_run(prepared.evidence_id, run.id)
    result: dict[str, object] = {"verdict": verdict, "findings": findings}
    if verdict == "approved":
        result["manifest_entries"] = [{"source_section": "1.1"}]
        result["routing_decisions"] = {}
        result["coverage_attestation"] = coverage_attestation(
            evidence_id=prepared.evidence_id,
            manifest_entries=[{"source_section": "1.1"}],
        )
    else:
        result["coverage_attestation"] = coverage_attestation(
            evidence_id=prepared.evidence_id,
            shadow_valid=False,
        )
    result["convergence_telemetry"] = enriched_telemetry()
    lineage.service.finalize_plan_review_evidence(prepared.evidence_id, result)
    return prepared.evidence_id


def _repair_attestation(finding_id: str) -> dict[str, object]:
    return {
        "prior_finding_id": finding_id,
        "check_key": f"check-{finding_id.lower()}",
        "changed_section_ids": ["1.1"],
        "accepted_resolution": f"Fix {finding_id}",
        "deviation_from_minimal_repair": None,
        "changed_symbols": ["gobby.review_learning.repaired"],
        "consumer_sites_swept": ["src/gobby/review_learning/service.py"],
        "adjacent_variants_swept": ["src/gobby/review_learning/lessons.py"],
        "validation_evidence": ["pytest tests/review_learning/test_round_diff.py"],
        "deferred_sites": [],
        "sweep_scope_digest": _SWEEP_SCOPE_DIGEST,
        "sweep_query_evidence": [],
        "repair_bundle_interactions": [],
    }


def _settled_repair_inputs(
    *,
    prior_evidence: PlanReviewEvidence,
    repair_finding_ids: list[str] | tuple[str, ...],
    **_kwargs: object,
) -> tuple[CandidateSiteInventory, SweepScope]:
    assert prior_evidence.round_result is not None
    findings = cast(list[dict[str, object]], prior_evidence.round_result["findings"])
    finding_map = {cast(str, finding["finding_id"]): finding for finding in findings}
    consumer_site = "src/gobby/review_learning/service.py"
    adjacent_site = "src/gobby/review_learning/lessons.py"
    sites = tuple(
        CandidateSite(
            site_id=site_id,
            path=site_id,
            source_kind="symbol_call",
            source_ref="gobby.review_learning.repaired",
            status="resolved",
            language="python",
            section_ids=("1.1",),
        )
        for site_id in (consumer_site, adjacent_site)
    )
    inventory = CandidateSiteInventory(
        changed_acceptance_item_ids=("1.1.1",),
        changed_targets=(),
        changed_symbols=("gobby.review_learning.repaired",),
        changed_contracts=(),
        targets_by_section={},
        contracts_by_section={},
        resolved_languages=("python",),
        unsupported_targets=(),
        sites=sites,
    )
    universe = SweepScope(
        candidate_sites=sites,
        requirements=tuple(
            SweepRequirement(
                prior_finding_id=finding_id,
                check_key=cast(str, finding_map[finding_id]["check_key"]),
                changed_section_ids=(cast(str, finding_map[finding_id]["section_id"]),),
                changed_contracts=(),
                changed_targets=(),
                required_consumer_site_ids=(consumer_site,),
                adjacent_variant_ids=(adjacent_site,),
                interaction_edge_ids=(),
            )
            for finding_id in repair_finding_ids
        ),
        interaction_edges=(),
    )
    return inventory, universe


def _create_durable_lineage(
    temp_db: HubDatabase,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> DurableLineage:
    monkeypatch.setattr(
        "gobby.plans.review_evidence_preparation.derive_settled_sweep_inputs",
        _settled_repair_inputs,
    )
    project = LocalProjectManager(temp_db).create(
        name="round-diff",
        repo_path=str(tmp_path),
    )
    session = SessionManager(temp_db).register(
        external_id="round-diff-parent",
        machine_id="test-machine",
        source="codex",
        project_id=project.id,
    )
    plan_path = tmp_path / PLAN_PATH
    plan_path.parent.mkdir(parents=True)
    plan_path.write_text(_plan_text(), encoding="utf-8")
    manager = LocalTaskManager(temp_db)
    task = manager.create_task(
        project.id,
        "Plan review lesson lineage",
        task_type="review_anchor",
        category="planning",
        isolation="none",
        validation_criteria="Test task completion is observable.",
    )
    lineage = DurableLineage(
        db=temp_db,
        manager=manager,
        service=PlanReviewEvidenceService(temp_db),
        task_id=task.id,
        stage=STAGE,
        session_id=session.id,
        plan_path=plan_path,
        approval_evidence_id="",
    )
    _persist_round(
        lineage,
        round_number=1,
        findings=[_finding("BASE", participating=["1.1"]) | {"section_id": "1.1"}],
    )
    plan_path.write_text(_plan_text("Round two repairs BASE."), encoding="utf-8")
    _persist_round(
        lineage,
        round_number=2,
        findings=[
            _finding(
                "MISS",
                causal=["1.1"],
                causal_finding_id="BASE",
                introduced_in_round=1,
            )
            | {"section_id": "1.1"}
        ],
        prior_finding_resolutions=[
            {"prior_finding_id": "BASE", "decision": "repair"},
        ],
        repair_attestations=[_repair_attestation("BASE")],
    )
    plan_path.write_text(_plan_text("Round three repairs MISS."), encoding="utf-8")
    lineage.approval_evidence_id = _persist_round(
        lineage,
        round_number=3,
        findings=[],
        verdict="approved",
        prior_finding_resolutions=[
            {"prior_finding_id": "MISS", "decision": "repair"},
        ],
        repair_attestations=[_repair_attestation("MISS")],
    )
    return lineage


@pytest.fixture
def durable_lineage(
    temp_db: HubDatabase,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> DurableLineage:
    return _create_durable_lineage(temp_db, tmp_path, monkeypatch)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_backfill_idempotence(durable_lineage: DurableLineage) -> None:
    recorder = StubReviewLearningService(fail=True)
    failed = await mint_plan_review_lessons(
        durable_lineage.task_id,
        durable_lineage.stage,
        db=durable_lineage.db,
        review_learning_service=recorder,
        session_id=durable_lineage.session_id,
    )
    assert failed["lesson_mint_status"] == "failed"

    recorder.fail = False
    minted = await mint_plan_review_lessons(
        durable_lineage.task_id,
        durable_lineage.stage,
        db=durable_lineage.db,
        review_learning_service=recorder,
        session_id=durable_lineage.session_id,
    )
    assert minted["lesson_mint_status"] == "minted"
    assert minted["minted_lesson_ids"] == ["lesson-1"]

    replay = await mint_plan_review_lessons(
        durable_lineage.task_id,
        durable_lineage.stage,
        db=durable_lineage.db,
        review_learning_service=recorder,
        session_id=durable_lineage.session_id,
    )
    assert replay == minted
    assert len(recorder.calls) == 2


@pytest.mark.integration
@pytest.mark.asyncio
async def test_escalation_mints_nothing(durable_lineage: DurableLineage) -> None:
    durable_lineage.manager.escalate_task(durable_lineage.task_id, "abandoned")
    recorder = StubReviewLearningService()

    result = await mint_plan_review_lessons(
        durable_lineage.task_id,
        durable_lineage.stage,
        db=durable_lineage.db,
        review_learning_service=recorder,
        session_id=durable_lineage.session_id,
    )

    assert result["lesson_mint_status"] == "none"
    assert result["minted_lesson_ids"] == []
    assert recorder.calls == []


@pytest.mark.integration
@pytest.mark.asyncio
async def test_description_mutation_immunity(durable_lineage: DurableLineage) -> None:
    durable_lineage.manager.update_task(
        durable_lineage.task_id,
        description="The durable findings projection was replaced wholesale.",
    )
    recorder = StubReviewLearningService()

    result = await mint_plan_review_lessons(
        durable_lineage.task_id,
        durable_lineage.stage,
        db=durable_lineage.db,
        review_learning_service=recorder,
        session_id=durable_lineage.session_id,
    )

    assert result["lesson_mint_status"] == "minted"
    assert recorder.calls[0]["finding"]["finding_id"] == "MISS"


@pytest.mark.integration
def test_approval_evidence_finalization(
    request: pytest.FixtureRequest,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from gobby.plans.review_evidence_models import ReviewEvidenceError
    from gobby.plans.review_evidence_store import PlanReviewEvidenceStore

    setup = cast(
        StageReviewSetup,
        request.getfixturevalue("stage_review_setup_fixture"),
    )
    evidence_id, run_id = prepare_bound_review(setup)
    derived = setup.evidence.derive_plan_review_manifest(
        evidence_id,
        routing_decisions={},
    )
    raw_manifest_entries = derived["manifest_entries"]
    assert isinstance(raw_manifest_entries, list)
    manifest_entries = cast(list[dict[str, object]], raw_manifest_entries)
    findings: list[dict[str, object]] = []
    routing_decisions: dict[str, object] = {}
    attestation = coverage_attestation(
        evidence_id=evidence_id,
        manifest_entries=manifest_entries,
    )
    telemetry = enriched_telemetry()
    approval: dict[str, object] = {
        "findings": findings,
        "routing_decisions": routing_decisions,
        "manifest_entries": manifest_entries,
        "coverage_attestation": attestation,
        "convergence_telemetry": telemetry,
    }

    with pytest.raises(ReviewEvidenceError) as wrong_round:
        setup.manager.approve_review(
            setup.task_id,
            "planning",
            evidence_id=evidence_id,
            round_number=2,
            findings=findings,
            routing_decisions=routing_decisions,
            manifest_entries=manifest_entries,
            coverage_attestation=attestation,
            convergence_telemetry=telemetry,
            dispatch_run_id=run_id,
        )
    assert wrong_round.value.code == "wrong_attempt"
    wrong_state = setup.manager.stage_states.current_stage(setup.task_id)
    assert wrong_state is not None
    assert wrong_state.state == "needs_review"

    original_finalize = PlanReviewEvidenceStore.finalize

    def crash_finalize(self: PlanReviewEvidenceStore, **_kwargs: object) -> PlanReviewEvidence:
        raise RuntimeError("crash inside approval commit")

    monkeypatch.setattr(PlanReviewEvidenceStore, "finalize", crash_finalize)
    with pytest.raises(RuntimeError, match="crash inside approval commit"):
        setup.manager.approve_review(
            setup.task_id,
            "planning",
            evidence_id=evidence_id,
            round_number=1,
            findings=findings,
            routing_decisions=routing_decisions,
            manifest_entries=manifest_entries,
            coverage_attestation=attestation,
            convergence_telemetry=telemetry,
            dispatch_run_id=run_id,
        )
    interrupted_state = setup.manager.stage_states.current_stage(setup.task_id)
    assert interrupted_state is not None
    assert interrupted_state.state == "needs_review"
    interrupted = setup.evidence.get_evidence(evidence_id)
    assert interrupted.manifest_state is None
    assert interrupted.round_result is None
    assert interrupted.finalized_at is None

    monkeypatch.setattr(PlanReviewEvidenceStore, "finalize", original_finalize)
    approved = setup.manager.approve_review(
        setup.task_id,
        "planning",
        evidence_id=evidence_id,
        round_number=1,
        findings=findings,
        routing_decisions=routing_decisions,
        manifest_entries=manifest_entries,
        coverage_attestation=attestation,
        convergence_telemetry=telemetry,
        dispatch_run_id=run_id,
    )
    finalized = setup.evidence.get_evidence(evidence_id)
    assert approved.id == setup.task_id
    approved_state = setup.manager.stage_states.current_stage(setup.task_id)
    assert approved_state is not None
    assert approved_state.state == "review_approved"
    assert finalized.finalized_at is not None
    assert finalized.approval_result == {
        "verdict": "approved",
        **approval,
        "quality_ledger": [],
    }
    assert finalized.lesson_mint_status == "pending"

    replay = setup.manager.approve_review(
        setup.task_id,
        "planning",
        evidence_id=evidence_id,
        round_number=1,
        findings=findings,
        routing_decisions=routing_decisions,
        manifest_entries=manifest_entries,
        coverage_attestation=attestation,
        convergence_telemetry=telemetry,
        dispatch_run_id=run_id,
    )
    assert replay.id == setup.task_id
    assert setup.evidence.get_evidence(evidence_id).approved_at == finalized.approved_at
