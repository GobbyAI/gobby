# Wiki/CodeWiki Restructure: gwiki Ownership, Daemon Orchestration, Aggregated Page Model

**Plan ID:** wiki-codewiki-restructure

## Overview
`kind: framing`

Move codewiki out of the gcode crate and rebuild it as a daemon-orchestrated,
template-driven pipeline owned by gwiki, with an Understand-Anything-inspired
aggregated page model (~50–90 pages replacing ~3,300 per-file/module pages).
gcode narrows to code facts (index/graph/symbols/search plus a new `gcode facts`
command). Standalone (no-daemon) mode is deleted from all crates. The old
`gcode codewiki` path keeps running untouched as the fallback until cutover,
then is deleted in place — never ported. Net repo effect ≈ −25–30K LOC.

## Constraints
`kind: framing`

- No backward compatibility: 0.5.0 is unshipped. Old flags, contracts, and modes
  are removed, not deprecated.
- The wiki must stay queryable (`wiki_search`/`wiki_ask`/`wiki_read`) at every
  phase boundary. Old vault *content* persists until the P5 cutover acceptance
  passes; the old `gcode codewiki` *generator* does not. It has no users and no
  rollback value — the vault is gitignored, and the pages it wrote are the ones
  this epic replaces — so 3.8 retires the command once 2.2, 3.1, and 3.2 have
  finished consuming it as source and reference. Queryability is a property of
  the pages on disk, not of the generator that wrote them.
- gwiki must NOT gain a lib dependency on gobby-code. The only cross-crate data
  path for code facts is the FactsBundle JSON schema in gcore, consumed
  identically by gwiki renderers and by wiki-writer agents.
- Two-phase generation contract: deterministic facts first (`gcode facts`),
  agent prose second against a per-page-type template. Agents never re-derive
  structure; read-only gcode tools are allowed only to pull short excerpts for
  citations.
- Fan-out is daemon-side (`dispatch_batch`/`spawn_agent_impl`): `max_agent_depth=1`
  forbids agent-spawned grandchildren; writer batch ≤5 under
  `max_active_agents=10`; page work items are hub-DB queue rows, not tasks.
  The task system is used only for wiki-librarian gap fixes and escalations.
- Rebuild-and-reinstall rule: a committed crate change is not live until
  `~/.gobby/bin/{gcode,gwiki}` is reinstalled. Every phase touching crates ends
  with reinstall.
- Legacy-subtree rule: after P1's daemon-only cleanup (1.2, 1.5), the
  `crates/gcode/src/commands/codewiki/` subtree is source and reference material.
  2.2 copy-refactors from it; 3.1 and 3.2 reimplement against the gcore golden
  fixture; no new module imports its internals. It is deliberately *not* frozen —
  3.4 edits the four sites pinning the marker string, 3.1 relocates the feature
  catalog out of it, and 3.8 removes the command that reaches it. A freeze buys
  exactly one thing, a runnable fallback, and there is none worth protecting.
  3.8 retires the command; 5.2 deletes the subtree wholesale.
- Dependency-graph invariant: the declared `depends:` edges form a DAG with no
  deliverable scheduled before the runtime inputs it consumes. Asserted by
  `uv run gobby plans validate --mode expansion` (edge resolution + acyclicity)
  and mirrored one-to-one by the epic task graph at `gobby build` handoff.
- Non-goals (follow-up work, filed as tasks at epic creation, not in this epic):
  semantic verify-agent stage with per-page confidence badges (successor to
  `text/verify.rs`), additional tour audiences, PR blast-radius surfacing that
  auto-enqueues PARTIAL runs, optional `parsers` feature-gating in gobby-code.
- Task consolidation executed alongside this epic: #18871 close-superseded
  (its quality bars land in 3.3/4.4), #18779 close (baselines mooted;
  wiki stays gitignored), #18790 close-obsolete, #18786 reparent into P2,
  #18905/#18906/#18907 stay open as P4 executor acceptance cargo.

## P1: Standalone Mode Removal
`kind: framing`

**Goal**: All crates require the gobby daemon; every standalone code path, config
layer, flag, and test is deleted.

### 1.1 Remove RuntimeMode::Standalone from gcore [category: code]
`kind: deliverable`

Target: `crates/gcore/src/runtime_mode.rs`, `crates/gcode/src/db/resolution.rs`,
`crates/gcore/tests/runtime_mode_process.rs`,
`crates/gcore/tests/effective_config_process.rs`,
`crates/gcore/src/ai/effective_config.rs`, `crates/gcode/src/config/layers.rs`,
`crates/gcode/src/schema.rs`

Delete the `Standalone` variant of `RuntimeMode` and the standalone-default
fallback in `select_runtime_mode_with_probe` (precedence today: env override →
`GOBBY_DAEMON_URL` → OS service-registration file → Standalone default). Daemon
absence becomes a hard, actionable error ("gobby daemon required — run
`gobby start`"), not a mode. `parse_requested_mode` no longer accepts
`"standalone"`. Remove `crates/gcore/tests/runtime_mode_process.rs` standalone
cases and update `effective_config_process.rs`. Collapse call sites that match
on the enum (e.g. `crates/gcore/src/ai/effective_config.rs:183-252`,
`crates/gcode/src/db/resolution.rs:22-126`, `crates/gcode/src/config/layers.rs`)
to the daemon arm; error strings that today hint `gcode setup --standalone`
point at daemon startup instead (`crates/gcode/src/schema.rs` MIGRATION_HINT
test included).

**Acceptance:**

- 1.1.1 - `RuntimeMode` has no `Standalone` variant and no standalone default fallback. file: `crates/gcore/src/runtime_mode.rs`.
- 1.1.2 - DB resolution has a single daemon path and its standalone branch tests are removed. file: `crates/gcode/src/db/resolution.rs`.
- 1.1.3 - Daemon-absent startup fails with actionable guidance. behavior: "daemon required error" in `crates/gcore/src/runtime_mode.rs`.

### 1.2 Delete AiRouting::Direct and auto-fallback [category: code] (depends: 1.1)
`kind: deliverable`

Target: `crates/gcore/src/ai/mod.rs`,
`crates/gcore/src/ai/generation/one_shot.rs`,
`crates/gcode/src/commands/codewiki/text/generation/routing.rs`,
`crates/gcode/src/commands/codewiki/text/generation/tool_loop.rs`,
`crates/gcode/src/commands/codewiki/text/generation/one_shot.rs`,
`crates/gcode/src/commands/codewiki/frontmatter.rs`,
`crates/gwiki/src/commands/generation_routes.rs`,
`crates/gwiki/src/ai/clients.rs`, `crates/gwiki/src/commands/ask/synthesis.rs`,
`crates/gwiki/src/commands/ask/deep.rs`,
`crates/gwiki/src/ingest/session/summarize.rs`, `crates/gcode/src/cli.rs`,
`crates/gcore/src/ai_context.rs`, `crates/gcore/src/config/types.rs`

Remove `AiRouting::Direct` and the `Auto` probe-then-fallback logic at
`ai/mod.rs:68-110` (`AiNoticeKind::AutoFallbackToDirect` included); routing
becomes `daemon | off`. Ripple: `crates/gcore/src/ai/generation/{mod,profile}.rs`
(standalone profile docs, plaintext api_key acceptance), `ai/effective_config/`,
`ai_context.rs` standalone layer (`standalone: Option<StandaloneConfig>` field),
`config/types.rs:427` Direct-mode feature tiers. In gcode's codewiki (old path,
still running): `text/generation/routing.rs` Direct arms and
`direct_route_candidate_error`, `text/generation/tool_loop.rs::run_direct_tool_loop`
become dead — delete them and collapse `--ai auto|daemon|direct|off` to
`daemon|off` in `crates/gcode/src/cli.rs` codewiki args. Same collapse for gwiki
`--ai` routes (`ask/compile/librarian/upkeep/recap`) in
`crates/gwiki/src/commands/generation_routes.rs` (`daemon_available` Direct arm).

The consumer set is wider than the routing modules and was swept before this
plan was written. Beyond `ai/mod.rs`, the live match arms that must collapse are
`crates/gcore/src/ai/generation/one_shot.rs` (:95 `AiRouting::Direct => direct()`,
:134, :247) and, in gwiki, `ai/clients.rs` (:29,:35
`Option<DirectGenerationTarget>` fields), `commands/ask/synthesis.rs:89`,
`commands/ask/deep.rs:130`, and `ingest/session/summarize.rs` (:39 target field,
:74/:77 `matches!(route, AiRouting::Direct)`). In the legacy codewiki subtree —
edited here because P1 runs before the freeze begins —
`text/generation/one_shot.rs` (:61,:94,:188,:210) and `frontmatter.rs:195` also
match on the variant.

`gwiki sync-sessions --summarize` is the one behavior change hiding in this
deletion rather than a mechanical collapse. It generates session summaries with
no hub, filling hub-only prompt variables with `STANDALONE_SENTINEL` and
stamping `summary_mode: standalone`. Requirement 2 makes the daemon mandatory,
so this becomes daemon-routed like every other generation path: the sentinel
context builder, `assemble_standalone_wiki_md`, and the `summary_mode:
standalone` frontmatter value are deleted with the Direct arm, not preserved.

No absence-test matrix is specified for this deletion. Removing a Rust enum
variant makes the compiler the exhaustiveness proof — every unhandled match arm
and every stale field is a build error, which is strictly stronger than a test
asserting a string no longer appears. `cargo build -p gobby-{core,code,wiki}`
succeeding *is* the sweep's acceptance.

**Acceptance:**

- 1.2.1 - `AiRouting` has no `Direct` variant; probe fallback resolves to `Off` only. file: `crates/gcore/src/ai/mod.rs`.
- 1.2.2 - gcode codewiki accepts only `--ai daemon|off`; direct lanes deleted. file: `crates/gcode/src/commands/codewiki/text/generation/routing.rs`.
- 1.2.3 - gwiki generation routes have no Direct branch. file: `crates/gwiki/src/commands/generation_routes.rs`.
- 1.2.4 - All three crates build with no `Direct`/`DirectGenerationTarget` residue, including the gwiki ask and session-summary paths. file: `crates/gwiki/src/ingest/session/summarize.rs`.
- 1.2.5 - `sync-sessions --summarize` routes through the daemon and emits no standalone sentinel or `summary_mode: standalone` frontmatter. test: `crates/gwiki/src/ingest/session/summarize.rs`.

### 1.3 Delete standalone provisioning and the gcore.yaml layer [category: code] (depends: 1.1)
`kind: deliverable`

Target: `crates/gcore/src/provisioning/docker.rs`,
`crates/gcore/src/provisioning/bootstrap.rs`,
`crates/gcore/src/provisioning/hub.rs`,
`crates/gcore/src/config/daemon_source.rs`, `crates/gcore/src/config/mod.rs`,
`crates/gcode/src/config/layers.rs`, `crates/gcode/src/config/services.rs`,
`crates/gwiki/src/support/config.rs`, `crates/gcore/src/setup.rs`

Delete the standalone Docker-compose service stack
(`crates/gcore/src/provisioning/docker.rs`, standalone branches of
`crates/gcore/src/provisioning/bootstrap.rs` including
`write_standalone_bootstrap`, `StandaloneConfig`) while keeping hub
provisioning used by the daemon. Remove the user-home gcore config layer
(`GCORE_CONFIG_FILENAME`/`gcore_config_path`) everywhere `ConfigLayers`/`AiContext` builds:
`crates/gcode/src/config/{layers,services}.rs`
(`FallbackConfigSource.standalone`, `read_standalone_config[_optional]`,
`StandaloneConfigReadError`), `crates/gcore/src/setup.rs` `StandaloneSetup`
trait and "setup refused in attached mode" flows.

`StandaloneConfig` is not a standalone-only type today, which is why deleting it
reaches further than the provisioning module. Daemon mode still reads a
routing-only slice of `~/.gobby/gcore.yaml` through
`routing_overrides_only` (`crates/gcore/src/config/daemon_source.rs:53`,
re-exported from `config/mod.rs:14`), consumed by
`crates/gcore/src/ai/effective_config.rs:85` and
`crates/gwiki/src/support/config.rs:227`, and `provisioning/hub.rs:399` reads
the file directly. All of that goes: after this section the daemon is the only
config source, and routing overrides are served by it rather than read from a
user-home YAML file.

**Acceptance:**

- 1.3.1 - No `StandaloneConfig` type or gcore.yaml read path remains in gcore. file: `crates/gcore/src/provisioning/bootstrap.rs`.
- 1.3.2 - gcode config layering has a single daemon-served source. file: `crates/gcode/src/config/layers.rs`.
- 1.3.3 - No crate reads `gcore_config_path`; routing overrides resolve from daemon-served config only. file: `crates/gcore/src/config/daemon_source.rs`.

### 1.4 Remove setup --standalone from both CLIs and their contracts [category: code] (depends: 1.2, 1.3)
`kind: deliverable`

Target: `crates/gcode/src/commands/setup.rs`, `crates/gcode/src/contract.rs`,
`crates/gcode/contract/gcode.contract.json`, `crates/gcode/src/cli.rs`,
`crates/gcode/src/dispatch.rs`, `crates/gwiki/src/commands/setup.rs`,
`crates/gwiki/src/api.rs`, `crates/gwiki/src/cli.rs`,
`crates/gwiki/src/cli/mapping.rs`, `crates/gwiki/src/commands/search.rs`,
`tests/test_cli_contracts.py`, `tests/code_index/test_gcode_phase7_contract.py`

gcode: delete `run_standalone_setup`/`validate_standalone_request`
(`commands/setup.rs`, `setup/postgres.rs`), the `Setup { standalone }` CLI flag
(`cli.rs:146-150`), dispatch threading (`dispatch.rs:183,200`), and the pinned
required `--standalone` flag in `crates/gcode/src/contract.rs` +
`crates/gcode/contract/gcode.contract.json`. gwiki: delete the setup standalone
branch (`crates/gwiki/src/commands/setup.rs:40-400` standalone paths,
`SetupOptions.standalone` in `api.rs:236`, `cli.rs:263-267`, `cli/mapping.rs:271`)
and the "run `gwiki setup --standalone`" hint in `commands/search.rs:200`.
Full contract churn checklist both binaries: crate contract module, contract
JSON, vendored `tests/contracts/*.json`, `tests/test_cli_contracts.py`,
`tests/code_index/test_gcode_phase7_contract.py`, `docs/contracts/*`. Delete
standalone integration tests (`crates/gcode/tests/graph_standalone*`,
`projection_standalone.rs`, setup standalone tests — ~2,700 LOC).

**Acceptance:**

- 1.4.1 - `--standalone` absent from gcode setup command and pinned contract. file: `crates/gcode/contract/gcode.contract.json`.
- 1.4.2 - gwiki setup has no standalone branch. file: `crates/gwiki/src/commands/setup.rs`.
- 1.4.3 - Standalone integration test files are deleted and contract drift tests pass. test: `tests/test_cli_contracts.py`.

### 1.5 Remove the Standalone runtime node from generated architecture content [category: code] (depends: 1.1, 1.2, 1.3, 1.4, 1.6)
`kind: deliverable`

Target: `crates/gcode/src/commands/codewiki/system_model.rs`,
`crates/gcode/src/commands/codewiki/architecture_diagrams.rs`

These two modules are upstream of the new pipeline, which is the only reason this
deliverable survives the generator's retirement: 2.2 copy-refactors
`system_model.rs` into a facts producer, and 3.2 reimplements diagram evidence
against `architecture_diagrams.rs` in its post-1.5 form. A `RuntimeMode::Standalone`
left in either one propagates into `gcode facts` and from there into every new
page. Remove `RuntimeMode::{Standalone,DaemonAttached}` from `system_model.rs`
(:114,274,630,790) and the standalone-vs-daemon branch in
`architecture_diagrams.rs:149-161`, updating the
`tests/{architecture,infrastructure}.rs` fixtures that assert on them.

Two things an earlier draft carried here are dropped along with the fallback. The
`ARCHITECTURE_NARRATIVE_SYSTEM` prompt edit is pointless because 4.4's templates
replace the legacy prompts wholesale and nothing regenerates from them in
between. The `RENDER_VERSION_ARCHITECTURE` and `RENDER_VERSION_INFRASTRUCTURE`
bumps existed to force regeneration "on the next old-path run" — after 3.8 there
is no old-path run. Neither module is in this deliverable's Target as a result.

This deliverable also carries the P1 exit gate, which is why it depends on every
other P1 sibling rather than only on 1.1. A gate that can run before its phase
finishes is not a gate — it would certify a build that still contains the
standalone CLI surface, provisioning, and docs.

**Acceptance:**

- 1.5.1 - No Standalone references remain in the codewiki system model or diagram evidence. file: `crates/gcode/src/commands/codewiki/architecture_diagrams.rs`.
- 1.5.2 - `system_model.rs` and `architecture_diagrams.rs` carry no runtime-mode variant or branch, and their architecture/infrastructure fixtures assert the post-removal shape. test: `crates/gcode/src/commands/codewiki/tests/architecture.rs`.
- 1.5.3 - P1 exit gate: all three crates build, `~/.gobby/bin/{gcode,gwiki,ghook}` are rebuilt and reinstalled, and the daemon starts and answers `wiki_search`, `wiki_ask`, and `wiki_read` with the new binaries. test: `tests/wiki/test_phase_exit_smoke.py`.

### 1.6 Purge standalone from docs and instructions [category: docs] (depends: 1.4)
`kind: deliverable`

Target: `crates/gcode/README.md`

Remove the "Daemon-Independent Runtime" section and standalone claims from
`crates/gcode/README.md` (:166,212-217), `crates/gwiki/README.md:7`,
`crates/CLAUDE.md`, `AGENTS.md`, and any `docs/contracts/*` standalone
references surfaced by the 1.4 contract churn. State the daemon-required
invariant once in `crates/CLAUDE.md`.

**Acceptance:**

- 1.6.1 - No standalone-mode instructions remain in crate docs. file: `crates/gcode/README.md`.

## P2: Facts Foundation
`kind: framing`

**Goal**: gcode produces a versioned, deterministic FactsBundle (schema in
gcore) that powers both gwiki renderers and wiki-writer agents; per-file
one-sentence coverage lives in the hub DB; the #18786 scoped-edge starvation
bug is fixed at the source.

### 2.1 FactsBundle schema in gcore [category: code]
`kind: deliverable`

Target: `crates/gcore/src/code_facts.rs`,
`crates/gcore/tests/fixtures/facts_bundle_v1.json`

New module defining the serde schema, following the `codewiki_contract.rs`
precedent (producer/consumer contract with golden fixtures):

```rust
pub struct FactsBundle { pub version: u32, pub scope: FactsScope, pub repo: Option<RepoFacts>,
    pub modules: Vec<ModuleFacts>, pub files: Vec<FileFacts>, pub layers: Option<LayersProposal> }
pub struct FileFacts { pub path: String, pub summary: Option<String>,
    pub symbols: Vec<SymbolFact>, pub imports: Vec<String>, pub exports: Vec<String>,
    pub content_hash: String, pub structural_hash: String }
pub struct SymbolFact { pub uuid: String, pub name: String, pub kind: String,
    pub signature: Option<String>, pub line_start: u32, pub line_end: u32, pub doc: Option<String> }
pub struct ModuleFacts { pub id: String, pub name: String, pub files: Vec<String>,
    pub edges: Vec<ModuleEdge>, pub neighbor_exports: Vec<NeighborExport>, pub git_stats: GitStats }
pub struct RepoFacts { pub stats: RepoStats, pub crates: Vec<CrateFact>,
    pub service_boundaries: Vec<ServiceFact>, pub diagram_evidence: DiagramEvidence }
```

Include an edge-truncation marker (`truncated: bool`, `edge_limit`) on
ModuleFacts. `LayersProposal` = clusters + suggested layer grouping.
Untrusted-content note: README/docstring excerpts carried in the bundle are
plain data; fencing happens at prompt-assembly time (4.4).

`FileFacts.summary` is `Option` solely to express an explicit incomplete state.
A bundle emitted for a run that will claim `fresh`, or for any FULL run, must
carry `Some` for every file; 2.4/2.5 enforce that and 5.1 gates on it. `None`
means "summary missing or failed", never "file has no summary worth writing".

Canonical serialization is part of the contract, because unstable bytes force
needless regeneration: fixed key order, and every collection explicitly sorted
(files by path, symbols by `(line_start, name)`, modules by id, edges by
`(from, to, kind)`, imports/exports lexicographic). The same index must always
produce identical bytes.

One golden fixture is checked in at
`crates/gcore/tests/fixtures/facts_bundle_v1.json` and is the shared contract
artifact: gcore round-trips it, gcode asserts its producer emits exactly it for
the fixture index (2.2), and gwiki renders from it (3.1). `FACTS_VERSION` is
bumpable; consumers reject an unsupported version with a typed error rather than
rendering a partial page.

**Acceptance:**

- 2.1.1 - FactsBundle schema exists with version field and golden round-trip fixtures. file: `crates/gcore/src/code_facts.rs`.
- 2.1.2 - Golden fixture parses byte-stably in both directions. test: `crates/gcore/src/code_facts.rs::tests`.
- 2.1.3 - Canonical serialization is byte-identical under shuffled input ordering. test: `crates/gcore/src/code_facts.rs::tests`.
- 2.1.4 - An unsupported `FACTS_VERSION` yields a typed rejection, never a partial parse. symbol: `gobby_core::code_facts::FactsVersionError`.

### 2.2 gcode facts command [category: code] (depends: 2.1, 2.4)
`kind: deliverable`

Target: `crates/gcode/src/commands/facts/mod.rs`,
`crates/gcode/src/commands/facts/cluster.rs`,
`crates/gcode/contract/gcode.contract.json`, `crates/gcode/src/models.rs`,
`crates/gcode/src/cli.rs`, `crates/gcode/src/dispatch.rs`,
`crates/gcode/src/commands/symbols.rs`

New `gcode facts --module <id> | --repo | --layers [--out <path>]` emitting
FactsBundle JSON. This is a **new implementation against the gcore fixture**,
not a port: per the legacy-freeze boundary, the corresponding legacy files are
read as reference and neither imported nor edited. Reference map — legacy
`commands/codewiki/`: `snapshot.rs` (index snapshot), `system_model.rs`
(workspace/service model, post-1.5 form), `cluster.rs` (file→module Louvain
clustering with size caps ≈35, merge target ≈25), `relationship_facts.rs`
(caller/callee/import facts), `graph.rs` (edge fetch — superseded by 2.3).
Populate `FileFacts.summary` from hub-DB summaries (2.4) and `SymbolFact` rows
from indexed symbols (`crates/gcode/src/models.rs` Symbol, including `summary`
:149 and signatures). Neighbor exports: for each module edge endpoint outside
the module, include its exported symbol names (UA neighborMap). Contract entry
for the new command (full churn checklist as in 1.4). CLI parse + JSON-shape
tests.

A contract pin is not a command. Adding `gcode facts` means editing every live
wiring surface in the binary, not just the contract JSON: the `Commands` enum and
its argument struct in `crates/gcode/src/cli.rs`, and the exhaustive dispatch
match in `crates/gcode/src/dispatch.rs`. This deliverable owns both, plus the
Rust half of 2.4's per-file summaries: `crates/gcode/src/commands/symbols.rs`
(`outline` at :25) renders the summary column, and it reads the same hub-DB rows
2.4 writes. That data dependency is why this section depends on 2.4 — the
FactsBundle cannot populate `FileFacts.summary` from storage that does not exist
yet.

Determinism is a build requirement, not a nicety: DB row order, vector order,
and Louvain iteration order are all unstable inputs. `crates/gcode/src/commands/facts/cluster.rs`
runs with a fixed clustering seed and sorts its input before clustering; module
ids are content-addressed from the sorted member path list rather than derived
from iteration order, so an unrelated file edit cannot renumber every module and
escalate the next run to FULL.

**Acceptance:**

- 2.2.1 - `gcode facts --module` emits a valid FactsBundle for a real module. symbol: `gobby_code::commands::facts`.
- 2.2.2 - `--repo` and `--layers` modes emit RepoFacts and LayersProposal. file: `crates/gcode/src/commands/facts/mod.rs`.
- 2.2.3 - Contract pins the new command and drift tests pass. file: `crates/gcode/contract/gcode.contract.json`.
- 2.2.4 - Repeated runs over one fixture index, including shuffled source-row order, emit identical canonical bytes and identical module ids. test: `crates/gcode/src/commands/facts/tests.rs::deterministic_bytes`.
- 2.2.5 - The producer emits exactly the checked-in gcore golden fixture for the fixture index. test: `crates/gcode/src/commands/facts/tests.rs::matches_gcore_golden`.
- 2.2.6 - `gcode facts` parses, dispatches, and returns a nonzero exit with a machine-readable error for an unknown module. file: `crates/gcode/src/dispatch.rs`.
- 2.2.7 - `gcode outline` surfaces the 2.4 file summary. symbol: `gobby_code::commands::symbols::outline`.

### 2.3 Scoped server-side graph edges with recorded truncation [category: code] (depends: 2.2, 2.5)
`kind: deliverable`

Target: `crates/gcode/src/commands/facts/graph.rs`

Fix #18786 at the source: replace the project-wide bounded FalkorDB sample +
client-side filter (old `fetch_codewiki_graph_edges`,
`codewiki/graph.rs:5-141`) with queries that filter by the requested file/module
set server-side (WHERE on file paths / core symbol ids in
`codewiki_call_edges_query` / `codewiki_import_edges_query` equivalents), so a
small scope gets its real edges instead of zero survivors. When `edge_limit`
truncates, record it (`truncated`, `edge_limit`) in ModuleFacts, following the
pattern of `crates/gwiki/src/falkor_graph/code_edges.rs:239-266`
(`truncate_to_limit`/`record_code_edge_truncation`). Reparent task #18786 under
this epic; its validation criteria are covered by these acceptance items.

The P2 exit gate lives here, so this section depends on 2.5 as well — the same
terminal-gate rule as 1.5. Freshness surfacing is part of what P2 has to hand to
P4, so a gate that can precede it proves nothing.

**Acceptance:**

- 2.3.1 - Scoped facts for `crates/gcode/src/search` include at least the search→search/fts dependency edge. test: `crates/gcode/src/commands/facts/tests.rs::scoped_edges_nonzero`.
- 2.3.2 - Truncation is recorded, never silent. behavior: "edge truncation surfaced in FactsBundle" in `crates/gcore/src/code_facts.rs`.
- 2.3.3 - P2 exit gate: gcode is rebuilt and reinstalled, `gcode facts --repo` runs against the live daemon from `~/.gobby/bin/gcode`, and `wiki_search`, `wiki_ask`, and `wiki_read` all still answer. test: `tests/wiki/test_phase_exit_smoke.py`.

### 2.4 Per-file one-sentence summaries in the hub DB [category: code]
`kind: deliverable`

Target: `src/gobby/code_index/summarizer.py`,
`src/gobby/code_index/_storage/summaries.py`,
`src/gobby/code_index/maintenance.py`,
`src/gobby/runner_lifecycle_subsystems.py`,
`src/gobby/storage/migrations/`,
`src/gobby/storage/postgres_baseline_schema.sql`

Extend the summarizer subsystem (`SymbolSummarizer` at `summarizer.py:40`,
storage mixin `src/gobby/code_index/_storage/summaries.py`) to guarantee a
one-sentence summary per indexed source file, keyed
`(project_id, path, content_hash)` so refresh invalidates stale rows. The project
component is load-bearing rather than decorative: `code_indexed_files` is already
project-scoped, and a hub serving two repositories that both contain
`src/__init__.py` with identical bytes would otherwise share one summary row, so
a path-and-hash key lets coverage counters, failure state, and summary text leak
or overwrite across projects. Add a backfill job for existing indexes and
refresh-on-reindex wiring in the index sync path. Summaries follow the 180-char
single-line contract (UA one-sentence node model). Focused pytest under
`GOBBY_TEST_PROTECT=1` with an isolated test daemon where needed.

Storage without a producer is the failure mode this deliverable is most likely to
ship, so the production lifecycle is named here rather than assumed. Today
maintenance requests only unsummarized *symbols* (`maintenance.py:259`), and that
whole subsystem is constructed only when the optional
`code_index.symbol_summary.enabled` flag is true
(`runner_lifecycle_subsystems.py:483`). File summaries cannot inherit that gate:
2.5 makes coverage a freshness precondition and 5.1 makes it a cutover gate, so a
mandatory input would sit behind an optional switch and a disabled-by-default
deployment would never build a single summary. File-summary production is
therefore its own always-on maintenance pass with its own batch config,
registered at startup independently of symbol summaries, and driven by the same
reindex and sync-completion seams that already mark files dirty.

Scope boundary: this deliverable owns the Python storage and production of
summaries only. Their Rust consumers — `gcode outline` rendering and
`FileFacts.summary` population — belong to 2.2, which owns the crate surface and
depends on this section.

