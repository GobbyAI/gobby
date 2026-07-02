# DeepWiki-Open — Wiki Bake-off Setup (gobby-cli, local LM Studio)

This document records the exact setup used to generate a DeepWiki-Open wiki for the
local `gobby-cli` repo using a local LM Studio model, plus teardown commands.
All secrets are redacted as `***`.

## Summary

- Tool: DeepWiki-Open at `/Users/josh/Projects/wiki-bakeoff/deepwiki-open` (benchmark commit `43ed8a2`).
- Model endpoint: LM Studio on host port 1234 (OpenAI-compatible), reached from the
  container via `http://host.docker.internal:1234/v1`.
  - Chat model: `google/gemma-4-26b-a4b-qat` (configured as a custom model under DeepWiki's `openai` provider).
  - Embedding model: `text-embedding-nomic-embed-text-v1.5` (native 768-dim; LM Studio ignores the `dimensions` request param).
- Target repo: `gobby-cli`, fed as a LOCAL repo (read-only mount), NOT cloned from GitHub.
- Transport: DeepWiki's own backend HTTP streaming endpoint `/chat/completions/stream`,
  driven headlessly with DeepWiki's own frontend prompt templates (see "Generation method").

## Fairness note

Only four things were pointed at the model: API base URL, API key, chat model id,
embedding model id. All other DeepWiki settings (chunking 350-word/100 overlap,
retriever top_k=20, comprehensive 8-12 page view default, temperature/top_p, etc.)
were left at DeepWiki's own defaults. The `dimensions` field in `embedder.json` was
set to 768 to match the nomic model's native output (the `.bak` placeholder was 256
for a different model); LM Studio ignores it regardless, so vectors are 768-dim either way.

## How the local repo was ingested

