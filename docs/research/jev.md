# Jev and TypeSafe AI for fast classification in Gobby

**Date:** 2026-09-20
**Status:** Research complete; no implementation or paid inference benchmark performed
**Branch:** `0.5.0` at `fb7231ccb5`
**Task:** #22623

## Summary

Jev is a hosted decision model from TypeSafe AI. It accepts shared state plus bounded
questions and returns typed choices, scores, or yes/no probabilities. It cannot generate
summaries, explanations, plans, or arbitrary JSON. Its value to Gobby is fast semantic
classification after deterministic checks or retrieval have reduced a problem to a small
decision space.

The best initial experiment is Gobby's existing memory-relevance shadow judge. It already
collects replayable presentations and labels without changing retrieval behavior. The best
potential production uses are skill/MCP-tool reranking and found-work confirmation.

Jev cannot run through Gobby's current OpenRouter generation endpoint. OpenRouter serves it
through the alpha Decisions API, `POST /api/alpha/decisions`, rather than Chat Completions or
Responses. A production integration should therefore add a small decision service and
adapter, with an optional internal MCP tool. It should not add another global model router or
make Jev authoritative for permissions, rules, dispatch, validation, or task closure.

## Model and API

TypeSafe describes Jev as a "System One" model for machine-facing decisions. A request
contains state and one or more independent questions. Jev 1.13 supports three primitives:

- **Choice:** choose one of up to 255 caller-defined options and return option
  probabilities plus confidence.
- **Score:** choose among 2–10 ordered rubric levels and return the distribution, weighted
  score, and confidence.
- **Noul:** return the probability that a yes/no proposition is true. Noul does not return a
  separate confidence value.

Multiple questions can share the same state in one request, but each is evaluated
independently. This is useful for judging several retrieved memories or tool candidates
without repeating the full context.

Type safety constrains the output envelope; it does not guarantee semantic correctness.
TypeSafe documents weaknesses in arithmetic, counting, dates, multi-step indirection,
irrelevant context, adversarial state, contradictory rubrics, and structural invariants. Jev
can confidently select the wrong valid answer.

