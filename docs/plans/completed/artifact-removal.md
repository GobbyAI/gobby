# Artifact System Removal Plan

**Status:** Planned
**Created:** 2026-02-11
**Rationale:** Artifacts duplicate what git + session transcripts already provide. Code that matters gets committed, errors are ephemeral, and everything is in the transcript. Rather than investing further (embeddings, mem0 sync), we're removing the entire artifact system. A git management page will replace it in a future session.

---

## Overview

The artifact system consists of ~30 dedicated files (source, test, web, docs) plus ~50 files with artifact references that need cleanup. The removal also requires a database migration to drop 3 tables and 5 indexes.

**Total scope:**

- **30 files to delete** (8 source + 4 web + 12 test + 1 doc + 5 web assets)
- **~48 files to modify** (~25 source + ~15 test + ~10 docs)
- **1 database migration** to add

---

## Phase 1: Files to DELETE

### Source Files (8)

| File | Purpose |
| --- | --- |
| `src/gobby/storage/artifacts.py` | `LocalArtifactManager` — CRUD for artifact storage |
| `src/gobby/storage/artifact_classifier.py` | `ArtifactClassifier` — content type classification |
| `src/gobby/hooks/artifact_capture.py` | `ArtifactCaptureHook` — auto-capture from tool outputs |
| `src/gobby/mcp_proxy/tools/artifacts.py` | MCP artifact tools + `gobby-artifacts` server definition |
| `src/gobby/mcp_proxy/tools/workflows/_artifacts.py` | Workflow-scoped artifact tools |
| `src/gobby/servers/routes/artifacts.py` | REST API routes (`/api/artifacts/*`) |
| `src/gobby/cli/artifacts.py` | CLI commands (`gobby artifacts list/show/delete`) |
| `src/gobby/workflows/artifact_actions.py` | Workflow action implementations for artifacts |

### Web Files (4)

| File | Purpose |
| --- | --- |
| `web/src/components/ArtifactsPage.tsx` | Artifacts page component |
| `web/src/components/ArtifactsPage.css` | Artifacts page styles |
| `web/src/components/artifacts/ArtifactIcons.tsx` | Artifact type icons |
| `web/src/hooks/useArtifacts.ts` | React hook for artifact data fetching |

### Test Files (12)

| File | Tests |
| --- | --- |
| `tests/cli/test_cli_artifacts.py` | CLI artifact commands |
| `tests/cli/test_artifacts_cli.py` | CLI artifact commands (alt) |
| `tests/hooks/test_artifact_capture.py` | Artifact capture hook |
| `tests/servers/test_artifacts_routes.py` | REST API routes |
| `tests/storage/test_artifact_classifier.py` | Classifier logic |
| `tests/storage/test_artifact_tags.py` | Artifact tagging |
| `tests/storage/test_artifacts_schema.py` | Schema validation |
| `tests/storage/test_storage_artifacts.py` | Storage CRUD |
| `tests/mcp_proxy/tools/test_artifacts_write.py` | MCP write tools |
| `tests/mcp_proxy/tools/test_artifacts.py` | MCP read tools |
| `tests/mcp_proxy/test_artifacts_server.py` | MCP server tests |
| `tests/workflows/test_artifact_actions.py` | Workflow actions |

### Documentation (1)

| File | Purpose |
| --- | --- |
| `docs/guides/artifacts.md` | Artifact system user guide |

### Web Assets (check for additional)

After deleting the above, also check:

- `web/src/components/artifacts/` — delete entire directory if empty after `ArtifactIcons.tsx` removal

### Total: 25 confirmed files + directory cleanup

---

## Phase 2: Files to MODIFY

### 2a. Registration / Wiring (HIGH PRIORITY)

These files import or register artifact modules. Failure to update them causes import errors at startup.

| File | What to Remove |
| --- | --- |
| `src/gobby/servers/http.py` | Artifact route registration (`include_router` call for artifacts) |
| `src/gobby/servers/routes/__init__.py` | Artifact route import and `__all__` entry |
| `src/gobby/mcp_proxy/registries.py` | Artifact tools registry entry |
| `src/gobby/hooks/factory.py` | `ArtifactCaptureHook` creation/registration |
| `src/gobby/hooks/hook_manager.py` | Artifact hook registration |
| `src/gobby/hooks/__init__.py` | Artifact exports from `__all__` |
| `src/gobby/hooks/event_handlers/__init__.py` | Artifact handler exports |
| `src/gobby/hooks/event_handlers/_base.py` | Artifact handler logic |
| `src/gobby/hooks/event_handlers/_tool.py` | Artifact tool handler |
| `src/gobby/cli/__init__.py` | `artifacts` CLI group (`app.add_command(artifacts)`) |
| `src/gobby/storage/database.py` | `LocalArtifactManager` init, `artifact_manager` property |
| `src/gobby/mcp_proxy/tools/workflows/__init__.py` | Artifact tool imports |

