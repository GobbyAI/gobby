# Memory V2: Memora-Inspired Enhancements

## Overview

Memory V2 overhauls gobby-memory's search and relationship capabilities, inspired by [Memora](https://github.com/agentic-mcp-tools/memora). The goal is better semantic search without API dependencies, automatic memory relationships, and visualization.

**Key improvements:**
- **TF-IDF semantic search** - Zero-dependency local search (no OpenAI API required)
- **Cross-references** - Auto-link related memories based on similarity
- **Knowledge graph visualization** - Interactive HTML graph with vis.js
- **Enhanced tag filtering** - Boolean logic (AND/OR/NOT)

**What stays the same:**
- Project scoping, session linking, importance/decay
- MCP tool interface (remember, recall, forget, etc.)
- Workflow integration, handoff context
- Git sync to .gobby/memories.jsonl

## Architecture

```
┌─────────────────────────────────────────────────────────────┐
│                     MemoryManager                            │
│  ┌─────────────────────────────────────────────────────────┐│
│  │                   Search Backend                         ││
│  │  ┌─────────────┐  ┌─────────────┐  ┌─────────────────┐  ││
│  │  │   TF-IDF    │  │   OpenAI    │  │  Text Search    │  ││
│  │  │  (default)  │  │ (optional)  │  │   (fallback)    │  ││
│  │  └──────┬──────┘  └──────┬──────┘  └────────┬────────┘  ││
│  │         └────────────────┼──────────────────┘           ││
│  │                          ▼                               ││
│  │                  Hybrid Ranker (RRF)                     ││
│  └─────────────────────────────────────────────────────────┘│
│                                                              │
│  ┌─────────────────────────────────────────────────────────┐│
│  │               Cross-Reference Engine                     ││
│  │  - Auto-link on create                                   ││
│  │  - Similarity threshold configurable                     ││
│  │  - Bidirectional relationships                           ││
│  └─────────────────────────────────────────────────────────┘│
│                                                              │
│  ┌─────────────────────────────────────────────────────────┐│
│  │               Visualization Export                       ││
│  │  - vis.js HTML graph                                     ││
│  │  - Color by memory type                                  ││
│  │  - Size by importance                                    ││
│  └─────────────────────────────────────────────────────────┘│
└─────────────────────────────────────────────────────────────┘
```

## Phase 1: TF-IDF Search Backend

### Why TF-IDF?

- **Zero dependencies** - Uses sklearn's TfidfVectorizer (already a transitive dep)
- **No API costs** - Works completely offline
- **Fast** - Sub-millisecond search for thousands of memories
- **Good enough** - For memory recall, TF-IDF captures keyword/concept overlap well

The existing OpenAI embedding search remains available as an optional backend for users who want deeper semantic matching.

### Implementation

Create `src/gobby/memory/search/tfidf.py`:

```python
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.metrics.pairwise import cosine_similarity
import numpy as np

class TFIDFSearcher:
    """Zero-dependency semantic search using TF-IDF."""

    def __init__(self):
        self.vectorizer = TfidfVectorizer(
            stop_words='english',
            ngram_range=(1, 2),  # Unigrams + bigrams
            max_features=10000,
            min_df=1,
        )
        self._fitted = False
        self._memory_ids: list[str] = []
        self._vectors = None

    def fit(self, memories: list[tuple[str, str]]) -> None:
        """Build index from all memories. Call after bulk changes."""
        if not memories:
            self._fitted = False
            return

        self._memory_ids = [m[0] for m in memories]
        contents = [m[1] for m in memories]
        self._vectors = self.vectorizer.fit_transform(contents)
        self._fitted = True

    def search(self, query: str, top_k: int = 10) -> list[tuple[str, float]]:
        """Return [(memory_id, similarity_score), ...] sorted by relevance."""
        if not self._fitted or len(self._memory_ids) == 0:
            return []

        query_vec = self.vectorizer.transform([query])
        similarities = cosine_similarity(query_vec, self._vectors)[0]

        # Get top-k indices
        top_indices = np.argsort(similarities)[-top_k:][::-1]

        return [
            (self._memory_ids[i], float(similarities[i]))
            for i in top_indices
            if similarities[i] > 0
        ]

    def needs_refit(self) -> bool:
        """Check if index needs rebuilding."""
        return not self._fitted
```

### Search Backend Protocol

Create `src/gobby/memory/search/__init__.py`:

