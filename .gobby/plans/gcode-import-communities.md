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

This plan builds the graph Leiden should see: one node per visible internal source
file, one weighted undirected edge per importer-to-provider relationship resolved
through the existing `McgIdentity` provider map, external and ambiguous modules
excluded, clustered once per call over the whole visible project from PostgreSQL
active import rows. MCG nodes are labeled from that partition, and a new
`--view=communities` lists each community with its files and the inter-community
import edges. Labels are content-derived (dominant directory) so they stay stable
under small index changes.

Decision record (confirmed in conversation):

- Scope: whole visible project, computed per call, no persistence. The
  completed plan `class-hierarchy-graph.md` said "Do not run Leiden on the whole
  project graph"; that decision is superseded here. Per-call cost is one Leiden pass
  over about 7,000 nodes, well under a second in the Rust kernel.
- Surface: fix MCG labels and add `--view=communities` with `--min-size`.
- Labels: dominant directory prefix, fallback highest in-degree file. `community-N`
  ids are removed; the label is the community id.
- Graph: plain import edges only. No call edges, no hub exclusion, no resolution
  knob. `DEFAULT_GAMMA` stays 1.0.
- Memory is out of scope for this epic. Memory uses no community detection today;
  the `memory_crossrefs` graph is the obvious future substrate. That work is carried
  as deferred section D1 so it becomes a tracked task at finalization.

## Constraints
`kind: framing`

- Do not add a second community algorithm. Reuse
  `crates/gcore/src/graph_analytics/leiden.rs` through the `graph_analytics` façade.
- `PreparedGraph::new` dedups edges by `(source, target, kind)` and does not sum
  multiplicity, so edge folding happens in the gcode builder, never by emitting
  duplicate `AnalyticsEdge`s.
- Review guidance in force: graph analytics code surfaces invalid graph input as
  typed errors. Ordinary data conditions (external modules, ambiguous providers,
  self-imports, rows for non-visible files, zero files) are never errors.
- Hand-maintained `.rs` files stay under 1,000 lines. Current production sizes:
  `cli.rs` 798, `graph_analytics.rs` 532, `mod.rs` 403, all others under 350.
- The `gcode` binary is live only after rebuild and install via a new inode
  (`cp` to a dotfile, `mv -f` over the name). Live smoke needs that step.
- No backward compatibility: 0.5.0 is unshipped. The CLI contract version bumps
  8 to 9 because `--view` gains a value, `--min-size` is new, and
  `communities[]` changes shape.
- Consumer sweep evidence: `assign_leiden_communities` is called from
  `crates/gcode/src/commands/graph/view/mcg/fetch.rs::run` and
  `crates/gcode/src/commands/graph/view/mcg/tests.rs::mcg_assigns_leiden_communities_on_scoped_imports`
  (`gcode usages`). `GraphViewSeed::File` is matched in
  `crates/gcode/src/dispatch.rs::run` (`gcode grep -F "GraphViewSeed::File"`).
  `"communities"` appears in `crates/gcode/src/contract/schema.rs` and
  `crates/gcode/contract/gcode.contract.json` (`gcode grep -F '"communities"'`).

## P1: Kernel entry point
`kind: framing`

**Goal**: gcore exposes a communities-only entry with typed input validation.

### 1.1 Add `communities()` and `GraphInputError` to gcore graph analytics [category: code]
`kind: deliverable`

Targets:
- `crates/gcore/src/graph_analytics.rs::*` — scope-reason: add the communities entry point and typed input error beside the existing analyze façade, plus inline tests

`analyze` (`graph_analytics.rs`) runs Tarjan bridges, centrality, god nodes,
unexpected links, and hotspots; MCG uses only `communities`. Add a cheap,
validating entry and leave `analyze` untouched for `graph report`.

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
`PreparedGraph::new(graph).communities().0`. `thiserror` is already a gcore
dependency. Empty graph returns an empty vec; nodes without edges return one
singleton community each, matching `analyze`.

