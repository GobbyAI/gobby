<!-- markdownlint-disable-file MD013 MD024 MD032 MD041 MD060 -->

> Superseded on April 14, 2026 by [Rust Migration Epic](../rust-migration-epic.md).
> Preserved as historical input only. Do not use this file as the active
> migration plan.

# Gobby Rust Rewrite — Strategic Roadmap

## Context

gsqz and gcode are successfully ported to Rust as standalone binaries in the gobby-cli monorepo (~9K LOC). The Python daemon is ~184K LOC across 670 files with 63 PostgreSQL tables. Goal: incrementally replace the Python daemon with a compiled Rust binary. This is a clean rewrite opportunity — shed legacy cruft, optimize, ship a single binary.

The gsqz/gcode experience (standalone crate, clean design, fast iteration) is the template for how this should feel.

---

## Phase 1: `gobby-core` Shared Crate

**What:** Extract duplicated patterns from gcode and gsqz into a shared foundation crate.

**Why now:** Both crates duplicate config resolution, daemon communication, and bootstrap parsing. This foundation is required by everything downstream — the daemon crate, future CLI crate, and existing tools all need it.

**Modules to extract (~500 LOC):**

| Module | From | LOC | Purpose |
|--------|------|-----|---------|
| `bootstrap.rs` | gcode/config.rs, gsqz/config.rs | ~100 | bootstrap.yaml parsing, daemon URL resolution, GOBBY_PORT env |
| `daemon.rs` | gsqz/daemon.rs, gcode/savings.rs | ~80 | ureq HTTP client for daemon API (savings reporting, config fetch) |
| `db.rs` | gcode/db.rs | ~40 | PostgreSQL connection helpers (WAL, foreign keys, busy timeout) |
| `secrets.rs` | gcode/secrets.rs | ~180 | Fernet decryption (machine_id + secret_salt → PBKDF2 → decrypt) |
| `project.rs` | gcode/project.rs | ~80 | Project root detection, project.json reading, UUID5 generation |

**Cargo.toml feature gates:**
- `PostgreSQL` — ruPostgreSQL dependency
- `secrets` — fernet, pbkdf2, sha2, base64
- `daemon` — ureq, serde_json

**Deliverables:**
- [ ] Create `crates/gobby-core/` in gobby-cli monorepo
- [ ] Extract modules, wire up feature gates
- [ ] Update gcode to depend on `gobby-core` (remove duplicated code)
- [ ] Update gsqz to depend on `gobby-core` (remove duplicated code)
- [ ] Tests for each module (bootstrap resolution, secret decryption, project detection)

---

## Phase 2: Storage Layer in `gobby-core`

**What:** Port the full PostgreSQL hub schema and core CRUD operations to Rust.

**Why now:** Storage is the most coupled module (42% of Python files import it). Every other component builds on it. Getting this right in Rust is the foundation for everything.

**Scope:**
- Embed v182 baseline schema directly (no migration history needed for fresh Rust builds)
- Port `HubDatabase` — 10 core methods (execute, fetchone, fetchall, transaction, safe_update, etc.)
- Port storage managers in priority order:

| Manager | Methods | Priority | Rationale |
|---------|---------|----------|-----------|
| `database.rs` | 10 | 1 | Foundation — connection pool, transactions, identifier validation |
| `tasks.rs` | 32 | 1 | Core task CRUD, dependency graph, FTS5 search |
| `sessions.rs` | 42 | 1 | Session lifecycle, parent/child, cost tracking |
| `config_store.rs` | 15 | 1 | Key-value config, secret masking |
| `workflow_definitions.rs` | 21 | 2 | Rule/pipeline/agent definitions, enablement |
| `mcp.rs` | 18 | 2 | Server registry, tool discovery |
| `memories.rs` | 27 | 2 | Memory CRUD, crossrefs, graph processing flags |
| `agents.rs` | 29 | 3 | Agent runs, mode switching |
| `pipelines.rs` | 27 | 3 | Execution state, approval workflows |
| Others | varies | 4 | worktrees, clones, comms, cron, metrics, prompts, skills |

**Key patterns to get right:**
- Thread-safe connection pooling (r2d2 or deadpool-PostgreSQL)
- FTS5 trigger-synced virtual tables (3 tables, 12 triggers)
- JSON column handling (serde_json for metadata blobs)
- Composite unique indexes and soft deletes
- `safe_update` with identifier validation (regex-based SQL injection prevention)

**Deliverables:**
- [ ] `gobby-core::schema` — full v182 baseline as embedded SQL
- [ ] `gobby-core::storage::Database` — connection pool with HubDatabase equivalent
- [ ] Priority 1 managers (tasks, sessions, config_store)
- [ ] Priority 2 managers (workflow_definitions, mcp, memories)
- [ ] Integration tests against real PostgreSQL (not mocks)

---

## Phase 3: `gobby-daemon` Crate — HTTP Shell

**What:** New crate in the monorepo. axum + tokio HTTP/WebSocket server. Starts handling endpoints alongside the Python daemon.

