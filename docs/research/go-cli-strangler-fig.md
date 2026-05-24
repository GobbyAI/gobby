# Go CLI Migration: Strangler Fig Feasibility Study

**Task:** #8893
**Date:** 2026-02-21
**Status:** Feasibility study complete, ready for implementation

## Executive Summary

Gobby's CLI (Python/Click, ~90 commands, 26 groups) can be incrementally migrated to Go/Cobra using the strangler fig pattern. A Go binary wraps all commands — ported commands run natively, unported commands fall through to a bundled `gobby-py`. This gives users a static binary with fast startup from day one while commands are ported incrementally.

~60 commands are "thin clients" (HTTP calls to the daemon) and trivial to port. ~30 commands have local logic (file I/O, process management) and require real reimplementation. The daemon stays Python — the CLI boundary is clean HTTP.

## Architecture

```
User runs: gobby tasks list --status open
                |
      Go binary (Cobra)
                |
    +---------------------+
    | Ported command?      |
    |                      |
   YES                    NO
    |                      |
 DaemonClient         fallback.RunPython()
 HTTP call to          shells out to
 localhost:60887       gobby-py tasks list --status open
    |                      |
    +----------+-----------+
               |
        Formatted output
```

### Key Components

| Go File | Ports | Python Source |
|---------|-------|-------------|
| `go/internal/client/daemon.go` | DaemonClient (health, status, callAPI, callMCPTool) | `src/gobby/utils/daemon_client.py` (236 LOC) |
| `go/internal/config/bootstrap.go` | Bootstrap YAML loading | `src/gobby/config/bootstrap.py` (93 LOC) |
| `go/internal/fallback/python.go` | Shell out to gobby-py | N/A (new) |
| `go/internal/output/format.go` | Table/JSON output formatting | Various CLI utils |

### Go Dependencies

| Package | Purpose |
|---------|---------|
| `github.com/spf13/cobra` | CLI framework |
| `gopkg.in/yaml.v3` | YAML parsing |
| `github.com/shirou/gopsutil` | Process management (replaces psutil) |
| `net/http`, `encoding/json`, `os/exec` | stdlib — HTTP, JSON, subprocess |

## Command Inventory

### Thin Client (~60 commands) — Native Go from Day 1

These follow one pattern: parse flags, HTTP call, format output.

| Group | Commands | Daemon Endpoint Pattern |
|-------|----------|------------------------|
| `tasks` | list, show, create, update, close, reopen, delete, search, ready, blocked, stats, deps, labels | MCP tool calls |
| `sessions` | list, show, active | MCP tool calls |
| `projects` | list, show, rename, delete | MCP tool calls |
| `workflows` | list, show, status, set, clear, reset, enable, disable, reload, audit, set-variable, get-variable | HTTP + MCP |
| `agents` | list, show, status, stop, spawn | HTTP + MCP |
| `worktrees` | list, show, claim, release, stale, create, delete, spawn, sync, cleanup | HTTP + MCP |
| `clones` | list, show, create, delete, spawn, sync, merge | HTTP + MCP |
| `memory` | create, recall, update, delete, tag | MCP tool calls |
| `skills` | list, show, enable, disable, search, install, remove | MCP tool calls |
| `cron` | list, show, add, remove, enable, disable, trigger | MCP + HTTP |
| `mcp-proxy` | list-servers, list-tools, get-schema, call-tool, add-server, remove-server, recommend-tools | HTTP |
| `pipelines` | list, status, approve, reject | HTTP + MCP |
| `webhooks` | list, add, remove, test | MCP + HTTP |
| `conductor` | start, stop, restart, status, chat | HTTP |
| `github` | status, sync, link-issue, unlink-issue | MCP + HTTP |
| `linear` | status, sync, link-issue, unlink-issue | MCP + HTTP |
| `hooks` | list, test | HTTP |

### Local Logic (~30 commands) — Python Fallback Initially

| Command | Complexity | Why it's hard |
|---------|-----------|---------------|
| `start` | HIGH | Subprocess spawning, PID files, port polling, watchdog, Neo4j Docker |
| `stop` | MEDIUM | SIGTERM/SIGKILL with timeout, PID file cleanup |
| `restart` | LOW | Calls stop + start |
| `status` (enhanced) | MEDIUM | PID check + process alive + HTTP health |
| `install` | VERY HIGH | Multi-CLI config writing, hook installation, DB init, bundled content sync (~1000 LOC) |
| `uninstall` | HIGH | Reverse of install across multiple CLIs |
| `init` | MEDIUM | Project metadata, verification command detection |
| `tasks sync` | LOW | JSONL import/export |
| `tasks compact apply` | LOW | Task compaction with summary |
| `tasks reindex` | LOW | Rebuild search index |
| `sessions export` | LOW | File writing |
| `memory export` | LOW | File writing |
| `sync` | MEDIUM | Bundled content verification + DB sync |
| `export/import` | LOW | File copy operations |
| `merge start/resolve/apply/abort` | MEDIUM | Git operations, conflict resolution |
| `plugins install/remove/list` | LOW | File system scan + copy |
| `pipelines run` | MEDIUM | Daemon HTTP with local fallback executor |
| `agents kill` | LOW | Send signal to process |
| `setup` | KEEP | Node.js Ink wizard — always subprocess |
| `ui start/stop/build/dev` | KEEP | npm process management — always subprocess |
| `mcp-server` | DEFER | stdio MCP server — needs Go MCP SDK |

