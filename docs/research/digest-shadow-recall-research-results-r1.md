# Digest-Shadow Recall Judge Validation — Round 1 Results

**Date:** 2026-08-08
**Status:** Round-1 human audit complete — ship gate **failed** (38/50). Holdout sealed and intact. Refit not shipped; static ranking remains active.
**Internal refs:** task #18375 "Run the digest-shadow recall refit rollout checkpoint," Linear GOB-197, epic #18421.

This document is self-contained for external handoff (e.g., deep-research tooling); no Gobby knowledge is assumed.

## 1. System context

Gobby is a local-first daemon for AI coding agents. One subsystem is persistent memory with query-driven recall: on each recall request a ranker scores candidate memories and surfaces up to 8 to the assistant. We want to refit the ranker's constants from organic usage instead of hand-tuning. Human relevance labels at scale are impractical, so a **shadow judge** — a background LLM call attached to each organic recall request — labels every surfaced candidate as relevant (`y`) or irrelevant (`n`) to the query. If the judge is trustworthy, its labels become pairwise training evidence for the refit.

**Core question:** Can the shadow judge label recalled memories closely enough to a human reviewer that its labels are safe to use for fitting recall-ranking constants?

## 2. Experiment design

Two predeclared validation stages:

1. **Human audit of the judge** (agreement gate) — completed, **failed**.
2. **Holdout comparison** of fitted ranking vs. static ranking — never run; the holdout stayed sealed because stage 1 failed first (gate read zero holdout rows).

### Judge configuration

