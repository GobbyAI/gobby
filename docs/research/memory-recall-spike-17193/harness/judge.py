"""De-biased LLM-judge run for task #17193 (protocol 17193-v1).

Judge: gemma4:31b via local Ollama (Gemma/Google family — different family from
both generator populations: Claude and GPT/Codex).
De-biasing per docs/contracts/memory-usefulness-label.md §4:
  1. different model family        -> gemma vs claude/gpt generators
  2. position-randomized           -> block memories shuffled per row (seeded)
  3. length-controlled             -> fixed truncation budgets + explicit rubric
  4. per-memory verdict + one-line rationale stored
Sampling: block-level stratified so within-block siblings are all judged
(needed for the headroom/concordance cell).
"""

import hashlib
import json
import random
import time
import urllib.request
from pathlib import Path
from typing import Any

SCRATCH = Path(__file__).parent
JUDGE_MODEL = "gemma4:31b"
PROTOCOL = "17193-v1"
OLLAMA = "http://localhost:11434/api/chat"

PROMPT_BUDGET = 1200
CONTENT_BUDGET = 900
RESPONSE_BUDGET = 2200
TARGET_ROWS = 300

RUBRIC = """You are auditing an AI coding assistant's memory system. On a past turn, \
the assistant received a user request plus injected "project memory" notes, and \
produced a response. Judge whether ONE SPECIFIC memory (marked [TARGET]) materially \
helped the assistant produce this response.

Rules:
- "Useful" means the response uses information from the TARGET memory that is not \
already present in the user request: specific facts, file paths, commands, \
conventions, decisions, or warnings that show up in the response or clearly steered it.
- Judge causal usefulness for THIS turn only, not the general quality of the memory.
- Ignore length and verbosity everywhere. A long response does not mean the memory \
helped; a short one does not mean it did not.
- If the response would plausibly be the same without the TARGET memory, answer false.
- Overlap in generic words is NOT usefulness; look for specific transferred content \
or a steered decision.

Return ONLY JSON: {"useful": true|false, "confidence": 0.0-1.0, "rationale": "<one line>"}"""


def trunc(s: str, n: int) -> str:
    s = s or ""
    return s if len(s) <= n else s[:n] + " …[truncated]"


def block_key(r: dict[str, Any]) -> str:
    return f"{r['external_id']}:{r['line_index']}"


def call_judge(messages: list[dict[str, str]], num_predict: int = 160) -> str:
    payload = {
        "model": JUDGE_MODEL,
        "think": False,
        "stream": False,
        "options": {"temperature": 0, "num_predict": num_predict},
        "messages": messages,
        "keep_alive": "30m",
    }
    req = urllib.request.Request(
        OLLAMA,
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=600) as resp:
        data = json.loads(resp.read())
    return str(data["message"]["content"])


def parse_verdict(text: str) -> dict[str, Any] | None:
    t = text.strip()
    if t.startswith("```"):
        t = t.strip("`")
        if t.startswith("json"):
            t = t[4:]
    start, end = t.find("{"), t.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        obj = json.loads(t[start : end + 1])
    except json.JSONDecodeError:
        return None
    if not isinstance(obj, dict) or "useful" not in obj:
        return None
    return obj


def build_sample() -> list[dict[str, Any]]:
    rows = [json.loads(ln) for ln in (SCRATCH / "dataset.jsonl").open()]
    usable = [
        r for r in rows if r["prompt_len"] > 20 and r["response_len"] > 80 and r.get("content")
    ]
    # group into blocks
    blocks: dict[str, list[dict[str, Any]]] = {}
    for r in usable:
        blocks.setdefault(block_key(r), []).append(r)
    # drop giant blocks (block_size > 6): budget-blown session_start dumps
    blocks = {k: v for k, v in blocks.items() if len(v) <= 6}

    # dedupe identical (memory set, prompt) blocks
    seen: set[str] = set()
    uniq: list[tuple[str, list[dict[str, Any]]]] = []
    for k, v in sorted(blocks.items()):
        sig_src = (
            "|".join(sorted(x["memory_id"] for x in v)) + (v[0].get("user_prompt") or "")[:300]
        )
        h = hashlib.sha1(sig_src.encode(), usedforsecurity=False).hexdigest()
        if h in seen:
            continue
        seen.add(h)
        uniq.append((k, v))

    rng = random.Random(171930)

    def stratum(v: list[dict[str, Any]]) -> tuple[str, str, str]:
        vias = {x.get("render_via") for x in v}
        via = "semantic" if "semantic" in vias else "keyword"
        size = "1" if len(v) == 1 else ("2-3" if len(v) <= 3 else "4-6")
        smax = max(x.get("render_score") or 0 for x in v)
        band = "hi" if smax >= 0.9 else ("mid" if smax >= 0.75 else "lo")
        return (via, size, band)

    strata: dict[tuple[str, str, str], list[tuple[str, list[dict[str, Any]]]]] = {}
    for k, v in uniq:
        strata.setdefault(stratum(v), []).append((k, v))

    # all semantic blocks first (scarce), then proportional fill
    chosen: list[tuple[str, list[dict[str, Any]]]] = []
    n_rows = 0
    for key in sorted(strata):
        if key[0] == "semantic":
            for k, v in strata[key]:
                chosen.append((k, v))
                n_rows += len(v)
    kw_strata = {k: v for k, v in strata.items() if k[0] == "keyword"}
    total_kw_blocks = sum(len(v) for v in kw_strata.values())
    remaining = TARGET_ROWS - n_rows
    for key in sorted(kw_strata):
        bucket = kw_strata[key]
        rng.shuffle(bucket)
        share = max(1, round(remaining * len(bucket) / total_kw_blocks / 1.9))
        for k, v in bucket[:share]:
            chosen.append((k, v))
            n_rows += len(v)
    rng.shuffle(chosen)

    sample: list[dict[str, Any]] = []
    for k, v in chosen:
        for r in v:
            r = dict(r)
            r["block_key"] = k
            sample.append(r)
    print(
        f"usable rows: {len(usable)}; uniq blocks: {len(uniq)}; "
        f"sampled blocks: {len(chosen)}; sampled rows: {len(sample)}"
    )
    return sample