## Migration Phases

### Phase 1: Scaffold

Go module + Cobra root + DaemonClient + bootstrap config + Python fallback. Everything else depends on this.

```
go/
├── cmd/gobby/
│   ├── main.go
│   └── root.go
├── internal/
│   ├── client/daemon.go
│   ├── config/bootstrap.go
│   ├── fallback/python.go
│   └── output/format.go
├── go.mod
└── go.sum
```

**Done when:** `gobby version` works natively, all other commands fall through to `gobby-py`.

### Phase 2: Thin Clients (depends on Phase 1)

Port ~60 commands. Each is ~30-50 LOC in Go. Highly parallelizable — all 16 command groups are independent, so multiple agents can work on different groups simultaneously.

**Done when:** All thin client commands return identical `--json` output to their Python counterparts.

### Phase 3: Local Logic (depends on Phase 1, independent of Phase 2)

Port ~30 commands with real client-side logic.

- **Batch 1 (no deps):** Daemon lifecycle (`start`/`stop`/`restart`/`status`/`agents kill`)
- **Batch 2 (depends on Batch 1):** File I/O commands (`init`, `sync`, `export/import`, `merge`, `plugins`, `pipelines run`, `tasks sync/compact/reindex`, `sessions export`, `memory export`)
- **Batch 3 (depends on Batch 1+2):** `install`/`uninstall` — largest, reuses utilities from earlier batches
- **Keep as subprocess:** `setup` (Node.js), `ui` (npm)
- **Defer:** `mcp-server` (needs Go MCP SDK)

**Done when:** `gobby-py` fallback is only hit by `setup`, `ui`, and `mcp-server`.

### Phase 4: Cleanup (depends on Phase 2+3)

Remove fallback, remove Python CLI package, update distribution.

**Done when:** Single static Go binary is the sole CLI entry point (except deferred commands).

## Risks

| Risk | Impact | Mitigation |
|------|--------|-----------|
| `gobby-py` not on PATH | Fallback fails | Bundle alongside Go binary, or embed Python wheel |
| Output format drift | Go vs Python output differs | Parity test script comparing `--json` output |
| `install` complexity | ~1000 LOC of file I/O across 6 CLIs | Port last, keep Python fallback longest |
| `mcp-server` (stdio) | Needs Go MCP SDK | Defer indefinitely, keep as Python |

## Verification Strategy

1. **Parity tests:** Script runs every command with `--help` and `--json` on both `gobby` (Go) and `gobby-py` (Python), diffs output
2. **Unit tests:** Each Go command mocks DaemonClient, verifies correct HTTP calls
3. **Integration tests:** Both CLIs run against a live daemon, compare results
4. **Build matrix:** `GOOS=darwin GOARCH=arm64`, `GOOS=darwin GOARCH=amd64`, `GOOS=linux GOARCH=amd64`
5. **Startup benchmark:** Measure cold-start time of Go binary vs Python CLI

## Codebase Context (from exploration)

### Overall Scale
- ~150K LOC across 510 Python files
- 192 files with async code (2,461 async functions) — all in the daemon, not relevant to CLI port
- 34 runtime dependencies — CLI only uses a subset (Click, httpx, rich, psutil, pyyaml, pydantic)

### CLI-Daemon Communication
- Centralized in `DaemonClient` (`src/gobby/utils/daemon_client.py`)
- Uses `httpx` for HTTP calls, no auth (localhost-only)
- Key endpoints: `/admin/health`, `/admin/status`, `/mcp/{server}/tools/{tool}`
- MCP tool calls: `POST /mcp/{server}/tools/{tool}` with JSON body

### CLI Shared Utilities (`src/gobby/cli/utils.py`, ~900 LOC)
- `get_gobby_home()` — returns `~/.gobby`
- `load_full_config_from_db()` — loads DaemonConfig when daemon not running
- `resolve_project_ref()` / `resolve_session_id()` — reference resolution
- `init_local_storage()` — DB migrations
- `is_port_available()` / `wait_for_port_available()` — port checks
- `kill_all_gobby_daemons()` — process discovery and termination
- `stop_daemon()` — graceful shutdown with SIGTERM/SIGKILL fallback

### Bootstrap Config (`~/.gobby/bootstrap.yaml`)
```yaml
database_url: "~/.gobby/PostgreSQL hub"
daemon_port: 60887
bind_host: "localhost"
websocket_port: 60888
ui_port: 60889
```
