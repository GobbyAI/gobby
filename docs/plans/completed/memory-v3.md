# Memory V3: Backend Abstraction Layer

## Overview

Memory V3 transforms gobby-memory from a monolithic implementation into a **pluggable abstraction layer** that can integrate with external memory systems. Users can choose between Gobby's built-in PostgreSQL backend, or plug in established memory frameworks like MemU, Mem0, or OpenMemory.

**Strategic rationale:** Like Microsoft making Excel compatible with Lotus 1-2-3, reducing switching costs accelerates adoption. Users already invested in Mem0 or MemU can use Gobby's orchestration layer without migrating their memory infrastructure.

**Key changes:**

- **Backend protocol** - Standardized interface for memory operations
- **Pluggable backends** - PostgreSQL (built-in), MemU, Mem0, OpenMemory, custom
- **Multimodal support** - Image attachments for browser automation (Playwright, Puppeteer)
- **Universal JSONL backup** - All backends get automatic backup to `.gobby/memories.jsonl`
- **Capability detection** - Graceful degradation for feature differences

**What stays the same:**

- MCP tool interface (`create_memory`, `search_memories`, `delete_memory`, etc.)
- Workflow integration and hooks
- Session linking and project scoping
- CLI commands

## Architecture

```
┌───────────────────────────────────────────────────────────────────────┐
│                       MemoryManager (facade)                          │
│  ┌─────────────────────────────────────────────────────────────────┐  │
│  │ create_memory(content, ...)                                     │  │
│  │   1. backend.create_memory() → primary storage                  │  │
│  │   2. backup_manager.trigger_export() → .gobby/memories.jsonl    │  │
│  └─────────────────────────────────────────────────────────────────┘  │
└───────────────────────────────┬───────────────────────────────────────┘
                                │
      ┌─────────────┬───────────┼───────────┬─────────────┐
      │             │           │           │             │
 ┌────▼────┐  ┌─────▼─────┐ ┌───▼───┐ ┌─────▼──────┐ ┌────▼────┐
 │ PostgreSQL  │  │   MemU    │ │ Mem0  │ │ OpenMemory │ │ Custom  │
 │ Backend │  │  Backend  │ │Backend│ │  Backend   │ │ Backend │
 └────┬────┘  └─────┬─────┘ └───┬───┘ └─────┬──────┘ └────┬────┘
      │             │           │           │             │
  PostgreSQL DB    MemU markdown  Cloud API  localhost:60887  User impl
  (local)      (local files)  (remote)   (self-hosted)

                                │
                         ┌──────▼──────┐
                         │ JSONL Backup │  ← Universal backup
                         │ (snapshot)   │     regardless of backend
                         └─────────────┘
```

### Component Responsibilities

| Component | Responsibility |
|-----------|----------------|
| `MemoryManager` | High-level API, capability adaptation, multimodal helpers, backup trigger |
| `MemoryBackend` (protocol) | Contract all backends implement |
| `SqliteMemoryBackend` | Built-in implementation with TF-IDF search |
| `MemuMemoryBackend` | Integration with MemU framework |
| `Mem0MemoryBackend` | Integration with Mem0 cloud API |
| `OpenMemoryBackend` | Integration with self-hosted OpenMemory |
| `NullMemoryBackend` | No-op for testing/disabled memory |
| `MemoryBackupManager` | Universal JSONL backup with debouncing |

### Key Design Decisions

| Decision | Choice | Rationale |
|----------|--------|-----------|
| JSONL sync | Universal backup at facade level | All backends get backup; migration path between backends |
| JSONL mode | Snapshot (periodic rewrite) | Simpler than append-only; matches current behavior |
| JSONL import | Manual recovery only | Backends are source of truth; CLI for migration |
| Search abstraction | `search_memories()` delegates to backend's native search | PostgreSQL→TF-IDF, MemU→RAG/LLM, Mem0→embedding; no forced dependency |
| Naming convention | Consistent with gobby-tasks pattern | `create_memory`, `search_memories`, `delete_memory`, etc. |
| Image descriptions | Route through LLMService.describe_image() | Consistent with other LLM features; provider-agnostic |

## Naming Convention

### MCP Tools

| Tool | Purpose |
|------|---------|
| `create_memory` | Store a new memory |
| `search_memories` | Find memories by query (backend-native search) |
| `delete_memory` | Remove a memory |
| `list_memories` | List all memories (no query, just filters) |
| `get_memory` | Get single memory by ID |
| `update_memory` | Modify existing memory |
| `memory_stats` | Get statistics |

