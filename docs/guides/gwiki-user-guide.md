# gwiki User Guide

A complete guide to using `gwiki` to capture, search, upkeep, and synthesize a
local-first knowledge vault: install → vault → ingest → search/ask →
maintenance.

`gwiki` ingests multimodal sources (documents, PDFs, URLs, MediaWiki, git,
audio/image/video) into a Markdown vault, indexes them for hybrid search, and
turns accepted research notes into cited articles. `search` is the retrieval
primitive — for humans and for agents composing `search` + `read` research
loops — and `ask` layers one bounded-evidence completion on top of it. It
shares gcode's hybrid BM25 + semantic + graph search stack and the shared
Gobby AI routing layer.

## Getting Started

### Install

Download from [GitHub Releases](https://github.com/GobbyAI/gobby-cli/releases/latest)
or build from source:

```bash
cargo install gobby-wiki
```

If you use [Gobby](https://github.com/GobbyAI/gobby), gwiki is already installed.

Release downloads include `gwiki-*.sha256` checksum files. Verify the archive
before installing it:

```bash
sha256sum -c gwiki-*.sha256
```

`sha256sum -c` should print `OK` for the downloaded `gwiki` artifact. On macOS,
install GNU coreutils for `sha256sum`, or compare the digest with `shasum -a 256`.

`gwiki` is daemon-independent but not database-independent. PostgreSQL-backed
search uses ParadeDB's **`pg_search`** BM25 indexes on the `gwiki_documents` and
`gwiki_chunks` tables. The hub DSN is resolved from `~/.gobby/bootstrap.yaml`.
Run `gwiki setup --standalone` only against a PostgreSQL hub where the
`pg_search` extension is installed — setup preflights the extension before it
creates any BM25 indexes.

PostgreSQL, FalkorDB, Qdrant, and the embedding endpoint are the required
runtime stack. Use `gwiki setup --standalone` to provision or validate the same
Docker-backed services that attached Gobby mode uses.

### Scopes: `--project` vs `--topic`

Every `gwiki` command runs against one vault scope. There are two:

- `--project [ROOT]` — a Gobby project's wiki scope. Bare `--project` (no value)
  uses the current directory; `--project /path/to/repo` targets another project
  root.
- `--topic <NAME>` — a named topic vault that is independent of any project.

`--topic` wins over `--project` when both are given. With **no** scope flag,
`gwiki` detects the project from the current working directory. Scoped JSON
results carry a resolved `scope` identity with `kind` and `id`.

```bash
gwiki --project status          # current directory's project vault
gwiki --project /repo status    # another project root
gwiki --topic rust-async status # a standalone topic vault
```

The rest of this guide uses `gwiki --topic NAME <cmd>` and `gwiki --project <cmd>`
forms so the scope is always explicit.

### Initialize a Vault

```bash
gwiki --topic rust-async init
```

`gwiki init` scaffolds the vault structure and registers the scope. It creates:

| Path | Purpose |
|------|---------|
| `raw/` (+ `raw/assets/`) | Captured source bytes and the raw manifest |
| `knowledge/` (+ `knowledge/sources/`, `knowledge/concepts/`, `knowledge/topics/`) | Compiled article pages |
| `code/` | Generated code documentation |
| `inbox/` | Drop zone for files awaiting `collect` |
| `outputs/` | Exported bundles and reports |
| `meta/` (+ `meta/health/`) | Health snapshots and vault metadata |
| `_meta/` | Generator metadata (codewiki/ownership state) |
| `_index.md` | Wiki index landing page |
| `raw/INDEX.md` | Manifest of raw sources (drives `refresh`) |
| `knowledge/INDEX.md`, `code/INDEX.md` | Section index pages |
| `log.md` | Vault activity log |
| `_gwiki/scope.json` | Resolved scope identity for the vault |

The `knowledge/sources`, `knowledge/concepts`, and `knowledge/topics`
subdirectories line up with the three `compile --kind` values.

### Set Up Storage

```bash
gwiki --topic rust-async setup --standalone
```

`gwiki setup` creates gwiki-owned derived storage. `--standalone` resolves or
provisions the shared Gobby hub before creating gwiki storage, and provisions
Docker services when no reachable hub is configured. Setup preflights
`pg_search` before creating BM25 indexes.

**Options:**
- `--standalone` — Resolve or provision the shared Gobby hub before creating gwiki storage.
- `--database-url <DSN>` — PostgreSQL URL to use for setup without persisting the value in output.
- `--no-services` — Do not provision Docker services when no reachable hub is configured.
- `--falkordb-host <HOST>`, `--falkordb-port <PORT>`, `--falkordb-password <PASSWORD>` — FalkorDB graph store.
- `--qdrant-url <URL>` — Qdrant vector store.
- `--embedding-provider`, `--embedding-api-base <URL>`, `--embedding-model`, `--embedding-query-prefix`, `--embedding-vector-dim <DIM>`, `--embedding-api-key <KEY>` — Embedding endpoint for semantic search.

Valid embedding provider values are `lmstudio`, `ollama`,
`openai-compatible`, and `none`. `gwiki` is pre-release, so removed aliases are
rejected instead of translated.

See [AI Configuration](./ai-configuration.md) for the canonical embedding and
routing matrix.

### Check Readiness

```bash
gwiki --topic rust-async status
```

Reports shell readiness, daemon URL, runtime mode, and configured service
metadata. Run it after `init`/`setup` to confirm the scope resolves before you
start ingesting.

### Check Trust

```bash
gwiki --topic rust-async trust
gwiki --topic rust-async trust --format json
```

Reports whether the selected wiki is indexed, fresh, searchable, graph-backed,
and audit-clean. JSON output includes `trust_status`, `services`,
`index_counts`, `degradations`, `freshness`, `audit_summary`, `link_summary`,
`graph_metrics`, and `health_summary`.

PostgreSQL, FalkorDB, Qdrant, and embeddings are required for the full product
path. `trust` can still run when a service is missing so it can report the
degradation explicitly; PostgreSQL count failures fall back to a memory scan of
the vault and mark `index_counts.backend` as `memory`.

## Capture Sources

Ingestion lands raw source bytes under `raw/` and records a manifest entry in
`raw/INDEX.md`. Derived Markdown is written with frontmatter and provenance so
every downstream claim can cite its source.

### Capture a Local File (`ingest-file`)

```bash
gwiki --topic rust-async ingest-file notes/spec.pdf
gwiki --topic rust-async ingest-file media/talk.mp4 --translate --target-lang en
gwiki --topic rust-async ingest-file media/private.mp3 --no-ai
```

`ingest-file` routes a local source by type. Text-bearing sources (documents,
PDFs, HTML, Markdown, text) are derived directly; audio, image, and video are
sent through the multimodal orchestrators (transcription/translation, vision,
frame extraction) when AI routing is enabled.

**When to use:** You have a local file — document, PDF, image, audio, or video —
to bring into the vault.

**Options:**
- `--no-ai` — Disable AI-backed media extraction for this ingest (the privacy path). The source is still stored as a raw asset, with degraded derived output where applicable.
- `--translate` — Prefer audio translation over transcription where a backend is available.
- `--target-lang <LANG>` — Target language for audio translation.
- `--video-frame-interval <SECONDS>` — Seconds between sampled video frames; `0` disables frames.
- `--transcription-routing <auto|daemon|direct|off>` — Routing override for audio transcription and translation.
- `--vision-routing <auto|daemon|direct|off>` — Routing override for vision extraction.
- `--text-routing <auto|daemon|direct|off>` — Routing override for text generation.

The three `--*-routing` flags and `--no-ai` override the configured AI routing
for a single ingest. See [AI Routing & Configuration](#ai-routing--configuration).

### Fetch URLs (`ingest-url`)

```bash
gwiki --topic rust-async ingest-url https://example.com/post
gwiki --topic rust-async ingest-url https://a.com/x https://b.com/y
```

Fetches one or more URL sources into the vault. The positional `URL` argument is
repeatable. URL ingest handles web pages, Wayback captures, MediaWiki pages, and
git repositories by source type.

### Sync Session Transcripts (`sync-sessions`)

```bash
gwiki sync-sessions
gwiki --topic rust-async sync-sessions --archive-dir ~/.gobby/sessions/archive
gwiki --topic rust-async sync-sessions --limit 20
gwiki --topic rust-async sync-sessions --raw
gwiki --topic rust-async sync-sessions --summarize
```

Syncs daemon-synthesized Gobby session wiki pages into the wiki vault. By
default, raw `*.jsonl.gz` archives are used only for presence reconciliation and
are not parsed as fallback content. Pass `--raw` to opt in to legacy raw
transcript fallback when no synthesized page exists. Per-CLI adapters recognize
Claude Code, Codex, Grok, Qwen, and Droid archives, normalize each raw
fallback into a derived Markdown page with deterministic session frontmatter, and
redact secrets on ingest.

Session pages are machine-wide rather than project-scoped, so a `sync-sessions`
run with **no** `--topic` / `--project` defaults to the machine-global `sessions`
scope. Pass `--topic` or `--project` to route the pages into a specific vault.

**When to use:** You want archived agent/CLI sessions captured as cited,
searchable vault pages.

**Options:**
- `--archive-dir <PATH>` — Directory containing archived `*.jsonl.gz` session transcripts. Defaults to the standard Gobby session archive location when omitted.
- `--wiki-dir <PATH>` — Directory containing daemon-synthesized session wiki `*.md` files. Defaults to the standard Gobby session wiki location when omitted.
- `--limit <N>` — Maximum number of archives to process in this run.
- `--raw` — Parse raw transcript archives as fallback content when no synthesized page exists.
- `--summarize` — When a raw archive has no daemon-synthesized page, generate a session summary directly from the transcript using the shared `handoff/session_end` prompt (falling back to a skeleton page when text generation is routed off) instead of relying on the raw fallback.

### Collect Inbox Drops (`collect`)

```bash
gwiki --topic rust-async collect
gwiki --topic rust-async collect "design docs"
```

Promotes recognized files dropped into `inbox/` into raw storage. The optional
`QUERY` positional narrows which drops to collect. Use this when you stage files
into `inbox/` out of band and want them brought into the vault in one pass.

### Refresh URL-Backed Sources (`refresh`)

```bash
gwiki --topic rust-async refresh
gwiki --topic rust-async refresh --id src-abc123 --id src-def456
gwiki --topic rust-async refresh --dry-run
```

`gwiki index` rebuilds derived state from files already on disk; `gwiki refresh`
re-fetches the upstream sources first, then indexes once when a batch changed.
Refresh is **manifest-driven** from `raw/INDEX.md` — it never scans directories
or follows globs.

It refreshes URL records and local files originally captured by `ingest-file`
(audio, image, video, PDF, Office, HTML, Markdown, text, and generic file
sources). Unchanged local file bytes are reported without rerunning AI/media
extraction. Connector records without replay contracts — stdin, research notes,
MediaWiki, Wayback, and git repositories — are skipped unless explicitly
selected with `--id`. Local file/media records that lack replay metadata are
reported in `failed`.

**Options:**
- `--id <SOURCE_ID>` — Source ID to refresh. Repeat to refresh multiple explicit sources.
- `--dry-run` — Preview refresh candidates without fetching, reading source bytes, writing, deleting, or indexing.

Changed content gets a new content-hash source ID and reports `old_id`,
`new_id`, `raw_path`, and `previous_raw_path`; unchanged content is left
untouched. See [`crates/gwiki/README.md`](../../crates/gwiki/README.md) for the
full refresh contract.

### List Sources (`sources`)

```bash
gwiki --topic rust-async sources
```

Lists the raw source manifest entries in the selected scope — each with its
`id`, `url`, `path`, `raw_path`, and `source_path`. Use it to find the
`SOURCE_ID` values that `refresh` and `remove-source` consume.

### Remove a Source (`remove-source`)

```bash
gwiki --topic rust-async remove-source --id src-abc123 --dry-run
gwiki --topic rust-async remove-source --id src-abc123 --yes
gwiki --topic rust-async remove-source --id src-abc123 --yes --keep-asset
```

Removes a raw source, its manifest entry, and its raw asset. `--id` is required.

**Options:**
- `--id <SOURCE_ID>` — Source ID to remove (required).
- `--dry-run` — Preview file and manifest changes without mutating the vault.
- `--yes` — Confirm destructive removal.
- `--keep-asset` — Preserve the raw source asset referenced by `source_asset` frontmatter.

### Purge Generated State (`purge`)

```bash
gwiki --project /path/to/repo purge --yes
gwiki --topic rust-async purge --yes
```

Purges generated/indexed wiki state for an explicit project or topic scope. It
removes scoped PostgreSQL rows for documents, chunks, links, sources, and
ingestions, then clears matching Qdrant vectors and FalkorDB wiki graph state
when those backends are configured. Global purge is rejected; choose a project
or topic scope.

**Options:**
- `--yes` — Confirm destructive purge. Without it, `gwiki` reports what would be
  purged and exits with an input error.

## Search & Ask

### Index (`index`)

```bash
gwiki --topic rust-async index
```

Rebuilds BM25/derived search state for the Markdown and source notes in the
selected scope. Run it after capturing or editing sources so search can see
them. In PostgreSQL-backed mode, indexing also syncs the wiki graph to FalkorDB;
semantic search uses the required Qdrant and embedding configuration.

### Search (`search`)

```bash
gwiki --topic rust-async search "lifetime elision"
gwiki --topic rust-async search "lifetime elision" --limit 5
gwiki --topic rust-async search "lifetime elision" --no-semantic
```

Hybrid search across wiki documents: pg_search BM25 text matching merged with
semantic vector search and FalkorDB graph boost via Reciprocal Rank Fusion.
JSON search results include a `fusion_key` (`scope-kind:scope-id:page-path`) so
agents can identify the canonical wiki page even when BM25 chunks, semantic
points, and graph documents contributed separate backend hit IDs.

**When to use:** General-purpose queries over vault content. Best for natural
language and conceptual searches. `search` is also the agent retrieval
primitive: each hit carries a bounded query-token snippet (never a full
document body), `wiki_page`, `source_path`, `result_type` (`wiki`/`code`),
contributing `sources` with per-source `explanations`, and the output includes
`code_citations` derived from the returned hits. Follow up with
`gwiki read --path <wiki_page>` for full content.

**Options:**
- `--limit <N>` — Max results (default: `10`).
- `--no-semantic` — Disable semantic vector ranking for this query (BM25 + graph only). This is a query-time ranking control; Qdrant and embeddings are still required runtime infrastructure.
- `--token-budget <N>` — Trim results to fit an approximate token budget. When hits are dropped to stay inside the budget, the output emits a narrowing hint so an agent can refine the query rather than silently lose context.

### Ask (`ask`)

```bash
gwiki --topic rust-async ask "How does the borrow checker handle reborrows?"
gwiki --topic rust-async ask "..." --llm
gwiki --topic rust-async ask "..." --llm --ai direct --require-ai
```

`ask` is a thin RAG layer over `search`: it retrieves the top-k hits, builds a
bounded evidence prompt from query-centered excerpts (capped at
`prompt_token_budget`, ~12K estimated tokens; actual usage is reported as
`prompt_tokens_estimated`), and — with `--llm` — runs a single completion to
synthesize a written answer with grounded citations. When evidence has to be
dropped to stay inside the budget, `truncated` is set and
`truncated_components` lists `evidence`.

**When to use:** You want an answer assembled from the vault, not just a ranked
list of pages.

**Options:**
- `--llm` — Synthesize an answer from retrieved wiki hits.
- `--ai <auto|daemon|direct|off>` — AI routing override for synthesis (default: `auto`). Inert unless `--llm` is set. `daemon` routes through the Gobby daemon; `direct` hits any OpenAI-compatible endpoint (`ai.text_generate.api_base`/`api_key`), including LM Studio locally.
- `--require-ai` — Fail if synthesis is requested but no AI route succeeds.
- `--token-budget <N>` — Trim retrieval hits to fit an approximate token budget before the evidence prompt is built, emitting a narrowing hint when hits are dropped. This applies on top of the `prompt_token_budget` evidence cap described above.

Without `--llm`, `ask` is a pure retrieval command and runs without any AI route.

**Citation check.** Synthesized answers are checked after generation against the
retrieved evidence (hit titles, snippets, paths, the prompt's evidence
excerpts, and code citations). The JSON output carries the verdict in
`synthesis.citation_check` — `status` is `supported` when every extracted claim
overlaps the evidence, or `unsupported_claims` with the offending claims listed
in `unsupported_claims[]`; each ungrounded claim also adds a `warnings[]` entry.
Text output appends an `[unverified]` note when any claim fails the check. The
check is a deterministic token-overlap heuristic, not a semantic proof — treat
flagged claims as unverified rather than wrong, and verify them against the
cited pages. (Persisted prose is validated separately: research-note linting
rejects uncited claims and `audit` checks pages against provenance.)

## Navigate the Vault

### Read (`read`)

```bash
gwiki --topic rust-async read --path knowledge/topics/borrow-checker.md
gwiki --topic rust-async read --title "Borrow Checker"
```

Reads a wiki page or document in the selected scope. Exactly one of `--path` or
`--title` is required.

**Options:**
- `--path <PATH>` — Vault-relative wiki path to read.
- `--title <TITLE>` — First-heading title to resolve inside the selected scope.

Read targets are restricted to canonical vault documents: `raw/INDEX.md`,
`_index.md`, `log.md`, or paths under `knowledge/` and `code/`.

### Backlinks (`backlinks`)

```bash
gwiki --topic rust-async backlinks "Borrow Checker"
```

Shows the pages that link to the given page. Useful for understanding how a
concept is referenced across the vault.

### Suggest Links (`link-suggest`)

```bash
gwiki --topic rust-async link-suggest
gwiki --topic rust-async link-suggest --limit 25
```

Suggests unresolved wiki links in the selected scope — references that point at
pages that don't exist yet, so you can fill gaps or fix typos.

**Options:**
- `--limit <N>` — Max suggestions (default: `10`).

## Research & Compile

### Agent Research (search + read + deposit)

There is no built-in research loop: the calling agent *is* the research loop.
Compose the retrieval primitives —

```bash
gwiki --topic rust-async search "async fn in traits stabilization"
gwiki --topic rust-async read --path knowledge/topics/async-traits.md
```

— then deposit findings back into the vault as cited notes through the capture
commands: write a Markdown note with a `citation:` line, register it with
`gwiki ingest-file <note.md>` (or drop it in the inbox for `collect`), and run
`index`. Provenance, citations, and lint/audit checks apply to deposited notes
the same as any other source.

**When to use:** You (or an agent) want to enrich the vault on a question,
gathering and citing new sources with full control over the loop.

### Compile (`compile`)

```bash
gwiki --topic rust-async compile "Borrow Checker"
gwiki --topic rust-async compile "Borrow Checker" --kind concept
gwiki --topic rust-async compile "Borrow Checker" \
  --outline "Overview" --outline "Reborrows" --outline "Common errors"
gwiki --topic rust-async compile "Borrow Checker" \
  --source src-abc123 --source raw/src-def456.md
gwiki --topic rust-async compile "Borrow Checker" --target knowledge/topics/borrow.md --write-intent
```

Compiles accepted research notes into a wiki article. The positional `[TOPIC]`
names the article topic — it never changes the scope (use `--topic`/`--project`
for that). With no positional, the article topic defaults to the topic scope's
own name, then to the research session's compile state or question.

Use `--source <SOURCE_ID_OR_PATH>` to select already-ingested manifest sources
for the compile session. It is repeatable; selectors resolve as source ID,
derived raw path (`raw/<id>.md`), then exact manifest `location` or
`canonical_location`. Passing `--source` replaces the checkpoint's accepted
notes with the selected sources in selector order. On a fresh vault with no
research checkpoint, compile creates the checkpoint from the provided `[TOPIC]`
or `--topic`; without either topic seed it returns `invalid_input`.

**Options:**
- `[TOPIC]` — Optional positional naming the article topic.
- `--outline <HEADING>` — Seed an article heading. Repeatable to set the section order.
- `--source <SOURCE_ID_OR_PATH>` — Select an ingested source for this compile session. Repeatable; duplicates by source ID are ignored after the first occurrence.
- `--kind <source|concept|topic>` — Article kind (default: `topic`). Determines which `knowledge/` subdirectory the page lands in (`knowledge/sources`, `knowledge/concepts`, or `knowledge/topics`).
- `--target <PAGE>` — Explicit vault-relative target page path.
- `--write-intent` — Authorize overwriting an existing target page. By default `compile` **refuses** to overwrite an existing page and returns an `invalid_input` error asking for merge/diff handling; `--write-intent` opts into overwrite-after-merge.

`compile` is the only article-generation step. Research and audit produce
accepted notes and findings; `compile` turns those notes into cited pages and
updates the wiki index and provenance.

## Maintenance & Export

### Audit (`audit`)

```bash
gwiki --topic rust-async audit
```

Reports claims that lack source support — the uncited-claims check over the
vault. Findings include the offending page and evidence.

### Lint (`lint`)

```bash
gwiki --topic rust-async lint
```

Detects broken links and vault hygiene issues — dangling wiki links, malformed
references, and similar structural problems. Lint also runs Mermaid diagram
checks on curated wiki pages: a **validity** check that rejects malformed Mermaid
blocks (and verifies diagram headers against the supported set), and a
**grounding** check that flags Mermaid node labels with no support in the
surrounding page text.

### Normalize (`normalize`)

```bash
gwiki --topic rust-async normalize
gwiki --topic rust-async normalize --check
```

Normalizes whitespace in already-written vault Markdown (markdownlint repair) —
collapsing the whitespace issues that authored or generated pages accumulate.
By default it rewrites the affected docs in place.

**Options:**
- `--check` — Report which authored docs need normalization without rewriting
  them. This is an exit-code gate: it reports the docs that are out of compliance
  and fails rather than mutating the vault, so it can guard CI or a pre-commit
  step.

### Health (`health`)

```bash
gwiki --topic rust-async health
```

Writes wiki health snapshots under `meta/health/` and reports the `text_path`
and `json_path` it wrote. Use it to capture a point-in-time readiness snapshot
for review or CI.

### Benchmark (`benchmark`)

```bash
gwiki --topic rust-async benchmark
gwiki --topic rust-async benchmark --retrieval-candidates 12
```

Reports diagnostic metrics for a seeded, indexed wiki: token compression, graph
coverage, retrieval precision, source mix, and optional backend availability.
The command requires PostgreSQL index configuration and returns a configuration
error if no attached index is available.

- `--retrieval-candidates <N>` — Number of seeded retrieval precision probes to
  run. The value must be greater than zero.

Optional graph, vector, embedding, and model-provider backends are reported as
unavailable or listed under `degraded_sources`; their absence does not fabricate
zero-valued metrics.

### Graph (`graph`)

```bash
gwiki --topic rust-async graph
```

Exports unified wiki graph artifacts under `outputs/`: `graph.json` for
machine-readable graph data and `GRAPH_REPORT.md` for a readable summary. The
JSON includes document/source/citation/link nodes, trust and audit edges, graph
analytics, and `degraded`/`degraded_sources` fields.

`graph` also writes static agent-context exports alongside those artifacts:

- `llms.txt` — a portable, link-indexed table of contents over the vault
  document graph, in the `llms.txt` convention.
- `llms-full.txt` — the same index expanded with the full Markdown body of each
  present document for single-file agent ingestion.
- `graph.jsonld` — a schema.org JSON-LD representation of the vault document
  graph for structured/linked-data consumers.

`graph` requires PostgreSQL index configuration. Missing optional FalkorDB,
Qdrant, or embedding support marks the exported graph degraded so consumers can
distinguish a partial graph from a complete one.

### Graph Context (`graph-context`)

```bash
gwiki --project graph-context
gwiki --topic rust-async graph-context --format json
```

Builds a compact context pack for review and automation. Text output reports the
scope, neighborhood count, and warning count; JSON output returns the context
pack with `degradation`, `warnings`, `neighborhoods`, and `recommendations`.

`graph-context` requires PostgreSQL index configuration. Project scopes can add
shared code-graph edges when FalkorDB is configured; topic and global scopes, a
missing FalkorDB config, query failures, or truncation add explicit degraded
sources and warnings instead of hiding the limitation.

### Librarian (`librarian`)

```bash
gwiki --topic rust-async librarian
```

Proposes wiki upkeep tasks and patch candidates without rewriting pages. Use it
as a planning report before applying manual edits or opening follow-up work. The
output is a normal scoped command result, so `--format json` is useful when a
workflow needs to consume the proposals.

The command is vault-backed and reports analysis errors through the normal
`gwiki` error format; it does not require optional AI, graph, or vector services.

### Review Report (`review-report`)

```bash
gwiki --project review-report --file crates/gwiki/src/main.rs
gwiki --project review-report --symbol <SYMBOL_ID>
gwiki --project review-report --diff /tmp/change.diff --output review.md
```

Exports a Markdown review report for proposed code changes. The report connects
changed files or symbols to affected wiki pages, stale docs, code graph
neighborhoods, and risky dependency shifts.

- `--file <PATH>` — Include a changed file. Repeat the flag for multiple files.
- `--symbol <SYMBOL_ID>` — Include a changed symbol ID. Repeat the flag for
  multiple symbols.
- `--diff <PATH>` — Read changed file paths from a unified diff.
- `--output <FILE>` — Report filename under `outputs/` (default:
  `review-report.md`).

Pass at least one file, symbol, or diff. The command requires PostgreSQL index
configuration. Missing FalkorDB, non-project scopes, graph query failures, or
semantic partial-data conditions are surfaced in the report payload as
`degraded=true` with `degraded_sources`.

### Citation Quality (`citation-quality`)

```bash
gwiki --topic rust-async citation-quality
gwiki --topic rust-async citation-quality --format json
```

Writes `outputs/reports/citation-quality.md` and returns the same report through
the command output. The report covers dependency classification, source
credibility, coverage gaps, contradiction findings, stale sources, and output
confidence.

`citation-quality` requires the attached PostgreSQL-backed gwiki index. AI text
generation enables contradiction diagnostics; if AI availability cannot be
resolved, the command logs a warning and continues without AI-backed
contradiction findings.

### Export (`export`)

```bash
gwiki --topic rust-async export workflow-assets
gwiki --topic rust-async export workflow-assets --output my-assets.md
gwiki --topic rust-async export report --from outputs/report.md --output shared/report.md
```

Exports generated bundles and reports under `outputs/`.

- `export workflow-assets` — Export bundled workflow prompts and skill assets.
  - `--output <FILE>` — Destination file (default: `workflow-assets.md`).
- `export report` — Export an existing generated report file.
  - `--output <FILE>` — Destination file (required).
  - `--from <PATH>` — Source report path (required).

## The Vault Model

A `gwiki` vault is a Markdown-first knowledge store with clear separation
between captured material and synthesized articles:

- `raw/` — Captured source bytes (`raw/assets/`) and the source manifest
  (`raw/INDEX.md`). Everything ingested lands here first.
- `inbox/` — Staging area for files dropped out of band, promoted by `collect`.
- `knowledge/` — Compiled article pages, split into `knowledge/sources/`,
  `knowledge/concepts/`, and `knowledge/topics/` to mirror the `compile --kind` values.
- `code/` — Generated code documentation from `gcode codewiki`.
- `outputs/` — Exported bundles and reports.
- `meta/` — Vault metadata and `meta/health/` snapshots.

Derived Markdown carries **frontmatter** and **provenance**, and every accepted
claim **cites** a source. Research validation rejects an accepted note when a
factual claim has no source id, a source id is missing from frontmatter, a
source path is outside the resolved scope, a URL was not ingested or recorded as
intentionally external, or a quoted passage lacks available location metadata.
Unsupported claims become recorded gaps, not accepted notes. The vault model is
implemented in `crates/gwiki/src/vault.rs`, `frontmatter.rs`, `provenance.rs`,
and `citations.rs`; see the
[gwiki Development Guide](./gwiki-development-guide.md) for internals.

## AI Routing & Configuration

`gwiki` routes AI per capability. Each capability can use the daemon, a direct
OpenAI-compatible HTTP endpoint, or stay off:

| Capability | Used for |
|------------|----------|
| Audio transcription (`ai.audio_transcribe`) | Speech-to-text for audio/video |
| Audio translation (`ai.audio_translate`) | Translating audio segments |
| Vision extraction (`ai.vision_extract`) | Image/frame understanding |
| Text generation (`ai.text_generate`) | `ask --llm` synthesis, derived text |
| Embeddings (`ai.embeddings`) | Semantic vectors for hybrid search |

Routing values are `auto`, `daemon`, `direct`, and `off`. `direct` means any
OpenAI-compatible endpoint, local or remote — there is no `local` route. AI
settings resolve from daemon-supported `config_store` keys when the daemon
database is available, then `~/.gobby/gcore.yaml`, then defaults; `GOBBY_*`
environment variables are not an AI configuration layer. `ai.text_generate.*`
is the CLI standalone/direct namespace; daemon text generation uses daemon
provider config such as `ai.generation.local.endpoints.<name>`.

Indexing respects `.gitignore`, `.git/info/exclude`, and global git excludes by
default. The shared setting resolves from `config_store`, then
`~/.gobby/gcore.yaml`, then default `true`:

```yaml
indexing:
  respect_gitignore: true
```

Override routing for a single ingest with `--transcription-routing`,
`--vision-routing`, and `--text-routing`, or take the privacy path with
`--no-ai`, which forces every capability to `off` for that command while still
storing the raw asset and recording degraded derived output.

For the full capability/backend matrix, mixed-routing examples, local model
budget, and `$secret:` handling, see the canonical
[AI Configuration](./ai-configuration.md) guide.

## Graceful Degradation

The datastore stack is required: PostgreSQL stores the wiki hub/search rows,
FalkorDB stores the wiki graph, and Qdrant stores vectors. Missing datastore
configuration is a setup error, not a normal degraded search mode.

- **BM25 search** requires the PostgreSQL hub and the selected scope to be
  indexed.
- **Semantic search** requires Qdrant and a configured embedding endpoint.
- **Graph boost** requires FalkorDB and a synced wiki graph projection.
- **Multimodal ingest** stores the raw asset with explicit degradation markers
  when required transcription, vision, or text routing is unavailable.

JSON output still surfaces a `degradations` array for configured query-time
datastore outages, AI/media processing degradation, and explicit query-time
controls.

## Output Formats

All commands honor the global `--format` flag:

```bash
gwiki --topic rust-async search "query" --format json   # Default — structured JSON
gwiki --topic rust-async search "query" --format text   # Human-readable text
```

JSON is the default and the format daemon-facing code consumes; scoped results
include the resolved `scope` identity (`kind`, `id`). Suppress status messages
with `--quiet`:

```bash
gwiki --topic rust-async index --quiet
```

`gwiki contract --format json` emits the machine-readable CLI contract for
daemon conformance tests; it mirrors `crates/gwiki/contract/gwiki.contract.json`.
See [gwiki CLI Contract](../contracts/gwiki-cli.md) for the daemon-consumed
surface.

## Troubleshooting

### "No wiki scope found"

No scope resolved from the current directory. Initialize a vault or pass a scope
explicitly:

```bash
gwiki --topic NAME init
gwiki --project /path/to/repo status
```

### Empty search results

- Run `gwiki index` to pick up newly captured or edited sources.
- Confirm the scope is the one you ingested into (`gwiki status`).
- Use `gwiki status` to confirm PostgreSQL, FalkorDB, Qdrant, and embeddings
  are configured.

### `pg_search` missing

`setup` preflights ParadeDB's `pg_search` extension and fails if it is absent.
Install the `pg_search` extension on the PostgreSQL hub before running
`gwiki setup --standalone`.

### Semantic Search Configuration Error

Qdrant and the embedding endpoint are required for PostgreSQL-backed search.
Run `gwiki setup --standalone` or attach to Gobby's full datastore stack, then
re-run `gwiki index`.

### Media ingest produced skeleton output

The relevant AI capability was routed `off` or was unavailable, so ingest stored
the raw asset with degradation markers instead of derived text. Check the
`degradations` array, then re-run with an explicit routing override (for example
`--transcription-routing direct`) once the backend is reachable. `--no-ai`
always produces degraded output by design.

### `compile` refuses to overwrite a page

By default `compile` will not overwrite an existing target page. Pass
`--write-intent` to authorize overwrite-after-merge once you've confirmed the
merge is intended.

---

See also: [gwiki Development Guide](./gwiki-development-guide.md) (internals),
[gwiki README](../../crates/gwiki/README.md) (source refresh contract), and
[gwiki CLI Contract](../contracts/gwiki-cli.md).
