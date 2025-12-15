# MCP Proxy Improvements Plan

## Vision

Transform Gobby's MCP proxy from a simple pass-through layer into an **intelligent tool orchestration system** that learns from usage patterns, optimizes tool discovery, and provides self-healing capabilities when tools fail.

Inspired by:
- [AnyTool](https://github.com/HKUDS/AnyTool) - Smart Tool RAG, quality-aware selection, self-healing

## Current State

Gobby's MCP proxy currently provides:
- **Progressive tool discovery**: `list_tools()` → `get_tool_schema()` → `call_tool()`
- **Tool schema caching**: Schemas stored in `~/.gobby/tools/<server>/<tool>.json`
- **Lightweight metadata**: Brief descriptions in `.mcp.json` for quick listing
- **LLM-based recommendations**: `recommend_tools()` uses LLM to suggest tools for tasks

## Proposed Improvements

### 1. Tool Success Tracking
Track call/success rates per tool to prioritize reliable tools and surface quality metrics.

### 2. Lazy Server Initialization
Defer MCP server connections until first tool call, reducing startup time and resource usage.

### 3. Semantic Tool Search
Pre-computed embeddings enable fast semantic search before falling back to LLM ranking.

### 4. Autonomous Tool Switching (Self-Healing)
When tools fail, automatically suggest alternatives based on similarity and success history.

### 5. Incremental Re-indexing
Detect tool schema changes via hashing and only re-process modified tools.

---

## Architecture

### Enhanced Data Flow

```
┌─────────────────────────────────────────────────────────────────┐
│                        Tool Request                              │
│   list_tools() | get_tool_schema() | call_tool() | recommend()  │
└─────────────────────────┬───────────────────────────────────────┘
                          │
                          ▼
┌─────────────────────────────────────────────────────────────────┐
│                    Enhanced MCP Proxy                            │
│  ┌─────────────────┐  ┌─────────────────┐  ┌────────────────┐  │
│  │  Lazy Connector │  │  Tool Metrics   │  │  Semantic      │  │
│  │  (deferred init)│  │  (SQLite)       │  │  Search        │  │
│  └────────┬────────┘  └────────┬────────┘  └───────┬────────┘  │
│           │                    │                    │           │
│  ┌────────▼────────────────────▼────────────────────▼────────┐  │
│  │              MCPClientManager (enhanced)                   │  │
│  │  - Connection pooling with lazy init                      │  │
│  │  - Tool execution with metrics capture                    │  │
│  │  - Fallback suggestions on failure                        │  │
│  └───────────────────────────┬──────────────────────────────┘  │
└──────────────────────────────┼──────────────────────────────────┘
                               │
                               ▼
┌─────────────────────────────────────────────────────────────────┐
│                   Downstream MCP Servers                         │
│        context7 | supabase | playwright | serena | ...          │
└─────────────────────────────────────────────────────────────────┘
```

### New Components

| Component | Location | Purpose |
|-----------|----------|---------|
| `ToolMetricsManager` | `src/mcp_proxy/metrics.py` | Track tool call/success rates |
| `LazyServerConnector` | `src/mcp_proxy/lazy.py` | Deferred server connections |
| `SemanticToolSearch` | `src/mcp_proxy/semantic.py` | Embeddings-based tool search |
| `ToolFallbackResolver` | `src/mcp_proxy/fallback.py` | Suggest alternatives on failure |
| `SchemaHashManager` | `src/tools/hashing.py` | Track schema changes |

### Storage Schema Extensions

```sql
-- Tool execution metrics
CREATE TABLE tool_metrics (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    server_name TEXT NOT NULL,
    tool_name TEXT NOT NULL,
    call_count INTEGER DEFAULT 0,
    success_count INTEGER DEFAULT 0,
    failure_count INTEGER DEFAULT 0,
    avg_latency_ms REAL,
    last_called_at TIMESTAMP,
    last_error TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(server_name, tool_name)
);

-- Tool embeddings for semantic search
CREATE TABLE tool_embeddings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    server_name TEXT NOT NULL,
    tool_name TEXT NOT NULL,
    description_hash TEXT NOT NULL,  -- Detect changes
    embedding BLOB NOT NULL,          -- Float32 array
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(server_name, tool_name)
);

-- Schema hashes for incremental updates
CREATE TABLE tool_schema_hashes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    server_name TEXT NOT NULL,
    tool_name TEXT NOT NULL,
    schema_hash TEXT NOT NULL,
    last_checked_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(server_name, tool_name)
);
```

---

## Feature Details

### 1. Tool Success Tracking

**Problem**: All tools are treated equally regardless of reliability history.

**Solution**: Track metrics for every tool call and surface this in recommendations.

**Metrics captured**:
- `call_count`: Total invocations
- `success_count`: Successful executions
- `failure_count`: Failed executions (errors, timeouts)
- `avg_latency_ms`: Average response time
- `last_error`: Most recent error message

**Integration points**:
- `call_tool()`: Record metrics after each call
- `list_tools()`: Optionally include success rate
- `recommend_tools()`: Factor reliability into ranking

**Example output**:
```json
{
  "name": "get-library-docs",
  "brief": "Fetch documentation for a library",
  "metrics": {
    "success_rate": 0.94,
    "avg_latency_ms": 1250,
    "call_count": 47
  }
}
```

---

### 2. Lazy Server Initialization

**Problem**: All configured MCP servers connect on daemon startup, even if unused.

**Solution**: Defer connection until first tool call for that server.

**States**:
- `configured`: Server in config but not connected
- `connecting`: Connection in progress
- `connected`: Active connection
- `failed`: Connection failed (with retry logic)

**Behavior**:
- `list_mcp_servers()`: Returns all servers with connection state
- `list_tools(server)`: Triggers connection if needed
- `call_tool(server, tool)`: Triggers connection if needed

**Retry logic**:
- Exponential backoff: 1s, 2s, 4s, 8s, 16s (max)
- Max 3 retries per connection attempt
- Circuit breaker after repeated failures

---

### 3. Semantic Tool Search

**Problem**: `recommend_tools()` uses LLM for every request, which is slow and expensive.

**Solution**: Pre-compute embeddings for tool descriptions; use semantic search as fast first pass.

**Pipeline**:
```
User query
    │
    ▼
[1] Embedding lookup (instant, local)
    │
    ▼
[2] Cosine similarity search (top 20 candidates)
    │
    ▼
[3] LLM ranking (only if needed, on reduced set)
    │
    ▼
Final recommendations (1-3 tools)
```

**Embedding generation**:
- Use small, fast embedding model (e.g., `text-embedding-3-small`)
- Generate on first tool discovery or schema change
- Store in SQLite as BLOB (float32 array)

**Search modes** (configurable):
- `semantic`: Embeddings only (fastest)
- `hybrid`: Embeddings + LLM filter (balanced)
- `llm`: Full LLM ranking (current behavior)

---

### 4. Autonomous Tool Switching (Self-Healing)

**Problem**: When a tool fails, the LLM must figure out alternatives from scratch.

**Solution**: On failure, automatically suggest similar tools based on embeddings and success history.

**Trigger conditions**:
- Tool returns error
- Tool times out
- Tool returns empty/invalid response

**Fallback selection criteria**:
1. Semantic similarity (embedding cosine distance)
2. Same server preference (lower latency)
3. Historical success rate
4. Recency of successful calls

**Response format**:
```json
{
  "success": false,
  "error": "Connection timeout after 30s",
  "fallback_suggestions": [
    {
      "server": "context7",
      "tool": "search-libraries",
      "similarity": 0.87,
      "success_rate": 0.96,
      "reason": "Similar functionality for library lookup"
    }
  ]
}
```

---

### 5. Incremental Re-indexing

**Problem**: Full tool refresh re-processes all tools even if unchanged.

**Solution**: Hash tool schemas and only update changed tools.

**Hash computation**:
- SHA-256 of `name + description + JSON(inputSchema)`
- Stored in `tool_schema_hashes` table

**Refresh behavior**:
```python
async def refresh_server_tools(server_name: str):
    current_tools = await fetch_tools_from_server(server_name)

    for tool in current_tools:
        new_hash = compute_schema_hash(tool)
        old_hash = get_stored_hash(server_name, tool.name)

        if new_hash != old_hash:
            # Tool changed - update schema and embedding
            write_tool_schema(server_name, tool.name, tool)
            regenerate_embedding(server_name, tool.name)
            update_schema_hash(server_name, tool.name, new_hash)
        else:
            # Tool unchanged - skip
            pass
```

**Benefits**:
- Faster refresh cycles
- Reduced embedding generation costs
- Audit trail of tool changes

---

## Implementation Checklist

### Phase 1: Tool Success Tracking

#### Database Setup
- [ ] Create migration for `tool_metrics` table
- [ ] Add indexes on `(server_name, tool_name)` and `last_called_at`

#### Metrics Manager
- [ ] Create `src/mcp_proxy/metrics.py` module
- [ ] Implement `ToolMetricsManager` class
- [ ] Implement `record_call(server, tool, success, latency_ms, error)`
- [ ] Implement `get_metrics(server, tool)` -> single tool metrics
- [ ] Implement `get_server_metrics(server)` -> all tools for server
- [ ] Implement `get_top_tools(limit, min_calls)` -> most reliable tools
- [ ] Implement `get_failing_tools(threshold)` -> tools below success rate

#### Integration
- [ ] Modify `call_tool()` in `manager.py` to record metrics
- [ ] Capture latency using `time.perf_counter()`
- [ ] Capture error messages on failure
- [ ] Add `include_metrics` parameter to `list_tools()`
- [ ] Include metrics in `recommend_tools()` context for LLM

#### MCP Tool Exposure
- [ ] Add `get_tool_metrics(server?, tool?)` MCP tool
- [ ] Add `reset_tool_metrics(server?, tool?)` MCP tool (admin)

#### Tests
- [ ] Unit tests for `ToolMetricsManager`
- [ ] Integration test for metrics capture in `call_tool()`
- [ ] Test metrics persistence across daemon restarts

---

### Phase 2: Lazy Server Initialization

#### Connection State Management
- [ ] Create `src/mcp_proxy/lazy.py` module
- [ ] Define `ServerConnectionState` enum: `configured`, `connecting`, `connected`, `failed`
- [ ] Implement `LazyServerConnector` class
- [ ] Track connection state per server in memory

#### Deferred Connection Logic
- [ ] Modify `MCPClientManager.__init__()` to not connect immediately
- [ ] Implement `ensure_connected(server_name)` async method
- [ ] Call `ensure_connected()` in `list_tools()` when server specified
- [ ] Call `ensure_connected()` in `get_tool_schema()`
- [ ] Call `ensure_connected()` in `call_tool()`

#### Retry and Circuit Breaker
- [ ] Implement exponential backoff for connection retries
- [ ] Implement circuit breaker pattern (fail fast after N failures)
- [ ] Add `connection_timeout` config option
- [ ] Add `max_connection_retries` config option

#### Status Reporting
- [ ] Modify `list_mcp_servers()` to show connection state
- [ ] Add `connected_at` timestamp to server info
- [ ] Add `last_error` to server info for failed connections

#### Background Connection (Optional)
- [ ] Add `preconnect_servers` config option (list of servers to connect eagerly)
- [ ] Implement background connection task for preconnect list

#### Tests
- [ ] Unit tests for `LazyServerConnector`
- [ ] Test connection on first `list_tools()` call
- [ ] Test connection on first `call_tool()` call
- [ ] Test retry logic with mock failing server
- [ ] Test circuit breaker behavior

---

### Phase 3: Semantic Tool Search

#### Embedding Infrastructure
- [ ] Create `src/mcp_proxy/semantic.py` module
- [ ] Create migration for `tool_embeddings` table
- [ ] Choose embedding model (e.g., OpenAI `text-embedding-3-small` or local)
- [ ] Implement `generate_embedding(text)` using LLM provider

#### Embedding Generation
- [ ] Implement `SemanticToolSearch` class
- [ ] Implement `embed_tool(server, tool_name, description)` method
- [ ] Implement `embed_all_tools(server?)` batch method
- [ ] Generate embeddings on tool discovery (in `write_server_tools()`)
- [ ] Store embeddings in SQLite as BLOB

#### Search Implementation
- [ ] Implement `search_tools(query, limit=20)` -> list of (server, tool, score)
- [ ] Implement cosine similarity calculation
- [ ] Support filtering by server
- [ ] Return results sorted by similarity score

#### Integration with recommend_tools()
- [ ] Add `search_mode` parameter: `semantic`, `hybrid`, `llm`
- [ ] In `hybrid` mode: semantic search first, then LLM ranking on top N
- [ ] In `semantic` mode: return semantic results directly
- [ ] Add `search_mode` to config: `recommend_tools.search_mode`

#### CLI/MCP Exposure
- [ ] Add `search_tools(query, mode?, limit?)` MCP tool
- [ ] Add `regenerate_embeddings(server?)` MCP tool (admin)

#### Tests
- [ ] Unit tests for embedding generation
- [ ] Unit tests for cosine similarity search
- [ ] Integration test for `search_tools()` with real embeddings
- [ ] Benchmark: compare `semantic` vs `llm` latency

---

### Phase 4: Autonomous Tool Switching (Self-Healing)

#### Fallback Resolver
- [ ] Create `src/mcp_proxy/fallback.py` module
- [ ] Implement `ToolFallbackResolver` class
- [ ] Implement `find_alternatives(server, tool, error)` method

#### Similarity-Based Suggestions
- [ ] Query embeddings for similar tools (top 5)
- [ ] Exclude tools from same server with poor success rates
- [ ] Rank by: similarity * success_rate * recency_score

#### Integration with call_tool()
- [ ] On tool failure, call `find_alternatives()`
- [ ] Include `fallback_suggestions` in error response
- [ ] Add `include_fallbacks` parameter (default: true)
- [ ] Add `max_fallbacks` config option (default: 3)

#### Automatic Retry (Optional)
- [ ] Add `auto_retry_fallback` config option (default: false)
- [ ] If enabled, automatically try first fallback on failure
- [ ] Return both original error and fallback result

#### MCP Tool Exposure
- [ ] Add `get_tool_alternatives(server, tool)` MCP tool

#### Tests
- [ ] Unit tests for `ToolFallbackResolver`
- [ ] Integration test for fallback suggestions on failure
- [ ] Test similarity ranking with mock embeddings
- [ ] Test success rate filtering

---

### Phase 5: Incremental Re-indexing

#### Schema Hashing
- [ ] Create `src/tools/hashing.py` module
- [ ] Create migration for `tool_schema_hashes` table
- [ ] Implement `compute_schema_hash(tool_data)` using SHA-256
- [ ] Implement `SchemaHashManager` class

#### Hash Storage
- [ ] Implement `get_hash(server, tool)` -> hash or None
- [ ] Implement `set_hash(server, tool, hash)`
- [ ] Implement `get_all_hashes(server)` -> dict of tool -> hash

#### Incremental Refresh
- [ ] Implement `refresh_server_tools_incremental(server)` method
- [ ] Compare new hashes with stored hashes
- [ ] Only write schema and regenerate embedding if changed
- [ ] Track and return: `added`, `updated`, `removed`, `unchanged` counts

#### Integration
- [ ] Use incremental refresh in `add_mcp_server()` after connection
- [ ] Add `force_refresh` parameter to bypass hash check
- [ ] Add `gobby mcp refresh [--force]` CLI command

#### Change Detection Reporting
- [ ] Log tool changes at INFO level
- [ ] Include change summary in `list_mcp_servers()` response
- [ ] Add `last_schema_change` timestamp per tool

#### Tests
- [ ] Unit tests for `compute_schema_hash()`
- [ ] Unit tests for `SchemaHashManager`
- [ ] Integration test for incremental refresh
- [ ] Test hash stability (same input = same hash)

---

### Phase 6: Configuration

#### Config Schema Updates
- [ ] Add `mcp_client_proxy.lazy_connect` (default: true)
- [ ] Add `mcp_client_proxy.preconnect_servers` (default: [])
- [ ] Add `mcp_client_proxy.connection_timeout` (default: 30)
- [ ] Add `mcp_client_proxy.max_connection_retries` (default: 3)
- [ ] Add `recommend_tools.search_mode` (default: "hybrid")
- [ ] Add `recommend_tools.embedding_model` (default: "text-embedding-3-small")
- [ ] Add `recommend_tools.include_metrics` (default: true)
- [ ] Add `recommend_tools.max_fallbacks` (default: 3)

#### Config Validation
- [ ] Validate `search_mode` is one of: semantic, hybrid, llm
- [ ] Validate `embedding_model` is supported
- [ ] Validate timeout and retry values are positive

#### Documentation
- [ ] Update README.md config section
- [ ] Add inline comments in default config

---

### Phase 7: Testing & Documentation

#### Integration Tests
- [ ] End-to-end test: tool call with metrics capture
- [ ] End-to-end test: lazy connection on first call
- [ ] End-to-end test: semantic search with real queries
- [ ] End-to-end test: fallback suggestion on tool failure
- [ ] End-to-end test: incremental refresh with schema changes

#### Performance Benchmarks
- [ ] Benchmark: startup time with lazy vs eager connection
- [ ] Benchmark: `recommend_tools()` latency by search mode
- [ ] Benchmark: incremental vs full refresh time

#### Documentation
- [ ] Update CLAUDE.md with new MCP proxy features
- [ ] Document new MCP tools in README.md
- [ ] Add architecture diagram to this plan
- [ ] Create user guide for tool metrics interpretation

---

## Open Questions

1. **Embedding model choice**: Use cloud API (OpenAI) or local model (sentence-transformers)? Trade-off between quality and offline capability.

2. **Metrics retention**: How long to keep tool metrics? Per-session, per-day, forever?

3. **Fallback UX**: Should fallback suggestions be returned inline or as a separate field? Should auto-retry be default?

4. **Multi-tenant metrics**: If Gobby is used across multiple projects, should metrics be global or per-project?

5. **Embedding dimensions**: What embedding size to use? Smaller = faster search, larger = better quality.

---

## Future Enhancements

- **Tool categorization**: Auto-categorize tools by domain (database, documentation, file system, etc.)
- **Usage analytics**: Dashboard showing most-used tools, failure patterns, latency trends
- **Tool aliases**: Allow users to define aliases for frequently-used tools
- **Tool chaining**: Suggest tool sequences based on common patterns
- **Collaborative filtering**: "Users who called X also called Y"