**Note:** Rename `recall_memory` → `search_memories` for consistency with gobby-tasks naming.

### Friendly Slash Commands

| Command | Maps to |
|---------|---------|
| `/gobby:remember` | `create_memory` |
| `/gobby:recall` | `search_memories` |
| `/gobby:forget` | `delete_memory` |

### Backend Protocol Methods

| Method | Notes |
|--------|-------|
| `create_memory()` | Returns MemoryRecord |
| `search_memories(query)` | Backend-native search |
| `delete_memory(id)` | Returns bool |
| `get_memory(id)` | Returns MemoryRecord or None |
| `list_memories()` | No query, just filters |
| `update_memory(id, ...)` | Returns updated MemoryRecord |

## Backend Protocol

### Core Types

```python
# src/gobby/memory/protocol.py

from abc import abstractmethod
from dataclasses import dataclass, field
from enum import Flag, auto
from typing import Protocol, runtime_checkable


class MemoryCapability(Flag):
    """Capabilities a backend may support. Backends declare what they provide."""

    # Core operations
    BASIC = auto()              # create, search, delete, get
    UPDATE = auto()             # update existing memories

    # Search modes
    SEMANTIC_SEARCH = auto()    # Embedding-based similarity search
    KEYWORD_SEARCH = auto()     # BM25/TF-IDF text search
    HYBRID_SEARCH = auto()      # Combined semantic + keyword

    # Multimodal
    IMAGES = auto()             # Image attachments

    # Organization
    CATEGORIES = auto()         # Hierarchical organization (MemU-style)
    TAGS = auto()               # Tag-based filtering
    CROSS_REFERENCES = auto()   # Auto-linking related memories

    # Maintenance
    IMPORTANCE_DECAY = auto()   # Automatic importance decay over time
    DEDUPLICATION = auto()      # Semantic duplicate detection


@dataclass
class MemoryQuery:
    """Unified query object for memory retrieval."""
    text: str | None = None              # Query string for search
    memory_type: str | None = None       # Filter: fact, preference, pattern, context
    min_importance: float = 0.0          # Minimum importance threshold
    tags_all: list[str] | None = None    # Must have ALL these tags
    tags_any: list[str] | None = None    # Must have at least ONE of these tags
    tags_none: list[str] | None = None   # Must NOT have any of these tags
    project_id: str | None = None        # Scope to specific project
    include_global: bool = True          # Include project_id=NULL memories
    limit: int = 10                       # Max results
    offset: int = 0                       # Pagination offset
    category: str | None = None          # MemU: filter by category


@dataclass
class MediaAttachment:
    """Attachment for multimodal memories (images)."""
    media_type: str                      # "image"
    mime_type: str                       # "image/png", "image/jpeg"
    reference: str                       # File path or URL
    storage_mode: str = "file"           # "file", "url", "inline"
    description: str | None = None       # Text description for searchability
    width: int | None = None
    height: int | None = None


@dataclass
class MemoryRecord:
    """Normalized memory representation across all backends."""
    id: str
    content: str
    memory_type: str                               # fact, preference, pattern, context
    importance: float                              # 0.0-1.0
    tags: list[str] = field(default_factory=list)
    created_at: str = ""
    updated_at: str = ""
    project_id: str | None = None
    source_type: str | None = None                 # user, session, skill, inferred
    source_session_id: str | None = None
    access_count: int = 0
    last_accessed_at: str | None = None
    media: list[MediaAttachment] = field(default_factory=list)
    similarity_score: float | None = None          # From search results
    category: str | None = None                    # MemU category
    backend_metadata: dict = field(default_factory=dict)
```

### Backend Protocol

