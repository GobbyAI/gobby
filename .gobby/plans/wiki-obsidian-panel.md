# Obsidian-Grade Wiki Activity Panel (wiki / code / ask / research)

## Overview
`kind: framing`

The June 2026 wiki bakeoff (`docs/evidence/wiki-bakeoff-2026-06/`) proved gwiki/codewiki are **parity-plus on the backend** against Graphify, DeepWiki-Open, CodeWiki, OpenDeepWiki, and llm-wiki — but it explicitly scoped UI/UX out. Gobby's presentation layer today is "open the vault in Obsidian," and the web app's Wiki tab is a sources-management table. This epic builds the missing presentation half: an Obsidian-grade experience inside the activity panel with four modes — **Wiki** (browse/edit knowledge pages), **Code** (browse codewiki), **Ask** (grounded RAG with verified-navigable citations), and **Research** (launch and monitor wiki-research pipeline runs) — plus a 2D force-directed graph over the already-computed wikilink+code-edge graph.

Parity-plus targets vs the bakeoff field: citations that are *verified navigable or explicitly marked broken* (DeepWiki-Open shipped 63/63 dead citation hrefs — its W1 weakness); rendered mermaid diagrams (bakeoff gap C4); an interactive graph fusing wikilinks **and** code edges (no competitor has one; Obsidian itself has no code-graph layer); and Obsidian-core interactions (wikilink navigation, backlinks pane, quick-open, edit-in-place) on top of gwiki's hybrid retrieval.

Prior exploration is recorded in this session; backend and frontend designs were produced by dedicated planning passes and reconciled here. User decisions locked: 2D Obsidian-style graph (`react-force-graph-2d`), read+edit pages, research mode with live monitoring, all four modes inside the single existing `wiki` activity tab.

## Constraints
`kind: framing`

- **Pre-0.5.0: no backward compatibility.** Replace the existing WikiTab UI wholesale; keep `useWiki.ts` low-level fetch helpers and `WikiSourceRemovalDialog.tsx` where reusable.
- **Vault is filesystem-first.** `wiki/` markdown is source of truth; `gwiki index` (incremental, content-hash diff) syncs Postgres (`gwiki_documents`, `gwiki_chunks`, `gwiki_links`). The daemon `WikiWatcher` + `WikiUpdateCoordinator.handle_write_result` already await reindex for write commands that emit `changed_paths`.
- **All wiki HTTP routes carry `?project=&topic=` scope params**; envelope shape is `{ok, command, payload, stderr, index_handoff}`.
- **Timeout budget:** `INTERACTIVE_GWIKI_TIMEOUT_SECONDS=30.0`, `GENERATION_GWIKI_TIMEOUT_SECONDS=270.0` (`src/gobby/gwiki_gateway.py`); generation must stay under the 300s transport ceiling.
- **gwiki binary deployment:** Rust changes are live only after `cargo build --release -p gobby-wiki` and reinstalling `target/release/gwiki` → `~/.gobby/bin/gwiki`.
- **Frontend conventions:** file family under `web/src/components/activity/wiki/` per `docs/guides/one-surface-tab-recipe.md`; non-test source files < 1,000 lines; Tailwind 4 + OKLCH tokens (`web/src/styles/tokens.css`); no `rehype-raw`; no redux/react-query — bespoke hooks + `useWebSocketEvent`.
- **react-kapsule footgun:** `react-force-graph` re-applies props on parent re-render and destroys custom canvas objects — graph components must be ref-isolated, memoized, error-boundaried (pattern: `web/src/components/activity/memory/MemoryGraphView.tsx`).
- **Design contract:** `.impeccable.md` governs — dark default with first-class light, brand accent hue 125, deutan-safe (state never rides on hue alone), WCAG 2.2 AA, keyboard-first, minimal motion, no border-left stripes/gradient text/neon-on-dark.
- **Decisions (not open questions):** no wiki WebSocket event in this epic — UI refetches after its own mutations plus manual refresh; topic scope is a free-text input persisted per panel (no topics-list endpoint); code-page → source navigation ships as "Copy source path" (no cross-tab Files hop); wikilink hover shows title+path tooltip (no excerpt popover); no per-page local-graph pane (backlinks pane covers it at 360px; the graph wrapper accepts arbitrary subsets so it remains a thin follow-up).
- **Non-goals:** editing generated `code/**` pages (read-only by confinement); embed transclusion `![[page]]` rendering (degrades to a link); mobile-specific layouts beyond the panel's existing responsive behavior; Obsidian plugin compatibility.

## Design Direction
`kind: framing`

Per `.impeccable.md`: industrial, efficient, slick — Vercel/Linear restraint at product density. The panel must read as *the same product* as Sessions/Tasks/Files: `SegmentedControl` mode switcher (`controlHeight="sm"`), `ActivityPanelSearch`, `ResizeHandle` splits, `QuickMenu` kebabs, `fields/` draft primitives. Hierarchy from type + spacing, not chrome. Graph node kinds are encoded by color **plus** shape/ring cues and a persistent legend (deutan-safe); unresolved targets are hollow with a dashed treatment, never hue-only. Prose columns cap ~70ch. Both themes get equal polish; canvas and mermaid colors resolve from tokens via `resolveCssVar` and rebuild on theme flip. Motion: graph settles via pre-simulation under `prefers-reduced-motion`; CSS transitions ≤150ms.

## P1: Backend wiki surfaces (Rust gwiki + Python daemon)
`kind: framing`

**Goal**: Every capability the UI needs is served over HTTP with vault-confinement enforced in Rust: graph JSON, lightweight page listing, page write/delete with awaited reindex, detached pipeline runs, and codewiki refresh status.

### 1.1 Add `--stdout` and `--include` to `gwiki graph` [category: code]
`kind: deliverable`

Domain: backend (Rust).

Targets: `crates/gwiki/src/commands/graph.rs`, `crates/gwiki/src/graph/mod.rs`, `crates/gwiki/src/main.rs`, `crates/gwiki/src/api.rs`, `crates/gwiki/src/commands/mod.rs`

Today `gwiki graph` only writes artifacts (`wiki/outputs/graph.json`, `GRAPH_REPORT.md`). Add flags so the daemon can fetch fresh graph JSON on demand:

- `CliCommand::Graph` (clap lives in `crates/gwiki/src/main.rs`) gains `GraphArgs { #[arg(long)] stdout: bool, #[arg(long, value_enum, default_value = "all")] include: GraphInclude }` with `GraphInclude::{Knowledge, Code, All}`. Thread through `api.rs` as `Command::Graph { scope, options: GraphCommandOptions }` and dispatch in `commands/mod.rs`.
- `--stdout`: skip `export_graph_artifacts`; emit JSON payload `{"command":"graph","scope":…,"graph": GraphExport}` where `GraphExport` is the existing serializable type (`nodes`, `edges: {links, imports, calls, callers, trust, audit}`, `analytics: {communities, centrality, bridges, god_nodes, unexpected_links, hotspots}`, `degraded`, `degraded_sources`). Without `--stdout`, behavior is byte-identical to today (artifacts written).
- `--include`: new `WikiGraphFacts::retain_include(include)` in `graph/mod.rs` — `knowledge` retains documents whose graph path starts with `knowledge/` plus recaps/root pages, their sources/citations/unresolved targets, and drops `code_edges`; `code` retains `code/**` documents plus code edges; `all` is a no-op. Analytics run on filtered facts automatically via the existing `export_graph`.
- Global `--format json` / `--project` / `--topic` clap args already apply, so `GwikiGateway._run_json` works unchanged.

Validation: `cargo test -p gobby-wiki`, `cargo clippy -p gobby-wiki`, then `cargo build --release -p gobby-wiki` and reinstall `~/.gobby/bin/gwiki`.

**Acceptance:**

- 1.1.1 - `gwiki graph --stdout` prints a JSON envelope containing the full `GraphExport` and writes no artifact files. symbol: `gwiki::commands::graph::execute`.
- 1.1.2 - `retain_include` filters facts by scope with analytics recomputed on the filtered set. test: `crates/gwiki/src/graph/mod.rs::retain_include_knowledge_drops_code_edges`.
- 1.1.3 - Default (no `--stdout`) behavior remains artifact-writing and unchanged. behavior: "graph artifact regression" in `crates/gwiki/src/commands/graph.rs` tests.

### 1.2 Add `gwiki pages` listing + outputs read allowlist [category: code]
`kind: deliverable`

Domain: backend (Rust).

Targets: `crates/gwiki/src/commands/pages.rs` (new), `crates/gwiki/src/commands/read.rs`, `crates/gwiki/src/main.rs`, `crates/gwiki/src/api.rs`, `crates/gwiki/src/commands/mod.rs`

