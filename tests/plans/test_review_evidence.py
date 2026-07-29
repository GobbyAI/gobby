from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Never

import pytest

from gobby.plans.consumer_sweep import CandidateSite, CandidateSiteInventory
from gobby.plans.digests import canonical_json_sha256
from gobby.plans.review_evidence import PlanReviewEvidenceService
from gobby.plans.review_evidence_io import (
    atomic_write_bytes,
    build_section_manifest,
    ensure_checkpoint,
    manifest_key,
)
from gobby.plans.review_evidence_models import ReviewEvidenceError
from gobby.plans.review_requirements import (
    REQUEST_ANCHOR_VARIABLE,
    build_request_anchor,
    requirements_bundle_from_context,
    validate_source_citation,
)
from gobby.plans.review_sweep_scope import SweepScope, derive_sweep_scope
from gobby.plans.review_telemetry import persist_delivered_round_result
from gobby.plans.review_terminal import terminalize_plan_review_run
from gobby.storage.agents import LocalAgentRunManager
from gobby.storage.hub.protocol import (
    HubDatabase,
    PlanReviewEvidenceMutation,
    Transaction,
)
from gobby.storage.migrations import _execute_sql_script
from gobby.storage.projects import LocalProjectManager
from gobby.storage.sessions import SessionManager
from gobby.storage.tasks import LocalTaskManager
from gobby.workflows.state_manager import SessionVariableManager
from tests.review_coverage_helpers import coverage_attestation
from tests.review_telemetry_helpers import delivered_telemetry, enriched_telemetry

pytestmark = pytest.mark.integration


@pytest.fixture
def review_setup(
    temp_db: HubDatabase,
    tmp_path: Path,
) -> tuple[PlanReviewEvidenceService, str, str, Path]:
    project = LocalProjectManager(temp_db).create(
        name="review-evidence",
        repo_path=str(tmp_path),
    )
    session = SessionManager(temp_db).register(
        external_id="review-evidence-parent",
        machine_id="test-machine",
        source="codex",
        project_id=project.id,
    )
    plan_dir = tmp_path / ".gobby" / "plans"
    plan_dir.mkdir(parents=True)
    plan_path = plan_dir / "review-evidence.md"
    plan_path.write_text(
        "\n".join(
            [
                "# Review Evidence",
                "**Plan ID:** review-evidence",
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
                "- 1.1.1 — Behavior exists. test: `tests/test_example.py`",
                "",
                "## Task Mapping",
                "`kind: framing`",
                "",
                "Pending.",
                "",
                "## V1 Plan Changelog",
                "`kind: verification`",
                "",
                "No rounds yet.",
                "",
                "## M1 Task Manifest",
                "`kind: manifest`",
                "",
                "```yaml",
                "- title: Implement example",
                "  source_section: '1.1'",
                "  covers:",
                "    - 1.1.1",
                "  category: code",
                "  implementation_domain: backend",
                "  priority: 2",
                "  task_type: feature",
                "  tdd: false",
                "  labels:",
                "    - covers:review-evidence:1.1:1.1.1",
                "  description: Implement the example.",
                "  validation_criteria: Example behavior is tested.",
                "```",
                "",
            ]
        ),
        encoding="utf-8",
    )
    SessionVariableManager(temp_db).merge_variables(
        session.id,
        {
            REQUEST_ANCHOR_VARIABLE: build_request_anchor(
                "review-evidence-request",
                "Review the evidence plan",
            )
        },
    )
    return PlanReviewEvidenceService(temp_db), project.id, session.id, plan_path


@dataclass(frozen=True)
class ManifestReviewSetup:
    service: PlanReviewEvidenceService
    project_id: str
    session_id: str
    plan_path: Path
    evidence_id: str
    run_id: str
    approval: dict[str, object]
    original_bytes: bytes


def _canonical_approval(
    service: PlanReviewEvidenceService,
    evidence_id: str,
) -> dict[str, object]:
    derived = service.derive_plan_review_manifest(
        evidence_id,
        routing_decisions={},
    )
    manifest_entries = derived["manifest_entries"]
    assert isinstance(manifest_entries, list)
    return {
        "verdict": "approved",
        "findings": [],
        "routing_decisions": {},
        "manifest_entries": manifest_entries,
        "coverage_attestation": coverage_attestation(
            evidence_id=evidence_id,
            manifest_entries=manifest_entries,
        ),
        "convergence_telemetry": enriched_telemetry(),
    }


@pytest.fixture
def manifest_review(
    review_setup: tuple[PlanReviewEvidenceService, str, str, Path],
) -> ManifestReviewSetup:
    service, project_id, session_id, plan_path = review_setup
    prepared = service.prepare_plan_review_round(
        project_id=project_id,
        plan_path=plan_path,
        round_number=1,
        session_id=session_id,
    )
    run = LocalAgentRunManager(service.db).create(
        parent_session_id=session_id,
        provider="codex",
        prompt="review",
    )
    service.bind_evidence_run(prepared.evidence_id, run.id)
    return ManifestReviewSetup(
        service=service,
        project_id=project_id,
        session_id=session_id,
        plan_path=plan_path,
        evidence_id=prepared.evidence_id,
        run_id=run.id,
        approval=_canonical_approval(service, prepared.evidence_id),
        original_bytes=plan_path.read_bytes(),
    )


def test_prepare_round_snapshot(
    review_setup: tuple[PlanReviewEvidenceService, str, str, Path],
) -> None:
    service, project_id, session_id, plan_path = review_setup
    expected_snapshot = plan_path.read_bytes()

    prepared = service.prepare_plan_review_round(
        project_id=project_id,
        plan_path=plan_path,
        round_number=1,
        session_id=session_id,
    )
    assert (
        service.prepare_plan_review_round(
            project_id=project_id,
            plan_path=plan_path,
            round_number=1,
            session_id=session_id,
        ).evidence_id
        == prepared.evidence_id
    )
    with pytest.raises(ReviewEvidenceError) as active_attempt:
        service.prepare_plan_review_round(
            project_id=project_id,
            plan_path=plan_path,
            round_number=2,
            session_id=session_id,
        )
    assert active_attempt.value.code == "review_round_active"

    assert prepared.plan_hash
    assert prepared.sections[0].section_id == "__preamble__"
    assert service.snapshot_bytes(prepared.evidence_id) == expected_snapshot
    row = service.get_evidence(prepared.evidence_id)
    assert row.snapshot == expected_snapshot
    assert row.session_id == session_id
    assert row.task_id is None
    assert row.lease_expires_at is not None


def test_section_hash_canonicalization() -> None:
    snapshot = (
        b"# Title\n**Plan ID:** p\n\n"
        b"## 3.1 Work\nalpha\n\n"
        b"## Task   Mapping\nbeta\n\n"
        b"## V1 Plan Changelog\ngamma\n\n"
        b"## M1 Task Manifest\ndelta\n"
    )
    first = build_section_manifest(snapshot)
    second = build_section_manifest(snapshot)
    assert first == second
    assert [section.section_id for section in first] == [
        "__preamble__",
        "3.1",
        "Task Mapping",
        "V1",
        "M1",
    ]
    assert manifest_key("## 3.1 Work") == "3.1"
    assert manifest_key("## Task Mapping") == "Task Mapping"
    assert manifest_key("## V1 Plan Changelog") == "V1"
    assert manifest_key("## M1 Task Manifest") == "M1"

    changed = build_section_manifest(snapshot.replace(b"alpha", b"omega"))
    differing = [
        before.section_id
        for before, after in zip(first, changed, strict=True)
        if before.section_hash != after.section_hash
    ]
    assert differing == ["3.1"]

    for heading, key in [
        ("## 3.1 Again", "3.1"),
        ("## Task Mapping", "Task Mapping"),
        ("## V1 Another", "V1"),
        ("## M1 Another", "M1"),
    ]:
        duplicated = snapshot + f"\n{heading}\nrepeat\n".encode()
        with pytest.raises(ReviewEvidenceError, match=rf"duplicate manifest key: {re.escape(key)}"):
            build_section_manifest(duplicated)
    with pytest.raises(ReviewEvidenceError, match="duplicate manifest key: Ordinary"):
        build_section_manifest(snapshot + b"\n## Ordinary\none\n## Ordinary\ntwo\n")


