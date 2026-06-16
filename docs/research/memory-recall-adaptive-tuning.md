# Adaptive Tuning of Memory Recall Parameters from Feedback — Literature Review

**Status:** research artifact / paper seed for epic #17099.
**Sources accessed:** 2026-06-16. Claims verified at paper-abstract or source-code
level; marketing claims flagged as such. This document validates the approach in
epic #17099 against open-source practice and academic literature. It is not an
implementation plan — see `.claude/plans/17099-epic-adaptive-tuning-curried-pretzel.md`
and the task tree under #17099.

## The approach under review

Gobby's memory recall ranks candidate memories with a weighted knowledge-graph
traversal: embedding cosine similarity blended with co-occurrence support, plus
temporal/recency decay (`COOCCUR_ALPHA`, `COOCCUR_SUPPORT_CAP`, edge/temporal
half-lives, HDBSCAN density). Today these are hand-tuned, frozen constants. The
epic proposes to:

1. Derive a per-memory **usefulness label** — did an injected memory influence the
   model's response — from an LLM-judge and/or "referenced-in-output" detection.
2. **Fit the retrieval weights offline** on that label; a holdout must beat the
   static constants before anything ships.
3. **Pool data across projects** (partial pooling) to fight overfit on small
   per-project graphs (tens–hundreds of nodes).
4. Defer any **online** tuning until the offline re-fit demonstrably wins.

## Headline verdict

**On the right path. The principle is established in research and nearly absent
in production OSS — so this is defensible engineering synthesis, not a novel
mechanism.** Two findings shaped the design, and four named hazards must be
engineered around.

1. **Production agent-memory frameworks are almost universally STATIC.** Of the
   widely-adopted OSS systems (Mem0, Zep/Graphiti, Letta/MemGPT, LangMem, A-MEM),
   none tune retrieval ranking weights from usefulness feedback. "Intelligence"
   lives at *write* time (LLM extraction/consolidation), not at read-time ranking.
   The lone production exception is **Cognee** (EMA loop on a graph-element weight)
   — off by default, one scalar, not the interpretable factor blend.
2. **The research idea is established; our specific framing is near-frontier.**
   Feedback-tuned retrieval is mature as *neural* training (REPLUG, RA-DIT,
   Self-RAG) and now exists with the exact "referenced-in-output → adapt recall"
   loop (**RMM**, ACL 2025) and with interpretable weights (**Learn-to-Memorize**,
   Aug 2025). No published, named system combines all three legs: (a) a few
   *interpretable scalar weights*, (b) a *citation/referenced-in-output* usefulness
   label, (c) a *local-first, no-training-infra* setting.

**Positioning:** frame as "differentiated synthesis / under-explored
combination," not "novel mechanism." Cite RMM + Learn-to-Memorize as the prior
art to differentiate against. The genuinely open gap nobody handles is
**small-data overfit / per-user parameter tuning** — that is where the real
differentiation lives.

## Part A — Open-source landscape

Adaptive retrieval-weight tuning from usefulness feedback is a real gap in
shipping systems.

| System | Weighted retrieval? Factors | Weights learned? | Usefulness loop tunes retrieval? | LLM judges usefulness | Per-user tuned params / overfit |
|---|---|---|---|---|---|
| **Mem0** | Yes: semantic + BM25 + entity boost | Hardcoded (`ENTITY_BOOST=0.5`) | **No** | Write-time ADD/UPDATE/DELETE; optional off-the-shelf reranker (off) | Data partition only |
| **Zep / Graphiti** | Yes: RRF / MMR / node-distance / cross-encoder | Hardcoded (RRF k, MMR λ=0.5, min_score=0.6) | **No** (temporal = filter, not score) | Write-time extraction + edge invalidation | `group_id` partition + static recipes |
| **Letta / MemGPT** | RRF hybrid (vector+BM25), rank-based | Hardcoded (0.5/0.5, k=60) | **No** | Agentic only; no numeric scorer | Per-agent data isolation only |
| **Cognee** | Triplet distance + importance + `feedback_weight` | **Partial: `feedback_weight` via EMA**; rest hardcoded | **YES** — `used_graph_element_to_answer` → EMA (α=0.1), **OFF by default** | Yes — LLM maps feedback text to 1–5 | Per-user element weights; only implicit overfit damping |
| **LangMem** | No — cosine top-k | None | **No** (feedback loop optimizes *prompts*) | Write-time extract/consolidate | Namespace partition only |
| **A-MEM** | No — cosine top-k, neighbors unranked | None | **No** — "evolution" rewrites *content*; `retrieval_count` is dead code | Write/evolution only | None |
| **Generative Agents** (archetype) | Yes: `α_rec·recency + α_imp·importance + α_rel·relevance` | **Hardcoded: all α=1**; decay 0.995; importance = LLM 1–10 | **No** | Importance 1–10 at write time | Global constants |
| **MemoryBank** | Cosine only (FAISS) | Hardcoded forgetting curve | Retention only: `R=e^(−t/S)`, recall does `S+=1` — strengthens *persistence*, not ranking | No | Curve params fixed |
| **Memary** | Entity frequency count (recency stored-but-unused; likely inverted-sort bug) | Hardcoded | Counter increments on re-reference — citation-count, not usefulness | No | Per-user graphs only |

