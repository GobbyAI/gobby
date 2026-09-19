"""Contract tests for the bundled feedback-reviewer step allowlists."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

pytestmark = pytest.mark.unit

AGENT_PATH = (
    Path(__file__).resolve().parents[2]
    / "src/gobby/install/shared/workflows/agents/feedback-reviewer.yaml"
)

RECALL_TOOLS = (
    "gobby-review-learning:recall_review_context",
    "gobby-memory:search_memories",
    "gobby-memory:get_memory",
)

WRITE_TOOLS = (
    "gobby-review-learning:record_review_lesson",
    "gobby-review-learning:retire_review_lesson",
    "gobby-memory:create_memory",
    "gobby-memory:update_memory",
    "gobby-memory:delete_memory",
    "gobby-memory:restore_memory",
)


def _steps() -> list[dict[str, Any]]:
    data = yaml.safe_load(AGENT_PATH.read_text(encoding="utf-8"))
    assert isinstance(data, dict)
    return list(data["step_workflow"]["steps"])


def _review_allowlist() -> set[str]:
    review = next(step for step in _steps() if step["name"] == "review")
    return set(review["allowed_mcp_tools"])


@pytest.mark.parametrize("tool", RECALL_TOOLS)
def test_review_step_allows_read_only_recall_tools(tool: str) -> None:
    """Regression for #22509.

    `allowed_mcp_tools` is a closed list, so omitting these left the reviewer
    unable to recall prior review context or check memory before
    characterizing runtime behavior — the context its triage reference
    (references/memory/review-lessons.md) requires it to consult.
    """
    assert tool in _review_allowlist()


def test_search_memories_is_paired_with_get_memory() -> None:
    """`search_memories` is a bootstrap tool whose hits only `get_memory` expands.

    A step listing a bootstrap tool without its follow-on fetch gets the agent
    killed by `step-mcp-tool-allowlist` after three denials (fixed repo-wide
    in #21247).
    """
    allowed = _review_allowlist()
    assert ("gobby-memory:search_memories" in allowed) == ("gobby-memory:get_memory" in allowed)


@pytest.mark.parametrize("tool", WRITE_TOOLS)
def test_no_step_allows_memory_or_review_lesson_writes(tool: str) -> None:
    """The reviewer recalls durable knowledge; it never writes any."""
    for step in _steps():
        assert tool not in set(step.get("allowed_mcp_tools", []))
