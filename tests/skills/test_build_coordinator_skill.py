"""Contract tests for the bundled build-coordinator skill."""

from __future__ import annotations

from pathlib import Path

import pytest

from gobby.skills.loader import SkillLoader
from gobby.skills.parser import parse_skill_file

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[2]
SKILL_DIR = REPO_ROOT / "src/gobby/install/shared/skills/build-coordinator"
SKILLS_ROOT = REPO_ROOT / "src/gobby/install/shared/skills"
HANDOFF_INTERRUPT_WARNING = (
    "In a terminal session that call comes back as a rejected or cancelled tool use "
    "attributed to the user. That is the daemon interrupting the turn to deliver the "
    "compaction command, never a refusal: do not stop, do not ask the user about it, "
    "and resume from the continuation prompt."
)


def _body() -> str:
    return (SKILL_DIR / "SKILL.md").read_text(encoding="utf-8")


def _normalized_body() -> str:
    return " ".join(_body().split())


def test_build_coordinator_skill_parses_and_is_discoverable() -> None:
    parsed = parse_skill_file(SKILL_DIR / "SKILL.md")
    skills = SkillLoader().load_directory(SKILLS_ROOT)

    assert parsed.name == "build-coordinator"
    assert "coordinator" in parsed.description.lower()
    assert "gobby build" in parsed.description.lower()
    assert "build-coordinator" in {skill.name for skill in skills}


def test_build_coordinator_documents_interactive_e2e_validation_pattern() -> None:
    content = _body()

    assert "coordinator/tracking epic" in content
    assert "automation target" in content
    assert "without `--quick`" in content
    assert "real merge SHA" in content
    assert "no agents are running" in content
    assert "no tasks remain claimed" in content
    assert "no stale build worktrees or clones" in content
    assert "root `README.md`" in content


def test_build_coordinator_forbids_changing_requirements_to_pass_e2e() -> None:
    content = _body()

    assert "Do not make the test pass by changing the required agent" in content
    assert "provider" in content
    assert "lifecycle route" in content
    assert "task scope" in content
    assert "acceptance criteria" in content
    assert "preserving the requested path" in content
    assert "extreme edge case" in content
    assert "exhausting practical fixes" in content


def test_build_coordinator_forbids_manual_dispatcher_ticks_during_unattended_e2e() -> None:
    content = _body()

    assert "project build automation is enabled" in content
    assert "run `gobby build resume` once" in content
    assert "launch `gobby build #epic ...` once" in content
    assert "daemon-owned automation" in content
    assert "manual dispatcher ticks" in content
    assert "anti-pattern" in content
    assert "can hide a broken dispatcher loop" in content
    assert "If project automation is paused, use `gobby build resume`" in content
    assert "bounded explicit tick" in content
    assert "only as a diagnostic or recovery step" in content


def test_build_coordinator_documents_provider_neutral_automation_diagnostics() -> None:
    content = _body()

    assert "Automation Debugging Pattern" in content
    assert "Compare against the last known successful run" in content
    assert "SessionStart activation completed" in content
    assert "first provider-neutral prompt event" in content
    assert "ensure_session_activation(session_id)" in content
    assert "Do not replay the raw SessionStart hook wholesale" in content
    assert "OpenTelemetry" in content
    assert "agent_run_id" in content
    assert "session_id" in content


def test_build_coordinator_separates_target_and_coordination_epic() -> None:
    body = _body()

    assert "separate coordination epic outside the target task tree" in body
    assert "Target task or epic: the user's product work" in body
    assert "Coordination epic: build coordination" in body
    assert "Do not close the target task or epic" in body
    assert "all discovered `gobby build` bugs from the run are closed" in body


