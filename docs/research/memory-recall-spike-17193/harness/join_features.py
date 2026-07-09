"""Join harvested injection rows to recall-signal features; profile the dataset."""

import json
import re
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

SCRATCH = Path(__file__).parent

WORD_RE = re.compile(r"[a-zA-Z_][a-zA-Z0-9_./-]{3,}")


def toks(text: str) -> set[str]:
    return {w.lower() for w in WORD_RE.findall(text)}


def parse_ts(ts: str | None) -> datetime | None:
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None


def main() -> None:
    sig_path = Path.home() / ".gobby" / "logs" / "recall_signal.jsonl"
    # Index signal hits by (session_id, memory_id)
    hit_index: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for line in sig_path.open():
        ev = json.loads(line)
        sid = ev.get("session_id")
        if not sid:
            continue
        w = ev.get("weighting") or {}
        regime = "weighted" if w.get("graph_edge_weighting") else "unweighted"
        for hit in ev.get("hits") or []:
            key = (sid, hit["memory_id"])
            hit_index.setdefault(key, []).append(
                {
                    "caller": ev.get("caller"),
                    "recall_request_id": ev.get("recall_request_id"),
                    "event_ts": ev.get("timestamp"),
                    "rrf_applied": ev.get("rrf_applied"),
                    "regime": regime,
                    "weighting": w,
                    **hit,
                }
            )

    rows = [json.loads(ln) for ln in (SCRATCH / "injections.jsonl").open()]

    joined = 0
    join_caller = Counter()
    for r in rows:
        r["overlap_pre"] = None
        r["joined"] = False
        sid = r.get("session_id")
        mid = r["memory_id"]
        rts = parse_ts(r.get("timestamp"))
        best = None
        if sid:
            for cand in hit_index.get((sid, mid), []):
                # score match: rendered score is similarity rounded to 4dp
                sim = cand.get("similarity")
                rs = r.get("render_score")
                score_ok = (
                    rs is not None and sim is not None and abs(sim - rs) < 5e-4
                ) or rs is None
                ets = parse_ts(cand.get("event_ts"))
                dt = abs((rts - ets).total_seconds()) if rts and ets else None
                time_ok = dt is not None and dt < 1800
                if score_ok and time_ok:
                    if best is None or (dt or 0) < (best[0] or 0):
                        best = (dt, cand)
        if best:
            cand = best[1]
            r["joined"] = True
            joined += 1
            join_caller[cand["caller"]] += 1
            for k in (
                "caller",
                "recall_request_id",
                "rrf_applied",
                "regime",
                "rank",
                "search_via",
                "similarity",
                "raw_semantic_score",
                "temporal_decay_factor",
                "ranking_score",
                "ranking_mode",
                "graph_score",
            ):
                r[f"sig_{k}"] = cand.get(k)
            r["sig_join_dt_seconds"] = best[0]

        # transcript-derived features (feature_extractor_version retro-v1)
        mem_t = toks(r.get("content", ""))
        resp_t = toks(r.get("assistant_response", ""))
        r["referenced_overlap"] = len(mem_t & resp_t) / len(mem_t) if mem_t else None
        r["prompt_len"] = len(r.get("user_prompt") or "")
        r["response_len"] = len(r.get("assistant_response") or "")
        r["content_len"] = len(r.get("content") or "")

    print(f"rows: {len(rows)}; joined to signal hit: {joined}")
    print("joined by caller:", dict(join_caller))
    print("by source:", dict(Counter(r.get("source") for r in rows)))
    print("by format:", dict(Counter(r.get("format") for r in rows)))
    print(
        "render_score present:",
        sum(1 for r in rows if r.get("render_score") is not None),
    )
    print(
        "usable turn (prompt & response non-empty):",
        sum(1 for r in rows if r["prompt_len"] > 0 and r["response_len"] > 0),
    )
    print(
        "usable + joined:",
        sum(1 for r in rows if r["prompt_len"] > 0 and r["response_len"] > 0 and r["joined"]),
    )
    print("joined regimes:", dict(Counter(r.get("sig_regime") for r in rows if r["joined"])))
    print(
        "joined by via:",
        dict(Counter(r.get("sig_search_via") for r in rows if r["joined"])),
    )
    ts_vals = sorted(r["timestamp"] for r in rows if r.get("timestamp"))
    if ts_vals:
        print("time range:", ts_vals[0], "..", ts_vals[-1])
    monthly = Counter(str(r.get("timestamp"))[:7] for r in rows)
    print("rows by month:", dict(sorted(monthly.items())))
    dedup = {(r["memory_id"], (r.get("user_prompt") or "")[:200]) for r in rows}
    print("distinct (memory_id, prompt) pairs:", len(dedup))

    out = SCRATCH / "dataset.jsonl"
    with out.open("w") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")
    print(f"wrote {out}")


if __name__ == "__main__":
    main()