**Standard practice:** retrieve by cosine top-k; apply LLM intelligence at *write*
time; hardcode any multi-factor weights (Generative Agents' all-α=1 is the copied
archetype); optional pretrained cross-encoder rerankers (relevance only, never
tuned by the host); "per-user" means *data partitioning*, not tuned parameters.
Production-RAG fusion weights are hand-tuned constants too (Weaviate alpha 0.75;
RRF k=60); practitioner advice is offline grid-search against a labeled eval set.

**Rare/absent:** read-time loops that tune retrieval from usefulness (only Cognee);
usage counters that actually feed ranking (MemoryBank's `S` governs forgetting;
Memary's count feeds a likely-buggy selector; A-MEM's counter is dead); learned
per-user retrieval *parameters* (absent); overfit/regularization machinery for
small-data weight fitting (absent). Learning-to-rank from clicks is mature in web
IR but essentially **not imported into agent-memory recall** — a cross-pollination
gap to exploit.

**Bugs to avoid (observed in the wild):** Memary's stored-but-unused recency and
inverted argsort; A-MEM's dead `retrieval_count`. If a factor is in the formula it
must actually drive ranking and be exercised by tests.

## Part B — Academic grounding, by area

### B1. Weighted multi-factor retrieval — STRUCTURE well-trodden, LEARNING the weights is the contribution
- **Generative Agents** (Park et al., 2023) — arXiv:2304.03442 — canonical
  recency+importance+relevance sum; verbatim "all alpha's are set to 1" (set, not
  learned); decay 0.995; importance LLM-scored; relevance = cosine.
- **MemGPT** (Packer et al., 2023) — arXiv:2310.08560 — hierarchy/paging,
  orthogonal to scoring.
- **A-MEM** (Xu et al., 2025) — arXiv:2502.12110 — LLM-driven link generation
  (closest analog to similarity+co-occurrence; links LLM-decided, not a learned
  blend).
- **MemoryBank** (Zhong et al., 2023) — arXiv:2305.10250 — Ebbinghaus decay +
  importance (fixed, not learned).
- **Reflexion** (Shinn et al., 2023) — arXiv:2303.11366 — feedback updates memory
  *content*, not weights. Surveys: arXiv:2404.13501, arXiv:2504.15965.

Verdict: the weighted blend is standard since 2023; weights *learned from
usefulness feedback* has no canonical precedent. Frame the contribution as
adaptive tuning of the blend, not the blend itself.

### B2. LLM-as-judge for context usefulness — established for relevance, weak for "influence," severe label-noise
- **Zheng et al.** (2023) — arXiv:2306.05685 — canonical judge; ~80%+ human
  agreement on open-ended preference (not the memory-helpfulness task); documents
  position/verbosity/self-enhancement bias.
- **RAGAS** — arXiv:2309.15217 / **ARES** — arXiv:2311.09476 (does NOT trust a raw
  judge; fine-tunes + prediction-powered inference with human anchors — the
  de-biasing bar). **SePer** — arXiv:2503.01478 (utility via perplexity reduction,
  a model-internals signal). **Reality Check** — arXiv:2412.17031 (relevance
  correlates *weakly* with actual use; synthetic data inflates it).
- Bias literature: position bias flips verdicts — arXiv:2305.17926; self-preference
  — arXiv:2410.21819; **preference leakage** (same-family generate+judge
  contaminates labels) — arXiv:2502.01534.

