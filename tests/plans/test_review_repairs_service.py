"""Postgres-backed tests for ``PlanReviewEvidenceService.apply_plan_review_repairs``."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path

import pytest

from gobby.plans.review_evidence import PlanReviewEvidenceService
from gobby.plans.review_evidence_models import ReviewEvidenceError
from gobby.storage.agents import LocalAgentRunManager
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.projects import LocalProjectManager
from gobby.storage.sessions import SessionManager
from gobby.storage.tasks import LocalTaskManager
from gobby.utils.machine_id import require_machine_id
from tests.review_coverage_helpers import coverage_attestation

PLAN_TEXT = "\n".join(
    [
        "# Repairs",
        "**Plan ID:** repairs",
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
        "### 1.2 Follow-up",
        "`kind: deliverable`",
        "",
        "Target: `src/other.py`",
        "",
        "**Acceptance:**",
        "- 1.2.1 — Follow-up done. file: `src/other.py`",
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
    ]
)


@dataclass(frozen=True)
class RepairSetup:
    service: PlanReviewEvidenceService
    project_id: str
    session_id: str
    plan_path: Path


@pytest.fixture
def repair_setup(temp_db: HubDatabase, tmp_path: Path) -> RepairSetup:
    project = LocalProjectManager(temp_db).create(name="review-repairs", repo_path=str(tmp_path))
    session = SessionManager(temp_db).register(
        external_id="review-repairs-parent",
        machine_id=require_machine_id(),
        source="codex",
        project_id=project.id,
    )
    plan_dir = tmp_path / ".gobby" / "plans"
    plan_dir.mkdir(parents=True)
    plan_path = plan_dir / "repairs.md"
    plan_path.write_text(PLAN_TEXT, encoding="utf-8")
    return RepairSetup(PlanReviewEvidenceService(temp_db), project.id, session.id, plan_path)


def _finding(finding_id: str, **overrides: object) -> dict[str, object]:
    finding: dict[str, object] = {
        "finding_id": finding_id,
        "section_id": "1.1",
        "check_key": "targets-complete",
        "severity": "blocking",
        "category": "traceability",
        "location": "§ 1.1 Targets",
        "description": "The consumer is missing.",
        "fix": "Add the consumer.",
        "prevention": "Sweep consumers.",
        "root_cause": "Only direct edits were inventoried.",
    }
    finding.update(overrides)
    return finding


def _findings() -> list[dict[str, object]]:
    return [
        _finding(
            "F1",
            repairs=[
                {"kind": "add_targets", "section_id": "1.1", "entries": ["`src/consumer.py`"]},
                {
                    "kind": "add_acceptance",
                    "section_id": "1.1",
                    "items": [{"prose": "Consumer updated", "artifact": "file: `src/consumer.py`"}],
                },
            ],
        ),
        _finding(
            "F2",
            section_id="1.2",
            category="bad-sequencing",
            check_key="ordering",
            repairs=[{"kind": "add_dependency", "section_id": "1.2", "on": ["1.1"]}],
        ),
        _finding("F3", category="unhandled-edge", check_key="edge"),
    ]


def _rejection(evidence_id: str) -> dict[str, object]:
    return {
        "verdict": "needs_review",
        "findings": _findings(),
        "coverage_attestation": coverage_attestation(evidence_id=evidence_id, shadow_valid=False),
    }


def _bind(setup: RepairSetup, *, round_number: int = 1) -> tuple[str, str]:
    prepared = setup.service.prepare_plan_review_round(
        project_id=setup.project_id,
        plan_path=setup.plan_path,
        round_number=round_number,
        session_id=setup.session_id,
    )
    run = LocalAgentRunManager(setup.service.db).create(
        parent_session_id=setup.session_id,
        provider="codex",
        prompt="review",
    )
    setup.service.bind_evidence_run(prepared.evidence_id, run.id)
    return prepared.evidence_id, run.id


def _finalized_rejection(setup: RepairSetup) -> str:
    evidence_id, _run_id = _bind(setup)
    payload = _rejection(evidence_id)
    setup.service.append_plan_changelog_round(
        evidence_id,
        "**Round 1** `kind: verification`\n\n- verdict: needs_review",
        payload,
    )
    setup.service.finalize_plan_review_evidence(evidence_id, payload)
    return evidence_id


def _fences(text: str) -> list[str]:
    return re.findall(r"```json plan-review-round\n(.+?)\n```", text, flags=re.DOTALL)


def _sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def test_apply_then_reapply_is_noop(repair_setup: RepairSetup) -> None:
    evidence_id = _finalized_rejection(repair_setup)
    before = repair_setup.plan_path.read_bytes()
    fences_before = _fences(before.decode())
    assert len(fences_before) == 1

    result = repair_setup.service.apply_plan_review_repairs(evidence_id, ["F1", "F2", "F3"])

    after = repair_setup.plan_path.read_bytes()
    assert result["ok"] is True
    assert result["evidence_id"] == evidence_id
    assert result["changed"] is True
    assert result["plan_hash_before"] == _sha256(before)
    assert result["plan_hash_after"] == _sha256(after)
    assert result["plan_hash_before"] != result["plan_hash_after"]
    assert result["applied"] == [
        {
            "finding_id": "F1",
            "kind": "add_targets",
            "section_id": "1.1",
            "added": ["src/consumer.py"],
        },
        {
            "finding_id": "F1",
            "kind": "add_acceptance",
            "section_id": "1.1",
            "added": ["Consumer updated. file: `src/consumer.py`."],
        },
        {"finding_id": "F2", "kind": "add_dependency", "section_id": "1.2", "added": ["1.1"]},
    ]
    assert result["skipped"] == [{"finding_id": "F3", "reason": "prose_only"}]
    assert isinstance(result["diff"], str) and result["diff"].startswith("--- ")
    text = after.decode()
    assert "Target: `src/example.py`\n- `src/consumer.py`\n" in text
    assert "- 1.1.2 — Consumer updated. file: `src/consumer.py`.\n" in text
    assert "### 1.2 Follow-up (depends: 1.1)\n" in text
    assert _fences(text) == fences_before

    again = repair_setup.service.apply_plan_review_repairs(evidence_id, ["F1", "F2", "F3"])

    assert again["changed"] is False
    assert again["applied"] == []
    skipped_again = again["skipped"]
    assert isinstance(skipped_again, list)
    assert {entry["reason"] for entry in skipped_again} == {"already_present", "prose_only"}
    assert again["diff"] == ""
    assert again["plan_hash_before"] == again["plan_hash_after"] == _sha256(after)
    assert repair_setup.plan_path.read_bytes() == after


def test_empty_accepted_ids_is_a_noop(repair_setup: RepairSetup) -> None:
    evidence_id = _finalized_rejection(repair_setup)
    before = repair_setup.plan_path.read_bytes()

    result = repair_setup.service.apply_plan_review_repairs(evidence_id, [])

    assert result["ok"] is True
    assert result["changed"] is False
    assert result["applied"] == [] and result["skipped"] == []
    assert result["plan_hash_before"] == result["plan_hash_after"] == _sha256(before)
    assert repair_setup.plan_path.read_bytes() == before


def test_gate_order(repair_setup: RepairSetup) -> None:
    service = repair_setup.service
    with pytest.raises(ReviewEvidenceError) as not_found:
        service.apply_plan_review_repairs("00000000-0000-4000-8000-000000000000", ["F1"])
    assert not_found.value.code == "evidence_not_found"

    evidence_id, _run_id = _bind(repair_setup)
    other_plan = repair_setup.plan_path.with_name("other.md")
    other_plan.write_text(PLAN_TEXT, encoding="utf-8")
    with pytest.raises(ReviewEvidenceError) as wrong_plan:
        service.apply_plan_review_repairs(evidence_id, ["F1"], plan_path=other_plan)
    assert wrong_plan.value.code == "wrong_plan"

    with pytest.raises(ReviewEvidenceError) as unfinalized:
        service.apply_plan_review_repairs(evidence_id, ["F1"])
    assert unfinalized.value.code == "evidence_not_finalized"

    payload = _rejection(evidence_id)
    service.append_plan_changelog_round(
        evidence_id,
        "**Round 1** `kind: verification`\n\n- verdict: needs_review",
        payload,
    )
    service.finalize_plan_review_evidence(evidence_id, payload)
    with pytest.raises(ReviewEvidenceError) as unknown:
        service.apply_plan_review_repairs(evidence_id, ["F1", "NOPE"])
    assert unknown.value.code == "unknown_finding_id"
    assert unknown.value.details["unknown_finding_ids"] == ["NOPE"]


def test_staged_evidence_is_rejected(repair_setup: RepairSetup) -> None:
    service = repair_setup.service
    task = LocalTaskManager(service.db).create_task(
        repair_setup.project_id,
        "Review stage evidence",
        validation_criteria="Staged evidence never applies repairs.",
    )
    prepared = service.prepare_plan_review_round(
        project_id=repair_setup.project_id,
        plan_path=repair_setup.plan_path,
        round_number=1,
        task_id=task.id,
        stage="review",
    )
    with pytest.raises(ReviewEvidenceError) as excinfo:
        service.apply_plan_review_repairs(prepared.evidence_id, ["F1"])
    assert excinfo.value.code == "not_interactive_evidence"


def test_approved_round_is_rejected(repair_setup: RepairSetup) -> None:
    service = repair_setup.service
    evidence_id, run_id = _bind(repair_setup)
    derived = service.derive_plan_review_manifest(evidence_id, routing_decisions={})
    manifest_entries = derived["manifest_entries"]
    assert isinstance(manifest_entries, list)
    approval = {
        "verdict": "approved",
        "findings": [],
        "routing_decisions": {},
        "manifest_entries": manifest_entries,
        "coverage_attestation": coverage_attestation(
            evidence_id=evidence_id,
            manifest_entries=manifest_entries,
        ),
    }
    service.apply_plan_review_manifest(
        evidence_id,
        approval,
        plan_path=repair_setup.plan_path,
        run_id=run_id,
    )
    service.append_plan_changelog_round(
        evidence_id,
        "**Round 1** `kind: verification`\n\n- verdict: approved",
        approval,
    )
    service.finalize_plan_review_evidence(evidence_id, approval)

    with pytest.raises(ReviewEvidenceError) as excinfo:
        service.apply_plan_review_repairs(evidence_id, ["F1"])
    assert excinfo.value.code == "not_rejection_round"


def test_write_failure_leaves_plan_untouched(
    repair_setup: RepairSetup,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    evidence_id = _finalized_rejection(repair_setup)
    before = repair_setup.plan_path.read_bytes()

    def failing_write(path: Path, content: bytes) -> None:
        raise OSError("disk full")

    monkeypatch.setattr("gobby.plans.review_evidence.atomic_write_bytes", failing_write)
    with pytest.raises(OSError, match="disk full"):
        repair_setup.service.apply_plan_review_repairs(evidence_id, ["F1"])
    assert repair_setup.plan_path.read_bytes() == before
