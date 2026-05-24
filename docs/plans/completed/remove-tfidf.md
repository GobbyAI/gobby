# Replace TF-IDF with FTS5 for Task and Skill Search

## Context

Gobby has three search backends: Qdrant (vector/semantic), FTS5 (PostgreSQL full-text), and TF-IDF (scikit-learn in-memory). TF-IDF is the weakest — it requires in-memory fitting, has no persistence, needs a dirty-flag/refit cycle, and brings a scikit-learn dependency. Both task search and skill search currently use TF-IDF, but the data already lives in PostgreSQL where FTS5 is a natural fit. Qdrant handles real semantic search for memory/code. TF-IDF is redundant.

## Scope

- Add FTS5 virtual tables + triggers for `tasks` and `skills` tables
- Rewrite `TaskSearcher` and `SkillSearch` to query FTS5 directly
- Remove TF-IDF, UnifiedSearcher, and related abstractions
- Slim `SearchConfig` → `EmbeddingConfig` (keep only embedding model/key/base fields)
- Delete dead code and tests

## Phase 1: Migration — FTS5 Tables and Triggers

**File: `src/gobby/storage/migrations.py`**

Add `_setup_tasks_fts(db)` and `_setup_skills_fts(db)` callable functions following the existing `_setup_code_symbols_fts` pattern.

### tasks_fts

```sql
CREATE VIRTUAL TABLE IF NOT EXISTS tasks_fts USING fts5(
    title, description, labels, task_type, category,
    content='tasks', content_rowid='rowid'
);
```
- Standard INSERT/UPDATE/DELETE triggers (same pattern as code_symbols_fts)
- Initial population: `INSERT OR IGNORE INTO tasks_fts(...) SELECT ... FROM tasks`
- JSON labels column works fine — FTS5 tokenizer strips brackets/quotes naturally

### skills_fts

```sql
CREATE VIRTUAL TABLE IF NOT EXISTS skills_fts USING fts5(
    name, description, metadata,
    content='skills', content_rowid='rowid'
);

-- INSERT trigger: only index non-deleted rows
CREATE TRIGGER IF NOT EXISTS skills_fts_insert AFTER INSERT ON skills
WHEN NEW.deleted_at IS NULL
BEGIN
    INSERT INTO skills_fts(rowid, name, description, metadata)
    VALUES (NEW.rowid, NEW.name, NEW.description, NEW.metadata);
END;

-- UPDATE trigger: remove old entry, re-insert only if not soft-deleted
CREATE TRIGGER IF NOT EXISTS skills_fts_update AFTER UPDATE ON skills
BEGIN
    INSERT INTO skills_fts(skills_fts, rowid, name, description, metadata)
    VALUES ('delete', OLD.rowid, OLD.name, OLD.description, OLD.metadata);
    INSERT INTO skills_fts(rowid, name, description, metadata)
    SELECT NEW.rowid, NEW.name, NEW.description, NEW.metadata
    WHERE NEW.deleted_at IS NULL;
END;

-- DELETE trigger: remove from FTS
CREATE TRIGGER IF NOT EXISTS skills_fts_delete AFTER DELETE ON skills
BEGIN
    INSERT INTO skills_fts(skills_fts, rowid, name, description, metadata)
    VALUES ('delete', OLD.rowid, OLD.name, OLD.description, OLD.metadata);
END;
```

- Index raw `metadata` JSON — tags/category words get tokenized naturally
- Initial population excludes soft-deleted: `INSERT ... SELECT ... FROM skills WHERE deleted_at IS NULL`
- UPDATE trigger handles soft-delete transitions: removes FTS entry when `deleted_at` is set

### Migration entry
- Add migration v177 (callable that runs both setup functions)
- Update `_apply_baseline()` to call both (like it does for code_symbols/code_content)
- **Do NOT bump BASELINE_VERSION** — that happens separately when baseline_schema.sql is updated

## Phase 2: Rewrite TaskSearcher

**File: `src/gobby/storage/tasks/_search.py`** — full rewrite

New `TaskSearcher`:
- Constructor takes `db` (not TF-IDF config params)
- `search(query, top_k)` → single FTS5 SQL query returning `list[tuple[str, float]]`
- `_fts5_query(query)` → sanitizes user input (quote each token, implicit AND)
- Score = `-rank` (FTS5 rank is negative; more negative = better)
- **Remove**: `fit()`, `mark_dirty()`, `needs_refit()`, `clear()`, `get_stats()`, `build_searchable_content()`

## Phase 3: Simplify LocalTaskManager.search_tasks()

**File: `src/gobby/storage/tasks/_manager.py`**

- Merge FTS5 MATCH + SQL filters into one query (currently two-phase: TF-IDF candidates → Python filter)
- Remove `_ensure_searcher()`, `_ensure_search_fitted()` lazy-init methods
- Remove dirty-flag logic from `_notify_listeners()` (search-specific `mark_dirty()` call)
- Simplify `reindex_search()` to `INSERT INTO tasks_fts(tasks_fts) VALUES('rebuild')`
- `TaskSearcher` constructed eagerly in `__init__`

## Phase 4: Rewrite SkillSearch

**File: `src/gobby/skills/search.py`** — major simplification

New `SkillSearch`:
- Constructor takes `db` (not `SearchConfig`)
- `search(query, top_k, filters)` → FTS5 query with `deleted_at IS NULL` and `enabled = 1`
- Category filter pushed into SQL via `jsonb_path_query(s.metadata, '$.skillport.category')`
- Tag filters remain Python-side (JSON array membership)
- `search_async()` kept as thin `asyncio.to_thread` wrapper
- **Remove**: `index_skills()`, `index_skills_async()`, `add_skill()`, `update_skill()`, `remove_skill()`, `needs_reindex()`, fallback tracking methods

