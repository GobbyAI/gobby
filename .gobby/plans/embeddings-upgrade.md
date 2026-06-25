# Plan: Add an embedding-model picker to `gobby install` (nomic quants + Qwen3 sizes)

## Context

Today Gobby is hard-wired to one embedding model: **nomic-embed-text-v1.5 @ 768-dim**.
The user wants to *add* (not swap) the ability to choose a stronger local embedder at
install time. The original ask was NV-Embed-v2; research killed it:

- **License:** `cc-by-nc-4.0` (non-commercial). Not redistributing weights dodges the
  *distribution* clause, but CC-BY-NC restricts *use* — it follows whoever runs the model,
  and most Gobby users run it for paid work (= commercial). Bad footgun for a commercial product.
- **Serving:** custom `NVEmbedModel` arch won't convert to GGUF (`llama.cpp`
  `NotImplementedError`) + needs `trust_remote_code` — no LM Studio/Ollama path, bespoke sidecar only.

**Decision:** drop NV-Embed-v2. Build a 6-option install picker between **nomic-embed-text-v1.5
(Q4/Q8/F16)** and **Qwen3-Embedding (0.6B/4B/8B)** — all Apache-2.0. Catalog entries are
**GGUF-backed**; **Ollama is stable for Qwen3**, **LM Studio is experimental** for Qwen3, and
**per-provider quant support differs** (see catalog). Then set up **Qwen3-Embedding-8B** locally
(128GB unified memory; 8B trivial). Qwen3-8B is ~the same MTEB-class jump over nomic that
NV-Embed-v2 offered (~62 → ~70+), without the license/serving baggage.

## Architecture (verified)

Two **independent** embedding paths; both must move together on a model change because dim
changes and dim is shared config.

| Path | Where | How it serves | Reads |
| --- | --- | --- | --- |
| **Daemon** (memory, semantic tool search, skills) | this repo | OpenAI-compatible HTTP via LM Studio `:1234` / Ollama `:11434` (`AsyncOpenAI` + `api_base`) | `ai.embeddings.*` |
| **gcode** (code index) | separate `gobby-code` repo | **in-process** `llama-cpp-2`, GGUF in `~/.gobby/models/` (hardcoded nomic Q8_0) | generated resolved-config file under `~/.gobby/` (checksum+version) + local GGUF |

Reused, **not** rewritten:

- `EmbeddingsConfig` — `src/gobby/config/persistence.py:180-228` (has `model/dim/api_base/api_key/query_prefix`).
- `_apply_prefix`/`_needs_nomic_prefix` — `src/gobby/ai/embeddings.py:225-243`. Applies
  `query_prefix` to **queries only**; docs get nothing → exactly Qwen3's instruction-aware design.
- `install_embedding()` — `src/gobby/cli/installers/embedding.py:60` with `_persist_embedding_config`,
  `_probe_embedding_dim`, `_health_check_embedding`, `_setup_lmstudio` (`lms get`/`lms load`),
  `_setup_ollama` (`ollama pull`). Persistence keys in `src/gobby/config/embedding_keys.py`.
- Install picker — `src/gobby/cli/_install_embedding_prompts.py`.
- **`src/gobby/cli/embeddings.py` already exists** — extend it for `switch` (do not create a new group).
- `MemoryManager.reindex_embeddings`; tool auto-repair in `src/gobby/mcp_proxy/semantic_search.py`;
  `VectorStore.ensure_collection(..., recreate_on_mismatch=True)` (`vectorstore.py:604-671`).

## Decisions (from Q&A + review)

1. **Scope:** full — daemon **and** code-index. gcode is a separate repo → specced + filed as a
   task with a release dependency; daemon work lands first.
2. **Reindex:** one orchestrated, **staged two-phase, resumable/idempotent** command (old index
   stays live until a verified flip) — not a single ACID transaction. A **switch journal**
   (`embedding_switch_runs`) + startup/doctor reconciliation closes the flip/config crash window.