Tests (inline `#[cfg(test)]` in the same file): `communities_matches_analyze_partition_on_seeded_graph`
(same groups as `analyze(&seeded_graph())`), `communities_rejects_unknown_endpoint`,
`communities_rejects_invalid_weight` (NaN and 0.0), `communities_rejects_duplicate_node`,
`communities_rejects_self_loop`, `communities_empty_graph_is_empty`,
`communities_without_edges_is_all_singletons`.

Verify: `cargo fmt -p gobby-core -- --check`, `cargo clippy -p gobby-core --features graph-analytics`,
`cargo nextest run -p gobby-core --features graph-analytics -E 'test(graph_analytics)'`.

**Acceptance:**

- 1.1.1 - `communities(&AnalyticsGraph)` returns the same partition as `analyze` on the seeded graph and skips the other passes. symbol: `communities`. file: `crates/gcore/src/graph_analytics.rs`.
- 1.1.2 - `GraphInputError` is returned for duplicate node, unknown endpoint, invalid weight, and self-loop instead of silent sanitizing. symbol: `GraphInputError`.
- 1.1.3 - Empty and edge-less graphs behave as `analyze` does. test: `crates/gcore/src/graph_analytics.rs::communities_without_edges_is_all_singletons`.

## P2: Project partition
`kind: framing`

**Goal**: a file-level import partition over the whole visible project, and MCG
labeled from it.

### 2.1 Add the project partition builder [category: code] (depends: 1.1)
`kind: deliverable`

Targets:
- `crates/gcode/src/commands/graph/view/partition.rs`
- `crates/gcode/src/commands/graph/view/partition/tests.rs`
- `crates/gcode/src/commands/graph/view/mod.rs::*` — scope-reason: declare the new partition submodule alongside the existing view modules

New file `crates/gcode/src/commands/graph/view/partition.rs` (under 400 lines),
tests in `crates/gcode/src/commands/graph/view/partition/tests.rs` via the `#[path]`
idiom from `crates/AGENTS.md`.
It lives in the view layer because `McgIdentity`
(`crates/gcode/src/commands/graph/view/mcg/identity.rs`) is `pub(crate)` view-layer
state, and `crates/gcode/src/graph/` is the FalkorDB/PostgreSQL projection layer.

```rust
pub(super) struct FileCommunity { pub label: String, pub files: Vec<String> } // files sorted asc
pub(super) struct DirectedImport { pub importer: String, pub provider: String, pub count: usize }

pub(super) struct ProjectPartition {
    communities: Vec<FileCommunity>,      // size desc, then files[0] asc
    index_by_file: HashMap<String, usize>,
    directed: Vec<DirectedImport>,        // folded per ordered (importer, provider)
    file_count: usize,
}

impl ProjectPartition {
    pub(super) fn build(identity: &McgIdentity, rows: &[(String, String)]) -> Result<Self, PartitionError>;
    pub(super) fn label_for_file(&self, file: &str) -> Option<&str>;
    pub(super) fn label_for_module(&self, identity: &McgIdentity, module: &str) -> Option<&str>; // unique_provider then label_for_file
    pub(super) fn community_of_file(&self, file: &str) -> Option<(usize, &FileCommunity)>;
    pub(super) fn communities(&self) -> &[FileCommunity];
    pub(super) fn directed_imports(&self) -> &[DirectedImport];
    pub(super) fn file_count(&self) -> usize;
}

pub(super) enum PartitionError {
    InvalidGraph(gobby_core::graph_analytics::GraphInputError),
    ProviderNotVisible { module: String, file: String },
}
// manual Display + std::error::Error, same pattern as McgSeedError in mcg/identity.rs
```

`build` algorithm:

1. Nodes: `identity.visible_files` sorted into a `Vec<String>` with an index map.
2. Dedup rows into a `BTreeSet<(source, module)>` (mirrors `McgIdentity::from_resolution`).
3. Per `(source, module)`: skip if `source` is not visible; `provider = identity.unique_provider(module)`,
   skip on `None` (external or ambiguous); skip if `provider == source` (self-import);
   `ProviderNotVisible` if the provider is outside `visible_files` (invariant violation,
   since `providers_for` filters by visibility); else `directed[(source, provider)] += 1`.
