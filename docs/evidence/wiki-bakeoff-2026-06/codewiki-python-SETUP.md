# CodeWiki — Setup & Teardown (Python bake-off run)

Competitor: **CodeWiki** (FSoft-AI4Code) — https://github.com/FSoft-AI4Code/CodeWiki
Run date: 2026-06-14 (UTC)
Operator note: all secret values redacted as `***`. The LLM API key is read programmatically
from `/Users/josh/Projects/gobby/.env` (`OPENAI_API_KEY`, mode 600) and never printed.

---

## 0. Source currency (fairness)

The local clone at `/Users/josh/Projects/wiki-bakeoff/CodeWiki` was pinned at an old commit
`972dba9` ("Add module clustering diagnostics"). It was updated to upstream HEAD:

```bash
cd /Users/josh/Projects/wiki-bakeoff/CodeWiki
git fetch origin
git checkout origin/main      # -> 06e6d01 (Merge PR #64, 2026-06-11), detached HEAD
```

Commits gained by the update (`972dba9..06e6d01`):

| Commit  | Summary |
|---------|---------|
| 06e6d01 | Merge PR #64 from improv/c-cpp-java-parser |
| ef59c23 | Improve dependency analyzer symbol resolution |
| 9284774 | Improve C/C++ and Java call graph resolution |

Files touched are **exclusively C/C++/Java parser/analyzer code** (`dependency_analyzer/analyzers/c.py`,
`cpp.py`, `java.py`, `call_graph_analyzer.py`, `external_symbols.py`, `patterns.py`, `models/core.py`).
**No Python-analyzer impact** — the Python pipeline used for this run is byte-identical to the old pin.
We still build from latest HEAD for currency.

---

## 1. Build the Docker image

CodeWiki ships a Dockerfile at `docker/Dockerfile` (Python 3.12-slim base; installs git, curl,
nodejs/npm for mermaid validation; pip-installs `requirements.txt`; copies the `codewiki` package).

```bash
cd /Users/josh/Projects/wiki-bakeoff/CodeWiki
docker build -f docker/Dockerfile -t codewiki:latest-bakeoff .
```

- Built image: **`codewiki:latest-bakeoff`** (id `643bdb3fb5ad`, 3.26 GB).
- Prior image `codewiki:0.0.1` (id `150153e71770`) is left in place, unused by this run.
- The pip/apt layers were cache hits (requirements unchanged from the old pin).

The image's default `CMD` launches a web app on :8000; for this run we override the entrypoint
to invoke the **CLI** (`python -m codewiki ...`) directly — no web server, no port published.

---

## 2. Target subsystem (the mini-repo we feed CodeWiki)

CodeWiki's validator allowlist excludes `.rs`, so Rust is out. We give it a Python target:
EXACTLY `src/gobby/agents` from the gobby daemon repo — a coherent agent-orchestration subsystem
of 65 `.py` files. Extracted as a standalone, read-only mini-repo:

```bash
mkdir -p /Users/josh/Projects/wiki-bakeoff/CodeWiki/gobby-agents-src
git -C /Users/josh/Projects/gobby archive --format=tar HEAD src/gobby/agents \
  | tar -x -C /Users/josh/Projects/wiki-bakeoff/CodeWiki/gobby-agents-src
# -> 65 .py files under gobby-agents-src/src/gobby/agents/
```

gobby source commit: `accc44ff9`. File count verified: **65 `.py`** (matches the whole tracked subtree).

Note: cross-package imports to `gobby.*` symbols OUTSIDE this subtree are unresolved by design
(we mounted only the subtree). CodeWiki tolerates this — analysis reported 0 failed files.

---

## 3. Configure CodeWiki (ONLY url / key / model — CodeWiki's own defaults for everything else)

CodeWiki stores config in `~/.codewiki/{config.json,credentials.json}`. In a headless container the
system keychain is unavailable, so it auto-falls back to an encrypted file; we also set
`CODEWIKI_NO_KEYRING=1`. We mount a host dir as `/root/.codewiki` so config persists across the
`config set` container and the `generate` container.

