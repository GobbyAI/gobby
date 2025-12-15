# Local-First Gobby Client

## Goal

Remove all gobby_server dependencies from gobby_client. The daemon should work standalone with:

- Local session tracking (SQLite)
- MCP proxy with lazy tool acquisition (no remote sync)
- Hooks (already local)
- Session handoff/pickup via MCP tools

## Migration Strategy: Hybrid Approach

Create `src/gobby/` fresh, then selectively port modules based on their coupling to platform code.

### Hard Constraints

1. **No imports from `gobby_client`** - The new `gobby` package must be fully standalone
2. **No imports from `gobby_server`** - No platform server dependencies
3. **No references to platform URLs** - No hardcoded or configurable platform endpoints
4. **All imports must be `from gobby.*`** - Internal imports use the new package name
5. **No success logging** - Only log errors, warnings, and debug info. No `logger.info("X succeeded")` or similar. If it worked, stay silent.

| Strategy | Modules | ~LOC | Rationale |
|----------|---------|------|-----------|
| **Copy as-is** | `utils/`, `llm/`, `adapters/`, `hooks/`, `sessions/transcripts/`, `codex/`, `install/` | 12k | Clean, isolated, no platform coupling |
| **Build fresh** | `storage/` (new), handoff/pickup tools | 1k | New local-first code |
| **Refactor while porting** | `mcp/`, `servers/`, `cli.py`, `config/`, `sessions/manager.py`, `auth/` | 8k | Platform coupling requires surgery |
| **Don't copy** | `services/`, `tools/sync.py` | 1k | Dead by design |

## Module Disposition

### Copy As-Is (No Changes)

| Source | Destination | Notes |
|--------|-------------|-------|
| `gobby_client/utils/` | `gobby/utils/` | All utilities except `daemon_client.py` |
| `gobby_client/llm/` | `gobby/llm/` | All LLM providers |
| `gobby_client/adapters/` | `gobby/adapters/` | All CLI adapters |
| `gobby_client/hooks/` | `gobby/hooks/` | Hook event system |
| `gobby_client/sessions/transcripts/` | `gobby/sessions/transcripts/` | Transcript parsers |
| `gobby_client/codex/` | `gobby/codex/` | Codex integration |
| `gobby_client/mcp/stdio.py` | `gobby/mcp/stdio.py` | Stdio transport |
| `gobby_client/runner.py` | `gobby/runner.py` | Async runner |

### Build Fresh

| File | Purpose |
|------|---------|
| `gobby/storage/__init__.py` | Package exports |
| `gobby/storage/database.py` | SQLite connection manager |
| `gobby/storage/migrations.py` | Schema versioning |
| `gobby/storage/sessions.py` | `LocalSessionManager` |
| `gobby/storage/projects.py` | `LocalProjectManager` |
| `gobby/storage/mcp.py` | `LocalMCPManager` |

### Refactor While Porting

| Source | Changes Required |
|--------|------------------|
| `gobby_client/servers/http.py` | Remove platform proxy endpoints, swap to local storage |
| `gobby_client/mcp/manager.py` | Config from `LocalMCPManager` instead of platform |
| `gobby_client/mcp/server.py` | Add handoff/pickup tools, use local storage |
| `gobby_client/mcp/actions.py` | Use local storage for MCP server CRUD |
| `gobby_client/sessions/manager.py` | Swap platform calls → `LocalSessionManager` |
| `gobby_client/sessions/summary.py` | Minor: update file paths |
| `gobby_client/config/app.py` | Remove platform URL requirements |
| `gobby_client/config/mcp.py` | Keep but simplify |
| `gobby_client/auth/service.py` | Gut platform OAuth, keep keyring shell |
| `gobby_client/cli.py` | Remove platform commands, update daemon init |

### Do Not Copy (Dead Code)

