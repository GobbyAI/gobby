Plan artifact: `.gobby/plans/decision-classifier-path.md`

# Decision Classifier Path: A Provider-Agnostic Home for Jev and Its Open Clones

**Plan ID:** decision-classifier-path

## Overview
`kind: framing`

Task #23024 (Plan a provider-agnostic classifier path for Jev or open-source
alternatives), under #22949. On 2026-09-28 Josh asked that "Jev (or an open
source variant) get a path like embeddings gets a path." Today Gobby has no
decision capability. The only related code is five `decisions_*` keys on
`code_index.community_label`, which nothing reads. They are reserved for #22604's
unlanded gate.

This plan adds that path. It mirrors the four parts of the embedding path:
- a config block, `ai.decisions`;
- a capability, `AICapability.DECIDE`, with a registry binding that reports why
  it is unavailable;
- one service module, `gobby.ai.decisions`;
- per-consumer policy.

The provider contract is the `POST /v1/systemone` wire. TypeSafe's Jev, Kev and
the other open clones all serve it. Jev is one backend; the contract is the wire.

The plan also sets:
- a shadow-capture and evaluation harness, so every consumer is promoted on
  measured quality, calibration and latency;
- two consumers: MCP tool reranking and found-work confirmation;
- how #22604's community-label gate consumes the service;
- the activation gate that #22075 waits on.

## Decision Record
`kind: framing`

1. **The contract is the `/v1/systemone` wire.** The request is
   `{model, state, questions}`. It returns a typed answer per question key.
   Several backends serve it:
   - TypeSafe's direct endpoint;
   - Kev (Apache-2.0, MLX on Apple Silicon), whose server takes TypeSafe's SDK
     unchanged;
   - jeff and reflex.

   `api_base` is the server root; the service posts to `{api_base}/v1/systemone`.
   OpenRouter serves Jev at `/api/alpha/decisions`, an alpha contract that
   #22604's 6.3 records as unverified. No path or `wire` field is added for it,
   because Decision 2 leaves one wire in use and a knob with one value is
   unjustified. Adding the OpenRouter path is a later task, taken only if Josh
   lifts Decision 2.