4. Fold undirected: `undirected[(min, max)] += count`.
5. `AnalyticsGraph`: one `AnalyticsNode { id: path, kind: "file", weight: 1.0 }` per visible
   file (raw path, no `file:` prefix; the graph is homogeneous); one
   `AnalyticsEdge { source: min, target: max, kind: "IMPORTS", weight: count as f64 }`
   per unordered pair. Modularity is scale-invariant, so the fold count is the weight.
6. `gobby_core::graph_analytics::communities(&graph)?`, map node ids back to paths.
7. In-degree per provider = number of distinct importers, from `directed`.
8. Label each community, sort by `(files.len() desc, files[0] asc)`, then enforce label
   uniqueness in that order.

Label rule, `fn derive_label(files: &[String], in_degree: &HashMap<String, usize>) -> String`:

- Size 1: the file path.
- Build `coverage: BTreeMap<String, usize>` over every proper directory ancestor of each
  member (`a/b/c.rs` contributes `a` and `a/b`; root-level files contribute nothing).
- `majority = { p | 2 * coverage[p] >= size }`. If non-empty pick max
  `(depth(p), coverage[p], Reverse(p))`: deepest prefix with at least half the members,
  tie to more coverage, tie to lexicographically smallest.
- Else, among depth-1 prefixes pick max `(coverage, Reverse(p))`.
- Else (all members root-level) pick the member with max `(in_degree, Reverse(path))`.
- Uniqueness: walk communities in final order; on collision append ` #2`, ` #3`, the
  smallest free ordinal. Size-desc ordering means the largest community keeps the bare
  directory label, which is the stability property wanted.

Tests in `crates/gcode/src/commands/graph/view/partition/tests.rs` (duplicate the
ten-line `identity_from` helper from `crates/gcode/src/commands/graph/view/mcg/tests.rs`): `alias_collapse_folds_into_one_edge` (rows `(a,"pkg.mod")` and
`(a,"./mod")` both resolving to `b` give one edge of weight 2), `external_module_adds_no_edge`,
`ambiguous_provider_adds_no_edge`, `self_import_is_dropped`, `non_visible_source_row_is_skipped`,
`provider_outside_visible_set_is_typed_error`, `two_cliques_joined_by_bridge_split_by_directory`,
`label_singleton_is_path`, `label_prefers_deepest_majority_prefix`, `label_falls_back_to_top_level_plurality`,
`label_falls_back_to_in_degree_for_root_files`, `label_collision_gets_ordinal_suffix`,
`partition_is_invariant_to_row_order`, `build_handles_twenty_thousand_rows` (assert completion
and node count only, mirroring `mcg_identity_build_handles_twenty_thousand_rows`).

Verify: `cargo nextest run -p gobby-code -E 'test(partition)'`.

**Acceptance:**

- 2.1.1 - `ProjectPartition::build` produces one node per visible file and one folded undirected edge per importer/provider pair, excluding external, ambiguous, and self imports. symbol: `ProjectPartition::build`. file: `crates/gcode/src/commands/graph/view/partition.rs`.
- 2.1.2 - Labels follow the dominant-directory rule with in-degree fallback and ordinal suffixes on collision. test: `crates/gcode/src/commands/graph/view/partition/tests.rs::label_prefers_deepest_majority_prefix`.
- 2.1.3 - A provider outside the visible set surfaces as `PartitionError::ProviderNotVisible`; kernel input errors surface as `PartitionError::InvalidGraph`. symbol: `PartitionError`.
- 2.1.4 - The partition is deterministic under row reordering. test: `crates/gcode/src/commands/graph/view/partition/tests.rs::partition_is_invariant_to_row_order`.

### 2.2 Label MCG nodes from the project partition [category: code] (depends: 2.1)
`kind: deliverable`

