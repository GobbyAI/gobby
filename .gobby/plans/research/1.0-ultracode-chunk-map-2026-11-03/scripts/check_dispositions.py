"""Check that every open #21542 node has exactly one valid disposition.

Usage: python3 -I check_dispositions.py <open-nodes.json> <map.json> <dispositions.json>...

Each dispositions file holds {"dispositions": [{"ref": "#N", "disposition": kind,
"target": str, "rationale": str}, ...]}.

Kinds and what "target" must name:
- chunk: a chunk id from <map.json>. The node's work is done inside that chunk.
- superseded: a chunk id from <map.json>. The node closes as superseded by that chunk.
- container: a chunk id from <map.json>, or "map-root". The node is an umbrella that the
  restructure replaces with the chunk DAG.
- start_gate: free text naming the 0.5.0 work it belongs to (it finishes before the cut).
- post_1_0: free text naming where it moves (out of the 1.0 tree).
- decision: a decision id from <map.json> "decisions" (for example "D3").
Exit 0 only when no node is missing, none is doubled, no ref is unknown and every target
is valid.
"""

from __future__ import annotations

import json
import sys
from collections import defaultdict
from pathlib import Path

CHUNK_TARGET = {"chunk", "superseded"}
KINDS = CHUNK_TARGET | {"container", "start_gate", "post_1_0", "decision"}


def main() -> int:
    nodes = json.loads(Path(sys.argv[1]).read_text())["nodes"]
    mapping = json.loads(Path(sys.argv[2]).read_text())
    chunk_ids = {c["id"] for c in mapping.get("chunks", [])}
    decision_ids = {
        (d.get("id") if isinstance(d, dict) else str(d)) for d in mapping.get("decisions", [])
    }
    expected = {n["ref"]: n for n in nodes}
    seen: dict[str, list[dict]] = defaultdict(list)
    for path in sys.argv[3:]:
        for item in json.loads(Path(path).read_text())["dispositions"]:
            item = dict(item, _file=Path(path).name)
            seen[str(item.get("ref", "")).strip()].append(item)
    missing = sorted(ref for ref in expected if ref not in seen)
    unknown = sorted(ref for ref in seen if ref not in expected)
    doubled = {ref: items for ref, items in seen.items() if len(items) > 1}
    invalid: list[str] = []
    per_kind: dict[str, int] = defaultdict(int)
    per_chunk: dict[str, int] = defaultdict(int)
    for ref, items in seen.items():
        for item in items:
            kind = item.get("disposition")
            target = str(item.get("target") or "").strip()
            per_kind[str(kind)] += 1
            if kind not in KINDS:
                invalid.append(f"{ref}: unknown kind {kind!r} ({item['_file']})")
            elif kind in CHUNK_TARGET and target not in chunk_ids:
                invalid.append(f"{ref}: {kind} target {target!r} is not a chunk id")
            elif kind == "container" and target != "map-root" and target not in chunk_ids:
                invalid.append(f"{ref}: container target {target!r} is not a chunk id")
            elif kind == "decision" and target not in decision_ids:
                invalid.append(f"{ref}: decision target {target!r} is not in the map")
            elif not target:
                invalid.append(f"{ref}: {kind} has no target")
            if not str(item.get("rationale") or "").strip():
                invalid.append(f"{ref}: no rationale")
            if kind in CHUNK_TARGET:
                per_chunk[target] += 1
    print(
        f"expected={len(expected)} disposed={len(seen)} missing={len(missing)} "
        f"unknown={len(unknown)} doubled={len(doubled)} invalid={len(invalid)}"
    )
    for ref in missing[:80]:
        print("MISSING", ref, expected[ref].get("title"))
    for ref in unknown[:40]:
        print("UNKNOWN", ref)
    for ref, items in list(doubled.items())[:40]:
        print("DOUBLE", ref, [(i.get("disposition"), i.get("target"), i["_file"]) for i in items])
    for line in invalid[:80]:
        print("INVALID", line)
    print("PER_KIND", json.dumps(dict(sorted(per_kind.items()))))
    print("PER_CHUNK", json.dumps(dict(sorted(per_chunk.items()))))
    empty = sorted(chunk_ids - set(per_chunk))
    print("CHUNKS_WITHOUT_NODES", json.dumps(empty))
    return 0 if not (missing or unknown or doubled or invalid) else 1


if __name__ == "__main__":
    sys.exit(main())
