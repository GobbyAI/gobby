"""Interactive planning routing, phase authority, evidence, and handoff contracts."""

from pathlib import Path

import pytest

from gobby.skills.capability_catalog import load_capability_catalog

pytestmark = pytest.mark.unit
REFERENCES = Path("src/gobby/install/shared/skills/gobby/references")


def _body(topic: str) -> str:
    return " ".join((REFERENCES / topic).read_text().split())


def test_plan_is_a_catalogued_capability() -> None:
    catalog = load_capability_catalog()
    assert catalog.folded_skills["plan"] == "gobby:references/plan/overview.md"
    plan = next(item for item in catalog.capabilities if item.name == "plan")
    assert {topic.name for topic in plan.topics} >= {
        "drafting",
        "enhancement",
        "review",
        "approval",
        "expansion",
        "repair",
    }


def test_plan_investigates_before_routing_by_deliverable_graph() -> None:
    body = _body("plan/overview.md")
    assert (
        "Investigate the repository and map independently closeable outcomes before choosing"
        in body
    )
    assert "One atomic outcome" in body
    assert "Multiple dependent deliverables use a plan" in body
    assert "Duration alone is not the discriminator" in body


def test_elicit_is_required_for_both_routes_and_prior_decisions_persist() -> None:
    body = _body("plan/overview.md")
    assert "Before finalizing either route, load standalone `restraint` and `elicit`" in body
    assert "grill-me method" in body
    assert "confirm the Decision Record with the user" in body
    assert "Preserve already confirmed decisions and authorization" in body


def test_atomic_route_reuses_task_and_skips_plan_lifecycle() -> None:
    body = _body("plan/overview.md")
    for term in (
        "reuse and refine an existing matching task",
        "task-ready contract",
        "scope, exact targets, acceptance criteria, verification, dependencies, research context",
        "do not create a plan artifact, planning task, manifest, registry row, or expansion run",
        "audit each proposed mechanism against restraint's ladder and its concrete consumer",
    ):
        assert term in body


def test_canonical_authority_and_restricted_staging() -> None:
    body = _body("plan/drafting.md")
    for term in (
        "`.gobby/plans/<slug>.md` as the sole authority",
        "Plan artifact:",
        "Provider display mirrors must stay synchronized",
        "never overwrite it with a stale mirror",
        "complete latest Markdown draft",
        "set_handoff(clear_session=false)",
        "Restore with argumentless `get_handoff`",
        "No scratch store, indirect writer, or subagent",
        "Materialize the complete draft once writable; only then validate/review/register/expand",
        "compaction-triggered interrupted set_handoff is the daemon boundary, not a user refusal",
    ):
        assert term in body


def test_optional_phases_and_implementation_require_distinct_authority() -> None:
    overview = _body("plan/overview.md")
    approval = _body("plan/approval.md")
    assert "optional and require their own authorization" in overview
    assert "continued drafting request does not imply implementation approval" in overview
    assert (
        "Explicit user implementation approval is required whether or not optional review ran"
        in approval
    )
    assert "Existing authorization persists within its stated scope" in approval
    assert (
        "neither a menu, drafting continuation, nor reviewer approval supplies human consent"
        in approval
    )


@pytest.mark.parametrize(
    "topic, agent",
    [
        ("review", "plan-adversary-taskless"),
        ("enhancement", "plan-enhancer-taskless"),
    ],
)
def test_taskless_launch_checkpoints_before_waiting(topic: str, agent: str) -> None:
    body = _body(f"plan/{topic}.md")
    assert agent in body
    assert "without task_id" in body
    assert "isolation none" in body
    assert "Immediately save a structured" in body
    assert "clear_session=false" in body
    assert "event-driven" in body
    if topic == "review":
        assert body.index("prepare_plan_review_round") < body.index(
            "Bind once with bind_evidence_run"
        )
        assert "expire failed launches/binds" in body


def test_waiting_uses_completion_events_and_complete_capture() -> None:
    waits = _body("sessions/waits.md")
    lifecycle = _body("agents/lifecycle.md")
    assert "wait_for_agent(run_id=...)` once" in waits
    assert "completed result can be handled immediately" in waits
    assert "yield the turn and let completion wake the session" in waits
    assert "instead of polling status or registering repeatedly" in waits
    assert "capture metadata" in lifecycle
    assert "get_agent_capture" in lifecycle
    assert "consume every page before judging the result" in lifecycle


def test_checkpoint_choices_and_active_handoff() -> None:
    draft = _body("plan/drafting.md")
    for choice in (
        "continue interactively",
        "run enhancement",
        "run adversarial review",
        "approve for implementation",
        "stop",
    ):
        assert choice in draft
    assert "After drafting, enhancement, each finalized review, and approval" in draft
    assert "Stop preserves the current authority and starts no next phase" in draft
    approval = _body("plan/approval.md")
    assert "while an enhancer or reviewer is active, mark it pending" in approval
    assert "finish the run, votes, accepted edits and checkpoints" in approval
    assert "launch no new optional round" in approval


def test_votes_precede_edits_and_rejection_checkpoint_precedes_repairs() -> None:
    review = _body("plan/review.md")
    repair = _body("plan/repair.md")
    assert (
        "Present every finding with full metadata and collect individual accept/decline votes before editing"
        in review
    )
    assert "coordinator votes with rationale" in review
    assert (
        "append canonical result, finalize, then apply accepted typed repairs and prose fixes"
        in repair
    )
    assert "missing_round_result: append the canonical round_result, not a summary" in repair
    assert "missing_v1_checkpoint: append via append_plan_changelog_round, then finalize" in repair
    assert "invalid_repair: the atomic apply leaves bytes unchanged" in repair
    assert "Never" in review
    assert "Do not hand-build V1 fences" in repair


def test_approval_uses_canonical_payload_and_finalized_round_counts() -> None:
    body = _body("plan/approval.md")
    reviewed = body.split("## Reviewed approval", 1)[1].split("## Approval without", 1)[0]
    order = [
        reviewed.index(name)
        for name in (
            "apply_plan_review_manifest",
            "append_plan_changelog_round",
            "finalize_plan_review_evidence",
            "checkpoint_plan_review_lesson_mint",
        )
    ]
    assert order == sorted(order)
    assert "complete canonical approved result, never reconstructed fields" in reviewed
    assert "Approval completes only after pending clears" in reviewed
    assert "verification section with bold round labels" in reviewed
    assert "planning_seed_state approved" in body
    assert "only finalized completed_plan_review_rounds" in body


def test_approval_without_review_does_not_fabricate_evidence() -> None:
    body = _body("plan/approval.md")
    for term in (
        "derive_plan_handoff_manifest",
        "source_plan_hash, rendered_plan_hash and manifest_digest",
        "apply_plan_handoff_manifest",
        "rejects drift before atomic write",
        "Run expansion-mode validation afterward",
        "Never synthesize reviewer verdicts",
        "never invoke a stub manifest emitter",
    ):
        assert term in body


def test_enhancement_preserves_full_suggestions_votes_and_history() -> None:
    body = _body("plan/enhancement.md")
    for term in (
        "full description and suggested_enhancement text plus every metadata field",
        "do not substitute a summary",
        "rationale for every suggestion",
        "individual accept/decline decisions before edits",
        "bold round entries",
        "kind: enhancement",
        "enhancer_run",
        "suggestions_presented",
    ):
        assert term in body


def test_unattended_build_retains_stage_native_sequence() -> None:
    body = _body("plan/overview.md")
    assert (
        "retains its stage-manifest sequence, installed review policy and configured round counts"
        in body
    )
    assert "Do not inject interactive menus into it" in body