- Model: `claude/haiku` (Anthropic's small/cheap tier).
- Protocol: `digest-shadow-query-relevance-v1`; generation-config fingerprint `836f52b3644e75b30e5bb9037896cec63ab0996880f04f715552d244ff0c94aa`; weighting regime `[true,false,true,true]` (feature-toggle vector for the fit).
- Judge input: the stored user query plus the exact candidate memory excerpts as presented. The judge never sees the assistant's eventual response (prevents rewarding memories merely because the response repeated them).
- Verdict semantics: `y` = memory materially helps answer or act on the query; `n` = does not. Both parties answering `n` counts as agreement.
- One judge call labels up to 8 candidates for a request, so labels within a request share context and correlated errors.

### Stored provenance per request

Query; up to 8 candidate memories; candidate scores and ranking features; exact excerpts shown to the judge; content hashes; per-candidate verdicts; judge model, protocol version, config fingerprint.

### Cohort freeze

- Data cutoff and completion cutoff: both `2026-08-08T07:44:45Z`. Data cutoff excludes later requests; completion cutoff excludes judge snapshots completed later — labels cannot arrive mid-audit and silently change the population.
- Label source `digest_shadow`; candidate scope `full`.
- Live eligible cohort: **1,558 requests**, deterministically split by request ID:
  - **Training:** 779 requests → 669 "mixed" requests → 8,121 relevance pairs.
  - **Holdout:** 779 requests → 647 mixed requests → 7,872 pairs (sealed, still intact).
- "Mixed" = at least one `y` and one `n` among the request's candidates. Only mixed requests generate pairwise evidence: every relevant candidate should rank above every irrelevant one within its request.

### Human audit protocol

- Deterministic, hash-bound selection of **50 distinct training requests, one candidate each**. One-per-request because a single judge call labels up to 8 correlated candidates; the independent unit is the request–memory pair. Hash binding prevents the CLI or gate substituting easier items.
- Single reviewer (N=1; also the system's primary user). Saw the exact query and exact sampled memory, answered y/n; the judge's verdict was hidden until after each answer.
- Predeclared ship gate: observed agreement ≥ 80% AND Wilson 95% lower bound ≥ 65%. At n=50 both conditions bind at the same boundary (40/50), so the Wilson condition is vacuous at this exact n. The thresholds are conservative product-policy choices; no cited benchmark or power analysis backs the specific values 80/65/50.

## 3. Round-1 audit results

- **Agreement: 38/50 = 76%.** Wilson 95% interval ≈ **[62.59%, 85.70%]**. Gate failed — exactly two agreements short.

| Agreements | Observed | Wilson lower | Result |
| --- | --- | --- | --- |
| 38/50 | 76% | 62.59% | Fail |
| 39/50 | 78% | ~64.8% | Fail |
| 40/50 | 80% | ~67.0% | Pass |

- Human marginals: **16 relevant / 34 irrelevant** (32% positive).
- Confusion matrix (judge × human), per-class precision/recall, and Cohen's kappa **not yet extracted** — both label sets are stored, so this is computable.
- Derived observations:
  - A degenerate judge answering `n` always would score 68% raw agreement given the class imbalance; the 80% bar sits only 12 points above that baseline.
  - Assuming judge marginals similar to the human's, chance agreement ≈ 56.5%, giving estimated kappa ≈ 0.45 ("moderate").
  - Gate power: a judge whose true agreement is exactly 80% passes only ~55% of the time at n=50 (Bin(50, 0.8), P(X≥40)); a reliable pass needs true agreement near 88%.
  - The interval remains compatible with a true rate ≥ 80%. The failure correctly withholds shipping while leaving open whether the judge actually meets the target.

## 4. Known limitations of the round-1 audit

1. **Single un-validated human reference.** No second reviewer, no intra-reviewer consistency check. 76% agreement confounds judge error, reviewer error, and genuine ambiguity. The human–human ceiling on this task is unmeasured; if humans agree only ~82–85% with each other (typical for graded-relevance tasks), the gate demands near-clone fidelity to one person.
2. **Raw agreement under class imbalance.** No chance correction, no per-class metrics, unknown disagreement direction (judge false positives vs. false negatives), which matters asymmetrically downstream: judge false positives both corrupt pairs and convert non-mixed requests into spurious mixed ones, injecting noise requests into training.
3. **Underpowered gate.** n=50 cannot distinguish a 76%-true judge from an 82%-true judge; the gate is roughly a coin flip in the region of interest.
4. **Per-label agreement understates per-pair corruption.** Fitting consumes pairs; a pair is wrong if either endpoint is mislabeled (~0.76² ≈ 58% cleanly labeled pairs under independence), and within-request errors are correlated via the shared judge call — a structure the one-per-request audit deliberately cannot see.
5. **Information asymmetry.** Queries come from the reviewer's own sessions; the reviewer has latent context for vague queries (task notifications, fragments) that no text-only judge can access. For those items the achievable ceiling for any judge is below 100% by construction.
6. **Agreement is a proxy for the wrong quantity.** Pairwise ranking fits are fairly robust to symmetric label noise; what breaks them is structured error (judge biased by memory length, lexical overlap, recency). The audit measures marginal agreement and leaves error structure unmeasured.
7. **Rubric alignment unverified.** "Materially helps" is a subjective threshold; if human and judge hold different implicit thresholds, systematic disagreement follows without either being wrong.
8. **Audit population vs. fitting population.** One-per-request sampling weights by request; the fit weights by pairs, over-representing candidate-rich requests.

## 5. Candidate improvements

1. **Extract the confusion matrix and read all 12 disagreements** (both labels stored; zero new human time). Classify: judge clearly wrong / human arguably wrong / query too context-poor to decide. This decides whether the fix is a better model, a better rubric, or query filtering.
2. **Offline re-judge of the same 50 frozen presentations with stronger models** (Sonnet/Opus-class) under a new protocol digest. Presentations and human labels are hash-bound and stored; direct model comparison costs pennies and zero human time. Caveat: once a model is selected for scoring well on these 50, they are a dev set — a passing model must confirm on a fresh disjoint human-audited sample with a predeclared rule.
3. **Decouple the live shadow judge from fitting labels.** Constants are fit offline; the fit can use any label source. Batch re-label the full training cohort (669 mixed requests) with the strongest affordable model once, offline. The live shadow can remain Haiku or be dropped.
4. **Redesigned audit round 2:** n ≥ 100–200; second reviewer on an overlap subset to measure the human ceiling; adjudicated disagreements; report kappa, per-class precision/recall, and confusion matrix alongside raw agreement; explicitly disjoint replication sample; decision rule fixed before seeing results.
5. **Rubric hardening:** anchored definition of "materially helps" with worked positive/negative examples shared verbatim by human and judge.
6. **Query-quality filtering or stratification:** exclude or separately treat context-poor queries (notifications, fragments) in both fitting and auditing.
7. **Ensemble options:** 3-vote self-consistency, or two-stage judging (Haiku screens, stronger model adjudicates low-confidence items).

## 6. Open research questions

1. **Reported human–LLM agreement on relevance judgment.** What agreement/kappa levels do published LLM-judge relevance systems achieve vs. human assessors (e.g., TREC-style evaluation, Microsoft/Bing LLM assessor work — Thomas et al., UMBRELA, Faggioli et al. on LLM relevance judgments)? How does the small-model tier compare with frontier-class models on this task family?
2. **Human–human ceilings.** What inter-annotator agreement is typical for binary relevance judgment (TREC qrels overlap studies, Voorhees' work on assessor disagreement)? Is an 80% raw-agreement gate against a single human above the realistic ceiling?
3. **Judge validation methodology.** Established best practice for meta-evaluating an LLM judge before using it as a label source: sample sizes, chance-corrected metrics, blinding, adjudication, sequential/group-sequential designs that permit a second audit batch without multiple-testing inflation.
4. **Label noise in learning-to-rank.** How much pairwise label noise can ranking fits tolerate before fitted parameters degrade below a hand-tuned baseline? Symmetric vs. structured noise; correlated within-group errors.
5. **Judge capability scaling.** Evidence that relevance-judgment accuracy scales with model capability, and where returns diminish. Cost/quality tradeoffs of ensembles and self-consistency voting vs. a single stronger model.
6. **Rubric and prompt effects.** Measured impact of anchored rubrics / few-shot exemplars on judge–human agreement in relevance tasks.
7. **Statistical gate design.** For a ship/no-ship agreement gate: recommended n and decision rules with controlled type I/II error when the target is 80% agreement (power analysis for one-sided proportion tests; Wilson vs. Clopper-Pearson vs. Bayesian intervals in small-n audits).
8. **Handling context-poor queries** in query-conditioned relevance labeling: filtering heuristics, abstention labels ("cannot determine"), and how production systems treat unanswerable queries in judge pipelines.

## 7. Storage and traceability

- Audit verdict store: `recall_shadow_audit_verdicts`.
- Decision artifact (absent until gate passes): `~/.gobby/recall_refit_decision.json`.
- Legacy `label_source="digest"` rows are read-only evidence; current collection uses `digest_shadow` with durable judge state and prompt/audit records.
- Holdout identity excludes tunable fit settings while the fit digest includes them, preventing tuning from minting fresh holdouts.
