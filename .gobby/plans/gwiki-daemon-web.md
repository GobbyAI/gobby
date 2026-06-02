# gwiki daemon and web integration

**Plan ID:** gwiki-daemon-web

## O1: Overview

`kind: framing`

Add Gobby daemon, MCP, and web-chat integration for the `gwiki` Rust CLI after the `gobby-cli` `gwiki` plan lands its JSON command contracts. The daemon does not own wiki domain behavior, schema, vault layout, synthesis, or indexing internals. It wraps `gwiki --format json`, exposes API and MCP surfaces, and gives web chat a Wiki Activity panel plus chat actions for search, read, attach, ingest, compile, audit, and health checks.

This plan lives in the Gobby repo because it changes daemon routes, MCP tools, scheduling,
watchers, and web UI. It depends on upstream Plan ID `gwiki`
(`gobby-cli/.gobby/plans/completed/gwiki.md`) for the Rust CLI/library contract.

Daemon AI/multimodal capability routes, structured capability errors, text-generation adoption, and `gwiki_*` hub adoption are covered by the companion plan `.gobby/plans/gwiki-daemon-ai-contract.md`.

## S1: Source Contract

`kind: framing`

The upstream CLI JSON-command source plan is
`gobby-cli/.gobby/plans/completed/gwiki.md` (Plan ID `gwiki`). This daemon plan
consumes existing upstream `gwiki --format json` commands through `GwikiGateway`:

- `gwiki status --format json`
- `gwiki index --format json`
- `gwiki search --format json`
- `gwiki backlinks --format json`
- `gwiki ingest-file --format json`
- `gwiki ingest-url --format json URL...`
- `gwiki collect --format json`
- `gwiki research --format json`
- `gwiki compile --format json`
- `gwiki audit --format json`
- `gwiki health --format json`
- `gwiki sources --format json`
- `gwiki remove-source --id <SOURCE_ID> --format json [--dry-run|--yes] [--keep-asset]`

The upstream CLI additions required by this daemon/web plan are `gwiki read --format json`,
`gwiki ingest-url --format json URL...`, `gwiki sources --format json`, and
`gwiki remove-source --id <SOURCE_ID> --format json [--dry-run|--yes] [--keep-asset]`.
Daemon/web `attach` is local integration terminology: the daemon handles upload and staging,
then maps attach requests to existing `gwiki ingest-file --format json` via
`GwikiGateway.ingest_file`. No upstream `gwiki attach` command is required.

URL batch ingest is upstream-owned. `gwiki ingest-url --format json URL...` owns HTTP
fetching, final URL resolution, accepted source persistence, raw Markdown writes,
per-URL failure classification, and once-per-batch indexing after accepted sources are
written. Daemon/web code passes URL arrays to `GwikiGateway.ingest_url`; it must not fetch
URLs, write URL raw sources, classify URL failures, or trigger duplicate indexing for the
accepted batch already indexed by the CLI result.

Wiki parsing, vault semantics, source lifecycle ownership, command ownership, and AI
capability routing remain outside this web/gateway scope. The AI capability source contract
is tracked in
`.gobby/plans/gwiki-daemon-ai-contract.md`.

## C1: Constraints

`kind: framing`

- **Contract dependency**: daemon and web work consume stable `gwiki --format json` commands. Do not duplicate wiki parsing, source manifests, search ranking, compile, audit, or datastore ownership in Python or TypeScript.
- **Gateway boundary**: the daemon calls `gwiki` through a single `GwikiGateway` wrapper until a future direct Rust linking path exists.
- **Source lifecycle ownership**: source listing and source removal are CLI-owned. Daemon/web code must not parse raw source files or `INDEX.md`, delete wiki files/assets directly, or implement `gwiki remove-source` behavior in Python or TypeScript.
- **No schema ownership leakage**: the daemon must not create, alter, or drop `gwiki_*` tables or wiki graph/vector stores. `gwiki setup` owns explicit setup.
- **Filesystem source of truth**: local vault files remain canonical. API/MCP routes report derived index status and invoke `gwiki index` where appropriate.
- **Explicit writes only**: ingest, attach, compile, remove-source confirmation, and fix-style operations require an explicit user action or scheduled job. Search, read, sources, status, audit, and health routes stay read-only unless their `gwiki` command contract says otherwise.
- **Hybrid freshness model**: explicit `gwiki` writes index immediately; daemon watchers debounce local file changes; cron jobs are only for user-visible scheduled research, refresh, health checks, and audits.
- **Cron signal quality**: lightweight maintenance/status bookkeeping belongs in the daemon automation/status loop, not user-visible cron-history spam.
- **Scope clarity**: every API, MCP, and web result carries project/topic scope identity and never crosses scopes implicitly.

## D1: Dependency Contracts

`kind: framing`

The daemon/web work requires these `gwiki` JSON command contracts from the CLI plan:

