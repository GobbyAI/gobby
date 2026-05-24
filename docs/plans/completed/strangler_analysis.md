# Strangler Fig Decomposition Analysis

This document outlines the strategy for decomposing three logic-heavy files in the Gobby codebase using the Strangler Fig pattern.

## Overview

| File | Lines | Priority | Risk |
| :--- | :--- | :--- | :--- |
| `mcp_proxy/tools/session_messages.py` | 1,056 | 1st | Low |
| `memory/manager.py` | 1,010 | 2nd | Medium |
| `workflows/task_enforcement_actions.py` | 1,572 | 3rd | Medium |

**Pattern**: Extract to new modules, update all imports, delete original files. No backward compatibility shims.

---

## Phase 1: Session Messages Decomposition

**File**: `src/gobby/mcp_proxy/tools/session_messages.py` (1,056 lines)
**Role**: Defines the `gobby-sessions` MCP server and all its tools.
**Why first**: Greenfield (no existing `sessions/` package), follows existing `tasks/` pattern, lowest coupling.

### Target Structure (Phase 1)

```
src/gobby/mcp_proxy/tools/sessions/
├── __init__.py        # Exports create_session_messages_registry
├── _messages.py       # get_session_messages, search_messages
├── _handoff.py        # create_handoff, get_handoff_context, pickup, helpers
├── _crud.py           # get_session, get_current_session, list_sessions, session_stats
├── _commits.py        # get_session_commits, mark_loop_complete
└── _factory.py        # create_session_messages_registry
```

### Tasks (Phase 1)

- [ ] Create sessions package with `__init__.py` (category: config)

- [ ] Extract handoff helpers to _handoff.py (category: refactor)
- [ ] Extract message tools to _messages.py (category: refactor)
- [ ] Extract handoff tools to _handoff.py (category: refactor)
- [ ] Extract CRUD tools to _crud.py (category: refactor)
- [ ] Extract commits tools to _commits.py (category: refactor)
- [ ] Create factory in _factory.py (category: refactor)
- [ ] Update imports in registries.py to use sessions package (category: refactor)
- [ ] Update test imports to use sessions package (category: refactor)
- [ ] Delete session_messages.py (category: refactor)

### Verification (Phase 1)

```bash
uv run pytest tests/mcp_proxy/test_mcp_tools_session_messages.py tests/mcp_proxy/tools/test_session_messages_coverage.py -v
```

---

## Phase 2: Memory Manager Decomposition

**File**: `src/gobby/memory/manager.py` (1,010 lines)
**Role**: High-level orchestrator for the memory system.
**Why second**: Existing package structure (`backends/`, `search/`), clear boundaries.

### Target Structure (Phase 2)

```
src/gobby/memory/
├── ingestion/
│   ├── __init__.py
│   └── multimodal.py    # MultimodalIngestor class
├── services/
│   ├── __init__.py
│   └── crossref.py      # CrossrefService class
└── search/
    └── coordinator.py   # SearchCoordinator class (NEW)
```

### Tasks (Phase 2)

- [ ] Create SearchCoordinator in search/coordinator.py (category: refactor)
- [ ] Create MultimodalIngestor in ingestion/multimodal.py (category: refactor)
- [ ] Create CrossrefService in services/crossref.py (category: refactor)
- [ ] Refactor MemoryManager as facade (category: refactor)

### Component Details

**SearchCoordinator** (lines 48-157):
- `_search_backend` property and state
- `_ensure_search_backend_fitted()`
- `mark_search_refit_needed()`
- `reindex_search()`

**MultimodalIngestor** (lines 216-383):
- `remember_with_image()`
- `remember_screenshot()`

**CrossrefService** (lines 385-474):
- `_create_crossrefs()`
- `get_related()`

### MemoryManager Facade Specification

After extraction, `MemoryManager` becomes a thin facade that delegates to the new coordinators/services. This maintains backward compatibility for existing callers while internal responsibilities are reorganized.

#### Retained Public API

| Method | Signature | Delegates To |
| :--- | :--- | :--- |
| `save_memory()` | `async def save_memory(self, content: str, metadata: dict | None = None) -> Memory` | Direct (core CRUD) |
| `get_memory()` | `async def get_memory(self, memory_id: str) -> Memory | None` | Direct (core CRUD) |
| `delete_memory()` | `async def delete_memory(self, memory_id: str) -> bool` | Direct (core CRUD) |
| `search_memory()` | `async def search_memory(self, query: str, limit: int = 10) -> list[Memory]` | `SearchCoordinator` |
| `reindex_search()` | `async def reindex_search(self) -> None` | `SearchCoordinator` |
| `mark_search_refit_needed()` | `def mark_search_refit_needed(self) -> None` | `SearchCoordinator` |
| `remember_with_image()` | `async def remember_with_image(self, image_path: str, description: str) -> Memory` | `MultimodalIngestor` |
| `remember_screenshot()` | `async def remember_screenshot(self, description: str) -> Memory` | `MultimodalIngestor` |
| `get_related()` | `async def get_related(self, memory_id: str, limit: int = 5) -> list[Memory]` | `CrossrefService` |