Targets:
- `crates/gcode/src/commands/graph/view/mcg.rs::*` — scope-reason: replace assign_leiden_communities with label_communities and drop the analytics imports
- `crates/gcode/src/commands/graph/view/mcg/fetch.rs::*` — scope-reason: load import rows with the identity, build the partition, and label the walk in run
- `crates/gcode/src/commands/graph/view/render.rs::*` — scope-reason: add the size field to ViewCommunity and keep the payload builder in step
- `crates/gcode/src/commands/graph/view/mcg/tests.rs::*` — scope-reason: rewrite the clustering test against the partition path
- `crates/gcode/src/commands/graph/view/render_tests.rs::*` — scope-reason: construct ViewCommunity with the new size field

In `crates/gcode/src/commands/graph/view/mcg/fetch.rs` rename `load_identity` to `load_project_imports` returning
`ProjectImports { identity: McgIdentity, rows: Vec<(String, String)> }` (same body,
keep the rows instead of dropping them). In `run`, after the availability gate:

```rust
let ProjectImports { identity, rows } = load_project_imports(ctx)?;
let partition = ProjectPartition::build(&identity, &rows)
    .context("partition project import graph")?;
// resolve seed, walk_mcg as today, then:
let (nodes, communities) = label_communities(walk.nodes, &partition, &identity);
```

In `mcg.rs` delete `assign_leiden_communities` and the `analyze`, `AnalyticsGraph`,
`AnalyticsNode`, `AnalyticsEdge`, `weight_for_kind` imports. Add:

```rust
pub(super) fn label_communities(
    nodes: Vec<ViewNodeInput>, partition: &ProjectPartition, identity: &McgIdentity,
) -> (Vec<ViewNodeInput>, Vec<ViewCommunity>)
```

Per node, on `node.key.kind`: `File` uses `partition.label_for_file(&key.identity)`;
`Module` uses `partition.label_for_module(identity, &key.identity)`; `Symbol`,
`External`, `Unresolved` get `None`. `communities[]` holds only communities that
intersect the view's nodes, listing only the view's node ids (canonical, sorted), plus
the project-level `size`. Full membership belongs to `--view=communities`; the payload
invariant that every id in `communities[].nodes` exists in `nodes[]` is what Mermaid
and the existing tests rely on. `walk_mcg` and `close_endpoint` are unchanged.

`ViewCommunity` in `render.rs` becomes `{ id: String, size: usize, nodes: Vec<String> }`
where `id` is the label. `ViewNode.community` stays `Option<String>` and now carries the
label; the Mermaid `Name [label]` rendering needs no change.

Rewrite `mcg_assigns_leiden_communities_on_scoped_imports`: build an identity and
partition from rows, walk a scoped view, call `label_communities`; assert file node
labels come from the partition, a module node with a unique provider inherits the
provider's label, an external module node has `community: None`, `communities[].nodes`
contains only view node ids while `size` is the project-level count, and the label
appears in the Mermaid output.

Verify: `cargo nextest run -p gobby-code -E 'test(mcg) | test(render)'`.

**Acceptance:**

- 2.2.1 - MCG file and uniquely resolved module nodes carry the project-level community label; external and ambiguous modules carry none. symbol: `label_communities`. file: `crates/gcode/src/commands/graph/view/mcg.rs`.
- 2.2.2 - `assign_leiden_communities` and the per-view `analyze` call are removed. file: `crates/gcode/src/commands/graph/view/mcg.rs`.
- 2.2.3 - `ViewCommunity` carries `id` (label), `size`, and view-scoped `nodes`. symbol: `ViewCommunity`. file: `crates/gcode/src/commands/graph/view/render.rs`.
- 2.2.4 - The MCG clustering test exercises the partition path end to end. test: `crates/gcode/src/commands/graph/view/mcg/tests.rs::mcg_labels_nodes_from_project_partition`.

## P3: Communities view
`kind: framing`

**Goal**: `gcode graph view --view=communities [--min-size N]` lists subsystems.

### 3.1 Add the communities view kind, `--min-size`, and contract v9 [category: code]
`kind: deliverable`

