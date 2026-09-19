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
  confirmed or replaced by the threshold spike that runs inside 2.2 before that leaf
  closes (Q1.5 records the result). No CLI knobs; `DEFAULT_GAMMA` stays 1.0.
- Labels: gcode always writes a deterministic label (dominant directory, in-degree
  fallback) plus candidates. A daemon job generates a 2–5 word purpose name, validates
  it against a schema, and asks Jev (TypeSafe System One, a `choice` question) whether
  the generated name beats the deterministic candidates. The generated name is admitted
  at confidence ≥ 0.5; when no decisions endpoint is configured, schema validation alone
  admits it; otherwise the deterministic label stands. Labels are cached by member
  signature; a stale model label is displayed as the deterministic label with
  `label_stale: true`.
- Surfaces: fix MCG labels, add a seedless `--view=communities` with `--min-size` and a
  `--community <id|label|path>` detail mode, add an `## Import communities` section to
  `gcode graph report`, and add the `communities` evidence operation. CLI contract 10 → 11.
  MCG Mermaid output groups nodes into one subgraph per community.
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
- Migration number: 3.1 is written against `441_add_coordination_reply_waits.sql` as the
  latest migration at `1ee3db66d3`, so it names 442. Task #22572 (gobby#13891, workspace
  ref re-basing) is landing its own `442_*.sql` concurrently. The number in this plan is
  the next free migration number when 3.1 lands, not a decision: the executor re-resolves
  every occurrence of the provisional number in this plan to that number in the same
  commit as 3.1: the file name, `MIGRATIONS`, `latest_version`, `cli_contract.rs`, every
  "442" in 3.1, the migration number 7.1 writes into `crates/CHANGELOG.md`, and Q1
  step 3's "applies 442". V1 records the number actually used (3.1.7).
- Hand-maintained production `.rs`/`.py` files stay under 1,000 lines. Sizes on 2026-09-19 at
  `7b6dc3bfd0`: `cli.rs` 827 (split in 4.1 before it grows), `graph_analytics.rs` 693
  (about 620 after 1.2: 1.1 moves the 161-line inline test module at :533-693 to
  `crates/gcore/src/graph_analytics/tests.rs` before adding roughly 90 production lines),
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
  deliverables that touch one file), generated from the Targets blocks on 2026-09-19 and
  re-derived whenever a Targets block changes:
  `crates/gcore/src/graph_analytics.rs` and `crates/gcore/src/graph_analytics/tests.rs` 1.1 → 1.2;
  `crates/gcode/src/communities.rs` 2.1 → 2.2 → 2.3 → 3.2;
  `crates/gcode/src/commands/graph/view/mcg/fetch.rs` and
  `crates/gcode/src/commands/graph/view/mcg/tests.rs` 2.1 → 4.2 (via 3.2);
  `crates/gcode/src/commands/graph/view/tests.rs` 4.2 → 4.3;
  `crates/gcode/src/cli/graph_view.rs` 4.1 → 4.3;
  `crates/gcode/src/contract.rs` 3.2 → 4.3;
  `crates/gcode/contract/gcode.contract.json` and `tests/contracts/gcode.contract.json` 3.2 → 4.3 → 4.4 → 5.1;
  `crates/gcode/src/contract/schema.rs` 4.4 → 5.1;
  `crates/gcode/tests/contract.rs` 4.3 → 5.1 (via 4.4);
  `crates/gcode/security/managed_postgres_privileges.json` 3.1 → 3.2;
  `docs/contracts/gcode-cli.md` 4.3 → 5.2;
  `src/gobby/code_index/community_labeler.py` and `tests/code_index/test_community_labeler.py` 6.2 → 6.3.
  `crates/gcode/src/commands/graph/view/mcg.rs`, `crates/gcode/src/commands/graph/view/render.rs`,
  `render_tests.rs`, and `crates/gcode/src/graph/report/summary.rs` each have a single
  owner (4.2, 4.2, 4.2, 1.2); `mcg.rs` holds no identity reference today, so 2.1 does not
  touch it and its `ImportIdentity` import arrives with 4.2's `label_communities`.
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
  `EvidenceItem::` matches without a wildcard arm in `crates/gcode/src/evidence/search.rs:41-42`
  (the search-lane eligibility retain; 5.1 adds the `Community` arm) and
  `crates/gcode/src/evidence/contracts.rs:238` (`canonical_key`); constructor and pattern uses in
  `crates/gcode/src/evidence/graph.rs`, `crates/gcode/src/evidence/read.rs`, `crates/gcode/src/evidence/mod.rs:345`,
  `crates/gcode/src/evidence/tests.rs`, and `crates/gcode/tests/evidence.rs` need no arm of their own;
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
- `crates/gcore/src/graph_analytics.rs::*` — scope-reason: add the communities entry point and typed input error beside the existing analyze façade, and move the inline test module to a #[path] sibling
- `crates/gcore/src/graph_analytics/tests.rs`

Research context: `analyze` (`graph_analytics.rs:78-95`) runs Tarjan bridges,
centrality, god nodes, unexpected links, and hotspots; `PreparedGraph` (:127-133) and
its methods (`new` :136, `centrality` :211, `communities` :279) are private. `leiden.rs`
is a private module whose items are `pub(super)` (`DEFAULT_GAMMA` :16,
`detect_communities` :366). `PreparedGraph::communities` assigns ordinals by the
lexicographically smallest member id, so ordinal ids renumber on any membership change;
that is why P2 remaps rather than reusing ordinals. `thiserror` is already a gcore
dependency. Existing tests at :537-692 (`seeded_graph`, singleton and empty cases) are
the fixtures to reuse. The inline `#[cfg(test)]` module (:533-693, 161 lines, four tests)
moves to `crates/gcore/src/graph_analytics/tests.rs` under the `#[path]` idiom from the
crates AGENTS guide (the directory already holds `leiden.rs`): inline test modules count
toward the 1,000-line ceiling, and the two P1 leaves would otherwise land the file near
900 lines, inside the decomposition hook's blocking band. Rejected: exposing
`PreparedGraph` publicly (its sanitizing constructor is what the typed error replaces).

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

Tests (in `crates/gcore/src/graph_analytics/tests.rs`, beside the moved `seeded_graph`
fixtures): `communities_matches_analyze_partition_on_seeded_graph`,
`communities_rejects_unknown_endpoint`, `communities_rejects_invalid_weight` (NaN and
0.0), `communities_rejects_duplicate_node`, `communities_rejects_self_loop`,
`communities_empty_graph_is_empty`, `communities_without_edges_is_all_singletons`.

Verify: `cargo fmt -p gobby-core -- --check`, `cargo clippy -p gobby-core --features graph-analytics`,
`cargo nextest run -p gobby-core --features graph-analytics -E 'test(graph_analytics)'`.

**Acceptance:**

- 1.1.1 - `communities(&AnalyticsGraph)` returns the same partition as `analyze` on the seeded graph and skips the other passes. symbol: `communities`. file: `crates/gcore/src/graph_analytics.rs`.
- 1.1.2 - `GraphInputError` is returned for duplicate node, unknown endpoint, invalid weight, and self-loop instead of silent sanitizing. symbol: `GraphInputError`. file: `crates/gcore/src/graph_analytics.rs`.
- 1.1.3 - Empty and edge-less graphs behave as `analyze` does. test: `crates/gcore/src/graph_analytics/tests.rs::communities_without_edges_is_all_singletons`.
- 1.1.4 - The pre-existing inline test module lives in `crates/gcore/src/graph_analytics/tests.rs` under `#[path]` and `crates/gcore/src/graph_analytics.rs` declares no `#[cfg(test)]` module. file: `crates/gcore/src/graph_analytics/tests.rs`.

### 1.2 Add `centrality()` and stop `graph report` from running Leiden twice [category: code] (depends: 1.1)
`kind: deliverable`

Targets:
- `crates/gcore/src/graph_analytics.rs::*` — scope-reason: add the centrality entry point sharing the validation of communities
- `crates/gcore/src/graph_analytics/tests.rs`
- `crates/gcode/src/graph/report/summary.rs::*` — scope-reason: replace both analyze calls with centrality in gcore_hotspots_for_code_graph and gcore_incoming_call_hotspots
- `docs/evidence/community-labels-2026-09/report-latency.md`

Research context: `gcore_hotspots_for_code_graph` (`summary.rs:51-91`, `analyze` at
:68) and `gcore_incoming_call_hotspots` (:102-156, `analyze` at :133) each run the full
`analyze` pass and read only `.centrality`, discarding two Leiden partitions per report
(observed 2026-09-19; the executor confirms no other field is read before swapping).
`CentralityScore` (`graph_analytics.rs:42-46`) is the element type of
`GraphAnalytics.centrality`. Rejected: caching the `analyze` result across the two
callers (still pays for Leiden once per report for nothing).

`pub fn centrality(graph: &AnalyticsGraph) -> Result<Vec<CentralityScore>, GraphInputError>`
validates exactly as `communities()` and returns `PreparedGraph::new(graph).centrality()`.
Test `centrality_matches_analyze_centrality` on `seeded_graph()` in
`crates/gcore/src/graph_analytics/tests.rs`. In `summary.rs`
map `GraphInputError` into the existing hotspot degradation path (the report already
tolerates a missing analytics input); no output shape changes.

Verify: `cargo nextest run -p gobby-core --features graph-analytics -E 'test(graph_analytics)'`,
`cargo nextest run -p gobby-code -E 'test(report)'`, and the Q1.7 report timing (median
wall time of `gcode graph report` over five runs before and after this leaf) recorded in
`docs/evidence/community-labels-2026-09/report-latency.md`.

**Acceptance:**

- 1.2.1 - `centrality(&AnalyticsGraph)` returns the same scores as `analyze(...).centrality` on the seeded graph. test: `crates/gcore/src/graph_analytics/tests.rs::centrality_matches_analyze_centrality`.
- 1.2.2 - No `analyze` call remains in `crates/gcode/src/graph/report/summary.rs`; report hotspots and bridges are byte-identical before and after on the Gobby checkout. file: `crates/gcode/src/graph/report/summary.rs`.
- 1.2.3 - `gcode graph report` median wall time over five runs is recorded before and after the `centrality()` swap with no regression. behavior: "median" in `docs/evidence/community-labels-2026-09/report-latency.md`.

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
and calls `from_resolution`; it drops the rows after building the identity.
`read_active_imports` filters `WHERE ci.project_id = $2` for one id while `visible_tree`
under `ProjectIndexScope::Overlay` unions the overlay's files with every unshadowed parent
file, so `load_identity`'s body alone leaves every parent-only file edgeless under an
overlay context: latent in today's depth-bounded MCG view, and a persisted project-wide
shred of singletons once the body feeds 2.2. `visibility::visible_project_ids(ctx)`
(`crates/gcode/src/visibility.rs:36-47`) returns the overlay id first, then the parent. gcode is a
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

The body follows `load_identity` with one change: import rows are read once per id in
`visibility::visible_project_ids(ctx)` (`read_active_imports` keeps its single-project
signature), and the pure `merge_visible_imports(overlay_rows, parent_rows, overlay_paths)`
drops every parent row whose `file_path` the overlay shadows, mirroring `visible_tree`'s
anti-join, so under an overlay context every visible file keeps its edges. `rows` stay
`(file_path, module_name)`. `crates/gcode/src/commands/graph/view/mcg/fetch.rs::run`
opens its connection as today and passes it. Identity tests move to
`crates/gcode/src/communities/identity_tests.rs` via the `#[path]` test-file idiom from the crates AGENTS guide, including
`mcg_identity_build_handles_twenty_thousand_rows` and the `identity_from` helper (keep a
one-line re-export of the helper for `crates/gcode/src/commands/graph/view/mcg/tests.rs`).

Verify: `cargo nextest run -p gobby-code -E 'test(identity) | test(mcg)'`, `cargo clippy -p gobby-code`.

**Acceptance:**

- 2.1.1 - `ImportIdentity` lives in `crates/gcode/src/communities/identity.rs` with `unique_provider` and `from_resolution` reachable crate-wide; `crates/gcode/src/commands/graph/view/mcg/identity.rs` keeps only seed resolution. symbol: `ImportIdentity`. file: `crates/gcode/src/communities/identity.rs`.
- 2.1.2 - `load_project_imports(conn, ctx)` returns the identity and the import rows on a caller-supplied connection; `load_identity` is gone. symbol: `load_project_imports`. file: `crates/gcode/src/communities/identity.rs`.
- 2.1.3 - The moved identity tests pass unchanged in their new file. test: `crates/gcode/src/communities/identity_tests.rs::mcg_identity_build_handles_twenty_thousand_rows`.
- 2.1.4 - Under an overlay context `load_project_imports` returns import rows for every visible file, parent-only files included, with parent rows the overlay shadows by `file_path` dropped. test: `crates/gcode/src/communities/identity_tests.rs::overlay_partition_covers_parent_only_files`.

### 2.2 Build the partition: graph, Leiden, splits, cohesion, deterministic labels, signatures [category: code] (depends: 1.1, 2.1)
`kind: deliverable`

Targets:
- `crates/gcode/src/communities.rs`
- `crates/gcode/src/communities/partition.rs`
- `crates/gcode/src/communities/labels.rs`
- `crates/gcode/src/communities/partition_tests.rs`
- `docs/evidence/community-labels-2026-09/thresholds.md`

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
    pub partition_signature: String,            // sha256 over every stored per-community column and the folded directed imports (below)
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
`partition_signature` is sha256 over, per community in partition order, `member_signature`,
`internal_edges`, and an in-degree digest (sha256 over `(member, in_degree)` pairs in member
order), followed by every `DirectedImport` in `(importer, provider)` order. It therefore
covers every column 3.2 stores (`representatives` derive from `in_degree`, `boundary` and
`cohesion` from the edges), so 3.2's skip predicate can never freeze an edge-only change.

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
`partition_is_invariant_to_row_order`, `partition_signature_covers_edges` (adding one
import between existing members changes `partition_signature` while every
`member_signature` is unchanged), `build_handles_twenty_thousand_rows`, and the
`#[ignore]` experiment `partition_experiment_reports_distribution`, which reads
`GCODE_EXPERIMENT_DSN` and a project root, loads the live rows through 2.1's
`load_project_imports`, and prints community count, size distribution, largest share,
singletons, and cohesion with and without each split pass.

Threshold spike, inside this leaf: run the experiment on the Gobby checkout and on the
frozen Game Goblins baseline (`0216f1e33f05…` under
`/Users/josh/Projects/wiki-bakeoff-code-2026-09/`, indexed with `gcode index`), record
both distributions in `docs/evidence/community-labels-2026-09/thresholds.md` against
Graphify's baseline (116 communities, largest 3.4% of nodes, median 17.5, 27
singletons), and keep or replace the three constants before the leaf closes. The record,
per corpus: the largest community's share of visible files after splitting against the
25% target, stated as met or exceeded with the cause of an exceedance (a cohesive blob
above `max_size` whose induced subgraph Leiden returns as one child survives the single
non-recursive oversized pass by design, and no constant choice changes that); that no
`community-N` placeholder exists anywhere; and that communities of five or more files on
Gobby carry deterministic labels that name subsystems (`src/gobby/memory`,
`crates/gcode/src/...`, `web/src/...`). An exceedance is recorded with its cause and is
not a close blocker; recursion on children stays rejected above. A replaced constant
updates `cohesion_threshold_uses_integer_math` and the reference table in the same commit.

Verify: `cargo nextest run -p gobby-code -E 'test(partition)'`.

**Acceptance:**

- 2.2.1 - `build_partition` produces one node per visible file and one folded undirected edge per importer/provider pair, excluding external, ambiguous, and self imports. symbol: `build_partition`. file: `crates/gcode/src/communities/partition.rs`.
- 2.2.2 - Oversized and low-cohesion splits apply once each with Graphify's constants in integer math. test: `crates/gcode/src/communities/partition_tests.rs::cohesion_threshold_uses_integer_math`.
- 2.2.3 - Cohesion, in-degree, and the 16-hex member signature are computed per community and the signature matches Graphify's shape. test: `crates/gcode/src/communities/partition_tests.rs::member_signature_matches_graphify_shape`.
- 2.2.4 - Deterministic labels follow the dominant-directory rule with the in-degree fallback for root files. test: `crates/gcode/src/communities/partition_tests.rs::label_prefers_deepest_majority_prefix`.
- 2.2.5 - A provider outside the visible set surfaces as `PartitionError::ProviderNotVisible`; kernel input errors surface as `PartitionError::InvalidGraph`. symbol: `PartitionError`. file: `crates/gcode/src/communities/partition.rs`.
- 2.2.6 - The partition is deterministic under row reordering. test: `crates/gcode/src/communities/partition_tests.rs::partition_is_invariant_to_row_order`.
- 2.2.7 - The threshold spike ran on both corpora before close and `thresholds.md` records, per corpus, the size distribution, the largest community's share of visible files against the 25% target as met or exceeded with the cause of any exceedance, and the constants kept or replaced; an exceedance is a recorded outcome, not a close blocker. behavior: "largest" in `docs/evidence/community-labels-2026-09/thresholds.md`.
- 2.2.8 - Colliding deterministic labels receive ` #2`, ` #3` suffixes in partition order so the largest community keeps the bare label. test: `crates/gcode/src/communities/partition_tests.rs::label_collision_gets_ordinal_suffix`.
- 2.2.9 - `label_candidates` returns up to four distinct strings led by the deterministic label. test: `crates/gcode/src/communities/partition_tests.rs::candidates_are_distinct_and_lead_with_deterministic`.
- 2.2.10 - `partition_signature` changes when an import is added or removed between existing members while every `member_signature` is unchanged. test: `crates/gcode/src/communities/partition_tests.rs::partition_signature_covers_edges`.
- 2.2.11 - `thresholds.md` records that no `community-N` placeholder appears in any stored label or view output on either corpus. behavior: "placeholder" in `docs/evidence/community-labels-2026-09/thresholds.md`.
- 2.2.12 - `thresholds.md` records that communities of five or more files on Gobby carry deterministic labels naming subsystems. behavior: "subsystems" in `docs/evidence/community-labels-2026-09/thresholds.md`.

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
    pub label_attempted_at: Option<DateTime<Utc>>,
}
pub(crate) struct AssignedCommunity { pub community_id: i32, pub matched_prior: Option<i32>, pub partition_index: usize, pub label: LabelCarry }
pub(crate) enum LabelSource { Deterministic, Model }   // crates/gcode/src/communities.rs; as_str/parse map exactly to 'deterministic' | 'model', the 3.1 CHECK set
pub(crate) struct LabelCarry {                         // crates/gcode/src/communities/remap.rs; the four-way carry-forward result for one new community
    pub label: String, pub label_deterministic: String, pub label_source: LabelSource,
    pub label_confidence: Option<f64>, pub label_model: Option<String>,
    pub labeled_signature: Option<String>, pub labeled_at: Option<DateTime<Utc>>,
    pub label_attempted_at: Option<DateTime<Utc>>, pub label_candidates: Vec<String>,
}
pub(crate) fn assign_ids(partition: &ProjectPartition, prior: &[PriorCommunity], watermark: i32) -> (Vec<AssignedCommunity>, i32 /* new watermark */);
```

`LabelSource` lives in `crates/gcode/src/communities.rs` (defined here; 3.2's
`StoredCommunity` and `db/communities.rs` reuse it) and is the only place the two text
values are spelled in Rust; `LabelCarry` is exactly the set of label columns 3.2 writes.

Candidates are every `(new, old)` pair with overlap ≥ 1 and `(overlap, union)`; sort by
Jaccard desc using integer cross-multiplication (`o1*u2` vs `o2*u1`), then overlap desc,
then old id asc, then new index asc; take pairs greedily while both sides are unmatched.
Unmatched new communities, in partition order, receive `max(watermark, max old id) + 1,
+2, …`; the returned watermark is the last id issued (or the input when none). Label
carry-forward per new community: unmatched → deterministic label and candidates from
2.2; matched with equal signature → carry every label field, including
`labeled_signature`, `label_confidence`, `label_model`, `labeled_at`, and
`label_attempted_at`; matched with a changed signature and prior source `deterministic`
→ recompute `label_deterministic` and write the four gate-decision fields
`labeled_signature`, `label_confidence`, `label_model`, and `labeled_at` as `None`, so a
gate decision recorded against the old membership (6.3 stores a rejection as a
deterministic label with the signature stamped) is reopened with no residue and the row
re-enters the label queue, while `label_attempted_at` is carried unchanged: it is the
daemon's failure cooloff (6.1, 6.2), and clearing it on every hook-triggered membership
change would reset the backoff for exactly the communities whose generation keeps
failing; matched with a changed signature and prior source `model` → carry the model
label with its `labeled_signature`, confidence, and model, which the readers interpret
as `label_stale`. `label_candidates` are always recomputed.

Tests in `remap_tests.rs`: `unchanged_partition_keeps_every_id`,
`split_keeps_id_on_larger_child_and_issues_fresh_id`,
`merge_keeps_id_of_larger_parent`, `jaccard_beats_raw_overlap_on_swallow_case`,
`retired_ids_are_never_reissued` (delete a community, add an unrelated one, id is above
the watermark), `renamed_files_keep_id_and_flip_signature`,
`model_label_carries_with_stale_signature`, `deterministic_label_recomputes_on_change`,
`gate_rejected_label_reopens_on_membership_change` (a prior deterministic row with
`labeled_signature`, `label_confidence`, `label_model`, and `label_attempted_at` set
comes back with the four gate-decision fields `None` and `label_attempted_at` unchanged
after a membership change),
`watermark_is_monotone_across_runs`.

Verify: `cargo nextest run -p gobby-code -E 'test(remap)'`.

**Acceptance:**

- 2.3.1 - `assign_ids` matches greedily by Jaccard, overlap, old id, new index and keeps an id on the larger child of a split and the larger parent of a merge. test: `crates/gcode/src/communities/remap_tests.rs::split_keeps_id_on_larger_child_and_issues_fresh_id`.
- 2.3.2 - Unmatched communities take ids above the watermark and a retired id is never reissued. test: `crates/gcode/src/communities/remap_tests.rs::retired_ids_are_never_reissued`.
- 2.3.3 - Label fields carry forward by the four-way rule and a model label with a changed signature is carried as stale. symbol: `assign_ids`. file: `crates/gcode/src/communities/remap.rs`.
- 2.3.4 - A deterministic row whose membership changed comes back with the four fields `labeled_signature`, `label_confidence`, `label_model`, and `labeled_at` cleared and `label_attempted_at` carried unchanged. test: `crates/gcode/src/communities/remap_tests.rs::gate_rejected_label_reopens_on_membership_change`.
- 2.3.5 - `LabelSource` maps exactly to the text values `deterministic` and `model` and round-trips them. symbol: `LabelSource`. file: `crates/gcode/src/communities.rs`.
- 2.3.6 - `LabelCarry` carries every label column the four-way rule assigns, including the recomputed `label_candidates`. symbol: `LabelCarry`. file: `crates/gcode/src/communities/remap.rs`.

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
- `crates/gcode/security/managed_postgres_privileges.json::*` — scope-reason: declare the code_communities relation grant mirroring code_indexed_project_states
- `crates/gcode/src/schema.rs::*` — scope-reason: add the code_communities table contract and required-table entry and the watermark and partition_signature columns on code_indexed_project_states
- `tests/code_index/test_gcode_privilege_manifest.py::*` — scope-reason: add code_communities to the exact managed-relation set asserted by test_manifest_privileges_match_the_managed_relation_set
- `docs/evidence/community-labels-2026-09/schema-apply.md`

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
explicitly, so the new columns survive it. `GCODE_RLS_TABLES` is `[&str; 11]` at
`runner_tests.rs:64`. The privilege manifest's `relations[]` entries look like
`{"relation": "code_imports", "operations": ["SELECT","INSERT","UPDATE","DELETE"], "scope_column": "project_id"}`;
mirror the `code_indexed_project_states` entry. `schema_expected_identity.json` records
`latest_version: 441` and hashes; `crates/gdaemon/tests/cli_contract.rs:58` asserts 441.
`tests/code_index/test_gcode_privilege_manifest.py::test_manifest_privileges_match_the_managed_relation_set`
(:52) asserts `set(relations)` equals an exact fourteen-name literal set, and
`test_manifest_covers_every_rust_database_call_at_head` (:41) compares the source
inventory with a live scan of `crates/gcode/src` and `crates/gcore/src`; the first breaks
when 3.1 adds the fifteenth relation, the second when 3.2 adds `db/communities.rs`.
No derived table exists in the code index today; the only model-derived column is
`code_symbols.summary`. Rejected: an identity sequence on `community_id` (ids are
per-project and issued by the remap watermark); a foreign key from `members` to
`code_indexed_files` (content-versioned keys would cascade on every edit); a
`code_community_members` child table (the read paths always want the whole community;
membership is resolved in memory over the rows already read); an index over `members`
(no specified query filters by member: 4.2, 4.3, and 5.1 scan the rows
`read_for_context` returned, and a GIN rebuild on every replace would count against
Q1.7's budget); a `(machine_id, project_id, member_count DESC, community_id)` index
(every reader loads all rows for the pair, which the primary key already serves as a
prefix scan, and orders in memory; the label queue is served by `idx_cc_label_queue`).

```sql
ALTER TABLE code_indexed_project_states
    ADD COLUMN community_id_watermark integer NOT NULL DEFAULT 0,
    ADD COLUMN partition_signature text;   -- 2.2's partition_signature (member signatures, edges, in-degree, directed imports); NULL until the first refresh

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
CREATE INDEX idx_cc_label_queue ON code_communities (machine_id, project_id)
    WHERE labeled_signature IS DISTINCT FROM member_signature;
