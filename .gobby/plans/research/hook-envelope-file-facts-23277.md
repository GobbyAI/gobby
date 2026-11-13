# #23277 working context (Plan Writer #16764, 2026-10-14)

Request (Orchestrator #15977, ~18:50 CDT): write the decision-complete spec for #23277
"Hook envelope carries edited-file content for disk-reading rules" into the card, or into a
short plan if it needs one, then report back; #15977 dispatches to Lane 3 once specced.
Spec must cover: scope, envelope field, size and redaction limits, which rules read it, test refs.

## Facts gathered
- Card (#23277, category planning, labels needs-planning, deferred-from:gdaemon-api-keys-nodes:D5):
  opens with Josh's park (2026-10-05): "park until Rust multi-machine; re-plan this in Rust then".
  2026-10-12 de-escalation re-expressed the park structurally: parent is #24150 (S2.11 node half),
  related to #24151 (hub half); keeps needs-planning. Only blocker #23271 is closed.
- Criterion "D1.7: D1.7" is a deferral artifact. In `.gobby/plans/completed/gdaemon-api-keys-nodes.md`
  D1.7 is 4.3.7 (e2e corpus schema_version 2), unrelated. The real obligation is D5 (lines 1089-1104):
  "Rules that read the edited file from disk cannot do so on a hub when the hook came from a node;
  the envelope must carry the fact. Which rules do this is unverified. Owned by S2.11."
- Prior draft `.gobby/plans/hook-envelope-file-facts.md` (stamped M1 at 74f5b4ec32, never approved,
  reference only; working tree equals the commit). 15 leaves: 1.1 ghook captures `file_facts`,
  1.3 daemon delivery scope carries `file_facts` and hook origin, 1.4 monolith projection and Rust
  TDD classification read `file_facts`, 1.6-1.15 foreign-origin fail-closed (git gates, ledger
  recount, TDD path identity, found-work evidence, source reads, lexical paths, post-tool edits,
  run_command, rtk rewrite, project resolution), plus deferrals D1-D3. Sections "D1.7 Mapping",
  "Persistence Inventory", "Site Disposition" exist. Do not assume its shape.
- Closed already: 1.5 -> #23537 (MultiEdit monolith guard), 1.2 -> #23538 (owner-only hook inbox).
- node-halves plan (`.gobby/plans/node-halves.md`, #24150) is approved and in implementation; it
  never mentions file_facts or #23277. #24364 (closed today) froze `POST /api/hooks/execute` in
  `docs/contracts/hook-ingress.md` (envelope v1: hook_type, input_data, source, critical,
  enqueued_at, response_capability, schema_version; X-Gobby-Envelope-Id, X-Gobby-Session-Id) with
  four corpus cases in `tests/contracts/http/`. #24368 Node hook ingress arm is open (forwards to hub).
  #24151 hub half (Rust rule engine) is blocked and unplanned: no cross-plan dep on it is valid.

## Decisions the spec must make (advisor)
- Additive member inside `input_data` (no contract change) vs a new envelope-level field (amends the
  frozen doc, re-records corpus cases, may bump schema_version). Pick and justify.
- Node arm (#24368) must forward the field unchanged, or the spec targets it.
- Scope rule consumers to the Python hub engine that exists; #24151 inherits the port.
- No deferred sections (Josh's no-deferral policy). Leaf count from D5 plus the rule inventory.
- Research still to do: the rule inventory (which rules/handlers read files or stat paths today:
  monolith guard, TDD classification, git gates, source-read rules...), the draft's 1.1 and 1.3,
  and the frozen contract doc.

## Findings after compaction (2026-10-14)
- Plan-sized: old inventory 15 sites + s1-s5. Will rewrite `.gobby/plans/hook-envelope-file-facts.md`.
- hub-mode 2.2 (not landed; `tests/hooks/test_session_binding.py` absent) binds envelope machine to the
  key: verified identity -> must equal key machine; none -> must equal hub machine; else 403
  `hook_machine_mismatch`. So node event == `event.machine_id != get_machine_id()`. Old decision 7 and
  D2 drop out; cross-plan dep on hub-mode 2.2.
- node-halves 1.5 forwards hub bytes unchanged; `src/gobby/servers/routes/node_hooks.py` (1.3) landed.
- Hub node-command client (hub-mode 1.3) not landed; `src/gobby/hub/node_commands.py` models exist.
  hub-mode 4.1 takes node-commands contract to v3 (adds `checkout.status` = branch/ahead/behind only).
- rust-rule-evaluation D1.6: `WorkspaceFiles` seam; "Files edited on a node are front-door D3's
  envelope-content requirement" (line 1027).
- Original obligation text (front-door D3 / api-keys D5): "Rules that read the edited file from disk
  cannot do so on a hub when the hook came from a node; the envelope must carry the fact."
  front-door 4.3.7 / D1.7 are corpus items, unrelated.
- `commit_guard.foreign_staged_commit_conflict` runs `ls-files`, `diff --cached`, `diff HEAD`, status
  via `_git_paths_async` / `_dirty_owned_paths_releasing_clean`; fail-closed for node = no node commits.
- Decision N1 sent to #15977 (msg f76dadc8): (1) recommended read-only node command (file facts +
  allow-listed read-only git) behind one hub seam; (2) envelope file_facts + node git command;
  (3) envelope only, git gates fail closed (rejected). Awaiting ruling.

## N1 ruling (#15977): option (1)
Read-only node command, one hub seam, local unchanged, unanswered node fails closed with named
diagnostic. No envelope/ingress/ghook change. Cross-plan edges: hub-mode 1.2, 1.3, 1.4, 2.2, 4.1.
Write the plan at .gobby/plans/hook-envelope-file-facts.md; Plan Adversary; #15977 reviews stamped plan;
send card replacement criteria with the plan SHA.

## Design notes (writer)
- Contract rule: "A new command or field bumps `v`" -> `checkout.read` ships as node-commands v4 after
  hub-mode 4.1's v3. Transport: Python -> gdaemon `POST /api/internal/nodes/{machine_id}/commands`.
- Git seam: all hook-path async git funnels through `DaemonGitService._execute`
  (`src/gobby/utils/daemon_git.py`, 686 lines; run/status both call it). Route there when the delivery
  origin is a node -> zero edits to commit_guard, code_review_scope, git_utils, task_dirty_state,
  ledger_reconcile, resolve_git_worktree_root_async. Sync git via `spawn.run` in hook paths must move to
  daemon_git: tdd_paths._git_out, validation_cover._tracked_at_head, code_navigation_recovery._git_ignored.
- Git argv seen: rev-parse (--show-toplevel, --path-format=absolute --git-common-dir, --verify
  --end-of-options), diff (HEAD/--cached, --name-status/--name-only/--stat/-z/--diff-filter), ls-files,
  ls-tree, status (porcelain v1 -z), log (-1 --format=%ct, -N --format=%H|%s), check-ignore --stdin -z,
  merge-base --is-ancestor, rev-list. Global opts: -c core.fsmonitor=false, --literal-pathspecs,
  --no-optional-locks, commit_guard `invocation.root_options` (-C, --git-dir, --work-tree).
- Origin: ContextVar set in `adapter_execution.run_adapter_hook` inner `run_adapter` next to
  `worker_staging_scope()` from `payload` machine_id vs `get_machine_id()` (bound by hub-mode 2.2).
- Loop bridge: `run_coro_blocking` (src/gobby/hooks/dispatchers/mcp.py) returns None on failure.
- Line counts now (>=850 need split): found_work_gate.py 882, validation_detection.py 862,
  workflows/hooks.py 922. commit_guard.py is 713 (was 956).
- Sites re-verified present on 0.5.0 (all 20 prior symbols).

## New sites found 2026-10-14 sweep (beyond the old 20)
- `hooks/_normalization_git.py::_positional_checkout_is_path`: sync spawn git rev-parse/ls-files -C cwd +
  `Path(path).exists()` (normalization).
- `hooks/_normalization_operands.py::_file_transfer_paths`: `probe.is_dir()` on destination.
- `hooks/shell_execution.py::path_execution` (+ line 137 script read): reads executable header bytes
  (native magic, shebang) -> files op needs base64 head bytes.
- `hooks/event_handlers/_misc.py::handle_worktree_create`: creates a git worktree ON THE HUB from
  event.cwd -> node event must answer allow before any hub resolution (provider default).
- Provider files: `adapters/claude_code.py::_saved_mcp_result`, `adapters/grok.py::_spilled_mcp_output`,
  `adapters/codex_impl/hooks_adapter.py::_command_execution_result` (transcript) -> node event: no read,
  existing missing-file branch (hub-mode Decision 4 precedent: no transcript snapshot for other machine).
- `workflows/condition_helpers.py::tdd_gate_open` resolves project_path + tdd_path_identity (site 4).
- Not working tree (no change): `_agent._load_agent_prompt` (bundled), `profile.read_user_profile_content`
  (already node-aware), `step_tool_input` (daemon cwd context).
- Finding outside scope, report to #15977: `hooks/terminal_context.py::_is_fresh_seat_tui` inspects hub
  processes with psutil for node events (session identity; hub-mode 2.2 area).
- `daemon_git.status` coalescing key must include the origin machine.
- files op entry shape idea: resolved, kind (file|dir|other|missing|outside), checkout_root,
  git_common_dir, size, line_count, text (capped, utf-8 replace), truncated, head_b64.
- Node project-managed rule: path inside a confinement root (project checkout + node workspaces).

## _tool.py post-tool (read 2026-10-14)
- `_record_successful_file_mutation` -> `_resolve_repo_edit_paths(file_path, cwd, project_id)`: expanduser,
  resolve, `find_project_root` (hub marker walk), `get_project_context(target_root)` -> hub disk. Then
  `_notify_code_index(repo_root, rel)` (hub gcode trigger via `ProjectIdResolver.resolve_for_index`),
  `_paths_landed_before_edit` (git log via `paths_committed_after_async` -> daemon_git, routed by seam),
  `record_edited_files(session_id, paths, checkout_root=...)`.
- Node event plan: repo root = enclosing confinement root from node files facts (checkout_root), project
  check via the machine's registered rows; skip hub `_notify_code_index` (node runs its own gcode,
  node-halves 2.11) ; ledger rows machine-scoped.

## Draft leaf outline (writer, not final)
P1 Node checkout reads:
- 1.1 node-commands v4 `checkout.read` contract (docs/contracts/node-commands.md, crates/gdaemon/src/
  node_commands.rs, src/gobby/hub/node_commands.py, corpus.json, parity tests). Ops: `files`
  {project_id, cwd, paths, content: none|head|text} and `git` {project_id, cwd, args, input, timeout_ms}.
- 1.2 node handler (Rust, gdaemon nodes/agents/checkout_read.rs): confinement = that project's registered
  checkout root on this node + node workspaces for the project; outside -> kind "outside", no content;
  git allow-list (subcommands + global opts above), GIT_OPTIONAL_LOCKS=0, output cap, deadline.
- 1.3 hub seam `src/gobby/hooks/checkout_reads.py`: origin ContextVar set in run_adapter; node calls on
  daemon loop (run_coroutine_threadsafe / run_coro_blocking); typed `CheckoutReadUnavailable` with
  named diagnostic; local implementation of `files` = Python oracle; Python/Rust parity fixture.
- 1.4 git routing: `DaemonGitService._execute` routes node-origin calls; status key gains origin; sync
  git sites (tdd_paths._git_out, validation_cover._tracked_at_head, code_navigation_recovery._git_ignored,
  _normalization_git._positional_checkout_is_path) use a seam sync git call.
P2 Hub sites:
- project path/resolution (workflows/hooks.py 922 lines -> split; project_context; startup_claim_preflight;
  project_checkout_ingress skip for node); path scope (_path_scope node facts); monolith (projection +
  recount); rust TDD evidence + tdd_gate_open; code navigation reads; post-tool edits + ledger machine
  scope (task_claim_state); validation_detection (862 -> split); write scope + plan write realpath;
  shell normalization reads (operands is_dir, shell_execution headers); worktree_create allow for node;
  provider-file reads skip; run_command effects + rtk dispositions; found_work_gate (882 -> split).
Still to read: project_context, startup_claim_preflight, project_checkout_ingress, found_work_gate
(_terminal_failures, _foreign_owned_dirty_paths), validation_cover.surviving_path_failure,
validation_detection, condition_helpers_paths.write_paths_within, blocking.plan_write_paths_allowed,
run_command_effects._apply_run_command, proxy_hooks._run_rtk_proxy, ledger_reconcile,
task_claim_state (normalize_task_checkout_root, record_edited_files), node-halves 2.7/2.9 node state
(confinement roots source), node channel frame limits (none configured).

## Status
- FYI sent to #15977 (park history, frozen contract, live cards, D1.7 artifact, closed leaves).
- Josh's new-work pause on task filing and graph edits still applies; seat guidance says task edits
  go through the Orchestrator, so send card text to #15977 to apply rather than calling update_task.

## Design v2 (writer, after advisor; 2026-10-14 ~20:00 CDT) — WRITE THE PLAN FROM THIS
Sent #15977 msg 0c04009c (send is coroutine on daemon loop; extra edges node-halves 2.1/2.3/2.7 + hub-mode 4.2;
2.2.7 supersession; Security Reviewer LAND gate on node handler; worktree_remove/_resolve_project_path/found_work findings).
CORRECTION TO SEND #15977 with the plan: confinement is NOT hub-named. Adapters normalize (path scope via
normalize_tool_fields, e.g. claude_code.py:260) BEFORE hook_manager resolves the project (hook_manager.py:330/343), so
the hub has no project/checkout row at normalization time. Confinement is node-derived from cwd:
- R0 = nearest ancestor of canonical cwd holding .gobby/project.json (find_project_root semantics); its id = project.
- A path is admitted iff canonical path (Path.resolve strict=False semantics, ~ expanded with NODE home, relative
  joined to cwd) is under R0, or its nearest .git checkout shares R0's git common dir AND holds a marker with R0's id
  (mirrors _path_scope._is_project_managed_path + marker check). Everything else kind "outside", no data.
- Any path under the node's <gobby_home> except <home>/worktrees/ and <home>/clones/ is outside (pin bootstrap.yaml
  outside with an isolated home test; never the live file). No marker above cwd -> error no_project_root.
