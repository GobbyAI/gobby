# vLLM Parity Fixes

**Plan ID:** vllm-parity-fixes

Canonical plan artifact. Mirror: ~/.claude/plans/let-s-plan-fixes-so-composed-penguin.md (plan-mode display copy).

## Overview
`kind: framing`

vllm-metal is installed and serving locally (text `mlx-community/Qwen2.5-3B-Instruct-4bit`
on :8321 with `--enable-auto-tool-choice --tool-call-parser hermes`; vision
`mlx-community/Qwen3-VL-4B-Instruct-4bit` on :8322). Gobby endpoints `vllm` and
`vllm-vision` are activated (config revision 20). Goal: every surface that works
with LM Studio / Ollama works with vLLM. First-pass testing found two blockers
and several gaps; this plan fixes them, then runs the full parity matrix.

Root causes already confirmed:
- `/api/providers/models` returns zero `endpoint:*` groups because
  `src/gobby/servers/routes/providers.py:274` reads `server.services.config`, an
  attribute removed by #19980; the live config is the `HTTPServer.config`
  property. Same dead lookup in `src/gobby/servers/routes/memory.py:391`. The
  route-test stub fakes the removed attribute, hiding the bug.
- vLLM 400s every `tools` request unless started with
  `--enable-auto-tool-choice --tool-call-parser <parser>` (model-family
  specific; hermes for Qwen). The activation probe records `probed_tools: false`
  but swallows the error (bare `except`, no logger in the module), the activate
  route omits `probed_tools` from its response, and web chat / spawn never
  consult it — a flagless vLLM looks healthy and fails on the first Codex turn.
  Live-verified that the probe's named `tool_choice` does 400 flagless, so the
  probe itself needs no change.

## Constraints
`kind: framing`

Decision Record (confirmed):
- Tool-probe failure reason: activate response + WARNING log only; NO persisted
  config field (`GenerationEndpointConfig` stays as-is).
- Picker gating applies ONLY to the failure case (`probed_tools is False`);
  healthy endpoints are untouched. No runtime refusal in
  `runtime_manager`/spawn (out of scope).
- `feature_llm_call` success flips to INFO for all features (revertable).
- Embeddings: `--provider vllm` is a switch/install-time strategy; persisted
  config stays generic (api_base + served-id model + dim). No
  `ai.embeddings.provider` field, no Rust `runtime_config_contract.json` change.
- Embedding provider identity comes from server fingerprinting (Ollama answers
  `GET /api/tags`; LM Studio answers `GET /api/v1/models`; vLLM `/v1/models`
  entries carry `owned_by: "vllm"`; else openai-compatible), replacing the
  `:1234` / `:11434` port-string checks and the switch runner's
  "unknown → ollama" default.
- `gobby status` endpoint health is probed daemon-side inside
  `/api/admin/status` (one `GET {origin}/v1/models` per configured endpoint,
  1.5 s cap, gathered, non-fatal); never reuse full model discovery there.
- Agent-spawn parity leg runs with `isolation: "worktree"`.
- No backward compatibility (0.5.0 unshipped). Least mechanism.
- Out of scope: persisting probe diagnostics; runtime web-chat/spawn refusal on
  `probed_tools: false`; metrics/spans for feature calls; candidate-pin flags
  beyond `gwiki code`; vllm auto-detection by port (impossible — no fixed port).

Operational prerequisites for P6 (no code): restart the :8322 vision server with
`--enable-auto-tool-choice --tool-call-parser hermes`; grant for `/api/llm`
comes from the Rust client cache (`~/.gobby/grants/...`) or
`POST /api/runtime/handshake`.

## P1: Provider listing repair
`kind: framing`

**Goal**: `endpoint:*` groups reappear in `/api/providers` and
`/api/providers/models` from the live runtime config.

### 1.1 Fix dead `services.config` reads and silent discovery failures [category: code]
`kind: deliverable`

Targets:
- `src/gobby/servers/routes/providers.py::_configured_endpoints`
- `src/gobby/servers/routes/memory.py::entity_graph`
- `src/gobby/servers/local_provider_models.py::*` — scope-reason: add a module logger and WARNING in the silent discovery fallback
- `tests/servers/routes/test_servers_routes_providers.py::*` — scope-reason: retire the `_server_stub` fixture shape that fakes the removed `services.config` attribute across every endpoint-group test

