# Design: gcode/gwiki MCP shim for sandboxed agents

## Context

Shell-capable agents run `gcode`/`gwiki` directly via Bash — they need nothing. But Gobby
spawns agents in sandboxed worktrees/clones that may have **no shell** or **no filesystem
access to the binaries**, so they currently cannot consult the code index or the knowledge
vault. The daemon runs *outside* the sandbox and already owns DB/broker access and subprocess
wrappers for both binaries. This design exposes the binaries as first-class MCP tools so a
sandboxed session can ask the daemon to run them and get structured results back.

**Why a shim and not just bundling the binaries into sandboxes?** gcode/gwiki are *thin
clients*, not self-contained tools — they query daemon-owned backends (PostgreSQL hub index,
FalkorDB graph, Qdrant vectors, embedding/model API). The index lives in the hub DB, not the
repo. Shipping the binary into a sandbox ships the client without the database, the credentials,
or (for no-shell agents) any way to exec it. Bundling can't solve the three real blockers — no
exec channel, no DB reachability, no credentials — and the workarounds (open DB network +
inject hub credentials into every sandbox) would hand each sandbox unscoped read/write to the
*entire* hub, inverting the isolation the sandbox exists to provide. Only the daemon should hold
DB access; MCP is the one transport a no-shell/remote agent already speaks.

**Scope:** this shim is *only* for **no-shell** and **remote/no-DB-access** sandboxes. Shell-
capable same-host agents need nothing — the binaries are already at `~/.gobby/bin` (resolved
from `$HOME`, not per-worktree) with localhost backends and `~/.gobby/bootstrap.yaml` creds, and
they keep using Bash. We do not build for that case.

**Goal = parity, not read-only.** The shim should give a sandboxed agent the same gcode/gwiki
surface a shell-capable agent has via the binary — queries **and** mutations. Artificially
restricting it to read-only would defeat the purpose (a sandboxed agent would be a second-class
citizen vs. a shell agent). The exposed set is defined non-arbitrarily by the contract itself:
the **`daemon_consumed=true`** flag is the contract's own designation of "commands the daemon
is the intended executor for, with a stable JSON surface" — which is exactly the shim's mandate,
and also the only set we can derive schemas from. The whitelist is therefore
`daemon_consumed=true`, minus `contract` (redundant with MCP discovery). Local-bootstrap /
maintenance ops (`init`, `setup`, `prune`, `invalidate`, `projects`, `repo-outline`,
`embeddings doctor`, `vector rebuild/clear`, `graph report`) are `daemon_consumed=false` and
fall out of scope naturally — no hand-curated allow/deny list needed.

Three facts discovered during research shape the whole plan:

1. **The implementation is ahead of the contract (now repaired).** Every gcode query command
   (`outline`, `symbol`, `symbols`, `symbol-at`, `tree`, `imports`, `callers`, `usages`,
   `path`, `blast-radius`) already emits clean, stable JSON today; the `gcode` contract was
   pinned at v1 and didn't mark them `daemon_consumed`. **That repair is already done** — the
   installed `~/.gobby/bin/gcode` reports `contract_version: 2` with those commands
   `daemon_consumed=true` and `json_output_keys` populated (verified). The `gwiki` contract
   (v5) was already healthy.
2. **Decisions locked with the user:** schema derivation = **build the registry at startup
   from the vendored contract JSON** (drift test guarantees vendored == real CLI); sequencing
   = **gobby-cli first** (done); scope = **parity (queries + mutations)** per the
   `daemon_consumed` whitelist above.
3. **`callees` was a hallucination in the brief.** No such command exists; the real graph
   surface is `callers`/`usages`/`path`/`imports`/`blast-radius`. Dropped, no gobby-cli change.

