# Spike #17193 — Retrospective LLM-judge + ablation calibration (GO/NO-GO)

Status: **complete** (2026-07-09). Decision: see §6.
Epic: #17099 Phase 0b. Label contract: `docs/contracts/memory-usefulness-label.md` (#17192).
Prior art: `docs/research/memory-recall-adaptive-tuning.md` (#17194).

This spike answers: can we produce a **defensible usefulness label** for injected
memories from real transcripts, does any ranker component **correlate** with that
label, is there **headroom** over the static constants, and is there **volume**
to fit on? It is the epic's kill switch: a failed label or a no-signal result
stops the epic before any heavy plumbing.

## 1. Method

**Harvest.** All local transcript sources were scanned (84,674 candidate files
deduped by CLI `external_id`): the normalized store (`~/.gobby/session_transcripts`,
3,924 gz files), raw Claude Code projects (`~/.claude/projects/*/*.jsonl`), and raw
Codex rollouts (`~/.codex/sessions/**/rollout-*.jsonl`). The parser extracts
rendered `<project-memory>` blocks (all three renderer call sites emit the same
format via `format_memory_metadata_suffix`), the per-entry
`(memory_id, score, via)` suffix, the turn's user prompt, and the assistant
response for the turn. Rows were mapped to platform sessions through
`sessions.external_id` (per contract §2 the platform UUID is the join key).

**Result:** 1,683 injected-memory rows across 408 transcripts / 402 sessions /
2 projects; 1,673 mapped to platform sessions. Every row predates the
2026-07-02T05:19Z weighted-regime cutover, so the whole retro corpus is
single-regime (unweighted; hook path keyword-degraded per #17491) — no regime
pooling occurs (contract §7). Via mix: 1,599 keyword / 84 semantic.

**Judge (protocol `17193-v1`).** gemma4:31b via local Ollama, satisfying the
contract §4 de-biasing requirements:

1. **Different model family** — Gemma vs both generator populations (Claude
   n=879 rows, GPT/Codex n=794).
2. **Position-randomized** — block sibling order shuffled per row (seeded);
   target marked explicitly.
3. **Length-controlled** — fixed truncation budgets (prompt 1200 / memory 900 /
   response 2200 chars) plus an explicit anti-verbosity rubric; verified
   empirically below (response-length AUC ≈ 0.46, i.e. no long-response bias).
4. Per-memory verdict with one-line rationale, stored per row.

**Sample.** Block-level stratified (via × block-size × score-band), keeping all
within-block siblings together (required for the headroom cell): 163 blocks /
299 rows judged, 0 parse failures. Judged base rate: **20.4% useful**
(claude 20.7%, codex 20.4% — no generator-family artifact).

**Ablation calibration (`loo_regen_judge`).** On a verdict-stratified subsample
(20 judge-useful / 20 judge-not), the turn was regenerated through the judge
model with and without the target memory (siblings present in both arms), and
both regens were graded against the real assistant response under an identical
rubric (self-preference cancels in the delta). `ablation_delta =
score_with − score_without`, positive = memory helped.

## 2. Pipeline-health findings (outside the matrix, load-bearing)

Two defects found while measuring, filed as **bug #17772** (P1, under #17099):

1. **Delivery is broken post-#17491.** `caller="memory.recall"` signal events
   flow (262 events / 2,104 hits over 8 days, 96 on 2026-07-09 alone) but
   rendered `<project-memory>` deliveries stopped: the last rendered injection
   in any local transcript is 2026-07-02T05:53Z. The three heaviest
   post-cutover recall sessions (24/14/13 events) show 0/0/1 rendered blocks.
2. **Hook search is still keyword-degraded.** Post-cutover hook-path hits are
   98.6% pure keyword (2,074/2,104) with `similarity=null`; only 30 hits carry
   any semantic/graph via. The MCP `search_memories` path is healthy
   (semantic/graph vias present), so embeddings/Qdrant are fine — the
   degradation is specific to the hook recall path.

Consequence: **fit-eligible rows (label ⋈ features on the §2 key) = 0 today**,
and forward volume is ~0/day until #17772 is fixed. Even with delivery fixed,
forward rows would carry no semantic/graph feature variation while symptom 2
persists — there would be nothing for #17198 to re-fit.

## 3. Decision matrix

### (a) Label validity — judge ↔ ablation agreement: **provisional pass**

40 calibration rows (20 judge-useful / 20 judge-not), `loo_regen_judge`:

| Metric | Value |
| --- | --- |
| mean `ablation_delta` \| judge_useful=true | **+0.75** |
| mean `ablation_delta` \| judge_useful=false | **+0.00** |
| positive-delta rate (useful vs not) | 45% vs 20% |
| sign agreement on nonzero deltas (n=21) | 0.619 |
| point-biserial r(judge, delta) | +0.240 |
| permutation p (two-sided / one-sided) | 0.168 / 0.081 |

The relationship is directionally correct with a moderate effect, but
underpowered: 19/40 rows produced a zero delta (the 200-word regen sketch often
does not change when one memory is removed), diluting the test. The STOP
condition — judge fails calibration, label unusable — is **not met**: there is
no inversion and no null split. The pass is **provisional**: recalibration at
larger n (and preferably a logprob-based LOO instead of regen ties) is
**mandatory before #17198 consumes judge labels for fitting** — this lands
naturally in #17197's harness scope as forward rows accrue.

### (b) Component signal: **weak but non-zero; informative components unobserved**

Association with `judge_useful` (Mann-Whitney AUC; point-biserial r):

| Feature (n=299) | AUC | p | r_pb | Note |
| --- | --- | --- | --- | --- |
| `render_score` (ranker similarity) | 0.552 | 0.21 | +0.08 | 0.631 (p=0.013) on the confidence=1.0 subset (n=223) |
| `render_score`, via=keyword only (n=220) | 0.501 | 0.98 | +0.01 | keyword-regime score is noise |
| `block_position` | 0.478 | 0.60 | −0.04 | no position effect |
| `block_size` | 0.544 | 0.29 | −0.00 | none |
| `referenced_overlap` (feature, never target) | 0.592 | 0.027 | +0.13 | 0.647 (p=0.001) within keyword |
| `content_len` | 0.495 | 0.90 | +0.07 | none |
| `response_len` (judge bias check) | 0.455 | 0.28 | −0.08 | judge not length-biased ✓ |

Reading: the **keyword-regime score carries essentially no usefulness signal**;
weak signal appears only in the pooled/confident subsets. `referenced_overlap`
carrying the strongest signal is consistent with a valid label (content that was
actually transferred into the response gets judged useful) while remaining
banned as a training target (§1 of the contract). The components the epic
actually wants to fit — semantic similarity, graph/edge weights, temporal decay
— are **unobservable in the retro corpus** (keyword-degraded era) and currently
absent from the forward stream (bug #17772, symptom 2). Cell (b) therefore
reads: *no evidence of strong component signal, no evidence of absence for the
target components.*

### (c) Headroom: **wide open**

- Within-block concordance (does the rendered score order a judged-useful
  memory above a judged-useless sibling?): **0.383** (18 concordant / 29
  discordant across 26 mixed-verdict blocks) — at-or-below chance.
- Global AUC(render_score → useful): **0.552**.

The static ranking does not meaningfully order candidates by usefulness. This
is the opposite of the "static already optimal" STOP condition — a fitted
ranker has room to improve, and even the trivial re-ranking baseline
("demote never-referenced memory types") would be hard to underperform.

### (d) Volume: **retro sufficient for calibration; forward volume zero and blocked**

| Population | Count |
| --- | --- |
| Retro judgeable rows (prompt+response present) | 1,554 |
| … per project | 877 (gobby) / 667 (second project) / 10 unmapped |
| Distinct sessions | 388 |
| Judged this spike | 299 (+ ablation 40) |
| **Fit-eligible rows (label ⋈ signal features)** | **0** |
| Forward delivered injections/day (post-cutover) | ~0 (bug #17772) |
| Post-cutover recall searches that *would* be labelable | ~33 events/day (262/8 days) |

Retro rows use synthetic ids (`retro:<session_id>:<turn_seq>`, contract §6) and
never join the signal table — they are calibration/volume evidence only, by
design. The fit itself (#17198) needs forward rows, which do not exist and
cannot accrue until #17772 is fixed.

## 4. Sensitivity checks

- Judge confidence is near-binary (223 rows at 1.0, 76 at 0.9); the
  confidence=1.0 subset strengthens score signal (AUC 0.631) without changing
  any conclusion.
- Base rates match across generator families (20.7% vs 20.4%) — no
  self/family-preference artifact detectable in verdicts.
- Semantic-via rows (n=79, single project era) show *lower* usefulness (8.9%)
  than keyword rows (24.5%); confounded with delivery channel and project —
  reported, not interpreted.

## 5. Artifacts

- `calibration-dataset.jsonl` — one line per judged row: labels
  (`judge_useful`, `judge_confidence`, rationale), features (`render_score`,
  `render_via`, `block_position`, `block_size`, `referenced_overlap`,
  lengths), provenance (session/project/source/timestamp, synthetic
  `retro_id`), protocol fields, and `ablation_*` where run. No transcript text
  is included (prompt/response are SHA-1 fingerprints).
- `harness/` — the exact scripts that produced everything: `harvest.py`
  (transcript → injection rows), `join_features.py` (signal join + features),
  `judge.py` (protocol 17193-v1), `ablate.py` (loo_regen_judge), `analyze.py`
  (this matrix), `export_dataset.py`.
- Reproduction: scripts read live local stores (`~/.gobby`, `~/.claude`,
  `~/.codex`) and a local Ollama; they are research artifacts, not product
  code.

## 6. Decision: **GO — conditional**

Applying the gate (GO only if (a) ∧ (b) ∧ (c) ∧ (d)):

- **(a) PASS (provisional).** The label is directionally valid and not
  refuted; recalibration at larger n is mandatory before any fit consumes
  judge labels (#17197 scope).
- **(b) PASS (weak, with a hole).** Signal exists (score AUC 0.63 on confident
  verdicts; overlap AUC 0.65) but the in-regime keyword score is noise, and the
  components the fit targets (semantic/graph/decay) are unobserved — they
  cannot be measured until #17772 is fixed. No STOP: "no component carries
  signal" is not established, and what *is* established (near-zero signal in
  the shipped ranking) is an argument for the epic, not against it.
- **(c) PASS decisively.** Concordance 0.383 / AUC 0.552: the static ranking
  does not order usefulness; only ~20% of injected memories helped their turn.
  Enormous headroom; the "static already optimal" STOP is excluded.
- **(d) PASS for calibration, BLOCKED for fitting.** 1,554 retro rows support
  judge calibration and volume checks (this spike). Fit-eligible rows are 0
  and forward accrual is 0/day until **#17772** lands.

**Conditions attached to the GO:**

1. **#17772 (P1) is a hard prerequisite** for #17197/#17198 — no fit data
   exists or can accrue before the recall delivery + hook-search-degradation
   bugs are fixed.
2. **Judge recalibration at larger n** (forward rows, stronger LOO) before
   #17198 fits on judge labels.
3. #17195/#17196 proceed now — they are exactly the machinery that creates
   forward labeled volume, and the label spec they implement is validated
   here.

The epic's kill switch does not fire: the label methodology works, and the
measured weakness of the current static ranking is the strongest evidence yet
that fitting recall constants from usefulness labels is worth the plumbing.