def test_toctou_snapshot_isolation(
    review_setup: tuple[PlanReviewEvidenceService, str, str, Path],
) -> None:
    service, project_id, session_id, plan_path = review_setup
    original = plan_path.read_bytes()
    prepared = service.prepare_plan_review_round(
        project_id=project_id,
        plan_path=plan_path,
        round_number=1,
        session_id=session_id,
    )
    plan_path.write_bytes(original.replace(b"Behavior exists.", b"Behavior changed."))

    assert service.snapshot_bytes(prepared.evidence_id) == original
    assert service.snapshot_payload(prepared.evidence_id)["snapshot"] == original


def test_full_round_lifecycle_integration(
    review_setup: tuple[PlanReviewEvidenceService, str, str, Path],
) -> None:
    service, project_id, session_id, plan_path = review_setup
    original = plan_path.read_bytes()
    prepared = service.prepare_plan_review_round(
        project_id=project_id,
        plan_path=plan_path,
        round_number=1,
        session_id=session_id,
    )
    plan_path.write_bytes(original.replace(b"Pending.", b"Coordinator update."))
    assert service.verify_plan_unchanged(prepared.evidence_id, plan_path)

    plan_path.write_bytes(plan_path.read_bytes().replace(b"# Review Evidence", b"# Changed Title"))
    with pytest.raises(ReviewEvidenceError, match="reviewed plan sections changed"):
        service.verify_plan_unchanged(prepared.evidence_id, plan_path)

    plan_path.write_bytes(original)
    with pytest.raises(ReviewEvidenceError) as replayed_coverage:
        service.render_v1_round_checkpoint(
            prepared.evidence_id,
            {
                "verdict": "needs_review",
                "findings": [],
                "coverage_attestation": coverage_attestation(
                    evidence_id="another-evidence",
                    shadow_valid=False,
                ),
                "convergence_telemetry": enriched_telemetry(),
            },
        )
    assert replayed_coverage.value.code == "coverage_evidence_mismatch"
    rejection = {
        "verdict": "needs_review",
        "findings": [],
        "coverage_attestation": coverage_attestation(
            evidence_id=prepared.evidence_id,
            shadow_valid=False,
        ),
        "convergence_telemetry": enriched_telemetry(),
    }
    checkpoint = service.render_v1_round_checkpoint(prepared.evidence_id, rejection)
    ensure_checkpoint(plan_path, checkpoint)
    next_round = service.prepare_plan_review_round(
        project_id=project_id,
        plan_path=plan_path,
        round_number=2,
        session_id=session_id,
    )
    recovered = service.get_evidence(prepared.evidence_id)
    assert recovered.finalized_at is not None
    assert recovered.expired_at is None
    assert recovered.round_result == rejection
    assert next_round.evidence_id != prepared.evidence_id

    dead_path = plan_path.with_name("dead-attempt.md")
    dead_path.write_bytes(original)
    dead = service.prepare_plan_review_round(
        project_id=project_id,
        plan_path=dead_path,
        round_number=1,
        session_id=session_id,
    )
    manager = LocalAgentRunManager(service.db)
    dead_run = manager.create(
        parent_session_id=session_id,
        provider="codex",
        prompt="dead review",
    )
    service.bind_evidence_run(dead.evidence_id, dead_run.id)
    manager.cancel(dead_run.id)
    replacement = service.prepare_plan_review_round(
        project_id=project_id,
        plan_path=dead_path,
        round_number=2,
        session_id=session_id,
    )
    assert service.get_evidence(dead.evidence_id).expired_at is not None
    assert replacement.evidence_id != dead.evidence_id

    live_path = plan_path.with_name("live-attempt.md")
    live_path.write_bytes(original)
    live = service.prepare_plan_review_round(
        project_id=project_id,
        plan_path=live_path,
        round_number=1,
        session_id=session_id,
    )
    live_run = manager.create(
        parent_session_id=session_id,
        provider="codex",
        prompt="live review",
    )
    service.bind_evidence_run(live.evidence_id, live_run.id)
    with pytest.raises(ReviewEvidenceError) as still_live:
        service.prepare_plan_review_round(
            project_id=project_id,
            plan_path=live_path,
            round_number=2,
            session_id=session_id,
        )
    assert still_live.value.code == "review_round_active"
    assert service.get_evidence(live.evidence_id).expired_at is None


def test_schema_migration_baseline_parity(temp_db: HubDatabase) -> None:
    repo_root = Path(__file__).resolve().parents[2]
    migration = (
        repo_root / "src/gobby/storage/migrations/338_plan_review_evidence.sql"
    ).read_text()
    quality_ledger_migration = (
        repo_root / "src/gobby/storage/migrations/345_plan_review_quality_ledger.sql"
    ).read_text()

    def catalog() -> dict[str, list[tuple[object, ...]]]:
        columns = temp_db.execute(
            """
            SELECT column_name, data_type, udt_name, is_nullable, column_default
            FROM information_schema.columns
            WHERE table_schema = current_schema()
              AND table_name = 'plan_review_evidence'
            ORDER BY ordinal_position
            """
        ).fetchall()
        constraints = temp_db.execute(
            """
            SELECT constraint_name, constraint_type, is_deferrable, initially_deferred
            FROM information_schema.table_constraints
            WHERE table_schema = current_schema()
              AND table_name = 'plan_review_evidence'
            ORDER BY constraint_name
            """
        ).fetchall()
        indexes = temp_db.execute(
            """
            SELECT indexname, indexdef
            FROM pg_indexes
            WHERE schemaname = current_schema()
              AND tablename = 'plan_review_evidence'
            ORDER BY indexname
            """
        ).fetchall()
        return {
            "columns": [
                tuple(
                    row[key]
                    for key in (
                        "column_name",
                        "data_type",
                        "udt_name",
                        "is_nullable",
                        "column_default",
                    )
                )
                for row in columns
            ],
            "constraints": [
                tuple(
                    row[key]
                    for key in (
                        "constraint_name",
                        "constraint_type",
                        "is_deferrable",
                        "initially_deferred",
                    )
                )
                for row in constraints
            ],
            "indexes": [
                (row["indexname"], row["indexdef"].replace(" IF NOT EXISTS", "")) for row in indexes
            ],
        }

    baseline_catalog = catalog()
    temp_db.execute("DROP TABLE plan_review_evidence")
    for migration_sql in (migration, quality_ledger_migration):
        _execute_sql_script(temp_db, migration_sql)
    assert catalog() == baseline_catalog


def test_path_boundary_and_binding_validation(
    review_setup: tuple[PlanReviewEvidenceService, str, str, Path],
    tmp_path: Path,
) -> None:
    service, project_id, session_id, plan_path = review_setup
    outside = tmp_path.parent / "outside-review.md"
    outside.write_text("# Outside\n", encoding="utf-8")
    with pytest.raises(ReviewEvidenceError, match="escapes project root"):
        service.prepare_plan_review_round(
            project_id=project_id,
            plan_path=outside,
            round_number=1,
            session_id=session_id,
        )
    symlink = plan_path.with_name("linked.md")
    symlink.symlink_to(plan_path)
    with pytest.raises(ReviewEvidenceError, match="symlinked plan paths"):
        service.prepare_plan_review_round(
            project_id=project_id,
            plan_path=symlink,
            round_number=1,
            session_id=session_id,
        )

    task = LocalTaskManager(service.db).create_task(
        project_id,
        "Review stage evidence",
        validation_criteria="Stage review evidence round-trips through the bound task.",
    )
    prepared = service.prepare_plan_review_round(
        project_id=project_id,
        plan_path=plan_path,
        round_number=1,
        task_id=task.id,
        stage="review",
    )
    with pytest.raises(ReviewEvidenceError) as pending:
        service.authorize_current_attempt(
            prepared.evidence_id,
            project_id=project_id,
            plan_path=plan_path,
            round_number=1,
            task_id=task.id,
            stage="review",
        )
    assert pending.value.code == "binding_pending"
    assert pending.value.retryable

    run = LocalAgentRunManager(service.db).create(
        parent_session_id=session_id,
        provider="codex",
        prompt="review",
        task_id=task.id,
    )
    service.bind_evidence_run(prepared.evidence_id, run.id)
    current = service.authorize_current_attempt(
        prepared.evidence_id,
        project_id=project_id,
        plan_path=plan_path,
        round_number=1,
        task_id=task.id,
        stage="review",
        run_id=run.id,
    )
    assert current.evidence_id == prepared.evidence_id
    finalized = service.finalize_plan_review_evidence(
        prepared.evidence_id,
        {
            "verdict": "needs_review",
            "findings": [],
            "coverage_attestation": coverage_attestation(
                evidence_id=prepared.evidence_id,
                shadow_valid=False,
            ),
            "convergence_telemetry": enriched_telemetry(),
        },
    )
    replay = service.authorize_current_attempt(
        prepared.evidence_id,
        project_id=project_id,
        plan_path=plan_path,
        round_number=1,
        task_id=task.id,
        stage="review",
        run_id=run.id,
        allow_rejection_replay=True,
    )
    assert replay.round_result == finalized.round_result
    assert (
        service.resolve_historical_proof(
            prepared.evidence_id,
            project_id=project_id,
            plan_path=plan_path,
            task_id=task.id,
        ).evidence_id
        == prepared.evidence_id
    )


