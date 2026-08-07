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
  `gwiki code` in isolated temporary vaults after normalizing only the
  intentional diffs (provenance marker, generated-by strings). Production wiki
  files stay byte-for-byte untouched throughout.
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

Create `scripts/codewiki_parity_baseline.sh`: runs a codewiki generation with
`--ai off` against a small committed fixture project (anchored by
`crates/gwiki/tests/fixtures/codewiki_parity/project/README.md`; the
fixture's sources live beside that README and never change during the move)
into a fresh temporary vault (isolated out-dir, never the production vault),
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
shifted source digest can therefore leak into generated output. Normalization strips exactly two classes of
difference and nothing else. First, the intentional post-move diffs:
`generated_by:` frontmatter values and any `gcode-codewiki` marker strings.
Second, the per-run stamps the engine emits unconditionally and which no
fixture can hold constant — `commit:`, `commit_dirty:`, and the wall-clock
`generated:` frontmatter lines, plus the matching `commit` / `commit_dirty` /
generation-timestamp fields in `_meta/codewiki.json` (the engine derives these
from `git rev-parse HEAD` and `git status --porcelain` against the enclosing
repository and from local wall-clock time, so they differ between the
pre-move capture and every later comparison run). Everything else (page set,
page bodies, per-file source hashes, remaining `_meta` manifest structure,
lock/repair behavior artifacts) is hashed as-is into a manifest file
`crates/gwiki/tests/fixtures/codewiki_parity/baseline.sha256` (one
`<sha256>  <vault-relative-path>` line per generated file, sorted), committed
as the fixture the 2.6 parity test consumes. The 2.6 parity test invokes this
same script in `--engine gwiki` mode rather than reimplementing normalization,
so the producers and the consumer of the manifest cannot drift. The README
documents the capture command, the engine modes, the normalization rules, and
the regeneration procedure. The baseline MUST be captured and committed (via
`--engine gcode`) before 2.4 removes `gcode codewiki`.

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
- 1.1.4 - Both engine modes run against the identical committed fixture
  project with an isolated index identity, execute only freshly built
  `--locked` workspace binaries, assert the pinned fixture source digest
  before generating, and record revision/version capture metadata outside the
  hashed manifest and outside `project/`.
  file: `crates/gwiki/tests/fixtures/codewiki_parity/README.md`.
- 1.1.5 - Every byte under
  `crates/gwiki/tests/fixtures/codewiki_parity/project/` is unchanged and that
  subtree clean after each engine mode; `--engine gcode` writes no tracked
  file beyond the outer README and `baseline.sha256`, and `--engine gwiki`
  leaves the whole tracked fixture tree clean. Normalization strips the
  per-run `commit`/`commit_dirty`/generation-timestamp stamps so two captures
  of the identical fixture agree.
  file: `scripts/codewiki_parity_baseline.sh`.

### 1.2 Implement the `codewiki_facts` facade in gobby-code [category: code]
`kind: deliverable`

Targets:
- `crates/gcode/src/codewiki_facts/mod.rs`
- `crates/gcode/src/codewiki_facts/scope.rs`
- `crates/gcode/src/codewiki_facts/symbols.rs`
- `crates/gcode/src/codewiki_facts/graph.rs`
- `crates/gcode/src/codewiki_facts/search.rs`
- `crates/gcode/src/codewiki_facts/text.rs`
- `crates/gcode/src/lib.rs::*` — scope-reason: register the new public facade module alongside the existing module declarations

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
tool-loop operations → enumerated explicitly and carried by that runtime,
never by the facade. The facade covers the first bucket only. The
expected shape (adjust names to inventory findings, keep the boundary rules):

```rust
// crates/gcode/src/codewiki_facts/mod.rs
pub struct CodewikiFacts { /* private: Context, lazy connections */ }

impl CodewikiFacts {
    /// Opens facts for a project root; resolves config/Context internally.
    pub fn open(project_root: &Path) -> Result<Self>;

    // scope.rs — scoped file/module facts
    pub fn scoped_files(&mut self, scope: &ScopeSelector) -> Result<Vec<FileFact>>;
    // symbols.rs — symbol facts incl. language/visibility/hash
    pub fn symbols_in(&mut self, files: &[FileId]) -> Result<Vec<SymbolFact>>;
    // graph.rs — typed graph edges (calls/imports/coupling)
    pub fn edges(&mut self, seed: &ScopeSelector, kind: EdgeKind) -> Result<Vec<EdgeFact>>;
    // search.rs — index-backed search
    pub fn search(&mut self, query: &str, limit: usize) -> Result<Vec<SearchHit>>;
    // text.rs — grep over indexed sources
    pub fn grep(&mut self, pattern: &str, scope: &ScopeSelector) -> Result<Vec<GrepHit>>;
}
```

Boundary rules (non-negotiable): every return type is a plain owned fact struct
defined in this module; no `postgres::Client`, `Context`, connection string,
or datastore configuration appears in any public signature; connections are
opened lazily and read-only (`db::connect_readonly`); no method performs a
write. Indexing and projection writes remain owned by gcode's existing
internal paths. Document in the module header that this facade is the seed
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
- 1.2.3 - Facade performs no datastore writes; a test proves read-only
  connection use. symbol: `CodewikiFacts`.

### 1.3 Privatize gcode's datastore modules and pin the boundary [category: code] (depends: 1.2)
`kind: deliverable`

Targets:
- `crates/gcode/src/lib.rs::*` — scope-reason: flip datastore module visibility, absorb cli/dispatch behind one public entrypoint, and pin the public API set
- `crates/gcode/src/main.rs::*` — scope-reason: reduced to a thin caller of the public entrypoint
- `crates/gcode/src/cli.rs::*` — scope-reason: moves from the binary into the library as a private module
- `crates/gcode/src/dispatch.rs::*` — scope-reason: moves from the binary into the library behind the public entrypoint
- `crates/gcode/tests/facade_boundary.rs`
- `crates/gcode/tests/graph_standalone/local_c_cpp.rs::*` — scope-reason: db-touching cases relocate to in-crate unit tests
- `crates/gcode/tests/graph_standalone/support.rs::*` — scope-reason: typed-query helper relocates with its consumers
- `crates/gcode/tests/projection_stale.rs::*` — scope-reason: models-constant access rerouted through the pinned public test surface
- `crates/gcode/tests/vector_projection.rs::*` — scope-reason: config/models/vector internals cases relocate to in-crate unit tests

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

The crate's integration tests are external consumers too: the
`graph_standalone` db/typed-query cases, `projection_stale`'s models-constant
access, and `vector_projection`'s config/models/vector cases relocate to
in-crate unit tests (or reroute through the pinned public
`test_env`/`contract` surfaces where that is the smaller change);
`tests/contract.rs` keeps using the deliberately public `contract` module.

Add `crates/gcode/tests/facade_boundary.rs`: a compile-time/API test asserting
the crate's public surface is exactly the intended set (facade + explicitly
pinned items), so a future `pub` leak fails CI.