```

No query filters on `members`: every membership lookup is an in-memory scan over the rows
`read_for_context` already returned. Mirror the same DDL, RLS block, and GRANTs into
`baseline.sql`.

Carrier regeneration, all in this commit: `catalog.manifest.json` via
`POSTGRES_TEST_DSN=<test dsn> UPDATE_GCORE_SCHEMA_MANIFEST=1 cargo test -p gobby-core --features postgres --test catalog_manifest_freshness`;
`schema_expected_identity.json` via
`uv run python scripts/generate_schema_expected_identity.py --gdaemon target/release/gdaemon`
after `cargo build --release -p gobby-daemon`; the five signed golden files
and `crates/gcore/src/grant/bundle.rs` through their existing regeneration paths;
`schema_contract.rs`, `cli_contract.rs`, `runner_tests.rs` by hand. In gcode's
`schema.rs` add the `code_communities` `TABLE_CONTRACTS` entry, the `REQUIRED_TABLES`
entry, and `community_id_watermark` and `partition_signature` on the
`code_indexed_project_states` contract.

Live: `uv run gobby restart` (plans and applies 442), then `uv run gobby cutover` for the
coherent set, after a `global` announcement, before 3.2 starts: 3.2's refresh writes a
table the installed binaries must already know. Record the `gdaemon schema plan` output
before and after the apply (nothing pending afterwards) in `docs/evidence/community-labels-2026-09/schema-apply.md`,
with the line "landed as migration <N>" naming the number the substitution resolved to.

Granularity: eighteen target files, three of them hand-maintained Rust. A schema change
and its carriers cannot land partially (every carrier test fails until all agree), so
this stays one leaf by construction.

Verify: `cargo nextest run -p gobby-core --features postgres`, `cargo nextest run -p gobby-daemon --test cli_contract`,
`cargo nextest run -p gobby-code -E 'test(schema)'`,
`DATABASE_URL=postgresql://gobby_test:gobby_test@127.0.0.1:60892/gobby_test GOBBY_TEST_PROTECT=1 uv run pytest tests/runtime_grants tests/storage/test_schema_identity.py tests/code_index/test_gcode_privilege_manifest.py -q`
(or the nearest existing schema-identity test module).

**Acceptance:**

- 3.1.1 - The new migration (442, or the next free number at landing) creates `code_communities` with the constraints, the `idx_cc_label_queue` index, machine-scoped RLS policies, and GRANTs above, and adds `community_id_watermark` and `partition_signature` to `code_indexed_project_states`. file: `crates/gcore/assets/schema/migrations/442_add_code_communities.sql`.
- 3.1.2 - `baseline.sql`, `catalog.manifest.json`, `assets.rs::MIGRATIONS`, `crates/gcore/src/grant/bundle.rs`, `schema_contract.rs`, `gdaemon` `cli_contract.rs`, `schema_expected_identity.json`, and the five goldens agree on the new latest version (442, or the next free number at landing). file: `src/gobby/storage/schema_expected_identity.json`.
- 3.1.3 - `GCODE_RLS_TABLES` lists twelve tables and the runner tests assert the machine-scoped predicates on `code_communities`. file: `crates/gcore/src/schema/runner_tests.rs`.
- 3.1.4 - The privilege manifest grants `code_communities` to the gcode capability with the same scope declaration as `code_indexed_project_states`, and gcode's `schema.rs` contracts include the table and both new columns. file: `crates/gcode/security/managed_postgres_privileges.json`.
- 3.1.5 - `uv run gobby restart` applies 442 on the live hub and `gdaemon schema plan` reports nothing pending afterwards, as recorded in the apply log. behavior: "nothing pending" in `docs/evidence/community-labels-2026-09/schema-apply.md`.
- 3.1.6 - The managed-relation set assertion names `code_communities`. test: `tests/code_index/test_gcode_privilege_manifest.py::test_manifest_privileges_match_the_managed_relation_set`.
- 3.1.7 - The migration number actually used replaces every occurrence of the provisional 442 named in Constraints in the same commit, and the apply log and V1 record it. behavior: "landed as migration" in `docs/evidence/community-labels-2026-09/schema-apply.md`.

### 3.2 Persist the partition at index time and expose the read API [category: code] (depends: 2.3, 3.1)
`kind: deliverable`

Targets:
- `crates/gcode/src/communities.rs`
- `crates/gcode/src/communities/refresh_tests.rs`
- `crates/gcode/src/db/communities.rs`
- `crates/gcode/src/db/mod.rs::*` — scope-reason: declare and re-export the communities query module
- `crates/gcode/src/index/indexer/lifecycle.rs::*` — scope-reason: add refresh_communities beside refresh_project_stats and attach_projection_sync, and delete this machine's code_communities rows inside invalidate's transaction
- `crates/gcode/src/index/indexer/pipeline.rs::*` — scope-reason: call refresh_communities at both full and incremental completion sites
- `crates/gcode/src/index/indexer/overlay.rs::*` — scope-reason: call refresh_communities at the overlay completion site under the overlay project id
- `crates/gcode/src/index/indexer/types.rs::*` — scope-reason: add the CommunityRefreshFailed degradation and the optional communities report on IndexOutcome
- `crates/gcode/src/contract.rs::*` — scope-reason: add communities to the index command's JSON output keys
- `crates/gcode/contract/gcode.contract.json::*` — scope-reason: regenerate the pinned contract snapshot from gcode contract
- `tests/contracts/gcode.contract.json::*` — scope-reason: regenerate the vendored contract snapshot to match the crate copy
- `crates/gcode/security/managed_postgres_privileges.json::*` — scope-reason: add the source inventory entry for crates/gcode/src/db/communities.rs
- `docs/evidence/community-labels-2026-09/churn.md`
- `docs/evidence/community-labels-2026-09/refresh-latency.md`

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
partition, and Leiden over ~7,000 nodes is sub-second in Rust). `invalidate`
(`lifecycle.rs:29-52`, reached from `gcode invalidate` and from prune through
`invalidate_project_locked` in `crates/gcode/src/commands/status/invalidate.rs:58-67`) deletes this
machine's `code_indexed_file_states` and `code_indexed_project_states` rows in one
transaction and never touches `code_indexed_projects`, so the table's `ON DELETE CASCADE`
does not fire and, without a matching delete, `code_communities` rows would outlive the
watermark that protects their ids. `project_lock_key` (`crates/gcode/src/index_lock.rs:583-595`)
hashes only the project id, so an overlay run and its parent's run hold different
advisory locks.

`crates/gcode/src/db/communities.rs` (SQL only): `read_project_communities(conn, machine_id, project_id) -> Vec<StoredCommunity>`
and `read_partition_signature(conn, machine_id, project_id) -> Option<String>` for the read
paths, and the transaction-scoped pair `begin_replace(conn, machine_id,
project_id) -> ReplaceTxn<'_>` and `ReplaceTxn::{commit, skip}`. `begin_replace` opens
the transaction, runs `SELECT community_id_watermark, partition_signature FROM
code_indexed_project_states … FOR UPDATE`, and reads the prior `code_communities` rows
`FOR UPDATE` inside it; `commit(rows, watermark, partition_signature)` runs `DELETE
FROM code_communities WHERE machine_id = %s AND project_id = %s`, `INSERT` each row,
`UPDATE code_indexed_project_states SET community_id_watermark = %s,
partition_signature = %s`, and commits; `skip()` rolls back without writes. This is the
one legitimate multi-statement transaction in gcode, and it closes two windows: readers
never observe a half-replaced partition, and the daemon labeler's signature-guarded
`UPDATE` (6.1) can never land between the prior-row read and the `DELETE`. A label
committed before the read is carried forward by 2.3; one issued after the read blocks
on the row lock, matches zero rows once the replace commits, and leaves the row in the
label queue for the next pass. Nothing is discarded silently. `invalidate` gains a third
statement in its existing transaction, `DELETE FROM code_communities WHERE machine_id = $1
AND project_id = $2`, so the rows and the watermark that guards their ids always die
together; the next index run starts from an empty partition and a zero watermark, and
an id may repeat an earlier one only because the whole history was deliberately
discarded (Ask evidence ids also hash the member signature, so a reissued id never
replays as the old community).