3. **Backend:** both GGUF backends in the picker. **Ollama is the default/stable path for
   Qwen3**; **LM Studio is experimental for Qwen3** (LM Studio issue #965 classifies
   `Qwen/Qwen3-Embedding-8B-GGUF` as an inference model). nomic stays fine on either. MLX was
   considered and rejected — its reliable path needs a Python sidecar (the thing dropping
   NV-Embed-v2 avoided); GGUF + Metal already gives GPU acceleration on this Mac.
4. **One model system-wide:** daemon + gcode use the same model (shared dim); the chosen GGUF is
   also placed in `~/.gobby/models/` for gcode's in-process loader.

## Config changes (review fixes #1, #2)

- **ConfigStore is the only source of truth** for dynamic embedding state. Persist canonical
  `ai.embeddings.*` keys through `ConfigStore` — **never write mutable model state into
  `bootstrap.yaml`** (bootstrap only locates the hub/config store).
- **New field `ai.embeddings.catalog_key`** — **quant-qualified** stable identity (e.g.
  `qwen3-8b-q8`), decoupled from the provider model ID. `ai.embeddings.model` stays the
  provider/runtime ID (e.g. `qwen3-embedding:8b-q8_0`); gcode resolves GGUF/quant/pooling from
  `catalog_key`, **not** by reverse-mapping `model`. Quant lives **in the key** so gcode's pinned
  GGUF and the daemon's Ollama tag can't drift. Add `catalog_key` to `EmbeddingsConfig` and a new
  `AI_EMBEDDING_CATALOG_KEY` in `embedding_keys.py`.
- **`AI_EMBEDDING_QUERY_PREFIX_KEY` already exists** (`embedding_keys.py:33`). Missing work is only
  *persisting* it: include `query_prefix` in the `_persist_embedding_config` write that runs at the
  switch **flip** (not a direct install write). Runtime already reads `EmbeddingsConfig.query_prefix`.
- **gcode handoff (until gcode reads ConfigStore):** the daemon writes a generated
  `~/.gobby/embeddings.resolved.json` derived from ConfigStore + catalog — `catalog_key, dim,
  gguf_filename, gguf_sha256, pooling, append_eos, normalize, query_prefix` — plus a `version` and a
  `checksum`. **Checksum is defined precisely** so both sides agree: `sha256` over the **canonical
  JSON** serialization of the payload **excluding the `checksum` field** — sorted keys, UTF-8, fixed
  separators (`(",", ":")`), no trailing whitespace. gcode recomputes it the same way and validates
  checksum + version before use; mismatch ⇒ refuse + signal "needs rebuild". Derived cache, not a
  second source of truth.

## Model catalog (single source of truth — review fixes #1, #4, #5)

**Location (fix #1):** shared module **`src/gobby/ai/embedding_catalog.py`** (next to
`ai/embeddings.py`), **not** installer-private — it's consumed by install, `embeddings switch`,
runtime diagnostics, and the gcode resolved-config emitter. Frozen `key -> EmbeddingModelSpec`. Fields:

`key, label, dim, family, query_prefix, provider_models{ollama?, lmstudio?}, gguf_repo,
gguf_filename, gguf_revision, gguf_sha256, quant, pooling, append_eos, normalize,
compatibility{ollama, lmstudio}, recommended`

