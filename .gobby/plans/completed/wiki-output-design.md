# Wiki Output Design — Orientation + Narrative Generated Artifacts

> **Plan ID:** wiki-output-design

## Overview
`kind: framing`

Design contract and realization plan for the redesigned generated wiki output under
epic #19664 (parent umbrella #19670). Replaces today's ~3,300 per-file/module pages
with an orientation + narrative model of ~50–90 pages: a compact repository
orientation spine, narrative module/concept pages, guided tours, an insight report,
and a typed graph projection. Source files remain evidence — addressable through
resolvable `[file:line]` citations and gcode facts — never wiki pages.

Design inputs: the June bakeoff evidence (`docs/evidence/wiki-bakeoff-2026-06/ADOPTION-CANDIDATES.md`,
candidates C1–C9 and weakness W1), the Understand Anything comparison retained in
#18871, DeepWiki-Open page anatomy, and the prior draft
`.gobby/plans/wiki-codewiki-restructure.md` (superseded by this plan; still-valid
mechanics absorbed by reference).

## Constraints
`kind: framing`

- **No persistent per-file pages.** `code/files/**` is not regenerated. Files stay
  evidence via citations and gcode outlines/facts/symbol summaries.
- **Storage namespaces stay split.** `code/**` is generated and replaced wholesale by
  regeneration under a build manifest; `knowledge/**` stays curated
  (compile/upkeep lifecycle, trust, archival). Unification happens at retrieval,
  navigation, and graph projection only.
- **Page taxonomy** (manifest-driven): `overview | module | concept | tour | insights`
  plus deterministic projections (`layers`, `features`, `deprecations`, `changes`,
  `hotspots`, `ownership`, `infrastructure`).
- **Diagram kinds:** architecture/component map, key data/control flows, class/type
  hierarchies. No dedicated dependency/import diagram — coupling renders as
  uses/used-by tables plus on-demand gcode coupling views. Diagrams degrade to a
  bounded top-N rendering with explicit truncation labels, never full suppression
  (bakeoff C4).
- **Compact summaries** follow the #18871 contract: single-line `summary`
  frontmatter, ≤180 Unicode characters, word-boundary cap, no structure.
- **Prose contracts** follow #18871: one short paragraph per explanatory section,
  3–6 walkthrough steps per tour, ≤6 rows per generated table except the key-symbol
  reference table (≤24 rows) and the module files table (≤12 rows), 1–3
  next-reading links, no repeated claim across sections. The paragraph and
  duplicate-claim rules are mechanical predicates, not editorial guidance: an
  explanatory section holds exactly one paragraph block (no blank-line-separated
  second block, no bullet list standing in for one), and a *duplicate claim* is
  the same **normalized assertion** — a sentence lowercased, whitespace-collapsed,
  with citations, wikilinks, and trailing punctuation stripped — emitted in two
  explanatory sections of the same page. Both predicates are enforced before
  staging by the prose renderer (4.2) and re-checked across every page by the 7.1
  anatomy lint. Any table hitting its cap
  renders top-N with an explicit `top N of M` truncation label and a gcode
  command pointer for the full listing — bounded rendering, never silent
  truncation (bakeoff C4 applied to tables).
- **Tour audiences:** `new-contributor` and `operator`.
- **Grounding is non-negotiable:** every generated claim carries a resolvable
  `[path:Lstart-Lend]` citation; frontmatter provenance per the pinned gcore
  CodeWiki contract (`crates/gcore/src/codewiki_contract.rs`), with
  `generated_by: gwiki-code` per #19668's marker flip.
- **Sequencing preconditions** (external, enforced in the live task graph, not
  plan-internal dependencies): #19668 moves the engine into `crates/gwiki` first;
  deterministic code facts, bounded graph views, persisted symbol summaries, and
  typed tagged-comment spans come from #17678; AI execution and datastore grants
  are daemon-mediated per #18902. Every one of these is a hard input precondition,
  so #19664 is `blocked_by` both #18902 and #17678 (#19668 arrives transitively —
  it blocks #18902); a producer named here without a graph edge would let this
  plan expand and start FactsBundle-consuming leaves before the producer exists.
  New engine code is authored under `crates/gwiki/src/code_wiki/` as new files, so
  this plan does not depend on the exact post-move layout of the legacy modules.
- **Deterministic renderer input boundary:** the
  `gobby_core::code_facts::FactsBundle` contract delivered by #17678 (schema in
  `crates/gcore/src/code_facts.rs`) is the sole input to deterministic renderers —
  no DB or index access from render code. The bundle carries typed
  tagged-comment spans (`NOTE:`/`WHY:`/`HACK:`), so the insights rationale
  renders without source scanning. The checked-in golden consumer fixture
  is retained as the shared renderer test input. An unsupported `FACTS_VERSION`
  is rejected before any page is written; only the current version is accepted,
  consistent with the pre-0.5 no-compatibility policy.
- **Production vault untouched.** All work validates in isolated temporary vaults.
  Activation happens only through #18779's manual destructive-cutover acceptance
  child. Orchestration (triggers, cron, recovery) belongs to #19665.
- **Task-graph reconciliation:** #18871's seven implementation-direction items are
  absorbed by this plan (normalizer/prose/retrieval-preference → 6.1 and the prose
  contracts above; at-a-glance facts → 3.1; layers and tour metadata → 3.2 and 4.3;
  reference-row quality → 3.3; render versions and deterministic regeneration →
  2.1 and the 7.1 end-to-end acceptance). #18871 is closed as superseded by this
  plan, so the disposition is settled before expansion rather than deferred to it:
  the six sections named above are the sole implementation owners, and no open
  task can start the same normalizer, prose, facts, layers, tours, retrieval, or
  regeneration work in parallel with this plan's leaves.
- **Module-root ownership:** 2.1 creates `code_wiki/mod.rs` and
  `code_wiki/render/mod.rs` declaring only the files 2.1 itself delivers. Every
  later leaf that introduces a new file under `code_wiki/` (or `code_wiki/render/`)
  owns the `mod` declaration and any public re-export for that file in its parent
  module root, plus the `lib.rs` re-export when the symbol is part of the crate's
  public surface. Those roots are shared append-only surfaces and are implicit
  targets of every such leaf, so each expanded leaf compiles independently and no
  leaf declares a module whose file does not yet exist.
- **Monolith ceiling:** every new hand-maintained file stays below 1,000 lines.
- All Rust work is `implementation_domain: backend`; build/reinstall of the `gwiki`
  binary is required before daemon-visible behavior changes.

## Output Design
`kind: framing`

The target `code/**` tree (slugs illustrative):

```
code/
  _index.md                  # overview page: at-a-glance landing (absorbs repo.md)
  insights.md                # insight report
  architecture/
    layers.md                # named layers + architecture map
  modules/<slug>.md          # one per module (directory-faithful), ~30–50 pages
  concepts/<slug>.md         # semantic cross-cutting clusters, ~8–15 pages
  tours/new-contributor.md   # ordered guided tours
  tours/operator.md
  features.md  deprecations.md  changes.md  hotspots.md  ownership.md  infrastructure.md
  _meta/build.json           # generated-state manifest (bounded by config-store ceiling)
  _meta/layers.json          # machine-readable layer membership
```

Page anatomy (module page, the richest type):

1. Frontmatter (new-engine pinned contract): `title`, `type` (carries the
   page-type id — `overview | module | concept | tour | insights` or a
   projection id; no separate `page_type` key), `provenance` (file/ranges),
   `generated_by: gwiki-code`, `trust`, `freshness`, `degraded`,
   `degraded_sources`, plus `render_version`, `template_version`, `layer`,
   `summary` (≤180 chars). The legacy `ai_route`, `ai_fallback`, and
   `ai_generation_status` keys are dropped: daemon-mediated execution is
   required for #19670, so route collapses to a constant, no fallback lane
   exists, and status is derivable from the manifest's `ai_permitted` plus
   `degraded`.
2. `# <Module title>`
3. Collapsible provenance header (bakeoff C2), rendered from frontmatter
   provenance: `<details><summary>Relevant source files (N)</summary>` with
   resolvable links.
4. At-a-glance facts table (deterministic).
5. `## Purpose` — one-paragraph prose slot (grounded, cited).
6. `## Architecture` — bounded mermaid (component map or key flow) + short prose.
7. `## Key symbols` — reference table ≤24 rows: Symbol | Signature | Purpose |
   Source (`[path:Lx-Ly]`); no structural filler purposes.
8. `## Uses / used by` — coupling tables (≤6 rows each) with module wikilinks.
9. `## Files` — deterministic table: Path | Summary | Key symbols.
10. `## Next reading` — 1–3 wikilinks.

Overview (`code/_index.md`): totals table (languages, components, indexed symbols,
tour chapters), layer table with wikilinks, tour entry points, top concepts,
freshness badge, link to insights — one screen; its `## Overview` block stays under
the 2,000-char session-injection budget. Concept pages keep the
Purpose / How it works / Key components (Symbol/Role/Evidence) shape. Tours are
3–6 dependency-ordered steps, each step one paragraph + evidence links. Insights:
god nodes by centrality, surprising cross-layer connections ranked by
unexpectedness, design rationale extracted from `NOTE:`/`WHY:`/`HACK:` comments as
cited items, and 4–5 "questions this wiki answers". Every page type is enumerated
in a page-type manifest with render versions; regeneration is deterministic and
byte-identical for identical inputs.

## P1: Design contract and page-type manifest
`kind: framing`

**Goal**: The output design is pinned as a reviewable contract and a
machine-readable page-type manifest before any renderer work.

### 1.1 Author the wiki output contract [category: docs]
`kind: deliverable`

Targets:
- `docs/contracts/wiki-output.md`
- `docs/contracts/wiki-output/examples/overview.md`
- `docs/contracts/wiki-output/examples/module.md`
- `docs/contracts/wiki-output/examples/concept.md`
- `docs/contracts/wiki-output/examples/tour.md`
- `docs/contracts/wiki-output/examples/layers.md`
- `docs/contracts/wiki-output/examples/insights.md`
- `docs/contracts/wiki-output/examples/hotspots.md`
- `.gobby/plans/wiki-codewiki-restructure.md`

Create `docs/contracts/wiki-output.md` — the canonical output design contract.
Transcribe and complete the design pinned in this plan's `## Output Design` and
`## Constraints` sections:

- Target `code/**` tree and the five generated page types
  (`overview | module | concept | tour | insights`) plus the seven deterministic
  projections; page-count target 50–90 for a repo of Gobby's size.
- Full page anatomy per type (frontmatter keys, collapsible relevant-source-files
  header, at-a-glance tables, prose slots, key-symbol reference table ≤24 rows,
  uses/used-by tables, next-reading links).
- Frontmatter schema: the new engine's leaner pinned contract (successor to the
  legacy gcore CodeWiki contract, same `GOLDEN_PAGE` pinning pattern): `title`,
  `type` (page-type id), `provenance`, `generated_by: gwiki-code`, `trust`,
  `freshness`, `degraded`, `degraded_sources`, `render_version`,
  `template_version`, `layer`, `summary` (≤180 chars). `ai_route`,
  `ai_fallback`, and `ai_generation_status` are dropped (daemon-required
  execution makes them constant or derivable).
- **Normative exemplars**: one complete example page per class under
  `docs/contracts/wiki-output/examples/` (`overview.md`, `module.md`,
  `concept.md`, `tour.md`, `layers.md`, `insights.md`, plus one representative
  deterministic projection `hotspots.md`; the remaining five projections are
  specified by field tables in the contract doc). Exemplars are the
  `GOLDEN_PAGE` pattern (`crates/gcore/src/codewiki_contract.rs`) scaled to the
  page level: structure is normative (field order, section order, table columns,
  caps, citation and truncation-label form); figures and prose are illustrative.
  Renderer tests in P3–P5 structure-diff their output against the matching
  exemplar (heading sequence, frontmatter keys, table columns, cap compliance)
  so templates cannot drift from the contract.
- Citation rules: inline `[path:Lstart-Lend]` that must resolve against the indexed
  tree; wikilink navigation; a lint failure is a generation failure.
- Namespace model: `code/**` generated vs `knowledge/**` curated; unification at
  retrieval/navigation/graph only; same-slug concept aliasing between namespaces.
- Diagram policy: three kinds (architecture map, key flows, class hierarchy),
  bounded top-N fallback with `simplified — top N of M edges` labeling, validated
  mermaid only, no dependency/import diagram.
- Identity, invalidation, and manifest semantics (summarizing the contracts
  implemented in P2): stable page identities, `_meta/build.json` shape, change
  classes `SKIP | COSMETIC | PARTIAL | ARCHITECTURE | FULL`, rename handling,
  orphan cleanup, schema-evolution rules.
- Trust and provenance signals preserved from the existing contract (`trust`,
  `freshness`, `degraded`, `degraded_sources`).

Add a superseded banner to the top of `.gobby/plans/wiki-codewiki-restructure.md`
pointing at this plan and the new contract doc, stating which parts remain valid as
absorbed mechanics (facts foundation, template/queue mechanics, change classifier)
and that the page model authority is now `docs/contracts/wiki-output.md`.

**Acceptance:**

- 1.1.1 - Output contract exists and covers taxonomy, anatomy, frontmatter,
  citations, namespaces, diagrams, identity/invalidation/manifest semantics, and
  trust signals. file: `docs/contracts/wiki-output.md`.
- 1.1.2 - Superseded banner present with pointers to this plan and the contract.
  file: `.gobby/plans/wiki-codewiki-restructure.md`.
- 1.1.3 - Normative exemplars exist for all seven listed page classes with
  contract frontmatter, full section anatomy, and truncation-label examples.
  file: `docs/contracts/wiki-output/examples/module.md`.

### 1.2 Add the page-type manifest template [category: config] (depends: 1.1)
`kind: deliverable`

Targets:
- `src/gobby/install/shared/templates/codewiki/manifest.yaml`
- `src/gobby/install/bundled_content_manifest.json::*` — scope-reason: generated inventory regenerated whole by the manifest generator
- `src/gobby/cli/installers/shared.py::sync_bundled_content_to_db`
- `tests/cli/installers/test_shared.py::*` — scope-reason: consumer of `sync_bundled_content_to_db`; re-anchored when that contract changes
- `tests/e2e/test_stateless_ambient_session.py::*` — scope-reason: consumer of `sync_bundled_content_to_db`; re-anchored when that contract changes
- `src/gobby/sync/integrity.py::*` — scope-reason: registering the protected `templates` content type spans the module constants and `_content_type_for_shared_relative_path`
- `tests/sync/test_integrity.py::*` — scope-reason: clean/modified/untracked template states extend the existing integrity suite

Create the machine-readable page-type manifest bundled as a template. One entry per
page type with: `id` (`overview`, `module`, `concept`, `tour`, `insights`, and the
seven deterministic projections), `template_version`, `render_version`, output path
pattern, required sections, prose-contract limits (paragraph counts, table row
caps, tour step range 3–6, next-reading 1–3), diagram slots and allowed kinds, and
whether AI prose is permitted (deterministic projections: no). The manifest is the
single authority renderers consult; adding a future page type (e.g., glossary) is a
manifest + template addition, not an engine change.

Bundled-content bookkeeping and installation are part of this leaf: regenerate
`bundled_content_manifest.json` with the existing generator (the tree-equality
integrity test fails otherwise), and add a file-install step to
`sync_bundled_content_to_db` (the `gobby install` single import point) copying
the template to `~/.gobby/templates/codewiki/manifest.yaml`. That installed
path is where the gwiki loader (2.3) resolves the manifest from `GOBBY_HOME`;
a missing installed copy fails generation loudly in 2.3.

The new subtree participates in bundled-content integrity: `templates` is
registered as a protected content type (`BUNDLED_SYNC_CONTENT_TYPES`,
`CONTENT_TYPE_DIRS`, `_GIT_PROTECTED_PATHS`), `shared/templates/**` paths
classify to it in `_content_type_for_shared_relative_path`, and the
file-install step honors `skip_types` — a tampered bundled template is blocked
in production mode exactly like tampered skills or rules.

**Acceptance:**

- 1.2.1 - Manifest enumerates all five generated page types and seven
  deterministic projections with versions, section requirements, and prose limits.
  file: `src/gobby/install/shared/templates/codewiki/manifest.yaml`.
- 1.2.2 - `bundled_content_manifest.json` is regenerated and the bundled-content
  integrity test passes; `gobby install` places the template at the `GOBBY_HOME`
  path the 2.3 loader reads. symbol: `sync_bundled_content_to_db`.
- 1.2.3 - A modified `shared/templates/**` file classifies to the protected
  `templates` content type and blocks the file-install step via `skip_types`;
  clean, modified, and untracked states are covered. test: `tests/sync/test_integrity.py`.

## P2: Identity, build manifest, and invalidation model
`kind: framing`

**Goal**: Stable artifact identities, a compact generated-state manifest, and
incremental invalidation semantics — the information-model core of #19664.

### 2.1 Implement stable identities and the build manifest [category: code] (depends: P1)
`kind: deliverable`

Targets:
- `crates/gwiki/src/code_wiki/mod.rs`
- `crates/gwiki/src/code_wiki/render/mod.rs`
- `crates/gwiki/src/code_wiki/identity.rs`
- `crates/gwiki/src/code_wiki/build_manifest.rs`
- `crates/gwiki/src/code_wiki/publication.rs`
- `crates/gwiki/src/lib.rs::*` — scope-reason: register the new `code_wiki` module in the crate root
- `crates/gwiki/tests/code_wiki_manifest.rs`
- `crates/gwiki/tests/code_wiki_publication.rs`

New module `crates/gwiki/src/code_wiki/` (engine namespace for the redesigned
generator; coexists with the #19668-moved legacy code until P5 of the umbrella
retires it). `mod.rs` and `render/mod.rs` declare the module tree and `lib.rs`
registers `code_wiki` at crate visibility, so every later leaf compiles into
gwiki and integration tests can reach the engine.

This leaf delivers the engine's foundation primitives only. The public
composition entrypoint (`generate_code_wiki`, `engine.rs`) is owned by 6.2,
which runs after every P2–P6 producer exists — 2.1 never depends on downstream
leaves to close. Daemon and CLI adapter wiring (triggers, refresh service,
command surface) is explicitly deferred to #19665 (D2); this plan delivers the
library surface only.

`identity.rs`: every page carries an immutable `page_id` minted the first time
the page enters the build manifest and persisted across regenerations via the
prior manifest; `page_id` is independent of slug and path. Module slugs derive
from repository-relative directory paths; concept slugs from cluster names;
fixed slugs for singleton pages. A slug change is a re-slug event: the new
slug/path binds to the existing `page_id` through lineage kept in the build
manifest (prior-slug → `page_id` mapping), so build records, tour metadata, and
graph node references keep their identity. A cold rebuild with no prior manifest
mints fresh ids; that is documented as a full-identity reset.