| File | Reason |
|------|--------|
| `gobby_client/services/sessions_service.py` | Platform session proxy |
| `gobby_client/services/projects_service.py` | Platform project registration |
| `gobby_client/tools/sync.py` | Remote tool sync |
| `gobby_client/utils/daemon_client.py` | HTTP client for platform (may need local version) |

---

## SQLite Schema

```sql
CREATE TABLE projects (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    repo_path TEXT,
    github_url TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE sessions (
    id TEXT PRIMARY KEY,
    cli_key TEXT NOT NULL,
    machine_id TEXT NOT NULL,
    source TEXT NOT NULL,
    project_id TEXT REFERENCES projects(id),
    title TEXT,
    status TEXT DEFAULT 'active',
    jsonl_path TEXT,
    summary_path TEXT,
    cwd TEXT,
    git_branch TEXT,
    parent_session_id TEXT REFERENCES sessions(id),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE mcp_servers (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    transport TEXT NOT NULL,
    url TEXT,
    command TEXT,
    args TEXT,
    env TEXT,
    headers TEXT,
    enabled INTEGER DEFAULT 1,
    description TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE tools (
    id TEXT PRIMARY KEY,
    mcp_server_id TEXT NOT NULL REFERENCES mcp_servers(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    description TEXT,
    input_schema TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(mcp_server_id, name)
);
```

## File Structure

```text
~/.gobby/
├── config.yaml          # Global config (human-editable, keep as-is)
├── gobby.db             # SQLite database
└── projects/
    └── {project_name}/
        └── summaries/
            └── {session_id}.md
```

---

## Implementation Checklist

### Phase 0: Scaffold New Package

- [x] 0.1 Create `src/gobby/__init__.py` with version and exports
- [x] 0.2 Create `src/gobby/py.typed` marker file
- [x] 0.3 Update `pyproject.toml` to include `gobby` package
- [x] 0.4 Add `gobby` CLI entry point in pyproject.toml

### Phase 1: Copy Clean Modules (No Changes)

- [x] 1.1 Copy `gobby_client/utils/` → `gobby/utils/` (exclude `daemon_client.py`)
- [x] 1.2 Copy `gobby_client/llm/` → `gobby/llm/`
- [x] 1.3 Copy `gobby_client/adapters/` → `gobby/adapters/`
- [x] 1.4 Copy `gobby_client/hooks/` → `gobby/hooks/`
- [x] 1.5 Copy `gobby_client/sessions/transcripts/` → `gobby/sessions/transcripts/`
- [x] 1.6 Copy `gobby_client/codex/` → `gobby/codex/`
- [x] 1.7 Copy `gobby_client/mcp/stdio.py` → `gobby/mcp/stdio.py`
- [x] 1.8 Copy `gobby_client/runner.py` → `gobby/runner.py` (rewrote minimal, no auth)
- [x] 1.9 Update all import paths from `gobby_client` → `gobby` in copied files
- [x] 1.10 Verify no `gobby_client` or `gobby_server` imports remain:
  - [x] 1.10.1 Run `grep -r "from gobby_client" src/gobby/` (expect 0 results)
  - [x] 1.10.2 Run `grep -r "import gobby_client" src/gobby/` (expect 0 results)
  - [x] 1.10.3 Run `grep -r "from gobby_server" src/gobby/` (expect 0 results)
  - [x] 1.10.4 Run `grep -r "import gobby_server" src/gobby/` (expect 0 results)
- [x] 1.11 Audit and remove success logging in copied modules:
  - [x] 1.11.1 Find all `logger.info` calls: `grep -rn "logger.info" src/gobby/`
  - [x] 1.11.2 Remove success messages (e.g., "Connected successfully", "Loaded X", "Completed Y")
  - [x] 1.11.3 Keep only: startup info, config loaded, version info (minimal)
  - [x] 1.11.4 Convert necessary info logs to `logger.debug`
- [ ] 1.12 Verify copied modules import successfully (deferred - needs later phases)

### Phase 2: Build Storage Layer