**Stack:**
- `axum` — HTTP routing
- `tokio` — async runtime
- `tower` — middleware (auth, logging, CORS)
- `tokio-tungstenite` — WebSocket
- `gobby-core` — storage layer

**Initial endpoints (lowest-risk, highest-value):**
- `GET /api/health` — health check
- `GET /api/status` — daemon status
- Task CRUD (`/api/tasks/*`) — read-heavy, well-defined
- Session queries (`/api/sessions/*`) — read-heavy
- Config store (`/api/config/*`) — simple key-value

**Deployment strategy:**
- Run on a separate port initially (e.g., 60889)
- Python daemon stays authoritative on 60887
- CLI `DaemonClient` already HTTP-based — can point at either
- Migrate routes one at a time, test in parallel
- Eventually: Rust daemon takes port 60887, Python daemon retired

**Deliverables:**
- [ ] `crates/gobby-daemon/` with axum scaffold
- [ ] Health + status endpoints
- [ ] Task CRUD endpoints (backed by gobby-core storage)
- [ ] Session query endpoints
- [ ] WebSocket broadcast skeleton

---

## Phase 4: Hot Path Migration

**What:** Port the performance-sensitive subsystems that benefit most from Rust.

**MCP Proxy Transport Layer:**
- stdio, HTTP, WebSocket transports — pure async I/O
- Connection pooling (MCPClientManager equivalent)
- Clean boundary: transports don't know about tool semantics
- tokio is purpose-built for this

**Rule Engine:**
- `SafeExpressionEvaluator` — AST-based condition evaluation, pure logic, zero I/O
- Condition helpers (task_tree_complete, mcp_called, etc.) — deterministic functions
- Effect dispatch (block, set_variable, inject_context, mcp_call)
- Runs on every hook event — high call frequency, latency-sensitive

**Hook Event Pipeline:**
- Event receive → rule lookup → condition eval → effect dispatch
- Currently: Python asyncio with multiple await points
- Rust: synchronous rule eval + async effect dispatch

---

## Phase 5: Full Daemon Replacement

**What:** Port remaining subsystems, retire Python daemon.

**Remaining modules (in rough order):**
1. Pipeline executor (step handlers, approval workflow)
2. Agent spawning and lifecycle (process management, worktree isolation)
3. Memory manager (embedding-based recall)
4. Skill loader (filesystem, GitHub, ZIP)
5. Session processor (transcript parsing)
6. LLM service (thin API wrapper — low priority)
7. CLI commands (already HTTP-abstracted — port last or never)

**What to cut during the rewrite:**
- Legacy hook adapters with accumulated workarounds
- ServiceContainer DI pattern (Rust's type system handles this)
- runner_init.py orchestration layer
- Python async complexity (asyncio → tokio is a clean win)
- Unused/deprecated storage managers and tables

---

## What NOT to Port

- **CLI commands** — Already abstracted via HTTP. Works fine as-is. Port last or keep as thin Python/Rust wrapper.
- **LLM service** — Thin wrapper around API calls. No performance benefit from Rust.
- **Install setup** — One-time operation, Python is fine.

---

## Verification

Each phase has its own verification:

- **Phase 1:** gcode and gsqz build and pass tests with gobby-core dependency. No behavior change.
- **Phase 2:** Storage integration tests run against real PostgreSQL. CRUD operations match Python behavior. FTS5 search returns same results.
- **Phase 3:** Rust daemon serves endpoints. Compare responses against Python daemon for same requests. CLI works against both.
- **Phase 4:** Hook evaluation latency measured before/after. Rule engine produces same decisions as Python for test corpus.
- **Phase 5:** Python daemon fully replaced. All existing tests pass against Rust daemon (adapted to Rust test harness).

---

## Critical Files

**Rust (gobby-cli monorepo):**
- `~/Projects/gobby-cli/Cargo.toml` — workspace members
- `~/Projects/gobby-cli/crates/gcode/src/config.rs` — config to extract
- `~/Projects/gobby-cli/crates/gcode/src/db.rs` — PostgreSQL to extract
- `~/Projects/gobby-cli/crates/gcode/src/secrets.rs` — secrets to extract
- `~/Projects/gobby-cli/crates/gcode/src/project.rs` — project detection to extract
- `~/Projects/gobby-cli/crates/gsqz/src/config.rs` — config to extract
- `~/Projects/gobby-cli/crates/gsqz/src/daemon.rs` — daemon client to extract

**Python (schema source of truth):**
- `src/gobby/storage/baseline_schema.sql` — v182 full schema
- `src/gobby/storage/database.py` — HubDatabase (10 methods)
- `src/gobby/storage/sessions.py` — biggest manager (42 methods)
- `src/gobby/storage/tasks/` — task sub-package (32 methods, 2798 LOC)
- `src/gobby/workflows/engine/core.py` — rule engine to port in Phase 4
- `src/gobby/mcp_proxy/manager.py` — MCP client manager to port in Phase 4
