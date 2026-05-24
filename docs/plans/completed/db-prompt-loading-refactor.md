# Plan: Database-Backed Prompt Storage

## Context

Prompts are currently managed as Markdown files with YAML frontmatter, stored in `install/shared/prompts/` and copied to `.gobby/prompts/` during installation. This filesystem approach requires manual syncing, creates divergence between dev/production, and makes prompts second-class citizens compared to skills (which already migrated to database storage). Moving prompts to the database brings them in line with skills, enables proper UI editing and import/export, and eliminates the filesystem copy/sync complexity.

## Scope

1. New `prompts` table with migration
2. `LocalPromptManager` storage class (CRUD, with bundled read-only enforcement)
3. `sync_bundled_prompts()` for daemon startup
4. `PromptLoader` refactored: database is the sole runtime source (no filesystem fallback)
5. Configuration API routes updated to use database (list, detail, override, revert)
6. Export/import updated: HTTP API reads/writes from database, CLI updated to match
7. All consumer PromptLoader creation sites wired with `db` parameter
8. Filesystem prompt files retained only as source for initial sync and CLI import
9. Bundled prompts are read-only unless dev mode (edits create `scope='global'` overrides)

---

## Files to Create

### `src/gobby/storage/prompts.py` (~350 lines)

Database storage class following `src/gobby/storage/skills.py` pattern.

**`PromptRecord` dataclass**:
- `id` (prefixed `pmt-`), `name` (path-style, e.g. `"expansion/system"`), `description`, `content` (raw template body), `version`, `variables` (JSON dict of variable specs), `scope` (`bundled` | `global` | `project`), `source_path`, `project_id`, `enabled`, timestamps
- `from_row()` classmethod, `to_dict()`, `to_prompt_template()` (converts back to existing `PromptTemplate` model for backward compat)

**`LocalPromptManager` class**:
- `create_prompt(name, description, content, version, variables, scope, source_path, project_id)` - ID via `generate_prefixed_id("pmt", ...)`
- `get_by_name(name, project_id=None)` - **precedence-aware**: project > global > bundled using `ORDER BY CASE scope WHEN 'project' THEN 1 WHEN 'global' THEN 2 WHEN 'bundled' THEN 3 END`
- `get_bundled(name)` - always returns the bundled version (for comparison/revert in UI)
- `update_prompt(prompt_id, ...)` - partial update with `_UNSET` sentinel. **Raises `ValueError` if target record has `scope='bundled'` and `dev_mode=False`** (constructor param).
- `delete_prompt(prompt_id)` - **Raises `ValueError` if target record has `scope='bundled'` and `dev_mode=False`**.
- `list_prompts(project_id, scope, category, enabled, limit, offset)`
- `list_overrides(project_id=None)` - returns only `scope='global'` or `scope='project'` records (for export)
- `search_prompts(query_text, project_id, limit)`
- `count_prompts(project_id, scope)`

Constructor accepts `dev_mode: bool = False`. When `True`, bundled records can be directly updated/deleted (for prompt development). When `False` (production default), bundled records are read-only — edits go through the override mechanism (`scope='global'`).

### `src/gobby/utils/dev.py` (~15 lines)

Extract `_is_dev_mode()` from `src/gobby/cli/installers/shared.py` into a shared utility:
```python
def is_dev_mode(project_path: Path | None = None) -> bool:
    """Detect if running inside the gobby source repo."""
    path = project_path or Path.cwd()
    return (path / "src" / "gobby" / "install" / "shared").is_dir()
```
Update `shared.py` to import from here instead of defining its own copy.

### `src/gobby/prompts/sync.py` (~120 lines)

Follows `src/gobby/skills/sync.py` pattern exactly.

- `get_bundled_prompts_path()` - returns `get_install_dir() / "shared" / "prompts"`
- `sync_bundled_prompts(db)` - walks all `.md` files, parses frontmatter + body, creates/updates in DB with `scope='bundled'`. Idempotent: creates new, updates changed content, skips identical.
- Reuses `parse_frontmatter()` extracted to `prompts/models.py`

### Test Files (~300 lines total)

- `tests/storage/test_prompts.py` - `LocalPromptManager` CRUD, precedence in `get_by_name()`, `list_overrides()`, `get_bundled()`
- `tests/prompts/test_prompt_sync.py` - sync idempotency, update detection, counting

---

## Files to Modify

### `src/gobby/storage/migrations.py`

- Add migration 106: CREATE TABLE `prompts` with indexes
- Add same DDL to `BASELINE_SCHEMA`
- Bump `BASELINE_VERSION` to 106

