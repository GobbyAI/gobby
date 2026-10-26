"""Contract test: the search guide names tools by split server and tool."""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SEARCH_GUIDE = ROOT / "docs/guides/search.md"
DOTTED_ROUTE = re.compile(r"\bgobby-[a-z0-9-]+\.[a-z_]+\b")


def test_search_guide_has_no_dotted_server_tool_names() -> None:
    lines = SEARCH_GUIDE.read_text(encoding="utf-8").splitlines()
    hits = [
        f"{number}: {line.strip()}"
        for number, line in enumerate(lines, start=1)
        if DOTTED_ROUTE.search(line)
    ]

    assert not hits, "dotted server.tool names remain in docs/guides/search.md:\n" + "\n".join(hits)