`communities.rs`: `StoredCommunity` (every column plus `label_stale: bool` computed as
`label_source == Model && labeled_signature != Some(member_signature)`),
`refresh_project_communities(conn, ctx) -> anyhow::Result<CommunityRefreshReport>`, and
`read_for_context(conn, ctx) -> Vec<StoredCommunity>` (overlay project first, parent
fallback when the overlay has no rows), `partition_refreshed(conn, ctx) -> bool` (true
when `partition_signature` is non-NULL for the project whose rows `read_for_context`
returns; 5.1 uses it to tell a never-refreshed project from a refreshed one with no
communities), and `pub(crate) const MISSING_PARTITION_HINT` (the text 4.2, 4.3, and 5.1
show when no partition is stored). `read_for_context` is the one place the stale rule
is applied: when `label_stale` is true it returns the row with `label` set to
`label_deterministic`, `label_source` set to `Deterministic`, and `label_confidence` and
`label_model` set to `None` (a stale confidence describes a label no longer shown, and
5.1's `CommunityEvidence` carries the field), so every reader (4.2
`ViewCommunity`, 4.4 `GraphReportCommunity`, 5.1 `CommunityEvidence`) shows the
deterministic label with `label_stale: true` without carrying `label_deterministic` in
its payload. The stored row is untouched, and the replace path reads raw rows through
`begin_replace`, never `read_for_context`, so 2.3's carry-forward still sees the stored
model label. The refresh sequence: load imports through 2.1
and build through 2.2 outside any transaction; `begin_replace`; when the
`partition_signature` stored under `ctx.project_id` (never a seeded parent signature; it
is `NULL` on an overlay's first refresh, so that refresh always commits) equals the new
one, `skip()` and report `skipped_unchanged`; else
run 2.3's pure `assign_ids` on the prior rows read inside the transaction, build the
rows (`representatives` are the top five members by in-degree with ties by path;
`boundary` folds `directed` into per-pair import counts between distinct communities),
and `commit`. Overlay projects: when `ctx` resolves to an overlay project that has no
rows of its own, `begin_replace` seeds the prior rows and the watermark from the parent
project (the same parent lookup `read_for_context` uses) inside the same READ COMMITTED
transaction, taking `FOR UPDATE` on the parent's `code_indexed_project_states` row before
reading the parent's `code_communities` rows: the parent's run holds a different advisory
lock, so that row lock is what serializes the seed against the parent's own replace. The
seed never copies the parent's `partition_signature`; the skip compares against the
overlay's own stored value. `assign_ids` then carries the parent's ids and label fields
forward by signature match and only genuinely new communities take fresh ids above the
parent's watermark; the commit writes under the overlay project id. Later overlay refreshes remap against the
overlay's own rows, so a worktree shows the main checkout's ids and model labels as of
its first index, and ids it issues afterwards are overlay-local. The daemon labeler
skips overlay projects (`_run_maintenance` continues on `decision.kind == "overlay"`,
`maintenance.py:107-122`), so overlay rows never gain labels of their own; the seed is
what makes a worktree's view match the main checkout's. `CommunityRefreshReport {
communities, changed, new_ids, retired_ids, skipped_unchanged }`.

Cost: every completed index run, including hook-triggered single-file runs, loads the
active import set, builds the graph, runs Leiden and both splits, and remaps before it
can discover nothing changed; the skip saves the write only. Q1.7 measures that latency
against a budget. If the budget is exceeded, the fix is a digest of the deduped
`(source, module)` row set stored beside `partition_signature` in a follow-up migration
and compared before the graph is built; it is not added now.

Evidence owned by this leaf: the Q1.4 churn experiment (two `gcode index` runs on unchanged
input, a three-file rename inside one community with that community's row first seeded
to `label_source='model'` by direct `UPDATE` because no model label exists before 6.2
runs, one added file, one deleted plus one
unrelated new community, on the Gobby checkout and the frozen Game Goblins baseline) with
its row diffs in `docs/evidence/community-labels-2026-09/churn.md`, and the Q1.7
incremental single-file `gcode index` timing (median of five runs before and after this
leaf, both corpora) against the +250 ms p50 budget in
`docs/evidence/community-labels-2026-09/refresh-latency.md`, naming the digest
contingency when over budget.

`lifecycle::refresh_communities(conn, ctx, outcome)` calls
`refresh_project_communities`, sets `outcome.communities = Some(report)` on success, and
pushes `IndexDegradation::CommunityRefreshFailed { message }` on any error. It is called
after `refresh_project_stats` and before `attach_projection_sync` at the three sites,
including hook-triggered single-file runs. `contract.rs`: the `index` command's JSON
output keys gain `communities`; regenerate both pinned JSON copies (the version stays 10
until 4.3).

Live: coherent-set cutover after landing, announced per Constraints;
`tests/test_cli_contracts.py::test_vendored_cli_contract_matches_real_cli[gcode]`
compares the installed binary with the vendored contract and is green only after it.

Tests in `refresh_tests.rs` against the Postgres test fixture the indexer tests already
use: `refresh_writes_rows_watermark_and_signature`, `unchanged_partition_skips_write`
(`refreshed_at` untouched, stored `partition_signature` compared),
`refresh_failure_degrades_index_outcome`, `read_for_context_prefers_overlay_rows`,
`replace_is_atomic_under_concurrent_read` (a reader inside the replace sees either the
old or the new set), `label_written_during_refresh_is_not_lost` (a label update
committed between the build and `begin_replace` is present on the committed row; one
issued after `begin_replace` blocks, matches zero rows, and leaves the row queued),
`overlay_refresh_seeds_prior_rows_from_parent` (an overlay's first refresh keeps the
parent's ids and model labels for unchanged communities and issues fresh ids above the
parent's watermark), `overlay_first_refresh_commits_when_partition_matches_parent` (an
overlay whose partition equals the parent's commits rows under the overlay id on its
first refresh and skips on its second), `overlay_seed_locks_parent_state_row` (a seed
started while a parent replace holds the row lock blocks until the parent commits and
reads the committed rows), `invalidate_removes_community_rows_with_project_state`
(after `invalidate` neither table holds a row for the machine and project), `edge_change_without_membership_change_rewrites_rows` (adding one
import between existing members of one community rewrites its `internal_edges`,
`cohesion`, `representatives`, and `boundary` with every `member_signature` unchanged),
`stale_model_label_reads_as_deterministic` (a row with `label_source = 'model'` and a
`labeled_signature` that differs from `member_signature` comes back from
`read_for_context` with the deterministic label, `label_source` deterministic,
`label_confidence` and `label_model` `None`, and `label_stale` true, while the stored row
still holds the model label, confidence, and model).

Verify: `cargo nextest run -p gobby-code -E 'test(communities) | test(index)'`,
`cargo nextest run -p gobby-code --test contract`, and
`DATABASE_URL=postgresql://gobby_test:gobby_test@127.0.0.1:60892/gobby_test GOBBY_TEST_PROTECT=1 uv run pytest tests/test_cli_contracts.py tests/code_index/test_gcode_privilege_manifest.py -q`
after the coherent-set cutover.

**Acceptance:**

- 3.2.1 - `gcode index` (full, incremental, hook, and overlay runs) refreshes `code_communities` after project stats and before projection sync, reading the prior rows under row locks and replacing them in the same transaction that advances the watermark and writes the partition signature. symbol: `refresh_project_communities`. file: `crates/gcode/src/communities.rs`.
- 3.2.2 - A partition signature equal to the stored `partition_signature` skips the write. test: `crates/gcode/src/communities/refresh_tests.rs::unchanged_partition_skips_write`.
- 3.2.3 - A refresh failure degrades the outcome with `CommunityRefreshFailed` and never fails the index run. test: `crates/gcode/src/communities/refresh_tests.rs::refresh_failure_degrades_index_outcome`.
- 3.2.4 - `read_for_context` returns overlay rows first and parent rows as fallback, with `label_stale` derived from the signatures. symbol: `read_for_context`. file: `crates/gcode/src/communities.rs`.
- 3.2.5 - `IndexOutcome.communities` is additive and absent when `None`; the `index` contract keys and both pinned JSON copies include it. file: `crates/gcode/contract/gcode.contract.json`.
- 3.2.6 - The privilege manifest's source inventory lists `crates/gcode/src/db/communities.rs`. file: `crates/gcode/security/managed_postgres_privileges.json`.
- 3.2.7 - A label the daemon writes while a refresh is in flight is either carried forward or left queued; it is never discarded. test: `crates/gcode/src/communities/refresh_tests.rs::label_written_during_refresh_is_not_lost`.
- 3.2.8 - An overlay project's first refresh seeds ids, watermark, and label fields from the parent project. test: `crates/gcode/src/communities/refresh_tests.rs::overlay_refresh_seeds_prior_rows_from_parent`.
- 3.2.9 - An import added or removed between existing members rewrites the rows with new `internal_edges`, `cohesion`, `representatives`, and `boundary` although no `member_signature` changed. test: `crates/gcode/src/communities/refresh_tests.rs::edge_change_without_membership_change_rewrites_rows`.
- 3.2.10 - `read_for_context` returns a stale model-labeled row with `label` rewritten to the deterministic label, `label_source` deterministic, `label_confidence` and `label_model` `None`, and `label_stale` true, leaving the stored row unchanged. test: `crates/gcode/src/communities/refresh_tests.rs::stale_model_label_reads_as_deterministic`.
- 3.2.11 - The Q1.4 churn experiment ran on both corpora before close, with one row seeded to `label_source='model'` by direct `UPDATE` before the rename step so the `label_stale` flip is observable, and its row diffs are recorded. behavior: "member_signature" in `docs/evidence/community-labels-2026-09/churn.md`.
- 3.2.12 - Incremental single-file index latency before and after this leaf is recorded against the +250 ms p50 budget on both corpora. behavior: "p50" in `docs/evidence/community-labels-2026-09/refresh-latency.md`.
- 3.2.13 - `invalidate` removes this machine's `code_communities` rows in the same transaction that removes its `code_indexed_project_states` row. test: `crates/gcode/src/communities/refresh_tests.rs::invalidate_removes_community_rows_with_project_state`.
- 3.2.14 - An overlay's first refresh commits rows under the overlay id even when its partition equals the parent's, because the skip compares against the overlay's own stored signature. test: `crates/gcode/src/communities/refresh_tests.rs::overlay_first_refresh_commits_when_partition_matches_parent`.
- 3.2.15 - The overlay seed reads the parent's rows under `FOR UPDATE` on the parent's state row in one READ COMMITTED transaction and observes a concurrent parent replace only after it commits. test: `crates/gcode/src/communities/refresh_tests.rs::overlay_seed_locks_parent_state_row`.

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
- 4.1.2 - Every existing projection test passes without edits and the moved types keep their names. test: `crates/gcode/src/cli/tests/projection.rs::graph_view_requires_typed_compatible_selector`.

### 4.2 Label MCG nodes from stored rows and extend the view payload shapes [category: code] (depends: 3.2)
`kind: deliverable`

Targets:
- `crates/gcode/src/commands/graph/view/mcg.rs::*` — scope-reason: replace assign_leiden_communities with label_communities reading stored rows and drop the analytics imports
- `crates/gcode/src/commands/graph/view/mcg/fetch.rs::*` — scope-reason: read stored communities in run and pass them to label_communities
- `crates/gcode/src/commands/graph/view/mcg/tests.rs::*` — scope-reason: rewrite the clustering test against stored rows and the missing-partition hint
- `crates/gcode/src/commands/graph/view/render.rs::*` — scope-reason: reshape ViewCommunity, add NodeKind::Community and ViewEdge.count, validate community membership in build_view_payload, group Mermaid nodes into community subgraphs and label edges, delete analytics_graph_from_payload
- `crates/gcode/src/commands/graph/view/render_tests.rs::*` — scope-reason: cover the new ViewCommunity fields, the Community node kind, edge counts, subgraph rendering, and payload validation
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
`community: None` and `hint = Some(MISSING_PARTITION_HINT.to_string())`, the constant 3.2
declares in `crates/gcode/src/communities.rs` ("No stored import
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
or a node's `community` names an unlisted community; `render_mermaid` takes the
`communities` and, for every community whose `nodes` is non-empty, emits one
`subgraph community_<id>["<label>"]` block containing those nodes (Mermaid's native
grouping construct), leaves unclustered nodes at top level, emits every edge after the
subgraph blocks so cross-subgraph edges route normally, and prints `IMPORTS (12)` when
`count` is set; node text stays the bare name, since the subgraph title carries the
label. Labels are quoted and Mermaid-escaped (`"` and `]`), and the subgraph id derives
from the numeric community id, never the label. A community whose `nodes` is empty
(the list view in 4.3) emits no subgraph, so that view renders as before.

Tests: rewrite `mcg_assigns_leiden_communities_on_scoped_imports` as
`mcg_labels_nodes_from_stored_communities` (file nodes carry the stored label, a
uniquely resolved module inherits its provider's label, an external module has
`community: None`, `communities[].nodes` are view ids while `size` is project-level, the
label appears as a Mermaid subgraph title) and add `mcg_without_stored_partition_hints_and_leaves_null`.
`render_tests.rs`: `payload_rejects_unknown_community_member`,
`community_node_has_null_file`, `edge_count_renders_in_mermaid`,
`mermaid_groups_nodes_into_community_subgraphs` (two communities and one unclustered
node: two subgraph blocks titled by label, the unclustered node at top level, a
cross-subgraph edge after the blocks, a label containing `"` escaped), and the
`Communities` arm in the view-kind loop (relation `IMPORTS`).

Verify: `cargo nextest run -p gobby-code -E 'test(mcg) | test(render) | test(view)'`.

**Acceptance:**

- 4.2.1 - MCG file and uniquely resolved module nodes carry the stored community label; external and ambiguous modules carry none; no `graph_analytics` import remains in `mcg.rs`. symbol: `label_communities`. file: `crates/gcode/src/commands/graph/view/mcg.rs`.
- 4.2.2 - With no stored rows every node's `community` is null and the payload carries the missing-partition hint. test: `crates/gcode/src/commands/graph/view/mcg/tests.rs::mcg_without_stored_partition_hints_and_leaves_null`.
- 4.2.3 - `ViewCommunity` carries id, label, size, cohesion, label_source, label_stale, and view-scoped nodes; `build_view_payload` rejects membership outside `nodes[]`. test: `crates/gcode/src/commands/graph/view/render_tests.rs::payload_rejects_unknown_community_member`.
- 4.2.4 - `NodeKind::Community` exists with a null file and the `community:` key prefix, `ViewEdge.count` is optional, and `analytics_graph_from_payload` is deleted. symbol: `NodeKind`. file: `crates/gcode/src/commands/graph/view/render.rs`.
- 4.2.5 - Mermaid output groups clustered nodes into one subgraph per community titled by its label, with unclustered nodes at top level and edges after the blocks. test: `crates/gcode/src/commands/graph/view/render_tests.rs::mermaid_groups_nodes_into_community_subgraphs`.

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
- `docs/guides/gcode-user-guide.md`
- `crates/gcode/Cargo.toml`
- `Cargo.lock`
- `docs/evidence/community-labels-2026-09/view-latency.md`

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
`tests/contracts/gcode.contract.json` (memory `833f0b1a`). `docs/contracts/gcode-cli.md:8`
is the literal `contract_version` line and :10 the Version 10 paragraph;
`docs/guides/gcode-user-guide.md:352-353` enumerates exactly the three existing view kinds. `crates/gcode/Cargo.toml:3` is `version = "1.8.0"`;
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
`.required(false)` and gains `--community <ID|LABEL|PATH>`; `--min-size N` parsed with
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
`communities[]` one entry per listed row with `nodes` empty (member ids are not view
nodes here, and `build_view_payload` rejects membership outside `nodes[]`; membership
comes from the detail form or the 5.1 evidence operation);
`seed = { id: project_id, name: project_root, kind: "project", file: None }`; the
missing-partition hint from 4.2 when there are no rows. Detail: the selector is resolved
by a ladder of rungs, each an in-memory scan over the rows `read_for_context` already
returned: an integer id; else a case-insensitive exact `label`; else an exact
`label_deterministic`; else, when it contains a path separator or equals a stored member
exactly, the community whose `members` contain it (a path belongs to at most one
community); else a case-insensitive substring of `label` or `label_deterministic`
(members are never substring-matched). The first rung with at least one match decides.
At every rung more than one match is a typed exit-2 error listing up to five matches
(model labels carry no cross-row uniqueness, so even the exact-label rung can tie), and a
selector that parses as an integer but matches no stored `community_id`, a path that
belongs to no community, and a selector matching nothing at any rung are each a typed
exit-2 error naming the selector. Nodes: the community, up to 50 members as `File` nodes
(representatives first, then path asc), up to 12 neighbors by `import_count` desc;
edges `CONTAINS` to members and `IMPORTS` with counts to neighbors;
`outgoing_truncated`/`incoming_truncated` carry the caps. Mermaid renders through
`render_mermaid` unchanged; text keeps `print_view`.

Contract: `contract.rs` `--view` value name `fcg|mcg|class-hierarchy|communities`,
`allowed_values` gains `"communities"`, `FlagContract::value("--min-size", "N")` and
`FlagContract::value("--community", "ID|LABEL|PATH")` after `--outgoing-limit`, summary
"Render a scoped fcg, mcg, or class-hierarchy graph view, or project-wide import
communities", `contract_version: 11`. Regenerate both pinned JSON copies; rename the
test to `contract_is_version_eleven_with_project_import_communities` and pin 11 at both
assertions; pin 11 in `tests/test_cli_contracts.py`. `docs/contracts/gcode-cli.md`: the
`contract_version` line at :8 becomes 11, plus a "Version 11" paragraph (persisted per-machine communities, seedless view, `--min-size`,
`--community` by id, label, or member path, `communities[]` shape with `label`, `size`, `cohesion`, `label_source`,
`label_stale`, `community:<id>` node ids, `edges[].count`, the `index` `communities`
report, and the `communities` evidence operation named ahead of 5.1) plus the
`graph view` bullet. `docs/guides/gcode-user-guide.md:352-353` names `--view communities`
as the fourth kind with its seedless form and `--community`. Bump `crates/gcode/Cargo.toml`
to 1.9.0 and refresh `Cargo.lock`.

Tests: `crates/gcode/src/cli/tests/projection.rs` — `communities_parses_without_seed`,
`communities_parses_min_size_and_community_selectors` (`--min-size 5`, `--community memory`,
`--community crates/gcode/src/lib.rs`), `communities_rejects_other_seeds_depth_and_row_limits`
(`--file`, `--symbol`, `--depth`, `--incoming-limit`), `min_size_rejected_off_communities_and_at_zero`
(`--min-size` with `mcg`, `--min-size 0`), `effective_min_size_defaults_to_two`,
`seeded_views_still_require_a_seed` (mcg/fcg/class-hierarchy).
`crates/gcode/src/commands/graph/view/tests.rs` — `run_routes_every_view_and_seed_combination` (table-driven over the
match, including `(Communities, None)`). `crates/gcode/src/commands/graph/view/communities/tests.rs` —
`min_size_hides_singletons_by_default`, `list_edges_are_between_listed_communities_only`,
`detail_resolves_id_label_and_unique_substring`, `detail_resolves_member_path`,
`detail_unknown_path_is_typed_error`, `detail_ambiguous_substring_is_typed_error`,
`detail_no_match_and_multi_match_are_typed_errors_at_every_rung` (an unknown integer id,
a selector matching nothing, two rows sharing an exact label, and two deterministic
labels sharing a substring),
`detail_truncates_members_and_neighbors_with_flags`, `list_without_rows_hints`,
`mermaid_validates_for_list_and_detail`.

Granularity: seventeen target files, three of them hand-maintained Rust besides the new
view module. A contract bump cannot land partially: the five pins, the flags, and the
view they describe must agree in one commit, so this stays one leaf.

Verify: `cargo nextest run -p gobby-code`, `cargo nextest run -p gobby-code --test contract`,
`cargo clippy -p gobby-code`, `cargo fmt -p gobby-code -- --check`,
`DATABASE_URL=postgresql://gobby_test:gobby_test@127.0.0.1:60892/gobby_test GOBBY_TEST_PROTECT=1 uv run pytest tests/test_cli_contracts.py -q`
after the coherent-set cutover.

**Acceptance:**

- 4.3.1 - `--view=communities` parses without a seed and accepts `--min-size` and `--community`. test: `crates/gcode/src/cli/tests/projection.rs::communities_parses_without_seed`.
- 4.3.2 - The list view emits one node per listed community, inter-community `IMPORTS` edges with counts, one `communities[]` entry per listed row with empty `nodes`, and valid Mermaid, reading stored rows only. symbol: `run_list`. file: `crates/gcode/src/commands/graph/view/communities.rs`.
- 4.3.3 - The detail view resolves an integer id, a case-insensitive exact label, an exact deterministic label, or a unique case-insensitive substring of `label` or `label_deterministic`, never of members. test: `crates/gcode/src/commands/graph/view/communities/tests.rs::detail_resolves_id_label_and_unique_substring`.
- 4.3.4 - Routing covers every view and seed combination including the seedless form. test: `crates/gcode/src/commands/graph/view/tests.rs::run_routes_every_view_and_seed_combination`.
- 4.3.5 - The contract advertises `communities`, `--min-size`, and `--community` at version 11 in `contract.rs`, both pinned JSON copies, the Rust and Python pins, and `docs/contracts/gcode-cli.md`. file: `tests/contracts/gcode.contract.json`.
- 4.3.6 - The gcode crate version is 1.9.0. behavior: "1.9.0" in `crates/gcode/Cargo.toml`.
- 4.3.7 - `--community <path>` resolves to the community whose members contain the path. test: `crates/gcode/src/commands/graph/view/communities/tests.rs::detail_resolves_member_path`.
- 4.3.8 - The detail view caps members at 50 and neighbors at 12 and sets `outgoing_truncated`/`incoming_truncated` when it does. test: `crates/gcode/src/commands/graph/view/communities/tests.rs::detail_truncates_members_and_neighbors_with_flags`.
- 4.3.9 - A path that belongs to no community is a typed exit-2 error naming it. test: `crates/gcode/src/commands/graph/view/communities/tests.rs::detail_unknown_path_is_typed_error`.
- 4.3.10 - An ambiguous substring is a typed exit-2 error listing up to five matches. test: `crates/gcode/src/commands/graph/view/communities/tests.rs::detail_ambiguous_substring_is_typed_error`.
- 4.3.11 - A project with no stored rows lists nothing and carries the missing-partition hint. test: `crates/gcode/src/commands/graph/view/communities/tests.rs::list_without_rows_hints`.
- 4.3.12 - `--view=communities` rejects other seeds, `--depth`, and row limits with typed clap conflicts. test: `crates/gcode/src/cli/tests/projection.rs::communities_rejects_other_seeds_depth_and_row_limits`.
- 4.3.13 - `--min-size` is rejected off communities and at zero. test: `crates/gcode/src/cli/tests/projection.rs::min_size_rejected_off_communities_and_at_zero`.
- 4.3.14 - `docs/contracts/gcode-cli.md` states `contract_version`: 11 at its version line and `docs/guides/gcode-user-guide.md` names the communities view kind with its seedless form. behavior: "--view communities" in `docs/guides/gcode-user-guide.md`.
- 4.3.15 - `gcode graph view --view communities` on the Gobby checkout completes in under one second from stored rows over five runs. behavior: "under one second" in `docs/evidence/community-labels-2026-09/view-latency.md`.
- 4.3.16 - At every rung a selector that matches nothing, an unknown integer id included, is a typed exit-2 error naming it, and more than one match, two rows sharing an exact label included, is a typed exit-2 error listing up to five matches. test: `crates/gcode/src/commands/graph/view/communities/tests.rs::detail_no_match_and_multi_match_are_typed_errors_at_every_rung`.

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
- `crates/gcode/src/evidence/tests.rs::*` — scope-reason: extend FakeFacts and cover list, detail, empty, never-refreshed, ambiguous, and truncation outcomes
- `crates/gcode/src/codewiki_facts/mod.rs::*` — scope-reason: declare the communities facts module
- `crates/gcode/src/codewiki_facts/communities.rs`
- `crates/gcode/src/commands/evidence.rs::*` — scope-reason: accept the new operation where the command matches on it
- `crates/gcode/tests/evidence.rs::*` — scope-reason: add an end-to-end communities request through --request-json
- `crates/gcode/src/contract/schema.rs::*` — scope-reason: add the community item keys to evidence_keys
- `crates/gcode/contract/gcode.contract.json::*` — scope-reason: regenerate the pinned contract snapshot from gcode contract
- `tests/contracts/gcode.contract.json::*` — scope-reason: regenerate the vendored contract snapshot to match the crate copy
- `crates/gcode/tests/contract.rs::*` — scope-reason: assert the communities operation and item keys in the evidence contract test
- `crates/gcode/src/evidence/search.rs::*` — scope-reason: add the Community arm to the search-lane eligibility retain (a community is not a file-scoped item, so it is dropped from search results)

Research context: `EVIDENCE_SCHEMA_VERSION` is 1 (`crates/gcode/src/evidence/contracts.rs:3`);
`EvidenceOperation` (:99-103) is a three-variant `deny_unknown_fields` enum internally tagged
on `operation`, so a request carries `"operation": "communities"` as a string with the
selector as a sibling key (`EvidenceAdmission.query` builds exactly that flat shape at
`src/gobby/ask/evidence.py:240-246`);
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
pub struct CommunityFact {   // crates/gcode/src/codewiki_facts/communities.rs, beside FileFact (scope.rs) and ScopedGraph (graph.rs)
    pub community_id: i32, pub label: String, pub label_source: String, pub label_confidence: Option<f64>,
    pub label_stale: bool, pub size: usize, pub cohesion: f64, pub internal_edges: usize,
    pub member_signature: String, pub members: Vec<String>, pub representatives: Vec<String>,
    pub boundary: Vec<(i32, usize)>,   // (other_community_id, import_count); the label resolves from the same Vec
}
pub struct ProjectCommunities { pub refreshed: bool, pub communities: Vec<CommunityFact> }   // refreshed = 3.2's partition_refreshed for the same project
```

The selector field is named `path` so Ask's `_iter_paths` admission check covers it.
`CommunityFact` is one stored row as the evidence layer sees it, built from
`StoredCommunity` by `CodewikiFacts::project_communities`; its `label`, `label_source`,
and `label_stale` come from `read_for_context`, which already applies the stale rule
(3.2), so a stale row's item carries the deterministic label.
`canonical_key` follows the existing arms in `crates/gcode/src/evidence/contracts.rs:238-250`: a
leading variant discriminator (`"2"`, after `Source` `"0"` and `Graph` `"1"`) and every
numeric component zero-padded as `{:020}`, so the arm is
`format!("2\0{:020}\0{:020}\0{}", usize::MAX - size, community_id, evidence_id)`; without
the discriminator community items interleave with source and graph items, and without
the padding `usize::MAX - size` sorts by digit count. Outcomes: zero stored rows →
`CompleteEmpty`, with the warning `community_partition_missing` carrying
`MISSING_PARTITION_HINT` when `refreshed` is false (never refreshed; a refreshed project
with no internal imports reports plain `CompleteEmpty`), never `IndexUnavailable`, which
is `EvidenceError::IndexUnavailable` with a fixed connection-recovery text in
`crates/gcode/src/commands/evidence.rs` that cannot describe an empty partition, and the Constraints
make zero stored communities a data condition; no match or all below `min_size` →
`CompleteEmpty`; more than `limit` → `TruncatedIndex`. Detail mode (any of
`community_id`, `label`, `path` set) resolves the selector with 4.3's ladder over the
same fields (`label` walks the exact-label, exact-deterministic, and
`label`/`label_deterministic` substring rungs; members are never substring-matched) and
returns one item with members bounded by `max_members`; when more than one row matches at
the deciding rung every match is returned with the warning `community_selector_ambiguous`
(the evidence layer reports data conditions, it does not error), and no match at any
rung is `CompleteEmpty`; a member missing from the snapshot is dropped
with warning `community_member_not_in_snapshot` while `size` still counts it. Add
`fn project_communities(&self) -> anyhow::Result<ProjectCommunities>` to `EvidenceFacts`
(required; `FakeFacts` supplies a fixture), implement it for `CodewikiFacts` in
`crates/gcode/src/codewiki_facts/communities.rs` through `communities::read_for_context` and
`communities::partition_refreshed`, execute in
`crates/gcode/src/evidence/communities.rs`, dispatch from `crates/gcode/src/evidence/mod.rs`, extend `evidence_keys()`
with the community item keys, and regenerate both pinned JSON copies.

Live: coherent-set cutover after landing, announced per Constraints, for the same
reason as 3.2: the vendored-contract test compares the installed binary.

Tests: `crates/gcode/src/evidence/tests.rs` — `communities_list_orders_by_size_then_id`
(fixture sizes straddle a power of ten, for example 9, 10, 11, and 100, so a digit-count
ordering fails), `communities_detail_bounds_members_and_flags_truncation`,
`communities_without_rows_is_complete_empty_with_hint`,
`communities_refreshed_but_empty_is_complete_empty`,
`communities_ambiguous_label_returns_every_match_with_warning`,
`communities_below_min_size_is_complete_empty`,
`communities_evidence_id_survives_relabel`, `communities_selector_rejects_two_keys`;
`crates/gcode/tests/evidence.rs` — `evidence_communities_request_json_round_trips`.

Verify: `cargo nextest run -p gobby-code -E 'test(evidence)'`, `cargo nextest run -p gobby-code --test evidence --test contract`,
and `DATABASE_URL=postgresql://gobby_test:gobby_test@127.0.0.1:60892/gobby_test GOBBY_TEST_PROTECT=1 uv run pytest tests/test_cli_contracts.py -q`
after the coherent-set cutover.

**Acceptance:**

- 5.1.1 - `gcode evidence --request-json` accepts the flat internally tagged request `{"schema_version": 1, "binding": …, "operation": "communities", "communities": {…}, "max_bytes": …}`, with the selector as a sibling of `operation`, and returns `Community` items in list and detail modes. symbol: `CommunitiesSelector`. file: `crates/gcode/src/evidence/contracts.rs`.
- 5.1.2 - `evidence_id` is derived from binding, community id, and member signature so it survives relabeling. test: `crates/gcode/src/evidence/tests.rs::communities_evidence_id_survives_relabel`.
- 5.1.3 - Zero stored rows on a never-refreshed project report `CompleteEmpty` with the `community_partition_missing` warning, below-threshold results report `CompleteEmpty`, and over-limit results report `TruncatedIndex`; no community data condition is an error. test: `crates/gcode/src/evidence/tests.rs::communities_without_rows_is_complete_empty_with_hint`.
- 5.1.4 - `EvidenceFacts::project_communities` is required and implemented for `CodewikiFacts` and `FakeFacts`. symbol: `EvidenceFacts`. file: `crates/gcode/src/evidence/mod.rs`.
- 5.1.5 - `evidence_keys()`, the evidence contract test, and both pinned JSON copies list the community item keys. file: `crates/gcode/src/contract/schema.rs`.
- 5.1.6 - `CommunityFact` is the row shape `project_communities` returns inside `ProjectCommunities` and carries the read-path label fields. symbol: `CommunityFact`. file: `crates/gcode/src/codewiki_facts/communities.rs`.
- 5.1.7 - A refreshed project with no stored communities reports `CompleteEmpty` without the missing-partition warning. test: `crates/gcode/src/evidence/tests.rs::communities_refreshed_but_empty_is_complete_empty`.
- 5.1.8 - Detail mode resolves `label` through 4.3's ladder over `label` and `label_deterministic`, never members, returns every match with the `community_selector_ambiguous` warning when more than one row matches, and reports `CompleteEmpty` for no match. test: `crates/gcode/src/evidence/tests.rs::communities_ambiguous_label_returns_every_match_with_warning`.
- 5.1.9 - `canonical_key` for a community item leads with its variant discriminator and zero-pads size and id, so community items sort after source and graph items and by size across a power of ten. test: `crates/gcode/src/evidence/tests.rs::communities_list_orders_by_size_then_id`.

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
- `docs/guides/ask.md`
- `src/gobby/install/shared/skills/gobby/references/code-index/ask.md`
- `docs/guides/code-index.md`

Research context: `EvidenceAdmission.__init__` (`src/gobby/ask/evidence.py:117-146`)
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
`docs/contracts/gcode-cli.md:153-167` enumerates the operations, and `docs/guides/ask.md:155`
enumerates them in prose as "search, read, graph". Rejected: a
`CommunityCitation` (communities orient; answers cite `read` items from their members);
admitting `communities` only in interactive mode (the pipeline stages are where a
subsystem overview saves the most retrieval budget).

Add `"communities"` to the permitted set in `evidence.py` and `stage_runtime.py`, to
both `Literal`s, and to the tool descriptions with the sentence "communities orients
you; it is not citable, cite `read` items from its members". `_normalize_selector`
defaults `min_size` to 2 and `max_members` to 50 and rejects more than one of
`community_id`, `label`, `path`. `CommunityEvidenceItem` mirrors the Rust struct field
for field and joins the union with `item_type: Literal["community"]` (the field name mirrors
the Rust `#[serde(tag = "item_type")]` discriminator that `validation_models.py:94-97` keys
on; a member declared under any other name has no discriminator and fails at model build);
`validation.py` adds
an `isinstance(item, CommunityEvidenceItem)` arm that records the item in the manifest
and rejects any citation that points at it. Docs: `ask.md` gains a paragraph on the
operation and its non-citability; `gcode-cli.md` gains the `communities` bullet in the
operations list (the Version 11 paragraph from 4.3 already names it); `docs/guides/ask.md:155`
gains `communities` in its enumeration; the agent-facing skill reference
`src/gobby/install/shared/skills/gobby/references/code-index/ask.md` ("Search, read, graph, and
commit-patch evidence without starting an Ask run") and `docs/guides/code-index.md` each
gain `communities` in their operation enumeration with the non-citability sentence.

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
- 5.2.5 - The user guide's evidence-operation enumeration names `communities`. behavior: "communities" in `docs/guides/ask.md`.
- 5.2.6 - The code-index ask reference and the code-index guide enumerate `communities` with its non-citability sentence. behavior: "communities" in `src/gobby/install/shared/skills/gobby/references/code-index/ask.md`.

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
reads; two tables would need a join and a second lifecycle); rendering the new settings in
`web/src/components/settings/sections/RuntimeInfrastructureSection.tsx` beside the
hardcoded `code_index.symbol_summary.*` paths (a recorded scope decision: the settings UI
is out of scope for this epic and `community_label` is daemon-config-only, edited in the
runtime config file; the regenerated contract and codec vectors still carry the fields,
so a later UI leaf needs no schema work).

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

Live: `uv run gobby restart` after landing, announced with a `global` `send_message`
per Constraints; the daemon loads config and storage code at start.

Tests: `tests/code_index/test_code_index_storage.py` —
`test_get_unlabeled_communities_respects_signature_and_cooloff`,
`test_update_community_label_is_signature_guarded`.

Verify: `DATABASE_URL=postgresql://gobby_test:gobby_test@127.0.0.1:60892/gobby_test GOBBY_TEST_PROTECT=1 uv run pytest tests/code_index/test_code_index_storage.py tests/config -q`.

**Acceptance:**

- 6.1.1 - `CodeIndexCommunityStorageMixin` queues by signature mismatch with a cooloff and writes labels only when the member signature still matches. test: `tests/code_index/test_code_index_storage.py::test_update_community_label_is_signature_guarded`.
- 6.1.2 - `CodeIndexCommunityLabelConfig` exists with the fields above, `decisions_api_key` classifies as a secret, and the runtime config contract, codec vectors, and audit rows are regenerated. symbol: `CodeIndexCommunityLabelConfig`. file: `src/gobby/config/code_index.py`.
- 6.1.3 - `StoredCommunity` round-trips a `code_communities` row. symbol: `StoredCommunity`. file: `src/gobby/code_index/models.py`.

### 6.2 Generate and validate community names in the maintenance loop [category: code] (depends: 3.2, 6.1)
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
`if summarizer: await _summarize_unsummarized(...)` (:195-201);
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
`CommunityLabeler(llm_service, config)` with `generate_batch(communities:
Sequence[StoredCommunity]) -> dict[int, GeneratedLabel]` calling `call_json_feature`
with schema `{name: string 3–40, rationale: string ≤ 120}`. `GeneratedLabel` is a frozen
dataclass in `community_labeler.py`: `name: str` (schema-validated, pre-sanitation),
`rationale: str` (used only as the `generated` option's description for 6.3 and never
persisted), and `model: str` (the generation profile's resolved model, written to
`label_model` on the ungated path). The prompt is built from
stored row fields only: `representatives`, `label_deterministic`, `label_candidates`,
`member_count`, and member paths capped at 40 (representatives first, then path
ascending), all marked as untrusted data, and asks for a 2–5 word purpose name;
`rationale` is never persisted. No symbol read: `code_communities` stores paths, not
symbols, and whether symbols would label better is exactly what Q1.6's D/G comparison
measures; a `code_symbols` read is follow-up work only if that comparison says paths
label poorly. Every outcome names its queue-exit field, because the queue is
`labeled_signature IS DISTINCT FROM member_signature` with a `label_attempted_at` cooloff
(6.1) and a row that sets neither is regenerated on every pass: a validated name equal to
any deterministic candidate skips the gate (6.3) and writes `label_source='deterministic'`,
`label_model` = the generation profile (so Q1.6 can count the arm), `label_confidence`
`NULL`, and `labeled_signature` = the member signature (`written_deterministic`; the row
leaves the queue). Without a gate configured, a validated name is written as
`label_source='model'`, confidence `NULL`, `label_model` = the generation profile,
`labeled_signature` = the member signature (`written_model`; the row leaves the queue). A
schema rejection, a sanitation rejection, and a generation failure each stamp
`label_attempted_at` (300-second cooloff via the storage default) and change nothing else
(`schema_rejected`, `sanitation_rejected`, `generation_failed`; the row stays queued and
is retried after the cooloff). Every outcome emits one structured log event
`code_index.community_label.outcome` with fields `project_id`, `community_id`,
`member_signature`, and `outcome` in `schema_rejected`, `sanitation_rejected`,
`generation_failed`, `written_deterministic`, `written_model` (6.3 adds the gate
values), so Q1.6's schema-rejection rate and arm counts come from one log query. Wire `_label_unlabeled_communities(context, project, labeler,
batch_size)` after the summary block with `community_labeler: CommunityLabeler | None =
None` and `community_label_batch_size: int = 10` keyword-only on `_run_maintenance` and
the loop; construct the labeler in `runner_startup_code_index.py` when
`config.code_index.community_label.enabled`.

Live: `uv run gobby restart` after landing, announced with a `global` `send_message`
per Constraints.

Tests: `tests/code_index/test_community_labeler.py` —
`test_sanitize_rejects_paths_fences_and_long_names`,
`test_generated_name_equal_to_candidate_writes_deterministic`,
`test_generation_failure_stamps_attempt_only`, `test_rejections_stamp_attempt_only`,
`test_every_outcome_emits_one_log_event`,
`test_ungated_validated_name_writes_model_label`;
`test_code_index_maintenance.py` — `test_maintenance_labels_unlabeled_communities`.

Verify: `DATABASE_URL=postgresql://gobby_test:gobby_test@127.0.0.1:60892/gobby_test GOBBY_TEST_PROTECT=1 uv run pytest tests/code_index/test_community_labeler.py tests/code_index/test_code_index_maintenance.py -q`,
`uv run ruff check src/ && uv run mypy src/`.

**Acceptance:**

- 6.2.1 - The maintenance pass labels queued communities after symbol summaries with keyword-only wiring that leaves existing call sites untouched. symbol: `_label_unlabeled_communities`. file: `src/gobby/code_index/maintenance.py`.
- 6.2.2 - Generated names pass schema validation and sanitation before any write, and a name equal to a deterministic candidate is stored as deterministic with `label_model` set to the generation profile and `labeled_signature` equal to the member signature, so the row leaves the label queue. test: `tests/code_index/test_community_labeler.py::test_generated_name_equal_to_candidate_writes_deterministic`.
- 6.2.3 - A generation failure stamps `label_attempted_at` and changes no label field. test: `tests/code_index/test_community_labeler.py::test_generation_failure_stamps_attempt_only`.
- 6.2.4 - Every labeling outcome emits one `code_index.community_label.outcome` log event with a discriminator value. test: `tests/code_index/test_community_labeler.py::test_every_outcome_emits_one_log_event`.
- 6.2.5 - With no `decisions_api_base` configured a validated name is written with `label_source='model'`, `label_confidence` NULL, `label_model` set to the generation profile, and `labeled_signature` equal to the member signature. test: `tests/code_index/test_community_labeler.py::test_ungated_validated_name_writes_model_label`.
- 6.2.6 - `GeneratedLabel` carries the validated name, the never-persisted rationale, and the generation model. symbol: `GeneratedLabel`. file: `src/gobby/code_index/community_labeler.py`.
- 6.2.7 - A schema rejection and a sanitation rejection each stamp `label_attempted_at` and change no label field, so the row waits out the cooloff instead of regenerating on the next pass. test: `tests/code_index/test_community_labeler.py::test_rejections_stamp_attempt_only`.

### 6.3 Gate generated names through a Jev `choice` decision [category: code] (depends: 6.2)
`kind: deliverable`

Targets:
- `src/gobby/llm/decisions.py`
- `src/gobby/code_index/community_labeler.py`
- `tests/llm/test_decisions_client.py`
- `tests/code_index/test_community_labeler.py`
- `docs/guides/llm-features.md`
- `docs/evidence/community-labels-2026-09/label-quality.md`
- `docs/evidence/community-labels-2026-09/decisions-wire-spike.md`

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
`community_labeler.py`, one request per batch: state = the batch's communities (id, the
same stored fields 6.2's prompt uses: representatives, the capped member paths, the
deterministic candidates, plus the generated name and its rationale), one `choice`
per community with criteria `generated` and `deterministic_<i>` for each candidate.
Budget about 500 tokens of state per community (40 paths at roughly 10 tokens each plus
candidates and the generated name) plus 250 per question, so a batch of 20 stays near
15k of the 32k limit; cost about $0.0003 per pass. Admission: choice
`generated` with `confidence >= decisions_min_confidence` → `label` = generated,
`label_source='model'`, confidence, `label_model='jev-latest'`, `labeled_signature`
set; any other choice or lower confidence → deterministic label written with
`label_confidence`, `label_model='jev-latest'`, and `labeled_signature` set (a
decision, reopened only by a membership change, which 2.3 clears; the CHECK permits
confidence and model on a deterministic row, and keeping them lets Q1.6 recover the
rejected population's confidences from `code_communities`); transport failure →
attempt stamped only. That reject/failure split is the anti-hot-loop rule. The 6.2 log
event gains the outcomes `gate_admitted`, `gate_rejected`, `transport_failed`, and
`write_superseded`: every write path (`written_deterministic`, `written_model`,
`gate_admitted`, `gate_rejected`) reports `write_superseded` instead of its own outcome
when the signature-guarded `UPDATE` (6.1) affects zero rows because a refresh replaced the
row between the read and the write; the row is legitimately requeued, so
`write_superseded` stamps nothing, and the event keeps Q1.6's arm counts honest by never
reporting a write that did not happen.
`decisions_api_base` unset skips the gate (6.2 behavior). Record the wire-shape spike
(1P base versus the OpenRouter alpha base, one keyed request each) in
`docs/evidence/community-labels-2026-09/decisions-wire-spike.md` before enabling either
base in live config. This leaf also owns the Q1.6 label-quality study: its three arms,
the blind ratings, the metrics against every bar, and the ship decision (gate enabled, or
`decisions_api_base` left unset because G already regresses at most 5%) are recorded in
`docs/evidence/community-labels-2026-09/label-quality.md` before this leaf closes, and the
live config follows that decision.

Live: `uv run gobby restart` after landing, announced with a `global` `send_message`
per Constraints.

Tests: `tests/llm/test_decisions_client.py` — `test_choose_posts_state_model_questions`
(fake transport), `test_unauthorized_does_not_retry`, `test_missing_answer_key_is_error`;
`tests/code_index/test_community_labeler.py` gains `test_gate_admits_at_threshold_and_rejects_below`
(added here, file created in 6.2) and `test_gate_outcomes_extend_the_discriminator_to_nine`,
which covers all nine outcomes (`schema_rejected`, `sanitation_rejected`,
`generation_failed`, `written_deterministic`, `written_model`, `gate_admitted`,
`gate_rejected`, `transport_failed`, `write_superseded`) and leaves 6.2's five-outcome
test untouched so each leaf's acceptance names its own symbol. `llm-features.md`: new row `code_index.community_label`
and a paragraph on the decisions gate (no `AICapability`, not a profile route).

Verify: `DATABASE_URL=postgresql://gobby_test:gobby_test@127.0.0.1:60892/gobby_test GOBBY_TEST_PROTECT=1 uv run pytest tests/llm/test_decisions_client.py tests/code_index/test_community_labeler.py -q`,
`uv run ruff check src/ && uv run mypy src/`.

**Acceptance:**

- 6.3.1 - `DecisionsClient.choose` posts `{state, model, questions}` with bearer auth, retries only on 429/529, and matches answers by key. test: `tests/llm/test_decisions_client.py::test_choose_posts_state_model_questions`.
- 6.3.2 - A `generated` choice at or above the threshold writes the model label with confidence and model; anything else writes the deterministic label with confidence, model, and signature stamped; transport failure stamps the attempt only. test: `tests/code_index/test_community_labeler.py::test_gate_admits_at_threshold_and_rejects_below`.
- 6.3.3 - `docs/guides/llm-features.md` documents `code_index.community_label` and the decisions gate. behavior: "community_label" in `docs/guides/llm-features.md`.
- 6.3.4 - The outcome event's discriminator admits exactly nine values, the five from 6.2 plus `gate_admitted`, `gate_rejected`, `transport_failed`, and `write_superseded`, where `write_superseded` stamps no field, and the exhaustiveness test covers all nine. test: `tests/code_index/test_community_labeler.py::test_gate_outcomes_extend_the_discriminator_to_nine`.
- 6.3.5 - The Q1.6 label-quality study ran with arms D, G, and J and `label-quality.md` records the metrics against every bar and the ship decision the live config follows. behavior: "ship" in `docs/evidence/community-labels-2026-09/label-quality.md`.
- 6.3.6 - The wire-shape spike records one keyed request against the 1P base and one against the OpenRouter alpha base before either is enabled. behavior: "alpha" in `docs/evidence/community-labels-2026-09/decisions-wire-spike.md`.

## P7: Documentation
`kind: framing`

**Goal**: the skill reference and the workspace changelog describe the new behavior.

### 7.1 Update the code-index graphs reference and the Rust changelog [category: docs] (depends: 4.3, 4.4, 5.2, 6.3)
`kind: deliverable`

Targets:
- `src/gobby/install/shared/skills/gobby/references/code-index/graphs.md`
- `crates/CHANGELOG.md`
- `docs/evidence/community-labels-2026-09/live-smoke.md`

Research context: `graphs.md:20-24` says "Communities use the graph analysis
implementation, not file ownership inferred from incoming edges", which is now wrong in
two ways (they come from a persisted per-machine partition, and labels may be
model-derived). `crates/CHANGELOG.md` follows Keep a Changelog with an Unreleased
section. The bundled content manifest is git-ignored and must not be generated.
Rejected: touching the router skill under the gcode assets (it has no community text).

Rewrite `graphs.md:20-24`: MCG community labels come from the persisted project-level
import partition written by `gcode index` (per machine and project, ids stable across
runs); when no partition is stored the view leaves `community` null and says so; add the
`gcode graph view --view communities [--min-size N] [--community ID|LABEL|PATH]` bullet and
the "Which files form a subsystem, and how subsystems depend on each other" row; note
that `label_stale` means the model label predates the current membership and the
deterministic label is shown; MCG Mermaid output groups nodes into one subgraph per
community. `crates/CHANGELOG.md` Unreleased: Added — gcode
`--view=communities`, `--min-size`, `--community`, the `code_communities` table
(naming the migration number 3.1 actually landed under, provisionally 442, per the
Constraints substitution), the `communities` evidence operation, the report section; Added —
gobby-core `graph_analytics::communities`, `centrality`, `GraphInputError`; Changed —
MCG labels are persisted and content-derived, `community-N` ids removed, Mermaid
subgraphs per community, contract 11, gcode 1.9.0.

This leaf owns the Q1.8 live smoke, since it depends on every surface: run the Q1.8
command list after the final cutover and record each command with its outcome in
`docs/evidence/community-labels-2026-09/live-smoke.md`, including the pre-change MCG
baseline of one community per node.

Verify: `DATABASE_URL=postgresql://gobby_test:gobby_test@127.0.0.1:60892/gobby_test GOBBY_TEST_PROTECT=1 uv run pytest tests/skills -q -k graphs`
(or the nearest skill-reference test), `uv run gobby skills validate` if present.

**Acceptance:**

- 7.1.1 - The graphs reference describes the persisted partition, the seedless and detail view forms, and `label_stale`. behavior: "--view communities" in `src/gobby/install/shared/skills/gobby/references/code-index/graphs.md`.
- 7.1.2 - The changelog records every added and changed surface under Unreleased, naming the migration number 3.1 landed under. behavior: "code_communities" in `crates/CHANGELOG.md`.
- 7.1.3 - The Q1.8 live smoke ran after the cutover and each command's outcome is recorded. behavior: "--view communities" in `docs/evidence/community-labels-2026-09/live-smoke.md`.

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

The deferred obligation `D1.1` named in the block below reads: a Leiden partition over
`memory_crossrefs` produces memory clusters the memory-curator batch consumes, reached
in-process through the kernel entry 1.1 exposes. A deferred section carries no
`**Acceptance:**` block (the validator rejects one); the yaml list is the item's
definition and this sentence is its prose.

```yaml
deferral:
  task_ref: "#TBD"
  deferred_from: "gcode-import-communities:D1"
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
   with a `global` `send_message`, `uv run gobby restart` (applies the new migration: 442, or
   the number 3.1 landed under), `uv run gobby cutover`
   for the coherent set. Read binary hashes from `~/.gobby/bin/`, never `target/release/`.
4. Determinism and churn (Gobby checkout and the frozen Game Goblins baseline
   `0216f1e33f05…` under `/Users/josh/Projects/wiki-bakeoff-code-2026-09/`): two `gcode index`
   runs on unchanged input produce byte-identical `code_communities` rows and skip the
   write; renaming three files inside one community, with that row seeded to
   `label_source='model'` by direct `UPDATE` before the rename (no model label exists
   before 6.2 runs), keeps its `community_id`, changes only its `member_signature`, and
   flips exactly one row to `label_stale`; adding one file
   changes at most one id; deleting a community and adding an unrelated one never reuses
   the retired id. Record row diffs in `docs/evidence/community-labels-2026-09/churn.md`
   (owner: the 3.2 leaf, pinned by acceptance 3.2.11).
5. Split thresholds: run inside 2.2 as the threshold spike (owner: the 2.2 leaf, gated by
   acceptance 2.2.7) and recorded in `docs/evidence/community-labels-2026-09/thresholds.md`:
   size distribution and cohesion with and without the two split passes on both corpora
   against Graphify's Game Goblins partition (116 communities, largest 3.4% of nodes,
   median 17.5, 27 singletons); the largest community's share of visible files after
   splitting against the 25% target, recorded as met or exceeded with the cause (2.2.7; an
   exceedance is not a close blocker); no `community-N` placeholder anywhere (2.2.11); `--min-size 5` on Gobby yields deterministic
   labels that name subsystems (`src/gobby/memory`, `crates/gcode/src/...`,
   `web/src/...`) (2.2.12), re-checked here through the CLI after 4.3. The 25% / 0.05 / 50
   constants were kept or replaced with that recorded evidence before 2.2 closed.
6. Label quality: 30 communities (15 per corpus, stratified by size), arms D
   (deterministic), G (generation only, gate unset), J (Jev-gated); blind rating on
   accuracy 0–2 and findability 0–2 by a rater who does not see the arm. Ship the gate
   enabled only if J beats D by ≥ 0.3 accuracy and ≥ 0.5 findability, J regresses ≤ 5% of
   communities below D, gate precision ≥ 0.85 and recall ≥ 0.60 against the rater, mean
   confidence separation between admitted and rejected ≥ 0.15, schema-rejection rate
   ≤ 10%, and the run costs ≤ $0.05. If G already regresses ≤ 5%, record that the gate is
   not earning its keep and ship with `decisions_api_base` unset. Evidence sources: gate
   precision, recall, and confidence separation come from one query over
   `code_communities` (`label_source`, `label_confidence`, `label_model`,
   `labeled_signature`), since 6.3 persists confidence on both the admit and reject
   paths; schema-rejection rate and arm counts come from the
   `code_index.community_label.outcome` log events (6.2). Record the wire-shape spike
   (1P versus OpenRouter alpha) alongside. Evidence lands in
   `docs/evidence/community-labels-2026-09/label-quality.md` and
   `decisions-wire-spike.md` (owner: the 6.3 leaf, pinned by acceptance 6.3.5 and 6.3.6,
   which also pin the ship decision).
7. Read-path and refresh cost: `gcode graph report` median wall time over five runs
   before and after 1.2; no regression allowed. `gcode graph view --view communities` on
   Gobby completes under one second from stored rows. Incremental single-file
   `gcode index` median wall time over five runs before and after 3.2, on the Gobby
   checkout and the frozen Game Goblins corpus, with a budget of +250 ms at p50; record
   the numbers in `docs/evidence/community-labels-2026-09/refresh-latency.md` (owner: the
   3.2 leaf, acceptance 3.2.12) and the report timing in `report-latency.md` (owner: the
   1.2 leaf, acceptance 1.2.3). Over budget triggers the digest contingency named in 3.2,
   in a follow-up migration.
8. Live smoke after cutover (owner: the 7.1 leaf, acceptance 7.1.3, recorded in
   `docs/evidence/community-labels-2026-09/live-smoke.md`): `gcode index` on this repo, then
   `gcode graph view --view communities --min-size 5 --format json` (labels name
   subsystems, sizes descend, `edges[]` join `community:` ids with counts, Mermaid
   validates), `gcode graph view --view communities --community memory`,
   `gcode graph view --view communities --community crates/gcode/src/commands/graph/view/mcg.rs`
   (resolves by member path to the community the MCG view labels),
   `gcode graph view --view mcg --file crates/gcode/src/commands/graph/view/mcg.rs`
   (`mcg.rs`, `crates/gcode/src/commands/graph/view/mcg/fetch.rs`, `crates/gcode/src/commands/graph/view/mcg/identity.rs`, `mod.rs`, `render.rs` share one label and one Mermaid subgraph;
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
- **Enhancement round 1 of 1 (2026-09-19)** — `kind: enhancement`,
  `enhancer_run: 428ca14a-08a6-4a71-bd67-43c1efba268d` (plan-enhancer-taskless on
  claude/opus/xhigh, child session `e47e864c-b01b-4693-a67c-49b9e485b516`),
  `suggestions_presented: 13`, converged false (cap reached). Every suggestion was
  presented in full and voted on individually by the user; all thirteen were accepted.
  - E1 accept (better/clarity, impact high, effort small, risk low): the prior-row read
    sat outside the replace transaction, so a daemon label written between the read and
    the `DELETE` was lost permanently. 3.2 now reads prior rows `FOR UPDATE` inside
    `begin_replace` and gains `label_written_during_refresh_is_not_lost` (3.2.7).
  - E2 accept (better/clarity, high, small, low), column form: no stored counterpart
    existed for the skip predicate. Migration 442 adds `partition_signature` to
    `code_indexed_project_states`, written in the same commit as the watermark (3.1.1,
    3.2.1, 3.2.2).
  - E3 accept (better/scope, high, medium, low), seed-from-parent form: the daemon
    labeler skips overlay projects, so worktree rows shadowed the parent's model labels
    with deterministic ones under unrelated ids. An overlay's first refresh seeds ids,
    watermark, and label fields from the parent (3.2.8).
  - E4 accept (better/testability, med, small, low): Q1.6's gate metrics were not
    recoverable from stored rows. The reject path persists confidence and model, and
    6.2 emits one `code_index.community_label.outcome` event per outcome (6.2.4, 6.3.2,
    Q1.6 evidence sources).
  - E5 accept (better/clarity, med, small, low), no-read form: `read_context` and "top
    symbols" were undefined. The prompt and the Jev state use stored row fields only
    with member paths capped at 40; a symbol read is follow-up work only if Q1.6 shows
    paths label poorly.
  - E6 accept (better/sequencing, med, small, low): 6.2 depended on 4.2 without
    consuming it. The edge is now 3.2, so P6 can run concurrently with P4 and P5.
  - E7 accept (better/clarity, med, small, low): the recompute branch left the gate
    bookkeeping fields ambiguous. 2.3 clears all five and gains
    `gate_rejected_label_reopens_on_membership_change` (2.3.4).
  - E8 accept (better/testability, med, small, low): the refresh cost on hook runs was
    asserted, not measured. Q1.7 adds an incremental-index latency budget of +250 ms at
    p50, and 3.2 names the digest contingency without adding it.
  - E9 accept (better/sequencing, med, small, low): install points now sit in 3.1, 3.2,
    5.1, 6.1, 6.2, and 6.3, and the vendored-contract test is run after the cutover in
    3.2 and 5.1.
  - E10 accept (bigger/scope, med, small, low): `--community` resolves a member path as
    well as an id or label, matching the 5.1 selector (4.3.3, 4.3.7, 7.1, Q1.8).
  - E11 accept, resolution (a) (better/sequencing, med, small, low): the threshold
    experiment runs inside 2.2 as the `#[ignore]` experiment test with the 25% bar as
    acceptance 2.2.7; Q1.5 records the result instead of gating it.
  - E12 accept (better/clarity, low, small, low): the privilege-manifest scope-reasons in
    3.1 and 3.2 now match the acceptance split.
  - E13 accept (bigger/scope, med, medium, med): `render_mermaid` emits one subgraph per
    community (4.2.5). The coordinator recommended deferring it for risk; the user chose
    to include it.
  - Coordinator correction found while folding E13: 4.3's list view claimed full `file:`
    member ids in `communities[]`, which 4.2's `build_view_payload` validation rejects.
    The list view now carries empty `nodes`; membership comes from the detail form or
    the evidence operation (4.3.2).
- **Review round 1, attempt 1 (2026-09-19), protocol failure, not a counted round** —
  reviewer run `9ef3ed6b-2fd8-4447-bf07-8242fa7591e5` (plan-adversary-taskless on
  claude/opus/xhigh, child session `13eb6d74-0b11-4448-a340-4b3b9f1b092a`, gobby#13881),
  evidence `cd76010c-231c-4d19-bb43-a119b421041e`, plan at `6a6cabae9e`. All three lanes
  completed and produced eight blocking and six nit draft findings, then
  `validate_plan_review_coverage` rejected the reviewer's compact `shadow_manifest_status`
  echo (`shadow_manifest_mismatch`): the proxy had offloaded the 23k-character derivation
  and the validator required the full `manifest_entries` echo. The validator now accepts
  the derivation without `manifest_entries` (task #22571, `1ee3db66d3`). Every draft
  finding was presented in full and voted on individually by the user; all fourteen were
  accepted and folded here as coordinator amendments before round 1 is re-run.
  - F1 accept (3.2, unhandled-edge): `partition_signature` covered membership only, so an
    edge-only change froze `internal_edges`, `cohesion`, `representatives`, and `boundary`.
    It now covers every stored column (2.2, 2.2.10, 3.2.9).
  - F2 accept, restated form (2.2, unhandled-edge): the single oversized pass cannot
    guarantee the 25% bar for a cohesive blob Leiden returns as one child. 2.2.7 is now a
    measured-and-recorded outcome with the exceedance cause; recursion stays rejected.
  - F3 accept (2.3, missing-requirement): `LabelSource`, `LabelCarry`, `CommunityFact`, and
    `GeneratedLabel` are now defined with home files and acceptance items (2.3.5, 2.3.6,
    5.1.6, 6.2.6).
  - F4 accept, read-path form (4.2, missing-requirement): `read_for_context` applies the
    stale rule once, so every reader shows the deterministic label with `label_stale`
    without a payload change (3.2, 3.2.10); the Overview and 7.1 wording stand.
  - F5 accept (5.2, missing-requirement): the union discriminator is `item_type`,
    mirroring the serde tag.
  - F6 accept (5.1, missing-requirement): 5.1.1 states the flat internally tagged request.
  - F7 accept (3.1, traceability): `tests/code_index/test_gcode_privilege_manifest.py` is
    targeted with its exact-set assertion and run in the 3.1 and 3.2 Verify blocks (3.1.6).
  - F8 accept (6.3, traceability): every gating Q1 item now has an owning leaf and an
    acceptance item: churn and refresh latency on 3.2 (3.2.11, 3.2.12), report timing on
    1.2 (1.2.3), label quality and the wire-shape spike on 6.3 (6.3.5, 6.3.6), the live
    smoke on 7.1 (7.1.3).
  - F-N1 accept (3.1, over-engineering): the unused GIN index over `members` and its query
    sentence are gone; membership is an in-memory scan.
  - F-N2 accept (4.3, traceability): bundled acceptance items are split (2.2.4, 2.2.8,
    2.2.9, 2.2.11, 2.2.12, 4.3.3, 4.3.8 to 4.3.13), 4.3's projection tests are named as
    symbols, and 3.1.5 and 4.1.2 point at artifacts only their obligation satisfies.
  - F-N3 accept (Constraints, gobby-format): the shared-target chain is regenerated from
    the Targets blocks with full paths; three over-claims dropped, two omissions added.
  - F-N4 accept (4.3, traceability): `docs/contracts/gcode-cli.md:8`, `docs/guides/ask.md`,
    and `docs/guides/gcode-user-guide.md` are targeted (4.3.14, 5.2.5).
  - F-N5 accept (5.2, traceability): three research spans corrected against the index.
  - F-N6 accept (6.2, weak-testability): the ungated default path and the three gate
    outcomes are pinned (6.2.5, 6.3.4).
  - Coordinator addition while folding: task #22572 is landing its own migration 442, so
    the Constraints now state that 3.1's number is the next free one at landing.

Review round 1 of 3 (2026-09-19), reviewer run da5a5190-ae06-4e13-9545-4165f2aefe5e (plan-adversary-taskless on claude/opus/xhigh, child session 6e5645d9-a580-488b-a1c1-79637135cfd0), verdict needs_review: eleven blocking findings and eleven nits, three lanes complete, shadow manifest valid at digest 59c4db2f with the 4.3 backend routing override accepted. The reviewer re-ran the fourteen folds from the protocol-failed attempt and found thirteen genuinely resolved and one (F8) partly resolved, which returns as F-09. Under the user's delegated authority for this round ("use your best judgment on the findings") the coordinator voted on each finding individually: all twenty-two accepted. Coordinator variations: F-02 lands in 3.2, which already owns crates/gcode/src/index/indexer/lifecycle.rs, so invalidate deletes this machine's code_communities rows in the same transaction that removes code_indexed_project_states, rather than adding code to the schema-only 3.1 leaf; F-N11 is resolved as a recorded exclusion (community_label is daemon-config-only, the settings UI is out of scope for this plan); F-N4's new 6.3.4 test symbol counts nine outcomes because F-N6 adds write_superseded. One canonical-payload correction: the reviewer's F-01 typed repair named the wildcard target without the scope-reason the target grammar requires, so the entry carries the scope-reason stated in the same finding's fix text; nothing else in the result is altered. Typed repairs are applied for F-01, F-08, F-09 and F-N10; every other accepted finding is folded as coordinator prose in the commit that follows this checkpoint.

```json plan-review-round
{"evidence_id":"f66bb9ae-ba7f-401f-9dc1-67d5a5ad4d4d","plan_hash":"5672dab819875aeaae5d9a1ad235dcd33c4e6c41b09458ecab27c9c88bdf3b32","round_number":1,"round_result":{"coverage_attestation":{"adjacent_variant_complete":true,"attestation_digest":"026ede0994c85fd87f5c2455edbdeb640cee9e4ff3746a1d202669331490070f","cross_lane_interaction_complete":true,"disposition_counts":{"dismissed":4,"emitted_findings":21,"total":25},"evidence_id":"f66bb9ae-ba7f-401f-9dc1-67d5a5ad4d4d","lanes":[{"candidate_count":9,"lane_id":"requirements_traceability","status":"completed"},{"candidate_count":5,"lane_id":"repository_blast_radius","status":"delegated-verified"},{"candidate_count":11,"lane_id":"runtime_invariants","status":"completed"}],"shadow_manifest_status":{"entry_count":17,"manifest_digest":"59c4db2f379b77ae7328a8357ca67081357f9f7ec8309ee46900a6f9b3d9fe2a","status":"valid"},"source_digest":"b9ade638535a47c80638cda83389d1577c264438cb35f2e5dc83e7b62a819179","version":1},"cross_lane_interactions":["F-03 and F-04 must land together: today the overlay always skips, so the shredded singleton partition F-04 describes is never written. Correcting the skip predicate makes F-04 manifest.","F-02 and F-03 both concern the overlay/parent watermark path; the F-03 redesign should be written with F-02's lifecycle fix in hand so the seeded watermark and the invalidate path agree.","F-06 and F-N5 both cause redundant model calls from different root causes (no queue-exit stamp vs. a reset cooloff); fixing only one leaves the other.","F-01 and F-05 both land in 5.1 and touch the same evidence surface; fold them in one pass."],"evidence_id":"f66bb9ae-ba7f-401f-9dc1-67d5a5ad4d4d","findings":[{"category":"traceability","check_key":"evidence-item-variant-exhaustive-match-consumers","description":"5.1 adds EvidenceItem::Community, but crates/gcode/src/evidence/search.rs:41-42 matches `EvidenceItem::Source(source) => eligible.contains(&source.path), EvidenceItem::Graph(_) | EvidenceItem::CommitMetadata(_) => false` with no wildcard arm, and search.rs appears in no deliverable's Targets. 5.1 as written is a non-exhaustive-match compile error (E0004) in a file nothing owns. Verified in source by the reviewer.","finding_id":"F-01","fix":"Add `crates/gcode/src/evidence/search.rs::*` to 5.1's Targets with scope-reason 'add the Community arm to the search-lane eligibility retain (a community is not a file-scoped item, so it is dropped from search results)', and extend the Constraints consumer sweep to record the EvidenceItem:: sites beside the EvidenceOperation:: line.","location":"5.1 Targets block","prevention":"When a deliverable adds an enum variant, sweep the variant's own type name, not only the sibling enum, and target every wildcard-free match.","principle":"Adding a variant to an enum obliges every exhaustive match over it; each such site is an owned consumer that must appear in Targets.","repairs":[{"entries":["`crates/gcode/src/evidence/search.rs::*` — scope-reason: add the Community arm to the search-lane eligibility retain (a community is not a file-scoped item, so it is dropped from search results)"],"kind":"add_targets","section_id":"5.1"}],"root_cause":"The Constraints consumer sweep enumerates EvidenceOperation:: sites but never sweeps EvidenceItem:: itself, so the sibling exhaustive match on EvidenceItem was missed while the one on EvidenceOperation was caught.","section_id":"5.1","severity":"blocking"},{"category":"unhandled-edge","check_key":"watermark-outlives-rows-under-invalidate","description":"code_communities.project_id REFERENCES code_indexed_projects(id) ON DELETE CASCADE, but the watermark is community_id_watermark on code_indexed_project_states (NOT NULL DEFAULT 0). indexer::invalidate deletes code_indexed_file_states and code_indexed_project_states without touching code_indexed_projects, so the rows survive while the watermark resets to 0. assign_ids issues max(watermark, max old id) + 1, which protects only ids of SURVIVING communities: any id issued and then retired above the surviving maximum is reissued. That is precisely what the plan says the watermark exists to prevent ('an id that can change meaning cannot be cited; hence the watermark'), and it breaks the Overview guarantee and Q1.4's 'deleting a community and adding an unrelated one never reuses the retired id'. Reachable through the user-facing `gcode invalidate` subcommand and through prune, which calls invalidate_project_locked per project.","finding_id":"F-02","fix":"Add `crates/gcode/src/index/indexer/lifecycle.rs::*` to 3.1's Targets with scope-reason 'delete code_communities rows in the invalidate transaction', so rows and watermark die together; alternatively persist the watermark on a row with the same lifetime as the community rows. Add acceptance 3.1.7 pinning that invalidate removes this machine's code_communities rows in the same transaction that removes code_indexed_project_states, with a test in refresh_tests.rs.","location":"3.1 DDL, community_id_watermark on code_indexed_project_states","prevention":"For every new table, enumerate the existing lifecycle paths (invalidate, prune, cascade) and state what each does to the new rows and to any counter that governs them.","principle":"A monotone counter that guarantees an identifier is never reissued must not live in a row that a reachable path deletes independently of the rows whose identifiers it governs.","section_id":"3.1","severity":"blocking"},{"category":"unhandled-edge","check_key":"overlay-seed-signature-defeats-skip-predicate","description":"3.2 says 'when the stored partition_signature equals the new one, skip() and report skipped_unchanged' and also that for an overlay with no rows of its own, begin_replace 'seeds the prior rows, the watermark, and the stored signature from the parent project'. For the common worktree case where the overlay's partition equals the parent's, the seeded signature equals the newly computed one, so the refresh skips and never commits. The overlay therefore never acquires rows, its own partition_signature stays NULL, the seed repeats on every run, and the stated two-phase design ('Later overlay refreshes remap against the overlay's own rows') never engages. Acceptance 3.2.8 cannot be satisfied on that input. This finding also carries dismissed candidate ri-04: because project_lock_key hashes only the project id, an overlay run and a parent run hold different advisory locks, so the reworked seed must read the parent's watermark and rows under the parent's row lock in one transaction.","finding_id":"F-03","fix":"State that the skip predicate compares against the partition_signature stored under ctx.project_id, never the seeded parent signature, so an overlay's first refresh always commits. In the same edit, state that the overlay seed takes FOR UPDATE on the parent's code_indexed_project_states row before reading the parent's code_communities rows, in the same transaction, and name the isolation level. Pin both with a concurrency test.","location":"3.2 refresh sequence, skip predicate and the overlay seeding paragraph","prevention":"When two accepted enhancements touch one code path, re-read the path as a whole and state which key every comparand is read from.","principle":"A change-detection signature must describe the state stored under the key being written; seeding it from another key makes 'unchanged' indistinguishable from 'nothing stored', so the write that would create the state never happens.","root_cause":"The E2 fold added the stored-signature skip predicate and the E3 fold added parent seeding of the stored signature; each is sound alone and they contradict in combination.","section_id":"3.2","severity":"blocking"},{"category":"unhandled-edge","check_key":"node-scope-edge-scope-mismatch-overlay","description":"2.1 specifies load_project_imports as the 'Same body as load_identity'. Verified in source: load_identity derives its node set from visibility::visible_tree(&mut conn, ctx), which under ProjectIndexScope::Overlay unions the overlay's files with every unshadowed parent file, but derives its edges from crate::db::read_active_imports(&mut conn, &ctx.project_id), whose SQL filters WHERE ci.project_id = $2 for a single id. 2.2 then makes nodes identity.visible_files. Under an overlay context every parent-only file becomes an isolated singleton, so the committed overlay partition is a shred of singletons that read_for_context prefers over the parent's real partition, defeating 3.2's stated goal that a worktree shows the main checkout's ids and model labels. This is latent in today's depth-bounded MCG view and becomes a persisted, project-wide artifact once 2.1 moves the body into the partition path.","finding_id":"F-04","fix":"Have load_project_imports read imports for every id in visibility::visible_project_ids(ctx), dropping parent rows the overlay shadows by file_path (mirroring visible_tree's anti-join), and add acceptance 2.1.4 pinning that it returns imports for every visible file under an overlay context, with a test such as overlay_partition_covers_parent_only_files.","location":"2.1 load_project_imports, 'Same body as load_identity'","prevention":"Before reusing a body verbatim, check that each data source it reads is scoped consistently for the new caller's context.","principle":"A graph's node set and edge set must be drawn from the same scope, or every edge incident to a node the narrower scope omits is silently lost.","section_id":"2.1","severity":"blocking"},{"category":"unhandled-edge","check_key":"empty-partition-classified-as-error","description":"5.1 says 'zero stored rows -> IndexUnavailable with the \"run gcode index\" detail, never CompleteEmpty', pinned by acceptance 5.1.3. This directly contradicts the plan's Constraints, which list 'zero stored communities' among ordinary data conditions that 'are never errors'. IndexUnavailable is not a Completeness variant; it is EvidenceError::IndexUnavailable, which commands/evidence.rs maps to exit status 2 with the fixed recovery string 'restore the managed PostgreSQL code-index connection and retry' -- advice that cannot fix an empty partition, and 5.1's Targets do not change that arm. It also diverges from the other three readers of the same state (4.2/4.3 emit a missing-partition hint, 4.4 pushes an optional ReportDegradation) and is unrecoverable for a project that legitimately has no internal imports: gcode index will run, commit an empty partition, and the operation keeps erroring.","finding_id":"F-05","fix":"Make zero stored rows CompleteEmpty carrying the missing-partition hint as a warning, and distinguish 'never refreshed' from 'refreshed and legitimately empty' by consulting partition_signature IS NULL, the column 3.1 already adds. Rewrite 5.1.3 and add 5.1.7 pinning that a refreshed-but-empty project reports CompleteEmpty rather than an error.","location":"5.1 outcomes paragraph and acceptance 5.1.3","prevention":"Check each new reader's outcome classification against the plan's own Constraints and against the sibling readers of the same state.","principle":"One persisted state must produce one consistent classification across all readers, and an ordinary self-healing data condition must not be reported as an infrastructure error.","section_id":"5.1","severity":"blocking"},{"category":"unhandled-edge","check_key":"label-queue-predicate-not-cleared-on-every-path","description":"6.1's queue selects rows where labeled_signature IS DISTINCT FROM member_signature, with idx_cc_label_queue indexing exactly that. 6.2 pins labeled_signature on the ungated path and 6.3 pins it on both gate outcomes, but the third path is stated only as 'A validated name equal to any deterministic candidate skips the gate (6.3) and writes label_source='deterministic'' -- with no labeled_signature. 3.1's CHECK permits a deterministic row with labeled_signature NULL, so nothing catches it: the row stays queued and is regenerated on every pass, burning model calls indefinitely with no cooloff, since label_attempted_at is stamped only on failures. Reviewer adjacent-variant sweep widens this: 6.2 enumerates five outcomes (schema_rejected, sanitation_rejected, generation_failed, written_deterministic, written_model) and only written_model stamps labeled_signature, while only generation_failed is pinned to stamp label_attempted_at (6.2.3); the sentence 'Failures stamp label_attempted_at' does not say whether the two rejection outcomes count as failures, leaving three of five outcomes underdetermined. Independently found by both the requirements and runtime lanes.","finding_id":"F-06","fix":"State the queue-exit field for all five outcomes, not just one path: written_deterministic writes labeled_signature = member_signature (and label_model = the generation profile so Q1.6 can count the arm), and schema_rejected and sanitation_rejected stamp label_attempted_at. Extend acceptance 6.2.2 to '...is stored as deterministic with labeled_signature equal to the member signature, so the row leaves the label queue', and add an acceptance item covering the two rejection outcomes' cooloff stamp.","location":"6.2 write paths, the written_deterministic branch","prevention":"Enumerate the worker's terminal outcomes and state, for each one, which field takes it out of the queue.","principle":"Every terminal write path of a queue-draining worker must clear the predicate that queued the item, or the item is redelivered forever.","section_id":"6.2","severity":"blocking"},{"category":"missing-requirement","check_key":"targeted-file-crosses-decomposition-trigger","description":"crates/gcore/src/graph_analytics.rs is 693 lines with an inline #[cfg(test)] module at :533-693 holding only four tests in 161 lines (~30-40 lines each). 1.1 adds GraphInputError, a validating communities() with four checks, and seven inline tests; 1.2 adds centrality() and a parity test. That lands the file near 880-970, past the 850 decomposition trigger and inside the hook's blocking band, on the 1.1 commit. crates/AGENTS.md is explicit: 'Inline #[cfg(test)] modules count toward the owning production file's 1,000-line ceiling. Keep large unit-test modules out of production Rust files. Place the tests at <module>/tests.rs and declare them from <module>.rs with #[path]'. P1 is the only phase in this plan adding tests inline; 2.1, 2.2 and 2.3 all use the sibling test-file idiom already. Independently found by the requirements and repository lanes.","finding_id":"F-07","fix":"Add `crates/gcore/src/graph_analytics/tests.rs` as a bare-path Target on 1.1 (the crates/gcore/src/graph_analytics/ directory already exists for leiden.rs), move the existing :533-693 module there with the #[path] idiom in the same leaf, and restate acceptance 1.1.3 and 1.2.1 as crates/gcore/src/graph_analytics/tests.rs::communities_without_edges_is_all_singletons and ::centrality_matches_analyze_centrality. Add the projected post-1.2 size to the Constraints table.","location":"1.1 and 1.2 Targets; Constraints size table","prevention":"For each targeted production file, record projected post-change size, not just today's size, and apply the growth-commit rule uniformly.","principle":"A targeted production file whose planned additions cross the decomposition trigger needs an extraction in the same plan, and the size claim must be forward-looking rather than as-of-today.","root_cause":"The Constraints size table asserts only the baseline ('No targeted production file is at or above 850 lines') and gives no growth budget, while 4.1 applies the growth-commit rule to cli.rs at 827.","section_id":"1.1","severity":"blocking"},{"category":"traceability","check_key":"deferral-references-nonexistent-acceptance-item","description":"D1's deferral block declares original_acceptance_items with the single entry D1.1, but D1.1 occurs exactly once in the entire plan -- on line 1606, inside the yaml that references it. D1 carries no Acceptance block at all. The coordinator will create a deferred-from:gcode-import-communities:D1 task citing an acceptance item the plan never wrote. A reviewer sweep of every other id reference in the plan found no second instance of this defect; all N.N.N references resolve.","finding_id":"F-08","fix":"Add a one-item Acceptance block to D1 before the yaml defining D1.1 against the memory-community deliverable it defers, so the reference resolves; and lift the deferred-from provenance out of the surrounding prose into an explicit key on the yaml block so all five typed fields are machine-readable.","location":"D1 deferral yaml, original_acceptance_items","prevention":"Validate every id named in a typed block against the plan's actual item set before finalizing.","principle":"A typed deferral's original_acceptance_items must reference acceptance item IDs that exist in the plan, or the deferred-item audit trail is broken at the moment the deferral task is created.","repairs":[{"items":[{"artifact":"symbol: `communities`","prose":"A Leiden partition over memory_crossrefs produces memory clusters the memory-curator batch consumes."}],"kind":"add_acceptance","section_id":"D1"}],"section_id":"D1","severity":"blocking"},{"category":"traceability","check_key":"gating-verification-bar-without-owner","description":"Q1.7 contains three bars and its ownership parenthetical names only two: refresh latency (owner 3.2, acceptance 3.2.12) and report timing (owner 1.2, acceptance 1.2.3). The middle sentence -- 'gcode graph view --view communities on Gobby completes under one second from stored rows' -- has no owning leaf, no evidence file, and no acceptance item. 4.3's fourteen acceptance items say nothing about latency, and Q1.8/7.1.3 record command outcomes rather than timings. The reviewer re-checked all of Q1: Q1.4, Q1.5, Q1.6 and Q1.8 are correctly owned, so this is the sole survivor.","finding_id":"F-09","fix":"Add acceptance 4.3.15 pinning that gcode graph view --view communities on the Gobby checkout completes in under one second from stored rows over five runs, recorded in docs/evidence/community-labels-2026-09/view-latency.md; add that evidence file to 4.3's Targets; and append '(owner: the 4.3 leaf, acceptance 4.3.15)' to that sentence in Q1.7.","location":"Q1.7, second sentence","prevention":"When folding an ownership finding, enumerate every assertion inside the item, not just the ones the finding quoted.","principle":"Every gating assertion in the verification section must have an owning deliverable and an acceptance item, or it cannot be enforced at close.","repairs":[{"entries":["`docs/evidence/community-labels-2026-09/view-latency.md`"],"kind":"add_targets","section_id":"4.3"},{"items":[{"artifact":"behavior: \"under one second\" in `docs/evidence/community-labels-2026-09/view-latency.md`","prose":"`gcode graph view --view communities` on the Gobby checkout completes in under one second from stored rows over five runs."}],"kind":"add_acceptance","section_id":"4.3"}],"root_cause":"UNRESOLVED-PRIOR:F8. The V1 changelog records 'F8 accept (6.3, traceability): every gating Q1 item now has an owning leaf and an acceptance item', but the fold covered only two of Q1.7's three bars.","section_id":"4.3","severity":"blocking"},{"category":"bad-sequencing","check_key":"close-experiment-needs-later-phase-artifact","description":"Q1.4 requires that renaming three files inside one community 'keeps its community_id, changes only its member_signature, and flips exactly one row to label_stale', and acceptance 3.2.11 requires the experiment to run on both corpora before 3.2 closes. But 3.2 defines label_stale as label_source == Model && labeled_signature != Some(member_signature), and the only producers of label_source='model' are the daemon labeler in 6.2/6.3, which depends on 3.2. At 3.2's close no model-labeled row exists on either corpus, so label_stale is identically false and the flip cannot be observed. 3.2's unit test stale_model_label_reads_as_deterministic is unaffected because it constructs the model row in a fixture; only the corpus experiment is blocked. No add_dependency repair is offered because the real edge already runs 6.2 -> 3.2, so reversing it would create a cycle.","finding_id":"F-10","fix":"Amend the Q1.4 sentence to name the seeding step the experiment needs -- 'with one row seeded to label_source='model' by direct UPDATE before the rename, exactly one row flips to label_stale' -- and mirror that wording into 3.2.11; or move only the label_stale clause into a Q1.6 sub-item owned by 6.3, where model labels exist.","location":"Q1.4 churn experiment and acceptance 3.2.11","prevention":"For each experiment, check that every field it asserts on can be non-trivially populated by the phase that owns it.","principle":"A deliverable's close-gating experiment must be observable in the state that exists at its close; it must not require an artifact only a later deliverable can produce.","section_id":"3.2","severity":"blocking"},{"category":"missing-requirement","check_key":"selector-ladder-underdetermined","description":"Three gaps, merged from both lanes. (a) The final rung is 'else a unique case-insensitive substring' and never says substring of what, while every preceding rung names its field; acceptance 4.3.3 and 4.3.10 inherit the ambiguity, so two implementations can both pass, and Q1.8's `--community memory` lands on exactly this rung. (b) The exact-label rung assumes `label` is unique, but 2.2's dedupe_labels appends ordinal suffixes only to deterministic labels within one partition, and 6.2/6.3 write a model `label` with no cross-row uniqueness check, so two rows can share a label; the ambiguity sentence is attached only to the substring rung, leaving the exact-label rung with no tie rule and a silently arbitrary result. (c) A selector that parses as an integer but matches no stored community has no stated outcome; 4.3.9 covers only the unknown-path case.","finding_id":"F-11","fix":"State the last rung as 'a unique case-insensitive substring of label or label_deterministic (members are not substring-matched)'; state that the ambiguity rule (typed exit-2 error listing up to five matches) applies at every rung, not only the substring rung; state that an integer selector matching no community is the same typed exit-2 error as an unknown path. Extend 4.3.3 to name both substring fields and add an acceptance item covering no-match and multi-match at every rung. Mirror the same rule in 5.1's CommunitiesSelector, which today states only the no-match and below-min_size outcomes.","location":"4.3 --community resolution ladder","prevention":"For each resolution rung, write the field, the no-match outcome and the multi-match outcome explicitly; check uniqueness claims against the writer, not the reader.","principle":"A plan must be decision-complete: a selector ladder must name the field each rung searches and define the no-match and multiple-match outcome at every rung, and must not assume uniqueness of a field the write path never makes unique.","section_id":"4.3","severity":"blocking"},{"category":"over-engineering","check_key":"stored-index-without-named-consumer","description":"Same class as the already-folded F-N1, which removed the GIN index over members on the ground that membership lookups are in-memory scans. CREATE INDEX idx_cc_project_size ON code_communities (machine_id, project_id, member_count DESC, community_id) has no query in the plan that uses it: 3.2's read_project_communities reads every row for the pair, which the PRIMARY KEY (machine_id, project_id, community_id) already serves as a prefix scan; 4.3's list view, 4.4's top/thin_count and 5.1's canonical_key all order in memory; and 6.1's queue is served by the partial idx_cc_label_queue. Acceptance 3.1.1 pins it only via 'indexes ... above', so removing it changes no acceptance item.","finding_id":"F-N1","fix":"Delete the idx_cc_project_size line from the migration DDL and from the mirrored baseline.sql DDL, keeping idx_cc_label_queue. If a future SQL-side ordering is intended, name that query in 3.2 and keep the index.","location":"3.1 DDL, idx_cc_project_size","prevention":"For each index, name the query that uses it in the same sentence that creates it.","principle":"A stored mechanism pinned by acceptance must have a named consumer; an index no specified query uses is unearned weight.","section_id":"3.1","severity":"nit"},{"category":"traceability","check_key":"provisional-value-escapes-substitution-scope","description":"The Constraints scope the landing-time migration-number substitution to 'the file name, MIGRATIONS, latest_version, cli_contract.rs, and every \"442\" in 3.1'. Two occurrences fall outside 3.1: 7.1's Research context writes the literal '(migration 442)' into the crates/CHANGELOG.md Unreleased text, and Q1 step 3 says restart 'applies 442'. Acceptance 7.1.2 pins only the string 'code_communities' in the changelog, so nothing catches a stale number. Since task #22572 is landing its own 442 concurrently, 7.1 can ship a wrong migration number into a released changelog. Additionally, no deliverable owns the Constraints obligation that 'V1 records the substitution', and 3.1.1/3.1.2 hard-code the literal.","finding_id":"F-N2","fix":"Broaden the Constraints substitution sentence to cover every occurrence of the provisional number in the plan, naming 7.1's changelog text and Q1 step 3 explicitly; reword 3.1.1 and 3.1.2 to 'the new migration (442, or the next free number at landing)'; and give 3.1 an acceptance item that the number actually used is recorded in V1 and resolved everywhere in the same commit.","location":"7.1 Research context, crates/CHANGELOG.md Unreleased text","prevention":"After declaring a value provisional, grep the whole plan for the literal and confirm each occurrence is inside the stated substitution scope.","principle":"When a framing section declares a value provisional and names the substitution scope, that scope must cover every place the literal is written, including into shipped artifacts.","section_id":"7.1","severity":"nit"},{"category":"gobby-format","check_key":"stated-constraint-violated-by-own-targets","description":"Constraints says 'Hand-maintained .rs/.py files stay under 1,000 lines' with no production qualifier, then qualifies the very next sentence ('No targeted production file is at or above 850 lines'). Measured at HEAD, five files this plan targets and adds to already exceed 1,000: crates/gcore/src/schema/runner_tests.rs 2500 (3.1), tests/code_index/test_code_index_storage.py 1516 (6.1), crates/gcode/src/evidence/tests.rs 1216 (5.1), crates/gcode/tests/evidence.rs 1192 (5.1), tests/code_index/test_code_index_maintenance.py 1117 (6.2). The repository rule excludes tests, so the plan's sentence is mis-stated rather than the plan being non-compliant. Every other size figure in the Constraints table was verified exact at HEAD.","finding_id":"F-N3","fix":"Insert one word: 'Hand-maintained production .rs/.py files stay under 1,000 lines', matching the following sentence and the governing repository rule.","location":"Constraints, the 1,000-line sentence","prevention":"Qualify size constraints the same way the governing repository rule does, and check the qualified rule against the plan's own Targets.","principle":"A constraint the plan states must be one the plan's own deliverables satisfy.","section_id":"Constraints","severity":"nit"},{"category":"weak-testability","check_key":"acceptance-artifact-shared-across-leaves","description":"Found by the reviewer's adjacent-variant sweep, not by a lane. 6.2.4 and 6.3.4 both name tests/code_index/test_community_labeler.py::test_every_outcome_emits_one_log_event. A test symbol is a complete artifact with no further discriminator, so the coverage labels covers:...:6.2:6.2.4 and covers:...:6.3:6.3.4 resolve to one symbol and cannot tell which leaf's obligation a pass proves. This is the only complete-artifact collision in the plan -- every other repeated artifact is discriminated by a distinct symbol or distinct prose -- and it is exactly the property the accepted F-N2 fold established for 3.1.5 and 4.1.2.","finding_id":"F-N4","fix":"Give 6.3.4 its own test symbol, for example tests/code_index/test_community_labeler.py::test_gate_outcomes_extend_the_discriminator_to_eight, leaving 6.2.4 on the five-outcome test.","location":"acceptance 6.3.4 and 6.2.4","prevention":"Before reusing a test symbol, check no other leaf's acceptance already names it.","principle":"An acceptance artifact should be one that only its own obligation satisfies, so a passing artifact discriminates which leaf's obligation it proves.","section_id":"6.3","severity":"nit"},{"category":"unhandled-edge","check_key":"cooloff-reset-by-unrelated-subsystem","description":"The E7 fold made 2.3 clear five fields on a matched-with-changed-signature deterministic row, including label_attempted_at. That field is the daemon's failure cooloff: 6.1 selects rows where label_attempted_at is null or older than the cooloff, and 6.2 stamps it with a 300-second cooloff on failure. Since a never-successfully-labeled row has label_source='deterministic', every membership change clears its cooloff, and 3.2 runs the refresh on hook-triggered single-file runs, so an actively edited repository resets the backoff repeatedly for exactly the communities whose generation keeps failing. Bounded by membership-change frequency rather than per-tick, hence nit rather than blocking alongside F-06.","finding_id":"F-N5","fix":"Exclude label_attempted_at from the set 2.3 clears, clearing only the four gate-decision fields, and amend acceptance 2.3.4 to name four fields; or add an explicit attempt counter with a retry cap so the cooloff is not the only bound.","location":"2.3 carry-forward, the cleared field set","prevention":"When clearing a field set, check which fields another subsystem owns as retry state.","principle":"A failure cooloff owned by one subsystem must not be resettable as a side effect of another subsystem's routine write.","section_id":"2.3","severity":"nit"},{"category":"unhandled-edge","check_key":"outcome-enum-missing-reachable-state","description":"3.2 states the intended race outcome -- a label issued after the read 'matches zero rows once the replace commits, and leaves the row in the label queue for the next pass' -- and 6.1 makes update_community_label guarded by AND member_signature = %s. But 6.3.4 pins the discriminator to exactly eight values, none of which is 'the guarded UPDATE matched zero rows'. The labeler therefore logs written_model or gate_admitted for a write that did not happen, inflating the arm counts Q1.6 derives from those events.","finding_id":"F-N6","fix":"Add a ninth value write_superseded, emitted when the guarded UPDATE affects zero rows, state that it does not stamp label_attempted_at because the row is legitimately requeued, and change 6.3.4 to nine values with the exhaustiveness test extended.","location":"6.3.4 outcome discriminator","prevention":"Cross-check the outcome enumeration against every guarded write the design describes.","principle":"An exhaustively enumerated outcome discriminator must include every reachable terminal state, including one the design deliberately creates.","section_id":"6.3","severity":"nit"},{"category":"unhandled-edge","check_key":"stale-rewrite-leaves-stale-provenance-fields","description":"The F4 fold made read_for_context the single place the stale rule is applied, returning the row with label set to label_deterministic and label_source set to Deterministic. It says nothing about label_confidence or label_model. 5.1's CommunityEvidence and CommunityFact both carry label_confidence, so a stale row yields label_source 'deterministic' together with the superseded model's confidence; and because 6.3 deliberately persists confidence on genuinely rejected deterministic rows, a consumer cannot distinguish 'confidence of the shown label' from 'confidence of a label no longer shown'. 4.2's ViewCommunity and 4.4's GraphReportCommunity do not carry the field and are unaffected.","finding_id":"F-N7","fix":"State in 3.2 that the stale rewrite also sets label_confidence and label_model to None in the returned StoredCommunity, leaving the stored row untouched, and extend acceptance 3.2.10 to name all four rewritten fields.","location":"3.2 read_for_context stale rule","prevention":"List every field that describes the substituted value and rewrite all of them together.","principle":"When a read path substitutes one label for another it must substitute every field describing that label, or the payload asserts provenance for a value it is not showing.","section_id":"3.2","severity":"nit"},{"category":"weak-testability","check_key":"canonical-key-encoding-underspecified","description":"5.1 specifies only that canonical_key 'encodes size-desc as usize::MAX - size then community_id'. canonical_key returns a String and every existing arm in crates/gcode/src/evidence/contracts.rs leads with a variant discriminator and zero-pads each numeric component as {:020}. Without both, community items interleave with source and graph items in a mixed response, and usize::MAX - size sorts by digit count rather than magnitude. The named test communities_list_orders_by_size_then_id catches the padding bug only if its fixture straddles a power-of-ten boundary.","finding_id":"F-N8","fix":"State the encoding explicitly with a leading variant discriminator and {:020} padding on each numeric component, matching the existing arms, and require the ordering test fixture to include community sizes that straddle a power-of-ten boundary.","location":"5.1 canonical_key sentence","prevention":"Specify a new sort-key arm against the existing arms' encoding, and make the ordering fixture straddle a digit boundary.","principle":"A string sort key encoding numeric order must be zero-padded and namespaced by variant, or lexicographic comparison silently disagrees with the intended order.","section_id":"5.1","severity":"nit"},{"category":"traceability","check_key":"target-scope-reason-contradicts-consumer-sweep","description":"2.1 targets crates/gcode/src/commands/graph/view/mcg.rs with scope-reason 'import ImportIdentity from its new module', but mcg.rs contains no McgIdentity or ImportIdentity reference today -- its only identity-related lines are `mod identity;` and `pub(crate) use identity::McgSeedSelector;`. The plan's own Constraints consumer sweep correctly lists McgIdentity in exactly identity.rs, fetch.rs and tests.rs, not mcg.rs, so the Targets block and the sweep contradict each other. The ImportIdentity import mcg.rs genuinely needs arrives with 4.2's label_communities, not 2.1. Consequence: the shared-target chain entry 'mcg.rs ... 2.1 -> 4.2 (via 3.2)' rests on a target 2.1 may never touch. No build impact, so nit.","finding_id":"F-N9","fix":"Either drop the mcg.rs entry from 2.1's Targets and re-derive the shared-target chain so mcg.rs is solely owned by 4.2, or correct the scope-reason to the change 2.1 actually makes there so the executor does not invent an import.","location":"2.1 Targets, mcg.rs entry","prevention":"Reconcile each Targets entry against the consumer sweep before finalizing.","principle":"A Target's scope-reason must describe a change the deliverable actually makes in that file, and must agree with the plan's own consumer sweep.","section_id":"2.1","severity":"nit"},{"category":"traceability","check_key":"operation-set-enumerations-untargeted","description":"Two owned surfaces enumerate the gcode evidence operation set and no deliverable targets either: src/gobby/install/shared/skills/gobby/references/code-index/ask.md ('Search, read, graph, and commit-patch evidence without starting an Ask run') is live bundled skill text -- the same artifact class 7.1 updates for the sibling references/code-index/graphs.md -- and docs/guides/code-index.md repeats the enumeration. 5.2 already targets docs/contracts/ask.md, docs/guides/ask.md and docs/contracts/gcode-cli.md for exactly this reason, so the omission leaves the agent-facing skill and the code-index guide describing a three-operation surface after it becomes four.","finding_id":"F-N10","fix":"Add src/gobby/install/shared/skills/gobby/references/code-index/ask.md and docs/guides/code-index.md as bare-path Targets on 5.2 (or fold the skill file into 7.1 beside graphs.md), and extend acceptance 5.2.4 to name the skill reference the way 7.1.1 pins graphs.md.","location":"5.2 Targets","prevention":"Sweep bundled skill text and guides for the enumeration, not only contracts and code.","principle":"Every owned surface that enumerates a set being extended is a consumer and must be targeted.","repairs":[{"entries":["`src/gobby/install/shared/skills/gobby/references/code-index/ask.md`","`docs/guides/code-index.md`"],"kind":"add_targets","section_id":"5.2"}],"section_id":"5.2","severity":"nit"},{"category":"traceability","check_key":"new-config-without-settings-surface-or-recorded-exclusion","description":"6.1 adds CodeIndexCommunityLabelConfig under CodeIndexConfig.community_label and targets the generated runtime_config_contract.json and runtimeConfigCodecVectors.gen.ts plus the configuration audit, but not web/src/components/settings/sections/RuntimeInfrastructureSection.tsx, which hardcodes the six code_index.symbol_summary.* paths and renders them. The new settings -- including decisions_api_base, decisions_min_confidence and the secret-classified decisions_api_key that 6.1.2 calls out -- would have no UI. No test forces the coverage, so this is an owned coverage gap rather than a build break. Note for routing: this is the plan's only genuine hand-maintained web/ production surface, and it sits in 6.1, not 4.3.","finding_id":"F-N11","fix":"Either add web/src/components/settings/sections/RuntimeInfrastructureSection.tsx and its test to 6.1's Targets with a scope-reason to render the community_label settings beside symbol_summary, or state explicitly in 6.1 that the settings UI is deliberately out of scope and community_label is daemon-config-only, so the omission is a recorded decision.","location":"6.1 Targets","prevention":"When adding a config group, check the hand-maintained settings surface that renders the sibling group, not only the generated artifacts.","principle":"A new user-facing config group should either reach the surface that renders its sibling group, or the exclusion should be a recorded decision.","section_id":"6.1","severity":"nit"}],"notes":"Blocking findings prevent approval, so no manifest_entries are returned this round. The shadow manifest derived valid with 17 entries (1:1 with the 17 kind: deliverable sections), digest 59c4db2f379b77ae7328a8357ca67081357f9f7ec8309ee46900a6f9b3d9fe2a, and the 4.3 -> backend routing override was accepted by the server. Three lanes ran as read-only provider-native internal subagents; none edited, mutated or emitted findings. The runtime lane emitted absolute paths and one sha256 containing a stray space (lifecycle.rs); the reviewer recomputed and corrected every cited hash before validation.","plan_hash":"5672dab819875aeaae5d9a1ad235dcd33c4e6c41b09458ecab27c9c88bdf3b32","plan_id":"gcode-import-communities","plan_identity_precondition":"pass: **Plan ID:** gcode-import-communities at line 5, outside all fenced blocks; no covers:unknown label anywhere","reviewer_session":"6e5645d9-a580-488b-a1c1-79637135cfd0","round":1,"routing_decisions":{"4.3":{"implementation_domain":"backend"}},"routing_rationale":"Verified independently against the snapshot: all sixteen of 4.3's Targets are under crates/, tests/, docs/ and Cargo files, with zero web/ or frontend surface. The server's derived 'frontend' is a false positive. NOTE for the next round: 6.1 does target a real web/ surface (web/src/api/runtimeConfigCodecVectors.gen.ts, generated) and F-N11 proposes adding the hand-maintained RuntimeInfrastructureSection.tsx; confirm 6.1's derived domain at approval.","verdict":"needs_review","verified_clean":{"deterministic_sweep_spot_check":"Agrees. Reviewer independently verified: migration 441 is the highest on disk so 442 is free; all five contract_version 10 pins exist at the exact cited locations; McgIdentity, load_identity and assign_leiden_communities resolve at the cited spans; every Constraints file-size figure is exact at HEAD; no bare test-file acceptance artifacts; all N.N.N references resolve; test seam directories exist or are created by the plan's own deliverables.","evidence_integrity":"The canonical file at .gobby/plans/gcode-import-communities.md hashes to 5672dab8...bdf3b32, byte-identical to the snapshot plan_hash, so lane reads of the file were reads of the immutable bytes.","prior_findings_genuinely_resolved":["F1 (3.2 reads prior rows FOR UPDATE inside begin_replace; race closed)","F2 (2.2.7 is a measured-and-recorded outcome)","F3 (LabelSource, LabelCarry, CommunityFact, GeneratedLabel each have a distinct home file, distinct consumer layer and acceptance item)","F4 (stale rule applied once in read_for_context and inherited consistently by 4.2, 4.3, 4.4 and 5.1 -- but see F-N7 for the two unrewritten provenance fields)","F5 (item_type mirrors the Rust serde tag that validation_models.py:94-97 keys on)","F6 (5.1.1 states the flat internally tagged request; verified no field-name collision against the existing EvidenceRequest)","F7 (3.1.6 targets the privilege-manifest test with its exact-set assertion)","F-N1 (the unearned GIN index is gone -- but see F-N1 above for the same class on idx_cc_project_size)","F-N3 (the shared-target chain reproduces the Targets blocks exactly: all 16 multi-owner paths listed, every one carrying a real dependency edge)","F-N4, F-N5, F-N6 (docs targeted, research spans corrected, gate outcomes pinned)"],"prior_findings_only_partly_resolved":["F8 -- see F-09: Q1.7's one-second communities-view bar still has no owning leaf or acceptance item"],"proportionality":"No structural over-engineering beyond F-N1. The four new label types, the centrality() facade, the graph_view.rs extraction, the partition signature and the Jev gate each have a named consumer or a stated requirement. Plan ambition and size are not findings."}},"session_id":"7e1958df-7aa2-47c2-b32e-783a0061e0ef"}
```

Coordinator fold of round 1 (2026-09-19), under the user's delegation: all twenty-two
findings accepted; F-01, F-09, and F-N10 landed as typed repairs and the rest by hand.
Variations from the reviewer's fix text: F-02 lands in 3.2, which already owns
`lifecycle.rs`, as 3.2.13 rather than in 3.1; F-05 adds `partition_refreshed` and
`MISSING_PARTITION_HINT` to 3.2 so 5.1 can tell never-refreshed from empty; F-11's mirror
in 5.1 keeps the evidence layer's no-error posture (ambiguity returns every match with a
warning, no match is `CompleteEmpty`); F-N4 and F-N6 together make 6.3.4 a nine-value test
on its own symbol; F-N11 is recorded as a scope decision rather than a new target; F-08 is folded as
prose defining `D1.1` plus a `deferred_from` key, because the validator rejects an
`**Acceptance:**` block on a deferred section and the yaml list is the item's definition.

## M1 Task Manifest
`kind: manifest`

```yaml
- title: Add `communities()` and `GraphInputError` to gcore graph analytics
  category: code
  task_type: feature
  depends_on: []
  validation_criteria: '1.1.1: `communities(&AnalyticsGraph)` returns the same partition
    as `analyze` on the seeded graph and skips the other passes. symbol: `communities`.
    file: `crates/gcore/src/graph_analytics.rs`.

    1.1.2: `GraphInputError` is returned for duplicate node, unknown endpoint, invalid
    weight, and self-loop instead of silent sanitizing. symbol: `GraphInputError`.
    file: `crates/gcore/src/graph_analytics.rs`.

    1.1.3: Empty and edge-less graphs behave as `analyze` does. test: `crates/gcore/src/graph_analytics/tests.rs::communities_without_edges_is_all_singletons`.

    1.1.4: The pre-existing inline test module lives in `crates/gcore/src/graph_analytics/tests.rs`
    under `#[path]` and `crates/gcore/src/graph_analytics.rs` declares no `#[cfg(test)]`
    module. file: `crates/gcore/src/graph_analytics/tests.rs`.'
  labels:
  - covers:gcode-import-communities:1.1:1.1.1
  - covers:gcode-import-communities:1.1:1.1.2
  - covers:gcode-import-communities:1.1:1.1.3
  - covers:gcode-import-communities:1.1:1.1.4
  tdd: true
  source_section: '1.1'
  implementation_domain: backend
- title: Add `centrality()` and stop `graph report` from running Leiden twice
  category: code
  task_type: feature
  depends_on:
  - '1.1'
  validation_criteria: '1.2.1: `centrality(&AnalyticsGraph)` returns the same scores
    as `analyze(...).centrality` on the seeded graph. test: `crates/gcore/src/graph_analytics/tests.rs::centrality_matches_analyze_centrality`.

    1.2.2: No `analyze` call remains in `crates/gcode/src/graph/report/summary.rs`;
    report hotspots and bridges are byte-identical before and after on the Gobby checkout.
    file: `crates/gcode/src/graph/report/summary.rs`.

    1.2.3: `gcode graph report` median wall time over five runs is recorded before
    and after the `centrality()` swap with no regression. behavior: "median" in `docs/evidence/community-labels-2026-09/report-latency.md`.'
  labels:
  - covers:gcode-import-communities:1.2:1.2.1
  - covers:gcode-import-communities:1.2:1.2.2
  - covers:gcode-import-communities:1.2:1.2.3
  tdd: true
  source_section: '1.2'
  implementation_domain: backend
- title: Move `McgIdentity` to `communities::identity::ImportIdentity` with a connection-taking
    loader
  category: refactor
  task_type: feature
  depends_on: []
  validation_criteria: '2.1.1: `ImportIdentity` lives in `crates/gcode/src/communities/identity.rs`
    with `unique_provider` and `from_resolution` reachable crate-wide; `crates/gcode/src/commands/graph/view/mcg/identity.rs`
    keeps only seed resolution. symbol: `ImportIdentity`. file: `crates/gcode/src/communities/identity.rs`.

    2.1.2: `load_project_imports(conn, ctx)` returns the identity and the import rows
    on a caller-supplied connection; `load_identity` is gone. symbol: `load_project_imports`.
    file: `crates/gcode/src/communities/identity.rs`.

    2.1.3: The moved identity tests pass unchanged in their new file. test: `crates/gcode/src/communities/identity_tests.rs::mcg_identity_build_handles_twenty_thousand_rows`.

    2.1.4: Under an overlay context `load_project_imports` returns import rows for
    every visible file, parent-only files included, with parent rows the overlay shadows
    by `file_path` dropped. test: `crates/gcode/src/communities/identity_tests.rs::overlay_partition_covers_parent_only_files`.'
  labels:
  - covers:gcode-import-communities:2.1:2.1.1
  - covers:gcode-import-communities:2.1:2.1.2
  - covers:gcode-import-communities:2.1:2.1.3
  - covers:gcode-import-communities:2.1:2.1.4
  tdd: false
  source_section: '2.1'
  assigned_agent: backend-developer
- title: 'Build the partition: graph, Leiden, splits, cohesion, deterministic labels,
    signatures'
  category: code
  task_type: feature
  depends_on:
  - '1.1'
  - '2.1'
  validation_criteria: '2.2.1: `build_partition` produces one node per visible file
    and one folded undirected edge per importer/provider pair, excluding external,
    ambiguous, and self imports. symbol: `build_partition`. file: `crates/gcode/src/communities/partition.rs`.

    2.2.2: Oversized and low-cohesion splits apply once each with Graphify''s constants
    in integer math. test: `crates/gcode/src/communities/partition_tests.rs::cohesion_threshold_uses_integer_math`.

    2.2.3: Cohesion, in-degree, and the 16-hex member signature are computed per community
    and the signature matches Graphify''s shape. test: `crates/gcode/src/communities/partition_tests.rs::member_signature_matches_graphify_shape`.

    2.2.4: Deterministic labels follow the dominant-directory rule with the in-degree
    fallback for root files. test: `crates/gcode/src/communities/partition_tests.rs::label_prefers_deepest_majority_prefix`.

    2.2.5: A provider outside the visible set surfaces as `PartitionError::ProviderNotVisible`;
    kernel input errors surface as `PartitionError::InvalidGraph`. symbol: `PartitionError`.
    file: `crates/gcode/src/communities/partition.rs`.

    2.2.6: The partition is deterministic under row reordering. test: `crates/gcode/src/communities/partition_tests.rs::partition_is_invariant_to_row_order`.

    2.2.7: The threshold spike ran on both corpora before close and `thresholds.md`
    records, per corpus, the size distribution, the largest community''s share of
    visible files against the 25% target as met or exceeded with the cause of any
    exceedance, and the constants kept or replaced; an exceedance is a recorded outcome,
    not a close blocker. behavior: "largest" in `docs/evidence/community-labels-2026-09/thresholds.md`.

    2.2.8: Colliding deterministic labels receive ` #2`, ` #3` suffixes in partition
    order so the largest community keeps the bare label. test: `crates/gcode/src/communities/partition_tests.rs::label_collision_gets_ordinal_suffix`.

    2.2.9: `label_candidates` returns up to four distinct strings led by the deterministic
    label. test: `crates/gcode/src/communities/partition_tests.rs::candidates_are_distinct_and_lead_with_deterministic`.

    2.2.10: `partition_signature` changes when an import is added or removed between
    existing members while every `member_signature` is unchanged. test: `crates/gcode/src/communities/partition_tests.rs::partition_signature_covers_edges`.

    2.2.11: `thresholds.md` records that no `community-N` placeholder appears in any
    stored label or view output on either corpus. behavior: "placeholder" in `docs/evidence/community-labels-2026-09/thresholds.md`.

    2.2.12: `thresholds.md` records that communities of five or more files on Gobby
    carry deterministic labels naming subsystems. behavior: "subsystems" in `docs/evidence/community-labels-2026-09/thresholds.md`.'
  labels:
  - covers:gcode-import-communities:2.2:2.2.1
  - covers:gcode-import-communities:2.2:2.2.2
  - covers:gcode-import-communities:2.2:2.2.3
  - covers:gcode-import-communities:2.2:2.2.4
  - covers:gcode-import-communities:2.2:2.2.5
  - covers:gcode-import-communities:2.2:2.2.6
  - covers:gcode-import-communities:2.2:2.2.7
  - covers:gcode-import-communities:2.2:2.2.8
  - covers:gcode-import-communities:2.2:2.2.9
  - covers:gcode-import-communities:2.2:2.2.10
  - covers:gcode-import-communities:2.2:2.2.11
  - covers:gcode-import-communities:2.2:2.2.12
  tdd: true
  source_section: '2.2'
  implementation_domain: backend
- title: Remap ids against the stored partition and carry labels forward
  category: code
  task_type: feature
  depends_on:
  - '2.2'
  validation_criteria: '2.3.1: `assign_ids` matches greedily by Jaccard, overlap,
    old id, new index and keeps an id on the larger child of a split and the larger
    parent of a merge. test: `crates/gcode/src/communities/remap_tests.rs::split_keeps_id_on_larger_child_and_issues_fresh_id`.

    2.3.2: Unmatched communities take ids above the watermark and a retired id is
    never reissued. test: `crates/gcode/src/communities/remap_tests.rs::retired_ids_are_never_reissued`.

    2.3.3: Label fields carry forward by the four-way rule and a model label with
    a changed signature is carried as stale. symbol: `assign_ids`. file: `crates/gcode/src/communities/remap.rs`.

    2.3.4: A deterministic row whose membership changed comes back with the four fields
    `labeled_signature`, `label_confidence`, `label_model`, and `labeled_at` cleared
    and `label_attempted_at` carried unchanged. test: `crates/gcode/src/communities/remap_tests.rs::gate_rejected_label_reopens_on_membership_change`.

    2.3.5: `LabelSource` maps exactly to the text values `deterministic` and `model`
    and round-trips them. symbol: `LabelSource`. file: `crates/gcode/src/communities.rs`.

    2.3.6: `LabelCarry` carries every label column the four-way rule assigns, including
    the recomputed `label_candidates`. symbol: `LabelCarry`. file: `crates/gcode/src/communities/remap.rs`.'
  labels:
  - covers:gcode-import-communities:2.3:2.3.1
  - covers:gcode-import-communities:2.3:2.3.2
  - covers:gcode-import-communities:2.3:2.3.3
  - covers:gcode-import-communities:2.3:2.3.4
  - covers:gcode-import-communities:2.3:2.3.5
  - covers:gcode-import-communities:2.3:2.3.6
  tdd: true
  source_section: '2.3'
  implementation_domain: backend
- title: 'Migration 442: `code_communities`, the id watermark, and every derived carrier'
  category: code
  task_type: feature
  depends_on: []
  validation_criteria: '3.1.1: The new migration (442, or the next free number at
    landing) creates `code_communities` with the constraints, the `idx_cc_label_queue`
    index, machine-scoped RLS policies, and GRANTs above, and adds `community_id_watermark`
    and `partition_signature` to `code_indexed_project_states`. file: `crates/gcore/assets/schema/migrations/442_add_code_communities.sql`.

    3.1.2: `baseline.sql`, `catalog.manifest.json`, `assets.rs::MIGRATIONS`, `crates/gcore/src/grant/bundle.rs`,
    `schema_contract.rs`, `gdaemon` `cli_contract.rs`, `schema_expected_identity.json`,
    and the five goldens agree on the new latest version (442, or the next free number
    at landing). file: `src/gobby/storage/schema_expected_identity.json`.

    3.1.3: `GCODE_RLS_TABLES` lists twelve tables and the runner tests assert the
    machine-scoped predicates on `code_communities`. file: `crates/gcore/src/schema/runner_tests.rs`.

    3.1.4: The privilege manifest grants `code_communities` to the gcode capability
    with the same scope declaration as `code_indexed_project_states`, and gcode''s
    `schema.rs` contracts include the table and both new columns. file: `crates/gcode/security/managed_postgres_privileges.json`.

    3.1.5: `uv run gobby restart` applies 442 on the live hub and `gdaemon schema
    plan` reports nothing pending afterwards, as recorded in the apply log. behavior:
    "nothing pending" in `docs/evidence/community-labels-2026-09/schema-apply.md`.

    3.1.6: The managed-relation set assertion names `code_communities`. test: `tests/code_index/test_gcode_privilege_manifest.py::test_manifest_privileges_match_the_managed_relation_set`.

    3.1.7: The migration number actually used replaces every occurrence of the provisional
    442 named in Constraints in the same commit, and the apply log and V1 record it.
    behavior: "landed as migration" in `docs/evidence/community-labels-2026-09/schema-apply.md`.'
  labels:
  - covers:gcode-import-communities:3.1:3.1.1
  - covers:gcode-import-communities:3.1:3.1.2
  - covers:gcode-import-communities:3.1:3.1.3
  - covers:gcode-import-communities:3.1:3.1.4
  - covers:gcode-import-communities:3.1:3.1.5
  - covers:gcode-import-communities:3.1:3.1.6
  - covers:gcode-import-communities:3.1:3.1.7
  tdd: true
  source_section: '3.1'
  implementation_domain: backend
- title: Persist the partition at index time and expose the read API
  category: code
  task_type: feature
  depends_on:
  - '2.3'
  - '3.1'
  validation_criteria: '3.2.1: `gcode index` (full, incremental, hook, and overlay
    runs) refreshes `code_communities` after project stats and before projection sync,
    reading the prior rows under row locks and replacing them in the same transaction
    that advances the watermark and writes the partition signature. symbol: `refresh_project_communities`.
    file: `crates/gcode/src/communities.rs`.

    3.2.2: A partition signature equal to the stored `partition_signature` skips the
    write. test: `crates/gcode/src/communities/refresh_tests.rs::unchanged_partition_skips_write`.

    3.2.3: A refresh failure degrades the outcome with `CommunityRefreshFailed` and
    never fails the index run. test: `crates/gcode/src/communities/refresh_tests.rs::refresh_failure_degrades_index_outcome`.

    3.2.4: `read_for_context` returns overlay rows first and parent rows as fallback,
    with `label_stale` derived from the signatures. symbol: `read_for_context`. file:
    `crates/gcode/src/communities.rs`.

    3.2.5: `IndexOutcome.communities` is additive and absent when `None`; the `index`
    contract keys and both pinned JSON copies include it. file: `crates/gcode/contract/gcode.contract.json`.

    3.2.6: The privilege manifest''s source inventory lists `crates/gcode/src/db/communities.rs`.
    file: `crates/gcode/security/managed_postgres_privileges.json`.

    3.2.7: A label the daemon writes while a refresh is in flight is either carried
    forward or left queued; it is never discarded. test: `crates/gcode/src/communities/refresh_tests.rs::label_written_during_refresh_is_not_lost`.

    3.2.8: An overlay project''s first refresh seeds ids, watermark, and label fields
    from the parent project. test: `crates/gcode/src/communities/refresh_tests.rs::overlay_refresh_seeds_prior_rows_from_parent`.

    3.2.9: An import added or removed between existing members rewrites the rows with
    new `internal_edges`, `cohesion`, `representatives`, and `boundary` although no
    `member_signature` changed. test: `crates/gcode/src/communities/refresh_tests.rs::edge_change_without_membership_change_rewrites_rows`.

    3.2.10: `read_for_context` returns a stale model-labeled row with `label` rewritten
    to the deterministic label, `label_source` deterministic, `label_confidence` and
    `label_model` `None`, and `label_stale` true, leaving the stored row unchanged.
    test: `crates/gcode/src/communities/refresh_tests.rs::stale_model_label_reads_as_deterministic`.

    3.2.11: The Q1.4 churn experiment ran on both corpora before close, with one row
    seeded to `label_source=''model''` by direct `UPDATE` before the rename step so
    the `label_stale` flip is observable, and its row diffs are recorded. behavior:
    "member_signature" in `docs/evidence/community-labels-2026-09/churn.md`.

    3.2.12: Incremental single-file index latency before and after this leaf is recorded
    against the +250 ms p50 budget on both corpora. behavior: "p50" in `docs/evidence/community-labels-2026-09/refresh-latency.md`.

    3.2.13: `invalidate` removes this machine''s `code_communities` rows in the same
    transaction that removes its `code_indexed_project_states` row. test: `crates/gcode/src/communities/refresh_tests.rs::invalidate_removes_community_rows_with_project_state`.

    3.2.14: An overlay''s first refresh commits rows under the overlay id even when
    its partition equals the parent''s, because the skip compares against the overlay''s
    own stored signature. test: `crates/gcode/src/communities/refresh_tests.rs::overlay_first_refresh_commits_when_partition_matches_parent`.

    3.2.15: The overlay seed reads the parent''s rows under `FOR UPDATE` on the parent''s
    state row in one READ COMMITTED transaction and observes a concurrent parent replace
    only after it commits. test: `crates/gcode/src/communities/refresh_tests.rs::overlay_seed_locks_parent_state_row`.'
  labels:
  - covers:gcode-import-communities:3.2:3.2.1
  - covers:gcode-import-communities:3.2:3.2.2
  - covers:gcode-import-communities:3.2:3.2.3
  - covers:gcode-import-communities:3.2:3.2.4
  - covers:gcode-import-communities:3.2:3.2.5
  - covers:gcode-import-communities:3.2:3.2.6
  - covers:gcode-import-communities:3.2:3.2.7
  - covers:gcode-import-communities:3.2:3.2.8
  - covers:gcode-import-communities:3.2:3.2.9
  - covers:gcode-import-communities:3.2:3.2.10
  - covers:gcode-import-communities:3.2:3.2.11
  - covers:gcode-import-communities:3.2:3.2.12
  - covers:gcode-import-communities:3.2:3.2.13
  - covers:gcode-import-communities:3.2:3.2.14
  - covers:gcode-import-communities:3.2:3.2.15
  tdd: true
  source_section: '3.2'
  implementation_domain: backend
