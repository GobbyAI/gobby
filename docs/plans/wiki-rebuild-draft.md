# Wiki rebuild — discussion draft

Saved 2026-09-06 from Gobby session #12034; documentation task #21910.

Status: unfinished discussion record, not an approved or implementation-ready
plan. This preserves decisions, research progress, and remaining design work so
planning can resume without repeating the conversation. It is not a registered
implementation plan, task manifest, parity certification, or authorization to
activate production writers. Full planning depth was selected. No replacement
wiki implementation or benchmark was performed during this discussion.

## Confirmed decisions

- Rebuild one hub-owned wiki system for human understanding and supplementary
  agent retrieval, aligned with `ROADMAP.md`. `gcode` remains agents' primary
  code-intelligence tool.
- Durable engine: Rust `gwiki`, with a versioned contract and thin, replaceable
  daemon adapters during the `gdaemon` migration.
- PostgreSQL owns canonical revisions. Original source bytes and Markdown
  exports live under hub-owned `files_home`. Filesystem edits are not
  automatically authoritative database edits.
- Publish automatically after validation. AI replacements of human-edited
  articles require review; optimistic concurrency protects intervening edits.
- Code-wiki parity+ targets: Graphify, Archify, Understand Anything, and the
  DeepWiki family. General-knowledge parity+ targets: llm_wiki, WeKnora, RAGFlow.
  This requires explicit capability and quality acceptance cases, not a short
  list of selected adoption candidates.
- Credit and reuse current `gcode` and Gobby services: AI routing, normal agents,
  agent definitions, cron, pipelines, progress, cancellation, and recovery.
  Do not assume the deleted wiki engine or extraction subsystem still exists.
- CLI, the `gobby-wiki` MCP wrapper, and necessary authenticated daemon APIs are
  in scope. The custom web UI and all wiki-based enhancements to the current
  herdr client port are deferred. No wiki UI work in `gclient` or the terminal
  client belongs to this rebuild's present scope.
- Obsidian compatibility comes first. The user chose a **live managed vault**,
  refreshed after successful publication, rather than snapshot-only exports.
- Repository generation follows a configured committed branch per project,
  pinned to a commit for each build. Gobby follows `0.5.0`, not the remote default
  `main`. No working-tree-preview product was selected.
- Maintenance runs nightly and on demand using existing scheduling/pipelines.
  Explicit new imports run immediately. Unchanged evidence and generation
  settings must produce a no-op with zero model calls.
- Wiki remains a concurrent roadmap side quest. Implementation approval and
  production activation approval remain separate; existing explicit grant
  reservations remain unchanged and outside the rebuild's critical path.

## Baseline after retirement

The user states the old wiki has been removed. Task inspection corroborated:

- #21771 completed full legacy wiki retirement, including engine, schema,
  public surfaces, integration, and runtime content.
- #19670 and #18790 are closed as obsolete. The original draft's instruction to
  reconcile/reuse those epics must not be followed literally. Plan a fresh
  replacement task tree later; do not reopen obsolete work or repeat retirement.
  The original #18779 reference has not been separately revalidated here.
- Historical source is available at Git tag
  `legacy-wiki-before-retirement-21771`, for history only. It is neither a
  compatibility contract nor an assumed reusable subsystem.
- The current Rust workspace has no `gwiki` crate. Migration
  `crates/gcore/assets/schema/migrations/426_retire_legacy_wiki.sql` retires the
  old `gwiki_*` tables. Choose a fresh schema deliberately rather than restoring
  those tables by accident.

Fresh content comes from current repositories and newly supplied sources. No
legacy conversion, reimport, or compatibility obligation exists. Preserve
canonical session summaries, revisions, handoffs, transcript archives, and
independently used redaction. Do not restore automatic wiki session ingestion,
recaps, flat-file session-summary mirrors, or their freshness-repair machinery.

## Product and identity contract to carry forward

```text
files_home/wiki/
├── general/
└── projects/
    ├── gobby/
    └── <name-as-recorded-in-projects-table>/
```

Project directories use recorded project names. UUIDs remain internal stable
identifiers. Renames preserve links and refuse destination collisions. Gobby's
repository wiki is required; managed project wikis are opt-in.

