# Digest-Shadow Recall Judge Validation — Round 2 Protocol

**Version:** 1.0 (predeclared; see §14 Amendments)
**Date:** 2026-08-08
**Status:** Protocol locked before any round-2 labeling. No labels collected yet.
**Prior round:** `digest-shadow-recall-research-results-r1.md` (gate failed 38/50; round-1 sample is burned as a dev set and is reused here only as rubric material).

This protocol governs the second validation round of the digest-shadow shadow-judge experiment and the redefined stage-2 ranker evaluation. Every gate, sample size, and decision rule below is fixed before data collection. Deviations after the first label is recorded require a dated amendment (§14).

## 1. Purpose

Round 1 failed its gate in a way that was uninformative: n=50 raw agreement against a single un-calibrated human, with a threshold stricter than the sample could support. Round 2 answers the same core question — is an LLM judge safe to use as the label source for fitting recall-ranking constants — with a boundary-consistent gate, consensus human labels, a multi-model judge panel, direct measurement of pairwise noise, and a non-circular stage-2 evaluation.

## 2. Fixed design decisions and rationale

1. **Single primary gate, boundary-consistent.** The gate boundary, the sample size, and the decision rule all derive from the same null boundary p₀ = 0.75 (§7). Round 1's defect — powering for one boundary and gating at another — is prohibited.
2. **Boundary p₀ = 0.75, alternative p₁ = 0.85.** The audit's job is to screen out unusable judges; end-to-end safety is carried by stage 2, whose primary metric is human-labeled (§12). Against *consensus* human labels (which remove single-rater noise), a judge at true agreement ≥ 0.85 is consistent with GPT-4o-class judge performance on binary relevance (UMBRELA binary κ 0.42–0.50); demanding confidence of ≥ 0.80–0.90 would push the bar at or above the human–human ceiling.
3. **Bonferroni judge panel instead of a confirmation round.** All panel models are evaluated on the same human-labeled sample, each gated at α = 0.05/3. Family-wise error (probability any model at true p ≤ 0.75 passes) stays ≤ 0.05, so choosing among passing models introduces no uncontrolled selection bias and no second human-labeled confirmation sample is needed.
4. **κ and pairwise noise are descriptive in round 2.** Stacking conjunctive gates destroys joint power. Chance-corrected and pair-level metrics are computed and reported (§8, §9) and are candidates for promotion to gates in round 3 once their empirical distributions are known. One soft sanity floor applies (§7.4).
5. **Stage 2 is non-circular.** Training-label judges never gate shipping. The primary stage-2 metric is a human-labeled holdout slice; cross-family judge evaluation is secondary; same-judge holdout evaluation is ablation only (§12).
6. **Abstention is a first-class label.** Humans and judges may answer "cannot determine from text alone." Context-poor queries are stratified, and abstain-consensus items leave the primary denominator (§10).

## 3. Cohort and sampling

**Population.** The frozen cohort of `digest-shadow-recall-research-results-r1.md`: data/completion cutoffs `2026-08-08T07:44:45Z`, label source `digest_shadow`, candidate scope `full`; training split 779 requests (669 mixed), holdout split 779 requests (647 mixed, sealed).

**Exclusions.** The 50 round-1 audit requests are burned (dev set; reused only as rubric examples, §4). All round-2 training-side samples draw from the remaining 729 training requests.

**Samples.** All draws are deterministic and hash-bound (request-ID hash order under a per-sample salt); the selected request IDs and content hashes are frozen and recorded before any labeling.

| Sample | Salt | Source | Size | Unit |
| --- | --- | --- | --- | --- |
| Main agreement sample | `r2-main` | training requests minus burned 50 | 190 requests, one candidate each (target ≥ 170 scoreable) | request–candidate pair |
| Backfill blocks | `r2-backfill-<k>` | same pool, disjoint | blocks of 10, drawn in order until ≥ 170 scoreable or pool exhausted | request–candidate pair |
| Second-rater overlap | first 60 of `r2-main` in draw order | subset of main sample | 60 items | request–candidate pair |
| Full-request subset | `r2-fullreq` | mixed training requests, disjoint from main sample and burned 50 | 40 requests, all shown candidates (~240 labels) | request |
| Stage-2 human slice | `r2-holdout-slice` | sealed holdout, mixed requests | 40 requests, all shown candidates (~240 labels); drawn only at §12 time | request |

Candidate-within-request selection for the main sample is uniform by hash over the request's shown candidates (recorded), so the audit sample is not biased toward high-scoring positions.

## 4. Rubric