```bash
mkdir -p /Users/josh/Projects/wiki-bakeoff/CodeWiki/cw-home

# Key loaded into a shell var programmatically; never echoed.
OPENAI_API_KEY="$(grep '^OPENAI_API_KEY=' /Users/josh/Projects/gobby/.env | cut -d= -f2-)"

docker run --rm \
  -e CODEWIKI_NO_KEYRING=1 \
  -e "CW_KEY=$OPENAI_API_KEY" \
  -v /Users/josh/Projects/wiki-bakeoff/CodeWiki/cw-home:/root/.codewiki \
  --entrypoint bash codewiki:latest-bakeoff -c '
    python -m codewiki config set \
      --provider openai-compatible \
      --api-key "$CW_KEY" \
      --base-url http://host.docker.internal:1234/v1 \
      --main-model    google/gemma-4-26b-a4b-qat \
      --cluster-model google/gemma-4-26b-a4b-qat \
      --fallback-model google/gemma-4-26b-a4b-qat'
```

Final config (`config show`, key redacted):

```
Provider:        openai-compatible
Main Model:      google/gemma-4-26b-a4b-qat
Cluster Model:   google/gemma-4-26b-a4b-qat
Fallback Model:  google/gemma-4-26b-a4b-qat
Base URL:        http://host.docker.internal:1234/v1
API Key:         ***   (encrypted file at /root/.codewiki/credentials.json)
Max Tokens:            32768   (CodeWiki default — NOT tuned)
Max Token/Module:      36369   (CodeWiki default — NOT tuned)
Max Token/Leaf Module: 16000   (CodeWiki default — NOT tuned)
Max Depth:             2       (CodeWiki default — NOT tuned)
```

LM Studio runs on the host at port 1234, OpenAI-compatible API, **Bearer auth required**
(401 without). The `/v1` suffix is included in the base URL. The model
`google/gemma-4-26b-a4b-qat` is loaded in LM Studio (verified present in `/v1/models`).

Connectivity validated before generation:

```bash
docker run --rm -e CODEWIKI_NO_KEYRING=1 \
  -v /Users/josh/Projects/wiki-bakeoff/CodeWiki/cw-home:/root/.codewiki \
  --add-host=host.docker.internal:host-gateway \
  --entrypoint python codewiki:latest-bakeoff -m codewiki config validate
# -> "✓ API connectivity test successful  ✓ Configuration is valid!"
```

---

## 4. Generate documentation

`codewiki generate` documents the container's **current working directory** (`Path.cwd()`).
We mount the mini-repo read-only as `/repo`, set workdir to `/repo`, and write docs to a separate
writable host mount `/out`. No `--create-branch` (that flag would require git; default path does not).
No model/depth/token flags — CodeWiki defaults only.

```bash
docker run -d --name codewiki-bakeoff-run \
  -e CODEWIKI_NO_KEYRING=1 -e PYTHONUNBUFFERED=1 \
  --add-host=host.docker.internal:host-gateway \
  -v /Users/josh/Projects/wiki-bakeoff/CodeWiki/cw-home:/root/.codewiki \
  -v /Users/josh/Projects/wiki-bakeoff/CodeWiki/gobby-agents-src:/repo:ro \
  -v /Users/josh/Projects/wiki-bakeoff/outputs/CodeWiki-python/docs:/out \
  -w /repo \
  --entrypoint python codewiki:latest-bakeoff \
  -m codewiki generate -o /out --verbose
```

CodeWiki's 5-phase pipeline: (1) Dependency Analysis, (2) Module Clustering (LLM),
(3..5) hierarchical doc generation / writing. See `RUN_LOG.txt` for the live console + timings.

The Docker `HEALTHCHECK` probes :8000 and therefore marks the CLI container "unhealthy" — this is
cosmetic (no web server runs in CLI mode) and does not affect generation.

### Outcome (FAILED — exit code 4)

| Phase | Result | Internal time |
|-------|--------|---------------|
| 1. Dependency Analysis | OK — 65/65 files analyzed, 0 failed, 334 functions, 2596 relationships, 67 leaf nodes | ~0.2 s |
| 2. Module Clustering (LLM) | Returned **0 modules** (empty module tree) | 349.1 s (~5.8 min) |
| 3. Documentation Generation | **FAILED** — `openai.APITimeoutError: Request timed out` | ~30 min (two 600 s timeouts) |