```python
@runtime_checkable
class MemoryBackend(Protocol):
    """Core protocol all memory backends must implement."""

    @property
    def capabilities(self) -> MemoryCapability:
        """Declare what this backend supports."""
        ...

    @property
    def name(self) -> str:
        """Human-readable backend name."""
        ...

    # === Core Operations ===

    async def create_memory(
        self,
        content: str,
        memory_type: str = "fact",
        importance: float = 0.5,
        tags: list[str] | None = None,
        project_id: str | None = None,
        source_type: str | None = None,
        source_session_id: str | None = None,
        media: list[MediaAttachment] | None = None,
        category: str | None = None,
    ) -> MemoryRecord:
        """Store a new memory."""
        ...

    async def search_memories(self, query: MemoryQuery) -> list[MemoryRecord]:
        """Retrieve memories matching the query using backend-native search."""
        ...

    async def delete_memory(self, memory_id: str) -> bool:
        """Delete a memory by ID."""
        ...

    async def get_memory(self, memory_id: str) -> MemoryRecord | None:
        """Get a specific memory by ID."""
        ...

    async def update_memory(
        self,
        memory_id: str,
        content: str | None = None,
        importance: float | None = None,
        tags: list[str] | None = None,
        media: list[MediaAttachment] | None = None,
    ) -> MemoryRecord | None:
        """Update an existing memory. Requires UPDATE capability."""
        ...

    async def list_memories(
        self,
        project_id: str | None = None,
        memory_type: str | None = None,
        min_importance: float = 0.0,
        limit: int = 100,
        offset: int = 0,
    ) -> list[MemoryRecord]:
        """List memories with basic filtering (no search)."""
        ...

    async def count(
        self,
        project_id: str | None = None,
        memory_type: str | None = None,
    ) -> int:
        """Count memories matching filters."""
        ...

    # === Lifecycle ===

    async def initialize(self) -> None:
        """Initialize the backend."""
        ...

    async def shutdown(self) -> None:
        """Clean shutdown."""
        ...

    async def health_check(self) -> dict:
        """Check backend health."""
        ...
```

## Backend Implementations

### PostgreSQL Backend (Built-in)

```python
# src/gobby/memory/backends/PostgreSQL.py

class SqliteMemoryBackend:
    """Built-in PostgreSQL backend. Zero external dependencies."""

    @property
    def capabilities(self) -> MemoryCapability:
        return (
            MemoryCapability.BASIC |
            MemoryCapability.UPDATE |
            MemoryCapability.KEYWORD_SEARCH |  # TF-IDF
            MemoryCapability.TAGS |
            MemoryCapability.CROSS_REFERENCES |
            MemoryCapability.IMPORTANCE_DECAY |
            MemoryCapability.IMAGES
        )

    @property
    def name(self) -> str:
        return "PostgreSQL"
```

### MemU Backend

**MemU API** (memu-py): https://github.com/NevaMind-AI/memU

```python
# src/gobby/memory/backends/memu.py

from memu import MemUService

class MemuMemoryBackend:
    """Backend for MemU framework."""

    @property
    def capabilities(self) -> MemoryCapability:
        return (
            MemoryCapability.BASIC |
            MemoryCapability.UPDATE |
            MemoryCapability.SEMANTIC_SEARCH |
            MemoryCapability.HYBRID_SEARCH |
            MemoryCapability.IMAGES |
            MemoryCapability.CATEGORIES |
            MemoryCapability.TAGS
        )

    async def create_memory(self, content, **kwargs) -> MemoryRecord:
        # Maps to service.memorize()
        ...

    async def search_memories(self, query) -> list[MemoryRecord]:
        # Maps to service.retrieve()
        ...
```

### Mem0 Backend

**Mem0 API** (mem0ai): https://mem0.ai

```python
# src/gobby/memory/backends/mem0.py

from mem0 import Memory

class Mem0MemoryBackend:
    """Backend for Mem0 cloud API."""

    @property
    def capabilities(self) -> MemoryCapability:
        return (
            MemoryCapability.BASIC |
            MemoryCapability.UPDATE |
            MemoryCapability.SEMANTIC_SEARCH |
            MemoryCapability.TAGS |
            MemoryCapability.DEDUPLICATION |
            MemoryCapability.CROSS_REFERENCES
        )

    async def create_memory(self, content, **kwargs) -> MemoryRecord:
        # Maps to memory.add()
        ...

    async def search_memories(self, query) -> list[MemoryRecord]:
        # Maps to memory.search()
        ...
```

### OpenMemory Backend

**OpenMemory**: Self-hosted Mem0 variant - https://github.com/mem0ai/mem0/tree/main/openmemory

```python
# src/gobby/memory/backends/openmemory.py

class OpenMemoryBackend:
    """Backend for self-hosted OpenMemory."""

    def __init__(self, base_url: str = "http://localhost:60887"):
        self._base_url = base_url

    @property
    def capabilities(self) -> MemoryCapability:
        return (
            MemoryCapability.BASIC |
            MemoryCapability.UPDATE |
            MemoryCapability.SEMANTIC_SEARCH |
            MemoryCapability.TAGS
        )

    # Maps to REST API endpoints
```

### Null Backend (Testing)

