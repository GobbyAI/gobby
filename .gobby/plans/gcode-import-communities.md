Plan artifact: `.gobby/plans/gcode-import-communities.md`

# gcode Project-Level Import Communities

**Plan ID:** gcode-import-communities

## Overview
`kind: framing`

`gcode graph view --view=mcg` runs Leiden on the wrong graph. The walk it clusters is
bipartite: file nodes point at raw module-specifier nodes, the provider file behind a
module is added as a node with no incident edge, one file appears as several alias
nodes, and external packages are clustered as ordinary nodes. Default depth is one
hop, so each call clusters a tiny truncated neighborhood. The live result is one
`community-N` per node, which carries no architectural signal.

Draft 1 fixed the projection: one node per visible internal source file, one weighted
undirected edge per importer-to-provider relationship resolved through the existing
`McgIdentity` provider map, external and ambiguous modules excluded, Leiden once over
the whole visible project. Draft 2 raises the goal to parity+ with Graphify 0.9.55's
community subsystem (pinned at `/Users/josh/Projects/wiki-bakeoff-code-2026-09/sources/graphify/graphify/`,
verified line by line on 2026-09-19). Graphify has seven capabilities Draft 1 had no
answer for: cohesion scoring, oversized splitting, low-cohesion splitting, cross-run
identity, signature-gated label reuse, persisted memberships, and semantic labels.
Draft 2 closes all seven and adds three things Graphify lacks: identity that never
recycles a retired id, a calibrated admission gate for generated labels, and a
community evidence operation that Ask and future navigation surfaces reach through
the stable `gcode evidence` contract.

Parity+ rests on four properties, because upstream Graphify 0.9.60 closed its own
py3.13 Leiden gap: a file-level projection with externals excluded by construction,
monotone community ids with Jaccard remapping, a generated label admitted only when a
decision model is confident it beats the deterministic candidates, and an evidence
surface with byte-bounded, replay-verifiable items.

Decision record (confirmed with the user on 2026-09-10 and 2026-09-19):

- Scope: whole visible project. The completed plan `class-hierarchy-graph.md` said
  "Do not run Leiden on the whole project graph"; that decision is superseded here.
- Persistence: the partition is computed and persisted at index time inside
  `gcode index`, per `(machine_id, project_id)`, in a new `code_communities` table.
  Read paths (MCG view, communities view, report, evidence) only read stored rows and
  never run Leiden inline. This supersedes Draft 1's "computed per call, no persistence".
- Identity: community ids survive re-indexing through greedy Jaccard remapping against
  the stored partition; unmatched new communities take ids above a per-project
  watermark, so a retired id is never reissued (Graphify recycles them).
- Cohesion and splitting: Graphify's constants (oversized `max(10, N/4)`, low-cohesion
  `size >= 50 && cohesion < 0.05`) are the starting point, evaluated in integer math,
  confirmed or replaced by the Q1 experiment. No CLI knobs; `DEFAULT_GAMMA` stays 1.0.
- Labels: gcode always writes a deterministic label (dominant directory, in-degree
  fallback) plus candidates. A daemon job generates a 2–5 word purpose name, validates
  it against a schema, and asks Jev (TypeSafe System One, a `choice` question) whether
  the generated name beats the deterministic candidates. The generated name is admitted
  at confidence ≥ 0.5; when no decisions endpoint is configured, schema validation alone
  admits it; otherwise the deterministic label stands. Labels are cached by member
  signature; a stale model label is displayed as the deterministic label with
  `label_stale: true`.
- Surfaces: fix MCG labels, add a seedless `--view=communities` with `--min-size` and a
  `--community <id|label>` detail mode, add an `## Import communities` section to
  `gcode graph report`, and add the `communities` evidence operation. CLI contract 10 → 11.