Design-arounds: different-family judge; randomize position; control length;
human-anchor + correct labels (ARES/PPI); augment with a model-internals utility
signal; validate on *real* retrieved context.

### B3. Context attribution / counterfactual utility — ablation is gold, "referenced" is a weak proxy
- **ContextCite** (Cohen-Wang, Madry et al., 2024) — arXiv:2409.00729 — ablate
  context, measure log-prob change; separates **contributive (causal)** from
  **corroborative (cited)**.
- **AttriBoT** — arXiv:2411.15102 — LOO as the principled target; >300× speedup
  makes ablation practical. **SelfCite** — arXiv:2502.09604 — necessity (LOO) +
  sufficiency two-sided test.
- **Correctness ≠ Faithfulness in RAG Attributions** — arXiv:2412.18004 — up to
  **57% of citations are post-rationalized** (quoted ≠ relied upon). **ALCE** —
  arXiv:2305.14627 (citation-as-entailment, the notion newer work critiques).

Design-around: adopt **ablation/LOO** (drop the memory, measure response log-prob
delta) as the usefulness signal. Keep "referenced-in-output" only as a cheap
candidate pre-filter confirmed by ablation — never as the label.

### B4. Learning-to-rank from implicit feedback — analogy sound, but correct the propensities
- **Joachims** (KDD 2002; SIGIR 2005) — clicks as implicit *relative* relevance;
  pairwise preferences; biased by position/presentation.
- **Unbiased LTR with Biased Feedback** (Joachims, Swaminathan, Schnabel, 2017) —
  arXiv:1608.04468 — naive LTR on raw clicks is biased; fix with propensity-weighted
  (IPS) counterfactual learning. **Dual Learning** — arXiv:1804.05938; **trust bias
  needs affine correction** — arXiv:2008.10242; **doubly-robust** — arXiv:2203.17118;
  survey — arXiv:1907.07260.

Bias mapping to the memory setting:
- **Position/examination bias** → memories injected earlier/more prominently get
  referenced regardless of usefulness → IPS-weight by injection-position propensity.
- **Selection bias** → never-retrieved memories get no signal; "0 references" is
  *unlabeled, not negative*.
- **Trust/presentation bias** → authoritatively-framed memories over-referenced →
  affine/doubly-robust correction, not plain IPS.

### B5. Adaptive tuning + small data — the prudent design is exactly what the literature endorses
- **REPLUG** — arXiv:2301.12652 / **RA-DIT** — arXiv:2310.01352 — tune retriever
  from LM/usefulness feedback **offline** (nearly the proposed loop). **REALM** —
  arXiv:2002.08909. **Self-RAG** — arXiv:2310.11511 (learned IsUseful critique — a
  proxy to be optimized, caution). **RAG-RL** — arXiv:2503.12759 (naive RL on noisy
  signal needs curriculum).
- **Safe Exploration** (Jagerman, Markov, de Rijke, 2020) — arXiv:2002.00467 —
  deploy only if high-confidence off-policy eval beats baseline = the published
  "must-beat-static-on-holdout" gate. **CRM** (Swaminathan & Joachims, 2015, JMLR)
  — offline logged-feedback needs propensity correction + variance regularization.
- **Gelman & Hill** (2007) partial pooling — shrink small-per-group estimates toward
  the population mean; beats both no-pooling and complete-pooling; win case is
  small per-group samples = small per-project graphs.
- **Reward hacking / shift:** Amodei et al. arXiv:1606.06565; Skalse et al.
  arXiv:2209.13085 (proxies essentially always hackable); Gao et al. arXiv:2210.10760
  (proxy–true gap grows with optimization pressure); Goodhart taxonomy
  arXiv:1803.04585; dataset shift (Quiñonero-Candela et al., 2009).

Caution: naive *full* pooling is the complete-pooling extreme Gelman warns against
— use a per-project random effect. Offline holdout wins do not guarantee online
wins (off-policy shift) — use off-policy estimators with confidence bounds and a
guarded online A/B.

## Closest prior art to differentiate against

- **RMM — Reflective Memory Management** (arXiv:2503.08026, ACL 2025) — closest:
  refines retrieval online from the LLM's cited evidence (usefulness =
  referenced-in-output). But adapts a **neural reranker layer**, not interpretable
  scalars, and is not local-first.
