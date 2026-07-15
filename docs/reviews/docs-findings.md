# Review: docs accuracy — code bugs surfaced by guide verification

- **Scope:** `docs/guides/*.md`, both halves alphabetical, plus
  `docs/architecture/*.md`. Guides A: `README.md` through `memory.md` (20 of
  47 guides, 7,196 lines, reviewed at `07db2d036`). Guides B:
  `observability.md` through `worktrees.md` (27 guides, 7,118 lines, reviewed
  at `f2a540306`). Architecture: 6 docs, 1,947 lines, reviewed at
  `daac005bb`. Entries here are cases where the doc states the
  clearly-intended contract and the CODE violates it; pure doc drift was
  fixed directly in the docs in the same commits.
- **Reviewer:** Claude Fable 5 (parallel verifier agents per batch; every
  MAJOR finding re-verified against source by the synthesizer)
- **Commit / branch:** `0.5.0` @ `07db2d036` (guides A) / `f2a540306`
  (guides B) / `daac005bb` (architecture)
- **Summary:** 0 Blocker · 8 Important · 4 Nit — the guides were largely
  honest; where doc and code disagree on intent, the code is the drifted side
  in these cases.

## Findings

### [IMPORTANT] gwiki timeout envelope omits contracted diagnostic fields

- **Where:** `src/gobby/gwiki_gateway.py:370-382` (`_timeout_envelope`),
  kill path at `src/gobby/gwiki_gateway.py:292-304`; contract at
  `docs/guides/gwiki-daemon-web.md:69-72`
- **Failure mode:** The guide's timeout contract says a timeout result "must
  identify the command, scope, timeout value, elapsed time when known,
  stderr/stdout captured before termination". The envelope carries only
  `{ok, command, status, payload: None, stderr: "", error: {type, message}}` —
  no scope, no timeout value (even though `self._timeout_seconds` is in hand),
  no elapsed time, and the kill path never reads the pipes, so stderr/stdout
  are always empty.
- **Why it matters:** Timeout diagnostics are the only signal an operator gets
  for hung gwiki commands; an empty envelope makes degradation guidance
  impossible. The doc contract was left as written (it is the intent).
- **Minimal fix:** Include `scope` identity and `error.timeout_seconds` in
  `_timeout_envelope`; capture whatever the pipes hold before kill.
- **Confidence:** high
- **Related:** the Gateway Error Handling table in the same guide previously
  promised `exit_code`/`stdout_json`/`raw_stdout`/`scope`/`degradations`
  fields that were never implemented anywhere (`gcode grep 'raw_stdout'
  src/gobby` → no matches). That table was rewritten to the implemented
  envelope shape; if the richer contract is still wanted, it should be a code
  task, not a doc revert.

### [IMPORTANT] `sse` transport accepted by validation but unconnectable

- **Where:** `src/gobby/mcp_proxy/models.py:119,145` and
  `src/gobby/config/mcp.py:219` accept `"sse"`;
  `src/gobby/mcp_proxy/transports/factory.py:32-41` has no SSE entry and
  raises `Unsupported transport: sse` at connection time
- **Failure mode:** An MCP server registered with `transport: "sse"` passes
  config/registry validation and is stored, then fails every connection
  attempt with a ValueError raised from the transport factory.
- **Why it matters:** Validation-time acceptance plus connect-time rejection is
  a stored-but-broken state the user can't diagnose from the add flow; the
  failure surfaces later and far from the cause.
- **Minimal fix:** Either remove `"sse"` from the accepted transport lists in
  `models.py`/`config/mcp.py`, or implement an SSE transport in
  `transports/`. Rejecting at validation is the smaller change.
- **Confidence:** high

### [IMPORTANT] cron CLI cannot address jobs by name despite storage support

- **Where:** `src/gobby/cli/cron.py:139-260` (run/toggle/runs/edit/remove all
  call `storage.get_job(job_id)`); `src/gobby/storage/cron.py:218-221`
  (`get_job` is UUID-only); `src/gobby/storage/cron.py:223`
  (`get_job_by_name` exists but has no CLI caller)
- **Failure mode:** Every cron CLI mutation/inspection command fails with
  "Job not found" when given a job name; only UUIDs resolve. The guide
  previously documented `NAME_OR_ID` (now corrected to `JOB_ID`).
