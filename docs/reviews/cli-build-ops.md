# Review: cli build/ops (pipelines / plans / build / postgres / skills / merge / clones / integrations / packaging)

- **Scope:** `src/gobby/cli/` remaining command groups (~12,000 lines): `pipelines.py`, `workflows/` (manage, check, inspect, variables), `stages.py`, `build.py` + `_build_daemon.py`, `plans.py` + `plan.py`, `postgres.py` + `postgres_backup.py`, `services.py` + `service.py` + `init.py`, `skills.py`, `mcp_proxy.py`, `extensions.py`, `clones.py`, `merge.py`, `projects.py`, `profiles.py`, `sync.py`, `export_import.py`, `linear.py`, `github.py`, `communications.py`, `cron.py`, `tokens.py`, `pack.py`. **Split boundary:** the install subsystem (`install.py`, `install_setup_*.py`, `_install_*.py`, `installers/`) is out of scope here — it belongs to the install/runner leaf (#15792). cli-core (#15771) covered daemon/agents/rules/sessions/tasks/memory/worktrees. Split across 6 parallel reviewers; synthesized and Blocker-verified against source.
- **Reviewer:** Claude (Fable 5) — 6 general-purpose review agents + synthesizer verification
- **Commit / branch:** `0.5.0` @ 3f5bccfb0
- **Summary:** 6 Blocker · 15 Important · 18 Nit — this surface concentrates the epic's most dangerous bugs: an unauthenticated-grade **zip-slip in `gobby unpack`**, **secrets packed world-readable**, two **`gobby merge` bugs** (acts on the wrong project; reports a merge that never happened), a **pipeline that re-runs locally on daemon failure** (double side effects), and **`workflows reinstall` that blanket-deletes user-authored definitions**. Beneath them the cli-core systemic pattern (exit 0 on failure, missing confirm gates) recurs.

> Verification note: all 6 Blockers were re-read directly against source — `unpack` writes each tar member via `get_gobby_home() / rel` with no containment check (`pack.py:545-558`); the pack tarball is created `w:gz` with no `chmod` while bundling `.secret_salt` + a full PG dump (`pack.py:413-419`); `merge apply`/`abort` call `get_active_resolution()` with no worktree scope and then status-flip + print "Applied merge" (`merge.py:296,314-324`); `_try_daemon_run` returns `None` on a daemon 500 so `run_pipeline` re-runs locally (`pipelines.py:88-96`); `reinstall_workflows` runs `DELETE FROM workflow_definitions` (no source filter) and re-syncs only bundled rows (`workflows/manage.py:46-53`). `%s` placeholders were not flagged (stale CLAUDE.md `$N` drift).

## Findings

### Security & data loss

### [BLOCKER] Zip-slip / arbitrary file write in `gobby unpack`
- **Where:** `src/gobby/cli/pack.py:514-558` (also the `project-gobby` branch `:527-543` and postgres members `:600-609`)
- **Failure mode:** `unpack` extracts each tar member by stripping a known prefix and joining the *raw* member name to a base dir with no containment check: `rel = member.name.removeprefix("gobby/")` → `target = get_gobby_home() / rel` → `target.write_bytes(f.read())`. A malicious member named `gobby/../../../.ssh/authorized_keys` yields `rel = "../../../.ssh/authorized_keys"`, resolving outside `~/.gobby`. It uses manual `tar.extractfile()` + `Path.write_bytes()` (not `tar.extractall(filter="data")`), so none of Python 3.12+'s tar filtering applies, and there is no `is_relative_to`/`commonpath` guard anywhere. The `_archive_would_overwrite` check (`:228-242`) tests `(home / rel).exists()` with the traversing `rel` (usually nonexistent), so even the overwrite prompt is skipped.
- **Why it matters:** `unpack` operates on an untrusted, user-supplied tarball. This is textbook zip-slip — arbitrary file write to any path the user can write (shell profile, cron, ssh key), RCE-grade, and it bypasses the `--force` confirmation.
- **Minimal fix:** Before writing each member, resolve and assert containment (`dest = (base / rel).resolve(); if not dest.is_relative_to(base.resolve()): abort`); reject absolute `rel`, symlinks, and device members; prefer `extractall(..., filter="data")`. Apply to all three target roots.
- **Confidence:** high

### [BLOCKER] Pack tarball containing secrets is written world-readable
- **Where:** `src/gobby/cli/pack.py:413-419` (tar created `w:gz`, no `chmod`); payload at `:41-45,382-394`
- **Failure mode:** `tarfile.open(output_path, "w:gz")` creates the archive with default umask (commonly `0644`, world-readable). The archive bundles `.secret_salt` and `bootstrap.yaml`, a full PostgreSQL logical dump (all task/session/memory data), and `~/.gobby/services` (compose files that can carry credentials). No `chmod` is applied in `pack.py` or `postgres_backup.py`, and the default auto-name drops the archive in the CWD.
- **Why it matters:** On a shared/multi-user host, any other user can read the DB salt, the full hub dump, and config — secret-at-rest exposure.
- **Minimal fix:** Create the archive with restricted mode or `os.chmod(output_path, 0o600)` immediately after; warn that the archive contains secrets.
- **Confidence:** high

### [BLOCKER] `merge apply` / `merge abort` operate on a globally-selected resolution, not the current project's
- **Where:** `src/gobby/cli/merge.py:296` (`merge_apply`), `:353` (`merge_abort`); `src/gobby/storage/merge_resolutions.py:561` (`get_active_resolution`)
- **Failure mode:** Both call `manager.get_active_resolution()` with no `worktree_id`. `get_active_resolution(None)` selects the single most-recently-created pending resolution across *every* worktree/project (`SELECT … WHERE status='pending' ORDER BY created_at DESC LIMIT 1`). `merge_status` correctly scopes by the current worktree, but `apply`/`abort` do not — so running `gobby merge apply`/`abort` from project A, when project B has a newer pending merge, resolves/deletes **project B's** resolution (and `abort`'s `delete_resolution` cascades to its conflicts). The success message names an ID the user never selected.
- **Why it matters:** Wrong-target destructive operation with cascade deletion across projects.
- **Minimal fix:** Compute `worktree_id` the way `merge_start`/`merge_status` do and pass it to `get_active_resolution(worktree_id=...)`; error if none is found *for this scope*.
- **Confidence:** high