2. **Loopback by default, hosted only by explicit opt-in.** Josh ruled on
   2026-09-24 (relayed by #14069) that no memory, session or repository text
   goes to a hosted decision provider for the pilots. On 2026-09-29 he chose
   the `allow_remote` button: loopback stays the default, and a hosted or
   other non-loopback endpoint requires `ai.decisions.allow_remote: true`.
   - `DecisionsConfig.allow_remote: bool = False`.
   - With `allow_remote` false, `api_base`'s host must be `localhost`, an
     address in `127.0.0.0/8`, or `::1`. Any other host fails config
     validation, and the error names `allow_remote` and cites the ruling.
   - With `allow_remote` true, any host that passes
     `validate_optional_endpoint_url` is accepted, and the daemon logs one
     WARNING at startup naming the host, because consumer text then leaves
     the machine.
   - Rejected: policy prose alone. Prose makes "provider-agnostic" and "no
     hosted" read as a contradiction and enforces neither.
   - A remote backend reaches `enforce` only with a Decision 10 backend
     identity. An endpoint with no `/v1/models` card runs as `shadow`, fail
     closed.
   - The OpenRouter `/api/alpha/decisions` path stays a separate later task
     (Decision 1). `allow_remote` admits only `/v1/systemone` endpoints.
3. **Gobby points at a server and never runs one.** Kev's server is its own
   process. Gobby does not install, start, stop or load models (memory
   606b6838). Josh ruled on 2026-09-24 that local Kev is an opt-in installer
   option after the 0.6 golden path, beside MLX embeddings (memory 5d9cd410),
   and that installer work belongs to the golden-path installer.
   - `local-inference-runtime-foundation.md` is out of scope because its family
     roles cover LM Studio, Ollama and vLLM, and Kev supports none of them.
   - The one-model-instance rule (memory a5ad8b5c) applies to any local
     decision server while the daemon runs.
4. **`ai.decisions` lives on `AIConfig`.** It is a new
   `AIConfig.decisions: DecisionsConfig` field, read directly with no
   translation layer.
   - Embeddings need a separate runtime section, a key inventory and a switch
     journal because stored vectors carry model identity. Decisions persist no
     model-bound state, so none of that machinery applies.
   - `$secret:NAME` in `api_key` is expanded at load by
     `config/_loading.py`, the same as every other secret reference.
5. **The capability mirrors `EMBED`.** `AICapability.DECIDE = "decide"` joins
   the enum, and `_decision_binding` in `ai/registry_builder.py` follows
   `_embedding_binding`:
   - it is `unavailable` with a reason when `api_base` or `model` is unset;
   - otherwise it is available, with provider `local`, and
     `metadata = {model, max_input_tokens}`.

   `GET /api/llm/status` already returns `registry.status_snapshot()` for every
   capability, so no new route is needed. This supersedes the research doc's
   "no registry capability until an agent-facing need," per Josh's 2026-09-28
   clarification.
6. **No MCP tool.** Every consumer is daemon-internal and calls the service
   directly. `jev.md`'s `gobby-decisions:evaluate` tool has no caller.
   Rejected under `restraint`. An agent-facing need reopens it.
7. **Primitives: Choice first, then Noul, as a proposed build order.**
   Josh's 2026-09-24 ruling reads: Choice only until the second consumer adds
   Noul. This plan proposes, for Josh's approval, to apply it as build order:
   - 1.2 builds `choose`, the client that #22604's stamped 6.3.1 anchors.
   - 3.1 builds `noul` together with tool rerank, the in-plan consumer that
     needs it, and 3.2 reuses it.
   - Score has no consumer and is not built.

   No consumer is live today. #22604 keeps its own independent parking, and
   tool rerank ships in `shadow`. No ordering edge ties 3.1 to #22604 in
   either direction, and there is no rollout or activation order between
   community labels and tool rerank. In-plan sequencing stays
   1.2, 2.1, 3.1, 3.2.
8. **Request ceiling and truncation guard.** At Kev commit `0fe8fc97c2bc` (2026-09-29), Kev trains on
   384 state tokens and serves up to 65,536 state tokens
   (`kev/model.py::SERVE_MAX_STATE`) and 73,728 branch tokens
   (`SERVE_MAX_BRANCH`). `Server.submit` calls `encode` without `strict=True`,
   so an overlong state is truncated from its end with no error. Its response
   reports `usage.input_tokens`, the encoded length after truncation
   (`kev/serve.py::Server._body`).
   - `ai.decisions.max_input_tokens` (default `8192`) is a Gobby-side quality
     and latency ceiling on the estimated request size,
     `len(json.dumps(body)) // 4`. No Python token estimator exists; gcore's
     `estimate_tokens` is Rust-only. The estimate is advisory and carries no
     truncation guarantee.
   - The service raises `DecisionsUnavailable(reason="oversize")` before
     sending when the estimate exceeds the ceiling.
   - The truncation guard is enforced per response. A truncated state always
     leaves at least `backend_max_state_tokens - 1` input tokens, so a
     response whose `usage.input_tokens` reaches that value raises
     `DecisionsUnavailable(reason="truncated")`, and so does a response with no
     `usage.input_tokens`. `backend_max_state_tokens` defaults to 65,536 from
     the pinned Kev commit. A false positive fails closed.
   - Consumers batch to `service.max_input_tokens`.
   - The evaluation (2.1) measures quality at the state sizes each consumer
     actually sends, because every consumer runs far beyond Kev's training
     length.
9. **Failure and cooldown.**
   - 401, 403, 422 and every other 4xx raise without retry. Every 3xx is a
     non-retryable `http_status`.
   - Transport errors and 429, 529 and 5xx retry through `retry_async`
     (`llm/claude_runtime.py:141`): one attempt plus at most two retries.
     `asyncio.timeout(timeout_seconds)` bounds the whole call, backoff
     included. `retry_async` gains an optional caller retry predicate, because
     its default `is_transient_error` (`:40-53`) would retry a 422.
   - Retries use `retry_async(..., max_retries=3, delay=0.1)`, so the
     backoffs are about 0.1 s and 0.2 s. Three attempts fit inside the default
     2 s budget, and the budget cuts the sequence short when it expires first.
   - A call's budget is `min(timeout_seconds, caller timeout)`. Consumers pass
     the time they have left.
   - Every remote failure opens a cooldown of `failure_cooldown_seconds`
     (default 60): transport errors, exhausted transient statuses, permanent
     `http_status`, `parse`, and a `timeout` at the full configured budget.
     During it, calls fail fast with `reason="cooldown"` and never dial. This
     keeps a dead or misconfigured server from charging the found-work hook
     its timeout on every Stop.
   - Local outcomes never open it: `unconfigured`, `oversize`, a `timeout`
     under a caller budget shorter than `timeout_seconds`, and caller
     cancellation. So one consumer's short budget or oversized input cannot
     disable the shared service for the others. `asyncio.CancelledError`
     propagates unchanged.
   - `get_decision_service(config)` keeps one cached service, identified by a
     fingerprint of every service-affecting field: `api_base`, `model`, a hash
     of the resolved `api_key`, `allow_remote`, `identity_contract`,
     `timeout_seconds`, `max_input_tokens`, `backend_max_state_tokens`, and
     `failure_cooldown_seconds`. It is replaced whenever the fingerprint
     changes.
     - The cache exists only to share cooldown state.
     - The credential hash is used for identity only and is never logged.
     - The httpx client opens and closes per call, following the embedding
       transport pattern. `EmbeddingService.from_config` itself caches
       nothing.
10. **Per-consumer policy, typed.** `DecisionsConfig` carries one typed field
    per consumer rather than a free map:
    - `community_label`: `mode`, `min_confidence` (default `0.5`),
      `evaluated_model`, and `evaluated_backend`.
    - `tool_rerank`: `mode`, `min_probability`, `evaluated_model`, and
      `evaluated_backend`.
    - `found_work`: `mode`, `accept_below`, `accept_above`,
      `evaluated_model`, and `evaluated_backend`.

    `mode` is `off`, `shadow` or `enforce`, and defaults to `off`.

    How `enforce` behaves:
    - It acts only when `evaluated_model == ai.decisions.model` and
      `evaluated_backend` equals the backend identity returned with that same decision.
      Otherwise the consumer runs as `shadow` and logs
      `evaluated_backend_mismatch` once per cooldown window.
    - Kev echoes the requested model name in `response.model` and accepts
      both `kev-latest` and `jev-latest` for any loaded checkpoint
      (`kev/serve.py::MODEL_NAMES`, `Server._body`). So the alias alone proves
      nothing about which checkpoint answered.
    - Backend identity is `sha256:` plus the sha256 of the canonical JSON of
      the stable fields in the configured model's `GET {api_base}/v1/models`
      card: run path, base model, LoRA config, dtype, and calibration
      temperature, plus the configured `backend_max_state_tokens`. Runtime
      statistics are excluded.
    - Identity is established for every logical decision. Each `choose` or
      `noul` call fetches the card concurrently with the decision request,
      under the same deadline, and returns the identity with the answers. A
      server restarted onto a different checkpoint therefore shows up on the
      very next decision. A failed fetch, or a card missing any of those
      fields, yields no identity, and enforce then runs as shadow.
    - `ai.decisions.identity_contract` names how identity is verified, and
      consumer code never branches on vendor:
      - `model_card`: the card hash above, the verified Kev contract.
      - `response_version`: identity is `response:` plus the response's
        `model`. Hosted Jev resolves an alias to a versioned ID there, and
        TypeSafe recommends pinning that ID for tuned thresholds
        (`https://docs.typesafe.ai/models.md`). Its `/v1/models` lists only
        name, description, and release date, so no card is fetched. This
        contract requires `allow_remote: true`, because Kev echoes any
        requested name and so gives no version signal.
      - unset (the default): no identity, so every consumer stays in
        `shadow`, fail closed.
    - Residual gap: under `model_card`, the card fetch and the decision
      request are separate requests. A per-call card detects an observed
      change but never proves which checkpoint answered: card A can succeed
      while a restarted server answers the decision, or its retry, from
      checkpoint B. Kev also reports the run path as submitted, unresolved,
      with no server source version, so a swap in place at the same path
      with an identical card, or a server upgrade that changes its state
      limit, is invisible. Under `response_version` the identity comes from
      the answering response itself.
    - Deployment owner rule, for every backend: before any checkpoint or
      server replacement, the owner moves every consumer out of `enforce`
      and lets in-flight decisions drain. After it, a new live capture
      (Activation Gate item 4) and a new evaluation precede any return to
      `enforce`, and a new checkpoint gets a new run path. The plan claims
      detection only for what the card, the response, and the config report.
    - Consumers read `mode` and thresholds from the resolver's current config
      on every call. The service fingerprint excludes consumer fields, so a
      policy-only reload takes effect on the next call without rebuilding the
      service.
    - A model or backend change is therefore a new evaluator by construction
      (`jev.md`), and it can never inherit thresholds.
    - Probability, confidence and weighted score are never compared across
      primitives.

    The five `code_index.community_label.decisions_*` keys are removed
    (`config/code_index.py:58-79`). They were reserved for #22604 and nothing
    reads them.
11. **Shadow first; the incumbent is the fallback and the stronger judge.**
    Shadow mode calls the classifier beside the incumbent, changes no behavior,
    and appends one local record for evaluation.
    - Tool rerank's incumbent is the LLM rerank.
    - Found-work's incumbent is `call_json_feature`.
    - Community labels' incumbent is the deterministic label.

    In enforce mode found-work is a confidence cascade:
    - a confident classifier verdict is accepted;
    - an uncertain one escalates to the incumbent LLM call;
    - an unavailable classifier also escalates, so an outage never clears a
      finding.

    This is the accept-when-confident, escalate-when-unsure design from arXiv
    2609.26550 (Decision 13).
12. **Promotion gates, measured on the frozen holdout (2.1).** A consumer
    enters `enforce` only after the Activation Gate's live capture (item 4)
    holds and its report is committed under `docs/evidence/decisions/`, and
    only when:
    - expected calibration error is at most 0.10;
    - the option-order or candidate-order flip rate is at most 10%;
    - p95 latency is at most 1 s against the configured backend, local or
      remote;
    - its consumer bar is met:
      - tool rerank: Recall@k no lower than the LLM rerank, with reject-all
        accuracy reported;
      - found-work: the false-clear rate is no higher than the incumbent's,
        the escalation rate is at most 50%, and false clears and false alerts
        are reported separately;
      - community labels: #22604's Q1.6 bars.

    The ECE, flip-rate, and latency bars apply to the two harness consumers,
    tool rerank and found-work. The community-label gate is #22604's Q1.6
    report alone, which that task owns and supplies.

    Thresholds come from the development split and are confirmed on the
    holdout. No threshold is copied from vendor examples.
13. **arXiv 2609.26550, verified.** "JEV-as-a-Judge: Accept When Confident,
    Escalate When Unsure", by Yubo Li, Yidi Miao, Ramayya Krishnan and Rema
    Padman (v1 2026-09-22, v2 2026-09-27). Checked against the arXiv abstract
    page.

    What the paper reports:
    - Jev comes within three points of GPT-6 on verdict-readable tasks, at
      0.36% of the cost.
    - Median latency is 0.15 s.
    - The cascade matches GPT-6 at 41% of its cost.

    Its caveats:
    - Jev degrades on math, code and logic derivation.
    - Confidence routing weakens on style-adversarial pairs and reference-free
      prose.

    Limits on applying it here:
    - The numbers are for hosted Jev. Kev is about four points below Jev at 4B
      and 9B (builder-reported).
    - The cascade is used only where the verdict is extraction-shaped: whether
      a final message defers asked-for work.
    - No consumer asks the classifier to judge code correctness.
14. **#22075 assumption, the PD's disposition for Josh's buttons.** It is
    recorded as the plan assumption on 2026-09-29.
    - Classifying outage types from pane and transcript evidence (usage window,
      model capacity, or transient) is a future consumer of this path.
    - Candidate fallback selection stays deterministic, reading the typed
      persisted outage mark, and is never a classifier decision.
    - The consumer contract and the activation gate are in "Activation Gate
      and #22075".

## As-Is Facts
`kind: framing`

- `AICapability` (`ai/registry.py:33-43`) has `embed`, `audio_transcribe`,
  `audio_translate`, `vision_extract`, `text_generate`, `tool_chat`,
  `agent_spawn`, and `web_chat`. There is no decision capability.
  `CANONICAL_AI_CAPABILITIES = tuple(AICapability)`.
- The embedding precedent:
  - `EmbeddingsConfig` (`config/persistence.py:187-248`, `extra="ignore"`,
    `validate_optional_endpoint_url` on `api_base`);
  - `DaemonConfig.embeddings` (`config/app.py:327`);
  - `_embedding_binding` (`ai/registry_builder.py:126-162`);
  - `EmbeddingService.from_config` (`ai/embeddings.py:922`).
- `AIConfig` (`config/ai.py:281`, `extra="forbid"`) holds `generation` and
  `model_metadata_aliases`. `DaemonConfig.ai` is at `config/app.py:331`.
- `GET /api/llm/status` (`servers/routes/llm.py:229-233`) returns
  `build_daemon_ai_capability_registry(config).status_snapshot()`.
- Generation endpoints speak only `chat-completions` and `responses`
  (`config/ai.py:47-94`). Neither can call a decision wire, so configuring Jev
  as a generation model would be a false integration (`jev.md`).
- #22837 retired the recall-signal stack on 2026-09-27 (commit 3394b5e696,
  "retire the recall-signal stack"). That removed
  `memory/shadow_relevance.py`, the `recall_*` modules, and the
  `memory_usefulness` config. `config/app.py:169` now rejects that config.
  - The research doc's second pilot, the relevance-judge shadow run, and
    `jev.md`'s "best pilot" no longer exist.
  - Their replay and calibration machinery went with them.
  - This plan's 2.1 supplies the evaluation harness.
- Surviving consumers in code:
  - `CommunityLabeler` (`code_index/community_labeler.py`, 210 lines) landed
    with #22604's 6.2. Its 6.3 gate has not landed.
  - `RecommendationService._recommend_hybrid`
    (`mcp_proxy/services/recommendation.py:146-212`) takes semantic top
    `2 * top_k`, then an LLM rerank through `call_feature`. It falls back to
    semantic order (`search_mode="hybrid_fallback"`) on any exception. It is
    constructed at `mcp_proxy/server.py:120` with `config_resolver` and
    `llm_service_resolver`.
  - `FoundWorkStopAnalyzer._confirm_shirk`
    (`workflows/found_work_gate.py:531-568`) calls `call_json_feature` with the
    `gobby_tasks.validation` config and an 8 s cap. It returns `True`, `False`,
    or `None`, and on `None` the caller alerts on the fast-path verdict. It is
    constructed at `workflows/hooks.py:133` with `config_resolver`.
- `retry_async` is at `llm/claude_runtime.py:141-164`. The daemon already
  depends on `httpx`.

## Constraints
`kind: framing`

- This is a planning task, so it contains no code. The leaves below are the
  implementation, and none executes before Josh approves the plan.
