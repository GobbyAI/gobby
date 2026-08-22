"""Content tests for taskless /gobby plan skill behavior."""

from pathlib import Path

import pytest

from gobby.sessions.compact_markers import (
    COMPACT_SELF_CONTINUE_INTRO,
    COMPACT_SELF_INTERRUPT_WARNING,
)
from gobby.skills.loader import SkillLoader

pytestmark = pytest.mark.unit

SHARED_CONTENT_DIR = Path(__file__).resolve().parents[2] / "src/gobby/install/shared"
SKILL_ROOT = SHARED_CONTENT_DIR / "skills"
SKILL_DIR = SKILL_ROOT / "plan"
SKILL_PATH = SKILL_DIR / "SKILL.md"
AUTO_COMPACT_RULE_PATH = (
    SHARED_CONTENT_DIR / "workflows/rules/context-handoff/auto-compact-after-task-close.yaml"
)


def _normalize_prose(value: str) -> str:
    return " ".join(value.split())


@pytest.fixture(scope="module")
def body() -> str:
    return SKILL_PATH.read_text()


def test_compact_self_interrupt_warning_is_canonical() -> None:
    assert COMPACT_SELF_INTERRUPT_WARNING == (
        "In a terminal session that call comes back as a rejected or cancelled tool use "
        "attributed to the user. That is the daemon interrupting the turn to deliver the "
        "compaction command, never a refusal: do not stop, do not ask the user about it, "
        "and resume from the continuation prompt."
    )


@pytest.mark.parametrize(
    ("skill_name", "expected_count"),
    [
        ("goal", 1),
        ("build-coordinator", 1),
        ("bridge", 1),
        ("plan", 2),
    ],
)
def test_compact_self_interrupt_warning_is_shared_by_skills(
    skill_name: str,
    expected_count: int,
) -> None:
    skill_body = _normalize_prose((SKILL_ROOT / skill_name / "SKILL.md").read_text())

    assert skill_body.count(COMPACT_SELF_INTERRUPT_WARNING) == expected_count


def test_compact_self_interrupt_warning_is_shared_by_runtime_surfaces() -> None:
    rule_body = _normalize_prose(AUTO_COMPACT_RULE_PATH.read_text())
    intro = _normalize_prose(COMPACT_SELF_CONTINUE_INTRO)

    assert COMPACT_SELF_INTERRUPT_WARNING in rule_body
    assert COMPACT_SELF_INTERRUPT_WARNING in intro


def test_plan_skill_version(body: str) -> None:
    assert 'version: "3.9.0"' in body


def test_plan_investigates_before_recommending_depth(body: str) -> None:
    section = _normalize_prose(
        body[
            body.index("## Depth Selection and Required Elicitation") : body.index(
                "## Lightweight Workflow"
            )
        ]
    )
    investigate = section.index("Investigate the request and repository")
    classify = section.index("Determine whether the proposed implementation is a major change")
    recommend = section.index("Recommend **Full** only for a major change")
    ask = section.index("Ask the user to choose")

    assert investigate < classify < recommend < ask
    for signal in (
        "subsystem redesign or rework",
        "complex new feature with multiple dependent deliverables",
        "broad migration or architecture/security-model change",
        "bug fixes, maintenance, localized features or refactors",
        "none independently makes a change major",
    ):
        assert signal in section
    assert "honor that choice without asking again" in section


def test_elicit_is_mandatory_for_both_depths(body: str) -> None:
    section = body[
        body.index("## Depth Selection and Required Elicitation") : body.index(
            "## Lightweight Workflow"
        )
    ]
    normalized = " ".join(section.split())

    assert 'get_skill(name="elicit")' in section
    assert "Run its grill-me protocol in both depths" in normalized
    assert "ask one material decision at a time with a recommendation" in normalized
    assert "confirmed Decision Record before drafting either plan" in normalized
    assert "Do not ask the user for facts the repository can answer" in normalized


