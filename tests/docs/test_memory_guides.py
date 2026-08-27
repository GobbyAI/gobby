"""Contract tests for memory and hook-schema guidance."""

import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
MEMORY_GUIDE = ROOT / "docs/guides/memory.md"
HOOK_GUIDE = ROOT / "docs/guides/hook-schemas.md"
MEMORY_RULES = ROOT / "src/gobby/install/shared/workflows/rules/memory-lifecycle"


def _text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_memory_guide_matches_search_and_tool_contracts() -> None:
    guide = _text(MEMORY_GUIDE)
    normalized = " ".join(guide.split())
    mcp_section = guide.split("## MCP Tools", 1)[1].split("### Common Calls", 1)[0]

    assert "Agents search on demand; no rule injects memories automatically." in guide
    assert "p10 `0.62`, p50 `0.69`, and p90 `0.75`" in normalized
    assert "`review_task_memories`" in mcp_section
    assert "`bootstrap_session_title`" not in mcp_section
    assert "Judge each hit by its `similarity`, `type`, `rationale`, and content" in normalized
    assert "`gobby-results:get_tool_result`" in guide
    assert "`gobby-results:search_tool_result`" in guide
    assert "context epoch" in guide
    assert "`memory_recall` and `memory.min_recall_score` settings fail" in guide
    assert "memory_recall:" not in guide


def test_memory_guide_lists_every_bundled_lifecycle_rule() -> None:
    guide = _text(MEMORY_GUIDE)
    rule_table = guide.split("Current bundled memory rules:", 1)[1].split(
        "## Retrieval Is Agent-Driven", 1
    )[0]
    rule_names = {
        name
        for path in MEMORY_RULES.glob("*.yaml")
        for name in yaml.safe_load(_text(path))["rules"]
    }
    documented_names = set(re.findall(r"^\| `([^`]+)` \|", rule_table, re.MULTILINE))

    assert rule_names
    assert documented_names == rule_names


def test_hook_guide_maps_stop_failure_and_grok_hooks() -> None:
    guide = _text(HOOK_GUIDE)
    mapping = guide.split("## Native To Workflow Mapping", 1)[1].split(
        "## Common Payload Fields", 1
    )[0]

    assert "| `turn_end` | `after_agent`, `stop`, `stop_failure` |" in mapping
    assert "### Grok" in mapping
    for native_hook in (
        "session_start",
        "session_end",
        "user_prompt_submit",
        "pre_tool_use",
        "post_tool_use",
        "stop",
        "stop_failure",
        "pre_compact",
        "post_compact",
        "notification",
        "permission_denied",
        "subagent_start",
        "subagent_stop",
    ):
        assert f"`{native_hook}`" in mapping
    assert mapping.count("| `StopFailure` | `stop_failure` | `turn_end` |") == 2


def test_adjacent_guides_name_memory_lifecycle_gates() -> None:
    for relative_path in (
        "docs/guides/rules.md",
        "docs/guides/skills.md",
        "docs/guides/agents.md",
    ):
        guide = _text(ROOT / relative_path)
        assert "memory-lifecycle" in guide
        assert "search-memories-on-claim" in guide
        assert "guard-plan-memory-writes" in guide
        assert "review-closed-task-memories" in guide