- Line counts on 2026-09-29, against the 1,000-line ceiling:

  | File | Lines | Note |
  | --- | --- | --- |
  | `found_work_gate.py` | 995 | 3.2 decomposes it first |
  | `registry_builder.py` | 759 | |
  | `config/persistence.py` | 679 | untouched |
  | `config/app.py` | 535 | untouched |
  | `ai/registry.py` | 505 | |
  | `recommendation.py` | 270 | |
  | `config/code_index.py` | 209 | |
  | `ai/embeddings.py` | 1,017 | untouched; already over the ceiling |
- No raw state in logs. The daemon log carries identifiers, counts, hashes,
  latency and outcomes only. Shadow records hold inputs in a machine-local
  0600 file that is never committed (2.1).
- A restart is needed to load new Python. The PD owns it, with global notices
  before and after, outside quiet hours (04:45-06:45 CT).
- Found work in touched files is fixed in its leaf under the repository ladder.

## Coordination With #22604
`kind: framing`

#22604 (the community-label Jev gate) stays the owner of the label gate's
admission logic, its Q1.6 label-quality study, and its outcome discriminator.
Its plan is `.gobby/plans/completed/gcode-import-communities.md` §6.3, with a
stamped M1, so that file is not edited. At expansion, the PD updates task
#22604's description to consume this plan and adds a dependency from #22604 to
1.2. That updated description:
- drops `src/gobby/llm/decisions.py` and `tests/llm/test_decisions_client.py`,
  and calls `gobby.ai.decisions.get_decision_service(config.ai.decisions).choose`
  instead;
- reads `ai.decisions.community_label.min_confidence`, `mode`, and
  `evaluated_model`, since the `code_index.community_label.decisions_*` keys
  are gone after 1.1;
- sizes each batch to `service.max_input_tokens`. At the 8,192-token ceiling and
  6.3's budget of about 500 tokens of state plus 250 per question per
  community, that is at most 10 communities per request instead of 20;
- records its wire spike against the local `/v1/systemone` server only. The
  OpenRouter alpha arm is dropped under Decisions 1 and 2, and the Activation
  Gate's live capture (item 4) satisfies the spike;
- keeps 6.3.2 to 6.3.5 unchanged.

Two stamped 6.3 items change their anchors:
- 6.3.1's test anchor (`tests/llm/test_decisions_client.py`) is satisfied by
  1.2.1 here.
- 6.3.6's two-arm spike is satisfied by the Activation Gate's live capture
  (item 4), with the OpenRouter arm dropped under Decision 2.

The completed plan's M1 is not edited. The PD records both substitutions in
#22604's description and validation criteria at expansion, so the close
reviewer checks the new anchors. This is one of the PD's disposition items.

#22604 keeps its independent `enhancement` parking (Josh, 2026-09-23). This
plan adds no dependency on it in either direction.

## Activation Gate and #22075
`kind: framing`

The decision classifier counts as **deployed**, beyond merely planned, only
when every one of these holds:

1. Leaves 1.1 and 1.2 are landed on `0.5.0`, and the PD has restarted the
   daemon from the main checkout.
2. A `/v1/systemone` server is reachable, and `ai.decisions.api_base` and
   `ai.decisions.model` name it. By default it listens on loopback on Josh's
   machine: Kev from the post-0.6 installer option, or any compatible server
   Josh runs. A non-loopback endpoint also needs `ai.decisions.allow_remote:
   true` (Decision 2).
3. `GET /api/llm/status` reports `decide` available with that model.
4. The PD has verified a live capture against that server, committed as
   `docs/evidence/decisions/systemone-live-<date>.md`:
   - the Decision 10 backend identity under the configured
     `identity_contract`;
   - one Choice and one Noul round trip that the service parses;
   - the measured characters-per-token ratio on a path-heavy request;
   - the server's actual state limit, which must equal
     `backend_max_state_tokens`, and evidence that `usage.input_tokens`
     counts every encoded state token, by contract:
     - `model_card` (a server Josh runs): the server's source commit, its
       `/v1/models` card, and the limit and usage semantics read from that
       source;
     - `response_version` (hosted): `ai.decisions.model` set to the
       versioned ID, such as `jev-1.13.0`, and every captured response
       reporting that ID; TypeSafe's documented state limit, error codes,
       and `usage.input_tokens` semantics, cited by URL and retrieval date;
       and two live checks: a request estimated just under
       `backend_max_state_tokens` answers with a `usage.input_tokens`
       consistent with the measured ratio, and a request over it returns a
       documented error or a usage the Decision 8 guard classifies as
       `truncated`.

     If the limit or the usage semantics are undocumented, unknown, or
     mismatched, this item does not hold.

   No consumer leaves `shadow` before this item holds. Gobby does not start,
   stop, or install the server.
5. At least one consumer is in `mode: enforce`. It has passed its Decision 12
   gate, and its report is committed under `docs/evidence/decisions/`.

The Assistant presents that state to Josh when it holds. Until then, #22075
(Detect provider usage outages across all providers and fall back to the next
candidate) stays blocked.

#22075 also remains `deferred-by-josh` under his 2026-09-10 instruction.
Closing #23024 does not unblock it.
- At expansion, the PD moves #22075's `blocked_by` edge from #23024 to leaf
  3.2, the last consumer leaf.
- Gate items 2 to 5 are operational conditions, not leaves. The PD verifies
  them and records them on #22075 before lifting anything.
- Josh lifting `deferred-by-josh` is the final hold.

The consumer contract #22075's own plan inherits (Decision 14):

- Exact per-provider rules run first. The classifier sees only an error-shaped
  pane or transcript excerpt that no rule matched, redacted by the watchdog's
  existing redaction.
- The question is a Choice among `usage_window`, `model_capacity`, `transient`,
  and `other`.
- `other`, confidence below that consumer's threshold, or a classifier that is
  unavailable all yield today's behavior: the generic `provider_error`, with no
  rotation and no outage mark.
- Candidate fallback selection reads only the typed persisted outage mark in
  `provider_capacity`. No classifier call occurs on the selection path.

## P1: Capability and Service
`kind: framing`

**Goal:** Gobby has a configured, status-reporting decision capability and one
service that speaks the `/v1/systemone` wire.

### 1.1 `ai.decisions` config and the `decide` capability binding [category: code]
`kind: deliverable`

Targets:
- `src/gobby/config/ai.py::*` — scope-reason: add `DecisionsConfig`, the three typed consumer configs, the loopback validator with its `allow_remote` opt-in, and `AIConfig.decisions`
- `src/gobby/config/code_index.py::CodeIndexCommunityLabelConfig`
- `src/gobby/ai/registry.py::AICapability`
- `src/gobby/ai/registry_builder.py::*` — scope-reason: add `_decision_binding` and register it in `build_daemon_ai_capability_registry`
- `docs/audits/configuration-audit.md`
- `crates/gcore/assets/config/runtime_config_contract.json::*` — scope-reason: derived config carrier; regenerated with `scripts/generate_runtime_config_contract.py --stdout`
- `web/src/api/runtimeConfigCodecVectors.gen.ts::*` — scope-reason: derived config carrier; regenerated with `scripts/generate_runtime_config_contract.py --stdout-web` and expected unchanged unless key encoding moves
- `tests/config/test_decisions_config.py`
- `tests/ai/test_capability_registry.py::*` — scope-reason: cover the `decide` binding's unavailable and available states
- `tests/code_index/test_community_labeler.py::test_ungated_validated_name_writes_model_label`

**Granularity:** Sixteen acceptance items, one outcome: a loadable
`ai.decisions` config surfaced as the `decide` capability. 1.1.5 to 1.1.16
give each row of the `DecisionsConfig` table its own obligation, because
plan-coverage requires one item per work-enumerating table row. The rows share
one model, one validator set, and two parametrized tests, and no row can land
alone without leaving `AIConfig` unloadable.

**Research context:** The precedent is `EmbeddingsConfig`
(`config/persistence.py:187-248`), which uses
`validate_optional_endpoint_url(value, field_name="api_base")` from
`config/url_validation.py`, and `_embedding_binding`
(`ai/registry_builder.py:126-162`). That binding returns
`CapabilityBinding.unavailable(capability, provider, adapter_style=..., reason=..., models=..., metadata=...)`
when unconfigured and `CapabilityBinding(...)` when available. Use the
existing `AIAdapterStyle.LOCAL` (`ai/registry.py:49`). `OPENAI_COMPATIBLE`
names a different protocol, and no enum member is added.

`DecisionsConfig` (`extra="forbid"`, like `AIConfig`) has these fields:

| Field | Type | Default |
| --- | --- | --- |
| `api_base` | `str \| None` | `None` |
| `allow_remote` | `bool` | `False` |
| `identity_contract` | `Literal["model_card", "response_version"] \| None` | `None` |
| `api_key` | `str \| None` | `None` |
| `model` | `str \| None` | `None` |
| `timeout_seconds` | `float` | `2.0`, `gt=0` |
| `max_input_tokens` | `int` | `8192`, `ge=256`, below `backend_max_state_tokens` |
| `backend_max_state_tokens` | `int` | `65536`, `gt=0` |
| `failure_cooldown_seconds` | `float` | `60`, `ge=0` |
| `community_label` | `ChoiceConsumerConfig` | `default_factory` |
| `tool_rerank` | `RerankConsumerConfig` | `default_factory` |
| `found_work` | `CascadeConsumerConfig` | `default_factory` |

`AIConfig.decisions = Field(default_factory=DecisionsConfig)`, so an existing
config loads with the capability unconfigured and every consumer `off`.
`min_confidence`, `min_probability`, `accept_below`, and `accept_above` each
carry `Field(ge=0, le=1)`.

The consumer configs:
- `ChoiceConsumerConfig`: `mode: Literal["off", "shadow", "enforce"] = "off"`,
  `min_confidence: float = 0.5`, `evaluated_model: str | None = None`,
  `evaluated_backend: str | None = None`.
