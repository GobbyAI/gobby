# Digest-Shadow Recall in Gobby: Positioning, Comparisons, and Next-Phase Evaluation Plan

## Executive overview

Gobby’s digest-shadow recall experiment is a *meta-learning* layer over your persistent memory system: every organic recall request is labeled by a background LLM judge (Haiku) and those labels are turned into pairwise constraints for refitting the recall ranker. The first validation round deliberately blocked shipping: agreement between the judge and a single human reviewer was 38/50 (76%), with a Wilson 95% interval of about [62.6%, 85.7%], short of the pre-declared 80% gate. This is a healthy failure: it kept a noisy label source out of the fit while revealing that the design of the audit, not just the model, is the main bottleneck.[^1]

In the IR literature, LLM judges like Bing’s UMBRELA (GPT‑4o) typically achieve moderate human-level agreement (Cohen’s κ ≈ 0.3–0.45 for graded relevance, higher for binary/pairwise) and, more importantly, induce system rankings highly correlated (Kendall τ ≈ 0.87–0.94) with rankings based on fully manual labels. Human–human agreement for binary relevance is itself modest (overlaps and κ often around 0.3–0.5), which means demanding ≥80% raw agreement with a single human is above the realistic ceiling for many tasks. Learning‑to‑rank work shows that pairwise rankers can tolerate substantial *symmetric* label noise but degrade when pairwise noise (incorrect preference orderings) grows or errors become structured (e.g., biased by length or lexical overlap).[^2][^3][^4][^5][^6][^7][^8]

The upshot: your basic architecture (shadow judge → pairwise constraints → offline refit → sealed holdout) is well aligned with the best practice emerging from TREC-style LLM‑judge work, but your current audit is underpowered and not measuring the right quantities. The most valuable next steps are to (1) extract richer diagnostics from Round‑1, (2) run a properly powered Round‑2 multi-rater audit with stronger models and explicit rubric, and (3) design downstream tests that focus on pairwise noise and end-to-end recall quality instead of marginal per-item agreement.

The rest of this report: (1) restates your experiment in a more general framework, (2) compares it to memory/recall implementations in common agent frameworks and IR evaluation practice, and (3) proposes concrete, publishable evaluation protocols you can run in Gobby over the next few months.

## 1. Restating the digest-shadow design

### 1.1 System and objective

Gobby’s persistent memory subsystem maintains a store of prior interactions and facts, and on each recall request the ranker scores and surfaces up to 8 memories to the assistant. The goal of the digest-shadow experiment is to refit ranker constants from real usage instead of hand-tuning, using a cheap LLM judge to produce dense labels at scale. Each organic recall invocation triggers a Haiku call that sees the original query and the candidate memory excerpts (but not the assistant’s eventual answer) and returns binary relevance labels (`y`/`n`).[^1]

These labels are converted into within-request pairwise constraints: for every query with at least one `y` and one `n`, all `y` memories are expected to rank above all `n` memories, giving 8,121 training pairs from 669 mixed training requests in the frozen cohort. The held-out half of requests (647 mixed holdout requests with 7,872 pairs) remains sealed until the judge passes an audit, avoiding accidental leakage of information about tuning choices into evaluation.[^1]

### 1.2 Round‑1 human audit protocol

The Round‑1 audit sampled 50 distinct training requests deterministically via a hash, took one candidate memory per request, and had a single human reviewer (also the primary Gobby user) label each as relevant/irrelevant under a "materially helps answer or act on the query" rubric. The Haiku verdicts were hidden during annotation and only revealed after each response to avoid bias.[^1]

The predeclared ship gate required both (a) ≥80% observed agreement and (b) a Wilson 95% lower bound ≥65%; at n=50, these coincide at 40/50 agreements. The realized agreement was 38/50 (76%) with Wilson CI ≈ [62.6%, 85.7%], so the gate failed by exactly two items. Human marginals were 16 `y` / 34 `n` (32% positive), and no confusion matrix or κ was yet computed, though both label sets are stored.[^1]