### Contract corrections vs. the task brief
- **`blast-radius` takes `--depth`/`--token-budget`, not `--limit`** (positional `<TARGET>`).
- `symbol`/`symbol-at` return a heavy `source` field — see Bounded Output.
- Mutations included via parity: `gcode codewiki`, `graph sync-file`/`overview`/`file`/
  `neighbors`/`blast-radius`/`clear`/`rebuild`/`cleanup-orphans`, `vector cleanup-orphans`,
  `index`; `gwiki ingest-file`/`ingest-url`/`compile`/`collect`/`audit`/`refresh`/
  `sync-sessions`/`remove-source`/`index`. All are project-scoped (see Security), so a sandbox
  can only mutate its **own** project's projection — the same blast radius a shell agent already
  has. Heavy rebuilds (`index`, `graph rebuild`) should route through the daemon's existing
  task-mutex/maintenance path to avoid racing the daemon's own scheduler (see Execution model).

---

## Tool surface (derived from contract; whitelist = `daemon_consumed`)

All schemas are **generated at daemon startup** from the vendored contract files; the tables
below are the expected result, not hand-authored schemas. `--project`/`--format`/`--quiet`/
`--verbose`/`--no-freshness` are **never** exposed as tool params — the shim controls them.
**No tool takes a `project_id`/`project`/path-root argument** (see Security). Queries are the
high-traffic surface and are detailed below; mutations (also `daemon_consumed`) are listed
compactly after each query table.

### Server `gobby-code` (NEW internal registry) — query tools

| MCP tool | CLI command | Input params (from contract) | Result (json_output_keys) |
| --- | --- | --- | --- |
| `code_search` | `search QUERY [PATH...]` | `query`*, `paths[]`, `limit`, `offset`, `kind`, `language`, `token_budget` | `PagedResponse<SearchResult>` |
| `code_search_symbol` | `search-symbol` | same as search | `PagedResponse<SearchResult>` |
| `code_grep` | `grep PATTERN [PATH...]` | `pattern`*, `paths[]`, `fixed_strings`, `ignore_case`, `word`, `context`/`before_context`/`after_context`, `glob[]`, `max_count` | `GrepResponse` |
| `code_outline` | `outline FILE` | `file`*, `summarize` | `[OutlineSymbol]` |
| `code_symbol` | `symbol ID` | `id`*, `include_source` (shim-added, default false) | `Symbol` |
| `code_symbols` | `symbols ID...` | `ids[]`*, `include_source` | `[Symbol]` |
| `code_symbol_at` | `symbol-at PATH[:LINE[:COL]]` | `location`*, `include_source` | `Symbol` |
| `code_tree` | `tree` | (none) | `[TreeEntry]` |
| `code_callers` | `callers SYMBOL` | `symbol`*, `limit`, `offset` | `PagedResponse<GraphResult>` |
| `code_usages` | `usages SYMBOL` | `symbol`*, `limit`, `offset`, `token_budget` | `PagedResponse<GraphResult>` |
| `code_path` | `path A B` | `from`*, `to`*, `max_depth` | `PathResponse` |
| `code_imports` | `imports FILE` | `file`* | `PagedResponse<GraphResult>` |
| `code_blast_radius` | `blast-radius TARGET` | `target`*, `depth`, `token_budget` | `PagedResponse<GraphResult>` |

BM25-only fallbacks (useful when semantic/graph degrade): `code_search_text`,
`code_search_content` — same shape as `code_search`.

Result key sets confirmed from the live CLI (these populate the repaired contract):
- `SearchResult`: `id, name, qualified_name, kind, language, file_path, line_start, line_end, score, rrf_score?, signature?, summary?, sources[]?`
- `OutlineSymbol`: `id, name, kind, line_start, line_end, signature`
- `Symbol` (symbol/symbol-at/symbols), per installed v2 contract: `id, project_id, file_path, name, qualified_name, kind, language, byte_start, byte_end, line_start, line_end, signature, content_hash, summary, created_at, updated_at, source` (note: runtime JSON also emits `docstring`, which the contract omits — treat `output_schema` as advisory, do **not** hard-validate output keys)
- `TreeEntry`: `file_path, language, symbol_count`
- `GraphResult`: `id, name, file_path, line, confidence, relation?, distance?, metadata?`
- `PathResponse`: `project_id, found, from, to, max_depth, hops, path[]`