The UI's file tree and codewiki tree must not require the multi-MB graph payload. Add a lightweight DB-backed listing:

- New `gwiki pages [--prefix <p>]`: query `gwiki_documents` for the resolved scope selecting `path, title, frontmatter, content_hash, updated_at` (verify exact timestamp column name in `crates/gwiki/src/setup.rs` during implementation); extract `tags` from frontmatter JSONB. Additionally walk the vault's outputs directory for markdown files on the filesystem (unindexed by design) into a separate array. Payload:

```json
{"command":"pages","scope":"…",
 "pages":[{"path":"knowledge/topics/foo.md","title":"Foo","tags":["rust"],"content_hash":"…","updated_at":"…"}],
 "outputs":[{"path":"outputs/foo-run-report.md","size":1234,"modified":"…"}]}
```

- Extend `is_readable_wiki_path` (`crates/gwiki/src/commands/read.rs:234`) to allow `outputs/**` so `gwiki read --path outputs/<file>.md` renders run reports and `GRAPH_REPORT.md`. Reads only — writes to `outputs/**` stay blocked (see 1.3).
- Include the page's `content_hash` in both the `pages` listing entries and the `gwiki read` payload — this is the revision baseline the editor holds for the conditional-write contract in 1.3/3.2.

Validation: crate tests + clippy + release build + reinstall, as in 1.1.

**Acceptance:**

- 1.2.1 - `gwiki pages` returns all indexed pages with path/title/tags/content_hash (read payload also carries content_hash) and a separate `outputs` array. file: `crates/gwiki/src/commands/pages.rs`.
- 1.2.2 - `--prefix code/` restricts the listing to codewiki pages. test: `crates/gwiki/src/commands/pages.rs::prefix_filters_listing`.
- 1.2.3 - `gwiki read --path outputs/<report>.md` succeeds; `outputs/**` remains excluded from indexing. test: `crates/gwiki/src/commands/read.rs::outputs_paths_are_readable`.

### 1.3 Add `gwiki page write|delete` with vault confinement [category: code]
`kind: deliverable`

Domain: backend (Rust).

Targets: `crates/gwiki/src/commands/page.rs` (new), `crates/gwiki/src/commands/read.rs`, `crates/gwiki/src/main.rs`, `crates/gwiki/src/api.rs`, `crates/gwiki/src/commands/mod.rs`

Vault mutation belongs in Rust where scope-root resolution and path normalization already live (`normalize_requested_path` in `read.rs`) — the daemon treats paths as opaque and CLI/MCP/HTTP stay mirrored:

- Hoist `normalize_requested_path` out of `read.rs` into a shared `pub(crate)` helper used by read and page commands.
- `gwiki page write --path <relative page path> [--mode upsert|create]` — content read from **stdin** (avoids argv length/quoting limits). Validation: relative path, traversal-normalized, markdown extension required, prefix must be `knowledge/**` — writes to `code/**` (generated), `outputs/**`, `raw/**`, `meta/**`, `.obsidian/**` are rejected. `--mode create` errors if the file exists; `upsert` creates parent dirs inside `knowledge/`. Content written verbatim (frontmatter round-trips untouched; `gwiki normalize` remains a separate pass). Canonicalize the resolved parent against the vault root to close symlink escapes. Conditional-write precondition: optional `--expected-hash <sha256>` compares against the current on-disk content hash before writing (for `--mode create`, the precondition is nonexistence); mismatch returns a distinct precondition-failed error envelope and leaves the file untouched. Payload: `{"command":"page-write","scope":…,"path":…,"created":bool,"bytes":n,"content_hash":sha,"changed_paths":[path]}`.
- `gwiki page delete --path <relative page path>` — same confinement; missing file → error envelope; payload `{"command":"page-delete",…,"changed_paths":[path]}`. The incremental indexer's `IndexEvent::Deleted` path prunes DB rows.

Validation: crate tests + clippy + release build + reinstall, as in 1.1.

**Acceptance:**

- 1.3.1 - `gwiki page write` upserts a `knowledge/**` page from stdin and reports `changed_paths`. file: `crates/gwiki/src/commands/page.rs`.
- 1.3.2 - Writes outside `knowledge/**` and traversal/symlink escapes are rejected. test: `crates/gwiki/src/commands/page.rs::write_rejects_confinement_violations`.
- 1.3.3 - `--mode create` on an existing page returns a distinct already-exists error. test: `crates/gwiki/src/commands/page.rs::create_mode_conflicts_on_existing`.
- 1.3.4 - `gwiki page delete` removes the file and emits `changed_paths` so reindex prunes derived rows. behavior: "delete reindex prune" in `crates/gwiki/src/commands/page.rs` tests.
- 1.3.5 - `--expected-hash` mismatch returns a distinct precondition-failed error and leaves the file untouched. test: `crates/gwiki/src/commands/page.rs::write_precondition_hash_mismatch`.

### 1.4 Python read surfaces for graph and pages routes [category: code] (depends: 1.1, 1.2)
`kind: deliverable`

Domain: backend.

Targets: `src/gobby/gwiki_gateway.py`, `src/gobby/servers/routes/wiki.py`, `src/gobby/servers/_app_routes.py`, `tests/servers/routes/test_wiki_routes.py`

- `GwikiGateway.graph(self, *, include: str = "all") -> dict[str, Any]` → `self._run_json("graph", ["graph", "--stdout", "--include", include])`; `GwikiGateway.pages(self, *, prefix: str | None = None) -> dict[str, Any]`. Both interactive-timeout.
- Routes on the existing `create_wiki_router`: `GET /api/wiki/graph?project=&topic=&include=all|knowledge|code` and `GET /api/wiki/pages?project=&topic=&prefix=`, returning the standard envelope; validate `include` against the enum with a 400 validation-error envelope on mismatch.
- Add starlette `GZipMiddleware` to the FastAPI app (graph JSON compresses ~10x; no gzip middleware exists today).

`wiki.py` is 482 lines and `gwiki_gateway.py` 440 — additions fit; if implementation pushes `wiki.py` near 1,000, split read routes into a sibling routes module and register a refactor task per repo rules.

**Acceptance:**

- 1.4.1 - Gateway exposes `graph()`/`pages()` and routes serve them with scope params. symbol: `gobby.gwiki_gateway.GwikiGateway.graph`.
- 1.4.2 - `GET /api/wiki/graph?include=knowledge` returns a filtered graph envelope; invalid `include` yields a 400 validation envelope. test: `tests/servers/routes/test_wiki_routes.py::test_graph_route_include_validation`.
- 1.4.3 - Responses are gzip-compressed when the client sends `Accept-Encoding: gzip`. test: `tests/servers/routes/test_wiki_routes.py::test_gzip_enabled`.

### 1.5 Python write surfaces and MCP parity [category: code] (depends: 1.3)
`kind: deliverable`

Domain: backend.

Targets: `src/gobby/gwiki_gateway.py`, `src/gobby/servers/routes/wiki.py`, `src/gobby/wiki/update_coordinator.py`, `src/gobby/mcp_proxy/tools/wiki.py`, `tests/servers/routes/test_wiki_routes.py`