```python
# src/gobby/memory/backends/null.py

class NullMemoryBackend:
    """No-op backend for testing."""

    @property
    def capabilities(self) -> MemoryCapability:
        return MemoryCapability.BASIC | MemoryCapability.UPDATE

    @property
    def name(self) -> str:
        return "null"
```

## Configuration

```yaml
# ~/.gobby/config.yaml

memory:
  # Backend selection
  backend: memu  # memu (default), PostgreSQL, mem0, openmemory, null

  # Common settings
  enabled: true
  importance_threshold: 0.3

  # Backend-specific configuration
  backends:
    PostgreSQL:
      tfidf_enabled: true
      decay_enabled: true
      decay_rate: 0.05
      decay_floor: 0.1
      images_enabled: true
      resources_path: .gobby/resources

    memu:
      # Uses memu-py package
      # pip install memu-py

    mem0:
      api_key: ${MEM0_API_KEY}
      base_url: null  # For self-hosted

    openmemory:
      base_url: http://localhost:60887
      user_id: default

# Backup settings
memory_backup:
  enabled: true
  export_debounce: 5.0  # seconds
```

## Implementation Phases

### Phase 1: Protocol & PostgreSQL Refactor

**Goal**: Extract protocol, refactor current implementation, maintain 100% compatibility.

**Files:**
```
src/gobby/memory/
├── protocol.py              # NEW
├── manager.py               # MODIFY
├── backends/
│   ├── __init__.py          # NEW
│   ├── PostgreSQL.py            # NEW (from storage/memories.py)
│   └── null.py              # NEW
```

**Tasks:**
1. [ ] Create `protocol.py` with types
2. [ ] Create `backends/__init__.py` with factory
3. [ ] Create `backends/PostgreSQL.py` (refactor LocalMemoryManager)
4. [ ] Create `backends/null.py`
5. [ ] Modify `manager.py` to use backend protocol
6. [ ] Modify `sync/memories.py` → backup-only
7. [ ] Add config for backend selection
8. [ ] Rename MCP tool `recall_memory` → `search_memories`
9. [ ] Create slash commands (`/gobby:remember`, `/gobby:recall`, `/gobby:forget`)
10. [ ] Ensure all existing tests pass

### Phase 2: Multimodal Support

**Goal**: Add image attachment support with LLM-generated descriptions.

**Tasks:**
1. [ ] Add `MediaAttachment` to protocol
2. [ ] Add `media` column migration
3. [ ] Add `LLMService.describe_image()`
4. [ ] Add `remember_with_image()`, `remember_screenshot()` helpers
5. [ ] Update SqliteMemoryBackend for media
6. [ ] Add `.gobby/resources/` directory
7. [ ] Add tests

### Phase 3: MemU Backend

**Tasks:**
1. [ ] Create `backends/memu.py`
2. [ ] Map to MemUService API (`memorize()`, `retrieve()`)
3. [ ] Add integration tests

### Phase 4: Mem0 Backend

**Tasks:**
1. [ ] Create `backends/mem0.py`
2. [ ] Map to Memory API (`add()`, `search()`)
3. [ ] Add integration tests

### Phase 5: OpenMemory Backend

**Tasks:**
1. [ ] Create `backends/openmemory.py`
2. [ ] Connect to REST API
3. [ ] Add health checks
4. [ ] Add integration tests

### Phase 6: Markdown Sync (Optional)

**Tasks:**
1. [ ] Add `export_markdown()` method
2. [ ] Create markdown format
3. [ ] Add CLI commands

## Migration from V2

Memory V3 is **additive** to V2. Existing features remain in PostgreSQL backend:

| V2 Feature | V3 Location |
|------------|-------------|
| TF-IDF search | PostgreSQL backend (`KEYWORD_SEARCH`) |
| Cross-references | PostgreSQL backend (`CROSS_REFERENCES`) |
| Importance decay | PostgreSQL backend (`IMPORTANCE_DECAY`) |
| Visualization | Utility function (works with any backend) |

## Existing Features to Preserve

| Feature | Location | Status |
|---------|----------|--------|
| Cross-references | `memory_crossrefs` table | Keep in PostgreSQL backend |
| TF-IDF search | `memory/search/tfidf.py` | Keep, PostgreSQL uses it |
| Importance decay | `MemoryManager.decay_memories()` | Keep in facade |
| Tag filtering | `tags_all/any/none` params | Keep in protocol |
| Access tracking | `access_count`, `last_accessed_at` | Keep in record |
| Content deduplication | Hash-based memory IDs | Keep in PostgreSQL backend |