**Acceptance:**

- 1.3.1 - Datastore modules are no longer `pub`; the binary is a thin caller
  of the public entrypoint; the crate, binary, and integration tests all
  compile with unchanged behavior. file: `crates/gcode/src/lib.rs`.
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
later regeneration for the version bump.

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
file is left behind or dropped. The Targets above enumerate the destination
files this leaf changes *beyond* pure relocation — the new module root and
runtime carrier, the decomposed composer, the ownership-identity files, and
the gcode-side facade — rather than restating all ≈108 mechanically copied
paths, whose per-file identity carries no decision and would go stale the
moment a file is added to the tree before implementation. Acceptance 2.1.1
and 2.4.1 pin the wholesale outcome instead. Do NOT place anything under
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

Ownership identities embedded in the moved engine move with it. The daemon
agentic caller constant (`DAEMON_AGENTIC_CALLER`, today `"gcode.codewiki"`,
pinned by a stability test and by an outcome test fixture) becomes
`"gwiki.code"`, and the lock-contention diagnostics that tell users "another
gcode codewiki run is already writing" become "another `gwiki code` run"
(production message, its test assertion, and the module doc comment).
`ToolPolicy.cli` stays `"gcode"` — that field names the CLI whose read-only
query tools the loop executes, which remains gcode after the move, so it is
deliberately preserved rather than renamed. `code_engine_boundary.rs` pins all
three distinctions.