- title: Extract the graph-view CLI types into `crates/gcode/src/cli/graph_view.rs`
  category: refactor
  task_type: feature
  depends_on: []
  validation_criteria: '4.1.1: The graph-view CLI types and impls live in `crates/gcode/src/cli/graph_view.rs`
    and `cli.rs` is under 700 lines with unchanged public paths. file: `crates/gcode/src/cli/graph_view.rs`.

    4.1.2: Every existing projection test passes without edits and the moved types
    keep their names. test: `crates/gcode/src/cli/tests/projection.rs::graph_view_requires_typed_compatible_selector`.'
  labels:
  - covers:gcode-import-communities:4.1:4.1.1
  - covers:gcode-import-communities:4.1:4.1.2
  tdd: false
  source_section: '4.1'
  assigned_agent: backend-developer
- title: Label MCG nodes from stored rows and extend the view payload shapes
  category: code
  task_type: feature
  depends_on:
  - '3.2'
  validation_criteria: '4.2.1: MCG file and uniquely resolved module nodes carry the
    stored community label; external and ambiguous modules carry none; no `graph_analytics`
    import remains in `mcg.rs`. symbol: `label_communities`. file: `crates/gcode/src/commands/graph/view/mcg.rs`.

    4.2.2: With no stored rows every node''s `community` is null and the payload carries
    the missing-partition hint. test: `crates/gcode/src/commands/graph/view/mcg/tests.rs::mcg_without_stored_partition_hints_and_leaves_null`.

    4.2.3: `ViewCommunity` carries id, label, size, cohesion, label_source, label_stale,
    and view-scoped nodes; `build_view_payload` rejects membership outside `nodes[]`.
    test: `crates/gcode/src/commands/graph/view/render_tests.rs::payload_rejects_unknown_community_member`.

    4.2.4: `NodeKind::Community` exists with a null file and the `community:` key
    prefix, `ViewEdge.count` is optional, and `analytics_graph_from_payload` is deleted.
    symbol: `NodeKind`. file: `crates/gcode/src/commands/graph/view/render.rs`.

    4.2.5: Mermaid output groups clustered nodes into one subgraph per community titled
    by its label, with unclustered nodes at top level and edges after the blocks.
    test: `crates/gcode/src/commands/graph/view/render_tests.rs::mermaid_groups_nodes_into_community_subgraphs`.'
  labels:
  - covers:gcode-import-communities:4.2:4.2.1
  - covers:gcode-import-communities:4.2:4.2.2
  - covers:gcode-import-communities:4.2:4.2.3
  - covers:gcode-import-communities:4.2:4.2.4
  - covers:gcode-import-communities:4.2:4.2.5
  tdd: true
  source_section: '4.2'
  implementation_domain: backend