- Graph: plain import edges only. No call edges, no hub exclusion (externals are
  excluded by construction, which is what Graphify's hub exclusion approximates).
- Out of scope by user direction: any wiki renderer (gwiki was deleted 2026-09-05 in
  `0a4d3cb3c3`; a future wiki reads the evidence operation), memory community detection
  (deferred section D1), and Understand Anything batching comparisons.

## Constraints
`kind: framing`

- Do not add a second community algorithm. Reuse
  `crates/gcore/src/graph_analytics/leiden.rs` through the `graph_analytics` façade.
- `PreparedGraph::new` dedups edges by `(source, target, kind)` and does not sum
  multiplicity, so edge folding happens in the gcode builder, never by emitting
  duplicate `AnalyticsEdge`s.
- Graph analytics code surfaces invalid graph input as typed errors. Ordinary data
  conditions (external modules, ambiguous providers, self-imports, rows for non-visible
  files, zero files, zero stored communities) are never errors.
- gcode has zero text-generation call sites and never holds provider credentials
  (`AiRouting` is `{Daemon, Off}`; memory `959d8494`). Every model call in this plan
  lives in the Python daemon (P6). gcode reads label rows only.
- A new table lands as one commit with every derived carrier and is live only after
  `uv run gobby restart` (schema plan + apply) and `uv run gobby cutover` for the
  coherent set `gcode`, `gdaemon`, `ghook` (`promote_workspace_binary_set`). Copying a
  single binary by hand is refused as `mixed installed binary set`. Announce the
  restart with a `global` `send_message` and wait for a quiet window.
- Hand-maintained `.rs`/`.py` files stay under 1,000 lines. Sizes on 2026-09-19 at
  `7b6dc3bfd0`: `cli.rs` 827 (split in 4.1 before it grows), `graph_analytics.rs` 693,
  `contract.rs` 693, `db/queries.rs` 664 (new SQL goes to `crates/gcode/src/db/communities.rs`),
  `evidence/graph.rs` 673, `overlay.rs` 691, `pipeline.rs` 546, `crates/gcode/src/evidence/mod.rs` 476,
  `crates/gcode/src/evidence/contracts.rs` 405, `crates/gcode/src/commands/graph/view/mod.rs` 406, `render.rs` 332, `crates/gcode/src/commands/graph/view/mcg/identity.rs` 260,
  `crates/gcode/src/commands/graph/view/mcg/fetch.rs` 241, `mcg.rs` 217, `maintenance.py` 481, `evidence.py` 756,
  `validation.py` 756, `stage_runtime.py` 790. No targeted production file is at or
  above 850 lines.
- No backward compatibility: 0.5.0 is unshipped. The CLI contract version bumps 10 to
  11. Version 10 is pinned in five places: `crates/gcode/src/contract.rs:12`,
  `crates/gcode/contract/gcode.contract.json`, `tests/contracts/gcode.contract.json`,
  `crates/gcode/tests/contract.rs:87,151` (the test at 149 is named
  `contract_is_version_ten_with_ask_and_evidence_without_codewiki`), and
  `tests/test_cli_contracts.py:175`. Pinned JSON is regenerated with
  `gcode contract --format json | python3 -m json.tool --indent 2` (no `--sort-keys`).
- `src/gobby/install/bundled_content_manifest.json` is a git-ignored build artifact. It
  is never generated or committed by this plan. `skills/code-index/SKILL.md` was deleted
  in `9acef616df`; the live skill text is
  `src/gobby/install/shared/skills/gobby/references/code-index/graphs.md`.
  `crates/gcode/assets/SKILL.md` is the router skill and carries no community text.
- Target scope form is plan-wide per file: every existing symbol-bearing file is
  targeted as `path::*` with a scope-reason naming the symbols; bare paths name new
  files. Exact indexed names are recorded in each Research context instead.
- Shared-target chain (the validator requires a dependency path between any two
  deliverables that touch one file): `communities.rs` 2.1 → 2.2 → 2.3 → 3.2;
  `mcg.rs`/`crates/gcode/src/commands/graph/view/mcg/fetch.rs`/`crates/gcode/src/commands/graph/view/mcg/tests.rs` 2.1 → 4.2 (via 3.2); `render.rs`,
  `render_tests.rs`, `crates/gcode/src/commands/graph/view/tests.rs` 4.2 → 4.3; `contract.rs` and both pinned JSON copies
  3.2 → 4.3 → 4.4 → 5.1; `crates/gcode/src/contract/schema.rs` and `crates/gcode/tests/contract.rs`
  4.3 → 4.4 → 5.1; `docs/contracts/gcode-cli.md` 4.3 → 5.2; `summary.rs` 1.2 → 4.4;
  `managed_postgres_privileges.json` 3.1 → 3.2; `community_labeler.py` 6.2 → 6.3;
  `crates/gcode/src/cli/graph_view.rs` 4.1 → 4.3.
- Consumer sweep evidence (`gcode grep -F`, 2026-09-19, `crates/` and `src/`):
  `McgIdentity` appears in `crates/gcode/src/commands/graph/view/mcg/identity.rs`, `crates/gcode/src/commands/graph/view/mcg/fetch.rs`, `crates/gcode/src/commands/graph/view/mcg/tests.rs`;
  `assign_leiden_communities` in `mcg.rs`, `crates/gcode/src/commands/graph/view/mcg/fetch.rs`, `crates/gcode/src/commands/graph/view/mcg/tests.rs`;
  `load_identity` only in `crates/gcode/src/commands/graph/view/mcg/fetch.rs` (definition :112, call :158);
  `GraphViewSeed::` in `cli.rs`, `dispatch.rs:570`, `crates/gcode/src/commands/graph/view/mod.rs:386-396`,
  `crates/gcode/src/commands/graph/view/tests.rs`; `GraphViewKind::` in `cli.rs`, `crates/gcode/src/cli/tests/projection.rs`,
  `crates/gcode/src/commands/graph/view/mod.rs`, `crates/gcode/src/commands/graph/view/tests.rs`, `render_tests.rs:113-115,133-135` (exhaustive arms),
  `crates/gcode/src/commands/graph/view/mcg/tests.rs`, and constructor uses in `fcg.rs:444`, `class_hierarchy.rs:430`;
  `ViewCommunity` in `render.rs`, `mcg.rs`; `analytics_graph_from_payload` in
  `render.rs:225` and `crates/gcode/src/commands/graph/view/tests.rs` only; `weight_for_kind` in gcore,
  `graph/code_graph/payload.rs`, `render.rs`, `mcg.rs` (stays `pub`);
  `read_active_imports` in `db/queries.rs`, `crates/gcode/src/commands/graph/view/mcg/fetch.rs`, `code_graph/tests.rs`,
  `queries_cas_tests.rs` (signature unchanged); `EvidenceOperation::` in
  `crates/gcode/src/evidence/mod.rs`, `crates/gcode/src/evidence/tests.rs`, `crates/gcode/src/commands/evidence.rs`, `crates/gcode/tests/evidence.rs`;
  `graph_report_keys` and `evidence_keys` in `crates/gcode/src/contract/schema.rs` and `contract.rs`;
  Python evidence operation literals at `src/gobby/ask/evidence.py:129`,
  `stage_runtime.py:730`, `interactive_evidence.py:16`, `src/gobby/mcp_proxy/tools/ask.py:108,300`.

## Reference behavior and baselines
`kind: framing`

Graphify 0.9.55 (anchors in the pinned source; upstream 0.9.64 changes none of the
thresholds, remap, signature, prompt, or MCP tool):

| Capability | Graphify | Draft 2 |
| --- | --- | --- |
| Partition | undirected heterogeneous graph, Leiden γ=1.0 seed 42, Louvain fallback (`cluster.py:96-166`) | file-level import graph, deterministic Rust Leiden γ=1.0, no RNG |
| Oversized split | `max_size = max(10, floor(0.25·N))`, strict `>`, one re-partition, no recursion (`cluster.py:169-170, 298-305`) | same constants, integer math, children not re-checked |
| Low-cohesion split | `len >= 50 and cohesion < 0.05`, single pass, ≤1-group result discarded (`cluster.py:171-172, 307-316`) | `n >= 50 && 40*E < n*(n-1)` in `u128` |
| Cohesion | `edges / (n(n-1)/2)`, `n<=1 → 1.0`, displayed 2dp (`cluster.py:346-354`) | same, stored as a double for display |
| Ordering | `(-size, sorted members)` (`cluster.py:318-325`) | `(size desc, members[0] asc)` |
| Member signature | sha256 over sorted `str(member) + "\x00"`, `hexdigest()[:16]` (`cluster.py:202-220`) | same over sorted member paths |
| Remap | greedy by overlap count, unmatched take lowest free ids, retired ids recycled (`cluster.py:361-409`) | greedy by Jaccard then overlap, unmatched take ids above a watermark, never recycled |
| Hub label | max-degree member (`cluster.py:175-199`) | dominant directory, in-degree fallback |
| LLM label | free-form 2–5 words from 12 representatives, `Community N` on failure (`llm.py:3189-3300`) | generated 2–5 words, schema-validated, Jev-gated against deterministic candidates |
| Label reuse | per-cid signature equality in `.graphify_labels.json.sig` (`cli.py:2134-2232`) | `labeled_signature` vs `member_signature` per row |
| Retrieval | MCP `get_community(community_id, token_budget)` (`serve.py:1186-1209`) | `gcode evidence` operation `communities` with `max_bytes` continuation |

Bakeoff baselines (sealed September 2026 artifacts, Game Goblins corpus, frozen commit
`0216f1e33f05…`): Graphify C1 produced 116 communities over 2,308 nodes, largest 3.4% of
nodes, median size 17.5, 27 singletons, cohesion median 0.18 for communities of three or
more, and zero semantic labels; gcode R05's MCG produced 19 communities over 35 nodes,
17 of them singletons. Across the June, August, and September runs Graphify never
produced a semantically named community; `ADOPTION-CANDIDATES.md:71` calls semantic
naming "the highest-confidence adoption candidate". These are the numbers Q1 must beat.

Jev contract (docs.typesafe.ai, verified 2026-09-19): `POST https://api.typesafe.ai/v1/systemone`
with bearer auth and body `{state, model: "jev-latest", questions}`; a `choice` question
takes `criteria: {option: description}` (≤255) and answers `{choice, probabilities,
confidence}`; the docs recommend acting at confidence ≥ 0.5; 64k tokens per request, 32k
for state plus the longest question; $0.042 per million input tokens, output free.
OpenRouter exposes `POST /api/alpha/decisions` (returns 401 without a key) but its wire
shape is unverified; 6.3 records the spike result.

## P1: Kernel entry points
`kind: framing`

**Goal**: gcore exposes a communities-only entry with typed input validation and a
centrality-only entry for the report.

### 1.1 Add `communities()` and `GraphInputError` to gcore graph analytics [category: code]
`kind: deliverable`

Targets:
- `crates/gcore/src/graph_analytics.rs::*` — scope-reason: add the communities entry point and typed input error beside the existing analyze façade, plus inline tests

Research context: `analyze` (`graph_analytics.rs:78-95`) runs Tarjan bridges,
centrality, god nodes, unexpected links, and hotspots; `PreparedGraph` (:127-133) and
its methods (`new` :136, `centrality` :211, `communities` :279) are private. `leiden.rs`
is a private module whose items are `pub(super)` (`DEFAULT_GAMMA` :16,
`detect_communities` :366). `PreparedGraph::communities` assigns ordinals by the
lexicographically smallest member id, so ordinal ids renumber on any membership change;
that is why P2 remaps rather than reusing ordinals. `thiserror` is already a gcore
dependency. Existing tests at :537-692 (`seeded_graph`, singleton and empty cases) are
the fixtures to reuse. Rejected: exposing `PreparedGraph` publicly (its sanitizing
constructor is what the typed error replaces).

```rust
#[derive(Debug, Clone, PartialEq, thiserror::Error)]
pub enum GraphInputError {
    #[error("duplicate node id {id}")]
    DuplicateNode { id: String },
    #[error("edge {source} -> {target} ({kind}) references an unknown node")]
    UnknownEndpoint { source: String, target: String, kind: String },
    #[error("edge {source} -> {target} ({kind}) has invalid weight {weight}")]
    InvalidWeight { source: String, target: String, kind: String, weight: f64 },
    #[error("self-loop on {id} ({kind})")]
    SelfLoop { id: String, kind: String },
}

/// Leiden communities only. Validates input instead of sanitizing it.
pub fn communities(graph: &AnalyticsGraph) -> Result<Vec<Community>, GraphInputError>;
```

Validation order: duplicate node ids, then per edge unknown endpoint, self-loop,
non-finite or non-positive weight. On success return
`PreparedGraph::new(graph).communities().0`. Empty graph returns an empty vec; nodes
without edges return one singleton community each, matching `analyze`.

Tests (inline `#[cfg(test)]`): `communities_matches_analyze_partition_on_seeded_graph`,
`communities_rejects_unknown_endpoint`, `communities_rejects_invalid_weight` (NaN and
0.0), `communities_rejects_duplicate_node`, `communities_rejects_self_loop`,
`communities_empty_graph_is_empty`, `communities_without_edges_is_all_singletons`.

Verify: `cargo fmt -p gobby-core -- --check`, `cargo clippy -p gobby-core --features graph-analytics`,
`cargo nextest run -p gobby-core --features graph-analytics -E 'test(graph_analytics)'`.

**Acceptance:**

- 1.1.1 - `communities(&AnalyticsGraph)` returns the same partition as `analyze` on the seeded graph and skips the other passes. symbol: `communities`. file: `crates/gcore/src/graph_analytics.rs`.
- 1.1.2 - `GraphInputError` is returned for duplicate node, unknown endpoint, invalid weight, and self-loop instead of silent sanitizing. symbol: `GraphInputError`. file: `crates/gcore/src/graph_analytics.rs`.
- 1.1.3 - Empty and edge-less graphs behave as `analyze` does. test: `crates/gcore/src/graph_analytics.rs::communities_without_edges_is_all_singletons`.

### 1.2 Add `centrality()` and stop `graph report` from running Leiden twice [category: code] (depends: 1.1)
`kind: deliverable`

Targets:
- `crates/gcore/src/graph_analytics.rs::*` — scope-reason: add the centrality entry point sharing the validation of communities, plus an inline parity test
- `crates/gcode/src/graph/report/summary.rs::*` — scope-reason: replace both analyze calls with centrality in gcore_hotspots_for_code_graph and gcore_incoming_call_hotspots

Research context: `gcore_hotspots_for_code_graph` (`summary.rs:51-91`, `analyze` at
:68) and `gcore_incoming_call_hotspots` (:102-156, `analyze` at :133) each run the full
`analyze` pass and read only `.centrality`, discarding two Leiden partitions per report
(observed 2026-09-19; the executor confirms no other field is read before swapping).
`CentralityScore` (`graph_analytics.rs:42-46`) is the element type of
`GraphAnalytics.centrality`. Rejected: caching the `analyze` result across the two
callers (still pays for Leiden once per report for nothing).

`pub fn centrality(graph: &AnalyticsGraph) -> Result<Vec<CentralityScore>, GraphInputError>`
validates exactly as `communities()` and returns `PreparedGraph::new(graph).centrality()`.
Inline test `centrality_matches_analyze_centrality` on `seeded_graph()`. In `summary.rs`
map `GraphInputError` into the existing hotspot degradation path (the report already
tolerates a missing analytics input); no output shape changes.

Verify: `cargo nextest run -p gobby-core --features graph-analytics -E 'test(graph_analytics)'`,
`cargo nextest run -p gobby-code -E 'test(report)'`, and the Q1 read-path timing.

**Acceptance:**

- 1.2.1 - `centrality(&AnalyticsGraph)` returns the same scores as `analyze(...).centrality` on the seeded graph. test: `crates/gcore/src/graph_analytics.rs::centrality_matches_analyze_centrality`.
- 1.2.2 - No `analyze` call remains in `crates/gcode/src/graph/report/summary.rs`; report hotspots and bridges are byte-identical before and after on the Gobby checkout. file: `crates/gcode/src/graph/report/summary.rs`.

## P2: Partition and identity
`kind: framing`

**Goal**: a pure, deterministic file-level partition with cohesion, splits, signatures,
deterministic labels, and stable ids, in a gcode module the index pipeline and the views
both reach. No schema, no I/O beyond loading import rows.

### 2.1 Move `McgIdentity` to `communities::identity::ImportIdentity` with a connection-taking loader [category: refactor]
`kind: deliverable`

Targets:
- `crates/gcode/src/communities.rs`
- `crates/gcode/src/communities/identity.rs`
- `crates/gcode/src/communities/identity_tests.rs`
- `crates/gcode/src/lib.rs::*` — scope-reason: declare the new communities module beside the other crate modules
- `crates/gcode/src/commands/graph/view/mcg/identity.rs::*` — scope-reason: remove McgIdentity, providers_for, unique_provider, and from_resolution; keep the seed types
- `crates/gcode/src/commands/graph/view/mcg/fetch.rs::*` — scope-reason: replace load_identity with communities::identity::load_project_imports on the connection run already opens
- `crates/gcode/src/commands/graph/view/mcg.rs::*` — scope-reason: import ImportIdentity from its new module
- `crates/gcode/src/commands/graph/view/mcg/tests.rs::*` — scope-reason: move the identity tests and the identity_from helper to communities/identity_tests.rs

Research context: `McgIdentity` (`crates/gcode/src/commands/graph/view/mcg/identity.rs:19-23`) with `providers_for` (:138,
private), `unique_provider` (:150, `pub(super)`), and `from_resolution` (:162,
`pub(super)`) is the provider map the partition needs. `McgSeed`, `McgSeedSelector`,
`McgSeedError`, `resolve_mcg_seed` (:76), `close_endpoint` (:107), `closed_file_seed`,
`file_endpoint`, `module_endpoint` stay in `crates/gcode/src/commands/graph/view/mcg/identity.rs`. `load_identity`
(`crates/gcode/src/commands/graph/view/mcg/fetch.rs:112-128`) opens its own read-only connection, computes
`visibility::visible_tree`, reads `crate::db::read_active_imports(&mut conn, &ctx.project_id)`
(queries module, lines 338-364, joins `code_imports` to this machine's
`code_indexed_file_states`), builds the resolver with `build_import_resolution_context`,
and calls `from_resolution`; it drops the rows after building the identity. gcode is a
library crate: `crates/gcode/src/lib.rs:2-45` declares modules (`mod db;`, `pub mod
evidence;`, `mod commands;` …), so `mod communities;` is declared there. The index
pipeline (3.2) must not depend on a command module, and it already holds a connection
inside its checkout fence, so the loader takes the connection. Rejected: keeping the
identity under `mcg/` with widened visibility (a command module would become an index
dependency); a new top-level `imports` module (one module fewer this way, and the type
has exactly two consumers, both community-related).

New `crates/gcode/src/communities.rs` (module root, `pub(crate) mod identity;` now; 2.2,
2.3, and 3.2 add siblings). New `crates/gcode/src/communities/identity.rs` holds `ImportIdentity`
(the moved struct, `unique_provider`/`from_resolution`/`providers_for` `pub(crate)`) and:

```rust
pub(crate) struct ProjectImports { pub identity: ImportIdentity, pub rows: Vec<(String, String)> }
pub(crate) fn load_project_imports(conn: &mut postgres::Client, ctx: &Context) -> anyhow::Result<ProjectImports>;
```

Same body as `load_identity`, keeping `rows` as `(file_path, module_name)`. `crates/gcode/src/commands/graph/view/mcg/fetch.rs::run`
opens its connection as today and passes it. Identity tests move to
`crates/gcode/src/communities/identity_tests.rs` via the `#[path]` test-file idiom from the crates AGENTS guide, including
`mcg_identity_build_handles_twenty_thousand_rows` and the `identity_from` helper (keep a
one-line re-export of the helper for `crates/gcode/src/commands/graph/view/mcg/tests.rs`).

Verify: `cargo nextest run -p gobby-code -E 'test(identity) | test(mcg)'`, `cargo clippy -p gobby-code`.

**Acceptance:**

- 2.1.1 - `ImportIdentity` lives in `crates/gcode/src/communities/identity.rs` with `unique_provider` and `from_resolution` reachable crate-wide; `crates/gcode/src/commands/graph/view/mcg/identity.rs` keeps only seed resolution. symbol: `ImportIdentity`. file: `crates/gcode/src/communities/identity.rs`.
- 2.1.2 - `load_project_imports(conn, ctx)` returns the identity and the import rows on a caller-supplied connection; `load_identity` is gone. symbol: `load_project_imports`. file: `crates/gcode/src/communities/identity.rs`.
- 2.1.3 - The moved identity tests pass unchanged in their new file. test: `crates/gcode/src/communities/identity_tests.rs::mcg_identity_build_handles_twenty_thousand_rows`.

### 2.2 Build the partition: graph, Leiden, splits, cohesion, deterministic labels, signatures [category: code] (depends: 1.1, 2.1)
`kind: deliverable`

Targets:
- `crates/gcode/src/communities.rs`
- `crates/gcode/src/communities/partition.rs`
- `crates/gcode/src/communities/labels.rs`
- `crates/gcode/src/communities/partition_tests.rs`

Research context: inputs are `ImportIdentity` and the `(source, module)` rows from 2.1.
`gobby_core::graph_analytics::communities` (1.1) is the only kernel entry used; `analyze`
is never called from gcode for partitions. Graphify's split and cohesion behavior is in
the reference table above; its cohesion is a raw float and its CHANGELOG records a split
threshold made unreachable by rounding, hence integer comparison here. Rejected:
recursion on split children (Graphify does one pass and the experiment can revisit);
hub exclusion (externals are already excluded, `module:typing` degree 1210 was the
dominant hub in the #22140 diagnostic); weighting by anything other than fold count.

`partition.rs`:

```rust
pub(crate) struct PartitionCommunity {
    pub members: Vec<String>,          // sorted asc, raw paths
    pub internal_edges: usize,         // unordered pairs with at least one import
    pub cohesion: f64,                 // internal_edges / (n(n-1)/2); 1.0 when n <= 1
    pub in_degree: BTreeMap<String, usize>, // distinct importers per member, project-wide
    pub member_signature: String,      // sha256(sorted member + "\0" each)[..16]
}
pub(crate) struct DirectedImport { pub importer: String, pub provider: String, pub count: usize }
pub(crate) struct ProjectPartition {
    pub communities: Vec<PartitionCommunity>,   // (members.len() desc, members[0] asc)
    pub directed: Vec<DirectedImport>,          // folded per ordered (importer, provider)
    pub file_count: usize,
    pub partition_signature: String,            // sha256 over ordered member_signatures
}
pub(crate) fn build_partition(identity: &ImportIdentity, rows: &[(String, String)]) -> Result<ProjectPartition, PartitionError>;
pub(crate) enum PartitionError { InvalidGraph(GraphInputError), ProviderNotVisible { module: String, file: String } }
```

Algorithm: nodes are `identity.visible_files` sorted; rows dedup into a
`BTreeSet<(source, module)>`; per row skip a non-visible source, skip `None` from
`unique_provider` (external or ambiguous), skip `provider == source`, return
`ProviderNotVisible` for a provider outside the visible set (an invariant violation,
since `providers_for` filters by visibility), else `directed[(source, provider)] += 1`.
Fold undirected as `undirected[(min, max)] += count`; one `AnalyticsNode { id: path,
kind: "file", weight: 1.0 }` per file and one `AnalyticsEdge { kind: "IMPORTS", weight:
count as f64 }` per unordered pair (modularity is scale-invariant). Run `communities()`
once. Oversized pass: `max_size = max(10, file_count / 4)`; every community with
`len > max_size` is re-partitioned once on its induced subgraph at γ=1.0 and replaced by
its children (a single-child result keeps the parent). Low-cohesion pass over the
resulting set: for `n >= 50` with `40 * E < n * (n - 1)` (all `u128`), re-partition once;
discard the split when it yields one group. Children are never re-checked. Sort by
`(size desc, members[0] asc)`; compute `cohesion`, `in_degree`, and signatures.

`labels.rs`: `derive_label(members, in_degree) -> String` keeps Draft 1's rule (size 1
→ the path; deepest proper directory prefix covering at least half the members, ties to
more coverage then lexicographically smallest; else the depth-1 prefix with the most
coverage; else the member with the highest in-degree), `dedupe_labels` appends ` #2`,
` #3` in partition order so the largest community keeps the bare label, and
`label_candidates(community, in_degree) -> Vec<String>` returns up to four distinct
strings: the deterministic label, the top in-degree file stem, the most common
identifier prefix among member stems, and the deepest shared path segment.

Tests in `partition_tests.rs` (reuse the `identity_from` helper from 2.1):
`alias_collapse_folds_into_one_edge`, `external_module_adds_no_edge`,
`ambiguous_provider_adds_no_edge`, `self_import_is_dropped`,
`non_visible_source_row_is_skipped`, `provider_outside_visible_set_is_typed_error`,
`two_cliques_joined_by_bridge_split_by_directory`, `oversized_community_is_split_once`
(one 60-file blob in a 100-file project splits, children are not re-split),
`low_cohesion_community_is_split_once` (a 50-member near-star with 40E < n(n-1)),
`cohesion_threshold_uses_integer_math` (E exactly at the boundary is not split),
`singleton_cohesion_is_one`, `member_signature_matches_graphify_shape` (known vector),
`label_prefers_deepest_majority_prefix`, `label_falls_back_to_top_level_plurality`,
`label_falls_back_to_in_degree_for_root_files`, `label_collision_gets_ordinal_suffix`,
`candidates_are_distinct_and_lead_with_deterministic`,
`partition_is_invariant_to_row_order`, `build_handles_twenty_thousand_rows`.

Verify: `cargo nextest run -p gobby-code -E 'test(partition)'`.

**Acceptance:**

- 2.2.1 - `build_partition` produces one node per visible file and one folded undirected edge per importer/provider pair, excluding external, ambiguous, and self imports. symbol: `build_partition`. file: `crates/gcode/src/communities/partition.rs`.
- 2.2.2 - Oversized and low-cohesion splits apply once each with Graphify's constants in integer math. test: `crates/gcode/src/communities/partition_tests.rs::cohesion_threshold_uses_integer_math`.
- 2.2.3 - Cohesion, in-degree, and the 16-hex member signature are computed per community and the signature matches Graphify's shape. test: `crates/gcode/src/communities/partition_tests.rs::member_signature_matches_graphify_shape`.
- 2.2.4 - Deterministic labels follow the dominant-directory rule with in-degree fallback and ordinal suffixes; candidates are distinct and lead with the deterministic label. symbol: `derive_label`. file: `crates/gcode/src/communities/labels.rs`.
- 2.2.5 - A provider outside the visible set surfaces as `PartitionError::ProviderNotVisible`; kernel input errors surface as `PartitionError::InvalidGraph`. symbol: `PartitionError`. file: `crates/gcode/src/communities/partition.rs`.
- 2.2.6 - The partition is deterministic under row reordering. test: `crates/gcode/src/communities/partition_tests.rs::partition_is_invariant_to_row_order`.

### 2.3 Remap ids against the stored partition and carry labels forward [category: code] (depends: 2.2)
`kind: deliverable`

Targets:
- `crates/gcode/src/communities.rs`
- `crates/gcode/src/communities/remap.rs`
- `crates/gcode/src/communities/remap_tests.rs`

Research context: Graphify's `remap_communities_to_previous` (`cluster.py:361-409`)
sorts all `(new, old)` pairs by `(-overlap, old_cid, new_cid)`, matches greedily one to
one, and gives unmatched new communities the lowest integers not held by matched old
ids, so a retired id is reissued to an unrelated community on the next run. Ask evidence
records are content-hashed and replay-verified (Ask evidence module, lines 677-744), so
an id that can change meaning cannot be cited; hence the watermark. Rejected: pure
overlap ordering (a large community swallowing a small one's members would steal its
id; Jaccard prefers the better-matching pair), content-hash ids (every edit would rename
the community).

```rust
pub(crate) struct PriorCommunity {   // one stored row, minimal fields for remap
    pub community_id: i32, pub members: Vec<String>, pub member_signature: String,
    pub label: String, pub label_deterministic: String, pub label_source: LabelSource,
    pub label_confidence: Option<f64>, pub label_model: Option<String>,
    pub labeled_signature: Option<String>, pub labeled_at: Option<DateTime<Utc>>,
}
pub(crate) struct AssignedCommunity { pub community_id: i32, pub matched_prior: Option<i32>, pub partition_index: usize, pub label: LabelCarry }
pub(crate) fn assign_ids(partition: &ProjectPartition, prior: &[PriorCommunity], watermark: i32) -> (Vec<AssignedCommunity>, i32 /* new watermark */);
```

Candidates are every `(new, old)` pair with overlap ≥ 1 and `(overlap, union)`; sort by
Jaccard desc using integer cross-multiplication (`o1*u2` vs `o2*u1`), then overlap desc,
then old id asc, then new index asc; take pairs greedily while both sides are unmatched.
Unmatched new communities, in partition order, receive `max(watermark, max old id) + 1,
+2, …`; the returned watermark is the last id issued (or the input when none). Label
carry-forward per new community: unmatched → deterministic label and candidates from
2.2; matched with equal signature → carry every label field; matched with a changed
signature and prior source `deterministic` → recompute; matched with a changed signature
and prior source `model` → carry the model label with its `labeled_signature`, which the
readers interpret as `label_stale`. `label_candidates` are always recomputed.

Tests in `remap_tests.rs`: `unchanged_partition_keeps_every_id`,
`split_keeps_id_on_larger_child_and_issues_fresh_id`,
`merge_keeps_id_of_larger_parent`, `jaccard_beats_raw_overlap_on_swallow_case`,
`retired_ids_are_never_reissued` (delete a community, add an unrelated one, id is above
the watermark), `renamed_files_keep_id_and_flip_signature`,
`model_label_carries_with_stale_signature`, `deterministic_label_recomputes_on_change`,
`watermark_is_monotone_across_runs`.

Verify: `cargo nextest run -p gobby-code -E 'test(remap)'`.

**Acceptance:**

- 2.3.1 - `assign_ids` matches greedily by Jaccard, overlap, old id, new index and keeps an id on the larger child of a split and the larger parent of a merge. test: `crates/gcode/src/communities/remap_tests.rs::split_keeps_id_on_larger_child_and_issues_fresh_id`.
- 2.3.2 - Unmatched communities take ids above the watermark and a retired id is never reissued. test: `crates/gcode/src/communities/remap_tests.rs::retired_ids_are_never_reissued`.
- 2.3.3 - Label fields carry forward by the four-way rule and a model label with a changed signature is carried as stale. symbol: `assign_ids`. file: `crates/gcode/src/communities/remap.rs`.

## P3: Persistence
`kind: framing`

**Goal**: `code_communities` exists with its carriers, `gcode index` refreshes it inside
the checkout fence, and every read path has one API.

### 3.1 Migration 442: `code_communities`, the id watermark, and every derived carrier [category: code]
`kind: deliverable`

Targets:
- `crates/gcore/assets/schema/migrations/442_add_code_communities.sql`
- `crates/gcore/assets/schema/baseline.sql`
- `crates/gcore/src/schema/assets.rs::*` — scope-reason: append the 442 entry to MIGRATIONS
- `crates/gcore/assets/schema/catalog.manifest.json::*` — scope-reason: regenerate the catalog manifest for migration 442
- `crates/gcore/src/grant/bundle.rs::*` — scope-reason: refresh the embedded schema identity goldens
- `crates/gcore/tests/schema_contract.rs::*` — scope-reason: pin the new latest migration and table set
- `crates/gdaemon/tests/cli_contract.rs::*` — scope-reason: update the latest_version assertion from 441 to 442
- `src/gobby/storage/schema_expected_identity.json::*` — scope-reason: regenerate the expected schema identity for latest version 442
- `tests/runtime_grants/golden/brokered_datastores.json::*` — scope-reason: regenerate the signed runtime-grant golden for the new schema identity
- `tests/runtime_grants/golden/direct_datastores.json::*` — scope-reason: regenerate the signed runtime-grant golden for the new schema identity
- `tests/runtime_grants/golden/old_client_new_grant.json::*` — scope-reason: regenerate the signed runtime-grant golden for the new schema identity
- `tests/runtime_grants/golden/payload_skew_unknown_field.json::*` — scope-reason: regenerate the signed runtime-grant golden for the new schema identity
- `tests/runtime_grants/golden/unavailable_datastores.json::*` — scope-reason: regenerate the signed runtime-grant golden for the new schema identity
- `crates/gcore/src/schema/runner_tests.rs::*` — scope-reason: add code_communities to GCODE_RLS_TABLES and the machine-scoped predicate list
- `crates/gcode/security/managed_postgres_privileges.json::*` — scope-reason: declare the code_communities relation grant and the new SQL source inventory entry
- `crates/gcode/src/schema.rs::*` — scope-reason: add the code_communities table contract and required-table entry and the watermark column on code_indexed_project_states

Research context: `BASELINE_VERSION` is 420 (`crates/gcore/src/schema/assets.rs:4`);
migrations run through 441 (`441_add_coordination_reply_waits.sql`); `MIGRATIONS` is
`crates/gcore/src/schema/assets.rs:24`. `code_indexed_project_states`
(`baseline.sql:2197-2210`) is keyed `(machine_id, project_id)` and carries the
machine-scoped six-policy RLS block this table mirrors (`baseline.sql:5852-5970`:
`gobby_daemon_runtime_access`, `gobby_migration_owner_access`, and
`gobby_gcode_project_{read,insert,update,delete}` with
`machine_id = gobby_agent_auth.current_machine_id()` and the overlay `COALESCE`
predicate), plus the two GRANTs at :6216-6218. `upsert_project_stats`
(index api module, lines 383-475) names its `ON CONFLICT` column list
explicitly, so the new column survives it. `GCODE_RLS_TABLES` is `[&str; 11]` at
`runner_tests.rs:64`. The privilege manifest's `relations[]` entries look like
`{"relation": "code_imports", "operations": ["SELECT","INSERT","UPDATE","DELETE"], "scope_column": "project_id"}`;
mirror the `code_indexed_project_states` entry. `schema_expected_identity.json` records
`latest_version: 441` and hashes; `crates/gdaemon/tests/cli_contract.rs:58` asserts 441.
No derived table exists in the code index today; the only model-derived column is
`code_symbols.summary`. Rejected: an identity sequence on `community_id` (ids are
per-project and issued by the remap watermark); a foreign key from `members` to
`code_indexed_files` (content-versioned keys would cascade on every edit); a
`code_community_members` child table (the read paths always want the whole community
and a GIN index answers membership).

```sql
ALTER TABLE code_indexed_project_states
    ADD COLUMN community_id_watermark integer NOT NULL DEFAULT 0;

CREATE TABLE code_communities (
    machine_id uuid NOT NULL REFERENCES machines(id),
    project_id uuid NOT NULL REFERENCES code_indexed_projects(id) ON DELETE CASCADE,
    community_id integer NOT NULL,
    member_count integer NOT NULL,
    members text[] NOT NULL,
    representatives text[] NOT NULL,        -- top 5 by in-degree, ties by path
    internal_edges integer NOT NULL,
    cohesion double precision NOT NULL,
    boundary jsonb NOT NULL,                -- [{"other_community_id": int, "import_count": int}]
    member_signature text NOT NULL,
    label_deterministic text NOT NULL,
    label text NOT NULL,
    label_source text NOT NULL,
    label_confidence double precision,
    label_model text,
    label_candidates text[] NOT NULL,
    labeled_signature text,
    labeled_at timestamp with time zone,
    label_attempted_at timestamp with time zone,
    refreshed_at timestamp with time zone NOT NULL DEFAULT now(),
    PRIMARY KEY (machine_id, project_id, community_id),
    CHECK (label_source IN ('deterministic', 'model')),
    CHECK (member_count = cardinality(members) AND member_count > 0),
    CHECK (member_signature ~ '^[0-9a-f]{16}$'),
    CHECK (cohesion >= 0 AND cohesion <= 1),
    CHECK (jsonb_typeof(boundary) = 'array'),
    CHECK (label_source = 'deterministic' OR (labeled_signature IS NOT NULL AND label_model IS NOT NULL))
);
CREATE INDEX idx_cc_project_size ON code_communities (machine_id, project_id, member_count DESC, community_id);
CREATE INDEX idx_cc_members ON code_communities USING gin (members);
CREATE INDEX idx_cc_label_queue ON code_communities (machine_id, project_id)
    WHERE labeled_signature IS DISTINCT FROM member_signature;
```

Membership queries use `members @> ARRAY[%s::text]`, never `= ANY`, so the GIN index
serves them. Mirror the same DDL, RLS block, and GRANTs into `baseline.sql`.

Carrier regeneration, all in this commit: `catalog.manifest.json` via
`POSTGRES_TEST_DSN=<test dsn> UPDATE_GCORE_SCHEMA_MANIFEST=1 cargo test -p gobby-core --features postgres --test catalog_manifest_freshness`;
`schema_expected_identity.json` via
`uv run python scripts/generate_schema_expected_identity.py --gdaemon target/release/gdaemon`
after `cargo build --release -p gobby-daemon`; the five signed golden files
and `crates/gcore/src/grant/bundle.rs` through their existing regeneration paths;
`schema_contract.rs`, `cli_contract.rs`, `runner_tests.rs` by hand. In gcode's
`schema.rs` add the `code_communities` `TABLE_CONTRACTS` entry, the `REQUIRED_TABLES`
entry, and `community_id_watermark` on the `code_indexed_project_states` contract.

Live: `uv run gobby restart` (plans and applies 442), then `uv run gobby cutover` for the
coherent set, after a `global` announcement.

Granularity: sixteen target files, three of them hand-maintained Rust. A schema change
and its carriers cannot land partially (every carrier test fails until all agree), so
this stays one leaf by construction.

Verify: `cargo nextest run -p gobby-core --features postgres`, `cargo nextest run -p gobby-daemon --test cli_contract`,
`cargo nextest run -p gobby-code -E 'test(schema)'`,
`DATABASE_URL=postgresql://gobby_test:gobby_test@127.0.0.1:60892/gobby_test GOBBY_TEST_PROTECT=1 uv run pytest tests/runtime_grants tests/storage/test_schema_identity.py -q`
(or the nearest existing schema-identity test module).

**Acceptance:**

- 3.1.1 - Migration 442 creates `code_communities` with the constraints, indexes, machine-scoped RLS policies, and GRANTs above, and adds `community_id_watermark` to `code_indexed_project_states`. file: `crates/gcore/assets/schema/migrations/442_add_code_communities.sql`.
- 3.1.2 - `baseline.sql`, `catalog.manifest.json`, `assets.rs::MIGRATIONS`, `crates/gcore/src/grant/bundle.rs`, `schema_contract.rs`, `gdaemon` `cli_contract.rs`, `schema_expected_identity.json`, and the five goldens agree on latest version 442. file: `src/gobby/storage/schema_expected_identity.json`.
- 3.1.3 - `GCODE_RLS_TABLES` lists twelve tables and the runner tests assert the machine-scoped predicates on `code_communities`. file: `crates/gcore/src/schema/runner_tests.rs`.
- 3.1.4 - The privilege manifest grants `code_communities` to the gcode capability with the same scope declaration as `code_indexed_project_states`, and gcode's `schema.rs` contracts include the table and the watermark column. file: `crates/gcode/security/managed_postgres_privileges.json`.
- 3.1.5 - `uv run gobby restart` applies 442 on the live hub and `gdaemon schema plan` reports nothing pending afterwards. behavior: "442" in `src/gobby/storage/schema_expected_identity.json`.

### 3.2 Persist the partition at index time and expose the read API [category: code] (depends: 2.3, 3.1)
`kind: deliverable`

Targets:
- `crates/gcode/src/communities.rs`
- `crates/gcode/src/communities/refresh_tests.rs`
- `crates/gcode/src/db/communities.rs`
- `crates/gcode/src/db/mod.rs::*` — scope-reason: declare and re-export the communities query module
- `crates/gcode/src/index/indexer/lifecycle.rs::*` — scope-reason: add refresh_communities beside refresh_project_stats and attach_projection_sync
- `crates/gcode/src/index/indexer/pipeline.rs::*` — scope-reason: call refresh_communities at both full and incremental completion sites
- `crates/gcode/src/index/indexer/overlay.rs::*` — scope-reason: call refresh_communities at the overlay completion site under the overlay project id
- `crates/gcode/src/index/indexer/types.rs::*` — scope-reason: add the CommunityRefreshFailed degradation and the optional communities report on IndexOutcome
- `crates/gcode/src/contract.rs::*` — scope-reason: add communities to the index command's JSON output keys
- `crates/gcode/contract/gcode.contract.json::*` — scope-reason: regenerate the pinned contract snapshot from gcode contract
- `tests/contracts/gcode.contract.json::*` — scope-reason: regenerate the vendored contract snapshot to match the crate copy
- `crates/gcode/security/managed_postgres_privileges.json::*` — scope-reason: declare the code_communities relation grant and the new SQL source inventory entry

Research context: `refresh_project_stats` (`lifecycle.rs:54-91`, `pub(super)`) runs
inside the checkout fence and `attach_projection_sync` (`lifecycle.rs:15-26`) follows
it at `pipeline.rs:241-253`, `pipeline.rs:420-432`, and `overlay.rs:325-337` (line
hints from 2026-09-19; anchor on the two function names). `IndexDegradation`
(`types.rs:50-63`) is an enum of struct variants (`FileIndexError`,
`ProjectionSyncSkipped`, `ProjectionCleanupFailed`); `IndexOutcome` (:66-93) already
uses `#[serde(default, skip_serializing_if = "Option::is_none")]` for optional blocks
(`projection_sync`, `overlay`). `crates/gcode/src/db/mod.rs` (35 lines) declares `mod queries;` and
re-exports with `pub use`. `read_active_imports` is machine-scoped through
`code_indexed_file_states`, so the partition is per machine, matching the table key.
The privilege manifest's `source_inventory[]` lists every gcode file that issues SQL
with per-call-kind counts (`{"path": …, "calls": {"query": 1, …}, "classification": …}`)
and a test keeps it honest, so the new `crates/gcode/src/db/communities.rs` needs an entry. Overlay runs
(`overlay.rs`) index a worktree under an overlay project id whose visible tree shadows
the parent's; `visibility::visible_tree` already applies that rule. Rejected: refreshing
only when the daemon asks (the hook per-file path would leave stale rows visible to the
very next `graph view`); a partial refresh of touched communities (remap needs the full
partition, and Leiden over ~7,000 nodes is sub-second in Rust).

`crates/gcode/src/db/communities.rs` (SQL only): `read_project_communities(conn, machine_id, project_id) -> Vec<StoredCommunity>`,
`read_watermark_for_update(conn, …)`, `replace_project_communities(conn, machine_id,
project_id, rows, watermark)` executing one transaction: `SELECT community_id_watermark
… FOR UPDATE`, `DELETE FROM code_communities WHERE machine_id = %s AND project_id = %s`,
`INSERT` each row, `UPDATE code_indexed_project_states SET community_id_watermark`,
commit. This is the one legitimate multi-statement transaction in gcode: readers never
observe a half-replaced partition.

`communities.rs`: `StoredCommunity` (every column plus `label_stale: bool` computed as
`label_source == Model && labeled_signature != Some(member_signature)`),
`refresh_project_communities(conn, ctx) -> anyhow::Result<CommunityRefreshReport>`
(load imports through 2.1, build through 2.2, read prior rows, remap through 2.3, skip
the write when the stored `partition_signature` equals the new one, else replace;
`representatives` are the top five members by in-degree with ties by path; `boundary`
folds `directed` into per-pair import counts between distinct communities), and
`read_for_context(conn, ctx) -> Vec<StoredCommunity>` (overlay project first, parent
fallback when the overlay has no rows). `CommunityRefreshReport { communities, changed,
new_ids, retired_ids, skipped_unchanged }`.

`lifecycle::refresh_communities(conn, ctx, outcome)` calls
`refresh_project_communities`, sets `outcome.communities = Some(report)` on success, and
pushes `IndexDegradation::CommunityRefreshFailed { message }` on any error. It is called
after `refresh_project_stats` and before `attach_projection_sync` at the three sites,
including hook-triggered single-file runs. `contract.rs`: the `index` command's JSON
output keys gain `communities`; regenerate both pinned JSON copies (the version stays 10
until 4.3).

Tests in `refresh_tests.rs` against the Postgres test fixture the indexer tests already
use: `refresh_writes_rows_and_watermark`, `unchanged_partition_skips_write`
(`refreshed_at` untouched), `refresh_failure_degrades_index_outcome`,
`read_for_context_prefers_overlay_rows`, `replace_is_atomic_under_concurrent_read`
(a reader inside the replace sees either the old or the new set).

Verify: `cargo nextest run -p gobby-code -E 'test(communities) | test(index)'`,
`cargo nextest run -p gobby-code --test contract`.

**Acceptance:**

- 3.2.1 - `gcode index` (full, incremental, hook, and overlay runs) refreshes `code_communities` after project stats and before projection sync, replacing rows in one transaction and advancing the watermark. symbol: `refresh_project_communities`. file: `crates/gcode/src/communities.rs`.
- 3.2.2 - An unchanged partition signature skips the write. test: `crates/gcode/src/communities/refresh_tests.rs::unchanged_partition_skips_write`.
- 3.2.3 - A refresh failure degrades the outcome with `CommunityRefreshFailed` and never fails the index run. test: `crates/gcode/src/communities/refresh_tests.rs::refresh_failure_degrades_index_outcome`.
- 3.2.4 - `read_for_context` returns overlay rows first and parent rows as fallback, with `label_stale` derived from the signatures. symbol: `read_for_context`. file: `crates/gcode/src/communities.rs`.
- 3.2.5 - `IndexOutcome.communities` is additive and absent when `None`; the `index` contract keys and both pinned JSON copies include it. file: `crates/gcode/contract/gcode.contract.json`.
- 3.2.6 - The privilege manifest's source inventory lists `crates/gcode/src/db/communities.rs`. file: `crates/gcode/security/managed_postgres_privileges.json`.

## P4: Surfaces at contract v11
`kind: framing`

**Goal**: MCG nodes carry stored labels, `--view=communities` lists and details
subsystems, and `graph report` gains a communities section, all reading stored rows only.

### 4.1 Extract the graph-view CLI types into `crates/gcode/src/cli/graph_view.rs` [category: refactor]
`kind: deliverable`

Targets:
- `crates/gcode/src/cli.rs::*` — scope-reason: move GraphViewKind, GraphViewSeed, GraphViewArgs, GraphViewArgsRaw, and their impls out and declare the graph_view submodule
- `crates/gcode/src/cli/graph_view.rs`

Research context: `cli.rs` is 827 lines. `GraphViewKind` (:515-520), `GraphViewSeed`
(:548-552), `GraphViewArgs` (:555-561), `GraphViewArgsRaw` (:576-602, private), and the
`FromArgMatches`/`Args` impls through about :686 are a self-contained block;
`positive_usize` (:727) and `bounded_positive_usize` (:735) are private helpers the
block calls. `cli.rs` is file-form with a sibling `cli/` directory already holding
`ask.rs` (120 lines) as the precedent for a split-out command surface. 4.3 adds roughly
120 lines to this block, which would cross the 850-line decomposition trigger in place.
Rejected: splitting after 4.3 (the lint fires on the growth commit).

Move the block to `crates/gcode/src/cli/graph_view.rs`, re-export the three public
types from `cli.rs` so every consumer keeps its import path, and
make `positive_usize`/`bounded_positive_usize` `pub(super)`. No behavior change;
the existing projection tests pass without edits.

Verify: `cargo nextest run -p gobby-code -E 'test(graph_view) | test(projection)'`, `wc -l crates/gcode/src/cli.rs` under 700.

**Acceptance:**

- 4.1.1 - The graph-view CLI types and impls live in `crates/gcode/src/cli/graph_view.rs` and `cli.rs` is under 700 lines with unchanged public paths. file: `crates/gcode/src/cli/graph_view.rs`.
- 4.1.2 - Every existing projection test passes without edits and the moved types keep their names. behavior: "GraphViewKind" in `crates/gcode/src/cli/graph_view.rs`.

### 4.2 Label MCG nodes from stored rows and extend the view payload shapes [category: code] (depends: 3.2)
`kind: deliverable`

Targets:
- `crates/gcode/src/commands/graph/view/mcg.rs::*` — scope-reason: replace assign_leiden_communities with label_communities reading stored rows and drop the analytics imports
- `crates/gcode/src/commands/graph/view/mcg/fetch.rs::*` — scope-reason: read stored communities in run and pass them to label_communities
- `crates/gcode/src/commands/graph/view/mcg/tests.rs::*` — scope-reason: rewrite the clustering test against stored rows and the missing-partition hint
- `crates/gcode/src/commands/graph/view/render.rs::*` — scope-reason: reshape ViewCommunity, add NodeKind::Community and ViewEdge.count, validate community membership in build_view_payload, label Mermaid nodes and edges, delete analytics_graph_from_payload
- `crates/gcode/src/commands/graph/view/render_tests.rs::*` — scope-reason: cover the new ViewCommunity fields, the Community node kind, edge counts, and payload validation
- `crates/gcode/src/commands/graph/view/tests.rs::*` — scope-reason: remove the analytics_graph_from_payload test

Research context: `assign_leiden_communities` (`mcg.rs:131-173`) builds an
`AnalyticsGraph` from the walk and calls `analyze`; `crates/gcode/src/commands/graph/view/mcg/fetch.rs::run` (:130-193)
calls it after the depth-bounded walk. `ViewCommunity` (`render.rs:105-108`) is
`{ id, nodes }`; `ViewNode.community: Option<String>` (:89-95); `ViewEdge` (:98-102);
`NodeKind` (:13-19) with `key_prefix` (:22) and `NodeKey` constructors (:40-75);
`node_file_for_kind` (:142); `build_view_payload` (:161-222) sorts and dedups nodes and
edges but passes `communities` through unvalidated; `render_mermaid` (:275-324) prints
`Name [community]` for nodes; `analytics_graph_from_payload` (:225-247) is referenced
only by `crates/gcode/src/commands/graph/view/tests.rs` and dies with the analyze path. `render_tests.rs:113-115` loops
over the view kinds and :133-135 matches the relation per kind. Rejected: computing a
partition inline when no rows exist (the decision record forbids inline Leiden; the hint
tells the caller to index).

`mcg.rs`: delete `assign_leiden_communities` and the `analyze`, `AnalyticsGraph`,
`AnalyticsNode`, `AnalyticsEdge`, `weight_for_kind` imports; add

```rust
pub(super) struct LabeledCommunities { pub nodes: Vec<ViewNodeInput>, pub communities: Vec<ViewCommunity>, pub hint: Option<String> }
pub(super) fn label_communities(nodes: Vec<ViewNodeInput>, stored: &[StoredCommunity], identity: &ImportIdentity) -> LabeledCommunities;
```

Per node: `File` looks up the owning stored community by path; `Module` resolves
`identity.unique_provider` then the file lookup; `Symbol`, `External`, `Unresolved`
get `None`. `communities[]` lists only communities intersecting the view, with the
view-scoped node ids and the project-level `size`. Empty `stored` → every node
`community: None` and `hint = Some(MISSING_PARTITION_HINT)` ("No stored import
communities for this project; run `gcode index` to compute them. Node `community` stays
null until then; this view never computes a partition inline."); the existing
graph-availability hint outranks it. `crates/gcode/src/commands/graph/view/mcg/fetch.rs::run` reads rows with
`communities::read_for_context` on its connection.

`render.rs`: `ViewCommunity { id: String, label: String, size: usize, cohesion: f64,
label_source: String, label_stale: bool, nodes: Vec<String> }` where `id` is
`community:<community_id>`, `size` is the project-level member count and `nodes` the
view-scoped ids (doc comment plus a dedicated assertion); `NodeKind::Community` with
`key_prefix` `"community"`, `NodeKey::community(id)`, `node_file_for_kind → None`;
`ViewEdge.count: Option<usize>` with `skip_serializing_if` so other views stay
byte-identical; `build_view_payload` returns an error when `communities[].nodes ⊄ nodes[]`
or a node's `community` names an unlisted community; `render_mermaid` prints
`name [label]` for non-community nodes and `IMPORTS (12)` when `count` is set.

Tests: rewrite `mcg_assigns_leiden_communities_on_scoped_imports` as
`mcg_labels_nodes_from_stored_communities` (file nodes carry the stored label, a
uniquely resolved module inherits its provider's label, an external module has
`community: None`, `communities[].nodes` are view ids while `size` is project-level, the
label appears in Mermaid) and add `mcg_without_stored_partition_hints_and_leaves_null`.
`render_tests.rs`: `payload_rejects_unknown_community_member`,
`community_node_has_null_file`, `edge_count_renders_in_mermaid`, and the `Communities`
arm in the view-kind loop (relation `IMPORTS`).

Verify: `cargo nextest run -p gobby-code -E 'test(mcg) | test(render) | test(view)'`.

**Acceptance:**

- 4.2.1 - MCG file and uniquely resolved module nodes carry the stored community label; external and ambiguous modules carry none; no `graph_analytics` import remains in `mcg.rs`. symbol: `label_communities`. file: `crates/gcode/src/commands/graph/view/mcg.rs`.
- 4.2.2 - With no stored rows every node's `community` is null and the payload carries the missing-partition hint. test: `crates/gcode/src/commands/graph/view/mcg/tests.rs::mcg_without_stored_partition_hints_and_leaves_null`.
- 4.2.3 - `ViewCommunity` carries id, label, size, cohesion, label_source, label_stale, and view-scoped nodes; `build_view_payload` rejects membership outside `nodes[]`. test: `crates/gcode/src/commands/graph/view/render_tests.rs::payload_rejects_unknown_community_member`.
- 4.2.4 - `NodeKind::Community` exists with a null file and the `community:` key prefix, `ViewEdge.count` is optional, and `analytics_graph_from_payload` is deleted. symbol: `NodeKind`. file: `crates/gcode/src/commands/graph/view/render.rs`.

### 4.3 Add the communities view kind, `--min-size`, `--community`, the view module, and contract v11 [category: code] (depends: 3.2, 4.1, 4.2)
`kind: deliverable`

Targets:
- `crates/gcode/src/cli/graph_view.rs`
- `crates/gcode/src/dispatch.rs::*` — scope-reason: match the optional file seed when rewriting the seed path
- `crates/gcode/src/commands/graph/view/mod.rs::*` — scope-reason: declare the communities submodule and route the seedless and detail forms in run
- `crates/gcode/src/commands/graph/view/communities.rs`
- `crates/gcode/src/commands/graph/view/communities/tests.rs`
- `crates/gcode/src/commands/graph/view/tests.rs::*` — scope-reason: cover routing of the optional seed
- `crates/gcode/src/cli/tests/projection.rs::*` — scope-reason: cover communities parsing and rejections
- `crates/gcode/src/contract.rs::*` — scope-reason: bump contract_version to 11, extend the --view values, add --min-size and --community
- `crates/gcode/contract/gcode.contract.json::*` — scope-reason: regenerate the pinned contract snapshot from gcode contract
- `tests/contracts/gcode.contract.json::*` — scope-reason: regenerate the vendored contract snapshot to match the crate copy
- `crates/gcode/tests/contract.rs::*` — scope-reason: pin version 11, rename the version test, assert the new view value and flags
- `tests/test_cli_contracts.py::*` — scope-reason: pin contract version 11
- `docs/contracts/gcode-cli.md`
- `crates/gcode/Cargo.toml`
- `Cargo.lock`

Research context: after 4.1 the CLI types live in `crates/gcode/src/cli/graph_view.rs`. `crates/gcode/src/commands/graph/view/mod.rs::run`
(:384-402) matches `(args.view, &args.seed)` with a `_ => bail!` arm, which hides a
forgotten `Some(...)` case, hence the routing test. `dispatch.rs:570` rewrites
`GraphViewSeed::File(file)` to a project-relative path. `contract()`
(`contract.rs:9-693`) holds the `graph view` flag contracts and `contract_version: 10`
at :12; `graph_view_output_keys` (`crates/gcode/src/contract/schema.rs:293-308`) is unchanged because
`communities[]` already exists as a key. `crates/gcode/tests/contract.rs` pins 10 at
:87 and :151 and names the version in
`contract_is_version_ten_with_ask_and_evidence_without_codewiki`;
`tests/test_cli_contracts.py:175` pins 10 and
`test_vendored_cli_contract_matches_real_cli[gcode]` compares the installed binary with
`tests/contracts/gcode.contract.json` (memory `833f0b1a`). `docs/contracts/gcode-cli.md:10`
is the Version 10 paragraph. `crates/gcode/Cargo.toml:3` is `version = "1.8.0"`;
`MIN_GCODE_PRUNE_BUDGET_VERSION = "1.8.0"` in the gcode gateway module (line 29)
is a floor and stays. Python needs no change for the CLI: the code-navigation hook
classifies on the `graph view` token pair and the tool-chat authorizer authorizes on the
`view` subcommand. Rejected: token paging for the detail view (graph views emit complete
JSON and report truncation, per the contract); a separate `gcode communities`
subcommand (one more contract surface for the same payload family).

`crates/gcode/src/cli/graph_view.rs`: `GraphViewKind::Communities` (`#[value(name = "communities")]`,
`as_str` `"communities"`, `default_depth` 0, `allows_row_limits` false);
`GraphViewSeed::Community(String)`; `GraphViewArgs.seed: Option<GraphViewSeed>`;
`min_size: Option<usize>` with `effective_min_size()` defaulting to 2 (no clap default,
so misuse stays detectable). `GraphViewArgsRaw`: the `seed` `ArgGroup` becomes
`.required(false)` and gains `--community <ID|LABEL>`; `--min-size N` parsed with
`positive_usize`. `from_arg_matches_mut`, in order: row limits on a view without
`allows_row_limits` reject with `ArgumentConflict` naming the view; `--depth` with
communities rejects ("--depth cannot be used with --view=communities"); `--min-size`
without communities rejects; four-way seed arity check; selector compatibility:
`Communities` accepts `Community(_)` or `None`, every other view requires its existing
seed kind and keeps `MissingRequiredArgument` when absent. `dispatch.rs`:
`if let Some(GraphViewSeed::File(file)) = &mut args.seed`; `--community` is never
path-resolved. `crates/gcode/src/commands/graph/view/mod.rs::run` matches `(args.view, args.seed.as_ref())` and routes
`(Communities, None) → communities::run_list` and
`(Communities, Some(Community(sel))) → communities::run_detail`.

New `crates/gcode/src/commands/graph/view/communities.rs` (about 300 lines, reads only through
`communities::read_for_context`, skips `CodewikiFacts`). List: rows with
`size >= effective_min_size()`; one `NodeKind::Community` node per row named by its
label; inter-community `IMPORTS` edges from stored `boundary` with `count`;
`communities[]` one entry per listed row with full `file:` member ids;
`seed = { id: project_id, name: project_root, kind: "project", file: None }`; the
missing-partition hint from 4.2 when there are no rows. Detail: the selector is an
integer id, else a case-insensitive exact `label`, else an exact `label_deterministic`,
else a unique case-insensitive substring; ambiguity is a typed exit-2 error listing up
to five matches. Nodes: the community, up to 50 members as `File` nodes
(representatives first, then path asc), up to 12 neighbors by `import_count` desc;
edges `CONTAINS` to members and `IMPORTS` with counts to neighbors;
`outgoing_truncated`/`incoming_truncated` carry the caps. Mermaid renders through
`render_mermaid` unchanged; text keeps `print_view`.

Contract: `contract.rs` `--view` value name `fcg|mcg|class-hierarchy|communities`,
`allowed_values` gains `"communities"`, `FlagContract::value("--min-size", "N")` and
`FlagContract::value("--community", "ID|LABEL")` after `--outgoing-limit`, summary
"Render a scoped fcg, mcg, or class-hierarchy graph view, or project-wide import
communities", `contract_version: 11`. Regenerate both pinned JSON copies; rename the
test to `contract_is_version_eleven_with_project_import_communities` and pin 11 at both
assertions; pin 11 in `tests/test_cli_contracts.py`. `docs/contracts/gcode-cli.md`: a
"Version 11" paragraph (persisted per-machine communities, seedless view, `--min-size`,
`--community`, `communities[]` shape with `label`, `size`, `cohesion`, `label_source`,
`label_stale`, `community:<id>` node ids, `edges[].count`, the `index` `communities`
report, and the `communities` evidence operation named ahead of 5.1) plus the
`graph view` bullet. Bump `crates/gcode/Cargo.toml` to 1.9.0 and refresh `Cargo.lock`.

Tests: `crates/gcode/src/cli/tests/projection.rs` — `--view communities` parses with no seed, with
`--min-size 5`, and with `--community memory`; rejects `--file`, `--symbol`, `--depth`,
`--incoming-limit`, `--min-size` with `mcg`, `--min-size 0`; defaults
`effective_min_size() == 2`; mcg/fcg/class-hierarchy still require a seed.
`crates/gcode/src/commands/graph/view/tests.rs` — `run_routes_every_view_and_seed_combination` (table-driven over the
match, including `(Communities, None)`). `crates/gcode/src/commands/graph/view/communities/tests.rs` —
`min_size_hides_singletons_by_default`, `list_edges_are_between_listed_communities_only`,
`detail_resolves_id_label_and_unique_substring`, `detail_ambiguous_substring_is_typed_error`,
`detail_truncates_members_and_neighbors_with_flags`, `list_without_rows_hints`,
`mermaid_validates_for_list_and_detail`.

Granularity: fifteen target files, three of them hand-maintained Rust besides the new
view module. A contract bump cannot land partially: the five pins, the flags, and the
view they describe must agree in one commit, so this stays one leaf.

Verify: `cargo nextest run -p gobby-code`, `cargo nextest run -p gobby-code --test contract`,
`cargo clippy -p gobby-code`, `cargo fmt -p gobby-code -- --check`,
`DATABASE_URL=postgresql://gobby_test:gobby_test@127.0.0.1:60892/gobby_test GOBBY_TEST_PROTECT=1 uv run pytest tests/test_cli_contracts.py -q`
after the coherent-set cutover.

**Acceptance:**

- 4.3.1 - `--view=communities` parses without a seed, accepts `--min-size` and `--community`, and rejects other seeds, `--depth`, and row limits with typed clap conflicts. symbol: `GraphViewArgs`. file: `crates/gcode/src/cli/graph_view.rs`.
- 4.3.2 - The list view emits one node per listed community, inter-community `IMPORTS` edges with counts, full membership in `communities[]`, and valid Mermaid, reading stored rows only. symbol: `run_list`. file: `crates/gcode/src/commands/graph/view/communities.rs`.
- 4.3.3 - The detail view resolves id, exact label, deterministic label, or unique substring, caps members at 50 and neighbors at 12 with truncation flags, and reports ambiguity as a typed error. test: `crates/gcode/src/commands/graph/view/communities/tests.rs::detail_resolves_id_label_and_unique_substring`.
- 4.3.4 - Routing covers every view and seed combination including the seedless form. test: `crates/gcode/src/commands/graph/view/tests.rs::run_routes_every_view_and_seed_combination`.
- 4.3.5 - The contract advertises `communities`, `--min-size`, and `--community` at version 11 in `contract.rs`, both pinned JSON copies, the Rust and Python pins, and `docs/contracts/gcode-cli.md`. file: `tests/contracts/gcode.contract.json`.
- 4.3.6 - The gcode crate version is 1.9.0. behavior: "1.9.0" in `crates/gcode/Cargo.toml`.

### 4.4 Add the `## Import communities` section to `gcode graph report` [category: code] (depends: 1.2, 4.3)
`kind: deliverable`

Targets:
- `crates/gcode/src/graph/report/types.rs::*` — scope-reason: add GraphReportCommunities and GraphReportCommunity to ProjectGraphReport
- `crates/gcode/src/graph/report/loading.rs::*` — scope-reason: load stored communities as an optional report input with a degradation when absent
- `crates/gcode/src/graph/report/render.rs::*` — scope-reason: render the Import communities Markdown section after the target sections
- `crates/gcode/src/graph/report/tests.rs::*` — scope-reason: cover the section, the thin-community floor, and the degradation
- `crates/gcode/src/contract/schema.rs::*` — scope-reason: add communities to graph_report_keys
- `crates/gcode/contract/gcode.contract.json::*` — scope-reason: regenerate the pinned contract snapshot from gcode contract
- `tests/contracts/gcode.contract.json::*` — scope-reason: regenerate the vendored contract snapshot to match the crate copy

Research context: `ProjectGraphReport` (`crates/gcode/src/graph/report/types.rs:54-69`), `ReportDegradation`
(:152-156, `{ input, required, … }`), and the report module directory are the
surface; `graph_report_keys` (`crates/gcode/src/contract/schema.rs:336-338`) lists the top-level keys.
Graphify's report prints a community summary count, hub navigation, and per-community
`Cohesion: X.XX` with `min_community_size` 3 (`report.py:141-276`) and undercounted its
own sections in the bakeoff (`GRAPH_REPORT.md` C1 listed 78 shown + 32 thin of 116 while
38 ids had no section). Rejected: running Leiden inside the report (read-only surface);
a separate `gcode communities report` command.

`GraphReportCommunities { total: usize, thin_count: usize, top: Vec<GraphReportCommunity> }`
with `GraphReportCommunity { community_id, label, label_source, label_stale, size, cohesion }`,
`REPORT_COMMUNITY_MIN_SIZE = 3`, `top` truncated to `--top-n`, `total` and `thin_count`
always counted over every row so the numbers reconcile. Loaded before the FalkorDB block
through `communities::read_for_context` as an optional input; absence pushes
`ReportDegradation { input: "code_communities", required: false }`. Markdown: `## Import
communities` after the target sections with `N communities (M thin, below 3 members)`
and a table of `id | label | size | cohesion | source`, stale labels marked. JSON: the
`communities` key on `ProjectGraphReport`; `graph_report_keys()` gains it; regenerate
both pinned JSON copies.

Tests in `crates/gcode/src/graph/report/tests.rs`: `report_communities_section_counts_reconcile`,
`report_without_communities_degrades_optional_input`, `report_marks_stale_labels`.

Verify: `cargo nextest run -p gobby-code -E 'test(report)'`, `cargo nextest run -p gobby-code --test contract`.

**Acceptance:**

- 4.4.1 - `gcode graph report` includes an `Import communities` section and JSON key whose total and thin counts reconcile with the stored rows. test: `crates/gcode/src/graph/report/tests.rs::report_communities_section_counts_reconcile`.
- 4.4.2 - A project without stored communities degrades the optional input instead of failing the report. test: `crates/gcode/src/graph/report/tests.rs::report_without_communities_degrades_optional_input`.
- 4.4.3 - `graph_report_keys()` and both pinned JSON copies list `communities`. file: `crates/gcode/src/contract/schema.rs`.

## P5: Evidence operation
`kind: framing`

**Goal**: `gcode evidence` answers `communities` requests inside schema v1, and Ask's
Python side admits, validates, and describes the operation.

### 5.1 Add `EvidenceOperation::Communities` and `EvidenceItem::Community` to gcode evidence [category: code] (depends: 4.4)
`kind: deliverable`

Targets:
- `crates/gcode/src/evidence/contracts.rs::*` — scope-reason: add the Communities operation, CommunitiesSelector, CommunityEvidence, and its canonical_key arm
- `crates/gcode/src/evidence/mod.rs::*` — scope-reason: add project_communities to EvidenceFacts, implement it for CodewikiFacts, and dispatch the operation
- `crates/gcode/src/evidence/communities.rs`
- `crates/gcode/src/evidence/tests.rs::*` — scope-reason: extend FakeFacts and cover list, detail, empty, unavailable, and truncation outcomes
- `crates/gcode/src/codewiki_facts/mod.rs::*` — scope-reason: declare the communities facts module
- `crates/gcode/src/codewiki_facts/communities.rs`
- `crates/gcode/src/commands/evidence.rs::*` — scope-reason: accept the new operation where the command matches on it
- `crates/gcode/tests/evidence.rs::*` — scope-reason: add an end-to-end communities request through --request-json
- `crates/gcode/src/contract/schema.rs::*` — scope-reason: add the community item keys to evidence_keys
- `crates/gcode/contract/gcode.contract.json::*` — scope-reason: regenerate the pinned contract snapshot from gcode contract
- `tests/contracts/gcode.contract.json::*` — scope-reason: regenerate the vendored contract snapshot to match the crate copy
- `crates/gcode/tests/contract.rs::*` — scope-reason: assert the communities operation and item keys in the evidence contract test

Research context: `EVIDENCE_SCHEMA_VERSION` is 1 (`crates/gcode/src/evidence/contracts.rs:3`);
`EvidenceOperation` (:99-103) is a three-variant `deny_unknown_fields` tagged enum;
`GraphQuery` (:180-187) is `{Callers, Callees, Usages, Imports, DirectedPath, ScopedView}`;
`EvidenceItem` (:223-227) with `canonical_key` (:238); `SourceEvidence` (:268-282)
carries file, byte range, and excerpt, which a community does not have. `EvidenceFacts`
(`crates/gcode/src/evidence/mod.rs:185-200`) is implemented for `CodewikiFacts` at :202 and for
`FakeFacts` in `crates/gcode/src/evidence/tests.rs:161`; `CodewikiFacts` lives in
`crates/gcode/src/codewiki_facts/mod.rs:52-55` with one module per fact domain as the pattern. `evidence_keys` (`crates/gcode/src/contract/schema.rs:201-287`) lists
item keys. Graphify's MCP `get_community` takes `community_id` and `token_budget`
(`serve.py:1186-1209`); the evidence continuation model already provides `max_bytes`
paging bound to the request fingerprint. Rejected: a `CommunityCitation` (a community is
orientation, not a citable byte range); a new schema version (variants are additive and
Python's discriminated union rejects unknown items already).

Contract additions:

```rust
EvidenceOperation::Communities { communities: CommunitiesSelector }
pub struct CommunitiesSelector {   // deny_unknown_fields
    pub community_id: Option<i32>, pub label: Option<String>, pub path: Option<String>, // at most one
    pub min_size: Option<usize>,   // default 2
    pub limit: Option<usize>,
    pub max_members: Option<usize>, // default 50, max 500
}
EvidenceItem::Community(CommunityEvidence {
    evidence_id: String,   // "com:" + canonical hash of (binding, community_id, member_signature)
    community_id: i32, label: String, label_source: String, label_confidence: Option<f64>, label_stale: bool,
    size: usize, cohesion: f64, internal_edges: usize, member_signature: String,
    members: Vec<CommunityMember { path, content_hash }>,  // empty in list mode, bounded in detail mode
    members_truncated: bool,
    representatives: Vec<String>,
    boundary: Vec<CommunityBoundary { other_community_id, label, import_count }>,
})
```

The selector field is named `path` so Ask's `_iter_paths` admission check covers it.
`canonical_key` encodes size-desc as `usize::MAX - size` then `community_id`. Outcomes:
zero stored rows → `IndexUnavailable` with the "run gcode index" detail, never
`CompleteEmpty`; no match or all below `min_size` → `CompleteEmpty`; more than `limit`
→ `TruncatedIndex`. Detail mode (any of `community_id`, `label`, `path` set) returns one
item with members bounded by `max_members`; a member missing from the snapshot is dropped
with warning `community_member_not_in_snapshot` while `size` still counts it. Add
`fn project_communities(&self) -> anyhow::Result<Vec<CommunityFact>>` to `EvidenceFacts`
(required; `FakeFacts` supplies a fixture), implement it for `CodewikiFacts` in
`crates/gcode/src/codewiki_facts/communities.rs` through `communities::read_for_context`, execute in
`crates/gcode/src/evidence/communities.rs`, dispatch from `crates/gcode/src/evidence/mod.rs`, extend `evidence_keys()`
with the community item keys, and regenerate both pinned JSON copies.

Tests: `crates/gcode/src/evidence/tests.rs` — `communities_list_orders_by_size_then_id`,
`communities_detail_bounds_members_and_flags_truncation`,
`communities_without_rows_is_index_unavailable`,
`communities_below_min_size_is_complete_empty`,
`communities_evidence_id_survives_relabel`, `communities_selector_rejects_two_keys`;
`crates/gcode/tests/evidence.rs` — `evidence_communities_request_json_round_trips`.

Verify: `cargo nextest run -p gobby-code -E 'test(evidence)'`, `cargo nextest run -p gobby-code --test evidence --test contract`.

**Acceptance:**

- 5.1.1 - `gcode evidence --request-json` accepts `{"operation": {"communities": {...}}}` inside schema version 1 and returns `Community` items in list and detail modes. symbol: `CommunitiesSelector`. file: `crates/gcode/src/evidence/contracts.rs`.
- 5.1.2 - `evidence_id` is derived from binding, community id, and member signature so it survives relabeling. test: `crates/gcode/src/evidence/tests.rs::communities_evidence_id_survives_relabel`.
- 5.1.3 - Zero stored rows report `IndexUnavailable`, below-threshold results report `CompleteEmpty`, and over-limit results report `TruncatedIndex`. test: `crates/gcode/src/evidence/tests.rs::communities_without_rows_is_index_unavailable`.
- 5.1.4 - `EvidenceFacts::project_communities` is required and implemented for `CodewikiFacts` and `FakeFacts`. symbol: `EvidenceFacts`. file: `crates/gcode/src/evidence/mod.rs`.
- 5.1.5 - `evidence_keys()`, the evidence contract test, and both pinned JSON copies list the community item keys. file: `crates/gcode/src/contract/schema.rs`.

### 5.2 Admit, validate, and describe the `communities` operation in Ask [category: code] (depends: 4.3, 5.1)
`kind: deliverable`

Targets:
- `src/gobby/ask/evidence.py::*` — scope-reason: admit communities in EvidenceAdmission, normalize its selector defaults, and build its request
- `src/gobby/ask/stage_runtime.py::*` — scope-reason: add communities to the permitted operation set
- `src/gobby/ask/interactive_evidence.py::*` — scope-reason: extend the operation Literal
- `src/gobby/ask/validation_models.py::*` — scope-reason: add CommunityEvidenceItem to the discriminated union
- `src/gobby/ask/validation.py::*` — scope-reason: handle community items in the manifest and citation checks before the git-metadata fallthrough
- `src/gobby/mcp_proxy/tools/ask.py::*` — scope-reason: extend both operation Literals and the tool descriptions
- `tests/ask/test_validation.py::*` — scope-reason: cover community items in manifest validation and citation diagnostics
- `docs/contracts/ask.md`
- `docs/contracts/gcode-cli.md`

Research context: `EvidenceAdmission.__init__` (`src/gobby/ask/evidence.py:122-133`)
validates `permitted_operations` against `{"search", "read", "graph"}` at :129 and
enforces it in `_admit` (:520-535); `EvidenceAdmission.query` (:226-492) builds requests
inline with `"schema_version": 1`; `_normalize_selector` (:494-518) applies defaults;
`_iter_paths` (:554-567) is the path admission check. `stage_runtime.py:730` sets
`permitted_operations={"search", "read", "graph"}`; `interactive_evidence.py:16` and
`src/gobby/mcp_proxy/tools/ask.py:108,300` carry the operation `Literal`, with descriptions at
:103-104, :279, :295. `validation_models.py:94-97` is the `EvidenceItem` discriminated
union over `SourceEvidenceItem`, `GraphEvidenceItem`, `CommitMetadataEvidenceItem`
(`extra="forbid"`, so an unknown item fails the response at answer time rather than at
retrieval); `validation.py::_validate_evidence_manifest` (:261-371) branches on item
type at :356-363 and `_citation_diagnostics` (:452-537) at :468 and :498.
`docs/contracts/ask.md` describes evidence under `## Evidence And Provenance` (:58);
`docs/contracts/gcode-cli.md:153-167` enumerates the operations. Rejected: a
`CommunityCitation` (communities orient; answers cite `read` items from their members);
admitting `communities` only in interactive mode (the pipeline stages are where a
subsystem overview saves the most retrieval budget).

Add `"communities"` to the permitted set in `evidence.py` and `stage_runtime.py`, to
both `Literal`s, and to the tool descriptions with the sentence "communities orients
you; it is not citable, cite `read` items from its members". `_normalize_selector`
defaults `min_size` to 2 and `max_members` to 50 and rejects more than one of
`community_id`, `label`, `path`. `CommunityEvidenceItem` mirrors the Rust struct field
for field and joins the union with `type: Literal["community"]`; `validation.py` adds
an `isinstance(item, CommunityEvidenceItem)` arm that records the item in the manifest
and rejects any citation that points at it. Docs: `ask.md` gains a paragraph on the
operation and its non-citability; `gcode-cli.md` gains the `communities` bullet in the
operations list (the Version 11 paragraph from 4.3 already names it).

Tests: `tests/ask/test_validation.py` — `test_manifest_accepts_community_item`,
`test_citation_of_community_item_is_rejected`,
`test_selector_rejects_multiple_community_keys`.

Verify: `DATABASE_URL=postgresql://gobby_test:gobby_test@127.0.0.1:60892/gobby_test GOBBY_TEST_PROTECT=1 uv run pytest tests/ask -q`,
`uv run ruff check src/ && uv run mypy src/`.

**Acceptance:**

- 5.2.1 - Ask admits `communities` in pipeline and interactive modes and forwards a schema-1 request with normalized selector defaults. symbol: `EvidenceAdmission._normalize_selector`. file: `src/gobby/ask/evidence.py`.
- 5.2.2 - `CommunityEvidenceItem` is a member of the `EvidenceItem` union and manifest validation accepts it. test: `tests/ask/test_validation.py::test_manifest_accepts_community_item`.
- 5.2.3 - A citation that targets a community item is rejected with a diagnostic naming the operation as non-citable. test: `tests/ask/test_validation.py::test_citation_of_community_item_is_rejected`.
- 5.2.4 - The MCP tool literals, descriptions, `docs/contracts/ask.md`, and the `gcode evidence` operations list describe the operation. behavior: "communities" in `docs/contracts/ask.md`.

## P6: Labels in the daemon
`kind: framing`

**Goal**: a daemon maintenance job generates purpose names for unlabeled or stale
communities, gates them through Jev, and writes them back with signature guards,
mirroring the symbol summarizer.

### 6.1 Community label storage, model, and configuration [category: code] (depends: 3.1)
`kind: deliverable`

Targets:
- `src/gobby/code_index/_storage/communities.py`
- `src/gobby/code_index/storage.py::*` — scope-reason: register CodeIndexCommunityStorageMixin on CodeIndexStorage
- `src/gobby/code_index/models.py::*` — scope-reason: add the StoredCommunity dataclass
- `src/gobby/config/code_index.py::*` — scope-reason: add CodeIndexCommunityLabelConfig and the community_label field on CodeIndexConfig
- `crates/gcore/assets/config/runtime_config_contract.json::*` — scope-reason: regenerate the runtime config contract for the new community_label settings
- `web/src/api/runtimeConfigCodecVectors.gen.ts::*` — scope-reason: regenerate the codec vectors alongside the runtime config contract
- `docs/audits/configuration-audit.md`
- `tests/code_index/test_code_index_storage.py::*` — scope-reason: cover the signature-guarded label writes and the queue query

Research context: `CodeIndexSummaryStorageMixin`
(the summaries storage mixin, lines 14-78) is the template:
`get_unsummarized_symbols` joins the machine's `code_indexed_file_states`, filters
`summary IS NULL` with a `summary_attempted_at` cooloff (`SYNC_FAILURE_COOLOFF_SECONDS`
from the storage constants module), `update_symbol_summary` writes only when the content
hash still matches, and `mark_symbol_summaries_attempted` stamps failures.
`CodeIndexStorage` (`storage.py:19-34`) registers mixins by plain multiple inheritance.
`Symbol` (`models.py:46-166`) shows the row-dataclass shape with `from_row`.
`CodeIndexSymbolSummaryConfig(FeatureDefaultConfig)` (`src/gobby/config/code_index.py:11-32`,
fields `enabled`, `batch_size`, `max_concurrency`, `max_tokens`) hangs off
`CodeIndexConfig.symbol_summary` (:101). Any config-module change requires
regenerating `crates/gcore/assets/config/runtime_config_contract.json` (and the codec
vectors) with `uv run python scripts/generate_runtime_config_contract.py`, and
`docs/audits/configuration-audit.md` carries one row per setting. The registry
classifies fields whose names end in `api_key` as `$secret:` references automatically.
Rejected: storing labels in a daemon-owned table (the label is a column on the row gcode
reads; two tables would need a join and a second lifecycle).

`CodeIndexCommunityStorageMixin`: `get_unlabeled_communities(project_id, limit,
failure_cooloff_seconds)` selecting rows where `labeled_signature IS DISTINCT FROM
member_signature` and `label_attempted_at` is null or older than the cooloff, ordered by
`member_count DESC`; `update_community_label(project_id, community_id,
member_signature, *, label, label_source, label_confidence, label_model,
labeled_signature)` guarded by `AND member_signature = %s` (a refresh in between makes
the write a no-op); `mark_community_labels_attempted(rows)` likewise guarded. All three
use `require_machine_id()`. `StoredCommunity` dataclass mirrors the table.
`CodeIndexCommunityLabelConfig(FeatureDefaultConfig)`: `enabled=True`, `batch_size=10`
(le 20), `max_concurrency=2`, `max_tokens=120`, `decisions_api_base: str | None = None`,
`decisions_api_key: str | None = None`, `decisions_model="jev-latest"`,
`decisions_min_confidence=0.5`, `decisions_timeout_seconds=30`; field
`CodeIndexConfig.community_label`. Regenerate the contract and codec vectors; add the
audit rows.

Tests: `tests/code_index/test_code_index_storage.py` —
`test_get_unlabeled_communities_respects_signature_and_cooloff`,
`test_update_community_label_is_signature_guarded`.

Verify: `DATABASE_URL=postgresql://gobby_test:gobby_test@127.0.0.1:60892/gobby_test GOBBY_TEST_PROTECT=1 uv run pytest tests/code_index/test_code_index_storage.py tests/config -q`.

**Acceptance:**

- 6.1.1 - `CodeIndexCommunityStorageMixin` queues by signature mismatch with a cooloff and writes labels only when the member signature still matches. test: `tests/code_index/test_code_index_storage.py::test_update_community_label_is_signature_guarded`.
- 6.1.2 - `CodeIndexCommunityLabelConfig` exists with the fields above, `decisions_api_key` classifies as a secret, and the runtime config contract, codec vectors, and audit rows are regenerated. symbol: `CodeIndexCommunityLabelConfig`. file: `src/gobby/config/code_index.py`.
- 6.1.3 - `StoredCommunity` round-trips a `code_communities` row. symbol: `StoredCommunity`. file: `src/gobby/code_index/models.py`.

### 6.2 Generate and validate community names in the maintenance loop [category: code] (depends: 4.2, 6.1)
`kind: deliverable`

Targets:
- `src/gobby/code_index/community_labeler.py`
- `src/gobby/code_index/community_label_safety.py`
- `src/gobby/code_index/maintenance.py::*` — scope-reason: add the community labeling step after the symbol summary step with keyword-only wiring
- `src/gobby/runner_startup_code_index.py::*` — scope-reason: construct the labeler beside the summarizer and pass it to the loop
- `tests/code_index/test_community_labeler.py`
- `tests/code_index/test_code_index_maintenance.py::*` — scope-reason: cover the labeling step and its failure cooloff

Research context: `code_index_maintenance_loop` (`maintenance.py:32-76`) drives
`_run_maintenance(context, summarizer=None, symbol_summary_batch_size=20,
missing_root_observations=None)` (:79-201), which ends each active project with
`if summarizer: await _summarize_unsummarized(...)` (:196-201);
`_summarize_unsummarized` (:381-428) reads a batch, generates through
`SymbolSummarizer.summarize_batch`, stamps failures, and writes results.
`SymbolSummarizer` (summarizer module, lines 40-125) is the generation template;
`LLMService.call_json_feature` (llm service module, lines 130-159) takes a
`json_schema` and routes through profile/candidate fallback with provider-native JSON
where available, so schema validation happens in Python regardless of adapter.
`runner_startup_code_index.py:95-104` constructs the loop task and passes
`symbol_summary_batch_size`. `tests/code_index/test_code_index_maintenance.py` calls
`_run_maintenance` positionally at fifteen sites, hence keyword-only additions.
Rejected: labeling inside gcode (no credentials there); free-form labels written
directly (Graphify's revalidation record shows the labeling pass repeatedly failing to
`Community N` placeholders and its wiki orphaning pages when names drift).

`community_label_safety.py`: `sanitize_community_label(text) -> str | None` strips
fences and quotes, requires 2–5 words and ≤ 40 characters, charset `[A-Za-z0-9 /&+.-]`,
rejects source extensions and names equal to a member path. `community_labeler.py`:
`CommunityLabeler(llm_service, config)` with `generate_batch(communities,
read_context) -> dict[int, GeneratedLabel]` calling `call_json_feature` with schema
`{name: string 3–40, rationale: string ≤ 120}`; the prompt marks member paths and
representative symbols as untrusted data and asks for a 2–5 word purpose name;
`rationale` is never persisted. A validated name equal to any deterministic candidate
skips the gate (6.3) and writes `label_source='deterministic'`. Without a gate
configured, a validated name is written as `label_source='model'`, confidence `NULL`,
`label_model` = the generation profile, `labeled_signature` = the member signature.
Failures stamp `label_attempted_at` (300-second cooloff via the storage default) and
change nothing else. Wire `_label_unlabeled_communities(context, project, labeler,
batch_size)` after the summary block with `community_labeler: CommunityLabeler | None =
None` and `community_label_batch_size: int = 10` keyword-only on `_run_maintenance` and
the loop; construct the labeler in `runner_startup_code_index.py` when
`config.code_index.community_label.enabled`.

Tests: `tests/code_index/test_community_labeler.py` —
`test_sanitize_rejects_paths_fences_and_long_names`,
`test_generated_name_equal_to_candidate_writes_deterministic`,
`test_generation_failure_stamps_attempt_only`; `test_code_index_maintenance.py` —
`test_maintenance_labels_unlabeled_communities`.

Verify: `DATABASE_URL=postgresql://gobby_test:gobby_test@127.0.0.1:60892/gobby_test GOBBY_TEST_PROTECT=1 uv run pytest tests/code_index/test_community_labeler.py tests/code_index/test_code_index_maintenance.py -q`,
`uv run ruff check src/ && uv run mypy src/`.

**Acceptance:**

- 6.2.1 - The maintenance pass labels queued communities after symbol summaries with keyword-only wiring that leaves existing call sites untouched. symbol: `_label_unlabeled_communities`. file: `src/gobby/code_index/maintenance.py`.
- 6.2.2 - Generated names pass schema validation and sanitation before any write, and a name equal to a deterministic candidate is stored as deterministic. test: `tests/code_index/test_community_labeler.py::test_generated_name_equal_to_candidate_writes_deterministic`.
- 6.2.3 - A generation failure stamps `label_attempted_at` and changes no label field. test: `tests/code_index/test_community_labeler.py::test_generation_failure_stamps_attempt_only`.

### 6.3 Gate generated names through a Jev `choice` decision [category: code] (depends: 6.2)
`kind: deliverable`

Targets:
- `src/gobby/llm/decisions.py`
- `src/gobby/code_index/community_labeler.py`
- `tests/llm/test_decisions_client.py`
- `tests/code_index/test_community_labeler.py`
- `docs/guides/llm-features.md`

Research context: the Jev contract is in the reference section above; the 1P endpoint is
GA and documented, the OpenRouter alpha endpoint exists but its wire shape is
unverified. `retry_async` (llm claude runtime module, lines 141-164) is the repo's
async retry helper; the daemon already uses `httpx`. `src/gobby/llm/decisions.py` does
not exist. `docs/guides/llm-features.md:170` is the `code_index.symbol_summary` row.
Rejected: a general decision-provider abstraction (one consumer; `restraint`);
routing the choice through `call_json_feature` (a text model's self-reported confidence
is not calibrated, which is the whole reason for the gate); asking Jev to name anything
(it selects among fixed keys and cannot emit text).

`decisions.py`: `ChoiceQuestion(criteria: dict[str, str])`, `ChoiceAnswer(choice,
probabilities, confidence)`, `DecisionsUnavailable`, and `DecisionsClient(base_url,
api_key, model, timeout_seconds)` with `async choose(state, questions: dict[str,
ChoiceQuestion]) -> dict[str, ChoiceAnswer]` posting `{state, model, questions}`;
401/422 raise without retry, 429/529 retry with backoff through `retry_async`; answers
are matched by question key and a missing key is a hard failure. In
`community_labeler.py`, one request per batch: state = the batch's communities (id,
representative paths, top symbols, the generated name and its rationale), one `choice`
per community with criteria `generated` and `deterministic_<i>` for each candidate.
Budget about 500 tokens of state per community plus 250 per question, so a batch of 20
stays near 15k of the 32k limit; cost about $0.0003 per pass. Admission: choice
`generated` with `confidence >= decisions_min_confidence` → `label` = generated,
`label_source='model'`, confidence, `label_model='jev-latest'`, `labeled_signature`
set; any other choice or lower confidence → deterministic label written with
`labeled_signature` set (a decision, reopened only by a membership change); transport
failure → attempt stamped only. That reject/failure split is the anti-hot-loop rule.
`decisions_api_base` unset skips the gate (6.2 behavior). Record the wire-shape spike
(1P base versus the OpenRouter alpha base, one keyed request each) in the Q1 label
evidence directory before enabling either base in live config.

Tests: `tests/llm/test_decisions_client.py` — `test_choose_posts_state_model_questions`
(fake transport), `test_unauthorized_does_not_retry`, `test_missing_answer_key_is_error`;
`tests/code_index/test_community_labeler.py` gains `test_gate_admits_at_threshold_and_rejects_below`
(added here, file created in 6.2). `llm-features.md`: new row `code_index.community_label`
and a paragraph on the decisions gate (no `AICapability`, not a profile route).

Verify: `DATABASE_URL=postgresql://gobby_test:gobby_test@127.0.0.1:60892/gobby_test GOBBY_TEST_PROTECT=1 uv run pytest tests/llm/test_decisions_client.py tests/code_index/test_community_labeler.py -q`,
`uv run ruff check src/ && uv run mypy src/`.

**Acceptance:**

- 6.3.1 - `DecisionsClient.choose` posts `{state, model, questions}` with bearer auth, retries only on 429/529, and matches answers by key. test: `tests/llm/test_decisions_client.py::test_choose_posts_state_model_questions`.
- 6.3.2 - A `generated` choice at or above the threshold writes the model label with confidence and model; anything else writes the deterministic label with the signature stamped; transport failure stamps the attempt only. test: `tests/code_index/test_community_labeler.py::test_gate_admits_at_threshold_and_rejects_below`.
- 6.3.3 - `docs/guides/llm-features.md` documents `code_index.community_label` and the decisions gate. behavior: "community_label" in `docs/guides/llm-features.md`.

## P7: Documentation
`kind: framing`

**Goal**: the skill reference and the workspace changelog describe the new behavior.

### 7.1 Update the code-index graphs reference and the Rust changelog [category: docs] (depends: 4.3, 4.4, 5.2, 6.3)
`kind: deliverable`

Targets:
- `src/gobby/install/shared/skills/gobby/references/code-index/graphs.md`
- `crates/CHANGELOG.md`

Research context: `graphs.md:20-24` says "Communities use the graph analysis
implementation, not file ownership inferred from incoming edges", which is now wrong in
two ways (they come from a persisted per-machine partition, and labels may be
model-derived). `crates/CHANGELOG.md` follows Keep a Changelog with an Unreleased
section. The bundled content manifest is git-ignored and must not be generated.
Rejected: touching the router skill under the gcode assets (it has no community text).

Rewrite `graphs.md:20-24`: MCG community labels come from the persisted project-level
import partition written by `gcode index` (per machine and project, ids stable across
runs); when no partition is stored the view leaves `community` null and says so; add the
`gcode graph view --view communities [--min-size N] [--community ID|LABEL]` bullet and
the "Which files form a subsystem, and how subsystems depend on each other" row; note
that `label_stale` means the model label predates the current membership and the
deterministic label is shown. `crates/CHANGELOG.md` Unreleased: Added — gcode
`--view=communities`, `--min-size`, `--community`, the `code_communities` table
(migration 442), the `communities` evidence operation, the report section; Added —
gobby-core `graph_analytics::communities`, `centrality`, `GraphInputError`; Changed —
MCG labels are persisted and content-derived, `community-N` ids removed, contract 11,
gcode 1.9.0.

Verify: `DATABASE_URL=postgresql://gobby_test:gobby_test@127.0.0.1:60892/gobby_test GOBBY_TEST_PROTECT=1 uv run pytest tests/skills -q -k graphs`
(or the nearest skill-reference test), `uv run gobby skills validate` if present.

**Acceptance:**

- 7.1.1 - The graphs reference describes the persisted partition, the seedless and detail view forms, and `label_stale`. behavior: "--view communities" in `src/gobby/install/shared/skills/gobby/references/code-index/graphs.md`.
- 7.1.2 - The changelog records every added and changed surface under Unreleased. behavior: "code_communities" in `crates/CHANGELOG.md`.

## D1 Memory community detection over memory_crossrefs (depends: 1.1)
`kind: deferred`

Memory has a persisted memory-to-memory similarity graph in the `memory_crossrefs`
table (`source_id`, `target_id`, `similarity`), written by `CrossrefService` in
`src/gobby/memory/services/crossref.py` and read by `get_related_memories` and
`GET /api/memories/graph`. Nothing partitions it. The `memory-curator` workflow
(`src/gobby/install/shared/workflows/agents/memory-curator.yaml`) is written to
consume near-duplicate memory clusters that no in-repo code produces, and the
nightly dream is a per-memory sweep with no cross-memory grouping. A Leiden pass
over `memory_crossrefs` is the natural producer for those clusters.

The Leiden kernel this plan exposes through `graph_analytics::communities` (1.1)
is Rust-only: gcore has no PyO3 or cdylib binding, and `gdaemon` does not compile
`gobby-core` with the `graph-analytics` feature. The memory subsystem is Python
today, so reaching the kernel would need a bridge that is thrown away once memory
moves to Rust. The external prerequisite is the Memory and search family epic
#21563 under Stage 2 (#21544) of the 1.0.0 epic (#21542), which lands the
gobby-memory crate. After that, `communities()` is reachable in-process and the
remaining decisions are memory-side:

- Edge supply: `auto_crossref` defaults to `False`, so `memory_crossrefs` is empty on
  a default install. The partition needs either that default flipped or edges built
  on demand from Qdrant similarity at cluster time.
- Consumer: whether the partition feeds the memory-curator batch, a dream
  consolidation step (`.gobby/plans/memory-dream-consolidation.md` groups
  candidates through the LLM planner and names no clustering algorithm), or a
  graph-view coloring in `web/src/components/activity/memory/MemoryGraphView.tsx`.

At finalization the coordinator creates the deferral task under this plan's epic
with label `deferred-from:gcode-import-communities:D1`, `needs-planning`, a
`blocked-by` edge on #21563, and a `blocked-by` edge on the 1.1 leaf.

```yaml
deferral:
  task_ref: "#TBD"
  reason: "Memory community detection over memory_crossrefs waits for the gobby-memory crate from the Memory and search family (#21563, Stage 2 of the 1.0.0 epic) so the Rust kernel is reachable in-process; edge-supply and consumer decisions follow that move. This epic delivers only the reusable kernel entry point."
  owner: "memory-and-search family (#21563)"
  original_acceptance_items:
    - D1.1
```

## Q1 Verification and experiments
`kind: verification`

1. Crate gates: `cargo fmt --all -- --check`, `cargo clippy -p gobby-core -p gobby-code`,
   `cargo nextest run -p gobby-core --features graph-analytics --features postgres`,
   `cargo nextest run -p gobby-code`, `cargo nextest run -p gobby-code --test contract --test evidence`,
   `cargo nextest run -p gobby-daemon --test cli_contract`.
2. Python gates: `uv run ruff check src/`, `uv run mypy src/`, and the focused pytest
   modules named in 5.2, 6.1, 6.2, 6.3 with `DATABASE_URL` pointed at the test hub and
   `GOBBY_TEST_PROTECT=1`.
3. Install: `cargo build --release -p gobby-code -p gobby-daemon -p gobby-hooks`, announce
   with a `global` `send_message`, `uv run gobby restart` (applies 442), `uv run gobby cutover`
   for the coherent set. Read binary hashes from `~/.gobby/bin/`, never `target/release/`.
4. Determinism and churn (Gobby checkout and the frozen Game Goblins baseline
   `0216f1e33f05…` under `/Users/josh/Projects/wiki-bakeoff-code-2026-09/`): two `gcode index`
   runs on unchanged input produce byte-identical `code_communities` rows and skip the
   write; renaming three files inside one community keeps its `community_id`, changes only
   its `member_signature`, and flips exactly one row to `label_stale`; adding one file
   changes at most one id; deleting a community and adding an unrelated one never reuses
   the retired id. Record row diffs in `docs/evidence/community-labels-2026-09/churn.md`.
5. Split thresholds: report size distribution and cohesion with and without the two split
   passes on both corpora. Baselines to beat: Graphify's Game Goblins partition had 116
   communities, largest 3.4% of nodes, median 17.5, 27 singletons. Acceptance for the
   file-level partition: largest community ≤ 25% of visible files after splitting; no
   `community-N` placeholder anywhere; `--min-size 5` on Gobby yields deterministic labels
   that name subsystems (`src/gobby/memory`, `crates/gcode/src/...`, `web/src/...`).
   Replace the 25% / 0.05 / 50 constants only with recorded evidence.
6. Label quality: 30 communities (15 per corpus, stratified by size), arms D
   (deterministic), G (generation only, gate unset), J (Jev-gated); blind rating on
   accuracy 0–2 and findability 0–2 by a rater who does not see the arm. Ship the gate
   enabled only if J beats D by ≥ 0.3 accuracy and ≥ 0.5 findability, J regresses ≤ 5% of
   communities below D, gate precision ≥ 0.85 and recall ≥ 0.60 against the rater, mean
   confidence separation between admitted and rejected ≥ 0.15, schema-rejection rate
   ≤ 10%, and the run costs ≤ $0.05. If G already regresses ≤ 5%, record that the gate is
   not earning its keep and ship with `decisions_api_base` unset. Record the wire-shape
   spike (1P versus OpenRouter alpha) alongside. Evidence lands in
   `docs/evidence/community-labels-2026-09/`.
7. Read-path cost: `gcode graph report` median wall time over five runs before and after
   1.2; no regression allowed. `gcode graph view --view communities` on Gobby completes
   under one second from stored rows.
8. Live smoke after cutover: `gcode index` on this repo, then
   `gcode graph view --view communities --min-size 5 --format json` (labels name
   subsystems, sizes descend, `edges[]` join `community:` ids with counts, Mermaid
   validates), `gcode graph view --view communities --community memory`,
   `gcode graph view --view mcg --file crates/gcode/src/commands/graph/view/mcg.rs`
   (`mcg.rs`, `crates/gcode/src/commands/graph/view/mcg/fetch.rs`, `crates/gcode/src/commands/graph/view/mcg/identity.rs`, `mod.rs`, `render.rs` share one label;
   uniquely provided `module:` nodes carry the provider's label; external modules carry
   `null`), `gcode graph report` shows the section, and a `communities` request through
   `gcode evidence --request-json` and the `gobby-ask` MCP `evidence` tool returns items
   whose `evidence_id` is unchanged after a relabel. Sanity baseline before the change:
   the same MCG call today yields one community per node.
9. Daemon coordination: the label job runs inside the daemon, so 6.x changes need
   `uv run gobby restart`; announce with a `global` `send_message` and wait for a quiet
   window with no live spawned worker or close validator.

## V1 Plan Changelog
`kind: framing`

- Draft 1 (2026-09-10): initial narrative from exploration and the confirmed decision record.
- Draft 2 (2026-09-19): parity+ with Graphify 0.9.55. Persisted per-machine identity with
  Jaccard remapping and a non-recycling watermark (`code_communities`, migration 442),
  index-time refresh with degrade-not-fail, cohesion and both split passes, a `centrality()`
  façade for the report, `--community` detail mode, a report section, the `communities`
  evidence operation, and a daemon label pipeline gated by Jev. Corrected stale facts from
  Draft 1: contract is 10 → 11 in five pins; `skills/code-index/SKILL.md` is deleted and
  `graphs.md` is the live text; the bundled manifest is never regenerated; install is a
  coherent-set cutover; `McgIdentity` moves to `communities::identity::ImportIdentity`.
