# Epic #17744 Work Log

Wiki Obsidian panel epic. One entry per leaf task, appended as tasks are worked.

## #17751 — Add `--stdout` and `--include` to `gwiki graph`

**Status:** closed — commit ad0e2e7b9, validation passed
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

## #17752 — Add `gwiki pages` listing + outputs read allowlist

**Status:** implemented — validation green, closing with this commit
**Session:** #8116 (274e513b)
**Plan section:** `wiki-obsidian-panel` 1.2 (backend, Rust)

### Plan

The UI file tree needs a lightweight listing that avoids the multi-MB graph
payload. Two surfaces: a DB-backed page listing and an outputs read allowlist.

1. `crates/gwiki/src/commands/pages.rs` (new)
   - `execute(selection, prefix)` mirrors `graph::execute`:
     `resolve_selection_context` → `database_url_for("gwiki pages")` →
     `gobby_core::postgres::connect_readonly`.
   - `load_page_entries`: `SELECT path, title, frontmatter, content_hash,
     updated_at FROM gwiki_documents WHERE scope_kind=$1 AND scope_id=$2 ORDER
     BY path` (verified: `updated_at TIMESTAMPTZ` exists in `setup.rs`;
     TIMESTAMPTZ → `SystemTime` natively, then
     `chrono::DateTime::<Utc>::from(..).to_rfc3339()` — chrono is already a
     dep).
   - `frontmatter_tags`: defensive extraction from frontmatter JSONB — accepts
     array-of-strings or single string. **Known gap:** `upsert_document`
     currently writes `frontmatter = json!({})`, so tags are `[]` until the
     indexer populates the column; filed as an epic leaf task (indexer +
     backfill are outside this task's targets).
   - `filter_by_prefix`: pure `path.starts_with(prefix)` retain (unit-testable
     without Postgres; rows are tiny without `body`).
   - `collect_output_entries`: recursive walk of `<root>/outputs` for `*.md`
     only (exports' `.json` siblings excluded), path/size/modified (RFC3339),
     sorted by path, missing dir → empty.
   - Envelope: `{"command":"pages","scope":…,"pages":[…],"outputs":[…]}` via
     `scoped_outcome`.
2. `crates/gwiki/src/commands/read.rs`
   - `is_readable_wiki_path`: add `["outputs", ..]` arm; update degradation
     message. Writes untouched (1.3 owns write confinement).
   - `content_hash: Option<String>` on `ReadOutput` + `content_hash: String`
     on `ReadFoundContent`, computed with `gobby_core::indexing::content_hash`
     over full file bytes — same fn the indexer uses, so the read baseline
     matches the DB row for unchanged files (conditional-write contract
     for 1.3/3.2).
3. `crates/gwiki/src/indexer.rs`: widen `is_indexable_vault_path` to
   `pub(crate)` so the read test can assert outputs stay unindexed (allowlist
   is `code/**`, `knowledge/**`, `raw/INDEX.md` — outputs excluded by design).
4. `crates/gwiki/src/api.rs`: `Command::Pages { scope, prefix: Option<String> }`.
5. `crates/gwiki/src/main.rs`: `Pages(PagesArgs)` with `--prefix`; mapping
   test in `main_tests.rs`.
6. `crates/gwiki/src/commands/mod.rs`: `mod pages;` + dispatch.
7. Contract v11 → v12 (4-place): `contract.rs` gains `pages` entry
   (`--prefix`, keys pages/outputs/path/title/tags/content_hash/updated_at/
   size/modified) and `read` gains `content_hash` key; regenerate
   `crates/gwiki/contract/gwiki.contract.json` + vendored
   `tests/contracts/gwiki.contract.json`; bump both version assertions;
   document in `docs/contracts/gwiki-cli.md`. Rebuild + reinstall
   `~/.gobby/bin/gwiki`.

### Acceptance mapping

- 1.2.1 pages listing with path/title/tags/content_hash + separate outputs
  array → `crates/gwiki/src/commands/pages.rs` (file) + envelope test.
- 1.2.2 `--prefix code/` restricts listing → test
  `crates/gwiki/src/commands/pages.rs::prefix_filters_listing`.
- 1.2.3 outputs readable via `gwiki read`, still unindexed → test
  `crates/gwiki/src/commands/read.rs::outputs_paths_are_readable`.

### TDD evidence

- **Red:** wrote `prefix_filters_listing`,
  `frontmatter_tags_accepts_arrays_and_strings`,
  `collect_output_entries_lists_markdown_reports`,
  `collect_output_entries_handles_missing_directory`,
  `pages_outcome_emits_envelope` (commands/pages.rs),
  `outputs_paths_are_readable` + full-file-hash assertion in
  `read_path_caps_content_and_marks_truncated` (commands/read.rs), and
  `pages_cli_maps_to_command` (main_tests.rs) before implementation.
  `GOBBY_TEST_PROTECT=1 cargo test -p gobby-wiki pages` failed to compile:
  15 lib errors (E0422/E0425/E0603/E0609 — `PageEntry`, `filter_by_prefix`,
  `frontmatter_tags`, `collect_output_entries`, `pages_outcome`,
  `content_hash` field, `is_indexable_vault_path` private) + 4 bin errors
  (`CliCommand::Pages`, `Command::Pages` missing).
- **Green:** after implementation, same command: all 5 pages tests ok;
  `cargo test -p gobby-wiki read`: 25 passed (one fix during green:
  `outputs_paths_are_readable` needed the existing `READ_TEST_ENV_LOCK`
  guard — the truncation test mutates `GWIKI_READ_MAX_BYTES` process-wide);
  `--bin gwiki pages_cli`: ok.
- **Refactor/final green:** `cargo fmt -p gobby-wiki`,
  `cargo clippy -p gobby-wiki --all-targets` (clean). Full crate suite run in
  an isolated worktree at HEAD + only this task's diff (see "Concurrent
  session" below): 811 lib tests + all integration suites, 0 failed.
  `GOBBY_TEST_PROTECT=1 uv run pytest tests/test_cli_contracts.py -v`:
  9 passed.
- **Test-quality audit:** `uv run gobby test-quality audit
  crates/gwiki/src/commands/pages.rs crates/gwiki/src/commands/read.rs
  crates/gwiki/src/main_tests.rs tests/test_cli_contracts.py --baseline
  .gobby/test-quality-baseline.json --fail-on-new --min-severity high` →
  40 tests, 0 issues, 0 new.

### Additional work pulled in

- CLI contract v11 → v12 (4-place update): `contract.rs` gains the `pages`
  entry and `read` gains the `content_hash` output key; regenerated pinned +
  vendored contract JSONs; bumped version assertions in
  `crates/gwiki/tests/cli_contract.rs` and `tests/test_cli_contracts.py`;
  documented in `docs/contracts/gwiki-cli.md`. Also fixed the doc's stale
  `contract_version: 10` header line (missed in the v11 bump).
- `read_markdown_prefix` refactored to `markdown_prefix(bytes, …)` — the file
  is now read once in full (needed for the hash baseline), truncation logic
  unchanged.
- Filed #17805: `upsert_document` hardcodes `frontmatter = json!({})`, so
  `gwiki pages` tags stay `[]` until the indexer persists parsed frontmatter
  (+ backfill for unchanged rows). Extraction here is defensive
  (array-of-strings or single string) and starts working once the column is
  populated.

### Concurrent session note

`upkeep::tests::upkeep_near_duplicate_hit_chooses_update_over_create` fails in
the shared working tree. Verified not mine: it passes in an isolated worktree
at clean HEAD, and still passes with only this task's diff applied. The
failure is introduced by another active session's uncommitted
`crates/gwiki/src/compile/*` changes (topic/checkpoint validation); their
close-time validation gate will catch it. Release binary was therefore built
from the isolated worktree (HEAD + this diff only) and installed to
`~/.gobby/bin/gwiki` (sha256 d8e2787b…, contract_version 12 confirmed via the
installed binary).

### End-to-end verification

Live vault, installed binary: `gwiki pages` lists 3,623 indexed pages
(path/title/tags/content_hash/updated_at, no bodies) + 29 `outputs/**`
markdown reports (path/size/modified). `--prefix code/` returns 3,232 pages,
all `code/`-prefixed. `gwiki read --path outputs/GRAPH_REPORT.md` returns
`status: found` with `content_hash` equal to the sha256 of the full 616,527-byte
file. Listing hash == read hash for an unchanged indexed page
(`knowledge/concepts/action.md`), proving the shared revision baseline.