In `_configured_endpoints` (providers.py:270-281) read the live config via the
`HTTPServer.config` property — `config = getattr(server, "config", None)` —
instead of `server.services.config` (removed by #19980; `ServiceContainer` has
only `config_runtime`). Apply the same repair to the `ui` config read in
`entity_graph` (memory.py:391). In `discover_local_endpoint_model_group`'s
`except Exception` fallback (local_provider_models.py:76-84) add a module logger
and log the swallowed discovery error at WARNING (endpoint name + short error);
the group is still returned with `source="config"`.

Rework `_server_stub` in the providers route tests so the stub exposes `config`
on the server object and its `services` container has NO `config` attribute
(matching production `ServiceContainer`); keep `cast(HTTPServer, ...)` but the
fixture must fail against the pre-fix code. These are the only two
`services.config` readers left in `src/`.

**Acceptance:**

- 1.1.1 - Providers route reads endpoint config from the live server config;
  with configured endpoints, `/api/providers/models` returns `endpoint:<name>`
  groups. symbol: `_configured_endpoints`. file: `src/gobby/servers/routes/providers.py`.
- 1.1.2 - Memory route no longer reads `services.config`. symbol: `entity_graph`.
- 1.1.3 - Discovery failure logs one WARNING naming the endpoint. symbol: `discover_local_endpoint_model_group`.
- 1.1.4 - Route test asserts endpoint groups appear using a production-shaped
  stub (server-level `config`, `services` without `config`). test: `tests/servers/routes/test_servers_routes_providers.py::test_list_providers_includes_configured_local_endpoints`.

## P2: Tool-probe diagnostics and picker gate
`kind: framing`

**Goal**: a flagless vLLM is diagnosable at activation and invisible to the
web-chat picker, with the fix named.

### 2.1 Surface degraded-probe diagnostics from endpoint activation [category: code] (depends: P1)
`kind: deliverable`

Targets:
- `src/gobby/ai/endpoint_activation.py::EndpointActivationResult`
- `src/gobby/ai/endpoint_activation.py::_activation_result`
- `src/gobby/ai/endpoint_activation.py::_sanitized_activation_error`
- `src/gobby/ai/endpoint_activation.py::probe_chat_completions_endpoint`
- `src/gobby/ai/endpoint_activation.py::probe_responses_endpoint`
- `src/gobby/agents/local_model.py::*` — scope-reason: add the `VLLM_TOOL_CALLING_HINT` module constant beside the existing vLLM helpers and export it

Add module logger + `_redact(message, api_key)` helper (api key →
`[REDACTED]`). `EndpointActivationResult` gains
`diagnostics: dict[str, str] = field(default_factory=dict)` (keys `"json"`,
`"tools"`, `"vision"`); `_activation_result` passes it through. Each degraded
`except Exception:` branch in both probe chains binds the exception, stores the
redacted message via a `_degraded_probe(endpoint_name, probe, exc, api_key)`
helper, and logs it at WARNING ("activation continues without it"). Give
`_sanitized_activation_error` a `transport` parameter so the chat-completions
path stops reporting "Responses endpoint activation failed". Chat chain keeps
`endpoint_name` (drop the `del`). New constant in `local_model.py`:
`VLLM_TOOL_CALLING_HINT = "start vLLM with --enable-auto-tool-choice --tool-call-parser <parser> (hermes for Qwen models), then re-activate the endpoint"`.

Test seam: make `_FakeCompletions` raise a configurable exception (replacing the
fixed `succeed` RuntimeError) in `tests/ai/test_endpoint_activation.py`; extend
`test_probe_outcome_table` with the real vLLM 400 text and diagnostics
assertions; new `test_degraded_tool_probe_warning_is_redacted` (caplog; secret
absent, `[REDACTED]` present) and
`test_chat_text_probe_failure_names_chat_transport`.

**Acceptance:**

- 2.1.1 - Tool-probe failure captures the redacted server error in
  `diagnostics["tools"]` and logs one WARNING. symbol: `probe_chat_completions_endpoint`.
- 2.1.2 - Fatal chat-path text-probe errors say "Chat-completions", with the
  Responses wording preserved on the responses path. symbol: `_sanitized_activation_error`.
- 2.1.3 - Probe outcome table covers diagnostics for json/tools/vision
  degradation and empty diagnostics on success/skip.
  test: `tests/ai/test_endpoint_activation.py::test_probe_outcome_table`.
