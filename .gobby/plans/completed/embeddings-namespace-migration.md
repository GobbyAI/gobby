# Embeddings Namespace Migration (daemon) — `embeddings.* → ai.embeddings.*`

**Plan ID:** embeddings-namespace-migration

## O1: Overview
`kind: framing`

The daemon half of a **cross-repo P0** (same Plan ID, gobby-cli side in `~/Projects/gobby-cli`): migrate the daemon's
embedding configuration from `embeddings.*` to `ai.embeddings.*` via **expand → migrate → contract**, owning the one-time
`config_store` rewrite and the runtime config-model change so no install suffers a silent embeddings outage. This epic is
**self-contained** — it does **not** gate on a "D6" in `gwiki-daemon-web.md` (no such section exists); the gobby-cli
`gwiki-multimodal-ai` epic's §6.1 D6 only *documents* this contract CLI-side.

Verified live: `config_store` holds the OLD namespace only — `embeddings.{api_base, model, dim, api_key}` — and
`ai.embeddings.*` does not exist yet; `dim` is under `embeddings.dim` (768). `## M1` is intentionally omitted (emitted by
adversarial expansion on approval).

## C1: Contract, constraints & verified surface
`kind: framing`

- **Config sources — NO ENV VARS (product decision)**: the daemon does **not** add a `GOBBY_AI_EMBEDDINGS_*` env overlay;
  embedding config is read from `config_store` only; secret values resolve via `$secret:` (SecretStore), not `${VAR}` env
  expansion. This is the intended end state, not an omission.
- **Key set**: `ai.embeddings.{api_base, model, api_key, query_prefix, provider, dim}`. Dimension stays on
  `ai.embeddings.dim` (already the daemon's field name); gcode converges onto it. **Old dim keys are repo-specific**: the
  daemon's old dim key is `embeddings.dim` (gcode's is `embeddings.vector_dim`), and the P1 dual-read maps **this repo's**
  old key (`embeddings.dim`) into canonical `ai.embeddings.dim`.
- **api_key is `is_secret`, never plaintext** (a separate upstream bug-fix guarantees secret storage — assume it; add no
  plaintext branch). End state: `ai.embeddings.api_key = $secret:embeddings_api_key`, `is_secret=true`. The
  embeddings-namespaced secret is `embeddings_api_key`; for legacy installs that shared `openai_api_key`, **copy-not-move**
  (never orphan the LLM key).
- **Migration ownership**: only the daemon rewrites `config_store`; the CLIs never do.
- **Canonical-read sequencing**: P1 dual-reads preferring `embeddings.*` → `ai.embeddings.*` (unchanged behavior) and
  dual-writes both; P2 runs the migration then flips canonical to prefer `ai.embeddings.*`; P3 drops dual-write + the old
  fallback.
- **CI guard (allowlisted)**: all embedding key names in one constants module; a CI test rejects literal
  `embeddings.`/`ai.embeddings.` strings outside it + the migration code + tests; tightened at P3.
- **Blocking decision #1 — runtime config model**: `DaemonConfig` has a top-level `embeddings` model and DB keys are
  blindly unflattened (`src/gobby/config/app.py:341` and `:802`), so `ai.embeddings.dim` would land under an unknown `ai`
  object and be dropped. **Decision (recommended): normalize `ai.embeddings.*` onto the existing runtime
  `config.embeddings` at load** (map the `ai.embeddings` subtree onto the current model) so readers stay unchanged; the
  load-time normalization also implements the dual-read.
- **Blocking decision #2 — api_key custom write path**: the OpenAI installer stores the key as SecretStore `openai_api_key`
  (`src/gobby/cli/installers/embedding.py:336`); `ConfigStore.set_many` cannot preserve `is_secret`
  (`src/gobby/storage/config_store.py:93`) and `set_secret("ai.embeddings.api_key")` derives the **wrong** secret name
  `api_key` (`src/gobby/storage/config_store.py:149`). A **custom** write/migration path must ensure the
  `embeddings_api_key` secret exists (copy from `openai_api_key` for legacy shared installs), write
  `ai.embeddings.api_key = $secret:embeddings_api_key` with `is_secret=true` preserved, and never write a plaintext value.