Sources: [TypeSafe introduction](https://docs.typesafe.ai/introduction),
[API reference](https://docs.typesafe.ai/api), and
[Jev 1.13 jaggedness](https://docs.typesafe.ai/model-jaggedness/jev-1.13).

### OpenRouter

OpenRouter exposes Jev through a dedicated alpha API:

```http
POST https://openrouter.ai/api/alpha/decisions
Authorization: Bearer $OPENROUTER_API_KEY
Content-Type: application/json

{
  "model": "typesafe/jev-1.13",
  "state": { "...": "..." },
  "questions": { "...": "..." }
}
```

Use the pinned model `typesafe/jev-1.13` during evaluation. The alias
`~typesafe/jev-latest` is convenient for exploration but prevents reproducible calibration.
The Decisions contract is alpha and may change.

At the research date, OpenRouter listed $0.042 per million input tokens, no output-token
charge, and a 32K context window. TypeSafe's native service documents a 64K aggregate request
limit, with `state + longest question` limited to 32K. Native TypeSafe limits should not be
assumed for OpenRouter.

Sources: [OpenRouter Jev recipe](https://openrouter.ai/labs/jev/compile),
[OpenRouter model page](https://openrouter.ai/typesafe/jev-1.13), and
[TypeSafe model limits](https://docs.typesafe.ai/models).

## Fit with the current Gobby architecture

`GenerationEndpointConfig` in `src/gobby/config/ai.py` supports two wire APIs:
`chat-completions` and `responses`. Both register under `AICapability.TEXT_GENERATE`. The
OpenAI-compatible JSON path ultimately calls Chat Completions and validates JSON Schema after
the response. None of those paths can call `/api/alpha/decisions`.

Adding Jev as another `ai.generation.endpoints` model would therefore be a false integration:
the model slug could be configured, but requests would go to the wrong API. The current
OpenRouter secret can be reused by reference, but the generation endpoint itself cannot.

Gobby also deliberately removed its generic speed/route axis. Jev should be selected by the
few features whose contracts are bounded decisions, not introduced as a global "fast" tier.

The appropriate boundary is:

```text
Gobby feature
    -> DecisionEvaluationService
    -> OpenRouterDecisionAdapter
    -> POST /api/alpha/decisions
    -> typed answers and probabilities
    -> feature-owned policy and fallback
```

The model interprets state. Tested code owns thresholds, permissions, fallback behavior, and
all state changes.

## Ranked Gobby use cases

### 1. Memory relevance shadow judge: best pilot

`src/gobby/memory/shadow_relevance.py::_judge_shadow_entries` is already a bounded
classification system. It:

- batches up to eight candidates against one query;
- randomizes neutral candidate keys;
- validates typed verdicts;
- records model, protocol, confidence, and label provenance;
- treats failures as retryable rather than user-blocking; and
- feeds existing replay, refit, audit, guard, and ship-gate machinery.

Run Jev in shadow against the current judge and human-audited samples. Do not change retrieval
behavior until it wins a predeclared evaluation. Choice between `relevant` and
`not_relevant` provides a distribution and confidence. The existing judge also stores a
generated rationale; a Jev protocol should store probabilities and deterministic provenance
instead of inventing explanatory prose.

This pilot has the best calibration infrastructure and the lowest product risk.

### 2. Skill and MCP-tool reranking: best likely product payoff

`RecommendationService._recommend_hybrid` already performs semantic retrieval and then pays
for a generative reranker. Jev fits as the second stage:

1. Semantic retrieval produces the top 10–20 candidates.
2. One Noul question per candidate asks whether the tool materially applies to the request.
3. Candidates are ranked by probability.
4. An absolute threshold permits rejecting every candidate.
5. Timeout, schema failure, or service failure returns the existing semantic order.

Choice alone is insufficient because it always selects an option. Independent Noul questions
provide both reranking and a reject-all path.

TypeSafe's closest published experiment used 488 requests and 182 skills. It reports wrong
loads falling from 16.8% to 7.3% and needless loads from 9.8% to 4.0%. The experiment is
vendor-authored and uses a comparatively easy synthetic set, but the workload maps closely to
Gobby's progressive discovery. See the
[skill-suggestion cookbook](https://docs.typesafe.ai/cookbooks/skill_suggestion).

### 3. Found-work confirmation: strongest hot-path replacement

`FoundWorkStopAnalyzer._confirm_shirk` already asks a bounded boolean question after
deterministic fast-path checks. The current implementation:

- calls the general task-validation LLM path;
- allows up to eight seconds;
- requests `{block, reason}`; and
- uses only the boolean `block` value.

Noul matches the consumed contract. Keep every deterministic prefilter and exemption. Shadow
Jev against the current confirmation path before adoption, and preserve the current failure
behavior. A classifier outage must not silently clear a finding.

### 4. Knowledge-graph relation invalidation

`KnowledgeGraphExtractor.select_outdated_relations` makes a yes/no decision for each existing
relation during refresh. The graph is a rebuildable secondary projection, which limits the
impact of mistakes. Jev could select invalidated relations; generative models remain necessary
for extracting new entities and relationships.

### 5. Agent-trace and feedback triage

After an agent run, classify stored evidence into categories such as defect, guidance gap,
noise, praise, human review, or priority review. Use the result to prioritize the existing
reviewer rather than suppress findings automatically. TypeSafe's
[agent-trace observability example](https://evals.typesafe.ai/agent_trace_observability)
describes a similar workload.

### 6. Advisory action-risk tripwire

For calls already admitted by deterministic policy, Jev could flag `off_task`, `destructive`,
`secret_exposure`, `external_side_effect`, or `untrusted_execution`. A positive result can
require review or deny an action. A negative result cannot grant permission or bypass Gobby's
rules, allowlists, approval checks, or validation gates.

Vercel demonstrates a comparable clear/caution classification before tool execution, with code
retaining authority over permissions and failures returning to user review. See
[Auto-approve tool calls with Eve and Jev](https://vercel.com/kb/guide/auto-approve-tool-calls-eve-jev)
and [Agent control with Jev](https://vercel.com/i/jev-agent-control).

### Other plausible uses

- Classify retained context or tool results as `retain`, `offload`, or `drop`; keep selected
  content verbatim rather than asking Jev to summarize it.
- Check whether cited evidence supports a research or handoff claim. TypeSafe's small
  [citation-check example](https://docs.typesafe.ai/cookbooks/citation_check) caught planted
  failures, but it is not an independent benchmark.
- Suggest task category, affected subsystem, or review lane. Keep task transitions,
  dependencies, and close gates deterministic.
- Pre-filter likely MCP pages and transport families before generative MCP-import synthesis.

## Poor fits

Do not use Jev for:

- summaries, plan drafting or review, task expansion, merge resolution, memory dreams, or
  knowledge extraction, all of which require synthesis or explanatory text;
- task-close authority, destructive-action authorization, wake/interruption authority, rule
  evaluation, stage dispatch, or validation gates;
- deterministic arithmetic, counting, dates, schema checks, allowlists, or conditions; or
- a global fast/balanced/frontier model router.

Gobby previously removed a prompt-level memory-recall classifier because hybrid search was
cheap enough to run for every eligible prompt. Reintroducing classification before recall
would add latency without avoiding meaningful work. Classification belongs after retrieval,
where it judges the returned candidates.

## Existing MCP servers

No official TypeSafe MCP server was found as of the research date. Three community projects
are relevant:

| Project | Shape | OpenRouter | Assessment |
| --- | --- | --- | --- |
| [`itsmostafa/typesafe-mcp`](https://github.com/itsmostafa/typesafe-mcp) | One generic `evaluate` tool; Go binary | Yes | Best minimal agent-facing experiment |
| [`jkudish/jev-mcp`](https://github.com/jkudish/jev-mcp) | Ten recipe-oriented tools; Node 20 | Yes | Useful reference surface; broader than Gobby needs |
| [`blakestone-x/jev-mcp`](https://github.com/blakestone-x/jev-mcp) | Seven tools; Python | Direct TypeSafe in documented setup | Useful production-data measurements |

For a no-core-code trial, register the minimal server behind Gobby's MCP proxy and pass the key
as `OPENROUTER_API_KEY: $secret:OPENROUTER_API_KEY`. Pin `typesafe/jev-1.13`. Do not place the
key, provider, or model in agent-callable arguments.

An external MCP is useful for ad hoc agent calls and evaluation. It cannot support mandatory
daemon hot paths without making the daemon call back through MCP and duplicating auth,
timeouts, metrics, and fallback policy.

## Recommended native design

If the pilot passes, add the least mechanism that supports the three decision primitives:

- `AICapability.DECISION_EVALUATE`, separate from `TEXT_GENERATE`.
- `DecisionEvaluationService` with typed state, Choice, Score, Noul, answer, usage, and
  provider-metadata models.
- An OpenRouter adapter isolated around the alpha Decisions wire contract.
- Dedicated endpoint configuration that reuses `$secret:OPENROUTER_API_KEY` without reaching
  into another generation endpoint's internals.
- One internal MCP tool, `gobby-decisions:evaluate`, for agents and pipelines.
- Direct service calls from memory, workflow, and recommendation consumers.

The MCP tool should accept state and bounded typed questions. It should return selected labels,
probabilities, confidence where supplied by the primitive, resolved model, latency, usage, and
fallback provenance. Provider selection, credentials, feature thresholds, and policy actions
remain daemon-side. Existing pipeline MCP steps can invoke the tool and branch with normal
deterministic conditions; no new pipeline step type is needed.

Transport behavior should include strict response parsing, short caller-owned timeouts, bounded
retry for 429/529 and transient 5xx responses, circuit breaking, and redacted logs. Persist the
pinned model ID, question-schema hash, protocol version, probabilities, latency, usage, chosen
policy action, and fallback reason. Do not log raw repository, transcript, tool-argument, or
memory state.

Failure policy is feature-specific:

| Consumer | Failure behavior |
| --- | --- |
| Memory shadow labels | Mark retryable; no retrieval effect |
| Tool/skill recommendation | Return semantic ranking |
| Found-work confirmation | Preserve the current deterministic fast-path behavior |
| Advisory action-risk check | Require existing review/approval; never convert failure to allow |
| Explicit pipeline decision | Fail the step with typed provider/timeout metadata |

## Evaluation and promotion

Start with recorded, labeled Gobby traffic. Do not copy probability thresholds from examples or
between primitives. Probability, confidence, and weighted score have different semantics.
Vercel's [threshold guidance](https://vercel.com/i/jev-probabilities-and-thresholds) likewise
recommends labeled examples, shadow operation, and an explicit destination for failures.

For each feature, record:

- confusion matrix, precision, recall, and false-action rate;
- Brier score or log loss plus expected calibration error;
- selective accuracy and coverage at candidate thresholds;
- option-order sensitivity and results under irrelevant/adversarial context;
- p50/p95 latency, timeout and fallback rate, 429/5xx rate, and cost; and
- downstream outcome, such as subsequent tool success, rather than agreement alone.

For the memory pilot, compare Jev with current judge labels and existing human-audited samples.
Freeze the development cohort and confirm any selected configuration on a disjoint holdout.
For tool recommendation, add Recall@k, MRR/NDCG, reject-all accuracy, and actual tool-call
success. For found-work, measure false clears separately from false alerts because their costs
are asymmetric.

Promote one feature at a time. Keep the incumbent path as fallback until the pinned Jev version
passes its use-case-specific gate. A model upgrade is a new evaluator and requires replay plus
holdout confirmation.

## Evidence quality and risks

TypeSafe launched Jev on 2026-09-15, and OpenRouter listed it shortly afterward. No public model
paper, weights, parameter count, training corpus, or self-hosting option was found. TypeSafe's
launch post claims 70–500 ms end-to-end latency and 40–200x improvements on System-One-shaped
workloads, while acknowledging that the headline comparisons are high-end results and that its
team authored the evaluated workflows. Treat these as vendor claims. See
[Introducing System One Models and Jev](https://typesafe.ai/blog/introducing-system-one-models-and-jev).

Independent evidence is limited. A community MCP author reports that richer label descriptions
improved agreement from 81.0% to 84.5% on 200 production examples. Reversing label order changed
32 of 200 decisions, with low average confidence on the flips. This supports domain calibration
and explicit order-sensitivity tests rather than blind trust in confidence. See
[`blakestone-x/jev-mcp` measurements](https://github.com/blakestone-x/jev-mcp#what-we-measured).

Pydantic's integration guidance independently warns about irrelevant context, adversarial input,
repeated tool calls, option-order sensitivity, and the need to evaluate against domain data. See
[Pydantic AI's TypeSafe integration](https://pydantic.dev/docs/ai/models/typesafe/).

Before sending repository content, sessions, tool arguments, or memories, enforce OpenRouter
zero-data-retention and deny data collection at the account or API-key guardrail level. The
alpha Decisions request may not support the normal per-request provider controls. Zero retention
still means data is transmitted to OpenRouter and TypeSafe; it is not local execution. See
[OpenRouter guardrails](https://openrouter.ai/docs/guides/features/guardrails/overview) and
[zero-data-retention guidance](https://openrouter.ai/blog/insights/zero-data-retention/).

## Recommendation

Run one shadow-only Jev evaluation on memory relevance through a community MCP or a disposable
direct harness using the existing OpenRouter secret reference. Pin `typesafe/jev-1.13`, enforce
privacy guardrails, and compare against frozen Gobby labels and the current judge. This proves
the model, provider route, latency, calibration, and privacy assumptions without changing user
behavior.

If it passes, implement the native decision service and adopt tool/skill reranking first, then
found-work confirmation. Keep all policy deterministic and feature-local. Do not build a general
speed router, generative abstraction, or authoritative classifier around an alpha API.
