"""Regression checks for active context-aware progressive-discovery guidance."""

from __future__ import annotations

import re
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
ACTIVE_GUIDANCE_ROOTS = (
    PROJECT_ROOT / "docs/guides",
    PROJECT_ROOT / "src/gobby/install/shared/prompts",
    PROJECT_ROOT / "src/gobby/install/shared/skills",
    PROJECT_ROOT / "src/gobby/install/shared/workflows/agents",
    PROJECT_ROOT / "src/gobby/install/shared/workflows/rules",
)
ACTIVE_GUIDANCE_FILES = (
    PROJECT_ROOT / "AGENTS.md",
    PROJECT_ROOT / "src/gobby/hooks/event_handlers/_agent.py",
    PROJECT_ROOT / "src/gobby/mcp_proxy/instructions.py",
    PROJECT_ROOT / "src/gobby/mcp_proxy/stdio_tools.py",
    PROJECT_ROOT / "src/gobby/memory/dream/truth_digest.py",
    PROJECT_ROOT / "src/gobby/sessions/compact_continuation.py",
    PROJECT_ROOT / "src/gobby/skills/formatting.py",
    PROJECT_ROOT / "src/gobby/workflows/engine/skill_load_guidance.py",
)
ACTIVE_GUIDANCE_SUFFIXES = {".md", ".py", ".txt", ".yaml", ".yml"}
MANDATORY_ORDERED_CHAIN = re.compile(
    r"(?:use|follow|required?|must|all servers follow)[^\n]{0,120}"
    r"(?:progressive[^\n]{0,40})?(?:discovery|chain)"
    r".{0,240}?list_mcp_servers"
    r".{0,160}?list_tools"
    r".{0,160}?get_tool_schema"
    r".{0,160}?call_tool",
    re.IGNORECASE | re.DOTALL,
)
ARROW_ORDERED_CHAIN = re.compile(
    r"list_mcp_servers.{0,80}?(?:->|→)"
    r".{0,80}?list_tools.{0,80}?(?:->|→)"
    r".{0,80}?get_tool_schema.{0,80}?(?:->|→)"
    r".{0,80}?call_tool",
    re.IGNORECASE | re.DOTALL,
)


def _active_guidance_files() -> list[Path]:
    files = set(ACTIVE_GUIDANCE_FILES)
    for root in ACTIVE_GUIDANCE_ROOTS:
        files.update(
            path
            for path in root.rglob("*")
            if path.is_file() and path.suffix in ACTIVE_GUIDANCE_SUFFIXES
        )
    return sorted(files)


def test_active_guidance_has_no_mandatory_inventory_first_chain() -> None:
    offenders: list[str] = []
    for path in _active_guidance_files():
        content = path.read_text(encoding="utf-8")
        if MANDATORY_ORDERED_CHAIN.search(content) or ARROW_ORDERED_CHAIN.search(content):
            offenders.append(str(path.relative_to(PROJECT_ROOT)))

    assert offenders == []


def test_isolated_inventory_references_remain_allowed() -> None:
    content = (
        "Use list_tools only for an unknown tool name. "
        "Use list_mcp_servers for explicit registry inspection."
    )

    assert MANDATORY_ORDERED_CHAIN.search(content) is None
    assert ARROW_ORDERED_CHAIN.search(content) is None
