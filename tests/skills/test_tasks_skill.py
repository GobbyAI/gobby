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
REPO_ROOT = Path(__file__).resolve().parents[2]
TASKS_GUIDE_PATH = REPO_ROOT / "docs" / "guides" / "tasks.md"
MCP_GUIDE_PATH = REPO_ROOT / "docs" / "guides" / "mcp-tools.md"
LIFECYCLE_SCENARIO_PATH = (
    REPO_ROOT / "tests" / "skills" / "scenarios" / "tasks" / "complete-task-lifecycle.yaml"
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
        "2. Run focused validation after the final edit."
    )
    assert content.index("2. Run focused validation after the final edit.") < content.index(
        "4. Stage specific files and commit"
    )
    assert content.index("4. Stage specific files and commit") < content.index(
        "5. Call `close_task` once"
    )
    assert content.index("5. Call `close_task` once") < content.index(
        "6. Call `review_task_memories`"
    )
    assert "review_task_memories" not in content[: content.index("5. Call `close_task` once")]
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
