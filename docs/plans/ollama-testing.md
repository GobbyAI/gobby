# Plan: Test Gobby's compatibility with Ollama for embeddings

## Context

**Why this work exists.** Josh is testing whether Gobby works correctly when its embedding provider is swapped from LM Studio (currently working baseline) to Ollama. The goal is end-to-end validation of the embedding plumbing under Ollama, not exact byte-compatibility of vectors. Ollama is already 95% wired in the codebase — `_PROVIDER_CONFIG["ollama"]` exists at `src/gobby/cli/installers/embedding.py:28-32`, `_setup_ollama()` exists at line 229, and the installer can already persist Ollama config. The actual "swap" is one CLI invocation plus a daemon restart.

**Hardware/environment context.** Josh is on M5 Max 128GB / 8TB disk. Ollama is already installed and running. `nomic-embed-text:latest` (f16, 274MB, v1.5, dim 768) is already pulled. LM Studio currently serves `text-embedding-nomic-embed-text-v1.5@q8_0` (Q8, dim 768) — same model family at lower precision. The two will produce embeddings in the same vector space at ~0.999 cosine equivalence, so existing Qdrant collections stay valid post-swap (no rebuild required).

**What the discovery phase changed about scope.**
- **Originally proposed:** add Nomic prefix gating (`"nomic" in model.lower()`).
  **Reality:** already implemented at `src/gobby/search/embeddings.py:104-120` with full test coverage at `tests/search/test_nomic_prefix.py`. **Dropped from scope.**
- **Still missing:** runtime dimension validation. Nothing checks that the embeddings the provider actually returns match `EmbeddingsConfig.dim` (default 768). A misconfigured `dim` produces a cryptic Qdrant error later, not a clear failure at embedding time. **In scope — small addition.**
- **Discovered separately (out of scope, file follow-up task):** LM Studio-specific eviction handling at `src/gobby/search/embeddings.py:277` checks for the literal string `"no models loaded"`, which is LM Studio's error message. Ollama uses a different error path on eviction. Ollama rarely evicts embedding models so this is a degraded-path issue, not a blocker. File a follow-up task; do not fix here.

---

## Approach

Bundle into one task with two phases: **(1) execute the swap and validate end-to-end**, then **(2) add the dim validation safety net** so the next provider/model swap surfaces config drift loudly instead of silently.

### Phase 1 — Execute the swap

1. **Confirm preconditions** (read-only):
   - `ollama list` shows `nomic-embed-text` (already done, confirmed by user).
   - Daemon is currently healthy: `uv run gobby status`.
   - Snapshot current embedding config from DB (via `gobby` CLI or direct `ConfigStore` read) so we can compare/rollback.

2. **Run the installer** to flip the provider and persist new config:
   ```bash
   uv run gobby install embedding --provider ollama
   ```
   This calls `install_embedding("ollama")` at `src/gobby/cli/installers/embedding.py:51`, which:
   - Calls `_setup_ollama()` (line 229) — verifies model is pulled (already done).
   - Calls `_health_check_embedding()` (line 325) — fires a real test embedding against Ollama.
   - Calls `_persist_embedding_config()` (line 274) — writes `embeddings.model="nomic-embed-text"`, `embeddings.api_base="http://localhost:11434/v1"`, `embeddings.dim=768` to `ConfigStore`.

3. **Restart the daemon** to pick up the new `EmbeddingsConfig`:
   ```bash
   uv run gobby restart
   ```

4. **Verify config landed** — `gobby status` or direct DB read confirms the new `embeddings.*` values.

### Phase 2 — Smoke test the three call sites

Each of the three embedding consumers must work end-to-end against the live Ollama endpoint. All existing tests are mocked at the HTTP layer, so this is the **first time** the runtime config gets exercised against a real Ollama server.

1. **Memory system** — `src/gobby/memory/manager.py:226` (`_embed_content_for_store`):
   - Create a memory via `mcp__gobby__call_tool` → `gobby-memory.create_memory` with distinctive content (e.g., "ollama compat test marker April 14 2026").
   - Issue a semantic search via `gobby-memory.search_memories` for a paraphrase of that content.
   - **Pass criterion:** the new memory ranks in the top results.

2. **Code index** — `src/gobby/code_index/sync_worker.py:53` (`_EmbedAdapter.embed`):
   - Trigger a code index sync (touch a file or run the sync worker manually) so a small batch of symbols gets embedded against Ollama.
   - Run a `gcode` semantic query for one of the touched symbols.
   - **Pass criterion:** the touched symbol appears in results.