- `RerankConsumerConfig`: `mode`, `min_probability: float = 0.5`,
  `evaluated_model`, `evaluated_backend`.
- `CascadeConsumerConfig`: `mode`, `accept_below: float = 0.1`,
  `accept_above: float = 0.9` (validated `accept_below < accept_above`),
  `evaluated_model`, `evaluated_backend`.

`api_key` accepts a `$secret:` reference, which `config/_loading.py`
resolves at load like every other secret field.

Validators:
- `api_base` passes `validate_optional_endpoint_url`. Unless `allow_remote`
  is true, its parsed host must be `localhost`, an `ipaddress` loopback
  address, or `::1`. Otherwise it raises `ValueError("ai.decisions.api_base
  must be a loopback address unless ai.decisions.allow_remote is true (Josh,
  2026-09-24: no hosted text by default)")`.
- `model` is required when `api_base` is set.
- `mode == "enforce"` with `evaluated_model` or `evaluated_backend` unset is
  rejected, because enforcement needs a recorded evaluation.
- `max_input_tokens >= backend_max_state_tokens` is rejected.
- `identity_contract == "response_version"` without `allow_remote` is
  rejected.

`CodeIndexCommunityLabelConfig` loses `decisions_api_base`,
`decisions_api_key`, `decisions_model`, `decisions_min_confidence`, and
`decisions_timeout_seconds` (`config/code_index.py:58-79`). No reader exists;
confirm with `gcode grep -F "decisions_" -m 40 src/`. The class inherits
`FeatureDefaultConfig`'s `extra` behavior; if stored rows carry those keys and
the class forbids extras, add the keys to that config's removed-key rejection
the way `config/app.py:169` rejects `memory_usefulness`. The executor checks
`CodeIndexConfig`'s `model_config` first. The configuration audit's rows for
the five keys (`docs/audits/configuration-audit.md:456-460`) become one
`ai.decisions` entry.

`tests/code_index/test_community_labeler.py::test_ungated_validated_name_writes_model_label`
asserts `CodeIndexCommunityLabelConfig().decisions_api_base is None` (`:332`)
as its ungated precondition. That line becomes
`assert DecisionsConfig().community_label.mode == "off"`, which states the same
precondition against the shared config. The rest of the test is unchanged.

`_decision_binding(config)`:
- `unavailable` with reason "Decision capability requires ai.decisions.api_base
  and ai.decisions.model." when either is unset;
- otherwise available, with provider `local`,
  `models=(config.ai.decisions.model,)`, and
  `metadata={"max_input_tokens": ..., "api_base_configured": True}`.

Register it in `build_daemon_ai_capability_registry` beside
`_embedding_binding`. Any test that pins the capability list or the
status-snapshot order in `tests/ai/test_capability_registry.py` gains `decide`.

Config carriers: any `.py` change under `src/gobby/config/` regenerates
`crates/gcore/assets/config/runtime_config_contract.json` and
`web/src/api/runtimeConfigCodecVectors.gen.ts` with
`scripts/generate_runtime_config_contract.py` (`--stdout` and `--stdout-web`);
`tests/config/test_runtime_config_contract.py` checks both byte for byte.

Consumers unchanged:
- `src/gobby/ai/__init__.py` — no-edit-reason: it names existing `AICapability` members only; adding `DECIDE` changes no existing member or lookup.
- `src/gobby/ai/_text_generation_service.py` — no-edit-reason: it names existing `AICapability` members only; adding `DECIDE` changes no existing member or lookup.
- `src/gobby/ai/_tool_chat_codex.py` — no-edit-reason: it names existing `AICapability` members only; adding `DECIDE` changes no existing member or lookup.
- `src/gobby/ai/_tool_chat_droid.py` — no-edit-reason: it names existing `AICapability` members only; adding `DECIDE` changes no existing member or lookup.
- `src/gobby/ai/_tool_chat_service.py` — no-edit-reason: it names existing `AICapability` members only; adding `DECIDE` changes no existing member or lookup.
- `src/gobby/ai/audio.py` — no-edit-reason: it names existing `AICapability` members only; adding `DECIDE` changes no existing member or lookup.
- `src/gobby/ai/vision.py` — no-edit-reason: it names existing `AICapability` members only; adding `DECIDE` changes no existing member or lookup.
- `src/gobby/servers/routes/voice.py` — no-edit-reason: it names existing `AICapability` members only; adding `DECIDE` changes no existing member or lookup.
- `tests/ai/test_audio_capabilities.py` — no-edit-reason: it names existing `AICapability` members only; adding `DECIDE` changes no existing member or lookup.
- `tests/ai/test_endpoint_activation.py` — no-edit-reason: it names existing `AICapability` members only; adding `DECIDE` changes no existing member or lookup.
- `tests/ai/test_tool_chat_protocols.py` — no-edit-reason: it names existing `AICapability` members only; adding `DECIDE` changes no existing member or lookup.
- `tests/communications/test_sticker_vision.py` — no-edit-reason: it names existing `AICapability` members only; adding `DECIDE` changes no existing member or lookup.
- `tests/servers/routes/test_voice_routes.py` — no-edit-reason: it names existing `AICapability` members only; adding `DECIDE` changes no existing member or lookup.

Verification planned: `DATABASE_URL=postgresql://gobby_test:gobby_test@127.0.0.1:60892/gobby_test GOBBY_TEST_PROTECT=1 uv run pytest tests/config/test_decisions_config.py tests/config/test_runtime_config_contract.py tests/ai/test_capability_registry.py tests/code_index/test_community_labeler.py tests/config -q`;
`uv run ruff check src/ && uv run mypy src/`.

**Acceptance:**

- 1.1.1 - With `allow_remote` false, a non-loopback `api_base` fails
  validation with the message naming `allow_remote`, and `localhost`,
  `127.0.0.1`, and `[::1]` pass. With `allow_remote` true, a hosted `https`
  `api_base` passes and one startup WARNING names its host. test:
  `tests/config/test_decisions_config.py::test_api_base_must_be_loopback`.
- 1.1.2 - `model` is required with `api_base`, `enforce` requires
  `evaluated_model` and `evaluated_backend`, `accept_below < accept_above`,
  every threshold outside
  [0,1] is rejected, and an empty `AIConfig` loads with every consumer `off`.
  test:
  `tests/config/test_decisions_config.py::test_decisions_config_invariants`.
- 1.1.3 - The five `code_index.community_label.decisions_*` keys no longer
  exist on the model. symbol:
  `src/gobby/config/code_index.py::CodeIndexCommunityLabelConfig`. test:
  `tests/config/test_decisions_config.py::test_community_label_decision_keys_removed`.
- 1.1.4 - `decide` reports unavailable with its reason when unconfigured and
  available with the model and `adapter_style` `local` when configured, and it
  appears in the registry status snapshot. test:
  `tests/ai/test_capability_registry.py::test_decide_binding_reports_configuration`.
- 1.1.5 - `api_base` row: defaults to `None`, and the capability is
  unavailable while it is unset. test:
  `tests/config/test_decisions_config.py::test_decisions_scalar_rows_defaults_and_bounds`.
- 1.1.6 - `api_key` row: defaults to `None`, a `$secret:` reference loads
  resolved, and the value is never logged. test:
  `tests/config/test_decisions_config.py::test_decisions_scalar_rows_defaults_and_bounds`.
- 1.1.7 - `model` row: defaults to `None`. test:
  `tests/config/test_decisions_config.py::test_decisions_scalar_rows_defaults_and_bounds`.
- 1.1.8 - `timeout_seconds` row: defaults to `2.0` and rejects `0`. test:
  `tests/config/test_decisions_config.py::test_decisions_scalar_rows_defaults_and_bounds`.
- 1.1.9 - `max_input_tokens` row: defaults to `8192`, rejects `255`, and
  rejects a value at or above `backend_max_state_tokens`. test:
  `tests/config/test_decisions_config.py::test_decisions_scalar_rows_defaults_and_bounds`.
- 1.1.10 - `backend_max_state_tokens` row: defaults to `65536` and rejects
  `0`. test:
  `tests/config/test_decisions_config.py::test_decisions_scalar_rows_defaults_and_bounds`.
- 1.1.11 - `failure_cooldown_seconds` row: defaults to `60` and rejects `-1`.
  test:
  `tests/config/test_decisions_config.py::test_decisions_scalar_rows_defaults_and_bounds`.
- 1.1.12 - `community_label` row: defaults to `mode` `off`, `min_confidence`
  `0.5`, and unset `evaluated_model` and `evaluated_backend`, and rejects
  `enforce` without both. test:
  `tests/config/test_decisions_config.py::test_decisions_consumer_rows_defaults`.
- 1.1.13 - `tool_rerank` row: defaults to `mode` `off`, `min_probability`
  `0.5`, and unset `evaluated_model` and `evaluated_backend`, and rejects
  `enforce` without both. test:
  `tests/config/test_decisions_config.py::test_decisions_consumer_rows_defaults`.
- 1.1.14 - `found_work` row: defaults to `mode` `off`, `accept_below` `0.1`,
  `accept_above` `0.9`, and unset `evaluated_model` and `evaluated_backend`,
  and rejects `enforce` without both. test:
  `tests/config/test_decisions_config.py::test_decisions_consumer_rows_defaults`.
- 1.1.15 - `allow_remote` row: defaults to `False`, and only `True` admits a
  non-loopback `api_base`. test:
  `tests/config/test_decisions_config.py::test_decisions_scalar_rows_defaults_and_bounds`.
- 1.1.16 - `identity_contract` row: defaults to `None`, accepts `model_card`,
  and accepts `response_version` only with `allow_remote` true. test:
  `tests/config/test_decisions_config.py::test_decisions_scalar_rows_defaults_and_bounds`.