### 1.3 Limitations already identified

The existing writeup correctly calls out several limitations:

- Single unvalidated human reference; no second reviewer or intra-rater test.[^1]
- Class imbalance (32% positive); naive agreement conflates chance agreement and asymmetric error costs.[^1]
- Underpowered audit; n=50 has low power to distinguish a 76%‑true from 82%‑true judge, and even a truly 80%‑accurate judge passes only ~55% of the time.[^1]
- Per-label agreement understates per-pair corruption; a 76% accurate labeler yields only about 58% cleanly labeled pairs under independence, and within-request errors are correlated.[^1]
- Information asymmetry: the human enjoys latent context from being the original user, raising the ceiling above what any text-only judge can see.[^1]
- Agreement is a proxy for the wrong quantity; structured biases (length, lexical overlap, recency) are more dangerous than symmetric noise for rank learning.[^6][^1]
- Rubric alignment was not tested; "materially helps" may be interpreted differently by human and judge.[^1]

These limitations are exactly the points where you can build a publishable story: replacing ad hoc thresholds with proper power analysis, chance-corrected metrics, and diagnostics on error structure.

## 2. How this compares to other memory and recall systems

### 2.1 Memory implementations in current agent frameworks

Most open-source agent frameworks implement memory via some variant of RAG over an append-only store, with retrieval based on dense embeddings, keyword BM25, or hybrid search; very few instrument recall with systematic label pipelines. In LangChain-style systems, retrieval tuning is often manual or guided by offline relevance benchmarks unrelated to real usage; in AutoGen-like stacks, conversational memory is usually time-based or heuristic rather than optimized with pairwise training.

Some commercial systems (e.g., Bing’s search relevance stack and TREC RAG pipelines) use LLM judges to generate graded relevance labels and then optimize rankers or evaluate systems on those labels. However, these are usually applied to web documents or RAG chunks, not to the internal long-lived memory graph of a coding agent.[^3][^5][^8]

What is distinctive in Gobby is:

- The label source is tied directly to *organic* recall queries inside a coding agent workflow, rather than synthetic prompts or external benchmarks.[^1]
- The judge’s labels are used not only for evaluation but as training signal for the ranker, with a sealed holdout of the same organic distribution.[^1]
- The system maintains full provenance (query, candidates, scores, features, excerpts, hashes, verdicts, protocol configs) to enable post hoc analysis and reproducibility.[^1]

That design pushes Gobby closer to a modern IR lab-in-the-loop system than to typical agent memory implementations, which are mostly static RAG.

### 2.2 LLM judges vs human judges in IR

Recent work on LLM-based relevance assessment shows that state-of-the-art judges (GPT‑4o, DeepSeek V3) can match or approach human-level agreement on TREC Deep Learning tracks and produce system rankings highly correlated with those obtained using human labels. For UMBRELA (OpenAI GPT‑4o-based Bing assessor), reported κ scores are around 0.3–0.42 and Kendall τ correlations 0.87–0.97 across DL 2019–2023. Later work finds that UMBRELA judgments can replace manual qrels for run-level evaluation in nDCG and recall without large loss of fidelity, though fine-grained differences still benefit from human review.[^9][^10][^5][^8][^3]

At the same time, diagnostic studies show that LLM judges tend to overrate relevance and are sensitive to superficial features like passage length and lexical overlap, leading to systematic biases. Other work documents that humans themselves disagree substantially on relevance, with inter-annotator overlap and κ often in the 0.3–0.5 range even for binary judgments, reinforcing that any single annotator is a noisy target.[^11][^4][^5]

Compared to these systems, your Haiku-based judge is:

- A much smaller model, so 76% raw agreement against a single human on a task with asymmetric context access is consistent with "moderate" κ regimes seen in literature for harder graded tasks.[^4][^3][^1]
- Used in a binary relevance setting on short memory snippets rather than multi-grade passage relevance, which likely yields higher baseline agreement but also makes overrating and length biases riskier (since a single `y` is enough to define pairs).[^11][^1]

