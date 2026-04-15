> Superseded on April 14, 2026 by [Rust Migration Epic](../rust-migration-epic.md).
> Preserved as historical input only. Do not use this file as the active
> migration plan.

# Rust Port: Phase 1 — Foundation Crates

## Context

Gobby's Python daemon is ~184K LOC across 670 files. gsqz and gcode are already ported to Rust in the gobby-cli monorepo (~5.8K LOC, 2 crates). This plan covers the first wave of Rust extraction: shared foundation, storage layer (tasks + memory + config), and the rule engine as a standalone crate.

The hook dispatcher (`hook_dispatcher.py`) is initially Python but is replaced by a compiled Rust binary (`gobby-hook`, Crate 4) in this phase — it's a hot path invoked on every hook event, so eliminating Python cold-start is high-value. The *daemon-side* hook orchestrator (session lookup, event enrichment, webhooks, broadcasting) stays in Python; only the CLI-invoked dispatcher binary moves to Rust.

Docker-based Kùzu+Qdrant coexists with this work — the Rust crates talk to them over HTTP, not embedded.

---

## Crate Dependency Graph

```text
gcode ──────> gobby-core
gsqz ──────> gobby-core
gobby-storage ──> gobby-core
gobby-rules ───> gobby-core (types only)
                  gobby-storage (dev-deps, integration tests)
gobby-hook ────> gobby-core (bootstrap, daemon, project)
```

---

## Crate 1: `gobby-core` — Shared Foundation

**Goal:** Eliminate duplication between gcode and gsqz. ~500 LOC extracted.

**Location:** `crates/gobby-core/`

### Modules

| Module | Source | Purpose |
|--------|--------|---------|
| `bootstrap.rs` | gcode/config.rs:152-234, gsqz/config.rs | bootstrap.yaml parsing, daemon URL resolution, GOBBY_PORT env |
| `daemon.rs` | gsqz/daemon.rs (81 LOC) | ureq HTTP client for daemon API (get_json, post_json) |
| `db.rs` | gcode/db.rs (38 LOC) | SQLite connection helpers (WAL, FK, busy timeout) |
| `secrets.rs` | gcode/secrets.rs (179 LOC) | Fernet decryption (machine_id + secret_salt -> PBKDF2 -> decrypt) |
| `project.rs` | gcode/project.rs (338 LOC) | Project root detection, project.json, UUID5 generation |
| `error.rs` | new | GobbyError enum with thiserror |

### Feature Gates

- `sqlite` — rusqlite dep (db.rs)
- `secrets` — fernet, pbkdf2, hmac, sha2, base64 (secrets.rs)
- `daemon` — ureq, serde_json (daemon.rs)

### Migration

- **gcode:** Replace `crate::db`, `crate::secrets`, `crate::project`, bootstrap parts of `crate::config` with `gobby_core::*`. gcode-specific config (Neo4j, Qdrant, Context) stays in gcode. Remove direct deps on fernet/pbkdf2/hmac/openssl/base64.
- **gsqz:** Replace `crate::daemon` with `gobby_core::daemon::DaemonClient`. Remove ureq/serde_json optional deps. Config stays unchanged (gsqz-specific YAML).

### Verification

- `cargo test -p gobby-core` — unit tests for bootstrap resolution, secret decryption, project detection
- `cargo test -p gobby-code` — existing tests still pass
- `cargo test -p gobby-squeeze` — existing tests still pass
- `cargo clippy --workspace` clean

---

## Crate 2: `gobby-storage` — Storage Layer

**Goal:** Port task, memory, config, and dependency managers to Rust. ~3,500 LOC new.

**Location:** `crates/gobby-storage/`

### Key Decisions

- **deadpool-sqlite** for async connection pooling (not r2d2 — async-native, aligns with tokio)
- **Embed v182 baseline schema** directly via `include_str!` — no migration history
- **`&'static str` column names** in UpdateBuilder instead of Python's regex validation — compile-time safety
- **`Explicit<T>` enum** for sentinel pattern: `Unset` vs `Set(Option<T>)` — replaces Python's `_UNSET = object()`

### Module Layout