- `gwiki status --format json`
- `gwiki index --format json`
- `gwiki search --format json`
- `gwiki read --format json`
- `gwiki backlinks --format json`
- `gwiki ingest-file --format json`
- `gwiki ingest-url --format json URL...`
- `gwiki collect --format json`
- `gwiki research --format json`
- `gwiki compile --format json`
- `gwiki audit --format json`
- `gwiki health --format json`
- `gwiki sources --format json`
- `gwiki remove-source --id <SOURCE_ID> --format json [--dry-run|--yes] [--keep-asset]`

Successful command JSON must include `scope`, `command`, and command-specific payloads.
Structured degradation, actionable paths, changed paths, setup guidance, health findings, or
similar path/status data are required where the specific command can produce them. The
upstream CLI should preserve command-specific JSON style; it does not need to force a
universal `ok` / `degraded` / `paths` envelope across every command.

`gwiki read --format json` must return a scoped wiki page/document payload suitable for
daemon pass-through and normalization, including requested path/title identity, resolved wiki
path, Markdown `content`, and structured degradation/error guidance when available. The
current daemon/web contract consumes Markdown content as returned by upstream; any future
`rendered_text` field is additive and must not become a prerequisite for daemon/web
implementation. Read lookup accepts exactly one selector, `--path` or `--title`; title lookup
is exact first-heading resolution until the upstream CLI intentionally expands matching to
frontmatter aliases or wikilinks.

`gwiki sources --format json` must return scoped source records in CLI-owned JSON suitable
for pass-through to the daemon, MCP, and web UI. Each source entry includes `id`, `kind`,
`title`, `location`, `citation`, `content_hash`, `fetched_at`, `compile_status`,
`raw_path`, `raw_exists`, and optional `source_asset`. Missing raw files are CLI
degradations, not daemon-side errors. The daemon must not normalize the source record schema
beyond its standard HTTP/MCP envelope.

`gwiki remove-source --id <SOURCE_ID> --format json [--dry-run|--yes] [--keep-asset]`
must own all source-removal planning and mutation. Dry-run returns the CLI preview payload
for display; confirmed removal returns the CLI result with `status`, `dry_run`, `source`,
`removed_paths`, `kept_paths`, `missing_paths`, `degradations`, `follow_up`, and
`index_status.index_required`. `follow_up: ["audit_recommended"]` signals that compiled
claims may need review. Source removal is conservative: it removes raw source provenance and
raw assets only; compiled wiki articles, concepts, health snapshots, research checkpoints,
and `outputs/` exports remain out of scope. The daemon uses `index_status.index_required`
only to coordinate follow-up indexing and does not infer removal effects by inspecting vault
files.

`gwiki ingest-url --format json URL...` must return a scoped batch result with
`command: "ingest-url"`, `scope`, `status: "ingested" | "partial" | "failed"`, `accepted`
entries containing `requested_url`, `final_url`, `raw_path`, and `source { id, kind,
content_hash, location }`, `failed` entries containing `url`, `code`, and `message`, and
`indexed { documents, chunks, links, sources, ingestions }`. Partial success is a successful
subprocess result. All-failed batches return nonzero while preserving the same structured
result on stdout when available. The daemon consumes the CLI failure classification and must
not retry, refetch, or persist URL sources itself.

`GwikiGateway` normalizes command-specific CLI JSON into daemon HTTP/MCP response envelopes.
For `gwiki read`, payload statuses such as `not_found`, `invalid_request`, and `ambiguous`
are successful subprocess JSON payloads and must flow through the gateway as command results,
not typed subprocess failures. On nonzero exits, gateway errors must preserve stderr and
include parsed structured guidance from stdout or stderr when available. The same error
contract applies to source listing and removal, including dry-run previews and structured
remove-source failures.
For `gwiki ingest-url`, the gateway must preserve accepted/failed arrays, stderr, stdout
JSON on nonzero all-failed exits, and the CLI-owned `indexed` counts without scheduling a
second index pass.

## P1: Gateway And Contract Probe

`kind: framing`

**Goal**: create one daemon gateway for all `gwiki` subprocess calls and pin the JSON contract before route work starts.

### 1.1 Document gwiki JSON command contracts [category: docs]

`kind: deliverable`

Targets: `docs/guides/gwiki-daemon-web.md`

Document each consumed `gwiki --format json` command, required arguments, request context, response shape, degradation fields, timeout behavior, and write/read classification.

**Acceptance:**

- 1.1.1 - Guide lists every consumed `gwiki` command and required JSON fields. file: `docs/guides/gwiki-daemon-web.md`.
- 1.1.2 - Guide classifies each operation as read-only, explicit write, or scheduled write. file: `docs/guides/gwiki-daemon-web.md`.
- 1.1.3 - Guide states daemon/web code must use `GwikiGateway`, not direct subprocess calls. file: `docs/guides/gwiki-daemon-web.md`.
- 1.1.4 - Guide documents CLI-owned source listing/removal, `--dry-run`/`--yes`, `--keep-asset`, and `index_status.index_required`. file: `docs/guides/gwiki-daemon-web.md`.
- 1.1.5 - Guide documents CLI-owned URL batch ingest, including URL arrays, accepted/failed result shape, partial success, all-failed nonzero behavior, and once-per-batch CLI indexing. file: `docs/guides/gwiki-daemon-web.md`.