### 1.2 `DecisionService` with Choice, request ceiling, and cooldown [category: code] (depends: 1.1)
`kind: deliverable`

Targets:
- `src/gobby/ai/decisions.py`
- `src/gobby/llm/claude_runtime.py::retry_async`
- `tests/llm/test_llm_claude.py::TestRetryAsync`
- `tests/ai/test_decisions_service.py`
- `tests/ai/fixtures/systemone_choice_response.json`
- `docs/evidence/decisions/systemone-wire.md`

**Granularity:** Ten acceptance items, one outcome: a `choose` call that
either returns validated answers with their backend identity metadata, which
is `None` when identity is unverified, or raises a typed
`DecisionsUnavailable`.
- Transport limits (1.2.2, 1.2.7), the size ceiling and truncation guard
  (1.2.3), the shared cooldown and its service identity (1.2.4, 1.2.8),
  backend identity (1.2.9, 1.2.10), strict parsing (1.2.1), and log redaction (1.2.5)
  are all properties of that one call path, in one module and one test file.
- No subset is independently closeable. A service without strict parsing
  hands consumers unvalidated answers, one without the ceiling lets Kev
  truncate silently, and one without the cooldown stalls every consumer on a
  dead server.
- The pinned contract record (1.2.6) fixes the field names the parser and
  its fixtures use, so it comes first inside the same leaf.

**Research context:** #22604's 6.3 specified a client that this module
replaces. It used `ChoiceQuestion(criteria: dict[str, str])`,
`ChoiceAnswer(choice, probabilities, confidence)`, `DecisionsUnavailable`, and
`choose(state, questions)`, posting `{state, model, questions}`. Answers matched
by question key, and a missing key was a hard failure. Keep those shapes,
with one addition: the TypeSafe API (`https://docs.typesafe.ai/api.md`) and
Kev's README both require `instructions` on every question, so
`ChoiceQuestion` gains it.

`get_decision_service(config)` fingerprints every service-affecting field
(Decision 9): `api_base`, `model`, a sha256 of the resolved `api_key`,
`allow_remote`, `identity_contract`, `timeout_seconds`, `max_input_tokens`,
`backend_max_state_tokens`, and `failure_cooldown_seconds`. Consumer fields
are excluded, so a policy-only reload keeps the cached service.

The documented API is the contract, and the wire shape is pinned from it
before any code, with no live server:
- `docs/evidence/decisions/systemone-wire.md` records the TypeSafe request and
  response schema as fetched on 2026-09-29, for Choice and Noul.
- It also records Kev commit `0fe8fc97c2bc` (2026-09-29) as the reference
  local backend: `MODEL_NAMES`, `SERVE_MAX_STATE`, `SERVE_MAX_BRANCH`, the
  silent truncation in `Server.submit`, the `usage` block in `Server._body`,
  and the `/v1/models` card fields Decision 10 hashes.
- `tests/ai/fixtures/systemone_choice_response.json` is authored from that
  documented schema, with `usage.input_tokens`.
- Where 6.3's field names differ from the documented schema, the schema wins,
  and the evidence file records the difference.

Schema fixtures prove conformance to the documented contract only. They make
no claim that a given server is compatible at runtime. That is established by
the Activation Gate's live capture (item 4), which is also #22604's wire spike
and measures the characters-per-token ratio. The estimate stays advisory
(Decision 8), and the truncation guard reads every response's own
`usage.input_tokens`.

Module contents:
- `ChoiceQuestion(instructions: str, criteria: Mapping[str, str])`,
  serialized as `{"type": "choice", "instructions": ..., "criteria": ...}`.
  `instructions` is required, with no default; each consumer writes its own.
- `ChoiceAnswer(choice, probabilities, confidence)`.
- `DecisionsUnavailable(reason: Literal["unconfigured", "cooldown",
  "oversize", "truncated", "timeout", "transport", "http_status", "parse"],
  detail: str)`. `truncated` is a local outcome under Decision 9 and never
  opens the cooldown.
- Every failure leaves the service as `DecisionsUnavailable`, and no `httpx`
  exception escapes. After retries, an `httpx.TransportError` maps to
  `transport`. Expiry of the service's own `asyncio.timeout` maps to
  `timeout`. A caller's `asyncio.CancelledError` is never caught and
  propagates unchanged.
- The service holds only transport fields and cooldown state. It reads no
  consumer mode or threshold.
- `DecisionResult[A](answers: dict[str, A], backend_identity: str | None,
  response_model: str)`.
- `DecisionService`, holding the config and a cooldown deadline. Each call
  opens its own `httpx.AsyncClient`. Its method is `async choose(consumer: str, state:
  Mapping[str, Any], questions: Mapping[str, ChoiceQuestion], *,
  timeout_seconds: float | None = None) -> DecisionResult[ChoiceAnswer]`. The
  keyword is the caller budget from Decision 9.
- Backend identity, per Decision 10:
  - Each call runs `GET {api_base}/v1/models` concurrently with the decision
    request, inside the same `asyncio.timeout(budget)`. There is no separate
    fetch at service creation, so `get_decision_service` stays synchronous.
  - The identity is `sha256:` plus the sha256 of the canonical JSON of the
    card entry for the configured model: run path, base model, LoRA config,
    dtype, temperature, and the configured `backend_max_state_tokens`.
    Runtime statistics are excluded.
  - A card fetch that fails, times out, or lacks any of those fields gives
    `backend_identity=None`. It never fails the decision and never opens the
    cooldown. If the decision request itself fails, the card task is
    cancelled and awaited.
  - The card fetch runs only under `identity_contract: model_card`. Under
    `response_version`, the identity is `response:` plus the parsed
    `response_model`, and no card is fetched. With the contract unset, the
    identity is `None`.
- `estimate_tokens(body) -> int`, computed as `len(json.dumps(body)) // 4`.
- `get_decision_service(config: DecisionsConfig) -> DecisionService`, which
  returns the one cached service while the Decision 9 fingerprint is
  unchanged, and replaces it when the fingerprint changes.

Transport, per Decisions 8 and 9:
- the oversize check runs before sending;
- the client is `httpx.AsyncClient(trust_env=False,
  follow_redirects=False)`, so proxy environment variables and redirects can
  never carry a request to a host other than the configured `api_base`. This
  follows the local-daemon
  hardening in `utils/daemon_client.py:396-466`. There is no config knob to
  change it;
- `Authorization: Bearer` is sent only when `api_key` is set;
- every 3xx and every 4xx except 429 raise `http_status` without retry;
- transport errors and 429, 529 and 5xx retry through `retry_async` with the
  caller predicate and `delay=0.1`: one attempt plus at most two retries, and
  `asyncio.timeout(budget)` around the whole call;
- responses are parsed strictly. Any violation raises `parse` before any
  consumer policy sees it:
  - the response `model` must be a non-empty string. It is returned as
    `response_model` and never compared with the request, because the
    documented API answers `jev-latest` with a resolved version such as
    `jev-1.13.0`. Backend identity is Decision 10's job;
  - answer keys must match the question keys exactly;
  - each answer's `type` must equal its question's type;
  - probability keys must match the option keys;
  - every probability, confidence, and `noul` value must be finite and in
    [0,1];
  - Choice probabilities must sum to 1 within `1e-3`;
  - `choice` must be an option whose probability is within `1e-6` of the
    maximum, so any tied maximum is accepted as returned;
  - `usage.input_tokens` is required: an `int` that is not a `bool` and is
    at least 0. A missing or malformed value raises `truncated` under
    Decision 8, before any policy sees the answers;
  - the truncation guard is sound only for a backend whose
    `usage.input_tokens` counts every encoded state token and whose actual
    state limit equals `backend_max_state_tokens`. Kev at the pinned commit
    satisfies both. The Activation Gate's live capture verifies both for the
    configured server, and an unverified or mismatched server keeps every
    consumer in `shadow`;
- the Decision 9 cooldown rules apply exactly: remote failures open it, and
  local outcomes and cancellation never do.

Logging: one `decisions.call` event per call, with `consumer`, `model`,
`question_count`, `schema_hash` (sha256 of the sorted question keys and option
keys), `estimated_tokens`, `latency_ms`, and `outcome`.
- A successful call logs at DEBUG.
- Entering the cooldown logs at WARNING, once.
- State and option text are never logged.

Verification planned: `DATABASE_URL=postgresql://gobby_test:gobby_test@127.0.0.1:60892/gobby_test GOBBY_TEST_PROTECT=1 uv run pytest tests/ai/test_decisions_service.py -q`
(fake `httpx` transport, no live server); `uv run ruff check src/ && uv run mypy src/`.

**Acceptance:**

- 1.2.1 - `choose` posts `{model, state, questions}` to
  `{api_base}/v1/systemone`, parses the documented-schema fixture, and raises `parse`
  for these responses: an empty or non-string model, a missing or extra answer key, a
  wrong answer type, a choice outside the offered options or below the
  maximum probability, mismatched probability keys, probabilities summing
  outside `1 ± 1e-3`, and any non-finite or out-of-range value. A tied maximum
  is accepted, and a resolved model name such as `jev-1.13.0` for a
  `jev-latest` request is accepted and returned as `response_model`. The
  request carries `type` and `instructions` on every question. test:
  `tests/ai/test_decisions_service.py::test_choose_posts_and_parses_documented_wire`.
- 1.2.2 - With an ample budget, call counts are exact per status: 3xx, 401,
  403, 404, and 422 make one call each; 429, 529, 500, and transport errors
  make three. With a budget shorter than the backoff sequence, the call ends
  at the budget with fewer attempts and raises `timeout`. `retry_async`
  without a predicate keeps its current behavior. test:
  `tests/ai/test_decisions_service.py::test_retry_only_on_transient_status`.