### 2.3 Learning-to-rank under noisy pairwise labels

Learning-to-rank research directly studies robustness to label noise. Experiments injecting random or structured noise into training labels show that the *pairwise noise ratio* (fraction of pairs whose preference ordering is wrong) is a better predictor of performance degradation than document-level noise (fraction of mislabeled documents). Theoretical and empirical results suggest that ranking algorithms can tolerate moderate symmetric pairwise noise but suffer when noise becomes concentrated in certain regions (e.g., near the decision boundary) or when biases systematically flip preferences.[^7][^6]

This aligns with your own observation that per-label agreement understates pairwise corruption: if each endpoint is correct only 76% of the time and errors are independent, only about 58% of pairs are clean, and correlated within-request errors may further worsen pairwise noise. Gobby’s design is therefore exploring a very relevant question for the field: how much real, structured noise from an LLM judge can a practical recall ranker handle before it underperforms a hand-tuned baseline?[^6][^1]

## 3. How to stress-test and improve what you are researching

### 3.1 Deeper analysis of Round‑1 (no more human work)

There is more signal in the existing cohort without asking humans to do anything:

- **Compute the full confusion matrix and class metrics** for Haiku vs human on the 50 audited items: per-class precision, recall, F1, and Cohen’s κ, including chance-corrected agreement. This will tell you whether the judge is worse on false positives (overrating memories) or false negatives (missing helpful memories).[^3][^1]
- **Estimate pairwise noise** implied by these errors by mapping label disagreements to wrong preference pairs. Using the pNoise framework (ratio of noisy pairs) from learning-to-rank robustness work gives a direct estimate of how much effective supervision remains.[^7][^6]
- **Analyze the 12 disagreements qualitatively** with a small taxonomy: judge clearly wrong, human plausibly wrong, inherently ambiguous/context-dependent. This informs whether to invest in rubric work, better models, or query filtering.[^1]
- **Probe for structured biases** by regressing judge–human disagreement on features like memory length, lexical overlap with query, recency, and source type. That can reveal whether the judge overweights shallow similarity or particular memory modalities.[^11][^6]

All of this is derivable from your existing verdict store and the stored excerpts/provenance.[^1]

### 3.2 Round‑2 audit: powered, multi-rater, multi-model

Design Round‑2 as a proper mini-study that you can later drop into a paper:

- **Sample size and power**: target n≈150–300 items to get tight Wilson intervals and meaningful power to distinguish, say, 0.75 vs 0.85 true agreement at one-sided α=0.05. Use a power calculation for a one-sample proportion test to set this explicitly.[^3][^1]
- **Multiple human raters**: at least 2, ideally 3, labeling an overlapping subset of items so you can estimate human–human agreement (overlap and κ) as the true ceiling. This directly answers whether an 80% threshold versus a single human is realistic.[^4]
- **Multiple models**: replay the *same frozen* items through Haiku, Sonnet, and a stronger model (e.g., Opus or GPT‑4o via a generic judge prompt), using identical rubric and prompt shape. This gives a capability-scaling curve of agreement and lets you choose a judge based on empirical quality vs cost.[^10][^2][^3]
- **Explicit rubric and examples**: write a short, anchored rubric with 5–10 positive and negative examples from your own corpus, shown to both humans and models (few-shot in the prompt). This reduces threshold drift and lets you study how much rubric refinement moves agreement metrics.[^5]
- **Chance-corrected metrics and error structure**: report raw agreement, κ, per-class precision/recall, and breakdown of disagreement types for each model vs the *consensus* human label (e.g., majority vote, or adjudicated label where humans disagree).[^4][^3]

With this design, the Round‑2 audit becomes a small but respectable LLM‑judge paper section, directly comparable to UMBRELA and related work.

### 3.3 Offline re-judging and decoupling live vs training labels

You already proposed decoupling live shadow judgments from training labels, which is aligned with IR practice:[^3][^1]

