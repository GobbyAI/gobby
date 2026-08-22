"""Contract tests for the bundled review-learning skill and producer hooks."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import cast
from unittest.mock import patch

import pytest

from gobby.plans.review_evidence import PlanReviewEvidenceService
from gobby.plans.review_evidence_io import atomic_write_bytes, ensure_checkpoint
from gobby.plans.review_evidence_models import ReviewEvidenceError
from gobby.review_learning.class_recall import RetirementTaskManager
from gobby.review_learning.service import ReviewLearningMemoryManager, ReviewLearningService
from gobby.skills.loader import SkillLoader
from gobby.skills.parser import parse_skill_file
from gobby.storage.agents import LocalAgentRunManager
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.projects import LocalProjectManager
from gobby.storage.sessions import SessionManager
from tests.review_coverage_helpers import coverage_attestation
from tests.review_learning.conftest import FakeMemoryManager, FakeTaskManager

pytestmark = pytest.mark.unit

LOCAL_MACHINE_ID = "21000000-0000-4000-8000-000000000002"


@pytest.fixture(autouse=True)
def _local_machine_identity() -> Iterator[None]:
    with patch("gobby.utils.machine_id._cached_machine_id", LOCAL_MACHINE_ID):
        yield


REPO_ROOT = Path(__file__).resolve().parents[2]
SKILL_DIR = REPO_ROOT / "src/gobby/install/shared/skills/review-learning"
SKILLS_ROOT = REPO_ROOT / "src/gobby/install/shared/skills"
WORKFLOWS = REPO_ROOT / "src/gobby/install/shared/workflows/agents"
PLAN_SKILL = SKILLS_ROOT / "plan/SKILL.md"
PLAN_DRAFT_SKILL = SKILLS_ROOT / "plan-draft/SKILL.md"
PLAN_REVIEW_SKILL = SKILLS_ROOT / "plan-review/SKILL.md"


def _body() -> str:
    return (SKILL_DIR / "SKILL.md").read_text(encoding="utf-8")


def _skill_body(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _manifest_entries(stem: str) -> list[dict[str, object]]:
    return [
        {
            "title": f"Implement {name}",
            "source_section": section,
            "covers": [acceptance],
            "category": "code",
            "implementation_domain": "backend",
            "priority": 2,
            "task_type": "feature",
            "tdd": False,
            "labels": [f"covers:{stem}:{section}:{acceptance}"],
            "description": f"Implement {name}.",
            "validation_criteria": f"{name} behavior is tested.",
        }
        for name, section, acceptance in (
            ("A", "1.1", "1.1.1"),
            ("B", "1.2", "1.2.1"),
        )
    ]


def _manifest_yaml(stem: str) -> list[str]:
    lines: list[str] = []
    for entry in _manifest_entries(stem):
        covers = entry["covers"]
        labels = entry["labels"]
        assert isinstance(covers, list)
        assert isinstance(labels, list)
        lines.extend(
            [
                f"- title: {entry['title']}",
                f"  source_section: '{entry['source_section']}'",
                "  covers:",
                f"    - {covers[0]}",
                "  category: code",
                "  implementation_domain: backend",
                "  priority: 2",
                "  task_type: feature",
                "  tdd: false",
                "  labels:",
                f"    - {labels[0]}",
                f"  description: {entry['description']}",
                f"  validation_criteria: {entry['validation_criteria']}",
            ]
        )
    return lines


def _review_setup(
    temp_db: HubDatabase,
    tmp_path: Path,
    stem: str,
) -> tuple[PlanReviewEvidenceService, str, str, Path]:
    root = tmp_path / stem
    root.mkdir()
    project = LocalProjectManager(temp_db).create(name=stem, repo_path=str(root))
    session = SessionManager(temp_db).register(
        external_id=f"{stem}-parent",
        machine_id="21000000-0000-4000-8000-000000000002",
        source="codex",
        project_id=project.id,
    )
    plan_dir = root / ".gobby" / "plans"
    plan_dir.mkdir(parents=True)
    plan_path = plan_dir / "plan.md"
    plan_path.write_text(
        "\n".join(
            [
                "# Review Learning",
                f"**Plan ID:** {stem}",
                "",
                "## P1 Phase",
                "`kind: framing`",
                "",
                "### 1.1 A",
                "`kind: deliverable`",
                "",
                "Target: `src/a.py`",
                "",
                "**Acceptance:**",
                "- 1.1.1 — A works. test: `tests/test_a.py`",
                "",
                "### 1.2 B",
                "`kind: deliverable`",
                "",
                "Target: `src/b.py`",
                "",
                "**Acceptance:**",
                "- 1.2.1 — B works. test: `tests/test_b.py`",
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
                *_manifest_yaml(stem),
                "```",
                "",
            ]
        ),
        encoding="utf-8",
    )
    return PlanReviewEvidenceService(temp_db), project.id, session.id, plan_path


def _bind_run(
    service: PlanReviewEvidenceService,
    session_id: str,
    evidence_id: str,
    prompt: str,
) -> str:
    run = LocalAgentRunManager(service.db).create(
        parent_session_id=session_id,
        provider="codex",
        prompt=prompt,
    )
    service.bind_evidence_run(evidence_id, run.id)
    return run.id


def _approval(
    service: PlanReviewEvidenceService,
    evidence_id: str,
) -> dict[str, object]:
    derived = service.derive_plan_review_manifest(evidence_id, routing_decisions={})
    entries = derived["manifest_entries"]
    assert isinstance(entries, list)
    return {
        "verdict": "approved",
        "findings": [],
        "routing_decisions": {},
        "manifest_entries": entries,
        "coverage_attestation": coverage_attestation(
            evidence_id=evidence_id,
            manifest_entries=entries,
        ),
    }


def test_review_learning_skill_parses_and_is_discoverable() -> None:
    parsed = parse_skill_file(SKILL_DIR / "SKILL.md")
    skills = SkillLoader().load_directory(SKILLS_ROOT)

    assert parsed.name == "review-learning"
    assert parsed.description.startswith("Use when")
    assert "review-learning" in {skill.name for skill in skills}


def test_review_learning_skill_documents_tool_contract() -> None:
    body = _body()

    assert "gobby-review-learning" in body
    assert "recall_review_context" in body
    assert "record_review_lesson" in body
    assert "Relevant memory/lesson" in body
    assert "pattern_id" in body
    assert "principle" in body
    assert "root_cause" in body
    assert "prevention" in body
    assert "query_hints" in body
    assert "gcode search" in body
    assert "gcode grep" in body
    assert "Required for `confirmed` and `no-fix-policy`: non-empty `title` or `message`" in body
    assert "plus non-empty `principle` or `prevention`" in body
    assert "`stale` and `invalid` remain no-op decisions" in body


def test_review_learning_skill_documents_memory_only_delivery() -> None:
    body = _body()

    assert "A raw failure with no verified fix must not" in body
    assert "`stale` or `invalid`: skip recording" in body
    assert "Review learning never creates" in body
    assert "updates Gobby tasks automatically" in body
    assert "It is metadata only and causes no task or repository mutation" in body
    assert "concrete artifact, owner, implementation approach" in body
    assert "Recurrence count alone is evidence of importance" in body


def test_plan_skill_documents_parallel_review_contract() -> None:
    body = _skill_body(PLAN_SKILL)
    for phrase in (
        "plan-adversary-taskless",
        "prepare_plan_review_round",
        "bind_evidence_run",
        "coverage_attestation",
        "completed_plan_review_rounds",
        "finalization succeeds",
    ):
        assert phrase in body


def test_review_producer_hooks_reference_review_learning() -> None:
    code_reviewer = (SKILLS_ROOT / "code-reviewer/SKILL.md").read_text(encoding="utf-8")
    epic = (SKILLS_ROOT / "epic-review/SKILL.md").read_text(encoding="utf-8")
    qa_reviewer = (WORKFLOWS / "qa-reviewer.yaml").read_text(encoding="utf-8")

    assert "REQUIRED SKILL: review-learning" in code_reviewer
    assert "recall_review_context" in code_reviewer
    assert "source_kind=agent_review" in code_reviewer
    assert "REQUIRED SKILL: review-learning" in epic
    assert "source_kind=qa_rejection" in epic
    assert "review-learning" in qa_reviewer
    assert "record_review_lesson" in qa_reviewer


@pytest.mark.asyncio
async def test_plan_loop_recording_contract(monkeypatch: pytest.MonkeyPatch) -> None:
    review_learning = _body()
    plan = _skill_body(PLAN_SKILL)
    plan_review = _skill_body(PLAN_REVIEW_SKILL)
    planner = (WORKFLOWS / "planner.yaml").read_text(encoding="utf-8")

    assert "`reviewer-miss`" in review_learning
    assert "`fixer-induced-defect`" in review_learning
    assert "list_check_keys" in review_learning
    assert "plan-review:<lesson_type>:<adversary-category>:<check_key>" in review_learning
    assert "plan-review:<adversary-category>" in review_learning
    assert "participating_section_ids" in review_learning
    assert "causal_section_ids" in review_learning
    assert "The reviser records" in review_learning
    assert "recall_review_lessons_by_class" in plan_review
    assert "mandatory extra review pass" in plan_review
    assert "fixer-induced-defect" in planner
    assert "before every revision" in planner.lower()
    assert "class-aware" in plan

    monkeypatch.setattr(
        "gobby.review_learning.service._current_project_id",
        lambda: "plan-loop-project",
    )
    memories = FakeMemoryManager()
    service = ReviewLearningService(
        cast(ReviewLearningMemoryManager, memories),
        cast(RetirementTaskManager, FakeTaskManager()),
    )
    events: list[str] = []

    await service.record(
        source_kind="plan_review",
        source="seeded-plan-review",
        source_review="seed-round",
        decision="confirmed",
        finding={
            "title": "Cross-section invariants were missed",
            "lesson_type": "reviewer-miss",
            "pattern_id": "plan-review:reviewer-miss:unhandled-edge:cross-section-state",
            "check_key": "cross-section-state",
            "category": "unhandled-edge",
            "principle": "Review every section participating in one invariant",
            "prevention": "Check the complete participating section set",
            "guardrail_target": "checklist",
            "rule_id": "plan-review:unhandled-edge",
        },
        evidence={"participating_section_ids": ["1.1", "1.2"], "rounds_missed": 2},
    )
    recalled = await service.recall_review_lessons_by_class(
        "plan",
        ["reviewer-miss"],
    )
    events.append("recall")
    assert recalled["count"] == 1

    cases = [
        ("unproven", "weak-testability", [], []),
        ("cross-section", "unhandled-edge", [], ["1.1"]),
        ("dual", "bad-sequencing", ["1.1", "1.2"], ["1.1"]),
    ]
    recorded_classes: list[tuple[str, str]] = []
    for finding_id, category, participating, causal in cases:
        class_bundles = (
            ("reviewer-miss", {"participating_section_ids": participating}),
            (
                "fixer-induced-defect",
                {
                    "causal_section_ids": causal,
                    "causal_finding_id": f"{finding_id}-cause",
                    "introduced_in_round": 2,
                },
            ),
        )
        for lesson_type, evidence in class_bundles:
            section_ids = participating if lesson_type == "reviewer-miss" else causal
            if not section_ids:
                continue
            events.append("record")
            result = await service.record(
                source_kind="plan_review",
                source="interactive-approval",
                source_review="approval-round-3",
                decision="confirmed",
                finding={
                    "title": f"{finding_id} {lesson_type}",
                    "lesson_type": lesson_type,
                    "pattern_id": (
                        f"plan-review:{lesson_type}:{category}:{finding_id}-{lesson_type}"
                    ),
                    "check_key": f"{finding_id}-{lesson_type}",
                    "category": category,
                    "principle": "Preserve evidence-backed plan review lessons",
                    "prevention": "Review the proven causal section bundle",
                    "guardrail_target": "checklist",
                    "rule_id": f"plan-review:{category}",
                },
                evidence=evidence,
            )
            recorded_classes.append((finding_id, lesson_type))
            assert result["lesson_id"]

    assert events[0] == "recall"
    assert recorded_classes == [
        ("cross-section", "fixer-induced-defect"),
        ("dual", "reviewer-miss"),
        ("dual", "fixer-induced-defect"),
    ]
    recorded = memories.memories[1:]
    assert len(recorded) == 3
    assert all("lesson-domain:plan" in (memory.tags or []) for memory in recorded)
    assert all("- guardrail_target: checklist" in memory.content for memory in recorded)
    assert all("- rule_id: plan-review:" in memory.content for memory in recorded)
    assert "participating_section_ids" in recorded[1].content
    assert "causal_section_ids" in recorded[0].content


def test_interactive_approval_sequence(
    temp_db: HubDatabase,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan_contract = _skill_body(PLAN_SKILL)
    draft_contract = _skill_body(PLAN_DRAFT_SKILL)
    adversary_contract = _skill_body(PLAN_REVIEW_SKILL)
    taskless_agent = (WORKFLOWS / "plan-adversary-taskless.yaml").read_text(encoding="utf-8")
    protocol = plan_contract.split("## Interactive Review Evidence Protocol", 1)[1]
    normalized_protocol = " ".join(protocol.split())

    operation_order = [
        protocol.index("apply_plan_review_manifest"),
        protocol.index("append_plan_changelog_round"),
        protocol.index("render_plan_changelog_round"),
        protocol.index("finalize_plan_review_evidence"),
        protocol.index("checkpoint_plan_review_lesson_mint"),
    ]
    assert operation_order == sorted(operation_order)
    assert "durable pre-finalization approval intent" in normalized_protocol
    assert "pending_lesson_mint" in protocol
    assert "manifest_state=revoked" in protocol
    assert "append_plan_changelog_round" in draft_contract
    assert "preserve those bytes verbatim" in draft_contract.lower()
    assert "never writes the plan file" in adversary_contract
    assert "manifest_entries" in taskless_agent
    assert "full typed entries" in taskless_agent

    service, project_id, session_id, plan_path = _review_setup(
        temp_db,
        tmp_path,
        "approval-sequence",
    )
    rejected = service.prepare_plan_review_round(
        project_id=project_id,
        plan_path=plan_path,
        round_number=1,
        session_id=session_id,
    )
    _bind_run(service, session_id, rejected.evidence_id, "reject round")
    rejection = {
        "verdict": "needs_review",
        "coverage_attestation": coverage_attestation(
            evidence_id=rejected.evidence_id,
            shadow_valid=False,
        ),
        "findings": [
            {
                "finding_id": "round-1-miss",
                "section_id": "1.1",
                "check_key": "cross-section-state",
                "severity": "blocking",
                "category": "unhandled-edge",
                "location": "Section 1.1",
                "description": "The shared state invariant is incomplete.",
                "fix": "Define the invariant across both participating sections.",
                "principle": "Review the whole invariant",
                "prevention": "Check both participating sections",
                "participating_section_ids": ["1.1", "1.2"],
            }
        ],
    }
    rejected_checkpoint = service.render_plan_changelog_round(
        rejected.evidence_id,
        rejection,
    )
    assert ensure_checkpoint(plan_path, rejected_checkpoint)
    service.finalize_plan_review_evidence(rejected.evidence_id, rejection)

    prepared = service.prepare_plan_review_round(
        project_id=project_id,
        plan_path=plan_path,
        round_number=2,
        session_id=session_id,
    )
    run_id = _bind_run(service, session_id, prepared.evidence_id, "approve round")
    approval = _approval(service, prepared.evidence_id)
    applied = service.apply_plan_review_manifest(
        prepared.evidence_id,
        approval,
        plan_path=plan_path,
        run_id=run_id,
    )
    applied_bytes = plan_path.read_bytes()
    assert rejected_checkpoint in applied_bytes
    assert (
        service.apply_plan_review_manifest(
            prepared.evidence_id,
            approval,
            plan_path=plan_path,
            run_id=run_id,
        )
        == applied
    )
    assert plan_path.read_bytes() == applied_bytes

    plan_path.write_bytes(applied_bytes.replace(b"A works.", b"A changed after apply."))
    approval_checkpoint = service.render_plan_changelog_round(prepared.evidence_id)
    assert ensure_checkpoint(plan_path, approval_checkpoint)
    finalized = service.finalize_plan_review_evidence(prepared.evidence_id, approval)
    assert finalized.lesson_mint_status == "pending"
    proof = service.resolve_historical_proof(
        rejected.evidence_id,
        project_id=project_id,
        plan_path=plan_path,
        session_id=session_id,
    )
    assert proof.round_result == rejection
    minted = service.checkpoint_plan_review_lesson_mint(
        prepared.evidence_id,
        status="minted",
        detail={"lesson_ids": ["round-1-miss"]},
    )
    assert minted.lesson_mint_status == "minted"
    assert service.checkpoint_plan_review_lesson_mint(
        prepared.evidence_id,
        status="minted",
        detail={"lesson_ids": ["round-1-miss"]},
    ).lesson_mint_detail == {"lesson_ids": ["round-1-miss"]}

    stale_service, stale_project, stale_session, stale_path = _review_setup(
        temp_db,
        tmp_path,
        "pre-apply-drift",
    )
    stale = stale_service.prepare_plan_review_round(
        project_id=stale_project,
        plan_path=stale_path,
        round_number=1,
        session_id=stale_session,
    )
    stale_run = _bind_run(stale_service, stale_session, stale.evidence_id, "stale")
    stale_path.write_bytes(stale_path.read_bytes().replace(b"A works.", b"A drifted."))
    drifted_bytes = stale_path.read_bytes()
    with pytest.raises(ReviewEvidenceError, match="reviewed plan sections changed"):
        stale_service.apply_plan_review_manifest(
            stale.evidence_id,
            _approval(stale_service, stale.evidence_id),
            plan_path=stale_path,
            run_id=stale_run,
        )
    stale_row = stale_service.get_evidence(stale.evidence_id)
    assert stale_row.manifest_state is None
    assert stale_row.round_result is None
    assert stale_path.read_bytes() == drifted_bytes

    pending_service, pending_project, pending_session, pending_path = _review_setup(
        temp_db,
        tmp_path,
        "pending-drift",
    )
    pending = pending_service.prepare_plan_review_round(
        project_id=pending_project,
        plan_path=pending_path,
        round_number=1,
        session_id=pending_session,
    )
    pending_run = _bind_run(
        pending_service,
        pending_session,
        pending.evidence_id,
        "pending drift",
    )

    def crash_write(_path: Path, _content: bytes) -> None:
        raise OSError("simulated apply crash")

    monkeypatch.setattr("gobby.plans.review_manifest_service.atomic_write_bytes", crash_write)
    with pytest.raises(OSError, match="simulated apply crash"):
        pending_service.apply_plan_review_manifest(
            pending.evidence_id,
            _approval(pending_service, pending.evidence_id),
            plan_path=pending_path,
            run_id=pending_run,
        )
    monkeypatch.setattr(
        "gobby.plans.review_manifest_service.atomic_write_bytes",
        atomic_write_bytes,
    )
    pending_path.write_bytes(
        pending_path.read_bytes().replace(b"A works.", b"A drifted while pending.")
    )
    with pytest.raises(ReviewEvidenceError, match="reviewed plan sections changed"):
        pending_service.apply_plan_review_manifest(
            pending.evidence_id,
            _approval(pending_service, pending.evidence_id),
            plan_path=pending_path,
            run_id=pending_run,
        )
    revoked = pending_service.get_evidence(pending.evidence_id)
    assert revoked.manifest_state is None
    assert revoked.round_result is None
    LocalAgentRunManager(temp_db).cancel(pending_run)
    replacement = pending_service.prepare_plan_review_round(
        project_id=pending_project,
        plan_path=pending_path,
        round_number=2,
        session_id=pending_session,
    )
    assert replacement.evidence_id != pending.evidence_id

    recovery_service, recovery_project, recovery_session, recovery_path = _review_setup(
        temp_db,
        tmp_path,
        "pending-recovery",
    )
    recovery = recovery_service.prepare_plan_review_round(
        project_id=recovery_project,
        plan_path=recovery_path,
        round_number=1,
        session_id=recovery_session,
    )
    recovery_run = _bind_run(
        recovery_service,
        recovery_session,
        recovery.evidence_id,
        "pending recovery",
    )
    monkeypatch.setattr("gobby.plans.review_manifest_service.atomic_write_bytes", crash_write)
    with pytest.raises(OSError, match="simulated apply crash"):
        recovery_service.apply_plan_review_manifest(
            recovery.evidence_id,
            _approval(recovery_service, recovery.evidence_id),
            plan_path=recovery_path,
            run_id=recovery_run,
        )
    monkeypatch.setattr(
        "gobby.plans.review_manifest_service.atomic_write_bytes",
        atomic_write_bytes,
    )
    restarted = PlanReviewEvidenceService(temp_db)
    recovery_approval = _approval(restarted, recovery.evidence_id)
    restarted.apply_plan_review_manifest(
        recovery.evidence_id,
        recovery_approval,
        plan_path=recovery_path,
        run_id=recovery_run,
    )
    recovered_checkpoint = restarted.render_plan_changelog_round(recovery.evidence_id)
    assert ensure_checkpoint(recovery_path, recovered_checkpoint)
    restarted.finalize_plan_review_evidence(recovery.evidence_id, recovery_approval)
    with pytest.raises(ReviewEvidenceError) as pending_mint:
        restarted.prepare_plan_review_round(
            project_id=recovery_project,
            plan_path=recovery_path,
            round_number=2,
            session_id=recovery_session,
        )
    assert pending_mint.value.code == "pending_lesson_mint"
    recovered_row = restarted.get_evidence(recovery.evidence_id)
    assert recovered_row.manifest_state == "applied"
    assert recovered_row.lesson_mint_status == "pending"
    assert recovery_path.read_bytes().count(recovered_checkpoint) == 1
    restarted.checkpoint_plan_review_lesson_mint(
        recovery.evidence_id,
        status="minted",
        detail={"lesson_ids": ["recovered"]},
    )
    assert restarted.prepare_plan_review_round(
        project_id=recovery_project,
        plan_path=recovery_path,
        round_number=2,
        session_id=recovery_session,
    )
