# gwiki daemon and web integration

**Plan ID:** gwiki-daemon-web

## O1: Overview

`kind: framing`

Add Gobby daemon, MCP, and web-chat integration for the `gwiki` Rust CLI after the `gobby-cli` `gwiki` plan lands its JSON command contracts. The daemon does not own wiki domain behavior, schema, vault layout, synthesis, or indexing internals. It wraps `gwiki --format json`, exposes API and MCP surfaces, and gives web chat a Wiki Activity panel plus chat actions for search, read, attach, ingest, compile, audit, and health checks.

This plan lives in the Gobby repo because it changes daemon routes, MCP tools, scheduling, watchers, and web UI. It depends on `gobby-cli/.gobby/plans/gwiki.md` for the Rust CLI/library contract.

Daemon AI/multimodal capability routes, structured capability errors, text-generation adoption, and `gwiki_*` hub adoption are covered by the companion plan `.gobby/plans/gwiki-daemon-ai-contract.md`.

## C1: Constraints

`kind: framing`

- **Contract dependency**: daemon and web work consume stable `gwiki --format json` commands. Do not duplicate wiki parsing, source manifests, search ranking, compile, audit, or datastore ownership in Python or TypeScript.
- **Gateway boundary**: the daemon calls `gwiki` through a single `GwikiGateway` wrapper until a future direct Rust linking path exists.
- **No schema ownership leakage**: the daemon must not create, alter, or drop `gwiki_*` tables or wiki graph/vector stores. `gwiki setup` owns explicit setup.
- **Filesystem source of truth**: local vault files remain canonical. API/MCP routes report derived index status and invoke `gwiki index` where appropriate.
- **Explicit writes only**: ingest, attach, compile, and fix-style operations require an explicit user action or scheduled job. Search, read, status, audit, and health routes stay read-only unless their `gwiki` command contract says otherwise.
- **Hybrid freshness model**: explicit `gwiki` writes index immediately; daemon watchers debounce local file changes; cron jobs are only for user-visible scheduled research, refresh, health checks, and audits.
- **Cron signal quality**: lightweight maintenance/status bookkeeping belongs in the daemon automation/status loop, not user-visible cron-history spam.
- **Scope clarity**: every API, MCP, and web result carries project/topic scope identity and never crosses scopes implicitly.

## D1: Dependency Contracts

`kind: framing`

The daemon/web work requires these `gwiki` JSON command contracts from the CLI plan:

- `gwiki status --format json`
- `gwiki index --format json`
- `gwiki search --format json`
- `gwiki backlinks --format json`
- `gwiki ingest-file --format json`
- `gwiki collect --format json`
- `gwiki research --format json`
- `gwiki compile --format json`
- `gwiki audit --format json`
- `gwiki health --format json`

Required JSON fields: `scope`, `command`, `ok`, `degraded`, actionable `paths`, and command-specific payloads. Errors must preserve stderr plus parsed structured guidance when available.

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

### 1.2 Add GwikiGateway wrapper [category: code] (depends: 1.1)

`kind: deliverable`

Targets: `src/gobby/gwiki_gateway.py`, `tests/test_gwiki_gateway.py`

Add a single async wrapper around `gwiki --format json` with methods for status, index, search, read, backlinks, ingest, collect, research, compile, audit, and health. The gateway resolves the binary path, passes project/topic scope, enforces timeouts, parses JSON stdout, captures stderr, and raises typed errors on non-zero exits.

**Acceptance:**

- 1.2.1 - `GwikiGateway` exposes methods for all commands listed in D1. file: `src/gobby/gwiki_gateway.py`.
- 1.2.2 - Gateway parses JSON stdout and preserves stderr on failure. test: `tests/test_gwiki_gateway.py::test_error_preserves_stderr`.
- 1.2.3 - Gateway enforces per-command timeout and reports structured degradation. test: `tests/test_gwiki_gateway.py::test_timeout_degrades`.
- 1.2.4 - No route, MCP tool, watcher, or cron path invokes `gwiki` outside `GwikiGateway`. behavior: "grep for create_subprocess_exec gwiki has only gateway hits" in `src/gobby/`.

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
- `POST /api/wiki/index`
- `POST /api/wiki/attach`
- `POST /api/wiki/ingest`
- `POST /api/wiki/collect`
- `POST /api/wiki/research`
- `POST /api/wiki/compile`
- `POST /api/wiki/audit`

Explicit write routes trigger immediate indexing when the `gwiki` result reports changed vault files.

**Acceptance:**

- 2.1.1 - Wiki route module exposes the listed routes and calls `GwikiGateway`. file: `src/gobby/servers/routes/wiki.py`.
- 2.1.2 - Route tests cover scope validation, JSON response pass-through, and gateway error mapping. test: `tests/servers/routes/test_wiki_routes.py`.
- 2.1.3 - Explicit write routes invoke immediate index handoff when changed paths are reported. test: `tests/servers/routes/test_wiki_routes.py::test_write_routes_trigger_index`.

### 2.2 Add gobby-wiki MCP tools [category: code] (depends: 2.1)

`kind: deliverable`

Targets: `src/gobby/mcp_proxy/tools/wiki.py`, `tests/mcp_proxy/tools/test_wiki.py`