`build_manifest.rs`: read/write `code/_meta/build.json` — per-page records
`{page_id, page_type, slug, path, render_version, template_version, inputs_hash,
ai_artifact_ids, source_files, generated_at, degraded}` plus top-level
`{schema_version, engine_version, generation_id, layer_names, tours,
rename_lineage, ai_artifacts, graph_projection_digest}`. `layer_names` is the
cached cluster/layer naming state written by 4.1; `tours` is the ordered tour
metadata written by 4.3. `graph_projection_digest` is the typed, optional
cross-run selection value 5.1's late projection check compares against; this
leaf owns its field, its serialization, and the only mutation path — an exported
`set_graph_projection_digest` on the manifest writer that 5.1 calls inside the
same publication that emits the projection, so no other leaf edits
`build_manifest.rs`. Absent means "no committed projection to trust": a cold
rebuild, an unsupported `schema_version`, and an unparseable manifest all leave
it absent, and an absent digest always mismatches, so recovery regenerates the
projection rather than trusting an output it cannot describe.
`ai_artifacts` is the per-artifact AI state map, keyed by a stable `ArtifactId`
with exactly the cardinality of independent regeneration selection — one entry
per page prose slot (`purpose`, `architecture`), per concept prose, per
layer-name set, per tour's steps, and per insight-question set — each entry
carrying `{cache_key, outcome, cause, failure_count, owner}`, where `owner` is
the owning `page_id` or the non-page artifact identity. Page records reference
their artifact ids instead of collapsing them into one page-level key, so every
independently selectable artifact has its own cache identity *and* its own
repair state, and both round-trip; non-page artifacts need no BuildRecord to
persist either. All are part of the typed schema and round-trip. Determinism: `generation_id` is content-derived
(hash over the manifest's input state), and `generated_at` is preserved for
pages whose `inputs_hash` is unchanged, so regenerating identical inputs yields
a byte-identical `build.json`. Replaces the legacy 14.6 MB `_meta/codewiki.json`;
~100 KB is the expected order at the 50–90 page scale, but that figure is an
expectation, not a gate. The only enforced bound is an extreme runaway-growth
ceiling read from the gcore config store
(`wiki.code_wiki.build_manifest_max_bytes`, default 100 MB); exceeding the
configured ceiling fails generation before publication. Schema evolution
(pre-0.5, no compatibility): an existing `code/_meta/build.json` whose
`schema_version` is unsupported, or that fails to parse, is treated as absent —
the documented cold-rebuild path runs (fresh `page_id`s, full regeneration,
orphan cleanup of manifest-untracked pages); no migration or partial read is
attempted. Include orphan detection: any
`code/**` page not present in the manifest (excluding gwiki-catalog-owned
`code/INDEX.md` and `_context.md`) is reported and removed on regeneration.

That ownership test is exported as one predicate, `is_engine_owned_generated_path`,
defined here rather than at either call site: 3.4's catalog reconciliation and
6.1's search filter must both answer the question identically, and neither can own
a symbol the other compiles against — 3.4 would have to depend on a later leaf.
2.1 precedes both, so the predicate lands with the manifest that defines
ownership. It answers exactly one question — whether a vault-relative path is an
engine-owned generated page described by the build manifest. Manifest-tracked
pages and `code/_meta/**` are engine-owned; `code/INDEX.md`, `_context.md`, the
vault-root `_index.md`, `knowledge/**`, and rendered source pages are not.

`publication.rs`: transactional publication for the whole generation. Semantics
are adapted from the legacy staged/journaled `CodewikiPublication` mechanics
(staging area, journaled replacement and pruning, recovery on restart), but the
implementation lives in these new files and never imports the #19668-moved
legacy modules — the plan's constraint that the new engine does not depend on
the post-move layout holds.

Writer serialization comes first: the writer acquires the per-vault exclusive
generation lock **before** journal recovery and before any prior-manifest or
cache read, and holds it through manifest commit and staged catalog completion.
Reading prior state, classifying changes, minting cold-start identities, running
AI passes, staging, swapping in, and committing are therefore one serialized
writer transaction — two concurrent generations can never derive output from the
same stale prior manifest and then race at swap time, with the later run
clobbering the first. (The equivalent alternative, if a future orchestration
layer wants writer concurrency, is revalidating `generation_id` under the lock
with a compare-and-swap and restarting selection on mismatch; this plan takes
the serialized form because generation is a per-vault singleton.)

Protocol: every artifact (pages, the layers JSON, graph output) renders into a
staging area; staged artifacts are validated; page swap-in and orphan pruning
are journaled; the build manifest commits last as the publication point; the
catalog refreshes after commit under the existing codewiki lock (one documented
lock order). A crash at any point either leaves the prior generation fully
intact or completes the new one on recovery; readers never observe
mixed-generation state.

Reader atomicity is part of the protocol, not an aspiration. `publication.rs`
exports the generation lock as a public gwiki primitive so every retrieval
reader can take it in shared mode and observe exactly one generation across
pages, projections, catalog, and manifest. Three policies make that hold in the
crash window. Lock modes are explicit: recovery mutates the published tree, so a
reader that finds a dirty journal (writer died, lock released, swaps incomplete)
while holding the lock in shared mode releases shared ownership, acquires it
exclusively, rechecks the journal — another process may have recovered first —
runs recovery, then downgrades to shared before serving, or fails loudly with the
recovery error; recovery never runs under shared ownership and a half-swapped
tree is never served. Guard ownership is explicit: every lock-protected
operation exists as a public entrypoint that acquires the lock and an internal
form that takes an already-held guard, so the writer — which holds exclusive
ownership through staged catalog completion — invokes the guarded form and no
in-generation call path re-enters the lock against itself. Lock acquisition uses
a bounded timeout with a documented, non-blocking failure mode. 2.1 owns the
primitive, the journal, the guard type, and the recovery policy; adoption by the
concrete reader entrypoints
(`wiki_read`, `wiki_search`) is owned by 6.1 and catalog readers by 3.4, so no
two leaves edit the same reader file.

Search-index visibility boundary: `wiki_search` resolves candidates from an
independently refreshed BM25/semantic store (`crates/gwiki/src/search/`,
`store/`), not from the published tree, so no filesystem lock can make it
atomic with publication. The contract is stated rather than pretended: index
rows are a projection of some committed generation; results are filtered at read
time against the committed manifest for engine-owned generated paths only — the
manifest describes the generated `code/**` set and nothing else, so curated
`knowledge/**`, rendered source pages, and catalog-owned indexes keep their own
lifecycles and are never subject to generation-membership filtering — which
means a generated hit absent from the committed generation is dropped instead of
served as a dangling result; and generated pages published since the last
refresh stay invisible to search until reindex —
bounded staleness, never mixed-generation content. The read-time filter and its
tests are owned by 6.1; triggering the post-publication index refresh is
orchestration and stays deferred to #19665 (D2).

**Acceptance:**

- 2.1.1 - Page identities are stable across two regenerations of an identical
  input tree. test: `crates/gwiki/tests/code_wiki_manifest.rs::identity_stable_across_regeneration`.
- 2.1.2 - `build.json` round-trips the complete typed schema (including
  `layer_names`, `tours`, `rename_lineage`, `graph_projection_digest` in both
  its present and absent forms, and an `ai_artifacts` map holding an
  independent `cache_key` and repair state for each page prose slot and each
  non-page artifact class), stays under the configured build-manifest ceiling,
  and regenerating identical inputs yields byte-identical bytes; a digest
  written through `set_graph_projection_digest` is readable verbatim by the
  next run, and a manifest treated as absent presents no digest.
  test: `crates/gwiki/tests/code_wiki_manifest.rs::build_json_roundtrip_and_byte_identical`.
- 2.1.3 - Orphaned generated pages are detected and pruned; catalog-owned files
  are never touched; and `is_engine_owned_generated_path` classifies every path
  class exactly once — manifest-tracked pages and `code/_meta/**` engine-owned,
  `code/INDEX.md`, `_context.md`, the vault-root `_index.md`, `knowledge/**`, and
  rendered source pages not — so 3.4 and 6.1 share one answer.
  test: `crates/gwiki/tests/code_wiki_manifest.rs::orphan_prune_spares_catalog_files`.
- 2.1.4 - A slug-changing rename preserves `page_id` while updating path,
  inbound wikilinks, tour metadata, and graph references; lineage is recorded.
  test: `crates/gwiki/tests/code_wiki_manifest.rs::rename_preserves_page_id`.
- 2.1.5 - An injected interruption at each publication stage (before staging
  validation, during replacement, during pruning, before manifest commit,
  during catalog refresh) leaves either the old or the new generation fully
  observable, never a mix. test: `crates/gwiki/tests/code_wiki_publication.rs::interrupted_publication_recovers`.
- 2.1.6 - A shared-mode reader at each publication stage observes exactly one
  generation across pages, projections, catalog, and manifest; two readers
  entering the crash window with a dirty journal recover exactly once under
  exclusive ownership and both serve the recovered generation (or fail loudly),
  never a half-swapped tree; a guarded operation invoked from inside the writer's
  transaction never re-acquires the lock; and lock acquisition honors its bounded
  timeout. test: `crates/gwiki/tests/code_wiki_publication.rs::concurrent_reader_single_generation`.
- 2.1.7 - An existing manifest with an unsupported `schema_version` or
  unparseable content triggers the documented full-identity cold-rebuild path;
  no partial read occurs. test: `crates/gwiki/tests/code_wiki_manifest.rs::unsupported_schema_version_resets_identity`.
- 2.1.8 - Two concurrent writers serialize end-to-end on both a cold vault (no
  prior manifest) and a warm one: the second run reads the first run's committed
  manifest rather than the stale prior state, no page id is minted twice, and no
  generation is clobbered. test: `crates/gwiki/tests/code_wiki_publication.rs::concurrent_writers_serialize`.

### 2.2 Implement change classification and incremental invalidation [category: code] (depends: 2.1, 2.4)
`kind: deliverable`

Targets:
- `crates/gwiki/src/code_wiki/invalidation.rs`
- `crates/gwiki/tests/code_wiki_invalidation.rs`

Classification inputs are the complete set of inputs that can change output
bytes: changed indexed files with symbol-level facts (including call/import edge
digests), tagged `NOTE:`/`WHY:`/`HACK:` comment-line digests (insights rationale
input), the knowledge/catalog digest (knowledge concepts and aliases feed graph
nodes and cross-namespace links), and **both** membership fingerprints — the
prior manifest's and the current one computed by 2.4. The selector cannot detect
an `ARCHITECTURE` change without the current fingerprint, so 2.4's deterministic
membership pass is a producer this leaf consumes and the engine (6.2) runs it
before selection; membership is a plain input value, so the classifier stays
vault-free and unit-testable. Classify a change set into
`SKIP | COSMETIC | PARTIAL | ARCHITECTURE | FULL`:

- `SKIP`: no classification input changed.
- `COSMETIC`: only comments/docs changed (symbol hashes stable) — refresh
  citations/line ranges of affected pages without AI regeneration; when tagged
  rationale comments changed, also re-render the insights rationale section.
- `PARTIAL`: symbol changes confined to existing modules — regenerate those module
  pages, plus landing facts and any concept/tour/insights page whose
  `source_files` intersect the change set. The graph projection is decided by the
  late projection check defined below, never at this selection point.
- `ARCHITECTURE`: module membership or layer assignment changed — detected by
  comparing the prior and current membership fingerprints, so a pure membership
  shift with no file edits still classifies — additionally regenerate the
  **architecture set**: `layers.md`, `layers.json`, affected concept pages,
  tours, and the graph projection. That named set is the single definition;
  2.2.1, 2.2.5, and 6.2.3 assert against it verbatim.
- `FULL`: manifest schema, template/render version, or engine version changed.
- Knowledge-only changes (knowledge/catalog digest moved, no indexed files):
  regenerate the graph projection, insights, and catalog cross-namespace
  aliasing; code pages are untouched.

The graph projection cannot be selected at this point, and the model says so
rather than pretending otherwise. Its inputs include values that do not exist
until upstream rendering, naming, layer assignment, and the AI passes finish —
final node summaries, key-symbol membership, layer assignment, and
cross-namespace alias pairs — so a selector running before P3–P5 has nothing to
hash. Selection is therefore split by responsibility: this leaf defines the
**graph-projection input digest** and the comparison rule, and 5.1 performs the
check at its own point in the composition order (6.2). The digest covers every
field 5.1 serializes — node identity, kind, slug, layer assignment, node summary,
key-symbol membership, cross-namespace alias pairs, and the edge set with its
evidence spans — and its prior value is persisted in the build manifest. Once
every upstream producer has materialized its final values, 5.1 builds the
complete projection input without writing, digests it, compares against the
persisted prior, and writes `outputs/graph.json` plus the new digest only on a
mismatch. A change leaving call/import edges identical while moving a symbol
summary, a key-symbol set, a layer assignment, or an alias pair therefore still
regenerates the projection, and an unchanged projection is never rewritten.

Every change class reaches that same late check instead of predicting its
outcome. Naming the graph projection in the `ARCHITECTURE` set below states which
class marks it stale, not that a different code path regenerates it: a membership
change necessarily moves layer assignment, which is a digest field, so an
`ARCHITECTURE` run always mismatches and always regenerates. Knowledge-only runs
reach it through changed alias pairs.

The complete artifact-by-input dependency matrix (which inputs invalidate each
page class, `layers.json`, the graph projection, and insights) is documented in
`invalidation.rs` and every nonempty cell is covered by a test.

Degraded-output repair: every AI artifact (page prose slots, concept prose,
tour steps, cluster/layer names, insight questions) owns an `ArtifactId`-keyed
entry in the manifest's `ai_artifacts` state (2.1) carrying its own cache key
and a typed outcome: verified success, or degraded with a cause
(`unavailable`, `timeout`, `malformed`, `failed_verification`) and a
failure count. Records with a degraded outcome are unioned into the
regeneration set whenever the capabilities input reports AI available,
independent of input changes — a transient failure never becomes permanent
prose, and that holds for non-page artifacts (layer names, tour steps, insight
questions) exactly as for pages. Publishable degradation (a deterministic
fallback exists: provisional layer names, an omitted prose slot) is
distinguished from fatal error (no coherent output is possible); the outcome
is retained if regeneration fails again and cleared only after verified output.

Rename handling: a moved file re-slugs its module page; the old page is pruned via
manifest diff and inbound wikilinks are rewritten. Invalidation decisions are pure
functions of (manifest, change set, membership fingerprints, capabilities) and
unit-testable without a vault. `GenerationCapabilities` is not an availability
flag: it is an immutable **resolved execution snapshot** captured once before
selection, carrying AI availability, the resolved daemon route/profile, the
provider/model or daemon-lane version, the prompt and response-schema digests,
and the executable transport handle. Every cache key derives from that snapshot
and every AI pass executes through it, so a configuration or daemon-lane change
landing mid-run cannot make selection key on one lane while execution and
persistence happen on another. Identical (manifest, change set, membership)
inputs select differently only when the snapshot differs, and that difference is
an explicit function argument, never ambient state.

AI artifact cache identity: each AI artifact class declares its own semantic
inputs, and its cache key is built from exactly those. Module/concept prose
keys on `(page_id, symbol-level fact hashes of its source set, validated
manifest-entry digest, prompt/schema version, render/template version,
model-lane version)` — comment-only edits leave symbol hashes stable, so
`COSMETIC` changes re-anchor citation line ranges with zero AI regeneration,
exactly as the class definition above requires. Insights rationale keys on the
tagged `NOTE:`/`WHY:`/`HACK:` span digests; tour steps on the tour's member
page set and their fact hashes; cluster/layer names on the 2.4 membership
fingerprint plus prompt/schema and model-lane versions — the exact and only
naming invalidators (nothing else renames a stable membership). Citation
positions are never cache-key inputs: re-anchoring is a deterministic
post-pass. The validated manifest-entry digest rides in both `inputs_hash` and
every cache key, so behavior-bearing manifest edits (caps, required sections)
invalidate even when template/render versions are unchanged. Every "model-lane
version" above is read from the resolved execution snapshot, never from live
configuration. Keys persist per `ArtifactId` in the manifest's `ai_artifacts`
map (2.1) — one key per independently selectable artifact, page and non-page
alike; cached output is reused only on an exact key match.
Warm-cache stability in 7.1 and the `COSMETIC`/`PARTIAL` behaviors above are
defined in terms of these keys.

**Acceptance:**

- 2.2.1 - Each change class maps to the exact regeneration set defined above.
  test: `crates/gwiki/tests/code_wiki_invalidation.rs::change_classes_select_expected_pages`.
- 2.2.2 - A file rename produces a re-slugged page, pruned old page, and rewritten
  inbound wikilinks. test: `crates/gwiki/tests/code_wiki_invalidation.rs::rename_reslug_rewrites_links`.
- 2.2.3 - Identical inputs hit the AI cache; a comment-only edit re-anchors
  citations with zero AI regeneration; a change to symbol facts, prompt/schema
  version, template/render version, model lane, or a behavior-bearing
  manifest field each invalidates exactly the artifact classes keyed on it.
  test: `crates/gwiki/tests/code_wiki_invalidation.rs::ai_cache_key_hits_and_invalidations`.
- 2.2.4 - A degraded artifact — page prose and each non-page artifact (layer
  names, tour steps, insight questions) — regenerates on the next AI-available
  run with unchanged inputs; the outcome survives a second failure and clears
  only after verified output; the unavailable→available capability transition
  is covered. test: `crates/gwiki/tests/code_wiki_invalidation.rs::degraded_artifacts_reselected_until_repaired`.
- 2.2.5 - The artifact-by-input matrix is enforced: the late projection check
  regenerates the graph on a PARTIAL edge change, a node-field change with
  identical call/import edges (symbol summary, key-symbol membership, layer
  assignment, alias pair) moves the graph-projection input digest and regenerates
  it too, an input whose digest is unchanged leaves `outputs/graph.json`
  unwritten, COSMETIC tagged-comment changes re-render insights rationale, knowledge-only
  changes regenerate graph, insights, and aliasing, and a membership-fingerprint
  change with no file edits classifies ARCHITECTURE and selects exactly the
  architecture set named in 2.2.
  test: `crates/gwiki/tests/code_wiki_invalidation.rs::input_matrix_selects_all_stale_artifacts`.
- 2.2.6 - Selection and execution share one resolved execution snapshot: a
  route, profile, provider/model, daemon-lane, or prompt/schema mutation applied
  after capture changes neither the keys selection used nor the lane execution
  and persistence run on, and each of those fields independently invalidates
  when it differs at capture time.
  test: `crates/gwiki/tests/code_wiki_invalidation.rs::capability_snapshot_is_immutable_and_keyed`.

### 2.3 Load and validate the page-type manifest [category: code] (depends: 1.2)
`kind: deliverable`

Targets:
- `crates/gwiki/src/code_wiki/page_manifest.rs`
- `crates/gwiki/src/code_wiki/render/frontmatter.rs`
- `crates/gwiki/tests/code_wiki_page_manifest.rs`

Typed loader for the page-type manifest (1.2), run before renderer dispatch. The
loader resolves the installed manifest from `GOBBY_HOME`
(`~/.gobby/templates/codewiki/manifest.yaml`, installed by 1.2); a missing or
malformed manifest must fail generation before any write, never drift into
runtime behavior. The manifest is the single renderer authority. Validation:
unique page-type ids; output path patterns are safe and vault-relative (no
absolute paths, no `..`); required-section ids present; numeric caps within
contract ranges (table rows, tour steps 3–6, next-reading 1–3); diagram slots
reference only the three permitted kinds; `ai_permitted: false` enforced for all
seven deterministic projections. Renderers receive the validated typed manifest —
none reads the YAML directly.

`render/frontmatter.rs`: the shared frontmatter builder every renderer uses.
Renderers construct a validated `FrontmatterInput` — title, summary, provenance
file/line spans, trust, freshness, `degraded`/`degraded_sources`, and layer —
which the builder combines with the page's typed manifest entry and
build-record metadata (`render_version`, `template_version`, `generated_by`)
to emit the pinned contract keys (including `summary` presence rules). A
missing required input fails generation before staging; partial frontmatter is
never emitted. The all-pages frontmatter invariant has one owner instead of
per-renderer reimplementations.

**Acceptance:**

- 2.3.1 - Loader accepts the bundled template manifest and exposes typed entries
  renderers consume. test: `crates/gwiki/tests/code_wiki_page_manifest.rs::bundled_manifest_valid`.
- 2.3.2 - Each invalid-manifest class (duplicate id, unsafe path pattern,
  out-of-range cap, forbidden diagram kind, AI-enabled deterministic projection)
  fails generation before any page write; a missing installed manifest fails the
  same way. test: `crates/gwiki/tests/code_wiki_page_manifest.rs::invalid_manifests_fail_before_write`.
- 2.3.3 - Every page type's frontmatter is produced through the shared builder
  from a complete `FrontmatterInput` and carries the pinned contract keys; a
  missing required input fails before staging, covered per page type including
  degraded output. test: `crates/gwiki/tests/code_wiki_page_manifest.rs::frontmatter_built_per_page_type`.

### 2.4 Implement deterministic module clustering membership [category: code] (depends: 2.1)
`kind: deliverable`

Targets:
- `crates/gwiki/src/code_wiki/clustering.rs`
- `crates/gwiki/tests/code_wiki_clustering.rs`

Deterministic membership detection, split out of the naming pass so P3
renderers have their producer before P4 runs: cluster the module set over
gcode's bounded call/import/coupling views (from the FactsBundle) into
cross-cutting semantic groups (bakeoff C5/C9). Cluster count target 8–15 for a
Gobby-size repo. Membership is a pure function of the input graph — no AI, no
DB. Output feeds layer membership (3.2), the naming pass (4.1), concept pages
(4.2), and tours (4.3).

**Acceptance:**

- 2.4.1 - Clustering is deterministic for identical graphs and produces the
  target cluster-count range on fixture graphs. test: `crates/gwiki/tests/code_wiki_clustering.rs::clustering_deterministic_and_bounded`.

### 2.5 Define the external-source information model [category: code] (depends: 2.1)
`kind: deliverable`

Targets:
- `docs/contracts/wiki-sources.md`
- `crates/gwiki/src/sources/mod.rs::*` — scope-reason: the 26-line module root declares and re-exports the segment and citation surface this leaf adds
- `crates/gwiki/src/sources/types.rs::*` — scope-reason: segment identity and tombstone state extend the existing `SourceKind`/`SourceDraft`/`SourceRecord` type set across the file
- `crates/gwiki/src/sources/manifest.rs::*` — scope-reason: segment persistence and tombstoning span `SourceManifest::read`, the `register*` constructors, and `SourceManifest::remove`
- `crates/gwiki/src/sources/render.rs::*` — scope-reason: rendered source pages gain stable segment anchors so segment citations resolve on the existing read surface
- `crates/gwiki/src/citations.rs::*` — scope-reason: the unified citation shape spans `render_citations`, `source_records_for_paths`, and every `render_source_citation*` helper
- `crates/gwiki/src/commands/sources.rs::*` — scope-reason: tombstone removal spans `execute_remove`, `rollback_removed_source_state`, `rollback_removed_source`, and this file's manifest test fixtures
- `crates/gwiki/src/ingest/session_archive.rs::*` — scope-reason: latest-wins replacement spans `remove_session_page` and `supersede_session_page`, which must skip tombstones
- `crates/gwiki/src/commands/citation_quality.rs::*` — scope-reason: `SourceRecord` literal migrated to the canonical constructor
- `crates/gwiki/src/commands/compile.rs::*` — scope-reason: `SourceRecord` literal migrated to the canonical constructor
- `crates/gwiki/src/commands/refresh/selection.rs::*` — scope-reason: `SourceRecord` literal migrated to the canonical constructor
- `crates/gwiki/src/commands/refresh/tests.rs::*` — scope-reason: five `SourceRecord` seed helpers migrated to the canonical constructor
- `crates/gwiki/src/compile/select.rs::*` — scope-reason: `SourceRecord` literal migrated to the canonical constructor
- `crates/gwiki/src/health/tests.rs::*` — scope-reason: `SourceRecord` literal migrated to the canonical constructor
- `crates/gwiki/src/ingest/mod.rs::*` — scope-reason: `SourceRecord` literal migrated to the canonical constructor
- `crates/gwiki/src/paths.rs::*` — scope-reason: `SourceRecord` literal migrated to the canonical constructor
- `crates/gwiki/src/recap.rs::*` — scope-reason: two `SourceRecord` literals migrated to the canonical constructor
- `crates/gwiki/src/transcribe.rs::*` — scope-reason: `SourceRecord` literal migrated to the canonical constructor
- `crates/gwiki/src/upkeep/tests.rs::*` — scope-reason: `SourceRecord` literal migrated to the canonical constructor
- `crates/gwiki/src/video/tests.rs::*` — scope-reason: `SourceRecord` literal migrated to the canonical constructor
- `crates/gwiki/src/vision.rs::*` — scope-reason: `SourceRecord` literal migrated to the canonical constructor
- `crates/gwiki/tests/cli_sources.rs::*` — scope-reason: `SourceRecord` literal migrated to the canonical constructor
- `crates/gwiki/tests/wiki_sources.rs`

Epic #19664 owns stable identities for external sources and extracted
segments, unified repository/external attribution, retrieval addressability,
and deletion semantics; #19671 explicitly leaves that model here and keeps
operational ingestion (D1). This leaf delivers it by **extending the existing
`crates/gwiki/src/sources/` module**, never by introducing a parallel model:
`sources/types.rs` already defines `SourceKind`, `SourceDraft`, and
`SourceRecord`, `sources/manifest.rs` persists them in the vault's source
manifest, and `citations.rs` already renders source citations. (A second
`sources.rs` file cannot exist beside `sources/mod.rs`; the model belongs in the
registered module tree.)

The model gains what the epic requires and today's code lacks. Identity:
`SourceRecord` carries a content-derived, stable `source_id` (distinct from its
manifest position), and a new `SourceSegment` carries a `segment_id`, its parent
`source_id`, a span/locator within the source, and a content hash — so an
extracted segment is addressable independently of its parent's re-ingestion.
Attribution: one `Citation` shape covers repository spans
(`[path:Lstart-Lend]`) and segment references, and `citations.rs` renders both
through it, so a generated artifact attributes either source kind identically.
Addressability: `sources/render.rs` emits a stable per-segment anchor on the
rendered source page, making a segment citation resolvable through the existing
`wiki_read` surface without a second retrieval path. Deletion: `SourceManifest::remove`
today drops the entry outright, which dangles every citation into it — it
becomes a tombstone that retains `source_id`, segment ids, and removal cause,
and this leaf delivers the pure derivation from a tombstone to the set of
dependent artifacts and their `degraded_sources` attribution. No migration path
exists: pre-0.5, no segment state has shipped, and existing manifest entries
gain their `source_id` on first read.

Removal has two live callers and both migrate here, because a tombstone changes
what they observe: `commands/sources.rs::execute_remove` deletes the raw source
and asset files and rolls back the manifest when indexing fails, and
`ingest/session_archive.rs::remove_session_page` performs latest-wins
replacement of a session's page through `SourceManifest::remove_with_cleanup`.
A tombstone keeps exactly enough on the retrieval surface for a citation to land
somewhere real. Raw sources and assets are deleted outright. The rendered source
page is replaced by a content-free tombstone document at the same stable path,
carrying only the source and segment identities, the per-segment anchors, and the
removal cause — so a retained segment citation resolves through the existing
`wiki_read` path to a tombstone instead of a not-found, without adding a second
retrieval path or a read-path special case. Tombstone documents are excluded from
listing, `wiki_search`, compile selection, reindexing, and the latest-wins
presence check, so nothing surfaces them as live content and replacement never
accumulates duplicate live entries.

Both live removal callers change, because retaining the id changes what they
observe. `rollback_removed_source` today refuses to restore whenever the manifest
already contains the record's id; with a tombstone that condition is now always
true, so index-failure rollback would fail permanently. It becomes a
tombstone-to-live transition: a tombstoned entry with the same id is the expected
precondition and is promoted back to live, while a *live* duplicate remains the
error it always was. `supersede_session_page` matches every manifest entry by
kind and canonical location; it gains a tombstone exclusion so latest-wins
replacement never re-removes an already-tombstoned page. `SourceManifest`'s
remove and supersede helpers carry the same exclusion.

Identity fields are populated through one canonical `SourceRecord` constructor.
Adding identity fields to the struct breaks every existing literal, so the
migration is crate-wide by necessity and every literal site is an exact target
above — production (`sources/manifest.rs`, `paths.rs`, `recap.rs`,
`transcribe.rs`, `vision.rs`, `ingest/mod.rs`, `citations.rs`) and test fixture
(`compile/select.rs`, `commands/refresh/`, `commands/compile.rs`,
`commands/citation_quality.rs`, `health/`, `upkeep/`, `video/`,
`tests/cli_sources.rs`) alike. The leaf compiles the crate as part of its own
acceptance rather than leaving literals stranded.

Boundary: extraction execution, retries, progress, daemon/UI ingestion, and
surfacing segments as independent `wiki_search` hits stay with #19671 (D1) —
that epic creates segments in the first place, and this leaf hands it the model,
the tombstone contract, and the degradation derivation to wire into.

**Acceptance:**

- 2.5.1 - The contract doc defines source/segment identity, unified
  attribution, retrieval addressability, and deletion semantics; the extended
  types round-trip through the existing source manifest, and an entry written
  before this leaf reads back with a stable `source_id`.
  test: `crates/gwiki/tests/wiki_sources.rs::source_model_roundtrip`.
- 2.5.2 - Removing a source through the existing manifest path writes a
  tombstone instead of dropping the entry, and the derivation returns exactly
  the dependent artifacts plus their `degraded_sources` attribution; no citation
  into a removed source dangles. Both live callers keep their contracts: the CLI
  removal path still deletes raw sources and assets, and its index-failure
  rollback promotes the tombstoned entry back to live rather than failing on the
  retained id, while a live duplicate id still errors; repeated latest-wins
  session replacement leaves exactly one live entry, skips already-tombstoned
  pages instead of re-removing them, and keeps tombstones out of listing,
  selection, and reindexing. The crate compiles with every `SourceRecord` literal
  migrated to the canonical constructor.
  test: `crates/gwiki/tests/wiki_sources.rs::deletion_tombstones_and_degrades`.
- 2.5.3 - A segment citation rendered through the unified `Citation` shape
  resolves to its anchor on the rendered source page via the existing read
  surface, and a citation into a tombstoned segment resolves through that same
  `wiki_read` path to the content-free tombstone document at the stable rendered
  path — carrying source and segment identities, anchors, and removal cause —
  rather than a missing target, after both CLI removal and latest-wins
  replacement; the tombstone document stays absent from listing and search.
  test: `crates/gwiki/tests/wiki_sources.rs::segment_citations_resolve`.

## P3: Orientation spine (deterministic renderers)
`kind: framing`

**Goal**: The deterministic, AI-free skeleton every other layer hangs off: landing
page, named layers, module scaffolds, and catalog coexistence.

### 3.1 Render the at-a-glance landing page [category: code] (depends: P2)
`kind: deliverable`

Targets:
- `crates/gwiki/src/code_wiki/render/landing.rs`
- `crates/gwiki/tests/code_wiki_landing.rs`

Render `code/_index.md` (page type `overview`; absorbs legacy `repo.md` and
`_onboarding.md` roles): totals table (languages, module count, concept count,
indexed symbol count, tour chapter count — all from FactsBundle facts and build
manifest records, no AI), layer table with wikilinks into
`architecture/layers.md` and module pages, tour entry points, top concepts,
freshness badge from index state, link to `insights.md`. Deterministic and
byte-identical for identical inputs. Concept, tour, and insights references
come from build-manifest records, so in the full pipeline the engine renders
the landing page last (6.2 composition order); sections whose producers have
not registered records render as omitted sections, never placeholder prose.
The `## Overview` block stays ≤2,000 characters as a page-quality cap; session
injection reads the catalog-owned vault-root `_index.md`, and that proof lives
in 6.1.

**Acceptance:**

- 3.1.1 - Landing renders all fact tables from supplied facts and manifest
  records and is byte-identical across two runs. test: `crates/gwiki/tests/code_wiki_landing.rs::landing_deterministic`.
- 3.1.2 - `## Overview` block length ≤2,000 chars; absent page classes render
  as omitted sections. test: `crates/gwiki/tests/code_wiki_landing.rs::overview_block_fits_injection_budget`.

### 3.2 Render named layers and the architecture map [category: code] (depends: 2.4, 5.3)
`kind: deliverable`

Targets:
- `crates/gwiki/src/code_wiki/render/layers.rs`
- `crates/gwiki/tests/code_wiki_layers.rs`

Render `code/architecture/layers.md` + `code/_meta/layers.json`. Layer membership
is deterministic (from 2.4 clustering); layer *names* come from the naming pass
in 4.1 and are cached in the build manifest's `layer_names` state and
`layers.json`. Cold start (no cached names): render deterministic slug-derived
provisional names with `named: false` in `layers.json`; the 4.1 naming pass
rewrites them. Warm runs reuse cached names on an exact match of 4.1's complete
naming cache key (membership fingerprint plus prompt/schema and model-lane
versions), never on membership alone. Page contains: layer table (Layer | Modules | Purpose), a
validated mermaid architecture map of layers and their coupling edges (bounded
via the 5.3 primitive), and per-layer module lists with wikilinks.
`layers.json` carries `{layer, modules[], summary, named}` for UI consumption
(#18869).

**Acceptance:**

- 3.2.1 - `layers.md` and `layers.json` agree on membership and render
  deterministically: provisional names on a cold run, cached names on a warm
  run with a stable membership fingerprint. test: `crates/gwiki/tests/code_wiki_layers.rs::layers_page_matches_json`.
- 3.2.2 - Architecture map is valid mermaid and carries truncation labeling when
  bounded. test: `crates/gwiki/tests/code_wiki_layers.rs::architecture_map_valid_and_labeled`.

### 3.3 Render module page scaffolds [category: code] (depends: P2, 5.3)
`kind: deliverable`

Targets:
- `crates/gwiki/src/code_wiki/render/module_scaffold.rs`
- `crates/gwiki/tests/code_wiki_module_scaffold.rs`

Render `code/modules/<slug>.md` deterministic scaffolds: frontmatter (contract keys
with `type: module`, `layer`, `summary` placeholder), collapsible
relevant-source-files header from provenance, at-a-glance facts table, key-symbol
reference table (≤24 rows: Symbol | Signature | Purpose | Source with resolvable
`[path:Lx-Ly]`; purposes come from persisted symbol summaries — never structural
filler like `Indexed <kind> <symbol> in <file>`), uses/used-by coupling tables
(≤6 rows each, module wikilinks, from gcode bounded coupling views), files table
(Path | Summary | Key symbols, per-file one-liners from the hub-DB summary store;
≤12 rows, top-N by symbol count with a truncation label and gcode pointer),
and HTML-comment prose slots (`<!-- prose:purpose -->`, `<!-- prose:architecture -->`)
for P4 to fill. The diagram is assembled by the scaffold itself: module
component-map/key-flow diagrams derive deterministically from coupling facts
through the 5.3 primitive, so every module page ships with a valid diagram or
a labeled truncation — no page ever publishes an empty diagram slot. Scaffold
rendering is AI-free.

**Acceptance:**

- 3.3.1 - Scaffold emits every section of the module anatomy with correct caps
  and resolvable citations, structure-matching
  `docs/contracts/wiki-output/examples/module.md`. test: `crates/gwiki/tests/code_wiki_module_scaffold.rs::scaffold_sections_and_caps`.
- 3.3.2 - Symbol rows never contain structural-filler purposes; missing summaries
  render as omitted rows, not filler. test: `crates/gwiki/tests/code_wiki_module_scaffold.rs::no_structural_filler_rows`.
- 3.3.3 - Across a fixture corpus, every module page carries a validating
  mermaid diagram or a labeled truncation; none carries an empty diagram slot.
  test: `crates/gwiki/tests/code_wiki_module_scaffold.rs::every_module_page_has_diagram_or_label`.

### 3.4 Reconcile the gwiki catalog with the new code namespace [category: code] (depends: 3.1)
`kind: deliverable`

Targets:
- `crates/gwiki/src/catalog.rs::*` — scope-reason: 1,316-line monolith decomposed into the `catalog/` module tree below; the file is dissolved by this leaf
- `crates/gwiki/src/catalog/mod.rs`
- `crates/gwiki/src/catalog/regenerate.rs`
- `crates/gwiki/src/catalog/wiki_index.rs`
- `crates/gwiki/src/catalog/overview.rs`
- `crates/gwiki/src/vault.rs::*` — scope-reason: the edit is scoped to the AI_README_TEMPLATE constant, which is not an indexed symbol

`catalog.rs` is 1,316 lines — over the 1,000-line production ceiling — and this
leaf is the first to edit it, so it owns the mandatory same-task decomposition:
dissolve `catalog.rs` into the `catalog/` module tree (`mod.rs` public surface,
`regenerate.rs`, `wiki_index.rs`, `overview.rs`), every resulting file below
1,000 lines, behavior-preserving.

The gwiki catalog owns `code/INDEX.md`, `_context.md` files, and the vault root
`_index.md`; the new engine owns `code/_index.md`. Update the catalog so the two
never fight: `regenerate` treats `code/_index.md`, `code/_meta/**`, and
manifest-tracked pages as engine-owned (listed, never rewritten), deciding
ownership through 2.1's shared `is_engine_owned_generated_path` predicate rather
than a second local rule, so the catalog and 6.1's search filter can never
disagree about which paths the engine owns; `render_wiki_index`
groups the code section by the new taxonomy (Landing / Layers / Modules / Concepts /
Tours / Insights / Projections) instead of the legacy Handbook/files split;
`render_overview` sources its code totals from `code/_meta/build.json` when present
and adds cross-namespace concept aliasing — when `code/concepts/<slug>` and
`knowledge/concepts/<slug>` share a slug, each page's catalog entry links the other
as a see-also. Byte-identical rerun behavior is preserved.

Catalog reads join the publication contract here: `regenerate` and the catalog
readers take 2.1's generation lock in shared mode (the catalog's own
`_gwiki/index.lock` keeps its documented order relative to it), so a catalog
read concurrent with a publication observes one generation rather than a
half-swapped code tree. `regenerate` splits into a public entrypoint that
acquires the lock and an internal form taking 2.1's already-held guard, because
publication completes the staged catalog while holding exclusive ownership —
the writer calls the guarded form and never re-enters the lock. 2.1 owns the
primitive; this leaf is the only editor of the catalog files, so adoption lands
here.

Catalog page writes replace files atomically (`sources::atomic::write_atomic`,
already in the crate) instead of the current truncate-then-write `fs::write`.
The vault-root `_index.md` is read outside the generation lock by the Python
session-start injector (6.1), so atomic replacement is what makes that read
observe one complete index rather than a truncated file mid-regeneration.

Installed navigation moves with the taxonomy: `AI_README_TEMPLATE` in `vault.rs`
is rewritten to describe the Landing / Layers / Modules / Concepts / Tours /
Insights / Projections entry points (the handbook/file-pages guidance it
currently ships is retired), so new and restored vaults orient agents to pages
that exist. Existing user-edited guide files are preserved — the template is
written only where the file is absent, matching current vault-init behavior.

**Acceptance:**

- 3.4.1 - Catalog regeneration never rewrites engine-owned pages and remains
  byte-identical on rerun. test: `crates/gwiki/src/catalog/regenerate.rs::regenerate_is_byte_identical_on_rerun`.
- 3.4.2 - Root index groups code entries by the new taxonomy and cross-links
  same-slug concepts across namespaces. symbol: `render_wiki_index`.
- 3.4.3 - Every file touched by this leaf is below 1,000 lines; `catalog.rs`
  no longer exists as a monolith. file: `crates/gwiki/src/catalog/mod.rs`.
- 3.4.4 - A fresh vault init writes the new-taxonomy agent guide; an existing
  user-edited guide is left untouched. symbol: `AI_README_TEMPLATE`.
- 3.4.5 - A catalog read concurrent with each publication stage takes the shared
  generation lock and observes one generation; regeneration invoked from inside
  a publication runs through the guarded form without re-acquiring the lock; the
  documented lock order with `_gwiki/index.lock` is exercised without deadlock;
  and a reader of the vault-root `_index.md` concurrent with regeneration always
  reads one complete index, never a truncated one.
  test: `crates/gwiki/src/catalog/regenerate.rs::catalog_reads_are_generation_consistent`.

### 3.5 Render the six deterministic projection pages [category: code] (depends: P2, 3.4)
`kind: deliverable`

Targets:
- `crates/gwiki/src/code_wiki/render/projections.rs`
- `crates/gwiki/tests/code_wiki_projections.rs`

Render the six remaining deterministic projections (`layers` is 3.2):
`code/features.md`, `code/deprecations.md`, `code/changes.md`, `code/hotspots.md`,
`code/ownership.md`, `code/infrastructure.md`. Pure FactsBundle → Markdown, AI-free
(manifest `ai_permitted: false` per 2.3), rendered from the shared golden fixture
in tests with no index or DB access. Per page: facts source pinned to the
corresponding `FactsBundle` section; bounded anatomy (dense tables under the
generic ≤6-row cap unless the page-type manifest grants a larger cap, with
explicit `top N of M` truncation labels); every row cites `[path:Lstart-Lend]`;
each page registers a build-manifest record (2.1) and appears in the catalog's
Projections group (3.4). Byte-identical across reruns of identical inputs.

**Acceptance:**

- 3.5.1 - All six projection pages render from the shared golden FactsBundle
  fixture with no index access, and rerun byte-identical.
  test: `crates/gwiki/tests/code_wiki_projections.rs::projections_deterministic_from_fixture`.
- 3.5.2 - Truncation is labeled, rows are cited, and every projection registers a
  manifest record and catalog entry.
  test: `crates/gwiki/tests/code_wiki_projections.rs::projections_bounded_cited_registered`.
- 3.5.3 - The projection renderer rejects an unsupported `FACTS_VERSION` as
  local defense; the global before-any-side-effect gate is owned by 6.2.
  test: `crates/gwiki/tests/code_wiki_projections.rs::rejects_unsupported_facts_version`.

## P4: Narrative layer (grounded AI)
`kind: framing`

**Goal**: Semantic concept-modules, grounded narrative prose, and guided tours —
the human-readable half of the hybrid.

### 4.1 Implement semantic cluster naming [category: code] (depends: 2.4, P3)
`kind: deliverable`

Targets:
- `crates/gwiki/src/code_wiki/naming.rs`
- `crates/gwiki/tests/code_wiki_naming.rs`

The LLM naming pass over 2.4's deterministic membership (bakeoff C5/C9 —
confirmed across two competitors): daemon-routed, labels each cluster from
member purposes (e.g., `monitoring-and-detection`, not `src/agents/tmux`).
Names cache in the build manifest's `layer_names` state and `layers.json` so
regeneration without AI reuses them. Reuse is keyed on the complete naming
`ArtifactId` cache key 2.2 defines — the 2.4 membership fingerprint plus the
prompt/schema and model-lane versions read from the resolved execution snapshot —
and names regenerate on any exact-match miss of that key, so a prompt, schema,
route-model, or daemon-lane change renames a stable membership. No threshold or
similarity test sits between the key and the decision. Membership change stays the
ARCHITECTURE classifier in 2.2; it is one component of the naming key rather than
the whole of it. Naming rewrites the 3.2
provisional cold-start names. Output feeds concept pages (4.2), layer names
(3.2), and tours (4.3).