- **Why it matters:** Names are the human handle (`cron add -n nightly-health`
  requires one); forcing a `cron list` → copy-UUID round-trip for every
  operation is needless friction, and the storage layer already supports the
  lookup.
- **Minimal fix:** In the cron CLI's job resolution, fall back to
  `get_job_by_name` when the argument is not a UUID.
- **Confidence:** high

### [IMPORTANT] comms secret category `"comms"` is not a valid category

- **Where:** `src/gobby/communications/manager.py:585` stores channel secrets
  with `category="comms"`; `src/gobby/storage/secrets.py:35` defines
  `VALID_CATEGORIES = {"general", "llm", "mcp_server", "memory", "integration"}`
- **Failure mode:** Channel secret writes carry a category the secrets store
  does not recognize as valid (the guide documents this mismatch as a known
  limitation — doc accurate, code inconsistent).
- **Why it matters:** Category-filtered secret listing/cleanup misses comms
  secrets; the validation set and the writers disagree about the taxonomy.
- **Minimal fix:** Add `"comms"` to `VALID_CATEGORIES` or switch the manager
  to `"integration"`.
- **Confidence:** high

### [NIT] `gobby comms status` expects the wrong response shape

- **Where:** `src/gobby/cli/communications.py:56-61` requests
  `/api/comms/channels?status=true` and reads `response.json().get("channels",
  [])`; `list_channels` at
  `src/gobby/servers/routes/communications.py:111-118` accepts no query
  parameter and returns a JSON list directly.
- **Failure mode:** A successful response raises `AttributeError` when the CLI
  calls `.get` on the list. The focused CLI test at
  `tests/cli/test_communications.py:18-44` masks the mismatch by returning a
  synthetic `{"channels": [...]}` object and expecting the ignored query
  parameter.
- **Minimal fix:** Request `/api/comms/channels`, consume the returned list,
  and keep status rendering tolerant of absent optional fields.
- **Validation:** Update `test_comms_status_success` to return the route's list
  shape and assert the query-free path, then run
  `GOBBY_TEST_PROTECT=1 uv run pytest tests/cli/test_communications.py -k comms_status -v`.
- **Confidence:** high

## Findings — guides B (observability.md → worktrees.md)

### [IMPORTANT] `/#integrations` deep link silently falls back to chat

- **Where:** `web/src/components/app/appNavigation.tsx:32-42`
  (`APP_VALID_TABS` set); nav item registered at `appNavigation.tsx:60` and
  rendered/hash-written by `web/src/App.tsx:211-213, 916-917`; hash fallback
  at `web/src/App.tsx:204-208`
- **Failure mode:** `"integrations"` is missing from `APP_VALID_TABS`, so
  loading or refreshing `/#integrations` falls back to the chat tab even
  though the Integrations nav item exists, renders the page, and writes
  `#integrations` to the hash. Deep links and refreshes silently lose the
  page; web-ui.md documents the route as the intent.
- **Why it matters:** The documented route works only via in-app clicks; any
  bookmark, refresh, or shared link lands on chat with no error.
- **Minimal fix:** Add `"integrations"` to the `APP_VALID_TABS` set.
- **Confidence:** high

### [IMPORTANT] `gobby worktrees sync --source` is a silent no-op

- **Where:** `src/gobby/cli/worktrees.py:297-300` sends `source_branch`;
  `src/gobby/mcp_proxy/tools/worktrees/_sync.py:84-88` (`sync_worktree`
  accepts only `worktree_id`, `strategy`, `project_path`);
  `src/gobby/mcp_proxy/tools/internal.py:307-317` silently drops unknown
  kwargs
- **Failure mode:** The CLI help promises "Source branch to sync from
  (default: base branch)", but the proxy filters the unknown
  `source_branch` argument before dispatch, so sync always runs against
  `worktree.base_branch` regardless of `--source` — no warning, no error.
- **Why it matters:** An operator syncing from a non-base branch gets a
  successful-looking sync against the wrong branch.
- **Minimal fix:** Add `source_branch: str | None = None` to `sync_worktree`
  and thread it into the sync path, or remove `--source` from the CLI.
  (worktrees.md now discloses the no-op.)
- **Confidence:** high

### [IMPORTANT] `gobby qdrant install --port` cannot produce a working install

