"""Contract tests for bundled task lifecycle guidance."""

from __future__ import annotations

from pathlib import Path

import pytest

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
EVIDENCE_REFERENCE_PATH = SKILL_DIR / "references" / "evidence-provider-recovery.md"


def test_validation_guidance_is_provider_neutral_and_source_aware() -> None:
    content = EVIDENCE_REFERENCE_PATH.read_text()

    assert "Run each validation command as one native terminal invocation" in content
    assert "follow every `wait` or `write_stdin` token until termination" in content
    assert "## Recovery" in content
    assert "Claude Code and Qwen" in content
    assert "Droid and Grok" in content
    assert "Codex" in content
    assert "Manual `validation_command` evidence is prohibited" in content


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
        "5. Review session memories"
    )
    assert content.index("5. Review session memories") < content.index("7. Call `close_task`")
    assert "references/creation.md" in content
    assert "references/evidence-provider-recovery.md" in content
    assert "references/no-work-closures.md" in content
    assert "references/review-flows.md" in content
    assert "```python" not in content