- title: Add the communities view kind, `--min-size`, `--community`, the view module,
    and contract v11
  category: code
  task_type: feature
  depends_on:
  - '3.2'
  - '4.1'
  - '4.2'
  validation_criteria: '4.3.1: `--view=communities` parses without a seed and accepts
    `--min-size` and `--community`. test: `crates/gcode/src/cli/tests/projection.rs::communities_parses_without_seed`.

    4.3.2: The list view emits one node per listed community, inter-community `IMPORTS`
    edges with counts, one `communities[]` entry per listed row with empty `nodes`,
    and valid Mermaid, reading stored rows only. symbol: `run_list`. file: `crates/gcode/src/commands/graph/view/communities.rs`.

    4.3.3: The detail view resolves an integer id, a case-insensitive exact label,
    an exact deterministic label, or a unique case-insensitive substring of `label`
    or `label_deterministic`, never of members. test: `crates/gcode/src/commands/graph/view/communities/tests.rs::detail_resolves_id_label_and_unique_substring`.

    4.3.4: Routing covers every view and seed combination including the seedless form.
    test: `crates/gcode/src/commands/graph/view/tests.rs::run_routes_every_view_and_seed_combination`.

    4.3.5: The contract advertises `communities`, `--min-size`, and `--community`
    at version 11 in `contract.rs`, both pinned JSON copies, the Rust and Python pins,
    and `docs/contracts/gcode-cli.md`. file: `tests/contracts/gcode.contract.json`.

    4.3.6: The gcode crate version is 1.9.0. behavior: "1.9.0" in `crates/gcode/Cargo.toml`.

    4.3.7: `--community <path>` resolves to the community whose members contain the
    path. test: `crates/gcode/src/commands/graph/view/communities/tests.rs::detail_resolves_member_path`.

    4.3.8: The detail view caps members at 50 and neighbors at 12 and sets `outgoing_truncated`/`incoming_truncated`
    when it does. test: `crates/gcode/src/commands/graph/view/communities/tests.rs::detail_truncates_members_and_neighbors_with_flags`.

    4.3.9: A path that belongs to no community is a typed exit-2 error naming it.
    test: `crates/gcode/src/commands/graph/view/communities/tests.rs::detail_unknown_path_is_typed_error`.

    4.3.10: An ambiguous substring is a typed exit-2 error listing up to five matches.
    test: `crates/gcode/src/commands/graph/view/communities/tests.rs::detail_ambiguous_substring_is_typed_error`.

    4.3.11: A project with no stored rows lists nothing and carries the missing-partition
    hint. test: `crates/gcode/src/commands/graph/view/communities/tests.rs::list_without_rows_hints`.

    4.3.12: `--view=communities` rejects other seeds, `--depth`, and row limits with
    typed clap conflicts. test: `crates/gcode/src/cli/tests/projection.rs::communities_rejects_other_seeds_depth_and_row_limits`.

    4.3.13: `--min-size` is rejected off communities and at zero. test: `crates/gcode/src/cli/tests/projection.rs::min_size_rejected_off_communities_and_at_zero`.

    4.3.14: `docs/contracts/gcode-cli.md` states `contract_version`: 11 at its version
    line and `docs/guides/gcode-user-guide.md` names the communities view kind with
    its seedless form. behavior: "--view communities" in `docs/guides/gcode-user-guide.md`.

    4.3.15: `gcode graph view --view communities` on the Gobby checkout completes
    in under one second from stored rows over five runs. behavior: "under one second"
    in `docs/evidence/community-labels-2026-09/view-latency.md`.

    4.3.16: At every rung a selector that matches nothing, an unknown integer id included,
    is a typed exit-2 error naming it, and more than one match, two rows sharing an
    exact label included, is a typed exit-2 error listing up to five matches. test:
    `crates/gcode/src/commands/graph/view/communities/tests.rs::detail_no_match_and_multi_match_are_typed_errors_at_every_rung`.'
  labels:
  - covers:gcode-import-communities:4.3:4.3.1
  - covers:gcode-import-communities:4.3:4.3.2
  - covers:gcode-import-communities:4.3:4.3.3
  - covers:gcode-import-communities:4.3:4.3.4
  - covers:gcode-import-communities:4.3:4.3.5
  - covers:gcode-import-communities:4.3:4.3.6
  - covers:gcode-import-communities:4.3:4.3.7
  - covers:gcode-import-communities:4.3:4.3.8
  - covers:gcode-import-communities:4.3:4.3.9
  - covers:gcode-import-communities:4.3:4.3.10
  - covers:gcode-import-communities:4.3:4.3.11
  - covers:gcode-import-communities:4.3:4.3.12
  - covers:gcode-import-communities:4.3:4.3.13
  - covers:gcode-import-communities:4.3:4.3.14
  - covers:gcode-import-communities:4.3:4.3.15
  - covers:gcode-import-communities:4.3:4.3.16
  tdd: true
  source_section: '4.3'
  implementation_domain: backend