```python
from typing import Protocol

class SearchBackend(Protocol):
    """Protocol for pluggable search backends."""

    def fit(self, memories: list[tuple[str, str]]) -> None:
        """Build/rebuild the search index."""
        ...

    def search(self, query: str, top_k: int = 10) -> list[tuple[str, float]]:
        """Search for relevant memories."""
        ...

    def needs_refit(self) -> bool:
        """Check if index needs rebuilding."""
        ...


def get_search_backend(backend_type: str, **kwargs) -> SearchBackend:
    """Factory for search backends."""
    if backend_type == "tfidf":
        from gobby.memory.search.tfidf import TFIDFSearcher
        return TFIDFSearcher()
    elif backend_type == "openai":
        from gobby.memory.semantic_search import SemanticMemorySearch
        # Wrap existing implementation
        return OpenAISearchAdapter(**kwargs)
    else:
        raise ValueError(f"Unknown search backend: {backend_type}")
```

### Configuration

```yaml
memory:
  search_backend: "tfidf"  # tfidf, openai, or hybrid

  tfidf:
    ngram_range: [1, 2]
    max_features: 10000
    refit_threshold: 10  # Refit after N new memories

  openai:
    model: "text-embedding-3-small"
    # Requires OPENAI_API_KEY

  hybrid:
    tfidf_weight: 0.5
    openai_weight: 0.5
```

### Checklist

- [ ] Create `src/gobby/memory/search/` package
- [ ] Implement `TFIDFSearcher` class
- [ ] Create `SearchBackend` protocol
- [ ] Implement `OpenAISearchAdapter` wrapping existing code
- [ ] Add `HybridSearcher` combining both
- [ ] Update `MemoryManager.recall()` to use search backend
- [ ] Add refit trigger on memory mutations
- [ ] Add `gobby memory reindex` CLI command
- [ ] Add config schema for search backend selection
- [ ] Unit tests for TFIDFSearcher
- [ ] Integration tests for search backend switching

---

## Phase 2: Cross-References

### Concept

Automatically link related memories when created. This enables:
- "Show me related memories" queries
- Graph-based memory exploration
- Better context injection (include related memories)

### Data Model

```sql
CREATE TABLE memory_crossrefs (
    source_id TEXT NOT NULL,
    target_id TEXT NOT NULL,
    similarity REAL NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (source_id, target_id),
    FOREIGN KEY (source_id) REFERENCES memories(id) ON DELETE CASCADE,
    FOREIGN KEY (target_id) REFERENCES memories(id) ON DELETE CASCADE
);

CREATE INDEX idx_crossrefs_source ON memory_crossrefs(source_id);
CREATE INDEX idx_crossrefs_target ON memory_crossrefs(target_id);
```

### Implementation

```python
# In MemoryManager

async def remember(self, content: str, ...) -> Memory:
    memory = await self.storage.create_memory(...)

    # Auto cross-reference if enabled
    if self.config.auto_crossref:
        await self._create_crossrefs(memory)

    return memory

async def _create_crossrefs(
    self,
    memory: Memory,
    threshold: float = 0.3,
    max_links: int = 5,
) -> None:
    """Find and link similar memories."""
    similar = self.search_backend.search(memory.content, top_k=max_links + 1)

    for other_id, score in similar:
        if other_id != memory.id and score >= threshold:
            self.storage.create_crossref(memory.id, other_id, score)

def get_related(self, memory_id: str, limit: int = 5) -> list[Memory]:
    """Get memories linked to this one."""
    crossrefs = self.storage.get_crossrefs(memory_id, limit)
    return [self.storage.get_memory(ref.target_id) for ref in crossrefs]
```

### MCP Tool

```python
@registry.tool(
    name="get_related_memories",
    description="Get memories related to a specific memory.",
)
def get_related_memories(
    memory_id: str,
    limit: int = 5,
) -> dict[str, Any]:
    """Get memories linked via cross-references."""
    related = memory_manager.get_related(memory_id, limit)
    return {
        "success": True,
        "memories": [m.to_dict() for m in related],
    }
```

### Configuration

```yaml
memory:
  auto_crossref: true
  crossref_threshold: 0.3  # Minimum similarity to create link
  crossref_max_links: 5    # Max links per memory
```

### Checklist

- [ ] Create database migration for `memory_crossrefs` table
- [ ] Add `create_crossref()`, `get_crossrefs()` to storage layer
- [ ] Implement `_create_crossrefs()` in MemoryManager
- [ ] Add `get_related()` method
- [ ] Add `get_related_memories` MCP tool
- [ ] Add `gobby memory related MEMORY_ID` CLI command
- [ ] Add config options for cross-referencing
- [ ] Unit tests for crossref creation
- [ ] Integration tests for related memory queries

---

## Phase 3: Enhanced Tag Filtering

### Current State

Basic tag matching with simple LIKE queries.

### Enhanced Filtering