def test_lightweight_writes_a_validated_artifact_and_skips_the_full_phases(body: str) -> None:
    """a2b779f60 (#19368) gave Lightweight a real artifact.

    It previously produced a conversational plan with no artifact and no
    validation; it now drafts `.gobby/plans/<slug>.md` and base-validates it,
    while still skipping enhancement and adversarial review by default.
    """
    section = body[body.index("## Lightweight Workflow") : body.index("## Full Workflow")]
    normalized = " ".join(section.split())

    assert "Plan-Coverage Contract as formatting guidance" in normalized
    assert "decision-complete plan to `.gobby/plans/<slug>.md`" in normalized
    assert "uv run gobby plans validate <plan-file>" in normalized
    assert "Lightweight skips enhancement and adversarial review by default" in normalized
    assert "opt into either Full phase later without redrafting" in normalized


def test_explicit_commands_are_both_documented(body: str) -> None:
    assert "Both `$gobby plan` and `/gobby plan` invoke this workflow." in body


def test_plan_is_artifact_first_and_taskless(body: str) -> None:
    lowered = body.lower()
    normalized = " ".join(lowered.split())

    assert "artifact-first" in lowered
    assert "creating task records for planning or per-round reviews" in lowered
    assert "do not create or claim tasks" in lowered
    assert (
        "any `.md` under `.gobby/`, `.claude/`, or `.codex/` (cli-owned artifact "
        "trees) is exempt from `require-task-before-edit`" in normalized
    )
    assert "review-anchor" not in lowered
    assert "review anchor" not in lowered


def test_full_plan_body_starts_with_authoritative_artifact_path(body: str) -> None:
    full_workflow = body[body.index("## Full Workflow") : body.index("## Boundaries")]
    normalized = " ".join(full_workflow.split())

    assert "every user-facing Full plan body" in normalized
    assert "first line" in normalized
    assert "Plan artifact: `.gobby/plans/<slug>.md`" in full_workflow
    assert "A link outside the plan body does not satisfy this requirement" in normalized

    parsed = SkillLoader().load_skill(SKILL_DIR, validate=True)
    assert parsed.name == "plan"


def test_full_plan_loads_draft_methodology_and_validates_before_review(body: str) -> None:
    normalized = _normalize_prose(body)

    assert 'get_skill(name="plan-draft")' in body
    assert "uv run gobby plans validate <plan-file>" in body
    assert "ask separately whether to run enhancement" in normalized
    assert "Declining enhancement does not imply adversarial-review approval" in normalized
    assert "ask separately whether to begin adversarial review" in normalized


def test_selecting_full_does_not_launch_later_phases(body: str) -> None:
    full_intro = body[body.index("## Full Workflow") : body.index("### Draft checkpoint")]
    normalized = " ".join(full_intro.split())

    assert "Choosing Full authorizes investigation, elicitation, and drafting only" in normalized
    assert (
        "explicit approvals described below before enhancement, adversarial review, "
        "or build handoff" in normalized
    )
    assert "Selecting Full alone never launches any of those phases" in normalized
    # Handoff stays an explicitly approved menu choice, never an automatic step.
    assert "`hand off to build`" in body
    assert (
        "explicit human approval to skip all remaining enhancement and adversarial rounds"
        in _normalize_prose(body)
    )


def test_review_spawn_uses_taskless_adversary_without_task_id(body: str) -> None:
    assert "plan-adversary-taskless" in body
    assert "without `task_id`" in body
    assert 'isolation="none"' in body
    assert "artifact_path" in body
    assert "round_number" in body
    # The round cap is passed through, but 3.7.0 names it inline rather than
    # exposing a `max_review_rounds` variable (a2b779f60, #19368).
    assert "cap, and parent session id" in body