- [x] 2.1 Create `gobby/storage/__init__.py`
- [x] 2.2 Create `gobby/storage/database.py`
  - [x] 2.2.1 `LocalDatabase` class with connection pooling
  - [x] 2.2.2 Context manager for transactions
  - [x] 2.2.3 Auto-create `~/.gobby/gobby.db` on first access
- [x] 2.3 Create `gobby/storage/migrations.py`
  - [x] 2.3.1 Schema version tracking table
  - [x] 2.3.2 Migration 001: Create projects table
  - [x] 2.3.3 Migration 002: Create sessions table
  - [x] 2.3.4 Migration 003: Create mcp_servers table
  - [x] 2.3.5 Migration 004: Create tools table
  - [x] 2.3.6 `run_migrations()` function
- [x] 2.4 Create `gobby/storage/projects.py`
  - [x] 2.4.1 `LocalProjectManager` class
  - [x] 2.4.2 `create(name, repo_path, github_url)` → Project
  - [x] 2.4.3 `get(id)` → Project | None
  - [x] 2.4.4 `get_by_name(name)` → Project | None
  - [x] 2.4.5 `list()` → list[Project]
  - [x] 2.4.6 `update(id, **fields)` → Project
  - [x] 2.4.7 `delete(id)` → bool
- [x] 2.5 Create `gobby/storage/sessions.py`
  - [x] 2.5.1 `LocalSessionManager` class
  - [x] 2.5.2 `register(cli_key, machine_id, source, ...)` → Session
  - [x] 2.5.3 `get(id)` → Session | None
  - [x] 2.5.4 `find_current(cli_key, machine_id, source)` → Session | None
  - [x] 2.5.5 `find_parent(cwd, source)` → Session | None
  - [x] 2.5.6 `update_status(id, status)` → Session
  - [x] 2.5.7 `update_summary(id, summary_path)` → Session
  - [x] 2.5.8 `list(project_id?, status?)` → list[Session]
- [x] 2.6 Create `gobby/storage/mcp.py`
  - [x] 2.6.1 `LocalMCPManager` class
  - [x] 2.6.2 `add_server(name, transport, url?, command?, ...)` → MCPServer
  - [x] 2.6.3 `get_server(name)` → MCPServer | None
  - [x] 2.6.4 `list_servers(enabled_only=True)` → list[MCPServer]
  - [x] 2.6.5 `update_server(name, **fields)` → MCPServer
  - [x] 2.6.6 `remove_server(name)` → bool
  - [x] 2.6.7 `cache_tools(server_name, tools: list)` → None
  - [x] 2.6.8 `get_cached_tools(server_name)` → list[Tool]
  - [x] 2.6.9 `import_from_mcp_json(path)` → int (count imported)
- [ ] 2.7 Write tests for storage layer (deferred)

### Phase 3: Port Config Module

- [x] 3.1 Create `gobby/config/` fresh (renamed ClientConfig → DaemonConfig)
- [x] 3.2 Edit `gobby/config/app.py`
  - [x] 3.2.1 Remove `platform_url` from config
  - [x] 3.2.2 Remove `platform_api_key` from config
  - [x] 3.2.3 Remove platform-related validation
  - [x] 3.2.4 Add `database_path` config (default: `~/.gobby/gobby.db`)
- [x] 3.3 Skip `config/mcp.py` - obsolete (LocalMCPManager in storage/ replaces it)
- [x] 3.4 All imports use `gobby` package
- [x] 3.5 Verified config loads without platform settings

### Phase 4: Auth Module - DELETED

- [x] 4.1 **Deleted entire auth module** - not needed for local-first
- [x] 4.2 Removed `get_user_id()` usage from hook_manager.py
- [x] 4.3 Removed `SupabaseAuthManager` usage from mcp/stdio.py
- [x] 4.4 Rewrote `init_project` tool to use LocalProjectManager instead of platform API
- [x] 4.5 Sessions use `machine_id` for identity (no user_id needed)

### Phase 5: Port Sessions Module