**Table schema:**
```sql
CREATE TABLE prompts (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    content TEXT NOT NULL,
    version TEXT DEFAULT '1.0',
    variables TEXT,  -- JSON
    scope TEXT NOT NULL DEFAULT 'bundled'
        CHECK(scope IN ('bundled', 'global', 'project')),
    source_path TEXT,
    project_id TEXT REFERENCES projects(id) ON DELETE CASCADE,
    enabled INTEGER DEFAULT 1,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX idx_prompts_name ON prompts(name);
CREATE INDEX idx_prompts_scope ON prompts(scope);
CREATE INDEX idx_prompts_project ON prompts(project_id);
CREATE UNIQUE INDEX idx_prompts_name_scope_project
    ON prompts(name, scope, COALESCE(project_id, ''));
```

### `src/gobby/prompts/models.py` (+25 lines)

- Extract `parse_frontmatter(content: str) -> tuple[dict, str]` as a standalone module-level function (currently a private method in `PromptLoader._parse_frontmatter()`). Used by both `sync.py` and `loader.py`.

### `src/gobby/prompts/loader.py` (significant refactor)

Database becomes the sole runtime source. The filesystem search path logic is removed from `load()`/`exists()`/`list_templates()`.

- Replace `project_dir`, `global_dir`, `defaults_dir` constructor params with `db: HubDatabase` and `project_id: str | None = None`
- `load()`: queries `LocalPromptManager.get_by_name()` only — no filesystem fallback
- `exists()`: queries database only
- `list_templates()`: queries database only
- `_parse_frontmatter()` delegates to the new `parse_frontmatter()` in models.py
- `render()` and Jinja2 rendering logic stays identical
- Remove `_find_template_file()`, `_search_paths`, `DEFAULTS_DIR` constant
- Remove `get_default_loader()` / `load_prompt()` / `render_prompt()` module-level convenience functions (they relied on filesystem; callers should use an injected loader instance instead)

### `src/gobby/prompts/__init__.py` (+3 lines)

- Export `parse_frontmatter`, `sync_bundled_prompts`

### `src/gobby/runner.py` (+8 lines)

Wire `sync_bundled_prompts()` into startup after skill sync (line ~131). Also detect dev mode and store on the runner instance for downstream consumers:
```python
from gobby.prompts.sync import sync_bundled_prompts
from gobby.utils.dev import is_dev_mode

self._dev_mode = is_dev_mode(Path.cwd())

try:
    prompt_result = sync_bundled_prompts(self.database)
    if prompt_result["synced"] > 0:
        logger.info(f"Synced {prompt_result['synced']} bundled prompts to database")
except Exception as e:
    logger.warning(f"Failed to sync bundled prompts: {e}")
```

---

## Configuration API Routes

### `src/gobby/servers/routes/configuration.py`

All four prompt endpoints switch from filesystem to database. The API contract (request/response shapes) stays identical so the frontend works without changes, except the `source` field gains a new value.

**Helper changes:**
- `_get_prompt_loader()` → pass `db=server.services.database` from ServiceContainer
- Add `_get_prompt_manager()` → returns `LocalPromptManager(server.services.database)`
- Remove `GLOBAL_PROMPTS_DIR` constant (no longer writing to filesystem)

**`GET /api/config/prompts`** (list):
- Currently: walks filesystem via `loader.list_templates()`, determines source by checking if `source_path` contains `~/.gobby/prompts`
- Change to: query `LocalPromptManager.list_prompts()`, derive `category` from name prefix, derive `source` and `has_override` from the `scope` field. A prompt `has_override=True` if a `scope='global'` or `scope='project'` record exists for that name.

**`GET /api/config/prompts/{path}`** (detail):
- Currently: loads via `PromptLoader`, manually checks filesystem for override and bundled files
- Change to: `manager.get_by_name(path)` for the effective prompt. `manager.get_bundled(path)` for `bundled_content` comparison. `has_override` = whether a `scope='global'` record exists. Variables from the `variables` JSON column.

**`PUT /api/config/prompts/{path}`** (save override):
- Currently: writes raw content to `~/.gobby/prompts/{path}.md`
- Change to: parse frontmatter from content, upsert a `scope='global'` record via `LocalPromptManager`. If one exists, update it. If not, create it. Clear the PromptLoader cache.
- **Never modifies the `scope='bundled'` record** — the override is always a separate `scope='global'` row. In dev mode, an additional `?dev=true` query param allows direct editing of the bundled record (for prompt authors iterating on bundled content).