**`gobby-code` mutation tools (parity, `daemon_consumed`):** `code_index`,
`code_codewiki`, `code_graph_sync_file`, `code_graph_overview`, `code_graph_file`,
`code_graph_neighbors`, `code_graph_blast_radius`, `code_graph_clear`, `code_graph_rebuild`,
`code_graph_cleanup_orphans`, `code_vector_cleanup_orphans`. Schemas derived the same way; each
binds to the matching existing `GcodeGateway` projection method (most already exist —
`graph_sync_file`, `graph_overview`, `graph_rebuild`, `vector_*`, `codewiki`). `index` and
`graph rebuild` route through the daemon's task-mutex path (see Execution model).

### Server `gobby-wiki` (EXISTING registry — extend, don't duplicate)

`gobby-wiki` already exists and already exposes most of the parity surface, **including
mutations** (`wiki_ingest`, `wiki_compile`, `wiki_attach`, `wiki_audit`, `wiki_remove_source`,
`wiki_collect`) — which the parity decision keeps as-is. The work here is to close gaps and put
the read tools on the same contract-derived builder for drift safety:

| MCP tool | CLI command | Status |
| --- | --- | --- |
| `wiki_search` | `search QUERY` | exists — move onto contract-derived builder |
| `wiki_ask` | `ask QUESTION` | exists — re-validate; degradation envelope |
| `wiki_read` | `read --path\|--title` | exists |
| `wiki_backlinks` | `backlinks PAGE` | **NEW** (gateway+HTTP exist; MCP tool missing) |
| `wiki_sources` | `sources` | exists as `wiki_list_sources` |

gwiki commands are already `daemon_consumed=true` with full `json_output_keys`, so **no gwiki
contract change is needed** — the existing mutation tools and the read tools are all already on
the daemon-consumed surface.

---

## Proxy integration & routing

**Decision: first-class internal `gobby-` registries, not a parallel/external MCP server.**
This matches every existing internal domain (`gobby-tasks`, `gobby-memory`, `gobby-wiki`) and
inherits the proxy's progressive discovery, enforcement hooks, and context seeding for free.

- Build two `InternalToolRegistry` instances and register them in
  `src/gobby/mcp_proxy/registries.py` `setup_internal_registries()` (where `create_wiki_registry`
  is already wired). `gobby-code` is new; `gobby-wiki` gets `wiki_backlinks` added.
- Routing path is unchanged and already correct for internal tools: HTTP
  `POST /api/mcp/tools/call` (`src/gobby/servers/routes/mcp/endpoints/execution.py`) →
  `ToolProxyService` → `InternalRegistryManager` (prefix `gobby-`). `canonicalize_call_tool_wrapper`
  (`src/gobby/mcp_proxy/_call_tool_wrapper.py`) normalizes wrapper vs. nested args before
  dispatch; our tools live behind it and need no special handling.
- Discovery (`list_mcp_servers` / `list_tools` / `get_tool_schema`) works automatically once
  the registries are added — no extra plumbing.

### Contract-derived registry builder (the one new mechanism)
**The MCP tool surface is built dynamically in-process at daemon startup — but data-driven, not
codegen and not blind reflection.** The tool *list* and arg *schemas* are generated from the
vendored contract (so they can never drift from the CLI); the selection rule
(`daemon_consumed=true` minus `contract`) and each tool's *handler* (a `GcodeGateway`/
`GwikiGateway` method) are explicit Python. No separate external MCP process is spawned —
`gobby-code` is an in-memory `InternalToolRegistry`, the same first-class kind as
`gobby-tasks`/`gobby-wiki`, that the proxy already serves via progressive discovery. A small
data-driven builder turns a vendored contract into such a registry:
- Load `tests/contracts/{gcode,gwiki}.contract.json` at startup.
- Select commands where `daemon_consumed == true` and name != `contract` (the contract's own
  parity designation — no hand-maintained allow/deny list).