**Acceptance:**

- 4.1.1 - Cached names survive regeneration when membership is stable and the
  full naming key matches, and the naming pass replaces provisional cold-start
  names; with membership held stable, a prompt/schema version change and a
  model-lane change each independently miss the key and regenerate names.
  test: `crates/gwiki/tests/code_wiki_naming.rs::names_cached_across_runs`.

### 4.2 Generate grounded narrative prose and concept pages [category: code] (depends: 4.1)
`kind: deliverable`

Targets:
- `crates/gwiki/src/code_wiki/render/narrative.rs`
- `crates/gwiki/tests/code_wiki_narrative.rs`

Fill module-page prose slots (`purpose`, `architecture`) and render
`code/concepts/<slug>.md` pages (Purpose / How it works / Key components with
Symbol/Role/Evidence rows). Reuse the existing grounding pipeline (grounded
prompts, verification, summary caching) — improve it, never build a second
generator. Enforce prose contracts at render time: one paragraph per explanatory
section, no claim repeated across sections of the same page, every claim cited
`[path:Lx-Ly]`, citations verified against the index before write; a failed
verification degrades the section with `degraded_sources`, never emits ungrounded
prose. Concept-page frontmatter (including `summary`, ≤180 chars, word-boundary
cap) is produced through the shared 2.3 builder; the all-pages summary
invariant is owned by the 2.3 builder plus the 7.1 anatomy lint.

**Acceptance:**

- 4.2.1 - Prose slots and concept pages render with verified citations; failed
  verification produces degraded sections, not ungrounded text. test: `crates/gwiki/tests/code_wiki_narrative.rs::ungrounded_prose_degrades`.
- 4.2.2 - Every page this leaf produces carries a contract-compliant `summary`
  built through the shared builder. test: `crates/gwiki/tests/code_wiki_narrative.rs::summaries_within_contract`.
- 4.2.3 - The prose contract's mechanical predicates are enforced before
  staging: a section rendering a second paragraph block and a normalized
  assertion repeated across two explanatory sections of the same page are each
  rejected (degraded, never published), and single-paragraph pages with distinct
  claims pass unchanged.
  test: `crates/gwiki/tests/code_wiki_narrative.rs::prose_shape_and_duplicate_claims_rejected`.

### 4.3 Generate dependency-ordered guided tours [category: code] (depends: 4.1, 4.2)
`kind: deliverable`

Targets:
- `crates/gwiki/src/code_wiki/render/tours.rs`
- `crates/gwiki/tests/code_wiki_tours.rs`

Render `code/tours/new-contributor.md` and `code/tours/operator.md`: 3–6
dependency-ordered steps each (order derived from the module dependency structure —
foundations before consumers), one grounded paragraph per step with wikilinks into
module/concept pages and citations. Tour metadata (ordered step list with page ids)
is emitted into the build manifest for UI consumption (#18869 ordered tours).

**Acceptance:**

- 4.3.1 - Both audience tours render 3–6 dependency-ordered steps with resolvable
  links. test: `crates/gwiki/tests/code_wiki_tours.rs::tours_ordered_and_bounded`.
- 4.3.2 - Tour metadata appears in the build manifest with stable page ids. file: `crates/gwiki/src/code_wiki/render/tours.rs`.

## P5: Graph projection and insight artifacts
`kind: framing`

**Goal**: The typed graph the UI consumes and the generated insight report — the
Graphify-inspired layer.

### 5.1 Emit the typed graph projection [category: code] (depends: 4.2, 4.3)
`kind: deliverable`

Targets:
- `crates/gwiki/src/code_wiki/graph_projection.rs`
- `crates/gwiki/src/graph/mod.rs::*` — scope-reason: 1,177-line monolith defining the GraphExport schema types; this leaf owns its mandatory same-leaf decomposition below 1,000 lines
- `crates/gwiki/src/graph/types.rs`
- `crates/gwiki/src/graph/statements.rs`
- `crates/gwiki/src/graph/memory.rs`
- `crates/gwiki/src/graph/tests.rs`
- `crates/gwiki/src/graph/export.rs::*` — scope-reason: `export_graph` constructs every extended node/edge shape
- `crates/gwiki/src/graph/analytics.rs::*` — scope-reason: analytics types derive from the extended schema
- `crates/gwiki/src/commands/graph.rs::*` — scope-reason: the live `gwiki graph` producer is rewired to re-export the committed projection
- `crates/gwiki/src/exports/graph.rs::*` — scope-reason: the GraphExport schema extension touches serialization, node/edge builders, and every consumer renderer in this file
- `crates/gwiki/src/exports/tests.rs::*` — scope-reason: every export test asserts the extended schema
- `crates/gwiki/tests/fixtures/graph_export_schema.json`
- `crates/gwiki/tests/code_wiki_graph_projection.rs`

`outputs/graph.json` has one owner: the existing export pipeline. The
`GraphExport` schema types (`GraphExport`, `GraphExportNode`,
`GraphExportEdges`, `GraphExportEdge`) are defined in `graph/mod.rs` —
1,177 lines, over the production ceiling, so this leaf owns its mandatory
same-leaf decomposition below 1,000 lines along these seams: `graph/types.rs`
takes the schema and domain types (`WikiGraphDocument`, `WikiGraphSource`,
`WikiGraphLink*`, `WikiGraphCodeEdge`, `WikiGraphFacts` with `retain_include`,
`GraphInclude`, `GraphExportOptions`, the four `GraphExport*` types,
`GraphStatement`, `WikiBacklink`, `LinkSuggestion`, `RelatedPathOptions` —
~185 lines); `graph/statements.rs` takes `document_target_map`,
`graph_write_statements`, and the node/id/label/mermaid helpers (~290 lines);
`graph/memory.rs` takes `MemoryWikiGraph` and its backlink, link-suggestion, and
related-path behavior (~225 lines); the inline `#[cfg(test)]` module moves to
`graph/tests.rs` (~450 lines), leaving `mod.rs` as declarations and re-exports
(~35 lines). Every resulting production file lands far below the ceiling and the
public path stays `graph::*`. The types are constructed by `export_graph` in
`graph/export.rs`, derived from in `graph/analytics.rs`, and written/served
through `exports/graph.rs`; all four migrate together with their tests. This
leaf extends that pipeline — it does not add a second writer. The live
`gwiki graph` command (`commands/graph.rs`) is rewired in the same leaf: it
re-exports the committed projection under the publication lock instead of
independently rebuilding `outputs/graph.json`, so the path keeps exactly one
semantic producer. `graph_projection.rs` builds the typed graph from the build
manifest, FactsBundle, and catalog; existing consumers (JSON-LD/llms
renderers, export tests) migrate in this leaf, and the #18869 web reader
adapts in its own task against the committed schema fixture this leaf publishes
at `crates/gwiki/tests/fixtures/graph_export_schema.json` — regenerated
byte-identically by the `GraphExport` serializer from a representative
cross-namespace graph and carrying the schema version #18869 consumes.

This leaf also owns the late projection check 2.2 defers to it. Running after
every upstream producer has materialized its final values, it builds the complete
projection input in memory without writing, computes the graph-projection input
digest over exactly the fields serialized below, and compares it against the
prior digest persisted in the build manifest. On a match it writes nothing and
leaves the committed `outputs/graph.json` in place; on a mismatch it emits the
projection and records the new digest through 2.1's exported
`set_graph_projection_digest`, in the same publication that commits the
projection. That ordering is what makes the digest safe to trust on the next
run: a projection that fails to emit commits neither the output nor the digest,
so the prior digest survives and the next run still sees a mismatch. Producer
and digest are the same code path over the same struct, so they cannot drift
apart, and no earlier selector has to predict values that do not exist yet. The
manifest field, its serialization, and its update API are 2.1's (§2.1
`build_manifest.rs`); this leaf calls that API and defines nothing.