3. **MCP semantic tool search** — `src/gobby/mcp_proxy/semantic_search.py:250` (`SemanticToolSearch.embed_text`):
   - Call `mcp__gobby__recommend_tools` with a natural-language query (e.g., "search the web").
   - **Pass criterion:** returns ranked tool suggestions, no exception.

If any of the three fails, **stop and diagnose before continuing.** Do not implement Phase 3 on a broken Phase 2.

### Phase 3 — Add dim validation

The only code change in this task. Surfaces config drift at the embedding boundary instead of letting it propagate silently to Qdrant.

**Where:** `src/gobby/search/embeddings.py`, inside `_fetch_embeddings()` (line 242), immediately after the successful `client.embeddings.create()` response is parsed (around line 267).

**Behavior:**
- After receiving the first response in a process, compare `len(embeddings[0])` against the configured `EmbeddingsConfig.dim`.
- If they match, no-op (fast path for steady state).
- If they mismatch, **log a hard warning once per process** (use a module-level `_dim_warned` set keyed on `(model, api_base, actual_dim, expected_dim)` to avoid log spam).
- Do **not** raise — the embedding is still valid in the model's natural dim, and raising would block the swap test from completing. The warning is the safety net; raising is too aggressive for a property that's recoverable by the user updating config.

**Wiring:** `_fetch_embeddings` doesn't currently know the expected dim. Two options:
- **Option A (preferred):** thread `expected_dim: int | None = None` through `generate_embeddings()` → `_fetch_embeddings()` and let callers pass it from `EmbeddingsConfig.dim`. Backward-compatible default of `None` skips the check.
- Option B: import `load_config()` inside `_fetch_embeddings` to look up `EmbeddingsConfig.dim`. Rejected because it couples the embedding layer to the config layer and breaks the existing pattern of pure parameter passing.

Use Option A.

**Caller updates** — only the call sites that actually have a `dim` to pass need updating:
- `src/gobby/code_index/sync_worker.py:29` — `_EmbedAdapter` already takes `EmbeddingsConfig` in its constructor; pass `cfg.dim` when calling `generate_embeddings`.
- `src/gobby/memory/manager.py:226` — passes embeddings via `MemoryManager`. Check if the manager has access to `EmbeddingsConfig`; if so, plumb it through. If not, leave as `None` for now (the path will silently skip the check, which is acceptable).
- `src/gobby/mcp_proxy/semantic_search.py:250` — same, pass if the class already has the config.

The dim validation is **opt-in per call site**. Call sites that don't pass `expected_dim` get the current behavior. This avoids forcing an architectural change just for a safety net.

**Tests** — extend `tests/search/test_nomic_prefix.py` (or create `tests/search/test_dim_validation.py`):
1. Test that a matching dim does not log a warning.
2. Test that a mismatched dim logs exactly one warning (and a second call does not log again — verify dedup).
3. Test that `expected_dim=None` skips the check entirely and never warns.
4. Test that the embedding result is still returned correctly when there's a mismatch (warning is non-fatal).

---

## Critical files

| File | Why |
| --- | --- |
| `src/gobby/search/embeddings.py` | **Modify.** Add `expected_dim` param to `generate_embeddings()` and `_fetch_embeddings()`. Add dim check + warning logic. Module-level `_dim_warned` set for dedup. |
| `src/gobby/code_index/sync_worker.py` | **Modify.** Pass `cfg.dim` as `expected_dim` in `_EmbedAdapter.embed()` (line ~53). |
| `src/gobby/memory/manager.py` | **Inspect, modify only if the path to `EmbeddingsConfig.dim` is short.** Otherwise leave as `None`. |
| `src/gobby/mcp_proxy/semantic_search.py` | **Inspect, modify only if the path to `EmbeddingsConfig.dim` is short.** Otherwise leave as `None`. |
| `src/gobby/cli/installers/embedding.py` | **Read-only.** Confirms `_setup_ollama()` + `_health_check_embedding()` semantics. No changes. |
| `tests/search/test_nomic_prefix.py` | **Read-only context.** Existing prefix tests already cover the gating behavior — no changes needed. |
| `tests/search/test_dim_validation.py` (new) **or** extend `test_nomic_prefix.py` | **Add.** Four tests for the dim validation path. |

## Reused functions / utilities