- 2.1.4 - Secrets never appear in logs or diagnostics.
  test: `tests/ai/test_endpoint_activation.py::test_degraded_tool_probe_warning_is_redacted`.

### 2.2 Return probe evidence from the activate route [category: code] (depends: 2.1)
`kind: deliverable`

Target: `src/gobby/servers/routes/configuration_generation_endpoints.py::activate_generation_endpoint`

Add `probed_json`, `probed_tools`, and `probe_diagnostics`
(= `probe_result.diagnostics`) to the activate response body (currently
persisted but omitted from the 200). New route test patching
`probe_chat_completions_endpoint` (the chat branch has no route test today)
asserting the response carries `probed_tools: false` + the diagnostic and that
persisted values are unchanged in shape.

**Acceptance:**

- 2.2.1 - Activate response includes `probed_json`, `probed_tools`,
  `probe_diagnostics`. symbol: `activate_generation_endpoint`.
- 2.2.2 - Chat-completions activation branch covered by a route test.
  test: `tests/servers/routes/test_config_values_api.py::test_endpoint_activation_response_reports_probe_evidence`.

### 2.3 Hide tool-probe-failed endpoints from the web-chat picker [category: code] (depends: 2.1)
`kind: deliverable`

Targets:
- `src/gobby/servers/local_provider_models.py::*` — scope-reason: add probed_tools evidence to the endpoint model group and its constructors
- `src/gobby/servers/routes/providers.py::_local_generation_provider_entries`
- `src/gobby/ai/registry_builder.py::_tool_chat_endpoint_unavailable_reason`

`LocalEndpointModelGroup` gains `probed_tools: bool | None = None`, copied from
the endpoint config in both constructor sites of
`discover_local_endpoint_model_group`. In
`_local_generation_provider_entries`, between the `not group.models` and
codex-missing branches, set `unavailable_reason` when
`group.probed_tools is False` for any routable group — vLLM-specific message
built from `VLLM_TOOL_CALLING_HINT`, generic "re-activate the endpoint after
enabling tool calling" otherwise. Downstream `available` /
`supports_web_chat` / `execution_provider` logic is untouched (mirror of the
unreachable-server shape; `ProviderPicker` already hides
`supports_web_chat: false`). Append the vLLM hint to
`_tool_chat_endpoint_unavailable_reason` when `endpoint.protocol == "vllm"`.
`probed_tools is None` (no probe evidence, e.g. YAML-only endpoint with
`tool_chat: false`) changes nothing.

**Acceptance:**

- 2.3.1 - Group carries probe evidence. symbol: `LocalEndpointModelGroup`.
- 2.3.2 - A routable group with `probed_tools: false` is
  `supports_web_chat: false` with a reason naming the vLLM flags; `None` leaves
  the group routable. test: `tests/servers/test_local_llm.py::test_failed_tool_probe_hides_routable_groups_from_web_chat`.
- 2.3.3 - Models-route test covers a vllm endpoint with failed probe end-to-end.
  test: `tests/servers/routes/test_servers_routes_providers.py::test_vllm_endpoint_with_failed_tool_probe_is_unavailable_for_web_chat`.
- 2.3.4 - tool_chat registry reason names the flags for vllm endpoints.
  test: `tests/ai/test_capability_registry.py::test_tool_binding_probe_evidence_gate`.

### 2.4 Document the vLLM tool-calling server flags [category: docs] (depends: 2.3)
`kind: deliverable`

Targets:
- `docs/guides/llm-features.md`
- `docs/guides/providers-and-models.md`
- `docs/guides/system-requirements.md`

llm-features.md (after the `wire_api` paragraph in "## vLLM endpoints", ~line
87): new "Tool calling (required for Codex web chat and agent spawn)"
subsection — start vLLM with `--enable-auto-tool-choice --tool-call-parser
<parser>` (hermes for Qwen; parser must match the model family; LM Studio and
Ollama infer this from the model template, vLLM does not); a flagless server
returns HTTP 400 on every tools request; activate with `tool_chat: true` so the
probe runs; a failed probe persists `probed_tools: false`, logs WARNING,
returns `probe_diagnostics.tools`, and hides the endpoint from the picker until
re-activation succeeds. providers-and-models.md vllm row (~226): append the
picker-hiding clause. system-requirements.md vLLM/vllm-metal rows (~119-120):
append the flags to Notes.

**Acceptance:**

