"""Content tests for taskless /gobby plan skill behavior."""

from pathlib import Path

import pytest

from gobby.skills.loader import SkillLoader

pytestmark = pytest.mark.unit

SHARED_CONTENT_DIR = Path(__file__).resolve().parents[2] / "src/gobby/install/shared"
SKILL_ROOT = SHARED_CONTENT_DIR / "skills"
SKILL_DIR = SKILL_ROOT / "plan"
SKILL_PATH = SKILL_DIR / "SKILL.md"
HANDOFF_INTERRUPT_WARNING = (
    "In a terminal session that call comes back as a rejected or cancelled tool use "
    "attributed to the user. That is the daemon interrupting the turn to deliver the "
    "compaction command, never a refusal: do not stop, do not ask the user about it, "
    "and resume from the continuation prompt."
)


def _normalize_prose(value: str) -> str:
    return " ".join(value.split())


@pytest.fixture(scope="module")
def body() -> str:
    reference_order = (
        "work-routing.md",
        "drafting-and-staging.md",
        "adversarial-review.md",
        "evidence-and-recovery.md",
        "build-handoff.md",
    )
    references = SKILL_PATH.parent / "references"
    return "\n\n".join(
        [
            SKILL_PATH.read_text(),
            *(references.joinpath(path).read_text() for path in reference_order),
        ]
    )


@pytest.mark.parametrize(
    ("skill_name", "expected_count"),
    [
        ("build-coordinator", 1),
        ("bridge", 1),
        ("plan", 3),
    ],
)
def test_set_handoff_interrupt_warning_is_shared_by_skills(
    skill_name: str,
    expected_count: int,
) -> None:
    skill_path = SKILL_ROOT / skill_name / "SKILL.md"
    content = skill_path.read_text()
    if skill_name == "plan":
        references = skill_path.parent / "references"
        content = "\n\n".join(
            [content, *(reference.read_text() for reference in sorted(references.glob("*.md")))]
        )
    skill_body = _normalize_prose(content)

    assert skill_body.count(HANDOFF_INTERRUPT_WARNING) == expected_count


def test_plan_skill_version(body: str) -> None:
    assert 'version: "5.0.0"' in body


def test_plan_investigates_before_routing_by_deliverable_graph(body: str) -> None:
    section = _normalize_prose(
        body[
            body.index("## Investigation, Routing, and Required Elicitation") : body.index(
                "## Work Routing"
            )
        ]
    )
    investigate = section.index("Investigate the request and repository")
    inventory = section.index("Inventory independently closeable deliverables")
    route = section.index("Route one atomic")
    elicit = section.index("Resolve every material decision")

    assert investigate < inventory < route < elicit
    for signal in (
        "independently closeable deliverable",
        "expected to fit one focused agent session",
        "multiple dependent deliverables",
        "bugs, maintenance, features, and refactors",
        "Duration is an estimate",
    ):
        assert signal in section
    assert "Do not ask the user for facts the repository can answer" in section


def test_elicit_is_mandatory_before_either_route_is_finalized(body: str) -> None:
    section = body[
        body.index("## Investigation, Routing, and Required Elicitation") : body.index(
            "## Work Routing"
        )
    ]
    normalized = " ".join(section.split())

    assert 'get_skill(name="elicit")' in section
    assert "Run its grill-me protocol before finalizing either route" in normalized
    assert "ask one material decision at a time with a recommendation" in normalized
    assert "confirmed Decision Record" in normalized
    assert "Do not ask the user for facts the repository can answer" in normalized


def test_atomic_route_uses_existing_task_workflow_and_skips_plan_lifecycle(body: str) -> None:
    section = body[body.index("## Work Routing") : body.index("## Plan Drafting and Staging")]
    normalized = " ".join(section.split())

    assert "one independently closeable deliverable" in normalized
    assert "concrete scope" in normalized
    assert "validation criteria" in normalized
    assert "existing `tasks` workflow" in normalized
    assert "real implementation task" in normalized
    assert "no plan file, plan registry row, manifest, or planning task" in normalized
    assert "Do not load `plan-draft`" in normalized
    assert "Before handoff, run a mechanism audit" in normalized
    for mechanism in (
        "new subsystem",
        "dependency",
        "abstraction",
        "configuration surface",
        "paid-operation loop",
    ):
        assert mechanism in normalized
    assert "unnecessary for complete acceptance coverage" in normalized