### 1.2 Add GwikiGateway wrapper [category: code] (depends: 1.1)

`kind: deliverable`

Targets: `src/gobby/gwiki_gateway.py`, `tests/test_gwiki_gateway.py`

Add a single async wrapper around `gwiki --format json` with methods for `status`, `index`,
`search`, `read`, `backlinks`, `ingest_file`, `ingest_url(urls)`, `collect`, `research`,
`compile`, `audit`, `health`, `sources`, and
`remove_source(source_id, *, dry_run, yes, keep_asset)`.
The gateway resolves the binary path, passes project/topic scope, enforces
timeouts, parses JSON stdout, captures stderr, normalizes command-specific CLI JSON into
daemon envelopes, and raises typed errors on non-zero exits. `read` must pass exactly one of
`--path` or `--title`; upstream read statuses `not_found`, `invalid_request`, and `ambiguous`
are parsed JSON command results, not subprocess failures. `sources` must preserve CLI-owned
source records. `remove_source` must pass `--id`, `--dry-run` or `--yes`, and optional
`--keep-asset`, while preserving CLI dry-run previews, confirmed-removal payloads,
`index_status`, stderr, and structured errors. `ingest_url(urls)` must pass URL arguments
without daemon-side fetching, preserve CLI `accepted`, `failed`, `status`, and `indexed`
fields, treat partial success as a command result, and preserve structured stdout plus
stderr on all-failed nonzero exits.

**Acceptance:**

- 1.2.1 - `GwikiGateway` exposes `status`, `index`, `search`, `read`, `backlinks`, `ingest_file`, `ingest_url`, `collect`, `research`, `compile`, `audit`, `health`, `sources`, and `remove_source`. file: `src/gobby/gwiki_gateway.py`.
- 1.2.2 - Gateway parses JSON stdout and preserves stderr on failure. test: `tests/test_gwiki_gateway.py::test_error_preserves_stderr`.
- 1.2.3 - Gateway enforces per-command timeout and reports structured degradation. test: `tests/test_gwiki_gateway.py::test_timeout_degrades`.
- 1.2.4 - No route, MCP tool, watcher, or cron path invokes `gwiki` outside `GwikiGateway`. behavior: "grep for create_subprocess_exec gwiki has only gateway hits" in `src/gobby/`.
- 1.2.5 - `GwikiGateway.read` passes exactly one selector (`--path` or `--title`) and treats `not_found`, `invalid_request`, and `ambiguous` statuses as successful JSON command payloads. test: `tests/test_gwiki_gateway.py::test_read_status_payloads_are_not_subprocess_failures`.
- 1.2.6 - `GwikiGateway.sources` preserves CLI source-list JSON and scope identity. test: `tests/test_gwiki_gateway.py::test_sources_preserves_cli_payload`.
- 1.2.7 - `GwikiGateway.remove_source` passes source id, dry-run/confirmation flags, `keep_asset`, stderr, dry-run preview payloads, and `index_status` through the gateway contract. test: `tests/test_gwiki_gateway.py::test_remove_source_preserves_cli_payloads`.
- 1.2.8 - `GwikiGateway.ingest_url` passes all URL arguments to `gwiki ingest-url --format json` and preserves `accepted`, `failed`, `status`, `scope`, and `indexed` fields. test: `tests/test_gwiki_gateway.py::test_ingest_url_passes_batch_and_preserves_payload`.
- 1.2.9 - `GwikiGateway.ingest_url` treats partial failures as successful command JSON and preserves stdout JSON plus stderr on all-failed nonzero exits. test: `tests/test_gwiki_gateway.py::test_ingest_url_preserves_partial_and_all_failed_errors`.

## P2: API And MCP Surfaces

`kind: framing`

**Goal**: expose wiki capabilities to local clients through HTTP and MCP without duplicating `gwiki` logic.

### 2.1 Add `/api/wiki/*` routes [category: code] (depends: 1.2)

`kind: deliverable`

Targets: `src/gobby/servers/routes/wiki.py`, `tests/servers/routes/test_wiki_routes.py`

Add HTTP routes backed by `GwikiGateway`:

- `GET /api/wiki/status`
- `GET /api/wiki/search`
- `GET /api/wiki/read`
- `GET /api/wiki/backlinks`
- `GET /api/wiki/health`
- `GET /api/wiki/sources`
- `POST /api/wiki/index`
- `POST /api/wiki/attach`
- `POST /api/wiki/ingest`
- `POST /api/wiki/collect`
- `POST /api/wiki/research`
- `POST /api/wiki/compile`
- `POST /api/wiki/audit`
- `POST /api/wiki/remove-source`

