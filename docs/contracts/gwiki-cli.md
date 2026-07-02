# gwiki CLI Contract

The machine-readable contract lives at `crates/gwiki/contract/gwiki.contract.json`.
`gwiki contract --format json` must emit the same contract version and contents.

## Version

`contract_version`: 7

Version 7 covers the daemon-consumed surface:

- `contract`
- `index`
- `search`
- `ask`
- `read`
- `refresh`
- `ingest-file`
- `ingest-url`
- `collect`
- `compile`
- `audit`
- `graph`
- `graph-context`
- `health`
- `librarian`
- `review-report`
- `citation-quality`
- `sources`
- `backlinks`
- `status`
- `trust`
- `remove-source`

Version 5 makes `search` the agent retrieval primitive and rebuilds `ask` as a
thin bounded-evidence layer over it. `search` results carry bounded
query-token snippets (never full document bodies), provenance (`wiki_page`,
`source_path`, `result_type`, `sources`, `explanations`), and top-level
`code_citations` derived from the returned hits only. `ask` retrieves top-k
hits, assembles a prompt capped at `prompt_token_budget` (~12K estimated
tokens), runs a single completion through the daemon route or a direct
OpenAI-compatible endpoint, and reports `evidence`, `prompt_tokens_estimated`,
and `truncated`/`truncated_components` accounting; the whole-scope graph
expansion (`related_pages`, `code_edges`, `gaps`, `stale_candidates`,
`suggested_questions`) is gone. The `research` command is removed — agents
compose `search` and `read` for retrieval and deposit results through
`collect`/`ingest-file`; `compile` still compiles accepted research notes and
can select ingested manifest records with repeatable
`--source SOURCE_ID_OR_PATH`.

An additive version 7 update gives `compile` a source-selection surface.
`--source` selectors resolve as exact source ID, derived raw path
`raw/<id>.md`, then exact manifest `location`/`canonical_location`. Passing at
least one `--source` replaces the compile checkpoint's `accepted_notes` with the
resolved raw markdown sources, deduped by source ID in selector order, before
the article is compiled. On a fresh vault, `compile` may create the research
checkpoint only when a positional topic or `--topic` supplies the topic seed.

An additive version 7 update gives `compile` an LLM explainer layer over its
deterministic skeleton. `--ai auto|daemon|direct|off` routes one bounded
completion (the same ~12K estimated-token budget as `ask`) through the daemon
text lane or a direct OpenAI-compatible endpoint. Generated prose is grounded
against the accepted sources before it reaches the vault: `[source: <path>]`
markers that match an accepted source are rewritten to vault wiki links,
invented citations are stripped, and prose sections left uncited get a
fallback citation to the lexically closest source. A failed attempt keeps the
deterministic skeleton and marks the page frontmatter with
`degraded`/`degraded_sources` (`model_provider_unavailable`); `--ai off`, an
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

