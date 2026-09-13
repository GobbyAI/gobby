"""Contract tests for bundled task lifecycle guidance."""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.skills.scenario_runner import run_recorded_skill_scenario

pytestmark = pytest.mark.unit

SKILL_DIR = (
    Path(__file__).resolve().parents[2]
    / "src"
    / "gobby"
    / "install"
    / "shared"
    / "skills"
    / "gobby"
    / "references"
    / "tasks"
)
SKILL_PATH = SKILL_DIR / "closing.md"
HANDOFFS_PATH = SKILL_DIR.parent / "sessions" / "handoffs.md"
REPO_ROOT = Path(__file__).resolve().parents[2]
HANDOFF_PROMPT_PATH = (
    REPO_ROOT / "src" / "gobby" / "install" / "shared" / "prompts" / "handoff" / "authoring.md"
)
BREVITY_PATH = (
    REPO_ROOT / "src" / "gobby" / "install" / "shared" / "skills" / "brevity" / "SKILL.md"
)
AGENTS_PATH = REPO_ROOT / "AGENTS.md"
TASKS_GUIDE_PATH = REPO_ROOT / "docs" / "guides" / "tasks.md"
MCP_GUIDE_PATH = REPO_ROOT / "docs" / "guides" / "mcp-tools.md"
LIFECYCLE_SCENARIO_PATH = (
    REPO_ROOT / "tests" / "skills" / "scenarios" / "tasks" / "complete-task-lifecycle.yaml"
)
OWNERSHIP_SCENARIO_PATH = (
    REPO_ROOT / "tests" / "skills" / "scenarios" / "tasks" / "task-ownership-handoffs.yaml"
)


def test_validation_guidance_is_provider_neutral_and_source_aware() -> None:
    implementation = (SKILL_DIR / "implementation.md").read_text()
    closing = SKILL_PATH.read_text()
    for expected in (
        "final validation after every final edit and formatting change",
        "definitive exit",
        "environment prefixes",
        "pipelines",
        "fallbacks",
        "backgrounding",
        "subshells",
        "trailing output",
        "direct rerun",
        "later edit makes",
        "prior evidence stale; committing does not",
    ):
        assert expected in implementation
    assert "claiming, closing, and worked-on sessions" in closing
    assert "link windows" in closing
    for provider in ("Claude Code", "Qwen", "Droid", "Grok", "Codex"):
        assert provider not in implementation + closing


def test_core_is_compact_and_keeps_creation_and_exact_close_sequence() -> None:
    content = SKILL_PATH.read_text()
    assert len(content) < 15_000
    steps = (
        "1. Finish all edits; resolve every owned finding",
        "2. Run focused validation after the final edit",
        "3. Stage only task paths and commit",
        "4. Call `close_task` once",
        "5. Repair any deterministic blocker",
        "6. After `closed=true` or a closure notification",
    )
    positions = [content.index(step) for step in steps]
    assert positions == sorted(positions)
    assert "exact validation commands and results" in content
    assert "`preview=true` closes when ready" in content
    assert "review_task_memories" in content[positions[-1] :]
    assert "Repeat the same `close_task` call without `preview`" not in content
    overview = (SKILL_DIR / "overview.md").read_text()
    for topic in ("creation", "implementation", "closing", "reviews"):
        assert topic in overview


def test_agentic_close_review_waits_once_and_parks_the_caller() -> None:
    content = SKILL_PATH.read_text()
    assert "`agentic_review_required`, register `wait_for_agent`" in content
    assert "`validator_run_id`" in content
    assert "and yield" in content
    assert "Do not poll or repeatedly call close while review is running" in content


def test_creation_guidance_uses_structured_named_test_references() -> None:
    content = (SKILL_DIR / "creation.md").read_text()
    assert "`test: path::test_symbol`" in content
    assert "`file: path`" in content
    assert '"validation_criteria":' in content
    assert '"claim": True' in content


