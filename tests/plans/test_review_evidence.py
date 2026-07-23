from __future__ import annotations

import re
from collections.abc import Mapping
from pathlib import Path
from typing import Never

import pytest

from gobby.plans.review_evidence import PlanReviewEvidenceService
from gobby.plans.review_evidence_io import (
    atomic_write_bytes,
    build_section_manifest,
    ensure_checkpoint,
    manifest_key,
)
from gobby.plans.review_evidence_models import ReviewEvidenceError
from gobby.storage.agents import LocalAgentRunManager
from gobby.storage.hub.protocol import HubDatabase, Transaction
from gobby.storage.projects import LocalProjectManager
from gobby.storage.sessions import SessionManager
from gobby.storage.tasks import LocalTaskManager


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
    return PlanReviewEvidenceService(temp_db), project.id, session.id, plan_path


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


def test_stale_write_guard_and_lifecycle(
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
    checkpoint = service.render_v1_round_checkpoint(
        prepared.evidence_id,
        {"verdict": "needs_review", "findings": [{"message": "fix it"}]},
    )
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
    assert recovered.round_result == {
        "verdict": "needs_review",
        "findings": [{"message": "fix it"}],
    }
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
    baseline = (repo_root / "src/gobby/storage/postgres_baseline_schema.sql").read_text()
    migration = (
        repo_root / "src/gobby/storage/migrations/338_plan_review_evidence.sql"
    ).read_text()

    def table_definition(sql: str) -> str:
        match = re.search(
            r"CREATE TABLE(?: IF NOT EXISTS)? plan_review_evidence \((.*?)\n\);",
            sql,
            flags=re.DOTALL,
        )
        assert match is not None
        return " ".join(match.group(1).split())

    assert table_definition(baseline) == table_definition(migration)

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
    for statement in migration.split(";"):
        if statement.strip():
            temp_db.execute(statement)
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

    task = LocalTaskManager(service.db).create_task(project_id, "Review stage evidence")
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
        {"verdict": "needs_review", "findings": []},
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


def test_manifest_compare_and_apply(
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
    run = LocalAgentRunManager(service.db).create(
        parent_session_id=session_id,
        provider="codex",
        prompt="review",
    )
    service.bind_evidence_run(prepared.evidence_id, run.id)
    approval = {
        "verdict": "approved",
        "findings": [],
        "manifest_entries": [
            {
                "title": "Implement example",
                "source_section": "1.1",
                "covers": ["1.1.1"],
                "category": "code",
                "implementation_domain": "backend",
                "priority": 2,
                "task_type": "feature",
                "tdd": False,
                "labels": ["covers:review-evidence:1.1:1.1.1"],
                "description": "Implement the example.",
                "validation_criteria": "Example behavior is tested.",
            }
        ],
    }
    original_bytes = plan_path.read_bytes()

    def crash_atomic_write(_path: Path, _content: bytes) -> None:
        raise OSError("simulated crash")

    monkeypatch.setattr(
        "gobby.plans.review_evidence.atomic_write_bytes",
        crash_atomic_write,
    )
    with pytest.raises(OSError, match="simulated crash"):
        service.apply_plan_review_manifest(
            prepared.evidence_id,
            approval,
            plan_path=plan_path,
            run_id=run.id,
        )
    pending = service.get_evidence(prepared.evidence_id)
    assert pending.manifest_state == "pending"
    assert pending.round_result == approval
    assert plan_path.read_bytes() == original_bytes

    monkeypatch.setattr(
        "gobby.plans.review_evidence.atomic_write_bytes",
        atomic_write_bytes,
    )
    applied = service.apply_plan_review_manifest(
        prepared.evidence_id,
        approval,
        plan_path=plan_path,
        run_id=run.id,
    )
    row = service.get_evidence(prepared.evidence_id)
    assert row.manifest_state == "applied"
    assert row.round_result == approval
    assert row.finalized_at is None
    first_bytes = plan_path.read_bytes()
    assert (
        service.apply_plan_review_manifest(
            prepared.evidence_id,
            approval,
            plan_path=plan_path,
            run_id=run.id,
        )
        == applied
    )
    assert plan_path.read_bytes() == first_bytes

    changed = {**approval, "findings": [{"message": "different"}]}
    with pytest.raises(ReviewEvidenceError, match="different manifest payload"):
        service.apply_plan_review_manifest(
            prepared.evidence_id,
            changed,
            plan_path=plan_path,
            run_id=run.id,
        )

    landed_path = plan_path.with_name("review-evidence-landed.md")
    landed_path.write_bytes(original_bytes)
    landed_prepared = service.prepare_plan_review_round(
        project_id=project_id,
        plan_path=landed_path,
        round_number=1,
        session_id=session_id,
    )
    landed_run = LocalAgentRunManager(service.db).create(
        parent_session_id=session_id,
        provider="codex",
        prompt="review landed",
    )
    service.bind_evidence_run(landed_prepared.evidence_id, landed_run.id)
    complete_manifest_apply = service.store.complete_manifest_apply

    def crash_before_checkpoint(
        *,
        transaction: Transaction,
        evidence_id: str,
        result: Mapping[str, object],
    ) -> Never:
        _ = transaction, evidence_id, result
        raise RuntimeError("simulated checkpoint crash")

    monkeypatch.setattr(service.store, "complete_manifest_apply", crash_before_checkpoint)
    with pytest.raises(RuntimeError, match="simulated checkpoint crash"):
        service.apply_plan_review_manifest(
            landed_prepared.evidence_id,
            approval,
            plan_path=landed_path,
            run_id=landed_run.id,
        )
    landed_bytes = landed_path.read_bytes()
    assert landed_bytes != original_bytes
    assert service.get_evidence(landed_prepared.evidence_id).manifest_state == "pending"
    monkeypatch.setattr(
        service.store,
        "complete_manifest_apply",
        complete_manifest_apply,
    )
    service.apply_plan_review_manifest(
        landed_prepared.evidence_id,
        approval,
        plan_path=landed_path,
        run_id=landed_run.id,
    )
    assert landed_path.read_bytes() == landed_bytes
    assert service.get_evidence(landed_prepared.evidence_id).manifest_state == "applied"

    drift_path = plan_path.with_name("review-evidence-drift.md")
    drift_path.write_bytes(original_bytes)
    drift_prepared = service.prepare_plan_review_round(
        project_id=project_id,
        plan_path=drift_path,
        round_number=1,
        session_id=session_id,
    )
    drift_run = LocalAgentRunManager(service.db).create(
        parent_session_id=session_id,
        provider="codex",
        prompt="review drift",
    )
    service.bind_evidence_run(drift_prepared.evidence_id, drift_run.id)
    monkeypatch.setattr(
        "gobby.plans.review_evidence.atomic_write_bytes",
        crash_atomic_write,
    )
    with pytest.raises(OSError, match="simulated crash"):
        service.apply_plan_review_manifest(
            drift_prepared.evidence_id,
            approval,
            plan_path=drift_path,
            run_id=drift_run.id,
        )
    monkeypatch.setattr(
        "gobby.plans.review_evidence.atomic_write_bytes",
        atomic_write_bytes,
    )
    drift_path.write_bytes(
        drift_path.read_bytes().replace(b"Behavior exists.", b"Behavior drifted.")
    )
    drifted_bytes = drift_path.read_bytes()
    with pytest.raises(ReviewEvidenceError, match="reviewed plan sections changed"):
        service.apply_plan_review_manifest(
            drift_prepared.evidence_id,
            approval,
            plan_path=drift_path,
            run_id=drift_run.id,
        )
    revoked = service.get_evidence(drift_prepared.evidence_id)
    assert revoked.manifest_state == "revoked"
    assert revoked.round_result is None
    assert revoked.manifest_payload == approval
    assert drift_path.read_bytes() == drifted_bytes
    with pytest.raises(ReviewEvidenceError, match="manifest intent was revoked"):
        service.apply_plan_review_manifest(
            drift_prepared.evidence_id,
            approval,
            plan_path=drift_path,
            run_id=drift_run.id,
        )
    LocalAgentRunManager(service.db).cancel(drift_run.id)
    rereview = service.prepare_plan_review_round(
        project_id=project_id,
        plan_path=drift_path,
        round_number=2,
        session_id=session_id,
    )
    assert rereview.evidence_id != drift_prepared.evidence_id
    assert service.get_evidence(drift_prepared.evidence_id).expired_at is not None


def test_two_phase_run_binding(
    review_setup: tuple[PlanReviewEvidenceService, str, str, Path],
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
    approval = {
        "verdict": "approved",
        "findings": [],
        "manifest_entries": [
            {
                "title": "Implement example",
                "source_section": "1.1",
                "covers": ["1.1.1"],
                "category": "code",
                "implementation_domain": "backend",
                "priority": 2,
                "task_type": "feature",
                "tdd": False,
                "labels": ["covers:review-evidence:1.1:1.1.1"],
                "description": "Implement the example.",
                "validation_criteria": "Example behavior is tested.",
            }
        ],
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