- For each, map positionals + value/boolean flags → JSON-Schema `inputSchema` (drop
  shim-controlled flags; `value_name=N` → integer; `allowed_values` → enum; required positionals
  → `required`); attach `json_output_keys` as an advisory `output_schema`; bind the handler to
  the matching gateway method. Commands with no matching gateway method yet (a few gcode
  mutations) are added to the gateway as thin `_run_json` wrappers.
- A unit test asserts the **built tool set == the `daemon_consumed` set** of the vendored
  contract — so contract drift (a command added/removed/reflagged) fails CI. This is the "never
  drift" guarantee, layered on the existing vendored-vs-real-CLI conformance test
  (`tests/test_cli_contracts.py`).

---

## Execution model

**Subprocess, reusing the existing gateways.** Both `GcodeGateway`
(`src/gobby/code_index/gcode_gateway.py`) and `GwikiGateway` (`src/gobby/gwiki_gateway.py`)
already: resolve the binary via `resolve_native_bin()` (`~/.gobby/bin/` → PATH), run
`asyncio.create_subprocess_exec(... --format json)` with timeouts, parse JSON, and raise typed
`*Unavailable`/`*Json`/`*Command` errors. Config (Postgres/FalkorDB/Qdrant) flows to the child
via inherited `os.environ`, exactly as the daemon already does for indexing.

- **gwiki**: gateway already has `search/ask/read/backlinks/sources` plus the mutation methods
  (`ingest/compile/collect/audit/...`). No new gateway code beyond wiring `wiki_backlinks`.
- **gcode**: `GcodeGateway` today has the *projection/mutation* methods (graph/vector sync,
  `codewiki`) but **no query methods** — add them (`search`, `grep`, `outline`, `symbol`,
  `symbols`, `symbol_at`, `tree`, `callers`, `usages`, `path`, `imports`, `blast_radius`,
  `search_text`, `search_content`) as thin wrappers over the existing `_run_json` helper. This
  is the bulk of the daemon code. The mutation tools mostly reuse existing gateway methods.
- **Heavy mutations (`index`, `graph rebuild`):** route through the daemon's existing
  per-task mutex / maintenance path (`task_dispatch_mutex`, code-index maintenance) rather than
  firing an ad-hoc subprocess, so an agent-triggered rebuild can't race the daemon's own
  scheduler. Lightweight mutations (`graph sync-file`, `codewiki`, `gwiki ingest/compile`) run
  directly via the gateway.
- In-process Rust binding is rejected: no stable library API, the binaries are CLI-first and
  already emit JSON, and subprocess matches the established daemon pattern. Latency is a process
  spawn (~tens of ms) — acceptable for agent-initiated queries. Concurrency is bounded by the
  daemon event loop; gateways are async and per-call.

---

## Project scoping & security model

The sandbox boundary is the crux: the daemon executes **outside** the sandbox with full DB
access, so a sandboxed agent must not be able to widen its scope.

1. **No project/path arguments, ever.** Tools expose only query params — never `project_id`,
   `project`, or a raw filesystem root. Scope is taken **entirely from the authenticated
   session context** the proxy already seeds (`_set_context_for_request` reads `X-Gobby-*`
   headers → context vars; tools read `get_project_context()`). This simultaneously (a) kills
   arbitrary-`project_id` injection and (b) sidesteps the known double-presence quirk (where
   `project_id` had to appear both top-level and inside `arguments`) — there is no `project_id`
   arg to disagree.
2. **Resolve caller project → root, inject `--project <root>`.** The shim looks up the caller's
   resolved `project_id` → project root path and passes it as `--project`. Reuse the daemon's
   existing project→root resolution — the same `roots_by_scope` mapping used in
   `src/gobby/runner_lifecycle_periodic.py` to construct the gateways, sourced from the projects
   registry (`project_id`/root pair). The agent never supplies the path, so path traversal is
   impossible.