def test_explicit_commands_are_both_documented(body: str) -> None:
    assert "Both `$gobby plan` and `/gobby plan` invoke this workflow." in body
    assert (
        "Interactive Plan Mode also loads this skill on its first submitted prompt; "
        "route the work here after investigating the request."
    ) in _normalize_prose(body)
    assert "Plan Mode Consider prompt" not in body


def test_plan_drafting_has_one_authority_and_no_planning_tasks(body: str) -> None:
    lowered = body.lower()
    normalized = " ".join(lowered.split())

    assert "one authority" in lowered
    assert "creating task records for planning or per-round reviews" in lowered
    assert "do not create planning or per-round review tasks" in normalized
    assert (
        "any `.md` under `.gobby/`, `.claude/`, or `.codex/` (cli-owned artifact "
        "trees) is exempt from `require-task-before-edit` when the provider allows the write"
        in normalized
    )
    assert "never bypass provider write restrictions" in lowered
    assert "review-anchor" not in lowered
    assert "review anchor" not in lowered


def test_canonical_plan_body_starts_with_authoritative_artifact_path(body: str) -> None:
    drafting = (SKILL_DIR / "references" / "drafting-and-staging.md").read_text()
    normalized = " ".join(drafting.split())

    assert "every user-facing canonical plan body" in normalized
    assert "first line" in normalized
    assert "Plan artifact: `.gobby/plans/<slug>.md`" in drafting
    assert "A link outside the plan body does not satisfy this requirement" in normalized

    parsed = SkillLoader().load_skill(SKILL_DIR, validate=True)
    assert parsed.name == "plan"


def test_plan_loads_draft_methodology_and_validates_only_after_materialization(
    body: str,
) -> None:
    normalized = _normalize_prose(body)

    assert 'get_skill(name="plan-draft")' in body
    assert "uv run gobby plans validate <plan-file>" in body
    assert "A conversational draft has not passed deterministic validation" in normalized
    assert "Materialize the complete latest draft" in normalized
    assert "before starting any review" in normalized


def test_write_restricted_draft_uses_existing_structured_handoff(body: str) -> None:
    section = _normalize_prose(
        body[body.index("### Write-restricted staging") : body.index("### Draft checkpoint")]
    )

    assert "complete latest draft" in section
    assert "`current_state`" in section
    assert "Decision Record and review-stage approvals" in section
    assert "`key_decisions`" in section
    assert "unresolved material questions" in section
    assert "`notes`" in section
    assert "`next_steps`" in section
    assert "`clear_session=false`" in section
    assert "`gobby-sessions:get_handoff` with no arguments" in section
    assert "no scratch file, draft-storage tool, or periodic autosave" in section
    assert "not a validated artifact" in section
    assert "Never ask another agent or MCP tool to write around the restriction" in section
    assert HANDOFF_INTERRUPT_WARNING in section


def test_review_runs_deterministic_gate_and_bounded_mechanic_before_adversary(
    body: str,
) -> None:
    section = _normalize_prose(
        body[
            body.index("### Adversarial review phase") : body.index(
                "## Universal Checkpoint and Handoff Contract"
            )
        ]
    )

    base_gate = section.index("uv run gobby plans validate <plan-file> -p <project-root>")
    prepare = section.index("prepare_plan_review_round")

    assert base_gate < prepare
    assert "--mode expansion" not in section[:prepare]
    assert "mid-tier internal subagent" in section
    assert "plan-mechanic" in section
    assert "validator rerun-until-clean" in section
    assert "deterministic sweep report" in section
    assert "needs-planner" in section