Support boolean logic for tag queries:

```python
def search_memories(
    self,
    query_text: str | None = None,
    tags_all: list[str] | None = None,    # AND - must have all
    tags_any: list[str] | None = None,    # OR - must have at least one
    tags_none: list[str] | None = None,   # NOT - must not have any
    **kwargs,
) -> list[Memory]:
    sql = "SELECT * FROM memories WHERE 1=1"
    params = []

    if tags_all:
        for tag in tags_all:
            sql += " AND tags LIKE ?"
            params.append(f'%"{tag}"%')

    if tags_any:
        tag_clauses = [f"tags LIKE ?" for _ in tags_any]
        sql += f" AND ({' OR '.join(tag_clauses)})"
        params.extend(f'%"{tag}"%' for tag in tags_any)

    if tags_none:
        for tag in tags_none:
            sql += " AND (tags IS NULL OR tags NOT LIKE ?)"
            params.append(f'%"{tag}"%')

    # ... rest of query
```

### MCP Tool Update

```python
@registry.tool(
    name="recall",
    description="Recall memories with optional filtering.",
)
def recall(
    query: str | None = None,
    tags_all: list[str] | None = None,
    tags_any: list[str] | None = None,
    tags_none: list[str] | None = None,
    # ... existing params
) -> dict[str, Any]:
    ...
```

### Checklist

- [ ] Update `search_memories()` with boolean tag logic
- [ ] Update `recall` MCP tool with new tag params
- [ ] Update `gobby memory recall` CLI with tag flags
- [ ] Add documentation for tag filtering
- [ ] Unit tests for each tag filter mode

---

## Phase 4: Knowledge Graph Visualization

### Concept

Export memory graph as standalone HTML with vis.js for interactive exploration.

### Implementation

Create `src/gobby/memory/viz.py`:

```python
import json
from pathlib import Path

def export_memory_graph(
    memories: list[Memory],
    crossrefs: list[CrossRef],
    output_path: Path | None = None,
) -> str:
    """Generate standalone HTML with vis.js graph."""

    # Color by type
    colors = {
        "fact": "#4CAF50",      # Green
        "preference": "#2196F3", # Blue
        "pattern": "#FF9800",    # Orange
        "context": "#9C27B0",    # Purple
    }

    nodes = []
    edges = []

    for mem in memories:
        nodes.append({
            "id": mem.id,
            "label": _truncate(mem.content, 40),
            "title": mem.content,  # Tooltip
            "color": colors.get(mem.memory_type, "#9E9E9E"),
            "size": 10 + (mem.importance * 20),
            "font": {"size": 12},
        })

    for ref in crossrefs:
        edges.append({
            "from": ref.source_id,
            "to": ref.target_id,
            "value": ref.similarity,
            "title": f"Similarity: {ref.similarity:.2f}",
        })

    html = _generate_html(nodes, edges)

    if output_path:
        output_path.write_text(html)

    return html


def _truncate(text: str, max_len: int) -> str:
    if len(text) <= max_len:
        return text
    return text[:max_len-3] + "..."


def _generate_html(nodes: list, edges: list) -> str:
    return f"""<!DOCTYPE html>
<html>
<head>
    <title>Gobby Memory Graph</title>
    <script src="https://unpkg.com/vis-network/standalone/umd/vis-network.min.js"></script>
    <style>
        body {{ margin: 0; padding: 0; font-family: system-ui, sans-serif; }}
        #graph {{ width: 100%; height: 100vh; }}
        #legend {{
            position: absolute; top: 10px; right: 10px;
            background: white; padding: 10px; border-radius: 5px;
            box-shadow: 0 2px 10px rgba(0,0,0,0.1);
        }}
        .legend-item {{ display: flex; align-items: center; margin: 5px 0; }}
        .legend-color {{ width: 16px; height: 16px; border-radius: 50%; margin-right: 8px; }}
    </style>
</head>
<body>
    <div id="graph"></div>
    <div id="legend">
        <div class="legend-item"><div class="legend-color" style="background: #4CAF50"></div>Fact</div>
        <div class="legend-item"><div class="legend-color" style="background: #2196F3"></div>Preference</div>
        <div class="legend-item"><div class="legend-color" style="background: #FF9800"></div>Pattern</div>
        <div class="legend-item"><div class="legend-color" style="background: #9C27B0"></div>Context</div>
    </div>
    <script>
        var nodes = new vis.DataSet({json.dumps(nodes)});
        var edges = new vis.DataSet({json.dumps(edges)});
        var container = document.getElementById('graph');
        var data = {{ nodes: nodes, edges: edges }};
        var options = {{
            physics: {{
                stabilization: {{ iterations: 100 }},
                barnesHut: {{ gravitationalConstant: -2000 }}
            }},
            nodes: {{ shape: 'dot', borderWidth: 2 }},
            edges: {{ smooth: {{ type: 'continuous' }} }},
            interaction: {{ hover: true, tooltipDelay: 100 }}
        }};
        var network = new vis.Network(container, data, options);
    </script>
</body>
</html>"""
```

