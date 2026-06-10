# Review: cli core (daemon / agents / rules / sessions / tasks / memory / worktrees / status)

- **Scope:** `src/gobby/cli/` core command groups (~8,260 lines): `daemon.py` (start/stop/restart/status), `agents.py`, `rules.py`, `sessions.py`, `tasks/` (crud, main, expand, ai, commits, search, `_utils/{rendering,listing,tree}`), `memory/` (graph, crud, maintenance, export, dream), `worktrees.py`, plus shared CLI infrastructure `utils.py` and `ui.py`. **Split boundary:** install/services/postgres/pipelines/skills/clones/merge/projects/build and the `installers/` subtree are out of scope here — they belong to `cli-build-ops` (#15772). Split across 4 parallel reviewers; synthesized and Blocker-verified against source.
- **Reviewer:** Claude (Fable 5) — 4 general-purpose review agents + synthesizer verification
- **Commit / branch:** `0.5.0` @ 834eb8ded
- **Summary:** 4 Blocker · 21 Important · 11 Nit — the dominant systemic gap is **CLI commands that exit 0 on failure** (a failed daemon call, not-found resource, or partial operation prints to stderr then `return`s, so scripts/CI can't detect failure). Above that sit four real-behavior bugs: `gobby start` silently kills a healthy daemon, `rules import <file>` re-syncs the whole parent directory, a constructible parent-hierarchy cycle crashes all task listing, and `memory dedupe` does unconfirmed cross-project deletion.

> Verification note: all 4 Blockers were re-read directly against source — `start` runs `kill_all_gobby_daemons()` before the "already running" guard (`daemon.py:427` vs `:433-447`); `import_rules` passes `path.parent` to `sync_bundled_rules` whose `_iter_active_rule_files` does `rglob("*.yaml")` (`rules.py:213`, `sync_rules.py:50-58`); `tree.py`'s `traverse`/`collect_children` recurse with no visited guard (`:85-88,:164-168`); `dedupe_memories` groups by `content.strip()` across all projects and deletes with no `--yes`/confirm (`maintenance.py:61-118`). `%s` placeholders were not flagged (stale CLAUDE.md `$N` drift).

## Findings

### [BLOCKER] `gobby start` unconditionally kills the running daemon — the "already running" guard is dead code
- **Where:** `src/gobby/cli/daemon.py:427` (`killed_count = kill_all_gobby_daemons()`) followed by `pid_file.unlink(missing_ok=True)`, before the stale-PID guard at `:433-447`
- **Failure mode:** `start` calls `kill_all_gobby_daemons()` (SIGTERM/SIGKILL to every `gobby.runner` process) and unlinks the PID file *before* the `if _is_process_alive(pid): … "Daemon already running" sys.exit(1)` check. By the time that guard runs, the live daemon is already dead and its PID file gone, so the guard never fires. Running `gobby start` against a healthy daemon silently tears it down and starts a fresh one instead of refusing.
- **Why it matters:** Double-start protection is the guard's entire purpose and it is unreachable. A user/script running `gobby start` expecting a no-op causes an unplanned restart — dropping in-flight agent work, WebSocket clients, and the dev UI server.
- **Minimal fix:** Move the stale-PID / "already running" detection before `kill_all_gobby_daemons()`; only kill when the PID is dead/stale or not a gobby process, otherwise print "already running" and `sys.exit(1)` without killing.
- **Confidence:** high

### [BLOCKER] `rules import <file>` ignores the named file and re-syncs the entire parent directory (and silently no-ops on `.yml`)
- **Where:** `src/gobby/cli/rules.py:197-225` (`sync_bundled_rules(db, rules_path=path.parent)` at `:213`); `src/gobby/workflows/sync_rules.py:50-58` (`_iter_active_rule_files` does `root.rglob("*.yaml")`)
- **Failure mode:** The command validates a single `FILE` argument, then passes `path.parent` to `sync_bundled_rules`, which recursively globs every `*.yaml` under that directory and syncs them all into `workflow_definitions` (plus a table-wide repair UPDATE and orphan soft-delete of other `gobby`-tagged rows). `gobby rules import ~/myrule.yaml` imports/updates every rule YAML sibling, not the one named. Separately, because the glob is `*.yaml` only while the CLI accepts `.yml`, a directory whose target is `foo.yml` matches zero files and reports `Imported rules: 0 new, 0 updated` (exit 0) — a successful-looking no-op.
- **Why it matters:** Silent mass-mutation of the rule registry from a command the user believes is scoped to one file; or a silent import-of-nothing for valid `.yml` input.
- **Minimal fix:** Add a single-file import path that loads/validates/upserts just `path` (regardless of `.yaml`/`.yml`), instead of passing `path.parent` to the directory-wide sync.
- **Confidence:** high — both the call site and the `rglob("*.yaml")` confirmed.

### [BLOCKER] Parent-hierarchy cycle causes infinite recursion in task tree rendering
- **Where:** `src/gobby/cli/tasks/_utils/tree.py:85-88` (`traverse`), `:119-141` (`compute_prefix`), `:164-168` (`collect_children`) — all recurse over parent→children with no visited-set/depth guard; cycle is constructible via `src/gobby/cli/tasks/crud.py:578` (`update_task` sets `parent_task_id` with no cycle validation in `storage/tasks/_updates.py` or `_manager.py`)
- **Failure mode:** A `parent_task_id` cycle (e.g. `update #1 --parent #2` then `update #2 --parent #1`) makes these walkers recurse forever → `RecursionError`, crashing `tasks list`, `tasks ready`, `tasks blocked`, and any `--tree` path on every invocation. The storage path-cache layer (`storage/tasks/_path_cache.py`) already carries a `max_depth=100` guard for exactly this hazard; the tree renderers do not, and `doctor`'s cycle check only inspects the *dependency* graph, not the parent hierarchy.
- **Why it matters:** Normal, allowed CLI operations can wedge the task graph into a state where core listing commands crash every time, with no `doctor` diagnosis.
- **Minimal fix:** Add a `visited: set[str]` guard to the three recursive walkers, and reject parent cycles at write time in `update_task` (refuse a `parent_task_id` that is the task or one of its descendants).
- **Confidence:** high — recursion has no guard (read in full); the write path has no cycle check.

### [BLOCKER] `memory dedupe` deletes across all projects with no confirmation
- **Where:** `src/gobby/cli/memory/maintenance.py:61-118` (`dedupe_memories`; decorator has only `--dry-run`, no `--yes`); `_list_all_memories` (`:34-55`) passes no `project_id`; `delete_memory(memory_id)` deletes by id alone
- **Failure mode:** `gobby memory dedupe` (no flags) immediately and irreversibly deletes with no interactive confirmation. It lists ALL memories regardless of project and groups duplicates by `content.strip()` alone, keeping the earliest-created entry. The same fact legitimately stored in two different projects (different `project_id`, identical content) is treated as a duplicate, so one project silently loses its copy. Every other destructive memory command (`clear_graph`, `invalidate` in `graph.py`) gates on `click.confirm(..., abort=True)` unless `--yes`; the one that actually deletes rows has neither confirm nor project scoping.
- **Why it matters:** Silent, unconfirmed, cross-project data loss.
- **Minimal fix:** Add `--yes/-y` + `click.confirm(abort=True)` before the delete branch; group by `(project_id, content)` (or scope to the current project unless `--all-projects --yes`).
- **Confidence:** high

### [IMPORTANT] Silent-success on failure across agents / sessions / tasks / memory commands (exit 0)
- **Where:** `agents.py` (`show_agent_run:459-461`, `agent_status:506-508`, `stop_agent:543-563`, `kill_agent:600-622`, `check_agent:670-674`); `sessions.py` (`show_session`/`show_messages`/`delete_session` not-found + `:283,:288`); `tasks/crud.py` (`show_task:499`, `update_task:578`, `reopen_task_cmd:702`, `de_escalate_cmd:809`, `close_task_cmd:632-696` tracks `failed_count` but exits 0, `delete_task:743-796`); `tasks/main.py:239-279` (`doctor` always exits 0 even with integrity issues); `tasks/ai.py` (`validate` error paths `:53,:134,:144,:155,:215`); `memory/crud.py` (`delete`/`show`/`update` errors to **stdout** + exit 0); `worktrees.py` (`:113-114,223-225,304-305,361-362,420-423,451-452` print error then `return`)
- **Failure mode:** Each detects a real failure (not-found, daemon call failed, operation returned `success: False`, integrity issues), prints a message (often via `err=True`, sometimes to stdout), then `return`s — yielding exit code 0. Sibling code in the same files does it correctly (`show_agent_definition:349`, `restore_transcript:652`, graph/dream use `click.ClickException`), so the idiom exists and is applied inconsistently.
- **Why it matters:** Scripts/CI (`gobby agents stop X && next-step`, `gobby tasks close #N && deploy`) treat failures as success; `doctor` can't gate automation. This is the single largest correctness gap in the CLI.
- **Minimal fix:** Replace the failure-branch `return`s with `raise click.ClickException(...)` / `raise SystemExit(1)`; route a shared error-mapping helper (connect → HTTP → timeout → tool-failure → message + exit 1).
- **Confidence:** high

### [IMPORTANT] `rules enable`/`disable` mutate the DB directly; a running daemon may not pick up the toggle
- **Where:** `src/gobby/cli/rules.py:167-177` (`enable_rule`), `:182-192` (`disable_rule`) — `manager.update(row.id, enabled=...)` against a CLI-owned `open_runtime_hub_database` connection
- **Failure mode:** These write `enabled` straight to `workflow_definitions` rather than through the daemon. If the live daemon's `RuleEngine` caches active rules in memory, the toggle won't take effect until reload/restart, so `gobby rules disable X` reports success while X keeps firing (or a re-enabled blocker keeps blocking).
- **Why it matters:** User-visible drift between the success message and actual enforcement; potential safety issue for blocking rules.
- **Minimal fix:** Route enable/disable through a daemon endpoint/MCP tool that updates the DB and invalidates the engine cache; confirm `RuleEngine` reload behavior first.
- **Confidence:** med — direct-DB write verified; whether the engine caches vs reads live per-event needs confirmation.

### [IMPORTANT] `create_handoff --notes` is accepted but never persisted
- **Where:** `src/gobby/cli/sessions.py:370-588` — `notes` is only echoed at `:582-583`; never written to `summary_markdown`, the output file, or `handoff_ctx`
- **Failure mode:** `gobby sessions handoff --notes "…" --output all` prints the notes once and discards them; the durable handoff artifact omits the operator's notes.
- **Minimal fix:** Append `notes` (e.g. a `## Notes` section) into `full_markdown`/`handoff_ctx` before the save-to-db / save-to-file blocks.
- **Confidence:** high

### [IMPORTANT] `commit link`/`unlink` resolve the SHA against the wrong directory; `unlink` falsely reports success
- **Where:** `src/gobby/cli/tasks/commits.py:34` (`link_commit`), `:58` (`unlink_commit`) — neither passes `cwd`, unlike `auto_link` (`:106`) / `diff_cmd` (`:145`) which thread `project_path`
- **Failure mode:** The CLI runs in-process against the hub DB, so `normalize_commit_sha` shells `git` against `Path.cwd()`. Invoked from a subdirectory/worktree/non-repo cwd: `link` raises `ValueError("Invalid or unresolved commit SHA")`, and `unlink` silently no-ops (`normalize_commit_sha` → `None`, nothing removed, returns `False`) while the CLI unconditionally prints `"Unlinked commit … from task …"`.
- **Minimal fix:** Resolve `cwd` via `get_project_context` (as `auto_link`/`diff_cmd` do) and pass it; have `unlink` echo a "not found / nothing to unlink" message when unchanged.
- **Confidence:** high

### [IMPORTANT] `resolve_agent_run_id` / `find_tasks_by_prefix` interpolate user input into `LIKE` without escaping wildcards
- **Where:** `src/gobby/cli/agents.py:97-133` (`… WHERE id LIKE %s`, `(f"{run_ref}%",)`); `src/gobby/storage/tasks/_read.py:54-60` (`find_tasks_by_prefix`, `WHERE id LIKE '{prefix}%'`) reached from `src/gobby/cli/tasks/_utils/resolution.py:57`
- **Failure mode:** Values are parameterized (no injection) but LIKE metacharacters are not escaped: `_` matches any single char, `%` matches any run. A ref containing `_`/`%` can match unintended runs/tasks, and an empty/whitespace task ref → `LIKE '%'` matches the entire (unscoped) tasks table. For `stop`/`kill` this can target the wrong agent run.
- **Why it matters:** Wrong/ambiguous resolution on destructive commands; UUID ids make it unlikely but reachable with malformed input.
- **Minimal fix:** Escape `%`/`_` (with `ESCAPE` clause) or restrict prefixes to `[0-9a-f-]`; reject empty/whitespace refs before prefix lookup.
- **Confidence:** high (missing escape) / med (real-world exploitability).

### [IMPORTANT] `status` reports a zombie daemon process as "running"
- **Where:** `src/gobby/cli/daemon.py:696` (`os.kill(pid, 0)`) instead of the zombie-aware `_is_process_alive` (`utils.py:493-509`, used by `health`/`stop_daemon`)
- **Failure mode:** `os.kill(pid, 0)` succeeds on a defunct/zombie process, so a crashed-but-unreaped daemon is reported as running; `status` is the outlier among the three lifecycle commands.
- **Minimal fix:** Use `_is_process_alive(pid)` in `status`.
- **Confidence:** high

### [IMPORTANT] Worktree commands exit 0 on failure and let read timeouts escape
- **Where:** `src/gobby/cli/worktrees.py:43-61` (`_call_worktree_tool` catches only `ConnectError`/`HTTPStatusError`, not `ReadTimeout`/`RequestError`); error branches `:113-114,223-225,304-305,361-362,420-423,451-452` print + `return` (exit 0); `create_worktree:118` raises on `HTTPStatusError` but not `ConnectError`, so even the one "correct" command is partial
- **Failure mode:** Daemon-down / HTTP-error / tool-failure / timeout all surface a message but exit 0; a slow daemon (`ReadTimeout`) either degrades to "Error: …" + exit 0 or, in any future call lacking the broad catch, dumps a traceback.
- **Minimal fix:** Funnel all six commands through one helper mapping connect/HTTP/timeout/tool-failure → clean message + `SystemExit(1)`.
- **Confidence:** high (exit-0) / med (timeout path).

### [IMPORTANT] `worktrees claim`/`release` mutate state via direct DB access while siblings route through the daemon
- **Where:** `src/gobby/cli/worktrees.py:259` (`manager.claim(...)`), `:278` (`manager.release(...)`) use the in-process `LocalWorktreeManager`, whereas create/delete/sync/cleanup/stats go through `_call_worktree_tool` → daemon
- **Failure mode:** Two worktree mutations write the DB directly from the CLI, bypassing daemon caches/events/invariants — diverging the daemon's view from the DB and behaving inconsistently when the daemon is down.
- **Minimal fix:** Route claim/release through the gobby-worktrees MCP tools like the other mutating commands.
- **Confidence:** med — confirm the MCP-tool equivalents exist and the daemon emits events on worktree state changes.

### [IMPORTANT] CLI expansion commands leave runs stuck "running" on failure and dump raw tracebacks
- **Where:** `src/gobby/cli/tasks/expand.py:84-117` (`compile_cmd`), `:124-134` (`apply_cmd`), `:195-212` (`resume_cmd`) — no try/except around `service.compile_run`/`compile_and_apply_run`/`apply_run`, which raise after `run_manager.start(run_id)` flipped the run to running
- **Failure mode:** A validation/generation failure surfaces as a raw traceback and leaves the run in `running` forever. The MCP path for the same service deliberately calls `run_manager.fail(run_id, str(e))` (`mcp_proxy/tools/tasks/_expansion.py:263`); the CLI diverges.
- **Minimal fix:** Wrap the calls; on exception call `run_manager.fail(run.id, str(exc))` and `raise click.ClickException(str(exc))`.
- **Confidence:** high

### [IMPORTANT] `tasks expand compile` always reports success-shaped output
- **Where:** `src/gobby/cli/tasks/expand.py:84-117`
- **Failure mode:** After `compile_run`, the command unconditionally prints `Status`/`Phases`/`Tasks` and exits 0 without checking `run.status`/`run.error`; a non-completed run (e.g. the early-return dev-only branch) looks successful.
- **Minimal fix:** If `run.status != "completed"` or `run.error`, echo and `raise click.ClickException(...)`.
- **Confidence:** med — depends on whether a non-raising failure path through `compile_run` exists.

### [IMPORTANT] `create_task` swallows all dependency-attach errors; task created with partial/no dependencies
- **Where:** `src/gobby/cli/tasks/crud.py:485-494` (the `for blocker_ref in depends_on:` loop wrapped in `except Exception` → `Warning:` + continue)
- **Failure mode:** The task is created first; a bad `--depends-on` ref or failed `add_dependency` only logs a warning and continues, so the user gets a task with none/partial of the requested dependencies, exit 0. The broad `except` also masks DB errors.
- **Minimal fix:** Catch `ValueError` specifically, collect failures, and `raise click.ClickException` if any dependency could not be attached.
- **Confidence:** high

### [IMPORTANT] `tasks validate`: `--max-iterations` is dead and the command exits 0 on every error
- **Where:** `src/gobby/cli/tasks/ai.py:26` (`--max-iterations` never referenced after the signature; body uses `MAX_RETRIES = 3` ~`:180`); error paths `:53,:134,:144,:155,:215` print then `return`/swallow with no nonzero exit
- **Failure mode:** Setting `--max-iterations` is silently ignored (retries always 3), and every failure path (not-found, summary read error, missing changes summary, validator init error, terminal `except Exception`) exits 0.
- **Minimal fix:** Wire `max_iterations` into the retry count; `raise SystemExit(1)` on each error path and narrow the terminal `except`.
- **Confidence:** high

### [IMPORTANT] `memory restore` reports success/no-op even when import silently fails
- **Where:** `src/gobby/cli/memory/export.py:182` (`count = backup_mgr.import_sync(force=force)`) consuming `src/gobby/sync/memories.py:274` (`import_sync` wraps the body in `except Exception: logger.warning(...); return 0`)
- **Failure mode:** A corrupt JSONL, malformed row, or DB write error during restore is swallowed and surfaces as `count == 0`, so the CLI prints "No memories restored." (exit 0) — indistinguishable from a legitimately empty file in the one scenario (disaster recovery) where failure must be visible.
- **Minimal fix:** Have `import_sync` raise (or return a typed result distinguishing skipped vs errored) and surface a nonzero exit + clear message on error.
- **Confidence:** med — the swallow is in `sync/memories.py`, just outside scope, but it is the failure this command surfaces.

### [IMPORTANT] `memory rebuild-graph` daemon calls are unguarded and ignore the overall timeout per request
- **Where:** `src/gobby/cli/memory/graph.py:188` (initial `call_http_api`) and the poll loop `:223`, neither wrapped in the `except (httpx.HTTPError, ConnectionError, OSError, ValueError)` that `clear_graph`/`graph_counts`/`invalidate` use; each status request uses `timeout=30.0` with no cap by the remaining `--timeout` deadline (unlike `dream._wait_for_completion:160`)
- **Failure mode:** A daemon drop between the health check and these calls yields a raw traceback; a blocked status request can run ~30s past the advertised `--timeout`.
- **Minimal fix:** Wrap both calls in the same `except (...)` → message + `SystemExit(1)`; cap each request at `min(30.0, deadline - now)`.
- **Confidence:** high (unguarded) / med (timeout overrun).

### [IMPORTANT] `memory fix-null-project` writes directly to the DB from a CLI command
- **Where:** `src/gobby/cli/memory/maintenance.py:124-196` (`runtime_hub_database`, `db.fetchall`, `db.transaction()` with `UPDATE memories SET project_id …`)
- **Failure mode:** A routine, re-runnable subcommand issues raw SELECT/UPDATE against the hub, bypassing manager-side invariants/listeners (cache invalidation, index/graph requeue) that `delete_memory`/`create_memory` go through. Sits next to `dedupe`, which uses the manager.
- **Minimal fix:** Move the lookup+update behind a `MemoryManager` method (or daemon endpoint), or explicitly document it as one-shot repair tooling.
- **Confidence:** med — direct-DB pattern unambiguous; "sanctioned tooling" is a judgment call.

## Nits

### [NIT] Unclosed CLI-owned hub DB connections
`rules.py:29-39`, `agents.py:29-38,97-133` (`resolve_agent_run_id` opens a *second* connection), `agents.py:763`, `sessions.py:20-23`, `worktrees.py:29-32` (`get_worktree_manager`) — all return managers over a freshly-opened `open_runtime_hub_database` that is never closed, while `session_manager_context`/`runtime_hub_database` (used by `restore_transcript`) close correctly. Short-lived CLI processes have the OS reclaim the sockets, so low-impact, but it's an inconsistent contract and a latent pool-exhaustion risk for any long-lived caller. Confidence: high.

### [NIT] Broad `except Exception` swallowing in PID-file handling
`daemon.py:444-447,680-684,692,801-803` — PID parse/inspect wraps logic in bare `except Exception: pass`/`unlink`, masking permission/psutil errors as "no daemon." Narrow to `(ValueError, OSError, psutil.Error)` + `logger.debug`. Confidence: med.

### [NIT] `is_port_available` checks `localhost` even when the daemon binds elsewhere
`utils.py:293-316` called at `daemon.py:455,460` with no host; a non-loopback `bind_host` could read a held port as available. Default `bind_host` is `localhost`, so unaffected by default. Pass `config.bind_host` through. Confidence: low.

### [NIT] Files approaching the monolith cap
`utils.py` (987), `tasks/crud.py` (905), `daemon.py` (831), `agents.py` (833) — under the 1,000-line cap but trending; `utils.py`'s process/PID/UI-server clusters and the `tasks/crud.py` command bodies are natural extraction seams. No action required now. Confidence: high.

### [NIT] `restore_transcript --all` skipped-record display is shape-fragile
`sessions.py:677-679` — `sid[:12]` is safe today (only restored items, which carry `session_id`) but skipped items carry `external_id`; normalize result dicts to a `display_ref`. Confidence: med.

### [NIT] Unvalidated `--status`/`--role`/`--source` filters
`agents.py:399-444` (`list_agent_runs`), `sessions.py` (`list_sessions`/`show_messages`) — no `click.Choice`, so a typo silently returns an empty list instead of an error. Add `type=click.Choice([...])`. Confidence: med.

### [NIT] `close_task_cmd` and `delete_task` use divergent multi-ref parsing
`tasks/crud.py:651-657` (hand-rolled comma split) vs `:756` (`parse_task_refs`). Route both through `parse_task_refs`. Confidence: high.

### [NIT] `task_stats` `by_priority` silently absorbs out-of-range priorities
`tasks/crud.py:346` — a priority outside `{0..4}` creates a stray dict key never surfaced in the printed buckets, so its count vanishes. Clamp/validate or add an "other" bucket. Confidence: med.

### [NIT] Escalated/stage-filtered listing capped at a hardcoded 10000 fetch
`tasks/crud.py` `list_tasks` (`limit=10000 if stage_name or escalated`) + client-side `[:limit]` — escalated tasks beyond the first 10000 fetched are invisible regardless of `--limit`. Push the filter into the storage query. Confidence: high (behavior) / low (impact).

### [NIT] `commit link` echoes the raw input SHA, not the normalized stored SHA
`tasks/commits.py:36` — a full 40-char SHA is echoed while the short form is stored, so `list`/`unlink` then show/expect the short form. Echo `updated_task.commits[-1]`. Confidence: med.

### [NIT] `memory create` drops to a global (null-project) memory when no `-p` is given
`memory/crud.py:26` — `project_id = … if project_ref else None`, so unlike `recall`/`list`/`stats` (which pass `project_ref` through `resolve_project_ref`, mapping `None` → current context), `create` with no `-p` stores a global memory. Call `resolve_project_ref` unconditionally, or confirm explicit-scoping-only is intended. Confidence: low.

## Systemic patterns

1. **No non-zero exit codes on failure.** The dominant correctness gap across `agents.py`, `sessions.py`, `worktrees.py`, `tasks/{crud,main,ai,expand}`, and `memory/{crud,export,graph}`: failure branches `click.echo(..., err=True)` (sometimes to stdout) then `return`, yielding exit 0. `doctor` and `close`/`delete` even track failure counts and still exit 0. The correct idiom (`click.ClickException` / `SystemExit(1)`) exists in the same files. A shared error-mapping helper would fix the whole class.

2. **CLI reaching past the daemon into the DB, with uneven discipline.** Many commands resolve refs via `open_runtime_hub_database` directly (an accepted read convention), but the *write* paths cross the line: `rules enable/disable` and `worktrees claim/release` mutate state directly (bypassing engine-cache invalidation / daemon events), and `memory fix-null-project` issues raw UPDATEs. These are where direct-DB access turns into real behavior/consistency bugs. Connections opened this way are also never closed.

3. **Inconsistent liveness / error-handling idioms within a command group.** Three liveness checks coexist (`_is_process_alive`, raw `os.kill(pid, 0)`, cmdline-string matching); daemon-call guarding is wrapped in some memory/worktree commands and bare in their siblings; the CLI marks expansion runs `running`-forever where the MCP path marks them `failed`. The right idiom usually exists two functions away.

4. **Recursive graph walks without cycle guards in the CLI layer.** `tree.py`'s three walkers recurse on the parent hierarchy with no visited set, even though the storage `compute_path_cache` already guards the same hazard and nothing prevents constructing a parent cycle via `update_task`.

5. **Unescaped `LIKE` prefix matching.** Both `resolve_agent_run_id` and `find_tasks_by_prefix` interpolate user refs into `LIKE` patterns without escaping `%`/`_` (and without rejecting empty refs), so a metacharacter or blank input can resolve to the wrong row — on destructive `stop`/`kill`/`close`/`delete` commands.

6. **Scope/contract mismatch between a CLI argument and the underlying operation.** `rules import` (a single-file argument driving a recursive directory sync + table-wide repair) is the sharpest case; `create_handoff --notes` (accepted, discarded) and `tasks validate --max-iterations` (accepted, ignored) are the same shape — the flag/argument promises something the implementation doesn't honor.
