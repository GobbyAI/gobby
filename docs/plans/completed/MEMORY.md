# Persistent Memory & Skills System

## Overview

A memory-first system that transforms Gobby from tracking sessions to maintaining **persistent agent memory** across sessions. Unlike session-based assistants where each conversation starts fresh, this system creates continuity—the agent learns, remembers, and improves over time.

**Inspiration:** [Letta Code](https://github.com/letta-ai/letta-code) - Memory-first coding agent with persistent learning and skill acquisition.

## Vision

> "From meeting a new contractor each session to having a coworker who remembers and learns."

Current AI CLIs (Claude Code, Gemini, Codex) treat each session as independent. Gobby already tracks sessions—this plan extends that to:

1. **Persistent Memory** - Learnings, preferences, and patterns survive across sessions
2. **Skill Learning** - Extract reusable patterns from successful sessions
3. **Context Injection** - Automatically enrich new sessions with relevant memories
4. **Cross-CLI Sharing** - Memory and skills work across Claude, Gemini, and Codex

## Core Design Principles

1. **Memory-first** - Memory persists even when session history is cleared
2. **Learn by doing** - Skills extracted from actual work, not predefined
3. **Selective recall** - Query relevant memories, not dump everything
4. **Git-distributed** - JSONL export enables sharing via git (optional)
5. **Privacy-aware** - Stealth mode keeps memories local-only

## Data Model

### Memories Table

```sql
CREATE TABLE memories (
    id TEXT PRIMARY KEY,              -- mm-{6 chars} hash-based ID
    project_id TEXT,                  -- NULL for global memories
    memory_type TEXT NOT NULL,        -- fact, preference, pattern, context
    content TEXT NOT NULL,            -- The actual memory content
    source_type TEXT,                 -- session, user, skill, inferred
    source_session_id TEXT,           -- Session that created this memory
    importance REAL DEFAULT 0.5,      -- 0.0-1.0 for recall prioritization
    access_count INTEGER DEFAULT 0,   -- How often retrieved
    last_accessed_at TEXT,            -- For decay calculations
    embedding BLOB,                   -- Vector embedding for semantic search
    tags TEXT,                        -- JSON array of tags
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY (project_id) REFERENCES projects(id),
    FOREIGN KEY (source_session_id) REFERENCES sessions(id)
);

CREATE INDEX idx_memories_project ON memories(project_id);
CREATE INDEX idx_memories_type ON memories(memory_type);
CREATE INDEX idx_memories_importance ON memories(importance DESC);
```

### Skills Table

```sql
CREATE TABLE skills (
    id TEXT PRIMARY KEY,              -- sk-{6 chars} hash-based ID
    project_id TEXT,                  -- NULL for global skills
    name TEXT NOT NULL,               -- Human-readable skill name
    description TEXT,                 -- What this skill does
    trigger_pattern TEXT,             -- When to suggest this skill
    instructions TEXT NOT NULL,       -- Actual skill content/instructions
    source_session_id TEXT,           -- Session skill was learned from
    usage_count INTEGER DEFAULT 0,    -- Times applied
    success_rate REAL,                -- Effectiveness tracking
    tags TEXT,                        -- JSON array
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY (project_id) REFERENCES projects(id),
    FOREIGN KEY (source_session_id) REFERENCES sessions(id)
);

CREATE INDEX idx_skills_project ON skills(project_id);
CREATE INDEX idx_skills_name ON skills(name);
```

### Memory-Session Links

```sql
CREATE TABLE session_memories (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    memory_id TEXT NOT NULL,
    action TEXT NOT NULL,             -- injected, created, accessed, updated
    created_at TEXT NOT NULL,
    FOREIGN KEY (session_id) REFERENCES sessions(id) ON DELETE CASCADE,
    FOREIGN KEY (memory_id) REFERENCES memories(id) ON DELETE CASCADE,
    UNIQUE(session_id, memory_id, action)
);

CREATE INDEX idx_session_memories_session ON session_memories(session_id);
CREATE INDEX idx_session_memories_memory ON session_memories(memory_id);
```

## Memory Types

| Type | Description | Example |
|------|-------------|---------|
| `fact` | Verified information about the project/codebase | "Uses pytest with conftest.py fixtures" |
| `preference` | User or project preferences | "Prefer functional components over class components" |
| `pattern` | Observed code patterns or conventions | "All API routes follow /api/v1/{resource} pattern" |
| `context` | Background context for the project | "This is a CLI tool for managing Docker containers" |

## Source Types

| Source | Description |
|--------|-------------|
| `user` | Explicitly added by user via `/remember` |
| `session` | Extracted from session summary |
| `skill` | Generated when learning a skill |
| `inferred` | LLM-inferred from multiple sessions |

## Memory Lifecycle

```text
┌─────────────────────────────────────────────────────────────────┐
│                     Memory Lifecycle                             │
├─────────────────────────────────────────────────────────────────┤
│                                                                  │
│  1. CREATION                                                     │
│     • User: /remember "always use uv for Python"                 │
│     • Session end: Extract from summary                          │
│     • Skill learning: Generate from trajectory                   │
│                                                                  │
│  2. STORAGE                                                      │
│     • PostgreSQL (local): ~/.gobby/PostgreSQL hub                         │
│     • Git sync (optional): .gobby/memories.jsonl                │
│     • Embeddings for semantic search                             │
│                                                                  │
│  3. RECALL                                                       │
│     • Session start: Inject relevant project memories            │
│     • On query: Semantic search + importance ranking             │
│     • Update access_count and last_accessed_at                   │
│                                                                  │
│  4. DECAY                                                        │
│     • Reduce importance over time if not accessed                │
│     • Archive low-importance memories after threshold            │
│     • Never delete user-created memories automatically           │
│                                                                  │
└─────────────────────────────────────────────────────────────────┘
```

## Skill Learning

Skills are reusable instructions extracted from successful work patterns.

### Skill Structure

```json
{
  "id": "sk-a1b2c3",
  "name": "run-tests",
  "description": "How to run tests in this project",
  "trigger_pattern": "test|pytest|testing|verify",
  "instructions": "This project uses pytest with specific configuration:\n\n1. Run all tests: `uv run pytest`\n2. Run single file: `uv run pytest tests/test_example.py -v`\n3. Fixtures are in `tests/conftest.py`\n4. Use `-m 'not slow'` to skip slow tests",
  "tags": ["testing", "pytest", "development"]
}
```

### Skill Learning Process

```text
┌─────────────────────────────────────────────────────────────────┐
│                     Skill Learning                               │
├─────────────────────────────────────────────────────────────────┤
│                                                                  │
│  1. User invokes: /skill "how to run tests"                     │
│                                                                  │
│  2. LLM analyzes current session trajectory:                     │
│     • Commands executed                                          │
│     • Files read/modified                                        │
│     • Patterns observed                                          │
│                                                                  │
│  3. LLM generates skill:                                         │
│     • Name and description                                       │
│     • Trigger pattern (when to suggest)                          │
│     • Step-by-step instructions                                  │
│                                                                  │
│  4. Skill saved to database and exported to .claude/skills/     │
│                                                                  │
│  5. Future sessions:                                             │
│     • Claude Code automatically discovers skills in .claude/    │
│     • No runtime matching or injection needed                    │
│                                                                  │
└─────────────────────────────────────────────────────────────────┘
```

## Hook Integration

### Session Start Hook

On `session_start`, inject relevant memories:

```python
def on_session_start(session: Session, project: Project) -> HookResponse:
    # Query relevant memories for this project
    memories = memory_manager.recall(
        project_id=project.id,
        limit=10,
        min_importance=0.3
    )

    # Build context injection (memories only)
    # Skills are provided via Claude Code native format (.claude/skills/<name>/)
    context = build_memory_context(memories)

    return HookResponse(
        action="continue",
        inject_context=context
    )
```

### Session End Hook

On `session_end`, extract memories from the session:

```python
def on_session_end(session: Session) -> None:
    # Get session summary (already generated)
    summary = session.summary

    # Extract potential memories via LLM
    memories = extract_memories_from_summary(summary)

    for memory in memories:
        memory_manager.create(
            content=memory.content,
            memory_type=memory.type,
            source_type="session",
            source_session_id=session.id,
            importance=memory.importance
        )
```

## MCP Tools

### Memory Management

```python
@mcp.tool()
def remember(
    content: str,
    memory_type: str = "fact",
    importance: float = 0.7,
    tags: list[str] | None = None,
    global_: bool = False,
) -> dict:
    """
    Store a memory for future sessions.

    Use global_=True for memories that apply across all projects.
    """

@mcp.tool()
def recall(
    query: str | None = None,
    memory_type: str | None = None,
    limit: int = 10,
    include_global: bool = True,
) -> dict:
    """
    Retrieve relevant memories.

    If query provided, uses semantic search.
    Otherwise returns most important memories.
    """

@mcp.tool()
def forget(memory_id: str) -> dict:
    """Remove a specific memory."""

@mcp.tool()
def list_memories(
    memory_type: str | None = None,
    min_importance: float = 0.0,
    limit: int = 50,
) -> dict:
    """List all memories with optional filtering."""

@mcp.tool()
def update_memory(
    memory_id: str,
    content: str | None = None,
    importance: float | None = None,
    tags: list[str] | None = None,
) -> dict:
    """Update an existing memory."""
```

### Skill Management

```python
@mcp.tool()
def learn_skill(
    name: str,
    instructions: str | None = None,
    from_session: bool = True,
) -> dict:
    """
    Learn a new skill.

    If from_session=True, extracts from current session trajectory.
    Otherwise, uses provided instructions.
    """

@mcp.tool()
def get_skill(skill_id: str) -> dict:
    """Get skill details."""

@mcp.tool()
def list_skills(
    query: str | None = None,
    limit: int = 20,
) -> dict:
    """List skills, optionally filtered by query match."""

@mcp.tool()
def apply_skill(skill_id: str) -> dict:
    """
    Apply a skill to the current context.

    Returns the skill instructions and marks it as used.
    """

@mcp.tool()
def update_skill(
    skill_id: str,
    name: str | None = None,
    instructions: str | None = None,
    trigger_pattern: str | None = None,
) -> dict:
    """Update an existing skill."""

@mcp.tool()
def delete_skill(skill_id: str) -> dict:
    """Delete a skill."""
```

### Memory Initialization

```python
@mcp.tool()
def init_memory(
    scan_codebase: bool = True,
    import_claude_md: bool = True,
) -> dict:
    """
    Initialize memory system for a project.

    - scan_codebase: Analyze project structure and create initial memories
    - import_claude_md: Parse CLAUDE.md for existing instructions/preferences
    """
```

## CLI Commands

```bash
# Memory management
gobby memory list [--type TYPE] [--min-importance N]
gobby memory show MEMORY_ID
gobby memory add "content" [--type TYPE] [--importance N] [--global]
gobby memory update MEMORY_ID [--content C] [--importance N]
gobby memory delete MEMORY_ID
gobby memory search "query" [--limit N]

# Skill management
gobby skill list [--query Q]
gobby skill show SKILL_ID
gobby skill add NAME --instructions FILE
gobby skill learn NAME [--from-session SESSION_ID]
gobby skill update SKILL_ID [--name N] [--instructions FILE]
gobby skill delete SKILL_ID
gobby skill export [--output DIR]

# Initialization
gobby memory init [--scan] [--import-claude-md]

# Sync
gobby memory sync [--import] [--export]
gobby memory config --stealth [on|off]

# Stats
gobby memory stats
gobby skill stats
```

## Git Sync Architecture

### File Structure

```text
.gobby/
├── memories.jsonl        # Memory records (optional, stealth mode disables)
├── memory_meta.json      # Sync metadata


.claude/
└── skills/               # Skill files (Claude Code native format)
    ├── run-tests/
    │   └── SKILL.md
    ├── deploy/
    │   └── SKILL.md
    └── debug-api/
        └── SKILL.md
```

### Skill File Format

Skills can also be stored as markdown files for easy editing:

```markdown
<!-- .claude/skills/run-tests/SKILL.md -->
---
id: sk-a1b2c3
name: run-tests
trigger_pattern: test|pytest|testing|verify
tags: [testing, pytest, development]
---

# Running Tests

This project uses pytest with specific configuration:

1. Run all tests: `uv run pytest`
2. Run single file: `uv run pytest tests/test_example.py -v`
3. Fixtures are in `tests/conftest.py`
4. Use `-m 'not slow'` to skip slow tests
```

## Context Injection Format

When memories are injected at session start:

```markdown
<project-memory>
## Project Context

This is a CLI tool for managing Docker containers.

## Preferences

- Prefer functional components over class components
- Always use uv for Python dependencies
- Run tests before committing

## Patterns

- API routes follow /api/v1/{resource} pattern
- All database models inherit from BaseModel

## Facts

- Uses pytest with conftest.py fixtures
- Database is PostgreSQL with SQLAlchemy ORM

</project-memory>
```

**Note:** Skills are no longer injected via `<project-memory>`. Instead, skills are exported to `.claude/skills/<name>/` in Claude Code native format, making them automatically available to Claude Code sessions without runtime injection.

## Implementation Checklist

### Phase 1: Storage Layer

- [x] Create database migration for memories table
- [x] Create database migration for skills table
- [x] Create database migration for session_memories table
- [x] Implement ID generation utility (mm-{hash}, sk-{hash})
- [x] Create `src/storage/memories.py` with `LocalMemoryManager` class
- [x] Implement `create()`, `get()`, `update()`, `delete()` methods
- [x] Implement `list()` method with filters
- [x] Implement `search()` method (text-based initially)
- [x] Create `src/storage/skills.py` with `LocalSkillManager` class
- [x] Implement skill CRUD methods
- [x] Add unit tests for storage layer

### Phase 2: Memory Operations

- [x] Create `src/memory/manager.py` with `MemoryManager` class
- [x] Implement `remember()` method
- [x] Implement `recall()` method with importance ranking
- [x] Implement `forget()` method
- [x] Implement memory importance decay (background job)
- [ ] Add access tracking (update access_count, last_accessed_at)
      - **Stub location:** `src/gobby/memory/manager.py:_update_access_stats()` (line ~105-111)
      - **Rationale:** Deferred as low priority; schema supports it but perf tuning needed (batch vs sync updates)
      - **Follow-up:** See Phase 10 TODO below
- [x] Add unit tests for memory operations

### Phase 3: Skill Learning

- [x] Create `src/memory/skills.py` with `SkillLearner` class
- [x] Implement `learn_from_session()` method
- [x] Implement skill extraction prompt template
- [x] ~~Implement `match_skills()` method (trigger pattern matching)~~ **REMOVED** - Skills now use Claude Code plugin format
- [x] Implement skill usage tracking
- [x] Add unit tests for skill learning

### Phase 4: Hook/Workflow Integration

Note: Memory injection/extraction should be done via workflow actions, not hardcoded in hook_manager.

- [x] Create memory context builder (`src/memory/context.py`)
- [x] Implement selective injection (relevance threshold via min_importance)
- [x] Add `memory_inject` workflow action
- [x] Add `memory_sync_import` workflow action
- [x] Add `memory_sync_export` workflow action
- [x] Add `skills_learn` workflow action
- [x] Add unit tests for workflow memory actions
- [x] Create example workflow using memory injection at session_start
      - See `src/gobby/templates/workflows/memory-lifecycle.yaml` (uses `memory_inject` action)
      - See `src/gobby/templates/workflows/memory-sync.yaml` (uses `memory_sync_import` action)
- [x] Create example workflow using memory extraction at session_end
      - See `src/gobby/templates/workflows/memory-lifecycle.yaml` (uses `skills_learn` action)
      - See `src/gobby/templates/workflows/memory-sync.yaml` (uses `memory_sync_export` action)

### Phase 5-6: MCP Tools & CLI Commands (Unified)

MCP tools and CLI commands should have parity. Each operation is implemented in both interfaces.

**Status Legend:**
- `MCP+CLI` = Both MCP tool and CLI command implemented
- `MCP only` = MCP tool implemented, CLI pending
- `CLI only` = CLI command implemented, MCP pending
- `TODO` = Neither implemented yet

#### Memory Operations

| Operation | MCP Tool | CLI Command | Status | Notes |
|-----------|----------|-------------|--------|-------|
| Create | `remember` | `gobby memory remember` | MCP+CLI | |
| Retrieve/Search | `recall` | `gobby memory recall` | MCP+CLI | |
| Delete | `forget` | `gobby memory forget` | MCP+CLI | |
| List all | `list_memories` | `gobby memory list` | MCP+CLI | |
| Show one | `get_memory` | `gobby memory show` | MCP+CLI | |
| Update | `update_memory` | `gobby memory update` | MCP+CLI | Mutable: content, importance, tags |
| Initialize | `init_memory` | `gobby memory init` | TODO | Blocked by Phase 9 (MemoryExtractor) |
| Stats | `memory_stats` | `gobby memory stats` | MCP+CLI | |

#### Skill Operations

| Operation | MCP Tool | CLI Command | Status | Notes |
|-----------|----------|-------------|--------|-------|
| Learn from session | `learn_skill` | `gobby skill learn` | MCP+CLI | |
| List | `list_skills` | `gobby skill list` | MCP+CLI | |
| Show/Get | `get_skill` | `gobby skill get` | MCP+CLI | |
| Delete | `delete_skill` | `gobby skill delete` | MCP+CLI | |
| Create directly | `create_skill` | `gobby skill add` | MCP+CLI | |
| Update | `update_skill` | `gobby skill update` | MCP+CLI | Supports name, instructions, trigger, tags |
| Apply/Use | `apply_skill` | `gobby skill apply` | MCP+CLI | Returns instructions, increments usage |
| Export to files | `export_skills` | `gobby skill export` | MCP+CLI | Exports to .claude/skills/ as markdown |

#### Checklist

**Done:**
- [x] Add `gobby memory` command group
- [x] Add `gobby skill` command group
- [x] Add `remember` MCP tool + CLI command
- [x] Add `recall` MCP tool + CLI command
- [x] Add `forget` MCP tool + CLI command
- [x] Add `learn_skill` MCP tool + `skill learn` CLI command
- [x] Add `list_skills` MCP tool + `skill list` CLI command
- [x] Add `get_skill` MCP tool + `skill get` CLI command
- [x] Add `delete_skill` MCP tool + `skill delete` CLI command
- [x] ~~Add `match_skills` MCP tool (MCP-only, used by workflows)~~ **REMOVED** - Skills now use Claude Code plugin format

- [x] Add `list_memories` MCP tool + `memory list` CLI command
- [x] Add `get_memory` MCP tool + `memory show` CLI command
- [x] Add `update_memory` MCP tool + `memory update` CLI command
- [x] Add `memory_stats` MCP tool + `memory stats` CLI command
- [x] Add `create_skill` MCP tool + `skill add` CLI command
- [x] Add `update_skill` MCP tool + `skill update` CLI command
- [x] Add `apply_skill` MCP tool + `skill apply` CLI command
- [x] Add `export_skills` MCP tool + `skill export` CLI command
- [x] Update MCP tool documentation in CLAUDE.md

**TODO (blocked):**
- [ ] Add `init_memory` MCP tool + `memory init` CLI command (blocked by Phase 9: MemoryExtractor)

### Phase 7: Git Sync

- [x] Create `src/sync/memories.py` with `MemorySyncManager` class
- [x] Implement JSONL serialization for memories
- [x] Implement markdown serialization for skills
- [x] Implement `export_to_files()` method
- [x] Implement `import_from_files()` method
- [x] Implement skill file read/write
- [x] Add stealth mode support
- [x] Add debounced sync trigger after memory mutations
- [x] Add unit tests for sync functionality

### Phase 8: Semantic Search (Enhancement)

- [ ] Add embedding generation using configured LLM
- [ ] Implement vector similarity search
- [ ] Create embedding cache for performance
- [ ] Add `rebuild_embeddings` maintenance command
- [ ] Benchmark semantic vs text search

### Phase 9: Auto-Memory Extraction

- [ ] Create `src/memory/extractor.py` with `MemoryExtractor` class
- [ ] Implement extraction from session summaries
- [ ] Implement extraction from CLAUDE.md files
- [ ] Implement codebase scanning for patterns
- [ ] Add deduplication logic
- [ ] Add unit tests for extraction

### Phase 10: Documentation & Polish

- [ ] Add memory section to README
- [ ] Create `docs/memory.md` with usage guide
- [ ] Add example workflows for memory usage
- [ ] Add memory configuration options to `config.yaml`
- [ ] Performance testing with 1000+ memories
- [ ] Document cross-CLI memory sharing
- [ ] Implement access tracking in `MemoryManager._update_access_stats()`
      - **Gating:** Implement after semantic search (Phase 8) to batch with embedding updates
      - **Scope:** Update `access_count` and `last_accessed_at` on recall; consider debouncing for perf

## Configuration

```yaml
# ~/.gobby/config.yaml additions

memory:
  enabled: true
  auto_extract: true              # Extract memories from sessions
  injection_limit: 10             # Max memories to inject per session
  importance_threshold: 0.3       # Min importance for injection
  decay_enabled: true             # Enable importance decay
  decay_rate: 0.05                # Importance decay per month
  decay_floor: 0.1                # Minimum importance after decay

skills:
  enabled: true
  learning_model: claude-haiku-4-5
  # Skills are exported to .claude/skills/<name>/ in Claude Code native format

memory_sync:
  enabled: true
  stealth: false                  # If true, store in ~/.gobby instead of .gobby
  export_debounce: 5              # Seconds to wait before export
```

## Workflow Integration

Memory and skills integrate with the workflow engine:

### Memory Actions

```yaml
# In workflow definition
actions:
  - type: inject_memories
    query: "{user_prompt}"
    limit: 5

  - type: save_memory
    content: "{artifact.plan}"
    memory_type: context
    importance: 0.8
```

### Skill-Based Workflows

```yaml
# Workflow that learns skills from sessions
name: skill-learning
triggers:
  - event: session_end
    actions:
      - type: skills_learn
        # Skills are automatically available via Claude Code plugin format
        # after export to .claude/skills/<name>/
```

**Note:** Skills no longer need runtime injection via workflows. Once exported to `.claude/skills/<name>/`, they are automatically available to Claude Code as project skills.

## Decisions

| # | Question | Decision | Rationale |
|---|----------|----------|-----------|
| 1 | **Memory scope** | Project + global | Most memories are project-specific, but some (preferences) are global |
| 2 | **Skill storage** | DB + markdown files | DB for querying, markdown for git sync and easy editing |
| 3 | **Injection timing** | Session start hook | Early injection ensures LLM has context from the beginning |
| 4 | **Decay model** | Time-based with access boost | Unused memories fade, frequently accessed ones stay important |
| 5 | **Embedding storage** | PostgreSQL BLOB | Simple, no external dependencies |
| 6 | **Skill delivery** | Claude Code native format | Skills exported to `.claude/skills/<name>/` are automatically available to Claude Code without runtime injection. Removed `match_skills` method/tool and `auto_suggest`/`max_suggestions` config. Memories still use runtime injection via `<project-memory>` tags. |

## Future Enhancements

- **Memory graphs**: Link related memories for better context
- **Shared memories**: Team-level memories via platform sync
- **Memory conflicts**: Handle conflicting memories from different sessions
- **Skill versioning**: Track skill evolution over time
- **Skill sharing**: Export/import skills between projects
- **Memory visualization**: Web dashboard for memory exploration
- **LLM-powered consolidation**: Merge similar memories automatically