- 1.2.7 - The client ignores `HTTP_PROXY`/`HTTPS_PROXY` and never follows a
  redirect. test:
  `tests/ai/test_decisions_service.py::test_no_proxy_or_redirect_hop`.
- 1.2.8 - A rotated secret, a changed timeout, ceiling, `allow_remote`,
  `identity_contract` (including `None` to `response_version` or
  `model_card` on an otherwise identical config), or
  `backend_max_state_tokens` yields a new service, while identical config
  shares one cooldown. test:
  `tests/ai/test_decisions_service.py::test_service_identity_fingerprint`.
- 1.2.9 - `backend_identity` is established on every call: two consecutive
  calls on one cached service against a server whose card changes between
  them (a restart onto another checkpoint) return different identities. A
  changed run path, dtype, temperature, or configured
  `backend_max_state_tokens` changes it under the same alias; a changed
  statistic does not. A failed, slow, or incomplete card yields `None`
  without failing the decision or opening the cooldown, and a failed decision
  leaves no pending card task. test:
  `tests/ai/test_decisions_service.py::test_backend_identity_from_model_card`.
- 1.2.10 - Under `response_version`, a `jev-latest` request answered as
  `jev-1.13.0` yields identity `response:jev-1.13.0` with no card request, a
  later answer as `jev-1.14.0` yields a different identity, and with the
  contract unset the identity is `None`. test:
  `tests/ai/test_decisions_service.py::test_backend_identity_from_response_version`.
- 1.2.3 - A request over `max_input_tokens` raises `oversize` without sending,
  for path-heavy state and for low characters-per-token state. A response
  with `usage.input_tokens` at `backend_max_state_tokens - 1` or above, or
  with no `usage.input_tokens`, raises `truncated` without opening the
  cooldown; one token below passes. test:
  `tests/ai/test_decisions_service.py::test_oversize_request_never_dials`.
- 1.2.4 - Table-driven: transport error, exhausted 529, 401, `parse`, and a
  full-budget `timeout` each open the cooldown; `unconfigured`, `oversize`, a
  caller-budget `timeout`, and caller cancellation do not, and cancellation
  propagates. Inside the cooldown, calls raise `cooldown` without dialing, and
  the first call after it dials again. test:
  `tests/ai/test_decisions_service.py::test_cooldown_fails_fast_then_recovers`.
- 1.2.5 - Log records carry no state or option text. test:
  `tests/ai/test_decisions_service.py::test_call_log_redacts_state`.
- 1.2.6 - The pinned contract record holds the documented TypeSafe Choice and
  Noul schemas and the Kev `0fe8fc97c2bc` reference facts: model names, state
  and branch limits, truncation, `usage`, and the `/v1/models` card fields.
  behavior:
  "/v1/systemone" in `docs/evidence/decisions/systemone-wire.md`.

## P2: Evaluation
`kind: framing`

**Goal:** Every consumer is promoted on measured quality, calibration, order
sensitivity, and latency against the backend it will use.

### 2.1 Shadow capture and the reproducible evaluation harness [category: code] (depends: 1.2)
`kind: deliverable`

Targets:
- `src/gobby/ai/decisions_shadow.py`
- `scripts/decisions_eval.py`
- `tests/ai/test_decisions_shadow.py`
- `tests/scripts/test_decisions_eval.py`

**Research context:** #22837 removed the relevance judge's replay and
calibration code, so no harness remains to reuse. `scripts/` already holds
standalone Python tools (`schema_diff.py`, `flatten_schema.py`). `jev.md`'s
"Evaluation and promotion" section lists the metrics. Kev's calibration is a
single fitted temperature, and its answers are order-sensitive (research doc
§2.3), so order permutation is mandatory.

`decisions_shadow.record(consumer, record)` appends one JSON line to
`~/.gobby/decisions/shadow/<consumer>.jsonl`.
- The directory is created `0700` and the file `0600`.
- Every record carries `id` (uuid4), `ts`, `consumer`, `model`,
  `content_hash` (sha256 of the canonical JSON of `state` and the questions or
  propositions), `state`, the questions or propositions, `classifier` as
  `{status: "ok" | "unavailable", reason, answers}`, `incumbent` as
  `{status: "ok" | "unavailable", verdict}`, `backend_identity`,
  `response_model`, `endpoint_host`,
  `latency_ms`, and `estimated_tokens`.
- `tool_rerank` records add `k` (the requested `top_k`), `candidates` (the
  fetched `server/tool` ids in semantic order), classifier probabilities keyed
  by candidate id, and the incumbent verdict as the ordered id list the LLM
  rerank returned. When the LLM rerank failed, the verdict is the semantic
  order the caller actually received, with incumbent status `unavailable`.
- `found_work` records add the classifier probability and the incumbent
  verdict as `true`, `false`, or `null`. `null` means the caller alerted on
  the fast-path verdict.
- 3.1 and 3.2 write these fields, and their shadow tests assert them.
- The file is capped at 10,000 lines. On overflow it is rewritten with the
  newest 5,000 lines through a temp file and a rename.
- Writes are best effort: a failure logs once at WARNING and never raises into
  the consumer.

The file is machine-local and never committed.

Labeling:
- The promotion cohort is a uniform random sample of the consumer's shadow
  records, drawn by ascending `sha256(id)`. Enriched sampling would skew every
  rate toward disagreements, so the cohort takes no disagreement weighting and
  stands for the live distribution.
- Gold fields, added by the labeler to a curated copy:
  - `tool_rerank`: `gold_relevant`, a list of candidate ids that materially
    apply to the request. An empty list means reject-all is correct.
  - `found_work`: `gold_shirk`, a boolean.
- Plan expansion creates no labeling task, because no data exists before
  deployment.
- After deployment, once a consumer's shadow file reaches 200 records, the
  PD files one labeling request per consumer per activation as a lane task.
  The PD records the task ref with that consumer's promotion evidence.
- The consumer stays in `shadow` until the labeling task and its eval report
  are complete, and `enforce` stays gated on that report.
- The lane labels the cohort.
- Separately, the Assistant presents 20 disagreements per consumer to Josh as
  a spot audit before any promotion. Audit records never enter the metrics.

`scripts/decisions_eval.py --consumer <name> --dataset <jsonl> --out <md>`
runs against `ai.decisions` loaded from the daemon config:
1. Split by `int(content_hash, 16) % 5 == 0` into a holdout, with the rest
   as the development set. Records with identical content share a hash, so a
   repeated example never lands in both splits.
2. Replay every record with its original order and with options, or candidate
   order in the state, reversed.
   - Every available replayed answer must carry the same non-null
     `backend_identity` and the same `response_model`. Answers that span
     identities or response models, or any available answer with no
     identity, make the verdict `FAIL` with the reason `mixed or unverified
     backend`.
   - The report records that one identity and response model as the run's
     evaluated backend, the value an operator copies into
     `evaluated_backend`.
   - Shadow provenance in the dataset may name other backends. Only the
     replayed predictions decide the gate.
3. Compute accuracy, Brier score, ECE (10 equal-width bins on confidence for
   Choice, or on probability for Noul), and selective accuracy and coverage at
   thresholds from 0.50 to 0.95 in steps of 0.05.
4. Compute the order-flip rate and p50 and p95 latency. Also compute cost:
   estimated input tokens per call, and a hosted-equivalent cost at the
   pinned OpenRouter Jev price ($0.042 per million input tokens, `jev.md`).
   The local backend's per-call cost is zero, so this gives one
   local-versus-hosted number per consumer.
5. Compute every Decision 12 metric for the consumer:
   - tool rerank, with the classifier's list being candidates at or above
     `min_probability`, ordered by probability:
     - the deployed list is the classifier's list when the classifier is
       available, and the incumbent's recorded order otherwise, the
       all-or-fallback policy 3.1 ships;
     - Recall@k = `|top_k(list) ∩ gold_relevant| / |gold_relevant|`, averaged
       over records with non-empty gold. The gate compares the deployed list
       with the incumbent's recorded order over the same records;
     - a paired complete-case comparison over classifier-available records,
       and the unavailability rate, are reported beside the gate and do not
       decide it;
     - reject-all accuracy = the share of empty-gold records where the
       deployed list is empty.
   - found-work, simulating the cascade at the candidate thresholds:
     - probability at or above `accept_above` is a confirm, at or below
       `accept_below` a clear, anything else or `unavailable` an escalation;
     - an escalation takes the incumbent verdict, and a `null` verdict counts
       as an alert;
     - false-clear rate = final clears with `gold_shirk` true, over records
       with `gold_shirk` true, for the cascade and for the incumbent alone;
     - false-alert rate = final alerts with `gold_shirk` false, over records
       with `gold_shirk` false;
     - escalation rate = escalations over all records.
   Calibration (step 3) for Noul consumers treats each probability as one
   prediction. Tool rerank labels each candidate `1` when it is in
   `gold_relevant`. Found-work labels each record by `gold_shirk`. Brier, ECE,
   and accuracy at the consumer threshold are micro-averaged over all
   predictions in the split.

   Thresholds are selected on the development split only, and the frozen
   holdout is evaluated once:
   - `min_probability` is the grid value, 0.05 to 0.95 in steps of 0.05, with
     the highest reject-all accuracy among values whose deployed Recall@k is
     at least the incumbent's; the lowest such value wins ties;
   - `(accept_below, accept_above)` is the grid pair with
     `accept_below < accept_above` and the lowest escalation rate among pairs
     whose cascade false-clear rate is at most the incumbent's; the widest
     band wins ties.
   - When no value or pair qualifies, the verdict is `FAIL`.

   Support: each split needs at least 20 records per gold class (non-empty and
   empty `gold_relevant`; `gold_shirk` true and false). A metric with a zero
   denominator or short support is undefined, and any undefined gate metric
   makes the verdict `FAIL` with the reason `insufficient support`. The PD
   then requests a further random cohort.

   Gate constants (Decision 12), each measured on the holdout:
   - ECE at most 0.10;
   - order-flip rate at most 10%;
   - p95 latency at most 1 s against the configured backend, local or
     remote;
   - at least 20 records per gold class in each split;
   - `tool_rerank`: deployed Recall@k at least the incumbent's;
   - `found_work`: cascade false-clear rate at most the incumbent's, and
     escalation rate at most 50%.

   The verdict is `PASS` only when every constant holds. Any breached or
   undefined constant makes it `FAIL`, naming that constant.

   Community labels are outside the harness. Their promotion evidence is
   #22604's Q1.6 report, which that task owns, cited by path in the consumer's
   promotion evidence.