Non-datastore context threads through a new gwiki-owned carrier
`crates/gwiki/src/commands/code/runtime.rs`: a `CodeEngineRuntime` struct
holding project root/id, quiet/output settings, and the `CodewikiFacts`
handle, populated from 1.2's dependency classification. AI/daemon routing and
tool-loop operations are carried explicitly on the runtime per that
classification — the facade stays queries-only.
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

The command threads gwiki's full exhaustive-command architecture,
compile-enforced at every site: the public `Command` enum, module
registration and the exhaustive dispatch match, admission classification in
`classify_command` (`Code` generates vault pages, so it is pinned as a
writer arm with the admission tests updated), the `CLI_SUBCOMMANDS` sync
list and its clap-parity tests, and a `CommandResult`/outcome renderer for
the code modes. Only the codewiki parse cases move from gcode's CLI tests
into `crates/gwiki/src/cli/tests/code.rs` (the unrelated setup case stays
behind — see 2.4).

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
  compile-enforced, and `Code` is pinned as a writer in the admission tests.
  test: `crates/gwiki/src/commands/project_admission/tests.rs`.

### 2.4 Remove the `gcode codewiki` surface [category: code] (depends: 2.3)
`kind: deliverable`

Targets:
- `crates/gcode/src/cli.rs::*` — scope-reason: remove the Codewiki variant, its value enums, and From impls
- `crates/gcode/src/dispatch.rs::*` — scope-reason: remove the three codewiki dispatch sites (service-config arms, ai-options helper, execution arm)
- `crates/gcode/src/lib.rs::*` — scope-reason: make `commands` private now that its last external consumer is gone, and repin the enduring public API set
- `crates/gcode/src/commands/mod.rs::*` — scope-reason: remove the codewiki module declaration and reduce child-module visibility alongside the parent flip
- `crates/gcode/src/commands/codewiki/mod.rs::*` — scope-reason: the whole source tree is deleted here, after 2.1 copied it to gwiki; no symbol survives at this path
- `crates/gcode/src/commands/codewiki/diagram_compose.rs::*` — scope-reason: the over-ceiling source file is deleted with the tree; its decomposed counterpart already exists in gwiki
- `crates/gcode/tests/facade_boundary.rs`
- `crates/gcode/src/contract.rs::contract`
- `crates/gcode/src/cli/tests/codewiki.rs::*` — scope-reason: module deleted after its codewiki cases moved to gwiki (2.3) and its setup case moved to the new setup test module
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
Nothing may reference the old tree afterwards.

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
- `crates/gwiki/src/librarian/tests.rs::*` — scope-reason: update hardcoded marker literals in tests and add the namespace-scope pin

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
NO recognition of the old `gcode-codewiki` value anywhere after this change;
existing vault pages simply stop matching. Verify (and pin with a test) that
pages whose `generated_by` no longer matches are treated as ordinary
non-generated pages by lint/audit/indexing — skipped or defaulted, never
errored — and that no AI-consuming maintenance path (librarian/upkeep) gains
scope over `code/**` because of the changed classification.

**Acceptance:**

- 2.5.1 - The renamed constant with value `gwiki-code` is the only marker any
  production reader or writer recognizes; a scan of `crates` and `src` finds
  `gcode-codewiki` only in the enumerated allowlist — the parity normalizer
  script, the parity fixture README, and the unrecognized-marker
  characterization test literals — and fails on any occurrence outside it.
  file: `crates/gcore/src/codewiki_contract.rs`.