- **Where:** `src/gobby/cli/qdrant.py:20` (flag);
  `src/gobby/cli/installers/qdrant.py:65-82` (compose up without env
  override), `:96-101` (health check on the custom port);
  `src/gobby/data/docker-compose.services.yml:33`
  (`"${GOBBY_QDRANT_HTTP_PORT:-6333}:6333"`)
- **Failure mode:** Nothing ever sets `GOBBY_QDRANT_HTTP_PORT` (repo-wide,
  the only reference is the compose default), so the container always binds
  host 6333 while the installer health-checks `http://localhost:<port>` and
  persists a `qdrant_url` the container does not listen on. Any non-default
  `--port` value fails install and leaves broken config; `gobby start`
  likewise never sets the env var.
- **Why it matters:** The documented remedy for a 6333 port conflict
  (system-requirements.md troubleshooting) cannot work.
- **Minimal fix:** Pass `env={**os.environ, "GOBBY_QDRANT_HTTP_PORT":
  str(port)}` to the compose subprocess in `install_qdrant` and persist the
  value (e.g. `~/.gobby/services/.env`) so restarts honor it.
- **Confidence:** high

### [NIT] `RuleDefinition.to_rule_definition_body` is dead code that can only raise

- **Where:** `RuleDefinition.to_rule_definition_body` at
  `src/gobby/workflows/definitions.py:64-77` passes `effect=` to
  `RuleDefinitionBody`; that model declares and validates `effects` at
  `src/gobby/workflows/definitions.py:260-277`.
- **Failure mode:** Every call fails the `"'effects' is required"` validator.
  The method has no callers in `src/` or `tests/`, so existing tests do not
  expose the broken conversion.
- **Minimal fix:** Pass `effects=[effect]`, or delete the conversion method if
  it is not part of the supported API.
- **Validation:** Add a unit test that converts a `RuleDefinition` and asserts
  the resulting body contains the expected single effect, then run the
  focused `tests/workflows/test_rule_definitions.py` file.
- **Confidence:** high

## Findings — architecture docs (docs/architecture/)

### [NIT] Phantom `gobby.storage.mcp_db` import in route dependencies

- **Where:** `src/gobby/servers/routes/dependencies.py:23`
  (`from gobby.storage.mcp_db import MCPDatabaseManager` under
  `TYPE_CHECKING`), used as return annotation at
  `src/gobby/servers/routes/dependencies.py:82`
- **Failure mode:** The module does not exist — `uv run python -c "import
  gobby.storage.mcp_db"` raises `ModuleNotFoundError`. The real class is
  `LocalMCPManager` at `src/gobby/storage/mcp.py:12`. The phantom import is
  masked by `ignore_missing_imports = true` in mypy config, so the
  annotation is silently unchecked.
- **Why it matters:** A type annotation referencing a nonexistent module
  documents an API that was removed; type checking on that dependency
  function is a no-op, and the stale name propagated into architecture docs
  (now corrected to `LocalMCPManager`).
- **Minimal fix:** Repoint `dependencies.py:23` and `:82` to
  `gobby.storage.mcp.LocalMCPManager`.
- **Validation:** Add a focused test in
  `tests/servers/routes/test_dependencies.py` that imports `LocalMCPManager`
  from `gobby.storage.mcp` and asserts
  `get_mcp_db_manager`'s return annotation names it; run that focused file
  plus Ruff on `src/gobby/servers/routes/dependencies.py`.
- **Confidence:** high

## Systemic patterns

Two recur. First, **aspirational contracts written as present tense**: the
gwiki gateway error/timeout tables and the comms guide's limitation notes both
describe richer behavior than the code ships; the guides drift into "must"
language for fields no writer produces. Docs that state contracts should be
backed by a test or marked as design intent. Second, **validation/consumer
splits**: `sse` passes validation but cannot connect; `"comms"` is written but
not valid; `?status=true` is sent but never read. In each case two layers
disagree about an enum or surface and nothing forces them to converge —
shared constants (transport map, category set) imported by both sides would
close the class.

Guides B adds a third: **surface accepts, plumbing drops**. The CLI accepts
`--source` but the proxy filters the argument; the installer accepts `--port`
but never exports the env var the compose template reads; the nav registers
Integrations but the hash whitelist omits it. Each is an interface that
accepts input no downstream layer consumes — a contract test from flag/route
to effect would catch all three.