`GET /api/wiki/read` calls `GwikiGateway.read` and depends on upstream
`gwiki read --format json`; daemon code must not parse vault files directly. The route
accepts exactly one read selector (`path` or `title`) and returns upstream Markdown `content`
plus read status payloads directly through the daemon envelope. `POST /api/wiki/attach`
accepts and stages uploads in daemon code, then calls
`GwikiGateway.ingest_file` / `gwiki ingest-file --format json`; it must not require an
upstream `gwiki attach` command. `POST /api/wiki/ingest` also calls
`GwikiGateway.ingest_file` for file/path ingest requests. When the ingest request body
contains a URL array, `POST /api/wiki/ingest` calls `GwikiGateway.ingest_url(urls)` and
passes the CLI result through; daemon code must not fetch URLs or write URL raw sources.
Mixed file and URL ingest requests are rejected before gateway dispatch.
`GET /api/wiki/sources` calls `GwikiGateway.sources`.
`POST /api/wiki/remove-source` requires request body field `id`, accepts `dry_run`, `yes`,
and `keep_asset`, rejects `dry_run: true` with `yes: true`, and maps gateway/CLI errors
through the existing wiki route error contract. Removal without `yes: true` must be a
dry-run preview. Explicit write routes trigger immediate indexing when the `gwiki` result
reports changed vault files. `remove-source` is also an explicit write, but it triggers
daemon indexing only when the CLI result includes `index_status.index_required: true`.
`ingest-url` is an explicit write whose CLI result already indexed the accepted batch, so the
route must not schedule duplicate daemon indexing for the same result.

**Acceptance:**

- 2.1.1 - Wiki route module exposes the listed routes and calls `GwikiGateway`. file: `src/gobby/servers/routes/wiki.py`.
- 2.1.2 - Route tests cover scope validation, JSON response pass-through, and gateway error mapping. test: `tests/servers/routes/test_wiki_routes.py`.
- 2.1.3 - Explicit write routes invoke immediate index handoff when changed paths are reported. test: `tests/servers/routes/test_wiki_routes.py::test_write_routes_trigger_index`.
- 2.1.4 - Source routes expose `GET /api/wiki/sources` and `POST /api/wiki/remove-source`, require `id` for removal, and reject simultaneous `dry_run` and `yes`. test: `tests/servers/routes/test_wiki_routes.py::test_source_routes_contract`.
- 2.1.5 - Source route tests prove gateway error mapping preserves CLI stderr and structured remove-source guidance. test: `tests/servers/routes/test_wiki_routes.py::test_remove_source_error_mapping`.
- 2.1.6 - `/api/wiki/ingest` routes URL arrays to `GwikiGateway.ingest_url` without daemon URL fetching and rejects mixed file/URL requests. test: `tests/servers/routes/test_wiki_routes.py::test_ingest_url_batch_routes_to_gateway`.
- 2.1.7 - URL ingest route tests preserve CLI partial success, all-failed nonzero payloads, stderr, and `indexed` counts without duplicate index handoff. test: `tests/servers/routes/test_wiki_routes.py::test_ingest_url_batch_passthrough_and_indexing`.

### 2.2 Add gobby-wiki MCP tools [category: code] (depends: 2.1)

`kind: deliverable`

Targets: `src/gobby/mcp_proxy/tools/wiki.py`, `tests/mcp_proxy/tools/test_wiki.py`

Expose MCP tools for `wiki_search`, `wiki_read`, `wiki_attach`, `wiki_ingest`,
`wiki_compile`, `wiki_audit`, `wiki_health`, `wiki_list_sources`, and
`wiki_remove_source`. Tools use the gateway and return structured JSON with scope identity,
command payloads, citations, and path/degradation metadata when present. `wiki_read`
depends on `GwikiGateway.read` / `gwiki read --format json`.
`wiki_read` accepts exactly one of `path` or `title` and returns Markdown `content` plus
upstream read status payloads. `wiki_attach` stages daemon uploads and maps to
`GwikiGateway.ingest_file`. `wiki_ingest` accepts either file/path ingest input or URL batch
input; URL arrays pass through to `GwikiGateway.ingest_url` without daemon URL fetching.
`wiki_list_sources` matches `GET /api/wiki/sources`.
`wiki_remove_source` matches `POST /api/wiki/remove-source`: `id` is required, `dry_run`
and `yes` are mutually exclusive, `keep_asset` is optional, and dry-run preview payloads are
passed through from the CLI.

**Acceptance:**