def judge_row(r: dict[str, Any], siblings: list[dict[str, Any]]) -> dict[str, Any]:
    rng = random.Random(f"{r['block_key']}:{r['memory_id']}")
    entries = list(siblings)
    rng.shuffle(entries)
    mem_lines = []
    for e in entries:
        tag = " [TARGET]" if e["memory_id"] == r["memory_id"] else ""
        mem_lines.append(f"- {trunc(e['content'], CONTENT_BUDGET)}{tag}")
    user = (
        f"USER REQUEST (truncated):\n{trunc(r['user_prompt'], PROMPT_BUDGET)}\n\n"
        f"INJECTED MEMORIES (order shuffled; judge the one marked [TARGET]):\n"
        + "\n".join(mem_lines)
        + f"\n\nASSISTANT RESPONSE (truncated):\n{trunc(r['assistant_response'], RESPONSE_BUDGET)}"
    )
    raw = call_judge([{"role": "system", "content": RUBRIC}, {"role": "user", "content": user}])
    verdict = parse_verdict(raw)
    out = {
        "block_key": r["block_key"],
        "memory_id": r["memory_id"],
        "external_id": r["external_id"],
        "session_id": r.get("session_id"),
        "project_id": r.get("project_id"),
        "source": r.get("source"),
        "timestamp": r.get("timestamp"),
        "render_score": r.get("render_score"),
        "render_via": r.get("render_via"),
        "block_position": r.get("block_position"),
        "block_size": r.get("block_size"),
        "referenced_overlap": r.get("referenced_overlap"),
        "content_len": r.get("content_len"),
        "response_len": r.get("response_len"),
        "judge_model": JUDGE_MODEL,
        "judge_protocol_version": PROTOCOL,
        "position_randomized": True,
        "length_controlled": True,
        "raw": raw if verdict is None else None,
    }
    if verdict is None:
        out["judge_useful"] = None
    else:
        out["judge_useful"] = bool(verdict.get("useful"))
        try:
            out["judge_confidence"] = float(verdict.get("confidence"))
        except (TypeError, ValueError):
            out["judge_confidence"] = None
        out["rationale"] = str(verdict.get("rationale", ""))[:300]
    return out


def main() -> None:
    sample = build_sample()
    (SCRATCH / "judge_sample.jsonl").write_text("".join(json.dumps(r) + "\n" for r in sample))
    by_block: dict[str, list[dict[str, Any]]] = {}
    for r in sample:
        by_block.setdefault(r["block_key"], []).append(r)

    out_path = SCRATCH / "judgments.jsonl"
    done: set[tuple[str, str]] = set()
    if out_path.exists():
        for ln in out_path.open():
            j = json.loads(ln)
            done.add((j["block_key"], j["memory_id"]))
    print(f"already judged: {len(done)}")

    t0 = time.time()
    n = 0
    with out_path.open("a") as f:
        for r in sample:
            key = (r["block_key"], r["memory_id"])
            if key in done:
                continue
            try:
                res = judge_row(r, by_block[r["block_key"]])
            except Exception as e:  # noqa: BLE001 — checkpointing loop must survive
                print(f"ERROR on {key}: {e}", flush=True)
                time.sleep(5)
                continue
            f.write(json.dumps(res) + "\n")
            f.flush()
            n += 1
            if n % 10 == 0:
                rate = (time.time() - t0) / n
                left = (len(sample) - len(done) - n) * rate / 60
                print(f"judged {n} (+{len(done)}) rate={rate:.1f}s/row eta={left:.0f}m", flush=True)
    print(f"DONE: judged {n} new rows in {(time.time() - t0) / 60:.1f} min")


if __name__ == "__main__":
    main()
