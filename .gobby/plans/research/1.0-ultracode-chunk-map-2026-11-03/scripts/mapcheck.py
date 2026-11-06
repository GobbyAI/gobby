"""Mechanical checks on one chunk map (read-only).

Usage: python3 -I mapcheck.py <map.json> <open-nodes.json> [--checkpoints CP-RUNNER,CP-MCP,...]

Checks:
- ids unique; every depends_on id resolves; no cycles; chunks listed in topological order
- oracle text cites only self or upstream chunk ids
- tree ownership: each open #21542 node resolves to exactly one chunk through its deepest
  listed ancestor-or-self in tree_nodes (orphans and double-listed refs are reported; an
  orphan is fine only when a disposition covers it, so they are counted, not failed)
- required fields non-empty
- every decision_refs id resolves to a decisions[].id
- with --checkpoints: each named checkpoint chunk exists, and every chunk listed after it in
  topological order whose closure does not include it is reported (a chunk that should wait
  for the checkpoint but does not). Report only; the restructure decides which chunks may run
  in parallel with a checkpoint.
Exit 1 on dup ids, unresolved deps, cycles, order violations, unresolved decision refs,
non-upstream oracle cites, or double-listed tree refs. Exit 0 otherwise.
"""

from __future__ import annotations

import json
import re
import sys
from collections import defaultdict
from pathlib import Path

REQUIRED = ["goal", "oracle", "size", "registration", "risks", "engines", "python_edits"]


def load_nodes(path: str) -> dict[str, dict]:
    return {n["ref"]: n for n in json.loads(Path(path).read_text())["nodes"]}


def norm_ref(r: object) -> str:
    s = str(r).strip()
    m = re.search(r"#?(\d{4,6})", s)
    return f"#{m.group(1)}" if m else s