Targets:
- `crates/gcode/src/cli.rs::*` — scope-reason: add the communities view kind, make the seed optional, add --min-size, and validate the new combinations
- `crates/gcode/src/dispatch.rs::run`
- `crates/gcode/src/contract.rs::contract`
- `crates/gcode/contract/gcode.contract.json::*` — scope-reason: regenerate the pinned contract snapshot for version 9
- `crates/gcode/tests/contract.rs::*` — scope-reason: pin contract version 9, the new --view value, and --min-size
- `crates/gcode/src/cli/tests/projection.rs::*` — scope-reason: cover communities parsing and rejections
- `docs/contracts/gcode-cli.md`

`cli.rs`:

- `GraphViewKind::Communities` (`#[value(name = "communities")]`); `as_str` returns
  `"communities"`, `default_depth` returns `0`, `allows_row_limits` returns `false`.
- `GraphViewArgs.seed` becomes `Option<GraphViewSeed>`; add `min_size: Option<usize>` and
  `effective_min_size()` defaulting to `2` (singletons hidden by default; `--min-size 1`
  lists everything).
- `GraphViewArgsRaw`: the `seed` ArgGroup becomes `.required(false)`; add `--min-size`
  parsed with the existing `positive_usize`, doc "Minimum community size (communities only; default 2)".
- `from_arg_matches_mut`, in order: row limits with `!allows_row_limits()` reject with
  `ArgumentConflict` naming class-hierarchy and communities; for `Communities`, any seed
  rejects with "--view=communities takes no --file, --module, or --symbol" and an explicit
  `--depth` rejects with "--depth cannot be used with --view=communities"; for other views,
  `--min-size` rejects with "--min-size requires --view=communities", a missing seed keeps the
  existing `MissingRequiredArgument`, and selector compatibility is unchanged.
- Doc strings that enumerate views mention `communities`.

`dispatch.rs::run`: the seed rewrite becomes `if let Some(GraphViewSeed::File(file)) = &mut args.seed`.

`contract.rs::contract`: `--view` value name `fcg|mcg|class-hierarchy|communities`,
`allowed_values` gains `"communities"`, add `FlagContract::value("--min-size", "N")`
after `--outgoing-limit`, summary "Render a scoped fcg, mcg, or class-hierarchy graph
view, or project-wide import communities." Bump `contract_version` 8 to 9 in `contract.rs`,
the pinned JSON, `crates/gcode/tests/contract.rs`, and the version line of
`docs/contracts/gcode-cli.md` with a short "Version 9" paragraph (new view, new flag,
content-derived `communities[].id`, new `size` field, `community:<label>` node ids).
Regenerate the pinned JSON from `gcode contract --format json`. `json_output_keys` is unchanged.

`crates/gcode/src/cli/tests/projection.rs`: `--view communities` parses with no seed and with `--min-size 5`;
rejects `--file`, `--symbol`, `--depth`, `--incoming-limit`; `--min-size` with `mcg` rejects;
`--min-size 0` rejects; defaults `effective_min_size() == 2` and `effective_depth() == 0`;
existing mcg/fcg/class-hierarchy cases still require a seed.

Python needs no change: `src/gobby/hooks/code_navigation.py` classifies on the
`graph view` token pair and `src/gobby/ai/_tool_chat_tools.py` authorizes on the
`view` subcommand; neither inspects `--view` or seed flags.

Verify: `cargo nextest run -p gobby-code -E 'test(graph_view)'`, `cargo nextest run -p gobby-code --test contract`.

**Acceptance:**

- 3.1.1 - `--view=communities` parses without a seed, accepts `--min-size`, and rejects seeds, `--depth`, and row limits with typed clap conflicts. symbol: `GraphViewArgs::from_arg_matches_mut`. file: `crates/gcode/src/cli.rs`.
- 3.1.2 - The contract advertises `communities` and `--min-size` at version 9 and the pinned JSON matches. file: `crates/gcode/contract/gcode.contract.json`.
- 3.1.3 - Projection tests cover every new accept and reject case. test: `crates/gcode/src/cli/tests/projection.rs::graph_view_communities_rejects_seed_and_depth`.
- 3.1.4 - The contract doc records version 9 and the new surface. behavior: "Version 9" in `docs/contracts/gcode-cli.md`.