### CLI Command

```python
@memory.command()
@click.option("--output", "-o", type=click.Path(), help="Output HTML file")
@click.option("--open", "open_browser", is_flag=True, help="Open in browser")
def graph(output: str | None, open_browser: bool):
    """Export and view memory graph visualization."""
    memories = memory_manager.list_memories(limit=500)
    crossrefs = memory_manager.get_all_crossrefs()

    if output:
        output_path = Path(output)
    else:
        output_path = Path(tempfile.gettempdir()) / "gobby_memory_graph.html"

    export_memory_graph(memories, crossrefs, output_path)

    click.echo(f"Graph exported to {output_path}")

    if open_browser:
        import webbrowser
        webbrowser.open(f"file://{output_path}")
```

### Checklist

- [ ] Create `src/gobby/memory/viz.py`
- [ ] Implement `export_memory_graph()` function
- [ ] Add `gobby memory graph` CLI command
- [ ] Add `--output` and `--open` flags
- [ ] Add optional MCP tool for graph export
- [ ] Unit tests for graph generation
- [ ] Integration test for CLI command

---

## Phase 5: Migration & Configuration

### Config Updates

```yaml
# ~/.gobby/config.yaml

memory:
  enabled: true

  # Search backend (NEW)
  search_backend: "tfidf"  # tfidf, openai, hybrid

  tfidf:
    ngram_range: [1, 2]
    max_features: 10000
    refit_threshold: 10

  # Cross-references (NEW)
  auto_crossref: true
  crossref_threshold: 0.3
  crossref_max_links: 5

  # Existing settings
  auto_extract: true
  injection_limit: 10
  importance_threshold: 0.3
  decay_enabled: true
  decay_rate: 0.05
  decay_floor: 0.1
```

### Migration Steps

1. **Database migration** - Add `memory_crossrefs` table
2. **Backfill crossrefs** - Run similarity search on existing memories
3. **Build TF-IDF index** - Index all existing memories

```python
async def migrate_to_v2():
    """One-time migration to Memory V2."""

    # 1. Create crossrefs table
    db.execute(CROSSREFS_MIGRATION_SQL)

    # 2. Build TF-IDF index
    memories = storage.list_memories(limit=10000)
    search_backend.fit([(m.id, m.content) for m in memories])

    # 3. Backfill crossrefs
    for memory in memories:
        await manager._create_crossrefs(memory)

    logger.info(f"Migrated {len(memories)} memories to V2")
```

### Checklist

- [ ] Create database migration script
- [ ] Add migration command: `gobby memory migrate-v2`
- [ ] Update config schema with new options
- [ ] Add startup check for pending migration
- [ ] Document migration process

---

## Implementation Order

1. **Phase 1: TF-IDF Search**
   - Highest value - enables semantic search without API
   - Unblocks Phase 2

2. **Phase 2: Cross-References**
   - Depends on search backend for similarity
   - Unblocks Phase 4

3. **Phase 3: Tag Filtering**
   - Independent, can be done anytime
   - Quick win

4. **Phase 4: Visualization**
   - Depends on crossrefs for graph edges
   - Nice-to-have, impressive demo

5. **Phase 5: Migration**
   - Do after Phase 1+2 are stable
   - Required for existing users

---

## Decisions

| # | Question | Decision | Rationale |
|---|----------|----------|-----------|
| 1 | Primary search backend | TF-IDF | Zero dependencies, fast, good enough for memory recall |
| 2 | Keep OpenAI backend? | Yes, as optional | Some users may prefer deeper semantic matching |
| 3 | Crossref storage | Separate table | Clean schema, easy to query bidirectionally |
| 4 | Crossref creation | On memory create | Real-time linking, no batch job needed |
| 5 | Visualization library | vis.js (CDN) | No bundling needed, standalone HTML |
| 6 | Migration approach | Explicit command | User controls when to migrate |

---

## Supersedes

This plan supersedes **Phase 8: Semantic Memory Search with PostgreSQL-vec** from `docs/plans/enhancements.md`.

**Why the change:**
- PostgreSQL-vec requires native extension loading (platform issues)
- sentence-transformers adds ~500MB dependency
- TF-IDF achieves similar results for memory recall use case
- Memora's approach is battle-tested and simpler
