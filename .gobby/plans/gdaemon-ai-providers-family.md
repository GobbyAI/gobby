Plan artifact: `.gobby/plans/gdaemon-ai-providers-family.md`

# Gobby 1.0 Stage 2: native AI providers, LLM routing and local models family

**Plan ID:** gdaemon-ai-providers-family

## Overview
`kind: framing`

Port the AI providers, LLM routing and local models family (#21568 item 8,
epic #21587) from Python ownership to native Rust services. The bounded
inventory is 19 proxied ledger rows. Sixteen live in
`docs/contracts/parity-ledger-status.d/remainder.json`, whose notes cite
"#21568 item 8": the four CLI rows `gobby embeddings`, `gobby embeddings
catalog`, `gobby embeddings doctor` and `gobby embeddings switch`; seven
`/api/embeddings` HTTP rows; and five `/api/llm` HTTP rows. Three
`/api/providers` HTTP rows live in
`docs/contracts/parity-ledger-status.d/daemon-config-auth-infra.json`. The
family has zero MCP rows. The rendered `docs/contracts/parity-ledger.md` is
generated. This plan does not expand work or authorize a restart, cutover or
push.

The work is necessary: all 19 rows are proxied, and gdaemon has no
`/api/llm`, `/api/embeddings` or `/api/providers` handler. gcore already holds
the client halves (`crates/gcore/src/ai/embeddings.rs::embed_batch`,
`crates/gcore/src/qdrant.rs`, `crates/gcore/src/ai/generation/tool_loop.rs`
and the daemon clients in `crates/gcore/src/ai/daemon/operations.rs`) but no
server side. Restraint stops at one library crate, `crates/gai` (package
`gobby-ai`, lib `gobby_ai`), statically linked into gdaemon; three
`RouteFamily` instances; the shared pool, active-config and secret-store seams
from the runtime family; existing gcore clients; and the provider CLIs already
installed on the serving machine. No new process, executable, database table,
plugin framework or second capability registry is added.

Three route families give the three groups independent activation, each with
its own `front_door.routes.<name>` key and a default of Proxy:

- `ai` claims `/api/llm` and `/api/embeddings`: eight HTTP rows.
- `ai-embedding-switch` claims `/api/embeddings/switch`: four HTTP rows.
  `crates/gdaemon/src/front_door/routes.rs::RouteTable::lookup` picks the
  longest matching prefix, so switch paths never resolve to `ai`.
- `providers` claims `/api/providers`: three HTTP rows.

The four CLI rows move to a native `gdaemon embeddings` command (7.5) with the
existing `gobby embeddings` entry forwarding to it (7.7) once 7.6 has released
the binary that carries it. Activation order is `ai` (5.2), then `providers`
(6.3), then `ai-embedding-switch` (7.6); 8.1 records the ledger result.

**Boundary with `.gobby/plans/rust-internal-mcp-servers.md` (#24841).** The
family owns no MCP row, so it adds no `ToolFamily` descriptor and consumes no
#24883 type. Adjacent tools stay with their owners:
`gobby-metrics:get_provider_capacity` (#21568 item 10),
`gobby-memory:reindex_embeddings` (#21563), the `gobby-config` tools (#21559)
and the `gobby-voice` vocabulary tools (#21588).

**Boundary with Voice (#21588).** Voice owns speech-to-text, text-to-speech,
the whisper runtime and the audio capability adapters. This family owns the
capability registry port and reports the audio bindings in `GET
/api/llm/status` through the Voice seam #25072,
`voice_stt_available(voice_config) -> bool`. Python's probe is
`src/gobby/ai/registry.py::_whisper_runtime_available`, which imports
`gobby.voice.stt.WhisperSTT`. No voice code lands here.

Other boundaries: the runtime family (#21559,
`.gobby/plans/gdaemon-runtime-family.md`) owns `/api/config/*`, secrets,
generation-endpoint probe and activation (its 5.13 and 5.14), grant issuance
and capability tokens. Code index #24691 owns the node-local `POST
/api/embeddings` arm and the node bootstrap embeddings key. WS chat #21592
owns the native web-chat runtime; 6.0 defines the per-provider health
contract that runtime supplies to the providers routes at 6.3. Provider
usage #23603 owns the capacity snapshot `details` field. Memory #21563 and the
MCP front door #21570 own the collection rebuild seams #25070 and #25071.

`.gobby/plans/local-inference-runtime-foundation.md` is superseded by this
plan. It was never executed, carries no M1 and no tasks, and its planned
local-runtime surfaces are absent from the ledger. The file is not moved here;
Josh moves plan files. `.gobby/plans/decision-classifier-path.md:128-130`
cites the rule in memory c0ba0b7c that Gobby must not download, load or manage
LM Studio models. Josh retired that rule on 2026-10-25 (Decision 2). That file
is not edited here; the citation is reported to the coordinator.

**Non-goal: background capability refresh.**
`src/gobby/providers/capabilities/refresh.py::CapabilityRefreshCoordinator`
stays in Python. It is daemon maintenance that refreshes the provider
capability tables with its collectors and coverage audit, and no ledger row
of this family serves it. 6.1 reads its durable output as Python does.

**Cross-plan contention.** `.gobby/plans/gdaemon-skills-family.md` leaf 5.1
also targets `crates/gdaemon/src/main.rs::Command` and `::main`, which 7.5
edits. The two landings serialize, and the second rebases onto the first.

## Decision Record
`kind: framing`

1. The Orchestrator assigned planning task #25066 under epic #21587. Plan
   Writer gobby#17712 and Plan Adversary gobby#17713 reach consensus; Triage
   gobby#17480 is the coordination route. Plan Enhancer session cb658b79 sent
   five edits, and the Orchestrator ruled on each. ENH-001 was folded into
   AI-ADV-01. ENH-002 was accepted for its test half only (7.3.5, staging-abort
   parity). ENH-003 was rejected. ENH-004 was accepted as wording only on 4.1.2
   and 4.1.3 (vision temp-file safety). ENH-F1-001's restart gate was rejected,
   and its recovery-test label was accepted on 7.6.6. 27c530f985 applied the
   accepted parts, and the consensus record lists the same dispositions. The
   19-row inventory above is the confirmed scope.
2. **Model setup during an embedding switch (Josh, 2026-10-25, decision
   button `25066-d1v2-a`).** Josh retired memory c0ba0b7c. The native switch
   stage manages models through each provider's own HTTP API. It never starts
   a server and never runs `lms` or `ollama`. Origin is `api_base` with any
   trailing `/v1` removed.
   - LM Studio 0.4.0 or later: an exact-key check against `GET
     {origin}/api/v1/models`. A 404 there is a typed unavailable error naming
     LM Studio 0.4.0 or later. When the catalog model is absent, `POST
     {origin}/api/v1/models/download` with `{model:
     "https://huggingface.co/<gguf_repo>", quantization: <quant>}` returns a
     `job_id` or `already_downloaded`. Poll `GET
     {origin}/api/v1/models/download/status/{job_id}` (`downloading`, `paused`,
     `completed`, `failed`) within 600 seconds. Unless the model is already
     loaded, `POST {origin}/api/v1/models/load` with `{model: <lmstudio_ref>}`
     within 120 seconds.
   - Ollama: an exact-tag check against `GET {origin}/api/tags`; otherwise
     `POST {origin}/api/pull` with `{model: <ollama_tag>, stream: true}` until
     `success`, within 600 seconds.
   - vLLM and generic OpenAI-compatible endpoints: no setup.
   - No unload. The stage runs only where the hub pool exists.
3. **Collection rebuilds (Orchestrator, D2 option A).** The family ports the
   switch journal, coordinator, stage, build orchestration, alias flip and
   garbage collection. Per-collection rebuilds go through two seams: #25070
   (memories, under #21563) and #25071 (tool embeddings, under #21570). Their
   contract is `rebuild(kind, physical_name, embedder)` plus
   `project_change(kind, source_id, physical_name, embedder)`. The switch owns
   the ledger read, watermark, replay, collection creation, alias flip and GC.
   Producers keep calling `append_change` and dual-writing.
4. **Agent-token and runtime-grant admission (Orchestrator, D3).** #25068 owns
   one native seam for agent tokens and runtime grants. It depends on #25046
   (API key, local operator and confined browser admission), #24452
   (capability-token verification) and #24457 (managed grant presentation).
   Section 5.1 drafts the contract #25068 implements. Until #25068 and #25046
   land, agent, grant and cookie traffic for these rows stays proxied as a
   stated remainder.
5. **Providers routes (Josh, D4 option A).** `GET /api/providers`, `GET
   /api/providers/models` and `GET /api/providers/{provider}/usage` stay in
   this family, read per-provider web-chat health through 6.0's seam, which
   #21592's runtime supplies at 6.3, and activate
   after the `ai` family as their own family.
6. **Voice seam.** #25072 supplies `voice_stt_available(voice_config) ->
   bool`. Only 4.2 consumes it.
7. **Placement.** The runtime plan's capability rule says text generation and
   tool chat are "Always daemon": the serving daemon executes them. The three
   families compose only where the hub pool exists, exactly as `RuntimeFamily`
   composes in `crates/gdaemon/src/serve.rs`: hub and standalone. A node
   composes none, and its requests for these rows keep their current backend.
   Provider secrets are hub-owned, and a node never holds them (epic #21587).
   `docs/contracts/secrets.md`: "Gobby stores secret values in the hub.
   Decryption is limited to the daemon and trusted local binaries… remote
   clients never receive plaintext." A Codex endpoint key reaches only the
   child environment, as `GOBBY_CODEX_ENDPOINT_API_KEY`, never argv.
8. **Claude adapter.** Python's `ClaudeSDKClient` uses the Python
   `claude_agent_sdk` package, which drives the `claude` CLI over stream-json.
   The native adapter drives the same CLI directly with the options the SDK
   passes. No Rust SDK or new provider library is added.
9. **One new external library.** Vision extraction reads a multipart upload.
   axum's `multipart` feature, which adds `multer`, is the single new external
   library authorized here; 4.1 commits its `Cargo.lock` resolution. Every
   other dependency is already in the lockfile.
10. **Debt-first review (memory 9066f1eb).** The disposition list below
    decides every surfaced debt item. Two candidates proved not to be debt:
    the chat session header is already bound to the grant principal by
    `src/gobby/servers/grant_auth.py::identity_headers_match`, and the codex,
    grok and droid vision bindings already report unavailable with a reason
    in `src/gobby/ai/registry_builder.py::_vision_extract_binding`.

## Debt not to port
`kind: framing`

Josh's debt-first ruling (memory `9066f1eb`) governs this plan. Keep all 19
public surfaces; parity matches their intentional contracts.

**KEEP**, with the reason each stays:

- The reserved `project_id` and `provider` request fields on embeddings, text
  and vision requests. gcore sends them today
  (`crates/gcore/src/ai/daemon/request.rs` and
  `crates/gcore/src/ai/daemon/operations.rs`), so dropping them would break a
  live caller.
- The two capability-unavailable envelopes: embeddings uses key `error`, LLM
  routes use key `code`. Both are declared through #24970's HTTP adapter;
  gcore and web callers parse them.
- The OpenAI default endpoint `https://api.openai.com/v1` in embeddings status.
  Python accepts only known OpenAI cloud models with an explicit key when
  `api_base` is unset, so the reported default is truthful.
- The `too_late` abort conflict: 409 with an object `detail`. The CLI and web
  read its fields.
- The chat session header (see Decision 10).
- No startup auto-resume of an interrupted switch. Resume is an explicit
  operator action.
- The per-request registry build with a probe cache. Configuration changes
  apply on the next request without a restart.
- The unavailable codex, grok and droid vision bindings (see Decision 10).
- The neutral temporary working directory for CLI text generation. It keeps
  provider CLIs out of any checkout.

**DROP**, each through an exact-surface #24963 waiver:

- **`TextGeneratePayload.cwd`.** The field is never used: the CLI runner
  always runs in a neutral temporary directory, and no caller sends it.
  Native decoding ignores it like any unknown field, so a non-string `cwd` no
  longer returns 422. Owner 2.4.
- **Switch model setup through provider CLIs.** Python's
  `src/gobby/cli/installers/embedding.py::_setup_lmstudio` and
  `::_setup_ollama` start the LM Studio server, run `lms` and `ollama`, and
  match models by substring, which can skip downloading the exact catalog
  model. Native setup follows Decision 2: no server start, exact identifiers,
  and typed provider API errors in place of CLI errors. Owners 7.3 for the
  HTTP surfaces and 7.5 for the CLI surface.
- **`gobby embeddings doctor` local report.** Python reads local config and
  the hub database directly and emits dead `agrees` and `drift` fields set to
  null. The native command forwards to `gcode embeddings doctor`, whose report
  compares the database with the daemon peer and fills those fields. Owner 7.5.
- **`gobby embeddings switch` transport failure.** Python's handler catches
  `ImportError`, `OSError`, `RuntimeError` and `psycopg.Error`, but not
  httpx's transport errors, so an unreachable or timed-out daemon ends in an
  uncaught traceback with exit 1. The native command prints `Error: <message>`
  to stderr and keeps exit 1. Owner 7.5.

**Waiver inventory:**

| ID | Exact ledger surface | Owner |
| --- | --- | --- |
| `ai.http-llm-generate.unused-cwd` | `HTTP POST /api/llm/generate` | 2.4 |
| `ai.http-embeddings-switch-start.provider-api-model-setup` | `HTTP POST /api/embeddings/switch/start` | 7.3 |
| `ai.http-embeddings-switch-resume.provider-api-model-setup` | `HTTP POST /api/embeddings/switch/resume` | 7.3 |
| `ai.http-embeddings-switch-status.provider-api-model-setup` | `HTTP GET /api/embeddings/switch/status` | 7.3 |
| `ai.cli-embeddings-switch.provider-api-model-setup` | `CLI gobby embeddings switch` | 7.5 |
| `ai.cli-embeddings-doctor.gcode-report` | `CLI gobby embeddings doctor` | 7.5 |
| `ai.cli-embeddings-switch.transport-error-message` | `CLI gobby embeddings switch` | 7.5 |

Each owner adds its entries to `docs/contracts/parity-waivers.json` and depends
on #24963, which owns that file's schema, loader and consumers. Entry fields
are `id`, `surface`, `behaviour`, `decision` and `replacement`, all required.
IDs use `<family>.<surface-slug>.<behaviour-slug>`. Each `surface` is exactly
the key above. Each `decision` is a plan citation such as
`.gobby/plans/gdaemon-ai-providers-family.md#7.3`. The behaviour sentence and
replacement name the bounded changed branch. Corpus vectors keep the Python
outcome and assert the chosen native outcome.

**S1 shared readiness:** ledger generation, corpus replay and compare consume
the one #24963 manifest. A changed behavior reports `Waived` with its ID,
decision and passing native assertions, never a mask or `Skipped`. At least one
ordinary `Passed` case is required. Zero-Passed, all-Skipped, unwaived Skipped,
unknown or stale IDs, missing decisions and unmatched differences fail
readiness.

**S2 shared error owner:** #24970 supplies the single seven-class native
classification (validation, missing, forbidden, conflict, capacity,
unavailable, internal) and the HTTP and CLI adapters. This family maps its
domain errors onto those classes and declares its existing envelopes through
the adapters; it adds no family-local error framework. Preserve observed
status, envelope and CLI exit behavior unless an exact waiver above authorizes
the change.

**S3 compare safety:** compare mode runs Python and native side by side only
for read rows. Rows that spawn provider CLIs, call provider APIs, write vectors
or mutate the switch journal (`POST /api/embeddings`, `POST /api/llm/generate`,
`POST /api/llm/chat/completions`, `POST /api/llm/vision/extract` and the three
switch mutations) are proven against isolated stubs and cloned state, never by
running the effect twice in production.

## Constraints
`kind: framing`

- Observed research baseline: HEAD `6645eaf85253a50d902271d2d95773879c6988b9`.
  Existing files and symbols were inspected with `gcode outline`, `gcode
  symbol-at` and bounded literal searches. Paths under `crates/gai`, the new
  test files and proposed test symbols are new.
- Placement follows Decision 7. `crates/gdaemon/src/serve.rs` builds
  `families` from `FAMILIES` and pushes `RuntimeFamily` inside `if let
  Some(pool) = &pool`; 1.1 adds the three families in that same block through
  one `gobby_ai` call, so serve.rs (817 lines at baseline) gains only a few
  lines.
- Crate layout keeps the three branches apart. 1.1 creates `crates/gai/src/lib.rs`,
  the shared `config.rs`, `registry.rs` and `admission.rs`, and one module
  directory per family: `crates/gai/src/llm/mod.rs`,
  `crates/gai/src/providers/mod.rs` and `crates/gai/src/switch/mod.rs`. The
  `ai` chain (1.x to 5.x) edits `llm/` and owns every later crate-root edit:
  2.5 creates `local_context/`, 4.2 edits `registry.rs` and 5.1 edits
  `admission.rs`. The providers chain (6.x) edits only `providers/`, and the
  switch chain (7.x) only `switch/` and `contracts/embedding_catalog.v1.json`;
  neither edits a crate-root file, and each reads the shared modules through
  dependency edges. Outside `crates/gai`, 6.3 wires the web-chat health
  adapter in gdaemon's `serve.rs`, 7.0 edits gcore's Qdrant module and 7.5
  edits the gdaemon command dispatcher. 1.1 declares the crate's complete
  dependency set, including `clap` with `derive` and `reqwest`'s `blocking`
  feature for 7.5's command tree, except 4.1's `multipart` feature, so only
  1.1, 4.1 and the activation leaves edit `crates/gai/Cargo.toml` or
  `Cargo.lock`.
- gcore's embeddings and Qdrant clients use blocking reqwest. Async handlers
  call them through `tokio::task::spawn_blocking`.
- Storage reuses existing tables: `config_store` and `config_state` (switch
  journal keys `ai.embeddings.switch_run` and `ai.embeddings.completed_switch`),
  `embedding_generation_acks` and `embedding_projection_changes`. No schema
  change.
- Source changes are built and tested before deployment. The activation
  leaves 5.2, 6.3 and 7.6 are the shipping commits and own release bumps and
  promotion; earlier commits are unshipped source commits. Read the current
  `project_policy.release_guidance` at execution, honor the published and
  unpublished binary distinction, and use
  `src/gobby/install/bin_set_coherence.py::promote_workspace_binary_set`.
- Heavy Rust commands require the Release Manager's heavy-work admission and
  `CARGO_BUILD_JOBS=3`. Rust tests run through `cargo nextest run -p <package>`.
  No full pytest run.
- Python checks use `uv run`. Tests that touch the database use isolated
  temporary state and ports with
  `DATABASE_URL=postgresql://gobby_test:gobby_test@127.0.0.1:60892/gobby_test`
  and `GOBBY_TEST_PROTECT=1`, never the operator's daemon database.
- Provider CLIs, provider HTTP APIs, Qdrant and LM Studio or Ollama are stubbed
  in every test: fake executables on a temporary `PATH`, local HTTP stubs and
  isolated Qdrant collections. No test spends provider credit or touches an
  operator model store.
- #24968 does not apply: no row in this family reads or writes `files_home`.

Consumer evidence: the ledger shard predicate (keys containing `embeddings`,
`/api/llm` or `/api/providers`) returned exactly the 16 plus 3 keys above.
Wire-shape consumers that must keep working unchanged are
`crates/gcore/src/ai/daemon/operations.rs` (`/api/llm/generate`,
`/api/llm/vision/extract`, `/api/embeddings`),
`crates/gcore/src/ai/generation/transport.rs` (`/api/llm/chat/completions`),
`crates/gcode/src/commands/embeddings_doctor.rs` (`GET
/api/embeddings/doctor`, which needs `endpoint`, `model` and `dim`),
`crates/gruntime/src/capabilities.rs` (the `embed POST /api/embeddings` broker
operation), and the web callers `useChatPageProviderState.ts:319` and
`useSessionProviderOptions.ts:45` (`GET /api/providers`), `providerModels.ts:84`
(`GET /api/providers/models`) and `MemoryKnowledgeSection.tsx:398` (`POST
/api/embeddings/switch/start`). The Python route modules
`src/gobby/servers/routes/llm.py`, `src/gobby/servers/routes/embeddings.py`
and `src/gobby/servers/routes/providers.py` stay as the proxy backend until
the global Python removal; this plan edits none of them.

## P0: Freeze the family contract
`kind: framing`

### 0.1 Executable 19-row characterization corpus [category: test] (depends: #24963)
`kind: deliverable`

Targets:
- `scripts/ai_parity_corpus.py`
- `crates/gai/tests/fixtures/parity.json`
- `tests/contracts/test_ai_parity_corpus.py`

**Research context:** Entry points are
`src/gobby/servers/routes/llm.py::create_llm_router`,
`src/gobby/servers/routes/embeddings.py::create_embeddings_router`,
`src/gobby/servers/routes/providers.py::create_providers_router` and
`src/gobby/cli/embeddings.py::embeddings`. Existing Python fixtures live in
`tests/servers/routes/test_llm_routes.py`,
`tests/servers/routes/test_embeddings_routes.py`,
`tests/servers/routes/test_servers_routes_providers.py`, `tests/ai/` and
`tests/cli/`. Generate a committed, versioned JSON corpus from isolated Python
fixtures: method and path or command, arguments, headers, seeded config and
journal rows, stub provider behavior, expected response, exit code and
effects, with only explicitly enumerated volatile fields normalized. The
`CLI gobby embeddings switch` transport vector, whose Python outcome is an
uncaught httpx traceback, normalizes stderr to the final exception class and
message; the traceback's checkout and environment paths are its volatile
fields.

Stubs make effects observable without real providers. Fake `claude`, `codex`,
`droid`, `grok`, `agy`, `gcode`, `lms` and `ollama` executables on a temporary
`PATH` record argv, environment keys (never values), working directory and
stdin. A local OpenAI-compatible stub serves embeddings, chat completions,
`/v1/models` and the LM Studio and Ollama model APIs. An isolated Qdrant
collection set records collection, alias and point effects.

For every HTTP row, record the admission matrix: no credential, API key,
ordinary and managed browser cookies, agent token with matching and
mismatched session identity, and runtime grant with missing grant, forged
identity header, stale epoch and insufficient capability. Python's tables are
`src/gobby/servers/auth_service.py::_AGENT_CAPABILITY_MATRIX` and
`src/gobby/servers/grant_auth.py::_GRANT_ROUTES`.

Consume #24963's one `docs/contracts/parity-waivers.json` loader. The seven
changed behaviors in the waiver inventory keep their Python baseline and get
case-specific `Waived` assertions with a required native outcome, never masks
or `Skipped`. This first leaf uses test-only waiver entries and native-result
doubles to check loader matching while recording real isolated Python
baselines; the owning leaves add the production entries and replay against
real handlers. No dependency cycle is introduced.

**Acceptance:**

- 0.1.1 - The corpus inventory equals the exact 19-key ledger predicate, every
  key has a success case and a representative failure case, and a key outside
  the family is rejected.
  test: `tests/contracts/test_ai_parity_corpus.py::test_inventory_matches_ai_ledger`.
- 0.1.2 - Running the collector twice against reset isolated state gives the
  same normalized fixtures, ordering, stdout, stderr, exit codes and recorded
  stub effects. test: `tests/contracts/test_ai_parity_corpus.py::test_collection_is_reproducible`.
- 0.1.3 - Each of the 15 HTTP rows records the full admission matrix,
  including `missing_grant` 401, `forged_identity` 401 and `stale_epoch` 409
  on the eight grant rows.
  test: `tests/contracts/test_ai_parity_corpus.py::test_http_admission_matrix`.
- 0.1.4 - Each of the seven changed behaviors resolves to one exact-surface test
  waiver with explicit native assertions, while untouched cases on the same
  surface stay `Passed`.
  test: `tests/contracts/test_ai_parity_corpus.py::test_debt_waivers_are_behavior_scoped`.
- 0.1.5 - Skipped-only reports, unwaived `Skipped`, stale or missing waiver
  decisions and an unrelated injected regression fail readiness even beside a
  valid `Waived` case.
  test: `tests/contracts/test_ai_parity_corpus.py::test_waiver_does_not_hide_unrelated_diff`.

Planned verification: the five named tests with the protected Python test
prefix; collector runs use temporary homes, a temporary `PATH` and the isolated
test database. No characterization test has run during drafting.

## P1: Native families, configuration and capability reads
`kind: framing`

### 1.1 Crate, three family shells, configuration snapshot and status reads [category: code] (depends: 0.1, #24443, #24448, #24963, #24970)
`kind: deliverable`

Targets:
- `Cargo.toml`
- `Cargo.lock`
- `crates/gai/Cargo.toml`
- `crates/gai/src/lib.rs`
- `crates/gai/src/config.rs`
- `crates/gai/src/registry.rs`
- `crates/gai/src/admission.rs`
- `crates/gai/src/llm/mod.rs`
- `crates/gai/src/llm/status.rs`
- `crates/gai/src/providers/mod.rs`
- `crates/gai/src/switch/mod.rs`
- `crates/gdaemon/Cargo.toml`
- `crates/gdaemon/src/serve.rs::*` — scope-reason: push the three AI families beside RuntimeFamily in the pool-gated block of both cfg-gated serve entry points
- `crates/gai/tests/status.rs`

**Research context:** `crates/gruntime/src/lib.rs::RuntimeFamily` is the
pattern: a `NATIVE_ROUTES` table of method, path and handler rows,
`RuntimeFamily.claims` matching method, path and caller, and
`RuntimeFamily.check_proxied` for routes left to Python.
`crates/gdaemon/src/front_door/routes.rs::RouteTable::new` selects each
family's backend from `routes.get(family.name())` or the family default, and
`RouteTable::lookup` takes the longest prefix. Create `AiFamily` (name `ai`,
prefixes `/api/llm` and `/api/embeddings`), `EmbeddingSwitchFamily` (name
`ai-embedding-switch`, prefix `/api/embeddings/switch`) and `ProvidersFamily`
(name `providers`, prefix `/api/providers`), each defaulting to Proxy.
`ProvidersFamily` and `EmbeddingSwitchFamily` start with empty route tables
and claim nothing; their chains fill them. `crates/gai/src/lib.rs` exposes one
function that pushes all three, called from serve.rs only when the pool
exists. `crates/gai/Cargo.toml` declares the crate's complete dependency set,
including `clap` with `derive` and `reqwest`'s `blocking` feature for 7.5's
command tree; only 4.1 later adds a feature (`multipart`).

`crates/gai/src/admission.rs` holds `AdmissionClass`, the data form of
Python's declarations: `RuntimeGrant { capability, effectful }`, `AgentToken {
bind_identity }` and `OperatorOrBrowser`. Every route row declares its class.
Its `admitted(parts, class)` check returns false in this leaf, so every claim
fails closed and traffic stays proxied; 5.1 binds it to #25046 and #25068.
This is the stated remainder from Decision 4, not a stub awaiting a later
choice.

`config.rs` reads the active configuration through
`crates/gruntime/src/config.rs::active_state` and `machine_values`, and opens
secret bindings with `SecretStore::open_sealed` and `SecretStore::secret`
(#24448). Plaintext secrets stay in hub memory for the request. The snapshot
covers `ai.embeddings`, `ai.generation` (endpoints, profiles, timeouts, tool
loop limits), `ai.decisions`, `voice` and `attachments`.

`registry.rs` ports `src/gobby/ai/registry.py::AICapabilityRegistry`,
`CapabilityBinding.to_dict` (`capability`, `provider`, `adapter_style`,
`available`, `reason`, `models`, `strict_models`, `metadata`),
`CapabilityStatus.to_dict` (`capability`, `available`, `state`, `reason`,
`bindings`) and the builder order of `src/gobby/ai/registry_builder.py`:
embed, decide, endpoint bindings, then per-provider text, tool chat and
vision bindings. Provider presence uses a `PATH` lookup, as Python's
`shutil.which` does. The builder iterates
`src/gobby/providers/registry.py::provider_metadata`, so `registry.rs` also
carries that static table: claude, codex, droid, grok and agy, each with its
binary, display name, user directory, web-chat and agent-spawn support,
deprecation fields and unavailable reason. The agy agent-spawn and web-chat
bindings read `src/gobby/providers/version_gate.py::peek_agy_support`, so
`registry.rs` also ports that gate: `AGY_REQUIRED_VERSION` 1.1.18, an identity
of real path, device, inode, size and modification time, one `agy --version`
probe (10-second bound) when the family composes, a non-blocking peek of the
published record, and `ensure_agy_support`'s at-most-one re-probe per
identity under a per-path lock. Endpoint probes reuse a cache keyed by `api_base` and a
SHA-256 key fingerprint. Audio bindings are added in 4.2. Build the registry
per request, as Python does (KEEP).

`llm/status.rs` serves three read rows: `GET /api/llm/vision/status`, `GET
/api/embeddings/status` and `GET /api/embeddings/doctor`. Embeddings status
returns `embedding_enabled`, `capability`, `available`, `state`, `provider`,
`model`, `dim`, `reason`, `endpoint`, `api_base_configured`,
`api_key_configured`, `metadata` filtered to `api_base_configured` and `dim`,
and `bindings`. Doctor keeps `{endpoint, model, dim}`, which
`crates/gcode/src/commands/embeddings_doctor.rs` reads as its peer report. A
missing configuration returns 503 `Daemon config not found`. Map errors onto
#24970's classes.

**Granularity:** The crate, three family shells, configuration snapshot,
registry and three read rows form one usable outcome: the first native reads.
The target count exceeds six because composition crosses the crate boundary
and the shells must exist before the three chains can proceed without sharing
files. Generation, providers and the switch remain separate deliverables.

**Acceptance:**

- 1.1.1 - `HTTP GET /api/embeddings/status` matches Python's payload for
  configured, unconfigured, cloud-default and endpoint cases.
  test: `crates/gai/tests/status.rs::embeddings_status_matches_python`.
- 1.1.2 - `HTTP GET /api/embeddings/doctor` matches Python and keeps the
  `endpoint`, `model` and `dim` fields gcode's peer report reads.
  test: `crates/gai/tests/status.rs::embeddings_doctor_matches_python`.
- 1.1.3 - `HTTP GET /api/llm/vision/status` matches Python's capability
  status, including endpoint image modality and the unavailable codex, grok
  and droid bindings. test: `crates/gai/tests/status.rs::vision_status_matches_python`.
- 1.1.4 - The registry builds the same bindings, order, availability and
  reasons as Python for every non-audio capability across the fixture
  configurations. test: `crates/gai/tests/status.rs::registry_matches_python_builder`.
- 1.1.5 - Each family claims only its own implemented rows, the longest
  prefix routes switch paths to `ai-embedding-switch`, absent configuration
  keeps all three on Proxy, a node composes none, and every claim fails
  closed until admission is bound.
  test: `crates/gai/tests/status.rs::families_are_scoped_and_fail_closed`.

Planned verification: `CARGO_BUILD_JOBS=3 cargo nextest run -p gobby-ai --test status`;
`cargo check -p gobby-daemon`. No implementation check has run during
drafting.

### 1.2 Native embedding generation [category: code] (depends: 1.1)
`kind: deliverable`

Targets:
- `crates/gai/src/llm/mod.rs`
- `crates/gai/src/llm/embed.rs`
- `crates/gai/tests/embed.rs`

**Research context:**
`src/gobby/servers/routes/embeddings.py::generate_embedding_batch` accepts
`input` as a string or list, `is_query`, `model`, and the reserved
`project_id` and `provider` fields (KEEP). It returns `{embeddings, model,
dim}`. Errors: 503 `Daemon config not found`; 400 `capability_unavailable`
with body `{error, capability, available, reason, bindings}`; 400 with the
`EmbeddingGenerationError` text; 500 `Embedding generation failed`.
`src/gobby/ai/embeddings.py::_apply_prefix` adds the configured query prefix
or the built-in `search_query: ` and `search_document: ` prefixes for nomic
models. When `api_base` is set without a key, Python sends the key `unused`;
without `api_base`, only known OpenAI cloud models with an explicit key are
allowed. Reuse `crates/gcore/src/ai/embeddings.rs::embed_batch` through
`spawn_blocking`; it already reorders by index and rejects count mismatches
and duplicate indexes. Add the row to the `ai` route table with class
`RuntimeGrant { capability: embed, effectful: true }`.

**Acceptance:**

- 1.2.1 - `HTTP POST /api/embeddings` matches Python for string and list
  input, query and document prefixes, the default model and the reserved
  fields. test: `crates/gai/tests/embed.rs::embed_matches_python`.
- 1.2.2 - Unconfigured, unavailable, provider-error and malformed-response
  cases match Python's status and envelope, with `error` as the
  capability-unavailable key. test: `crates/gai/tests/embed.rs::embed_errors_match_python`.

Planned verification: `CARGO_BUILD_JOBS=3 cargo nextest run -p gobby-ai --test embed`
against a local OpenAI-compatible stub.

## P2: Native text generation
`kind: framing`

### 2.1 Feature-profile candidate contract [category: code] (depends: 1.2)
`kind: deliverable`

Targets:
- `crates/gai/contracts/profile_candidates.v1.json`
- `crates/gai/src/llm/mod.rs`
- `crates/gai/src/llm/profiles.rs`
- `crates/gai/tests/profiles.rs`
- `tests/ai/test_profile_candidates_contract.py`

Consumers unchanged:
- `src/gobby/config/feature_base.py` — no-edit-reason: DEFAULT_PROFILE_CANDIDATES stays the Python source; the guard test compares it with the contract.

**Research context:** Python resolves a profile with no configured
`ai.generation.profile_defaults.<profile>` to
`src/gobby/config/feature_base.py::DEFAULT_PROFILE_CANDIDATES`: `feature_low`
is `codex/gpt-5.6-luna` then `claude/haiku`; `feature_mid` is
`codex/gpt-6-luna` then `claude/sonnet`; `feature_high` is `codex/gpt-5.6-sol`
with `xhigh` reasoning then `claude/opus` with `high` reasoning. The runtime
config contract `crates/gcore/assets/config/runtime_config_contract.json`
declares `ai.generation.profile_defaults.{profile}` as a pattern with no
default, so native generation has no source for these values. One versioned
JSON contract holds the three ordered candidate lists with optional reasoning
pins; `crates/gai/src/llm/profiles.rs` loads it with `include_str!`, as
`crates/gcore/src/ai/generation/tool_loop.rs` loads
`crates/gcore/contracts/tool_loop_limits.v1.json`. A Python guard test,
modeled on `tests/ai/test_tool_loop_contract.py`, fails whenever the contract
and `DEFAULT_PROFILE_CANDIDATES` differ, so neither side can change alone.
Six entries do not justify a generator script or a generated Python module;
the 7.1 catalog, with dozens of fields per entry, does. The values do not
change in this leaf.

**Acceptance:**

- 2.1.1 - The contract equals `DEFAULT_PROFILE_CANDIDATES` in order,
  candidates and reasoning pins, and a changed entry on either side fails.
  test: `tests/ai/test_profile_candidates_contract.py::test_contract_matches_python_defaults`.
- 2.1.2 - The Rust loader returns the same three ordered lists and rejects a
  contract with an unknown profile or version.
  test: `crates/gai/tests/profiles.rs::profile_candidates_match_contract`.

Planned verification: the Python guard test with the protected prefix;
`CARGO_BUILD_JOBS=3 cargo nextest run -p gobby-ai --test profiles`.

### 2.2 Text-generation service and endpoint adapters [category: code] (depends: 2.1)
`kind: deliverable`

Targets:
- `crates/gai/src/llm/mod.rs`
- `crates/gai/src/llm/generation.rs`
- `crates/gai/src/llm/candidates.rs`
- `crates/gai/src/llm/endpoint.rs`
- `crates/gai/tests/generation.rs`

**Research context:** Port
`src/gobby/ai/_text_generation_service.py::TextGenerationService`.
Candidate order is explicit candidates, then provider and model together,
then the profile (configured `profile_defaults` or 2.1's contract), then the
bare provider. Each candidate selects a registry binding; any failure falls
through to the next candidate, and exhausting them returns Python's
aggregate error. A circuit breaker per binding and candidate opens after 8
failures within 60 seconds, as `tests/ai/test_text_generation_circuit_breaker.py`
pins. Spawn-cold adapter styles (`cli`, `daemon`, `llm_provider`, `acp`) use
`cli_candidate_timeout_seconds` (default 600) behind a semaphore of
`spawn_cold_max_concurrency` (3); other styles use `candidate_timeout_seconds`
(default 30); one total timeout bounds the request. The reasoning gate drops
an effort the candidate cannot apply and reports `applied_reasoning_effort`.
Prompt-echo validation rejects output that only repeats the prompt. The image
filter skips candidates whose binding cannot accept images.

`crates/gai/src/llm/endpoint.rs` ports `src/gobby/llm/local.py::LocalLLMProvider`
for named endpoints. `openai-compatible` and `vllm` use chat completions with a
120-second request bound, a 5-second connect bound and no retries; `lmstudio`
and `ollama` use their HTTP chat APIs. The adapter applies Python's default
system prompt, `max_tokens` default 8000, reasoning effort unless `auto`,
images as data-URL `image_url` parts, and normalized usage. vLLM `model: auto`
resolves exactly one served id from `{origin}/v1/models` and errors with the
served names otherwise (`docs/guides/llm-features.md`); the literal `auto` is
never sent. Endpoint keys come from 1.1's sealed bindings and stay in hub
memory. Use async reqwest, already in the lockfile.

**Acceptance:**

- 2.2.1 - Candidate order matches Python for explicit candidates, provider
  and model, configured and contract profile defaults, and a bare provider.
  test: `crates/gai/tests/generation.rs::candidate_order_matches_python`.
- 2.2.2 - Fallback, aggregate errors and the circuit breaker's open, skip and
  recovery behavior match Python.
  test: `crates/gai/tests/generation.rs::fallback_and_breaker_match_python`.
- 2.2.3 - Per-candidate and total timeouts and the cold-spawn concurrency limit
  match Python. test: `crates/gai/tests/generation.rs::timeouts_and_cold_spawn_limit_match_python`.
- 2.2.4 - The reasoning gate, prompt-echo rejection and image filter match
  Python. test: `crates/gai/tests/generation.rs::request_gates_match_python`.
- 2.2.5 - Endpoint adapters match Python's wire requests and parsed results
  for every protocol, vLLM `auto`, images, usage and key handling.
  test: `crates/gai/tests/generation.rs::endpoint_adapters_match_python`.

Planned verification: `CARGO_BUILD_JOBS=3 cargo nextest run -p gobby-ai --test generation`
against local HTTP stubs.

### 2.3 CLI provider adapters [category: code] (depends: 2.2)
`kind: deliverable`

Targets:
- `crates/gai/src/llm/mod.rs`
- `crates/gai/src/llm/cli_runner.rs`
- `crates/gai/src/llm/adapters/mod.rs`
- `crates/gai/src/llm/adapters/claude.rs`
- `crates/gai/src/llm/adapters/codex.rs`
- `crates/gai/src/llm/adapters/droid.rs`
- `crates/gai/src/llm/adapters/grok.rs`
- `crates/gai/src/llm/adapters/agy.rs`
- `crates/gai/tests/cli_adapters.rs`

**Research context:** The shared runner is
`src/gobby/ai/_text_generation_adapters.py::_run_cli_text_generation_command`.
The child environment is the daemon environment plus adapter overrides, with
`GOBBY_AGENT_API_TOKEN` removed and `GOBBY_HOOKS_DISABLED=1` set. The child
starts in a new session, as `src/gobby/utils/spawn.py::create_session_exec`
does, with a fresh neutral temporary directory `gobby-textgen-*` as its
working directory (KEEP). A nonzero exit returns `<Provider> CLI failed with
exit code N`. Cleanup sends SIGTERM to the process group, waits 2 seconds,
sends SIGKILL and waits 2 more; shutdown cancels tracked runs within 5 seconds.
Use `tokio::process` with a new process group and `libc::killpg`, both already
in the lockfile.

Adapters, each recorded by 0.1's fake executables:

- **Claude.** `src/gobby/llm/claude_sdk.py::ClaudeSDKClient.generate_text_result`
  runs `claude_agent_sdk.query` with default system prompt `You are a helpful
  assistant.`, `max_turns` 8, default model `claude-sonnet-4-6`, no tools, no
  MCP servers, the textgen working directory,
  `CLAUDE_CODE_DISABLE_AUTO_MEMORY=1`, reasoning options, 3 retries, images as
  image blocks and normalized usage; blank output is an error. Native runs the
  `claude` CLI with the argv, environment and stream-json input the SDK builds
  for those options (Decision 8).
- **Codex.** `codex --ask-for-approval never [-c ...] exec --ephemeral
  --ignore-user-config --skip-git-repo-check --ignore-rules --sandbox
  read-only --output-last-message <cwd>/last-message.txt [--model M] [-c
  model_reasoning_effort="E"] [--image P] -`, with the prompt on stdin.
  Responses-wire endpoints run through this adapter with the `-c` overrides
  from `src/gobby/ai/codex_endpoint.py` (`model_providers.<id>.env_key` and
  `shell_environment_policy.exclude`); `codex_endpoint_env` puts the key only
  in the child environment as `GOBBY_CODEX_ENDPOINT_API_KEY`, never on argv or
  in a `-c` value.
- **Droid.** `droid exec --output-format text [--model M] [--reasoning-effort
  E] "<prompt>"`, with isolated `HOME` and XDG temporary directories that seed
  the whitelisted `~/.factory` files unless `FACTORY_API_KEY` is set, and its
  own 600-second default timeout.
- **Grok.** `grok --output-format plain --permission-mode plan --max-turns 1
  --no-memory --no-subagents --disable-web-search --deny * --disallowed-tools
  * --leader-socket <cwd>/leader.sock [--model M] [--reasoning-effort E]
  --single "<system>\n\n<prompt and one-shot directive>"`.
- **Agy.** `agy --sandbox --print-timeout Ns [--model <display name from
  model and effort>] --print "<prompt>"`.

**Granularity:** Five adapters share one runner and one adapter trait; each
lives in its own file so none nears the line ceiling. They form one outcome,
CLI-backed text generation, and are tested together against one fake-binary
harness. Tool chat styles for the same providers are separate (3.2).

**Acceptance:**

- 2.3.1 - The runner reproduces Python's environment, working directory,
  session, exit-error text, timeout and process-group cleanup, and leaves no
  orphan process. test: `crates/gai/tests/cli_adapters.rs::runner_isolation_and_cleanup_match_python`.
- 2.3.2 - The Claude adapter matches the SDK's recorded argv, environment,
  stream-json exchange, images, usage, retries and blank-output error.
  test: `crates/gai/tests/cli_adapters.rs::claude_adapter_matches_python`.
- 2.3.3 - The Codex adapter matches Python, including responses-wire
  endpoints, and the endpoint key appears only in the child environment.
  test: `crates/gai/tests/cli_adapters.rs::codex_adapter_matches_python`.
- 2.3.4 - The Droid adapter matches Python's argv, isolated home and seeding.
  test: `crates/gai/tests/cli_adapters.rs::droid_adapter_matches_python`.
- 2.3.5 - The Grok adapter matches Python's argv and parsing.
  test: `crates/gai/tests/cli_adapters.rs::grok_adapter_matches_python`.
- 2.3.6 - The Agy adapter matches Python's argv, display-name resolution and
  parsing. test: `crates/gai/tests/cli_adapters.rs::agy_adapter_matches_python`.

Planned verification: `CARGO_BUILD_JOBS=3 cargo nextest run -p gobby-ai --test cli_adapters`
with fake executables on a temporary `PATH`.

### 2.4 Native text generation route [category: code] (depends: 2.3, #24963)
`kind: deliverable`

Targets:
- `crates/gai/src/llm/mod.rs`
- `crates/gai/src/llm/generate.rs`
- `crates/gai/tests/generate.rs`
- `docs/contracts/parity-waivers.json`

**Research context:** `src/gobby/servers/routes/llm.py::TextGeneratePayload`
carries `prompt` (min length 1), `provider`, `profile`, `candidates`,
`system_prompt` (alias `system`), `model`, `max_tokens` (positive),
`reasoning_effort`, `cwd`, the three positive timeouts and `images` (paths
on the serving machine). `TextGeneratePayload.effective_profile` defaults to
`feature_low` when no provider, model, profile or candidates are given.
`src/gobby/servers/routes/llm.py::generate_text` caps timeouts through
`_generation_timeout_overrides` and returns `{text, capability, provider,
model}` plus `usage` and `applied_reasoning_effort` when present. Errors: 400
`capability_unavailable` with `{code, capability, provider, model, reason}`;
400 with the `ValueError` text; 500 `Text generation failed`; 503 when config
or the service is missing. The caller is
`crates/gcore/src/ai/daemon/operations.rs` (`TEXT_GENERATE_PATH`). Declare
class `RuntimeGrant { capability: text_generate, effectful: true }`.

Add `ai.http-llm-generate.unused-cwd` to `docs/contracts/parity-waivers.json`
with decision `.gobby/plans/gdaemon-ai-providers-family.md#2.4`. Native
decoding has no `cwd` field and ignores it like any unknown field; the waived
case is Python's 422 for a non-string `cwd`.

**Acceptance:**

- 2.4.1 - `HTTP POST /api/llm/generate` matches Python's routing, defaults,
  timeout caps, images and response fields.
  test: `crates/gai/tests/generate.rs::generate_matches_python`.
- 2.4.2 - Unavailable, invalid, failed and unconfigured requests match
  Python's status and `code` envelope.
  test: `crates/gai/tests/generate.rs::generate_errors_match_python`.
- 2.4.3 - The `cwd` case reports `Waived` with its exact entry and decision,
  and every other case on the row stays `Passed`.
  test: `crates/gai/tests/generate.rs::unused_cwd_is_waived`.

Planned verification: `CARGO_BUILD_JOBS=3 cargo nextest run -p gobby-ai --test generate`.

### 2.5 Local endpoint context observations [category: code] (depends: 2.4)
`kind: deliverable`

Targets:
- `crates/gai/src/lib.rs`
- `crates/gai/src/local_context/mod.rs`
- `crates/gai/src/local_context/route.rs`
- `crates/gai/src/local_context/observation.rs`
- `crates/gai/src/local_context/collectors.rs`
- `crates/gai/src/local_context/store.rs`
- `crates/gai/src/llm/endpoint.rs`
- `crates/gai/tests/local_context.rs`

**Research context:** Before every endpoint generation,
`src/gobby/llm/local.py::LocalLLMProvider._refresh_local_context` refreshes
exact local evidence without making it an execution prerequisite: a failure
is logged and generation proceeds. `generate_text_result` and `generate_json`
both call it, so 2.2's endpoint adapter (and 4.1's vision path through it)
gains the same call. `GET /api/providers/models` reads the result in 6.1.

Port the endpoint-route half of Python's local context modules under
`src/gobby/providers/capabilities/`:

- `local_context_config.py::endpoint_route` and `LocalContextRoute`: machine
  id, endpoint id, normalized identity base, configuration fingerprint and
  `refresh_key`. `cli_endpoint_route` and the codex and claude CLI routes
  stay with agent spawn, their only caller.
- `local_context.py`: `LocalContextIdentity`, `LocalContextInstance`,
  `LocalContextObservation` with `to_dict`, `ContextDiagnostic`,
  `build_context_observation` and `effective_local_limit`.
- The four collectors in `local_context_refresh.py::_default_collectors`:
  generic, lmstudio (with instance id), ollama and vllm, from
  `local_context_generic.py`, `local_context_lmstudio.py`,
  `local_context_ollama.py` and `local_context_vllm.py`.
- `local_context_store.py::LocalContextStore`: observations live in the
  existing `provider_model_capabilities` and
  `provider_capability_refresh_state` tables under the `__local_context__`
  provider namespace with source key `local-context`. Writes take the same
  `pg_advisory_xact_lock(hashtextextended('gobby:provider-capability:<provider>', 0))`
  as `src/gobby/providers/capabilities/store.py::ProviderCapabilityStore._lock_provider`
  and the same generation rule, so Python and native writers interleave
  safely.
- `local_context_refresh.py::LocalContextService`: per endpoint, a shared
  in-flight refresh per `refresh_key` whose cancellation is not shared, a
  generation token that marks superseded work `SUPERSEDED`, invalidation of
  the previous route, `INVALID_METADATA` for a mismatched observation, and
  `get_observation`, which returns only evidence this process refreshed for
  the current route.

The process-local freshness rule is kept exactly. One consequence follows
from serving two processes during staged activation: evidence refreshed by
Python, such as by its agent spawn, appears only in Python-served responses,
and evidence refreshed natively appears only in native ones. 6.3 accounts for
this when choosing compare rows. The module stays split across five files so
each stays well under the production line ceiling.

**Granularity:** The route, observation, collector, store and refresh files
form one lifecycle owner, local context evidence. They split only to respect
the file-size ceiling, and none is independently usable.

**Acceptance:**

- 2.5.1 - Endpoint routes, fingerprints and refresh keys match Python for
  every protocol and configuration change.
  test: `crates/gai/tests/local_context.rs::endpoint_routes_match_python`.
- 2.5.2 - Each collector produces Python's observation, diagnostics and
  effective limit from recorded stub responses.
  test: `crates/gai/tests/local_context.rs::collectors_match_python`.
- 2.5.3 - Stored rows, namespace, generation and lock interleave with
  Python's writer without lost or torn snapshots.
  test: `crates/gai/tests/local_context.rs::store_interoperates_with_python`.
- 2.5.4 - Shared in-flight refresh, supersession, invalidation and the
  process-local `get_observation` rule match Python.
  test: `crates/gai/tests/local_context.rs::refresh_service_matches_python`.
- 2.5.5 - Endpoint generation refreshes before each request, and a refresh
  failure is logged without failing generation.
  test: `crates/gai/tests/local_context.rs::generation_refresh_is_best_effort`.

Planned verification: `CARGO_BUILD_JOBS=3 cargo nextest run -p gobby-ai --test local_context`
against local HTTP stubs and an isolated database; the Python interleave case
runs with the protected Python prefix.

## P3: Native tool chat
`kind: framing`

### 3.1 Read-only gcode tool runtime and endpoint tool loop [category: code] (depends: 2.5)
`kind: deliverable`

Targets:
- `crates/gai/src/llm/mod.rs`
- `crates/gai/src/llm/tools.rs`
- `crates/gai/src/llm/endpoint_chat.rs`
- `crates/gai/tests/tool_runtime.rs`

**Research context:** Port `src/gobby/ai/_tool_chat_tools.py::ToolRuntime`,
`run_argv` and `validate_policy`. The policy is `{cli: "gcode", tools,
allow_mutation}`; tools must come from `GCODE_READONLY_TOOLS`, or from
`GCODE_ALLOWED_TOOLS` when mutation is allowed, with `_GCODE_READONLY_NESTED`
for nested subcommands. `_reject_metacharacters` refuses any argument holding
one of ``;&|`$<>``, newline, carriage return or NUL. Tool names are
`gcode_<subcommand>` with input schema `{args: string[]}`. Execution runs the
`gcode` found on `PATH` with the project path as working directory, strips
`DATABASE_URL`, `GCODE_DATABASE_URL` and `GOBBY_POSTGRES_DSN`, applies the
per-tool timeout and the `max_bytes_per_tool_result` stdout cap, and keeps a
2048-byte stderr tail.

Endpoint styles (`openai_compatible`, `local`) run the loop server-side with
OpenAI tool schemas. Reuse
`crates/gcore/src/ai/generation/tool_loop.rs::run_tool_loop` with a
`ChatTransport` over 2.2's endpoint client and a `ToolExecutor` over this
runtime; the trait is synchronous, so the loop runs under `spawn_blocking`.
Limits come from the request, else `ai.generation.tool_loop`, validated by
`ToolLoopLimits` against `crates/gcore/contracts/tool_loop_limits.v1.json`.
Stop reasons are `completed`, `max_turns`, `max_tool_calls` and `timeout`.

**Acceptance:**

- 3.1.1 - Policy validation matches Python for read-only, allowed, nested and
  unknown tools and for every rejected metacharacter.
  test: `crates/gai/tests/tool_runtime.rs::policy_matches_python`.
- 3.1.2 - gcode execution matches Python's argv, working directory, stripped
  environment, timeout, stdout cap and stderr tail.
  test: `crates/gai/tests/tool_runtime.rs::gcode_execution_matches_python`.
- 3.1.3 - The endpoint loop matches Python's turns, tool results, limits and
  stop reasons through `run_tool_loop`.
  test: `crates/gai/tests/tool_runtime.rs::endpoint_tool_loop_matches_python`.

Planned verification: `CARGO_BUILD_JOBS=3 cargo nextest run -p gobby-ai --test tool_runtime`
with a fake `gcode` and an endpoint stub.

### 3.2 Provider tool-chat styles [category: code] (depends: 3.1)
`kind: deliverable`

Targets:
- `crates/gai/src/llm/mod.rs`
- `crates/gai/src/llm/tool_chat/mod.rs`
- `crates/gai/src/llm/tool_chat/mcp_server.rs`
- `crates/gai/src/llm/tool_chat/claude.rs`
- `crates/gai/src/llm/tool_chat/codex.rs`
- `crates/gai/src/llm/tool_chat/droid.rs`
- `crates/gai/src/llm/tool_chat/grok.rs`
- `crates/gai/tests/tool_chat_styles.rs`

**Research context:** `src/gobby/ai/_tool_chat_service.py::ToolChatService`
selects an adapter per binding style and falls back only on
`CapabilityUnavailableError`, under one deadline. Styles:

- **Claude** (`llm_provider`). Python's SDK hosts an in-process MCP server
  named `repo`. Native hosts the same tools on the loopback MCP server below
  and passes it to the `claude` CLI as server `repo`, so tool names are
  unchanged.
- **Codex** (`daemon`). `codex app-server` JSON-RPC with dynamic tools.
- **Droid** (`cli`). Stream JSON-RPC plus the loopback MCP server.
- **Grok** (`acp`). The ACP session lets Grok run gcode itself through its
  shell under the same policy.

`src/gobby/ai/_tool_chat_mcp_server.py::ToolRuntimeMCPServer` is a small
hand-written MCP endpoint: protocol `2025-06-18`, server name
`gobby-tool-loop`, 1 MiB request bound, a per-run bearer token, loopback bind,
and `ToolLoopController` enforcing turn and call budgets. Port it onto axum
and tokio, already dependencies; it needs no MCP library. Each run's server
dies with the run.

**Granularity:** Four provider styles share one tool runtime, one loopback
server and one fallback rule. They are one outcome, tool chat across the
installed providers; the endpoint style landed in 3.1 and the route lands in
3.3.

**Acceptance:**

- 3.2.1 - The loopback MCP server matches Python's protocol version,
  bearer check, request bound, loopback bind, budgets and teardown.
  test: `crates/gai/tests/tool_chat_styles.rs::loopback_mcp_server_matches_python`.
- 3.2.2 - The Claude style exposes the `repo` tools and matches Python's
  turns, stop reasons and investigation fields.
  test: `crates/gai/tests/tool_chat_styles.rs::claude_style_matches_python`.
- 3.2.3 - The Codex app-server style matches Python's JSON-RPC exchange and
  dynamic tool results. test: `crates/gai/tests/tool_chat_styles.rs::codex_style_matches_python`.
- 3.2.4 - The Droid style matches Python's stream exchange and MCP use.
  test: `crates/gai/tests/tool_chat_styles.rs::droid_style_matches_python`.
- 3.2.5 - The Grok ACP style matches Python's session and policy.
  test: `crates/gai/tests/tool_chat_styles.rs::grok_style_matches_python`.
- 3.2.6 - Fallback happens only on capability unavailability, and one
  deadline bounds every style.
  test: `crates/gai/tests/tool_chat_styles.rs::fallback_and_deadline_match_python`.

Planned verification: `CARGO_BUILD_JOBS=3 cargo nextest run -p gobby-ai --test tool_chat_styles`
with fake provider executables.

### 3.3 Native chat completions with managed tool credentials [category: code] (depends: 3.2, #24454, #24457)
`kind: deliverable`

Targets:
- `crates/gai/src/llm/mod.rs`
- `crates/gai/src/llm/chat.rs`
- `crates/gai/src/llm/tool_lease.rs`
- `crates/gai/tests/chat.rs`

**Research context:** `src/gobby/servers/routes/llm.py::chat_completions`
accepts `messages`, `project_path`, `tool_policy`, `caller`, `request_id` (a
UUID4), `limits` and `profile` (default `feature_high`), rejecting extra
fields. `_split_chat_messages` joins system messages into the system prompt
and renders the others as `Role:\ncontent`. The session is the verified
grant principal's run session; a request without it returns 401
`Authenticated session header is required`. The response is OpenAI-shaped
`choices` plus `investigation {caller, request_id, tool_use_count, turns,
tools, adapter_style, stop_reason}`. `finish_reason` maps empty,
`completed` and `stop` to `stop`, `max_turns` and `max_tool_calls` to
`length`, and passes others through. Failure returns 500 `tool_chat failed`.
The caller is `crates/gcore/src/ai/generation/transport.rs`
(`DAEMON_CHAT_COMPLETIONS_PATH`).

Every run holds a managed tool credential. Python's
`src/gobby/ai/_managed_tool_chat_lease.py::build_managed_tool_chat_lease_factory`
calls `src/gobby/storage/managed_credentials.py::ManagedCredentialManager.issue_tool_request`
with the session, the requested project path (checked against the
operation root and overlay by `resolve_operation_root`) and owner kind
`tool_chat`, materializes the launch credential for the child, and revokes it
when the run ends. Native uses #24454's managed issuance and #24457's grant
presentation; it writes no second issuer. The overlay check fails closed.
Declare class `RuntimeGrant { capability: tool_chat, effectful: true }`.

**Acceptance:**

- 3.3.1 - `HTTP POST /api/llm/chat/completions` matches Python's request
  validation, message split, response, `finish_reason` map and investigation
  fields. test: `crates/gai/tests/chat.rs::chat_completions_match_python`.
- 3.3.2 - Each run issues one managed tool credential bound to the session and
  checked project root, the child receives it, and it is revoked on success,
  failure and cancellation; an overlay mismatch refuses before any spawn.
  test: `crates/gai/tests/chat.rs::managed_tool_credential_lifecycle`.
- 3.3.3 - Missing session, unavailable capability and adapter failure match
  Python's status and body. test: `crates/gai/tests/chat.rs::chat_errors_match_python`.

Planned verification: `CARGO_BUILD_JOBS=3 cargo nextest run -p gobby-ai --test chat`
with isolated credential state.

## P4: Vision and the full capability snapshot
`kind: framing`

### 4.1 Native vision extraction [category: code] (depends: 3.3)
`kind: deliverable`

Targets:
- `crates/gai/Cargo.toml`
- `Cargo.lock`
- `crates/gai/src/llm/mod.rs`
- `crates/gai/src/llm/vision.rs`
- `crates/gai/tests/vision.rs`

**Research context:** `src/gobby/servers/routes/llm.py::extract_vision` takes
a multipart `file` plus `provider`, `model` and `context`, bounds the upload at
the smaller of `MAX_IMAGE_BYTES` and `attachments.max_file_bytes`, and
returns `{text, description, ocr_text, bytes, content_type, capability,
provider, model}`. The upload goes to `$TMPDIR/gobby-vision/vision-*<suffix>`
with directory mode 0700 and file mode 0600 and is deleted when the request
ends. `src/gobby/servers/routes/llm.py::start_vision_temp_cleanup_task` sweeps
that directory every 3600 seconds and deletes files older than 24 hours; the
native sweep starts with the family and stops at shutdown.
`src/gobby/servers/routes/llm.py::_write_temp_image` writes only after the
bounded read, so an over-limit or interrupted upload leaves no file. It
creates the file exclusively under the `_image_suffix` allowlist (`.jpg`,
`.jpeg`, `.png`, `.gif`, `.webp`, otherwise `.png`), so a client filename
never selects a path. The sweep unlinks directory entries, so a symlink is
removed without touching its target.
`src/gobby/ai/vision.py::VisionExtractService.extract` routes Claude through
its adapter and endpoints whose input modalities include `image` through the
endpoint adapter, or through `codex app-server` (3.2's client) for
responses-wire endpoints. The codex, grok and droid bindings stay unavailable
(KEEP). Enable axum's `multipart` feature (Decision 9) and commit the
`multer` resolution. Declare class `RuntimeGrant { capability:
vision_extract, effectful: true }`.

**Acceptance:**

- 4.1.1 - `HTTP POST /api/llm/vision/extract` matches Python for Claude,
  endpoint and responses-wire bindings.
  test: `crates/gai/tests/vision.rs::extract_matches_python`.
- 4.1.2 - Upload bounds, content types, unavailable bindings and adapter
  failures match Python's status and body, and an over-limit or interrupted
  upload leaves no file.
  test: `crates/gai/tests/vision.rs::upload_bounds_and_errors_match_python`.
- 4.1.3 - Temporary files are private and created exclusively under an
  allowlisted suffix, so a traversal-style filename never selects a path.
  They are deleted after every request outcome and swept after 24 hours, and
  the sweep removes a symlink without touching its target.
  test: `crates/gai/tests/vision.rs::temp_files_are_private_and_swept`.

Planned verification: `CARGO_BUILD_JOBS=3 cargo nextest run -p gobby-ai --test vision`.

### 4.2 Full capability snapshot with Voice audio bindings [category: code] (depends: 4.1, #25072)
`kind: deliverable`

Targets:
- `crates/gai/src/registry.rs`
- `crates/gai/src/llm/mod.rs`
- `crates/gai/src/llm/status.rs`
- `crates/gai/tests/llm_status.rs`

**Research context:** `src/gobby/servers/routes/llm.py::llm_status` returns
the registry's `status_snapshot`, `{"capabilities": {...}}`, for every
`src/gobby/ai/registry.py::AICapability` value. The audio bindings come from
`src/gobby/ai/registry_builder.py`: whisper transcription and translation
when voice enables speech-to-text and the runtime is present, and
OpenAI-compatible audio bindings from `voice.openai_compatible_audio`.
Python's runtime check is `src/gobby/ai/registry.py::_whisper_runtime_available`.
Native calls #25072's `voice_stt_available(voice_config) -> bool` and owns no
voice code. Declare class `RuntimeGrant { capability: text_generate,
effectful: false }`.

**Acceptance:**

- 4.2.1 - `HTTP GET /api/llm/status` matches Python's full snapshot across
  the fixture configurations. test: `crates/gai/tests/llm_status.rs::llm_status_matches_python`.
- 4.2.2 - Audio bindings follow the Voice seam's answer and Python's
  configuration rules, with matching reasons when unavailable.
  test: `crates/gai/tests/llm_status.rs::audio_bindings_use_voice_seam`.

Planned verification: `CARGO_BUILD_JOBS=3 cargo nextest run -p gobby-ai --test llm_status`
with a stub Voice seam.

## P5: ai family admission and activation
`kind: framing`

### 5.1 Native admission binding and the #25068 contract [category: code] (depends: 4.2, #25046, #25068)
`kind: deliverable`

Targets:
- `crates/gai/src/admission.rs`
- `crates/gai/tests/admission.rs`
- `docs/contracts/ai-native.md`

**Research context:** This section is the contract #25068 implements. Python's
tables are `src/gobby/servers/grant_auth.py::_GRANT_ROUTES` (`GrantRoute`
method, route, effectful and `RequiredCapability(name, mode)`) and
`src/gobby/servers/auth_service.py::_AGENT_CAPABILITY_MATRIX` (`_AgentRoute`
method, route and `bind_identity`). The 15 HTTP rows fall into three classes.

Runtime-grant rows, all with mode `daemon` and `bind_identity` true for an
accompanying agent token:

- `POST /api/embeddings`: capability `embed`, effectful.
- `GET /api/embeddings/status`: `embed`.
- `GET /api/embeddings/doctor`: `embed`.
- `GET /api/llm/status`: `text_generate`.
- `POST /api/llm/generate`: `text_generate`, effectful.
- `POST /api/llm/chat/completions`: `tool_chat`, effectful.
- `GET /api/llm/vision/status`: `vision_extract`.
- `POST /api/llm/vision/extract`: `vision_extract`, effectful.

Every caller class must present `X-Gobby-Runtime-Grant` on these rows. A
missing grant returns 401 `missing_grant`; an identity header that differs
from the grant principal (`identity_headers_match`) returns 401
`forged_identity`; a stale fencing epoch returns 409 `stale_epoch`; an
insufficient or revoked grant keeps Python's refusal. A bearer is optional;
when present it must match the grant principal's machine and, for an agent
token, its session. Effectful rows pass through the grant lease fence before
any effect.

Agent-token rows with `bind_identity` false, also open to operators and
browsers: `GET /api/providers/models` and `GET /api/embeddings/switch/status`.

Operator and browser rows: `GET /api/providers`, `GET
/api/providers/{provider}/usage`, `POST /api/embeddings/switch/start`, `POST
/api/embeddings/switch/resume` and `POST /api/embeddings/switch/abort`. API
keys, the local operator and ordinary `gobby_session` cookies keep Python's
per-route permissions. Managed `browser-testing.` cookies keep
`src/gobby/servers/browser_sessions.py::browser_route_allowed`: GET, HEAD and
OPTIONS only, so the three switch mutations refuse them before any effect.

The family consumes the Admitted value #25046 and #25068 bind before
dispatch; it never verifies a token, grant or cookie itself. This leaf
replaces 1.1's fail-closed `admitted` with a match of that value against each
row's declared `AdmissionClass`. Unavailable authority returns a typed 503.
Until both seams land and Security approves this leaf's exact SHA, every row
stays proxied in all switch states; the activation record states that
remainder.

**Expansion obligation:** Immediately after expansion, the Orchestrator applies
`security-review` to the leaf for source section 5.1 and checks the persisted
label before dispatch. A missing label holds dispatch. Native admission stays
held until a Security Reviewer records an approved step for the exact
implementation SHA; a later implementation change needs renewed approval.
Server-derived M1 emits coverage labels only, so this obligation needs no
emitter change.

**Acceptance:**

- 5.1.1 - The eight `ai` rows declare exactly Python's grant capability,
  mode and effectful flag, and grant callers are admitted or refused with
  Python's codes before any effect.
  test: `crates/gai/tests/admission.rs::grant_rows_match_python`.
- 5.1.2 - Agent tokens with matching, mismatched, revoked and expired
  identity get Python's outcomes for both `bind_identity` settings.
  test: `crates/gai/tests/admission.rs::agent_token_classes_match_python`.
- 5.1.3 - API keys, the local operator and ordinary cookies keep Python's
  per-route permissions, and managed cookies are refused on mutations.
  test: `crates/gai/tests/admission.rs::operator_and_browser_classes_match_python`.
- 5.1.4 - Without a bound Admitted value the row stays proxied, unavailable
  authority returns 503, and no second verifier exists in the crate.
  test: `crates/gai/tests/admission.rs::admission_fails_closed_without_shared_seam`.
- 5.1.5 - The admission record names the persisted `security-review` label,
  the pre-dispatch check and the approved step for the exact SHA; absent or
  stale approval holds native admission.
  behavior: "AI admission security gate" in `docs/contracts/ai-native.md`.

Planned verification: `CARGO_BUILD_JOBS=3 cargo nextest run -p gobby-ai --test admission`
composing the landed #25046 and #25068 seams with isolated credential state;
0.1's admission-matrix test with the protected Python prefix.

### 5.2 ai family activation and coherent release [category: code] (depends: 5.1, 7.0, #24963)
`kind: deliverable`

Targets:
- `crates/gdaemon/Cargo.toml`
- `crates/gcode/Cargo.toml`
- `crates/ghook/Cargo.toml`
- `Cargo.lock`
- `src/gobby/install/version_pins.py::MANAGED_BIN_VERSION_PINS`
- `crates/gdaemon/tests/ai_cutover.rs`
- `tests/e2e/test_ai_native.py`
- `docs/contracts/ai-native.md`

Consumers unchanged:
- `src/gobby/cli/install_setup_gclient.py` — no-edit-reason: dynamically reads its unchanged native version pin.
- `src/gobby/cli/install_setup_gcode.py` — no-edit-reason: dynamically reads its bumped native version pin.
- `src/gobby/cli/install_setup_gdaemon.py` — no-edit-reason: dynamically reads the bumped gdaemon pin.
- `src/gobby/cli/install_setup_ghook.py` — no-edit-reason: dynamically reads its bumped native version pin.
- `src/gobby/cli/install_setup_gmcp.py` — no-edit-reason: dynamically reads its unchanged native version pin.
- `src/gobby/cli/install_setup_gterm.py` — no-edit-reason: dynamically reads unchanged native version pins.
- `src/gobby/cli/install_setup_versions.py` — no-edit-reason: resolves every pin dynamically by binary name.
- `src/gobby/code_index/gcode_versions.py` — no-edit-reason: both gcode maintenance floors derive from the bumped gcode pin.
- `src/gobby/hooks/runtime_compat.py` — no-edit-reason: the hook compatibility floor derives from the bumped ghook pin.
- `src/gobby/install/bin_freshness_models.py` — no-edit-reason: enumerates the map dynamically.
- `src/gobby/install/distribution.py` — no-edit-reason: resolves minimum versions dynamically.
- `tests/cli/installers/test_grok_installer.py` — no-edit-reason: derives expected ghook versions from the map.
- `tests/cli/test_install_ghook.py` — no-edit-reason: derives expected versions from the map.
- `tests/cli/test_install_setup.py` — no-edit-reason: derives expected versions from the map.
- `tests/cli/test_install_setup_gclient.py` — no-edit-reason: derives expected versions from the map.
- `tests/cli/test_install_setup_gdaemon.py` — no-edit-reason: derives expected versions from the map.
- `tests/cli/test_install_setup_gmcp.py` — no-edit-reason: derives expected versions from the map.
- `tests/cli/test_install_setup_gterm.py` — no-edit-reason: derives expected versions from the map.
- `tests/code_index/test_gcode_gateway.py` — no-edit-reason: derives gcode expectations from the bumped pin.
- `tests/hooks/test_runtime_compat.py` — no-edit-reason: derives ghook expectations from the bumped pin.
- `tests/install/test_distribution.py` — no-edit-reason: dynamically parameterizes managed version expectations.
- `tests/install/test_version_pins.py` — no-edit-reason: already verifies each managed pin against its Cargo manifest.

**Research context:** Activation selects `front_door.routes.ai` through the
existing bootstrap mechanism; Proxy stays selectable for rollback. Compare
runs only the read rows (`GET /api/llm/status`, `GET /api/llm/vision/status`,
`GET /api/embeddings/status`, `GET /api/embeddings/doctor`). The effectful
rows are proven by the owning leaves' stub tests and isolated e2e runs, never
by running a provider call twice (S3). Consume #24963's readiness result:
`ai.http-llm-generate.unused-cwd` needs its exact entry and passing native
assertion, and at least one ordinary `Passed` case is mandatory. The
`ai-embedding-switch` and `providers` families stay on Proxy.

This is the first shipping commit since 1.1 added the `crates/gai` member to
the root `Cargo.toml`. `src/gobby/install/bin_release_identity.py::workspace_source_identity`
hashes the root manifest for every managed binary, so gdaemon, gcode and ghook
each take a 0.0.1 bump above their preceding released version, with manifests,
lock and pins updated. 7.0's gcore Qdrant alias operations ship in this same
release, so no later leaf in this plan changes gcode's or ghook's source
identity. Rebuild gclient, gmcp and gterm alongside;
unpublished binaries keep their versions and use the existing component
promotion paths. Promote the coherent trio through
`promote_workspace_binary_set` and read installed hashes after signing. Only
the Orchestrator delegates cutover, with a global announcement, a quiet
window, a passing source, schema and installed-set preflight, and a health
report. No push is authorized here.

**Granularity:** The targets and acceptance items belong to one
family-specific activation and release state machine. 8.1 separately records
ledger evidence.

**Acceptance:**

- 5.2.1 - Proxy, compare and native states affect only the eight `ai` rows;
  compare runs read rows only, effectful rows run once, and rollback to Proxy
  preserves state. test: `crates/gdaemon/tests/ai_cutover.rs::ai_cutover_is_scoped_and_single_effect`.
- 5.2.2 - With the Python backend unavailable, all eight rows run natively
  for grant, agent-token, API-key and cookie callers on hub and standalone,
  and a node keeps its current backend.
  test: `tests/e2e/test_ai_native.py::test_ai_rows_work_without_python_backend`.
- 5.2.3 - Readiness rejects zero-Passed, all-Skipped and unwaived-Skipped
  reports and unknown or stale waivers, and requires the exact `unused-cwd`
  entry beside ordinary `Passed` cases.
  test: `crates/gdaemon/tests/ai_cutover.rs::waiver_readiness_gates_ai_activation`.
- 5.2.4 - Shipping manifests, lock and pins agree at the required patch
  increment. test: `tests/install/test_version_pins.py::test_managed_bin_pins_match_crate_versions`.
- 5.2.5 - The runbook records build and installed hashes, compared cases,
  rollback command, announcement, health, the Security approval for 5.1 and
  native smoke evidence. behavior: "ai activation evidence" in `docs/contracts/ai-native.md`.

Planned verification: `CARGO_BUILD_JOBS=3 cargo nextest run -p gobby-daemon --test ai_cutover`;
the isolated e2e test with the protected Python prefix; scoped Ruff and mypy
with a changed-test type audit; the targeted version-pin check; an admitted
release build and Orchestrator-directed cutover. These are execution gates,
not results observed while drafting.

## P6: Native providers routes
`kind: framing`

### 6.0 Web-chat provider health seam [category: code] (depends: 1.1)
`kind: deliverable`

Targets:
- `crates/gai/src/providers/mod.rs`
- `crates/gai/src/providers/health.rs`
- `crates/gai/tests/provider_health.rs`

**Research context:** `src/gobby/servers/routes/providers.py::_provider_health`
reads `src/gobby/servers/websocket/chat/runtime_manager.py::WebChatRuntimeManager.health`,
which returns one provider's `available` and `startup_error`. The native
providers rows need the same read from #21592's native web-chat runtime, and
this plan owns that contract. `crates/gai/src/providers/health.rs` defines
`ProviderHealth { available: bool, startup_error: Option<String> }` and the
trait `WebChatHealth: Send + Sync` with `fn health(&self, provider: &str) ->
ProviderHealth`. `ProvidersFamily` in `providers/mod.rs` gains
`with_web_chat_health(Arc<dyn WebChatHealth>)`. Without a source the family
claims no row, so traffic stays proxied, and Python's absent-manager branch,
where availability is binary presence alone, never applies natively. gai
never depends on #21592's crate: 6.3 wires the runtime in gdaemon through an
adapter that implements this trait. This leaf edits no crate-root file.

**Acceptance:**

- 6.0.1 - Without a health source the providers family claims no row; with a
  stub source, each provider's `available` and `startup_error` reach the
  caller unchanged, including a `None` error and an unknown provider.
  test: `crates/gai/tests/provider_health.rs::web_chat_health_seam_contract`.

Planned verification: `CARGO_BUILD_JOBS=3 cargo nextest run -p gobby-ai --test provider_health`.

### 6.1 Native provider list and model catalog [category: code] (depends: 2.5, 6.0)
`kind: deliverable`

Targets:
- `crates/gai/src/providers/mod.rs`
- `crates/gai/src/providers/list.rs`
- `crates/gai/src/providers/models.rs`
- `crates/gai/tests/providers.rs`

**Research context:** `src/gobby/servers/routes/providers.py::create_providers_router`
serves both rows. Declare class `OperatorOrBrowser` on `GET /api/providers`
and `AgentToken { bind_identity: false }` on `GET /api/providers/models`.

`list_providers` looks up every provider binary on `PATH` concurrently
(`_probe_providers`), using the static provider table and AGY support gate
that 1.1 placed in `crates/gai/src/registry.rs`. `_provider_health` decides
availability in order: a provider without web-chat support returns its
unavailable reason; agy with no binary or an unsupported record returns the
record's reason; claude is available when its binary exists and the runtime
reports it healthy; grok, a lazy ACP provider, is available when its binary
exists and carries the runtime's startup error; every other provider takes
the runtime's health. Each row carries `name`, `available`, `path`,
`startup_error` and `_provider_metadata_fields` (`display_name`, `installed`,
`deprecated`, `deprecation_message`, `supports_web_chat`,
`supports_agent_spawn`, and `unavailable_reason`, which for agy is the
support record's reason when unsupported). `_configured_endpoint_provider_entries`
then appends one row per `chat-completions` generation endpoint:
`endpoint:<name>`, unavailable, display name `Local: <label>`, installed, no
web chat or agent spawn, and `_GENERIC_LOCAL_UNAVAILABLE_REASON`.

Runtime health comes through 6.0's `WebChatHealth` seam, the native
counterpart of `src/gobby/servers/websocket/chat/runtime_manager.py::WebChatRuntimeManager.health`;
6.3 connects #21592's runtime to it. Python's branch for an absent runtime
manager never applies natively, because without a source the family claims
nothing.

`list_provider_models` builds one entry per CLI provider with the same health
and metadata fields plus a capability payload. The payload reads the durable
last-good snapshot that `src/gobby/providers/capabilities/store.py::ProviderCapabilityStore.get_provider_snapshot`
reads from `provider_model_capabilities` and
`provider_capability_refresh_state`. With a snapshot, `_matrix_snapshot_payload`
lists the non-hidden models with `_matrix_model_entry`'s full field set and
`refresh {generation, sources}`; without one, `_pending_snapshot_payload`.
Context facts follow `src/gobby/providers/capabilities/resolve.py::CapabilityResolver.resolve_context`
with no local route: the matrix value with its provenance source key, then
the `model_metadata` table under `normalize_model_id`, then the configured
`ai.model_metadata_aliases`, reporting source `registry` for metadata and
`unknown` otherwise. Codex appends `_responses_endpoint_models` for
`responses` endpoints and sets `execution_provider: codex`. Local entries
come from `src/gobby/servers/local_provider_models.py::discover_local_endpoint_model_group`
for each `chat-completions` endpoint (LM Studio, Ollama with its native,
validated-fallback and detail probes, vLLM and generic), `_merge_default_model`,
and 2.5's `get_observation`, which adds `context_observation`,
`context_length` and `context_length_source: local_observation`.
`_local_generation_provider_entries` applies `codex_local_transport_strategy`
routability and Python's unavailable reasons: generic catalog-only,
discovery error, `NO_COMPLETION_MODELS_ERROR`, the tool-probe failure (with
the vLLM hint) from the persisted `probed_tools` flag, `_CODEX_REQUIRED_REASON`
and `_CODEX_UNHEALTHY_REASON`.

`CapabilityRefreshCoordinator` refreshes those tables in the background with
its collectors and coverage audit. That maintenance is no ledger row of this
family; this leaf reads its durable output exactly as Python does and changes
neither the writer nor the tables.

**Acceptance:**

- 6.1.1 - `HTTP GET /api/providers` matches Python for installed, missing,
  deprecated and unsupported providers, every health branch and the endpoint
  rows. test: `crates/gai/tests/providers.rs::provider_list_matches_python`.
- 6.1.2 - `HTTP GET /api/providers/models` matches Python's matrix entries,
  pending payloads, context precedence, codex responses models and
  `execution_provider`. test: `crates/gai/tests/providers.rs::provider_models_match_python`.
- 6.1.3 - Local endpoint groups match Python's discovery, routability,
  unavailable reasons, codex gating and 2.5's context projection.
  test: `crates/gai/tests/providers.rs::local_model_groups_match_python`.
- 6.1.4 - Health is read through 6.0's seam for claude, grok and the other
  providers with Python's branch rules, and the rows declare their admission
  classes.
  test: `crates/gai/tests/providers.rs::provider_health_uses_native_runtime`.

Planned verification: `CARGO_BUILD_JOBS=3 cargo nextest run -p gobby-ai --test providers`
with fake provider binaries on a temporary `PATH`, local HTTP stubs, an
isolated database and a stub 6.0 health source.

### 6.2 Native provider usage [category: code] (depends: 6.1, #23603)
`kind: deliverable`

Targets:
- `crates/gai/src/providers/mod.rs`
- `crates/gai/src/providers/usage.rs`
- `crates/gai/tests/usage.rs`

**Research context:** `src/gobby/servers/routes/providers.py::create_providers_router`
serves `GET /api/providers/{provider}/usage` from
`src/gobby/providers/capacity_service.py::ProviderCapacityService.get` and
returns `ProviderCapacitySnapshot.to_dict()`. Declare class
`OperatorOrBrowser`. Port the service:

- Storage is `src/gobby/storage/provider_capacity.py::ProviderCapacityStorage`,
  the existing `provider_capacity_snapshots` table scoped to the serving
  machine id.
- `get` normalizes the provider name (`_provider_name`), returns a persisted
  row no older than 60 seconds, and otherwise refreshes. Concurrent requests
  join one in-flight operation per provider (`_join_operation`).
- `_refresh_with_fallback`: no reporter returns unknown `no usage reporter`;
  the AGY support resolver (1.1's `ensure_agy_support` port) returns unknown
  with its reason when unsupported and otherwise supplies the source version;
  a transient refresh error returns the persisted row as `stale` or, with no
  row, unknown with the error's reason.
- The only reporter is AGY's in `src/gobby/providers/usage.py`:
  `agy -p /usage --output-format json` within 15 seconds, normalized into
  usage windows and persisted by `_refresh_and_persist`.
- #23603 owns the snapshot's `details` field (`quota_components`,
  `pane_hints`, per-component staleness of 900 seconds). Port the shape #23603
  lands.

Python's `provider capacity service unavailable` branch covers a daemon
without the service. The native family composes only with the hub pool, so
the service always exists; storage failures map through #24970's classes.
`gobby-metrics:get_provider_capacity` (#21568 item 10) keeps using Python's
service. Both writers upsert the same machine-scoped row, so their refreshes
converge on the latest snapshot.

**Acceptance:**

- 6.2.1 - Fresh, stale, missing, unsupported, no-reporter and transient
  failure cases match Python's snapshot and persisted row.
  test: `crates/gai/tests/usage.rs::usage_matches_python`.
- 6.2.2 - Concurrent requests share one refresh, and the AGY reporter's argv,
  15-second bound and parsing match Python.
  test: `crates/gai/tests/usage.rs::usage_refresh_is_single_flight`.
- 6.2.3 - The `details` field matches #23603's landed contract.
  test: `crates/gai/tests/usage.rs::usage_details_match_capacity_contract`.

Planned verification: `CARGO_BUILD_JOBS=3 cargo nextest run -p gobby-ai --test usage`
with a fake `agy` on a temporary `PATH` and an isolated database.

### 6.3 providers family activation and gdaemon release [category: code] (depends: 6.2, 5.2, #21592)
`kind: deliverable`

Targets:
- `crates/gdaemon/src/serve.rs::*` — scope-reason: pass #21592's web-chat runtime to the providers family through an adapter implementing 6.0's WebChatHealth, retaining the file-wide scope used by 1.1
- `crates/gdaemon/Cargo.toml`
- `Cargo.lock`
- `src/gobby/install/version_pins.py::MANAGED_BIN_VERSION_PINS`
- `crates/gdaemon/tests/providers_cutover.rs`
- `tests/e2e/test_providers_native.py`
- `docs/contracts/ai-native.md`

Consumers unchanged:
- `src/gobby/cli/install_setup_gclient.py` — no-edit-reason: dynamically reads its unchanged native version pin.
- `src/gobby/cli/install_setup_gcode.py` — no-edit-reason: dynamically reads its unchanged native version pin.
- `src/gobby/cli/install_setup_gdaemon.py` — no-edit-reason: dynamically reads the bumped gdaemon pin.
- `src/gobby/cli/install_setup_ghook.py` — no-edit-reason: dynamically reads its unchanged native version pin.
- `src/gobby/cli/install_setup_gmcp.py` — no-edit-reason: dynamically reads its unchanged native version pin.
- `src/gobby/cli/install_setup_gterm.py` — no-edit-reason: dynamically reads unchanged native version pins.
- `src/gobby/cli/install_setup_versions.py` — no-edit-reason: resolves every pin dynamically by binary name.
- `src/gobby/code_index/gcode_versions.py` — no-edit-reason: both gcode maintenance floors derive from the unchanged gcode pin.
- `src/gobby/hooks/runtime_compat.py` — no-edit-reason: the hook compatibility floor derives from the unchanged ghook pin.
- `src/gobby/install/bin_freshness_models.py` — no-edit-reason: enumerates the map dynamically.
- `src/gobby/install/distribution.py` — no-edit-reason: resolves minimum versions dynamically.
- `tests/cli/installers/test_grok_installer.py` — no-edit-reason: derives expected ghook versions from the map.
- `tests/cli/test_install_ghook.py` — no-edit-reason: derives expected versions from the map.
- `tests/cli/test_install_setup.py` — no-edit-reason: derives expected versions from the map.
- `tests/cli/test_install_setup_gclient.py` — no-edit-reason: derives expected versions from the map.
- `tests/cli/test_install_setup_gdaemon.py` — no-edit-reason: derives expected versions from the map.
- `tests/cli/test_install_setup_gmcp.py` — no-edit-reason: derives expected versions from the map.
- `tests/cli/test_install_setup_gterm.py` — no-edit-reason: derives expected versions from the map.
- `tests/code_index/test_gcode_gateway.py` — no-edit-reason: derives gcode expectations from the unchanged pin.
- `tests/hooks/test_runtime_compat.py` — no-edit-reason: derives ghook expectations from the unchanged pin.
- `tests/install/test_distribution.py` — no-edit-reason: dynamically parameterizes managed version expectations.
- `tests/install/test_version_pins.py` — no-edit-reason: already verifies each managed pin against its Cargo manifest.

**Research context:** Activation selects `front_door.routes.providers` through
the existing bootstrap mechanism; Proxy stays selectable for rollback. It
requires 5.2's activation and #21592's native web-chat runtime serving web
chat, because list and models read that runtime's health; without it the
family stays on Proxy. In both serve entry points, `serve.rs` wraps the
runtime's per-provider health read in an adapter that implements 6.0's
`WebChatHealth` and passes it to `ProvidersFamily::with_web_chat_health`.

Activation goes from Proxy to native without compare. All three rows read
state the serving process owns: web-chat runtime health, 2.5's process-local
context evidence, and a capacity refresh that spawns `agy` and persists a row
(S3). A side-by-side diff would measure which process answered, so it cannot
certify the native result. Proof comes from 0.1's corpus replay through the
#24963 manifest, the 6.1 and 6.2 tests and the isolated e2e test. No providers
row carries a waiver, so each needs ordinary `Passed` cases.

Since 5.2, the 6.x commits change only `crates/gai`, which only gdaemon
depends on. Switch-chain commits that may land in the same window (7.1 to
7.4) change only `crates/gai`, Python and scripts; 7.0's gcore change shipped
with 5.2, and 7.5 depends on this leaf. `src/gobby/install/bin_release_identity.py::workspace_source_identity`
hashes the root manifest and each binary's locked closure, so only gdaemon
changes identity: it takes a 0.0.1 bump with its manifest, the lock and its pin.
gcode and ghook keep their versions and re-promote at unchanged source, which
the promotion guard permits; promote the coherent trio through
`promote_workspace_binary_set` and read installed hashes after signing. Only
the Orchestrator delegates cutover, with a global announcement, a quiet
window, the source, schema and installed-set preflight, and a health report.
No push is authorized here.

**Granularity:** The targets and acceptance items belong to one
family-specific activation and release state machine. 8.1 separately records
ledger evidence.

**Acceptance:**

- 6.3.1 - Proxy and native states affect only the three providers rows, the
  family stays on Proxy without #21592's runtime, and rollback to Proxy
  restores Python serving.
  test: `crates/gdaemon/tests/providers_cutover.rs::providers_cutover_is_scoped`.
- 6.3.2 - With the Python backend unavailable, all three rows run natively
  for API-key, local operator and cookie callers, `GET /api/providers/models`
  also for agent tokens, and a node keeps its current backend.
  test: `tests/e2e/test_providers_native.py::test_provider_rows_work_without_python_backend`.
- 6.3.3 - Readiness requires ordinary `Passed` cases on each row and rejects
  zero-Passed, Skipped and any waiver citing a providers row.
  test: `crates/gdaemon/tests/providers_cutover.rs::readiness_gates_providers_activation`.
- 6.3.4 - Shipping manifests, lock and pins agree with only gdaemon bumped.
  test: `tests/install/test_version_pins.py::test_managed_bin_pins_match_crate_versions`.
- 6.3.5 - The runbook records build and installed hashes, the #21592 runtime
  check, rollback command, announcement, health and native smoke evidence.
  behavior: "providers activation evidence" in `docs/contracts/ai-native.md`.

Planned verification: `CARGO_BUILD_JOBS=3 cargo nextest run -p gobby-daemon --test providers_cutover`;
the isolated e2e test with the protected Python prefix; scoped Ruff and mypy
with a changed-test type audit; the targeted version-pin check; an admitted
release build and Orchestrator-directed cutover. These are execution gates,
not results observed while drafting.

## P7: Native embedding switch
`kind: framing`

### 7.0 gcore Qdrant alias operations [category: code]
`kind: deliverable`

Targets:
- `crates/gcore/src/qdrant.rs::*` — scope-reason: add public alias create, atomic switch, list and delete operations beside the existing collection operations
- `crates/gcore/src/qdrant/tests.rs::*` — scope-reason: add alias operation tests beside the existing module tests

**Research context:** 7.4's flip switches the `memories` and
`tool_embeddings` aliases in one Qdrant `POST /collections/aliases` request,
and its GC and abort cleanup list aliases before deleting physical
collections. gcore's Qdrant client (`crates/gcore/src/qdrant.rs`, 666 lines at
baseline) has collection operations but no alias operations, and its request
helper `qdrant_request` is private. Add four public blocking functions beside
the collection operations: create alias, atomic switch of several aliases in
one request, list aliases, and delete alias. They reuse the module's existing
URL, API-key and timeout handling, so no second Qdrant client exists. The file
stays well under the 850-line heuristic, and the tests go in the existing
`crates/gcore/src/qdrant/tests.rs`.

This leaf has no in-plan dependency, and 5.2 depends on it so the gcore change
ships in 5.2's coherent release, which bumps gdaemon, gcode and ghook anyway.
No later leaf in this plan changes gcore, so 6.3 and 7.6 release only gdaemon.

**Acceptance:**

- 7.0.1 - Alias create, list and delete round-trip against a Qdrant stub, and
  the atomic switch sends every alias action in one request.
  test: `crates/gcore/src/qdrant/tests.rs::alias_operations_round_trip`.

Planned verification: `CARGO_BUILD_JOBS=3 cargo nextest run -p gobby-core -E 'test(alias)'`
with the qdrant feature.

### 7.1 Embedding catalog contract [category: code] (depends: 1.1)
`kind: deliverable`

Targets:
- `crates/gai/contracts/embedding_catalog.v1.json`
- `scripts/generate_embedding_catalog.py`
- `src/gobby/_generated_embedding_catalog.py`
- `src/gobby/ai/embedding_catalog.py::*` — scope-reason: build the catalog table, picker list, default key and GGUF digest constants from the generated module while every public helper keeps its signature and result
- `crates/gai/src/switch/mod.rs`
- `crates/gai/src/switch/catalog.rs`
- `crates/gai/tests/catalog.rs`
- `tests/ai/test_embedding_catalog_contract.py`

Consumers unchanged:
- `src/gobby/ai/embedding_switch.py` — no-edit-reason: calls the unchanged catalog helpers.
- `src/gobby/ai/embedding_switch_runner.py` — no-edit-reason: calls the unchanged catalog helpers.
- `src/gobby/ai/embedding_switch_service.py` — no-edit-reason: calls the unchanged catalog helpers.
- `src/gobby/cli/_install_embedding_prompts.py` — no-edit-reason: reads the unchanged picker helpers.
- `src/gobby/cli/installers/embedding.py` — no-edit-reason: reads unchanged specs by key.
- `tests/ai/test_embedding_catalog.py` — no-edit-reason: pins the unchanged helper results.
- `tests/cli/installers/test_embedding_installer.py` — no-edit-reason: exercises unchanged specs.

**Research context:** `src/gobby/ai/embedding_catalog.py` holds `_CATALOG`
(lines 164-334), `PICKER_ENTRIES`, `DEFAULT_CATALOG_ID` (`nomic-v1.5-f16`),
the GGUF repository and SHA-256 constants, and helpers through
`catalog_summary` (line 430). `EmbeddingModelSpec` fields are `key`,
`label`, `dim`, `family`, `query_prefix`, `ollama_tag`, `ollama_quant_real`,
`lmstudio_ref`, `gguf_repo`, `gguf_filename`, `gguf_sha256`, `quant`,
`compatibility {ollama, lmstudio}` and `recommended`. The native switch, the
native CLI and Python's remaining consumers need one source. Follow the
existing precedent: `crates/gcore/contracts/tool_loop_limits.v1.json`,
`scripts/generate_tool_loop_limits.py` (with `--check`),
`src/gobby/_generated_tool_loop_limits.py` and
`tests/ai/test_tool_loop_contract.py`. The JSON contract carries every spec,
the picker order and the default key. The generator writes the Python module;
`crates/gai/src/switch/catalog.rs` embeds the same JSON with `include_str!`
and exposes the same lookups. The module is outside `src/gobby/config`, so no
configuration carrier changes.

**Acceptance:**

- 7.1.1 - The contract round-trips to Python data identical to the baseline
  catalog, and the generator's `--check` passes.
  test: `tests/ai/test_embedding_catalog_contract.py::test_generated_catalog_is_current`.
- 7.1.2 - Native lookups, provider resolution and `catalog_summary` output
  match Python for every key. test: `crates/gai/tests/catalog.rs::catalog_matches_python`.

Planned verification: `uv run python scripts/generate_embedding_catalog.py --check`;
the contract test and `tests/ai/test_embedding_catalog.py` with the protected
Python prefix; `CARGO_BUILD_JOBS=3 cargo nextest run -p gobby-ai --test catalog`.

### 7.2 Switch journal, coordinator and routes [category: code] (depends: 7.1, #24466)
`kind: deliverable`

Targets:
- `crates/gai/src/switch/mod.rs`
- `crates/gai/src/switch/journal.rs`
- `crates/gai/src/switch/coordinator.rs`
- `crates/gai/src/switch/routes.rs`
- `crates/gai/tests/switch_routes.rs`

**Research context:** `src/gobby/servers/routes/embeddings.py::create_embeddings_router`
serves the four rows. Status returns `SwitchOperationStatus` as `{run_id,
status, message}`. Start takes `EmbeddingSwitchPayload` (`catalog_key`
required, `provider` and `api_base` optional). Start returns 409 for
`EmbeddingSwitchTaskActive`, `SwitchAlreadyActiveError` and
`EmbeddingConfigMutationBlocked` and 400 for `ValueError`; resume returns 409
for the first and third; abort returns 409 for a blocked mutation and 409
with the status object as `detail` for `too_late`. Declare class `AgentToken
{ bind_identity: false }` on status and `OperatorOrBrowser` on the three
mutations. The 503 `Embedding switch service is unavailable` branch cannot
occur natively, because the coordinator exists wherever the family composes.

Port the journal from `src/gobby/ai/embedding_switch.py`: `SwitchJournal`,
`CompletedSwitchRecord`, the phases staging, building, flipping, active, gc
and aborted, `start_switch`, `advance_phase`, `record_switch_error`,
`persist_journal`, `complete_switch`, `load_completed_switch`,
`abort_switch`, `complete_aborted_switch` and `build_physical_names`. The
journal keys `ai.embeddings.switch_run` and `ai.embeddings.completed_switch`
live in `config_store` through the internal lifecycle API. Every journal
mutation takes `pg_advisory_xact_lock(hashtext('embedding_switch_journal_mutation'))`,
the key `src/gobby/storage/hub/postgres_pool.py::advisory_lock_keys` derives
for `EmbeddingSwitchJournalMutation`, so Python's configuration writers and
the native switch serialize, and the runtime family's secret delete and value
patch keep refusing while `switch_run` is set. `complete_switch` writes the
managed embedding keys and the completed record in one compare-and-set
revision with source `embedding_switch` through #24466's configuration write
core.

Port `src/gobby/ai/embedding_switch_service.py::EmbeddingSwitchCoordinator`:
`start` detects a missing provider with `src/gobby/utils/deps.py::fingerprint_embedding_server`
(a `ValueError` naming the `api_base` when unidentifiable), uses `api_base`
only for vLLM through `_resolve_vllm_target`, and otherwise takes the default
from `src/gobby/cli/installers/embedding.py::_PROVIDER_CONFIG`; `resume`
returns `not_found` without a journal; `abort` relaunches cleanup for an
aborted journal (`cleanup_retry`), returns `too_late` once flipping has
started, and waits `ABORT_WAIT_TIMEOUT_SECONDS` (30) before `timeout`;
`status` reports `running` for this daemon's task and otherwise the
persisted phase; `_launch` marks flipping started for the flipping, active
and gc phases. One task runs per daemon, and nothing resumes at startup
(KEEP). Python's coordinator stays in the Python daemon, but only its routes
call it, so after 7.6 it receives no request; the journal and lock make a
second start fail with `SwitchAlreadyActiveError` in either process. 7.6
moves ownership only across a restart that ends the outgoing task. The
runner is a trait here, and tests use a scripted runner as Python's
`RunnerFactory` does; 7.3 and 7.4 add the phases.

**Acceptance:**

- 7.2.1 - The four switch routes match Python's status codes, payloads and
  `too_late` detail. test: `crates/gai/tests/switch_routes.rs::switch_routes_match_python`.
- 7.2.2 - Journal rows, keys, phases, lock and completion revision
  interoperate with Python's writers.
  test: `crates/gai/tests/switch_routes.rs::journal_interoperates_with_python`.
- 7.2.3 - Start, resume, abort and status follow Python's state machine,
  including `cleanup_retry`, `too_late`, `timeout` and no startup resume.
  test: `crates/gai/tests/switch_routes.rs::coordinator_matches_python`.
- 7.2.4 - Provider detection and vLLM target resolution match Python.
  test: `crates/gai/tests/switch_routes.rs::start_resolves_provider_like_python`.

Planned verification: `CARGO_BUILD_JOBS=3 cargo nextest run -p gobby-ai --test switch_routes`
against an isolated database; the interleave case runs Python's writer with
the protected Python prefix.

### 7.3 Switch staging through provider APIs [category: code] (depends: 7.2, 2.4)
`kind: deliverable`

Targets:
- `crates/gai/src/switch/mod.rs`
- `crates/gai/src/switch/stage.rs`
- `crates/gai/tests/switch_stage.rs`
- `docs/contracts/parity-waivers.json`

**Research context:** `src/gobby/ai/embedding_switch_runner.py::EmbeddingSwitchRunner.stage`
runs provider setup, checks the target dimension with
`src/gobby/cli/installers/embedding.py::_probe_embedding_dim` (error
`Target model dimension probe failed: expected <dim>, got <actual>`), runs
`::_semantic_smoke_test`, then advances to building. Native setup follows
Decision 2 exactly: LM Studio 0.4.0 or later through its model API, Ollama
through its tag and pull API, and no setup for vLLM or generic endpoints. It
never starts a server, never runs `lms` or `ollama`, matches exact
identifiers and never unloads. Failures are typed provider API errors
recorded with `record_switch_error`, so they appear in the journal and in
status. The probe and smoke test use gcore's blocking embeddings client
through `spawn_blocking`, with keys from 1.1's sealed bindings.

Python checks abort only after staging ends, through
`src/gobby/ai/embedding_switch_runner.py::EmbeddingSwitchRunner._check_abort`
after the smoke test, and its setup runs in threads that abort cannot
interrupt. The native stage keeps that behavior. Setup, probe and smoke waits
are not cancelled, remote provider work continues after an abort, and nothing
is unloaded.

Add three waivers to `docs/contracts/parity-waivers.json`, each with decision
`.gobby/plans/gdaemon-ai-providers-family.md#7.3`:
`ai.http-embeddings-switch-start.provider-api-model-setup`,
`ai.http-embeddings-switch-resume.provider-api-model-setup` and
`ai.http-embeddings-switch-status.provider-api-model-setup`. The changed
branch is model setup: server auto-start and CLI setup with substring
matching become provider API setup with exact identifiers, so a stopped
server or an absent exact model now records a typed staging error. Depending
on 2.4 orders the shared waiver file.

**Acceptance:**

- 7.3.1 - LM Studio setup checks the exact key, reports 404 as a typed
  LM Studio 0.4.0 requirement, downloads by repository link and quantization,
  polls within 600 seconds and loads within 120 seconds unless loaded.
  test: `crates/gai/tests/switch_stage.rs::lmstudio_setup_uses_provider_api`.
- 7.3.2 - Ollama pulls an absent exact tag until `success` within 600
  seconds, vLLM and generic endpoints skip setup, and no server start, CLI or
  unload occurs. test: `crates/gai/tests/switch_stage.rs::ollama_and_passthrough_setup`.
- 7.3.3 - The dimension probe and smoke test match Python, and a failure is
  recorded while the phase stays staging.
  test: `crates/gai/tests/switch_stage.rs::stage_validation_matches_python`.
- 7.3.4 - The three model-setup cases report `Waived` with their exact
  entries, and every other case stays `Passed`.
  test: `crates/gai/tests/switch_stage.rs::model_setup_cases_are_waived`.
- 7.3.5 - An abort during LM Studio or Ollama setup, the dimension probe or
  the smoke test takes effect at the check after staging, as in Python.
  Staging runs to its end, the run moves to aborted, and nothing is unloaded.
  Abort returns `timeout` when staging outlasts the 30-second wait and
  `aborted` otherwise.
  test: `crates/gai/tests/switch_stage.rs::abort_during_staging_matches_python`.

Planned verification: `CARGO_BUILD_JOBS=3 cargo nextest run -p gobby-ai --test switch_stage`
against LM Studio, Ollama and embedding HTTP stubs.

### 7.4 Switch build, flip and garbage collection [category: code] (depends: 7.3, 7.0, #25070, #25071)
`kind: deliverable`

Targets:
- `crates/gai/src/switch/mod.rs`
- `crates/gai/src/switch/build.rs`
- `crates/gai/src/switch/flip.rs`
- `crates/gai/tests/switch_build.rs`

**Research context:** Port `EmbeddingSwitchRunner.build`, `.flip`, `.gc`,
`._replay_projection_changes` and `._cleanup_staged_collections`. Physical
collections are `{kind}@{run_id}` for `memories` and `tool_embeddings`
(`src/gobby/memory/collection_names.py`), and each alias is its kind. Build
creates each physical collection at the target dimension with gcore's
`ensure_collection`; on resume, a schema mismatch deletes and recreates it
with `collection_schema` and `delete_collection`, as Python's
`recreate_on_mismatch` does. Build keeps Python's journal-first order on the
first run and on resume. It persists `physical_names` in
`ai.embeddings.switch_run` first. Only then does it read the ledger watermark
(`src/gobby/storage/embedding_generation_state.py::EmbeddingGenerationState`)
and persist it as `caught_up_watermark`. Python producers dual-project into
the names that `src/gobby/ai/embedding_switch.py::managed_embedding_projection`
reads from that key, so publishing the names first arms dual-writes for every
change the watermark does not cover. Reversing the two writes can lose a
projection change across a crash. Build reads the ledger through a reader the
tests replace, as Python's tests replace `EmbeddingSwitchRunner.generation_state`.
It then calls `rebuild(kind, physical_name, embedder)` on #25070 (memories) and
#25071 (tool embeddings), then replays `changes_after(watermark)` through
`project_change(kind, source_id, physical_name, embedder)` for at most
`_REPLAY_MAX_PASSES` (20) passes before `EmbeddingGenerationNotCaughtUp`.
Ledger reads take `pg_advisory_xact_lock(0x474F4242, 1)`. Flip marks
flipping started, replays once more, switches both aliases in one Qdrant
`POST /collections/aliases` request through 7.0's alias switch, completes through 7.2 and enters
active. GC waits up to `_GC_WAIT_SECONDS` (120) for `can_collect`, which
fails while any live serving lease reports another generation, another
revision or no acknowledgement, then deletes only the superseded physical
collections. Abort cleanup deletes staged collections only after checking
that no live alias names them.

Alias reads and changes use 7.0's public gcore functions. The native daemon
holds no serving lease: its embedding row reads the active configuration per
request and writes no collection. Python daemons keep their leases in
`src/gobby/runner_init/embedding_lease.py`, and GC honors them.

**Acceptance:**

- 7.4.1 - Build matches Python's physical names, dimensions, mismatch
  recreation, seam calls, watermark and bounded replay.
  test: `crates/gai/tests/switch_build.rs::build_matches_python`.
- 7.4.2 - Flip performs the final replay, switches both aliases atomically
  and records the completion revision.
  test: `crates/gai/tests/switch_build.rs::flip_matches_python`.
- 7.4.3 - GC waits for compatible acknowledgements within 120 seconds and
  deletes only superseded collections, and abort cleanup spares any
  collection a live alias names.
  test: `crates/gai/tests/switch_build.rs::gc_and_abort_cleanup_match_python`.
- 7.4.4 - A resume after interruption in each phase reaches Python's end state.
  test: `crates/gai/tests/switch_build.rs::resume_matches_python_per_phase`.
- 7.4.5 - Build persists `physical_names` before it reads and persists
  `caught_up_watermark`, on the first run and on resume. A producer upsert and
  delete recorded at the watermark read are replayed. A failure injected at
  the watermark read, and another at the first `rebuild` call, each followed
  by resume, leave the target points equal to the authoritative sources.
  test: `crates/gai/tests/switch_build.rs::build_publishes_names_before_watermark`.

Planned verification: `CARGO_BUILD_JOBS=3 cargo nextest run -p gobby-ai --test switch_build`
against isolated Qdrant collections, an isolated database, stubs of the two
seams and a stub ledger reader.

### 7.5 Native embeddings command tree [category: code] (depends: 7.4, 6.3)
`kind: deliverable`

Targets:
- `crates/gai/src/switch/mod.rs`
- `crates/gai/src/switch/cli.rs`
- `crates/gdaemon/src/main.rs::Command`
- `crates/gdaemon/src/main.rs::main`
- `crates/gai/tests/switch_cli.rs`
- `docs/contracts/parity-waivers.json`

**Research context:** `src/gobby/cli/embeddings.py` defines the Click group
`embeddings` and its commands `catalog`, `doctor` and `switch`, which are the
rows `CLI gobby embeddings`, `CLI gobby embeddings catalog`, `CLI gobby
embeddings doctor` and `CLI gobby embeddings switch`. `gdaemon embeddings` is
the non-ledger internal dispatcher for that existing public contract. It adds
no public surface, and 7.7 makes `gobby embeddings` forward to it. Clap
parsing and output live in `crates/gai/src/switch/cli.rs`. Add one
`Embeddings` variant to `crates/gdaemon/src/main.rs::Command`; `main`
dispatches it and exits with the command's status. The command's display name
is `gobby embeddings`, and clap help and usage templates reproduce Click's
text: usage lines, docstrings including the switch examples block, option help
and the command list. Help exits 0. A usage error prints Click's usage line,
its `Try '... --help' for help.` hint and `Error: <message>` to stderr and
exits 2, and an unknown command prints Click's `No such command` error. 0.1's
corpus holds the exact bytes.

JSON output reproduces Python's `json.dumps(indent=2, sort_keys=True)`:
two-space indentation, `,` line ends, `": "` key separators, keys sorted at
every level, non-ASCII escaped as `\uXXXX` and floats in `float.__repr__`
form. `crates/gruntime/src/config.rs::PythonJson` is the compact precedent;
it is private and unindented, so `cli.rs` holds the indented form. `catalog`
prints 7.1's `catalog_summary` locally in that form.

`switch [CATALOG_KEY]` takes `--status`, `--resume`, `--abort`, `--provider`
and `--api-base`. More than one action flag is the usage error `Choose only
one of --status, --resume, or --abort`. With no action flag and no key, it
prints `Error: catalog_key is required to start a switch.` to stderr and
`Available keys: gobby embeddings catalog` to stdout and exits 1. Otherwise it
sends `GET /api/embeddings/switch/status`, `POST /api/embeddings/switch/abort`,
`POST /api/embeddings/switch/resume` or `POST /api/embeddings/switch/start`
with `{catalog_key, provider, api_base}` including nulls, under a 30-second
timeout. The base URL comes from `crates/gcore/src/daemon_url.rs::daemon_url`,
the resolver every Gobby binary shares with Python's
`gobby.utils.daemon_url.daemon_url`. The bearer comes from
`crates/gcore/src/local_token.rs::read_api_key` and `::authorization_bearer`,
as `crates/gcode/src/commands/embeddings_doctor.rs::fetch_daemon_peer` does.
`read_api_key` follows the precedence of
`src/gobby/utils/local_token.py::daemon_auth_headers`: managed capability,
`GOBBY_AGENT_API_TOKEN`, the `managed capability is unavailable` refusal under
a managed bootstrap, then the bootstrap key. The refusal prints `Error:
managed capability is unavailable` and exits 1, as Python's caught
`RuntimeError` does. With no bootstrap key the request carries no
`Authorization` header, as Python's does. Python also sends agent identity
headers beside an agent token; none of the four routes binds identity, so
native sends the bearer alone.

Responses follow `src/gobby/cli/embeddings.py::switch` and
`src/gobby/utils/daemon_client.py::DaemonClient.call_http_api`. A 401 prints
`Error: Daemon authentication failed: API key missing or stale; run 'gobby
auth login' to provision this machine's bootstrap key` and exits 1. Any other
status of 400 or above prints `Error: <detail>` and exits 1. The detail is a
JSON object's `detail` field (the body text when absent), any other non-empty
JSON value, or the body text. A non-string detail prints in Python's `str`
form (single-quoted strings, `None`, `True`), as the `too_late` abort's
status-object detail does. A successful JSON body prints in the JSON form
above, a non-JSON body prints verbatim, and a JSON object whose `status` is
`failed` exits 1 after printing. A connection failure or timeout prints
`Error: <message>` and exits 1.

`doctor` runs `<gobby home>/bin/gcode embeddings doctor`, with the home from
`crates/gcore/src/lib.rs::gobby_home`, in the caller's working directory and
environment with inherited streams, and exits with its status: 0 healthy, 10
configuration missing, 11 drift and 20 transport (`EXIT_HEALTHY`,
`EXIT_CONFIG_MISSING`, `EXIT_DRIFT` and `EXIT_TRANSPORT` in
`crates/gcode/src/commands/embeddings_doctor.rs`). A missing or unexecutable
gcode prints `Error: cannot run <path>: <error>` and exits 1. gcode resolves
embedding configuration itself and fills `agrees` and `drift` from the daemon
peer.

Add three waivers to `docs/contracts/parity-waivers.json`, each with decision
`.gobby/plans/gdaemon-ai-providers-family.md#7.5`:
`ai.cli-embeddings-switch.provider-api-model-setup` (a start against a stopped
server or an absent exact model prints 7.3's typed staging error in the
status), `ai.cli-embeddings-doctor.gcode-report` (gcode's report and exit
codes replace Python's local report) and
`ai.cli-embeddings-switch.transport-error-message` (an `Error:` line replaces
Python's uncaught traceback, and exit 1 stays). The tests replay 0.1's four
CLI rows through `cli.rs`'s entry point against 7.2's routes on an isolated
database with stubs, so the CLI rows' readiness is proven here. Depending on
7.4 orders the shared waiver file and `switch/mod.rs`. Depending on 6.3 means
the command reaches users only through 7.6's gdaemon release.

**Acceptance:**

- 7.5.1 - `catalog` prints 7.1's summary byte-identical to Python's output.
  test: `crates/gai/tests/switch_cli.rs::catalog_output_matches_python`.
- 7.5.2 - `switch` matches Python's flags, usage error, missing-key messages,
  requests, 30-second timeout, credential precedence, 401 message, `Error:
  <detail>` forms, JSON and verbatim output and the `failed` exit, and the
  model-setup case reports `Waived` with its exact entry.
  test: `crates/gai/tests/switch_cli.rs::switch_cli_matches_python`.
- 7.5.3 - A connection failure and a timeout print `Error: <message>` and exit
  1, reported `Waived` with the transport entry.
  test: `crates/gai/tests/switch_cli.rs::switch_transport_failure_is_waived`.
- 7.5.4 - `doctor` runs the home's gcode with inherited streams and passes
  exits 0, 10, 11 and 20 through, a missing gcode exits 1, and the case
  reports `Waived` with the doctor entry.
  test: `crates/gai/tests/switch_cli.rs::doctor_forwards_to_gcode`.
- 7.5.5 - Group and command help, unknown commands and usage errors match
  Click's text and exit codes.
  test: `crates/gai/tests/switch_cli.rs::group_matches_python`.
- 7.5.6 - Readiness over the four CLI rows requires ordinary `Passed` cases
  beside exactly the three CLI waivers and rejects unknown, stale or missing
  entries. test: `crates/gai/tests/switch_cli.rs::cli_readiness_requires_exact_waivers`.

Planned verification: `CARGO_BUILD_JOBS=3 cargo nextest run -p gobby-ai --test switch_cli`
against an isolated database, the HTTP stubs and a fake gcode;
`CARGO_BUILD_JOBS=3 cargo build -p gobby-daemon` for the dispatcher.

### 7.6 ai-embedding-switch activation and gdaemon release [category: code] (depends: 7.5, 6.3)
`kind: deliverable`

Targets:
- `crates/gdaemon/Cargo.toml`
- `Cargo.lock`
- `src/gobby/install/version_pins.py::MANAGED_BIN_VERSION_PINS`
- `crates/gdaemon/tests/embedding_switch_cutover.rs`
- `tests/e2e/test_embedding_switch_native.py`
- `docs/contracts/ai-native.md`

Consumers unchanged:
- `src/gobby/cli/install_setup_gclient.py` — no-edit-reason: dynamically reads its unchanged native version pin.
- `src/gobby/cli/install_setup_gcode.py` — no-edit-reason: dynamically reads its unchanged native version pin.
- `src/gobby/cli/install_setup_gdaemon.py` — no-edit-reason: dynamically reads the bumped gdaemon pin.
- `src/gobby/cli/install_setup_ghook.py` — no-edit-reason: dynamically reads its unchanged native version pin.
- `src/gobby/cli/install_setup_gmcp.py` — no-edit-reason: dynamically reads its unchanged native version pin.
- `src/gobby/cli/install_setup_gterm.py` — no-edit-reason: dynamically reads unchanged native version pins.
- `src/gobby/cli/install_setup_versions.py` — no-edit-reason: resolves every pin dynamically by binary name.
- `src/gobby/code_index/gcode_versions.py` — no-edit-reason: both gcode maintenance floors derive from the unchanged gcode pin.
- `src/gobby/hooks/runtime_compat.py` — no-edit-reason: the hook compatibility floor derives from the unchanged ghook pin.
- `src/gobby/install/bin_freshness_models.py` — no-edit-reason: enumerates the map dynamically.
- `src/gobby/install/distribution.py` — no-edit-reason: resolves minimum versions dynamically.
- `tests/cli/installers/test_grok_installer.py` — no-edit-reason: derives expected ghook versions from the map.
- `tests/cli/test_install_ghook.py` — no-edit-reason: derives expected versions from the map.
- `tests/cli/test_install_setup.py` — no-edit-reason: derives expected versions from the map.
- `tests/cli/test_install_setup_gclient.py` — no-edit-reason: derives expected versions from the map.
- `tests/cli/test_install_setup_gdaemon.py` — no-edit-reason: derives expected versions from the map.
- `tests/cli/test_install_setup_gmcp.py` — no-edit-reason: derives expected versions from the map.
- `tests/cli/test_install_setup_gterm.py` — no-edit-reason: derives expected versions from the map.
- `tests/code_index/test_gcode_gateway.py` — no-edit-reason: derives gcode expectations from the unchanged pin.
- `tests/hooks/test_runtime_compat.py` — no-edit-reason: derives ghook expectations from the unchanged pin.
- `tests/install/test_distribution.py` — no-edit-reason: dynamically parameterizes managed version expectations.
- `tests/install/test_version_pins.py` — no-edit-reason: already verifies each managed pin against its Cargo manifest.

**Research context:** Activation selects
`front_door.routes.ai-embedding-switch` through the existing bootstrap
mechanism; Proxy stays selectable for rollback. It requires #25070 and #25071,
which 7.4 consumes.

Activation goes from Proxy to native without compare. Status reports
`running` only for the serving daemon's own task, and the three mutations
start, resume or cancel that task and write the journal (S3). A side-by-side
run would start a second run or measure which process answered. Proof comes
from 0.1's corpus replay through the #24963 manifest, the 7.2 to 7.4 tests and
the isolated e2e test. Readiness for the four HTTP rows requires the three
exact HTTP waivers from 7.3 beside ordinary `Passed` cases. The four CLI rows
are not route-family rows. 7.5 proves their readiness with the three CLI
waivers, and they become native when 7.7's launcher forwards to the gdaemon
this leaf releases. The e2e test also drives the released `gdaemon
embeddings` against the native routes, so the binary 7.7 forwards to is
proven at release. After activation, Python's coordinator receives no
request, and the journal lock still makes a second start fail in either
process.

Activation and rollback hand switch ownership across a restart, so only one
coordinator can hold a switch task at a time. gdaemon reads
`front_door.routes` once, when
`crates/gdaemon/src/serve.rs::serve_with_runtime` builds the `RouteTable`, so
each flip edits `~/.gobby/bootstrap.yaml` and restarts gdaemon. On shutdown,
`crates/gdaemon/src/lifecycle/backend.rs::BackendSupervisor::supervise`
drains the Python backend through `BackendChild::drain` (SIGTERM, the grace
window, SIGKILL, reap) before gdaemon exits. That ends Python's in-process
task, and the native task ends with the gdaemon process. Neither coordinator
resumes at startup (7.2, KEEP). The flip and the rollback each start only
when `GET /api/embeddings/switch/status` reports no `running` task. A live run
first finishes at `active`, or is aborted. Once abort returns `too_late`, the
run completes forward and the flip waits for `active`. This check belongs to
the quiet window, and no lease is added. If a run is interrupted anyway, the
incoming owner treats the journal as Python does after any restart. Status
reports the persisted phase. Abort returns `not_found` unless the phase is
aborted, which relaunches cleanup as `cleanup_retry`. Resume relaunches the
run once from its phase through 7.4.4's path.

7.5 depends on 6.3, so this release always carries 7.5's command and every
7.x change made after 6.3. Those commits change only `crates/gai`, gdaemon's
command dispatcher, Python and scripts, and 7.0's gcore change shipped with
5.2. `src/gobby/install/bin_release_identity.py::workspace_source_identity`
ignores test files, so only gdaemon changes identity: it takes a 0.0.1 bump
with its manifest, the lock and its pin. gcode and ghook keep their versions
and re-promote at unchanged source, which the promotion guard permits; promote
the coherent trio through `promote_workspace_binary_set` and read installed
hashes after signing. Only the Orchestrator delegates cutover, with a global
announcement, a quiet window, the source, schema and installed-set preflight,
and a health report. No push is authorized here.

**Granularity:** The targets and acceptance items belong to one
family-specific activation and release state machine. 7.7 separately switches
the public CLI entry, and 8.1 records ledger evidence.

**Acceptance:**

- 7.6.1 - Proxy and native states affect only the four HTTP switch rows,
  other `/api/embeddings` paths stay with `ai`, and rollback to Proxy restores
  Python serving with the journal intact.
  test: `crates/gdaemon/tests/embedding_switch_cutover.rs::switch_cutover_is_scoped`.
- 7.6.2 - With the Python backend unavailable, the four HTTP rows run natively
  for API-key, local operator and cookie callers and status also for agent
  tokens, the released `gdaemon embeddings` drives a switch from start to
  active against isolated Qdrant and an embedding stub, and a node keeps its
  current backend.
  test: `tests/e2e/test_embedding_switch_native.py::test_switch_rows_work_without_python_backend`.
- 7.6.3 - Readiness requires the exact three HTTP waivers beside ordinary
  `Passed` cases and rejects zero-Passed, Skipped, unknown and stale waivers.
  test: `crates/gdaemon/tests/embedding_switch_cutover.rs::readiness_gates_switch_activation`.
- 7.6.4 - Shipping manifests, lock and pins agree with only gdaemon bumped.
  test: `tests/install/test_version_pins.py::test_managed_bin_pins_match_crate_versions`.
- 7.6.5 - The runbook records build and installed hashes, rollback command,
  announcement, the pre-flip switch status, health and native smoke evidence.
  behavior: "embedding switch activation evidence" in `docs/contracts/ai-native.md`.
- 7.6.6 - This case bypasses the no-`running` precondition to prove
  recovery. On an isolated daemon, a Proxy-owned run held in staging,
  building or flipping is interrupted by the flip restart. The old backend process is
  gone before the new daemon answers. Native status reports the persisted
  phase, abort returns `not_found`, and the embedding stub and Qdrant receive
  no request from the old run. One native resume then reaches `active`. The
  rollback restart does the same for a held native run, with Python resuming
  it. Each run creates one physical collection set.
  test: `tests/e2e/test_embedding_switch_native.py::test_route_flip_ends_the_outgoing_switch_run`.

Planned verification: `CARGO_BUILD_JOBS=3 cargo nextest run -p gobby-daemon --test embedding_switch_cutover`;
the isolated e2e test with the protected Python prefix; scoped Ruff and mypy
with a changed-test type audit; the targeted version-pin check; an admitted
release build and Orchestrator-directed cutover. These are execution gates,
not results observed while drafting.

### 7.7 Forward the public embeddings CLI [category: code] (depends: 7.6)
`kind: deliverable`

Targets:
- `src/gobby/cli/embeddings.py::*` — scope-reason: replace the complete command implementation with a literal argument-forwarding Click entry point
- `tests/cli/test_embeddings_doctor.py::*` — operation: delete — scope-reason: retire the local doctor-report tests whose expectations move to the corpus and 7.5's native tests
- `tests/cli/test_lifecycle_daemon_commands.py::*` — scope-reason: remove the embeddings import and the three switch tests rehomed to the forwarding file while the project purge tests and shared fake-client helpers stay unchanged
- `tests/cli/test_embeddings_native_forwarding.py`

Consumers unchanged:
- `src/gobby/cli/__init__.py` — no-edit-reason: its lazy `"embeddings": ("embeddings", "embeddings")` registration loads the forwarding command unchanged.

**Research context:** Literal sweeps for `gobby.cli.embeddings` and
`cli/embeddings.py` found two importers, both tests:
`tests/cli/test_embeddings_doctor.py` imports the module for
`_doctor_payload` and its exit constants, and
`tests/cli/test_lifecycle_daemon_commands.py` imports the `embeddings` group
for three switch tests. `src/gobby/cli/__init__.py` registers the group lazily
by name. After 7.6 releases gdaemon with `gdaemon embeddings`, replace the
module with a Click command named `embeddings` that disables option parsing
and help for forwarded arguments. It runs the gdaemon that
`src/gobby/utils/native_bin.py::resolve_native_bin` resolves, with
`embeddings` and the literal arguments, in the caller's working directory and
environment with inherited streams, and exits with its exact status. When
resolution fails, it prints `gdaemon is missing; reinstall the managed native
binary set` to stderr and exits 1, as `src/gobby/cli/daemon_start.py` does.
The root `--config` option feeds only Python's local doctor report, which
7.5's doctor waiver replaces, so the launcher forwards nothing from it. The
module imports no catalog, configuration, storage or HTTP client module.
Landing after 7.6 means `gobby embeddings` never forwards to an installed
gdaemon that lacks the command, and the pin 7.6 bumps refreshes stale
installs.

Delete `tests/cli/test_embeddings_doctor.py`. Its redaction and exit-10
expectations are Python-report behaviors that the doctor waiver replaces, and
0.1's corpus keeps them as the Python baseline. Remove the three switch tests
and the `embeddings` import from `tests/cli/test_lifecycle_daemon_commands.py`.
Their daemon-only, non-JSON and unreachable-daemon expectations live in 7.5's
`switch_cli_matches_python` and `switch_transport_failure_is_waived`, and the
forwarding file keeps launcher integration coverage. The fake-client helpers
stay for the purge tests.

**Acceptance:**

- 7.7.1 - The launcher forwards literal arguments, working directory,
  environment, streams and exit status, including help, unknown options,
  Unicode and errors, to the resolved gdaemon, reports a missing binary with
  exit 1, and imports no embeddings business module.
  test: `tests/cli/test_embeddings_native_forwarding.py::test_launcher_only_forwards`.
- 7.7.2 - Every retired Python test expectation maps to 0.1's corpus and a 7.5
  native test, and import and patch-string sweeps find no reference to the
  removed helpers.
  test: `tests/cli/test_embeddings_native_forwarding.py::test_retired_cli_tests_have_native_coverage`.

Planned verification: the forwarding tests and
`tests/cli/test_lifecycle_daemon_commands.py` with the protected Python prefix
and a fake gdaemon; scoped Ruff and mypy with a changed-test type audit.

## P8: Ledger record
`kind: framing`

### 8.1 Publish AI family parity ownership [category: docs] (depends: 7.7)
`kind: deliverable`

Targets:
- `docs/contracts/parity-ledger-status.d/remainder.json::*` — scope-reason: update only the 16 AI family status records and retain every other record
- `docs/contracts/parity-ledger-status.d/daemon-config-auth-infra.json::*` — scope-reason: update only the three providers status records and retain every other record
- `docs/contracts/parity-ledger.md`
- `docs/contracts/ai-native.md`
- `docs/guides/llm-features.md`

**Research context:** All 19 rows are `proxied` today: 16 in
`docs/contracts/parity-ledger-status.d/remainder.json` and the three
providers rows in `docs/contracts/parity-ledger-status.d/daemon-config-auth-infra.json`.
`scripts/generate_parity_ledger.py` accepts `proxied`, `delegated` and
`dropped by decision #N`; objects carry exactly `status` and `note`. After
5.2, 6.3, 7.6 and 7.7, every row is served natively. Set each of the 19 to
`delegated` with a note citing its owning leaf, its native tests and any
waiver ID, following `.gobby/plans/completed/native-ws-transport.md`'s ledger
step. Regenerate `docs/contracts/parity-ledger.md` with `uv run python
scripts/generate_parity_ledger.py`; a second run leaves it unchanged.
`docs/contracts/ai-native.md` gains the final row ownership map, and
`docs/guides/llm-features.md` says that gdaemon serves the AI routes, the
providers routes and the embedding switch natively, that `gobby embeddings`
forwards to `gdaemon embeddings`, and that the doctor report comes from
gcode, with its last-verified date updated.

**Acceptance:**

- 8.1.1 - The 16 AI rows in the remainder shard are `delegated` with notes
  citing their leaf, tests and waiver IDs, and every other record is
  unchanged. behavior: "16 delegated AI parity rows" in `docs/contracts/parity-ledger-status.d/remainder.json`.
- 8.1.2 - The three providers rows are `delegated` with notes citing 6.1,
  6.2 and their tests, and every other record is unchanged.
  behavior: "3 delegated providers parity rows" in `docs/contracts/parity-ledger-status.d/daemon-config-auth-infra.json`.
- 8.1.3 - The regenerated ledger matches the shards and the public inventory.
  test: `tests/test_parity_ledger.py::test_committed_ledger_matches_current_public_inventory`.
- 8.1.4 - The guide describes native AI operation, the forwarding CLI and the
  gcode doctor report. behavior: "native AI operation" in `docs/guides/llm-features.md`.

Planned verification: `uv run python scripts/generate_parity_ledger.py` run
twice with no second-run diff; `tests/test_parity_ledger.py` with the
protected Python prefix.

## E1 End-to-end verification
`kind: verification`

The acceptance items own all 19 rows: 1.1 owns `GET /api/llm/vision/status`,
`GET /api/embeddings/status` and `GET /api/embeddings/doctor`; 1.2 owns `POST
/api/embeddings`; 2.4 owns `POST /api/llm/generate`; 3.3 owns `POST
/api/llm/chat/completions`; 4.1 owns `POST /api/llm/vision/extract`; 4.2 owns
`GET /api/llm/status`; 6.1 owns `GET /api/providers` and `GET
/api/providers/models`; 6.2 owns `GET /api/providers/{provider}/usage`; 7.2
owns the four switch routes; 7.5 owns the four CLI rows, and 7.7 forwards the
public entry to them. 0.1 rejects missing or extra keys. 5.1 owns admission
binding and the Security approval gate. 2.5 supports 6.1's model groups, 6.0
owns the web-chat health seam 6.1 reads and 6.3 connects to #21592's runtime,
and 7.0 supports 7.4's flip and cleanup. 5.2, 6.3 and 7.6 activate the three route
families, and 8.1 records the delegated ledger state. 7.6 moves switch
ownership only across a restart that ends the outgoing task, and 7.4 keeps
Python's journal-first build order.

The #24963 readiness gate requires ordinary `Passed` cases and exactly cited,
native-asserted `Waived` cases for the seven waivers, and fails skipped-only
and unwaived reports. #24970 supplies the shared error classes and adapters.
Shared target files are dependency-ordered throughout, and no section leaves
a product choice to its executor.

Draft validation command:
`uv run gobby plans validate .gobby/plans/gdaemon-ai-providers-family.md -p /Users/josh/Projects/gobby`.
Expansion validation is unrun for the narrative draft and becomes mandatory
after the Adversary derives and applies M1. No implementation tests,
production cutover or task expansion have been performed during planning.

## V1 Plan Changelog
`kind: framing`

- 2026-10-25 — Initial narrative by Plan Writer gobby#17712 for #25066;
  awaiting adversarial review by gobby#17713; no consensus claimed.
- 2026-10-25 — Consensus between Plan Writer gobby#17712 and Plan Adversary
  gobby#17713 on narrative 27c530f985032d0e7e550bbce63abaa740c45ffc. The
  revisions applied the Orchestrator's rulings: leaf 6.0 owns the web-chat
  health seam, `CapabilityRefreshCoordinator` stays a Python non-goal, and the
  `crates/gdaemon/src/main.rs` contention with the skills plan is recorded.
  AI-ADV-01 is resolved: 7.6 moves switch ownership only across a restart that
  ends the outgoing task, with the e2e recovery case 7.6.6. AI-ADV-02 is
  resolved: 7.4 keeps the journal-first build order, tested by 7.4.5. Enhancer
  dispositions: ENH-001 folded into AI-ADV-01, ENH-002 added test 7.3.5 only,
  ENH-003 rejected, ENH-004 added wording on 4.1.2 and 4.1.3, and ENH-F1-001
  rejected the restart gate and labelled 7.6.6. No findings or decisions
  remain open.

## M1 Task Manifest
`kind: manifest`

```yaml
- title: Executable 19-row characterization corpus
  category: test
  task_type: feature
  depends_on: []
  validation_criteria: '0.1.1: The corpus inventory equals the exact 19-key ledger
    predicate, every key has a success case and a representative failure case, and
    a key outside the family is rejected. test: `tests/contracts/test_ai_parity_corpus.py::test_inventory_matches_ai_ledger`.

    0.1.2: Running the collector twice against reset isolated state gives the same
    normalized fixtures, ordering, stdout, stderr, exit codes and recorded stub effects.
    test: `tests/contracts/test_ai_parity_corpus.py::test_collection_is_reproducible`.

    0.1.3: Each of the 15 HTTP rows records the full admission matrix, including `missing_grant`
    401, `forged_identity` 401 and `stale_epoch` 409 on the eight grant rows. test:
    `tests/contracts/test_ai_parity_corpus.py::test_http_admission_matrix`.

    0.1.4: Each of the seven changed behaviors resolves to one exact-surface test
    waiver with explicit native assertions, while untouched cases on the same surface
    stay `Passed`. test: `tests/contracts/test_ai_parity_corpus.py::test_debt_waivers_are_behavior_scoped`.

    0.1.5: Skipped-only reports, unwaived `Skipped`, stale or missing waiver decisions
    and an unrelated injected regression fail readiness even beside a valid `Waived`
    case. test: `tests/contracts/test_ai_parity_corpus.py::test_waiver_does_not_hide_unrelated_diff`.'
  labels:
  - covers:gdaemon-ai-providers-family:0.1:0.1.1
  - covers:gdaemon-ai-providers-family:0.1:0.1.2
  - covers:gdaemon-ai-providers-family:0.1:0.1.3
  - covers:gdaemon-ai-providers-family:0.1:0.1.4
  - covers:gdaemon-ai-providers-family:0.1:0.1.5
  tdd: false
  source_section: '0.1'
  assigned_agent: developer
- title: Crate, three family shells, configuration snapshot and status reads
  category: code
  task_type: feature
  depends_on:
  - '0.1'
  validation_criteria: '1.1.1: `HTTP GET /api/embeddings/status` matches Python''s
    payload for configured, unconfigured, cloud-default and endpoint cases. test:
    `crates/gai/tests/status.rs::embeddings_status_matches_python`.

    1.1.2: `HTTP GET /api/embeddings/doctor` matches Python and keeps the `endpoint`,
    `model` and `dim` fields gcode''s peer report reads. test: `crates/gai/tests/status.rs::embeddings_doctor_matches_python`.

    1.1.3: `HTTP GET /api/llm/vision/status` matches Python''s capability status,
    including endpoint image modality and the unavailable codex, grok and droid bindings.
    test: `crates/gai/tests/status.rs::vision_status_matches_python`.

    1.1.4: The registry builds the same bindings, order, availability and reasons
    as Python for every non-audio capability across the fixture configurations. test:
    `crates/gai/tests/status.rs::registry_matches_python_builder`.

    1.1.5: Each family claims only its own implemented rows, the longest prefix routes
    switch paths to `ai-embedding-switch`, absent configuration keeps all three on
    Proxy, a node composes none, and every claim fails closed until admission is bound.
    test: `crates/gai/tests/status.rs::families_are_scoped_and_fail_closed`.'
  labels:
  - covers:gdaemon-ai-providers-family:1.1:1.1.1
  - covers:gdaemon-ai-providers-family:1.1:1.1.2
  - covers:gdaemon-ai-providers-family:1.1:1.1.3
  - covers:gdaemon-ai-providers-family:1.1:1.1.4
  - covers:gdaemon-ai-providers-family:1.1:1.1.5
  tdd: true
  source_section: '1.1'
  implementation_domain: backend
- title: Native embedding generation
  category: code
  task_type: feature
  depends_on:
  - '1.1'
  validation_criteria: '1.2.1: `HTTP POST /api/embeddings` matches Python for string
    and list input, query and document prefixes, the default model and the reserved
    fields. test: `crates/gai/tests/embed.rs::embed_matches_python`.

    1.2.2: Unconfigured, unavailable, provider-error and malformed-response cases
    match Python''s status and envelope, with `error` as the capability-unavailable
    key. test: `crates/gai/tests/embed.rs::embed_errors_match_python`.'
  labels:
  - covers:gdaemon-ai-providers-family:1.2:1.2.1
  - covers:gdaemon-ai-providers-family:1.2:1.2.2
  tdd: true
  source_section: '1.2'
  implementation_domain: backend
- title: Feature-profile candidate contract
  category: code
  task_type: feature
  depends_on:
  - '1.2'
  validation_criteria: '2.1.1: The contract equals `DEFAULT_PROFILE_CANDIDATES` in
    order, candidates and reasoning pins, and a changed entry on either side fails.
    test: `tests/ai/test_profile_candidates_contract.py::test_contract_matches_python_defaults`.

    2.1.2: The Rust loader returns the same three ordered lists and rejects a contract
    with an unknown profile or version. test: `crates/gai/tests/profiles.rs::profile_candidates_match_contract`.'
  labels:
  - covers:gdaemon-ai-providers-family:2.1:2.1.1
  - covers:gdaemon-ai-providers-family:2.1:2.1.2
  tdd: true
  source_section: '2.1'
  implementation_domain: backend
- title: Text-generation service and endpoint adapters
  category: code
  task_type: feature
  depends_on:
  - '2.1'
  validation_criteria: '2.2.1: Candidate order matches Python for explicit candidates,
    provider and model, configured and contract profile defaults, and a bare provider.
    test: `crates/gai/tests/generation.rs::candidate_order_matches_python`.

    2.2.2: Fallback, aggregate errors and the circuit breaker''s open, skip and recovery
    behavior match Python. test: `crates/gai/tests/generation.rs::fallback_and_breaker_match_python`.

    2.2.3: Per-candidate and total timeouts and the cold-spawn concurrency limit match
    Python. test: `crates/gai/tests/generation.rs::timeouts_and_cold_spawn_limit_match_python`.

    2.2.4: The reasoning gate, prompt-echo rejection and image filter match Python.
    test: `crates/gai/tests/generation.rs::request_gates_match_python`.

    2.2.5: Endpoint adapters match Python''s wire requests and parsed results for
    every protocol, vLLM `auto`, images, usage and key handling. test: `crates/gai/tests/generation.rs::endpoint_adapters_match_python`.'
  labels:
  - covers:gdaemon-ai-providers-family:2.2:2.2.1
  - covers:gdaemon-ai-providers-family:2.2:2.2.2
  - covers:gdaemon-ai-providers-family:2.2:2.2.3
  - covers:gdaemon-ai-providers-family:2.2:2.2.4
  - covers:gdaemon-ai-providers-family:2.2:2.2.5
  tdd: true
  source_section: '2.2'
  implementation_domain: backend
- title: CLI provider adapters
  category: code
  task_type: feature
  depends_on:
  - '2.2'
  validation_criteria: '2.3.1: The runner reproduces Python''s environment, working
    directory, session, exit-error text, timeout and process-group cleanup, and leaves
    no orphan process. test: `crates/gai/tests/cli_adapters.rs::runner_isolation_and_cleanup_match_python`.

    2.3.2: The Claude adapter matches the SDK''s recorded argv, environment, stream-json
    exchange, images, usage, retries and blank-output error. test: `crates/gai/tests/cli_adapters.rs::claude_adapter_matches_python`.

    2.3.3: The Codex adapter matches Python, including responses-wire endpoints, and
    the endpoint key appears only in the child environment. test: `crates/gai/tests/cli_adapters.rs::codex_adapter_matches_python`.

    2.3.4: The Droid adapter matches Python''s argv, isolated home and seeding. test:
    `crates/gai/tests/cli_adapters.rs::droid_adapter_matches_python`.

    2.3.5: The Grok adapter matches Python''s argv and parsing. test: `crates/gai/tests/cli_adapters.rs::grok_adapter_matches_python`.

    2.3.6: The Agy adapter matches Python''s argv, display-name resolution and parsing.
    test: `crates/gai/tests/cli_adapters.rs::agy_adapter_matches_python`.'
  labels:
  - covers:gdaemon-ai-providers-family:2.3:2.3.1
  - covers:gdaemon-ai-providers-family:2.3:2.3.2
  - covers:gdaemon-ai-providers-family:2.3:2.3.3
  - covers:gdaemon-ai-providers-family:2.3:2.3.4
  - covers:gdaemon-ai-providers-family:2.3:2.3.5
  - covers:gdaemon-ai-providers-family:2.3:2.3.6
  tdd: true
  source_section: '2.3'
  implementation_domain: backend
- title: Native text generation route
  category: code
  task_type: feature
  depends_on:
  - '2.3'
  validation_criteria: '2.4.1: `HTTP POST /api/llm/generate` matches Python''s routing,
    defaults, timeout caps, images and response fields. test: `crates/gai/tests/generate.rs::generate_matches_python`.

    2.4.2: Unavailable, invalid, failed and unconfigured requests match Python''s
    status and `code` envelope. test: `crates/gai/tests/generate.rs::generate_errors_match_python`.

    2.4.3: The `cwd` case reports `Waived` with its exact entry and decision, and
    every other case on the row stays `Passed`. test: `crates/gai/tests/generate.rs::unused_cwd_is_waived`.'
  labels:
  - covers:gdaemon-ai-providers-family:2.4:2.4.1
  - covers:gdaemon-ai-providers-family:2.4:2.4.2
  - covers:gdaemon-ai-providers-family:2.4:2.4.3
  tdd: true
  source_section: '2.4'
  implementation_domain: backend
- title: Local endpoint context observations
  category: code
  task_type: feature
  depends_on:
  - '2.4'
  validation_criteria: '2.5.1: Endpoint routes, fingerprints and refresh keys match
    Python for every protocol and configuration change. test: `crates/gai/tests/local_context.rs::endpoint_routes_match_python`.

    2.5.2: Each collector produces Python''s observation, diagnostics and effective
    limit from recorded stub responses. test: `crates/gai/tests/local_context.rs::collectors_match_python`.

    2.5.3: Stored rows, namespace, generation and lock interleave with Python''s writer
    without lost or torn snapshots. test: `crates/gai/tests/local_context.rs::store_interoperates_with_python`.

    2.5.4: Shared in-flight refresh, supersession, invalidation and the process-local
    `get_observation` rule match Python. test: `crates/gai/tests/local_context.rs::refresh_service_matches_python`.

    2.5.5: Endpoint generation refreshes before each request, and a refresh failure
    is logged without failing generation. test: `crates/gai/tests/local_context.rs::generation_refresh_is_best_effort`.'
  labels:
  - covers:gdaemon-ai-providers-family:2.5:2.5.1
  - covers:gdaemon-ai-providers-family:2.5:2.5.2
  - covers:gdaemon-ai-providers-family:2.5:2.5.3
  - covers:gdaemon-ai-providers-family:2.5:2.5.4
  - covers:gdaemon-ai-providers-family:2.5:2.5.5
  tdd: true
  source_section: '2.5'
  implementation_domain: backend
- title: Read-only gcode tool runtime and endpoint tool loop
  category: code
  task_type: feature
  depends_on:
  - '2.5'
  validation_criteria: '3.1.1: Policy validation matches Python for read-only, allowed,
    nested and unknown tools and for every rejected metacharacter. test: `crates/gai/tests/tool_runtime.rs::policy_matches_python`.

    3.1.2: gcode execution matches Python''s argv, working directory, stripped environment,
    timeout, stdout cap and stderr tail. test: `crates/gai/tests/tool_runtime.rs::gcode_execution_matches_python`.

    3.1.3: The endpoint loop matches Python''s turns, tool results, limits and stop
    reasons through `run_tool_loop`. test: `crates/gai/tests/tool_runtime.rs::endpoint_tool_loop_matches_python`.'
  labels:
  - covers:gdaemon-ai-providers-family:3.1:3.1.1
  - covers:gdaemon-ai-providers-family:3.1:3.1.2
  - covers:gdaemon-ai-providers-family:3.1:3.1.3
  tdd: true
  source_section: '3.1'
  implementation_domain: backend
- title: Provider tool-chat styles
  category: code
  task_type: feature
  depends_on:
  - '3.1'
  validation_criteria: '3.2.1: The loopback MCP server matches Python''s protocol
    version, bearer check, request bound, loopback bind, budgets and teardown. test:
    `crates/gai/tests/tool_chat_styles.rs::loopback_mcp_server_matches_python`.

    3.2.2: The Claude style exposes the `repo` tools and matches Python''s turns,
    stop reasons and investigation fields. test: `crates/gai/tests/tool_chat_styles.rs::claude_style_matches_python`.

    3.2.3: The Codex app-server style matches Python''s JSON-RPC exchange and dynamic
    tool results. test: `crates/gai/tests/tool_chat_styles.rs::codex_style_matches_python`.

    3.2.4: The Droid style matches Python''s stream exchange and MCP use. test: `crates/gai/tests/tool_chat_styles.rs::droid_style_matches_python`.

    3.2.5: The Grok ACP style matches Python''s session and policy. test: `crates/gai/tests/tool_chat_styles.rs::grok_style_matches_python`.

    3.2.6: Fallback happens only on capability unavailability, and one deadline bounds
    every style. test: `crates/gai/tests/tool_chat_styles.rs::fallback_and_deadline_match_python`.'
  labels:
  - covers:gdaemon-ai-providers-family:3.2:3.2.1
  - covers:gdaemon-ai-providers-family:3.2:3.2.2
  - covers:gdaemon-ai-providers-family:3.2:3.2.3
  - covers:gdaemon-ai-providers-family:3.2:3.2.4
  - covers:gdaemon-ai-providers-family:3.2:3.2.5
  - covers:gdaemon-ai-providers-family:3.2:3.2.6
  tdd: true
  source_section: '3.2'
  implementation_domain: backend
- title: Native chat completions with managed tool credentials
  category: code
  task_type: feature
  depends_on:
  - '3.2'
  validation_criteria: '3.3.1: `HTTP POST /api/llm/chat/completions` matches Python''s
    request validation, message split, response, `finish_reason` map and investigation
    fields. test: `crates/gai/tests/chat.rs::chat_completions_match_python`.

    3.3.2: Each run issues one managed tool credential bound to the session and checked
    project root, the child receives it, and it is revoked on success, failure and
    cancellation; an overlay mismatch refuses before any spawn. test: `crates/gai/tests/chat.rs::managed_tool_credential_lifecycle`.

    3.3.3: Missing session, unavailable capability and adapter failure match Python''s
    status and body. test: `crates/gai/tests/chat.rs::chat_errors_match_python`.'
  labels:
  - covers:gdaemon-ai-providers-family:3.3:3.3.1
  - covers:gdaemon-ai-providers-family:3.3:3.3.2
  - covers:gdaemon-ai-providers-family:3.3:3.3.3
  tdd: true
  source_section: '3.3'
  implementation_domain: backend
- title: Native vision extraction
  category: code
  task_type: feature
  depends_on:
  - '3.3'
  validation_criteria: '4.1.1: `HTTP POST /api/llm/vision/extract` matches Python
    for Claude, endpoint and responses-wire bindings. test: `crates/gai/tests/vision.rs::extract_matches_python`.

    4.1.2: Upload bounds, content types, unavailable bindings and adapter failures
    match Python''s status and body, and an over-limit or interrupted upload leaves
    no file. test: `crates/gai/tests/vision.rs::upload_bounds_and_errors_match_python`.

    4.1.3: Temporary files are private and created exclusively under an allowlisted
    suffix, so a traversal-style filename never selects a path. They are deleted after
    every request outcome and swept after 24 hours, and the sweep removes a symlink
    without touching its target. test: `crates/gai/tests/vision.rs::temp_files_are_private_and_swept`.'
  labels:
  - covers:gdaemon-ai-providers-family:4.1:4.1.1
  - covers:gdaemon-ai-providers-family:4.1:4.1.2
  - covers:gdaemon-ai-providers-family:4.1:4.1.3
  tdd: true
  source_section: '4.1'
  implementation_domain: backend
- title: Full capability snapshot with Voice audio bindings
  category: code
  task_type: feature
  depends_on:
  - '4.1'
  validation_criteria: '4.2.1: `HTTP GET /api/llm/status` matches Python''s full snapshot
    across the fixture configurations. test: `crates/gai/tests/llm_status.rs::llm_status_matches_python`.

    4.2.2: Audio bindings follow the Voice seam''s answer and Python''s configuration
    rules, with matching reasons when unavailable. test: `crates/gai/tests/llm_status.rs::audio_bindings_use_voice_seam`.'
  labels:
  - covers:gdaemon-ai-providers-family:4.2:4.2.1
  - covers:gdaemon-ai-providers-family:4.2:4.2.2
  tdd: true
  source_section: '4.2'
  implementation_domain: backend
- title: 'Native admission binding and the #25068 contract'
  category: code
  task_type: feature
  depends_on:
  - '4.2'
  validation_criteria: '5.1.1: The eight `ai` rows declare exactly Python''s grant
    capability, mode and effectful flag, and grant callers are admitted or refused
    with Python''s codes before any effect. test: `crates/gai/tests/admission.rs::grant_rows_match_python`.

    5.1.2: Agent tokens with matching, mismatched, revoked and expired identity get
    Python''s outcomes for both `bind_identity` settings. test: `crates/gai/tests/admission.rs::agent_token_classes_match_python`.

    5.1.3: API keys, the local operator and ordinary cookies keep Python''s per-route
    permissions, and managed cookies are refused on mutations. test: `crates/gai/tests/admission.rs::operator_and_browser_classes_match_python`.

    5.1.4: Without a bound Admitted value the row stays proxied, unavailable authority
    returns 503, and no second verifier exists in the crate. test: `crates/gai/tests/admission.rs::admission_fails_closed_without_shared_seam`.

    5.1.5: The admission record names the persisted `security-review` label, the pre-dispatch
    check and the approved step for the exact SHA; absent or stale approval holds
    native admission. behavior: "AI admission security gate" in `docs/contracts/ai-native.md`.'
  labels:
  - covers:gdaemon-ai-providers-family:5.1:5.1.1
  - covers:gdaemon-ai-providers-family:5.1:5.1.2
  - covers:gdaemon-ai-providers-family:5.1:5.1.3
  - covers:gdaemon-ai-providers-family:5.1:5.1.4
  - covers:gdaemon-ai-providers-family:5.1:5.1.5
  tdd: true
  source_section: '5.1'
  implementation_domain: backend
- title: ai family activation and coherent release
  category: code
  task_type: feature
  depends_on:
  - '5.1'
  - '7.0'
  validation_criteria: '5.2.1: Proxy, compare and native states affect only the eight
    `ai` rows; compare runs read rows only, effectful rows run once, and rollback
    to Proxy preserves state. test: `crates/gdaemon/tests/ai_cutover.rs::ai_cutover_is_scoped_and_single_effect`.

    5.2.2: With the Python backend unavailable, all eight rows run natively for grant,
    agent-token, API-key and cookie callers on hub and standalone, and a node keeps
    its current backend. test: `tests/e2e/test_ai_native.py::test_ai_rows_work_without_python_backend`.

    5.2.3: Readiness rejects zero-Passed, all-Skipped and unwaived-Skipped reports
    and unknown or stale waivers, and requires the exact `unused-cwd` entry beside
    ordinary `Passed` cases. test: `crates/gdaemon/tests/ai_cutover.rs::waiver_readiness_gates_ai_activation`.

    5.2.4: Shipping manifests, lock and pins agree at the required patch increment.
    test: `tests/install/test_version_pins.py::test_managed_bin_pins_match_crate_versions`.

    5.2.5: The runbook records build and installed hashes, compared cases, rollback
    command, announcement, health, the Security approval for 5.1 and native smoke
    evidence. behavior: "ai activation evidence" in `docs/contracts/ai-native.md`.'
  labels:
  - covers:gdaemon-ai-providers-family:5.2:5.2.1
  - covers:gdaemon-ai-providers-family:5.2:5.2.2
  - covers:gdaemon-ai-providers-family:5.2:5.2.3
  - covers:gdaemon-ai-providers-family:5.2:5.2.4
  - covers:gdaemon-ai-providers-family:5.2:5.2.5
  tdd: true
  source_section: '5.2'
  implementation_domain: backend
- title: Web-chat provider health seam
  category: code
  task_type: feature
  depends_on:
  - '1.1'
  validation_criteria: '6.0.1: Without a health source the providers family claims
    no row; with a stub source, each provider''s `available` and `startup_error` reach
    the caller unchanged, including a `None` error and an unknown provider. test:
    `crates/gai/tests/provider_health.rs::web_chat_health_seam_contract`.'
  labels:
  - covers:gdaemon-ai-providers-family:6.0:6.0.1
  tdd: true
  source_section: '6.0'
  implementation_domain: backend
- title: Native provider list and model catalog
  category: code
  task_type: feature
  depends_on:
  - '2.5'
  - '6.0'
  validation_criteria: '6.1.1: `HTTP GET /api/providers` matches Python for installed,
    missing, deprecated and unsupported providers, every health branch and the endpoint
    rows. test: `crates/gai/tests/providers.rs::provider_list_matches_python`.

    6.1.2: `HTTP GET /api/providers/models` matches Python''s matrix entries, pending
    payloads, context precedence, codex responses models and `execution_provider`.
    test: `crates/gai/tests/providers.rs::provider_models_match_python`.

    6.1.3: Local endpoint groups match Python''s discovery, routability, unavailable
    reasons, codex gating and 2.5''s context projection. test: `crates/gai/tests/providers.rs::local_model_groups_match_python`.

    6.1.4: Health is read through 6.0''s seam for claude, grok and the other providers
    with Python''s branch rules, and the rows declare their admission classes. test:
    `crates/gai/tests/providers.rs::provider_health_uses_native_runtime`.'
  labels:
  - covers:gdaemon-ai-providers-family:6.1:6.1.1
  - covers:gdaemon-ai-providers-family:6.1:6.1.2
  - covers:gdaemon-ai-providers-family:6.1:6.1.3
  - covers:gdaemon-ai-providers-family:6.1:6.1.4
  tdd: true
  source_section: '6.1'
  implementation_domain: backend
- title: Native provider usage
  category: code
  task_type: feature
  depends_on:
  - '6.1'
  validation_criteria: '6.2.1: Fresh, stale, missing, unsupported, no-reporter and
    transient failure cases match Python''s snapshot and persisted row. test: `crates/gai/tests/usage.rs::usage_matches_python`.

    6.2.2: Concurrent requests share one refresh, and the AGY reporter''s argv, 15-second
    bound and parsing match Python. test: `crates/gai/tests/usage.rs::usage_refresh_is_single_flight`.

    6.2.3: The `details` field matches #23603''s landed contract. test: `crates/gai/tests/usage.rs::usage_details_match_capacity_contract`.'
  labels:
  - covers:gdaemon-ai-providers-family:6.2:6.2.1
  - covers:gdaemon-ai-providers-family:6.2:6.2.2
  - covers:gdaemon-ai-providers-family:6.2:6.2.3
  tdd: true
  source_section: '6.2'
  implementation_domain: backend
- title: providers family activation and gdaemon release
  category: code
  task_type: feature
  depends_on:
  - '6.2'
  - '5.2'
  validation_criteria: '6.3.1: Proxy and native states affect only the three providers
    rows, the family stays on Proxy without #21592''s runtime, and rollback to Proxy
    restores Python serving. test: `crates/gdaemon/tests/providers_cutover.rs::providers_cutover_is_scoped`.

    6.3.2: With the Python backend unavailable, all three rows run natively for API-key,
    local operator and cookie callers, `GET /api/providers/models` also for agent
    tokens, and a node keeps its current backend. test: `tests/e2e/test_providers_native.py::test_provider_rows_work_without_python_backend`.

    6.3.3: Readiness requires ordinary `Passed` cases on each row and rejects zero-Passed,
    Skipped and any waiver citing a providers row. test: `crates/gdaemon/tests/providers_cutover.rs::readiness_gates_providers_activation`.

    6.3.4: Shipping manifests, lock and pins agree with only gdaemon bumped. test:
    `tests/install/test_version_pins.py::test_managed_bin_pins_match_crate_versions`.

    6.3.5: The runbook records build and installed hashes, the #21592 runtime check,
    rollback command, announcement, health and native smoke evidence. behavior: "providers
    activation evidence" in `docs/contracts/ai-native.md`.'
  labels:
  - covers:gdaemon-ai-providers-family:6.3:6.3.1
  - covers:gdaemon-ai-providers-family:6.3:6.3.2
  - covers:gdaemon-ai-providers-family:6.3:6.3.3
  - covers:gdaemon-ai-providers-family:6.3:6.3.4
  - covers:gdaemon-ai-providers-family:6.3:6.3.5
  tdd: true
  source_section: '6.3'
  implementation_domain: backend
- title: gcore Qdrant alias operations
  category: code
  task_type: feature
  depends_on: []
  validation_criteria: '7.0.1: Alias create, list and delete round-trip against a
    Qdrant stub, and the atomic switch sends every alias action in one request. test:
    `crates/gcore/src/qdrant/tests.rs::alias_operations_round_trip`.'
  labels:
  - covers:gdaemon-ai-providers-family:7.0:7.0.1
  tdd: true
  source_section: '7.0'
  implementation_domain: backend
- title: Embedding catalog contract
  category: code
  task_type: feature
  depends_on:
  - '1.1'
  validation_criteria: '7.1.1: The contract round-trips to Python data identical to
    the baseline catalog, and the generator''s `--check` passes. test: `tests/ai/test_embedding_catalog_contract.py::test_generated_catalog_is_current`.

    7.1.2: Native lookups, provider resolution and `catalog_summary` output match
    Python for every key. test: `crates/gai/tests/catalog.rs::catalog_matches_python`.'
  labels:
  - covers:gdaemon-ai-providers-family:7.1:7.1.1
  - covers:gdaemon-ai-providers-family:7.1:7.1.2
  tdd: true
  source_section: '7.1'
  implementation_domain: backend
- title: Switch journal, coordinator and routes
  category: code
  task_type: feature
  depends_on:
  - '7.1'
  validation_criteria: '7.2.1: The four switch routes match Python''s status codes,
    payloads and `too_late` detail. test: `crates/gai/tests/switch_routes.rs::switch_routes_match_python`.

    7.2.2: Journal rows, keys, phases, lock and completion revision interoperate with
    Python''s writers. test: `crates/gai/tests/switch_routes.rs::journal_interoperates_with_python`.

    7.2.3: Start, resume, abort and status follow Python''s state machine, including
    `cleanup_retry`, `too_late`, `timeout` and no startup resume. test: `crates/gai/tests/switch_routes.rs::coordinator_matches_python`.

    7.2.4: Provider detection and vLLM target resolution match Python. test: `crates/gai/tests/switch_routes.rs::start_resolves_provider_like_python`.'
  labels:
  - covers:gdaemon-ai-providers-family:7.2:7.2.1
  - covers:gdaemon-ai-providers-family:7.2:7.2.2
  - covers:gdaemon-ai-providers-family:7.2:7.2.3
  - covers:gdaemon-ai-providers-family:7.2:7.2.4
  tdd: true
  source_section: '7.2'
  implementation_domain: backend
- title: Switch staging through provider APIs
  category: code
  task_type: feature
  depends_on:
  - '7.2'
  - '2.4'
  validation_criteria: '7.3.1: LM Studio setup checks the exact key, reports 404 as
    a typed LM Studio 0.4.0 requirement, downloads by repository link and quantization,
    polls within 600 seconds and loads within 120 seconds unless loaded. test: `crates/gai/tests/switch_stage.rs::lmstudio_setup_uses_provider_api`.

    7.3.2: Ollama pulls an absent exact tag until `success` within 600 seconds, vLLM
    and generic endpoints skip setup, and no server start, CLI or unload occurs. test:
    `crates/gai/tests/switch_stage.rs::ollama_and_passthrough_setup`.

    7.3.3: The dimension probe and smoke test match Python, and a failure is recorded
    while the phase stays staging. test: `crates/gai/tests/switch_stage.rs::stage_validation_matches_python`.

    7.3.4: The three model-setup cases report `Waived` with their exact entries, and
    every other case stays `Passed`. test: `crates/gai/tests/switch_stage.rs::model_setup_cases_are_waived`.

    7.3.5: An abort during LM Studio or Ollama setup, the dimension probe or the smoke
    test takes effect at the check after staging, as in Python. Staging runs to its
    end, the run moves to aborted, and nothing is unloaded. Abort returns `timeout`
    when staging outlasts the 30-second wait and `aborted` otherwise. test: `crates/gai/tests/switch_stage.rs::abort_during_staging_matches_python`.'
  labels:
  - covers:gdaemon-ai-providers-family:7.3:7.3.1
  - covers:gdaemon-ai-providers-family:7.3:7.3.2
  - covers:gdaemon-ai-providers-family:7.3:7.3.3
  - covers:gdaemon-ai-providers-family:7.3:7.3.4
  - covers:gdaemon-ai-providers-family:7.3:7.3.5
  tdd: true
  source_section: '7.3'
  implementation_domain: backend
- title: Switch build, flip and garbage collection
  category: code
  task_type: feature
  depends_on:
  - '7.3'
  - '7.0'
  validation_criteria: '7.4.1: Build matches Python''s physical names, dimensions,
    mismatch recreation, seam calls, watermark and bounded replay. test: `crates/gai/tests/switch_build.rs::build_matches_python`.

    7.4.2: Flip performs the final replay, switches both aliases atomically and records
    the completion revision. test: `crates/gai/tests/switch_build.rs::flip_matches_python`.

    7.4.3: GC waits for compatible acknowledgements within 120 seconds and deletes
    only superseded collections, and abort cleanup spares any collection a live alias
    names. test: `crates/gai/tests/switch_build.rs::gc_and_abort_cleanup_match_python`.

    7.4.4: A resume after interruption in each phase reaches Python''s end state.
    test: `crates/gai/tests/switch_build.rs::resume_matches_python_per_phase`.

    7.4.5: Build persists `physical_names` before it reads and persists `caught_up_watermark`,
    on the first run and on resume. A producer upsert and delete recorded at the watermark
    read are replayed. A failure injected at the watermark read, and another at the
    first `rebuild` call, each followed by resume, leave the target points equal to
    the authoritative sources. test: `crates/gai/tests/switch_build.rs::build_publishes_names_before_watermark`.'
  labels:
  - covers:gdaemon-ai-providers-family:7.4:7.4.1
  - covers:gdaemon-ai-providers-family:7.4:7.4.2
  - covers:gdaemon-ai-providers-family:7.4:7.4.3
  - covers:gdaemon-ai-providers-family:7.4:7.4.4
  - covers:gdaemon-ai-providers-family:7.4:7.4.5
  tdd: true
  source_section: '7.4'
  implementation_domain: backend
- title: Native embeddings command tree
  category: code
  task_type: feature
  depends_on:
  - '7.4'
  - '6.3'
  validation_criteria: '7.5.1: `catalog` prints 7.1''s summary byte-identical to Python''s
    output. test: `crates/gai/tests/switch_cli.rs::catalog_output_matches_python`.

    7.5.2: `switch` matches Python''s flags, usage error, missing-key messages, requests,
    30-second timeout, credential precedence, 401 message, `Error: <detail>` forms,
    JSON and verbatim output and the `failed` exit, and the model-setup case reports
    `Waived` with its exact entry. test: `crates/gai/tests/switch_cli.rs::switch_cli_matches_python`.

    7.5.3: A connection failure and a timeout print `Error: <message>` and exit 1,
    reported `Waived` with the transport entry. test: `crates/gai/tests/switch_cli.rs::switch_transport_failure_is_waived`.

    7.5.4: `doctor` runs the home''s gcode with inherited streams and passes exits
    0, 10, 11 and 20 through, a missing gcode exits 1, and the case reports `Waived`
    with the doctor entry. test: `crates/gai/tests/switch_cli.rs::doctor_forwards_to_gcode`.

    7.5.5: Group and command help, unknown commands and usage errors match Click''s
    text and exit codes. test: `crates/gai/tests/switch_cli.rs::group_matches_python`.

    7.5.6: Readiness over the four CLI rows requires ordinary `Passed` cases beside
    exactly the three CLI waivers and rejects unknown, stale or missing entries. test:
    `crates/gai/tests/switch_cli.rs::cli_readiness_requires_exact_waivers`.'
  labels:
  - covers:gdaemon-ai-providers-family:7.5:7.5.1
  - covers:gdaemon-ai-providers-family:7.5:7.5.2
  - covers:gdaemon-ai-providers-family:7.5:7.5.3
  - covers:gdaemon-ai-providers-family:7.5:7.5.4
  - covers:gdaemon-ai-providers-family:7.5:7.5.5
  - covers:gdaemon-ai-providers-family:7.5:7.5.6
  tdd: true
  source_section: '7.5'
  implementation_domain: backend
- title: ai-embedding-switch activation and gdaemon release
  category: code
  task_type: feature
  depends_on:
  - '7.5'
  - '6.3'
  validation_criteria: '7.6.1: Proxy and native states affect only the four HTTP switch
    rows, other `/api/embeddings` paths stay with `ai`, and rollback to Proxy restores
    Python serving with the journal intact. test: `crates/gdaemon/tests/embedding_switch_cutover.rs::switch_cutover_is_scoped`.

    7.6.2: With the Python backend unavailable, the four HTTP rows run natively for
    API-key, local operator and cookie callers and status also for agent tokens, the
    released `gdaemon embeddings` drives a switch from start to active against isolated
    Qdrant and an embedding stub, and a node keeps its current backend. test: `tests/e2e/test_embedding_switch_native.py::test_switch_rows_work_without_python_backend`.

    7.6.3: Readiness requires the exact three HTTP waivers beside ordinary `Passed`
    cases and rejects zero-Passed, Skipped, unknown and stale waivers. test: `crates/gdaemon/tests/embedding_switch_cutover.rs::readiness_gates_switch_activation`.

    7.6.4: Shipping manifests, lock and pins agree with only gdaemon bumped. test:
    `tests/install/test_version_pins.py::test_managed_bin_pins_match_crate_versions`.

    7.6.5: The runbook records build and installed hashes, rollback command, announcement,
    the pre-flip switch status, health and native smoke evidence. behavior: "embedding
    switch activation evidence" in `docs/contracts/ai-native.md`.

    7.6.6: This case bypasses the no-`running` precondition to prove recovery. On
    an isolated daemon, a Proxy-owned run held in staging, building or flipping is
    interrupted by the flip restart. The old backend process is gone before the new
    daemon answers. Native status reports the persisted phase, abort returns `not_found`,
    and the embedding stub and Qdrant receive no request from the old run. One native
    resume then reaches `active`. The rollback restart does the same for a held native
    run, with Python resuming it. Each run creates one physical collection set. test:
    `tests/e2e/test_embedding_switch_native.py::test_route_flip_ends_the_outgoing_switch_run`.'
  labels:
  - covers:gdaemon-ai-providers-family:7.6:7.6.1
  - covers:gdaemon-ai-providers-family:7.6:7.6.2
  - covers:gdaemon-ai-providers-family:7.6:7.6.3
  - covers:gdaemon-ai-providers-family:7.6:7.6.4
  - covers:gdaemon-ai-providers-family:7.6:7.6.5
  - covers:gdaemon-ai-providers-family:7.6:7.6.6
  tdd: true
  source_section: '7.6'
  implementation_domain: backend
- title: Forward the public embeddings CLI
  category: code
  task_type: feature
  depends_on:
  - '7.6'
  validation_criteria: '7.7.1: The launcher forwards literal arguments, working directory,
    environment, streams and exit status, including help, unknown options, Unicode
    and errors, to the resolved gdaemon, reports a missing binary with exit 1, and
    imports no embeddings business module. test: `tests/cli/test_embeddings_native_forwarding.py::test_launcher_only_forwards`.

    7.7.2: Every retired Python test expectation maps to 0.1''s corpus and a 7.5 native
    test, and import and patch-string sweeps find no reference to the removed helpers.
    test: `tests/cli/test_embeddings_native_forwarding.py::test_retired_cli_tests_have_native_coverage`.'
  labels:
  - covers:gdaemon-ai-providers-family:7.7:7.7.1
  - covers:gdaemon-ai-providers-family:7.7:7.7.2
  tdd: true
  source_section: '7.7'
  implementation_domain: backend
- title: Publish AI family parity ownership
  category: docs
  task_type: feature
  depends_on:
  - '7.7'
  validation_criteria: '8.1.1: The 16 AI rows in the remainder shard are `delegated`
    with notes citing their leaf, tests and waiver IDs, and every other record is
    unchanged. behavior: "16 delegated AI parity rows" in `docs/contracts/parity-ledger-status.d/remainder.json`.

    8.1.2: The three providers rows are `delegated` with notes citing 6.1, 6.2 and
    their tests, and every other record is unchanged. behavior: "3 delegated providers
    parity rows" in `docs/contracts/parity-ledger-status.d/daemon-config-auth-infra.json`.

    8.1.3: The regenerated ledger matches the shards and the public inventory. test:
    `tests/test_parity_ledger.py::test_committed_ledger_matches_current_public_inventory`.

    8.1.4: The guide describes native AI operation, the forwarding CLI and the gcode
    doctor report. behavior: "native AI operation" in `docs/guides/llm-features.md`.'
  labels:
  - covers:gdaemon-ai-providers-family:8.1:8.1.1
  - covers:gdaemon-ai-providers-family:8.1:8.1.2
  - covers:gdaemon-ai-providers-family:8.1:8.1.3
  - covers:gdaemon-ai-providers-family:8.1:8.1.4
  tdd: false
  source_section: '8.1'
  assigned_agent: tech-writer
```