- Gateway: thread `stdin_data: bytes | None = None` through `_run_json` → `_run_command` (`stdin=asyncio.subprocess.PIPE`, `proc.communicate(input=stdin_data)`); add `write_page(self, *, path: str, content: str, mode: str = "upsert", expected_hash: str | None = None)` — appending `--expected-hash <sha256>` to the `gwiki page write` argv when set — and `delete_page(self, *, path: str)`. Route and MCP layers pass `expected_hash` through this gateway parameter verbatim; the precondition must never be droppable at the gateway boundary.
- Routes: `POST /api/wiki/write?project=&topic=` body `{path, content, mode?="upsert", expected_hash?}`; `POST /api/wiki/delete?project=&topic=` body `{path}` (POST-with-body matches the router's action-verb style). Both go through the existing `_write_call` helper so the response carries `index_handoff` and returns only **after** reindex completes. Map gwiki already-exists → 409, missing-file → 404, and expected-hash precondition failure → 412.
- Add `"page-write"`, `"page-delete"` to `EXPLICIT_WRITE_COMMANDS` in `src/gobby/wiki/update_coordinator.py`.
- MCP: add `wiki_write_page(path, content, mode="upsert", expected_hash=None, project=None, topic=None)` and `wiki_delete_page(path, project=None, topic=None)` to `src/gobby/mcp_proxy/tools/wiki.py` via the existing `write_call` helper; interactive timeout (no `EXTENDED_TIMEOUT_TOOL_NAMES` change). Deliberately **no `wiki_graph` MCP tool** — a multi-MB tool result poisons agent context; agents keep `gwiki graph` artifacts and graph-context packs. Record that rationale in the registry docstring.

**Acceptance:**

- 1.5.1 - `POST /api/wiki/write` persists content verbatim and responds only after reindex handoff. test: `tests/servers/routes/test_wiki_routes.py::test_write_awaits_reindex`.
- 1.5.2 - Create-mode conflict maps to 409, missing-page delete to 404, and expected-hash mismatch to 412 — with the test asserting a stale hash reaches the gwiki argv through the reindex-backed write path. test: `tests/servers/routes/test_wiki_routes.py::test_write_delete_error_mapping`.
- 1.5.3 - `wiki_write_page`/`wiki_delete_page` MCP tools exist with vault-confinement errors surfaced. symbol: `gobby.mcp_proxy.tools.wiki.create_wiki_registry`.

### 1.6 Detached pipeline runs: `background` flag on `POST /api/pipelines/run` [category: code]
`kind: deliverable`

Domain: backend.

Targets: `src/gobby/workflows/pipeline_executor.py`, `src/gobby/servers/routes/pipelines.py`, `tests/workflows/test_pipeline_executor.py`

`POST /api/pipelines/run` currently awaits `executor.execute()` to completion — a wiki-research run would hold the HTTP request open for up to ~1h (`wait_researcher` timeout 3600s). Add a generic detached capability:

- Refactor the record-creation block of `PipelineExecutor.execute` (definition snapshot + `execution_manager.create_execution`, lines ~206–219) into `_create_execution_record`.
- `start_detached(self, pipeline, inputs, project_id, session_id=None) -> PipelineExecution`: create the record, then `asyncio.create_task(self.execute(..., execution_id=execution.id))` retained in `self._detached_tasks: set[asyncio.Task]` with a discard + exception-logging done-callback (mirror `CronExecutor._background_tasks`). The existing resume path accepts a fresh non-terminal record (only CANCELLED/COMPLETED are rejected).
- Route: `PipelineRunRequest` gains `background: bool = False`; when true, return `202 {"status":"running","execution_id","pipeline_name"}` immediately. Existing `pipeline_event` WS broadcasts (`pipeline_started|step_started|step_completed|step_skipped|pipeline_completed|pipeline_failed`) and `GET /api/pipelines/executions*` provide monitoring unchanged.

- Startup sweep: on executor startup, mark RUNNING executions whose background task no longer exists as FAILED with a daemon-restart note. This covers detached runs orphaned by restart and heals the same pre-existing exposure for approval-parked runs — without it, the research UI (5.2) would poll a phantom RUNNING execution forever.

**Acceptance:**

- 1.6.1 - `start_detached` returns immediately with a RUNNING execution that completes in the background. test: `tests/workflows/test_pipeline_executor.py::test_start_detached_completes`.
- 1.6.2 - `POST /api/pipelines/run` with `background: true` responds 202 with `execution_id` while steps stream over `pipeline_event`. symbol: `gobby.workflows.pipeline_executor.PipelineExecutor.start_detached`.
- 1.6.3 - Executor startup marks restart-orphaned RUNNING executions FAILED. test: `tests/workflows/test_pipeline_executor.py::test_startup_sweep_marks_orphans_failed`.

### 1.7 Codewiki refresh status endpoint [category: code]
`kind: deliverable`

Domain: backend.

Targets: `src/gobby/code_index/codewiki_trigger.py`, `src/gobby/servers/routes/code_index.py`, `tests/code_index/test_codewiki_trigger.py`

Codewiki refresh is fire-and-forget today; the Code mode needs to show freshness:

- `CodewikiRefreshTrigger.status()` returning pending root keys, active flush tasks, and last-run outcome/timestamps — store the last `CodewikiRefreshResult` (or error) in `_run_refresh`.
- `GET /api/code-index/codewiki/status` in `src/gobby/servers/routes/code_index.py` returning that snapshot alongside the existing refresh POST.

**Acceptance:**

- 1.7.1 - Trigger records and exposes last refresh outcome and pending state. symbol: `gobby.code_index.codewiki_trigger.CodewikiRefreshTrigger.status`.
- 1.7.2 - `GET /api/code-index/codewiki/status` returns the snapshot. test: `tests/code_index/test_codewiki_trigger.py::test_status_endpoint_snapshot`.

## P2: Frontend foundation (depends: P1)
`kind: framing`

**Goal**: Typed data layer, the four-mode shell replacing the old WikiTab, and the markdown extensions (wikilinks, mermaid) every mode renders through.

### 2.1 Wiki data layer + model rewrite [category: code]
`kind: deliverable`

Domain: frontend.

Targets: `web/src/components/activity/wiki/WikiTabModel.ts`, `web/src/components/activity/wiki/WikiTabData.ts`, `web/src/components/activity/wiki/__tests__/`

Rewrite the wiki data layer against the new backend contracts (keep old exports compiling until 2.2 deletes the old UI):

- `WikiTabModel.ts` (~350): types `WikiMode = 'wiki' | 'code' | 'ask' | 'research'`, `WikiGraphPayload`, `WikiPageMeta`, `WikiOutputMeta`, `PageTreeNode`; path helpers `pageKindFromPath`, `breadcrumbSegments`, `codePathToSourcePath("code/files/src/gobby/runner.py.md") → "src/gobby/runner.py"`; `buildPageTree(pages, outputs, rootFilter)` or a pre-merged `pages + outputs` input grouping by path segments (`knowledge/{concepts,topics,sources}`, `recaps/`, `outputs/`, root pages `_index`, `log`; code mode: `INDEX`, `repo`, `modules/`, `files/**` mirror); `buildNodeIndex(pages)` producing path→meta and title/alias→path maps for wikilink + citation resolution; node color/size mapping tables for the graph (kind → token var, `val = 2 + 3*sqrt(degree)` clamped).
- `WikiTabData.ts` (~400): typed fetchers over the envelope — `fetchGraph(scope, include)`, `fetchPages(scope, prefix?)`, `fetchPage(path|title)`, `fetchBacklinks(target)`, `fetchSearch`, `fetchAsk({query, llm, signal})`, `savePage(path, content, mode, expectedHash)` → `POST /api/wiki/write` (412 normalized to a typed conflict result), `createPage`, `deletePage` → `POST /api/wiki/delete`, `launchResearch(inputs)` → `POST /api/pipelines/run {name:'wiki-research', background:true}`; normalizers `normalizeGraph`, `normalizePages`, `normalizePage` (frontmatter split via `js-yaml`, already a dep), `normalizeAskAnswer` (answer markdown, citations `{target, title, resolvedPath|null}`, grounding warnings — field names normalized defensively in the established `asRecord`/`fieldText` style against fixture envelopes captured from live `gwiki ask` output), `normalizeBacklinks`.
- **Tree and citation resolution use the lightweight `pages` listing; the graph payload is fetched lazily only for the graph view and unresolved-mentions data.**
- Tests use fixtures shaped from `wiki/outputs/graph.json` (1,846 nodes in the current vault: 835 `unresolved_target`, 337 `source` + 337 `citation`) and live envelope captures.

**Acceptance:**

- 2.1.1 - Model types, tree builder, node index, and path helpers exist with unit tests. file: `web/src/components/activity/wiki/WikiTabModel.ts`.
- 2.1.2 - Fetchers and defensive normalizers cover graph/pages/page/backlinks/ask/write/delete/research. file: `web/src/components/activity/wiki/WikiTabData.ts`.
- 2.1.3 - Tree building, wikilink target resolution, and ask normalization are pinned by fixture tests. test: `web/src/components/activity/wiki/__tests__/WikiTabModel.test.ts`.

### 2.2 Mode shell, toolbar, navigation, sources port [category: code] (depends: 2.1)
`kind: deliverable`

Domain: frontend.

Targets: `web/src/components/activity/WikiTab.tsx`, `web/src/components/activity/wiki/WikiTabToolbar.tsx`, `web/src/components/activity/wiki/WikiTabActions.ts`, `web/src/components/activity/wiki/WikiSourcesManager.tsx`, `web/src/components/activity/ActivityPanel.tsx`, `web/src/components/activity/wiki/WikiTabList.tsx` (deleted), `web/src/components/activity/wiki/WikiDetailPanel.tsx` (deleted)

Replace `WikiTab.tsx` with the four-mode shell (the `wiki` tab id is already registered — no tab registration work):

- Shell (~250): mode state via `SegmentedControl` ("Wiki | Code | Ask | Research", `controlHeight="sm"`), scope state `{projectId, topic}` (topic = free-text input in the kebab, persisted), `useWikiNav()` — `{current, openPage(path, opts), back(), forward(), canBack, canForward}` with an in-memory history stack (cap 50); `openPage` flips mode automatically (`code/` prefix → code mode) and is dirty-guarded; container-width measurement via `ResizeObserver` (wide ≥560px vs narrow) — the repo bans Tailwind `md:` variants for this.
- Persistence keys: `gobby:wiki-tab:mode`, `:topic`, `:tree-width`, `:split`, `:last-page:wiki`, `:last-page:code`, `:graph` (JSON settings); sessionStorage `gobby:wiki-tab:ask-history`.
- Toolbar (~200): segmented control, `ActivityPanelSearch` (filters current mode's tree/list), graph button (wiki+code modes), kebab `QuickMenu`: New page, Quick open, Refresh index, Compile…, Audit, Attach file…, Ingest URL…, Manage sources…, Topic scope….
- `WikiTabActions.ts` (~250): orchestration — save→refetch page/pages/backlinks, create→navigate, delete→confirm+navigate-back, launchResearch, compile/audit/refresh/attach/ingest ported from the old toolbar and `WikiChatActions`.
- `WikiSourcesManager.tsx` (~250): the old sources list/detail capability as an in-pane view opened from the kebab, reusing `WikiSourceRemovalDialog`.
- `ActivityPanel.tsx`: `case "wiki"` gains `requestPanelOverride`/`releasePanelOverride` props (mirrors `case "memory"`) for the graph's full-width takeover.
- Degraded banner: existing `buildWikiSummary` normalizers drive one slim banner under the toolbar (info icon + "Wiki degraded: <services>" + details popover); ask/research composers disable when the gateway is down; browse keeps working.
- Delete the old wiki list/detail components (targets above) and the old `WikiTab.tsx` body; mode bodies render placeholders until P3–P5 land.

**Acceptance:**

- 2.2.1 - Four-mode shell with persisted mode/scope/nav replaces the old WikiTab. file: `web/src/components/activity/WikiTab.tsx`.
- 2.2.2 - Toolbar, kebab actions, and sources manager port work against the actions layer. file: `web/src/components/activity/wiki/WikiTabToolbar.tsx`.
- 2.2.3 - Wiki tab participates in panel override like MemoryTab. behavior: "wiki case passes requestPanelOverride" in `web/src/components/activity/ActivityPanel.tsx`.
- 2.2.4 - Mode switch and page navigation are dirty-guarded. test: `web/src/components/activity/wiki/__tests__/WikiTab.shell.test.tsx`.

### 2.3 Wikilink remark plugin + MarkdownBody extension seam [category: code]
`kind: deliverable`

Domain: frontend.

Targets: `web/src/lib/markdown/remarkWikilink.ts`, `web/src/components/shared/MarkdownBody.tsx`, `web/src/lib/markdown/__tests__/remarkWikilink.test.ts`

- `remarkWikilink.ts`: mdast transformer over `text` nodes; regex `/(!?)\[\[([^\]|]+?)(?:\|([^\]]+?))?\]\]/g`; splits text nodes and inserts standard `link` nodes with `url: 'wikilink:' + encodeURIComponent(target)`, child text = alias ?? prettified last path segment; `data.hProperties = { className: 'wikilink' | 'wikilink wikilink--unresolved', 'data-wiki-target': target }`. Options `{ resolve?: (target) => { path } | null }` — resolver comes from the 2.1 node index (exact path match sans `.md`, then title/alias). Without a resolver, links render resolved-optimistic. Embeds (`![[…]]`) degrade to plain links. Pure remark — no rehype-raw, no HTML parsing. Wikilink targets in this vault are **paths with aliases** (e.g. `[[knowledge/sources/src-…|Session: 019efb0c]]`).
- `MarkdownBody.tsx`: add optional `remarkPlugins?: PluggableList` and `components?: Partial<Components>` props merged over the defaults (`remarkGfm`, `codeBlockComponents`); default behavior byte-identical; the memo block key must include a plugins/components identity hash so cached blocks re-render when the plugin set changes.
- Styling: resolved wikilinks = accent underline-on-hover; unresolved = dashed underline + `--text-muted` + `aria-description="Page not created yet"` (never hue-only).

**Acceptance:**

- 2.3.1 - Plugin handles alias, anchor, unresolved, adjacent links, and embed degradation. test: `web/src/lib/markdown/__tests__/remarkWikilink.test.ts`.
- 2.3.2 - `MarkdownBody` accepts plugin/component extensions with unchanged defaults. file: `web/src/components/shared/MarkdownBody.tsx`.

### 2.4 Mermaid rendering [category: code] (depends: 2.3)
`kind: deliverable`

Domain: frontend.

Targets: `web/src/components/shared/MermaidBlock.tsx`, `web/package.json`

Codewiki pages carry mermaid fences; rendering them beats the bakeoff C4 gap in the reader:

- `MermaidBlock.tsx`: a `code` component override intercepting `language-mermaid` fences (plain fenced blocks — compatible with no-rehype-raw). Lazy `import('mermaid')` singleton; `initialize({ startOnLoad:false, securityLevel:'strict', theme:'base', themeVariables })` with `themeVariables` mapped from tokens via `resolveCssVar` (`--bg-secondary`, `--text-primary`, `--border`, `--accent`), re-initialized on `useResolvedTheme()` change. `mermaid.render(uid, code)` → SVG into a container with `overflow-auto`, max-height + expand toggle. States: loading skeleton; render error → fall back to the normal `CodeBlockInner` code block with a small "diagram failed to render" note.
- New dep `mermaid` (lazy chunk — must not enter the main bundle).

**Acceptance:**

- 2.4.1 - Mermaid fences render themed SVG in both themes with strict security. file: `web/src/components/shared/MermaidBlock.tsx`.
- 2.4.2 - Render failure falls back to a highlighted code block with an error note. test: `web/src/components/shared/__tests__/MermaidBlock.test.tsx`.

## P3: Browse experience (depends: P2)
`kind: framing`

**Goal**: Obsidian-core browsing and editing: tree, reader with wikilink navigation, backlinks, quick-open, history, and edit/create/delete.

### 3.1 Page tree, reader, backlinks, quick-open, history [category: code]
`kind: deliverable`

Domain: frontend.

Targets: `web/src/components/activity/wiki/WikiPageTree.tsx`, `web/src/components/activity/wiki/WikiPageReader.tsx`, `web/src/components/activity/wiki/WikiBacklinks.tsx`, `web/src/components/activity/wiki/WikiQuickOpen.tsx`

- **Layout**: wide (≥560px) — left `WikiPageTree` (persisted width 200–480px, horizontal `ResizeHandle`) + right reader; narrow (360px split mode) — stacked with vertical percent `ResizeHandle` (`DEFAULT_TOP_PANEL_PERCENT`). Backlinks render as a collapsible section at the reader bottom.
- **Tree** (~300): built from the `pages` response via 2.1's `buildPageTree`, with the `outputs` array passed explicitly or merged into the tree input so run reports appear under `outputs/`; wiki mode hides `code/`; sources folder collapsed by default (266 entries) and lazily rendered; custom recursive `renderEntry` in the FilesTab style (28px rows, `pointer-coarse:min-h-11`), icons colored by page kind from existing tokens (`--accent` concepts, `--color-info` topics, `--text-muted` sources, `--color-warning-foreground` recaps, `--lang-folder` folders); keyboard nav via `useTreeKeyboardNavigation`; toolbar search filters to a flat match list (react-virtuoso when >100 matches); row kebab: Open, New page here, Copy path, Delete page. If `pages` fetch fails: `ActivityPanelEmpty` + retry + degraded banner; the reader still works via quick-open/direct path (read route independent).
- **Reader** (~350): top h-10 strip — back/forward chevrons, breadcrumbs (middle segments elide at narrow widths), right: Edit toggle, kebab (Open in graph, Copy path, Copy source path [code pages], Delete). Content: prose capped ~70ch, `MarkdownBody` with `remarkPlugins: [remarkWikilink(resolver)]` and components = wiki anchor override (intercepts `wikilink:` hrefs → `preventDefault` → `nav.openPage`; delegates the rest to `Anchor`) + `MermaidBlock`. Frontmatter parsed and stripped from the body: H1 title, `source_kind` badge, neutral tag chips, raw frontmatter behind a "Details" disclosure. Wikilink hover: title+path tooltip from the node index. "Sources" strip from the page's `## Citations` plus graph `trust` edges when the graph is loaded (degrades to in-body citations alone).
- **Backlinks** (~150): "Linked mentions" via `GET /api/wiki/backlinks?target=<path>` (row: page title → `openPage`); "Unresolved mentions" derived from graph `links` edges targeting `unresolved_target` nodes whose `raw_target` equals this page's path (lazily fetched with the graph payload; section hidden until available).
- **Quick-open** (~250): Cmd+K overlay scoped to panel focus (keydown on the tab root, not window): fuzzy title/path jump over the node index + server search fallback; Esc closes.
- **Read edge cases**: `not_found` from a wikilink click → "create this page?" affordance (wired fully in 3.2); `ambiguous` with `matches[]` → match picker.

**Acceptance:**

- 3.1.1 - Tree renders vault structure from the pages listing with keyboard nav and kind-colored icons. file: `web/src/components/activity/wiki/WikiPageTree.tsx`.
- 3.1.2 - Reader renders frontmatter header + markdown with clickable wikilinks that navigate (unresolved marked distinctly). file: `web/src/components/activity/wiki/WikiPageReader.tsx`.
- 3.1.3 - Backlinks pane shows linked and unresolved mentions with navigation. file: `web/src/components/activity/wiki/WikiBacklinks.tsx`.
- 3.1.4 - Quick-open fuzzy-jumps to any page; back/forward history works with dirty-guarded transitions. test: `web/src/components/activity/wiki/__tests__/WikiBrowse.test.tsx`.

### 3.2 Editing, create, delete [category: code] (depends: 3.1)
`kind: deliverable`

Domain: frontend.

Targets: `web/src/components/activity/wiki/WikiPageEditor.tsx`, `web/src/components/activity/wiki/WikiTabActions.ts`

- Edit toggle swaps the reader body for `CodeMirrorEditor` (`language="markdown"`, full raw content including frontmatter) with a `DetailPaneHeader` strip: dirty dot + "Unsaved" text, Save (accent), Discard (ghost), `serverChanged` indicator ("Page changed on disk" when the watcher reindexes underneath an open editor).
- State via `useDetailDraft<{path, content}>` — auto-registers with the shell dirty-guard registry (tab change, panel close, layout toggles); local transitions (tree click, mode switch, graph open, back/forward) call `confirmIfDirty`. `Cmd+S` via the editor's `onSave`.
- Save → `savePage` (`POST /api/wiki/write`, which returns after reindex) → update page cache and base hash, exit edit mode, refetch pages/backlinks (and graph if loaded).
- Revision contract (closes the lost-update race): the editor holds the base `content_hash` from the read payload; it revalidates the hash on window focus, on manual refresh, and immediately before save; save passes `expectedHash` and a 412 opens a conflict panel with three explicit choices — Reload (discard local draft), Overwrite (resave against the fresh hash after confirmation), or keep editing. `serverChanged` derives from the hash comparison, so it fires for watcher reindexes, second editors, and external filesystem edits alike. Silent last-write-wins is never allowed.
- **Create** (recipe-compliant, no modal): kebab "New page" / tree "New page here" / not-found wikilink affordance → reader pane becomes a create form: path field (pre-filled prefix, validated `a-z0-9-/_.`, must resolve under `knowledge/`), editor seeded with a frontmatter template (`title`, `tags: []`); Save → `createPage(mode="create")` → 409 surfaces inline → `openPage(newPath)`.
- **Delete**: `useConfirmDialog` (destructive icon + text) → `deletePage` → history-back or tree root; refetch. Code pages are read-only: edit/delete affordances hidden for `code/**` (backend rejects anyway).

**Acceptance:**

- 3.2.1 - Edit toggle with draft state, dirty guard, Cmd+S, and save-await-reindex flow works. file: `web/src/components/activity/wiki/WikiPageEditor.tsx`.
- 3.2.2 - Create flow validates paths, seeds frontmatter, and handles 409 conflicts inline. test: `web/src/components/activity/wiki/__tests__/WikiPageEditor.test.tsx`.
- 3.2.3 - Delete confirms destructively and navigates back; code pages expose no edit/delete affordances. behavior: "code pages read-only" in `web/src/components/activity/wiki/WikiPageReader.tsx`.
- 3.2.4 - Concurrent modification surfaces the reload/overwrite conflict flow; silent overwrite is impossible. test: `web/src/components/activity/wiki/__tests__/WikiPageEditor.conflict.test.tsx`.

## P4: Graph and codewiki (depends: P3)
`kind: framing`

**Goal**: The Obsidian-style 2D graph over wikilinks + code edges, and the Code mode browsing the generated codewiki.

### 4.1 2D force graph view [category: code]
`kind: deliverable`

Domain: frontend.

Targets: `web/src/components/activity/wiki/WikiForceGraph.tsx`, `web/src/components/activity/wiki/WikiGraphView.tsx`, `web/package.json`

- New dep `react-force-graph-2d` (canvas; shares the force-graph core with the installed 3D package). Loaded via `lazy(() => import('./WikiForceGraph'))` inside `WikiGraphView`'s Suspense — out of the main bundle.
- **`WikiForceGraph.tsx`** (~300), kapsule-safe: wrapped in `memo` with equality on `(dataRevision, theme, width, height)` only — `dataRevision` bumps only when filters/scope change the node/edge set; all interaction state (hover node, highlight set, search matches) lives in **refs** read inside `nodeCanvasObject`/`linkColor` accessors, repainted via the graph's internal rAF with `useRafCoalescedHandler` — zero React state updates per pointer-move; callbacks passed once via stable refs; `WikiGraphErrorBoundary` (clone of `MemoryGraphErrorBoundary`) with retry/close fallback; pixel-ratio caps matching the memory knowledge-graph exemplar.
- **Node styling**: colors resolved once per theme via `resolveCssVar` and baked into node objects at build time — never per frame. Kind/prefix colors per 2.1's mapping; `unresolved_target` = hollow circle with error-token ring (shape + lightness, not hue alone); size from `analytics.centrality` degree map; community-coloring toggle cycling `--chart-series-1..6` with a persistent legend and a ring/shape cue preserving kind. Labels drawn only when `globalScale > ~1.4` or node hovered/highlighted/matched (Obsidian behavior).
- **Edges**: default layer `links` (`--border`, brightening to `--accent-soft` on hover-incidence); toolbar checkboxes for `trust` (supports), `audit` (cites, off by default — doubles edge count), and code `imports`/`calls` (dashed `linkLineDash`; on by default in code mode).
- **`WikiGraphView.tsx`** (~300): opens from the toolbar graph button, replaces mode content, calls `requestPanelOverride()` (full panel width; MemoryTab precedent); Esc/Close releases (idempotent release-once ref — the override can be released externally by layout toggles). Filters: SegmentedControl `All | Knowledge | Code` (maps to `include` fetch param), toggles "Sources & citations" (off by default — removes ~674 nodes), "Unresolved" (off by default — removes ~835), "Orphans"; search input dims non-matches to 15%; community colors toggle; settings persisted in `gobby:wiki-tab:graph`. Default view ≈330 document nodes; hard cap 1,500 by descending degree with an info chip "showing top 1500 of N".
- **Performance/motion**: precomputed adjacency map; `warmupTicks≈80, cooldownTicks≈100`; `prefers-reduced-motion` → `warmupTicks: 200, cooldownTicks: 0` (pre-simulated, no visible settling) and non-animated `zoomToFit`.
- **Interactions**: click → close graph → `openPage(node.path)` (auto mode-flip); hover → neighbor highlight; wrapper focusable with an SR summary ("Graph of N pages…"), `+`/`-` zoom, arrow-key pan; every graph fact remains reachable non-visually via tree/backlinks (graph is progressive enhancement).

**Acceptance:**

- 4.1.1 - Ref-isolated memoized 2D graph renders ~1,500 nodes with hover/click/zoom and no kapsule re-mounts. file: `web/src/components/activity/wiki/WikiForceGraph.tsx`.
- 4.1.2 - Graph view takes over the panel via override, filters/layers/legend work, settings persist, close restores mode+page. file: `web/src/components/activity/wiki/WikiGraphView.tsx`.
- 4.1.3 - Reduced motion pre-simulates layout; node kinds remain distinguishable in grayscale. behavior: "reduced-motion + deutan-safe graph" in `web/src/components/activity/wiki/WikiGraphView.tsx`.
- 4.1.4 - Node click navigates to the page and unresolved nodes render hollow. test: `web/src/components/activity/wiki/__tests__/WikiGraph.test.tsx`.

### 4.2 Codewiki mode [category: code] (depends: 4.1)
`kind: deliverable`

Domain: frontend.

Targets: `web/src/components/activity/wiki/WikiPageTree.tsx`, `web/src/components/activity/wiki/WikiPageReader.tsx`, `web/src/components/activity/WikiTab.tsx`

Code mode is the browse experience with `rootFilter='code'` plus code-specific affordances:

- Tree roots `INDEX`, `repo`, `modules/`, `files/**` (source-tree mirror, 1,068 pages — folders collapsed by default; only visible rows render; search switches to the virtuoso flat list) fetched with `prefix=code/`.
- Reader identical; mermaid diagrams render via `MermaidBlock`; heavy code fences use the existing `CodeBlockInner` highlighting; reader kebab gains "Copy source path" from `codePathToSourcePath`.
- Graph button in code mode opens `WikiGraphView` pre-filtered to `include=code` with code edge layers on.
- Freshness strip: `GET /api/code-index/codewiki/status` (1.7) renders last-refresh time and a pending indicator near the tree header, with the existing `POST /api/code-index/codewiki/refresh` wired to a "Refresh codewiki" kebab action.

**Acceptance:**

- 4.2.1 - Code mode tree browses the codewiki mirror at scale with collapsed-by-default folders. behavior: "code rootFilter tree" in `web/src/components/activity/wiki/WikiPageTree.tsx`.
- 4.2.2 - Code pages render mermaid + highlighted fences and expose Copy source path. behavior: "code page reader affordances" in `web/src/components/activity/wiki/WikiPageReader.tsx`.
- 4.2.3 - Codewiki freshness status and refresh action are visible in code mode. test: `web/src/components/activity/wiki/__tests__/WikiCodeMode.test.tsx`.

## P5: Ask and research (depends: P3)
`kind: framing`

**Goal**: Grounded Q&A with citations that always resolve or declare themselves broken, and research runs launched and monitored without leaving the panel.

### 5.1 Ask mode [category: code]
`kind: deliverable`

Domain: frontend.

Targets: `web/src/components/activity/wiki/WikiAskMode.tsx`

- Layout top→bottom: history list (collapsed rows: question, mode chip, age; click restores; kebab rerun/delete), active answer area, composer pinned at bottom (textarea, Enter submit / Shift+Enter newline; SegmentedControl `Extractive | Synthesized` → `llm=false/true`).
- Lifecycle (no server streaming — gwiki returns one JSON): submit → pending entry, composer disabled, progress row (spinner + elapsed mm:ss + staged hint: "Searching vault…" → after 8s "Synthesizing…" when llm) + Cancel via client `AbortController` ("server may keep working" tooltip). Synthesized budget note: "can take a few minutes" (worst case ~300s). Timeout envelope → inline error + Retry. Single-flight per mode.
- Answer: `MarkdownBody` + wikilink plugin (in-answer citations clickable). Below, a "Citations" chip row resolved against the node index: resolved → `openPage` (auto mode-flip); unresolved → dashed-broken treatment + "Search vault" fallback action. **Invariant: a citation is either verified-navigable or explicitly marked broken — never a silent dead link** (the anti-DeepWiki guarantee).
- Grounding warnings from the normalized envelope (2.1) → warning callout (icon + warning tokens + text, monochrome-legible) listing ungrounded claims; absent fields render nothing.
- History: sessionStorage cap 20 `{id, question, llm, ts, envelope}`. Gateway-down state disables the composer with an info banner.

**Acceptance:**

- 5.1.1 - Ask flow with extractive/synthesized toggle, staged progress, cancel, and error/timeout states works. file: `web/src/components/activity/wiki/WikiAskMode.tsx`.
- 5.1.2 - Every citation chip either navigates to a vault page or is explicitly marked unresolved with a search fallback. test: `web/src/components/activity/wiki/__tests__/WikiAskMode.test.tsx`.
- 5.1.3 - Grounding warnings render as a monochrome-legible callout when present. behavior: "ungrounded claims callout" in `web/src/components/activity/wiki/WikiAskMode.tsx`.

### 5.2 Research mode [category: code]
`kind: deliverable`

Domain: frontend.

Targets: `web/src/components/activity/wiki/WikiResearchMode.tsx`

- Composer card: question textarea; "Options" disclosure (`topic_slug`, `max_sources` default 12, `max_items` default 8, `create_tasks` switch, provider/model selects — inputs per `src/gobby/install/shared/workflows/pipelines/wiki-research.yaml`); "Run research" (accent).
- Launch via `WikiTabActions.launchResearch` → `POST /api/pipelines/run {name:'wiki-research', project_id, background:true, inputs}` (202). Single-flight: while a `running` execution exists, the composer disables with "A research run is in progress" (respects the pipeline's own re-entrancy guard).
- Live monitoring: `usePipelineExecutions({projectId})` with `pipeline_name:'wiki-research'` filter — the hook already refetches on `pipeline_event` WS messages with 500ms debounce. Live run card: status dot + label text (`--exec-status-*` tokens), step checklist (create_research_task → spawn_researcher → wait_researcher) with per-step status, elapsed time, "View in Pipelines tab" escape hatch. Completion flips a success strip: "Open report" / "Open topic page".
- Past runs: merged view of executions history (status, duration, question from `inputs_json`) and reports from the `pages` listing's `outputs` array filtered to `*-run-report.md`, sorted by recency; clicking a report opens it in the shared `WikiPageReader` in place (back chevron returns). Report wikilinks to `knowledge/topics/<slug>` navigate via the plugin (mode auto-flip); compiled topic also derived from the execution's `outputs_json` when present.
- Resilient monitoring (closes the WS-drop/restart gap): while a run is active or the WebSocket is disconnected, poll `GET /api/pipelines/executions?pipeline_name=wiki-research` on a 10s fallback interval; a RUNNING execution with no event or poll progress for a bounded window, or one the 1.6 startup sweep marked FAILED after a daemon restart, renders a recovery state (warning icon + text) with Refresh and Dismiss actions; the composer re-enables whenever no live execution remains. Monitoring state never depends on the WebSocket alone.

**Acceptance:**

- 5.2.1 - Research runs launch detached and stream live step progress in the panel. file: `web/src/components/activity/wiki/WikiResearchMode.tsx`.
- 5.2.2 - Past run reports list from vault outputs and open in the reader with navigable topic links. test: `web/src/components/activity/wiki/__tests__/WikiResearchMode.test.tsx`.
- 5.2.3 - Single-flight guard and completion strip (report/topic shortcuts) behave correctly. behavior: "single-flight research composer" in `web/src/components/activity/wiki/WikiResearchMode.tsx`.
- 5.2.4 - WebSocket drop falls back to polling; restart-orphaned runs surface a recovery state that re-enables the composer. test: `web/src/components/activity/wiki/__tests__/WikiResearchMode.recovery.test.tsx`.

## P6: Verification and polish (depends: P4, P5)
`kind: framing`

**Goal**: The panel passes the repo's UI gates in both themes and reads as one system with the rest of the app.

### 6.1 Polish and accessibility pass [category: code] (depends: 3.2, 4.2, 5.1, 5.2)
`kind: deliverable`

Domain: frontend.

Targets: `web/src/components/activity/wiki/`, `web/src/styles/tokens.css`

Run the one-surface-tab-recipe §7 gates across all four modes and fix everything found:

- `npm run type-check`, `vitest`, `npm run lint:js`, `lint:css`, `lint:tokens` clean.
- DevTools walkthrough in dark **and** light; a grayscale screenshot pass proving every state (dirty dot, run status, unresolved links, graph kinds) reads without hue; AA contrast spot-checks on new token usages (add any missing wiki-specific tokens to `tokens.css` rather than hardcoding).
- Keyboard walkthrough: mode radiogroup arrows, tree arrows/Home/End, quick-open, editor Cmd+S, graph focus/zoom/pan, Esc chains (quick-open → graph → kebab), every kebab reachable.
- 390×844 and 360px-split reachability: no horizontal body scroll, breadcrumb elision, stacked splits usable.
- Empty/loading/error/degraded audit per mode against `ActivityPanelEmpty` and the degraded banner; reduced-motion verification for graph and transitions.

**Acceptance:**

- 6.1.1 - All frontend gates pass and both-theme + grayscale walkthroughs are completed with fixes applied. behavior: "recipe §7 verification" across `web/src/components/activity/wiki/`.
- 6.1.2 - Keyboard-only operation covers every interactive surface in all four modes. test: `web/src/components/activity/wiki/__tests__/WikiA11y.test.tsx`.

## E1 End-to-End Verification
`kind: verification`

With the daemon running (`uv run gobby restart` after backend tasks; rebuild+reinstall `gwiki` after Rust tasks) and `npm run dev` in `web/`:

1. **Graph**: open Wiki tab → graph button → force layout of knowledge pages appears; toggle Code layer → code edges join; click a node → its page opens in the right mode. `curl -s --compressed 'localhost:60887/api/wiki/graph?include=knowledge'` returns a gzip envelope with `payload.graph.nodes`.
2. **Obsidian loop**: click a `[[wikilink]]` in a concept page → navigate; backlinks pane lists the source page; Cmd+K quick-open jumps by title; edit the page, Cmd+S → save returns post-reindex; the edit is immediately searchable via `wiki_search`; create a new page from an unresolved link, then delete it.
3. **Codewiki**: Code mode tree mirrors `wiki/code/files/**`; a module page renders a mermaid diagram; Copy source path yields the real repo path; freshness strip shows last nightly refresh.
4. **Ask**: extractive ask returns cited evidence; synthesized ask renders an answer whose citation chips all either navigate or show the broken treatment; an ungrounded-claim fixture renders the warning callout.
5. **Research**: launch a run → 202 + live step checklist; on completion the report opens in the reader and its topic wikilink navigates to the compiled topic page. Confirm the run also appears in the Pipelines tab.
6. **Contract checks**: `POST /api/wiki/write` with `path=code/files/x.md` → 400/confinement error; `--mode create` conflict → 409; grayscale screenshot of the graph legend distinguishes all node kinds; `prefers-reduced-motion` shows a settled graph with no animation.

## Task Mapping
`kind: framing`

<!-- Updated after task creation -->
| Plan Item | Task Ref | Status |
|-----------|----------|--------|

## V1 Plan Changelog
`kind: verification`

**Round 1** `kind: verification`

- reviewer_run: 22cd8501-0005-4f09-b4ff-ba5c0f828e3e
- reviewer_session: 6c1e49bd-9d78-4b11-86e9-fbb18a6e67b0
- verdict: approved
- findings:
  - none — two qualitative passes (traceability, sequencing, proportionality) surfaced no blocking issues
- resolution_notes: Enhancement phase skipped by user decision. Adversary appended the
  `## M1 Task Manifest` (18 entries, one per deliverable, 53 coverage labels), mapped
  phase-level dependencies to concrete prior leaf sections (expansion rejects phase IDs
  in manifest depends_on), preserved the plan's explicit backend/frontend domains, and
  ran expansion-mode validation successfully. No narrative changes were required.
  Post-round: coordinator enriched all 18 manifest validation_criteria into testable
  criteria sentences derived from acceptance items; expansion validation re-passed.

**Round 2** `kind: verification`

- reviewer_run: d5f76db0-29e5-4297-a72c-1dbf53f120e7
- reviewer_session: 8909f4a7-8664-45ee-be33-a29697a74bd2
- verdict: needs_review
- findings:
  - R2-F1/blocking/unhandled-edge (1.3, 1.5, 3.2): concurrent-editor/watcher race named
    but unspecified — no revision contract on read/pages/write, so verbatim writes allow
    silent lost updates and serverChanged has no data source.
  - R2-F2/blocking/unhandled-edge (1.6, 5.2): research monitoring depended on the
    WebSocket alone and single-flighted on any RUNNING execution while 1.6 deferred the
    restart sweep — WS drop or daemon restart strands stale progress and a permanently
    disabled composer.
- resolution_notes: R2-F1 resolved with a content-hash revision contract — `pages` and
  `read` payloads carry `content_hash` (1.2), `gwiki page write` gains `--expected-hash`
  precondition returning precondition-failed (1.3.5), routes/MCP map it to 412 (1.5.2),
  and the editor revalidates on focus/refresh/pre-save with a reload/overwrite conflict
  panel (3.2.4). R2-F2 resolved by bringing the executor startup sweep into scope
  (1.6.3) and adding poll-fallback monitoring with a stale-run recovery state that
  re-enables the composer (5.2.4). Manifest labels and validation_criteria updated for
  all four sections; draft and expansion validation re-run clean.

**Round 3** `kind: verification`

- reviewer_run: 5779843e-f25a-4de4-96df-e28fcf9d28a1
- reviewer_session: 99596ec6-7ab5-4c51-b14f-cdb4d9fb8f85
- verdict: needs_review
- findings:
  - R3-F1/blocking/unhandled-edge (1.5): gateway signature omitted expected_hash, so the
    precondition could be silently dropped between route/MCP and the gwiki CLI, making
    the 412 lost-update protection unreachable. R2-F2 fix verified sufficient.
- resolution_notes: `GwikiGateway.write_page` now takes `expected_hash: str | None`,
  appends `--expected-hash` to the gwiki argv when set, routes/MCP pass it verbatim, and
  acceptance 1.5.2 requires the test to assert a stale hash reaches the gwiki argv
  through the reindex-backed write path. Manifest criteria updated; validation re-run.

## M1 Task Manifest
`kind: manifest`

```yaml
- title: Add `--stdout` and `--include` to `gwiki graph`
  category: code
  task_type: feature
  depends_on: []
  validation_criteria: 'gwiki graph --stdout prints the full GraphExport JSON envelope without writing artifacts; --include knowledge|code filters facts with analytics recomputed on the filtered set; default artifact-writing behavior is unchanged. Crate tests cover retain_include and stdout emission; clippy clean; release binary rebuilt and reinstalled to ~/.gobby/bin/gwiki.'
  labels:
  - covers:unknown:1.1:1.1.1
  - covers:unknown:1.1:1.1.2
  - covers:unknown:1.1:1.1.3
  implementation_domain: backend
  tdd: true
  source_section: '1.1'
- title: Add `gwiki pages` listing + outputs read allowlist
  category: code
  task_type: feature
  depends_on: []
  validation_criteria: 'gwiki pages returns indexed pages with path/title/tags plus a filesystem-walked outputs array; --prefix code/ restricts the listing; run reports under outputs/ become readable via gwiki read while staying unindexed. Crate tests cover prefix filtering and outputs readability; release binary reinstalled.'
  labels:
  - covers:unknown:1.2:1.2.1
  - covers:unknown:1.2:1.2.2
  - covers:unknown:1.2:1.2.3
  implementation_domain: backend
  tdd: true
  source_section: '1.2'
- title: Add `gwiki page write|delete` with vault confinement
  category: code
  task_type: feature
  depends_on: []
  validation_criteria: 'gwiki page write upserts knowledge/ pages from stdin emitting changed_paths; confinement rejects writes outside knowledge/ plus traversal and symlink escapes; --mode create returns a distinct already-exists error; --expected-hash mismatch returns a precondition-failed error leaving the file untouched; page delete removes the file and reindex prunes derived rows. Crate tests cover all five behaviors; release binary reinstalled.'
  labels:
  - covers:unknown:1.3:1.3.1
  - covers:unknown:1.3:1.3.2
  - covers:unknown:1.3:1.3.3
  - covers:unknown:1.3:1.3.4
  - covers:unknown:1.3:1.3.5
  implementation_domain: backend
  tdd: true
  source_section: '1.3'
- title: Python read surfaces for graph and pages routes
  category: code
  task_type: feature
  depends_on:
  - '1.1'
  - '1.2'
  validation_criteria: 'GwikiGateway.graph()/pages() exist and GET /api/wiki/graph and /api/wiki/pages serve them with project/topic scope params; an invalid include value yields a 400 validation envelope; gzip middleware compresses large responses. Focused route tests prove include validation and gzip behavior.'
  labels:
  - covers:unknown:1.4:1.4.1
  - covers:unknown:1.4:1.4.2
  - covers:unknown:1.4:1.4.3
  implementation_domain: backend
  tdd: true
  source_section: '1.4'
- title: Python write surfaces and MCP parity
  category: code
  task_type: feature
  depends_on:
  - '1.3'
  validation_criteria: 'POST /api/wiki/write persists content verbatim and responds only after the reindex handoff completes; GwikiGateway.write_page threads expected_hash into the gwiki argv so the precondition cannot be dropped at the gateway boundary; create-mode conflict maps to 409, missing-page delete to 404, and expected-hash mismatch to 412; wiki_write_page and wiki_delete_page MCP tools are registered with confinement errors surfaced. Focused route tests prove write-await-reindex, hash threading, and error mapping.'
  labels:
  - covers:unknown:1.5:1.5.1
  - covers:unknown:1.5:1.5.2
  - covers:unknown:1.5:1.5.3
  implementation_domain: backend
  tdd: true
  source_section: '1.5'
- title: 'Detached pipeline runs: `background` flag on `POST /api/pipelines/run`'
  category: code
  task_type: feature
  depends_on: []
  validation_criteria: 'PipelineExecutor.start_detached returns a RUNNING execution immediately and the run completes in the background with task retention and exception-logging done-callbacks; POST /api/pipelines/run with background true responds 202 carrying execution_id while pipeline_event WS messages stream progress; executor startup marks restart-orphaned RUNNING executions FAILED. Executor tests prove detached completion and the startup sweep.'
  labels:
  - covers:unknown:1.6:1.6.1
  - covers:unknown:1.6:1.6.2
  - covers:unknown:1.6:1.6.3
  implementation_domain: backend
  tdd: true
  source_section: '1.6'
- title: Codewiki refresh status endpoint
  category: code
  task_type: feature
  depends_on: []
  validation_criteria: 'CodewikiRefreshTrigger.status() exposes pending root keys, active flush tasks, and last-run outcome with timestamps; GET /api/code-index/codewiki/status returns that snapshot. Focused tests prove the snapshot contents after a refresh cycle.'
  labels:
  - covers:unknown:1.7:1.7.1
  - covers:unknown:1.7:1.7.2
  implementation_domain: backend
  tdd: true
  source_section: '1.7'
- title: Wiki data layer + model rewrite
  category: code
  task_type: feature
  depends_on:
  - '1.4'
  - '1.5'
  - '1.6'
  - '1.7'
  validation_criteria: 'WikiTabModel and WikiTabData expose typed models, the page-tree builder, node index, path helpers, and defensive fetchers/normalizers for graph, pages, page, backlinks, ask, write, delete, and research launch. Vitest fixtures pin tree building, wikilink target resolution, and ask normalization against captured envelopes.'
  labels:
  - covers:unknown:2.1:2.1.1
  - covers:unknown:2.1:2.1.2
  - covers:unknown:2.1:2.1.3
  implementation_domain: frontend
  tdd: true
  source_section: '2.1'
- title: Mode shell, toolbar, navigation, sources port
  category: code
  task_type: feature
  depends_on:
  - '2.1'
  validation_criteria: 'The four-mode WikiTab shell replaces the old implementation with persisted mode/scope/navigation, the segmented mode control, kebab actions, the ported sources manager, and panel-override wiring mirroring MemoryTab; the old wiki list/detail components are deleted. Shell tests prove mode switching and dirty-guarded transitions.'
  labels:
  - covers:unknown:2.2:2.2.1
  - covers:unknown:2.2:2.2.2
  - covers:unknown:2.2:2.2.3
  - covers:unknown:2.2:2.2.4
  implementation_domain: frontend
  tdd: true
  source_section: '2.2'
- title: Wikilink remark plugin + MarkdownBody extension seam
  category: code
  task_type: feature
  depends_on:
  - '2.1'
  validation_criteria: 'remarkWikilink transforms wikilink syntax including alias, anchor, unresolved, adjacent, and embed-degradation cases into wikilink-scheme links without rehype-raw; MarkdownBody accepts merged remarkPlugins/components props with byte-identical default behavior and a memo key covering the plugin identity. Unit tests pin every listed case.'
  labels:
  - covers:unknown:2.3:2.3.1
  - covers:unknown:2.3:2.3.2
  implementation_domain: frontend
  tdd: true
  source_section: '2.3'
- title: Mermaid rendering
  category: code
  task_type: feature
  depends_on:
  - '2.3'
  validation_criteria: 'MermaidBlock lazily renders language-mermaid fences as token-themed SVG in both themes with strict security level, re-initializing on theme change; render failure falls back to a highlighted code block with an error note; the mermaid dependency stays out of the main bundle. Component tests prove render and fallback paths.'
  labels:
  - covers:unknown:2.4:2.4.1
  - covers:unknown:2.4:2.4.2
  implementation_domain: frontend
  tdd: true
  source_section: '2.4'
- title: Page tree, reader, backlinks, quick-open, history
  category: code
  task_type: feature
  depends_on:
  - '2.1'
  - '2.2'
  - '2.3'
  - '2.4'
  validation_criteria: 'The tree renders vault structure from the pages listing with keyboard navigation and kind-colored icons; the reader renders frontmatter header plus markdown with clickable wikilink navigation and distinct unresolved treatment; the backlinks pane lists linked and unresolved mentions; quick-open fuzzy-jumps to any page and back/forward history is dirty-guarded. Browse tests prove navigation flows.'
  labels:
  - covers:unknown:3.1:3.1.1
  - covers:unknown:3.1:3.1.2
  - covers:unknown:3.1:3.1.3
  - covers:unknown:3.1:3.1.4
  implementation_domain: frontend
  tdd: true
  source_section: '3.1'
- title: Editing, create, delete
  category: code
  task_type: feature
  depends_on:
  - '3.1'
  validation_criteria: 'The edit toggle provides draft state with dirty guard and Cmd+S; save passes the base content hash and awaits the reindex-backed write; a 412 opens the reload/overwrite conflict panel so silent overwrite is impossible; create validates knowledge/ paths, seeds frontmatter, and surfaces 409 conflicts inline; delete confirms destructively and navigates back; code pages expose no edit or delete affordances. Editor tests prove create, save-conflict, and concurrent-modification flows.'
  labels:
  - covers:unknown:3.2:3.2.1
  - covers:unknown:3.2:3.2.2
  - covers:unknown:3.2:3.2.3
  - covers:unknown:3.2:3.2.4
  implementation_domain: frontend
  tdd: true
  source_section: '3.2'
- title: 2D force graph view
  category: code
  task_type: feature
  depends_on:
  - '3.1'
  validation_criteria: 'The ref-isolated memoized 2D graph renders around 1500 nodes with hover, click-to-navigate, and zoom without kapsule re-mounts; the graph view takes the panel override with working filters, layers, legend, and persisted settings; reduced motion pre-simulates layout; node kinds remain distinguishable in grayscale and unresolved nodes render hollow. Graph tests prove interactions.'
  labels:
  - covers:unknown:4.1:4.1.1
  - covers:unknown:4.1:4.1.2
  - covers:unknown:4.1:4.1.3
  - covers:unknown:4.1:4.1.4
  implementation_domain: frontend
  tdd: true
  source_section: '4.1'
- title: Codewiki mode
  category: code
  task_type: feature
  depends_on:
  - '4.1'
  validation_criteria: 'Code mode browses the codewiki mirror at scale with collapsed-by-default folders; code pages render mermaid diagrams and highlighted fences and expose Copy source path; the graph opens pre-filtered to code edges; the freshness strip surfaces codewiki refresh status and a refresh action. Code-mode tests prove the browsing flow.'
  labels:
  - covers:unknown:4.2:4.2.1
  - covers:unknown:4.2:4.2.2
  - covers:unknown:4.2:4.2.3
  implementation_domain: frontend
  tdd: true
  source_section: '4.2'
- title: Ask mode
  category: code
  task_type: feature
  depends_on:
  - '3.1'
  validation_criteria: 'Ask supports the extractive/synthesized toggle, staged progress with cancel, and error/timeout retry; every citation chip either navigates to a vault page or is explicitly marked unresolved with a search fallback (never a silent dead link); grounding warnings render as a monochrome-legible callout. Ask tests prove citation resolution behavior.'
  labels:
  - covers:unknown:5.1:5.1.1
  - covers:unknown:5.1:5.1.2
  - covers:unknown:5.1:5.1.3
  implementation_domain: frontend
  tdd: true
  source_section: '5.1'
- title: Research mode
  category: code
  task_type: feature
  depends_on:
  - '1.6'
  - '3.1'
  validation_criteria: 'Research runs launch detached with a 202 and stream live step progress via pipeline events with a 10s polling fallback when the WebSocket is down; restart-orphaned runs surface a recovery state that re-enables the composer; past run reports list from vault outputs and open in the shared reader with navigable topic wikilinks; the single-flight guard and completion strip behave correctly. Research tests prove launch, monitor, recovery, and report flows.'
  labels:
  - covers:unknown:5.2:5.2.1
  - covers:unknown:5.2:5.2.2
  - covers:unknown:5.2:5.2.3
  - covers:unknown:5.2:5.2.4
  implementation_domain: frontend
  tdd: true
  source_section: '5.2'
- title: Polish and accessibility pass
  category: code
  task_type: feature
  depends_on:
  - '3.2'
  - '4.2'
  - '5.1'
  - '5.2'
  validation_criteria: 'All frontend gates pass (type-check, vitest, JS/CSS/token lint); dark, light, and grayscale walkthroughs are completed across all four modes with fixes applied; keyboard-only operation covers every interactive surface; reduced-motion and 360px-split reachability are verified. A11y tests pin keyboard coverage.'
  labels:
  - covers:unknown:6.1:6.1.1
  - covers:unknown:6.1:6.1.2
  implementation_domain: frontend
  tdd: true
  source_section: '6.1'
```