```text
src/
  lib.rs
  error.rs              — StorageError enum (thiserror)
  database.rs           — Pool, Database struct, transaction/transaction_immediate
  schema.rs             — embed baseline_schema.sql, ensure_schema()
  config_store.rs       — 14 methods: get/set/delete/list_keys/set_secret
  tasks/
    mod.rs
    models.rs           — Task (41 fields), TaskStatus, TaskType, Priority, Explicit<T>
    crud.rs             — create_task, get_task, update_task, find_by_prefix
    lifecycle.rs        — close_task, reopen_task, delete_task, labels, commits
    queries.rs          — list_tasks with filter builder, list_ready/blocked (recursive CTE)
    search.rs           — FTS5 search, BM25 weights (10,5,2,1,2)
    ordering.rs         — hierarchical ordering, topo-sort
    path_cache.rs       — dotted path computation, cascade updates
    aggregates.rs       — count_tasks, count_by_status, count_blocked, count_closed_since
    id.rs               — UUID generation, resolve_task_reference (#N, prefix, UUID)
  memories.rs           — Memory model, UUID5 dedup, crossrefs, graph flags, project scoping
  task_dependencies.rs  — DFS cycle detection, bidirectional traversal, dep types
```

### Implementation Order

1. `database.rs` + `schema.rs` + `error.rs` — foundation
2. `config_store.rs` — simplest (14 methods, flat KV), validates DB layer works
3. `task_dependencies.rs` — small (12 methods), needed by tasks
4. `tasks/` — largest, depends on above. Start with models -> crud -> lifecycle -> queries -> search -> ordering -> path_cache -> aggregates
5. `memories.rs` — independent of tasks, can parallelize with step 4

### Key Implementation Details

- **seq_num allocation:** Must use `transaction_immediate` (BEGIN IMMEDIATE) to prevent TOCTOU race on MAX(seq_num). Python does this at `_crud.py:64-70`. Note: BEGIN IMMEDIATE acquires a RESERVED lock immediately, preventing concurrent writers from interleaving but allowing readers. Contention is low (single-user daemon, task creation is infrequent). Configure `busy_timeout` (5000ms matches Python default) and use a simple retry loop (3 attempts, 100ms backoff) for `SQLITE_BUSY` — this matches the Python behavior where SQLite's internal busy handler retries transparently.
- **FTS5 triggers:** Created by baseline schema SQL. Integration tests must verify triggers fire on INSERT/UPDATE/DELETE.
- **Path cache:** Parent chain traversal with depth limit (100). Cascade updates on reparenting affect multiple rows.
- **Cycle detection:** Iterative DFS with explicit stack and visited set. `would_create_cycle()` checks before insert. Note: the Python implementation (`task_dependencies.py:114-138`) is already iterative — port it directly, do not introduce recursion.
- **JSON columns:** labels, commits, tags stored as JSON strings. Use serde_json for ser/deser.
- **Task model from_row:** Handle optional columns gracefully (migration-safe column reading).

### Python Source Files

| File | LOC | Methods |
|------|-----|---------|
| `src/gobby/storage/database.py` | ~300 | 9 (DatabaseProtocol) |
| `src/gobby/storage/config_store.py` | 231 | 14 |
| `src/gobby/storage/tasks/` (11 files) | 2,811 | 32 |
| `src/gobby/storage/memories.py` | 450+ | 27 |
| `src/gobby/storage/task_dependencies.py` | 49 | 12 |
| `src/gobby/storage/baseline_schema.sql` | 1,131 | 68 tables, 160 indexes |

### Verification

- Integration tests against real SQLite (tempfile DBs, not mocks)
- FTS5 search returns ranked results with correct BM25 scoring
- `transaction_immediate` serializes concurrent seq_num allocation (multi-tokio-task test)
- Cycle detection finds cycles, rejects them
- Path cache computes correct dotted paths
- All ConfigStore methods covered
- `cargo test -p gobby-storage`

---

## Crate 3: `gobby-rules` — Standalone Rule Engine

**Goal:** Port the rule engine with trait-based DI. ~2,000 LOC new.

**Location:** `crates/gobby-rules/`

### Key Decisions

- **minijinja** for template rendering (not tera — closer to Jinja2 syntax, supports `trim_blocks`/`lstrip_blocks`, designed for embedding)
- **Custom expression parser** for SafeExpressionEvaluator — Pratt/precedence-climbing parser is the primary approach (~300-400 LOC). Consider prototyping with **winnow** (`combinator::expression`) first; if its error recovery and list-comprehension support are adequate, prefer it over hand-rolling. Keep Pratt as fallback if the combinator approach fights the grammar (especially YAML normalization and dunder-blocking). Actual LOC will be higher than the base parser: budget ~150 LOC for error recovery/source locations, ~100 for list-comprehension parsing, ~50 for YAML whitespace normalization, ~50 for method whitelist/dunder checks
- **`Box<dyn Trait>`** for dependency injection (not generics — the engine is called per-event not per-token, dynamic dispatch simplifies the API enormously)

### Module Layout