- 2.2.1 - MCP wiki tools are registered and use `GwikiGateway`. file: `src/gobby/mcp_proxy/tools/wiki.py`.
- 2.2.2 - Tool schemas include scope, project/topic, command-specific arguments, `wiki_list_sources`, and `wiki_remove_source` with the HTTP removal contract. test: `tests/mcp_proxy/tools/test_wiki.py::test_tool_schemas`.
- 2.2.3 - Tools preserve gateway degradation and path metadata. test: `tests/mcp_proxy/tools/test_wiki.py::test_degradation_passthrough`.
- 2.2.4 - Source lifecycle MCP tools preserve CLI source-list, dry-run preview, confirmed removal, and `index_status` payloads. test: `tests/mcp_proxy/tools/test_wiki.py::test_source_lifecycle_passthrough`.
- 2.2.5 - `wiki_ingest` schema accepts URL batch input and passes URL arrays through to `GwikiGateway.ingest_url` while preserving accepted/failed CLI payloads. test: `tests/mcp_proxy/tools/test_wiki.py::test_wiki_ingest_url_batch_passthrough`.

## P3: Web Chat Wiki Experience

`kind: framing`

**Goal**: make wiki activity visible and actionable from web chat.

### 3.1 Add Wiki Activity panel [category: code] (depends: 2.1)

`kind: deliverable`

Targets: `web/src/components/activity/WikiTab.tsx`, `web/src/components/activity/WikiSourceRemovalDialog.tsx`, `web/src/hooks/useWiki.ts`, `web/src/components/activity/ActivityPanelTabs.tsx`, `web/src/components/activity/useActivityPanel.ts`, `web/src/components/activity/ActivityPanel.tsx`

Add a Wiki tab showing current scope, status, recent searches, indexed paths, health
findings, CLI source records, and source/wiki page links. Follow the activity panel
conventions already used by other tabs. Source removal starts from a source record and
always calls `/api/wiki/remove-source` with `dry_run: true` first, renders the CLI preview
payload without schema normalization, and requires explicit user confirmation before sending
`yes: true`. `keep_asset` is an optional confirmed-removal setting.

**Acceptance:**

- 3.1.1 - Wiki tab is registered in the Activity panel and fetches `/api/wiki/status` and `/api/wiki/health`. file: `web/src/components/activity/WikiTab.tsx`.
- 3.1.2 - Wiki tab renders scope, health, degraded services, and actionable file paths. file: `web/src/components/activity/WikiTab.tsx`.
- 3.1.3 - Frontend hook has typed API wrappers for wiki routes. file: `web/src/hooks/useWiki.ts`.
- 3.1.4 - Wiki tab lists CLI source records from `/api/wiki/sources` and exposes source removal actions. file: `web/src/components/activity/WikiTab.tsx`.
- 3.1.5 - Source removal UI always dry-runs first, renders the CLI preview, and requires explicit confirmation before sending `yes: true`. test: `web/src/components/activity/__tests__/WikiTab.test.tsx::test_source_removal_requires_dry_run_confirmation`.

### 3.2 Add chat actions for wiki operations [category: code] (depends: 3.1)

`kind: deliverable`

Targets: `web/src/components/chat/`, `web/src/hooks/useWiki.ts`

Add chat actions for search, read, attach, ingest, compile, audit, and health. Actions call
HTTP routes, render progress and results in chat, and link back to the Wiki Activity panel
state. Read actions call `/api/wiki/read`, which depends on upstream
`gwiki read --format json`; chat renders or previews Markdown `content` supplied by upstream,
and `rendered_text` is not required for the first daemon/web implementation. Attach actions
call `/api/wiki/attach`, which stages uploads in the daemon and maps to `gwiki ingest-file`
through `GwikiGateway.ingest_file`. Ingest actions also support explicit URL batch ingest:
when the user provides one or more URLs, chat sends a URL array to `/api/wiki/ingest`, renders
CLI `accepted` and `failed` entries, and treats partial failures as a completed explicit
write with per-URL follow-up information.

**Acceptance:**

- 3.2.1 - Chat can trigger search, read, attach, ingest, compile, audit, and health actions. file: `web/src/components/chat/`.
- 3.2.2 - Action results show citations, wiki paths, source paths, and degradation messages. file: `web/src/components/chat/`.
- 3.2.3 - Attach/ingest/compile actions require explicit user intent before writes. test: `web/src/components/chat/__tests__/wiki-actions.test.tsx`.
- 3.2.4 - Chat ingest action supports URL batch input and renders CLI accepted/failed results without daemon-side URL fetching. test: `web/src/components/chat/__tests__/wiki-actions.test.tsx::test_url_batch_ingest_action`.

## P4: Hybrid Self-Updating Model

`kind: framing`

**Goal**: keep wiki indexes fresh without turning routine daemon bookkeeping into noisy cron history.

### 4.1 Index immediately after explicit gwiki writes [category: code] (depends: 2.1)

`kind: deliverable`

Targets: `src/gobby/wiki/update_coordinator.py`, `tests/wiki/test_update_coordinator.py`