- **Reader inventory to migrate** (all read embedding config; centralized + dual-read in P1): `EmbeddingsConfig`
  (`src/gobby/config/persistence.py`), `src/gobby/servers/http.py`, `src/gobby/code_index/sync_worker.py`,
  `src/gobby/ai/registry.py` (embed capability), `src/gobby/memory/vectorstore.py`,
  `src/gobby/memory/knowledge_graph/code_linker.py`, `src/gobby/cli/memory/indices.py`, `src/gobby/utils/deps.py`,
  `src/gobby/runner_init/storage.py`, `src/gobby/search/models.py`, `src/gobby/mcp_proxy/semantic_search.py` (its
  dimension-mismatch guard is resolved here), and the settings-UI config-write route
  (`src/gobby/servers/configuration_values.py`). (`configuration_ui_settings.py` persists only `ui_settings.*`, never
  embedding config, so it is **not** in scope; a future settings form matters here only if it writes embedding keys
  through `/config/values`.)
- **External constraint**: standalone-hub classifier/adoption (`gwiki_*` recognition, additive in-place upgrade) is owned
  by `gwiki-daemon-web.md`'s D5 — referenced as the migration's wiring point, not re-planned here.

## R1: Phase order
`kind: framing`

`P1 (Expand)` → `P2 (Migrate)` → `P3 (Contract)`. P1 (dual-read/dual-write + the pre-built migration) is non-breaking and
ships anytime; P2 runs the migration on upgrade and flips canonical; P3 is the no-alias cut.

## P1: Expand
`kind: framing`

**Goal**: add `ai.embeddings.*` as a non-breaking dual-read/dual-write, fix the runtime config model so the new keys are
honored, build (but do not yet run) the migration, and ship the doctor.

### 1.1 Centralize embedding key constants + allowlisted CI guard [category: code]
`kind: deliverable`

Target: `src/gobby/config/persistence.py`

Move every embedding key string into one constants module (alongside `EmbeddingsConfig`) and add an allowlisted CI test
rejecting literal `embeddings.`/`ai.embeddings.` strings outside that module + the migration code + their tests.

**Acceptance:**

- 1.1.1 - All embedding key names resolve from one constants module; the CI guard fails on a stray literal added elsewhere.
  test: `src/gobby/config/persistence.py::tests::test_embedding_keys_centralized_and_guarded`.

### 1.2 Normalize `ai.embeddings.*` onto the runtime config model at load [category: code]
`kind: deliverable`

Target: `src/gobby/config/app.py`

Resolve blocking decision #1: at load, map the `ai.embeddings` subtree onto the existing runtime `config.embeddings`
model (so readers are unchanged), preferring `embeddings.*` → `ai.embeddings.*` for the dual-read window. For the
dimension specifically, the daemon's old key is `embeddings.dim` (preferred during P1) falling back to canonical
`ai.embeddings.dim`; both populate `config.embeddings.dim`.

**Acceptance:**

- 1.2.1 - A `config_store` row `ai.embeddings.dim` is honored (populates `config.embeddings.dim`) instead of being dropped
  under an unknown `ai` object; `embeddings.*` still takes precedence during P1. test:
  `src/gobby/config/app.py::tests::test_ai_embeddings_normalized_at_load`.

### 1.3 Dual-write both namespaces + custom api_key secret path [category: code]
`kind: deliverable`

Target: `src/gobby/cli/installers/embedding.py`, `src/gobby/storage/config_store.py`, `src/gobby/servers/configuration_values.py`

Resolve blocking decision #2: the installer and the settings-UI config-write route (`configuration_values.py`) write
**both** namespaces during the window;
the api_key uses the custom path — ensure the `embeddings_api_key` secret exists (copy from `openai_api_key` for legacy
shared installs), write `ai.embeddings.api_key = $secret:embeddings_api_key` with `is_secret` preserved, never plaintext.

**Acceptance:**

- 1.3.1 - Writing embedding config persists both `embeddings.*` and `ai.embeddings.*`; the api_key is written as a secret
  reference with `is_secret=true` and no plaintext value, via the custom path (not `set_many`/`set_secret`). test:
  `src/gobby/cli/installers/embedding.py::tests::test_dual_write_and_secret_api_key`.

### 1.4 Pre-build the idempotent `config_store` migration [category: code]
`kind: deliverable`

Target: `src/gobby/storage/migrations/embeddings_namespace.py`

