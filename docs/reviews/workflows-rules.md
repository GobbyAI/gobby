# Review: workflows rule engine

- **Scope:** `src/gobby/workflows/` rule layer — `engine/core.py`, `engine/templating.py`,
  `engine/effects.py`, `engine/enforcement.py`, `engine/blocked_tool_recovery.py`,
  `enforcement/blocking.py`, `safe_evaluator.py`, `condition_helpers.py`, `selectors.py`,
  `definitions.py`, `loader.py`, `loader_sync.py`, `loader_validation.py`,
  `loader_discovery.py`, `loader_cache.py`, `sync_rules.py`, `sync_variables.py`,
  `template_hashes.py`, `template_writer.py`, `templates.py`, `workflow_templates.py`,
  `lobster_compat.py`, `observers.py`, `observer_*.py`, `git_utils.py`, `hooks.py`,
  `claimed_task_skills.py`, `verification_evidence.py`, `agent_resolver.py`,
  `task_actions.py`, `task_claim_state.py`. Cross-seam reads into `mcp_proxy/` (call_tool
  canonicalization), `hooks/rule_evaluator.py`, and `adapters/claude_code.py` where the
  rule layer's contract is actually honored or broken. **Split boundary:** pipeline
  executor (`pipeline_executor.py`, `pipeline_*.py`, `pipeline/`), `state_manager.py`,
  `dry_run.py`, `summary_actions.py`, and `webhook*.py` belong to the workflows-engine
  leaf (#15776) and were read only as consumers, not reviewed for their own findings.
- **Reviewer:** Claude Fable 5 — 7-agent parallel fan-out, all Blockers synthesizer-verified link-by-link against source.
- **Commit / branch:** `0.5.0` @ HEAD `722ecad14` (working tree clean at review time).
- **Summary:** 6 Blocker · 24 Important · 13 Nit — the rule engine's correctness gates
  are real, but its *failure posture is fail-open at every layer above the evaluator*,
  and two independent enforcement bypasses defeat the flagship progressive-discovery
  guard and any block-last rule. The template→DB sync can silently and permanently wipe
  bundled enforcement.

## Findings

### [BLOCKER] Progressive-discovery enforcement bypassed by decoy `server_name`/`tool_name` in nested args
- **Where:** `src/gobby/workflows/engine/templating.py:68-71` (conditional re-injection), defeating `src/gobby/install/shared/workflows/rules/progressive-discovery/require-schema-before-call.yaml:9`; real dispatch diverges at `src/gobby/mcp_proxy/_call_tool_wrapper.py:115-151` and `src/gobby/mcp_proxy/server.py:212-214`; before-tool event built at `src/gobby/mcp_proxy/services/result_handling.py:34-39`.
- **Failure mode:** An agent calls
  `call_tool(server_name="gobby-tasks", tool_name="delete_task", arguments={"task_id":"…", "server_name":"gobby-skills", "tool_name":"list_tools"})`.
  1. Real dispatch: `canonicalize_call_tool_wrapper` prefers **top-level** routing (`_pick_wrapper_value`, `_call_tool_wrapper.py:50-55,119-120`). Because routing came from top-level, `wrapper_route_from_nested` is `False`, so the wrapper-field strip at `:143-151` is skipped — the decoy `server_name`/`tool_name` keys **remain inside `arguments`**. `server.py:212-214` therefore executes the real `gobby-tasks:delete_task`, and `prepare_client_guard` (`wait_tools.py:141-185`) passes those decoy-laden args through unchanged.
  2. The before-tool event's `tool_input.arguments` is the decoy-laden dict (`result_handling.py:38`).
  3. `_build_eval_context` unwraps `tool_input.arguments` into `tool_input` (`templating.py:56-67`), then re-injects the real outer `server_name`/`tool_name` **only `if field not in raw_tool_input`** (`:70`). The decoy keys are already present, so the real values are NOT injected → `tool_input.tool_name == "list_tools"`.
  4. The rule `when` evaluates `not is_discovery_tool(tool_input.get('tool_name'))` → `is_discovery_tool("list_tools")` is `True` (`enforcement/blocking.py:16-19,90`) → `not True` → the whole `when` short-circuits to `False` → **no block**. `is_tool_unlocked(tool_input)` (`blocking.py:124-131`) is likewise fooled, computing key `gobby-skills:list_tools`.
  5. `delete_task` runs without ever requiring `get_tool_schema`. `enforce_tool_schema_check` defaults `true`, so this is live on default installs.
- **Why it matters:** The flagship enforcement (Guiding Principle 1) is silently bypassable on default config. The eval-context routing fields diverge from what the proxy actually executes — the gate inspects one tool while a different tool runs. Reporting "allowed" while the contract is violated is the Blocker bar.
- **Minimal fix:** In `_build_eval_context`, routing fields from the outer `call_tool` envelope must **unconditionally overwrite** inner-arg keys (mirror the proxy's "top-level wins"): drop the `field not in raw_tool_input` guard at `templating.py:70` so `original_tool_input`'s `server_name`/`tool_name` always win. Add a regression test (none exists).
- **Confidence:** high — traced end-to-end through six files.

### [BLOCKER] Rule-level `when` fails OPEN for any rule whose `block` effect is not first
- **Where:** `src/gobby/workflows/engine/core.py:580-586` (heuristic) + `src/gobby/workflows/engine/templating.py:240-245` (`_evaluate_condition` fail-closed only when `effect_type == "block"`).
- **Failure mode:**
  1. The rule-level `when` derives its fail-open/closed posture from `first_type = body.resolved_effects[0].type` — the **first effect in definition order** (`core.py:582-583`). `resolved_effects` is not reordered; the engine defers `block` to after sibling effects at apply time (`core.py:605-608`), so authors legitimately list `block` last.
  2. `_evaluate_condition` returns `fail_closed = (effect_type == "block")` on a raised condition (`templating.py:240`). So a rule whose first effect is non-block gets `fail_closed = False`.
  3. The bundled `enforce-tdd-block` rule has effects `[set_variable, mcp_call, block]` (`install/shared/workflows/rules/tdd-enforcement/enforce-tdd-block.yaml`), so `first_type == "set_variable"`. Its `when` calls `normalize_path(tool_input.get('file_path',''))` where `normalize_path = lambda p: p.replace("\\","/")` (`safe_evaluator.py:521`). A `Write`/`Edit` whose `file_path` is a non-string (model-controlled tool input — e.g. `null` or an object) makes `normalize_path` raise `AttributeError` → condition errors → `_evaluate_condition` returns `False` → `if not …: continue` skips **the entire rule, including its block** → the action is allowed.
- **Why it matters:** Any enforcement rule authored block-last (the documented, engine-encouraged ordering) is bypassable by making its `when` throw. The documented contract (`templating.py:226-228`, "block effects fail closed — prevents action") is defeated by sampling the wrong effect. Silent enforcement bypass.
- **Minimal fix:** Compute fail-open/closed from whether the rule contains **any** block effect, not by position:
  `first_type = "block" if any(e.type == "block" for e in body.resolved_effects) else (body.resolved_effects[0].type if body.resolved_effects else "block")`.
  Better: thread an explicit `fail_closed=any(block)` flag rather than reusing `effect_type`. Per-effect `when` for block effects already fails closed correctly (`core.py:599-603` passes the real `effect.type`); only the rule-level heuristic is wrong.
- **Confidence:** high — mechanism + concrete affected bundled rule both verified.

### [BLOCKER] `gobby rules import` soft-deletes every bundled rule — enforcement wiped, not self-healing
- **Where:** `src/gobby/cli/rules.py:213-216` (call) + `src/gobby/workflows/sync_rules.py:60-63` (default `tag="gobby"`), `:188-202` (orphan cleanup), `:300-307` (soft-deletes never restored).
- **Failure mode:**
  1. `import_rules` calls `sync_bundled_rules(db, rules_path=path.parent)` with the **default `tag="gobby"`**.
  2. Orphan cleanup selects **all** active rows `workflow_type='rule' AND tags ∋ 'gobby'` (`sync_rules.py:190-196`) and soft-deletes every one whose name is not found in `path.parent` (`:198-202`) — i.e. every bundled rule, since the user imported from an arbitrary directory holding one file.
  3. Recovery never happens: `_sync_single_rule` skips any soft-deleted row ("Respect soft-deletes", `:304-307`), unlike pipelines which restore. Bundled rules stay dead until `purge_deleted` hard-deletes them ~30 days later.
  4. Bonus: the imported rules are themselves created tagged `gobby`, so the next real bundled sync (`gobby install`) orphan-deletes the user's imported rules too. And `cli/rules.py:209` accepts `.yml`, but `_iter_active_rule_files` globs `*.yaml` only (`sync_rules.py:54`) — a `.yml` import syncs **zero** rules yet still runs the wipe.
- **Why it matters:** One innocuous CLI command silently disables the entire bundled rule engine (stop-gates, task-enforcement, worker-safety, progressive-discovery) for up to 30 days. `result["success"]` stays `True` throughout.
- **Minimal fix:** In `import_rules`, pass a non-`gobby` tag (e.g. `tag="user"`), or add an `orphan_cleanup: bool = True` parameter to `sync_bundled_rules` and pass `False` from import. Make `_iter_active_rule_files` include `*.yml` (or reject it in the CLI).
- **Confidence:** high.

### [BLOCKER] One unreadable/parse-failing template file permanently soft-deletes its rules, reporting `success: True`
- **Where:** `src/gobby/workflows/sync_rules.py:183-186` (per-file `except` records error but the file's rule names never reach `on_disk`), `:188-202` (orphan pass runs unconditionally even when `errors` is non-empty), `:97-98` (`success` never set `False`), `:304-307` (soft-deleted rows skipped forever); identical chain in `src/gobby/workflows/sync_variables.py:115-118,100,120-135,183-186`; `sync_pipelines.py:158-165` even drops validation failures without recording them as errors.
- **Failure mode:** A transient read/parse failure of one bundled or user YAML (I/O error, encoding issue, partial write — see the non-atomic writer Important below) makes every rule from that file look "removed from disk." The orphan pass soft-deletes them. The next *successful* sync hits the `deleted_at is not None → skip` branch and never restores them. The function still returns `success: True` with the error buried in a list the installer only logs. An existing-but-empty directory triggers the same mass-delete (the early-return guards only fully-missing roots, `sync_rules.py:105-108`).
- **Why it matters:** Blocking enforcement rules vanish silently and permanently from one transient error — enforcement changed without consent, reported as success. Hits rules and variables.
- **Minimal fix:** Skip the orphan pass (or skip orphaning names that belonged to a failed file, tracked per-file) whenever any file-level error occurred; set `success: False` when `errors` is non-empty; port the pipeline restore-on-reintroduction branch (`sync_pipelines.py:196-201`) to `_sync_single_rule`/`_sync_single_variable`.
- **Confidence:** high.

### [BLOCKER] Multi-root `tag="user"` install sync mutually orphan-wipes user rules/variables
- **Where:** `src/gobby/cli/installers/shared.py:289-341` (`_sync_user_templates_to_db` calls `sync_bundled_rules` once per directory with `tag="user"`) + `src/gobby/workflows/sync_rules.py:188-202` (tag-global orphan select, per-call `on_disk`) + `:304-307` (no restore); same for variables (`sync_variables.py:120-135,183-186`).
- **Failure mode:** Each call computes `on_disk` from one directory only, then soft-deletes **all** `"user"`-tagged rows not in that set. With both `.gobby/workflows/rules/` (project) and `~/.gobby/workflows/rules/` (global) populated — exactly what `auto_export_definition` produces for project-vs-`make_global` rules — call 1 (project) soft-deletes every global user rule; call 2 (global) finds those rows soft-deleted and **skips instead of restoring**, then its own orphan pass soft-deletes the project rules call 1 just created. After one `gobby install`, user rules from at least one scope are gone; after two runs, all of them. The same mechanism also kills user rules belonging to *other projects* (all user rows have `project_id=None`). `tests/workflows/test_user_template_sync.py` only tests cross-*tag* isolation, never two same-tag directories — the load-bearing case is untested.
- **Why it matters:** The documented user-template directories cannot coexist; the install flow that is supposed to seed user rules destroys them, violating "user/project-owned rows are preserved." Distinct from the `gobby`-tag wipe above: this fires with correctly-tagged user rows and no user error.
- **Minimal fix:** Scope orphan cleanup to the roots actually scanned — accumulate `on_disk` across project+global before the orphan pass (as the bundled two-root call already does), or record a source-root marker per row and only orphan rows from the scanned root. Never orphan-delete `tag != "gobby"` rows from a partial-root call.
- **Confidence:** high.

### [BLOCKER] Any exception in rule evaluation fails open to `allow` at the hook boundary — including STOP gates
- **Where:** `src/gobby/hooks/rule_evaluator.py:87-89` (`except Exception: return None, None`), reached after `src/gobby/workflows/hooks.py:694-696` and `:733-735` re-raise; consumed by `src/gobby/hooks/hook_manager.py:354-356`.
- **Failure mode:** `RuleEngine.evaluate()` raising anything (malformed rule, transient sync-DB error, template error, the condition-eval errors above, or a 30 s coroutine timeout at `hooks.py:721`) propagates up through the two re-raises and is swallowed by `WorkflowRuleEvaluator.evaluate`, which returns `(None, None)` — no blocking response. `hook_manager` then runs the event handler normally and returns its `allow`. The CLI receives allow. On a STOP event this disables the stop-gate ("never stop with a claimed task"), every `block` rule, and worker-safety — all of it — while reporting success. The codebase already fail-closes STOP on variable-load failure (`hooks.py:531-540`) and on cancellation (`_handle_cancelled`, `hooks.py:437-445`), and the HTTP route force-blocks Stop on timeout (`servers/routes/mcp/hooks.py`), so the generic-exception path contradicts the established fail-closed posture for the same event. Note: a test pins the current behavior (`tests/hooks/test_hook_manager.py:test_workflow_evaluation_exception_fails_open`), so the fix must update that test.
- **Why it matters:** All product enforcement hangs off this one seam; one exception class anywhere under `RuleEngine.evaluate` makes Gobby decorative for that event. Success-while-violating-contract.
- **Minimal fix:** In `WorkflowRuleEvaluator.evaluate`'s `except`, return a blocking `HookResponse` for fail-closed events (at minimum `HookEventType.STOP`/`STOP_FAILURE`, mirroring `hooks.py:531-540`'s "could not load session state" block) instead of `(None, None)`; keep fail-open only for advisory events. Update the pinning test to assert fail-closed for STOP.
- **Confidence:** high (mechanism); the "is fail-open partly intentional?" nuance is resolved by the STOP-specific contract the rest of the code already enforces.

### [IMPORTANT] Enforcement-control variables are agent-writable via the always-allowed `set_variable`
- **Where:** `src/gobby/workflows/engine/enforcement.py:34` (`RESERVED_STEP_WORKFLOW_VARIABLES` = two names) + `:405-429` (`_is_reserved_variable_write`), `:220` (check returns early when no step workflow active); `src/gobby/workflows/enforcement/blocking.py:28-31` (`set_variable`/`get_variable` always allowed).
- **Failure mode:** `set_variable` is an infrastructure tool that bypasses allow-lists. The only reserved names the enforcement layer blocks are `step_workflow_complete`/`_step_workflow_name`, and only while a step workflow is active. Runtime-managed enforcement state — `unlocked_tools`, `listed_servers`, `enforce_tool_schema_check`, `consecutive_tool_blocks`, `_last_blocked_tool`, `edit_write_pending` — is therefore directly writable by the agent it constrains. `set_variable(name="enforce_tool_schema_check", value=false)` disables progressive discovery wholesale; `set_variable(name="consecutive_tool_blocks", value=0)` defeats retry-escalation; `set_variable(name="unlocked_tools", value=[…])` unlocks any tool.
- **Why it matters:** Every enforcement mechanism gated on a session variable can be turned off by the agent. The reserved-variable mechanism exists precisely to prevent this; its allow-list is two names wide.
- **Minimal fix:** Centralize a reserved/runtime-managed variable set (`unlocked_tools`, `listed_servers`, `servers_listed`, `enforce_*`, `consecutive_tool_blocks`, `_last_blocked_*`, `edit_write_*`) and enforce it in `_is_reserved_variable_write` independent of active step workflow, plus in the `set_variable` tool's protected set.
- **Confidence:** medium — the terminal write-gate lives in the (split-boundary) `set_variable` tool, but the in-scope reserved-set is the layer meant to stop this.

### [IMPORTANT] DB `enabled` column is ignored by the workflow/pipeline loader — disabled pipelines still load, run, and expose as tools
- **Where:** `src/gobby/workflows/loader.py:122-146,399` (`_load_from_db` parses `definition_json`, never consults `row.enabled`); `src/gobby/workflows/loader_discovery.py:33-53,69-90`; consumed by `dispatch/dispatcher.py:506`, `servers/routes/pipelines.py:289`, `mcp_proxy/tools/workflows/_pipelines.py:541-550` (filters only on `expose_as_tool`).
- **Failure mode:** `update_workflow_definition(enabled=False)` updates only the row column. The loader reparses the embedded JSON and ignores the column, so the dispatcher, HTTP run route, and tool exposure all use a pipeline the user disabled. `validate_workflow_for_agent` gates on `workflow.enabled` from the embedded JSON, not the DB toggle. Only rules honor the column (`list_all(..., enabled=True)`).
- **Why it matters:** Direct violation of "the DB is the source of truth for what's active" (Guiding Principle 13). The user's disable toggle — the one thing drift refresh carefully preserves — has no runtime effect for workflows/pipelines.
- **Minimal fix:** In `_load_from_db`/`_merge_db_*`, overlay `data["enabled"] = row.enabled` (the column) before model construction; have pipeline execution/exposure check it.
- **Confidence:** high.

### [IMPORTANT] One observer exception aborts all later observers, drops variable persistence, and (on STOP) fails the gate open
- **Where:** `src/gobby/workflows/hooks.py:447-496` (`_run_observers`, no per-observer isolation), `:667,677-682`; `src/gobby/workflows/observers.py:150-154,302-305,328-335` (narrow `except (ValueError, KeyError, TaskNotFoundError)` lets `psycopg.Error` escape).
- **Failure mode:** A transient `psycopg.Error` inside `detect_task_claim`/`reconcile_claimed_tasks`'s `get_task` escapes the narrow catch (the sibling `claimed_task_skills._load_task:133-138` correctly catches `psycopg.Error` — this is drift). The exception aborts the remaining observers (commit/verification/MCP state lost for the event), skips the changed-keys merge, and propagates into the fail-open boundary above. On STOP, `reconcile_claimed_tasks` runs first, so a DB hiccup means the `require-task-close` stop gate is never evaluated and the session stops with a claimed task. Related explicit fail-open: when `claimed_tasks` is empty and the DB rebuild query fails, `task_claimed` is forced `False` (`observers.py:328-347`).
- **Why it matters:** Violates Guiding Principle 6; the opposite of the deliberate fail-closed at `hooks.py:531-540`.
- **Minimal fix:** Wrap each observer call in `_run_observers` in try/except-log-continue; widen the two narrow catch tuples to include `psycopg.Error`; on STOP, convert reconcile/list failures into a block response.
- **Confidence:** high.

### [IMPORTANT] `git status --porcelain` parsing returns wrong path for renames and quoted/non-ASCII paths — dirty-tree close gate silently passes
- **Where:** `src/gobby/workflows/git_utils.py:283-297`; consumer `src/gobby/workflows/hooks.py:649-659` (`has_dirty_files`).
- **Failure mode:** (1) Renames: porcelain emits `R  old -> new`; line 290 takes `split(" -> ")[0]` — the *old* path, no longer on disk. The dirty file is `new`; `session_edited_files` holds `new`; the intersection in `_check_dirty` is empty → `has_dirty_files` False. (2) Quoted paths: with `core.quotePath=true` (git default), non-ASCII paths are C-quoted (`?? "t\303\253st.py"`); line 290 keeps the quotes/escapes verbatim so it never matches the tracked relative path, and the `.gobby/` exclusion misses quoted entries. Result: `require-clean-tree-before-status` does not block, and `close_task` succeeds with uncommitted changes (violates Guiding Principle 5). No test covers `get_dirty_files_categorized`.
- **Minimal fix:** Use `git status --porcelain -z` (NUL-delimited, never quoted; rename records carry new-then-old) and take the *new* path for `R`/`C`. Add parser tests for rename, space, non-ASCII.
- **Confidence:** high.

### [IMPORTANT] Context-usage observer is never wired into the hook path — the compact-nudge rule can never fire
- **Where:** `src/gobby/workflows/observer_context_usage.py:26-90` (only writer of `context_compact_guidance_message`); `src/gobby/workflows/hooks.py:447-496` (`_run_observers` calls every other observer but not this one); rule `install/shared/workflows/rules/context-handoff/nudge-compact-on-context-pressure.yaml:9`.
- **Failure mode:** Repo-wide, the only callers of `detect_context_compact_guidance` are tests. The bundled, enabled-by-default rule gates on `variables.get('context_compact_guidance_message')`, which is therefore never truthy in production. Agents never get the 65%/80% compact nudges. Tests pass because they assert the observer and the rule YAML separately — no test drives a turn-start event end-to-end.
- **Minimal fix:** Call `detect_context_compact_guidance(...)` from `_run_observers` in the turn-start branch; add an integration test.
- **Confidence:** high.

### [IMPORTANT] Plan-mode stale-state "heal" clears `mode_level` but leaves `plan_mode`/`plan_skill_loaded` stuck True
- **Where:** `src/gobby/workflows/observer_plan_mode.py:191-202` (contrast the entry/exit/`set_mode` paths at `:47-49,59-69,103-122`).
- **Failure mode:** When a plan exit is missed (user toggles out without `ExitPlanMode`, compaction eats the reminder), the heal resets only `mode_level`, leaving `plan_mode=True`. Inconsistent state `mode_level=2, plan_mode=True`: `block-edits-plan-mode` keeps blocking every Edit/Write (wedge) while `require-task-close` (`when: not variables.get('plan_mode') and …`) silently disables the stop gate (bypass). Both from one stale flag, with no other in-session corrective path.
- **Minimal fix:** In the heal branch, when `new_level != 0`, also clear `plan_mode` and `plan_skill_loaded` (mirror `set_mode`'s `:47-49`).
- **Confidence:** medium — mechanics certain; frequency of marker-less exits depends on CLI.

### [IMPORTANT] Git summary functions run in the daemon CWD — handoff summaries get the wrong repo's git data
- **Where:** `src/gobby/workflows/git_utils.py:21-37,39-65,68-104` (no `cwd`/`project_path` param); callers `summary_actions.py:655-675`, `sessions/summarize.py:514-515`.
- **Failure mode:** `get_git_status`/`get_file_changes`/`get_recent_git_commits` call `subprocess.run` with no `cwd`, executing in the daemon's launch dir (or `/` post-daemonize) — never the session's project. `get_git_diff_summary` *has* a `project_path` param but `generate_summary` calls it without one. The daemon serves many projects from one process; if started inside a git repo, every session's handoff embeds *that* repo's status/commits/diff; otherwise empty. Wrong "Recent Commits" in every handoff fed to the LLM.
- **Minimal fix:** Add `project_path: str | None` (pattern already in `get_git_diff_summary`), pass `cwd=project_path`, thread the session project path through `generate_summary`/`summarize.py`.
- **Confidence:** high.

### [IMPORTANT] Sync DB queries and git subprocesses run on the asyncio event loop for every hook event
- **Where:** `src/gobby/workflows/hooks.py:713-721` (coroutine scheduled onto the daemon loop), then `:421,529,588-590,609/637/642/667/678-682` (sync `LocalProjectManager.get`, `get_variables`, `list_all`, `merge_variables`, `get_dirty_files_categorized`, `_run_observers`); `git_utils.py:267-273` (`git status` with `timeout=10`); per-rule context rebuild at `engine/templating.py:93-124` re-resolves project via `SessionManager`/`LocalProjectManager` for **every** matching rule (`core.py:574`) because the resolved project is never written back to `variables["project"]`. Only `get_active_step_workflow_context` (`hooks.py:553-557`) is offloaded via `to_thread`.
- **Failure mode:** Every hook event from every CLI stalls the shared event loop for the duration of these blocking psycopg round-trips and git subprocesses; a slow `git status` on a cold/large repo (up to 10 s) freezes WebSocket broadcast, chat, voice, and all other sessions. The per-rule project re-resolution multiplies it to ~2·M blocking queries per event with M matching rules.
- **Minimal fix:** Offload the blocking sections (`get_variables`/`merge_variables`, `get_dirty_files_categorized`, `_run_observers`) via `asyncio.to_thread` (matching the existing `:553-557` pattern); hoist project/session resolution out of the per-rule loop and cache it into `variables["project"]` after first resolution.
- **Confidence:** high.

### [IMPORTANT] Sync DB/audit writes on the event loop in effects/enforcement dispatch
- **Where:** `src/gobby/workflows/engine/effects.py:444,472,494,514` (`SessionVariableManager.get_variables`/`append_to_set_variable`, sync per `state_manager.py:147,243`); `src/gobby/workflows/engine/enforcement.py:583,605,895,966` (`save_instance`, sync) and the `workflow_audit.log*` calls in `_process_step_after_tool`/`_audit_step_*` (sync per `storage/workflow_audit.py:56`).
- **Failure mode:** `_process_step_after_tool` and the inline-`mcp_call` formatting in `_apply_effect` are `async` and run on the loop (scheduled via `run_coroutine_threadsafe`); the sync psycopg calls block the loop thread per round-trip on a per-tool-call hot path.
- **Minimal fix:** Offload via the existing `app_context.run_db`/executor, or provide async variants.
- **Confidence:** medium.

### [IMPORTANT] Unbounded string/list `*` multiplication is a memory-bomb DoS on the hook path
- **Where:** `src/gobby/workflows/safe_evaluator.py:192-207` (`_SAFE_BIN_OPS` includes `ast.Mult`; `visit_BinOp` applies it with no size guard).
- **Failure mode:** A rule condition `'a' * 999999999` or `[0] * 999999999` eagerly allocates ~1 GB synchronously on the event loop (`core.py:585`). `**` is correctly rejected, but `*` is the real bomb. Conditions come from bundled, user, and *project-level* rule YAML (synced from cloned repos), so a crafted/buggy condition OOMs or stalls the daemon. `MemoryError` is "caught" but the OOM killer / loop stall hits first.
- **Minimal fix:** Guard `Mult` (and string `Add`/`Mod`) by rejecting operands above a size cap in `visit_BinOp`, or drop `Mult` from `_SAFE_BIN_OPS` (no bundled condition needs multiplication).
- **Confidence:** high (medium that project-level YAML is the intended trust boundary; high if it is → Blocker).

### [IMPORTANT] `_normalize_expr` collapses whitespace inside string literals, silently corrupting comparisons
- **Where:** `src/gobby/workflows/safe_evaluator.py:116-125` (`" ".join(expr.split())`) applied to the whole expression at `:127-141`.
- **Failure mode:** `expr.split()`+`join` collapses every whitespace run in the *entire* source, including quoted literals: `tool_input.get('command') == 'git  commit'` (two spaces) becomes `… == 'git commit'`; tabs/newlines in a literal are rewritten. The comparison silently matches a different string than authored — under-enforce or match-the-wrong-value, no error.
- **Minimal fix:** Normalize only outside string literals (tokenize, or `re.sub(r"\n\s+", " ", expr)` for the YAML-folding case) so interior literal whitespace is preserved.
- **Confidence:** high.

### [IMPORTANT] `_run_sync` deadlocks on a worker thread running its own loop; blocks the daemon loop — and is called from `async def spawn_agent`
- **Where:** `src/gobby/workflows/loader_sync.py:78-98`; call site `src/gobby/mcp_proxy/tools/spawn_agent/_factory.py:297` (`async def spawn_agent`), `:396`.
- **Failure mode:** `asyncio.get_running_loop()` returns a loop only if it runs in the *current* thread, so the `:96-98` "worker thread with loop running elsewhere" branch (comment is wrong) calls `run_coroutine_threadsafe(coro, loop).result()` on the loop's own thread → permanent deadlock. The main-thread branch (`:90-94`) avoids deadlock but synchronously blocks the entire loop on `pool.submit(...).result()`, and it's invoked from inside the async `spawn_agent`, so every agent spawn stalls the daemon loop.
- **Minimal fix:** In `spawn_agent`, `await loader.load_workflow(...)` directly (already async). In `_run_sync`, replace the non-main-thread branch with the executor-offload used by the main-thread branch.
- **Confidence:** high mechanics; medium reachability of the deadlock branch today.

### [IMPORTANT] RuleEffect/RuleDefinitionBody silently ignore unknown fields and never enforce per-type required fields
- **Where:** `src/gobby/workflows/definitions.py:104-236` (RuleEffect, no `model_config` → Pydantic default `extra="ignore"`; warnings cover only *known* fields), `:239-258` (RuleDefinitionBody validates only "effects non-empty, ≤1 block").
- **Failure mode:** A typo'd effect key (`templte:`, `command_patern:`) is silently dropped at validation. An `inject_context` effect missing `template` syncs successfully and injects nothing; a `block` effect whose `command_pattern` was typo'd loses its selector and blocks *every* matching tool call. `model_post_init` warns only on recognized fields via `warnings.warn` (invisible in daemon logs). No validator requires `variable` for `set_variable`, `server`/`tool` for `mcp_call`, etc. The test fixture at `tests/workflows/test_user_template_sync.py:30` uses a nonexistent `content:` field and passes.
- **Minimal fix:** `model_config = ConfigDict(extra="forbid")` on `RuleEffect` (and `RuleDefinitionBody`), plus a `model_validator` enforcing per-type required fields.
- **Confidence:** high (gap); medium (real-world frequency).

### [IMPORTANT] Project-scope shadowing in discovery is nondeterministic
- **Where:** `src/gobby/workflows/loader_discovery.py:38-53,74-90` (`discovered[row.name] = …`, last-write-wins), relying on `storage/workflow_definitions.py:300-316` (`ORDER BY name` only).
- **Failure mode:** `list_all(project_id=X)` returns both the project row and the global row for a shared name, ordered only by `name`; the tie between two equal-named rows is unspecified by PostgreSQL. The docstring promises "project workflows shadow global," but which wins depends on row order — pipeline lists / exposed tools can flip between project and global variants across restarts. `detect_override_conflict` is never consulted on this path.
- **Minimal fix:** Skip the overwrite unless the incoming row is project-scoped, or add a deterministic secondary sort (`project_id NULLS LAST`).
- **Confidence:** high.

### [IMPORTANT] Loader scopes lookups by filesystem path while rows are created with project IDs
- **Where:** `src/gobby/workflows/loader.py:191,238` (`project_id = str(project_path)`), `loader_discovery.py:115,155`; vs `mcp_proxy/tools/workflows/_definitions.py:62,100` (rows created with caller `project_id`) and `hooks/session_activation.py:315,345` (lookups by `session.project_id`).
- **Failure mode:** `load_workflow`/`discover_workflows` pass the project *path* string as the `project_id` SQL filter, while rows created through the MCP import tool use real project IDs. A project-scoped definition created with an ID is invisible to every path-keyed load/discover call, silently falling back to the global definition.
- **Minimal fix:** Resolve `project_path` → canonical project ID at the loader boundary before querying; use that key in cache keys too.
- **Confidence:** medium.

### [IMPORTANT] `gobby workflows import` (CLI) leaves the daemon serving stale definitions
- **Where:** `src/gobby/workflows/loader.py:184-196` (cache stores `mtime=0.0`, never revalidated); `loader_cache.py:24-37`; `cli/workflows/manage.py:261` clears only the CLI process's loader vs the daemon-reload notification used by `sync` (`:62,107-125`).
- **Failure mode:** `_Cached*` entries are keyed forever; only in-process `clear_cache()` invalidates. MCP/HTTP mutation clears the daemon loader and `gobby workflows sync` POSTs `/api/admin/workflows/reload`, but `import_workflow` clears only its own CLI-process loader and never notifies the daemon, which keeps executing the pre-import definition until restart.
- **Minimal fix:** Call `_notify_daemon_reload()` from `import_workflow` (and other CLI mutation paths), or version the loader cache against a DB-backed revision.
- **Confidence:** medium.

### [IMPORTANT] `task_tree_complete` no-manager fallback returns True (fail-open), contradicting the standalone's False
- **Where:** `src/gobby/workflows/safe_evaluator.py:541` (`lambda task_id: True`) vs `condition_helpers.py:336-341` (standalone returns `False`); caller `session_coordinator.py:621` builds helpers with no `task_manager`.
- **Failure mode:** With no `task_manager`, the closure binds `task_tree_complete` to `True` while every sibling no-data fallback returns the safe negative (`task_state_in`→False, etc.). Any workflow `exit_condition` calling `task_tree_complete(...)` is then unconditionally satisfied, letting a step "complete" without the tree being done. No bundled workflow uses it in an exit_condition today, so impact is latent, but the two code paths disagree on fail direction.
- **Minimal fix:** Make the no-manager fallback return `False` (or delegate to `task_tree_complete(None, task_id)` for a single source of truth).
- **Confidence:** medium.

### [IMPORTANT] Synchronous DB / full-subtree walk in condition helpers on the event loop
- **Where:** `src/gobby/workflows/condition_helpers.py:313` (`db.fetchall("SELECT id FROM tasks WHERE seq_num = %s")`), recursive `_is_tree_complete` (`:423-452`) issuing `list_tasks` + `get_task` per node.
- **Failure mode:** `task_tree_complete`/`task_state_in`/`task_type_in`/`task_needs_human_review` are evaluated synchronously inside the async `RuleEngine.evaluate` (`core.py:585`) with no executor offload; `task_tree_complete` recurses the entire subtree, one `get_task`+`list_tasks` per node, on every matching hook event.
- **Minimal fix:** Offload via `run_in_executor`, or precompute tree-completion into a session variable the condition reads.
- **Confidence:** high.

### [IMPORTANT] `category:` selector silently never matches rules or variables
- **Where:** `src/gobby/workflows/selectors.py:15` (`KNOWN_PREFIXES` includes `"category"`) vs `_match_rule` (`:24-37`, no `category` branch) used for rules/variables (`:100-104,119-123,217-225`).
- **Failure mode:** `parse_selector("category:dev")` is recognized, but `_match_rule` has no `category` case and falls through to `return False`. As an include → the rule/variable never activates (under-selection); as an exclude → it never excludes, so a rule the operator tried to disable stays active (over-enforcement). Skills handle category (`_match_skill:146-152`), masking the asymmetry; tests cover only the skill path.
- **Minimal fix:** Add a `category` branch to `_match_rule` reading `definition_json.get("category")`, or drop `"category"` from `KNOWN_PREFIXES` so it fails visibly.
- **Confidence:** medium-high.

### [IMPORTANT] Inline `mcp_call` effect silently drops `block_on_failure`/`block_on_success`
- **Where:** `src/gobby/workflows/engine/effects.py:139-225` (inline branch) vs the deferred branch which forwards the flags (honored in `hooks/dispatchers/mcp.py:379-493`).
- **Failure mode:** With `inject_result: true` (non-background), dispatch goes inline, awaits the call, returns `True`/`False`, and never consults `effect.block_on_failure`/`block_on_success`. The same effect run deferred *does* enforce them. So identical rule definitions block differently based solely on whether `inject_result` routes them inline — latent (no bundled rule combines the flags) but silent.
- **Minimal fix:** In the inline branch, surface a block when the call fails and `block_on_failure` is set (or succeeds with `block_on_success`); or refuse the inline path when those flags are present.
- **Confidence:** medium.

### [IMPORTANT] `exit_condition` evaluated with un-merged context — `vars` and `variables` diverge
- **Where:** `src/gobby/workflows/engine/enforcement.py:912-917` (exit_ctx) vs merged context everywhere else (`:861,809,822-823`).
- **Failure mode:** Elsewhere `vars`/`variables` alias `{**variables, **instance.variables}`. In exit_ctx, `vars = instance.variables` only. Native `set_variable` writes land in session `variables`, not `instance.variables` (`:795-797`), so an `exit_condition` referencing `vars.X` for a natively-set var sees a dict that never contains `X` → exit never fires → `_complete_agent_workflow_run` never runs (agent-backed workflows hang at a terminal step).
- **Minimal fix:** Build exit_ctx with both `vars` and `variables` set to the merged dict.
- **Confidence:** medium.

### [IMPORTANT] Consecutive-block escalation evadable via native-vs-proxied tool identity mismatch
- **Where:** `src/gobby/workflows/engine/core.py:70-85` (`_get_tool_identity`), used at `:317-319,408,427,645`.
- **Failure mode:** A native MCP call yields identity `mcp__server__tool`; the proxied `call_tool` form yields `server:tool`. The same logical tool blocked natively then retried via `call_tool(...)` produces a different identity, so `tool_name == last_blocked` is False and the consecutive counter resets instead of escalating. Weakens the retry-loop breaker (does not bypass the primary block).
- **Minimal fix:** Normalize native `mcp__server__tool` to `server:tool` so both forms share an identity.
- **Confidence:** high (mechanism); impact is escalation-only.

### [IMPORTANT] Variable-load failure on non-STOP events clobbers persisted session state
- **Where:** `src/gobby/workflows/hooks.py:526-547,636-645,664-683`.
- **Failure mode:** When `get_variables` raises on a non-STOP event, evaluation continues with `variables = {}`. Then `baseline_dirty_files` is recomputed from the current set and persisted (overwriting the real baseline), observers build 1-item `verification_evidence`/`claimed_tasks` from the empty dict, and the diff against the empty `pre_eval` marks everything changed → `merge_variables` replaces evidence history, claim set, and baseline with fragments. One transient read error followed by a successful write corrupts session state that later stop gates read.
- **Minimal fix:** On variable-load failure, set a `load_failed` flag and skip lazy-baseline persistence and the end-of-eval `merge_variables` (evaluate read-only), or fail the evaluation like the STOP path.
- **Confidence:** medium.

### [IMPORTANT] `resolve_agent` breaks when any non-agent definition shares the name
- **Where:** `src/gobby/workflows/agent_resolver.py:44-45` (no `workflow_type` filter), same at `engine/core.py:898-899`.
- **Failure mode:** `get_by_name(name, project_id=...)` has no type filter and prefers the project-scoped row. If a rule/workflow/pipeline shares a name with an agent (names are not unique per type — see filed storage finding), or a project non-agent row shadows a global agent, the lookup returns a non-agent row → `resolve_agent` returns `None` → agent unresolvable for spawn/persona/rule-scoping. `_load_active_agent_definition` caches the `None`.
- **Minimal fix:** Add a `workflow_type: str | None` filter to `get_by_name`, pass `"agent"`.
- **Confidence:** medium-high.

### [IMPORTANT] Nonexistent `additional_skills` on a claimed task wedges all source writes with no exit
- **Where:** `src/gobby/workflows/claimed_task_skills.py:85,115-126`; `observer_mcp.py:30-50`; rule `task-enforcement/require-claimed-task-required-skills.yaml:10-15`.
- **Failure mode:** `build_claimed_task_skill_state` copies `task.additional_skills` verbatim with no existence check. The rule blocks every `write` until `first_unloaded_claimed_task_required_skill()` is empty, and `loaded_skills` is appended only on a *successful* `get_skill`. A typo'd/removed skill name ⇒ `get_skill` always fails ⇒ requirement never satisfiable ⇒ every source write blocked; the consecutive-block breaker also *blocks* at the cap, so there is no automated escape.
- **Minimal fix:** Drop (with a warning) required skills that don't resolve in the skills registry when aggregating, or have `first_unloaded_…` skip names recorded as failed lookups.
- **Confidence:** medium.

### [IMPORTANT] Language-skill mapping covers only `.py`/`.rs` despite 15+ bundled language skills
- **Where:** `src/gobby/workflows/claimed_task_skills.py:175-182`.
- **Failure mode:** `_language_skills_for_files` maps only `python`←`.py`, `rust`←`.rs`. Bundled skills exist for typescript, javascript, go, java, c, cpp, csharp, dart, elixir, kotlin, php, ruby, swift (incl. the recent C/C++/C# additions), but tasks touching those files never get language-skill requirements — `require-claimed-task-required-skills` silently doesn't enforce for the entire non-Python/Rust surface (e.g. `web/` TypeScript).
- **Minimal fix:** Extend the extension→skill table (or derive it from the bundled-skill registry).
- **Confidence:** medium — could be deliberate rollout, but nothing in code marks it so.

### [IMPORTANT] PreToolUse translation lets `permission_decision="allow"` override a block
- **Where:** `src/gobby/adapters/claude_code.py:334-342` (with `:376`); `engine/core.py:682-689`.
- **Failure mode:** In PRE_TOOL_USE style, `response.permission_decision` is consulted before `is_denied` (only `if not permission_decision` applies deny). A higher-priority rule with a permission "allow" effect plus a lower-priority block on the same `before_tool` event yields `decision="block"` + `permission_decision="allow"` → adapter emits `permissionDecision: "allow"` and `continue` stays true → the tool runs despite first-block-wins.
- **Minimal fix:** In the PRE_TOOL_USE branch, force `permission_decision = "deny"` whenever `is_denied`.
- **Confidence:** low — no bundled rule emits permission effects on `before_tool`; requires a custom rule combination (mechanism verified both sides).

### [IMPORTANT] Non-atomic template writes corrupt YAML, which the orphan path then deletes
- **Where:** `src/gobby/workflows/template_writer.py:153-157` (`path.write_text` truncate-then-write, no tmp+`os.replace`, no fsync).
- **Failure mode:** Crash/power loss mid-write leaves an empty/partial `foo.yaml` in a rules dir. The next `gobby install` parses it as non-dict (rules treated as removed) or raises (file-level error) — both feed the orphan pass, which soft-deletes those rules permanently (see Blocker 4). A routine durability gap escalates to permanent enforcement loss.
- **Minimal fix:** Write to `path.with_suffix(".yaml.tmp")` then `os.replace`.
- **Confidence:** high.

### [IMPORTANT] Definition-sourced Jinja2 templates render unsandboxed in the daemon
- **Where:** `src/gobby/workflows/templates.py:70-77` (plain `jinja2.Environment`, not `SandboxedEnvironment`); template strings come from rule `reason`/`inject_context` (`engine/templating.py:166-178`), pipeline steps (`pipeline/renderer.py:184`), webhooks (`webhook_executor.py:251`).
- **Failure mode:** Project-scoped rule YAML lives in `.gobby/workflows/rules/` and is synced by `gobby install` run in that cwd. A cloned third-party repo can plant a rule whose `reason`/`template` contains an SSTI gadget (`{{ ''.__class__.__mro__… }}`); when the rule fires it executes in the daemon process. The render context also merges `allowed_funcs` plus event/session data.
- **Minimal fix:** Use `jinja2.sandbox.SandboxedEnvironment` for definition/effect rendering; keep trusted `FileSystemLoader` templates on a separate engine.
- **Confidence:** medium-high — severity hinges on the untrusted-project threat model.

### [IMPORTANT] Tool-context cache only cleared on SESSION_END — leaks and rehydrates stale context
- **Where:** `src/gobby/workflows/hooks.py:244-246,272,292-299,328-330`; `hooks/events.py:183-188` (Codex SESSION_END is `None`).
- **Failure mode:** BEFORE_TOOL snapshots are removed only on matching AFTER_TOOL or SESSION_END. Codex has no SESSION_END mapping, and any crashed/interrupted tool never sends one — entries persist for the daemon's lifetime. Stale entries are live ammunition: `_match_tool_context` falls back to `pending[-1]`, so a later AFTER_TOOL missing `tool_name` rehydrates with a different, stale tool's name/input, feeding wrong data into `detect_task_claim`/`detect_mcp_call`.
- **Minimal fix:** Clear the session's cache on turn-end events too; cap per-session pending snapshots.
- **Confidence:** medium.

### [IMPORTANT] `read_template`/`import_file` parse untrusted YAML with no shape validation
- **Where:** `src/gobby/workflows/template_writer.py:137-147` (`read_template` returns `yaml.safe_load` with no dict check despite `-> dict`); `lobster_compat.py:120-150` (`import_file` guards only `YAMLError`) → `:85-118` (`convert_pipeline` calls `.get`).
- **Failure mode:** A `.lobster`/`.yaml` whose top level is a list or scalar passes `safe_load`, then `.get(...)` raises `AttributeError` — which `import_file`'s docstring promises to raise as `ValueError` but doesn't. Programmatic callers get an unannounced crash; malformed step entries crash similarly.
- **Minimal fix:** Assert dict after `safe_load` (raise `ValueError` otherwise) in both; skip/error on non-dict step entries.
- **Confidence:** high.

## Nits

### [NIT] Three contradictory `enabled` defaults for the same definition; two bundled pipelines install disabled
- **Where:** `loader_discovery.py:41-42` (`enabled = type == "lifecycle"`), `definitions.py:486,620` (model default `True`), `sync_pipelines.py:175` / `sync_rules.py:298` (`get("enabled", False)`). `expand-task.yaml` and `nightly-fixes.yaml` carry no `enabled:` key and install `enabled=False`, contradicting Guiding Principle 13. (Note: all 149 bundled *rule* YAMLs set `enabled:` explicitly, so the rule-side default is latent.) Pick one default and derive sync from the parsed model.

### [NIT] `LazyBool` lacks `__eq__`/`__hash__`/`__contains__`; `==`/`is`/`in` compare identity
- **Where:** `safe_evaluator.py:58-84`. `has_dirty_files == True` routes through `operator.eq` on identity → `False` even when the deferred value is True (the thunk never runs). Bundled rules only use it in truthy/`not` contexts (correct via `__bool__`), so latent. Add `__eq__`/`__ne__` coercing via `bool(self)`, or resolve thunks before comparison.

### [NIT] `**` dict/keyword spread is silently dropped, not rejected
- **Where:** `safe_evaluator.py:306-312,271-272`. `{**a, 'b':1}` yields `{'b':1}`; `f(**d)` drops `**d`. Raise `ValueError("unpacking not supported")` instead of computing a different value.

### [NIT] `task_state_in` is case-sensitive while `task_type_in` lowercases — inconsistent
- **Where:** `condition_helpers.py:364-365` vs `:404-406`. `task_state_in(id, "Closed")` never matches canonical lowercase states. Lowercase in `task_state_in` to match.

### [NIT] `fnmatch.fnmatch` is OS-case-normalizing; selector matching diverges on Windows
- **Where:** `selectors.py:30-36,141-158`. `name:Plan*` matches `plan-draft` on Windows but not POSIX. Use `fnmatch.fnmatchcase`. Negligible unless Windows is supported.

### [NIT] `detect_bash_commit` can set `task_has_commits=true` for failed/unrelated commands
- **Where:** `observer_commits.py:129,143-155`. Success gate is only `is_error` (ignores `exitCode`/`returncode`/`metadata.is_failure`), and `_GIT_COMMIT_RE` runs against any successful command's output before the command check, so `cat`-ing a log containing `[main abc1234]` flips the flag. Reuse `_shell_tool_succeeded` and gate the regex on `_is_git_commit_command`.

### [NIT] `task_has_commits` is session-scoped and never resets across tasks
- **Where:** `observer_commits.py:85,119`. The first commit satisfies "commit before close" for every later task in a multi-task session. Reset when `remove_claimed_task` empties `claimed_tasks`, or rename/document as session-scoped.

### [NIT] `mcp_results` stores every MCP tool's full result, deep-copied every hook event
- **Where:** `observer_mcp.py:138`; `hooks.py:664` (`deepcopy(variables)` per event). Unbounded session-variable growth + per-event deepcopy/JSON cost on the hot path; conditions read only a few scalars. Truncate stored results to consumed fields.

### [NIT] Verification-evidence freshness reset misses shell edits
- **Where:** `hooks/event_handlers/_tool.py:14-21,170-196,289-295` (seam of `observer_verification.py:80-88`). Evidence is invalidated only for structured `EDIT_TOOLS` with `file_path`; `sed -i`/redirects/`git checkout --` neither reset evidence nor enter `session_edited_files`, so pre-edit evidence still counts fresh. Route shell-modified paths through the same reset via the existing `_normalization_shell` detection.

### [NIT] sync_pipelines coerces any unrecognized `type` to `workflow_type="pipeline"`
- **Where:** `sync_pipelines.py:155-159,171-172`. A root YAML with `type: step` is stored as pipeline, then `_load_from_db` force-parses it as `PipelineDefinition` and fails. Latent (all bundled root YAMLs are `type: pipeline`). Skip with an error instead of coercing.

### [NIT] `_validate_pipeline_references` misaligns positions when steps lack `id`
- **Where:** `loader_validation.py:25-37`. `valid_at_position` is built over id-bearing steps but indexed by all steps, producing false "references later step" errors when any step lacks `id`. Build the valid set while iterating `steps` once.

### [NIT] `agent_resolver._SOURCE_TO_PROVIDER` is an identity no-op
- **Where:** `agent_resolver.py:13-23`. Every key maps to itself; `_normalize_provider` does nothing. Delete it or make it validate against known providers.

### [NIT] Doc drift: `workflows/CLAUDE.md` mislabels `loader_sync.py` and references nonexistent `rule_engine.py`
- **Where:** `src/gobby/workflows/CLAUDE.md:9,18,35`. The doc names `rule_engine.py` (RuleEngine actually lives in `engine/core.py`) and says `loader_sync.py` "syncs bundled templates to DB" (it is sync wrappers for async loader methods; template sync lives in `sync_rules.py`/`sync_pipelines.py`/`sync_variables.py`). This doc drift misdirected the review's own scope. Triage candidate for the docs-accuracy leaves (#15799–#15801).

## Systemic patterns

- **Fail-open is the house default at every layer above the evaluator.** The rule-level
  `when` heuristic (`core.py:582`), the no-manager `task_tree_complete` fallback
  (`safe_evaluator.py:541`), the observer-exception path, `WorkflowRuleEvaluator.evaluate`'s
  `except → (None, None)` (`rule_evaluator.py:87-89`), the silent-allow branches in
  `hooks.py:713-726`, and reconcile's `task_claimed=False`-on-DB-error all choose the
  permissive branch when state is unknown — the exact opposite of the few deliberately
  fail-closed spots (STOP variable-load block at `hooks.py:531-540`, cancellation block,
  HTTP Stop-timeout block). The engine is rigorously fail-closed-*aware*; each wrapper
  above it re-opens the gate. Enforcement posture should be stated once per event type and
  applied at the outermost boundary.
- **Eval-context vs real-dispatch divergence is an enforcement hole.** The rule layer
  reconstructs `tool_input` (unwrap + conditional re-inject, `templating.py`) independently
  of how the proxy canonicalizes routing (`_call_tool_wrapper.py`). Any disagreement is a
  bypass — Blocker 1 is the concrete instance, and `_step_handler_tool_input`
  (`enforcement.py:443-454`) handles `args`/JSON-string while `_build_eval_context` only
  handles `arguments`, so the two reconstructions aren't even consistent with each other.
- **Enforcement state lives in agent-writable session variables.** Progressive discovery,
  retry escalation, and edit/write gates all key off variables an agent can set via the
  always-allowed `set_variable`; the reserved-name guard is two entries wide and active
  only inside step workflows.
- **Orphan cleanup conflates "not seen this pass" with "removed from disk."** All three
  sync modules pair a tag-global soft-delete with a per-call `on_disk` set, so any
  narrower-than-expected pass (arbitrary `import` dir, second user-template directory,
  parse failure, empty dir) mass-deletes rows it never owned — and rules/variables, unlike
  pipelines/agents, never restore them. Compounded by `success: True` with non-empty
  `errors`, the damage is silent and sticky.
- **Sync I/O on the asyncio loop is pervasive on the hook hot path** (DB, git subprocesses,
  observers, audit writes, per-rule project re-resolution), with exactly one call site
  (`get_active_step_workflow_context`) offloaded via `to_thread` — the pattern is known but
  unevenly applied. One slow repo or DB stall degrades every session.
- **Two sources of truth drift.** Row `enabled` column vs embedded `definition_json`;
  project *ID* vs project *path*; standalone `task_tree_complete` vs its closure; native
  `mcp__server__tool` vs proxied `server:tool` identity. Each half is used by a different
  part of the stack, and rules happen to use the right half where workflows/pipelines use
  the wrong one.
- **Silent-skip validation and `extra="ignore"` models** mean a misspelled effect key or a
  definition that fails validation is indistinguishable at runtime from a rule that was
  never shipped — an enforcement rule can be absent with no operator signal.

### Verified non-bugs (confirmed correct, do not re-file)
- Dunder/sandbox escape is genuinely blocked: `visit_Attribute` rejects dunders,
  `SAFE_METHODS` allowlist blocks dunder method calls, `Name` resolves only context
  vars + True/False/None, `**`/`Pow` is rejected, the regex helper uses an allowlist with
  an 8000-char haystack cap. The threat that survives is DoS (`*` bomb, no timeout), not code exec.
- First-block-wins / priority sort is correct: rules sorted by `(priority, trigger_index,
  name)`, block deferred after sibling effects, loop breaks on first block; later
  permissive responses cannot overwrite an earlier deny inside the engine
  (`force_allow_stop` is suppressed when a task is claimed).
- Per-effect `when` for block effects fails closed correctly (`core.py:599-603`); only the
  rule-level heuristic is wrong.
- `mcp_called()`/`observer_mcp` key on the same normalized `mcp_server`/`mcp_tool` fields —
  no native-vs-proxied mismatch for *primary* MCP enforcement (only the consecutive-block
  guard, above).
- Enabled-toggle preservation on normal drift refresh works: `_build_rule_update_fields`
  never touches `enabled`; `_is_sync_managed_bundled_rule` restricts refresh to
  Gobby-owned installed rows; no force/version path resets it.
- `yaml.safe_load` everywhere in scope (no unsafe loader); non-dict top-level guarded in
  the sync loops; `extends`-chain cycles raise via `_visited`.
- `dry_run.py` is a static structural validator only — no live-vs-dry drift, no leaked
  state mutation.
- Verification-evidence `success` is strictly boolean (`Field(strict=True)`); no
  truthiness bug and no timezone-naive timestamp comparison in the evidence path.
- `%s` placeholders throughout are correct per the psycopg3 contract (the repo-root
  CLAUDE.md `$N` mandate is stale doc drift).