- 2.4.1 - Tool-calling subsection exists. behavior: "Tool calling" in `docs/guides/llm-features.md`.
- 2.4.2 - Routability row names the flags. behavior: "tool-call parser" in `docs/guides/providers-and-models.md`.
- 2.4.3 - Requirements table names the flags. behavior: "enable-auto-tool-choice" in `docs/guides/system-requirements.md`.

## P3: Small parity fixes
`kind: framing`

**Goal**: unblock gwiki tool-chat and make feature routing observable.

### 3.1 Accept `cli: "gwiki"` in the tool-chat route [category: code]
`kind: deliverable`

Target: `src/gobby/servers/routes/llm.py::ToolPolicyPayload`

`ToolPolicyPayload.cli: Literal["gcode"]` → `Literal["gcode", "gwiki"]`
(llm.py:144). The service layer already maps gwiki tools
(`_tool_chat_tools.py` GWIKI_* tables); the route is the only rejector, which
currently 422s every `gwiki compile` / `gwiki upkeep` daemon tool-chat call.
Extend the existing route tests with a gwiki policy pass-through assertion
(mirror of the `tool_policy.cli == "gcode"` assertions at
test_llm_routes.py:1640).

**Acceptance:**

- 3.1.1 - Route accepts a gwiki tool policy and forwards it verbatim.
  symbol: `ToolPolicyPayload`. test: `tests/servers/routes/test_llm_routes.py::test_chat_completions_accepts_gwiki_tool_policy`.

### 3.2 Log successful `feature_llm_call` at INFO [category: code]
`kind: deliverable`

Target: `src/gobby/ai/_text_generation_service.py::*` — scope-reason: flip the success branch of the service's `_log_generation_event` to `logger.info` inside the service class

In `_log_generation_event` (~line 844) the `success` branch uses
`logger.debug`; change to `logger.info` (all features; `_CircuitOpenError` and
recoverable-failure branches stay DEBUG). Rewrite
`test_successful_text_generation_omits_feature_llm_call_at_info` to assert the
record IS present at INFO with provider/model extras; keep the debug-level
recoverable-failure test as-is.

**Acceptance:**

- 3.2.1 - Successful feature calls emit `feature_llm_call` at INFO with
  provider/model/candidate extras.
  test: `tests/ai/test_text_generation.py::test_successful_text_generation_logs_feature_llm_call_at_info`.

## P4: vLLM embeddings provider with fingerprint identity
`kind: framing`

**Goal**: `gobby install` / `gobby embeddings switch` target a vllm-metal
embedding server; provider identity is fingerprinted, not port-guessed.

### 4.1 Extract shared vLLM served-model helpers [category: refactor]
`kind: deliverable`

Targets:
- `src/gobby/agents/local_model.py::*` — scope-reason: extract vllm_served_model_ids and change the select_vllm_served_model signature
- `src/gobby/servers/local_provider_models.py::*` — scope-reason: adapt `_default_alias_canonical_id` to the new `select_vllm_served_model` signature

Extract the HTTP half of `resolve_vllm_served_model` into
`async def vllm_served_model_ids(api_base, api_key, *, timeout=10.0) -> list[str]`
(same httpx→`LocalModelError` mappings). Change `select_vllm_served_model` to a
string signature `(requested, served, *, api_base)` (error text unchanged) and
update both callers (`resolve_vllm_served_model`,
`_default_alias_canonical_id`). Behavior-preserving; existing tests in
`tests/agents/test_local_model.py` and
`tests/servers/test_local_provider_models.py` are updated to the new
signatures, plus new coverage for `vllm_served_model_ids` success and
transport-error mapping.

**Acceptance:**

- 4.1.1 - Served ids resolvable without an endpoint config object.
  symbol: `select_vllm_served_model`.
- 4.1.2 - Transport errors map to `LocalModelError`.
  test: `tests/agents/test_local_model.py::test_vllm_served_model_ids_maps_transport_errors_to_local_model_error`.

### 4.2 Fingerprint embedding servers; drop port inference [category: code] (depends: 4.1)
`kind: deliverable`

Targets:
- `src/gobby/utils/deps.py::get_configured_embedding_provider`
- `src/gobby/ai/embedding_switch_runner.py::*` — scope-reason: replace detect_provider_from_config port inference with fingerprinting
- `src/gobby/cli/_install_state.py::*` — scope-reason: route `_embedding_provider` through the shared fingerprint helper
- `src/gobby/utils/status.py::format_status_message`

