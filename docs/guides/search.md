# Search Guide

Gobby has several search surfaces. They share a preference for local-first
results and graceful fallback, but each surface indexes a different kind of
data.

## Quick Start

```bash
# Recall memories
gobby memory recall "authentication patterns" --limit 10

# Search tasks
gobby tasks search "login bug" --type bug --limit 20

# Search installable skills from configured hubs
gobby skills search "git commit" --limit 10
```

```python
# MCP: search memories
call_tool(server_name="gobby-memory", tool_name="search_memories", arguments={
    "query": "authentication patterns",
    "limit": 10,
    "tags_any": ["security"]
})

# MCP: search tasks
call_tool(server_name="gobby-tasks", tool_name="search_tasks", arguments={
    "query": "login bug",
    "current_stage_state": ["ready", "in_progress"],
    "task_type": "bug",
    "limit": 20
})

# MCP: search installed skills
call_tool(server_name="gobby-skills", tool_name="search_skills", arguments={
    "query": "git commit",
    "top_k": 5
})
```

Use progressive MCP discovery before calling a searched tool:

```python
list_mcp_servers()
list_tools(server_name="gobby-tasks")
get_tool_schema(server_name="gobby-tasks", tool_name="search_tasks")
call_tool(server_name="gobby-tasks", tool_name="search_tasks", arguments={
    "query": "login bug"
})
```

## Search Surfaces

| Surface | Tool or command | Index | Best for |
|---------|-----------------|-------|----------|
| Memories | `gobby-memory.search_memories`, `gobby memory recall` | Memory content, tags, project scope | Persistent project facts and preferences |
| Tasks | `gobby-tasks.search_tasks`, `gobby tasks search` | Title, description, labels, task type, category | Finding work by intent or topic |
| Installed skills | `gobby-skills.search_skills` | Skill names, descriptions, metadata | Finding local workflow guidance |
| Skill hubs | `gobby-skills.search_hub`, `gobby skills search` | Configured external skill hubs | Finding installable skills |
| MCP tools | `search_tools`, `recommend_tools` | Registered MCP tools | Tool discovery during agent work |
| Code | `gcode search` | Indexed repository symbols and content | Source navigation |

`gcode search`, `gcode search-symbol`, and `gcode search-content` accept
optional path or glob filters as positional arguments after the query:

```bash
gcode search-content "validation evidence" docs/guides "src/**/*.py"
```

## Unified Search Modes

The shared `UnifiedSearcher` supports four modes:

| Mode | Behavior | Fallback | Use case |
|------|----------|----------|----------|
| `keyword` | pg_search BM25 keyword search only | None | Deterministic PostgreSQL-backed search |
| `embedding` | Embedding search only | Fails if unavailable | Strict semantic search |
| `auto` | Try embeddings, then use pg_search BM25 | Automatic | Default reliability |
| `hybrid` | Combine pg_search BM25 and embedding scores | Continues with keyword search when embeddings fail | Higher quality when embeddings are available |

Fallback in `auto` mode:

```text
1. Probe the embedding endpoint
2. If unavailable, emit a FallbackEvent
3. Fit the pg_search BM25 keyword backend
4. Serve this and later searches through keyword search
```

`hybrid` mode always indexes keyword search first. If embedding indexing or searching
fails, Gobby logs a fallback event and keeps returning keyword results.

## Configuration

Embedding settings are shared by memory, skill search, MCP tool search, and code
indexing:

```yaml
embeddings:
  model: nomic-embed-text
  dim: 768
  api_base: http://localhost:11434/v1  # Ollama or another OpenAI-compatible endpoint
  api_key: null                        # Optional; can use ${ENV_VAR}
```

Unified search behavior is configured separately:

```yaml
search:
  mode: auto              # keyword, embedding, auto, hybrid
  keyword_weight: 0.4     # Used by hybrid mode
  embedding_weight: 0.6   # Used by hybrid mode
  notify_on_fallback: true
```

MCP tool recommendation defaults live under `mcp_client_proxy`:

```yaml
mcp_client_proxy:
  search_mode: llm        # llm, semantic, hybrid
  min_similarity: 0.3
  top_k: 10
```

### Embedding Providers

Gobby uses an OpenAI-compatible embedding client. Common configurations:

| Provider | Model | Required config |
|----------|-------|-----------------|
| Ollama | `nomic-embed-text` | `embeddings.api_base: http://localhost:11434/v1` |
| LM Studio | `nomic-embed-text` | `embeddings.api_base: http://localhost:1234/v1` |
| OpenAI | `text-embedding-3-small` | `OPENAI_API_KEY` or `embeddings.api_key` |

Set `embeddings.dim` to match the model output. The default `nomic-embed-text`
uses `768`; `text-embedding-3-small` uses `1536`.

## Memory Search

Memory search combines the available memory stores:

- Qdrant vector search when embeddings and the vector store are configured
- pg_search BM25 keyword search as local fallback
- FalkorDB graph search when memory graph search is enabled
- Reciprocal Rank Fusion when multiple ranked lists are available