### 3.2 Render the communities view [category: code] (depends: 2.2, 3.1)
`kind: deliverable`

Targets:
- `crates/gcode/src/commands/graph/view/communities.rs`
- `crates/gcode/src/commands/graph/view/communities/tests.rs`
- `crates/gcode/src/commands/graph/view/render.rs::*` — scope-reason: add the community node kind, its key constructor, and its file-nullability arm
- `crates/gcode/src/commands/graph/view/render_tests.rs::*` — scope-reason: cover the communities view kind and community node nullability
- `crates/gcode/src/commands/graph/view/mod.rs::*` — scope-reason: declare the communities submodule and route the new view kind in run

New `crates/gcode/src/commands/graph/view/communities.rs` (under 200 lines) with
`pub(crate) fn run(ctx: &Context, args: &GraphViewArgs, format: Format) -> anyhow::Result<()>`,
routed from `run` in `crates/gcode/src/commands/graph/view/mod.rs`. This view reads PostgreSQL only
(`load_project_imports`), so it skips `CodewikiFacts` and the graph-availability hint.

Payload reuses `build_view_payload` unchanged:

- `seed = ViewSeed { id: ctx.project_id, name: ctx.project_root.display(), kind: "project", file: None }`,
  `depth = 0`, both truncation flags false, `hint = None`.
- Listed communities = `partition.communities()` with `files.len() >= effective_min_size()`.
- `nodes[]`: one node per listed community. Add `NodeKind::Community` with key prefix
  `"community"`, `NodeKey::community(label)`, and a `node_file_for_kind` arm returning
  `None`. `ViewNodeInput { name: label, kind: "community", file: None, community: None }`
  so Mermaid prints the label once.
- `edges[]`: for each `DirectedImport`, map importer and provider to their community;
  keep when both are listed and distinct; dedup;
  `ViewEdgeInput { source: community(importer label), target: community(provider label), rel: "IMPORTS" }`.
- `communities[]`: listed communities as `{ id: label, size, nodes: files as file: canonical ids }`,
  full membership.
- Mermaid: the community-level graph through the existing `render_mermaid` (one node per
  community, `IMPORTS` edges). No file-level diagram: a 7,000-node Mermaid is unreadable
  and the payload carries no file-level edges. Text format keeps `print_view` as is.

Tests in `crates/gcode/src/commands/graph/view/communities/tests.rs`: min-size filtering hides singletons by default and
`--min-size 1` shows them; inter-community edges are directed, deduped, and only between
listed communities; `communities[].nodes` are `file:`-prefixed canonical ids; the Mermaid
block validates. `render_tests.rs`: add the `Communities` arm to the view-kind loop and a
`NodeKind::Community` case to the file-nullability test.

Verify: `cargo nextest run -p gobby-code`, `cargo clippy -p gobby-code`, `cargo fmt -p gobby-code -- --check`.

**Acceptance:**

- 3.2.1 - `gcode graph view --view=communities` emits one node per listed community, directed inter-community `IMPORTS` edges, full membership in `communities[]`, and valid Mermaid. symbol: `run`. file: `crates/gcode/src/commands/graph/view/communities.rs`.
- 3.2.2 - `--min-size` filters listed communities with default 2. test: `crates/gcode/src/commands/graph/view/communities/tests.rs::min_size_hides_singletons_by_default`.
- 3.2.3 - `NodeKind::Community` exists with a null file and `community:` key prefix. symbol: `NodeKind`. file: `crates/gcode/src/commands/graph/view/render.rs`.

## P4: Documentation
`kind: framing`

**Goal**: skills, contract doc, and changelog describe the new behavior.