def test_plan_compacts_after_every_review_agent_launch(body: str) -> None:
    """Each taskless launch compacts before it starts waiting.

    The compaction call must carry the shared interrupt warning, or the
    coordinator reads the daemon's interrupt as a user refusal and stops.
    """
    enhancement = _normalize_prose(
        body[body.index("### Enhancement phase") : body.index("### Adversarial review phase")]
    )
    adversary = _normalize_prose(
        body[
            body.index("### Adversarial review phase") : body.index(
                "## Universal Checkpoint and Handoff Contract"
            )
        ]
    )

    for phase, agent in (
        (enhancement, "plan-enhancer-taskless"),
        (adversary, "plan-adversary-taskless"),
    ):
        launch = phase.index(f"`{agent}` without `task_id`")
        compact = phase.index("`gobby-sessions:compact_self`", launch)
        wait = phase.index("**Waiting on Spawned Runs**", compact)

        assert launch < compact < wait
        assert COMPACT_SELF_INTERRUPT_WARNING in phase[compact:]


def test_spawned_run_waiting_policy_is_shared_and_wake_driven(body: str) -> None:
    section = body[body.index("## Waiting on Spawned Runs") : body.index("## Changelog Contract")]
    normalized = " ".join(section.split())

    independent_work = section.index("Keep doing useful independent work")
    subscribe = section.index("wait_for_agent(run_id)")
    end_turn = section.index("end the turn")
    terminal_snapshot = section.index("re-call `gobby-agents:wait_for_agent(run_id)`")
    status_sweep = section.index("full status and health sweep")
    assert independent_work < subscribe < end_turn < terminal_snapshot < status_sweep

    assert "subscribe once by calling `gobby-agents:wait_for_agent(run_id)`" in normalized
    assert "daemon wake" in normalized
    assert "custom foreground poll" in normalized
    assert "direct agent-run API polling" in normalized
    assert "Bash sleep heartbeat" in normalized
    assert "only supported resume mechanism" in normalized
    assert "get_agent_result(run_id)` only if" in normalized
    assert "mandatory post-launch `gobby-sessions:compact_self`" in normalized
    assert "already known to be terminal skips subscribing and waiting" in normalized
    assert "timeout_seconds" not in section
    assert "background watcher" not in section
    assert "/loop" not in section
    assert "/schedule" not in section


def test_interactive_review_does_not_require_session_marker(body: str) -> None:
    section = body[
        body.index("## Interactive Review Evidence Protocol") : body.index(
            "## Waiting on Spawned Runs"
        )
    ]
    assert "`request_user_input`" in section
    assert "waiting_on_user_input" not in section
    assert "set_variable" not in section


def test_waiting_steps_redirect_to_shared_policy(body: str) -> None:
    assert _normalize_prose(body).count("then use **Waiting on Spawned Runs**") == 2


def test_review_history_uses_v1_changelog_verification_entries(body: str) -> None:
    assert "## V1 Plan Changelog" in body
    assert "`kind: verification`" in body
    for field in (
        "reviewer_run",
        "reviewer_session",
        "verdict: approved | needs_review",
        "findings",
        "resolution_notes",
    ):
        assert field in body
    assert "Keep prior rounds" in body


def test_build_handoff_uses_manifest_and_seed_flags(body: str) -> None:
    assert "## M1 Task Manifest" in body
    assert "uv run gobby plans validate <plan-file> --mode expansion" in body
    assert "uv run gobby build <plan-file>" in body
    assert "--planning-seed-state approved" in body
    assert "--completed-plan-review-rounds <N>" in body
    assert "planning_seed_state=drafted" in body
    assert "planning_seed_state=needs_review" in body
    assert "planning_seed_state=approved" in body


def test_enhancement_phase_precedes_adversary_gate(body: str) -> None:
    # Step 4.5: constructive enhancement runs after approval, before the adversary.
    assert "step 4.5" in body
    assert "plan-enhancer-taskless" in body
    assert body.index("### Enhancement phase") < body.index("### Adversarial review phase")
    assert "after enhancement approval and before the adversary gate" in _normalize_prose(body)

    normalized = _normalize_prose(body)
    # Advisory and capped: accepted suggestions only, one round unless changed.
    assert "Enhancement is advisory, default-on for Full, and capped at one round" in normalized
    assert "Apply only accepted suggestions" in normalized
    assert "converged: true | false" in normalized
    # Human is the scope gate; the enhancer never gates the adversary.
    assert (
        "never let it gate, approve, reject, or block the adversary review. The human is the "
        "scope gate" in normalized
    )