Separate wiki identity, repository identity, and execution checkout. Hub or
standalone owns canonical state and publication. Nodes perform machine-local
checkout work; remote readers use authenticated hub APIs, not shared mounts.
General operations require no checkout.

Represent sources, source revisions, articles, article revisions, and citations
explicitly. Preserve original imported bytes and locators: code ranges,
document pages, slides, sheets, and media timestamps. Reads/search identify the
owning wiki and published revision. Writes name one destination and supply a
revision precondition.

`gwiki` performs bounded synthesis and publication validation. Open-ended
research and interactive questions belong to normal Gobby agents, using the
wiki's retrieval tools as evidence. Do not introduce another wiki queue or agent
runtime. Reuse PostgreSQL, Qdrant, and FalkorDB where their existing contracts fit.

### Search semantics

| Scope | Required behavior |
| --- | --- |
| `repo` | Search the explicitly selected/contextual repository wiki. |
| `general` | Search the general wiki without needing a repository or checkout. |
| `repo+general` | Default when repository context exists; combine that repository with general knowledge. |
| `all` | Search all available wikis, preserving each result's owner and revision. |

Repository selection is independent of agent working directory. With no
repository context, an omitted scope searches general knowledge; an explicitly
repo-dependent scope/filter requires a repository. An unavailable project wiki
is reported explicitly while general retrieval remains available. All CLI, MCP,
and HTTP surfaces must agree. Resolve each result against its own wiki; no
single-vault retrieval assumption.

## Live managed Obsidian vault

The user accepted the live-vault tradeoff: a stable folder refreshed from
successful hub publications. Hub CLI/MCP reads remain pinned to a complete
published revision, but Obsidian can briefly observe a mixture of article
revisions while individual files are replaced. An interrupted export can leave
that mirror mixed until recovery. Do not describe the whole vault as atomically
published when only individual file replacement is atomic.

Required design properties to preserve this decision safely:

- Managed-file manifest, progress/checkpoint state, and resumable recovery tied
  to a published revision. Exact representation remains to be designed.
- Local-edit collision detection. Never silently overwrite locally changed
  generated files. Preserve user notes, `.obsidian` settings, bookmarks, and
  layouts; cleanup applies only to known managed files under validated paths.
- PostgreSQL remains authoritative. Automatic bidirectional Obsidian ingestion
  was not selected. An explicit CLI/MCP import-as-revision operation was proposed
  for local edits, but its detailed review/conflict behavior is not settled.
- Portable Markdown links, evidence references, readable diagrams, and
  unambiguous paths for colliding titles. Rendered Mermaid alone does not supply
  Obsidian Graph relationships; ordinary note links must carry relevant edges.
- Project rename and source removal must update managed links safely without
  losing user content. Recovery and collision handling need explicit tests.

Additional snapshot export for archives/sharing was suggested, not separately
confirmed as required scope. Avoid symlink-switch tricks as the publication
mechanism. The live-vs-snapshot decision affects filesystem consistency and
maintenance, not synthesis/model cost.