6. Write a Markdown report. It names the configured model, the reported
   `response_model` values, the endpoint host, the backend identity, the
   dataset hash, and both splits, and ends in an explicit gate `PASS` or `FAIL` listing each measured
   value against its threshold.

A labeled set holds at least 200 records per consumer. Reports live under
`docs/evidence/decisions/<consumer>-<date>.md`.

Verification planned: `DATABASE_URL=postgresql://gobby_test:gobby_test@127.0.0.1:60892/gobby_test GOBBY_TEST_PROTECT=1 uv run pytest tests/ai/test_decisions_shadow.py tests/scripts/test_decisions_eval.py -q`
(fake service, synthetic dataset); `uv run ruff check src/ scripts/ && uv run mypy src/`.

**Acceptance:**

- 2.1.1 - Shadow records are written `0600` under a `0700` directory, capped
  and rotated, and a write failure never raises. test:
  `tests/ai/test_decisions_shadow.py::test_shadow_record_permissions_cap_and_failure`.
- 2.1.2 - The harness splits deterministically by id and computes accuracy,
  Brier, ECE, selective accuracy, and order-flip rate on a synthetic set with
  known answers. test:
  `tests/scripts/test_decisions_eval.py::test_metrics_on_known_dataset`.
- 2.1.3 - The report names the configured model, the one evaluated backend
  identity and response model, the dataset hash, both splits, and the
  consumer bar. test:
  `tests/scripts/test_decisions_eval.py::test_report_identifies_run`.
- 2.1.4 - Synthetic passing and failing datasets for `tool_rerank` and
  `found_work` produce the expected metric values and gate verdicts. `PASS`
  requires ECE at most 0.10, order-flip rate at most 10%, p95 latency at most
  1 s against the configured backend, at least 20 records per gold class per
  split, and the consumer inequality (deployed Recall@k at least the
  incumbent's; cascade false-clear rate at most the incumbent's with
  escalation at most 50%). Each failing set breaches one constant and yields
  `FAIL` naming it. Thresholds come from the development split only. test:
  `tests/scripts/test_decisions_eval.py::test_gate_verdicts_on_synthetic_sets`.
- 2.1.5 - Known-answer edge cases: an incumbent `null` verdict counts as an
  alert, an unavailable classifier escalates, a cohort where the classifier
  beats the incumbent on available records but the deployed list trails it
  overall yields `FAIL`, the found-work pair and `min_probability` selections
  match hand-computed values, a record whose `k` exceeds its
  returned list length still scores Recall@k against `k`, short support or
  a zero denominator yields `FAIL` with `insufficient support`, and a replay
  whose backend identity or response model switches midway, or whose answers
  carry no identity, yields `FAIL` with `mixed or unverified backend`. test:
  `tests/scripts/test_decisions_eval.py::test_metric_edge_cases_fail_closed`.
- 2.1.6 - On a synthetic skewed mix with a 5% disagreement rate, the gate
  metrics equal the cohort's actual rates; audit records are excluded, and
  duplicate content lands in one split. test:
  `tests/scripts/test_decisions_eval.py::test_gate_uses_representative_cohort`.

## P3: Consumers
`kind: framing`

**Goal:** Tool reranking and found-work confirmation consume the service in
shadow mode, and each can be promoted by config once its gate passes.

### 3.1 MCP tool reranking through Noul [category: code] (depends: 2.1)
`kind: deliverable`

Targets:
- `src/gobby/ai/decisions.py`
- `src/gobby/mcp_proxy/services/recommendation.py::*` — scope-reason: add the decision-rerank branch in `_recommend_hybrid` and a `decisions_resolver` constructor argument
- `src/gobby/mcp_proxy/server.py::*` — scope-reason: pass `decisions_resolver` to `RecommendationService`
- `tests/ai/test_decisions_service.py`
- `tests/ai/fixtures/systemone_noul_response.json`
- `docs/evidence/decisions/systemone-wire.md`
- `tests/mcp_proxy/services/test_recommendation_decisions.py`

**Research context:** `_recommend_hybrid`
(`mcp_proxy/services/recommendation.py:146-212`) takes semantic
`top_k * 2` candidates and asks the LLM to rerank them. On any exception it
returns semantic order with `search_mode="hybrid_fallback"`. Choice alone
cannot reject every candidate, which is why the tool consumer uses Noul
(`jev.md` use case 2). Noul lands here with the consumer that needs it
(Decision 7).

Add `async noul(consumer, state, propositions: Mapping[str, str], *,
timeout_seconds: float | None = None) -> DecisionResult[NoulAnswer]`, with
`NoulAnswer(probability: float)`, to `DecisionService`. Each proposition is serialized as `{"type":
"noul", "instructions": <proposition>}`, and `probability` is read from the
answer's `noul` field under the same strict parsing. It uses the same
transport, ceiling, and cooldown. Its wire field names come from the Noul
schema that 1.2 pinned in `docs/evidence/decisions/systemone-wire.md`:
- Before any code in this leaf, author
  `tests/ai/fixtures/systemone_noul_response.json` from that schema, with
  three propositions. The `noul` parser tests read this fixture.
- Append to the evidence file a note naming the fixture and any field the
  consumer relies on. Runtime compatibility waits for the Activation Gate's
  live capture.

Consumer: state is `{"request": task_description}`, with one proposition per
candidate, "Tool `<server>/<tool>` (`<description>`) materially applies to the
request."

One logical rerank has one budget, `ai.decisions.timeout_seconds`, and one
complete result or none:
- Batching packs candidates in semantic order, greedily, into requests that
  each fit the ceiling.
- If the state plus one candidate alone exceeds the ceiling, the whole
  classifier result is `oversize`. Nothing is truncated.
- Batches run concurrently under one `asyncio.TaskGroup` and one deadline. If
  any batch fails, the rest are cancelled and awaited, and the whole
  classifier result is unavailable. A partial set never becomes a ranking or a
  reject-all.
- Probabilities combine only from one model: every batch must return the
  same non-null `backend_identity` and the same `response_model`. Differing
  identities or response models, or a known identity beside an unknown one,
  make the whole classifier result unavailable. Enforce then falls back to
  the LLM rerank, and shadow
  records the classifier as unavailable. This is a consumer outcome and never
  opens the service cooldown. When every batch returns no identity, Decision
  10 applies and enforce runs as shadow.
- Ranking sorts by descending probability, with ties broken by semantic
  order, and returns at most `top_k`, the same slice today's successful LLM
  rerank takes.

By `tool_rerank.mode`:
- `off`: today's path, unchanged.
- `shadow`: today's path decides and returns its result or its fallback,
  unchanged. The classifier runs concurrently with it under `asyncio.gather`
  within the rerank budget, and the request waits for both, so no task
  outlives the request. A wrapper converts every classifier `Exception` into
  an unavailable result. A shadow record pairs its probabilities with the LLM
  rerank order.
- `enforce`, with a matching `evaluated_model` and `evaluated_backend`
  (Decision 10): candidates at or above
  `min_probability` come back in probability order (`search_mode="decide"`),
  and an empty result is a valid reject-all. On `DecisionsUnavailable`, the
  consumer runs the existing LLM rerank over the semantic candidates it
  already fetched. It returns semantic order with
  `search_mode="hybrid_fallback"` only if that incumbent also fails.

`decisions_resolver` returns the daemon's `DecisionsConfig`, or `None` when
the config is unavailable. `None` means `off`.

Scope: MCP tools only. Skill search has no reranker today, so it is not a
consumer here.

Verification planned: `DATABASE_URL=postgresql://gobby_test:gobby_test@127.0.0.1:60892/gobby_test GOBBY_TEST_PROTECT=1 uv run pytest tests/ai/test_decisions_service.py tests/mcp_proxy/services -q`;
`uv run ruff check src/ && uv run mypy src/`.

**Acceptance:**

- 3.1.1 - `noul` posts per-proposition questions, parses the
  documented-schema Noul fixture, and returns probabilities by key under the
  same ceiling and cooldown. The fixture note is appended to
  `docs/evidence/decisions/systemone-wire.md`. test:
  `tests/ai/test_decisions_service.py::test_noul_returns_probabilities_by_key`.
- 3.1.2 - Shadow mode returns today's result unchanged and writes one shadow
  record. test:
  `tests/mcp_proxy/services/test_recommendation_decisions.py::test_shadow_keeps_llm_rerank`.
- 3.1.3 - Enforce mode ranks by probability, drops candidates below
  `min_probability`, and can return none. An unavailable classifier invokes
  the LLM rerank; semantic order is returned only when both the classifier and
  the LLM fail. test:
  `tests/mcp_proxy/services/test_recommendation_decisions.py::test_enforce_ranks_rejects_and_falls_back`.
- 3.1.4 - Enforce mode with a mismatched `evaluated_model`, a mismatched
  `evaluated_backend`, or no backend identity behaves as shadow. A `mode` or
  `min_probability` change reaches the next call through the same cached
  service. test:
  `tests/mcp_proxy/services/test_recommendation_decisions.py::test_model_mismatch_downgrades_to_shadow`.