Keep: `SearchFilters`, `SkillSearchResult` dataclasses (external API)

## Phase 5: Update SkillManager

**File: `src/gobby/skills/manager.py`**

- `SkillSearch(db=db)` instead of `SkillSearch(config=search_config)`
- Remove `search_config` parameter from `__init__`
- Remove `_on_skill_change()` listener (triggers handle sync)
- Simplify `reindex()` to FTS5 rebuild
- Remove `needs_reindex()`

## Phase 6: Update MCP Skill Tools

**File: `src/gobby/mcp_proxy/tools/skills/__init__.py`**
- Remove `SearchConfig` import/param from `create_skills_registry()`
- `SkillSearch(db=db)`

**File: `src/gobby/mcp_proxy/tools/skills/search_skills.py`**
- Remove `_SkillIndexer` class (dirty-flag debouncing no longer needed)
- Remove `_setup_indexing()`
- Simplify search tool handler — just call `ctx.search.search_async()` directly

**File: `src/gobby/mcp_proxy/registries.py`**
- Remove `search_config` kwarg from `create_skills_registry()` call

## Phase 7: Slim SearchConfig → EmbeddingConfig

**File: `src/gobby/search/models.py`**

- Rename `SearchConfig` → `EmbeddingConfig`
- Keep only: `embedding_model`, `embedding_api_base`, `embedding_api_key`
- Delete: `mode`, `tfidf_weight`, `embedding_weight`, `notify_on_fallback`, `get_normalized_weights()`, `get_mode_enum()`
- Delete: `SearchMode` enum, `FallbackEvent` dataclass

**File: `src/gobby/config/app.py`**
- `search: SearchConfig` → `embedding: EmbeddingConfig`
- Update `get_search_config()` → `get_embedding_config()`

**File: `src/gobby/runner_init.py`**
- Update references from `config.search.*` to new field name

## Phase 8: Clean Up Search Module

**File: `src/gobby/search/__init__.py`** — update exports (keep embedding utilities only)

**Delete files:**
- `src/gobby/search/tfidf.py`
- `src/gobby/search/unified.py`
- `src/gobby/search/backends/` (entire directory)
- `src/gobby/search/protocol.py`

**File: `src/gobby/skills/__init__.py`** — update re-exports (remove UnifiedSearcher, SearchConfig, etc.)

## Phase 9: Update Tests

**Delete:**
- `tests/search/test_tfidf.py`
- `tests/search/test_unified.py`

**Rewrite:**
- `tests/storage/test_task_search.py` — uses real DB fixture already; remove refit/dirty-flag assertions, verify FTS5 search results
- `tests/skills/test_search.py` — use real DB fixture; INSERT skills into DB (triggers handle FTS5); search and verify
- `tests/mcp_proxy/tools/tasks/test_search.py` — remove reindex/refit references
- `tests/mcp_proxy/tools/test_skills_coverage.py` — update SearchConfig → EmbeddingConfig
- `tests/cli/tasks/test_search_coverage.py` — update reindex command tests

## FTS5 Query Sanitization

Helper function used by both TaskSearcher and SkillSearch:
```python
def fts5_query(query: str) -> str:
    """Convert user query to safe FTS5 query. Quote each token, implicit AND."""
    tokens = query.strip().split()
    if not tokens:
        return '""'
    # Strip quotes from tokens, then re-quote to escape FTS5 operators
    return " ".join(f'"{t.replace('"', "")}"' for t in tokens)
```

Shared in a small utility, e.g. `src/gobby/search/fts5.py` or inline in each searcher.

## Verification

1. Run migrations test: `uv run pytest tests/storage/test_migrations.py -v`
2. Run task search tests: `uv run pytest tests/storage/test_task_search.py -v`
3. Run skill search tests: `uv run pytest tests/skills/test_search.py -v`
4. Run MCP tool tests: `uv run pytest tests/mcp_proxy/tools/tasks/test_search.py tests/mcp_proxy/tools/test_skills_coverage.py -v`
5. Run CLI search tests: `uv run pytest tests/cli/tasks/test_search_coverage.py -v`
6. Verify no remaining imports of deleted modules: `grep -r "from gobby.search.tfidf\|from gobby.search.unified\|from gobby.search.backends\|from gobby.search.protocol\|TFIDFSearcher\|UnifiedSearcher\|TFIDFBackend" src/`
7. Lint: `uv run ruff check src/gobby/search/ src/gobby/storage/tasks/ src/gobby/skills/`

## Critical Files

| File | Action |
|------|--------|
| `src/gobby/storage/migrations.py` | Add FTS5 setup functions + v177 migration |
| `src/gobby/storage/tasks/_search.py` | Full rewrite (TF-IDF → FTS5) |
| `src/gobby/storage/tasks/_manager.py` | Simplify search_tasks, remove dirty-flag |
| `src/gobby/skills/search.py` | Major rewrite (UnifiedSearcher → FTS5) |
| `src/gobby/skills/manager.py` | Remove search_config, simplify |
| `src/gobby/mcp_proxy/tools/skills/__init__.py` | Remove SearchConfig |
| `src/gobby/mcp_proxy/tools/skills/search_skills.py` | Remove indexer |
| `src/gobby/mcp_proxy/registries.py` | Remove search_config kwarg |
| `src/gobby/search/models.py` | Slim to EmbeddingConfig |
| `src/gobby/search/__init__.py` | Update exports |
| `src/gobby/config/app.py` | SearchConfig → EmbeddingConfig |
| `src/gobby/runner_init.py` | Update config references |
| `src/gobby/search/tfidf.py` | Delete |
| `src/gobby/search/unified.py` | Delete |
| `src/gobby/search/backends/` | Delete directory |
| `src/gobby/search/protocol.py` | Delete |