### [BLOCKER] `merge resolve --strategy ai` and `merge apply` report success without performing any merge
- **Where:** `src/gobby/cli/merge.py:249-254` (`merge_resolve`), `:314-324` (`merge_apply`); `MergeResolutionManager` is pure CRUD; `MergeResolver` (`src/gobby/worktrees/merge.py`) is never invoked
- **Failure mode:** `merge_resolve` with `strategy == "ai"` prints `Resolving … with AI...`, fetches the resolver only to "validate" it exists, then `update_conflict(conflict.id, status="resolved")` — the comment literally says `# Would call AI resolver here`. No AI resolution, no file write, no git. `merge_apply` flips `merge_resolutions.status` to `"resolved"` and prints `Applied merge: … N file(s) merged` — but that status flip triggers no git merge/commit anywhere. The whole `gobby merge` surface is DB bookkeeping that claims merges happened.
- **Why it matters:** The user is told the branch merged and conflicts resolved when nothing touched the working tree or git; downstream automation keying off "resolved" acts on a merge that never occurred.
- **Minimal fix:** Wire `apply`/`resolve` to the real `MergeResolver`/git path, or make the commands explicitly report they only track resolution metadata and fail loudly on `--strategy ai` until implemented. Don't emit "Applied merge / file(s) merged" for a status-only flip.
- **Confidence:** high — manager is CRUD-only; no git side-effect on status change.

### [BLOCKER] Daemon-side pipeline failure silently re-runs the entire pipeline locally
- **Where:** `src/gobby/cli/pipelines.py:76-99` (`_try_daemon_run`), `:269-307` (`run_pipeline`)
- **Failure mode:** `_try_daemon_run` treats only HTTP `200`/`202` as a real result; for any other status it returns `None`. The daemon route raises `HTTPException(500)` whenever a pipeline *executes but fails* (`servers/routes/pipelines.py:321-323`), and `DaemonClient.call_http_api` returns the 500 response rather than raising. So a daemon-side pipeline failure makes `_try_daemon_run` return `None`, and `run_pipeline` falls through to the **local executor** and re-executes the entire pipeline (`pipelines.py:300+`).
- **Why it matters:** Double execution of side-effecting steps (shell `exec`, task creation) on any failure; the real daemon error is discarded and replaced by a usually-different local failure (no MCP tools/LLM), masking the cause. Pipelines prefer the daemon precisely for MCP access.
- **Minimal fix:** Distinguish daemon-ran-and-failed from daemon-unreachable — propagate non-2xx bodies (print detail + `SystemExit(1)`), and only fall back to local when the daemon is genuinely unreachable.
- **Confidence:** high — route, client, and executor traced end to end.