- [x] 5.1 Create `gobby/sessions/__init__.py`
- [x] 5.2 Copy `gobby/sessions/transcripts/` (already done in Phase 1)
- [x] 5.3 Port `gobby/sessions/summary.py`
  - [x] 5.3.1 Rewrote for local-first (uses LocalSessionManager)
  - [x] 5.3.2 Uses default summary path `~/.gobby/session_summaries`
  - [x] 5.3.3 Updated imports to gobby
- [x] 5.4 Port `gobby/sessions/manager.py`
  - [x] 5.4.1 Rewrote for local-first (uses LocalSessionManager)
  - [x] 5.4.2 Removed all HTTP/platform API calls
  - [x] 5.4.3 Injected `LocalSessionManager` as `session_storage`
  - [x] 5.4.4 Direct storage calls instead of HTTP
  - [x] 5.4.5 Direct storage calls instead of HTTP
  - [x] 5.4.6 Direct storage calls instead of HTTP
  - [x] 5.4.7 Updated imports to gobby
- [x] 5.5 Fixed ClientConfig -> DaemonConfig rename across llm/ modules
- [ ] 5.6 Write tests for session manager (deferred)

### Phase 6: Port MCP Module

- [x] 6.1 Create `gobby/mcp/__init__.py`
- [x] 6.2 Copy `gobby/mcp/stdio.py` (already done in Phase 1)
- [x] 6.3 Port `gobby/mcp/manager.py`
  - [x] 6.3.1 Copied from gobby_client, updated imports
  - [ ] 6.3.2 LocalMCPManager integration (deferred - .mcp.json still works)
  - [ ] 6.3.3 Load server configs from `LocalMCPManager.list_servers()`
  - [ ] 6.3.4 Cache tools on connection: `LocalMCPManager.cache_tools()`
  - [x] 6.3.5 No platform sync calls (uses config_manager pattern)
  - [x] 6.3.6 Updated imports
- [x] 6.4 Port `gobby/mcp/actions.py`
  - [x] 6.4.1 Rewrote for local-first (removed platform sync)
  - [x] 6.4.2 `add_mcp_server` uses mcp_manager.add_server()
  - [x] 6.4.3 `remove_mcp_server` uses mcp_manager.remove_server()
  - [x] 6.4.4 Added `list_mcp_servers` function
  - [x] 6.4.5 Updated imports
- [x] 6.4.6 Copied tools/filesystem.py and tools/summarizer.py
- [x] 6.5 Port `gobby/mcp/server.py` (1440 lines, platform deps removed)
  - [x] 6.5.1 Ported from gobby_client with updated imports
  - [x] 6.5.2 Removed auth_manager and platform_base_url parameters
  - [x] 6.5.3 Removed platform auth tools (get_auth_status, refresh_auth_token, sync_mcp_tools)
  - [x] 6.5.4 Simplified status() - removed auth status display
  - [x] 6.5.5 Updated add/remove_mcp_server to use local actions
  - [x] 6.5.6 Removed gobby://auth resource
  - [ ] 6.5.7 Add `handoff()` MCP tool (deferred - needs session storage integration)
  - [ ] 6.5.8 Add `pickup()` MCP tool (deferred - needs session storage integration)
- [ ] 6.6 Write tests for MCP tools (deferred)
- [ ] 6.7 Write tests for handoff/pickup flow (deferred)

### Phase 7: Port HTTP Server

- [x] 7.1 Create `gobby/servers/__init__.py`
- [x] 7.2 Port `gobby/servers/websocket.py`
  - [x] 7.2.1 Ported from gobby_client with updated imports
  - [x] 7.2.2 Made auth_callback optional (local-first: accepts all connections)