#### Delegation Flow

```python
class MemoryManager:
    """Facade for memory operations. Delegates to specialized coordinators/services."""

    def __init__(self, config: MemoryConfig, db: HubDatabase):
        self._db = db
        self._config = config
        # Initialize coordinators/services
        self._search = SearchCoordinator(config, db)
        self._ingestor = MultimodalIngestor(config, db)
        self._crossref = CrossrefService(config, db)

    # Search delegation (async, propagates exceptions)
    async def search_memory(self, query: str, limit: int = 10) -> list[Memory]:
        return await self._search.search(query, limit)

    async def reindex_search(self) -> None:
        await self._search.reindex()

    def mark_search_refit_needed(self) -> None:
        self._search.mark_refit_needed()

    # Multimodal delegation (async, propagates exceptions)
    async def remember_with_image(self, image_path: str, description: str) -> Memory:
        return await self._ingestor.remember_with_image(image_path, description)

    async def remember_screenshot(self, description: str) -> Memory:
        return await self._ingestor.remember_screenshot(description)

    # Crossref delegation (async, propagates exceptions)
    async def get_related(self, memory_id: str, limit: int = 5) -> list[Memory]:
        return await self._crossref.get_related(memory_id, limit)
```

#### Error Handling Strategy

- **Propagation**: All exceptions from coordinators/services propagate to callers unchanged
- **No transformation**: The facade does not wrap or transform exceptions
- **Logging**: Errors are logged at the coordinator/service level, not the facade

#### Backward Compatibility

- **No adapters required**: The facade maintains the exact public API signatures
- **Internal-only change**: Callers continue using `MemoryManager` without modification
- **Deprecation path**: None needed; this is a transparent refactor

### Verification (Phase 2)

```bash
uv run pytest tests/memory/test_manager.py tests/memory/test_v2_features.py -v
```

---

## Phase 3: Task Enforcement Decomposition

**File**: `src/gobby/workflows/task_enforcement_actions.py` (1,572 lines)
**Role**: Contains logic for enforcing task rules and evaluating conditions for tool blocking.
**Why last**: Most complex, existing files to reconcile, extensive test coverage.

### Naming Reconciliation

- Existing `evaluator.py` has `ConditionEvaluator` (uses eval)
- Create `safe_evaluator.py` for `SafeExpressionEvaluator` (AST-based)
- Existing `git_utils.py` → extend with `get_dirty_files()`

### Target Structure (Phase 3)

```
src/gobby/workflows/
├── safe_evaluator.py     # SafeExpressionEvaluator, _LazyBool, helpers
├── git_utils.py          # (extend existing) + get_dirty_files
└── enforcement/
    ├── __init__.py       # Exports all handlers
    ├── blocking.py       # block_tools + handler
    ├── commit_policy.py  # capture_baseline, require_commit + handlers
    └── task_policy.py    # require_task_*, validate_scope + handlers
```

### Tasks (Phase 3)

- [ ] Extract SafeExpressionEvaluator to safe_evaluator.py (category: refactor)
- [ ] Extend git_utils.py with get_dirty_files (category: refactor)
- [ ] Create enforcement package with __init__.py (category: config)
- [ ] Extract blocking action to enforcement/blocking.py (category: refactor)
- [ ] Extract commit policy to enforcement/commit_policy.py (category: refactor)
- [ ] Extract task policy to enforcement/task_policy.py (category: refactor)
- [ ] Update imports in actions.py to use enforcement package (category: refactor)
- [ ] Update test imports to use new modules (category: refactor)
- [ ] Delete task_enforcement_actions.py (category: refactor)

### Component Details

**safe_evaluator.py** (lines 32-305):
- `_LazyBool` class
- `_is_plan_file()` function
- `SafeExpressionEvaluator` class
- `_evaluate_block_condition()` function

**enforcement/blocking.py** (lines 308-458 + handler):
- `block_tools()` function
- `handle_block_tools()` handler

**enforcement/commit_policy.py** (lines 560-699 + handlers):
- `capture_baseline_dirty_files()` + handler
- `require_commit_before_stop()` + handler

**enforcement/task_policy.py** (lines 702-1344 + handlers):
- `_get_task_session_liveness()` helper
- `require_task_review_or_close_before_stop()` + handler
- `require_task_complete()` + handler
- `require_active_task()` + handler (DEPRECATED)
- `validate_session_task_scope()` + handler

### Verification (Phase 3)

```bash
uv run pytest tests/workflows/test_task_enforcement.py -v
```

---

## Risk Mitigation

| Risk | Mitigation |
| :--- | :--- |

| Test breakage from imports | Update all imports before deleting original files |
| Circular imports | Use TYPE_CHECKING imports; utilities have no deps |
| Runtime behavior change | No logic changes during extraction |
| Naming conflicts | `safe_evaluator.py` (new), extend `git_utils.py` |

## Final Verification

After all phases:
```bash
uv run ruff check src/
uv run mypy src/gobby/mcp_proxy/tools/ src/gobby/memory/ src/gobby/workflows/
uv run pytest tests/ -v --tb=short
```