Expose MCP tools for `wiki_search`, `wiki_read`, `wiki_attach`, `wiki_ingest`, `wiki_compile`, `wiki_audit`, and `wiki_health`. Tools use the gateway and return structured JSON with scope identity, paths, citations, and degradation metadata.

**Acceptance:**

- 2.2.1 - MCP wiki tools are registered and use `GwikiGateway`. file: `src/gobby/mcp_proxy/tools/wiki.py`.
- 2.2.2 - Tool schemas include scope, project/topic, and command-specific arguments. test: `tests/mcp_proxy/tools/test_wiki.py::test_tool_schemas`.
- 2.2.3 - Tools preserve gateway degradation and path metadata. test: `tests/mcp_proxy/tools/test_wiki.py::test_degradation_passthrough`.

## P3: Web Chat Wiki Experience

`kind: framing`

**Goal**: make wiki activity visible and actionable from web chat.

### 3.1 Add Wiki Activity panel [category: code] (depends: 2.1)

`kind: deliverable`

Targets: `web/src/components/activity/WikiTab.tsx`, `web/src/hooks/useWiki.ts`, `web/src/components/activity/ActivityPanelTabs.tsx`, `web/src/components/activity/useActivityPanel.ts`, `web/src/components/activity/ActivityPanel.tsx`

Add a Wiki tab showing current scope, status, recent searches, indexed paths, health findings, and source/wiki page links. Follow the activity panel conventions already used by other tabs.

**Acceptance:**

- 3.1.1 - Wiki tab is registered in the Activity panel and fetches `/api/wiki/status` and `/api/wiki/health`. file: `web/src/components/activity/WikiTab.tsx`.
- 3.1.2 - Wiki tab renders scope, health, degraded services, and actionable file paths. file: `web/src/components/activity/WikiTab.tsx`.
- 3.1.3 - Frontend hook has typed API wrappers for wiki routes. file: `web/src/hooks/useWiki.ts`.

### 3.2 Add chat actions for wiki operations [category: code] (depends: 3.1)

`kind: deliverable`

Targets: `web/src/components/chat/`, `web/src/hooks/useWiki.ts`

Add chat actions for search, read, attach, ingest, compile, audit, and health. Actions call HTTP routes, render progress and results in chat, and link back to the Wiki Activity panel state.

**Acceptance:**

- 3.2.1 - Chat can trigger search, read, attach, ingest, compile, audit, and health actions. file: `web/src/components/chat/`.
- 3.2.2 - Action results show citations, wiki paths, source paths, and degradation messages. file: `web/src/components/chat/`.
- 3.2.3 - Attach/ingest/compile actions require explicit user intent before writes. test: `web/src/components/chat/__tests__/wiki-actions.test.tsx`.

## P4: Hybrid Self-Updating Model

`kind: framing`

**Goal**: keep wiki indexes fresh without turning routine daemon bookkeeping into noisy cron history.

### 4.1 Index immediately after explicit gwiki writes [category: code] (depends: 2.1)

`kind: deliverable`

Targets: `src/gobby/wiki/update_coordinator.py`, `tests/wiki/test_update_coordinator.py`

After explicit writes through attach, ingest, collect, compile, or accepted research output, enqueue or run `gwiki index` for the affected scope and changed paths. Keep the write response visible to the caller and report index degradation separately.

**Acceptance:**

- 4.1.1 - Explicit write results trigger same-scope index handoff with changed paths. test: `tests/wiki/test_update_coordinator.py::test_explicit_write_indexes_changed_paths`.
- 4.1.2 - Index failures are reported as degradation, not hidden success. test: `tests/wiki/test_update_coordinator.py::test_index_failure_degrades`.
- 4.1.3 - Read-only operations never trigger indexing. test: `tests/wiki/test_update_coordinator.py::test_read_only_operations_do_not_index`.

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

Implementation validation after expansion:

- `GOBBY_TEST_PROTECT=1 uv run pytest tests/test_gwiki_gateway.py tests/servers/routes/test_wiki_routes.py tests/mcp_proxy/tools/test_wiki.py tests/wiki/`
- `npm --prefix web test -- Wiki`
- Manual web smoke: search/read/attach/ingest/compile/audit actions update the Wiki Activity panel without duplicate cron-history entries.

## AC1: Acceptance Criteria

`kind: verification`

- Daemon/wiki integration goes through `GwikiGateway` and stable `gwiki --format json` contracts.
- `/api/wiki/*` and MCP tools expose search, read, attach, ingest, compile, audit, and health.
- Web chat has a Wiki Activity panel and chat actions for common wiki workflows.
- Explicit `gwiki` writes index immediately; local file changes index via a debounced watcher.
- Cron is reserved for user-visible scheduled research, refresh, health checks, and audits.
- Routine maintenance/status stays out of cron-history spam.

## V1 Plan Changelog

`kind: verification`

- **R1 (2026-05-28)**: Initial sibling Gobby repo plan for daemon/web `gwiki` integration. Scoped implementation to daemon gateway, API routes, MCP tools, Wiki Activity panel, chat actions, and hybrid self-updating behavior dependent on the `gobby-cli` `gwiki` JSON/CLI contracts.

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
