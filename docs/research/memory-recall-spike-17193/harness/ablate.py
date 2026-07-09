"""Ablation/LOO calibration for task #17193 (ablation_method=loo_regen_judge).

For a stratified subsample of judged rows: regenerate the turn through the judge
model with and without the target memory (other block memories present in both
arms), grade both regens against the real assistant response with an identical
rubric (self-preference cancels in the delta), and record
ablation_delta = score_with - score_without (positive = memory helped).
"""

import json
import random
import re
import time
from pathlib import Path
from typing import Any

from judge import (  # noqa: E402 — sibling module in scratchpad
    CONTENT_BUDGET,
    PROMPT_BUDGET,
    RESPONSE_BUDGET,
    call_judge,
    trunc,
)

SCRATCH = Path(__file__).parent
N_PER_ARM = 20  # target ablation rows per judge-verdict arm (useful / not useful)

REGEN_SYS = (
    "You are an AI coding assistant. Given a user request and project memory notes, "
    "describe concisely how you would respond: the key facts you would state, files or "
    "commands you would use, decisions you would make, and any cautions you would raise. "
    "At most 200 words. Do not pad."
)

GRADE_SYS = (
    "You compare a CANDIDATE plan against a REFERENCE (what the assistant actually did). "
    "Score 0-10 how well the CANDIDATE anticipates the REFERENCE's key specific content: "
    "concrete facts, file paths, commands, decisions, cautions. Ignore length, style, and "
    "generic statements; only specific matching content counts. "
    'Return ONLY JSON: {"score": <0-10>}'
)


def regen(prompt: str, memories: list[str]) -> str:
    mems = "\n".join(f"- {trunc(m, CONTENT_BUDGET)}" for m in memories)
    if not memories:
        mems = "(none)"
    user = (
        f"USER REQUEST (truncated):\n{trunc(prompt, PROMPT_BUDGET)}\n\n"
        f"PROJECT MEMORY NOTES:\n{mems}\n\nYour concise response plan:"
    )
    return call_judge(
        [{"role": "system", "content": REGEN_SYS}, {"role": "user", "content": user}],
        num_predict=320,
    )


def grade(candidate: str, reference: str) -> float | None:
    user = (
        f"REFERENCE (truncated):\n{trunc(reference, RESPONSE_BUDGET)}\n\n"
        f"CANDIDATE:\n{trunc(candidate, 1800)}\n\nScore JSON:"
    )
    raw = call_judge(
        [{"role": "system", "content": GRADE_SYS}, {"role": "user", "content": user}],
        num_predict=40,
    )
    m = re.search(r'"score"\s*:\s*([0-9.]+)', raw)
    if not m:
        return None
    try:
        return float(m.group(1))
    except ValueError:
        return None


def main() -> None:
    judgments = [json.loads(ln) for ln in (SCRATCH / "judgments.jsonl").open()]
    sample_rows = [json.loads(ln) for ln in (SCRATCH / "judge_sample.jsonl").open()]
    row_by_key = {(r["block_key"], r["memory_id"]): r for r in sample_rows}
    block_rows: dict[str, list[dict[str, Any]]] = {}
    for r in sample_rows:
        block_rows.setdefault(r["block_key"], []).append(r)

    eligible = [
        j
        for j in judgments
        if j.get("judge_useful") is not None
        and (row_by_key.get((j["block_key"], j["memory_id"])) or {}).get("response_len", 0) >= 200
    ]
    rng = random.Random(171931)
    useful = [j for j in eligible if j["judge_useful"]]
    not_useful = [j for j in eligible if not j["judge_useful"]]
    rng.shuffle(useful)
    rng.shuffle(not_useful)
    todo = useful[:N_PER_ARM] + not_useful[:N_PER_ARM]
    rng.shuffle(todo)
    print(
        f"eligible: {len(eligible)} (useful={len(useful)}, not={len(not_useful)}); ablating {len(todo)}"
    )

    out_path = SCRATCH / "ablations.jsonl"
    done: set[tuple[str, str]] = set()
    if out_path.exists():
        for ln in out_path.open():
            a = json.loads(ln)
            done.add((a["block_key"], a["memory_id"]))

    t0 = time.time()
    n = 0
    with out_path.open("a") as f:
        for j in todo:
            key = (j["block_key"], j["memory_id"])
            if key in done:
                continue
            r = row_by_key[key]
            siblings = block_rows[j["block_key"]]
            mems_with = [x["content"] for x in siblings]
            mems_without = [x["content"] for x in siblings if x["memory_id"] != j["memory_id"]]
            try:
                regen_with = regen(r["user_prompt"], mems_with)
                regen_without = regen(r["user_prompt"], mems_without)
                score_with = grade(regen_with, r["assistant_response"])
                score_without = grade(regen_without, r["assistant_response"])
            except Exception as e:  # noqa: BLE001 — checkpointing loop must survive
                print(f"ERROR on {key}: {e}", flush=True)
                time.sleep(5)
                continue
            delta = (
                score_with - score_without
                if score_with is not None and score_without is not None
                else None
            )
            f.write(
                json.dumps(
                    {
                        "block_key": j["block_key"],
                        "memory_id": j["memory_id"],
                        "judge_useful": j["judge_useful"],
                        "judge_confidence": j.get("judge_confidence"),
                        "score_with": score_with,
                        "score_without": score_without,
                        "ablation_delta": delta,
                        "ablation_method": "loo_regen_judge",
                        "judge_model": j["judge_model"],
                        "judge_protocol_version": j["judge_protocol_version"],
                    }
                )
                + "\n"
            )
            f.flush()
            n += 1
            if n % 5 == 0:
                rate = (time.time() - t0) / n
                print(
                    f"ablated {n}/{len(todo)} rate={rate:.0f}s/row "
                    f"eta={(len(todo) - n) * rate / 60:.0f}m",
                    flush=True,
                )
    print(f"DONE: {n} ablations in {(time.time() - t0) / 60:.1f} min")


if __name__ == "__main__":
    main()