- title: Add the `## Import communities` section to `gcode graph report`
  category: code
  task_type: feature
  depends_on:
  - '1.2'
  - '4.3'
  validation_criteria: '4.4.1: `gcode graph report` includes an `Import communities`
    section and JSON key whose total and thin counts reconcile with the stored rows.
    test: `crates/gcode/src/graph/report/tests.rs::report_communities_section_counts_reconcile`.

    4.4.2: A project without stored communities degrades the optional input instead
    of failing the report. test: `crates/gcode/src/graph/report/tests.rs::report_without_communities_degrades_optional_input`.

    4.4.3: `graph_report_keys()` and both pinned JSON copies list `communities`. file:
    `crates/gcode/src/contract/schema.rs`.'
  labels:
  - covers:gcode-import-communities:4.4:4.4.1
  - covers:gcode-import-communities:4.4:4.4.2
  - covers:gcode-import-communities:4.4:4.4.3
  tdd: true
  source_section: '4.4'
  implementation_domain: backend
- title: Add `EvidenceOperation::Communities` and `EvidenceItem::Community` to gcode
    evidence
  category: code
  task_type: feature
  depends_on:
  - '4.4'
  validation_criteria: "5.1.1: `gcode evidence --request-json` accepts the flat internally\
    \ tagged request `{\"schema_version\": 1, \"binding\": \u2026, \"operation\":\
    \ \"communities\", \"communities\": {\u2026}, \"max_bytes\": \u2026}`, with the\
    \ selector as a sibling of `operation`, and returns `Community` items in list\
    \ and detail modes. symbol: `CommunitiesSelector`. file: `crates/gcode/src/evidence/contracts.rs`.\n\
    5.1.2: `evidence_id` is derived from binding, community id, and member signature\
    \ so it survives relabeling. test: `crates/gcode/src/evidence/tests.rs::communities_evidence_id_survives_relabel`.\n\
    5.1.3: Zero stored rows on a never-refreshed project report `CompleteEmpty` with\
    \ the `community_partition_missing` warning, below-threshold results report `CompleteEmpty`,\
    \ and over-limit results report `TruncatedIndex`; no community data condition\
    \ is an error. test: `crates/gcode/src/evidence/tests.rs::communities_without_rows_is_complete_empty_with_hint`.\n\
    5.1.4: `EvidenceFacts::project_communities` is required and implemented for `CodewikiFacts`\
    \ and `FakeFacts`. symbol: `EvidenceFacts`. file: `crates/gcode/src/evidence/mod.rs`.\n\
    5.1.5: `evidence_keys()`, the evidence contract test, and both pinned JSON copies\
    \ list the community item keys. file: `crates/gcode/src/contract/schema.rs`.\n\
    5.1.6: `CommunityFact` is the row shape `project_communities` returns inside `ProjectCommunities`\
    \ and carries the read-path label fields. symbol: `CommunityFact`. file: `crates/gcode/src/codewiki_facts/communities.rs`.\n\
    5.1.7: A refreshed project with no stored communities reports `CompleteEmpty`\
    \ without the missing-partition warning. test: `crates/gcode/src/evidence/tests.rs::communities_refreshed_but_empty_is_complete_empty`.\n\
    5.1.8: Detail mode resolves `label` through 4.3's ladder over `label` and `label_deterministic`,\
    \ never members, returns every match with the `community_selector_ambiguous` warning\
    \ when more than one row matches, and reports `CompleteEmpty` for no match. test:\
    \ `crates/gcode/src/evidence/tests.rs::communities_ambiguous_label_returns_every_match_with_warning`.\n\
    5.1.9: `canonical_key` for a community item leads with its variant discriminator\
    \ and zero-pads size and id, so community items sort after source and graph items\
    \ and by size across a power of ten. test: `crates/gcode/src/evidence/tests.rs::communities_list_orders_by_size_then_id`."
  labels:
  - covers:gcode-import-communities:5.1:5.1.1
  - covers:gcode-import-communities:5.1:5.1.2
  - covers:gcode-import-communities:5.1:5.1.3
  - covers:gcode-import-communities:5.1:5.1.4
  - covers:gcode-import-communities:5.1:5.1.5
  - covers:gcode-import-communities:5.1:5.1.6
  - covers:gcode-import-communities:5.1:5.1.7
  - covers:gcode-import-communities:5.1:5.1.8
  - covers:gcode-import-communities:5.1:5.1.9
  tdd: true
  source_section: '5.1'
  implementation_domain: backend