def main() -> int:
    args = sys.argv[1:]
    if len(args) < 2:
        print(__doc__)
        return 2
    map_path = Path(args[0])
    cps: list[str] = []
    if "--checkpoints" in args:
        cps = [x for x in args[args.index("--checkpoints") + 1].split(",") if x]
    m = json.loads(map_path.read_text())
    chunks = m["chunks"]
    ids = [c["id"] for c in chunks]
    idset = set(ids)
    fail: list[str] = []

    dup = sorted({i for i in ids if ids.count(i) > 1})
    deps = {c["id"]: list(c.get("depends_on") or []) for c in chunks}
    unresolved = sorted({(c, d) for c, ds in deps.items() for d in ds if d not in idset})
    if dup:
        fail.append(f"dup_ids={dup}")
    if unresolved:
        fail.append(f"unresolved_deps={unresolved}")

    # cycles
    color: dict[str, int] = {}
    cycles: list[str] = []

    def visit(n: str, stack: list[str]) -> None:
        if color.get(n) == 1:
            cycles.append(" > ".join(stack + [n]))
            return
        if color.get(n) == 2:
            return
        color[n] = 1
        for d in deps.get(n, []):
            if d in idset:
                visit(d, stack + [n])
        color[n] = 2

    for n in ids:
        visit(n, [])
    if cycles:
        fail.append(f"cycles={cycles[:5]}")

    # closure
    memo: dict[str, set[str]] = {}

    def closure(cid: str) -> set[str]:
        if cid in memo:
            return memo[cid]
        memo[cid] = set()
        acc: set[str] = set()
        for d in deps.get(cid, []):
            if d in idset:
                acc.add(d)
                acc |= closure(d)
        memo[cid] = acc
        return acc

    clo = {c: closure(c) for c in ids}

    # longest path
    longest: dict[str, tuple[int, list[str]]] = {}

    def lp(n: str) -> tuple[int, list[str]]:
        if n in longest:
            return longest[n]
        longest[n] = (1, [n])
        best = (1, [n])
        for d in deps.get(n, []):
            if d in idset:
                ln, path = lp(d)
                if ln + 1 > best[0]:
                    best = (ln + 1, path + [n])
        longest[n] = best
        return best

    best = max((lp(n) for n in ids), key=lambda t: t[0]) if ids else (0, [])

    pos = {c: i for i, c in enumerate(ids)}
    order_viol = [(c, d) for c, ds in deps.items() for d in ds if d in pos and pos[d] > pos[c]]
    if order_viol:
        fail.append(f"order_violations={order_viol[:10]}")

    print(f"chunks={len(ids)} dup_ids={dup} unresolved={unresolved[:10]} cycles={len(cycles)} "
          f"longest={best[0]} order_violations={len(order_viol)}")
    print("  longest path:", " > ".join(best[1]))

    # oracle cites
    gaps = []
    for c in chunks:
        text = json.dumps(c.get("oracle"))
        for other in ids:
            if other == c["id"]:
                continue
            if re.search(r"(?<![A-Za-z0-9_-])" + re.escape(other) + r"(?![A-Za-z0-9_-])", text):
                if other not in clo[c["id"]]:
                    gaps.append((c["id"], other))
    print(f"  oracle-text cites non-upstream chunk ids: {len(gaps)} {gaps[:25]}")
    if gaps:
        fail.append(f"oracle_non_upstream={len(gaps)}")

    kinds: dict[str, list[str]] = defaultdict(list)
    for c in chunks:
        kinds[c.get("kind")].append(c["id"])
    print("  kinds:", {k: len(v) for k, v in kinds.items()})

    # tree ownership
    nodes = load_nodes(args[1])
    listed: dict[str, list[str]] = defaultdict(list)
    for c in chunks:
        for r in c.get("tree_nodes") or []:
            listed[norm_ref(r)].append(c["id"])
    dbl = {r: sorted(set(v)) for r, v in listed.items() if len(set(v)) > 1}
    not_open = sorted(r for r in listed if r not in nodes)
    owned: dict[str, list[str]] = {}
    orphan: list[str] = []
    for ref, n in nodes.items():
        chain = [f"#{p}" for p in str(n.get("path") or "").split(".") if p]
        owner = None
        for anc in reversed(chain):
            if anc in listed:
                owner = listed[anc]
                break
        if owner is None:
            orphan.append(ref)
        else:
            owned[ref] = owner
    print(f"  tree: open={len(nodes)} listed_refs={len(listed)} owned={len(owned)} "
          f"orphans={len(orphan)} double_listed={len(dbl)} listed_not_open={len(not_open)}")
    if dbl:
        print("    double-listed:", dict(list(dbl.items())[:20]))
        fail.append(f"double_listed_tree_refs={len(dbl)}")
    if not_open:
        print("    listed_not_open:", not_open[:30])

    # fields
    empty: dict[str, list[str]] = defaultdict(list)
    for c in chunks:
        for k in REQUIRED:
            if c.get(k) in (None, "", [], {}):
                empty[k].append(c["id"])
    print("  empty required fields:", {k: (len(v), v[:8]) for k, v in empty.items()})

    # decisions
    dec_ids = {d.get("id") for d in m.get("decisions", []) if isinstance(d, dict)}
    bad_refs = sorted({(c["id"], r) for c in chunks for r in (c.get("decision_refs") or [])
                       if r not in dec_ids})
    print(f"  decisions={len(dec_ids)} unresolved decision_refs={len(bad_refs)} {bad_refs[:15]}")
    if bad_refs:
        fail.append(f"unresolved_decision_refs={len(bad_refs)}")

    # checkpoints
    for cp in cps:
        if cp not in idset:
            print(f"  checkpoint {cp}: MISSING")
            fail.append(f"missing_checkpoint={cp}")
            continue
        after = [c for c in ids if pos[c] > pos[cp]]
        waits = [c for c in after if cp in clo[c]]
        free = [c for c in after if cp not in clo[c]]
        print(f"  checkpoint {cp} (kind={next(x.get('kind') for x in chunks if x['id'] == cp)}): "
              f"upstream={len(clo[cp])} downstream_waiting={len(waits)} "
              f"listed_after_but_not_waiting={len(free)} {free[:20]}")

    print("RESULT:", "FAIL " + "; ".join(fail) if fail else "PASS")
    return 1 if fail else 0


if __name__ == "__main__":
    sys.exit(main())