Build and unit-test (but do not yet run) the one-time, idempotent migration: rename `embeddings.* → ai.embeddings.*`
preserving values and the `is_secret` flag, with the api_key row renamed to `$secret:embeddings_api_key`. Wire it into the
hub install/upgrade + standalone-adoption (D5) path.

**Acceptance:**

- 1.4.1 - The migration is idempotent and preserves values + `is_secret`; re-running is a no-op; the api_key row points at
  `$secret:embeddings_api_key`. test: `src/gobby/storage/migrations/embeddings_namespace.py::tests::test_migration_idempotent_preserves_secret`.

### 1.5 `gobby embeddings doctor` [category: code]
`kind: deliverable`

Target: `src/gobby/cli/embeddings.py`

A read-only check emitting the shared-contract JSON (`endpoint, model, dim, api_key_present, api_key_fingerprint,
namespace_resolved, source, agrees, drift`) — `api_key_fingerprint` is the api_key's `sha256[:8]` redaction
(`string | null`, null when no key is present). **Exit-code contract** (shared verbatim with the gcode doctor): `0` =
healthy (config resolved and self-consistent; `agrees=true`, or `agrees=null` when no peer is reachable); `10` = config not
resolved (`namespace_resolved=null`); `11` = drift (`agrees=false` — daemon and gcode disagree on ≥1 of
endpoint/model/dim); `20` = probe/transport failure (couldn't reach the embedding endpoint or peer to verify). **`drift`
shape**: `null` when `agrees ∈ {true, null}`; otherwise an array of `{ field, self, peer }` objects, one per differing
field (`field ∈ {"endpoint","model","dim"}`; `self` = this tool's resolved value, `peer` = the other side's; values
`string|number|null`).

**Acceptance:**

- 1.5.1 - `gobby embeddings doctor` emits the contract JSON with the api_key redacted and the documented exit codes. test:
  `src/gobby/cli/embeddings.py::tests::test_doctor_json_and_exit_codes`.

## P2: Migrate
`kind: framing`

**Goal**: run the migration on upgrade so every install carries `ai.embeddings.*`, then flip the canonical read.

### 2.1 Run the migration on upgrade and flip canonical [category: code]
`kind: deliverable`

Target: `src/gobby/config/app.py`, `src/gobby/storage/migrations/embeddings_namespace.py`

Execute the migration in the install/upgrade path so every reachable install has `ai.embeddings.*` rows, then flip the
load-time normalization to prefer `ai.embeddings.*` (old still fallback). The doctor confirms daemon + gcode agree.

**Acceptance:**

- 2.1.1 - After upgrade, every install resolves embedding config from `ai.embeddings.*`; the doctor reports
  `namespace_resolved = "ai.embeddings"` and `agrees=true`. test:
  `src/gobby/config/app.py::tests::test_upgrade_migrates_and_flips`.

## P3: Contract
`kind: framing`

**Goal**: the no-alias cut — drop dual-write and the old fallback.

### 3.1 Drop dual-write + old-key fallback; `ai.embeddings.*` only [category: code]
`kind: deliverable`

Target: `src/gobby/cli/installers/embedding.py`, `src/gobby/config/app.py`

Remove dual-write and the `embeddings.*` fallback; read/write only `ai.embeddings.*`; retire the migration past the install
baseline; tighten the CI allowlist.

**Acceptance:**

- 3.1.1 - Only `ai.embeddings.*` is read/written; `embeddings.*` is no longer honored or emitted, and the CI guard rejects
  any old-namespace literal. test: `src/gobby/config/app.py::tests::test_ai_embeddings_only`.

## VS1: Verification
`kind: verification`

The daemon half succeeds when: `ai.embeddings.*` is honored at runtime (decision #1) and the api_key is always an
`is_secret` reference, never plaintext (decision #2); P1 is non-breaking (dual-read old-canonical + dual-write) and the
migration is pre-built/idempotent; P2 runs the migration on upgrade so every install carries `ai.embeddings.*` and flips
canonical; `gobby embeddings doctor` agrees with gcode and redacts the key; and at P3 only `ai.embeddings.*` is read/written
with the CI guard enforcing it. The gobby-cli half (same Plan ID) cuts independently once this epic's Migrate has populated
the rows.