After explicit writes through attach, ingest-file, collect, compile, remove-source, or
accepted research output, enqueue or run `gwiki index` for the affected scope and changed
paths when the CLI result requires indexing. `ingest-url` is also an explicit write, but its
CLI result already includes once-per-batch indexing for accepted sources; the coordinator
must preserve the `indexed` result and skip duplicate index handoff for that CLI-indexed
batch. Keep the write response visible to the caller and report index degradation
separately. For `remove-source`, use only
`index_status.index_required` from the CLI result to decide whether to index; do not infer
indexing from deleted paths, dry-run previews, or local file inspection. A single write
result must not produce duplicate index handoffs when both changed-path metadata and
`index_status` are present.

**Acceptance:**

- 4.1.1 - Explicit write results trigger same-scope index handoff with changed paths or CLI `index_status.index_required`. test: `tests/wiki/test_update_coordinator.py::test_explicit_write_indexes_changed_paths`.
- 4.1.2 - Index failures are reported as degradation, not hidden success. test: `tests/wiki/test_update_coordinator.py::test_index_failure_degrades`.
- 4.1.3 - Read-only operations never trigger indexing. test: `tests/wiki/test_update_coordinator.py::test_read_only_operations_do_not_index`.
- 4.1.4 - `remove-source` is an explicit write that indexes only when CLI `index_status.index_required` is true. test: `tests/wiki/test_update_coordinator.py::test_remove_source_indexes_only_when_required`.
- 4.1.5 - Coordinator avoids duplicate index handoffs when a write result includes both changed paths and `index_status`. test: `tests/wiki/test_update_coordinator.py::test_index_status_does_not_duplicate_handoff`.
- 4.1.6 - `ingest-url` is an explicit write whose CLI-indexed accepted batch does not trigger duplicate daemon indexing. test: `tests/wiki/test_update_coordinator.py::test_ingest_url_does_not_duplicate_cli_indexing`.

### 4.2 Add debounced daemon watcher for local wiki file changes [category: code] (depends: 4.1)

`kind: deliverable`

Targets: `src/gobby/wiki/watcher.py`, `tests/wiki/test_watcher.py`

Watch configured project and topic wiki roots for local edits. Debounce bursts, group changed paths by scope, and call the update coordinator to index changed files. Ignore generated health/output churn unless it affects indexable wiki state.

**Acceptance:**

- 4.2.1 - Watcher debounces file changes and groups them by scope. test: `tests/wiki/test_watcher.py::test_debounce_groups_scope_changes`.
- 4.2.2 - Watcher triggers index for local edits outside explicit API writes. test: `tests/wiki/test_watcher.py::test_local_edit_triggers_index`.
- 4.2.3 - Watcher ignores `outputs/` and routine `meta/health/` churn unless configured otherwise. test: `tests/wiki/test_watcher.py::test_ignores_noncanonical_churn`.

### 4.3 Add user-visible scheduled wiki jobs [category: code] (depends: 4.1)

`kind: deliverable`

Targets: `src/gobby/wiki/scheduled_jobs.py`, `tests/wiki/test_scheduled_jobs.py`

Add cron-backed scheduled jobs only for user-visible wiki operations: scheduled research, source refresh, health checks, and audits. Jobs must record purpose, scope, user-visible result, and linked `gwiki` command output.

**Acceptance:**

- 4.3.1 - Scheduled jobs exist for research, refresh, health checks, and audits. file: `src/gobby/wiki/scheduled_jobs.py`.
- 4.3.2 - Cron history entries include purpose, scope, command, result, and changed paths. test: `tests/wiki/test_scheduled_jobs.py::test_cron_history_is_user_visible`.
- 4.3.3 - Scheduled jobs use `GwikiGateway` and update coordinator, not direct subprocess calls. test: `tests/wiki/test_scheduled_jobs.py::test_scheduled_jobs_use_gateway`.

### 4.4 Keep lightweight maintenance out of cron history [category: code] (depends: 4.2)

`kind: deliverable`

Targets: `src/gobby/wiki/status.py`, `tests/wiki/test_status.py`, `web/src/components/activity/WikiTab.tsx`

Expose lightweight wiki maintenance/status signals through daemon status or activity state: watcher health, last index time, pending debounce, gateway availability, and service degradation. Do not create cron runs for these routine checks.

**Acceptance:**

- 4.4.1 - Status reports watcher health, last index time, pending debounce, gateway availability, and degradation. test: `tests/wiki/test_status.py::test_status_reports_maintenance_state`.
- 4.4.2 - Routine maintenance/status checks do not create cron history rows. test: `tests/wiki/test_status.py::test_status_not_recorded_as_cron_history`.
- 4.4.3 - Wiki Activity panel consumes the status surface. file: `web/src/components/activity/WikiTab.tsx`.

## VS1: Verification

`kind: verification`

Plan validation:

- `uv run gobby plans validate .gobby/plans/gwiki-daemon-web.md`
- `uv run gobby plans validate .gobby/plans/gwiki-daemon-web.md --mode expansion`

Implementation validation after expansion:

- `GOBBY_TEST_PROTECT=1 uv run pytest tests/test_gwiki_gateway.py tests/servers/routes/test_wiki_routes.py tests/mcp_proxy/tools/test_wiki.py tests/wiki/`
- `npm --prefix web test -- Wiki`
- Manual web smoke: search/read/source listing/source removal dry-run and confirmation/attach/file ingest/URL batch ingest/compile/audit actions update the Wiki Activity panel without duplicate cron-history entries or duplicate indexing.

## AC1: Acceptance Criteria

`kind: verification`

- Daemon/wiki integration goes through `GwikiGateway` and stable `gwiki --format json` contracts.
- `/api/wiki/*` and MCP tools expose search, read, source listing, source removal, attach, file ingest, URL batch ingest, compile, audit, and health.
- Web chat has a Wiki Activity panel, CLI source listing, dry-run-first source removal confirmation, URL batch ingest, and chat actions for common wiki workflows.
- Explicit `gwiki` writes index immediately when required; local file changes index via a debounced watcher.
- URL batch ingest never fetches or persists URLs in daemon/web code and never duplicates the once-per-batch indexing already reported by `gwiki ingest-url`.
- Source removal never deletes files in daemon/web code and daemon indexing runs once only when CLI `index_status.index_required` is true.
- Cron is reserved for user-visible scheduled research, refresh, health checks, and audits.
- Routine maintenance/status stays out of cron-history spam.

## V1 Plan Changelog

`kind: verification`

- **R1 (2026-05-28)**: Initial sibling Gobby repo plan for daemon/web `gwiki` integration. Scoped implementation to daemon gateway, API routes, MCP tools, Wiki Activity panel, chat actions, and hybrid self-updating behavior dependent on the `gobby-cli` `gwiki` JSON/CLI contracts.
- **R2 (2026-06-01)**: Revised daemon/web scope for CLI-owned source lifecycle contracts. Added source listing, dry-run-first source removal, gateway/API/MCP/UI coverage, and `index_status.index_required` coordination to avoid duplicate indexing.
- **R3 (2026-06-02)**: Expanded daemon/web scope for upstream `gwiki ingest-url --format json URL...`. Added URL batch gateway/API/MCP/web coverage, CLI-owned fetch/source/failure/indexing contract, partial/all-failed passthrough expectations, and update-coordinator duplicate-index prevention.
- **R4 (2026-06-02)**: Verified plan against the current codebase for stage-native review. Confirmed target inventory paths resolve (`web/src/components/activity/useActivityPanel.ts`, `ActivityPanelTabs.tsx`, `ActivityPanel.tsx` are the live tab-registration surfaces; `src/gobby/servers/routes/` and `src/gobby/mcp_proxy/tools/` package roots exist; `src/gobby/wiki/` and `web/src/hooks/useWiki.ts` are net-new). Confirmed draft and expansion validation pass, consumer sweep is clean, and the M1 manifest's 10 entries map 1:1 to the 10 deliverable sections. No structural changes required.

## M1 Task Manifest

`kind: manifest`