def test_plan_review_orchestration_uses_roles_and_tiers(body: str) -> None:
    normalized = _normalize_prose(body)

    assert "planner, enhancer, and adversary" in normalized
    assert "Provider, model, and reasoning-effort choices live in user-editable agent profiles" in (
        normalized
    )
    assert "Omitted profile fields inherit provider or session defaults" in normalized
    assert "explicit profile values win" in normalized
    for model_name in ("gpt-5.6-sol", "gemini-3.1-pro", "fable"):
        assert model_name not in body


def test_selecting_plan_route_does_not_launch_optional_phases(body: str) -> None:
    drafting_intro = body[
        body.index("## Plan Drafting and Staging") : body.index("### Draft checkpoint")
    ]
    normalized = " ".join(drafting_intro.split())

    assert "Choosing the plan route authorizes investigation, elicitation, and drafting only" in (
        normalized
    )
    assert "explicit approval before enhancement or adversarial review" in normalized
    assert "The route alone never launches either optional phase or implementation" in normalized
    assert "explicit user approval remains mandatory before expansion" in _normalize_prose(body)


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
        compact = phase.index("`gobby-sessions:set_handoff`", launch)
        wait = phase.index("**Waiting on Spawned Runs**", compact)

        assert launch < compact < wait
        assert HANDOFF_INTERRUPT_WARNING in phase[compact:]


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
    assert "mandatory post-launch `gobby-sessions:set_handoff`" in normalized
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
    assert "offer both manual expansion and `gobby build`" in _normalize_prose(body)
    assert "gobby-plans:create_plan" in body
    assert "requires a real `root_task_ref`" in _normalize_prose(body)
    assert "generates the initial coverage manifest" in _normalize_prose(body)
    assert "Never create a planning task merely to obtain a registry root" in body
    assert "uv run gobby build <plan-file>" in body
    assert "--planning-seed-state approved" in body
    assert "--completed-plan-review-rounds <N>" in body
    assert "planning_seed_state=drafted" in body
    assert "planning_seed_state=needs_review" in body
    assert "planning_seed_state=approved" in body


def test_enhancement_phase_precedes_adversary_gate(body: str) -> None:
    # Constructive enhancement remains an optional stage before the adversary.
    assert "plan-enhancer-taskless" in body
    assert body.index("### Enhancement phase") < body.index("### Adversarial review phase")
    assert "before an optional adversarial review" in _normalize_prose(body)

    normalized = _normalize_prose(body)
    # Recommended, optional, and capped: accepted suggestions only, one round unless changed.
    assert "Enhancement is recommended, optional, advisory, and capped at one round" in normalized
    assert "Apply only accepted suggestions" in normalized
    assert "converged: true | false" in normalized
    # Human is the scope gate; the enhancer never gates the adversary.
    assert (
        "never let it gate, approve, reject, or block the adversary review. The human is the "
        "scope gate" in normalized
    )


def test_optional_reviews_do_not_weaken_validation_or_user_approval(body: str) -> None:
    normalized = _normalize_prose(body)

    assert "Enhancement and adversarial review are recommended and optional" in normalized
    assert "base validation is mandatory before review or approval" in normalized
    assert "explicit user approval is mandatory before expansion" in normalized
    assert "Skipping adversarial review" in normalized
    assert "derive_plan_handoff_manifest(plan_path, routing_decisions)" in normalized
    assert "apply_plan_handoff_manifest" in normalized


def test_unattended_build_keeps_stage_native_sequence(body: str) -> None:
    normalized = _normalize_prose(body)

    assert "Unattended `gobby build` keeps its existing stage-manifest sequence" in normalized
    assert "review policy and configured round counts" in normalized
    assert "Do not inject interactive menus" in normalized


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
    apply_repairs = presentation.index(
        "apply_plan_review_repairs(evidence_id, accepted_finding_ids)"
    )
    prose_fixes = presentation.index("hand-apply the accepted prose-only fixes")
    validate = presentation.index("base-validate the artifact")

    assert result < vote < checkpoint < finalize < apply_repairs < prose_fixes < validate
    assert "is idempotent and all-or-nothing" in presentation
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
        "`invalid_repair` from `apply_plan_review_repairs`: the plan is untouched",
        "hand-apply that finding's `fix`",
        "re-running `apply_plan_review_repairs` is always safe",
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