DeepWiki's backend treats any `repo_url` NOT starting with `http://`/`https://` as a
local filesystem path (`api/data_pipeline.py` `_create_repo`, ~L799-814): it sets the
repo dir directly to that path and does NOT clone. `gobby-cli` IS a public GitHub repo
(`https://github.com/GobbyAI/gobby-cli.git`), but to wiki the LOCAL working state
faithfully (branch `dev`, HEAD `ea6a26c`) and to avoid DeepWiki's `glob('**/*')`
walking the 209 GB `target/` build dir, the repo's tracked files at HEAD were exported
with `git archive` into `./gobby-cli-src` (19 MB, 1030 files, 421 `.rs`) and mounted
READ-ONLY into the container at `/repos/gobby-cli`. The driver passes
`repo_url=/repos/gobby-cli`, `type=local`.

## Exact commands run

```bash
# 0. Verify LM Studio reachable + models present (host)
curl -s http://localhost:1234/v1/models -H "Authorization: Bearer ***"   # 200, lists gemma + nomic

# 1. Outputs dir
mkdir -p /Users/josh/Projects/wiki-bakeoff/outputs/deepwiki-open

# 2. Clean read-only export of gobby-cli HEAD (no target/, no .git)
SRC=/Users/josh/Projects/gobby-cli
DST=/Users/josh/Projects/wiki-bakeoff/deepwiki-open/gobby-cli-src
mkdir -p "$DST"
git -C "$SRC" archive --format=tar HEAD | tar -x -C "$DST"

# 3. Embedder config -> OpenAI-compatible variant w/ nomic model id (see file below)
#    (api/config/embedder.json replaced; original preserved at api/config/embedder.json.bak)

# 4. .env (mode 600) with secret read programmatically from /Users/josh/Projects/gobby/.env
#    OPENAI_API_KEY (from source .env), OPENAI_BASE_URL, DEEPWIKI_EMBEDDER_TYPE=openai

# 5. docker-compose.yml: add extra_hosts host.docker.internal + read-only gobby-cli mount

# 6. Build + start
cd /Users/josh/Projects/wiki-bakeoff/deepwiki-open
docker compose build
docker compose up -d

# 7. Verify
curl -s http://localhost:8001/health                                  # healthy
docker compose exec deepwiki sh -c 'curl -s http://host.docker.internal:1234/v1/models -H "Authorization: Bearer $OPENAI_API_KEY" -o /dev/null -w "%{http_code}"'   # 200

# 8. Headless generation (driver replicates frontend orchestration; runs INSIDE container)
docker cp driver.py deepwiki-open-deepwiki-1:/tmp/driver.py
docker exec deepwiki-open-deepwiki-1 sh -c 'P=$(command -v python3); mkdir -p /out && "$P" -u /tmp/driver.py 2>&1 | tee /out/driver_run.log'

# 9. Copy artifacts out
docker cp deepwiki-open-deepwiki-1:/out/. /Users/josh/Projects/wiki-bakeoff/outputs/deepwiki-open/
```

## Generation method (why a driver script)

DeepWiki has NO single "generate whole wiki" backend endpoint — the Next.js frontend
orchestrates generation page-by-page. To run headlessly, `driver.py` replicates that
orchestration against the backend, using DeepWiki's OWN prompt templates copied verbatim
from `src/app/[owner]/[repo]/page.tsx` (`determineWikiStructure` and `generatePageContent`):

1. `GET /local_repo/structure?path=/repos/gobby-cli` -> file tree + README.
2. POST structure prompt to `/chat/completions/stream` -> parse `<wiki_structure>` XML -> page list.
3. For each page, POST the page-content prompt -> collect streamed Markdown.
4. Persist per-page `.md`, combined `.md`, and `wiki_full.json`.

The driver lives at `/Users/josh/Projects/wiki-bakeoff/deepwiki-open/driver.py`. It uses
Python stdlib only (urllib/json/re/xml) and runs inside the container (which has the venv).

## Final config files (secrets redacted)

### api/config/embedder.json (replaced; original at api/config/embedder.json.bak)

```json
{
  "embedder": {
    "client_class": "OpenAIClient",
    "initialize_kwargs": {
      "api_key": "${OPENAI_API_KEY}",
      "base_url": "${OPENAI_BASE_URL}"
    },
    "batch_size": 10,
    "model_kwargs": {
      "model": "text-embedding-nomic-embed-text-v1.5",
      "dimensions": 768,
      "encoding_format": "float"
    }
  },
  "embedder_ollama": {
    "client_class": "OllamaClient",
    "model_kwargs": { "model": "nomic-embed-text" }
  },
  "retriever": { "top_k": 20 },
  "text_splitter": { "split_by": "word", "chunk_size": 350, "chunk_overlap": 100 }
}
```

### .env (mode 600)

```
OPENAI_API_KEY=***
OPENAI_BASE_URL=http://host.docker.internal:1234/v1
DEEPWIKI_EMBEDDER_TYPE=openai
```

`DEEPWIKI_EMBEDDER_TYPE=openai` selects the top-level `embedder` block in `embedder.json`
(`api/config.py` `get_embedder_config`). `generator.json` was left UNCHANGED — the custom
model id `google/gemma-4-26b-a4b-qat` is passed through the request body's `model` field;
DeepWiki's `openai` provider has `supportsCustomModel: true` and tolerates off-list models.

### docker-compose.yml — diff applied

Added under the `deepwiki` service:

```yaml
    extra_hosts:
      - "host.docker.internal:host-gateway"
    volumes:
      # (existing ~/.adalflow and ./api/logs mounts retained)
      - ./gobby-cli-src:/repos/gobby-cli:ro
```

## Docker artifacts created

| Kind | Name | ID | Notes |
|------|------|----|-------|
| Image | `deepwiki-open-deepwiki:latest` | `5bc61a1d017b` | 1.2 GB. LEFT ON DISK per instructions. |
| Container | `deepwiki-open-deepwiki-1` | (from image above) | Stopped+removed at teardown. |
| Network | `deepwiki-open_default` | `8f4b0c61cc0b` | bridge; removed by `compose down`. |
| Compose project | `deepwiki-open` | — | config: this dir's docker-compose.yml |

Ports published while running: host `8001` -> API, host `3000` -> Next.js frontend.

Persisted host data (NOT removed by `compose down`):
- `~/.adalflow/databases/` — embeddings/index for the local repo (nomic, 768-dim).
- `~/.adalflow/repos/` — DeepWiki repo registry (local mount path recorded).
- `~/.adalflow/wikicache/` — wiki cache (if written).
- `./api/logs/` and `./gobby-cli-src/` under the deepwiki-open dir.

Existing daemon containers were NOT touched: `gobby-postgres`, `gobby-postgres-test-1`,
`services-falkordb-1`, `services-qdrant-1`.

## Teardown commands (exact)

```bash
cd /Users/josh/Projects/wiki-bakeoff/deepwiki-open

# Stop + remove the container and the deepwiki-open_default network (leaves images).
docker compose down

# (Optional, NOT done here) remove the built image to reclaim 1.2 GB:
#   docker image rm deepwiki-open-deepwiki:latest
# (Optional) remove persisted embedding/wiki data:
#   rm -rf ~/.adalflow/databases ~/.adalflow/wikicache ~/.adalflow/repos
# (Optional) remove the read-only repo export:
#   rm -rf /Users/josh/Projects/wiki-bakeoff/deepwiki-open/gobby-cli-src
```

Image left behind for reuse: `deepwiki-open-deepwiki:latest` (`5bc61a1d017b`, 1.2 GB).
