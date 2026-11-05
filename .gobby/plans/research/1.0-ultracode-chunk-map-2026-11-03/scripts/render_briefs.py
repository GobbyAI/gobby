"""Render the chunk briefs and the shared protocol from the map, the rulings and the dispositions.

Usage: python3 -I render_briefs.py [--base <sha>] <map.json> <rulings.md> <open-nodes.json> <out-dir>
           <dispositions.json>...

Writes:
- <out-dir>/protocol.md: what every chunk follows (chunk_protocol, branching, the live-checkout
  constraints and rollback_and_packaging, verbatim from the map) with the decisions,
  assumptions and rulings that text cites. It ships as docs/port/protocol.md in the
  map-approval commit (start gate item 16; items are numbered from 1, so item 16 is
  start_gate[15]).
- <out-dir>/<chunk id>.md: one brief per chunk, which becomes the chunk task's description: the
  chunk's own fields, its dependencies' titles, the open nodes its task supersedes (D31), and
  the decisions, assumptions and rulings its own fields cite.
- <out-dir>/_gaps.md and <out-dir>/_index.json: what a brief cannot fill, cites loosely or
  leaks (the lens input), and per-brief sizes and citations.

Nothing is paraphrased: every sentence comes from the map, the rulings file or the node records.
--base sets the reference base each brief names (start gate item 8: the pushed cut SHA).
"""

from __future__ import annotations

import json
import re
import sys
from collections import defaultdict
from collections.abc import Callable
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))  # -I drops the script's folder from sys.path
from mapcheck import load_nodes, norm_ref

LEAKS = ("/private/tmp", "scratchpad/", "tree-audit", "phase2", "v2-work", "fold-", "map-synth",
         "dispositions-nodes", "wf-", "check-C", "verify-V")
RULING_LIST = re.compile(r"\brulings?\s+(\d+(?:\s*-\s*\d+)?(?:\s*(?:,\s*and|,|and)\s*\d+"
                         r"(?:\s*-\s*\d+)?)*)")
DEC = re.compile(r"(?<![A-Za-z0-9-])D(\d{1,3})(?![0-9A-Za-z-])")
NODE_KINDS = {"family", "engine", "runner", "hubnode"}


def parse_rulings(text: str) -> dict[int, str]:
    """Numbered rulings from the top of the rulings file, up to its first ## heading."""
    body = text.split("\n## ", 1)[0]
    out: dict[int, str] = {}
    for m in re.finditer(r"(?ms)^(\d+)\. (.*?)(?=^\d+\. |\Z)", body):
        out[int(m.group(1))] = " ".join(m.group(2).split())
    return out


def ruling_numbers(text: str) -> set[int]:
    nums: set[int] = set()
    for m in RULING_LIST.finditer(text):
        for part in re.split(r"\s*(?:,\s*and|,|and)\s*", m.group(1)):
            if "-" in part:
                a, b = (int(x) for x in part.split("-"))
                nums.update(range(a, b + 1))
            elif part.strip():
                nums.add(int(part))
    return nums


def ownership(mapping: dict, nodes: dict) -> tuple[Callable[[str], tuple[list[str], str]], dict]:
    """Return owner(ref) -> (owning chunk ids, listed ancestor-or-self) and the listing map."""
    listed: dict[str, list[str]] = defaultdict(list)
    for chunk in mapping["chunks"]:
        for ref in chunk.get("tree_nodes") or []:
            listed[norm_ref(ref)].append(chunk["id"])

    def owner(ref: str) -> tuple[list[str], str]:
        chain = [f"#{p}" for p in str((nodes.get(ref) or {}).get("path") or "").split(".") if p]
        for anc in reversed(chain or [ref]):
            if anc in listed:
                return listed[anc], anc
        return [], ""

    return owner, listed


def load_dispositions(paths: list[str]) -> dict[str, list[dict]]:
    disp: dict[str, list[dict]] = defaultdict(list)
    for path in paths:
        for item in json.loads(Path(path).read_text())["dispositions"]:
            disp[item["ref"]].append(dict(item, _file=Path(path).name))
    return disp


def word_in(token: str, text: str) -> bool:
    return re.search(rf"(?<![A-Za-z0-9-]){re.escape(token)}(?![A-Za-z0-9-])", text) is not None