- Wall clock: container started ~16:08:55Z, exited 16:44:55Z (~36 min total run).
- Phase 3 ran from ~16:15Z to 16:44:55Z (~30 min): main-model request hit the 600 s (10-min) SDK
  default timeout, the `FallbackModel` retried the fallback model which hit another 600 s timeout,
  then the chain was exhausted (plus connection/overhead between attempts).
- Because clustering produced 0 modules, CodeWiki logged "Processing whole repo because repo can
  fit in the context window" and issued a **single** doc-generation request for the entire 52K-token repo.
- That request exceeded the **OpenAI SDK default 600 s (10-min) timeout** (CodeWiki's
  `create_main_model()` in `src/be/llm_services.py` builds `OpenAIProvider(...)` with no `timeout`/`http_client`).
  The slow local gemma-4-26b could not emit the full `max_tokens=32768` response within 600 s.
- pydantic-ai's `FallbackModel` then retried the fallback model (same id, same slow endpoint), which
  also timed out — exhausting the chain. Process raised `APIError` and exited **4**.
- **CodeWiki exposes no flag to raise the LLM request timeout** (only `--base-url/--api-key/--*-model/
  --max-tokens/--max-token-per-*/--max-depth`). Per the fairness rule we configured only url/key/model
  and used CodeWiki's own defaults, so this default-timeout failure is the genuine result.

### Artifacts produced before failure

```
docs/temp/dependency_graphs/repo_dependency_graph.json   826 KB  (Phase 1 — 334 symbol nodes)
docs/module_tree.json                                    2 B  ({} — empty, 0 modules)
docs/first_module_tree.json                              2 B  ({} — empty)
```

**0 `.md` documentation pages were written** (Phase 3 never returned).

The dependency graph is CodeWiki's grounding/citation substrate: each node carries
`file_path`, `relative_path`, `start_line`, `end_line`, `source_code`, `docstring`, `depends_on`,
`component_type`, etc. (e.g. `src/gobby/agents/checkpoint_manager.py::CheckpointManager`, lines 25–156).
Citations would have been file+line-range grounded — but no doc pages were emitted to carry them.

---

## 5. Docker inventory

**Images (left in place after the run):**

| Image | ID | Size | Note |
|-------|----|----|------|
| `codewiki:latest-bakeoff` | 643bdb3fb5ad | 3.26 GB | built for this run (HEAD 06e6d01) |
| `codewiki:0.0.1`          | 150153e71770 | 3.26 GB | pre-existing, unused |
| `python:3.12-slim`        | d764629ce0dd | 205 MB  | base layer |

**Containers started by this run:** `codewiki-bakeoff-run` (one-off; removed in teardown).
Two earlier `--rm` containers (`config set`, `config validate`) auto-removed on exit.

**Daemon containers — NOT touched:** `gobby-postgres`, `gobby-postgres-test-1`,
`services-falkordb-1`, `services-qdrant-1` (all still Up/healthy).

---

## 6. Teardown

```bash
# Remove the generation container (it is not --rm because we ran it detached)
docker rm -f codewiki-bakeoff-run

# Images are intentionally LEFT in place (codewiki:latest-bakeoff, codewiki:0.0.1, python:3.12-slim).
# Daemon containers are NOT touched.
```

Scratch dirs kept on host for reproducibility (not committed):
`/Users/josh/Projects/wiki-bakeoff/CodeWiki/gobby-agents-src` (mini-repo source),
`/Users/josh/Projects/wiki-bakeoff/CodeWiki/cw-home` (CodeWiki config, contains encrypted key file).
### Teardown performed (verified)

- `docker rm -f codewiki-bakeoff-run` → removed. No CodeWiki containers remain (`docker ps -a` clean).
- The two `--rm` helper containers (`config set`, `config validate`) auto-removed on exit.
- Images LEFT in place: `codewiki:latest-bakeoff` (643bdb3fb5ad), `codewiki:0.0.1` (150153e71770), `python:3.12-slim` (d764629ce0dd).
- Daemon containers confirmed untouched and still healthy: `gobby-postgres`, `gobby-postgres-test-1`,
  `services-falkordb-1`, `services-qdrant-1`.

---

## Patched re-run (1800s timeout)