def test_build_coordinator_documents_unattended_build_discipline() -> None:
    body = _body()
    normalized = _normalized_body()

    assert "coordinator intervention as evidence" in body
    assert "current coordinator session" in body
    assert "Do not create or switch to a separate agent definition" in normalized
    assert "$gobby build-coordinator <target-ref>" in body
    assert "/gobby build-coordinator" in body
    assert "without `--quick`" in body
    assert "manual-ticking the dispatcher" in body
    assert "daemon-owned automation" in body
    assert "Monitor dispatch directly" in body
    assert "Check target build state, dispatch eligibility, stage state" in body
    assert "Check the coordination epic's child tasks" in body
    assert "Work the highest-priority actionable coordination bug" in body
    assert "when you have not compacted recently" in body
    assert "after completing a coordination bug task" in body
    assert "gobby-agents:wait_for_agent" in body
    assert (
        "the last idle action only when agents are running and no actionable work remains"
        in normalized
    )
    assert "subscribe once by calling" in normalized
    assert "end the turn" in normalized
    assert "daemon wake" in normalized
    assert "re-call `gobby-agents:wait_for_agent`" in normalized
    assert "full status and health sweep" in normalized
    assert "timeout_seconds" not in body


def test_build_coordinator_orders_compaction_before_agent_waits() -> None:
    normalized = _normalized_body()

    monitor_idx = normalized.index("Check target build state, dispatch eligibility")
    bugs_idx = normalized.index("Work the highest-priority actionable coordination bug")
    compact_idx = normalized.index("Use `gobby-sessions:set_handoff` when context pressure")
    wait_idx = normalized.index("Use `gobby-agents:wait_for_agent` as the last idle action")

    assert monitor_idx < bugs_idx < compact_idx < wait_idx
    assert (
        "Use `gobby-agents:wait_for_agent` as the last idle action only when agents are running "
        "and no actionable work remains" in normalized
    )
    assert "subscribe once by calling" in normalized
    assert "end the turn" in normalized


def test_build_coordinator_documents_set_handoff_tool_path() -> None:
    body = _body()
    normalized = _normalized_body()

    assert "gobby-sessions:set_handoff" in body
    assert 'list_tools(server_name="gobby-sessions")' not in body
    assert 'get_tool_schema(server_name="gobby-sessions", tool_name="set_handoff")' in body
    assert 'tool_name="set_handoff"' in body
    assert "top-level `call_tool.session_id`" in body
    assert HANDOFF_INTERRUPT_WARNING in normalized


def test_build_coordinator_requires_restart_after_dispatch_affecting_fixes() -> None:
    body = _body()
    normalized = _normalized_body()

    assert "Post-Fix Daemon Restart Gate" in body
    assert "dispatch, spawn, build controls, stage transitions" in normalized
    assert "worktree or clone isolation" in normalized
    assert "assume the running daemon still has the old code" in normalized
    assert "Stop or keep blocked the affected build targets" in normalized
    assert "record their run IDs, task refs, workspace paths, and isolation metadata" in normalized
    assert "Restart the daemon after notifying active agents" in normalized
    assert "Verify daemon health, call `gobby-sessions:set_handoff`" in normalized
    assert "uses the expected isolation and workspace metadata" in normalized
    assert "file or keep open a child build bug for stale daemon behavior" in normalized

    manual_tick_idx = normalized.index("manual-ticking the dispatcher")
    restart_gate_idx = normalized.index("Post-Fix Daemon Restart Gate")
    compaction_idx = normalized.index("## Compaction")

    assert manual_tick_idx < restart_gate_idx < compaction_idx


def test_build_coordinator_requires_stage_normalization_and_bug_fixes() -> None:
    body = _body()

    assert "Normalize leaf task stages" in body
    assert "default leaf tasks to `development`" in body
    assert "Fix blocking bugs immediately" in body
    assert "Fix non-blocking bugs when agents are running" in body
    assert "All discovered unattended-build bugs must be fixed" in body
    assert "committed, linked, and closed before the target" in body


def test_build_coordinator_forbids_stop_hook_goal_closure_shortcut() -> None:
    body = _body()
    normalized = _normalized_body()

    assert "The coordination epic is the active goal record" in body
    assert "Do not close, unclaim, or move work out of it just to satisfy a stop hook" in normalized
    assert "continue the coordinator loop above" in normalized
    assert "A stop hook is a reminder to finish or hand off the goal" in normalized
    assert "do not detach or reparent them to make the coordinator task closable" in body
    assert "Do not close the coordination epic to clear a stop hook" in normalized


def test_build_coordinator_is_generic_not_one_off() -> None:
    body = _body()

    assert "#12746" not in body
    assert "Neo4j" not in body
    assert "FalkorDB" not in body
    assert "<target-ref>" in body
