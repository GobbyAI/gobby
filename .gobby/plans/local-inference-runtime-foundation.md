Plan artifact: `.gobby/plans/local-inference-runtime-foundation.md`

# Unified Local Inference Runtime Foundation

## Overview
`kind: framing`

**Plan ID:** `local-inference-runtime-foundation`

Replace the current collection of local generation and embedding paths with one
machine-local runtime contract for LM Studio, Ollama, and vLLM. Gobby exposes
optional local `text`, `coding`, `vision`, and `embeddings` roles through one
normalized model catalog, lifecycle service, API, CLI, and Settings surface.
Cloud and generic remote providers remain independent.

The coding role supports Codex, Claude, Qwen, Grok, and Droid through
runtime-specific local-model adapters. Every local coding session receives a
bounded lean profile and requires a provider-reported context window of at least
65,536 tokens. Hosted OpenAI/ChatGPT Codex sessions, hosted sessions from other
CLIs, and unclassified Responses endpoints keep their complete feature sets.

## Constraints
`kind: framing`

- Gobby requires one working embedding route. It may be cloud-backed or the
  active local family's `embeddings` role. Every local role is optional.
- Configured local roles share exactly one active family: `lmstudio`,
  `ollama`, or `vllm`. Generic remote OpenAI-compatible/Responses endpoints
  remain under existing generation configuration and never inherit local
  lifecycle, eligibility, or lean-profile behavior.
- Machine-local family and role intent is restart-bound and belongs in
  `bootstrap.yaml`. Structural embedding identity and active collection state
  remain journaled hub state. The family-switch coordinator owns the
  cross-store transition as one idempotent recovery seam.
- Public local selectors use `local:<role>` for the configured role model and
  `local:<role>/<model>` for an explicit model in the active family. Existing
  `endpoint:<name>/<model>` selectors remain for independent remote endpoints.
- Roles map to daemon capabilities as follows: `text` owns `text_generate`;
  `coding` owns `tool_chat`, `agent_spawn`, and repository-aware `web_chat`;
  `vision` owns `vision_extract`; `embeddings` owns `embed`. Multimodal is
  capability metadata, not another role.
- Every role uses a `model` reference. A normalized model record carries the
  provider-native id, canonical family/version, quantization, artifact
  digest/revision, context or dimensions, modalities, preprocessing/query
  prefix, execution location, and exact provenance.
- Provider/backend identity participates in embedding identity. Any unresolved
  identity change rebuilds vectors even when model names and dimensions match.
- Model choice is manual for 0.5.0. Gobby displays observed metadata (artifact
  size, quantization, context, dimensions, modalities, execution location)
  without resource estimates, ranking, or silently choosing a model. Resource
  estimates are post-0.5 work under #18498.
- First-release acquisition covers installed inventory, shipped per-family role
  default references (1.1), and exact provider references. Defaults are one
  static constant table and pre-fill values only; a preset catalog with digest
  policy, license/remote-code metadata, or drift checks is post-0.5 work under
  #18498. One authorization policy covers every mutating operation: downloads
  and other artifact acquisition, and profile changes that require a migration,
  need explicit confirmation from the caller; lease-driven load, unload, and
  idle eviction follow the declared load policy and never ask. Model deletion
  and remote cross-provider search are outside this plan.
- Role load policy is `on_demand` or `pinned`, defaulting to `on_demand`.
  Active leases prevent eviction. Idle eviction may unload LM Studio/Ollama
  models or stop Gobby-owned vLLM role processes.
- LM Studio/llmster and Ollama are installed by the setup-wizard subsystem and
  keep their provider daemon contracts. Gobby manages selected model lifecycle.
  Gobby supervises role-specific vllm-metal processes on Apple Silicon and CUDA
  vLLM processes on Linux/NVIDIA. Other vLLM hosts are unavailable with a typed
  remedy.
- Ollama cloud support uses a signed-in localhost Ollama daemon and `:cloud`
  tags. Those models report `execution_location="cloud"`. Direct authenticated
  `ollama.com` endpoint routing is outside this plan.
- Local coding eligibility requires reported context `>= 65_536`, successful
  evidence for the runtime's required tool transport, and a complete enforceable
  runtime profile. Missing context is ineligible.
- Local coding profiles retain core shell/edit tools, lifecycle hooks, Gobby MCP,
  and image inspection only for image-capable models. They suppress apps,
  plugins, browser/computer use, image generation, provider-native multi-agent,
  remote plugins, native skill search, goals, and tool suggestions.
- Local coding profiles preserve the full harness instruction chain: system and
  developer instructions, AGENTS/CLAUDE/QWEN project instructions, Gobby
  session/task/workflow context, persona and task prompts, hooks, and explicitly
  loaded skills. They never use safe/bare modes, replacement system prompts, or
  settings-source filters that suppress instruction discovery.
- The internal daemon `tool_chat` path keeps its existing native-tool isolation.
  It may expose bounded gcode commands through an explicit
  `ToolPolicy(cli="gcode", ...)` dynamic-tool contract.
- Missing runtime controls fail closed with a typed eligibility reason. No
  undocumented flag or user-config mutation may stand in for an enforceable
  profile.
- Transport activation probes each distinct supported wire once: OpenAI Chat
  Completions, Responses, and Anthropic Messages. Tool-probe and context
  failures remain independent reason entries.
- Direct `text` generation remains eligible for short-context models under its
  own capability checks. Coding ineligibility never marks transport connectivity
  unhealthy.
- faster-whisper STT, Chatterbox TTS, and image generation remain separate
  provider systems.
- UI-TARS remains the post-0.5 local/offline computer-use fallback. Its task
  lifecycle is already landed: #20405 sits under #18498 (path `18498.20405`)
  and #18866 closed completed on 2026-08-22, so handoff takes no lifecycle
  action on either and expansion creates no leaf for them; 6.2's follow-up link
  to #20405 under #18498 stands as written. Hosted Codex and Claude keep native
  computer/browser capabilities.
- Preserve landed work: #20670 shared vLLM helpers, #20666 tool-probe gating,
  #20676/#20677 health surfaces, #20679 Responses transport, #20678 live parity
  evidence, and #20680 embedding-journal correction. Run no replacement live
  parity matrix.
- Preserve unrelated dirty plan files and any foreign-session work. Run no live
  download, provider-family switch, embedding migration, temporary embedding
  server, or vector mutation during implementation validation.
- Before build handoff, preserve the criteria from #19653 (1.2, including
  1.2.5's remote-provider OpenRouter branch), close it as `superseded` with
  the replacement plan reference and that disposition note, and let expansion
  create the canonical implementation leaves.
- #20672 is a completed prerequisite rather than plan work. It closed at commit
  5032d6b3cd, which landed the unconditional
  `-c check_for_update_on_startup=false` override in `command_builder.py`, the
  typed `codex_composer_not_ready` prompt-delivery failure carrying redacted
  pane text, and their focused tests. Handoff takes no lifecycle action on it,
  and expansion creates no leaf for it. 4.4 keeps that behavior as baseline
  regression validation (4.4.1 and the 4.4.5 live `e2e` managed-launch case)
  and implements only the remaining classifier, lifecycle-fence, launch-race,
  and integration work.
- #20151 owns installer-wizard UX and third-party runtime installation. This
  plan supplies shared pre-daemon detection (2.3 `detection.py`, 5.2 library
  path) and post-start control contracts. #20151's description still names
  "P4" as the detection owner from an earlier numbering; at handoff, update
  that scope-boundary sentence to cite 2.3 and 5.2.
- Post-0.5 follow-ups under #18498: unified remote model search,
  hardware/model recommendations and resource estimates, a model preset
  catalog, direct Ollama cloud routing, llama.cpp and mlx-vlm families, and
  UI-TARS. The model-independent sensitive-root proof
  failure remains separate security work.
- Current size-sensitive targets (2026-08-22 counts): `src/gobby/ai/embeddings.py`
  992, `src/gobby/runner_lifecycle_shutdown.py` 916,
  `src/gobby/config/registry.py` 878, `src/gobby/runner_lifecycle_subsystems.py`
  857, `src/gobby/runner_init/services.py` 850,
  `src/gobby/ai/_text_generation_service.py` 864, and
  `src/gobby/ai/embedding_switch_runner.py` 839. Their owning deliverables (3.1,
  2.3, 1.1, 2.3, 3.1, 3.1, 3.2) split/move behavior into new modules before
  adding logic. Watch list, each owned by a deliverable that adds only thin calls into
  new modules:
  `src/gobby/servers/websocket/chat/backends/droid.py` 791 (4.5),
  `src/gobby/agents/spawn_executor.py` 782 (4.3),
  `web/src/components/settings/sections/MemoryKnowledgeSection.tsx` 755 (5.3),
  `src/gobby/agents/resume_executor.py` 749 (4.3),
  `src/gobby/servers/websocket/chat/backends/codex.py` 748 (4.5),
  `src/gobby/config/persistence.py` 747 (1.1), and
  `src/gobby/ai/registry_builder.py` 734 (3.1).
- The resolved `EmbeddingsConfig` field set (`model`, `dim`, `api_base`,
  `api_key`, `query_prefix`, `catalog_key`) is the effective-config projection
  read by Python search/skills/memory modules and by `gcore`/`gcode`/`gwiki`.
  It stays byte-stable; 1.1 adds `source` beside it and no crate changes for
  embedding config are in scope.
- The `.gobby/plans/local-inference-runtime-foundation.coverage-ledger.yaml`
  companion required by `docs/contracts/plan-coverage.md` binds the epic
  `root_task_ref` and the approved `plan_hash`, neither of which exists during
  adversarial review; it is authored at build handoff from the approved plan,
  before expansion, enumerating every acceptance item and its expected leaves.
  The ledger is a mechanical derivation of the adversary-approved acceptance
  items and M1 manifest entries, and the contract requires it to be
  adversary-reviewed before expansion, so the handoff obligation has four
  ordered steps: generate it once `root_task_ref` and the approved
  `plan_hash` exist; verify its header binding and complete
  acceptance-to-leaf parity against the approved manifest (the checks
  `src/gobby/plans/bootstrap_ledger.py` performs); run one taskless
  adversarial review of the exact bound ledger file (the review receives the
  ledger path and the approved `plan_hash` and never edits the approved
  plan), regenerating or correcting and re-reviewing the ledger after a
  `needs_review` verdict; and block expansion until that review returns
  `approved`.

## P1: Machine-Local Authority and Model Identity
`kind: framing`

**Goal:** Establish one restart-bound local-family schema and one normalized
model identity consumed by every later subsystem.

### 1.1 Add local-family role configuration and selector authority [category: code]
`kind: deliverable`

Targets:
- `src/gobby/config/local_runtime.py`
- `src/gobby/config/bootstrap.py::*` — scope-reason: parse, validate, serialize, and project the complete machine-local runtime block
- `src/gobby/config/app.py::*` — scope-reason: expose the bootstrap-projected local runtime to daemon services and validation
- `src/gobby/config/ai.py::*` — scope-reason: reserve local protocols for the role system while preserving generic remote endpoints
- `src/gobby/config/persistence.py::*` — scope-reason: add `EmbeddingsConfig.source` and its local-source validators while keeping the resolved field set byte-stable so no existing consumer changes
- `src/gobby/config/feature_base.py::*` — scope-reason: accept the typed `local:` candidate grammar in `_parse_feature_candidate_label`; existing candidate labels and consumers are unchanged
- `src/gobby/config/registry_bootstrap_ownership.py`
- `src/gobby/config/registry.py::*` — scope-reason: replace the flattened-default `BOOTSTRAP_RUNTIME_PATHS` derivation with a call into the new schema-prefix ownership module; the file is 878 lines and gains only that call
- `src/gobby/config/embedding_keys.py::*` — scope-reason: add coordinator-owned `source` to the structural embedding key inventory and translators while the six-field resolved projection stays byte-stable
- `src/gobby/ai/endpoints.py::*` — scope-reason: add the typed `local:<role>[/<model>]` grammar beside `endpoint:` parsing and retire `_reject_removed_local_selector` for that grammar only
- `src/gobby/llm/local_detection.py::*` — scope-reason: classify the typed `local:` grammar as local in `is_local_agent_definition`; route consumers keep calling the same predicate
- `crates/gcore/assets/config/runtime_config_contract.json::*` — scope-reason: regenerate the complete derived runtime config contract
- `src/gobby/storage/config_store.py::*` — scope-reason: enforce `embedding_local_fields_derived` for every non-coordinator embedding write (user patches and installer bootstrap) at the transaction boundary
- `src/gobby/storage/config_mutations.py::*` — scope-reason: carry the local-source derived-field rejection through the shared mutation authorization path
- `tests/config/test_local_runtime.py`
- `tests/config/test_bootstrap.py::*` — scope-reason: cover parsing, round trips, rejected shapes, and file projections
- `tests/config/test_app_config.py::*` — scope-reason: cover the projected `local_runtime` daemon field and join-only rejection
- `tests/config/test_ai.py`
- `tests/config/test_persistence.py::*` — scope-reason: cover cloud-versus-local embedding source invariants
- `tests/config/test_feature_base.py::*` — scope-reason: cover accepted `local:` candidate labels
- `tests/config/test_config_registry.py::*` — scope-reason: prove every nested `local_runtime` leaf is bootstrap-owned and rejected from the runtime registry
- `tests/config/test_embedding_keys.py::*` — scope-reason: cover `source` in the structural key inventory, translators, and storage-key validation
- `tests/config/test_runtime_config_contract.py::test_checked_in_contract_matches_registry`
- `tests/ai/test_endpoints.py::*` — scope-reason: cover local selector parsing beside unchanged endpoint selectors
- `tests/llm/test_local_detection.py::*` — scope-reason: cover `local:` selectors classifying as local agent definitions
- `tests/storage/test_config_store.py::*` — scope-reason: cover coordinator-only derived-field writes under `source="local"`
- `tests/storage/test_embedding_switch_config_contract.py::*` — scope-reason: cover `source` inside the switch-write contract and the installer-bootstrap rejection

Create typed models equivalent to:

```python
LocalFamily = Literal["lmstudio", "ollama", "vllm"]
LoadPolicy = Literal["on_demand", "pinned"]
CodingRuntime = Literal["codex", "claude", "qwen", "grok", "droid"]

class LocalRoleConfig(BaseModel):
    model: str
    load_policy: LoadPolicy = "on_demand"

class CodingRoleConfig(LocalRoleConfig):
    runtime: CodingRuntime

class LocalRuntimeConfig(BaseModel):
    provider: LocalFamily | None = None
    base_url: str | None = None
    api_key: str | None = None
    text: LocalRoleConfig | None = None
    coding: CodingRoleConfig | None = None
    vision: LocalRoleConfig | None = None
    embeddings: LocalRoleConfig | None = None
```

Store this block in machine-local bootstrap configuration and project it into
`DaemonConfig`. Require `provider` whenever any role is configured; reject
local roles on join-only machines.

`base_url` and `api_key` are the only connection inputs for the active family.
`base_url` defaults to `http://127.0.0.1:1234` for `lmstudio` and
`http://127.0.0.1:11434` for `ollama`, must be a loopback origin (`127.0.0.1`,
`localhost`, or `::1`) with no path, and is rejected for `vllm`, whose origins
are the 2.2 supervisor's allocated loopback ports. `api_key` is a literal or a
`$secret:NAME` reference resolved through the existing `SecretStore` when the
2.3 service is constructed; adapters receive the resolved origin and credential
from the service and never read bootstrap or secrets themselves. Status, HTTP,
CLI, and Settings projections redact `api_key`. Join-only machines reject the
whole block, including these two fields.

Ship `DEFAULT_ROLE_MODELS: dict[LocalFamily, dict[str, str]]` in
`src/gobby/config/local_runtime.py` with exactly these twelve references:

| role | `lmstudio` | `ollama` | `vllm` (Hugging Face id) |
|---|---|---|---|
| `text` | `qwen/qwen3-8b` | `qwen3:8b` | `Qwen/Qwen3-8B` |
| `coding` | `qwen/qwen3-coder-30b` | `qwen3-coder:30b` | `Qwen/Qwen3-Coder-30B-A3B-Instruct` |
| `vision` | `qwen/qwen2.5-vl-7b` | `qwen2.5vl:7b` | `Qwen/Qwen2.5-VL-7B-Instruct` |
| `embeddings` | `text-embedding-nomic-embed-text-v1.5` | `nomic-embed-text:v1.5` | `nomic-ai/nomic-embed-text-v1.5` |

Beside each `coding` default record `DEFAULT_CODING_CONTEXT` as the
provider-documented context: 262,144 for every `coding` entry above
(Qwen3-Coder-30B-A3B native context). Provenance for every entry is the
provider catalog page for that exact reference (LM Studio model catalog, Ollama
library, Hugging Face model card), recorded as a module-level comment next to
the table. Defaults are pre-fill values for 5.2 `set-role` and the 5.3 role
editor. They never become implicit configuration: a configured role always
carries its explicit `model`, and a missing `model` is rejected rather than
defaulted. Keep generic remote generation endpoints in the current AI
generation namespace and reject `lmstudio`, `ollama`, or `vllm` there with the
replacement path.

Pre-existing rows under `ai.generation.endpoints` that carry one of those three
protocols (for example a hand-configured `lm-studio` row from 0.4 testing) are
handled at upgrade time without stopping the daemon: resolved `DaemonConfig`
construction in `src/gobby/config/app.py` and `src/gobby/config/persistence.py`
excludes each such row from the resolved generation endpoints, so nothing
probes, catalogs, or executes through it, and logs one startup warning per row
naming the endpoint and the `local:<role>` replacement path. The stored
`desired` row is neither rewritten nor deleted by the daemon; it remains
visible in Settings until the operator removes it, and any write that asserts a
local protocol there — a user patch, YAML import, or whole-row resubmission —
fails with the 1.1.16 rejection. A config snapshot containing only legacy local
rows therefore resolves to an empty generation endpoint map rather than a
startup failure.

Own the whole `local_runtime` subtree in the runtime config registry by schema
prefix, not by flattening default values: `src/gobby/config/registry.py`
derives `BOOTSTRAP_RUNTIME_PATHS` from `BootstrapConfig().to_config_dict()`,
which cannot enumerate leaves under optional role objects that default to
`None`. Move the bootstrap-ownership derivation out of
`src/gobby/config/registry.py` (878 lines) into the new
`src/gobby/config/registry_bootstrap_ownership.py`, which walks the
`BootstrapConfig`/`LocalRuntimeConfig` schema (not a value tree) to produce
the owned path set and prefix set; `registry.py` keeps its `_validate`
conflict check, which now also rejects any runtime leaf under an owned prefix,
and gains only the call into the new module. `local_runtime`
and every nested leaf such as `local_runtime.coding.model`,
`local_runtime.coding.runtime`, and `local_runtime.embeddings.load_policy`
are therefore bootstrap-owned and can never enter the runtime-mutable registry.

Add `source` to the structural embedding key inventory in
`src/gobby/config/embedding_keys.py` (`AI_EMBEDDING_CONFIG_KEYS`,
`RUNTIME_EMBEDDING_CONFIG_KEYS`, the storage/runtime key translators, and
`validate_embedding_storage_config_key`) as a coordinator-owned key: the 3.2
coordinator commits `source` in the same structural switch write as `model`,
`dim`, `query_prefix`, and `catalog_key`, and user-facing config writes reject
it under `source="local"` with `embedding_local_fields_derived`. The six-field
resolved `EmbeddingsConfig` projection is unchanged; `source` is a seventh
persisted key beside it, never part of the projection.

The transaction boundary enforces the single-writer rule: `ConfigStore` and
`ConfigMutations` accept `model`, `dim`, `query_prefix`, and `catalog_key`
under `source="local"` (persisted or in the same patch) only through
`set_embedding_switch_values`; user-facing patches and the installer's
`set_embedding_bootstrap_values` reject them with
`embedding_local_fields_derived`. `source` joins the bootstrap-writable key
set so the installer can stage `source="local"` with null `api_base`/`api_key`
and no derived fields (3.2 reroutes that installer branch).

Extend `EmbeddingsConfig` with `source: Literal["cloud", "local"] = "cloud"`.
The existing resolved field set (`model`, `dim`, `api_base`, `api_key`,
`query_prefix`, `catalog_key`) stays byte-stable as the effective-config
projection: it is the wire shape consumed by the Python search/skills/memory
modules and by the Rust effective-config path (`crates/gcore/src/config/types.rs`,
`crates/gcore/src/config/resolve.rs`, `crates/gcode/src/vector/code_symbols/*`,
`crates/gwiki/src/vector.rs`), none of which change in this plan. Cloud source
owns its remote `api_base`/`api_key`/`model`. Local source requires the bootstrap
`embeddings` role on a hub/solo machine; `api_base` and `api_key` must be null,
and `model`, `dim`, `query_prefix`, and `catalog_key` are written only by the
family-switch coordinator (3.2) from the completed switch record. A user-supplied
value for one of those four fields under `source="local"` is rejected with
`embedding_local_fields_derived`. No field is removed.

Recorded consumer sweep for the stable projection (re-run before closing; the
hit list must not shrink or gain a constructor that passes a removed field):

```bash
gcode grep -w EmbeddingsConfig src/ tests/ crates/ web/ -l -m 80
gcode grep "embeddings\.(model|dim|api_base|api_key|query_prefix|catalog_key)\b" src/ -l -m 80
gcode grep -i "catalog_key|query_prefix" crates/ -l -m 40
```

Hits (2026-08-22): `src/gobby/ai/embeddings.py`, `src/gobby/config/app.py`,
`src/gobby/config/persistence.py`, `src/gobby/config/servers.py`,
`src/gobby/mcp_proxy/tools/skills/__init__.py`, `src/gobby/runner_init/services.py`,
`src/gobby/search/backends/embedding.py`, `src/gobby/search/models.py`,
`src/gobby/search/unified.py`, `src/gobby/servers/routes/embeddings.py`,
`src/gobby/skills/manager.py`, `src/gobby/skills/search.py`,
`src/gobby/ai/embedding_switch_runner.py`, `src/gobby/ai/embedding_switch_service.py`,
`src/gobby/ai/registry_builder.py`, `src/gobby/cli/installers/embedding.py`,
`src/gobby/config/embedding_keys.py`, `src/gobby/mcp_proxy/registries.py`,
`src/gobby/runner_lifecycle_subsystems.py`, `src/gobby/runtime_grants/service.py`;
13 test files under `tests/ai`, `tests/cli`, `tests/config`, `tests/memory`,
`tests/search`, `tests/servers`, plus `tests/test_runner_init.py`; Rust readers
listed above plus `crates/gcore/src/config/tests/embedding_guard.rs` and
`crates/gcore/src/ai/embeddings.rs`.

Parse `local:<role>` and `local:<role>/<model>` in `src/gobby/ai/endpoints.py`
beside `parse_endpoint_model_selector`, partitioning only the first slash after
the role so provider model ids retain embedded slashes. `REMOVED_LOCAL_PROVIDER_PREFIX`
and `_reject_removed_local_selector` stop rejecting this typed grammar only;
`_parse_feature_candidate_label` in `src/gobby/config/feature_base.py` accepts
the same grammar, and `is_local_agent_definition` in
`src/gobby/llm/local_detection.py` classifies it as local. Do not translate
removed pre-0.5 local endpoint shapes such as `local:lm-studio/<model>`.

**Acceptance:**

- 1.1.1 - Bootstrap round trips one optional active family and four optional
  role configs, rejects mixed/roleless invalid shapes, and preserves load policy
  and coding runtime. test: `tests/config/test_local_runtime.py`.
- 1.1.2 - Cloud embeddings validate independently; local embeddings require the
  matching local role and a hub/solo machine, null `api_base`/`api_key`, and
  reject user-supplied derived fields; the resolved field set is unchanged.
  test: `tests/config/test_persistence.py`.
- 1.1.3 - Local selectors preserve slash-bearing model ids and resolve only
  against the active family; generic endpoint selectors remain unchanged. test:
  `tests/ai/test_endpoints.py`.
- 1.1.4 - Runtime config carrier freshness passes after adding the projected
  daemon field and `embeddings.source`. test:
  `tests/config/test_runtime_config_contract.py::test_checked_in_contract_matches_registry`.
- 1.1.5 - Feature candidate labels and local agent detection accept the typed
  `local:` grammar and still reject removed pre-0.5 shapes. test:
  `tests/config/test_feature_base.py` and `tests/llm/test_local_detection.py`.
- 1.1.6 - `DEFAULT_ROLE_MODELS` equals the twelve-entry table above exactly,
  every `coding` default records context 262,144 (`>= 65_536`), and a role
  block without `model` is rejected instead of defaulted. test:
  `tests/config/test_local_runtime.py`.
- 1.1.7 - `local_runtime` and every nested leaf (`provider`, each role's
  `model`/`load_policy`, `coding.runtime`) are bootstrap-owned by prefix;
  a runtime-registry leaf under that prefix raises `RegistryError`, and the
  conflict validation walks the schema rather than default values. test:
  `tests/config/test_config_registry.py`.
- 1.1.8 - `source` is a structural embedding key in both key inventories and
  both translators, user writes of `source="local"` derived fields fail with
  `embedding_local_fields_derived`, and the six-field resolved projection is
  byte-identical to the pre-change fixture. test:
  `tests/config/test_embedding_keys.py` and `tests/config/test_persistence.py`.
- 1.1.9 - The text-role defaults equal the LM Studio, Ollama, and vLLM references in the text row and retain exact provider-catalog provenance. test: `tests/config/test_local_runtime.py::test_default_role_models_text`.
- 1.1.10 - The coding-role defaults equal the three coding references, record context 262,144, and retain exact provider-catalog provenance. test: `tests/config/test_local_runtime.py::test_default_role_models_coding`.
- 1.1.11 - The vision-role defaults equal the LM Studio, Ollama, and vLLM references in the vision row and retain exact provider-catalog provenance. test: `tests/config/test_local_runtime.py::test_default_role_models_vision`.
- 1.1.12 - The embeddings-role defaults equal the LM Studio, Ollama, and vLLM references in the embeddings row and retain exact provider-catalog provenance. test: `tests/config/test_local_runtime.py::test_default_role_models_embeddings`.
- 1.1.13 - `base_url` defaults per family, rejects non-loopback origins, paths,
  and any value for `vllm`; `api_key` accepts a literal or `$secret:` reference,
  resolves at service construction, and is absent from every projection. test:
  `tests/config/test_local_runtime.py` and `tests/config/test_bootstrap.py`.
- 1.1.14 - Under `source="local"`, derived fields written through user patches
  or `set_embedding_bootstrap_values` fail with `embedding_local_fields_derived`
  while `set_embedding_switch_values` commits them with `source` in one
  transaction. test: `tests/storage/test_config_store.py` and
  `tests/storage/test_embedding_switch_config_contract.py`.
- 1.1.15 - Join-only machines reject the entire local-runtime block, including provider-only connection fields and every role configuration. test: `tests/config/test_app_config.py`.
- 1.1.16 - Generic generation configuration rejects lmstudio, ollama, and vllm protocol values and reports the local-runtime replacement path. test: `tests/config/test_ai.py`.
- 1.1.17 - A stored config snapshot with pre-existing `lmstudio`, `ollama`, or
  `vllm` rows under `ai.generation.endpoints` still resolves to a `DaemonConfig`:
  each legacy row is excluded from the resolved endpoints, one startup warning
  per row names the endpoint and the `local:<role>` replacement path, the
  stored `desired` values are unchanged, and a snapshot holding only legacy
  rows resolves to an empty endpoint map. test: `tests/config/test_app_config.py`
  and `tests/config/test_ai.py`.

### 1.2 Normalize model records and complete context discovery [category: code] (depends: 1.1)
`kind: deliverable`

Targets:
- `src/gobby/ai/local_runtime/__init__.py`
- `src/gobby/ai/local_runtime/contracts.py`
- `src/gobby/ai/local_runtime/catalog.py`
- `src/gobby/agents/local_model.py::*` — scope-reason: replace id-only vLLM parsing with shared typed records while keeping id projection consumers
- `src/gobby/servers/local_provider_models.py::*` — scope-reason: emit normalized records and complete provider-specific context/modalities extraction
- `src/gobby/runner_init/servers.py::*` — scope-reason: derive `_local_model_metadata_exclusions` and `_local_provider_metadata_exclusions` from normalized loopback/active-family records; no other function in the file changes in this deliverable
- `tests/providers/capabilities/test_providers_capabilities_refresh.py::*` — scope-reason: cover the coverage auditor excluding only loopback/active-family models while remote provider models keep OpenRouter coverage auditing
- `tests/agents/test_local_model.py::*` — scope-reason: cover shared vLLM record parsing, selection, and id projections
- `tests/servers/test_local_provider_models.py::*` — scope-reason: cover normalized LM Studio, Ollama, vLLM, and mixed-catalog records
- `tests/servers/test_local_llm.py::*` — scope-reason: prove existing group consumers keep their entry shape beside the attached normalized record

Define `NormalizedLocalModel` with stable family-qualified identity, provider
model id, display label, installed/loaded state, execution location, artifact
digest/revision, quantization, size, canonical context, runtime context,
effective context, embedding dimension, input/output modalities, capability
facts, and per-field provenance. Invalid or unavailable facts stay `None`.

`effective_context` is the one value every consumer uses for eligibility,
profile context, and display. It is the minimum of every verified hard limit:
the canonical (architecture) context, the loaded-instance/runtime context
(LM Studio loaded context length, Ollama `/api/ps` context, vLLM
`max_model_len`), and any Gobby launch cap from the 2.2 supervisor profile.
When the model is loaded and its runtime context is unknown, or when the family
requires a runtime value that is absent, `effective_context` is `None` with a
`context_runtime_unknown` provenance diagnostic; a canonical value never stands
in for a missing runtime value. Raw canonical and runtime values remain on the
record for diagnostics.

The startup coverage auditor keeps #19653's split: `_local_model_metadata_exclusions`
and `_local_provider_metadata_exclusions` derive their exclusion set from the
normalized records of loopback/active-family models (by family-qualified
identity, never by name prefix), so those models use authoritative local
metadata and never raise OpenRouter coverage warnings, while every remote
provider model continues through `ModelMetadataCoverageAuditor` OpenRouter
coverage auditing unchanged.

Extend #20670's shared vLLM parser to retain every catalog record, including
`max_model_len`. Keep `vllm_served_model_ids` as a projection over those shared
records. Resolve `model: auto` before every wire path and return the selected
record.

For LM Studio, map native `max_context_length` and loaded-instance context.
For Ollama, read nested `model_info` keys ending in `.context_length` and
`.embedding_length`, plus `/api/ps` runtime context. Require one unambiguous
positive architecture value; contradictory values remain unknown with a
diagnostic. Preserve completion/vision capability filtering and cloud-tag
classification.

A record carries two identity projections. `display_identity` is
`(family, provider model id)`: it is stable for a daemon lifetime and keys
jobs, owned processes, leases, and backend caches, together with
digest/revision whenever the provider reports them (2.3).
`compatibility_identity` is `(family, backend, artifact digest/revision,
quantization, dimensions)`, and for an embeddings-role model it additionally
carries the catalog entry's vector-affecting preprocessing facts (document
prefix, normalization, pooling); it is `verified` only when every one of those
facts is present. Every cross-observation reuse — 4.1 probe evidence, 3.2 embedding
identity retention, 4.5 backend cache reuse across reconnects — requires an
equal verified compatibility identity; an unverified identity never matches
any prior record, so it forces a fresh probe or a full switch. Provider aliases
such as `nomic-embed-text` never establish quantization or vector
compatibility by name alone. A re-pull under the same name is observed as a
changed compatibility identity at the next inventory refresh: it invalidates
evidence and retention decisions and never retargets a held lease or running
process.

Keep the `LocalEndpointModelGroup` shape and the
`discover_local_endpoint_model_group` signature stable. Attach each
`NormalizedLocalModel` to its existing entry under a `normalized` key so the
current consumers (`src/gobby/ai/_text_generation_builder.py`,
`src/gobby/servers/routes/admin/_health.py`, `src/gobby/servers/routes/providers.py`)
keep working unchanged until 3.1 and 5.1 consume the records.

**Acceptance:**

- 1.2.1 - vLLM records preserve 32,768, 65,536, and 262,144
  `max_model_len` values; id-only consumers receive the same selected ids. test:
  `tests/agents/test_local_model.py`.
- 1.2.2 - Ollama nested context and embedding dimensions, LM Studio native
  context, cloud execution location, modalities, digest, and quantization retain
  exact provenance. test: `tests/servers/test_local_provider_models.py`.
- 1.2.3 - Missing, invalid, contradictory, and unreachable metadata remains
  typed unknown; mixed catalogs retain independent records. test:
  `tests/servers/test_local_provider_models.py`.
- 1.2.4 - Normalized identity distinguishes provider/backend and artifact
  variants even when provider-native names and dimensions match. test:
  `tests/servers/test_local_provider_models.py`.
- 1.2.5 - Loopback/active-family models are excluded from OpenRouter coverage
  auditing by normalized identity and raise no coverage warning, while remote
  provider models still pass through `ModelMetadataCoverageAuditor` and report
  missing OpenRouter metadata exactly as before. test:
  `tests/providers/capabilities/test_providers_capabilities_refresh.py`.
- 1.2.6 - `effective_context` equals the minimum verified hard limit: a
  65,536 canonical / 32,768 loaded model yields 32,768; a loaded model with
  unknown runtime context yields `None` with `context_runtime_unknown`; raw
  canonical and runtime values stay on the record. test:
  `tests/servers/test_local_provider_models.py`.
- 1.2.7 - Two consecutive observations of one family/name with absent
  digest/revision share one display identity for jobs and leases, carry an
  unverified compatibility identity that matches no stored probe evidence or
  retention record, and a digest change under the same name invalidates
  evidence without retargeting a held lease. test:
  `tests/servers/test_local_provider_models.py`.
- 1.2.8 - discover_local_endpoint_model_group keeps its existing signature and LocalEndpointModelGroup entries keep their current shape with only the normalized record key added. test: `tests/servers/test_local_llm.py`.
- 1.2.9 - Canonical and runtime context 262,144 with a supervisor launch cap of 32,768 yields effective_context 32,768 with launch-cap provenance, while a 65,536 cap yields 65,536. test: `tests/servers/test_local_provider_models.py::test_effective_context_applies_launch_cap`.

## P2: Provider Control, Jobs, and Leases
`kind: framing`

**Goal:** Put LM Studio, Ollama, and managed vLLM behind one lifecycle contract
with observable jobs and safe model residency.

### 2.1 Implement LM Studio and Ollama control adapters [category: code] (depends: P1)
`kind: deliverable`

Targets:
- `src/gobby/ai/local_runtime/providers/__init__.py`
- `src/gobby/ai/local_runtime/providers/base.py`
- `src/gobby/ai/local_runtime/providers/lmstudio.py`
- `src/gobby/ai/local_runtime/providers/ollama.py`
- `src/gobby/ai/local_runtime/providers/registry.py`
- `tests/ai/local_runtime/__init__.py`
- `tests/ai/local_runtime/test_lmstudio_provider.py`
- `tests/ai/local_runtime/test_ollama_provider.py`
- `tests/ai/local_runtime/test_provider_registry.py`

Define a typed provider contract for `detect`, `health`, `discover`,
`inspect`, `download`, `download_status`, `load`, `unload`, and
`probe_transport`. Each adapter declares supported operations and returns
normalized records/statuses. Unsupported operations fail before network send
with `operation_unsupported`.

LM Studio uses native model listing, download jobs/status, load, and unload
endpoints. Exact references may be catalog ids or Hugging Face URLs; optional
quantization is provider data. Ollama uses tags/show, pull streaming, ps, chat
keep-alive, and unload semantics. Adapters are constructed by the 2.3 service
with the resolved 1.1 origin and credential and send that credential as the
provider's authentication header on every native request; they never read
bootstrap or secrets directly. Local signed-in Ollama owns cloud
authentication; label `:cloud` models as cloud execution.

Adapters apply the Constraints authorization policy: `download` requires an
explicit caller confirmation carried by the 2.3 acquisition job (the job
record's `confirmed=True` together with the confirmed artifact identity and
resolved digest/revision; an adapter refuses an unconfirmed job before any
network send), while `load` and `unload` are lease-driven operations the 2.3
service invokes without confirmation under the role's declared load policy. Adapter methods
never install the provider runtime, delete model artifacts, silently pull a
missing model during inference, or send an inference request as a capability
probe after another request may have succeeded.

**Acceptance:**

- 2.1.1 - Both adapters implement the same operation envelope and advertise
  exact support/cancellation capabilities. test:
  `tests/ai/local_runtime/test_provider_registry.py`.
- 2.1.2 - LM Studio download/load/unload and Ollama pull/warm/unload map progress,
  auth, already-present state, errors, and cancellation into typed results.
  test: `tests/ai/local_runtime/test_lmstudio_provider.py` and
  `tests/ai/local_runtime/test_ollama_provider.py`.
- 2.1.3 - Ollama localhost cloud tags remain discoverable and carry cloud
  execution location; direct remote hosts are rejected with a scoped remedy.
  test: `tests/ai/local_runtime/test_ollama_provider.py`.
- 2.1.4 - Missing models produce `model_not_installed` until an explicit
  acquisition job completes. test:
  `tests/ai/local_runtime/test_lmstudio_provider.py` and
  `tests/ai/local_runtime/test_ollama_provider.py`.
- 2.1.5 - Download without a valid caller confirmation fails before network send, one confirmed request proceeds idempotently, and lease-driven load and unload require no confirmation. test: `tests/ai/local_runtime/test_provider_registry.py::test_download_confirmation_enforced_before_send`.

### 2.2 Add managed vLLM and vllm-metal role supervision [category: code] (depends: 2.1)
`kind: deliverable`

Targets:
- `src/gobby/ai/local_runtime/providers/vllm.py`
- `src/gobby/ai/local_runtime/vllm_platform.py`
- `src/gobby/ai/local_runtime/vllm_supervisor.py`
- `src/gobby/ai/local_runtime/process_state.py`
- `tests/ai/local_runtime/test_vllm_provider.py`
- `tests/ai/local_runtime/test_vllm_supervisor.py`
- `tests/ai/local_runtime/test_vllm_platform.py`

Detect preinstalled vllm-metal on Darwin/arm64 and CUDA vLLM on Linux with a
usable NVIDIA runtime. Record executable/version/environment compatibility
without installing packages. Other hosts expose `runtime_unsupported`.

Use exact repository/revision references for acquisition. Resolve a committed
Hugging Face revision and cache path before marking an artifact installed.
Require explicit `allow_remote_code` and render its risk in status. Custom
coding models require a declared or deterministically resolved tool parser.

Supervise one process per distinct loaded role model, sharing a process when
multiple roles select the same artifact and compatible launch settings. Build
direct argv without a shell, create a process group, capture bounded redacted
startup logs, and wait for health/catalog readiness.

Launch runs through one admission fence inside the 2.3 mutation fence, so two
loads never race on allocation: under the fence the supervisor reserves a free
loopback port in the persisted ownership record, persists a `launching` entry
carrying a unique launch nonce, the reserved port, the model, and the profile
hash before spawning, passes the nonce to the child as
`GOBBY_VLLM_LAUNCH_NONCE` in its environment, and after the child binds
checkpoints pid and start identity into the same record. A reserved port is
released only by that record's terminal cleanup.

On restart, adopt only a live process whose pid, executable, start identity,
port, model, and profile hash all match a checkpointed record. A listener on a
reserved port whose process environment carries a known launch nonce but has no
pid checkpoint is Gobby's own orphan: recovery kills its process group and
clears the record. A `launching` record with no checkpointed pid and no
listener on its reserved port (a crash after reservation and before spawn, or
a spawn that never bound) is marked aborted: recovery clears the ownership
record and releases the reserved port idempotently so a later load may reuse
it. Any other foreign or ambiguous listener becomes
`process_conflict`. Pinned roles start during daemon initialization; on-demand
roles start on first lease. Idle owned processes stop through bounded graceful
then forced process-group cleanup.

**Acceptance:**

- 2.2.1 - Platform detection selects vllm-metal on supported Apple Silicon and
  CUDA vLLM on supported Linux/NVIDIA, with typed remedies elsewhere. test:
  `tests/ai/local_runtime/test_vllm_platform.py`.
- 2.2.2 - Supervisor argv, environment, port allocation, remote-code gate,
  parser selection, readiness, and log redaction are deterministic. test:
  `tests/ai/local_runtime/test_vllm_supervisor.py`.
- 2.2.3 - Same-artifact roles share one process; conflicting profiles use
  separate processes; adoption accepts exact owned processes and refuses foreign
  listeners. test: `tests/ai/local_runtime/test_vllm_supervisor.py`.
- 2.2.4 - Idle eviction stops owned process groups while active leases and
  pinned roles prevent shutdown. test:
  `tests/ai/local_runtime/test_vllm_supervisor.py`.
- 2.2.5 - Mutable, unresolved, or cache-mismatched Hugging Face references never become installed; a committed revision and matching cache path retain remote-code and parser gates on the resolved artifact. test: `tests/ai/local_runtime/test_vllm_provider.py`.
- 2.2.6 - Two incompatible loads admitted concurrently receive distinct
  reserved ports; crash injection before spawn, after spawn, after bind, after
  the pid checkpoint, and before readiness converges on restart to exactly
  one of an adopted process, a killed-by-nonce orphan with its port released,
  or an aborted empty reservation with its record cleared and its port
  released and reusable by the next load, and a listener without a known
  nonce remains `process_conflict`. test:
  `tests/ai/local_runtime/test_vllm_supervisor.py`.

### 2.3 Build the unified model job, lease, and control service [category: code] (depends: 2.2)
`kind: deliverable`

Targets:
- `src/gobby/ai/local_runtime/jobs.py`
- `src/gobby/ai/local_runtime/leases.py`
- `src/gobby/ai/local_runtime/service.py`
- `src/gobby/ai/local_runtime/detection.py`
- `src/gobby/runner_init/servers.py::*` — scope-reason: construct the runtime service, assign the runner carrier, and register it for HTTP, WebSocket, and agent consumers
- `src/gobby/runner.py::*` — scope-reason: add the typed `local_runtime_service` carrier field beside `embedding_switch_coordinator`
- `src/gobby/app_context.py::*` — scope-reason: expose the typed service to routes and chat/agent consumers through the shared context
- `src/gobby/runner_lifecycle_local_runtime.py`
- `src/gobby/runner_lifecycle_subsystems.py::*` — scope-reason: add the tracked-subsystem start call into the new lifecycle module; the file is 857 lines and gains only that call
- `src/gobby/runner_lifecycle_shutdown.py::*` — scope-reason: add the stop call into the new lifecycle module from `shutdown_daemon_services`; the file is 916 lines and gains only that call
- `src/gobby/runner_rollback.py::*` — scope-reason: add the same stop call for a partially started service when daemon startup fails
- `src/gobby/servers/routes/local_runtime.py`
- `src/gobby/servers/routes/__init__.py::*` — scope-reason: export the new router through the canonical route package
- `src/gobby/servers/_app_routes.py::*` — scope-reason: register the complete local-runtime HTTP surface
- `tests/ai/local_runtime/test_jobs.py`
- `tests/ai/local_runtime/test_leases.py`
- `tests/ai/local_runtime/test_service.py`
- `tests/servers/routes/test_local_runtime.py`
- `tests/test_app_context.py::*` — scope-reason: cover the typed service carrier
- `tests/test_runner_lifecycle_subsystems.py::*` — scope-reason: cover awaited reconciliation before readiness and pinned-role startup
- `tests/test_runner_shutdown.py::*` — scope-reason: cover idempotent drain/cancel/release on shutdown
- `tests/test_runner_lifecycle.py::*` — scope-reason: cover failed-start rollback stopping owned processes
- `tests/test_runner_init.py::*` — scope-reason: cover the single LocalRuntimeService carrier and safely unconfigured initialization path

Create one `LocalRuntimeService` over the provider registry. It exposes current
family/config, pre-daemon detection, normalized inventory, model inspection,
download/load/unload jobs, role acquisition, lease release, cancellation,
status history, and staged-profile mutation. There is no separate `retry`
operation: resubmitting the same download, load, or unload is the retry.

The service is one daemon subsystem with explicit lifecycle owners. Both
runner initialization paths (`GobbyRunner._initialize_post_database_services`
and `_initialize_runtime_services`) call `init_orchestration` before
`init_servers`, so `runner_init/servers.py` owns construction and
distribution together: it constructs the service, assigns the typed
`GobbyRunner.local_runtime_service` carrier, and places that same instance in
`AppContext` for routes and chat/agent consumers. Earlier phases never touch
the instance; the 4.3 lifecycle wiring in `runner_init/orchestration.py`
installs only the lazy getter `lambda: runner.local_runtime_service`, which
resolves after server initialization. The new
`src/gobby/runner_lifecycle_local_runtime.py` owns `start_local_runtime`
and `stop_local_runtime`: start runs as a tracked subsystem in which restart
reconciliation of persisted jobs, leases, and owned vLLM processes completes,
then pinned roles start, and only then do local-runtime routes, capabilities,
and role acquisition become ready; stop drains subscribers, cancels
queued/running jobs, releases daemon-owned leases, and stops owned processes
idempotently. Stop takes the runner's `ShutdownIntent`: under `RESTART`, the
same intent that preserves active agent runs, every lease owned by a preserved
run and every owned process such a lease references stay persisted and
running, and the next start's reconciliation adopts them (2.2) before
readiness; under `STOP`, run-owned leases are released and every owned
process stops. After a family switch, an old-family process that still serves
a preserved run is retained until that run terminalizes and its lease
releases, then idle cleanup stops it; status reports that process as
`retained for run <id>`. Split that start/stop logic into
`src/gobby/runner_lifecycle_local_runtime.py` rather than growing
`src/gobby/runner_lifecycle_subsystems.py` (857 lines) or
`src/gobby/runner_lifecycle_shutdown.py` (916 lines); those two files,
`runner_rollback.py` (for a partially started service when daemon startup
fails), and `shutdown_daemon_services` each gain only the call into the new
module.

`stage_profile(desired: LocalRuntimeConfig, expected_profile_hash: str,
confirm: bool = False)` is the only public mutation path for family, role
model, coding runtime, and load policy. With `confirm=False` the call
normalizes, diffs, validates, and returns the generation token and typed
requirements without writing pending state; only `confirm=True` stages.
Callers that need user confirmation (5.2, 5.3) preview first and stage only
after the user confirms. `cancel_profile_change(expected_profile_hash)` is the
idempotent reverse of a staged change: before the 3.2 flipping intent is
persisted it discards the pending profile, aborts any open switch (staged
collections are cleaned up), and returns the new token, and a repeated call is
a no-op; after flipping intent it returns `switch_past_cancel_boundary` and
recovery completes the switch. Cancellation and the coordinator's flipping
intent are two CAS transitions of the same durable pending-profile record
(`prepared -> cancelling` and `prepared -> flipping`) taken under the one
mutation fence, so exactly one wins: a coordinator that loses re-reads the
record and aborts before any irreversible step, and a cancel that loses
returns `switch_past_cancel_boundary`. The coordinator never proceeds from an
in-memory copy of the record. `expected_profile_hash` is the profile
generation token: it hashes the
active profile together with the pending profile and any open switch journal,
so it changes as soon as any staged mutation exists. `stage_profile` normalizes
the desired block, CASes the active and pending state together under one
durable mutation fence, rejects a stale token with `profile_hash_stale`, and
rejects a different desired profile while a staged change or switch is open
with `profile_change_in_progress` (the caller cancels or completes that change
first). It returns the normalized diff, the new generation token, and typed
requirements: `restart_required`, `embedding_migration_required`, and the
acquisition jobs still needed. A change to the family or to the normalized
embedding identity (including a same-family embedding model change and a
cloud/local `source` change) is handed to the 3.2 coordinator; every other
change uses pending-profile validation and atomic restart-bound promotion.
Replaying an identical request with the same token is idempotent and returns
the recorded result. Download, load, and unload jobs never read or write active
or pending config.

Jobs use stable ids and states `queued`, `checking`, `downloading`,
`loading`, `unloading`, `succeeded`, `failed`, and `cancelled`. `succeeded`
is operation-independent and carries a typed result (`installed`, `loaded`
with the instance or process id, or `unloaded`), so download, load, and unload
share one terminal vocabulary across persistence, restart reconciliation, API,
CLI, and UI. Single-flight identity is at
least as specific as the resource it produces: download jobs key on exact
artifact identity (family, normalized model identity, digest/revision); load
jobs key on that artifact plus the 2.2 supervisor compatibility identity
(launch settings, tool parser, context cap, profile hash), so two concurrent
loads of one artifact with incompatible launch profiles are separate jobs and
separate processes; unload jobs key on the owned process or loaded-instance id.
That key coalesces active work only: at most one job per key may occupy
`ACTIVE_JOB_STATES`, a creation request whose key matches that active job
returns it, and a terminal job never answers a later request. A submission
made after the previous attempt reached `succeeded`, `failed`, or `cancelled`
therefore creates a new attempt, so a failed download, load, or unload is
retried by submitting it again. A redundant resubmission is cheap and
side-effect-free because every job begins in `checking`: a download whose
artifact is already installed at the confirmed digest, a load whose target
instance is already loaded under the same compatibility identity, and an
unload whose instance or process is already absent each pass from `checking`
straight to `succeeded` with the matching typed result and no provider
mutation. That is also what makes a replay after a lost response harmless once
the original job has terminalized, so no caller-supplied request id or
consumed-response record is needed. `checking` closes the remaining overlap
window with the evidence restart reconciliation already uses: when the
provider reports an in-flight matching operation — a provider-owned download
of that artifact, or an owned process starting under that compatibility
identity — the new job adopts it and advances to the corresponding active
state instead of issuing a second provider call, so even a resubmission that
follows an attempt whose provider outcome after send was unknown joins the
existing work rather than duplicating it.
Persist machine-local job/ownership checkpoints under the Gobby runtime-state
directory with atomic writes; reconcile provider-owned downloads and exact vLLM
processes after restart. Cancellation is intent followed by confirmed
quiescence, because releasing a single-flight key while the provider mutation
is still running would let the next submission overlap it: `cancel` stops
Gobby's wait and subscribers immediately and invokes a provider cancel
operation only when advertised, and the job stays in its current active state
holding its key until quiescence is confirmed — the provider acknowledges the
cancel, reports the operation absent, or the owned process is confirmed
stopped — and only then becomes `cancelled`. A cancel-requested job reports
that intent in its status and job list rows, a resubmission during that window
returns the still-active job, and a job whose provider offers no cancel and no
quiescence evidence stays active until its own operation terminalizes on the
provider's outcome; a restart during the window resolves it from that same
operation's evidence like any other job in `ACTIVE_JOB_STATES`.

Artifact-acquisition (download) job creation carries the exact artifact
identity and `confirm: bool`. `confirm=False` is the preview: it resolves and
returns the normalized identity, the resolved digest/revision, and the known
size, and creates nothing. A confirmed request is processed in one
job-creation transaction in a fixed order: a single-flight hit on an active
job with that key returns the existing job (this is how a retry sent while the
first attempt is still running, including one after a lost response, stays
idempotent); otherwise an identity whose resolved digest/revision differs from
the one the caller previewed is rejected with `confirmation_stale`; otherwise
the job is persisted with `confirmed=True` and the confirmed identity, and
only such a job reaches the 2.1 adapter's `download`. No issued token, expiry,
or consumed-token record exists: an active-job hit makes a confirmed retry
idempotent, the `checking` short-circuit makes a resubmission after
terminalization harmless, and the digest/revision binding detects a changed
artifact. Because a resubmission after a terminal attempt is an ordinary new
request, it carries its own `confirm=False` preview and `confirm=True`
confirmation under the same rules; no previously confirmed identity authorizes
it. CLI `--yes` (5.2) and the Settings dialog (5.3) preview first and resubmit
the previewed identity with `confirm=True`.

Persisted job state is bounded by fixed constants in `jobs.py`: each job keeps
a ring of its most recent 256 events; terminal jobs are retained for 7 days or
the most recent 200, whichever prunes first; nonterminal jobs, their
idempotency keys, and ownership records are never pruned, and their number is
bounded at admission instead. `jobs.py` declares
`ACTIVE_JOB_STATES = {queued, checking, downloading, loading, unloading}` and
`MAX_ACTIVE_JOBS = 32` caps the count of jobs in any of those states; a
request that would create a job above the cap is rejected with the
typed `job_capacity_exceeded` before anything is persisted (an idempotency
hit still returns the existing job), and cancellation is the operator remedy
for a job whose provider stops making progress. Restart reconciliation
resolves every job in `ACTIVE_JOB_STATES` from its own operation's evidence,
so a crash never leaves a nonterminal job that nothing drives: a `queued` or
`checking` job dispatched nothing and fails with `orphaned_after_restart`; a
`downloading` job adopts a matching provider-owned download and otherwise
fails with `orphaned_after_restart`; a `loading` job succeeds when the
provider reports the target loaded instance or an adopted owned process with
matching identity and otherwise fails; an `unloading` job succeeds when the
instance or process is confirmed absent and otherwise fails. A job's idempotency
record lives and dies with the job it references: it is pruned in the same
write that prunes its terminal job, so no key outlives its job and the
idempotency index is bounded by the same window. Uniqueness on that key is
enforced only across `ACTIVE_JOB_STATES`, so the retained terminal jobs may
repeat a key as history, and a submission matching only terminal jobs — or one
whose key was already pruned — creates a new attempt. Pruning runs at
every checkpoint write and at restart reconciliation before any job is
replayed. The job list endpoint, CLI `jobs`, and the Settings job table page
through `limit` (default 50, maximum 200) and an opaque cursor, and every
status projection carries at most that page.

Leases are keyed by normalized model identity and role. They carry owner,
acquired time, optional deadline, and profile hash. Release starts the idle
TTL, the module-level constant `IDLE_TTL_SECONDS` in `leases.py` (no config
field or operator surface consumes it in 0.5.0); LRU eviction may evict only
zero-lease, on-demand models. A resource
failure evicts idle models and retries once. Leased or pinned models produce
`resource_conflict` with current owners.

Role acquisition is the activation path for a cold on-demand model, so
eligibility evidence that needs a loaded model is gathered inside it rather
than required before it. The probe and eligibility implementations belong to
4.1, which depends on this leaf, so this leaf owns only the activation
contract: `leases.py` defines a `CodingActivationEvaluator` protocol (one
call taking the activated model's identity, runtime context, and runtime,
returning the typed eligibility result or typed failure reasons), and
`LocalRuntimeService.__init__` takes `coding_evaluator:
CodingActivationEvaluator | None = None`. `leases.py` also defines the typed
`RoleAcquisitionRequest` (role, optional explicit model override, optional
runtime override) that every acquisition carries, so an explicit
`local:coding/<model>` selection survives the service boundary instead of
collapsing to the configured role model; 4.3's `LocalRoleSelection` is an
alias of this type re-exported through `local_profiles/contracts.py` for
profile-side consumers, and 4.5 web chat uses the same request.
`acquire_role(request, owner)` is
single-flight per activation identity and cancellation-safe in the same
single-flight shape 4.1 later reuses for probes: it loads or starts the
artifact through the load job, refreshes the 1.2 runtime context, invokes the
injected evaluator exactly once for the coding role, and only then converts
the activation into the owner's lease. The activation identity is the load
job's key — the exact artifact identity plus the 2.2 supervisor compatibility
identity (launch settings, tool parser, context cap, profile hash) — together
with the role, not the display identity: the request's explicit model and
runtime overrides change which artifact and which process the activation
produces, and the evaluator reads runtime, wire, and profile facts, so two
concurrent requests that differ in any of them run separate activations and
separate evaluations exactly as they already run separate load jobs and
processes. Every joiner of one activation therefore shares every fact the
evaluator consumes, which is what keeps a single evaluator invocation correct
for all of them, and each successful waiter converts that shared activation
into its own lease under its own owner, so joining never transfers or shares
one owner's lease. The Constraints rule that missing
runtime controls fail closed applies here: with no evaluator injected, a
coding acquisition is refused after the context refresh with the typed
reason `coding_evaluator_unavailable`, releases the activation, and grants
no lease (the `None` default exists only so the service is constructible
before 4.1 lands; text, vision, and embeddings acquisition never consult it).
A failed evaluation records the evaluator's typed reasons, releases the
activation, and lets the load policy decide whether the model unloads; a
cancelled waiter never cancels the shared activation. `text`, `vision`, and
`embeddings` acquisition use the same path without the evaluator step. 4.1
implements the evaluator (required-wire probe plus eligibility) and wires it
into the daemon-constructed service.

Expose credential-free HTTP endpoints for detection/config status, inventory,
job creation/status/cancel, role acquire/release diagnostics, staged-profile
preview/mutation/cancel, and switch status. Model deletion has no endpoint.

**Acceptance:**

- 2.3.1 - Installer and daemon compositions share detection/provider contracts
  and produce identical normalized records. test:
  `tests/ai/local_runtime/test_service.py`.
- 2.3.2 - Concurrent identical work joins one active job; concurrent loads of
  one artifact with incompatible launch profiles run as separate
  jobs/processes; restart reconciliation, resubmission, cancellation, and
  status history preserve typed terminal states; a cancel against a provider
  that advertises no cancel, one whose acknowledgement is delayed, and one
  against a starting owned process each leave the job active and holding its
  key with cancel intent visible, a resubmission during that window returns
  that same job with no second provider send, and the job reaches `cancelled`
  only once quiescence is confirmed; unload jobs pass through
  `unloading` to `succeeded` with an `unloaded` result. test:
  `tests/ai/local_runtime/test_jobs.py`.
- 2.3.3 - Leases protect active/pinned models, idle TTL evicts only eligible
  models, and one bounded resource retry occurs. test:
  `tests/ai/local_runtime/test_leases.py`.
- 2.3.4 - HTTP responses expose no secrets, raw auth URLs, or unbounded logs.
  test: `tests/servers/routes/test_local_runtime.py`.
- 2.3.5 - Staged-profile mutation rejects stale tokens, replays identical
  requests idempotently, routes family changes, same-family embedding-model
  changes, and cloud/local source changes to the 3.2 coordinator, routes other
  role/runtime/load-policy edits to restart-bound promotion, and leaves
  active/pending config untouched across download-only operations. test:
  `tests/ai/local_runtime/test_service.py` and
  `tests/servers/routes/test_local_runtime.py`.
- 2.3.6 - Two clients racing distinct profiles with the same token before,
  during, and after each irreversible switch phase: exactly one wins, the
  other receives `profile_change_in_progress` or `profile_hash_stale`, and
  pending state is never overwritten or promoted by the loser. test:
  `tests/ai/local_runtime/test_service.py`.
- 2.3.7 - Routes and role acquisition are unavailable until reconciliation and
  pinned startup complete; a `STOP` shutdown and failed-start rollback cancel
  jobs, release daemon-owned and run-owned leases, and stop owned processes
  exactly once. test:
  `tests/test_runner_lifecycle_subsystems.py`, `tests/test_runner_shutdown.py`,
  and `tests/test_runner_lifecycle.py`.
- 2.3.8 - `stage_profile(confirm=False)` returns diff, token, and requirements
  without changing active or pending state; declining after a preview leaves a
  different profile stageable; `cancel_profile_change` discards a staged change
  idempotently before flipping intent and returns
  `switch_past_cancel_boundary` after it; a cancel and a flipping-intent write
  started simultaneously produce exactly one durable winner, with the losing
  coordinator aborting before any alias or config write and the losing cancel
  reporting `switch_past_cancel_boundary`. test:
  `tests/ai/local_runtime/test_service.py` and
  `tests/servers/routes/test_local_runtime.py`.
- 2.3.9 - Under the real initialization order (`init_orchestration` before
  `init_servers`) one LocalRuntimeService instance is constructed by
  `init_servers`, carried by GobbyRunner, and exposed through AppContext to
  route, chat, and agent consumers, while the orchestration-installed getter
  resolves that same instance afterwards; the service remains safely absent
  when local runtime is unconfigured. test: `tests/test_app_context.py` and
  `tests/test_runner_init.py`.
- 2.3.10 - A `RESTART` shutdown with a live run-owned lease keeps that lease
  and its owned process persisted and running, the next start adopts both
  before readiness, and an old-family process serving a preserved run after a
  family switch is retained until the run terminalizes and then stopped by
  idle cleanup. test: `tests/test_runner_shutdown.py` and
  `tests/ai/local_runtime/test_service.py`.
- 2.3.11 - With a fake `CodingActivationEvaluator` injected, acquiring the
  coding role for an installed, unloaded model loads it, refreshes runtime
  context, invokes the evaluator exactly once with the activated facts, and
  converts to the owner's lease; an evaluator failure releases the activation
  with its typed reasons and no lease; with no evaluator a coding acquisition
  is refused with `coding_evaluator_unavailable` and no lease while a text
  acquisition on the same service converts normally; a request carrying an
  explicit model override activates that exact model and one carrying a
  runtime override activates under that runtime, while a request with neither
  uses the configured role model; concurrent acquirers
  whose requests resolve to the same artifact and compatibility identity join
  one activation, invoke the evaluator once, and each receive their own lease
  under their own owner, while two concurrent acquirers differing only in
  runtime override run separate activations, separate evaluations, and
  separate processes; a cancelled waiter leaves the shared activation
  running. test:
  `tests/ai/local_runtime/test_service.py` and
  `tests/ai/local_runtime/test_leases.py`.
- 2.3.12 - Above 256 events a job keeps its latest 256; above 200 terminal
  jobs or 7 days the oldest terminal jobs are pruned together with their
  idempotency records while jobs in `ACTIVE_JOB_STATES`, their keys, and
  ownership records survive, so the persisted job and idempotency state stay
  within fixed bounds after any number of unique completed requests; a replay
  whose key was pruned creates a new job; the 33rd unique request while 32
  jobs occupy `ACTIVE_JOB_STATES` (counting `checking`, `loading`, and
  `unloading` jobs, not only queued and downloading ones) is rejected with
  `job_capacity_exceeded` and persists nothing while an idempotent replay of
  an active job still returns it; restart injected in each of the five
  nonterminal states resolves that job from its own operation's evidence — a
  queued or checking job and a downloading job with no matching provider
  download fail `orphaned_after_restart`, a loading job whose target loaded
  instance or adopted process matches succeeds and otherwise fails, and an
  unloading job succeeds only on confirmed absence; list endpoints reject
  `limit` above 200 and page by cursor; restart reconciliation prunes before
  replay. test: `tests/ai/local_runtime/test_jobs.py` and
  `tests/servers/routes/test_local_runtime.py`.
- 2.3.13 - Direct HTTP artifact-acquisition job creation with `confirm=False`
  returns the normalized identity, resolved digest/revision, and known size
  and creates no job; a request without confirmation is rejected before
  provider send; a confirmed request whose previewed digest/revision no
  longer matches is rejected with `confirmation_stale` before provider send;
  one confirmed request creates a job carrying `confirmed=True`, and the same
  request resubmitted after a lost response while that job is still active
  returns it and triggers no second adapter send. test:
  `tests/servers/routes/test_local_runtime.py::test_download_job_requires_confirmation`.
- 2.3.14 - Resubmitting a download, load, or unload after its previous attempt
  reached `failed` or `cancelled` creates a new attempt that runs the
  operation, while the same submission during an active attempt returns that
  attempt; a download resubmission carries its own preview and confirmation
  and is rejected with `confirmation_stale` when the refreshed
  digest/revision differs; a resubmission after `succeeded` creates a new
  attempt that passes from `checking` to `succeeded` with the matching typed
  result and issues no provider download, load, or unload; and a resubmission
  made after an attempt failed with an unknown post-send outcome adopts the
  provider's still-running download or starting owned process from `checking`
  and issues no second provider call. test:
  `tests/ai/local_runtime/test_jobs.py` and
  `tests/servers/routes/test_local_runtime.py`.

## P3: Role Routing and Atomic Family Switching
`kind: framing`

**Goal:** Route daemon capabilities through role leases and integrate local
embedding changes with recoverable family activation.

### 3.1 Route text, coding tools, vision, and embeddings through roles [category: code] (depends: P2)
`kind: deliverable`

Targets:
- `src/gobby/ai/local_runtime/role_router.py`
- `src/gobby/ai/embedding_binding.py`
- `src/gobby/ai/registry_builder.py::*` — scope-reason: replace local endpoint bindings with role-derived capability bindings while preserving cloud bindings
- `src/gobby/ai/_text_generation_builder.py::*` — scope-reason: resolve local text candidates through role/model leases before transport construction
- `src/gobby/ai/_tool_chat_builder.py::*` — scope-reason: resolve local coding tool-chat while preserving bounded ToolPolicy execution
- `src/gobby/ai/vision.py::*` — scope-reason: acquire and validate the vision role before image extraction
- `src/gobby/ai/embeddings.py::*` — scope-reason: move config binding out of the 992-line module and consume leased resolved embeddings
- `src/gobby/runner_init/local_runtime.py`
- `src/gobby/runner_init/services.py::*` — scope-reason: move local embedding and runtime assembly out of the 850-line service module
- `tests/ai/local_runtime/test_role_router.py`
- `tests/ai/test_capability_registry.py::*` — scope-reason: cover cloud/local capability composition and unavailable reasons
- `tests/ai/test_text_generation.py::*` — scope-reason: cover local text leases and existing fallback ordering
- `tests/ai/test_tool_chat_builder.py::*` — scope-reason: cover role resolution and preserved bounded gcode tool policy
- `tests/ai/test_embedding_binding.py`
- `tests/ai/test_vision_extraction.py::*` — scope-reason: cover vision-role lease acquisition and release around extraction
- `tests/search/test_embedding_provider_policy.py::*` — scope-reason: re-point provider resolution coverage at the moved binding module
- `tests/search/test_embeddings_availability.py::*` — scope-reason: re-point configured/reachable coverage at the moved binding module
- `tests/test_runner_init.py::*` — scope-reason: cover the extracted local runtime and embedding service assembly
- `src/gobby/ai/_text_generation_service.py::*` — scope-reason: enforce typed pre-send-only candidate advancement in result and JSON generation loops
- `src/gobby/ai/_text_generation_attempts.py`
- `src/gobby/mcp_proxy/semantic_search.py::SemanticToolSearch.search_tools`
- `src/gobby/search/backends/embedding.py::EmbeddingBackend.search_async`
- `src/gobby/servers/routes/embeddings.py::generate_embedding_batch`
- `tests/servers/routes/test_embeddings_routes.py::*` — scope-reason: cover the HTTP embeddings route holding the shared generation binding and local lease across the complete batch
- `src/gobby/mcp_proxy/registries.py::build_skill_search`
- `src/gobby/skills/search.py::*` — scope-reason: every member that participates in the published index snapshot changes together — construction, the single-flight reindex and its generation-checked publication, both search readers, the keyword and filter helpers, the indexed/mode/fallback state accessors, the incremental add/update/remove mutations, stats, and clear
- `src/gobby/search/unified.py::UnifiedSearcher.__init__`
- `src/gobby/search/unified.py::UnifiedSearcher._get_embedding_backend`
- `src/gobby/search/unified.py::UnifiedSearcher.fit_async`
- `src/gobby/search/backends/embedding.py::EmbeddingBackend.__init__`
- `src/gobby/search/backends/embedding.py::EmbeddingBackend.fit_async`
- `src/gobby/mcp_proxy/tools/skills/search_skills.py::*` — scope-reason: await skill index generation on the daemon loop so the shared binding's guard and lease hold across it
- `tests/skills/test_skills_search.py::*` — scope-reason: cover shared local embedding binding, guarded index generation, and keyword-fallback prevention
- `tests/mcp_proxy/tools/skills/test_search_skills.py::*` — scope-reason: cover the real tool's daemon-loop index generation and its snapshot-coherent results

Map local roles to canonical AI capabilities exactly as stated in Constraints.
A configured local candidate acquires its model before adapter creation and
releases after the request or bounded tool loop. Readiness failure may advance an
existing candidate chain only before inference send. The result and JSON
candidate loops in `src/gobby/ai/_text_generation_service.py` enforce that
rule: a local candidate returns a typed attempt outcome, the loop continues
past it only for definite pre-send unavailability (lease or readiness failure,
connection refused), and a timeout after send, an invalid or unparsable
response, or an unknown send state ends the chain with that candidate's error.
Cloud candidates keep their current fallback behavior (3.1.2).
`_text_generation_service.py` is 864 lines, so the typed attempt outcome,
the send-state classification (pre-send unavailable, sent, unknown), and the
advance-or-stop decision move into the new
`src/gobby/ai/_text_generation_attempts.py` before any local semantics are
added; each of the service's two loops gains only the call into that module
and the file ends below its current size.

Move embedding binding/config resolution (`_resolve_embedding_provider`,
`_is_embedding_configured`, `_is_embedding_reachable`, and the local reload /
LM Studio recovery helpers) from the near-ceiling embedding module into
`embedding_binding.py`, and delete `_strip_local_embedding_prefix` with its
removed `local:lm-studio/` and `local:ollama/` shapes. `embedding_binding.py`
also owns the in-process embedding generation gate: every daemon-internal
embed-and-search request holds its shared side for the request's duration, so
such a request never straddles the 3.2 publication of a new
embedder/collection tuple, and the 3.2 boundary acquires its exclusive side to
publish one. The outermost request owners that split embedding from
collection or ranking access — `SemanticToolSearch.search_tools` and
`EmbeddingBackend.search_async` — hold one shared generation binding across
both halves; their signatures and return shapes are unchanged, so their callers
(`mcp_proxy/services/fallback.py`, `search/backends/__init__.py`,
`tests/mcp_proxy/test_semantic_search.py`) stay untouched. The daemon's
`/api/embeddings` route (`generate_embedding_batch`) stops building its own
`EmbeddingService.from_config(config.embeddings)` — that path cannot serve a
local source whose persisted `api_base` and `api_key` are null — and resolves
the shared binding from `AppContext` instead, holding the shared side and the
local embeddings-role lease across the complete batch and releasing both on
success, error, timeout, and cancellation. Its cloud routing, request payload,
and response shape are unchanged. The route holds nothing across the caller's
subsequent Qdrant search, so the out-of-process window that leaves is
documented in 3.2. The skills registry is the third such consumer, and it
generates embeddings before it searches them: `build_skill_search` constructs
`SkillSearch` from `EmbeddingsConfig.model`, `api_base`, and `api_key`, and
`SkillSearch.index_skills_async` reaches `UnifiedSearcher.fit_async` and
`EmbeddingBackend.fit_async`, each of which builds its own `EmbeddingService`
from those same fields — the path a local source, whose persisted `api_base`
and `api_key` are null, degrades to keyword search on. `build_skill_search`
resolves the shared binding through the same `embedding_binding.py` accessor
the role router uses instead of passing connection fields, `SkillSearch` and
`UnifiedSearcher` carry that binding to `EmbeddingBackend`, and index
generation holds one shared generation guard and the local embeddings-role
lease across the complete `fit_async`, so a 3.2 publication waits for a
coherent in-memory index instead of landing mid-fit.

That guard orders reindexing against profile publication and does not order
reindexing against searches or against another reindex, which
`index_skills_async` requires because it clears and repopulates `_skill_names`
and `_skill_meta` in place before awaiting `fit_async` and only then sets
`_indexed`: a concurrent search reads new metadata against an unfitted or
previous backend, a raised fit leaves swapped metadata behind stale indexed
state, and two overlapping fits can publish out of order. `SkillSearch`
therefore owns one reindex single-flight with a monotonic generation counter.
Each reindex captures the generation it was issued under, builds its candidate
metadata and a candidate `UnifiedSearcher` off to the side, fits that candidate
under the shared guard and lease, and publishes both through one atomic
assignment only while the captured generation is still current. A fit that
raises or is superseded discards its candidate and leaves the prior snapshot
intact. The candidate instance is the snapshot, so `UnifiedSearcher`'s
internals stay as they are.

Publishing atomically is only half of it, because `search_async` today reads
`_indexed` and the searcher before its await and then reaches `_passes_filters`
and `_skill_names` after it, so a publication landing inside that await pairs
one generation's ranking with another's metadata. Every reader therefore
captures the published snapshot once, at entry, and uses only that captured
value for the rest of the call: the indexed check, the ranking backend, the
keyword fallback, the filter helper, and the name and metadata lookups all read
the same generation, so a search issued before a publication returns wholly
previous-generation results and one issued after returns wholly new-generation
results. `clear` and the incremental `add_skill`, `update_skill`, and
`remove_skill` mutations publish through the same assignment rather than
editing a live snapshot, which makes an empty publication as coherent as a
fitted one, and the state accessors and `get_stats` report the captured
snapshot's facts. Holding that
guard requires the generation to run on the daemon loop, and today it does
not: `search_skills.py`'s indexer is synchronous and reaches
`SkillSearch.index_skills`, whose wrapper runs `index_skills_async` under a
nested `asyncio.run` on a `run_db` worker thread, where a loop-bound guard and
lease cannot be observed. The `search_skills` tool therefore awaits an async
indexer on the daemon loop — `run_db` still reads the skills, and only the
generation moves onto the loop — and the synchronous `index_skills` wrapper
survives unchanged for the keyword-mode callers outside the daemon
(`tests/skills/`, `tests/storage/test_dialect_parity.py`) that never generate
embeddings. `SkillSearch` is the only
production constructor of `UnifiedSearcher`, and `build_skill_search`,
`index_skills_async`, `fit_async`, and `search_async` all keep their
signatures, so the registry's `resolve_skill_search` cache, `skills/manager.py`
(which constructs a `SkillSearch` it never embeds with), and the
`search/__init__.py` and `search/backends/__init__.py` module docstrings and
protocol re-exports keep their code, and cloud sources keep their current
construction. Move
local runtime and
embedding service assembly from the near-ceiling runner services module into
`runner_init/local_runtime.py`.

Keep the daemon tool-chat isolation contract: native Codex/Droid tools remain
disabled in the ephemeral adapter, requested gcode subcommands remain explicit
dynamic tools, and all budget/result controls stay in force.

Short-context models may bind `text_generate`. The coding role requires later
eligibility and remains unavailable until proven. Vision requires image input;
a shared text/coding VLM may also satisfy vision through one underlying lease.

**Acceptance:**

- 3.1.1 - Each local role produces only its declared capability bindings and
  uses the selected normalized model. test:
  `tests/ai/local_runtime/test_role_router.py`.
- 3.1.2 - Short-context text generation remains routable while coding stays
  unavailable; cloud candidate order and generic endpoint behavior remain
  stable. test: `tests/ai/test_text_generation.py`.
- 3.1.3 - Tool chat can execute allowlisted gcode dynamic tools and cannot reach
  disabled native tools or undeclared commands. test:
  `tests/ai/test_tool_chat_builder.py`.
- 3.1.4 - Embedding and vision leases cover the full request/batch and release
  on success, error, timeout, or cancellation. test:
  `tests/ai/test_embedding_binding.py` and `tests/ai/test_vision_extraction.py`.
- 3.1.5 - The three split source files (`embeddings.py`,
  `runner_init/services.py`, `_text_generation_service.py`) each end below
  their pre-change line count and below 1,000 lines, and new behavior lives
  in the declared focused modules. behavior: scoped line-count audit in the
  task transcript.
- 3.1.6 - A request holding the generation gate completes against its original
  embedder and collections while a concurrent publication waits, and the
  publication proceeds once the request releases. test:
  `tests/ai/test_embedding_binding.py`.
- 3.1.7 - Text and JSON candidate chains advance past a local candidate only after definite pre-send unavailability; timeout-after-send, response validation, parse failure, and unknown send state on a local candidate never invoke another candidate, while cloud candidate fallback is unchanged. test: `tests/ai/test_text_generation.py`.
- 3.1.8 - Daemon-internal semantic tool search and embedding-backend search hold one shared generation across embedding and collection or ranking access, and switch publication waits while either request is paused between them. test: `tests/ai/test_embedding_binding.py::test_daemon_internal_search_holds_generation_gate`.
- 3.1.9 - The daemon embeddings route resolves the shared binding from AppContext, holds its shared generation guard and local embedding lease for the full batch, releases both on success, error, timeout, and cancellation, and leaves cloud routing unchanged. test: `tests/servers/routes/test_embeddings_routes.py::test_embeddings_route_holds_generation_binding_and_local_lease`.
- 3.1.10 - Skill-search construction and index generation resolve the shared local embedding binding despite null persisted api_base/api_key, hold one generation guard and embeddings-role lease across fit on the daemon loop rather than a nested `asyncio.run`, and block switch publication until the in-memory index is coherent; keyword-mode construction outside the daemon still indexes without a binding. test: `tests/skills/test_skills_search.py::test_local_binding_guards_index_generation`.
- 3.1.11 - Skill reindexing publishes one coherent generation: a search issued
  while a fit is in flight returns results from the previous successful
  snapshot with matching metadata, a fit that raises leaves that prior snapshot
  and its indexed state intact, and two overlapping reindexes leave the
  snapshot from the later generation regardless of which fit completes last;
  through the real `search_skills` tool with index generation on the daemon
  loop, a search paused between ranking and metadata lookup while a
  publication lands still resolves every returned id against its own captured
  snapshot, and a publication that clears the index leaves an in-flight search
  on the prior snapshot while the next search sees the empty one.
  test: `tests/skills/test_skills_search.py::test_reindex_publishes_one_generation`
  and `tests/mcp_proxy/tools/skills/test_search_skills.py`.

### 3.2 Extend the embedding journal into atomic family activation [category: code] (depends: 3.1)
`kind: deliverable`

Targets:
- `src/gobby/ai/local_runtime/switch.py`
- `src/gobby/ai/embedding_catalog.py::*` — scope-reason: bind catalog entries to normalized provider artifact identities and compatibility facts
- `src/gobby/ai/embedding_switch.py::*` — scope-reason: carry target family/profile identity through journal and completed proof
- `src/gobby/ai/embedding_switch_service.py::*` — scope-reason: prepare and coordinate local family targets through the public switch lifecycle
- `src/gobby/ai/embedding_switch_runner.py::*` — scope-reason: lease the target model and commit the family-aware flip/recovery phases
- `src/gobby/config/bootstrap_io.py::*` — scope-reason: atomically stage and activate pending local-runtime bootstrap blocks
- `src/gobby/memory/vectorstore_client.py::*` — scope-reason: add one batched `repoint_aliases` that submits every alias change in a single `update_collection_aliases` call
- `src/gobby/memory/vectorstore.py::*` — scope-reason: expose the batched repoint on the store facade beside `create_alias`/`get_aliases`
- `crates/gcore/assets/config/runtime_config_contract.json::*` — scope-reason: regenerate the derived carrier after pending-profile bootstrap changes
- `src/gobby/cli/installers/embedding.py::*` — scope-reason: stage bootstrap `local_runtime` and `source="local"` for local families instead of writing derived embedding fields
- `tests/config/test_runtime_config_contract.py::test_checked_in_contract_matches_registry`
- `tests/cli/installers/test_embedding_installer.py::*` — scope-reason: cover the local-family installer branch staging bootstrap only while the cloud branch is unchanged
- `tests/memory/test_vectorstore.py::*` — scope-reason: cover the batched alias repoint submitting one atomic operation set
- `tests/ai/test_embedding_switch.py::*` — scope-reason: cover family-aware journal construction and completion
- `tests/ai/test_embedding_switch_daemon_lifecycle.py::*` — scope-reason: cover crash recovery and active/pending family transitions
- `tests/ai/test_embedding_switch_runner.py::*` — scope-reason: cover target leases, vector rebuild, flip, and rollback behavior
- `src/gobby/config/bootstrap.py::*` — scope-reason: add typed pending local-profile parsing, serialization, and promotion required by bootstrap_io
- `tests/config/test_bootstrap.py::*` — scope-reason: cover pending-profile round trips, invalid shapes, promotion, and clearing

Add active/pending local profile hash, the 2.3 profile generation token, and
normalized target embedding identity to the switch journal and completed proof.
The 2.3 `stage_profile` operation is the only caller that prepares a family or
embedding-identity change; it hands the validated desired profile and its
generation token to this coordinator, and the open journal is what makes the
token stale for every other client until the switch completes or is cancelled.
A family change prepares all configured role models first. If embedding
identity changes, stage target collections, embed from the leased target
model, replay projection changes, and record the old physical names while
current routing stays active.

One coordinator method owns the irreversible boundary: acquire the 3.1
generation gate exclusively, persist flipping intent, repoint every active
alias in one batched `update_collection_aliases` operation
(`repoint_aliases`) so readers observe the complete old set or the complete
new set and never a mixture, then commit embedding config/proof (`model`,
`dim`, `query_prefix`, `catalog_key`, and `source` in one structural switch
write), rebind the live embedder to the leased target model through the
existing `reconcile_local_commit` path, atomically promote the pending
bootstrap profile, mark completion, and release the gate. Config/proof are
written only after the batch succeeds. Every step is idempotent. Recovery
compares journal, aliases, completed proof, and bootstrap hashes and converges
forward or preserves the old profile, and it always leaves the gate released.
It never exposes a half-active family.

Collection retention keys on the 1.2 verified `compatibility_identity`, so a
change in any vector-affecting preprocessing fact — the document prefix
included — forces a full switch even at equal artifact and dimensions, and an
unverified identity never retains collections. `query_prefix` affects only the
query path, so a query-prefix-only change retains collections and its new
query contract is promoted in the same structural switch write that commits
the other derived fields.

A family change with cloud embeddings or unchanged exact embedding identity
skips vector rebuild but still uses pending-profile validation and atomic
promotion. Successful preparation returns `restart_required=true`. After the
flip the live daemon serves `embed` from the target family's leased embeddings
model (the target lease is retained until restart promotes the profile) while
`text`, `coding`, and `vision` stay on the old family until restart; that
mixed window is the documented state between flip and restart and status
reports it as `pending restart`. `gcode`/`gwiki` embed through the daemon's
`/api/embeddings` route and then search Qdrant by alias in a separate
out-of-process operation, so the 3.1 gate cannot bind those two steps: one
out-of-process query whose embed call completed before the flip and whose
search runs after it may fail with a Qdrant vector-dimension error or return
one result set from the prior generation. That single-query window during an
operator-initiated switch that already requires a restart is an accepted,
documented limitation of this plan; the crate readers, effective-config
path, and alias resolution are unchanged, and 6.2 records the limitation in
the migration contract.

The installer's local-family branches in `src/gobby/cli/installers/embedding.py`
(`lmstudio`, `ollama`, `vllm`) stop writing `model`, `dim`, `query_prefix`, and
`catalog_key`: they write the `local_runtime` bootstrap block (provider plus
`embeddings` role) through `bootstrap_io` and stage `source="local"` with null
`api_base`/`api_key` through `set_embedding_bootstrap_values`. The first daemon
start's 2.3 reconciliation finds `source="local"` without completed proof and
runs this coordinator's activation (an identity switch from no collections, so
no rebuild) before the embedding route reports configured. Cloud,
`openai-compatible`, and `none` branches are unchanged.

`src/gobby/ai/embedding_switch_runner.py` is 839 lines. The new family-aware
phases (target-model lease, pending-profile staging, promotion, and recovery
comparison) live in `src/gobby/ai/local_runtime/switch.py`; the runner gains
only the calls into that coordinator and stays below the ceiling.

**Acceptance:**

- 3.2.1 - Same-name/dimension models with different provider or artifact
  identity trigger a full switch; exact identity retains collections. test:
  `tests/ai/test_embedding_switch.py`.
- 3.2.2 - Download/probe failure leaves active profile, aliases, and embedding
  config unchanged. test:
  `tests/ai/test_embedding_switch_daemon_lifecycle.py`.
- 3.2.3 - Crash injection after every irreversible step converges to one
  complete old or new state without a missing successor/profile. test:
  `tests/ai/test_embedding_switch_daemon_lifecycle.py`.
- 3.2.4 - Cloud-embedding family changes skip vector work and still require
  validated restart-bound promotion. test:
  `tests/ai/test_embedding_switch_runner.py`.
- 3.2.5 - The alias flip submits one `update_collection_aliases` call carrying
  every alias change; a concurrent reader observes only the complete old or
  complete new alias set; a crash between flipping intent and batch success
  converges without writing config/proof; `source` lands in the same
  structural write as the other derived fields. test:
  `tests/memory/test_vectorstore.py` and
  `tests/ai/test_embedding_switch_daemon_lifecycle.py`.
- 3.2.6 - The installer's local-family branch writes only bootstrap and
  `source="local"`; first daemon start activates the embeddings role through
  the coordinator and writes derived fields in one switch write; the cloud
  branch still writes its values directly. test:
  `tests/cli/installers/test_embedding_installer.py` and
  `tests/ai/test_embedding_switch_daemon_lifecycle.py`.
- 3.2.7 - An embedding request started before the flip completes against the
  old embedder and old aliases, a request started after it uses the target
  embedder and new aliases, and a crash between gate acquisition and release
  converges with the gate released. test:
  `tests/ai/test_embedding_switch_daemon_lifecycle.py`.
- 3.2.8 - The checked-in runtime configuration contract remains fresh after active and pending local-profile bootstrap fields are added. test: `tests/config/test_runtime_config_contract.py::test_checked_in_contract_matches_registry`.
- 3.2.9 - Active and pending local profiles parse and round-trip through BootstrapConfig; promotion writes one active profile and clears pending state, and invalid pending envelopes are rejected. test: `tests/config/test_bootstrap.py::test_pending_local_profile_round_trip_and_promotion`.
- 3.2.10 - At equal artifact digest, quantization, and dimensions, a changed
  document prefix or other vector-affecting preprocessing fact triggers a full
  switch with a rebuild, an unverified compatibility identity does the same,
  and a `query_prefix`-only change retains every collection while the new
  prefix lands in the same structural switch write as the other derived
  fields. test:
  `tests/ai/test_embedding_switch.py::test_retention_keys_on_vector_affecting_facts`.

## P4: Multi-CLI Coding Eligibility and Launch Safety
`kind: framing`

**Goal:** Make every local coding route explicit, lean, context-safe, and
independently diagnosable while preserving hosted runtime behavior.

### 4.1 Add transport evidence and structured runtime eligibility [category: code] (depends: P3)
`kind: deliverable`

Targets:
- `src/gobby/ai/local_runtime/eligibility.py`
- `src/gobby/ai/local_runtime/probes.py`
- `src/gobby/ai/endpoint_activation.py::*` — scope-reason: replace one flat tool probe with transport-keyed activation evidence
- `tests/ai/local_runtime/test_eligibility.py`
- `tests/ai/local_runtime/test_probes.py`
- `tests/ai/test_endpoint_activation.py::*` — scope-reason: cover persisted Chat, Responses, and Messages probe evidence
- `src/gobby/runner_init/servers.py::*` — scope-reason: pass the 4.1 coding evaluator into the 2.3 service constructor
- `tests/ai/local_runtime/test_service.py`
- `tests/test_runner_init.py::*` — scope-reason: cover the daemon-constructed service carrying the 4.1 evaluator

Define:

```python
class RuntimeEligibilityReason(TypedDict):
    code: str
    message: str

class RuntimeEligibility(TypedDict):
    eligible: bool
    state: Literal["eligible", "unproven", "ineligible"]
    context_window: int | None
    profile: str | None
    reasons: list[RuntimeEligibilityReason]
```

Compute one entry for each installed coding runtime. `state` is `unproven`
when the model is installed but the facts that need a loaded model (runtime
context, required-wire probe evidence) are absent and no check has failed;
`eligible` only when every check passed; `ineligible` when any check failed.
`eligible` is true only for `eligible`. Selection surfaces (5.4 picker,
direct web-chat selection, spawn) admit `unproven` models and let 2.3 role
acquisition activate and prove them on first use; `ineligible` models are
refused with their reasons. `eligibility.py` implements the 2.3
`CodingActivationEvaluator`: given the activated model's identity, runtime
context, and runtime, it runs the required-wire probe through `probes.py`
when no evidence exists for the key, evaluates every check below, and returns
the `RuntimeEligibility` or its typed reasons. `runner_init/servers.py`
passes that evaluator to the `LocalRuntimeService` constructor, so the
daemon-constructed service proves a cold coding model inside `acquire_role`
from this leaf onward. Require the selected
model's 1.2 `effective_context` `>= 65_536`; the canonical value alone never
qualifies a model whose runtime context is lower or unknown. Context errors
include model id, effective value (`missing` when unknown, with the canonical
and runtime values that produced it), required value, provider family, and the
provider remedy. `context_window` in `RuntimeEligibility` is the effective
value. Validate runtime/profile support independently.

Activation probes each distinct wire supported by the family/model once and
stores typed evidence for `openai_chat_completions`, `openai_responses`, and
`anthropic_messages`. Runtime adapters declare their required wire. A failed
tool probe and a failed context/profile check append separate stable codes;
ordering is deterministic and no condition overwrites another.

Evidence is keyed by normalized model identity, wire, endpoint/config
fingerprint, tool-parser or launch-profile hash, and adapter/runtime version.
A lookup with an unchanged key reuses stored evidence; any changed component
requires a fresh probe. A successful probe atomically replaces the evidence for
its exact key. A failed fresh probe records failure for that key and never falls
back to an older success. The store is bounded by supersession: `probes.py`
keeps exactly one record (success or failure) per normalized model identity
and wire, and the write for a key deletes every other record for that model
identity and wire in the same atomic write, so endpoint, parser/profile,
artifact, or runtime-version churn replaces evidence instead of accumulating
it. Supersession follows configuration order rather than completion order:
each model/wire slot carries a monotonic observation generation, a probe
records the generation it observed when it started, and its write is a CAS
that persists only while that generation is still current. A probe for an
obsolete key that finishes after a newer one returns its result to its own
waiters and stores nothing, so current evidence is never replaced by a stale
completion and no redundant probe is provoked. Startup reconciliation prunes records whose model is no longer
installed before the first eligibility projection. No diagnostic history of
superseded evidence is retained.

Within one daemon process, a probe is sent at most once per evidence key per
attempt. Probe sends bypass the generic activation retry wrapper: only definite
pre-send failures (connection refused, DNS, TLS handshake) retry; a timeout or
any failure after request bytes were written records failure for that key and
is never resent on the same or another wire. `probes.py` joins concurrent
lookups for one key on a single in-flight probe, cancellation-safe: a cancelled
waiter never cancels the shared probe. The registry and its cancellation state
are in-process only: a probe cancelled or lost to a daemon crash after bytes
were written leaves no evidence, and the next lookup sends a fresh probe. That
duplicate is acceptable because a probe is a side-effect-free bounded
generation request against a local model, so no durable attempt record or
possibly-sent state is kept.

Transport `healthy` means reachable/catalog-valid. Runtime eligibility is a
separate per-model projection. Direct text and embeddings use their own
capability evidence.

**Acceptance:**

- 4.1.1 - Boundary cases reject 32,768 and missing context, accept 65,536 and
  262,144, and classify every model independently in mixed catalogs. test:
  `tests/ai/local_runtime/test_eligibility.py`.
- 4.1.2 - Tool-probe, context, runtime-missing, and profile-control failures
  retain independent codes/messages in stable order. test:
  `tests/ai/local_runtime/test_eligibility.py`.
- 4.1.3 - Chat, Responses, and Messages probes persist independent evidence and
  never replay an ambiguous sent request through another wire. test:
  `tests/ai/local_runtime/test_probes.py`.
- 4.1.4 - Transport health remains true for a reachable short-context model
  while every coding runtime may be ineligible. test:
  `tests/ai/test_endpoint_activation.py`.
- 4.1.5 - Unchanged keys reuse evidence; family, role-model, artifact,
  parser/profile, endpoint, and runtime-version changes each require a fresh
  probe; success replaces evidence atomically for its key; a failed fresh probe
  never yields stale success. test: `tests/ai/local_runtime/test_probes.py`.
- 4.1.6 - A 65,536 canonical / 32,768 loaded model is ineligible with the
  effective, canonical, and runtime values in its reason; a loaded model with
  unknown runtime context is ineligible with `missing`; `context_window`
  reports the effective value. test:
  `tests/ai/local_runtime/test_eligibility.py`.
- 4.1.7 - Connection-refused retries once and stores one success;
  timeout-after-send records failure without resending; four concurrent
  lookups for one key send one probe and receive the same evidence; cancelling
  one waiter leaves the shared probe running; a shared probe cancelled after
  bytes were written leaves no evidence and the next lookup sends exactly one
  fresh probe. test: `tests/ai/local_runtime/test_probes.py`.
- 4.1.8 - An installed model with no runtime context and no probe evidence is
  `unproven` with `eligible=false` and no failure reason; after activation it
  becomes `eligible` or `ineligible`, and a failed check is never reported as
  `unproven`. test: `tests/ai/local_runtime/test_eligibility.py`.
- 4.1.9 - A model with canonical and runtime context 262,144 is coding-ineligible under a 32,768 launch cap and eligible at a 65,536 launch cap when every other check passes. test: `tests/ai/local_runtime/test_eligibility.py::test_launch_cap_controls_coding_eligibility`.
- 4.1.10 - The daemon-constructed service carries the 4.1 evaluator, and
  acquiring the coding role for an installed, unloaded, unprobed model through
  it sends exactly one required-wire probe, converts to the owner's lease when
  every check passes, and releases the activation with the probe or context
  reasons and no lease when one fails. test:
  `tests/ai/local_runtime/test_service.py::test_coding_activation_uses_probe_evaluator`
  and `tests/test_runner_init.py`.
- 4.1.11 - Fifty probes for one model and wire under fifty distinct
  endpoint/parser/runtime-version keys leave exactly one stored record (the
  newest, whether success or failure) and the superseded keys are absent after
  each write; a concurrent in-flight probe for another key completes and
  stores its own record; two probes for one model and wire started under
  different generations leave the newer record stored no matter which finishes
  last, and the stale completion still returns its result to its own waiters;
  startup reconciliation removes records for an
  uninstalled model before eligibility is projected. test:
  `tests/ai/local_runtime/test_probes.py::test_evidence_supersedes_per_model_and_wire`.

### 4.2 Implement scoped Codex and Claude local coding profiles [category: code] (depends: 4.1)
`kind: deliverable`

Targets:
- `src/gobby/agents/local_profiles/__init__.py`
- `src/gobby/agents/local_profiles/contracts.py`
- `src/gobby/agents/local_profiles/codex.py`
- `src/gobby/agents/local_profiles/claude.py`
- `src/gobby/agents/local_profiles/registry.py`
- `src/gobby/ai/codex_endpoint.py::*` — scope-reason: compose transport overrides with local-only profile overrides and selected context
- `src/gobby/agents/codex_oss.py::*` — scope-reason: apply the same local profile to LM Studio/Ollama app-server launches
- `src/gobby/mcp_proxy/tools/spawn_agent/_generation_endpoint.py::*` — scope-reason: resolve local role model, context, modalities, and invocation profile together
- `src/gobby/agents/spawners/command_builder.py::*` — scope-reason: compose provider command ordering and invocation-scoped local profile controls
- `tests/agents/test_command_builder.py::*` — scope-reason: cover local endpoint resolution and generated profile overrides
- `tests/agents/test_codex_oss.py::*` — scope-reason: cover OSS profile args and hosted/local separation
- `tests/ai/test_codex_endpoint.py::*` — scope-reason: cover custom Responses preservation and local context overrides
- `tests/agents/spawners/test_command_builder.py::*` — scope-reason: cover provider command ordering and profile flags

Create a runtime profile contract that returns transport config, selected model
and context, invocation args/env/settings, enforced-control evidence, and
image-inspection availability.

Selected context in the profile contract is always the 1.2 `effective_context`
that 4.1 judged eligible. For local Codex, pass it as `model_context_window` and apply
documented feature controls for the lean contract. Explicitly keep shell,
unified execution/editing, hooks, and Gobby MCP. Set image inspection from
model modalities. Preserve Codex system/developer layers, AGENTS discovery,
Gobby injected context, persona/task prompts, and explicitly loaded skills.
Apply to vLLM config providers and LM Studio/Ollama OSS launches.

For local Claude, normalize the family origin for Anthropic Messages, pass the
selected model and context limit, generate an invocation-scoped settings layer,
strictly expose Gobby MCP and core edit/shell tools, preserve Gobby hooks, and
disable Chrome/computer use, skills, plugins, subagents/teams, and prompt
suggestions through documented controls. Preserve CLAUDE.md/AGENTS.md loading,
system/developer instructions, Gobby context, persona/task prompts, and
explicitly supplied skill instructions. Do not use `--bare`, `--safe-mode`,
`--system-prompt`, or settings-source restriction as a lean-profile shortcut.

The scope predicate requires a resolved `local:coding` selector. Hosted
OpenAI/ChatGPT Codex, ordinary hosted Claude, base web-chat backends, and
unclassified Responses endpoints receive no local profile overrides. Existing
Codex daemon tool-chat isolation remains separate and unchanged.

**Acceptance:**

- 4.2.1 - Local vLLM, LM Studio, and Ollama Codex commands/app servers receive
  selected context and the complete lean profile. test:
  `tests/agents/test_command_builder.py`.
- 4.2.2 - Local Claude receives normalized Messages routing, context, Gobby-only
  MCP/hooks, core tools, and documented feature suppression. test:
  `tests/agents/spawners/test_command_builder.py`.
- 4.2.3 - Hosted Codex/ChatGPT, hosted Claude, ordinary web chat, and
  unclassified Responses endpoints receive no lean overrides. test:
  `tests/ai/test_codex_endpoint.py`.
- 4.2.4 - Text-only models disable image inspection and image-capable models
  retain it without enabling image generation. test:
  `tests/agents/test_command_builder.py`.
- 4.2.5 - Local Codex and Claude launches retain the same harness instruction
  sources and Gobby/persona/task prompt ordering as hosted launches. test:
  `tests/agents/spawners/test_command_builder.py`.

### 4.3 Implement Qwen, Grok, and Droid local coding profiles [category: code] (depends: 4.2)
`kind: deliverable`

Targets:
- `src/gobby/agents/local_profiles/qwen.py`
- `src/gobby/agents/local_profiles/grok.py`
- `src/gobby/agents/local_profiles/droid.py`
- `src/gobby/agents/local_profiles/run_lease.py`
- `src/gobby/agents/spawn_executor.py::*` — scope-reason: consume runtime profile output for every provider, acquire the coding lease before launch, and remove stale endpoint env shortcuts
- `src/gobby/agents/resume_executor.py::*` — scope-reason: reconstruct and revalidate the original local role/profile and reacquire its lease on native resume
- `src/gobby/agents/agent_cleanup.py::AgentCleanupHandler.post_terminal_cleanup`
- `src/gobby/agents/lifecycle_reconciliation.py::LifecycleReconciliation.reconcile_pending_terminations`
- `src/gobby/agents/lifecycle_monitor.py::*` — scope-reason: accept a `get_run_lease` getter and pass it into the cleanup handler and reconciliation constructors; the file is 893 lines and gains only that pass-through
- `src/gobby/runner_init/orchestration.py::*` — scope-reason: pass the runner's local-runtime run-lease getter to the lifecycle monitor
- `src/gobby/agents/resume_metadata.py::*` — scope-reason: persist only secret-free local profile identity and rebuild secrets on resume
- `src/gobby/agents/spawners/auth_env.py::*` — scope-reason: retire the `QWEN_API_BASE`/`GROK_API_BASE` passthrough entries in favor of profile-generated invocation settings
- `tests/agents/spawners/test_auth_env.py::*` — scope-reason: cover the removed passthrough names and unchanged credential splitting
- `tests/agents/test_spawn_executor.py::*` — scope-reason: cover local runtime profile application across terminal providers
- `tests/agents/test_resume_executor.py::*` — scope-reason: cover original-runtime preservation, current eligibility checks, and lease reacquisition
- `tests/agents/test_spawn_executor_droid.py::*` — scope-reason: cover invocation-scoped custom model settings
- `tests/agents/test_run_lease.py`
- `tests/agents/test_agent_cleanup.py::*` — scope-reason: cover exactly-once lease release on every terminal path and restart reconciliation
- `tests/agents/test_lifecycle_monitor.py::*` — scope-reason: cover the run-lease getter reaching both handlers and the absent-service default
- `src/gobby/agents/agent_cleanup.py::AgentCleanupHandler.__init__`
- `src/gobby/agents/lifecycle_reconciliation.py::LifecycleReconciliation.__init__`
- `tests/agents/cleanup_test_support.py::_handler`
- `src/gobby/mcp_proxy/tools/spawn_agent/_local_runtime_assembly.py`
- `src/gobby/mcp_proxy/tools/spawn_agent/_implementation.py::*` — scope-reason: move the endpoint-resolution and request-assembly block into the new assembly module and keep only the call into it; the file is 953 lines and must shrink
- `src/gobby/agents/spawn_models.py::SpawnRequest`
- `src/gobby/agents/local_profiles/contracts.py`
- `src/gobby/mcp_proxy/tools/spawn_agent/_generation_endpoint.py::*` — scope-reason: return the unresolved `LocalRoleSelection` on `SpawnGenerationEndpointResolution` from `resolve_spawn_generation_endpoint` in place of a pre-activation resolved profile
- `tests/mcp_proxy/tools/spawn_agent/test_local_endpoint.py::*` — scope-reason: cover the unresolved local selection and service reaching `SpawnRequest` and the executor

Qwen uses current OpenAI-compatible auth/base-url/model inputs and an
invocation-scoped provider model with exact context/modalities. Replace stale
`QWEN_API_BASE` behavior. Grok uses a generated custom-model layer or documented
custom-model endpoint controls with context and wire selection; replace stale
`GROK_API_BASE` behavior. Both stale names are set in
`src/gobby/agents/spawn_executor.py`, mapped in
`src/gobby/agents/resume_executor.py`, and allowlisted in
`src/gobby/agents/spawners/auth_env.py`; all three sites change together.
Droid uses `--settings` with one invocation-scoped `customModels` entry and
selects its generated `custom:` identifier. `src/gobby/agents/spawn_executor.py`
is 782 lines: profile composition lives in `src/gobby/agents/local_profiles/`,
and the executor gains only the call that applies a resolved profile.

The local selection reaches the executor through one typed carrier, and the
profile is built only after activation, because 2.3 `acquire_role` is what
discovers a cold model's runtime context and probe evidence.
`resolve_spawn_generation_endpoint` (4.2) returns an unresolved
`local_selection: LocalRoleSelection` (role, explicit model override, configured
or overridden runtime) beside its existing fields; `LocalRoleSelection` is an
alias of the 2.3 `RoleAcquisitionRequest` defined in
`ai/local_runtime/leases.py` and re-exported from `local_profiles/contracts.py`
beside the profile contract, so the executor hands the service the exact type
its `acquire_role` consumes and no dependency inverts; `SpawnRequest` gains
`local_selection: LocalRoleSelection | None = None` and
`local_runtime_service: LocalRuntimeService | None = None`. The executor first
calls `acquire_role(selection, owner=run_id)` through `local_runtime_service`,
then builds the 4.2 runtime profile through the profile registry from the
activation result's exact model identity, 1.2 `effective_context`, modalities,
and transport evidence, validates its enforced controls, and only then
launches; a profile-control failure after activation releases the lease through
the same executor failure path. Resume applies the same order under the same
run id. This leaf orders those steps; 4.4 fences them, entering the per-run
lifecycle mutex after `acquire_role` returns to recheck terminal state before
the lease is kept and the process is created. `spawn_agent_impl` in
`src/gobby/mcp_proxy/tools/spawn_agent/_implementation.py` is the only
`SpawnRequest` constructor and the file is 953 lines, so move its existing
endpoint-resolution, speed-application, and local request-assembly block into
the new `src/gobby/mcp_proxy/tools/spawn_agent/_local_runtime_assembly.py`,
which returns the endpoint-derived `SpawnRequest` keyword arguments
(`model`, `api_base`, `api_token`, `is_local`, `codex_oss_provider`,
`codex_config_overrides`, `local_selection`, `local_runtime_service`, and the
endpoint child env); `_implementation.py` keeps only the call and the
`SpawnRequest(...)` construction and ends below its current size. The HTTP
spawn route keeps calling `spawn_agent_impl` unchanged. Both new
`SpawnRequest` fields default to `None`, so the other `SpawnRequest`
constructors (`tests/agents/test_srt_spawn.py`,
`tests/agents/test_verified_review_regressions.py`,
`tests/mcp_proxy/tools/spawn_agent/test_error_handling.py`,
`tests/mcp_proxy/tools/spawn_agent/test_initial_variables.py`,
`tests/mcp_proxy/tools/test_spawn_agent_speed.py`) are unchanged.

`AgentCleanupHandler.__init__` and `LifecycleReconciliation.__init__` take the
getter as a keyword with default `lambda: None`, and
`tests/agents/cleanup_test_support.py::_handler` forwards an optional
`get_run_lease`; the other constructor callers (`agent_health.py`,
`idle_check_handler.py`, `memory_watchdog.py`, `watchdog/recovery.py`, and the
`_handler` consumers `test_srt_process_cleanup.py`, `test_terminal_cleanup.py`,
`test_terminal_delivery.py`) keep their calls unchanged through that default.

Each adapter implements the shared lean controls through documented settings,
flags, or isolated config homes. It preserves core edit/shell, Gobby MCP, and
hooks, plus each harness's normal project instruction discovery and Gobby
context/persona/task prompt ordering. It may not use a safe mode, replacement
system prompt, or isolated home that drops those sources. When the installed
CLI version cannot prove every required control, the profile reports
`profile_control_missing` and execution stops before launch.

Spawn uses the configured coding runtime; an explicit provider override rebuilds
eligibility for that runtime. Resume preserves the original provider/native
session id, re-resolves current model/context and transport evidence, and refuses
a now-ineligible route with typed reasons. Secrets are refreshed from current
stores and never serialized into resume metadata.

Each terminal agent run owns at most one local coding lease for its whole
lifetime. `src/gobby/agents/local_profiles/run_lease.py` is the one idempotent
seam. The run record already exists before any process launches
(`request.run_id` / `spawn_context.agent_run_id`), so the spawn executor
acquires the 2.3 lease with that run id as owner from the first acquisition;
there is no provisional or transferred ownership, and every lease is classifiable
by run state at any instant. Launch failure before process start releases the
lease through the same executor failure path that marks the run failed.
`post_terminal_cleanup` releases the run's lease exactly once on every terminal
state (completed, failed, cancelled, killed, timed out); repeated release is a
no-op. Resume reacquires and revalidates the lease under the same run id before
relaunch. `reconcile_pending_terminations` reconciles persisted run-owned
leases against durable launch evidence after daemon restart, not against run
status alone: a lease whose run is terminal or absent is released; a lease
held by a nonterminal run with no recorded pid, tmux session, or start
checkpoint belongs to an executor that died before launch, so that run is
terminalized with the typed `orphaned_before_launch` and its lease released;
a lease held by a nonterminal run whose recorded process identity is live is
retained for that run; and a recorded process identity that cannot be
verified fails closed by retaining the lease with a typed diagnostic rather
than releasing a lease a live process may still be using. Idle TTL never evicts a run-owned model. `run_lease.py` defines the
`RunLeaseReleaser` protocol (`release_for_run`, `reconcile_run_leases`)
implemented by the 2.3 service. `AgentCleanupHandler.__init__` and
`LifecycleReconciliation.__init__` gain one
`get_run_lease: Callable[[], RunLeaseReleaser | None]` argument (the same
lazy-getter shape as `get_session_manager`); `AgentLifecycleMonitor.__init__`
accepts it and passes it through, and `runner_init/orchestration.py` supplies
`lambda: runner.local_runtime_service` so construction order does not matter.
A `None` result means no local runtime is configured and both methods skip
lease work. Both methods keep their signatures and their
`lifecycle_monitor.py` call sites. `src/gobby/agents/lifecycle_monitor.py` is
893 lines: every lease-related behavior lives in the new
`src/gobby/agents/local_profiles/run_lease.py` (the protocol, the release and
reconciliation helpers both handlers call), so the monitor gains only the
getter pass-through and no logic is added to it; move any helper that would
otherwise land in the monitor into `run_lease.py`.

**Acceptance:**

- 4.3.1 - Qwen, Grok, and Droid local invocations use their current documented
  endpoint/model configuration and complete lean controls. test:
  `tests/agents/test_spawn_executor.py`.
- 4.3.2 - Missing CLI controls fail before process launch with exact runtime,
  version, and missing control. test:
  `tests/agents/test_spawn_executor.py`.
- 4.3.3 - Explicit runtime override validates against the selected local model;
  hosted launches remain unchanged. test:
  `tests/agents/test_spawn_executor.py`.
- 4.3.4 - Resume preserves original runtime and revalidates live local facts
  without persisting credentials. test: `tests/agents/test_resume_executor.py`.
- 4.3.5 - Qwen, Grok, and Droid local launches retain hosted-launch instruction
  discovery and Gobby/persona/task prompt ordering. test:
  `tests/agents/test_spawn_executor.py`.
- 4.3.6 - Failure injection at acquisition, process start, ordinary launch
  failure, cancellation of a run that never launched, and a daemon crash
  between acquisition and process start shows each run holds at most one
  lease owned by its run id, every terminal path releases exactly once, and
  restart reconciliation classifies from durable launch evidence: a still
  pending run holding a lease with no pid, tmux session, or start checkpoint
  is terminalized `orphaned_before_launch` and released, a nonterminal run
  with a live recorded process keeps its lease, an unverifiable recorded
  process identity retains the lease with a typed diagnostic, and no lease
  survives whose run is terminal or absent;
  races between activation-to-launch and a concurrent terminal CAS are
  proven only by 4.4.11, which owns the fence. test:
  `tests/agents/test_run_lease.py` and `tests/agents/test_agent_cleanup.py`.
- 4.3.7 - Constructing the lifecycle monitor with a run-lease getter reaches
  both handlers; with no local runtime the getter returns `None` and terminal
  cleanup and reconciliation complete without lease calls. test:
  `tests/agents/test_lifecycle_monitor.py` and
  `tests/agents/test_agent_cleanup.py`.
- 4.3.8 - A `local:coding` spawn through the MCP tool carries the unresolved
  local selection and the runtime service on `SpawnRequest`; for an
  installed, unloaded, unprobed model the executor acquires the run-owned
  lease first, builds the profile from the activation result's exact model,
  effective context, modalities, and evidence, and launches with those facts
  (a pre-activation profile is never constructed); resume follows the same
  order; a hosted spawn carries neither field; and
  `_implementation.py` ends below its pre-change line count with the moved
  block living in `_local_runtime_assembly.py`. test:
  `tests/mcp_proxy/tools/spawn_agent/test_local_endpoint.py` and
  `tests/agents/test_spawn_executor.py`; behavior: scoped line-count audit in
  the task transcript.

### 4.4 Harden every daemon-managed Codex TUI launch [category: code] (depends: 4.3)
`kind: deliverable`

Targets:
- `src/gobby/agents/spawn_executor_support.py::*` — scope-reason: replace the complete Codex prompt-delivery scheduler and polling state machine with a per-run delivery registry fenced by the run lifecycle mutex
- `src/gobby/agents/run_completion.py::complete_and_notify_agent_run`
- `src/gobby/agents/run_lifecycle_fence.py`
- `tests/agents/test_run_completion.py::*` — scope-reason: cover terminalization awaiting delivery cancellation under the per-run mutex
- `tests/agents/test_agent_cleanup.py::*` — scope-reason: race every cleanup-handler terminal path against the final paste under the shared fence
- `tests/e2e/test_codex_managed_launch.py`
- `tests/agents/test_spawn_executor.py::*` — scope-reason: cover pane resolution, composer recognition, delivery, typed failure, and terminal races
- `tests/agents/test_resume_executor.py::*` — scope-reason: cover resumed TUI flags and terminal-aware delivery scheduling
- `tests/agents/test_tmux.py::*` — scope-reason: prove pane-id capture and remain-on-exit behavior used by prompt delivery
- `tests/agents/fixtures/codex_composer.txt`
- `tests/agents/fixtures/codex_update_menu.txt`
- `tests/agents/fixtures/codex_login_modal.txt`
- `tests/agents/fixtures/codex_config_error.txt`
- `src/gobby/mcp_proxy/tools/agents.py`
- `src/gobby/mcp_proxy/tools/agents_termination.py::_complete_self_terminated_run`
- `src/gobby/workflows/engine/enforcement.py::*` — scope-reason: preserve the completion facade export consumed by workflow enforcement
- `src/gobby/workflows/engine/enforcement_completion.py::EnforcementCompletionMixin._complete_agent_workflow_run`
- `tests/mcp_proxy/tools/test_agents.py::*` — scope-reason: cover MCP self-termination through the fenced completion path
- `tests/workflows/test_agent_workflow_runtime_cleanup.py::*` — scope-reason: cover enforcement completion through the fenced completion path
- `src/gobby/agents/agent_cleanup.py::AgentCleanupHandler.terminalize_successful_run`
- `src/gobby/agents/agent_cleanup.py::AgentCleanupHandler._terminalize_successful_run_unshielded`
- `src/gobby/agents/agent_cleanup.py::AgentCleanupHandler.terminalize_cancelled_run`
- `src/gobby/agents/agent_cleanup.py::AgentCleanupHandler._terminalize_cancelled_run_unshielded`
- `src/gobby/agents/agent_cleanup.py::AgentCleanupHandler.cleanup_agent`
- `src/gobby/mcp_proxy/tools/agent_cancellation.py::terminalize_cancelled_agent_run`
- `src/gobby/mcp_proxy/tools/agent_cancellation.py::terminalize_killed_agent_run`
- `src/gobby/mcp_proxy/tools/agents_query_tools.py::unregister_agent`
- `tests/mcp_proxy/tools/test_agent_cancellation.py::*` — scope-reason: cover no-monitor cancellation through the shared terminal fence and lease cleanup
- `src/gobby/servers/routes/agents.py::cancel_agent_run`
- `src/gobby/servers/routes/agent_cancel.py`
- `src/gobby/build/control_runtime.py::_cancel_active_agents`
- `src/gobby/dispatch/spawn_actions.py::cleanup_unattached_spawned_run`
- `src/gobby/servers/websocket/handlers/session_observe_continue.py::_release_source_session`
- `tests/servers/routes/test_agents_routes.py::*` — scope-reason: cover HTTP cancellation entering the shared terminal fence
- `tests/build/test_build_stop.py::*` — scope-reason: cover build-stop cancellation entering the shared terminal fence on the no-monitor branch
- `tests/dispatch/test_dispatcher.py::*` — scope-reason: cover unattached-spawn cleanup entering the shared terminal fence
- `tests/servers/websocket/test_resume_blocked.py::*` — scope-reason: cover observe/continue source-session release entering the shared terminal fence
- `src/gobby/agents/spawn_executor.py::*` — scope-reason: enter the per-run fence between activation and process launch and route the guarded block's launch failures through one fenced terminal owner; the file gains only that guarded block and that owner
- `src/gobby/agents/resume_executor.py::*` — scope-reason: enter the per-run fence between lease reacquisition and relaunch and route the guarded block's launch failures through one fenced terminal owner; the file gains only that guarded block and that owner
- `src/gobby/agents/kill.py::_close_tmux_session`
- `src/gobby/hooks/session_coordinator.py::SessionCoordinator._terminate_agent_run_inline`
- `src/gobby/mcp_proxy/tools/spawn_agent/_health.py::_deferred_tmux_health_check`
- `src/gobby/workflows/engine/enforcement_checks.py::EnforcementCheckMixin._flush_pending_terminal_denial`
- `tests/agents/test_kill.py::*` — scope-reason: cover the tmux-close default CAS entering the shared terminal fence
- `tests/hooks/test_session_coordinator.py::*` — scope-reason: cover hook-thread inline terminalization entering the fence through the thread bridge
- `tests/mcp_proxy/tools/spawn_agent/test_health.py::*` — scope-reason: cover the deferred health-check failure entering the fence
- `tests/workflows/test_step_enforcement.py::*` — scope-reason: cover the third-denial terminal failure entering the fence through the thread bridge

`-c check_for_update_on_startup=false` already applies unconditionally to
daemon-managed Codex TUI spawn and resume commands for hosted and local models;
commit 5032d6b3cd landed it in `build_cli_command` with its focused test. This
deliverable keeps that behavior unchanged and validates it as baseline
regression (4.4.1) rather than implementing it.

Replace marker-only readiness with a dedicated Codex composer classifier. It
requires the current bottom prompt region and rejects update menus, login/auth,
approval/trust, config errors, numbered choices, shell output, and other modal
screens. Resolve the authoritative `%pane_id` through existing tmux metadata
before polling and use it for every capture/send so remain-on-exit output
survives session cleanup.

After readiness and settle delay, recapture immediately and rerun the classifier
before pasting. Check run status before every poll, sleep, failure transition,
paste, and retry Enter. Terminal runs cancel delivery silently. Missing composer
fails an active run through the existing terminal CAS with the landed typed
reason `codex_composer_not_ready` plus the last 40 lines, normalized by the
shared session redactor and capped at the landed 1,024 characters; this
deliverable keeps that reason string and both caps and adds only the classifier
gate and the fenced delivery registration around them. Send no keys after
classifier failure.

Pre-send status checks alone cannot close the race between the final paste and
terminalization. Replace the module-global `_CODEX_PROMPT_DELIVERY_TASKS` set
with one delivery task registered per run id in the new
`src/gobby/agents/run_lifecycle_fence.py`, which owns the per-run lifecycle
mutex, the per-run terminal-intent flag, the delivery-task registry, and one
`terminalize(run_id, cas)` entry. The registry records, under the mutex,
whether a delivery task has been admitted to it. The entry publishes intent
before it waits: it sets the run's terminal-intent flag, cancels every
registered delivery task that is not admitted and is not the current task
(pre-admission work is pane capture and sleep only, so cancelling it has no
pane side effect), then acquires the run's mutex, awaits those
cancellations, runs the terminal CAS, and releases. An admitted delivery
task is never cancelled: it holds the mutex across the final
recapture/classify/paste/Enter sequence, every tmux subprocess it dispatches
is awaited to completion under the mutex, and it rechecks the
terminal-intent flag under the mutex immediately before the paste and again
immediately before Enter, aborting silently when it is set. Cancellation
therefore never unwinds a task whose tmux child may still be writing, and no
child kill-and-reap path is needed.
Mutex admission is the linearization boundary, and a dispatched tmux send is
never retracted: intent published before delivery's under-mutex recheck (which
includes every terminalization that starts before delivery is admitted)
yields zero keys; intent published after a recheck lets only the single
bounded `send-keys` already dispatched complete (no Enter follows when intent
landed between the paste and the Enter recheck) while the terminal CAS waits
for the mutex; so no pane input can land after a run is terminal on any path
(including remain-on-exit panes and reused pane ids). A delivery
task that itself initiates terminalization (missing composer) removes its own
registration before calling the fence, so terminalization never cancels or
awaits the current task.

Registry entries have a defined lifetime. Lookup, creation, and removal of an
entry happen under one module-level lock. An entry is created on the first
fence use for a run id (delivery registration, the activation-to-launch entry
below, or `terminalize`) and is removed by the last releaser when the run's
durable status is terminal and the entry has no mutex holder, no mutex
waiter, and no registered delivery task; an entry whose run is not yet
terminal stays until a terminal CAS lands. A late actor therefore either
finds the live entry or creates a fresh one after the old one is gone, never
both, and a fresh entry's `terminalize` runs a compare-and-set against
durable terminal state that performs no side effect. No entry survives a
completed, failed, cancelled, timed-out, or launch-failed run.

The same mutex fences the activation-to-launch boundary that 4.3 orders.
After `acquire_role` returns the run-owned lease, the spawn executor (and the
resume executor after lease reacquisition) enters the run's mutex, re-reads
the terminal-intent flag and the durable run status, and when either is
terminal releases the lease through the 4.3 `run_lease.py` helper and aborts
before profile construction; otherwise it holds the mutex through process
creation and releases it once the pid is checkpointed. A terminalization
that wins the mutex first leaves a run that can neither keep its lease nor
launch; one that loses waits for the launched process to be recorded and
then kills it through the existing kill path, so a terminal run never owns
a lease or a live process after its cleanup has passed.

That guarded interval contains two writers that terminalize storage directly
today, and neither can call the fence from inside it: `spawn_executor.
_prepare_provider_sandbox` calls `run_manager.fail` when sandbox startup fails
closed, and `resume_executor._park_unlaunched_successor` calls
`run_storage.cancel` when the tmux spawn raises or returns unsuccessfully —
both while the mutex is held, so wrapping either in `terminalize` would
deadlock against its own holder. Both helpers stop writing terminal state and
return their typed failure instead (`_prepare_provider_sandbox` already
returns `SandboxLaunch | SpawnResult`, and `_park_unlaunched_successor` keeps
its handoff finalization and runtime cleanup while dropping the CAS). Each
executor's guarded block owns the terminal transition for every failure inside
it — sandbox startup failure, a spawn exception, and an unsuccessful spawn
result alike. Terminal intent becomes visible before admission reopens: because
admission is the under-mutex re-read of that flag and the durable status,
unlocking first would leave a window in which a queued spawn or resume passes
both checks and creates a process while the failing owner releases the lease
underneath it. `run_lifecycle_fence.py` therefore exposes a non-reentrant
`publish_terminal_intent(run_id)` for the current mutex holder, which sets the
entry's terminal-intent flag and claims the cleanup for that caller. The
guarded block calls it while still holding the mutex, then releases the mutex,
releases the run-owned lease through the 4.3 `run_lease.py` helper, and runs
its one claimed `terminalize(run_id, cas)` outside the mutex without
re-entering admission. Every competing launch that wins the mutex afterwards
reads the published intent and aborts before profile construction, and an
external terminalization that published intent first leaves the guarded block
with no claim, so its CAS is the ordinary no-op against durable terminal
state. A cleanup that terminalized the run first therefore finds the launch
already abandoned, and a launch failure that wins the race records exactly one
terminal state; both keep the ordering that no terminal run holds a lease or a
live process.

Every terminal-state writer enters that entry. `complete_and_notify_agent_run`
and the `AgentCleanupHandler` methods `terminalize_successful_run`,
`_terminalize_successful_run_unshielded`, `terminalize_cancelled_run`,
`_terminalize_cancelled_run_unshielded`, and `cleanup_agent` (the success and
cancellation paths used by timeout, kill, cancellation, daemon stop, and
lifecycle cleanup) run their CAS through it. `cleanup_agent` is a terminal
writer in its own right rather than a wrapper over the four methods above: its
`pending`/`running` branch runs the `complete`, `timeout`, or `fail` CAS
itself, so each of those three branches is wrapped in `terminalize(run_id,
cas)`. It is a loop-side coroutine, so it uses `terminalize` directly and
never the thread bridge, and no fenced method calls it, so the entry is never
re-entered. Its signature is unchanged, so its callers in `agent_health.py`,
`lifecycle_monitor.py`, `lifecycle_reconciliation.py`, `memory_watchdog.py`,
and `watchdog/recovery.py` keep their code. The MCP paths that today write cancellation or failure
directly when no lifecycle monitor is wired — the no-monitor branch of
`terminalize_cancelled_agent_run`, `terminalize_killed_agent_run`, and
`unregister_agent` — call `terminalize(run_id, cas)` with their existing CAS
and then the 4.3 `run_lease.py` release helper, so the per-run mutex and
exactly-once lease release hold on those branches too; their signatures are
unchanged, so `spawn_agent/_failure_cleanup.py` and
`agents_lifecycle_tools.py` keep their calls. The remaining direct writers
that run their own CAS inside a `shielded_terminal_delivery` operation — the
HTTP `cancel_agent_run` route, the no-monitor branch of build
`_cancel_active_agents`, dispatch `cleanup_unattached_spawned_run`, and the
WebSocket observe/continue `_release_source_session` — wrap that existing CAS
in `terminalize(run_id, cas)` the same way, so every terminal-state writer in
the daemon enters the fence; their signatures are unchanged, so their callers
(`build/controls.py` for `_cancel_active_agents` and
`handlers/session_observe.py` for `_release_source_session`) keep their
code. `src/gobby/servers/routes/agents.py` is 886
lines, so the route's `cancel_and_deliver` operation (kill, fenced
reconciliation CAS, terminal delivery) moves into the new
`src/gobby/servers/routes/agent_cancel.py` beside the existing
`agent_spawn.py` split, and `cancel_agent_run` keeps only the lookup,
status checks, and the call into it, so the routes file ends below its
current size. `shielded_terminal_delivery` itself is unchanged:
it also shields non-terminal work (capture storage offloads keyed by run id,
terminal re-read delivery, and the multi-run `stale-sweeps` operation), so the
fence is entered by each CAS writer and never by the shield.
`complete_and_notify_agent_run`
keeps its signature; its facade re-exports in `mcp_proxy/tools/agents.py` and
`workflows/engine/enforcement.py` and the direct callers in
`agents_termination.py` and `enforcement_completion.py` keep their code and
are covered by 4.4.6.

Four further writers reach storage outside every facade above and also enter
the fence. `kill._close_tmux_session` passes no callback to
`terminate_managed_tmux_async`, so its CAS is `capture._default_terminalize`;
it now passes a callback that runs that same default CAS through
`terminalize(run_id, cas)` (the callers `cancel_agent_run` and
`terminalize_killed_agent_run` keep their later fenced reconciliation CAS,
which sees the terminal state and is a no-op, and lease release is
idempotent). `_deferred_tmux_health_check` wraps its `run_storage.fail` the
same way. Two writers run off the event loop: `SessionCoordinator.
_terminate_agent_run_inline` runs `capture_then_kill_sync` with a
`complete`/`fail` CAS on the terminal-delivery offload thread — and, when the
run has no `tmux_session_name`, completes or fails directly on that thread
without ever reaching `capture_then_kill_sync`, so that earlier branch is
routed through the same bridge as the callback CAS — and
`EnforcementCheckMixin._flush_pending_terminal_denial` calls `storage.fail`
on the workflow engine's offload thread. For them `run_lifecycle_fence.py`
exposes `terminalize_from_thread(run_id, cas)` as a two-phase bridge in which
neither side ever waits on the other's completion. One loop-side coroutine owns
the entry: it publishes terminal intent, cancels un-admitted delivery, acquires
the mutex, sets a `threading.Event` acquired-signal, awaits the CAS outcome on
a thread-safe result channel, schedules the 4.3 `run_lease.py` release, and
releases the mutex and reclaims the entry in a `finally` that runs on success,
a CAS exception (re-raised to the caller), cancellation, and loop shutdown, so
no mutex or entry can strand. The calling thread submits that coroutine with
`run_coroutine_threadsafe` and then waits on the acquired-signal under
`FENCE_BRIDGE_ACQUIRE_TIMEOUT_SECONDS` instead of blocking on the coroutine
itself; on that signal it runs its synchronous CAS on its own thread, publishes
the returned value or the raised exception through the result channel, and
waits for the coroutine's finalization under
`FENCE_BRIDGE_FINALIZE_TIMEOUT_SECONDS`. Both constants live in
`run_lifecycle_fence.py`, and the calling thread is never the loop thread.

Every phase has one owner and one disposition. When `run_coroutine_threadsafe`
refuses because the loop is gone or shutting down, no CAS runs and the writer
returns the typed `fence_unavailable`. When the acquired-signal wait expires,
the calling thread cancels the submitted future and waits for it to settle
before returning `fence_unavailable`: cancellation is delivered either before
the mutex is acquired or at the result-channel await, so the CAS never runs and
the coroutine's `finally` has completed by the time the call returns. When
finalization expires after a CAS that already landed, the terminal state is
durable and the call returns that outcome rather than `fence_unavailable`. That
return hands nothing back: the loop coroutine stays the sole cleanup owner, and
its `finally` still releases the mutex, drives the 4.3 lease release, and
reclaims the entry exactly once whenever it resumes, while the returning writer
performs no cleanup of its own and never cancels it. A concurrent or later
terminalization for the same run therefore meets the still-live non-reentrant
entry rather than a fresh one, reads the terminal intent that CAS published, and
returns the landed outcome without running a second CAS or a second release.
Restart reconciliation is not the fallback on this branch, because the run is
already terminal and 4.3 classifies only nonterminal runs. `fence_unavailable`
is the branch that means no terminal state was written and no loop-side work
remains live, leaving a nonterminal run to the 4.3 restart reconciliation that
already owns it; a later retry after `fence_unavailable` finds a fresh entry and
terminalizes normally with no overlap against the prior attempt. All four keep their signatures, so `kill_agent`, the
session-coordinator hook path, `spawn_agent` health scheduling, and
`_check_step_tool_enforcement` keep their calls, and the existing scheduling
coverage in `tests/mcp_proxy/tools/spawn_agent/test_failure_cleanup.py` is
unchanged. `capture.py` and the other
`terminate_managed_tmux_async`/`capture_then_kill_async` callers
(`agent_health.py`, `lifecycle_monitor.py`, `lifecycle_reconciliation.py`,
`memory_watchdog.py`, `watchdog/recovery.py`) are unchanged: every terminal
state they record is written by the now-fenced `cleanup_agent` or by the
fenced handler methods, and `_failure_cleanup.py` passes a non-writing
`keep_run` callback.

Preserve the operator config mitigation and backup. Daemon correctness depends
only on invocation flags and delivery state.

#20672's manual criterion (a managed Codex spawn with the user-level
`check_for_update_on_startup` suppression absent and an update-eligible older
build reaches the composer and delivers the prompt) is preserved two ways.
`tests/e2e/test_codex_managed_launch.py` is an `e2e`-marked isolated test that
spawns a real daemon-managed Codex TUI in tmux with an isolated `CODEX_HOME`
lacking the suppression key and asserts composer readiness and prompt delivery;
it skips when `codex` or tmux is unavailable and runs outside the focused unit
gate. The update-eligible condition cannot be forced (the menu appears only
when a newer release exists upstream), so the menu-rejection branch is covered
deterministically by the `codex_update_menu.txt` classifier fixture and the
4.4.1 command-shape assertion. That substitution is recorded here, in the
plan, because #20672 is already closed and takes no handoff disposition.

Create the pane-capture fixtures under `tests/agents/fixtures/` for the real
composer, the update menu, the login modal, and a config error, alongside the
existing `pane_approval.txt`. The classifier tests load them from disk; the
6.1 matrices reuse them without creating new fixture files.

**Acceptance:**

- 4.4.1 - Baseline regression over landed commit 5032d6b3cd, which this
  deliverable changes nothing about: every Codex managed spawn/resume command
  carries the startup update suppression override, and `build_cli_command`
  appends it last so it wins over any caller-supplied
  `check_for_update_on_startup` value. test:
  `tests/agents/spawners/test_command_builder.py`.
- 4.4.2 - Composer fixtures accept the real composer and reject update, login,
  approval, config-error, numbered-menu, and shell/early-exit panes. test:
  `tests/agents/test_spawn_executor.py`.
- 4.4.3 - Delivery resolves and uses a pane id, recaptures before send, emits a
  typed bounded/redacted failure, and never sends keys to rejected panes. test:
  `tests/agents/test_spawn_executor.py` and `tests/agents/test_tmux.py`.
- 4.4.4 - A run becoming terminal during polling/settle/submit cancels delivery
  and produces no delayed duplicate failure; a deterministic race that
  terminalizes the run after the final readiness check and before paste sends
  zero keys because terminal intent is published before the mutex wait and
  delivery rechecks it under the mutex before paste and before Enter; with
  delivery already holding the mutex, intent published before the paste
  recheck sends zero keys, intent published between the paste and the Enter
  recheck completes only the dispatched paste and sends no Enter, intent
  published while an admitted task awaits a dispatched `set-buffer`,
  `paste-buffer`, or `send-keys` subprocess never cancels that task and the
  subprocess completes under the mutex before the task aborts at its next
  recheck, and in every ordering the terminal CAS lands after the mutex is
  released with no input after the terminal state. test:
  `tests/agents/test_spawn_executor.py`.
- 4.4.5 - With an isolated `CODEX_HOME` lacking `check_for_update_on_startup`,
  a real daemon-managed Codex spawn and resume reach the composer and deliver
  the prompt; the test skips when `codex` or tmux is absent. test:
  `tests/e2e/test_codex_managed_launch.py`.
- 4.4.6 - MCP self-termination and workflow enforcement completion both enter the shared prompt-delivery terminal fence while preserving their public facades. test: `tests/mcp_proxy/tools/test_agents.py` and `tests/workflows/test_agent_workflow_runtime_cleanup.py`.
- 4.4.7 - Terminalizing through the `AgentCleanupHandler` success,
  cancellation, timeout, kill, and daemon-stop paths after the final readiness
  check and before paste sends zero keys and records exactly one terminal
  state; each of `cleanup_agent`'s own success, timeout, and failure branches
  does the same when driven from a health, monitor, reconciliation, watchdog,
  or memory-watchdog caller, and each releases any run-owned local-model lease
  exactly once, including against a spawn paused between `acquire_role` and
  process creation. test: `tests/agents/test_agent_cleanup.py` and
  `tests/agents/test_spawn_executor.py`.
- 4.4.8 - A missing-composer failure raised inside the delivery task
  terminalizes through the real completion wrapper with one terminal CAS, both
  tasks settle, and the registry is empty; 500 runs driven through success,
  failure, cancellation, timeout, and launch failure (including terminalization
  while delivery holds the mutex and a terminalization that arrives after the
  entry was removed) leave an empty registry, never hold two live entries for
  one run id, and record exactly one terminal state per run. test:
  `tests/agents/test_run_completion.py` and
  `tests/agents/test_spawn_executor.py`.
- 4.4.9 - No-monitor cancellation, the killed-run failure path, and unregister_agent traverse the shared lifecycle fence, send no post-terminal pane input, and release any run-owned local-model lease exactly once. test: `tests/mcp_proxy/tools/test_agent_cancellation.py` and `tests/mcp_proxy/tools/test_agents.py`.
- 4.4.10 - HTTP cancellation, no-monitor build stop, unattached-spawn
  cleanup, and observe/continue source release each run their terminal CAS
  through the shared fence: raced against a delivery task paused before its
  final paste, each sends zero keys and records exactly one terminal state.
  test: `tests/servers/routes/test_agents_routes.py`,
  `tests/build/test_build_stop.py`, `tests/dispatch/test_dispatcher.py`, and
  `tests/servers/websocket/test_resume_blocked.py`.
- 4.4.11 - A spawn or resume paused after `acquire_role` returns and before
  process creation, then terminalized through the cleanup handler or
  no-monitor cancellation, releases its lease and launches nothing; the same
  pause resumed before terminalization launches, and the later
  terminalization kills the recorded process and releases the lease exactly
  once; a sandbox startup failure inside the guarded spawn block and a raised
  or unsuccessful tmux spawn inside the guarded resume block each terminalize
  once through the fence after the mutex is released, never deadlock, release
  the run-owned lease exactly once, and record exactly one terminal state when
  raced against an external terminalization of the same run; a spawn and a
  resume each paused between publishing terminal intent and running the claimed
  CAS admit no competing launch, so a queued spawn or resume for the same run
  aborts before profile construction and creates no process, and the lease is
  still released exactly once. test: `tests/agents/test_spawn_executor.py`,
  `tests/agents/test_resume_executor.py`, and
  `tests/mcp_proxy/tools/test_agent_cancellation.py`.
- 4.4.12 - The tmux-close default CAS inside `kill_agent`, the deferred
  health-check failure, hook-thread inline terminalization on both its
  `capture_then_kill_sync` branch and its missing-`tmux_session_name` branch,
  and the third-denial enforcement failure each run their CAS through the fence:
  raced against a delivery task paused before its final paste, each sends
  zero keys, records exactly one terminal state, and releases any run-owned
  lease exactly once; the two thread writers wait for the loop-side
  acquired-signal and never run their CAS on the loop thread; a CAS that
  raises propagates to the caller through the result channel with the mutex
  released and the entry reclaimed, an acquired-signal wait that exceeds
  `FENCE_BRIDGE_ACQUIRE_TIMEOUT_SECONDS` returns `fence_unavailable` only after
  the cancelled coroutine has settled with no CAS run, a submission to a
  stopped loop returns `fence_unavailable` without an unfenced CAS, and a retry
  after either of those outcomes terminalizes through a fresh entry exactly
  once with no overlap against the prior attempt; a finalization wait that
  exceeds `FENCE_BRIDGE_FINALIZE_TIMEOUT_SECONDS` after a landed CAS returns
  that terminal outcome instead, and a retry issued while that finalization is
  deliberately stalled joins the still-live entry, reads the published terminal
  intent, and returns the landed outcome without a second CAS, after which the
  stalled coroutine's `finally` releases the mutex, releases the lease, and
  reclaims the entry exactly once. test:
  `tests/agents/test_kill.py`, `tests/mcp_proxy/tools/spawn_agent/test_health.py`,
  `tests/hooks/test_session_coordinator.py`, and
  `tests/workflows/test_step_enforcement.py`.

### 4.5 Route local web chat through model-scoped runtime backends [category: code] (depends: 4.3)
`kind: deliverable`

Targets:
- `src/gobby/servers/websocket/chat/runtime_manager.py::*` — scope-reason: resolve local coding role/runtime and cache model-scoped backends
- `src/gobby/servers/websocket/chat/backends/codex.py::*` — scope-reason: acquire local models and start model-profile-scoped app servers
- `src/gobby/servers/websocket/chat/backends/claude.py::*` — scope-reason: create local Messages sessions from the shared runtime profile
- `src/gobby/servers/websocket/chat/backends/qwen.py::*` — scope-reason: use local OpenAI-compatible session configuration
- `src/gobby/servers/websocket/chat/backends/grok.py::*` — scope-reason: use local custom-model session configuration
- `src/gobby/servers/websocket/chat/backends/droid.py::*` — scope-reason: use local customModels session configuration
- `tests/servers/websocket/chat/test_runtime_manager.py::*` — scope-reason: cover local runtime routing, caching, eligibility, and hosted preservation
- `tests/servers/websocket/chat/test_provider_backends.py::*` — scope-reason: cover model-scoped backend construction, conversation-owned lease lifecycle, and hosted base-backend preservation for every provider
- `tests/servers/websocket/chat/test_droid_backend.py::*` — scope-reason: cover local customModels session configuration beside existing Droid behavior
- `tests/servers/websocket/chat/test_codex_backend_preflight.py::*` — scope-reason: cover local-model preflight and profile-scoped app-server start
- `src/gobby/sessions/acp_lifecycle.py::*` — scope-reason: resolve close, delete, and capability operations through the persisted model/profile-scoped backend identity
- `tests/sessions/test_acp_lifecycle_service.py::*` — scope-reason: cover model-scoped ACP lifecycle routing and conversation lease isolation
- `src/gobby/servers/websocket/chat/_streaming.py::ChatStreamingMixin._maybe_switch_model`
- `src/gobby/servers/websocket/chat/_stream_persistence.py::ChatStreamPersistence.persist_model_switch`

Resolve `local:coding[/model]` to the configured or explicit model and runtime.
Cache backend/client instances by runtime, family, normalized model identity,
context, modalities, and profile hash so process-global context settings never
leak between models. Backend caching is lease-free: a cached backend never
owns a lease, so a shared cache entry can never release a model another
conversation still uses.

Each web-chat conversation owns exactly one local coding lease, keyed by its
conversation id. It is acquired before backend start, and it follows the
persistent chat session rather than any one transport connection:
`WebSocketServer._handle_connection` cleans up client state on disconnect while
deliberately preserving `_chat_sessions`, so a closed socket ends no
conversation and a reconnect resumes the same one, possibly overlapping the old
socket's cleanup or an in-flight stream. Releasing on that event would unload or
evict a backend the live conversation still uses, so raw websocket disconnect is
not a release trigger. Release is idempotent and runs on failed start,
clear/reset, deletion, explicit session stop, the existing
`cleanup_idle_sessions` teardown that tears the chat session down after
`IDLE_TIMEOUT_SECONDS`, and daemon shutdown. Release is owner-wide rather than
identity-scoped: each of
those paths enumerates every lease in the 2.3 inventory whose owner is that
conversation id and releases each one, so it does not depend on the live
registry entry, which records only the currently attached cache identity.
That set is one lease in the ordinary case and two only while a failed
predecessor release is outstanding, and an owner-wide release drives it to
zero either way.

The 2.3 lease inventory cannot decide cache eviction, because a lease is keyed
by normalized model identity and role and carries only owner, times, and
profile hash, while a cache identity is runtime, family, normalized model
identity, context, modalities, and profile hash. Two entries for the same
normalized model that differ by runtime, effective context, or modalities are
indistinguishable to that inventory, so it can say a model is still leased
somewhere but never which backend is attached. The attachment record is instead
the session-id-to-cache-identity registry `runtime_manager.py` already keeps:
it holds exactly the full cache identity a conversation is attached to, so
"attached" is decidable by comparison.

That registry and the removal it authorizes share one cache-global lock in
`runtime_manager`: lookup-or-create, the attach that writes the registry entry,
the detach that clears it, and the compare-and-remove eviction all run under it,
and eviction removes an entry only when no registry value equals its full cache
identity. A conversation takes its per-conversation lock first and the
cache-global lock second, in that fixed order everywhere, so the two never
deadlock. Lease acquisition is ordered before cache attach, and the 2.3 lease
keeps its own role of holding the model loaded for that conversation's
lifetime. Both interleavings of a last detach against a new acquisition are
then safe. An acquirer whose attach wrote its registry entry before the
eviction comparison is seen, so the entry is kept and its backend stays live.
An acquirer whose attach has not yet run blocks on the cache-global lock until
the removal completes, then its lookup-or-create misses and builds a fresh
entry, so it never attaches to a removed or stopped backend. A same-model
switch between two runtimes or two profiles releases and evicts only the
predecessor identity, because only the predecessor's registry value is cleared
and the successor's value keeps its own entry live.

A model switch, clear/reset, and session teardown for one conversation
serialize on one per-conversation lock, and the switch commits in a fixed order with a
specified transition at every step: acquire the successor lease; attach the
live backend to the successor (`ChatStreamingMixin._maybe_switch_model`
drives this order) — on failure release the successor, leave the predecessor
live and persisted, and return the error on the chat error channel; persist
the conversation's selected model and backend cache identity to the session
record through `ChatStreamPersistence.persist_model_switch`, which propagates
write failures instead of logging them, as the durable linearization point —
on failure reattach the predecessor (still leased), release the successor,
and return the error; then release the predecessor lease. Predecessor release
is an in-process idempotent bookkeeping call with no durable state. When it
fails, the switch has already committed: the conversation is on one persisted
model and one live backend, the predecessor lease stays in the 2.3 lease
inventory under the conversation id where status surfaces report it, and the
conversation's next release path (clear, deletion, stop, idle teardown,
shutdown) or restart
reconciliation releases it because each of those releases every lease that
conversation owns rather than only the attached identity; no separate cleanup
state or retry loop exists because the inventory already exposes the retained
lease and release is idempotent. Conversation leases are not
durable: 2.3 restart reconciliation releases every conversation-owned lease
because no conversation is live after a restart, and the next message on a
conversation reacquires from the persisted selected model, so any crash
inside the sequence recovers to the persisted identity. The durable state of
a conversation is its session row's `model`, written by
`persist_model_switch`; backend instances are process-local, so the backend
cache identity (runtime, family, normalized model identity, context,
modalities, profile hash) is not a schema field. `runtime_manager.py` keeps
an in-process registry from session id to the attached cache identity,
written with the live attach and cleared on release under the cache-global
lock described above (taken inside the per-conversation lock), and
`ACPSessionLifecycleService` resolves `close`, `delete`,
reconnect, and capability lookups through that registry instead of the
provider name alone, so every lifecycle operation reaches the backend
instance that owns the session and releases only that conversation's lease.
When no registry entry exists (after a restart, before the next message),
the service re-derives the identity from the persisted `model`, the
session's provider, and the current profile; that identity owns no live
backend and no lease, so close and delete transition the row without a
release and reconnect attaches through the normal acquisition path.
`attach_acp_block` keeps its serializer role and the sessions schema is
unchanged.

Session creation, resume, clear, and switch validate current eligibility. A
model switch that changes cache identity creates/attaches the correct backend.
Hosted sessions continue through existing long-lived base backends. `backends/droid.py` is 791 lines and
`backends/codex.py` is 748: local session configuration comes from the 4.2/4.3
profile contract, and each backend gains only the call that applies it.

Expose typed local eligibility errors through the existing chat error channel.
Tool-probe and context reasons remain visible together. A stale direct request
cannot bypass provider-picker filtering.

**Acceptance:**

- 4.5.1 - Local web chat routes through the configured Codex/Claude/Qwen/Grok/
  Droid runtime with selected model context and profile. test:
  `tests/servers/websocket/chat/test_runtime_manager.py`.
- 4.5.2 - Two models on one family receive distinct backend cache identities
  and exact context windows; hosted base backends remain shared and full-featured.
  test: `tests/servers/websocket/chat/test_runtime_manager.py`.
- 4.5.3 - Two conversations sharing one cache identity survive one of them
  switching, clearing, failing to start, being deleted, or being torn down as
  idle without the other's model being released or evicted; each release path is
  idempotent and shutdown releases every conversation lease; a last detach
  raced against a new acquisition of the same cache identity is safe in both
  completion orders, so an acquirer whose registry attach precedes the eviction
  comparison keeps and attaches the live entry, and one whose attach follows it
  builds a fresh entry after the removal completes and never attaches to a
  removed or stopped backend; a same-model switch between two distinct runtimes
  releases and evicts only the predecessor cache identity while the successor
  entry stays live; and a websocket disconnect — raced against a reconnect that
  overlaps the old socket's cleanup, and again during an active stream —
  releases no lease and evicts no entry, leaving the persistent conversation
  streaming through the same backend. test:
  `tests/servers/websocket/chat/test_provider_backends.py` and
  `tests/servers/websocket/chat/test_runtime_manager.py`.
- 4.5.4 - Direct stale/ineligible selections return all typed reasons before
  thread creation. test:
  `tests/servers/websocket/chat/test_runtime_manager.py`.
- 4.5.5 - ACP close, delete, reconnect, and capability lookup resolve the
  live model/profile-scoped backend through the session-id registry and
  release only that conversation's lease; with no registry entry (simulated
  restart) they re-derive the identity from the persisted model and provider,
  close and delete transition the row without releasing any lease, and
  reconnect attaches through acquisition. test:
  `tests/sessions/test_acp_lifecycle_service.py`.
- 4.5.6 - Failure injected at successor acquisition, live successor attach,
  or durable model write, and a concurrent clear or idle teardown, leave the
  conversation on exactly one persisted model, one live backend, and one
  lease; a failed attach or a failed durable write (which
  `persist_model_switch` now raises) surfaces its error, retains the
  predecessor live and persisted, and releases the successor; a failed
  predecessor release leaves one persisted model and one live backend with
  both the successor and the retained predecessor lease visible in the lease
  inventory under the conversation id, the next clear, deletion, stop, idle
  teardown, shutdown,
  or restart reconciliation releases every lease that conversation owns and
  leaves it holding none, a later message reacquires exactly one, and the
  predecessor cache entry is evicted once its lease is gone; after a simulated
  restart the next message reacquires the persisted model. test:
  `tests/servers/websocket/chat/test_provider_backends.py` and
  `tests/servers/websocket/chat/test_runtime_manager.py`.
- 4.5.7 - An installed, unloaded, unprobed local:coding model selected for web chat acquires the conversation lease, activates and probes on first use, continues when proven eligible, and returns typed ineligibility without thread creation when proof fails. test: `tests/servers/websocket/chat/test_runtime_manager.py::test_unproven_model_activates_on_first_message`.

## P5: Operator API, CLI, and Product UI
`kind: framing`

**Goal:** Present one consistent local-runtime state across status, command-line,
Settings, and model selection.

### 5.1 Project health and model eligibility through shared projections [category: code] (depends: 4.5)
`kind: deliverable`

Targets:
- `src/gobby/servers/routes/providers.py::*` — scope-reason: project active-family groups and per-model runtime eligibility
- `src/gobby/servers/routes/admin/_health.py::*` — scope-reason: include local runtime jobs, role readiness, and separate transport/eligibility health
- `src/gobby/utils/status.py::*` — scope-reason: render the full local-runtime health projection through existing status formatting
- `tests/servers/routes/test_servers_routes_providers.py::*` — scope-reason: cover provider picker payloads and mixed eligible catalogs
- `tests/servers/routes/test_admin.py::*` — scope-reason: cover admin local-runtime health projection
- `tests/utils/test_utils_status.py::*` — scope-reason: cover concise CLI status rendering and diagnostics

Extend provider model payloads with normalized model facts and
`runtime_eligibility`. The payload is an explicit union of producers:
active-local-family entries carry normalized facts plus `runtime_eligibility`
for the configured runtime; hosted-provider and generic-endpoint entries keep
their current shape with no local-runtime fields. Represent the active local
family as one named provider group with its family label and configured coding
runtime. One shared selectable predicate, `state in {eligible, unproven}`,
decides availability here and visibility in the 5.4 picker: mark the family
web-chat available when at least one coding model is selectable, so a cold
all-`unproven` family stays reachable for first-use activation, and reserve
`eligible` for proven health; return every discovered model to
diagnostic/settings consumers.

Admin status reports family, configured roles, loaded/pinned/leased models,
jobs, provider health, restart-required/pending switch, and per-role
eligibility. Existing `generation_endpoints` connectivity remains for generic
remote endpoints. `gobby status` renders the same facts with bounded errors and
distinguishes `healthy`, `coding unavailable`, `loading`, `pending restart`,
and `cloud execution`.

**Acceptance:**

- 5.1.1 - Provider payloads keep transport health separate from per-runtime
  model eligibility and preserve all independent reason entries. test:
  `tests/servers/routes/test_servers_routes_providers.py`.
- 5.1.2 - Mixed catalogs make the family available when any coding model is
  `eligible` or `unproven`, an all-`unproven` catalog is available, an
  all-`ineligible` catalog is unavailable with its reasons, and
  short/missing-context models are retained for diagnostics. test:
  `tests/servers/routes/test_servers_routes_providers.py`.
- 5.1.3 - Admin and CLI status agree on family, roles, jobs, leases, pending
  restart, cloud location, and exact failure reason. test:
  `tests/servers/routes/test_admin.py` and
  `tests/utils/test_utils_status.py`.

### 5.2 Add one scriptable local-runtime CLI [category: code] (depends: 5.1)
`kind: deliverable`

Targets:
- `src/gobby/cli/local_runtime.py`
- `src/gobby/cli/__init__.py::*` — scope-reason: register the local-runtime command group with the root CLI
- `tests/cli/test_local_runtime.py`
- `src/gobby/cli/embeddings.py::switch`
- `tests/cli/test_lifecycle_daemon_commands.py::*` — scope-reason: cover retained embeddings-switch routing and replacement guidance

Add `gobby local-runtime` commands for `status`, `detect`, `models`,
`inspect`, `download`, `load`, `unload`, `jobs`, `cancel`,
`set-role`, `prepare-switch`, `cancel-change`, and `switch-status`. Commands
call the daemon API after startup; detection also has a pre-daemon library
path for #20151.

Require explicit `--yes` for downloads and family switch preparation in
non-interactive mode. `download` previews with `confirm=False`, prints the
normalized identity, resolved digest/revision, and known size, then (after
the prompt or under `--yes`) resubmits that previewed identity with
`confirm=True`. A `confirmation_stale` result invalidates the previewed
authorization: an interactive run re-previews and requires a fresh
confirmation of the refreshed identity, and a `--yes` run prints the refreshed
preview and exits nonzero without resubmitting, so only a new invocation
authorizes the changed artifact. There is no `retry` command: rerunning
`download`, `load`, or `unload` after a failed or cancelled job is the retry,
and for `download` it repeats the preview and confirmation like any other
invocation.
Poll jobs with bounded output and Ctrl-C cancellation of
the wait; cancel provider work only through the advertised cancel endpoint.
`set-role` and `prepare-switch` call the 2.3 `stage_profile(confirm=False)`
preview with the current profile hash, print the normalized diff and
restart/migration requirements, require confirmation (or `--yes`) before any
migration-requiring change, and then call `stage_profile(confirm=True)` with
the same token. A `profile_hash_stale` result invalidates the previewed diff
and its authorization: an interactive run re-previews and requires a fresh
confirmation of the refreshed diff and requirements, and a `--yes` run prints
the refreshed preview and exits nonzero without resubmitting, so a materially
different diff — including an ordinary edit that has become
migration-requiring — is never staged by an automatic retry. `cancel-change` calls `cancel_profile_change` with the current hash and
reports `switch_past_cancel_boundary` when the switch is already irreversible.
`set-role` pre-fills the 1.1 family default reference when `--model` is
omitted and echoes it before submitting. Never print credentials or raw
provider logs.

Keep `gobby embeddings switch` as a compatibility-free structural command for
cloud embedding changes during 0.5 development, implemented through the same
family-aware coordinator. Document its replacement path before removal.

**Acceptance:**

- 5.2.1 - Every command maps to one public API operation and supports JSON output
  with stable job/error fields. test: `tests/cli/test_local_runtime.py`.
- 5.2.2 - Download/switch commands require confirmation, render progress and
  cancellation accurately, and redact secrets; injected digest/revision drift
  returns `confirmation_stale`, and the `--yes` invocation prints the
  refreshed preview and exits nonzero with no job created and no provider
  send; rerunning `download` after a failed job previews and confirms again
  and starts a new attempt. test: `tests/cli/test_local_runtime.py`.
- 5.2.3 - Pre-daemon detection and daemon detection use the same adapter
  contract; missing daemon affects only control operations. test:
  `tests/cli/test_local_runtime.py`.
- 5.2.4 - `set-role` previews then submits through `stage_profile` with the
  current hash, surfaces stale-hash and migration-required results, stages
  nothing when the user declines, offers the family default reference when
  `--model` is omitted, and never writes bootstrap directly; concurrent drift
  that changes the diff — including an ordinary edit that becomes
  migration-requiring — returns `profile_hash_stale`, and the `--yes`
  invocation prints the refreshed preview and exits nonzero with no pending
  profile written. test: `tests/cli/test_local_runtime.py`.
- 5.2.5 - `cancel-change` discards a staged change idempotently and reports
  `switch_past_cancel_boundary` once the flip is irreversible. test:
  `tests/cli/test_local_runtime.py`.
- 5.2.6 - The retained gobby embeddings switch command routes cloud structural changes through the family-aware coordinator and reports the local-runtime replacement path. test: `tests/cli/test_lifecycle_daemon_commands.py`.
- 5.2.7 - The jobs command forwards opaque cursors, honors the default 50 and maximum 200 limits, fetches later pages, and keeps text and JSON output bounded. test: `tests/cli/test_local_runtime.py::test_jobs_pages_with_bounded_output`.
- 5.2.8 - CLI resubmission after failed or cancelled download, load, and unload creates a new attempt, download repeats preview and confirmation, and an active attempt is returned without a duplicate provider send. test: `tests/cli/test_local_runtime.py::test_resubmission_matrix`.

### 5.3 Replace endpoint editing with an Operate-mode local runtime surface [category: code] (depends: 5.1)
`kind: deliverable`

Targets:
- `web/src/components/settings/sections/ProvidersModelsSection.tsx::*` — scope-reason: replace local endpoint editing with the focused active-family control surface
- `web/src/components/settings/sections/MemoryKnowledgeSection.tsx::*` — scope-reason: select cloud versus local embedding source and show switch impact
- `web/src/components/settings/inference/LocalRuntimeSection.tsx`
- `web/src/components/settings/inference/LocalRoleEditor.tsx`
- `web/src/components/settings/inference/LocalModelJobs.tsx`
- `web/src/lib/localRuntime.ts`
- `web/src/components/settings/sections/__tests__/ProvidersModelsSection.test.tsx::*` — scope-reason: cover family, roles, jobs, errors, and responsive states
- `web/src/components/settings/sections/__tests__/MemoryKnowledgeSection.test.tsx::*` — scope-reason: cover cloud/local embedding selection and migration confirmation
- `web/src/components/settings/inference/__tests__/LocalRuntimeSection.test.tsx`

Build one inline, progressively disclosed Settings surface using the existing
Button, Chip, NativeSelect/Select, FormField, SegmentedControl, Switch,
ConfirmDialog, and Tooltip primitives. Show active family first, then four role
rows with model, load policy, coding runtime where applicable, readiness, and
lease/job state. Use exact action labels and recovery text.

Installed model choice is a standard selector that lists the 1.1 family default
reference beside installed models. Exact-reference download is an inline
expansion. Use protected confirmation for downloads and family activation
because they consume disk or trigger re-embedding: the download dialog shows
the `confirm=False` preview (normalized identity, resolved digest/revision,
known size) and its confirm action resubmits that previewed identity with
`confirm=True`; a `confirmation_stale` result invalidates that confirmation,
re-previews the refreshed identity in place, and requires the user to confirm
again before any job is created. The job list offers no bare retry action: a
failed or cancelled download row reopens that same confirmed download dialog
with a fresh preview, and a failed or cancelled load or unload row resubmits
its operation, each starting a new attempt. Keep
ordinary role edits inline. Every role, runtime, and load-policy edit previews through the 2.3
`stage_profile(confirm=False)` operation with the displayed profile hash;
ordinary edits then stage with `confirm=True`, a migration-required preview
opens the shared switch confirmation and stages only on confirm, and a
`profile_hash_stale` result invalidates the previewed authorization, reloads
the surface with the refreshed normalized diff and requirements, and stages
nothing until the user confirms that refreshed diff — a reload that newly
reports `embedding_migration_required` reopens the switch confirmation. A
pending
change or open switch renders a Cancel action bound to
`cancel_profile_change`, disabled with the `switch_past_cancel_boundary`
reason once the flip is irreversible. Missing runtimes show #20151 installation guidance. Empty
states teach the first action; loading uses skeletons; disabled/error states
name the blocking fact and remedy.

Render state through icon/text/position plus the locked deutan-safe palette.
Use existing type and spacing tokens, avoid nested card grids and side stripes,
preserve keyboard/focus parity, and respect reduced motion. Verify dark/light at
440x956, 932x430, and 1440x900; mobile becomes one column and mono diagnostics
wrap.

**Acceptance:**

- 5.3.1 - Users can inspect one family, manually select role models/runtimes and
  load policies, start confirmed downloads, follow jobs, and stage a confirmed
  switch; resubmitting a failed download from the job list reopens the download
  dialog with a fresh preview and creates a new attempt only after the user
  confirms it. test:
  `web/src/components/settings/inference/__tests__/LocalRuntimeSection.test.tsx`.
- 5.3.2 - Cloud/local embedding choice shows collection impact and uses the
  shared switch status. test:
  `web/src/components/settings/sections/__tests__/MemoryKnowledgeSection.test.tsx`.
- 5.3.3 - Empty, loading, loaded, leased, pinned, disabled, error, cloud, and
  pending-restart states are keyboard-operable and identify recovery without
  hue-only meaning. test:
  `web/src/components/settings/sections/__tests__/ProvidersModelsSection.test.tsx`.
- 5.3.4 - Visual QA covers both themes and all reference viewports with no
  clipped overlays, nested horizontal scrolling, side stripes, gradient text,
  or one-off controls. behavior: verified screenshots recorded in the task
  transcript.
- 5.3.5 - Inline role edits preview then submit through `stage_profile`,
  surface stale-hash and migration-required results, cancel a pending change
  through `cancel_profile_change`, and never write bootstrap directly. test:
  `web/src/components/settings/inference/__tests__/LocalRuntimeSection.test.tsx`.
- 5.3.6 - Both themes provide AA-visible focus rings, coarse-pointer controls expose 44×44 hit areas without inflating visual chrome, and every animation over 150ms honors reduced motion. behavior: accessibility and visual checks recorded in the task transcript at the three reference viewports.
- 5.3.7 - LocalModelJobs forwards opaque cursors, renders at most 200 rows per page, and keeps component state bounded while navigating retained history beyond one page. test: `web/src/components/settings/inference/__tests__/LocalRuntimeSection.test.tsx`.
- 5.3.8 - Injected drift returning `confirmation_stale` invalidates the
  download dialog's confirmation, shows the refreshed identity, and creates no
  job until the user confirms again; drift returning `profile_hash_stale`
  reloads the refreshed diff and requirements, reopens the switch
  confirmation when the reloaded preview newly requires migration, and writes
  no pending profile until the user confirms that refreshed diff. test:
  `web/src/components/settings/inference/__tests__/LocalRuntimeSection.test.tsx`.
- 5.3.9 - Settings resubmission from failed or cancelled download, load, and unload rows creates a new attempt, download reopens fresh confirmation, and an active attempt is not duplicated. test: `web/src/components/settings/inference/__tests__/LocalRuntimeSection.test.tsx`.

### 5.4 Filter the provider picker through coding eligibility [category: code] (depends: 5.1)
`kind: deliverable`

Targets:
- `web/src/components/chat/ProviderPicker.tsx::*` — scope-reason: render the active local family and filter its models through coding eligibility
- `web/src/lib/providerModelTypes.ts::*` — scope-reason: type normalized local facts and per-runtime eligibility
- `web/src/lib/providerModelCatalog.ts::*` — scope-reason: validate/map the extended provider payload
- `web/src/components/chat/__tests__/ProviderPicker.test.tsx::*` — scope-reason: cover family labels, filtering, runtime routing, and modalities
- `web/src/lib/__tests__/providerModels.test.ts::*` — scope-reason: cover payload validation and mapping

Show the active family under its provider label with a quiet `via <runtime>`
annotation. Include `eligible` and `unproven` models for the configured
runtime (an `unproven` row is selectable and carries an `activates on first
use` hint, since 2.3 activation proves it on the first message); hide
`ineligible` rows (short or missing runtime context, failed probe,
unenforceable profile) from this picker. The Settings/status surfaces remain
their diagnostic home.

Pass `local:coding/<model>` plus the configured execution runtime to session
creation. Preserve Text/Image chips using existing Chip primitives and existing
per-model modality rules. Family visibility uses the same 5.1 selectable
predicate: when no model is `eligible` or `unproven` (every row `ineligible`
or malformed), hide the local family and surface its reason through the
existing provider availability affordance; an all-`unproven` family remains
visible with its rows selectable.

The catalog mapper validates per producer. A local entry with malformed or
missing `runtime_eligibility` fails closed alone with a typed availability
reason; hosted and generic-endpoint entries map unchanged and are never
evaluated against local-runtime fields.

**Acceptance:**

- 5.4.1 - Eligible local models appear under LM Studio, Ollama, or vLLM with the
  correct execution runtime and modality chips. test:
  `web/src/components/chat/__tests__/ProviderPicker.test.tsx`.
- 5.4.2 - Ineligible rows are absent from the picker, `unproven` rows are
  selectable with the first-use hint, a catalog whose every coding model is
  `unproven` passes route serialization and mapping and renders the family
  visible with selectable rows, an all-`ineligible` family is hidden with its
  reason, and hosted providers and generic remote endpoints remain unchanged.
  test: `web/src/components/chat/__tests__/ProviderPicker.test.tsx` and
  `tests/servers/routes/test_servers_routes_providers.py`.
- 5.4.3 - Payload validation preserves structured eligibility and rejects
  malformed reason/profile records. test:
  `web/src/lib/__tests__/providerModels.test.ts`.
- 5.4.4 - One mixed payload spanning hosted, generic-endpoint, and local
  producers passes route serialization, catalog mapping, picker filtering, and
  availability rendering, with one malformed local entry excluded alone. test:
  `tests/servers/routes/test_servers_routes_providers.py` and
  `web/src/lib/__tests__/providerModels.test.ts`.

## P6: Cross-Provider Verification and Documentation
`kind: framing`

**Goal:** Prove common lifecycle/runtime behavior without mutating live model or
vector state and publish the final operator contract.

### 6.1 Add local-family and coding-runtime contract matrices [category: test] (depends: P5, 4.4)
`kind: deliverable`

Targets:
- `tests/ai/local_runtime/fixtures.py`
- `tests/ai/local_runtime/test_provider_contract_matrix.py`
- `tests/ai/local_runtime/test_runtime_profile_matrix.py`
- `tests/ai/local_runtime/test_switch_recovery_matrix.py`

Create deterministic HTTP, process, filesystem, and CLI fixtures. Provider
matrix runs discovery, metadata, download, load, unload, lease, idle eviction,
auth redaction, cloud label, cancellation, failure, and restart reconciliation
through the public control contract for all three families.

Runtime matrix covers all five coding CLIs across hosted and local selectors,
32,768/65,536/262,144/missing contexts, text/image modalities, transport probe
combinations, missing profile controls, explicit override, spawn, resume, and
web chat. It asserts hosted invocations contain no local lean overrides and
local invocations preserve the hosted instruction-source and prompt-order
contract.

Switch matrix injects failure at every family/embedding coordinator boundary and
asserts one authoritative old/new state. The Codex composer fixtures created in
4.4 are reused by the runtime matrix; this deliverable creates no new pane
fixtures.

These fixtures use temporary files, fake servers, and fake processes. They
contact no installed runtime, daemon, model registry, or vector collection.

**Acceptance:**

- 6.1.1 - One provider matrix proves equivalent public job/lease/status
  semantics and declared provider differences. test:
  `tests/ai/local_runtime/test_provider_contract_matrix.py`.
- 6.1.2 - One runtime matrix proves local profile/context behavior and complete
  hosted-session preservation for all five CLIs. test:
  `tests/ai/local_runtime/test_runtime_profile_matrix.py`.
- 6.1.3 - Crash matrix proves family/embedding recovery at every irreversible
  boundary. test:
  `tests/ai/local_runtime/test_switch_recovery_matrix.py`.

### 6.2 Publish the local runtime, API, and migration contract [category: docs] (depends: P5)
`kind: deliverable`

Targets:
- `docs/guides/local-inference-runtimes.md`
- `docs/guides/README.md`
- `docs/guides/providers-and-models.md`
- `docs/guides/configuration.md`
- `docs/guides/system-requirements.md`
- `docs/guides/llm-features.md`
- `docs/guides/ai-daemon-contract.md`

Register the new guide in `docs/guides/README.md`: add its row to the
`## Product Surfaces` table beside `providers-and-models.md` and add a step to
the `### Building Integrations` learning path after the provider/model step.

Document the one-family/four-role model, manual selection policy, normalized
identity, local selector grammar, cloud/local embedding requirement, load
policies, leases, job API/CLI, vLLM platform supervision, Ollama cloud bridge,
multi-CLI coding matrix, 65,536 policy, lean profile, transport probes, family
switch/recovery (including the 3.2 single-query out-of-process window for
`gcode`/`gwiki` during a switch), installer boundary, and hosted-session
preservation.

Use official provider/runtime installation and custom-model links. Explain
download size and remote-code risks. Mark direct Ollama cloud routing,
llama.cpp/mlx-vlm, remote search, and hardware recommendations as post-0.5 work.
Point UI-TARS local fallback to #20405 under #18498. Remove stale operator-owned
vLLM and mixed-local-endpoint guidance. The migration contract states the 1.1
upgrade behavior for pre-existing local-protocol rows under
`ai.generation.endpoints`: the daemon starts, excludes each legacy row from the
resolved endpoints with a startup warning, leaves the stored row in place, and
rejects any write that reasserts a local protocol there, so the operator's
migration step is to remove the row in Settings and configure the matching
family role instead.

**Acceptance:**

- 6.2.1 - Local runtime guide contains complete Settings and CLI workflows for
  each family and role. file: `docs/guides/local-inference-runtimes.md`.
- 6.2.2 - Configuration and daemon contracts show the exact bootstrap, selector,
  API, and embedding switch shapes. file: `docs/guides/configuration.md` and
  `docs/guides/ai-daemon-contract.md`.
- 6.2.3 - Provider/system/feature guides agree on supported families, coding
  runtimes, context policy, cloud behavior, and follow-up boundaries. file:
  `docs/guides/providers-and-models.md`.
- 6.2.4 - The guide index lists `local-inference-runtimes.md` under Product
  Surfaces and the Building Integrations learning path links it. file:
  `docs/guides/README.md`.
- 6.2.5 - The migration contract documents the 1.1.17 upgrade behavior for
  pre-existing local-protocol generation endpoint rows (daemon starts, row
  excluded with a warning, stored row untouched, writes rejected) and the
  operator's remove-and-configure-role step. file:
  `docs/guides/configuration.md` and `docs/guides/local-inference-runtimes.md`.

## V1 Plan Changelog
`kind: verification`

**Round 1** `kind: enhancement`

- enhancer_run: 251d3127-8c20-4bb1-a977-58e332cb274f
- enhancer_session: 173d650a-4e5a-4fdf-81da-46b3595ab716
- converged: false
- suggestions_presented: 6
- accepted:
  - E1 / better / key transport-probe evidence by model, wire, fingerprint,
    profile hash, and runtime version with stale-evidence rules (4.1, 4.1.5)
  - E2 / better / one `stage_profile` mutation operation consumed by CLI and
    Settings, routing embedding-identity changes to 3.2 (2.3, 2.3.5, 3.2, 5.2,
    5.2.4, 5.3, 5.3.5)
  - E3 / better / agent runs own one coding lease from pre-launch acquisition
    through every terminal state with restart reconciliation (4.3, 4.3.6)
  - E4 / better / explicit provider-payload producer union and fail-closed
    local entries with one mixed-payload test (5.1, 5.4, 5.4.4)
- declined:
  - E5 / better / typed resource-estimate projection — Constraint edited
    instead: 0.5.0 shows observed metadata only; estimates deferred under
    #18498
  - E6 / better / checked-in preset catalog — replaced by a static
    `DEFAULT_ROLE_MODELS` table used as pre-fill values (1.1, 1.1.6, 5.2,
    5.3); preset catalog deferred under #18498
- resolution_notes: Four suggestions folded in as new contract paragraphs and
  acceptance items on existing deliverables; no new deliverable sections. E5
  and E6 resolved by narrowing the Constraints to what 0.5.0 ships and adding
  both to the #18498 post-0.5 list.

**Round 1** `kind: verification`

- reviewer_run: a884c4c4-6b1a-4a8c-a187-c50b9588cf0f
- reviewer_session: e4eafb59-1aa0-46bc-8670-0c17191387d6
- verdict: needs_review
- findings:
- F-R1-001 / blocking / 1.2 dropped #19653's remote-provider OpenRouter coverage-auditing branch
- F-R1-002 / blocking / 4.4 omitted #20672's managed-launch end-to-end case with user suppression absent
- F-R1-003 / nit / 5.1 `depends: P4` gates health projection on 4.4 composer work
- F-R1-004 / blocking / `DEFAULT_ROLE_MODELS` values and provenance unspecified
- F-R1-005 / blocking / `local_runtime` nested leaves not bootstrap-owned in the runtime registry
- F-R1-006 / blocking / `embeddings.source` outside the six-key embedding switch inventory
- F-R1-007 / blocking / `LocalRuntimeService` lacks carrier, readiness, recovery, shutdown, and rollback owners
- F-R1-008 / nit / new guide not registered in `docs/guides/README.md`
- F-R1-009 / blocking / single-flight key `family/model/operation` coarser than process-compatibility identity
- F-R1-010 / blocking / `expected_profile_hash` CAS covers active state only while pending state is in flight
- F-R1-011 / blocking / per-alias Qdrant repoint exposes a mixed old/new alias set
- F-R1-012 / blocking / lease acquired before run ownership transfer is unreconcilable after a crash
- F-R1-013 / blocking / Codex prompt delivery is check-then-act against run terminalization
- F-R1-014 / blocking / web-chat lease ownership mismatches shared backend cache lifetime
- F-R1-015 / blocking / effective context precedence across canonical, runtime, and launch caps undefined
- resolution_notes: All 15 findings accepted by the user after per-finding votes. F-R1-002 accepted as live `e2e` managed-launch case plus fixture coverage for the non-forcible update-menu condition. F-R1-012 accepted as run-id-owned lease from first acquisition (no transfer step). F-R1-004 accepted with the Qwen3/nomic default table proposed by the coordinator. Repairs applied to 1.1, 1.2, 2.3, 3.2, 4.1, 4.3, 4.4, 4.5, 5.1, 6.2, and Constraints after this checkpoint; no declines, no deferrals.

```json plan-review-round
{"evidence_id":"50fd71c9-7952-47fd-96ec-3b29d6321bc8","plan_hash":"aa1717c7b9694b6fa2071dbf0e8fa4aee4b34ad1eacbf06a4c11da4ad0f2d81c","round_number":1,"round_result":{"coverage_attestation":{"adjacent_variant_complete":true,"attestation_digest":"97dd36acc8fb131709bc590c7fbc5eb6fca9608ee98eff368d6ad9688eae2762","cross_lane_interaction_complete":true,"disposition_counts":{"dismissed":2,"emitted_findings":15,"total":17},"evidence_id":"50fd71c9-7952-47fd-96ec-3b29d6321bc8","lanes":[{"candidate_count":4,"lane_id":"requirements_traceability","status":"completed"},{"candidate_count":4,"lane_id":"repository_blast_radius","status":"completed"},{"candidate_count":9,"lane_id":"runtime_invariants","status":"completed"}],"shadow_manifest_status":{"entry_count":18,"manifest_digest":"1387e6ba82feab13e81e1a0e792d209ab9d6a7124b62134e7c8348015e4b05c0","status":"valid"},"source_digest":"09fecbccc39051ea582ec3646b125a8043599c5522ff16080a439c8fa0f92d36","version":1},"findings":[{"category":"missing-requirement","check_key":"preserved-task-acceptance-parity","description":"The Constraints promise to preserve #19653, but no acceptance item proves remote provider models still use OpenRouter coverage auditing while loopback models use authoritative local metadata.","finding_id":"F-R1-001","fix":"Add a 1.2 acceptance item and concrete test Target covering the remote-provider OpenRouter path alongside LM Studio, Ollama, and vLLM local metadata and typed-unknown cases.","location":"§ 1.2","prevention":"Diff every superseded task's validation criteria against plan acceptance items before handoff.","principle":"A replacement plan must preserve every acceptance branch of a task it promises to supersede.","root_cause":"Section 1.2 carried local discovery criteria from #19653 but dropped its remote-provider OpenRouter regression branch.","section_id":"1.2","severity":"blocking"},{"category":"missing-requirement","check_key":"preserved-task-end-to-end-parity","description":"The plan promises to preserve #20672, yet it omits the managed Codex launch case with user-level suppression absent and an update-eligible older build.","finding_id":"F-R1-002","fix":"Add an isolated managed spawn/resume acceptance case proving the invocation override bypasses the update menu, reaches the composer, and delivers the prompt; otherwise revise the preservation commitment and task disposition explicitly.","location":"§ 4.4","prevention":"Map each source task validation criterion to an acceptance item or record an explicit approved replacement.","principle":"Superseding a bug task requires preserving its end-to-end validation, including the environment condition that triggered the bug.","root_cause":"Section 4.4 replaced #20672's managed-launch criterion with command and fixture tests without declaring that substitution.","section_id":"4.4","severity":"blocking"},{"category":"bad-sequencing","check_key":"dependency-minimality","description":"`(depends: P4)` unnecessarily gates health and eligibility projection on § 4.4's composer-delivery work.","finding_id":"F-R1-003","fix":"Narrow 5.1 to the real prerequisite, likely 4.5, then regenerate and inspect manifest dependency closure.","location":"§ 5.1","prevention":"Expand phase dependencies to leaves and justify each direct edge against a consumed output.","principle":"A phase dependency should encode a real prerequisite rather than unrelated phase completion.","root_cause":"The P4 dependency expands to Codex TUI prompt hardening even though shared health and eligibility projection does not consume it.","section_id":"5.1","severity":"nit"},{"category":"missing-requirement","check_key":"exact-default-identity","description":"`DEFAULT_ROLE_MODELS` is required, but the LM Studio, Ollama, and vLLM references for text, coding, vision, and embeddings are unspecified.","finding_id":"F-R1-004","fix":"List every exact default provider reference, the coding-context fact and provenance, and acceptance tests that pin the table values.","location":"§ 1.1","prevention":"Pin every shipped default value and provenance in the plan and acceptance tests.","principle":"A decision-complete plan must name product defaults whose choice materially changes shipped behavior.","root_cause":"The plan requires twelve exact family/role references but leaves every value for the implementation leaf to choose.","section_id":"1.1","severity":"blocking"},{"category":"traceability","check_key":"bootstrap-subtree-ownership","description":"Local role leaves such as `local_runtime.text.model` can be misclassified as mutable runtime keys because registry ownership and its tests are absent from Targets.","finding_id":"F-R1-005","fix":"Target `src/gobby/config/registry.py` and `tests/config/test_config_registry.py`; make the full `local_runtime` subtree schema/prefix-owned by bootstrap and test every nested leaf.","location":"§ 1.1","prevention":"Sweep configuration ownership registries and their tests whenever adding an optional nested bootstrap model.","principle":"Every nested bootstrap-owned key must be excluded from runtime mutation by construction.","root_cause":"The runtime registry discovers bootstrap paths from a default value tree, which cannot enumerate leaves inside optional local-role objects.","section_id":"1.1","severity":"blocking"},{"category":"traceability","check_key":"embedding-source-switch-inventory","description":"The coordinator cannot atomically commit cloud/local source with structural embedding values through the current ConfigStore switch seam.","finding_id":"F-R1-006","fix":"Add `src/gobby/config/embedding_keys.py`, its tests, and affected storage/subscriber tests to Targets; add coordinator-owned source to the structural switch inventory while preserving the six-field resolved projection.","location":"§§ 1.1 / 3.2","prevention":"Sweep structural-key inventories, mutation fences, subscribers, and focused tests when adding a transition-owned field.","principle":"Every field committed by an atomic transition must participate in the transition's canonical key inventory and tests.","root_cause":"`embeddings.source` is introduced outside the existing six-key embedding switch allowlist.","section_id":"3.2","severity":"blocking"},{"category":"traceability","check_key":"daemon-service-lifecycle-ownership","description":"`LocalRuntimeService` has no planned owner for typed access, awaited reconciliation before readiness and pinned startup, idempotent shutdown, or failed-start cleanup.","finding_id":"F-R1-007","fix":"Target `app_context.py`, `runner.py`, lifecycle start/shutdown/rollback modules, and focused lifecycle tests; define the carrier once, await recovery before exposing capabilities/routes, and drain/cancel work on shutdown and rollback.","location":"§§ 2.2–2.3","prevention":"For every new daemon service, inventory construction, carrier, start/readiness, recovery, shutdown, and partial-start rollback paths plus focused tests.","principle":"A daemon-wide background service needs explicit carrier, startup readiness, recovery, shutdown, and rollback owners.","root_cause":"The plan assigns construction to `runner_init/servers.py` but omits the repository's typed service carrier and lifecycle orchestration surfaces.","section_id":"2.3","severity":"blocking"},{"category":"traceability","check_key":"documentation-index-registration","description":"The new local-inference guide can ship without a discoverable entry point from `docs/guides/README.md`.","finding_id":"F-R1-008","fix":"Add `docs/guides/README.md` to Targets and acceptance, link the guide from the appropriate setup/product section, and update the learning path.","location":"§ 6.2","prevention":"Check documentation indexes and learning paths for every new guide file.","principle":"A new operator guide must be registered in the repository's canonical guide inventory.","root_cause":"The deliverable creates `local-inference-runtimes.md` without targeting the categorized guide index.","section_id":"6.2","severity":"nit"},{"category":"unhandled-edge","check_key":"process-compatibility-singleflight","description":"Concurrent loads for one artifact but incompatible launch profiles would be coalesced into one job and process result.","finding_id":"F-R1-009","fix":"Key downloads by exact artifact, loads by artifact plus supervisor compatibility/profile hash, and unloads by owned process instance; add concurrent join/separation tests.","location":"§§ 2.2–2.3","prevention":"Compare every idempotency key with the complete resource-sharing key and test both compatible and incompatible concurrent requests.","principle":"Single-flight identity must be at least as specific as the compatibility identity of the resource it creates.","root_cause":"`family/model/operation` omits launch settings, parser, context, and profile hash even though 2.2 requires incompatible loads to use separate processes.","section_id":"2.3","severity":"blocking"},{"category":"unhandled-edge","check_key":"pending-profile-cas","description":"A second client can submit a different profile with the same active hash and overwrite or promote pending state underneath the first switch.","finding_id":"F-R1-010","fix":"Add a durable pending-generation or switch token; CAS active and pending hashes under one mutation fence, replay identical tokens idempotently, reject distinct work with `profile_change_in_progress`, and add race tests.","location":"§§ 2.3 / 3.2","prevention":"Race two distinct staged mutations before and during every irreversible phase and require a single durable mutation fence.","principle":"A cross-store staged mutation needs one generation token and CAS covering both active and pending state.","root_cause":"`expected_profile_hash` guards only active state, which remains unchanged while a pending profile and switch journal are in flight.","section_id":"2.3","severity":"blocking"},{"category":"unhandled-edge","check_key":"atomic-alias-batch","description":"Readers can observe a mixed old/new alias set during the per-alias loop before embedding config and proof advance.","finding_id":"F-R1-011","fix":"Add and target one batched `update_collection_aliases` operation after durable flipping intent; commit config/proof only after batch success and test concurrent reads plus crashes around that boundary.","location":"§ 3.2","prevention":"Inspect the physical backend transaction boundary and test concurrent readers around every irreversible routing change.","principle":"A claimed old-or-new routing flip must update all externally visible aliases in one atomic backend operation.","root_cause":"The current vector client repoints aliases through separate Qdrant calls, while the plan names only a plural flip and no batch primitive.","section_id":"3.2","severity":"blocking"},{"category":"unhandled-edge","check_key":"prelaunch-lease-recovery","description":"A daemon crash after lease acquisition and before ownership transfer can leave a provisional lease that prevents eviction indefinitely.","finding_id":"F-R1-012","fix":"Use the already-created pending run id as owner from first acquisition, or persist and reconcile a provisional token; add crash tests for each boundary.","location":"§ 4.3","prevention":"Inject daemon crashes at acquisition, process start, ownership handoff, and cleanup, then prove no unowned lease remains.","principle":"Every lease acquired before launch must have a durable owner that restart reconciliation can classify.","root_cause":"Ownership transfers to the run only after process start, leaving the acquisition-to-transfer interval outside run-owned reconciliation.","section_id":"4.3","severity":"blocking"},{"category":"unhandled-edge","check_key":"prompt-delivery-terminal-fence","description":"A run can become terminal after the last status check and before `send_keys`, allowing delayed prompt input into remain-on-exit or a reused pane.","finding_id":"F-R1-013","fix":"Register one delivery task per run and have terminalization cancel and await it under the same per-run lifecycle mutex used for final recapture/paste; add the deterministic race test.","location":"§ 4.4","prevention":"Pause after the final readiness check, terminalize the run, and assert cancellation completes before any pane input.","principle":"An external send and terminal transition require a shared per-run synchronization boundary; pre-send checks alone cannot close the race.","root_cause":"Delivery tasks are globally tracked and status checks occur before tmux actions without serializing the final paste/Enter against terminalization.","section_id":"4.4","severity":"blocking"},{"category":"unhandled-edge","check_key":"session-scoped-webchat-leases","description":"A cache-owned lease can be released while another session still uses the backend; session-owned leases lack complete release semantics.","finding_id":"F-R1-014","fix":"Keep backend caching lease-free and give each conversation one lease: acquire on start, acquire successor before atomic switch, release predecessor afterward, and release idempotently on failed start, reset, deletion, stop, and shutdown.","location":"§ 4.5","prevention":"Test two concurrent sessions sharing one cache identity across switch, clear, failure, deletion, and shutdown paths.","principle":"Lease ownership must match the lifetime and multiplicity of the consumers using a shared cache entry.","root_cause":"The plan mixes model-scoped backend caching with unspecified lease ownership and releases an old lease on one session's switch.","section_id":"4.5","severity":"blocking"},{"category":"unhandled-edge","check_key":"effective-context-precedence","description":"A model advertising 65,536 tokens while its loaded instance is capped at 32,768 can be marked eligible and launched with an overstated context window.","finding_id":"F-R1-015","fix":"Define effective context across canonical, runtime, and launch caps; use the tightest verified hard limit, fail closed when required runtime capacity is unknown, and add conflicting-value tests.","location":"§§ 1.2 / 4.1–4.2","prevention":"Test conflicting context sources and require an explicit precedence/fail-closed rule.","principle":"Eligibility must use the tightest verified runtime capacity, not an ambiguous metadata field.","root_cause":"The plan records canonical and loaded/runtime context separately but never defines which value controls eligibility and profile context.","section_id":"4.1","severity":"blocking"}],"reviewer_session":"e4eafb59-1aa0-46bc-8670-0c17191387d6","round":1,"verdict":"needs_review"},"session_id":"4b85e4ac-ec4c-4fea-b191-8a2d71162afb"}
```

**Round 2** `kind: verification`

- reviewer_run: 392723c8-b275-4a27-a163-c70573ea0af7
- reviewer_session: 85498562-450a-46d6-9ba6-1434fc1962d3
- verdict: needs_review
- findings:
- F-R2-001 / blocking / 1.1 `DEFAULT_ROLE_MODELS` rows lack per-row acceptance items
- F-R2-002 / blocking / 5.3 acceptance omits focus-ring, coarse-pointer, and reduced-motion evidence
- F-R2-003 / blocking / local adapters have no origin or credential authority after leaving `GenerationEndpointConfig`
- F-R2-004 / blocking / installer and ConfigStore still write local embedding derived fields outside the coordinator
- F-R2-005 / blocking / 4.3 lease release/reconciliation has no injection path into the lifecycle handlers
- F-R2-006 / blocking / 4.4 exact Target omits the real `complete_and_notify_agent_run` consumers and their tests
- F-R2-007 / blocking / `AgentCleanupHandler` terminal CAS paths bypass the per-run delivery mutex
- F-R2-008 / blocking / unknown digest/revision collapses distinct artifacts into one identity
- F-R2-009 / blocking / job states have no unload progress or operation-independent success
- F-R2-010 / blocking / `stage_profile` mutates before migration confirmation and has no cancel
- F-R2-011 / blocking / embedding readers are not fenced across alias/config/embedder publication
- F-R2-012 / blocking / transport probes can replay on the same wire or run concurrently for one key
- F-R2-013 / blocking / delivery-originated terminalization awaits its own cancellation
- resolution_notes: Unattended round; the coordinator judged every finding. All 13 accepted. F-R2-001, F-R2-002, and F-R2-006 accepted as their typed repairs (acceptance rows per default-table row; one accessibility acceptance item; the MCP termination and workflow enforcement consumers plus focused tests on 4.4). F-R2-003 accepted as `base_url`/`api_key` on `LocalRuntimeConfig` with loopback-only validation, `$secret:` resolution at service construction, and supervisor-owned vLLM origins. F-R2-004 accepted as ConfigStore/ConfigMutations enforcing `embedding_local_fields_derived` at the transaction boundary (1.1) and the installer's local-family branch staging bootstrap plus `source="local"` and deferring derived fields to the 3.2 coordinator's first-start activation (3.2). F-R2-005 accepted as a lazy `RunLeaseReleaser` getter threaded through `AgentLifecycleMonitor` into both handlers, with `lifecycle_monitor.py` and `orchestration.py` gaining only that pass-through. F-R2-007 accepted as one per-run lifecycle fence module used by `complete_and_notify_agent_run`, `AgentCleanupHandler._terminalize`, and the delivery registry. F-R2-008 accepted with a narrowed fix: display identity keys intra-daemon resources, a verified compatibility identity gates every cross-observation reuse, unverified facts fail closed; the suggested persisted observation-generation counter was declined as unneeded mechanism because a re-pull never retargets a held lease. F-R2-009 accepted as `unloading` plus an operation-independent `succeeded` state with a typed result. F-R2-010 accepted as a non-mutating `confirm=False` preview on `stage_profile` plus idempotent `cancel_profile_change` across service, HTTP, CLI, and Settings. F-R2-011 accepted as one in-process embedding generation gate in `embedding_binding.py` acquired exclusively at the 3.2 irreversible boundary, with the mixed-family window until restart documented; crate readers are unchanged. F-R2-012 accepted as pre-send-only retry classification and a cancellation-safe single-flight registry keyed like evidence. F-R2-013 accepted as delivery-task self-deregistration before delivery-originated terminalization. No new deliverables; repairs land on 1.1, 1.2, 2.1, 2.3, 3.1, 3.2, 4.1, 4.3, 4.4, 5.2, and 5.3 after this checkpoint.

```json plan-review-round
{"evidence_id":"e26ddf18-63c4-468d-88c2-fe74e2c81384","plan_hash":"bb7d963a3afa916c1d04d50eee185a5181a3e29b742a677c5eb6f59801f91cdf","round_number":2,"round_result":{"coverage_attestation":{"adjacent_variant_complete":true,"attestation_digest":"ae1a09f4f480f39a4d5cfc4477506846936be4606da5af3a2f806e2f6f7924a1","cross_lane_interaction_complete":true,"disposition_counts":{"dismissed":2,"emitted_findings":13,"total":15},"evidence_id":"e26ddf18-63c4-468d-88c2-fe74e2c81384","lanes":[{"candidate_count":4,"lane_id":"requirements_traceability","status":"completed"},{"candidate_count":4,"lane_id":"repository_blast_radius","status":"completed"},{"candidate_count":7,"lane_id":"runtime_invariants","status":"completed"}],"shadow_manifest_status":{"entry_count":18,"manifest_digest":"3bca99b3193ee9f985266976d9a2451bce5465622c84b768a4a9a0def574fb6f","status":"valid"},"source_digest":"ca29a4b99bead26f5a2e2884c34cac6c39cc1565f747b841c0861ff5a25f082a","version":1},"findings":[{"category":"gobby-format","check_key":"table-row-decomposition","description":"The text, coding, vision, and embeddings default rows have no row-level acceptance identities, violating the Plan-Coverage table-row decomposition contract.","finding_id":"F-R2-001","fix":"Add one acceptance item per role row that pins all three provider references and provenance; keep the coding-context assertion separate.","location":"§ 1.1 DEFAULT_ROLE_MODELS table and Acceptance","prevention":"For every markdown work table, map each data row to one unique acceptance ID before review.","principle":"Every deliverable table data row requires its own stable acceptance item.","repairs":[{"items":[{"artifact":"test: `tests/config/test_local_runtime.py::test_default_role_models_text`","prose":"The text-role defaults equal the LM Studio, Ollama, and vLLM references in the text row and retain exact provider-catalog provenance."},{"artifact":"test: `tests/config/test_local_runtime.py::test_default_role_models_coding`","prose":"The coding-role defaults equal the three coding references, record context 262,144, and retain exact provider-catalog provenance."},{"artifact":"test: `tests/config/test_local_runtime.py::test_default_role_models_vision`","prose":"The vision-role defaults equal the LM Studio, Ollama, and vLLM references in the vision row and retain exact provider-catalog provenance."},{"artifact":"test: `tests/config/test_local_runtime.py::test_default_role_models_embeddings`","prose":"The embeddings-role defaults equal the LM Studio, Ollama, and vLLM references in the embeddings row and retain exact provider-catalog provenance."}],"kind":"add_acceptance","section_id":"1.1"}],"root_cause":"Acceptance item 1.1.6 aggregates four role rows instead of decomposing them.","section_id":"1.1","severity":"blocking"},{"category":"weak-testability","check_key":"operate-accessibility-acceptance","description":"The new Settings control surface can pass its current acceptance while violating AA focus visibility, the 44×44 coarse-pointer floor, or reduced-motion behavior.","finding_id":"F-R2-002","fix":"Add one acceptance item covering focus-ring contrast in both themes, coarse-pointer hit areas without visual inflation, and reduced-motion behavior.","location":"§ 5.3 Acceptance","prevention":"Map every HARD .impeccable.md accessibility rule to an acceptance item and evidence surface.","principle":"Hard accessibility requirements need executable acceptance evidence.","repairs":[{"items":[{"artifact":"behavior: accessibility and visual checks recorded in the task transcript at the three reference viewports","prose":"Both themes provide AA-visible focus rings, coarse-pointer controls expose 44×44 hit areas without inflating visual chrome, and every animation over 150ms honors reduced motion."}],"kind":"add_acceptance","section_id":"5.3"}],"root_cause":"The body mentions keyboard and reduced-motion behavior, while acceptance omits focus contrast, coarse-pointer target size, and reduced-motion verification.","section_id":"5.3","severity":"blocking"},{"category":"missing-requirement","check_key":"local-provider-connection-authority","description":"LM Studio and Ollama adapters must send configured authentication headers, yet the plan removes their current api_base/api_key carrier without defining a replacement origin or secret authority.","finding_id":"F-R2-003","fix":"Define the machine-local connection contract: fixed or configurable loopback origins, optional secret references, discovery/default rules, join-only validation, service construction, redaction, and focused tests.","location":"§§ 1.1 / 2.1 LocalRuntimeConfig and provider construction","prevention":"Trace every provider constructor input back to bootstrap, ConfigStore, or SecretStore before finalizing the schema.","principle":"Every adapter input needs one authoritative configuration and secret-resolution path.","root_cause":"Local protocols are removed from GenerationEndpointConfig, but the replacement schema carries no native origin or credential reference.","section_id":"1.1","severity":"blocking"},{"category":"traceability","check_key":"legacy-embedding-mutation-closure","description":"The existing installer can still download a local embedding model and write model, api_base, dim, query_prefix, and catalog_key outside stage_profile, bypassing the new source/family-switch fence.","finding_id":"F-R2-004","fix":"Target the installer, ConfigMutations, ConfigStore, and focused tests; route legacy local installation through acquisition/stage_profile and enforce embedding_local_fields_derived at the transaction boundary.","location":"§§ 1.1 / 3.2 / 5.2 mutation ownership","prevention":"Literal-sweep every write of transition-owned keys and target each admission boundary and caller.","principle":"A single-writer transition contract must close every existing mutation seam.","root_cause":"The plan inventories config models and the switch coordinator but omits the installer and transactional ConfigStore writers that currently mutate local embedding fields directly.","section_id":"1.1","severity":"blocking"},{"category":"traceability","causal_finding_id":"F-R1-012","causal_section_ids":["4.3"],"check_key":"run-lease-lifecycle-wiring","description":"AgentCleanupHandler and LifecycleReconciliation cannot access the local-runtime lease service under the declared Targets; adding that wiring to the 893-line lifecycle_monitor.py would also cross the size-growth rule without a split.","finding_id":"F-R2-005","fix":"Define the injection path into both lifecycle owners, target constructor/caller tests, and move the new wiring into a focused module before adding logic to lifecycle_monitor.py.","introduced_in_round":2,"location":"§ 4.3 lifecycle construction and restart reconciliation","prevention":"For every lifecycle-owned service call, trace construction through the monitor and all constructor fakes, then apply the source-size gate.","principle":"A new lifecycle dependency needs an explicit constructor, carrier, caller, and test seam.","root_cause":"The two methods that release/reconcile leases are targeted, while their shared lifecycle monitor constructor has no planned LocalRuntimeService/run_lease injection.","section_id":"4.3","severity":"blocking"},{"category":"traceability","check_key":"exact-target-consumer-closure","description":"The exact complete_and_notify_agent_run Target omits MCP termination and workflow enforcement consumers, so expansion can fail consumer coverage and the new fence can ship without both call paths verified.","finding_id":"F-R2-006","fix":"Add the four production consumer/facade files and focused MCP/workflow tests, with acceptance proving both routes enter the fenced completion path.","location":"§ 4.4 complete_and_notify_agent_run Targets","prevention":"Run gcode usages plus a literal symbol sweep for every exact Target and inventory re-exports, dynamic facades, direct callers, and tests.","principle":"Every owned consumer of an exact symbol Target must appear in the plan inventory and focused acceptance.","repairs":[{"entries":["`src/gobby/mcp_proxy/tools/agents.py`","`src/gobby/mcp_proxy/tools/agents_termination.py::_complete_self_terminated_run`","`src/gobby/workflows/engine/enforcement.py::*` — scope-reason: preserve the completion facade export consumed by workflow enforcement","`src/gobby/workflows/engine/enforcement_completion.py::EnforcementCompletionMixin._complete_agent_workflow_run`","`tests/mcp_proxy/tools/test_agents.py::*` — scope-reason: cover MCP self-termination through the fenced completion path","`tests/workflows/test_agent_workflow_runtime_cleanup.py::*` — scope-reason: cover enforcement completion through the fenced completion path"],"kind":"add_targets","section_id":"4.4"},{"items":[{"artifact":"test: `tests/mcp_proxy/tools/test_agents.py` and `tests/workflows/test_agent_workflow_runtime_cleanup.py`","prose":"MCP self-termination and workflow enforcement completion both enter the shared prompt-delivery terminal fence while preserving their public facades."}],"kind":"add_acceptance","section_id":"4.4"}],"root_cause":"The plan names two facade callers as unchanged and misses their split termination/enforcement implementation modules and tests.","section_id":"4.4","severity":"blocking"},{"category":"unhandled-edge","causal_finding_id":"F-R1-013","causal_section_ids":["4.4"],"check_key":"prompt-delivery-terminal-path-closure","description":"Timeout, kill, cancellation, daemon-stop, and lifecycle cleanup can still terminalize through AgentCleanupHandler between the final classifier pass and send_keys, leaving the Round 1 late-send race open.","finding_id":"F-R2-007","fix":"Centralize the delivery/terminal mutex in a primitive used by run_completion and AgentCleanupHandler, then test every success/cancel/timeout/kill/daemon-stop path against the final paste.","introduced_in_round":2,"location":"§ 4.4 per-run lifecycle mutex","prevention":"Inventory every terminal status writer and race each one after final readiness and before pane send.","principle":"The pane-send fence must serialize against every terminal CAS for the run.","root_cause":"Only complete_and_notify_agent_run is placed under the mutex; AgentCleanupHandler performs independent success and cancellation CAS operations.","section_id":"4.4","severity":"blocking"},{"category":"unhandled-edge","check_key":"unknown-artifact-identity-policy","description":"Different artifacts can collapse to the same jobs, processes, leases, probe evidence, or backend cache when digest/revision are unavailable; treating every unknown record as unequal would instead break stable ownership.","finding_id":"F-R2-008","fix":"Separate display identity from verified compatibility identity; require verified backend plus artifact facts for cross-observation reuse and persist an observation/installed-artifact generation for stable intra-observation ownership.","location":"§§ 1.2 / 2.2–2.3 / 4.1 / 4.5 normalized identity","prevention":"Test two consecutive same-family/same-name observations with absent artifact facts across every identity-keyed cache and resource.","principle":"Unknown compatibility facts must fail closed without making identity unstable inside one observation.","root_cause":"Normalized identity permits digest/revision None but supplies no observation generation or separate compatibility key.","section_id":"1.2","severity":"blocking"},{"category":"unhandled-edge","check_key":"job-state-operation-closure","description":"An unload job must either finish as ready after removing the model or overload ready as generic success, making status rendering and restart reconciliation ambiguous.","finding_id":"F-R2-009","fix":"Use an operation-independent succeeded state plus explicit resource result, or add unloading/unloaded states, and test every legal transition across persistence and public projections.","location":"§ 2.3 job state machine","prevention":"Build a transition table for every operation and verify serialization, recovery, cancellation, API, CLI, and UI projection.","principle":"Every operation needs unambiguous progress and terminal outcomes.","root_cause":"The state list models download/load progress and ready success but omits unload progress and unloaded success.","section_id":"2.3","severity":"blocking"},{"category":"unhandled-edge","causal_finding_id":"F-R1-010","causal_section_ids":["2.3","3.2"],"check_key":"confirm-before-profile-stage","description":"A user who declines migration after the CLI or UI calls stage_profile can leave a pending profile that rejects every different desired profile indefinitely.","finding_id":"F-R2-010","fix":"Add a non-mutating normalize/diff/requirements preview or confirmation capability before staging, then expose idempotent cancel_profile_change through service, HTTP, CLI, and Settings with a precise irreversible boundary.","introduced_in_round":2,"location":"§§ 2.3 / 3.2 / 5.2 / 5.3 staged mutation flow","prevention":"Walk accept, decline, disconnect, retry, and too-late branches around every confirmation-gated mutation.","principle":"User confirmation must precede the durable mutation it authorizes, with an explicit reversible boundary.","root_cause":"stage_profile commits pending state before returning migration_required, while no profile/switch cancellation surface is specified.","section_id":"2.3","severity":"blocking"},{"category":"unhandled-edge","check_key":"embedding-reader-generation-fence","description":"A request can embed with the old family against newly aliased vectors, or require an undocumented mixed-family runtime, because the daemon keeps the old family until restart while aliases and embedding config advance.","finding_id":"F-R2-011","fix":"Define one reader-visible activation boundary: quiesce readers across tuple publication or pin each request to an immutable generation binding embedder identity to physical collection names; retain the target lease and test recovery at every boundary.","location":"§ 3.2 alias/config/bootstrap activation boundary","prevention":"Inject concurrent queries before and after every irreversible write and assert model identity matches physical collections.","principle":"An embedding request must observe one matching embedder and vector-generation tuple from start to finish.","root_cause":"Alias batching makes aliases mutually atomic but does not synchronize readers with later config/proof and bootstrap publication.","section_id":"3.2","severity":"blocking"},{"category":"unhandled-edge","check_key":"transport-probe-at-most-once","description":"Timeout-after-send or concurrent eligibility requests can issue the same tool probe multiple times despite the once-per-wire contract; avoiding only cross-wire replay is insufficient.","finding_id":"F-R2-012","fix":"Retry only definitely pre-send failures unless a transport idempotency key makes replay safe, and join concurrent misses in a cancellation-safe single-flight registry keyed exactly like evidence.","location":"§ 4.1 activation probe execution","prevention":"Classify pre-send versus ambiguous/post-send failures and race concurrent callers for every evidence key.","principle":"A probe promised once per wire needs a single-flight, at-most-once send boundary.","root_cause":"The existing retry wrapper replays transient same-wire failures and the new evidence cache has no concurrent-miss joining policy.","section_id":"4.1","severity":"blocking"},{"category":"unhandled-edge","causal_finding_id":"F-R1-013","causal_section_ids":["4.4"],"check_key":"delivery-originated-terminal-self-await","description":"Missing-composer failure can deadlock: delivery waits for terminalization, terminalization waits for delivery cancellation, and the shielded completion child preserves the cycle.","finding_id":"F-R2-013","fix":"Unregister or explicitly exclude the initiating delivery task before delivery-originated terminalization; add a deterministic real-wrapper test proving one terminal CAS and an empty registry.","introduced_in_round":2,"location":"§ 4.4 missing-composer terminalization","prevention":"Exercise every delivery-originated failure through the real completion wrapper and assert both tasks settle and the registry empties.","principle":"A task must never cancel and await itself through a shielded terminalization cycle.","root_cause":"The delivery coroutine initiates completion, while completion is specified to cancel and await the registered delivery coroutine.","section_id":"4.4","severity":"blocking"}],"reviewer_session":"85498562-450a-46d6-9ba6-1434fc1962d3","round":2,"round_number":2,"verdict":"needs_review"},"session_id":"4b85e4ac-ec4c-4fea-b191-8a2d71162afb"}
```

**Round 3** `kind: verification`

- reviewer_run: c6311d05-3a2b-4af9-a77f-7fa8497101c1
- reviewer_session: d4cc7f1a-b45b-44f9-bce7-e1a04fbfc2e1
- verdict: needs_review
- findings:
- F-R3-001 / blocking / 1.1 join-only whole-block and generic-generation protocol rejections lack acceptance items
- F-R3-002 / blocking / 1.2 preserved `LocalEndpointModelGroup` shape and discovery signature have no acceptance item
- F-R3-003 / blocking / 2.1 "every provider mutation requires confirmation" contradicts lease-driven automatic load/unload/eviction
- F-R3-004 / blocking / 2.2 committed-revision and cache-path installed-state rule has no acceptance item
- F-R3-005 / blocking / 2.3 `GobbyRunner`/`AppContext` service carrier has no acceptance item
- F-R3-006 / blocking / 3.2 pending-profile bootstrap fields have no carrier freshness acceptance in 3.2
- F-R3-007 / blocking / 5.2 retained `gobby embeddings switch` is absent from Targets and acceptance
- F-R3-008 / blocking / 4.3 no typed carrier from `resolve_spawn_generation_endpoint` through `SpawnRequest` to `spawn_executor`; `_implementation.py` (953 lines) untargeted
- F-R3-009 / blocking / 4.3 `AgentCleanupHandler.__init__`, `LifecycleReconciliation.__init__`, and `cleanup_test_support._handler` untargeted (fixer-induced, F-R2-005)
- F-R3-010 / blocking / 4.4 Target names a nested capture callback; no-monitor cancellation and `unregister_agent` bypass the fence (fixer-induced, F-R2-007)
- F-R3-011 / blocking / 4.5 `ACPSessionLifecycleService` resolves backends by provider only once caches are model-scoped
- F-R3-012 / blocking / 3.2 out-of-process `gcode`/`gwiki` embed-then-search can straddle the flip outside the in-process gate
- F-R3-013 / blocking / 2.3 local-runtime stop releases run-owned leases and kills processes that a preserving restart keeps agents on
- F-R3-014 / blocking / 2.3 cold on-demand coding models cannot gather runtime context or probe evidence before admission
- F-R3-015 / blocking / 3.1 text/JSON candidate loops continue past post-send failures; service file untargeted
- F-R3-016 / blocking / 4.5 conversation model switch has no durable ordering between lease, live binding, and persisted model
- F-R3-017 / blocking / 2.2 vLLM launch has no port reservation or pre-spawn ownership intent
- F-R3-018 / blocking / 2.3 `cancel_profile_change` and flipping-intent persistence have no shared linearization point (fixer-induced, F-R2-010)
- F-R3-019 / blocking / 2.3 persisted jobs, per-job events, and list endpoints are unbounded
- F-R3-020 / blocking / 4.4 terminalizer waits on the delivery mutex before publishing intent, so the sender pastes first (fixer-induced, F-R2-007)
- resolution_notes: Unattended round; the coordinator judged every finding. All 20 accepted, five with narrowed repairs. F-R3-001, F-R3-002, F-R3-004, F-R3-005, F-R3-006, F-R3-007, F-R3-009, and F-R3-011 accepted as their typed repairs (F-R3-005 additionally targets `tests/test_runner_init.py` in 2.3 for consumer coverage; F-R3-011 additionally persists the backend cache identity on the session's ACP block so lifecycle operations resolve the exact instance). F-R3-010 accepted as its typed repairs plus removal of the wrong `AgentCleanupHandler._terminalize` Target and routing of `terminalize_killed_agent_run` (same file, same direct-write class) through the fence. F-R3-003 accepted as one authorization policy: downloads/acquisition and migration-requiring profile changes need explicit confirmation; lease-driven load, unload, and idle eviction follow the declared load policy without confirmation. F-R3-008 accepted as a typed `local_profile` carrier on `SpawnRequest` and `LocalRuntimeService` access through the request, with the endpoint-resolution and request-assembly block moved out of the 953-line `_implementation.py` into a new `spawn_agent/_local_runtime_assembly.py`. F-R3-013 accepted as intent-aware `stop_local_runtime`: a preserving restart keeps run-owned leases and their processes persisted for adoption before readiness; a full stop releases everything; old-family processes that still serve preserved runs are retained until those runs terminalize. F-R3-014 accepted narrowed: coding-role acquisition is the single-flight, cancellation-safe activation (load, runtime-context refresh, required-wire probe, eligibility) that converts to the owner's lease on success; eligibility gains an `unproven` state that the picker and direct admission treat as selectable, so no separate activation lease type is introduced. F-R3-015 accepted narrowed to the plan's existing local-candidate rule: the service loops advance past a local candidate only on definite pre-send unavailability, while cloud chain behavior stays unchanged per 3.1.2. F-R3-016 accepted as a fixed switch order (successor lease, durable selected-model write with visible failure, live backend switch, predecessor release) serialized with clear/disconnect on one per-conversation lock; conversation leases are not durable, so restart reconciliation releases them and the next message reacquires from the persisted model. F-R3-017 accepted as a launch admission fence inside the 2.3 mutation fence: port reservation and a launch nonce persisted before spawn, passed to the child environment, pid/start identity checkpointed after bind, and recovery adopting or killing by nonce. F-R3-018 accepted: cancellation and flipping intent are competing CAS transitions of the same pending-profile record under the one mutation fence. F-R3-019 accepted narrowed to fixed bounds (per-job event ring of 256, 200 terminal jobs or 7 days retained, active/idempotency records never pruned, paginated job list with `limit` default 50 max 200); no separate history endpoint. F-R3-020 accepted: terminalization publishes terminal intent and cancels registered delivery tasks before waiting on the mutex, and delivery rechecks that intent under the mutex before paste and before Enter. F-R3-012 accepted as a documented bounded window rather than a crate change: daemon-internal requests keep the full tuple guarantee, while one in-flight out-of-process query may straddle the flip (Qdrant dimension rejection or one prior-generation result) during an operator-initiated switch that already requires restart; crate readers stay out of scope per Constraints. No new deliverables; repairs land on 1.1, 1.2, 2.1, 2.2, 2.3, 3.1, 3.2, 4.1, 4.3, 4.4, 4.5, 5.2, and 5.4 after this checkpoint.

```json plan-review-round
{"evidence_id":"57fab4ea-c1ab-4ca2-970a-89745c554c82","plan_hash":"e69029dfefea9a591198463a809475f090c73fa3d58f99bd7c595aa99700b203","round_number":3,"round_result":{"coverage_attestation":{"adjacent_variant_complete":true,"attestation_digest":"349b6db34cdd465aa8d5924d3bd7002c20f30243684982e647dadcce78ce23e4","cross_lane_interaction_complete":true,"disposition_counts":{"dismissed":1,"emitted_findings":20,"total":21},"evidence_id":"57fab4ea-c1ab-4ca2-970a-89745c554c82","lanes":[{"candidate_count":8,"lane_id":"requirements_traceability","status":"completed"},{"candidate_count":4,"lane_id":"repository_blast_radius","status":"completed"},{"candidate_count":9,"lane_id":"runtime_invariants","status":"completed"}],"shadow_manifest_status":{"entry_count":18,"manifest_digest":"28bca463c0a71f8cb8a9cb3498f521b8504710c6797f595c4d3d170b929c5c39","status":"valid"},"source_digest":"b7ef9a71fa4441d87ab096d4a2a2a6d0704b9cbce4b70b144a4d162532fb759b","version":1},"findings":[{"category":"weak-testability","check_key":"local-config-rejection-acceptance","description":"The plan can pass all 1.1 acceptance while allowing a provider-only local_runtime block on a join-only machine or accepting lmstudio/ollama/vllm in generic generation configuration.","finding_id":"F-R3-001","fix":"Add separate acceptance items for whole-block join-only rejection and for local-family protocol rejection from generic generation configuration with the replacement path.","location":"§ 1.1 configuration validation and Acceptance","prevention":"Map every validation branch in a configuration schema to a named acceptance item before handoff.","principle":"Every explicit configuration rejection branch needs executable closure criteria.","repairs":[{"items":[{"artifact":"test: `tests/config/test_app_config.py`","prose":"Join-only machines reject the entire local-runtime block, including provider-only connection fields and every role configuration."},{"artifact":"test: `tests/config/test_ai.py`","prose":"Generic generation configuration rejects lmstudio, ollama, and vllm protocol values and reports the local-runtime replacement path."}],"kind":"add_acceptance","section_id":"1.1"}],"root_cause":"Join-only and generic-generation namespace rejection rules were added to the body and Targets without acceptance items.","section_id":"1.1","severity":"blocking"},{"category":"weak-testability","check_key":"local-group-shape-acceptance","description":"An implementation can change the existing group signature or entry shape and still satisfy every 1.2 acceptance item, breaking consumers that are deliberately deferred to 3.1 and 5.1.","finding_id":"F-R3-002","fix":"Add acceptance requiring the existing discovery signature and group entry shape to remain stable with only the normalized record key added.","location":"§ 1.2 compatibility contract and Acceptance","prevention":"For every explicitly preserved interface, add one acceptance criterion covering signature, shape, and permitted extension.","principle":"A promised stable interface needs an acceptance item that fails when its shape or signature drifts.","repairs":[{"items":[{"artifact":"test: `tests/servers/test_local_llm.py`","prose":"discover_local_endpoint_model_group keeps its existing signature and LocalEndpointModelGroup entries keep their current shape with only the normalized record key added."}],"kind":"add_acceptance","section_id":"1.2"}],"root_cause":"The body and test Target preserve LocalEndpointModelGroup and discover_local_endpoint_model_group, while 1.2.1-1.2.7 never assert that seam.","section_id":"1.2","severity":"blocking"},{"category":"missing-requirement","check_key":"provider-mutation-confirmation-boundary","description":"The plan does not determine whether on-demand load, idle unload, and eviction require human confirmation. Applying 2.1 literally deadlocks automatic role acquisition; applying 2.3/3.1 silently violates 2.1.","finding_id":"F-R3-003","fix":"Narrow and state one policy: downloads/acquisition and migration-triggering profile changes require explicit confirmation, while lease-driven load, unload, and eviction follow declared automatic policy; align adapter, API, CLI, and UI acceptance with it.","location":"§§ 2.1 / 2.3 / 3.1 / 5.2 confirmation and lease-driven lifecycle","prevention":"Walk every mutating operation from API, CLI, UI, and automatic lifecycle callers and assign one explicit authorization policy.","principle":"Human authorization boundaries must distinguish user-initiated acquisition from automatic runtime lifecycle operations.","root_cause":"Section 2.1 says every provider mutation requires confirmation, while request-time leases automatically load and unload models and the Constraints only require download confirmation.","section_id":"2.1","severity":"blocking"},{"category":"weak-testability","check_key":"vllm-artifact-resolution-acceptance","description":"The supervisor acceptance can pass while a mutable Hugging Face reference or unmatched cache path is treated as installed, undermining adoption and remote-code/parser identity.","finding_id":"F-R3-004","fix":"Add acceptance rejecting mutable or unresolved references and proving that only a committed revision plus matching cache path reaches installed state with its gates attached.","location":"§ 2.2 acquisition identity and Acceptance","prevention":"For every installed-state transition, test mutable, unresolved, mismatched, and fully resolved artifact identities.","principle":"Security-sensitive artifact resolution must be proven at the state transition that marks an artifact installed.","repairs":[{"items":[{"artifact":"test: `tests/ai/local_runtime/test_vllm_provider.py`","prose":"Mutable, unresolved, or cache-mismatched Hugging Face references never become installed; a committed revision and matching cache path retain remote-code and parser gates on the resolved artifact."}],"kind":"add_acceptance","section_id":"2.2"}],"root_cause":"Exact committed revision and matching cache-path requirements have a provider test Target but no acceptance item.","section_id":"2.2","severity":"blocking"},{"category":"weak-testability","check_key":"daemon-service-carrier-acceptance","description":"Section 2.3 can pass readiness/shutdown tests while constructing duplicate services, failing to expose the service through AppContext, or mishandling the unconfigured case.","finding_id":"F-R3-005","fix":"Add acceptance proving one LocalRuntimeService is wired into GobbyRunner, exposed through AppContext to routes/chat/agent consumers, and absent safely when unconfigured.","location":"§ 2.3 service construction, carrier, and Acceptance","prevention":"For every new daemon service, map construction, carrier, readiness, shutdown, and rollback to distinct acceptance evidence.","principle":"A daemon service lifecycle contract needs executable proof of its construction and consumer carrier, not only startup and shutdown behavior.","repairs":[{"items":[{"artifact":"test: `tests/test_app_context.py` and `tests/test_runner_init.py`","prose":"One LocalRuntimeService instance is carried by GobbyRunner, exposed through AppContext to route, chat, and agent consumers, and remains safely absent when local runtime is unconfigured."}],"kind":"add_acceptance","section_id":"2.3"}],"root_cause":"The Round-1 repair added GobbyRunner and AppContext Targets and prose without an acceptance item for the carrier itself.","section_id":"2.3","severity":"blocking"},{"category":"weak-testability","check_key":"pending-profile-carrier-freshness","description":"The pending-profile bootstrap change can leave runtime_config_contract.json stale after 1.1 has already closed, while every 3.2 acceptance item still passes.","finding_id":"F-R3-006","fix":"Add a 3.2 acceptance item requiring the checked-in runtime contract freshness test after pending-profile fields are introduced.","location":"§ 3.2 pending bootstrap carrier and Acceptance","prevention":"Attach the checked-in carrier freshness test to every deliverable that changes its source schema.","principle":"Every deliverable that changes a derived carrier must re-run and own its freshness gate.","repairs":[{"items":[{"artifact":"test: `tests/config/test_runtime_config_contract.py::test_checked_in_contract_matches_registry`","prose":"The checked-in runtime configuration contract remains fresh after active and pending local-profile bootstrap fields are added."}],"kind":"add_acceptance","section_id":"3.2"}],"root_cause":"The runtime contract and freshness test are Targets in 3.2, but the only carrier acceptance is 1.1.4 before pending-profile fields exist.","section_id":"3.2","severity":"blocking"},{"category":"traceability","check_key":"legacy-embedding-switch-consumer","description":"The existing embeddings switch command can remain on its old path or regress without any 5.2 target or validation failure.","finding_id":"F-R3-007","fix":"Target the existing switch function and lifecycle CLI tests, then add acceptance proving family-aware coordinator routing and replacement guidance.","location":"§ 5.2 retained gobby embeddings switch command","prevention":"Literal-sweep every retained CLI command named in a migration plan and inventory its implementation and focused tests.","principle":"A retained public command whose routing changes must appear in Targets and acceptance.","repairs":[{"entries":["`src/gobby/cli/embeddings.py::switch`","`tests/cli/test_lifecycle_daemon_commands.py::*` — scope-reason: cover retained embeddings-switch routing and replacement guidance"],"kind":"add_targets","section_id":"5.2"},{"items":[{"artifact":"test: `tests/cli/test_lifecycle_daemon_commands.py`","prose":"The retained gobby embeddings switch command routes cloud structural changes through the family-aware coordinator and reports the local-runtime replacement path."}],"kind":"add_acceptance","section_id":"5.2"}],"root_cause":"The body keeps gobby embeddings switch through the new coordinator, while Targets and acceptance cover only the new local-runtime group.","section_id":"5.2","severity":"blocking"},{"category":"traceability","check_key":"spawn-profile-carrier-closure","description":"The declared Targets cannot deliver context, modalities, enforced controls, or LocalRuntimeService access from resolve_spawn_generation_endpoint to spawn_executor. Hidden wiring in _implementation.py would also violate the production-size growth rule.","finding_id":"F-R3-008","fix":"Add the spawn assembly consumer, SpawnRequest carrier, and MCP spawn tests; extract local-runtime resolution/request assembly into a new focused module so _implementation.py shrinks below the ceiling, then prove profile fields and the run-owned lease reach spawn_executor.","location":"§§ 4.2–4.3 spawn endpoint resolution to SpawnRequest and spawn_executor","prevention":"Trace each new profile/service field from resolver output through request construction to execution, including all carrier constructors and size gates.","principle":"A resolved runtime profile and service lease must have an explicit typed carrier through every assembly boundary.","root_cause":"The plan targets the profile producer and executor but omits their sole assembly consumer, SpawnRequest, and a service-access seam; the omitted _implementation.py is 953 lines.","section_id":"4.3","severity":"blocking"},{"category":"traceability","causal_finding_id":"F-R2-005","causal_section_ids":["4.3"],"check_key":"run-lease-constructor-closure","description":"AgentCleanupHandler.__init__, LifecycleReconciliation.__init__, and cleanup_test_support._handler must accept or propagate get_run_lease, yet none is targeted.","finding_id":"F-R3-009","fix":"Add exact Targets for both constructors and the shared test helper; existing 4.3.7 then closes their injected/default behavior.","introduced_in_round":3,"location":"§ 4.3 lifecycle getter injection Targets","prevention":"After adding a lifecycle dependency, sweep every constructor and factory/fake before finalizing Targets.","principle":"Every changed constructor and shared fake belongs in the exact Targets inventory.","repairs":[{"entries":["`src/gobby/agents/agent_cleanup.py::AgentCleanupHandler.__init__`","`src/gobby/agents/lifecycle_reconciliation.py::LifecycleReconciliation.__init__`","`tests/agents/cleanup_test_support.py::_handler`"],"kind":"add_targets","section_id":"4.3"}],"root_cause":"The Round-2 getter fix changes two constructors and a test constructor while targeting only their behavior methods.","section_id":"4.3","severity":"blocking"},{"category":"traceability","causal_finding_id":"F-R2-007","causal_section_ids":["4.4"],"check_key":"terminal-fence-writer-closure","description":"AgentCleanupHandler success/cancel paths are not accurately inventoried, while terminalize_cancelled_agent_run without a monitor and unregister_agent bypass both the new mutex and exactly-once lease release.","finding_id":"F-R3-010","fix":"Target the four real AgentCleanupHandler terminal methods plus both direct MCP writers, route them through the common fence/cleanup seam, and add a focused no-monitor/unregister acceptance case.","introduced_in_round":3,"location":"§§ 4.3–4.4 terminal transition inventory","prevention":"Literal-sweep complete/cancel/fail/kill writes and test the no-monitor and unregister fallbacks through the common terminalizer.","principle":"Every terminal state writer must enter the same prompt-delivery fence and run-lease cleanup seam.","repairs":[{"entries":["`src/gobby/agents/agent_cleanup.py::AgentCleanupHandler.terminalize_successful_run`","`src/gobby/agents/agent_cleanup.py::AgentCleanupHandler._terminalize_successful_run_unshielded`","`src/gobby/agents/agent_cleanup.py::AgentCleanupHandler.terminalize_cancelled_run`","`src/gobby/agents/agent_cleanup.py::AgentCleanupHandler._terminalize_cancelled_run_unshielded`","`src/gobby/mcp_proxy/tools/agent_cancellation.py::terminalize_cancelled_agent_run`","`src/gobby/mcp_proxy/tools/agents_query_tools.py::unregister_agent`","`tests/mcp_proxy/tools/test_agent_cancellation.py::*` — scope-reason: cover no-monitor cancellation through the shared terminal fence and lease cleanup"],"kind":"add_targets","section_id":"4.4"},{"items":[{"artifact":"test: `tests/mcp_proxy/tools/test_agent_cancellation.py` and `tests/mcp_proxy/tools/test_agents.py`","prose":"No-monitor cancellation and unregister_agent traverse the shared lifecycle fence, send no post-terminal pane input, and release any run-owned local-model lease exactly once."}],"kind":"add_acceptance","section_id":"4.4"}],"root_cause":"The Round-2 repair targets a nested capture callback rather than the real terminal methods, and two MCP branches still call cancellation storage directly.","section_id":"4.4","severity":"blocking"},{"category":"traceability","check_key":"model-scoped-acp-lifecycle","description":"Close, delete, reconnect, and capability lookup can reach the wrong Qwen/Grok backend and release the wrong conversation lease once backend identity includes model, context, modalities, and profile hash.","finding_id":"F-R3-011","fix":"Target the ACP lifecycle service and tests; persist or resolve the exact backend/cache identity for each session and prove every lifecycle operation reaches that instance only.","location":"§ 4.5 model-scoped backend cache and ACP lifecycle","prevention":"Sweep create, resume, capability, close, delete, clear, and reconnect consumers whenever cache identity gains fields.","principle":"Every lifecycle consumer must resolve the same full identity used to create and cache its resource.","repairs":[{"entries":["`src/gobby/sessions/acp_lifecycle.py::*` — scope-reason: resolve close, delete, and capability operations through the persisted model/profile-scoped backend identity","`tests/sessions/test_acp_lifecycle_service.py::*` — scope-reason: cover model-scoped ACP lifecycle routing and conversation lease isolation"],"kind":"add_targets","section_id":"4.5"},{"items":[{"artifact":"test: `tests/sessions/test_acp_lifecycle_service.py`","prose":"ACP close, delete, reconnect, and capability lookup resolve the persisted model/profile-scoped backend and release only that conversation's lease."}],"kind":"add_acceptance","section_id":"4.5"}],"root_cause":"ACPSessionLifecycleService resolves a single backend by provider and is absent from Targets, while the plan introduces multiple model/profile-scoped backends per provider.","section_id":"4.5","severity":"blocking"},{"category":"unhandled-edge","check_key":"out-of-process-embedding-generation-fence","description":"gcode or gwiki can obtain an old-family query vector, then observe newly repointed aliases after the daemon gate is released, violating the promised complete old-or-new tuple.","finding_id":"F-R3-012","fix":"Return a generation id and exact physical collection binding with each embedding and require Rust clients to search it, or move their complete embed-and-search operation behind the daemon-held gate; add deterministic races for both clients.","location":"§§ 3.1–3.2 embedding publication boundary for gcode and gwiki","prevention":"Pause every in-process and out-of-process consumer after embedding but before vector search, flip generations, and assert identity/collection parity.","principle":"An embed-and-search request must bind its query vector and physical collections to one immutable generation across process boundaries.","root_cause":"The repaired shared gate covers Python in-process requests only; Rust clients leave it after embedding and search Qdrant in a separate operation.","section_id":"3.2","severity":"blocking"},{"category":"unhandled-edge","check_key":"restart-preserves-run-leases","description":"A normal daemon restart can kill the vLLM process serving a still-running local agent, making restart adoption ineffective; a family-switch restart can similarly strand old-family runs.","finding_id":"F-R3-013","fix":"Make local-runtime shutdown intent-aware: preserve active run-owned leases and processes across restart and adopt them before readiness; define full-stop behavior and block promotion/restart or retain old-family processes until active runs terminalize.","location":"§§ 2.2–2.3 shutdown/restart and § 4.3 run-owned leases","prevention":"Test restart and full-stop intents with a live local agent and verify lease/process adoption or an explicit typed termination policy.","principle":"A daemon restart that preserves active agent processes must preserve the model resources their run-owned leases protect.","root_cause":"LocalRuntimeService stop unconditionally releases daemon-owned leases and stops owned processes, while the existing shutdown path deliberately preserves active agent runs.","section_id":"2.3","severity":"blocking"},{"category":"unhandled-edge","check_key":"cold-on-demand-eligibility-bootstrap","description":"Cold vLLM and runtime-context-dependent LM Studio/Ollama coding models can be permanently ineligible or require eager startup, contradicting on_demand policy.","finding_id":"F-R3-014","fix":"Define a cancellation-safe single-flight activation lease/state that starts or loads the artifact, refreshes runtime context, probes required transport, atomically converts to the run/conversation lease on success, and cleans up on failure.","location":"§§ 1.2 / 2.2–2.3 / 4.1 / 4.3 / 4.5 cold coding activation","prevention":"Exercise first spawn and first web-chat selection from a fully installed, unloaded, unprobed state for every family.","principle":"An on-demand resource needs a bootstrap transition that can gather eligibility evidence before final admission.","root_cause":"Runtime context and transport proof require a loaded model, while first lease/load and picker/direct admission require eligibility already proven.","section_id":"2.3","severity":"blocking"},{"category":"traceability","check_key":"pre-send-only-candidate-fallback","description":"One logical request can still be sent to a second model after the first inference began, despite §3.1 allowing candidate advancement only before send.","finding_id":"F-R3-015","fix":"Target the text-generation service, introduce a typed attempt outcome, continue only for definite pre-send unavailability, and add result/JSON tests proving no second invocation after send.","location":"§ 3.1 text generation candidate boundary","prevention":"Trace retry/fallback decisions to their owning loop and test definite pre-send, timeout-after-send, invalid response, parse failure, and unknown send state.","principle":"A pre-send-only fallback promise must be enforced at the loop that decides whether another candidate runs.","repairs":[{"entries":["`src/gobby/ai/_text_generation_service.py::*` — scope-reason: enforce typed pre-send-only candidate advancement in result and JSON generation loops"],"kind":"add_targets","section_id":"3.1"},{"items":[{"artifact":"test: `tests/ai/test_text_generation.py`","prose":"Text and JSON candidate chains advance only after definite pre-send unavailability; timeout-after-send, response validation, parse failure, and unknown send state never invoke another candidate."}],"kind":"add_acceptance","section_id":"3.1"}],"root_cause":"The current result and JSON loops catch generic post-send/ambiguous failures and continue, but the service file is absent from Targets.","section_id":"3.1","severity":"blocking"},{"category":"unhandled-edge","check_key":"durable-webchat-model-switch","description":"A crash or persistence failure can leave the live/new lease on model B while durable session state still names model A after model A's lease is released, so restart cannot reconstruct ownership.","finding_id":"F-R3-016","fix":"Add a serialized durable switch record or generation CAS covering old/new identities; define commit, rollback, and recovery ordering, make persistence failure visible, and race acquire/switch/release against clear and disconnect.","location":"§ 4.5 conversation model switch","prevention":"Inject failures and concurrent clear/disconnect at every switch boundary and recover to one authoritative old or new binding.","principle":"A resource switch spanning durable identity, live binding, and leases needs one recoverable generation transition.","root_cause":"The plan orders successor lease, live backend switch, and predecessor release without durable CAS/recovery; current model persistence happens afterward and swallows failures.","section_id":"4.5","severity":"blocking"},{"category":"unhandled-edge","check_key":"vllm-launch-ownership-fence","description":"Two incompatible loads can select the same port, and a daemon crash after child creation but before ownership metadata can leave a live listener that recovery classifies as foreign process_conflict and cannot safely adopt or clean.","finding_id":"F-R3-017","fix":"Specify one supervisor launch admission fence: atomically reserve the port, persist a unique launching nonce before spawn, pass it to the child, checkpoint pid/start identity after bind, and recover or kill by nonce at every boundary.","location":"§§ 2.2–2.3 managed vLLM process launch","prevention":"Race incompatible launches and crash before spawn, after spawn, after bind, after ownership checkpoint, and before readiness.","principle":"Process launch needs atomic resource reservation and durable ownership intent before the child can outlive its parent.","root_cause":"Stable port allocation and post-launch ownership metadata leave concurrent allocation and crash-before-checkpoint gaps.","section_id":"2.2","severity":"blocking"},{"category":"unhandled-edge","causal_finding_id":"F-R2-010","causal_section_ids":["2.3","3.2","5.2","5.3"],"check_key":"cancel-vs-flip-cas","description":"Cancel can observe no flipping intent and remove pending/journal state while the coordinator concurrently persists intent or proceeds from stale memory, corrupting the only recovery record.","finding_id":"F-R3-018","fix":"Model prepared-to-cancelling and prepared-to-flipping as competing durable CAS transitions under the same mutation fence; define winner-specific cleanup/forward recovery and add simultaneous race tests.","introduced_in_round":3,"location":"§§ 2.3 / 3.2 cancel_profile_change irreversible boundary","prevention":"Barrier-race cancellation against every transition into the irreversible state and require exactly one durable winner.","principle":"Cancellation and irreversible commit must compete through one durable linearization point.","root_cause":"The Round-2 cancellation surface checks whether flipping intent exists without specifying a shared CAS/fence with the coordinator that creates that intent.","section_id":"2.3","severity":"blocking"},{"category":"unhandled-edge","check_key":"job-history-retention-bound","description":"A long-running daemon can accumulate unbounded runtime-state data, startup work, admin payloads, CLI JSON, and Settings rows.","finding_id":"F-R3-019","fix":"Define bounded per-job event rings, terminal-job retention by age and count, protected active/idempotency records, paginated list/history endpoints, capped status projections, and recovery/UI/CLI tests beyond the limits.","location":"§§ 2.3 / 5.1–5.3 persisted jobs and projections","prevention":"Run retention and projection tests above every configured count/event/page limit while preserving nonterminal and idempotency records.","principle":"Persistent operational histories and list endpoints need explicit count, age, event, and page bounds.","root_cause":"Atomic checkpoints and bounded rendering are specified, while stored terminal jobs, per-job events, startup reconciliation, and API lists remain unbounded.","section_id":"2.3","severity":"blocking"},{"category":"unhandled-edge","causal_finding_id":"F-R2-007","causal_section_ids":["4.4"],"check_key":"terminal-intent-before-delivery-lock","description":"When terminalization starts after the last delivery status check, it waits for the sender to release the mutex; the sender sees the run still active and pastes first, contradicting 4.4.4 and 4.4.7.","finding_id":"F-R3-020","fix":"Publish terminal intent or cancel registered delivery tasks before waiting on the send mutex, then recheck that intent under the mutex immediately before paste/Enter; retain self-exclusion for delivery-originated failure.","introduced_in_round":3,"location":"§ 4.4 final prompt paste versus terminalization","prevention":"Pause immediately after the last status check, schedule terminalization, and assert terminal intent is published before any pane input.","principle":"A cancellation request must become visible before it waits on a mutex held by the operation it must stop.","root_cause":"The repaired terminalizer acquires the delivery mutex before cancelling registered delivery tasks, while delivery holds that mutex across the final status check and paste.","section_id":"4.4","severity":"blocking"}],"reviewer_session":"d4cc7f1a-b45b-44f9-bce7-e1a04fbfc2e1","round":3,"verdict":"needs_review"},"session_id":"4b85e4ac-ec4c-4fea-b191-8a2d71162afb"}
```

**Round 4** `kind: verification`

- reviewer_run: 2681b345-cf00-4099-b4c1-43f0e312a048
- reviewer_session: 0ac873ce-d006-4a53-853d-5e592ee444dd
- verdict: needs_review
- findings:
- F-R4-001 / blocking / 5.1 availability and 5.4 family visibility keyed to `eligible` only, hiding an all-`unproven` family before first-use activation (fixer-induced, F-R3-014)
- F-R4-002 / blocking / 1.2 and 4.1 acceptance omit the supervisor launch-cap branch of `effective_context`
- F-R4-003 / blocking / 5.2 CLI `jobs` and 5.3 Settings job table lack cursor/limit/bounded-state acceptance
- F-R4-004 / blocking / 2.1 provider and 2.3 HTTP acquisition lack server-side confirmation-enforcement acceptance
- F-R4-005 / blocking / `.coverage-ledger.yaml` companion absent
- F-R4-006 / blocking / 3.1 generation gate does not target `SemanticToolSearch.search_tools` and `EmbeddingBackend.search_async`, which split embed from collection/ranking access
- F-R4-007 / blocking / 4.3 resolves `local_profile` before `acquire_role`, so a cold model's effective context and probe evidence are unavailable when the profile is built (fixer-induced, F-R3-014)
- F-R4-008 / blocking / 4.1 cancellation or crash after probe bytes are written leaves no evidence and the next lookup resends despite the at-most-once promise
- F-R4-009 / blocking / 4.4 zero-key claim for a terminalization that starts while delivery holds the mutex is unenforceable once the tmux send is dispatched (fixer-induced, F-R3-020)
- F-R4-010 / blocking / 4.5 live-switch and predecessor-release failures after the durable write have no specified transition; `persist_model_switch` swallows write failures (fixer-induced, F-R3-016)
- F-R4-011 / blocking / 2.2 crash after the `launching` record and before spawn matches neither adoption nor killed-by-nonce recovery (fixer-induced, F-R3-017)
- F-R4-012 / blocking / 2.3 Targets omit `tests/test_runner_init.py` required by acceptance 2.3.9 (fixer-induced, F-R3-005)
- resolution_notes: Unattended round; the coordinator judged every finding. Eleven accepted, three of them narrowed; F-R4-005 accepted as a handoff obligation rather than a review-time artifact. F-R4-002, F-R4-003, F-R4-004, F-R4-006, and F-R4-012 accepted as their typed repairs (every named test file is already a Target of its deliverable; both 3.1 consumer symbols resolve and their files are 626 and 280 lines). F-R4-001 accepted as one shared selectable predicate, `state in {eligible, unproven}`, for 5.1 family availability and 5.4 family visibility, with an all-unproven acceptance case; `eligible` stays the proven-health state. F-R4-007 accepted as a sequencing fix: `resolve_spawn_generation_endpoint` returns an unresolved `local_selection` (role, explicit model and runtime overrides) instead of a resolved profile, `SpawnRequest` carries `local_selection` and `local_runtime_service`, the executor first calls 2.3 `acquire_role` under the run id and only then builds and validates the 4.2 runtime profile from the activation result's exact model, effective context, modalities, and evidence; resume follows the same order. F-R4-008 accepted narrowed: the at-most-once promise is scoped to one in-process attempt (no resend after bytes are written, one shared probe per key); a cancellation or daemon crash after send leaves no evidence and the next lookup sends fresh, which is acceptable because probes are side-effect-free local requests, so the suggested durable per-key attempt record and `possibly_sent` state are declined as unneeded mechanism. F-R4-009 accepted: mutex admission is the linearization boundary, intent published before admission yields zero keys, delivery admitted first completes only the bounded send it already dispatched while the terminal CAS waits, the under-mutex rechecks before paste and before Enter remain, and no input lands after the terminal state; 4.4.4 states both ordered outcomes. F-R4-010 accepted narrowed: the switch order becomes successor lease, live successor attach (failure releases the successor and keeps the predecessor live and persisted), durable selected-model write through `ChatStreamPersistence.persist_model_switch` which now propagates failure (failure reattaches the predecessor and releases the successor), then predecessor release; `ChatStreamingMixin._maybe_switch_model` and `persist_model_switch` join the 4.5 Targets. Predecessor release is an in-process idempotent bookkeeping call retried by the conversation's next release path and by restart reconciliation, so the suggested `cleanup_pending` record is declined. F-R4-011 accepted: a `launching` record with no checkpointed pid and no listener on its reserved port is marked aborted, its ownership record cleared, and its port released idempotently; 2.2.6 proves the port is reusable. F-R4-005 narrowed: the ledger binds `root_task_ref` and the final `plan_hash`, neither of which exists during adversarial review, so Constraints records that the companion is authored at build handoff from the approved hash before expansion; no file is created in this round. No new deliverables; repairs land on Constraints, 1.2, 2.1, 2.2, 2.3, 3.1, 4.1, 4.3, 4.4, 4.5, 5.1, 5.2, 5.3, and 5.4 after this checkpoint.

```json plan-review-round
{"evidence_id":"4bc465d4-4922-4f02-984e-a34839bb087c","plan_hash":"e038d83f0611f82c8098d9bcaf23ef743145ba76013a08ac8601fd400156a62e","round_number":4,"round_result":{"coverage_attestation":{"adjacent_variant_complete":true,"attestation_digest":"590fb9278841d1ce26892601ed6f80d73296f10cc5c01c1ba74f1886ca4196d9","cross_lane_interaction_complete":true,"disposition_counts":{"dismissed":3,"emitted_findings":12,"total":15},"evidence_id":"4bc465d4-4922-4f02-984e-a34839bb087c","lanes":[{"candidate_count":6,"lane_id":"requirements_traceability","status":"completed"},{"candidate_count":2,"lane_id":"repository_blast_radius","status":"completed"},{"candidate_count":7,"lane_id":"runtime_invariants","status":"completed"}],"shadow_manifest_status":{"entry_count":18,"manifest_digest":"b6bf6e7eb3f864502b2a8225225aed4fbe3985d5350a7f9b8c11d8017267f1fd","status":"valid"},"source_digest":"98ab28c66b9d7bccb750c3195346fae43d1d1fc0e8f519a7c03a682d7ca9e2b4","version":1},"findings":[{"category":"unhandled-edge","causal_finding_id":"F-R3-014","causal_section_ids":["2.3","4.1","5.4"],"check_key":"unproven-picker-availability-closure","description":"`unproven` models are explicitly selectable for first-use activation, yet 5.1 marks the family available only when a model is `eligible` and 5.4 hides the family when none is eligible. A cold all-unproven family is hidden before activation can occur.","finding_id":"F-R4-001","fix":"Define the shared selectable predicate as `state in {eligible, unproven}` for route availability and picker visibility, reserve `eligible` for proven health, hide only all-ineligible or malformed families, and add one route-to-picker all-unproven acceptance case.","introduced_in_round":4,"location":"§§ 4.1 / 5.1 / 5.4 — all-unproven local catalog","prevention":"Test route serialization, provider availability, picker visibility, and first-use activation with a catalog whose every installed coding model is unproven.","principle":"Every state admitted for first-use activation must remain reachable through the selection surface.","root_cause":"The round-3 unproven-state repair changed row admission while leaving family availability keyed only to proven eligibility.","section_id":"5.4","severity":"blocking"},{"category":"weak-testability","check_key":"effective-context-launch-cap-acceptance","description":"The body defines `effective_context` as the minimum of canonical context, runtime context, and a supervisor launch cap, but every acceptance example omits the launch-cap branch. An implementation can ignore a 32,768 cap and still pass while admitting an undersized coding model.","finding_id":"F-R4-002","fix":"Add normalized-record and eligibility acceptance proving canonical/runtime 262,144 plus launch cap 32,768 yields effective context 32,768 and coding ineligibility, while launch cap 65,536 remains eligible when other checks pass.","location":"§§ 1.2 / 2.2 / 4.1 — effective context and coding threshold","prevention":"Enumerate canonical, runtime, and launch-cap minima and test each one as the controlling bound at 32,768 and 65,536.","principle":"Every hard limit in an eligibility minimum needs an executable boundary case.","repairs":[{"items":[{"artifact":"test: `tests/servers/test_local_provider_models.py::test_effective_context_applies_launch_cap`","prose":"Canonical and runtime context 262,144 with a supervisor launch cap of 32,768 yields effective_context 32,768 with launch-cap provenance, while a 65,536 cap yields 65,536."}],"kind":"add_acceptance","section_id":"1.2"},{"items":[{"artifact":"test: `tests/ai/local_runtime/test_eligibility.py::test_launch_cap_controls_coding_eligibility`","prose":"A model with canonical and runtime context 262,144 is coding-ineligible under a 32,768 launch cap and eligible at a 65,536 launch cap when every other check passes."}],"kind":"add_acceptance","section_id":"4.1"}],"root_cause":"Acceptance covers canonical-versus-runtime context while omitting the third input, the Gobby supervisor launch cap.","section_id":"1.2","severity":"blocking"},{"category":"weak-testability","check_key":"job-pagination-consumer-acceptance","description":"The plan requires the HTTP list, CLI `jobs`, and Settings job table to page through `limit` and an opaque cursor. Current 5.2 and 5.3 acceptance can pass while ignoring the cursor, truncating permanently, or accumulating unbounded client rows.","finding_id":"F-R4-003","fix":"Add separate CLI and Settings acceptance for cursor forwarding, subsequent pages, the 200 maximum, and bounded rendered/in-memory rows when retained history exceeds one page.","location":"§§ 2.3 / 5.2 / 5.3 — bounded job history consumers","prevention":"For each paginated projection, test first page, opaque cursor forwarding, next page, maximum limit, and bounded client state.","principle":"A bounded paginated API needs acceptance at every stateful consumer, not only at the route.","repairs":[{"items":[{"artifact":"test: `tests/cli/test_local_runtime.py::test_jobs_pages_with_bounded_output`","prose":"The jobs command forwards opaque cursors, honors the default 50 and maximum 200 limits, fetches later pages, and keeps text and JSON output bounded."}],"kind":"add_acceptance","section_id":"5.2"},{"items":[{"artifact":"test: `web/src/components/settings/inference/__tests__/LocalRuntimeSection.test.tsx`","prose":"LocalModelJobs forwards opaque cursors, renders at most 200 rows per page, and keeps component state bounded while navigating retained history beyond one page."}],"kind":"add_acceptance","section_id":"5.3"}],"root_cause":"The retention repair added cursor requirements in 2.3 without adding cursor and bounded-state acceptance to the CLI and Settings consumers.","section_id":"5.2","severity":"blocking"},{"category":"weak-testability","check_key":"acquisition-confirmation-server-enforcement","description":"Downloads and artifact acquisition require explicit confirmation, but no backend acceptance proves that provider, service, or direct HTTP job creation rejects missing or invalid confirmation before network send. Client checks alone can be bypassed.","finding_id":"F-R4-004","fix":"Add provider and route/service acceptance that rejects missing or invalid download confirmation before any provider request, accepts one confirmed idempotent request, and proves lease-driven load, unload, and idle eviction remain confirmation-free.","location":"§§ 2.1 / 2.3 — provider and HTTP acquisition boundary","prevention":"For every confirmation-gated operation, test missing, invalid, replayed, and valid confirmation at the innermost server-side boundary before external effects.","principle":"Authorization policy must be enforced and tested at the server-side mutation boundary.","repairs":[{"items":[{"artifact":"test: `tests/ai/local_runtime/test_provider_registry.py::test_download_confirmation_enforced_before_send`","prose":"Download without a valid caller confirmation fails before network send, one confirmed request proceeds idempotently, and lease-driven load and unload require no confirmation."}],"kind":"add_acceptance","section_id":"2.1"},{"items":[{"artifact":"test: `tests/servers/routes/test_local_runtime.py::test_download_job_requires_confirmation`","prose":"Direct HTTP artifact-acquisition job creation rejects missing, invalid, and replayed confirmation before provider send and accepts one confirmed idempotent request."}],"kind":"add_acceptance","section_id":"2.3"}],"root_cause":"Confirmation is tested in CLI and Settings while provider/service/direct HTTP rejection remains prose-only.","section_id":"2.3","severity":"blocking"},{"category":"missing-requirement","check_key":"bootstrap-coverage-ledger-companion","description":"`docs/contracts/plan-coverage.md` requires every new epic plan to ship an adversary-reviewed `.coverage-ledger.yaml` companion. No `local-inference-runtime-foundation.coverage-ledger.yaml` exists, so the independent acceptance-to-leaf parity record is absent.","finding_id":"F-R4-005","fix":"Create `.gobby/plans/local-inference-runtime-foundation.coverage-ledger.yaml`, bind it to plan ID `local-inference-runtime-foundation` and the current plan hash, enumerate all 123 acceptance items and 18 expected leaves, and include it in the next adversarial parity check.","location":"Plan companion artifacts","prevention":"Before approval, inventory the canonical plan, required coverage ledger, M1 manifest, and plan-hash bindings as one handoff set.","principle":"A new epic plan must ship every companion artifact required by the canonical coverage contract before expansion.","root_cause":"The plan has 123 acceptance items and a derived 18-entry manifest, while its required `.coverage-ledger.yaml` companion was never created.","section_id":"Overview","severity":"blocking"},{"category":"traceability","check_key":"embedding-reader-generation-fence-consumers","description":"`SemanticToolSearch.search_tools` and `EmbeddingBackend.search_async` split embedding generation from collection/ranking access. Neither consumer is targeted, so an inner embedding-call gate can release before search and let 3.2 publication interleave with a request the plan promises is indivisible.","finding_id":"F-R4-006","fix":"Target both outer consumers, make them hold one shared generation binding across embedding and collection/ranking access, and add a deterministic test that pauses after embedding while publication requests the exclusive side.","location":"§§ 3.1 / 3.2 — daemon-internal embed-and-search gate","prevention":"Trace every embed call through its subsequent collection or ranking access and target the outermost request owner.","principle":"A request-scoped consistency fence must target every caller that spans both sides of the protected tuple.","repairs":[{"entries":["`src/gobby/mcp_proxy/semantic_search.py::SemanticToolSearch.search_tools`","`src/gobby/search/backends/embedding.py::EmbeddingBackend.search_async`"],"kind":"add_targets","section_id":"3.1"},{"items":[{"artifact":"test: `tests/ai/test_embedding_binding.py::test_daemon_internal_search_holds_generation_gate`","prose":"Daemon-internal semantic tool search and embedding-backend search hold one shared generation across embedding and collection or ranking access, and switch publication waits while either request is paused between them."}],"kind":"add_acceptance","section_id":"3.1"}],"root_cause":"The plan places the gate around embedding binding while current semantic-search consumers await embedding and then access Qdrant or rankings outside that call.","section_id":"3.1","severity":"blocking"},{"category":"bad-sequencing","causal_finding_id":"F-R3-014","causal_section_ids":["2.3","4.1","5.4"],"check_key":"cold-profile-after-activation-order","description":"Section 4.3 has `resolve_spawn_generation_endpoint` produce a resolved local profile before the executor calls `acquire_role`. A cold model cannot supply the eligible effective context and probe evidence required by 4.2 until that acquisition completes, so it either cannot launch or launches with stale/pre-activation facts.","finding_id":"F-R4-007","fix":"Carry an unresolved local selection plus `LocalRuntimeService` to the executor, acquire the run-owned role lease first, then construct and validate the runtime profile from the activation result's exact model, effective context, modalities, and evidence before process launch; apply the same order on resume.","introduced_in_round":4,"location":"§§ 2.3 / 4.1 / 4.2 / 4.3 — cold local coding spawn","prevention":"Trace installed-unloaded-unprobed spawn and resume from selector resolution through acquisition, profile construction, and process launch.","principle":"A launch profile must be constructed from facts established before the profile is validated or used.","root_cause":"The round-3 cold-activation repair kept a fully resolved `local_profile` on `SpawnRequest` even though acquisition is what discovers its effective context and probe evidence.","section_id":"4.3","severity":"blocking"},{"category":"unhandled-edge","check_key":"probe-ambiguous-send-recovery","description":"Cancellation or daemon crash after probe bytes are written but before evidence persistence leaves no record. The next lookup can send the same evidence key again despite the plan's at-most-once promise.","finding_id":"F-R4-008","fix":"Persist a per-evidence-key attempt record before send, transition it to `possibly_sent` at the transport boundary, and recover that state as terminal failure evidence unless the transport supplies an idempotency key; add cancellation and crash injection at the bytes-written boundary.","location":"§ 4.1 — cancellation and crash after probe send","prevention":"Inject cancellation and daemon crash immediately before bytes-written, immediately after bytes-written, and before evidence commit for each wire.","principle":"An at-most-once external send needs durable classification of the possibly-sent state.","root_cause":"The single-flight registry and cancellation state are in-process, and evidence is written only after the response.","section_id":"4.1","severity":"blocking"},{"category":"unhandled-edge","causal_finding_id":"F-R3-020","causal_section_ids":["4.4"],"check_key":"terminal-intent-send-linearization","description":"If delivery owns the mutex and has started the external tmux send, terminalization can publish intent and cancel the task, but cancellation cannot retract pane input already dispatched. The claimed zero-key outcome for every terminalization that starts while delivery owns the mutex is unenforceable.","finding_id":"F-R4-009","fix":"Make mutex admission the linearization boundary: terminal intent published before delivery admission yields zero keys; delivery admitted first may complete its bounded send while terminal CAS waits, with the enforceable guarantee that no input occurs after terminal state. Replace the impossible overlap acceptance assertion with both ordered outcomes.","introduced_in_round":4,"location":"§ 4.4 — terminalization overlapping an in-flight tmux send","prevention":"Test terminal intent before mutex admission, before subprocess dispatch, after dispatch, and before terminal CAS, and state the permitted outcome at each point.","principle":"Cancellation cannot retract an external effect after dispatch; the invariant must use an enforceable linearization point.","root_cause":"The round-3 repair treats flag checks before `send_keys` as if they could prevent input after the tmux subprocess has begun.","section_id":"4.4","severity":"blocking"},{"category":"unhandled-edge","causal_finding_id":"F-R3-016","causal_section_ids":["4.5"],"check_key":"webchat-switch-postcommit-recovery","description":"After successor persistence, live-backend switching can fail with durable state naming the successor while the predecessor remains live and leased; predecessor release can also fail and leave two leases. Acceptance demands one persisted model and one lease, but no rollback, forward-retry, or cleanup state is defined, and the current persistence wrapper swallows write failures.","finding_id":"F-R4-010","fix":"Add `_streaming.py` and `_stream_persistence.py` to the design surface and define a durable transition record: selected model remains the predecessor until successor live attach succeeds; attach failure releases the successor; commit then advances selected model; predecessor-release failure records `cleanup_pending` and retries idempotently. Make persistence errors propagate and test every phase plus restart recovery.","introduced_in_round":4,"location":"§ 4.5 — live-backend and predecessor-release failures after successor preparation","prevention":"Inject failure after each durable and live transition and assert explicit persisted phase, live backend, lease ownership, retry, and user-visible outcome.","principle":"Every post-durable-write failure needs a specified forward or compensating transition.","root_cause":"The round-3 fixed order specifies compensation only for the durable selected-model write.","section_id":"4.5","severity":"blocking"},{"category":"unhandled-edge","causal_finding_id":"F-R3-017","causal_section_ids":["2.2"],"check_key":"vllm-prespawn-recovery-outcome","description":"A crash after the `launching` record is persisted but before spawn leaves a reserved port with no pid, listener, or process group. That state matches neither adoption nor killed-by-nonce recovery, although acceptance explicitly injects the crash.","finding_id":"F-R4-011","fix":"Add an explicit recovery branch for a `launching` record with no matching pid and no listener: mark it aborted, clear the ownership record, release the reserved port idempotently, and prove a subsequent load can reuse the port.","introduced_in_round":4,"location":"§ 2.2 — crash after reservation persistence and before spawn","prevention":"Crash after reservation, spawn, bind, pid checkpoint, and readiness; enumerate the exact record/listener/process outcome for each.","principle":"Every persisted launch phase needs an idempotent recovery outcome, including phases with no process.","root_cause":"The round-3 launch-intent repair enumerates adoptable processes, nonce-bearing orphans, and foreign listeners while omitting an empty reserved launch.","section_id":"2.2","severity":"blocking"},{"category":"traceability","causal_finding_id":"F-R3-005","causal_section_ids":["2.3"],"check_key":"runner-init-carrier-target-parity","description":"Acceptance 2.3.9 requires `tests/test_runner_init.py`, and round-3 resolution explicitly said to target it for carrier consumer coverage, but the current 2.3 Targets block omits the file.","finding_id":"F-R4-012","fix":"Add `tests/test_runner_init.py` to 2.3 Targets with scoped ownership of the single-service carrier and safely unconfigured initialization tests.","introduced_in_round":4,"location":"§ 2.3 Targets and acceptance 2.3.9","prevention":"After applying acceptance repairs, literal-sweep every referenced changed test file against the owning Targets block.","principle":"Every test file changed to prove a deliverable belongs in that deliverable's Targets inventory.","repairs":[{"entries":["`tests/test_runner_init.py::*` — scope-reason: cover the single LocalRuntimeService carrier and safely unconfigured initialization path"],"kind":"add_targets","section_id":"2.3"}],"root_cause":"The round-3 carrier repair added `tests/test_runner_init.py` to acceptance and resolution notes while omitting it from the current Targets block.","section_id":"2.3","severity":"blocking"}],"reviewer_session":"0ac873ce-d006-4a53-853d-5e592ee444dd","round":4,"round_number":4,"verdict":"needs_review"},"session_id":"4b85e4ac-ec4c-4fea-b191-8a2d71162afb"}
```

**Round 5** `kind: verification`

- reviewer_run: 8f1d32d5-0266-4915-9046-24d390727998
- reviewer_session: 8020e12e-9999-4012-a604-08ac537e1453
- verdict: needs_review
- findings:
- F-R5-001 / blocking / 2.3.11 requires the 4.1 probe and eligibility inside `acquire_role`, but 4.1 depends on 2.3 through P3, so the leaf cannot pass its own gate
- F-R5-002 / blocking / 4.5 has no acceptance for first web-chat use of an installed, unloaded, unprobed `unproven` model
- F-R5-003 / blocking / Constraints ledger obligation omits the contract's review-before-expansion gate (fixer-induced, F-R4-005)
- F-R5-004 / nit / 5.4 depends on 5.3 while consuming only the 5.1 provider projection
- F-R5-005 / blocking / 4.5 treats `attach_acp_block` response enrichment as persistence of the backend cache identity; session storage keeps only `model`
- F-R5-006 / blocking / 4.4 omits the direct terminal CAS writers behind `shielded_terminal_delivery` (HTTP cancel, build stop, dispatch cleanup, WebSocket observe/continue)
- F-R5-007 / blocking / 3.1 adds loop branching to the 864-line `_text_generation_service.py` with no split seam or size audit
- F-R5-008 / blocking / 3.2 targets `bootstrap_io` but not `BootstrapConfig`, which owns pending-profile parsing and projection
- F-R5-009 / blocking / 2.3 retains job idempotency keys forever while pruning the terminal jobs they reference
- F-R5-010 / blocking / 4.4 cancels admitted delivery tasks, so a cancelled tmux send can land after the mutex is released (fixer-induced, F-R4-009)
- F-R5-011 / blocking / 4.5.6 demands one lease immediately after an injected predecessor-release failure that the prose defers to a later release path (fixer-induced, F-R4-010)
- F-R5-012 / blocking / 4.3 lease conversion and launch admission are outside the per-run fence, so a terminalized pending run can still take a lease and launch (fixer-induced, F-R4-007)
- resolution_notes: Unattended round; the coordinator judged every finding. All 12 accepted, six with narrowed repairs. F-R5-002 and F-R5-008 accepted as their typed repairs (`tests/servers/websocket/chat/test_runtime_manager.py` is already a 4.5 Target; `src/gobby/config/bootstrap.py` is 408 lines and `tests/config/test_bootstrap.py` exists). F-R5-004 accepted: 5.4 depends on 5.1, and the two leaves share no Target. F-R5-007 accepted: the typed attempt outcome and candidate classification move into a new `src/gobby/ai/_text_generation_attempts.py`, the service's two loops gain only the call, the file joins the Constraints size inventory, and 3.1.5 audits three files. F-R5-009 accepted: an idempotency record is pruned with the terminal job it references (same 7-day/200-job window), active-job keys are never pruned, and a replay after pruning is a new job; 2.3.12 proves fixed bounds and replay after expiry. F-R5-001 accepted narrowed as injection rather than reordering: 2.3 defines a `CodingActivationEvaluator` protocol taken by the service constructor (default `None`, in which case coding acquisition converts after load and context refresh with no eligibility step), `acquire_role` invokes the injected evaluator exactly once after the context refresh, and 2.3.11 proves the activation contract with a fake evaluator; 4.1 implements the evaluator from its probe registry and eligibility rules, wires it in `runner_init/servers.py`, and gains acceptance 4.1.10 in `tests/ai/local_runtime/test_service.py`. F-R5-003 accepted narrowed: the ledger is a mechanical derivation of the adversary-approved acceptance items and M1 manifest entries, so the handoff obligation becomes generate, verify header binding and acceptance-to-leaf parity against the approved manifest, and block expansion until that verification passes; a separate adversary round for a derived file is declined as unneeded mechanism. F-R5-005 accepted narrowed: backend instances are process-local, so the cache identity is kept in an in-process registry in `runtime_manager.py` keyed by session id and written with the live attach under the per-conversation lock; `ACPSessionLifecycleService` resolves through that registry and, when no live entry exists (after restart), re-derives the identity from the persisted `model`, provider, and current profile, which owns no lease and releases nothing; `attach_acp_block` keeps its serializer role and the sessions schema is unchanged, so the suggested migration is declined. F-R5-006 accepted narrowed: the four direct CAS writers (`cancel_agent_run`, `_cancel_active_agents`, `cleanup_unattached_spawned_run`, `_release_source_session`) route their existing CAS through `terminalize(run_id, cas)` and join the 4.4 Targets with their tests; integrating the fence into `shielded_terminal_delivery` is declined because that wrapper also shields non-terminal work (capture storage offloads keyed by run id, terminal re-read delivery, and the multi-run `stale-sweeps` operation), so fencing it would cancel live delivery on every capture write. F-R5-010 accepted narrowed to the first option: terminalization cancels only delivery tasks not yet admitted to the mutex (admission is recorded in the registry under the mutex; pre-admission work is capture and sleep with no pane side effect), an admitted task is never cancelled and completes its dispatched bounded send before aborting at its next recheck, and 4.4.4 adds the in-flight-send race; no tmux child kill-and-reap path is introduced. F-R5-011 accepted narrowed: the prose now names the immediate state after a failed predecessor release (one persisted model, one live backend, the predecessor lease retained and reported in the lease inventory under the conversation id) and 4.5.6 proves convergence to one lease on the next release path or restart reconciliation; the suggested `predecessor_release_pending` state and retry bound are declined because the lease inventory already exposes the retained lease and release is idempotent. F-R5-012 accepted narrowed: the per-run fence is owned by 4.4, so 4.4 also fences the activation-to-launch boundary in the spawn and resume executors — after `acquire_role` returns, the executor enters the run's mutex, re-reads terminal intent and durable run status, releases the lease and aborts when terminal, and holds the mutex through process creation — with acceptance 4.4.10 racing terminalization against lease retention and launch, including no-monitor cancellation. No new deliverables; repairs land on Constraints, 2.3, 3.1, 3.2, 4.1, 4.3, 4.4, 4.5, and 5.4 after this checkpoint.

```json plan-review-round
{"evidence_id":"932acc0d-24a2-4e25-b59f-fb761eaaa9fd","plan_hash":"ffa465306b80e062c02c24460698105cf15d4fdec0c1f8887924e6e7afe0fc72","round_number":5,"round_result":{"coverage_attestation":{"adjacent_variant_complete":true,"attestation_digest":"c7e2d0cbd7cfb73a923ea4b0997c38956b096f2397c32692053e2286973122af","cross_lane_interaction_complete":true,"disposition_counts":{"dismissed":1,"emitted_findings":12,"total":13},"evidence_id":"932acc0d-24a2-4e25-b59f-fb761eaaa9fd","lanes":[{"candidate_count":4,"lane_id":"requirements_traceability","status":"completed"},{"candidate_count":4,"lane_id":"repository_blast_radius","status":"completed"},{"candidate_count":5,"lane_id":"runtime_invariants","status":"completed"}],"shadow_manifest_status":{"entry_count":18,"manifest_digest":"6995bb1cb4e073fd1c7ee23c500e5d0033af8d5afc477e77b0f2e0da2507aa5f","status":"valid"},"source_digest":"765fe66427875da8d81c8ae21480f378669ea56799e4ec8a26ec9d9e57653dac","version":1},"findings":[{"category":"bad-sequencing","check_key":"downstream-owner-acceptance-cycle","description":"Acceptance 2.3.11 requires `acquire_role` to run the required-wire probe and eligibility evaluation, but §4.1 exclusively owns those new implementations and depends transitively on §2.3 through P3. The 2.3 leaf cannot pass its own validation before its downstream owner exists.","finding_id":"F-R5-001","fix":"Move or split the probe/eligibility primitives into a deliverable before 2.3 and make 2.3 depend on it, or move activation integration and acceptance 2.3.11 into a later leaf that depends on both the service and the probe/eligibility owner.","location":"§ 2.3 acceptance 2.3.11 and § 4.1 probe/eligibility ownership","prevention":"For every acceptance item, resolve each required production symbol to its owning deliverable and verify the dependency graph orders that owner first.","principle":"A leaf must depend on every production artifact required to satisfy its own acceptance gate.","root_cause":"Cold-role activation was added to the service leaf while the probe and eligibility implementations remained in a later leaf that transitively depends on that service.","section_id":"2.3","severity":"blocking"},{"category":"weak-testability","check_key":"unproven-webchat-first-use-acceptance","description":"The plan exposes `unproven` local models as selectable, yet §4.5 has no acceptance case proving first web-chat use acquires and activates an installed, unloaded, unprobed model and then either continues when proof succeeds or returns the resulting typed ineligibility.","finding_id":"F-R5-002","fix":"Add a §4.5 acceptance case starting with an installed, unloaded, unprobed `local:coding/<model>` selection and covering successful first-use activation plus failed-probe or short-context rejection before thread creation.","location":"§ 4.5 web-chat admission and § 5.4 selectable `unproven` rows","prevention":"For every newly selectable state, trace one test from projection through selection, consumer activation, and both success and typed failure.","principle":"Every selectable state needs end-to-end acceptance through the consumer that resolves it.","repairs":[{"items":[{"artifact":"test: `tests/servers/websocket/chat/test_runtime_manager.py::test_unproven_model_activates_on_first_message`","prose":"An installed, unloaded, unprobed local:coding model selected for web chat acquires the conversation lease, activates and probes on first use, continues when proven eligible, and returns typed ineligibility without thread creation when proof fails."}],"kind":"add_acceptance","section_id":"4.5"}],"root_cause":"Route and picker acceptance proves that `unproven` rows are visible, while web-chat acceptance only exercises already-eligible model routing.","section_id":"4.5","severity":"blocking"},{"category":"missing-requirement","causal_finding_id":"F-R4-005","causal_section_ids":["Constraints"],"check_key":"bootstrap-ledger-adversarial-review-gate","description":"`docs/contracts/plan-coverage.md` requires every new epic plan's `.coverage-ledger.yaml` companion to be adversary-reviewed before expansion. Constraints schedule generation at build handoff but omit that review gate, so an unreviewed ledger can reach expansion.","finding_id":"F-R5-003","fix":"Extend the handoff obligation to generate the ledger after `root_task_ref` and the approved `plan_hash` exist, adversarially review complete acceptance-to-leaf parity, and block expansion until that review passes.","introduced_in_round":5,"location":"Constraints — coverage-ledger handoff obligation","prevention":"When translating a repository contract into a handoff obligation, copy generation, review, identity binding, and blocking order as separate checklist items.","principle":"A required pre-expansion artifact must include every gate imposed by its governing contract.","root_cause":"The round-4 repair added ledger generation after approval but dropped the contract's separate adversarial-review-before-expansion requirement.","section_id":"Constraints","severity":"blocking"},{"category":"bad-sequencing","check_key":"dependency-minimality","description":"Section 5.4 depends on 5.3, but its Targets and body consume the provider payload and selectable predicate from 5.1 and no Settings surface, role editor, job table, or `localRuntime` artifact owned by 5.3.","finding_id":"F-R5-004","fix":"Change 5.4 to depend directly on 5.1, or name and target the specific 5.3 artifact that 5.4 genuinely consumes.","location":"§ 5.4 heading `(depends: 5.3)`","prevention":"Expand each dependency to concrete consumed Targets and remove edges with no shared artifact or behavioral precondition.","principle":"A dependency edge should represent a consumed output or implementation precondition.","root_cause":"The picker leaf was serialized behind the sibling Settings leaf despite consuming only the shared provider projection from 5.1.","section_id":"5.4","severity":"nit"},{"category":"traceability","check_key":"persisted-backend-identity-storage-closure","description":"Section 4.5 promises to persist runtime, family, normalized model identity, context, modalities, and profile hash, but its Targets contain no session schema/model/storage migration. Current `attach_acp_block` only enriches an in-memory response, so restart and ACP lifecycle routing cannot recover the promised model-scoped backend identity.","finding_id":"F-R5-005","fix":"Add a durable session backend-identity field through the next numbered migration, schema assets, `Session` row/dict mapping, web-chat create/update storage seams, and focused storage tests. Persist selected model plus validated backend identity atomically, then project the persisted value from `attach_acp_block`.","location":"§ 4.5 persisted ACP backend identity and Targets","prevention":"For every new durable field, sweep schema migrations, baselines/catalogs, row models, create/update paths, serializers, restart readers, and focused storage tests.","principle":"A promised durable identity needs a schema field, atomic write seam, model projection, migration, and recovery reader in scope.","root_cause":"The plan treats `attach_acp_block` response enrichment as persistence even though current session storage retains only `model` and `is_local`, and `persist_model_switch` updates only `model`.","section_id":"4.5","severity":"blocking"},{"category":"traceability","check_key":"terminal-delivery-shared-seam-coverage","description":"Existing production paths can terminalize through `shielded_terminal_delivery`, yet that shared seam and its tests are absent from §4.4 Targets. Updating only the named facades leaves HTTP cancellation, build cancellation, dispatch cleanup, WebSocket observe/continue, and other callers able to bypass the new prompt-delivery fence.","finding_id":"F-R5-006","fix":"Target `src/gobby/agents/terminal_delivery.py::shielded_terminal_delivery` and `tests/agents/test_terminal_delivery.py::*`; integrate the existing cancellation shield with `run_lifecycle_fence.terminalize`, and add representative direct-caller races proving terminal intent reaches the shared fence.","location":"§ 4.4 every terminal-state writer and Targets","prevention":"Resolve all direct terminal CAS and delivery-wrapper callers, then place the invariant at one shared seam and test representative caller families.","principle":"A cross-cutting lifecycle invariant must be enforced at the shared seam traversed by every writer.","root_cause":"The plan enumerates several completion facades but omits `shielded_terminal_delivery`, through which additional HTTP, build, dispatch, WebSocket, and no-monitor callbacks execute terminal CAS operations.","section_id":"4.4","severity":"blocking"},{"category":"traceability","check_key":"size-sensitive-target-decomposition","description":"`src/gobby/ai/_text_generation_service.py` is already 864 lines and §3.1 adds typed local attempt/send-state branching to two large candidate loops. The Constraints size inventory and acceptance 3.1.5 omit this file, leaving implementation at risk of crossing the 1,000-line hard ceiling.","finding_id":"F-R5-007","fix":"Create a focused module such as `src/gobby/ai/_text_generation_attempts.py`, move candidate-attempt classification or the result/JSON loops into it before adding local pre-send semantics, keep the existing service to thin orchestration, and extend 3.1.5 to audit this split.","location":"§ 3.1 `_text_generation_service.py` Target and acceptance 3.1.5","prevention":"Recompute line counts after every target repair and add each newly near-ceiling production file to the owning decomposition and acceptance inventory.","principle":"Material behavior added to a near-ceiling production module needs an owned decomposition seam and a line-count gate.","root_cause":"The round-3 fallback repair targeted an 864-line service but the plan's size inventory and split acceptance continued to cover only embeddings and runner-init services.","section_id":"3.1","severity":"blocking"},{"category":"traceability","check_key":"pending-bootstrap-schema-target-parity","description":"Section 3.2 introduces and promotes pending local profiles and claims runtime-contract freshness, but targets only `bootstrap_io`. Durable publication validates through `bootstrap_from_mapping`, while `BootstrapConfig` owns typed parsing and projection, so the declared Targets cannot implement the stated typed pending state.","finding_id":"F-R5-008","fix":"Add `bootstrap.py` and its tests to 3.2, define a typed pending-profile envelope in `BootstrapConfig`, and cover parse, serialize, invalid shapes, atomic promotion, and pending-state clearing.","location":"§ 3.2 pending-profile bootstrap promotion and Targets","prevention":"When adding persisted bootstrap fields, include schema model, parser, serializer, ownership projection, contract asset, and round-trip tests in one target sweep.","principle":"Every typed persisted state transition must own its parser, serializer, projection, and round-trip tests.","repairs":[{"entries":["`src/gobby/config/bootstrap.py::*` — scope-reason: add typed pending local-profile parsing, serialization, and promotion required by bootstrap_io","`tests/config/test_bootstrap.py::*` — scope-reason: cover pending-profile round trips, invalid shapes, promotion, and clearing"],"kind":"add_targets","section_id":"3.2"},{"items":[{"artifact":"test: `tests/config/test_bootstrap.py::test_pending_local_profile_round_trip_and_promotion`","prose":"Active and pending local profiles parse and round-trip through BootstrapConfig; promotion writes one active profile and clears pending state, and invalid pending envelopes are rejected."}],"kind":"add_acceptance","section_id":"3.2"}],"root_cause":"Section 3.2 targets bootstrap I/O and the derived contract while omitting `BootstrapConfig`, which owns validation and `to_config_dict` projection for the pending fields it claims to add.","section_id":"3.2","severity":"blocking"},{"category":"unhandled-edge","check_key":"bounded-idempotency-retention","description":"Section 2.3 calls persisted job state bounded but says job idempotency keys are never pruned. Unique completed requests therefore grow durable state without limit, and retained keys can reference terminal jobs that have already been removed.","finding_id":"F-R5-009","fix":"Retain terminal-job idempotency records only for the same seven-day/200-result window, or replace them with bounded tombstones carrying an explicit expired-key response. Keep only active-job keys unpruned and add a test proving fixed storage bounds and deterministic replay after expiry.","location":"§ 2.3 persisted job retention and acceptance 2.3.12","prevention":"For each retention policy, enumerate payloads, secondary indexes, idempotency records, ownership records, and replay behavior after expiry.","principle":"A bounded durable queue must bound every terminal index as well as its payload rows.","root_cause":"Terminal jobs are pruned while their idempotency keys are explicitly retained forever, with no tombstone bound or replay contract after the referenced job disappears.","section_id":"2.3","severity":"blocking"},{"category":"unhandled-edge","causal_finding_id":"F-R4-009","causal_section_ids":["4.4"],"check_key":"cancelled-tmux-child-settlement","description":"Terminalization cancels every registered delivery task before acquiring the mutex, including a task already awaiting `set-buffer`, `paste-buffer`, or `send-keys`. Current wrappers do not settle `CancelledError`, so the task can release the mutex while the tmux child continues and pane input can land after the terminal CAS.","finding_id":"F-R5-010","fix":"Publish terminal intent and cancel only pre-admission delivery while letting an admitted bounded send finish under the mutex, or make cancellation kill and await every spawned tmux child before the delivery task can release the mutex. Add deterministic races after subprocess creation for all send paths.","introduced_in_round":5,"location":"§ 4.4 terminalization cancellation before mutex acquisition","prevention":"Inject cancellation after each external subprocess is created and prove the child is either awaited under the fence or killed and reaped before terminal CAS.","principle":"Cancelling an async wrapper cannot release a lifecycle fence while its external side-effecting child may still complete.","root_cause":"The repaired fence cancels admitted delivery tasks, while current tmux subprocess wrappers clean up on timeout only; `CancelledError` can unwind the task and release the mutex without killing and awaiting the child.","section_id":"4.4","severity":"blocking"},{"category":"unhandled-edge","causal_finding_id":"F-R4-010","causal_section_ids":["4.5"],"check_key":"predecessor-release-convergence","description":"After successor attachment and durable model persistence, predecessor release may fail. The prose allows cleanup only on a later clear, disconnect, shutdown, or restart, while acceptance 4.5.6 requires that injected failure to leave exactly one lease; the specified transition can temporarily leave two.","finding_id":"F-R5-011","fix":"Define a fixed retry bound and a typed `predecessor_release_pending` state backed by the existing conversation lease-owner inventory; on exhaustion keep the successor persisted/live, surface the retained predecessor, and make acceptance prove convergence on the next lifecycle cleanup or restart instead of asserting an immediate single lease.","introduced_in_round":5,"location":"§ 4.5 final predecessor-release step and acceptance 4.5.6","prevention":"For each switch step, write the immediate state, surfaced result, retry owner, crash recovery, and eventual invariant before writing acceptance.","principle":"Every declared failure injection must have an observable state that satisfies its acceptance invariant or an explicitly bounded transient state.","root_cause":"The round-4 repair deferred failed predecessor cleanup to a later lifecycle event while its new acceptance still requires exactly one lease immediately after that injected failure.","section_id":"4.5","severity":"blocking"},{"category":"unhandled-edge","causal_finding_id":"F-R4-007","causal_section_ids":["4.3"],"check_key":"terminalization-versus-launch-admission","description":"A pending run can terminalize while cold activation is in flight. Cleanup may observe no lease; activation can then convert to a run-owned lease and continue to profile construction or process launch because neither conversion nor launch admission rechecks terminal intent under the run fence.","finding_id":"F-R5-012","fix":"After activation completes, enter the per-run lifecycle fence before retaining the run lease, re-read terminal intent and durable run status, release and abort if terminal, and keep the fence through process-launch admission. Add paused spawn and resume races before lease conversion and process creation, including no-monitor cancellation.","introduced_in_round":5,"location":"§§ 2.3 / 4.3 / 4.4 activation-to-launch boundary","prevention":"Pause before lease conversion and before process creation, race every terminal path, and assert terminal state prevents both resource ownership and launch.","principle":"A terminal run must be unable to acquire new resources or launch after terminal cleanup has passed.","root_cause":"The post-round-4 order acquires a cold role before profile construction, but lease conversion and process-launch admission remain outside the per-run lifecycle fence used by terminalization.","section_id":"4.3","severity":"blocking"}],"reviewer_session":"8020e12e-9999-4012-a604-08ac537e1453","round":5,"round_number":5,"verdict":"needs_review"},"session_id":"4b85e4ac-ec4c-4fea-b191-8a2d71162afb"}
```

**Round 6** `kind: verification`

- reviewer_run: f07b15d4-c13b-480b-b60d-aacb3b405a26
- reviewer_session: b3a6d6b6-5e2b-4c94-8ba1-9188e09dc38c
- verdict: needs_review
- findings:
- F-R6-001 / blocking / 2.3 converts a coding activation to a lease when no `CodingActivationEvaluator` is injected, contradicting the Constraints fail-closed rule (fixer-induced, F-R5-001)
- F-R6-002 / blocking / 4.3.6 still requires terminal-CAS race correctness that the downstream 4.4 fence owns (fixer-induced, F-R5-012)
- F-R6-003 / blocking / Constraints ledger obligation substitutes mechanical verification for the contract's adversary review before expansion (fixer-induced, F-R5-003)
- F-R6-004 / blocking / 4.4 omits live terminal writers: `kill._close_tmux_session` default CAS, `SessionCoordinator._terminate_agent_run_inline`, `_deferred_tmux_health_check`, and `EnforcementCheckMixin._flush_pending_terminal_denial`
- F-R6-005 / blocking / 4.3 Targets omit the `LocalRoleSelection` producer files `local_profiles/contracts.py` and `_generation_endpoint.py` (fixer-induced, F-R4-007)
- F-R6-006 / blocking / 2.1/2.3/5.2/5.3 require a confirmation token with no issuer, binding, consumption, or ordering against idempotency (fixer-induced, F-R4-004)
- F-R6-007 / blocking / 2.3 bounds terminal job history but never bounds queued/running admission (fixer-induced, F-R5-009)
- F-R6-008 / blocking / 4.1 persisted transport evidence keyed by mutable fingerprints has no supersession or retention bound
- F-R6-009 / blocking / 4.4 per-run fence registry has no entry teardown rule, so entries leak or a late actor can split the mutex (fixer-induced, F-R5-010)
- F-R6-010 / nit / 2.3 idle TTL is labelled configurable with no consumer, field, or second value
- resolution_notes: Unattended round; the coordinator judged every finding. All ten accepted, four with narrowed repairs. F-R6-001 accepted: the Constraints rule "missing runtime controls fail closed" governs, so a coding acquisition with no injected evaluator returns the typed failure `coding_evaluator_unavailable` and no lease (text, vision, and embeddings acquisition are unaffected), the constructor keeps `coding_evaluator=None` as its default so 2.3 remains constructible before 4.1, 2.3.11 proves the no-evaluator refusal, and 4.1.10 stays the production-wiring proof. F-R6-002 accepted as the first option: 4.3.6 narrows to acquisition, process-start, ordinary launch-failure, non-admission cancellation, and daemon-crash/restart-reconciliation injections, and activation-to-launch terminal races live only in 4.4.11. F-R6-003 accepted and the round-5 narrowing reversed: `docs/contracts/plan-coverage.md` states the ledger is adversary-reviewed before expansion, so the Constraints obligation gains a fourth step, a taskless adversary review of the exact bound ledger after mechanical verification, and expansion is blocked until that review approves; a rejected ledger is regenerated or corrected and re-reviewed without editing the approved plan. F-R6-004 accepted: repository inspection confirms all four writers (`kill._close_tmux_session` passes no callback so `terminate_managed_tmux_async` reaches `capture._default_terminalize`; `_terminate_agent_run_inline` runs `capture_then_kill_sync` with a `complete`/`fail` CAS on the terminal-delivery offload thread; `_deferred_tmux_health_check` calls `run_storage.fail` directly; `_flush_pending_terminal_denial` calls `storage.fail` on the engine offload thread), while every other `terminate_managed_tmux_async`/`capture_then_kill_async` caller (`agent_health.py`, `lifecycle_monitor.py`, `lifecycle_reconciliation.py`, `memory_watchdog.py`, `watchdog/recovery.py`) already terminalizes through `cleanup_agent` and the fenced handler methods, and `_failure_cleanup.py` passes a non-writing `keep_run` callback. The four writers and their focused tests join the 4.4 Targets; `run_lifecycle_fence.py` gains `terminalize_from_thread(run_id, cas)` for the two synchronous off-loop writers, which schedules the fence entry onto the daemon loop with `run_coroutine_threadsafe`, runs the caller's synchronous CAS on the calling thread while the loop-side entry holds the mutex, and releases and schedules lease release through the same bridge; acceptance 4.4.12 covers the four paths. F-R6-005 accepted as its typed repair (both producer symbols resolve in `_generation_endpoint.py`; `contracts.py` is created by 4.2, so its Target is a bare path). F-R6-006 accepted narrowed to a preview-bound confirmation rather than issued tokens: artifact-acquisition job creation takes `confirm: bool` beside the exact artifact identity; `confirm=False` is the preview and returns the normalized identity, the resolved digest/revision, and known size without creating a job; a confirmed request is processed in one job-creation transaction that first returns an idempotency hit on the single-flight key (the lost-response retry path), otherwise rejects an identity whose resolved digest/revision differs from the previewed one with `confirmation_stale`, and otherwise persists the job with `confirmed=True`; the 2.1 adapter refuses an unconfirmed job before network send; `--yes` and the Settings dialog resubmit the previewed identity with `confirm=True`. Issued opaque tokens, expiry, and consumed-token retention are declined because the single-flight key already makes a confirmed retry idempotent and the digest/revision binding already detects a changed artifact. F-R6-007 accepted narrowed: `jobs.py` gains a fixed `MAX_ACTIVE_JOBS` ceiling on queued/running jobs enforced before persistence with the typed `job_capacity_exceeded` rejection (idempotency hits still return the existing job), and restart reconciliation fails a queued/running job with `orphaned_after_restart` when the provider reports no matching download and no owned process is adopted for it; per-family limits, heartbeats, deadlines, and a stall state are declined because provider progress is already a subscribed stream, cancellation is the operator remedy for a hung provider, and the ceiling bounds persisted state. F-R6-008 accepted narrowed: `probes.py` keeps exactly one evidence record per normalized model identity and wire, a successful or failed write for a key deletes every other record for that model and wire in the same atomic write (an in-flight in-process probe is unaffected because it is not yet stored), and startup reconciliation prunes records whose model is no longer installed before eligibility projection; a bounded diagnostic history is declined as unneeded mechanism, and 4.1.11 proves the bound under repeated key churn. F-R6-009 accepted: registry lookup, creation, and removal happen under one module-level lock; an entry is removed by the last releaser when the run's durable status is terminal and the entry has no mutex holder or waiter and no registered delivery task, so a late actor either finds the live entry or creates a fresh one after removal, never both; a fresh entry's CAS is a compare-and-set against durable terminal state and performs no side effect; 4.4.8 generalizes to every terminal path and launch failure under volume. F-R6-010 accepted: idle TTL becomes the module-level constant `IDLE_TTL_SECONDS` in `leases.py`. No new deliverables; repairs land on Constraints, 2.1, 2.3, 4.1, 4.3, 4.4, 5.2, and 5.3 after this checkpoint.

```json plan-review-round
{"evidence_id":"96136cf5-e9cf-489d-9acc-71e7ef61ef0a","plan_hash":"31886e3893bf1d6cb596afd4a4c751d501b6405ea5658d5ad88c2d629002c1ea","round_number":6,"round_result":{"coverage_attestation":{"adjacent_variant_complete":true,"attestation_digest":"454674d85aa31e9c2a376ea9de84ec72b667c27dc99b66c743d19f01090ffadd","cross_lane_interaction_complete":true,"disposition_counts":{"dismissed":1,"emitted_findings":10,"total":11},"evidence_id":"96136cf5-e9cf-489d-9acc-71e7ef61ef0a","lanes":[{"candidate_count":4,"lane_id":"requirements_traceability","status":"completed"},{"candidate_count":2,"lane_id":"repository_blast_radius","status":"completed"},{"candidate_count":5,"lane_id":"runtime_invariants","status":"completed"}],"shadow_manifest_status":{"entry_count":18,"manifest_digest":"eb71f1d8cf85fc9b6e4121f9d2a556ee2eb0fb177d39e56b2fd7b37815743283","status":"valid"},"source_digest":"cc809b7593236c2a3d2351e1934440525bab391fe453e2a1dafb5587f3e96d7f","version":1},"findings":[{"category":"unhandled-edge","check_key":"fail-closed-coding-evaluator","description":"`LocalRuntimeService` explicitly converts a coding activation to an owner lease when `coding_evaluator` is absent, despite the plan-wide rule that missing runtime controls fail closed. Production wiring in §4.1 does not make the public constructor, direct acquire endpoint, or §2.3 acceptance contract safe.","finding_id":"F-R6-001","fix":"Require an evaluator whenever coding acquisition is exposed, or install a rejecting default that returns `coding_evaluator_unavailable`; change 2.3.11 so the no-evaluator case yields no lease and keep 4.1.10 as the successful production-wiring proof.","location":"§ 2.3 CodingActivationEvaluator fallback and acceptance 2.3.11","prevention":"Exercise every protected service operation with each optional enforcement collaborator absent and require a typed refusal before resource acquisition.","principle":"A missing enforcement dependency must fail closed before a protected capability is acquired.","root_cause":"The optional evaluator was used as a sequencing seam, and its None branch was defined as successful coding activation.","section_id":"2.3","severity":"blocking"},{"category":"bad-sequencing","check_key":"downstream-owner-acceptance-cycle","description":"Acceptance 4.3.6 still requires terminal-CAS race correctness, while §4.3 says §4.4 owns the required lifecycle mutex and §4.4 depends on §4.3. The §4.3 leaf cannot satisfy that part of its gate before its downstream fence exists.","finding_id":"F-R6-002","fix":"Narrow 4.3.6 to lease acquisition, ordinary launch failure, non-admission cancellation, and restart reconciliation, leaving activation-to-launch terminal races solely in 4.4.11; alternatively move the fence owner ahead of §4.3 and update dependencies.","location":"§ 4.3 acceptance 4.3.6 and downstream § 4.4.11","prevention":"Resolve every race acceptance item to the leaf that owns its synchronization primitive and verify dependency order before handoff.","principle":"A leaf must depend on every production artifact required to satisfy its own acceptance gate.","root_cause":"The activation-to-launch fence moved to §4.4 while §4.3 retained a broad terminal-CAS race criterion.","section_id":"4.3","severity":"blocking"},{"category":"missing-requirement","check_key":"bootstrap-ledger-adversarial-review-gate","description":"`docs/contracts/plan-coverage.md` requires every new epic ledger to be adversary-reviewed before expansion. The repaired Constraints run only mechanical header/parity verification and request a fresh plan round only for non-derived content, so a derived ledger can still reach expansion without that review.","finding_id":"F-R6-003","fix":"After ledger generation and mechanical verification, require a taskless adversarial review of the exact bound ledger and block expansion until it approves.","location":"Constraints coverage-ledger handoff obligation","prevention":"Copy generation, identity binding, mechanical verification, adversarial review, and expansion blocking as separate checklist steps for each required handoff artifact.","principle":"A required pre-expansion artifact must pass every gate imposed by its governing contract.","root_cause":"The repair equates `bootstrap_ledger.py` header/parity verification with the contract's separate adversarial-review requirement.","section_id":"Constraints","severity":"blocking"},{"category":"traceability","check_key":"terminal-fence-writer-closure","description":"Several live terminal writers remain outside Targets: `kill._close_tmux_session` reaches `capture._default_terminalize` through `terminate_managed_tmux_async`, `SessionCoordinator._terminate_agent_run_inline` completes or fails runs on a managed synchronous executor, `_deferred_tmux_health_check` fails runs, and `EnforcementCheckMixin._flush_pending_terminal_denial` fails runs. They can bypass both the prompt-delivery fence and exactly-once run-lease cleanup.","finding_id":"F-R6-004","fix":"Inventory these production symbols and focused tests in §4.4, define how the synchronous hook executor enters the per-run fence, and route every CAS through the fence plus lease cleanup; where an outer operation already owns terminalization, remove or override the nested default CAS.","location":"§§ 4.3–4.4 terminal writer and lease-cleanup inventory","prevention":"Literal-sweep complete/fail/cancel/kill storage writes and every default terminal callback, then inspect sync/async bridges and focused tests for each caller family.","principle":"Every writer of a cross-cutting terminal state must traverse the same synchronization and cleanup seam.","root_cause":"The writer sweep stopped at named outer facades and missed nested default callbacks, hook-thread terminalization, deferred health failure, and enforcement denial.","section_id":"4.4","severity":"blocking"},{"category":"traceability","check_key":"spawn-profile-carrier-closure","description":"Section 4.3 says `LocalRoleSelection` is added in `local_profiles/contracts.py` and returned by `resolve_spawn_generation_endpoint` through `SpawnGenerationEndpointResolution`, yet its Targets omit both producer files. The leaf's declared inventory cannot implement the carrier proven by 4.3.8.","finding_id":"F-R6-005","fix":"Add the carrier schema, result type, and resolver symbols to §4.3 Targets; the existing 4.3.8 acceptance already covers the producer-to-executor path.","location":"§ 4.3 unresolved local-selection producer Targets","prevention":"Trace every new carrier field from type definition through producer, assembly, constructor, executor, resume path, fakes, and focused tests.","principle":"A changed typed carrier requires explicit ownership of its schema and producer as well as its consumers.","repairs":[{"entries":["`src/gobby/agents/local_profiles/contracts.py`","`src/gobby/mcp_proxy/tools/spawn_agent/_generation_endpoint.py::SpawnGenerationEndpointResolution`","`src/gobby/mcp_proxy/tools/spawn_agent/_generation_endpoint.py::resolve_spawn_generation_endpoint`"],"kind":"add_targets","section_id":"4.3"}],"root_cause":"The cold-activation repair updated the consumer path while leaving the producer files assigned only to §4.2's earlier resolved-profile shape.","section_id":"4.3","severity":"blocking"},{"category":"unhandled-edge","check_key":"confirmation-token-idempotency-ordering","description":"The plan requires a confirmation token, rejects invalid and replayed tokens, and also promises confirmed idempotent requests, yet defines no token issuer, payload binding, expiry, durable consumption, or ordering against idempotency lookup. `--yes` and the Settings dialog therefore have no deterministic server input, and a lost-response retry can conflict with replay rejection.","finding_id":"F-R6-006","fix":"Define a preview-issued opaque token bound to caller and exact artifact/request fingerprint with expiry and bounded consumed-token retention. Atomically return an existing caller-scoped idempotency hit first; otherwise validate and consume the token in the job-creation transaction, with mismatch, expiry, replay, concurrent duplicate, and lost-response tests.","location":"§§ 2.1 / 2.3 / 5.2 / 5.3 artifact-acquisition confirmation","prevention":"For every confirmation-gated mutation, specify issuance, request binding, expiry, persistence, consumption, idempotency-hit ordering, and lost-response retry tests.","principle":"A replay-protected authorization capability needs a defined lifecycle and one atomic order relative to idempotency.","root_cause":"Client confirmation UI was specified before the server-side token issuance, binding, consumption, and retry contract.","section_id":"2.3","severity":"blocking"},{"category":"unhandled-edge","check_key":"active-job-capacity-bound","description":"Queued and running jobs, their idempotency keys, and ownership records are never pruned, while the public API accepts unlimited unique download/load requests and defines no active-job ceiling, backpressure, provider deadline, heartbeat, stall state, or orphan timeout. Unique nonterminal work can grow persisted state and restart reconciliation without limit.","finding_id":"F-R6-007","fix":"Add fixed global and per-family/per-operation active-job limits with `job_capacity_exceeded` before persistence, plus operation-specific deadline/heartbeat and restart rules that adopt provider-owned work or terminalize stale records. Test over-cap admission and crash-recovered stalled work.","location":"§ 2.3 persisted job bounds and public admission","prevention":"For every durable work queue, enumerate active rows, secondary indexes, deadlines, heartbeats, restart adoption, stale terminalization, and admission backpressure.","principle":"A bounded durable queue must bound nonterminal admission and stalled work as well as terminal history.","root_cause":"The retention repair bounded completed jobs and events while explicitly exempting queued/running jobs and their indexes without an admission ceiling.","section_id":"2.3","severity":"blocking"},{"category":"unhandled-edge","check_key":"probe-evidence-retention-bound","description":"Transport evidence is persisted under a key containing mutable artifact, endpoint/config, parser/profile, wire, and runtime-version facts. Each changed component creates a fresh key, replacement is only for the exact key, and no supersession or retention bound removes old successes and failures.","finding_id":"F-R6-008","fix":"Retain only the newest evidence per logical model/runtime/wire plus an explicitly bounded diagnostic history, delete superseded keys atomically with the new write, protect only in-flight keys, prune before startup eligibility projection, and test repeated key churn beyond the bound.","location":"§ 4.1 persisted transport-evidence keys","prevention":"For every persistent cache, define its logical owner key, supersession transaction, age/count bounds, in-flight exception, and startup pruning test.","principle":"A durable cache keyed by mutable fingerprints needs an explicit supersession or eviction rule.","root_cause":"Exact-key invalidation was specified without lifecycle ownership for evidence made obsolete by profile, endpoint, artifact, parser, wire, or runtime-version churn.","section_id":"4.1","severity":"blocking"},{"category":"unhandled-edge","check_key":"run-fence-registry-lifecycle","description":"The new registry retains per-run mutex, terminal intent, admission, and task state, but specifies an empty registry only for the delivery-originated missing-composer path. Keeping entries leaks every historical run; deleting them naively can let a late actor create a second mutex while another actor still holds the first.","finding_id":"F-R6-009","fix":"Define a reference-counted fence-entry lifecycle: settle delivery and launch actors, run terminal CAS and lease/process cleanup, remove only after the last actor releases, and make late callers re-read durable terminal state before any side effect. Add high-volume tests for every terminal path and launch failure.","location":"§ 4.4 per-run lifecycle-fence registry","prevention":"For every per-run registry, test high-volume success, failure, cancellation, timeout, launch failure, late callers, and reclamation without two live entries for one id.","principle":"A process-global registry keyed by monotonically created identities needs safe reclamation that cannot split synchronization ownership.","root_cause":"The fence repair defined terminalization ordering while omitting normal and exceptional entry teardown.","section_id":"4.4","severity":"blocking"},{"category":"over-engineering","check_key":"single-value-config-knob","description":"Idle eviction introduces a configurable TTL with no authority, field, operator surface, default, bounds, units, or second value required anywhere in the plan.","finding_id":"F-R6-010","fix":"Use a named module-level `IDLE_TTL_SECONDS` constant for 0.5.0. If operator configurability is a real requirement, add the concrete consumer and fully specify `idle_ttl_seconds` ownership, bounds, serialization, projection, and restart semantics.","location":"§ 2.3 configurable idle TTL","prevention":"Apply the proportionality check to every new flag, timeout, and profile field; name its consumer and second required value or use a constant.","principle":"A configuration surface earns its place through a concrete consumer and a stated need for multiple values.","root_cause":"The plan labels idle TTL configurable without naming an operator surface or requirement that consumes configurability.","section_id":"2.3","severity":"nit"}],"reviewer_session":"b3a6d6b6-5e2b-4c94-8ba1-9188e09dc38c","round":6,"round_number":6,"verdict":"needs_review"},"session_id":"4b85e4ac-ec4c-4fea-b191-8a2d71162afb"}
```

**Round 7** `kind: verification`

- reviewer_run: fe629502-3aa9-4236-865f-c42db71cc54c
- reviewer_session: 3e2d56fc-3d52-4038-bba0-87d6e499ab19
- verdict: needs_review
- findings:
- F-R7-001 / blocking / 1.2 `compatibility_identity` omits vector-affecting embedding preprocessing and prefix facts that 3.2 uses to retain collections (fixer-induced, F-R2-008)
- F-R7-002 / blocking / 2.3 constructs `LocalRuntimeService` in `init_servers` while assigning the runner carrier from the earlier `init_orchestration` phase
- F-R7-003 / blocking / 2.3 `acquire_role(role, owner)` cannot carry the explicit model and runtime override that 4.3 and 4.5 pass (fixer-induced, F-R4-007)
- F-R7-004 / blocking / 2.3 `MAX_ACTIVE_JOBS` and restart reconciliation use undefined `queued/running` shorthand and only download/owned-process evidence (fixer-induced, F-R6-007)
- F-R7-005 / blocking / 3.1 omits the `/api/embeddings` route consumer `generate_embedding_batch`, which still constructs `EmbeddingService` from config
- F-R7-006 / blocking / 4.1 per-model/wire supersession lets a stale concurrent probe overwrite newer evidence (fixer-induced, F-R6-008)
- F-R7-007 / blocking / 4.3 lease reconciliation releases only terminal-or-absent runs, so a crash after acquisition and before process start strands the lease
- F-R7-008 / blocking / 4.4 `terminalize_from_thread` has no CAS-exception, timeout, or stopped-loop path that releases the loop-held mutex (fixer-induced, F-R6-004)
- F-R7-009 / blocking / 5.2/5.3 `confirmation_stale` "asks again" is undefined for non-interactive `--yes` and unproven in Settings (fixer-induced, F-R6-006)
- F-R7-010 / blocking / 5.2/5.3 `profile_hash_stale` "asks again" is undefined for non-interactive `--yes` and unproven in Settings (fixer-induced, F-R2-010)
- resolution_notes: Unattended round; the coordinator judged every finding and verified each against the repository. All ten accepted, five with narrowed repairs. F-R7-001 accepted narrowed: 3.2 commits `query_prefix` in the structural switch write while retention keys on `compatibility_identity`, so that identity gains the catalog entry's vector-affecting preprocessing facts (document prefix, normalization, pooling) under the same verified rule (any absent fact leaves it unverified and forces a full switch), and 3.2 states that a `query_prefix`-only change retains collections and is promoted atomically in the same structural write because the prefix affects only the query path; 3.2.10 proves both deltas at equal artifact and dimensions. A separate embedding-specific identity type is declined because the existing tuple carries the facts. F-R7-002 accepted: repository inspection confirms both `GobbyRunner._initialize_post_database_services` and `_initialize_runtime_services` call `init_orchestration` before `init_servers`, so `runner_init/servers.py` now constructs the service, assigns `GobbyRunner.local_runtime_service`, and places the same instance in `AppContext`; `runner_init/orchestration.py` leaves the 2.3 Targets and keeps only the 4.3 lazy getter `lambda: runner.local_runtime_service`, which resolves after server initialization; 2.3.9 asserts one instance under the real initialization order. F-R7-003 accepted narrowed: `leases.py` defines `RoleAcquisitionRequest` (role, optional explicit model, optional runtime override) beside the `CodingActivationEvaluator` protocol and `LocalRuntimeService.acquire_role(request, owner)` consumes it; 4.3's `LocalRoleSelection` is an alias of that type re-exported through `local_profiles/contracts.py` for profile-side consumers, so no dependency inverts; 2.3.11 gains the explicit-model and explicit-runtime activation cases. A new shared contracts module is declined. F-R7-004 accepted narrowed: `jobs.py` declares `ACTIVE_JOB_STATES = {queued, checking, downloading, loading, unloading}` and counts every one of them against `MAX_ACTIVE_JOBS`, and restart reconciliation resolves each nonterminal job from its own operation's evidence — a queued or checking job dispatched nothing and fails `orphaned_after_restart`; a downloading job adopts a matching provider download or fails; a loading job succeeds when the provider reports the target loaded instance or an adopted owned process with matching identity and otherwise fails; an unloading job succeeds on confirmed absence and otherwise fails — with 2.3.12 extended to crash injection in every nonterminal state. A family-by-family restart table, per-operation deadlines, and heartbeats are declined as unneeded mechanism. F-R7-005 accepted as its typed repair: `generate_embedding_batch` calls `EmbeddingService.from_config(config.embeddings)` directly today and `tests/servers/routes/test_embeddings_routes.py` exists, so both join the 3.1 Targets and the route resolves the `AppContext` binding and holds the shared generation guard and local embedding lease across the complete batch. F-R7-006 accepted: each model/wire slot carries an observation generation, a probe records the generation it observed at start, and its write persists only while that generation is current — an obsolete completion returns to its waiters without replacing newer stored evidence — with the old-probe-finishes-last ordering added to 4.1.11. F-R7-007 accepted: reconciliation classifies from durable launch evidence — terminal or absent releases; a nonterminal run holding a run lease with no recorded pid, tmux session, or start checkpoint is terminalized as a typed `orphaned_before_launch` and released; a nonterminal run whose recorded process identity is live retains its lease; unverifiable recorded identity fails closed by retaining the lease with a typed diagnostic — and 4.3.6 asserts the present-pending/no-pid crash explicitly. F-R7-008 accepted narrowed: one loop-side coroutine owns the entry for the whole bridge, acquiring the mutex, signalling the calling thread to run its synchronous CAS, awaiting the thread-safe result, and releasing and reclaiming the entry in `finally` on success, CAS exception, cancellation, or a bounded wait bounded by a module-level constant in `run_lifecycle_fence.py`; when the loop is unavailable or shutting down the writer performs no unfenced CAS and returns the typed `fence_unavailable`, leaving the run for the 4.3 restart reconciliation that already owns nonterminal runs; 4.4.12 gains the CAS-exception, stopped-loop, timeout, and later-retry cases. A new durable deferred-intent record is declined because restart reconciliation already resolves a nonterminal run. F-R7-009 and F-R7-010 accepted narrowed together: `confirmation_stale` and `profile_hash_stale` invalidate the previous preview and its authorization, interactive CLI and Settings display the refreshed identity or normalized diff with its requirements and require a fresh confirmation, and a non-interactive `--yes` invocation prints the refreshed preview and exits nonzero without resubmitting, so only a new invocation authorizes it; 5.2.2 and 5.2.4 gain the `--yes` nonzero-exit clauses and 5.3.8 proves no job and no pending profile are written before refreshed confirmation, including an ordinary edit that becomes migration-requiring. No new deliverables; repairs land on 1.2, 2.3, 3.1, 3.2, 4.1, 4.3, 4.4, 5.2, and 5.3 after this checkpoint.

```json plan-review-round
{"evidence_id":"768ea6d5-0756-4a62-aaf4-e176701bb4b8","plan_hash":"eabb2a360fd45680b220e3fba40e693514bb6667e61c69ba8d511ebe96b86118","round_number":7,"round_result":{"coverage_attestation":{"adjacent_variant_complete":true,"attestation_digest":"e1d18cab0980a4bbc97694c1264bb003276d795ab237554670443ba290cec159","cross_lane_interaction_complete":true,"disposition_counts":{"dismissed":1,"emitted_findings":10,"total":11},"evidence_id":"768ea6d5-0756-4a62-aaf4-e176701bb4b8","lanes":[{"candidate_count":2,"lane_id":"requirements_traceability","status":"completed"},{"candidate_count":2,"lane_id":"repository_blast_radius","status":"completed"},{"candidate_count":7,"lane_id":"runtime_invariants","status":"completed"}],"shadow_manifest_status":{"entry_count":18,"manifest_digest":"c7f46c2f0fea14d96c90468be35e6f2f819311e9eee5c059f94ae35b520637ce","status":"valid"},"source_digest":"6f35188d50770fc7ce17c3042e630cdcd0d63aeabfb18b1458184c4047131222","version":1},"findings":[{"category":"unhandled-edge","causal_finding_id":"F-R2-008","causal_section_ids":["1.2"],"check_key":"embedding-compatibility-identity","description":"`compatibility_identity` contains family, backend, artifact digest/revision, quantization, and dimensions, yet § 3.2 uses it to retain embedding collections while preprocessing and prefix semantics are omitted. Equal artifact/dimensions can therefore retain collections across a vector-affecting preprocessing change or silently change the query contract.","finding_id":"F-R7-001","fix":"Define an embedding-specific verified identity covering every vector-affecting preprocessing/tokenization and document-prefix fact. Specify query-prefix semantics explicitly: either include it in the switch identity or prove that a query-prefix-only change retains collections while atomically promoting the new query contract. Make changed or unknown vector-affecting facts force a full switch and add same-artifact/same-dimension delta tests.","introduced_in_round":2,"location":"§ 1.2 compatibility_identity and § 3.2 collection-retention decision","prevention":"Diff every normalized embedding fact against the reuse key and add one-field-delta tests for preprocessing, document prefix, query prefix, dimensions, backend, and artifact revision.","principle":"A cross-observation reuse key must include every fact that can change the produced vectors or their query contract.","root_cause":"The artifact-oriented compatibility tuple was reused for embedding retention even though the normalized record and switch journal carry additional preprocessing and prefix semantics.","section_id":"1.2","severity":"blocking"},{"category":"bad-sequencing","check_key":"initialization-owner-order","description":"Both runner initialization paths call `init_orchestration` before `init_servers`. The plan nevertheless constructs `LocalRuntimeService` in `runner_init/servers.py` and says `runner_init/orchestration.py` wires that constructed instance into the runner carrier, so the declared owner cannot access the instance. Moving all of `init_servers` earlier would also violate its existing orchestration dependencies.","finding_id":"F-R7-002","fix":"Give one feasible phase both responsibilities. The least disruptive contract is for `init_servers` to construct the evaluator-backed service, assign `runner.local_runtime_service`, and put the same instance in `AppContext`; earlier orchestration should install only lazy getters that resolve the carrier after server initialization. Add a real-order initialization test proving one instance reaches runner, AppContext, HTTP, WebSocket, and lifecycle consumers.","location":"§ 2.3 LocalRuntimeService construction and carrier wiring","prevention":"Trace both real runner initialization call orders for every new daemon service and name one phase that owns construction plus carrier assignment.","principle":"A service instance must be constructed before any initialization phase assigns or distributes that instance.","root_cause":"Construction was assigned to `init_servers`, while assignment to `GobbyRunner.local_runtime_service` was assigned to the earlier `init_orchestration` phase.","section_id":"2.3","severity":"blocking"},{"category":"bad-sequencing","causal_finding_id":"F-R4-007","causal_section_ids":["4.3"],"check_key":"boundary-contract-parity","description":"Section 2.3 exposes `acquire_role(role, owner)`, while § 4.3 calls `acquire_role(selection, owner=run_id)` with a `LocalRoleSelection` containing an explicit model and runtime override; § 4.5 needs the same explicit-model path. The service cannot type or honor those fields, so `local:coding/<model>` can collapse to the configured role model or fail at the interface boundary.","finding_id":"F-R7-003","fix":"Define a dependency-safe `RoleAcquisitionRequest` in the 2.3-owned local-runtime contract with role, optional explicit model, and runtime override; make `LocalRuntimeService.acquire_role` consume it. Make `LocalRoleSelection` reuse or alias that contract and add configured-model, explicit-model, and explicit-runtime cases through service, spawn, resume, and web chat.","introduced_in_round":4,"location":"§ 2.3 acquire_role contract versus §§ 4.3 and 4.5 consumers","prevention":"Trace every typed carrier from selector parsing through the service boundary, spawn/resume/web-chat consumers, and explicit-override acceptance before finalizing dependencies.","principle":"An upstream service contract must represent every selector field its downstream consumers are required to preserve.","root_cause":"The cold-activation repair introduced `LocalRoleSelection` downstream without revising the earlier `acquire_role(role, owner)` interface that performs selection and activation.","section_id":"2.3","severity":"blocking"},{"category":"unhandled-edge","causal_finding_id":"F-R6-007","causal_section_ids":["2.3"],"check_key":"active-job-recovery-matrix","description":"Jobs declare `queued`, `checking`, `downloading`, `loading`, and `unloading`, but the new cap and recovery contract refer only to `queued/running`. Restart reconciliation looks only for a matching download or an adopted owned process, leaving pre-dispatch work and LM Studio/Ollama loaded-instance load/unload outcomes without deterministic capacity or crash semantics.","finding_id":"F-R7-004","fix":"Declare `ACTIVE_JOB_STATES` exactly and count every nonterminal state against `MAX_ACTIVE_JOBS`. Add a family-by-operation restart table: queued/checking work is requeued or failed-before-send; downloads adopt provider work or fail orphaned; LM Studio/Ollama loads inspect loaded-instance identity; unloads inspect confirmed absence; vLLM operations reconcile ownership records and processes. Add crash injection for every nonterminal state and supported operation.","introduced_in_round":6,"location":"§ 2.3 MAX_ACTIVE_JOBS and restart reconciliation","prevention":"Build a state-by-operation-by-provider restart table whenever durable work admission or orphan recovery changes.","principle":"Every durable nonterminal state for every supported operation needs one capacity classification and one restart outcome.","root_cause":"The admission repair used undefined `queued/running` shorthand and recovery probes limited to downloads and Gobby-owned processes, while the declared job state machine also covers checking, provider-owned loads, and unloads.","section_id":"2.3","severity":"blocking"},{"category":"traceability","check_key":"embedding-route-consumer-closure","description":"Section 3.1 requires the daemon `/api/embeddings` route to hold the shared generation binding during its embed call, yet `generate_embedding_batch` and its route tests are absent from Targets. The live route constructs `EmbeddingService` from `config.embeddings`, bypassing the gate and local-role lease and failing the planned local source whose persisted `api_base` and `api_key` are null.","finding_id":"F-R7-005","fix":"Target `generate_embedding_batch` and the focused route tests. Route the handler through the `AppContext` embedding binding, hold the shared generation guard and local embedding lease for the complete batch, release on success, error, timeout, and cancellation, and prove the existing cloud path remains unchanged.","location":"§ 3.1 `/api/embeddings` generation-binding consumer","prevention":"Run caller and route inventory for every new binding/gate, including HTTP handlers and their focused tests.","principle":"Every live consumer named by a cross-cutting routing or synchronization contract must appear in Targets and focused acceptance.","repairs":[{"entries":["`src/gobby/servers/routes/embeddings.py::generate_embedding_batch`","`tests/servers/routes/test_embeddings_routes.py::*` — scope-reason: cover the HTTP embeddings route holding the shared generation binding and local lease across the complete batch"],"kind":"add_targets","section_id":"3.1"},{"items":[{"artifact":"test: `tests/servers/routes/test_embeddings_routes.py::test_embeddings_route_holds_generation_binding_and_local_lease`","prose":"The daemon embeddings route resolves the shared binding from AppContext, holds its shared generation guard and local embedding lease for the full batch, releases both on success, error, timeout, and cancellation, and leaves cloud routing unchanged."}],"kind":"add_acceptance","section_id":"3.1"}],"root_cause":"The consumer sweep covered daemon-internal search owners but omitted the HTTP embeddings route that still constructs `EmbeddingService` directly from config.","section_id":"3.1","severity":"blocking"},{"category":"unhandled-edge","causal_finding_id":"F-R6-008","causal_section_ids":["4.1"],"check_key":"concurrent-supersession-ordering","description":"Two distinct evidence keys for one model and wire may probe concurrently. If an obsolete endpoint/profile probe finishes after the current-key probe, its atomic delete-and-insert removes the newer record and installs the obsolete key; exact lookup remains fail-closed, but current evidence is lost and another probe is sent. Acceptance 4.1.11 covers concurrent completion without ordering this race.","finding_id":"F-R7-006","fix":"Associate each model/wire slot with a monotonic observation or configuration generation, or CAS the slot from the key observed at probe start. Persist only if that generation is still current; return an obsolete completion to its existing waiters without replacing newer stored evidence. Add the old-probe-finishes-last ordering to 4.1.11.","introduced_in_round":6,"location":"§ 4.1 per-model/wire probe supersession and acceptance 4.1.11","prevention":"For every bounded cache with concurrent producers, test old-start/new-start with both completion orders and require stale completion to preserve the newer generation.","principle":"Supersession of concurrent observations must follow the current configuration generation, not completion order.","root_cause":"The retention repair atomically deletes other keys when any probe completes but leaves different evidence keys concurrently in flight for the same model/wire slot.","section_id":"4.1","severity":"blocking"},{"category":"unhandled-edge","check_key":"prelaunch-restart-classification","description":"A daemon crash after lease acquisition and before process start leaves an existing nonterminal pending run with no pid/tmux/start checkpoint. The plan says reconciliation releases only leases whose run is terminal or absent, yet claims that predicate covers this crash, so the lease can remain indefinitely with no executor that can finish launch.","finding_id":"F-R7-007","fix":"Define restart classification from durable launch evidence: terminal or absent releases; pending with a run lease and no pid/tmux/start checkpoint is terminalized as a typed prelaunch abort and releases; running with matching process identity is retained; ambiguous evidence fails closed with a typed conflict. Make 4.3.6 assert the present-pending/no-pid crash explicitly.","location":"§ 4.3 run-owned lease reconciliation and acceptance 4.3.6","prevention":"Inject restart at every boundary between run creation, lease acquisition, process start, process checkpoint, and terminalization; enumerate the durable predicate for each.","principle":"Crash recovery must classify durable launch evidence, not infer ownership safety from terminal-or-absent status alone.","root_cause":"Run-id ownership made every lease attributable but the reconciliation predicate was never extended to a present nonterminal run whose executor died before process checkpoint.","section_id":"4.3","severity":"blocking"},{"category":"unhandled-edge","causal_finding_id":"F-R6-004","causal_section_ids":["4.4"],"check_key":"thread-bridge-failure-closure","description":"`terminalize_from_thread` blocks until the daemon loop holds the run mutex, runs the synchronous CAS on the worker thread, then schedules release. No CAS-exception, timeout, stopped-loop, or daemon-shutdown path guarantees loop-owned mutex release and registry reclamation, so a worker or fence entry can strand and block later terminalization and lease cleanup.","finding_id":"F-R7-008","fix":"Keep lock ownership in one loop coroutine that acquires the entry, awaits a thread-safe CAS-result signal, and releases/reclaims in `finally` on success, exception, cancellation, or timeout. Bound `run_coroutine_threadsafe` waits; when the loop is unavailable, durably defer terminal intent for restart reconciliation without an unfenced CAS. Extend 4.4.12 with CAS-exception, stopped-loop, timeout, and subsequent-retry cases.","introduced_in_round":6,"location":"§ 4.4 terminalize_from_thread bridge and acceptance 4.4.12","prevention":"For every blocking thread-to-loop bridge, test CAS exception, submission failure, loop shutdown, wait timeout, cancellation, registry reclamation, and a later retry.","principle":"A sync-to-async lock bridge needs bounded, exception-safe release and a recovery outcome when its owning loop is unavailable.","root_cause":"The new bridge specifies only the successful acquire/CAS/release sequence for off-loop terminal writers.","section_id":"4.4","severity":"blocking"},{"category":"unhandled-edge","causal_finding_id":"F-R6-006","causal_section_ids":["2.1","2.3","5.2","5.3"],"check_key":"edge-case-coverage","description":"On `confirmation_stale`, § 5.2 re-previews and “asks again,” which is impossible for a non-interactive `--yes` invocation; the plan leaves exit versus silent authorization undefined. Section 5.3 re-previews in place without acceptance proving the old dialog confirmation is invalidated and no refreshed artifact can create a job or reach the provider until separately confirmed.","finding_id":"F-R7-009","fix":"State that `confirmation_stale` invalidates the prior authorization. Interactive CLI and Settings must display the refreshed identity and require a new confirmation; non-interactive `--yes` must print the refreshed preview and exit nonzero so a new invocation explicitly authorizes it. Add CLI and Settings tests injecting digest/revision drift and proving no job or provider send occurs before refreshed confirmation.","introduced_in_round":6,"location":"§§ 5.2–5.3 artifact `confirmation_stale` client branches","prevention":"For each preview-confirm mutation, test stale response behavior in interactive CLI, non-interactive CLI, and UI before the server mutation can run.","principle":"A stale preview invalidates its authorization; every client mode needs a deterministic path to a separately confirmed refreshed preview.","root_cause":"The round-6 download repair added server-side stale binding but described every client as asking again, including non-interactive `--yes`.","section_id":"5.2","severity":"blocking"},{"category":"unhandled-edge","causal_finding_id":"F-R2-010","causal_section_ids":["2.3","5.2","5.3"],"check_key":"edge-case-coverage","description":"On `profile_hash_stale`, § 5.2 re-reads the profile and “asks again,” which is undefined for a non-interactive migration-requiring operation invoked with `--yes`; automatic resubmission could authorize a materially different diff. Section 5.3 reloads the surface, but acceptance does not prove a refreshed diff—especially one newly requiring migration—cannot stage until the user reconfirms it.","finding_id":"F-R7-010","fix":"State that `profile_hash_stale` invalidates the previous preview and authorization. Interactive CLI and Settings must display the refreshed normalized diff and requirements and obtain fresh confirmation when required; non-interactive `--yes` must print the refreshed preview and exit nonzero for an explicit rerun. Add concurrent-drift tests, including an ordinary edit becoming migration-requiring, and assert no pending profile is written before refreshed authorization.","introduced_in_round":2,"location":"§§ 5.2–5.3 `profile_hash_stale` client branches","prevention":"Exercise profile preview/confirm with concurrent drift that changes the diff and newly introduces migration in interactive CLI, non-interactive CLI, and Settings.","principle":"Authorization for a staged profile is bound to the exact normalized diff and migration requirements the caller reviewed.","root_cause":"The original preview-confirm repair specified re-reading and asking again without defining the non-interactive stale path or proving refreshed migration requirements require new approval.","section_id":"5.2","severity":"blocking"}],"reviewer_session":"3e2d56fc-3d52-4038-bba0-87d6e499ab19","round":7,"round_number":7,"verdict":"needs_review"},"session_id":"4b85e4ac-ec4c-4fea-b191-8a2d71162afb"}
```

**Round 8** `kind: verification`

- reviewer_run: 2e8a5185-d46a-4463-a62e-dbbed467e2d7
- reviewer_session: d38f91ab-d5d2-46af-bff4-220f1807a7bf
- verdict: needs_review
- findings:
- F-R8-001 / blocking / 4.4 `AgentCleanupHandler.cleanup_agent` and the missing-tmux inline branch write terminal state outside the fence
- F-R8-002 / blocking / 2.3 a terminal job's retained single-flight key returns the old result to a resubmission, and the listed `retry` operation has no definition or consumer
- resolution_notes: Unattended round; the coordinator judged both findings and verified each against the repository. Both accepted, one narrowed. F-R8-001 accepted: repository inspection confirms `AgentCleanupHandler.cleanup_agent` runs its own `complete`/`timeout`/`fail` CAS and is reached by fourteen production call sites in `agent_health.py`, `lifecycle_monitor.py`, `lifecycle_reconciliation.py`, `memory_watchdog.py`, and `watchdog/recovery.py`, so the round-6 claim that those callers "already terminalize through `cleanup_agent` and the fenced handler methods" left the actual writer unfenced; `cleanup_agent` joins the 4.4 Targets in the same exact-symbol form as its four sibling methods and runs its three CAS branches through `terminalize(run_id, cas)` from the loop side, and the sweep sentence now names it as fenced rather than treating it as a fenced boundary. Repository inspection also confirms `SessionCoordinator._terminate_agent_run_inline` completes or fails directly when `tmux_session_name` is absent, before `capture_then_kill_sync` is ever reached, so that branch is explicitly routed through `terminalize_from_thread` alongside the `capture_then_kill_sync` CAS; the symbol is already a Target, so no inventory change is needed. Acceptances 4.4.7 and 4.4.12 gain the two branches raced against paused delivery and against the activation-to-launch pause. F-R8-002 accepted narrowed: the plan retains a terminal job's idempotency key for 7 days or 200 jobs and returns the existing job on any single-flight hit, so a resubmission after `failed` or `cancelled` is answered with the old terminal result, and the `retry` entry in the 2.3 service surface has no attempt identity and no HTTP, CLI, or Settings consumer (5.2's command list has none). Single-flight coalescing is narrowed to jobs in `ACTIVE_JOB_STATES`, at most one of which may exist per key, so a submission after any terminal state creates a new attempt; the `retry` entry is removed because resubmission is the retry. A separate caller request id is declined: the existing `checking` state already makes a redundant resubmission cheap and side-effect-free — a download whose artifact is installed at the confirmed digest, a load whose target instance is already loaded under the same compatibility identity, and an unload whose instance or process is already absent each pass `checking` straight to `succeeded` with no provider mutation — which preserves the lost-response guarantee without a second durable identity. Download resubmission is an ordinary new request that repeats the `confirm=False` preview and `confirm=True` confirmation under the existing `confirmation_stale` rule, and the CLI `download` command and the Settings download dialog reach the same behavior through the job list. 2.3.2 and 2.3.13 are updated for the narrowed coalescing rule, new acceptance 2.3.14 proves resubmission across download, load, and unload after `failed`, `cancelled`, and `succeeded`, and 5.2.2 and 5.3.1 gain their consumer clauses. No new deliverables; repairs land on 2.3, 4.4, 5.2, and 5.3 after this checkpoint.

```json plan-review-round
{"evidence_id":"44dbd123-756e-4aa9-8d09-192bf36a67cc","plan_hash":"a25162e45180792bd8cc2a9ee90e46912eee3fbd06a820db8b97b50ea3a37e2f","round_number":8,"round_result":{"coverage_attestation":{"adjacent_variant_complete":true,"attestation_digest":"112cdd84fd391c7294680b9757320b32b7b8f7ce3fe3f9c45ff5e4bd769eb7d0","cross_lane_interaction_complete":true,"disposition_counts":{"dismissed":2,"emitted_findings":2,"total":4},"evidence_id":"44dbd123-756e-4aa9-8d09-192bf36a67cc","lanes":[{"candidate_count":1,"lane_id":"requirements_traceability","status":"completed"},{"candidate_count":2,"lane_id":"repository_blast_radius","status":"completed"},{"candidate_count":1,"lane_id":"runtime_invariants","status":"completed"}],"shadow_manifest_status":{"entry_count":18,"manifest_digest":"eaf020a6beb3cd909b97a6c83306079425e4359d6891389753a62b17319c5c2d","status":"valid"},"source_digest":"04f6a28d1d4766c0443794c87ceab31355ebe6f7a21598ca564a196bd09936ab","version":1},"findings":[{"category":"unhandled-edge","check_key":"terminal-fence-writer-closure","description":"`AgentCleanupHandler.cleanup_agent` directly calls complete, timeout, or fail, yet § 4.4 targets only four sibling terminalize methods and incorrectly says those methods own lifecycle cleanup. `SessionCoordinator._terminate_agent_run_inline` also directly completes or fails when `tmux_session_name` is absent before it reaches the planned `capture_then_kill_sync` callback. Both branches can bypass the shared fence and its lease-release ordering.","finding_id":"F-R8-001","fix":"Add `AgentCleanupHandler.cleanup_agent` to § 4.4 Targets and route its success, timeout, and failure CAS branches through `terminalize`. Explicitly route `_terminate_agent_run_inline`'s missing-tmux branch through `terminalize_from_thread`. Extend the focused cleanup and hook tests to race both variants against prompt delivery and activation-to-launch, asserting one terminal CAS and exactly-once lease release.","location":"§ 4.4 AgentCleanupHandler and hook-thread terminal writers","prevention":"Literal-sweep every complete, fail, timeout, cancel, and kill storage call, then exercise each branch while delivery or activation-to-launch is paused.","principle":"Every reachable terminal-state CAS branch must enter the same lifecycle fence before it mutates durable status.","root_cause":"The writer inventory stopped at four terminalize wrapper methods and the tmux capture callback instead of walking every complete, fail, and timeout call inside their owning methods.","section_id":"4.4","severity":"blocking"},{"category":"unhandled-edge","check_key":"terminal-job-resubmission-identity","description":"A failed or cancelled download, load, or unload keeps its idempotency record until the 7-day/200-job pruning window, and creation returns the existing job on a single-flight hit. The plan therefore gives a normal resubmission the old terminal result. Its listed `retry` method has no attempt-key semantics or HTTP, CLI, or Settings consumer; for downloads it also has no rule requiring a fresh preview and confirmation.","finding_id":"F-R8-002","fix":"Use the simpler contract: coalesce only active work by resource identity, use a caller request id for lost-response replay, and let a submission after `failed` or `cancelled` create a new attempt. Remove the standalone unused `retry` method. Require download resubmission to repeat preview and confirmation, expose the same new-attempt behavior through HTTP, CLI, and Settings, and add focused acceptance for all three operations.","location":"§§ 2.3 / 5.2 / 5.3 durable job retry and resubmission","prevention":"For download, load, and unload, test lost-response replay, failed resubmission, cancelled resubmission, artifact drift, and fresh confirmation through HTTP, CLI, and Settings.","principle":"Resource single-flight identity and caller request idempotency are separate contracts; a terminal attempt must not block a newly authorized attempt.","root_cause":"The plan retains each terminal job's single-flight key and checks that key before creating work, while naming a standalone `retry` operation without defining a new attempt identity, authorization boundary, or consumer.","section_id":"2.3","severity":"blocking"}],"reviewer_session":"d38f91ab-d5d2-46af-bff4-220f1807a7bf","round":8,"round_number":8,"verdict":"needs_review"},"session_id":"4b85e4ac-ec4c-4fea-b191-8a2d71162afb"}
```

**Round 9** `kind: verification`

- reviewer_run: c8496729-6151-406a-8eb4-8c79216db701
- reviewer_session: 9f5842ca-cb57-4d88-96c2-898bd2b5ec38
- verdict: needs_review
- findings:
- F-R9-001 / blocking / 5.2 CLI and Settings acceptance prove only failed-download resubmission while 2.3.14 defines it for download, load, and unload after failed, cancelled, and succeeded (fixer-induced, F-R8-002)
- F-R9-002 / blocking / 3.1 the skills registry still builds `SkillSearch` from persisted `api_base`/`api_key` and indexes outside the shared embedding binding
- F-R9-003 / blocking / 2.3 a cancelled job leaves `ACTIVE_JOB_STATES` before its provider mutation is quiescent, so an immediate resubmission can overlap it (fixer-induced, F-R8-002)
- F-R9-004 / blocking / 2.3 `acquire_role` single-flights on display identity although requests carry runtime and profile overrides that change the shared activation
- F-R9-005 / blocking / 4.4 `spawn_executor._prepare_provider_sandbox` and `resume_executor._park_unlaunched_successor` write terminal state directly inside the mutex-held launch interval
- F-R9-006 / blocking / 4.5 conversation lease release resolves through the successor-only registry identity, so a retained predecessor lease is undiscoverable (fixer-induced, F-R5-011)
- resolution_notes: Unattended round; the coordinator judged every finding and verified each against the repository. All six accepted, four with narrowed repairs. F-R9-001 accepted as its typed repair: 2.3.14 proves resubmission for all three operations after every terminal state while 5.2.2 and 5.3.1 assert only failed download, so 5.2 and 5.3 each gain one focused acceptance covering failed and cancelled download, load, and unload resubmission, fresh download preview and confirmation, and an active attempt returned without a duplicate provider send; both artifacts are already Targets. F-R9-002 accepted as its typed repair: repository inspection confirms `mcp_proxy/registries.py::build_skill_search` constructs `SkillSearch` from `EmbeddingsConfig.model/api_base/api_key/dim`, that `SkillSearch.index_skills_async` reaches `UnifiedSearcher.fit_async`, and that both `UnifiedSearcher.fit_async` and `EmbeddingBackend.__init__` construct an `EmbeddingService` from those fields, which are null under a local source, so skill indexing silently degrades to keyword and runs outside the 3.1 generation guard and embeddings-role lease; `SkillSearch` is the only production constructor of `UnifiedSearcher`, so the eight production symbols plus `tests/skills/test_skills_search.py` join the 3.1 Targets in the same exact-symbol form already used for `EmbeddingBackend.search_async`, prose records the routing, and 3.1.10 proves guarded local indexing and blocked switch publication during fit. F-R9-003 accepted narrowed: the round-8 active-only uniqueness rule makes `cancelled` release the single-flight key immediately even though provider cancel is invoked only when advertised, so a resubmission can start a second mutation over a still-running download, load, or process start. Cancellation becomes intent followed by confirmed quiescence rather than a new job state: `cancel` stops Gobby's waiters and subscribers immediately, invokes the advertised provider cancel, and leaves the job in its current active state holding its key until quiescence is confirmed, and only then does it reach `cancelled`. The unknown-after-send branch is closed with machinery the plan already defines: `checking` adopts a matching in-flight provider operation under exactly the adoption rules restart reconciliation states, so a new attempt joins existing provider work instead of issuing a second call. A separate cancelling/reconciling state, per-operation deadlines, and provider heartbeats are declined. 2.3.2 and 2.3.14 carry the two clauses. F-R9-004 accepted narrowed: 2.3 keys load jobs on artifact identity plus the 2.2 supervisor compatibility identity precisely because incompatible launch profiles must not share a process, yet `acquire_role` single-flights on display identity while `RoleAcquisitionRequest` carries explicit model and runtime overrides and the evaluator consumes runtime, wire, and profile facts. The activation key becomes that same load-job compatibility identity together with the role, so every joiner shares the facts the evaluator reads and one invocation remains correct, and each successful waiter converts the shared activation into its own lease under its own owner. Splitting shareable loading from per-request evaluation into separate phases is declined because the widened key already makes joiners identical. 2.3.11 gains the incompatible-concurrent-request case. F-R9-005 accepted: repository inspection confirms `_prepare_provider_sandbox` calls `request.run_manager.fail` on sandbox startup failure and `_park_unlaunched_successor` calls `runner.run_storage.cancel` on spawn failure, both inside the interval 4.4 places under the run mutex, where wrapping either in `terminalize` would self-deadlock. Both helpers now return typed failures without terminal writes, and the guarded block's single owner releases the mutex and the run-owned lease and then runs one fenced terminal CAS, so sandbox failure, spawn exception, and unsuccessful spawn result share the launch path's existing terminal owner; the two `::*` scope-reasons are widened to state it and 4.4.11 races both failures against external terminalization. F-R9-006 accepted narrowed: 4.5 keys each conversation lease by conversation id but resolves release through the session-id registry, which records only the successor cache identity, so a predecessor retained by a failed release is unreachable until restart. Conversation release becomes owner-wide over the 2.3 lease inventory — every lease whose owner is that conversation id, which is normally one and is two only after a failed predecessor release — and clear, disconnect, shutdown, and restart reconciliation each drive that owner to zero leases, after which the next message reacquires exactly one and the freed predecessor cache entry becomes evictable. 4.5.6's "converges to exactly one lease" is corrected accordingly. A durable cleanup record or retry loop is declined because the inventory already exposes the retained lease and release is idempotent. No new deliverables; repairs land on 2.3, 3.1, 4.4, 4.5, 5.2, and 5.3 after this checkpoint.

```json plan-review-round
{"evidence_id":"54bc3a0b-7b45-487b-bc24-d0cdd3a750b7","plan_hash":"9655ba99321ca49eb3660d15a3ed188073c1f1cd67a78dce61d083f4e3a14746","round_number":9,"round_result":{"coverage_attestation":{"adjacent_variant_complete":true,"attestation_digest":"7cf3206b5d80777e87c8c30ac2a4ddf345d57e274b80b0be95064e6c146f275d","cross_lane_interaction_complete":true,"disposition_counts":{"dismissed":0,"emitted_findings":6,"total":6},"evidence_id":"54bc3a0b-7b45-487b-bc24-d0cdd3a750b7","lanes":[{"candidate_count":1,"lane_id":"requirements_traceability","status":"completed"},{"candidate_count":1,"lane_id":"repository_blast_radius","status":"completed"},{"candidate_count":4,"lane_id":"runtime_invariants","status":"completed"}],"shadow_manifest_status":{"entry_count":18,"manifest_digest":"76962e5f9e4328abd8f3e4a703caf3bb87d566a2e5990c1eaf255e17f33ef544","status":"valid"},"source_digest":"2d7b4d85348d34922c1c584db5c3231f0b47c0facc16c37d9db5d206b2bb8675","version":1},"findings":[{"category":"weak-testability","causal_finding_id":"F-R8-002","causal_section_ids":["2.3","5.2","5.3"],"check_key":"job-resubmission-consumer-acceptance-parity","description":"Section 2.3.14 defines new-attempt resubmission for download, load, and unload after failed, cancelled, and succeeded attempts. The CLI and Settings prose expose failed/cancelled retry for all three operations, while 5.2.2 and 5.3.1 prove only failed download; cancelled download and failed/cancelled load/unload can regress while every consumer gate passes.","finding_id":"F-R9-001","fix":"Add focused CLI and Settings acceptance for failed and cancelled download, load, and unload resubmission. Preserve fresh preview/confirmation for download, assert load/unload create a new attempt, and assert an active attempt is still coalesced.","introduced_in_round":8,"location":"§§ 5.2 / 5.3 consumer acceptance for § 2.3.14 resubmission","prevention":"Diff each service acceptance matrix against HTTP, CLI, and Settings acceptance by operation and prior terminal state.","principle":"Every public consumer named for a state transition must have acceptance coverage for every supported operation and terminal-origin branch.","repairs":[{"items":[{"artifact":"test: `tests/cli/test_local_runtime.py::test_resubmission_matrix`","prose":"CLI resubmission after failed or cancelled download, load, and unload creates a new attempt, download repeats preview and confirmation, and an active attempt is returned without a duplicate provider send."}],"kind":"add_acceptance","section_id":"5.2"},{"items":[{"artifact":"test: `web/src/components/settings/inference/__tests__/LocalRuntimeSection.test.tsx`","prose":"Settings resubmission from failed or cancelled download, load, and unload rows creates a new attempt, download reopens fresh confirmation, and an active attempt is not duplicated."}],"kind":"add_acceptance","section_id":"5.3"}],"root_cause":"The Round-8 repair added download consumer assertions while leaving failed/cancelled load and unload behavior only in prose.","section_id":"5.2","severity":"blocking"},{"category":"traceability","check_key":"embedding-binding-consumer-closure","description":"The skills registry still constructs SkillSearch from EmbeddingsConfig.model/api_base/api_key, then SkillSearch.index_skills_async reaches UnifiedSearcher.fit_async and EmbeddingBackend.fit_async. Under local source those persisted connection fields are null, causing keyword fallback or strict-mode failure, and index generation remains outside the § 3.1 generation guard and embeddings-role lease.","finding_id":"F-R9-002","fix":"Route skill-search construction and index generation through the shared embedding binding. Hold its generation guard and local embeddings lease across fit, preserve cloud behavior, and test local-source indexing/search plus switch publication while fit is paused.","location":"§ 3.1 daemon-internal embedding consumers and Targets","participating_section_ids":["3.1"],"prevention":"For every embedding backend, trace constructor, fit/index, search, refresh, and registry assembly paths under local source and inventory each exact symbol.","principle":"A shared embedding binding must cover every construction and generation path, including index generation before search.","repairs":[{"entries":["`src/gobby/mcp_proxy/registries.py::build_skill_search`","`src/gobby/skills/search.py::SkillSearch.__init__`","`src/gobby/skills/search.py::SkillSearch.index_skills_async`","`src/gobby/search/unified.py::UnifiedSearcher.__init__`","`src/gobby/search/unified.py::UnifiedSearcher._get_embedding_backend`","`src/gobby/search/unified.py::UnifiedSearcher.fit_async`","`src/gobby/search/backends/embedding.py::EmbeddingBackend.__init__`","`src/gobby/search/backends/embedding.py::EmbeddingBackend.fit_async`","`tests/skills/test_skills_search.py::*` — scope-reason: cover shared local embedding binding, guarded index generation, and keyword-fallback prevention"],"kind":"add_targets","section_id":"3.1"},{"items":[{"artifact":"test: `tests/skills/test_skills_search.py::test_local_binding_guards_index_generation`","prose":"Skill-search construction and index generation resolve the shared local embedding binding despite null persisted api_base/api_key, hold one generation guard and embeddings-role lease across fit, and block switch publication until the in-memory index is coherent."}],"kind":"add_acceptance","section_id":"3.1"}],"root_cause":"The consumer sweep stopped at semantic search and EmbeddingBackend.search_async, omitting the skills registry and fit/index path that still constructs from persisted api_base/api_key.","section_id":"3.1","severity":"blocking"},{"category":"unhandled-edge","causal_finding_id":"F-R8-002","causal_section_ids":["2.3","5.2","5.3"],"check_key":"cancelled-job-provider-quiescence","description":"Cancellation stops Gobby waiters and calls provider cancel only when advertised, yet a cancelled job leaves ACTIVE_JOB_STATES and a new submission immediately creates another attempt. An uncancellable or ambiguously-sent download, load, or unload can still be running, so the new attempt can overlap the same provider mutation; the checking short-circuit covers already-complete state only.","finding_id":"F-R9-003","fix":"Separate subscriber cancellation from provider-operation quiescence. Retain an active cancelling/reconciling owner and its single-flight key until cancellation or terminal provider evidence is confirmed, make resubmissions join or adopt it, and apply the same rule to unknown-after-send failures across all three operations.","introduced_in_round":8,"location":"§§ 2.1 / 2.3 / 5.2 / 5.3 cancellation-to-resubmission boundary","prevention":"For every mutation, race immediate resubmission against supported cancel, unsupported cancel, cancel acknowledgement, and unknown-after-send failure.","principle":"A single-flight identity may be released only when the underlying provider mutation is confirmed quiescent or durably owned for adoption.","root_cause":"The Round-8 active-only uniqueness repair treats cancelled as terminal even when cancellation is unsupported or the provider outcome after send is unknown.","section_id":"2.3","severity":"blocking"},{"category":"unhandled-edge","check_key":"role-acquisition-compatibility-singleflight","description":"acquire_role is single-flight only by display_identity, but CodingActivationEvaluator and process compatibility depend on runtime, wire, parser/profile, and launch context. Concurrent Codex/Claude or incompatible-profile requests can share one evaluator result or process, and the plan never states how every successful waiter receives its own owner lease.","finding_id":"F-R9-004","fix":"Split shareable artifact loading from request-specific evaluation and lease conversion. Key any shared activation by full launch compatibility, evaluate each runtime/wire/profile independently, grant one lease per successful owner, and add concurrent incompatible-request and cancelled-waiter tests.","location":"§§ 1.2 / 2.2 / 2.3 / 4.1 / 4.3 / 4.5 role acquisition","participating_section_ids":["1.2","2.2","2.3","4.1","4.3","4.5"],"prevention":"Test concurrent same-model requests across runtimes, wires, launch profiles, owners, and waiter cancellation before fixing a single-flight key.","principle":"Single-flight keys must contain every fact that changes the shared work, while owner-specific evaluation and lease grants remain per requester.","root_cause":"acquire_role coalesces the whole load/evaluate/lease sequence by display identity although requests carry runtime/profile overrides and distinct owners.","section_id":"2.3","severity":"blocking"},{"category":"unhandled-edge","check_key":"terminal-fence-writer-closure","description":"spawn_executor._prepare_provider_sandbox directly calls run_manager.fail, and resume_executor._park_unlaunched_successor directly calls run_storage.cancel. Both are reachable during the interval § 4.4 places under the run mutex; leaving them direct bypasses terminal intent and lease ordering, while wrapping them with terminalize from inside the mutex self-deadlocks.","finding_id":"F-R9-005","fix":"Make sandbox and process-start helpers return typed failures without terminal writes. Give one outer fence-aware cleanup owner the terminal CAS and exactly-once lease release after the guarded launch block, and race sandbox/spawn failure against external terminalization for spawn and resume.","location":"§§ 4.3 / 4.4 activation-to-launch failure writers","participating_section_ids":["4.3","4.4"],"prevention":"Literal-sweep every complete/fail/cancel/timeout storage call, then classify whether it runs outside, inside, or re-entering each lifecycle mutex.","principle":"Every durable terminal writer must have one non-reentrant ownership path through the lifecycle fence.","root_cause":"The terminal-writer sweep covered named cleanup facades while missing direct fail/cancel calls inside two already-targeted spawn/resume files.","section_id":"4.4","severity":"blocking"},{"category":"unhandled-edge","causal_finding_id":"F-R5-011","causal_section_ids":["4.5"],"check_key":"conversation-owner-wide-lease-release","description":"A failed predecessor release after commit leaves both successor and predecessor leases under the conversation id. The live registry records only the successor cache identity, yet the plan says the next clear, disconnect, or restart converges to exactly one lease even though those paths must release every conversation lease; the predecessor can remain undiscoverable and leak its cache.","finding_id":"F-R9-006","fix":"Define idempotent owner-wide conversation release over the 2.3 lease inventory. State that failed predecessor release leaves two visible leases, clear/disconnect/shutdown/restart reduces them to zero, a later message reacquires exactly one, and old-cache eviction follows the sweep; update 4.5.6 accordingly.","introduced_in_round":5,"location":"§ 4.5 predecessor-release failure and lifecycle cleanup","prevention":"For every partial switch, state immediate resource counts and prove clear, disconnect, shutdown, and restart reduce owner-held resources to zero before reacquisition.","principle":"Lifecycle cleanup must enumerate ownership, not only current routing identity, whenever one owner can temporarily retain multiple resources.","root_cause":"The Round-5 repair exposes a retained predecessor in the lease inventory but leaves release scoped to the successor-only backend registry identity.","section_id":"4.5","severity":"blocking"}],"reviewer_session":"9f5842ca-cb57-4d88-96c2-898bd2b5ec38","round":9,"round_number":9,"verdict":"needs_review"},"session_id":"4b85e4ac-ec4c-4fea-b191-8a2d71162afb"}
```

**Round 10** `kind: verification`

- reviewer_run: 7a05cb4b-25a7-405b-91b4-2a9fc7169703
- reviewer_session: 8c081dc4-ecb0-40b2-abc8-b25d90b9a448
- verdict: needs_review
- findings:
- F-R10-001 / blocking / 4.4 the Constraints handoff disposition still closes #20672 as superseded although it is already closed completed at 5032d6b3cd with its flag and tests landed
- F-R10-002 / blocking / 4.5 backend-cache eviction is decided from a lease-inventory observation with no cache-global boundary, so a last release can evict an entry another conversation is attaching (fixer-induced, F-R9-006)
- F-R10-003 / blocking / 4.4 the `terminalize_from_thread` bridge has the calling thread block on the coroutine that waits for that same thread's CAS, with no ready/result handshake and no proof the loop side settled before `fence_unavailable` (fixer-induced, F-R7-008)
- F-R10-004 / blocking / 4.4 guarded launch releases the mutex and the run lease before `terminalize` publishes terminal intent, so a queued spawn or resume can be admitted into that gap (fixer-induced, F-R9-005)
- F-R10-005 / blocking / 3.1 `SkillSearch.index_skills_async` swaps live metadata in place across the `fit_async` await with no reindex single-flight or generation check (fixer-induced, F-R9-002)
- resolution_notes: Unattended round; the coordinator judged every finding and verified each against the repository. This round was relaunched after its first attempt terminated on a non-retryable `invalid_source_citation` (an unknown `lines` field) with no verdict and no coverage attestation; that attempt's draft findings were not carried into the relaunch, so this result is independently derived. All five accepted, three narrowed. F-R10-001 accepted narrowed: `get_task(#20672)` reports closed completed at 2026-08-23T13:58:51Z and commit 5032d6b3cd landed `-c check_for_update_on_startup=false` in `command_builder.py`, the typed `codex_composer_not_ready` failure with redacted pane text in `_deliver_codex_prompt`, and their tests, so the handoff instruction to close it as superseded is now an impossible action against a closed task. The Constraints bullet splits: #19653 keeps its close-as-superseded disposition, and #20672 becomes a completed-prerequisite record naming commit 5032d6b3cd, with the non-forcible update-menu fixture substitution recorded in the plan itself instead of in a superseded disposition note. 4.4.1 and the `e2e` managed-launch case are retained as baseline regression validation over landed behavior rather than new implementation. No §4.4 writer-sweep change is needed: the landed `run_manager.fail` in `_deliver_codex_prompt` is the missing-composer delivery-task terminalization the fence prose already routes, and `spawn_executor_support.py::*` is already a §4.4 Target whose scope-reason replaces the whole delivery scheduler. F-R10-002 accepted narrowed: 4.5 makes backend caching lease-free and evicts an entry "only when no conversation lease references its identity", while conversations serialize only on their own per-conversation lock, so a last releaser can observe zero leases and remove an entry that a concurrent acquirer has just attached. One cache-global lock in `runtime_manager` covers lookup-or-create, attach, detach, and the compare-and-remove eviction, and the eviction decision re-reads the 2.3 lease inventory while holding it; with lease acquisition ordered before cache attach, an acquirer that took its lease before that read is seen and blocks eviction, and one that takes it after finds the entry already removed and creates a fresh one. A separate attachment-count owner is declined because the lease inventory read under the same lock already linearizes both orders. 4.5 gains both interleavings. F-R10-003 accepted: the bridge as written has the calling thread submit the loop coroutine and block on its completion under `FENCE_BRIDGE_TIMEOUT_SECONDS` while that coroutine waits for a CAS only the blocked thread can run, and it returns `fence_unavailable` on a bounded-wait expiry without establishing that the loop side settled. The bridge becomes two-phase: the worker submits the coroutine and waits on a bounded mutex-acquired signal, runs its synchronous CAS on the worker thread, publishes its value or exception through a thread-safe result channel, then waits for bounded loop-side finalization. Each phase names its owner and its timeout disposition, and a timeout cancels the loop coroutine and awaits its `finally` before returning `fence_unavailable`, so a later retry cannot overlap a live prior terminalization. 4.4 races each timeout phase and the subsequent retry. F-R10-004 accepted: the round-9 repair has the guarded block release the mutex and the run-owned lease and then run `terminalize` outside the mutex, but admission re-reads terminal intent and durable status under that same mutex, so the interval between unlock and intent publication admits a queued spawn or resume that then proceeds to profile construction and process creation while the failing actor releases the lease underneath it. The fence entry gains a non-reentrant publish-intent-and-claim operation available to the current mutex holder: the failing owner publishes terminal intent and claims the cleanup while still holding the mutex, then unlocks, releases the run lease, and runs the claimed CAS without re-entering admission. Every competing launch that wins the mutex afterwards reads the published intent and aborts before profile construction. 4.4.11 races spawn and resume paused between intent publication and the CAS, asserting no competing launch and exactly-once lease release. F-R10-005 accepted narrowed: repository inspection confirms `SkillSearch.index_skills_async` clears and repopulates `_skill_names` and `_skill_meta` before awaiting `self._searcher.fit_async(items)` and only then sets `_indexed`, so a concurrent search sees new metadata against an unfitted or previous backend, a raised fit leaves swapped metadata with stale indexed state, and two overlapping fits can publish out of order. `SkillSearch` gains one reindex single-flight with a monotonic generation counter: candidate metadata and a candidate `UnifiedSearcher` are built and fitted off to the side, one atomic assignment publishes them only while the captured generation is still current, and searches keep serving the previous successful snapshot until that assignment, with a failed or superseded fit discarding its candidate and leaving the prior snapshot intact. A broader immutable-snapshot refactor of `UnifiedSearcher` internals is declined because the candidate instance is itself the snapshot. 3.1.10 gains concurrent-search-during-fit, failed-fit, and stale-fit-publication clauses. No new deliverables and no new Targets; repairs land on the Constraints, 3.1, 4.4, and 4.5 after this checkpoint.

```json plan-review-round
{"evidence_id":"9a1ea9a9-bde7-49d3-a25c-8584faadb4b7","plan_hash":"62f34ebb0732b5f41252194d80dbe683333b3a6d3ee4ac1e1a9346a74ab5a3b1","round_number":10,"round_result":{"coverage_attestation":{"adjacent_variant_complete":true,"attestation_digest":"063ad7639e4d4f7bf51c5f48a9878f147e621a317a590fe211ef075c6422a0e7","cross_lane_interaction_complete":true,"disposition_counts":{"dismissed":0,"emitted_findings":5,"total":5},"evidence_id":"9a1ea9a9-bde7-49d3-a25c-8584faadb4b7","lanes":[{"candidate_count":1,"lane_id":"requirements_traceability","status":"completed"},{"candidate_count":0,"lane_id":"repository_blast_radius","status":"completed"},{"candidate_count":4,"lane_id":"runtime_invariants","status":"completed"}],"shadow_manifest_status":{"entry_count":18,"manifest_digest":"440a5519b8b41783d87393f8573c0c1aa282c975f43b9ba3eb943b2c2516374c","status":"valid"},"source_digest":"db58b268bf004f608ee57c5d2ca95f309e1638bfd45cb85a6403aca41465b176","version":1},"findings":[{"category":"traceability","check_key":"closed-prerequisite-handoff-drift","description":"The plan still instructs handoff to close #20672 as superseded and includes its Codex update-suppression behavior in § 4.4, but #20672 is already closed completed at commit 5032d6b3cd and the production flag plus regression test are present. Expansion would assign already-landed work and an invalid task action.","finding_id":"F-R10-001","fix":"Replace the supersede/close instruction with a completed-prerequisite record for #20672 and commit 5032d6b3cd. Narrow § 4.4 to the remaining classifier, lifecycle-fence, launch-race, and integration work while retaining the landed update-suppression test as baseline regression validation.","location":"Constraints handoff disposition and § 4.4 Codex startup behavior","prevention":"Refresh every referenced Gobby task immediately before final handoff; record closed commits as baseline and remove lifecycle actions against already-closed tasks.","principle":"An implementation plan must distinguish completed prerequisites from work that expansion still needs to perform.","root_cause":"Task #20672 completed after the handoff disposition was written, leaving already-landed behavior modeled as future implementation and an impossible close-as-superseded action.","section_id":"4.4","severity":"blocking"},{"category":"unhandled-edge","causal_finding_id":"F-R9-006","causal_section_ids":["4.5"],"check_key":"edge-case-coverage","description":"A last-releasing conversation can observe zero leases and decide to evict while another conversation concurrently acquires the same model and attaches the shared backend. The first conversation may then evict or stop the backend now used by the second; per-conversation locks do not close this cross-conversation race.","finding_id":"F-R10-002","fix":"Add one cache-global mutex or atomic attachment-count owner in `runtime_manager`. Under it, perform cache lookup/create, register attachment before eviction is possible, unregister on release, and compare-and-remove only while the count remains zero; state the ordering relative to model-lease acquisition/release. Add both completion orders of the deterministic last-release/new-attach race.","introduced_in_round":10,"location":"§§ 2.3 / 4.5 conversation release and backend-cache eviction","prevention":"Race the last release against a new acquisition for the same cache identity and require lookup, attachment registration, unregistration, and eviction to share one synchronization owner.","principle":"Process-global cache eviction must linearize against every attachment and release for the same cache identity.","root_cause":"The round-9 owner-wide release repair serializes each conversation independently and decides cache eviction from a separate lease-inventory observation, without one cache-global attachment owner or atomic compare-and-remove boundary.","section_id":"4.5","severity":"blocking"},{"category":"unhandled-edge","causal_finding_id":"F-R7-008","causal_section_ids":["4.4"],"check_key":"edge-case-coverage","description":"As written, the worker waits for coroutine completion while the coroutine waits for a CAS only that worker can execute. The plan also permits `fence_unavailable` after a bounded wait without proving the loop coroutine has settled, so a later retry can overlap a still-live prior terminalization.","finding_id":"F-R10-003","fix":"Specify a two-phase bridge: the worker submits the loop coroutine, waits on a bounded mutex-acquired signal, executes the CAS on the worker thread, publishes value or exception through a thread-safe result channel, then waits for bounded loop finalization. Define cancellation and ownership before acquisition, during CAS, and during finalization, and test each timeout plus later retry.","introduced_in_round":8,"location":"§ 4.4 `terminalize_from_thread` bridge","prevention":"For every thread/loop bridge, enumerate submit, mutex-acquired signal, caller work, result publication, finalization, timeout, exception, and stopped-loop ownership before accepting the design.","principle":"A synchronous-to-async mutex bridge needs an explicit non-cyclic ownership handshake and one owner for every timeout phase.","root_cause":"The repair says the loop coroutine signals the calling thread to execute the CAS while the same calling thread submits the coroutine and blocks on its completion; it never defines the ready/result exchange that lets that blocked thread perform the CAS.","section_id":"4.4","severity":"blocking"},{"category":"unhandled-edge","causal_finding_id":"F-R9-005","causal_section_ids":["4.4"],"check_key":"edge-case-coverage","description":"After sandbox or tmux-spawn failure, durable status remains nonterminal and terminal intent remains false while the executor releases the mutex and lease. A queued spawn or resume can enter that gap, pass both checks, and create or checkpoint a process; the failing actor can release the lease underneath that newly admitted launch before terminalization reacquires the mutex.","finding_id":"F-R10-004","fix":"Add a non-reentrant fence operation for the current mutex holder to publish terminal intent and claim cleanup before unlocking. Then unlock, release the run lease, and execute the claimed terminal CAS without re-entering admission. Add spawn and resume races paused after intent publication and before CAS, asserting no competing launch and exactly-once lease release.","introduced_in_round":10,"location":"§§ 4.3 / 4.4 guarded launch failure to terminalization","prevention":"Pause after every under-mutex failure decision and before terminal CAS; prove terminal intent is already visible and every competing spawn or resume aborts before profile construction or process creation.","principle":"Once guarded launch decides to terminalize, terminal intent must become visible before launch admission is reopened.","root_cause":"The round-9 repair moves terminalization outside the non-reentrant mutex but orders mutex release and run-lease release before `terminalize` publishes intent.","section_id":"4.4","severity":"blocking"},{"category":"unhandled-edge","causal_finding_id":"F-R9-002","causal_section_ids":["3.1"],"check_key":"edge-case-coverage","description":"A concurrent search can observe new skill metadata paired with an unfitted or old backend, a failed fit can leave `SkillSearch` reporting indexed while `UnifiedSearcher` is unfitted, and two fits can complete out of order with metadata from one generation and results from another. The shared embedding guard blocks a profile flip, but it does not serialize searches or competing fits.","finding_id":"F-R10-005","fix":"Give `SkillSearch` one reindex single-flight or monotonic generation owner. Build candidate metadata and a candidate `UnifiedSearcher` off to the side, atomically swap one immutable snapshot only if its generation is current, and keep searches on the previous successful snapshot until that swap. Add concurrent-search, fit-failure, and stale-fit-publication tests.","introduced_in_round":10,"location":"§ 3.1 skill-search construction and index generation","prevention":"For every asynchronous index rebuild, test search-during-fit, failed fit, concurrent fits, and old-fit-completes-last; require immutable generation capture and atomic publication.","principle":"Reindexing must publish one coherent generation atomically and preserve the last successful generation until replacement succeeds.","root_cause":"The round-9 repair moves fitting onto the daemon loop and guards embedding generation against profile publication, while `SkillSearch` and `UnifiedSearcher` still mutate live metadata and fitted state in place across awaits with no fit single-flight or generation check.","section_id":"3.1","severity":"blocking"}],"reviewer_session":"8c081dc4-ecb0-40b2-abc8-b25d90b9a448","round":10,"round_number":10,"verdict":"needs_review"},"session_id":"4b85e4ac-ec4c-4fea-b191-8a2d71162afb"}
```

**Round 11** `kind: verification`

- reviewer_run: 50b0c9de-42b0-44b2-a463-07bb9cef1cf6
- reviewer_session: d61b1ad5-68ad-45a3-94ea-9c200e5240ce
- verdict: needs_review
- findings:
- F-R11-001 / blocking / Constraints the UI-TARS handoff bullet still instructs reparenting #20405 under #18498 and closing #18866, both of which already landed
- F-R11-002 / blocking / 4.4 Targets still claim `command_builder.py` and its test add the Codex update override that commit 5032d6b3cd already landed (fixer-induced, F-R10-001)
- F-R11-003 / blocking / 3.1 the SkillSearch snapshot repair targets only construction and publication, leaving every reader, filter helper, mutation helper, and the real `search_skills` tool seam outside the captured generation (fixer-induced, F-R10-005)
- F-R11-004 / blocking / 4.4 the bridge's finalization-timeout branch returns a landed terminal outcome with no stated owner of the still-live coroutine, mutex, entry, and lease release, while 4.4.12 asserts a fresh entry after every timeout outcome (fixer-induced, F-R10-003)
- F-R11-005 / blocking / 4.5 the 2.3 lease inventory keys model identity and role, so it cannot decide which backend cache entry (runtime, family, model, context, modalities, profile hash) is attached (fixer-induced, F-R10-002)
- F-R11-006 / blocking / 4.5 raw websocket disconnect is listed as an unconditional conversation-lease release path although the connection handler explicitly preserves chat sessions
- resolution_notes: Unattended round at the configured review cap; the coordinator judged every finding and verified each against the repository. All six accepted, three narrowed. F-R11-001 accepted as its fix: `get_task(#20405)` reports `path_cache` `18498.20405` and `get_task(#18866)` reports closed completed at 2026-08-22T18:12:09Z, so both instructed mutations are already landed. The bullet becomes a landed-state record — UI-TARS stays the post-0.5 local/offline computer-use fallback, #20405 is already under #18498, #18866 is already closed completed, handoff takes no lifecycle action on either, and 6.2's follow-up link is retained. F-R11-002 accepted as its fix: the landed test file already asserts the override across every command shape including the caller-override case, so no delta remains and `src/gobby/agents/spawners/command_builder.py::*` and `tests/agents/spawners/test_command_builder.py::*` leave the 4.4 change inventory; 4.3 still targets the same production file for its own profile-control delta, so the file stays in the plan. 4.4.1 is retained as baseline regression validation and is corrected to the landed contract: the suppression override is applied last so it wins over any caller-supplied `check_for_update_on_startup` override rather than being the only such entry. F-R11-003 accepted narrowed: repository inspection confirms `SkillSearch.search_async` reads `_indexed` and the searcher before its await and then reaches `_passes_filters` and `_skill_names` after it, so a mid-search publication pairs one generation's ranking with another's metadata. The two exact Targets become one justified `src/gobby/skills/search.py::*` wildcard naming publication, the search readers, the keyword and filter helpers, the state accessors, the incremental mutations, stats, and clear, and `tests/mcp_proxy/tools/skills/test_search_skills.py::*` joins the Targets — that test seam is independently required because 3.1 already replaces `search_skills.py::*` with no test of its own. 3.1.11 gains the post-ranking-swap coherence clause and the empty-publication case and names both test files. A separate acceptance item through the tool is declined because 3.1.11 now carries that seam. F-R11-004 accepted narrowed: the prose settles the acquire-timeout branch but says nothing about who owns the still-live coroutine, mutex, entry, and scheduled lease release when finalization expires after a landed CAS, and 4.4.12 asserts a retry after "any of those outcomes" reaches a fresh entry, which is false for exactly that branch. The disposition is stated with machinery the plan already defines: the loop coroutine remains the sole cleanup owner and its `finally` still releases the mutex, drives the lease release, and reclaims the entry exactly once; the returning writer performs no cleanup of its own; and a concurrent or later terminalize for the same run meets the still-live non-reentrant entry, reads the durable terminal state through the published intent, and returns the landed outcome without a second CAS. 4.4.12 splits its retry clause accordingly and adds a retry against a deliberately stalled finalization. A separate acknowledgement set and a restart-reconciliation fallback are declined because the `finally` already owns reclamation and the run is already terminal on this branch, so 4.3 reconciliation has nothing to classify. F-R11-005 accepted as its fix: leases are keyed by normalized model identity and role and carry only owner, times, and profile hash, while the cache identity is runtime, family, normalized model identity, context, modalities, and profile hash, so the inventory read cannot answer which cache entry is attached and two same-model entries differing by runtime, context, or modalities are indistinguishable to it — the round-10 claim that the inventory read is the attachment record was wrong. `runtime_manager`'s existing session-id-to-cache-identity registry becomes the attachment record: its write and clear move under the cache-global lock, taken after the per-conversation lock in one fixed order, and compare-and-remove evicts only when no registry value equals the full cache identity. The 2.3 leases keep their model-lifetime role. 4.5.3 gains a same-model switch across distinct runtimes proving only the predecessor entry is released and evicted. F-R11-006 accepted as its fix: `WebSocketServer._handle_connection`'s `finally` block cleans up client state and states verbatim that chat sessions persist, so a transport disconnect ends no conversation, and releasing on it can unload or evict a backend a live conversation still uses or is streaming through. Raw websocket disconnect leaves the release triggers; the conversation lease follows the persistent chat session and releases on failed start, clear/reset, deletion, explicit stop, the existing `cleanup_idle_sessions` teardown, daemon shutdown, and restart reconciliation. 4.5.3 gains an overlapping reconnect/old-disconnect race and a disconnect during an active stream, and 4.5.6's release-path enumeration is corrected. No new deliverables; two Targets are removed and two added, and repairs land on the Constraints, 3.1, 4.4, and 4.5 after this checkpoint. This is the configured review cap, so no further adversary round follows; the plan proceeds to human handoff.

```json plan-review-round
{"evidence_id":"f672bff1-b861-4f0a-bc0a-fdc65c5b0e7b","plan_hash":"f96b4d2ce11e0e51ea8c07aa12b0fcdfca544df0f700d4700a9737df2b242269","round_number":11,"round_result":{"coverage_attestation":{"adjacent_variant_complete":true,"attestation_digest":"bc1b282571d7b0d852f63bc68b15ba81274121c8f859f230a60a3e76236550b8","cross_lane_interaction_complete":true,"disposition_counts":{"dismissed":1,"emitted_findings":6,"total":7},"evidence_id":"f672bff1-b861-4f0a-bc0a-fdc65c5b0e7b","lanes":[{"candidate_count":2,"lane_id":"requirements_traceability","status":"completed"},{"candidate_count":1,"lane_id":"repository_blast_radius","status":"completed"},{"candidate_count":4,"lane_id":"runtime_invariants","status":"completed"}],"shadow_manifest_status":{"entry_count":18,"manifest_digest":"a26e3868d6d705637900ae55837b243c664e33531cdb4bb9681225c10f8f84b4","status":"valid"},"source_digest":"c59479e93788d21d12494dd27747d87cb6e548a764779372ddace775589be01c","version":1},"findings":[{"category":"traceability","check_key":"handoff-task-state-freshness","description":"The plan still instructs handoff to reparent #20405 under #18498 and close #18866. Direct task reads show #20405 already has path 18498.20405 and #18866 closed completed on 2026-08-22, while §6.2 already describes the new parentage. Both requested mutations are stale.","finding_id":"F-R11-001","fix":"Rewrite the Constraints bullet as a landed-state record: #20405 is already under #18498 and #18866 is already closed completed, so handoff takes no lifecycle action on either; retain §6.2's follow-up link.","location":"Constraints — UI-TARS handoff lifecycle bullet","prevention":"Before each final review, call get_task for every task named in a handoff action and classify it as pending action or landed prerequisite.","principle":"Every handoff action must be valid against the current state and parentage of the task it mutates.","root_cause":"The plan preserved an earlier lifecycle recipe after #20405 was reparented and #18866 closed.","section_id":"Constraints","severity":"blocking"},{"category":"traceability","causal_finding_id":"F-R10-001","causal_section_ids":["Constraints","4.4"],"check_key":"completed-prerequisite-target-drift","description":"Section 4.4 says commit 5032d6b3cd already landed the unconditional Codex update override and focused command-builder coverage and that this deliverable keeps them unchanged. Its Targets still claim command_builder.py will add the override and its test will add spawn/resume assertions, so the leaf's declared implementation scope contradicts the repaired baseline contract.","finding_id":"F-R11-002","fix":"Remove src/gobby/agents/spawners/command_builder.py and tests/agents/spawners/test_command_builder.py from §4.4 Targets while retaining 4.4.1 as baseline regression validation. If another delta is required, replace those scope reasons with that exact remaining change.","introduced_in_round":11,"location":"§4.4 Targets versus landed #20672 baseline","prevention":"When a prerequisite becomes landed baseline, diff its commit against every related Target, scope reason, body change verb, and acceptance item.","principle":"Targets enumerate implementation deltas; already-landed prerequisite code may remain acceptance evidence without remaining a change Target.","root_cause":"The round-10 repair reclassified #20672 as completed baseline but left its implementation files in the §4.4 change inventory with stale add/assert scope reasons.","section_id":"4.4","severity":"blocking"},{"category":"traceability","causal_finding_id":"F-R10-005","causal_section_ids":["3.1"],"check_key":"edge-case-coverage","description":"Current SkillSearch.search_async reads indexed state and the searcher before awaiting, then reads metadata through helpers afterward. Section 3.1 targets only SkillSearch.__init__ and index_skills_async, so it cannot make ranking, fallback, filtering, metadata lookup, empty clears, and incremental mutations consume one captured snapshot; 3.1.11 also lacks a race paused after ranking and before metadata lookup through the real tool.","finding_id":"F-R11-003","fix":"Replace the two exact SkillSearch Targets with a justified src/gobby/skills/search.py wildcard covering publication, search readers, keyword/filter helpers, state accessors, add/update/remove, stats, and clear. Add tests/mcp_proxy/tools/skills/test_search_skills.py to Targets and acceptance proving the real tool keeps one prior coherent snapshot through a post-ranking swap, empty publication, failed fit, and superseded fit while index generation runs on the daemon loop.","introduced_in_round":11,"location":"§3.1 SkillSearch atomic generation Targets and acceptance","prevention":"For every atomic snapshot repair, sweep all reads and writes of each member moved into the snapshot, then pause after every await and prove the same captured generation reaches result assembly.","principle":"Every reader and helper that participates in an immutable published snapshot must capture and use one generation across all await boundaries.","root_cause":"The round-10 repair targeted snapshot construction and publication while omitting the existing readers, metadata/filter helpers, mutation helpers, state accessors, and real search_skills tool test seam.","section_id":"3.1","severity":"blocking"},{"category":"unhandled-edge","causal_finding_id":"F-R10-003","causal_section_ids":["4.4"],"check_key":"edge-case-coverage","description":"When FENCE_BRIDGE_FINALIZE_TIMEOUT_SECONDS expires, the loop coroutine may still own the run mutex, fence entry, or lease cleanup. Returning the terminal outcome can be valid, but the plan simultaneously promises that a later retry finds a fresh entry and cannot overlap, even though no finalization acknowledgement or retained-entry join rule establishes either property.","finding_id":"F-R11-004","fix":"Add a finalization acknowledgement set only after mutex release, lease-cleanup disposition, and entry reclamation. On post-CAS timeout, return the landed terminal outcome while retaining the live entry/future as cleanup owner and require retries to join or observe it; create a fresh entry only after acknowledgement. Test a retry while finalization is deliberately stalled and the restart-reconciliation fallback if the loop never resumes.","introduced_in_round":11,"location":"§4.4 terminalize_from_thread finalization timeout","prevention":"For every bridge timeout, enumerate the live future, mutex, registry entry, durable state, lease cleanup, retry owner, and acknowledgement after the timeout fires.","principle":"A bounded bridge must retain one cleanup owner until mutex release, lease-cleanup disposition, and entry reclamation are acknowledged.","root_cause":"The round-10 repair specifies settlement after acquire timeout but returns a landed CAS outcome after finalization timeout without settling or retaining a retry-visible cleanup contract, then claims the next retry uses a fresh entry with no overlap.","section_id":"4.4","severity":"blocking"},{"category":"unhandled-edge","causal_finding_id":"F-R10-002","causal_section_ids":["4.5"],"check_key":"edge-case-coverage","description":"Two backend entries for the same normalized coding model can differ by CLI runtime, effective context, or modalities while their conversation leases remain indistinguishable at the cache-key level. The cache-global lock linearizes operations, yet its lease-inventory read cannot decide which exact backend is attached, so an active entry may be conflated with an unrelated predecessor and old entries may never become deterministically evictable.","finding_id":"F-R11-005","fix":"Use runtime_manager's existing session-id-to-cache-identity registry under the cache-global lock as the exact attachment record; keep §2.3 leases as model-lifetime protection. Compare-and-remove only when no registry value equals the full cache identity, and add a same-model switch between distinct runtimes or profiles proving only the predecessor entry is released and evicted.","introduced_in_round":11,"location":"§§2.3 / 4.5 lease identity versus backend-cache attachment identity","prevention":"Before using one ownership inventory for another resource, compare every field in both identity tuples and race two resources that share the coarse key but differ in each omitted field.","principle":"The record that authorizes cache eviction must identify the exact cache entry attachment it protects.","root_cause":"The round-10 repair treats the §2.3 lease inventory as an attachment record even though leases key normalized model identity and role and carry profile hash, while §4.5 cache identity also includes runtime, context, and modalities.","section_id":"4.5","severity":"blocking"},{"category":"unhandled-edge","check_key":"conversation-disconnect-attachment-lifetime","description":"The current websocket lifecycle removes one client while explicitly preserving chat sessions, and a reconnect can overlap the old socket's disconnect or an active stream. Releasing the conversation's lease on that transport event can unload or evict the backend still used by the persistent conversation.","finding_id":"F-R11-006","fix":"Remove raw websocket disconnect from unconditional release triggers. Keep the lease with the persistent chat session and release it on clear/reset, delete, explicit session stop, idle-session cleanup, daemon shutdown, or the last defined conversation attachment; add overlapping reconnect/old-disconnect and disconnect-during-stream tests.","location":"§4.5 conversation lease release on client disconnect","prevention":"Race old-socket disconnect, new-socket reconnect, and active streaming for every resource owned by a persistent conversation.","principle":"Persistent conversation resources outlive an individual transport connection and release only when the conversation or its last active attachment ends.","root_cause":"The plan lists client disconnect as an unconditional conversation-lease release path without distinguishing a websocket from the persistent chat session it can reconnect to.","section_id":"4.5","severity":"blocking"}],"reviewer_session":"d61b1ad5-68ad-45a3-94ea-9c200e5240ce","round":11,"round_number":11,"verdict":"needs_review"},"session_id":"4b85e4ac-ec4c-4fea-b191-8a2d71162afb"}
```

**Human handoff** `kind: verification`

- trigger: configured adversarial-review cap reached at round 11
- completed_plan_review_rounds: 11
- final_verdict: needs_review (round 11, evidence f672bff1-b861-4f0a-bc0a-fdc65c5b0e7b)
- findings_disposition: all 6 round-11 findings accepted (3 narrowed) and repaired on this artifact after the round-11 checkpoint was finalized on the unchanged bytes
- base_validation: `uv run gobby plans validate` exits 0 with only non-fatal consumer-coverage warnings
- resolution_notes: Rounds 1 through 11 ran unattended under the user's standing
  directive, with the coordinator judging every finding, verifying each against
  the repository, and recording its rationale in that round's changelog entry.
  No further adversary round may run: round 11 was the cap, so this artifact
  carries a `needs_review` final verdict with every accepted repair applied
  rather than an adversary `approved` verdict. It therefore holds no M1 Task
  Manifest and no adversary-derived approval evidence. Continuing to build
  requires the explicit human handoff path — coordinator manifest derivation
  through `derive_plan_handoff_manifest` and `apply_plan_handoff_manifest`,
  expansion-mode validation, and `gobby build` seeded at
  `planning_seed_state=approved` with `--completed-plan-review-rounds 11`.
  Rounds 8 through 11 each returned findings that were mostly fixer-induced
  defects traced to the immediately preceding round's repairs, so the review
  has been converging on repair quality rather than discovering new plan gaps.

**Human edit** `kind: verification`

- editor: Josh Wilhelmi with Claude (session #11019, task #20795)
- change: 1.1 now states the upgrade-time handling of pre-existing
  `lmstudio`/`ollama`/`vllm` rows under `ai.generation.endpoints` (quarantined
  from the resolved config with a startup warning, stored row untouched,
  writes rejected per 1.1.16) and adds 1.1.17; 6.2 carries the matching
  migration-contract paragraph and 6.2.5. No new Targets: the behavior lives
  in `src/gobby/config/app.py` and `src/gobby/config/persistence.py`, which 1.1
  already scopes.
- rationale: 1.1.16 as written reads as hard validation, which would stop the
  daemon on DB-stored config the operator can only repair through the daemon's
  own API. Surfaced while removing two stale vLLM test endpoints from live
  config revision 46 on 2026-08-23.
