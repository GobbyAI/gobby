---
title: 'gobby-skills: SkillPort-compatible Skill Management'
slug: 'gobby-skills'
created: '2026-01-14'
updated: '2026-01-22'
status: 'completed'
stepsCompleted: ['phase-1-storage', 'phase-2-search', 'phase-3-loader', 'phase-4-mcp', 'phase-5-cli', 'phase-6-hooks', 'phase-7-docs']
files_to_modify:
  # Storage layer
  - src/gobby/storage/skills.py
  - src/gobby/storage/migrations.py
  # Skills module
  - src/gobby/skills/__init__.py
  - src/gobby/skills/parser.py
  - src/gobby/skills/validator.py
  - src/gobby/skills/search.py
  - src/gobby/skills/embeddings.py
  - src/gobby/skills/loader.py
  - src/gobby/skills/updater.py
  - src/gobby/skills/manager.py
  # MCP tools (separate models + registry)
  - src/gobby/mcp_proxy/tools/skills/__init__.py
  - src/gobby/mcp_proxy/tools/skills/models.py
  - src/gobby/mcp_proxy/tools/skills/registry.py
  - src/gobby/mcp_proxy/registries.py
  # CLI
  - src/gobby/cli/skills.py
  - src/gobby/cli/__init__.py
  # Hook integration
  - src/gobby/hooks/skill_manager.py
  - src/gobby/hooks/skill_injection.py
  - src/gobby/hooks/context_actions.py
  - src/gobby/adapters/claude_code.py
  # Config
  - src/gobby/config/app.py
  - src/gobby/config/features.py
  # Documentation
  - CLAUDE.md
  - docs/guides/skills.md
  - docs/guides/skill-files.md
code_patterns:
  - InternalToolRegistry pattern from src/gobby/mcp_proxy/tools/internal.py
  - TF-IDF search from src/gobby/memory/search/tfidf.py
  - YAML frontmatter parsing (similar to prompts/loader.py)
  - Storage CRUD from src/gobby/storage/memories.py
  - CLI command group from src/gobby/cli/memory.py
---

## Overview

### Problem Statement

AI agents benefit from structured instructions ("skills") for common tasks like writing commit messages, reviewing PRs, or following coding conventions. Currently, there's no way to store, discover, or inject these skills into Gobby sessions.

SkillPort (github.com/gotalab/skillport) solves this for other AI tools. Gobby should provide **full SkillPort feature parity** plus Gobby-specific advantages, following the Agent Skills specification (agentskills.io).

### Solution

Add a `gobby-skills` internal MCP server with:

1. Full Agent Skills spec compliance (SKILL.md format)
2. All SkillPort CLI commands (list, show, install, remove, update, validate, meta, init, doc, new)
3. All SkillPort MCP tools (list_skills, get_skill, search_skills, install_skill, etc.)
4. TF-IDF search + optional embedding support (like SkillPort)
5. Gobby-specific advantages (project-scoping, hook integration, workflow binding)

### Scope

**In Scope (Full SkillPort Parity):**

- SKILL.md file format with YAML frontmatter (Agent Skills spec)
- PostgreSQL storage for skill metadata
- `gobby-skills` MCP registry with all SkillPort tools
- Import from GitHub repos, local paths, ZIP archives
- TF-IDF search + optional embeddings (multi-provider)
- Category and tag filtering
- Core skills (`alwaysApply` flag)
- Skill update from source
- Full CLI parity: list, show, install, remove, update, validate, meta get/set/unset, init, doc, new
- `--json` output for CI pipelines
- Spec validation

**Gobby Advantages (Beyond SkillPort):**

