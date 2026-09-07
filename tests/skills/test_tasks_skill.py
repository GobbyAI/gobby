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
    / "tasks"
)
SKILL_PATH = SKILL_DIR / "SKILL.md"
HANDOFFS_PATH = SKILL_DIR.parent / "handoff-discipline" / "SKILL.md"
REPO_ROOT = Path(__file__).resolve().parents[2]
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
    """9c17a6466 replaced evidence receipts with the checklist close contract."""
    content = SKILL_PATH.read_text()

    assert "Shell validation must produce a definitive exit code" in content
    assert "follow every yielded cell or PTY session until exit" in content
    assert "derives validation evidence from the transcripts of the claiming" in content
    assert "rerun the command through a supported shell tool" in content
    for provider in ("Claude Code", "Qwen", "Droid", "Grok", "Codex"):
        assert provider not in content, f"close guidance must stay provider-neutral: {provider}"


def test_core_is_compact_and_keeps_creation_and_exact_close_sequence() -> None:
    content = SKILL_PATH.read_text()

    assert len(content) < 15_000
    assert "## Create or Claim Before Editing" in content
    assert "## Exact Interactive Close Sequence" in content
    assert content.index("1. Finish all file edits.") < content.index(
        "2. Sweep the native tracker and current transcript for owned findings"
    )
    assert content.index(
        "2. Sweep the native tracker and current transcript for owned findings"
    ) < content.index("3. Run focused validation after the final edit.")
    assert content.index("3. Run focused validation after the final edit.") < content.index(
        "5. Stage specific files and commit"
    )
    assert content.index("5. Stage specific files and commit") < content.index(
        "6. Call `close_task` once"
    )
    assert content.index("6. Call `close_task` once") < content.index(
        "7. Call `review_task_memories`"
    )
    assert "review_task_memories" not in content[: content.index("6. Call `close_task` once")]
    assert "Call `close_task` once with" in content
    assert "A ready call links the commit and closes atomically." in content
    assert "exact validation commands and results" in content
    assert "Repeat the same `close_task` call without `preview`" not in content
    assert "repeat the conditional close" not in content
    assert "references/creation.md" in content
    assert "references/no-work-closures.md" in content
    assert "references/review-flows.md" in content


def test_creation_guidance_uses_structured_named_test_references() -> None:
    content = (SKILL_DIR / "references" / "creation.md").read_text()

    assert "When criteria depend on named test bodies" in content
    assert "test: `tests/skills/test_tasks_skill.py::" in content
    assert '"validation_criteria": (' in content


def test_claimed_task_owns_native_substeps_and_found_work() -> None:
    content = SKILL_PATH.read_text()

    assert "For multi-step work, initialize the provider's native task" in content
    assert "One Gobby task owns the deliverable" in content
    assert "native tracker owns its implementation substeps" in content
    assert "add the finding to the claimed task's native tracker" in content
    assert "before closing that same Gobby task" in content
    assert "Create another Gobby task only when the user explicitly directs it" in content
    assert "`needs-decision` or `clean-window`" in content
    assert "parent coordinator" in content
    assert "failing command, diagnostics, paths," in content
    assert "Sweep the native tracker and current transcript for owned findings" in content


def test_standalone_handoff_skill_selects_boundaries_and_readable_payloads() -> None:
    content = HANDOFFS_PATH.read_text()

    assert "name: handoff-discipline" in content
    assert "Load `handoff-discipline`" in SKILL_PATH.read_text()
    assert not (SKILL_DIR / "references" / "handoffs.md").exists()

    assert "Planning or review reaches context pressure" in content
    assert "Active task reaches context pressure" in content
    assert "`set_handoff(clear_session=false)`" in content
    assert "Root/coordinator has closed the current task" in content
    assert "moves to another task or epic child" in content
    assert "`set_handoff(clear_session=true)`" in content
    assert "reserved for moving between tasks" in content
    assert "task closes" in content
    assert "Spawned worker finishes or completes a blocker handoff" in content
    assert "Structured `end_agent_run(...)`" in content
    assert "cumulative history, previous handoffs, raw logs, completed ledgers" in content
    assert "artificial shorthand" in content
    assert "Do not create a checkpoint file by default" in content
    assert "only when the user explicitly requests one" in content
    assert "`set_handoff` remains uncapped" in content


def test_brevity_uses_normal_prose_for_structured_handoffs() -> None:
    content = BREVITY_PATH.read_text()

    assert "Switch to normal prose for:" in content
    assert "Structured session and agent handoffs" in content


def test_wait_guidance_is_event_driven_in_agent_and_task_contracts() -> None:
    for content in (AGENTS_PATH.read_text(), SKILL_PATH.read_text()):
        assert "applicable `wait_for_*`" in content
        assert "yield the turn" in content
        assert "repeated status calls" in content
        assert "repeated `capture_output`" in content

    skill = SKILL_PATH.read_text()
    assert "`wait_for_agent` for spawned runs" in skill
    assert "`wait_for_output` for terminal" in skill


def test_guides_document_single_call_conditional_close() -> None:
    for path in (TASKS_GUIDE_PATH, MCP_GUIDE_PATH):
        content = path.read_text()
        assert "preview=true" in content
        assert "closed=true" in content
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