def test_enhancement_presentation_contract(body: str) -> None:
    """Every suggestion is presented in full and voted on before any edit.

    a2b779f60 (#19368) compressed the itemized presentation checklist, but the
    vote-before-edit gate and the unattended-mode rationale record survive.
    """
    presentation = _normalize_prose(
        body[body.index("### Enhancement phase") : body.index("### Adversarial review phase")]
    )

    present = presentation.index("Present every suggestion with its full text and metadata")
    vote = presentation.index("Collect one accept/decline vote per suggestion before editing")
    apply_accepted = presentation.index("Apply only accepted suggestions")

    assert present < vote < apply_accepted
    assert "append the enhancement changelog entry, and base-validate" in presentation
    assert (
        "In unattended mode, the coordinator judges every item and records each vote with its "
        "rationale" in presentation
    )


def test_adversary_presentation_contract(body: str) -> None:
    """Findings are presented and voted, then checkpointed before repairs."""
    presentation = _normalize_prose(
        body[
            body.index("### Adversarial review phase") : body.index(
                "## Universal Checkpoint and Handoff Contract"
            )
        ]
    )

    result = presentation.index("Read the canonical result")
    vote = presentation.index("collect one accept/decline vote per finding before editing")
    checkpoint = presentation.index("append_plan_changelog_round(evidence_id, prose, round_result)")
    finalize = presentation.index("finalize_plan_review_evidence(evidence_id, round_result)")
    apply_repairs = presentation.index("apply accepted repairs")
    validate = presentation.index("base-validate the artifact")

    assert result < vote < checkpoint < finalize < apply_repairs < validate
    assert "Present every finding with its full text and metadata" in presentation
    assert "Record declined items and deferrals explicitly" in presentation
    assert "canonical payload verbatim as `round_result` to both calls" in presentation
    assert (
        "fails with `missing_round_result` unless a durable intent already exists" in presentation
    )
    assert (
        "In unattended mode, the coordinator judges every item and records each vote with its "
        "rationale" in presentation
    )
    # Only finalized rounds count toward the cap.
    assert (
        "Increment `completed_plan_review_rounds` only when finalization succeeds" in presentation
    )
    for recovery_detail in (
        "### Recovery",
        "`missing_round_result` or `stale_plan_evidence` from `append_plan_changelog_round`",
        "re-call it with the canonical `round_result`",
        "`missing_v1_checkpoint` from `finalize_plan_review_evidence`",
        "call `append_plan_changelog_round` with the canonical payload, then finalize",
        "Never hand-build the fence",
    ):
        assert recovery_detail in presentation
    for stale_detail in (
        "Recovery: repairs applied before the checkpoint",
        "byte-identically",
        "get_plan_review_snapshot(evidence_id)",
    ):
        assert stale_detail not in presentation

    evidence_protocol = _normalize_prose(
        body[
            body.index("## Interactive Review Evidence Protocol") : body.index(
                "## Waiting on Spawned Runs"
            )
        ]
    )
    assert "rejection-round freshness gate" not in evidence_protocol
    assert "Rejection rounds have no freshness gate" in evidence_protocol
    assert (
        "`append_plan_changelog_round` verifies reviewed bytes only for approved payloads"
        in evidence_protocol
    )
    assert (
        "The approval freshness gate lives in `apply_plan_review_manifest` and exists only in "
        "step 1" in evidence_protocol
    )


def test_enhancement_changelog_uses_kind_enhancement(body: str) -> None:
    assert "`kind: enhancement`" in body
    assert "enhancer_run" in body
    assert "suggestions_presented" in body
    # Round entries are bold labels; an actual `### Round` heading (line-start)
    # fails plan validation, so the changelog examples must never use one.
    assert "\n### Round" not in body
    assert "**Round <N>**" in body


def test_old_anchor_workflow_is_absent(body: str) -> None:
    for forbidden in (
        "active_anchor_id",
        'task_type="review_anchor"',
        "submit_for_review(task_id=anchor.id",
        "start_stage(task_id=anchor.id",
    ):
        assert forbidden not in body