def test_manifest_rejects_invalid_or_noncanonical_payloads(
    manifest_review: ManifestReviewSetup,
) -> None:
    invalid_shadow = manifest_review.service.derive_plan_review_manifest(
        manifest_review.evidence_id,
        routing_decisions={"missing": {}},
    )
    assert invalid_shadow["status"] == "invalid"
    assert manifest_review.plan_path.read_bytes() == manifest_review.original_bytes
    assert manifest_review.service.get_evidence(manifest_review.evidence_id).manifest_state is None

    approval_entries = manifest_review.approval["manifest_entries"]
    assert isinstance(approval_entries, list)
    tampered_entries = [dict(entry) for entry in approval_entries if isinstance(entry, dict)]
    tampered_entries[0]["title"] = "Caller-controlled drift"
    tampered = {**manifest_review.approval, "manifest_entries": tampered_entries}
    with pytest.raises(ReviewEvidenceError) as noncanonical:
        manifest_review.service.apply_plan_review_manifest(
            manifest_review.evidence_id,
            tampered,
            plan_path=manifest_review.plan_path,
            run_id=manifest_review.run_id,
        )
    assert noncanonical.value.code == "noncanonical_manifest"
    assert manifest_review.plan_path.read_bytes() == manifest_review.original_bytes
    assert manifest_review.service.get_evidence(manifest_review.evidence_id).manifest_state is None