### [BLOCKER] `gobby workflows reinstall` (no `--type`) permanently destroys user/project-authored workflow definitions
- **Where:** `src/gobby/cli/workflows/manage.py:44-53` (`reinstall_workflows`)
- **Failure mode:** Step 1 runs `DELETE FROM workflow_definitions` with no `source` filter, hard-deleting **all** rows including user- and project-owned definitions. Step 2 (`_run_sync` → `sync_bundled_pipelines`) only recreates *bundled* rows (`source="installed"`). User/project-authored workflows/pipelines are never recreated and are gone. The confirm prompt says only "delete and reinstall … workflow definitions" — it does not warn that non-bundled definitions are unrecoverable.
- **Why it matters:** Irreversible loss of user content from a command whose name ("reinstall") implies restoring bundled state, not wiping custom work — the "templates only refresh Gobby-owned rows; DB is source of truth" contract violated by a blanket delete.
- **Minimal fix:** Scope the delete to Gobby-owned rows (`WHERE source IN ('installed','template')`) mirroring what sync manages, or make the prompt explicit and exclude user-authored rows.
- **Confidence:** high — delete scope vs sync recreate scope verified.

### [IMPORTANT] `workflows reinstall --type rule|agent|variable` deletes from the wrong table
- **Where:** `src/gobby/cli/workflows/manage.py:46-49` + sync map `:86-92`
- **Historical failure (review @ 3f5bccfb0):** For `--type rule`, the delete ran `DELETE FROM workflow_definitions WHERE workflow_type='rule'`. At review time every definition kind still lived in `workflow_definitions`; the typed tables named in the original write-up (`rule_definitions`, `agent_definitions`, `variable_definitions`) did **not** exist yet. The wipe therefore targeted the same table the copy still used, but with a type filter that missed user-authored rows the later split moved. The original claim that those typed tables were already live is false.
- **Current state (epic #18879):** `gobby workflows` is gone. Reinstall is `gobby sync --reinstall [rules|agents|pipelines|variables|all]`, which deletes only bundled `source='installed'` rows from `rule_definitions`, `agent_definitions`, `session_variable_defaults`, and `pipeline_definitions` and re-syncs those domains. User and project rows are preserved.
- **Minimal fix (historical):** Map each type to its real table for the delete (or delegate deletion to the type-specific reinstall helper).
- **Confidence:** high

### [IMPORTANT] `workflows reload` reports success and exits 0 even when the daemon reload fails
- **Where:** `src/gobby/cli/workflows/manage.py:240-262`
- **Failure mode:** When the daemon is running but reload fails (`status != "success"`, non-200, `ConnectError`, `RequestError`), the function prints the error to stderr but does not `return`/raise; it falls through to the local-cache fallback (`:257-262`) which prints `✓ Cleared local workflow cache` and exits 0. Clearing the *CLI's* loader cache does nothing for the running daemon.
- **Minimal fix:** `raise SystemExit(1)` on the daemon-running-but-failed branch; only run the local-cache fallback when the daemon is genuinely not running.
- **Confidence:** high

### [IMPORTANT] Pipeline `approve`/`reject` resume daemon-started pipelines in an MCP-less local executor
- **Where:** `src/gobby/cli/pipelines.py:458` (`approve_pipeline`), `:496` (`reject_pipeline`) use `get_pipeline_executor()` (`:51-73`, `llm_service=None`, no MCP); resume re-runs `execute` (`workflows/pipeline_executor.py:793-834`)
- **Failure mode:** A pipeline started via the daemon (which has MCP/LLM) can pause for approval. `gobby pipelines approve <token>` always builds a *local* executor and resumes there, so the remaining MCP-tool/prompt steps run without MCP tools/LLM — a different execution environment than where the run started. A daemon route (`/api/pipelines/approve/{token}`) exists but the CLI never uses it.
- **Minimal fix:** Have approve/reject prefer the daemon route when healthy, falling back to local only when unreachable.
- **Confidence:** med — resume path and MCP-less local executor confirmed; impact depends on the run having started on the daemon.

### [IMPORTANT] `postgres restore` loads a dump with no integrity check when sidecar metadata/SHA256SUMS are absent
- **Where:** `src/gobby/cli/postgres_backup.py:96-136` (`restore_postgres_backup`), `_expected_dump_sha256` (`:350-359`), `_read_metadata_for_dump` (`:340-347`)
- **Failure mode:** If neither `metadata.json` nor `SHA256SUMS` exists beside the dump, `_expected_dump_sha256` returns `None`, the `if expected_sha256:` checksum block is skipped, and execution falls through to `_run_pg_restore`, loading the dump into the live target DB (`sha256_verified: False` reported after the destructive restore already happened). The only remaining gate, `_verify_dump_with_pg_restore` (`pg_restore --list`), proves only that the file is a well-formed archive — no authenticity/integrity guarantee.
- **Why it matters:** The destructive path silently waives checksum verification for any dump that lost its sidecars or was hand-supplied.
- **Minimal fix:** When `expected_sha256 is None`, refuse by default (require `--allow-unverified` or a present sidecar) instead of restoring.
- **Confidence:** high — full chain traced; no test covers the no-sidecar restore.

### [IMPORTANT] `postgres uninstall --remove-data` permanently deletes the DB volumes with no confirmation
- **Where:** `src/gobby/cli/postgres.py:134-148` (`uninstall_cmd`) → `src/gobby/cli/installers/postgres.py:52-63,215` (`_uninstall_docker`, no prompt)
- **Failure mode:** `gobby postgres uninstall --remove-data` drops `gobby_postgres_data` and `gobby_pgaudit_log` (the entire hub DB + audit log) with no `click.confirm`/`--yes` gate anywhere — unlike `restore_cmd`, which gates correctly.
- **Minimal fix:** Require `click.confirm` (skippable via `--yes`) when `remove_data` is true.
- **Confidence:** high

### [IMPORTANT] `clones delete --force --json` destroys uncommitted work with no confirmation
- **Where:** `src/gobby/cli/clones.py:362` (`if not yes and not json_format:`) → daemon → `src/gobby/clones/git.py:458-467` (`shutil.rmtree` when `force=True`)
- **Failure mode:** The confirm gate is `if not yes and not json_format`, so `--json` suppresses the prompt even without `--yes`. With `--force`, `gobby clones delete <ref> --force --json` `shutil.rmtree`s the clone (bypassing the uncommitted-changes guard) — irreversible loss of unpushed work, no prompt. `--json` signals machine output, not consent.
- **Minimal fix:** Gate confirmation on `--yes` alone (emit the abort as JSON when `--json`); require `--yes` whenever `--force` is set.
- **Confidence:** high

### [IMPORTANT] `export-import import` writes outside the target directory via the `name`/`--from` arguments (path traversal)
- **Where:** `src/gobby/cli/export_import.py:171-176` (single-file), `:189-217` (`dest = target_dir / rel`)
- **Failure mode:** `dest_name = name or source.name`; `dest = target_dir / dest_name` with `name` an unvalidated CLI argument. An absolute `name` discards `target_dir` entirely, and `../..` escapes it, so a crafted invocation copies an attacker-supplied file outside `.gobby/`. Export is safe (`_copy_resource` uses `relative_to`); the import single-file path has no such guard.
- **Minimal fix:** Resolve and assert `dest.resolve().is_relative_to(target_dir.resolve())`; reject absolute/`..` `name` up front.
- **Confidence:** med — requires a hostile `name`; local single-user CLI, so foot-gun/sandbox-escape rather than remote.

### [IMPORTANT] `export-import import` performs no validation of imported content
- **Where:** `src/gobby/cli/export_import.py:161-219`
- **Failure mode:** Import is a raw `copy2` of any file under the source `.gobby/<type>/` (or a single `--from`), with no parse/schema check. A malformed or hostile definition lands in `.gobby/workflows/` and only surfaces later when the loader syncs it (or fails to).
- **Minimal fix:** Validate each file against the corresponding loader/parser before/after copying; restrict to known extensions rather than `rglob("*")`.
- **Confidence:** med — a "copy files between projects" tool may intend some looseness, but the scope flags import validation.

### [IMPORTANT] `cron add` / `cron edit` silently create jobs that never fire on a bad schedule
- **Where:** `src/gobby/cli/cron.py:98-133` (`add_job`), `:263-271` (`edit_job`); `src/gobby/storage/cron.py:88-97` (`compute_next_run` returns `None` on invalid cron), `create_job` (no schedule validation)
- **Failure mode:** A `--schedule` value that isn't a recognized interval is stored verbatim as `cron_expr` with no validation; if it's not valid croniter, `compute_next_run` swallows the `ValueError` and returns `None`, leaving `next_run_at` NULL so the job never runs — yet the CLI prints "Created cron job" (exit 0) and never inspects `next_run_at`.
- **Minimal fix:** After create/update, if a cron schedule was given but `next_run_at is None`, error + `SystemExit(1)`; better, validate the expression up front.
- **Confidence:** high

### [IMPORTANT] `linear sync` / `linear create` exit 0 when the task is not found
- **Where:** `src/gobby/cli/linear.py:497-499` (`linear_sync`), `:639-641` (`linear_create`); `tasks/_utils/resolution.py:11-73` (`resolve_task_id` prints then returns `None`, never raises)
- **Failure mode:** `if not resolved: return` exits the Click command with status 0, so `gobby linear sync <bad-id>` reports an error to stderr yet succeeds. (`github sync`/`github pr` pass the raw id to the service and surface errors as `ClickException` — they don't share this bug.)
- **Minimal fix:** `raise click.ClickException(f"Task not found: {task_id}")`.
- **Confidence:** high

### [IMPORTANT] `skills doc --output` crashes with an unhandled traceback on an unwritable/invalid path
- **Where:** `src/gobby/cli/skills.py:633-635`
- **Failure mode:** `with open(output, "w", …) as f: f.write(content)` has no error handling; a non-existent dir, a directory target, or an unwritable location raises a bare `OSError`/`IsADirectoryError`/`PermissionError` and exits with a traceback. Every other write path in the module (`hooks_disable`/`hooks_enable`, `meta_set`, `new`) wraps writes in try/except + `sys.exit(1)`.
- **Minimal fix:** Wrap the write in `try/except OSError` → clean message + `sys.exit(1)`.
- **Confidence:** high

### [IMPORTANT] `call_skills_tool` maps a non-dict-but-successful MCP result to `None`, which callers report as a failure
- **Where:** `src/gobby/cli/skills.py:50-77` (`return dict(result) if isinstance(result, dict) else None`)
- **Failure mode:** When the daemon returns `{"success": true, "result": <non-dict>}`, the helper returns `None`; callers (`install`/`remove`/`update`/`hub_list`/`search`) then print "Error: Failed to communicate with daemon" and `sys.exit(1)` for a *successful* operation. The failure branch also discards any `error` field the daemon supplied (`"Error: MCP call failed"` with no detail).
- **Minimal fix:** Propagate non-dict results (or distinguish "succeeded with non-dict payload" from "call failed"); surface the daemon `error` field.
- **Confidence:** med — the daemon skills tools appear to return dicts today, so the non-dict branch may be unreachable; the lost-error-detail half is unconditional.

### [IMPORTANT] `register_plan_command` leaks a raw traceback on a malformed/binary plan file
- **Where:** `src/gobby/cli/plans.py:97-98` (runs before the `try` at `:102`), helpers `_plan_id_from_file` (`:267-269`, calls `parse_plan` which raises `PlanParseError(ValueError)`) and `_root_ref_from_file` (`:272-279`, `read_text` raises `UnicodeDecodeError`/`OSError`)
- **Failure mode:** These resolution calls sit outside the only `try` block (whose `except` even lists `PlanParseError`/`OSError`), so a non-conforming or binary plan file escapes as a traceback instead of a clean `click.ClickException`. Exit code is still nonzero, so degraded UX rather than wrong-exit — hence IMPORTANT.
- **Minimal fix:** Wrap `:97-98` in `try/except (PlanParseError, OSError, UnicodeDecodeError, ValueError)` → `click.ClickException`, mirroring `_validate_plan_for_cli`.
- **Confidence:** high

### [IMPORTANT] No test covers `register_plan_command` (the primary CLI write into the `plans` registry)
- **Where:** `src/gobby/cli/plans.py:88-117`; `tests/cli/test_plans.py` only exercises `_root_ref_from_file` helpers
- **Failure mode:** The command that writes a row into the authoritative `plans` table has no direct test (neither happy path nor the malformed-file error path above), so a regression in plan-id/root-ref inference or error handling ships silently.
- **Minimal fix:** Add happy-path and malformed-plan-file tests (the latter pins the traceback fix above).
- **Confidence:** high

## Nits

### [NIT] Unclosed CLI-owned hub DB connections
`pipelines.py:60,357`, `workflows/check.py:133-134`, `workflows/manage.py:42`, `plan.py:170-175` (`_CliEvidenceContext.task_manager`), `init.py:160-200`, `skills.py:30-33` (`get_skill_storage`, ~8 callers), `clones.py:24`, `merge.py:23,58`, `projects.py:18` — all open `open_runtime_hub_database` and never close, while `stages.py`/`profiles.py:_open_manager`/`session_var_manager_context`/`hub_add` close correctly. Short-lived CLI reclaims on exit (NIT) but inconsistent. Confidence: high.

### [NIT] `clones`/`merge`/`projects` mutate the DB directly and several exit 0 on failure
`clones.py:125-127,212-214,267-269,317-319,395-399` (bare `except Exception` → echo → `return`, exit 0; `merge_clone` conflict path exits 0); `merge.py`/`projects.py` write the DB directly (`update_resolution`/`delete_resolution`/`manager.update`/`soft_delete`), bypassing daemon invariants/broadcasts, while `clones list`/`resolve_clone_id` read the DB directly though create/spawn/sync go through the daemon. Replace `return` with `SystemExit(1)`; prefer daemon-mediated mutations. Confidence: high.

### [NIT] `merge_resolve`/`merge_apply` catch `AttributeError` as dead defensive code
`merge.py:267-270,326,377` — `except AttributeError` to mean "method may not exist," but `get_conflict_by_path`/`get_active_resolution` both exist (`merge_resolutions.py:605,561`), so this swallows real `AttributeError`s from anywhere in the `try` and reports a misleading "not found." Remove the branches. Confidence: high.

### [NIT] `resolve_clone_id` prefix match is global across all projects
`clones.py:419` (`list_clones()` unscoped) — a unique cross-project prefix resolves silently to another project's clone, feeding spawn/sync/merge/delete. Scope `list_clones(project_id=...)`. Confidence: med.

### [NIT] `cron list` schedule column has a dead `or` chain
`cron.py:54` — `job.cron_expr or f"every {interval_seconds}s" or job.run_at or "?"`; the f-string is always truthy, so `once` jobs render `every Nones` and the `run_at` branch is unreachable. Branch on `schedule_type`. Confidence: high.

### [NIT] `cron edit` schedule parsing diverges from `cron add`
`cron.py:264` recognizes only the `s` suffix while `add_job:103-111` accepts `s`/`m`/`h`, so `gobby cron edit --schedule 5m` is treated as a cron expr (then never fires). Factor a shared parser. Confidence: high.

### [NIT] `communications add` raw-JSON fallback accepts non-dict config
`communications.py:203-212` — `json.loads` accepts any JSON, then `config.items()` raises `AttributeError` (uncaught) for a list/int. Assert `isinstance(config, dict)` → clean error. Confidence: med.

### [NIT] `_human_size` integer-truncates before the TB branch
`pack.py:633-639` — `size //= 1024` floors each iteration, so TB shows whole units and sub-unit precision is lost at the boundary. Carry a float for display. Confidence: med.

### [NIT] `is_qdrant_installed` bypasses `GOBBY_HOME`
`services.py:39-46` — `home = gobby_home or Path("~/.gobby").expanduser()` hardcodes `~/.gobby` instead of `get_gobby_home()` (which honors `GOBBY_HOME`), so it reports wrong status under a custom home. Use `get_gobby_home()`. Confidence: high.

### [NIT] `list_workflows` mislabels user-authored global workflows as "installed"
`workflows/inspect.py:65` — `"project" if is_project else "installed"`; user-global locations (`~/.gobby/workflows`, where `import_workflow --global` writes) show as bundled. Derive source from the actual directory. Confidence: med.

### [NIT] `skills` magic caps and cosmetic mismatches
`skills.py:121` (`fetch_limit = 10000 if tags` can silently truncate tag matches above 10k — implausible today), `:473-503` (`meta set` echoes the raw input string even when stored as parsed JSON). Paginate / push tag filter to storage; echo the parsed value. Confidence: high (behavior) / low (impact).

### [NIT] Stale line-number comment and lifecycle-word shadowing
`pipelines.py` `import_pipeline` (`assert … # guarded by check on line 570` — the guard is now ~`:837`); `build.py:253-289` (`build_command` routes `input_ref` literally equal to `stop`/`resume`/`clean`/`restart` as lifecycle actions — documented reserved words, negligible collision risk with numeric/UUID/path inputs). Drop the line number; noted for completeness. Confidence: high / med.

### [NIT] Restore-decline and missing-sidecar restore paths are untested
`tests/cli/test_postgres_cli.py:187` only covers `--yes`; `tests/cli/test_postgres_backup.py` covers verified/mismatch/unmanaged but not the `click.confirm`→abort path or the no-sidecar branch (the IMPORTANT above). Add both. Confidence: high.

## Context notes (verified, deliberately NOT flagged)

- **Build CLI → shared-service contract holds.** The CLI local fallback (`build.py` `build_command`, `_run_build_*`) and the HTTP route both call the same `gobby.build` entry points and return the same `BuildResult`/control results; destructive `clean`/`restart` confirm once at the CLI then pass `yes=True` downstream. No reimplementation, no inconsistent-state path (every local path uses `try/except ValueError → ClickException` with `finally: db.close()`).
- **MCP `add-server`/`import-server` command passthrough is a daemon-side concern.** `mcp_proxy.py:259-336,508-600` forward `command`/`args`/`env` unvalidated, but `transport` is `click.Choice`-constrained and the spawn/validation boundary is the daemon route — already flagged in `docs/reviews/servers-routes.md`. Re-flagging here would duplicate against the wrong layer.
- **Skill install zip-slip is not present in the CLI.** `install`/`update` forward `source` to the daemon; the local `validate` reads files but never extracts an archive. Extraction lives in `skills/loader.py::extract_zip`, which has a correct `relative_to` zip-slip guard.
- **No secret leakage in mcp/skills/linear/github/communications.** `import-server` prints only missing-secret *names*; `add-server`/`hub add` echo only names/references; secret prompts use `hide_input=True` and travel in the HTTP body; result DSNs pass through `_redact_dsn`; postgres dump/restore run via `docker exec` (no host-side `PGPASSWORD` on the command line).
- **`sync.py` is bundled-content sync** (skills/prompts/rules/agents/workflows, integrity-gated) — not the task/memory JSONL round-trip the brief anticipated; its integrity check, type filtering, and nonzero-exit handling are correct.

## Systemic patterns

1. **Untrusted-input handling without containment or permission hardening.** `pack.py` is the epicenter: `unpack` extracts tar members with no path/symlink validation (zip-slip), and `pack` writes a secret-bearing archive world-readable. `export-import import` joins an unvalidated `name` to the target dir with no containment. Each treats archive/file I/O as trusted; the fix shape is resolve-then-contain + restrictive permissions.

2. **"Reports success without doing the work."** The entire `gobby merge` surface flips DB status fields and prints merge-completed messages while performing zero git operations — the most dangerous correctness lie here. The pipeline daemon-failure fallback is the inverse: it silently *does extra work* (a full local re-run) while masking the real failure.

3. **Exit 0 on failure (carryover from cli-core).** `workflows reload` falls through to a green checkmark after a failed daemon call; `linear sync`/`create` `return` on unresolved task; `cron add`/`edit` report success for jobs that can never fire; `clones` commands `except Exception → return`. Trustworthy exit codes require `click.ClickException`/`SystemExit(1)` on every error path.

4. **Missing or leaky confirm gates on destructive ops.** `postgres uninstall --remove-data` (no gate), `clones delete --json` (waives the gate), and `restore` with absent sidecars (waives checksum verification) all let irreversible operations proceed without the safety the surrounding code applies elsewhere. `build`/`postgres restore --yes` show the correct pattern.

5. **Blanket DB deletes / direct DB writes from CLI handlers that ignore ownership and the daemon.** `workflows reinstall` deletes all `workflow_definitions` (and the wrong table for non-workflow types), destroying user content; `merge`/`projects`/`clones` write the hub DB directly, bypassing daemon side-effects. `stages.py`/`profiles.py` (delegate to the manager, close the DB, map `ValueError → ClickException`) are the clean model the others should follow.

6. **Create-vs-update asymmetry and unclosed connections** recur (`cron add` vs `cron edit` schedule parsing; the unclosed `open_runtime_hub_database` connections across ~10 handlers) — the same shapes flagged in cli-core, confirming they are package-wide rather than local.