3. **Verify the round-trip.** Every gcode JSON payload carries `project_id`; the shim asserts
   it equals the caller's resolved id and rejects on mismatch (defense in depth against a
   misresolved root). gwiki is scoped via `--project`/`--topic` at gateway construction.
4. **Mutations are scoped identically.** A mutation tool gets the same daemon-pinned
   `--project <root>`, so a sandboxed agent can only mutate **its own** project's projection —
   the exact blast radius a shell-capable agent in that project already has. No cross-project
   write is reachable.
5. **`file`/`path`/`id` args are index-relative, not FS reads.** `code_outline`/`code_symbol_at`
   resolve against the indexed project; they cannot escape to arbitrary files because gcode
   queries the index DB, not the filesystem, and the project root is daemon-pinned.
6. **Bounded by construction** (next section) so an agent can't exhaust its own (or the daemon's)
   resources with an unbounded result set.

---

## Bounded-output policy

Agents have context limits; every tool is bounded.

- **Default `limit`** on every paged command (`search*`, `callers`, `usages`, `imports`,
  `blast_radius`): default **20**, hard max **100** (clamp server-side, ignore larger requests).
- **`token_budget`** passed through where the CLI supports it (`search`, `usages`,
  `blast_radius`) with a sane shim default; the CLI does the trimming and reports `truncated`.
- **`grep`**: default `max_count` (e.g. 50) and surface the CLI's `truncated` flag.
- **Symbol bodies are the main hazard.** `symbol`/`symbol_at`/`symbols` return a full `source`
  field (can be hundreds of lines). **Default: strip `source`** and return
  signature+docstring+ranges; expose `include_source: bool` (default false) and, when true,
  cap the body to a token budget with an explicit `source_truncated` marker.
- **gwiki** `search`/`ask` are already bounded (snippet + `prompt_token_budget`); pass through
  `limit` and surface `truncated`/`truncated_components`.
- Adopts bake-off candidate **C8** (token/result budget); defaults documented per-tool in the
  generated schema descriptions.

---

## Degradation mapping (degraded ≠ error)

FalkorDB (graph), Qdrant (semantic), and the embedding/model API degrade independently. The
shim returns a **clean, uniform envelope** and only raises MCP errors for genuine failures.

Uniform success envelope wrapping the raw CLI payload:
```
{ "ok": true, "data": <cli json>, "degraded": bool, "degraded_sources": [..],
  "truncated": bool, "warnings": [..] }
```
- **gwiki** already speaks this vocabulary (`degraded`, `degraded_sources[]`, `truncated`,
  `truncated_components[]`, `warnings[]`, `ai.status`/`ai.error`) — pass through.
- **gcode** signals degradation implicitly: graph down → `callers`/`usages`/`blast_radius`/
  `path` return empty `results`/`found:false` (not an error); `search` drops `semantic`/
  `graph_expand` from each result's `sources[]`. The shim **infers** `degraded_sources` by
  diffing expected vs. present sources and from empty graph results, normalizing to the gwiki
  vocabulary so agents get one consistent shape.

Error mapping (MCP `isError` result, not envelope):
- `invalid_input` / `not_found` (gcode/gwiki) → user-error MCP result with the message.
- binary missing (`GcodeUnavailableError`/`GwikiUnavailableError`) → "code intelligence
  unavailable" MCP result.
- `backend_unavailable`/`index_unavailable` (gcode), `daemon`/`index`/`search` (gwiki) →
  prefer the degraded envelope when the command still returns partial JSON; raise only if no
  payload is produced.
- `missing_project` should be unreachable (shim always injects); treat as internal error.

---

## Decisions (resolved — no open questions)

- **Scope = parity (queries + mutations), gated by `daemon_consumed`.** Not read-only; the
  shim mirrors the binary surface a shell agent has, scoped to the caller's project.
- **Pre-existing `gobby-wiki` mutation tools stay as-is.** Per the parity decision they remain
  available; no gating task is needed.
- **`callees` dropped.** Hallucinated in the brief; not a CLI command, no gobby-cli addition.
- **Project→root resolution** reuses the daemon's existing `roots_by_scope` mapping
  (`runner_lifecycle_periodic.py`, projects registry) — same source the gateways already use.