```text
src/
  lib.rs
  error.rs              — RuleError enum
  types.rs              — RuleEvent, RuleEffect, RuleDefinitionBody, HookEvent, HookResponse
  engine.rs             — RuleEngine::evaluate() — main loop
  effects.rs            — apply_effect, should_block, apply_set_variable, coerce_rendered_value
  enforcement.rs        — agent/step tool enforcement, MCP wildcard matching, discovery bypass
  evaluator/
    mod.rs
    ast.rs              — Expr enum (BoolOp, Compare, UnaryOp, BinOp, Name, Const, Call, Attr, Subscript, List, Dict, IfExpr, ListComp)
    parser.rs           — Pratt parser: expression string -> Expr tree
    interpreter.rs      — evaluate Expr against context + allowed_funcs
  template.rs           — minijinja wrapper implementing TemplateRenderer trait
  condition_helpers.rs  — Built-in functions: task_tree_complete, has_stop_signal, mcp_called, etc.
  traits.rs             — All 8 trait definitions
```

### Trait Definitions

```rust
pub trait RuleStore: Send + Sync {
    fn load_rules_by_event(&self, event: RuleEvent) -> Result<Vec<(RuleRow, RuleDefinitionBody)>>;
    fn load_session_overrides(&self, session_id: &str) -> Result<HashMap<String, bool>>;
}

pub trait VariableStore: Send + Sync {
    fn get_variables(&self, session_id: &str) -> Result<HashMap<String, Value>>;
    fn set_variable(&self, session_id: &str, key: &str, value: Value) -> Result<()>;
}

pub trait TemplateRenderer: Send + Sync {
    fn render(&self, template: &str, context: &HashMap<String, Value>) -> Result<String>;
}

pub trait ConditionHelpers: Send + Sync {
    fn task_tree_complete(&self, task_id: &str) -> bool;
    fn has_stop_signal(&self, session_id: &str) -> bool;
    fn mcp_called(&self, server: &str, tool: Option<&str>, variables: &HashMap<String, Value>) -> bool;
    fn mcp_failed(&self, server: &str, tool: &str, variables: &HashMap<String, Value>) -> bool;
}

#[async_trait]
pub trait MCPDispatcher: Send + Sync {
    async fn dispatch(&self, server: &str, tool: &str, args: HashMap<String, Value>) -> Result<Value>;
}

pub trait EnforcementRegistry: Send + Sync {
    fn is_discovery_tool(&self, tool: &str) -> bool;
    fn get_step_for_session(&self, session_id: &str) -> Option<WorkflowStep>;
}

pub trait SkillManager: Send + Sync {
    fn resolve_skill(&self, name: &str) -> Option<SkillContent>;
}

pub trait MetricsStore: Send + Sync {
    fn record_rule_eval(&self, name: &str, session_id: &str, blocked: bool, latency_ms: f64);
}
```

### SafeExpressionEvaluator

The Python version piggybacks on `ast.parse()`. Rust needs a hand-written parser.

**Expression language (subset to support):**
- Literals: strings, numbers, booleans, None, lists, dicts, tuples
- Names: variable lookup from context
- Boolean ops: `and`, `or` (Python semantics — return values, not just bool)
- Unary: `not`, `-`
- Binary: `+`, `-`, `*`, `//`, `%`
- Comparisons: `==`, `!=`, `<`, `>`, `<=`, `>=`, `is`, `is not`, `in`, `not in`
- Attribute access: `obj.attr` (blocks dunder)
- Subscript: `obj[key]`
- Method calls: whitelist only (dict.get/keys/values/items, str.strip/startswith/endswith/lower/upper/split, list.count/index)
- Function calls: allowed_funcs dict (len, bool, str, int, any, all, etc.)
- Ternary: `x if cond else y`
- List comprehensions: `[x for x in items if cond]`
- YAML normalization: collapse whitespace (folded scalars produce newlines)

**Parser approach:** Pratt parser with precedence levels. Small enough to hand-write (~300-400 LOC).

### Implementation Order

1. `types.rs` — port from `definitions.py:57-195`
2. `evaluator/` — pure logic, testable in isolation
3. `traits.rs` — all trait definitions
4. `template.rs` — minijinja wrapper
5. `condition_helpers.rs` — built-in functions
6. `effects.rs` — effect application
7. `enforcement.rs` — tool enforcement
8. `engine.rs` — main evaluate loop

### Python Source Files

| File | LOC | Purpose |
|------|-----|---------|
| `src/gobby/workflows/engine/core.py` | 597 | Main evaluation loop |
| `src/gobby/workflows/engine/effects.py` | 317 | Effect handling |
| `src/gobby/workflows/engine/enforcement.py` | 338 | Tool access control |
| `src/gobby/workflows/engine/templating.py` | 196 | Template rendering + context |
| `src/gobby/workflows/safe_evaluator.py` | 446 | AST-based expression evaluator |
| `src/gobby/workflows/definitions.py` | 560 | Definition models |