| Symbol | Where | How used |
| --- | --- | --- |
| `_PROVIDER_CONFIG["ollama"]` | `src/gobby/cli/installers/embedding.py:28` | Already exists. Source of truth for the swap target. |
| `_setup_ollama()` | `src/gobby/cli/installers/embedding.py:229` | Already exists. Verifies pull, no work needed. |
| `_health_check_embedding()` | `src/gobby/cli/installers/embedding.py:325` | Already exists. Validates the swap before persisting. |
| `_persist_embedding_config()` | `src/gobby/cli/installers/embedding.py:274` | Already exists. Writes the new `embeddings.*` config. |
| `_needs_nomic_prefix()` / `_apply_prefix()` | `src/gobby/search/embeddings.py:104-120` | **Already implemented** — the originally proposed prefix gating fix. No changes. |
| `EmbeddingsConfig` | `src/gobby/config/persistence.py:128` | Source of `dim` for the validation check. |
| `generate_embeddings()` / `generate_embedding()` | `src/gobby/search/embeddings.py:123, 313` | Add `expected_dim` parameter. |

---

## Verification

### Automated tests (must all pass)
```bash
uv run pytest tests/search/test_nomic_prefix.py -v
uv run pytest tests/search/test_dim_validation.py -v   # if created as new file
uv run pytest tests/cli/installers/test_embedding_installer.py -v
uv run pytest tests/search/ -v                         # broader sanity
```

### Type/lint
```bash
uv run ruff check src/gobby/search/embeddings.py src/gobby/code_index/sync_worker.py
uv run mypy src/gobby/search/embeddings.py
```

### End-to-end smoke (the actual user goal)
1. `gobby status` shows `embeddings.model=nomic-embed-text`, `embeddings.api_base=http://localhost:11434/v1`, `embeddings.dim=768`.
2. **Memory:** create + recall a distinctive memory — round-trips correctly.
3. **Code index:** sync + semantic query — returns expected symbol.
4. **MCP tool search:** `recommend_tools` returns ranked results, no exception.
5. **Daemon logs:** no errors related to embeddings during the smoke test window.
6. **Dim validation:** temporarily set `embeddings.dim` to a wrong value (e.g., 1024), trigger an embedding call, verify the warning fires exactly once. Reset to 768 afterward.

### Negative test for the safety net
- Wrong `dim` → warning logs once → embeddings still return → no crash.
- Restart daemon → next process cycle resets the warned set → warning fires once again. (Confirms per-process dedup, not global.)

---

## Out of scope (filed as separate follow-up tasks)

1. **LM Studio-specific eviction error handling** at `src/gobby/search/embeddings.py:277`. The literal `"no models loaded"` string check is LM Studio-specific. Ollama eviction produces a different error and would not trigger the reload path. Ollama rarely evicts embedding models, so this is a degraded-path concern, not a blocker. **Action:** file as a separate task with a clear repro (force Ollama to evict the embedding model and watch the failure mode).

2. **`is_embedding_available()` returns `True` for any local endpoint without pinging it** (`src/gobby/search/embeddings.py:377`). This is a known shortcut, separate concern.

3. **Choosing between LM Studio and Ollama as daily driver.** Josh will decide later after running both. Not part of this compatibility test — this task only validates that Ollama *works*, not that it's better.

---

## Risk register

| Risk | Likelihood | Mitigation |
| --- | --- | --- |
| Ollama eviction during a long-running task triggers the LM-Studio-only reload path → silent failure | Low (Ollama keeps embedding models loaded) | Out-of-scope follow-up task; not blocking. Watch logs during the smoke test for "no models loaded" — if it appears, we know the issue is real today. |
| Existing Qdrant collections written by LM Studio (Q8) drift in cosine space against new Ollama (f16) writes | Low | Walked through with user — same vector space, ~0.999 cosine equivalence, no rebuild needed. Documented above. |
| Misconfigured `embeddings.dim` post-swap | Mitigated by Phase 3 | The new dim validation is exactly the safety net for this. |
| Installer health check passes but live call from one of the three call sites still fails (e.g., context length, batch size) | Low | Phase 2 smoke test is specifically designed to catch this. If it happens, diagnose before Phase 3. |

---

## Commit strategy

One commit, one task. Title roughly: `[gobby-#XXXX] feat: validate embedding dim and verify Ollama compatibility`

Commit body should:
- Describe the swap test (what was validated end-to-end).
- Describe the dim validation addition (what it catches and why it's a warning, not a raise).
- Note the Nomic prefix gating was discovered to be already implemented — no change there.
- Reference the follow-up task for LM Studio-specific eviction handling.