MCP search supports tag filters and a score threshold:

```python
call_tool(server_name="gobby-memory", tool_name="search_memories", arguments={
    "query": "authentication",
    "limit": 10,
    "min_score": 0.6,
    "tags_all": ["security"],
    "tags_any": ["critical", "high"],
    "tags_none": ["obsolete"]
})
```

The MCP tool automatically scopes results to the current project. The CLI also
accepts an explicit project filter:

```bash
gobby memory recall "authentication" --project my-project --tags-any security --limit 10
```

Memory-specific ranking settings:

```yaml
memory:
  temporal_decay_half_life_days: 30.0
  min_recall_score: 0.6
  auto_crossref: false
  crossref_threshold: 0.3
  crossref_max_links: 5
```

## Task Search

Task search is pg_search BM25 over task title, description, labels, task type,
and category. MCP supports more filters than the CLI:

```python
call_tool(server_name="gobby-tasks", tool_name="search_tasks", arguments={
    "query": "fix login bug",
    "current_stage_state": "in_progress",
    "task_type": "bug",
    "priority": 1,
    "parent_task_id": "#123",
    "category": "code",
    "limit": 20,
    "min_score": 0.0,
    "all_projects": False
})
```

CLI task search:

```bash
gobby tasks search QUERY [OPTIONS]
```

| Option | Description |
|--------|-------------|
| `--type`, `-t` | Filter by task type: `task`, `bug`, `feature`, or `epic` |
| `--priority`, `-p` | Filter by priority: `1`, `2`, or `3` |
| `--project` | Filter by project name or UUID |
| `--all-projects`, `-a` | Search every project |
| `--limit`, `-n` | Maximum result count |
| `--min-score` | Minimum relevance score |
| `--json` | Output JSON |

Use `gobby tasks reindex` or `gobby-tasks-ops.reindex_tasks` after bulk task
changes if results seem stale.

## Skill Search

There are two skill search paths:

- `gobby-skills.search_skills` searches installed skills.
- `gobby-skills.search_hub` and `gobby skills search` search configured hubs for
  installable skills.

Installed skill search:

```python
call_tool(server_name="gobby-skills", tool_name="search_skills", arguments={
    "query": "source control",
    "category": "core",
    "tags_any": ["git"],
    "top_k": 5,
    "include_internal": False
})
```

Hub search:

```python
call_tool(server_name="gobby-skills", tool_name="search_hub", arguments={
    "query": "code review",
    "hub_name": "clawdhub"
})
```

```bash
gobby skills search "code review" --hub clawdhub --limit 10
```

## MCP Tool Discovery

Use lightweight discovery before tool calls:

```python
list_mcp_servers()
list_tools(server_name="gobby-memory")
get_tool_schema(server_name="gobby-memory", tool_name="search_memories")
```

Use direct proxy search when you need tool suggestions:

```python
recommend_tools(
    task_description="Find memories related to authentication",
    search_mode="hybrid",
    top_k=10,
    min_similarity=0.3
)

search_tools(
    query="memory search",
    top_k=10,
    min_similarity=0.3,
    server_name="gobby-memory"
)
```

`recommend_tools` defaults to `llm` mode. `search_tools` is embedding-based and
requires semantic tool embeddings to be available.

## Statistics

Unified searchers expose `get_stats()` from code. The returned dictionary
includes:

```python
{
    "mode": "auto",
    "fitted": True,
    "fitted_mode": "auto",
    "active_backend": "embedding",
    "using_fallback": False,
    "fallback_reason": None,
    "item_count": 1500,
    "keyword": {...},
    "embedding": {...}
}
```

Memory search results may also include per-result diagnostics such as
`search_via`, `ranking_score`, `raw_semantic_score`, `temporal_decay_factor`,
and `ranking_mode`.

## Troubleshooting

### Search returns no results

1. Verify the query is non-empty.
2. Remove restrictive filters such as tags, project, category, or stage state.
3. Lower `min_score` or `min_similarity`.
4. Reindex the relevant surface if bulk data changed.

### Embedding search falls back

1. Check `embeddings.model`, `embeddings.dim`, and `embeddings.api_base`.
2. Confirm the endpoint exposes `/models`.
3. Set an API key for cloud providers, or use a local OpenAI-compatible server.
4. Use `search.mode: keyword` when offline behavior matters more than semantic quality.

### Tool search fails

1. Use progressive discovery first: `list_mcp_servers`, `list_tools`, then
   `get_tool_schema`.
2. For `search_tools`, confirm semantic tool embeddings are configured.
3. For `recommend_tools`, use `search_mode="llm"` when semantic search is unavailable.

## See Also

- [memory.md](./memory.md) - Memory system
- [tasks.md](./tasks.md) - Task management
- [skills.md](./skills.md) - Skill system
- [code-index.md](./code-index.md) - Source code search
- [configuration.md](./configuration.md) - Full config reference

_Last verified: 2026-05-29_