Run date: 2026-06-14. Purpose: isolate CodeWiki's doc-generation behaviour from the
default 600s LLM-timeout failure documented above, by raising ONLY the LLM request
timeout to 1800s so the slow local gemma has room to finish. No other setting changed
(max_tokens, temperature, depth, prompts, chunking, models, URL all identical to the
default-timeout run). `SCORECARD.md` is preserved unchanged — it records the
default-timeout finding.

### The patch (the only change)

- **File:** `codewiki/src/be/llm_services.py` (in the throwaway clone at
  `/Users/josh/Projects/wiki-bakeoff/CodeWiki`; not a tracked Gobby project).
- **Functions:** `create_main_model()` and `create_fallback_model()` — the model
  constructors used by the documentation agent. Call chain:
  `create_fallback_models()` → `create_main_model()` + `create_fallback_model()`,
  consumed by `be/agent_tools/generate_sub_module_documentations.py` and
  `be/pydantic_ai_backend.py`. These are exactly the constructors whose
  `OpenAIProvider(base_url, api_key)` (no timeout) blew the 600s default in the prior
  run. The clustering path (`call_llm()` → `create_openai_client()`) was **not**
  touched — it already completed at the default in ~208-349s.
- **Mechanism:** inject an `httpx.AsyncClient(timeout=1800.0)` into the provider via
  the SDK's `http_client` argument. Confirmed live inside the container that the
  resulting client reports `Timeout(timeout=1800.0)`.

Exact diff (`pydantic_ai` 1.0.6, `openai` 1.107.1, `httpx` 0.28.1 in the image):

```diff
 import logging
+import httpx
 from openai.types import chat
@@ def create_main_model(config: Config) -> CompatibleOpenAIModel:
     """Create the main LLM model from configuration."""
+    # BAKE-OFF NON-SHIPPING PATCH (2026-06-14): raised LLM timeout 600s->1800s so a slow local model can finish; NOT a CodeWiki default.
     return CompatibleOpenAIModel(
         model_name=config.main_model,
         provider=OpenAIProvider(
             base_url=config.llm_base_url,
-            api_key=config.llm_api_key
+            api_key=config.llm_api_key,
+            http_client=httpx.AsyncClient(timeout=1800.0)
         ),
         settings=_build_model_settings(config, config.main_model)
     )
@@ def create_fallback_model(config: Config) -> CompatibleOpenAIModel:
     """Create the fallback LLM model from configuration."""
+    # BAKE-OFF NON-SHIPPING PATCH (2026-06-14): raised LLM timeout 600s->1800s so a slow local model can finish; NOT a CodeWiki default.
     return CompatibleOpenAIModel(
         model_name=config.fallback_model,
         provider=OpenAIProvider(
             base_url=config.llm_base_url,
-            api_key=config.llm_api_key
+            api_key=config.llm_api_key,
+            http_client=httpx.AsyncClient(timeout=1800.0)
         ),
         settings=_build_model_settings(config, config.fallback_model)
     )
```

### How the patch was made active (no image rebuild)

The patched host file is bind-mounted read-only over the installed package path inside
the existing `codewiki:latest-bakeoff` image — cleaner/faster than a 3.26 GB rebuild and
guarantees the patch is the live code:

```bash
-v /Users/josh/Projects/wiki-bakeoff/CodeWiki/codewiki/src/be/llm_services.py:\
   /app/codewiki/src/be/llm_services.py:ro
```

Verified before the run:
```bash
docker run --rm -v <patched file>:/app/codewiki/src/be/llm_services.py:ro \
  --entrypoint python codewiki:latest-bakeoff -c \
  "import inspect, codewiki.src.be.llm_services as m; \
   print('http_client 1800 in main:', 'timeout=1800.0' in inspect.getsource(m.create_main_model)); \
   import httpx; from pydantic_ai.providers.openai import OpenAIProvider; \
   p=OpenAIProvider(base_url='http://x/v1', api_key='k', http_client=httpx.AsyncClient(timeout=1800.0)); \
   print(p.client._client.timeout)"
# -> http_client 1800 in main: True ;  Timeout(timeout=1800.0)
```

### Run command and outcome

See `RUN_LOG_patched.txt` for the full console, LM-Studio-log diagnostics, wall-clock
and LLM-call counts. Same `generate -o /out --verbose` invocation as the default run,
plus the read-only patched-file mount; container name `codewiki-bakeoff-patched`,
`--restart=no`.