- 2.5.2 - Unrecognized `generated_by` values degrade to non-generated handling
  without errors in lint, audit, and indexing. test: `crates/gwiki/src/lint.rs::unrecognized_generated_by_is_plain_page`.
- 2.5.3 - No librarian/upkeep AI path processes `code/**` pages as curated
  content after the flip. test: `crates/gwiki/src/librarian/tests.rs::generated_code_namespace_not_curated`.

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
Path-diff reporting and the production-vault refusal/assertion stay in the
Rust harness: any diff fails with the offending paths listed. Also exercise the failure-path
surface: `compare`, `purge`, `repair`, lock contention, and scope flags each
behave as the legacy CLI did (reuse the moved test suite; this test is the
end-to-end byte gate). Mark `#[ignore]`-gated if runtime exceeds normal CI
budget and wire it into the focused validation command set instead — do not
silently skip it.

**Acceptance:**

- 2.6.1 - Normalized full-run output is byte-identical to the committed
  pre-move baseline. test: `crates/gwiki/tests/code_parity.rs::deterministic_output_matches_baseline`.
- 2.6.2 - Production vault checksums are identical before and after the parity
  run (the test asserts it never touched the real vault).
  test: `crates/gwiki/tests/code_parity.rs::production_vault_untouched`.

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
under tests/code_index/ are deleted with their modules, and the codewiki
cron-registration cases in `tests/test_runner_init.py` are deleted here; the
reconciliation cases that replace them are owned by 3.2, which introduces
that behavior.

**Acceptance:**

- 3.1.1 - The three codewiki modules are gone and nothing imports them;
  `gcode grep "codewiki_refresh\|codewiki_trigger\|codewiki_nightly" src`
  returns zero production hits. file: `src/gobby/code_index/__init__.py`.
- 3.1.2 - `GcodeGateway` has no codewiki operation and its remaining consumers
  are unchanged. symbol: `GcodeGateway`.
- 3.1.3 - App startup constructs no codewiki trigger and registers no nightly
  handler. symbol: `create_lifespan`.

### 3.2 Reconcile CodeWiki cron rows to disabled at startup [category: code] (depends: 3.1)
`kind: deliverable`

Targets:
- `src/gobby/wiki/codewiki_dormant.py`
- `src/gobby/wiki/__init__.py::*` — scope-reason: export the dormancy reconciler
- `src/gobby/runner_init/orchestration.py::*` — scope-reason: insert the reconciliation call where registration used to happen
- `tests/test_runner_init.py::*` — scope-reason: add the reconciliation-at-startup cases
- `tests/wiki/test_codewiki_dormant.py`

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
    """Idempotently set enabled=False on every system cron row under the
    nightly prefix. Registers no handler, takes no runner, and marks no
    service state. Returns the disabled, failed, and still-enabled row ids
    so the caller can report residue."""
