# Skill Template Refactor: Eliminate DB Template Rows

## Context

Skills currently use a two-row model: `source='template'` (disabled) + `source='installed'` (enabled) per bundled skill, with propagation logic to keep them in sync. This is unnecessarily complex. Workflows/rules already sync directly as `source='installed'` with no template rows.

**New model**: DB is source of truth. Templates are install-time defaults that seed the DB. No template rows in DB. Bundled skills (gobby-tagged) get overwritten on sync. User skills are never touched by sync.

**Dev workflow**: Edit in DB via UI/MCP, then manually export to template files when ready to commit. No export tooling needed — just ask the assistant to overwrite templates from DB installed skills.

## Files to Change

### 1. `src/gobby/skills/sync.py` — Rewrite sync logic

**Current**: Creates `source='template'`, `enabled=False` rows. Propagates to installed copies. Manages template/installed shadow pairs.

**New**: Create `source='installed'`, `enabled=True`, `tags=['gobby']` rows directly. On sync:
- Row doesn't exist → create from template
- Gobby-tagged row exists → overwrite from template (we own it)
- Non-gobby row with same name exists → skip (user's skill)
- Orphan cleanup: soft-delete gobby-tagged rows whose SKILL.md was removed (no cascade needed — no template/installed pairs)

**Delete**: `_propagate_to_installed()`, `_handle_existing_template()`, `_handle_installed_shadows_template()`, `_sync_single_skill()`. Replace with simpler `_sync_single_skill()`.

### 2. `src/gobby/storage/skills/_templates.py` — Delete

Remove entirely. `install_from_template()` and `install_all_templates()` become unnecessary.

### 3. `src/gobby/storage/skills/_manager.py` — Remove mixin

Remove `SkillTemplatesMixin` from `LocalSkillManager` composition. Update docstring.

### 4. `src/gobby/storage/skills/_metadata.py` — Remove template filtering

Methods with `include_templates` param:
- `get_by_name()` (line 191)
- `list_skills()` (line 499)
- `search_skills()` (line 567)
- `count_skills()` (line 658)

For each: keep the parameter in the signature (backward compat) but make it a no-op — no longer filter on `source != 'template'` since there are no template rows. Add deprecation comment.

Also remove `source='template'` special-casing from `create_skill()` if any.

### 5. `src/gobby/skills/manager.py` — Remove template methods

Remove:
- `install_from_template()` (line 434)
- `install_all_templates()` (line 448)
- `include_templates` params from `list_skills()` (line 301) and `search_skills()` (line 409) — keep as no-op

### 6. `src/gobby/mcp_proxy/tools/skills/install_from_template.py` — Delete

Remove the MCP tool entirely.

### 7. `src/gobby/mcp_proxy/tools/skills/__init__.py` — Remove registration

Remove `install_from_template` import (line 17) and `.register()` call (line 94).

### 8. `src/gobby/servers/routes/skills.py` — Remove HTTP routes

Remove:
- `install_all_templates` endpoint (line 422)
- `install_from_template` endpoint (line 489)
- `include_templates` query param (line 106) — keep but make no-op
- Template count query (line 203) — update to not filter on `source='template'`

### 9. `src/gobby/servers/routes/workflows.py` — Remove shared template routes

Remove:
- `install_from_template` endpoint (line 273)
- `install_all_templates` endpoint (line 281)

### 10. `src/gobby/cli/installers/shared.py` — Remove template install step

Remove `install_all_templates()` call (line 277). Bundled skills are directly installed by sync — no second step needed.

### 11. `web/src/hooks/useSkills.ts` — Remove template UI logic

- Remove `installFromTemplate` callback (line 426)
- Remove `includeTemplates` filter param (line 144)
- Remove from hook return value (line 562)

### 12. `web/src/components/skills/SkillsPage.tsx` — Remove install button

- Remove `installFromTemplate` usage (lines 49, 232, 236)
- Skills page no longer needs an "Install" button for bundled skills — they're already installed

### 13. DB Migration — Clean up template rows

Add migration:
1. For each `source='template'` skill with NO matching `source='installed'` row: update to `source='installed'`, `enabled=True`, add `'gobby'` to tags
2. For each `source='template'` skill with a matching installed row: hard-delete the template row (installed copy is the live version)
3. Ensure gobby-tagged installed copies have `tags` containing `'gobby'` for sync identification

**Index**: The unique index `(name, project_id, source)` can stay — `source` still differentiates `installed`/`project`/`custom`. After migration, no `template` rows exist so the constraint has same practical effect.

### 14. `src/gobby/workflows/template_hashes.py` — Extend for skills

Add `_load_skills()` method to `TemplateHashCache`. Compute SHA-256 of bundled SKILL.md content + files. Expose `has_drift(skill_row)` for skills.

### 15. Tests — Update

**Files to update:**
- `tests/storage/test_skill_sync.py` — Rewrite. Tests currently assert `source='template'`, `enabled=False`, propagation, `install_from_template`, `install_all_templates`. All change.
- `tests/mcp_proxy/tools/test_skills_coverage.py` — Remove `install_from_template` test class (line 320+)
- `tests/storage/skills/test_skill_files.py` — Update `source='template'` test data

## Execution Order

1. **DB migration** — Convert template → installed rows
2. **Storage layer** — Delete `_templates.py`, update `_metadata.py`, `_manager.py`
3. **Sync rewrite** — Rewrite `skills/sync.py`
4. **Skills manager** — Update `skills/manager.py`
5. **MCP tools** — Delete `install_from_template.py`, update `__init__.py`
6. **HTTP routes** — Update `routes/skills.py` and `routes/workflows.py`
7. **CLI installer** — Update `cli/installers/shared.py`
8. **Drift detection** — Extend `TemplateHashCache`
9. **Frontend** — Update `useSkills.ts` and `SkillsPage.tsx`
10. **Tests** — Update all affected test files
11. **Verify** — Run skill-related tests, confirm sync on fresh + existing DB

## Verification

```bash
# Skill storage tests
uv run pytest tests/storage/skills/ tests/storage/test_skill_sync.py -v

# Skill MCP tool tests
uv run pytest tests/mcp_proxy/tools/test_skills_coverage.py -v

# Skill route tests  
uv run pytest tests/servers/routes/test_skills_routes.py -v

# Fresh DB sync
uv run gobby stop && rm ~/.gobby/PostgreSQL hub && uv run gobby start --verbose

# Verify: all bundled skills source=installed, enabled=true, tagged gobby
uv run gobby skills list
```