New shared helper (in `src/gobby/utils/deps.py` or a small module beside it):
`fingerprint_embedding_server(api_base, api_key, *, timeout=1.5) -> str | None`
— probe the origin: `GET /api/tags` 200 → `"ollama"`; `GET /api/v1/models` 200
→ `"lmstudio"`; `GET /v1/models` with an entry `owned_by == "vllm"` → `"vllm"`;
any `/v1/models` 200 otherwise → `"openai-compatible"`; unreachable → `None`.
Bounded timeouts; sync wrapper for CLI callers (same `asyncio.run` pattern as
`_probe_embedding_dim`). Replace the `:1234`/`:11434` string checks in
`get_configured_embedding_provider` and `_install_state._embedding_provider`
with the fingerprint (fall back to `"openai-compatible"` for a configured but
unreachable api_base). `detect_provider_from_config` loses its
"unknown → ollama" default: fingerprint, and raise a `ValueError` naming
`--provider` when the server cannot be identified (prevents silently
retargeting a vllm user to `:11434`). `format_status_message` renders `vllm` /
`openai-compatible` embedding providers explicitly instead of falling through
to "Ollama (stopped)" CLI-presence guesses.

**Acceptance:**

- 4.2.1 - Custom-port Ollama and LM Studio are identified by fingerprint.
  test: `tests/utils/test_deps.py::test_fingerprint_embedding_server_identifies_custom_port_providers`.
- 4.2.2 - A vLLM embeddings api_base is identified as `vllm` and rendered
  without local-CLI fallbacks. symbol: `format_status_message`.
- 4.2.3 - Switch auto-detect refuses an unidentifiable server instead of
  defaulting to ollama. symbol: `detect_provider_from_config`.

### 4.3 vllm switch path: catalog, coordinator, runner, route, CLI [category: code] (depends: 4.2)
`kind: deliverable`

Targets:
- `src/gobby/ai/embedding_catalog.py::*` — scope-reason: add the `catalog_model_for_provider` mapping helper beside the existing per-provider accessors
- `src/gobby/ai/embedding_switch.py::*` — scope-reason: thread `target_model` through `start_switch`/`_start_switch_unlocked`
- `src/gobby/ai/embedding_switch_service.py::*` — scope-reason: vllm branch in `EmbeddingSwitchCoordinator.start` (served-model resolution, dim pre-flight, explicit `target_api_base`)
- `src/gobby/ai/embedding_switch_runner.py::*` — scope-reason: `_PROVIDER_CONFIG["vllm"]` row and `_provider_api_base` pass-through
- `src/gobby/servers/routes/embeddings.py::*` — scope-reason: `api_base` field on the switch payload, forwarded to the coordinator
- `src/gobby/cli/embeddings.py::*` — scope-reason: `--api-base` option and updated `--provider` help on the switch command

`catalog_model_for_provider(spec, provider)`: ollama→`ollama_tag`,
lmstudio→`lmstudio_ref`, vllm→`None` (served id resolved live), else
`spec.key`; replaces the duplicated if/elif in switch and installer.
`start_switch` gains `target_model: str | None` (journal shape unchanged).
Coordinator vllm branch: require an api_base (explicit `--api-base` or
configured), resolve the single served id via `vllm_served_model_ids` +
`select_vllm_served_model("auto", ...)`, pre-flight one embedding call so a dim
mismatch against `spec.dim` fails before staging collections
(`EmbeddingService` already raises on mismatch), pass
`target_model`/`target_api_base` to the journal. `LocalModelError` →
`ValueError` so the route's 400 mapping applies. Route payload gains
`api_base: str | None`; CLI `switch` gains `--api-base` and forwards it.
Multiple served models fail with an error naming the ids (never guess).

**Acceptance:**

- 4.3.1 - vllm switch resolves the served model and records
  `target_model`/`target_api_base` in the journal.
  test: `tests/ai/test_embedding_switch_daemon_lifecycle.py::test_start_vllm_resolves_served_model_before_opening_journal`.
- 4.3.2 - Missing api_base, multiple served models, and dim mismatch each fail
  with actionable errors before any staging.
  test: `tests/ai/test_embedding_switch_daemon_lifecycle.py::test_start_vllm_rejects_dim_mismatch`.
- 4.3.3 - Route forwards `api_base`; CLI sends it.
  test: `tests/servers/routes/test_embeddings_routes.py::test_embedding_switch_start_forwards_api_base_for_vllm`.

