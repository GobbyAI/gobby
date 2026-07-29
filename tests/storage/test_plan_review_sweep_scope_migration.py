from __future__ import annotations

import json
import uuid
from pathlib import Path
from typing import cast

from gobby.plans.consumer_sweep import CandidateSite
from gobby.plans.review_sweep_scope import (
    SweepRequirement,
    SweepScope,
    canonicalize_sweep_scope,
)
from gobby.plans.review_sweeps import _repair_requirements
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.migrations import _execute_sql_script
from gobby.storage.projects import LocalProjectManager
from gobby.storage.sessions import SessionManager


def test_sweep_scope_migration_fans_out_all_locations_idempotently(
    postgres_db: HubDatabase,
) -> None:
    project = LocalProjectManager(postgres_db).create(f"sweep-migration-{uuid.uuid4()}")
    session = SessionManager(postgres_db).register(
        external_id=f"sweep-migration-{uuid.uuid4()}",
        machine_id="test-machine",
        source="codex",
        project_id=project.id,
    )
    scope = SweepScope(
        candidate_sites=(
            CandidateSite(
                site_id="site-1",
                path="src/example.py",
                source_kind="symbol_call",
                source_ref="example.consumer",
                status="resolved",
                language="python",
                section_ids=("1.1",),
            ),
        ),
        requirements=(
            SweepRequirement(
                prior_finding_id="finding-1",
                check_key="repair.finding-1",
                changed_section_ids=("1.1",),
                changed_contracts=(),
                changed_targets=(),
                required_consumer_site_ids=("site-1",),
                adjacent_variant_ids=("variant-1",),
                interaction_edge_ids=(),
            ),
        ),
        interaction_edges=(),
    )
    digest = scope.digest
    old_scope: dict[str, object] = {**scope.to_dict(), "digest": digest}
    old_attestation: dict[str, object] = {
        "prior_finding_id": "finding-1",
        "repair_universe_digest": digest,
        "unrelated_attestation_key": "preserved",
    }
    old_context: dict[str, object] = {
        "prior_finding_resolutions": [{"prior_finding_id": "finding-1", "decision": "repair"}],
        "repair_attestations": [old_attestation],
        "repair_universe": old_scope,
        "repair_universe_digest": digest,
        "unrelated_context_key": {"preserved": True},
    }
    expected_scope = {key: value for key, value in old_scope.items() if key != "digest"}
    evidence_id = _insert_evidence(
        postgres_db,
        project_id=project.id,
        session_id=session.id,
        plan_path=".gobby/plans/migrated.md",
        repair_attestations=[old_attestation],
        prior_round_context=old_context,
    )
    null_id = _insert_evidence(
        postgres_db,
        project_id=project.id,
        session_id=session.id,
        plan_path=".gobby/plans/null.md",
        repair_attestations=None,
        prior_round_context=None,
    )
    empty_id = _insert_evidence(
        postgres_db,
        project_id=project.id,
        session_id=session.id,
        plan_path=".gobby/plans/empty.md",
        repair_attestations=[],
        prior_round_context={},
    )
    expected_requirements = _repair_requirements(
        {
            "prior_finding_resolutions": old_context["prior_finding_resolutions"],
            "repair_attestations": old_context["repair_attestations"],
            "current_sweep_scope": old_scope,
        }
    )
    migration = (
        Path(__file__).parents[2] / "src/gobby/storage/migrations/347_plan_review_sweep_scope.sql"
    ).read_text(encoding="utf-8")

    with postgres_db.transaction() as transaction:
        _execute_sql_script(transaction, migration)
    first = postgres_db.fetchone(
        """
        SELECT repair_attestations, prior_round_context
        FROM plan_review_evidence
        WHERE evidence_id = %s
        """,
        (evidence_id,),
    )
    assert first is not None
    migrated_context = cast(dict[str, object], _json_value(first["prior_round_context"]))
    assert migrated_context["submitted_sweep_scope"] == expected_scope
    assert migrated_context["current_sweep_scope"] == expected_scope
    assert migrated_context["submitted_sweep_scope_digest"] == digest
    assert (
        canonicalize_sweep_scope(
            cast(dict[str, object], migrated_context["submitted_sweep_scope"]),
            digest=digest,
        ).digest
        == digest
    )
    assert migrated_context["unrelated_context_key"] == {"preserved": True}
    assert "repair_universe" not in migrated_context
    assert "repair_universe_digest" not in migrated_context
    assert migrated_context["required_scope_delta"] == migrated_context["inventory_churn"]
    nested = cast(list[dict[str, object]], migrated_context["repair_attestations"])
    stored = cast(list[dict[str, object]], _json_value(first["repair_attestations"]))
    for attestations in (nested, stored):
        assert attestations == [
            {
                "prior_finding_id": "finding-1",
                "sweep_scope_digest": digest,
                "unrelated_attestation_key": "preserved",
            }
        ]
    assert _repair_requirements(migrated_context) == expected_requirements

    with postgres_db.transaction() as transaction:
        _execute_sql_script(transaction, migration)
    second = postgres_db.fetchone(
        """
        SELECT repair_attestations, prior_round_context
        FROM plan_review_evidence
        WHERE evidence_id = %s
        """,
        (evidence_id,),
    )
    assert second == first
    null_row = postgres_db.fetchone(
        """
        SELECT repair_attestations, prior_round_context
        FROM plan_review_evidence
        WHERE evidence_id = %s
        """,
        (null_id,),
    )
    empty_row = postgres_db.fetchone(
        """
        SELECT repair_attestations, prior_round_context
        FROM plan_review_evidence
        WHERE evidence_id = %s
        """,
        (empty_id,),
    )
    assert null_row == {"repair_attestations": None, "prior_round_context": None}
    assert empty_row is not None
    assert _json_value(empty_row["repair_attestations"]) == []
    assert _json_value(empty_row["prior_round_context"]) == {}


def _insert_evidence(
    db: HubDatabase,
    *,
    project_id: str,
    session_id: str,
    plan_path: str,
    repair_attestations: list[dict[str, object]] | None,
    prior_round_context: dict[str, object] | None,
) -> str:
    evidence_id = str(uuid.uuid4())
    db.execute(
        """
        INSERT INTO plan_review_evidence (
            evidence_id,
            project_id,
            plan_path,
            plan_hash,
            section_manifest,
            snapshot,
            round_number,
            session_id,
            lease_expires_at,
            repair_attestations,
            prior_round_context
        )
        VALUES (
            %s, %s, %s, %s, '[]'::jsonb, %s, 2, %s, NOW() + INTERVAL '1 hour',
            CAST(%s AS JSONB), CAST(%s AS JSONB)
        )
        """,
        (
            evidence_id,
            project_id,
            plan_path,
            "b" * 64,
            b"plan",
            session_id,
            json.dumps(repair_attestations) if repair_attestations is not None else None,
            json.dumps(prior_round_context) if prior_round_context is not None else None,
        ),
    )
    return evidence_id


def _json_value(raw: object) -> object:
    return json.loads(raw) if isinstance(raw, str) else raw