- So hub-mode 4.2 owned_workspace_path edge is NOT needed (workspaces carry a copied marker); drop node-halves 2.3/hub
  4.2 edges unless used; keep node-halves 2.1 (NodeAgents/mod.rs) and 2.7 (commands.rs dispatch).
Contract v4 `checkout.read` payload: {cwd, query: {kind:"files", paths:[str], read_limit:int|null} |
  {kind:"git", args:[str], stdin:b64|null, timeout_ms:int}}. Reply: {root, project_id, files:[entry]|null,
  git:{exit_code:int|null, timed_out:bool, stdout:b64, stderr:str(<=64KiB), truncated:bool}|null}.
  Entry: {path, resolved|null(outside), kind file|dir|other|missing|outside, checkout_root|null, git_common_dir|null,
  project_root|null, data:b64|null (first read_limit bytes), truncated:bool}. Reply cap 8 MiB total; <=256 paths.
  Errors stage "none": invalid_payload (shape, refused argv, cwd not dir), no_project_root, outside (git cwd/-C).
  Nullable fields required (4.1 strictness).
Git allow-list (node): subcommands rev-parse, diff, ls-files, ls-tree, status, log, check-ignore, merge-base,
  rev-list; global opts only -c core.fsmonitor=false, --literal-pathspecs, --no-optional-locks, --no-pager,
  -C/--git-dir/--work-tree (path must be admitted); refused options: --output*, --no-index, --ext-diff, --textconv,
  -O*/--orderfile*, --exclude-from*, --open-files-in-pager. Node strips GIT_* from its env, sets
  GIT_OPTIONAL_LOCKS=0, stdout cap 8 MiB (truncated -> hub GitFailed checkout_read_truncated), deadline timeout_ms.
  Config-driven commands (filters, fsmonitor) behave as the hub's local git today: no new execution surface.