**`DELETE /api/config/prompts/{path}`** (revert to bundled):
- Currently: deletes `~/.gobby/prompts/{path}.md`
- Change to: delete the `scope='global'` record for that name. The bundled record remains untouched, effectively reverting. Clear the PromptLoader cache.
- **Bundled records cannot be deleted** through this endpoint (returns 400). The only way to remove a bundled prompt is to remove the `.md` source file and restart the daemon.

---

## Export/Import

### HTTP Export (`POST /api/config/export`)

**Current** (lines 453-483): Reads `~/.gobby/prompts/` recursively, builds `prompts: Record<string, string>` of path → raw file content.

**Change**: Query `LocalPromptManager.list_overrides()` which returns only `scope='global'` records. Build the same `prompts: Record<string, string>` structure from database records. Each entry is `name → content` (just the template body, not frontmatter, since metadata is stored separately).

Actually, to maintain backward compatibility with existing export bundles, export the **full markdown with reconstructed frontmatter** (description, version, variables YAML + body content). This way imports work identically whether the bundle was created before or after this migration.

### HTTP Import (`POST /api/config/import`)

**Current** (lines 527-533): Writes each prompt file to `~/.gobby/prompts/{rel_path}`.

**Change**: For each prompt in `request.prompts`:
1. Parse frontmatter + body using `parse_frontmatter()`
2. Upsert a `scope='global'` record via `LocalPromptManager`
3. Clear PromptLoader cache after all imports

The `ImportConfigRequest` model stays the same (`prompts: dict[str, str] | None`).

### CLI Export/Import (`src/gobby/cli/export_import.py`)

**Current**: File-based copy between `.gobby/prompts/` directories using `shutil.copy2`.

**Change**: Since prompts no longer live on the filesystem at runtime, the CLI export/import for prompts must go through the database.

- **`gobby export prompt`**: Connect to the database directly (same pattern as other CLI commands that access DB), query `LocalPromptManager.list_overrides()`, write override prompts as `.md` files to the target path (reconstructing frontmatter + body).
- **`gobby import prompt`**: Read `.md` files from the source path, parse frontmatter, upsert as `scope='global'` (or `scope='project'` if importing into a project context) records via `LocalPromptManager`.
- **`gobby export prompt --to` / `--global`**: Exports override prompts from DB to filesystem for sharing.
- **`gobby import prompt --from`**: Reads `.md` files from disk, imports into DB.

The `RESOURCE_TYPES` dict entry for `"prompt"` stays, but the implementation changes from filesystem copy to DB read/write.

---

## Web UI

**No frontend changes needed.** The backend API response contract stays identical:
- `scope='global'` → `source='overridden'` in the response
- `scope='bundled'` → `source='bundled'` in the response

The existing `PromptsTab` in `web/src/components/ConfigurationPage.tsx` and `useConfiguration` hook in `web/src/hooks/useConfiguration.ts` work without modification. The `PromptInfo.source`, `PromptDetail.bundled_content`, save/delete/export/import flows all map directly to the new backend.

---

## Consumer PromptLoader Wiring (~2-3 lines each, ~11 files)

Since `PromptLoader` now requires `db`, all creation sites must pass a database reference. Every consumer listed below already has access to a database instance through its parent context (runner, action context, service container, etc.).

Each site currently creates `PromptLoader()` or `PromptLoader(project_dir=...)`. All must change to `PromptLoader(db=<database>, project_id=<project_id>)`. The `db` source for each:

| File | Current | DB Source |
|------|---------|-----------|
| `tasks/validation.py` | `PromptLoader(project_dir=...)` | Add `db` param to `TaskValidator.__init__()` (callers already have DB access) |
| `tasks/external_validator.py` | `PromptLoader(project_dir=...)` | Add `db` param to `_get_loader()` |
| `memory/extractor.py` | `PromptLoader()` or passed | Add `db` param to constructor; callers (`MemoryManager`) have `self.db` |
| `memory/manager.py` | `PromptLoader()` | Already has `self.db` |
| `memory/services/dedup.py` | `PromptLoader()` or passed | Add `db` param to constructor; callers have DB access via storage |
| `memory/services/knowledge_graph.py` | Takes loader as param | No change needed — caller passes loader |
| `workflows/summary_actions.py` | `PromptLoader()` | Thread `db` through action context (already has `session_manager`) |
| `servers/chat_session.py` | `PromptLoader()` | Add `db` param to `_load_chat_system_prompt()` |
| `mcp_proxy/services/recommendation.py` | `PromptLoader()` | Add `db` param to `RecommendationService.__init__()` |
| `mcp_proxy/importer.py` | `PromptLoader(project_dir=...)` | Already has `self.db` |
| `mcp_proxy/tools/sessions/_handoff.py` | `PromptLoader()` | Thread `db` from `session_manager` in `register_handoff_tools()` |