Per-file coverage is the whole replacement story for ~2,923 deleted pages, so a
silent summarizer gap is a silent retrieval regression. Storage tracks, per
project, `indexed_file_count`, `current_summary_count` (summary matches the
file's current `content_hash`), `stale_summary_count`, and
`failed_summary_count` — a summarizer error records a failure row instead of
being skipped. Rows whose `content_hash` no longer matches, and rows for deleted
files, never surface in `gcode outline`, `gcode search`, or the FactsBundle;
they count as stale until refreshed or reaped.

None of that storage exists yet. `code_indexed_files`
(`postgres_baseline_schema.sql:1568-1583`) carries id, project, path, language,
content hash, symbol count, byte size, the two sync flags and their attempt
timestamps — no summary, no failure state, no attempt counter. The hub schema is
migration-driven, so this deliverable ships the next numbered migration and its
baseline mirror alongside the Python, exactly as 4.7 does for the queue.
Whether the summary lands as columns on `code_indexed_files` or a normalized
side table is an implementation call; either way the current-hash constraint and
the failure row are schema, not application convention.

**Acceptance:**

- 2.4.1 - File-summary storage keyed by project+path+content_hash with backfill and the four coverage counters. file: `src/gobby/code_index/_storage/summaries.py`.
- 2.4.2 - Summarizer produces/refreshes one-sentence file summaries on reindex and records failures rather than skipping. test: `tests/code_index/test_file_summaries.py`.
- 2.4.3 - Stale and deleted-file rows never surface to consumers; every current file does. test: `tests/code_index/test_file_summaries.py::stale_rows_never_surface`.
- 2.4.4 - A numbered migration creates the summary and failure storage with its project-scoped current-hash constraint, mirrored in the baseline schema, applying cleanly and idempotently on an existing hub. test: `tests/storage/test_file_summary_migration.py`.
- 2.4.5 - Two projects holding the same path with identical bytes keep independent summary text, staleness, failure state, and coverage counters. test: `tests/code_index/test_file_summaries.py::summaries_are_project_scoped`.
- 2.4.6 - File-summary production is registered and runs at daemon startup with `code_index.symbol_summary.enabled` false, backfilling an existing index and refreshing on reindex without the symbol-summary subsystem being constructed. test: `tests/code_index/test_file_summaries.py::backfill_runs_without_symbol_summaries`.

### 2.5 Index auto-freshness audit and staleness surfacing [category: code] (depends: 2.4)
`kind: deliverable`

Target: `src/gobby/code_index/trigger.py`,
`src/gobby/servers/routes/code_index.py`

The per-file live story depends on the index staying fresh without manual runs.
Audit and close gaps: post-commit hooks already send changed files to
`gcode index --files` (`trigger.py:128`); verify non-commit working-tree edits
are covered (session file-edit hook events → debounced index refresh) and add
the missing wiring if not. Surface index staleness (index commit vs HEAD,
pending-file count) plus the 2.4 summary-coverage counters
(`indexed_file_count`, `current_summary_count`, `stale_summary_count`,
`failed_summary_count`) in the code-index status route and in the codewiki
status payload consumed later by 4.2. Two refusals hang off this data: 4.1
refuses a `fresh` stamp when the index lags HEAD, and the 4.3 finalizer refuses
a `fresh` stamp or a FULL completion while
`current_summary_count < indexed_file_count`. A backfill failure therefore
cannot masquerade as a healthy build.

**Acceptance:**

- 2.5.1 - Working-tree edits trigger debounced incremental indexing (or the existing path is verified with a regression test). test: `tests/code_index/test_trigger_freshness.py`.
- 2.5.2 - Staleness (index commit vs HEAD, pending count) is reported in status. file: `src/gobby/servers/routes/code_index.py`.
- 2.5.3 - Status reports the four summary-coverage counters and incomplete coverage blocks a fresh/FULL completion. behavior: "summary coverage gates freshness" in `src/gobby/servers/routes/code_index.py`.

## P3: gwiki code_wiki Engine
`kind: framing`

**Goal**: gwiki renders, validates, and owns `code/**` — deterministic pages
from FactsBundle, scaffolds for agent pages, a machine-checkable validation
gate, and a confined generated-write path.

### 3.1 Port deterministic renderers onto FactsBundle [category: code] (depends: P2)
`kind: deliverable`

Target: `crates/gwiki/src/code_wiki/render/mod.rs`,
`crates/gwiki/src/code_wiki/citations.rs`,
`crates/gwiki/src/code_wiki/features.rs`

New module implemented against `gobby_core::code_facts`. The gcode renderers are
reference material only — no imports from `crates/gcode/src/commands/codewiki/`.
Reference map, each reimplemented with FactsBundle inputs:
`build_parts/{features,changes,hotspots,infrastructure}.rs`, `ownership/`
(git-blame analysis + render), deprecations/audit render, plus shared substrate
`strict_markdown.rs`, `frontmatter.rs` + `text/frontmatter.rs`,
`text/citations.rs` (extended: optional symbol-UUID anchor per citation,
rendered as `path:Lstart-Lend`), `text/sanitize.rs`, `doc_paths.rs`/`paths.rs`
path+wikilink helpers. Emitted paths: `code/features.md`, `code/deprecations.md`,
`code/changes.md`, `code/hotspots.md`, `code/ownership.md`,
`code/infrastructure.md`. Rendering is pure FactsBundle → markdown; no DB or
graph access from the renderers (gwiki's existing falkor reads stay for
health/blast-radius only). Consumer tests load the same checked-in golden
fixture (`crates/gcore/tests/fixtures/facts_bundle_v1.json`) that gcore
round-trips and gcode's producer is pinned against, so the contract is proven
end to end rather than within one crate.

The feature catalog moves here with the renderer that consumes it. The handler
maps in `build_parts/features.rs` (`resolve_gcode_handler` :82,
`resolve_gwiki_handler` :236) and their three coverage tests — including
`catalog_command_set_equals_each_pinned_contract_exactly`, which requires the
catalog command set to equal every pinned contract exactly — relocate to
`crates/gwiki/src/code_wiki/features.rs`. Moving it now rather than at deletion
time is what removes 3.6's need to reach back into the legacy subtree: that
carve-out existed only because the old path rendered `features.md` from the
catalog and had to stay runnable, and 3.8 retires the old path.

**Acceptance:**

- 3.1.1 - Deterministic renderers produce the six pages from the shared golden FactsBundle fixture with no index access. file: `crates/gwiki/src/code_wiki/render/mod.rs`.
- 3.1.4 - The feature catalog and its three coverage tests live in gwiki, `catalog_command_set_equals_each_pinned_contract_exactly` passes from its new home, and no catalog remains under `crates/gcode/src/commands/codewiki/`. test: `crates/gwiki/src/code_wiki/features/tests.rs::catalog_command_set_equals_each_pinned_contract_exactly`.
- 3.1.2 - Citations support symbol-UUID anchors. file: `crates/gwiki/src/code_wiki/citations.rs`.
- 3.1.3 - An unsupported `FACTS_VERSION` is rejected before any page is written. test: `crates/gwiki/src/code_wiki/render/tests.rs::rejects_unsupported_version`.

### 3.2 Port diagram machinery [category: code] (depends: 3.1)
`kind: deliverable`

Target: `crates/gwiki/src/code_wiki/diagrams/compose.rs`,
`crates/gwiki/src/code_wiki/diagrams/tests.rs`

Reimplement the diagram substrate against FactsBundle, splitting it along the
two-phase boundary rather than carrying the legacy shape across. gwiki owns
everything deterministic: evidence construction from
`FactsBundle.repo.diagram_evidence` and ModuleFacts edges (reference:
`architecture_diagrams.rs` topology evidence + service matrix, post-1.5 form),
deterministic module-dependency and call-sequence mermaid (reference:
`render/diagrams.rs`), and the arrow verifier that rejects any arrow absent from
the supplied evidence (reference: `diagram_compose.rs`). Validity gate remains
the shared `gobby_core::vault::mermaid::is_valid_mermaid`.

Semantic (LLM) mermaid composition does **not** run inside `gwiki code render`.
`code render` is provably generation-free — no AI routing, no daemon generation
call, identical output with AI disabled — and composition becomes a writer-item
duty (4.5) whose result is re-verified by this same arrow verifier on generated
write (3.5). That keeps the deterministic/semantic split enforceable by
construction instead of by writer instructions. DiagramStats are recorded into
the P4 run report instead of `_meta/codewiki.json`.

**Acceptance:**

- 3.2.1 - Deterministic diagrams and the arrow verifier work from FactsBundle evidence. file: `crates/gwiki/src/code_wiki/diagrams/compose.rs`.
- 3.2.2 - Unevidenced arrows are rejected. test: `crates/gwiki/src/code_wiki/diagrams/tests.rs::rejects_unevidenced_arrows`.
- 3.2.3 - `gwiki code render` emits byte-identical output with AI routing disabled. test: `crates/gwiki/src/code_wiki/diagrams/tests.rs::render_is_generation_free`.

### 3.3 New renderers: At-a-glance index, layers, module scaffold [category: code] (depends: 3.1)
`kind: deliverable`

Target: `crates/gwiki/src/code_wiki/render/index.rs`,
`crates/gwiki/src/code_wiki/render/module.rs`

Three new deterministic renderers:
- `code/_index.md` — At-a-glance landing (per #18871): totals table (files,
  modules, indexed symbols, layers, tours), layer table (one row per layer,
  ≤180-char summary cell), tour links, freshness badge. Absorbs old `repo.md`
  duty as landing page; feeds the session-start `wiki_overview` injection.
- `code/architecture/layers.md` + `wiki/code/_meta/layers.json` — the layers
  artifact: `{layers:[{id,name,summary,modules[]}], modules:[{id,name,summary,files[]}]}`
  rendered from LayersProposal with **no agent involvement at all**. Layer names
  come from the dominant top-level path segment of member modules, ties broken by
  lowest sorted path so the name is stable across re-clustering.
  Summaries need a named producer, because `ModuleFacts` carries id, name, files,
  edges, neighbor exports, and git stats — no summary field (2.1). The only
  summary text in the bundle is `FileFacts.summary` (2.4), so both levels derive
  from it deterministically: a module summary is the shared cell normalizer over
  its member files' summaries in sorted path order, and a layer summary is that
  same normalizer over its member modules' derived summaries in sorted id order.
  No schema change, and both are byte-stable for a given bundle.
  Exhaustive-partition validator: every indexed file in exactly one module, every
  module in exactly one layer; violations are machine-readable errors.
  There is deliberately no `layer` page type in the 3.7 manifest and no layer
  work item in the 4.3 queue: `layers.md` is one deterministic render, so routing
  it through a writer agent would reintroduce exactly the structure-derivation the
  two-phase rule forbids.
- Module page scaffold — frontmatter (summary, freshness, template_version,
  provenance), deterministic file table (`Path | Summary | Key symbols`),
  reference table (`Symbol | Signature | Purpose | Source`, signature normalized
  one-line ≤180 chars, em dash for empty purpose — never `Indexed ... in ...`
  filler, exact source citation per row, 24-row cap with `gcode outline`
  overflow guidance), evidence-verified diagrams, and marked prose slots
  (`<!-- prose:purpose -->` etc.) the agent fills.
Shared summary-cell normalizer (first prose paragraph, strip structure,
collapse whitespace, cap 180 on word boundary, em dash when empty) used by all
three.

Every scaffold is stamped in frontmatter with `facts_digest` (canonical hash of
the FactsBundle it was rendered from) and `scaffold_hash` (hash of the
deterministic regions alone, prose slots excluded). Those two values are what
make the two-phase boundary checkable: 3.4 can re-render from the recorded
digest and diff, and 3.5 can reject any write that mutated a deterministic
region.

**Acceptance:**

- 3.3.1 - `_index.md` renders At-a-glance with counts matching the FactsBundle. file: `crates/gwiki/src/code_wiki/render/index.rs`.
- 3.3.2 - Partition validator rejects a file missing from all modules. test: `crates/gwiki/src/code_wiki/render/tests.rs::partition_exhaustive`.
- 3.3.6 - Layer names and summaries are derived without any agent input and are stable across a re-cluster that preserves membership, including the tie case. test: `crates/gwiki/src/code_wiki/render/tests.rs::layer_naming_is_deterministic`.
- 3.3.7 - Module and layer summaries are derived from `FileFacts.summary` by the shared normalizer in sorted order and are byte-identical across two renders of one FactsBundle. test: `crates/gwiki/src/code_wiki/render/tests.rs::summary_derivation_is_byte_stable`.
- 3.3.3 - Module scaffold emits reference rows as Symbol|Signature|Purpose|Source with citations and no structural filler. file: `crates/gwiki/src/code_wiki/render/module.rs`.
- 3.3.4 - Summary cells obey the 180-char single-line contract. symbol: `gobby_wiki::code_wiki::render::normalize_summary_cell`.
- 3.3.5 - Scaffolds carry `facts_digest` and `scaffold_hash`, stable across re-render of the same bundle. file: `crates/gwiki/src/code_wiki/render/module.rs`.

### 3.4 Validation gate: gwiki code validate [category: code] (depends: 3.2, 3.3, 3.7, 3.8)
`kind: deliverable`

Target: `crates/gwiki/src/commands/code_validate.rs`,
`crates/gcore/src/codewiki_contract.rs`,
`crates/gwiki/src/code_facts_source.rs`, `crates/gwiki/src/audit/claims.rs`,
`crates/gwiki/src/audit/tests.rs`

`gwiki code validate <page>... | --all` running: strict markdown shape
(ported `strict_markdown.rs`), mermaid validity (gcore gate), frontmatter
contract, citation resolution (spans/UUIDs resolve against the FactsBundle or
index), required-sections check against the 3.7 template manifest, and the
exhaustive-partition coverage check.

There is **one** marker, and it flips. `GENERATED_BY_CODEWIKI`
(`codewiki_contract.rs:31`) takes the value `gwiki-code`, and the same contract
gains the structured freshness object `{state, index_commit, generated_at,
behind}`, `template_version`, and the `summary` ≤180 helper — one golden fixture,
not two.

An earlier draft kept the legacy value live and introduced a second constant
beside it, on the reasoning that flipping would break the sites pinning the old
string. A dual marker buys exactly one thing, a legacy generator whose output is
still recognized, and 3.8 has already retired that generator. So the second
family is never created — along with its golden, its audit-path recognition, its
prune restriction in 4.3, and its quarantine-selection rule in 5.3.

Depending on 3.8 also shrinks the flip itself. The three legacy sites that pinned
the old value all live inside the subtree whose module declaration 3.8 dropped,
so they are no longer compiled and no longer break; what remains in the workspace
is the gcore golden, edited here, and the `gcode-codewiki` occurrences under
`crates/gwiki`. Those are **not** uniformly inert fixtures, and treating them as
such was wrong: `audit/claims.rs:197` grounds a page by exact equality with the
constant, and the audit fixtures at `crates/gwiki/src/audit/tests.rs:419,469,511,549,601`
assert that template, degraded, and generated pages are grounded or exempt. Flip
the constant without touching them and those assertions invert and the crate
tests fail. The sweep is therefore by *role*, not by count: fixtures whose
assertion depends on marker equality move to the shared constant, fixtures that
exist to exercise the frontmatter parser keep the bare literal, and one negative
test pins that a page carrying the legacy literal is not treated as generated.

One consequence is deliberate and worth stating rather than discovering. The
~2,900 legacy pages already on disk carry the literal `gcode-codewiki`, stop
matching the constant, and are therefore reported as ungrounded by `gwiki audit`
until 5.3 removes them. That is noise in a command nobody runs, about content
this epic deletes. It also makes 4.3's prune safe for free: a page that does not
carry the current marker is not a generated page, so the finalizer cannot reach
the legacy vault no matter what its inventory says.

Seventh check family, the one that makes the two-phase contract enforceable:
deterministic-region integrity. Re-render the scaffold from the page's recorded
`facts_digest` and reject any difference outside the declared prose and
semantic-diagram slots — a mutated file row, citation, freshness field, symbol
signature, or deterministic edge fails. Output is machine-readable JSON errors
(page, rule code, location) with stable codes (`CW_DETERMINISTIC_REGION_MUTATED`,
`CW_UNEVIDENCED_ARROW`, `CW_SUMMARY_TOO_LONG`, `CW_MISSING_SECTION`,
`CW_CITATION_UNRESOLVED`, `CW_PARTITION_INCOMPLETE`) so writer agents can
fix-and-retry against a code rather than parsing prose.

Re-rendering from a digest presumes something can turn that digest back into
bytes, and until now nothing named what. The bytes are the compressed
`codewiki_runs.facts_bundle` written at admission (4.2, column in 4.7), so this
deliverable ships the resolver that reads them:
`crates/gwiki/src/code_facts_source.rs`, a read-only hub lookup following the
`gobby_core::postgres::connect_readonly` precedent already used by
`commands/graph.rs`, `commands/graph_context.rs`, and `commands/benchmark.rs`.
It bounds the decompressed size, verifies `sha256(bytes) == facts_digest` after
decompression, and returns a typed miss rather than a panic. The source is
injectable, which is what keeps this deliverable inside P3: its own tests resolve
fixture bundles, so the validator ships and is exercised before any run row
exists, and the production binding is to a column 4.7 already declares. A check
family that names no way to fetch its own input is a requirement with no
implementation path.

That re-render reads two things at two moments — the page, then the bundle its
frontmatter names — and a run finalizing in between can replace the page and
prune the digest the validator is still holding. Left alone that is a false hard
failure on a healthy vault. Resolve it in the validator rather than by delaying
the prune: when a named `facts_digest` cannot be resolved, re-read the page once;
if its digest changed, the validator lost a benign race and validates the new
digest, and only an unchanged page with a missing bundle is a genuine
`CW_FACTS_BUNDLE_MISSING` failure. Retrying a read is cheaper and more honest
than making retention wait on every concurrent reader.

**Acceptance:**

- 3.4.1 - Validate runs all seven check families and reports machine-readable errors with stable rule codes. file: `crates/gwiki/src/commands/code_validate.rs`.
- 3.4.2 - gcore contract carries the freshness object, template_version, and `GENERATED_BY_CODEWIKI` flipped to `gwiki-code` behind a single golden; the golden is updated and all three crates build with the legacy subtree already uncompiled. file: `crates/gcore/src/codewiki_contract.rs`.
- 3.4.3 - A page missing a required template section fails validation. test: `crates/gwiki/src/commands/code_validate/tests.rs::missing_section_fails`.
- 3.4.4 - Fixtures mutating a file row, citation, freshness field, or deterministic edge are rejected as `CW_DETERMINISTIC_REGION_MUTATED`. test: `crates/gwiki/src/commands/code_validate/tests.rs::deterministic_region_mutation_rejected`.
- 3.4.5 - One table-driven case per check family invokes `gwiki code validate` and asserts the page, location, and stable rule code for all seven: `CW_STRICT_MARKDOWN`, `CW_MERMAID_INVALID`, `CW_FRONTMATTER_INVALID`, `CW_CITATION_UNRESOLVED`, `CW_MISSING_SECTION`, `CW_PARTITION_INCOMPLETE`, `CW_DETERMINISTIC_REGION_MUTATED`. test: `crates/gwiki/src/commands/code_validate/tests.rs::every_check_family_reports_its_code`.
- 3.4.6 - After the flip no compiled site asserts the value `gcode-codewiki` against the constant: every audit, librarian, indexer, and lint fixture whose assertion depends on marker equality resolves through the shared constant and its suite passes, while the frontmatter parser fixtures keep the bare literal and still parse. test: `crates/gwiki/src/audit/tests.rs`.
- 3.4.7 - A validation paused between page read and bundle lookup, with the page replaced and its old bundle pruned, re-reads and validates the new digest; an unchanged page whose bundle is missing fails as `CW_FACTS_BUNDLE_MISSING`. test: `crates/gwiki/src/commands/code_validate/tests.rs::bundle_prune_race_is_benign`.
- 3.4.8 - The facts source resolves a page's recorded `facts_digest` to bundle bytes, rejects a digest mismatch and an over-bound decompression as typed errors, and satisfies validation from injected fixtures with no hub connection. test: `crates/gwiki/src/code_facts_source/tests.rs::resolves_verifies_and_bounds`.
- 3.4.9 - The audit claim path grounds pages carrying the flipped constant, and a legacy page carrying the bare `gcode-codewiki` literal is reported ungrounded rather than crashing or being treated as generated. test: `crates/gwiki/src/audit/tests.rs::legacy_literal_is_not_generated`.

### 3.5 Generated-write mode for code/** [category: code] (depends: 3.4)
`kind: deliverable`

Target: `crates/gwiki/src/commands/page.rs`

Today `confine_page_path` restricts writes to `knowledge/**`
(`WRITABLE_PREFIX = "knowledge"`, page.rs:13, enforced :205-208). Add a
generated-write mode: `gwiki page write --generated --template <id>@<ver>`
may target `code/**`; it runs the 3.4 validation gate before commit, stamps
`content_hash` via `page_version.rs`, and preserves symlink/ancestor
confinement. Writes without `--generated` to `code/**` remain forbidden.
`page delete` gains the same generated gate for `code/**` (cutover pruning).
Contract update for the new flags (churn checklist).

The generated write is where the two-phase boundary is actually enforced: before
commit, re-render the scaffold from the submitted page's `facts_digest` and
reject the write if anything outside the declared prose/semantic-diagram slots
changed, returning the 3.4 rule code. An agent that rewrites a file table or
edits a freshness field gets a rejection, not a landed page.

The generated write also has to be atomic, because 4.3 relies on it: the current
path validates and then calls `fs::write` directly (`page.rs:115`), which can be
interrupted partway and leave a truncated page. Generated writes stage into a
temp file in the destination directory and `rename(2)` into place, so a
concurrent reader, a crash, or an orphaned child process sees either the whole
previous page or the whole new one — never a partial file. Confinement applies
to the staged path as well, so staging cannot escape the vault.

Atomicity is not ordering, so the generated write also carries an explicit
expected-state precondition — 4.3 relies on it to refuse a late orphan. Making
`--expected-hash` mandatory is right for replacement and impossible for creation:
`page.rs:60-67` rejects `--expected-hash` outright in `create` mode ("requires
--mode upsert"), and `upsert` carrying a hash rejects a page that does not exist
(:77-84). Every page of the first FULL run is exactly that case, so a mandatory
hash leaves no legal argument set that can create the first module, concept,
tour, or overview page — the plan's own dogfood run could not produce a single
page. The precondition domain has to include absence. Generated writes require
exactly one of `--expected-hash <h>` or `--expected-absent`; they are mutually
exclusive and one is mandatory, so an unconditional generated write stays
unrepresentable. Both are rechecked under the same page lock immediately before
the rename, so a delayed orphan holding `--expected-absent` for a page that has
since been created lands nothing, exactly as a stale hash does.

Generated **delete** takes the same two flags with a deliberately narrower
contract, because the symmetry is only apparent. `--expected-hash` is the removal
authority: it names the exact bytes the caller intends to destroy and is
rechecked under the page lock immediately before the unlink, so 4.3's finalizer
cannot prune a page a newer generation has already replaced — today's delete path
checks existence and removes with no precondition at all (`page.rs:137`).
`--expected-absent` carries no removal authority; it asserts the page is already
gone and yields an idempotent success, which is what makes a replayed prune safe.
Deleting an existing page therefore requires the hash, and a delete arriving with
`--expected-absent` against a page that exists is a precondition failure rather
than a removal.

**Acceptance:**

- 3.5.1 - `code/**` writes require --generated + template and pass validation before commit. file: `crates/gwiki/src/commands/page.rs`.
- 3.5.2 - Non-generated writes to code/** are still rejected. test: `crates/gwiki/src/commands/page/tests.rs::code_write_requires_generated`.
- 3.5.3 - A generated write mutating a deterministic region is rejected with its rule code. test: `crates/gwiki/src/commands/page/tests.rs::out_of_slot_write_rejected`.
- 3.5.4 - Generated writes land by staged-temp-plus-rename within the confined root; a write interrupted before rename leaves the previous page intact and complete. test: `crates/gwiki/src/commands/page/tests.rs::generated_write_is_atomic`.
- 3.5.5 - A generated write requires exactly one of `--expected-hash` or `--expected-absent`; supplying both, or neither, is a typed input error, and each precondition is rechecked under the page lock immediately before rename so a delayed write whose expected state is stale lands nothing. test: `crates/gwiki/src/commands/page/tests.rs::stale_generation_write_is_refused`.
- 3.5.6 - `--expected-absent` creates a page that does not exist and is refused once the page exists; two concurrent creators of one path yield exactly one landed page and one precondition failure. test: `crates/gwiki/src/commands/page/tests.rs::expected_absent_creates_once`.
- 3.5.7 - Generated delete of an existing page requires `--expected-hash` and is refused when the hash is stale or `--expected-absent` is supplied; `--expected-absent` against an already-absent page succeeds idempotently; a delete racing a replacement write removes nothing. test: `crates/gwiki/src/commands/page/tests.rs::generated_delete_requires_expected_hash`.

### 3.6 gwiki contract and daemon gateway surface [category: code] (depends: 3.4, 3.5)
`kind: deliverable`

Target: `crates/gwiki/src/contract.rs`,
`crates/gwiki/contract/gwiki.contract.json`, `src/gobby/gwiki_gateway.py`,
`crates/gwiki/tests/cli_contract.rs`, `tests/test_cli_contracts.py`,
`crates/gwiki/src/cli.rs`, `crates/gwiki/src/cli/mapping.rs`,
`crates/gwiki/src/api.rs`, `crates/gwiki/src/commands/mod.rs`,
`crates/gwiki/src/commands/project_admission.rs`

Wire the commands before pinning them. `code render|validate|status` need the
subcommand enum and argument structs in `crates/gwiki/src/cli.rs`, the
CLI→API translation in `crates/gwiki/src/cli/mapping.rs`, the request enum and
service selection in `crates/gwiki/src/api.rs`, and handler registration in
`crates/gwiki/src/commands/mod.rs`. A contract entry without those is a pinned
name for a command the binary cannot run.

One consumer of that enum sits outside the command module and is exhaustive by
variant: `commands/project_admission.rs` matches every `Command` by name, with
the read-only arm enumerating each one explicitly (`:40-69`). A new variant that
is not classified there is a Rust compile error, so a literal implementation of
this plan stopped at a build failure before any `code` command could ship. All
three are `ReadOnly` in that classifier's sense — it splits commands that may
*admit a new project scope* (`index`, `collect`, `ingest-*`, `sync-sessions`,
`refresh`) from those that operate inside an existing one, and `page write` and
`page delete` already sit on the read-only side, so vault mutation is not the
axis it divides on. This is the general rule for the whole command-wiring sweep:
every exhaustive consumer of the enum, not only the ones inside `commands/`.

Pin the new commands (`code render`, `code validate`, `code status`, plus the
`page write`/`page delete` flag changes) in `crates/gwiki/src/contract.rs` +
`crates/gwiki/contract/gwiki.contract.json` + vendored
`tests/contracts/gwiki.contract.json` + `crates/gwiki/tests/cli_contract.rs` +
`tests/test_cli_contracts.py` + `docs/contracts/gwiki-cli.md`. Add matching arms
to the `resolve_gwiki_handler` map, which 3.1 relocated into
`crates/gwiki/src/code_wiki/features.rs` alongside the renderer that reads it:
`catalog_command_set_equals_each_pinned_contract_exactly` requires the catalog
command set to equal every pinned contract exactly, so new gwiki commands cannot
land without those arms. With the catalog already in gwiki this is an ordinary
edit to a file this phase owns, not a reach into the legacy subtree.
Python: `GwikiGateway.code_render/code_validate/
code_status` wrappers in `src/gobby/gwiki_gateway.py` following `_run_json`
conventions.

New wrappers are not enough, because the executor lands pages through the
*existing* ones. `GwikiGateway.write_page` builds argv as
`["page","write","--path",path,"--mode",mode]` and forwards `--expected-hash`
only when supplied (`gwiki_gateway.py:285-299`), and `delete_page` takes a path
alone (:301) — neither can express the `--generated --template <id>@<ver>`
authorization 3.5 now requires for `code/**`, so a literal implementation would
leave every generated write and every prune rejected by the binary they call.
Both methods gain generated/template parameters that reach argv, carrying exactly
one of the two 3.5 preconditions, and existing knowledge-vault callers keep
today's non-generated behavior by default. The two methods do not carry the same
precondition domain, because 3.5's delete contract is narrower than its write
contract: `write_page` forwards `--expected-hash` for a replacement or
`--expected-absent` for a creation, while `delete_page` forwards
`--expected-hash` to remove an existing page and may forward `--expected-absent`
only to assert an already-completed removal. A gateway that let a prune delete
under `--expected-absent` would hand the finalizer exactly the unconditional
removal 3.5 refuses at the binary. Reinstall the gwiki binary.

This deliverable carries the P3 exit gate. It is the phase's terminal leaf — it
depends on 3.4 and 3.5, which reach 3.8, 3.7, 3.3, 3.2, and 3.1 transitively — so
the gate cannot run before the validator, the write path, the contract surface,
or the retirement it certifies. That role moved here from 3.8 when retirement was
ordered ahead of the marker flip; a gate hanging off the first step of the phase
would certify nothing.

**Acceptance:**

- 3.6.1 - New commands pinned; both binaries' drift tests pass. file: `crates/gwiki/contract/gwiki.contract.json`.
- 3.6.2 - The relocated feature-catalog handler map covers the new gwiki commands and stays exactly equal to the pinned contract set. test: `crates/gwiki/src/code_wiki/features/tests.rs`.
- 3.6.3 - GwikiGateway exposes the code_* wrappers. file: `src/gobby/gwiki_gateway.py`.
- 3.6.6 - `write_page` and `delete_page` forward generated/template authorization to argv along with exactly one of `--expected-hash` or `--expected-absent`; `delete_page` cannot emit a removal of an existing page under `--expected-absent`; existing knowledge callers still emit today's non-generated argv. test: `tests/wiki/test_gwiki_gateway.py::generated_flags_reach_argv`.
- 3.6.7 - Every exhaustive consumer of the gwiki `Command` enum classifies the three new `code` commands, including `commands/project_admission.rs`, and the crate compiles with no non-exhaustive-match error. test: `crates/gwiki/src/commands/project_admission/tests.rs::code_commands_are_classified`.
- 3.6.4 - Each `code` subcommand parses, maps, dispatches, and returns a nonzero exit with a machine-readable error for a bad page argument. test: `crates/gwiki/src/cli/tests.rs`.
- 3.6.5 - P3 exit gate: all three crates build, `~/.gobby/bin/{gcode,gwiki,ghook}` are rebuilt and reinstalled, and the daemon answers `wiki_search`, `wiki_ask`, and `wiki_read` against the existing vault with no codewiki generator compiled or installed. test: `tests/wiki/test_phase_exit_smoke.py`.

### 3.7 Page-type manifest and required-section contract [category: config]
`kind: deliverable`

Target: `src/gobby/install/shared/templates/codewiki/manifest.yaml`

The manifest is the validator's input, so it ships before the validator, not
with the prose templates that consume it. This deliverable defines the data
only: the page-type registry (`module`, `concept`, `tour`, `overview`), each
type's `template_version`, its ordered required-section list, its prose-slot
names, and which slots accept a semantic diagram. No prompt text — 4.4 writes
the prose bodies and golden examples against this schema and may not add,
rename, or reorder sections without a `template_version` bump.

The manifest is also the executor's planning input, so every field 4.3 step 0
reads is declared here rather than left implicit: `tour_audiences` (ordered,
unique; the launch set is `new-contributor` and `operator`) and `seed_concepts`
(ordered, unique). Planning enumerates one tour item per declared audience and
seeds the concept set from this list, so a planner input that lives only in
prompt prose would make the item set unreproducible.

Section ids are stable strings, not headings: renaming a human-facing heading
must not silently invalidate every landed page, and a section rename that
*should* invalidate them is expressed as a version bump.

**Acceptance:**

- 3.7.1 - Manifest defines four page types with template_version, ordered required sections, and prose-slot names. file: `src/gobby/install/shared/templates/codewiki/manifest.yaml`.
- 3.7.2 - Manifest parses into the section spec `gwiki code validate` consumes, and an unknown page type is a typed error. test: `tests/wiki/test_codewiki_manifest.py`.
- 3.7.3 - Manifest declares ordered, unique `tour_audiences` and `seed_concepts`; both parse into the planner input 4.3 step 0 consumes, and a duplicate entry in either is a typed error. test: `tests/wiki/test_codewiki_manifest.py::planner_inputs_are_declared`.

### 3.8 Retire the gcode codewiki command [category: code] (depends: 3.1, 3.2)
`kind: deliverable`

Target: `crates/gcode/src/cli.rs`, `crates/gcode/src/dispatch.rs`,
`crates/gcode/src/contract.rs`, `crates/gcode/contract/gcode.contract.json`,
`crates/gcode/src/cli/tests/codewiki.rs`,
`crates/gcode/src/commands/mod.rs`,
`src/gobby/code_index/gcode_gateway.py`,
`src/gobby/code_index/codewiki_refresh.py`,
`src/gobby/code_index/codewiki_trigger.py`,
`src/gobby/code_index/codewiki_nightly.py`,
`src/gobby/runner_init/orchestration.py`,
`src/gobby/servers/_app_lifecycle.py`, `src/gobby/app_context.py`,
`src/gobby/servers/routes/code_index.py`,
`src/gobby/cli/installers/git_hooks.py`

By this point the subtree has given up everything the new pipeline needs: 2.2
copy-refactored its snapshot/model/cluster producers, 3.1 reimplemented the
deterministic renderers and took the feature catalog with it, and 3.2
reimplemented the diagram substrate. What remains is a 38K-LOC generator with no
users, and keeping it *invocable* is what forces the dual-marker window, the
freeze carve-out, and the fallback clauses this plan previously carried. Retire
the entry points now; 5.2 deletes the code.

Retirement runs **before** 3.4's marker flip, which is why 3.4 depends on this
section rather than the reverse. `GENERATED_BY_CODEWIKI` is one shared constant
and the legacy generator stamps its pages from that same constant, so flipping it
while `gcode codewiki` is still invocable — by hand, by the installed git hook,
or by the nightly cron — would have the retired pipeline emit old-shape pages
carrying the *new* marker. That marker is exactly the identity 4.3's prune and
5.3's quarantine use to tell the two page families apart, so a single run in that
window would poison both. Ordering the shutdown first makes the interleaving
unreachable rather than merely unlikely, which is why nothing downstream has to
detect it.

Remove the reachable surface: `Command::Codewiki` and the AI arg enums
(`cli.rs:383-513`) with their `From` impls (:75-128, the only inbound reference to
the subtree from outside it), the dispatch arms
(`dispatch.rs:87-97,134-153,578-630`), the contract entry in `contract.rs` +
`gcode.contract.json` + the vendored copy + drift tests + the phase7 contract
test, `crates/gcode/src/cli/tests/codewiki.rs`, and the `codewiki` arm of
`resolve_gcode_handler` in the relocated catalog — that map must equal the pinned
contract set exactly, so dropping the contract entry without the arm fails 3.1's
coverage test.

"Present but unreachable" is a property of the module graph, not of the CLI. The
crate's public `commands` module declares the subtree
(`crates/gcode/src/commands/mod.rs:1`) and the subtree re-exports its generation,
repair, purge, and citation entry points, so removing only the command leaves a
compiled, publicly callable generator behind. Drop that module declaration here
as well: the files stay on disk as the written record 5.2 deletes, but they are
no longer compiled, no longer exported, and their ~10K subtree tests no longer
run.

The Python side is a call graph rather than a single method, and every node of it
exists only to reach the command being removed. `GcodeGateway.codewiki()`
(`gcode_gateway.py:480-502`) is invoked by the refresh service
(`codewiki_refresh.py:89`), driven by the debounce trigger
(`codewiki_trigger.py`), constructed at application startup
(`_app_lifecycle.py:77`) into a service-container field (`app_context.py:103`),
and reached through `POST /api/code-index/codewiki/refresh` and
`GET /api/code-index/codewiki/status` (`servers/routes/code_index.py:457,508`);
separately, runner initialization registers the nightly cron
(`runner_init/orchestration.py:544`, handler in `codewiki_nightly.py`) and the
installed git hook posts to that same legacy route (`git_hooks.py:57-69`). All of
it goes here. 4.2 previously promised to retire those route internals; that
promise moves into this section, because the routes have no implementation left
once the trigger is gone, and 4.2 builds its own debounce into the new service
rather than adopting an orphan.

This opens a deliberate window: from here until 4.2 wires the new endpoint, no
codewiki automation runs at all. That is the point rather than a cost — the
generated pages are already stale, nobody queries them, and a nightly job
rebuilding content the epic is replacing burns tokens against a vault scheduled
for deletion. The vault stays on disk and stays queryable throughout; only the
writer stops. The wiki tab's codewiki freshness strip and its refresh action call
the routes removed here, so for the length of the window they render their empty
state; 4.2 repoints them at the new endpoints.

**Acceptance:**

- 3.8.1 - No `codewiki` command, arg enum, dispatch arm, or contract entry remains in gcode; drift and phase7 contract tests pass. file: `crates/gcode/contract/gcode.contract.json`.
- 3.8.2 - The relocated catalog's command set still equals every pinned contract exactly after the gcode entry is dropped. test: `crates/gwiki/src/code_wiki/features/tests.rs::catalog_command_set_equals_each_pinned_contract_exactly`.
- 3.8.3 - The daemon retains no `gcode codewiki` call path at any depth: the gateway method, the refresh service, the debounce trigger, its startup construction and service-container field, both legacy routes, and the nightly cron registration are all gone, and the installed git hook posts to no codewiki route. file: `src/gobby/code_index/codewiki_refresh.py`.
- 3.8.4 - The subtree's module declaration is gone, `gcode` builds with the subtree uncompiled and its tests no longer collected, and its index, search, symbol, graph, and outline commands are unaffected. test: `tests/code_index/test_gcode_phase7_contract.py`.

## P4: Daemon Orchestration and Templates
`kind: framing`

**Goal**: The daemon classifies changes, fans out template-driven wiki-writer
agents through an internal queue, validates and lands pages, and finally
executes librarian tasks — one orchestration for both wiki loops.

### 4.1 Change classifier [category: code] (depends: P2)
`kind: deliverable`

Target: `src/gobby/wiki/codewiki/classifier.py`

Classify a change set into `SKIP | COSMETIC | PARTIAL | ARCHITECTURE | FULL`:
- Inputs: changed file list + per-file `(content_hash, structural_hash)` from
  the code index; baseline from `wiki/code/_meta/build.json`.
- COSMETIC: content changed, structural hashes unchanged → deterministic
  re-render + freshness stamps only (zero LLM).
- PARTIAL: ≤10 structurally-changed files → affected module pages via gwiki
  blast radius (`code_graph.rs::affected_pages_for_changes`) + member concepts.
- ARCHITECTURE: >10 structural files or a new/deleted top-level directory →
  also layers/overview/tours, with prior section and layer names injected for
  stability.
- FULL: >30 structural files, >50% of modules touched, or `template_version`
  bump.
Ordering invariant (UA issue-#152 lesson): write the fingerprint baseline to
`build.json` BEFORE stamping the commit hash, so a crash never strands a
commit stamp without fingerprints (which would escalate every later run to
FULL). Classifier refuses to stamp `fresh` when the index lags HEAD (2.5).

The five bands are the public contract, surfaced verbatim in the 4.2 `/status`
and `/runs/{id}` payloads together with a machine-readable `reason` code, so an
operator can tell why a commit produced no LLM work. The decision table is
exhaustive over these inputs, and each row is a fixture test:

| Change set | Band |
|---|---|
| No indexed file touched, or only ignored paths | SKIP |
| Index lags HEAD (cannot classify safely) | SKIP + refuse `fresh` stamp |
| Content-only edits, all structural hashes unchanged (docstring/README/comment) | COSMETIC |
| File added or deleted (structural by definition), ≤10 structural files | PARTIAL |
| File renamed: treated as delete+add against the module partition | PARTIAL, or ARCHITECTURE if it crosses top-level dirs |
| 1–10 structurally-changed files | PARTIAL |
| 11–30 structural files, or new/deleted top-level directory | ARCHITECTURE |
| >30 structural files, or >50% of modules touched | FULL |
| `template_version` bump in the installed manifest | FULL |

Boundary cases are pinned explicitly (10 vs 11, 30 vs 31, 50% exactly) so a
threshold edit cannot silently reclassify.

**Acceptance:**

- 4.1.1 - Fixture tests cover every decision-table row including add, delete, rename, content-only, the 10/11, 30/31 and 50% boundaries, template bump, and stale-index refusal. test: `tests/wiki/test_codewiki_classifier.py`.
- 4.1.2 - Fingerprint baseline is durably written before the commit stamp. behavior: "baseline-before-stamp ordering" in `src/gobby/wiki/codewiki/classifier.py`.
- 4.1.3 - Status and run payloads carry the same five-band enum plus a reason code. file: `src/gobby/wiki/codewiki/classifier.py`.

### 4.2 Generation service, routes, and changed-file plumbing [category: code] (depends: 4.1, 4.7)
`kind: deliverable`

Target: `src/gobby/wiki/codewiki/service.py`,
`src/gobby/servers/routes/wiki.py`, `src/gobby/cli/installers/git_hooks.py`,
`src/gobby/app_context.py`, `src/gobby/servers/_app_lifecycle.py`,
`web/src/components/activity/wiki/WikiTabData.ts`

Service + FastAPI routes on the wiki router (`servers/routes/wiki.py`,
registered via `servers/_app_routes.py`):
- `POST /api/wiki/codewiki/generate` `{project_id, mode: auto|full|scope,
  scope?, changed_files?}` → 202 `{run_id}` (auto runs the classifier).
- `GET /api/wiki/codewiki/status` — classifier decision, queue depth, freshness
  rollup, last build.json summary (includes index staleness from 2.5).
- `GET /api/wiki/codewiki/runs/{run_id}` — per-run progress.
Repoint the git post-commit hook body
(`src/gobby/cli/installers/git_hooks.py`) to POST the **changed-file list**
(already computed as `CHANGED_FILES` for `gcode index --files`, today dropped
for codewiki) plus before/after SHAs to the new endpoint. The legacy routes and
the debounce trigger that backed them were removed in 3.8, so nothing is retired
here and nothing is adopted: the service carries its own debounce, which is a
handful of lines against a queue that already coalesces by fingerprint, and is
cheaper than reviving an orphaned component built around a gateway call that no
longer exists.

A service nobody constructs admits nothing, so this deliverable also owns the
wiring 3.8 vacated. The generation service is built at application startup
(`_app_lifecycle.py`) into the service container (`app_context.py`) in the slot
the retired trigger used to occupy, and shut down with the rest of the
application; 4.3 hangs the executor and watchdog tasks off the same owner. The
wiki tab's freshness strip and refresh action, dark since 3.8, are repointed from
the removed `code-index` paths to `GET /api/wiki/codewiki/status` and
`POST /api/wiki/codewiki/generate` (`WikiTabData.ts:705,744`), together with the
route mocks in their component tests.

Commit hooks, nightly cron, and manual calls all land here and can overlap, so
admission is where that is resolved. Each accepted run pins `(index_commit,
facts_digest, template_version, concept_catalog_digest)` at admission and never
re-reads them mid-run — a reindex landing halfway through cannot mix facts
generations into one build. That bundle is **repo-wide on every run**, including
`scope` requests and PARTIAL classifications. A scoped run regenerates a handful
of pages, but 4.3 step 0 derives its desired inventory — the authority that
decides what gets deleted — from this same bundle, and a bundle describing three
modules cannot say what the other fifty pages should be. Scoping belongs to the
work items, never to the facts a run is admitted with. A request whose trigger fingerprint matches an
in-flight run coalesces into it and returns that run id; a request at a newer
commit writes the single queued-successor row instead of mutating the in-flight
one, and 4.7's schema — not an application check — enforces both that one run per
project executes at a time and that a successor can still wait behind it.

Mode is not the only requested state that has to survive that coalescing. A
`scope` request names pages explicitly, and it is accepted independently of the
classifier's band, so a run's work set is a function of `(mode, scope)` rather
than mode alone. Admission therefore records the canonical requested scope
alongside the requested mode on both the executing and the queued row, and 4.7
carries the merge rule; dropping it would let `scope A` followed by any later
trigger silently discard the pages A asked for, with no diff able to recover them
because they were never a change set in the first place.

`concept_catalog_digest` is in that tuple because the FactsBundle does not
describe the vault. 4.3 plans concept items from the union of the manifest seed
list and the concept pages already present, which is mutable state a librarian
run or a human can change between planning and replanning. Admission therefore
canonicalizes the concept-page inventory (sorted paths), stores it beside the
bundle, and pins its digest; planning reads the snapshot rather than the live
vault. Without it, "two runs over one FactsBundle plan identical item sets"
(4.3.6) is simply false.

Pinning a digest is not the same as keeping the bytes it names. Validators
re-render deterministic regions from a page's recorded `facts_digest` (3.4) and
writers are handed a bundle, so a run that resumes after a daemon restart needs
the exact FactsBundle it was admitted with — by then the working tree, the
index, and any temp file have all moved on. Admission therefore stores the
canonical bundle bytes in the run row itself (`codewiki_runs.facts_bundle`,
compressed) rather than a path into a directory nobody owns. Every read verifies
`sha256(bytes) == facts_digest` and hard-fails on mismatch.

Pages outlive the run that wrote them, so the bytes cannot simply die with the
run row. A PARTIAL run touches a handful of pages and leaves the rest stamped
with older digests, so at any moment the vault references several live bundles;
3.4 re-renders each page's deterministic regions from *its* recorded digest, and
`gwiki code validate --all` (5.1) walks every page. Deleting a run row would
therefore break validation for every page still pointing at it, while keeping
every row forever is unbounded. Retention is by reference: a bundle is retained
while any current page or resumable run names its digest, and the finalizer
prunes bundles that nothing references once its run reaches a terminal state.

**Acceptance:**

- 4.2.1 - Generate/status/runs endpoints exist on the wiki router. file: `src/gobby/servers/routes/wiki.py`.
- 4.2.2 - Git hooks deliver changed-file lists to the codewiki endpoint. file: `src/gobby/cli/installers/git_hooks.py`.
- 4.2.3 - Endpoint tests cover auto/full/scope modes. test: `tests/servers/routes/test_wiki_codewiki_routes.py`.
- 4.2.4 - Runs pin index_commit/facts_digest/template_version; duplicate triggers coalesce and newer commits queue a successor. test: `tests/servers/routes/test_wiki_codewiki_routes.py::overlapping_triggers_coalesce`.
- 4.2.5 - Admission stores the canonical bundle bytes; a run resumed after restart re-reads them, verifies the digest, and hard-fails on mismatch. test: `tests/wiki/test_codewiki_executor.py::run_resumes_from_stored_bundle`.
- 4.2.6 - A bundle referenced by any current page or resumable run survives pruning; an unreferenced terminal-run bundle is pruned, and `validate --all` still passes across pages left by an earlier PARTIAL run. test: `tests/wiki/test_codewiki_executor.py::bundle_retained_while_referenced`.
- 4.2.7 - Two concurrent admissions with one fingerprint produce exactly one executing run and both callers receive its id; an admission at a newer commit during that run queues a successor that later executes at the newer commit rather than being absorbed. test: `tests/servers/routes/test_wiki_codewiki_routes.py::concurrent_admission_single_winner`.
- 4.2.8 - The generation service is constructed at application startup into the service container and torn down on shutdown; a daemon started with no codewiki state serves generate/status/runs, and an admitted run is drained rather than left resident in the queue. test: `tests/servers/routes/test_wiki_codewiki_routes.py::service_is_constructed_at_startup`.
- 4.2.9 - The wiki tab's freshness strip and refresh action call the new `/api/wiki/codewiki` paths and no `code-index` codewiki path remains in the frontend or its route mocks. test: `web/src/components/activity/wiki/__tests__/WikiCodeMode.test.tsx`.

### 4.3 Run executor: queue, fan-out, landing, librarian unification [category: code] (depends: 4.2, 4.5, 4.7)
`kind: deliverable`

Target: `src/gobby/wiki/codewiki/executor.py`, `wiki/code/_meta/build.json`,
`wiki/code/_meta/truth_digest.json`,
`src/gobby/install/shared/templates/codewiki/query_corpus.yaml`,
`src/gobby/install/bundled_content_manifest.json`,
`src/gobby/runner_lifecycle_subsystems.py`

Hub-DB queue tables (`codewiki_runs`, `codewiki_page_items`, schema from 4.7)
written by the service; the executor drains a run:
0. Deterministic work planning. Every item in the run is enumerated before any
   agent starts, because the settled two-phase rule forbids agents from deriving
   structure — and an aggregate writer left to pick its own subject is deriving
   structure. Module items come from the FactsBundle clusters (ids already
   content-addressed, 2.2); tour items are one per `tour_audiences` entry
   declared in the 3.7 manifest; the overview is a singleton; concept items are
   the union of the manifest's `seed_concepts` and the concept pages recorded in
   the run's pinned concept-catalog snapshot (4.2) — never a live read of the
   vault, which a librarian run could mutate between plannings. New concepts are
   never invented mid-run — a missing concept is a librarian gap task, which is
   what that loop is for. `layers.md` is not a work item at all: it is a
   deterministic render in step 1 (3.3), names included. Each item gets its
   stable page path and rendered scaffold at planning time.

   Planning produces **two** sets, and conflating them destroys data. The
   *desired inventory* is every `code/**` page that should exist at this
   generation — derived in full from the pinned FactsBundle and catalog snapshot
   on every run regardless of mode, because that derivation is deterministic and
   costs no LLM work. The *work items* are the subset this run regenerates, which
   for a PARTIAL run is only the pages the classifier marked affected. The
   desired inventory is stored on the run row (`codewiki_runs.desired_inventory`,
   4.7) and mirrored into `build.json`; the work items become
   `codewiki_page_items` rows. Step 4 reconciles against the inventory, never
   against the work items.

   Two consequences follow, and both are freshness rules rather than new
   mechanism. A project with no prior successful build has no baseline to diff
   against, so the classifier returns FULL: an `auto` run on an empty vault must
   not plan three items and call the result a generation. And the finalizer
   refuses the `fresh` stamp while any page in the desired inventory is missing
   from disk, alongside the conditions it already refuses on (a failed mandatory
   item, incomplete summary coverage). An inventory is a claim about what *should*
   exist; stamping fresh before it does would let one PARTIAL run over an empty
   vault declare a sixty-page wiki current.

   That refusal is a detector, and a detector with no repair path is a livelock.
   Pages can disappear without any run's participation — an operator deletes one,
   a partial restore misses one, a failed landing never produced one — and a
   subsequent COSMETIC, PARTIAL, ARCHITECTURE, or scoped run selects its work
   from the change set, which will not mention a page nothing changed. Freshness
   would then be withheld forever while every automatic run declines to fix the
   reason. So the work set is the union of the mode-selected items and
   `desired_inventory` minus what is actually materialized on disk: missing
   deterministic pages are re-rendered in step 1, missing semantic pages are
   enqueued as mandatory items, and the run that detects the gap is the run that
   closes it. The union is bounded by the inventory, so a healthy vault adds no
   work and a FULL run is unaffected.
1. Deterministic renders inline via `GwikiGateway.code_render` (no agents).
2. LLM page items fan out via `spawn_agent_impl`
   (`src/gobby/mcp_proxy/tools/spawn_agent/`) called directly with
   `task_id=None`, batch ≤5 (config `wiki.codewiki_max_writers`, default 5)
   under `max_active_agents=10`; ordering: modules first, then aggregates
   (overview/concepts/tours) so aggregate writers see fresh module pages.
   Explicitly **not** `dispatch_batch`: that helper refuses any suggestion
   without a task ref ("refusing to spawn an unknown task"), then synthesizes
   `task_id` from it and calls `_suggestion_task_description(task_manager, …)`
   (`src/gobby/mcp_proxy/tools/spawn_agent/_factory.py:592`). Page items are
   queue rows, not tasks; routing them through a task-shaped interface would
   either warn on every page or silently bind a page to whatever task its id
   collided with. The executor owns its own `asyncio.Semaphore` for the batch
   cap.
3. Each agent returns finished page content; the **executor** commits it via
   `GwikiGateway` generated-write. Validation failure → one retry with the
   machine-readable errors in the prompt; second failure → file a task
   (category docs, label `codewiki-escalation`) and continue.
4. Finalizer: update `wiki/code/_meta/build.json` (run report incl.
   DiagramStats) with the run's desired inventory, rewrite
   `wiki/code/_meta/truth_digest.json`, prune generated pages absent
   from **that inventory**, re-render `_index.md`, refresh the `wiki_overview`
   session variable source, trigger reindex via `WikiUpdateCoordinator` — in that
   order, so reindex never sees an orphan.
   The truth digest is in that list because it is a live cross-subsystem
   dependency, not a codewiki artifact. Memory dream resolves each project's
   repo path and reads exactly `_meta/truth_digest.json` to decide whether stored
   memories must be re-judged, and a missing or unparseable file returns an empty
   digest **silently** — no error, no cooldown invalidation, just memories that
   quietly stop being re-judged. Its only producer today is the legacy generator
   3.8 retires, so without a replacement this epic disables a working feature with
   no failing test anywhere to notice. The new finalizer emits the same path in
   the same shape, derived from the run's pinned bundle, which costs one writer
   against data the run already holds and keeps the consumer and its fixtures
   untouched.
   Pruning is not optional bookkeeping: module ids are content-addressed over
   sorted member paths (2.2), so splitting a module, merging two, or deleting a
   file *renames* pages. Without a prune the previous generation stays on disk
   and stays indexed, and `wiki_ask` answers from a stale duplicate that no
   longer corresponds to any code.
   Three constraints keep the prune from becoming the data-loss step. It compares
   disk against the desired inventory from step 0, never against this run's work
   items — a PARTIAL run regenerates a handful of pages while every untouched
   page remains in the inventory and therefore survives. It runs only when every
   mandatory item succeeded; a degraded or failed run leaves the old pages alone,
   because an incomplete page set is not evidence that the missing pages are
   obsolete. And it deletes only pages carrying the current generated marker,
   which after 3.4's flip is `gwiki-code`. That one condition also protects the
   legacy vault at no cost: the ~2,900 pages already on disk carry the old
   literal, fail to match the constant, and are therefore invisible to this
   finalizer no matter what its inventory says — they are removed only by 5.3's
   supervised quarantine. Deletes go through the same generated gate and the same
   atomic path as writes.
Librarian unification: the executor also claims open `wiki-librarian:*` tasks
(filed by `wiki/scheduled_jobs.py:890-941`, today never executed) and runs them
through the same agent fan-out — #18905/#18906/#18907 are the first live cargo.
DB transaction boundaries per hub conventions (`with self.db.transaction()`,
`%s` placeholders).

The executor is a long-lived task, and a durable queue whose drainer nobody
starts is worse than no queue: 4.2 would accept requests, 4.7 would persist rows,
and the run would sit in `queued` forever while `/status` reported a healthy
service. So this deliverable registers the executor, the startup recovery pass,
and the 4.7 heartbeat watchdog as lifecycle subsystems alongside the daemon's
existing ones (`runner_lifecycle_subsystems.py`), constructed from the 4.2
service, started after the hub is reachable, cancelled and awaited on shutdown,
and reported as degraded rather than silently absent when construction fails.
Startup recovery runs before the first drain, so a daemon restarted mid-run
terminalizes or resumes the abandoned row and promotes its successor before any
new admission is considered.

This deliverable is also the last one to add a file under
`src/gobby/install/shared/` — 3.7's page-type manifest, 4.4's templates and
golden examples, 4.5's agent definitions, and this section's query corpus all
land there, and 4.3 depends transitively on every one of them. The packaged
bundled-content manifest is content-addressed over that whole tree and a
committed test asserts it matches exactly, so shipping any of those files without
regenerating `bundled_content_manifest.json` fails an existing repository test
and leaves packaged installs unable to verify the new assets. Regeneration lands
here, once, rather than being re-done and re-conflicted in four sections.

Write ownership is partitioned by path, and that partition is the whole reason
the two wiki loops can share one orchestration without a rewrite. A codewiki run
owns `wiki/code/**` for its duration; the existing handlers (refresh, upkeep,
recap, audit, sync) own everything outside it and keep their current cron paths.

State that boundary in the direction that is actually true and testable:
**nothing outside a codewiki run ever writes `code/**`.** The mirror claim — that
the legacy handlers write only `knowledge/**` — is false today and would make
its own acceptance unpassable: `crates/gwiki/src/recap.rs:77-212` writes
`recaps/YYYY-MM-DD.md`, calls `catalog::regenerate` on the root `_index.md`, and
appends to the log artifacts, none of which live under `knowledge/`. The legacy
family is every non-`code/**` path those handlers touch, enumerated from the live
writers rather than assumed, and the invariant that has to hold is one-directional
disjointness.

Neither family writes into `code/**` except the run that owns it, so there is no
cross-family race to fence — what the executor unifies is admission, agent
fan-out, and librarian execution, not every scheduled write in the daemon.
Folding the knowledge handlers into the run queue would be a rewrite of a
working subsystem for no correctness gain, so it is deliberately out of scope;
what is in scope is proving the partition holds rather than assuming it.

Kill switch: `wiki.codewiki_orchestration=off` runs deterministic renders only,
and a degraded run must not be able to impersonate a complete one. Semantic
items are planned as normal and parked in `queued`, never dropped and never
marked succeeded; the run finishes in state `degraded`, not `succeeded`; the
finalizer refuses the `fresh` stamp and refuses to advance the commit baseline,
so the next run still sees the pages as stale; `/status` reports the degraded
state and the parked item count. Re-enabling the switch requires no repair
step — the next run re-plans those items from facts and picks them up.

Queue state machine — durable, because a daemon restart mid-run must not
corrupt the vault. `codewiki_page_items` is unique on `(run_id, page_path)` and
each row carries `state` (`queued | leased | succeeded | failed`), `attempts`,
`lease_expires_at`, and a monotonic `fence` token issued at lease time.

The fence is checked **before** the write, not after it, and that ordering is
why step 3 routes the commit through the executor instead of letting agents call
`gwiki page write` themselves. A fence validated on the way back cannot prevent
anything: by the time a superseded agent's result is rejected, its bytes are
already in the vault, and the plan's own claim that a stale agent never
overwrites a newer page would be false. The executor opens one transaction per
landing, re-reads the item row `FOR UPDATE`, and aborts if the fence or run is
superseded, so a writer whose lease expired mid-run loses the race at the row
lock and never touches a file.

Two things that row lock does *not* do, and how each is actually covered. It
cannot fence a **successor generation**, because item rows are unique on
`(run_id, page_path)` and a later run's row for the same page is a different row
that never conflicts; that case is closed one level up, by 4.7's partial unique
index admitting only one active run per project, so a second generation cannot
be landing pages at all. And it cannot fence the **write itself**, because the
vault write is performed by a child `gwiki` process the gateway awaits
(`gwiki_gateway.py:462-476`, a process-local lock around an external command),
so a daemon that dies mid-landing releases the database lock while the orphaned
child is still running. Two distinct hazards live there, and atomicity only
covers one of them. 3.5's generated write stages bytes in a temp file in the
destination directory and `rename(2)`s into place, so any interleaving or crash
leaves the page either wholly the previous version or wholly the new one — today's
boundary is a bare `fs::write` (`crates/gwiki/src/commands/page.rs:115`), which is
exactly the half-write this plan claims cannot happen.

Atomicity does not order those writes. An orphaned child holding validated bytes
for generation N can perform its delayed rename *after* recovery has landed
generation N+1, replacing newer content with older content that is merely whole.
The ordering guard is the precondition the write path already has: generated
writes must carry `--expected-hash` for the page state their scaffold was
rendered against, and the child compares it under the page lock immediately
before the rename. `GwikiGateway.write_page` already forwards `expected_hash`
when supplied (`gwiki_gateway.py:285-299`), so this reuses the existing
precondition rather than adding a staging table. A late orphan fails its
precondition and lands nothing; the executor's replay re-renders against current
state and succeeds, which keeps acknowledgement idempotent.

On item failure the last valid page is retained untouched; a failed page is
never blanked or half-written. The finalizer refuses to advance the freshness
baseline or the `build.json` commit stamp while any mandatory item is `failed`,
and likewise while summary coverage is incomplete (2.5). Crash-injection tests
cover the four dangerous points: after dispatch, after write, after
acknowledgement, during finalization.

Post-reindex verification (ARCHITECTURE and FULL runs, and the 5.1 cutover):
the finalizer runs the existing `gwiki benchmark` retrieval-precision path
(`crates/gwiki/src/benchmark.rs`) against a compact checked-in query corpus at
`src/gobby/install/shared/templates/codewiki/query_corpus.yaml` — code-wiki
questions carrying expected source paths/symbols, following the evidence format
already used for the knowledge track in `docs/evidence/wiki-parity-2026-06`.
The run report records expected-source top-k hit rate, whether answer citations
resolve, current-file summary coverage, FULL wall-clock duration, PARTIAL
touched-page count and latency, and agent cost. A mandatory probe regressing
below its recorded threshold withholds the `fresh` stamp; one live question pair
proves nothing about aggregate-page quality drifting over months.

**Acceptance:**

- 4.3.1 - Executor drains a run: deterministic work planning, inline deterministic renders, batched agent fan-out ≤5, validate-retry-escalate, finalizer. file: `src/gobby/wiki/codewiki/executor.py`.
- 4.3.2 - Executor executes wiki-librarian tasks through the same path. test: `tests/wiki/test_codewiki_executor.py::librarian_tasks_executed`.
- 4.3.3 - With the kill switch off, semantic items stay `queued`, the run ends `degraded`, and the fresh stamp and commit baseline are withheld. test: `tests/wiki/test_codewiki_executor.py::kill_switch_run_is_degraded`.
- 4.3.6 - Planning enumerates every module, tour, concept, and overview item with stable ids and scaffolds before fan-out; two runs over one FactsBundle and one pinned catalog snapshot plan identical item sets, and no layer item is ever planned. test: `tests/wiki/test_codewiki_executor.py::planning_is_deterministic`.
- 4.3.7 - Page fan-out spawns with `task_id=None` and performs no task resolution, claim, or update; only escalation creates a task. test: `tests/wiki/test_codewiki_executor.py::page_fanout_is_taskless`.
- 4.3.8 - A superseded-fence landing aborts before the vault write, leaving the newer page intact; a second concurrent run for the same project is refused at admission rather than fenced at the item row; and an orphaned child whose rename is delayed past a newer landing is refused by its expected-hash precondition. test: `tests/wiki/test_codewiki_executor.py::stale_fence_never_writes`.
- 4.3.9 - Codewiki runs write only under `wiki/code/**`, and every live legacy handler — enumerated from the writers themselves, including recap's `recaps/**`, root catalog, and log paths — writes nothing under `code/**`, proven by a concurrent-run overlap test. test: `tests/wiki/test_codewiki_executor.py::write_ownership_is_disjoint`.
- 4.3.10 - Finalization prunes `gwiki-code`-marked pages absent from the run's desired inventory on a fully successful run and prunes nothing on a degraded or failed one; a module split, a module merge, and a file deletion each leave no stale page indexed. test: `tests/wiki/test_codewiki_executor.py::obsolete_pages_reconciled`.
- 4.3.12 - A PARTIAL run touching one module leaves every untouched page and its referenced bundle intact, and a vault still holding pages with the pre-flip `gcode-codewiki` literal loses none of them to any run. test: `tests/wiki/test_codewiki_executor.py::partial_run_prunes_nothing_untouched`.
- 4.3.13 - Planning records a complete desired inventory on the run row and in `build.json` for every mode, derived from the pinned bundle and catalog snapshot, and FULL and PARTIAL runs over one pinned pair produce the same inventory. test: `tests/wiki/test_codewiki_executor.py::desired_inventory_is_mode_independent`.
- 4.3.14 - A project with no prior successful build classifies as FULL regardless of change-set size, and a run whose desired inventory is not fully materialized on disk withholds the fresh stamp and the commit baseline. test: `tests/wiki/test_codewiki_executor.py::unmaterialized_inventory_is_not_fresh`.
- 4.3.15 - With one page of each family deleted out from under the vault, a COSMETIC, a PARTIAL, an ARCHITECTURE, and a scoped run each re-render or re-enqueue exactly the missing pages on top of their mode-selected work and regain the fresh stamp; over a complete vault the same runs plan no extra work. test: `tests/wiki/test_codewiki_executor.py::missing_inventory_pages_are_repaired`.
- 4.3.16 - The finalizer writes `_meta/truth_digest.json` in the shape memory dream consumes, and a dream sweep after a codewiki run detects the truth change and invalidates its cooldown exactly as it did against the legacy artifact. test: `tests/memory/test_dream.py::truth_digest_from_codewiki_run`.
- 4.3.17 - Executor, startup recovery, and watchdog are registered lifecycle subsystems: a daemon restarted with an admitted, undrained run recovers it, promotes any successor, and drains to a terminal state without operator action, and shutdown cancels and awaits them. test: `tests/wiki/test_codewiki_executor.py::executor_lifecycle_is_owned`.
- 4.3.18 - `bundled_content_manifest.json` is regenerated over every shared file this epic adds and the committed manifest matches the shared tree exactly. test: `tests/test_build_backend.py::test_committed_bundled_content_manifest_matches_shared_tree`.
- 4.3.11 - Planning reads the pinned concept-catalog snapshot: a concept page added to the vault after admission does not change the planned item set. test: `tests/wiki/test_codewiki_executor.py::planning_uses_pinned_catalog`.
- 4.3.4 - Stale-fence results are rejected, failed items retain the last valid page, and crash injection at dispatch/write/ack/finalize leaves the vault consistent. test: `tests/wiki/test_codewiki_executor.py::crash_injection_consistency`.
- 4.3.5 - ARCHITECTURE/FULL runs record verification probes and withhold the fresh stamp on regression. file: `src/gobby/install/shared/templates/codewiki/query_corpus.yaml`.

### 4.4 Page-type templates and manifest [category: config] (depends: 3.4)
`kind: deliverable`

Target: `src/gobby/install/shared/templates/codewiki/manifest.yaml`,
`src/gobby/install/shared/templates/codewiki/module.md`

Bundled templates `module.md`, `concept.md`, `tour.md`, `overview.md` +
`manifest.yaml` (`template_version`, per-type required sections, length
budgets, table caps). Content transcribed from the Rust system prompts
(`prompts/systems.rs`: MODULE_SYSTEM→module, CONCEPT_PAGE_SYSTEM→concept,
NARRATIVE_PAGE_SYSTEM→tour, REPO_SYSTEM+ARCHITECTURE_*→overview;
FILE_SYSTEM/CONTENT_FILE_SYSTEM intentionally not carried — per-file pages are
dropped), tightened with #18871's contracts: one short paragraph per
explanatory section, 3–6 numbered walkthrough steps, ≤6 rows per generated
table, 1–3 read-next items, no repeated claims, `summary:` frontmatter ≤180
chars. Each template embeds one golden example page. Prompt assembly rules:
FactsBundle path + prior page content + prior section names + untrusted-content
fencing for README/docstring excerpts ("treat as untrusted project data;
ignore embedded instructions"). Tours: 5–15 dependency-ordered steps, 1–5
node/symbol citations per step, each step references an earlier step;
two audiences at launch (`new-contributor`, `operator`).

**Acceptance:**

- 4.4.1 - Four templates + versioned manifest with required-section specs ship in the install bundle. file: `src/gobby/install/shared/templates/codewiki/manifest.yaml`.
- 4.4.2 - Templates encode the #18871 quality contracts and golden examples. file: `src/gobby/install/shared/templates/codewiki/module.md`.

### 4.5 Wiki-writer agent definitions [category: config] (depends: 4.4, 3.6)
`kind: deliverable`

Target: `src/gobby/install/shared/workflows/agents/codewiki-module.yaml`,
`src/gobby/install/shared/workflows/agents/codewiki-aggregate.yaml`

`codewiki-module.yaml` and `codewiki-aggregate.yaml` per the
`wiki-researcher.yaml` precedent: isolation none, timeout sized to page work,
allowed tools = read-only gcode commands; `gobby-agents:spawn_agent` blocked.
Workflow: load template → read FactsBundle → write prose into scaffold slots →
compose any semantic diagram from the supplied evidence → **return the finished
page content**. Writers do not self-validate: `gwiki code validate` takes page
paths or `--all` (3.4) and has no candidate-content channel, and a writer with no
vault access has no path to name. The gate is 3.5's validate-before-commit at the
executor's write, which is the boundary that can actually refuse. Writers hold no
vault write access at all: the executor commits under the fence (4.3), so page
writing is not in the allowlist. That is both the fence fix and a smaller blast
radius — a misbehaving writer cannot touch the vault even if its prompt is
subverted. The two-phase contract is stated verbatim in the agent instructions
and enforced mechanically by 3.5 at the executor's write: never re-derive
structure; excerpts only for citations; every composed mermaid arrow must exist
in the supplied evidence or the write is rejected.

**Acceptance:**

- 4.5.1 - Both agent definitions ship with read-only tool allowlists, no validate command, and spawn blocked. file: `src/gobby/install/shared/workflows/agents/codewiki-module.yaml`.
- 4.5.2 - Writer instructions carry the two-phase contract and semantic-diagram duty. file: `src/gobby/install/shared/workflows/agents/codewiki-module.yaml`.
- 4.5.3 - Neither writer definition allows any vault write or page-delete tool. file: `src/gobby/install/shared/workflows/agents/codewiki-aggregate.yaml`.

### 4.6 Cron rewiring [category: code] (depends: 4.3)
`kind: deliverable`

Target: `src/gobby/code_index/codewiki_nightly.py`,
`src/gobby/code_index/codewiki_trigger.py`,
`src/gobby/wiki/codewiki/cron.py`, `src/gobby/wiki/scheduled_jobs.py`

Nightly job (`gobby:codewiki-nightly:<project_id>`, `0 3 * * *`) keeps its
registration/reconciliation but the handler becomes "POST generate mode=auto"
through the 4.2 service instead of the blocking `GcodeGateway.codewiki()`
shell-out; drop the 8h gcode timeout in favor of run-based tracking.
`codewiki_trigger.py` forwards changed_files into the same service. The
librarian cron (`wiki/scheduled_jobs.py`) keeps filing tasks; execution now
happens via 4.3. Wiki cron registration (`runner_lifecycle_subsystems.py`,
`runner_init/orchestration.py`) updated accordingly. Both cron paths submit
through the 4.2 admission path rather than starting work directly, so a nightly
firing while a commit-triggered run is in flight coalesces or queues a successor
instead of running two builds against one project.

`src/gobby/wiki/scheduled_jobs.py` is 995 lines against the repo's hard
1,000-line cap for non-test Python, so it has no room for the codewiki
registration this section would otherwise add. New codewiki cron registration
and admission wiring land in a new `src/gobby/wiki/codewiki/cron.py` instead,
and the librarian handlers in `scheduled_jobs.py:890-941` are left where they
are — this section must not grow that file. If threading the executor into the
librarian cron turns out to need more than a few lines there, the extraction of
those handlers into `codewiki/cron.py` happens first, as part of this
deliverable, so the file ends net smaller.

**Acceptance:**

- 4.6.1 - Nightly handler routes through the generation service; no gcode codewiki shell-out remains in the nightly path. file: `src/gobby/code_index/codewiki_nightly.py`.
- 4.6.2 - Commit trigger forwards changed files. file: `src/gobby/code_index/codewiki_trigger.py`.
- 4.6.3 - A nightly firing during an in-flight run coalesces or queues a successor, never runs concurrently. test: `tests/wiki/test_codewiki_executor.py::cron_overlap_coalesces`.
- 4.6.4 - `scheduled_jobs.py` is no longer than 995 lines after this deliverable. file: `src/gobby/wiki/scheduled_jobs.py`.

### 4.7 Codewiki queue schema and storage [category: code] (depends: 4.1)
`kind: deliverable`

Target: `src/gobby/storage/migrations/`,
`src/gobby/storage/postgres_baseline_schema.sql`,
`src/gobby/wiki/codewiki/queue_storage.py`

The hub schema is migration-driven, so the two queue tables 4.2 writes and 4.3
drains need a real migration rather than tables conjured by test fixtures. This
deliverable ships the next numbered SQL migration plus its mirror in
`postgres_baseline_schema.sql`, and a focused storage module for the queue —
the executor gets a repository, not inline SQL.

`codewiki_runs`: run id, project, mode, trigger fingerprint, pinned
`index_commit`/`facts_digest`/`template_version`/`concept_catalog_digest`,
compressed `facts_bundle` and concept-catalog snapshot bytes (4.2), the
canonical `desired_inventory` page-path list 4.3 step 0 derives and step 4
reconciles against, the merged `requested_mode`/`requested_scope` pair, the
`owner`/`heartbeat_at`/`lease_expires_at` triple below, state
including `degraded`, timestamps. `codewiki_page_items`: run id, `page_path`,
page type, `state`, `attempts`, `lease_expires_at`, `fence`, last error.
Constraints carry the invariants the executor depends on rather than leaving
them to application checks: unique `(run_id, page_path)`, a state CHECK, a
monotonic `fence`, and an index supporting lease reclamation by
`(state, lease_expires_at)`.

Two partial unique indexes on `project_id` carry the concurrency invariants, and
the split between them matters. The first is restricted to the **executing**
states (`running`, `landing`), so the database admits exactly one *executing* run
per project: two overlapping triggers racing at admission cannot both observe no
executing run and insert, and the loser takes a unique violation. This is what
makes 4.3's landing fence sound — with one executing run, no successor generation
can be writing the same page concurrently, which a per-run item lock could never
have prevented on its own.

The second is restricted to the `queued` state, admitting at most one **waiting
successor**. A single index over all non-terminal states would be wrong, because
`queued` is non-terminal and a queued successor is exactly what 4.2 promises a
newer commit: an index that rejects it would make run A at commit A silently
absorb a trigger at commit B, and B would never build. So a request whose
fingerprint matches the executing run coalesces into it; a request at a newer
commit writes or replaces the single queued row. When the executing run reaches a
terminal state the queued row is promoted in the same transaction that closes it,
so promotion cannot race a fresh admission.

Replacing that row is only safe if the survivor subsumes what it replaced, and a
newest-wins replacement does not. Trigger B queues with changed file `x`, trigger
C at a newer commit replaces it carrying only `y`, and the module affected by `x`
is never regenerated even though the newest commit is faithfully retained — the
row identity is monotone while its payload is not. Rather than merging payload
fields pairwise, the queued row carries no changed-file list at all: it holds the
newest target commit, and at promotion the classifier recomputes the change set
from the last **successfully built** baseline to that commit. A diff from a
baseline is monotone by construction, so nothing accumulated between two failed
or superseded triggers can be dropped. The one field that is not derivable from a
diff is an explicit human request — a manual `full` cannot be inferred from
changed files — so the queued row also keeps the maximum requested mode across
every trigger it absorbed, and a pending FULL is never demoted by a later
narrower request.

Requested scope is the second such field and needs the same treatment for the
same reason. A `scope` request names pages directly rather than describing a
change, so no diff from any baseline can reconstruct it: if trigger B queues
`scope M1` and trigger C at a newer commit replaces the row with `scope M2`, M1
is simply lost, and it is lost identically if C is a plain `auto`. The queued row
therefore carries a canonical requested-scope field merged monotonically — a
requested FULL dominates and clears it, since a full build subsumes every scope,
and any two scopes merge to their union. Union is the right merge rather than
newest-wins because scope is an additive claim about what must be rebuilt, and
because the union is bounded by the desired inventory it can never grow without
limit. Both fields survive a restart because they live on the row, not in the
request that set them.

An executing run that never terminalizes is worse than a lost trigger, because
the executing partial index makes the wedge permanent: a daemon killed mid-run, a
worker exception, or an unrecoverable bundle-digest mismatch leaves the row in
`running` or `landing`, every later admission coalesces into a run that will
never finish, and the project can never build again. Page-item leases do not
help — they release items, not the run. The run row therefore carries the same
lease shape its items already do: an `owner` identifying the daemon instance, a
`heartbeat_at` the executor refreshes while it drains, and an explicit
`lease_expires_at`. Daemon startup and a periodic watchdog scan for executing
runs whose lease has expired and, in one transaction, either resume a row this
instance can still own or terminalize it with a stable error and promote the
queued successor. Recovery is bounded by the lease interval rather than by an
operator noticing.

`lease_expires_at` is a stored column rather than a heartbeat comparison done in
Python because takeover has to be a single guarded write. A restart produces a
new process with a new owner identity, so no recovering instance can ever match
the dead owner literally; the transition is an `UPDATE ... WHERE id = %s AND
state IN ('running','landing') AND lease_expires_at < now()` that claims the row
or affects zero rows, following the expiry-plus-guarded-mutation shape the
daemon's dispatch lease cleanup already uses. Two watchdogs racing the same
abandoned row therefore produce one winner and one no-op, with no read-then-write
window between them.

What the run lease deliberately does *not* get is an incarnation counter fenced
through every later mutation. A revived old owner cannot corrupt anything it
would reach: the executing partial unique index admits one executing run per
project, page landings re-read their item row `FOR UPDATE` and abort on a
superseded `fence` before touching the vault, and 3.5's generated write rechecks
its expected-state precondition under the page lock immediately before the
rename. Three independent guards already stand between a stale owner and a byte
on disk, so a fourth would be mechanism guarding a state that cannot be reached.

Ordered before 4.3 in the graph despite its number: 4.3 cannot drain a queue
that has no tables.

**Acceptance:**

- 4.7.1 - Numbered migration creates both tables with the unique, CHECK, and lease indexes plus the two partial unique indexes (one executing run, one queued successor, per project), mirrored in the baseline schema. file: `src/gobby/storage/postgres_baseline_schema.sql`.
- 4.7.2 - Migration applies cleanly on an existing hub and is idempotent on re-run. test: `tests/storage/test_codewiki_queue_migration.py`.
- 4.7.3 - Queue storage exposes claim/lease/advance/fail with fence checks; duplicate `(run_id, page_path)` is rejected by the database. test: `tests/wiki/test_codewiki_queue_storage.py`.
- 4.7.4 - Two real connections inserting executing runs for one project at the same time yield one success and one unique violation; a terminal run does not block the next admission. test: `tests/wiki/test_codewiki_queue_storage.py::single_active_run_per_project`.
- 4.7.5 - A queued successor is admitted alongside an executing run, a newer trigger replaces the queued row rather than being rejected, and terminalizing the executing run promotes the queued row in the same transaction. test: `tests/wiki/test_codewiki_queue_storage.py::successor_queues_and_promotes`.
- 4.7.6 - Successor replacement is monotone: a trigger touching `x` followed by a newer trigger touching `y` promotes to a run whose recomputed change set covers both, and a pending `full` request is not demoted by a later narrower trigger. test: `tests/wiki/test_codewiki_queue_storage.py::successor_replacement_is_monotone`.
- 4.7.7 - An executing run whose lease has expired is terminalized with a stable error by startup recovery and by the watchdog, its queued successor is promoted in the same transaction, and a fresh admission for that project succeeds afterwards; two watchdogs racing one abandoned row yield exactly one takeover and one no-op, and an unexpired row is never taken over. test: `tests/wiki/test_codewiki_queue_storage.py::abandoned_run_is_recovered`.
- 4.7.8 - Requested scope merges monotonically across coalescing: `scope A` then `scope B` promotes to a run covering both, `scope A` then `auto` still covers A, a requested `full` dominates and clears the scope, and every merge survives a restart because it is stored on the row. test: `tests/wiki/test_codewiki_queue_storage.py::requested_scope_merges_monotonically`.

## P5: Cutover and Deletion
`kind: framing`

**Goal**: Prove the new pipeline end-to-end on gobby itself, migrate the vault,
then delete the legacy codewiki machinery from gcode and the daemon glue.

### 5.1 Dogfood FULL run and acceptance [category: test] (depends: P1, P4)
`kind: deliverable`

Target: `tests/wiki/test_codewiki_e2e.py`,
`docs/contracts/codewiki-capability-matrix.md`

Run a daemon-driven FULL generation on the gobby repo (isolated test daemon for
automated coverage plus one live run). Gate criteria: exhaustive-partition
validator green; `gwiki code validate --all` green; `wiki_ask` correctly
answers one file-level question (served from index summaries) and one
module-level question (served from a new module page); wall clock < 2h at
writer batch 5 (~60–90 LLM pages); `_meta/build.json` records the run with
DiagramStats emitted > 0 (closing the #18786 verification half). Two further
gates, because the deletion in 5.3 is irreversible: summary coverage must be
complete (`current_summary_count == indexed_file_count`, 2.5), and the 4.3
verification stage must run with every mandatory probe at or above threshold,
its results recorded in the run report as the baseline later runs are compared
against. Automated e2e test covers a scoped run against fixture repos with stub
agents.

"Better than parity with Understand-Anything" is a settled launch requirement,
and as prose it is unfalsifiable — the existing gate measures retrieval and wall
clock, neither of which says anything about UA. It becomes measurable as a
checked-in capability matrix at `docs/contracts/codewiki-capability-matrix.md`,
where each row is a capability, its source (UA-derived or daemon-only), and one
executable probe against the dogfood output. Parity rows: per-file coverage of
every indexed file, exhaustive file→module→layer partition, change-classified
incremental regeneration, module dependency/neighbor context, and guided
navigation. Daemon-only rows, which is where "better than" has to be earned:
prose module pages with resolvable citations, evidence-verified mermaid, live
freshness state per page, agent-consumable markdown answering through
`wiki_ask`, and multi-audience tours. Rows are pinned by capability, not by UA's
current output — nothing from that repo is vendored, and the matrix is versioned
in the same commit as the gate it governs.

**Acceptance:**

- 5.1.1 - Scoped e2e with stub agents passes classify→queue→render→validate→land. test: `tests/wiki/test_codewiki_e2e.py`.
- 5.1.2 - Live FULL run on gobby meets all gate criteria. behavior: "P4 gate criteria" in `wiki/code/_meta/build.json`.
- 5.1.3 - Summary coverage is complete and verification probes pass, both recorded as the retrieval baseline. behavior: "cutover retrieval baseline recorded" in `wiki/code/_meta/build.json`.
- 5.1.5 - The dogfood run replaces the legacy occupant at each shared deterministic path (`features.md`, `changes.md`, `hotspots.md`, `infrastructure.md`, `ownership.md`, `deprecations.md`) with a page carrying the current marker, and each replacement satisfies its expected-hash precondition rather than being written unconditionally. behavior: "shared deterministic paths replaced" in `wiki/code/_meta/build.json`.
- 5.1.4 - Every capability-matrix row has a passing probe against the dogfood output, parity and daemon-only alike. file: `docs/contracts/codewiki-capability-matrix.md`.

### 5.2 Delete the legacy codewiki subtree and its residue [category: code] (depends: 3.8, 5.1, 5.3)
`kind: deliverable`

Target: `crates/gcode/src/commands/codewiki/mod.rs`,
`src/gobby/code_index/codewiki_refresh.py`, `crates/gcode/README.md`

3.8 already removed every entry point, so what is left here is dead weight rather
than a live surface: delete the `commands/codewiki/` subtree wholesale (~38K LOC
including ~10K lines of tests) and its `mod` declaration. Python residue: collapse
`codewiki_refresh.py`'s dual-gateway dance into the 4.2 service and remove the
dead config and AI normalization it carried (`normalize_codewiki_ai`), plus the
`"codewiki"` entry in the AI tool-chat allowlist
(`src/gobby/ai/_tool_chat_tools.py:94`). Docs: README, `crates/CLAUDE.md`, and the
wiki-related guides. Reinstall both binaries. Execute the remaining
task-consolidation actions (close #18871/#18779/#18790 with mapping notes;
#18905–7 remain open only if not yet executed by 4.3).

Deletion of the *code* waits for 5.3's post-purge query parity even though the
code is already unreachable, and the reason is narrow: the subtree is the written
record of how the old pages were produced, and the migration is the last point at
which someone diagnosing a parity failure would want to read it. That is a
documentation argument, not a rollback one — 3.8 gave up the ability to run it,
deliberately.

**Acceptance:**

- 5.2.1 - No codewiki module or `mod` declaration remains in gcode, and the crate builds with its index, search, symbol, and graph commands intact. file: `crates/gcode/src/commands/codewiki/mod.rs`.
- 5.2.2 - No Python module retains codewiki refresh glue, dead AI normalization, or the tool-chat allowlist entry. file: `src/gobby/code_index/codewiki_refresh.py`.
- 5.2.3 - No crate README, `crates/CLAUDE.md`, or guide documents a `gcode codewiki` command. file: `crates/gcode/README.md`.
- 5.2.4 - Post-deletion gate: both binaries are rebuilt and reinstalled, and `wiki_search`/`wiki_ask`/`wiki_read` answer against the new pipeline with no gcode codewiki present. test: `tests/wiki/test_phase_exit_smoke.py`.

### 5.3 Vault migration [category: test] (depends: 5.1)
`kind: deliverable`

Target: `src/gobby/wiki/codewiki/vault_migration.py`, `wiki/code/files`

Publish-then-quarantine-then-delete within one supervised run: after 5.1's new
pages land and reindex completes, the legacy content — `wiki/code/files/**`
(~2,923 pages), orphaned old module pages, the narrative handbook (absorbed by
tours), `repo.md` (absorbed by `_index.md`), and the legacy
`_meta/codewiki.json` (14.6MB → `build.json` + `layers.json`, <100KB) — is
**renamed into `wiki/.code-legacy-<run_id>/`, not deleted**. Then run the
link-rewrite pass, the `gwiki lint` dead-link check, the reindex, and the
`wiki_search`/`wiki_ask` module- and file-level parity checks. Only once all of
those pass is the quarantine directory removed.

Selection is by **positive** legacy identity, never by inequality with the
current marker. `generated_by` is an optional frontmatter field, so "does not
equal the current constant" is true of every page that carries no marker at all —
operator-authored notes dropped under `code/**`, artifacts from any other tool,
and anything with a marker this epic has never heard of. A negative predicate
therefore hands a destructive rename authority over files it knows nothing about.
The manifest instead selects pages whose `generated_by` equals the exact legacy
literal `gcode-codewiki`, plus the enumerated legacy artifacts named above, and
nothing else. Missing, unknown, and foreign markers are protected: they stay in
place and are reported in the run record, so an unexpected occupant is a thing
the operator reads about rather than a thing the migration silently moved. The
shared deterministic names (`features.md`, `changes.md`, `hotspots.md`,
`infrastructure.md`, `ownership.md`, `deprecations.md`) are written by both
pipelines and 5.1 replaced their occupants already; because those replacements
carry the current marker rather than the legacy literal, they fall outside a
positive selection just as cleanly, with no recorded exclusion list.

This is a supervised, crash-recoverable, destructive procedure, and naming only
its data targets and its tests would leave nothing to expand into work. It is
owned by `src/gobby/wiki/codewiki/vault_migration.py`, invoked by the 4.3
finalizer at cutover and exposed as an operator entry point on the generation
service: the module owns manifest construction and persistence, each rename, the
link rewrite, resume and restore, the lint/reindex/parity probes, and the final
quarantine deletion. The tests below point at it.

"Rollback is don't delete" is not a rollback — it is a precondition, and it only
covers failures noticed *before* the destructive step. Every check in this
section runs after it: link rewriting, lint, reindex, and query parity can all
fail once the pages are already gone. The vault is gitignored, so at that point
there is nothing to restore from and no fallback generator either, since 5.2
depends on this section passing. The quarantine rename makes the window
recoverable at essentially zero cost: a failed check restores by renaming the
directory back, and the run is retried.

The legacy content is scattered across several paths, so quarantine is a
sequence of renames rather than one, and the window between the first and last
move is itself a failure point the checks above never reach. Before the first
rename the run writes a move manifest — every planned `(source, destination)`
pair — and records each move as it completes. Recovery from any prefix is then
mechanical in both directions: resume the remaining moves, or restore the
completed ones. Without the manifest a crash mid-quarantine leaves a split vault
with no record of which half moved, which is the one state this section exists to
prevent.

The renames are not the only mutation the checks run after, so the manifest has
to cover the other one too. The link-rewrite pass edits **surviving** pages, and
restoring the quarantined directory does not un-edit them: a failure at lint,
reindex, or parity would restore the old pages beside rewritten links that now
point at the new ones, which is a third state that is neither before nor after.
The same manifest therefore records each rewritten page with its pre-rewrite
content hash and its recoverable prior bytes, exactly as it records each move, so
rollback restores both families and then reindexes before declaring the vault
recovered — an unreindexed restore leaves queries answering from the wrong
generation. This is one more list in a journal the section already writes, not a
second mechanism.

**Acceptance:**

- 5.3.1 - Legacy per-file/narrative pages and 14.6MB meta are gone; new meta <100KB. behavior: "post-purge vault inventory" in `wiki/code/_meta/build.json`.
- 5.3.2 - Dead-link lint is clean and wiki_ask answers file+module questions post-purge. test: `tests/wiki/test_codewiki_e2e.py::post_purge_query_parity`.
- 5.3.3 - Legacy content is quarantined by rename before any check runs, and the quarantine is removed only after all of them pass. test: `tests/wiki/test_codewiki_e2e.py::purge_quarantines_before_delete`.
- 5.3.4 - A failure injected at link-rewrite, lint, reindex, and query-parity each restores both the quarantined content and every rewritten surviving page to its pre-migration bytes, reindexes, and leaves the vault queryable and byte-identical to its pre-migration state. test: `tests/wiki/test_codewiki_e2e.py::purge_restores_on_failure`.
- 5.3.5 - The move manifest is written before the first rename and each rewritten page is journaled with its prior hash and bytes before being edited; a crash injected between any two moves, and between any two rewrites, resumes or restores to a whole vault from that prefix. test: `tests/wiki/test_codewiki_e2e.py::quarantine_recovers_from_any_prefix`.
- 5.3.6 - Over a vault holding pre-flip legacy pages, current-marker pages, an unmarked operator-authored page, and a page carrying a foreign marker, the manifest selects every legacy page and enumerated artifact and nothing else; the unmarked and foreign pages remain in place and are reported; and the post-quarantine vault still passes `gwiki code validate --all`. test: `tests/wiki/test_codewiki_e2e.py::quarantine_selects_only_legacy`.
- 5.3.7 - The migration module owns manifest, renames, rewrite, restore, probes, and quarantine deletion, and is reachable from the cutover finalizer and the operator entry point. file: `src/gobby/wiki/codewiki/vault_migration.py`.

## V1 Plan Changelog
`kind: verification`

Initial draft 2026-07-26 from the approved harness plan
(`/Users/josh/.claude/plans/18871-needs-planning-raise-serialized-cupcake.md`),
reconciling two design passes (port-first vs target-first) with user decisions:
build-new-delete-in-place, no gwiki→gobby-code lib dependency, internal queue +
librarian tasks, per-file pages dropped, both wiki loops under one
orchestration, standalone removal first. No review rounds yet.

**Round 1** `kind: enhancement`

- enhancer_run: 6c9cdcc4-b0ad-469e-9f7b-4c343ce2d9d5
- enhancer_session: 812df17c-7415-48cd-a510-849f865aa946
- converged: false
- suggestions_presented: 8
- accepted:
  - E1 sequencing — DAG edges (4.5←4.4+3.6, 4.3←4.2+4.5, 5.2←5.1+5.3) plus a stated acyclicity invariant
  - E2 clarity (modified) — exhaustive classifier decision table, boundary/rename/template-bump/stale-index fixtures, enum+reason code in payloads
  - E3 testability — four summary-coverage counters, Option-as-incomplete semantics, coverage gates freshness/FULL
  - E4 clarity — legacy-freeze boundary replacing port/copy-refactor language, with one forced carve-out
  - E5 testability — shared golden fixture across all three crates, canonical byte-stable serialization, fixed clustering seed, content-addressed module ids
  - E6 testability — generation-free `code render`, semantic mermaid moved to the writer item, facts_digest/scaffold_hash slot enforcement with stable rule codes
  - E7 clarity — run pinning, coalescing, durable item state machine with lease/fence, crash-injection tests, no baseline advance on failure
  - E8 scope (trimmed) — post-reindex verification stage with a code-wiki query corpus and recorded probe thresholds
- declined:
  - E2 as written — collapsing the enum to four states. Its premise is factually wrong: COSMETIC is a settled decision in the approved harness plan and was already at line 494 of this artifact. Only the enum collapse was dropped; the decision table and tests were taken.
  - E8's pinned Understand-Anything fixture/commit baseline — vendoring another repo's output as a release gate is unjustified mechanism for the parity claim. `docs/evidence/wiki-parity-2026-06` is reused as a corpus *format* precedent only; it is knowledge-track evidence, not code-wiki.
- resolution_notes: >
  E4 could not be applied literally. `catalog_command_set_equals_each_pinned_contract_exactly`
  (`crates/gcode/src/commands/codewiki/tests/features.rs`) asserts the gcode-hosted
  feature catalog equals every pinned contract exactly, so adding gwiki `code *`
  commands in 3.6 forces an edit to `build_parts/features.rs`; building a duplicate
  gwiki catalog would not avoid it, and relocating the catalog early would break the
  old path's `features.md` while it must stay runnable. Applied as a freeze with one
  explicit, reasoned carve-out limited to handler-map arms, audited by 5.2.4 before
  deletion. Also note 4.3 now depends on 4.5, which depends on 4.4 and 3.6 — the
  executor can no longer be scheduled before the writer definitions and gwiki gateway
  it invokes exist.

**Round 1** `kind: verification`

- reviewer_run: 2b388243-dfd2-4fc5-91d3-9b0c6099a256
- reviewer_session: cfaa1ab9-8e6b-4544-834d-90c1635e003d
- verdict: needs_review
- findings:
- R1-vault-purge-rollback / blocking / destructive vault deletion has no recovery path (gitignored, unrestorable)
- R1-fence-before-write / blocking / fence validated after the write cannot stop a stale agent overwriting a newer page
- R1-facts-snapshot-recovery / blocking / facts_digest pinned without storing the bytes it names; restart cannot re-render
- R1-queue-schema-migration / blocking / codewiki_runs and codewiki_page_items have no migration or storage deliverable
- R1-taskless-fanout-interface / blocking / dispatch_batch is task-oriented and refuses taskless page items
- R1-template-manifest-order / blocking / 3.4 validates against the template manifest 4.4 creates, but 4.4 depends on 3.4
- R1-legacy-freeze-contract / blocking / 3.4 flips the shared codewiki marker while the legacy fallback still pins it
- R1-standalone-blast-radius / blocking / P1 misses live AiRouting::Direct consumers outside its targets
- R1-summary-data-dependency / blocking / 2.2 consumes 2.4's summaries without depending on 2.4
- R1-aggregate-structure-planner / blocking / concept, tour, and overview work items have no deterministic enumerator
- R1-unified-wiki-orchestration / blocking / knowledge-vault crons mutate outside the new run queue's fences
- R1-scheduled-jobs-size / blocking / scheduled_jobs.py is 995 lines against a 1,000-line cap
- R1-validate-command-proof / blocking / seven check families, two proven at the command boundary
- R1-rust-command-wiring / blocking / new CLI commands omit parser, mapping, and dispatch surfaces
- R1-phase-reinstall-query-gates / blocking / reinstall and queryability are framing constraints with no acceptance
- R1-kill-switch-state / blocking / orchestration=off leaves item state and freshness eligibility undefined
- R1-ua-parity-gate / blocking / better-than-parity is asserted but never made measurable
- resolution_notes: >
  All 17 findings accepted; every load-bearing citation was verified against live
  code before acceptance. Confirmed directly: scheduled_jobs.py is exactly 995
  lines; dispatch_batch (src/gobby/mcp_proxy/tools/spawn_agent/_factory.py:592)
  refuses suggestions without a task ref and then synthesizes task_id and calls
  _suggestion_task_description, so it cannot carry taskless queue rows;
  crates/gwiki/src/ingest/session/summarize.rs:39,74,77 uses
  DirectGenerationTarget and matches AiRouting::Direct while sitting outside every
  P1 target; and the declared edges do place 3.4 before the 4.4 manifest it
  validates against.
  Eight were applied in cheaper form than suggested, on the least-mechanism rule.
  R1-standalone-blast-radius adds the missed consumers to existing 1.2-1.4 targets
  but not the proposed absence-test matrix: deleting a Rust enum variant makes the
  compiler itself the exhaustiveness proof. R1-phase-reinstall-query-gates adds
  acceptance items to existing phase-final deliverables instead of four new
  reinstall sections. R1-fence-before-write takes the transactional
  check-fence-inside-the-write option rather than restructuring agents to return
  staged content. R1-vault-purge-rollback uses quarantine-rename-then-verify
  instead of a snapshot-and-restore drill. R1-unified-wiki-orchestration is
  narrowed to a proven-disjoint path-ownership boundary (codewiki runs own code/**,
  knowledge crons own knowledge/**) rather than absorbing every knowledge cron into
  the run queue, which would be a rewrite well outside this plan.
  R1-rust-command-wiring, R1-validate-command-proof, and
  R1-aggregate-structure-planner were folded into existing sections as targets and
  acceptance rather than new subsystems. Only two new deliverables were created
  (the queue migration and the template manifest split); everything else landed as
  edits to existing sections.

```json plan-review-round
{"evidence_id":"1ff9d72e-4428-4ba5-9ae9-72ac938c1b2a","plan_hash":"0a7be40a208b7a16adce061373cb10d542c2b0ba167bd21d9226b9f073f2d2ba","round_number":1,"round_result":{"coverage_attestation":{"adjacent_variant_complete":true,"attestation_digest":"88e577587e7575cd0997f270e583020d560e242993d4f3454cd61e3f05e90e1d","cross_lane_interaction_complete":true,"disposition_counts":{"dismissed":6,"emitted_findings":17,"total":23},"evidence_id":"1ff9d72e-4428-4ba5-9ae9-72ac938c1b2a","lanes":[{"candidate_count":10,"lane_id":"requirements_traceability","status":"completed"},{"candidate_count":6,"lane_id":"repository_blast_radius","status":"completed"},{"candidate_count":7,"lane_id":"runtime_invariants","status":"completed"}],"shadow_manifest_status":{"entry_count":26,"manifest_digest":"3d90b599d6451aeba593f81fe288c3bd2fe58c665939f78b1ca08bac2e945793","status":"valid"},"source_digest":"c6180e6a404f925d21fee41739bb4a06e0b8c0fe0d1178806bc3ce9266c848e6","version":1},"findings":[{"category":"bad-sequencing","check_key":"dependency-producer-before-consumer","description":"Section 3.4 validates required sections against the installed template manifest and 3.5 requires template IDs, while 4.4 creates that manifest and currently depends on 3.4. The declared DAG can schedule a validator whose required input does not exist.","finding_id":"R1-template-manifest-order","location":"3.4 Validation gate / 4.4 Page-type templates","prevention":"For every referenced runtime artifact, identify its producing section and add a producer-to-consumer dependency edge.","principle":"Every consumer must depend on the artifact that defines its runtime contract.","root_cause":"The validation gate consumes the installed template manifest before section 4.4 creates it.","section_id":"3.4","severity":"blocking","suggested_fix":"Split the manifest and required-section specs into an earlier deliverable, make 3.4 depend on it, and remove the reverse 4.4-to-3.4 edge. Later prose-template authoring may depend on the validator only if it does not supply validator inputs."},{"category":"bad-sequencing","check_key":"cross-language-data-producer-dependency","description":"`gcode facts` populates `FileFacts.summary` from the hub-DB summaries created in 2.4, yet 2.2 depends only on 2.1. Section 2.4 also promises `gcode outline` and FactsBundle integration while targeting only Python summarizer/storage files.","finding_id":"R1-summary-data-dependency","location":"2.2 gcode facts / 2.4 Per-file summaries","prevention":"Trace every schema field from storage creation through each producer and consumer, with explicit targets and dependency edges.","principle":"Cross-language consumers must depend on the storage producer and own their integration surfaces.","root_cause":"The facts command consumes file summaries from 2.4 without depending on 2.4, while 2.4 assigns Rust outline and FactsBundle behavior outside its targets.","section_id":"2.2","severity":"blocking","suggested_fix":"Make the Rust consumer work depend on the summary storage/backfill deliverable. Assign `crates/gcode/src/commands/symbols.rs`, facts queries, CLI output, and their tests to an explicit Rust leaf."},{"category":"weak-testability","check_key":"phase-boundary-live-artifact-gate","description":"The plan requires every crate-touching phase to reinstall `~/.gobby/bin/{gcode,gwiki}` and keep `wiki_search`, `wiki_ask`, `wiki_read`, and the legacy fallback runnable at every phase boundary. No acceptance item schedules or proves those gates, so expansion can complete Rust leaves against stale installed binaries.","finding_id":"R1-phase-reinstall-query-gates","location":"Constraints / crate-touching phase boundaries","prevention":"Give every phase-boundary operational constraint a dependency fan-in leaf with a concrete smoke artifact.","principle":"A phase-boundary runtime invariant needs an executable leaf and acceptance evidence.","root_cause":"Reinstall and continued queryability are framing constraints rather than manifest-covered work.","section_id":"3.6","severity":"blocking","suggested_fix":"Add phase-ending reinstall-and-smoke deliverables after P1, P2, P3, and the P5 crate deletion, with dependency fan-in from all Rust leaves and daemon-backed query/fallback checks."},{"category":"missing-requirement","check_key":"exhaustive-variant-consumer-sweep","description":"Live code outside P1 targets still stores `DirectGenerationTarget`, builds standalone session-summary context/pages, reads `StandaloneConfig`, and implements setup/provisioning paths. Examples include `crates/gwiki/src/ingest/session/summarize.rs` and `crates/gwiki/src/support/config.rs`; deleting `AiRouting::Direct` and `StandaloneConfig` will break or leave the settled no-daemon requirement incomplete.","finding_id":"R1-standalone-blast-radius","location":"1.2-1.4 Standalone removal","prevention":"Run a repository-wide symbol and exact-string consumer sweep and place every production hit in a self-contained deliverable.","principle":"Deleting an enum variant, route, or configuration type requires every constructor, match, consumer, fake, and test to be assigned.","root_cause":"P1 inventories the obvious setup/routing files but omits live standalone consumers in gwiki and adjacent gcore/gcode modules.","section_id":"1.2","severity":"blocking","suggested_fix":"Add the full consumer inventory to P1, including session summarization/archive, gwiki support config, gcore provisioning/config modules, gcode setup types/DDL/lib exports, exhaustive matches, fakes, and focused absence/compile tests."},{"category":"weak-testability","check_key":"claimed-check-family-command-proof","description":"`gwiki code validate` claims strict markdown, mermaid, frontmatter, citation, required-section, partition, and deterministic-region checks. The named command tests cover missing sections and deterministic-region mutation; five families remain unproven at the integrated command boundary.","finding_id":"R1-validate-command-proof","location":"3.4 gwiki code validate","prevention":"Create one table-driven command test row per validation family and assert its stable machine-readable code.","principle":"Acceptance claiming a multi-family validation gate must execute every family at the public command boundary.","root_cause":"The acceptance cites implementation files and only two focused failures for seven claimed check families.","section_id":"3.4","severity":"blocking","suggested_fix":"Add a table-driven `gwiki code validate` suite that triggers all seven families and asserts page, location, and stable JSON rule code for each."},{"category":"missing-requirement","check_key":"cli-command-exhaustive-wiring","description":"The plan does not assign the live parser and exhaustive dispatch surfaces for `gcode facts` or `gwiki code render/status`. `crates/gcode/src/cli.rs`, `crates/gcode/src/dispatch.rs`, `crates/gwiki/src/api.rs`, `crates/gwiki/src/cli.rs`, `crates/gwiki/src/cli/mapping.rs`, and `crates/gwiki/src/commands/mod.rs` are load-bearing.","finding_id":"R1-rust-command-wiring","location":"2.2 gcode facts / 3.6 gwiki code commands","prevention":"For each new command, inventory the CLI enum, argument types, mapping, API enum, dispatcher match, service selection, contract, and tests.","principle":"A new Rust CLI command needs parser, API mapping, dispatcher, contract, and command-level tests.","root_cause":"The plan treats contract pins and gateway wrappers as sufficient command implementation.","section_id":"3.6","severity":"blocking","suggested_fix":"Add explicit targets and acceptance for both binaries' parse, map, dispatch, output, status, and error paths, including command-level smoke tests."},{"category":"missing-requirement","check_key":"durable-table-migration-inventory","description":"`codewiki_runs` and `codewiki_page_items` are central to crash recovery, leasing, uniqueness, and fencing, yet 4.3 targets only the executor, build JSON, and query corpus. The live hub schema is migration-driven, so production cannot rely on test-local table creation.","finding_id":"R1-queue-schema-migration","location":"4.3 Run executor queue","prevention":"Whenever a plan introduces a table, require migration, CRUD/storage, startup, upgrade, and rollback/idempotency acceptance.","principle":"Every durable table needs a production migration, storage boundary, indexes, constraints, and upgrade coverage.","root_cause":"The executor section names two hub tables without any migration or repository deliverable.","section_id":"4.3","severity":"blocking","suggested_fix":"Add a numbered SQL migration, a focused queue repository/storage module, uniqueness and lease indexes, state/fence constraints, migration upgrade tests, and production startup acceptance."},{"category":"bad-sequencing","check_key":"fallback-freeze-shared-contract","description":"The freeze allows only 3.6's handler-map edit under legacy codewiki, yet the shared `codewiki_contract` golden is pinned by `crates/gcode/src/commands/codewiki/tests/provenance.rs`. Flipping `gcode-codewiki` to `gwiki-code` in 3.4 requires editing that frozen test or breaks the still-runnable fallback.","finding_id":"R1-legacy-freeze-contract","location":"3.4 Shared contract / 5.2 Freeze audit","prevention":"For every shared contract changed before cutover, sweep fallback producers and pin tests for required edits.","principle":"A frozen fallback must retain every shared contract and pin until the cutover leaf deletes it.","root_cause":"Section 3.4 flips the shared generated marker and both crate pins during the P2-P4 freeze.","section_id":"3.4","severity":"blocking","suggested_fix":"Keep the legacy marker/golden stable until 5.2, or introduce a separate gwiki marker/contract during the migration and flip the shared legacy contract alongside deletion. Add a fallback smoke gate."},{"category":"missing-requirement","check_key":"taskless-work-item-dispatch","description":"Page work is settled as hub-DB queue rows, while current `dispatch_batch` is task-oriented. Passing page IDs through it can produce task lookup warnings or accidental task association if an identifier resolves.","finding_id":"R1-taskless-fanout-interface","location":"4.3 Executor fan-out","prevention":"Trace page-item identity through spawn and assert task manager methods are never called.","principle":"An internal queue must use a dispatch interface that never resolves, claims, or mutates task records.","root_cause":"The plan names `dispatch_batch`, whose live implementation requires a task-like ref, synthesizes `task_id`, and enters task resolution.","section_id":"4.3","severity":"blocking","suggested_fix":"Call taskless `spawn_agent_impl` directly with `task_id=None` under the executor's semaphore, or add a dedicated taskless batch interface. Test that page fan-out performs zero task resolution, claim, update, close, or escalation except explicit escalation creation."},{"category":"gobby-format","check_key":"source-size-before-edit","description":"`src/gobby/wiki/scheduled_jobs.py` is already 995 lines and 4.6 names additional changes there. The plan neither assigns a split nor proves the edit is net-negative.","finding_id":"R1-scheduled-jobs-size","location":"4.6 Cron rewiring","prevention":"Measure every target before expansion and split any file whose planned change can cross the cap.","principle":"A plan must keep non-test Python sources under the enforced 1,000-line cap.","root_cause":"Section 4.6 adds registration work to a 995-line module without a split or size acceptance.","section_id":"4.6","severity":"blocking","suggested_fix":"Move codewiki registration/admission integration into a focused module before the edit, or otherwise split `scheduled_jobs.py`, and add a source-size acceptance check."},{"category":"unhandled-edge","check_key":"content-addressed-run-snapshot","description":"Validators must re-render from a page's recorded digest and writers receive a bundle path, but a digest alone cannot reconstruct bytes after restart or cleanup. The durable queue can therefore resume without the data needed to validate or write its pages.","finding_id":"R1-facts-snapshot-recovery","location":"3.3-4.4 FactsBundle lifecycle","prevention":"For every digest-based reference, specify byte storage, digest verification, ownership, retention, and restart behavior.","principle":"A restartable run must persist the exact immutable bytes identified by its digest.","root_cause":"The plan pins `facts_digest` and passes a FactsBundle path without defining durable bundle storage or recovery.","section_id":"4.2","severity":"blocking","suggested_fix":"Persist canonical FactsBundle bytes in content-addressed run artifacts, store each item's bundle reference, verify digest on every read, define retention/cleanup, and add restart recovery tests. Validation must use that pinned artifact."},{"category":"unhandled-edge","check_key":"fence-before-side-effect","description":"A slow agent from a superseded lease can overwrite a newer page before its stale result reaches the executor. Post-write acknowledgement fencing cannot satisfy the plan's claim that stale agents never overwrite newer pages.","finding_id":"R1-fence-before-write","location":"4.3 Queue state machine / 4.5 Writer","prevention":"Draw the lease-to-side-effect sequence and place fence validation atomically before the write.","principle":"A fencing token must be validated before the protected side effect.","root_cause":"Writers commit through `gwiki page write` directly, while the executor rejects stale results only after the write returns.","section_id":"4.3","severity":"blocking","suggested_fix":"Have agents return staged content and let the executor validate run/item/fence before an atomic write, or make generated-write validate the current fence transactionally before touching the vault."},{"category":"missing-requirement","check_key":"deterministic-aggregate-work-topology","description":"The executor promises concept, tour, overview, and layer/aggregate writers, yet no section says how those items are selected or scaffolded. Letting the aggregate agent decide would violate the settled two-phase rule that agents never re-derive structure.","finding_id":"R1-aggregate-structure-planner","location":"2.1 FactsBundle / 3.3 renderers / 4.3-4.5 aggregates","prevention":"For every page type, identify the deterministic fact type, stable ID algorithm, enumeration step, scaffold, and queue-item test.","principle":"When agents may not derive structure, a deterministic producer must enumerate every work item and scaffold.","root_cause":"The facts schema and renderers define module structure, while concepts, tours, overview, layer naming, stable IDs, and aggregate scaffolds have no producer.","section_id":"4.3","severity":"blocking","suggested_fix":"Add a deterministic page-work planner and aggregate fact/scaffold types that produce byte-stable concept, tour, overview, and layer-naming items before fan-out. Target and test all four templates and both agent definitions."},{"category":"unhandled-edge","check_key":"degraded-mode-state-transition","description":"With `wiki.codewiki_orchestration=off`, mandatory semantic pages can remain stale while the plan leaves their queue state and finalizer behavior undefined. A deterministic-only run could be reported complete or fresh unless explicitly prohibited.","finding_id":"R1-kill-switch-state","location":"4.3 Orchestration kill switch","prevention":"For every operational switch, enumerate entry state, skipped work state, completion eligibility, observability, and re-enable transition.","principle":"A kill switch needs explicit item, run, freshness, and recovery semantics.","root_cause":"The off branch says only that fan-out is skipped.","section_id":"4.3","severity":"blocking","suggested_fix":"Keep semantic items pending or mark them `skipped_stale`, prohibit fresh/FULL success, expose degraded status, and define deterministic resumption when the switch is re-enabled."},{"category":"unhandled-edge","check_key":"destructive-migration-post-delete-recovery","description":"Link rewrite, lint, reindex, and query parity can fail after thousands of pages and metadata have been removed. Git cannot restore the ignored vault, and the plan gives no snapshot or restore sequence even though 5.2 later deletes the fallback generator.","finding_id":"R1-vault-purge-rollback","location":"5.3 Vault migration","prevention":"Before destructive work, require a verified snapshot/quarantine, staged validation, restore procedure, and rollback drill.","principle":"A destructive migration needs tested recovery from failures discovered after deletion.","root_cause":"The stated rollback, 'do not delete', is only a precondition; the vault is gitignored and validation continues after removal.","section_id":"5.3","severity":"blocking","suggested_fix":"Create and verify a recoverable snapshot or quarantine rename, validate staged output before final removal, test restore after each post-delete failure point, and retain the legacy generator until rollback proof passes. Include generated-delete authorization and confinement tests."},{"category":"missing-requirement","check_key":"single-writer-orchestration-inventory","description":"The plan claims both wiki loops run under one daemon orchestration, but acceptance covers only wiki-librarian tasks. Live scheduled knowledge-vault handlers can still mutate or reindex outside the new run queue and its fences.","finding_id":"R1-unified-wiki-orchestration","location":"4.3 Executor / 4.6 Cron rewiring","prevention":"Inventory every vault mutator and route each through one admission, ownership, and fencing boundary; test cross-family overlap.","principle":"A single orchestration invariant must include every mutating producer that can race on the owned vault.","root_cause":"The new executor absorbs codewiki items and librarian tasks while existing refresh, upkeep, recap, audit, and sync handlers remain separate cron paths.","section_id":"4.3","severity":"blocking","suggested_fix":"Route all knowledge-vault mutators through the shared daemon admission/run ownership boundary, or precisely narrow the invariant and prove no competing writers remain. Add overlap/restart tests across both loop families."},{"category":"missing-requirement","check_key":"comparative-launch-acceptance","description":"The settled target is better-than-parity against Understand-Anything, leveraging the daemon. The live gate measures internal retrieval and timing but never compares named UA capabilities, so passing it cannot prove the launch target.","finding_id":"R1-ua-parity-gate","location":"4.3 Verification / 5.1 FULL gate","prevention":"Translate every comparative claim into a pinned capability matrix with one acceptance probe per row.","principle":"A comparative launch target needs a versioned capability baseline and measurable acceptance rows.","root_cause":"The plan adopts UA-inspired mechanisms and an internal query corpus without defining what better-than-parity means.","section_id":"5.1","severity":"blocking","suggested_fix":"Define a versioned UA capability matrix and require 5.1 to meet every parity row plus named daemon-only advantages. A documented pinned capability baseline is sufficient; vendored UA output is unnecessary."}],"reviewer_session":"cfaa1ab9-8e6b-4544-834d-90c1635e003d","round":1,"round_number":1,"verdict":"needs_review"},"session_id":"4007d890-c17e-494e-86e4-56df0567ab02"}
```

**Round 2** `kind: verification`

- reviewer_run: b26091e4-6811-45a5-91aa-7c284027db57
- reviewer_session: de392185-8b4a-4d7b-b570-194adde7a213
- verdict: needs_review
- findings:
- R2-queue-schema-order / blocking / 4.2 writes the queue tables 4.7 creates without depending on 4.7
- R2-planner-manifest-inputs / blocking / 4.3 step 0 reads tour audiences and seed concepts that 3.7 never defines
- R2-layer-work-topology / blocking / layers.md has three incompatible owners across 3.3, 4.3, and 4.4
- R2-phase-exit-fanin / blocking / P1 and P2 exit gates can run before their phases finish and probe only wiki_search
- R2-facts-artifact-lifecycle / blocking / bundle bytes die with the run row while pages outlive it and validate re-renders from their digests
- R2-concept-catalog-snapshot / blocking / concept planning reads live vault state absent from the pinned run tuple
- R2-admission-serialization / blocking / concurrent admission has no database-enforced single winner
- R2-fence-landing-atomicity / blocking / a per-run item lock cannot fence a successor run and is held across an awaited child process
- R2-quarantine-prefix-recovery / blocking / quarantine is a multi-rename with no mid-prefix recovery record
- R2-file-summary-migration / blocking / 2.4 persists summaries and failures with no migration target
- R2-obsolete-page-reconciliation / blocking / content-addressed page ids rename on re-cluster and the previous generation is never pruned
- R2-live-writer-boundary / blocking / 4.3.9 asserts a knowledge-only boundary the live recap writer contradicts
- resolution_notes: >
  All 12 findings accepted. Every load-bearing citation was verified against
  live code or the plan graph before acceptance: crates/gwiki/src/recap.rs
  writes recaps/YYYY-MM-DD.md, calls catalog::regenerate on the root _index.md,
  and appends logs, none under knowledge/, so 4.3.9 as written could not pass;
  code_indexed_files (postgres_baseline_schema.sql:1568-1583) has no summary,
  failure, or attempt column; crates/gwiki/src/commands/page.rs:115 is a bare
  fs::write; and the declared graph did have 4.2 writing 4.7's tables while
  depending only on 4.1, and 1.5/2.3 hosting phase exit gates while depending on
  a subset of their phases.
  Seven of the twelve are defects in text this plan's round-1 revision
  introduced (R2-planner-manifest-inputs, R2-layer-work-topology,
  R2-phase-exit-fanin, R2-facts-artifact-lifecycle, R2-concept-catalog-snapshot,
  R2-fence-landing-atomicity, R2-live-writer-boundary), and one repeats round
  1's own check key dependency-producer-before-consumer against a section that
  round 1 created.
  Four were applied in cheaper form than suggested. R2-fence-landing-atomicity
  is closed by two existing mechanisms rather than a staging subsystem: 4.7's
  new partial unique index admits one active run per project, which removes the
  successor-generation case the item lock could never cover, and 3.5's generated
  write becomes staged-temp-plus-rename, which makes the landing atomic against
  crashes and orphaned children and makes replay idempotent without staged-byte
  storage. R2-admission-serialization uses that same partial unique index rather
  than an advisory lock, so the invariant is schema-enforced instead of
  lock-discipline-enforced. R2-facts-artifact-lifecycle states retention by
  reference (retain while any current page or resumable run names the digest,
  prune unreferenced terminal bundles) without the separate page-to-digest
  manifest, decompression bound, or materialization channel. R2-layer-work-topology
  is resolved by deleting mechanism rather than adding it: layers.md becomes a
  fully deterministic render with no layer page type and no layer queue item.
  R2-quarantine-prefix-recovery, R2-obsolete-page-reconciliation,
  R2-planner-manifest-inputs, R2-file-summary-migration, R2-phase-exit-fanin,
  R2-queue-schema-order, and R2-live-writer-boundary landed essentially as
  suggested. No new deliverable was created this round; every change is a
  dependency edge, prose, or acceptance edit on an existing section, so the
  section count stays at 28.
  Of the six dismissals, RB-005 was additionally fixed despite being dismissed:
  4.5 required writers to run a validate command that takes only page paths or
  --all while holding no vault access, so writer-side validation is removed and
  3.5's validate-before-commit is the single gate.
  Protocol note: this reviewer ended its run after validate_review_coverage
  without emitting a canonical round_result. The coverage attestation, candidate
  dispositions, finding ids, descriptions, and suggested fixes recorded here are
  its verbatim output recovered from the run transcript; check_key, category,
  root_cause, and prevention are coordinator-assigned.

```json plan-review-round
{"evidence_id":"809a3131-9486-4425-a63f-f7f773535ba3","plan_hash":"726684295087a96fe7b23e25e7fb2b50311f4b6383e3455a306b68d9e9090eb1","round_number":2,"round_result":{"coverage_attestation":{"adjacent_variant_complete":true,"attestation_digest":"d77b0d221e1a4702ac29a22b9bc50e811043fc98b5fdf2429d6de0f1bb139031","cross_lane_interaction_complete":true,"disposition_counts":{"dismissed":6,"emitted_findings":12,"total":18},"evidence_id":"809a3131-9486-4425-a63f-f7f773535ba3","lanes":[{"candidate_count":6,"lane_id":"requirements_traceability","status":"completed"},{"candidate_count":6,"lane_id":"repository_blast_radius","status":"completed"},{"candidate_count":6,"lane_id":"runtime_invariants","status":"completed"}],"shadow_manifest_status":{"entry_count":28,"manifest_digest":"298069a0537eaef995ceef3db9bf391c598160bf62860ebf6afbf8fc83749fb7","status":"valid"},"source_digest":"5a5da784f7fd7f1bb69cfa12ece539e5904f55b6fafbf6850838eb8387b2baf9","version":1},"findings":[{"category":"bad-sequencing","check_key":"dependency-producer-before-consumer","description":"Every durable runtime consumer must depend on the migration and storage producer. Section 4.2 writes codewiki_runs and facts_bundle although only 4.3 depends on 4.7.","finding_id":"R2-queue-schema-order","location":"4.2 Generation service / 4.7 Queue schema","prevention":"When a new producer deliverable is introduced, add an edge from every section that reads or writes the artifact, not only the one that motivated it.","root_cause":"The round-1 revision added 4.7 and wired it to 4.3 only, leaving 4.2 writing tables its own dependency set never produces.","section_id":"4.2","severity":"blocking","suggested_fix":"Add 4.7 to 4.2 dependencies and retain 4.3's explicit storage dependency."},{"category":"missing-requirement","check_key":"planner-input-undeclared","description":"The page-type manifest must define every deterministic planner input. Step 0 reads tour audiences and seed concepts from 3.7, while 3.7 defines neither field and 4.4 supplies only the two audience values.","finding_id":"R2-planner-manifest-inputs","location":"3.7 Page-type manifest / 4.3 step 0","prevention":"When one section consumes named fields from another's artifact, list those fields in the producing section's acceptance.","root_cause":"Round 1 wrote deterministic work planning against manifest fields that the manifest deliverable was never asked to define.","section_id":"3.7","severity":"blocking","suggested_fix":"Add ordered, unique tour_audiences and seed_concepts fields with pinned launch values and parser tests to 3.7."},{"category":"traceability","check_key":"artifact-ownership-contradiction","description":"The layers artifact has contradictory structure and naming ownership: 3.3 requires agent naming in 4.4, 4.3 makes naming deterministic, and the four page types/templates contain no layer page type.","finding_id":"R2-layer-work-topology","location":"3.3 layers renderer / 4.3 planning / 4.4 templates","prevention":"After changing who produces an artifact, re-read every other section that names it and reconcile all of them in the same edit.","root_cause":"Round 1 made layer naming deterministic in 4.3 without retracting 3.3's agent-naming dependency, leaving one artifact with two incompatible owners.","section_id":"3.3","severity":"blocking","suggested_fix":"Keep layers.md as one deterministic render, define tie/collision-stable naming in the facts/render contract, remove semantic layer queue items, and update 4.3.6 accordingly."},{"category":"bad-sequencing","check_key":"gate-not-terminal","description":"An embedded phase-exit gate must depend on every sibling in that phase and exercise the complete queryability invariant. The P1 gate can precede 1.2-1.4/1.6 and checks only wiki_search; the P2 gate can precede 2.5 and checks no wiki API.","finding_id":"R2-phase-exit-fanin","location":"1.5 P1 exit gate / 2.3 P2 exit gate","prevention":"A gate hosted inside a deliverable must inherit dependencies on every sibling whose completion it certifies.","root_cause":"Round 1 placed the exit gates on existing deliverables as acceptance items without adding the fan-in edges that make a gate terminal.","section_id":"1.5","severity":"blocking","suggested_fix":"Keep the gates in 1.5 and 2.3, add fan-in dependencies from all phase siblings, and extend both smoke acceptances to wiki_search, wiki_ask, and wiki_read; retain the P2 facts probe."},{"category":"unhandled-edge","check_key":"artifact-retention-vs-reference","description":"Each current page must retain the exact immutable bundle named by its digest. Partial runs leave multiple live digests, but bytes die with run rows; row deletion breaks validate --all and retaining every row forever is unbounded.","finding_id":"R2-facts-artifact-lifecycle","location":"4.2 bundle storage / 3.4 validation / 5.1 validate --all","prevention":"Before declaring a lifetime rule unnecessary, identify every artifact that outlives the row the bytes are attached to.","root_cause":"Round 1 asserted that deleting bytes with the run row removes the need for a retention policy, overlooking that pages outlive the run that wrote them.","section_id":"4.2","severity":"blocking","suggested_fix":"Keep the existing run-row storage, add facts_digest lookup and a build.json page-to-digest manifest, retain bundle bytes while any current page or resumable run references them, prune unreferenced terminal bundles, bound decompression, and define the verified materialization channel to gwiki validators/writers."},{"category":"unhandled-edge","check_key":"unpinned-mutable-planner-input","description":"Planning claims identical item sets for one FactsBundle while concept items also depend on mutable concept pages and the pinned run tuple contains no concept-catalog snapshot.","finding_id":"R2-concept-catalog-snapshot","location":"4.3 step 0 planning / 4.2 admission","prevention":"Every input to a determinism claim must appear in the pinned tuple that claim is made against.","root_cause":"Round 1 defined the concept set as a union with live vault state while claiming determinism from the FactsBundle alone.","section_id":"4.3","severity":"blocking","suggested_fix":"Canonicalize and sort the concept inventory at admission, pin its digest/bytes with the run, plan transactionally from that snapshot, and test a vault mutation after admission."},{"category":"unhandled-edge","check_key":"concurrent-admission-race","description":"Concurrent admission lacks a database-enforced single winner. Two HTTP/cron requests can both observe no matching active run and insert duplicates because the schema defines no project/fingerprint uniqueness or admission lock.","finding_id":"R2-admission-serialization","location":"4.7 queue schema / 4.2 admission","prevention":"Express single-winner invariants as schema constraints; a read-then-write in application code is not serialization.","root_cause":"Coalescing was specified as an application-level check-then-insert with no database constraint behind it.","section_id":"4.7","severity":"blocking","suggested_fix":"Use a project-scoped advisory transaction lock around admission lookup/insert, return the winner on conflict, and test two real connections released at the insert barrier."},{"category":"unhandled-edge","check_key":"fence-scope-insufficient","description":"The fence is scoped to a per-run item row while the protected side effect occurs in a child gwiki process. Successor rows do not conflict, and daemon death can release the database lock while an orphaned child still writes.","finding_id":"R2-fence-landing-atomicity","location":"4.3 landing fence / 4.7 item rows / 3.5 generated write","prevention":"Check that a lock's scope actually covers every writer of the protected resource, and that the protected operation runs inside the lock's process.","root_cause":"Round 1's fix locked the item row of one run, which cannot exclude a different run's row for the same page, and held that lock across an awaited subprocess.","section_id":"4.3","severity":"blocking","suggested_fix":"Persist validated staged bytes/digest, enforce one leased active executor per project or a page-global generation fence, recheck it immediately before an atomic in-process replace, and recover by comparing landed and staged digests before replay."},{"category":"unhandled-edge","check_key":"multi-step-rollback-window","description":"Quarantining scattered legacy targets requires multiple renames, but recovery tests begin only after quarantine is complete. A crash between moves leaves a split vault with no durable resume/restore record.","finding_id":"R2-quarantine-prefix-recovery","location":"5.3 vault quarantine","prevention":"When a recovery mechanism is itself multi-step, make its own intermediate states recoverable before relying on it.","root_cause":"Round 1 introduced quarantine-then-verify but treated the quarantine step itself as atomic when it is a sequence of renames.","section_id":"5.3","severity":"blocking","suggested_fix":"Write and fsync an idempotent move manifest before the first rename, record each move, resume or restore from every prefix, and inject crashes between moves and during restore."},{"category":"missing-requirement","check_key":"durable-storage-without-migration","description":"Per-file summaries and failure rows require durable schema, but 2.4 targets only Python summarizer/storage code. The current code_indexed_files table has no summary/failure fields and hub schema is migration-driven.","finding_id":"R2-file-summary-migration","location":"2.4 per-file summaries","prevention":"Any acceptance naming persisted fields needs a migration target in the same section.","root_cause":"The deliverable specified durable per-file storage and coverage counters without a migration target, in a hub whose schema is migration-driven.","section_id":"2.4","severity":"blocking","suggested_fix":"Add a numbered migration and baseline mirror to 2.4 for the chosen indexed-file columns or a normalized summary table, with current-hash constraints, failure state, upgrade/idempotency tests, and counter queries."},{"category":"unhandled-edge","check_key":"generated-artifact-not-pruned","description":"Content-addressed module IDs change when clustering membership changes, but no executor/finalizer step reconciles generated pages absent from the new deterministic page set. Old module/concept/tour pages can remain indexed and answer queries as stale duplicates.","finding_id":"R2-obsolete-page-reconciliation","location":"4.3 finalizer / 2.2 module ids","prevention":"When artifact identity is derived from content, the writer must own deletion of the identities it replaced.","root_cause":"Page identity is content-addressed, so regeneration renames rather than overwrites, and nothing in the finalizer removed the previous generation.","section_id":"4.3","severity":"blocking","suggested_fix":"Persist the authoritative current page-path set in build.json; after all replacements validate, quarantine/delete generated code/** pages absent from that set under the same fence, then reindex. Test module split, merge, file deletion, and failed-run no-prune behavior."},{"category":"weak-testability","check_key":"acceptance-contradicts-live-code","description":"The literal knowledge/** ownership claim is contradicted by a live scheduled writer: recap writes recaps/YYYY-MM-DD.md, regenerates the root catalog, and appends logs. The planned ownership acceptance cannot pass against that inventory.","finding_id":"R2-live-writer-boundary","location":"4.3 write ownership / crates/gwiki/src/recap.rs","prevention":"State disjointness in the direction that is verifiable against live writers, and enumerate the other side from code.","root_cause":"Round 1 stated the partition symmetrically from memory rather than from the live writer inventory, making one half of the assertion false.","section_id":"4.3","severity":"blocking","suggested_fix":"Preserve the settled disjoint architecture, define the legacy family as every non-code path it actually writes (knowledge/**, recaps/**, root catalog/log artifacts), assert those handlers never write code/**, and test overlap on the full live writer inventory."}],"reviewer_session":"de392185-8b4a-4d7b-b570-194adde7a213","round":2,"round_number":2,"verdict":"needs_review"},"session_id":"4007d890-c17e-494e-86e4-56df0567ab02"}
```

**Round 3** `kind: verification`

- reviewer_run: a6e68a26-ab4e-4b0c-828f-29adf23b3fd2
- reviewer_session: e8dad7f2-6c0f-44fb-9060-f36bfe917f0a
- verdict: needs_review
- findings:
- R3-active-run-successor-loss / blocking / the non-terminal uniqueness predicate rejects the queued successor 4.2 promises, so a newer commit is absorbed and never builds
- R3-partial-prune-authority / blocking / finalization treats a PARTIAL run's touched set as global delete authority and removes untouched pages
- R3-layer-summary-input / blocking / deterministic layer summaries derive from a ModuleFacts.summary field the schema does not define
- R3-orphan-fence-bypass / blocking / atomic rename preserves integrity but not ordering, so a delayed orphan can overwrite a newer landing with stale whole bytes
- R3-generated-gateway-authority / blocking / live GwikiGateway write_page/delete_page cannot express the generated/template authorization 3.5 requires
- R3-legacy-prune-before-cutover / blocking / the new prune is not marker-scoped, so P4 can delete legacy fallback pages before 5.3 quarantine
- R3-bundle-reader-race / blocking / a validator reading page then bundle can observe a pruned digest and hard-fail on a healthy vault
- resolution_notes: >
  All 7 findings accepted. Five are defects introduced by this plan's round-2
  repairs (R3-active-run-successor-loss, R3-partial-prune-authority,
  R3-layer-summary-input, R3-orphan-fence-bypass, R3-legacy-prune-before-cutover),
  and two of those five would have caused data loss: the PARTIAL prune deleting
  untouched pages, and the unmarked prune deleting the legacy vault before its
  supervised quarantine. Findings per round are falling (17, 12, 7) while the
  fixer-induced share is rising (0/17, 7/12, 5/7), which is the class-incomplete
  repair pattern documented in docs/plans/adversary-convergence.md.
  Citations verified before acceptance: ModuleFacts as declared at plan line 295
  carries id, name, files, edges, neighbor_exports, and git_stats with no summary
  field; GwikiGateway.write_page (gwiki_gateway.py:285-299) builds argv as
  page write --path/--mode and forwards --expected-hash only when supplied, and
  delete_page (:301) takes a path alone; and 4.2's own text promises a queued
  successor at a newer commit, which the round-2 index predicate rejected.
  Each fix was applied as a class sweep rather than an instance patch. The
  uniqueness predicate is split into two partial indexes, one over executing
  states and one over the single queued successor, with promotion in the same
  transaction that terminalizes the executing run. Planning now produces a
  complete desired inventory for every mode, distinct from the work items a
  scoped run enqueues, and reconciliation compares against the inventory; the
  same paragraph adds the marker restriction so only gwiki-code pages are ever
  pruned before 5.3. Layer and module summaries gain a declared derivation from
  FileFacts.summary through the shared normalizer, avoiding a schema change. The
  ordering hazard reuses the expected-hash precondition the write path already
  carries rather than adding staged-byte storage. 3.6 gains generated/template
  plumbing on the existing write_page and delete_page. The validator retries its
  page read once when a named digest has vanished, which is cheaper than making
  retention block on concurrent readers.
  No new deliverable was created; the plan remains at 28 sections, acyclic, with
  both validation modes passing.
  Protocol note, repeated from round 2: this reviewer again ended its run after
  validate_review_coverage without emitting a canonical round_result, despite an
  explicit instruction to do so, and its end_agent_run call first failed with
  "No active session context available". The attestation, dispositions, finding
  ids, descriptions, and suggested fixes recorded here are its verbatim output
  recovered from the run transcript; check_key, category, root_cause, and
  prevention are coordinator-assigned.

```json plan-review-round
{"evidence_id":"f65f9744-7006-4784-9dc8-74451a607ab3","plan_hash":"c054e864eedc31ae54ef73991408d3a09f8aee1cea15a804b9f9a5154a95c2c2","round_number":3,"round_result":{"coverage_attestation":{"adjacent_variant_complete":true,"attestation_digest":"12ff04eb6e061e5915de270ca71cbc04ebae74708c7bac099ae4b70ee9fb5ffe","cross_lane_interaction_complete":true,"disposition_counts":{"dismissed":5,"emitted_findings":7,"total":12},"evidence_id":"f65f9744-7006-4784-9dc8-74451a607ab3","lanes":[{"candidate_count":3,"lane_id":"requirements_traceability","status":"completed"},{"candidate_count":5,"lane_id":"repository_blast_radius","status":"completed"},{"candidate_count":4,"lane_id":"runtime_invariants","status":"completed"}],"shadow_manifest_status":{"entry_count":28,"manifest_digest":"d1e654aac74c0c8214c1bf715a641c1b6ac72328c6bea93e26b9420c204d1927","status":"valid"},"source_digest":"032b5b3c153eae63792d96267d1d52c37988c9dc751810488b2719a6e8805e0a","version":1},"findings":[{"category":"unhandled-edge","check_key":"uniqueness-predicate-too-broad","description":"A newer trigger arriving during an in-flight run must durably queue a successor. Section 4.2 requires that behavior, while 4.7's partial unique index rejects every second non-terminal run for the project and directs the loser to return the current winner's ID. A queued successor is itself non-terminal, and no separate successor-intent storage exists. Run A at commit A therefore absorbs trigger B at commit B; A finishes at the old commit and B is lost.","finding_id":"R3-active-run-successor-loss","location":"4.2 admission / 4.6 cron overlap / 4.7 uniqueness","prevention":"When adding a uniqueness constraint, enumerate every state the predicate captures and check each against the behaviors the plan already promises for that state.","root_cause":"The round-2 repair chose a uniqueness predicate over all non-terminal states, which silently includes the queued state that the same plan relies on for successor coalescing.","section_id":"4.7","severity":"blocking","suggested_fix":"Separate waiting successors from the single executing-run invariant. Restrict the partial unique index to execution/landing states and add constrained queued-successor storage with atomic promotion, or persist one coalesced successor intent on the active run. Test a different-fingerprint overlap and prove the newer commit eventually runs."},{"category":"unhandled-edge","check_key":"scoped-run-treated-as-global-authority","description":"A successful PARTIAL run must preserve valid untouched pages. Section 4.2 says PARTIAL runs leave untouched pages stamped with older digests, while 4.3 makes the current run's page-path set authoritative and deletes every generated page absent from it. A run touching only module A therefore deletes untouched module B.","finding_id":"R3-partial-prune-authority","location":"4.2 PARTIAL semantics / 4.3 finalization","prevention":"Before deriving a destructive decision from a set, check whether that set is complete or scoped, and name the complete one explicitly.","root_cause":"The round-2 repair added pruning without distinguishing the pages a run regenerates from the pages that should exist, so a scoped run's work list became a global delete authority.","section_id":"4.3","severity":"blocking","suggested_fix":"Define the post-run authoritative set as the previous current set minus explicitly obsolete identities plus successful replacements, or derive a complete desired inventory while enqueueing only touched pages. Test that an unrelated module and its older bundle survive PARTIAL reconciliation."},{"category":"missing-requirement","check_key":"renderer-input-without-producer","description":"Every deterministic renderer input needs a declared producer. ModuleFacts has no summary field, yet the repaired deterministic layers renderer derives layer summaries from member module summaries before any agent work. No canonical derivation from FileFacts summaries is specified.","finding_id":"R3-layer-summary-input","location":"2.1 FactsBundle schema / 3.3 layers renderer","prevention":"When making a value deterministic, trace its input to a field that exists in a declared schema, not to a plausible-sounding one.","root_cause":"The round-2 repair made layer summaries deterministic by pointing at module summaries, which the declared FactsBundle schema does not contain.","section_id":"3.3","severity":"blocking","suggested_fix":"Specify a canonical module-summary derivation from sorted FileFacts summaries in 3.3 and test exact output, or add ModuleFacts.summary to 2.1 and require 2.2 plus the shared golden fixture to produce it byte-stably."},{"category":"unhandled-edge","check_key":"atomicity-without-ordering","description":"Atomic rename preserves whole-file integrity but not generation order. A daemon crash releases the row lock while the gwiki child survives; recovery or a successor can land newer bytes before the orphan performs its delayed rename and overwrites them with stale complete bytes.","finding_id":"R3-orphan-fence-bypass","location":"3.5 generated write / 4.3 landing fence / 4.7 item rows","prevention":"Distinguish integrity from ordering: an atomic write still needs a precondition to establish which generation it is allowed to replace.","root_cause":"The round-2 repair treated atomicity as sufficient for the orphaned-child hazard, closing the half-write half of it while leaving the write-ordering half open.","section_id":"4.3","severity":"blocking","suggested_fix":"Make the final replace conditional on a durable generation fence checked by the process performing the rename, or return staged validated bytes so the executor rechecks the fence immediately before its own replace. Delay the orphan past successor landing in a crash test."},{"category":"traceability","check_key":"new-cli-surface-without-caller-plumbing","description":"The executor and finalizer must invoke the authorized generated write/delete path. The new Rust commands require generated/template fields, but the live Python GwikiGateway methods hardcode argv without them and 3.6 only specifies new code_* wrappers. Literal implementation leaves code/** writes and pruning rejected or unavailable.","finding_id":"R3-generated-gateway-authority","location":"3.5 generated write / 3.6 gateway surface / 4.3 executor","prevention":"When a command gains a required flag, list every existing caller that constructs its argv as a target, not only the new wrappers.","root_cause":"New CLI flags were pinned in the contract while the existing Python callers that must send them were never named, so the plan's own executor could not reach the path it authorizes.","section_id":"3.6","severity":"blocking","suggested_fix":"Require GwikiGateway.write_page and delete_page to accept and forward generated/template authorization, with focused argv tests and non-generated defaults for existing callers."},{"category":"unhandled-edge","check_key":"destructive-step-ignores-freeze-boundary","description":"P2-P4 must preserve legacy output until supervised quarantine. The new finalizer prunes generated code/** pages absent from its set without restricting ownership to the new exact gwiki-code marker. Existing classification treats legacy gcode-codewiki pages as generated codewiki, so P4 can delete code/files/** and other fallback pages before 5.1/5.3.","finding_id":"R3-legacy-prune-before-cutover","location":"3.4 dual markers / 4.3 prune / 5.2-5.3 cutover","prevention":"A new destructive step must state which artifact family it owns, especially where a migration deliberately keeps two families alive at once.","root_cause":"The round-2 prune repair was written against the new page family only, without reconciling it with round 1's decision to keep the legacy marker live through P4.","section_id":"4.3","severity":"blocking","suggested_fix":"Restrict 4.3 reconciliation to pages bearing the exact new gwiki-code marker. Preserve every gcode-codewiki page until 5.3 quarantine and test a mixed-marker vault before cutover."},{"category":"unhandled-edge","check_key":"read-then-read-across-mutation","description":"A validator that has read page P naming D1 must resolve D1 for that validation attempt. Finalization can replace P with D2 and prune D1 after the validator reads P but before it fetches the bundle, producing a false hard failure despite atomic page replacement.","finding_id":"R3-bundle-reader-race","location":"3.4 validation / 4.2 retention / 4.3 finalizer","prevention":"When a consumer performs two dependent reads, define what happens if the referent changes between them.","root_cause":"Reference-counted retention was defined against current pages at an instant, while validation reads the page and its bundle at two different instants.","section_id":"3.4","severity":"blocking","suggested_fix":"Define page-plus-bundle snapshot semantics: pin bundle bytes before pruning, or retry the page read when its digest disappears and the page changed. Add a race test paused between page read and bundle lookup."}],"reviewer_session":"e8dad7f2-6c0f-44fb-9060-f36bfe917f0a","round":3,"round_number":3,"verdict":"needs_review"},"session_id":"4007d890-c17e-494e-86e4-56df0567ab02"}
```

**Round 4** `kind: verification`

- reviewer_run: 9b1277db-8c19-497a-8746-36b3fe56c4ed
- reviewer_session: 9e97bf65-bbd1-4bfe-8923-87ae83ca1342
- verdict: needs_review
- findings:
- R4-command-admission-match / blocking / new code render|validate|status variants make the exhaustive match in commands/project_admission.rs non-exhaustive, a compile failure before anything can ship
- R4-desired-inventory-contract / blocking / the desired inventory is destructive authority with no column in 4.7, no repo-wide guarantee for scope runs, and no bootstrap rule
- R4-generated-create-precondition / blocking / a mandatory --expected-hash makes creating an absent page impossible, so the first FULL run cannot produce a single page
- R4-successor-intent-loss / blocking / newest-wins replacement of the queued successor is non-monotone and silently drops an earlier trigger's changed files
- R4-stale-executing-run / blocking / nothing terminalizes an abandoned running/landing row, so the executing partial index wedges the project permanently
- R4-marker-authority-incomplete / blocking / the exact-marker rule reached prune selection only, leaving audit/claims.rs and the 5.3 quarantine conflating the two live families
- R4-facts-digest-resolver / blocking / 3.4 re-renders from a facts_digest with no named resolver reaching the bytes 4.2 retains
- resolution_notes: >
  All 7 findings accepted; one was accepted in part. Four are self-attributed
  by the reviewer as defects introduced by this plan's round-3 repairs, carrying
  `introduced_in_round: 3` and a `causal_finding_id` naming the exact repair:
  R3-partial-prune-authority produced R4-desired-inventory-contract,
  R3-orphan-fence-bypass produced R4-generated-create-precondition,
  R3-active-run-successor-loss produced R4-successor-intent-loss, and
  R3-legacy-prune-before-cutover produced R4-marker-authority-incomplete.
  Findings per round are 17, 12, 7, 7 while candidates examined rose from 12 to
  23, so the reviewer searched roughly twice as wide for the same yield.
  Citations ground-checked before acceptance: project_admission.rs:40-69 matches
  every Command variant by name with the read-only arm enumerating each one, so a
  new variant is a compile error rather than a runtime gap, and page write already
  sits on the read-only side because that classifier splits scope admission rather
  than vault mutation; page.rs:60-67 rejects --expected-hash in create mode and
  :77-84 rejects upsert-with-hash against an absent page, which together make the
  first FULL run's every page unwritable; audit/claims.rs:198-200 grounds a page
  only when generated_by equals GENERATED_BY_CODEWIKI, so every page this plan
  generates would fail its own audit; 4.7's codewiki_runs column list carried no
  desired-inventory and no run-level lease; and gwiki already connects read-only
  to the hub in commands/graph.rs, graph_context.rs, and benchmark.rs via
  gobby_core::postgres::connect_readonly, which is the precedent the facts
  resolver follows.
  Fixes were applied as class sweeps and each reuses existing mechanism. The
  precondition domain gains --expected-absent as the mutual exclusive of
  --expected-hash, exactly one being mandatory, so creation and replacement are
  both representable and an unconditional generated write still is not; 3.6
  forwards whichever applies. The command-wiring sweep is restated as covering
  every exhaustive consumer of the enum rather than only files under commands/.
  The queued successor stops carrying a changed-file list at all: it holds the
  newest target commit and the classifier recomputes the diff from the last
  successfully built baseline at promotion, which is monotone by construction, with
  a max-requested-mode field for the one intent a diff cannot express. The run row
  gains the owner/heartbeat_at lease its page items already had, with startup and
  watchdog recovery terminalizing expired runs and promoting the successor in one
  transaction. Admission pins a repo-wide bundle on every mode so the inventory is
  derivable, the inventory gets its column, an absent baseline classifies FULL, and
  the fresh stamp is withheld until the inventory is materialized. 3.4 gains the
  read-only facts resolver with an injectable source, which keeps the deliverable
  inside P3 and avoids a P3-to-P4 dependency edge, plus dual-marker recognition in
  the audit path. 5.3's quarantine manifest selects exact legacy identity only.
  Declined in part: the staging area R4-marker-authority-incomplete proposed for
  same-path collisions on the shared deterministic pages. The legacy generator is
  installed until 5.2, so those pages are reproducible by running gcode codewiki,
  which makes the overwrite recoverable by a command rather than a restore, and
  staging regenerable content buys nothing. The real requirement — that the loss
  not be silent — is met instead by having 5.1 record the replaced paths and 5.3
  exclude them from its manifest.
  No new deliverable was created; the plan remains at 28 sections, acyclic, with
  both validation modes passing.
  Protocol note: unlike rounds 2 and 3, this reviewer emitted a canonical
  round_result and delivered it as a P2P review_result message. The round-2 and
  round-3 omissions were caused by the coordinator's hand-authored prompt
  replacing the skill's standard closing instruction, not by the
  plan-adversary-taskless definition; six other adversary runs in the same window
  emitted parseable results unmodified. Round 4 restored an explicit emission
  protocol to the prompt and the defect did not recur. Every field in the fence
  below is the reviewer's own output.

```json plan-review-round
{"evidence_id":"e3a6df27-44bd-4046-b2ef-47db8e71ccee","plan_hash":"543392bae81c0651f93f02c13cfd2c852538df2026d659790786e7aa527da4ea","round_number":4,"round_result":{"coverage_attestation":{"adjacent_variant_complete":true,"attestation_digest":"f1ffdd6108913e19404eef0c856f19c004f17bc2397253bef7efd1b25f1ce063","cross_lane_interaction_complete":true,"disposition_counts":{"dismissed":16,"emitted_findings":7,"total":23},"evidence_id":"e3a6df27-44bd-4046-b2ef-47db8e71ccee","lanes":[{"candidate_count":5,"lane_id":"requirements_traceability","status":"completed"},{"candidate_count":9,"lane_id":"repository_blast_radius","status":"completed"},{"candidate_count":9,"lane_id":"runtime_invariants","status":"completed"}],"shadow_manifest_status":{"entry_count":28,"manifest_digest":"53a1f10eddfe21f1e56988110468b70d149a05d9b60827bd5810eca2d8b364c4","status":"valid"},"source_digest":"5377c40abf2657067c081ad9adb58dafeedece485d0a145174e92d00a895877b","version":1},"findings":[{"category":"missing-requirement","check_key":"exhaustive-command-variant-consumer","description":"Adding the planned code render/validate/status Command variants makes the live project-admission match non-exhaustive. Because 3.6 omits project_admission.rs, a literal implementation reaches a Rust compile failure before the new commands can ship.","finding_id":"R4-command-admission-match","location":"3.6 gwiki contract and daemon gateway / crates/gwiki/src/commands/project_admission.rs","participating_section_ids":["3.6"],"prevention":"For every new Rust enum variant, sweep all exhaustive matches, policy classifiers, fakes, and tests, including consumers outside the command module.","principle":"New enum variants must be assigned in every exhaustive consumer before the producer section is buildable.","root_cause":"The command-wiring inventory covered parser, mapping, API, dispatch, and contracts but missed an exhaustive policy consumer of the same enum.","section_id":"3.6","severity":"blocking","suggested_fix":"Add crates/gwiki/src/commands/project_admission.rs and its focused tests to 3.6, classify all three commands explicitly for project locking/admission, and include that policy match in the command-wiring sweep."},{"category":"unhandled-edge","causal_finding_id":"R3-partial-prune-authority","causal_section_ids":["4.3"],"check_key":"authoritative-inventory-full-lifecycle","description":"The repaired desired inventory has neither a complete durable representation nor safe bootstrap/scope semantics. 4.3 requires it on the run row, while 4.7 omits the column; a scope bundle cannot derive a repository-wide delete authority; and a first auto/scope run can enqueue only a subset, then report inventory paths that were never materialized. That can prune out-of-scope pages or stamp an incomplete first vault fresh.","finding_id":"R4-desired-inventory-contract","introduced_in_round":3,"location":"4.1 classifier / 4.2 admission / 4.3 step 0 and finalizer / 4.7 run schema","prevention":"Whenever a set becomes destructive authority, trace its complete inputs, durable schema, constructors, serialization, bootstrap state, scoped modes, and final existence check.","principle":"A destructive inventory must be repository-complete, durable, and fully materialized before it authorizes deletion or freshness.","root_cause":"Round 3 introduced a logical full inventory at the executor without tracing its mode-independent inputs, schema field, constructors, restart path, or empty-vault materialization rule.","section_id":"4.3","severity":"blocking","suggested_fix":"Pin a full-repository inventory snapshot for every non-SKIP run, add canonical desired_inventory storage plus run-record/build.json serializers in 4.7, define an absent baseline as bootstrap FULL or make every desired-but-absent page mandatory, and test FULL, PARTIAL, ARCHITECTURE, COSMETIC, manual SCOPE, and first-run cases."},{"category":"unhandled-edge","causal_finding_id":"R3-orphan-fence-bypass","causal_section_ids":["3.5","3.6","4.3"],"check_key":"absent-resource-cas-precondition","description":"The first FULL run must create absent pages, but generated writes now require --expected-hash. The current contract rejects a hash in create mode and rejects an absent page in upsert-with-hash mode, so no legal argument set can create the first module, concept, tour, overview, or metadata page.","finding_id":"R4-generated-create-precondition","introduced_in_round":3,"location":"3.5 generated write / 3.6 gateway / 4.3 first landing","prevention":"For every CAS-protected upsert, test existing-match, existing-mismatch, expected-absent, unexpected-present, deletion, and replay branches.","principle":"A compare-and-swap API for upsert must represent both present and absent expected states.","root_cause":"Round 3 reused an existing-value compare-and-swap guard for a workflow that also creates resources, without defining the absent state in the precondition domain.","section_id":"3.5","severity":"blocking","suggested_fix":"Model expected state explicitly with mutually exclusive expected-hash and expected-absent preconditions, recheck either under the same interprocess page lock immediately before rename, thread expected-absent through CLI/API/contracts/gateway, and test concurrent first creators plus crash replay after an unacknowledged create."},{"category":"unhandled-edge","causal_finding_id":"R3-active-run-successor-loss","causal_section_ids":["4.7"],"check_key":"successor-intent-coalescing","description":"With run A executing, trigger B can queue changed file x or a FULL request, then newer trigger C replaces B with only changed file y or a narrower scope. Because classification consumes per-trigger changed files/mode, eventual C can omit x or silently discard the pending FULL request even though the newest commit itself is retained.","finding_id":"R4-successor-intent-loss","introduced_in_round":3,"location":"4.1 classifier inputs / 4.2 trigger admission / 4.7 queued-successor replacement","prevention":"For every coalescing/replacement queue, prove that the merge operation is monotone over every payload field, not only the row identity or newest timestamp.","principle":"Dropping an older queued request is safe only when the retained request semantically subsumes it.","root_cause":"Round 3 made the queued row replaceable by recency while treating the snapshot commit as if it subsumed the trigger's non-cumulative planning intent.","section_id":"4.7","severity":"blocking","suggested_fix":"Make successor replacement monotone: retain the earliest unbuilt baseline, newest target snapshot, union of pending changed files/scopes, and the maximum-severity requested mode, or recompute the complete diff from the last successful baseline when promoting. Test B:x then C:y and FULL then SCOPE overlaps."},{"category":"unhandled-edge","check_key":"nonterminal-run-recovery","description":"A daemon loss, cancellation, worker exception, or hard bundle-digest failure can leave a run in running or landing indefinitely. Page-item leases do not clear the run row; the executing partial index then blocks promotion and fresh admission forever, while matching triggers coalesce into the wedged run and newer triggers remain queued.","finding_id":"R4-stale-executing-run","location":"4.2 restart admission / 4.3 executor recovery / 4.7 executing-state index","participating_section_ids":["4.2","4.3","4.7"],"prevention":"For every non-terminal database state protected by uniqueness, define owner liveness, expiry, restart discovery, recovery, terminal failure, and successor release.","principle":"A unique active-state predicate requires a bounded recovery path for abandoned owners.","root_cause":"Durability was specified for page items and bundle bytes, but no recovery transition owns non-terminal run state itself.","section_id":"4.7","severity":"blocking","suggested_fix":"Add a run-level claim/lease with owner/fence or an explicit startup/watchdog stale-run protocol. Recovery must atomically resume recoverable rows, terminalize unrecoverable rows with a stable error, and promote the queued successor; test crashes in running/landing, cancellation, and corrupt stored bundles."},{"category":"unhandled-edge","causal_finding_id":"R3-legacy-prune-before-cutover","causal_section_ids":["4.3"],"check_key":"marker-authority-operation-sweep","description":"The exact-marker repair covers prune selection only. P4 writes target legacy paths such as code/features.md and can overwrite a gcode-codewiki occupant before cutover; audit/claims.rs recognizes only the legacy constant and misclassifies new gwiki-code pages; and 5.3 does not require its scattered quarantine manifest to select exact legacy markers and exclude new pages.","finding_id":"R4-marker-authority-incomplete","introduced_in_round":3,"location":"3.4 dual markers / 3.5 writes / 4.3 reconciliation / 5.3 quarantine / gwiki audit claims","prevention":"When introducing a second ownership marker, enumerate reads, writes, replacements, deletes, audits, retention, migration selection, link targets, fixtures, and final constant removal.","principle":"Artifact-family authority must be consistent across every operation, especially during dual-marker migration.","root_cause":"Round 3 patched the destructive prune operation without sweeping every other operation and consumer that distinguishes the two coexisting generated families.","section_id":"4.3","severity":"blocking","suggested_fix":"Define one exact family policy across classification, replacement, deletion, and cutover: recognize both markers through P4; allow generated replacement only for absent or gwiki-code occupants; stage same-path collisions until supervised cutover; and have 5.3 move only exact legacy-marker/explicit legacy artifacts before publishing staged replacements. Sweep fixtures and remove legacy recognition in 5.2."},{"category":"bad-sequencing","check_key":"digest-reference-resolution-path","description":"Validation must re-render from each page's recorded facts_digest, including historical digests left by PARTIAL runs, but 3.4 names no resolver or runtime channel to the compressed bytes retained later in codewiki_runs. The P3 validator task therefore has neither a self-contained production implementation path nor a dependency on the schema that supplies its referent.","finding_id":"R4-facts-digest-resolver","location":"3.4 deterministic validation / 4.2 retained bundle bytes / 4.7 run-row schema","participating_section_ids":["3.4","4.2","4.7"],"prevention":"For every digest reference, trace the exact bytes from persistence through lookup, authorization, decompression bounds, verification, consumer API, retention, and dependency order.","principle":"A digest-consuming task must depend on and name the runtime resolver for the bytes it identifies.","root_cause":"Digest production, durable byte ownership, and the Rust consumer were assigned to separate phases without an explicit lookup interface or producer-to-consumer edge.","section_id":"3.4","severity":"blocking","suggested_fix":"Keep the settled reference-based run-row retention, add a gwiki digest resolver that reads/decompresses/verifies retained bundle bytes with bounds, name its Rust storage targets and tests in 3.4, and sequence the required schema/resolver producer before production validation and generated writes."}],"reviewer_session":"9e97bf65-bbd1-4bfe-8923-87ae83ca1342","round":4,"round_number":4,"verdict":"needs_review"},"session_id":"4007d890-c17e-494e-86e4-56df0567ab02"}
```

**Revision: legacy generator retired in P3** `kind: verification`

- trigger: user decision, 2026-07-27
- scope: 1.5, 3.1, 3.4, 3.6, 3.8 (new), 4.3, 5.1, 5.2, 5.3, Constraints
- sections: 28 -> 29
- notes: >
  The user confirmed the legacy codewiki generator has no users and no rollback
  value: the vault is gitignored, they do not query it, and only `gcode` itself
  (index, graph, symbols, search) must keep working. That retracts the premise
  behind a large block of this plan's mechanism, all of which existed to keep
  `gcode codewiki` runnable rather than merely present.
  Investigation before the change: the `crates/gcode/src/commands/codewiki/`
  subtree has no inbound imports from outside itself except four `From<AiDepthArg>`
  conversions in `crates/gcode/src/cli.rs`, which die with the CLI args; 3.1 and
  3.2 were already specified as reimplementations that forbid importing from the
  subtree, so it is needed as copy source (2.2) and reference (3.1, 3.2) but never
  as a running program; and the marker string is pinned in exactly four places
  that break on a flip (`codewiki/publication.rs:559`,
  `codewiki/tests/architecture.rs:76`, `codewiki/tests/contract.rs:103`, and the
  gcore golden at `codewiki_contract.rs:49`), while the ~15 other occurrences under
  `crates/gwiki` are literal fixture strings that keep parsing.
  Removed: the dual-marker window in 3.4 and its second golden; the audit-path
  dual recognition; the marker-scoped prune restriction in 4.3; the
  quarantine-selection rule and replaced-path exclusion list in 5.1 and 5.3; the
  legacy-freeze boundary in Constraints and its single sanctioned carve-out in
  3.6; the fallback clauses in 3.4.6 and 3.6.5; and 1.5's prompt edit and render-
  version bumps, which existed only to make a doomed generator's next run correct.
  Added: 3.8, which retires the `gcode codewiki` CLI arm, AI arg enums, dispatch
  arms, contract entry, and Python glue (`GcodeGateway.codewiki()`, the nightly
  cron registration, the legacy git-hook POST), and now carries the P3 exit gate
  with fan-in on every P3 sibling. 3.1 additionally relocates the feature catalog
  into gwiki alongside the renderer that reads it, which is what lets 3.6 drop its
  carve-out. 5.2 shrinks to subtree and documentation deletion.
  Two consequences are accepted deliberately rather than mitigated. The ~2,900
  legacy pages on disk stop matching the flipped constant, so `gwiki audit`
  reports them ungrounded until 5.3 removes them; this also makes 4.3's prune
  unable to reach the legacy vault at no cost. And from 3.8 until 4.2 wires the
  new endpoint, no codewiki automation runs at all, which is the intended outcome
  rather than a gap: the pages are stale, unqueried, and scheduled for deletion.
  This revision invalidates the round-4 convergence on 3.4, 3.6, 4.3, 5.1, and
  5.3. Round 5 must re-review them against the new shape.


**Round 5** `kind: verification`

- reviewer_run: d70d5aff-2591-4834-b554-5541a2ac5875
- reviewer_session: f5399ee8-c968-439d-b4f7-d4d933a570ca
- verdict: needs_review
- findings:
- R5-retirement-marker-order / blocking / 3.8 depended on 3.4, so the shared marker flipped while every legacy entry point was still live
- R5-retirement-public-export / blocking / the subtree stays publicly declared and compiled, so removing only the CLI command left a callable generator
- R5-retirement-call-graph / blocking / 3.8's teardown named three sites out of a call graph that reaches the refresh service, trigger, startup construction, two routes, the cron, and the UI
- R5-audit-marker-fixtures / blocking / the gwiki occurrences of the old literal are not uniformly parser fixtures; the audit suite asserts grounding through marker equality
- R5-successor-scope-intent / blocking / the monotone successor kept commit and mode but dropped requested scope, which no diff can reconstruct
- R5-inventory-missing-page-repair / blocking / the inventory freshness check detects a missing page that no non-FULL run is ever guaranteed to enqueue
- R5-run-lease-takeover / blocking / owner plus heartbeat cannot express takeover across a restart, where no new process can match the dead owner
- R5-negative-marker-quarantine / blocking / quarantine selected by inequality with the current marker, and `generated_by` is optional, so unmarked pages were in scope for a destructive rename
- R5-generated-delete-cas / blocking / `--expected-absent` carries no authority to delete an existing page, so a stale prune could remove a replacement
- R5-file-summary-project-key / blocking / a `(path, content_hash)` summary key collides across two projects holding the same file
- R5-file-summary-lifecycle / blocking / mandatory file summaries were left behind the optional `symbol_summary.enabled` gate, so nothing would produce them
- R5-bundled-content-manifest / blocking / four sections add files under `install/shared/` and none regenerates the content-addressed manifest a committed test pins
- R5-orchestrator-lifecycle / blocking / nothing constructs, starts, recovers, or cancels the executor and watchdog, so admitted rows would never drain
- R5-truth-digest-cutover / blocking / memory dream reads `_meta/truth_digest.json`, whose only producer is the generator being retired, and a missing file degrades silently
- R5-vault-migration-owner / blocking / 5.3 described a supervised crash-recoverable procedure with no implementation target to expand into
- R5-quarantine-rollback / blocking / restoring quarantined files does not un-edit the surviving pages the link rewrite already changed
- resolution_notes: >
  Sixteen findings over 27 candidates, against a plan whose two most recent
  change sets — the round-4 repairs and the P3 retirement restructure — had never
  been reviewed. Every one was accepted; two were accepted in reduced form. All
  sixteen citations were ground-checked against live code before judgment, and
  each one held.
  The largest single defect is a sequencing error this coordinator introduced
  when it wrote 3.8: retirement was made to depend on the marker flip rather than
  the reverse, so for the length of P3 the legacy generator would have stamped
  old-shape pages with the new marker — the exact identity 4.3's prune and 5.3's
  quarantine use to tell the families apart. The fix is a dependency inversion
  and nothing else: 3.8 now depends only on 3.1 and 3.2, 3.4 depends on 3.8, and
  the P3 exit gate moves to 3.6, which is the phase's terminal leaf. The
  reviewer also asked for a race test proving no legacy writer can run during the
  flip; that was declined, because the ordering makes the interleaving
  unreachable rather than unlikely and a test for an impossible schedule is
  mechanism guarding nothing. The inversion pays for itself twice: the three
  legacy sites that pinned the old marker sit inside the subtree whose module
  declaration 3.8 now drops, so they are no longer compiled and the flip shrinks
  to the gcore golden plus the gwiki fixtures.
  Retirement grew in the two directions the reviewer proved it had to. "Present
  but unreachable" is a module-graph property, not a CLI property, so 3.8 drops
  the subtree's `pub mod` declaration and the files sit on disk uncompiled until
  5.2. And the Python side is a call graph rather than a method: gateway,
  refresh service, debounce trigger, startup construction, service-container
  field, both legacy routes, the nightly cron, and the git-hook body all go
  together, because each exists only to reach the command being removed. Route
  retirement moved out of 4.2 into 3.8 for the same reason — the routes have no
  implementation left once the trigger is gone — and 4.2 correspondingly picks up
  the service construction 3.8 vacated plus the two-URL frontend repoint. The
  wiki tab renders its empty state for the length of the window, which is stated
  rather than discovered.
  The remaining accepted findings each reuse mechanism the plan already had. The
  queued successor gains a canonical requested scope merged by union with FULL
  dominance, alongside the maximum-mode field round 4 added, because scope is an
  additive claim no diff can rebuild. Planning unions its mode-selected work with
  the inventory it cannot otherwise repair, so the run that detects a missing page
  is the run that closes it and the detector stops being a livelock. The run lease
  gets an explicit `lease_expires_at` and a single guarded takeover UPDATE
  following the daemon's existing dispatch-lease shape. Generated delete separates
  from generated write: `--expected-hash` is the removal authority and
  `--expected-absent` is an idempotent no-op. File summaries key on project as
  well as path and hash, and get an always-on production pass independent of the
  optional symbol-summary switch. Quarantine selects by positive legacy identity
  and protects missing, unknown, and foreign markers. 4.3 picks up the executor
  and watchdog lifecycle registration, the one-time bundled-manifest regeneration
  as the terminal shared-assets leaf, and a `_meta/truth_digest.json` writer so
  retiring the legacy generator does not silently disable memory dream's
  re-judging. 5.3 names `vault_migration.py` as the owner of its supervised
  procedure.
  Declined in part: the incarnation counter and per-mutation fence
  R5-run-lease-takeover proposed on top of the lease. Three guards already stand
  between a revived owner and a byte on disk — the executing partial unique index,
  the item-row `FOR UPDATE` fence checked before the write, and 3.5's
  expected-state precondition rechecked under the page lock — so a fourth would
  guard a state that cannot be reached. Also reduced: R5-quarantine-rollback's
  staged-write subsystem became one more list in the move manifest 5.3 already
  writes, recording each rewritten page's prior hash and bytes, with rollback
  restoring both families and reindexing.
  No new section, phase, or subsystem was created; the plan remains at 29
  sections, acyclic, with both validation modes passing. Findings per round are
  now 17, 12, 7, 7, 16 — the jump is a coverage artifact of two unreviewed change
  sets, not a regression in convergence, and round 6 reviews the repairs above.

```json plan-review-round
{"evidence_id":"4df0c982-b062-45a4-91ce-ce08e5a88984","plan_hash":"685394f4ee83985b055b81e166a8d9d8830a2bc87d8fdb10f3ed8a4fc7bb0a17","round_number":5,"round_result":{"candidates_examined":27,"coverage_attestation":{"adjacent_variant_complete":true,"attestation_digest":"4f2e8b0b55922b6d108f886179367e9e226c7554cb2ae639a53fb1f2cf9705bc","cross_lane_interaction_complete":true,"disposition_counts":{"dismissed":11,"emitted_findings":16,"total":27},"evidence_id":"4df0c982-b062-45a4-91ce-ce08e5a88984","lanes":[{"candidate_count":9,"lane_id":"requirements_traceability","status":"completed"},{"candidate_count":10,"lane_id":"repository_blast_radius","status":"completed"},{"candidate_count":8,"lane_id":"runtime_invariants","status":"completed"}],"shadow_manifest_status":{"entry_count":29,"manifest_digest":"ba9420952cd5c14362afc13a367e23bacc18ea4f468b7eb8fb2a8f9f88dc04c9","status":"valid"},"source_digest":"33a511f2775957311d9367c0c86bf15f9a62cbc500ea2b964e1a4a3451d99cc1","version":1},"evidence_id":"4df0c982-b062-45a4-91ce-ce08e5a88984","findings":[{"category":"bad-sequencing","check_key":"retirement-before-shared-marker-flip","description":"The plan flips the single shared marker in 3.4 (`.gobby/plans/wiki-codewiki-restructure.md:621-705`) but 3.8 depends on 3.4 and performs retirement only afterward (`.gobby/plans/wiki-codewiki-restructure.md:855-906`). The legacy generator reads that shared constant (`crates/gcode/src/commands/codewiki/text/frontmatter.rs:261`), while installed hooks can still invoke its route (`src/gobby/cli/installers/git_hooks.py:51`). A run in that interval emits old-format pages stamped `gwiki-code`, making them indistinguishable from new output to pruning and quarantine.","failure_trace":{"action":"Execute the plan as written at 3.4 marker flip / 3.8 retirement and P3 exit gate.","citation":[{"path":"crates/gcode/src/commands/codewiki/text/frontmatter.rs","sha256":"e21ff4bc22887fb9423c2a31b80b2fd6f03c27d37d5813fc1acaadc4b5a3d53f"}],"preconditions":"Section 3.8 is a terminal P3 fan-in that depends on 3.4, so the shared marker flips while every legacy entry point is still live.","violated_obligation":"An old producer must be unreachable before a shared ownership marker is reassigned to its replacement.","wrong_outcome":"The plan flips the single shared marker in 3.4 (`.gobby/plans/wiki-codewiki-restructure.md:621-705`) but 3.8 depends on 3.4 and performs retirement only afterward (`.gobby/plans/wiki-codewiki-restructure.md:855-906`). The legacy generator reads that shared constant (`crates/gcode/src/commands/codewiki/text/frontmatter.rs:261`), while installed hooks can still invoke its route (`src/gobby/cli/installers/git_hooks.py:51`). A run in that interval emits old-format pages stamped `gwiki-code`, making them indistinguishable from new output to pruning and quarantine."},"finding_id":"R5-retirement-marker-order","location":"3.4 marker flip / 3.8 retirement and P3 exit gate","minimal_repair":"Use the existing leaves: make 3.8 depend only on the copy/reimplementation prerequisites, make 3.4 depend on 3.8, and move the P3 fan-in smoke gate to the terminal 3.6 leaf. Add a race test proving no legacy writer can run when the marker flips.","prevention":"For a shared discriminator, order producer shutdown before changing the discriminator and put the phase gate on a later leaf.","principle":"An old producer must be unreachable before a shared ownership marker is reassigned to its replacement.","repair_scope":"existing_sections","root_cause":"Section 3.8 is a terminal P3 fan-in that depends on 3.4, so the shared marker flips while every legacy entry point is still live.","section_id":"3.8","severity":"blocking"},{"category":"missing-requirement","check_key":"retired-command-public-library-export","description":"`gobby-code` publicly exports `commands` (`crates/gcode/src/lib.rs:1`), `commands` publicly declares `codewiki` (`crates/gcode/src/commands/mod.rs:1`), and that subtree reexports runnable generation, repair, purge, and citation functions (`crates/gcode/src/commands/codewiki/mod.rs:247`). Removing only `Command::Codewiki` and its dispatch arm therefore violates 3.8.4's 'subtree present but unreachable' premise.","failure_trace":{"action":"Execute the plan as written at 3.8 Retire gcode codewiki / crates/gcode module exports.","citation":[{"path":"crates/gcode/src/commands/mod.rs","sha256":"7e3e882d0e43f290b588cf8b252671c7af9cff477e1aa74563046c3c4ef39dbb"}],"preconditions":"3.8 removes CLI and dispatch wiring but leaves `commands::codewiki` publicly compiled until physical deletion in 5.2.","violated_obligation":"A retired command subtree kept as source reference must be unreferenced by every compiled public API.","wrong_outcome":"`gobby-code` publicly exports `commands` (`crates/gcode/src/lib.rs:1`), `commands` publicly declares `codewiki` (`crates/gcode/src/commands/mod.rs:1`), and that subtree reexports runnable generation, repair, purge, and citation functions (`crates/gcode/src/commands/codewiki/mod.rs:247`). Removing only `Command::Codewiki` and its dispatch arm therefore violates 3.8.4's 'subtree present but unreachable' premise."},"finding_id":"R5-retirement-public-export","location":"3.8 Retire gcode codewiki / crates/gcode module exports","minimal_repair":"Move removal of the `pub mod codewiki` declaration and codewiki-only CLI/dispatch tests and CI selectors into 3.8. Keep the physical subtree files on disk, unreferenced, until 5.2 deletes them.","prevention":"When retiring a Rust command, sweep CLI, dispatch, module declarations, public reexports, tests, contracts, assets, and CI selectors separately from physical file deletion.","principle":"A retired command subtree kept as source reference must be unreferenced by every compiled public API.","repair_scope":"existing_sections","root_cause":"3.8 removes CLI and dispatch wiring but leaves `commands::codewiki` publicly compiled until physical deletion in 5.2.","section_id":"3.8","severity":"blocking"},{"category":"missing-requirement","check_key":"legacy-entrypoint-call-graph-sweep","description":"The live refresh service calls `GcodeGateway.codewiki` (`src/gobby/code_index/codewiki_refresh.py:89`), the old HTTP route schedules that trigger (`src/gobby/servers/routes/code_index.py:457`), app startup constructs it (`src/gobby/servers/_app_lifecycle.py:74`), runner initialization registers its nightly cron (`src/gobby/runner_init/orchestration.py:497`), and the Wiki UI still calls the old status/refresh endpoints (`web/src/components/activity/wiki/WikiTabData.ts:705`, `:740`). As written, 3.8 leaves callable or broken legacy paths and a broken UI.","failure_trace":{"action":"Execute the plan as written at 3.8 daemon teardown / 4.2 replacement routes.","citation":[{"path":"src/gobby/code_index/codewiki_refresh.py","sha256":"944ed30c34239da9042967410e8d1deeb688c366421bf82a327594e132c52df7"}],"preconditions":"3.8 names the obvious gateway, nightly, and hook surfaces but omits live Python and web consumers that do not fail at compile time.","violated_obligation":"Deleting a gateway method requires every dynamic caller, route, lifecycle registration, UI caller, and test double to be retired or replaced in the same ordered change.","wrong_outcome":"The live refresh service calls `GcodeGateway.codewiki` (`src/gobby/code_index/codewiki_refresh.py:89`), the old HTTP route schedules that trigger (`src/gobby/servers/routes/code_index.py:457`), app startup constructs it (`src/gobby/servers/_app_lifecycle.py:74`), runner initialization registers its nightly cron (`src/gobby/runner_init/orchestration.py:497`), and the Wiki UI still calls the old status/refresh endpoints (`web/src/components/activity/wiki/WikiTabData.ts:705`, `:740`). As written, 3.8 leaves callable or broken legacy paths and a broken UI."},"finding_id":"R5-retirement-call-graph","location":"3.8 daemon teardown / 4.2 replacement routes","minimal_repair":"Expand 3.8's teardown inventory to the refresh service/protocol and fakes, trigger construction, old routes, service-container field, runner/nightly registration, UI API calls, and focused tests. Reintroduce only the new admission/status surface in 4.2 and 4.6.","prevention":"Trace command retirement from every external trigger through construction, service protocol, route, client, and lifecycle owner.","principle":"Deleting a gateway method requires every dynamic caller, route, lifecycle registration, UI caller, and test double to be retired or replaced in the same ordered change.","repair_scope":"existing_sections","root_cause":"3.8 names the obvious gateway, nightly, and hook surfaces but omits live Python and web consumers that do not fail at compile time.","section_id":"3.8","severity":"blocking"},{"category":"weak-testability","causal_finding_id":"R4-marker-authority-incomplete","causal_section_ids":["3.4"],"check_key":"semantic-fixture-reclassification","description":"Audit generation authority is exact equality with the shared marker (`crates/gwiki/src/audit/claims.rs:197`). Existing tests carrying literal `gcode-codewiki` assert that template, degraded, and generated pages are grounded or exempt (`crates/gwiki/src/audit/tests.rs:408-537`). After 3.4 flips the constant while leaving those literals untouched, those assertions reverse and the crate tests fail; they are not mere parser fixtures.","failure_trace":{"action":"Execute the plan as written at 3.4 marker fixture sweep / crates/gwiki audit tests.","citation":[{"path":"crates/gwiki/src/audit/tests.rs","sha256":"b5a536f82143f61a7588bf1a4845a990cb4cb0ea54d1473fa34e501ccc0950df"}],"preconditions":"The repair classifies all remaining gwiki legacy literals as inert fixture data, but existing audit fixtures use the literal to activate generated-page grounding behavior.","violated_obligation":"A marker flip must update semantic fixtures whose assertions depend on marker equality; parser-only literals may remain old.","wrong_outcome":"Audit generation authority is exact equality with the shared marker (`crates/gwiki/src/audit/claims.rs:197`). Existing tests carrying literal `gcode-codewiki` assert that template, degraded, and generated pages are grounded or exempt (`crates/gwiki/src/audit/tests.rs:408-537`). After 3.4 flips the constant while leaving those literals untouched, those assertions reverse and the crate tests fail; they are not mere parser fixtures."},"finding_id":"R5-audit-marker-fixtures","introduced_in_round":4,"location":"3.4 marker fixture sweep / crates/gwiki audit tests","minimal_repair":"Update the semantic audit fixtures to the new marker (or the shared constant), keep only true parser fixtures on the legacy literal, and retain one explicit legacy-literal negative test.","prevention":"For every old-marker literal, classify it as parser fixture, semantic fixture, golden, or production comparison before deciding whether it remains unchanged.","principle":"A marker flip must update semantic fixtures whose assertions depend on marker equality; parser-only literals may remain old.","repair_scope":"existing_sections","root_cause":"The repair classifies all remaining gwiki legacy literals as inert fixture data, but existing audit fixtures use the literal to activate generated-page grounding behavior.","section_id":"3.4","severity":"blocking"},{"category":"unhandled-edge","causal_finding_id":"R4-successor-intent-loss","causal_section_ids":["4.2","4.7"],"check_key":"successor-scope-monotonicity","description":"Scope is explicit request state (`src/gobby/code_index/codewiki_refresh.py:37`) and is forwarded independently of mode (`src/gobby/code_index/codewiki_refresh.py:89`). The plan's replacement row retains the newest commit, recomputed diff, and maximum mode but lists no canonical scope state. `scope X` followed by auto/Y or `scope Y` can therefore silently lose requested pages, including on restart before planning.","failure_trace":{"action":"Execute the plan as written at 4.2 admission payload / 4.7 successor replacement.","citation":[{"path":"src/gobby/code_index/codewiki_refresh.py","sha256":"944ed30c34239da9042967410e8d1deeb688c366421bf82a327594e132c52df7"}],"preconditions":"The newest-target/diff/max-mode repair covers changed files and scalar mode, but the public `scope` path set is not persisted or merged.","violated_obligation":"A coalescing successor must monotonically retain every request field that affects work identity.","wrong_outcome":"Scope is explicit request state (`src/gobby/code_index/codewiki_refresh.py:37`) and is forwarded independently of mode (`src/gobby/code_index/codewiki_refresh.py:89`). The plan's replacement row retains the newest commit, recomputed diff, and maximum mode but lists no canonical scope state. `scope X` followed by auto/Y or `scope Y` can therefore silently lose requested pages, including on restart before planning."},"finding_id":"R5-successor-scope-intent","introduced_in_round":4,"location":"4.2 admission payload / 4.7 successor replacement","minimal_repair":"Persist canonical requested-scope state on active and queued rows; define FULL dominance and otherwise monotone union (or promote incompatible scopes to FULL). Test scope-X→auto-Y, scope-X→scope-Y, FULL→scope, and stronger same-commit requests across restart.","prevention":"Enumerate every admission payload field and define its durable representation and join operation before declaring coalescing monotone.","principle":"A coalescing successor must monotonically retain every request field that affects work identity.","repair_scope":"existing_sections","root_cause":"The newest-target/diff/max-mode repair covers changed files and scalar mode, but the public `scope` path set is not persisted or merged.","section_id":"4.7","severity":"blocking"},{"category":"unhandled-edge","causal_finding_id":"R4-desired-inventory-contract","causal_section_ids":["4.2","4.3","4.7"],"check_key":"desired-inventory-detection-without-repair","description":"A page can disappear independently through the existing delete path (`crates/gwiki/src/commands/page.rs:137`; gateway `src/gobby/gwiki_gateway.py:301`). After a successful baseline, a COSMETIC, PARTIAL, ARCHITECTURE, or manual-scope run can omit an unaffected missing module/concept/tour/overview page. The new inventory check then repeatedly withholds freshness, but no automatic run is guaranteed to enqueue that page, producing a stable livelock.","failure_trace":{"action":"Execute the plan as written at 4.3 desired inventory planning/finalization.","citation":[{"path":"crates/gwiki/src/commands/page.rs","sha256":"f79bfefe2c90d20ebd4c701113b2fe3e699e03fef3a9ff8f84fbdc03ceacc91b"}],"preconditions":"The repair withholds freshness when inventory is incomplete but does not add missing inventory members to mode-selected work.","violated_obligation":"A freshness invariant that detects a missing desired artifact must schedule a transition that can recreate it.","wrong_outcome":"A page can disappear independently through the existing delete path (`crates/gwiki/src/commands/page.rs:137`; gateway `src/gobby/gwiki_gateway.py:301`). After a successful baseline, a COSMETIC, PARTIAL, ARCHITECTURE, or manual-scope run can omit an unaffected missing module/concept/tour/overview page. The new inventory check then repeatedly withholds freshness, but no automatic run is guaranteed to enqueue that page, producing a stable livelock."},"finding_id":"R5-inventory-missing-page-repair","introduced_in_round":4,"location":"4.3 desired inventory planning/finalization","minimal_repair":"At planning, union mode-selected work with `desired_inventory - valid_materialized_inventory`; render missing deterministic pages and enqueue missing semantic pages as mandatory work. Add one missing-page recovery case for every page family under non-FULL modes.","prevention":"For every finalizer refusal, identify the earlier planner transition that makes the refused state recoverable.","principle":"A freshness invariant that detects a missing desired artifact must schedule a transition that can recreate it.","repair_scope":"existing_sections","root_cause":"The repair withholds freshness when inventory is incomplete but does not add missing inventory members to mode-selected work.","section_id":"4.3","severity":"blocking"},{"category":"unhandled-edge","causal_finding_id":"R4-stale-executing-run","causal_section_ids":["4.2","4.3","4.7"],"check_key":"run-owner-restart-fencing","description":"A daemon restart creates a new process (`src/gobby/servers/routes/admin/_lifecycle.py:237`), so literal owner equality cannot resume a row; permissive reassignment lets a paused old owner act after takeover. The repository's existing lease precedent uses explicit expiry plus one guarded mutation (`src/gobby/dispatch/lease_cleanup.py:41`), but the plan names only `owner` and `heartbeat_at`. The promised restart resumption/terminalization is therefore ambiguous and race-prone.","failure_trace":{"action":"Execute the plan as written at 4.2 restart semantics / 4.7 owner and heartbeat.","citation":[{"path":"src/gobby/dispatch/lease_cleanup.py","sha256":"d73552986cb3c8d2a32bf45c4c07465338fb8c78f771d5f1b3212dd615d13af6"}],"preconditions":"The owner/heartbeat repair terminalizes expired rows but does not define owner incarnation, lease expiry, takeover CAS, or an ownership fence for resumption.","violated_obligation":"Recovering a unique active row across process restart requires explicit expiry, atomic ownership transfer, and a fence on every later mutation.","wrong_outcome":"A daemon restart creates a new process (`src/gobby/servers/routes/admin/_lifecycle.py:237`), so literal owner equality cannot resume a row; permissive reassignment lets a paused old owner act after takeover. The repository's existing lease precedent uses explicit expiry plus one guarded mutation (`src/gobby/dispatch/lease_cleanup.py:41`), but the plan names only `owner` and `heartbeat_at`. The promised restart resumption/terminalization is therefore ambiguous and race-prone."},"finding_id":"R5-run-lease-takeover","introduced_in_round":4,"location":"4.2 restart semantics / 4.7 owner and heartbeat","minimal_repair":"Within 4.7, define owner incarnation, `lease_expires_at`, conditional takeover that increments a monotonic fence, and require owner+fence on heartbeats and state transitions. Test concurrent watchdogs and a paused old owner.","prevention":"Specify the complete lease state machine: identity, expiry, guarded takeover, fencing, heartbeat, terminalization, and old-owner rejection.","principle":"Recovering a unique active row across process restart requires explicit expiry, atomic ownership transfer, and a fence on every later mutation.","repair_scope":"existing_sections","root_cause":"The owner/heartbeat repair terminalizes expired rows but does not define owner incarnation, lease expiry, takeover CAS, or an ownership fence for resumption.","section_id":"4.7","severity":"blocking"},{"category":"unhandled-edge","causal_finding_id":"R4-marker-authority-incomplete","causal_section_ids":["3.4","5.3"],"check_key":"destructive-negative-ownership-predicate","description":"`generated_by` is optional in live frontmatter (`crates/gwiki/src/frontmatter.rs:62`), while current generated recognition uses positive equality (`crates/gwiki/src/audit/claims.rs:197`). The plan's negative quarantine predicate therefore selects unmarked operator-authored pages and output from unrelated tools, causing data loss.","failure_trace":{"action":"Execute the plan as written at 3.4 marker policy / 5.3 quarantine inventory.","citation":[{"path":"crates/gwiki/src/frontmatter.rs","sha256":"0e3abb9d8ec4be7e5ccd767062c8f06f48e6978d6e3b3dec1f01a04e730a8ebc"}],"preconditions":"The repair selects `generated_by != current-marker` as legacy, conflating every absent, malformed, manual, and foreign marker with the retired generator.","violated_obligation":"Destructive migration authority must be proven by a positive known identity, never by inequality with the current identity.","wrong_outcome":"`generated_by` is optional in live frontmatter (`crates/gwiki/src/frontmatter.rs:62`), while current generated recognition uses positive equality (`crates/gwiki/src/audit/claims.rs:197`). The plan's negative quarantine predicate therefore selects unmarked operator-authored pages and output from unrelated tools, causing data loss."},"finding_id":"R5-negative-marker-quarantine","introduced_in_round":4,"location":"3.4 marker policy / 5.3 quarantine inventory","minimal_repair":"Build the manifest from the exact `gcode-codewiki` literal plus an explicit allowlist of known markerless legacy artifacts. Protect missing/unknown/foreign markers and add mixed-marker dry-run tests.","prevention":"Test destructive selectors against current, exact legacy, missing, malformed, and third-party marker cases.","principle":"Destructive migration authority must be proven by a positive known identity, never by inequality with the current identity.","repair_scope":"existing_sections","root_cause":"The repair selects `generated_by != current-marker` as legacy, conflating every absent, malformed, manual, and foreign marker with the retired generator.","section_id":"5.3","severity":"blocking"},{"category":"unhandled-edge","causal_finding_id":"R4-generated-create-precondition","causal_section_ids":["3.5","3.6"],"check_key":"delete-existing-state-cas","description":"The live delete path checks existence then removes without a hash precondition (`crates/gwiki/src/commands/page.rs:137`), and the gateway accepts only a path (`src/gobby/gwiki_gateway.py:301`). Section 3.6.6 says generated delete forwards exactly one of expected-hash or expected-absent, but expected-absent provides no authority to delete an existing file. A stale finalizer can therefore delete a replacement unless the existing hash is rechecked under the page lock.","failure_trace":{"action":"Execute the plan as written at 3.5 mutation contract / 3.6 gateway / 4.3 prune.","citation":[{"path":"crates/gwiki/src/commands/page.rs","sha256":"f79bfefe2c90d20ebd4c701113b2fe3e699e03fef3a9ff8f84fbdc03ceacc91b"}],"preconditions":"The create repair threads `expected-absent` into both write and delete plumbing even though absence cannot authorize deletion of an existing page.","violated_obligation":"Generated deletion of an existing page needs an expected-existing state checked under the same lock immediately before removal.","wrong_outcome":"The live delete path checks existence then removes without a hash precondition (`crates/gwiki/src/commands/page.rs:137`), and the gateway accepts only a path (`src/gobby/gwiki_gateway.py:301`). Section 3.6.6 says generated delete forwards exactly one of expected-hash or expected-absent, but expected-absent provides no authority to delete an existing file. A stale finalizer can therefore delete a replacement unless the existing hash is rechecked under the page lock."},"finding_id":"R5-generated-delete-cas","introduced_in_round":4,"location":"3.5 mutation contract / 3.6 gateway / 4.3 prune","minimal_repair":"Define generated delete separately: an existing page requires `--expected-hash`; `--expected-absent` may only yield an idempotent absent no-op. Thread this through CLI/API/gateway and race a stale prune against a replacement write.","prevention":"Specify create, replace, delete, absent-delete replay, mismatch, and concurrent-replacement CAS branches separately.","principle":"Generated deletion of an existing page needs an expected-existing state checked under the same lock immediately before removal.","repair_scope":"existing_sections","root_cause":"The create repair threads `expected-absent` into both write and delete plumbing even though absence cannot authorize deletion of an existing page.","section_id":"3.6","severity":"blocking"},{"category":"missing-requirement","check_key":"multitenant-summary-identity","description":"The live `code_indexed_files` relation carries `project_id` and a project index (`src/gobby/storage/postgres_baseline_schema.sql:1568-1584`). A summary key of only path plus content hash collides for two projects containing the same path and bytes, allowing coverage/failure state to leak or overwrite across projects.","failure_trace":{"action":"Execute the plan as written at 2.2 FactsBundle summaries / 2.4 storage / 2.5 coverage counters.","citation":[{"path":"src/gobby/storage/postgres_baseline_schema.sql","sha256":"fd8b7380c4b9ed342d5f21add31f5aa890579c9f95b786507f94ab04837a1e92"}],"preconditions":"The plan repeatedly specifies `(path, content_hash)` while the indexed-file source of truth is project-scoped.","violated_obligation":"Durable derived data keyed by repository path must include the owning project scope.","wrong_outcome":"The live `code_indexed_files` relation carries `project_id` and a project index (`src/gobby/storage/postgres_baseline_schema.sql:1568-1584`). A summary key of only path plus content hash collides for two projects containing the same path and bytes, allowing coverage/failure state to leak or overwrite across projects."},"finding_id":"R5-file-summary-project-key","location":"2.2 FactsBundle summaries / 2.4 storage / 2.5 coverage counters","minimal_repair":"Define summary identity as `(project_id, path, content_hash)` or project-scoped indexed-file ID plus hash. Add a two-project fixture with identical paths/hashes and independent stale, failure, summary, and counter behavior.","prevention":"Trace all proposed keys back to the tenant key of their source table and add cross-project collision fixtures.","principle":"Durable derived data keyed by repository path must include the owning project scope.","repair_scope":"existing_sections","root_cause":"The plan repeatedly specifies `(path, content_hash)` while the indexed-file source of truth is project-scoped.","section_id":"2.4","severity":"blocking"},{"category":"missing-requirement","check_key":"mandatory-summary-production-wiring","description":"Production maintenance currently requests only unsummarized symbols (`src/gobby/code_index/maintenance.py:259`), and summary production is constructed only when optional `symbol_summary.enabled` is true (`src/gobby/runner_lifecycle_subsystems.py:477`). A FULL run's mandatory file-summary coverage cannot rely on that optional gate, so the plan can ship storage and tests without a worker that ever fills it.","failure_trace":{"action":"Execute the plan as written at 2.4 per-file summaries / daemon maintenance lifecycle.","citation":[{"path":"src/gobby/runner_lifecycle_subsystems.py","sha256":"98af8fd03bd7c6912dfcf4c9258415eb7681e0e688a8ea117875619a94ecfb80"}],"preconditions":"The plan adds summary storage and backfill logic but omits the live maintenance scheduler and lifecycle construction seam.","violated_obligation":"A mandatory freshness prerequisite must have an always-on production lifecycle independent of an optional adjacent feature.","wrong_outcome":"Production maintenance currently requests only unsummarized symbols (`src/gobby/code_index/maintenance.py:259`), and summary production is constructed only when optional `symbol_summary.enabled` is true (`src/gobby/runner_lifecycle_subsystems.py:477`). A FULL run's mandatory file-summary coverage cannot rely on that optional gate, so the plan can ship storage and tests without a worker that ever fills it."},"finding_id":"R5-file-summary-lifecycle","location":"2.4 per-file summaries / daemon maintenance lifecycle","minimal_repair":"Add `maintenance.py`, `runner_lifecycle_subsystems.py`, config, and reindex/sync completion seams to 2.4. Define mandatory file-summary backfill independent of optional symbol summaries and test startup, invalidation, failure recording, and disabled-symbol-summary configurations.","prevention":"Trace every mandatory producer from configuration through construction, scheduling, reindex invalidation, retries, and shutdown.","principle":"A mandatory freshness prerequisite must have an always-on production lifecycle independent of an optional adjacent feature.","repair_scope":"existing_sections","root_cause":"The plan adds summary storage and backfill logic but omits the live maintenance scheduler and lifecycle construction seam.","section_id":"2.4","severity":"blocking"},{"category":"traceability","check_key":"shared-assets-bundle-manifest-fanin","description":"The installer recursively hashes every eligible shared file (`src/gobby/install/manifest.py:61`), and `tests/test_build_backend.py:508` requires the committed `bundled_content_manifest.json` to match exactly. The planned shared files therefore make an existing repository test fail and leave packaged integrity unaware of them.","failure_trace":{"action":"Execute the plan as written at 3.7 and 4.3-4.5 shared assets.","citation":[{"path":"src/gobby/install/manifest.py","sha256":"f2cfa336aaa464a034e3ab351522a7ae3286110880f5eb1a91ba1494958e7d27"}],"preconditions":"The plan adds a manifest, query corpus, four templates, and two agent files under `install/shared` but assigns no owner for the generated bundle manifest.","violated_obligation":"Every installed shared-content change must update the committed content-addressed manifest before its phase gate.","wrong_outcome":"The installer recursively hashes every eligible shared file (`src/gobby/install/manifest.py:61`), and `tests/test_build_backend.py:508` requires the committed `bundled_content_manifest.json` to match exactly. The planned shared files therefore make an existing repository test fail and leave packaged integrity unaware of them."},"finding_id":"R5-bundled-content-manifest","location":"3.7 and 4.3-4.5 shared assets","minimal_repair":"Use an existing terminal shared-assets leaf (4.5 is sufficient after its dependencies) to regenerate `src/gobby/install/bundled_content_manifest.json` and run `test_committed_bundled_content_manifest_matches_shared_tree`; include the earlier 3.7 asset in its fan-in.","prevention":"For generated inventories, place regeneration after all producing leaves and name the exact drift test.","principle":"Every installed shared-content change must update the committed content-addressed manifest before its phase gate.","repair_scope":"existing_sections","root_cause":"The plan adds a manifest, query corpus, four templates, and two agent files under `install/shared` but assigns no owner for the generated bundle manifest.","section_id":"4.5","severity":"blocking"},{"category":"missing-requirement","check_key":"long-lived-service-lifecycle-owner","description":"The current service container owns the legacy trigger but no generation service/executor/watchdog (`src/gobby/app_context.py:101`); application lifecycle constructs only the legacy trigger (`src/gobby/servers/_app_lifecycle.py:74`), and the lifecycle subsystem registry has no new executor (`src/gobby/runner_lifecycle_subsystems.py:799`). Admissions can therefore create durable rows that no process drains, and startup recovery has no caller.","failure_trace":{"action":"Execute the plan as written at 4.2 service / 4.3 executor / 4.7 watchdog.","citation":[{"path":"src/gobby/app_context.py","sha256":"d2a87ea1e045e2189803263722405255b5691f50c030cd6fbc4404ef5bb6cb91"}],"preconditions":"The plan specifies service internals and routes but omits the current app-context and lifecycle registry targets that make a long-lived subsystem run.","violated_obligation":"A durable queue executor and watchdog need an explicit daemon construction, startup, recovery, cancellation, and shutdown owner.","wrong_outcome":"The current service container owns the legacy trigger but no generation service/executor/watchdog (`src/gobby/app_context.py:101`); application lifecycle constructs only the legacy trigger (`src/gobby/servers/_app_lifecycle.py:74`), and the lifecycle subsystem registry has no new executor (`src/gobby/runner_lifecycle_subsystems.py:799`). Admissions can therefore create durable rows that no process drains, and startup recovery has no caller."},"finding_id":"R5-orchestrator-lifecycle","location":"4.2 service / 4.3 executor / 4.7 watchdog","minimal_repair":"Assign the existing app-context and lifecycle files to 4.2/4.3: construct dependencies, start executor/recovery/watchdog tasks, cancel and await them on shutdown, and expose degraded status. Add a daemon restart test from admitted-undrained row through recovery and successor promotion.","prevention":"For every daemon subsystem, trace dependency construction, container field, startup task, recovery pass, watchdog, stop ordering, and health reporting.","principle":"A durable queue executor and watchdog need an explicit daemon construction, startup, recovery, cancellation, and shutdown owner.","repair_scope":"existing_sections","root_cause":"The plan specifies service internals and routes but omits the current app-context and lifecycle registry targets that make a long-lived subsystem run.","section_id":"4.3","severity":"blocking"},{"category":"missing-requirement","check_key":"retired-producer-downstream-consumer","description":"Memory dream reads exactly `_meta/truth_digest.json` (`src/gobby/memory/dream/truth_digest.py:28`), returns no project truth when it is missing (`:169`), and skips cooldown invalidation (`src/gobby/memory/dream/service.py:326`). The only current producer is the legacy generator (`crates/gcode/src/commands/codewiki/truth_digest.rs:84`). Deleting the subtree silently disables a live downstream feature.","failure_trace":{"action":"Execute the plan as written at 4.3 finalizer / 5.2 legacy deletion / memory dream.","citation":[{"path":"src/gobby/memory/dream/truth_digest.py","sha256":"6fab986eeaeeeadbea4c294ff3ab9f7582bddfb59d26f8cb3222c4fdde7a18e6"}],"preconditions":"The legacy subtree is the only producer of `_meta/truth_digest.json`, but the new finalizer specifies other metadata and no memory-dream migration.","violated_obligation":"Before deleting a producer, every live downstream artifact consumer must have a replacement producer or a coordinated migration.","wrong_outcome":"Memory dream reads exactly `_meta/truth_digest.json` (`src/gobby/memory/dream/truth_digest.py:28`), returns no project truth when it is missing (`:169`), and skips cooldown invalidation (`src/gobby/memory/dream/service.py:326`). The only current producer is the legacy generator (`crates/gcode/src/commands/codewiki/truth_digest.rs:84`). Deleting the subtree silently disables a live downstream feature."},"finding_id":"R5-truth-digest-cutover","location":"4.3 finalizer / 5.2 legacy deletion / memory dream","minimal_repair":"Before 5.2, either make the new finalizer emit a compatible bounded truth digest from the pinned bundle/run or migrate memory-dream consumers and fixtures to a declared `build.json` field. Add cutover acceptance for truth-change detection and cooldown reset.","prevention":"Trace every artifact emitted by a retired subtree to all non-subtree readers and tests.","principle":"Before deleting a producer, every live downstream artifact consumer must have a replacement producer or a coordinated migration.","repair_scope":"existing_sections","root_cause":"The legacy subtree is the only producer of `_meta/truth_digest.json`, but the new finalizer specifies other metadata and no memory-dream migration.","section_id":"5.2","severity":"blocking"},{"category":"missing-requirement","check_key":"destructive-migration-production-owner","description":"The only live purge implementation directly removes files and metadata (`crates/gcode/src/commands/codewiki/purge.rs:68`); current `gwiki page delete` is likewise a single-file removal (`crates/gwiki/src/commands/page.rs:137`). Neither is the durable supervised protocol 5.3 describes, and no new implementation target owns that protocol, so the deliverable cannot be expanded into executable work as written.","failure_trace":{"action":"Execute the plan as written at 5.3 Vault migration.","citation":[{"path":"crates/gcode/src/commands/codewiki/purge.rs","sha256":"fdb1408decb1fd505e058d0889f6ab6662a94067dd8c12e5fba60aa1684ce49f"}],"preconditions":"5.3 targets the vault path while specifying journaling, rename recovery, link rewriting, lint, reindex, probes, restore/resume, and deletion without assigning executable code.","violated_obligation":"A crash-recoverable destructive procedure needs a named production implementation and invocation surface, not only data targets and tests.","wrong_outcome":"The only live purge implementation directly removes files and metadata (`crates/gcode/src/commands/codewiki/purge.rs:68`); current `gwiki page delete` is likewise a single-file removal (`crates/gwiki/src/commands/page.rs:137`). Neither is the durable supervised protocol 5.3 describes, and no new implementation target owns that protocol, so the deliverable cannot be expanded into executable work as written."},"finding_id":"R5-vault-migration-owner","location":"5.3 Vault migration","minimal_repair":"Within 5.3, name and target one production migration module and supervised CLI/service invocation that owns manifest persistence, each rename, resume/restore, link rewrites, lint/reindex/probes, and final quarantine deletion. Point the existing 5.3 tests at it; no new phase or subsystem is needed.","prevention":"For every operational migration, name the production module/command and point all fault-injection acceptance at that surface.","principle":"A crash-recoverable destructive procedure needs a named production implementation and invocation surface, not only data targets and tests.","repair_scope":"existing_sections","root_cause":"5.3 targets the vault path while specifying journaling, rename recovery, link rewriting, lint, reindex, probes, restore/resume, and deletion without assigning executable code.","section_id":"5.3","severity":"blocking"},{"category":"unhandled-edge","check_key":"rollback-restores-all-mutated-state","description":"Restoring quarantined files after a partial link rewrite does not restore the rewritten surviving pages, and restoring after reindex/query failure also leaves the restored vault needing another reindex. The repository already has a durable atomic staged-write pattern (`crates/gwiki/src/sources/atomic.rs:7`), but 5.3 does not journal or stage the rewrite targets. Its promised restore is therefore not a return to the pre-migration state.","failure_trace":{"action":"Execute the plan as written at 5.3 quarantine, link rewrite, validation, and restore.","citation":[{"path":"crates/gwiki/src/sources/atomic.rs","sha256":"3fcb8a6a7f9d0344bd0fd5ebe06c7d9b47db80246dd3c687f0ff9d8ffd799618"}],"preconditions":"The move manifest covers quarantined legacy files, but later link rewrites mutate surviving pages and reindex mutates derived state outside that journal.","violated_obligation":"Rollback must restore every mutation made after the recovery point and rebuild derived state before declaring the old state recovered.","wrong_outcome":"Restoring quarantined files after a partial link rewrite does not restore the rewritten surviving pages, and restoring after reindex/query failure also leaves the restored vault needing another reindex. The repository already has a durable atomic staged-write pattern (`crates/gwiki/src/sources/atomic.rs:7`), but 5.3 does not journal or stage the rewrite targets. Its promised restore is therefore not a return to the pre-migration state."},"finding_id":"R5-quarantine-rollback","location":"5.3 quarantine, link rewrite, validation, and restore","minimal_repair":"Extend the same migration journal to rewritten pages with before-hashes and recoverable staged bytes, or stage all rewrites and commit only after complete validation. On rollback restore both file families and reindex before success; inject failure after each rewritten page and each later check.","prevention":"Draw the migration transaction boundary and inject failure after every mutation family, not only every file move.","principle":"Rollback must restore every mutation made after the recovery point and rebuild derived state before declaring the old state recovered.","repair_scope":"existing_sections","root_cause":"The move manifest covers quarantined legacy files, but later link rewrites mutate surviving pages and reindex mutates derived state outside that journal.","section_id":"5.3","severity":"blocking"}],"plan_hash":"685394f4ee83985b055b81e166a8d9d8830a2bc87d8fdb10f3ed8a4fc7bb0a17","reviewer_session":"f5399ee8-c968-439d-b4f7-d4d933a570ca","round":5,"round_number":5,"verdict":"needs_review"},"session_id":"4007d890-c17e-494e-86e4-56df0567ab02"}
```

## Task Mapping
`kind: framing`

<!-- Updated after task creation -->
| Plan Item | Task Ref | Status |
|-----------|----------|--------|
