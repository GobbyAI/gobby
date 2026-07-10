# Epic #17744 Work Log

Wiki Obsidian panel epic. One entry per leaf task, appended as tasks are worked.

## #17751 — Add `--stdout` and `--include` to `gwiki graph`

**Status:** in progress
**Session:** #8116 (274e513b)
**Plan section:** `wiki-obsidian-panel` 1.1 (backend, Rust)

### Plan

Today `gwiki graph` only writes artifacts (`outputs/graph.json`, `GRAPH_REPORT.md`,
plus agent artifacts). Add flags so the daemon can fetch fresh graph JSON on demand.

1. `crates/gwiki/src/graph/mod.rs`
   - New `pub enum GraphInclude { Knowledge, Code, All }` (derives `clap::ValueEnum`
     — clap is already a lib dependency via `output::Format`; `All` is `Default`).
   - New `WikiGraphFacts::retain_include(&mut self, include)`:
     - `knowledge`: retain documents whose graph path starts with `knowledge/`,
       plus `recaps/` and root pages; retain links (incl. unresolved targets) whose
       `source_path` is retained and sources whose `document_path` is retained;
       clear `code_edges`.
     - `code`: retain `code/**` documents (links/sources filtered the same way);
       keep `code_edges`.
     - `all`: no-op.
     - Analytics recompute automatically because `export_graph` calls
       `analyze_facts(self)` on the filtered facts.
2. `crates/gwiki/src/api.rs` — `Command::Graph { scope, options: GraphCommandOptions }`;
   new `GraphCommandOptions { stdout: bool, include: GraphInclude }` (+ `Default`).
3. `crates/gwiki/src/lib.rs` — re-export `GraphCommandOptions`, `GraphInclude`.
4. `crates/gwiki/src/main.rs` — `Graph(GraphArgs)` with
   `--stdout` and `--include <knowledge|code|all>` (default `all`); map into
   `Command::Graph { scope, options }`.
5. `crates/gwiki/src/commands/mod.rs` — dispatch `graph::execute(scope, options)`.
6. `crates/gwiki/src/commands/graph.rs` — `execute` applies `retain_include` after
   `load_wiki_graph_facts`, then delegates to a testable `graph_outcome(root,
   output_scope, facts, export_options, options)`:
   - `--stdout`: skip artifact exports entirely; payload
     `{"command":"graph","scope":…,"graph": GraphExport}`.
   - default: byte-identical artifact behavior to today.
7. `crates/gwiki/src/exports/graph.rs` — widen `graph_export_error` to
   `pub(crate)` so the command reuses the shared typed-error conversion
   (`GraphAnalyticsError` → `WikiError::InvalidInput`).

### Acceptance mapping

- 1.1.1 `gwiki graph --stdout` prints envelope, no artifacts → symbol
  `gwiki::commands::graph::execute` + test `graph_stdout_emits_export_envelope_without_artifacts`.
- 1.1.2 `retain_include` filters with analytics on filtered set → test
  `crates/gwiki/src/graph/mod.rs::retain_include_knowledge_drops_code_edges`.
- 1.1.3 default stays artifact-writing → test
  `graph_default_writes_artifacts_regression` in `crates/gwiki/src/commands/graph.rs`.

### TDD evidence

- **Red:** wrote `retain_include_knowledge_drops_code_edges`,
  `retain_include_code_retains_code_documents_and_edges`,
  `retain_include_all_is_noop` (graph/mod.rs),
  `graph_stdout_emits_export_envelope_without_artifacts`,
  `graph_default_writes_artifacts_regression` (commands/graph.rs), and
  `graph_cli_maps_to_command_options` (main_tests.rs) before implementation.
  `GOBBY_TEST_PROTECT=1 cargo test -p gobby-wiki retain_include` failed to
  compile with E0433/E0599 (`GraphInclude` undeclared, `retain_include`
  missing) — 11 errors.
- **Green:** after minimal implementation, same command: 3 passed. Then
  `GOBBY_TEST_PROTECT=1 cargo test -p gobby-wiki graph`:
  `graph_stdout_emits_export_envelope_without_artifacts`,
  `graph_default_writes_artifacts_regression`, and
  `graph_cli_maps_to_command_options` all ok.
- **Refactor/final green:** `cargo fmt -p gobby-wiki`,
  `cargo clippy -p gobby-wiki --all-targets` (clean), then full crate
  `GOBBY_TEST_PROTECT=1 cargo test -p gobby-wiki`: 805 lib tests + all
  integration suites passed, 0 failed (includes pinned-contract conformance).
- **Test-quality audit:** `uv run gobby test-quality audit crates/gwiki/src/graph/mod.rs
  crates/gwiki/src/commands/graph.rs crates/gwiki/src/main_tests.rs
  crates/gwiki/tests/cli_contract.rs tests/test_cli_contracts.py --baseline
  .gobby/test-quality-baseline.json --fail-on-new --min-severity high` →
  5 files, 53 tests, 0 issues, 0 new.

### Additional work pulled in

- CLI contract v10 → v11 (4-place update): `contract.rs` graph entry gains
  `--stdout` + `--include` flags and the `graph` output key; regenerated
  `crates/gwiki/contract/gwiki.contract.json` and vendored
  `tests/contracts/gwiki.contract.json`; bumped version assertions in
  `crates/gwiki/tests/cli_contract.rs` and `tests/test_cli_contracts.py`;
  documented in `docs/contracts/gwiki-cli.md`.
- Pre-existing failure owned and fixed:
  `tests/test_cli_contracts.py::test_gwiki_gateway_argv_conforms_to_vendored_contract`
  still asserted `health` argv omits `--project`, stale since commit
  9b8bab427 (#15931) deliberately restored scoped wiki health. Removed the
  stale special case; all 9 contract tests pass.
- Release binary rebuilt and reinstalled to `~/.gobby/bin/gwiki`
  (contract_version 11 confirmed via installed binary).

### End-to-end verification

Live vault, installed binary: `gwiki graph --stdout --include knowledge` emits
the envelope with `imports/calls/callers` empty and only knowledge-scope
documents (26 `code/` nodes present are resolved wikilink targets of retained
knowledge pages, re-materialized by `export_graph`'s no-dangling-edges
placeholder invariant); `--include code` drops all knowledge docs;
`--stdout` writes no artifacts. Zero code-edge classes in `all` scope match
the pre-change artifact (vault currently has no code_edges) — filter behavior
with code edges present is covered by unit tests.

**Status:** done — pending close