class Records:
    def __init__(self, m: dict, rulings: dict[int, str]) -> None:
        self.decisions = {d["id"]: d for d in m["decisions"]}
        self.assumptions = {a["id"]: a for a in m["assumptions"]}
        self.rulings = rulings

    def cited(self, text: str) -> tuple[list[str], list[str], list[int]]:
        dec = {f"D{n}" for n in DEC.findall(text)} & set(self.decisions)
        ass = {a for a in self.assumptions if word_in(a, text)}
        return (sorted(dec, key=lambda d: int(d[1:])), sorted(ass),
                sorted(ruling_numbers(text)))

    def render(self, dec: list[str], ass: list[str], rul: list[int], title: str) -> list[str]:
        lines = [f"## {title}", ""] if dec or ass or rul else []
        for did in dec:
            d = self.decisions[did]
            lines += [f"### {did}: {d['question']}", "",
                      f"Default ({d.get('resolved_by') or 'unresolved'}): {d['recommended_default']}",
                      ""]
        if ass:
            lines += ["### Assumptions", ""]
            for aid in ass:
                a = self.assumptions[aid]
                lines.append(f"- {aid}: {a['assumption']} If wrong: {a['if_wrong']}")
            lines.append("")
        known = [n for n in rul if n in self.rulings]
        if known:
            lines += ["### Operator rulings", ""]
            lines += [f"{n}. {self.rulings[n]}" for n in known] + [""]
        return lines


def protocol_doc(m: dict, rec: Records) -> tuple[str, str]:
    cp, lc = m["chunk_protocol"], m["live_checkout_constraints"]
    lines = ["# The chunk protocol", "",
             "Every chunk follows this protocol; each chunk task's brief holds what is specific "
             "to that chunk. docs/port/map-v2.md holds every decision and assumption, and "
             "docs/port/rulings.md every operator ruling.", "", "## Steps", ""]
    lines += [f"{i}. {s}" for i, s in enumerate(cp["steps"], 1)]
    lines += ["", cp["note"], "", "## Branching", "", m["branching"], "",
              "## Live checkout constraints", "", lc["intro"], ""]
    lines += [f"- {item}" for item in lc["items"]]
    lines += ["", "## Rollback and packaging", "", m["rollback_and_packaging"], ""]
    body = "\n".join(lines)
    dec, ass, rul = rec.cited(body)
    return body + "\n" + "\n".join(rec.render(dec, ass, rul, "Records the protocol cites")), body