### 4.1 Update skill text, contract doc, changelog, and bundled manifest [category: docs] (depends: 3.1, 3.2)
`kind: deliverable`

Targets:
- `crates/gcode/assets/SKILL.md`
- `src/gobby/install/shared/skills/code-index/SKILL.md`
- `src/gobby/install/bundled_content_manifest.json::*` — scope-reason: regenerate deterministic checksums for the changed bundled code-index skill
- `docs/contracts/gcode-cli.md`
- `crates/CHANGELOG.md`

In both `SKILL.md` files replace "MCG communities are Leiden via `analyze`" with:
MCG community labels come from a project-level Leiden partition of the file-to-file
import graph, computed per call from active import rows, with content-derived labels.
Add the graph-view bullet `gcode graph view --view=communities [--min-size N]` (seedless,
lists subsystems with files and inter-community imports) and a "When to use which"
row "Which files form a subsystem, and how subsystems depend on each other".

`docs/contracts/gcode-cli.md`: describe the new view, `--min-size`, the seedless
invocation, and `communities[].{id,size,nodes}` semantics for both views.

`crates/CHANGELOG.md` under Unreleased: Added, gcode `--view=communities` and `--min-size`;
Added, gobby-core `graph_analytics::communities` and `GraphInputError`; Changed, gcode
MCG community labels are project-level and content-derived, `community-N` ids removed,
contract version 9.

Regenerate the bundled manifest: `uv run python -m gobby.install.manifest --write`.

Verify: `DATABASE_URL="${DATABASE_URL:-postgresql://gobby_test:gobby_test@127.0.0.1:60892/gobby_test}" GOBBY_TEST_PROTECT=1 uv run pytest tests/install/test_bundled_content_manifest.py tests/skills -q`.

**Acceptance:**

- 4.1.1 - Both skill files document the project-level partition and the communities view. behavior: "--view=communities" in `src/gobby/install/shared/skills/code-index/SKILL.md`.
- 4.1.2 - The bundled content manifest checksum for the skill is regenerated. file: `src/gobby/install/bundled_content_manifest.json`.
- 4.1.3 - The changelog records the added and changed surfaces. behavior: "communities" in `crates/CHANGELOG.md`.

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

## Q1 Verification
`kind: verification`

1. Crate gates: `cargo fmt --all -- --check`, `cargo clippy -p gobby-core -p gobby-code`,
   `cargo nextest run -p gobby-core --features graph-analytics`, `cargo nextest run -p gobby-code`.
2. Contract: `cargo nextest run -p gobby-code --test contract` passes against the regenerated pinned JSON.
3. Install via new inode: `cargo build --release -p gobby-code && cp target/release/gcode ~/.gobby/bin/.gcode.next && mv -f ~/.gobby/bin/.gcode.next ~/.gobby/bin/gcode`.
4. Live smoke on this repo:
   - `gcode graph view --view=communities --min-size 5 --format json`: labels resemble
     `src/gobby/memory`, `crates/gcode/src/...`, `web/src/...`; sizes descend; `edges[]` join
     `community:` ids; the Mermaid block validates.
   - `gcode graph view --view=mcg --file crates/gcode/src/commands/graph/view/mcg.rs`:
     `mcg.rs`, `mcg/fetch.rs`, `mcg/identity.rs`, `mod.rs`, and `render.rs` share one label;
     `module:` nodes with a unique provider carry the provider's label; external modules
     carry `null`.
   - Sanity baseline before the change: the same MCG call today yields one community per node.
5. Python: `DATABASE_URL=... GOBBY_TEST_PROTECT=1 uv run pytest tests/install/test_bundled_content_manifest.py tests/hooks/test_code_navigation.py -q`.
6. Daemon coordination: the daemon shells out to `~/.gobby/bin/gcode`; the binary swap needs
   no restart. Session #12697 has asked for no new validation or spawn while it diagnoses a
   restart; run the live smoke after that hold clears.

## V1 Plan Changelog
`kind: framing`

- Draft 1 (2026-09-10): initial narrative from exploration and the confirmed decision record.
