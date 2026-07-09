"""Decision-matrix analysis for task #17193.

Cells (per task description, corrected 2026-07-02):
  (a) LABEL VALIDITY  — judge↔ablation agreement on the calibration subsample
  (b) COMPONENT SIGNAL — per-component association vs judged usefulness
  (c) HEADROOM        — does the existing score already order by usefulness?
  (d) VOLUME          — labeled-row counts for a regularized fit
"""

import json
import math
import random
import statistics
from pathlib import Path
from typing import Any

SCRATCH = Path(__file__).parent


def mann_whitney_auc(pos: list[float], neg: list[float]) -> tuple[float, float]:
    """AUC via rank-sum + normal-approx two-sided p-value."""
    n1, n0 = len(pos), len(neg)
    if not n1 or not n0:
        return float("nan"), float("nan")
    allv = [(v, 1) for v in pos] + [(v, 0) for v in neg]
    allv.sort(key=lambda t: t[0])
    ranks: dict[int, float] = {}
    i = 0
    while i < len(allv):
        j = i
        while j < len(allv) and allv[j][0] == allv[i][0]:
            j += 1
        r = (i + j + 1) / 2  # average rank (1-based)
        for k in range(i, j):
            ranks[k] = r
        i = j
    r1 = sum(ranks[k] for k, (_, lab) in enumerate(allv) if lab == 1)
    u1 = r1 - n1 * (n1 + 1) / 2
    auc = u1 / (n1 * n0)
    mu = n1 * n0 / 2
    sigma = math.sqrt(n1 * n0 * (n1 + n0 + 1) / 12)
    z = (u1 - mu) / sigma if sigma else float("nan")
    p = 2 * (1 - 0.5 * (1 + math.erf(abs(z) / math.sqrt(2)))) if sigma else float("nan")
    return auc, p


def perm_pvalue_mean_diff(a: list[float], b: list[float], n_iter: int = 20000) -> float:
    """Two-sided permutation p for mean(a) - mean(b)."""
    if not a or not b:
        return float("nan")
    obs = statistics.mean(a) - statistics.mean(b)
    pool = a + b
    rng = random.Random(17193)
    hits = 0
    for _ in range(n_iter):
        rng.shuffle(pool)
        d = statistics.mean(pool[: len(a)]) - statistics.mean(pool[len(a) :])
        if abs(d) >= abs(obs) - 1e-12:
            hits += 1
    return hits / n_iter


def point_biserial(labels: list[bool], values: list[float]) -> float:
    pairs = [(1.0 if lab else 0.0, v) for lab, v in zip(labels, values, strict=True)]
    xs = [p[0] for p in pairs]
    ys = [p[1] for p in pairs]
    if len(set(xs)) < 2 or len(set(ys)) < 2:
        return float("nan")
    mx, my = statistics.mean(xs), statistics.mean(ys)
    cov = sum((x - mx) * (y - my) for x, y in pairs) / len(pairs)
    sx = statistics.pstdev(xs)
    sy = statistics.pstdev(ys)
    return cov / (sx * sy) if sx and sy else float("nan")