def main() -> int:
    args = sys.argv[1:]
    base = "the pushed cut SHA, filled in at chunk-task creation (start gate item 8)"
    if args[:1] == ["--base"]:
        base, args = args[1], args[2:]
    if len(args) < 5:
        print(__doc__)
        return 2
    m = json.loads(Path(args[0]).read_text())
    rulings = parse_rulings(Path(args[1]).read_text())
    nodes = load_nodes(args[2])
    out_dir = Path(args[3])
    out_dir.mkdir(parents=True, exist_ok=True)
    disp = load_dispositions(args[4:])
    owner, _ = ownership(m, nodes)
    chunks = {c["id"]: c for c in m["chunks"]}
    rec = Records(m, rulings)
    prefixes = sorted({cid.split("-")[0] for cid in chunks if "-" in cid})
    id_like = re.compile(rf"(?<![A-Za-z0-9-])((?:{'|'.join(prefixes)})-[A-Z0-9]+"
                         rf"(?:-[A-Z0-9]+)*)(?![A-Za-z0-9-])")
    protocol, protocol_body = protocol_doc(m, rec)
    gap_lines: list[str] = []
    for n in sorted(ruling_numbers(protocol_body) - set(rulings)):
        gap_lines.append(f"protocol ERROR cites ruling {n}, which the rulings file lacks")
    gap_lines += [f"protocol ERROR leak: {leak!r}" for leak in LEAKS if leak in protocol]
    (out_dir / "protocol.md").write_text(protocol)
    by_target: dict[str, list[tuple[str, dict]]] = {}
    for ref, items in disp.items():
        by_target.setdefault(items[0]["target"], []).append((ref, items[0]))
    index: dict[str, dict] = {"protocol.md": {"chars": len(protocol)}}
    for cid, c in chunks.items():
        gaps: list[str] = []
        own = json.dumps({k: v for k, v in c.items() if k != "id"}, ensure_ascii=False)
        for field in ("goal", "oracle", "size", "risks", "registration", "python_edits"):
            if str(c.get(field) or "").strip().upper() in {"", "TBD", "PENDING", "?"}:
                gaps.append(f"ERROR empty {field}")
        gaps += [f"ERROR unknown depends_on {d}" for d in c["depends_on"] if d not in chunks]
        gaps += [f"ERROR unknown decision_ref {d}" for d in c["decision_refs"]
                 if d not in rec.decisions]
        gaps += [f"WARN tree_node {r} is not an open node" for r in c["tree_nodes"]
                 if r not in nodes]
        dec, ass, rul = rec.cited(own)
        gaps += [f"WARN text cites {d} outside decision_refs" for d in dec
                 if d not in c["decision_refs"]]
        gaps += [f"ERROR cites ruling {n}, which the rulings file lacks" for n in rul
                 if n not in rulings]
        for tok in sorted(set(id_like.findall(own)) - set(chunks)):
            if not any(other.startswith(tok + "-") for other in chunks):  # a family name
                gaps.append(f"WARN chunk-like id {tok} is not a chunk")
        dec = sorted(set(dec) | (set(c["decision_refs"]) & set(rec.decisions)),
                     key=lambda d: int(d[1:]))
        mine = sorted(by_target.get(cid, []), key=lambda t: t[0])
        elsewhere = [(ref, items[0]) for ref, items in sorted(disp.items())
                     if items[0]["target"] != cid and cid in owner(ref)[0]]
        if c["kind"] in NODE_KINDS and not mine:
            gaps.append("WARN no open node is dispositioned to this chunk")
        lines = [f"# {cid}: {c['title']}", "", f"Kind: {c['kind']}. Size: {c['size']}", "",
                 f"Reference base: {base}.", "",
                 "Follow docs/port/protocol.md, the protocol every chunk follows; this brief "
                 f"holds what is specific to {cid}. docs/port/map-v2.md holds every decision and "
                 "assumption, and docs/port/rulings.md every operator ruling.", "",
                 "## Depends on", ""]
        lines += [f"- {d}: {chunks[d]['title'] if d in chunks else '(unknown)'}"
                  for d in c["depends_on"]] or ["None."]
        lines += ["", "## Goal", "", c["goal"], "", "## Scope", ""]
        if c["ledger_selectors"]:
            lines += ["Ledger selectors (this chunk owns every ledger row they match):", ""]
            lines += [f"- `{s}`" for s in c["ledger_selectors"]] + [""]
        lines += [f"Engines: {'; '.join(c['engines']) or 'none'}.", "",
                  f"Seam gaps: {'; '.join(c['seam_gaps']) or 'none'}.", "", "## Tree nodes", ""]
        carried = [(r, x) for r, x in mine if x["disposition"] == "chunk"]
        replaced = [(r, x) for r, x in mine if x["disposition"] == "superseded"]
        if carried:
            lines += ["This chunk carries the work of these open nodes of the #21542 tree; its "
                      "task supersedes them (D31), except a node its goal closes as done. Read each node before the debt-not-to-port "
                      "pass:", ""]
            lines += [f"- {r} {(nodes.get(r) or {}).get('title', '(not an open node)')}"
                      for r, _ in carried]
        if replaced:
            lines += [""] if carried else []
            lines += ["This chunk's design replaces the planned approach of these open nodes; "
                      "they close as superseded by its task (D31):", ""]
            lines += [f"- {r} {(nodes.get(r) or {}).get('title', '(not an open node)')}"
                      for r, _ in replaced]
        if not mine:
            lines.append("No open node is dispositioned to this chunk.")
        if elsewhere:
            lines += ["", "Nodes under this chunk's tree nodes that close another way:", ""]
            for ref, x in elsewhere:
                title = (nodes.get(ref) or {}).get("title", "")
                lines.append(f"- {ref} {title} ({x['disposition']} {x['target']})")
        lines += ["", "## Oracle", "", c["oracle"], "", "## Python edits", "", c["python_edits"],
                  "", "## Registration", "", c["registration"], "", "## Risks", "", c["risks"], ""]
        lines += rec.render(dec, ass, rul, "Records this brief cites")
        text = "\n".join(lines)
        gaps += [f"ERROR leak: brief contains {leak!r}" for leak in LEAKS if leak in text]
        (out_dir / f"{cid}.md").write_text(text)
        index[cid] = {"chars": len(text), "decisions": dec, "rulings": rul, "assumptions": ass,
                      "nodes": [r for r, _ in mine], "gaps": gaps}
        gap_lines += [f"{cid} {g}" for g in gaps]
    (out_dir / "_index.json").write_text(json.dumps(index, indent=1) + "\n")
    (out_dir / "_gaps.md").write_text("\n".join(gap_lines) + "\n")
    sizes = sorted(v["chars"] for k, v in index.items() if k != "protocol.md")
    n_err = sum(1 for g in gap_lines if " ERROR " in g)
    print(f"protocol.md chars={len(protocol)}; briefs={len(sizes)} chars min/median/max="
          f"{sizes[0]}/{sizes[len(sizes) // 2]}/{sizes[-1]} total={sum(sizes)} "
          f"gaps={len(gap_lines)} errors={n_err}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
