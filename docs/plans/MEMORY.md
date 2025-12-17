# Persistent Memory & Skills System

## Overview

A memory-first system that transforms Gobby from tracking sessions to maintaining **persistent agent memory** across sessions. Unlike session-based assistants where each conversation starts fresh, this system creates continuity—the agent learns, remembers, and improves over time.

**Inspiration:** [Letta Code](https://github.com/letta-ai/letta-code) - Memory-first coding agent with persistent learning and skill acquisition.

## Vision

> "From meeting a new contractor each session to having a coworker that remembers and learns."

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
│     • SQLite (local): ~/.gobby/gobby.db                         │
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
│  4. Skill saved to database and optionally .gobby/skills/       │
│                                                                  │
│  5. Future sessions:                                             │
│     • Match trigger pattern against user prompt                  │
│     • Inject skill instructions when relevant                    │
│                                                                  │
└─────────────────────────────────────────────────────────────────┘
```

## Hook Integration

### Session Start Hook

On `session_start`, inject relevant context:

```python
def on_session_start(session: Session, project: Project) -> HookResponse:
    # Query relevant memories for this project
    memories = memory_manager.recall(
        project_id=project.id,
        limit=10,
        min_importance=0.3
    )

    # Find skills matching the initial prompt (if available)
    skills = skill_manager.match_skills(
        project_id=project.id,
        query=session.initial_prompt
    )

    # Build context injection
    context = build_memory_context(memories, skills)

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
├── skills/               # Skill files (markdown format)
│   ├── run-tests.md
│   ├── deploy.md
│   └── debug-api.md
├── memory_meta.json      # Sync metadata
└── gobby.db              # SQLite cache (not committed)
```

### Skill File Format

Skills can also be stored as markdown files for easy editing:

```markdown
<!-- .gobby/skills/run-tests.md -->
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

When memories/skills are injected at session start:

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

## Relevant Skills

### run-tests
This project uses pytest with specific configuration:
1. Run all tests: `uv run pytest`
2. Run single file: `uv run pytest tests/test_example.py -v`

</project-memory>
```

## Implementation Checklist

### Phase 1: Storage Layer

- [ ] Create database migration for memories table
- [ ] Create database migration for skills table
- [ ] Create database migration for session_memories table
- [ ] Implement ID generation utility (mm-{hash}, sk-{hash})
- [ ] Create `src/storage/memories.py` with `LocalMemoryManager` class
- [ ] Implement `create()`, `get()`, `update()`, `delete()` methods
- [ ] Implement `list()` method with filters
- [ ] Implement `search()` method (text-based initially)
- [ ] Create `src/storage/skills.py` with `LocalSkillManager` class
- [ ] Implement skill CRUD methods
- [ ] Add unit tests for storage layer

### Phase 2: Memory Operations

- [ ] Create `src/memory/manager.py` with `MemoryManager` class
- [ ] Implement `remember()` method
- [ ] Implement `recall()` method with importance ranking
- [ ] Implement `forget()` method
- [ ] Implement memory importance decay (background job)
- [ ] Add access tracking (update access_count, last_accessed_at)
- [ ] Add unit tests for memory operations

### Phase 3: Skill Learning

- [ ] Create `src/memory/skills.py` with `SkillLearner` class
- [ ] Implement `learn_from_session()` method
- [ ] Implement skill extraction prompt template
- [ ] Implement `match_skills()` method (trigger pattern matching)
- [ ] Implement skill usage tracking
- [ ] Add unit tests for skill learning

### Phase 4: Hook Integration

- [ ] Update `session_start` hook to inject memories
- [ ] Update `session_end` hook to extract memories
- [ ] Create memory context builder
- [ ] Implement selective injection (relevance threshold)
- [ ] Add memory injection to workflow actions
- [ ] Add unit tests for hook integration

### Phase 5: MCP Tools

- [ ] Add `remember` tool to MCP server
- [ ] Add `recall` tool to MCP server
- [ ] Add `forget` tool to MCP server
- [ ] Add `list_memories` tool to MCP server
- [ ] Add `update_memory` tool to MCP server
- [ ] Add `learn_skill` tool to MCP server
- [ ] Add `get_skill` tool to MCP server
- [ ] Add `list_skills` tool to MCP server
- [ ] Add `apply_skill` tool to MCP server
- [ ] Add `update_skill` tool to MCP server
- [ ] Add `delete_skill` tool to MCP server
- [ ] Add `init_memory` tool to MCP server
- [ ] Update MCP tool documentation

### Phase 6: CLI Commands

- [ ] Add `gobby memory` command group
- [ ] Implement `gobby memory list` command
- [ ] Implement `gobby memory show` command
- [ ] Implement `gobby memory add` command
- [ ] Implement `gobby memory update` command
- [ ] Implement `gobby memory delete` command
- [ ] Implement `gobby memory search` command
- [ ] Add `gobby skill` command group
- [ ] Implement `gobby skill list` command
- [ ] Implement `gobby skill show` command
- [ ] Implement `gobby skill add` command
- [ ] Implement `gobby skill learn` command
- [ ] Implement `gobby skill update` command
- [ ] Implement `gobby skill delete` command
- [ ] Implement `gobby skill export` command
- [ ] Implement `gobby memory init` command
- [ ] Implement `gobby memory stats` command
- [ ] Add CLI help text and examples

### Phase 7: Git Sync

- [ ] Create `src/sync/memories.py` with `MemorySyncManager` class
- [ ] Implement JSONL serialization for memories
- [ ] Implement markdown serialization for skills
- [ ] Implement `export_to_jsonl()` method
- [ ] Implement `import_from_jsonl()` method
- [ ] Implement skill file read/write
- [ ] Add stealth mode support
- [ ] Add sync trigger after memory mutations
- [ ] Add unit tests for sync functionality

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
  auto_suggest: true              # Suggest skills matching prompts
  max_suggestions: 3              # Max skills to suggest
  learning_model: claude-haiku-4-5

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
# Workflow that uses skills
name: skill-assisted-development
triggers:
  - event: session_start
    actions:
      - type: match_skills
        inject: true

  - event: session_end
    actions:
      - type: suggest_skill_learning
        if: session.tool_count > 10
```

## Decisions

| # | Question | Decision | Rationale |
|---|----------|----------|-----------|
| 1 | **Memory scope** | Project + global | Most memories are project-specific, but some (preferences) are global |
| 2 | **Skill storage** | DB + markdown files | DB for querying, markdown for git sync and easy editing |
| 3 | **Injection timing** | Session start hook | Early injection ensures LLM has context from the beginning |
| 4 | **Decay model** | Time-based with access boost | Unused memories fade, frequently accessed ones stay important |
| 5 | **Embedding storage** | SQLite BLOB | Simple, no external dependencies |

## Future Enhancements

- **Memory graphs**: Link related memories for better context
- **Shared memories**: Team-level memories via platform sync
- **Memory conflicts**: Handle conflicting memories from different sessions
- **Skill versioning**: Track skill evolution over time
- **Skill sharing**: Export/import skills between projects
- **Memory visualization**: Web dashboard for memory exploration
- **LLM-powered consolidation**: Merge similar memories automatically