Primary compatibility references:
[internal links](https://help.obsidian.md/Linking+notes+and+files/Internal+links),
[advanced syntax and Mermaid](https://help.obsidian.md/advanced-syntax),
[Graph view](https://help.obsidian.md/plugins/graph),
[Canvas](https://help.obsidian.md/plugins/canvas), and
[symlink cautions](https://help.obsidian.md/Files+and+folders/Symbolic+links+and+junctions).

## Parity+ research baseline

The following repository heads were recorded during read-only research on
2026-09-06. These pin research inputs, not completed comparator runs. Findings
are preliminary primary-source documentation observations, not measured quality
or an exhaustive acceptance matrix.

| Comparator | Recorded branch and commit | Capability families to map and evaluate |
| --- | --- | --- |
| [Graphify](https://github.com/Graphify-Labs/graphify) | `v8` · `c9f99018774e2e0380e9f65b3959944559a0d5f6` | Graph traversal/path/explanation, communities and hubs, rationale/document references, provenance, multimodal evidence, incremental refresh and exports. |
| [Archify](https://github.com/tt-a1i/archify) | `main` · `c6519401f7b91b9d43011657880893b0a8955548` | Architecture, workflow, sequence, data-flow and lifecycle diagrams; typed intermediate representation; deterministic rendering/layout validation; revision-grounded change views and last-good delivery. |
| [Understand Anything](https://github.com/Egonex-AI/Understand-Anything) | `main` · `07edf82a04371b6f69779b067bdc8a1a8753a9db` | Structural and domain/business understanding, guided tours, semantic exploration, impact/change explanations, audience-appropriate detail. The older Lum1104 URL redirects here. |
| [DeepWiki-Open](https://github.com/AsyncFuncAI/deepwiki-open) | `main` · `d92819a9c9f3b99416e3580ff235fc9d3adf8b89` | Hierarchical repository documentation, explanation, diagrams and grounded questions. |
| [OpenDeepWiki](https://github.com/AIDotNet/OpenDeepWiki) | `main` · `75840e5e86213ca40ace9d5036b1f52603f8d038` | Repository-to-wiki generation, structured navigation and evidence-backed exploration. |
| [CodeWiki](https://github.com/FSoft-AI4Code/CodeWiki) | `main` · `2584854d7538dc3e3e8e6839cf8590b0cd12a431` | Additional repository-documentation comparator from the historical DeepWiki-family bakeoff; exact acceptance mapping still needed. |
| [llm_wiki](https://github.com/sdsrss/llm_wiki) | `main` · `428c0bb7a3952fd93844b79f389178b601c4bb4c` | Immutable raw sources; coherent linked source/entity/concept/comparison articles; citations, incremental invalidation, lint, graph navigation, whole-page retrieval, Markdown/Obsidian and CLI/MCP. |
| [WeKnora](https://github.com/Tencent/WeKnora) | `main` · `3d3bb7f6d1acca8caa84bb73b189fe46c30967a9` | Wiki synthesis, interconnected Markdown, edit/revision/rollback, multimodal ingestion, metadata, hybrid and hierarchical retrieval/reranking, graph and reprocessing. |
| [RAGFlow](https://github.com/infiniflow/ragflow) | `main` · `0c28d59ea1d362d9b6aa7481eed48c7fd9a95f0b` | Structured document parsing/tables/OCR, chunk inspection and curation, hybrid/reranked/graph retrieval, citation grounding, retrieval evaluation and workflow integration. |

The exact `llm_wiki` repository remains a confirmation point: `sdsrss/llm_wiki`
is the current working interpretation of the user's underscore name. Historical
local evidence used `Pratiyush/llm-wiki`, a different, session-oriented project;
do not silently substitute it. CodeWiki is an additional research comparator,
not a separately confirmed user-named repository.

For every relevant capability, the next plan must record: comparator/version,
observable acceptance case, existing Gobby capability credited, remaining gap,
implementation/evaluation owner, and evidence. Include output quality, not only
API existence. Deferred custom interfaces are not delivered parity. Conversely,
do not infer authorization to recreate entire tenant/admin products, every
connector, or separate agent platforms; reuse existing Gobby services and
resolve any material auxiliary scope explicitly.

Historical evidence under `docs/evidence/wiki-bakeoff-2026-06/` includes
`ADOPTION-CANDIDATES.md`, `graphify-revalidation-2026-08.md`, and DeepWiki,
OpenDeepWiki, CodeWiki, and llm-wiki setup notes. Those results are historical,
not replacement certification. New comparisons need common public/synthetic
corpora and recorded source versions, model/provider/settings, and costs.

## Current reuse audit and gaps

These are navigation anchors from the current-source audit, not a final target
inventory. Re-read indexed symbols and consumers before implementation.

### Existing evidence and graph functionality

`crates/gcode/src/models.rs` already defines `ProjectionProvenance` and
`ProjectionMetadata` carrying provenance, confidence, source system, source
file/line/symbol, and matching method. Graph payload links carry metadata in
`crates/gcode/src/graph/code_graph/payload.rs`. Do not claim relationship
provenance is wholly absent or rebuild delivered callers/callees, imports,
paths, blast radius, communities, or token-budgeted retrieval.

`crates/gcode/src/codewiki_facts/` survives as a read-only fact surface despite
its name. Typed rationale and ADR/RFC/document-reference evidence were not found
in the scoped audit; determine precise missing contracts before extending gcode.
`crates/gcode/src/index/indexer/overlay.rs` and checkout fencing are relevant to
commit-pinned evidence without disturbing an active agent index; exact build
integration is unfinished.

### Existing AI and orchestration

`src/gobby/ai/registry.py` exposes embedding, vision extraction, transcription,
translation, text generation, tool chat, and agent capabilities.
`crates/gcore/src/ai/daemon/operations.rs` provides daemon-backed transcribe,
describe-image, generate, and embed operations; existing generation facilities
live under `crates/gcore/src/ai/generation/`. Preserve actor/provider/model and
runtime-grant routing, rather than adding wiki-owned provider machinery.

`src/gobby/scheduler/executor.py` runs installed DB pipeline definitions,
records cron/session/execution identity, and supports overlap handling.
`src/gobby/workflows/pipeline_state.py` retains execution/step inputs, outputs,
definition snapshots, errors, and approval state. Existing pipeline steps cover
exec, prompt, nested pipeline, MCP, and wait operations.
`src/gobby/mcp_proxy/tools/workflows/_pipeline_execution.py` supports explicit
failed-pipeline resume using the same execution identity; startup recovery marks
orphan running executions failed.

`src/gobby/storage/checkpoints.py` is a git/task checkpoint mechanism, not a
general extraction checkpoint store. Associate wiki staging with existing
pipeline executions and immutable stage artifacts; exact persistence is open.
Also resolve checkout-free general-wiki execution attribution without inventing
a fake repository or new agent runtime.

### Storage, safety, and projections

Read `docs/architecture/hub-owned-files-home.md`. The hub root is provisioned
explicitly; writers must not silently recreate an unavailable root. Reuse the
descriptor-based containment/publication patterns in `src/gobby/paths.py` and
authenticated remote forwarding in `src/gobby/files_home_proxy.py`. Respect
role-bearing daemon/maintenance ownership and typed hub-unavailable failures.

`src/gobby/storage/projects.py` currently updates project names in the DB; the
new wiki needs coordinated directory/link rename and destination-collision
semantics. Exact API and transaction/recovery design remains open.

New Rust-owned schema work must include plan-coverage derived carriers:
`crates/gcore/assets/schema/catalog.manifest.json`,
`crates/gcore/src/grant/bundle.rs`, `crates/gcore/tests/schema_contract.rs`,
`crates/gdaemon/tests/cli_contract.rs`, and
`src/gobby/storage/schema_expected_identity.json`. Select migration numbering
at implementation time, accounting for concurrent work.

Retirement tests in `tests/test_wiki_retirement_contract.py`,
`tests/storage/test_wiki_schema_retirement.py`, and
`tests/code_index/test_codewiki_modules_retired.py` need careful contract
reconciliation when new interfaces arrive. Preserve prohibitions on old behavior;
do not disable retirement tests wholesale to add the authorized replacement.

### Extraction and rendering are not solved yet

The dependency audit did not establish surviving PDF/Office parsing foundations.
The local machine has `ffmpeg`/`ffprobe`, but the checked PDF command-line tools,
LibreOffice, and Pandoc were not found. Existing vision/audio routing is useful,
but does not by itself implement structured PDF, Office, or video extraction.
Parser libraries, limits, packaging, and provider fallback remain open. Xberg
(formerly Kreuzberg) was an exploratory candidate, not a selected dependency.

`crates/gcore/src/mermaid.rs` performs lightweight validity checks, not full
render/layout validation. Diagram acceptance must exercise actual rendering.

## Intended implementation sequence to refine

This carries forward the user's sequence without claiming complete targets,
dependency ordering, or acceptance IDs. Convert it to the typed plan contract
only after the remaining design decisions and audits are resolved.

1. Establish namespaces, explicit evidence/revisions, and staged publication,
   exposed through the CLI/MCP/necessary daemon contract. Immediately prove two
   isolated examples before expanding breadth: a Gobby overview plus major
   workflows grounded in current source; and a general article synthesizing
   overlapping sources including a disagreement. Both must be readable,
   searchable, and traceable to evidence.
2. Build human-first repository generation using gcode, current source, docs,
   comments/docstrings, and ADRs. Explain purpose, components, business/domain
   workflows, tours, architecture, sequence, data flow, and lifecycle. Include
   revision-supported change explanations and source-linked diagrams. File
   descriptions support navigation; generate no persistent per-file or
   per-symbol wiki pages. Fill only audited gcode evidence gaps.
3. Build general ingestion for text/Markdown, HTML/URLs, PDF, Office, images,
   audio, and video. Extract evidence, identify concepts/claims, plan article
   updates, synthesize across sources, validate citations. Explicitly expose
   contradictions, incomplete extraction, and missing providers. Produce
   interconnected articles, hierarchies, graphs, and timelines where supported.
4. Complete consistent multi-wiki retrieval and human curation through CLI/MCP
   and daemon APIs, plus the live managed Obsidian vault. Provide evidence/source
   access, guided exploration, revision diffs, rollback, human-edit revisions,
   AI review protection, and optimistic concurrency. Route questions to normal
   agents. Custom web/herdr reader work remains deferred.
5. Harden publication and maintenance: complete published revisions, last-good
   preservation, revision-consistent search/graph projections, zero-model-call
   no-ops, dependent-article invalidation including documentation/comments,
   existing execution identities/progress, durable retry/resume, and source
   removal that preserves claims still supported by surviving evidence. Recover
   the managed vault separately from authoritative publication.
6. Prepare a fresh replacement task tree and separately approved activation.
   Verify installed binaries and scoped rollback in isolated rehearsal. Any
   production cutover must obtain approval for exact affected files/derived
   records; no shared datastore wipe or unrelated platform mutation. Recheck that
   retired writers remain absent rather than repeating the completed retirement.

## Verification obligations

- Comprehension: a human can explain each fixture repository's purpose, major
  components, and principal workflows using the wiki and evidence links.
- Comparator coverage: explicit parity+ acceptance cases for all named families,
  evaluated on shared public/synthetic corpora with pinned versions/settings.
  Set measurable quality thresholds before claiming parity.
- Grounding: reject fabricated citations, identify stale evidence, surface
  contradictions, and distinguish inference from directly supported facts.
- Retrieval: all four scopes, cross-repository selection independent of checkout,
  absent checkout, unavailable project wiki, rename, and result identity.
- Reliability: failure injection during extraction, synthesis, publication,
  indexing, and export; cancellation, restart/explicit resume, duplicates,
  concurrent human edits, and interrupted/local-edited managed vault recovery.
- Security: URL/redirect safeguards, path containment, malformed/oversized
  inputs, source-preview safety, original-byte handling, and existing redaction.
- Topology: standalone and hub/node behavior with typed hub-unavailable errors.
- Obsidian: readable Markdown/diagrams, correct normal links and evidence
  locators, collision-safe names, managed updates, and preserved user content.

Use fresh isolated fixtures and replacement state, never the live daemon DB or
real vault. Run targeted tests, not the full pytest suite. No tests or comparator
benchmarks were run during the design discussion; draft-save validation is
documentation-only and does not validate the future implementation.

## Remaining work and resume instructions

1. Confirm the exact `llm_wiki` URL and finish the primary-source capability
   audit. Expand the preliminary table into acceptance cases with reuse credit,
   quality metrics, and explicit treatment of auxiliary/platform capabilities.
2. Finish extraction dependency/security/provider choices; do not resurrect
   archived wiki extraction by assumption. Define original-byte and locator
   contracts, extraction completeness reporting, and input/resource limits.
3. Specify versioned Rust/daemon/CLI/MCP contracts, schema, staged publication,
   revision-gated projections, human review, and failure/recovery state. Audit
   consumers using current gcode before choosing exact file/symbol targets.
4. Resolve general-wiki pipeline attribution without checkout, pinned-branch
   evidence/index ownership, stage checkpoints, and maintenance invalidation.
5. Detail live-vault manifest/recovery, local-edit conflicts, rename behavior,
   and whether explicit import-as-revision and optional snapshot export are
   included. Keep database authority and the accepted mixed-export window clear.
6. Define synthesis/model budgets and repeatable evaluation settings/thresholds.
   Confirm the completed Decision Record, then author and review the Full typed
   implementation plan under `docs/contracts/plan-coverage.md`. This discussion
   draft is not a substitute for that decision-complete plan.
7. Obtain the respective implementation and activation approvals, preserving
   manual activation and the nonblocking roadmap relationship. Do not reopen
   obsolete wiki epics or alter explicit reserved grants.

The immediate user request is only to save this discussion and reference it in
`set_handoff` **without `clear_session=true`**. Use `clear_session=false`; do not
automatically start implementation after writing the handoff.