- 3.1.5 - One rerank is bounded and complete:
  - more than `top_k` passing candidates return exactly `top_k`;
  - equal probabilities keep semantic order;
  - a failed second batch makes the whole result unavailable, so enforce falls
    back to the LLM rerank;
  - two batches with different backend identities, with different response
    models, or with one known and one unknown identity make the whole result
    unavailable, so enforce falls back to the LLM rerank
    and shadow records the classifier as unavailable;
  - an oversized singleton yields `oversize` without truncation;
  - a shadow classifier exception leaves the incumbent's result and failure
    semantics intact, with no task pending after return.

  test:
  `tests/mcp_proxy/services/test_recommendation_decisions.py::test_rerank_batches_are_bounded_and_complete`.

### 3.2 Found-work confirmation cascade [category: code] (depends: 3.1)
`kind: deliverable`

Targets:
- `src/gobby/workflows/found_work_confirm.py`
- `src/gobby/workflows/found_work_gate.py::*` — scope-reason: move `_confirm_shirk` and its prompt and schema constants into `found_work_confirm.py` and delegate
- `tests/workflows/test_found_work_confirm.py`

**Granularity:** The decomposition and the cascade share one leaf. At 995
lines, `found_work_gate.py` cannot take the cascade in place, and a
decomposition-only leaf would change nothing a test can observe.

**Research context:** `FoundWorkStopAnalyzer._confirm_shirk`
(`workflows/found_work_gate.py:531-568`) returns `True` (confirm), `False`
(clear), or `None` (unavailable), and on `None` the caller alerts on the
fast-path verdict. It builds the prompt from `USER INSTRUCTION` and
`FINAL ASSISTANT MESSAGE` and calls `call_json_feature` with
`_SHIRK_SYSTEM_PROMPT`, `_SHIRK_CONFIRM_SCHEMA`, and a cap of
`min(close_review_total_timeout_seconds, 8.0)`. The analyzer has a
`config_resolver` (`workflows/hooks.py:133`), which supplies
`config.ai.decisions`. The cascade is Decision 11 applied to Decision 13's
accept-or-escalate design.

Move `_confirm_shirk`, `_SHIRK_SYSTEM_PROMPT`, and `_SHIRK_CONFIRM_SCHEMA`
into `found_work_confirm.py` as `async confirm_shirk(message, user_prompt, *,
llm_service, daemon_config) -> bool | None`. The analyzer method becomes a
one-line delegate, and `found_work_gate.py` shrinks.

The Noul proposition: "The final assistant message defers, declines, or hands
back work the user instruction asked for, without doing it." Its state holds
both texts.

By `found_work.mode`:
- `off`: today's LLM path.
- `shadow`: today's LLM path decides. The classifier runs alongside it under
  `asyncio.gather` within the same budget, and a shadow record pairs the
  probability with the LLM verdict.
- `enforce`, with a matching `evaluated_model` and `evaluated_backend`
  (Decision 10):
  - a probability at or above `accept_above` returns `True`;
  - a probability at or below `accept_below` returns `False`;
  - anything between escalates to today's LLM path;
  - `DecisionsUnavailable` also escalates to today's LLM path, whose own
    `None` keeps the fast-path alert.

Admission is today's, in every mode. With no LLM service, no daemon
config, or `validation.enabled` false, `confirm_shirk` returns `None` before
any classifier call. The cascade extends the incumbent path and never runs
where the incumbent is not admitted.

Budget: `cap = min(validation.close_review_total_timeout_seconds, 8.0)`, the
incumbent's current cap. `confirm_shirk` fixes one deadline,
`loop.time() + cap`, at entry, and every call gets only the time left:
- A wrapper converts every classifier `Exception`, typed or unexpected, into
  an unavailable result. So a classifier failure never cancels the incumbent
  in `shadow`.
- In `shadow`, both calls get `remaining` as their timeout. A classifier task
  still pending at the deadline is cancelled and awaited before return.
- In `enforce`, the classifier's timeout is `remaining`. The escalated LLM
  call gets `remaining = max(0, deadline - loop.time())`.
- When `remaining` is 0, confirmation returns `None`, so the caller keeps the
  fast-path alert and no LLM call is made with an invalid timeout.
- Caller cancellation propagates. `asyncio.CancelledError` is never
  converted, and child tasks are cancelled and awaited before it re-raises.
- `enforce` with a mismatched `evaluated_model` behaves as `shadow`.

So a classifier outage never clears a finding, and only a confident
classifier verdict skips the LLM.

Verification planned: `DATABASE_URL=postgresql://gobby_test:gobby_test@127.0.0.1:60892/gobby_test GOBBY_TEST_PROTECT=1 uv run pytest tests/workflows/test_found_work_confirm.py tests/workflows -k found_work -q`;
`uv run ruff check src/ && uv run mypy src/`.

**Acceptance:**

- 3.2.1 - `found_work_gate.py` delegates confirmation to
  `found_work_confirm.confirm_shirk`, ends below its starting line count, and
  keeps its existing found-work tests passing. symbol:
  `src/gobby/workflows/found_work_confirm.py::confirm_shirk`.
- 3.2.2 - In enforce mode a probability at or above `accept_above` confirms
  and one at or below `accept_below` clears, both without an LLM call; the
  band between escalates to the LLM. test:
  `tests/workflows/test_found_work_confirm.py::test_cascade_accepts_confident_and_escalates_uncertain`.
- 3.2.3 - An unavailable classifier escalates to the LLM path and never
  returns `False` by itself. The escalated call's timeout is the configured
  cap minus the classifier's elapsed time, and an exhausted budget returns
  `None`. test:
  `tests/workflows/test_found_work_confirm.py::test_outage_never_clears_a_finding`.
- 3.2.4 - Shadow mode returns the LLM verdict and writes one shadow record.
  An unexpected classifier exception leaves the LLM verdict intact. test:
  `tests/workflows/test_found_work_confirm.py::test_shadow_returns_llm_verdict`.
- 3.2.5 - Budget and admission:
  - a configured 1 s cap bounds the whole confirmation in shadow and enforce;
  - `off` makes no classifier call and returns today's result;
  - `validation.enabled` false or a missing service returns `None` with no
    classifier call;
  - caller cancellation propagates and leaves no pending task;
  - a mismatched `evaluated_model` or `evaluated_backend` behaves as shadow;
  - a `mode` or threshold change reaches the next call through the same
    cached service.

  test:
  `tests/workflows/test_found_work_confirm.py::test_budget_admission_and_cancellation`.

## P4: Documentation
`kind: framing`

**Goal:** The guides describe the capability, its config, and each consumer's
modes.

### 4.1 Decision capability guide rows [category: docs] (depends: 3.2)
`kind: deliverable`

Targets:
- `docs/guides/llm-features.md`
- `docs/guides/configuration.md`

**Research context:** `docs/guides/llm-features.md` (191 lines) lists
features by config path. The research doc (§6) records it as already missing
rows. `docs/guides/configuration.md` documents config sections and the
`/api/config` routes.

Edits:
- Add an `ai.decisions` section to `configuration.md`. It covers the fields,
  the loopback default, the `allow_remote` opt-in, and the rulings behind
  them, the `/v1/systemone` contract,
  and `GET /api/llm/status`.
- Add rows to `llm-features.md` for `ai.decisions.tool_rerank` and
  `ai.decisions.found_work`, with their modes and fallbacks.
- Add a paragraph on shadow records, the 2.1 evaluation script, and the
  promotion gate.

**Acceptance:**

- 4.1.1 - The configuration guide documents `ai.decisions` with the loopback
  default, the `allow_remote` opt-in, and the wire contract. behavior:
  "allow_remote" in
  `docs/guides/configuration.md`.
- 4.1.2 - The features guide lists both consumers with their modes and
  fallbacks. behavior: "found_work" in `docs/guides/llm-features.md`.

## V1: Verification
`kind: verification`

Each leaf runs its own `Verification planned` command after its final
edit, because the combined command names test files that later leaves create.
Run the combined command below after the last leaf (4.1) and again before the
PD lands the branch:

```bash
DATABASE_URL=postgresql://gobby_test:gobby_test@127.0.0.1:60892/gobby_test GOBBY_TEST_PROTECT=1 uv run pytest tests/config/test_decisions_config.py tests/ai/test_capability_registry.py tests/ai/test_decisions_service.py tests/ai/test_decisions_shadow.py tests/scripts/test_decisions_eval.py tests/mcp_proxy/services tests/workflows/test_found_work_confirm.py -q
uv run ruff format --check src/ scripts/ && uv run ruff check src/ scripts/ && uv run mypy src/
uv run gobby plans validate /Users/josh/.gobby/worktrees/gobby/task-23024-classifier-path-plan/.gobby/plans/decision-classifier-path.md -p /Users/josh/Projects/gobby
```

Plan validation takes the absolute worktree plan path, with the project root
`/Users/josh/Projects/gobby`.

Live check after the PD-owned restart, on Josh's machine with a loopback
`/v1/systemone` server configured: `GET /api/llm/status` lists `decide` as
available with the configured model. With the server stopped, a
`recommend_tools` call in `shadow` mode returns its usual result. Do not run
the full pytest suite.

Plan changelog:
- 2026-09-29: First draft by Plan Writer gobby#14578.
- 2026-09-29: Enhancer pass (run 4aeff78d) and PD dispositions applied:
  - accepted E1 to E8 and E10: transport hardening, the status-aware retry
    predicate, the incumbent-first rerank fallback, the complete gate harness,
    strict response parsing, config defaults and bounds, the service
    fingerprint, found-work budget containment, and post-deployment labeling;
  - modified E9: V1 validates the absolute worktree plan path against the
    `/Users/josh/Projects/gobby` root.