```yaml
- title: Document gwiki JSON command contracts
  category: docs
  task_type: documentation
  depends_on: []
  validation_criteria: "docs/guides/gwiki-daemon-web.md documents all consumed gwiki commands, operation classes, JSON fields, gateway-only rule, and timeout/degradation behavior"
  labels:
    - covers:gwiki-daemon-web:1.1:1.1.1
    - covers:gwiki-daemon-web:1.1:1.1.2
    - covers:gwiki-daemon-web:1.1:1.1.3
    - covers:gwiki-daemon-web:1.1:1.1.4
    - covers:gwiki-daemon-web:1.1:1.1.5
  implementation_domain: backend
  tdd: false
  source_section: "1.1"
- title: Add GwikiGateway wrapper
  category: code
  task_type: feature
  depends_on:
    - "1.1"
  validation_criteria: "GOBBY_TEST_PROTECT=1 uv run pytest tests/test_gwiki_gateway.py"
  labels:
    - covers:gwiki-daemon-web:1.2:1.2.1
    - covers:gwiki-daemon-web:1.2:1.2.2
    - covers:gwiki-daemon-web:1.2:1.2.3
    - covers:gwiki-daemon-web:1.2:1.2.4
    - covers:gwiki-daemon-web:1.2:1.2.5
    - covers:gwiki-daemon-web:1.2:1.2.6
    - covers:gwiki-daemon-web:1.2:1.2.7
    - covers:gwiki-daemon-web:1.2:1.2.8
    - covers:gwiki-daemon-web:1.2:1.2.9
  implementation_domain: backend
  tdd: true
  source_section: "1.2"
- title: Add wiki HTTP routes
  category: code
  task_type: feature
  depends_on:
    - "1.2"
  validation_criteria: "GOBBY_TEST_PROTECT=1 uv run pytest tests/servers/routes/test_wiki_routes.py"
  labels:
    - covers:gwiki-daemon-web:2.1:2.1.1
    - covers:gwiki-daemon-web:2.1:2.1.2
    - covers:gwiki-daemon-web:2.1:2.1.3
    - covers:gwiki-daemon-web:2.1:2.1.4
    - covers:gwiki-daemon-web:2.1:2.1.5
    - covers:gwiki-daemon-web:2.1:2.1.6
    - covers:gwiki-daemon-web:2.1:2.1.7
  implementation_domain: backend
  tdd: true
  source_section: "2.1"
- title: Add gobby-wiki MCP tools
  category: code
  task_type: feature
  depends_on:
    - "2.1"
  validation_criteria: "GOBBY_TEST_PROTECT=1 uv run pytest tests/mcp_proxy/tools/test_wiki.py"
  labels:
    - covers:gwiki-daemon-web:2.2:2.2.1
    - covers:gwiki-daemon-web:2.2:2.2.2
    - covers:gwiki-daemon-web:2.2:2.2.3
    - covers:gwiki-daemon-web:2.2:2.2.4
    - covers:gwiki-daemon-web:2.2:2.2.5
  implementation_domain: backend
  tdd: true
  source_section: "2.2"
- title: Add Wiki Activity panel
  category: code
  task_type: feature
  depends_on:
    - "2.1"
  validation_criteria: "npm --prefix web test -- WikiTab"
  labels:
    - covers:gwiki-daemon-web:3.1:3.1.1
    - covers:gwiki-daemon-web:3.1:3.1.2
    - covers:gwiki-daemon-web:3.1:3.1.3
    - covers:gwiki-daemon-web:3.1:3.1.4
    - covers:gwiki-daemon-web:3.1:3.1.5
  implementation_domain: frontend
  tdd: true
  source_section: "3.1"
- title: Add chat actions for wiki operations
  category: code
  task_type: feature
  depends_on:
    - "3.1"
  validation_criteria: "npm --prefix web test -- wiki-actions"
  labels:
    - covers:gwiki-daemon-web:3.2:3.2.1
    - covers:gwiki-daemon-web:3.2:3.2.2
    - covers:gwiki-daemon-web:3.2:3.2.3
    - covers:gwiki-daemon-web:3.2:3.2.4
  implementation_domain: frontend
  tdd: true
  source_section: "3.2"
- title: Index immediately after explicit gwiki writes
  category: code
  task_type: feature
  depends_on:
    - "2.1"
  validation_criteria: "GOBBY_TEST_PROTECT=1 uv run pytest tests/wiki/test_update_coordinator.py"
  labels:
    - covers:gwiki-daemon-web:4.1:4.1.1
    - covers:gwiki-daemon-web:4.1:4.1.2
    - covers:gwiki-daemon-web:4.1:4.1.3
    - covers:gwiki-daemon-web:4.1:4.1.4
    - covers:gwiki-daemon-web:4.1:4.1.5
    - covers:gwiki-daemon-web:4.1:4.1.6
  implementation_domain: backend
  tdd: true
  source_section: "4.1"
- title: Add debounced daemon watcher for wiki file changes
  category: code
  task_type: feature
  depends_on:
    - "4.1"
  validation_criteria: "GOBBY_TEST_PROTECT=1 uv run pytest tests/wiki/test_watcher.py"
  labels:
    - covers:gwiki-daemon-web:4.2:4.2.1
    - covers:gwiki-daemon-web:4.2:4.2.2
    - covers:gwiki-daemon-web:4.2:4.2.3
  implementation_domain: backend
  tdd: true
  source_section: "4.2"
- title: Add user-visible scheduled wiki jobs
  category: code
  task_type: feature
  depends_on:
    - "4.1"
  validation_criteria: "GOBBY_TEST_PROTECT=1 uv run pytest tests/wiki/test_scheduled_jobs.py"
  labels:
    - covers:gwiki-daemon-web:4.3:4.3.1
    - covers:gwiki-daemon-web:4.3:4.3.2
    - covers:gwiki-daemon-web:4.3:4.3.3
  implementation_domain: backend
  tdd: true
  source_section: "4.3"
- title: Keep lightweight maintenance out of cron history
  category: code
  task_type: feature
  depends_on:
    - "4.2"
    - "3.1"
  validation_criteria: "GOBBY_TEST_PROTECT=1 uv run pytest tests/wiki/test_status.py && npm --prefix web test -- WikiTab"
  labels:
    - covers:gwiki-daemon-web:4.4:4.4.1
    - covers:gwiki-daemon-web:4.4:4.4.2
    - covers:gwiki-daemon-web:4.4:4.4.3
  implementation_domain: fullstack
  tdd: true
  source_section: "4.4"
```
