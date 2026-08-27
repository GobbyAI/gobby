# gwiki CLI Contract

The machine-readable contract lives at `crates/gwiki/contract/gwiki.contract.json`.
`gwiki contract --format json` must emit the same contract version and contents.

## Version

`contract_version`: 19

Version 19 adds resolved runtime-grant metadata to `status`. AI-backed commands
use daemon-issued routing by default and expose one override, `--no-ai`, for the
deterministic structural path. The contract covers generation, purge, citation
repair, metadata comparison, graph degradation, and JSON output keys.

The direct command remains available for isolated/manual use. Production-vault
execution and daemon-triggered generation are operationally paused pending the
wiki redesign; `GET /api/wiki/code/status` reports the disabled state and
`POST /api/wiki/code/refresh` returns HTTP 409 without scheduling work.

Version 15 adds `--time-budget-seconds` to `gwiki upkeep` and exposes
`budget_exhausted` plus `deferred_clusters` in its JSON output.

Version 14 adds the `--force` switch to `gwiki index`: re-index documents
whose content hashes are unchanged. This backfills derived rows (such as
`gwiki_documents.frontmatter`, populated at index time since this version)
for documents indexed before the current schema population rules existed.
Forced re-indexes of unchanged files still record `unchanged` ingestion
events.

Version 13 adds the vault mutation surface: `gwiki page write --path
<knowledge/….md> [--mode upsert|create] [--expected-hash <sha256>]` (content
read verbatim from stdin) and `gwiki page delete --path <knowledge/….md>`.
Contract command names with spaces (`page write`, `page delete`) denote
nested subcommands; the payload `command` keys are `page-write` and
`page-delete`. Writes and deletes are confined to `knowledge/**`: paths must
be vault-relative normalized markdown files, and symlink escapes (including a
redirected `knowledge/` itself) are rejected by canonicalizing the resolved
parent against the vault root. `--mode create` fails with the new
`already_exists` error code when the page exists and rejects
`--expected-hash`; `--expected-hash` on upsert compares the current on-disk
SHA-256 (the `gwiki read` revision baseline) and fails with the new
`precondition_failed` error code, leaving the file untouched. Both new error
codes exit 2. Payloads carry `changed_paths` so reindex prunes or refreshes
derived rows via the incremental indexer.

Version 10 covers the daemon-consumed surface:

- `contract`
- `index`
- `search`
- `ask`
- `read`
- `refresh`
- `ingest-file`
- `ingest-url`
- `sync-sessions`
- `collect`
- `compile`
- `audit`
- `graph`
- `graph-context`
- `benchmark`
- `health`
- `librarian`
- `upkeep`
- `recap`
- `review-report`
- `citation-quality`
- `sources`
- `backlinks`
- `status`
- `trust`
- `remove-source`

