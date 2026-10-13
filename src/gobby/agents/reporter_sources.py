"""Bounded projections of the Archivist's current digest for scheduled reporting."""

import argparse
import json
import re
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from gobby.utils.project_context import find_project_root


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
            "entries": [
                {"heading": name, "text": item.strip()}
                for name in (
                    "Decisions needed (Josh)",
                    "Operator actions (Josh)",
                    "Gaps and contradictions",
                )
                for item in re.split(
                    r"(?m)^(?=[-*+] |\d+[.)] )", sections.get(name, "missing heading")
                )
                if item.strip()
            ]
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


def _page_projection(
    projection: dict[str, object], metadata: dict[str, object], offset: int
) -> dict[str, object]:
    """Page complete evidence entries while measuring the entire JSON envelope."""
    entries = [
        (name, entry)
        for name, values in projection.items()
        if isinstance(values, list)
        for entry in values
    ]
    evidence: dict[str, object] = {
        name: [] if isinstance(value, list) else value for name, value in projection.items()
    }
    result = {
        **metadata,
        "evidence": evidence,
        "offset": offset,
        "count": 0,
        "total": len(entries),
        "next_offset": None,
    }
    for index in range(offset, len(entries)):
        name, entry = entries[index]
        values = evidence[name]
        assert isinstance(values, list)
        values.append(entry)
        result["count"] = index - offset + 1
        result["next_offset"] = index + 1 if index + 1 < len(entries) else None
        if len(json.dumps(result, ensure_ascii=False).encode()) + 1 >= 14_000:
            values.pop()
            result["count"] = index - offset
            result["next_offset"] = index
            if index == offset:
                raise ValueError(f"Digest entry at offset {offset} exceeds the 14KB page budget")
            break
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--section", choices=("queues", "fleet", "decisions"), required=True)
    parser.add_argument("--offset", type=int, default=0)
    args = parser.parse_args()
    today = datetime.now(ZoneInfo("America/Chicago")).date()
    path: Path | None = None
    try:
        if args.offset < 0:
            raise ValueError("offset must be nonnegative")
        root = find_project_root()
        if root is None:
            raise ValueError("Reporter digest requires a project root")
        reports = root / ".gobby" / "reports"
        path = reports / f"gobby-digest-{today.isoformat()}.md"
        source_date = today
        if not path.exists():
            previous = []
            for candidate in reports.glob("gobby-digest-*.md"):
                try:
                    digest_date = date.fromisoformat(candidate.stem.removeprefix("gobby-digest-"))
                except ValueError:
                    continue
                if digest_date < today and candidate.is_file():
                    previous.append((digest_date, candidate))
            if previous:
                source_date, path = max(previous)
        content = path.read_text(encoding="utf-8")
        metadata: dict[str, object] = {
            "source": str(path),
            "source_date": source_date.isoformat(),
            "stale": source_date < today,
            "age_days": (today - source_date).days,
            "modified_at": datetime.fromtimestamp(
                path.stat().st_mtime, ZoneInfo("America/Chicago")
            ).isoformat(),
            "section": args.section,
        }
        result = _page_projection(digest_projection(content, args.section), metadata, args.offset)
        encoded = json.dumps(result, ensure_ascii=False)
    except (OSError, ValueError) as exc:
        encoded = json.dumps(
            {
                "source": str(path) if path is not None else None,
                "section": args.section,
                "error": str(exc),
            }
        )
    print(encoded)


if __name__ == "__main__":
    main()