- title: Admit, validate, and describe the `communities` operation in Ask
  category: code
  task_type: feature
  depends_on:
  - '4.3'
  - '5.1'
  validation_criteria: '5.2.1: Ask admits `communities` in pipeline and interactive
    modes and forwards a schema-1 request with normalized selector defaults. symbol:
    `EvidenceAdmission._normalize_selector`. file: `src/gobby/ask/evidence.py`.

    5.2.2: `CommunityEvidenceItem` is a member of the `EvidenceItem` union and manifest
    validation accepts it. test: `tests/ask/test_validation.py::test_manifest_accepts_community_item`.

    5.2.3: A citation that targets a community item is rejected with a diagnostic
    naming the operation as non-citable. test: `tests/ask/test_validation.py::test_citation_of_community_item_is_rejected`.

    5.2.4: The MCP tool literals, descriptions, `docs/contracts/ask.md`, and the `gcode
    evidence` operations list describe the operation. behavior: "communities" in `docs/contracts/ask.md`.

    5.2.5: The user guide''s evidence-operation enumeration names `communities`. behavior:
    "communities" in `docs/guides/ask.md`.

    5.2.6: The code-index ask reference and the code-index guide enumerate `communities`
    with its non-citability sentence. behavior: "communities" in `src/gobby/install/shared/skills/gobby/references/code-index/ask.md`.'
  labels:
  - covers:gcode-import-communities:5.2:5.2.1
  - covers:gcode-import-communities:5.2:5.2.2
  - covers:gcode-import-communities:5.2:5.2.3
  - covers:gcode-import-communities:5.2:5.2.4
  - covers:gcode-import-communities:5.2:5.2.5
  - covers:gcode-import-communities:5.2:5.2.6
  tdd: true
  source_section: '5.2'
  implementation_domain: backend
