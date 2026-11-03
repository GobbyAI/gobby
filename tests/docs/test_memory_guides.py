"""Contract tests for memory and hook-schema guidance."""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
MEMORY_GUIDE = ROOT / "docs/guides/memory.md"
HOOK_GUIDE = ROOT / "docs/guides/hook-schemas.md"


def _text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_memory_guide_matches_search_and_tool_contracts() -> None:
    guide = _text(MEMORY_GUIDE)
    normalized = " ".join(guide.split())
    mcp_section = guide.split("## MCP Tools", 1)[1].split("### Common Calls", 1)[0]

    # Surfacing is bounded to the turn start and the four tool intents and
    # ends each index line in a `when:` clause; everything else stays a
    # deliberate agent search.
    assert "## Retrieval: Pushed Index and Agent Search" in guide
    assert "Automatic surfacing is bounded to the five moments" in normalized
    assert "pushes one ranked memory index per parent turn" in normalized
    assert "A hit with a rationale ends in `| when:`" in normalized
    assert "before acting, even when the code is familiar" in normalized
    assert "Everything beyond those indexes is the agent's own search." in normalized
    assert "p10 `0.62`, p50 `0.69`, and p90 `0.75`" in normalized
    assert "`review_task_memories`" in mcp_section
    assert "`bootstrap_session_title`" not in mcp_section
    assert "Judge each hit by its `similarity`, `type`, `rationale`, and content" in normalized
    assert "`gobby-results:get_tool_result`" in guide
    assert "`gobby-results:search_tool_result`" in guide
    assert "context epoch" in guide
    assert "`memory_recall` and `memory.min_recall_score` settings fail" in guide
    assert "memory_recall:" not in guide


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
        "post_tool_use_failure",
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
    assert mapping.count("| `StopFailure` | `stop_failure` | `turn_end` |") == 1
