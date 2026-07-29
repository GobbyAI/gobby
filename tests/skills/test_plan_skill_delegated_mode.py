"""Content tests for taskless /gobby plan skill behavior."""

from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

SKILL_PATH = Path(__file__).resolve().parents[2] / "src/gobby/install/shared/skills/plan/SKILL.md"


@pytest.fixture(scope="module")
def body() -> str:
    return SKILL_PATH.read_text()


def test_plan_skill_version(body: str) -> None:
    assert 'version: "3.4.0"' in body


def test_plan_investigates_before_recommending_depth(body: str) -> None:
    section = body[
        body.index("## Depth Selection and Required Elicitation") : body.index(
            "## Lightweight Workflow"
        )
    ]
    investigate = section.index("Investigate the request and repository")
    assess = section.index("Assess these complexity signals")
    recommend = section.index("Recommend **Full**")
    ask = section.index("Ask the user to choose")

    assert investigate < assess < recommend < ask
    for signal in (
        "multiple dependent deliverables or subsystems",
        "public API, schema, migration, security, or destructive-risk work",
        "material unresolved product decisions",
        "multi-agent coordination or durable handoff requirements",
        "artifact, lifecycle automation, or adversarial",
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


def test_lightweight_is_conversational_and_has_no_lifecycle_workflow(body: str) -> None:
    section = body[body.index("## Lightweight Workflow") : body.index("## Full Workflow")]
    normalized = " ".join(section.split())

    assert "Plan-Coverage Contract as formatting guidance" in normalized
    assert "conversational, decision-complete plan" in normalized
    assert (
        "no plan artifact, artifact validation, enhancement pass, adversarial review, "
        "or build handoff" in normalized
    )
    assert "End after the user receives the conversational plan" in normalized


def test_explicit_commands_are_both_documented(body: str) -> None:
    assert "Both `$gobby plan` and `/gobby plan` invoke this workflow." in body


def test_plan_is_artifact_first_and_taskless(body: str) -> None:
    lowered = body.lower()

    assert "artifact-first" in lowered
    assert "does not create a planning" in lowered
    assert "review-anchor task" in lowered
    assert "per-round review tasks" in lowered
    assert "do not create or claim tasks" in lowered
    assert "do not create review anchors" in lowered


def test_full_plan_loads_draft_methodology_and_validates_before_review(body: str) -> None:
    assert 'get_skill(name="plan-draft")' in body
    assert "uv run gobby plans validate <plan-file>" in body
    assert "approve the validated draft for the enhancement phase" in body
    assert "Ask for separate approval before starting adversarial" in body


def test_selecting_full_does_not_launch_later_phases(body: str) -> None:
    full_intro = body[body.index("## Full Workflow") : body.index("**Step 4.5")]
    normalized = " ".join(full_intro.split())

    assert "Choosing Full authorizes investigation, elicitation, and drafting only" in normalized
    assert (
        "explicit approvals described below before enhancement, adversarial review, "
        "or build handoff" in normalized
    )
    assert "Selecting Full alone never launches any of those phases" in normalized
    assert "Offer build handoff as an optional final step" in body
    assert "explicitly approves the handoff" in body


def test_review_spawn_uses_taskless_adversary_without_task_id(body: str) -> None:
    assert "plan-adversary-taskless" in body
    assert "without `task_id`" in body
    assert 'isolation="none"' in body
    assert "artifact_path" in body
    assert "round_number" in body
    assert "max_review_rounds" in body


def test_plan_compacts_after_every_review_agent_launch(body: str) -> None:
    enhancer_launch = body.index("Spawn `plan-enhancer-taskless`")
    enhancer_wait = body.index("Wait as described in **Waiting on Spawned Runs**", enhancer_launch)
    enhancer_handoff = body[enhancer_launch:enhancer_wait]
    assert "gobby-sessions:compact_self" in enhancer_handoff
    assert "every enhancement round" in enhancer_handoff

    adversary_launch = body.index("spawn `plan-adversary-taskless`")
    adversary_wait = body.index(
        "Wait as described in **Waiting on Spawned Runs**", adversary_launch
    )
    adversary_handoff = body[adversary_launch:adversary_wait]
    assert "gobby-sessions:compact_self" in adversary_handoff
    assert "every adversarial review round" in adversary_handoff


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
    assert "get_agent_result(run_id)` only if" in normalized
    assert "mandatory post-launch `gobby-sessions:compact_self`" in normalized
    assert "already known to be terminal skips subscribing and waiting" in normalized
    assert "timeout_seconds" not in section
    assert "background watcher" not in section
    assert "/loop" not in section
    assert "/schedule" not in section


def test_review_timeout_restarts_same_display_round_from_fresh_evidence(body: str) -> None:
    normalized = " ".join(body.split())

    assert "`inconclusive` with reason code `timeout` and `timeout_seconds: 2700`" in normalized
    assert "Do not reuse partial lane output or create a timeout checkpoint" in normalized
    assert "Call `expire_plan_review_evidence`" in normalized
    assert (
        "retry the same display round from a fresh snapshot, inventory, and index token"
        in normalized
    )
    assert "sole timeout recovery path" in normalized


def test_waiting_steps_redirect_to_shared_policy(body: str) -> None:
    assert body.count("Wait as described in **Waiting on Spawned Runs**") == 2


def test_review_history_uses_v1_changelog_verification_entries(body: str) -> None:
    assert "## V1 Plan Changelog" in body
    assert "`kind: verification`" in body
    for field in (
        "reviewer_run",
        "reviewer_session",
        "verdict: approved | needs_review | needs_requirements",
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
    assert "Step 4.5" in body
    assert "plan-enhancer-taskless" in body
    assert "max_enhancement_rounds" in body
    lowered = body.lower()
    # Human is the scope gate; enhancer never gates.
    assert "present-and-stop" in lowered
    assert "scope gate" in lowered
    # Advisory: accepted suggestions only, stop on convergence/decline/cap.
    assert "accepted" in lowered
    assert "converged" in lowered
    assert "never gate" in lowered


def test_enhancement_presentation_contract(body: str) -> None:
    start = body.index("3. Wait as described in **Waiting on Spawned Runs**")
    end = body.index("4. Apply only the **accepted** suggestions", start)
    presentation = " ".join(body[start:end].split())

    assert "ranked summary of every suggestion" in presentation
    assert "`id`, lens, location, impact, effort, risk, and a one-line gist" in presentation
    assert "full text verbatim" in presentation
    assert "description, suggested enhancement, and metadata" in presentation
    assert "quote the current plan sections" in presentation
    assert "individual accept/decline vote for each suggestion" in presentation
    assert "per-item exploration before recording its vote" in presentation
    assert "interaction payload" in presentation
    assert "full item text inside that payload" in presentation
    assert "outside tool calls is not guaranteed to render" in presentation
    assert "`kind: deferred`" in presentation
    assert "follow-up task" in presentation


def test_adversary_presentation_contract(body: str) -> None:
    start = body.index("6. Wait as described in **Waiting on Spawned Runs**")
    end = body.index("7. If the verdict is `needs_review`", start)
    presentation = " ".join(body[start:end].split())

    result = presentation.index("Read the run result")
    changelog = presentation.index("append a `## V1 Plan Changelog` entry")
    vote_gate = presentation.index("ranked summary of every finding")
    assert result < changelog < vote_gate
    assert "`id`, severity, location, impact, effort, risk, and a one-line gist" in presentation
    assert "full text verbatim" in presentation
    assert "description, finding detail, and metadata" in presentation
    assert "quote the current plan sections" in presentation
    assert "individual accept/decline vote for each finding" in presentation
    assert "per-item exploration before recording its vote" in presentation
    assert "interaction payload" in presentation
    assert "full item text inside that payload" in presentation
    assert "outside tool calls is not guaranteed to render" in presentation
    assert "`kind: deferred`" in presentation
    assert "follow-up task" in presentation


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