- [x] 7.3 Port `gobby/servers/http.py`
  - [x] 7.3.1 Ported from gobby_client with major refactoring
  - [x] 7.3.2 Removed all auth endpoints (/auth/status, /auth/health, /auth/refresh)
  - [x] 7.3.3 Removed auth_manager and platform_base_url parameters
  - [x] 7.3.4 Removed platform proxy functionality (no httpx calls to platform)
  - [x] 7.3.5 Added session_manager: LocalSessionManager parameter
  - [x] 7.3.6 Updated `/sessions/register` to use LocalSessionManager
  - [x] 7.3.7 Updated `/sessions/{id}` to use LocalSessionManager
  - [x] 7.3.8 Updated `/sessions/find_current` to use LocalSessionManager
  - [x] 7.3.9 Updated `/sessions/update_status` to use LocalSessionManager
  - [x] 7.3.10 Updated `/sessions/update_summary` to use LocalSessionManager
  - [x] 7.3.11 Kept `/sessions/find_parent` (uses LocalSessionManager)
  - [x] 7.3.12 Updated imports to gobby package
  - [x] 7.3.13 Removed sessions_service (platform proxy)
- [ ] 7.4 Write tests for HTTP endpoints with local storage (deferred)

### Phase 8: Port CLI

- [x] 8.1 Port `gobby/cli.py`
  - [x] 8.1.1 Rewrote from gobby_client with all platform deps removed
  - [x] 8.1.2 Entry point is `gobby` (configured in pyproject.toml)
  - [x] 8.1.3 Removed `login` command entirely
  - [x] 8.1.4 Removed `logout` command entirely
  - [x] 8.1.5 Removed `sync` command entirely
  - [x] 8.1.6 Update `start` command:
    - [x] 8.1.6.1 Initialize `LocalDatabase` on startup via `_init_local_storage()`
    - [x] 8.1.6.2 Run migrations via `run_migrations()`
    - [x] 8.1.6.3 `.mcp.json` import deferred to Phase 10
    - [x] 8.1.6.4 Storage managers created in runner.py
    - [x] 8.1.6.5 Session manager passed to HTTPServer via runner
  - [x] 8.1.7 Updated `status` command (simplified, no auth info)
  - [x] 8.1.8 Keep `install` commands as-is (install/uninstall)
  - [x] 8.1.9 All imports updated to gobby package
- [x] 8.2 Update `gobby/runner.py`
  - [x] 8.2.1 Initializes LocalDatabase and LocalSessionManager
  - [x] 8.2.2 Passes session_manager to HTTPServer
  - [x] 8.2.3 All imports use gobby package
- [x] 8.3 Added `gobby/config/mcp.py` (MCPConfigManager) - was missing
- [ ] 8.4 Write tests for CLI commands (deferred)

### Phase 9: Port Install Scripts

- [x] 9.1 Copy `gobby_client/install/` → `gobby/install/`
- [x] 9.2 Update hook scripts
  - [x] 9.2.1 Update `claude/hooks/` to reference `gobby` package
    - [x] Renamed `check_client_running` → `check_daemon_running`
    - [x] Updated status messages from "gobby_client" → "gobby daemon"
  - [x] 9.2.2 Update `gemini/hooks/` to reference `gobby` package
    - [x] Same changes as claude hooks
  - [x] 9.2.3 Update `codex/` (no changes needed - already used generic names)
- [x] 9.3 Update skill/workflow files
  - [x] 9.3.1 Update `claude/skills/gobby-daemon-mcp/SKILL.md` CLI examples
  - [x] 9.3.2 Update `gemini/workflows/gobby-daemon-mcp.md` references
- [x] 9.4 Update documentation
  - [x] 9.4.1 Update `claude/hooks/README.md` CLI commands and paths
- [x] 9.5 Add codex uninstall support
  - [x] 9.5.1 Added `_uninstall_codex_notify()` function
  - [x] 9.5.2 Added `--codex` flag to uninstall command
- [x] 9.6 Verified install/uninstall commands work

### Phase 10: One-Time Migration Support

- [x] 10.1 SKIPPED - Manual migration preferred
  - User will read `.mcp.json` and call `add_mcp_server` for each server
  - Simpler than auto-import logic
- [x] 10.2 Session history import - not needed (local-first has no prior platform data)