The `ask`, `graph-context`, `librarian`, `review-report`, and
`citation-quality` entries pin their dependency and degradation rows in the
machine-readable contract. `ask` treats model synthesis, semantic vectors, and
the FalkorDB graph boost as optional signals, and can degrade to
retrieval-only hits with grounded citations. `librarian` keeps deterministic
upkeep proposals available while skipping unavailable checks. `review-report`
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
gcode-codewiki`, `trust: generated`, `freshness: indexed`,
`degraded`/`degraded_sources`) and a golden page fixture. gcode pins its
frontmatter emitter against the golden fixture and gwiki pins its
frontmatter/audit parsers against it, so producer and consumer cannot drift
silently.

### Command Surfaces

| Command | Hard dependencies | Optional dependencies | Multimodal | Degraded output shape | Degradation metadata |
| --- | --- | --- | --- | --- | --- |
| `graph` | PostgreSQL, Markdown | FalkorDB, embeddings/Qdrant | none - not used | available nodes/edges; missing edge classes empty and flagged | `degraded`, `degraded_sources[]` in `graph.json`/`GRAPH_REPORT.md` |
| `graph-context` | PostgreSQL | FalkorDB, shared code graph | none - not used | wiki-link-only neighborhood | `warnings[]`, `degradation{degraded,degraded_sources[]}` |
| `benchmark` | PostgreSQL, seeded project | FalkorDB, Qdrant+embeddings, model | none - not used | metrics for available dimensions only | per-metric `available`, `degraded_sources[]` |
| `ask` | PostgreSQL | model synthesis, Qdrant+embeddings, FalkorDB graph boost | none - not used | model off emits retrieval-only hits with grounded citations; signal loss falls back to BM25-only evidence | `degraded`, `degraded_sources[]`, `truncated`, `truncated_components[]` on answer |
| `compile` | canonical Markdown vault, research session | model synthesis (daemon text lane or direct OpenAI-compatible endpoint) | none - not used | explainer failure keeps the deterministic skeleton with degradation markers; AI off compiles the structural article without markers | `ai.status`/`ai.error` in payload; page frontmatter `degraded`/`degraded_sources[]` |
| `librarian` | PostgreSQL, vault | FalkorDB/code graph, Qdrant+embeddings, model | none - not used | each check skipped independently with a note | per-check `available` in proposals report |
| `review-report` | PostgreSQL, change set | FalkorDB/code graph and analytics | none - not used | report without risky-shift section | `degraded`, `degraded_sources[]` on report |
| `citation-quality` | PostgreSQL | credibility signals, model contradiction detection | none - not used | per-section skipped with a note | per-section `available` |

### Codewiki Generated-Page Surfaces

These are gcode `codewiki` output surfaces, not commands. PostgreSQL code index
and Markdown vault are hard dependencies for every page.

| Generated page | Hard dependencies | Optional dependencies | Multimodal | Degraded output shape | Degradation metadata |
| --- | --- | --- | --- | --- | --- |
| `code/_architecture.md` | PostgreSQL code index, Markdown vault | model subsystem summaries, FalkorDB/graph cross-cluster edges | none - not used | structural module summaries plus reduced or empty subsystem diagram | frontmatter `degraded`/`degraded_sources`; `generated_by: gcode-codewiki` |
| `code/_onboarding.md` | PostgreSQL code index, Markdown vault | `gobby_core::graph_analytics` centrality | none - not used | structural entry-point list with no ranked reading order | frontmatter `degraded`/`degraded_sources` |
| `code/_hotspots.md` | PostgreSQL code index, Markdown vault | `gobby_core::graph_analytics` hotspots, god nodes, and bridges | none - not used | explicit "analytics unavailable" note | frontmatter `degraded`/`degraded_sources` |
| `code/_ownership.md` | PostgreSQL code index, Markdown vault | git repo and blame through `gix`, `CODEOWNERS` file | none - not used | CODEOWNERS-only output, "unknown ownership", or `partial` when capped or timed out | frontmatter `degraded`/`degraded_sources`; `partial` |
| `code/_changes.md` | PostgreSQL code index, Markdown vault | prior `CodewikiMeta` snapshot for diff baseline, FalkorDB/graph neighborhood fingerprint | none - not used | first run without prior snapshot emits an accepted baseline page, not a failure; graph-off output drops the neighborhood diff | frontmatter `baseline`/`degraded` |

## Drift Checks

Both the CLI and daemon tests load this contract. New daemon-facing flags or JSON
keys should update this document, the JSON contract, and the corresponding drift
tests in the same change.

The pinned `ask`, `graph-context`, `librarian`, `review-report`, and
`citation-quality` command entries record their classification rows with
top-level `hard_dependencies`, `optional_dependencies`, `multimodal`, and
`degradation` fields so daemon consumers can detect dependency and degradation
drift directly from the contract JSON.

_Last verified: 2026-06-14_