def main() -> None:
    judgments = [json.loads(ln) for ln in (SCRATCH / "judgments.jsonl").open()]
    judged = [j for j in judgments if j.get("judge_useful") is not None]
    print(f"judged rows: {len(judged)} (parse failures: {len(judgments) - len(judged)})")
    useful = [j for j in judged if j["judge_useful"]]
    print(
        f"base rate judge_useful=true: {len(useful)}/{len(judged)} = {len(useful) / len(judged):.3f}"
    )
    by_via: dict[str, list[dict[str, Any]]] = {}
    for j in judged:
        by_via.setdefault(j.get("render_via") or "?", []).append(j)
    for via, rows in sorted(by_via.items()):
        u = sum(1 for r in rows if r["judge_useful"])
        print(f"  via={via}: n={len(rows)} useful={u} ({u / len(rows):.3f})")

    # ---------------- (a) label validity ----------------
    ab_path = SCRATCH / "ablations.jsonl"
    print("\n=== (a) LABEL VALIDITY: judge vs ablation ===")
    if ab_path.exists():
        abls = [json.loads(ln) for ln in ab_path.open()]
        abls = [a for a in abls if a.get("ablation_delta") is not None]
        print(f"calibration rows with delta: {len(abls)}")
        du = [a["ablation_delta"] for a in abls if a["judge_useful"]]
        dn = [a["ablation_delta"] for a in abls if not a["judge_useful"]]
        if du and dn:
            print(f"mean delta | judge_useful=true : {statistics.mean(du):+.2f} (n={len(du)})")
            print(f"mean delta | judge_useful=false: {statistics.mean(dn):+.2f} (n={len(dn)})")
            nz = [a for a in abls if a["ablation_delta"] != 0]
            agree = sum(1 for a in nz if (a["ablation_delta"] > 0) == bool(a["judge_useful"]))
            print(
                f"sign agreement (delta!=0): {agree}/{len(nz)} = {agree / len(nz):.3f}"
                if nz
                else "no nonzero deltas"
            )
            p = perm_pvalue_mean_diff(du, dn)
            print(f"permutation p (mean diff): {p:.4f}")
            labels = [bool(a["judge_useful"]) for a in abls]
            deltas = [float(a["ablation_delta"]) for a in abls]
            print(f"point-biserial r(judge, delta): {point_biserial(labels, deltas):.3f}")
    else:
        print("ablations.jsonl missing — run ablate.py")

    # ---------------- (b) component signal ----------------
    print("\n=== (b) COMPONENT SIGNAL vs judge_useful ===")
    components = [
        ("render_score", "ranker similarity at injection"),
        ("block_position", "injection position within block"),
        ("block_size", "block size"),
        ("referenced_overlap", "lexical overlap resp~mem (feature)"),
        ("content_len", "memory length"),
        ("response_len", "response length (bias check)"),
    ]

    def component_report(rows: list[dict[str, Any]], label: str) -> None:
        print(f"-- subgroup: {label} (n={len(rows)})")
        for field, desc in components:
            pairs = [
                (bool(r["judge_useful"]), float(r[field])) for r in rows if r.get(field) is not None
            ]
            if len(pairs) < 10:
                continue
            pos = [v for lab, v in pairs if lab]
            neg = [v for lab, v in pairs if not lab]
            if not pos or not neg:
                continue
            auc, p = mann_whitney_auc(pos, neg)
            r_pb = point_biserial([lab for lab, _ in pairs], [v for _, v in pairs])
            print(
                f"  {field:<20} AUC={auc:.3f} p={p:.4f} r_pb={r_pb:+.3f} (n={len(pairs)}) — {desc}"
            )

    component_report(judged, "ALL")
    for via, rows in sorted(by_via.items()):
        if len(rows) >= 30:
            component_report(rows, f"via={via}")

    # ---------------- (c) headroom ----------------
    print("\n=== (c) HEADROOM: does render_score already order usefulness? ===")
    by_block: dict[str, list[dict[str, Any]]] = {}
    for j in judged:
        by_block.setdefault(j["block_key"], []).append(j)
    conc = disc = ties = 0
    mixed_blocks = 0
    for rows in by_block.values():
        if len(rows) < 2:
            continue
        u = [r for r in rows if r["judge_useful"] and r.get("render_score") is not None]
        n = [r for r in rows if not r["judge_useful"] and r.get("render_score") is not None]
        if u and n:
            mixed_blocks += 1
        for a in u:
            for b in n:
                if a["render_score"] > b["render_score"]:
                    conc += 1
                elif a["render_score"] < b["render_score"]:
                    disc += 1
                else:
                    ties += 1
    tot = conc + disc
    print(
        f"mixed-verdict blocks: {mixed_blocks}; within-block pairs: conc={conc} disc={disc} ties={ties}"
    )
    if tot:
        print(f"within-block concordance (score orders usefulness): {conc / tot:.3f}")
    pos = [float(j["render_score"]) for j in useful if j.get("render_score") is not None]
    neg = [
        float(j["render_score"])
        for j in judged
        if not j["judge_useful"] and j.get("render_score") is not None
    ]
    auc, p = mann_whitney_auc(pos, neg)
    print(f"global AUC(render_score -> useful): {auc:.3f} (p={p:.4f})")

    # ---------------- (d) volume ----------------
    print("\n=== (d) VOLUME ===")
    dataset = [json.loads(ln) for ln in (SCRATCH / "dataset.jsonl").open()]
    usable = [
        r for r in dataset if r["prompt_len"] > 20 and r["response_len"] > 80 and r.get("content")
    ]
    projects: dict[str, int] = {}
    for r in usable:
        pid = r.get("project_id") or "?"
        projects[pid] = projects.get(pid, 0) + 1
    print(f"retro rows total: {len(dataset)}; judgeable: {len(usable)}")
    print(f"distinct sessions: {len({r.get('session_id') for r in usable})}")
    print("per-project judgeable rows:", projects)
    print("joined-to-signal rows (fit-eligible):", sum(1 for r in dataset if r.get("joined")))
    print("(forward volume: see bug #17772 — ~0 delivered injections/day post-cutover)")


if __name__ == "__main__":
    main()
