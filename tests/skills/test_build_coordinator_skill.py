"""Operating contracts retained by the build coordination references."""

from pathlib import Path

import pytest

from gobby.skills.capability_catalog import load_capability_catalog

pytestmark = pytest.mark.unit
ROOT = Path(__file__).resolve().parents[2]
REFERENCES = ROOT / "src/gobby/install/shared/skills/gobby/references/build"


def _body(topic: str = "coordination") -> str:
    return " ".join((REFERENCES / f"{topic}.md").read_text().split())


def test_build_coordinator_skill_parses_and_is_discoverable() -> None:
    catalog = load_capability_catalog()
    assert catalog.folded_skills["build-coordinator"] == "gobby:references/build/coordination.md"
    assert not (REFERENCES.parents[2] / "build-coordinator" / "SKILL.md").exists()
    assert "coordination" in _body("overview")
    assert "Load only when the user assigns this session the coordinator role" in _body()


def test_build_coordinator_documents_interactive_e2e_validation_pattern() -> None:
    content = _body()
    for expected in (
        "target tree and a claimed coordination epic outside it",
        "without quick mode",
        "real delivery/merge evidence",
        "all required reviews/tests",
        "no running build agents or accidental claims",
        "workspace cleanup",
        "isolated test daemon/state for mutation probes",
    ):
        assert expected in content


def test_build_coordinator_forbids_changing_requirements_to_pass_e2e() -> None:
    content = _body()
    assert (
        "preserve the requested agent/provider, stage route, isolation, scope, and criteria"
        in content
    )
    assert "real user decision after practical fixes have been exhausted" in content


def test_build_coordinator_forbids_manual_dispatcher_ticks_during_unattended_e2e() -> None:
    content = _body()
    assert "Launch once" in content
    assert "Check project automation state" in content
    assert "Observe daemon-owned dispatch" in content
    assert "repeated launch/tick calls change the test and can hide a broken heartbeat" in content
    assert "explicit tick is a bounded diagnostic/recovery action" in _body("monitoring")
    assert "never proof of autonomous progress" in _body("monitoring")


def test_build_coordinator_documents_provider_neutral_automation_diagnostics() -> None:
    content = _body("monitoring")
    for expected in (
        "compare a known successful run",
        "`agent_run_id`/`session_id`",
        "SessionStart",
        "activation",
        "workflow state",
        "terminal pickup",
        "baseline dirty-file capture",
        "immutable step-workflow state",
        "Preserve the requested provider path",
        "Do not replay raw SessionStart or mutate tracking variables",
    ):
        assert expected in content


def test_build_coordinator_separates_target_and_coordination_epic() -> None:
    content = _body()
    assert "keep product work and coordination work separate" in content
    assert "coordination epic outside it" in content
    assert "Use an existing coordination epic when resuming" in content
    assert "closed automation bugs" in content


def test_build_coordinator_documents_unattended_build_discipline() -> None:
    content = _body()
    for expected in (
        "does not launch a separate coordinator agent or create a new epic",
        "coordinator session identified",
        "Each coordinator iteration checks target status",
        "dispatch explanation",
        "stages",
        "active agents",
        "history",
        "workspace health",
        "coordination blockers",
        "highest-priority actionable automation issue",
        "Intervention is evidence of an automation gap",
        "current tracker",
        "without editing their uncommitted files",
        "full status/health sweep",
    ):
        assert expected in content
    assert "timeout_seconds" not in content


def test_build_coordinator_orders_compaction_before_agent_waits() -> None:
    content = _body()
    assert "when no actionable work remains" in content.lower()
    assert "subscribe once to a running agent" in content
    assert "`gobby-agents:wait_for_agent` and yield" in content
    assert "`clear_session=false` before context degrades" in content
    assert "after a coordination fix before the next loop/wait" in content


def test_build_coordinator_documents_set_handoff_tool_path() -> None:
    content = (REFERENCES.parent / "sessions/handoffs.md").read_text()
    assert "`gobby-sessions:set_handoff`" in content
    assert "Fetch the applicable schemas" in content
    assert "Interactive sessions call `set_handoff` last" in content
    assert "Delivery starts after successful tool completion" in content
    assert "staged success is not proof" in content


def test_build_coordinator_requires_restart_after_dispatch_affecting_fixes() -> None:
    content = _body("recovery")
    for expected in (
        "dispatch, spawn, controls, stages, handoff, isolation, or startup",
        "keep affected builds blocked",
        "running daemon has the fix",
        "Record stale agents and workspace metadata",
        "Notify active sessions",
        "quiet restart window",
        "restart from the main checkout",
        "verify health",
        "save structured handoff",
        "next eligible spawn's workspace/isolation metadata",
        "commit alone does not update runtime code",
        "defects open until the affected path is verified",
    ):
        assert expected in content


def test_build_coordinator_requires_stage_normalization_and_bug_fixes() -> None:
    content = _body()
    assert "Implementation leaves normally use `development`" in content
    assert "supported lifecycle operations and applicable authorization" in content
    assert "Fix the highest-priority actionable automation issue" in content
    assert "closed automation bugs" in content
    assert "A daemon fix must be running before its path is released" in content


def test_build_coordinator_forbids_stop_hook_goal_closure_shortcut() -> None:
    content = _body()
    assert (
        "Do not close, unclaim, detach, or reparent unfinished work to satisfy a stop hook"
        in content
    )
    assert "Completion requires the target's real delivery/merge evidence" in content


def test_build_coordinator_is_generic_not_one_off() -> None:
    content = _body()
    assert "#12746" not in content
    assert "Neo4j" not in content
    assert "FalkorDB" not in content
    assert "target tree" in content