- Keep Haiku (or even drop the live judge) for interactive UX diagnostics or debugging, where occasional misjudgments are acceptable but labels are not used for fitting.[^1]
- For training, **batch re-label the full 669 mixed training requests** with the strongest affordable model selected from Round‑2 (e.g., Sonnet or an external GPT‑class judge), once, under a fixed protocol.[^3][^1]
- Optionally, **weight training pairs** by confidence or model consensus: pairs on which multiple strong judges agree get higher weight; ambiguous pairs are down-weighted or dropped, following noisy-label robustness strategies.[^6][^7]
- Preserve the original Haiku labels and the new labels side-by-side for analysis, so you can study how downstream ranker behavior changes when trained on noisy vs cleaner judgments.

This gives you a clean experimental knob: same candidate features and architecture, different label sources.

### 3.4 End-to-end recall evaluation within Gobby

Beyond judge–human agreement, you want *end-to-end* metrics that reflect the coding-agent user experience:

- **Task-level success**: for a curated set of coding tasks with known relevant memories (e.g., prior diffs, design decisions, API usage examples), measure whether the top‑k recall under the refit contains the key memory, and whether the agent produces better edits/answers when those memories are present.
- **Synthetic but realistic tasks**: generate small synthetic tasks that require stitching together 2–3 specific memories (e.g., a design doc snippet and a prior bug fix) and verify whether ranker changes improve the availability of these ingredients in the assistant context.
- **Live A/B with blinded users**: when you are comfortable, run a small A/B between static and refit rankers on your own sessions or a small tester group, logging ratings of "Was the memory recall helpful?" or implicit metrics like manual search frequency.

These tests are less paper-friendly on their own but, combined with judge metrics, they tell a coherent story: better judges → cleaner pairs → better rankers → better coding outcomes.

## 4. Framing for improving Gobby and writing a paper

### 4.1 How this concretely improves Gobby

Implementing the above will improve Gobby along several axes:

- **More reliable recall**: refits based on high-quality labels and pairwise-noise-aware training should surface more truly helpful memories in top‑k, reducing hallucinated or redundant context.[^6][^1]
- **Better observability**: with confusion matrices, κ, and bias diagnostics, you can debug and iterate on your judge prompt and model choice in a principled way.[^5][^3]
- **Safer automation**: explicit gates and sealed holdouts prevent a bad judge from silently degrading recall, aligning with best practices in evaluation science.[^3][^1]
- **Re-usable judgment pipeline**: the same infrastructure can later score other internal systems (e.g., code-search, tool-selection recall) with minimal additional work.

From an engineering perspective, the big step is to treat "judge quality" as a first-class metric with dashboards and tests, not just a one-off gate.

### 4.2 Paper-worthy contributions and structure

Given the IR and LLM‑judge literature, a paper based on this work could make at least three contributions:

1. **Domain shift**: evaluating LLM judges on *agent memory recall* rather than web passages, showing how agreement and biases change in this setting.[^3][^1]
2. **Pairwise noise and recall rankers**: empirically relating judge error patterns to pairwise noise and to the performance of a real recall ranker in an AI coding agent.[^6][^1]
3. **Practical gate design**: proposing statistically principled ship gates (n, thresholds, metrics) for using LLM judges as label sources in production systems, with recommendations grounded in your Round‑2 and Round‑3 results.[^8][^9][^3]

A plausible paper structure:

- Introduction: Gobby, persistent memory, need for scalable relevance labels.
- Related work: LLM judges (Thomas et al., UMBRELA, Faggioli, LLMJudge studies), human–human relevance agreement, learning-to-rank under label noise.[^2][^8][^11][^4][^6][^3]
- Methodology: digest-shadow design, cohort freeze, judge protocols, audit design, label-to-pair conversion, and evaluation metrics.
- Experiments:
  - Round‑1 (Haiku vs 1 human; limitations and lessons).
  - Round‑2 (multi-model, multi-rater, powered audit; human vs model κ, bias diagnostics).
  - Ranker training experiments with noisy vs cleaner labels and downstream recall/user metrics.[^6][^1]