### 2b. Configuration

| File | What to Remove |
| --- | --- |
| `src/gobby/config/app.py` | Artifact config fields (e.g., `artifacts:` section in `DaemonConfig`) |
| `src/gobby/config/sessions.py` | Artifact session config fields |

### 2c. Workflow Engine

| File | What to Remove |
| --- | --- |
| `src/gobby/workflows/actions.py` | Artifact action registrations |
| `src/gobby/workflows/engine.py` | Artifact action references |
| `src/gobby/workflows/context_actions.py` | Artifact context actions |
| `src/gobby/workflows/definitions.py` | Artifact definitions |
| `src/gobby/workflows/lifecycle_evaluator.py` | Artifact evaluation logic |
| `src/gobby/workflows/state_manager.py` | Artifact state tracking |
| `src/gobby/workflows/webhook_actions.py` | Artifact webhook actions |
| `src/gobby/mcp_proxy/tools/workflows/_lifecycle.py` | Artifact references in lifecycle tools |
| `src/gobby/mcp_proxy/tools/workflows/_query.py` | Artifact references in query tools |
| `src/gobby/install/shared/workflows/lifecycle/session-lifecycle.yaml` | Artifact workflow actions/steps |

### 2d. Other Source

| File | What to Remove |
| --- | --- |
| `src/gobby/sessions/manager.py` | Artifact manager references |
| `src/gobby/utils/status.py` | Artifact status reporting |
| `src/gobby/llm/executor.py` | Artifact references |
| `src/gobby/servers/routes/admin.py` | Artifact admin references |
| `src/gobby/install/shared/prompts/chat/system.md` | Artifact mentions in system prompt |
| `src/gobby/install/shared/skills/doctor/SKILL.md` | Artifact health checks |

### 2e. Web

| File | What to Remove |
| --- | --- |
| `web/src/App.tsx` | Artifacts nav item, `ArtifactsPage` import, route case |

### 2f. Schema / Migration

| File | What to Do |
| --- | --- |
| `src/gobby/storage/migrations.py` | Add migration to DROP tables (see Phase 4) |
| `src/gobby/storage/schema_dump.sql` | Remove artifact table definitions |

### 2g. Tests to Modify (remove artifact references, not delete)

| File | What to Remove |
| --- | --- |
| `tests/hooks/test_hooks_manager.py` | Artifact hook test cases |
| `tests/workflows/test_state_manager_orchestration.py` | Artifact state tests |
| `tests/orchestration/test_review_retry_worktree.py` | Artifact references |
| `tests/workflows/test_workflow_actions.py` | Artifact action tests |
| `tests/workflows/test_dotdict_and_fixes.py` | Artifact references |
| `tests/workflows/test_context_actions.py` | Artifact context action tests |
| `tests/workflows/test_workflow_variables.py` | Artifact variable tests |
| `tests/cli/test_cli_workflows.py` | Artifact CLI workflow tests |
| `tests/workflows/test_coverage_improvements.py` | Artifact coverage tests |
| `tests/workflows/test_context_sources.py` | Artifact context source tests |
| `tests/sessions/test_sessions_manager.py` | Artifact manager references |
| `tests/llm/test_executor.py` | Artifact references |
| `tests/workflows/test_plugin_action_workflow.py` | Artifact plugin action tests |
| `tests/storage/test_storage_database.py` | Artifact manager tests |
| `tests/fixtures/workflows/test-phase-actions.yaml` | Artifact action fixtures |

### 2h. Documentation to Update

Remove artifact references from these guides. Leave completed plan docs as historical record.

| File | What to Remove |
| --- | --- |
| `docs/guides/configuration.md` | Artifact config documentation |
| `docs/guides/mcp-tools.md` | Artifact MCP tools section |
| `docs/guides/workflows.md` | Artifact workflow references |
| `docs/guides/workflow-actions.md` | Artifact action documentation |
| `docs/guides/cli-commands.md` | `gobby artifacts` CLI docs |
| `docs/guides/README.md` | Artifact guide link |
| `docs/guides/search.md` | Artifact search references |
| `docs/guides/sessions.md` | Artifact session references |
| `docs/guides/webhooks-and-plugins.md` | Artifact webhook references |
| `docs/examples/workflows/README.md` | Artifact workflow examples |

