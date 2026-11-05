"""Prefill default dispositions for open #21542 nodes from a map's tree ownership (read-only).

Usage: python3 -I prefill_dispositions.py <map.json> <open-nodes.json> <out_dir> <batch.json>...

Ownership is mapcheck.py's rule: a node belongs to the chunk whose tree_nodes lists the node's
deepest ancestor-or-self. For each batch file this writes <out_dir>/prefill-<batch file name>
with {"batch", "part", "dispositions": [...]}, each item {ref, title, disposition, target,
rationale, prefill_basis}:
- owned by exactly one chunk: disposition "chunk", target that chunk;
- not owned, but an ancestor of owned nodes: disposition "container", target the one chunk
  that owns all its owned descendants, else "map-root";
- anything else (orphan, double-listed): disposition null; a reviewer must decide.
Reviewers confirm or override every item; the prefill is a default, not a verdict.
"""

from __future__ import annotations

import json
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))  # -I drops the script dir
from mapcheck import load_nodes, norm_ref


def main() -> int:
    if len(sys.argv) < 5:
        print(__doc__)
        return 2
    chunks = json.loads(Path(sys.argv[1]).read_text())["chunks"]
    nodes = load_nodes(sys.argv[2])
    out_dir = Path(sys.argv[3])
    listed: dict[str, set[str]] = defaultdict(set)
    for c in chunks:
        for r in c.get("tree_nodes") or []:
            listed[norm_ref(r)].add(c["id"])

    def chain(ref: str) -> list[str]:
        return [f"#{p}" for p in str(nodes[ref].get("path") or "").split(".") if p]

    owner: dict[str, tuple[set[str], str]] = {}
    for ref in nodes:
        for anc in reversed(chain(ref)):
            if anc in listed:
                owner[ref] = (listed[anc], anc)
                break
    below: dict[str, set[str]] = defaultdict(set)
    for ref, (owners, _) in owner.items():
        for anc in chain(ref)[:-1]:
            below[anc] |= owners

    totals: dict[str, int] = defaultdict(int)
    for batch_path in map(Path, sys.argv[4:]):
        batch = json.loads(batch_path.read_text())
        items = []
        for n in batch["nodes"]:
            ref = n["ref"]
            item = {"ref": ref, "title": n.get("title"), "disposition": None, "target": None,
                    "rationale": "", "prefill_basis": ""}
            if ref in owner and len(owner[ref][0]) == 1:
                (cid,), anc = owner[ref]
                how = "listed directly" if anc == ref else f"through listed ancestor {anc}"
                item.update(disposition="chunk", target=cid,
                            prefill_basis=f"owned by {cid}, {how}")
            elif ref in owner:
                item["prefill_basis"] = f"double-listed: {sorted(owner[ref][0])}"
            elif below.get(ref):
                tgt = next(iter(below[ref])) if len(below[ref]) == 1 else "map-root"
                item.update(disposition="container", target=tgt,
                            prefill_basis=f"ancestor of nodes owned by {sorted(below[ref])}")
            else:
                item["prefill_basis"] = "orphan: no listed ancestor and no owned descendant"
            totals[str(item["disposition"])] += 1
            items.append(item)
        out = out_dir / f"prefill-{batch_path.name}"
        out.write_text(json.dumps({"batch": batch.get("batch"), "part": batch.get("part"),
                                   "dispositions": items}, indent=1))
        print(f"wrote {out.name}: {len(items)} items")
    print("PREFILL_TOTALS", json.dumps(dict(sorted(totals.items()))))
    return 0


if __name__ == "__main__":
    sys.exit(main())