Extended schema: nodes `{id, kind: module|concept|layer|page|symbol, slug,
layer, summary}` spanning both namespaces (code pages from the build manifest,
knowledge concepts from the catalog, symbol nodes for key symbols so insights
can rank symbol god nodes); edges `{src, dst, type:
contains|calls|imports|references|alias, confidence: extracted|inferred,
evidence}` (bakeoff C7). `extracted` edges carry deterministic evidence spans
(`[path:Lstart-Lend]`); `inferred` edges (clustering/naming-derived and
cross-namespace alias edges) carry their derivation source (cluster id or alias
pair) and never fabricate file spans. The 4.2 dependency guarantees concept
pages exist before projection, so concept nodes resolve on a cold full run.
Paths are vault-relative; output is deterministic and committable. This
projection is the single feed for the web graph experience (#18869) — no
whole-project graph query at request time.

**Acceptance:**

- 5.1.1 - Projection contains typed, confidence-tagged nodes/edges (including
  symbol nodes and evidence spans on extracted edges) across both namespaces
  and is deterministic. test: `crates/gwiki/tests/code_wiki_graph_projection.rs::projection_typed_and_deterministic`.
- 5.1.2 - `outputs/graph.json` has exactly one semantic producer — the
  `gwiki graph` command re-exports the committed projection; existing export
  consumers (JSON-LD, llms indexes, export tests) render from the extended
  schema; every file this leaf touches is below 1,000 lines;
  `crates/gwiki/tests/fixtures/graph_export_schema.json` is committed and
  regenerated byte-identically by the serializer; and the late projection check
  writes the projection and its digest exactly on a digest mismatch, leaves the
  committed output untouched when the digest matches, leaves the prior digest
  unadvanced when the projection fails to emit, and performs no graph write on
  the next unchanged run that reads the persisted digest back.
  test: `crates/gwiki/src/exports/tests.rs::extended_schema_serves_all_consumers`.

### 5.2 Render the insight report [category: code] (depends: 5.1)
`kind: deliverable`

Targets:
- `crates/gwiki/src/code_wiki/render/insights.rs`
- `crates/gwiki/tests/code_wiki_insights.rs`

Render `code/insights.md` from the graph projection and FactsBundle facts —
the renderer performs no source or index scanning (the deterministic renderer
boundary in Constraints applies):

- **God nodes** — top modules/symbols by in+out degree with counts and wikilinks.
- **Surprising connections** — cross-layer edges ranked by unexpectedness
  (edges whose endpoints share no layer/cluster), each cited. The section draws
  only on `extracted` edges, which carry resolvable evidence spans; `inferred`
  edges (clustering- or alias-derived) hold a derivation source rather than a
  file span, so they are excluded here instead of being cited with fabricated
  evidence.
- **Design rationale** — rendered from the typed tagged-comment spans
  (`NOTE:`/`WHY:`/`HACK:`) carried by the FactsBundle (D6), as cited items
  grouped by module.
- **Questions this wiki answers** — 4–5 generated questions, each linking the
  pages/graph paths that answer it.

Degree/ranking computations are deterministic; only the question generation is an
AI pass (daemon-routed, grounded in existing pages).

**Acceptance:**

- 5.2.1 - Insights page renders all four sections with citations; deterministic
  sections are byte-stable across runs; an inferred cross-layer edge is excluded
  from surprising connections rather than cited.
  test: `crates/gwiki/tests/code_wiki_insights.rs::insights_sections_render`.
- 5.2.2 - Rationale renders the FactsBundle's tagged-comment spans with exact
  `[path:line]` citations; a test proves the renderer performs no direct
  source or index access. test: `crates/gwiki/tests/code_wiki_insights.rs::rationale_extraction_cited`.

### 5.3 Implement the bounded diagram fallback [category: code] (depends: P2)
`kind: deliverable`

Targets:
- `crates/gwiki/src/code_wiki/diagrams.rs`
- `crates/gwiki/tests/code_wiki_diagrams.rs`

Diagram emission for the three kinds (architecture map, key flows, class
hierarchies) with the bakeoff-C4 fallback: when a graph exceeds edge/hop bounds,
emit a top-N most-central subgraph labeled `simplified — top N of M edges` instead
of suppressing the diagram. All output validates via
`gobby_core::vault::mermaid::is_valid_mermaid`. Class-hierarchy diagrams render
only where a module has a meaningful hierarchy (from gcode hierarchy views); no
dependency/import diagram kind exists.

**Acceptance:**

- 5.3.1 - Over-bound graphs produce labeled top-N diagrams, never suppression.
  test: `crates/gwiki/tests/code_wiki_diagrams.rs::truncated_graph_yields_labeled_diagram`.
- 5.3.2 - Every emitted diagram validates as mermaid; only the three permitted
  kinds are emittable. test: `crates/gwiki/tests/code_wiki_diagrams.rs::only_permitted_kinds_valid`.

## P6: Retrieval alignment and engine assembly
`kind: framing`

**Goal**: Retrieval and session-start surfaces prefer the new compact
artifacts, and the assembled engine composes every producer into the public
entrypoint.

### 6.1 Prefer compact summaries in retrieval and session injection [category: code] (depends: P3)
`kind: deliverable`

Targets:
- `crates/gwiki/src/code_wiki/summary_normalizer.rs`
- `crates/gwiki/src/catalog/overview.rs`
- `crates/gwiki/src/commands/search.rs::*` — scope-reason: snippet construction spans result assembly and `bounded_snippet`; both gain summary-first behavior, and the same entrypoint takes the shared generation lock and the committed-generation filter
- `crates/gwiki/src/commands/read.rs::*` — scope-reason: the read entrypoint takes the shared generation lock across path resolution and file read
- `crates/gwiki/tests/code_wiki_reader_generation.rs`
- `src/gobby/hooks/event_handlers/_session_start/agents.py::load_wiki_overview`
- `tests/hooks/event_handlers/test_wiki_overview_seed.py::*` — scope-reason: consumer of `load_wiki_overview`; re-anchored when that contract changes
- `crates/gwiki/tests/code_wiki_summaries.rs`
- `tests/hooks/test_wiki_overview_injection.py`

Implement the shared compact-summary normalizer (absorbed #18871 item 1): first
meaningful prose paragraph → strip Markdown structure → collapse whitespace → cap
at 180 Unicode chars on a word boundary → omit empty results. Apply it wherever a
result summary renders. Search snippet construction currently strips frontmatter
before windowing (`frontmatter_body_start` → `bounded_snippet`), so `search.rs`
changes to parse the `summary` key before stripping: snippets prefer the
frontmatter `summary` and fall back to the normalized body window, across search
backends and output formats. Catalog entries get the same summary-first
preference. `render_overview` surfaces orientation entry points (landing, tours,
insights) inside the existing 2,000-char budget. Session injection reads the
catalog-owned vault-root `_index.md` through `load_wiki_overview`; this leaf
owns proving that seam with an integration test that renders the root index via
`render_overview` and passes it through `load_wiki_overview`. That Python reader
stays outside the generation lock by design: it opens one catalog-owned file and
extracts a single block, so it cannot observe a mixed-generation join, and 3.4's
atomic replacement guarantees it reads a complete index rather than a truncated
one. Its bounded staleness — the prior generation's overview until the catalog
commits — is the accepted behavior, and `load_wiki_overview` needs no behavior
change beyond that guarantee.

This leaf also makes the two concrete retrieval entrypoints honor 2.1's
publication contract, since neither takes any lock today. `wiki_read` acquires
the generation lock in shared mode across path resolution and read, and
`wiki_search` acquires it across result assembly, both using 2.1's bounded
timeout and dirty-journal policy. Because search candidates come from the
independently refreshed BM25/semantic store rather than the published tree, the
lock alone cannot make search generation-consistent: search additionally filters
results against the committed build manifest. That filter applies only to
engine-owned generated paths, decided by the one `is_engine_owned_generated_path`
predicate 2.1 defines and exports (§2.1 `build_manifest.rs`) — a stale
generated hit is dropped, while curated `knowledge/**`, rendered source pages,
`code/INDEX.md`, and `_context.md` files stay visible under their own
lifecycles, since the build manifest never described them. This leaf imports and
calls that predicate and defines nothing; 3.4's catalog ownership check calls the
same symbol, and 2.1.3 owns the parity test proving both get one answer.
Missing-until-reindex
staleness for generated pages is the accepted, documented behavior; scheduling
the refresh is #19665's (D2).

**Acceptance:**

- 6.1.1 - Normalizer obeys the 180-char/word-boundary/no-structure contract.
  test: `crates/gwiki/tests/code_wiki_summaries.rs::normalizer_contract`.
- 6.1.2 - Search snippets and catalog entries prefer `summary` frontmatter with
  normalized-body fallback, across backends and output formats. test: `crates/gwiki/tests/code_wiki_summaries.rs::search_snippets_prefer_summary`.
- 6.1.3 - Rendering the catalog root index and passing it through
  `load_wiki_overview` yields a ≤2,000-char overview block with orientation
  entry points, and a read concurrent with catalog regeneration returns either
  the prior or the new complete block, never a partial one.
  test: `tests/hooks/test_wiki_overview_injection.py`.
- 6.1.4 - `wiki_read` and `wiki_search` acquire the shared generation lock and
  observe one generation across every publication stage, including the
  dirty-journal window before recovery; the bounded timeout surfaces its
  documented failure instead of hanging.
  test: `crates/gwiki/tests/code_wiki_reader_generation.rs::readers_observe_single_generation`.
- 6.1.5 - Search results are filtered to the committed generation for
  engine-owned generated paths only: a generated hit absent from the committed
  manifest is dropped rather than served, a generated page published since the
  last index refresh is simply missing (never half-rendered), no snippet mixes
  generations, and current `knowledge/**`, source-page, `code/INDEX.md`, and
  `_context.md` hits survive the filter unchanged across both search backends.
  test: `crates/gwiki/tests/code_wiki_reader_generation.rs::search_results_match_committed_generation`.

### 6.2 Assemble the engine entrypoint [category: code] (depends: 2.2, 2.3, 2.4, 2.5, P3, P4, P5)
`kind: deliverable`

Targets:
- `crates/gwiki/src/code_wiki/engine.rs`
- `crates/gwiki/src/code_wiki/mod.rs`
- `crates/gwiki/src/lib.rs::*` — scope-reason: crate-root re-export of the public `generate_code_wiki` entrypoint
- `crates/gwiki/tests/code_wiki_engine.rs`

`engine.rs`: the one public composition entrypoint (`generate_code_wiki`),
sequenced after every producer it composes so no leaf depends on unbuilt
downstream work. This leaf declares the `engine` module in `code_wiki/mod.rs`
and re-exports `generate_code_wiki` at crate root — 2.1 cannot declare a module
whose file does not yet exist, so declaration follows creation.

Composition order, with each step's inputs available before the step that reads
them: global preflights first — page-type manifest load and validation (2.3) and
`FACTS_VERSION` validation, both proven side-effect-free (nothing is staged, no
cache is read, no AI is invoked before the gates pass) — then the exclusive
generation lock and journal recovery (2.1), deterministic clustering membership
and its fingerprint (2.4), capability capture as a resolved execution snapshot
(2.2), invalidation and selection over the prior manifest, the prior and current
membership fingerprints, and that snapshot (2.2), deterministic renderers (P3),
AI passes (P4/P5), the late graph-projection check (5.1 — it runs here, after
every producer of a digest field has materialized its final values, and writes
`outputs/graph.json` only on a digest mismatch), the final landing refresh (the
landing page renders last, after every other page class has registered its build
record), and transactional publication (2.1). The graph projection is
deliberately the one artifact not selected in the invalidation step: its digest
covers node summaries, key-symbol membership, layer assignment, and alias pairs,
none of which exist before P3–P5 finish, so deciding it earlier would mean
hashing values the run has not produced. Membership precedes selection deliberately: the classifier
cannot detect an `ARCHITECTURE` change without the current fingerprint, so
computing membership after selection would silently skip layer, concept, tour,
and graph regeneration. Daemon and CLI adapter wiring stays deferred to #19665
(D2); this leaf completes the library surface.

**Acceptance:**

- 6.2.1 - The public entrypoint composes the full pipeline end-to-end on a
  fixture vault (compile-time registration proven by the integration test
  invoking `generate_code_wiki`). test: `crates/gwiki/tests/code_wiki_engine.rs::engine_entrypoint_generates`.
- 6.2.2 - An unsupported `FACTS_VERSION` is rejected before any side effect —
  writer, cache, and AI spies observe zero activity. test: `crates/gwiki/tests/code_wiki_engine.rs::facts_version_preflight_no_side_effects`.
- 6.2.3 - A run whose only change is cluster membership (no file edits)
  classifies ARCHITECTURE through the engine and regenerates the architecture
  set named in 2.2 — `layers.md`, `layers.json`, affected concept pages, tours,
  and the graph projection — proving membership is computed before selection; the
  graph projection is reached through the late check, whose digest mismatches
  because layer assignment moved, and a run with unchanged upstream values leaves
  `outputs/graph.json` unwritten while the rest of the pipeline still runs.
  test: `crates/gwiki/tests/code_wiki_engine.rs::membership_change_selects_architecture_set`.

## P7: End-to-end acceptance
`kind: framing`

**Goal**: One owned, executable integration deliverable proves the full engine
against the Gobby repo. Every check below is implemented and closed by 7.1 —
none floats without an owner.

### 7.1 Run full-engine end-to-end acceptance [category: test] (depends: P2, P3, P4, P5, P6)
`kind: deliverable`

Targets:
- `crates/gwiki/tests/code_wiki_end_to_end.rs`

Run the full engine (6.2 `generate_code_wiki`) against the Gobby repo in an
isolated temporary vault (never the production vault):

**Acceptance:**

- 7.1.1 - Two consecutive full generations with identical inputs are
  byte-identical for every deterministic surface; AI prose is stable when
  caches are warm. test: `crates/gwiki/tests/code_wiki_end_to_end.rs::full_generation_deterministic`.
- 7.1.2 - Total generated page count lands in 50–90; zero pages under
  `code/files/**`. test: `crates/gwiki/tests/code_wiki_end_to_end.rs::page_count_in_band`.
- 7.1.3 - All seven deterministic projection paths exist
  (`architecture/layers.md` plus `features.md`, `deprecations.md`,
  `changes.md`, `hotspots.md`, `ownership.md`, `infrastructure.md`), each with
  a build-manifest record. test: `crates/gwiki/tests/code_wiki_end_to_end.rs::projection_paths_exist`.
- 7.1.4 - Every generated page passes the anatomy lint: contract frontmatter
  with `summary` ≤180 chars, resolvable citations, table caps respected,
  collapsible provenance header present, exactly one paragraph block per
  explanatory section, and no normalized assertion repeated across two
  explanatory sections of a page. test: `crates/gwiki/tests/code_wiki_end_to_end.rs::anatomy_lint_all_pages`.
- 7.1.5 - Every module page carries at least one valid diagram or a labeled
  truncation. test: `crates/gwiki/tests/code_wiki_end_to_end.rs::module_diagrams_present`.
- 7.1.6 - `_meta/build.json` stays under the configured build-manifest ceiling
  on the real Gobby corpus; orphan pruning leaves catalog-owned files
  intact. test: `crates/gwiki/tests/code_wiki_end_to_end.rs::manifest_size_and_orphans`.
- 7.1.7 - A PARTIAL change regenerates only the expected page set; a rename
  re-slugs, preserves `page_id`, and rewrites links. test: `crates/gwiki/tests/code_wiki_end_to_end.rs::incremental_and_rename`.
- 7.1.8 - `wiki_search`, `wiki_read`, and the graph projection work over the
  new layout; root index cross-links same-slug concepts across namespaces; and a
  regeneration concurrent with all three surfaces yields one generation
  everywhere, with search returning no engine-owned generated path absent from
  the committed manifest while curated and catalog-owned hits stay visible. test: `crates/gwiki/tests/code_wiki_end_to_end.rs::retrieval_over_new_layout`.
- 7.1.9 - Production vault byte-untouched throughout (checksum before/after).
  test: `crates/gwiki/tests/code_wiki_end_to_end.rs::production_vault_untouched`.

## D1 Session-wiki ingestion scope
`kind: deferred`

The cross-project session-wiki contamination fix (Model A per-project vs Model B
global scope, `.gobby/handoff-session-wiki-scope.md`) is ingestion-side work.
Handed to #19671 with the model 2.5 delivers: wire extraction to mint
`SourceSegment` identities, surface segments as independent `wiki_search` hits
and index entries, and drive removal through 2.5's tombstone contract and
degradation derivation (including the crash/journal semantics of a removal
interrupted mid-flight). 2.5 owns the types, persistence, citation shape, and
derivation; #19671 owns creating segments and operating those surfaces.

```yaml
deferral:
  task_ref: "#19671"
  reason: "Session/multimodal ingestion scope and identity belong to the daemon-native ingestion subepic, not the output design."
  owner: "epic-19671"
  original_acceptance_items:
    - D1.1
```

## D2 Generation orchestration and triggers
`kind: deferred`

Post-index/scheduled/on-demand triggers, work-item identity, retries, recovery,
and activation gating — including the post-publication search-index refresh that
closes the bounded-staleness window 2.1 documents and 6.1 filters against.

```yaml
deferral:
  task_ref: "#19665"
  reason: "Orchestration maps onto existing Gobby primitives under its own subepic; this plan delivers the engine those primitives invoke."
  owner: "epic-19665"
  original_acceptance_items:
    - D2.1
```

## D3 Ask/research retirement sweep
`kind: deferred`

Retiring `wiki_ask`, the HTTP endpoint, web Ask/Research modes, and the
advertising surfaces (session-start template, MCP server instructions).

```yaml
deferral:
  task_ref: "#19672"
  reason: "Agent-native exploration replaces embedded answering under its own subepic; retrieval surfaces this plan touches remain wiki_search/wiki_read."
  owner: "epic-19672"
  original_acceptance_items:
    - D3.1
```

## D4 Production cutover and activation
`kind: deferred`

Backup, verified restore, explicit destructive approval, empty-vault rebuild, and
acceptance-gated activation.

```yaml
deferral:
  task_ref: "#18779"
  reason: "This plan validates only in isolated temporary vaults; the destructive cutover has its own manually approved acceptance child."
  owner: "epic-18779"
  original_acceptance_items:
    - D4.1
```

## D5 Daemon-native runtime boundary
`kind: deferred`

Authenticated handshake, short-lived scoped datastore grants, and daemon-mediated
AI execution consumed by the narrative and naming passes.

```yaml
deferral:
  task_ref: "#18902"
  reason: "Runtime boundary is a sibling subepic; this plan consumes its contracts."
  owner: "epic-18902"
  original_acceptance_items:
    - D5.1
```

## D6 Deterministic code facts and symbol summaries
`kind: deferred`

Scoped code facts, bounded call/coupling/hierarchy graph views, automatically
persisted per-symbol/per-file summaries the scaffolds and tables consume, and
typed tagged-comment spans (`NOTE:`/`WHY:`/`HACK:`) consumed by the insights
rationale renderer (5.2) and change classification (2.2).

```yaml
deferral:
  task_ref: "#17678"
  reason: "gcode owns the deterministic fact surfaces; this plan renders from them."
  owner: "epic-17678"
  original_acceptance_items:
    - D6.1
```

## V1 Plan Changelog
`kind: verification`

<!-- Rounds appended by enhancement/adversary phases. -->

**Human handoff** `kind: verification`

- authorized_by: user (interactive)
- after_round: 6
- verdict_at_handoff: needs_review
- completed_plan_review_rounds: 6
- resolution_notes: Rounds 5 and 6 surfaced no independent defects. Every finding
  in both rounds was a regression introduced by the previous round's repair — F62
  from F56, F63 from F58 — and both were repaired in round 6 with zero
  acceptance-item churn, leaving the ledger at 21 sections and 71 acceptance
  items. The user authorized explicit human handoff, which skips all remaining
  enhancement and adversary rounds. `## M1 Task Manifest` was written by
  `apply_plan_handoff_manifest` from coordinator-derived routing decisions over
  all 21 deliverables (manifest_digest `766a62fb776f19b79a924630950bc7f8366d86dd2a35314ea77be8dd8b23fd8c`,
  source_plan_hash `0458717cdd04a593125051b501c12db53fc069dcc6ae763bff17a0afab7497e8`).
  This plan carries no adversary approval verdict and no `coverage_attestation`;
  the manifest is coordinator-derived, and the last recorded verdict remains
  `needs_review` at round 6.

**Round 6** `kind: verification`

- reviewer_run: f8d19c34-8fbe-46f4-b9d7-7164650b2c4b
- reviewer_session: #10281
- verdict: needs_review
- findings:
- F62 blocking unhandled-edge 2.1 late-projection-digest-schema-parity — accepted (regression, introduced_in_round 5 via F56)
- F63 blocking traceability 6.1 shared-helper-single-owner — accepted (regression, introduced_in_round 5 via F58)
- resolution_notes: Both accepted; neither refuted. Both are self-inflicted
  regressions from the round-5 repairs, each verified by re-reading the exact
  lines round 5 edited. F62 — the F56 repair had 2.2 and 5.1 compare against a
  graph-projection input digest "persisted in the build manifest" and commit a
  replacement on mismatch, but 2.1's top-level `build.json` field list
  (`schema_version`, `engine_version`, `generation_id`, `layer_names`, `tours`,
  `rename_lineage`, `ai_artifacts`) never carried that field and 2.1.2's
  round-trip acceptance never mentioned it, so the value had no typed schema
  owner, no mutation path, and no way to survive into the next run. 2.1 now owns
  `graph_projection_digest` as an optional typed field with one exported
  mutation path, `set_graph_projection_digest`, which 5.1 calls inside the same
  publication that commits the projection; absence is defined as "no committed
  projection to trust" so cold rebuilds and unsupported or unparseable manifests
  always mismatch and regenerate. 2.1.2 round-trips the field in both its present
  and absent forms and asserts the next run reads a written digest back
  verbatim, and 5.1.2 now additionally pins that a failed projection leaves the
  prior digest unadvanced and that the next unchanged run performs no graph
  write. 5.1 gains no new target: it calls 2.1's exported API, exactly as 3.4 and
  6.1 call 2.1's exported predicate. F63 — the F58 repair appended the correct
  2.1-ownership sentence to 6.1 without deleting the sentence it replaced, so
  6.1 simultaneously claimed the predicate was one "this leaf defines" and one
  that "this leaf calls and defines nothing"; an agent handed only 6.1 received
  mutually exclusive instructions. The residual claim is deleted: 6.1 now states
  once that 2.1 defines and exports `is_engine_owned_generated_path`, that 6.1
  and 3.4 both import and call it, and that 2.1.3 owns the parity test. No
  acceptance items were added; eight of the plan's 71 items were already the
  owners of this behavior and two were tightened, so the coverage ledger changes
  only its `plan_hash`.

```json plan-review-round
{"evidence_id":"f261bf92-4557-4b21-9d93-5aba3d57ada6","plan_hash":"e6f026aee2b1f2357f8f14bd9097ab847d2bf113eaf9d607e628c792b8372bdf","round_number":6,"round_result":{"coverage_attestation":{"adjacent_variant_complete":true,"attestation_digest":"9c5e322dbc9d09e43cb6f23c9aa7a929583d3d0f8eace4d0059bab61bbda1573","cross_lane_interaction_complete":true,"disposition_counts":{"dismissed":3,"emitted_findings":2,"total":5},"evidence_id":"f261bf92-4557-4b21-9d93-5aba3d57ada6","lanes":[{"candidate_count":2,"lane_id":"requirements_traceability","status":"completed"},{"candidate_count":2,"lane_id":"repository_blast_radius","status":"completed"},{"candidate_count":1,"lane_id":"runtime_invariants","status":"completed"}],"shadow_manifest_status":{"entry_count":21,"manifest_digest":"9ec66b9e12c98c4014326a2f68f9afa4cd06e41011633756a0ef5893f0b74471","status":"valid"},"source_digest":"b431bfbabaa7c930c33671f963dd8e88c74350943d4a6bd4c8abc48b1d853332","version":1},"findings":[{"category":"unhandled-edge","causal_finding_id":"F56","causal_section_ids":["2.2","5.1","6.2"],"check_key":"late-projection-digest-schema-parity","description":"Sections 2.2 and 5.1 compare against a prior graph-projection input digest persisted in the build manifest and commit a replacement on mismatch, but section 2.1's top-level BuildManifest field list and 2.1.2 round-trip acceptance omit the digest. Section 5.1 also does not target `code_wiki/build_manifest.rs` or the manifest test, so no leaf owns a schema capable of carrying the value into the next run.","finding_id":"F62","fix":"Add a typed graph-projection input-digest field to section 2.1's BuildManifest schema, constructors, serialization, byte-identical round-trip acceptance, and recovery contract. Define the 2.1-owned update API that 5.1 calls, or explicitly add the manifest implementation and test targets to 5.1; extend 5.1 acceptance to prove a successful mismatch persists the digest, a failed projection does not advance it, and the next unchanged run reads it and performs no graph write.","introduced_in_round":5,"location":"P2 / §2.1, interacting with §§2.2, 5.1, and 6.2","prevention":"For every newly persisted invalidation key, check the typed schema inventory, constructors, serialization round-trip, publication/recovery semantics, downstream target ownership, and next-run reuse test.","principle":"Every cross-run selection value must have a typed persisted-schema owner, mutation path, and round-trip acceptance before a later leaf consumes it.","root_cause":"The F56 repair introduced a manifest-persisted graph-projection input digest in sections 2.2 and 5.1 without adding that field to section 2.1's exhaustive BuildManifest schema or assigning a target and test owner for its persistence.","section_id":"2.1","severity":"blocking"},{"category":"traceability","causal_finding_id":"F58","causal_section_ids":["2.1","3.4","6.1"],"check_key":"shared-helper-single-owner","description":"Section 6.1 says `is_engine_owned_generated_path` is a predicate “this leaf defines” and, immediately afterward, says the predicate belongs to section 2.1's `build_manifest.rs` and that 6.1 “defines nothing.” Its Targets omit `build_manifest.rs`, while section 2.1 includes it. An implementing agent that receives only section 6.1 therefore has mutually exclusive ownership instructions.","finding_id":"F63","fix":"Delete the residual statement that 6.1 defines the predicate. State consistently that section 2.1 defines and exports `is_engine_owned_generated_path`, while sections 3.4 and 6.1 only import and call it; retain section 2.1.3 as the parity-test owner.","introduced_in_round":5,"location":"P6 / §6.1, interacting with §§2.1 and 3.4","prevention":"After moving a shared symbol between plan sections, sweep every definition claim, target list, dependency edge, consumer description, and acceptance item for the old owner.","principle":"A shared helper must have one dependency-ordered implementation owner, and each self-contained consuming leaf must identify itself unambiguously as a consumer.","root_cause":"The F58 repair added the correct 2.1 ownership statement but left the prior 6.1 sentence claiming that 6.1 defines the same predicate.","section_id":"6.1","severity":"blocking"}],"reviewer_session":"#10281","round":6,"round_number":6,"verdict":"needs_review"},"session_id":"b8f54985-47f1-493d-a08a-8b37e5211bd7"}
```

**Round 5** `kind: verification`

- reviewer_run: e4f421a7-2598-4c8f-9a92-5acaf5cb881f
- reviewer_session: #10276
- verdict: needs_review
- findings:
- F56 blocking bad-sequencing 2.2 graph-projection-digest-before-selection — accepted (regression, introduced_in_round 4 via F52)
- F57 blocking unhandled-edge 4.1 layer-name-cache-invalidator-parity — accepted (regression, introduced_in_round 4 via F50)
- F58 blocking bad-sequencing 6.1 shared-generated-path-predicate-owner-order — accepted (regression, introduced_in_round 4 via F48)
- F59 blocking unhandled-edge 2.5 tombstoned-segment-read-resolution — accepted (regression, introduced_in_round 4 via F54)
- F60 blocking traceability 2.5 tombstone-constructor-and-state-machine-target-closure — accepted (regression, introduced_in_round 4 via F54)
- F61 blocking bad-sequencing 6.1 absorbed-task-anchor-reconciled-before-expansion — accepted
- resolution_notes: All six accepted; none refuted. Five are self-inflicted
  regressions from the round-4 repairs, each verified by re-reading the sections
  round 4 edited; F59, F60, and F61 were additionally verified against the live
  repository and task graph before acceptance. F56 — the round-4 F52 repair
  assigned the graph-projection input digest to 5.1 while 6.2 still ran
  invalidation before P5, so the selector was required to compare a digest whose
  fields (final node summaries, key-symbol membership, layer assignment, alias
  pairs) do not exist until P3–P5 finish. Graph selection is now split into a
  late projection check: 2.2 defines the digest and comparison rule, 5.1 builds
  the complete projection input in memory after every upstream producer has
  materialized, digests it, and writes `outputs/graph.json` plus the new digest
  only on a mismatch; 6.2's composition order places that check explicitly, and
  2.2.5, 5.1.2, and 6.2.3 assert both the mismatch and the unchanged-digest
  no-write case. F57 — the round-4 F50 repair made the membership fingerprint the
  sole naming invalidator in 4.1 while 2.2 kept prompt/schema and model-lane
  versions in the naming cache key, so a stable-membership prompt change had to
  both reuse and regenerate the same names; 4.1 and 3.2 now key reuse on the
  complete naming `ArtifactId` key from 2.2, membership stays the ARCHITECTURE
  classifier as one component of it, and 4.1.1 covers independent prompt/schema
  and model-lane transitions under stable membership. F58 — the round-4 F48
  repair had 6.1 define `is_engine_owned_generated_path` while 3.4, an earlier
  leaf 6.1 depends on through P3, had to call it; the predicate moves to 2.1's
  `build_manifest.rs`, which precedes both consumers, and 2.1.3 asserts its
  classification parity across manifest-tracked pages, `code/_meta/**`,
  `code/INDEX.md`, `_context.md`, the vault-root `_index.md`, `knowledge/**`, and
  rendered source pages. F59 — the round-4 F54 repair made tombstones
  manifest-only and deleted the rendered page, leaving 2.5.3's requirement that a
  segment citation resolve through `wiki_read` to a tombstone unsatisfiable; a
  content-free tombstone document is now retained at the stable rendered path
  with identities, anchors, and removal cause, excluded from listing, search,
  compile selection, and reindexing, while raw sources and assets are still
  deleted. F60 — verified directly against the crate: `rollback_removed_source`
  (`commands/sources.rs:464`) errors whenever the manifest already holds the
  record's id, which a retained tombstone makes permanently true, and
  `supersede_session_page` (`ingest/session_archive.rs:574`) matches entries by
  kind and canonical location with no tombstone exclusion; rollback becomes an
  explicit tombstone-to-live promotion with live duplicates still erroring,
  supersede gains the exclusion, and 2.5 Targets expand to both rollback helpers,
  `supersede_session_page`, and the full crate-wide `SourceRecord` literal
  inventory, since adding identity fields breaks every literal. F61 — verified in
  the task graph: #18871 was open, unclaimed, and `blocked_by: []` under the same
  epic, so it could have run the absorbed work in parallel with the manifest
  leaves; it is now closed as a duplicate superseded by this plan
  (`validation_status: valid`) and the Constraints entry records the settled
  disposition instead of deferring it to expansion. No new acceptance items were
  created — eight existing items were tightened — so the coverage ledger changes
  only its `plan_hash`.

```json plan-review-round
{"evidence_id":"9ed18bbe-4aa1-496d-8af1-db061f30bdaa","plan_hash":"495f1086ea49c811b833a8ebc1723e27b3eadc5787c7099a2dfeb45bc67a6aa4","round_number":5,"round_result":{"coverage_attestation":{"adjacent_variant_complete":true,"attestation_digest":"722b897362f16af14203f8fa633ae650438c2e4f60a5e86a5705b23758a6a3b1","cross_lane_interaction_complete":true,"disposition_counts":{"dismissed":6,"emitted_findings":6,"total":12},"evidence_id":"9ed18bbe-4aa1-496d-8af1-db061f30bdaa","lanes":[{"candidate_count":4,"lane_id":"requirements_traceability","status":"completed"},{"candidate_count":3,"lane_id":"repository_blast_radius","status":"completed"},{"candidate_count":5,"lane_id":"runtime_invariants","status":"completed"}],"shadow_manifest_status":{"entry_count":21,"manifest_digest":"12256527fd7d0437b27bb77c931110a0a62f52e54c455d051c1c3a682bf88cda","status":"valid"},"source_digest":"32c59da6964394d89c753e75c48e4da806c939a520e2a06be20c123c71761205","version":1},"findings":[{"category":"bad-sequencing","causal_finding_id":"F52","causal_section_ids":["2.2","5.1"],"check_key":"graph-projection-digest-before-selection","description":"Section 2.2 must compare the prior and current graph-projection input digests to select graph regeneration, but 5.1 says it emits that digest and 6.2 runs invalidation before P5. Fields such as final node summaries may not exist until upstream rendering and AI finish, so the selector cannot compute the promised current digest; unchanged call/import edges with a changed serialized node field can still skip graph regeneration.","finding_id":"F56","fix":"Split graph selection into a late projection check: after upstream page, naming, layer, and alias producers materialize their final values, build the complete projection input without writing, compute and compare its digest, then invoke 5.1 only when changed and persist the same digest. Amend 2.2, 5.1, and 6.2 and add an engine test where edges stay identical while a serialized node summary, key-symbol set, layer, or alias changes.","introduced_in_round":4,"location":"P2 §2.2, P5 §5.1, and P6 §6.2","prevention":"For every persisted invalidation digest, verify that its current-state producer runs before selection and that output-derived fields have a specified late-selection stage.","principle":"An invalidation decision must receive the current value of every digest it compares before deciding whether the digest producer runs.","root_cause":"The F52 repair assigned graph-projection input-digest emission to downstream leaf 5.1 while 6.2 retained invalidation before P5, creating a producer-selector cycle.","section_id":"2.2","severity":"blocking"},{"category":"unhandled-edge","causal_finding_id":"F50","causal_section_ids":["2.2","3.2","4.1"],"check_key":"layer-name-cache-invalidator-parity","description":"Section 2.2 defines the exact layer-name cache key as membership fingerprint plus prompt/schema and model-lane versions and requires those fields to invalidate independently. Section 4.1 simultaneously says stable membership always reuses cached names and membership is the only naming invalidator. A prompt, schema, route-model, or daemon-lane change with stable membership must therefore both regenerate and reuse the same name set.","finding_id":"F57","fix":"Make 4.1 reuse names only when the complete naming ArtifactId cache key from 2.2 matches; regenerate on membership, prompt/schema, or model-lane changes. Keep membership change as the ARCHITECTURE classifier and add stable-membership prompt/schema/model transition coverage to 4.1.1.","introduced_in_round":4,"location":"P2 §2.2 and P4 §4.1","prevention":"Compare every artifact producer's reuse rule with its ArtifactId cache-key definition and test each key component changing independently.","principle":"One cached artifact must have one consistent invalidation key across selection, production, and acceptance tests.","root_cause":"The F50 repair made membership fingerprint the sole naming invalidator in 4.1 without reconciling 2.2's existing prompt/schema and model-lane key components.","section_id":"4.1","severity":"blocking"},{"category":"bad-sequencing","causal_finding_id":"F48","causal_section_ids":["2.1","3.4","6.1","7.1"],"check_key":"shared-generated-path-predicate-owner-order","description":"Section 6.1 depends on P3 yet claims to define the single is_engine_owned_generated_path predicate shared by 3.4. Live ownership traversal is in catalog regeneration while search filtering is in commands/search.rs; 3.4 cannot compile against a symbol created by a later dependent leaf, and 6.1 does not target catalog/regenerate.rs or another shared owner.","finding_id":"F58","fix":"Move is_engine_owned_generated_path into an existing P2-owned shared target such as 2.1 build_manifest.rs, export it there, and require both 3.4 catalog reconciliation and 6.1 search filtering to call it. Add parity tests covering manifest-tracked pages, code/_index.md, code/_meta, code/INDEX.md, _context.md, knowledge pages, and rendered source pages.","introduced_in_round":4,"location":"P2 §2.1, P3 §3.4, and P6 §6.1","prevention":"For every cross-leaf shared symbol, verify target ownership, dependency direction, and independent compile closure for its earliest consumer.","principle":"A shared primitive must be owned by a prerequisite that exists before every independently closing consumer.","root_cause":"The F48 repair says later leaf 6.1 defines is_engine_owned_generated_path even though earlier leaf 3.4 must already use it for catalog ownership checks.","section_id":"6.1","severity":"blocking"},{"category":"unhandled-edge","causal_finding_id":"F54","causal_section_ids":["2.5"],"check_key":"tombstoned-segment-read-resolution","description":"The live wiki_read path returns not found when its document is absent. Section 2.5 deletes the rendered source page and keeps only manifest tombstone state, yet 2.5.3 requires the old segment citation to resolve through wiki_read to its tombstone. No targeted read-path branch or remaining anchor can satisfy that acceptance.","finding_id":"F59","fix":"Retain a minimal content-free tombstone document at the stable rendered source path with source and segment identities, anchors, and removal cause; continue deleting raw source and assets and exclude tombstone documents from listing, search, compile selection, and reindexing. Amend 2.5.2/2.5.3 and test exact reads after both CLI removal and latest-wins replacement.","introduced_in_round":4,"location":"P2 §2.5","prevention":"For each deletion state, trace citation rendering through path resolution and storage lookup to a concrete live or tombstone response.","principle":"A retained citation target needs a concrete addressable representation on the retrieval surface that acceptance uses.","root_cause":"The F54 repair made tombstones manifest-only and required rendered source pages to be deleted while retaining an acceptance that resolves tombstoned segment anchors through the existing filesystem-backed wiki_read path.","section_id":"2.5","severity":"blocking"},{"category":"traceability","causal_finding_id":"F54","causal_section_ids":["2.5"],"check_key":"tombstone-constructor-and-state-machine-target-closure","description":"Live rollback_removed_source rejects restoration whenever the manifest already contains the same id, which becomes guaranteed once remove retains that id as a tombstone; supersede_session_page iterates every matching manifest entry without a tombstone exclusion. Direct SourceRecord literals also remain in production and test files outside 2.5 Targets, including compile, refresh selection, recap, paths, transcribe, and vision sites. The promised repair therefore requires edits the leaf does not target and can fail compilation or preserve wrong state transitions.","finding_id":"F60","fix":"Expand 2.5 Targets to rollback_removed_source_state, rollback_removed_source, supersede_session_page, every affected SourceManifest supersede/remove helper, and the full crate-wide SourceRecord literal inventory. Define tombstone-to-live rollback and tombstone-excluding latest-wins matching, migrate all literals to the constructor, and add crate-compile plus index-failure rollback, repeated replacement, and previously tombstoned-session tests.","introduced_in_round":4,"location":"P2 §2.5 Targets and removal contract","prevention":"After changing a public Rust struct or remove semantics, inventory all direct literals, rollback helpers, presence checks, supersede paths, fakes, and fixtures and reconcile each with exact Targets.","principle":"Exact Targets must include every constructor and state-transition owner whose code necessarily changes under a data-shape or deletion-semantic change.","root_cause":"The F54 repair added crate-wide constructor migration and tombstone rollback/latest-wins promises in prose but targeted only execute_remove and remove_session_page at those call sites.","section_id":"2.5","severity":"blocking"},{"category":"bad-sequencing","check_key":"absorbed-task-anchor-reconciled-before-expansion","description":"The constraint says #18871 either becomes an anchor or is closed as superseded at expansion. The live task remains open and unblocked, so it can begin the same normalizer, prose, facts, layers, tours, retrieval, and regeneration work while #19664 is still blocked. The valid shadow manifest does not encode an existing-task anchor, leaving the prohibited parallel implementation path live.","finding_id":"F61","fix":"Close #18871 as superseded by wiki-output-design before manifest expansion and amend the constraint to record that completed disposition; keep the six plan sections as the sole implementation owners.","location":"Constraints task-graph reconciliation and absorbed work in §6.1","prevention":"Before approving an absorbed-task plan, verify every overlapping open task has one recorded disposition and cannot start independently of the manifest leaves.","principle":"Absorbed implementation work must have one task-graph owner before expansion so an unblocked duplicate cannot execute in parallel.","root_cause":"The plan leaves #18871 as an anchor-or-supersede choice deferred to expansion, while the live task is open and unblocked and the derived shadow manifest creates 21 new leaves without resolving that choice.","section_id":"6.1","severity":"blocking"}],"reviewer_session":"#10276","round":5,"round_number":5,"verdict":"needs_review"},"session_id":"b8f54985-47f1-493d-a08a-8b37e5211bd7"}
```

**Round 4** `kind: verification`

- reviewer_run: 4ace8dc4-16a4-4802-9c89-23f1077cf842
- reviewer_session: #10273 (56ce2bf2-384f-4cc5-886c-a9c60da93abc, codex/gpt-5.6-sol)
- verdict: needs_review
- findings:
- F47/blocking/generation lock mode transition and guard ownership — accepted
- F48/blocking/search visibility filter spans non-generated namespaces — accepted
- F49/blocking/session-overview reader outside the publication boundary — accepted
- F50/blocking/layer-naming threshold contradicts fingerprint keying — accepted
- F51/blocking/ARCHITECTURE artifact set differs between 2.2 and 6.2.3 — accepted
- F52/blocking/graph regeneration keyed on edges alone — accepted
- F53/blocking/inferred edges cannot satisfy the citation contract — accepted
- F54/blocking/source-model change misses live removal callers — accepted
- F55/blocking/#18869 schema fixture has no target path — accepted
- resolution_notes: All nine findings verified against the repository and
  accepted; five are regressions from round-3 repairs (F43, F44, F35, F37) and
  two trace to rounds 1–2 (F8, F16, F23, F29). The generation lock gained an
  explicit mode-transition and guard-ownership protocol: a dirty reader releases
  shared ownership, takes the lock exclusively, rechecks, recovers, then
  downgrades, and every lock-protected operation has a public acquiring form
  plus an internal guarded form so the writer's own catalog completion cannot
  re-enter the lock (F47; 2.1.6, 3.4.5). The committed-generation search filter
  now applies only to engine-owned generated paths through one shared
  `is_engine_owned_generated_path` predicate — the build manifest never
  described `knowledge/**`, source pages, or catalog indexes, so filtering them
  would have deleted valid hits (F48; 6.1.5, 7.1.8). The Python session-overview
  reader stays outside the lock by design (it joins nothing), and 3.4 switches
  catalog page writes from truncating `fs::write` to the crate's existing
  `write_atomic`, which is what makes that out-of-lock read complete rather than
  torn (F49; 3.4.5, 6.1.3). 4.1's undefined rename threshold was deleted in
  favor of the exact 2.4 membership fingerprint 2.2 and 3.2 already key on
  (F50). ARCHITECTURE now names one **architecture set** in 2.2 —
  `layers.md`, `layers.json`, affected concept pages, tours, graph projection —
  that 2.2.1, 2.2.5, and 6.2.3 assert verbatim (F51). Graph invalidation moved
  off call/import edges onto a graph-projection input digest covering every
  field 5.1 serializes, emitted by 5.1 itself so producer and selector cannot
  drift (F52; 2.2.5). Surprising connections draw only on `extracted` edges,
  since `inferred` edges carry a derivation source rather than a resolvable span
  (F53; 5.2.1). 2.5 picked up its two live removal callers —
  `commands/sources.rs::execute_remove` and
  `ingest/session_archive.rs::remove_session_page` — plus tombstone visibility
  rules (files still deleted, tombstones excluded from listing, selection,
  reindexing, and the latest-wins presence check, rollback clears the tombstone)
  and a canonical `SourceRecord` constructor for the crate's struct literals
  (F54; 2.5.2). 5.1 binds the #18869 handoff to a concrete path,
  `crates/gwiki/tests/fixtures/graph_export_schema.json`, in both Targets and
  5.1.2 (F55). No new acceptance items were required; eight existing items were
  tightened, so the coverage ledger changed only its plan hash.

```json plan-review-round
{"evidence_id":"35ad3ab6-c9a2-421c-ae4a-a6a6a6eb8fae","plan_hash":"33261299beab48d50f17f424ddc9201430ecd10bd54e96a82362e96f97c0853c","round_number":4,"round_result":{"coverage_attestation":{"adjacent_variant_complete":true,"attestation_digest":"70c07aca42578655683df41036840635d687d4d4de43c986170e99cdb79484fc","cross_lane_interaction_complete":true,"disposition_counts":{"dismissed":7,"emitted_findings":9,"total":16},"evidence_id":"35ad3ab6-c9a2-421c-ae4a-a6a6a6eb8fae","lanes":[{"candidate_count":4,"lane_id":"requirements_traceability","status":"completed"},{"candidate_count":3,"lane_id":"repository_blast_radius","status":"completed"},{"candidate_count":9,"lane_id":"runtime_invariants","status":"completed"}],"shadow_manifest_status":{"entry_count":21,"manifest_digest":"bcc64c5e49740334060fb0176e1ac2ec30e4bc87ae380b4c4b6eebf086ccaf5e","status":"valid"},"source_digest":"c7b1f6130f686cd30f7c77fbf51e470205857b5e415978788f8872c40cb102f7","version":1},"findings":[{"category":"unhandled-edge","causal_finding_id":"F43","causal_section_ids":["2.1","3.4","6.1"],"check_key":"generation-lock-mode-transition","description":"Section 2.1 says a reader first acquires the generation lock in shared mode and then runs journal recovery when it finds a dirty journal, even though recovery mutates the published tree. Section 3.4 separately requires `regenerate` to acquire that lock in shared mode, while 2.1 holds the exclusive writer lock through staged catalog completion. The stated protocol therefore permits two shared readers to race recovery and makes the writer recursively acquire the same lock through catalog regeneration, which can self-deadlock or time out.","finding_id":"F47","fix":"Define a guard-aware protocol. A dirty reader must release shared ownership, acquire exclusive ownership, recheck and recover, then downgrade or reacquire shared before serving. Split catalog regeneration into a public lock-acquiring wrapper and an internal function that accepts the writer's existing exclusive guard. Add two-dirty-reader recovery, writer-through-catalog, and `_gwiki/index.lock` order tests.","introduced_in_round":3,"location":"P2/P3/P6 / §§ 2.1, 3.4, and 6.1 generation-lock protocol","prevention":"For every lock-protected entrypoint, trace clean read, dirty recovery, nested writer calls, upgrades, downgrades, and secondary-lock ordering with concurrent tests.","principle":"Mutating recovery and nested generation work must have an explicit lock mode and guard-ownership contract.","root_cause":"The F43 repair added shared-lock reader adoption and dirty-journal recovery without separating lock-acquiring public entrypoints from mutation-capable, guard-aware internal operations.","section_id":"2.1","severity":"blocking"},{"category":"unhandled-edge","causal_finding_id":"F44","causal_section_ids":["2.1","3.4","6.1","7.1"],"check_key":"search-visibility-namespace-filter","description":"Section 6.1.5 drops any search hit whose path is absent from the committed build manifest. That manifest records engine-owned generated code pages, while the plan preserves unified retrieval across curated `knowledge/**`, rendered sources, the catalog-owned root and `code/INDEX.md`, and `_context.md` files. Applying the rule as written removes valid non-engine results from `wiki_search`; the current search entrypoint is vault-wide and has no manifest-namespace restriction to prevent that.","finding_id":"F48","fix":"Define one `is_engine_owned_generated_path` predicate shared with catalog ownership. Apply build-manifest membership filtering only to those paths, and explicitly admit curated knowledge, source pages, root/catalog indexes, and context pages under their own lifecycle rules. Add stale generated-code, current knowledge, source, catalog, and context result cases across both search backends.","introduced_in_round":3,"location":"P2/P3/P6/P7 / §§ 2.1, 3.4, 6.1.5, and 7.1 retrieval","prevention":"For every visibility filter, enumerate all search namespaces and classify each as generation-owned, catalog-owned, curated, or external before defining inclusion rules.","principle":"A generation-membership filter may reject only paths owned by the generation manifest that defines that membership.","root_cause":"The F44 repair converted the search commit boundary into an unqualified build-manifest membership filter even though the build manifest owns only generated code-wiki artifacts.","section_id":"6.1","severity":"blocking"},{"category":"traceability","causal_finding_id":"F43","causal_section_ids":["2.1","3.4","6.1"],"check_key":"session-overview-generation-lock","description":"`load_wiki_overview` directly opens the catalog-owned vault-root `_index.md`, and the plan targets that function while saying it needs no behavior change. The writer holds exclusive ownership through staged catalog completion, but 6.1.4 gives shared-lock adoption only to `wiki_read` and `wiki_search`. Session startup can therefore read the root index outside the generation lock during publication or before dirty-journal recovery, contradicting the all-reader atomicity contract.","finding_id":"F49","fix":"Route `load_wiki_overview` through a generation-aware gwiki read operation, or implement the same bounded shared-lock and dirty-journal recovery protocol in the Python reader. Extend `tests/hooks/test_wiki_overview_injection.py` with publication-stage, dirty-journal, timeout, and recovery-error cases rather than testing only block layout and size.","introduced_in_round":3,"location":"P3/P6 / §§ 3.4 root-index ownership and 6.1 session injection","prevention":"Inventory direct file readers by artifact path across language boundaries, not only command entrypoints, and exercise each during publication and dirty-journal recovery.","principle":"Every direct reader of a transactionally published artifact must participate in the same read/recovery boundary.","root_cause":"The F43 repair assigned lock adoption to Rust `wiki_read`, `wiki_search`, and catalog readers but retained the Python session overview's direct filesystem read as behavior-preserving.","section_id":"6.1","severity":"blocking"},{"category":"unhandled-edge","causal_finding_id":"F23","causal_section_ids":["2.2","3.2","4.1"],"check_key":"layer-name-membership-transition-parity","description":"Section 2.2 keys cluster/layer names on the exact 2.4 membership fingerprint and says nothing else renames stable membership; section 3.2 reuses cached names only when that fingerprint is unchanged. Section 4.1 instead says renaming occurs only when membership changes beyond an unspecified threshold. A below-threshold membership change must therefore both miss the exact cache key and preserve the old name, leaving selection, persisted `layer_names`, and `layers.json` without a coherent outcome.","finding_id":"F50","fix":"Choose one contract. Prefer exact fingerprint change as the deterministic naming invalidator and remove the threshold language, or define separate exact membership and deterministic naming-equivalence states with a specified threshold and persisted digest. Add unchanged, below-boundary, at-boundary, and above-boundary cache/rename tests.","introduced_in_round":2,"location":"P2-P4 / §§ 2.2, 3.2, and 4.1 layer naming","prevention":"State every transition predicate once and reuse it in cache keys, invalidation classes, warm reuse, rendering, and boundary tests.","principle":"Cache identity, reuse, and regeneration must use one deterministic predicate for the same state transition.","root_cause":"The F23 repair made the exact membership fingerprint the naming cache's exact and only semantic invalidator but retained 4.1's older undefined beyond-threshold rename rule.","section_id":"4.1","severity":"blocking"},{"category":"unhandled-edge","causal_finding_id":"F35","causal_section_ids":["2.2","4.3","6.2"],"check_key":"architecture-regeneration-set-parity","description":"Section 2.2 defines ARCHITECTURE as regenerating `layers.md`, affected concept pages, and the graph projection. Section 6.2.3 defines the complete membership-only set as `layers.md`, `layers.json`, affected concept pages, tours, and the graph projection. Because 2.2.1 claims the exact set defined in 2.2 and 2.2.5 only says “full architecture set,” the selector can satisfy its own leaf while omitting `layers.json` and tours required by the engine.","finding_id":"F51","fix":"Define one named ARCHITECTURE artifact set in 2.2 that explicitly includes `layers.md`, `layers.json`, affected concept pages, tours, and graph projection; make 2.2.1, 2.2.5, and 6.2.3 reference it verbatim. Test both membership-only and layer-assignment-only changes against exact artifact IDs.","introduced_in_round":3,"location":"P2/P6 / §§ 2.2.1, 2.2.5, and 6.2.3","prevention":"Maintain one canonical artifact-by-input matrix and reference its named set from classification prose, focused tests, and engine-level tests.","principle":"A change class must name one complete artifact set at every selector and engine acceptance boundary.","root_cause":"The F35 repair added the complete membership-only architecture set to 6.2.3 but did not update 2.2's normative ARCHITECTURE rule or its exact-set acceptance.","section_id":"2.2","severity":"blocking"},{"category":"unhandled-edge","causal_finding_id":"F8","causal_section_ids":["2.2","5.1"],"check_key":"graph-node-field-invalidation","description":"Section 5.1 serializes module, concept, layer, page, and symbol nodes with slugs, layers, and summaries. Section 2.2 nevertheless regenerates the graph on a PARTIAL change only when call/import edges changed. A symbol fact, key-symbol set, page summary, or other projected node-field change can keep the call/import edges identical and leave `outputs/graph.json` stale; 2.2.5 covers only the edge-change case.","finding_id":"F52","fix":"Define and persist a graph-projection input digest covering every serialized node and edge field and select graph regeneration whenever it changes. Extend 2.2.5 with unchanged-edge cases for symbol membership, node summary, layer assignment, alias metadata, and every other projected field.","introduced_in_round":1,"location":"P2/P5 / §§ 2.2 classification and 5.1 GraphExport schema","prevention":"Derive invalidation from the complete serializer field-to-input matrix and test one stable-edge change for every node and edge field.","principle":"Every serialized graph field must be invalidated by every input that can change its bytes, not only by edge-set changes.","root_cause":"The F8 repair tied PARTIAL graph regeneration to call/import edge changes while the later graph schema also serializes symbol nodes and node summaries.","section_id":"2.2","severity":"blocking"},{"category":"unhandled-edge","causal_finding_id":"F16","causal_section_ids":["5.1","5.2"],"check_key":"inferred-edge-citation-resolution","description":"The plan-wide grounding rule requires every generated claim to trace to a resolvable path citation, and 5.2 requires every surprising cross-layer connection to be cited. Section 5.1 permits inferred clustering and alias edges to carry only a cluster id or alias pair and explicitly forbids fabricated file spans. An inferred edge selected as surprising therefore has no evidence shape that can satisfy 5.2's citation requirement.","finding_id":"F53","fix":"Either restrict the cited surprising-connections section to extracted edges with resolvable evidence spans, or extend inferred edges with deterministic supporting repository or source citations while preserving the inference label. Add an inferred cross-layer fixture that renders and resolves its evidence end to end.","introduced_in_round":1,"location":"Constraints and P5 / §§ 5.1-5.2 graph evidence","prevention":"For each consumer-required citation, round-trip every producer confidence/evidence variant through rendering and resolution before accepting the schema.","principle":"An output required to be cited may consume only evidence shapes that can resolve under the plan's citation contract.","root_cause":"The F16 repair distinguished inferred graph edges by storing only a cluster-id or alias-pair derivation while retaining the requirement that every surprising connection is cited with resolvable evidence.","section_id":"5.2","severity":"blocking"},{"category":"traceability","causal_finding_id":"F37","causal_section_ids":["2.5"],"check_key":"source-model-live-caller-blast-radius","description":"Section 2.5 adds a required `source_id` and tombstone state to `SourceRecord` and changes `SourceManifest::remove` from hard deletion to retention, but its targets omit numerous direct `SourceRecord { ... }` constructors and the live removal callers. In the current repository, `commands/sources.rs::execute_remove` and `ingest/session_archive.rs::remove_session_page` assume the record is removed and then delete files or perform rollback, while compilation, selection, citation-quality, ingest, recap, vision, and tests construct the record directly. The leaf can therefore fail to compile and can change list, reindex, cleanup, and rollback behavior despite passing its manifest-only tombstone test.","finding_id":"F54","fix":"Target and migrate every production constructor/literal to one canonical constructor or initialize the new fields explicitly. Target `commands/sources.rs`, `ingest/session_archive.rs`, and all list/read/reindex consumers; define whether tombstones are visible, indexable, file-backed, and rollbackable. Add CLI removal, archive cleanup, rollback-after-failure, list, reindex, legacy-read, and citation-resolution tests. Keep segment creation and ingestion orchestration in #19671.","introduced_in_round":3,"location":"P2 / § 2.5 SourceRecord and SourceManifest transition","prevention":"For persisted records, enumerate constructors, struct literals, serializers, list/read/index consumers, remove/rollback callers, and cleanup paths before finalizing targets.","principle":"A persisted type and state-transition change must own every constructor and live caller whose compile-time or behavioral contract changes.","root_cause":"The F37 repair retargeted 2.5 to the existing source model owners but stopped at the type, manifest, renderer, and citation modules instead of following direct `SourceRecord` construction and removal calls.","section_id":"2.5","severity":"blocking"},{"category":"traceability","causal_finding_id":"F29","causal_section_ids":["5.1"],"check_key":"graph-schema-fixture-handoff-artifact","description":"Section 5.1 says #18869 will adapt against a committed schema fixture published by this leaf, but its Targets list only Rust source/test owners and acceptance 5.1.2 names no fixture artifact. The coverage ledger mirrors that acceptance, so 5.1 can close without committing the only concrete handoff #18869 is told to consume.","finding_id":"F55","fix":"Choose the canonical checked-in fixture path, add it to 5.1 Targets, and bind that exact file to acceptance. Require the Rust GraphExport serializer to regenerate it byte-identically from a representative cross-namespace graph and record the schema version #18869 must consume.","introduced_in_round":2,"location":"P5 / § 5.1 targets and acceptance 5.1.2","prevention":"For every typed deferral or downstream consumer fixture, bind the producer task, canonical path, format/version, generation test, and consumer task in the target inventory.","principle":"A committed cross-task handoff must have one exact target path and an acceptance artifact that proves it is produced.","root_cause":"The F29 repair promised #18869 a committed schema fixture but added neither a target path nor a file-bound acceptance criterion.","section_id":"5.1","severity":"blocking"}],"reviewer_session":"#10273","round":4,"round_number":4,"verdict":"needs_review"},"session_id":"b8f54985-47f1-493d-a08a-8b37e5211bd7"}
```

**Round 3** `kind: verification`

- reviewer_run: 258e940d-fc79-4ff1-a58a-33a529f54f00
- reviewer_session: #10269 (79968c18-5870-4010-9f09-09fb657b651f, codex/gpt-5.6-sol)
- verdict: needs_review
- findings:
- F35/blocking/invalidation runs before membership snapshot — accepted
- F36/blocking/2.5 Rust module path collision — accepted
- F37/blocking/external-source live-surface ownership — accepted
- F38/blocking/prose contract has no executable predicate — accepted
- F39/blocking/FactsBundle precondition absent from task graph — accepted
- F40/blocking/late module registration ownership — accepted
- F41/blocking/graph decomposition targets unnamed — accepted
- F42/blocking/writer serialization scope too narrow — accepted
- F43/blocking/reader lock and crash-window coverage — accepted
- F44/blocking/search-index generation commit boundary — accepted
- F45/blocking/AI execution snapshot coherence — accepted
- F46/blocking/artifact cache state cardinality — accepted
- resolution_notes: All 12 findings accepted; 8 were regressions introduced by
  round-2 repairs (F19, F20, F22, F28, F32) and are corrected at their causal
  sections. Composition order in 6.2 now runs clustering membership (2.4) and
  its fingerprint *before* capability capture and selection, since the
  classifier cannot detect an ARCHITECTURE change without the current
  fingerprint; 2.2 takes both fingerprints as declared inputs, gains the
  `depends: 2.4` edge, and 6.2.3 proves a membership-only change regenerates the
  full architecture set (F35). 2.5 was retargeted onto the existing
  `crates/gwiki/src/sources/` tree — `sources.rs` cannot coexist with
  `sources/mod.rs`, and `SourceRecord`/`SourceManifest`/`citations.rs` already
  model external sources — so the leaf now extends them with content-derived
  `source_id`, `SourceSegment` identity, the unified `Citation` shape, rendered
  segment anchors, and tombstoning `SourceManifest::remove` (which today drops
  entries and dangles their citations), with 2.5.3 proving segment-citation
  resolution and D1 carrying the named handoff to #19671 (F36, F37). The prose
  contract gained mechanical predicates — one paragraph block per explanatory
  section, and a duplicate claim defined as the same normalized assertion
  (lowercased, whitespace-collapsed, citations/wikilinks/trailing punctuation
  stripped) in two explanatory sections of a page — enforced before staging by
  4.2.3 and re-checked by the 7.1.4 anatomy lint (F38). #19664 is now
  `blocked_by` #17678 in the live task graph, so FactsBundle-consuming leaves
  cannot start before their producer; #19668 was already transitive through
  #18902 (F39). A module-root ownership constraint makes each leaf declare the
  `mod` and re-exports for files it creates, and 6.2 targets
  `code_wiki/mod.rs` and `lib.rs` so `engine.rs` is declared only after it
  exists (F40). 5.1 names its decomposition destinations with projected counts:
  `graph/types.rs` (~185), `graph/statements.rs` (~290), `graph/memory.rs`
  (~225), `graph/tests.rs` (~450), leaving `mod.rs` at ~35 (F41). Publication
  became one serialized writer transaction — the exclusive lock is taken before
  journal recovery and any prior-manifest or cache read and held through catalog
  completion, with 2.1.8 covering two cold and warm writers (F42) — and the lock
  is exported as a public primitive with a dirty-journal recovery policy and
  bounded timeout, adopted by `wiki_read`/`wiki_search` in 6.1 (6.1.4) and by
  catalog readers in 3.4 (3.4.5), so no two leaves edit one reader file (F43).
  Search cannot be made atomic by a filesystem lock because candidates come from
  an independently refreshed BM25/semantic store, so the boundary is now stated
  outright: results are filtered against the committed manifest, dangling hits
  are dropped, missing-until-reindex is documented bounded staleness, and the
  refresh trigger stays deferred to #19665 (6.1.5, D2) (F44).
  `GenerationCapabilities` became an immutable resolved execution snapshot
  (availability, route/profile, provider/model or daemon-lane version,
  prompt/schema digests, transport handle) that both keys selection and executes
  every AI pass, with 2.2.6 covering post-capture mutation (F45). Per-page
  `ai_cache_key` and `ai_outcomes` collapsed into one `ai_artifacts` map keyed by
  stable `ArtifactId` carrying `{cache_key, outcome, cause, failure_count,
  owner}` at the same cardinality as selection, with page records referencing
  artifact ids and non-page artifacts persisting without a BuildRecord (F46).
  The companion coverage ledger was extended with 2.1.8, 2.2.6, 2.5.3, 3.4.5,
  4.2.3, 6.1.4, 6.1.5, and 6.2.3.

```json plan-review-round
{"evidence_id":"ef71dff2-e637-4fa4-8290-6bae7d63cb1c","plan_hash":"1d669093b1d9b001123b1f437ee0017e9058133e799d7a5767a5a2f6d490f7be","round_number":3,"round_result":{"coverage_attestation":{"adjacent_variant_complete":true,"attestation_digest":"e79b1466d4af3cd7d5bb10687055e9ea6bb105f7a0386d4fb24fc2a2f7cb9ec5","cross_lane_interaction_complete":true,"disposition_counts":{"dismissed":4,"emitted_findings":12,"total":16},"evidence_id":"ef71dff2-e637-4fa4-8290-6bae7d63cb1c","lanes":[{"candidate_count":4,"lane_id":"requirements_traceability","status":"completed"},{"candidate_count":4,"lane_id":"repository_blast_radius","status":"completed"},{"candidate_count":8,"lane_id":"runtime_invariants","status":"completed"}],"shadow_manifest_status":{"entry_count":21,"manifest_digest":"d4dfed0ed5f708e1a432b55fc5fa660e7596e55cdf6af124ce396cbe64c6ef66","status":"valid"},"source_digest":"997ed566e207a960a84dcd8b46d684bc6ac314d7e0dd1b3870af9ee0a4de7981","version":1},"findings":[{"category":"bad-sequencing","causal_finding_id":"F19","causal_section_ids":["6.2"],"check_key":"invalidation-after-membership-snapshot","description":"Section 6.2 captures capabilities and runs 2.2 invalidation before computing 2.4 clustering membership. Section 2.2 classifies module-membership or layer-assignment changes as ARCHITECTURE, so the selector cannot compare the current membership fingerprint and can omit required layer, concept, tour, or graph regeneration.","finding_id":"F35","fix":"After global preflights, compute deterministic 2.4 membership and its fingerprint before 2.2 selection; pass prior and current fingerprints into invalidation. Add an engine-level graph-only membership-change test that selects the complete ARCHITECTURE artifact set.","introduced_in_round":2,"location":"P6 / § 6.2 engine composition order","prevention":"For each pipeline selector, trace every classification input to an earlier producer and add a cold-run transition test.","principle":"A selector must receive every current-state producer whose output determines its classification.","root_cause":"The F19 assembly repair placed 2.2 invalidation before the 2.4 clustering membership producer.","section_id":"6.2","severity":"blocking"},{"category":"gobby-format","causal_finding_id":"F32","causal_section_ids":["2.5"],"check_key":"rust-module-path-collision","description":"Section 2.5 targets crates/gwiki/src/sources.rs, while lib.rs already declares pub mod sources and the live module is crates/gwiki/src/sources/mod.rs with its model in sources/types.rs and sources/manifest.rs. Rust rejects simultaneous sources.rs and sources/mod.rs ownership, so the leaf cannot compile.","finding_id":"F36","fix":"Replace sources.rs with exact targets in the existing sources/ tree, including sources/mod.rs, sources/types.rs, and sources/manifest.rs; reconcile existing exports and constructors and add a public-module compile/round-trip test.","introduced_in_round":2,"location":"P2 / § 2.5 external-source model targets","prevention":"Before adding a Rust module target, inventory lib.rs declarations, same-name file/directory modules, exports, constructors, and serialization owners.","principle":"A Rust module target must extend the repository's registered module owner without creating an ambiguous file/module pair.","root_cause":"The F32 repair invented sources.rs without inspecting the existing sources/mod.rs module tree.","section_id":"2.5","severity":"blocking"},{"category":"traceability","causal_finding_id":"F32","causal_section_ids":["2.5"],"check_key":"external-source-live-surface-ownership","description":"Section 2.5 promises unified repository/segment citations, wiki_search/wiki_read addressability, and deletion that tombstones segments and degrades dependent artifacts. Its targets and tests cover only a contract/model round-trip and deletion-state assertion; the live removal, citation, read, search/index, build-manifest, frontmatter, and engine owners are absent, so every listed acceptance can close without the promised surfaces being connected or crash-consistent.","finding_id":"F37","fix":"Make 2.5 depend on 2.3 and target the existing sources/removal modules, citations.rs, commands/read.rs, commands/search.rs and index ownership, code_wiki/build_manifest.rs, render/frontmatter.rs, and publication/engine seams. Add integration acceptance that registers and retrieves a segment through both live surfaces, deletes its source with documented lock/journal semantics, and verifies tombstones, non-dangling citations, search/read visibility, and degraded dependent artifacts. If operational deletion belongs to #19671, narrow 2.5 and assign these exact integration criteria to that owner.","introduced_in_round":2,"location":"P2/P6/P7 / §§ 2.5, 6.2, 7.1","prevention":"For each new persisted entity, inventory create/read/search/cite/delete/recovery consumers, reverse dependencies, lock order, and end-to-end tests.","principle":"An information-model requirement is complete only when every promised live consumer and state transition has an owned target and executable acceptance.","root_cause":"The F32 repair added a model leaf but left retrieval, citation serialization, dependency persistence, removal, and publication consumers outside its targets.","section_id":"2.5","severity":"blocking"},{"category":"weak-testability","check_key":"prose-contract-executable-lint","description":"The plan requires exactly one short paragraph per explanatory section and no repeated claim across sections, but 4.2 acceptance checks only grounding/degradation and summaries, while 7.1 anatomy lint omits both rules. “Same claim” has no mechanical identity definition, so an implementation cannot produce bounded pass/fail evidence.","finding_id":"F38","fix":"Define duplicate identity, for example an identical normalized cited assertion across explanatory sections; reject violations before staging. Add focused 4.2 tests and extend 7.1 anatomy lint with paragraph-count and duplicate-claim cases.","location":"P4/P7 / §§ 4.2 and 7.1 prose contracts","prevention":"Map every normative prose constraint to a deterministic predicate, focused renderer test, and end-to-end lint assertion.","principle":"Every normative output rule needs an objective identity rule and executable acceptance at its enforcement owner.","root_cause":"The plan states paragraph and duplicate-claim constraints in prose but tests only citations, degradation, summaries, and a narrower anatomy lint.","section_id":"4.2","severity":"blocking"},{"category":"bad-sequencing","check_key":"external-facts-task-precondition","description":"The plan declares #17678's FactsBundle, bounded graph views, summaries, and tagged-comment spans as prerequisites, but the live task graph makes #19664 and #17678 sibling tasks blocked only by #18902. Once #18902 closes, this plan can expand and start FactsBundle-consuming leaves before the producer exists.","finding_id":"F39","fix":"Add a task-graph dependency making #19664 blocked by #17678, or gate every expanded FactsBundle-consuming leaf on #17678. Preserve the existing #19668 → #18902 ordering.","location":"Constraints / P2-P7 FactsBundle-consuming leaves","prevention":"For every external sequencing precondition, compare plan prose with the live blocked_by graph and require a transitive producer-before-consumer path.","principle":"An external producer named as a hard input precondition must be enforced in the live task dependency graph.","root_cause":"The plan consumes #17678 FactsBundle outputs, while #19664 and #17678 are siblings that become runnable together after #18902.","section_id":"2.4","severity":"blocking"},{"category":"bad-sequencing","causal_finding_id":"F19","causal_section_ids":["6.2"],"check_key":"late-module-registration-ownership","description":"Section 6.2 creates engine.rs but targets only that file and its integration test. Section 2.1 cannot declare engine before the file exists without failing its own compile gate; if it omits the declaration, 6.2 never compiles or publicly exports generate_code_wiki. The same module-root ownership check must be applied to adjacent later code_wiki and render leaves.","finding_id":"F40","fix":"Add code_wiki/mod.rs and the required lib.rs re-export target to 6.2, declaring engine only after engine.rs exists. Assign code_wiki/mod.rs or render/mod.rs to every adjacent later leaf that introduces a module so each expanded leaf compiles independently.","introduced_in_round":2,"location":"P6 / § 6.2 engine entrypoint targets","prevention":"For every late-created Rust file, assign its mod declaration and required re-export to the same leaf and compile that leaf independently.","principle":"The leaf that creates a Rust module must own its declaration and public re-export at a point when all referenced files exist.","root_cause":"The F19 repair moved engine.rs and generate_code_wiki to 6.2 but left code_wiki/mod.rs and lib.rs exclusively targeted by the earlier foundation leaf.","section_id":"6.2","severity":"blocking"},{"category":"gobby-format","causal_finding_id":"F28","causal_section_ids":["5.1"],"check_key":"monolith-decomposition-target-inventory","description":"Section 5.1 requires graph/mod.rs to be decomposed below 1,000 lines in the same leaf, yet its Targets name no destination modules. The file contains declarations, schema/domain types, MemoryWikiGraph behavior, helpers, and inline tests, so the leaf is neither self-contained nor target-complete and cannot prove the resulting production files satisfy the ceiling.","finding_id":"F41","fix":"Name exact output targets and seams, such as graph/types.rs for schema/domain types and graph/memory.rs for MemoryWikiGraph behavior, plus the chosen test relocation. Record projected line counts for graph/mod.rs and every resulting production module.","introduced_in_round":2,"location":"P5 / § 5.1 graph schema decomposition","prevention":"Whenever an existing production file is at or above 1,000 lines, list current responsibilities, exact destination targets, and projected production line counts in the owning leaf.","principle":"A mandatory same-leaf decomposition must name every created production file and a bounded responsibility seam.","root_cause":"The F28 repair identifies graph/mod.rs as a 1,177-line monolith but leaves its decomposition outputs unspecified.","section_id":"5.1","severity":"blocking"},{"category":"unhandled-edge","causal_finding_id":"F20","causal_section_ids":["2.1"],"check_key":"generation-writer-serialization-scope","description":"Two concurrent generations can read the same prior manifest, make stale invalidation decisions, mint independent cold-start page IDs, and stage in parallel; their publications then serialize only at swap time, allowing the later run to clobber the first with state derived from an obsolete generation.","finding_id":"F42","fix":"Acquire the per-vault exclusive generation lock before recovery, prior-manifest/cache reads, invalidation, identity minting, and staging, and hold it through manifest/catalog completion. An alternative must revalidate generation_id under the lock with compare-and-swap and restart selection on mismatch. Add two-writer cold/warm concurrency acceptance.","introduced_in_round":2,"location":"P2/P6 / §§ 2.1 and 6.2 writer lifecycle","prevention":"Test two cold and warm writers from prior-manifest read through commit, requiring serialization or generation compare-and-swap restart.","principle":"Selection, identity minting, staging, and publication derived from one prior manifest form one serialized writer transaction.","root_cause":"The F20 repair begins exclusive writer ownership only at first page swap, after 6.2 has already read prior state, selected work, minted identities, run AI, and staged output.","section_id":"2.1","severity":"blocking"},{"category":"unhandled-edge","causal_finding_id":"F20","causal_section_ids":["2.1"],"check_key":"reader-recovery-lock-coverage","description":"Current wiki_read, wiki_search, catalog, and export entrypoints do not acquire a shared generation lock, and 2.1 does not target them. A process crash also releases the writer lock while journaled file swaps may remain incomplete, so a reader can acquire the shared lock during the exact “between crash and recovery” window that 2.1.6 claims is atomic.","finding_id":"F43","fix":"Expose the generation lock inside gwiki and target commands/read.rs, commands/search.rs, catalog reads, graph/export commands, and every direct manifest reader. Define timeout behavior plus a dirty-journal policy that recovers or blocks before reading, and test the actual subprocess-backed read/search surfaces during publication and pre-recovery.","introduced_in_round":2,"location":"P2/P6 / §§ 2.1 and 6.1 reader atomicity","prevention":"Inventory every direct reader and test each at every publication stage, including after lock release with a dirty journal and before recovery.","principle":"Reader atomicity requires every concrete reader to share the lock and to handle a dirty publication journal after a crashed writer releases that lock.","root_cause":"The F20 repair states an all-reader shared lock without targeting reader entrypoints or defining what readers do between writer crash and recovery.","section_id":"2.1","severity":"blocking"},{"category":"unhandled-edge","causal_finding_id":"F20","causal_section_ids":["2.1"],"check_key":"search-index-generation-commit-boundary","description":"After page publication and before the separate index command refreshes its stores, wiki_search can miss new pages or return removed paths while build.json and wiki_read expose the new generation. A shared filesystem lock cannot make that independent index snapshot atomic.","finding_id":"F44","fix":"Define the search-index commit boundary: build a generation-tagged index and atomically switch it with build.json, index under the same exclusive transaction before publication becomes visible, or explicitly gate search until #19665 commits an indexed generation. Add tests after file commit but before refresh, during refresh, and after recovery.","introduced_in_round":2,"location":"P2/P6/P7 / §§ 2.1, 6.1, 7.1 search visibility","prevention":"For every claimed atomic reader, inventory non-file backing stores and test before, during, and after their generation switch.","principle":"A search surface belongs to one generation only when its candidate index and returned files cross the same observable commit boundary.","root_cause":"The F20 repair covers filesystem artifacts with a generation lock while wiki_search obtains candidates from an independently refreshed PostgreSQL or local index.","section_id":"2.1","severity":"blocking"},{"category":"unhandled-edge","causal_finding_id":"F22","causal_section_ids":["2.2"],"check_key":"ai-execution-snapshot-coherence","description":"A configuration or daemon-lane change after GenerationCapabilities capture can make invalidation select against one model-lane digest and execute or persist output under another. Availability-only capture therefore does not restore the claimed pure selector/cache contract.","finding_id":"F45","fix":"Replace the availability-only input with an immutable resolved execution snapshot containing availability, route, tier/profile, provider/model or daemon-lane version, prompt/schema digest, and executable transport handle. Derive keys and execute every AI pass from it; test a lane/config mutation after capture.","introduced_in_round":2,"location":"P2/P6 / §§ 2.2 and 6.2 capability capture","prevention":"Enumerate every volatile selector used by a cache key or generator and require selection and execution to share one resolved snapshot object.","principle":"Selection, cache identity, and execution must consume one immutable snapshot of every volatile AI route selector.","root_cause":"The F22 repair snapshots daemon AI availability only, while 2.2 cache keys also depend on model-lane version and execution resolves route/profile/provider/model later.","section_id":"2.2","severity":"blocking"},{"category":"traceability","causal_finding_id":"F22","causal_section_ids":["2.1","2.2"],"check_key":"artifact-cache-state-cardinality","description":"Section 2.2 selects and records module prose slots, concept prose, layer names, tour steps, and insight questions independently and says every cache key persists in build records. The declared schema has one ai_cache_key in each page BuildRecord, while non-page artifacts have no BuildRecord and ai_outcomes declares only outcome/cause/failure state, so independent keys cannot round-trip.","finding_id":"F46","fix":"Persist one stable ArtifactId-keyed state map containing cache_key, typed outcome, failure count, and owning page or non-page identity. Page records may reference artifact IDs without collapsing them to one key. Add independent hit/failure/repair serialization tests for page slots and every non-page artifact class.","introduced_in_round":2,"location":"P2-P5 / §§ 2.1 and 2.2 AI artifact state","prevention":"For every independently selectable artifact class, map one stable artifact ID to its key, outcome, owner, serialization, and repair tests.","principle":"Persisted cache identity and outcome state must have the same artifact cardinality as independent regeneration selection.","root_cause":"The F22 repair adds per-artifact outcomes while retaining one ai_cache_key per page and no build record for non-page AI artifacts.","section_id":"2.1","severity":"blocking"}],"reviewer_session":"#10269","round":3,"verdict":"needs_review"},"session_id":"b8f54985-47f1-493d-a08a-8b37e5211bd7"}
```

**Round 2** `kind: verification`

- reviewer_run: 5498b2cc-6b80-47d0-8bb3-0fbb6a0dd953
- reviewer_session: #10265 (16e69972-dbac-4142-a267-ddb66d47b1d8, codex/gpt-5.6-sol)
- verdict: needs_review
- findings:
- F19/blocking/engine composition dependency cycle — accepted
- F20/blocking/publication reader commit boundary — accepted
- F21/blocking/bundled template integrity routing — accepted
- F22/blocking/AI capability repair state — accepted
- F23/blocking/artifact-specific cache digests — accepted
- F24/blocking/frontmatter builder input completeness — accepted
- F25/blocking/cold-run producer dependencies — accepted
- F26/blocking/facts-version entrypoint preflight — accepted
- F27/blocking/deterministic renderer source boundary — accepted
- F28/blocking/graph schema actual-owner coverage — accepted
- F29/blocking/graph live producer and consumer handoff — accepted
- F30/blocking/build-manifest size bound — accepted (operator-amended repair)
- F31/blocking/build-manifest schema evolution policy — accepted
- F32/blocking/external-source information model coverage — accepted
- F33/blocking/typed deferral provenance labels — accepted
- F34/blocking/epic bootstrap coverage ledger — accepted
- resolution_notes: All 16 findings accepted; F30's repair was amended by
  operator vote — no artificial size gate: the <100 KB acceptances were
  replaced by a configurable extreme ceiling in the gcore config store
  (`wiki.code_wiki.build_manifest_max_bytes`, default 100 MB), with ~100 KB
  kept as a non-normative expectation (F30; 2.1.2, 7.1.6). Engine assembly
  moved out of 2.1 into new leaf 6.2, sequenced after every P2–P6 producer,
  owning `generate_code_wiki`, the side-effect-free global preflights
  (manifest load, `FACTS_VERSION` with writer/cache/AI spies), and the
  composed integration test (F19, F26; 3.5.3 narrowed to local defense). 2.1's
  publication protocol gained reader atomicity via a shared generation lock
  with a concurrent-reader acceptance (F20), an `ai_outcomes` per-artifact
  state in the typed schema (F22), and the pre-0.5 schema-evolution policy —
  unsupported or unparseable prior manifest ⇒ documented full-identity cold
  rebuild, no partial read (F31). 2.2 gained the immutable
  `GenerationCapabilities` selector input restoring purity, typed
  degraded/repair outcomes covering non-page artifacts (F22), and
  artifact-specific cache identity: semantic hashes per artifact class,
  citation re-anchoring never keyed, the validated manifest-entry digest in
  `inputs_hash` and every cache key, exact naming invalidators (F23). 2.3
  defines the validated `FrontmatterInput` with fail-before-staging semantics
  (F24). New leaf 2.5 delivers the external-source information model
  (source/segment identity, unified citation attribution, retrieval
  addressability, deletion semantics) owed by epic #19664 and explicitly left
  to it by #19671, while operational ingestion stays deferred (F32).
  Dependency edges added: 3.5→3.4, 4.3→4.2, 5.1→4.3 (F25). 5.1 now targets
  the real GraphExport owners — `graph/mod.rs` (with its mandatory same-leaf
  decomposition), `graph/export.rs`, `graph/analytics.rs` (F28) — and rewires
  the live `gwiki graph` command to re-export the committed projection under
  the publication lock, with #18869 adapting in its own task against a
  committed schema fixture (F29). 5.2's rationale renders from typed
  tagged-comment spans carried by the FactsBundle (D6 and Constraints
  amended); a test proves no renderer source/index access (F27). 1.2
  registers `templates` as a protected bundled content type with `skip_types`
  routing and tamper tests (F21). The six deferral targets received
  `deferred-from:wiki-output-design:D1`–`D6` provenance labels (F33), and the
  bootstrap coverage ledger ships at
  `.gobby/plans/wiki-output-design.coverage-ledger.yaml` (F34).

```json plan-review-round
{"evidence_id":"bb4120bc-7777-4b30-b818-34153da65670","plan_hash":"58f113466fc11ee3b50555bf0608d2f1ea868c7f6b52cfccf55a28a2b087bbd8","round_number":2,"round_result":{"coverage_attestation":{"adjacent_variant_complete":true,"attestation_digest":"948c75b07e06a7423d59f7fdc513dc3c11fa0d22f8a27ae98e86676548caae45","cross_lane_interaction_complete":true,"disposition_counts":{"dismissed":6,"emitted_findings":16,"total":22},"evidence_id":"bb4120bc-7777-4b30-b818-34153da65670","lanes":[{"candidate_count":8,"lane_id":"requirements_traceability","status":"completed"},{"candidate_count":3,"lane_id":"repository_blast_radius","status":"completed"},{"candidate_count":11,"lane_id":"runtime_invariants","status":"completed"}],"shadow_manifest_status":{"entry_count":19,"manifest_digest":"2325739fab55f013999396ed267435b528c332b93069ba21c5efd1ec7ff05f72","status":"valid"},"source_digest":"354f200639fdad04c23ce1adf74df393649f3f5e57cb5341d51bf3917f4ccbe6","version":1},"findings":[{"category":"bad-sequencing","causal_finding_id":"F3","causal_section_ids":["2.1"],"check_key":"engine-composition-dependency-cycle","description":"Section 2.1 must close generate_code_wiki against 2.2, 2.3, 2.4, P3, P4, and P5 even though 2.2 and 2.4 depend on 2.1. The derived leaf therefore cannot satisfy 2.1.5 without implementing downstream work early or creating a cycle.","finding_id":"F19","fix":"Keep identity, build-manifest, publication primitives, and module registration in 2.1. Add a late code deliverable after all P2–P6 producers that owns engine.rs, generate_code_wiki, global preflights, and the composed integration test; make 7.1 depend on it.","introduced_in_round":1,"location":"P2 / § 2.1 engine foundation","prevention":"For each composition entrypoint, trace every callee to an earlier leaf and reject cycles before manifest derivation.","principle":"A composition leaf must run after every component it composes.","root_cause":"The F3 repair put the complete public engine and end-to-end entrypoint acceptance in the P2 foundation that several composed leaves depend on.","section_id":"2.1","severity":"blocking"},{"category":"unhandled-edge","causal_finding_id":"F5","causal_section_ids":["2.1"],"check_key":"publication-reader-commit-boundary","description":"Pages and JSON files are replaced individually, build.json becomes the publication point, and catalog regeneration then writes several files separately. A reader can see replacement-era pages with old metadata or a committed manifest with old/partial catalog files, contradicting 2.1.6 and “readers never observe mixed-generation state.”","finding_id":"F20","fix":"Require all retrieval readers to take a shared generation lock while the existing writer lock is held through staged catalog completion, or introduce one atomic generation switch covering every reader-visible artifact. Add concurrent-reader interruption tests, including the interval before recovery.","introduced_in_round":1,"location":"P2 / § 2.1 publication protocol","prevention":"At every publication stage, test a concurrent reader before recovery and require it to observe one generation identifier across pages, projections, catalog, and manifest.","principle":"Every reader-visible artifact in one generation needs one observable commit boundary.","root_cause":"The F5 repair commits build.json before catalog refresh and adapts a journal that recovers per-file swaps but does not make concurrent multi-file reads atomic.","section_id":"2.1","severity":"blocking"},{"category":"traceability","causal_finding_id":"F4","causal_section_ids":["1.2"],"check_key":"bundled-template-integrity-routing","description":"A modified shared/templates/codewiki/manifest.yaml can be detected as dirty yet map to no blocked content type, allowing the new file-install step to copy tampered content in production mode.","finding_id":"F21","fix":"Add integrity.py and sync routing targets; register templates as a protected bundled type, map shared/templates paths to it, make the copier honor skip_types, and add clean/modified/untracked installer tests.","introduced_in_round":1,"location":"P1 / § 1.2 bundled template installation","prevention":"For every new install/shared subtree, inventory manifest hashing, dirty-path classification, CLI type routing, skip handling, and tamper tests.","principle":"A new bundled install side effect must participate in the existing integrity and skip routing.","root_cause":"The F4 repair added a template copier to sync_bundled_content_to_db, but current integrity mapping has no templates content type and the copier has no protected skip_types route.","section_id":"1.2","severity":"blocking"},{"category":"unhandled-edge","causal_finding_id":"F7","causal_section_ids":["2.2"],"check_key":"ai-capability-repair-state","description":"Identical manifest/change-set inputs must return both SKIP when AI is unavailable and repair work when AI becomes available. Provisional layer names, tour steps, and insight questions also lack a common degraded/repair state, so an unchanged later run can neither select nor prove their repair.","finding_id":"F22","fix":"Add an immutable GenerationCapabilities input and a typed per-artifact AI outcome/state covering layer names, prose, tours, and insight questions. Define publishable degradation versus fatal error and test unavailable→available, repeated failure, and successful repair.","introduced_in_round":1,"location":"P2–P5 / §§ 2.2, 4.1–5.2","prevention":"For every AI artifact, enumerate unavailable, timeout, malformed, repeated-failure, cache-hit, verified-success, and repair transitions with all selector inputs explicit.","principle":"A pure regeneration selector must receive every state transition that can change its result.","root_cause":"The F7 repair makes degraded reselection depend on AI availability while declaring decisions pure in only manifest and change set, and it provides no uniform persisted repair state for non-page AI artifacts.","section_id":"2.2","severity":"blocking"},{"category":"unhandled-edge","check_key":"artifact-specific-cache-digests","description":"COSMETIC comment/doc edits require citation refresh with zero AI regeneration, while 2.2 says any source-content change misses the AI key. Section 4.1 says names change only with membership, while prompt/model-lane changes invalidate the same key. Behavior-bearing manifest edits can also change output without invalidation when versions are unchanged.","finding_id":"F23","fix":"Define artifact-specific semantic hashes separately from citation-position digests; include a canonical validated manifest-entry digest in inputs_hash/cache identity; and state exact naming invalidators. Add zero-AI citation re-anchoring plus prompt, model, and manifest-field tests.","location":"P2–P4 / §§ 1.2, 2.2, 4.1","prevention":"For each AI artifact, list semantic inputs, positional inputs, manifest behavior inputs, and version inputs, then test every included and excluded digest independently.","principle":"Cache identity and invalidation policy must use the same artifact-specific semantic inputs.","root_cause":"One global exact-key rule conflates source bytes, citation positions, cluster membership, and behavior-bearing page-manifest fields.","section_id":"2.2","severity":"blocking"},{"category":"traceability","causal_finding_id":"F10","causal_section_ids":["2.3","4.2"],"check_key":"frontmatter-builder-input-completeness","description":"The builder cannot derive per-page title, summary, provenance line ranges, trust, freshness, degraded_sources, or layer from the declared manifest-entry fields plus a BuildRecord containing only source file paths and generation metadata. Acceptance 2.3.3 is therefore not implementable as written.","finding_id":"F24","fix":"Define a validated FrontmatterInput containing all page-specific values and provenance spans alongside manifest/build metadata. Missing required inputs must fail before staging; test every page type and degraded output.","introduced_in_round":1,"location":"P2 / § 2.3 shared frontmatter builder","prevention":"Map every required frontmatter key to one typed input field and one producer before assigning shared-builder ownership.","principle":"A shared builder must receive every value required by the schema it owns.","root_cause":"The F10 repair restricts builder inputs to a typed manifest entry and BuildRecord, but those declared shapes omit required page-specific fields and provenance spans.","section_id":"2.3","severity":"blocking"},{"category":"bad-sequencing","check_key":"cold-run-producer-dependencies","description":"Section 3.5 requires projection catalog entries but can run before 3.4; 4.3 requires resolvable concept links but is a sibling of 4.2; and 5.1 builds page nodes from the manifest but can run before tour records from 4.3.","finding_id":"F25","fix":"Make 3.5 depend on 3.4, 4.3 depend on 4.2, and 5.1 depend on 4.3. Retain the existing transitive P3 and concept prerequisites.","location":"P3–P5 / §§ 3.5, 4.3, 5.1","prevention":"Trace every acceptance noun to its producing leaf and verify a transitive dependency before shadow-manifest approval.","principle":"A leaf must depend on every producer needed to make its acceptance resolvable on a cold run.","root_cause":"Three consumers cite sibling outputs without dependency edges.","section_id":"4.3","severity":"blocking"},{"category":"bad-sequencing","check_key":"facts-version-entrypoint-preflight","description":"Acceptance 3.5.3 cannot prove the global constraint that an unsupported FACTS_VERSION is rejected before any page is written because generate_code_wiki may perform invalidation, clustering, and earlier rendering first.","finding_id":"F26","fix":"Move FACTS_VERSION validation and its acceptance to the late engine assembly preflight. Use writer, cache, and AI spies to prove zero side effects; keep the projection check only as local defense.","location":"P2/P3 / §§ 2.1 and 3.5","prevention":"Place all whole-generation input-version gates before publication preparation, cache access, renderer dispatch, and AI calls, with side-effect spies.","principle":"A fail-before-any-write invariant must be owned and tested at the outermost side-effect boundary.","root_cause":"The only negative acceptance is projection-local even though earlier entrypoint stages can stage files, access caches, or invoke AI.","section_id":"3.5","severity":"blocking"},{"category":"traceability","check_key":"deterministic-renderer-source-boundary","description":"The design-rationale renderer is specified as graph projection plus source scanning, which contradicts the constraint that deterministic render code receives only FactsBundle and never accesses the DB or index.","finding_id":"F27","fix":"Extend D6/FactsBundle with typed tagged-comment spans and digests, or perform scanning in engine input assembly and pass those typed facts to 5.2. Add a test proving no direct source/index access.","location":"P5 / § 5.2 insight report","prevention":"For every deterministic renderer, inventory file, DB, index, network, and clock reads and require all facts to enter through the declared bundle.","principle":"Deterministic renderers must consume the one declared immutable input contract.","root_cause":"Section 5.2 directly scans indexed sources despite the plan-wide FactsBundle-only renderer boundary.","section_id":"5.2","severity":"blocking"},{"category":"traceability","causal_finding_id":"F16","causal_section_ids":["5.1"],"check_key":"graph-schema-actual-owner-coverage","description":"GraphExport, GraphExportNode, GraphExportEdges, and GraphExportEdge are defined in crates/gwiki/src/graph/mod.rs; export_graph constructs them in graph/export.rs and analytics derives from them in graph/analytics.rs. None is targeted, so the extended schema cannot compile, and touching the 1,177-line mod.rs triggers same-leaf decomposition.","finding_id":"F28","fix":"Add graph/mod.rs, graph/export.rs, graph/analytics.rs, and focused tests as exact targets. Decompose graph/mod.rs below 1,000 lines in this leaf and migrate every constructor, conversion, serializer, and exhaustive consumer.","introduced_in_round":1,"location":"P5 / § 5.1 graph projection","prevention":"Resolve exact type definitions, constructors, destructures, analytics, serializers, and line counts before finalizing Targets.","principle":"Schema migrations must target the files that define and construct the schema, including mandatory decomposition.","root_cause":"The F16 repair assigns GraphExport schema work to exports/graph.rs, while actual types and node/edge constructors live elsewhere and graph/mod.rs is already over 1,000 lines.","section_id":"5.1","severity":"blocking"},{"category":"unhandled-edge","causal_finding_id":"F17","causal_section_ids":["5.1"],"check_key":"graph-live-producer-and-consumer-handoff","description":"crates/gwiki/src/commands/graph.rs still rebuilds outputs/graph.json from WikiGraphFacts through the export batch, while the new engine would build it from BuildManifest, FactsBundle, and catalog. These independent snapshots can overwrite each other. The same section says the #18869 web reader migrates here despite having no web targets and being a later consumer task.","finding_id":"F29","fix":"Target commands/graph.rs and make it read/export the committed projection or use a distinct diagnostic output; route every live-path write through the publication lock/journal. Keep web adaptation under #18869, remove the in-leaf migration claim, and publish a schema fixture for that task.","introduced_in_round":1,"location":"P5 / § 5.1 outputs/graph.json","prevention":"Inventory each live-path writer separately from shared serializers, then assign every current and future consumer to exactly one owning leaf/task.","principle":"One persisted path needs one semantic producer and an explicit migration boundary for every live consumer.","root_cause":"The F17 repair selected the existing serializer but did not retire or rewire the current graph command producer, and it simultaneously claims a web migration owned by future #18869.","section_id":"5.1","severity":"blocking"},{"category":"weak-testability","check_key":"build-manifest-size-bound-real-corpus","description":"The tracked Gobby production-source inventory contains 2,291 paths totaling 90,845 raw path bytes before JSON quoting, per-record metadata, cross-page overlap, or rename history. The required <100 KB Gobby end-to-end acceptance cannot be established with the declared source_files arrays and unbounded lineage.","finding_id":"F30","fix":"Use a deduplicated source table with source-set IDs, define bounded/compacted lineage retention, and make 2.1.2/7.1.6 size-test the real Gobby path inventory plus worst-case overlap and repeated renames.","location":"P2/P7 / §§ 2.1 and 7.1","prevention":"Estimate serialized worst-case bytes from the required corpus, overlap, and retained history before setting a hard size gate.","principle":"A size acceptance must be feasible for the largest required input shape, including retained history.","root_cause":"The schema repeats uncompressed source paths per page and leaves rename_lineage unbounded, while the test fixture is described only by page count.","section_id":"2.1","severity":"blocking"},{"category":"missing-requirement","check_key":"build-manifest-schema-evolution-policy","description":"No acceptance defines what happens when an existing code/_meta/build.json has an unsupported schema_version, including whether identity is reset, old artifacts are preserved, or generation fails before writes.","finding_id":"F31","fix":"Under the pre-0.5 policy, define fail-before-write rejection or one explicit clean/full identity-reset path, including orphan/publication semantics, and add focused plus end-to-end tests.","location":"P1/P2/P7 / §§ 1.1, 2.1, 2.2, 7.1","prevention":"For every persisted schema_version, test current, older/unsupported, malformed, and partially written inputs at the pre-write boundary.","principle":"Persisted schema versions need an explicit read, migration, or rejection policy.","root_cause":"The plan promises schema-evolution and migration behavior but only tests current-schema round-trip and classifies a schema change as FULL.","section_id":"2.1","severity":"blocking"},{"category":"missing-requirement","check_key":"external-source-information-model-coverage","description":"The governing epic requires stable identities for external sources and extracted segments, unified repository/external attribution, deletion semantics, and a common retrieval/generated-output model. #19671 explicitly leaves that information model under #19664, yet this plan has no implementation or acceptance for it.","finding_id":"F32","fix":"Add a deliverable defining and testing external-source, segment, generated-artifact, citation, attribution, retrieval, and deletion semantics. Keep extraction execution, retries, and daemon/UI ingestion in #19671.","location":"P1–P6 / governing epic #19664","prevention":"Map each governing task noun—identity, relationship, attribution, retrieval, deletion, migration—to a deliverable or typed deferral before approval.","principle":"Every governing epic requirement needs an owned deliverable or a valid deferral that preserves ownership.","root_cause":"This plan implements repository-generated pages but leaves the #19664-owned external-source information model to no leaf; D1 defers operational ingestion only.","section_id":"1.1","severity":"blocking"},{"category":"gobby-format","check_key":"typed-deferral-provenance-labels","description":"The six deferral objects are syntactically complete, but their target tasks do not carry the provenance labels required by the Plan-Coverage Contract, so the deferrals are not valid handoffs.","finding_id":"F33","fix":"Add the exact deferred-from:wiki-output-design:D1, D2, D3, D4, D5, and D6 labels to the corresponding referenced tasks before the next review round.","location":"Deferred §§ D1–D6","prevention":"Before approval, read every deferral target and verify open state plus the exact deferred-from:<plan-id>:<section-id> label.","principle":"Typed deferral targets must carry section-qualified provenance labels.","root_cause":"All six referenced tasks are open but lack deferred-from:wiki-output-design:D1 through D6.","section_id":"D1","severity":"blocking"},{"category":"gobby-format","check_key":"epic-bootstrap-coverage-ledger","description":"This 19-deliverable, 57-acceptance epic plan has no companion ledger, so expected implementation leaves cannot be checked against expansion as required by docs/contracts/plan-coverage.md.","finding_id":"F34","fix":"Create .gobby/plans/wiki-output-design.coverage-ledger.yaml covering every deliverable and acceptance item with expected leaves, then include it in the next review handoff.","location":"Plan-Coverage bootstrap ledger","prevention":"Check the canonical companion path and acceptance/expected-leaf parity for every new epic plan.","principle":"Every new epic plan must ship its canonical reviewed coverage ledger before expansion.","root_cause":"The required .gobby/plans/wiki-output-design.coverage-ledger.yaml companion does not exist.","section_id":"1.1","severity":"blocking"}],"reviewer_session":"#10265","round":2,"verdict":"needs_review"},"session_id":"b8f54985-47f1-493d-a08a-8b37e5211bd7"}
```

**Round 1** `kind: verification`

- reviewer_run: 90d1005f-3835-404b-be83-4af587ad631a
- reviewer_session: #10257 (efafe4d5-6596-408b-88af-87051013fb11, codex/gpt-5.6-sol)
- verdict: needs_review
- findings:
- F1/blocking/rename-stable page identity — accepted
- F2/blocking/manifest-runtime producer parity — declined
- F3/blocking/Rust module registration and engine entrypoint — accepted
- F4/blocking/bundled template inventory and install path — accepted
- F5/blocking/multi-artifact publication transaction — accepted
- F6/blocking/build-manifest schema determinism — accepted
- F7/blocking/degraded-output repair trigger — accepted
- F8/blocking/artifact-input invalidation matrix — accepted
- F9/blocking/renderer-producer dataflow order — accepted
- F10/blocking/all-pages summary ownership — accepted
- F11/blocking/search summary consumer target — accepted
- F12/blocking/session-injection artifact seam — accepted
- F13/blocking/catalog.rs monolith ceiling — accepted
- F14/blocking/verification section had no expansion owner — accepted
- F15/blocking/module diagram assembly owner — accepted
- F16/blocking/graph projection consumer schema — accepted
- F17/blocking/outputs/graph.json single owner — accepted
- F18/blocking/installed navigation taxonomy migration — accepted
- resolution_notes: 17 findings accepted and applied; F2 declined as stale —
  its claims (no manifest loader, six projections unproduced) are contradicted
  by the reviewed artifact's 2.3 and 3.5, added in enhancement round 1, with
  parity covered by 2.1.3 orphan detection and 7.1.3. Repairs applied: 2.1
  gained an immutable `page_id` with rename lineage (F1), module
  registration/`lib.rs` targets and the public `generate_code_wiki` entrypoint
  with adapter wiring deferred to #19665 per session #10262's #19668 boundary
  ruling (F3), a transactional publication protocol adapted from legacy
  `CodewikiPublication` semantics without importing post-move legacy modules
  (F5), and a complete deterministic manifest schema with `layer_names`,
  `tours`, `rename_lineage`, content-derived `generation_id` (F6). 2.2 gained
  degraded-repair reselection (F7) and the artifact-by-input invalidation
  matrix incl. tagged comments and knowledge digests (F8). 2.4 was split out
  for deterministic clustering membership, 4.1 narrowed to naming, 3.2/3.3/5.3
  dependencies rewired with cold/warm naming semantics (F9). The shared
  frontmatter builder moved to 2.3 with 4.2 acceptance narrowed (F10). 6.1
  gained `commands/search.rs` targets with summary-first snippets (F11) and a
  root-index → `load_wiki_overview` integration test, with 3.1's misattributed
  injection claim corrected (F12). 3.4 owns the same-task `catalog.rs`
  decomposition into `catalog/` modules (F13, confirmed unclaimed by #19668
  planning) and the `AI_README_TEMPLATE` taxonomy rewrite (F18). E1 became the
  owned P7/7.1 test deliverable with numbered acceptance (F14). 3.3 assembles
  module diagrams via the 5.3 primitive so no empty slot publishes (F15,
  assembly assigned to the scaffold rather than 5.3 — deterministic facts make
  scaffold-time assembly the least mechanism). 5.1 extends the existing
  `exports/graph.rs` owner of `outputs/graph.json` with symbol nodes, evidence
  spans, and a 4.2 dependency, migrating consumers together (F16, F17,
  ownership confirmed ours by #10262).

```json plan-review-round
{"evidence_id":"427406d5-15da-4dde-a1c0-7239684dc008","plan_hash":"5965fc752b38627dc2797c739c943912206479c41369e36d44740382730f5afa","round_number":1,"round_result":{"coverage_attestation":{"adjacent_variant_complete":true,"attestation_digest":"2325a7b1ceba5007d6dba8839b85ed6dabcd6c1e76dab8acaaa9d311cd79ec95","cross_lane_interaction_complete":true,"disposition_counts":{"dismissed":6,"emitted_findings":18,"total":24},"evidence_id":"427406d5-15da-4dde-a1c0-7239684dc008","lanes":[{"candidate_count":11,"lane_id":"requirements_traceability","status":"completed"},{"candidate_count":6,"lane_id":"repository_blast_radius","status":"completed"},{"candidate_count":7,"lane_id":"runtime_invariants","status":"completed"}],"shadow_manifest_status":{"entry_count":15,"manifest_digest":"f6051184676582739df7805e19769075ca3c72bb63e9de7dc7fc98ac931f3395","status":"valid"},"source_digest":"6076ea871eea7da52e3b9a4fb7863f7a358c8357901545eb00c5b26a1062fe18","version":1},"findings":[{"category":"unhandled-edge","check_key":"rename-stable-page-identity","description":"A slug-changing rename necessarily becomes delete-plus-create under the declared identity key, so build records, tour metadata, and graph node references cannot preserve the page identity required by the plan.","finding_id":"F1","fix":"Define an immutable page_id separate from slug/path, persist rename lineage or identity mapping in build.json, and add acceptance proving one rename preserves page_id while updating paths, wikilinks, tours, and graph references.","location":"P2 / § 2.1 Stable identities and build manifest","prevention":"For every rename flow, verify identity, path, inbound links, manifest references, graph references, and tour references separately.","principle":"Stable identity must be independent of mutable display names and paths.","root_cause":"The plan keys page identity by (page_type, slug) while requiring identity to survive slug-changing renames.","section_id":"2.1","severity":"blocking"},{"category":"missing-requirement","check_key":"manifest-runtime-producer-parity","description":"The plan declares manifest.yaml as renderer authority and enumerates features, deprecations, changes, hotspots, ownership, infrastructure, and layers, yet no leaf loads that authority and six projection IDs have no new-engine producer or integration acceptance.","finding_id":"F2","fix":"Add a manifest-driven engine deliverable with a typed loader and validated dispatcher, port or invoke producers for all six missing projections, and test exact manifest-ID/output-path parity, invalid entries, and orphan rejection.","location":"P1 / § 1.2 Page-type manifest","prevention":"Maintain a manifest-ID matrix mapping every entry to loader validation, renderer target, output path, invalidation class, and acceptance test.","principle":"Every declared manifest entry needs a validated runtime consumer and an owned output producer.","root_cause":"Section 1.2 creates a manifest artifact without a typed loader/dispatcher, while the implementation leaves produce only the layers projection among seven declared projections.","section_id":"1.2","severity":"blocking"},{"category":"missing-requirement","check_key":"rust-module-entrypoint-registration","description":"Files under code_wiki/ and code_wiki/render/ will not compile into gwiki without module declarations, and the planned integration tests have no public engine entrypoint to invoke.","finding_id":"F3","fix":"Add exact targets for code_wiki/mod.rs, code_wiki/render/mod.rs, and lib.rs; define one public engine function composing manifest load, invalidation, rendering, projections, and publication. Keep the deferred daemon/CLI adapter boundary explicit.","location":"P2 / § 2.1 Engine foundation","prevention":"For every new Rust subtree, inventory mod.rs declarations, crate exports, composition entrypoints, and integration-test reachability.","principle":"New Rust modules must be declared, exported at the required visibility, and reachable through an owned composition entrypoint.","root_cause":"The plan lists new code_wiki source files without module roots, lib registration, or a callable full-engine function.","section_id":"2.1","severity":"blocking"},{"category":"traceability","check_key":"bundled-template-inventory","description":"Adding the template alone will fail the repository's bundled-content manifest equality test, and the current installer does not establish how gwiki finds a templates directory.","finding_id":"F4","fix":"Add bundled_content_manifest.json and its regeneration/test to 1.2, and specify the packaged resource or installed-path lookup used by the manifest loader.","location":"P1 / § 1.2 Page-type manifest","prevention":"For every install/shared target, check package data, bundled inventory, installer copy rules, runtime lookup, and tree-equality tests.","principle":"Changes under bundled-content roots must update their committed inventory and installation/load path.","root_cause":"Section 1.2 adds manifest.yaml under install/shared but omits bundled_content_manifest.json and ownership for making the template available to gwiki.","section_id":"1.2","severity":"blocking"},{"category":"unhandled-edge","check_key":"multi-artifact-publication-transaction","description":"A crash or concurrent reader can observe pages, graph/layer JSON, catalog links, and build metadata from different generations even though build.json itself is atomically renamed.","finding_id":"F5","fix":"Absorb the existing staged/journaled CodewikiPublication mechanics after the #19668 move, validate all staged artifacts, journal replacement and pruning, commit build.json last, define catalog lock order, and add injected-interruption recovery tests.","location":"P2 / § 2.1 Build manifest and publication","prevention":"Walk crash points before staging, during replacement, during pruning, before metadata commit, and during catalog refresh; require recovery tests for each.","principle":"A generation is publishable only when all reader-visible artifacts and ownership metadata cross one recoverable commit boundary.","root_cause":"The plan makes build.json atomic but leaves page replacement, JSON outputs, orphan pruning, and catalog refresh outside a defined publication protocol.","section_id":"2.1","severity":"blocking"},{"category":"unhandled-edge","check_key":"build-manifest-schema-determinism","description":"Later leaves require fields absent from the declared record shape, and volatile timestamps or run IDs can make build.json change on identical inputs despite the plan's byte-stability contract.","finding_id":"F6","fix":"Extend the typed schema for layer-name cache and tour metadata, define generation_id as content-derived or preserved for identical inputs, preserve generated_at for unchanged pages or move attempt time elsewhere, and test byte-identical build.json reruns.","location":"P2 / § 2.1 Build manifest schema","prevention":"Inventory every manifest reader and writer, then test typed round-trip and byte-identical no-op regeneration for the complete schema.","principle":"A persisted schema must declare every writer-owned field and give deterministic semantics to committed metadata.","root_cause":"The 2.1 schema omits cached layer names and tour metadata later written by 4.1/4.3, while generated_at and generation_id have no stable no-op semantics.","section_id":"2.1","severity":"blocking"},{"category":"unhandled-edge","check_key":"degraded-output-repair-trigger","description":"A transient daemon or citation-verification failure can leave degraded prose permanent because later healthy runs with unchanged indexed inputs select no regeneration.","finding_id":"F7","fix":"Union degraded=true records into the regeneration set when daemon-mediated AI is available, retain degraded on another failure, clear it only after verified output, and test failed-first then successful-second runs with unchanged inputs.","location":"P2 / § 2.2 Incremental invalidation","prevention":"For each persisted failure state, specify creation, retry eligibility, repeated failure, successful repair, and flag-clearing transitions.","principle":"Persisted degraded state needs a recovery transition independent of content changes.","root_cause":"The invalidation classifier returns SKIP for unchanged inputs and never selects build records whose degraded flag remains true.","section_id":"2.2","severity":"blocking"},{"category":"unhandled-edge","check_key":"artifact-input-invalidation-matrix","description":"PARTIAL call/import changes can alter graph edges, knowledge-only edits can alter knowledge nodes and aliases, and COSMETIC NOTE/WHY/HACK edits can alter cited design rationale, yet the declared exact regeneration sets can leave those outputs stale.","finding_id":"F8","fix":"Define the complete dependency matrix, include knowledge/catalog digests and tagged comments in classification inputs, regenerate graph and insights for every relevant class, and add PARTIAL, COSMETIC, and knowledge-only tests.","location":"P2 / § 2.2 Change classification","prevention":"Build an artifact-by-input matrix covering code facts, comments, knowledge pages, aliases, manifest state, and renderer versions; test each nonempty cell.","principle":"Every output must be invalidated by every input that can change its bytes or references.","root_cause":"The classifier considers indexed code files only and assigns graph/insight regeneration to change classes that exclude several of their real inputs.","section_id":"2.2","severity":"blocking"},{"category":"bad-sequencing","check_key":"renderer-producer-dataflow-order","description":"The landing page needs concept counts/top concepts before clustering and concept generation, while layers.md needs names from 4.1 and diagram bounding from 5.3. The derived DAG is syntactically acyclic but cannot satisfy P3 acceptance on a cold vault.","finding_id":"F9","fix":"Split deterministic clustering/membership and the bounded-diagram primitive into P2-dependent precursor leaves, define cold naming failure and warm cache-fingerprint behavior, and make landing/layers renderers explicitly depend on their producers.","location":"P3 / §§ 3.1–3.2 Rendering prerequisites","prevention":"For each rendered field and slot, trace its producer and require a forward dependency before manifest derivation.","principle":"A leaf must depend on every producer required to satisfy its acceptance criteria.","root_cause":"P3 renderers consume clustering/naming, concept statistics, and bounded-diagram behavior produced by P4/P5 leaves that themselves depend on P3.","section_id":"3.2","severity":"blocking"},{"category":"traceability","check_key":"all-pages-summary-ownership","description":"Landing, layers, tours, insights, and deterministic projection pages neither depend on 4.2 nor own their own summary generation, so acceptance 4.2.2 cannot establish the plan-wide invariant.","finding_id":"F10","fix":"Move summary/frontmatter construction into a shared prerequisite used by every renderer, add page-type coverage tests, and narrow 4.2 acceptance to pages it actually produces or move the all-pages assertion to the final integration leaf.","location":"P4 / § 4.2 Narrative and concept rendering","prevention":"Map each generated page type to its frontmatter builder and page-specific summary acceptance.","principle":"A global page invariant must be owned by a shared prerequisite or by every producing renderer.","root_cause":"Section 4.2 claims every page has summary frontmatter while it owns only module prose and concept pages and runs before several other page producers.","section_id":"4.2","severity":"blocking"},{"category":"traceability","check_key":"search-summary-consumer-target","description":"Current gwiki search strips frontmatter and builds snippets from body windows, so changes to render_overview cannot make search results prefer summary frontmatter.","finding_id":"F11","fix":"Add commands/search.rs symbols and focused search tests to 6.1; parse summary before frontmatter stripping, retain grounded body evidence, and verify summary-first plus normalized-body fallback across search backends and output formats.","location":"P6 / § 6.1 Retrieval summaries","prevention":"Resolve every acceptance noun to its current producer, formatter, API surface, and focused test before finalizing Targets.","principle":"Acceptance that changes consumer behavior must target and test the actual consumer path.","root_cause":"Section 6.1 targets the normalizer, catalog, and session hook while omitting the search implementation that constructs snippets.","section_id":"6.1","severity":"blocking"},{"category":"weak-testability","check_key":"session-injection-artifact-seam","description":"The code landing page's 2,000-character test does not prove session injection compatibility; the operative artifact is the catalog-owned root index.","finding_id":"F12","fix":"State the root _index.md ownership explicitly, move the injection budget proof to catalog/root overview acceptance, and add an integration test that renders the root index and passes it through load_wiki_overview.","location":"P6 / § 6.1 Session injection","prevention":"Trace each integration test from generated path through the exact runtime reader and parsed section.","principle":"An integration acceptance must exercise the artifact the runtime consumer actually reads.","root_cause":"The landing acceptance treats code/_index.md as the injection source, while load_wiki_overview reads the vault-root _index.md Overview.","section_id":"6.1","severity":"blocking"},{"category":"gobby-format","check_key":"touched-production-monolith-ceiling","description":"The first catalog edit triggers the mandatory decomposition rule; the plan's constraint for new files does not cover this existing threshold violation.","finding_id":"F13","fix":"Add cohesive catalog submodule decomposition to 3.4 with exact targets and acceptance that every touched production file is below 1,000 lines, then retarget 3.4 and 6.1 to the decomposed modules.","location":"P3 / § 3.4 Catalog reconciliation","prevention":"Check current and projected line counts for every production target before manifest approval.","principle":"Every touched hand-maintained production file must remain below the repository's 1,000-line ceiling within the same leaf.","root_cause":"Sections 3.4 and 6.1 target the existing 1,317-line catalog.rs without assigning decomposition ownership.","section_id":"3.4","severity":"blocking"},{"category":"traceability","check_key":"verification-section-expansion-owner","description":"Full-engine generation, page counts, anatomy lint, production-vault checksums, invalidation, and retrieval checks exist only in E1, so the 15-entry manifest creates no task that executes or closes them.","finding_id":"F14","fix":"Convert E1 into a standalone category:test deliverable dependent on all implementation leaves with exact integration targets and numbered acceptance, or distribute every check to named owners and retain one final integration deliverable.","location":"End-to-end acceptance after P6","prevention":"Compare all acceptance-bearing non-deliverable sections against the derived manifest and assign each check to one emitted leaf.","principle":"Every required acceptance check needs an expansion leaf that owns its implementation and closure.","root_cause":"E1 is kind: verification, while manifest synthesis emits entries only for deliverable sections.","section_id":"6.1","severity":"blocking"},{"category":"traceability","check_key":"module-diagram-materialization-owner","description":"No leaf guarantees that every module page receives a valid diagram or labeled truncation even though end-to-end acceptance requires it.","finding_id":"F15","fix":"Assign module-diagram assembly to 5.3 or a dedicated integration leaf, target the module scaffold/assembly symbols, and test across the generated corpus that no diagram slot remains empty and every emitted Mermaid block validates.","location":"P5 / § 5.3 Diagram fallback","prevention":"For every scaffold slot, identify producer, assembler, fallback, and a test proving no unresolved slot reaches publication.","principle":"A scaffold slot and a rendering primitive need an explicit assembly owner and corpus-level acceptance.","root_cause":"Section 3.3 emits a diagram slot, 4.2 fills prose slots, and 5.3 builds diagram primitives without assigning module-page integration.","section_id":"5.3","severity":"blocking"},{"category":"missing-requirement","check_key":"graph-projection-consumer-schema","description":"Section 5.2 requests module and symbol god nodes plus cited surprising connections, while 5.1 declares no symbol node kind, serializes edges without source spans, and lacks a dependency on 4.2 concept production.","finding_id":"F16","fix":"Add symbol nodes/required edges and deterministic evidence spans, define inferred-edge grounding rules, make 5.1 depend on concept production, and validate that insights can render every required cited section from graph.json alone or explicitly target its additional gcode inputs.","location":"P5 / §§ 5.1–5.2 Graph projection and insights","prevention":"Derive producer schemas and dependencies from every downstream query, then round-trip representative consumer fixtures.","principle":"A producer schema must preserve every node kind, dependency, and evidence field required by downstream consumers.","root_cause":"The projection omits symbol nodes and edge evidence, and it can run before concept pages exist even though insights consumes symbols, citations, and concept nodes.","section_id":"5.1","severity":"blocking"},{"category":"traceability","check_key":"graph-output-single-owner","description":"gwiki already writes and serves outputs/graph.json using GraphExport's node and edge-bucket schema; the proposed flat src/dst/type/confidence projection would collide with that owner and be discarded by current consumers.","finding_id":"F17","fix":"Choose one owner: extend the existing graph export pipeline and migrate its Rust/Python/web consumers and tests together, or use a distinct projection path and explicitly map #18869 to that contract.","location":"P5 / § 5.1 Graph projection","prevention":"Inventory all writers and readers before assigning an existing output path to a new artifact.","principle":"One persisted path must have one schema owner, with all readers migrated atomically when that schema changes.","root_cause":"Section 5.1 proposes a second incompatible writer for outputs/graph.json without targeting the existing exporter, gateway, route, or web normalizer.","section_id":"5.1","severity":"blocking"},{"category":"missing-requirement","check_key":"installed-navigation-taxonomy-migration","description":"New and restored vaults would direct agents to legacy entry points and nonexistent file pages after the new output taxonomy lands.","finding_id":"F18","fix":"Add vault.rs AI_README_TEMPLATE and restoration tests to the catalog/navigation work, document the Landing/Layers/Modules/Concepts/Tours/Insights/Projections entrypoints, and preserve existing user-edited guides.","location":"P3 / § 3.4 Catalog reconciliation","prevention":"For taxonomy changes, inventory generated indexes, default vault files, restoration templates, session injection, and UI entrypoints.","principle":"Changing navigation taxonomy requires updating every installed and restored orientation artifact.","root_cause":"The plan updates catalog pages but omits AI_README_TEMPLATE, which still advertises handbook, concepts, modules, and persistent file pages.","section_id":"3.4","severity":"blocking"}],"reviewer_session":"#10257","round":1,"round_number":1,"verdict":"needs_review"},"session_id":"b8f54985-47f1-493d-a08a-8b37e5211bd7"}
```

**Human handoff** `kind: verification`

Round 1 returned `needs_review` at the configured review cap (1); the operator
extended the review, and rounds continue until convergence (a round with zero
accepted repairs). All 18 findings were voted individually: 17 accepted and
applied as the repairs recorded above; F2 declined as stale against the
reviewed artifact. The repaired plan base-validates with 7 phases. Continuation
is human-directed: proceed via the explicit handoff tools
(`derive_plan_handoff_manifest` → `apply_plan_handoff_manifest` →
`uv run gobby build`) or stop with the canonical artifact in place.

### Enhancement round 1 (2026-08-06)
`kind: framing`

Enhancement round — agent: `plan-enhancer-taskless` (codex/gpt-5.6-sol), run
`c8fc0343-cb66-437d-8d04-1a8b94937d43`, evidence
`427406d5-15da-4dde-a1c0-7239684dc008`, round 1 of 1, converged: false.
Suggestions E1–E4 all accepted by user vote ("accept all"):

- **E1 accepted** — six missing deterministic projections had no deliverable.
  Added 3.5 (`render/projections.rs`: `features`, `deprecations`, `changes`,
  `hotspots`, `ownership`, `infrastructure`) and an E1 end-to-end assertion that
  all seven projection paths exist.
- **E2 accepted** — renderer input boundary made explicit. Added the Constraints
  bullet pinning `gobby_core::code_facts::FactsBundle` as the sole deterministic
  renderer input with golden consumer fixture and pre-write `FACTS_VERSION`
  rejection (also 3.5.3).
- **E3 accepted** — manifest authority needed a gate. Added 2.3 typed page-type
  manifest loader/validator with fail-before-write semantics and negative tests.
- **E4 accepted** — AI cache identity was implicit. Defined `ai_cache_key`
  (page id + sorted source/fact content hashes + prompt/schema version +
  render/template version + model-lane version) in 2.2, persisted it in the 2.1
  build record, and added acceptance 2.2.3.

### User revision 2 (2026-08-06)
`kind: framing`

Daemon integration is required for the broad epic #19670, so the user directed
trimming the AI-lane frontmatter keys. `ai_route` (collapses to a constant),
`ai_fallback` (no fallback lane exists), and `ai_generation_status` (derivable
from manifest `ai_permitted` + `degraded`) are dropped from the new engine's
frontmatter contract; `trust`, `freshness`, `degraded`, `degraded_sources`
retained. Output Design anatomy and 1.1 frontmatter schema updated; exemplars
trimmed to match.

### User revision 1 (2026-08-06)
`kind: framing`

User directed that the plan pin concrete exemplar outputs to build against.
Added normative per-class exemplars under `docs/contracts/wiki-output/examples/`
to 1.1 (new acceptance 1.1.3) with the `GOLDEN_PAGE` pattern scaled to page
level; P3–P5 renderer tests structure-diff against them (3.3.1 amended).
Drafting the exemplars settled two contract points: the pinned contract's `type`
key carries the page-type id (no duplicate `page_type` key; Output Design, 1.1,
and 3.3 aligned), and capped tables render bounded top-N with explicit
truncation labels and a gcode pointer (module files table cap set at 12).

## Task Mapping
`kind: framing`

Expanded 2026-08-07 by expansion run `75b22462-23ea-40e2-8da8-55699e854b44`
under epic #19664: 7 phase parents, 21 leaves, 83 dependency edges. Every leaf
carries its `covers:wiki-output-design:<section>:<item>` labels; the 18 code
leaves carry `tdd:required` and route to `implementation_domain: backend`.

| Plan Item | Task Ref | Status |
|-----------|----------|--------|
| P1 | #19766 | open |
| 1.1 | #19773 | open |
| 1.2 | #19774 | open |
| P2 | #19767 | open |
| 2.1 | #19775 | open |
| 2.2 | #19776 | open |
| 2.3 | #19777 | open |
| 2.4 | #19778 | open |
| 2.5 | #19779 | open |
| P3 | #19768 | open |
| 3.1 | #19780 | open |
| 3.2 | #19781 | open |
| 3.3 | #19782 | open |
| 3.4 | #19783 | open |
| 3.5 | #19784 | open |
| P4 | #19769 | open |
| 4.1 | #19785 | open |
| 4.2 | #19786 | open |
| 4.3 | #19787 | open |
| P5 | #19770 | open |
| 5.1 | #19788 | open |
| 5.2 | #19789 | open |
| 5.3 | #19790 | open |
| P6 | #19771 | open |
| 6.1 | #19791 | open |
| 6.2 | #19792 | open |
| P7 | #19772 | open |
| 7.1 | #19793 | open |
| D1 | #19671 | deferred |
| D2 | #19665 | deferred |
| D3 | #19672 | deferred |
| D4 | #18779 | deferred |
| D5 | #18902 | deferred |
| D6 | #17678 | deferred |

## M1 Task Manifest
`kind: manifest`

```yaml
- title: Author the wiki output contract
  category: docs
  task_type: feature
  depends_on: []
  validation_criteria: '1.1.1: Output contract exists and covers taxonomy, anatomy,
    frontmatter, citations, namespaces, diagrams, identity/invalidation/manifest semantics,
    and trust signals. file: `docs/contracts/wiki-output.md`.

    1.1.2: Superseded banner present with pointers to this plan and the contract.
    file: `.gobby/plans/wiki-codewiki-restructure.md`.

    1.1.3: Normative exemplars exist for all seven listed page classes with contract
    frontmatter, full section anatomy, and truncation-label examples. file: `docs/contracts/wiki-output/examples/module.md`.'
  labels:
  - covers:wiki-output-design:1.1:1.1.1
  - covers:wiki-output-design:1.1:1.1.2
  - covers:wiki-output-design:1.1:1.1.3
  tdd: false
  source_section: '1.1'
  assigned_agent: tech-writer
- title: Add the page-type manifest template
  category: config
  task_type: feature
  depends_on:
  - '1.1'
  validation_criteria: '1.2.1: Manifest enumerates all five generated page types and
    seven deterministic projections with versions, section requirements, and prose
    limits. file: `src/gobby/install/shared/templates/codewiki/manifest.yaml`.

    1.2.2: `bundled_content_manifest.json` is regenerated and the bundled-content
    integrity test passes; `gobby install` places the template at the `GOBBY_HOME`
    path the 2.3 loader reads. symbol: `sync_bundled_content_to_db`.

    1.2.3: A modified `shared/templates/**` file classifies to the protected `templates`
    content type and blocks the file-install step via `skip_types`; clean, modified,
    and untracked states are covered. test: `tests/sync/test_integrity.py`.'
  labels:
  - covers:wiki-output-design:1.2:1.2.1
  - covers:wiki-output-design:1.2:1.2.2
  - covers:wiki-output-design:1.2:1.2.3
  tdd: false
  source_section: '1.2'
  assigned_agent: backend-developer
- title: Implement stable identities and the build manifest
  category: code
  task_type: feature
  depends_on:
  - '1.1'
  - '1.2'
  validation_criteria: "2.1.1: Page identities are stable across two regenerations\
    \ of an identical input tree. test: `crates/gwiki/tests/code_wiki_manifest.rs::identity_stable_across_regeneration`.\n\
    2.1.2: `build.json` round-trips the complete typed schema (including `layer_names`,\
    \ `tours`, `rename_lineage`, `graph_projection_digest` in both its present and\
    \ absent forms, and an `ai_artifacts` map holding an independent `cache_key` and\
    \ repair state for each page prose slot and each non-page artifact class), stays\
    \ under the configured build-manifest ceiling, and regenerating identical inputs\
    \ yields byte-identical bytes; a digest written through `set_graph_projection_digest`\
    \ is readable verbatim by the next run, and a manifest treated as absent presents\
    \ no digest. test: `crates/gwiki/tests/code_wiki_manifest.rs::build_json_roundtrip_and_byte_identical`.\n\
    2.1.3: Orphaned generated pages are detected and pruned; catalog-owned files are\
    \ never touched; and `is_engine_owned_generated_path` classifies every path class\
    \ exactly once \u2014 manifest-tracked pages and `code/_meta/**` engine-owned,\
    \ `code/INDEX.md`, `_context.md`, the vault-root `_index.md`, `knowledge/**`,\
    \ and rendered source pages not \u2014 so 3.4 and 6.1 share one answer. test:\
    \ `crates/gwiki/tests/code_wiki_manifest.rs::orphan_prune_spares_catalog_files`.\n\
    2.1.4: A slug-changing rename preserves `page_id` while updating path, inbound\
    \ wikilinks, tour metadata, and graph references; lineage is recorded. test: `crates/gwiki/tests/code_wiki_manifest.rs::rename_preserves_page_id`.\n\
    2.1.5: An injected interruption at each publication stage (before staging validation,\
    \ during replacement, during pruning, before manifest commit, during catalog refresh)\
    \ leaves either the old or the new generation fully observable, never a mix. test:\
    \ `crates/gwiki/tests/code_wiki_publication.rs::interrupted_publication_recovers`.\n\
    2.1.6: A shared-mode reader at each publication stage observes exactly one generation\
    \ across pages, projections, catalog, and manifest; two readers entering the crash\
    \ window with a dirty journal recover exactly once under exclusive ownership and\
    \ both serve the recovered generation (or fail loudly), never a half-swapped tree;\
    \ a guarded operation invoked from inside the writer's transaction never re-acquires\
    \ the lock; and lock acquisition honors its bounded timeout. test: `crates/gwiki/tests/code_wiki_publication.rs::concurrent_reader_single_generation`.\n\
    2.1.7: An existing manifest with an unsupported `schema_version` or unparseable\
    \ content triggers the documented full-identity cold-rebuild path; no partial\
    \ read occurs. test: `crates/gwiki/tests/code_wiki_manifest.rs::unsupported_schema_version_resets_identity`.\n\
    2.1.8: Two concurrent writers serialize end-to-end on both a cold vault (no prior\
    \ manifest) and a warm one: the second run reads the first run's committed manifest\
    \ rather than the stale prior state, no page id is minted twice, and no generation\
    \ is clobbered. test: `crates/gwiki/tests/code_wiki_publication.rs::concurrent_writers_serialize`."
  labels:
  - covers:wiki-output-design:2.1:2.1.1
  - covers:wiki-output-design:2.1:2.1.2
  - covers:wiki-output-design:2.1:2.1.3
  - covers:wiki-output-design:2.1:2.1.4
  - covers:wiki-output-design:2.1:2.1.5
  - covers:wiki-output-design:2.1:2.1.6
  - covers:wiki-output-design:2.1:2.1.7
  - covers:wiki-output-design:2.1:2.1.8
  tdd: true
  source_section: '2.1'
  implementation_domain: backend
- title: Implement change classification and incremental invalidation
  category: code
  task_type: feature
  depends_on:
  - '2.1'
  - '2.4'
  validation_criteria: "2.2.1: Each change class maps to the exact regeneration set\
    \ defined above. test: `crates/gwiki/tests/code_wiki_invalidation.rs::change_classes_select_expected_pages`.\n\
    2.2.2: A file rename produces a re-slugged page, pruned old page, and rewritten\
    \ inbound wikilinks. test: `crates/gwiki/tests/code_wiki_invalidation.rs::rename_reslug_rewrites_links`.\n\
    2.2.3: Identical inputs hit the AI cache; a comment-only edit re-anchors citations\
    \ with zero AI regeneration; a change to symbol facts, prompt/schema version,\
    \ template/render version, model lane, or a behavior-bearing manifest field each\
    \ invalidates exactly the artifact classes keyed on it. test: `crates/gwiki/tests/code_wiki_invalidation.rs::ai_cache_key_hits_and_invalidations`.\n\
    2.2.4: A degraded artifact \u2014 page prose and each non-page artifact (layer\
    \ names, tour steps, insight questions) \u2014 regenerates on the next AI-available\
    \ run with unchanged inputs; the outcome survives a second failure and clears\
    \ only after verified output; the unavailable\u2192available capability transition\
    \ is covered. test: `crates/gwiki/tests/code_wiki_invalidation.rs::degraded_artifacts_reselected_until_repaired`.\n\
    2.2.5: The artifact-by-input matrix is enforced: the late projection check regenerates\
    \ the graph on a PARTIAL edge change, a node-field change with identical call/import\
    \ edges (symbol summary, key-symbol membership, layer assignment, alias pair)\
    \ moves the graph-projection input digest and regenerates it too, an input whose\
    \ digest is unchanged leaves `outputs/graph.json` unwritten, COSMETIC tagged-comment\
    \ changes re-render insights rationale, knowledge-only changes regenerate graph,\
    \ insights, and aliasing, and a membership-fingerprint change with no file edits\
    \ classifies ARCHITECTURE and selects exactly the architecture set named in 2.2.\
    \ test: `crates/gwiki/tests/code_wiki_invalidation.rs::input_matrix_selects_all_stale_artifacts`.\n\
    2.2.6: Selection and execution share one resolved execution snapshot: a route,\
    \ profile, provider/model, daemon-lane, or prompt/schema mutation applied after\
    \ capture changes neither the keys selection used nor the lane execution and persistence\
    \ run on, and each of those fields independently invalidates when it differs at\
    \ capture time. test: `crates/gwiki/tests/code_wiki_invalidation.rs::capability_snapshot_is_immutable_and_keyed`."
  labels:
  - covers:wiki-output-design:2.2:2.2.1
  - covers:wiki-output-design:2.2:2.2.2
  - covers:wiki-output-design:2.2:2.2.3
  - covers:wiki-output-design:2.2:2.2.4
  - covers:wiki-output-design:2.2:2.2.5
  - covers:wiki-output-design:2.2:2.2.6
  tdd: true
  source_section: '2.2'
  implementation_domain: backend
- title: Load and validate the page-type manifest
  category: code
  task_type: feature
  depends_on:
  - '1.2'
  validation_criteria: '2.3.1: Loader accepts the bundled template manifest and exposes
    typed entries renderers consume. test: `crates/gwiki/tests/code_wiki_page_manifest.rs::bundled_manifest_valid`.

    2.3.2: Each invalid-manifest class (duplicate id, unsafe path pattern, out-of-range
    cap, forbidden diagram kind, AI-enabled deterministic projection) fails generation
    before any page write; a missing installed manifest fails the same way. test:
    `crates/gwiki/tests/code_wiki_page_manifest.rs::invalid_manifests_fail_before_write`.

    2.3.3: Every page type''s frontmatter is produced through the shared builder from
    a complete `FrontmatterInput` and carries the pinned contract keys; a missing
    required input fails before staging, covered per page type including degraded
    output. test: `crates/gwiki/tests/code_wiki_page_manifest.rs::frontmatter_built_per_page_type`.'
  labels:
  - covers:wiki-output-design:2.3:2.3.1
  - covers:wiki-output-design:2.3:2.3.2
  - covers:wiki-output-design:2.3:2.3.3
  tdd: true
  source_section: '2.3'
  implementation_domain: backend
- title: Implement deterministic module clustering membership
  category: code
  task_type: feature
  depends_on:
  - '2.1'
  validation_criteria: '2.4.1: Clustering is deterministic for identical graphs and
    produces the target cluster-count range on fixture graphs. test: `crates/gwiki/tests/code_wiki_clustering.rs::clustering_deterministic_and_bounded`.'
  labels:
  - covers:wiki-output-design:2.4:2.4.1
  tdd: true
  source_section: '2.4'
  implementation_domain: backend
- title: Define the external-source information model
  category: code
  task_type: feature
  depends_on:
  - '2.1'
  validation_criteria: "2.5.1: The contract doc defines source/segment identity, unified\
    \ attribution, retrieval addressability, and deletion semantics; the extended\
    \ types round-trip through the existing source manifest, and an entry written\
    \ before this leaf reads back with a stable `source_id`. test: `crates/gwiki/tests/wiki_sources.rs::source_model_roundtrip`.\n\
    2.5.2: Removing a source through the existing manifest path writes a tombstone\
    \ instead of dropping the entry, and the derivation returns exactly the dependent\
    \ artifacts plus their `degraded_sources` attribution; no citation into a removed\
    \ source dangles. Both live callers keep their contracts: the CLI removal path\
    \ still deletes raw sources and assets, and its index-failure rollback promotes\
    \ the tombstoned entry back to live rather than failing on the retained id, while\
    \ a live duplicate id still errors; repeated latest-wins session replacement leaves\
    \ exactly one live entry, skips already-tombstoned pages instead of re-removing\
    \ them, and keeps tombstones out of listing, selection, and reindexing. The crate\
    \ compiles with every `SourceRecord` literal migrated to the canonical constructor.\
    \ test: `crates/gwiki/tests/wiki_sources.rs::deletion_tombstones_and_degrades`.\n\
    2.5.3: A segment citation rendered through the unified `Citation` shape resolves\
    \ to its anchor on the rendered source page via the existing read surface, and\
    \ a citation into a tombstoned segment resolves through that same `wiki_read`\
    \ path to the content-free tombstone document at the stable rendered path \u2014\
    \ carrying source and segment identities, anchors, and removal cause \u2014 rather\
    \ than a missing target, after both CLI removal and latest-wins replacement; the\
    \ tombstone document stays absent from listing and search. test: `crates/gwiki/tests/wiki_sources.rs::segment_citations_resolve`."
  labels:
  - covers:wiki-output-design:2.5:2.5.1
  - covers:wiki-output-design:2.5:2.5.2
  - covers:wiki-output-design:2.5:2.5.3
  tdd: true
  source_section: '2.5'
  implementation_domain: backend
- title: Render the at-a-glance landing page
  category: code
  task_type: feature
  depends_on:
  - '2.1'
  - '2.2'
  - '2.3'
  - '2.4'
  - '2.5'
  validation_criteria: "3.1.1: Landing renders all fact tables from supplied facts\
    \ and manifest records and is byte-identical across two runs. test: `crates/gwiki/tests/code_wiki_landing.rs::landing_deterministic`.\n\
    3.1.2: `## Overview` block length \u22642,000 chars; absent page classes render\
    \ as omitted sections. test: `crates/gwiki/tests/code_wiki_landing.rs::overview_block_fits_injection_budget`."
  labels:
  - covers:wiki-output-design:3.1:3.1.1
  - covers:wiki-output-design:3.1:3.1.2
  tdd: true
  source_section: '3.1'
  implementation_domain: backend
- title: Render named layers and the architecture map
  category: code
  task_type: feature
  depends_on:
  - '2.4'
  - '5.3'
  validation_criteria: '3.2.1: `layers.md` and `layers.json` agree on membership and
    render deterministically: provisional names on a cold run, cached names on a warm
    run with a stable membership fingerprint. test: `crates/gwiki/tests/code_wiki_layers.rs::layers_page_matches_json`.

    3.2.2: Architecture map is valid mermaid and carries truncation labeling when
    bounded. test: `crates/gwiki/tests/code_wiki_layers.rs::architecture_map_valid_and_labeled`.'
  labels:
  - covers:wiki-output-design:3.2:3.2.1
  - covers:wiki-output-design:3.2:3.2.2
  tdd: true
  source_section: '3.2'
  implementation_domain: backend
- title: Render module page scaffolds
  category: code
  task_type: feature
  depends_on:
  - '2.1'
  - '2.2'
  - '2.3'
  - '2.4'
  - '2.5'
  - '5.3'
  validation_criteria: '3.3.1: Scaffold emits every section of the module anatomy
    with correct caps and resolvable citations, structure-matching `docs/contracts/wiki-output/examples/module.md`.
    test: `crates/gwiki/tests/code_wiki_module_scaffold.rs::scaffold_sections_and_caps`.

    3.3.2: Symbol rows never contain structural-filler purposes; missing summaries
    render as omitted rows, not filler. test: `crates/gwiki/tests/code_wiki_module_scaffold.rs::no_structural_filler_rows`.

    3.3.3: Across a fixture corpus, every module page carries a validating mermaid
    diagram or a labeled truncation; none carries an empty diagram slot. test: `crates/gwiki/tests/code_wiki_module_scaffold.rs::every_module_page_has_diagram_or_label`.'
  labels:
  - covers:wiki-output-design:3.3:3.3.1
  - covers:wiki-output-design:3.3:3.3.2
  - covers:wiki-output-design:3.3:3.3.3
  tdd: true
  source_section: '3.3'
  implementation_domain: backend
- title: Reconcile the gwiki catalog with the new code namespace
  category: code
  task_type: feature
  depends_on:
  - '3.1'
  validation_criteria: '3.4.1: Catalog regeneration never rewrites engine-owned pages
    and remains byte-identical on rerun. test: `crates/gwiki/src/catalog/regenerate.rs::regenerate_is_byte_identical_on_rerun`.

    3.4.2: Root index groups code entries by the new taxonomy and cross-links same-slug
    concepts across namespaces. symbol: `render_wiki_index`.

    3.4.3: Every file touched by this leaf is below 1,000 lines; `catalog.rs` no longer
    exists as a monolith. file: `crates/gwiki/src/catalog/mod.rs`.

    3.4.4: A fresh vault init writes the new-taxonomy agent guide; an existing user-edited
    guide is left untouched. symbol: `AI_README_TEMPLATE`.

    3.4.5: A catalog read concurrent with each publication stage takes the shared
    generation lock and observes one generation; regeneration invoked from inside
    a publication runs through the guarded form without re-acquiring the lock; the
    documented lock order with `_gwiki/index.lock` is exercised without deadlock;
    and a reader of the vault-root `_index.md` concurrent with regeneration always
    reads one complete index, never a truncated one. test: `crates/gwiki/src/catalog/regenerate.rs::catalog_reads_are_generation_consistent`.'
  labels:
  - covers:wiki-output-design:3.4:3.4.1
  - covers:wiki-output-design:3.4:3.4.2
  - covers:wiki-output-design:3.4:3.4.3
  - covers:wiki-output-design:3.4:3.4.4
  - covers:wiki-output-design:3.4:3.4.5
  tdd: true
  source_section: '3.4'
  implementation_domain: backend
- title: Render the six deterministic projection pages
  category: code
  task_type: feature
  depends_on:
  - '2.1'
  - '2.2'
  - '2.3'
  - '2.4'
  - '2.5'
  - '3.4'
  validation_criteria: '3.5.1: All six projection pages render from the shared golden
    FactsBundle fixture with no index access, and rerun byte-identical. test: `crates/gwiki/tests/code_wiki_projections.rs::projections_deterministic_from_fixture`.

    3.5.2: Truncation is labeled, rows are cited, and every projection registers a
    manifest record and catalog entry. test: `crates/gwiki/tests/code_wiki_projections.rs::projections_bounded_cited_registered`.

    3.5.3: The projection renderer rejects an unsupported `FACTS_VERSION` as local
    defense; the global before-any-side-effect gate is owned by 6.2. test: `crates/gwiki/tests/code_wiki_projections.rs::rejects_unsupported_facts_version`.'
  labels:
  - covers:wiki-output-design:3.5:3.5.1
  - covers:wiki-output-design:3.5:3.5.2
  - covers:wiki-output-design:3.5:3.5.3
  tdd: true
  source_section: '3.5'
  implementation_domain: backend
- title: Implement semantic cluster naming
  category: code
  task_type: feature
  depends_on:
  - '2.4'
  - '3.1'
  - '3.2'
  - '3.3'
  - '3.4'
  - '3.5'
  validation_criteria: '4.1.1: Cached names survive regeneration when membership is
    stable and the full naming key matches, and the naming pass replaces provisional
    cold-start names; with membership held stable, a prompt/schema version change
    and a model-lane change each independently miss the key and regenerate names.
    test: `crates/gwiki/tests/code_wiki_naming.rs::names_cached_across_runs`.'
  labels:
  - covers:wiki-output-design:4.1:4.1.1
  tdd: true
  source_section: '4.1'
  implementation_domain: backend
- title: Generate grounded narrative prose and concept pages
  category: code
  task_type: feature
  depends_on:
  - '4.1'
  validation_criteria: '4.2.1: Prose slots and concept pages render with verified
    citations; failed verification produces degraded sections, not ungrounded text.
    test: `crates/gwiki/tests/code_wiki_narrative.rs::ungrounded_prose_degrades`.

    4.2.2: Every page this leaf produces carries a contract-compliant `summary` built
    through the shared builder. test: `crates/gwiki/tests/code_wiki_narrative.rs::summaries_within_contract`.

    4.2.3: The prose contract''s mechanical predicates are enforced before staging:
    a section rendering a second paragraph block and a normalized assertion repeated
    across two explanatory sections of the same page are each rejected (degraded,
    never published), and single-paragraph pages with distinct claims pass unchanged.
    test: `crates/gwiki/tests/code_wiki_narrative.rs::prose_shape_and_duplicate_claims_rejected`.'
  labels:
  - covers:wiki-output-design:4.2:4.2.1
  - covers:wiki-output-design:4.2:4.2.2
  - covers:wiki-output-design:4.2:4.2.3
  tdd: true
  source_section: '4.2'
  implementation_domain: backend
- title: Generate dependency-ordered guided tours
  category: code
  task_type: feature
  depends_on:
  - '4.1'
  - '4.2'
  validation_criteria: "4.3.1: Both audience tours render 3\u20136 dependency-ordered\
    \ steps with resolvable links. test: `crates/gwiki/tests/code_wiki_tours.rs::tours_ordered_and_bounded`.\n\
    4.3.2: Tour metadata appears in the build manifest with stable page ids. file:\
    \ `crates/gwiki/src/code_wiki/render/tours.rs`."
  labels:
  - covers:wiki-output-design:4.3:4.3.1
  - covers:wiki-output-design:4.3:4.3.2
  tdd: true
  source_section: '4.3'
  implementation_domain: backend
- title: Emit the typed graph projection
  category: code
  task_type: feature
  depends_on:
  - '4.2'
  - '4.3'
  validation_criteria: "5.1.1: Projection contains typed, confidence-tagged nodes/edges\
    \ (including symbol nodes and evidence spans on extracted edges) across both namespaces\
    \ and is deterministic. test: `crates/gwiki/tests/code_wiki_graph_projection.rs::projection_typed_and_deterministic`.\n\
    5.1.2: `outputs/graph.json` has exactly one semantic producer \u2014 the `gwiki\
    \ graph` command re-exports the committed projection; existing export consumers\
    \ (JSON-LD, llms indexes, export tests) render from the extended schema; every\
    \ file this leaf touches is below 1,000 lines; `crates/gwiki/tests/fixtures/graph_export_schema.json`\
    \ is committed and regenerated byte-identically by the serializer; and the late\
    \ projection check writes the projection and its digest exactly on a digest mismatch,\
    \ leaves the committed output untouched when the digest matches, leaves the prior\
    \ digest unadvanced when the projection fails to emit, and performs no graph write\
    \ on the next unchanged run that reads the persisted digest back. test: `crates/gwiki/src/exports/tests.rs::extended_schema_serves_all_consumers`."
  labels:
  - covers:wiki-output-design:5.1:5.1.1
  - covers:wiki-output-design:5.1:5.1.2
  tdd: true
  source_section: '5.1'
  implementation_domain: backend
- title: Render the insight report
  category: code
  task_type: feature
  depends_on:
  - '5.1'
  validation_criteria: '5.2.1: Insights page renders all four sections with citations;
    deterministic sections are byte-stable across runs; an inferred cross-layer edge
    is excluded from surprising connections rather than cited. test: `crates/gwiki/tests/code_wiki_insights.rs::insights_sections_render`.

    5.2.2: Rationale renders the FactsBundle''s tagged-comment spans with exact `[path:line]`
    citations; a test proves the renderer performs no direct source or index access.
    test: `crates/gwiki/tests/code_wiki_insights.rs::rationale_extraction_cited`.'
  labels:
  - covers:wiki-output-design:5.2:5.2.1
  - covers:wiki-output-design:5.2:5.2.2
  tdd: true
  source_section: '5.2'
  implementation_domain: backend
- title: Implement the bounded diagram fallback
  category: code
  task_type: feature
  depends_on:
  - '2.1'
  - '2.2'
  - '2.3'
  - '2.4'
  - '2.5'
  validation_criteria: '5.3.1: Over-bound graphs produce labeled top-N diagrams, never
    suppression. test: `crates/gwiki/tests/code_wiki_diagrams.rs::truncated_graph_yields_labeled_diagram`.

    5.3.2: Every emitted diagram validates as mermaid; only the three permitted kinds
    are emittable. test: `crates/gwiki/tests/code_wiki_diagrams.rs::only_permitted_kinds_valid`.'
  labels:
  - covers:wiki-output-design:5.3:5.3.1
  - covers:wiki-output-design:5.3:5.3.2
  tdd: true
  source_section: '5.3'
  implementation_domain: backend
- title: Prefer compact summaries in retrieval and session injection
  category: code
  task_type: feature
  depends_on:
  - '3.1'
  - '3.2'
  - '3.3'
  - '3.4'
  - '3.5'
  validation_criteria: "6.1.1: Normalizer obeys the 180-char/word-boundary/no-structure\
    \ contract. test: `crates/gwiki/tests/code_wiki_summaries.rs::normalizer_contract`.\n\
    6.1.2: Search snippets and catalog entries prefer `summary` frontmatter with normalized-body\
    \ fallback, across backends and output formats. test: `crates/gwiki/tests/code_wiki_summaries.rs::search_snippets_prefer_summary`.\n\
    6.1.3: Rendering the catalog root index and passing it through `load_wiki_overview`\
    \ yields a \u22642,000-char overview block with orientation entry points, and\
    \ a read concurrent with catalog regeneration returns either the prior or the\
    \ new complete block, never a partial one. test: `tests/hooks/test_wiki_overview_injection.py`.\n\
    6.1.4: `wiki_read` and `wiki_search` acquire the shared generation lock and observe\
    \ one generation across every publication stage, including the dirty-journal window\
    \ before recovery; the bounded timeout surfaces its documented failure instead\
    \ of hanging. test: `crates/gwiki/tests/code_wiki_reader_generation.rs::readers_observe_single_generation`.\n\
    6.1.5: Search results are filtered to the committed generation for engine-owned\
    \ generated paths only: a generated hit absent from the committed manifest is\
    \ dropped rather than served, a generated page published since the last index\
    \ refresh is simply missing (never half-rendered), no snippet mixes generations,\
    \ and current `knowledge/**`, source-page, `code/INDEX.md`, and `_context.md`\
    \ hits survive the filter unchanged across both search backends. test: `crates/gwiki/tests/code_wiki_reader_generation.rs::search_results_match_committed_generation`."
  labels:
  - covers:wiki-output-design:6.1:6.1.1
  - covers:wiki-output-design:6.1:6.1.2
  - covers:wiki-output-design:6.1:6.1.3
  - covers:wiki-output-design:6.1:6.1.4
  - covers:wiki-output-design:6.1:6.1.5
  tdd: true
  source_section: '6.1'
  implementation_domain: backend
- title: Assemble the engine entrypoint
  category: code
  task_type: feature
  depends_on:
  - '2.2'
  - '2.3'
  - '2.4'
  - '2.5'
  - '3.1'
  - '3.2'
  - '3.3'
  - '3.4'
  - '3.5'
  - '4.1'
  - '4.2'
  - '4.3'
  - '5.1'
  - '5.2'
  - '5.3'
  validation_criteria: "6.2.1: The public entrypoint composes the full pipeline end-to-end\
    \ on a fixture vault (compile-time registration proven by the integration test\
    \ invoking `generate_code_wiki`). test: `crates/gwiki/tests/code_wiki_engine.rs::engine_entrypoint_generates`.\n\
    6.2.2: An unsupported `FACTS_VERSION` is rejected before any side effect \u2014\
    \ writer, cache, and AI spies observe zero activity. test: `crates/gwiki/tests/code_wiki_engine.rs::facts_version_preflight_no_side_effects`.\n\
    6.2.3: A run whose only change is cluster membership (no file edits) classifies\
    \ ARCHITECTURE through the engine and regenerates the architecture set named in\
    \ 2.2 \u2014 `layers.md`, `layers.json`, affected concept pages, tours, and the\
    \ graph projection \u2014 proving membership is computed before selection; the\
    \ graph projection is reached through the late check, whose digest mismatches\
    \ because layer assignment moved, and a run with unchanged upstream values leaves\
    \ `outputs/graph.json` unwritten while the rest of the pipeline still runs. test:\
    \ `crates/gwiki/tests/code_wiki_engine.rs::membership_change_selects_architecture_set`."
  labels:
  - covers:wiki-output-design:6.2:6.2.1
  - covers:wiki-output-design:6.2:6.2.2
  - covers:wiki-output-design:6.2:6.2.3
  tdd: true
  source_section: '6.2'
  implementation_domain: backend
- title: Run full-engine end-to-end acceptance
  category: test
  task_type: feature
  depends_on:
  - '2.1'
  - '2.2'
  - '2.3'
  - '2.4'
  - '2.5'
  - '3.1'
  - '3.2'
  - '3.3'
  - '3.4'
  - '3.5'
  - '4.1'
  - '4.2'
  - '4.3'
  - '5.1'
  - '5.2'
  - '5.3'
  - '6.1'
  - '6.2'
  validation_criteria: "7.1.1: Two consecutive full generations with identical inputs\
    \ are byte-identical for every deterministic surface; AI prose is stable when\
    \ caches are warm. test: `crates/gwiki/tests/code_wiki_end_to_end.rs::full_generation_deterministic`.\n\
    7.1.2: Total generated page count lands in 50\u201390; zero pages under `code/files/**`.\
    \ test: `crates/gwiki/tests/code_wiki_end_to_end.rs::page_count_in_band`.\n7.1.3:\
    \ All seven deterministic projection paths exist (`architecture/layers.md` plus\
    \ `features.md`, `deprecations.md`, `changes.md`, `hotspots.md`, `ownership.md`,\
    \ `infrastructure.md`), each with a build-manifest record. test: `crates/gwiki/tests/code_wiki_end_to_end.rs::projection_paths_exist`.\n\
    7.1.4: Every generated page passes the anatomy lint: contract frontmatter with\
    \ `summary` \u2264180 chars, resolvable citations, table caps respected, collapsible\
    \ provenance header present, exactly one paragraph block per explanatory section,\
    \ and no normalized assertion repeated across two explanatory sections of a page.\
    \ test: `crates/gwiki/tests/code_wiki_end_to_end.rs::anatomy_lint_all_pages`.\n\
    7.1.5: Every module page carries at least one valid diagram or a labeled truncation.\
    \ test: `crates/gwiki/tests/code_wiki_end_to_end.rs::module_diagrams_present`.\n\
    7.1.6: `_meta/build.json` stays under the configured build-manifest ceiling on\
    \ the real Gobby corpus; orphan pruning leaves catalog-owned files intact. test:\
    \ `crates/gwiki/tests/code_wiki_end_to_end.rs::manifest_size_and_orphans`.\n7.1.7:\
    \ A PARTIAL change regenerates only the expected page set; a rename re-slugs,\
    \ preserves `page_id`, and rewrites links. test: `crates/gwiki/tests/code_wiki_end_to_end.rs::incremental_and_rename`.\n\
    7.1.8: `wiki_search`, `wiki_read`, and the graph projection work over the new\
    \ layout; root index cross-links same-slug concepts across namespaces; and a regeneration\
    \ concurrent with all three surfaces yields one generation everywhere, with search\
    \ returning no engine-owned generated path absent from the committed manifest\
    \ while curated and catalog-owned hits stay visible. test: `crates/gwiki/tests/code_wiki_end_to_end.rs::retrieval_over_new_layout`.\n\
    7.1.9: Production vault byte-untouched throughout (checksum before/after). test:\
    \ `crates/gwiki/tests/code_wiki_end_to_end.rs::production_vault_untouched`."
  labels:
  - covers:wiki-output-design:7.1:7.1.1
  - covers:wiki-output-design:7.1:7.1.2
  - covers:wiki-output-design:7.1:7.1.3
  - covers:wiki-output-design:7.1:7.1.4
  - covers:wiki-output-design:7.1:7.1.5
  - covers:wiki-output-design:7.1:7.1.6
  - covers:wiki-output-design:7.1:7.1.7
  - covers:wiki-output-design:7.1:7.1.8
  - covers:wiki-output-design:7.1:7.1.9
  tdd: false
  source_section: '7.1'
  assigned_agent: backend-developer
```