---

## Phased build plan

### Phase 0 — gobby-cli contract repair (PREREQUISITE, other repo) — ✅ DONE & VERIFIED
The gobby-cli contract repair is already complete. The installed `~/.gobby/bin/gcode` reports
`contract_version: 2` with all 12 query commands (`outline`, `symbol`, `symbol-at`, `symbols`,
`tree`, `imports`, `blast-radius`, `callers`, `usages`, `path`, `search-text`, `search-content`)
`daemon_consumed=true` with populated `json_output_keys` — verified via
`gcode contract --format json`. No gwiki change needed. Daemon work can start immediately.

### Phase 1 — Smallest useful slice: `code_search` end-to-end (this repo)
1. Refresh vendored `tests/contracts/gcode.contract.json` from the repaired CLI.
2. Add `GcodeGateway.search()` (read method over `_run_json`).
3. Add the contract-derived registry builder; build `gobby-code` with **only** `code_search`.
4. Project-scope injection + `project_id` round-trip assertion + bounded `limit`.
5. Register in `setup_internal_registries()`.
6. Tests: registry-built-from-contract unit test; project-scope rejection test; an isolated
   end-to-end test proving a sandboxed-style call (`POST /api/mcp/tools/call` with caller
   headers) returns scoped results. **This is the optional prototype, made real.**

### Phase 2 — Full gcode query surface
Add the remaining `GcodeGateway` query methods; the builder auto-exposes every query command
(`daemon_consumed`). Includes `include_source` handling for symbol tools, degradation inference,
`token_budget` pass-through. Per-tool tests for shape + bounding + degraded-backend behavior.

### Phase 3 — gcode mutation surface (parity)
Wire the `daemon_consumed` mutation commands (`code_codewiki`, `code_graph_*`,
`code_vector_cleanup_orphans`, `code_index`). Reuse existing `GcodeGateway` projection methods;
add thin wrappers for any missing. Route `index`/`graph rebuild` through the daemon task-mutex
path. Tests: project-scoped mutation can't touch another project; mutex serialization.

### Phase 4 — gwiki surface
Add `wiki_backlinks`; move `wiki_search`/`wiki_ask`/`wiki_read`/`wiki_sources` onto the
contract-derived builder. Existing gwiki mutation tools stay. Degradation envelope unification.

### Phase 5 — Hardening & docs
Drift test wiring (built tool set == `daemon_consumed` set of the vendored contract),
skill/docs note so agents discover `gobby-code`/`gobby-wiki`.

---

## Verification

- **Contract truth:** `gcode contract --format json | jq '.commands[] | select(.daemon_consumed)'`
  lists the parity surface (verified at v2); daemon vendored-vs-real drift test
  (`tests/test_cli_contracts.py`) green.
- **Registry naming:** internal server is `gobby-code` (matches the `gobby-code` crate).
- **Registry built from contract:** `GOBBY_TEST_PROTECT=1 uv run pytest` the new builder test —
  asserts the built tool set == the `daemon_consumed` set (minus `contract`), so a drift in
  either direction fails CI.
- **End-to-end sandboxed round-trip:** start an isolated test daemon; `POST /api/mcp/tools/call`
  with `{server_name:"gobby-code", tool_name:"code_search", arguments:{query:"RuleEngine"}}`
  and caller `X-Gobby-*` headers → scoped JSON results, no `project_id` arg accepted.
- **Scope rejection:** assert a forged/cross-project caller context yields an error, never
  another project's data — for both a query and a mutation tool.
- **Degradation:** with FalkorDB stopped, `code_callers` returns `{ok:true, degraded:true,
  degraded_sources:["falkordb"], data:{results:[]}}` — not an error.
- **Bounded:** `code_symbol` omits `source` by default; `limit` clamps at 100.
- Manual: discovery chain (`list_tools("gobby-code")` → `get_tool_schema` → `call_tool`).
