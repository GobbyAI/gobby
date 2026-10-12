"""Bounded projections of the Archivist's current digest for scheduled reporting."""

import argparse
import json
import re
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo


def task_refs(text: str) -> list[str]:
    """Preserve recorded order, expand ranges, and exclude session references."""
    refs: list[str] = []
    pattern = r"(?<![\w])#(\d+)(?:\s*[–—-]\s*#(\d+))?"
    for match in re.finditer(pattern, text):
        first = int(match[1])
        last = int(match[2]) if match[2] else first
        if last < first or last - first > 100:
            raise ValueError("Digest task range exceeds bounded resolution")
        for number in range(first, last + 1):
            ref = f"#{number}"
            if ref not in refs:
                refs.append(ref)
    return refs


def digest_projection(content: str, section: str) -> dict[str, object]:
    """Select headings and table columns; cumulative seat histories stay unread."""
    sections: dict[str, str] = {}
    parts = re.split(r"^## ([^\n]+)\n", content, flags=re.MULTILINE)
    for index in range(1, len(parts), 2):
        sections[parts[index]] = parts[index + 1]
    if section == "queues":
        lanes = []
        for line in sections.get("Lane queues — current", "").splitlines():
            if not line.startswith("|"):
                continue
            cells = [cell.strip() for cell in re.split(r"(?<!\\)\|", line)[1:-1]]
            if len(cells) != 6 or not re.match(r"^[1-8](?:[A-Z]|\b)", cells[0]):
                continue
            lanes.append(
                {
                    "lane": cells[0],
                    "epic": cells[1],
                    "worktree": cells[2],
                    "queue_refs": task_refs(cells[4]),
                    "parked_refs": task_refs(cells[5]),
                }
            )
        return {
            "lanes": lanes,
            "queue_rule": "Resolve refs in order; skip closed and claimed; combine sublanes",
        }
    if section == "decisions":
        return {
            name: sections.get(name, "missing heading")
            for name in (
                "Decisions needed (Josh)",
                "Operator actions (Josh)",
                "Gaps and contradictions",
            )
        }
    if section != "fleet":
        raise ValueError("section must be queues, fleet, or decisions")
    # Archivist appends updates in source order. A terminal status bullet puts
    # old states after 'Earlier:'; those states cannot establish today's hold.
    updates = [
        line.split("Earlier:", 1)[0].strip()
        for name, body in sections.items()
        if name.startswith(("Orchestrator updates —", "Terminal and daemon status —"))
        for line in body.splitlines()
        if line.startswith("- ")
    ]
    topics = {
        "backlog": r"close backlog|backlog.*?/10",
        "heavy_slots": r"heavy.*?slot|slot.*?grant|slot.*?released",
        "holds": r"drain|hold|DAEMON BACK|restart.*?retimed",
        "consolidator": r"Consolidator|consolidation.*?review",
        "roadmap": r"ROADMAP|Story B|critical path",
        "activation": r"activat|DAEMON BACK",
        "candidates": r"CANDIDATE|waiting.*?(?:landing|activation|close)",
    }
    return {
        topic: [line for line in updates if re.search(pattern, line, re.IGNORECASE)][-2:]
        for topic, pattern in topics.items()
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--section", choices=("queues", "fleet", "decisions"), required=True)
    args = parser.parse_args()
    today = datetime.now(ZoneInfo("America/Chicago")).date().isoformat()
    path = Path.home() / "Desktop" / f"gobby-digest-{today}.md"
    try:
        content = path.read_text(encoding="utf-8")
        result = {
            "source": str(path),
            "modified_at": datetime.fromtimestamp(
                path.stat().st_mtime, ZoneInfo("America/Chicago")
            ).isoformat(),
            "section": args.section,
            "evidence": digest_projection(content, args.section),
        }
        encoded = json.dumps(result, ensure_ascii=False)
        if len(encoded.encode()) >= 14_000:
            raise ValueError("Selected digest section exceeds 14KB; report source gap")
    except (OSError, ValueError) as exc:
        encoded = json.dumps({"source": str(path), "section": args.section, "error": str(exc)})
    print(encoded)


if __name__ == "__main__":
    main()