Version 12 adds the `pages` listing surface and the read revision baseline.
`gwiki pages [--prefix <p>]` lists indexed pages from `gwiki_documents`
(`path`, `title`, `tags` from frontmatter JSONB, `content_hash`, `updated_at`)
without page bodies, plus a separate `outputs` array walked from the vault's
unindexed `outputs/**` markdown reports (`path`, `size`, `modified`).
`--prefix` restricts the page listing to wiki paths with that prefix (e.g.
`code/`). `gwiki read` payloads now carry `content_hash` (SHA-256 of the full
document bytes, matching the indexer's hash) as the editor's revision baseline
for the conditional-write contract, and `outputs/**` paths become readable via
`gwiki read --path` while remaining excluded from indexing and writes.

Version 11 adds on-demand graph fetch flags to `graph`: `gwiki graph
[--stdout] [--include knowledge|code|all]`. `--stdout` emits the JSON envelope
`{"command":"graph","scope":…,"graph":<GraphExport>}` on stdout and writes no
artifact files; without it the artifact-writing behavior is unchanged.
`--include` (default `all`) filters facts before export and analytics:
`knowledge` keeps `knowledge/`, `recaps/`, and root pages with their
sources/citations and unresolved link targets while dropping code edges;
`code` keeps `code/**` documents plus code edges.

Version 10 adds the `recap` surface: `gwiki recap [--date YYYY-MM-DD]
[--no-ai]` writes the day's session recap page at
`recaps/YYYY-MM-DD.md`. Days attribute by UTC: each session digest's
`session_started_at` frontmatter wins, falling back to the manifest record's
`fetched_at`. Synthesis is one bounded single-shot completion — never an agent
tool loop — and rerunning the same day updates the existing page, folding its
current body into the prompt. A day with no sessions writes no page and is not
an error. `--date` defaults to today (UTC).

Version 9 adds the `upkeep` synthesis conductor: `gwiki upkeep [--max-pages N]
[--min-mentions N] [--max-sources-per-page N] [--dry-run]
[--no-ai]` drains pending sources into entity concept
pages. Unresolved wiki-link targets mentioned by at least `--min-mentions`
digests form clusters; each run synthesizes up to `--max-pages` concept pages
from at most `--max-sources-per-page` accepted sources apiece, then reconciles
compile status. `--dry-run` plans the run without writing to the vault.

Version 8 adds a `hint` key to `search` payloads, registers the `benchmark`
surface in the machine-readable contract, and gives `librarian` a `--no-ai`
switch for deterministic patch suggestions.

Version 5 makes `search` the agent retrieval primitive. `search` results carry bounded
query-token snippets (never full document bodies), provenance (`wiki_page`,
`source_path`, `result_type`, `sources`, `explanations`), and top-level
`code_citations` derived from the returned hits only. The `ask` and `research`
commands are removed — agents compose `search` and `read` for retrieval and
deposit results through `collect`/`ingest-file`; `compile` still compiles accepted
research notes and can select ingested manifest records with repeatable
`--source SOURCE_ID_OR_PATH`.

An additive version 7 update gives `compile` a source-selection surface.
`--source` selectors resolve as exact source ID, derived raw path
`raw/<id>.md`, then exact manifest `location`/`canonical_location`. Passing at
least one `--source` replaces the compile checkpoint's `accepted_notes` with the
resolved raw markdown sources, deduped by source ID in selector order, before
the article is compiled. On a fresh vault, `compile` may create the research
checkpoint only when a positional topic or `--topic` supplies the topic seed.

An additive version 7 update gives `compile` an LLM explainer layer over its
deterministic skeleton. Default execution routes one bounded completion through
the daemon text lane; `--no-ai` selects the structural path. Generated prose is grounded
against the accepted sources before it reaches the vault: `[source: <path>]`
markers that match an accepted source are rewritten to vault wiki links,
invented citations are stripped, and prose sections left uncited get a
fallback citation to the lexically closest source. A failed attempt keeps the
deterministic skeleton and marks the page frontmatter with
`degraded`/`degraded_sources` (`model_provider_unavailable`); `--no-ai`, an
unresolvable `auto` route, or a compile with no accepted sources stays
structural by intent with no degradation markers. The compile payload gains an
`ai` block (`requested_mode`, `route`, `status`, `model`, `error`, citation
grounding counts) and the `prompt` object reports `tokens_estimated` and
`truncated_sources` budget accounting.

Version 4 added the `librarian`, `review-report`, and `citation-quality`
surfaces to the daemon contract. These entries pin dependency/degradation
classification fields and advertise trust, freshness, audit, source, and
degradation payload keys where the surface emits them.

Version 3 added code-grounded payload fields to `ask` and `graph-context`.
`graph-context` returns `code_edges` and `code_citations` alongside `context`,
`source_bundle`, `trust`, `freshness`, `audit`, `warnings`, and `degradation`.

The `ask`, `graph-context`, `benchmark`, `librarian`, `upkeep`, `recap`,
`review-report`, and `citation-quality` entries pin their dependency and
degradation rows in the machine-readable contract. `ask` treats model
synthesis, semantic vectors, and the FalkorDB graph boost as optional signals,
and can degrade to retrieval-only hits with grounded citations. `librarian`
keeps deterministic upkeep proposals available while skipping unavailable
checks. `upkeep` records per-page synthesis failures and continues the run;
with AI off it writes structural skeleton pages. `recap` keeps its
deterministic session listing when synthesis is off or fails. `review-report`
can emit a report without the risky-shift section when graph analytics are
unavailable. `citation-quality` can skip unavailable quality sections
independently.

## Scope

`gwiki` accepts `--project <ROOT>` and `--topic <NAME>`.

No scope flag means detect the project from the current working directory. Bare
`--project` means the current directory. `--scope` is not part of this contract.

Every scoped JSON result consumed by Gobby carries a resolved `scope` identity
with `kind` and `id`.

## AI Routing

AI route flags use `auto|daemon|direct|off`. `direct` means any
OpenAI-compatible endpoint, local or remote. There is no `local` route.

## Dependency & Degradation Classification

PostgreSQL and canonical Markdown are hard dependencies for every Parity+
surface. Their absence is a command failure, not a degraded result. The tables
below classify the remaining hard dependencies, optional dependencies, degraded
output shape, and user-visible degradation metadata for every Parity+ command
surface and generated-page output surface.

Multimodal providers for transcription, vision, and video are not dependencies
of any Parity+ command or generated-page surface. They are used only by source
ingest (`gwiki collect` of audio, image, or video inputs) and therefore never
change Parity+ output. Every row records this as `none - not used` so the
non-dependency is explicit.

Each command deliverable must embed its row inline, and contract-registered
command surfaces must pin the same classification in
`crates/gwiki/contract/gwiki.contract.json`. Each generated-page deliverable must
embed its row inline; generated-page degradation metadata lives in page YAML
frontmatter rather than the CLI JSON contract.

The machine-readable side of the generated-page frontmatter contract lives in
`gobby_core::codewiki_contract`: the shared key/value constants (`provenance`,
`provenance_truncated` — emitted only when a page rolls up more provenance
files than the per-page cap, recording the omitted count — `generated_by:
gwiki-code`, `trust: generated`, `freshness: indexed`,
`degraded`/`degraded_sources`) and a golden page fixture. gwiki pins its
frontmatter emitter and audit parsers against the golden fixture so producer and
consumer cannot drift silently. The `generated_by: gwiki-code` value is the
persisted page-format identifier; pages generated before the ownership move
still carry the legacy pre-move marker on disk and are treated as untrusted
legacy output by the audit.

### Command Surfaces

| Command | Hard dependencies | Optional dependencies | Multimodal | Degraded output shape | Degradation metadata |
| --- | --- | --- | --- | --- | --- |
| `code` | PostgreSQL code index, Markdown vault | FalkorDB, model synthesis | none - not used | deterministic structural pages remain available when optional signals fail | `degraded_pages[]` |
| `graph` | PostgreSQL, Markdown | FalkorDB, embeddings/Qdrant | none - not used | available nodes/edges; missing edge classes empty and flagged | `degraded`, `degraded_sources[]` in `graph.json`/`GRAPH_REPORT.md` |
| `graph-context` | PostgreSQL | FalkorDB, shared code graph | none - not used | wiki-link-only neighborhood | `warnings[]`, `degradation{degraded,degraded_sources[]}` |
| `benchmark` | PostgreSQL, seeded project | FalkorDB, Qdrant+embeddings, model | none - not used | metrics for available dimensions only | per-metric `available`, `degraded_sources[]` |
| `ask` | PostgreSQL | model synthesis, Qdrant+embeddings, FalkorDB graph boost | none - not used | model off emits retrieval-only hits with grounded citations; signal loss falls back to BM25-only evidence | `degraded`, `degraded_sources[]`, `truncated`, `truncated_components[]` on answer |
| `compile` | canonical Markdown vault, research session | model synthesis (daemon text lane or direct OpenAI-compatible endpoint) | none - not used | explainer failure keeps the deterministic skeleton with degradation markers; AI off compiles the structural article without markers | `ai.status`/`ai.error` in payload; page frontmatter `degraded`/`degraded_sources[]` |
| `librarian` | PostgreSQL, vault | FalkorDB/code graph, Qdrant+embeddings, model | none - not used | each check skipped independently with a note | per-check `available` in proposals report |
| `upkeep` | vault | Qdrant+embeddings, model synthesis | none - not used | missing semantic backend skips near-duplicate checks with a note; AI off writes structural skeleton pages; per-page failures are recorded and the run continues | `notes[]`, `clusters[].error`, `ai` |
| `recap` | vault | model synthesis | none - not used | AI off or failed still writes the deterministic session listing with a fallback overview; a day with no sessions writes no page and is not an error | `synthesis`, `notes[]`, `ai` |
| `review-report` | PostgreSQL, change set | FalkorDB/code graph and analytics | none - not used | report without risky-shift section | `degraded`, `degraded_sources[]` on report |
| `citation-quality` | PostgreSQL | credibility signals, model contradiction detection | none - not used | per-section skipped with a note | per-section `available` |

### Codewiki Generated-Page Surfaces

These are `gwiki code` output surfaces. PostgreSQL code index and Markdown vault
are hard dependencies for every page.

| Generated page | Hard dependencies | Optional dependencies | Multimodal | Degraded output shape | Degradation metadata |
| --- | --- | --- | --- | --- | --- |
| `code/_architecture.md` | PostgreSQL code index, Markdown vault | model subsystem summaries, FalkorDB/graph cross-cluster edges | none - not used | structural module summaries plus reduced or empty subsystem diagram | frontmatter `degraded`/`degraded_sources`; `generated_by: gwiki-code` |
| `code/_onboarding.md` | PostgreSQL code index, Markdown vault | `gobby_core::graph_analytics` centrality | none - not used | structural entry-point list with no ranked reading order | frontmatter `degraded`/`degraded_sources` |
| `code/_hotspots.md` | PostgreSQL code index, Markdown vault | `gobby_core::graph_analytics` hotspots, god nodes, and bridges | none - not used | explicit "analytics unavailable" note | frontmatter `degraded`/`degraded_sources` |
| `code/_ownership.md` | PostgreSQL code index, Markdown vault | git repo and blame through `gix`, `CODEOWNERS` file | none - not used | CODEOWNERS-only output, "unknown ownership", or `partial` when capped or timed out | frontmatter `degraded`/`degraded_sources`; `partial` |
| `code/_changes.md` | PostgreSQL code index, Markdown vault | prior `CodewikiMeta` snapshot for diff baseline, FalkorDB/graph neighborhood fingerprint | none - not used | first run without prior snapshot emits an accepted baseline page, not a failure; graph-off output drops the neighborhood diff | frontmatter `baseline`/`degraded` |

## Drift Checks

Both the CLI and daemon tests load this contract. New daemon-facing flags or JSON
keys should update this document, the JSON contract, and the corresponding drift
tests in the same change.

The pinned `ask`, `code`, `graph-context`, `benchmark`, `librarian`, `upkeep`,
`recap`, `review-report`, and `citation-quality` command entries record their
classification rows with top-level `hard_dependencies`, `optional_dependencies`,
`multimodal`, and `degradation` fields so daemon consumers can detect dependency
and degradation drift directly from the contract JSON.

`trust` distinguishes curated from digest red links:
`link_summary.curated_broken_link_count` counts broken links on pages outside
`knowledge/sources/`, and only those gate `attention_required`. One-off
`[[Entity]]` red links inside session digests sit below the upkeep clustering
threshold by design — they keep the `broken_links` degradation label and the
total `broken_link_count`, classify the vault as `degraded`, and are
enumerated by `librarian`.

_Last verified: 2026-08-09_
