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