- title: Community label storage, model, and configuration
  category: code
  task_type: feature
  depends_on:
  - '3.1'
  validation_criteria: '6.1.1: `CodeIndexCommunityStorageMixin` queues by signature
    mismatch with a cooloff and writes labels only when the member signature still
    matches. test: `tests/code_index/test_code_index_storage.py::test_update_community_label_is_signature_guarded`.

    6.1.2: `CodeIndexCommunityLabelConfig` exists with the fields above, `decisions_api_key`
    classifies as a secret, and the runtime config contract, codec vectors, and audit
    rows are regenerated. symbol: `CodeIndexCommunityLabelConfig`. file: `src/gobby/config/code_index.py`.

    6.1.3: `StoredCommunity` round-trips a `code_communities` row. symbol: `StoredCommunity`.
    file: `src/gobby/code_index/models.py`.'
  labels:
  - covers:gcode-import-communities:6.1:6.1.1
  - covers:gcode-import-communities:6.1:6.1.2
  - covers:gcode-import-communities:6.1:6.1.3
  tdd: true
  source_section: '6.1'
  implementation_domain: backend
- title: Generate and validate community names in the maintenance loop
  category: code
  task_type: feature
  depends_on:
  - '3.2'
  - '6.1'
  validation_criteria: '6.2.1: The maintenance pass labels queued communities after
    symbol summaries with keyword-only wiring that leaves existing call sites untouched.
    symbol: `_label_unlabeled_communities`. file: `src/gobby/code_index/maintenance.py`.

    6.2.2: Generated names pass schema validation and sanitation before any write,
    and a name equal to a deterministic candidate is stored as deterministic with
    `label_model` set to the generation profile and `labeled_signature` equal to the
    member signature, so the row leaves the label queue. test: `tests/code_index/test_community_labeler.py::test_generated_name_equal_to_candidate_writes_deterministic`.

    6.2.3: A generation failure stamps `label_attempted_at` and changes no label field.
    test: `tests/code_index/test_community_labeler.py::test_generation_failure_stamps_attempt_only`.

    6.2.4: Every labeling outcome emits one `code_index.community_label.outcome` log
    event with a discriminator value. test: `tests/code_index/test_community_labeler.py::test_every_outcome_emits_one_log_event`.

    6.2.5: With no `decisions_api_base` configured a validated name is written with
    `label_source=''model''`, `label_confidence` NULL, `label_model` set to the generation
    profile, and `labeled_signature` equal to the member signature. test: `tests/code_index/test_community_labeler.py::test_ungated_validated_name_writes_model_label`.

    6.2.6: `GeneratedLabel` carries the validated name, the never-persisted rationale,
    and the generation model. symbol: `GeneratedLabel`. file: `src/gobby/code_index/community_labeler.py`.

    6.2.7: A schema rejection and a sanitation rejection each stamp `label_attempted_at`
    and change no label field, so the row waits out the cooloff instead of regenerating
    on the next pass. test: `tests/code_index/test_community_labeler.py::test_rejections_stamp_attempt_only`.'
  labels:
  - covers:gcode-import-communities:6.2:6.2.1
  - covers:gcode-import-communities:6.2:6.2.2
  - covers:gcode-import-communities:6.2:6.2.3
  - covers:gcode-import-communities:6.2:6.2.4
  - covers:gcode-import-communities:6.2:6.2.5
  - covers:gcode-import-communities:6.2:6.2.6
  - covers:gcode-import-communities:6.2:6.2.7
  tdd: true
  source_section: '6.2'
  implementation_domain: backend
- title: Gate generated names through a Jev `choice` decision
  category: code
  task_type: feature
  depends_on:
  - '6.2'
  validation_criteria: '6.3.1: `DecisionsClient.choose` posts `{state, model, questions}`
    with bearer auth, retries only on 429/529, and matches answers by key. test: `tests/llm/test_decisions_client.py::test_choose_posts_state_model_questions`.

    6.3.2: A `generated` choice at or above the threshold writes the model label with
    confidence and model; anything else writes the deterministic label with confidence,
    model, and signature stamped; transport failure stamps the attempt only. test:
    `tests/code_index/test_community_labeler.py::test_gate_admits_at_threshold_and_rejects_below`.

    6.3.3: `docs/guides/llm-features.md` documents `code_index.community_label` and
    the decisions gate. behavior: "community_label" in `docs/guides/llm-features.md`.

    6.3.4: The outcome event''s discriminator admits exactly nine values, the five
    from 6.2 plus `gate_admitted`, `gate_rejected`, `transport_failed`, and `write_superseded`,
    where `write_superseded` stamps no field, and the exhaustiveness test covers all
    nine. test: `tests/code_index/test_community_labeler.py::test_gate_outcomes_extend_the_discriminator_to_nine`.

    6.3.5: The Q1.6 label-quality study ran with arms D, G, and J and `label-quality.md`
    records the metrics against every bar and the ship decision the live config follows.
    behavior: "ship" in `docs/evidence/community-labels-2026-09/label-quality.md`.

    6.3.6: The wire-shape spike records one keyed request against the 1P base and
    one against the OpenRouter alpha base before either is enabled. behavior: "alpha"
    in `docs/evidence/community-labels-2026-09/decisions-wire-spike.md`.'
  labels:
  - covers:gcode-import-communities:6.3:6.3.1
  - covers:gcode-import-communities:6.3:6.3.2
  - covers:gcode-import-communities:6.3:6.3.3
  - covers:gcode-import-communities:6.3:6.3.4
  - covers:gcode-import-communities:6.3:6.3.5
  - covers:gcode-import-communities:6.3:6.3.6
  tdd: true
  source_section: '6.3'
  implementation_domain: backend
- title: Update the code-index graphs reference and the Rust changelog
  category: docs
  task_type: feature
  depends_on:
  - '4.3'
  - '4.4'
  - '5.2'
  - '6.3'
  validation_criteria: '7.1.1: The graphs reference describes the persisted partition,
    the seedless and detail view forms, and `label_stale`. behavior: "--view communities"
    in `src/gobby/install/shared/skills/gobby/references/code-index/graphs.md`.

    7.1.2: The changelog records every added and changed surface under Unreleased,
    naming the migration number 3.1 landed under. behavior: "code_communities" in
    `crates/CHANGELOG.md`.

    7.1.3: The Q1.8 live smoke ran after the cutover and each command''s outcome is
    recorded. behavior: "--view communities" in `docs/evidence/community-labels-2026-09/live-smoke.md`.'
  labels:
  - covers:gcode-import-communities:7.1:7.1.1
  - covers:gcode-import-communities:7.1:7.1.2
  - covers:gcode-import-communities:7.1:7.1.3
  tdd: false
  source_section: '7.1'
  assigned_agent: tech-writer
```