Seam src/gobby/hooks/checkout_reads.py (new): CheckoutReadUnavailable(OSError) str "checkout_read_unavailable:
  <code> (machine <id>)"; delivery_scope(machine_id, loop, cwd) set in adapter_execution.run_adapter_hook.run_adapter
  beside worker_staging_scope when payload input_data.machine_id != get_machine_id() (hub-mode 2.2 binds it);
  node_origin(); event_machine_id(); path_facts(paths, *, cwd=None, read_limit=None) (memo per delivery; first
  unavailable answer latches for the rest of the delivery); cwd_root(); run_git(args, *, cwd, timeout,
  input_bytes=None) sync (local = spawn.run(["git","-C",cwd,*args]) unchanged; node = git query);
  run_git_async used by DaemonGitService._execute. Node calls run on the captured daemon loop via
  run_coroutine_threadsafe; sync call on that loop thread -> unavailable code loop_thread. files timeout 5,000 ms;
  git timeout = caller's timeout. Content only in the per-delivery memo: never HookEvent.data/metadata, logs, DB, broadcaster.
Local events: every site keeps its current code; only a node_origin() branch is added (ruling: local reads as today).
Parity: golden crates/gdaemon/tests/fixtures/node_commands/checkout_read_facts.json (fixture tree spec + expected
  facts) checked by Rust handler test and by Python test against _path_scope.checkout_root/_git_common_dir,
  find_project_root, Path.resolve.