A single anchored rubric is shared verbatim by human raters and all judge models:

- **`y`** — the memory materially helps answer or act on this query: it supplies a fact, constraint, pointer, or decision the assistant would otherwise lack or plausibly get wrong.
- **`n`** — the memory does not materially help: redundant with the query itself, off-topic, stale, or too generic to change the response.
- **`abstain`** — a careful reader cannot determine relevance from the query and memory text alone (query is a fragment, notification boilerplate, or otherwise underdetermined).

The rubric document includes 5–10 worked positive and negative examples plus at least 2 abstain examples, all drawn from the burned round-1 sample (already dev, never reused for measurement). The rubric is finalized before labeling and its content hash is recorded in the protocol registry.

## 5. Raters and consensus labels

**Primary design (two raters).** Rater 1 labels every item. Rater 2 labels the 60-item overlap subset. Inter-rater agreement (raw and Cohen's κ) on the overlap is the measured human ceiling. Overlap disagreements are adjudicated by joint review to a consensus label; outside the overlap, rater 1's label is the consensus label.

**Solo fallback (predeclared, used only if no second rater is available).** Rater 1 relabels the 60-item overlap ≥ 14 days after the first pass, blinded to first-pass answers (test–retest). Stable items keep their label; unstable items get a third blinded pass ≥ 7 days later, majority of three passes wins. Test–retest agreement replaces inter-rater agreement as the reported ceiling.

**Blinding.** As in round 1: raters see only the stored query and the exact stored memory excerpt; judge verdicts stay hidden until all human labeling in a sample is complete. Human labeling of the main sample finishes before any round-2 judge output is inspected.

## 6. Judge panel and protocol v2

Panel (3 models): **Haiku** (incumbent), **Sonnet**, **Opus**.

All three run under a new judge protocol `digest-shadow-query-relevance-v2`: identical presentation format to v1 plus the §4 rubric and the abstain option. Each model's generation-config fingerprint is recorded. Each model judges the identical frozen presentations for the main sample, the full-request subset, and (at §12 time) the stage-2 slice. The live shadow judge remains Haiku/v1 and is unaffected; its labels are not used for fitting (§11).

## 7. Primary endpoint and gate

**7.1 Endpoint.** Per-model agreement with consensus human labels on *scoreable* items of the main sample. Scoreable = consensus label is `y` or `n`. A judge `abstain` on a scoreable item counts as non-agreement. Items with consensus `abstain` are excluded from the denominator and reported under §10.

**7.2 Hypotheses.** Per model: H₀: p ≤ 0.75 vs H₁: p ≥ 0.85, one-sided.

**7.3 Test and gate.** Exact one-sided binomial test of H₀ at α = 0.05/3 ≈ 0.0167 per model (Bonferroni over the 3-model panel). Equivalent statement: one-sided Wilson lower bound (z ≈ 2.128) ≥ 0.75. At n = 170 scoreable items the critical region is approximately ≥ 141/170 agreements (the exact binomial critical value is computed at analysis time; the test is the gate, the approximate count is illustrative). Power at true p = 0.85 is ≈ 0.80.

**Pass set** = models rejecting H₀. **Selected judge** = the cheapest model in the pass set (order: Haiku < Sonnet < Opus). Empty pass set = round 2 fails; proceed to §13.

**7.4 Sanity floor (soft).** If the selected judge's κ against consensus is below 0.20 ("slight" agreement) despite passing, selection halts and the result is escalated for review rather than shipped. This is a stop-and-look tripwire, and it is the only non-primary condition that can block selection.

**7.5 Scoreable-count shortfall.** If scoreable items end below 170 after backfill exhaustion, the gate still runs on what exists; the analysis reports achieved power at the realized n. No threshold is adjusted post hoc.

## 8. Secondary analyses (descriptive, no gates)

Reported per model against consensus labels:

- Confusion matrix, per-class precision/recall/F1, Cohen's κ.
- Human ceiling: inter-rater (or test–retest) raw agreement and κ on the overlap subset.
- Disagreement taxonomy on all judge–consensus disagreements: judge clearly wrong / human label arguably wrong / inherently ambiguous.
- Exploratory bias probe: logistic regression of disagreement on memory length, lexical overlap with query, memory age, and memory source type. Labeled exploratory; round-2 n supports direction-finding only.
- All of the above stratified by context-rich vs context-poor (§10).

Round-1 retro-diagnostics (confusion matrix, κ, taxonomy on the original 50) are computed alongside, reported separately, and never pooled with round-2 measurements.

## 9. Full-request subset: pairwise noise

For the 40 fully human-labeled requests, per model:

- **Pairwise noise** = fraction of within-request (consensus-`y`, consensus-`n`) pairs that the model's labels invert or destroy (either endpoint mislabeled or abstained).
- Confidence intervals by request-clustered bootstrap (resample requests, 10,000 draws) — within-request label errors are correlated by construction, so item-level inference is invalid here.
- Reported alongside the marginal agreement rate to quantify how much per-item metrics understate pair-level corruption.

Descriptive in round 2; candidate gate for round 3.

## 10. Context-poor queries and abstention

**Flagging heuristic (frozen before labeling):** a request is context-poor if its query is under 40 characters after whitespace collapse, or matches stored notification/boilerplate patterns (task-notification prefixes, bare file paths, tool-output fragments). The pattern list is recorded with the rubric hash.

**Handling:**

- All §7–§9 metrics are reported overall and per stratum (rich/poor).
- Consensus-`abstain` items: excluded from the primary denominator; their count and stratum are reported.
- **Training exclusion rule (predeclared):** when fitting (§11), pairs from requests that are context-poor-flagged, and pairs touching any candidate the selected judge abstained on, are excluded from the training set.

## 11. Post-gate actions

If the pass set is non-empty:

1. Batch re-label all 669 mixed training requests with the **selected judge** under protocol v2, offline, single pass, fingerprint recorded.
2. Regenerate training pairs from those labels with the §10 exclusion rule.
3. Fit the ranking constants per the existing fit-digest contract (holdout identity continues to exclude tunable fit settings; the fit digest includes them).
4. Keep the original Haiku/v1 labels stored side-by-side for the noisy-vs-clean comparison in §12.4.

The live Haiku shadow judge is retained for diagnostics only; its labels never enter fitting.

## 12. Stage 2: ranker evaluation (redefined, non-circular)

Evaluation ordering is fixed. The comparison is static ranking vs refit ranking.

**12.1 Primary (gates shipping): human-labeled holdout slice.** After the fit is locked, draw the `r2-holdout-slice` sample (40 mixed holdout requests, §3) via the existing holdout reservation flow and have rater 1 label all shown candidates under the §4 rubric (consensus rules of §5 apply if a second rater is available). Metric: within-request pairwise accuracy — fraction of human (`y`,`n`) pairs each ranker orders correctly. **Ship rule:** refit ships only if the one-sided 95% request-clustered bootstrap lower bound of (refit − static) pairwise accuracy is > 0. Existing rollout floors (raw and mixed train/holdout counts, guard-battery safety) remain in force unchanged.

**12.2 Secondary (descriptive): cross-family judge.** The full sealed holdout evaluated with labels from a judge model *not* used for training labels (if Haiku or Sonnet is selected: Opus; if Opus is selected: Sonnet). Reported for coverage; cannot gate, because family-correlated judge biases survive cross-judging.

**12.3 Ablation only: same-judge holdout.** The holdout evaluated with the selected judge's own labels, reported solely to estimate how much apparent improvement is bias-fitting (gap between 12.3 and 12.1 results). Never a shipping input.

**12.4 Label-source ablation.** Fit variants trained on (a) original Haiku/v1 labels and (b) selected-judge/v2 labels, both evaluated under 12.1's metric, to measure what cleaner labels bought.

**12.5 Blinding note.** Human slice labels are per-candidate relevance judgments collected without reference to either ranker's ordering, so the primary comparison is inherently blind to ranker identity.

## 13. Failure outcomes

- **Empty pass set (§7):** no judge is usable at the predeclared bar. Record the result, keep the static ranker, and decide separately whether round 3 tests external-family judges, ensembles, or a weak-supervision reformulation. No fit is run on failed labels.
- **Gate passes but §12.1 fails:** the refit does not ship; the label-source ablation (12.4) and bias diagnostics (§8) inform whether the failure is label quality, feature limits, or fit capacity. The sealed holdout remainder stays sealed for future rounds.

## 14. Amendments

Until the first round-2 label is recorded, this document may be revised freely with version bumps. After that point, gates, thresholds, sample identities, and decision rules are frozen; any change requires a dated amendment section here stating what changed, why, and what data had been observed at amendment time. Analyses added later that are not predeclared must be labeled post hoc in any results doc.

## 15. Effort budget (informational)

Rater 1: ~190 main-sample labels + ~240 full-request labels + ~240 stage-2 slice labels + adjudications ≈ 700 judgments across the round. Rater 2 (or retest passes): 60–120 labels. Judge-side costs are batch API calls over frozen presentations and are negligible by comparison.