- **Learn to Memorize** (arXiv:2508.16629, Aug 2025) — fits **interpretable**
  similarity/recency/importance weights from feedback, but the signal is **task
  success**, not a citation/referenced label.
- **Memory-R1** (arXiv:2508.19828) — RL-trains a memory-manager policy, not
  interpretable weights. **DAT** (arXiv:2503.23013) — LLM sets fusion alpha
  per-query (interpretable, but per-query inference, not accumulated learning).
  **Citation-reward training** (arXiv:2402.04315) — "referenced/cited" as a
  trainable reward. **Survey** (arXiv:2603.07670, §9.2) explicitly flags hybrid
  similarity+temporal+graph+counterfactual retrievers as "largely unexplored."

## Implications for Gobby's design

1. **Label:** demote "referenced-in-output" to a *feature/pre-filter*; make the
   primary label a **de-biased LLM-judge** (different family, position-randomized,
   length-controlled), **calibrated against ablation/LOO** on a sample.
2. **Fit:** pairwise LTR with **IPS** position-propensity correction; never-retrieved
   = unlabeled, not negative.
3. **Scope:** global, **partial-pooled** defaults first (per-project random effect),
   regularized; per-project overrides deferred.
4. **Gate:** must beat the frozen static constants on a **holdout** AND not regress
   a **judge-independent** quality eval (reward-hacking guard); guarded online A/B
   only after the offline win.
5. **Borrowable patterns:** RMM's referenced-in-output reward construction; Cognee's
   clipped, off-by-default EMA online update; DAT's interpretable per-query alpha.
6. **Hygiene:** every factor in the formula must drive ranking and be tested (avoid
   Memary/A-MEM dead-signal bugs).

## Four hazards to engineer around (summary)

1. **LLM-judge label noise / preference leakage** — arXiv:2502.01534, 2410.21819,
   2305.17926 → different-family judge, position/length controls, human-anchored
   correction (ARES/PPI), or model-internals signal (SePer).
2. **"Referenced ≠ used"** — arXiv:2409.00729, 2412.18004 → ablation/LOO as the
   real signal.
3. **Position/selection/trust bias in implicit feedback** — arXiv:1608.04468,
   2008.10242 → IPS propensity weighting; absence ≠ negative.
4. **Reward hacking + off-policy shift** — arXiv:2209.13085, 2210.10760 → cap
   optimization pressure, keep a judge-independent quality eval, guarded online A/B.

## Source URLs (accessed 2026-06-16)

**OSS systems:**
Mem0 — https://github.com/mem0ai/mem0 · https://arxiv.org/html/2504.19413v1 ·
Zep/Graphiti — https://github.com/getzep/graphiti · https://arxiv.org/html/2501.13956v1 ·
Letta/MemGPT — https://arxiv.org/pdf/2310.08560 ·
Cognee — https://github.com/topoteretes/cognee ·
LangMem — https://github.com/langchain-ai/langmem ·
A-MEM — https://arxiv.org/abs/2502.12110 · https://github.com/agiresearch/A-mem ·
Generative Agents — https://ar5iv.labs.arxiv.org/html/2304.03442 ·
MemoryBank — https://arxiv.org/pdf/2305.10250 · https://github.com/zhongwanjun/MemoryBank-SiliconFriend ·
Memary — https://github.com/kingjulio8238/memary

**Papers:**
2304.03442 · 2310.08560 · 2502.12110 · 2305.10250 · 2303.11366 · 2404.13501 ·
2504.15965 · 2306.05685 · 2309.15217 · 2311.09476 · 2503.01478 · 2412.17031 ·
2305.17926 · 2410.21819 · 2502.01534 · 2409.00729 · 2411.15102 · 2502.09604 ·
2412.18004 · 2305.14627 · 1608.04468 · 1804.05938 · 2008.10242 · 2203.17118 ·
1907.07260 · 2301.12652 · 2310.01352 · 2002.08909 · 2310.11511 · 2503.12759 ·
2002.00467 · 1606.06565 · 2209.13085 · 2210.10760 · 1803.04585 · 2503.08026 ·
2508.16629 · 2508.19828 · 2503.23013 · 2402.04315 · 2603.07670 (all arxiv.org/abs/<id>)

> One verification gap: the Joachims KDD'02 publisher page (ACM) returned 403;
> its metadata was confirmed via dblp/author copy. Everything else confirmed
> against arXiv abstracts.