### 4.4 vllm in the embedding installer and install wizard [category: code] (depends: 4.3)
`kind: deliverable`

Targets:
- `src/gobby/cli/installers/embedding.py::install_embedding`
- `src/gobby/cli/_install_embedding_prompts.py::*` — scope-reason: list vllm in the provider picker, prompt for its URL interactively, annotate the model picker
- `src/gobby/cli/install.py::*` — scope-reason: extend the `--embedding-provider` `click.Choice` and help text

`install_embedding`: `provider == "vllm"` requires `--embedding-url` (mirror
the openai-compatible guard); no setup step (operator-started); model =
override or served-id resolution via a sync helper over
`vllm_served_model_ids`; dim always probed when not overridden and NEVER
falling back to a provider default (guard `cfg["dim"] is not None` on the
fallback branch); catalog dim mismatch fails with both values named. Wizard:
vllm option always listed (never auto-picked), URL prompt when interactive and
no `--embedding-url`, blank URL records a failure. `click.Choice` gains
`"vllm"`.

**Acceptance:**

- 4.4.1 - Non-interactive vllm install resolves the served model, probes dim,
  persists generic config. symbol: `install_embedding`.
  test: `tests/cli/installers/test_embedding_installer.py::test_vllm_resolves_served_model_and_probes_dim`.
- 4.4.2 - vllm without a URL fails; probe failure returns an actionable error
  with no default-dim fallback.
  test: `tests/cli/installers/test_embedding_installer.py::test_vllm_probe_failure_returns_actionable_error`.
- 4.4.3 - Wizard lists vllm, prompts for URL, and never auto-selects it.
  test: `tests/cli/test_install_embedding_wizard.py::test_interactive_choose_vllm_prompts_for_url_and_passes_it_to_installer`.

### 4.5 Embeddings docs [category: docs] (depends: 4.4)
`kind: deliverable`

Targets:
- `docs/guides/system-requirements.md`
- `docs/guides/configuration.md`
- `docs/guides/llm-features.md`
- `docs/guides/cli-commands.md`

Embeddings table row for vLLM/vllm-metal (served model from `/v1/models`,
operator-chosen api_base, dim probed); configuration guide shows
`gobby install --embedding-provider vllm --embedding-url ...` and
`gobby embeddings switch <key> --provider vllm --api-base ...`, notes
fingerprint identification and that vLLM serves one model per process (separate
port from generation); cli-commands updates the provider values.

**Acceptance:**

- 4.5.1 - Embeddings guidance covers vllm install + switch. behavior: "embedding-provider vllm" in `docs/guides/configuration.md`.
- 4.5.2 - Requirements table has the vLLM embeddings row. behavior: "vllm-metal" in `docs/guides/system-requirements.md`.

## P5: Generation-endpoint health in gobby status
`kind: framing`

**Goal**: `gobby status` shows every configured generation endpoint with live
health and served model. (Lands after P4 — both edit `status.py`.)

### 5.1 Daemon-side endpoint probe in /api/admin/status [category: code] (depends: P4)
`kind: deliverable`

Targets:
- `src/gobby/servers/local_provider_models.py::*` — scope-reason: add `probe_generation_endpoint` / `probe_generation_endpoints` beside the discovery helpers they reuse
- `src/gobby/servers/routes/admin/_health.py::*` — scope-reason: launch the probe task at the top of `status_check` and attach `generation_endpoints` to the payload

`probe_generation_endpoint(name, endpoint, *, timeout=1.5)`: one
`GET {origin}/v1/models`, per-endpoint `asyncio.wait_for`; vllm resolves
`model: auto` via `select_vllm_served_model` (multi-model → unhealthy with the
error text); other protocols report the configured model. Returns
`{name, protocol, provider_label, wire_api, api_base, model, healthy,
served_model, model_count, error}`. `probe_generation_endpoints` gathers over
config order and never raises. In `status_check`, create the probe task early
(getattr-chain tolerant of test fixtures without `ai`), await it just before
building the payload under try/except→WARNING, and add
`"generation_endpoints"` to the payload; overall daemon `status` is unaffected
by endpoint health.

**Acceptance:**

- 5.1.1 - vllm probe resolves the served model; multi-model and transport
  errors report unhealthy with the reason.
  test: `tests/servers/test_local_provider_models.py::test_probe_generation_endpoint_vllm_resolves_auto_served_model`.