**Pinning (fix #5):** every GGUF entry pins `gguf_repo + gguf_filename + gguf_revision + gguf_sha256`.
Downloads verify sha256; "download if missing" never trusts an unverified file.

**Provider-aware quant truth (fix #4):** quant choice is real for the **GGUF artifact** (gcode's
`~/.gobby/models/` copy + LM Studio's `lms get <repo>:<quant-file>`). **Ollama's `nomic-embed-text`
is a single F16 blob — Q4/Q8 nomic choices are NOT real on Ollama**, so the picker hides or labels
them "gcode-GGUF / LM Studio only" when the daemon provider is Ollama. Qwen3 *does* have official
Ollama quant tags, so no community tags needed.

**Keys are quant-qualified; the picker shows six *friendly choices*, the catalog holds more keys.**
The menu presents six options (nomic Q4/Q8/F16 + Qwen3 0.6B/4B/8B); each maps to a default
quant-qualified catalog key (Qwen3 sizes default to `-q8` on capable hardware). The catalog itself
**contains additional variant keys** (e.g. `qwen3-8b-f16`, `qwen3-8b-q4`) reachable via an advanced
override — so don't expect the catalog to have exactly six keys. One key → one Ollama tag → one
pinned GGUF, so they can't drift.

| catalog_key | dim | family | ollama daemon tag | gguf_repo (gcode/LM Studio) | pool/eos/norm |
| --- | --- | --- | --- | --- | --- |
| `nomic-v1.5-q4` | 768 | nomic | F16-only (quant n/a) | nomic-embed-text-v1.5 GGUF · Q4_K_M | mean/no/yes |
| `nomic-v1.5-q8` | 768 | nomic | F16-only (quant n/a) | …Q8_0 | mean/no/yes |
| `nomic-v1.5-f16` | 768 | nomic | `nomic-embed-text` | …F16 | mean/no/yes |
| `qwen3-0.6b-q8` | 1024 | qwen3 | `qwen3-embedding:0.6b-q8_0` | `Qwen/Qwen3-Embedding-0.6B-GGUF` | last/yes/yes |
| `qwen3-4b-q8` | 2560 | qwen3 | `qwen3-embedding:4b-q8_0` | `Qwen/Qwen3-Embedding-4B-GGUF` | last/yes/yes |
| `qwen3-8b-q8` ⭐ | 4096 | qwen3 | `qwen3-embedding:8b-q8_0` | `Qwen/Qwen3-Embedding-8B-GGUF` | last/yes/yes |

- Qwen3 quant on Ollama uses **official** tags (`qwen3-embedding:8b`, `:8b-q8_0`, `:8b-fp16`) — the
  128GB local setup picks the `-q8`/`-f16` keys over the default Q4_K_M. No `dengcao/*` dependence.
  Each Qwen3 size also has a `-f16` (and `-q4`) key variant for the advanced override.
- `query_prefix` (qwen3): `"Instruct: Given a search query, retrieve relevant passages that answer
  the query\nQuery: "` (Qwen3 default task; tunable). nomic: `None` (auto via `_needs_nomic_prefix`).
- All six are Apache-2.0 → **no license gate**.
- `pooling`/`append_eos`/`normalize` are authoritative for gcode; confirm nomic's mean-pooling
  against current gcode behavior at impl.

## Implementation — daemon side (this repo)

1. **Catalog** — `src/gobby/ai/embedding_catalog.py` (spec + **six default entries plus advanced
   quant variants**; serializable for the gcode resolved-config emitter).
2. **Picker** — extend `_install_embedding_prompts.py`: after provider selection, a 6-option model
   menu over the catalog (default selection = current nomic, so existing default UX is unchanged).
   Each friendly choice resolves to a **quant-qualified catalog key** (see catalog) and the matching
   `provider_models.{ollama|lmstudio}` ref. Qwen3 under LM Studio prints an "experimental" warning.
3. **install delegates to switch (review fix — sequencing).** `gobby install` must NOT write the
   canonical active `ai.embeddings.{model,catalog_key,dim,query_prefix}` directly — that would point
   ConfigStore at e.g. Qwen3/4096 while live collections are still 768. Instead install **stages the
   resolved profile** (records the selection, stores `api_key` to `SecretStore`, makes the endpoint
   available) and **immediately invokes the same `embeddings switch` state machine** (option 1).
   The canonical write happens only at the switch **flip**. First-time installs with empty indexes
   take the fast path (build is trivial; flip just establishes the initial active collections).
4. **Per-provider pull** — generalize `_setup_lmstudio`/`_setup_ollama` to pull the catalog ref
   (`lms get <lmstudio_ref>` / `ollama pull <ollama_tag>`) instead of hardcoded nomic.
5. **gcode GGUF placement** — installer ensures the pinned `gguf_repo@gguf_revision/gguf_filename`
   is downloaded to `~/.gobby/models/` and **sha256-verified** (skip only if the present file's
   checksum matches) for gcode's in-process loader.
6. **Health check beyond dim (review fix #4)** — extend `_health_check_embedding` with a
   `_semantic_smoke_test`: embed a query + a related doc + an unrelated doc; assert
   `sim(query,related) > sim(query,unrelated)` by a margin **and** vector norm ≈ 1.0 (catches wrong
   EOS/pooling/normalization that still returns the right dim).
7. **`gobby embeddings switch <catalog_key>` — staged, two-phase, resumable (review fix #3, #6)** —
   extend existing `src/gobby/cli/embeddings.py`. NOT a single ACID transaction across 4 stores;
   instead a **resumable, idempotent state machine** doing a per-store **two-phase swap** so a
   mid-run failure never leaves a half-migrated *active* index. A **switch journal in ConfigStore**
   (`embedding_switch_runs`: `run_id, target profile, phase`) is **opened at the very start** and
   advances `staging → building → flipping → active`, so `--status/--resume/--abort` handle
   build-phase failures too, not just flip-phase:
   - **Phase 0 — Stage (no *active-config* or *alias* writes):** open journal `phase=staging`; pull
     provider model; download + sha256-verify GGUF; store install `api_key` to `SecretStore`;
     `_probe_embedding_dim`; semantic smoke test; **gcode capability gate** —
     `gcode embeddings capabilities --json` must report support for the target `catalog_key`,
     pooling, EOS handling, normalization, and vector rebuild. (These *are* writes — GGUF, secret,
     journal — but nothing **active** changes.) On failure nothing active changes; `--abort` cleans
     staged artifacts (journal run + any partial physical collections).
   - **Phase 1 — Build (old stays active):** set journal `phase=building`; build NEW versioned
     **physical** collections (`memories@<dim>-<rev>`, `tool_embeddings@…`, `skills@…`) + gcode's new
     vector projection, re-embedded and smoke-checked, while the OLD ones keep serving. **All
     build/rebuild operates on physical names** via `physical_name(kind, run_id)`; serving paths only
     ever resolve `active_alias(kind)`. **Never** call `ensure_collection(..., recreate_on_mismatch=True)`
     against an active alias during a switch — it could recreate/delete the wrong target; the resolver
     makes `active_alias(kind)` vs `physical_name(kind, run_id)` explicit so build code can't touch the live one.
   - **Phase 2 — Flip (crash-safe ordering):** using the journal opened in Phase 0. Order:
     (a) write **pending** target config + set `phase=flipping`; (b) atomically repoint each store's
     **active alias** to the new physical collection — **Qdrant `update_collection_aliases`** for
     memory/tools/skills via the resolver; gcode flips its analogous active pointer; (c) only after
     all aliases confirm, write **active** canonical
     `ai.embeddings.{model,catalog_key,dim,api_base,query_prefix}` to ConfigStore, regenerate
     `~/.gobby/embeddings.resolved.json`, set `phase=active`. This closes the
     aliases-flipped-but-config-stale window.
   - **Phase 3 — GC:** drop old physical collections after `phase=active` is confirmed.
   - **Recovery / reconciliation:** `embeddings switch --status` / `--resume` / `--abort`; idempotent
     re-runs resume from the journal phase. **Startup + `gobby doctor` read `embedding_switch_runs`:
     on `phase=flipping` they reconcile (verify aliases → finish the active-config write) or
     **refuse semantic search until reconciled** rather than serving with stale dim.** The existing
     startup `ensure_collection(recreate_on_mismatch=True)` path must defer to the journal — no
     auto-recreate of an alias target while a switch is mid-flight. Per-store success/failure report.
   - **Skills rebuild** is new: skills collection is fail-fast today; add the versioned build +
     `recreate_on_mismatch=True` reembed routine in
     `src/gobby/mcp_proxy/tools/skills/search_skills.py` (`_SkillIndexer`).

## Implementation — gcode side (`gobby-code` repo — specced, separate task + release dependency)

- Read the **resolved embedding fields** from `~/.gobby/embeddings.resolved.json` (checksum+version
  validated) — `catalog_key, dim, gguf_filename, gguf_sha256, pooling, append_eos, normalize,
  query_prefix` — instead of hardcoded nomic. No catalog reverse-mapping; the daemon emits resolved
  fields. (When gcode can read ConfigStore directly, the JSON cache can be retired.)
- `llama-cpp-2`: honor `pooling` (mean for nomic, last for Qwen3), append `<|endoftext|>` when
  `append_eos`, L2-normalize; apply the query instruction to **search queries only** (code symbols
  are documents), mirroring `_apply_prefix`.
- **Capability contract (fix #6):** add `gcode embeddings capabilities --json` reporting per
  `catalog_key`: supported keys, pooling modes, EOS handling, normalization, and vector-rebuild
  support + version. `embeddings switch` Phase 0 requires it before proceeding.
- `gcode vector rebuild` builds the new versioned projection at the new dim; old projection stays
  active until the daemon flips.

## Prerequisites (review fix #7)

- **Task #16289** (sort embedding responses by index) — land first or include. Bad vector↔item
  association would poison every reindex. Treat as a hard prerequisite of `embeddings switch`.

## Operational setup — Qwen3-Embedding-8B on this machine (post-approval)

1. `gobby install` → pick **Qwen3-8B + Ollama** (advanced: pick the `-f16` key for max quality on
   128GB; default key is `qwen3-8b-q8`). Install stages the profile and **drives `embeddings switch`
   automatically** — which pulls `qwen3-embedding:8b-q8_0`, downloads + sha256-verifies the gcode
   GGUF to `~/.gobby/models/`, builds new versioned memory/tools/skills (+ gcode once supported),
   then flips (writes canonical config @ `dim=4096`) and GCs the old 768-dim collections.
2. `gobby restart`; verify (memory/tool/skill search + `gcode search`).
   - Re-running later to change quant/size: `gobby embeddings switch <key>` directly.

## Verification

- **Unit** (`GOBBY_TEST_PROTECT=1 uv run pytest`): catalog integrity (quant-qualified keys,
  dims/prefixes/pinned-sha256/compatibility consistent); picker → `(model, catalog_key, dim,
  query_prefix, provider ref)` mapping + provider-aware quant gating (nomic Q4/Q8 hidden on Ollama);
  **install delegates to switch** (no canonical write before the flip); canonical config + resolved
  JSON written only at flip; `_apply_prefix` gives Qwen prefix on `is_query=True`, nothing on docs;
  semantic smoke test logic (related > unrelated, norm≈1); collection-name resolver / alias flip.
- **Reindex** (isolated test daemon, temp state/ports — never the user daemon): 768→4096 switch
  builds new versioned collections at 4096, flips, then GCs old; **a failure before the flip leaves
  the old index live and ConfigStore untouched**; `--resume` completes a partial run; no stale
  768-dim vectors remain after GC.
- **Crash-window** (the key one): kill the process **between alias-flip and active-config-write**
  (`phase=flipping`). Assert startup/`doctor` reconciles (finishes the active write) or **refuses
  semantic search until reconciled** — it must never serve a 768-dim config against 4096 collections;
  assert the startup `recreate_on_mismatch` path defers to the journal and doesn't recreate the alias target.
- **Manual E2E:** `gobby install` → Qwen3-8B (install auto-drives the switch); memory search +
  semantic tool search + skill search sane post-flip; `gcode search` after gcode capability lands.
- **gcode:** unit/integration in `gobby-code`.

## Out of scope / notes

- NV-Embed-v2 (dropped).
- No core embedding-code rewrite: `EmbeddingService`/`_fetch_embeddings`/`_apply_prefix` unchanged.
- New `.py` files stay < 1000 lines; split + file a refactor task if `embedding.py` /
  `_install_embedding_prompts.py` approach the limit.
- "Already exists" claims (`AI_EMBEDDING_QUERY_PREFIX_KEY` @ `embedding_keys.py:33`,
  `cli/embeddings.py`, `_persist_embedding_config`, task #16289) are taken from the owner's review;
  confirm at implementation (the code-index hook blocked direct re-verification this turn).
