"""Export the committed calibration dataset for task #17193.

One JSON line per judged row (labels + features + provenance), joined with
ablation results where present. Raw prompt/response/memory text is NOT
exported — only lengths, hashes, and derived features — so the committed
artifact carries no transcript content.
"""

import hashlib
import json
from pathlib import Path

SCRATCH = Path(__file__).parent
OUT = Path(
    "/Users/josh/Projects/gobby/docs/research/memory-recall-spike-17193/calibration-dataset.jsonl"
)

KEEP = [
    "block_key",
    "memory_id",
    "external_id",
    "session_id",
    "project_id",
    "source",
    "timestamp",
    "render_score",
    "render_via",
    "block_position",
    "block_size",
    "referenced_overlap",
    "content_len",
    "response_len",
    "judge_model",
    "judge_protocol_version",
    "position_randomized",
    "length_controlled",
    "judge_useful",
    "judge_confidence",
    "rationale",
]


def main() -> None:
    judgments = [json.loads(ln) for ln in (SCRATCH / "judgments.jsonl").open()]
    abl_by_key: dict[tuple[str, str], dict] = {}
    ab_path = SCRATCH / "ablations.jsonl"
    if ab_path.exists():
        for ln in ab_path.open():
            a = json.loads(ln)
            abl_by_key[(a["block_key"], a["memory_id"])] = a

    sample = {
        (r["block_key"], r["memory_id"]): r
        for r in (json.loads(ln) for ln in (SCRATCH / "judge_sample.jsonl").open())
    }

    n_ab = 0
    with OUT.open("w") as f:
        for j in judgments:
            if j.get("judge_useful") is None:
                continue
            row = {k: j.get(k) for k in KEEP}
            # synthetic retro id per contract §6 (no recall_request_id exists retro)
            row["retro_id"] = f"retro:{j.get('session_id')}:{j['block_key'].split(':')[-1]}"
            row["label_source"] = "llm_judge"
            row["feature_extractor_version"] = "retro-v1"
            src = sample.get((j["block_key"], j["memory_id"]))
            if src:
                row["prompt_sha1"] = hashlib.sha1(
                    (src.get("user_prompt") or "").encode(), usedforsecurity=False
                ).hexdigest()[:16]
                row["response_sha1"] = hashlib.sha1(
                    (src.get("assistant_response") or "").encode(), usedforsecurity=False
                ).hexdigest()[:16]
            a = abl_by_key.get((j["block_key"], j["memory_id"]))
            if a:
                n_ab += 1
                row["ablation_delta"] = a.get("ablation_delta")
                row["ablation_method"] = a.get("ablation_method")
                row["ablation_score_with"] = a.get("score_with")
                row["ablation_score_without"] = a.get("score_without")
            f.write(json.dumps(row) + "\n")
    print(f"wrote {OUT} ({sum(1 for _ in OUT.open())} rows, {n_ab} with ablation)")


if __name__ == "__main__":
    main()