### Verification

- SafeExpressionEvaluator: port Python test cases, verify identical results
- Parser handles YAML-folded whitespace (normalize before parse)
- Block effects deferred until after non-block sibling effects
- Conditions fail closed for block effects, fail open for others
- Discovery tools always pass enforcement
- MCP wildcard matching works (e.g., `gobby-tasks:*`)
- `cargo test -p gobby-rules` with mock trait implementations
- Integration tests with gobby-storage RuleStore impl

---

## Crate 4: `gobby-hook` — Hook Dispatcher Binary

**Goal:** Replace `hook_dispatcher.py` (~800 LOC) with a compiled Rust binary. Eliminates Python cold-start on every hook event.

**Location:** `crates/gobby-hook/`

### Why This Matters

`hook_dispatcher.py` runs on *every* hook event from Claude Code, Gemini CLI, and Codex. It's a standalone script in `~/.gobby/hooks/` invoked as a subprocess. Python cold-start + dependency import (httpx, pyyaml, aiofiles) adds latency to every tool call. A compiled binary starts in <1ms.

### What It Does

1. Parse CLI args (`--type`, `--cli`, `--debug`)
2. Read JSON from stdin
3. Check if hooks are disabled (env var or `.gobby/project.json`)
4. Resolve daemon URL from `~/.gobby/bootstrap.yaml`
5. Health check daemon (`GET /api/admin/health`)
6. Inject terminal context for session-start hooks (env vars: TMUX_PANE, TERM_SESSION_ID, VSCODE_IPC_HOOK_CLI, etc.)
7. POST to `/api/hooks/execute` with project/session headers
8. Parse response, detect block/deny, exit with correct code (0=allow, 2=block)
9. Fire-and-forget for session-end hooks (detached process survives parent death)
10. Agent failure tracking (consecutive daemon-down -> force-kill agent after 5 failures)

### Key Design

- Depends on `gobby-core` only (bootstrap, daemon client, project detection)
- **ureq** for HTTP (blocking is fine — short-lived CLI process, not a server)
- **serde_json** for stdin/stdout JSON
- **Fire-and-forget (non-session-end):** Spawn a thread that calls `ureq::post(...).send_json(...)` and **wait for it before main exits**. A background thread cannot outlive process termination, so the main thread must `JoinHandle::join()` (optionally with a bounded timeout via a channel + `recv_timeout`) so the request actually completes. "Fire-and-forget" here means "the dispatcher does not block the parent CLI, and we don't care about the response body" — not "the request can outlive main".
- **Fire-and-forget (session-end):** Session-end hooks must deliver their payload even if the parent process exits immediately. Use a daemonization sequence: `fork()` → parent exits immediately (returning control to the CLI) → child calls `libc::setsid()` to detach from the controlling terminal → child performs the `ureq::post(...).send_json(...)` in-process and exits. `exec()` is **not** required — the child reuses the existing binary's code. This is Unix-only (macOS, Linux) — document it separately from the thread-based path above.
- Agent kill: `libc::killpg()` / tmux kill-pane via `Command`
- Failure tracking: counter files in `std::env::temp_dir()/gobby-agent-failures/`. Cleanup policy: the hook binary itself prunes files older than 24h on each invocation (cheap `metadata().modified()` check). Cap at 100 files with LRU removal. No external cleanup daemon needed — the hook runs frequently enough to self-maintain.

**Platform scope:** gobby-hook targets Unix (macOS, Linux) only. `pre_exec`, `libc::setsid`, and `libc::killpg` are Unix-specific. Windows support is not planned for Phase 1. If needed later: replace `pre_exec` with `CREATE_NEW_PROCESS_GROUP` creation flag, replace `killpg` with `TerminateProcess` on job objects, and use `std::env::temp_dir()` (already portable) for failure tracking.

### CLI Config Registry

Per-CLI behavioral differences:

| CLI | Critical Hooks | JSON Error Exit | Session Start Hooks |
|-----|---------------|-----------------|---------------------|
| claude | session-start, session-end, pre-compact | 2 | session-start |
| gemini | SessionStart | 1 | SessionStart |
| codex | SessionStart, Stop | 2 | SessionStart |

### Module Layout