- Project-scoped skills (vs SkillPort's env var filtering)
- Enable/disable per-skill toggle
- Hook integration (auto-inject core skills at session start)
- Task system integration
- Workflow binding (skills as step requirements)

**Out of Scope (Not in SkillPort either):**

- Skill versioning history/changelog tracking
- Skill marketplace/central registry
- Script execution sandboxing (beyond allowed_commands)
- Skill dependencies (skill A requires skill B)

---

## SkillPort Feature Parity Checklist

| SkillPort Feature | gobby-skills Equivalent | Phase |
|-------------------|------------------------|-------|
| `skillport add <url>` | `gobby skills install <source>` | 5 |
| `skillport update [--all]` | `gobby skills update [name] [--all]` | 5 |
| `skillport list` | `gobby skills list [--category] [--tags] [--json]` | 5 |
| `skillport remove <id>` | `gobby skills remove <name>` | 5 |
| `skillport validate [path]` | `gobby skills validate [path] [--json]` | 5 |
| `skillport meta get/set/unset` | `gobby skills meta get/set/unset` | 5 |
| `skillport init` | `gobby skills init` | 5 |
| `skillport doc` | `gobby skills doc` | 5 |
| `skillport show <id>` | `gobby skills show <name>` | 5 |
| MCP `search_skills(query)` | `search_skills(query, category, tags)` | 4 |
| MCP `load_skill(skill_id)` | `get_skill(name)` | 4 |
| Progressive disclosure | Same (~100 tokens metadata) | 4 |
| GitHub import | GitHub import (all URL formats) | 3 |
| Local directory import | Local directory import | 3 |
| ZIP archive import | ZIP archive import | 3 |
| Category filtering | Category filtering | 2 |
| Tag filtering | Tag filtering (tags_any, tags_all) | 2 |
| `alwaysApply` flag | Core skills + auto-injection | 2, 6 |
| BM25/Tantivy search | TF-IDF search | 2 |
| Optional embeddings (OpenAI) | Optional embeddings (multi-provider) | 2 |
| Validation against spec | Full spec validation | 1 |
| CI-friendly JSON output | `--json` flag | 5 |
| Auto-reindex on changes | Change listeners + reindex | 2 |
| scripts/ directory | scripts/ directory support | 3 |
| references/ directory | references/ directory support | 3 |
| assets/ directory | assets/ directory support | 3 |
| Template scaffolding | `gobby skills new <name>` | 5 |

---

## Architecture

### Component Diagram

```text
┌─────────────────────────────────────────────────────────────────┐
│                        MCP Layer (stdio)                        │
│  gobby-skills: list_skills, get_skill, search_skills,           │
│                install_skill, remove_skill, update_skill        │
└─────────────────────────────────┬───────────────────────────────┘
                                  │
┌─────────────────────────────────▼───────────────────────────────┐
│                        SkillManager                              │
│  - load_skill(path) → Skill                                      │
│  - search(query) → list[SkillMetadata]                           │
│  - get_content(skill_id) → str                                   │
│  - update_skill(name) → Skill                                    │
│  - list_core_skills() → list[Skill]                              │
└─────────────────────────────────┬───────────────────────────────┘
                                  │
    ┌───────────────┬─────────────┼─────────────┬──────────────┐
    │               │             │             │              │
┌───▼─────────┐ ┌───▼─────────┐ ┌─▼───────────┐ ┌▼────────────┐ ┌▼──────────┐
│ LocalSkill  │ │ SkillLoader │ │ SkillSearch │ │ SkillUpdate │ │ Validator │
│ Manager     │ │ (parse/load)│ │ (TF-IDF +   │ │ r (refresh) │ │ (spec     │
│ (PostgreSQL)    │ │             │ │ embeddings) │ │             │ │ checks)   │
└─────────────┘ └─────────────┘ └─────────────┘ └─────────────┘ └───────────┘
```

### Key Abstractions

**Skill dataclass** (`src/gobby/storage/skills.py`):

```python
@dataclass
class Skill:
    # Identity
    id: str                          # Prefixed ID (skl-...)
    name: str                        # Required, max 64 chars, lowercase+hyphens

    # Agent Skills Spec Fields
    description: str                 # Required, max 1024 chars
    version: str | None              # Optional (from metadata.version)
    license: str | None              # Optional
    compatibility: str | None        # Optional, max 500 chars
    allowed_tools: list[str] | None  # Optional (for tool pre-approval)
    metadata: dict[str, Any] | None  # Free-form extensions
    content: str                     # Full markdown body

    # Source Tracking
    source_path: str                 # File path or URL
    source_type: str                 # 'local', 'github', 'url', 'zip'
    source_ref: str | None           # Git ref (branch/tag/commit) for updates

    # Gobby-specific
    enabled: bool                    # Toggle without removing
    project_id: str | None           # NULL = global, else project-scoped

    # Timestamps
    created_at: str
    updated_at: str
```

**Metadata Structure** (SkillPort-compatible extensions in `metadata.skillport`):

```yaml
---
name: commit-message
description: Generate conventional commit messages following Angular conventions
license: MIT
compatibility: Requires git CLI
metadata:
  author: anthropic
  version: "1.0"
  skillport:                    # SkillPort extensions namespace
    category: git
    tags: [git, commits, workflow]
    alwaysApply: false
  gobby:                        # Gobby extensions namespace
    triggers: ["/commit"]
    workflow_hint: "git-workflow"
allowed-tools: Bash(git:*)
---

# Commit Message Generator

## Instructions
When generating commit messages, follow these conventions...
```

---

## Implementation Patterns

This section documents key architectural patterns discovered during task expansion.

### Change Event System

Skill mutations fire change events to enable downstream reactions (search reindex, cache invalidation):

```python
class ChangeEventType(Enum):
    CREATE = "create"
    UPDATE = "update"
    DELETE = "delete"

@dataclass
class ChangeEvent:
    event_type: ChangeEventType
    skill_id: str
    skill_name: str
    timestamp: str
    metadata: dict[str, Any] | None = None
```

**SkillChangeNotifier** manages listener registration and broadcasts:

```python
class SkillChangeNotifier:
    def register_listener(self, callback: Callable[[ChangeEvent], None]) -> None: ...
    def unregister_listener(self, callback: Callable[[ChangeEvent], None]) -> None: ...
    def _fire_change(self, event: ChangeEvent) -> None:
        """Fire with try/except wrapping to prevent one failing listener from blocking others."""
        ...
```

LocalSkillManager accepts an optional `notifier` in `__init__` and calls `_fire_change()` on mutations.

### Project-Scoped Uniqueness

**Key Gobby advantage over SkillPort**: Name uniqueness is enforced per-project, not globally.

```sql
-- PostgreSQL constraint
UNIQUE(name, project_id)
```

This allows:
- Same skill name in different projects (project-specific customizations)
- Global skills (project_id=NULL) with unique names
- Project skills can shadow global skills

### Protocol-Based Design

Use Protocol classes for extensibility without tight coupling:

```python
class SkillSearchProtocol(Protocol):
    """Search backend abstraction."""
    def search(self, query: str, filters: SearchFilters) -> list[SkillSearchResult]: ...
    def reindex(self, skills: list[Skill]) -> None: ...

class EmbeddingProvider(Protocol):
    """Embedding provider abstraction."""
    def embed(self, text: str) -> list[float]: ...
    def embed_batch(self, texts: list[str]) -> list[list[float]]: ...
    @property
    def dimension(self) -> int: ...

class SkillChangeListener(Protocol):
    """Listener for skill mutations."""
    def on_skill_change(self, event: ChangeEvent) -> None: ...
```

### Graceful Degradation

All optional features degrade gracefully:

- **Embeddings**: Fall back to TF-IDF if no provider configured or embed fails
- **Missing files**: Log warning and skip, don't fail entire load
- **Invalid skills**: Return validation errors, don't raise exceptions
- **Network failures**: Cache GitHub clones, retry with backoff

### Metadata Persistence

Track installed skill metadata separately from skill content:

```python
@dataclass
class SkillMetadata:
    skill_name: str
    source: SkillSource
    source_url: str | None
    source_ref: str | None  # Git ref for updates
    install_path: str
    installed_at: str
    updated_at: str | None
    update_available: bool = False
```

Store as JSON in `.metadata/skills_metadata.json` or per-skill `.metadata/{name}.json`.

### Backup on Update

Transaction-like behavior for skill updates:

1. Backup existing skill content to temp location
2. Attempt update from source
3. On failure: restore from backup, log error
4. On success: delete backup

```python
def update_skill(self, name: str) -> UpdateResult:
    backup_path = self._backup_skill(name)
    try:
        new_skill = self._fetch_from_source(name)
        self._validate_and_store(new_skill)
        return UpdateResult(success=True, skill=new_skill)
    except Exception as e:
        self._restore_from_backup(name, backup_path)
        return UpdateResult(success=False, error=str(e))
    finally:
        self._cleanup_backup(backup_path)
```

---

## Phase 1: Storage Layer + Parser + Validation

**Goal**: Skill storage with full Agent Skills spec validation.

**Files:**

- `src/gobby/storage/skills.py` - Skill dataclass + LocalSkillManager
- `src/gobby/skills/__init__.py` - Module init
- `src/gobby/skills/parser.py` - YAML frontmatter parser
- `src/gobby/skills/validator.py` - Agent Skills spec validation
- `src/gobby/storage/migrations.py` - Add skills table (v67)

**Tasks:**

- [ ] Create Skill dataclass with all spec fields + Gobby extensions (category: code)
- [ ] Create `validate_skill_name()` per spec: max 64, lowercase+hyphens, no --/leading/trailing - (category: code)
- [ ] Create `validate_skill_description()`: max 1024 chars, non-empty (category: code)
- [ ] Create `validate_skill_compatibility()`: max 500 chars (category: code)
- [ ] Create YAML frontmatter parser for SKILL.md files (category: code)
- [ ] Create `SkillValidator` class with full spec validation (category: code)
- [ ] Create LocalSkillManager with CRUD methods (category: code)
- [ ] Add change listener pattern to LocalSkillManager (category: code)
- [ ] Add skills table migration (v67) (category: config)

**Acceptance Criteria:**

- [ ] Can create, read, update, delete skills in PostgreSQL
- [ ] Name validation rejects: uppercase, leading/trailing hyphens, consecutive hyphens, >64 chars
- [ ] Description validation rejects: empty, >1024 chars
- [ ] Parser extracts all frontmatter fields correctly
- [ ] Change listeners fire on mutations

**Implementation Details:**

Change Listener Pattern:
```python
# LocalSkillManager implementation
class LocalSkillManager:
    def __init__(self, db: HubDatabase, notifier: SkillChangeNotifier | None = None):
        self.db = db
        self._notifier = notifier

    def create(self, skill: Skill) -> Skill:
        # ... create logic ...
        if self._notifier:
            self._notifier._fire_change(ChangeEvent(
                event_type=ChangeEventType.CREATE,
                skill_id=skill.id,
                skill_name=skill.name,
                timestamp=datetime.utcnow().isoformat()
            ))
        return skill
```

Extended Metadata Validation:
- `tags`: Validate is list of strings (not nested, not None items)
- `version`: Validate follows semver pattern (`\d+\.\d+(\.\d+)?`)
- `author`: Required for official/verified skills (optional otherwise)
- `category`: Must be lowercase, alphanumeric + hyphens

---

## Phase 2: Search Integration

**Goal**: TF-IDF search with category/tag filtering + optional embeddings.

**Files:**

- `src/gobby/skills/search.py` - SkillSearch class
- `src/gobby/skills/embeddings.py` - Optional embedding provider support
- `src/gobby/skills/manager.py` - SkillManager coordinator

**Tasks:**

- [ ] Create SkillSearch adapting TF-IDF from memory system (category: code)
- [ ] Implement search on name + description + tags + category (category: code)
- [ ] Add category filtering to search (category: code)
- [ ] Add tag filtering (tags_any, tags_all) to search (category: code)
- [ ] Create optional embedding provider abstraction (category: code)
- [ ] Implement OpenAI embedding provider (via existing LLM abstraction) (category: code)
- [ ] Add hybrid search: TF-IDF + optional embedding similarity (category: code)
- [ ] Create SkillManager coordinating storage + search + loader (category: code)
- [ ] Wire change listener to trigger search reindex (category: code)
- [ ] Add `list_core_skills()` for alwaysApply=true skills (category: code)

**Acceptance Criteria:**

- [ ] TF-IDF search returns relevant skills (default, no API needed)
- [ ] Category filtering works: `search_skills(query="...", category="git")`
- [ ] Tag filtering works: `search_skills(query="...", tags_any=["git", "workflow"])`
- [ ] Optional embedding search works when configured
- [ ] Core skills (alwaysApply) can be listed separately
- [ ] Changes trigger search index rebuild

**Implementation Details:**

SearchConfig Location (place in `src/gobby/config/features.py`, not storage):
```python
@dataclass
class SearchConfig:
    default_backend: str = "tfidf"  # "tfidf", "embedding", "hybrid"
    embedding_provider: str | None = None  # "openai", "anthropic", "local"
    reindex_on_change: bool = True
    min_score_threshold: float = 0.1
```

SearchFilters Dataclass (separate concern from main search):
```python
@dataclass
class SearchFilters:
    categories: list[str] | None = None
    tags_any: list[str] | None = None  # Match any of these tags
    tags_all: list[str] | None = None  # Match all of these tags
    min_score: float = 0.0  # 0.0-1.0, validated

    def __post_init__(self):
        if not 0.0 <= self.min_score <= 1.0:
            raise ValueError("min_score must be between 0.0 and 1.0")
```

EmbeddingProvider Protocol (already defined in Implementation Patterns):
- Methods: `embed(text)`, `embed_batch(texts)`, `dimension` property
- Graceful fallback if no provider configured or embed fails

Hybrid Search Strategy:
1. Run TF-IDF search to get initial candidates
2. If embedding provider available: compute embedding similarity
3. Combine scores: `final_score = (tfidf_score * 0.4) + (embedding_score * 0.6)`
4. Apply category/tag filters AFTER similarity ranking (not before)
5. Apply `top_k` limit to final filtered results

SkillManager Constructor:
```python
class SkillManager:
    def __init__(
        self,
        storage: LocalSkillManager,
        search: SkillSearch,
        notifier: SkillChangeNotifier | None = None,
    ):
        self._storage = storage
        self._search = search
        if notifier:
            notifier.register_listener(self._on_skill_change)

    def _on_skill_change(self, event: ChangeEvent) -> None:
        """Trigger reindex on skill mutations."""
        if self._search.config.reindex_on_change:
            self._search.reindex_skill(event.skill_id)
```

---

## Phase 3: Skill Loader + Updater

**Goal**: Load skills from filesystem, GitHub, and ZIP archives; support updates.

**Files:**

- `src/gobby/skills/loader.py` - SkillLoader class
- `src/gobby/skills/updater.py` - SkillUpdater class

**Tasks:**

- [ ] Create SkillLoader.load_skill(path) method (category: code)
- [ ] Create SkillLoader.load_directory(path) for batch loading (category: code)
- [ ] Add GitHub import support via httpx (category: code)
- [ ] Support GitHub URL formats: `owner/repo`, full URL, branch/path (category: code)
- [ ] Add ZIP archive import support (category: code)
- [ ] Support skill directory structure (scripts/, references/, assets/) (category: code)
- [ ] Store `source_ref` for GitHub imports (branch/commit) (category: code)
- [ ] Create SkillUpdater.update_skill(name) to refresh from source (category: code)
- [ ] Create SkillUpdater.update_all() for bulk refresh (category: code)
- [ ] Validate directory name matches skill name (category: code)

**Acceptance Criteria:**

- [ ] Can load individual SKILL.md files
- [ ] Can load all skills from a directory
- [ ] Can import from `github:owner/repo` and `github:owner/repo/tree/branch/path`
- [ ] Can import from ZIP archives
- [ ] Handles skill directories with scripts/references/assets
- [ ] Can update skills from original source
- [ ] Validates skill directory name matches frontmatter name

**Implementation Details:**

SkillSource Enum (Pydantic model with validators):
```python
class SkillSource(str, Enum):
    LOCAL = "local"       # Local filesystem path
    GITHUB = "github"     # GitHub repository
    ZIP = "zip"           # ZIP archive
    FILESYSTEM = "filesystem"  # Directory structure

# In SkillMetadata
source: SkillSource = Field(...)

@field_validator("source")
def validate_source(cls, v):
    if isinstance(v, str):
        return SkillSource(v.lower())
    return v
```

UpdateCheckResult (check before actual refresh):
```python
@dataclass
class UpdateCheckResult:
    skill_name: str
    update_available: bool
    local_version: str | None
    remote_version: str | None
    error: str | None = None  # Set if check failed

    @property
    def can_update(self) -> bool:
        return self.update_available and self.error is None
```

GitHub URL Format Support:
```python
# Supported formats:
# - owner/repo                    → default branch, repo root
# - owner/repo#branch             → specific branch, repo root
# - owner/repo/tree/branch/path   → specific branch and path
# - https://github.com/owner/repo → full URL
# - github:owner/repo             → prefixed shorthand

def parse_github_url(url: str) -> GitHubRef:
    """Parse various GitHub URL formats into normalized GitHubRef."""
    ...
```

GitHub Cache Strategy:
```python
CACHE_DIR = Path.home() / ".gobby" / "skill_cache"

def clone_skill_repo(url: str, ref: str | None = None) -> Path:
    """Clone to cache with shallow depth for efficiency."""
    cache_path = CACHE_DIR / _hash_url(url)
    if cache_path.exists():
        # Pull latest if ref matches, otherwise re-clone
        ...
    else:
        cmd = ["git", "clone", "--depth", "1"]
        if ref:
            cmd.extend(["--branch", ref])
        cmd.extend([url, str(cache_path)])
        subprocess.run(cmd, check=True)
    return cache_path
```

ZIP Context Manager (temp directory with cleanup):
```python
@contextmanager
def extract_zip(zip_path: Path) -> Iterator[Path]:
    """Extract ZIP to temp directory, cleanup on exit."""
    temp_dir = Path(tempfile.mkdtemp(prefix="gobby_skill_"))
    try:
        with zipfile.ZipFile(zip_path, 'r') as zf:
            zf.extractall(temp_dir)
        yield temp_dir
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)
```

Directory Name Validation:
```python
def validate_skill_directory(path: Path) -> ValidationResult:
    """Validate directory name matches skill name in frontmatter."""
    skill_file = path / "SKILL.md"
    if not skill_file.exists():
        return ValidationResult(valid=False, error="Missing SKILL.md")

    metadata = parse_frontmatter(skill_file)
    if path.name != metadata.name:
        return ValidationResult(
            valid=False,
            error=f"Directory '{path.name}' doesn't match skill name '{metadata.name}'"
        )
    return ValidationResult(valid=True)
```

---

## Phase 4: MCP Registry

**Goal**: gobby-skills MCP server with all SkillPort-equivalent tools.

**Files:**

- `src/gobby/mcp_proxy/tools/skills.py` - create_skills_registry()
- `src/gobby/mcp_proxy/registries.py` - Register gobby-skills

**Tasks:**

- [ ] Create create_skills_registry() factory function (category: code)
- [ ] Implement `list_skills` tool (metadata only, ~100 tokens each) (category: code)
- [ ] Implement `get_skill` tool (full content + file paths) (category: code)
- [ ] Implement `search_skills` tool with category/tag filtering (category: code)
- [ ] Implement `install_skill` tool (from path/URL/GitHub/ZIP) (category: code)
- [ ] Implement `remove_skill` tool (category: code)
- [ ] Implement `update_skill` tool (refresh from source) (category: code)
- [ ] Implement `list_core_skills` tool (alwaysApply skills) (category: code)
- [ ] Add gobby-skills to setup_internal_registries() (category: config)

**Acceptance Criteria:**

- [ ] gobby-skills appears in list_mcp_servers()
- [ ] list_skills returns lightweight metadata (~100 tokens/skill)
- [ ] get_skill returns full content with references to scripts/assets
- [ ] search_skills supports category and tag filtering
- [ ] install_skill works with all source types

**Implementation Details:**

Separate Models File (`src/gobby/mcp_proxy/tools/skills/models.py`):
```python
# Keep MCP-specific request/response models separate from storage models
from dataclasses import dataclass
from gobby.storage.skills import Skill, SkillMetadata

@dataclass
class SkillSearchResult:
    """Lightweight search result for progressive disclosure."""
    name: str
    description: str
    category: str | None
    tags: list[str]
    score: float

@dataclass
class SkillInstallRequest:
    """Request to install a skill from various sources."""
    source: str  # Path, URL, or GitHub reference
    source_type: str | None = None  # Auto-detected if not provided
    project_scoped: bool = False
```

SkillsToolRegistry Pattern (follow hub.py pattern):
```python
# src/gobby/mcp_proxy/tools/skills/registry.py
from gobby.mcp_proxy.tools.internal import InternalToolRegistry

class SkillsToolRegistry(InternalToolRegistry):
    """Skills-specific tool registry with manager access."""

    def __init__(self, manager: SkillManager):
        super().__init__()
        self._manager = manager
        self._register_tools()

    def _register_tools(self) -> None:
        self.register("list_skills", self._list_skills)
        self.register("get_skill", self._get_skill)
        self.register("search_skills", self._search_skills)
        # ... etc
```

Core Skills Always Available:
```python
CORE_SKILLS_PATH = Path(__file__).parent.parent.parent / "install" / "shared" / "skills"

def list_core_skills() -> list[Skill]:
    """Return built-in skills that cannot be removed."""
    skills = []
    for skill_dir in CORE_SKILLS_PATH.iterdir():
        if skill_dir.is_dir() and (skill_dir / "SKILL.md").exists():
            skills.append(load_skill(skill_dir / "SKILL.md"))
    return skills
```

Progressive Disclosure Response Structure:
```python
# list_skills returns ~100 tokens per skill
{
    "skills": [
        {
            "name": "commit-message",
            "description": "Generate conventional commit messages",
            "category": "git",
            "tags": ["git", "commits"],
            "enabled": true
        }
    ],
    "total": 15,
    "hint": "Use get_skill(name) for full content"
}

# get_skill returns full content
{
    "name": "commit-message",
    "content": "# Commit Message Generator\n\n...",
    "scripts": ["scripts/validate.sh"],
    "assets": ["assets/template.txt"],
    "references": ["references/conventional-commits.md"]
}
```

---

## Phase 5: CLI Commands (Full SkillPort Parity)

**Goal**: CLI with all SkillPort commands plus Gobby extras.

**Files:**

- `src/gobby/cli/skills.py` - Click command group
- `src/gobby/cli/__init__.py` - Register skills group

**Tasks:**

- [ ] Create skills command group (category: code)
- [ ] Implement `gobby skills list [--category] [--tags] [--json]` (category: code)
- [ ] Implement `gobby skills show <name> [--json]` (category: code)
- [ ] Implement `gobby skills install <source> [--project]` (category: code)
- [ ] Implement `gobby skills remove <name>` (category: code)
- [ ] Implement `gobby skills update [name] [--all]` (category: code)
- [ ] Implement `gobby skills validate [path] [--json]` (category: code)
- [ ] Implement `gobby skills meta get <skill> <key>` (category: code)
- [ ] Implement `gobby skills meta set <skill> <key> <value>` (category: code)
- [ ] Implement `gobby skills meta unset <skill> <key>` (category: code)
- [ ] Implement `gobby skills init` (create .gobby/skills/ + config) (category: code)
- [ ] Implement `gobby skills new <name>` (scaffold new skill from template) (category: code)
- [ ] Implement `gobby skills doc` (generate AGENTS.md reference table) (category: code)
- [ ] Implement `gobby skills enable/disable <name>` (category: code)
- [ ] Implement `gobby skills sync` (sync filesystem to DB) (category: code)
- [ ] Register skills group in cli/__init__.py (category: config)

**Acceptance Criteria:**

- [ ] All SkillPort CLI commands have equivalents
- [ ] `--json` flag produces CI-friendly JSON output
- [ ] `gobby skills validate` catches all spec violations
- [ ] `gobby skills new` creates a complete skill scaffold
- [ ] `gobby skills doc` generates markdown reference table

**Implementation Details:**

Meta Subcommands (frontmatter field manipulation):
```bash
# Get a specific metadata field
gobby skills meta get commit-message author
# Output: anthropic

# Set a metadata field (creates if doesn't exist)
gobby skills meta set commit-message version "1.1.0"

# Remove a metadata field
gobby skills meta unset commit-message deprecated
```

```python
@skills.group()
def meta():
    """Manipulate skill metadata fields."""
    pass

@meta.command("get")
@click.argument("skill_name")
@click.argument("key")
def meta_get(skill_name: str, key: str):
    """Get a metadata field value."""
    skill = manager.get_by_name(skill_name)
    value = _get_nested_key(skill.metadata, key)
    click.echo(value)
```

Doc Command Output (AGENTS.md reference table):
```bash
gobby skills doc                    # Print to stdout
gobby skills doc --output AGENTS.md # Write to file
gobby skills doc --format json      # JSON for CI pipelines
```

Output format:
```markdown
# Available Skills

| Name | Description | Category | Tags |
|------|-------------|----------|------|
| commit-message | Generate conventional commit messages | git | git, commits |
| code-review | Review code for best practices | review | quality, pr |
```

Template Contents (`gobby skills new <name>`):
```python
SKILL_TEMPLATE = '''---
name: {name}
description: {description}
license: MIT
metadata:
  author: {author}
  version: "0.1.0"
  skillport:
    category: general
    tags: []
    alwaysApply: false
---

# {title}

## Overview

[Describe what this skill does]

## Instructions

[Detailed instructions for the AI agent]

## Examples

[Usage examples]
'''

def create_skill_scaffold(name: str, path: Path) -> None:
    """Create complete skill directory structure."""
    skill_dir = path / name
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(SKILL_TEMPLATE.format(...))
    (skill_dir / "scripts").mkdir()
    (skill_dir / "assets").mkdir()
    (skill_dir / "references").mkdir()
```

---

## Phase 6: Hook Integration (Gobby Advantage)

**Goal**: Integrate skills with Gobby's hook system.

**Files:**

- `src/gobby/hooks/skill_manager.py` - SkillManager for hook system (NOT in mcp_proxy/tools/skills)
- `src/gobby/hooks/skill_injection.py` - Skill injection logic
- `src/gobby/hooks/context_actions.py` - Add recommend_skills action
- `src/gobby/adapters/claude_code.py` - Update adapter

**Tasks:**

- [ ] Create SkillManager in hooks/ module (separate from MCP tools) (category: code)
- [ ] Add `inject_core_skills` config option to AppConfig (category: code)
- [ ] Implement core skill discovery from `src/gobby/install/shared/skills/` (category: code)
- [ ] Add skill injection to session-start hook response (category: code)
- [ ] Extend handoff payload with `active_skills` field (category: code)
- [ ] Create `recommend_skills` workflow action in context_actions.py (category: code)
- [ ] Integrate skill recommendation with `suggest_next_task` (category: code)
- [ ] Add workflow variable to control skill injection per-session (category: code)

**Acceptance Criteria:**

- [ ] Core skills are automatically injected at session start
- [ ] Skills persist across session handoffs
- [ ] Task suggestions can include relevant skills
- [ ] Skill injection can be disabled via config or workflow variable

**Implementation Details:**

SkillManager Location (hooks/, NOT mcp_proxy/tools/skills):
```python
# src/gobby/hooks/skill_manager.py
# This is a SEPARATE SkillManager from the MCP tools layer
# Handles hook-specific skill operations: injection, handoff, recommendations

class HookSkillManager:
    """Skill management for hook system.

    Note: This is separate from SkillsToolRegistry which handles MCP tools.
    This manager focuses on:
    - Core skill discovery and loading
    - Session injection timing
    - Handoff context building
    - Task-based skill recommendations
    """

    def __init__(self, storage: LocalSkillManager):
        self._storage = storage
        self._core_skills: list[Skill] | None = None

    def discover_core_skills(self) -> list[Skill]:
        """Discover built-in skills from install/shared/skills/."""
        if self._core_skills is None:
            self._core_skills = self._load_core_skills()
        return self._core_skills
```

Core Skill Discovery:
```python
CORE_SKILLS_PATH = Path(__file__).parent.parent / "install" / "shared" / "skills"

def _load_core_skills(self) -> list[Skill]:
    """Load metadata from SKILL.md files in core skills directory."""
    skills = []
    if not CORE_SKILLS_PATH.exists():
        return skills

    for skill_dir in CORE_SKILLS_PATH.iterdir():
        if skill_dir.is_dir():
            skill_file = skill_dir / "SKILL.md"
            if skill_file.exists():
                try:
                    skill = parse_skill_file(skill_file)
                    skills.append(skill)
                except Exception as e:
                    logger.warning(f"Failed to load core skill {skill_dir.name}: {e}")
    return skills
```

Injection Config (in `src/gobby/config/app.py`):
```python
@dataclass
class SkillsConfig:
    inject_core_skills: bool = True  # Auto-inject alwaysApply skills
    core_skills_path: str | None = None  # Override default path
    injection_format: str = "markdown"  # "markdown" or "structured"

@dataclass
class DaemonConfig:
    # ... existing fields ...
    skills: SkillsConfig = field(default_factory=SkillsConfig)
```

Session-Start Injection:
```python
# In session-start hook handler
def handle_session_start(event: HookEvent, config: DaemonConfig) -> HookResponse:
    response_data = {}

    if config.skills.inject_core_skills:
        skill_manager = HookSkillManager(storage)
        core_skills = skill_manager.discover_core_skills()
        alwaysApply = [s for s in core_skills if s.metadata.get("skillport", {}).get("alwaysApply")]

        if alwaysApply:
            response_data["injected_skills"] = format_skills_for_injection(alwaysApply)

    return HookResponse(data=response_data)
```

Handoff Context Extension:
```python
# Extend handoff payload with active skills
@dataclass
class HandoffPayload:
    # ... existing fields ...
    active_skills: list[str] | None = None  # Skill names active in previous session

def build_handoff_context(session: Session) -> HandoffPayload:
    # ... existing logic ...
    payload.active_skills = session.metadata.get("active_skills", [])
    return payload
```

recommend_skills Workflow Action:
```python
# src/gobby/hooks/context_actions.py

def recommend_skills(
    task: Task | None = None,
    category: str | None = None,
    limit: int = 3
) -> list[Skill]:
    """Recommend skills based on task category or explicit category.

    Used by workflows to suggest relevant skills when starting tasks.
    Integrates with suggest_next_task to include skill recommendations.
    """
    skill_manager = get_skill_manager()

    if task:
        # Infer category from task metadata or labels
        category = task.metadata.get("category") or _infer_category(task)

    if category:
        return skill_manager.search(
            query="",
            filters=SearchFilters(categories=[category]),
            limit=limit
        )
    return []
```

Integration with suggest_next_task:
```python
# In gobby-tasks suggest_next_task tool
def suggest_next_task(session_id: str) -> dict:
    task = _get_next_task(session_id)
    result = {"task": task.to_dict()}

    # Include skill recommendations if available
    recommended_skills = recommend_skills(task=task, limit=2)
    if recommended_skills:
        result["recommended_skills"] = [
            {"name": s.name, "description": s.description}
            for s in recommended_skills
        ]
        result["hint"] = f"Consider using skills: {', '.join(s.name for s in recommended_skills)}"

    return result
```

HookManager Integration:
```python
# HookManager accepts optional skill_manager
class HookManager:
    def __init__(
        self,
        config: DaemonConfig,
        skill_manager: HookSkillManager | None = None,
    ):
        self._config = config
        self._skill_manager = skill_manager
        if skill_manager is None and config.skills.inject_core_skills:
            self._skill_manager = HookSkillManager(get_storage())
```

Workflow Variable Control:
```python
# Per-session control via workflow variable
call_tool("gobby-workflows", "set_variable", {
    "name": "inject_skills",
    "value": False  # Disable skill injection for this session
})

# In hook handler, check workflow variable
if workflow_engine.get_variable("inject_skills", default=True):
    # Inject skills
    ...
```

---

## Phase 7: Documentation

**Goal**: Comprehensive documentation.

**Files:**

- `CLAUDE.md`
- `docs/guides/skills.md`

**Tasks:**

- [ ] Add gobby-skills to internal MCP servers table in CLAUDE.md (category: docs)
- [ ] Document skill file format (Agent Skills spec) (category: docs)
- [ ] Document all CLI commands (category: docs)
- [ ] Document MCP tools (category: docs)
- [ ] Document Gobby-specific extensions (triggers, project-scoping) (category: docs)
- [ ] Create docs/guides/skills.md user guide (category: docs)

**Acceptance Criteria:**

- [ ] CLAUDE.md documents gobby-skills server and tools
- [ ] Skill file format is documented
- [ ] All CLI commands documented with examples

---

## Dependencies

### External Dependencies

- PyYAML (already in dependencies)
- sklearn (already used by memory system)
- httpx (already in dependencies)

### Blockers

- None

---

## Configuration Reference

All skills-related configuration options:

### Daemon Config (`~/.gobby/config.yaml`)

```yaml
skills:
  # Auto-inject alwaysApply skills at session start
  inject_core_skills: true

  # Override default core skills path (default: src/gobby/install/shared/skills/)
  core_skills_path: null

  # Injection format: "markdown" (human-readable) or "structured" (JSON)
  injection_format: markdown

  # Search configuration
  search:
    # Default search backend: "tfidf", "embedding", "hybrid"
    default_backend: tfidf

    # Embedding provider: "openai", "anthropic", "local", null (disabled)
    embedding_provider: null

    # Auto-reindex when skills change
    reindex_on_change: true

    # Minimum similarity score threshold (0.0-1.0)
    min_score_threshold: 0.1

    # Hybrid search weights (must sum to 1.0)
    tfidf_weight: 0.4
    embedding_weight: 0.6
```

### Per-Session Control (Workflow Variables)

```python
# Disable skill injection for current session
call_tool("gobby-workflows", "set_variable", {
    "name": "inject_skills",
    "value": False
})

# Override search backend for session
call_tool("gobby-workflows", "set_variable", {
    "name": "skill_search_backend",
    "value": "embedding"
})
```

### Environment Variables

| Variable | Description | Default |
|----------|-------------|---------|
| `GOBBY_SKILLS_PATH` | Override core skills path | `src/gobby/install/shared/skills/` |
| `GOBBY_SKILL_CACHE_DIR` | GitHub clone cache directory | `~/.gobby/skill_cache/` |
| `GOBBY_EMBEDDING_PROVIDER` | Override embedding provider | (from config) |

---

## Cross-Cutting Patterns

Patterns that apply across all phases:

### TDD Pattern (Task Expansion)

When expanding tasks, use the TDD sandwich pattern:
1. **[TEST]** Write tests for the functionality
2. **[IMPL]** Implement the functionality (depends on #1)
3. **[REF]** Refactor if needed (depends on #2)

```python
# Task expansion generates this structure automatically
expand_task(task_id="#42")
# Creates:
# - #43: [TEST] Write tests for skill storage
# - #44: [IMPL] Implement skill storage (depends_on: #43)
# - #45: [REF] Refactor skill storage (depends_on: #44, optional)
```

### Progressive Disclosure

Token-efficient tool responses:
- **List operations**: ~100 tokens per item (name, description, category, tags)
- **Get operations**: Full content only when explicitly requested
- **Search operations**: Return ranked results with scores, not full content

```python
# Efficient: ~100 tokens
list_skills()  # Returns lightweight metadata

# Full content: ~1000+ tokens
get_skill(name="commit-message")  # Returns full skill content
```

### Protocol-Based Extensibility

Use Protocol classes for all extension points:
- `SkillSearchProtocol` - Search backend abstraction
- `EmbeddingProvider` - Embedding provider abstraction
- `SkillChangeListener` - Mutation listener abstraction
- `SkillSource` - Source type abstraction

This allows:
- Swapping implementations without changing calling code
- Testing with mocks
- Future extensibility

### Configuration-First

All behaviors controllable via config without code changes:
- Enable/disable features via YAML config
- Override via environment variables
- Session-specific overrides via workflow variables

Priority: workflow variable > env var > config file > default

### Graceful Degradation

All optional features fail gracefully:
- Embedding search falls back to TF-IDF
- Missing skills logged and skipped
- Network failures cached and retried
- Invalid skills return validation errors (not exceptions)

---

## Integration Points

How gobby-skills integrates with other Gobby systems:

### Hook System

| Hook | Integration |
|------|-------------|
| `session-start` | Inject core skills, restore active_skills from handoff |
| `pre-compact` | Include active_skills in handoff context |
| `pre-tool-use` | Check if skill provides tool pre-approval |

### Workflow System

| Workflow | Integration |
|----------|-------------|
| Variables | `inject_skills`, `skill_search_backend` control behavior |
| Actions | `recommend_skills` action for task-based suggestions |
| Steps | Future: skills as step requirements |

### Task System

| Feature | Integration |
|---------|-------------|
| `suggest_next_task` | Include skill recommendations based on task category |
| Task categories | Map to skill categories for recommendations |
| Task expansion | Consider skill-related subtasks |

### Session Handoff

| Field | Purpose |
|-------|---------|
| `active_skills` | Skills that were active in previous session |
| `skill_recommendations` | Skills recommended for continuation |

---

## Testing Strategy

### Unit Tests

- Storage CRUD operations
- YAML frontmatter parsing
- Name/description/compatibility validation
- TF-IDF search accuracy
- Embedding search (when configured)
- Tool input validation

### Integration Tests

- Full import flow: GitHub → parse → validate → store → search
- MCP tool round-trip: list → search → get → update
- CLI commands end-to-end
- Hook injection flow

### Manual Verification

1. `gobby skills init` - creates .gobby/skills/
2. `gobby skills new my-skill` - scaffolds template
3. `gobby skills validate ./my-skill` - validates against spec
4. `gobby skills install github:anthropics/skills` - imports from GitHub
5. `gobby skills list --json` - lists with JSON output
6. `gobby skills update --all` - updates from sources
7. Start Claude Code session - verify core skills injected
8. `list_tools(server="gobby-skills")` - verify MCP tools
9. `search_skills(query="commit", category="git")` - verify search

---

## Task Mapping

| Task # | Checkbox | Status |
| :--- | :--- | :--- |

---

## Completion Instructions

When completing a task:

1. Make all code changes
2. Run tests: `uv run pytest tests/skills/ -v`
3. Commit with task reference: `git commit -m "[#N] description"`
4. Close the task: `gobby tasks close #N --commit-sha <sha>`
5. Update the checkbox above to `[x]`

Never close a task without committing first unless it's a non-code task.

---

## Sources

- [SkillPort GitHub](https://github.com/gotalab/skillport)
- [Agent Skills Specification](https://agentskills.io/specification)
- [SkillPort Configuration Guide](https://github.com/gotalab/skillport/blob/main/guide/configuration.md)
- [SkillPort Creating Skills Guide](https://github.com/gotalab/skillport/blob/main/guide/creating-skills.md)
