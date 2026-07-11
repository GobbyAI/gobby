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

## #17754 — Python read surfaces for graph and pages routes

**Status:** closed — see commit on task
**Session:** #8116 (274e513b)
**Plan section:** `wiki-obsidian-panel` 1.4 (backend, Python)

### Plan

Expose the new `gwiki graph --stdout --include` and `gwiki pages --prefix`
surfaces (landed in #17751/#17752, installed contract v12 verified) through the
daemon's gateway and HTTP routes, plus gzip for the large graph payloads.

1. `src/gobby/gwiki_gateway.py` — two read methods following the existing
   `search`/`read` pattern (both interactive-timeout, scope args appended by
   `_run_json`):
   - `async def graph(self, *, include: str = "all")` →
     `self._run_json("graph", ["graph", "--stdout", "--include", include])`
   - `async def pages(self, *, prefix: str | None = None)` →
     `["pages"]` + optional `["--prefix", prefix]` → `self._run_json("pages", …)`
2. `src/gobby/servers/routes/wiki.py` — two GET routes on the existing
   `create_wiki_router`, delegating via the existing `_read` helper (which
   resolves scope and maps gateway errors to 502/503):
   - `GET /api/wiki/graph?project=&topic=&include=all|knowledge|code` —
     validate `include` against the enum (mirroring `_normalize_ai`'s
     `HTTPException(400, "include must be one of all, code, knowledge")`
     envelope style used by compile's kind/ai validation).
   - `GET /api/wiki/pages?project=&topic=&prefix=` — pass-through `prefix`.
3. `src/gobby/servers/app_factory.py` — `app.add_middleware(GZipMiddleware,
   minimum_size=1024)`. Note: the task targets name `_app_routes.py`, but that
   module only registers routers; the FastAPI app (and every other middleware)
   is constructed in `app_factory.py:create_app`, so the middleware goes there.
4. `tests/servers/routes/test_wiki_routes.py` — extend `FakeGateway` with
   `graph`/`pages`; add `test_graph_route_include_validation` (valid include →
   200 envelope + gateway call recorded; invalid → 400 envelope, no gateway
   constructed), `test_pages_route_passes_prefix_and_scope`, and
   `test_gzip_enabled` (full `create_http_server(config=DaemonConfig()).app`
   with the middleware stack; auth is disabled unless credentials are
   configured, so requests pass; a >1 KiB fake search response with
   `Accept-Encoding: gzip` must come back `content-encoding: gzip`, and
   `identity` must not).

### Acceptance mapping

- 1.4.1 gateway `graph()`/`pages()` + scoped routes → symbol
  `gobby.gwiki_gateway.GwikiGateway.graph`; route tests exercise
  project/topic scope pass-through.
- 1.4.2 include filtering + 400 on bad value → test
  `tests/servers/routes/test_wiki_routes.py::test_graph_route_include_validation`.
- 1.4.3 gzip when client sends `Accept-Encoding: gzip` → test
  `tests/servers/routes/test_wiki_routes.py::test_gzip_enabled`.

### TDD evidence

- **Red:** wrote `test_graph_route_include_validation`,
  `test_pages_route_passes_prefix_and_scope`, `test_gzip_enabled` (plus
  `FakeGateway.graph`/`pages`) before implementation.
  `GOBBY_TEST_PROTECT=1 uv run pytest tests/servers/routes/test_wiki_routes.py
  -k "graph_route_include or pages_route_passes or gzip_enabled" -v` →
  3 failed: both new routes 404, and `content-encoding` header absent.
- **Green:** after gateway methods + routes + `GZipMiddleware`, full file:
  23 passed.
- **Refactor/final green:** added gateway argv unit tests
  (`test_graph_builds_stdout_include_argv`, `test_pages_passes_optional_prefix`
  in `tests/test_gwiki_gateway.py`), registered `graph`/`pages` in
  `test_gateway_exposes_expected_methods` and in the contract-conformance call
  list (`tests/test_cli_contracts.py::test_gwiki_gateway_argv_conforms_to_vendored_contract`).
  `GOBBY_TEST_PROTECT=1 uv run pytest tests/test_gwiki_gateway.py
  tests/test_cli_contracts.py tests/servers/routes/test_wiki_routes.py -q` →
  59 passed. App-factory/auth suites through the full middleware stack
  (`test_app_factory_ui_modes.py`, `test_app_factory_production_ui.py`,
  `test_app_factory_vite_proxy.py`, `test_auth_routes.py`) → 20 passed.
  `ruff format`/`ruff check` clean on all six touched files; `mypy` clean on
  the three touched source files.
- **Test-quality audit:** `uv run gobby test-quality audit
  tests/servers/routes/test_wiki_routes.py tests/test_gwiki_gateway.py
  tests/test_cli_contracts.py --baseline .gobby/test-quality-baseline.json
  --fail-on-new --min-severity high` → 0 new issues.

### Implementation notes

- Middleware landed in `app_factory.py:create_app` (the task's stale
  `_app_routes.py` target only registers routers). Added innermost
  (`minimum_size=1024`); installed starlette 1.3.1 excludes
  `text/event-stream` from gzip via `DEFAULT_EXCLUDED_CONTENT_TYPES`, so the
  MCP streamable-HTTP mount's SSE responses are untouched — verified in the
  vendored middleware source before adding app-wide.
- Include validation mirrors `_normalize_ai`: strip/lower then 400 with
  `include must be one of all, code, knowledge`; the gateway is never
  constructed on invalid input (asserted in the route test).
- E2E argv sanity against the installed gwiki v12 binary: both
  `gwiki pages --prefix code/ --project … --format json` and
  `gwiki graph --stdout --include knowledge --project … --format json`
  return well-formed envelopes for the live vault.

### Found during close: validator guard bug (#17807)

`close_task` was rejected twice with a contradiction: the validator's prose
said "All acceptance criteria are met … No gaps found" but
`validation_status=invalid`. Root cause (daemon log 12:57): the
`validate_leaf_task_with_llm` failure-precedence guard regex-matched the TDD
red evidence the validator echoed ("red (3 failed …)", "red (3 failing
tests …)") and demoted the valid verdict. The validation_criteria template
itself contains "Red evidence: failing test output captured before
implementation", so every TDD-required task whose validator quotes it is
exposed. Fixed under bug #17807 (own commit): four TDD red-evidence strippers
added to `_searchable_feedback` in `src/gobby/tasks/_validation_feedback.py`
(red parenthetical, red/TDD-evidence window with contrastive guard,
failure-before-implementation proximity, failed-as-expected idiom), red→green
regression tests in
`tests/mcp_proxy/tools/tasks/test_lifecycle_validation_feedback.py`
(5 stripped shapes incl. both observed #17754 messages verbatim-shaped, plus
2 preserved genuine admissions). 87 guard tests + 248 task-tools tests +
155 validation tests pass; ruff/mypy clean; audit 0 new.

## #17753 — Add `gwiki page write|delete` with vault confinement (P1, plan §1.3)

### Plan

Rust feature in `crates/gwiki` (skills loaded: rust, test-driven-development,
code-index). Vault mutation lands in the CLI where scope-root resolution and
path normalization already live; CLI/MCP/HTTP mirroring is deferred to #17755.

Files:
- `crates/gwiki/src/commands/paths.rs` (new): hoisted
  `normalize_requested_path` returning `Result<PathBuf, PathViolation>`
  (`Absolute | Escape | Empty`); callers format their own messages so the
  read command's existing strings stay byte-identical.
- `crates/gwiki/src/commands/read.rs`: consume the shared helper, map
  `PathViolation` → `ReadDegradation::invalid_request` with today's messages.
- `crates/gwiki/src/commands/page.rs` (new): `execute_write` /
  `execute_delete` thin wrappers (scope resolve + stdin read) over pure
  `write_page` / `delete_page` cores that unit tests target directly with
  temp vault roots. Confinement: shared normalization; `.md` extension
  required; first component must be `knowledge` with a nested tail;
  file-symlink targets rejected; canonicalized deepest-existing ancestor must
  resolve under canonical root AND still under `knowledge/` before parent
  dirs are created (then re-verified) — closes both symlink-out-of-vault and
  knowledge→elsewhere-inside-vault redirects. Preconditions: `--mode create`
  errors `already_exists` when the file exists and rejects `--expected-hash`
  as `invalid_input` (its precondition is nonexistence per the plan);
  `--mode upsert` with `--expected-hash` compares lowercase-hex SHA-256 of
  on-disk bytes (`gobby_core::indexing::file_content_hash`, same hash as the
  read revision baseline) and returns `precondition_failed` leaving the file
  untouched — including when the page does not exist. Content is written
  verbatim from stdin (UTF-8; frontmatter round-trips untouched). Payloads:
  `{"command":"page-write",scope,path,created,bytes,content_hash,
  changed_paths}` and `{"command":"page-delete",scope,path,changed_paths}`
  via `scoped_outcome`. Delete of a missing page → `not_found` envelope; DB
  row pruning stays with the incremental indexer's `IndexEvent::Deleted`
  path (`crates/gwiki/src/indexer.rs:116`), which `changed_paths` triggers.
- `crates/gwiki/src/api.rs`: `Command::PageWrite { scope, path, mode,
  expected_hash }`, `Command::PageDelete { scope, path }`,
  `pub enum PageWriteMode { Upsert, Create }` (stdin is read at execute time,
  not parse time, so parse tests never block).
- `crates/gwiki/src/commands/mod.rs`: `mod page; mod paths;` + dispatch arms.
- `crates/gwiki/src/main.rs`: `Page(PageArgs)` with nested
  `PageSubcommand::{Write,Delete}`; `--path` required, `--mode` ValueEnum
  (upsert default), `--expected-hash`; error plumbing for the two new
  variants.
- `crates/gwiki/src/error.rs`: `AlreadyExists` (`already_exists`) and
  `PreconditionFailed` (`precondition_failed`) variants; exit code 2.
- Contract v12→v13 (4-place update): `contract.rs` (version, two new
  entries named `page write` / `page delete` — space-separated names denote
  nested subcommands — plus the two error codes), pinned
  `crates/gwiki/contract/gwiki.contract.json`, vendored
  `tests/contracts/gwiki.contract.json`, version asserts in
  `crates/gwiki/tests/cli_contract.rs` and `tests/test_cli_contracts.py`;
  `docs/contracts/gwiki-cli.md` v13 note.
- `crates/gwiki/tests/cli_parse.rs`: dedicated round-trip test spawning the
  binary with piped stdin: write then delete in a temp topic scope.

Acceptance mapping: 1.3.1 write-upsert+changed_paths (page.rs +
`write_upserts_knowledge_page_from_stdin_content`); 1.3.2
`write_rejects_confinement_violations`; 1.3.3
`create_mode_conflicts_on_existing`; 1.3.4
`delete_removes_page_and_emits_changed_paths_for_reindex_prune`; 1.3.5
`write_precondition_hash_mismatch`.

Shared-tree note: another session holds uncommitted changes incl. a 215-line
deletion in `crates/gwiki/src/support/env.rs` (compiles as a whole with its
gcore counterpart). Tests run in the shared tree; the release binary is
built and reinstalled from an isolated worktree at my commit so the
installed gwiki reflects committed state only.

Validation: targeted `cargo test -p gobby-wiki`, `cargo clippy -p
gobby-wiki`, `cargo fmt -p gobby-wiki -- --check`, focused pytest for the
vendored-contract test, release build + reinstall + `gwiki contract` v13
sanity, test-quality audit (Rust paths → expect unsupported-language warning
paired with the targeted cargo test runs).

### #17753 Implementation notes and evidence

TDD evidence:
- Red: `cargo test -p gobby-wiki --lib commands::page` — 6 failed (all new
  page tests panicking on `todo!()` stubs: upsert, confinement, symlink
  escapes, create-conflict, precondition-hash, delete/changed_paths),
  5 passed (pre-existing `commands::pages` tests in the filter).
- Green (minimal impl): same command — 11 passed / 0 failed.
- Refactor/final green: `cargo test -p gobby-wiki` — 821 lib + all
  integration suites 0 failed, including `--test cli_parse`
  (`page_write_and_delete_round_trip_via_stdin` spawning the real binary
  with piped stdin) and `--test cli_contract` (builder ↔ pinned JSON parity
  at v13). `cargo clippy -p gobby-wiki` clean, `cargo fmt -p gobby-wiki --
  --check` clean. `GOBBY_TEST_PROTECT=1 uv run pytest
  tests/test_cli_contracts.py -q` → 9 passed.
- Test-quality audit (page.rs, paths.rs, compile/tests.rs, cli_parse.rs):
  37 tests scanned, 0 issues, 0 new ≥ high.

Notes:
- `AlreadyExists` carries `resource`/`id` mirroring `NotFound` (not the
  planned bare `path`) so the Display arm reads uniformly.
- gcode's feature-catalog handler map (`resolve_gwiki_handler` in
  `crates/gcode/src/commands/codewiki/build_parts/features.rs`) hardcodes
  every gwiki contract command; added `page write`/`page delete` arms
  (session #7921 flagged this via P2P — same miss as `pages` in #17752).
  `cargo test -p gobby-code --lib features` → 6 passed.
- Cross-session coordination: acked #7921 (they leave features.rs alone);
  they hold uncommitted gcode/gcore changes — none of their paths staged.

### Found during #17753: two pre-existing failures at clean HEAD

**#17809 — upkeep near-duplicate merge broken by #17804 validator.**
`upkeep::tests::upkeep_near_duplicate_hit_chooses_update_over_create` failed
("failed" vs "updated") at clean HEAD (verified in a pristine worktree).
Commit 72c569e added `validate_existing_target_identity` requiring the
existing target's frontmatter title to equal the compile topic — but
upkeep's near-duplicate disposition (#17727) deliberately merges a cluster
into a semantically-matched page with a different title; key-match updates
with case-variant titles were exposed too (keys canonicalize
case-insensitively, the validator compared case-sensitively). Fix: new
`WikiCompileOptions.allow_target_identity_mismatch` (default false) guards
both validator call sites in `crates/gwiki/src/compile/mod.rs`; upkeep's
`compile_cluster` sets it whenever it resolved an Update disposition —
upkeep's disposition resolution is the identity decision; interactive
`gwiki compile --target` keeps full #17804 protection. New regression test
`allow_target_identity_mismatch_permits_upkeep_merge_into_existing_page`
plus the previously-failing upkeep test now green; #17804's protection
tests untouched and green.

**#17810 — vendored gcode contract drift.**
`test_vendored_cli_contract_matches_real_cli[gcode]` failed at HEAD: commit
c92e52613 (#17532) added `--max-workers` to the pinned
`crates/gcode/contract/gcode.contract.json` without syncing the vendored
`tests/contracts/gcode.contract.json`. Synced the vendored copy (pinned
file is source of truth); session #7921 notified via P2P so they don't
double-fix. All 9 `tests/test_cli_contracts.py` cases pass.

## #17755 — Python write surfaces and MCP parity (plan §1.5)

### Plan

Consume the gwiki v13 `page write|delete` surface (#17753) from the Python
daemon: gateway argv builders with stdin threading, HTTP routes with
precise error mapping, coordinator reindex wiring, and MCP parity tools.

1. **Gateway** (`src/gobby/gwiki_gateway.py`):
   - `_run_json`/`_run_command` gain `stdin_data: bytes | None = None`.
     When set, the subprocess is created with `stdin=PIPE` and output is
     collected via `proc.communicate(input=stdin_data)` under the existing
     `wait_for` timeout (partial-stream collection is skipped for stdin
     runs; timeout returns the streamless timeout envelope, same as the
     existing non-readable-pipe branch).
   - `write_page(*, path, content, mode="upsert", expected_hash=None)` →
     argv `page write --path <p> --mode <m> [--expected-hash <h>]`,
     content via stdin. `expected_hash` is appended verbatim whenever set —
     the precondition is not droppable at this boundary. Mode validated
     in-gateway (`upsert|create`) so callers get 400/validation errors
     instead of a clap parse failure surfacing as 502.
   - `delete_page(*, path)` → argv `page delete --path <p>`.
   - Both command names join `SERIALIZED_WRITE_COMMANDS` (vault mutations
     serialize per vault like every other write).
2. **Coordinator** (`src/gobby/wiki/update_coordinator.py`): add
   `"page-write"`, `"page-delete"` (gwiki payload command values) to
   `EXPLICIT_WRITE_COMMANDS` so `handle_write_result` triggers reindex off
   their `changed_paths`.
3. **Routes** (`src/gobby/servers/routes/wiki.py`):
   - `POST /api/wiki/write` body `{path, content, mode?="upsert",
     expected_hash?}`; `POST /api/wiki/delete` body `{path}`; both via the
     existing `_write_call` helper (response carries `index_handoff`,
     returns only after reindex).
   - `_map_gateway_errors`/`_write_call` gain an optional `command_status`
     resolver; page routes map gwiki error payload codes
     `already_exists→409`, `not_found→404`, `precondition_failed→412`,
     everything else keeps 502/503.
4. **MCP** (`src/gobby/mcp_proxy/tools/wiki.py`): `wiki_write_page`,
   `wiki_delete_page` via the existing `write_call` helper, interactive
   timeout; registry description updated; `create_wiki_registry` docstring
   records the deliberate absence of a `wiki_graph` tool (multi-MB graph
   payloads poison agent context; agents use `gwiki graph` artifacts and
   graph-context packs).

TDD (required): red tests first in `tests/test_gwiki_gateway.py` (argv +
stdin threading for both commands), `tests/servers/routes/test_wiki_routes.py`
(`test_write_awaits_reindex`, `test_write_delete_error_mapping` — the 412
case runs a real `GwikiGateway` subclass with `_run_command` stubbed to
capture argv, proving the stale hash reaches the gwiki argv through the
reindex-backed route path), and `tests/mcp_proxy/tools/test_wiki.py`
(tools registered, coordinator delegation, error envelope surfaced).

### Implementation notes (#17755)

- Gateway: `write_page`/`delete_page` on `GwikiGateway`; `_run_json` and
  `_run_command` thread `stdin_data`; stdin-fed runs use
  `proc.communicate(input=...)` (no partial-output timeout collection —
  same envelope as the existing non-readable-pipe branch). New module
  helpers `PAGE_WRITE_MODES` + `normalize_page_write_mode` mirror
  `normalize_kind`. Both commands joined `SERIALIZED_WRITE_COMMANDS`.
- Coordinator: `page-write`/`page-delete` in `EXPLICIT_WRITE_COMMANDS`;
  reindex fires off their `changed_paths` (delete included — the
  `IndexEvent::Deleted` path prunes rows).
- Routes: `POST /api/wiki/write` + `POST /api/wiki/delete` through
  `_write_call`; `_map_gateway_errors` gained an optional `command_status`
  resolver; `_page_mutation_status` maps `already_exists→409`,
  `not_found→404`, `precondition_failed→412`, everything else stays
  502/503. Mode validated at the route (400 instead of a clap parse 502).
- MCP: `wiki_write_page`/`wiki_delete_page` (interactive timeout);
  mode normalized at the tool boundary; `create_wiki_registry` docstring
  records the deliberate no-`wiki_graph` rationale. Behavior fix folded
  in: `write_call` now skips the update coordinator when the gateway
  result is not ok — failed writes changed nothing, mirroring the HTTP
  path which raises before its coordinator step.
- Contract conformance: `test_gwiki_gateway_argv_conforms_to_vendored_contract`
  extended with both page commands (space-separated contract names now
  split like the gcode variant); `wiki_write_page→page write`,
  `wiki_delete_page→page delete` added to the MCP↔contract mapping.

### TDD evidence (#17755)

- Red: `GOBBY_TEST_PROTECT=1 uv run pytest <8 new tests>` → 8 failed
  (`AttributeError: no attribute 'write_page'`, routes 404, MCP
  `Tool 'wiki_write_page' not found`).
- Green (minimal): same command → 8 passed.
- Final green (post-refactor/format):
  `GOBBY_TEST_PROTECT=1 uv run pytest tests/test_gwiki_gateway.py
  tests/servers/routes/test_wiki_routes.py tests/mcp_proxy/tools/test_wiki.py
  tests/wiki/ tests/test_cli_contracts.py` → **180 passed**.
- Audit: `uv run gobby test-quality audit <4 test paths> --baseline
  .gobby/test-quality-baseline.json --fail-on-new --min-severity high` →
  0 issues, 0 new.
- `ruff format`/`ruff check` clean; `mypy` clean on all 4 touched src files.
- Live E2E against installed gwiki v13 (scratch vault): write persists
  stdin content verbatim (unicode), create→already_exists, stale
  hash→precondition_failed with file untouched, matching-hash upsert,
  delete prunes + second delete→not_found.

## #17756 — Detached pipeline runs: `background` flag on `POST /api/pipelines/run`

### Plan

Plan section 1.6. `POST /api/pipelines/run` awaits `executor.execute()`; a wiki-research
run holds the request open up to ~1h. Add a generic detached capability.

Investigation findings that shaped the design:
- Daemon startup already recovers orphans: `_recover_pipelines`
  (runner_lifecycle_subsystems.py) resumes `resume_on_restart` RUNNING executions, then
  marks the rest INTERRUPTED via storage `fail_stale_running_executions` — which is a
  misleadingly named alias of `interrupt_stale_running_executions`.
- Scoping gap: `runner.pipeline_execution_manager` is scoped to `runner.project_id`, and
  `init_orchestration` skips executor creation entirely when `project_id` is None. Orphans
  in other projects (served by lazily created per-project executors via
  `ServiceContainer.get_pipeline_executor`) are never swept — phantom RUNNING forever.
  The executor-level startup sweep closes exactly this gap.

Changes:
1. `src/gobby/workflows/pipeline_executor.py`
   - Extract `_create_execution_record(pipeline, inputs, session_id)` from the
     record-creation block of `execute()` (definition snapshot + `create_execution`).
   - `async start_detached(pipeline, inputs, project_id, session_id=None) -> PipelineExecution`:
     create record, mark RUNNING (so the caller sees RUNNING immediately, no scheduling race),
     `asyncio.create_task(self.execute(..., execution_id=...), name="pipeline-detached-<id>")`,
     retain in `self._detached_tasks: set[asyncio.Task]` with a discard + exception-logging
     done-callback (mirrors `CronExecutor._track_background_task`). `ApprovalRequired` from a
     detached run is a park, not an error — log info, not error. Track live execution ids in
     `self._detached_execution_ids` (discarded in the same callback) so the sweep can exclude
     in-flight runs without parsing task names.
   - `startup_sweep() -> int`: delegate to
     `execution_manager.fail_stale_running_executions(exclude_ids=live detached ids)`.
2. `src/gobby/storage/pipeline_executions.py` — make `fail_stale_running_executions` do what
   its name says (FAILED) instead of aliasing INTERRUPTED; both variants share parameterized
   `_mark_stale_running_executions(exclude_ids, *, status)` (same atomic SQL: fail RUNNING
   steps + daemon-restart note). No backward compat pre-0.5.0.
3. `src/gobby/runner_lifecycle_subsystems.py` — `_recover_pipelines` calls
   `interrupt_stale_running_executions` explicitly (daemon-startup resume+notify flow keeps
   INTERRUPTED semantics).
4. `src/gobby/app_context.py` — `get_pipeline_executor` runs `pe.startup_sweep()` after lazy
   creation (own try/except; sweep failure must not kill executor availability). Not wired
   into `init_orchestration`: the runner path must keep `resume_interrupted_pipelines` →
   INTERRUPTED ordering, and a FAILED sweep before resume would break `resume_on_restart`.
5. `src/gobby/servers/routes/pipelines.py` — `PipelineRunRequest.background: bool = False`;
   when true, `await executor.start_detached(...)` and return
   `202 {"status": "running", "execution_id", "pipeline_name"}`. Monitoring unchanged
   (`pipeline_event` WS broadcasts + `GET /api/pipelines/executions*`).

Tests (TDD):
- NEW `tests/workflows/test_pipeline_executor.py`: `test_start_detached_completes` (1.6.1),
  `test_startup_sweep_marks_orphans_failed` (1.6.3), sweep excludes in-flight detached runs,
  done-callback logs detached failures, ApprovalRequired parks without error log.
- `tests/servers/routes/test_pipelines.py`: `background: true` → 202 running envelope (1.6.2);
  detached start failure → 500.
- `tests/storage/test_pipeline_storage.py`: fail variant asserts FAILED; interrupt variant
  asserts INTERRUPTED (split the old alias expectations).
- `tests/test_app_context.py`: lazily created executor runs the startup sweep.

### Implementation notes (#17756)

- `PipelineExecutor` gained `_create_execution_record` (extracted from `execute()`'s
  record-creation block), `start_detached` (record → RUNNING → retained named task), and
  `startup_sweep`. Done-callback treats `ApprovalRequired` as an approval park (info log);
  real failures log as errors. Live detached execution ids tracked in
  `_detached_execution_ids` (same callback discards) so the sweep never fails an in-flight run.
- Storage: `interrupt_stale_running_executions` and `fail_stale_running_executions` now share
  `_mark_stale_running_executions(exclude_ids, *, status)`; the fail variant is real
  (FAILED), no longer an INTERRUPTED alias. `_recover_pipelines` renamed its call to the
  interrupt variant; `resume_interrupted_pipelines` docstring updated.
- `ServiceContainer.get_pipeline_executor` runs `startup_sweep()` on lazy creation in its own
  try/except. Route `POST /api/pipelines/run` with `background: true` returns
  `202 {"status": "running", "execution_id", "pipeline_name"}`.

### TDD evidence (#17756)

- Red: `GOBBY_TEST_PROTECT=1 uv run pytest tests/workflows/test_pipeline_executor.py
  tests/servers/routes/test_pipelines.py::TestRunPipeline
  tests/storage/test_pipeline_storage.py::TestFailStaleRunningExecutions
  tests/storage/test_pipeline_storage.py::TestInterruptStaleRunningExecutions
  "tests/test_app_context.py::TestGetPipelineExecutor::test_lazy_creation_runs_startup_sweep"
  "tests/test_app_context.py::TestGetPipelineExecutor::test_startup_sweep_failure_does_not_block_lazy_creation" -q`
  → 9 failed, 18 passed (AttributeError: no `start_detached`; FAILED-variant storage tests
  asserting INTERRUPTED alias; app_context sweep not called; route 202 missing).
- Green (same command post-implementation): 37 passed.
- Refactor/final green: full pipeline-surface regression —
  `GOBBY_TEST_PROTECT=1 uv run pytest tests/workflows/test_pipeline_executor*.py
  tests/workflows/test_pipeline_resume.py tests/workflows/test_pipeline_heartbeat.py
  tests/servers/routes/test_pipelines.py tests/storage/test_pipeline_storage.py
  tests/test_app_context.py tests/events/test_pipeline_integration.py
  tests/events/test_wake_wiring.py tests/mcp_proxy/tools/test_pipeline_resume.py -q`
  → **300 passed**.
- `uv run ruff format` + `ruff check` clean; `uv run mypy` clean on all six touched sources
  (one `no-any-return` fixed with a declared-type local, matching `execute()`'s idiom).
- `uv run gobby test-quality audit tests/workflows/test_pipeline_executor.py
  tests/servers/routes/test_pipelines.py tests/storage/test_pipeline_storage.py
  tests/test_app_context.py --baseline .gobby/test-quality-baseline.json --fail-on-new
  --min-severity high` → exit 0 (3 MEDIUM below gate: two `asyncio.sleep(0)` event-loop
  yields for done-callbacks, one delegation test complemented by real-SQL storage tests).

## #17757 — Codewiki refresh status endpoint (plan section 1.7)

### Investigation
- `CodewikiRefreshTrigger` (`src/gobby/code_index/codewiki_trigger.py`) already tracks all live state needed for a snapshot: `_pending_by_root` (debounced requests), `_flush_timers_by_root`, `_flush_tasks` (in-flight asyncio tasks), `_running_roots`. `_run_refresh` logs the `CodewikiRefreshResult` (or gateway error) and discards it — nothing is retained for later inspection.
- `_run_refresh` catches `CodewikiGatewayConstructionError` and `(GcodeGatewayError, GwikiGatewayError)`; `CancelledError` re-raises; unexpected exceptions propagate to `_flush_task_done` (logged there).
- Routes: `create_code_index_router` uses prefix `/api/code-index`; refresh is `@router.post("/codewiki/refresh", status_code=202)` and 503s when `server.services.codewiki_trigger` is missing. Status route mirrors that guard.
- Route handlers run on the daemon event loop — the same loop that mutates trigger state via `call_soon_threadsafe`/timers — so a read-only `status()` called directly from the handler is race-free; copies are returned to keep the snapshot immutable.

### Changes
1. Trigger `_run_refresh`: capture `started_at = datetime.now(UTC)`; on success or handled gateway error call `_record_last_run(request, started_at, result=... | error=...)` storing `self._last_run: dict[str, Any]` with `outcome` (success/error), `root_path`, `project_id`, `changed_count`, `indexed`, `error`, `started_at`/`finished_at` ISO timestamps. Cancellation records nothing.
2. Trigger `status()` (acceptance 1.7.1): returns `{"pending_roots": sorted(...), "running_roots": sorted(...), "active_flush_tasks": len(self._flush_tasks), "last_run": copy | None}`.
3. Route `GET /api/code-index/codewiki/status` (acceptance 1.7.2): 503 when trigger/status unavailable, else returns `trigger.status()`.

### Tests (red first)
- `tests/code_index/test_codewiki_trigger.py`: `test_status_reports_pending_and_running_state`, `test_status_records_last_run_success`, `test_status_records_last_run_error`, `test_status_endpoint_snapshot` (FastAPI app + router + real trigger after a full refresh cycle — acceptance test).
- `tests/servers/routes/test_code_index_routes.py`: `test_codewiki_status_requires_trigger` (503 when trigger is None).
- Command: `GOBBY_TEST_PROTECT=1 uv run pytest tests/code_index/test_codewiki_trigger.py tests/servers/routes/test_code_index_routes.py -v`

### Implementation notes (#17757)
- `CodewikiRefreshTrigger` gained `_last_run: dict[str, Any] | None`, a `status()` snapshot (`pending_roots`/`running_roots` sorted, `active_flush_tasks` count, copied `last_run`), and `_record_last_run(request, started_at, *, result|error)` invoked from `_run_refresh` on success and on both handled gateway-error paths. Cancellation re-raises without recording. Timestamps are `datetime.now(UTC).isoformat()` strings so the snapshot is JSON-safe end to end.
- `GET /api/code-index/codewiki/status` added to `create_code_index_router`, mirroring the refresh route's 503 guard (`getattr` trigger + callable `status`).
- Found-it-own-it fix: `tests/code_index/test_gcode_phase7_contract.py` had 2 pre-existing failures — commit `9f7311de0` ([gobby-#17712] bound FalkorDB socket I/O) replaced the `falkordb` crate with a hand-rolled redis-backed client (`connection: Connection` + `graph_name: String`, bounded connect/read/write timeouts) and updated `crates/gcore/tests/public_boundary.rs` but not this Python contract test. Re-pinned the facade contract on the new surface (timeout constants, `set_read_timeout`/`set_write_timeout`, `GRAPH.QUERY`, `from_config_with_timeouts`) and the lockfile assertions (`redis` in, `falkordb` out).

### TDD evidence (#17757)
- Red: `GOBBY_TEST_PROTECT=1 uv run pytest tests/code_index/test_codewiki_trigger.py tests/servers/routes/test_code_index_routes.py -v` → 6 failed as expected (4× `AttributeError: no attribute 'status'` / 404 on status route in trigger file, 2× 404 in routes file), 41 passed.
- Green: same command → 47 passed.
- Final: `GOBBY_TEST_PROTECT=1 uv run pytest tests/code_index/ tests/servers/routes/test_code_index_routes.py` → 251 passed (includes the 2 repaired phase7 contract tests).
- `uv run ruff format`/`check` clean on all 5 touched files; `uv run mypy src/gobby/code_index/codewiki_trigger.py src/gobby/servers/routes/code_index.py` → no issues.
- `uv run gobby test-quality audit tests/code_index/test_codewiki_trigger.py tests/servers/routes/test_code_index_routes.py tests/code_index/test_gcode_phase7_contract.py --baseline .gobby/test-quality-baseline.json --fail-on-new --min-severity high` → 51 tests scanned, 0 issues, exit 0.

## #17805 — Populate gwiki_documents.frontmatter at index time so `gwiki pages` tags work

### Plan (2026-07-10, session #8116)

**Investigation.** `PostgresWikiStore::upsert_document` (`crates/gwiki/src/store/postgres.rs:127`) hardcodes `frontmatter = json!({})`, so the JSONB column never carries page frontmatter and `gwiki pages` always shows `tags: []`. `parse_wiki_document` (`crates/gwiki/src/indexer.rs:291`) builds `WikiDocument { path, kind, title, content_hash, body }` without touching the existing frontmatter parser (`crates/gwiki/src/frontmatter.rs::parse_frontmatter`, which returns `WikiFrontmatter` with `as_json()` producing exactly the JSONB shape needed, unknown keys preserved). The incremental indexer (`index_vault`) skips `Unchanged` files by content hash, so already-indexed rows keep `{}` forever without a backfill path. `gwiki index` CLI surface is pinned three ways: `crates/gwiki/src/contract.rs` (contract_version 13), `crates/gwiki/contract/gwiki.contract.json` (pinned by `crates/gwiki/tests/cli_contract.rs`), and `tests/contracts/gwiki.contract.json` (Python `tests/test_cli_contracts.py` compares vendored vs workspace vs installed binary) — all three plus the installed `~/.gobby/bin/gwiki` must move together (same trap as the phase7 contract memory).

**Changes.**
1. `crates/gwiki/src/store/types.rs` — `WikiDocument` gains `pub frontmatter: serde_json::Value` (serde_json::Value is Eq, derives keep working).
2. `crates/gwiki/src/indexer.rs` — `parse_wiki_document` parses frontmatter from the body via `parse_frontmatter`, storing `metadata.as_json()`; malformed/unterminated frontmatter degrades to `json!({})` so one bad page cannot fail a vault index. `IndexOptions` gains `pub force: bool` (default false). `index_vault`'s `Unchanged` arm re-runs `index_file` (recording the truthful `Unchanged` ingestion event) when `force` is set — this is the backfill path.
3. `crates/gwiki/src/store/postgres.rs` — `upsert_document` writes `document.frontmatter` instead of the hardcoded `json!({})`.
4. `crates/gwiki/src/support/config.rs` — `index_options_from_config` sets `force: false` (config never forces; it is per-invocation).
5. CLI plumbing for `gwiki index --force`: `main.rs` `CliCommand::Index { force }`, `api.rs` `Command::Index { scope, force }`, `commands/mod.rs` dispatch, `commands/index.rs` `execute`/`index_resolved_scope_report` apply the flag; `index_resolved_scope` (refresh/sources callers) stays force=false.
6. Contract sync: `contract.rs` index command gains `FlagContract::switch("--force")`, contract_version 13→14; regenerate `crates/gwiki/contract/gwiki.contract.json`; copy to `tests/contracts/gwiki.contract.json`; rebuild + reinstall `~/.gobby/bin/gwiki`.
7. Test fixtures gaining the new field: `indexer.rs::seed_derived_rows`, `support/graph.rs` two `WikiDocument` literals, `indexer.rs:575` `IndexOptions` literal.

**Tests (TDD, in `crates/gwiki/src/indexer.rs` tests).**
- `indexed_documents_carry_parsed_frontmatter` — YAML tags land in `store.documents[..].frontmatter`.
- `documents_without_frontmatter_store_empty_object`.
- `malformed_frontmatter_falls_back_to_empty_object` — unterminated block still indexes.
- `force_reindexes_unchanged_documents` — second run without force skips (document_upserts unchanged); with force re-upserts and repopulates a blanked frontmatter.
- Red is a compile error (E0609/E0063 missing field) for the field tests plus the new-flag contract drift; documented as expected.

**Commands.** `cargo test -p gobby-wiki`, `cargo clippy -p gobby-wiki`, `cargo fmt -p gobby-wiki`, Python-side `GOBBY_TEST_PROTECT=1 uv run pytest tests/test_cli_contracts.py -v`, `cargo build --release -p gobby-wiki` + install to `~/.gobby/bin/gwiki`. Test-quality audit: Rust is unsupported by the audit CLI — evidence is the focused repo-native `cargo test -p gobby-wiki` run per the TDD skill's unsupported-language clause; the Python contract JSON change carries no new tests.

### Implementation notes (#17805)

- `WikiDocument` carries `frontmatter: serde_json::Value`; `parse_wiki_document` fills it via `parse_frontmatter(&body).map(|p| p.metadata.as_json())` with `{}` fallback for malformed blocks. `upsert_document` writes `document.frontmatter` to the JSONB param (the `json!({})` hardcode is gone). Memory store flows the field through unchanged.
- Backfill: `IndexOptions.force` re-runs `index_file` for `Unchanged` events while recording the truthful `Unchanged` ingestion event; exposed as `gwiki index --force` (clap → `Command::Index { scope, force }` → `execute(scope, run_options, force)`); `index_resolved_scope` (refresh/sources callers) stays non-forced; config resolution never forces.
- Contract sync (contract_version 13→14, index gains `--force` switch) needed FIVE synchronized surfaces: `contract.rs`, `crates/gwiki/tests/cli_contract.rs` (pins version), `crates/gwiki/contract/gwiki.contract.json`, `tests/contracts/gwiki.contract.json` (both regenerated from the built binary), `tests/test_cli_contracts.py::test_gwiki_contract_documents_daemon_parsed_keys` (pins version), plus reinstalling `~/.gobby/bin/gwiki` so the installed-binary contract source matches.

### TDD evidence (#17805)

- Red: `cargo test -p gobby-wiki indexer` → 7 compile errors, E0560 `IndexOptions` has no field `force` + E0609 no field `frontmatter` on `WikiDocument` (expected red for struct-field TDD).
- Green: `cargo test -p gobby-wiki --lib indexer` → 14 passed (4 new: `indexed_documents_carry_parsed_frontmatter`, `documents_without_frontmatter_store_empty_object`, `malformed_frontmatter_falls_back_to_empty_object`, `force_reindexes_unchanged_documents`).
- Final: `cargo test -p gobby-wiki` → all suites ok (825 lib + integration incl. repinned `cli_contract`); `cargo clippy -p gobby-wiki` clean; `cargo fmt -p gobby-wiki -- --check` clean; `GOBBY_TEST_PROTECT=1 uv run pytest tests/test_cli_contracts.py -q` → 9 passed (vendored == workspace == installed binary at v14); `uv run ruff check`/`format --check` clean on `tests/test_cli_contracts.py`.
- `uv run gobby test-quality audit tests/test_cli_contracts.py crates/gwiki/src/indexer.rs --baseline .gobby/test-quality-baseline.json --fail-on-new --min-severity high` → 22 tests scanned, 0 issues, exit 0.
- Live check: rebuilt release `gwiki`, installed to `~/.gobby/bin/gwiki`, ran `gwiki index --force` on the project vault (3623 documents, 4m40s, no degradations); `gwiki pages` now lists `tags: ["gwiki","compiled","entity"]` for `knowledge/concepts/gobby.md` (previously `[]` everywhere).

## #17758 — Wiki data layer + model rewrite (plan section 2.1)

### Plan

**Investigation summary**
- Backend contracts (all live in `src/gobby/servers/routes/wiki.py`): every wiki route wraps `gwiki` CLI JSON in `{ok, command, payload, stderr}` (`GwikiGateway._success_envelope`); command failures map to `{ok:false, status:"failed", payload, stderr, error:{type,returncode,message}}` with HTTP 409/404/412 for `already_exists`/`not_found`/`precondition_failed` on write/delete (`_PAGE_MUTATION_STATUS`), 502 otherwise.
- Live payload shapes captured from `~/.gobby/bin/gwiki --format json` (HTTP is auth-gated; gateway shells out to the same binary):
  - `pages`: `{command, scope, pages:[{path,title,tags,content_hash,updated_at}], outputs:[{path,size,modified}]}` — 3623 pages, roots `code/` (3232), `knowledge/` (390), `raw/` (1). recaps/_index/log not listed today; tree builder stays data-driven.
  - `graph` (also `wiki/outputs/graph.json`, 1846 nodes): `{command, degraded, degraded_sources, nodes:[{id,kind,scope_kind,scope_id,path,title}], edges:{links|imports|calls|callers|trust|audit → [{source,target,kind,raw_target?}]}, analytics:{bridges,centrality,communities,god_nodes,hotspots,unexpected_links}}`. Node kinds: wiki_page 328, source 337, citation 337, unresolved_target 835, code 8, document 1.
  - `read`: `{command, scope, status, requested, wiki_path, absolute_path, title, content, content_format, content_hash, byte_len, truncated, candidates, degradations}` — content includes raw frontmatter block.
  - `backlinks`: `{command, scope, page, backlinks:[{source_path,target_path,raw_target}]}` (shape from `crates/gwiki/src/commands/backlinks.rs`).
  - `ask`: `{command, scope, query, status, degraded, degraded_sources, hits, sources, code_citations:[{file,line?,symbol?}], evidence, prompt_token_budget, prompt_tokens_estimated, truncated, truncated_components, warnings, hint?, ai?, synthesis?}`; synthesis `{answer, model, citation_check:{status, checked_claims, unsupported_claims}}`, ai `{requested, requested_mode, route, status, model, error}` (from `crates/gwiki/tests/cli_contract.rs` representative outputs + live capture).
  - `status`: `{command, scope, status, runtime, daemon_url, services:{embeddings,falkordb,postgres,qdrant → {configured,...}}}`; `health`: `{command, scope, broken_links, duplicate_*, stale_*, uncited_sources, uncompiled_sources, page_confidence, ...}`.
- `POST /api/pipelines/run` body is `{name, inputs, project_id?, background}` (`PipelineRunRequest`); 202 on detached start.
- Frontend conventions: `useWiki.ts` exports `WikiEnvelope`/`WikiJson`/`WikiSourceRecord` and keeps status/health/sources + action helpers — 2.1 reuses its envelope types and its `scopeQuery` idiom. Tests: vitest (jsdom, globals) via `npm test` from `web/`; fixtures-as-TS established (`activity/__tests__/fixtures`). `js-yaml` + types already deps. Graph token mapping precedent: `KnowledgeGraph.tsx` `ENTITY_TYPE_COLOR_VARS` + `resolveCssVar`; deutan-safe state palette per `.impeccable.md` (info 250, warning 75, destructive 350, success = lightness-only 125; never hue-only).

**Changes**
1. `web/src/components/activity/wiki/WikiTabModel.ts` (new): `WikiMode`, `WikiPageMeta`, `WikiOutputMeta`, `WikiGraphNode/Edge/Payload`, `PageTreeNode`; `pageKindFromPath`, `breadcrumbSegments`, `codePathToSourcePath`; `buildPageTree(pages, outputs, rootFilter?)` grouping by path segments (folders-first, alphabetical); `buildNodeIndex(pages)` (path→meta + normalized title/alias→path) and `resolveWikilinkTarget(index, target)`; graph display tables `wikiNodeColorVar(kind)` (deutan-safe token vars) and `wikiNodeVal(degree)` = `2 + 3*sqrt(degree)` clamped.
2. `web/src/components/activity/wiki/WikiTabData.ts` (new): scope/query helpers + defensive `asRecord`/`fieldText`/`fieldNumber`/`fieldStringList` helpers; fetchers `fetchGraph`, `fetchPages`, `fetchPage`, `fetchBacklinks`, `fetchSearch`, `fetchAsk` (AbortSignal), `savePage` (412 → typed conflict result), `createPage`, `deletePage`, `launchResearch`; normalizers `normalizeGraph` (edges object → flat typed list), `normalizePages`, `normalizePage` (js-yaml frontmatter split), `normalizeAskAnswer` (answer, wikilink citations `{target,title,resolvedPath|null}`, grounding warnings), `normalizeBacklinks`, `summarizeWikiStatus` (successor to deleted `buildWikiSummary`; feeds 2.2 degraded banner).
3. `web/src/components/activity/wiki/__tests__/fixtures.ts` (new): envelope fixtures shaped from the live captures above + graph fixture subset carrying all six node kinds and links/trust/audit edges with `raw_target`.
4. `web/src/components/activity/wiki/__tests__/WikiTabModel.test.ts` (new): tree building (roots, nesting, outputs, rootFilter, sort), node-index + wikilink target resolution (path, path-sans-.md, title, alias, miss), path helpers (incl. `codePathToSourcePath` round-trips), color/val tables.
5. `web/src/components/activity/wiki/__tests__/WikiTabData.test.ts` (new): normalizers pinned by fixtures (graph flatten, pages split, frontmatter split, ask normalization incl. citations + grounding warnings, backlinks, status summary incl. degraded/unavailable), savePage 412 conflict normalization, launchResearch body, fetch URL/scope composition (mocked fetch).

**Test commands**
- Red/green/final: `cd web && npm test -- src/components/activity/wiki`
- Final validation: `cd web && npm run type-check && npx eslint src/components/activity/wiki --report-unused-disable-directives --max-warnings 0`
- Test-quality audit: `uv run gobby test-quality audit web/src/components/activity/wiki/__tests__ --baseline .gobby/test-quality-baseline.json --fail-on-new --min-severity high` (TS may be unsupported → pair with the focused vitest run above per TDD skill).

**Notes**
- Data layer is UI-less (no visual output), but `.impeccable.md` read and honored for the graph token tables (deutan-safe, never hue-only).
- Tree/citation resolution built on the lightweight `pages` listing; graph payload stays lazy (fetched only by graph view / unresolved-mentions consumers).
- 1,000-line source cap respected (model ~350, data ~430).

### Implementation notes (#17758)

- `WikiTabModel.ts` (250 lines): pure models + helpers exactly as planned. Tree builder is fully data-driven (no hardcoded vault roots) since the live `pages` payload currently omits `recaps/`, `_index.md`, and `log.md` — fixtures include them so the tree handles their return. Node index resolves exact path → path+`.md` → normalized title/alias. Graph tokens: wiki_page `--accent`, code `--color-info`, document `--color-success-foreground`, source `--color-warning-foreground`, citation `--color-review`, unresolved_target/fallback `--text-muted` (matches KnowledgeGraph precedent; never hue-only). `wikiNodeVal` = 2+3·√degree clamped to [2,20].
- `WikiTabData.ts` (622 lines): `asRecord`/`fieldText`/`fieldNumber`/`fieldStringList` defensive helpers; envelope transport mirrors `useWiki` (`detail.error.message`/`detail.stderr` extraction); `normalizeGraph` flattens the kind-keyed edges object; `normalizePage` splits frontmatter with js-yaml, malformed block degrades to `{}`; `normalizeAskAnswer` extracts deduped wikilink citations from the synthesis answer with optional resolver + grounding warnings from `citation_check.unsupported_claims`/`ai.error`/payload warnings; `summarizeWikiStatus` maps unconfigured services → degraded, gateway error → unavailable, plus health counts. `savePage` normalizes 412/409 (code from `detail.payload.code`) to a typed conflict; `launchResearch` posts `{name:'wiki-research', inputs, project_id, background:true}`.
- Both source files well under the 1,000-line cap.

### TDD evidence (#17758)

- **Red**: `cd web && npm test -- src/components/activity/wiki` → `Test Files 2 failed (2)` — `Failed to resolve import "../WikiTabModel"` (implementation absent).
- **Green**: same command after implementation → `Test Files 2 passed (2), Tests 53 passed (53)`.
- **Refactor/final green**: replaced two `.at(-1)` uses flagged by `tsc` (lib target); final `npm run type-check` clean, `npm test -- src/components/activity/wiki` → 53 passed, `npx eslint src/components/activity/wiki --max-warnings 0` clean.
- **Test-quality audit**: `uv run gobby test-quality audit web/src/components/activity/wiki/__tests__ --baseline .gobby/test-quality-baseline.json --fail-on-new --min-severity high` → 3 files, 44 tests, 0 issues, 0 new ≥ high.
- Fixtures shaped from live `gwiki pages/read/backlinks/ask/status/health --format json` captures (2026-07-10) and a `wiki/outputs/graph.json` subset carrying all six node kinds; synthesis ask fixture shaped from the representative output pinned in `crates/gwiki/tests/cli_contract.rs`.

## Task #17759 — Mode shell, toolbar, navigation, sources port (plan §2.2)

### Plan (#17759)

**Investigation summary**
- `WikiTab.tsx` is the intentional blank stub from b03431941 (renders null; tab id already registered). The same commit deleted the legacy sources-table UI and instructs agents to design fresh from the plan + `.impeccable.md`, never the old UI. `useWiki.ts` and `WikiSourceRemovalDialog.tsx` were kept for reuse.
- `ActivityPanel.tsx` owns `dirtyGuardValue` (a `useDirtyGuardController()`) and wraps tab content in `DirtyGuardProvider`; `useActivityPanel` wraps tab switches in `dirtyGuard.guardedRun`. Children register guards via `useDirtyGuard().registerDirtyGuard` (see `fields/useDetailDraft.ts`). WikiTab therefore consumes the AMBIENT `useDirtyGuard()` for its own mode/nav transitions — future P3 editor registers into the same context, and tests can wrap WikiTab in `DirtyGuardProvider` with a controlled guard.
- `case "memory"` passes `requestPanelOverride`/`releasePanelOverride` (optional props, default noop in MemoryTab); `case "wiki"` currently passes only `projectId`.
- `SegmentedControl` (`web/src/components/ui/SegmentedControl.tsx`): `{value, onChange, options, ariaLabel, size?, controlHeight?: 'sm'|'md', disabled?, className?}`.
- `ActivityPanelSearch`: `{value, onChange, placeholder, ariaLabel?}` memoized input with `activity-panel-search` class.
- `QuickMenu`: `{items: (action|separator)[], menuLabel, triggerLabel?, disabled?}` — kebab trigger built in; items `{label, icon?, destructive?, disabled?, onSelect}`.
- `useWiki(options)` returns `{status, health, sources, isLoading, error, refresh, search, read, attach, ingest, compileWiki, audit, checkHealth, removeSource}`; `removeSource({id, dry_run|yes, keep_asset})` drives the kept `WikiSourceRemovalDialog` (`{source, preview, isPreviewLoading, isConfirming, error, onCancel, onConfirm({keep_asset})}`).
- `summarizeWikiStatus(status, health, error?)` from 2.1 yields `{state: ready|degraded|unavailable, services, degradedServices, brokenLinks, stalePages, uncompiledSources}` — drives the banner + ask/research gating.
- No shared localStorage hook exists; tabs read/write inline with guards (SkillsTab/PipelinesTab/FilesTab precedent). No global ResizeObserver in jsdom — tests stub it (TaskTree.test.tsx precedent); `useChatInputNarrow` is the observer idiom.
- Repo bans Tailwind `md:` variants → container width via ResizeObserver (wide ≥560px).

**Changes**
1. `web/src/components/activity/wiki/WikiTabState.ts` (new, ~170): `WIKI_TAB_KEYS` persistence constants (`gobby:wiki-tab:{mode,topic,tree-width,split,last-page:wiki,last-page:code,graph}` + sessionStorage `gobby:wiki-tab:ask-history`); safe storage read/write helpers; `useWikiNav({guardedRun, onModeChange})` — in-memory history stack (cap 50) with `{current, openPage(path, opts), back(), forward(), canBack, canForward}`; `openPage` derives mode from path (`code/` prefix → code, else wiki), truncates forward history, and every transition runs through `guardedRun`.
2. `web/src/components/activity/wiki/WikiTabToolbar.tsx` (new, ~200): SegmentedControl (Wiki|Code|Ask|Research, `controlHeight="sm"`), `ActivityPanelSearch`, graph button (wiki+code only), kebab `QuickMenu` (New page, Quick open [disabled until P3 browse], Refresh index, Compile, Audit, Attach file, Ingest URL, Manage sources, Topic scope); exports `WikiDegradedBanner` (slim, info icon + text + services detail, never hue-only) rendered under the toolbar.
3. `web/src/components/activity/wiki/WikiTabActions.ts` (new, ~200): `useWikiTabActions({scope, wiki, onNavigate?})` orchestration hook over 2.1 fetchers + kept useWiki helpers — `savePageAndRefresh`, `createPageAndNavigate`, `deletePageAndNavigateBack`, `runCompile`, `runAudit`, `refreshIndex`, `attachFile`, `ingestUrl`, `launchResearchRun`; busy/status/error state with one aria-live status line consumed by the shell.
4. `web/src/components/activity/wiki/WikiSourcesManager.tsx` (new, ~220): fresh in-pane sources view (list + detail, search filter) opened from the kebab; remove flow = dry-run preview → `WikiSourceRemovalDialog` → confirm → refresh. Designed per `.impeccable.md` (no side-stripes, ghost buttons, state never hue-only).
5. `web/src/components/activity/WikiTab.tsx` (rewrite, ~240): four-mode shell — persisted mode/topic, ambient dirty-guarded mode switch + `useWikiNav`, last-page persistence per mode, `ResizeObserver` wide/narrow, `useWiki` + `summarizeWikiStatus` banner, ask/research composers disabled when unavailable, mode-body placeholders (P3–P5 land later), sources view toggle, `requestPanelOverride`/`releasePanelOverride` props (noop defaults).
6. `web/src/components/activity/ActivityPanel.tsx`: `case "wiki"` passes the override props (mirrors memory).
7. `web/src/components/activity/wiki/__tests__/WikiTab.shell.test.tsx` (new): mocked fetch envelopes; asserts four-mode control renders; mode persists to + restores from `gobby:wiki-tab:mode`; dirty guard blocks/allows mode switch and `openPage` nav (DirtyGuardProvider harness); degraded banner renders from degraded status fixture; ask composer disabled when unavailable; kebab opens sources manager.
8. `web/src/components/activity/wiki/__tests__/WikiTabState.test.ts` (new): useWikiNav history semantics (push/truncate-forward/cap-50/mode derivation) via renderHook; storage helpers tolerate quota/parse failures.

**Test commands**
- Red/green/final: `cd web && npm test -- src/components/activity/wiki`
- Final validation: `cd web && npm run type-check && npx eslint src/components/activity/wiki src/components/activity/WikiTab.tsx src/components/activity/ActivityPanel.tsx --report-unused-disable-directives --max-warnings 0`
- Test-quality audit: `uv run gobby test-quality audit web/src/components/activity/wiki/__tests__ --baseline .gobby/test-quality-baseline.json --fail-on-new --min-severity high`

**Notes**
- New page / Quick open kebab items are present but disabled with a "requires browse mode (P3)" title — §3.1/§3.2 own quick-open and create/edit UI; wiring them here would duplicate P3 acceptance work.
- Working tree carries another session's uncommitted auth changes — stage only wiki paths + ActivityPanel.tsx + this log.

### Implementation notes (#17759)

- `WikiTabState.ts` (196 lines): `WIKI_TAB_KEYS` (all seven localStorage keys + sessionStorage ask-history), guarded `readStoredValue`/`writeStoredValue` (best-effort, quota/private-mode safe), mode/topic/last-page wrappers, `modeForPath`, and `useWikiNav` — history stack capped at 50 with forward-truncation on new opens, same-page no-op, and every transition (open/back/forward) routed through the injected `guardedRun`. A `commit()` helper updates a state ref alongside `setState` (react-hooks/refs bans render-phase ref writes; commit is the only mutation path).
- `WikiTabToolbar.tsx` (183 lines): SegmentedControl (`ariaLabel="Wiki mode"`, `controlHeight="sm"`), `ActivityPanelSearch`, Graph button (wiki+code only), kebab QuickMenu (`triggerLabel="Wiki actions"`) — New page / Quick open present but disabled until §3.x; Refresh/Compile/Audit/Attach/Ingest disable when the gateway is unavailable; Manage sources / Topic scope always live. `WikiDegradedBanner` uses `bg-info-soft text-info` + info icon (state never hue-only), shows degraded services or offline message plus health counts.
- `WikiTabActions.ts` (231 lines): `useWikiTabActions({scope, wiki, onRefetch, onNavigate, onNavigateBack})` — one `run()` wrapper drives `{busy, message, error}` status; `savePageAndRefresh` returns the typed conflict result without refetching on 412/409; `createPageAndNavigate` → refetch + `onNavigate(path)`; `deletePageAndNavigateBack` → refetch + back; refresh/compile/audit/attach/ingest wrap the kept useWiki helpers (`ingest` takes `{urls: [url]}` — the request shape is `WikiIngestRequest {path?, paths?, urls?}`, caught by tsc after an initial `{url}` guess); `launchResearchRun` wraps the §2.1 pipeline launcher.
- `WikiSourcesManager.tsx` (233 lines): fresh in-pane list/detail (no legacy port per b03431941): filterable rows (title + mono detail path/url), expandable string-field detail list, per-row Remove → dry-run preview → kept `WikiSourceRemovalDialog` → confirmed removal → `onRemoved()` refresh. "Back to wiki" returns to the shell.
- `WikiTab.tsx` (331 lines): four-mode shell — mode/topic persisted via WikiTabState, ambient `useDirtyGuard().guardedRun` wraps mode switches, `useWikiNav` wired to it with last-page persistence per browse mode, `ResizeObserver` wide (≥560px) toggling search placement (no Tailwind `md:`), `summarizeWikiStatus` → banner + gating, inline Topic-scope and Ingest-URL forms under the toolbar, hidden file input for attach, aria-live status/error line, graph view = full-pane placeholder that calls `requestPanelOverride`/`releasePanelOverride` (dirty-guarded open, mirrors MemoryTab), sources view swap, ask/research placeholder composers (disabled; offline note when gateway unreachable).
- `ActivityPanel.tsx`: `case "wiki"` now passes both override props (2.2.3).
- fixtures.ts: added `sourcesEnvelope` (two records matching `sourceRecordsFromEnvelope`'s `payload.sources[].id` contract).
- All new files well under the 1,000-line cap.

### TDD evidence (#17759)

- **Red**: `cd web && npm test -- src/components/activity/wiki` → `Test Files 2 failed | 2 passed` — both new suites failed on `Failed to resolve import "../WikiTabState"`; then `npm test -- src/components/activity/wiki/__tests__/WikiTabActions.test.ts` → `Test Files 1 failed (1)` (unresolved `../WikiTabActions`) before that module was written.
- **Green**: `npm test -- src/components/activity/wiki` → `Test Files 5 passed (5), Tests 81 passed (81)` (53 §2.1 + 28 new).
- **Refactor/final green**: fixed `ingest` request shape (`{urls}`) after tsc TS2322 and removed a render-phase ref write after `react-hooks/refs`; final `npm run type-check` clean, `npx eslint src/components/activity/wiki src/components/activity/WikiTab.tsx src/components/activity/ActivityPanel.tsx --max-warnings 0` clean, suite re-run 81 passed.
- **Test-quality audit**: `uv run gobby test-quality audit web/src/components/activity/wiki/__tests__ --baseline .gobby/test-quality-baseline.json --fail-on-new --min-severity high` → 6 files, 72 tests, 0 issues, 0 new ≥ high.
- Coverage judgment: unit level (vitest/jsdom) — shell behavior (mode persistence/restore, dirty-guard veto/allow, degraded banner, offline composer gating, kebab→sources flow) plus nav-history and actions-orchestration semantics. Remaining gap: QuickMenu positioning and ResizeObserver width switching are visual concerns deferred to the §6.1 polish/verification pass.

## #17760 — Wikilink remark plugin + MarkdownBody extension seam (§2.3)

**Investigation summary**
- `MarkdownBody.tsx` (132 lines) splits content into blocks via `marked.lexer`, renders each through a `MemoizedBlock` whose comparator is content-only and whose key is `` `${id}-${i}-${stableHash(block)}` ``. Plugins (`[remarkGfm]`) and `components` (`codeBlockComponents`) are hardcoded — no extension seam.
- `codeBlockComponents` (chat/CodeBlock.tsx) overrides `code/table/a/img/h1–h4`; its `Anchor` spreads incoming props AFTER its own `className="text-accent hover:underline"`, so an hProperties className on wikilink anchors replaces the default — `.wikilink` CSS must carry the full style itself.
- The §2.1 resolver exists: `resolveWikilinkTarget(index, target)` in `WikiTabModel.ts` (exact path → path+`.md` → normalized title). The plugin takes a generic `resolve?: (target) => {path} | null` option so P3 can adapt it.
- `@types/mdast`, `unified`, `remark-parse` are hoisted transitive deps — type-only imports resolve; no new package.json entries needed. The plugin itself has zero runtime imports (hand-rolled mdast walk; no unist-util-visit).
- Global styles: `web/src/styles/index.css` imports token/base layers; `--accent`/`--text-muted` tokens confirmed in use.

**Planned changes**
1. `web/src/lib/markdown/remarkWikilink.ts` (NEW): plugin `remarkWikilink(options?)` returning a Root transformer. Regex `/(!?)\[\[([^\]|]+?)(?:\|([^\]]+?))?\]\]/g` over `text` nodes; recursive walk skipping `link`/`linkReference` parents; splits matched text into `text`/`link` node sequences (no empty text nodes; adjacent links supported). Link node: `url = 'wikilink:' + encodeURIComponent(target)` (full raw target incl. anchor), child text = `alias ?? prettified last path segment` (strip `.md`, keep `#anchor` suffix), `data.hProperties = { className: 'wikilink' | 'wikilink wikilink--unresolved', 'data-wiki-target': target }` + `'aria-description': 'Page not created yet'` when unresolved. Resolution uses the pre-`#` page part; no resolver → resolved-optimistic. Embeds (`![[…]]`) degrade to the identical plain link. Pure remark, no rehype.
2. `web/src/components/shared/MarkdownBody.tsx` (EDIT): optional `remarkPlugins?: PluggableList` and `components?: Partial<Components>` props; merged as `[remarkGfm, ...remarkPlugins]` / `{...codeBlockComponents, ...components}` (defaults byte-identical when omitted — module-level `[remarkGfm]` const). `MemoizedBlock` receives plugins/components; comparator adds reference-identity checks; block key gains a WeakMap-based identity id for the extension props so cached blocks re-render when the plugin set changes.
3. `web/src/styles/markdown.css` (NEW) + import in `index.css`: `.wikilink` = accent, underline on hover (matches Anchor default look); `.wikilink--unresolved` = `--text-muted` + always-visible dashed underline (non-hue cue per deutan constraint; aria-description carries semantics).
4. `web/src/lib/markdown/__tests__/remarkWikilink.test.ts` (NEW): transformer unit tests on hand-built mdast trees (basic, alias, anchor incl. resolver receiving pre-`#` part, unresolved className+aria, no-resolver optimistic, adjacent links, embed degradation, `.md` prettify, text-inside-link untouched, nested emphasis recursion) + MarkdownBody rendering tests via `createElement` (defaults unchanged without props; wikilink anchor renders with href/class/data attrs; components merge override; plugin-set change re-renders memoized blocks — pins the identity-hash key).

**Validation commands**
- Red/green/final: `cd web && npm test -- src/lib/markdown`
- Final: `cd web && npm run type-check && npx eslint src/lib/markdown src/components/shared/MarkdownBody.tsx --max-warnings 0`
- Test-quality audit: `uv run gobby test-quality audit web/src/lib/markdown/__tests__ --baseline .gobby/test-quality-baseline.json --fail-on-new --min-severity high`

**Notes**
- `url` keeps the full raw target (anchor included) per spec; only *resolution* strips the anchor, since the node index has no heading entries.
- Working tree still carries another session's uncommitted auth changes — stage only `web/src/lib/markdown/**`, `MarkdownBody.tsx`, `web/src/styles/{markdown.css,index.css}`, and this log.

### Implementation notes (#17760)

- `remarkWikilink.ts` (121 lines): zero runtime imports (type-only mdast imports; hand-rolled walk instead of unist-util-visit — no new deps). Plain plugin function without `this` so it satisfies unified's `Pluggable` via its `any`-typed params *and* is directly callable in tests. Walk skips `link`/`linkReference` parents (no nested links), rebuilds children only when a text node actually matched. `deriveLabel`: last path segment of the pre-`#` part, `.md` stripped, anchor suffix kept (`gobby#History`). Unresolved adds `aria-description: "Page not created yet"` to hProperties.
- `MarkdownBody.tsx` (now 190 lines): optional `remarkPlugins`/`components` props; merges `[remarkGfm, ...extra]` / `{...codeBlockComponents, ...overrides}` with module-level defaults so the no-props path is byte-identical (empty-array extension list also short-circuits to the default plugin array). `MemoizedBlock` takes plugins/components with reference-identity comparator terms, and the block key gains a WeakMap-based `extensionIdentity` suffix so content-equal cached blocks re-render when the extension set changes. **Found during green**: react-markdown's `defaultUrlTransform` sanitizes unknown protocols — `wikilink:` hrefs rendered as `""`. Added a module-level `urlTransform` that passes `wikilink:` URLs through and defers everything else to `defaultUrlTransform` (default sanitization preserved).
- `web/src/styles/markdown.css` (new, imported from `index.css`): `.wikilink` = `--accent`, underline on hover (Anchor's hProperties className *replaces* its default `text-accent hover:underline`, so the class carries the full style); `.wikilink--unresolved` = `--text-muted` + permanent dashed underline incl. hover (non-hue cue, deutan-safe per `.impeccable.md`).

### TDD evidence (#17760)

- **Red**: `cd web && npm test -- src/lib/markdown` → `Test Files 1 failed (1)`, `Error: Failed to resolve import "../remarkWikilink"` (test written before the module existed).
- **Green (first run)**: 15/16 passed; the wikilink-href rendering test exposed the URL-sanitizer gap (`expected '' to be 'wikilink:knowledge%2Fconcepts%2Fgobby'`) → fixed with the `urlTransform` passthrough → `npm test -- src/lib/markdown` → `Tests 16 passed (16)`.
- **Refactor/final green**: `npm run type-check` clean; `npx eslint src/lib/markdown src/components/shared/MarkdownBody.tsx --report-unused-disable-directives --max-warnings 0` clean; regression `npm test -- src/components/chat/__tests__/ToolCallCard.render.test.tsx` (renders real MarkdownBody) → 12 passed; suite re-run 16 passed.
- **Test-quality audit**: `uv run gobby test-quality audit web/src/lib/markdown/__tests__ --baseline .gobby/test-quality-baseline.json --fail-on-new --min-severity high` → 1 file, 16 tests scanned, 0 issues, 0 new ≥ high.
- Coverage judgment: unit level (vitest/jsdom) — transformer semantics on hand-built mdast trees (alias, anchor+resolver contract, unresolved class/aria, optimistic default, adjacency, embed degradation, `.md` label strip, link-interior immunity, nested recursion, no-op) plus the MarkdownBody seam through real react-markdown rendering (byte-identical defaults, hProperties→DOM attribute mapping, component merge, plugin-set-change re-render pinning the identity-key mechanism). Remaining gap: visual verification of the `.wikilink` CSS is deferred to the §6.1 pass; the P3 reader wires the resolver + click handling.

## #17761 — Mermaid rendering (plan §2.4)

**Investigation summary**
- `mermaid` is NOT in `web/package.json` — new dependency, must load as a lazy chunk (`import('mermaid')` only; type-only top-level imports are erased and safe).
- `CodeBlockInner` + `CodeProps` are exported from `chat/CodeBlockRenderers.tsx`; it handles inline detection, the language header strip, copy/artifact buttons, and delegates to shared `CodeBlock`. `useArtifactContext` has a no-op fallback (console.warn) without a provider; existing `CodeBlock.test.tsx` mocks it plus `react-syntax-highlighter` — reuse those mock patterns.
- `useResolvedTheme()` (`hooks/useResolvedTheme.ts`) = `useSyncExternalStore` over `data-theme` on `<html>` (`'light'` only when attribute is `light`, else `'dark'`; SSR snapshot `'dark'`) — theme flips in tests via `document.documentElement.setAttribute('data-theme', …)`.
- `resolveCssVar(varName, alpha?)` (`lib/utils.ts`) returns canvas-normalized `rgb()/rgba()` strings, `''` when unresolvable — mock in tests for deterministic themeVariables assertions (spread `importOriginal` to keep `cn`).
- The #17760 extension seam is the integration point: §3.1 will pass `components = {code: MermaidBlock}` through `MarkdownBody`; this task ships the override itself.

**Planned changes**
1. `web/package.json`: add `mermaid` (npm install — lazy chunk only, never statically imported for values).
2. `web/src/components/shared/MermaidBlock.tsx` (NEW): exported `MermaidBlock` — a `code` component override (accepts `CodeProps`). Non-`language-mermaid` fences delegate to `CodeBlockInner` unchanged. Mermaid fences render via internal `MermaidDiagram`: module-level lazy singleton `import('mermaid')`; `ensureInitialized(api, theme)` calls `initialize({startOnLoad:false, securityLevel:'strict', theme:'base', themeVariables})` once per resolved theme (re-init on `useResolvedTheme()` change), themeVariables mapped via `resolveCssVar`: background/primaryColor ← `--bg-secondary`, textColor/primaryTextColor ← `--text-primary`, lineColor ← `--border`, primaryBorderColor ← `--accent`, `darkMode` from theme. `mermaid.render(uniqueId, code)` → SVG via `dangerouslySetInnerHTML` in an `overflow-auto` container with default max-height + Expand/Collapse toggle. States: loading skeleton (`role="status"`); error → `CodeBlockInner` fallback with a small "Diagram failed to render" note.
3. `web/src/components/shared/__tests__/MermaidBlock.test.tsx` (NEW): `vi.hoisted` mocks for `mermaid` (default export `{initialize, render}`), `react-syntax-highlighter` (+styles), `ArtifactContext`, `resolveCssVar`; `vi.resetModules()` + per-test dynamic import so the module-level singleton state is test-isolated. Tests: themed SVG render in dark (init config + token mapping asserted), light theme (`darkMode:false`), re-init + re-render on live theme flip, render-failure fallback to highlighted code block + error note (acceptance 2.4.2), non-mermaid delegation to `CodeBlockInner`, loading skeleton while pending, expand toggle, single `initialize` for two same-theme diagrams (lazy singleton).

**Validation commands**
- Red/green/final: `cd web && npm test -- src/components/shared/__tests__/MermaidBlock.test.tsx`
- Final: `cd web && npm run type-check && npx eslint src/components/shared/MermaidBlock.tsx src/components/shared/__tests__/MermaidBlock.test.tsx --report-unused-disable-directives --max-warnings 0`
- Test-quality audit: `uv run gobby test-quality audit web/src/components/shared/__tests__/MermaidBlock.test.tsx --baseline .gobby/test-quality-baseline.json --fail-on-new --min-severity high`

**Notes**
- Bundle-safety: the only top-level `mermaid` reference is `type MermaidApi = typeof import('mermaid').default` (erased at compile time); the value import stays inside the lazy loader.
- Working tree still carries another session's uncommitted auth changes — stage only `web/package.json`, `web/package-lock.json`, `MermaidBlock.tsx`, its test, and this log.

### Implementation notes (#17761)

- `mermaid ^11.16.0` added via `npm install` (package.json + lock). Nothing imports `MermaidBlock` yet (§3.1 wires it into the reader's `components`), and the only value reference to mermaid is `import('mermaid')` inside the module-level `loadMermaid()` singleton — a production build (`npm run build`, exit 0) emits zero mermaid chunks today, and the dynamic-import-only structure guarantees a lazy chunk once wired.
- `MermaidBlock.tsx` (131 lines): exported `MermaidBlock` accepts `CodeProps` and delegates non-`language-mermaid` fences to `CodeBlockInner` untouched. Internal `MermaidDiagram`: `ensureInitialized(api, theme)` re-runs `initialize({startOnLoad:false, securityLevel:'strict', theme:'base', themeVariables})` only when the resolved theme changes (module-level last-theme tracking — one init for N diagrams); themeVariables map `--bg-secondary`→background/primaryColor, `--text-primary`→textColor/primaryTextColor, `--border`→lineColor, `--accent`→primaryBorderColor, `darkMode` from `useResolvedTheme()`. Unique `mermaid-block-<seq>` id per render call (mermaid mounts a temp DOM node under the id). SVG lands via `dangerouslySetInnerHTML` in an `overflow-auto max-h-96` container with an Expand/Collapse footer toggle; loading is an `role="status"` skeleton; render failure falls back to `CodeBlockInner` plus a "Diagram failed to render — showing source." note.
- **Found during type-check**: the canary `react-hooks/set-state-in-effect` rule rejects synchronous `setState` in effect bodies. Replaced the effect's loading reset with derived state: results are stored keyed by the `(theme, code)` request pair and "loading" is derived on key mismatch — also kills stale-result races on top of the `cancelled` flag.

### TDD evidence (#17761)

- **Red**: `cd web && npm test -- src/components/shared/__tests__/MermaidBlock.test.tsx` → `Test Files 1 failed (1)`, `Failed to resolve import "../MermaidBlock"` (8 tests written before the module existed).
- **Green**: same command after `npm install mermaid` + implementation → `Tests 8 passed (8)`.
- **Refactor/final green**: derived-loading rewrite for the hooks rule, then `npm test -- src/components/shared/__tests__/MermaidBlock.test.tsx` → 8 passed; `npm run type-check` clean; `npx eslint src/components/shared/MermaidBlock.tsx src/components/shared/__tests__/MermaidBlock.test.tsx --report-unused-disable-directives --max-warnings 0` clean; regression `npm test -- src/components/chat/__tests__/CodeBlock.test.tsx` → 15 passed; `npm run build` exit 0.
- **Test-quality audit**: `uv run gobby test-quality audit web/src/components/shared/__tests__/MermaidBlock.test.tsx --baseline .gobby/test-quality-baseline.json --fail-on-new --min-severity high` → 8 tests scanned, 0 issues, 0 new ≥ high.
- Coverage judgment: component level (vitest/jsdom, mermaid API mocked) — init contract incl. token→themeVariables mapping in both themes (2.4.1), live theme-flip re-init + re-render via the real MutationObserver path, error fallback to highlighted source with note (2.4.2), non-mermaid delegation, loading skeleton, expand toggle, single-init lazy singleton with unique render ids. Remaining gap: real mermaid SVG output and visual theming are deferred to the §6.1 verification pass (mocked here — rendering real mermaid in jsdom is not meaningful); bundle-chunk shape re-checked when §3.1 wires the component.

## #17762 — Page tree, reader, backlinks, quick-open, history (plan §3.1)

**Investigation**
- P2 seams all landed: `WikiTabModel.ts` (buildPageTree(pages, outputs, rootFilter?), buildNodeIndex, resolveWikilinkTarget, pageKindFromPath, breadcrumbSegments, codePathToSourcePath, WikiPageKind incl. KIND_BY_PREFIX), `WikiTabData.ts` (fetchPages/fetchPage/fetchBacklinks/fetchSearch + WikiPageDetail{content,body,frontmatter,contentHash,status,truncated}, WikiBacklink{sourcePath,targetPath,rawTarget}), `WikiTabState.ts` (WIKI_TAB_KEYS incl. treeWidth/split, readStoredValue/writeStoredValue, useWikiNav w/ guardedRun + modeForPath), `WikiTab.tsx` shell (ModeBody placeholder is the seam; nav/actions/scope/search/wide already live in the shell), `remarkWikilink` ({resolve} option; href `wikilink:<encoded raw target>`; className wikilink/wikilink--unresolved), `MarkdownBody` (remarkPlugins/components extension props; memo key includes extension identity — memoize plugin array + components), `MermaidBlock` (#17761), `.wikilink` CSS in markdown.css.
- Shared kit: `useTreeKeyboardNavigation` (items{id,depth,isExpandable,isExpanded}, selectionFollowsFocus:false for fetch-on-select trees), `ResizeHandle` (FilesTab idiom: horizontal anchor=left panelWidth min/max; vertical panelHeight percent), `ActivityPanelEmpty{icon,heading,body,footer}`, `QuickMenu{items,menuLabel,anchor}`, `useConfirmDialog`, `Anchor` (CodeBlockRenderers), `DEFAULT_TOP_PANEL_PERCENT` (activity/constants), Virtuoso mock idiom in SessionsTab.test. Existing fixtures cover pages/read/backlinks/graph envelopes.
- `WikiTabActions` already ships `deletePageAndNavigateBack` — the row/reader kebab Delete wires to it behind `useConfirmDialog`; create lands in 3.2 (kebab item hidden when no handler).

**Planned changes**
- NEW `wiki/WikiBrowse.tsx` (~200): browse composition for wiki|code modes — pages fetch (derived-state keyed by scope+mode, retry), nodeIndex memo, lazy graph fetch (first backlinks expand), wide/narrow layout with persisted tree width (200–480, WIKI_TAB_KEYS.treeWidth) / vertical split percent (WIKI_TAB_KEYS.split, DEFAULT_TOP_PANEL_PERCENT), selectedPath = nav.current (mode-matched) ?? loadLastPage(mode), Cmd+K keydown on browse root opens quick-open.
- NEW `wiki/WikiPageTree.tsx` (~300): buildPageTree w/ rootFilter (wiki hides code/; code mode shows only code/), expanded-set flatten, knowledge/sources collapsed by default, 28px rows (h-7, pointer-coarse:min-h-11) FilesTab-style, kind-colored icons (concept --accent, topic --color-info, source --text-muted, recap --color-warning-foreground, code --color-info, folders --lang-folder; icon shape differs folder/page so color is never the only signal), useTreeKeyboardNavigation (selectionFollowsFocus:false), search → flat match list (Virtuoso >100), row kebab (Open, New page here?, Copy path, Delete page?), fetch-fail → ActivityPanelEmpty + retry.
- NEW `wiki/WikiPageReader.tsx` (~350): h-10 strip (back/forward via nav, breadcrumbs w/ middle elide, Edit toggle disabled until 3.2 handler, kebab: Open in graph, Copy path, Copy source path [code], Delete), fetchPage derived-state; status found → frontmatter header (title, source_kind badge, tag chips, Details disclosure w/ raw frontmatter) + ~70ch prose MarkdownBody(remarkPlugins=[[remarkWikilink,{resolve}]], components={a: wiki anchor override → preventDefault + resolve + nav.openPage | missing-page notice, code: MermaidBlock}); ambiguous → candidates picker; not_found → create affordance stub (wired 3.2); Citations strip from `## Citations` wikilinks + graph trust edges when loaded.
- NEW `wiki/WikiBacklinks.tsx` (~150): collapsible section at reader bottom; linked mentions via fetchBacklinks on expand/path-change; unresolved mentions from graph links edges → unresolved_target nodes whose raw_target equals page path (hidden until graph present); rows navigate via openPage.
- NEW `wiki/WikiQuickOpen.tsx` (~220): panel-scoped overlay; fuzzy title/path over nodeIndex (title-prefix > title-substr > path-substr), server fetchSearch fallback merge; ArrowUp/Down/Enter/Esc.
- EDIT `WikiTab.tsx`: ModeBody wiki/code branch → WikiBrowse (pass mode/scope/nav/search/actions/wide/summary).
- EDIT `__tests__/fixtures.ts`: add ambiguous + not_found read fixtures (and a second read fixture for history tests).
- NEW `__tests__/WikiBrowse.test.tsx`: acceptance 3.1.1–3.1.4 (tree structure + kind icons + keyboard nav; reader frontmatter + wikilink click-through + unresolved marking; backlinks linked/unresolved navigation; quick-open fuzzy jump; back/forward incl. dirty-guard decline; pages-fetch failure retry; ambiguous picker).

**Validation**
- Red/green: `cd web && npm test -- src/components/activity/wiki/__tests__/WikiBrowse.test.tsx`
- Suite-adjacent regression: `npm test -- src/components/activity/wiki src/components/shared/__tests__/MermaidBlock.test.tsx`
- `npm run type-check`; scoped eslint over new/edited files; `uv run gobby test-quality audit web/src/components/activity/wiki/__tests__/WikiBrowse.test.tsx --baseline .gobby/test-quality-baseline.json --fail-on-new --min-severity high`

**Notes**
- Derived-state fetch pattern everywhere (no sync setState in effects — canary react-hooks/set-state-in-effect).
- Working tree still carries another session's uncommitted auth work — stage only wiki paths + this log.

### Implementation notes (#17762)

- Five new files under `web/src/components/activity/wiki/`: `WikiBrowse.tsx` (278 — composition: derived-state pages fetch keyed by scope with retry, memoized node index, lazy graph fetch triggered by the backlinks expand, wide/narrow layouts with persisted tree width 200–480px / vertical split percent via `WIKI_TAB_KEYS`, ⌘K quick-open, delete behind `useConfirmDialog`), `WikiPageTree.tsx` (314 — `buildPageTree` + rootFilter [wiki hides `code/`, code shows only it], all folders default-collapsed so `knowledge/sources` and the `code/files` mirror never render children until expanded, 28px rows with `pointer-coarse:min-h-11`, folder/page icon shapes with kind-token colors, `useTreeKeyboardNavigation` with `selectionFollowsFocus:false`, flat search-match list with Virtuoso >100, per-row QuickMenu), `WikiPageReader.tsx` (434 — h-10 strip with history chevrons + eliding breadcrumbs + disabled-until-3.2 Edit + page kebab, frontmatter header [title/source_kind badge/tag chips/Details disclosure], `MarkdownBody` with `remarkWikilink` resolver + wiki anchor override + `MermaidBlock`, duplicate leading `# Title` stripped from compiled bodies, ambiguous-status match picker, missing-page notice, Citations sources strip + graph trust edges), `WikiBacklinks.tsx` (159 — collapsible linked mentions via `fetchBacklinks` on expand, unresolved mentions from graph links edges whose target node is `unresolved_target` with rawTarget equal to the page path), `WikiQuickOpen.tsx` (202 — combobox/listbox overlay, title-prefix > title-substring > path-substring ranking, `fetchSearch` fallback when local matches < 5).
- `WikiTab.tsx`: `ModeBody` wiki/code branch now renders `WikiBrowse` (scope/nav/search/wide/actions/onOpenGraph threaded through); ask/research placeholders unchanged.
- `WikiTabData.ts` (additive): `WikiPageCandidate` + `WikiPageDetail.candidates` normalized from `candidates`/`matches` (string or record entries) — the ambiguous match-picker input.
- Fixtures: `browseReadGobbyEnvelope` (wikilinks + `## Citations`), `browseReadGwikiEnvelope`, `browseAmbiguousReadEnvelope`, `browseGraphEnvelope` (links edge → unresolved node whose raw_target is the Gobby page path).
- Canary `react-hooks/set-state-in-effect` hit twice more; both fixed with request-keyed derived state (reader read state + missing-target notice), and the `exhaustive-deps` conditional-identity warning fixed by memoizing `{pages, outputs}` from the stored result rather than the per-render state object.

### TDD evidence (#17762)

- **Red**: `cd web && npm test -- src/components/activity/wiki/__tests__/WikiBrowse.test.tsx` → `Test Files 1 failed (1)`, `Tests 16 failed (16)` — no tree/reader/backlinks/quick-open rendered by the placeholder ModeBody (16 tests written before the modules existed).
- **Green**: same command after implementing the five modules + WikiTab wiring → intermediate 14/16 (roving-focus rAF timing + duplicate fixture title), then `Tests 16 passed (16)`.
- **Refactor/final green**: derived-state + memo-identity lint fixes, then `npm test -- src/components/activity/wiki src/components/shared/__tests__/MermaidBlock.test.tsx src/components/chat/__tests__/CodeBlock.test.tsx src/lib/markdown` → `Test Files 9 passed (9)`, `Tests 136 passed (136)`; `npm run type-check` clean; scoped `npx eslint … --max-warnings 0` clean over all nine touched files.
- **Test-quality audit**: `uv run gobby test-quality audit web/src/components/activity/wiki/__tests__/WikiBrowse.test.tsx --baseline .gobby/test-quality-baseline.json --fail-on-new --min-severity high` → 16 tests scanned, 0 issues, 0 new ≥ high.
- Coverage judgment: browser/UI level (vitest/jsdom against the full WikiTab shell with stubbed fetch): 3.1.1 tree structure/code-hiding/collapse defaults/kind-token icons/keyboard nav/search filter/fetch-fail retry; 3.1.2 frontmatter header + wikilink navigation + unresolved marking + missing-notice + ambiguous picker + citations strip; 3.1.3 linked/unresolved mentions with navigation; 3.1.4 quick-open jump + Escape, back/forward retrace, dirty-guard veto. Remaining gaps: real-vault rendering and the wide-layout resize interaction are visual — deferred to the §6.1 verification pass; Virtuoso >100-match branch exercised via mock only.

## #17763 — Editing, create, delete (§3.2)

**Scope** (plan §3.2, P3): edit toggle swaps the reader for a `CodeMirrorEditor` behind `useDetailDraft` + `DetailPaneHeader`; revision contract (base `content_hash`, revalidate on focus/manual refresh/pre-save, 412 → Reload/Overwrite/keep-editing conflict panel, no silent last-write-wins); recipe-compliant create form (path validated `a-z0-9-/_.` under `knowledge/` ending `.md` to mirror gwiki confinement, frontmatter template, inline 409); delete already wired via `useConfirmDialog` — this task hides edit/delete for `code/**` (read-only) and adds the not-found/wikilink create affordances.

**Design (files)**
- NEW `wiki/WikiPageEditor.tsx`: dual-intent (`{kind:"edit",path}` / `{kind:"create",seed}`). Edit: fetch page (derived-state keyed by scope+path) → base `{content, hash}`; `useDetailDraft<{path,content}>` (auto dirty-guard registration, Cmd+S via editor `onSave`); `DetailPaneHeader` title = path + "● Unsaved" dot, actions slot = Refresh (revalidate) + Close (confirmIfDirty). Revalidate on window focus/manual/pre-save: fetch fresh hash, differs → new base (serverChanged when dirty). Save → `savePageAndRefresh({path, content, expectedHash})`; ok → close; 412/pre-save mismatch → inline conflict panel (role=alert, warning tokens + text cue): Reload (fresh base + discard), Overwrite (destructive confirm dialog → resave against fresh hash), Keep editing. Create: path field (prefilled seed, charset/prefix/.md validation inline) + editor seeded `---\ntitle: …\ntags: []\n---`; Save → `createPageAndNavigate` → ok navigates via actions; 409 `already_exists` inline by the path field. Exports `WikiEditorIntent`, `seedCreatePath`, `validateCreatePath`.
- EDIT `wiki/WikiTabActions.ts`: conflicts become caller-owned UX — `savePageAndRefresh` returns the conflict result with status reset to idle (no global error strip under the conflict panel); `createPageAndNavigate` → `Promise<WikiSaveResult | null>`: ok → refetch + navigate + success status, conflict → quiet return for inline display.
- EDIT `wiki/WikiPageReader.tsx`: code pages read-only — Edit button hidden (not disabled) for `code/**` or absent handler; kebab gains "New page" (non-code, seeds current folder); not_found state + missing-wikilink notice gain "Create this page" buttons via new `onCreate?(seed)` prop; milestone placeholder copy removed.
- EDIT `wiki/WikiBrowse.tsx`: `editorIntent` state stale-guarded by `selectedPath` captured at open (nav/mode changes auto-dismiss without effects); reader slot renders `WikiPageEditor` when intent live; wires `onToggleEdit`/`onCreate`/tree `onCreateAt`; new `refreshSeq` prop re-runs pages fetch and (if loaded) graph fetch after save/create/delete.
- EDIT `WikiTab.tsx`: `browseRefreshSeq` bumped inside actions `onRefetch`, threaded to `WikiBrowse`.
- Tests: NEW `__tests__/WikiPageEditor.test.tsx` (3.2.1 edit/draft/dirty-guard/Cmd+S/save-exit; 3.2.2 create validate/seed/409-inline/navigate; 3.2.3 delete confirm + back, code read-only), NEW `__tests__/WikiPageEditor.conflict.test.tsx` (3.2.4 pre-save revalidation conflict, 412 panel Reload/Overwrite/keep-editing, focus revalidation serverChanged badge); EDIT `__tests__/WikiTabActions.test.ts` (conflict now quiet-return); fixtures: `alreadyExistsBody`, hash-bearing read envelope.

**Validation**
- Red/green: `(cd web && npm test -- src/components/activity/wiki/__tests__/WikiPageEditor.test.tsx src/components/activity/wiki/__tests__/WikiPageEditor.conflict.test.tsx)`
- Regression: `(cd web && npm test -- src/components/activity/wiki)`; `npm run type-check`; scoped eslint --max-warnings 0; test-quality audit on both new test files.

**Notes**
- CodeMirrorEditor mocked as textarea in tests (established idiom: RulesTab/SkillsTab); real editor syncs external `content` changes so Reload works.
- Working tree still carries another session's uncommitted changes — stage only wiki paths + this log.

### Implementation notes (#17763)

- NEW `wiki/WikiPageEditor.tsx` (305): dual-intent editor — `WikiEditorIntent = {kind:"edit",path} | {kind:"create",seed}` (host keys the component by intent, so internal state is per-intent). Edit: `fetchPage` seeds base `{content, hash}` with a synchronous `baseHashRef` mirror so revalidate/save never race a re-render; `useDetailDraft<{path,content}>` gives draft state, shell dirty-guard registration, and `confirmIfDirty` for Close/Cancel; `DetailPaneHeader` strip (path + ● Unsaved dot, Save/Discard when dirty, "Changed on server" badge, Refresh + Close actions). Revision contract: revalidate on window focus / Refresh / immediately before save; hash change adopts the fresh base (dirty → `serverChanged`, clean → in-place refresh); pre-save mismatch or a 412 opens the inline conflict panel (warning tokens + text cue, role=alert): Reload (discard → fresh base), Overwrite (destructive `useConfirmDialog` → adopt fresh hash → re-enter save), Keep editing. Create: path field validated inline against gwiki confinement, editor seeded with `---\ntitle: ""\ntags: []\n---`, 409 already_exists rendered inline; `runSave` fires `onSaved(path)` only after `useDetailDraft.save` marks the draft clean, so the host's `nav.openPage(newPath)` passes the dirty guard.
- EDIT `wiki/WikiTabActions.ts` (210): conflicts are caller-owned UX — `savePageAndRefresh` returns the conflict result with status reset to idle (no duplicate global error under the conflict panel/inline 409) and reports "Created"/"Saved" from `result.created`; `createPageAndNavigate` removed (bundled navigation ran inside `onSave` while the draft was still dirty, tripping the guard — the editor now navigates via `onSaved` after the clean-mark). `onNavigate` option dropped; `WikiTabData.createPage` deleted as dead code.
- EDIT `wiki/WikiPageReader.tsx` (464): `readOnly = path.startsWith("code/")` — code pages read-only (Edit hidden, kebab has no New page/Delete); kebab gains "New page" seeding the current folder; `onCreate?(seed)` prop powers "Create this page" buttons in the not_found empty state (footer slot) and the missing-wikilink notice via `seedCreatePath`; milestone placeholder copy removed.
- EDIT `wiki/WikiBrowse.tsx` (322): `editorIntent` stored with the selection it was opened over (`at`) — any guarded navigation that changes `selectedPath` dismisses the editor by derivation, no reset effects; reader slot renders `WikiPageEditor` when the intent is live; `refreshSeq` prop re-runs the pages fetch (stale-while-refetch: old tree stays until fresh data lands) and folds into the graph key so a loaded graph refetches after writes.
- EDIT `WikiTab.tsx` (367): `browseRefreshSeq` bumped in the actions `onRefetch` and threaded through ModeBody.
- EDIT `wiki/WikiTabModel.ts` (277): `validateCreatePath` (charset a-z0-9-/_., knowledge/ prefix, .md file, traversal-free — mirrors gwiki `confine_page_path`) + `seedCreatePath` (helpers here, not in the component file, per react-refresh/only-export-components).
- Fixtures: `browseReadGobbyChangedEnvelope` (watcher-reindexed variant), `alreadyExistsBody` (409), `notFoundReadEnvelope`, `browseReadCodeEnvelope`.
- Found-it-own-it: `gobby test-quality audit` misparsed prose `WikiPageEditor.conflict.test.tsx (3.2.4)` in a docblock (and would misparse `/^a/.test(...)`) as a test declaration → phantom NO_ASSERTION. Fixed `_SCRIPT_TEST_CALL_RE` in `src/gobby/test_quality/_analyzer_script.py` with a `(?<![.\w$])` lookbehind (test declarations are never dot-prefixed) + regression test `test_member_access_test_is_not_a_test_declaration`.

### TDD evidence (#17763)

- **Red (frontend)**: `(cd web && npm test -- src/components/activity/wiki/__tests__/WikiPageEditor.test.tsx src/components/activity/wiki/__tests__/WikiPageEditor.conflict.test.tsx)` → `Tests 14 failed | 2 passed (16)` — all §3.2 behavior missing (editor textbox, New page menu items, Create this page, code read-only, conflict panel); the 2 passes were the delete flows already wired in §3.1.
- **Green (frontend)**: same command → `Test Files 2 passed (2)`, `Tests 16 passed (16)`.
- **Red (scanner fix)**: `GOBBY_TEST_PROTECT=1 uv run pytest tests/test_quality/test_analyzer.py::test_member_access_test_is_not_a_test_declaration -q` → `assert 3 == 1` (two phantom tests from member-access matches).
- **Green (scanner fix)**: `GOBBY_TEST_PROTECT=1 uv run pytest tests/test_quality/ -q` → `18 passed`; `ruff check`/`ruff format --check`/`mypy src/gobby/test_quality/` clean.
- **Refactor/final green**: helpers moved to WikiTabModel (react-refresh warning), then `(cd web && npm test -- src/components/activity/wiki)` → `Test Files 8 passed (8)`, `Tests 113 passed (113)`; `npm run type-check` clean; scoped `npx eslint … --max-warnings 0` clean over all eleven touched web files.
- **Test-quality audit**: `uv run gobby test-quality audit web/src/components/activity/wiki/__tests__/WikiPageEditor.test.tsx web/src/components/activity/wiki/__tests__/WikiPageEditor.conflict.test.tsx --baseline .gobby/test-quality-baseline.json --fail-on-new --min-severity high` → 16 tests scanned, 0 issues, 0 new ≥ high.
- Coverage judgment: browser/UI level (vitest/jsdom over the full WikiTab shell with stubbed fetch + textarea CodeMirror mock, per repo idiom): 3.2.1 raw-content editor, Unsaved indicator, Cmd+S save with base `expected_hash`, exit + pages refetch, Discard/Close, dirty-guarded tree navigation (decline + accept); 3.2.2 kebab/tree/not-found seeding, frontmatter template, charset+prefix validation blocking writes, inline 409, post-create navigation; 3.2.3 destructive delete confirm + history-back, declined confirm, code pages with no edit/delete/create affordances; 3.2.4 pre-save revalidation conflict without a write, 412 panel, Reload adoption, destructive-confirmed Overwrite against the fresh hash, declined overwrite, focus-revalidation serverChanged badge. Gaps: real CodeMirror keybinding/serverChanged rendering and editor visuals are visual-level — deferred to §6.1 verification; `useConfirmDialog` mocked per FilesTab idiom (Radix portal + userEvent pointer-events limitation).

## #17764 — 2D force graph view (§4.1)

**Scope**: The Obsidian-style 2D force graph over wikilinks + code edges, replacing the WikiTab `view === "graph"` placeholder. New dep `react-force-graph-2d@1.29.1` (shares the `force-graph@1.51.4` core; canvas, no WebGL).

**Seam survey**
- Exemplars: `MemoryGraphView.tsx` (release-once override, Esc, error boundary + Suspense around a lazy graph), `KnowledgeGraph.tsx`/`CodeGraphExplorer.tsx` (resolveCssVar-baked colors, kapsule isolation).
- Existing model pieces: `wikiNodeColorVar` (kind → token var), `wikiNodeVal` (2+3·√degree clamp 20), `WikiGraphPayload {nodes, edges, degraded, analytics}` — analytics raw (`centrality: [{node:{id}, degree, score}]`, `communities: [{id, nodes:[{id}], weight}]` verified against `wiki/outputs/graph.json`).
- `useRafCoalescedHandler` (hover coalescing), `useResolvedTheme`, `--chart-series-1..6` tokens (already paired with dash patterns in `chartSeries.ts` — deutan precedent).
- **Found-it-own-it bug**: `WikiTabData.fetchGraph` typed `include` as `"all" | "wiki" | "code"` but the route enum is `{all, knowledge, code}` (`_GRAPH_INCLUDE_VALUES`, wiki.py:36) — "wiki" would 400. Fix to `"all" | "knowledge" | "code"` (exported `WikiGraphInclude`).

**Design**
- NEW `wiki/WikiGraphScene.ts` (pure, testable; component files keep only components per react-refresh): `buildGraphScene(payload, options, resolveColor)` → `{nodes, links, adjacency, totalNodes, capped, communities}`. Node filter (sources/citations off by default, unresolved off, orphans toggle), edge layers (`links` always; `trust`, `audit` off, `imports`/`calls` on; `callers` never — duplicate of calls), endpoint-pruning, degree from `analytics.centrality` fallback local, cap 1500 by descending degree, colors baked once per build via injected `resolveCssVar` (theme-keyed rebuild), `hollow` flag for `unresolved_target`, community color cycling `--chart-series-1..6`.
- NEW `wiki/WikiForceGraph.tsx`: default-export `memo` component, equality on `(dataRevision, theme, width, height, reducedMotion)` ONLY. All interaction state (hover, highlight set, search) lives in a parent-owned `interactionRef`; callbacks via parent-owned `handlersRef` — ref identities are stable so the memo wall never goes stale. `autoPauseRedraw={false}` keeps the internal rAF repainting accessors that read the refs (zero React state per pointer-move); `onNodeHover` coalesced with `useRafCoalescedHandler`. `nodeCanvasObject`: solid disc per baked color, hollow ring (fill bg, stroke color + destructive-token ring) for unresolved; labels only when `globalScale > 1.4` or hovered/highlighted/matched; search dims non-matches to 15% alpha; `nodePointerAreaPaint` for hit-testing. Links: baked `--border`, hover-incident brighten `--accent-soft`, dashed `linkLineDash` for imports/calls. `warmupTicks 80/cooldownTicks 100`; reduced motion → `200/0` + non-animated `zoomToFit` on engine stop. Wrapper focusable `role="application"` with SR summary, `+`/`-` zoom, arrow pan. Pixel-ratio cap from the 3D exemplar does not apply — `force-graph` 2D exposes no renderer/pixelRatio API and canvas framebuffers are ~9x cheaper than WebGL.
- NEW `wiki/WikiGraphView.tsx`: full-panel takeover replacing the WikiTab placeholder — release-once ref + Esc + unmount release (MemoryGraphView pattern), `WikiGraphErrorBoundary` + Suspense around `lazy(() => import("./WikiForceGraph"))`. Fetch keyed `(scope, include, refreshSeq-independent)`; header: title, "showing top 1,500 of N" chip when capped, Close. Filter row: SegmentedControl `All | Knowledge | Code` (maps to `include` fetch param), toggles Sources & citations / Unresolved / Orphans / Trust / Audit / Code edges / Community colors, search input. Persistent kind legend (dot + label, hollow cue for unresolved; community chips when communities on). Settings persisted as JSON at `gobby:wiki-tab:graph` (new `WIKI_TAB_KEYS.graph` + defensive load/store in WikiTabState.ts). Node click → `onOpenPage(path)` (host closes graph, `nav.openPage` flips mode); unresolved nodes (no path) don't navigate.
- EDIT `WikiTab.tsx`: `view === "graph"` renders `WikiGraphView` (initial include `code` in code mode per §4.2 pre-wiring); close restores mode content.
- Acceptance mapping: 4.1.1 WikiForceGraph.tsx (ref-isolated memo, hover/click/zoom); 4.1.2 WikiGraphView.tsx (override + filters/layers/legend + persisted settings + close-restore); 4.1.3 reduced-motion presimulation + grayscale-safe kinds (hollow/ring cues, lightness-differentiated tokens); 4.1.4 WikiGraph.test.tsx (click-nav + hollow unresolved).

**Test plan (`__tests__/WikiGraph.test.tsx`)**: mock `react-force-graph-2d` as a prop-capturing component with per-node click buttons + `useImperativeHandle` stub (`zoom/centerAt/zoomToFit`); WikiTab harness with stubbed fetch (graphEnvelope + browse fixtures). Component tests: open-graph fetches `/api/wiki/graph` and defaults exclude sources/citations/unresolved; node click navigates to the reader and releases override; Esc/Close restore; Sources toggle persists to localStorage and refetch-free re-scene; include=Code triggers refetch with `include=code`; reduced-motion matchMedia → warmup 200/cooldown 0 + `zoomToFit(0)`; hollow unresolved via `nodeCanvasObject(mockCtx)` stroke-not-fill; search dim via ctx.globalAlpha 0.15; keyboard `+` zooms. Scene unit tests: cap 1500 by degree + `capped`, orphan pruning, edge-layer filters, callers excluded, community color cycling, degree sizing from analytics centrality.

### Implementation notes (#17764)

- NEW `wiki/WikiGraphScene.ts` (269): pure `buildGraphScene(payload, options, resolveColor)` — kind filter (source/citation behind the Sources toggle, unresolved behind its toggle), edge layers (`links` always, `trust`/`audit`/code toggles, `callers` never — reverse duplicate of `calls`), endpoint pruning, degree from `analytics.centrality` (`{node:{id}, degree}`) with local-degree fallback, orphan pruning, cap `MAX_GRAPH_NODES = 1500` by descending degree with `totalNodes`/`capped` for the chip, undirected adjacency map, community ordinals cycling `--chart-series-1..6`, and all token colors baked once per build through the injected resolver (unit tests pass a fake; runtime defaults to `resolveCssVar`).
- NEW `wiki/WikiForceGraph.tsx` (291): default-export `memo` wall keyed `(dataRevision, theme, width, height, reducedMotion)` — parent re-renders never reach the kapsule. Interaction (hover/search) and callbacks arrive via parent-owned `MutableRefObject`s; `autoPauseRedraw={false}` keeps force-graph's own rAF repainting accessors that read the refs, so pointer-move causes zero React updates (hover set through `useRafCoalescedHandler`). `nodeCanvasObject`: solid disc (community fill keeps a kind-color ring), hollow unresolved = bg fill + kind stroke + dashed `[2,2]` error-token ring (shape+lightness, grayscale-safe); labels only past `globalScale > 1.4` or hovered/neighbor/matched; search dims non-matches to 0.15 alpha, hover dims non-neighbors to 0.3. `linkColor` brightens hover-incident links to `--accent-soft`, `linkLineDash [4,2]` for imports/calls; `nodePointerAreaPaint` circles for hit-testing. `warmupTicks/cooldownTicks 80/100`, reduced motion `200/0` + `zoomToFit(0)` once on engine stop. Wrapper `role="application"` with SR summary, `+`/`-` zoom, arrow-key pan. Pixel-ratio cap from the 3D exemplar doesn't apply: force-graph 2D exposes no renderer/pixelRatio API (verified against the shipped d.ts) and canvas framebuffers are far cheaper than WebGL.
- NEW `wiki/WikiGraphView.tsx` (387): replaces the WikiTab graph placeholder — release-once ref + Esc + unmount release (MemoryGraphView pattern), `WikiGraphErrorBoundary` keyed by a retry seq + Suspense around `lazy(() => import("./WikiForceGraph"))` (dep stays out of the main bundle). Header: cap chip "showing top 1,500 of N" + Close; filter row: SegmentedControl All|Knowledge|Code driving the `include` fetch param, search input writing the interaction ref, seven toggle checkboxes; persistent kind legend filtered to kinds present (hollow dashed dot for unresolved) plus community chips when enabled. Settings persist via `WikiTabState.loadGraphSettings/storeGraphSettings` (defensive JSON at `gobby:wiki-tab:graph`); node click → release + `onOpenPage(path)` (host `nav.openPage` auto-flips mode); fetch error → `ActivityPanelEmpty` + Retry.
- EDIT `WikiTab.tsx` (361): `view === "graph"` renders `WikiGraphView` (initial include `code` in code mode, §4.2 pre-wiring); the old close handler folded into props (`onClose` flips the view; release is owned by the graph view).
- EDIT `WikiTabModel.ts`/`WikiTabData.ts`: **found-it-own-it** — `fetchGraph` typed `include` as `"all" | "wiki" | "code"` but the route enum is `{all, knowledge, code}` (`_GRAPH_INCLUDE_VALUES`, `src/gobby/servers/routes/wiki.py:36`); `"wiki"` would 400. New `WikiGraphInclude` type in the model, threaded through `fetchGraph` and the settings; the existing `WikiTabData.test.ts` fetcher test encoded the bug (`include=wiki`) and was corrected to `knowledge`.
- EDIT `WikiTabState.ts` (240): `WikiGraphSettings` + defaults (sources/unresolved/audit/communities off; orphans/trust/codeEdges on) with per-field defensive load.
- NEW dep `react-force-graph-2d@1.29.1` (shares the `force-graph@1.51.4` core with the installed 3D package); prop surface verified via context7 + the installed d.ts before writing.
- Fixtures: `graphViewEnvelope` — all six node kinds, every edge layer including a `callers` edge the scene must drop, centrality degrees, two communities (shapes verified against live `wiki/outputs/graph.json`).

### TDD evidence (#17764)

- **Red**: `(cd web && npm test -- src/components/activity/wiki/__tests__/WikiGraph.test.tsx)` → `Test Files 1 failed (1)` — `Failed to resolve import "../WikiGraphScene"` (suite written against the not-yet-existing scene/view/graph modules).
- **Green**: same command after implementation → `Tests 19 passed (19)` on the first run.
- **Refactor/final green**: linkColor per-frame `Array.find` replaced with a memoized `nodesById` map; render-time prop capture in the test mock moved into an effect (react-hooks/immutability); then `(cd web && npm test -- src/components/activity/wiki)` → `Test Files 9 passed (9)`, `Tests 132 passed (132)`; `npm run type-check` clean; scoped `npx eslint <10 touched files> --max-warnings 0` clean.
- **Test-quality audit**: `uv run gobby test-quality audit web/src/components/activity/wiki/__tests__/WikiGraph.test.tsx --baseline .gobby/test-quality-baseline.json --fail-on-new --min-severity high` → 19 tests scanned, 0 issues, 0 new ≥ high.
- Coverage judgment: browser/UI level (vitest/jsdom over the WikiTab harness with a prop-capturing `react-force-graph-2d` mock and a recording canvas context) — 4.1.1 memo/ref isolation, hover-neighbor dimming, label threshold, search dim, keyboard zoom; 4.1.2 override request/release-once, include refetch, toggle persistence across reopen, cap chip, legend, Esc-close restoring the browsed page; 4.1.3 reduced-motion warmup 200/cooldown 0 + `zoomToFit(0)` and the grayscale-safe hollow/ring treatment; 4.1.4 click-nav to the reader + hollow unresolved (stroke + dashed ring asserted through the canvas mock). Gaps: real canvas painting, force layout, and visual polish in both themes are visual-level — deferred to the §6.1 verification pass alongside the rest of the panel.

## #17765 — Codewiki mode (§4.2)

**Seam survey — what P3/#17764 already shipped**

- 4.2.1 tree mechanics: `WikiPageTree` starts fully collapsed (`expandOverrides` default false), search switches to the virtuoso flat list past `VIRTUOSO_MATCH_THRESHOLD`, `WikiBrowse.rootFilter` scopes code mode to `code/**`, and code rows expose no create/delete menu items. Client-side `rootFilter` over the single pages listing (metadata-only rows) carries the 1,068-page mirror; the route's `prefix=` param (`wiki.py:137`) stays available if the listing ever needs server-side scoping.
- 4.2.2 reader affordances: `MermaidBlock` is the reader's `code` component and falls back to `CodeBlockInner` (Prism highlighting) for non-mermaid fences; kebab gains "Copy source path" via `codePathToSourcePath`; `code/**` pages are read-only (no Edit/Delete/New).
- Graph pre-filter: `WikiTab` passes `initialInclude="code"` to `WikiGraphView` in code mode (shipped with #17764); default settings keep code edge layers on.

**Delta (this task)**

1. NEW `wiki/WikiCodewikiStatus.tsx` — freshness strip rendered by `WikiBrowse` above the tree in code mode only: last-refresh relative time (`formatRelativeTime`) + outcome, animated "Refreshing…"/"Refresh queued" indicator (text + motion cue, never hue-only), gateway-unavailable degrades to a quiet "status unavailable" line. Polls `GET /api/code-index/codewiki/status` every 30s while mounted.
2. Data layer (`WikiTabData.ts`): `CodewikiStatus` normalize (`pending_roots`/`running_roots`/`last_run{outcome, finished_at, changed_count, error, root_path}` from `CodewikiRefreshTrigger.status()`), `fetchCodewikiStatus()`, and `requestCodewikiRefresh(scope)` — resolves `root_path` from `GET /api/projects/{id}.repo_path`, falling back to `last_run.root_path`, then POSTs `/api/code-index/codewiki/refresh` (`{accepted, reason}`; not-accepted surfaces the server reason).
3. Kebab action: `WikiTabToolbar` menu gains code-mode-only "Refresh codewiki" → `WikiTabActions.refreshCodewiki` (run-status plumbing like refresh/compile/audit).
4. NEW `__tests__/WikiCodeMode.test.tsx` (acceptance 4.2.3): code tree roots + collapsed default + no wiki roots + virtuoso search (4.2.1); mermaid fence → MermaidDiagram, plain fence → syntax highlighter, Copy source path present, Edit absent (4.2.2); freshness strip shows last-refresh + pending indicator, hidden in wiki mode, refresh kebab POSTs with resolved root_path (4.2.3).

**Test plan**: WikiTab harness from WikiBrowse.test.tsx (stubbed fetch incl. `/api/code-index/codewiki/status`, `/api/projects/p1`, refresh POST; mermaid/syntax-highlighter/virtuoso mocks), mode pre-seeded via `gobby:wiki-tab:mode = "code"`. Red = new test file against not-yet-existing strip/data/action seams.

### Implementation notes (#17765)

- `WikiTabModel.buildPageTree` gained `promoteRoot?: string`: after sorting, the named wrapper folder is replaced by its children so the code mirror's own top level (files/, _architecture, …) is the root set instead of one collapsed "code" folder. Node paths stay full vault paths — navigation, expansion keys, and kind icons are untouched. `WikiPageTree` threads a `promoteRoot` prop; `WikiBrowse` passes `"code"` in code mode.
- NEW `wiki/WikiCodewikiStatus.tsx` (85): one quiet `role="status"` line above the code-mode tree — "Refreshed 5m ago · 12 docs" from `formatRelativeTime`, "Refreshing codewiki…"/"Codewiki refresh queued" with a pulse dot (`motion-reduce:animate-none`; state carried by text, never hue alone), "Last refresh failed …" with the error in `title`, 503 → "Codewiki status unavailable". Polls the trigger snapshot every 30s while mounted.
- `WikiTabData.ts`: `CodewikiStatus`/`CodewikiLastRun` normalizers over the `CodewikiRefreshTrigger.status()` snapshot (plain JSON, not a gwiki envelope), `fetchCodewikiStatus()`, and `requestCodewikiRefresh(scope)` — the refresh route requires the daemon-side repo root, resolved from `GET /api/projects/{id}.repo_path` with `last_run.root_path` as fallback; a not-accepted response returns the server reason.
- `WikiTabActions.refreshCodewiki`: run-status plumbing like refresh/compile/audit; `accepted:false` is surfaced on the error line (nothing will happen — a success message would lie). `WikiTabToolbar` kebab gains a code-mode-only "Refresh codewiki" item; `WikiTab` wires it.
- Pre-existing `WikiPageEditor.test.tsx` read-only test navigated through the now-promoted "code" wrapper row — updated to click the promoted root directly.
- Rest of §4.2 was verified as already shipped (P3/#17764): collapsed-by-default tree + virtuoso search list, `MermaidBlock`→`CodeBlockInner` fence handling, "Copy source path" kebab, `code/**` read-only, graph `initialInclude="code"`. `WikiCodeMode.test.tsx` now pins those behaviors as the 4.2.1/4.2.2 acceptance evidence.

### TDD evidence (#17765)

- **Red**: `(cd web && npm test -- src/components/activity/wiki/__tests__/WikiCodeMode.test.tsx)` → `Tests 9 failed | 2 passed (11)` — promoted roots, collapsed mirror folders, reader mermaid/copy-source assertions (unreachable via the old wrapper root), freshness strip (all four), and both refresh-action tests failed; the two passes were the wiki-mode absence guard and the at-scale virtuoso list that §3.1 already provided.
- **Green**: same command after implementation → `Tests 11 passed (11)` plus the extended model test (40 passed across both files) on the first run.
- **Refactor/final green**: tsc flagged the fetch-mock call tuple (`call[1]`) — mock signature gained `_init?: RequestInit`; then `(cd web && npm test -- src/components/activity/wiki)` → `Test Files 10 passed (10)`, `Tests 144 passed (144)`; `npm run type-check` clean; `npx eslint <12 touched files> --max-warnings 0` clean.
- **Test-quality audit**: `uv run gobby test-quality audit web/.../WikiCodeMode.test.tsx web/.../WikiTabModel.test.ts web/.../WikiPageEditor.test.tsx --baseline .gobby/test-quality-baseline.json --fail-on-new --min-severity high` → 40 tests scanned, 0 issues, 0 new ≥ high.
- Coverage judgment: browser/UI level (vitest/jsdom over the WikiTab harness with stubbed daemon routes) — 4.2.1 promoted roots/collapse/expand/at-scale flat search, 4.2.2 mermaid diagram + python highlighting + Copy source path clipboard + read-only menu, 4.2.3 strip states (fresh/pending/unavailable), kebab wiring with resolved `root_path`, wiki-mode absence, and the not-accepted reason path; model-level unit coverage for `promoteRoot`. Gap: real polling cadence (30s interval) is untested — timer-based and low-risk; visual verification of the strip in both themes lands with §6.1.

## #17766 — Ask mode (§5.1)

**Seam survey (read-only)**

- §2.1 data layer already complete: `fetchAsk(scope, {query, llm, signal}, resolve)` → `WikiAskResult {status, degraded, answer, model, citations, groundingWarnings, hits, codeCitations, aiStatus, aiError}`. `citationsFromAnswer` extracts `[[wikilink]]` targets from the synthesized answer; `groundingWarnings` folds `citation_check.unsupported_claims` (prefixed "Unsupported claim: "), `ai.error`, and envelope `warnings`. `readEnvelope` throws `Error(errorMessage)` on any non-ok response — gwiki timeouts (`WikiError::Timeout`, code `"timeout"`) arrive as thrown errors, so the error+Retry path covers them.
- `WIKI_TAB_KEYS.askHistory` (`gobby:wiki-tab:ask-history`, sessionStorage) reserved in `WikiTabState.ts`; no helpers yet. `useWikiNav.openPage` auto mode-flips via `modeForPath` + WikiTab's `onNavigate → applyMode`.
- `ModeBody` in `WikiTab.tsx` renders the disabled ask placeholder — the replacement seam.
- `WikiPageReader` provides the reusable MarkdownBody pattern: `remarkWikilink` with `resolve` from `resolveWikilinkTarget(nodeIndex, …)` + an `a` component override on the `wikilink:` prefix.
- `SegmentedControl<T>` (`ui/SegmentedControl.tsx`): `value/onChange/options/ariaLabel/size` — the Extractive|Synthesized toggle.
- Fixtures already carry `askRetrievalEnvelope` (extractive: hits, no synthesis) and `askSynthesisEnvelope` (answer with resolved `[[knowledge/concepts/gobby|Gobby]]` + unresolved `[[code/files/src/gobby/wiki/watcher.py]]`, one `unsupported_claims` entry, one warning) — exactly the resolved/unresolved/grounding matrix §5.1 needs.

**Delta (this task)**

1. `WikiTabState.ts`: `AskHistoryEntry {id, question, llm, ts, envelope}`, `ASK_HISTORY_CAP = 20`, `loadAskHistory()/storeAskHistory()` over sessionStorage with defensive parsing.
2. NEW `wiki/WikiAskMode.tsx` — layout top→bottom: history list (question + mode chip + age; click restores; kebab rerun/delete), active answer area, composer pinned at bottom (textarea Enter/Shift+Enter, SegmentedControl Extractive|Synthesized → `llm`). Lifecycle: submit → pending + disabled composer + progress row (spinner + elapsed mm:ss + "Searching vault…" → "Synthesizing…" after 8s when llm + synthesized budget note) + Cancel (client AbortController, "server may keep working" title); thrown envelope errors → inline error + Retry; single-flight. Answer: MarkdownBody + wikilink plugin; citations chip row re-resolved against the live node index (pages fetched like WikiBrowse) — resolved → `nav.openPage` (auto mode-flip), unresolved → dashed-broken chip + "Search vault" fallback (invariant: never a silent dead link). Extractive answers render the hits list. Grounding warnings → ungrounded claims callout (icon + warning tokens + text). Gateway-down → info banner + disabled composer.
3. `WikiTab.tsx`: ModeBody renders `WikiAskMode` for ask (research placeholder stays); new `onSearchVault` sets toolbar search + flips to wiki mode.

**Test plan**: NEW `__tests__/WikiAskMode.test.tsx`, WikiCodeMode harness idioms (full WikiTab render, fetch stub keyed on pathname, mode seeded `"ask"`): extractive flow (hits render, no `llm` param), synthesized flow (`llm=true`, answer markdown), staged progress + elapsed + cancel (fake timers, deferred ask promise, AbortError rejection), error → Retry refires, citations invariant (resolved chip navigates + mode flips; unresolved chip marked broken + Search vault fills toolbar search), grounding callout present/absent, history (persist, restore, rerun, delete, cap 20), offline composer. Red = new test file against the placeholder.

### Implementation notes (#17766)

- NEW `wiki/WikiAskMode.tsx` (~470): the §5.1 grounded Q&A mode. History list on top (sessionStorage rows: question, Extractive/Synthesized chip, `formatRelativeTime` age; click restores the stored envelope; kebab Rerun/Delete), answer area in the middle, composer pinned at the bottom (textarea Enter/Shift+Enter, `SegmentedControl` Extractive|Synthesized, Ask button). The request lifecycle lives in module-level `runAskRequest` — the react-hooks/purity rule rejects `Date.now()` in render-scoped closures, so timestamps and the abort/error handling sit outside the component; single-flight is guarded by the in-flight `AbortController` ref (rerun-from-history bypasses the disabled composer, so the guard is in the runner, not just the UI).
- Staged progress: spinner (`motion-reduce:animate-none`, state carried by text) + "Searching vault…" → "Synthesizing…" after 8s when llm + elapsed mm:ss (1s interval over a `now` state) + "can take a few minutes" budget note + Cancel with the "server may keep working" title. Client aborts land in phase `idle` (no error); non-abort failures (incl. gwiki `timeout` envelopes, which `readEnvelope` throws) render `role="alert"` + Retry.
- Citation invariant: chips re-resolve `envelope.citations[].target` against the live node index (own `fetchPages` like WikiBrowse) at render — resolved chips `nav.openPage` (auto mode-flip via `modeForPath`), unresolved chips render dashed + explicit "unresolved" text + "Search vault" fallback. In-answer wikilinks share the invariant through the `a` override: resolved navigate, unresolved call `onSearchVault`. Extractive envelopes (no synthesis answer) render the hits list with openable wiki-page titles.
- Grounding warnings → the ungrounded claims callout: full border + `--color-warning-soft` bg + warning icon + `--color-warning-foreground` heading (no border-left stripe; text carries state), `role="status"` "Grounding warnings"; empty warnings render nothing.
- `WikiTabState.ts`: `AskHistoryEntry`, `ASK_HISTORY_CAP = 20`, `loadAskHistory`/`storeAskHistory` over sessionStorage (defensive parse, cap on read and write). Persistence via a history-change effect so all mutations are plain `setHistory` updates.
- `WikiTab.tsx`: ModeBody renders `WikiAskMode` for ask (research placeholder remains); `handleSearchVault` seeds the toolbar search then dirty-guard flips to wiki mode.
- Flake found and fixed during suite runs: the composer starts disabled until the first status fetch resolves (`summarizeWikiStatus(null, …)` → "unavailable"), so the test helper waits for the composer to enable before typing.

### TDD evidence (#17766)

- **Red**: `(cd web && npm test -- src/components/activity/wiki/__tests__/WikiAskMode.test.tsx)` → `Tests 13 failed | 1 passed (14)` — every ask-flow, staged-progress, citation, grounding, and history test failed against the placeholder; the single pass was the gateway-down guard the placeholder already satisfied (disabled textarea + banner copy).
- **Green**: same command after `WikiTabState` helpers + `WikiAskMode` + `WikiTab` wiring → `14 passed (14)` (one selector anchor fix: the row-button regex also matched its own kebab's "Actions for …" label).
- **Refactor/final green**: purity-lint refactor (module-level `runAskRequest`) + composer-enable wait in the test helper, then `(cd web && npm test -- src/components/activity/wiki)` → `Test Files 11 passed`, `Tests 158 passed (158)` (run twice for stability); `npm run type-check` clean; `npx eslint WikiAskMode.tsx WikiTabState.ts WikiTab.tsx WikiAskMode.test.tsx --max-warnings 0` clean.
- **Test-quality audit**: `uv run gobby test-quality audit web/.../WikiAskMode.test.tsx --baseline .gobby/test-quality-baseline.json --fail-on-new --min-severity high` → 14 tests scanned, 0 issues, 0 new ≥ high.
- Coverage judgment: browser/UI level (vitest/jsdom over the full WikiTab harness with stubbed daemon routes) — extractive/synthesized flows with `llm` param assertions, staged hint + elapsed + single-flight + cancel (fake timers, abortable deferred fetch), error + retry, resolved-citation navigation with mode flip, unresolved-citation marking + search fallback, grounding callout presence/absence, history persist/restore/rerun/delete/cap-20, gateway-down composer. Gaps: Shift+Enter newline (inverse of the tested Enter submit; trivial branch) and the in-answer wikilink `a` override (same resolver the chip tests cover) — both low-risk; visual verification in both themes lands with §6.1.

### Found-it-own-it: validation feedback classifier false positive (#17766)

First `close_task` attempt returned `validation_failed` while the LLM verdict's own feedback said "All acceptance criteria met" — the failure-precedence guard in `validate_leaf_task_with_llm` demoted the verdict because `_VALIDATION_GATE_THEN_ERRORS_REMAIN_RE` matched `'test.tsx (14 tests) covers … error+retry, resolved/unresolved'`: ask-mode domain vocabulary ("error+retry" states, "unresolved citations") read as "test … errors … unresolved" through the loose `.{0,40}` gap where "unresolved" was a bare adjective in an unrelated noun phrase, not a predicate on "errors". Fix (TDD, `src/gobby/tasks/_validation_feedback.py`): extracted `_ERRORS_REMAIN_PREDICATE` requiring remain/remaining/unresolved to predicate `errors` directly (bounded copula/adverb gap, `errors?\s+(?:(?:are|is|was|were|still|currently)\s+){0,2}(?:remain…|unresolved)`), used by both `_VALIDATION_GATE_THEN_ERRORS_REMAIN_RE` and `_ERRORS_REMAIN_THEN_VALIDATION_GATE_RE`. Red: 3 new false-positive regression cases (incident fragment + 2 distilled) failed against the old regexes; 3 new true-positive guards ("Test errors are unresolved…", "Errors remain unresolved in the coverage check.", "The lint gate reports errors still remaining…") passed before and after. Green: 93/93 in `test_lifecycle_validation_feedback.py`, then 409/409 across `tests/mcp_proxy/tools/tasks/` + `tests/tasks/test_validation.py`; ruff format+check clean, mypy clean. Daemon restart required for close_task to run the fixed classifier.

## #17767 — Research mode (§5.2)

**Seam survey**

- Data layer already landed with §2.1: `WikiTabData.launchResearch(scope, inputs)` POSTs `/api/pipelines/run {name:'wiki-research', inputs, project_id, background:true}` → `WikiResearchLaunch {executionId, status}`; `normalizePages` already carries the `outputs` array (`WikiOutputMeta {path, size, modified}`). `WikiTabActions.launchResearchRun` wraps it with the shared status line.
- `usePipelineExecutions` exists (`web/src/hooks/usePipelineExecutions.ts`): fetches `/api/pipelines/executions` with `project_id`/`status`/`pipeline_name` params, refetches on `pipeline_event` WS messages (500ms debounce), exposes `refetch`. Filters start empty — needs an option to seed `pipeline_name` so the first fetch is already scoped.
- `useWebSocketEvent.ts` singleton exposes no connection state; §5.2's "poll while the WebSocket is disconnected" needs a `useWebSocketConnected()` reader (module-level listener set updated in `onopen`/`onclose`).
- Status visuals: `--exec-status-*` tokens live in `styles/tokens.css` (dark + light); `getExecStatusKind` (lib/pipelineColors) maps execution status → `ActivityRowStatusDot` kind (shared/executions/execution-utils.tsx wraps it as PipelineStatusDot).
- Pipeline shape (`wiki-research.yaml`): inputs question/topic_slug/max_sources(12)/max_items(8)/create_tasks("true")/provider("claude")/model(""); meaningful steps `create_research_task → spawn_researcher → wait_researcher` (reentry_check/reentry_gate/researcher_failed are guards); outputs research_task_id/run_id/status.
- Restart-orphan marker: the §1.6 startup sweep (`fail_stale_running_executions`) marks RUNNING executions FAILED with `outputs_json = {"error": "Daemon restarted while execution was in progress"}` and step error `Daemon restarted` — the frontend detects orphaned runs by that marker.
- Tab escape hatch: no cross-tab navigation exists; `gobby:open-command-palette` CustomEvent is the precedent → add a `gobby:show-activity-tab` listener in `useActivityPanel` calling `showTab`.
- Provider/model selects: `lib/providerModels.ts` (`fetchProviderModelCatalog` cached, `getOrderedProviders`, `getModelsForProvider`) is the existing catalog surface (AgentEditForm precedent).
- Reader reuse: `WikiPageReader {scope, path, nav, nodeIndex, graph, onOpenGraph}` — research mode fetches pages itself (WikiAskMode precedent) for the node index; report wikilinks navigate through the passed-in `nav` (auto mode-flip).
- No existing test pins the research placeholder (shell test only asserts the mode toggle labels), so replacing the ModeBody branch is safe.

**Delta (this task)**

1. `hooks/usePipelineExecutions.ts`: add `pipelineName?: string` option that seeds the initial filters (first fetch already scoped; setFilters keeps working).
2. `hooks/useWebSocketEvent.ts`: track singleton connection state + notify a listener set from `onopen`/`onclose`; export `useWebSocketConnected(): boolean`.
3. `components/activity/useActivityPanel.ts`: `gobby:show-activity-tab` CustomEvent listener → `showTab(detail.tab)` for known tabs.
4. NEW `wiki/WikiResearchMode.tsx` — the §5.2 mode (behavior marker: single-flight research composer):
   - Composer card: question textarea; Options disclosure (topic_slug, max_sources 12, max_items 8, create_tasks Switch, provider/model selects from the catalog); "Run research" accent button. Disabled while a live execution exists ("A research run is in progress") or the gateway is down (info banner).
   - Launch via `actions.launchResearchRun`; immediate `refetch()` after the 202.
   - Live run card: `ActivityRowStatusDot` + status label, checklist for the three meaningful steps with per-step status, elapsed m:ss (`useNow`), "View in Pipelines tab" → `gobby:show-activity-tab`.
   - Completion strip on the newest completed run: "Open report" (newest `*-run-report.md` output) / "Open topic page" (topic from `outputs_json` topic keys, falling back to `inputs_json.topic_slug` → `knowledge/topics/<slug>.md`, only when resolvable).
   - Past runs: merged executions history (question from `inputs_json`, status, duration) + `*-run-report.md` outputs, recency-sorted; report rows open in `WikiPageReader` in place (back chevron returns); report wikilinks navigate via `nav` (mode auto-flip).
   - Resilient monitoring: 10s poll interval while a live execution exists OR `useWebSocketConnected()` is false; stall detection over a progress signature (execution id + updated_at + step statuses) with a bounded window; restart-orphaned runs (sweep marker in `outputs_json.error`) render the recovery card (warning icon + text + Refresh + Dismiss). Composer re-enables whenever no live execution remains.
5. `WikiTab.tsx`: research branch renders `WikiResearchMode` (placeholder retired); thread `actions` + `onOpenGraph` through ModeBody.

**Test plan** (red-first): NEW `__tests__/WikiResearchMode.test.tsx` (launch body incl. option overrides, single-flight disable, live step checklist + escape-hatch event, completion strip report/topic opens, merged past runs, report-wikilink topic navigation with mode flip, gateway-down) and NEW `__tests__/WikiResearchMode.recovery.test.tsx` (10s poll when WS disconnected, poll while run active, stall → recovery card with Refresh/Dismiss, restart-orphaned marker → recovery + composer re-enabled, composer re-enable when the run completes). Harness: WikiAskMode idioms + `/api/pipelines/*` + `/api/providers/models` routes; `useWebSocketEvent` module mocked (`useWebSocketConnected` per-test).

### Implementation notes (#17767)

- NEW `wiki/WikiResearchMode.tsx` (~710): the §5.2 launch/monitor mode. Composer card (question textarea, Options disclosure with topic_slug / max_sources / max_items / create_tasks Switch / provider+model selects from `fetchProviderModelCatalog`, accent "Run research"); launch goes through `actions.launchResearchRun` (shared status line surfaces failures) with an immediate `refetch()` after the 202. Single-flight research composer: any pending/running/waiting_approval execution disables the composer with "A research run is in progress".
- Live run card: `ActivityRowStatusDot` + `getExecStatusKind` (the `--exec-status-*` palette), checklist over the three meaningful pipeline steps (guard steps hidden), elapsed via `useNow(1000)` gated to live runs, "View in Pipelines tab" dispatching `gobby:show-activity-tab`. Completion strip (`role="status"`) offers "Open report" (newest `*-run-report.md` output) and "Open topic page" (slug from outputs_json falling back to inputs topic_slug, resolved against the live node index — only rendered when resolvable). Past runs merge non-live executions (question from inputs_json, status dot, duration) with report outputs (Report chip + relative age), recency-sorted; reports open in the shared `WikiPageReader` behind a "Back to research" chevron, and report wikilinks navigate through `nav` (auto mode-flip).
- Resilient monitoring: 10s poll interval active while a run is live OR `useWebSocketConnected()` is false — never WS-only. Stall detection counts unchanged progress-signature poll ticks (id + status + updated_at + step states) inside the interval callback (react-hooks/purity bans `Date.now()` in effect bodies, so ticks × interval replaces wall-clock deltas); 60s of no progress keys `stalledSignature`, and any observed progress clears the stall by derivation. Restart orphans are detected by the §1.6 sweep marker (`outputs_json.error` contains "Daemon restart"). Both render the warning recovery callout (full border + warning-soft bg + icon; no side stripes) with Refresh/Dismiss; the composer re-enables whenever no live execution remains.
- `hooks/usePipelineExecutions.ts`: `pipelineName` option seeds the initial filters so the first fetch is already scoped to wiki-research.
- `hooks/useWebSocketEvent.ts`: singleton connection tracking (`setConnected` on open/close/teardown) + `useWebSocketConnected()` via `useSyncExternalStore` (the set-state-in-effect lint steered away from a manual subscribe effect).
- `useActivityPanel.ts`: `gobby:show-activity-tab` CustomEvent listener → `showTab` for known tabs (`gobby:open-command-palette` precedent), so deep panel surfaces can switch tabs without prop threading.
- `WikiTab.tsx`: research branch renders `WikiResearchMode` (placeholder retired); header comment updated.

### TDD evidence (#17767)

- **Red**: `(cd web && npm test -- src/components/activity/wiki/__tests__/WikiResearchMode.test.tsx src/components/activity/wiki/__tests__/WikiResearchMode.recovery.test.tsx)` → `Tests 15 failed (15)` — every launch, options, single-flight, live-progress, completion, past-runs, polling, stall, and orphan test failed against the placeholder.
- **Green**: same command after the hook extensions + component + WikiTab wiring → `15 passed (15)` (one selector fix in my own test: "running" appears on both the header status and the running step, so the assertion moved to `getAllByText`).
- **Refactor/final green**: purity/compiler-lint refactor (tick-counting stall detection, `useSyncExternalStore`, memo drop) then `(cd web && npm test -- src/components/activity/wiki src/hooks/__tests__/usePipelineExecutions.test.ts src/hooks/__tests__/useWebSocketEvent.test.ts src/components/activity/__tests__/useActivityPanel.test.tsx)` → `Test Files 16 passed`, `Tests 223 passed (223)` (run twice); plus `src/components/activity/__tests__ src/components/shared` → 365 passed; `npm run type-check` clean; scoped `npx eslint … --max-warnings 0` clean.
- **Test-quality audit**: `uv run gobby test-quality audit <5 touched test paths> --baseline .gobby/test-quality-baseline.json --fail-on-new --min-severity high` → 65 tests scanned, 0 issues, 0 new ≥ high.
- Coverage judgment: browser/UI level (vitest/jsdom over the full WikiTab harness with stubbed `/api/pipelines/*`, `/api/providers/models`, and wiki routes) for the mode itself; hook level for the new infra (`pipelineName` seeding, `useWebSocketConnected` open/close/reconnect via the existing fresh-module WS harness, `gobby:show-activity-tab` tab switching incl. unknown-tab rejection). Gaps: elapsed-label ticking (cosmetic, `useNow` already covered elsewhere) and the WS-reconnect + poll interaction end-to-end (each half covered separately); visual verification in both themes lands with §6.1.

## #17768 — Polish and accessibility pass (§6.1)

**Baseline (§7 command gates)**: all clean before any edits — `npm run type-check` ✓, `npx vitest run src/components/activity/wiki` 173/173 ✓, `lint:js` / `lint:css` / `lint:tokens` ✓. The work is keyboard gaps, the 6.1.2 `WikiA11y.test.tsx` sweep, and DevTools walkthrough findings.

**Keyboard seam survey** (all four modes):

- Mode switcher: shared `SegmentedControl` already `role="radiogroup"` with roving tabIndex + arrow handling.
- Browse/code tree: `useTreeKeyboardNavigation` covers arrows + Enter/Space; **Home/End missing** (plan requires "tree arrows/Home/End"). Consumers: `WikiPageTree`, `TasksTabList` — additive fix in the shared hook.
- Quick-open: combobox pattern complete (Esc `preventDefault`+`stopPropagation`, arrows, Enter, `aria-activedescendant`).
- Editor: Cmd+S via shared `useDetailDraft` (covered by existing editor tests).
- Graph: `WikiForceGraph` `role="application"` tabIndex=0 with +/- zoom and arrow pan; `WikiGraphView` window-level Escape guarded by `event.defaultPrevented`; reduced-motion already threaded (0ms transitions, warmup/cooldown swap).
- Kebabs: shared `QuickMenu` menu pattern (arrows/Home/End/Enter/Esc, auto-focus first item, clamped fixed positioning). **Defect: Escape closes the menu but never restores focus to the trigger** — the focused menuitem unmounts and focus drops to `document.body`, stranding keyboard users (APG menu-button pattern requires focus return).
- Esc chain (quick-open → graph → kebab): quick-open stops propagation + prevents default; kebab prevents default; graph's window listener skips `defaultPrevented` events — each Esc peels exactly one layer. Correct as built.

**Fix list (TDD red-first):**

1. `useTreeKeyboardNavigation`: Home → first visible row, End → last visible row (selection follows focus). Unit tests in `useTreeKeyboardNavigation.test.tsx` + browse coverage in `WikiA11y.test.tsx`.
2. `QuickMenu`: Escape restores focus to the trigger button (no-op for anchor-positioned menus, which have no trigger). Unit test in `QuickMenu.test.tsx` + wiki kebab coverage in `WikiA11y.test.tsx`.
3. NEW `wiki/__tests__/WikiA11y.test.tsx` (acceptance 6.1.2): keyboard-only sweep — mode radiogroup arrows across all four modes, tree Home/End, kebab Esc focus-restore, code-mode tree operation, ask citation chips reachable/activatable, research options disclosure + switch keyboard toggles. Existing files already pin: tree arrows+Enter, Cmd+K open/Esc close, graph Esc/zoom, ask Enter submit, editor Cmd+S.

**Then**: DevTools walkthrough per recipe §7 — dark + light (ask mode both themes was explicitly deferred here from #17766), grayscale state legibility (dirty dot, run status, unresolved links, graph kinds), AA contrast spot-checks on wiki token usages, 390×844 + 360px-split reachability, reduced-motion, empty/loading/error/degraded per mode. Fixes land as found; any new color goes through `tokens.css`.

### Implementation notes (#17768)

Keyboard fixes (all red-first):

- `hooks/useTreeKeyboardNavigation.ts`: Home/End jump to the first/last visible row (`NavKey` union + `navigate` cases); consumers WikiPageTree + TasksTabList inherit. Also replaced the `requestAnimationFrame`-gated DOM focus with a `useLayoutEffect` keyed on a focus sequence — browsers suspend rAF in occluded windows, so queued focus moves either died silently or fired all at once on the next paint; the layout effect lands post-commit deterministically (proved live in a non-painting headless window where the rAF version demonstrably never moved focus). External selection sync still never steals DOM focus (only `focusRow` arms the pending ref).
- `activity/QuickMenu.tsx`: Escape now returns focus to the trigger (the focused menuitem unmounts, so focus was dropping to `<body>`); anchor-mode menus no-op (no trigger).
- Cmd+K conflict: inside the wiki pane the shortcut opened BOTH the wiki quick-open and the app command palette stacked. `WikiBrowse.handleKeyDown` now `stopPropagation()`s its claim, and `app/useAppKeyboardShortcuts.ts` gained a systemic `e.defaultPrevented` guard so any scoped surface that claims a key wins.
- NEW `wiki/__tests__/WikiA11y.test.tsx` (acceptance 6.1.2, 8 tests): mode radiogroup arrows/Home/End across all four modes, browse + code tree Home/End, quick-open keyboard select, Cmd+K claim (no window leak), kebab rove + Escape focus-restore, ask history reopen + citation chip activation by key, research disclosure/switch/launch by key. Existing files already pin tree arrows+Enter, Cmd+K open/Esc, graph Esc + zoom keys, ask Enter submit, editor Cmd+S.

Walkthrough findings fixed (each red-first where testable):

- **Graph parsed live data as permanently empty**: `normalizeGraph` read `payload.nodes`, but the real daemon envelope nests everything under `payload.graph.{nodes,edges,analytics,degraded,…}` (verified via authed curl: 1,243 knowledge / 3,638 all nodes). Normalizer now unwraps `payload.graph` when present; regression test pins the live shape. Post-fix the live graph renders 1,500 capped nodes with the cap chip.
- **Grayscale collapse of graph node kinds**: kinds borrowed clustered semantic tokens (L70–82 dark). New dedicated `--wiki-node-{page,source,code,citation,document}` tokens in `tokens.css` (both themes) form a ≥10–14pt lightness ladder so canvas dots separate without hue; `WIKI_NODE_COLOR_VARS` remapped. Grayscale screenshot now clearly separates kinds, legend included.
- **Empty graph rendered a bare canvas**: 0-node scenes now render `ActivityPanelEmpty` ("Nothing to graph") with a Refresh action, matching the fetch-error treatment.
- **Offline banner during normal load**: `summarizeWikiStatus` treated the in-flight first status fetch as an outage ("Browse, ask, and research are unavailable") on every mount. Added a `loading` summary state (`isLoading` threaded from `useWiki`); the banner stays quiet while loading, composers/actions stay disabled until status is known, and a real error still wins during retries.
- **Raw JSON in error copy**: gateway failures render `detail`'s embedded JSON envelope verbatim (`Error: {"code":…,"message":…}`). `humanizeWikiError` extracts the `message` field at the `errorMessage` throw funnel + the summarize fallback; tree/banner copy now reads the human message.
- **Narrow toolbar search grew vertically**: shared `.activity-panel-search` is `flex: 1 1 9rem`; the wiki toolbar's narrow branch dropped it directly into a column flex container (~200px-tall input on mobile + narrow desktop panels). Wrapped in a row.
- **Dev console flood**: provider-less `useArtifactContext` consumers (every code fence in the wiki reader) each warned; now warns once per load via a module function (React Compiler forbids global reassignment inside hook bodies).
- **Dead dev event socket**: `web/vite.config.ts` proxied `/ws` to the raw WebSocket port, which requires a Bearer header browsers can't send under 0.5.0 required auth — every `npm run dev` session had a permanent reconnect loop. Proxy now targets the daemon's HTTP `/ws` route, which authenticates the session cookie and injects the token itself.

### Verification evidence (#17768)

- **Red**: WikiA11y + hook/QuickMenu additions → `6 failed` (tree Home/End ×3, kebab focus-restore ×2, + selector fix); Cmd+K conflict tests → `2 failed`; graph empty-state → red; nested-envelope, loading-state, humanize-error, warn-once → red each before their fix.
- **Green/final**: `npx vitest run` over all 23 touched suites → `239 passed (239)` (run twice); `npm run type-check`, `lint:js --max-warnings 0`, `lint:css`, `lint:tokens` all clean.
- **Test-quality audit**: 8 touched test files, 95 tests, 0 issues, 0 new ≥ high.
- **Live keyboard walkthrough** (Chrome DevTools MCP against the dev server, real daemon data): tree arrows/Home/End move real DOM focus (incl. in a non-painting window post-fix), tree Enter opens pages, Cmd+K opens only the wiki quick-open, quick-open type→Enter navigates, kebab Enter/arrows/Escape with focus returning to the trigger, graph +/− zoom & arrow pan on the `role=application` canvas, Escape peels exactly one layer (kebab → quick-open → graph), `gobby:show-activity-tab` escape hatch verified switching real tabs.
- **Headless visual sweep** (playwright-core + Chrome for Testing, screenshots in session scratchpad): dark + light × browse/graph/ask/research (ask both themes closes the #17766 deferral); grayscale browse + graph (kind ladder separates); AA spot-checks via canvas-normalized sRGB (light: tree 16.4, muted 7.73, mode radio 17.4; dark: tree 15.5, muted 6.8 — all ≥ 4.5); 390×844 + 360px: no horizontal scroll, mode radios/graph button reachable, stacked tree/reader usable, breadcrumbs render (44px touch targets ship via `pointer-coarse:` variants); reduced-motion context renders a settled graph; empty (graph "Nothing to graph", browse ⌘K hint, ask teaching copy), degraded/offline (personal-vault project: humanized banner), and loading (no phantom outage banner) audited. Console: no errors; one intentional dev-only warning per load.
- Environment notes: the walkthrough authenticated with a short-lived minted `gobby_session` row (2h expiry); wiki requests were pinned to the populated project via an injected fetch wrapper — a data-scoping harness, not a product change.