### Phase 11: Final Cleanup

- [x] 11.1 Verify hard constraints (no legacy imports, no success logging):
  - [x] 11.1.1 Run `grep -r "gobby_client" src/gobby/` - ✓ 0 results
  - [x] 11.1.2 Run `grep -r "gobby_server" src/gobby/` - ✓ 0 results
  - [x] 11.1.3 Run `grep -r "platform_url" src/gobby/` - ✓ 0 results
  - [x] 11.1.4 Run `grep -r "platform_api_key" src/gobby/` - ✓ 0 results
  - [ ] 11.1.5 Audit `logger.info` calls (deferred)
- [ ] 11.2 Run full test suite (deferred - needs test updates)
- [x] 11.3 Verified all 20 modules import successfully
- [ ] 11.4 Remove `gobby_client` from pyproject.toml (after full testing)
- [ ] 11.5 Update CLAUDE.md references (after full testing)
- [ ] 11.6 Update README with new package name (after full testing)
- [ ] 11.7 Verify daemon starts and runs without platform
  - [ ] 11.7.1 Start daemon with `gobby start`
  - [ ] 11.7.2 Verify MCP server connects
  - [ ] 11.7.3 Verify hooks work (Claude Code, Gemini, Codex)
  - [ ] 11.7.4 Test handoff/pickup flow (deferred - needs session storage integration)
- [ ] 11.8 Delete `src/gobby_client/` directory (after full testing)

### Phase 12: Post-Migration Refactors (Optional)

- [ ] 12.1 Rename `cli_key` → `external_id` for clarity
  - [ ] 12.1.1 Update schema migration to rename column
  - [ ] 12.1.2 Update `LocalSessionManager` field references
  - [ ] 12.1.3 Update session registration code
  - [ ] 12.1.4 Update any queries referencing `cli_key`
  - [ ] 12.1.5 Update tests

---

## Final Directory Structure

```text
src/gobby/
├── __init__.py
├── py.typed
├── cli.py                    # Daemon lifecycle commands
├── runner.py                 # Async runner
│
├── storage/                  # NEW: Local SQLite storage
│   ├── __init__.py
│   ├── database.py           # Connection manager
│   ├── migrations.py         # Schema versioning
│   ├── sessions.py           # LocalSessionManager
│   ├── projects.py           # LocalProjectManager
│   └── mcp.py                # LocalMCPManager
│
├── adapters/                 # Copied as-is
├── auth/                     # Gutted (keyring only)
├── codex/                    # Copied as-is
├── config/                   # Simplified (no platform)
├── hooks/                    # Copied as-is
├── llm/                      # Copied as-is
├── mcp/                      # Refactored (local storage)
├── servers/                  # Refactored (local storage)
├── sessions/                 # Refactored (local storage)
├── utils/                    # Copied as-is
└── install/                  # Updated paths
```

---

## Risk Mitigation

| Risk | Mitigation |
|------|------------|
| Breaking existing hooks | Phase 1 copies hook system unchanged |
| Import path errors | Phase 1.9 does bulk find/replace |
| Missing edge cases | Phase 11.6 comprehensive integration test |
| Data loss on migration | Phase 10.1 imports existing `.mcp.json` |
| Circular imports | Storage layer has no dependencies on other modules |

## Success Criteria

- [ ] **Zero references to `gobby_client` in `src/gobby/`**
- [ ] **Zero references to `gobby_server` in `src/gobby/`**
- [ ] **No success logging** - `logger.info` only for startup/shutdown, not "X worked"
- [ ] Daemon starts without platform URL configured
- [ ] Daemon starts without auth token
- [ ] Sessions persist in SQLite
- [ ] MCP servers persist in SQLite
- [ ] Tools cached on connection
- [ ] Handoff creates summary file
- [ ] Pickup reads summary and creates child session
- [ ] All existing hooks work (Claude Code, Gemini, Codex)
- [ ] All existing MCP tools work