Leaves:
 P1 1.1 contract v4 (docs/contracts/node-commands.md, crates/gdaemon/src/node_commands.rs::*, src/gobby/hub/
   node_commands.py::*, corpus.json::*, corpus/parity tests) after hub-mode 4.1. 1.2 node handler
   crates/gdaemon/src/nodes/agents/checkout_read.rs (new), commands.rs, mod.rs, tests/node_checkout_read.rs (new);
   Security Reviewer LAND gate. 1.3 seam + run_adapter_hook + tests/hooks/test_checkout_reads.py +
   tests/hooks/checkout_read_fakes.py. 1.4 git routing: daemon_git DaemonGitService._execute (node branch maps reply
   to GitOk/GitFailed/GitTimeout, decoding stdout as _run_git_worker does), DaemonGitService.status key gains origin
   machine; sync sites tdd_paths._git_out, code_navigation_recovery._git_ignored,
   _normalization_git._positional_checkout_is_path (+ its Path.exists) onto seam run_git. validation_cover is
   unreachable for node sessions (found_work transcript refused) -> consumer unchanged. git_utils sync fns have no
   hook-path callers.
 P2 2.1 node project + project_path: project_context.resolve_hook_project_context node branch (after session paths:
   LocalProjectCheckoutManager(db).list_for_machine(machine) longest enclosing root_path, lexical; no match ->
   ValueError like local no-marker; unusable cwd -> skip current-context); no ProjectIdResolver registration for node;
   workflows/hooks.py (922) SPLIT: move _resolve_project_path body + _NO_REPO_* + _is_known_no_repo_project to new
   src/gobby/workflows/hook_project_path.py; node branch returns LocalProjectCheckoutManager.get(machine, project)
   root when worktree_root is it, else worktree_root (routed git), no hub Path.resolve; tests/workflows/
   test_workflow_hooks.py import update. Edge blocks hub-mode 2.2 (2.2.7 superseded for node events).
 2.2 path scope: _path_scope current_project_root/resolve_tool_path/checkout_root/_git_common_dir/
   _is_project_managed_path node branches from facts; _normalization_git checkout_root(Path(git_cwd).resolve()) at
   343/415/433; blocking.plan_write_paths_allowed realpath; condition_helpers_paths.write_paths_within (unavailable -> False).
 2.3 monolith: _project_root, _projection_for_path (read_limit 8 MiB; truncated -> unverifiable block),
   outstanding_monolith_paths recount batch.
 2.4 rust TDD: rust_test_evidence._read_absolute_text (missing -> None as local; unavailable/truncated ->
   rust_edit_is_test_writing False); condition_helpers.tdd_gate_open via tdd_paths (1.4 covers git).
 2.5 code navigation: _verified_source_line_count (read_limit _LINE_COUNT_SCAN_LIMIT+1), _ignore_decision
   (.git probes, .gobby/gcode.json), resolve paths via 2.2.
 2.6 post-tool edits: _tool.py _resolve_repo_edit_paths/_record_successful_file_mutation node branch (facts
   checkout_root; unavailable -> lexical path under project_path), skip _notify_code_index for node (node-halves
   2.11); commit_guard._live_path_owners gains machine_id filter (sessions.machine_id = event_machine_id()) through
   _owned_dirty_paths/foreign_*; MCP callers default local.
 2.7 validation detection: SPLIT validation_detection.py (862): move PROJECT_VALIDATION_DETECTION_KEY +
   load_project_validation_detection to new src/gobby/config/validation_detection_project.py (node branch reads
   <project_path>/.gobby/project.json via seam); validation_detection imports it (projects.py, tests unchanged).
 2.8 shell reads: shell_execution.path_execution (read_limit _HEADER_LIMIT), read_script_body (SCRIPT_BODY_LIMIT+1),
   _normalization_operands._file_transfer_paths is_dir, _normalization_canonical resolve (via 2.2).
 2.9 node-event dispositions: _misc.handle_worktree_create and handle_worktree_remove answer allow before any hub
   resolution/effect; provider files (claude_code._saved_mcp_result, grok._spilled_mcp_output,
   codex hooks_adapter._command_execution_result) no read -> existing missing branch; proxy_hooks._run_rtk_proxy
   skip; run_command_effects._apply_run_command skip; found_work_gate pinned no-change test
   (RemoteSessionOwnershipError -> ()); startup_claim_preflight unchanged (require_local_machine_id first).
Scope boundary: MCP tool calls by node sessions are not hook deliveries (seam unset -> local); owned by hub-mode
route families. #24151 inherits the seam. Drop old draft's D1.7 Mapping + Persistence Inventory; short
content-handling paragraph instead. Reuse Site Disposition legend with node-read replacing envelope-fact.