- 5.1.2 - `/api/admin/status` carries `generation_endpoints`, empty when none
  configured, and logs probe failure without failing the route.
  test: `tests/servers/routes/test_admin.py::test_status_endpoint_includes_generation_endpoint_health`.

### 5.2 Render Generation lines in gobby status [category: code] (depends: 5.1)
`kind: deliverable`

Target: `src/gobby/utils/status.py::format_status_message`

After the Embeddings block: first endpoint on a `Generation:` label line,
subsequent endpoints indented continuations —
`vllm (vLLM) http://localhost:8321 — healthy, mlx-community/Qwen2.5-3B-Instruct-4bit`
/ `— unreachable (<short error>)`. Omit the block entirely when no endpoints
are configured or the daemon payload lacks the key.

**Acceptance:**

- 5.2.1 - Healthy, unhealthy, multi-endpoint, and absent cases render
  correctly. test: `tests/utils/test_utils_status.py::test_format_status_message_renders_generation_endpoint_health`.

## P6: Live parity verification
`kind: verification`

Run after all fixes land, against the running daemon and the two local servers
(vision server restarted with the tool flags first; re-activate `vllm-vision`
and confirm `probed_tools: true`).

1. Activation: `PUT /api/config/generation-endpoints/vllm/activate` → response
   carries `probed_model`, `probed_json/probed_tools: true`, empty
   `probe_diagnostics`. Negative: activate against a flagless throwaway vllm →
   `probed_tools: false`, diagnostic naming the flags, WARNING in daemon log,
   group hidden from `/api/providers/models` with the reason.
2. Discovery: `GET /api/providers/models` lists `endpoint:vllm` (label vLLM,
   served model, context_length 32768) and `endpoint:vllm-vision`
   (`input_modalities` includes image); `endpoint:lm-studio` reappears.
3. Direct HTTP: with bearer + `X-Gobby-Runtime-Grant` (Rust grant cache or
   `/api/runtime/handshake`), `POST /api/llm/generate`
   `{"prompt": ..., "provider": "endpoint:vllm", "model": "auto"}` succeeds;
   `POST /api/llm/chat/completions` with a gcode read-only tool policy and
   `candidates: [{"candidate": "endpoint:vllm/auto"}]` completes a tool loop;
   a gwiki tool policy is accepted (3.1).
4. Feature routing: PATCH `ai.generation.profile_defaults.feature_low` to
   `[{"candidate": "endpoint:vllm/auto"}]`, trigger
   `POST /api/sessions/<uuid>/generate-summary`, observe the INFO
   `feature_llm_call` with `provider=endpoint:vllm`; restore the profile after.
5. Web chat: `/ws` with bearer auth — `set_project`, `set_provider: codex`,
   `chat_message` with `model: "endpoint:vllm"` streams `chat_stream` frames to
   `done: true`; picker data confirmed via `/api/providers/models`
   (`supports_web_chat: true`). UI spot-check in the browser.
6. Vision: `POST /api/llm/vision/extract` with a test image auto-selects
   `endpoint:vllm-vision` (response names provider/model); `gwiki ingest-file
   <image>` works.
7. Agent spawn: `spawn_agent` with `provider: codex`, `model: "endpoint:vllm"`,
   `isolation: "worktree"`, trivial reply-only prompt; verify the Codex config
   overrides (`gobby-vllm-vllm`, `wire_api="responses"`) and completion; delete the
   worktree afterward.
8. Embeddings: start a vllm-metal embedding server (e.g.
   `Qwen/Qwen3-Embedding-0.6B` on :8323), `gobby embeddings switch <matching
   catalog key> --provider vllm --api-base http://localhost:8323/v1`; doctor
   reports the served model and probed dim; fingerprint identifies the server.
9. Status: `gobby status` shows Generation lines for `vllm` and `vllm-vision`
   (healthy + served model) and the embeddings line without local-CLI guesses.
10. Regression: focused suites for every touched area — `tests/servers/routes`
    (providers, config_values_api, llm_routes, admin, embeddings_routes),
    `tests/ai` (endpoint_activation, capability_registry, text_generation,
    embedding_*), `tests/agents/test_local_model.py`, `tests/utils`
    (deps, status), `tests/cli` (embedding installer + wizard) — plus
    `ruff format/check`, `mypy src/`, and
    `uv run gobby plans validate .gobby/plans/vllm-parity-fixes.md`.
