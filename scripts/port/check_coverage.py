"""Check that chunk ledger selectors own every parity-ledger row exactly once.

Usage: python3 -I check_coverage.py <map.json> <ledger.md>

<map.json> holds {"chunks": [{"id": ..., "ledger_selectors": [regex, ...]}, ...]}.
Each selector is a Python regex matched with re.search against a row key: the
backticked first cell of the ledger table, e.g. "MCP gobby-tasks:create_task".
Exit 0 only when no row is orphaned or double-owned and every selector compiles.
"""

from __future__ import annotations

import json
import re
import sys
from collections import defaultdict
from pathlib import Path


def ledger_rows(path: Path) -> list[tuple[str, str]]:
    """Return (row key, status) for every ledger table row."""
    rows: list[tuple[str, str]] = []
    for line in path.read_text().splitlines():
        if not line.startswith("| `"):
            continue
        end = line.find("` |")
        if end < 0:
            continue
        cells = [cell.strip() for cell in line.split(" | ")]
        rows.append((line[3:end], cells[1] if len(cells) > 1 else ""))
    return rows


def main() -> int:
    if len(sys.argv) != 3:
        print(__doc__)
        return 2
    map_path = Path(sys.argv[1])
    ledger = Path(sys.argv[2])
    chunks = json.loads(map_path.read_text())["chunks"]
    compiled: list[tuple[str, re.Pattern[str]]] = []
    bad: list[str] = []
    for chunk in chunks:
        for selector in chunk.get("ledger_selectors") or []:
            try:
                compiled.append((chunk["id"], re.compile(selector)))
            except re.error as exc:
                bad.append(f"{chunk['id']}: {selector!r}: {exc}")
    rows = ledger_rows(ledger)
    owners: dict[str, set[str]] = defaultdict(set)
    for key, _status in rows:
        for chunk_id, pattern in compiled:
            if pattern.search(key):
                owners[key].add(chunk_id)
    orphans = [key for key, _status in rows if not owners[key]]
    doubles = {key: sorted(ids) for key, ids in owners.items() if len(ids) > 1}
    per_chunk: dict[str, int] = defaultdict(int)
    for ids in owners.values():
        if len(ids) == 1:
            per_chunk[next(iter(ids))] += 1
    print(
        f"rows={len(rows)} owned_once={sum(per_chunk.values())} orphans={len(orphans)} "
        f"double_owned={len(doubles)} bad_selectors={len(bad)}"
    )
    for line in bad:
        print("BAD", line)
    for key in orphans[:60]:
        print("ORPHAN", key)
    if len(orphans) > 60:
        print(f"... {len(orphans) - 60} more orphans")
    for key, ids in list(doubles.items())[:60]:
        print("DOUBLE", key, "->", ",".join(ids))
    if len(doubles) > 60:
        print(f"... {len(doubles) - 60} more double-owned")
    print("PER_CHUNK", json.dumps(dict(sorted(per_chunk.items()))))
    return 0 if not orphans and not doubles and not bad else 1


if __name__ == "__main__":
    sys.exit(main())
