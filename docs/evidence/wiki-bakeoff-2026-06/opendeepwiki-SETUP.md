# OpenDeepWiki (AIDotNet/OpenDeepWiki, a.k.a. KoalaWiki) — bake-off SETUP

**Track A2 — code → docs generation.** Comparator to `gcode codewiki`. Run: 2026-06-14.
Corpus: clean `git archive HEAD` (`ea6a26c`) of gobby-cli, scanned at `crates/` (433 code
files, primary language auto-detected **Rust**). Model: local LM Studio
`google/gemma-4-26b-a4b-qat` (chat + wiki catalog + wiki content). Config: endpoint + API
key + model id only; OpenDeepWiki's own defaults otherwise.

## What it is / architecture
ASP.NET Core **.NET 10** backend (repository-processing + wiki-generation workers, SQLite
by default) + a **Next.js** web UI. Ingests Git URLs, ZIP archives, or **approved local
directories**; generates a catalog (directory structure) then per-node content docs, and
reuses the indexed content for chat + MCP. Bundles `graphifyy` inside the backend image for
optional graph artifacts.

## Backend-only run (UI out of scope)
The wiki is produced by the backend `opendeepwiki` service (writes docs to the DB + `/data`);
the `web` Next.js service is only a browser UI (UI/UX is a deliberate later concern — vault/
Markdown compatibility is what we evaluate). So only the backend is built and run.

The compose `image:` points at an Aliyun (cn-shenzhen) registry — **built locally** instead of
pulling:
```
cd OpenDeepWiki && docker compose build opendeepwiki      # .NET 10 SDK build; bundles graphifyy
```

## Fair config (`compose.override.yaml`)
Only endpoint + key + model id are set, per AI lane (chat / wiki-catalog / wiki-content) →
host LM Studio at `http://host.docker.internal:1234/v1`, model `google/gemma-4-26b-a4b-qat`,
key via `${OPENAI_API_KEY}` interpolation (never inlined). Left at OpenDeepWiki defaults:
request type `OpenAI`, `WIKI_PARALLEL_COUNT=5`, `WIKI_MAX_OUTPUT_TOKENS=32000`, chunking,
retrieval, `WIKI_DOCUMENT_GENERATION_TIMEOUT_MINUTES=60`, `WIKI_MAX_RETRY_ATTEMPTS=3`. Set
`WIKI_LANGUAGES=en` (locale scope, not a quality knob; default also does zh/ja/ko). The clean
clone is bind-mounted read-only at `/data/inputs/gobby-cli` and allowlisted via
`RepositoryAnalyzer__AllowedLocalPathRoots__0=/data/inputs`.

Bring up backend only (key sourced into env, never echoed):
```
set -a; . ~/Projects/wiki-bakeoff/.env; set +a
docker compose up -d opendeepwiki
```

## Submission
Seeded admin (`DbInitializer`): `admin@routin.ai` / `Admin@123`. Login →
`POST /api/auth/login` → JWT. Submit the mounted clone →
`POST /api/v1/repositories/submit-local`
`{orgName:"gobby", repoName:"gobby-cli", localPath:"/data/inputs/gobby-cli",
branchName:"main", languageCode:"en"}`. A background worker prepares the workspace (Copy),
detects language, then runs catalog → content generation. Re-trigger with
`POST /api/admin/repositories/{id}/regenerate`.

## Honest config note (ergonomics nit, NOT a quality finding)
On first run, catalog generation failed immediately:
`System.InvalidOperationException: AI model 'ark-code-latest' is not available for provider
'legacy-openai-http-host-docker-internal-1234-v1-...'`. Root cause: the flat
`WIKI_CATALOG_MODEL` / `WIKI_CONTENT_MODEL` env vars did **not** propagate into the DB-backed
wiki model bindings — the **provider** bound correctly to LM Studio, but the **model id**
fell back to the appsettings default `ark-code-latest` (`WikiGenerator:CatalogModel`). The
`AiProviderResolver` requires the bound model to exist as a registered model row for the
provider, so it threw. (Oddly, `WIKI_TRANSLATION_MODEL_ID` and `GRAPHIFY_MODEL_ID` *did* pick
up gemma — only the catalog/content flat-env model ids were dropped.)

Fixed via the admin settings API (the user-facing config path):
`PUT /api/admin/settings/` with `WIKI_CATALOG_MODEL_ID` / `WIKI_CONTENT_MODEL_ID` =
`google/gemma-4-26b-a4b-qat` (gemma was already a discoverable model on the provider), then
`/regenerate`. This is still within the fair-config rule (configuring the model id, nothing
else). Logged as a config-ergonomics nit for OpenDeepWiki's env → DB migration, not a quality
result.

## Secrets
`OPENAI_API_KEY` (LM Studio token) only ever reaches the container via `${OPENAI_API_KEY}`
compose interpolation, sourced from `~/Projects/wiki-bakeoff/.env` at `up` time. Never inlined
in any file, never echoed.