- Discussion: guidance for other agent frameworks on adopting LLM judges safely.

Because Gobby is open-source, you can also release the anonymized recall cohort and judgment prompts, making the work a reference dataset for others.

## 5. Concrete next steps (checklist)

1. **From existing data**:
   - Compute confusion matrix, κ, per-class metrics for Round‑1, and estimate pairwise noise using pNoise-style ratios.[^6][^1]
   - Run simple regressions of disagreement vs length/overlap/recency features to probe structured bias.[^11][^6]
   - Manually classify the 12 disagreements into error categories.

2. **Design Round‑2 audit**:
   - Choose sample size (≈150–300), rater pool, and overlapping design for human–human agreement.[^4]
   - Finalize rubric with worked examples and prompts for Haiku/Sonnet/Opus (or external GPT‑4o/DeepSeek) judges.[^10][^2][^3]
   - Pre-register decision rules (e.g., target κ, max allowed pairwise noise) and ship gates.

3. **Label and train**:
   - Freeze an updated recall cohort if needed; re-label training subset with chosen strong judge(s).[^3]
   - Train rankers with: (a) Haiku labels, (b) strong-judge labels, (c) consensus/weighted labels; compare on sealed holdout and end-to-end recall tasks.[^6]

4. **Prepare publication**:
   - Keep a living doc of design decisions, parameter choices, and failures for later methods sections.[^1]
   - Plan anonymization of queries/memories consistent with user privacy for dataset release.

Executed well, this line of work can both materially improve Gobby’s memory system and produce a credible IR/LLM‑judge paper that speaks to the broader community’s interest in scalable, reliable evaluation using LLMs.

---

## References

1. [digest-shadow-recall-research-results-r1.md](https://ppl-ai-file-upload.s3.amazonaws.com/web/direct-files/attachments/119290898/3d6bf20e-7750-419c-b1cc-0e56b22af2b4/digest-shadow-recall-research-results-r1.md) - # Digest-Shadow Recall Judge Validation — Round 1 Results

**Date:** 2026-08-08
**Status:** Round-1 ...

2. [[PDF] Revisiting Human-vs-LLM judgments using the TREC Podcast Track](https://jmmackenzie.io/pdf/mcym26-ecir.pdf) - In this paper, we conduct an analysis on user agreement between LLM and human experts, Thomas et al'...

3. [UMbrela is the (Open-Source Reproduction of the) Bing RELevance ...](https://arxiv.org/html/2406.06519v1) - A recent study by Thomas et al. from Microsoft Bing suggested that large language models (LLMs) can ...

4. [Variations in Relevance Judgments  and the Shelf Life of Test Collections](https://arxiv.org/pdf/2502.20937.pdf)

5. [Benchmarking LLM-based Relevance Judgment Methods - ar5iv](https://ar5iv.labs.arxiv.org/html/2504.12558) - Large Language Models (LLMs) are increasingly deployed in both academic and industry settings to aut...

6. [[PDF] Which noise affects algorithm robustness for learning to rank](https://jiafengguo.github.io/2015/2015-Which%20Noise%20Affects%20Algorithm%20Robustness%20for%20Learning%20to%20Rank.pdf) - For simplicity, we randomly injected label errors (i.e. label noise) into training data (with fixed ...

7. [Which noise affects algorithm robustness for learning to rank](https://bohrium.dp.tech/paper/arxiv/55922f5f0cf2ceaae74c8ed6)

8. [A Large-Scale Study of Relevance Assessments with Large Language Models: An Initial Look](http://arxiv.org/pdf/2411.08275.pdf)

9. [LLM-based Relevance Assessment Still Can't Replace Human ...](https://arxiv.org/html/2412.17156v3) - Our results suggest that automatically generated Umbrela judgments can replace fully manual judgment...

10. [Does UMBRELA Work on Other LLMs?](https://arxiv.org/html/2507.09483v1)

11. [When LLM Judges Inflate Scores: Exploring Overrating in ... - arXiv](https://arxiv.org/html/2602.17170v3)

