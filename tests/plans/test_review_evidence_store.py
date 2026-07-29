from __future__ import annotations

import re
import uuid
from pathlib import Path

import pytest

from gobby.plans.review_evidence_models import ReviewEvidenceError, SectionHash
from gobby.plans.review_evidence_store import PlanReviewEvidenceStore
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.projects import LocalProjectManager
from gobby.storage.sessions import SessionManager


@pytest.fixture
def evidence_store_row(
    temp_db: HubDatabase,
    tmp_path: Path,
) -> tuple[PlanReviewEvidenceStore, str]:
    suffix = uuid.uuid4().hex
    project = LocalProjectManager(temp_db).create(
        name=f"review-evidence-store-{suffix}",
        repo_path=str(tmp_path),
    )
    session = SessionManager(temp_db).register(
        external_id=f"review-evidence-store-{suffix}",
        machine_id="test-machine",
        source="codex",
        project_id=project.id,
    )
    store = PlanReviewEvidenceStore(temp_db)
    with temp_db.transaction() as transaction:
        evidence = store.insert(
            transaction=transaction,
            project_id=project.id,
            plan_path=".gobby/plans/review.md",
            plan_hash="plan-hash",
            sections=(SectionHash(section_id="1.1", section_hash="section-hash"),),
            snapshot=b"snapshot",
            round_number=2,
            lease_seconds=300,
            session_id=session.id,
            task_id=None,
            stage=None,
        )
    return store, evidence.evidence_id


def test_evidence_jsonb_columns_round_trip(
    temp_db: HubDatabase,
    evidence_store_row: tuple[PlanReviewEvidenceStore, str],
) -> None:
    store, evidence_id = evidence_store_row
    repair_attestations: list[dict[str, object]] = [
        {
            "prior_finding_id": "F-1",
            "check_key": "consumer-parity",
            "validation_evidence": ["focused-test"],
        }
    ]
    prior_round_context: dict[str, object] = {
        "prior_evidence_id": "prior-1",
        "requirements_bundle": {"digest": "abc123"},
    }
    quality_ledger: list[dict[str, object]] = [
        {
            "ledger_entry_id": "ledger-1",
            "check_key": "consumer-parity",
            "status": "open",
        }
    ]

    with temp_db.transaction() as transaction:
        prepared = store.write_preparation_context(
            transaction=transaction,
            evidence_id=evidence_id,
            repair_attestations=repair_attestations,
            prior_round_context=prior_round_context,
        )
        ledgered = store.write_quality_ledger(
            transaction=transaction,
            evidence_id=evidence_id,
            quality_ledger=quality_ledger,
        )
        replayed_preparation = store.write_preparation_context(
            transaction=transaction,
            evidence_id=evidence_id,
            repair_attestations=repair_attestations,
            prior_round_context=prior_round_context,
        )
        replayed_ledger = store.write_quality_ledger(
            transaction=transaction,
            evidence_id=evidence_id,
            quality_ledger=quality_ledger,
        )

    persisted = store.require(evidence_id)
    assert prepared.repair_attestations == repair_attestations
    assert prepared.prior_round_context == prior_round_context
    assert ledgered.quality_ledger == quality_ledger
    assert replayed_preparation == ledgered
    assert replayed_ledger == ledgered
    assert persisted.quality_ledger == quality_ledger
    assert persisted.repair_attestations == repair_attestations
    assert persisted.prior_round_context == prior_round_context


def test_evidence_jsonb_column_writes_reject_conflicts(
    temp_db: HubDatabase,
    evidence_store_row: tuple[PlanReviewEvidenceStore, str],
) -> None:
    store, evidence_id = evidence_store_row
    with temp_db.transaction() as transaction:
        store.write_preparation_context(
            transaction=transaction,
            evidence_id=evidence_id,
            repair_attestations=[],
            prior_round_context={"prior_evidence_id": "prior-1"},
        )
        store.write_quality_ledger(
            transaction=transaction,
            evidence_id=evidence_id,
            quality_ledger=[],
        )

    with temp_db.transaction() as transaction:
        with pytest.raises(ReviewEvidenceError, match="preparation context conflicts"):
            store.write_preparation_context(
                transaction=transaction,
                evidence_id=evidence_id,
                repair_attestations=[{"prior_finding_id": "F-2"}],
                prior_round_context={"prior_evidence_id": "prior-2"},
            )
        with pytest.raises(ReviewEvidenceError, match="quality ledger conflicts"):
            store.write_quality_ledger(
                transaction=transaction,
                evidence_id=evidence_id,
                quality_ledger=[{"ledger_entry_id": "ledger-2"}],
            )


def test_preparation_context_replay_rejects_finalized_evidence(
    temp_db: HubDatabase,
    evidence_store_row: tuple[PlanReviewEvidenceStore, str],
) -> None:
    store, evidence_id = evidence_store_row
    context = {"prior_evidence_id": "prior-1"}
    with temp_db.transaction() as transaction:
        store.write_preparation_context(
            transaction=transaction,
            evidence_id=evidence_id,
            repair_attestations=[],
            prior_round_context=context,
        )
    temp_db.execute(
        "UPDATE plan_review_evidence SET finalized_at = NOW() WHERE evidence_id = %s",
        (evidence_id,),
    )

    with temp_db.transaction() as transaction:
        with pytest.raises(ReviewEvidenceError) as error:
            store.write_preparation_context(
                transaction=transaction,
                evidence_id=evidence_id,
                repair_attestations=[],
                prior_round_context=context,
            )

    assert error.value.code == "preparation_context_closed"


def test_quality_ledger_schema_migration_and_baseline_match() -> None:
    repo_root = Path(__file__).resolve().parents[2]
    migration = (
        repo_root / "src/gobby/storage/migrations/345_plan_review_quality_ledger.sql"
    ).read_text()
    baseline = (repo_root / "src/gobby/storage/postgres_baseline_schema.sql").read_text()

    expected_types = {
        "quality_ledger": "array",
        "repair_attestations": "array",
        "prior_round_context": "object",
    }
    for column, json_type in expected_types.items():
        constraint = (
            rf"CONSTRAINT plan_review_evidence_{column}_type\s+"
            rf"CHECK \(jsonb_typeof\({column}\) = '{json_type}'\)"
        )
        assert re.search(rf"ADD COLUMN(?: IF NOT EXISTS)? {column} JSONB", migration)
        assert re.search(rf"\b{column} JSONB", baseline)
        assert re.search(constraint, migration)
        assert re.search(constraint, baseline)
