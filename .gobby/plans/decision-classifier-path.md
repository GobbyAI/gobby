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
2. **No hosted text, enforced in config.** Josh ruled on 2026-09-24 (relayed by
   #14069): no memory, session or repository text goes to a hosted decision
   provider. Pilots wait for a local Kev.
   - `DecisionsConfig` validates that `api_base`'s host is `localhost`, an
     address in `127.0.0.0/8`, or `::1`. Any other host fails config
     validation, and the error cites the ruling.
   - A remote server stays reachable through a loopback tunnel the user
     creates. That is the user's choice, outside Gobby.
   - Rejected: policy prose alone. Prose makes "provider-agnostic" and "no
     hosted" read as a contradiction and enforces neither.
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
7. **Primitives: Choice first, then Noul.** Josh ruled on 2026-09-24 that
   Noul arrives with the second consumer.
   - 1.2 ships `choose`.
   - 3.1 (tool rerank, the second consumer after #22604) adds `noul`.
   - Score has no consumer and is not built.
8. **Request ceiling.** Kev is trained on 384 tokens and serves up to 8,192.
   `ai.decisions.max_input_tokens` (default `8192`) caps the estimated size of
   one request, estimated as `len(json.dumps(body)) // 4`. No Python token
   estimator exists; gcore's `estimate_tokens` is Rust-only.
   - The service raises `DecisionsUnavailable(reason="oversize")` before
     sending.
   - Consumers batch to `service.max_input_tokens`.
   - The evaluation (2.1) measures quality at the state sizes each consumer
     actually sends, because every consumer runs far beyond Kev's training
     length.
9. **Failure and cooldown.**
   - 401, 403 and 422 raise without retry.
   - 429, 529 and 5xx retry through `retry_async`
     (`llm/claude_runtime.py:141`), at most twice and within the caller's
     timeout.
   - Any final transport failure opens a cooldown of
     `failure_cooldown_seconds` (default 60). During it, calls fail fast with
     `reason="cooldown"` and never dial. This keeps a dead server from charging
     the found-work hook its timeout on every Stop.
   - One service instance per `(api_base, model)` is cached by
     `get_decision_service(config)`, the same pattern as
     `EmbeddingService.from_config`, so the cooldown is shared.
10. **Per-consumer policy, typed.** `DecisionsConfig` carries one typed field
    per consumer rather than a free map:
    - `community_label`: `mode`, `min_confidence` (default `0.5`), and
      `evaluated_model`.
    - `tool_rerank`: `mode`, `min_probability`, and `evaluated_model`.
    - `found_work`: `mode`, `accept_below`, `accept_above`, and
      `evaluated_model`.

    `mode` is `off`, `shadow` or `enforce`, and defaults to `off`.

    How `enforce` behaves:
    - It acts only when `evaluated_model == ai.decisions.model`. Otherwise the
      consumer runs as `shadow` and logs `evaluated_model_mismatch` once per
      cooldown window.
    - A model change is therefore a new evaluator by construction (`jev.md`),
      and a changed model can never inherit thresholds.
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
    enters `enforce` only after its report is committed under
    `docs/evidence/decisions/`, and only when:
    - expected calibration error is at most 0.10;
    - the option-order or candidate-order flip rate is at most 10%;
    - p95 latency is at most 1 s against the local backend;
    - its consumer bar is met:
      - tool rerank: Recall@k no lower than the LLM rerank, with reject-all
        accuracy reported;
      - found-work: the false-clear rate is no higher than the incumbent's,
        the escalation rate is at most 50%, and false clears and false alerts
        are reported separately;
      - community labels: #22604's Q1.6 bars.

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
- sizes each batch to `service.max_input_tokens`. At Kev's 8,192 tokens and
  6.3's budget of about 500 tokens of state plus 250 per question per
  community, that is at most 10 communities per request instead of 20;
- records its wire spike against the local `/v1/systemone` server only. The
  OpenRouter alpha arm is dropped under Decisions 1 and 2, and 1.2's wire
  capture satisfies the spike;
- keeps every other 6.3 acceptance item unchanged.

#22604 keeps its `enhancement` parking (Josh, 2026-09-23). Its own admission
leads the consumer order, as the research doc's §5 ruling set.

## Activation Gate and #22075
`kind: framing`

The decision classifier counts as **deployed**, beyond merely planned, only
when every one of these holds:

1. Leaves 1.1 and 1.2 are landed on `0.5.0`, and the PD has restarted the
   daemon from the main checkout.
2. On Josh's machine, a `/v1/systemone` server is listening on loopback, and
   `ai.decisions.api_base` and `ai.decisions.model` name it. The server can be
   Kev from the post-0.6 installer option, or any compatible server Josh runs.
3. `GET /api/llm/status` reports `decide` available with that model.
4. At least one consumer is in `mode: enforce`. It has passed its Decision 12
   gate, and its report is committed under `docs/evidence/decisions/`.

The Assistant presents that state to Josh when it holds. Until then, #22075
(Detect provider usage outages across all providers and fall back to the next
candidate) stays blocked.

#22075 also remains `deferred-by-josh` under his 2026-09-10 instruction. It
becomes executable only when Josh lifts that deferral, even after the gate
holds. Closing #23024 does not unblock it: at expansion the PD moves #22075's
`blocked_by` edge from #23024 to the leaf that satisfies gate item 4.

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
- `src/gobby/config/ai.py::*` — scope-reason: add `DecisionsConfig`, the three typed consumer configs, the loopback validator, and `AIConfig.decisions`
- `src/gobby/config/code_index.py::CodeIndexCommunityLabelConfig`
- `src/gobby/ai/registry.py::AICapability`
- `src/gobby/ai/registry_builder.py::*` — scope-reason: add `_decision_binding` and register it in `build_daemon_ai_capability_registry`
- `docs/audits/configuration-audit.md`
- `crates/gcore/assets/config/runtime_config_contract.json::*` — scope-reason: derived config carrier; regenerated with `scripts/generate_runtime_config_contract.py --stdout`
- `web/src/api/runtimeConfigCodecVectors.gen.ts::*` — scope-reason: derived config carrier; regenerated with `scripts/generate_runtime_config_contract.py --stdout-web` and expected unchanged unless key encoding moves
- `tests/config/test_decisions_config.py`
- `tests/ai/test_capability_registry.py::*` — scope-reason: cover the `decide` binding's unavailable and available states

**Research context:** The precedent is `EmbeddingsConfig`
(`config/persistence.py:187-248`), which uses
`validate_optional_endpoint_url(value, field_name="api_base")` from
`config/url_validation.py`, and `_embedding_binding`
(`ai/registry_builder.py:126-162`). That binding returns
`CapabilityBinding.unavailable(capability, provider, adapter_style=..., reason=..., models=..., metadata=...)`
when unconfigured and `CapabilityBinding(...)` when available. Use
`AIAdapterStyle.OPENAI_COMPATIBLE` unless the enum gains a closer style; do not
add one for a single binding.

`DecisionsConfig` (`extra="forbid"`, like `AIConfig`) has these fields:

| Field | Type | Default |
| --- | --- | --- |
| `api_base` | `str \| None` | `None` |
| `api_key` | `str \| None` | `None` |
| `model` | `str \| None` | `None` |
| `timeout_seconds` | `float` | `2.0`, `gt=0` |
| `max_input_tokens` | `int` | `8192`, `ge=256` |
| `failure_cooldown_seconds` | `float` | `60`, `ge=0` |
| `community_label` | `ChoiceConsumerConfig` | |
| `tool_rerank` | `RerankConsumerConfig` | |
| `found_work` | `CascadeConsumerConfig` | |

The consumer configs:
- `ChoiceConsumerConfig`: `mode: Literal["off", "shadow", "enforce"] = "off"`,
  `min_confidence: float = 0.5`, `evaluated_model: str | None = None`.
- `RerankConsumerConfig`: `mode`, `min_probability: float = 0.5`,
  `evaluated_model`.
- `CascadeConsumerConfig`: `mode`, `accept_below: float = 0.1`,
  `accept_above: float = 0.9` (validated `accept_below < accept_above`),
  `evaluated_model`.

Validators:
- `api_base` passes `validate_optional_endpoint_url`, and its parsed host must
  be `localhost`, an `ipaddress` loopback address, or `::1`. Otherwise it
  raises `ValueError("ai.decisions.api_base must be a loopback address: hosted
  decision providers are disabled (Josh, 2026-09-24: no hosted text)")`.
- `model` is required when `api_base` is set.
- `mode == "enforce"` with `evaluated_model is None` is rejected, because
  enforcement needs a recorded evaluation.

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
- `tests/code_index/test_community_labeler.py` — no-edit-reason: it builds `CodeIndexCommunityLabelConfig(candidates=...)` only and reads none of the removed `decisions_*` keys.
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

Verification planned: `DATABASE_URL=postgresql://gobby_test:gobby_test@127.0.0.1:60892/gobby_test GOBBY_TEST_PROTECT=1 uv run pytest tests/config/test_decisions_config.py tests/config/test_runtime_config_contract.py tests/ai/test_capability_registry.py tests/config -q`;
`uv run ruff check src/ && uv run mypy src/`.

**Acceptance:**

- 1.1.1 - A non-loopback `api_base` fails validation with the no-hosted-text
  message; `localhost`, `127.0.0.1`, and `[::1]` pass. test:
  `tests/config/test_decisions_config.py::test_api_base_must_be_loopback`.
- 1.1.2 - `model` is required with `api_base`, `enforce` requires
  `evaluated_model`, and `accept_below < accept_above`. test:
  `tests/config/test_decisions_config.py::test_decisions_config_invariants`.
- 1.1.3 - The five `code_index.community_label.decisions_*` keys no longer
  exist on the model. symbol:
  `src/gobby/config/code_index.py::CodeIndexCommunityLabelConfig`. test:
  `tests/config/test_decisions_config.py::test_community_label_decision_keys_removed`.
- 1.1.4 - `decide` reports unavailable with its reason when unconfigured and
  available with the model when configured, and it appears in the registry
  status snapshot. test:
  `tests/ai/test_capability_registry.py::test_decide_binding_reports_configuration`.

### 1.2 `DecisionService` with Choice, request ceiling, and cooldown [category: code] (depends: 1.1)
`kind: deliverable`

Targets:
- `src/gobby/ai/decisions.py`
- `tests/ai/test_decisions_service.py`
- `tests/ai/fixtures/systemone_choice_response.json`
- `docs/evidence/decisions/systemone-wire.md`

**Research context:** #22604's 6.3 specified a client that this module
replaces. It used `ChoiceQuestion(criteria: dict[str, str])`,
`ChoiceAnswer(choice, probabilities, confidence)`, `DecisionsUnavailable`, and
`choose(state, questions)`, posting `{state, model, questions}`. Answers matched
by question key, and a missing key was a hard failure. Keep those shapes.

The wire shape is pinned by capture, before any code:
- Run one Choice request against a local `/v1/systemone` server (Kev, per
  Decision 3) with two questions.
- Record the redacted request and response in
  `docs/evidence/decisions/systemone-wire.md`, and save the response body as
  the test fixture.
- If the captured field names differ from 6.3's, the capture wins. The evidence
  file records the difference.

This capture is also #22604's wire spike (Coordination With #22604).

Module contents:
- `ChoiceQuestion`, `ChoiceAnswer`.
- `DecisionsUnavailable(reason: Literal["unconfigured", "cooldown",
  "oversize", "timeout", "http_status", "parse"], detail: str)`.
- `DecisionService`, holding the config, one `httpx.AsyncClient`, and a
  cooldown deadline. Its method is `async choose(consumer: str, state:
  Mapping[str, Any], questions: Mapping[str, ChoiceQuestion]) -> dict[str,
  ChoiceAnswer]`.
- `estimate_tokens(body) -> int`, computed as `len(json.dumps(body)) // 4`.
- `get_decision_service(config: DecisionsConfig) -> DecisionService`, a
  module-level cache keyed by `(api_base, model, api_key is not None)`, which
  is rebuilt when the key changes.

Transport, per Decisions 8 and 9:
- the oversize check runs before sending;
- `Authorization: Bearer` is sent only when `api_key` is set;
- 401, 403 and 422 raise `http_status` without retry;
- 429, 529 and 5xx retry through `retry_async` at most twice, inside
  `timeout_seconds` overall;
- a final failure sets the cooldown, and calls inside the cooldown raise
  `cooldown` without dialing.

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
  `{api_base}/v1/systemone`, parses the captured fixture, and fails when an
  answer key is missing. test:
  `tests/ai/test_decisions_service.py::test_choose_posts_and_parses_captured_wire`.
- 1.2.2 - 401 and 422 raise without retry, while 429 and 529 retry at most
  twice. test:
  `tests/ai/test_decisions_service.py::test_retry_only_on_transient_status`.
- 1.2.3 - A request over `max_input_tokens` raises `oversize` without sending.
  test:
  `tests/ai/test_decisions_service.py::test_oversize_request_never_dials`.
- 1.2.4 - After a final transport failure, calls inside the cooldown raise
  `cooldown` without dialing, and the first call after it dials again. test:
  `tests/ai/test_decisions_service.py::test_cooldown_fails_fast_then_recovers`.
- 1.2.5 - Log records carry no state or option text. test:
  `tests/ai/test_decisions_service.py::test_call_log_redacts_state`.
- 1.2.6 - The wire capture from a local server is recorded. behavior:
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
- Each record carries `id` (uuid4), `ts`, `model`, `state`, `questions`, the
  classifier answer or unavailable reason, the incumbent verdict, and
  `latency_ms`.
- The file is capped at 10,000 lines. On overflow it is rewritten with the
  newest 5,000 lines through a temp file and a rename.
- Writes are best effort: a failure logs once at WARNING and never raises into
  the consumer.

The file is machine-local and never committed. Labelers add a `gold` field to
the copies they curate.

`scripts/decisions_eval.py --consumer <name> --dataset <jsonl> --out <md>`
runs against `ai.decisions` loaded from the daemon config:
1. Split by `int(sha256(id), 16) % 5 == 0` into a holdout, with the rest as the
   development set.
2. Replay every record with its original order and with options, or candidate
   order in the state, reversed.
3. Compute accuracy, Brier score, ECE (10 equal-width bins on confidence for
   Choice, or on probability for Noul), and selective accuracy and coverage at
   thresholds from 0.50 to 0.95 in steps of 0.05.
4. Compute the order-flip rate, p50 and p95 latency, and cost, reported as
   `local` for a loopback backend.
5. Add the consumer's bar from Decision 12.
6. Write a Markdown report naming the model, the dataset hash, and both
   splits.

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
- 2.1.3 - The report names the model, the dataset hash, both splits, and the
  consumer bar. test:
  `tests/scripts/test_decisions_eval.py::test_report_identifies_run`.

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
- `tests/mcp_proxy/services/test_recommendation_decisions.py`

**Research context:** `_recommend_hybrid`
(`mcp_proxy/services/recommendation.py:146-212`) takes semantic
`top_k * 2` candidates and asks the LLM to rerank them. On any exception it
returns semantic order with `search_mode="hybrid_fallback"`. Choice alone
cannot reject every candidate, which is why the tool consumer uses Noul
(`jev.md` use case 2). This is the second consumer, so Noul lands here
(Decision 7).

Add `async noul(consumer, state, propositions: Mapping[str, str]) ->
dict[str, NoulAnswer(probability: float)]` to `DecisionService`. It uses the
same transport, ceiling, and cooldown; its wire field names come from a second
capture appended to `systemone-wire.md`.

Consumer: state is `{"request": task_description}`, with one proposition per
candidate, "Tool `<server>/<tool>` (`<description>`) materially applies to the
request."

Batching: if the estimate exceeds the ceiling, split the candidates across
requests. Ranking is by probability.

By `tool_rerank.mode`:
- `off`: today's path, unchanged.
- `shadow`: today's path runs and returns. The classifier runs within its own
  timeout, and a shadow record pairs its probabilities with the LLM rerank
  order.
- `enforce`, with a matching `evaluated_model`: candidates at or above
  `min_probability` come back in probability order (`search_mode="decide"`),
  and an empty result is a valid reject-all. On `DecisionsUnavailable`, the
  consumer returns semantic order with `search_mode="hybrid_fallback"`, the
  same fallback as today.

`decisions_resolver` returns the daemon's `DecisionsConfig`, or `None` when
the config is unavailable. `None` means `off`.

Scope: MCP tools only. Skill search has no reranker today, so it is not a
consumer here.

Verification planned: `DATABASE_URL=postgresql://gobby_test:gobby_test@127.0.0.1:60892/gobby_test GOBBY_TEST_PROTECT=1 uv run pytest tests/ai/test_decisions_service.py tests/mcp_proxy/services -q`;
`uv run ruff check src/ && uv run mypy src/`.

**Acceptance:**

- 3.1.1 - `noul` posts per-proposition questions and returns probabilities by
  key under the same ceiling and cooldown. test:
  `tests/ai/test_decisions_service.py::test_noul_returns_probabilities_by_key`.
- 3.1.2 - Shadow mode returns today's result unchanged and writes one shadow
  record. test:
  `tests/mcp_proxy/services/test_recommendation_decisions.py::test_shadow_keeps_llm_rerank`.
- 3.1.3 - Enforce mode ranks by probability, drops candidates below
  `min_probability`, and can return none; an unavailable classifier returns
  semantic order. test:
  `tests/mcp_proxy/services/test_recommendation_decisions.py::test_enforce_ranks_rejects_and_falls_back`.
- 3.1.4 - Enforce mode with a mismatched `evaluated_model` behaves as shadow.
  test:
  `tests/mcp_proxy/services/test_recommendation_decisions.py::test_model_mismatch_downgrades_to_shadow`.

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
  `asyncio.gather` within the same 8 s cap, and a shadow record pairs the
  probability with the LLM verdict.
- `enforce`, with a matching `evaluated_model`:
  - a probability at or above `accept_above` returns `True`;
  - a probability at or below `accept_below` returns `False`;
  - anything between escalates to today's LLM path;
  - `DecisionsUnavailable` also escalates to today's LLM path, whose own
    `None` keeps the fast-path alert.

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
  returns `False` by itself. test:
  `tests/workflows/test_found_work_confirm.py::test_outage_never_clears_a_finding`.
- 3.2.4 - Shadow mode returns the LLM verdict and writes one shadow record.
  test:
  `tests/workflows/test_found_work_confirm.py::test_shadow_returns_llm_verdict`.

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
  the loopback rule and the ruling behind it, the `/v1/systemone` contract,
  and `GET /api/llm/status`.
- Add rows to `llm-features.md` for `ai.decisions.tool_rerank` and
  `ai.decisions.found_work`, with their modes and fallbacks.
- Add a paragraph on shadow records, the 2.1 evaluation script, and the
  promotion gate.

**Acceptance:**

- 4.1.1 - The configuration guide documents `ai.decisions` with the loopback
  rule and the wire contract. behavior: "ai.decisions" in
  `docs/guides/configuration.md`.
- 4.1.2 - The features guide lists both consumers with their modes and
  fallbacks. behavior: "found_work" in `docs/guides/llm-features.md`.

## V1: Verification
`kind: verification`

Run after each leaf's final edit and again before the PD lands the branch:

```bash
DATABASE_URL=postgresql://gobby_test:gobby_test@127.0.0.1:60892/gobby_test GOBBY_TEST_PROTECT=1 uv run pytest tests/config/test_decisions_config.py tests/ai/test_capability_registry.py tests/ai/test_decisions_service.py tests/ai/test_decisions_shadow.py tests/scripts/test_decisions_eval.py tests/mcp_proxy/services tests/workflows/test_found_work_confirm.py -q
uv run ruff format --check src/ scripts/ && uv run ruff check src/ scripts/ && uv run mypy src/
uv run gobby plans validate .gobby/plans/decision-classifier-path.md -p /Users/josh/Projects/gobby
```

Live check after the PD-owned restart, on Josh's machine with a loopback
`/v1/systemone` server configured: `GET /api/llm/status` lists `decide` as
available with the configured model. With the server stopped, a
`recommend_tools` call in `shadow` mode returns its usual result. Do not run
the full pytest suite.

Plan changelog:
- 2026-09-29: First draft by Plan Writer gobby#14578.
