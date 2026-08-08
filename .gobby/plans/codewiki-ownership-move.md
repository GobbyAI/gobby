# Move CodeWiki Ownership from gcode to gwiki

> **Plan ID:** codewiki-ownership-move
> **Epic:** #19668 (umbrella #19670)

## Overview
`kind: framing`

Relocate the CodeWiki engine from `gcode codewiki` to `gwiki code`, route all of
its datastore access through a new typed read-only facade
(`gobby_code::codewiki_facts`), and make every daemon-driven CodeWiki generation
path dormant. #19668 is the sequencing precondition for the whole
repository-intelligence redesign: it directly blocks #18902, which blocks
#17678/#19664/#19671/#19672. The moved legacy engine stays runnable for
isolated tests and manual use only; the redesigned engine (#19664) is authored
later as new files and the daemon re-enable belongs to #19665.

Josh's ruling shaping this plan: existing generated wiki content is disposable
("I don't care about anything the current wiki has generated. we can pause any
jobs that maintain it."), and the facade is built properly, not shortcut ("I'd
rather do it right than do it fast").

## Constraints
`kind: framing`

- **Confirmed Decision Record (2026-08-06)** is the authority for scope calls;
  its decisions are restated inline where they bind.
- **Facade done right.** `gobby_code::codewiki_facts` exposes typed read-only
  queries derived from a call-site inventory of the engine; gcode's datastore
  modules become private; no connection handles, `Context`, or credentials
  cross the crate boundary. The facade is the seed surface #17678 completes;
  the new engine's renderer input remains #17678's
  `gobby_core::code_facts::FactsBundle` — no second facts system.
- **Provenance marker flips writer-side with no reader compatibility.**
  `GENERATED_BY_CODEWIKI` (`gcode-codewiki`) becomes `GENERATED_BY_GWIKI_CODE`
  (`gwiki-code`) and gwiki's hardcoded `gcode-codewiki` literals are replaced by
  the new constant. Existing generated pages lose special recognition in
  lint/audit/indexing until #18790's cutover — accepted consequence.
- **Generated-content maintenance pauses.** Nightly codewiki cron rows are
  disabled at startup with no registered handler; the on-commit trigger and its
  installer git-hook curl are removed; jobs dedicated to generated `code/**`
  content stop. Shared jobs that maintain `knowledge/**` keep running and must
  not expand onto `code/**` as a side effect of the marker flip.
- **Dormant daemon surface**: `GET /api/wiki/code/status` returns HTTP 200 with
  exactly `{"enabled": false, "state": "disabled", "reason": "pending_wiki_redesign"}`
  (the E3-minimal shape recorded in V1 — no legacy live-only fields);
  `POST /api/wiki/code/refresh` returns
  HTTP 409 with stable error code `codewiki_disabled_pending_redesign`; legacy
  `/api/code-index/codewiki/*` routes are removed without aliases; no
  `GwikiGateway` execution operation is added (that is #19665's cutover).
  Auth mirrors today's semantics: refresh carries the agent-route table entry;
  status stays unlisted (open).
- **Sibling boundaries** (coordinated with session #10218, 2026-08-06): the
  moved legacy engine and its publication mechanics move as-is; new-engine
  transactional publication, engine entrypoint/composition, `catalog.rs`
  decomposition, and `outputs/graph.json` ownership are #19664's; live
  daemon→gwiki adapter rewiring is #19665's. The moved engine must NOT occupy
  `crates/gwiki/src/code_wiki/` — that namespace is reserved for #19664's new
  engine; the legacy move lands under `crates/gwiki/src/commands/code/`.
  Confirmed no-touch surfaces for this plan (per #10218's consistency reply,
  2026-08-06): `crates/gwiki/src/catalog.rs`, the `AI_README_TEMPLATE` in
  `crates/gwiki/src/vault.rs`, and `outputs/graph.json` with its writers
  (`crates/gwiki/src/exports/graph.rs` and the graph modules). The `gwiki code`
  command namespace is transitional: #19665/#19664 may reassign or retire it
  with the legacy tree, so the gwiki contract bump is not a permanent surface
  commitment.
- **Versions**: patch-bump policy applied to current versions — gobby-code
  1.6.0→1.6.1, gobby-core 0.9.1→0.9.2, gobby-wiki 0.9.0→0.9.1, gobby-daemon
  0.3.0→0.3.1, gobby-hooks 0.8.3→0.8.4; every internal `gobby-core` requirement
  moves to 0.9.2; gwiki's `gobby-code` requirement enters at the current 1.6.0
  in 2.1 and moves to 1.6.1 atomically with the package bump in 4.1;
  `Cargo.lock`, CLI contract
  JSONs (gcode `contract_version` 2→3, gwiki 15→16), and vendored fixtures
  regenerate. Re-derive the exact numbers at implementation time if crates have
  moved again; the policy (patch bumps, aligned internal reqs) is the contract,
  not the literals. Python Gobby stays 0.5.0.
- **Parity is the move gate**: deterministic `--ai off` output must be
  byte-identical between the pre-move `gcode codewiki` and post-move
  `gwiki code` in isolated temporary vaults. The complete normalization
  exception list — the only differences the parity comparison may strip — is:
  (a) the intentional post-move diffs: `generated_by:` frontmatter values and
  `gcode-codewiki` marker strings; (b) the unavoidable per-run stamps the
  engine emits unconditionally: `commit`, `commit_dirty`, and
  generation-timestamp fields in page frontmatter, in `_meta/codewiki.json`,
  and in the truth digest (the engine derives them from `git rev-parse HEAD`,
  `git status --porcelain`, and wall-clock time); (c) synchronization-only
  lock state: `_meta/codewiki.lock` (PID, acquisition time) is excluded from
  the hashed manifest entirely. Every remaining artifact byte is hashed as-is.
  1.1 and 2.6 implement exactly this list through the single normalization
  implementation in `scripts/codewiki_parity_baseline.sh`; neither may strip
  anything not enumerated here. Production wiki files stay byte-for-byte
  untouched throughout.
- **Monolith ceiling**: every hand-maintained production file below 1,000
  lines; `diagram_compose.rs` (1,062) is decomposed inside the same leaf that
  moves it (2.1);
  `crates/gwiki/src/catalog.rs` (1,316, over-ceiling) must not be edited by any
  task in this plan.
- **No backward compatibility** (pre-0.5.0): no route aliases, no forwarding
  CLI command, no stale-git-hook tolerance work, no old-marker readers.
- Rust work loads the `rust` skill; conventions per `crates/CLAUDE.md`. Changed
  crate behavior is live only after rebuild **and reinstall** of
  `~/.gobby/bin/{gcode,gwiki,gdaemon,ghook}`. Python tests run focused with
  `GOBBY_TEST_PROTECT=1`; never the full pytest suite.
- Direct `gwiki code` execution against the production vault is
  documentation-paused, no hard guard: worst-case writes are confined to
  disposable `code/**` and `_meta` content.

## P1: Facts facade and parity baseline
`kind: framing`

**Goal**: A typed read-only query boundary exists in gobby-code, gcode's
datastore internals are private, and the legacy engine's deterministic output
is captured as the parity baseline before anything moves.

### 1.1 Capture the deterministic parity baseline [category: test]
`kind: deliverable`

Targets:
- `scripts/codewiki_parity_baseline.sh`
- `crates/gwiki/tests/fixtures/codewiki_parity/README.md`
- `crates/gwiki/tests/fixtures/codewiki_parity/baseline.sha256`
- `crates/gwiki/tests/fixtures/codewiki_parity/project/README.md`
- `crates/gwiki/tests/fixtures/codewiki_parity/project/Cargo.toml`
- `crates/gwiki/tests/fixtures/codewiki_parity/project/src/lib.rs`

Create `scripts/codewiki_parity_baseline.sh`: runs a codewiki generation with
`--ai off` against a small committed fixture project — a minimal Rust crate:
`project/Cargo.toml` plus `project/src/lib.rs` holding a few documented
symbols with at least one intra-crate call, beside
`crates/gwiki/tests/fixtures/codewiki_parity/project/README.md`. The Rust
source is what makes the fixture indexable at all: markdown is not a detected
symbol language (gcode routes `.md` to content-only indexing —
`crates/gcode/src/index/languages.rs` pins
`markdown_extensions_are_not_detected`), so the README alone could anchor no
symbols, per-file hashes, or topology. The committed fixture file set is
exactly the enumerated Targets (README.md, Cargo.toml, src/lib.rs), it never
changes during the move, and the input-identity digest below covers exactly
that file set. Generation runs into a fresh temporary vault (isolated
out-dir, never the production vault),
indexing the fixture into an isolated temporary index/database identity
first. The fixture is what makes pre-move and post-move outputs comparable at
all: page identity, per-file hashes, symbols, and workspace topology all
derive from the documented project's paths and bytes, so running parity
against this repository — whose sources move mid-plan — would diff for
reasons no normalization can strip. The script then normalizes and hashes the
output; it is the single normalization implementation for the whole parity
gate. It takes an enumerated engine mode selecting which binary generates into
the temp vault — `--engine gcode` (pre-move, runs `gcode codewiki`) and
`--engine gwiki` (post-move, runs `gwiki code`) are the only two callers — and
both modes share the one manifest-producing path. Each mode builds its binary
from the workspace with `cargo build --locked` and executes the built target
path directly — never a PATH-resolved installed binary — and records the git
revision and reported binary version as a capture record (non-hashed
metadata). Everything under
`crates/gwiki/tests/fixtures/codewiki_parity/project/` is immutable indexed
input: the one-time `--engine gcode` capture writes its provenance record to
the outer fixture README
(`crates/gwiki/tests/fixtures/codewiki_parity/README.md`), and `--engine
gwiki` writes its capture metadata only into the temporary run directory.
Neither mode writes anything inside `project/`, so no comparison run can
mutate its own input. Before generating, the script computes a digest of the
fixture project's sources and refuses to run when it differs from the digest
pinned in the outer README (input-identity gate). Each mode also asserts its
own permitted output set afterwards: both modes leave every byte under
`project/` unchanged and that subtree clean, the one-time `--engine gcode`
capture may additionally write exactly the outer README and
`baseline.sha256` (its declared, then-committed outputs), and `--engine
gwiki` — which runs only after those artifacts are committed — leaves the
whole tracked fixture tree clean. Neither a dirty-state difference nor a
shifted source digest can therefore leak into generated output. Normalization
implements exactly the canonical exception list in Constraints ("Parity is
the move gate") and nothing else: (a) the intentional post-move diffs —
`generated_by:` frontmatter values and any `gcode-codewiki` marker strings;
(b) the per-run stamps the engine emits unconditionally and which no fixture
can hold constant — `commit:`, `commit_dirty:`, and the wall-clock
`generated:` frontmatter lines, plus the matching `commit` / `commit_dirty` /
generation-timestamp fields in `_meta/codewiki.json` and in the truth-digest
artifact (the engine derives these from `git rev-parse HEAD` and
`git status --porcelain` against the enclosing repository and from local
wall-clock time, so they differ between the pre-move capture and every later
comparison run); (c) synchronization-only lock state — `_meta/codewiki.lock`
(PID, acquisition time) is excluded from the hashed manifest entirely, since
it can persist after a run and no normalizer controls its contents.
Everything else (page set, page bodies, per-file source hashes, remaining
`_meta` manifest structure, repair-behavior artifacts) is hashed as-is into a
manifest file
`crates/gwiki/tests/fixtures/codewiki_parity/baseline.sha256` (one
`<sha256>  <vault-relative-path>` line per generated file, sorted), committed
as the fixture the 2.6 parity test consumes. Because the manifest must be
reproducible for two identical runs of the same engine before it can compare
different engines, the capture procedure runs `--engine gcode` twice
back-to-back and refuses to commit a baseline unless both normalized
manifests are identical (same-engine reproducibility gate). The 2.6 parity test invokes this
same script in `--engine gwiki` mode rather than reimplementing normalization,
so the producers and the consumer of the manifest cannot drift. The README
documents the capture command, the engine modes, the normalization rules, and
the regeneration procedure. The baseline MUST be captured and committed (via
`--engine gcode`) before 2.4 removes `gcode codewiki`.

Execution ordering: the `gwiki code` command does not exist until 2.3, so
this leaf **implements** the `--engine gwiki` branch (parser, shared
normalization path, cleanliness assertions) but never executes it — its
acceptance items exercise only argument parsing for that mode. Actual
`--engine gwiki` execution, its cleanliness assertions, and the cross-engine
comparison are owned by 2.6, which depends on 2.3/2.4/2.5. Every acceptance
item below is therefore executable at this leaf's position in the dependency
graph.

**Acceptance:**

- 1.1.1 - Baseline script produces a sorted normalized hash manifest from an
  isolated temp vault and refuses to run against the configured production
  vault path. file: `scripts/codewiki_parity_baseline.sh`.
- 1.1.2 - Committed baseline manifest exists with one entry per generated file
  and documented normalization rules. file: `crates/gwiki/tests/fixtures/codewiki_parity/README.md`.
- 1.1.3 - The script accepts exactly the two enumerated engine modes
  (`--engine gcode`, `--engine gwiki`) and both share the one
  normalization/manifest path; no second normalization implementation exists.
  file: `scripts/codewiki_parity_baseline.sh`.
- 1.1.4 - The `--engine gcode` capture runs against the committed fixture
  project with an isolated index identity, executes only a freshly built
  `--locked` workspace binary, asserts the pinned fixture source digest
  before generating, and records revision/version capture metadata outside
  the hashed manifest and outside `project/`; the `--engine gwiki` branch is
  validated for argument parsing only (2.6 owns its execution).
  file: `crates/gwiki/tests/fixtures/codewiki_parity/README.md`.
- 1.1.5 - After the `--engine gcode` capture, every byte under
  `crates/gwiki/tests/fixtures/codewiki_parity/project/` is unchanged and
  that subtree clean, and no tracked file beyond the outer README and
  `baseline.sha256` is written. Two back-to-back `--engine gcode` runs
  produce identical normalized manifests (`_meta/codewiki.lock` excluded,
  truth-digest and commit/timestamp stamps normalized per the Constraints
  exception list) — the capture refuses to commit a baseline otherwise.
  file: `scripts/codewiki_parity_baseline.sh`.
- 1.1.6 - The committed fixture project contains the enumerated
  symbol-indexable Rust sources (`project/Cargo.toml`, `project/src/lib.rs`),
  the input-identity digest covers exactly the committed fixture file set,
  and the captured baseline manifest derives from a non-empty generated page
  set with per-file source hashes — proving the fixture actually yields
  indexed symbols and topology.
  file: `crates/gwiki/tests/fixtures/codewiki_parity/project/src/lib.rs`.

### 1.2 Implement the `codewiki_facts` facade in gobby-code [category: code]
`kind: deliverable`

Targets:
- `crates/gcode/src/codewiki_facts/mod.rs`
- `crates/gcode/src/codewiki_facts/scope.rs`
- `crates/gcode/src/codewiki_facts/symbols.rs`
- `crates/gcode/src/codewiki_facts/graph.rs`
- `crates/gcode/src/codewiki_facts/search.rs`
- `crates/gcode/src/codewiki_facts/text.rs`
- `crates/gcode/src/codewiki_facts/tests.rs`
- `crates/gcode/src/lib.rs::*` — scope-reason: register the new public facade module alongside the existing module declarations
- `crates/gcode/security/managed_postgres_privileges.json::*` — scope-reason: the exact privilege inventory gains the facade's new query call sites
- `tests/code_index/test_gcode_privilege_manifest.py::*` — scope-reason: rerun as focused validation; adjust classifications only if the recomputed inventory requires the facade paths

Derive the facade surface from a complete call-site inventory of
`crates/gcode/src/commands/codewiki/` (the engine currently imports
`crate::db` — `connect_readonly`, `id_param`, raw `postgres::Client` threading —
plus `crate::graph::typed_query`, `crate::models::Symbol`,
`crate::index::hasher`, `crate::visibility`, `crate::commands::scope`, and
`crate::config::Context`). Run the inventory first as a full `crate::` sweep
(`gcode grep 'crate::' crates/gcode/src/commands/codewiki`, including
grouped-import forms `use crate::{...}` — not just the datastore modules),
and classify every hit into exactly one bucket: datastore/query access → a
typed facade family below; project identity and output/quiet settings → the
engine runtime carrier that 2.1 defines in gwiki; AI/daemon routing and
policy → enumerated explicitly and carried by that runtime, never by the
facade. Tool-loop operations are not one bucket: the tool executor
(`tool_executor.rs`) is itself a datastore consumer — it owns a
`config::Context`, opens a raw `postgres::Client` (its `connection()`
helper, tool_executor.rs:88), and calls the private `commands::grep`,
`graph::code_graph`, `index::security`, `models`, `output`, `search::fts`,
`db`, and `visibility` modules — so each tool operation classifies
individually: database-backed operations (index search, symbol/outline
reads, grep, visibility- and security-filtered queries, graph access) map
to typed facade query families, extending the family set below until every
such operation is covered, while filesystem-only operations are implemented
inside the moved engine in gwiki. After 1.3 privatizes those modules, no
facade bypass can compile. The facade covers the datastore buckets only. The inventory
also covers the **command wrapper**, not just the engine tree: gcode's
dispatch guards the codewiki **generation path only** with
`ensure_project_fresh(&ctx, cli.no_freshness)` (dispatch.rs:611 →
`freshness::ensure_fresh(ctx, FreshnessScope::Project)`); the
`--compare-to`, `--purge`, and `--repair-citations` modes return before
that call (dispatch.rs:602-610) and never invoke freshness. The busy-path
diagnostic is quiet-dependent: `warn_if_busy` prints only when `ctx.quiet`
is false (dispatch.rs:74-77). This precondition lives outside
`commands/codewiki/` and would otherwise be silently dropped by the move.
Classify it as the facade module's single non-query admission helper:
expose a free function
`codewiki_facts::ensure_project_fresh(project_root, disabled)` returning an
owned re-exported `FreshnessStatus`, running gcode's existing project-scope
freshness path (it mutates only gcode's own index through gcode-internal
code; no handle, `Context`, or credential crosses the boundary); the
**caller** owns the busy warning under its own quiet setting — the facade
emits no diagnostics. Documented in the module header as the one deliberate
exception to the queries-only rule and pinned by its own boundary test. 2.3
consumes it on the generation path only, preserving the legacy branch
order. The
expected shape (adjust names to inventory findings, keep the boundary rules):

```rust
// crates/gcode/src/codewiki_facts/mod.rs
pub struct CodewikiFacts { /* private: cheap-clone Context only — no stored connections */ }

impl CodewikiFacts {
    /// Opens facts for a project root; resolves config/Context internally.
    pub fn open(project_root: &Path) -> Result<Self>;

    // scope.rs — scoped file/module facts
    pub fn scoped_files(&self, scope: &ScopeSelector) -> Result<Vec<FileFact>>;
    // symbols.rs — symbol facts incl. language/visibility/hash
    pub fn symbols_in(&self, files: &[FileId]) -> Result<Vec<SymbolFact>>;
    // graph.rs — typed graph outcomes (calls/imports/coupling)
    pub fn graph_availability(&self) -> GraphAvailability; // Available | Unavailable { reason }
    pub fn edges(&self, seed: &ScopeSelector, kind: EdgeKind) -> Result<GraphEdges>; // edges + truncation
    // search.rs — index-backed search
    pub fn search(&self, query: &str, limit: usize) -> Result<Vec<SearchHit>>;
    // text.rs — grep over indexed sources
    pub fn grep(&self, pattern: &str, scope: &ScopeSelector) -> Result<Vec<GrepHit>>;
}
```

Boundary rules (non-negotiable): every return type is a plain owned fact struct
defined in this module; no `postgres::Client`, `Context`, connection string,
or datastore configuration appears in any public signature; connections are
opened per query call, owned by that call, and read-only
(`db::connect_readonly`) — `CodewikiFacts` itself stores no connection and is
a cheap-clone, thread-safe (`Send + Sync`) context-only handle, so a
timed-out tool operation detached on a worker thread retains only its own
call's connection and can never block a later call (preserving the executor
invariant pinned at tool_executor.rs:65: detached timeout workers never
share connection state); no `CodewikiFacts`
query method performs a write. The graph family preserves every
caller-visible outcome state the engine distinguishes today — available,
truncated (edge-limit hit, `CodewikiGraph::truncated`), successful-empty,
unavailable (unconfigured or unreachable, with reason), and genuine error —
as typed results. The legacy mapping these states feed is deliberately
modest: bulk generation treats graph availability as informational only and
never marks a page degraded — the auto-generated mermaid diagrams that were
the sole source of `graph-truncated`/`graph-unavailable` page degradation
are retired (commands/codewiki/mod.rs:28) — a truncated graph surfaces only
as the deterministic diagrams' "source graph was truncated" disclosure note
(render/diagrams.rs:517), and the tool loop records an explicit
`graph-unavailable` evidence-degradation result for unconfigured or
unreachable graph tools (text/generation/outcome.rs:32, tool_executor.rs:11)
that is listed in page evidence without marking the page degraded
(text/frontmatter.rs:561), while genuine query failures stay ordinary tool
errors; the tool loop has no `graph-truncated` outcome. Collapsing the typed
states to a bare edge list would erase the inputs those behaviors consume;
introducing any richer degradation policy (for example a new
truncation-degradation marker) is out of scope for the move and deferred to
#17678. Indexing and projection writes remain owned by
gcode's existing internal paths; the module-level
`ensure_project_fresh` admission helper above is the single documented
exception, and it writes only through those existing internal indexing
paths. Document in the module header that this facade is the seed
surface #17678 ("Complete scoped code facts…") completes and that new-engine
renderers consume `gobby_core::code_facts::FactsBundle` instead.

Unit-test the facade against a temp-indexed fixture project inside
`crates/gcode` (module tests are fine; no new top-level test crate required).

**Acceptance:**

- 1.2.1 - Facade module exists with typed owned fact structs and lazy read-only
  access; no public signature exposes connections, `Context`, or credentials.
  file: `crates/gcode/src/codewiki_facts/mod.rs`.
- 1.2.2 - Every external dependency found by the full `crate::` inventory is
  classified (facade family, runtime carrier, or explicit AI-routing
  carry-over); every facade-bucket family has a typed method; and the
  inventory with its classification is recorded in the module header docs.
  file: `crates/gcode/src/codewiki_facts/mod.rs`.
- 1.2.3 - `CodewikiFacts` query methods perform no datastore writes: every
  query family uses a per-call read-only connection, and a test proves both
  the read-only connection use and that no connection state is stored on the
  handle. The module-level `ensure_project_fresh` admission helper is the
  sole indexing exception — it refreshes the project index only through
  gcode's existing internal indexing paths, returns an owned
  `FreshnessStatus`, and emits no diagnostics (the busy warning is
  caller-owned, generation-path only). symbol: `CodewikiFacts`.
- 1.2.4 - The managed-PostgreSQL privilege inventory records the facade's
  query call sites and the exact-inventory equality test passes against the
  updated registry.
  test: `tests/code_index/test_gcode_privilege_manifest.py`.
- 1.2.5 - The graph facade family exposes typed outcome states — available,
  truncated, successful-empty, unavailable (with reason), and genuine
  error — with one test per state: available non-empty (the ordinary path),
  unconfigured, unreachable, genuine error, exact-limit truncation, and
  successful-empty.
  test: `crates/gcode/src/codewiki_facts/tests.rs`.

### 1.3 Privatize gcode's datastore modules and pin the boundary [category: code] (depends: 1.2)
`kind: deliverable`

Targets:
- `crates/gcode/src/lib.rs::*` — scope-reason: flip datastore module visibility, absorb cli/dispatch behind one public entrypoint, and pin the public API set
- `crates/gcode/src/main.rs::*` — scope-reason: reduced to a thin caller of the public entrypoint
- `crates/gcode/src/cli.rs::*` — scope-reason: moves from the binary into the library as a private module
- `crates/gcode/src/dispatch.rs::*` — scope-reason: moves from the binary into the library behind the public entrypoint
- `crates/gcode/tests/facade_boundary.rs`
- `crates/gcode/tests/graph_standalone/local_c_cpp.rs::*` — scope-reason: db-touching cases relocate to the pinned in-crate destination
- `crates/gcode/tests/graph_standalone/support.rs::*` — scope-reason: typed-query helper relocates with its consumers
- `crates/gcode/tests/projection_stale.rs::*` — scope-reason: the models-constant case relocates to the pinned in-crate destination
- `crates/gcode/tests/vector_projection.rs::*` — scope-reason: config/models/vector internals cases relocate to the pinned in-crate destination
- `crates/gcode/src/graph/tests/standalone_db.rs`
- `crates/gcode/src/projection/tests/stale.rs`
- `crates/gcode/src/vector/tests/projection.rs`

The `gcode` binary is a separate crate: `main.rs` compiles its own `cli.rs`
and `dispatch.rs` and consumes the library through `gobby_code::` paths, so
`pub(crate)` cannot serve it — the binary's needs define the minimum public
surface. Move `cli.rs` and `dispatch.rs` into the library as private modules
behind one deliberately public entrypoint (`gobby_code::run_cli()`), and
reduce `main.rs` to calling that entrypoint. Then make `db`, `config`,
`graph`, `models`, `index`, `schema`, `search`, `vector`, and `projection`
private (`pub mod` → `mod`, or `pub(crate)` where sibling modules require
it), keeping public only: `run_cli`, `commands` (until 2.4 removes the
codewiki variant handling), `codewiki_facts`, `contract`, `test_env`, and
whatever the existing lib.rs API-pinning
test (`crates/gcode/src/lib.rs:47-78`) already declares as the projection
boundary — reconcile that pin with the new visibility rather than deleting it.

The crate's integration tests are external consumers too, and each
private-API consumer resolves to exactly one in-crate destination — the
enduring public `test_env` API is not expanded, and no choice is left to the
implementing leaf: the `graph_standalone` db-touching cases
(`db::resolve_local_callee_symbol_id`, local_c_cpp.rs:163) move together
with `support.rs`'s `typed_query::string_params` helper to
`crates/gcode/src/graph/tests/standalone_db.rs`; `projection_stale.rs`'s
`models::CODE_INDEX_UUID_NAMESPACE` case (projection_stale.rs:26) moves to
`crates/gcode/src/projection/tests/stale.rs`; and `vector_projection.rs`'s
config/models/vector-internals cases move to
`crates/gcode/src/vector/tests/projection.rs` (each declared with the
workspace's `#[path]` unit-test convention). Relocated cases keep their
original test names and behavior; the cases remaining in each external suite
compile against public surfaces alone; `tests/contract.rs` keeps using the
deliberately public `contract` module.

Add `crates/gcode/tests/facade_boundary.rs`: a compile-time/API test asserting
the crate's public surface is exactly the intended set (facade + explicitly
pinned items), so a future `pub` leak fails CI.

**Acceptance:**

- 1.3.1 - Datastore modules are no longer `pub`; the binary is a thin caller
  of the public entrypoint; the crate, binary, and integration tests all
  compile with unchanged behavior, and the relocated cases keep their
  original test names and assertions in their pinned in-crate destinations.
  file: `crates/gcode/src/lib.rs`.
- 1.3.2 - Boundary test pins the public API set and fails on new leaks.
  test: `crates/gcode/tests/facade_boundary.rs::public_surface_is_pinned`.

## P2: Relocate the engine into gwiki
`kind: framing`

**Goal**: The complete legacy engine lives under `crates/gwiki`, is invoked as
`gwiki code`, reads only through `codewiki_facts`, emits the new provenance
marker, and proves parity; `gcode codewiki` is gone.

### 2.1 Move the codewiki engine under gobby-wiki behind the facade and decompose the diagram composer [category: code] (depends: P1)
`kind: deliverable`

Targets:
- `crates/gwiki/src/commands/code/mod.rs`
- `crates/gwiki/src/commands/code/runtime.rs`
- `crates/gwiki/src/commands/code/diagram_compose/mod.rs`
- `crates/gwiki/src/commands/code/diagram_compose/evidence.rs`
- `crates/gwiki/src/commands/code/diagram_compose/candidates.rs`
- `crates/gwiki/src/commands/code/diagram_compose/generation.rs`
- `crates/gwiki/src/commands/code/text/generation/tool_loop.rs`
- `crates/gwiki/src/commands/code/text/generation/outcome.rs`
- `crates/gwiki/src/commands/code/lock.rs`
- `crates/gwiki/src/commands/code/architecture_diagrams.rs`
- `crates/gwiki/src/commands/code/build_parts/audit.rs`
- `crates/gwiki/src/commands/code/build_parts/file.rs`
- `crates/gwiki/src/commands/code/build_parts/onboarding.rs`
- `crates/gwiki/src/commands/code/build_parts/snapshot.rs`
- `crates/gwiki/src/commands/code/compare.rs`
- `crates/gwiki/src/commands/code/generation.rs`
- `crates/gwiki/src/commands/code/graph.rs`
- `crates/gwiki/src/commands/code/ownership/analysis.rs`
- `crates/gwiki/src/commands/code/prompts/builders.rs`
- `crates/gwiki/src/commands/code/prompts/tests.rs`
- `crates/gwiki/src/commands/code/publication.rs`
- `crates/gwiki/src/commands/code/purge.rs`
- `crates/gwiki/src/commands/code/relationship_facts.rs`
- `crates/gwiki/src/commands/code/render/repo.rs`
- `crates/gwiki/src/commands/code/repair.rs`
- `crates/gwiki/src/commands/code/reuse_guard.rs`
- `crates/gwiki/src/commands/code/reuse.rs`
- `crates/gwiki/src/commands/code/run.rs`
- `crates/gwiki/src/commands/code/stubs.rs`
- `crates/gwiki/src/commands/code/system_model.rs`
- `crates/gwiki/src/commands/code/tests/concurrency.rs`
- `crates/gwiki/src/commands/code/tests/lock.rs`
- `crates/gwiki/src/commands/code/tests/support.rs`
- `crates/gwiki/src/commands/code/text/citations.rs`
- `crates/gwiki/src/commands/code/text/frontmatter.rs`
- `crates/gwiki/src/commands/code/text/generation/aggregate.rs`
- `crates/gwiki/src/commands/code/text/generation/one_shot.rs`
- `crates/gwiki/src/commands/code/text/generation/routing.rs`
- `crates/gwiki/src/commands/code/text/sanitize.rs`
- `crates/gwiki/src/commands/code/text/structural.rs`
- `crates/gwiki/src/commands/code/text/verify.rs`
- `crates/gwiki/src/commands/code/tool_executor.rs`
- `crates/gwiki/src/commands/code/types.rs`
- `crates/gwiki/src/commands/code/build.rs`
- `crates/gwiki/src/commands/code/cluster.rs`
- `crates/gwiki/src/commands/code/doc_paths.rs`
- `crates/gwiki/src/commands/code/frontmatter.rs`
- `crates/gwiki/src/commands/code/io.rs`
- `crates/gwiki/src/commands/code/ownership.rs`
- `crates/gwiki/src/commands/code/ownership_timeout_tests.rs`
- `crates/gwiki/src/commands/code/paths.rs`
- `crates/gwiki/src/commands/code/progress.rs`
- `crates/gwiki/src/commands/code/prompts.rs`
- `crates/gwiki/src/commands/code/render.rs`
- `crates/gwiki/src/commands/code/strict_markdown.rs`
- `crates/gwiki/src/commands/code/tests.rs`
- `crates/gwiki/src/commands/code/text.rs`
- `crates/gwiki/src/commands/code/truth_digest.rs`
- `crates/gwiki/src/commands/code/build_parts/architecture.rs`
- `crates/gwiki/src/commands/code/build_parts/changes.rs`
- `crates/gwiki/src/commands/code/build_parts/concepts.rs`
- `crates/gwiki/src/commands/code/build_parts/curated_content.rs`
- `crates/gwiki/src/commands/code/build_parts/features.rs`
- `crates/gwiki/src/commands/code/build_parts/hotspots.rs`
- `crates/gwiki/src/commands/code/build_parts/infrastructure.rs`
- `crates/gwiki/src/commands/code/build_parts/modules.rs`
- `crates/gwiki/src/commands/code/build_parts/concepts/plan.rs`
- `crates/gwiki/src/commands/code/build_parts/concepts/render.rs`
- `crates/gwiki/src/commands/code/build_parts/concepts/spans.rs`
- `crates/gwiki/src/commands/code/build_parts/concepts/support.rs`
- `crates/gwiki/src/commands/code/build_parts/concepts/types.rs`
- `crates/gwiki/src/commands/code/build_parts/curated_content/page_content.rs`
- `crates/gwiki/src/commands/code/build_parts/curated_content/tests.rs`
- `crates/gwiki/src/commands/code/build_parts/curated_content/tool_loop_dump.rs`
- `crates/gwiki/src/commands/code/ownership/codeowners.rs`
- `crates/gwiki/src/commands/code/ownership/render.rs`
- `crates/gwiki/src/commands/code/ownership/tests.rs`
- `crates/gwiki/src/commands/code/prompts/excerpts.rs`
- `crates/gwiki/src/commands/code/prompts/systems.rs`
- `crates/gwiki/src/commands/code/prompts/tables.rs`
- `crates/gwiki/src/commands/code/prompts/types.rs`
- `crates/gwiki/src/commands/code/render/audit.rs`
- `crates/gwiki/src/commands/code/render/common.rs`
- `crates/gwiki/src/commands/code/render/diagrams.rs`
- `crates/gwiki/src/commands/code/render/features.rs`
- `crates/gwiki/src/commands/code/render/infrastructure.rs`
- `crates/gwiki/src/commands/code/render/overview.rs`
- `crates/gwiki/src/commands/code/render/pages.rs`
- `crates/gwiki/src/commands/code/tests/ai.rs`
- `crates/gwiki/src/commands/code/tests/architecture.rs`
- `crates/gwiki/src/commands/code/tests/audit.rs`
- `crates/gwiki/src/commands/code/tests/changes.rs`
- `crates/gwiki/src/commands/code/tests/concepts.rs`
- `crates/gwiki/src/commands/code/tests/contract.rs`
- `crates/gwiki/src/commands/code/tests/features.rs`
- `crates/gwiki/src/commands/code/tests/graph.rs`
- `crates/gwiki/src/commands/code/tests/hotspots.rs`
- `crates/gwiki/src/commands/code/tests/incremental.rs`
- `crates/gwiki/src/commands/code/tests/infrastructure.rs`
- `crates/gwiki/src/commands/code/tests/invalidation.rs`
- `crates/gwiki/src/commands/code/tests/io_safety.rs`
- `crates/gwiki/src/commands/code/tests/modules.rs`
- `crates/gwiki/src/commands/code/tests/onboarding.rs`
- `crates/gwiki/src/commands/code/tests/progress.rs`
- `crates/gwiki/src/commands/code/tests/provenance.rs`
- `crates/gwiki/src/commands/code/tests/publication.rs`
- `crates/gwiki/src/commands/code/tests/purge.rs`
- `crates/gwiki/src/commands/code/tests/repair.rs`
- `crates/gwiki/src/commands/code/tests/reuse.rs`
- `crates/gwiki/src/commands/code/tests/truth_digest.rs`
- `crates/gwiki/src/commands/code/text/generation.rs`
- `crates/gwiki/src/commands/code/types/ai.rs`
- `crates/gcode/src/codewiki_facts/mod.rs`
- `crates/gcode/src/codewiki_facts/scope.rs`
- `crates/gcode/src/codewiki_facts/symbols.rs`
- `crates/gcode/src/codewiki_facts/graph.rs`
- `crates/gcode/src/codewiki_facts/search.rs`
- `crates/gcode/src/codewiki_facts/text.rs`
- `crates/gwiki/src/lib.rs::*` — scope-reason: register the moved engine module tree
- `crates/gwiki/Cargo.toml`
- `Cargo.lock`
- `crates/gwiki/src/api.rs::crate_has_no_gcode_dependency`
- `crates/gwiki/tests/code_engine_boundary.rs`

Add `gobby-code = { path = "../gcode", version = "1.6.0" }` (the current
version — 4.1 moves the package and this requirement to 1.6.1 atomically) to
`crates/gwiki/Cargo.toml` (the workspace's first gwiki→gcode dependency; keep
it one-way — gcode must not depend on gwiki), and regenerate `Cargo.lock` in
this same leaf: gwiki's current lock stanza lists `gobby-core` and no
`gobby-code`, so the new edge changes the lockfile, and 2.6 runs
`cargo build --locked` well before 4.1's version-bump regeneration. A focused
`cargo build --locked` must pass at this leaf's boundary. 4.1 still owns the
later regeneration for the version bump. The moved tree also carries two
direct external-crate imports gwiki does not declare: `pulldown_cmark`
(text/sanitize.rs:3) and `wait_timeout` (ownership/analysis.rs:9) — a
wholesale move cannot ride gcode's transitive dependencies, so add
`pulldown-cmark = "0.13"` and `wait-timeout = "0.2"` (gcode's current
requirements) to gwiki's `[dependencies]` in this same leaf; the
external-crate inventory rides the same sweep as the crate-qualified one,
and the leaf's locked build after lockfile regeneration is what proves the
dependency table complete.

**This leaf copies; 2.4 deletes.** The gcode tree stays in place here.
`crates/gcode/src/cli.rs` names four types from
`gobby_code::commands::codewiki` and `crates/gcode/src/dispatch.rs`
constructs `CodewikiAiOptions` and calls `run_compare`, `run_purge`,
`run_repair`, and `run`; those consumers are compiled and are not removed
until 2.4. Deleting the module here would leave a leaf that cannot compile,
so the old tree and its module declaration survive this leaf untouched and
2.4 removes the CLI references, the dispatch sites, the module declaration,
and the source tree together, atomically.

Copy the complete `crates/gcode/src/commands/codewiki/` tree (≈108 files:
`build_parts/`, `ownership/`, `prompts/`, `render/`, `text/`, `types/`,
`tests/`, and the top-level modules) to `crates/gwiki/src/commands/code/`,
preserving module structure, behavior, flags/defaults, page layout,
generation routes, locking, repair, comparison, purge, scope handling, and
incremental behavior. The relocation is wholesale and behavior-preserving:
every file under the source root lands under the destination root, and no
file is left behind or dropped. The Targets above enumerate the **complete
destination inventory** — every one of the 112 files this leaf creates
under `crates/gwiki/src/commands/code/`: the 107 one-to-one counterparts of
the source files, the four decomposed composer files replacing
`diagram_compose.rs`, and the new `runtime.rs` carrier — because every
created file requires its own Target and no file-level entry stands for a
directory tree. Within that inventory, every moved file is classified as
either **byte-identical** (relative `self::`/`super::` imports only —
copied unchanged, listed as a bare new-file path) or **semantically
changed** (contains crate-qualified references that must be rewired to the
facade, the runtime carrier, or the new `crate::commands::code` module
paths). The repository sweep for crate-qualified references over the source
tree (2026-08-08) finds exactly 35 files with such references
(`diagram_compose.rs` appears as its decomposed module) plus one file
semantically changed without any crate-qualified reference: the separately
registered `tests/lock.rs`, whose contention assertion (tests/lock.rs:15)
pins the diagnostic string the identity rename below changes. Alongside
those come the new module root and runtime carrier, the ownership-identity
files, and the gcode-side facade. A file gaining a crate-qualified
reference before implementation joins the changed set; the Target inventory
is already complete either way.
Acceptance 2.1.1 and 2.4.1 pin the wholesale outcome on both sides. Do NOT place anything under
`crates/gwiki/src/code_wiki/` — reserved for #19664's new engine.

The engine cannot compile without its diagram composer, so the 1,062-line
`diagram_compose.rs` is copied AND decomposed inside this same leaf — no
intermediate state ships an over-ceiling production file at the destination.
The over-ceiling source file is untouched here and disappears with the rest
of the old tree in 2.4. The copy lands as a
`diagram_compose/` module split along the provisional seams: `evidence.rs`
(evidence types, lookup, prompt rendering), `candidates.rs` (candidate
parsing, verification, component filtering, normalization), `generation.rs`
(generation, repair, outcomes, statistics), `mod.rs` (re-exports preserving
the call sites' API). Behavior-preserving, every resulting file below 1,000
lines. The adjacent architecture-diagrams module (765 lines) moves as-is and
is not split.

Replace `crate_has_no_gcode_dependency` (in `crates/gwiki/src/api.rs` — it
asserts gwiki must NOT depend on gobby-code and would fail this change) with
`dependency_direction_is_one_way`: gwiki depends on gobby-code with the
pinned path/version, and gcode has no gobby-wiki dependency.

Rewire every datastore access in the moved code through
`gobby_code::codewiki_facts` (the 1.2 facade): replace `crate::db` /
`crate::graph::typed_query` / `crate::models::Symbol` / `crate::index::hasher`
/ `crate::visibility` / `crate::config::Context` imports with facade calls; no
`postgres` dependency is added to gwiki for this engine. Where the engine
previously threaded a raw connection, thread the `CodewikiFacts` handle. If the
inventory in 1.2 missed a query family, extend the facade in the same change
(gcode side) rather than opening any datastore access from gwiki — that is why
the `crates/gcode/src/codewiki_facts/` files are Targets here, and their edits
are bounded to relocation-discovered typed families: any such extension also
updates the module-header classification and the read-only facade tests 1.2
established. No other gcode file is touched by this leaf for that reason.

The tool executor is rewired against an explicit **per-tool dependency
table** recorded in this leaf: for every tool operation the executor
exposes, the table names either the typed facade query family that replaces
the legacy private-module call (database-backed search via `search::fts`,
symbol/outline reads via `models`/`visibility`/`index::security`, grep via
`commands::grep`, graph access via `graph::code_graph`) or the gwiki-owned
filesystem implementation that absorbs it. The moved executor owns no
`Context`, exposes no `postgres::Client` in any signature (the legacy
`connection()` helper at tool_executor.rs:88 does not survive the move),
and imports no private gcode module — it depends on `CodewikiFacts` and the
runtime carrier alone. The executor's timeout path is part of this
contract: today each tool call opens its own connection precisely so
detached timeout workers never share connection state (the invariant
documented at tool_executor.rs:65), and the rewire preserves it — each tool
operation's facade call owns its per-call connection, no connection or
mutex is shared with a detached worker, and a first call that times out and
detaches never blocks a later call from completing. Graph-backed tool
operations consume the facade's typed outcome states
(available/truncated/successful-empty/unavailable/error) and preserve the
exact legacy mapping: an unavailable graph (unconfigured or unreachable)
yields the explicit `graph-unavailable` evidence-degradation result the
tool loop records today (text/generation/outcome.rs:32,
tool_executor.rs:11) — listed in page evidence, never marking the page
degraded (text/frontmatter.rs:561) — genuine query failures remain ordinary
tool errors, and no tool operation emits a `graph-truncated` outcome
because the tool loop has none today. Bulk generation keeps graph
availability informational only and never marks a page degraded
(mod.rs:28); truncation surfaces solely as the deterministic diagrams'
"source graph was truncated" disclosure note (render/diagrams.rs:517). The
move introduces no new degradation policy; richer truncation handling is
deferred to #17678.

Ownership identities embedded in the moved engine move with it. The daemon
agentic caller constant (`DAEMON_AGENTIC_CALLER`, today `"gcode.codewiki"`,
pinned by a stability test and by an outcome test fixture) becomes
`"gwiki.code"`, and the lock-contention diagnostics that tell users "another
gcode codewiki run is already writing" become "another `gwiki code` run"
(production message, both registered test assertions — the inline module
test at lock.rs:168 and the separately registered tests/lock.rs:15 — and
the module doc comment).
`ToolPolicy.cli` stays `"gcode"` — that field names the CLI whose read-only
query tools the loop executes, which remains gcode after the move, so it is
deliberately preserved rather than renamed. `code_engine_boundary.rs` pins all
three distinctions.

Non-datastore context threads through a new gwiki-owned carrier
`crates/gwiki/src/commands/code/runtime.rs`: a `CodeEngineRuntime` struct
holding project root/id, quiet/output settings, and the `CodewikiFacts`
handle, populated from 1.2's dependency classification. AI/daemon routing
and policy are carried explicitly on the runtime per that classification;
the tool loop's datastore-backed operations go through the facade query
families, and the facade stays queries-only (plus its single documented
admission helper).
`crates/gwiki/tests/code_engine_boundary.rs` pins the boundary (2.1.2) and
additionally covers that the moved engine compiles and dispatches across
AI-off/direct/daemon/tool-loop plus the compare, purge, and repair modes.

Moved tests come along (they are excluded from the monolith ceiling) and run
against temp vaults/fixture indexes as they do today.

**Acceptance:**

- 2.1.1 - Every file of the source tree has a counterpart under
  `crates/gwiki/src/commands/code/` with no file left behind or dropped, the
  workspace builds with `cargo build --locked`, and the still-present gcode
  tree and its consumers compile unchanged.
  file: `crates/gwiki/src/commands/code/mod.rs`.
- 2.1.2 - The moved engine contains zero direct datastore imports — no
  `gobby_code::db`, no `postgres::`, no `Context` construction — verified by a
  source-scan test in the gwiki crate. test: `crates/gwiki/tests/code_engine_boundary.rs::moved_engine_uses_only_facade`.
- 2.1.3 - gwiki depends on gobby-code one-way at the current version; gcode
  has no gwiki dependency. test: `crates/gwiki/src/api.rs::dependency_direction_is_one_way`.
- 2.1.4 - The composer exists as a decomposed module with all files below
  1,000 lines and unchanged public API; no over-ceiling production file
  exists at any point after this leaf.
  file: `crates/gwiki/src/commands/code/diagram_compose/mod.rs`.
- 2.1.5 - Existing diagram-composition tests pass unchanged after the split.
  behavior: "diagram composition parity" in `crates/gwiki/src/commands/code/diagram_compose/mod.rs`.
- 2.1.6 - The runtime carrier holds all non-datastore context (project
  identity, output settings, AI routing) and the boundary test covers
  AI-off/direct/daemon/tool-loop plus compare, purge, and repair modes.
  test: `crates/gwiki/tests/code_engine_boundary.rs::runtime_carries_non_datastore_context`.
- 2.1.7 - The daemon agentic caller is `gwiki.code` and lock-contention
  diagnostics name `gwiki code`, while `ToolPolicy.cli` remains `gcode`; all
  three are pinned.
  test: `crates/gwiki/tests/code_engine_boundary.rs::ownership_identities_moved`.
- 2.1.8 - The moved tool executor compiles against the facade alone: the
  per-tool dependency table is complete, and the boundary test proves no
  `Context` ownership, no `postgres::Client` in any signature, and no
  private gcode module import for every tool operation.
  test: `crates/gwiki/tests/code_engine_boundary.rs::tool_executor_uses_only_facade`.
- 2.1.9 - A tool operation that exceeds its timeout detaches with its own
  per-call connection, and a subsequent tool call completes while the
  detached worker is still running; no connection or mutex is shared with
  detached workers.
  test: `crates/gwiki/tests/code_engine_boundary.rs::tool_timeout_does_not_block_subsequent_calls`.
- 2.1.10 - Graph outcomes map exactly as before the move: an unavailable
  graph tool records the explicit `graph-unavailable` evidence-degradation
  result without marking the page degraded, no tool operation emits a
  `graph-truncated` outcome, bulk generation treats graph availability as
  informational only (no page-degradation marker is emitted), truncation
  surfaces solely as the deterministic diagrams' "source graph was
  truncated" note, and genuine failures surface as tool errors.
  test: `crates/gwiki/tests/code_engine_boundary.rs::graph_outcomes_match_legacy_mapping`.

### 2.3 Add the `gwiki code` CLI [category: code] (depends: 2.1)
`kind: deliverable`

Targets:
- `crates/gwiki/src/cli/code.rs`
- `crates/gwiki/src/cli.rs::*` — scope-reason: add the Code variant to CliCommand and extend the CLI_SUBCOMMANDS sync list
- `crates/gwiki/src/cli/tests.rs::*` — scope-reason: subcommand-sync and --project normalization tests enumerate every command
- `crates/gwiki/src/cli/tests/code.rs`
- `crates/gwiki/src/cli/mapping.rs::command_from_cli`
- `crates/gwiki/src/api.rs::Command`
- `crates/gwiki/src/commands/mod.rs::*` — scope-reason: module registration and the exhaustive dispatch arm
- `crates/gwiki/src/commands/project_admission.rs::classify_command`
- `crates/gwiki/src/commands/project_admission/tests.rs::*` — scope-reason: writer-arm classification pins gain the code command
- `crates/gwiki/src/contract.rs::*` — scope-reason: add the code command contract entries and bump contract_version
- `crates/gwiki/contract/gwiki.contract.json::*` — scope-reason: regenerated wholesale from the contract source
- `tests/contracts/gwiki.contract.json::*` — scope-reason: vendored copy synced wholesale
- `crates/gwiki/tests/cli_contract.rs::*` — scope-reason: update the contract_version pin
- `tests/test_cli_contracts.py::*` — scope-reason: update the gwiki contract_version pin

Add a `Code` variant to `CliCommand` with the SAME flat grammar the legacy
command has today — one command whose modes are flags (`--purge` +
`--force`, `--compare-to`, `--repair-citations`), not nested subcommands —
defined in a separate arg module `crates/gwiki/src/cli/code.rs`. Every flag,
default, value enum, and the full clap conflict matrix (the
`conflicts_with_all`/`requires` set at cli.rs:382-515) moves verbatim
(`--ai` route/depth/prose-depth/verify-scope/register arguments move with
their `From` impls). Map through `command_from_cli` to a new `Command::Code`
variant; the dispatch arm routes to the moved engine's
`run`/`run_compare`/`run_purge`/`run_repair` off the flag combination
exactly as gcode's dispatch does today.

The legacy wrapper's freshness precondition moves with the command and
keeps its legacy branch order: gcode's dispatch runs
`ensure_project_fresh(&ctx, cli.no_freshness)` only after the
`--compare-to`, `--purge`, and `--repair-citations` modes have returned
(dispatch.rs:602-611) — freshness guards generation alone. `gwiki code`
gains a `--no-freshness` switch (command-scoped, mirroring gcode's global
flag) and calls 1.2's
`codewiki_facts::ensure_project_fresh(project_root, disabled)` on the
generation path only, after the compare, purge, and repair branches exit:
fresh projects no-op, stale projects refresh the project-scope index,
`--no-freshness` bypasses exactly as the legacy flag did, and the returned
`FreshnessStatus` drives the busy warning in the gwiki CLI under its own
quiet setting (the legacy warning printed only when not quiet). Compare,
purge, and repair never invoke freshness and never mutate the index.

The command threads gwiki's full exhaustive-command architecture,
compile-enforced at every site: the public `Command` enum, module
registration and the exhaustive dispatch match, admission classification in
`classify_command`, the `CLI_SUBCOMMANDS` sync list and its clap-parity
tests, and a `CommandResult`/outcome renderer for the code modes. Admission
is **mode-aware and behavior-preserving**, not the generic writer path:
legacy `gcode codewiki` had no per-project admission layer — its only
concurrency guard is the engine's own per-output-directory lock (moved as
`crates/gwiki/src/commands/code/lock.rs` in 2.1) with its short timeout and
"another run is already writing" refusal. Classifying `Code` as a generic
`PersistentWriter` would swap that for gwiki's per-project long lock, gate
every mode behind a live project row, and block isolated or manual runs for
up to the project-lock timeout. Instead `classify_command` gives `Code` a
dedicated arm admitted without the generic per-project writer lock and
without requiring a live project row: the moved engine's per-output lock
remains the sole concurrency guard for generating modes, `--compare-to`
stays read-only, and isolated/manual runs (the only supported use until
#19665) are admitted as legacy gcode admitted them. The admission tests pin
the arm, the preserved lock identity/timeout under contention, the read-only
compare path, and the no-project-row admission. Only the codewiki parse
cases move from gcode's CLI tests into `crates/gwiki/src/cli/tests/code.rs`
(the unrelated setup case stays behind — see 2.4).

Update `crates/gwiki/src/contract.rs` with the `code` command contract
(mirroring the retired gcode codewiki entry's flat arguments), bump
`contract_version` 15→16, regenerate
`crates/gwiki/contract/gwiki.contract.json`, and sync the vendored copy
`tests/contracts/gwiki.contract.json` plus the version pins in
`crates/gwiki/tests/cli_contract.rs` and `tests/test_cli_contracts.py`.

**Acceptance:**

- 2.3.1 - `gwiki code` parses the full legacy FLAT flag surface with
  identical defaults and conflict matrix; the moved codewiki parse tests pass
  against the new paths. file: `crates/gwiki/src/cli/code.rs`.
- 2.3.2 - gwiki contract carries the `code` command at contract_version 16 with
  regenerated JSON and synced vendored fixture. file: `crates/gwiki/contract/gwiki.contract.json`.
- 2.3.3 - Every exhaustive command site (enum, dispatch, admission
  classification, CLI_SUBCOMMANDS, outcome rendering) carries `code`
  compile-enforced; the admission tests pin `Code`'s dedicated mode-aware
  arm — engine-owned per-output lock under contention (legacy identity and
  timeout), read-only `--compare-to`, and admission without a live project
  row. test: `crates/gwiki/src/commands/project_admission/tests.rs`.
- 2.3.4 - Freshness runs on the generation path only with legacy semantics:
  fresh project no-ops, stale project refreshes, `--no-freshness` bypasses,
  and the busy status warns only when not quiet; negative cases prove
  `--compare-to`, `--purge`, and `--repair-citations` never invoke the
  freshness helper and never mutate the index.
  test: `crates/gwiki/src/cli/tests/code.rs`.

### 2.4 Remove the `gcode codewiki` surface [category: code] (depends: 2.3)
`kind: deliverable`

Targets:
- `crates/gcode/src/cli.rs::*` — scope-reason: remove the Codewiki variant, its value enums, and From impls
- `crates/gcode/src/dispatch.rs::*` — scope-reason: remove the three codewiki dispatch sites (service-config arms, ai-options helper, execution arm)
- `crates/gcode/src/lib.rs::*` — scope-reason: make `commands` private now that its last external consumer is gone, and repin the enduring public API set
- `crates/gcode/src/commands/mod.rs::*` — scope-reason: remove the codewiki module declaration and reduce child-module visibility alongside the parent flip
- `crates/gcode/src/commands/codewiki/mod.rs::*` — scope-reason: the whole source tree is deleted here, after 2.1 copied it to gwiki; no symbol survives at this path
- `crates/gcode/src/commands/codewiki/diagram_compose.rs::*` — scope-reason: the over-ceiling source file is deleted with the tree; its decomposed counterpart already exists in gwiki
- `crates/gcode/src/commands/codewiki/architecture_diagrams.rs::*` — scope-reason: deleted with the tree
- `crates/gcode/src/commands/codewiki/build.rs`
- `crates/gcode/src/commands/codewiki/cluster.rs::*` — scope-reason: deleted with the tree
- `crates/gcode/src/commands/codewiki/compare.rs::*` — scope-reason: deleted with the tree
- `crates/gcode/src/commands/codewiki/doc_paths.rs::*` — scope-reason: deleted with the tree
- `crates/gcode/src/commands/codewiki/frontmatter.rs::*` — scope-reason: deleted with the tree
- `crates/gcode/src/commands/codewiki/generation.rs::*` — scope-reason: deleted with the tree
- `crates/gcode/src/commands/codewiki/graph.rs::*` — scope-reason: deleted with the tree
- `crates/gcode/src/commands/codewiki/io.rs::*` — scope-reason: deleted with the tree
- `crates/gcode/src/commands/codewiki/lock.rs::*` — scope-reason: deleted with the tree
- `crates/gcode/src/commands/codewiki/ownership.rs::*` — scope-reason: deleted with the tree
- `crates/gcode/src/commands/codewiki/ownership_timeout_tests.rs::*` — scope-reason: deleted with the tree
- `crates/gcode/src/commands/codewiki/paths.rs::*` — scope-reason: deleted with the tree
- `crates/gcode/src/commands/codewiki/progress.rs::*` — scope-reason: deleted with the tree
- `crates/gcode/src/commands/codewiki/prompts.rs`
- `crates/gcode/src/commands/codewiki/publication.rs::*` — scope-reason: deleted with the tree
- `crates/gcode/src/commands/codewiki/purge.rs::*` — scope-reason: deleted with the tree
- `crates/gcode/src/commands/codewiki/relationship_facts.rs::*` — scope-reason: deleted with the tree
- `crates/gcode/src/commands/codewiki/render.rs`
- `crates/gcode/src/commands/codewiki/repair.rs::*` — scope-reason: deleted with the tree
- `crates/gcode/src/commands/codewiki/reuse_guard.rs::*` — scope-reason: deleted with the tree
- `crates/gcode/src/commands/codewiki/reuse.rs::*` — scope-reason: deleted with the tree
- `crates/gcode/src/commands/codewiki/run.rs::*` — scope-reason: deleted with the tree
- `crates/gcode/src/commands/codewiki/strict_markdown.rs::*` — scope-reason: deleted with the tree
- `crates/gcode/src/commands/codewiki/stubs.rs::*` — scope-reason: deleted with the tree
- `crates/gcode/src/commands/codewiki/system_model.rs::*` — scope-reason: deleted with the tree
- `crates/gcode/src/commands/codewiki/tests.rs::*` — scope-reason: deleted with the tree
- `crates/gcode/src/commands/codewiki/text.rs::*` — scope-reason: deleted with the tree
- `crates/gcode/src/commands/codewiki/tool_executor.rs::*` — scope-reason: deleted with the tree
- `crates/gcode/src/commands/codewiki/truth_digest.rs::*` — scope-reason: deleted with the tree
- `crates/gcode/src/commands/codewiki/types.rs::*` — scope-reason: deleted with the tree
- `crates/gcode/src/commands/codewiki/build_parts/architecture.rs::*` — scope-reason: deleted with the tree
- `crates/gcode/src/commands/codewiki/build_parts/audit.rs::*` — scope-reason: deleted with the tree
- `crates/gcode/src/commands/codewiki/build_parts/changes.rs::*` — scope-reason: deleted with the tree
- `crates/gcode/src/commands/codewiki/build_parts/concepts.rs::*` — scope-reason: deleted with the tree
- `crates/gcode/src/commands/codewiki/build_parts/curated_content.rs::*` — scope-reason: deleted with the tree
- `crates/gcode/src/commands/codewiki/build_parts/features.rs::*` — scope-reason: deleted with the tree
- `crates/gcode/src/commands/codewiki/build_parts/file.rs::*` — scope-reason: deleted with the tree
- `crates/gcode/src/commands/codewiki/build_parts/hotspots.rs::*` — scope-reason: deleted with the tree
- `crates/gcode/src/commands/codewiki/build_parts/infrastructure.rs::*` — scope-reason: deleted with the tree
- `crates/gcode/src/commands/codewiki/build_parts/modules.rs::*` — scope-reason: deleted with the tree
- `crates/gcode/src/commands/codewiki/build_parts/onboarding.rs::*` — scope-reason: deleted with the tree
- `crates/gcode/src/commands/codewiki/build_parts/snapshot.rs::*` — scope-reason: deleted with the tree
- `crates/gcode/src/commands/codewiki/build_parts/concepts/plan.rs::*` — scope-reason: deleted with the tree
- `crates/gcode/src/commands/codewiki/build_parts/concepts/render.rs::*` — scope-reason: deleted with the tree
- `crates/gcode/src/commands/codewiki/build_parts/concepts/spans.rs::*` — scope-reason: deleted with the tree
- `crates/gcode/src/commands/codewiki/build_parts/concepts/support.rs::*` — scope-reason: deleted with the tree
- `crates/gcode/src/commands/codewiki/build_parts/concepts/types.rs::*` — scope-reason: deleted with the tree
- `crates/gcode/src/commands/codewiki/build_parts/curated_content/page_content.rs::*` — scope-reason: deleted with the tree
- `crates/gcode/src/commands/codewiki/build_parts/curated_content/tests.rs::*` — scope-reason: deleted with the tree
- `crates/gcode/src/commands/codewiki/build_parts/curated_content/tool_loop_dump.rs::*` — scope-reason: deleted with the tree
- `crates/gcode/src/commands/codewiki/ownership/analysis.rs::*` — scope-reason: deleted with the tree
- `crates/gcode/src/commands/codewiki/ownership/codeowners.rs::*` — scope-reason: deleted with the tree
- `crates/gcode/src/commands/codewiki/ownership/render.rs::*` — scope-reason: deleted with the tree
- `crates/gcode/src/commands/codewiki/ownership/tests.rs::*` — scope-reason: deleted with the tree
- `crates/gcode/src/commands/codewiki/prompts/builders.rs::*` — scope-reason: deleted with the tree
- `crates/gcode/src/commands/codewiki/prompts/excerpts.rs::*` — scope-reason: deleted with the tree
- `crates/gcode/src/commands/codewiki/prompts/systems.rs`
- `crates/gcode/src/commands/codewiki/prompts/tables.rs::*` — scope-reason: deleted with the tree
- `crates/gcode/src/commands/codewiki/prompts/tests.rs::*` — scope-reason: deleted with the tree
- `crates/gcode/src/commands/codewiki/prompts/types.rs::*` — scope-reason: deleted with the tree
- `crates/gcode/src/commands/codewiki/render/audit.rs::*` — scope-reason: deleted with the tree
- `crates/gcode/src/commands/codewiki/render/common.rs::*` — scope-reason: deleted with the tree
- `crates/gcode/src/commands/codewiki/render/diagrams.rs::*` — scope-reason: deleted with the tree
- `crates/gcode/src/commands/codewiki/render/features.rs::*` — scope-reason: deleted with the tree
- `crates/gcode/src/commands/codewiki/render/infrastructure.rs::*` — scope-reason: deleted with the tree
- `crates/gcode/src/commands/codewiki/render/overview.rs::*` — scope-reason: deleted with the tree
- `crates/gcode/src/commands/codewiki/render/pages.rs::*` — scope-reason: deleted with the tree
- `crates/gcode/src/commands/codewiki/render/repo.rs::*` — scope-reason: deleted with the tree
- `crates/gcode/src/commands/codewiki/tests/ai.rs::*` — scope-reason: deleted with the tree
- `crates/gcode/src/commands/codewiki/tests/architecture.rs::*` — scope-reason: deleted with the tree
- `crates/gcode/src/commands/codewiki/tests/audit.rs::*` — scope-reason: deleted with the tree
- `crates/gcode/src/commands/codewiki/tests/changes.rs::*` — scope-reason: deleted with the tree
- `crates/gcode/src/commands/codewiki/tests/concepts.rs::*` — scope-reason: deleted with the tree
- `crates/gcode/src/commands/codewiki/tests/concurrency.rs::*` — scope-reason: deleted with the tree
- `crates/gcode/src/commands/codewiki/tests/contract.rs::*` — scope-reason: deleted with the tree
- `crates/gcode/src/commands/codewiki/tests/features.rs::*` — scope-reason: deleted with the tree
- `crates/gcode/src/commands/codewiki/tests/graph.rs::*` — scope-reason: deleted with the tree
- `crates/gcode/src/commands/codewiki/tests/hotspots.rs::*` — scope-reason: deleted with the tree
- `crates/gcode/src/commands/codewiki/tests/incremental.rs::*` — scope-reason: deleted with the tree
- `crates/gcode/src/commands/codewiki/tests/infrastructure.rs::*` — scope-reason: deleted with the tree
- `crates/gcode/src/commands/codewiki/tests/invalidation.rs::*` — scope-reason: deleted with the tree
- `crates/gcode/src/commands/codewiki/tests/io_safety.rs::*` — scope-reason: deleted with the tree
- `crates/gcode/src/commands/codewiki/tests/lock.rs::*` — scope-reason: deleted with the tree
- `crates/gcode/src/commands/codewiki/tests/modules.rs::*` — scope-reason: deleted with the tree
- `crates/gcode/src/commands/codewiki/tests/onboarding.rs::*` — scope-reason: deleted with the tree
- `crates/gcode/src/commands/codewiki/tests/progress.rs::*` — scope-reason: deleted with the tree
- `crates/gcode/src/commands/codewiki/tests/provenance.rs::*` — scope-reason: deleted with the tree
- `crates/gcode/src/commands/codewiki/tests/publication.rs::*` — scope-reason: deleted with the tree
- `crates/gcode/src/commands/codewiki/tests/purge.rs::*` — scope-reason: deleted with the tree
- `crates/gcode/src/commands/codewiki/tests/repair.rs::*` — scope-reason: deleted with the tree
- `crates/gcode/src/commands/codewiki/tests/reuse.rs::*` — scope-reason: deleted with the tree
- `crates/gcode/src/commands/codewiki/tests/support.rs::*` — scope-reason: deleted with the tree
- `crates/gcode/src/commands/codewiki/tests/truth_digest.rs::*` — scope-reason: deleted with the tree
- `crates/gcode/src/commands/codewiki/text/citations.rs::*` — scope-reason: deleted with the tree
- `crates/gcode/src/commands/codewiki/text/frontmatter.rs::*` — scope-reason: deleted with the tree
- `crates/gcode/src/commands/codewiki/text/generation.rs`
- `crates/gcode/src/commands/codewiki/text/sanitize.rs::*` — scope-reason: deleted with the tree
- `crates/gcode/src/commands/codewiki/text/structural.rs::*` — scope-reason: deleted with the tree
- `crates/gcode/src/commands/codewiki/text/verify.rs::*` — scope-reason: deleted with the tree
- `crates/gcode/src/commands/codewiki/text/generation/aggregate.rs::*` — scope-reason: deleted with the tree
- `crates/gcode/src/commands/codewiki/text/generation/one_shot.rs::*` — scope-reason: deleted with the tree
- `crates/gcode/src/commands/codewiki/text/generation/outcome.rs::*` — scope-reason: deleted with the tree
- `crates/gcode/src/commands/codewiki/text/generation/routing.rs::*` — scope-reason: deleted with the tree
- `crates/gcode/src/commands/codewiki/text/generation/tool_loop.rs::*` — scope-reason: deleted with the tree
- `crates/gcode/src/commands/codewiki/types/ai.rs::*` — scope-reason: deleted with the tree
- `crates/gcode/tests/facade_boundary.rs`
- `crates/gcode/src/contract.rs::contract`
- `crates/gcode/src/cli/tests/codewiki.rs::*` — scope-reason: module deleted after its codewiki cases moved to gwiki (2.3) and its setup case moved to the new setup test module
- `crates/gcode/src/cli/tests.rs::*` — scope-reason: drop the `mod codewiki` child-test registration deleted with its module
- `crates/gcode/src/dispatch/tests.rs::*` — scope-reason: remove the codewiki routing and ai-options helper tests with their dispatch sites
- `crates/gcode/src/config/tests.rs::*` — scope-reason: repoint the include_str! source sample that reads a deleted engine file to a retained gcode source
- `crates/gcode/security/managed_postgres_privileges.json::*` — scope-reason: regenerated without the deleted commands/codewiki/graph.rs entry
- `tests/code_index/test_gcode_privilege_manifest.py::*` — scope-reason: rerun as focused validation against the regenerated inventory
- `crates/gcode/src/cli/tests/setup.rs`
- `crates/gwiki/src/commands/code/build_parts/features.rs`
- `crates/gcode/contract/gcode.contract.json::*` — scope-reason: regenerated wholesale without the codewiki entry
- `tests/contracts/gcode.contract.json::*` — scope-reason: vendored copy synced wholesale
- `crates/gcode/tests/contract.rs::*` — scope-reason: update the contract_version pin
- `tests/test_cli_contracts.py::*` — scope-reason: update the gcode contract_version pin

This leaf is the atomic deletion boundary for the old surface. 2.1 copied the
engine into gwiki and deliberately left the gcode tree in place, because
`cli.rs` and `dispatch.rs` still consume `commands::codewiki` and a leaf that
removed the module before its consumers could not compile. Remove all of it
together here: the CLI references, the dispatch sites, the module
declaration, the CLI arg test module, and the entire
`crates/gcode/src/commands/codewiki/` source tree (every file, wholesale —
the destination counterparts already exist and 2.6 proves their parity).
The Targets above enumerate every one of the 108 deleted source files
individually — symbol-bearing files as `::*` entries and the zero-symbol
files as bare paths — because no file-level entry stands for a directory
tree. Nothing may reference the old tree afterwards.

Remove the `Codewiki` variant (cli.rs:382-515) and its value enums/`From`
impls, the three dispatch sites (service-config match arms, the
`codewiki_ai_options` helper, the execution arm), the contract entry
(contract.rs:364), and the CLI arg test module. Bump gcode `contract_version`
2→3, regenerate `crates/gcode/contract/gcode.contract.json`, sync
`tests/contracts/gcode.contract.json`, and update the pins in
`crates/gcode/tests/contract.rs` and `tests/test_cli_contracts.py`. No
forwarding command, no deprecation shim.

The moved engine's features build-part enumerates one `BinaryContract`
descriptor per binary (gcode and gwiki) with per-binary handler resolvers —
keep BOTH descriptors intact: remove the retired `codewiki` arm from the
gcode resolver and add the flat `code` mode mappings to the gwiki resolver
in the moved `crates/gwiki/src/commands/code/build_parts/features.rs`, so
each contract section keeps its own binary identity (no repointing, which
would render gwiki's command set under a gcode heading). Before deleting the
gcode CLI test module, relocate its unrelated `parse_setup_standalone` case
into the enduring `crates/gcode/src/cli/tests/setup.rs` — only the codewiki
parse cases moved to gwiki in 2.3.

The compiled test consumers go with the tree, or the gcode test target
cannot build after the deletion: `crates/gcode/src/cli/tests.rs` registers
`mod codewiki` (cli/tests.rs:5) — the registration is removed with the
module; `crates/gcode/src/dispatch/tests.rs` exercises the removed
service-config arms and the `codewiki_ai_options` helper
(dispatch/tests.rs:104-177) — those cases are deleted with their dispatch
sites; and `crates/gcode/src/config/tests.rs` reads the engine's
`text/generation.rs` as an `include_str!` source sample
(config/tests.rs:103) — repoint that sample to a retained gcode source (the
adjacent `commands/symbols.rs` sample shows the pattern; the test needs a
real source file, not that specific one). The managed-PostgreSQL privilege
inventory records the two graph queries at the deleted
`commands/codewiki/graph.rs`; regenerate it in this leaf so the
exact-inventory equality test stays green.

This leaf closes the temporary export 1.3 opened. `commands` is public today
solely because the binary's `cli.rs` reaches `gobby_code::commands::codewiki::*`;
1.3 moved `cli.rs`/`dispatch.rs` into the library, so once this leaf deletes
the codewiki call sites and the module itself nothing outside the crate
consumes `commands`. Flip `pub mod commands` to private in `lib.rs`
(reducing its child-module visibility to match), and update
`crates/gcode/tests/facade_boundary.rs` so the pinned public set is the
enduring one: `run_cli`, `codewiki_facts`, `contract`, `test_env`, and the
projection-boundary items the existing lib.rs pin already declares.

**Acceptance:**

- 2.4.1 - `gcode codewiki` is absent from CLI parse, dispatch, and contract;
  `gcode --help` and the contract JSON show no codewiki entry; no file
  remains under `crates/gcode/src/commands/codewiki/` and nothing references
  it. file: `crates/gcode/contract/gcode.contract.json`.
- 2.4.2 - Contract-version pins across Rust and Python fixtures agree at gcode
  3 / gwiki 16. test: `tests/test_cli_contracts.py`.
- 2.4.3 - `commands` is no longer part of gobby-code's public surface and the
  boundary test pins the enduring public API set with no temporary exports
  remaining. test: `crates/gcode/tests/facade_boundary.rs::public_surface_is_pinned`.
- 2.4.4 - The gcode unit-test target compiles and passes after the deletion:
  no `mod codewiki` test registration remains, the dispatch codewiki cases
  are gone, and the config test's source sample reads a retained file.
  test: `crates/gcode/src/config/tests.rs`.
- 2.4.5 - The managed-PostgreSQL privilege inventory carries no
  `commands/codewiki` entry and the exact-inventory equality test passes.
  file: `crates/gcode/security/managed_postgres_privileges.json`.

### 2.5 Flip the provenance marker with no reader compatibility [category: code] (depends: 2.4)
`kind: deliverable`

Targets:
- `crates/gcore/src/codewiki_contract.rs::*` — scope-reason: rename the marker constant, flip its value, and update GOLDEN_PAGE plus self-checks
- `crates/gwiki/src/commands/code/publication.rs`
- `crates/gwiki/src/commands/code/build_parts/changes.rs`
- `crates/gwiki/src/commands/code/text/frontmatter.rs`
- `crates/gwiki/src/commands/code/ownership/render.rs`
- `crates/gwiki/src/commands/code/tests/architecture.rs`
- `crates/gwiki/src/commands/code/tests/contract.rs`
- `crates/gwiki/src/frontmatter.rs::*` — scope-reason: replace every hardcoded gcode-codewiki literal with the renamed constant
- `crates/gwiki/src/indexer.rs::*` — scope-reason: replace hardcoded marker literal
- `crates/gwiki/src/lint.rs::*` — scope-reason: replace hardcoded marker literal
- `crates/gwiki/src/audit/claims.rs::*` — scope-reason: replace marker constant usage with renamed constant
- `crates/gwiki/src/audit/tests.rs::*` — scope-reason: update hardcoded marker literals in tests
- `crates/gwiki/src/librarian.rs::*` — scope-reason: retire the outdated-codewiki aggregation and scope `page_is_codewiki` off any-substring matching
- `crates/gwiki/src/librarian/proposals.rs::*` — scope-reason: remove the outdated-codewiki proposal flow with its aggregation
- `crates/gwiki/src/librarian/tests.rs::*` — scope-reason: update hardcoded marker literals in tests and add the namespace-scope pin
- `crates/gwiki/src/upkeep.rs::*` — scope-reason: exclude `code/**` before stale archiving, unworthy-concept governance, and every mutation stage of scheduled upkeep
- `crates/gwiki/src/upkeep/tests.rs::*` — scope-reason: add the scheduled-run namespace-exclusion regression

This leaf depends on 2.4 because the marker constant is shared: until 2.4
deletes the old gcode tree, that tree still references
`GENERATED_BY_CODEWIKI`, and renaming the constant while two copies of the
engine exist would break the one that is about to be deleted.

Rename `GENERATED_BY_CODEWIKI` → `GENERATED_BY_GWIKI_CODE` with value
`"gwiki-code"` in `crates/gcore/src/codewiki_contract.rs`; update `GOLDEN_PAGE`
and its self-checks. The moved engine has four writers and they do not all
behave the same way under the rename:
`crates/gwiki/src/commands/code/text/frontmatter.rs`,
`crates/gwiki/src/commands/code/build_parts/changes.rs`, and
`crates/gwiki/src/commands/code/ownership/render.rs` reference the shared
constant and need the identifier updated, while
`crates/gwiki/src/commands/code/publication.rs`'s
placeholder-page writer emits the marker as a bare `generated_by: gcode-codewiki`
string literal and must be converted to use `GENERATED_BY_GWIKI_CODE` — it is
the one site a constant rename alone would silently miss. The moved
architecture and contract tests assert the bare literal too and move to the
new value. Update those, and every gwiki reader that currently hardcodes
`gcode-codewiki` — `crates/gwiki/src/frontmatter.rs`,
`crates/gwiki/src/indexer.rs`, `crates/gwiki/src/lint.rs`,
`crates/gwiki/src/audit/claims.rs`, plus test literals in
`crates/gwiki/src/audit/tests.rs` and `crates/gwiki/src/librarian/tests.rs` —
to the renamed constant. Per the Decision Record there is
NO recognition of the old `gcode-codewiki` value anywhere after this change.
Replacing the marker literal is **not sufficient**, because production
classifiers recognize the retired state through marker-independent
fallbacks that must each be retired or gated in this leaf:

- `crates/gwiki/src/lint.rs` (:210-223) classifies a page as generated when
  `frontmatter.generated_by.is_some()` — **any** value — or when its `type:`
  carries a legacy `code_*` prefix. Restrict the generated-page
  classification to the enumerated known set (the renamed
  `GENERATED_BY_GWIKI_CODE` constant plus gwiki's own catalog/index
  markers); drop the any-value and `code_*`-prefix fallbacks, with one
  negative regression per retired signal (arbitrary `generated_by` value,
  legacy `code_*` type).
- `crates/gwiki/src/audit/claims.rs` trust handling: any path-based or
  marker-independent treatment of `code/**` content as trusted/generated is
  gated the same way, with its own negative regression.
- `crates/gwiki/src/librarian.rs` `page_is_codewiki` (:497-502) matches
  `generated_by.contains("codewiki")` — old pages keep matching after the
  flip, and its `outdated_codewiki_pages` aggregation feeds a "Refresh
  outdated codewiki pages" proposal in `librarian/proposals.rs`: a
  production curation flow over `code/**` that scheduled librarian runs
  execute. Retire the outdated-codewiki check and its proposal wiring
  entirely (a dormant engine has no refresh path — this is exactly the
  "jobs dedicated to generated `code/**` content stop" constraint);
  `code/**` is filtered out before aggregation and before every mutation
  stage (proposal, task, patch, promotion), so no scheduled librarian
  entrypoint can curate generated code pages through any production path.
- `crates/gwiki/src/upkeep.rs`: scheduled upkeep is an independent
  maintenance writer over **all** pages — `archive_long_stale_pages`
  iterates `lint::collect_pages(vault_root)` with no namespace filter and
  applies an `Archived` lifecycle transition to any long-stale page
  (upkeep.rs:191-231), and the daemon runs upkeep on a cron schedule
  through the gateway (`src/gobby/wiki/scheduled_jobs.py`), so the
  librarian gate alone does not protect generated content. Exclude
  `code/**` from upkeep's page enumeration before stale archiving,
  unworthy-concept candidate governance, and every other mutation stage,
  so no scheduled upkeep run can archive or govern generated code pages.

Verify (and pin with tests) that pages whose `generated_by` no longer
matches are treated as ordinary non-generated pages by lint/audit/indexing —
skipped or defaulted, never errored — and that no AI-consuming maintenance
path (librarian/upkeep) gains scope over `code/**` because of the changed
classification. Each namespace-exclusion regression carries a **positive
control**: an eligible `knowledge/**` fixture in the same scheduled run
whose normal curation or archival still executes, proving the exclusion is
namespace-scoped rather than a wholesale disable — shared `knowledge/**`
maintenance keeps running.

**Acceptance:**

- 2.5.1 - The renamed constant with value `gwiki-code` is the only marker any
  production reader or writer recognizes; a scan of `crates` and `src` finds
  `gcode-codewiki` only in the enumerated allowlist — the parity normalizer
  script, the parity fixture README, and the unrecognized-marker
  characterization test literals — and fails on any occurrence outside it.
  file: `crates/gcore/src/codewiki_contract.rs`.
- 2.5.2 - Unrecognized `generated_by` values degrade to non-generated handling
  without errors in lint, audit, and indexing, with one negative regression
  per retired fallback signal: arbitrary `generated_by` value, legacy
  `code_*` type prefix, and `code/**` path-trust handling in audit.
  test: `crates/gwiki/src/lint.rs::unrecognized_generated_by_is_plain_page`.
- 2.5.3 - No production librarian/upkeep path aggregates, proposes, tasks,
  patches, or promotes `code/**` pages after the flip: the outdated-codewiki
  check and its proposal wiring are gone, and an end-to-end scheduled-run
  regression over a vault containing legacy `code/**` pages produces no
  code-page curation while an eligible `knowledge/**` control page in the
  same run still receives its normal curation flow (the `code/**` pages stay
  byte-identical; the run is proven not disabled wholesale).
  test: `crates/gwiki/src/librarian/tests.rs::generated_code_namespace_not_curated`.
- 2.5.4 - Scheduled upkeep never mutates `code/**`: page enumeration
  excludes the namespace before stale archiving, unworthy-concept
  governance, and every other mutation stage, and a scheduled-run
  regression pairing a long-stale legacy `code/**` page with an equally
  stale eligible `knowledge/**` control page archives the control while the
  `code/**` page remains byte-identical and uncurated — proving the
  exclusion is namespace-scoped, not a disabled job.
  test: `crates/gwiki/src/upkeep/tests.rs::code_namespace_excluded_from_upkeep`.

### 2.6 Prove old-vs-new parity [category: test] (depends: 2.3, 2.4, 2.5)
`kind: deliverable`

Targets:
- `crates/gwiki/tests/code_parity.rs`
- `scripts/codewiki_parity_baseline.sh`

Add the parity test: invoke `scripts/codewiki_parity_baseline.sh
--engine gwiki` to run the moved `gwiki code --ai off` against the same
committed fixture project the baseline was captured from (1.1's
input-identity gate asserts the pinned fixture digest) into a fresh temp
vault and produce the normalized manifest — the test does
not reimplement normalization; the script from 1.1 is the single
implementation — then compare that manifest against the committed baseline
`crates/gwiki/tests/fixtures/codewiki_parity/baseline.sha256`.
This leaf owns the first execution of the `--engine gwiki` mode (1.1
implemented but never ran it): its execution discipline and cleanliness
assertions land here. Path-diff reporting and the production-vault
cleanliness assertion stay in the Rust harness: any diff fails with the
offending paths listed. The production-vault control is **harness-only** —
per Constraints, neither the legacy CLI (whose `--out` is unrestricted) nor
direct `gwiki code` execution implements a production-vault guard; the
harness asserts its own runs never touched the real vault (2.6.2) and adds
no guard to the product.

Byte parity alone does not prove behavioral equivalence, so this leaf also
carries an explicit acceptance matrix over the promised legacy behaviors —
compare, purge, repair, lock contention, scope selection, incremental runs,
and representative failure paths — each mapped to a distinct test artifact
(reuse the moved test suite where it already covers a row; this test is the
end-to-end byte gate). Mark `#[ignore]`-gated if runtime exceeds normal CI
budget and wire it into the focused validation command set instead — do not
silently skip it.

**Acceptance:**

- 2.6.1 - Normalized full-run output is byte-identical to the committed
  pre-move baseline. test: `crates/gwiki/tests/code_parity.rs::deterministic_output_matches_baseline`.
- 2.6.2 - Production vault checksums are identical before and after the parity
  run (the test asserts it never touched the real vault).
  test: `crates/gwiki/tests/code_parity.rs::production_vault_untouched`.
- 2.6.3 - The `--engine gwiki` run obeys the capture discipline 1.1 pinned
  for the gcode mode: identical committed fixture with the pinned source
  digest asserted, isolated index identity, freshly built `--locked`
  workspace binary, capture metadata only in the temporary run directory,
  and the whole tracked fixture tree (including `project/`) clean
  afterwards. test: `crates/gwiki/tests/code_parity.rs::gwiki_mode_capture_discipline`.
- 2.6.4 - `--compare-to`, `--purge --force`, `--repair-citations`, scope
  selection, and incremental (`--since`-style) runs each behave as the
  legacy CLI did, with one test artifact per mode.
  test: `crates/gwiki/tests/code_parity.rs::legacy_mode_matrix`.
- 2.6.5 - Concurrent generation against the same output directory is refused
  with the legacy per-output lock semantics and timeout, naming `gwiki code`
  in the diagnostic. test: `crates/gwiki/tests/code_parity.rs::lock_contention_matches_legacy`.
- 2.6.6 - Representative failure paths the legacy CLI actually implements
  produce legacy-equivalent errors without partial writes: an invalid
  `--compare-to` ref fails with the legacy "compare ref … does not resolve
  to a commit" diagnostic (compare.rs:164/203/210), and `--complete-scope`
  without `--scope` fails with the legacy argument diagnostic (run.rs:46).
  An unindexed project is deliberately not a failure fixture: legacy
  freshness auto-indexes a never-indexed project
  (freshness.rs::project_needs_refresh returns refresh-needed and
  `ensure_fresh` builds the first index) rather than erroring.
  Production-vault cleanliness remains the harness-only safety assertion of
  2.6.2 and adds no guard to direct `gwiki code` execution.
  test: `crates/gwiki/tests/code_parity.rs::failure_paths_match_legacy`.

## P3: Dormant daemon surface
`kind: framing`

**Goal**: No daemon path can enqueue, trigger, or execute CodeWiki generation;
status is honest about why; the UI, auth, config, installer, and dream
surfaces reflect dormancy.

### 3.1 Remove the active CodeWiki daemon machinery [category: code]
`kind: deliverable`

Targets:
- `src/gobby/code_index/codewiki_refresh.py::*` — scope-reason: module deleted; service moves to dormant wiki ownership
- `src/gobby/code_index/codewiki_trigger.py::*` — scope-reason: module deleted
- `src/gobby/code_index/codewiki_nightly.py::*` — scope-reason: module deleted; cron reconciliation replaces registration in 3.2
- `src/gobby/code_index/gcode_gateway.py::GcodeGateway.codewiki`
- `src/gobby/code_index/__init__.py::*` — scope-reason: drop deleted-module re-exports
- `src/gobby/app_context.py::ServiceContainer`
- `src/gobby/servers/_app_lifecycle.py::create_lifespan`
- `src/gobby/runner_init/orchestration.py::*` — scope-reason: remove the codewiki nightly registration block and its degraded-service handling
- `tests/code_index/test_codewiki_refresh.py::*` — scope-reason: deleted with its module
- `tests/code_index/test_codewiki_trigger.py::*` — scope-reason: deleted with its module
- `tests/code_index/test_codewiki_nightly.py::*` — scope-reason: deleted with its module
- `tests/code_index/test_gcode_gateway.py::*` — scope-reason: direct test consumer of the deleted codewiki gateway method; its codewiki assertions are removed and the retained gateway contract repinned
- `tests/test_runner_init.py::*` — scope-reason: delete the codewiki cron-registration cases; 3.2 adds the reconciliation cases

Delete `codewiki_refresh.py`, `codewiki_trigger.py`, and `codewiki_nightly.py`
from `src/gobby/code_index/`; remove the `GcodeGateway.codewiki` operation
(`gcode_gateway.py:502`) — its other operations and consumers
(`context.py`, `sync_worker.py`, `trigger.py`, runner init) are untouched.
Remove `ServiceContainer.codewiki_trigger` and its TYPE_CHECKING import,
the `CodewikiRefreshTrigger` construction in `_app_lifecycle.py:82-90`, and the
`register_codewiki_nightly_crons` block in `orchestration.py:528-597`
(including the `codewiki_targets` fan-out over dream scopes and
`mark_service_degraded(runner, "codewiki_nightly_cron")`). Update the
associated test files in the same change — the three codewiki test modules
under tests/code_index/ are deleted with their modules,
`tests/code_index/test_gcode_gateway.py` (a direct consumer of the deleted
`GcodeGateway.codewiki` method) loses its codewiki assertions and repins the
retained gateway contract, and the codewiki cron-registration cases in
`tests/test_runner_init.py` are deleted here; the reconciliation cases that
replace them are owned by 3.2, which introduces that behavior.

**Acceptance:**

- 3.1.1 - The three codewiki modules are gone and nothing imports them:
  each of `gcode grep -w "codewiki_refresh" src`,
  `gcode grep -w "codewiki_trigger" src`, and
  `gcode grep -w "codewiki_nightly" src` returns zero hits (whole-word
  scans, one retired module per invocation — never an escaped-alternation
  pattern, which searches a literal pipe and passes vacuously). The
  intentionally retained surfaces stay out of match range by construction:
  the inert `wiki.codewiki_nightly_*` config keys and
  `CODEWIKI_NIGHTLY_JOB_PREFIX` embed the scan words only inside longer
  identifiers or hyphenated values. The deletion test seeds a positive
  control (a temp file with `from gobby.code_index.codewiki_nightly import
  x`) and asserts the same scan detects it, so a zero result is proven
  meaningful. file: `src/gobby/code_index/__init__.py`.
- 3.1.2 - `GcodeGateway` has no codewiki operation and its remaining consumers
  are unchanged. symbol: `GcodeGateway`.
- 3.1.3 - App startup constructs no codewiki trigger and registers no nightly
  handler. symbol: `create_lifespan`.

### 3.2 Reconcile CodeWiki cron rows to disabled at startup [category: code] (depends: 3.1, 2.5)
`kind: deliverable`

Targets:
- `src/gobby/wiki/codewiki_dormant.py`
- `src/gobby/wiki/__init__.py::*` — scope-reason: export the dormancy reconciler
- `src/gobby/runner_init/orchestration.py::*` — scope-reason: insert the reconciliation call where registration used to happen
- `tests/test_runner_init.py::*` — scope-reason: add the reconciliation-at-startup cases
- `tests/wiki/test_codewiki_dormant.py`
- `src/gobby/storage/cron.py::*` — scope-reason: add the bounded `list_jobs_by_name_prefix` listing that covers system and legacy non-system rows
- `tests/storage/test_cron.py::*` — scope-reason: pin the new prefix listing including the non-system variant

Create `src/gobby/wiki/codewiki_dormant.py` owning the dormant state:

```python
CODEWIKI_NIGHTLY_JOB_PREFIX = "gobby:codewiki-nightly:"
CODEWIKI_DISABLED_REASON = "pending_wiki_redesign"

@dataclass(frozen=True)
class CodewikiCronReconciliation:
    disabled: tuple[str, ...]
    failed: tuple[str, ...]
    residual_enabled: tuple[str, ...]


def reconcile_codewiki_crons_disabled(
    cron_storage,
) -> CodewikiCronReconciliation:
    """Idempotently set enabled=False on every cron row under the nightly
    prefix — system rows and legacy non-system rows alike. Registers no
    handler, takes no runner, and marks no service state. Returns the
    disabled, failed, and still-enabled row ids so the caller can report
    residue."""
```

The enumeration must cover **both** system and legacy non-system rows.
The retired registrar explicitly tolerated a pre-existing reserved-prefix
row with `is_system=False` and upgraded it only while the registrar ran
(codewiki_nightly.py:201-202) — after 3.1 deletes the registrar, nothing
upgrades such a row again — and scheduler eligibility filters only on
`enabled` and `next_run_at`, never on `is_system` (`get_due_jobs`,
cron.py:802), so a legacy non-system reserved-prefix row stays schedulable
while being invisible to a system-only sweep. Treat legacy non-system
matches as migration-owned and disable them identically. The existing
`list_system_jobs_by_name_prefix` primitive is system-only (cron.py:324),
so add a bounded storage primitive
`CronJobStorage.list_jobs_by_name_prefix(prefix, enabled=True)` — same
escaped-prefix matching, no `is_system` filter — pinned by its own storage
tests, and implement the reconciler with it: enumeration followed by
`update_job(job.id, enabled=False)` for each match, no bespoke SQL in the
reconciler. Result population is
explicit: an update returning a `CronJob` appends that row id to `disabled`,
an update that raises or returns `None` appends it to `failed`, and the
post-loop re-query populates `residual_enabled`. `update_job` permits
`enabled` changes on system rows
and recomputes `next_run_at` (`compute_next_run` returns `None` for disabled
jobs), so after reconciliation every matched row has `enabled=False` and
`next_run_at=NULL` while non-prefix rows and other wiki cron jobs remain
untouched.

The loop is fail-closed, not best-effort: each `update_job` call is wrapped
per-item, and a raised exception (`update_job` raises `SystemRowProtected` and
plain `ValueError` for several inputs) or a `None` return records that row's id
in `failed` without aborting the rest of the loop. After the loop the
reconciler re-queries the enabled prefix and reports whatever remains in
`residual_enabled`.

Detection and reaction are split because the storage helper has no runner:
`mark_service_degraded(runner, service_name)` requires a `GobbyRunner`, so the
reconciler stays runner-independent and returns the residue instead of acting
on it. `init_orchestration(runner)` — which already holds the runner and
already calls `mark_service_degraded` for other services — inspects the
returned result, logs the failed and residual row ids, and calls
`mark_service_degraded(runner, "codewiki_dormant_reconciliation")` when either
set is non-empty, relying on the next startup to converge.

Not every failure can be represented in that result: the two
`list_jobs_by_name_prefix` queries — the initial enumeration and the
residual re-query — can raise before any result exists. `init_orchestration`
therefore wraps the whole reconciliation call in its own try/except that
logs, marks `codewiki_dormant_reconciliation` degraded, and continues with
cron setup, so a storage fault cannot escape into the surrounding cron block
and skip scheduler construction. Scheduler startup proceeds either way — an orphaned enabled row is contained (its firing fails
with the executor's no-handler error, logged as a failed run with backoff)
until a later startup disables it; daemon startup is never blocked on this
reconciliation.

Call it from `init_orchestration` (3.1 removed the registrar); this leaf
owns that call-site insertion and its runner-init test cases. Placement is
load-bearing: the legacy registration block sits inside
`if runner.code_indexer is not None:` (orchestration.py:483), and a
reconciler inserted "where registration used to happen" would inherit that
gate — leaving enabled legacy cron rows active whenever the code indexer is
disabled, absent, or failed. The reconciliation call goes **outside** the
code-indexer conditional: persisted cron state must converge on every
startup regardless of optional-subsystem availability. It must be idempotent (safe on every startup), must not delete
rows, must not register `codewiki_nightly:*` handlers, and must leave every
other wiki cron job (`gobby.wiki.scheduled_jobs`) untouched. Verify that no
remaining scheduled job maintains generated `code/**` content: audit
`register_wiki_cron_jobs` registrations and pin the finding (if a shared job
touches `code/**`, scope it away rather than pausing it — knowledge upkeep
keeps running). This leaf depends on 2.5 as well as 3.1 because the
librarian and upkeep `code/**` exclusions that make this audit true are 2.5
deliverables (2.5.3/2.5.4): before 2.5 lands, shared scheduled jobs still
reach `code/**` and acceptance 3.2.3 is unsatisfiable at this leaf's
dependency position.

**Acceptance:**

- 3.2.1 - Startup disables existing nightly rows idempotently and registers no
  codewiki handler; the reconciler returns the disabled, failed, and residual
  row ids without touching runner state, and converges on a later startup,
  never blocking daemon startup. symbol: `reconcile_codewiki_crons_disabled`.
- 3.2.2 - A cron row created enabled by an older deployment is disabled on next
  startup with `enabled=False` and `next_run_at=NULL` regardless of its
  `is_system` flag; non-prefix rows and other wiki cron jobs are untouched.
  test: `tests/wiki/test_codewiki_dormant.py::reconcile_disables_and_preserves`.
- 3.2.3 - No scheduled job in `gobby.wiki.scheduled_jobs` maintains generated
  `code/**` content. behavior: "generated-content maintenance paused" in `src/gobby/wiki/codewiki_dormant.py`.
- 3.2.4 - With an injected mid-loop failure (`update_job` raising or returning
  `None`), the reconciler finishes the remaining rows and reports the failed
  and residual ids in its result; startup logs them and marks
  `codewiki_dormant_reconciliation` degraded on the runner, and a subsequent
  run converges to zero enabled rows.
  test: `tests/wiki/test_codewiki_dormant.py::mid_loop_failure_degrades_and_converges`.
- 3.2.5 - When either prefix query raises (initial enumeration or residual
  re-query), startup marks `codewiki_dormant_reconciliation` degraded and
  still constructs the scheduler.
  test: `tests/test_runner_init.py::reconciliation_query_failure_does_not_block_startup`.
- 3.2.6 - Reconciliation runs and converges when the code indexer is absent,
  disabled, or failed — the call sits outside the `code_indexer is not None`
  conditional and each of the three indexer states is tested at startup.
  test: `tests/test_runner_init.py::reconciliation_independent_of_code_indexer`.
- 3.2.7 - A legacy reserved-prefix row with `is_system=False` is disabled by
  reconciliation, is absent from `get_due_jobs` afterwards, and lands in the
  failed/residual reporting when its update fails.
  test: `tests/wiki/test_codewiki_dormant.py::legacy_non_system_row_reconciled`.

### 3.3 Register the dormant wiki-owned routes and retire the legacy routes [category: code] (depends: 3.1)
`kind: deliverable`

Targets:
- `src/gobby/servers/routes/wiki_code.py`
- `src/gobby/servers/routes/__init__.py::*` — scope-reason: export the new router factory
- `src/gobby/servers/_app_routes.py::register_routes`
- `src/gobby/servers/routes/code_index.py::create_code_index_router`
- `src/gobby/servers/auth_service.py::*` — scope-reason: replace the legacy codewiki agent-route entry with entries for the new paths
- `tests/servers/routes/test_wiki_code_routes.py`
- `tests/servers/routes/test_code_index_routes.py::*` — scope-reason: drop the retired codewiki route cases and pin their absence
- `tests/servers/test_auth_service.py::*` — scope-reason: update the codewiki auth-route case

New module `src/gobby/servers/routes/wiki_code.py` (keeps `wiki.py` from
growing; same error-envelope helpers) with router prefix `/api/wiki/code`:

- `GET /api/wiki/code/status` → HTTP 200 with exactly
  `{"enabled": false, "state": "disabled", "reason": "pending_wiki_redesign"}`.
  The in-process trigger state died with 3.1, and the legacy live-only fields
  (`pending_roots`, `running_roots`, `active_flush_tasks`, `last_run`) are NOT
  carried as empty vestiges — 3.6 rewrites the sole UI consumer to this
  dormant shape in the same change, so there is no reader left for them. Any
  additional field added to this response must name its persisted or derivable
  source in this section.
- `POST /api/wiki/code/refresh` → HTTP 409, body
  `{"error": "codewiki_disabled_pending_redesign", "reason": "pending_wiki_redesign"}`.
  It invokes no gateway, creates no work item, and touches no vault.

Remove the `/codewiki/refresh` and `/codewiki/status` routes, the
`CodewikiRefreshRequest` model, and `resolve_codewiki_scopes` usage from
`create_code_index_router`; no aliases. Register the new router in
`register_routes`. Auth: replace the legacy
`_AgentRoute("POST", "/api/code-index/codewiki/refresh", False)` entry with an
equivalent entry for `/api/wiki/code/refresh`; status stays unlisted (open),
mirroring today. Update the route and auth test files listed in Targets.

**Acceptance:**

- 3.3.1 - New status route returns 200 with exactly the pinned dormant shape
  (`enabled: false`, `state: "disabled"`, `reason: "pending_wiki_redesign"`,
  no legacy live-only fields); refresh returns 409 with the stable error code
  and provably performs no work. test: `tests/servers/routes/test_wiki_code_routes.py::dormant_status_and_refresh`.
- 3.3.2 - Legacy `/api/code-index/codewiki/*` paths return 404 with no alias
  or redirect. test: `tests/servers/routes/test_code_index_routes.py::codewiki_routes_absent`.
- 3.3.3 - Auth table carries the new refresh path and no legacy codewiki path.
  file: `src/gobby/servers/auth_service.py`.

### 3.4 Stop the installer emitting the codewiki git-hook trigger [category: code]
`kind: deliverable`

Targets:
- `src/gobby/cli/installers/git_hooks.py::*` — scope-reason: remove the codewiki refresh curl from both shell branches of the post-commit body
- `tests/cli/installers/test_git_hooks_installer.py::*` — scope-reason: repin the hook-body assertions to assert absence

Remove the `${DAEMON_URL}/api/code-index/codewiki/refresh` curl from both
branches (jq and sed-escaped fallback) of `_CODE_INDEX_REINDEX_BODY`; the
reindex curl stays. Update the pinned assertions in
`tests/cli/installers/test_git_hooks_installer.py` (currently pinning the exact
path at :702, :757, :1116-1155) to assert the codewiki curl is absent. No
stale-hook compatibility work — reinstall refreshes hooks.

**Acceptance:**

- 3.4.1 - Freshly installed post-commit hooks contain no codewiki refresh call
  in either shell branch. test: `tests/cli/installers/test_git_hooks_installer.py::post_commit_has_no_codewiki_curl`.

### 3.5 Clean up AI-surface coupling [category: code]
`kind: deliverable`

Targets:
- `src/gobby/ai/_tool_chat_tools.py::*` — scope-reason: remove the codewiki entry from the gcode subcommand whitelist
- `tests/ai/test_tool_chat_tools.py::*` — scope-reason: owning test file for the whitelist; gains the mutation-enabled rejection assertion
- `src/gobby/servers/routes/llm.py::*` — scope-reason: update the codewiki-specific timeout-override rationale comment
- `src/gobby/ai/_text_generation_service.py::*` — scope-reason: update the circuit-breaker sizing comment that cites the codewiki nightly batch
- `tests/ai/test_text_generation_circuit_breaker.py::*` — scope-reason: update stale codewiki references in breaker tests

Remove `"codewiki"` from the tool-chat gcode subcommand whitelist
(`_tool_chat_tools.py:101`) — the subcommand no longer exists. The removal
needs a regression that actually reaches the policy branch where mutation
would otherwise be allowed: the existing generic mutator coverage in
`tests/ai/test_tool_chat_tools.py` runs the default mutation-disabled path,
which rejects codewiki for the wrong reason. Add an explicit assertion that
with `allow_mutation=True` active, a codewiki invocation is still rejected
as unlisted. Keep the text-generation circuit breaker and LLM timeout
override mechanisms unchanged (they are generic protection); rewrite their
comments (`llm.py:87-95`, `_text_generation_service.py:66-72`) to stop
citing the retired nightly batch as the rationale. Update
`tests/ai/test_text_generation_circuit_breaker.py` references as needed.

**Acceptance:**

- 3.5.1 - Tool-chat whitelist has no codewiki entry; the whitelist tests
  include a mutation-enabled case proving codewiki is rejected as unlisted
  when `allow_mutation=True`. test: `tests/ai/test_tool_chat_tools.py`.
- 3.5.2 - No production comment cites codewiki nightly as an active rationale.
  behavior: "stale codewiki rationale removed" in `src/gobby/ai/_text_generation_service.py`.

### 3.6 Reflect dormancy in config descriptions and the web UI [category: code] (depends: 3.3)
`kind: deliverable`

Targets:
- `src/gobby/config/wiki.py::WikiConfig`
- `web/src/components/activity/WikiTab.tsx::*` — scope-reason: drop the onRefreshCodewiki wiring passed to the toolbar
- `web/src/components/settings/sections/MemoryKnowledgeSection.tsx::*` — scope-reason: remove the four codewiki automation controls and their config-key wiring
- `web/src/components/settings/sections.ts::*` — scope-reason: update the section description mentioning codewiki watchers
- `web/src/components/activity/wiki/WikiTabData.ts::*` — scope-reason: repoint status fetch to the new route and drop the refresh request
- `web/src/components/activity/wiki/WikiTabActions.ts::*` — scope-reason: remove the refreshCodewiki action
- `web/src/components/activity/wiki/WikiTabToolbar.tsx::*` — scope-reason: remove the code-mode refresh kebab item
- `web/src/components/activity/wiki/WikiCodewikiStatus.tsx::*` — scope-reason: render the dormant state from the new status shape
- `web/src/components/activity/wiki/__tests__/WikiCodeMode.test.tsx::*` — scope-reason: retest dormant code mode without refresh
- `web/src/components/activity/wiki/__tests__/WikiA11y.test.tsx::*` — scope-reason: update status-strip accessibility assertions
- `web/src/components/settings/sections/__tests__/MemoryKnowledgeSection.test.tsx::*` — scope-reason: drop removed-control cases

Config: keep every `wiki.codewiki_*` key (inert), rewriting descriptions to
state generation is paused pending the wiki redesign and the keys have no
effect until #19665 re-enables orchestration. Keep
`validate_codewiki_scopes` / `resolve_codewiki_scopes` (harmless validators;
`resolve_codewiki_scopes` loses its remaining callers — keep the function, drop
dead imports).

Web: remove the four automation controls from `MemoryKnowledgeSection.tsx`
(:691-717) and the subsection hint; update `sections.ts:70`. Repoint
`fetchCodewikiStatus` to `GET /api/wiki/code/status`, delete
`requestCodewikiRefresh` (and `resolveCodewikiRoot`, which exists only to
feed it), the `refreshCodewiki` action, the `onRefreshCodewiki` wiring in
`WikiTab.tsx`, and the kebab item;
`WikiCodewikiStatus.tsx` renders the dormant state (badge + reason,
"paused pending wiki redesign") from the pinned minimal shape
`{enabled, state, reason}` — the rewritten `CodewikiStatus` type and
`normalizeCodewikiStatus` parser match 3.3's response exactly, with no legacy
`pending_roots`/`running_roots`/`last_run` handling — and stops polling on
`state: "disabled"` (one fetch, no 30s loop). Update the three web test files
listed in Targets.

This leaf depends on 3.3: between 3.3 landing and this leaf landing, the dev
UI's code mode calls removed legacy routes and shows fetch errors — an
accepted, transient pre-0.5.0 window that the dependency edge closes within
the same phase.

**Acceptance:**

- 3.6.1 - Settings UI shows no codewiki automation controls; wiki code mode
  shows the dormant badge with reason and offers no refresh action.
  test: `web/src/components/activity/wiki/__tests__/WikiCodeMode.test.tsx`.
- 3.6.2 - Config descriptions state the paused status; keys remain accepted
  and inert. file: `src/gobby/config/wiki.py`.
- 3.6.3 - Status polling stops when state is disabled. file: `web/src/components/activity/wiki/WikiCodewikiStatus.tsx`.

### 3.7 Pin dream tolerance of a frozen truth digest [category: test]
`kind: deliverable`

Targets:
- `tests/memory/test_dream_frozen_digest.py`

Characterization test pinned to the real trigger semantics: triggers compare
the digest's content hash against the stored last-seen hash and never read
file age. Three cases: (a) frozen digest already seen — seed the stored hash
to the frozen digest's hash, run dream repeatedly, assert no truth-change
trigger fires and no error occurs; (b) digest file absent — the pipeline
(the truth-digest read path, `_apply_truth_change_triggers` in the dream
service, and the storage-runs digest handling) completes cleanly and fires
nothing; (c) first-sight characterization — a frozen digest with no stored
hash fires the trigger exactly once, then stays quiet on every later run.

This leaf edits no production code, and the Targets are deliberately limited
to the new test file. The read path is already fail-soft at every layer: a
missing vault, missing digest file, unreadable file, malformed JSON, or
wrong-schema payload each resolve to an empty payload and then an empty
digest, which the caller treats as "skip this project"; a missing stored hash
compares unequal rather than raising; and each per-project iteration is
wrapped in a catch-all that logs and continues. There is therefore no crash
branch this leaf expects to fix. If the characterization test nonetheless
uncovers one, the failure is owned by this same leaf: expand the Targets to
the exact implicated read-path module (the truth-digest read path,
`_apply_truth_change_triggers`, or the storage-runs digest handling), repair
the defect there, and rerun the characterization test cleanly before
closure — no separate-task deferral.

**Acceptance:**

- 3.7.1 - Dream runs cleanly across the seen-frozen, absent, and first-sight
  digest cases with the pinned trigger behavior (quiet, quiet, fire-once);
  any crash the test uncovers is repaired in this same leaf and the test
  rerun cleanly before closure.
  test: `tests/memory/test_dream_frozen_digest.py::frozen_digest_is_tolerated`.

## P4: Versions, contracts, and documentation
`kind: framing`

**Goal**: Every crate, contract, fixture, and document agrees on the new
ownership, and installed binaries prove it.

### 4.1 Bump and align Rust crate versions [category: config] (depends: P2, P3)
`kind: deliverable`

Targets:
- `crates/gcode/Cargo.toml`
- `crates/gcore/Cargo.toml`
- `crates/gwiki/Cargo.toml`
- `crates/gdaemon/Cargo.toml`
- `crates/ghook/Cargo.toml`
- `Cargo.lock`

Apply the patch-bump policy from Constraints (gobby-code 1.6.0→1.6.1,
gobby-core 0.9.1→0.9.2, gobby-wiki 0.9.0→0.9.1, gobby-daemon 0.3.0→0.3.1,
gobby-hooks 0.8.3→0.8.4 — re-derive from current values if drifted); align
every internal `gobby-core` requirement to the new version; pin gwiki's
`gobby-code` requirement; regenerate `Cargo.lock`. Build and reinstall
`~/.gobby/bin/{gcode,gwiki,gdaemon,ghook}` and verify each binary reports its
new version. Python Gobby stays 0.5.0.

This leaf is the reinstall gate, which is why it depends on P3 as well as
P2: the rebuilt gcode drops the `codewiki` command, and the daemon shells
out to the installed binaries, so reinstalling before every daemon producer
of that command is retired (3.1 deletes the refresh/trigger/nightly
machinery and the gateway operation; 3.2 reconciles the cron rows to
disabled) would turn every still-scheduled invocation into a parse error
against the live daemon. No binary reinstall happens until P3's dormancy
leaves have landed.

**Acceptance:**

- 4.1.1 - All five crates carry the bumped versions with aligned internal
  requirements and a regenerated lock. file: `Cargo.lock`.
- 4.1.2 - Installed binaries report the new versions.
  behavior: "reinstalled binary versions verified" in `crates/gwiki/Cargo.toml`.

### 4.2 Update contracts, docs, and skill guidance [category: docs] (depends: P2, P3)
`kind: deliverable`

Targets:
- `docs/guides/codewiki.md`
- `docs/contracts/gwiki-cli.md`
- `docs/contracts/gcode-cli.md`
- `docs/guides/ai-configuration.md`
- `docs/guides/gcode-user-guide.md`
- `docs/guides/gcode-development-guide.md`
- `docs/guides/gcore-development-guide.md`
- `docs/guides/gwiki-user-guide.md`
- `crates/gcode/README.md`
- `src/gobby/install/shared/skills/code-index/SKILL.md`
- `crates/gcode/assets/SKILL.md`
- `crates/gwiki/src/commands/index.rs::*` — scope-reason: comment-only update of the legacy `gcode codewiki` pipeline reference
- `crates/gwiki/src/vault.rs::*` — scope-reason: comment-only update of the module doc's `gcode codewiki` reference; the no-touch `AI_README_TEMPLATE` is not edited
- `tests/docs/test_codewiki_docs.py::*` — scope-reason: repin the docs contract test to the rewritten guide
- `tests/skills/test_code_index_skill.py::*` — scope-reason: repin the embedded-skill content assertions

This leaf documents behavior finalized across P2 **and P3** — dormant daemon
routes and status/error codes (3.3), disabled cron reconciliation (3.2),
config-key inertness and UI dormancy (3.6) — so it depends on both phases;
documenting P3 surfaces before they land would produce stale guidance and
parallel-edit risk.

Rewrite `docs/guides/codewiki.md` for `gwiki code`: direct CLI available for
isolated/manual use, production-vault execution operationally paused pending
the redesign, daemon routes dormant with the stable status/error codes, legacy
routes and `gcode codewiki` gone. Update `docs/contracts/gwiki-cli.md` for the
new command and `docs/contracts/gcode-cli.md` to drop its codewiki
JSON-envelope and citation-repair sections.

The Targets carry the **complete active-document inventory** for the retired
invocation (repository sweep for `gcode codewiki`, 2026-08-08): beyond the
guide and the two contract docs, the sweep finds `crates/gcode/README.md`
(quick-start invocation), `docs/guides/ai-configuration.md`,
`docs/guides/gcode-user-guide.md`, `docs/guides/gcode-development-guide.md`,
`docs/guides/gcore-development-guide.md`, and `docs/guides/gwiki-user-guide.md`
— each updates its invocation and ownership statements to `gwiki code` (or
removes the reference where the flow is retired). Two comment-only code
references ride the same sweep: `crates/gwiki/src/commands/index.rs:103` and
the `crates/gwiki/src/vault.rs` module doc. Explicit exclusions, with
reasons: `crates/gwiki/src/catalog.rs` (:816 comment) is a Constraints
no-touch surface owned by #19664; the workspace crate changelog and the
evidence and research archives under `docs/` are historical records; plan
artifacts and changelog evidence document history. The bundled `code-index` skill
template keeps owning the CodeWiki lifecycle guidance (no new gwiki skill
surface — one consumer, existing owner): rewrite its codewiki bullets to the
paused/manual `gwiki code` surface with a pointer to the guide, and update
the byte-identical embedded gcode skill asset (`crates/gcode/assets/SKILL.md`)
in the same change so its content pin stays green. Add a focused
stale-reference scan across this active inventory (skill/contract/guide/README
surfaces, with the exclusions above). Keep `tests/docs/test_codewiki_docs.py`
and `tests/skills/test_code_index_skill.py` passing against the rewritten
surfaces — update expectations in the same change.

**Acceptance:**

- 4.2.1 - The codewiki guide documents `gwiki code`, the paused state, and the
  dormant routes; the docs contract test passes against it.
  test: `tests/docs/test_codewiki_docs.py`.
- 4.2.2 - No active skill, contract doc, guide, or crate README references
  `gcode codewiki`: the stale-reference scan covers the full enumerated
  inventory (embedded gcode asset, gcode CLI contract doc, the five guides,
  and `crates/gcode/README.md` included; the enumerated historical and
  no-touch exclusions documented). behavior: "no stale gcode codewiki references" in `docs/contracts/gwiki-cli.md`.

## E1 End-to-end acceptance
`kind: verification`

Run in isolated temporary vaults and a test daemon; never the production vault
or the user's running daemon:

- Normalized deterministic `gwiki code --ai off` output is byte-identical to
  the committed pre-move baseline (2.6); `compare`/`purge`/`repair`/locking
  behave as legacy.
- `gcode codewiki` fails to parse; no codewiki entry in the gcode contract;
  gwiki contract carries `code` at the bumped contract version; vendored
  fixtures agree.
- `GET /api/wiki/code/status` → 200 `enabled:false/disabled/pending_wiki_redesign`;
  `POST /api/wiki/code/refresh` → 409 `codewiki_disabled_pending_redesign`
  with no gateway invocation or work creation; legacy codewiki routes 404.
- Startup disables nightly cron rows idempotently, registers no codewiki
  handler; post-commit hook bodies contain no codewiki curl; no daemon path
  (startup, hooks, cron, config) can enqueue CodeWiki work regardless of
  stored configuration.
- `gcode grep "gcode-codewiki"` over `crates` and `src` finds only the
  enumerated parity-normalizer/fixture/characterization allowlist;
  unrecognized `generated_by` degrades cleanly; no knowledge job gains
  `code/**` scope; dream tolerates the frozen digest.
- Production wiki files byte-for-byte untouched (checksum before/after the
  full validation run).
- The facade performs no writes; gcode's public API is exactly the pinned set.
- Focused Rust tests, `cargo fmt --check`, clippy, focused
  `GOBBY_TEST_PROTECT=1` Python tests for every touched module, web tests for
  touched components, production line-count validation, and contract/version
  drift checks all pass. The full pytest suite is not run.
- Binaries rebuilt and reinstalled; reported versions match 4.1.

## D1 Daemon-native runtime boundary
`kind: deferred`

Authenticated handshake, short-lived scoped datastore grants, standalone-mode
removal, and daemon-mediated AI for gcode/gwiki.

```yaml
deferral:
  task_ref: "#18902"
  reason: "The runtime boundary subepic explicitly follows the dormant ownership migration; this plan only creates the facade seam it will wrap with grants."
  owner: "epic-18902"
  original_acceptance_items:
    - D1.1
```

## D2 Redesigned engine and page model
`kind: deferred`

New-engine authorship under `crates/gwiki/src/code_wiki/`, page-model redesign,
transactional publication for the new engine, `catalog.rs` decomposition, and
`outputs/graph.json` ownership.

```yaml
deferral:
  task_ref: "#19664"
  reason: "Sibling subepic owns the redesigned generated artifacts; boundary rulings exchanged with its planning session on 2026-08-06."
  owner: "epic-19664"
  original_acceptance_items:
    - D2.1
```

## D3 Orchestration and daemon re-enable
`kind: deferred`

Triggers, cron, recovery, the `GwikiGateway` execution operation, and the
disabled-to-enabled cutover of the dormant surface this plan registers.

```yaml
deferral:
  task_ref: "#19665"
  reason: "Orchestration maps onto Gobby primitives under its own subepic; this plan guarantees dormancy until that cutover."
  owner: "epic-19665"
  original_acceptance_items:
    - D3.1
```

## D4 Production cutover
`kind: deferred`

Backup, verified restore, explicit destructive approval, empty-vault rebuild,
and activation.

```yaml
deferral:
  task_ref: "#18779"
  reason: "Production transition completes only through the manual destructive-cutover acceptance child; this plan leaves production bytes untouched."
  owner: "epic-18779"
  original_acceptance_items:
    - D4.1
```

## D5 Completed code-fact surfaces
`kind: deferred`

Typed deterministic code-fact contracts, bounded graph views, and persisted
symbol summaries that complete or supersede the `codewiki_facts` seed.

```yaml
deferral:
  task_ref: "#17678"
  reason: "gcode owns the completed deterministic fact surfaces; this plan ships only the narrow facade the moved engine needs."
  owner: "epic-17678"
  original_acceptance_items:
    - D5.1
```

## V1 Plan Changelog
`kind: verification`

<!-- Rounds appended by enhancement/adversary phases. -->

**Round 1** `kind: enhancement`

- enhancer_run: `18f85cc3-8824-4962-b9cf-27fa5715fde5`
- enhancer_session: `74c10afd-45a2-4f5f-9966-e24b4c91807a`
- converged: false (round cap 1 reached; no further enhancement rounds)
- suggestions_presented: 3 (E1, E2, E3)
- accepted: E1 (single normalization implementation — parity script gains
  enumerated `--engine gcode|gwiki` modes; 2.6 test invokes the script instead
  of reimplementing normalization; edits in 1.1 and 2.6), E2 (reconciler
  pinned to `CronJobStorage.list_system_jobs_by_name_prefix` +
  `update_job(enabled=False)` with `enabled=False`/`next_run_at=NULL`
  postconditions; edits in 3.2), E3 as modified (dormant status response
  pinned; edits in 3.3 and 3.6)
- declined: none
- resolution_notes: E3 accepted with a user-approved variant — the response
  shape is pinned to the minimal `{enabled, state, reason}` instead of the
  enhancer's version carrying empty legacy fields (`pending_roots: []`,
  `running_roots: []`, `active_flush_tasks: 0`, `last_run: null`), because the
  only consumer of those fields is rewritten by 3.6 in the same plan and
  carrying them would be a backward-compat shim. The enhancer's clause that
  any additional field must name its persisted/derivable source was kept.

**Round 2** `kind: adversary`

- reviewer_run: `7154f7e9-63b3-4e7c-a875-a22e36a05c6b`
- reviewer_session: `a0fc11b4-f66e-4c56-a47a-9a6ace1b939b` (#10270)
- evidence_id: `ea367992-6430-4d1e-aa77-c206471c9115` (round_number 2)
- verdict: needs_review; findings_presented: 20 (19 blocking, 1 nit)
- accepted: all 20 (R2-F01…R2-F20); every finding independently verified
  against the codebase before the vote
- declined: none
- resolution_notes: R2-F01/F02 resolved with a committed immutable fixture
  project, workspace-built `--locked` binaries, and a pinned input-identity
  digest; R2-F05 resolved by merging former section 2.2 into 2.1 (move +
  decompose in one leaf); R2-F07 resolved to the legacy FLAT flag grammar
  (the nested-subcommand spec was dropped as contradicting CLI parity — the
  draft's "first nested subcommand" note was also factually wrong, page and
  export already nest); R2-F14 accepted with a user-approved variant —
  fail-closed verification plus degraded-service marking and next-startup
  convergence instead of blocking scheduler startup; R2-F16 accepted with a
  user-approved variant — dependency edge 3.6→3.3 plus targeting the WikiTab
  toolbar wiring instead of a full leaf merge, with the transient dev-UI
  window recorded as accepted; R2-F08 and R2-F18 applied with additional
  sites found during verification (the CLI_SUBCOMMANDS sync list and
  admission-test pins; the embedded-skill content pin).

```json plan-review-round
{"evidence_id":"ea367992-6430-4d1e-aa77-c206471c9115","plan_hash":"cff62325c005a7708e7dce498996aa6eddd6210bfef0bdf0b66a593b2aa516f0","round_number":2,"round_result":{"accepted":["R2-F01","R2-F02","R2-F03","R2-F04","R2-F05","R2-F06","R2-F07","R2-F08","R2-F09","R2-F10","R2-F11","R2-F12","R2-F13","R2-F14","R2-F15","R2-F16","R2-F17","R2-F18","R2-F19","R2-F20"],"coverage_attestation":{"adjacent_variant_complete":true,"attestation_digest":"deb30466e45f975eeab04fa31657edfea2859fa2f1b91a268bc4beaaed2f7967","cross_lane_interaction_complete":true,"disposition_counts":{"dismissed":11,"emitted_findings":20,"total":31},"evidence_id":"ea367992-6430-4d1e-aa77-c206471c9115","lanes":[{"candidate_count":12,"lane_id":"requirements_traceability","status":"completed"},{"candidate_count":11,"lane_id":"repository_blast_radius","status":"completed"},{"candidate_count":8,"lane_id":"runtime_invariants","status":"completed"}],"shadow_manifest_status":{"entry_count":18,"manifest_digest":"77a832fc54d4325a7225ab70dcd66cb572015a23af6b9179b63610ad95b615a4","status":"valid"},"source_digest":"8fdc304f295817ada53ec0cec18e69877d18874987aa63bfe1099d89d3af6212","version":1},"declined":[],"findings":[{"category":"weak-testability","check_key":"parity-input-identity","description":"CodeWiki derives page identity, hashes, symbols, and workspace topology from repository paths and bytes. Provenance-only normalization cannot make pre-move and post-move outputs comparable when those inputs differ.","finding_id":"R2-F01","fix":"Make 1.1 and 2.6 run both binaries against one immutable fixture project or pinned checkout with one isolated index/database identity; assert the fixture revision and source digest before each mode.","location":"P1 / § 1.1","prevention":"For every parity gate, pin source bytes, index identity, configuration, and output normalization independently of the implementation worktree.","principle":"A parity comparison requires identical immutable source and index inputs.","root_cause":"The baseline runs before the ownership move against the implementation repository, while the comparison runs after source paths, manifests, and contracts change.","section_id":"1.1","severity":"blocking"},{"category":"weak-testability","check_key":"baseline-binary-provenance","description":"A stale installed binary can produce a valid-looking manifest from code outside the reviewed snapshot, creating a false parity pass.","finding_id":"R2-F02","fix":"Require the script to build and execute explicit workspace target binaries under `--locked`; store the git revision and binary versions as non-hashed metadata.","location":"P1 / § 1.1","prevention":"Require locked workspace builds and record source revision plus binary version for every captured baseline.","principle":"A parity artifact must identify and execute the reviewed binaries.","root_cause":"The baseline permits bare gcode and gwiki resolution through ambient PATH.","section_id":"1.1","severity":"blocking"},{"category":"missing-requirement","check_key":"legacy-runtime-boundary","description":"Sections 1.2 and 2.1 can satisfy their stated inventory scan while leaving grouped imports and non-datastore dependencies unresolved, so the moved engine lacks a compilable gwiki-owned runtime contract.","finding_id":"R2-F03","fix":"Add a gwiki-owned legacy runtime struct for project root/id, quiet/output settings, and `CodewikiFacts`; enumerate and classify every external engine dependency, define signature conversions, add `crates/gwiki/tests/code_engine_boundary.rs` to Targets, and cover AI-off/direct/daemon/tool-loop plus compare, purge, and repair modes.","location":"P1 / § 1.2","prevention":"Inventory every `crate::` dependency and every threaded context field before defining the destination runtime boundary.","principle":"A crate ownership move needs a complete replacement contract for every crossed runtime dependency.","root_cause":"The proposed facts facade covers datastore queries, while the engine also consumes project identity, output settings, graph configuration, daemon AI routing, search, security, and tool-loop operations through `Context` and other gcode modules.","section_id":"1.2","severity":"blocking"},{"category":"traceability","check_key":"rust-bin-privacy","description":"The planned `pub(crate)`/private flip breaks the gcode binary and integration-test crates before the ownership boundary can compile.","finding_id":"R2-F04","fix":"Move CLI dispatch behind one deliberately public library entrypoint, reduce `main.rs` to that entrypoint, migrate integration-test consumers to pinned public APIs or unit-test locations, and list every affected file in 1.3 Targets.","location":"P1 / § 1.3","prevention":"Include package binaries and `tests/` crates in every public-to-private blast-radius sweep.","principle":"Rust package binaries and integration tests are external consumers of the package library.","root_cause":"The visibility plan inventories downstream crates while current `src/cli.rs`, `src/dispatch.rs`, and integration tests import the modules being made private through `gobby_code`.","section_id":"1.3","severity":"blocking"},{"category":"bad-sequencing","check_key":"monolith-task-atomicity","description":"Section 2.1 cannot omit the composer and compile; moving it touches an over-ceiling production file until 2.2. Same-session prose does not make independently expanded leaves atomic.","finding_id":"R2-F05","fix":"Merge 2.2 into 2.1, or decompose the composer at its current path as a prerequisite and move the already-decomposed module tree. Require each leaf to compile independently.","location":"P2 / § 2.1","prevention":"Check current and projected source size plus module completeness at each manifest-leaf boundary.","principle":"Every expanded leaf must finish compilable and below the production source-size ceiling.","root_cause":"The engine requires `diagram_compose.rs` during relocation, while that file has 1,062 lines and decomposition sits in a later dependent deliverable.","section_id":"2.1","severity":"blocking"},{"category":"bad-sequencing","check_key":"cargo-path-version-order","description":"Cargo resolution fails during the phase that is supposed to compile and test the moved engine.","finding_id":"R2-F06","fix":"Use the current gobby-code version in 2.1 and change package plus requirement to 1.6.1 atomically in 4.1, or move the aligned bump before 2.1 and make P2 depend on it.","location":"P2 / § 2.1","prevention":"Validate Cargo version requirements after each dependency-producing deliverable, including intermediate states.","principle":"A path dependency's declared version must resolve at every buildable phase.","root_cause":"Section 2.1 requires gobby-code 1.6.1 while the local package remains 1.6.0 until section 4.1, which depends on P2.","section_id":"2.1","severity":"blocking"},{"category":"traceability","check_key":"legacy-cli-shape","description":"The planned nested surface contradicts the accepted legacy CLI contract and makes 2.4's one-entry contract mirror ambiguous.","finding_id":"R2-F07","fix":"Define `gwiki code` with the same flat flags, defaults, conflicts, and failure behavior; move the existing parser tests with executable/variant paths changed.","location":"P2 / § 2.3","prevention":"Diff the concrete Clap grammar and parse/conflict tests whenever a plan claims CLI parity.","principle":"A relocation promising full legacy flag/default/failure parity must preserve the command grammar.","root_cause":"The current command uses flat `--compare-to`, `--purge`, and `--repair-citations` modes, while 2.3 specifies nested compare/purge/repair subcommands.","section_id":"2.3","severity":"blocking"},{"category":"traceability","check_key":"gwiki-command-exhaustive-sites","description":"CLI mapping alone cannot invoke the moved engine through gwiki's architecture; omitted exhaustive sites will fail compilation or leave the command unreachable.","finding_id":"R2-F08","fix":"Add `api.rs::Command`, `commands/mod.rs` registration and dispatch, `commands/project_admission.rs::classify_command`, mapping sites, typed flat code-mode payloads, outcomes, and exhaustive tests to 2.3 Targets and acceptance.","location":"P2 / § 2.3","prevention":"Sweep constructors, enum matches, registration tables, dispatchers, and admission/permission classifiers for each command addition.","principle":"A new CLI command must cover every exhaustive command representation and dispatch boundary.","root_cause":"The Targets omit gwiki's public `Command` enum, module registration, exhaustive dispatch, project-admission classification, and associated command outcomes.","section_id":"2.3","severity":"blocking"},{"category":"traceability","check_key":"existing-dependency-guard","description":"The planned dependency makes gwiki's current test suite fail, yet the enforcing test is absent from Targets.","finding_id":"R2-F09","fix":"Add the test symbol to 2.1 Targets and replace it with a one-way dependency assertion that pins gwiki's gobby-code path/version and verifies gcode has no gobby-wiki dependency.","location":"P2 / § 2.1","prevention":"Search manifest dependency names across source and tests before adding or reversing crate edges.","principle":"Plans that invert a dependency must update existing tests that enforce the prior architecture.","root_cause":"`crates/gwiki/src/api.rs::crate_has_no_gcode_dependency` explicitly fails when gwiki depends on gobby-code.","section_id":"2.1","severity":"blocking"},{"category":"missing-requirement","check_key":"feature-contract-descriptor","description":"Repointing duplicates or mislabels gwiki commands and changes deterministic generated content beyond the permitted provenance normalization.","finding_id":"R2-F10","fix":"Keep both `BinaryContract` descriptors; remove the retired `codewiki` arm from the gcode resolver, add the flat `code` modes to the gwiki resolver, and target the moved feature builder plus contract-coverage tests.","location":"P2 / § 2.4","prevention":"Inspect registry cardinality and resolver ownership before repointing a contract source.","principle":"Per-binary contract descriptors must retain their binary identity during command ownership changes.","root_cause":"The feature catalog already enumerates gcode and gwiki descriptors separately, while 2.4 directs the gcode descriptor to the gwiki contract.","section_id":"2.4","severity":"blocking"},{"category":"bad-sequencing","check_key":"parity-terminal-gate","description":"A passing result can precede later output-affecting changes and therefore does not gate the final P2 state.","finding_id":"R2-F11","fix":"Add 2.2, 2.4, and 2.5 to 2.6 dependencies alongside 2.3.","location":"P2 / § 2.6","prevention":"Place terminal verification after all producers of the measured artifact in the dependency graph.","principle":"An end-to-end parity gate must run after every change that can affect its measured output.","root_cause":"Section 2.6 depends only on 2.3, while 2.2 refactors generation, 2.4 changes contract metadata, and 2.5 changes provenance.","section_id":"2.6","severity":"blocking"},{"category":"weak-testability","check_key":"marker-scan-allowlist","description":"The stated acceptance cannot pass while the required parity normalizer, README, and old-marker characterization exist.","finding_id":"R2-F12","fix":"Replace the workspace-wide zero scan with a production-reader/writer scan that permits only enumerated fixture and normalizer locations and fails on new production references.","location":"P2 / § 2.5","prevention":"Define production roots and a pinned fixture/normalizer allowlist for literal-retirement checks.","principle":"A source scan must distinguish forbidden production references from required test and normalization fixtures.","root_cause":"Section 1.1 requires old-marker literals, while 2.5 scans all `crates` and `src` for zero occurrences.","section_id":"2.5","severity":"blocking"},{"category":"traceability","check_key":"cron-reconciler-target-ownership","description":"The reconciler can be implemented yet never called; 3.1 also cannot validate 3.2 behavior before it exists.","finding_id":"R2-F13","fix":"Limit 3.1 to handler/registration removal. Add `init_orchestration`, `tests/test_runner_init.py`, and `tests/wiki/test_codewiki_dormant.py` to 3.2 Targets and make 3.2 own call insertion plus reconciliation assertions.","location":"P3 / § 3.2","prevention":"Match every acceptance-owned file to the leaf that first makes the behavior exist.","principle":"The deliverable introducing an integration must own its call site and tests.","root_cause":"Section 3.1 claims runner-init test changes for behavior introduced in dependent section 3.2, while 3.2 omits the runner-init call site and reconciliation tests from Targets.","section_id":"3.2","severity":"blocking"},{"category":"unhandled-edge","check_key":"cron-reconciliation-fail-closed","description":"A mid-loop exception or `update_job` returning `None` can leave enabled rows. Scheduler startup then has rows whose handler was removed, reaching the unknown-handler failure acceptance claims is impossible.","finding_id":"R2-F14","fix":"Treat `None` and exceptions as failure, re-query the enabled prefix, succeed only at zero rows, and fail or suppress scheduler initialization on reconciliation failure. Add injected mid-loop failure and post-verification tests.","location":"P3 / § 3.2","prevention":"Test partial update, `None`, exception, post-write verification, and scheduler-start ordering for destructive reconciliation.","principle":"Handler removal is safe only after every enabled legacy row is durably disabled.","root_cause":"The planned list/update loop uses independent calls and specifies only its successful path.","section_id":"3.2","severity":"blocking"},{"category":"traceability","check_key":"dormant-status-schema","description":"Both contracts cannot be satisfied, leaving route implementation and UI typing with conflicting sources of truth.","finding_id":"R2-F15","fix":"Update Constraints to the recorded V1 decision: exact three-field status response, refresh-only agent-route auth entry, and open unlisted status route.","location":"P3 / § 3.3","prevention":"After an accepted enhancement decision, update every authoritative constraint and downstream producer/consumer reference.","principle":"Authoritative constraints and producer/consumer acceptance must define one serialized schema and auth policy.","root_cause":"Constraints retain legacy status fields and ambiguous route auth, while V1, 3.3, and 3.6 require exactly `{enabled,state,reason}` with status open.","section_id":"3.3","severity":"blocking"},{"category":"bad-sequencing","check_key":"route-ui-atomicity","description":"Completing 3.3 alone removes live routes before the consumer rewrite; 3.6 also omits `WikiTab.tsx`, which constructs the deleted callback.","finding_id":"R2-F16","fix":"Combine 3.3 and 3.6 into one atomic task, or explicitly bind them to one closure/merge boundary; add `WikiTab.tsx` to Targets and require web typecheck plus integrated route/UI tests.","location":"P3 / §§ 3.3 and 3.6","prevention":"Trace API removals through every caller and encode one merge/closure boundary with integrated type and route tests.","principle":"Removing an API route and rewriting its sole consumer must form one atomic integration change.","root_cause":"Sections 3.3 and 3.6 are independent leaves even though current UI code calls both legacy routes and constructs the refresh callback through `WikiTab.tsx`.","section_id":"3.3","severity":"blocking"},{"category":"weak-testability","check_key":"frozen-digest-state","description":"A stale unseen digest still fires once when its hash differs or has no stored value, so file age alone cannot prove the planned no-trigger outcome.","finding_id":"R2-F17","fix":"Seed the stored hash to the frozen digest and verify repeated later runs stay quiet; test absent digest separately. If unseen stale digests must be ignored, add the timestamp production rule and its implementation scope.","location":"P3 / § 3.7","prevention":"Map each regression fixture variable to an actual implementation branch before naming the expected outcome.","principle":"An acceptance test must control the state transition the implementation observes.","root_cause":"The digest reader ignores age, and trigger evaluation compares content hash with stored state.","section_id":"3.7","severity":"blocking"},{"category":"traceability","check_key":"active-doc-surface","description":"The installed gcode skill and active gcode CLI contract remain stale after the command is removed, contradicting 4.2 acceptance.","finding_id":"R2-F18","fix":"Add `crates/gcode/assets/SKILL.md` and `docs/contracts/gcode-cli.md` to 4.2 Targets, update their retired-command guidance, and add a focused scan across current skill/contract/guide surfaces with historical changelogs excluded.","location":"P4 / § 4.2","prevention":"Inventory include-str assets, installed skill templates, CLI contracts, and current guides separately from historical evidence.","principle":"Command retirement must update every active installed or contractual documentation surface.","root_cause":"Targets cover the shared skill and gwiki docs while gcode's embedded skill asset and gcode CLI contract still advertise `gcode codewiki`.","section_id":"4.2","severity":"blocking"},{"category":"traceability","check_key":"cli-test-module-partition","description":"Relocating or deleting the module wholesale either loses unrelated gcode setup coverage or creates a gwiki test that cannot compile.","finding_id":"R2-F19","fix":"Move only CodeWiki parse cases to gwiki; relocate `parse_setup_standalone` into an enduring gcode setup/top-level test module and list both destinations in Targets.","location":"P2 / § 2.3","prevention":"Classify every symbol in a moved test file before assigning whole-file ownership.","principle":"A file move must preserve unrelated tests that share the source module.","root_cause":"`crates/gcode/src/cli/tests/codewiki.rs` also contains `parse_setup_standalone`, which belongs to the enduring gcode setup command.","section_id":"2.3","severity":"blocking"},{"category":"over-engineering","check_key":"skill-surface-proportionality","description":"A new gwiki skill surface would add installation and ownership machinery with no second consumer or stated requirement.","finding_id":"R2-F20","fix":"Resolve the option in the plan: update the existing shared code-index skill with paused/manual `gwiki code` guidance and a pointer to `docs/guides/codewiki.md`.","location":"P4 / § 4.2","prevention":"Resolve one-consumer documentation choices to the existing owner before plan approval.","principle":"A documentation mechanism earns its place through a named consumer and a concrete need for added indirection.","root_cause":"Section 4.2 leaves a choice to create or move guidance to a gwiki skill surface, while the existing shared code-index skill already owns the CodeWiki lifecycle consumer.","section_id":"4.2","severity":"nit"}],"resolution":"all 20 findings accepted by user vote (2026-08-07); R2-F14 and R2-F16 applied as user-approved variants; edits applied to the plan artifact","reviewer_session":"#10270","round":2,"round_number":2,"verdict":"needs_review"},"session_id":"89a747f7-16f8-4f17-a15e-81e23fb067e4"}
```

**Round 3** `kind: adversary`

- reviewer_run: `2ac68bac-3355-47b3-9677-52550fcfd909`
- reviewer_session: `185e4987-bf6d-4fc5-a7b1-a216f13224d6` (#10277)
- evidence_id: `91e36693-ed33-438c-b3a1-75a3c21ff145` (round_number 3)
- verdict: needs_review; findings_presented: 8 (all blocking)
- accepted: all 8 (R3-F01…R3-F08); every finding independently verified
  against the codebase or the plan text before the vote
- declined: none — R3-F07's defect was accepted but resolved with a smaller
  remedy than the finding proposed (see resolution_notes)
- resolution_notes: R3-F01 resolved by making everything under
  `fixtures/codewiki_parity/project/` immutable — the one-time gcode capture
  writes provenance to the outer fixture README, gwiki writes capture metadata
  only to the temp run dir, and each mode re-asserts unchanged fixture bytes
  and a clean tracked tree (new 1.1.5). Verification also surfaced a second,
  fatal gap inside the same finding's scope: `capture_commit_stamp`
  (`run.rs:594-622`) shells `git rev-parse HEAD` and `git status --porcelain`
  and stamps `commit`, `commit_dirty`, and a local wall-clock `generated:`
  into page frontmatter and `_meta/codewiki.json`, none of which the round-2
  normalization rule stripped — so the parity gate could never have passed;
  normalization now strips exactly those per-run stamps plus the intentional
  provenance diffs. R3-F02 resolved by giving 2.4 the terminal privatization
  it was promised in 1.3 (`lib.rs` `pub mod commands` → private,
  `commands/mod.rs` child visibility, facade-boundary repin, new 2.4.3);
  `commands` is public today only because the binary's `cli.rs` reaches
  `gobby_code::commands::codewiki::*`, which this leaf deletes. R3-F03
  resolved by adding the six `crates/gcode/src/codewiki_facts/` files to 2.1
  Targets with the bounded rationale in prose — the validator rejects
  `::*` scope-reasons on files with no index record, so these are listed as
  bare paths (they are created by 1.2). R3-F04 accepted as stated and applied:
  `DAEMON_AGENTIC_CALLER` `"gcode.codewiki"` → `"gwiki.code"` (tool_loop.rs:37,
  its stability test at :310, and the outcome.rs:476 fixture), lock
  diagnostics "another gcode codewiki run" → "another `gwiki code` run"
  (lock.rs:139 production, :168 assertion, :9 doc), and `ToolPolicy.cli`
  explicitly preserved as `"gcode"` because it names the CLI whose read-only
  query tools the loop executes; pinned by new 2.1.7. R3-F05 accepted with a
  corrected premise: verification refuted "four bare literals" — only
  `publication.rs:559` emits a bare `generated_by: gcode-codewiki` string
  (inside `placeholder_page`, reached from production `atomic_write`);
  `changes.rs:164`, `text/frontmatter.rs:266`, and `ownership/render.rs:81`
  reference the shared constant and still need edits because the constant is
  being renamed. All four plus the two moved test files carrying the bare
  literal are now 2.5 Targets. R3-F06 resolved by splitting detection from
  reaction: `reconcile_codewiki_crons_disabled` returns a structured
  `CodewikiCronReconciliation(disabled, failed, residual_enabled)` and stays
  runner-independent, because `mark_service_degraded(runner, service_name)`
  requires a `GobbyRunner`; `init_orchestration(runner)` — which already calls
  that helper for two other services — logs the residue and marks
  `codewiki_dormant_reconciliation` degraded. R3-F07's defect (a
  characterization leaf authorizing an undeclared production fix) was accepted,
  but the proposed remedy of adding conditional production Targets was not
  applied: verification showed the truth-digest path is fail-soft at every
  layer — missing vault, missing file, unreadable file, malformed JSON, and
  wrong-schema payload all resolve to an empty digest that the caller skips; a
  missing stored hash compares unequal rather than raising; and each
  per-project iteration is wrapped in a catch-all. There is no crash branch, so
  the smaller fix is to remove the edit authorization and route any surprise to
  a separate task, which closes the same gap without adding speculative scope.
  R3-F08 resolved by re-deriving M1 with corrected routing: 1.2 frontend →
  backend and 2.1 fullstack → backend (both Rust-only), 3.6 frontend →
  fullstack (Python config plus TypeScript UI).

```json plan-review-round
{"evidence_id":"91e36693-ed33-438c-b3a1-75a3c21ff145","plan_hash":"8f50978bc9978f47ecf2e00897e381cf21f806d59ba4013cc2b2a128c5179f4e","round_number":3,"round_result":{"accepted":["R3-F01","R3-F02","R3-F03","R3-F04","R3-F05","R3-F06","R3-F07","R3-F08"],"coverage_attestation":{"adjacent_variant_complete":true,"attestation_digest":"a2b65c6294d32b1d3b2fd08e9bb9de973d0d951c95155cc4fcdf6013ed2d98c7","cross_lane_interaction_complete":true,"disposition_counts":{"dismissed":1,"emitted_findings":8,"total":9},"evidence_id":"91e36693-ed33-438c-b3a1-75a3c21ff145","lanes":[{"candidate_count":2,"lane_id":"requirements_traceability","status":"completed"},{"candidate_count":5,"lane_id":"repository_blast_radius","status":"completed"},{"candidate_count":2,"lane_id":"runtime_invariants","status":"completed"}],"shadow_manifest_status":{"entry_count":17,"manifest_digest":"4218c86e4bdc657a7cb3bc75becdf7bcee12ed1dd5b1515454f8b1d0815dc840","status":"valid"},"source_digest":"7591f8d690f01e3bdf9b130589c69928a2085c5ecadfd647aff90fc5436163af","version":1},"declined":[],"findings":[{"category":"weak-testability","check_key":"parity-capture-metadata-location","description":"Section 1.1 says the committed fixture project never changes, yet both modes record capture metadata in a fixture README and acceptance 1.1.4 points to `project/README.md`. The engine serializes dirty Git state and hashes project-source bytes, so the gwiki comparison can mutate its own input or introduce a non-normalized dirty-state difference.","finding_id":"R3-F01","fix":"Keep every byte under `fixtures/codewiki_parity/project/` immutable. Write one-time gcode baseline provenance to a dedicated artifact outside `project/`; emit gwiki comparison metadata only to the temporary run directory or stdout. Point 1.1.4 and M1 at that metadata artifact and assert unchanged fixture bytes plus clean tracked state after each mode.","location":"P1 / § 1.1 (interaction with § 2.6 and M1)","prevention":"Separate immutable indexed input, committed baseline provenance, and transient comparison metadata; assert fixture bytes and Git cleanliness before and after each engine mode.","principle":"Parity inputs and repository state must remain immutable across baseline and comparison captures.","root_cause":"The round-2 provenance fix records per-run revision/version metadata in a tracked fixture README without separating the digest-pinned project README from outer capture metadata.","section_id":"1.1","severity":"blocking"},{"category":"traceability","check_key":"rust-public-boundary-closure","description":"The final plan state still exposes `gobby_code::commands` and its public child modules even though dispatch becomes an internal library consumer and `gcode codewiki` is removed. Section 2.4 omits `lib.rs`, `commands/mod.rs`, and the public-surface pin, so the applied round-2 boundary fix remains incomplete.","finding_id":"R3-F02","fix":"Add `crates/gcode/src/lib.rs::*`, `crates/gcode/src/commands/mod.rs::*`, and `crates/gcode/tests/facade_boundary.rs::*` to 2.4 Targets. Make `commands` private after the CodeWiki removal and repin the exact enduring public API.","location":"P1 / § 1.3 and P2 / § 2.4","prevention":"For every temporary public export, name its last consumer, removal leaf, visibility edit, and API-pin update.","principle":"A public-to-private ownership move must close every temporary export and repin the final API at the leaf that removes its last consumer.","root_cause":"Section 1.3 keeps `commands` public while moving dispatch behind `run_cli`, but sections 2.1 and 2.4 never target the module visibility or boundary test for the promised terminal privatization.","section_id":"1.3","severity":"blocking"},{"category":"gobby-format","check_key":"conditional-facade-target-coverage","description":"Relocation can concretely expose a missed query family across raw Context/db/model/scope and tool-executor seams. Section 2.1 instructs the implementer to extend the facade in that branch, but the leaf cannot claim or validate any `crates/gcode/src/codewiki_facts/` file.","finding_id":"R3-F03","fix":"Add `crates/gcode/src/codewiki_facts/{mod,scope,symbols,graph,search,text}.rs::*` to 2.1 Targets with a scope reason limited to relocation-discovered typed-family extensions. Require the same leaf to update the module-header classification and read-only facade tests for any extension.","location":"P2 / § 2.1 (boundary with § 1.2)","prevention":"For each contingency clause containing \"extend,\" \"fix,\" or \"update,\" enumerate its exact source and test Targets before manifest handoff.","principle":"Every reachable implementation branch that authorizes file edits must list those exact files in the deliverable Targets.","root_cause":"The applied runtime-boundary edit lets 2.1 extend any missed `codewiki_facts` family in the same leaf, while 2.1 declares none of the facade files as Targets.","section_id":"2.1","severity":"blocking"},{"category":"traceability","check_key":"runtime-ownership-identities","description":"The moved engine currently sends daemon-agentic requests as `gcode.codewiki`, pins that value in an outcome fixture, and tells lock-contention users another `gcode codewiki` run owns the lock. Those identities become false after the old CLI is removed. The tool policy's `cli: gcode` remains valid because those tools intentionally execute read-only gcode queries.","finding_id":"R3-F04","fix":"Add the post-move `text/generation/tool_loop.rs`, `text/generation/outcome.rs`, `lock.rs`, and associated tests to 2.1 Targets. Rename the daemon caller to `gwiki.code` and lock diagnostics to `gwiki code`; explicitly preserve `ToolPolicy.cli = \"gcode\"` and pin all three distinctions in `code_engine_boundary.rs`.","location":"P2 / §§ 2.1, 2.3, and 2.4","prevention":"Sweep protocol caller IDs, diagnostics, fixtures, lock messages, telemetry labels, and policy-owned binary names during every command ownership move.","principle":"An ownership migration must update caller identities and user-visible command references while preserving only identities that still describe a real dependency.","root_cause":"The move inventory covers imports, flags, and dispatch but omits embedded daemon-caller and lock-diagnostic identities.","section_id":"2.1","severity":"blocking"},{"category":"gobby-format","check_key":"provenance-writer-target-coverage","description":"Four current engine writers survive at post-move destinations: publication's bare literal plus changes, frontmatter, and ownership renderers. None appears in 2.5 Targets, so the marker-flip leaf cannot implement or validate its own production allowlist.","finding_id":"R3-F05","fix":"Add `crates/gwiki/src/commands/code/publication.rs`, `build_parts/changes.rs`, `text/frontmatter.rs`, `ownership/render.rs`, and the moved marker architecture/contract tests to 2.5 Targets. Require publication's bare literal to use `GENERATED_BY_GWIKI_CODE`.","location":"P2 / § 2.5","prevention":"Inventory and target every producer, consumer, fixture, and bare literal when changing a serialized provenance marker.","principle":"Every writer governed by a renamed serialization contract must be an explicit Target in the leaf that changes the contract.","root_cause":"Section 2.5 names moved-engine emitter edits in prose but its Targets cover only the shared constant and gwiki readers/tests.","section_id":"2.5","severity":"blocking"},{"category":"unhandled-edge","check_key":"cron-degradation-dataflow","description":"`reconcile_codewiki_crons_disabled(cron_storage) -> int` has no runner and cannot represent failed or residual row identities, yet the same section requires `mark_service_degraded(runner, \"codewiki_dormant_reconciliation\")`. The repository API requires a `GobbyRunner`, and `update_job` may return `None`, so the specified contract cannot perform or report the accepted degraded-startup transition.","finding_id":"R3-F06","fix":"Make the storage helper return a structured result with `disabled_count`, failed transition IDs, and residual enabled IDs. Keep it runner-independent; require `init_orchestration(runner)` to log residue and call `mark_service_degraded` when either failure set is non-empty. Update 3.2.1 and 3.2.4 to test the helper result and caller-visible degraded state for raised and `None` outcomes.","location":"P3 / § 3.2","prevention":"For each reconciliation branch, trace detection, returned residue, caller action, observable state, and retry behavior through concrete signatures.","principle":"Every required failure-state transition must have an explicit input/output path from detection to the owning state object.","root_cause":"The round-2 variant declares a storage-only helper returning one integer while also requiring that helper to mark a `GobbyRunner` degraded and distinguish nullable/exceptional updates from convergence.","section_id":"3.2","severity":"blocking"},{"category":"gobby-format","check_key":"conditional-production-target-coverage","description":"The stale/absent-digest behavior spans the dream service trigger, persisted last-seen hash methods, and digest loading. If the test exposes the allowed crash branch, the leaf cannot complete the instructed production fix within its declared Target inventory.","finding_id":"R3-F07","fix":"Add conditional, scope-reason Targets using the exact indexed symbols for `_apply_truth_change_triggers`, the truth-digest hash get/set methods in `storage_runs.py`, and the bounded digest-loading branch in `truth_digest.py`. State that production edits occur only for a crash demonstrated by the new characterization test.","location":"P3 / § 3.7","prevention":"When a characterization test may convert into a regression fix, list every named production branch as a conditional Target with a bounded scope reason.","principle":"A characterization task that authorizes a production fix must declare the exact production seams it may change.","root_cause":"The round-2 state-accurate characterization edit added a same-task crash-fix branch while retaining only the new test file in Targets.","section_id":"3.7","severity":"blocking"},{"category":"gobby-format","check_key":"manifest-code-domain-routing","description":"M1 routes Rust-only facade task 1.2 to frontend, Rust-only engine move 2.1 to fullstack, and mixed Python plus TypeScript task 3.6 to frontend. Deterministic expansion would dispatch all three to the wrong implementation scope.","finding_id":"R3-F08","fix":"Re-derive and apply M1 with `1.2.implementation_domain = backend`, `2.1.implementation_domain = backend`, and `3.6.implementation_domain = fullstack`; keep categories, dependencies, TDD flags, criteria, and covers labels unchanged.","location":"M1 entries sourced from §§ 1.2, 2.1, and 3.6","prevention":"Before manifest application, compare every code entry's domain against all target languages and runtime layers, then pass explicit overrides for mismatches.","principle":"Every code manifest entry must route to the implementation domain represented by its complete Target set.","root_cause":"The coordinator handoff retained heuristic domains without reviewer overrides for Rust-only and mixed Python/web leaves.","section_id":"M1","severity":"blocking"}],"resolution":"All 8 findings accepted after independent verification (2026-08-07); edits applied to the plan artifact. R3-F01 applied plus an in-scope extension: verification showed capture_commit_stamp stamps commit/commit_dirty/wall-clock generated into pages and _meta, none of which round-2 normalization stripped, so the parity gate could never have passed; normalization now strips those per-run stamps (new 1.1.5). R3-F05 applied with a corrected premise: only publication.rs emits a bare literal; changes.rs, text/frontmatter.rs, and ownership/render.rs reference the shared constant and need the rename. R3-F07's defect accepted but resolved with a smaller remedy than proposed: the truth-digest read path is fail-soft at every layer (missing vault/file, unreadable file, malformed JSON, wrong schema all degrade to an empty digest; a missing stored hash compares unequal; each iteration is wrapped in a catch-all), so no crash branch exists to target; the unbounded production-edit authorization was removed instead of adding speculative conditional Targets. R3-F03 applied as bare-path Targets because the validator rejects ::* scope-reasons on files with no index record.","reviewer_session":"#10277","round":3,"round_number":3,"verdict":"needs_review"},"session_id":"89a747f7-16f8-4f17-a15e-81e23fb067e4"}
```

**Round 4** `kind: adversary`

- reviewer_run: `4d420c7f-a82e-4fc5-9ad4-f531cb3c6475`
- reviewer_session: `1078a225-a20d-405b-8d10-83f2db35ed7f` (#10280)
- evidence_id: `3df23b57-5795-4dc8-86dd-30b3a6c2c5bb` (round_number 4)
- verdict: needs_review; findings_presented: 6 (all blocking)
- accepted: all 6 (R4-F01…R4-F06); the reviewer independently validated
  round 3's two departures (R3-F07's fail-soft reasoning and the R3-F03/F05
  bare-path choice) and did not re-litigate them
- declined: none — R4-F03's defect was accepted but resolved proportionally
  rather than by the enumeration it proposed (see resolution_notes)
- resolution_notes: three of the six were regressions introduced by round 3's
  own fixes. R4-F01: the 1.1.5 cleanliness assertion I added contradicted the
  capture mode's required outputs — the gcode mode must write the outer
  README and `baseline.sha256`, both tracked — so the assertion is now scoped
  per mode (`project/` immutable and clean for both; gcode may write exactly
  those two declared artifacts; gwiki, running after they are committed,
  leaves the whole tracked fixture tree clean). R4-F06: the round-3 structured
  -result rewrite left a stale "returning the count of successful
  transitions" sentence contradicting `CodewikiCronReconciliation`; the
  section now states explicit population rules for `disabled`, `failed`, and
  `residual_enabled`. R4-F02 is the most consequential: 2.1 deleted
  `commands::codewiki` while `crates/gcode/src/cli.rs` (four `From` impls
  naming `gobby_code::commands::codewiki` types at :75, :93, :112, :128) and
  `crates/gcode/src/dispatch.rs` (`CodewikiAiOptions` at :143-144 plus
  `run_compare`/`run_purge`/`run_repair`/`run` at :600-609) still consume it
  until 2.4 — so the 2.1 boundary could not compile. 2.1 now copies and
  leaves the source tree intact, 2.4 became the atomic deletion boundary for
  the CLI references, dispatch sites, module declaration, and whole source
  tree together, and 2.5 now depends on 2.4 because renaming the shared
  marker constant while two engine copies exist would break the one awaiting
  deletion. R4-F04: verified that gwiki's `Cargo.lock` stanza lists
  `gobby-core` and no `gobby-code`, so 2.1's new dependency changes the
  lockfile while 2.6 runs `cargo build --locked` before 4.1 regenerates it;
  `Cargo.lock` is now a 2.1 Target regenerated in the same leaf, with 4.1
  keeping the later version-bump regeneration. R4-F05: the two
  `list_system_jobs_by_name_prefix` calls can raise outside the structured
  result, so `init_orchestration` now wraps the reconciliation call in its own
  try/except that degrades and continues (new 3.2.5). R4-F03's
  under-declaration was accepted, but its proposed remedy — enumerating ~106
  source and ~103 destination paths as individual Targets — was not applied.
  The `target-coverage` lint is mention-driven (it fails a section only for a
  concrete path named in the body after a change-intent verb or in an
  acceptance ref) and the contract defines no recursive or directory Target
  form, so per-file enumeration is not what makes a tree move conforming. It
  would also encode no decision and would go stale the moment a file is added
  to the tree before implementation. Instead 2.1 and 2.4 now declare the
  wholesale move explicitly — every file relocates, none is left behind — the
  Targets enumerate the files changed beyond pure relocation, and acceptance
  2.1.1 and 2.4.1 pin the outcome on both sides (counterpart for every source
  file plus a locked build; nothing left under the old root and no references
  to it).

```json plan-review-round
{"evidence_id":"3df23b57-5795-4dc8-86dd-30b3a6c2c5bb","plan_hash":"42bd4eda41f23ccc4af00a2a82c9c3342258ce1d6c92351dd869fb61a1a2defa","round_number":4,"round_result":{"accepted":["R4-F01","R4-F02","R4-F03","R4-F04","R4-F05","R4-F06"],"coverage_attestation":{"adjacent_variant_complete":true,"attestation_digest":"a7cd6c3e19f8c2bef209357323ea25503608bff7e08448fa6ba548c8c3efe704","cross_lane_interaction_complete":true,"disposition_counts":{"dismissed":0,"emitted_findings":6,"total":6},"evidence_id":"3df23b57-5795-4dc8-86dd-30b3a6c2c5bb","lanes":[{"candidate_count":2,"lane_id":"requirements_traceability","status":"completed"},{"candidate_count":2,"lane_id":"repository_blast_radius","status":"completed"},{"candidate_count":2,"lane_id":"runtime_invariants","status":"completed"}],"shadow_manifest_status":{"entry_count":17,"manifest_digest":"4daf154560d231a6de468b1d754b2fbceaa8c60047b974002a34adbf8fe63c94","status":"valid"},"source_digest":"f739b8a007d6af0cf54ca1f1a157598c84dcc756e9b28b6ca8cb115cc17aafde","version":1},"declined":[],"findings":[{"category":"weak-testability","check_key":"parity-baseline-output-cleanliness","description":"The one-time gcode capture must write `crates/gwiki/tests/fixtures/codewiki_parity/README.md` and `baseline.sha256`, yet 1.1.5 and M1 require the tracked fixture tree to be clean after every engine mode. The baseline-producing mode therefore fails on its own required outputs even though `project/` remains immutable.","finding_id":"R4-F01","fix":"Specify that gcode mode leaves `project/` byte-identical and clean while allowing exactly the outer README and `baseline.sha256` as declared outputs; after those artifacts are committed, require gwiki mode to leave the entire tracked fixture tree clean. Update 1.1.5 and M1 to match.","location":"P1 / § 1.1","prevention":"For each parity mode, enumerate immutable inputs, permitted outputs, and the exact pre/post Git-state assertion.","principle":"A verification mode may permit only its declared outputs; its cleanliness assertion must exclude those outputs or run after they are committed.","root_cause":"The R3-F01 repair broadened the post-mode cleanliness check to the tracked fixture tree while the baseline mode is required to write the tracked outer README and baseline manifest.","section_id":"1.1","severity":"blocking"},{"category":"bad-sequencing","check_key":"command-module-removal-atomicity","description":"Completing 2.1 removes `commands::codewiki`, but compiled `cli.rs` still names four types from that module and `dispatch.rs` still constructs `CodewikiAiOptions` and calls `run_compare`, `run_purge`, `run_repair`, and `run`. Those references are deleted only in 2.4, so the 2.1 boundary cannot compile.","finding_id":"R4-F02","fix":"Make 2.1 copy and decompose the engine into gwiki while retaining the gcode tree and module. After 2.3 adds the new CLI, make 2.4 atomically delete the old CLI/dispatch references, module declaration, tests, and source tree; make 2.5 depend on 2.4 before renaming the shared marker.","location":"P2 / §§ 2.1, 2.3, and 2.4","prevention":"At every module-removal boundary, compile the library and binary and trace all type, function, and test references to the leaf that removes them.","principle":"Every expanded leaf must leave all compiled consumers and their implementation modules present together.","root_cause":"The engine tree and module declaration are removed in 2.1, while gcode CLI types, dispatch calls, and tests that consume `commands::codewiki` remain until 2.4.","section_id":"2.1","severity":"blocking"},{"category":"gobby-format","check_key":"recursive-module-move-target-closure","description":"Section 2.1 inventories only two source files and a small subset of destinations for the complete CodeWiki tree relocation. The repository contract defines `::*` as every indexed symbol in one file, leaving 106 source paths and 103 destination paths outside coverage. The R3-F03 and R3-F05 bare paths themselves are valid because those files do not exist in the snapshot.","finding_id":"R4-F03","fix":"After applying R4-F02's copy-then-delete sequence, enumerate every destination file created by 2.1 and every source file deleted by 2.4. Use exact or justified `::*` Targets for existing symbol-bearing files and bare paths for snapshot-absent destinations, then rerun target and symbol validation.","location":"P2 / §§ 2.1 and 2.4","prevention":"Expand recursive moves into a source/destination file inventory and validate every existing symbol-bearing file plus every new path before manifest handoff.","principle":"A file-wide Target covers one file; every file created, moved, or deleted by a deliverable requires its own Target.","root_cause":"The plan describes a recursive 108-file tree move but treats `crates/gcode/src/commands/codewiki/mod.rs::*` as if it authorized the whole directory.","section_id":"2.1","severity":"blocking"},{"category":"bad-sequencing","check_key":"cargo-lock-dependency-atomicity","description":"The current gobby-wiki lock stanza lacks gobby-code. Adding that dependency in 2.1 makes `cargo build --locked` require a lockfile change, but 2.6 invokes the locked build before 4.1 regenerates `Cargo.lock`, so the engine and parity gate fail deterministically.","finding_id":"R4-F04","fix":"Add `Cargo.lock` to 2.1 Targets and regenerate it atomically with the new dependency. Keep 4.1 responsible for the later version-bump regeneration and require a focused `cargo build --locked` at the 2.1 boundary.","location":"P2 / §§ 2.1 and 2.6; P4 / § 4.1","prevention":"After every Cargo.toml dependency edit, inspect the affected lock stanza and run the leaf's locked build before accepting downstream dependencies.","principle":"A Cargo manifest dependency change and its lockfile update must land in the same buildable leaf when later validation uses `--locked`.","root_cause":"Section 2.1 adds gwiki's gobby-code dependency without targeting `Cargo.lock`, while lock regeneration is deferred to 4.1 after the 2.6 locked parity build.","section_id":"2.1","severity":"blocking"},{"category":"unhandled-edge","check_key":"cron-reconciliation-query-failures","description":"`list_system_jobs_by_name_prefix` can fail both before the loop and during the residual re-query. Section 3.2 only specifies how `init_orchestration` reacts to a returned result; an exception can escape the cron setup block, bypass `codewiki_dormant_reconciliation` degradation, and skip scheduler construction despite the promised non-blocking startup.","finding_id":"R4-F05","fix":"Wrap the reconciliation call in a dedicated `init_orchestration` try/except that logs, marks `codewiki_dormant_reconciliation` degraded, and continues cron setup. Add initial-list and final-requery exception tests that also assert the scheduler is constructed.","location":"P3 / § 3.2","prevention":"Test initial read, each write outcome, final verification read, caller degradation, retry, and scheduler construction for every startup reconciler.","principle":"Every storage operation in startup reconciliation needs an explicit failure transition that preserves the required startup outcome.","root_cause":"The structured result covers per-row update failures and residual IDs, while initial enumeration and final verification queries can raise before any result reaches the caller.","section_id":"3.2","severity":"blocking"},{"category":"traceability","check_key":"reconciliation-result-contract-drift","description":"The section defines `CodewikiCronReconciliation` with disabled, failed, and residual-enabled ID tuples, then says the loop returns the count of successful transitions. That scalar instruction contradicts the accepted round-3 contract and leaves the implementation leaf ambiguous.","finding_id":"R4-F06","fix":"Replace the count sentence with explicit population rules: successful non-None updates append to `disabled`, raised or None updates append to `failed`, and the post-loop query populates `residual_enabled`. Keep acceptance 3.2.1/3.2.4 and M1 aligned.","location":"P3 / § 3.2","prevention":"After changing a planned interface, search the section, acceptance items, manifest criteria, and changelog for the superseded return type.","principle":"A self-contained deliverable must state one return shape consistently in its signature, prose, acceptance, and manifest.","root_cause":"The R3-F06 structured-result rewrite updated the code block, acceptance, and M1 but left the prior scalar-count sentence in the implementation instructions.","section_id":"3.2","severity":"blocking"}],"resolution":"All 6 findings accepted after independent verification (2026-08-07); edits applied to the plan artifact. Three were regressions from round 3's own fixes: R4-F01 (the 1.1.5 cleanliness assertion contradicted the capture mode's required tracked outputs — now scoped per mode) and R4-F06 (a stale scalar-count sentence surviving the structured-result rewrite — now explicit population rules). R4-F02 verified directly: crates/gcode/src/cli.rs:75,93,112,128 name four types from gobby_code::commands::codewiki and crates/gcode/src/dispatch.rs:143-144,600-609 construct CodewikiAiOptions and call run_compare/run_purge/run_repair/run, so removing the module in 2.1 could not compile; 2.1 now copies, 2.4 became the atomic deletion boundary for CLI references, dispatch sites, module declaration and source tree together, and 2.5 now depends on 2.4 because the shared marker constant is referenced by both engine copies until the old one is gone. R4-F04 verified against Cargo.lock: gobby-wiki's stanza lists gobby-core and no gobby-code, so Cargo.lock is now a 2.1 Target regenerated in the same leaf with a focused locked build at that boundary. R4-F05 applied as a dedicated init_orchestration try/except with new acceptance 3.2.5. R4-F03's under-declaration was accepted but resolved proportionally rather than by enumerating ~106 source and ~103 destination paths: the target-coverage lint is mention-driven and the contract defines no recursive or directory Target form, so per-file enumeration is not what makes a tree move conforming, encodes no decision, and would go stale the moment a file is added before implementation; instead 2.1 and 2.4 declare the wholesale move explicitly, the Targets enumerate files changed beyond pure relocation, and acceptance 2.1.1 and 2.4.1 pin the outcome on both sides.","reviewer_session":"#10280","round":4,"round_number":4,"verdict":"needs_review"},"session_id":"89a747f7-16f8-4f17-a15e-81e23fb067e4"}
```

**Human handoff** `kind: verification`

- authorized_by: user (interactive)
- after_round: 4
- verdict_at_handoff: needs_review
- completed_plan_review_rounds: 4
- resolution_notes: The user authorized expansion at round-4 `needs_review`
  with execution explicitly held. This plan is **not** converged and the
  handoff does not claim it is. Round 4 accepted six blocking findings, of
  which three (R4-F01, R4-F05, R4-F06) were regressions introduced by round
  3's own repairs, and three (R4-F02, R4-F03, R4-F04) were independent
  pre-existing defects — R4-F02 in particular would have left leaf 2.1
  uncompilable. Round 4's repairs were applied in commit `5a16be85f` under
  task #19756 and no round 5 confirmed them, so no round has yet closed with
  zero independent defects. The independent-defect trend across rounds is
  20 → 6 → 3, still declining and not yet zero. Expansion proceeds because it
  is reversible: the resulting tree can be reset and re-expanded if a later
  round changes the deliverable boundaries.
  `## M1 Task Manifest` was written earlier by `apply_plan_handoff_manifest`
  and re-derived during round 3 (R3-F08 domain corrections); it was verified
  against the post-round-4 sections before expansion — 17 entries for 17
  deliverables, every acceptance item carrying a `covers:` label and appearing
  in its entry's `validation_criteria`, and the round-4 dependency rewiring
  (2.5 → 2.4, 2.6 → 2.3/2.4/2.5) reflected.
  This plan carries no adversary approval verdict and no `coverage_attestation`
  for the current bytes; the last recorded verdict remains `needs_review` at
  round 4.

**Round 5** `kind: adversary`

- reviewer_run: `45d9dd49-abd7-41fd-8a50-7e3afc302cf2`
- reviewer_session: `03b16318-0004-4bd2-8eff-2e9128b75653` (#10314)
- evidence_id: `6ead05a4-ca7a-4400-b517-914178f0f06f` (round_number 5)
- verdict: needs_review; findings_presented: 15 (all blocking)
- accepted: all 15 (R5-F01…R5-F15) — user vote, interactive
- declined: none
- resolution_notes: first post-expansion review (round 4's repairs landed in
  `5a16be85f` and expansion created #19810–#19830 before this round). None of
  the 15 identify regressions introduced by round 4's repairs; all are
  independent pre-existing defects surfaced by deeper coverage — acceptance
  executability at dependency position (R5-F01), normalization-contract
  traceability and oracle reproducibility (R5-F02/F03), wrapper- and
  admission-level behavior preservation (R5-F04/F06), relocation target
  closure (R5-F05), marker-independent fallback classifiers and librarian
  production paths (R5-F07/F08), the 2.6 acceptance matrix (R5-F09), test
  consumer/scan/doc inventory closure (R5-F10/F11/F13/F15), reconciler
  placement (R5-F12), and the 4.2 dependency closure (R5-F14). All repairs
  are applied to the artifact (see the fence resolution for the per-finding
  mapping); repairs touching already-expanded sections must flow into tasks
  #19810–#19830 before build — expansion was authorized as reversible.
  Independent-defect trend across adversary rounds: 20 → 6 → 3 → 15 (the
  round-5 jump reflects new check_keys from post-expansion review depth, on
  plan bytes never previously reviewed at that depth). The user raised the
  review cap from 5 to 8 and authorized rounds 6–8 to run unattended
  (2026-08-08), with instruction to stop early if later rejections are
  dominated by fixer-induced defects rather than independent ones.

```json plan-review-round
{"evidence_id":"6ead05a4-ca7a-4400-b517-914178f0f06f","plan_hash":"a55e4791987cb50279332298766ce3dd9acfcaf918e4cb76e3ae175c46675e6e","round_number":5,"round_result":{"accepted":["R5-F01","R5-F02","R5-F03","R5-F04","R5-F05","R5-F06","R5-F07","R5-F08","R5-F09","R5-F10","R5-F11","R5-F12","R5-F13","R5-F14","R5-F15"],"coverage_attestation":{"adjacent_variant_complete":true,"attestation_digest":"f344727275a817010c1fa807e16764fe9f2fecc13b724719034817bb74957a4f","cross_lane_interaction_complete":true,"disposition_counts":{"dismissed":3,"emitted_findings":15,"total":18},"evidence_id":"6ead05a4-ca7a-4400-b517-914178f0f06f","lanes":[{"candidate_count":5,"lane_id":"requirements_traceability","status":"completed"},{"candidate_count":7,"lane_id":"repository_blast_radius","status":"completed"},{"candidate_count":6,"lane_id":"runtime_invariants","status":"completed"}],"shadow_manifest_status":{"entry_count":17,"manifest_digest":"be39e15614aaacca9e6f1f1372139966708dd4f0a3d704490fdcb6b56db619bc","status":"valid"},"source_digest":"cf09a9372df0b1ed965e87db41730a880c31458490199e8e9ebecc9b1d1c69cc","version":1},"declined":[],"findings":[{"category":"bad-sequencing","check_key":"parity-engine-mode-ordering","description":"Acceptance 1.1.4 and 1.1.5 require the gwiki engine mode before any preceding deliverable creates that command, so 1.1 cannot close at its declared point in the dependency graph.","finding_id":"R5-F01","fix":"Keep gcode capture, shared normalization, the two-mode parser, and fixture setup in 1.1; move actual gwiki execution, cleanliness assertions, and cross-engine comparison to 2.6 after 2.3.","location":"P1 / § 1.1","prevention":"For each executable acceptance item, verify that its command and all required binaries exist after the section's declared dependencies.","principle":"Every acceptance command must be executable when its deliverable becomes unblocked.","root_cause":"Section 1.1 owns both engine executions even though the gwiki code command is introduced in section 2.3.","section_id":"1.1","severity":"blocking"},{"category":"traceability","check_key":"parity-normalization-contract","description":"The parity acceptance broadens normalization beyond the stated constraint, allowing real output drift to disappear from the comparison.","finding_id":"R5-F02","fix":"Update Constraints with the complete unavoidable nondeterministic field list and require every remaining artifact byte to stay hashed; align 1.1 and 2.6 to that list.","location":"P1 / § 1.1","prevention":"Diff every normalization rule against the governing Constraints and enumerate each permitted exception in one canonical list.","principle":"Acceptance normalization must implement the exact exception set authorized by governing constraints.","root_cause":"Section 1.1 strips commit, dirty-state, and generation-time fields beyond the provenance and generated-by differences named in Constraints.","section_id":"1.1","severity":"blocking"},{"category":"weak-testability","check_key":"parity-nondeterministic-meta","description":"`_meta/codewiki.lock` can persist with PID and acquisition time, while the truth digest records current time, commit, and dirty state; hashing these artifacts makes identical runs differ.","finding_id":"R5-F03","fix":"Exclude synchronization-only lock state, normalize the explicitly authorized truth-digest fields, and add a same-engine back-to-back reproducibility assertion before cross-engine parity.","location":"P1 / § 1.1","prevention":"Run the parity pipeline twice with the same engine and fixture before comparing different engines, and inventory every time-, process-, and repository-state field.","principle":"A parity oracle must produce reproducible results for two identical runs of the same engine.","root_cause":"The planned hash manifest includes per-run lock and truth-digest state that the normalizer does not fully control.","section_id":"1.1","severity":"blocking"},{"category":"missing-requirement","check_key":"cli-freshness-wrapper-parity","description":"The plan can reproduce the flat engine flags while silently dropping freshness enforcement and its explicit bypass semantics.","finding_id":"R5-F04","fix":"Add the freshness wrapper and bypass flag to the migration inventory, specify their gwiki behavior and engine inputs, and test fresh, stale, and bypassed projects.","location":"P2 / § 2.3","prevention":"Inventory wrapper-level preconditions, flags, and bypass modes separately from engine imports during CLI ownership moves.","principle":"Moving a CLI-owned engine must preserve behavior implemented by the legacy command wrapper as well as behavior inside the engine.","root_cause":"The dependency inventory omits the legacy `ensure_project_fresh` wrapper and `--no-freshness` mode, and the gwiki CLI contract has no replacement policy.","section_id":"2.3","severity":"blocking"},{"category":"gobby-format","check_key":"non-pure-relocation-target-closure","description":"The current Targets cover the destination root and selected modules without enumerating the destination files whose imports must change, leaving expansion agents without the actual edit closure.","finding_id":"R5-F05","fix":"Add every destination file with changes beyond byte-for-byte copying to 2.1 Targets and to the relocation classification mapping; retain copy-only treatment for identical files.","location":"P2 / § 2.1","prevention":"Classify each moved file as byte-identical or semantically changed and require the changed set to equal the section's Targets.","principle":"Every file receiving semantic edits during a relocation must appear as an explicit changed-file Target.","root_cause":"The relocation is described as a wholesale copy, although crate-qualified and `self` imports require edits in at least 35 destination files.","section_id":"2.1","severity":"blocking"},{"category":"unhandled-edge","check_key":"mode-aware-project-admission","description":"Generic writer admission changes concurrency semantics, gates compare behind a live project row, and can block isolated or manual runs for up to the project-lock timeout.","finding_id":"R5-F06","fix":"Specify mode-aware admission that preserves the command's per-output lock, keeps compare read-only, and permits isolated or manual execution without a live project row; cover contention and timeout tests.","location":"P2 / § 2.3","prevention":"Compare old and new admission behavior per submode, including lock identity, timeout, project-row requirement, and read-only execution.","principle":"A command migration must preserve lock scope, timeout, read-only behavior, and isolated-operation admission for every mode.","root_cause":"The plan classifies `Code` as a generic gwiki writer, replacing the legacy per-output short lock with the generic per-project long lock.","section_id":"2.3","severity":"blocking"},{"category":"unhandled-edge","check_key":"old-marker-fallback-variants","description":"Old pages can remain specially treated after the marker flip because fallback signals still classify them as generated or trusted content.","finding_id":"R5-F07","fix":"Gate or retire each fallback in the affected lint and audit paths and add explicit variants for arbitrary `generated_by`, legacy type prefixes, and `code/**` trust handling.","location":"P2 / § 2.5","prevention":"Enumerate every production classifier for the retired state and add one negative regression for each independent signal.","principle":"Retiring an ownership marker requires removing every alternate classifier that recreates the retired classification.","root_cause":"Lint and audit paths retain marker-independent fallbacks based on any `generated_by`, legacy `code_*` types, and trusted `code/**` paths.","section_id":"2.5","severity":"blocking"},{"category":"traceability","check_key":"librarian-code-namespace-filter","description":"The planned test can pass while scheduled librarian work continues to curate generated code pages through production paths.","finding_id":"R5-F08","fix":"Add the production librarian modules to 2.5 Targets, filter `code/**` before aggregation and every mutation stage, and add an end-to-end scheduled-run regression.","location":"P2 / § 2.5","prevention":"Trace a namespace exclusion from discovery through aggregation, proposal, task, patch, promotion, and scheduled entrypoints.","principle":"A dormancy requirement must cover every production pipeline that can read, curate, task, patch, or promote the dormant content.","root_cause":"Section 2.5 targets a librarian test while production librarian modules still include `code/**` in aggregation and mutation flows.","section_id":"2.5","severity":"blocking"},{"category":"weak-testability","check_key":"legacy-behavior-acceptance-coverage","description":"The parity leaf can pass without exercising several promised modes and failure paths, so the manifest does not prove behavioral equivalence.","finding_id":"R5-F09","fix":"Add explicit 2.6 acceptance items and artifacts for compare, purge, repair, lock contention, scope selection, incremental runs, and representative failures; regenerate its manifest criteria.","location":"P2 / § 2.6","prevention":"Build an acceptance matrix from the governing behavior inventory and map each row to a test or behavior artifact.","principle":"Every legacy behavior promised by a migration plan needs a distinct observable acceptance artifact.","root_cause":"Section 2.6 asserts full-output parity and production-vault cleanliness while leaving compare, purge, repair, locking, scope, incremental, and failure behaviors implicit.","section_id":"2.6","severity":"blocking"},{"category":"traceability","check_key":"gateway-method-test-consumer","description":"The gateway deletion leaves an unowned direct test consumer, so the section's stated consumer closure is incomplete.","finding_id":"R5-F10","fix":"Add `tests/code_index/test_gcode_gateway.py` to 3.1 Targets and remove or replace its codewiki-method assertions with the retained gateway contract.","location":"P3 / § 3.1","prevention":"Run caller and direct-test-consumer sweeps for every deleted symbol and reconcile all hits against Targets.","principle":"Deleting a public method requires updating every direct production and test consumer.","root_cause":"`tests/code_index/test_gcode_gateway.py` directly exercises `GcodeGateway.codewiki` and is absent from section 3.1 Targets.","section_id":"3.1","severity":"blocking"},{"category":"weak-testability","check_key":"retired-module-scan-precision","description":"The current scan can pass trivially without detecting retired modules, while the obvious regex correction would make the required zero result impossible.","finding_id":"R5-F11","fix":"Replace it with exact retired module, import, and path scans or separate fixed-string checks; preserve the documented inert config keys and add an executable positive-control validation.","location":"P3 / § 3.1","prevention":"Validate search syntax with seeded positive and allowed-negative fixtures before adopting a zero-hit acceptance command.","principle":"A zero-hit retirement scan must match only retired artifacts and must demonstrably fail when one is present.","root_cause":"Acceptance 3.1.1 uses escaped alternation that searches for a literal pipe; using broad alternation would also match intentionally retained nightly configuration keys.","section_id":"3.1","severity":"blocking"},{"category":"bad-sequencing","check_key":"reconciler-indexer-independence","description":"Enabled legacy cron rows remain active whenever the code indexer is disabled or unavailable, violating the convergence guarantee.","finding_id":"R5-F12","fix":"Move reconciliation outside the code-indexer conditional and add startup tests for absent, disabled, and failed indexer configurations.","location":"P3 / § 3.2","prevention":"Trace startup placement through every surrounding conditional and test each optional dependency as present, absent, disabled, and failed.","principle":"Startup reconciliation for persisted state must run independently of optional subsystems it does not require.","root_cause":"The specified registrar replacement site sits inside `if runner.code_indexer is not None`, so reconciliation inherits the optional indexer's gate.","section_id":"3.2","severity":"blocking"},{"category":"weak-testability","check_key":"tool-chat-whitelist-regression","description":"Whitelist tests can pass without proving that codewiki stays unlisted when `allow_mutation=True`.","finding_id":"R5-F13","fix":"Add `tests/ai/test_tool_chat_tools.py` to 3.5 Targets and add an explicit mutation-enabled rejection assertion for codewiki.","location":"P3 / § 3.5","prevention":"For each allowlist removal, assert rejection with the broad enabling policy active and include the owning test file in Targets.","principle":"Removing a mutating tool from a policy allowlist needs a regression that reaches the policy branch where mutation would otherwise be allowed.","root_cause":"Section 3.5 omits the existing tool-chat test file, and its generic mutator coverage uses the default mutation-disabled path.","section_id":"3.5","severity":"blocking"},{"category":"bad-sequencing","check_key":"docs-daemon-dependency","description":"The documentation leaf can execute before the daemon and UI behavior is finalized, creating stale guidance and parallel-edit risk.","finding_id":"R5-F14","fix":"Add dependencies on every P3 deliverable whose final behavior 4.2 documents, including the daemon removal, reconciliation, dormant routes, and UI/config changes.","location":"P4 / § 4.2","prevention":"Map each documented behavior to its implementation section and include those sections in the docs leaf dependency closure.","principle":"Documentation must depend on the implementation deliverables that establish the behavior it describes.","root_cause":"Section 4.2 documents P3 dormancy, routes, and UI behavior while its manifest dependencies stop at P2.","section_id":"4.2","severity":"blocking"},{"category":"traceability","check_key":"active-codewiki-doc-inventory","description":"Several active guides will continue directing users to `gcode codewiki` after the command is removed.","finding_id":"R5-F15","fix":"Expand 4.2 Targets to the complete active-document inventory, update each invocation and ownership statement, and make the stale-reference test scan that inventory.","location":"P4 / § 4.2","prevention":"Inventory active documentation references to the retired command and reconcile every hit against Targets and explicit historical exclusions.","principle":"A command ownership migration must update every active document that teaches the retired invocation.","root_cause":"Section 4.2 omits active consumers including `crates/gcode/README.md` and the AI configuration, gcode, gcore, and gwiki user or developer guides.","section_id":"4.2","severity":"blocking"}],"resolution":"All 15 findings accepted by the user (interactive vote, 2026-08-08); repairs applied to the plan artifact. R5-F01: 1.1 now implements but never executes the gwiki mode (parse-only acceptance); execution, cleanliness assertions, and cross-engine comparison moved to 2.6.3. R5-F02: Constraints carry the canonical three-class normalization exception list (intentional diffs; unavoidable per-run stamps including the truth digest; lock-state exclusion) and 1.1/2.6 bind to it. R5-F03: _meta/codewiki.lock excluded from hashing, truth-digest fields normalized, and a same-engine back-to-back reproducibility gate added to the capture (1.1.5). R5-F04: the dispatch-level ensure_project_fresh wrapper and --no-freshness bypass are inventoried in 1.2, exposed as codewiki_facts::ensure_project_fresh (the facade's single documented non-query admission helper), and consumed by 2.3 with fresh/stale/bypassed tests (2.3.4). R5-F05: the 35-file semantically-changed set (crate-qualified references, repo sweep 2026-08-08) is enumerated as 2.1 destination Targets under an explicit byte-identical vs semantically-changed classification rule. R5-F06: Code gets a dedicated mode-aware admission arm — engine-owned per-output lock, read-only compare, no live-project-row requirement — pinned in the admission tests (2.3.3). R5-F07: lint's any-generated_by and code_*-prefix fallbacks and audit's code/** trust handling are retired/gated with one negative regression per signal (2.5.2). R5-F08: production librarian modules join 2.5 Targets; the outdated-codewiki aggregation/proposal flow is retired and code/** filtered before every mutation stage, with an end-to-end scheduled-run regression (2.5.3). R5-F09: 2.6 gains an explicit acceptance matrix — gwiki capture discipline, compare/purge/repair/scope/incremental modes, lock contention, and failure paths (2.6.3-2.6.6). R5-F10: tests/code_index/test_gcode_gateway.py added to 3.1 Targets; its codewiki assertions are removed and the retained gateway contract repinned. R5-F11: 3.1.1 replaced with three whole-word single-module scans plus a seeded positive control; retained inert config keys stay out of match range by construction. R5-F12: the reconciliation call is placed outside the code_indexer conditional with absent/disabled/failed startup tests (3.2.6). R5-F13: tests/ai/test_tool_chat_tools.py added to 3.5 Targets with an allow_mutation=True rejection assertion (3.5.1). R5-F14: 4.2 now depends on P2 and P3. R5-F15: 4.2 Targets carry the complete active-document inventory (five additional guides, crates/gcode/README.md, two comment-only code references) with explicit historical/no-touch exclusions, and the stale-reference scan covers that inventory (4.2.2).","reviewer_session":"#10314","round":5,"round_number":5,"verdict":"needs_review"},"session_id":"9a3e0d43-ad2e-49a1-bfec-067ec1802aa3"}
```

**Round 6** `kind: adversary`

- reviewer_run: `e16f4d64-eec6-4b52-b80f-3eb353badd7b`
- reviewer_session: `7079a808-0113-42bf-93c9-bf2bcc97b571` (#10322)
- evidence_id: `01e9d3b3-0145-4af3-8278-3abdc516ad53` (round_number 6)
- verdict: needs_review; findings_presented: 8 (all blocking)
- accepted: all 8 (R6-F01…R6-F08) — coordinator vote, unattended (user-raised
  cap 8, rounds 6–8 authorized 2026-08-08); every claim independently
  re-verified against the repository before voting, and all 8 verified
- declined: none
- resolution_notes: first unattended round. Fixer-induced share: 2 of 8 —
  R6-F05 (causal R5-F04: the freshness repair overgeneralized a
  generation-only precondition to every mode and dropped the quiet-gated
  busy warning) and R6-F08 (causal R5-F09: the failure matrix merged the
  harness's production-vault refusal with legacy CLI behavior). Independent
  defects still dominate (6 of 8), so the early-stop condition is not met
  and the loop continues. The six independent findings: post-repair
  artifact parity between plan bytes, task records, and coverage companions
  (R6-F01); tool-executor facade closure — the executor is itself a
  datastore consumer that would not compile against the planned facade
  after 1.3 privatization (R6-F02); deleted-test-consumer closure in the
  gcode crate (R6-F03); the managed-PostgreSQL privilege inventory keyed to
  a deleted source path (R6-F04); legacy non-system reserved-prefix cron
  rows escaping a system-only reconciler while remaining schedulable
  (R6-F06); and 3.7's separate-task escape clause conflicting with
  clean-path ownership (R6-F07). All repairs applied to the artifact (see
  the fence resolution for the per-finding mapping); the R6-F01 task-record
  and companion sync executes immediately after this round's finalization,
  before round 7 is prepared. Independent-defect trend across adversary
  rounds: 20 → 6 → 3 → 15 → 8.

```json plan-review-round
{"evidence_id":"01e9d3b3-0145-4af3-8278-3abdc516ad53","plan_hash":"bf9717dc59a7dbbc49471c338dabe671e48b0e7fe85829356e6e6102e9291226","round_number":6,"round_result":{"accepted":["R6-F01","R6-F02","R6-F03","R6-F04","R6-F05","R6-F06","R6-F07","R6-F08"],"coverage_attestation":{"adjacent_variant_complete":true,"attestation_digest":"b4fbc55e7cddeec6c339b89d1dc08b6d08754292a9b9e50f6b342f38116e63ee","cross_lane_interaction_complete":true,"disposition_counts":{"dismissed":6,"emitted_findings":8,"total":14},"evidence_id":"01e9d3b3-0145-4af3-8278-3abdc516ad53","lanes":[{"candidate_count":6,"lane_id":"requirements_traceability","status":"completed"},{"candidate_count":4,"lane_id":"repository_blast_radius","status":"completed"},{"candidate_count":4,"lane_id":"runtime_invariants","status":"completed"}],"shadow_manifest_status":{"entry_count":17,"manifest_digest":"e0f3eabb1ceaf0b3d10c26f11a99a580be7b11d1ca2802d7ac4bc1eba8a525a9","status":"valid"},"source_digest":"abfebf02aa3882eb18dd3e3909c30e97c8b0ede16566afaa0098dac48440aefe","version":1},"declined":[],"findings":[{"category":"traceability","check_key":"post-repair-artifact-parity","description":"The snapshot hash is bf9717dc59a7dbbc49471c338dabe671e48b0e7fe85829356e6e6102e9291226 with 56 acceptance items, while both coverage companions remain bound to a55e4791… with 50 items and omit 2.3.4, 2.6.3-2.6.6, and 3.2.6. Read-only task inspection confirms stale authoritative records: #19818 has only 2.3.1-2.3.3, #19821 only 2.6.1-2.6.2, #19823 only 3.2.1-3.2.5, and #19830 depends only on P2 instead of P2 plus P3.","finding_id":"R6-F01","fix":"Reapply the current 17-entry M1 manifest to #19814-#19830 and update root #19668, synchronizing authoritative descriptions, exact Targets, validation criteria, coverage labels, and dependencies. Regenerate .gobby/plans/codewiki-ownership-move.coverage-ledger.yaml and the root coverage manifest against bf9717dc59a7dbbc49471c338dabe671e48b0e7fe85829356e6e6102e9291226, then prove 56/56 acceptance coverage before build.","location":"P2 / § 2.3","prevention":"After every accepted repair that changes a deliverable, reapply the derived manifest to all mapped task records and regenerate both coverage companions before another review round.","principle":"Approved plan bytes, mapped task records, and coverage companions must describe the same requirements, acceptance set, Targets, and dependency closure before execution.","root_cause":"Round-5 repairs were applied to the plan and re-derived M1, while the existing task graph, root record, coverage ledger, and expansion coverage manifest remained on the prior plan revision.","section_id":"2.3","severity":"blocking"},{"category":"missing-requirement","check_key":"tool-executor-facade-closure","description":"The moved tool executor currently owns gcode Context, returns a raw postgres::Client, and calls private grep, search, visibility, graph, security, model, output, and database modules. Section 1.3 privatizes those modules, while the planned CodewikiFacts API does not provide the operations needed to compile the gwiki-owned executor.","finding_id":"R6-F02","fix":"Expand 1.2 and 2.1 with a per-tool dependency table. Add typed, owned CodewikiFacts query operations for database-backed search, outline, symbol, grep, visibility, security, and graph access; implement filesystem-only operations inside gwiki. Make the moved executor depend only on CodewikiFacts or one facade-owned typed adapter, expose no Context, Client, credentials, or datastore handles, and add compile-time public-API tests covering every tool operation. Propagate the resulting Targets and criteria to M1 and mapped tasks.","location":"P2 / § 2.1","prevention":"For every moved source file, enumerate imports, constructed types, returned types, and private-module calls; assign each dependency to a typed public facade operation or to a destination-owned implementation.","principle":"Every moved module must have an explicit, compilable dependency path across the new ownership and visibility boundary.","root_cause":"The plan classifies tool-loop operations by their runtime role without inventorying the concrete private gcode modules and raw datastore/context types imported by tool_executor.rs.","section_id":"2.1","severity":"blocking"},{"category":"traceability","check_key":"deleted-test-consumer-closure","description":"Deleting crates/gcode/src/commands/codewiki leaves crates/gcode/src/cli/tests.rs registering mod codewiki, crates/gcode/src/dispatch/tests.rs testing removed CodeWiki branches/helpers, and crates/gcode/src/config/tests.rs using include_str! on a deleted engine source. The gcode test target cannot compile after the planned deletion.","finding_id":"R6-F03","fix":"Add exact Targets for crates/gcode/src/cli/tests.rs, crates/gcode/src/dispatch/tests.rs, and crates/gcode/src/config/tests.rs. Remove the obsolete child-module registration and dispatch tests, relocate or repoint the AI-source include to the gwiki-owned engine, update self-crate paths after cli/dispatch become library modules, and include the gcode unit-test target in 2.4 validation and acceptance.","location":"P2 / § 2.4","prevention":"Before wholesale deletion, scan module declarations, include macros, self-crate paths, unit-test registries, integration tests, and configuration tests for every deleted path and moved symbol.","principle":"A source-tree deletion must update every compiled module registry, source include, and test consumer in the same atomic step.","root_cause":"The deletion inventory closes production modules while omitting compiled child-test registrations, dispatch-helper tests, and a compile-time include of a file being deleted.","section_id":"2.4","severity":"blocking"},{"category":"traceability","check_key":"managed-privilege-inventory-migration","description":"crates/gcode/security/managed_postgres_privileges.json records the two CodeWiki graph queries at crates/gcode/src/commands/codewiki/graph.rs, which 2.4 deletes. tests/code_index/test_gcode_privilege_manifest.py recomputes and compares the exact source inventory, so the plan deterministically leaves that contract failing.","finding_id":"R6-F04","fix":"Add crates/gcode/security/managed_postgres_privileges.json to the 1.2/2.4 Targets and regenerate its source entries after the final facade layout and old-tree deletion. Include tests/code_index/test_gcode_privilege_manifest.py in focused validation, targeting its scan roots or classifications too if they must change, and copy the criterion into M1 and the mapped tasks.","location":"P1 / § 1.2","prevention":"For every moved datastore query, inspect privilege manifests, generated registries, scan roots, and exact-inventory tests for source-path coupling.","principle":"Security inventories keyed by production source location must move atomically with the queries they govern.","root_cause":"The blast-radius inventory omitted the exact managed-PostgreSQL privilege registry and its equality test when graph queries move from commands/codewiki/graph.rs into codewiki_facts.","section_id":"1.2","severity":"blocking"},{"category":"unhandled-edge","causal_finding_id":"R5-F04","causal_section_ids":["1.2","2.3"],"check_key":"freshness-mode-scope","description":"Legacy dispatch returns from compare, purge, and citation-repair before ensure_project_fresh and runs freshness only for generation. Sections 1.2 and 2.3 instead call the helper before every mode, converting read-only compare and vault-only purge/repair into index-writing operations that can fail on freshness. The proposed helper also cannot preserve quiet-dependent warning behavior.","finding_id":"R6-F05","fix":"Place ensure_project_fresh only on the generation path after compare, purge, and repair branch out. Preserve FreshnessStatus and the runtime quiet/warning semantics through a typed argument or return value. Rewrite 2.3.4 to test fresh, stale, and --no-freshness generation plus negative tests proving compare, purge, and repair never invoke freshness or mutate the index; synchronize 1.2, M1, and tasks.","introduced_in_round":5,"location":"P2 / § 2.3","prevention":"Inventory all early-return modes before relocating a shared precondition; add positive and negative side-effect tests per mode, including diagnostic inputs and bypass flags.","principle":"A migrated wrapper must preserve legacy branch order, side-effect boundaries, and diagnostics independently for every CLI mode.","root_cause":"The round-5 freshness repair generalized a generation-only precondition to every Code mode and omitted the legacy Context.quiet input used by the busy warning.","section_id":"2.3","severity":"blocking"},{"category":"unhandled-edge","check_key":"legacy-cron-row-classification","description":"After 3.1 removes the registrar and handler, an enabled reserved-prefix CodeWiki row with is_system=false is invisible to 3.2 enumeration and residual reporting, yet remains eligible for scheduler claim and dispatch to the removed handler. Dormant status can therefore report success while executable legacy work remains.","finding_id":"R6-F06","fix":"Add a bounded reserved-CodeWiki-prefix query that includes system and non-system rows. Disable every enabled match and treat legacy non-system matches as migration-owned. Add regressions proving an is_system=false legacy row is disabled, cannot be scheduled, appears in residual/degraded reporting when reconciliation fails, and remains covered when code_indexer is disabled.","location":"P3 / § 3.2","prevention":"Build retirement matrices from historical registration states and compare the reconciler predicate with the scheduler's actual eligibility predicate.","principle":"A retirement reconciler must cover every historical row encoding that the live scheduler can still select.","root_cause":"The reconciler filters for the current is_system=true representation, while the retired registrar explicitly supports reserved CodeWiki rows created with is_system=false and the scheduler has no is_system selection filter.","section_id":"3.2","severity":"blocking"},{"category":"bad-sequencing","check_key":"validation-failure-ownership","description":"The separate-task escape clause conflicts with AGENTS.md's clean-path ownership and task-close checklist. It permits #19828 to close with its required characterization test failing and leaves the plan's frozen-digest acceptance unmet.","finding_id":"R6-F07","fix":"Remove the separate-task instruction. Require any failure of 3.7.1 to be fixed in the same leaf before closure, expanding Targets to the exact implicated dream read path when discovered, then rerun the characterization test cleanly and record that criterion in M1 and #19828.","location":"P3 / § 3.7","prevention":"Acceptance leaves that can expose production defects must authorize exact Target expansion and same-leaf repair before closure.","principle":"A validation failure in a clean or session-owned path remains part of the current leaf until repaired and rerun cleanly.","root_cause":"Section 3.7 explicitly instructs the implementer to file a separate task if its required characterization test exposes a production crash.","section_id":"3.7","severity":"blocking"},{"category":"traceability","causal_finding_id":"R5-F09","causal_section_ids":["2.6"],"check_key":"parity-harness-vs-cli-guard","description":"Constraints say direct gwiki code execution has no production-vault hard guard, and legacy gcode codewiki accepts an unrestricted optional --out. Acceptance 2.6.6 nevertheless requires a refused production-vault target to produce a legacy-equivalent error, contradicting both the governing policy and legacy behavior.","finding_id":"R6-F08","fix":"Restrict 2.6.6 legacy-equivalent failures to actual legacy cases such as missing or unindexed projects. Move production-vault refusal into a separately labeled harness-only safety criterion shared with 1.1.1/2.6.2, state that it adds no guard to direct gwiki code execution, and propagate the corrected criterion to M1 and #19821.","introduced_in_round":5,"location":"P2 / § 2.6","prevention":"For every parity failure case, label the execution locus and prove the legacy side implements the same product behavior before calling it legacy-equivalent.","principle":"Parity criteria must distinguish harness-only safety controls from product behavior and compare only behavior the legacy product actually exposes.","root_cause":"The round-5 failure matrix merged the parity harness's production-vault refusal with legacy-equivalent CLI failure behavior.","section_id":"2.6","severity":"blocking"}],"resolution":"All 8 findings accepted by the coordinator (unattended judging under the user-raised cap, 2026-08-08); every claim was independently re-verified against the repository before voting, and all 8 verified. Fixer-induced share: 2 of 8 (R6-F05 from R5-F04, R6-F08 from R5-F09) — independent defects still dominate, so the unattended loop continues. R6-F01: the manifest was re-derived and re-applied after this round's repairs (17 entries, digest 0db4d849ee6a72360ce0635b04d534e64e978b4a8275936db410e60cff49c953); immediately after this round's finalization and before any further round is prepared, the manifest is reapplied to task records #19814-#19830 with root #19668 updated, and both coverage companions are regenerated against the post-round-6 plan bytes with full acceptance coverage proven. R6-F02: 1.2 reclassifies tool-loop operations per-operation (the executor owns Context, returns postgres::Client at tool_executor.rs:88, and imports the private grep, code_graph, security, models, output, fts, db, and visibility modules); 2.1 adds the per-tool dependency table and new 2.1.8 pins the executor's facade-only compile surface. R6-F03: 2.4 Targets gain cli/tests.rs (drop the mod codewiki registration), dispatch/tests.rs (remove the codewiki routing and ai-options cases), and config/tests.rs (repoint the include_str! source sample), with new 2.4.4 pinning the compiling gcode test target. R6-F04: crates/gcode/security/managed_postgres_privileges.json joins 1.2 Targets (facade call sites recorded, new 1.2.4) and 2.4 Targets (regenerated without the deleted graph.rs entry, new 2.4.5), with the exact-inventory equality test in focused validation on both leaves. R6-F05 (fixer-induced): 1.2 and 2.3 bind freshness to the generation path only, after compare/purge/repair branch out (dispatch.rs:602-611); the facade helper returns FreshnessStatus and the caller owns the quiet-gated busy warning (dispatch.rs:74-77 semantics); 2.3.4 rewritten with negative never-invoke/never-mutate cases for the three non-generation modes. R6-F06: the reconciler enumerates system and legacy non-system reserved-prefix rows via a new bounded CronJobStorage.list_jobs_by_name_prefix (the retired registrar upgraded non-system rows only while it existed, codewiki_nightly.py:201-202, and get_due_jobs has no is_system filter, cron.py:802); 3.2.2 drops the non-system carve-out and new 3.2.7 pins the legacy-row regression including scheduler ineligibility and failure reporting. R6-F07: 3.7's separate-task escape clause replaced by same-leaf ownership — expand Targets to the implicated dream read path, repair, and rerun the characterization test cleanly before closure (3.7.1 updated). R6-F08 (fixer-induced): 2.6.6 restricted to failure paths the legacy CLI implements (missing/unindexed project); production-vault cleanliness labeled the harness-only safety assertion of 2.6.2, explicitly adding no guard to direct gwiki code execution, with the 2.6 prose and Constraints aligned.","reviewer_session":"7079a808-0113-42bf-93c9-bf2bcc97b571","round":6,"round_number":6,"verdict":"needs_review"},"session_id":"9a3e0d43-ad2e-49a1-bfec-067ec1802aa3"}
```

**Round 7** `kind: adversary`

- reviewer_run: `062e0098-a5a9-4656-a5e6-b6bf96492fc6`
- reviewer_session: `093b56b8-a2af-4ba4-9b8c-f02e6f935129` (#10325)
- evidence_id: `4f76fce8-bc69-4dbc-891b-a379ed639ef9` (round_number 7)
- verdict: needs_review; findings_presented: 11 (all blocking)
- accepted: all 11 (R7-F01…R7-F11) — coordinator vote, unattended (user-raised
  cap 8, rounds 6–8 authorized 2026-08-08); every claim independently
  re-verified against the repository before voting, and all 11 verified
- declined: none
- resolution_notes: second unattended round. Fixer-induced share: 6 of 11 —
  R7-F01 (causal R5-F04: the freshness helper joined the facade while 1.2.3
  kept an unqualified no-writes guarantee), R7-F03 (causal R5-F08: the
  generated-content protection covered the librarian while scheduled upkeep
  remained an unfiltered writer), R7-F07 (causal R6-F08: the rewritten
  failure matrix chose an unindexed project that legacy freshness
  auto-indexes rather than fails), R7-F08 (causal R3-F04: the diagnostic
  rename missed the separately registered tests/lock.rs assertion), R7-F10
  (causal R2-F01: the fixture repair named only a markdown file no symbol
  indexer detects), and R7-F11 (causal R2-F04: the privacy-test repair left
  an unresolved relocate-or-reroute choice with no destination Targets).
  That is a fixer-induced majority, which trips the user's early-stop
  criterion ("recommend a sooner stop if it looks like just your edits are
  causing the rejections"): after this round's finalization and the
  task-record/companion re-sync, the loop pauses for human review instead
  of launching round 8 (the cap). The five independent findings remain
  material — the ledger's misparsed 4.2 titles (R7-F02), the
  detached-timeout-worker connection-isolation contract (R7-F04), collapsed
  graph outcome states (R7-F05), the reinstall gate sequencing 4.1 ahead of
  P3 producer retirement (R7-F06), and gwiki's missing pulldown-cmark and
  wait-timeout direct dependencies (R7-F09, a compile blocker). All repairs
  applied to the artifact (see the fence resolution for the per-finding
  mapping); the manifest was re-derived and re-applied (17 entries, digest
  6d123e493f9e96356551aaea04a26240580d0c1b03d5169d8a1dc8a9e2f621ac).
  Finding-count trend across adversary rounds: 20 → 6 → 3 → 15 → 8 → 11;
  fixer-induced share trend: 2/8 → 6/11.

```json plan-review-round
{"evidence_id":"4f76fce8-bc69-4dbc-891b-a379ed639ef9","plan_hash":"198adae98016e6bb96041ee2f4aead85be23f4dcefc4882c6d6b4b8f2c72d133","round_number":7,"round_result":{"accepted":["R7-F01","R7-F02","R7-F03","R7-F04","R7-F05","R7-F06","R7-F07","R7-F08","R7-F09","R7-F10","R7-F11"],"coverage_attestation":{"adjacent_variant_complete":true,"attestation_digest":"49dd60c03490d632352dd4dcab93bcd33c797965ad2563e81edbc30d2961f084","cross_lane_interaction_complete":true,"disposition_counts":{"dismissed":0,"emitted_findings":11,"total":11},"evidence_id":"4f76fce8-bc69-4dbc-891b-a379ed639ef9","lanes":[{"candidate_count":3,"lane_id":"requirements_traceability","status":"completed"},{"candidate_count":4,"lane_id":"repository_blast_radius","status":"completed"},{"candidate_count":4,"lane_id":"runtime_invariants","status":"completed"}],"shadow_manifest_status":{"entry_count":17,"manifest_digest":"0db4d849ee6a72360ce0635b04d534e64e978b4a8275936db410e60cff49c953","status":"valid"},"source_digest":"b803afd101862a715ca2498c0d9004d8a4719f240b66391813eae70f5259a114","version":1},"declined":[],"findings":[{"category":"traceability","causal_finding_id":"R5-F04","causal_section_ids":["1.2","2.3"],"check_key":"facade-write-contract-consistency","description":"Acceptance 1.2.3 is internally impossible: ensure_project_fresh belongs to the facade and invokes api::index_files, which writes project index state.","finding_id":"R7-F01","fix":"Rewrite 1.2.3 so CodewikiFacts query methods and their connections are read-only, identify freshness as the sole indexing exception, and separately accept FreshnessStatus plus generation-only caller ownership of diagnostics.","introduced_in_round":5,"location":"P1 §1.2 and P2 §2.3","prevention":"Classify every facade operation as read-only or mutating and reconcile each classification with acceptance wording and caller ownership.","principle":"Acceptance criteria must scope side-effect guarantees to the exact API surface and name every mutation exception.","root_cause":"The R5 freshness-helper repair placed project index refresh inside the facade while 1.2.3 retained an unqualified no-writes guarantee.","section_id":"1.2","severity":"blocking"},{"category":"gobby-format","check_key":"coverage-ledger-title-scalar-parity","description":"The ledger parses 4.2 as title “Update contracts” plus unintended keys “docs” and “and skill guidance,” so it diverges from M1 and task #19830 despite byte-level parity work.","finding_id":"R7-F02","fix":"Quote the complete 4.2 title scalar, remove the unintended mapping keys, and add parsed-title parity validation against M1 and task #19830.","location":"Companion coverage ledger §4.2","prevention":"Parse every regenerated ledger and compare each title field structurally with the corresponding M1 entry and leaf task.","principle":"Companion coverage records must parse to the same canonical title and section identity as M1 and expanded task state.","root_cause":"The 4.2 title was emitted as an unquoted YAML scalar whose punctuation creates unintended null mapping keys.","section_id":"4.2","severity":"blocking"},{"category":"missing-requirement","causal_finding_id":"R5-F08","causal_section_ids":["2.5"],"check_key":"scheduled-upkeep-code-namespace-exclusion","description":"Scheduled gwiki upkeep still enumerates all pages and can archive or govern code/** content even though the plan requires generated CodeWiki pages to remain outside maintenance mutation.","finding_id":"R7-F03","fix":"Add upkeep production and test Targets to 2.5, exclude code/** before stale archiving, candidate governance, and every mutation stage, and add a daily scheduled-run regression.","introduced_in_round":5,"location":"P2 §2.5 and P3 §3.2","prevention":"Inventory every scheduled and interactive writer for protected paths, then test exclusions at each archive, governance, and rewrite stage.","principle":"Every maintenance writer that traverses a protected namespace must enforce the same ownership boundary before any mutation stage.","root_cause":"The prior generated-content protection covered librarian behavior while the daily gwiki upkeep pipeline remained an independent writer over all pages.","section_id":"2.5","severity":"blocking"},{"category":"unhandled-edge","check_key":"detached-worker-facade-connection-isolation","description":"ToolExecutor can leave a timed-out operation running in a detached worker; the plan lacks a contract preventing that worker from retaining a shared connection or mutex and blocking subsequent tool calls.","finding_id":"R7-F04","fix":"Specify a cheap-clone, thread-safe, context-only facade; give each tool call its own read connection; prohibit connection or mutex sharing with detached workers; test timeout of a first call followed by completion of a second.","location":"P1 §1.2 and P2 §2.1","prevention":"For each executor method, verify Send + Sync bounds, detached-worker lifetime, connection ownership, and post-timeout progress of a second call.","principle":"State used by timeout-detached workers must remain thread-safe and isolated from later calls for the full worker lifetime.","root_cause":"The facade design combines mutable methods and lazy connections with one runtime-owned handle without specifying per-call connection ownership.","section_id":"1.2","severity":"blocking"},{"category":"unhandled-edge","check_key":"graph-outcome-state-preservation","description":"The facade example cannot distinguish unavailable, truncated, successful-empty, and genuine-error graph results, although current bulk generation and tool execution treat those states differently.","finding_id":"R7-F05","fix":"Define typed graph-state outputs preserving available, truncated, unavailable, and error states; test unconfigured, unreachable, genuine error, exact-limit truncation, and successful-empty results across bulk and tool callers.","location":"P1 §1.2 and P2 §2.1","prevention":"Enumerate current outcome variants before designing facade return types and map each variant through bulk and tool consumers.","principle":"Boundary types must preserve every caller-visible outcome state that drives distinct behavior.","root_cause":"The proposed Result<Vec<EdgeFact>> facade shape collapses graph availability and truncation metadata into a bare collection.","section_id":"1.2","severity":"blocking"},{"category":"bad-sequencing","check_key":"binary-reinstall-after-producer-retirement","description":"Section 4.1 can reinstall gcode after CLI removal while P3 producers still invoke the removed command through the installed native binary.","finding_id":"R7-F06","fix":"Make 4.1 depend on every P2 and P3 leaf, or add an equivalent explicit gate that retires all producers and confirms daemon dormancy before reinstall; synchronize M1 and task #19829.","location":"P4 §4.1 with P2 §2.4 and P3 §3.1–§3.7","prevention":"Build release-gate dependencies from the complete producer inventory and verify process dormancy before installing a command-removal build.","principle":"A release gate that removes a command must wait until every producer of that command has been retired before rebuilding and reinstalling the binary.","root_cause":"Section 4.1 depends only on P2 leaves, while P3 refresh, trigger, nightly, and gateway paths remain command producers.","section_id":"4.1","severity":"blocking"},{"category":"weak-testability","causal_finding_id":"R6-F08","causal_section_ids":["2.6"],"check_key":"legacy-failure-path-reachability","description":"An unindexed project does not reach the claimed legacy failure: missing last-indexed state triggers api::index_files, so acceptance 2.6.6 cannot exercise the stated path.","finding_id":"R7-F07","fix":"Use a concrete reachable legacy failure such as an invalid compare ref, or define a no-freshness invocation and exact unavailable-index precondition; capture exit status, stderr, and mutation checks before deletion.","introduced_in_round":6,"location":"P2 §2.6 acceptance 2.6.6","prevention":"Trace each proposed negative fixture from dispatch through freshness and record the actual terminal branch before accepting it as a characterization case.","principle":"Characterization acceptance must name a reachable precondition and the exact branch whose behavior it freezes.","root_cause":"The R6 repair selected an unindexed project as a failure fixture without tracing the generation freshness transition.","section_id":"2.6","severity":"blocking"},{"category":"gobby-format","causal_finding_id":"R3-F04","causal_section_ids":["2.1"],"check_key":"moved-lock-test-target-closure","description":"The moved tests/lock.rs still pins the legacy gcode codewiki contention string and is absent from 2.1 Targets, so the planned diagnostic change leaves a failing moved test outside leaf ownership.","finding_id":"R7-F08","fix":"Add crates/gwiki/src/commands/code/tests/lock.rs to 2.1 Targets, require its contention assertion to use gwiki code, and propagate the Target and criterion through M1 and task state.","introduced_in_round":3,"location":"P2 §2.1 Targets and lock test module","prevention":"For each changed diagnostic, sweep all literal assertions and ensure every moved destination is represented in Targets and M1 criteria.","principle":"Every semantically changed moved file must appear as an exact Target with acceptance that covers its changed behavior.","root_cause":"The R3 diagnostic repair updated production and inline assertions while omitting the separately registered moved lock test.","section_id":"2.1","severity":"blocking"},{"category":"missing-requirement","check_key":"moved-crate-direct-dependency-inventory","description":"gwiki declares neither pulldown-cmark nor wait-timeout, so the wholesale moved tree cannot compile using gcode's transitive direct dependencies.","finding_id":"R7-F09","fix":"Add pulldown-cmark = \"0.13\" and wait-timeout = \"0.2\" to gwiki direct dependencies, list both in 2.1's inventory, and require a locked build after lockfile regeneration.","location":"P2 §2.1 dependency move","prevention":"Enumerate all non-std external imports in the moved tree and compare them with the destination crate's direct dependency table before accepting the move.","principle":"A Rust module move must inventory every direct external crate used by the destination production tree.","root_cause":"The dependency inventory omitted pulldown_cmark and wait_timeout imports present in the moved production sources.","section_id":"2.1","severity":"blocking"},{"category":"gobby-format","causal_finding_id":"R2-F01","causal_section_ids":["1.1"],"check_key":"fixture-source-target-closure","description":"Markdown is not a detected gcode language, so the sole named fixture file cannot supply the indexed symbols and code topology required by 1.1 and consumed by 2.6.","finding_id":"R7-F10","fix":"Define the minimal immutable fixture, add every committed file such as project/Cargo.toml and project/src/lib.rs to 1.1 Targets, and pin the exact source-digest file set in acceptance and M1.","introduced_in_round":2,"location":"P1 §1.1 fixture Targets","prevention":"Expand fixture acceptance into an exact committed file manifest and confirm at least one named source is indexable by the current language registry.","principle":"Every committed fixture input that supplies accepted behavior must be file-qualified in Targets and included in the pinned digest set.","root_cause":"The R2 fixture repair named only project/README.md while acceptance requires indexed source symbols and workspace topology.","section_id":"1.1","severity":"blocking"},{"category":"gobby-format","causal_finding_id":"R2-F04","causal_section_ids":["1.3"],"check_key":"privacy-test-destination-resolution","description":"Graph, projection-stale, and vector tests have source Targets only; the implementing leaf cannot determine where their private implementation access will move or which public surface to expose.","finding_id":"R7-F11","fix":"Resolve the migration to exact in-crate test destination files for all three cases, add them to 1.3 Targets, and pin preservation of test names and behavior without expanding the enduring public test_env API.","introduced_in_round":2,"location":"P1 §1.3 integration-test privacy migration","prevention":"For each external private-API consumer, choose one destination, name its exact Target, and preserve the original test identity and behavior.","principle":"A self-contained privacy refactor must resolve every external integration-test consumer to one concrete compiling destination before Targets are finalized.","root_cause":"The R2 repair retained an unresolved choice between in-crate relocation and public test-surface rerouting, leaving all possible destinations untargeted.","section_id":"1.3","severity":"blocking"}],"resolution":"All 11 findings accepted by the coordinator (unattended judging under the user-raised cap, 2026-08-08); every claim was independently re-verified against the repository before voting, and all 11 verified. Fixer-induced share: 6 of 11 (R7-F01 from R5-F04, R7-F03 from R5-F08, R7-F07 from R6-F08, R7-F08 from R3-F04, R7-F10 from R2-F01, R7-F11 from R2-F04) — a fixer-induced majority, which trips the user's early-stop criterion: after this round is finalized and companions are re-synced, the loop pauses for human review instead of launching round 8. R7-F01: 1.2.3 rewritten — the read-only guarantee is scoped to CodewikiFacts query methods with per-call read-only connections, and ensure_project_fresh is named the sole indexing exception (internal indexing paths only, owned FreshnessStatus, no diagnostics, caller-owned busy warning). R7-F02: the ledger's 4.2 expected_leaves titles are restored to the full quoted scalar with the unintended docs/and-skill-guidance keys removed, a full-ledger structural scan checks every expected-leaf mapping for misparse anomalies, and parsed-title parity with M1 and #19830 is verified during companion regeneration. R7-F03: 2.5 gains upkeep.rs and upkeep/tests.rs Targets, a classifier bullet documenting archive_long_stale_pages' unfiltered enumeration (upkeep.rs:191-231) and the scheduled gateway cron (src/gobby/wiki/scheduled_jobs.py), and new 2.5.4 pinning code/** exclusion before every upkeep mutation stage with a scheduled-run regression. R7-F04: 1.2's boundary rules make CodewikiFacts a cheap-clone Send + Sync context-only handle with per-call owned read-only connections and no stored connection state (facade shape updated to &self methods); 2.1's executor paragraph preserves the tool_executor.rs:65 invariant, and new 2.1.9 pins the timeout-then-second-call regression. R7-F05: the facade graph family returns typed outcomes (GraphAvailability plus GraphEdges with truncation) preserving available/truncated/successful-empty/unavailable/error; new 1.2.5 requires one test per state, and new 2.1.10 pins that bulk generation and the tool loop preserve graph-truncated/graph-unavailable page degradation. R7-F06: 4.1 now depends on P2 and P3 (heading, reinstall-gate prose, and M1 depends_on gaining 3.1-3.7); task #19829 gains the P3 leaf dependencies. R7-F07 (fixer-induced): 2.6.6 rewritten to reachable legacy failures — an invalid --compare-to ref (compare.rs:164/203/210) and --complete-scope without --scope (run.rs:46) — and documents that legacy freshness auto-indexes never-indexed projects (freshness.rs::project_needs_refresh), so unindexed-project is explicitly not a fixture. R7-F08 (fixer-induced): crates/gwiki/src/commands/code/tests/lock.rs joins the 2.1 changed set and Targets; the identity-rename prose names both contention assertions (lock.rs:168 and tests/lock.rs:15). R7-F09: pulldown-cmark = \"0.13\" and wait-timeout = \"0.2\" are added to 2.1's dependency inventory for gwiki's [dependencies] (imports at text/sanitize.rs:3 and ownership/analysis.rs:9), with the locked build after lockfile regeneration proving the table complete. R7-F10 (fixer-induced): the fixture is defined as a minimal Rust crate — project/Cargo.toml and project/src/lib.rs join 1.1 Targets, markdown is pinned as content-only (languages.rs::markdown_extensions_are_not_detected), the input-identity digest covers exactly the enumerated file set, and new 1.1.6 proves the baseline derives from a non-empty page set. R7-F11 (fixer-induced): all three external-test migrations resolve to pinned in-crate destinations — graph_standalone db cases with the support helper to src/graph/tests/standalone_db.rs, projection_stale's models-constant case (projection_stale.rs:26) to src/projection/tests/stale.rs, and vector_projection's internals cases to src/vector/tests/projection.rs — with test names and behavior preserved, test_env unexpanded, and 1.3.1 extended. The manifest was re-derived and re-applied after this round's repairs (17 entries, digest 6d123e493f9e96356551aaea04a26240580d0c1b03d5169d8a1dc8a9e2f621ac); task records #19814-#19817, #19820, #19821, and #19829 are re-synced and both coverage companions regenerated against the post-round-7 plan bytes immediately after finalization, before any further round could be prepared.","reviewer_session":"093b56b8-a2af-4ba4-9b8c-f02e6f935129","round":7,"round_number":7,"verdict":"needs_review"},"session_id":"9a3e0d43-ad2e-49a1-bfec-067ec1802aa3"}
```

**Round 8** `kind: adversary`

- reviewer_run: `d8cfce75-b99d-417b-876e-70ce9fd53e55`
- reviewer_session: `e7bc8b1e-7a61-47b9-802c-fbf7d5d0d7e3`
- evidence_id: `b7509ccc-f7ef-40fa-b49c-e2a47c9a8864` (round_number 8)
- verdict: needs_review; findings_presented: 4 (all blocking)
- accepted: all 4 (R8-F01…R8-F04) — coordinator vote, unattended (user-raised
  cap 8, rounds 6–8 authorized 2026-08-08); every claim independently
  re-verified against the repository before voting, and all 4 verified
- declined: none
- resolution_notes: final capped round. Fixer-induced share: 4 of 4 —
  R8-F01 (causal R7-F05: the round-7 graph-state repair pinned
  `graph-truncated`/`graph-unavailable` page degradation that
  commands/codewiki/mod.rs:28 explicitly retired; bulk generation treats
  graph availability as informational only and the tool loop records only
  `graph-unavailable` as evidence degradation), R8-F02 (causal R5-F08: the
  namespace-exclusion regressions lacked a `knowledge/**` positive control,
  so a wholesale disable would pass), R8-F03 (causal R5-F08: 3.2.3's
  scheduled-maintenance audit depends on 2.5's exclusions but 3.2 declared
  only 3.1), and R8-F04 (causal R5-F05: the relocation Target inventory
  omitted byte-identical copies in 2.1 and proxied 2.4's 108-file deletion
  behind two entries). All repairs applied to the artifact (see the fence
  resolution for the per-finding mapping); the manifest was re-derived and
  re-applied (17 entries, digest
  f73f1034d7d4124f225e8ab6bbfabe22ca60d16ff7ab03f34df680daccc5bd05; 3.2 now
  depends on 3.1 and 2.5). Finding-count trend across adversary rounds:
  20 → 6 → 3 → 15 → 8 → 11 → 4; fixer-induced share trend:
  2/8 → 6/11 → 4/4. The review cap of 8 is reached with a needs_review
  verdict: no further adversary rounds; continuation requires explicit
  human handoff (see the handoff entry below).

```json plan-review-round
{"evidence_id":"b7509ccc-f7ef-40fa-b49c-e2a47c9a8864","plan_hash":"9fddb00216e51b45ab5f45ec001f04e2f86c064dacf921e165f03920c2304861","round_number":8,"round_result":{"accepted":["R8-F01","R8-F02","R8-F03","R8-F04"],"coverage_attestation":{"adjacent_variant_complete":true,"attestation_digest":"c5a5ef7d2e39153c53105708f1df9aa31c35dea77df2996f13ca7797e5611c32","cross_lane_interaction_complete":true,"disposition_counts":{"dismissed":1,"emitted_findings":4,"total":5},"evidence_id":"b7509ccc-f7ef-40fa-b49c-e2a47c9a8864","lanes":[{"candidate_count":2,"lane_id":"requirements_traceability","status":"completed"},{"candidate_count":1,"lane_id":"repository_blast_radius","status":"completed"},{"candidate_count":2,"lane_id":"runtime_invariants","status":"completed"}],"shadow_manifest_status":{"entry_count":17,"manifest_digest":"6d123e493f9e96356551aaea04a26240580d0c1b03d5169d8a1dc8a9e2f621ac","status":"valid"},"source_digest":"4179dce81101933e25dea3ad5da8f47985722e86fc91991137e91c9be20bade0","version":1},"declined":[],"findings":[{"category":"traceability","causal_finding_id":"R7-F05","causal_section_ids":["1.2","2.1"],"check_key":"graph-outcome-rendering-parity","description":"Acceptance 2.1.10 would add `graph-truncated`/`graph-unavailable` page degradation during a byte-parity move, although current bulk generation treats graph availability as informational and the direct tool executor has no `graph-truncated` outcome. Section 1.2.5 also omits the ordinary available, non-empty state from its claimed one-test-per-state matrix.","finding_id":"R8-F01","fix":"Keep the typed facade outcomes, rewrite 1.2 and 2.1.10 to preserve the actual legacy mappings, add an explicit available/non-empty/non-truncated facade test, retain direct `graph-unavailable` only for invoked unconfigured or unreachable graph tools, keep genuine failures as tool errors, and defer any new truncation-degradation policy to #17678.","introduced_in_round":7,"location":"P1 / § 1.2 and P2 / § 2.1","prevention":"Trace every typed outcome through each current bulk and tool consumer to its exact rendered marker before writing parity acceptance.","principle":"A behavior-preserving relocation must preserve the exact legacy mapping from dependency outcomes to caller-visible output.","root_cause":"The round-7 graph-state repair inferred page-degradation markers that the current bulk engine explicitly retired; the direct tool loop records only graph-unavailable.","section_id":"2.1","severity":"blocking"},{"category":"weak-testability","causal_finding_id":"R5-F08","causal_section_ids":["2.5"],"check_key":"maintenance-namespace-positive-control","description":"Acceptance 2.5.3 and 2.5.4 can pass if scheduled librarian or upkeep work is disabled wholesale, contradicting the governing requirement that shared `knowledge/**` maintenance keeps running.","finding_id":"R8-F02","fix":"Extend both scheduled-run regressions with an eligible `knowledge/**` control page and assert its existing curation or archival behavior occurs while the paired `code/**` page remains byte-identical; resynchronize M1, task #19820, and both coverage companions.","introduced_in_round":5,"location":"P2 / § 2.5","prevention":"For every protected-namespace negative assertion, include an eligible retained-namespace fixture and assert its existing mutation path still runs.","principle":"A namespace-exclusion regression must pair the excluded fixture with a positive control proving retained scope still executes.","root_cause":"The maintenance repairs assert only that `code/**` stays untouched, so disabling librarian or upkeep wholesale satisfies the acceptance criteria.","section_id":"2.5","severity":"blocking"},{"category":"bad-sequencing","causal_finding_id":"R5-F08","causal_section_ids":["2.5"],"check_key":"scheduled-maintenance-dependency-closure","description":"Section 3.2 can run before 2.5 even though acceptance 3.2.3 requires librarian and upkeep to exclude `code/**`; the leaf is therefore unable to close at its declared dependency position.","finding_id":"R8-F03","fix":"Make 3.2 depend on both 3.1 and 2.5, then regenerate M1 and synchronize task #19823 plus both coverage companions with the new cross-phase dependency.","introduced_in_round":5,"location":"P2 / § 2.5 and P3 / § 3.2","prevention":"Map each cross-section acceptance claim to the deliverable that establishes it and add that deliverable to the leaf dependency closure.","principle":"Every leaf acceptance criterion must already be satisfiable after that leaf's declared dependencies.","root_cause":"Section 3.2.3 claims the repository-wide scheduled-maintenance exclusion owned by 2.5, while 3.2 depends only on 3.1.","section_id":"3.2","severity":"blocking"},{"category":"gobby-format","causal_finding_id":"R5-F05","causal_section_ids":["2.1"],"check_key":"relocation-target-inventory-closure","description":"The source engine contains 108 files. Section 2.1 creates the wholesale destination tree while explicitly omitting byte-identical copies from Targets, and 2.4 deletes the whole source tree while targeting only `mod.rs` and `diagram_compose.rs`; this violates the canonical file-level Target Inventory contract.","finding_id":"R8-F04","fix":"Enumerate every destination file created by 2.1, including copy-identical files and the one-to-four composer mapping, and every source file deleted by 2.4. Use bare paths for genuinely new files and exact or justified `::*` entries for existing symbol-bearing files, then resynchronize M1, the affected tasks, and both coverage companions.","introduced_in_round":5,"location":"P2 / §§ 2.1 and 2.4","prevention":"Diff the exact pre/post file inventory for every relocation and require the created and deleted path sets to equal the corresponding Target sets.","principle":"Every created or deleted file requires its own Target; a file-level wildcard never stands for a directory tree.","root_cause":"The round-5 relocation repair treated byte-identical copies as exempt from Targets and the deletion leaf uses two file targets as a proxy for the complete source tree.","section_id":"2.1","severity":"blocking"}],"resolution":"Final capped round (8 of 8), unattended per the user's standing authorization; every claim independently re-verified against the repository before voting, all four verified and accepted, all four repairs applied. R8-F01: crates/gcode/src/commands/codewiki/mod.rs:28 states the auto-generated mermaid diagrams were the sole source of graph-truncated/graph-unavailable page degradation and that graph availability is now informational only; rendered-page tests assert the markers' absence (tests/architecture.rs:83,315; tests/graph.rs:431-432,462-463,499,541), truncation surfaces only as the deterministic diagrams' 'source graph was truncated' note (render/diagrams.rs:517), and the tool loop records only graph-unavailable as evidence degradation (text/generation/outcome.rs:32, tool_executor.rs:11) without a graph-truncated outcome — repaired the §1.2 boundary paragraph to state the actual legacy mapping, added the available/non-empty test to 1.2.5, rewrote the §2.1 executor paragraph and 2.1.10 (test renamed graph_states_drive_degradation → graph_outcomes_match_legacy_mapping), and deferred any richer truncation policy to #17678. R8-F02: 2.5.3/2.5.4 as written passed under a wholesale librarian/upkeep disable while the plan's own constraint keeps knowledge upkeep running — added an eligible knowledge/** positive-control page to both scheduled-run regressions (control is curated/archived in the same run; code/** stays byte-identical) plus a body paragraph making the positive-control requirement explicit. R8-F03: 3.2.3's audit is only satisfiable after 2.5's librarian/upkeep exclusions land — heading now (depends: 3.1, 2.5) with body rationale; M1 3.2 entry carries depends_on [3.1, 2.5]; task #19823 gains the dependency on #19820. R8-F04: the source tree holds exactly 108 files (verified by full enumeration); §2.1's Targets now enumerate the complete 112-file destination inventory (107 one-to-one counterparts, four decomposed composer files, the new runtime.rs carrier) with byte-identical copies as bare new-file paths, and §2.4's Targets enumerate all 108 deleted source files (symbol-bearing as ::* with scope-reason, the five zero-symbol files — build.rs, prompts.rs, render.rs, prompts/systems.rs, text/generation.rs — as bare paths); the authorizing prose ('byte-identical files are deliberately not restated') was replaced with the complete-inventory statement. Manifest re-derived and re-applied: 17 entries, digest f73f1034d7d4124f225e8ab6bbfabe22ca60d16ff7ab03f34df680daccc5bd05. Fixer-induced share this round: 4/4 (R8-F01 causal R7-F05; R8-F02/F03 causal R5-F08; R8-F04 causal R5-F05) — no independent findings remain, consistent with the early-stop signal raised after round 7. The configured review cap of 8 is reached with a needs_review verdict: no further adversary rounds will be launched; continuation requires explicit human handoff.","reviewer_session":"e7bc8b1e-7a61-47b9-802c-fbf7d5d0d7e3","round":8,"round_number":8,"verdict":"needs_review"},"session_id":"9a3e0d43-ad2e-49a1-bfec-067ec1802aa3"}
```

**Review Cap Reached — Human Handoff** `kind: verification`

- The configured adversarial-review cap of 8 rounds is exhausted with a
  `needs_review` verdict on round 8 (evidence
  `b7509ccc-f7ef-40fa-b49c-e2a47c9a8864`, finalized as a normal rejection).
  All four round-8 findings were accepted and repaired in the artifact; the
  M1 manifest was re-derived and re-applied (17 entries, digest
  `f73f1034d7d4124f225e8ab6bbfabe22ca60d16ff7ab03f34df680daccc5bd05`).
- No further adversary rounds may be launched for this plan. Continuation is
  human-only: `continue interactively` (plan refinement without automated
  review), or explicit `hand off to build` via the coordinator-only handoff
  tools (`derive_plan_handoff_manifest` → `apply_plan_handoff_manifest` →
  `uv run gobby plans validate --mode expansion` →
  `uv run gobby build <plan-file>`), or `stop`.
- Reviewer context for the human: every round-8 finding was classified
  fixer-induced (4/4; causal rounds 5 and 7) — the fixer-induced share trend
  across rounds 6→7→8 was 2/8 → 6/11 → 4/4, and an early stop was
  recommended after round 7 on exactly this signal. The repairs in rounds
  5–8 are all applied and both validation modes pass; the residual risk is
  churn from repair-on-repair cycles, not an unrepaired defect inventory.

## Task Mapping
`kind: framing`

<!-- Updated after task creation -->
| Plan Item | Task Ref | Status |
|-----------|----------|--------|
| P1 | #19810 | open |
| 1.1 | #19814 | open |
| 1.2 | #19815 | open |
| 1.3 | #19816 | open |
| P2 | #19811 | open |
| 2.1 | #19817 | open |
| 2.3 | #19818 | open |
| 2.4 | #19819 | open |
| 2.5 | #19820 | open |
| 2.6 | #19821 | open |
| P3 | #19812 | open |
| 3.1 | #19822 | open |
| 3.2 | #19823 | open |
| 3.3 | #19824 | open |
| 3.4 | #19825 | open |
| 3.5 | #19826 | open |
| 3.6 | #19827 | open |
| 3.7 | #19828 | open |
| P4 | #19813 | open |
| 4.1 | #19829 | open |
| 4.2 | #19830 | open |
| D1 | #18902 | deferred |
| D2 | #19664 | deferred |
| D3 | #19665 | deferred |
| D4 | #18779 | deferred |
| D5 | #17678 | deferred |

## M1 Task Manifest
`kind: manifest`

```yaml
- title: Capture the deterministic parity baseline
  category: test
  task_type: feature
  depends_on: []
  validation_criteria: "1.1.1: Baseline script produces a sorted normalized hash manifest\
    \ from an isolated temp vault and refuses to run against the configured production\
    \ vault path. file: `scripts/codewiki_parity_baseline.sh`.\n1.1.2: Committed baseline\
    \ manifest exists with one entry per generated file and documented normalization\
    \ rules. file: `crates/gwiki/tests/fixtures/codewiki_parity/README.md`.\n1.1.3:\
    \ The script accepts exactly the two enumerated engine modes (`--engine gcode`,\
    \ `--engine gwiki`) and both share the one normalization/manifest path; no second\
    \ normalization implementation exists. file: `scripts/codewiki_parity_baseline.sh`.\n\
    1.1.4: The `--engine gcode` capture runs against the committed fixture project\
    \ with an isolated index identity, executes only a freshly built `--locked` workspace\
    \ binary, asserts the pinned fixture source digest before generating, and records\
    \ revision/version capture metadata outside the hashed manifest and outside `project/`;\
    \ the `--engine gwiki` branch is validated for argument parsing only (2.6 owns\
    \ its execution). file: `crates/gwiki/tests/fixtures/codewiki_parity/README.md`.\n\
    1.1.5: After the `--engine gcode` capture, every byte under `crates/gwiki/tests/fixtures/codewiki_parity/project/`\
    \ is unchanged and that subtree clean, and no tracked file beyond the outer README\
    \ and `baseline.sha256` is written. Two back-to-back `--engine gcode` runs produce\
    \ identical normalized manifests (`_meta/codewiki.lock` excluded, truth-digest\
    \ and commit/timestamp stamps normalized per the Constraints exception list) \u2014\
    \ the capture refuses to commit a baseline otherwise. file: `scripts/codewiki_parity_baseline.sh`.\n\
    1.1.6: The committed fixture project contains the enumerated symbol-indexable\
    \ Rust sources (`project/Cargo.toml`, `project/src/lib.rs`), the input-identity\
    \ digest covers exactly the committed fixture file set, and the captured baseline\
    \ manifest derives from a non-empty generated page set with per-file source hashes\
    \ \u2014 proving the fixture actually yields indexed symbols and topology. file:\
    \ `crates/gwiki/tests/fixtures/codewiki_parity/project/src/lib.rs`."
  labels:
  - covers:codewiki-ownership-move:1.1:1.1.1
  - covers:codewiki-ownership-move:1.1:1.1.2
  - covers:codewiki-ownership-move:1.1:1.1.3
  - covers:codewiki-ownership-move:1.1:1.1.4
  - covers:codewiki-ownership-move:1.1:1.1.5
  - covers:codewiki-ownership-move:1.1:1.1.6
  tdd: false
  source_section: '1.1'
  assigned_agent: backend-developer
- title: Implement the `codewiki_facts` facade in gobby-code
  category: code
  task_type: feature
  depends_on: []
  validation_criteria: "1.2.1: Facade module exists with typed owned fact structs\
    \ and lazy read-only access; no public signature exposes connections, `Context`,\
    \ or credentials. file: `crates/gcode/src/codewiki_facts/mod.rs`.\n1.2.2: Every\
    \ external dependency found by the full `crate::` inventory is classified (facade\
    \ family, runtime carrier, or explicit AI-routing carry-over); every facade-bucket\
    \ family has a typed method; and the inventory with its classification is recorded\
    \ in the module header docs. file: `crates/gcode/src/codewiki_facts/mod.rs`.\n\
    1.2.3: `CodewikiFacts` query methods perform no datastore writes: every query\
    \ family uses a per-call read-only connection, and a test proves both the read-only\
    \ connection use and that no connection state is stored on the handle. The module-level\
    \ `ensure_project_fresh` admission helper is the sole indexing exception \u2014\
    \ it refreshes the project index only through gcode's existing internal indexing\
    \ paths, returns an owned `FreshnessStatus`, and emits no diagnostics (the busy\
    \ warning is caller-owned, generation-path only). symbol: `CodewikiFacts`.\n1.2.4:\
    \ The managed-PostgreSQL privilege inventory records the facade's query call sites\
    \ and the exact-inventory equality test passes against the updated registry. test:\
    \ `tests/code_index/test_gcode_privilege_manifest.py`.\n1.2.5: The graph facade\
    \ family exposes typed outcome states \u2014 available, truncated, successful-empty,\
    \ unavailable (with reason), and genuine error \u2014 with one test per state:\
    \ available non-empty (the ordinary path), unconfigured, unreachable, genuine\
    \ error, exact-limit truncation, and successful-empty. test: `crates/gcode/src/codewiki_facts/tests.rs`."
  labels:
  - covers:codewiki-ownership-move:1.2:1.2.1
  - covers:codewiki-ownership-move:1.2:1.2.2
  - covers:codewiki-ownership-move:1.2:1.2.3
  - covers:codewiki-ownership-move:1.2:1.2.4
  - covers:codewiki-ownership-move:1.2:1.2.5
  tdd: true
  source_section: '1.2'
  implementation_domain: backend
- title: Privatize gcode's datastore modules and pin the boundary
  category: code
  task_type: feature
  depends_on:
  - '1.2'
  validation_criteria: '1.3.1: Datastore modules are no longer `pub`; the binary is
    a thin caller of the public entrypoint; the crate, binary, and integration tests
    all compile with unchanged behavior, and the relocated cases keep their original
    test names and assertions in their pinned in-crate destinations. file: `crates/gcode/src/lib.rs`.

    1.3.2: Boundary test pins the public API set and fails on new leaks. test: `crates/gcode/tests/facade_boundary.rs::public_surface_is_pinned`.'
  labels:
  - covers:codewiki-ownership-move:1.3:1.3.1
  - covers:codewiki-ownership-move:1.3:1.3.2
  tdd: true
  source_section: '1.3'
  implementation_domain: backend
- title: Move the codewiki engine under gobby-wiki behind the facade and decompose
    the diagram composer
  category: code
  task_type: feature
  depends_on:
  - '1.1'
  - '1.2'
  - '1.3'
  validation_criteria: "2.1.1: Every file of the source tree has a counterpart under\
    \ `crates/gwiki/src/commands/code/` with no file left behind or dropped, the workspace\
    \ builds with `cargo build --locked`, and the still-present gcode tree and its\
    \ consumers compile unchanged. file: `crates/gwiki/src/commands/code/mod.rs`.\n\
    2.1.2: The moved engine contains zero direct datastore imports \u2014 no `gobby_code::db`,\
    \ no `postgres::`, no `Context` construction \u2014 verified by a source-scan\
    \ test in the gwiki crate. test: `crates/gwiki/tests/code_engine_boundary.rs::moved_engine_uses_only_facade`.\n\
    2.1.3: gwiki depends on gobby-code one-way at the current version; gcode has no\
    \ gwiki dependency. test: `crates/gwiki/src/api.rs::dependency_direction_is_one_way`.\n\
    2.1.4: The composer exists as a decomposed module with all files below 1,000 lines\
    \ and unchanged public API; no over-ceiling production file exists at any point\
    \ after this leaf. file: `crates/gwiki/src/commands/code/diagram_compose/mod.rs`.\n\
    2.1.5: Existing diagram-composition tests pass unchanged after the split. behavior:\
    \ \"diagram composition parity\" in `crates/gwiki/src/commands/code/diagram_compose/mod.rs`.\n\
    2.1.6: The runtime carrier holds all non-datastore context (project identity,\
    \ output settings, AI routing) and the boundary test covers AI-off/direct/daemon/tool-loop\
    \ plus compare, purge, and repair modes. test: `crates/gwiki/tests/code_engine_boundary.rs::runtime_carries_non_datastore_context`.\n\
    2.1.7: The daemon agentic caller is `gwiki.code` and lock-contention diagnostics\
    \ name `gwiki code`, while `ToolPolicy.cli` remains `gcode`; all three are pinned.\
    \ test: `crates/gwiki/tests/code_engine_boundary.rs::ownership_identities_moved`.\n\
    2.1.8: The moved tool executor compiles against the facade alone: the per-tool\
    \ dependency table is complete, and the boundary test proves no `Context` ownership,\
    \ no `postgres::Client` in any signature, and no private gcode module import for\
    \ every tool operation. test: `crates/gwiki/tests/code_engine_boundary.rs::tool_executor_uses_only_facade`.\n\
    2.1.9: A tool operation that exceeds its timeout detaches with its own per-call\
    \ connection, and a subsequent tool call completes while the detached worker is\
    \ still running; no connection or mutex is shared with detached workers. test:\
    \ `crates/gwiki/tests/code_engine_boundary.rs::tool_timeout_does_not_block_subsequent_calls`.\n\
    2.1.10: Graph outcomes map exactly as before the move: an unavailable graph tool\
    \ records the explicit `graph-unavailable` evidence-degradation result without\
    \ marking the page degraded, no tool operation emits a `graph-truncated` outcome,\
    \ bulk generation treats graph availability as informational only (no page-degradation\
    \ marker is emitted), truncation surfaces solely as the deterministic diagrams'\
    \ \"source graph was truncated\" note, and genuine failures surface as tool errors.\
    \ test: `crates/gwiki/tests/code_engine_boundary.rs::graph_outcomes_match_legacy_mapping`."
  labels:
  - covers:codewiki-ownership-move:2.1:2.1.1
  - covers:codewiki-ownership-move:2.1:2.1.2
  - covers:codewiki-ownership-move:2.1:2.1.3
  - covers:codewiki-ownership-move:2.1:2.1.4
  - covers:codewiki-ownership-move:2.1:2.1.5
  - covers:codewiki-ownership-move:2.1:2.1.6
  - covers:codewiki-ownership-move:2.1:2.1.7
  - covers:codewiki-ownership-move:2.1:2.1.8
  - covers:codewiki-ownership-move:2.1:2.1.9
  - covers:codewiki-ownership-move:2.1:2.1.10
  tdd: true
  source_section: '2.1'
  implementation_domain: backend
- title: Add the `gwiki code` CLI
  category: code
  task_type: feature
  depends_on:
  - '2.1'
  validation_criteria: "2.3.1: `gwiki code` parses the full legacy FLAT flag surface\
    \ with identical defaults and conflict matrix; the moved codewiki parse tests\
    \ pass against the new paths. file: `crates/gwiki/src/cli/code.rs`.\n2.3.2: gwiki\
    \ contract carries the `code` command at contract_version 16 with regenerated\
    \ JSON and synced vendored fixture. file: `crates/gwiki/contract/gwiki.contract.json`.\n\
    2.3.3: Every exhaustive command site (enum, dispatch, admission classification,\
    \ CLI_SUBCOMMANDS, outcome rendering) carries `code` compile-enforced; the admission\
    \ tests pin `Code`'s dedicated mode-aware arm \u2014 engine-owned per-output lock\
    \ under contention (legacy identity and timeout), read-only `--compare-to`, and\
    \ admission without a live project row. test: `crates/gwiki/src/commands/project_admission/tests.rs`.\n\
    2.3.4: Freshness runs on the generation path only with legacy semantics: fresh\
    \ project no-ops, stale project refreshes, `--no-freshness` bypasses, and the\
    \ busy status warns only when not quiet; negative cases prove `--compare-to`,\
    \ `--purge`, and `--repair-citations` never invoke the freshness helper and never\
    \ mutate the index. test: `crates/gwiki/src/cli/tests/code.rs`."
  labels:
  - covers:codewiki-ownership-move:2.3:2.3.1
  - covers:codewiki-ownership-move:2.3:2.3.2
  - covers:codewiki-ownership-move:2.3:2.3.3
  - covers:codewiki-ownership-move:2.3:2.3.4
  tdd: true
  source_section: '2.3'
  implementation_domain: backend
- title: Remove the `gcode codewiki` surface
  category: code
  task_type: feature
  depends_on:
  - '2.3'
  validation_criteria: '2.4.1: `gcode codewiki` is absent from CLI parse, dispatch,
    and contract; `gcode --help` and the contract JSON show no codewiki entry; no
    file remains under `crates/gcode/src/commands/codewiki/` and nothing references
    it. file: `crates/gcode/contract/gcode.contract.json`.

    2.4.2: Contract-version pins across Rust and Python fixtures agree at gcode 3
    / gwiki 16. test: `tests/test_cli_contracts.py`.

    2.4.3: `commands` is no longer part of gobby-code''s public surface and the boundary
    test pins the enduring public API set with no temporary exports remaining. test:
    `crates/gcode/tests/facade_boundary.rs::public_surface_is_pinned`.

    2.4.4: The gcode unit-test target compiles and passes after the deletion: no `mod
    codewiki` test registration remains, the dispatch codewiki cases are gone, and
    the config test''s source sample reads a retained file. test: `crates/gcode/src/config/tests.rs`.

    2.4.5: The managed-PostgreSQL privilege inventory carries no `commands/codewiki`
    entry and the exact-inventory equality test passes. file: `crates/gcode/security/managed_postgres_privileges.json`.'
  labels:
  - covers:codewiki-ownership-move:2.4:2.4.1
  - covers:codewiki-ownership-move:2.4:2.4.2
  - covers:codewiki-ownership-move:2.4:2.4.3
  - covers:codewiki-ownership-move:2.4:2.4.4
  - covers:codewiki-ownership-move:2.4:2.4.5
  tdd: true
  source_section: '2.4'
  implementation_domain: backend
- title: Flip the provenance marker with no reader compatibility
  category: code
  task_type: feature
  depends_on:
  - '2.4'
  validation_criteria: "2.5.1: The renamed constant with value `gwiki-code` is the\
    \ only marker any production reader or writer recognizes; a scan of `crates` and\
    \ `src` finds `gcode-codewiki` only in the enumerated allowlist \u2014 the parity\
    \ normalizer script, the parity fixture README, and the unrecognized-marker characterization\
    \ test literals \u2014 and fails on any occurrence outside it. file: `crates/gcore/src/codewiki_contract.rs`.\n\
    2.5.2: Unrecognized `generated_by` values degrade to non-generated handling without\
    \ errors in lint, audit, and indexing, with one negative regression per retired\
    \ fallback signal: arbitrary `generated_by` value, legacy `code_*` type prefix,\
    \ and `code/**` path-trust handling in audit. test: `crates/gwiki/src/lint.rs::unrecognized_generated_by_is_plain_page`.\n\
    2.5.3: No production librarian/upkeep path aggregates, proposes, tasks, patches,\
    \ or promotes `code/**` pages after the flip: the outdated-codewiki check and\
    \ its proposal wiring are gone, and an end-to-end scheduled-run regression over\
    \ a vault containing legacy `code/**` pages produces no code-page curation while\
    \ an eligible `knowledge/**` control page in the same run still receives its normal\
    \ curation flow (the `code/**` pages stay byte-identical; the run is proven not\
    \ disabled wholesale). test: `crates/gwiki/src/librarian/tests.rs::generated_code_namespace_not_curated`.\n\
    2.5.4: Scheduled upkeep never mutates `code/**`: page enumeration excludes the\
    \ namespace before stale archiving, unworthy-concept governance, and every other\
    \ mutation stage, and a scheduled-run regression pairing a long-stale legacy `code/**`\
    \ page with an equally stale eligible `knowledge/**` control page archives the\
    \ control while the `code/**` page remains byte-identical and uncurated \u2014\
    \ proving the exclusion is namespace-scoped, not a disabled job. test: `crates/gwiki/src/upkeep/tests.rs::code_namespace_excluded_from_upkeep`."
  labels:
  - covers:codewiki-ownership-move:2.5:2.5.1
  - covers:codewiki-ownership-move:2.5:2.5.2
  - covers:codewiki-ownership-move:2.5:2.5.3
  - covers:codewiki-ownership-move:2.5:2.5.4
  tdd: true
  source_section: '2.5'
  implementation_domain: backend
- title: Prove old-vs-new parity
  category: test
  task_type: feature
  depends_on:
  - '2.3'
  - '2.4'
  - '2.5'
  validation_criteria: "2.6.1: Normalized full-run output is byte-identical to the\
    \ committed pre-move baseline. test: `crates/gwiki/tests/code_parity.rs::deterministic_output_matches_baseline`.\n\
    2.6.2: Production vault checksums are identical before and after the parity run\
    \ (the test asserts it never touched the real vault). test: `crates/gwiki/tests/code_parity.rs::production_vault_untouched`.\n\
    2.6.3: The `--engine gwiki` run obeys the capture discipline 1.1 pinned for the\
    \ gcode mode: identical committed fixture with the pinned source digest asserted,\
    \ isolated index identity, freshly built `--locked` workspace binary, capture\
    \ metadata only in the temporary run directory, and the whole tracked fixture\
    \ tree (including `project/`) clean afterwards. test: `crates/gwiki/tests/code_parity.rs::gwiki_mode_capture_discipline`.\n\
    2.6.4: `--compare-to`, `--purge --force`, `--repair-citations`, scope selection,\
    \ and incremental (`--since`-style) runs each behave as the legacy CLI did, with\
    \ one test artifact per mode. test: `crates/gwiki/tests/code_parity.rs::legacy_mode_matrix`.\n\
    2.6.5: Concurrent generation against the same output directory is refused with\
    \ the legacy per-output lock semantics and timeout, naming `gwiki code` in the\
    \ diagnostic. test: `crates/gwiki/tests/code_parity.rs::lock_contention_matches_legacy`.\n\
    2.6.6: Representative failure paths the legacy CLI actually implements produce\
    \ legacy-equivalent errors without partial writes: an invalid `--compare-to` ref\
    \ fails with the legacy \"compare ref \u2026 does not resolve to a commit\" diagnostic\
    \ (compare.rs:164/203/210), and `--complete-scope` without `--scope` fails with\
    \ the legacy argument diagnostic (run.rs:46). An unindexed project is deliberately\
    \ not a failure fixture: legacy freshness auto-indexes a never-indexed project\
    \ (freshness.rs::project_needs_refresh returns refresh-needed and `ensure_fresh`\
    \ builds the first index) rather than erroring. Production-vault cleanliness remains\
    \ the harness-only safety assertion of 2.6.2 and adds no guard to direct `gwiki\
    \ code` execution. test: `crates/gwiki/tests/code_parity.rs::failure_paths_match_legacy`."
  labels:
  - covers:codewiki-ownership-move:2.6:2.6.1
  - covers:codewiki-ownership-move:2.6:2.6.2
  - covers:codewiki-ownership-move:2.6:2.6.3
  - covers:codewiki-ownership-move:2.6:2.6.4
  - covers:codewiki-ownership-move:2.6:2.6.5
  - covers:codewiki-ownership-move:2.6:2.6.6
  tdd: false
  source_section: '2.6'
  assigned_agent: backend-developer
- title: Remove the active CodeWiki daemon machinery
  category: code
  task_type: feature
  depends_on: []
  validation_criteria: "3.1.1: The three codewiki modules are gone and nothing imports\
    \ them: each of `gcode grep -w \"codewiki_refresh\" src`, `gcode grep -w \"codewiki_trigger\"\
    \ src`, and `gcode grep -w \"codewiki_nightly\" src` returns zero hits (whole-word\
    \ scans, one retired module per invocation \u2014 never an escaped-alternation\
    \ pattern, which searches a literal pipe and passes vacuously). The intentionally\
    \ retained surfaces stay out of match range by construction: the inert `wiki.codewiki_nightly_*`\
    \ config keys and `CODEWIKI_NIGHTLY_JOB_PREFIX` embed the scan words only inside\
    \ longer identifiers or hyphenated values. The deletion test seeds a positive\
    \ control (a temp file with `from gobby.code_index.codewiki_nightly import x`)\
    \ and asserts the same scan detects it, so a zero result is proven meaningful.\
    \ file: `src/gobby/code_index/__init__.py`.\n3.1.2: `GcodeGateway` has no codewiki\
    \ operation and its remaining consumers are unchanged. symbol: `GcodeGateway`.\n\
    3.1.3: App startup constructs no codewiki trigger and registers no nightly handler.\
    \ symbol: `create_lifespan`."
  labels:
  - covers:codewiki-ownership-move:3.1:3.1.1
  - covers:codewiki-ownership-move:3.1:3.1.2
  - covers:codewiki-ownership-move:3.1:3.1.3
  tdd: true
  source_section: '3.1'
  implementation_domain: backend
- title: Reconcile CodeWiki cron rows to disabled at startup
  category: code
  task_type: feature
  depends_on:
  - '3.1'
  - '2.5'
  validation_criteria: "3.2.1: Startup disables existing nightly rows idempotently\
    \ and registers no codewiki handler; the reconciler returns the disabled, failed,\
    \ and residual row ids without touching runner state, and converges on a later\
    \ startup, never blocking daemon startup. symbol: `reconcile_codewiki_crons_disabled`.\n\
    3.2.2: A cron row created enabled by an older deployment is disabled on next startup\
    \ with `enabled=False` and `next_run_at=NULL` regardless of its `is_system` flag;\
    \ non-prefix rows and other wiki cron jobs are untouched. test: `tests/wiki/test_codewiki_dormant.py::reconcile_disables_and_preserves`.\n\
    3.2.3: No scheduled job in `gobby.wiki.scheduled_jobs` maintains generated `code/**`\
    \ content. behavior: \"generated-content maintenance paused\" in `src/gobby/wiki/codewiki_dormant.py`.\n\
    3.2.4: With an injected mid-loop failure (`update_job` raising or returning `None`),\
    \ the reconciler finishes the remaining rows and reports the failed and residual\
    \ ids in its result; startup logs them and marks `codewiki_dormant_reconciliation`\
    \ degraded on the runner, and a subsequent run converges to zero enabled rows.\
    \ test: `tests/wiki/test_codewiki_dormant.py::mid_loop_failure_degrades_and_converges`.\n\
    3.2.5: When either prefix query raises (initial enumeration or residual re-query),\
    \ startup marks `codewiki_dormant_reconciliation` degraded and still constructs\
    \ the scheduler. test: `tests/test_runner_init.py::reconciliation_query_failure_does_not_block_startup`.\n\
    3.2.6: Reconciliation runs and converges when the code indexer is absent, disabled,\
    \ or failed \u2014 the call sits outside the `code_indexer is not None` conditional\
    \ and each of the three indexer states is tested at startup. test: `tests/test_runner_init.py::reconciliation_independent_of_code_indexer`.\n\
    3.2.7: A legacy reserved-prefix row with `is_system=False` is disabled by reconciliation,\
    \ is absent from `get_due_jobs` afterwards, and lands in the failed/residual reporting\
    \ when its update fails. test: `tests/wiki/test_codewiki_dormant.py::legacy_non_system_row_reconciled`."
  labels:
  - covers:codewiki-ownership-move:3.2:3.2.1
  - covers:codewiki-ownership-move:3.2:3.2.2
  - covers:codewiki-ownership-move:3.2:3.2.3
  - covers:codewiki-ownership-move:3.2:3.2.4
  - covers:codewiki-ownership-move:3.2:3.2.5
  - covers:codewiki-ownership-move:3.2:3.2.6
  - covers:codewiki-ownership-move:3.2:3.2.7
  tdd: true
  source_section: '3.2'
  implementation_domain: backend
- title: Register the dormant wiki-owned routes and retire the legacy routes
  category: code
  task_type: feature
  depends_on:
  - '3.1'
  validation_criteria: '3.3.1: New status route returns 200 with exactly the pinned
    dormant shape (`enabled: false`, `state: "disabled"`, `reason: "pending_wiki_redesign"`,
    no legacy live-only fields); refresh returns 409 with the stable error code and
    provably performs no work. test: `tests/servers/routes/test_wiki_code_routes.py::dormant_status_and_refresh`.

    3.3.2: Legacy `/api/code-index/codewiki/*` paths return 404 with no alias or redirect.
    test: `tests/servers/routes/test_code_index_routes.py::codewiki_routes_absent`.

    3.3.3: Auth table carries the new refresh path and no legacy codewiki path. file:
    `src/gobby/servers/auth_service.py`.'
  labels:
  - covers:codewiki-ownership-move:3.3:3.3.1
  - covers:codewiki-ownership-move:3.3:3.3.2
  - covers:codewiki-ownership-move:3.3:3.3.3
  tdd: true
  source_section: '3.3'
  implementation_domain: backend
- title: Stop the installer emitting the codewiki git-hook trigger
  category: code
  task_type: feature
  depends_on: []
  validation_criteria: '3.4.1: Freshly installed post-commit hooks contain no codewiki
    refresh call in either shell branch. test: `tests/cli/installers/test_git_hooks_installer.py::post_commit_has_no_codewiki_curl`.'
  labels:
  - covers:codewiki-ownership-move:3.4:3.4.1
  tdd: true
  source_section: '3.4'
  implementation_domain: backend
- title: Clean up AI-surface coupling
  category: code
  task_type: feature
  depends_on: []
  validation_criteria: '3.5.1: Tool-chat whitelist has no codewiki entry; the whitelist
    tests include a mutation-enabled case proving codewiki is rejected as unlisted
    when `allow_mutation=True`. test: `tests/ai/test_tool_chat_tools.py`.

    3.5.2: No production comment cites codewiki nightly as an active rationale. behavior:
    "stale codewiki rationale removed" in `src/gobby/ai/_text_generation_service.py`.'
  labels:
  - covers:codewiki-ownership-move:3.5:3.5.1
  - covers:codewiki-ownership-move:3.5:3.5.2
  tdd: true
  source_section: '3.5'
  implementation_domain: backend
- title: Reflect dormancy in config descriptions and the web UI
  category: code
  task_type: feature
  depends_on:
  - '3.3'
  validation_criteria: '3.6.1: Settings UI shows no codewiki automation controls;
    wiki code mode shows the dormant badge with reason and offers no refresh action.
    test: `web/src/components/activity/wiki/__tests__/WikiCodeMode.test.tsx`.

    3.6.2: Config descriptions state the paused status; keys remain accepted and inert.
    file: `src/gobby/config/wiki.py`.

    3.6.3: Status polling stops when state is disabled. file: `web/src/components/activity/wiki/WikiCodewikiStatus.tsx`.'
  labels:
  - covers:codewiki-ownership-move:3.6:3.6.1
  - covers:codewiki-ownership-move:3.6:3.6.2
  - covers:codewiki-ownership-move:3.6:3.6.3
  tdd: true
  source_section: '3.6'
  implementation_domain: fullstack
- title: Pin dream tolerance of a frozen truth digest
  category: test
  task_type: feature
  depends_on: []
  validation_criteria: '3.7.1: Dream runs cleanly across the seen-frozen, absent,
    and first-sight digest cases with the pinned trigger behavior (quiet, quiet, fire-once);
    any crash the test uncovers is repaired in this same leaf and the test rerun cleanly
    before closure. test: `tests/memory/test_dream_frozen_digest.py::frozen_digest_is_tolerated`.'
  labels:
  - covers:codewiki-ownership-move:3.7:3.7.1
  tdd: false
  source_section: '3.7'
  assigned_agent: backend-developer
- title: Bump and align Rust crate versions
  category: config
  task_type: feature
  depends_on:
  - '2.1'
  - '2.3'
  - '2.4'
  - '2.5'
  - '2.6'
  - '3.1'
  - '3.2'
  - '3.3'
  - '3.4'
  - '3.5'
  - '3.6'
  - '3.7'
  validation_criteria: '4.1.1: All five crates carry the bumped versions with aligned
    internal requirements and a regenerated lock. file: `Cargo.lock`.

    4.1.2: Installed binaries report the new versions. behavior: "reinstalled binary
    versions verified" in `crates/gwiki/Cargo.toml`.'
  labels:
  - covers:codewiki-ownership-move:4.1:4.1.1
  - covers:codewiki-ownership-move:4.1:4.1.2
  tdd: true
  source_section: '4.1'
  assigned_agent: backend-developer
- title: Update contracts, docs, and skill guidance
  category: docs
  task_type: feature
  depends_on:
  - '2.1'
  - '2.3'
  - '2.4'
  - '2.5'
  - '2.6'
  - '3.1'
  - '3.2'
  - '3.3'
  - '3.4'
  - '3.5'
  - '3.6'
  - '3.7'
  validation_criteria: '4.2.1: The codewiki guide documents `gwiki code`, the paused
    state, and the dormant routes; the docs contract test passes against it. test:
    `tests/docs/test_codewiki_docs.py`.

    4.2.2: No active skill, contract doc, guide, or crate README references `gcode
    codewiki`: the stale-reference scan covers the full enumerated inventory (embedded
    gcode asset, gcode CLI contract doc, the five guides, and `crates/gcode/README.md`
    included; the enumerated historical and no-touch exclusions documented). behavior:
    "no stale gcode codewiki references" in `docs/contracts/gwiki-cli.md`.'
  labels:
  - covers:codewiki-ownership-move:4.2:4.2.1
  - covers:codewiki-ownership-move:4.2:4.2.2
  tdd: false
  source_section: '4.2'
  assigned_agent: tech-writer
```
