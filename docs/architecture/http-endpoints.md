# Gobby HTTP Endpoints

This document lists all HTTP endpoints exposed by the Gobby daemon's HTTP server.

## Base URL

```
http://localhost:8765
```

The port is configurable via `~/.gobby/config.yaml` (default: 8765).

---

## Admin Endpoints

### `GET /admin/status`

Comprehensive status check endpoint.

**Response:**
```json
{
  "status": "healthy",
  "server": {
    "port": 8765,
    "test_mode": false,
    "running": true,
    "uptime_seconds": 3600
  },
  "daemon": { ... },
  "process": {
    "memory_rss_mb": 45.2,
    "memory_vms_mb": 120.5,
    "cpu_percent": 2.5,
    "num_threads": 8
  },
  "background_tasks": {
    "active": 0,
    "total": 10,
    "completed": 10,
    "failed": 0
  },
  "mcp_servers": {
    "context7": {
      "connected": true,
      "status": "connected",
      "health": "healthy",
      "consecutive_failures": 0,
      "last_health_check": "2024-01-01T00:00:00Z",
      "response_time_ms": 50
    }
  },
  "response_time_ms": 5.2
}
```

---

### `GET /admin/metrics`

Prometheus-compatible metrics endpoint.

**Response:** `text/plain; version=0.0.4`

Returns metrics in Prometheus text exposition format including:
- HTTP request counts and durations
- Background task metrics
- Daemon health metrics
- Memory and CPU usage

---

### `GET /admin/config`

Get daemon configuration and version information.

**Response:**
```json
{
  "status": "success",
  "config": {
    "server": {
      "port": 8765,
      "test_mode": false,
      "running": true,
      "version": "1.0.0"
    },
    "features": {
      "session_manager": true,
      "mcp_manager": true
    },
    "endpoints": {
      "mcp": ["/mcp/{server_name}/tools/{tool_name}"],
      "sessions": ["/sessions/register", "/sessions/{id}"],
      "admin": ["/admin/status", "/admin/metrics", "/admin/config", "/admin/shutdown"]
    }
  },
  "response_time_ms": 1.2
}
```

---

### `POST /admin/shutdown`

Graceful daemon shutdown endpoint.

**Response:**
```json
{
  "status": "shutting_down",
  "message": "Graceful shutdown initiated",
  "response_time_ms": 0.5
}
```

**Behavior:**
- Waits for pending background tasks to complete (up to 30s)
- Disconnects all MCP servers
- Shuts down gracefully

---

## Session Endpoints

### `POST /sessions/register`

Register session metadata in local storage.

**Request Body:**
```json
{
  "cli_key": "session-abc123",
  "machine_id": "machine-xyz",
  "jsonl_path": "/path/to/transcript.jsonl",
  "title": "Session Title",
  "source": "Claude Code",
  "parent_session_id": "uuid-of-parent",
  "status": "active",
  "project_id": "project-uuid",
  "project_path": "/path/to/project",
  "git_branch": "main",
  "cwd": "/current/working/dir"
}
```

**Required Fields:** `cli_key`

**Response:**
```json
{
  "status": "registered",
  "cli_key": "session-abc123",
  "id": "generated-uuid",
  "machine_id": "machine-xyz"
}
```

---

### `GET /sessions/{session_id}`

Get session by ID from local storage.

**Path Parameters:**
| Parameter | Description |
|-----------|-------------|
| `session_id` | Session UUID |

**Response:**
```json
{
  "status": "success",
  "session": {
    "id": "session-uuid",
    "cli_key": "session-abc123",
    "machine_id": "machine-xyz",
    "source": "Claude Code",
    "status": "active",
    "cwd": "/path/to/project",
    "title": "Session Title",
    "created_at": "2024-01-01T00:00:00Z"
  },
  "response_time_ms": 2.1
}
```

---

### `POST /sessions/find_current`

Find current active session by composite key.

**Request Body:**
```json
{
  "cli_key": "session-abc123",
  "machine_id": "machine-xyz",
  "source": "Claude Code"
}
```

**Required Fields:** `cli_key`, `machine_id`, `source`

**Response:**
```json
{
  "session": { ... }
}
```

Returns `{ "session": null }` if not found.

---

### `POST /sessions/find_parent`

Find parent session for handoff.

**Request Body:**
```json
{
  "cwd": "/path/to/project",
  "source": "Claude Code"
}
```

**Required Fields:** `cwd`, `source`

**Behavior:** Looks for most recent session in same `cwd` with completed/paused status.

**Response:**
```json
{
  "session": { ... }
}
```

Returns `{ "session": null }` if not found.

---

### `POST /sessions/update_status`

Update session status.

**Request Body:**
```json
{
  "session_id": "session-uuid",
  "status": "completed"
}
```

**Required Fields:** `session_id`, `status`

**Response:**
```json
{
  "session": { ... }
}
```

---

### `POST /sessions/update_summary`

Update session summary path.

**Request Body:**
```json
{
  "session_id": "session-uuid",
  "summary_path": "/path/to/summary.md"
}
```

**Required Fields:** `session_id`, `summary_path`

**Response:**
```json
{
  "session": { ... }
}
```

---

## MCP Proxy Endpoints

### `GET /mcp/{server_name}/tools`

List available tools from an MCP server.

**Path Parameters:**
| Parameter | Description |
|-----------|-------------|
| `server_name` | Name of the MCP server (e.g., "supabase", "context7") |

**Response:**
```json
{
  "status": "success",
  "server": "context7",
  "tools": [
    {
      "name": "get-library-docs",
      "description": "Fetch documentation for a library",
      "inputSchema": { ... }
    }
  ],
  "tool_count": 5,
  "response_time_ms": 150
}
```

---

### `POST /mcp/{server_name}/tools/{tool_name}`

Call a tool on an MCP server.

**Path Parameters:**
| Parameter | Description |
|-----------|-------------|
| `server_name` | Name of the MCP server |
| `tool_name` | Name of the tool to call |

**Request Body:** Tool-specific arguments as JSON object.

**Response:**
```json
{
  "status": "success",
  "result": { ... },
  "response_time_ms": 200
}
```

---

## Hook Endpoints

### `POST /hooks/execute`

Execute CLI hook via adapter pattern.

**Request Body:**
```json
{
  "hook_type": "session-start",
  "input_data": { ... },
  "source": "claude"
}
```

**Supported Sources:** `claude`, `gemini`, `codex`

**Response:**
```json
{
  "continue": true,
  "additionalContext": "Session registered: abc123"
}
```

---

## MCP Server Mount

### `/mcp/*`

The FastMCP server is mounted at `/mcp`. This provides the MCP protocol interface via HTTP transport.

**Usage:**
- Tools are accessible via JSON-RPC at `/mcp/`
- Uses stateless HTTP transport with JSON responses

---

## Health Check

### `GET /health`

Simple health check endpoint (via FastMCP mount).

**Response:** `200 OK` if healthy.

---

## Error Handling

All endpoints return 200 OK to prevent CLI hook failures. Errors are logged and returned with an error status:

```json
{
  "status": "error",
  "message": "Internal error occurred but request acknowledged",
  "error_logged": true
}
```

For endpoints that need to indicate actual errors, HTTP status codes are used:
- `400` - Bad request (missing required fields)
- `404` - Resource not found
- `500` - Internal server error
- `503` - Service unavailable (manager not initialized)