---

## Phase 3: MCP Server Removal

The `gobby-artifacts` MCP server is defined in `src/gobby/mcp_proxy/tools/artifacts.py` (which is in the DELETE list). After deletion, verify:

1. The server no longer appears in `list_mcp_servers()` output
2. No other code references `gobby-artifacts` as a server name
3. The MCP proxy instructions in `src/gobby/mcp_proxy/instructions.py` don't mention artifacts (verified: they don't)

---

## Phase 4: Database Migration

Add a new migration to `src/gobby/storage/migrations.py` that drops all artifact-related database objects.

### Tables to Drop

```sql
DROP TABLE IF EXISTS session_artifacts;
DROP TABLE IF EXISTS artifact_tags;
```

### FTS Table to Drop

```sql
DROP TABLE IF EXISTS session_artifacts_fts;
```

### Indexes to Drop

These will be dropped automatically with their parent tables, but for documentation:

- `idx_session_artifacts_session`
- `idx_session_artifacts_type`
- `idx_session_artifacts_created`
- `idx_session_artifacts_task`
- `idx_artifact_tags_tag`

### Migration Pattern

Follow existing migration patterns in `migrations.py`:

```python
def migrate_NNN_drop_artifacts(conn: psycopg.Connection) -> None:
    """Drop artifact tables and related objects."""
    conn.execute("DROP TABLE IF EXISTS artifact_tags")
    conn.execute("DROP TABLE IF EXISTS session_artifacts_fts")
    conn.execute("DROP TABLE IF EXISTS session_artifacts")
```

Note: Drop `artifact_tags` first (references `session_artifacts`), then FTS, then the base table.

### schema_dump.sql

Remove from `schema_dump.sql`:

- `CREATE TABLE session_artifacts (...)` block
- `CREATE TABLE artifact_tags (...)` block
- `CREATE VIRTUAL TABLE session_artifacts_fts (...)` block
- All `CREATE INDEX idx_session_artifacts_*` statements
- All `CREATE INDEX idx_artifact_tags_*` statements

---

## Phase 5: Execution Order

Execute in this order to minimize intermediate breakage:

| Step | Action | Fixes |
| --- | --- | --- |
| 1 | Delete 25 artifact-only files | Removes dead code |
| 2 | Update registration/wiring files (Phase 2a) | Fixes import errors from step 1 |
| 3 | Update config files (Phase 2b) | Removes config references |
| 4 | Update workflow files (Phase 2c) | Removes workflow references |
| 5 | Update other source files (Phase 2d) | Removes remaining source references |
| 6 | Update web `App.tsx` (Phase 2e) | Removes UI routes |
| 7 | Add database migration (Phase 4) | Handles data cleanup |
| 8 | Update `schema_dump.sql` (Phase 2f) | Matches migration |
| 9 | Run `uv run ruff check src/` | Find any remaining import errors |
| 10 | Run `uv run pytest` (targeted) | Find remaining test references |
| 11 | Fix test files (Phase 2g) | Clean up test references |
| 12 | Update docs (Phase 2h) | Clean up documentation |

### Validation Checkpoints

After steps 2-5: `uv run ruff check src/` should pass (no import errors)
After step 8: `uv run python -c "from gobby.storage.migrations import *"` should work
After step 11: `uv run pytest tests/ -x --ignore=tests/e2e` should pass
After step 12: `grep -r "artifact" docs/guides/` should only show historical plan references

---

## Risks and Mitigations

| Risk | Mitigation |
| --- | --- |
| Missed references cause runtime errors | Grep audit + ruff check + pytest after each phase |
| Database migration breaks existing installs | `DROP IF EXISTS` is safe; tables may not exist on fresh installs |
| Web build breaks | Remove imports before route references; test with `npm run build` |
| Workflow YAML references cause startup errors | Update `session-lifecycle.yaml` in step 4 |

---

## Out of Scope

- **Git management page** — future session, replaces artifact browsing
- **Session transcript viewer** — already exists, no changes needed
- **Completed plan docs** — leave artifact references as historical record
- **Third-party integrations** — no external systems depend on artifacts

---

## File Count Summary

| Category | Delete | Modify |
| --- | --- | --- |
| Source (src/) | 8 | ~25 |
| Web (web/) | 4 | 1 |
| Tests (tests/) | 12 | 15 |
| Docs (docs/) | 1 | ~10 |
| **Total** | **25** | **~51** |
