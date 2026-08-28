"""Contract tests for bundled planning and adversarial-review prompts."""

from pathlib import Path

import pytest

from gobby.skills.parser import parse_skill_file

pytestmark = pytest.mark.unit

SKILL_PATH = Path("src/gobby/install/shared/skills/plan-review/SKILL.md")
PLAN_SKILL_PATH = Path("src/gobby/install/shared/skills/plan/SKILL.md")
TASKLESS_AGENT_PATH = Path("src/gobby/install/shared/workflows/agents/plan-adversary-taskless.yaml")
STAGED_AGENT_PATH = Path("src/gobby/install/shared/workflows/agents/plan-adversary.yaml")


def _normalized(path: Path) -> str:
    content = path.read_text(encoding="utf-8")
    if path in {SKILL_PATH, PLAN_SKILL_PATH}:
        references = path.parent / "references"
        content = "\n\n".join(
            [content, *(reference.read_text() for reference in sorted(references.glob("*.md")))]
        )
    return " ".join(content.split())


def test_plan_review_frontmatter_parses() -> None:
    parsed = parse_skill_file(SKILL_PATH)
    assert parsed.name == "plan-review"
    assert parsed.metadata is not None
    assert parsed.metadata["gobby"]["audience"] == "all"


def test_plan_review_uses_complete_snapshot_and_three_lanes() -> None:
    body = _normalized(SKILL_PATH)

    assert "Call `get_plan_review_snapshot(evidence_id)` once" in body
    for lane in (
        "requirements_traceability",
        "repository_blast_radius",
        "runtime_invariants",
    ):
        assert lane in body
    assert "Run the three lanes concurrently" in body
    assert "candidate_dispositions" in body
    assert "shadow-manifest status" in body
    assert "coverage_attestation" in body


def test_plan_review_finding_and_verdict_vocabulary() -> None:
    body = _normalized(SKILL_PATH)

    assert "**severity** — `blocking` or `nit`" in body
    assert "**fix** — one short paragraph" in body
    assert "`verdict: approved` or `verdict: needs_review`" in body
    assert "non-blocking nits never trigger escalation" in body.lower()


def test_plan_review_protocol_failure_omits_verdict() -> None:
    body = _normalized(SKILL_PATH)

    assert "When `validate_plan_review_coverage` succeeds" in body
    assert "A protocol-failure terminal result" in body
    assert "omits `verdict`" in body
    assert "exact tool error" in body
    assert "draft findings" in body


def test_plan_review_resolves_symbol_targets_before_blast_radius() -> None:
    body = _normalized(SKILL_PATH)

    assert "symbol_validation.status: passed" in body
    assert "exact file-qualified Target first" in body
    exact_position = body.index("exact file-qualified Target first")
    usages_position = body.index("`gcode usages`", exact_position)
    blast_position = body.index("`gcode blast-radius`", exact_position)
    assert exact_position < usages_position
    assert exact_position < blast_position
    assert "regardless of category" in body


def test_review_prompts_have_direct_repository_and_task_access() -> None:
    skill = _normalized(SKILL_PATH)
    taskless = _normalized(TASKLESS_AGENT_PATH)
    staged = _normalized(STAGED_AGENT_PATH)

    assert "read directly from the repository and Gobby tasks" in skill
    assert "gobby-tasks:get_task" in taskless
    assert "gobby-tasks:get_task" in staged
    assert "never edit the plan file" in staged.lower()
    assert "never edit it" in taskless.lower()


def test_interactive_plan_always_writes_and_validates_canonical_artifact() -> None:
    body = _normalized(PLAN_SKILL_PATH)

    assert "Lightweight skips enhancement and adversarial review by default" in body
    assert "Write the decision-complete plan to `.gobby/plans/<slug>.md`" in body
    assert "Full depth is artifact-first" in body
    assert "uv run gobby plans validate <plan-file>" in body


def test_interactive_checkpoint_and_handoff_contract() -> None:
    body = _normalized(PLAN_SKILL_PATH)

    for choice in ("`continue interactively`", "`hand off to build`", "`stop`"):
        assert choice in body
    assert "After drafting, enhancement, every finalized adversary round" in body
    assert "During elicitation or drafting" in body
    assert "If handoff is requested while an enhancer or adversary is active" in body
    assert "without launching another enhancement or adversary round" in body
    assert "planning_seed_state=approved" in body
    assert "completed_plan_review_rounds` only when finalization succeeds" in body
    assert "derive_plan_handoff_manifest(plan_path, routing_decisions)" in body
    assert "apply_plan_handoff_manifest" in body
    assert "Never invoke `emit_stub_manifest`" in body


def test_capped_review_processes_final_findings_before_human_handoff() -> None:
    body = _normalized(PLAN_SKILL_PATH)

    cap = body.index("reaches the configured review cap")
    vote = body.index("process every final finding and vote", cap)
    finalize = body.index("finalize the normal rejection checkpoint", vote)
    handoff = body.index("explicit human-handoff tools", finalize)
    assert cap < vote < finalize < handoff
    assert "Do not launch another adversary round" in body[cap:handoff]
    assert "never manufactures an adversary verdict" in body
    assert "`coverage_attestation`" in body


def test_interactive_phase_approvals_and_item_voting_remain_separate() -> None:
    body = _normalized(PLAN_SKILL_PATH)

    assert "Start it only after explicit enhancement approval" in body
    assert "Start only after explicit adversarial-review approval" in body
    assert "one accept/decline vote per suggestion" in body
    assert "one accept/decline vote per finding" in body


def test_repair_class_section() -> None:
    parsed = parse_skill_file(SKILL_PATH)
    assert parsed.version == "1.5.0"
    text = _normalized(SKILL_PATH)

    assert "### Repair class vs design class" in text
    assert "| `traceability` | `add_targets`, `add_acceptance` |" in text
    assert "| `bad-sequencing` | `add_dependency` |" in text
    assert "| `weak-testability` | `add_acceptance` |" in text
    assert "| `gobby-format` | `add_targets`, `add_dependency`, `add_acceptance` |" in text
    assert "`missing-requirement`, `unhandled-edge`, `over-engineering` | none" in text
    assert "a repair satisfies the reviewer's own check" in text
    assert "fresh reviewer re-runs that check" in text
    assert "Design-class repairs never ride on `repairs`" in text
    assert "`apply_plan_review_repairs` is coordinator-only" in text
    assert "kind: add_targets" in text and "kind: add_acceptance" in text