```

Implement it with the existing `CronJobStorage` primitives — no bespoke SQL:
`list_system_jobs_by_name_prefix(CODEWIKI_NIGHTLY_JOB_PREFIX, enabled=True)`
(escaped, system-only prefix matching) followed by
`update_job(job.id, enabled=False)` for each match. Result population is
explicit: an update returning a `CronJob` appends that row id to `disabled`,
an update that raises or returns `None` appends it to `failed`, and the
post-loop re-query populates `residual_enabled`. `update_job` permits
`enabled` changes on system rows
and recomputes `next_run_at` (`compute_next_run` returns `None` for disabled
jobs), so after reconciliation every matched row has `enabled=False` and
`next_run_at=NULL` while non-system and non-prefix rows remain untouched.

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
`list_system_jobs_by_name_prefix` queries — the initial enumeration and the
residual re-query — can raise before any result exists. `init_orchestration`
therefore wraps the whole reconciliation call in its own try/except that
logs, marks `codewiki_dormant_reconciliation` degraded, and continues with
cron setup, so a storage fault cannot escape into the surrounding cron block
and skip scheduler construction. Scheduler startup proceeds either way — an orphaned enabled row is contained (its firing fails
with the executor's no-handler error, logged as a failed run with backoff)
until a later startup disables it; daemon startup is never blocked on this
reconciliation.

Call it from `init_orchestration` where registration used to happen (3.1
removed the registrar); this leaf owns that call-site insertion and its
runner-init test cases. It must be idempotent (safe on every startup), must not delete
rows, must not register `codewiki_nightly:*` handlers, and must leave every
other wiki cron job (`gobby.wiki.scheduled_jobs`) untouched. Verify that no
remaining scheduled job maintains generated `code/**` content: audit
`register_wiki_cron_jobs` registrations and pin the finding (if a shared job
touches `code/**`, scope it away rather than pausing it — knowledge upkeep
keeps running).

**Acceptance:**

- 3.2.1 - Startup disables existing nightly rows idempotently and registers no
  codewiki handler; the reconciler returns the disabled, failed, and residual
  row ids without touching runner state, and converges on a later startup,
  never blocking daemon startup. symbol: `reconcile_codewiki_crons_disabled`.
- 3.2.2 - A cron row created enabled by an older deployment is disabled on next
  startup with `enabled=False` and `next_run_at=NULL`; non-system rows,
  non-prefix rows, and other wiki cron jobs are untouched. test: `tests/wiki/test_codewiki_dormant.py::reconcile_disables_and_preserves`.
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
- `src/gobby/servers/routes/llm.py::*` — scope-reason: update the codewiki-specific timeout-override rationale comment
- `src/gobby/ai/_text_generation_service.py::*` — scope-reason: update the circuit-breaker sizing comment that cites the codewiki nightly batch
- `tests/ai/test_text_generation_circuit_breaker.py::*` — scope-reason: update stale codewiki references in breaker tests

Remove `"codewiki"` from the tool-chat gcode subcommand whitelist
(`_tool_chat_tools.py:101`) — the subcommand no longer exists. Keep the
text-generation circuit breaker and LLM timeout override mechanisms unchanged
(they are generic protection); rewrite their comments (`llm.py:87-95`,
`_text_generation_service.py:66-72`) to stop citing the retired nightly batch
as the rationale. Update `tests/ai/test_text_generation_circuit_breaker.py`
references as needed.

**Acceptance:**

- 3.5.1 - Tool-chat whitelist has no codewiki entry and whitelist tests pass.
  file: `src/gobby/ai/_tool_chat_tools.py`.
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
branch for this task to fix. If the characterization test nonetheless
uncovers one, it is a defect outside this plan's coverage — file it as a
separate task rather than editing production code from a leaf that declares
no production Targets.

**Acceptance:**

- 3.7.1 - Dream runs cleanly across the seen-frozen, absent, and first-sight
  digest cases with the pinned trigger behavior (quiet, quiet, fire-once).
  test: `tests/memory/test_dream_frozen_digest.py::frozen_digest_is_tolerated`.

## P4: Versions, contracts, and documentation
`kind: framing`

**Goal**: Every crate, contract, fixture, and document agrees on the new
ownership, and installed binaries prove it.

### 4.1 Bump and align Rust crate versions [category: config] (depends: P2)
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

**Acceptance:**

- 4.1.1 - All five crates carry the bumped versions with aligned internal
  requirements and a regenerated lock. file: `Cargo.lock`.
- 4.1.2 - Installed binaries report the new versions.
  behavior: "reinstalled binary versions verified" in `crates/gwiki/Cargo.toml`.

### 4.2 Update contracts, docs, and skill guidance [category: docs] (depends: P2)
`kind: deliverable`

Targets:
- `docs/guides/codewiki.md`
- `docs/contracts/gwiki-cli.md`
- `docs/contracts/gcode-cli.md`
- `src/gobby/install/shared/skills/code-index/SKILL.md`
- `crates/gcode/assets/SKILL.md`
- `tests/docs/test_codewiki_docs.py::*` — scope-reason: repin the docs contract test to the rewritten guide
- `tests/skills/test_code_index_skill.py::*` — scope-reason: repin the embedded-skill content assertions

Rewrite `docs/guides/codewiki.md` for `gwiki code`: direct CLI available for
isolated/manual use, production-vault execution operationally paused pending
the redesign, daemon routes dormant with the stable status/error codes, legacy
routes and `gcode codewiki` gone. Update `docs/contracts/gwiki-cli.md` for the
new command and `docs/contracts/gcode-cli.md` to drop its codewiki
JSON-envelope and citation-repair sections. The bundled `code-index` skill
template keeps owning the CodeWiki lifecycle guidance (no new gwiki skill
surface — one consumer, existing owner): rewrite its codewiki bullets to the
paused/manual `gwiki code` surface with a pointer to the guide, and update
the byte-identical embedded gcode skill asset (`crates/gcode/assets/SKILL.md`)
in the same change so its content pin stays green. Add a focused
stale-reference scan across active skill/contract/guide surfaces (historical
changelogs and plan evidence excluded). Keep `tests/docs/test_codewiki_docs.py`
and `tests/skills/test_code_index_skill.py` passing against the rewritten
surfaces — update expectations in the same change.

**Acceptance:**

- 4.2.1 - The codewiki guide documents `gwiki code`, the paused state, and the
  dormant routes; the docs contract test passes against it.
  test: `tests/docs/test_codewiki_docs.py`.
- 4.2.2 - No active skill, contract doc, or guide references `gcode codewiki`
  (embedded gcode asset and gcode CLI contract doc included; historical
  changelogs excluded). behavior: "no stale gcode codewiki references" in `docs/contracts/gwiki-cli.md`.

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

## Task Mapping
`kind: framing`

<!-- Updated after task creation -->
| Plan Item | Task Ref | Status |
|-----------|----------|--------|

## M1 Task Manifest
`kind: manifest`

```yaml
- title: Capture the deterministic parity baseline
  category: test
  task_type: feature
  depends_on: []
  validation_criteria: '1.1.1: Baseline script produces a sorted normalized hash manifest
    from an isolated temp vault and refuses to run against the configured production
    vault path. file: `scripts/codewiki_parity_baseline.sh`.

    1.1.2: Committed baseline manifest exists with one entry per generated file and
    documented normalization rules. file: `crates/gwiki/tests/fixtures/codewiki_parity/README.md`.

    1.1.3: The script accepts exactly the two enumerated engine modes (`--engine gcode`,
    `--engine gwiki`) and both share the one normalization/manifest path; no second
    normalization implementation exists. file: `scripts/codewiki_parity_baseline.sh`.

    1.1.4: Both engine modes run against the identical committed fixture project with
    an isolated index identity, execute only freshly built `--locked` workspace binaries,
    assert the pinned fixture source digest before generating, and record revision/version
    capture metadata outside the hashed manifest and outside `project/`. file: `crates/gwiki/tests/fixtures/codewiki_parity/README.md`.

    1.1.5: Every byte under `crates/gwiki/tests/fixtures/codewiki_parity/project/`
    is unchanged and that subtree clean after each engine mode; `--engine gcode` writes
    no tracked file beyond the outer README and `baseline.sha256`, and `--engine gwiki`
    leaves the whole tracked fixture tree clean. Normalization strips the per-run
    `commit`/`commit_dirty`/generation-timestamp stamps so two captures of the identical
    fixture agree. file: `scripts/codewiki_parity_baseline.sh`.'
  labels:
  - covers:codewiki-ownership-move:1.1:1.1.1
  - covers:codewiki-ownership-move:1.1:1.1.2
  - covers:codewiki-ownership-move:1.1:1.1.3
  - covers:codewiki-ownership-move:1.1:1.1.4
  - covers:codewiki-ownership-move:1.1:1.1.5
  tdd: false
  source_section: '1.1'
  assigned_agent: backend-developer
- title: Implement the `codewiki_facts` facade in gobby-code
  category: code
  task_type: feature
  depends_on: []
  validation_criteria: '1.2.1: Facade module exists with typed owned fact structs
    and lazy read-only access; no public signature exposes connections, `Context`,
    or credentials. file: `crates/gcode/src/codewiki_facts/mod.rs`.

    1.2.2: Every external dependency found by the full `crate::` inventory is classified
    (facade family, runtime carrier, or explicit AI-routing carry-over); every facade-bucket
    family has a typed method; and the inventory with its classification is recorded
    in the module header docs. file: `crates/gcode/src/codewiki_facts/mod.rs`.

    1.2.3: Facade performs no datastore writes; a test proves read-only connection
    use. symbol: `CodewikiFacts`.'
  labels:
  - covers:codewiki-ownership-move:1.2:1.2.1
  - covers:codewiki-ownership-move:1.2:1.2.2
  - covers:codewiki-ownership-move:1.2:1.2.3
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
    all compile with unchanged behavior. file: `crates/gcode/src/lib.rs`.

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
    \ test: `crates/gwiki/tests/code_engine_boundary.rs::ownership_identities_moved`."
  labels:
  - covers:codewiki-ownership-move:2.1:2.1.1
  - covers:codewiki-ownership-move:2.1:2.1.2
  - covers:codewiki-ownership-move:2.1:2.1.3
  - covers:codewiki-ownership-move:2.1:2.1.4
  - covers:codewiki-ownership-move:2.1:2.1.5
  - covers:codewiki-ownership-move:2.1:2.1.6
  - covers:codewiki-ownership-move:2.1:2.1.7
  tdd: true
  source_section: '2.1'
  implementation_domain: backend
- title: Add the `gwiki code` CLI
  category: code
  task_type: feature
  depends_on:
  - '2.1'
  validation_criteria: '2.3.1: `gwiki code` parses the full legacy FLAT flag surface
    with identical defaults and conflict matrix; the moved codewiki parse tests pass
    against the new paths. file: `crates/gwiki/src/cli/code.rs`.

    2.3.2: gwiki contract carries the `code` command at contract_version 16 with regenerated
    JSON and synced vendored fixture. file: `crates/gwiki/contract/gwiki.contract.json`.

    2.3.3: Every exhaustive command site (enum, dispatch, admission classification,
    CLI_SUBCOMMANDS, outcome rendering) carries `code` compile-enforced, and `Code`
    is pinned as a writer in the admission tests. test: `crates/gwiki/src/commands/project_admission/tests.rs`.'
  labels:
  - covers:codewiki-ownership-move:2.3:2.3.1
  - covers:codewiki-ownership-move:2.3:2.3.2
  - covers:codewiki-ownership-move:2.3:2.3.3
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
    `crates/gcode/tests/facade_boundary.rs::public_surface_is_pinned`.'
  labels:
  - covers:codewiki-ownership-move:2.4:2.4.1
  - covers:codewiki-ownership-move:2.4:2.4.2
  - covers:codewiki-ownership-move:2.4:2.4.3
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
    \ errors in lint, audit, and indexing. test: `crates/gwiki/src/lint.rs::unrecognized_generated_by_is_plain_page`.\n\
    2.5.3: No librarian/upkeep AI path processes `code/**` pages as curated content\
    \ after the flip. test: `crates/gwiki/src/librarian/tests.rs::generated_code_namespace_not_curated`."
  labels:
  - covers:codewiki-ownership-move:2.5:2.5.1
  - covers:codewiki-ownership-move:2.5:2.5.2
  - covers:codewiki-ownership-move:2.5:2.5.3
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
  validation_criteria: '2.6.1: Normalized full-run output is byte-identical to the
    committed pre-move baseline. test: `crates/gwiki/tests/code_parity.rs::deterministic_output_matches_baseline`.

    2.6.2: Production vault checksums are identical before and after the parity run
    (the test asserts it never touched the real vault). test: `crates/gwiki/tests/code_parity.rs::production_vault_untouched`.'
  labels:
  - covers:codewiki-ownership-move:2.6:2.6.1
  - covers:codewiki-ownership-move:2.6:2.6.2
  tdd: false
  source_section: '2.6'
  assigned_agent: backend-developer
- title: Remove the active CodeWiki daemon machinery
  category: code
  task_type: feature
  depends_on: []
  validation_criteria: '3.1.1: The three codewiki modules are gone and nothing imports
    them; `gcode grep "codewiki_refresh\|codewiki_trigger\|codewiki_nightly" src`
    returns zero production hits. file: `src/gobby/code_index/__init__.py`.

    3.1.2: `GcodeGateway` has no codewiki operation and its remaining consumers are
    unchanged. symbol: `GcodeGateway`.

    3.1.3: App startup constructs no codewiki trigger and registers no nightly handler.
    symbol: `create_lifespan`.'
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
  validation_criteria: '3.2.1: Startup disables existing nightly rows idempotently
    and registers no codewiki handler; the reconciler returns the disabled, failed,
    and residual row ids without touching runner state, and converges on a later startup,
    never blocking daemon startup. symbol: `reconcile_codewiki_crons_disabled`.

    3.2.2: A cron row created enabled by an older deployment is disabled on next startup
    with `enabled=False` and `next_run_at=NULL`; non-system rows, non-prefix rows,
    and other wiki cron jobs are untouched. test: `tests/wiki/test_codewiki_dormant.py::reconcile_disables_and_preserves`.

    3.2.3: No scheduled job in `gobby.wiki.scheduled_jobs` maintains generated `code/**`
    content. behavior: "generated-content maintenance paused" in `src/gobby/wiki/codewiki_dormant.py`.

    3.2.4: With an injected mid-loop failure (`update_job` raising or returning `None`),
    the reconciler finishes the remaining rows and reports the failed and residual
    ids in its result; startup logs them and marks `codewiki_dormant_reconciliation`
    degraded on the runner, and a subsequent run converges to zero enabled rows. test:
    `tests/wiki/test_codewiki_dormant.py::mid_loop_failure_degrades_and_converges`.

    3.2.5: When either prefix query raises (initial enumeration or residual re-query),
    startup marks `codewiki_dormant_reconciliation` degraded and still constructs
    the scheduler. test: `tests/test_runner_init.py::reconciliation_query_failure_does_not_block_startup`.'
  labels:
  - covers:codewiki-ownership-move:3.2:3.2.1
  - covers:codewiki-ownership-move:3.2:3.2.2
  - covers:codewiki-ownership-move:3.2:3.2.3
  - covers:codewiki-ownership-move:3.2:3.2.4
  - covers:codewiki-ownership-move:3.2:3.2.5
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
  validation_criteria: '3.5.1: Tool-chat whitelist has no codewiki entry and whitelist
    tests pass. file: `src/gobby/ai/_tool_chat_tools.py`.

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
    and first-sight digest cases with the pinned trigger behavior (quiet, quiet, fire-once).
    test: `tests/memory/test_dream_frozen_digest.py::frozen_digest_is_tolerated`.'
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
  validation_criteria: '4.2.1: The codewiki guide documents `gwiki code`, the paused
    state, and the dormant routes; the docs contract test passes against it. test:
    `tests/docs/test_codewiki_docs.py`.

    4.2.2: No active skill, contract doc, or guide references `gcode codewiki` (embedded
    gcode asset and gcode CLI contract doc included; historical changelogs excluded).
    behavior: "no stale gcode codewiki references" in `docs/contracts/gwiki-cli.md`.'
  labels:
  - covers:codewiki-ownership-move:4.2:4.2.1
  - covers:codewiki-ownership-move:4.2:4.2.2
  tdd: false
  source_section: '4.2'
  assigned_agent: tech-writer
```
