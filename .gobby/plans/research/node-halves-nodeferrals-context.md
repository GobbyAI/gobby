# node-halves no-deferrals revision: working context

Working notes for the Plan Writer (gobby#16767) revising `.gobby/plans/node-halves.md`
after Josh's rulings "No deferrals", "Complete plan" and "No second version. Plan
fully or explain why you can't." (relayed by Orchestrator #15977, memory 878d0c35).

## Plan state (uncommitted edits in the working tree)

- Committed base: `1a4e96a` (M1 withdrawn). Adversary 16791 bounced it as NH-11
  (D1-D3 TBD task_refs; Decisions 3, 4, 13; Q9; the 2.7 provider check). I replied
  that the revision is in progress and asked it to hold until I send a SHA.
- Done in the working tree (not committed, not validated):
  - Overview: "changes none of it" replaced with the v2/P3 sentence.
  - Decision 1: P2 and P3 under #24148; 2.1 depends on 1.6 and 3.1; 3.3 on 1.3.
  - Decision 3: five providers.
  - Decision 9: contract version 2 + builder + hub-mode 2.5 edit;
    credential_manager=None stays.
  - New decisions 20-24 after 19 (live proof, preflight 2.11, v2 sandbox, remote
    resume reroute, gmcp).
- Still to edit: Decision 4 (held for D), Decision 13 (drop "deferred section
  D3" sentence and point at Decision 20), Constraints, P2 framing, 2.3-2.7,
  new 2.10 and 2.11, new P3 (3.1, 3.2, 3.3), remove D1 and D3 sections (D2 held),
  V1 entry, V2 (drop D3 line, add the new tests), hub-mode.md edits.

## Rulings in force

1. Five providers on nodes. D1 removed.
2. ITEM A: this plan owns `src/gobby/hub/spawn_payload.py::build_spawn_payload`
   with full tests; v=2 on both sides. Edit hub-mode.md 2.5 in the same commit.
   credential_manager=None STAYS (confirmed by #15977).
3. ITEM B: hub-mode D4 owns the live proof. Remove D3; reference in prose only.
4. ITEM C: leaf 2.11 node code-index preflight, blocked at expansion by #21586
   route leaves; plan it fully; state the cross-plan edge.
5. ITEM D (pane detection): STILL OPEN with Josh. Hold D2 and Decision 4.
   My recommendation was (a): contract `terminal.snapshot` command, hub routes
   node-terminal snapshots over the channel like hub-mode 2.3 writes, and
   hub-mode 2.1's pane-reading loops include node runs.
6. ITEM E: v2 `sandbox` = hub's fully resolved `SandboxConfig`; drop `network`.
7. ITEM F: resume AUTO-REROUTES to the resumed session's machine, overriding
   `machine` and origin (NOT a refusal). Plan the hub-mode 2.5 edit that way.
8. ITEM G: nodes write `~/.gobby/bin/gmcp`; 2.3 and 2.6 blocked at expansion by
   gmcp-stdio 1.6 (#23275).
9. Defects to fix (accepted by #15977): codex typed prompt delivery; Rust
   ControlClient lacks snapshot; 2.6 omitted codex MCP overrides, api env,
   extra_env. Note each hub-mode cross-plan edge in the plan.
10. Keep Decisions 18 (unlaunched runs fail on restart) and 19 (300 s, 24 h).
11. Notify Lane 5 LM gobby#16719 of the hub-mode edit after commit. hub-mode's M1
    becomes stale (already told #15977).

## Design for the revision

### P3 (new phase, leaves under #24148), placed after P2 and before V1

3.1 Contract version 2 [category: code]. Targets: `docs/contracts/node-commands.md`,
`crates/gdaemon/tests/fixtures/node_commands/corpus.json`,
`crates/gdaemon/src/node_commands.rs::*` (scope: many structs),
`src/gobby/hub/node_commands.py::*`, `crates/gdaemon/tests/node_command_corpus.rs::corpus_round_trips`,
`tests/hub/test_node_command_corpus.py::test_corpus_round_trips`.
Rust structs: Sandbox, SandboxNetwork (remove), SpawnPayload, ReportExit, etc.
Python: Sandbox, SpawnPayload, etc. `v` becomes 2 on frames and node bodies; the
node handler accepts only 2.
`agent.spawn` v2 payload adds:
- `sandbox`: full SandboxConfig {enabled, backend, mode, allow_network,
  extra_read_paths, extra_write_paths, extra_deny_read_paths,
  extra_deny_write_paths, allowed_domains, denied_domains, allow_git_network,
  allow_package_registries, allow_unix_sockets} (ruling E).
- `agent_name` (string|null): drives the reviewer cases.
- `api_base`, `api_token` (string|null); `extra_env` (object string->string, from
  endpoint.child_env).
- `codex_oss_provider` (string|null), `codex_config_overrides` (array).
- `resume_session_id` (string|null; provider-native id when
  resume_metadata.resume_existing_session), `execution_mode` (string|null; from
  `_runtime.py` resume_metadata["execution_mode"]; "interactive" makes grok run
  in terminal mode).
- `droid_mode` ("exec"|"interactive"), `prewarm_pre_commit_store` (bool),
  `code_index_preflight` (string|null mode).
Hub-flow code table: remove `remote_resume_unsupported` (contract line 95).
New node bodies (v2): `POST /api/nodes/runs/prompt-delivery-failure`
{v, run_id, error} -> {failed}; `POST /api/nodes/runs/code-index-warning`
{v, run_id, warning:{preflight,cwd,message}, prompt} -> {}.
Field closure (write per field in 3.1): not carried + reason:
- `initial_variables`: hub DB write already done by `prepare_terminal_spawn`
  (`spawn.py:272-278`); agy's second merge is redundant on the hub path.
- `is_local`: run-row attribute recorded hub-side; executor never reads it.
- `terminal_lifetime`: executor never reads it; `persistent_role` is web_spawn only.
- `sandbox_args`/`sandbox_env`: executor never reads them (resume path only).
- `resume_metadata_json` bookkeeping (`_record_resume_launch_details`): post-launch
  details derive deterministically from the payload on the node; a resume of a node
  run is a fresh `agent.spawn`, so the node reports none of them.
- `auto_approve`: always True (`_request.py`), no field.

3.2 Hub spawn payload builder [category: code] (depends 3.1). Targets:
`src/gobby/hub/spawn_payload.py` (new), `tests/hub/test_spawn_payload.py` (new),
`src/gobby/agents/spawn_executor_providers.py::_agent_prompt_prefix` (extract the
body into a request-free `agent_prompt_prefix(db, agent_name, initial_variables,
project_id)` and have `_agent_prompt_prefix` call it).
Hub sources: `_request.py::build_spawn_request` inputs: `endpoint`
(SpawnGenerationEndpointResolution: model, is_local, codex_oss_provider,
codex_config_overrides, api_base, api_token, child_env), `reasoning.effective_effort`,
`sandbox_config` (from `resolve_spawn_sandbox`), `resume_metadata`
(provider_native_session_id + resume_existing_session, execution_mode),
`code_index_mode`, `prewarm_pre_commit_store`, `droid_mode`, `agent_name`.
Builder steps: codex persona prefix (`agent_prompt_prefix`) prepended to the
prompt; merge `_agent_context_injected: True` into the child session variables
before send (Python `spawn_executor_codex.py`); workspace mapping moved from
hub-mode 2.5 step 4.

3.3 Hub routes for node run events [category: code] (depends 1.3, 3.1). Targets:
`src/gobby/servers/routes/node_runs.py` (new, `create_node_runs_router(server)`),
`src/gobby/servers/_app_routes.py::register_routes`, `tests/servers/test_node_run_routes.py`.
Identity rules exactly as 1.3's node_hooks.py (403 node_identity_required,
machine_is_local, 422 unsupported_version/invalid_body); refuse a run on another
machine. prompt-delivery-failure calls `run_manager.fail_uninitialized_prompt_delivery(run_id, error=)`
and answers {failed: transitioned}. code-index-warning merges session variables as
`_prepare_managed_code_index`'s failure branch: additional_skills minus
`gobby:references/code-index/overview.md`, code_index_preflight_warning, prompt.

### P2 changes

- 2.1: depends (1.6, 3.1).
- 2.3: droid `.factory/hooks/hooks.json` via `_copy_droid_hooks_for_isolation`
  (template `src/gobby/install/droid/hooks-template.json` via `get_install_dir()`,
  `rewrite_hook_template_commands(cli_name="droid", hooks_dir=GOBBY_HOOKS_DIR or
  ~/.gobby/hooks)`, merge `merge_gobby_hook_groups` over `DROID_PASCAL_HOOK_NAMES`);
  agy has no hook dir copy (`_copy_cli_hooks` covers claude .claude, codex .codex,
  droid generator). Python env seed ported (`python_env_seed.py`): skip unless
  pyproject.toml and uv.lock; `uv` on PATH; clear UV_CACHE_DIR,
  UV_PROJECT_ENVIRONMENT (+ rest of `_UV_ENV_TO_CLEAR`), UV_PYTHON_DOWNLOADS=never;
  `uv cache dir` (10 s, fallback ~/.cache/uv); skip if cache dir missing;
  `uv sync --offline --frozen --no-progress --link-mode copy --cache-dir <dir>`
  180 s; failures only warn. Repair order: project.json, seed, MCP patch.
  MCP patch writes gmcp (ruling G), blocked by gmcp-stdio 1.6.
  Needs Granularity line (>6 items).
- 2.4: PolicyInputs takes the v2 sandbox spec (ruling E); drop the trusted seed
  include_str and agent_sandbox_config(None)/definition_sandbox_config research;
  node applies `spawn_cache_policy.py::sandbox_config_for_spawn` (project write paths
  from `<source_root>/.gobby/project.json` via get_project_sandbox_config,
  `apply_spawn_cache_policy`, `_apply_sandbox_cache_policy`, `sandbox_write_paths(env)`,
  `allow_package_registries: true`) and the nextest grant
  (`<source_root>/.nextest` when declared in source_root's project sandbox config);
  `allow_run_unix_sockets=True`; pre-commit store prewarm
  (`sandbox_policy.py::_prewarm_pre_commit_store`, gated by payload flag); tables for
  droid and agy (sandbox_policy.py lines 46-96 have droid entries). 2.4.3 rewritten.
- 2.5: claude reviewer home (`claude_sandbox_home.py::prepare_claude_reviewer_home`,
  108 lines; when backend srt, provider claude and agent_name task-close-reviewer,
  auto_approve True); droid ps shim (#22407, `droid_ps_shim.py` 102 lines,
  `srt_runtime.py` 799-807 and `ps_shim_path_env`).
- 2.6: five providers. Per-provider env: claude ANTHROPIC_BASE_URL/ANTHROPIC_AUTH_TOKEN;
  codex OPENAI_BASE_URL/OPENAI_API_KEY; grok GROK_API_BASE/XAI_API_KEY; droid
  FACTORY_API_KEY/FACTORY_API_BASE_URL; agy none. `_apply_extra_env`: pop
  GOBBY_AGENT_API_TOKEN, PATH merged (`merge_spawn_path_env`), skip
  `_RESERVED_EXTRA_ENV_KEYS` (ALL_TERMINAL_ENV_VARS, SPAWN_CACHE_ENV_VARS,
  GOBBY_MACHINE_ID). Claude reviewer under SRT: CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1,
  CLAUDE_CODE_DISABLE_OFFICIAL_MARKETPLACE_AUTOINSTALL=1. Codex: overrides =
  `_codex_mcp_config_overrides` (gmcp per ruling G; required=true,
  startup_timeout_sec=120, tool_timeout_sec=360, runtime overrides, six preapproved
  tools) + payload codex_config_overrides + reviewer overrides (features.plugins=false,
  features.remote_plugin=false, unified-computer-use disabled, node_repl disabled) +
  sandbox_mode="danger-full-access" under SRT; `--oss --local-provider` when
  codex_oss_provider; headless (`codex exec`, prompt on argv) only for
  task-close-reviewer under enforced SRT (`codex_launches_headless`); otherwise no
  argv prompt (2.10 types it). Grok mode terminal when execution_mode interactive.
  Droid: `exec` unless droid_mode interactive (command_builder 169-186), prompt last.
  Agy: `--sandbox=false` under enforced SRT; version gate on node binary
  (version_gate.py: AGY_REQUIRED_VERSION 1.1.18, regex `v?(\d+\.\d+\.\d+)`, cache by
  realpath/dev/ino/size/mtime, refusal text = record.reason, unpublished/revalidating
  reasons) -> prepare_failed. Resume flags per command_builder. pre_approve_directory
  for all five (claude only when auto_approve). Needs Granularity line.
- 2.7: provider admission is the five; v must be 2; codex and others.
- 2.10 Codex prompt delivery (new; depends 2.6, 3.3): Targets
  `crates/gterminals/src/control.rs::ControlClient` (add `snapshot(host_terminal_id,
  mode, max_bytes, max_lines)` with the mode-echo check like
  `src/gobby/terminals/host_client.py::snapshot`), `crates/gdaemon/src/nodes/agents/codex_prompt.rs`,
  mod.rs, tests. Constants (`spawn_executor_support.py` 244-255): marker "›",
  capture 40 lines, poll 1 s, ready 60 s + retry 45 s, probe 5 s, settle 1 s, Enter
  retry delay 3 s, failure pane 1024 chars with "[truncated]\n", kill grace 5 s.
  Flow: poll snapshot until marker; liveness via list(); exit before composer ->
  failure code codex_terminal_exited_before_composer; timeout -> codex_composer_not_ready;
  then write text prompt, 3 s delay, key enter (gterminals write); indeterminate ->
  log only; refusal -> codex_prompt_delivery_failed. Failure: POST 3.3
  prompt-delivery-failure; terminate (TERM, 5 s, KILL) only when the hub answers
  failed=true. Pane text redaction: Python `redact_terminal_output` -> port or
  hub-side? DECIDE: send raw pane tail to the hub route and let the hub redact
  (route calls `_codex_prompt_failure_reason`-equivalent with redaction).
- 2.11 Node code-index preflight (new; depends 2.6, 3.3; blocked at expansion by
  #21586): `gcode status --quiet --format json --allow-stale --project <ws>` (5 s),
  `gcode index --quiet --project <ws>` (rest of 120 s), `gcode search-content
  __gobby_code_index_smoke__ --limit 1 --quiet --allow-stale --project <ws>` (10 s);
  env = identity (GOBBY_AGENT_RUN_ID, GOBBY_PROJECT_ID, GOBBY_SESSION_ID,
  GOBBY_MACHINE_ID) + GOBBY_DAEMON_URL node front door (gcode reaches #21586's
  routes through the relay). Failure: append `_append_code_index_warning` text to
  the prompt, POST 3.3 code-index-warning. No managed credential.
- Pane detection: HELD (ITEM D).

### hub-mode.md edits (same commit)

- Decision 13 prose (lines ~147-149): drop the resume refusal; resume reroutes.
- Line ~514 code list: drop `remote_resume_unsupported`.
- 2.5 preamble (~1443): no resume refusal; a request with resume_session_id
  resolves the resumed session's machine (resolve_resume_target) and routes there,
  overriding machine and origin. Placement stays refused.
- Step 4: call `build_spawn_payload` (node-halves 3.2), cross-plan edge set at
  expansion. Keep credential_manager=None. Hub agy gate (`_implementation.py:265`,
  `ensure_agy_support`) applies only to a local result; the node gates remote agy.
- Acceptance 2.5.7 (`test_unsupported_routes_refused`): placement refused, resume
  routed to the session's machine.

## Source facts (kept from earlier research)

- No code builds `agent.spawn` today; `src/gobby/hub/` holds only `node_commands.py`.
- Codex typed delivery rationale: #19953 (Codex 0.147+ cancels MCP startup when a
  first turn begins).
- Hub pane consumers that are machine-scoped (local runs only): idle_check_handler
  (list_active_for_machine), lifecycle_monitor, agent_health; terminal_prompt_monitor
  is driven by lifecycle_monitor.
- `_issue_prelaunch_credential` (spawn.py ~476) issues the managed Postgres role.

## Session 2 (2026-10-14) progress and facts

Done in the plan this session (uncommitted): Decision 13 points at hub-mode D4 (#24283);
new writer Decisions 25 (loopback endpoint refused for remote spawn, code
`remote_loopback_endpoint`, stage none, hub-mode 2.5 step 1) and 26 (api_token only in
memory and provider env); Constraints rewritten (launch-input split, cross-plan edges
list, monolith adds control.rs 733 and providers.py 626, S2.8 overlap names #23213's
targets); P2 framing (five providers, chain 2.1-2.11, 2.10/2.11 depend 3.3); 2.1
depends (1.6, 3.1). Still to write: 2.3-2.9 edits, 2.10, 2.11, P3, remove D1/D3, V1, V2,
hub-mode edits. ITEM D still with Josh (#15977: options (a) Rust full-parity port of
idle_detector/prompt_detector/stall_classifier/auto-enter in gdaemon, (b) hook-only plus
a planned leaf). Either way D2 becomes planned leaves; no D edits until his answer.

Facts:
- No Rust terminal redactor. 2.10 body is `{v, run_id, code, detail, pane}`; hub
  route builds the error with `_codex_prompt_failure_reason(pane, code=, detail=)`
  (redact then 1024-char tail) and calls `LocalAgentRunManager(server.services.database)
  .fail_uninitialized_prompt_delivery(run_id, error=)` (storage/agents/_lifecycle.py
  606); answers `{failed: bool}`. Pane = last non-empty snapshot (max_lines 40,
  max_bytes 262144). Node terminates (TERM, 5 s, KILL) only on failed=true; route
  does not call cleanup_agent: the exit reaches the hub via the report, hub-mode 2.6
  settles a run already in error. AgentRun has `machine_id`; unknown/other-machine
  run -> 404 `run_not_found`.
- Code-index (src/gobby/agents/code_index.py 271-401, `_run_gcode` 719-784): with no
  credential env={} and the three commands run with identity env; GOBBY_AGENT_API_TOKEN
  popped. Errors: `gcode_not_installed`, `gcode_index_workspace_missing:<p>`,
  `gcode_index_timeout:deadline_exceeded`, `<timeout_code>:<n>s`,
  `<failure_code>:<rc>:<preview>`; codes status gcode_index_unavailable(_timeout),
  index gcode_index_failed/gcode_index_timeout, smoke gcode_search_content_failed/
  _timeout; `_process_detail` redacts postgres URLs, collapses whitespace, 500-char
  preview with "[truncated]\n". Warning text `_append_code_index_warning`
  (spawn_executor_providers.py 70-77). Node posts the warning before launch; non-2xx
  -> prepare_failed (Python's merge failure fails the spawn). Session vars via
  `SessionVariableManager(db).get_variables/merge_variables`.
- Codex prompt order: preflight appends warning, then persona prefix prepended.
  `_agent_context_injected: True` merged when persona injected (spawn_executor_codex.py
  22-38). Builder: `agent_prompt_prefix(db, agent_name, initial_variables, project_id)`
  extracted beside `_agent_prompt_prefix` (providers.py 80-107: resolve_agent +
  prompt_for("agent", variables=)).
- Codex overrides (spawn_executor_support.py 569-643): after gmcp-stdio 1.6 command
  gmcp, args []; required=true, startup_timeout_sec=120, tool_timeout_sec=360;
  runtime: TMPDIR, TMPPREFIX=<tmp>/zsh, mcp env TMPDIR, OCR_NO_UPDATE="1",
  GOBBY_HOME/GOBBY_DAEMON_URL/GOBBY_MACHINE_ID to mcp env and shell policy, identity
  vars to mcp env, SPAWN_CACHE_ENV_VARS + CARGO_TARGET_DIR to both; six preapproved tools.
- Env: `get_terminal_env_vars` (constants.py 197-261) gives GOBBY_DAEMON_URL, identity,
  build_spawn_cache_env (UV_CACHE_DIR per-session tempdir, CARGO_HOME shared), PATH
  via merge_spawn_path (managed tool bin), CARGO_TARGET_DIR
  ensure_agent_cargo_target_dir(checkout, project_id) (cargo_target.py 113). hub-mode
  2.5 skips it for remote runs, so the node computes it (port in 2.4 with corpus).
  SANDBOX_CACHE_POLICY 15 vars under tempdir `get_agent_session_cache_dir`
  (gobby/<name>/<safe>-<sha16>). sandbox_config_for_spawn spawn_cache_policy.py 118-155.
- Prewarm: sandbox_policy.py 784 `_prewarm_pre_commit_store` (needs
  .pre-commit-config.yaml, operator store, spare consume/clone/schedule; failures warn).
- hub-mode 2.5 does not use build_spawn_request. Builder takes the resolution objects
  like build_spawn_request (_request.py 16-103). hub-mode 2.5 step 1 must run the local
  resolvers: resolve_spawn_generation_endpoint (_generation_endpoint.py 41),
  resolve_spawn_reasoning, resolve_spawn_sandbox, code_index_preflight_mode,
  build_spawn_context, prewarm from agent body; refuse is_local endpoint (Decision 25).
  Hub agy gate only for local results. hub-mode 2.5.11 stays (observes payload).
- hub-mode tasks: 1.3 #24269, 1.4 #24270, 2.3 #24273, 2.4 #24274, 2.5 #24275, 2.6
  #24276, D2 #24281, D4 #24283; 1.1 #24268 landed (leave its v:1 frames).
- gmcp-stdio 1.6: `.mcp.json` `{"command": "<gobby_home>/bin/gmcp", "args": []}` plus
  the ~/.claude.json project scope for claude; 1.8 drops mcp_config_read_exceptions
  (2.4 blocked by 1.8).
- Droid hooks (isolation_repair.py 145-227): `.factory/hooks/hooks.json`, template
  `get_install_dir()/droid/hooks-template.json`, rewrite_hook_template_commands
  (cli_name droid, hooks_dir GOBBY_HOOKS_DIR or ~/.gobby/hooks), merge_gobby_hook_groups
  over DROID_PASCAL_HOOK_NAMES; failures only warn. `.claude`/`.codex` copytree, warn.
- control.rs index lists ControlClient methods unqualified: target
  `crates/gterminals/src/control.rs::*` with scope-reason; host `snapshot` request
  {host_terminal_id, mode, max_bytes, max_lines} -> {ok, mode, text, truncated,
  dropped_bytes, total_bytes}; refuse echo mismatch `snapshot_mode_mismatch`. Test in
  crates/gterminals/tests/protocol_contract.rs.
- 3.1 adds `CONTRACT_VERSION = 2` (Rust pub const, Python); corpus tests assert every
  message's v equals it; test_node_bodies_name_no_machine (tests/hub/...:47) gains the
  two new bodies and the `network` refusal. Contract code table: remove
  remote_resume_unsupported, add remote_loopback_endpoint.

## ITEM D ruled (Josh via #15977, 2026-10-14): "rust on the node now" (option a)

- #24148's node half ports `src/gobby/agents/lifecycle_monitor.py` to Rust at full
  parity: idle_detector, prompt_detector, stall_classifier, health and auto-enter. It
  runs in gdaemon beside the node's gterm and reports attention states to the hub over
  the node route from 2.8.
- Build it as ONE Rust module that #24149 (the hub half) reuses when the hub's Python
  monitor retires.
- Replace the D2 deferral (TBD-node-pane-detection) with planned leaves carrying
  dependencies, categories and parity tests against the Python detectors' existing
  fixtures. Rewrite Decision 4 to this ruling.
- Then: no open questions, no deferrals. Validate, commit, send to 16791 to stamp.
- Research needed next session: lifecycle_monitor.py and its detector modules and
  their test fixtures; how the hub consumes attention today (terminal_prompt_monitor,
  idle_check_handler, agent_health) and what the "node route from 2.8" body must carry
  (likely a v2 report field or a new node body in 3.1, with a hub consumer leaf in P3).
  Module placement: a gdaemon-or-shared crate module #24149 can reuse (check ROADMAP
  decision 16 and crates/AGENTS.md); the node uses ControlClient::snapshot (2.10).

## Session 3 (2026-10-14): ITEM D design locked (advisor-confirmed)

Facts:
- Pane reads in the monitor (all machine-scoped today): terminal_prompt_monitor 15 lines
  (trust, loop, approval), 30 (queued continuation), 40 ansi (periodic enter);
  idle_check_handler 15 (attention scan + idle check); agent_health 8 (provider stalls),
  50 (unhealthy error tail); lifecycle_monitor 764 `_defer_stagnation_for_live_pane` 15.
- Detectors are manifest-driven: `src/gobby/install/shared/detection/{agy,claude,codex,droid,grok}.toml`
  synced to table `detection_manifests(provider_id, content)`; `DetectionManifestRegistry`
  refreshes every 30 s. Engine: detection/{schema,matcher,safe_regex}.py (874 lines);
  Python `regex` with MULTILINE, size 2048, CPU budget -> pattern_timeout.
  droid.toml:133 uses a negative lookahead, so Rust needs fancy-regex (0.11 already in
  Cargo.lock, transitive); backtrack_limit maps to pattern_timeout.
- Tests: inline panes (test_idle_detector 75 tests, test_prompt_detector 44,
  test_stall_classifier 17); detection/test_matcher.py loads tests/agents/fixtures/
  {detection_manifest.toml,pane_approval.txt}; BundledDetectionRegistry in
  tests/agents/detection_test_support.py loads the bundled TOMLs.
- TmuxConfig is fixed at monitor construction (lifecycle_monitor.py:109): fields
  auto_enter_agent_terminals, auto_enter_agent_interval_seconds, auto_enter_approval_prompts,
  init_timeout_seconds, idle_*. LoopTracker(threshold=3). LOOP_PROMPT_DISMISS_MIN_INTERVAL 60 s.
- Attention: AgentAttentionTracker.sync = classify (prompt detector + stall classifier) then
  persist (AttentionStateManager.transition_async, lifecycle wait enter/resolve, metadata
  store "retrying provider" 30 s / "needs attention" 60 s). clear_after_injection on
  trust/approval injection.
- Idle policy (`_handle_idle_check`) pane facts: quota (watchdog/quota.py, codex only),
  capacity (codex CODEX_MODEL_CAPACITY_MESSAGE), idle status (unknown/context_full/idle/active),
  turn_in_flight, unsubmitted_input, queued_message_prompt. Transcript reads return
  (None, None) for a session on another machine (`_read_local_transcript`).
- Hub-local seams for remote runs: composer gate (`read_composer` -> unknown -> reprompt held
  forever), `TerminalServices.is_live`/`snapshot`, `terminate_managed_runtime_async`
  (capture.py 611, 649 lines). recovery.py is 990 lines: do not target.
  LocalAgentRunManager has `.db` (_manager.py:37). kill_agent has hub-mode 2.4's remote guard.
- Checkpoint (checkpoint_manager.py): git plumbing + hidden ref refs/gobby/ckpt/<task>/<seq>
  + DB row; rows have no readers besides storage.

Locked design:
- Module crates/gdaemon/src/pane_detection/ (outside nodes/, #24149 reuses); amend Decision 5.
- Corpus crates/gdaemon/tests/fixtures/pane_detection/corpus.json, Python + Rust parity tests.
- Payload v2 adds task_id and monitor {auto_enter_agent_terminals,
  auto_enter_agent_interval_seconds, auto_enter_approval_prompts, init_timeout_seconds}.
- GET /api/nodes/detection-manifests (3.5) with ETag; node refreshes on the 30 s tick.
- ReportAgent.pane {attention, injections, composer, idle}; hub consumer in 1.4's route
  after put (edge #24270).
- POST /api/nodes/runs/terminate {v, run_id, cause in doom_loop|provider_stall|timeout|
  init_timeout, detail, pane_tail, checkpoint} -> {terminated, retry}; 2.6 timer posts.
- Hub guards: capture.py terminate guard (kill_agent remote), services.is_live/snapshot,
  composer gate; idle handler covers remote runs. agent_health init-timeout conditions
  extracted (edge #24276).
- Scope: Josh's five names; transcript watchdog takes the non-local branch; memory watchdog,
  interactive_attention_monitor and DB sweeps unnamed. Flag the boundary to #15977.

### Session 3 plan-edit progress (uncommitted, not validated)

Done in node-halves.md: Overview (pane state, P3 consumers, Python monitor stays on hub),
Decision 1 (2.10, 2.11, 2.14 depend on P3 routes), Decision 4 rewritten (five names mapped
to modules; unnamed parts listed with where they stay), Decision 5 (pane_detection exception),
new Decisions 27 (manifest GET, 30 s ETag), 28 (monitor block in payload), 29 (fancy-regex,
invalid_pattern drops the rule, backtrack limit -> pattern_timeout), 30 (node terminates,
hub decides; idle-policy kills via capture guard -> kill_agent), 31 (checkpoint ref
refs/gobby/ckpt/<task_id>/<run_id>, hub records row).

Leaf layout to write:
- 2.12 Detection manifest engine + corpus [code] (no deps): crates/gdaemon/Cargo.toml
  (fancy-regex 0.11, toml), crates/gdaemon/src/lib.rs (mod pane_detection),
  crates/gdaemon/src/pane_detection/{mod.rs,manifest.rs,regions.rs},
  crates/gdaemon/tests/fixtures/pane_detection/corpus.json,
  crates/gdaemon/tests/pane_detection_corpus.rs, tests/agents/test_pane_detection_corpus.py.
  Acceptance: every bundled TOML rule compiles; schema refusals; regions; matcher cases.
- 2.13 Pane detectors [code] (depends 2.12): pane_detection/{prompt.rs,idle.rs,stall.rs,
  loop_tracker.rs,quota.rs,attention.rs}; corpus extended with cases lifted from
  test_prompt_detector/test_idle_detector/test_stall_classifier inline panes.
- 2.14 Node pane monitor [code] (depends 2.8, 2.10, 2.13, 3.4, 3.5): nodes/agents/
  pane_monitor.rs, checkpoint.rs; reports.rs gains `pane`; 2.6 timer posts terminate
  (update 2.6.4); init-timeout timing; manifest fetch job. Granularity line.
- 3.1 adds: payload task_id, monitor block; ReportAgent.pane {attention {reason, kind,
  fingerprint, payload, dismissed}|null, injections, composer, idle {status,
  turn_in_flight, unsubmitted_input, queued_message_prompt, capacity_message,
  quota {provider, reset_time}|null}|null}; terminate body/response {terminated, retry};
  detection-manifests GET shape.
- 3.4 Terminate route [code] (depends 3.3): node_runs.py; causes doom_loop|provider_stall|
  timeout|init_timeout; timeout applies cooperative_close_handoff_pending /
  bound_task_is_closed (retry=true) and _bootstrap_accounting_stall_error; init_timeout
  applies extracted session conditions from check_initialization_timeout (agent_health.py,
  edge #24276; initialized -> retry=false); records checkpoint row.
- 3.5 Detection manifest route [code] (depends 3.4): GET in node_runs.py with ETag.
- 3.6 Pane state from reports [code] (depends 3.1): src/gobby/hub/remote_pane.py (store +
  apply), attention_tracker.py split sync into classify/apply, one call in
  src/gobby/servers/routes/nodes.py after put (edge #24270).
- 3.7 Idle policy for node runs [code] (depends 3.6): idle_check_handler.py (remote runs,
  pane facts object), watchdog/composer_gate.py (remote composer from store),
  terminals/services.py (is_live/snapshot remote), agents/capture.py
  terminate_managed_runtime_async remote guard via kill_agent. Acceptance: remote run gets
  reprompted (composer gate), failed after max reprompts. Granularity line.
- Delete D1, D2, D3. Constraints: add edges 3.6->#24270, 3.4->#24276, 3.7 after hub-mode
  2.3/2.4 (#24273, #24274); monolith notes (capture.py 649, idle_check_handler 580,
  agent_health 587, recovery.py 990 untouched).

## Session 4 (2026-10-14): P3 re-sequenced, remote runtime seam (no plan text written yet this session)

Reloaded skills; read plan lines 1-310 and 831-1653 (2.1-2.9, D1-D3, V1, V2 as listed
in "Plan state"). Nothing new written to node-halves.md this session.

Revised P3 (supersedes the Session 3 leaf list numbering; sweep Decisions 1, 4, 27, 30
and Constraints to these numbers):
- 3.1 Contract version 2 [code] (no deps). Targets: docs/contracts/node-commands.md,
  crates/gdaemon/src/node_commands.rs::* (scope), src/gobby/hub/node_commands.py::*
  (scope), corpus.json, crates/gdaemon/tests/node_command_corpus.rs::corpus_round_trips,
  tests/hub/test_node_command_corpus.py::test_corpus_round_trips and
  ::test_node_bodies_name_no_machine. Contract doc headings: "## v1 commands" (rename
  "Commands"), Hub-flow table line 95 (remote_resume_unsupported -> remote_loopback_endpoint),
  Hub route codes table (add run_not_found 404), Node bodies (add terminate,
  prompt-delivery-failure, code-index-warning, detection-manifests GET), sandbox row 127.
  pane block: {attention {reason,kind,fingerprint,payload,dismissed}|null, injections u64,
  composer {state empty|draft|unknown, line|null}, idle {status unknown|context_full|idle|active,
  turn_in_flight, unsubmitted_input, queued_message_prompt, capacity_message,
  quota {provider, reset_time}|null}|null, tail string (last 15-line read, <=2,000 chars)}.
  Pane composer never reads "held" (idle_detector.py ComposerRead: only the ledger reads held).
- 3.2 Builder (depends 3.1) unchanged.
- 3.3 Node pane state on the hub (depends 3.1): src/gobby/hub/remote_pane.py (store keyed by
  terminal_id: machine_id, run_id, revision, pane; mark_stopped(terminal_id); entries for a
  machine replaced per report), attention_tracker.py::AgentAttentionTracker split sync into
  classify(run, pane)->AttentionClassification{reason,kind,fingerprint,payload,dismissed} and
  apply(run, c); injections counter rise -> clear_after_injection; one call in
  src/gobby/servers/routes/nodes.py (bare path; hub-mode 1.4 creates it) after put. Test
  tests/hub/test_remote_pane.py. Edge: 3.3 after #24270.
- 3.4 Remote terminal runtime (depends 3.3): src/gobby/hub/remote_runtime.py
  RemoteTerminalRuntime implements TerminalRuntime (runtime.py 222-256, 13 methods):
  is_live/session_present = listed in store and not stopped; snapshot/snapshot_full = stored
  tail (SnapshotResult text,truncated,dropped_bytes,total_bytes); terminate = hub-mode 2.4
  remote_cancel.cancel(run...) TERM then store.mark_stopped on stopped/not_found; writes raise
  TerminalWriteError(stage="none") (hub-mode 2.3 guard means unreachable); spawn/resize/
  attach_locator raise. services.py::TerminalServices.runtime_for returns it when
  terminal.machine_id is not local (one guard; covers capture.py terminate_managed_runtime_async,
  terminal_cleanup._close_tmux_session, recovery._terminalize_idle_agent, agent_health
  _terminate_tmux_run, lifecycle doom loop). composer_gate.py::composer_refuses_automation:
  remote terminal reads store composer (missing -> unknown); only "empty" passes. capture.py
  NOT edited. Edges: 3.4 after #24273 and #24274.
- 3.5 Hub routes for launch outcomes (depends 1.3, 3.4): node_runs.py, _app_routes.py,
  tests/servers/test_node_run_routes.py. prompt-delivery-failure mirrors
  spawn_executor_support.py::_fail_codex_prompt_delivery: fail_uninitialized_prompt_delivery,
  then runtime_for(terminal).terminate (agent.cancel), then
  server.services.agent_lifecycle_monitor.get_cleanup_agent()(transitioned, terminal_payload=error,
  preterminalized_run=transitioned); answers {failed}. Node does not kill itself.
- 3.6 Terminate route (depends 3.5): node_runs.py + src/gobby/agents/agent_health.py::*
  (scope: extract module functions session_never_initialized(run, session) from
  check_initialization_timeout and bootstrap_accounting_stall_error(run, session, *,
  age_seconds, pane_snapshot) from _bootstrap_accounting_stall_error). Route: 404
  run_not_found; is_recovery_protected or ended -> {terminated:false, retry:false/true};
  timeout: not interactive and (cooperative_close_handoff_pending or bound_task_is_closed) ->
  {false, retry:true}; reason "Agent exceeded {t}s timeout" + "\n\n--- Last terminal output
  ---\n" + tail[-2000:], bootstrap check swaps reason, action timeout/fail; init_timeout:
  initialized -> {false, retry:false}; reason "Provider connection timed out: agent never
  initialized after {age:.0f}s (provider=..)"; provider_stall: "Provider stall: {reason}
  (provider=.., consecutive_hits=..)\n\n{pane}"; doom_loop: "doom loop: dismissed loop prompt
  {threshold}+ times". Record checkpoint row LocalCheckpointManager(db).create(Checkpoint(...))
  (src/gobby/storage/checkpoints.py) with node ref. Store body tail in 3.3 store, then
  terminate_managed_runtime_async(storage=run manager, run, terminal, runtime=services.runtime_for,
  action, reason, terminalize=cleanup wrapper as lifecycle_monitor._checkpoint_and_kill_looping_agent).
  Answer {terminated: result.success, retry: not success}. Edge 3.6 after #24276.
- 3.7 Manifest route (depends 3.6): GET, SELECT provider_id, content FROM detection_manifests
  ORDER BY provider_id; ETag = sha256 over provider_id + sha256(content) pairs; If-None-Match ->
  304. Server: server.services.detection_registry exists (app_context.py 106).
- 3.8 Idle policy for node runs (depends 3.4): idle_check_handler.py::IdleCheckHandler (class
  target). check_idle_agents also visits remote runs (new _get_remote_runs: runs on machines in
  the store); check_attention_agents unchanged (local only). _handle_idle_check takes facts:
  PaneFacts {quota, capacity_candidate, status, turn_in_flight, unsubmitted_input,
  queued_message_prompt, tail}; local computed from pane_output as today; remote from store
  (no entry/idle null -> return 0, no attention sync). Hub keeps reset_idle/should_fail/
  should_reprompt counters. Remote transcript reads return (None, None), so recover_completed_turn
  and _recover_capacity_error are unreachable. Writes via hub-mode 2.3; kills via 3.4 runtime.
  Test tests/hub/test_remote_idle_policy.py: reprompt delivered, failed after max reprompts,
  quota fail, context_full fail, composer draft holds.
- hub-mode 2.1 is #24271 (post_terminal_cleanup skips local phases for remote runs); 3.3 after
  #24270 covers it transitively.

Node side changes from this: 2.10 posts 3.5 prompt-delivery-failure and does NOT kill (hub
kills via agent.cancel); 2.14 posts 3.6 terminate and does NOT kill (the hub kills via
agent.cancel inside the route); retry:true -> node re-posts on its next tick; retry:false ->
stop asking for that cause. Rewrite Decision 30 accordingly. 2.10 runs as its own task after
the launched commit (not inside the 300 s prep task) so a cancel takes the launched branch.

Local monitor order (_check_loop, 30 s interval, 5 s start delay): trust, loop, approval,
queued continuation, periodic enters, attention (reuse_for_idle), unhealthy, memory, ...,
init timeout, idle, provider stalls, completed task, stuck, mutexes. Node 2.14 runs: trust,
loop, approval, queued continuation, periodic enters, attention classify, provider stalls,
idle facts, composer read, init-timeout and timeout timing; one 30 s tick after a 5 s delay.
Doom loop locally = LoopTracker threshold 3 -> checkpoint + terminate_managed_runtime_async
fail. Provider stall locally = checkpoint + terminate fail. Checkpoint git plumbing:
checkpoint_manager.py create_checkpoint (status --porcelain, write-tree, add -u, write-tree,
rev-parse HEAD, commit-tree -m "gobby: auto-checkpoint for task {task_id} (run {run_id[:8]})",
update-ref, read-tree restore); task_id must match ^[\w-]+$.
Detection engine: schema.py (engine 1, MAX_BOTTOM_NON_EMPTY_LINES 200, states blocked/idle/
working/stall, region bottom_non_empty_lines(N)), matcher.py (issues invalid_pattern,
pattern_timeout; composer_region, _last_prompt_box, codex composer regexes), safe_regex.py
(MAX_PATTERN_SIZE 2048). Idle detector: COMPOSER_PROBE_LINES 40; detect() is stateless.

## Session 5 (2026-10-14): plan text written through 2.6 (uncommitted, not validated)

Written in node-halves.md this session (sections are spliced from scratch files with
scratchpad/splice.py; that helper is gone after a reset, so use Edit):
- Decision 1 (3.5 on 1.3; 2.10/2.11 on 3.5; 2.14 on 3.6 and 3.7), Decision 4 numbers (3.6,
  3.8), Decision 27 (3.7), Decision 30 REWRITTEN (node observes; hub decides and kills inside
  the route via agent.cancel through 3.4 runtime + hub-mode 2.4 remote_cancel; node never kills
  on a reply; retry:true re-posts next tick, retry:false stops that cause; hub unreachable ->
  re-post each tick, run keeps running), Decision 31 (route 3.6 records the row).
- Constraints: 3.5's routes; edges #24270->3.3, #24273+#24274->3.4, #24276->3.6; monolith
  notes (agent_health 587 and idle_check_handler 580 stay <850; capture.py 649 and
  recovery.py 990 not edited).
- P2 framing: chain 2.1-2.11 plus 2.14; 2.10/2.11 on 3.5; 2.14 on 3.6, 3.7; 2.12/2.13 outside.
- 2.3 rewritten: targets add repair.rs, crates/gdaemon/tests/fixtures/node_isolation/droid_hooks.json,
  tests/agents/test_node_isolation_golden.py; nine items (droid golden Rust + Python, Python
  seed, gmcp .mcp.json + ~/.claude.json under mkdir lock); Granularity line.
- 2.4 rewritten: v2 sandbox; caches.rs (nextest grant, spawn/sandbox cache vars,
  CARGO_TARGET_DIR, project write paths, allow_package_registries), prewarm.rs, settings.rs
  ports render_srt_settings AND ClaudeSandboxResolver.build_settings (provider-native is
  claude-only; others refused "cannot prove the sensitive-root contract"); five items.
- 2.5 rewritten: claude_home.rs (prepare_claude_reviewer_home), ps_shim.rs (#22407),
  provider-native returns claude --settings unwrapped; seven items; Granularity line.
- 2.6 rewritten: (depends: 2.5, 2.12); targets Cargo.toml (toml_edit), provider_command.rs,
  provider_env.rs (native env policy port), trust.rs (claude + codex trust), agy_gate.rs,
  launch corpus crates/gdaemon/tests/fixtures/node_launch/corpus.json +
  tests/agents/test_node_launch_corpus.py; NO timeout timer (2.14 owns timing); nine items.

Still to write: 2.7 (admission = five providers; v must equal CONTRACT_VERSION, replace "models
accept any v of at least 1"), 2.8 (`v: CONTRACT_VERSION`; 2.14 adds pane to reports.rs), 2.9
unchanged, new 2.10-2.14, P3 3.1-3.8 replacing D1-D3, V1 entry (date 2026-10-14), V2 (new test
files + must-statements; drop D3 sentence, cite Decision 20), hub-mode.md edits, validate,
advisor, commit, send.

Decided this session:
- 2.12 owns Cargo.toml for fancy-regex 0.11 and toml; 2.6 adds toml_edit and depends on 2.12.
- 2.14 (depends: 2.8, 2.10, 2.13, 3.6, 3.7) owns run-timeout and init-timeout observation; the
  node measures from RunRecord.accepted_at_ms (no new field). The 3.6 route recomputes age from
  the run's DB started_at; a younger run answers {terminated:false, retry:true}.
- 3.2 must produce `monitor` (hub TmuxConfig values) and `task_id`, one acceptance item each.
- 3.3 store keeps a per-machine revision and ignores a report at or below it (put's rule).
- 3.4 composer gate: local passes empty/held; remote passes only "empty".
- Health facts for 3.6: local timeout reads a 50-line pane; error = reason +
  "\n\n--- Last terminal output ---\n" + pane[-2000:]; `_bootstrap_accounting_stall_error`
  (age_seconds, pane_snapshot) swaps the reason and makes the action fail; provider stall reads
  8 lines; init timeout "initialized" = external_id != id, context_injected, any message/turn/
  tool counter, run turns/tool calls > 0, or last_activity > created.
- services.py: TerminalServices.runtime_for(terminal) = registry.resolve(terminal.backend);
  storage/terminals.py Terminal has machine_id. lib.rs has no indexed symbols (bare path ok).
- Memory 217e0dc7 changes no edge. f5577ae0: hub-mode's stale M1 is withdrawn by hub-mode's own
  writer under its plan task (remove the whole M1 block and its preceding blank line, validate,
  dated changelog entry, adversary re-derives); report that route to #15977.

## Session 6 (2026-10-14): all sections written; validation fixes in progress (uncommitted)

Written in node-halves.md: 2.10-2.14 and P3 (3.1-3.8) spliced in place of D1-D3 (drafts kept in
the scratchpad as p2new.md and p3.md); 2.7 (CONTRACT_VERSION, five providers, 2.7.1), 2.8
(`v: CONTRACT_VERSION`), 2.9 (`v` = CONTRACT_VERSION), Decision 26 (launched run keeps only
2.14's watch record). Fixed: recovery path is src/gobby/agents/watchdog/recovery.py; corpus.json
target now `::*` with scope-reason.

Design choices made this session (keep): 2.10 posts once and only logs on failure (Python parity;
2.14 init timeout covers it); a cancel record stops delivery; 2.11 runs in the prep task between
prepare and launch, refused post = prepare_failed; 2.14 adds RunRecord.watch {provider,
session_id, task_id, timeout_seconds, monitor} committed by launch.rs, a node-local automatic-write
latch (port of _blocked_automatic, lifted by a successful hub terminal.write), monitor pass runs on
2.8's node-agent-report tick (serve.rs target); 3.4 terminate = remote_cancel TERM,
close_terminal=True, KILL after grace on not_stopped; 3.6 is_recovery_protected applies to ALL
runs, cooperative close / closed bound task only to non-interactive; init_timeout with missing
session -> retry:true; 3.1 has 5 items incl. two `file: docs/contracts/node-commands.md` items.

Validator state (last run): 1 error fixed after the run (corpus.json scope). Warnings to resolve
(consumer-coverage; add Targets or "Consumers unchanged" no-edit-reasons; see `rtk recall
6f7e4908bc01` for full text):
- 3.2 `_agent_prompt_prefix` consumers (tests/a...)
- 3.3 `AgentAttentionTracker` consumers (tests/agents/...)
- 3.4 `composer_refuses_automation` consumers (src/...)
- 3.5 `register_routes` consumers (src/gobby/servers/app_fa...)
- 3.8 `IdleCheckHandler` consumers (src/gobby/agents/...)
Run `uv run gobby plans validate .gobby/plans/node-halves.md -p .` bare (no pipes) to list them.

Remaining after validation: V1 entry (2026-10-14, describe the no-deferrals revision and ITEM D);
V2 (add pytest files: tests/agents/test_node_launch_corpus.py, tests/agents/test_node_isolation_golden.py,
tests/agents/test_pane_detection_corpus.py, tests/hub/test_node_command_corpus.py,
tests/hub/test_spawn_payload.py, tests/hub/test_remote_pane.py, tests/hub/test_remote_runtime.py,
tests/hub/test_remote_idle_policy.py, tests/servers/test_node_run_routes.py; cargo binaries
pane_detection_corpus, node_command_corpus and gobby-terminals control_snapshot; drop "D3's live
proof runs in hub-mode D4" -> cite Decision 20; keep must-statements); hub-mode.md edits (list
above under "hub-mode.md edits"); validate both; advisor; commit (3 calls); sends.

## Session 7 (2026-10-13 CT): validation clean, resume gap closed, ready to commit

- Consumer-coverage warnings fixed (3.2, 3.3, 3.4, 3.5, 3.8). 3.8 keeps `_handle_idle_check`'s
  signature. 3.2 research corrected: `prepare_codex_spawn` prepends the prefix;
  `_spawn_codex_terminal` merges `_agent_context_injected`.
- V1 entry (2026-10-13) and V2 (all pytest files, pane_detection_corpus, node_command_corpus,
  gobby-terminals control_snapshot, test-types audit, Decision 20 citation) written.
- Remote resume gap (advisor-confirmed): payload `workspace.mode "existing"` + `path` (3.1);
  builder `resumed_workspace(db, run.machine_id, run.child_session_id)` reads the session's active
  worktrees/clones row (3.2.8); node uses it in place, `WorkspaceRecord.reused`, 2.1 `to_removals`
  drops reused records (2.1.5, 2.3.10, 2.7.4/2.7.6).
- hub-mode.md: Decision 13, code list, 2.5 (resume routing in callers, resolve_resume_target,
  prepare_terminal_resume target with routed_machine_id, build_spawn_payload, agy gate local only,
  rollback keeps session), 2.5.7 rewritten (test_resume_routes_and_placement_refused), V1 entry.
  hub-mode M1 left untouched (stale; f5577ae0 route).
- Dates: local clock is 2026-10-13 CT; Decision 4 and the rulings header corrected from 10-14.