The consistent pattern: every `PromptLoader(db=db)` call uses whatever local name the database has in that scope. No need for each consumer to store `db` as a new attribute if it's only used to construct the loader.

Also update any callers of the removed module-level convenience functions (`load_prompt()`, `render_prompt()`, `get_default_loader()`) to use an injected `PromptLoader` instance instead.

### `src/gobby/cli/installers/shared.py`

Remove the `("prompts", "prompts", "prompts")` entry from `resource_dirs` and delete `_copy_prompts()`. Prompts are now database-only at runtime; the bundled `.md` files serve solely as source material for `sync_bundled_prompts()` on daemon startup.

---

## Override/Precedence Model

Three tiers of prompt records, all in the database:

| DB `scope` | `project_id` | How it gets there | Precedence |
|---|---|---|---|
| `"bundled"` | `NULL` | `sync_bundled_prompts()` on daemon startup from `.md` source files | Lowest |
| `"global"` | `NULL` | UI save override / HTTP import / CLI import | Medium |
| `"project"` | set | Future: project-scoped prompt editing | Highest |

`get_by_name()` returns the highest-priority match using SQL ordering.

---

## Key Design Decisions

1. **Database-only at runtime**: No filesystem fallback. `PromptLoader` requires `db`. Bundled `.md` files are only read by `sync_bundled_prompts()` on daemon startup; they are never read at prompt-load time.
2. **`scope` column**: Unlike skills (which only have `project_id`), prompts need three tiers. The `scope` column with a CHECK constraint captures this cleanly.
3. **`PromptRecord.to_prompt_template()`**: Bridge method converts DB records back to `PromptTemplate`, keeping all existing Jinja2 rendering code untouched.
4. **Installer prompt copying removed**: `_copy_prompts()` and the `prompts` entry in `resource_dirs` are deleted. No more filesystem copies.
5. **Export format backward-compatible**: Exports reconstruct full markdown (frontmatter + body) so bundles are portable.
6. **No frontend changes needed**: The API response contract stays identical. `scope='global'` maps to `source='overridden'`, `scope='bundled'` maps to `source='bundled'`.
7. **CLI export/import updated**: Both read/write through the database, not the filesystem.
8. **Bundled prompts are read-only in production**: `LocalPromptManager` enforces that `scope='bundled'` records cannot be updated or deleted unless `dev_mode=True`. The API override flow (PUT) always creates/updates a `scope='global'` record, never modifies the bundled record. Revert (DELETE) removes the override, leaving the bundled record untouched. This ensures bundled prompts remain pristine references.
9. **Dev mode detection**: Reuses the existing pattern from `src/gobby/cli/installers/shared.py:19-25` — `_is_dev_mode()` checks if `(project_path / "src" / "gobby" / "install" / "shared").is_dir()`. Extract this to a shared utility (e.g. `src/gobby/config/app.py` or a new `src/gobby/utils/dev.py`) so both the installer and `LocalPromptManager` can use it. In the daemon context, `GobbyRunner` detects dev mode at startup and passes it through to `LocalPromptManager`.

---

## Verification

1. **Unit tests**: `uv run pytest tests/storage/test_prompts.py tests/prompts/test_prompt_sync.py -v`
2. **Migration**: `uv run gobby restart` - verify `prompts` table created, 30 records synced (check logs)
3. **API list**: `curl localhost:60887/api/config/prompts` - should list all 30 prompts from DB
4. **API detail**: `curl localhost:60887/api/config/prompts/expansion/system` - should return content + variables
5. **Override via API**: `curl -X PUT localhost:60887/api/config/prompts/expansion/system -d '{"content":"test"}'` - creates `scope='global'` record
6. **Revert via API**: `curl -X DELETE localhost:60887/api/config/prompts/expansion/system` - removes override, falls back to bundled
7. **Export**: `curl -X POST localhost:60887/api/config/export` - verify `prompts` dict contains only overrides
8. **Import**: Export, modify a prompt, import back - verify override is restored
9. **Web UI**: Open Configuration → Prompts tab. Verify list loads, can edit/save/revert prompts
10. **Rendering**: Verify memory extraction, task validation, handoff generation still work (most frequent prompt consumers)
11. **Existing tests**: `uv run pytest tests/prompts/ -v` - existing prompt loader tests should still pass