def test_claimed_task_owns_native_substeps_and_found_work() -> None:
    content = (SKILL_DIR / "implementation.md").read_text()
    assert "one deliverable's implementation substeps" in content
    assert "provider's native tracker" in content
    assert "Fix and verify it inside the current task" in content
    assert "another active session" in content
    assert "command, diagnostics, paths, and impact" in content
    assert "Preserve their files" in content
    assert "passing scoped run" in content
    for label in ("`needs-decision`", "`needs-planning`", "`clean-window`"):
        assert label in content
    assert "Filing alone does not finish found work" in content
    assert "coordination within the fix, not deferral reasons" in content


def test_standalone_handoff_skill_selects_boundaries_and_readable_payloads() -> None:
    content = HANDOFFS_PATH.read_text()
    for expected in (
        "Load before authoring",
        "implementation",
        "tracker",
        "current context",
        "epoch",
        "previous handoffs, logs, or completed ledgers",
        "10,000 JSON-escaped",
        "characters",
        "compressing prose into shorthand",
        "optional Markdown progress log",
        "reference its path",
        "`clear_session=false` during planning, review, or ongoing task work",
        "`true` only for a root/coordinator",
        "after closing the",
        "current task",
        "spawned worker finishes with structured `end_agent_run`",
    ):
        assert expected in content


def test_handoff_feedback_precedes_handoff_without_duplicate_epoch_submission() -> None:
    content = HANDOFFS_PATH.read_text()
    prompt = HANDOFF_PROMPT_PATH.read_text()
    assert "submit the epoch's feedback first" in content
    assert "`feedback(observations=[])`" in content
    assert "duplicate acknowledgment for the same epoch" in content
    assert "human-reviewed" in content
    assert content.index("`feedback(observations=[])`") < content.index("Call `set_handoff` last.")
    assert "gobby-sessions:feedback first" in prompt
    assert prompt.index("gobby-sessions:feedback first") < prompt.index("set_handoff last")


def test_brevity_uses_normal_prose_for_structured_handoffs() -> None:
    content = BREVITY_PATH.read_text()

    assert "Switch to normal prose for:" in content
    assert "Structured session and agent handoffs" in content


def test_wait_guidance_is_event_driven_in_agent_and_task_contracts() -> None:
    agents = AGENTS_PATH.read_text()
    for expected in (
        "applicable `wait_for_*`",
        "yield the turn",
        "repeated status calls",
        "repeated `capture_output`",
    ):
        assert expected in agents
    closing = SKILL_PATH.read_text()
    assert "register `wait_for_agent`" in closing
    assert "and yield" in closing
    assert "Do not poll or repeatedly call close" in closing


def test_guides_document_single_call_conditional_close() -> None:
    for path in (TASKS_GUIDE_PATH, SKILL_PATH):
        content = path.read_text()
        assert "preview=true" in content
        assert "closes" in content
        assert "agentic_review_required" in content
        assert "review_task_memories" in content
        assert "preview=false" not in content


def test_lifecycle_scenario_closes_with_one_conditional_call() -> None:
    result = run_recorded_skill_scenario(LIFECYCLE_SCENARIO_PATH)

    assert result.loaded.action_names == (
        "create_task",
        "edit",
        "run_validation",
        "commit",
        "preview_close",
        "review_memory",
        "respond",
    )
    assert "conditionally closed" in result.loaded.combined_text


def test_ownership_and_handoff_pressure_scenario_stays_inside_one_task() -> None:
    result = run_recorded_skill_scenario(OWNERSHIP_SCENARIO_PATH)

    assert result.loaded.action_names == (
        "claim_task",
        "initialize_native_tracker",
        "record_finding",
        "fix_finding",
        "set_handoff_compact",
        "close_current_task",
        "set_handoff_clear",
        "respond",
    )
    assert "create_task" not in result.loaded.action_names
    assert "checkpoint_file" not in result.loaded.action_names
    assert "readable current state" in result.loaded.combined_text
    assert "closing the current task" in result.loaded.combined_text
    assert "moving to another task" in result.loaded.combined_text
    assert "cumulative history" not in result.loaded.combined_text