```text
src/
  main.rs             — arg parsing, main dispatch loop
  cli_config.rs       — CLIConfig struct, CLI_CONFIGS registry
  terminal_context.rs — env var capture (tmux, vscode, iterm, kitty, etc.)
  response.rs         — is_blocked(), extract_reason()
  agent_safety.rs     — failure tracking, force_kill_agent()
```

### Installation

`gobby install` copies the binary to `~/.gobby/hooks/hook_dispatcher` (no `.py` extension). Hook templates in settings.json / gemini_cli_config.json updated to point at the binary instead of `python hook_dispatcher.py`.

### Verification

- Binary starts in <5ms (benchmark vs Python ~200-400ms cold start)
- All hook types route correctly for claude/gemini/codex
- Block responses produce exit code 2 with reason on stderr
- Fire-and-forget hooks (session-end) deliver payload after parent exits
- Agent failure tracking increments/resets/kills correctly
- Terminal context captures all env vars matching Python output
- `cargo test -p gobby-hook`

### Python Source

| File | LOC |
|------|-----|
| `src/gobby/install/shared/hooks/hook_dispatcher.py` | 803 |

---

## What's NOT in This Plan

- **gobby-daemon (HTTP shell)** — comes after these crates are solid
- **Session storage** — deferred; 42 methods, most edge cases, not needed until HTTP endpoints
- **CLI commands** — HTTP wrappers, port last or never
- **LLM service** — thin API wrapper, no perf benefit from Rust

---

## Workspace Changes

```toml
# gobby-cli/Cargo.toml
[workspace]
members = [
    "crates/gobby-core",
    "crates/gobby-storage",
    "crates/gobby-rules",
    "crates/gobby-hook",
    "crates/gcode",
    "crates/gsqz",
]
resolver = "3"
```

## Key Dependencies (new crates)

| Crate | Key Deps |
|-------|----------|
| gobby-core | thiserror, serde, serde_yaml, rusqlite (feature), fernet/pbkdf2 (feature), ureq (feature), uuid |
| gobby-storage | gobby-core, rusqlite, deadpool-sqlite, tokio, serde_json, thiserror, uuid |
| gobby-rules | gobby-core, minijinja, serde_json, tokio, thiserror, async-trait |
| gobby-hook | gobby-core (daemon, bootstrap, project), serde_json, clap, libc |

---

## Operational Concerns

### Migration Phasing

Rust crates integrate with the Python daemon via HTTP — no FFI/pyo3. The daemon remains the orchestrator; Rust crates are either:
- **Libraries** consumed by Rust binaries (gobby-hook) or Rust CLIs (gcode, gsqz), or
- **Standalone binaries** that talk to the daemon over its existing HTTP API.

The rule engine (`gobby-rules`) is consumed as a library by `gobby-hook` and eventually by a Rust HTTP shell (`gobby-daemon`, not in this phase). During transition, the Python rule engine and Rust rule engine coexist — the Python daemon uses its own, gobby-hook uses the Rust one. Parity is verified by running the same test vectors against both.

The storage layer (`gobby-storage`) reads/writes the same SQLite DB as the Python daemon. Schema is shared (embedded baseline). No migration needed — both sides see the same tables. Concurrent access is safe via WAL mode + busy_timeout.

### Rollback

Each crate ships independently. Rollback = revert to previous binary:
- **gobby-hook:** `gobby install` re-copies the Python `hook_dispatcher.py` if the Rust binary is removed or renamed. Feature flag: `GOBBY_HOOK_PYTHON=1` env var forces the Python dispatcher.
- **gobby-storage / gobby-rules:** Library crates, no separate deployment. Rollback = revert the consuming binary.
- **Schema:** No new migrations in this phase — Rust embeds the existing baseline. No backwards-compatibility risk.

### Success Criteria

| Metric | Target |
|--------|--------|
| gobby-hook cold start | <5ms (vs Python ~200-400ms) |
| Rule engine evaluate() P95 | <2ms for typical 10-rule session |
| gobby-storage task CRUD P95 | <1ms per operation |
| Test parity | All Python test vectors pass against Rust implementations |
| `cargo clippy --workspace` | Zero warnings |
| Coverage | >80% per crate |

### Observability

- Use the `tracing` crate with `tracing-subscriber` (JSON output, `RUST_LOG` env filter).
- gobby-hook logs to stderr (captured by the daemon's hook executor). Structured JSON lines match the Python dispatcher's log format so existing log parsers work unchanged.
- gobby-storage and gobby-rules are libraries — they use `tracing` spans/events, the consumer configures the subscriber.
- No separate metrics exporter in Phase 1. The daemon's existing `/api/admin/metrics` endpoint covers rule eval timing via the Python path; Rust metrics integrate when `gobby-daemon` ships.