def test_manifest_prewrite_failure_rolls_back_intent(
    manifest_review: ManifestReviewSetup,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def crash_atomic_write(_path: Path, _content: bytes) -> None:
        raise OSError("simulated crash")

    monkeypatch.setattr(
        "gobby.plans.review_manifest_service.atomic_write_bytes",
        crash_atomic_write,
    )
    with pytest.raises(OSError, match="simulated crash"):
        manifest_review.service.apply_plan_review_manifest(
            manifest_review.evidence_id,
            manifest_review.approval,
            plan_path=manifest_review.plan_path,
            run_id=manifest_review.run_id,
        )
    rolled_back = manifest_review.service.get_evidence(manifest_review.evidence_id)
    assert rolled_back.manifest_state is None
    assert rolled_back.round_result is None
    assert manifest_review.plan_path.read_bytes() == manifest_review.original_bytes

    monkeypatch.setattr(
        "gobby.plans.review_manifest_service.atomic_write_bytes",
        atomic_write_bytes,
    )
    applied = manifest_review.service.apply_plan_review_manifest(
        manifest_review.evidence_id,
        manifest_review.approval,
        plan_path=manifest_review.plan_path,
        run_id=manifest_review.run_id,
    )
    assert applied["applied"] is True
    row = manifest_review.service.get_evidence(manifest_review.evidence_id)
    assert row.manifest_state == "applied"
    assert row.round_result == manifest_review.approval
    assert row.finalized_at is None


def test_manifest_reapplication_is_idempotent_and_payload_bound(
    manifest_review: ManifestReviewSetup,
) -> None:
    applied = manifest_review.service.apply_plan_review_manifest(
        manifest_review.evidence_id,
        manifest_review.approval,
        plan_path=manifest_review.plan_path,
        run_id=manifest_review.run_id,
    )
    first_bytes = manifest_review.plan_path.read_bytes()
    assert (
        manifest_review.service.apply_plan_review_manifest(
            manifest_review.evidence_id,
            manifest_review.approval,
            plan_path=manifest_review.plan_path,
            run_id=manifest_review.run_id,
        )
        == applied
    )
    assert manifest_review.plan_path.read_bytes() == first_bytes

    changed = {
        **manifest_review.approval,
        "findings": [{"message": "different"}],
    }
    with pytest.raises(ReviewEvidenceError) as invalid_finding:
        manifest_review.service.apply_plan_review_manifest(
            manifest_review.evidence_id,
            changed,
            plan_path=manifest_review.plan_path,
            run_id=manifest_review.run_id,
        )
    assert invalid_finding.value.code == "invalid_review_findings"


def test_manifest_postwrite_checkpoint_failure_recovers(
    manifest_review: ManifestReviewSetup,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    complete_manifest_apply = manifest_review.service.store.complete_manifest_apply

    def crash_before_checkpoint(
        *,
        transaction: Transaction,
        evidence_id: str,
        result: Mapping[str, object],
    ) -> Never:
        _ = transaction, evidence_id, result
        raise RuntimeError("simulated checkpoint crash")

    monkeypatch.setattr(
        manifest_review.service.store,
        "complete_manifest_apply",
        crash_before_checkpoint,
    )
    with pytest.raises(RuntimeError, match="simulated checkpoint crash"):
        manifest_review.service.apply_plan_review_manifest(
            manifest_review.evidence_id,
            manifest_review.approval,
            plan_path=manifest_review.plan_path,
            run_id=manifest_review.run_id,
        )
    landed_bytes = manifest_review.plan_path.read_bytes()
    assert landed_bytes != manifest_review.original_bytes
    rolled_back = manifest_review.service.get_evidence(manifest_review.evidence_id)
    assert rolled_back.manifest_state is None
    assert rolled_back.round_result is None
    monkeypatch.setattr(
        manifest_review.service.store,
        "complete_manifest_apply",
        complete_manifest_apply,
    )
    manifest_review.service.apply_plan_review_manifest(
        manifest_review.evidence_id,
        manifest_review.approval,
        plan_path=manifest_review.plan_path,
        run_id=manifest_review.run_id,
    )
    assert manifest_review.plan_path.read_bytes() == landed_bytes
    assert (
        manifest_review.service.get_evidence(manifest_review.evidence_id).manifest_state
        == "applied"
    )


def test_pending_manifest_drift_revokes_intent(
    manifest_review: ManifestReviewSetup,
) -> None:
    evidence = manifest_review.service.get_evidence(manifest_review.evidence_id)
    mutation = PlanReviewEvidenceMutation(
        project_id=manifest_review.project_id,
        plan_path=evidence.plan_path,
    )
    with manifest_review.service.db.transaction_immediate(mutation) as transaction:
        manifest_review.service.store.begin_manifest_apply(
            transaction=transaction,
            evidence_id=manifest_review.evidence_id,
            digest=canonical_json_sha256(manifest_review.approval),
            payload=manifest_review.approval,
        )

    manifest_review.plan_path.write_bytes(
        manifest_review.plan_path.read_bytes().replace(
            b"Behavior exists.",
            b"Behavior drifted.",
        )
    )
    drifted_bytes = manifest_review.plan_path.read_bytes()
    with pytest.raises(ReviewEvidenceError, match="reviewed plan sections changed"):
        manifest_review.service.apply_plan_review_manifest(
            manifest_review.evidence_id,
            manifest_review.approval,
            plan_path=manifest_review.plan_path,
            run_id=manifest_review.run_id,
        )
    revoked = manifest_review.service.get_evidence(manifest_review.evidence_id)
    assert revoked.manifest_state == "revoked"
    assert revoked.round_result is None
    assert revoked.manifest_payload == manifest_review.approval
    assert manifest_review.plan_path.read_bytes() == drifted_bytes
    with pytest.raises(ReviewEvidenceError, match="manifest intent was revoked"):
        manifest_review.service.apply_plan_review_manifest(
            manifest_review.evidence_id,
            manifest_review.approval,
            plan_path=manifest_review.plan_path,
            run_id=manifest_review.run_id,
        )
    LocalAgentRunManager(manifest_review.service.db).cancel(manifest_review.run_id)
    rereview = manifest_review.service.prepare_plan_review_round(
        project_id=manifest_review.project_id,
        plan_path=manifest_review.plan_path,
        round_number=2,
        session_id=manifest_review.session_id,
    )
    assert rereview.evidence_id != manifest_review.evidence_id
    assert manifest_review.service.get_evidence(manifest_review.evidence_id).expired_at is not None


def test_two_phase_run_binding(
    review_setup: tuple[PlanReviewEvidenceService, str, str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, project_id, session_id, plan_path = review_setup
    prepared = service.prepare_plan_review_round(
        project_id=project_id,
        plan_path=plan_path,
        round_number=1,
        session_id=session_id,
    )
    manager = LocalAgentRunManager(service.db)
    run = manager.create(parent_session_id=session_id, provider="codex", prompt="review")
    bound = service.bind_evidence_run(prepared.evidence_id, run.id)
    assert bound.dispatch_run_id == run.id
    assert bound.lease_expires_at is None

    def fail_preparation(*_args: object, **_kwargs: object) -> Never:
        raise AssertionError("bound evidence must refuse before preparation work")

    with monkeypatch.context() as scoped:
        scoped.setattr(
            "gobby.plans.review_evidence.prepare_review_round_context",
            fail_preparation,
        )
        scoped.setattr(service, "_assemble_requirements_bundle", fail_preparation)
        scoped.setattr(service.store, "write_preparation_context", fail_preparation)
        with pytest.raises(ReviewEvidenceError) as bound_attempt:
            service.prepare_plan_review_round(
                project_id=project_id,
                plan_path=plan_path,
                round_number=1,
                session_id=session_id,
            )
    assert bound_attempt.value.code == "review_round_bound"
    assert bound_attempt.value.retryable is True
    assert bound_attempt.value.details == {
        "evidence_id": prepared.evidence_id,
        "run_id": run.id,
    }

    assert service.bind_evidence_run(prepared.evidence_id, run.id).dispatch_run_id == run.id

    other = manager.create(parent_session_id=session_id, provider="codex", prompt="other")
    with pytest.raises(ReviewEvidenceError, match="already bound"):
        service.bind_evidence_run(prepared.evidence_id, other.id)
    manager.cancel(run.id)
    expired = service.expire_plan_review_evidence(prepared.evidence_id)
    assert expired.expired_at is not None

    spawn_failed = service.prepare_plan_review_round(
        project_id=project_id,
        plan_path=plan_path,
        round_number=2,
        session_id=session_id,
    )
    assert (
        service.expire_plan_review_evidence(
            spawn_failed.evidence_id,
            spawn_failed=True,
        ).expired_at
        is not None
    )

    bind_failed = service.prepare_plan_review_round(
        project_id=project_id,
        plan_path=plan_path,
        round_number=3,
        session_id=session_id,
    )
    other_session = SessionManager(service.db).register(
        external_id="other-review-parent",
        machine_id="test-machine",
        source="codex",
        project_id=project_id,
    )
    wrong_run = manager.create(
        parent_session_id=other_session.id,
        provider="codex",
        prompt="wrong lineage",
    )
    with pytest.raises(ReviewEvidenceError, match="does not belong"):
        service.bind_evidence_run(bind_failed.evidence_id, wrong_run.id)
    assert service.get_evidence(bind_failed.evidence_id).expired_at is not None
    cancelled = manager.get(wrong_run.id)
    assert cancelled is not None
    assert cancelled.status == "cancelled"


def test_interactive_mint_status_lifecycle(
    review_setup: tuple[PlanReviewEvidenceService, str, str, Path],
) -> None:
    service, project_id, session_id, plan_path = review_setup
    prepared = service.prepare_plan_review_round(
        project_id=project_id,
        plan_path=plan_path,
        round_number=1,
        session_id=session_id,
    )
    derived = service.derive_plan_review_manifest(
        prepared.evidence_id,
        routing_decisions={},
    )
    manifest_entries = derived["manifest_entries"]
    assert isinstance(manifest_entries, list)
    approval = {
        "verdict": "approved",
        "findings": [],
        "routing_decisions": {},
        "manifest_entries": manifest_entries,
        "coverage_attestation": coverage_attestation(
            evidence_id=prepared.evidence_id,
            manifest_entries=manifest_entries,
        ),
        "convergence_telemetry": enriched_telemetry(),
    }
    with pytest.raises(ReviewEvidenceError, match="V1 checkpoint"):
        service.finalize_plan_review_evidence(prepared.evidence_id, approval)
    run = LocalAgentRunManager(service.db).create(
        parent_session_id=session_id,
        provider="codex",
        prompt="approve",
    )
    service.bind_evidence_run(prepared.evidence_id, run.id)
    service.apply_plan_review_manifest(
        prepared.evidence_id,
        approval,
        plan_path=plan_path,
        run_id=run.id,
    )
    checkpoint = service.render_v1_round_checkpoint(prepared.evidence_id)

    with pytest.raises(ReviewEvidenceError) as pending:
        service.prepare_plan_review_round(
            project_id=project_id,
            plan_path=plan_path,
            round_number=2,
            session_id=session_id,
        )
    assert pending.value.code == "pending_lesson_mint"
    pending_rows = pending.value.details["pending"]
    assert isinstance(pending_rows, list)
    assert pending_rows
    pending_row = pending_rows[0]
    assert isinstance(pending_row, dict)
    assert pending_row["round_result"] == approval
    finalized = service.get_evidence(prepared.evidence_id)
    assert finalized.lesson_mint_status == "pending"
    assert checkpoint in plan_path.read_bytes()

    minted = service.checkpoint_plan_review_lesson_mint(
        prepared.evidence_id,
        status="minted",
        detail={"lesson_ids": ["lesson-1"]},
    )
    assert minted.lesson_mint_status == "minted"
    assert (
        service.checkpoint_plan_review_lesson_mint(
            prepared.evidence_id,
            status="minted",
            detail={"lesson_ids": ["lesson-1"]},
        ).lesson_mint_status
        == "minted"
    )
    assert service.prepare_plan_review_round(
        project_id=project_id,
        plan_path=plan_path,
        round_number=2,
        session_id=session_id,
    )


def test_service_integration_call_sites(
    review_setup: tuple[PlanReviewEvidenceService, str, str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, _, _, _ = review_setup
    manifest_result: dict[str, object] = {"status": "valid"}
    checkpoint = b"checkpoint"
    calls: list[tuple[str, object]] = []

    def derive_manifest(
        evidence_id: str,
        routing_decisions: Mapping[str, object],
    ) -> dict[str, object]:
        calls.append(("manifest", (evidence_id, dict(routing_decisions))))
        return manifest_result

    def render_round_checkpoint(
        evidence_id: str,
        round_result: Mapping[str, object] | None = None,
    ) -> bytes:
        calls.append(("checkpoint", (evidence_id, round_result)))
        return checkpoint

    monkeypatch.setattr(
        service.manifests,
        "derive_plan_review_manifest",
        derive_manifest,
    )
    monkeypatch.setattr(
        service.checkpoints,
        "render_v1_round_checkpoint",
        render_round_checkpoint,
    )

    assert service.derive_plan_review_manifest("evidence-1", {"lane": "task"}) is manifest_result
    assert (
        service.render_v1_round_checkpoint("evidence-1", {"verdict": "needs_review"}) == checkpoint
    )
    assert calls == [
        ("manifest", ("evidence-1", {"lane": "task"})),
        ("checkpoint", ("evidence-1", {"verdict": "needs_review"})),
    ]


def test_upstream_leaves_close_independently() -> None:
    plan_path = (
        Path(__file__).parents[2] / ".gobby/plans/completed/adversary-convergence-improvements.md"
    )
    plan = plan_path.read_text(encoding="utf-8")
    upstream_ids = ("2.2", "2.4", "4.1", "4.3", "5.1", "5.2", "6.5", "7.2")
    acceptance_blocks: list[str] = []
    for section_id in upstream_ids:
        marker = f"### {section_id} "
        start = plan.index(marker)
        next_section = plan.find("\n### ", start + len(marker))
        section = plan[start : next_section if next_section >= 0 else len(plan)]
        acceptance_blocks.append(section.partition("**Acceptance:**")[2])

    combined = "\n".join(acceptance_blocks)
    assert all("test:" in block for block in acceptance_blocks)
    assert "preparation persists" not in combined.lower()
    assert "snapshot assembly" not in combined.lower()
    assert "finalization persists" not in combined.lower()
    assert "evidence expiry" not in combined.lower()
    assert all(
        term in combined.lower()
        for term in ("validation", "refus", "merge", "deriv", "classif", "pars")
    )


def test_ledger_round_trip_through_finalize(
    review_setup: tuple[PlanReviewEvidenceService, str, str, Path],
) -> None:
    service, project_id, session_id, plan_path = review_setup
    prepared = service.prepare_plan_review_round(
        project_id=project_id,
        plan_path=plan_path,
        round_number=1,
        session_id=session_id,
    )
    finding = {
        "finding_id": "ledger-finding",
        "section_id": "1.1",
        "check_key": "consumer-parity",
        "severity": "major",
        "category": "unhandled-edge",
        "location": "src/example.py:1",
        "description": "A consumer misses the new field.",
        "minimal_repair": "Read the new field.",
        "repair_scope": "existing_sections",
        "principle": "Review every consumer of a changed contract.",
        "prevention": "Audit every consumer.",
    }
    result = {
        "verdict": "needs_review",
        "findings": [finding],
        "coverage_attestation": coverage_attestation(
            evidence_id=prepared.evidence_id,
            manifest_entries=[{"source_section": "1.1"}],
        ),
        "convergence_telemetry": enriched_telemetry(),
    }
    ensure_checkpoint(
        plan_path,
        service.render_v1_round_checkpoint(prepared.evidence_id, result),
    )

    finalized = service.finalize_plan_review_evidence(prepared.evidence_id, result)

    assert finalized.quality_ledger is not None
    assert len(finalized.quality_ledger) == 1
    assert finalized.quality_ledger[0]["aliases"] == ["ledger-finding"]
    assert (
        service.finalize_plan_review_evidence(
            prepared.evidence_id,
            result,
        ).quality_ledger
        == finalized.quality_ledger
    )

    round_two = service.prepare_plan_review_round(
        project_id=project_id,
        plan_path=plan_path,
        round_number=2,
        session_id=session_id,
        prior_finding_resolutions=[{"prior_finding_id": "ledger-finding", "decision": "carry"}],
    )
    round_two_result = {
        "verdict": "needs_review",
        "findings": [],
        "coverage_attestation": coverage_attestation(
            evidence_id=round_two.evidence_id,
            manifest_entries=[{"source_section": "1.1"}],
        ),
        "convergence_telemetry": enriched_telemetry(),
    }
    ensure_checkpoint(
        plan_path,
        service.render_v1_round_checkpoint(round_two.evidence_id, round_two_result),
    )
    carried = service.finalize_plan_review_evidence(
        round_two.evidence_id,
        round_two_result,
    )
    assert carried.quality_ledger is not None
    assert (
        carried.quality_ledger[0]["ledger_entry_id"]
        == finalized.quality_ledger[0]["ledger_entry_id"]
    )
    assert carried.quality_ledger[0]["rounds_carried"] == 2


def test_inventory_unavailable_aborts_preparation(
    review_setup: tuple[PlanReviewEvidenceService, str, str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, project_id, session_id, plan_path = review_setup
    prepared = service.prepare_plan_review_round(
        project_id=project_id,
        plan_path=plan_path,
        round_number=1,
        session_id=session_id,
    )
    result = {
        "verdict": "needs_review",
        "findings": [],
        "coverage_attestation": coverage_attestation(
            evidence_id=prepared.evidence_id,
            manifest_entries=[{"source_section": "1.1"}],
        ),
        "convergence_telemetry": enriched_telemetry(),
    }
    ensure_checkpoint(
        plan_path,
        service.render_v1_round_checkpoint(prepared.evidence_id, result),
    )
    service.finalize_plan_review_evidence(prepared.evidence_id, result)

    def unavailable(*args: object, **kwargs: object) -> Never:
        raise ReviewEvidenceError(
            "inventory_unavailable",
            "code index is unavailable",
        )

    monkeypatch.setattr(
        "gobby.plans.review_evidence.prepare_review_round_context",
        unavailable,
    )

    with pytest.raises(ReviewEvidenceError, match="code index is unavailable"):
        service.prepare_plan_review_round(
            project_id=project_id,
            plan_path=plan_path,
            round_number=2,
            session_id=session_id,
        )

    rows = service.store.list_for_path(
        project_id=project_id,
        plan_path=".gobby/plans/review-evidence.md",
    )
    assert [row.round_number for row in rows] == [1]


def _integration_finding() -> dict[str, object]:
    return {
        "finding_id": "integration-finding",
        "section_id": "1.1",
        "check_key": "consumer-parity",
        "severity": "major",
        "category": "unhandled-edge",
        "location": "src/example.py:1",
        "description": "A consumer misses the changed contract.",
        "minimal_repair": "Update every consumer.",
        "repair_scope": "existing_sections",
        "principle": "Review every consumer of a changed contract.",
        "prevention": "Audit every consumer.",
    }


def _finalize_integration_round(
    service: PlanReviewEvidenceService,
    plan_path: Path,
    evidence_id: str,
    *,
    findings: list[dict[str, object]],
) -> None:
    result = {
        "verdict": "needs_review",
        "findings": findings,
        "coverage_attestation": coverage_attestation(
            evidence_id=evidence_id,
            manifest_entries=[{"source_section": "1.1"}],
        ),
        "convergence_telemetry": enriched_telemetry(),
    }
    ensure_checkpoint(
        plan_path,
        service.render_v1_round_checkpoint(evidence_id, result),
    )
    service.finalize_plan_review_evidence(evidence_id, result)


def _integration_inventory_and_universe() -> tuple[
    CandidateSiteInventory,
    SweepScope,
]:
    site = CandidateSite(
        site_id="site-consumer",
        path="src/consumer.py",
        source_kind="file_consumer",
        source_ref="src/example.py",
        status="resolved",
        language="python",
        section_ids=("1.1",),
    )
    inventory = CandidateSiteInventory(
        changed_acceptance_item_ids=("1.1.1",),
        changed_targets=("src/example.py",),
        changed_symbols=(),
        changed_contracts=("example-contract",),
        targets_by_section={"1.1": ("src/example.py",)},
        contracts_by_section={"1.1": ("example-contract",)},
        resolved_languages=("python",),
        unsupported_targets=(),
        sites=(site,),
    )
    universe = derive_sweep_scope(
        prior_findings=[_integration_finding()],
        inventory=inventory,
        repair_finding_ids=["integration-finding"],
    )
    return inventory, universe


def _integration_attestation(universe: SweepScope) -> dict[str, object]:
    requirement = universe.requirements[0]
    return {
        "prior_finding_id": "integration-finding",
        "check_key": "consumer-parity",
        "changed_section_ids": ["1.1"],
        "accepted_resolution": "Update every consumer.",
        "deviation_from_minimal_repair": None,
        "changed_symbols": [],
        "consumer_sites_swept": list(requirement.required_consumer_site_ids),
        "adjacent_variants_swept": list(requirement.adjacent_variant_ids),
        "validation_evidence": ["focused integration test passed"],
        "deferred_sites": [],
        "sweep_scope_digest": universe.digest,
        "sweep_query_evidence": ["gcode callers example-contract"],
        "repair_bundle_interactions": [],
    }


def _prepare_integration_repair_round(
    review_setup: tuple[PlanReviewEvidenceService, str, str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[
    PlanReviewEvidenceService,
    str,
    Path,
    CandidateSiteInventory,
    SweepScope,
]:
    service, project_id, session_id, plan_path = review_setup
    round_one = service.prepare_plan_review_round(
        project_id=project_id,
        plan_path=plan_path,
        round_number=1,
        session_id=session_id,
    )
    _finalize_integration_round(
        service,
        plan_path,
        round_one.evidence_id,
        findings=[_integration_finding()],
    )
    plan_path.write_text(
        plan_path.read_text(encoding="utf-8").replace(
            "Behavior exists.",
            "Updated behavior exists.",
        ),
        encoding="utf-8",
    )
    inventory, universe = _integration_inventory_and_universe()
    monkeypatch.setattr(
        "gobby.plans.review_evidence_preparation.derive_settled_sweep_inputs",
        lambda **_kwargs: (inventory, universe),
    )
    round_two = service.prepare_plan_review_round(
        project_id=project_id,
        plan_path=plan_path,
        round_number=2,
        session_id=session_id,
        prior_finding_resolutions=[
            {"prior_finding_id": "integration-finding", "decision": "repair"}
        ],
        repair_attestations=[_integration_attestation(universe)],
        sweep_scope=universe.to_dict(),
        sweep_scope_digest=universe.digest,
    )
    return service, round_two.evidence_id, plan_path, inventory, universe


def test_inventory_first_call_succeeds(
    review_setup: tuple[PlanReviewEvidenceService, str, str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, evidence_id, _plan_path, inventory, universe = _prepare_integration_repair_round(
        review_setup, monkeypatch
    )

    context = service.get_evidence(evidence_id).prior_round_context
    assert context is not None
    assert context["consumer_site_inventory"] == inventory.to_dict()
    assert context["submitted_sweep_scope_digest"] == universe.digest
    assert context["current_sweep_scope"] == universe.to_dict()
    assert context["required_scope_delta"] == {
        "requirements": {"added": [], "removed": [], "changed": []},
        "candidate_sites": {"added": [], "removed": [], "changed": []},
        "interaction_edges": {"added": [], "removed": [], "changed": []},
    }


def test_round_context_records_no_index_generation(
    review_setup: tuple[PlanReviewEvidenceService, str, str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Repository churn during a round is untracked by design.

    Pinning an index generation made any concurrent commit terminate the round,
    so nothing records one. A change that actually moves the plan surface is a
    finding for the reviewer to report.
    """
    service, evidence_id, _plan_path, _inventory, _universe = _prepare_integration_repair_round(
        review_setup, monkeypatch
    )

    restarted = PlanReviewEvidenceService(service.db)
    context = restarted.get_evidence(evidence_id).prior_round_context

    assert context is not None
    assert "index_token" not in context


def test_prior_round_context_atomic_and_source_independent(
    review_setup: tuple[PlanReviewEvidenceService, str, str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, project_id, session_id, plan_path = review_setup
    original_write = service.store.write_preparation_context

    def fail_write(*args: object, **kwargs: object) -> Never:
        raise RuntimeError("preparation context write failed")

    with monkeypatch.context() as scoped:
        scoped.setattr(service.store, "write_preparation_context", fail_write)
        with pytest.raises(RuntimeError, match="preparation context write failed"):
            service.prepare_plan_review_round(
                project_id=project_id,
                plan_path=plan_path,
                round_number=1,
                session_id=session_id,
            )
    assert service.store.write_preparation_context == original_write
    assert (
        service.store.list_for_path(
            project_id=project_id,
            plan_path=".gobby/plans/review-evidence.md",
        )
        == []
    )

    prepared = service.prepare_plan_review_round(
        project_id=project_id,
        plan_path=plan_path,
        round_number=1,
        session_id=session_id,
    )
    persisted = service.get_evidence(prepared.evidence_id).prior_round_context
    plan_path.write_text("live source changed after preparation", encoding="utf-8")

    assert (
        PlanReviewEvidenceService(service.db).get_evidence(prepared.evidence_id).prior_round_context
        == persisted
    )


def test_requirements_bundle_persisted_and_sufficient(
    review_setup: tuple[PlanReviewEvidenceService, str, str, Path],
) -> None:
    service, project_id, session_id, plan_path = review_setup
    prepared = service.prepare_plan_review_round(
        project_id=project_id,
        plan_path=plan_path,
        round_number=1,
        session_id=session_id,
    )
    context = service.get_evidence(prepared.evidence_id).prior_round_context
    bundle = requirements_bundle_from_context(context)
    assert bundle is not None
    sources = bundle["sources"]
    assert isinstance(sources, list)
    source = sources[0]
    assert isinstance(source, dict)
    citation = {
        "requirement_id": source["requirement_id"],
        "content_sha256": source["content_sha256"],
    }
    plan_path.unlink()

    restarted_context = (
        PlanReviewEvidenceService(service.db).get_evidence(prepared.evidence_id).prior_round_context
    )
    restarted_bundle = requirements_bundle_from_context(restarted_context)
    assert restarted_bundle == bundle
    assert (
        validate_source_citation(
            citation,
            requirements_bundle=restarted_bundle,
        )
        == citation
    )


def test_finalize_validates_findings_and_blocks_approval(
    review_setup: tuple[PlanReviewEvidenceService, str, str, Path],
) -> None:
    service, project_id, session_id, _plan_path = review_setup
    prepared = service.prepare_plan_review_round(
        project_id=project_id,
        plan_path=".gobby/plans/review-evidence.md",
        round_number=1,
        session_id=session_id,
    )
    invalid = {
        "verdict": "needs_review",
        "findings": [{"finding_id": "missing-fields"}],
        "coverage_attestation": coverage_attestation(
            evidence_id=prepared.evidence_id,
            manifest_entries=[{"source_section": "1.1"}],
        ),
        "convergence_telemetry": enriched_telemetry(),
    }
    with pytest.raises(ReviewEvidenceError, match="section_id must be a non-empty string"):
        service.finalize_plan_review_evidence(prepared.evidence_id, invalid)

    evidence = service.get_evidence(prepared.evidence_id)
    bundle = requirements_bundle_from_context(evidence.prior_round_context)
    assert bundle is not None
    sources = bundle["sources"]
    assert isinstance(sources, list)
    source = sources[0]
    assert isinstance(source, dict)
    blocking = {
        **_integration_finding(),
        "severity": "blocking",
        "failure_trace": {
            "preconditions": "The changed contract has a direct consumer.",
            "action": "The consumer reads the old shape.",
            "wrong_outcome": "The consumer rejects the new value.",
            "violated_obligation": "Every consumer must accept the changed contract.",
            "citation": [
                {
                    "requirement_id": source["requirement_id"],
                    "content_sha256": source["content_sha256"],
                }
            ],
        },
    }
    derived = service.derive_plan_review_manifest(
        prepared.evidence_id,
        routing_decisions={},
    )
    manifest_entries = derived["manifest_entries"]
    assert isinstance(manifest_entries, list)
    approved = {
        "verdict": "approved",
        "findings": [blocking],
        "routing_decisions": {},
        "manifest_entries": manifest_entries,
        "coverage_attestation": coverage_attestation(
            evidence_id=prepared.evidence_id,
            manifest_entries=manifest_entries,
        ),
        "convergence_telemetry": enriched_telemetry(),
    }
    with pytest.raises(ReviewEvidenceError) as blocked:
        service.finalize_plan_review_evidence(prepared.evidence_id, approved)
    assert blocked.value.code == "blocking_findings_remaining"


def test_telemetry_persisted_at_finalize(
    review_setup: tuple[PlanReviewEvidenceService, str, str, Path],
) -> None:
    service, project_id, session_id, plan_path = review_setup
    prepared = service.prepare_plan_review_round(
        project_id=project_id,
        plan_path=plan_path,
        round_number=1,
        session_id=session_id,
    )
    telemetry = enriched_telemetry()
    result = {
        "verdict": "needs_review",
        "findings": [],
        "coverage_attestation": coverage_attestation(
            evidence_id=prepared.evidence_id,
            manifest_entries=[{"source_section": "1.1"}],
        ),
        "convergence_telemetry": telemetry,
    }
    ensure_checkpoint(
        plan_path,
        service.render_v1_round_checkpoint(prepared.evidence_id, result),
    )
    service.finalize_plan_review_evidence(prepared.evidence_id, result)

    persisted = (
        PlanReviewEvidenceService(service.db).get_evidence(prepared.evidence_id).round_result
    )
    assert persisted is not None
    assert persisted["convergence_telemetry"] == telemetry
    reviewer = telemetry["reviewer"]
    assert isinstance(reviewer, dict)
    repeated = reviewer["repeated_check_keys"]
    assert isinstance(repeated, dict)
    assert repeated["count"] == 1


def test_snapshot_carries_prior_round_context(
    review_setup: tuple[PlanReviewEvidenceService, str, str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, evidence_id, _plan_path, _inventory, _universe = _prepare_integration_repair_round(
        review_setup, monkeypatch
    )
    expected = service.get_evidence(evidence_id).prior_round_context

    assert expected is not None
    assert (
        PlanReviewEvidenceService(service.db).snapshot_payload(evidence_id)["prior_round_context"]
        == expected
    )


def test_approval_surfaces_carried_ledger(
    review_setup: tuple[PlanReviewEvidenceService, str, str, Path],
) -> None:
    service, project_id, session_id, plan_path = review_setup
    prepared = service.prepare_plan_review_round(
        project_id=project_id,
        plan_path=plan_path,
        round_number=1,
        session_id=session_id,
    )
    derived_manifest = service.derive_plan_review_manifest(
        prepared.evidence_id,
        routing_decisions={},
    )
    manifest_entries = derived_manifest["manifest_entries"]
    assert isinstance(manifest_entries, list)
    major = _integration_finding()
    minor = {
        **_integration_finding(),
        "finding_id": "integration-minor",
        "check_key": "adjacent-variant",
        "severity": "minor",
        "description": "An adjacent variant needs the same update.",
    }
    coverage = coverage_attestation(
        evidence_id=prepared.evidence_id,
        manifest_entries=manifest_entries,
    )
    section_hash = next(
        section.section_hash for section in prepared.sections if section.section_id == "1.1"
    )
    coverage["disposition_counts"] = {
        "total": 1,
        "emitted_findings": 0,
        "dismissed": 1,
    }
    lanes = coverage["lanes"]
    assert isinstance(lanes, list)
    first_lane = lanes[0]
    assert isinstance(first_lane, dict)
    first_lane["candidate_count"] = 1
    record_bundle = coverage["record_bundle"]
    assert isinstance(record_bundle, dict)
    record_bundle["candidate_dispositions"] = [
        {
            "candidate_id": "dismissed-integration-candidate",
            "check_key": "dismissed-adjacent-variant",
            "source_section_ids": ["1.1"],
            "source_hash": section_hash,
            "disposition": "dismissed",
            "rationale": "The adjacent variant already consumes the new shape.",
        }
    ]
    record_bundle["adjacent_variant_sweeps"] = [
        {
            "check_key": "dismissed-adjacent-variant",
            "seed_candidate_id": "dismissed-integration-candidate",
            "query_evidence": ["gcode search dismissed-integration-candidate"],
            "sites_checked": ["src/example.py"],
            "resulting_candidate_ids": [],
        }
    ]
    coverage_without_digest = {
        key: value for key, value in coverage.items() if key != "attestation_digest"
    }
    coverage["attestation_digest"] = hashlib.sha256(
        json.dumps(
            coverage_without_digest,
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()
    approval = {
        "verdict": "approved",
        "findings": [major, minor],
        "routing_decisions": {},
        "manifest_entries": manifest_entries,
        "coverage_attestation": coverage,
        "convergence_telemetry": enriched_telemetry(),
    }
    with service.db.transaction() as transaction:
        derived_ledger = service.derive_quality_ledger_for_evidence(
            prepared.evidence_id,
            approval,
            transaction=transaction,
        )
    checkpoint = service.render_v1_round_checkpoint(
        prepared.evidence_id,
        approval,
    )
    assert b'"manifest_entries"' in checkpoint
    ensure_checkpoint(plan_path, checkpoint)

    finalized = service.finalize_plan_review_evidence(
        prepared.evidence_id,
        approval,
        _derived_quality_ledger=derived_ledger,
    )

    assert finalized.approval_result is not None
    assert finalized.approval_result["manifest_entries"] == manifest_entries
    assert finalized.approval_result["quality_ledger"] == derived_ledger
    assert finalized.quality_ledger == derived_ledger
    finding_entries = [entry for entry in derived_ledger if entry["kind"] == "finding"]
    assert {entry["severity"] for entry in finding_entries} == {"major", "minor"}
    assert any(entry["kind"] == "dismissed" for entry in derived_ledger)


def test_sweep_scope_production_sequence(
    review_setup: tuple[PlanReviewEvidenceService, str, str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, project_id, _session_id, plan_path = review_setup
    staged_path = plan_path.with_name("staged-repair.md")
    staged_path.write_bytes(plan_path.read_bytes())
    task = LocalTaskManager(service.db).create_task(
        project_id,
        "Repair staged review findings",
        validation_criteria="The staged repair universe is rederived before dispatch.",
    )
    round_one = service.prepare_plan_review_round(
        project_id=project_id,
        plan_path=staged_path,
        round_number=1,
        task_id=task.id,
        stage="review",
    )
    round_one_result = {
        "verdict": "needs_review",
        "findings": [_integration_finding()],
        "coverage_attestation": coverage_attestation(
            evidence_id=round_one.evidence_id,
            manifest_entries=[{"source_section": "1.1"}],
        ),
        "convergence_telemetry": enriched_telemetry(),
    }
    service.finalize_plan_review_evidence(round_one.evidence_id, round_one_result)
    staged_path.write_text(
        staged_path.read_text(encoding="utf-8").replace(
            "Behavior exists.",
            "Updated staged behavior exists.",
        ),
        encoding="utf-8",
    )
    inventory, universe = _integration_inventory_and_universe()
    current_site = CandidateSite(
        site_id="site-consumer",
        path="src/current-consumer.py",
        source_kind="file_consumer",
        source_ref="src/example.py",
        status="resolved",
        language="python",
        section_ids=("1.1",),
    )
    current_inventory = CandidateSiteInventory(
        changed_acceptance_item_ids=inventory.changed_acceptance_item_ids,
        changed_targets=inventory.changed_targets,
        changed_symbols=inventory.changed_symbols,
        changed_contracts=inventory.changed_contracts,
        targets_by_section=inventory.targets_by_section,
        contracts_by_section=inventory.contracts_by_section,
        resolved_languages=inventory.resolved_languages,
        unsupported_targets=inventory.unsupported_targets,
        sites=(current_site,),
    )
    current_scope = derive_sweep_scope(
        prior_findings=[_integration_finding()],
        inventory=current_inventory,
        repair_finding_ids=["integration-finding"],
    )
    monkeypatch.setattr(
        "gobby.plans.review_evidence_preparation.derive_settled_sweep_inputs",
        lambda **_kwargs: (current_inventory, current_scope),
    )

    with pytest.raises(ReviewEvidenceError) as inconsistent:
        service.prepare_plan_review_round(
            project_id=project_id,
            plan_path=staged_path,
            round_number=2,
            task_id=task.id,
            stage="review",
            prior_finding_resolutions=[
                {"prior_finding_id": "integration-finding", "decision": "repair"}
            ],
            repair_attestations=[_integration_attestation(universe)],
            sweep_scope=universe.to_dict(),
            sweep_scope_digest="d" * 64,
        )
    assert inconsistent.value.code == "sweep_scope_digest_mismatch"

    staged_round_two = service.prepare_plan_review_round(
        project_id=project_id,
        plan_path=staged_path,
        round_number=2,
        task_id=task.id,
        stage="review",
        prior_finding_resolutions=[
            {"prior_finding_id": "integration-finding", "decision": "repair"}
        ],
        repair_attestations=[_integration_attestation(universe)],
        sweep_scope=universe.to_dict(),
        sweep_scope_digest=universe.digest,
    )
    assert [
        row.round_number
        for row in service.store.list_for_path(
            project_id=project_id,
            plan_path=".gobby/plans/staged-repair.md",
        )
    ] == [1, 2]
    staged_context = service.get_evidence(staged_round_two.evidence_id).prior_round_context
    assert staged_context is not None
    assert staged_context["submitted_sweep_scope_digest"] == universe.digest
    site_delta = staged_context["required_scope_delta"]
    assert isinstance(site_delta, dict)
    assert site_delta["candidate_sites"] == {
        "added": [],
        "removed": [],
        "changed": [
            {
                "id": "site-consumer",
                "submitted": universe.candidate_sites[0].to_dict(),
                "current": current_site.to_dict(),
            }
        ],
    }

    taskless_service, taskless_evidence_id, *_rest = _prepare_integration_repair_round(
        review_setup,
        monkeypatch,
    )
    taskless_context = taskless_service.get_evidence(taskless_evidence_id).prior_round_context
    assert taskless_context is not None
    assert taskless_context["consumer_site_inventory"] == inventory.to_dict()


def test_production_paths_end_to_end(
    review_setup: tuple[PlanReviewEvidenceService, str, str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, project_id, _session_id, plan_path = review_setup
    original = plan_path.read_bytes()
    taskless_service, taskless_evidence_id, *_rest = _prepare_integration_repair_round(
        review_setup,
        monkeypatch,
    )
    restarted_taskless = PlanReviewEvidenceService(service.db).get_evidence(taskless_evidence_id)
    assert restarted_taskless.round_number == 2
    assert restarted_taskless.prior_round_context is not None

    manager = LocalTaskManager(service.db)
    staged_results: list[tuple[str, object]] = []
    for verdict in ("needs_review", "approved"):
        staged_path = plan_path.with_name(f"staged-{verdict}.md")
        staged_path.write_bytes(original)
        task = manager.create_task(
            project_id,
            f"Staged {verdict}",
            validation_criteria=f"The staged {verdict} verdict is delivered before mutation.",
        )
        before = manager.get_task(task.id)
        prepared = service.prepare_plan_review_round(
            project_id=project_id,
            plan_path=staged_path,
            round_number=1,
            task_id=task.id,
            stage="review",
        )
        result: dict[str, object] = {
            "verdict": verdict,
            "findings": [],
            "coverage_attestation": coverage_attestation(
                evidence_id=prepared.evidence_id,
                shadow_valid=verdict == "approved",
                manifest_entries=[{"source_section": "1.1"}],
            ),
            "convergence_telemetry": enriched_telemetry(),
        }
        if verdict == "approved":
            derived = service.derive_plan_review_manifest(
                prepared.evidence_id,
                routing_decisions={},
            )
            entries = derived["manifest_entries"]
            assert isinstance(entries, list)
            result["routing_decisions"] = {}
            result["manifest_entries"] = entries
            result["coverage_attestation"] = coverage_attestation(
                evidence_id=prepared.evidence_id,
                manifest_entries=entries,
            )
        finalized = service.finalize_plan_review_evidence(
            prepared.evidence_id,
            result,
        )
        after = manager.get_task(task.id)
        assert after.to_dict()["state"] == before.to_dict()["state"]
        staged_results.append((verdict, finalized.round_result))

    assert [verdict for verdict, _result in staged_results] == [
        "needs_review",
        "approved",
    ]
    staged_contract = (
        Path(__file__).parents[2] / "src/gobby/install/shared/workflows/agents/plan-adversary.yaml"
    ).read_text(encoding="utf-8")
    assert '"gobby-agents:end_agent_run"' in staged_contract
    assert '"gobby-agents:send_message"' not in staged_contract


def test_inconclusive_terminal_replaces_run(
    review_setup: tuple[PlanReviewEvidenceService, str, str, Path],
) -> None:
    service, project_id, session_id, plan_path = review_setup
    old = service.prepare_plan_review_round(
        project_id=project_id,
        plan_path=plan_path,
        round_number=1,
        session_id=session_id,
    )
    runs = LocalAgentRunManager(service.db)
    old_run = runs.create(
        parent_session_id=session_id,
        provider="codex",
        prompt="Review the old evidence generation.",
    )
    service.bind_evidence_run(old.evidence_id, old_run.id)
    persist_delivered_round_result(
        service.db,
        run_id=old_run.id,
        round_result={
            "verdict": "inconclusive",
            "evidence_id": old.evidence_id,
            "reason": {
                "reason_code": "source_drift",
                "paths": [".gobby/plans/plan.md"],
            },
            "convergence_telemetry": delivered_telemetry(),
        },
    )

    outcome = terminalize_plan_review_run(
        runs,
        run_id=old_run.id,
        action="complete",
    )
    assert outcome.expired is True
    assert service.get_evidence(old.evidence_id).expired_at is not None

    plan_path.write_text(
        plan_path.read_text(encoding="utf-8").replace(
            "Behavior exists.",
            "Replacement behavior exists.",
        ),
        encoding="utf-8",
    )
    replacement = service.prepare_plan_review_round(
        project_id=project_id,
        plan_path=plan_path,
        round_number=1,
        session_id=session_id,
    )
    replacement_run = runs.create(
        parent_session_id=session_id,
        provider="codex",
        prompt="Review the replacement evidence generation.",
    )
    service.bind_evidence_run(replacement.evidence_id, replacement_run.id)

    assert replacement.evidence_id != old.evidence_id
    assert replacement_run.id != old_run.id
    assert (
        service.get_evidence(replacement.evidence_id).snapshot
        != service.get_evidence(old.evidence_id).snapshot
    )
    assert service.get_evidence(replacement.evidence_id).round_result is None


def test_list_recent_orders_filters_and_bounds_results(
    review_setup: tuple[PlanReviewEvidenceService, str, str, Path],
    temp_db: HubDatabase,
) -> None:
    service, project_id, session_id, plan_path = review_setup
    first_prepared = service.prepare_plan_review_round(
        project_id=project_id,
        plan_path=plan_path,
        round_number=1,
        session_id=session_id,
    )
    first = service.get_evidence(first_prepared.evidence_id)

    with temp_db.transaction() as transaction:
        second = service.store.insert(
            transaction=transaction,
            project_id=project_id,
            plan_path=".gobby/plans/temporary.md",
            plan_hash="second-plan-hash",
            sections=first.section_manifest,
            snapshot=first.snapshot,
            round_number=2,
            lease_seconds=300,
            session_id=session_id,
            task_id=None,
            stage=None,
        )
        transaction.execute(
            """
            UPDATE plan_review_evidence
            SET plan_path = %s,
                finalized_at = TIMESTAMPTZ '2026-07-28 12:00:00+00',
                created_at = TIMESTAMPTZ '2026-07-28 12:00:00+00'
            WHERE evidence_id = %s
            """,
            (first.plan_path, second.evidence_id),
        )
        third = service.store.insert(
            transaction=transaction,
            project_id=project_id,
            plan_path=".gobby/plans/other.md",
            plan_hash="third-plan-hash",
            sections=first.section_manifest,
            snapshot=first.snapshot,
            round_number=3,
            lease_seconds=300,
            session_id=session_id,
            task_id=None,
            stage=None,
        )
        transaction.execute(
            """
            UPDATE plan_review_evidence
            SET created_at = TIMESTAMPTZ '2026-07-28 12:00:00+00'
            WHERE evidence_id = %s
            """,
            (third.evidence_id,),
        )
        transaction.execute(
            """
            UPDATE plan_review_evidence
            SET created_at = TIMESTAMPTZ '2026-07-27 12:00:00+00'
            WHERE evidence_id = %s
            """,
            (first.evidence_id,),
        )

    tied_ids = sorted([second.evidence_id, third.evidence_id])
    assert [row.evidence_id for row in service.store.list_recent(project_id=project_id)] == [
        *tied_ids,
        first.evidence_id,
    ]
    assert [
        row.evidence_id
        for row in service.store.list_recent(
            project_id=project_id,
            plan_path=first.plan_path,
        )
    ] == [second.evidence_id, first.evidence_id]
    assert {
        row.evidence_id for row in service.store.list_recent(project_id=project_id, live_only=True)
    } == {first.evidence_id, third.evidence_id}
    assert len(service.store.list_recent(project_id=project_id, limit=2)) == 2

    with pytest.raises(ValueError, match="limit must be between 1 and 500"):
        service.store.list_recent(project_id=project_id, limit=501)
