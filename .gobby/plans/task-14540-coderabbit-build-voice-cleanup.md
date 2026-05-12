# CodeRabbit Build Voice Cleanup Plan

## Decision Ledger

| Finding | Decision | Why |
|---|---:|---|
| `src/gobby/build/lifecycle.py` task_stage_states dynamic `UPDATE` | Fix | Still valid. Add a concise comment that `updates` contains only hardcoded assignments and validate against an allowed set before `task_manager.db.execute`. |
| `src/gobby/build/lifecycle.py` `_current_stage_name` empty `specs` | Fix | Still valid. Add an explicit guard before `min(specs, key=...)`, raising clear `ValueError` for an empty manifest. |
| `src/gobby/build/project_controls.py` lifecycle event DDL/INSERT transaction | Fix | Still valid. Wrap `CREATE TABLE`, `CREATE INDEX`, and INSERT cursor assignment in one `db.transaction()` block. |
| `src/gobby/build/project_controls.py` dispatcher cron update atomicity | Fix | Still valid. Wrap `CronJobStorage.update_job` and `update_system_job_bookkeeping` in one `db.transaction()` block. |
| `src/gobby/build/results.py` `BuildResult.stage_manifest` alias | Fix | Still valid as a nit. Keep the property for compatibility and add a docstring saying it intentionally aliases `manifest`. |
| `src/gobby/build/stage_manifest.py` private helpers imported by lifecycle | Fix | Still valid. Rename `_stage_state_specs` to `stage_state_specs`, `_specs_payload` to `specs_payload`, update lifecycle imports/calls, and add both to `__all__`. |
| `src/gobby/build/target_branch.py` `Path.cwd()` fallback | Fix | Still valid. `_current_target_branch` returns `None` when project, repo_path, or `.git` is missing; no silent fallback. |
| `src/gobby/build/validation.py` `_validate_clones_dir` missing vs unwritable | Fix | Still valid. First require existing directory with `ValueError`, then keep the writable check/message. |
| `src/gobby/hooks/session_activation.py` `_agent_run_from_row` key access | Fix | Still valid. Convert row to a mapping safely or catch missing keys and return `None`; only construct recovery when required keys are present. |
| `src/gobby/servers/routes/voice.py` `bool(None)` conversion | Fix | Still valid. Preserve `None` by forwarding raw `want_stt` / `want_tts`; keep no-arg call when both omitted. |
| `src/gobby/storage/tasks/_stage_state_schema.py` PRAGMA table name interpolation | Fix | Still valid. Validate `table_name` with a strict SQLite identifier regex before `PRAGMA table_info(...)`. |
| `src/gobby/workflows/hooks.py` duplicate no-repo project IDs | Fix | Valid as future-proofing. Current constants are UUIDs, so keep constants plus legacy literals conditionally to avoid duplicates if constants ever become literals. |
| `tests/meta/test_import_hygiene.py` enforce `<400` for all storage task modules | No fix | Current repo has `_manager.py`, `_crud.py`, `_transitions.py`, and `_models.py` over 400 lines. Adding the assertion would intentionally fail CI and force a broad refactor outside this minimal cleanup. |
| `tests/servers/routes/test_voice_routes.py` partial voice query test | Fix | Still valid. Add `test_status_forwards_partial_scoped_voice_targets` asserting `want_tts=None`. |
| `web/src/components/agents/AgentRulesEditor.tsx` datalist option `aria-label` | Fix | Still valid. Remove redundant `aria-label` from both filtered suggestion `<option>` maps. |
| `web/src/components/rules/RuleEditForm.tsx` datalist option `aria-label` | Fix | Still valid. Remove redundant `aria-label={t}`. |
| `web/src/hooks/voice/useVoiceStatus.ts` always sends false params | Fix | Still valid. Add only true flags to `URLSearchParams`; fetch `/api/voice/status` without `?` when no params exist. |

## Implementation Changes

- Backend build cleanup: add guarded lifecycle SQL update construction, empty-manifest guard, public stage manifest helper names, explicit target-branch resolution, clone-dir validation, and atomic project-control transactions.
- Defensive data handling: harden `_agent_run_from_row`, validate stage-state schema table identifiers, and future-proof `_NO_REPO_SYSTEM_PROJECTS`.
- Voice behavior: preserve backend `None` query semantics and align frontend polling URLs so omitted targets mean "use default".
- Frontend accessibility nits: remove redundant datalist option `aria-label`s.

## Test Plan

- Python focused tests: `GOBBY_TEST_PROTECT=1 uv run pytest tests/build/test_build_stop.py tests/build/test_target_branch.py tests/build_pipeline/test_service.py tests/build_pipeline/test_build_resolves_manifest.py tests/servers/routes/test_voice_routes.py tests/storage/tasks/test_stage_states.py tests/hooks/test_session_activation_reconciliation.py tests/workflows/test_workflow_hooks.py tests/meta/test_import_hygiene.py -v`
- Python lint/type checks: `uv run ruff check` on modified Python files; run targeted `uv run mypy` on touched `src/gobby/...` modules if imports/types changed.
- Web validation: `cd web && npm run lint`, `cd web && npm run type-check`, and `cd web && npm run test -- src/hooks/__tests__/useVoice.test.ts`.

## Assumptions

- Use `ValueError` for a missing/non-directory `clones_dir` to match existing config validation style.
- Keep `BuildResult.stage_manifest`; removing it would churn existing callers and tests for no behavioral gain.
- Do not add the `<400` task-storage meta assertion until the oversized modules are intentionally refactored.
- Do not run the full pytest suite.
