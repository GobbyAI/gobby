# Review: hooks

- **Scope:** `src/gobby/hooks/` — core (`hook_manager.py`, `rule_evaluator.py`, `factory.py`),
  event models + cross-CLI normalization (`events.py`, `hook_types/`, `normalization.py`,
  `_normalization_*.py`), session layer (`session_coordinator.py`, `session_lookup.py`,
  `session_activation.py`, `session_types.py`, `session_ref_resolution.py`), event handlers
  (`event_handlers/` incl. `_session_start/`, `_session_end.py`, `_agent.py`, `_tool.py`,
  `_dispatch.py`, `_misc.py`, `_plan.py`, `_session_responses.py`), broadcast/dispatch
  (`broadcaster.py`, `dispatchers/`, `webhooks.py`, `mcp_dispatch.py`, `inbox.py`,
  `session_summary_dispatcher.py`), and verification/health/misc (`verification_runner.py`,
  `health_monitor.py`, `health_gate.py`, `git.py`, `code_navigation.py`, `project_context.py`,
  `event_enrichment.py`, `skill_manager.py`). Plus the END-TO-END DELIVERY SEAM: the `ghook`
  binary (sibling repo `gobby-cli/crates/ghook/`, read cross-repo), `servers/routes/mcp/hooks.py`
  (receiving route), and `cli/installers/` hook registration. **Split boundary:** adapters/
  internals belong to #15781; workflows/hooks.py (rule bridge) was reviewed in #15775.
- **Reviewer:** Claude Fable 5 — 7-agent parallel fan-out, all Blockers synthesizer-verified
  link-by-link against source.
- **Commit / branch:** `0.5.0` @ HEAD `be3035496` (working tree clean at review time).
- **Summary:** 6 Blocker · 38 Important · 22 Nit — the enforcement perimeter has holes at
  every layer above the rule engine: a computed rule deny is discarded by the web-chat
  hold-open path, the health gate fails STOP open, normalization misses the nested
  `call_tool` form, Grok enforcement is inert end-to-end, and failed activation
  permanently downgrades a spawned agent to default rules while writing success markers.
  Fail-open is the ambient default, decided independently (and inconsistently) at six+
  seams.

## Findings

### [BLOCKER] Web-chat hold-open path discards a computed rule deny and returns approve
- **Where:** `servers/routes/mcp/hooks.py:571-594` — `result = await _run_adapter_hook(...)` (full pipeline incl. rule evaluation) is computed first; then for any request with `X-Gobby-Session-Id` + a hold-open hook type, `_maybe_hold_open` (`:292-452`) runs **without ever inspecting `result`**, and `:593-594` returns its output unconditionally.
- **Failure mode:** For a `web_chat` session's PreToolUse: (a) if the tool matches session/project/global auto-approval (`is_tool_auto_allowed`, `:400-407`), the route returns `{"decision": "approve"}`, silently discarding the rule deny with no user involvement; (b) otherwise a browser approve/approve-always click returns approve — the user is never shown that a Gobby rule denied the call, and "approve always" makes the bypass permanent. Found independently by two reviewers.
- **Why it matters:** The same rule that blocks a terminal session is silently overridable (or auto-overridden) on the web-chat surface — declarative enforcement is not surface-uniform. `tests/servers/routes/test_hold_open_gate.py` has no rule-deny-precedence case.
- **Minimal fix:** Short-circuit before hold-open when `result` already encodes a deny (`permissionDecision == "deny"`, top-level `decision == "block"`, or `continue is False`); at minimum surface the rule denial in the pending-interaction payload as non-overridable.
- **Confidence:** high (mechanism); med-high (exposure — web_chat sessions only).

### [BLOCKER] Health gate fails STOP open when the daemon reports unhealthy — preempting every stop-gate rule
- **Where:** `hooks/health_gate.py:13-18` (`CRITICAL_HOOKS` includes `STOP` and `AFTER_AGENT`), `:46-49,57-60` (`_unavailable_response` returns `decision="allow"` in both the planned-restart and unplanned branches), `:80-101` (3 retries ≈3.5s, then the allow); wired before any rule evaluation at `hook_manager.py:252-270`.
- **Failure mode:** The daemon HTTP server is up but `/status` reports unready (Postgres restart, pool exhaustion, startup grace) → STOP gets `allow` before `_evaluate_workflow_rules` runs → `require-task-close`/`require-epic-tree-close` and completion-readiness never evaluate → an agent with a claimed task stops cleanly. The downstream fail-closed protection for exactly this class (`workflows/hooks.py:528-540` blocks STOP when variables can't load, "blocking for safety") is preempted and unreachable. The route-level fail-safe covers only *timeouts*.
- **Why it matters:** Same inconsistency class as workflows-rules.md Blocker 6 (and graded by the same precedent): the codebase deliberately fail-closes STOP in three places (variable-load, cancellation, route timeout) and fails it open here. A degraded-DB window silently disables the primary enforcement gate for every active session. `tests/hooks/test_health_gate.py` constructs only BEFORE_TOOL events — STOP posture is unpinned.
- **Minimal fix:** In `_unavailable_response`, return `decision="block"` for STOP/AFTER_AGENT in the unplanned branch (keep allow for planned restarts), matching the timeout posture.
- **Confidence:** high (behavior); med (trigger frequency).

### [BLOCKER] `normalize_mcp_fields` misses the nested `call_tool` routing form — `mcp_tool`-keyed rules silently don't fire on calls the proxy executes
- **Where:** `hooks/_normalization_mcp.py:128-140` — step 1b reads `tool_input.get("server_name")`/`tool_input.get("tool_name")` at the **top level only**; the proxy's canonicalizer (`mcp_proxy/_call_tool_wrapper.py:115-118`, verified in the #15775 review) also hoists routing from nested `arguments.server_name`/`arguments.tool_name` when top-level keys are absent.
- **Failure mode:** An agent calls `call_tool` with `tool_input = {"arguments": {"server_name": "gobby-tasks", "tool_name": "escalate_task", "arguments": {...}}}` (no top-level routing keys — a form the proxy supports and executes). Normalization's override branch is skipped, leaving the prefix-parsed wrapper values `mcp_server="gobby"`, `mcp_tool="call_tool"`. Every rule keyed on the real target never fires: `reviewer-lifecycle/terminal-verdict-after-validation.yaml:48-66` (`approve_review`/`reject_review`/`escalate_task` gating), `context-handoff/auto-compact-after-task-close.yaml:12-13`, `memory-lifecycle/*` `mcp_tools:` selectors. The proxy executes the real tool; the rule layer sees `gobby/call_tool` and allows.
- **Why it matters:** Same class as workflows-rules.md Blocker 1 (decoy routing keys): the enforcement layer and the execution layer disagree about the call target. `tests/hooks/test_normalization.py` covers top-level extraction thoroughly and has zero nested-arguments cases.
- **Minimal fix:** When top-level keys are absent, also read `tool_input["arguments"]`/`["args"]` (mirroring `CALL_TOOL_ARGUMENT_FIELDS` precedence); ideally share the proxy's canonicalizer so the two cannot drift.
- **Confidence:** high on divergence (both sides verified); med on in-the-wild frequency of the nested form.

### [BLOCKER] Failed agent activation permanently locks in default identity — spawned agent runs without its rule set and tool blocks, with success markers written
- **Where:** `hooks/session_activation.py:166-199` (reconcile flow), `:259-273` (`_fallback_agent_updates` — defaults `_agent_type="default"`, empty block lists, `_active_rule_names=None`), `:511-531` (`_activate_agent` returns False/None on transient DB error or missing agent definition), `:238-246` (`_marker_updates` writes `_session_activation_completed=True` in the same merged write).
- **Failure mode:** On the first event where `_AGENT_KEYS` are missing, if activation fails for any reason, the fallback fills default identity AND the completion markers in one write. Every subsequent reconcile sees no missing keys and valid markers — never repairs. Downstream, `engine/enforcement.py:159-162` short-circuits agent tool enforcement on empty block lists and `engine/core.py:867-872` filters rules by the *default* agent's selectors — the spawned agent meant to run with blocked tools and an agent-specific rule set runs unrestricted for its entire lifetime, with a one-time debug log. The sibling `_fallback_agent_updates` flip of `is_spawned_agent` True→False (`:270-272`) compounds it by disabling step-workflow repair and flipping audience-gated rules (see Important below).
- **Why it matters:** One transient fault converts "missing state, repairable" into "wrong state, marked complete" — enforcement silently bypassed with success markers. Textbook success-while-contract-violated.
- **Minimal fix:** Don't write markers (or default identity) when `activation_missing` was non-empty and activation failed; leave keys absent so the next event retries; log at WARNING with the agent name.
- **Confidence:** high (mechanism); med (trigger frequency).

### [BLOCKER] Gemini/Qwen transcript fallback binds another session's transcript to the new session
- **Where:** `hooks/event_handlers/_session_start/transcripts.py:105-115` — prefix glob `session-*-{prefix}.json` misses (CLI hasn't written its chat file yet at SessionStart) → fallback `sorted(chats_dir.glob("session-*.json"), reverse=True)[0]` returns the newest **previous** session's transcript; registered on the new session row and with the message processor (`flow.py:317,397-403`), never re-derived.
- **Failure mode:** The processor and lifecycle ingest the old transcript's messages, token stats, and summary content under the new session — another session's conversation becomes this session's messages, handoff summaries, and archived transcript. By construction the fallback only fires when the session's own file doesn't exist, so when it returns a path it is essentially always the wrong session's. The canonical implementation (`sessions/transcript_paths.py:66-78`) deliberately has no most-recent fallback — in-repo contract drift; a test enshrines the wrong behavior (`tests/hooks/test_transcript_path_derivation.py:69`).
- **Why it matters:** Cross-session data attribution violating the session boundary — wrong context injected into summaries and handoffs.
- **Minimal fix:** Delete the most-recent fallback (return None, matching `transcript_paths.py`); re-derive the path on a later hook once the real file exists.
- **Confidence:** high (logic); med (frequency — depends on when Gemini/Qwen write the chat file).

### [BLOCKER] Grok hook traffic is mis-sourced as "claude" by the installed ghook — all Grok enforcement is silently inert
- **Where:** `cli/installers/grok.py:116-125` + `install/grok/hooks-template.json` write `ghook --gobby-owned --cli=grok --type=<snake_case>`; the ghook binary (sibling repo `gobby-cli/crates/ghook/src/cli_config.rs:21-56`) has **no grok arm** and `for_dispatch` falls back to the **claude** config; nothing sets `GOBBY_SOURCE` on the install path; envelope arrives `source: "claude"` → route selects `ClaudeCodeAdapter` (`servers/routes/mcp/hooks.py:540`) instead of the existing `GrokAdapter` (`:548-549`); grok's snake_case hook types are unknown to the Claude contract (`adapters/claude_contract.py:225-240`) → every event degrades to `NOTIFICATION` (the contract's explicit fail-open fallback, `claude_code.py:158-161`).
- **Failure mode:** No PreToolUse gating, no stop gate, no session registration — 100% of the time, not just degraded states. The claude-fallback critical set (kebab-case `session-start`/…) also never matches grok's snake_case types, so every grok hook additionally fails open on daemon-down. Droid had this exact hazard and got both an upstream ghook route and an installer version gate (`cli/installers/droid.py:212-235`); grok got neither. Installed ghook 0.4.6 matches workspace source — no version skew masking this.
- **Why it matters:** A daemon-supported CLI with a dedicated adapter and installer ships with enforcement off.
- **Minimal fix:** Add a grok arm to ghook's `CliConfig` (cross-repo) and bump the floor in `install/version_pins.py:8`; until that ships, gate `install_grok` with a version check mirroring droid's. Add a conformance test asserting every `--cli=` value emitted by installers is a recognized ghook route.
- **Confidence:** high (all links verified; ghook side read cross-repo with binary version confirmed).

### [IMPORTANT] Route exception handling fails STOP open while timeout fails it closed
- **Where:** `servers/routes/mcp/hooks.py:617-664` (ValueError/Exception/outer → `_graceful_error_response` = allow) vs `:216-244,626-641` (timeout → block "for safety"); unguarded pre-rule steps `hook_manager.py:287-313` (project resolution, session lookup, ref resolution) raise through to the route.
- **Failure mode:** The same Stop hook that blocks when it hangs sails through with allow + context when it raises (transient DB error at `:306`). The fail-safe contract holds against hangs, not raises. No test references `FAIL_SAFE_HOOK_TYPES` at all. Filed by two reviewers independently.
- **Minimal fix:** Apply the timeout posture (block) in the exception handlers when `hook_type` is fail-safe; add tests for both failure modes on Stop.

### [IMPORTANT] Daemon-down: the Stop gate fails open on every CLI except Codex, undocumented
- **Where:** ghook `cli_config.rs:21-52` — claude critical set = `{session-start, session-end, pre-compact}` (no `stop`); gemini/qwen = `{SessionStart}`; droid = `{}`; only codex includes `Stop`. Non-critical failures exit 1 → CLI proceeds (`main.rs:422-482`).
- **Failure mode:** Daemon stopped/crashed (unplanned — the planned-shutdown marker correctly suppresses only fresh intents): Claude/Gemini/Qwen/Droid agents stop freely with claimed tasks. Documented only in a completed plan file and Rust unit tests, never operator-facing. The cross-CLI asymmetry (codex closed, others open) reads accidental.
- **Minimal fix:** Document per-CLI daemon-down posture in `docs/guides/hook-schemas.md`; deliberately decide whether claude/gemini Stop join codex's fail-closed set.

### [IMPORTANT] No daemon-side execution bound for non-Stop hooks; ghook gives up at 30s and the CLI proceeds — and Gemini's stop equivalent has no fail-safe at all
- **Where:** `servers/routes/mcp/hooks.py:46` (`FAIL_SAFE_HOOK_TYPES = {"stop"}` — `afteragent` absent), `:206-213` (non-fail-safe → no timeout), ghook `transport.rs:23` (30s POST timeout, then fail-open for non-critical).
- **Failure mode:** A >30s rule/webhook/DB stall on PreToolUse → tool executes; any later-computed deny is read by nobody. Gemini/Qwen's `AfterAgent` (their turn-end gate) is not fail-safe, so their stop gate is unbounded daemon-side and resolves by ghook timeout → allow — unlike claude/codex Stop which gets the 20s block.
- **Minimal fix:** Add `afteragent` to `FAIL_SAFE_HOOK_TYPES`; bound non-fail-safe hooks comfortably below 30s so the daemon decides degraded behavior, not the transport.

### [IMPORTANT] `mcp_call` success detection inverted vs the proxy result contract — `block_on_success` can never fire; `block_on_failure` blocks on success
- **Where:** `hooks/dispatchers/mcp.py:480` (`result.get("success", False)`) vs the proxy contract: `normalize_internal_success_result` strips `success: True` from successful internal results (`mcp_proxy/tools/internal.py:76-87`), and external servers return non-dict `CallToolResult` objects. The inline dispatcher (`factory.py:381-382`) uses the correct `get("success", True)` — two paths, opposite semantics; a third (`mcp_dispatch.py`) captures nothing.
- **Failure mode:** Every successful call on a non-`_proxy` server is judged failed: `block_on_failure` rules deterministically false-block; `block_on_success` interceptors silently allow. Latent (no bundled rule uses `block_on_*` today) but it is a documented rule feature, and `tests/hooks/test_auto_heal_dispatch.py` mocks the proxy with `{"success": True}` shapes production never returns. Found by two reviewers.
- **Minimal fix:** `failed = result is None or (isinstance(result, dict) and result.get("success") is False)`; consolidate the three dispatchers.

### [IMPORTANT] Shared `httpx.AsyncClient` reused across event loops — blocking (`can_block`) webhooks degrade to permanent silent allow
- **Where:** `hooks/webhooks.py:62-86` (client + `asyncio.Lock` cached once), `dispatchers/webhook.py:118-136` (blocking path runs `asyncio.run` per event from `to_thread` workers — a fresh loop each time), `:176-186` (non-blocking path uses the same client on the daemon loop).
- **Failure mode:** The client's pool binds to the first ephemeral loop, which then closes; subsequent use raises cross-loop errors, caught by the retry loop → `success=False, decision=None` → `get_blocking_decision` allows (`webhooks.py:320-339`); `evaluate_blocking_webhooks` additionally swallows everything as "fail-open for webhook errors" (`webhook.py:76-80`). A configured `can_block` endpoint's deny becomes allow after first use. Latent until an operator configures a blocking endpoint — then it breaks reliably.
- **Minimal fix:** Per-call client on the blocking path (or one client pinned to the daemon loop via `run_coroutine_threadsafe`); add a `fail_closed` option per endpoint (today there is no way to make a policy webhook fail closed).

### [IMPORTANT] Web-chat lifecycle dispatch drops `inject_result`/`block_on_*` and the `query` mapping
- **Where:** `hooks/mcp_dispatch.py:31-82` (no capture flags, no `query` injection, returns None) vs `dispatchers/mcp.py:396-398,480+`; consumer `servers/websocket/chat/_lifecycle.py:220-222,368-390` (also bypasses `strip_unknown`/schema validation by calling `registry.call` directly).
- **Failure mode:** Rules whose blocking depends on an `mcp_call` gate enforce on the CLI path and no-op on web-chat; memory/skill recall (`inject_result`) injects nothing; `search_memories`-style tools get no `query` and fail with a warning. The comments claim "parity with CLI path".
- **Minimal fix:** Port the capture/return contract; route internal calls through `proxy.call_tool(..., strip_unknown=True, enforce_workflow=False)`.

### [IMPORTANT] Failed shell commands can be recorded as successful validation evidence (close-gate poisoning)
- **Where:** `workflows/observer_utils.py:60-74` (`_shell_tool_succeeded` defaults **True** when no signal), `hooks/_normalization_tools.py:115-140` (`_detect_tool_error` inspects only *string* output for the literal "exit code N" pattern; dict outputs skipped), `observer_verification.py:55-85` (records `success=True` → `verification_evidence_recorded=True`), consumed by the close gate (`_lifecycle_close.py:216-223`).
- **Failure mode:** Claude Code's `tool_response` is a dict with no exit-code key; mainline Claude is rescued only by the separate `post-tool-use-failure` event setting `is_failure`. Any path without a failure-flavored hook (SDK/web-chat dict responses, providers without the event) records a failed `pytest`/`ruff` run as passing evidence — satisfying Gate 1 with a failing build.
- **Minimal fix:** Make success three-valued; unknown ⇒ record with `success: null` and do NOT set `verification_evidence_recorded`.

### [IMPORTANT] Validation-command classification: `pytest --collect-only`/`--version` count as a successful test run; a passing lint clears a failed test run
- **Where:** `config/validation_detection.py:182-217` (pytest prefix matcher; only mutating args excluded at `:20`); `workflows/condition_helpers.py:163-183` (any later `success=True` validation clears `failed_validation_unresolved`, category-blind).
- **Failure mode:** The close gate's evidence requirement is satisfiable without executing a single test, accidentally or deliberately; "pytest fails → ruff passes" unlocks the gate with known-failing tests.
- **Minimal fix:** Add non-executing flags to `forbidden_args_any` (mechanism exists); make failed evidence clearable only by same-category success.

### [IMPORTANT] `gobby hooks run <stage>` exits 0 when configured command names don't resolve
- **Where:** `hooks/verification_runner.py:229-242` (undefined command → `success=True, skipped=True`), `:215-221` (missing verification block → whole-stage skip), `cli/extensions.py:218-221,252-254` (exit 0).
- **Failure mode:** A typo'd command name (`"run": ["unit_tets"]`) runs nothing and the pre-commit/pre-push gate passes. Misconfiguration indistinguishable from success.
- **Minimal fix:** Treat unresolvable configured names as failures; exit non-zero when a stage with configured entries executed zero commands.

### [IMPORTANT] Daemon-global skill manager and pipeline-execution manager pinned to the personal project, cached forever
- **Where:** `hooks/factory.py:481-507` (`resolve_project_id(None, None)` → `PERSONAL_PROJECT_ID`; one `HookSkillManager` + one `LocalPipelineExecutionManager` for the whole daemon); `hooks/skill_manager.py:117-182` (cache never expires; transient-DB-error fallback to bundled filesystem set is itself cached permanently, ignoring user/project skills and `enabled` flags; `refresh()` has zero callers).
- **Failure mode:** Project-installed skills never surface in hook skill resolution in any project; personal-project skills leak into every project; rule/hook-triggered pipeline executions are filed under the personal project (invisible to project-scoped queries). Same wrong-scope class as the agents review's scattered TmuxConfig.
- **Minimal fix:** Resolve project per event; TTL/keyed cache; never cache the degraded fallback; wire `refresh()` to skill mutations.

### [IMPORTANT] Rule/webhook blocks skip broadcasting, enrichment, and non-blocking webhooks — blocked events invisible to observers
- **Where:** `hook_manager.py:344-362` (early returns) vs `:392-402` (broadcast/webhooks on fall-through only).
- **Failure mode:** WebSocket clients and webhook integrations never see blocked tool calls or blocked stops — the highest-signal events; external automation sees a falsified stream of allowed-only events.
- **Minimal fix:** Route block responses through the common broadcast tail before returning.

### [IMPORTANT] Abandoned `to_thread` workers + no-timeout rule evaluation can exhaust the shared executor
- **Where:** `servers/routes/mcp/hooks.py:247-260` (`wait_for` over `to_thread` — worker not cancelled on timeout), `factory.py:521-524` + `config/tasks.py:595-598` (workflow timeout default 0.0 → None) → `workflows/hooks.py:103,718` (`future.result(timeout=None)`).
- **Failure mode:** Each hung rule-evaluation coroutine pins a default-executor thread forever; saturation (~32 threads) stalls every `asyncio.to_thread` user → all hook requests hang → CLI-side timeouts → all gates fail open at the CLI.
- **Minimal fix:** Finite default workflow timeout; bound the worker wait independently of the route's `wait_for`.

### [IMPORTANT] Bundled build-coordinator rule is permanently dead — `canonical_tool_kind == 'execute'` is never emitted
- **Where:** `_normalization_canonical.py:47-67,102-221` (kinds emitted: read/write/search/mcp only) vs `install/shared/workflows/rules/build-coordinator/require-build-coordinator-for-gobby-build.yaml:16` (requires `'execute'` as an and-conjunct).
- **Failure mode:** The `gobby build` skill gate never blocks; `is_gobby_build_command` is registered and working but gated behind an impossible condition.
- **Minimal fix:** Drop the conjunct or emit an `execute` kind for shell commands.

### [IMPORTANT] Canonical write detection evaded by chained, glued-redirection, and heredoc commands — write-gates fail open
- **Where:** `_normalization_canonical.py:102-117` (bails to `{}` on any chain/heredoc token), `_normalization_shell.py:120-129` (`_extract_redirection_paths` matches only exact `>`/`>>` tokens — `>>file.py` glued is missed and then misclassified as a *read* of `">>file.py"`).
- **Failure mode:** `true && printf 'x' > module.py`, `cat > module.py <<EOF`, `printf x >>module.py` produce no write classification → `require-claimed-task-required-skills` and the `edit_write_pending` commit-before-stop accounting never trip. (The worker-safety block rules are immune — they regex the raw command string.)
- **Minimal fix:** Evaluate each chain segment; treat unparseable commands containing redirection glyphs as writes.

### [IMPORTANT] `complete_agent_run`'s stats flush is dead in production — successful short runs marked failed ("no activity")
- **Where:** `hooks/session_coordinator.py:483-501` — `asyncio.get_event_loop()` in a `to_thread` worker on 3.13 raises → broad except skips the flush silently; even with a loop, `ensure_future` isn't awaited before the re-fetch at `:499`; `:507-537` then fails runs reading 0/0 counts; `result` computed pre-flush at `:408-412`.
- **Failure mode:** Genuinely successful agent runs recorded as `error` with "no activity"; longer runs persist undercounted stats. Wrong outcomes feed the completion registry and task recovery.
- **Minimal fix:** `run_coroutine_threadsafe(flush, self._event_loop).result(timeout=5)` (the loop is already captured at `:128-135`); recompute `result` after the refresh.

### [IMPORTANT] `complete_agent_run` ignores `complete()`/`fail()` return values — notifies "success" for a run that lost the terminalization race
- **Where:** `session_coordinator.py:513-552` (read-then-act status check; unconditional notify) vs the guarded transitions (`storage/agents/_lifecycle.py:188-267`) and first-write-wins registry dedup.
- **Failure mode:** If the lifecycle monitor terminalizes between check and UPDATE, `complete()` returns None but subscribers are told `success` while the DB row says error/cancelled. `terminalize_successful_run` handles the None correctly — drift.
- **Minimal fix:** On None, re-fetch and notify the stored status.

### [IMPORTANT] ACP-child guard exists only on SESSION_START — non-start events auto-register stray duplicate sessions
- **Where:** `session_lookup.py:266-301` (auto-registration with `parent_session_id=None`); the `gobby_acp_child` skip exists only in `_session_start/flow.py:164-173`; web-chat external-id binding is late and fail-open (`websocket/chat/_session.py:767-790`).
- **Failure mode:** ACP children's non-start hooks miss the cache and auto-register a stray `terminal`-type row; usage/rules/messages attach to the phantom while the UI reads the web_chat row; post-binding lookups are nondeterministic (`fetchone` with no ORDER BY).
- **Minimal fix:** Apply the ACP-child skip in `SessionLookupService` before auto-registration.

### [IMPORTANT] `_fallback_agent_updates` force-flips `is_spawned_agent` True→False when run recovery lags
- **Where:** `session_activation.py:270-272,464-469,534-543,619-620`.
- **Failure mode:** A truthful `is_spawned_agent=True` variable is overwritten to False when the session row lacks `agent_run_id`/`agent_depth` (pickup metadata not yet backfilled) — step-workflow repair skipped, audience-gated rules flip to interactive. Part of the activation-Blocker family; distinct mechanism.
- **Minimal fix:** Never overwrite True→False unless the run lookup affirmatively shows no agent run.

### [IMPORTANT] Global session-lookup lock held across DB queries and git subprocess — serializes all sessions' hook handling
- **Where:** `session_coordinator.py:229-236` (one process-wide lock), `session_lookup.py:213-296` (held across lookup + `register_session`, which shells out to `get_git_branch`).
- **Failure mode:** Under a slow git/DB call, every concurrent hook event for any session queues; CLI-side hook timeouts fire and those events proceed fail-open — rules unevaluated for unrelated sessions.
- **Minimal fix:** Per-`(external_id, source)` locks; registration is already idempotent under the DB advisory lock.

### [IMPORTANT] `find_parent` checks only the single newest handoff candidate — concurrent terminals silently lose their handoff
- **Where:** `_session_start/handoff.py:38-70` (single candidate + terminal-context check applied after; backoff loop breaks on first found parent); query `storage/sessions/_discovery.py:226-240` (`LIMIT 1`).
- **Failure mode:** Two terminals `/clear` near-simultaneously → both children fetch the same newest parent → the non-matching child downgrades to `startup`, losing summary + task-claim carryover; sessions without terminal context always lose. The guard correctly prevents *theft* — the failure mode is loss.
- **Minimal fix:** Fetch N candidates, pick the first that matches, keep polling past wrong-terminal candidates.

### [IMPORTANT] One DB error in unwrapped session-start steps aborts all boot context including rule-based injection
- **Where:** `_session_start/flow.py:409` (`populate_handoff_session_variables` unwrapped; internals raw DB), `_session_responses.py:115-117` (reconcile writes outside try, contradicting its own docstring), `flow.py:518-522`.
- **Failure mode:** A raise propagates to `hook_manager.py:331-336` → allow returned **before** SESSION_START rule evaluation → no banner, no claimed-task block, no handoff/profile rules. Most steps are isolated; these seams aren't.
- **Minimal fix:** Wrap with the adjacent narrow except+warning pattern.

### [IMPORTANT] `session_end` unregister falls back to external_id, which can never match the processor's key
- **Where:** `_session_end.py:96-101` vs registration always under platform session_id (`flow.py:397-403,609-616`). A test pins the wrong contract (`test_session_end_handlers.py:166`).
- **Failure mode:** Unresolved session ends leave the processor polling the transcript forever — unbounded `_active_sessions` growth.
- **Minimal fix:** Drop the fallback or map external→platform id first; fix the test.

### [IMPORTANT] Pre-created sessions (web-chat) never seed `user_profile_content`
- **Where:** `flow.py:499-668` (pre-created path lacks the `seed_user_profile_content` call; the only call site is the fresh path at `:377`).
- **Failure mode:** USER.md profile silently absent from all web-chat sessions; the `inject-user-profile` rule renders nothing. Same pre-created/fresh drift family: `skip_default_agent_activation` also ignored on the pre-created path (`:592-606`).
- **Minimal fix:** Call the seeder (and honor the skip flag) on the pre-created path.

### [IMPORTANT] Stage-pipeline terminal handler releases the dispatch mutex before mutating stage state
- **Where:** `event_handlers/_dispatch.py:183-214` (read stage → release mutex → transition based on pre-release snapshot); the sibling `on_expansion_run_completed` (`:63-72`) does it correctly (side effect in try, release in finally).
- **Failure mode:** The heartbeat dispatcher can acquire the freed mutex and dispatch against the same `in_progress` stage; the hook then transitions it underneath the new run. Violates acquire-before-side-effects; drift, not design.
- **Minimal fix:** Release in `finally` after the transition.

### [IMPORTANT] `is_subagent` is a boolean toggled per subagent event — parallel subagents flip enforcement mid-flight
- **Where:** `event_handlers/_agent.py:505,527`.
- **Failure mode:** Start(A), Start(B), Stop(A) → `is_subagent=False` while B runs: native task tools re-blocked for B, gobby-tasks subagent policy lifted. Stale-True from a crashed subagent persists until the next turn.
- **Minimal fix:** Atomic `subagent_count` RMW; derive the boolean; clamp at turn_start.

### [IMPORTANT] Subagent depth tracking is dead and broken: wrong column queried, result never consumed, dict never pruned
- **Where:** `_agent.py:477-498` — query keys `external_id` with a value that is `sessions.id`; always 0 matches, hidden by a debug-level except; `_pending_subagent_depths` has zero readers repo-wide and entries are never removed.
- **Failure mode:** The documented depth-marking contract is a silent no-op plus a slow leak.
- **Minimal fix:** Delete the block or fix the column, add the consumer, prune on SUBAGENT_STOP.

### [IMPORTANT] `NotebookEdit` escapes edit tracking — commit gates blind to notebook-only changes
- **Where:** `event_handlers/_tool.py:14-21` (`EDIT_TOOLS` lacks `"notebookedit"`; the canonical write set knows it, `_normalization_canonical.py:34-44`).
- **Failure mode:** No `session_edited_files` append → `has_dirty_files` scoping, `require-commit-before-status`, and completion-readiness never see notebook edits — a notebook-only task can close with uncommitted changes.
- **Minimal fix:** Use the canonical write-tool set instead of the drifted local list.

### [IMPORTANT] Gemini/Qwen session-usage broadcast can never fire in production
- **Where:** `event_handlers/_misc.py:136-143` — `asyncio.get_running_loop()` from a `to_thread` worker always raises; swallowed at DEBUG.
- **Failure mode:** DB updates succeed; the WebSocket usage broadcast never happens — dead code that looks functional. The correct pattern exists in `session_summary_dispatcher.py:75-83`.
- **Minimal fix:** `run_coroutine_threadsafe` on the captured daemon loop.

### [IMPORTANT] `on_epic_terminal` catches too narrowly — a failed plan archive poisons an already-committed task close
- **Where:** `event_handlers/_plan.py:25-32` (catches only FileNotFoundError/PlanNotFoundError; `archive_plan` can raise PermissionError/OSError/DB errors); call site `_lifecycle_close.py:380-391` runs unguarded **after** the close committed.
- **Failure mode:** The agent sees the close fail (it succeeded); parent notification, session close-linking, and claimed-task cleanup are skipped; retries hit "already closed".
- **Minimal fix:** Catch Exception at the hook (archive is best-effort by intent).

### [IMPORTANT] Inter-session messages marked delivered before delivery is assured
- **Where:** `event_enrichment.py:186-194` (`mark_delivered` before formatting/attachment; failures `except: pass`), outer except logs at debug (`:166-170`).
- **Failure mode:** Any exception after marking permanently loses P2P/web-chat/command-result messages (looks like an unresponsive peer); a failed mark causes duplicates.
- **Minimal fix:** Attach first, then mark; log mark failures at warning.

### [IMPORTANT] Non-critical hooks gate on up-to-10s-stale cached health with no fresh check
- **Where:** `health_gate.py:78-101` (retries only for critical hooks; immediate allow otherwise), `health_monitor.py:33-58` (10s poll; cache starts not-ready).
- **Failure mode:** One failed loopback poll disables all BEFORE_TOOL enforcement for up to 10s with no fresh check; logs report the pre-retry status.
- **Minimal fix:** One `check_now()` for non-critical hooks before allowing.

### [IMPORTANT] `GOBBY_PROJECT_ID` env preempts event-cwd project resolution for session-less hooks
- **Where:** `hooks/project_context.py:140-154`, `utils/project_context.py:100-114`.
- **Failure mode:** A daemon (re)started from a web-chat subprocess environment attributes every session-less hook to that one project regardless of `event.cwd` until restart.
- **Minimal fix:** Prefer explicit cwd resolution over process-env when both exist.

### [IMPORTANT] Hook inbox replay: at-least-once with no envelope identity, no staleness bound, poison retry forever, reordering across failures
- **Where:** ghook `transport.rs:129-193` (file deleted only on 2xx); `hooks/inbox.py:88-178` (no envelope ID → no dedupe; `enqueued_at` carried but never used; 4xx/5xx envelopes retried every 60s forever; loop continues past failures so later envelopes deliver before stuck earlier ones).
- **Failure mode:** A ghook 30s timeout with late daemon success replays the same side-effecting hook; days-old `session-end`/`stop` envelopes mutate moved-on sessions; one poison envelope spams forever.
- **Minimal fix:** Envelope UUID + daemon-side recent-ID dedupe; TTL on replay; quarantine on 4xx/N consecutive failures.

### [IMPORTANT] `install_claude`/`install_gemini`/`install_qwen` overwrite the user's existing hook arrays per event type
- **Where:** `cli/installers/claude.py:270-274`, `gemini.py:138-142` (assignment replaces; comment claims preservation); codex does it correctly (strip Gobby handlers, append).
- **Failure mode:** User-authored hooks for 26 event types silently dropped on every install; only a timestamped backup, no notice.
- **Minimal fix:** Port the codex merge semantics.

### [IMPORTANT] No runtime ghook↔daemon schema-skew detection — a skew fails open fleet-wide
- **Where:** route hard-400s any `schema_version != 1` (`hooks.py:42,160-186`); ghook writes `.ghook-runtime.json` precisely for skew detection (`main.rs:557-570`) but no daemon code reads it; the bin updater is floor-only (`install/bin_freshness_updater.py:114-167`).
- **Failure mode:** A schema bump without a pin bump 400s every hook POST → PreToolUse/Stop(claude) exit-1 fail-open on all CLIs. The GOBBY_MCP_WRAPPER_STALE class, with the detection artifact already on disk and unread.
- **Minimal fix:** Read the stamp at startup/health-poll; surface in `gobby status`; release-time test tying the pin floor to the supported schema.

### [IMPORTANT] `/api/hooks/execute` is auth-exempt; non-loopback binds allow forged lifecycle events
- **Where:** `servers/middleware/auth.py:32-40` (`/api/hooks/` public); bind host operator-configurable (0.0.0.0 advertised); route trusts `source`/`session_id`/`X-Gobby-Session-Id` verbatim.
- **Failure mode:** Any peer that can reach the port can POST fabricated `session-end`/`stop` envelopes for real sessions. Default-localhost keeps it same-user; one config flag away from a real boundary.
- **Minimal fix:** Require a shared token on `/api/hooks/*` when bound non-loopback, or refuse non-loopback bind without auth.

### [IMPORTANT] Outbound hook webhooks: redirects + custom headers + full event payload; no aggregate deadline for blocking effects
- **Where:** `hooks/webhooks.py:84` (`follow_redirects=True`), `:117-176` (full event.data incl. prompts/tool inputs; custom auth headers re-sent cross-origin); per-endpoint worst case ≈47s sequential (`config/extensions.py:62-79`) plus 30s per foreground mcp_call with no overall budget; `dispatch_webhooks_sync`'s running-loop branch blocks the loop untimed (`webhook.py:126-133`).
- **Failure mode:** A compromised/MITM'd endpoint can redirect daemon POSTs (with headers + payload) to internal addresses; one slow endpoint pushes hook latency past CLI timeouts → fail-open at the CLI seam.
- **Minimal fix:** `follow_redirects=False`, scheme/host validation, strip headers cross-origin; shared deadline well under 20s for blocking effects.

### [IMPORTANT] Fire-and-forget `create_task` without strong references — four more sites
- **Where:** `broadcaster.py:74`, `dispatchers/mcp.py:523`, `mcp_dispatch.py:74`, `dispatchers/webhook.py:181` (same hazard as the filed session_summary_dispatcher case; distinct call sites).
- **Minimal fix:** Shared task-set helper (add/discard) — now 5+ sites.

### [IMPORTANT] Edit-tracking failure silently breaks evidence invalidation
- **Where:** `event_handlers/_tool.py:269-297` — append + evidence reset wrapped in `except Exception → logger.debug`.
- **Failure mode:** On a DB write failure, new edits neither extend `session_edited_files` nor reset `verification_evidence_recorded` — stale evidence stays fresh over unvalidated edits.
- **Minimal fix:** Warn loudly; consider failing the freshness flag closed.

### [NIT] hook_manager small items
- **Where:** `rule_evaluator.py:211-270` (memory/skill dedup RMW race → duplicate injection); `hook_manager.py:382-384` (write-only `_raw_tool_input` deepcopy per tool event); `:100-103` (`_injected_sessions` unbounded).

### [NIT] `EVENT_TYPE_CLI_SUPPORT` omits droid entirely; dead branch in `normalize_mcp_fields`
- **Where:** `events.py:176-364` (no droid column despite `SessionSource.DROID` and a full adapter — documentation table, not routing); `_normalization_mcp.py:128` (`mcp_gobby_call_tool` unreachable after the pre-normalizer).

### [NIT] Session-lookup cache key drops machine/project and never evicts
- **Where:** `storage/sessions/_registration_cache.py:194-293` — composite contract advertised, `(external_id, source)` used; entries live forever.

### [NIT] session_activation small items
- **Where:** `:564-567` (`_workflow_definition_exists` not project-scoped, unlike its sibling); `:477-489` (`update_terminal_pickup_metadata` via hasattr, absent from the protocol); `hook_manager.py:309-313` (#N resolution comment overclaims — never propagated to the CLI via `updatedInput`).

### [NIT] session_coordinator stores entire tmux scrollback as the run result on the success path
- **Where:** `session_coordinator.py:435-455` (`capture-pane -S -` unbounded; error paths truncate, success doesn't).

### [NIT] session-start response assembly
- **Where:** `_session_responses.py:151-239` (dead `agent_info`/`claimed_tasks_info` params; `_get_claimed_task_info` runs twice per boot, second result dropped); `_session_start/agents.py:184-225` (`skills_count` hardcoded 0 vs docstring); coordinator `_registered_sessions` never unregistered on session end; compact-continuation marker consumed even when scheduling fails (`flow.py:81-99` → `compact_continuation.py:188-205`).

### [NIT] Dead/legacy code
- **Where:** `event_handlers/_dispatch.py:27-117` (agent/expansion dispatch handlers unwired; docstrings describe behavior that never executes), `:313-321` (`_release_run_mutex` wrong-arity fallback is an obfuscated no-op); `hooks/git.py` (MergeHookManager has zero production callers; a raising gate hook is treated as allow — and verified: it shares none of the workflows/git_utils porcelain-parsing family); `webhooks.py:262-318` (`trigger()` production-dead; tests validate the dead copy).

### [NIT] Verification/CLI cosmetics
- **Where:** `verification_runner.py:53-56` (skipped results double-counted as passed); `terminal_context.py:29-32` (whitespace-only strings accepted); `_agent.py:375-395` (`handle_after_agent` skips debug echo unlike its two siblings).

### [NIT] Broadcast path
- **Where:** `servers/websocket/broadcast.py:107-112` (sequential per-client send, no timeout — head-of-line blocking behind a slow-but-ponging client); `broadcaster.py:269,354-355` (malformed adapter input → event silently dropped with a single warning); `webhooks.py:183` (unbounded `response.json()`); `dispatchers/mcp.py:534-540` (background-flagged calls run synchronously without a loop).

### [NIT] ghook contract drift docs
- **Where:** non-critical failure exits 1 vs design doc's exit 0 (`main.rs:470-481` vs completed plan + `docs/guides/hook-schemas.md:436-438`); grok template `"timeout": 30` vs gemini/qwen `30000` — units unverified.

## Systemic patterns

1. **Fail-open is the ambient default, decided independently at six+ seams that disagree.** Health gate (allow, all events incl. STOP), route exceptions (allow incl. STOP), rule-evaluator swallow (filed in #15775), handler exceptions (allow, pre-rule for SESSION_START), blocking webhooks (allow on any failure, no fail-closed option), ghook non-critical failures (exit 1 → proceed), unknown hook types (NOTIFICATION), unknown CLIs (claude fallback — the Grok Blocker). Fail-closed islands exist (route Stop timeout, workflows variable-load, codex Stop, cancellation) but posture is keyed to *failure class*, not *event criticality* — the same STOP event is fail-closed for one failure mode and fail-open for four others. One explicit per-event-type posture table would close the class.
2. **Per-CLI contract data is quadruplicated with no conformance test**: ghook `CliConfig` (sibling repo), `FAIL_SAFE_HOOK_TYPES`, the `events.py` support table, and six install templates each encode hook names/criticality independently. Grok fell through exactly this gap; droid avoided it only via a hand-written installer gate.
3. **Absence of signal treated as success** — `_shell_tool_succeeded` defaults True, undefined verification commands "pass", skipped stages exit 0, hold-open never consults the computed result. Inference-by-default is the root of both gate-poisoning findings.
4. **Three mcp_call dispatchers with three success contracts** (deferred: default-False; inline: default-True; web-chat: none), plus two webhook dispatch copies (one dead). Any rule behaves differently per surface.
5. **Thread/loop bridging by convention, not abstraction** — the `get_running_loop → create_task | run_coroutine_threadsafe | asyncio.run` triad is hand-copied in 6+ places with per-copy bugs (dead flush, never-firing broadcast, cross-loop clients, missing strong refs). One audited `schedule_on_daemon_loop()` helper would fix the class.
6. **Wrong-scope wiring of daemon-global singletons** (skill manager, pipeline-execution manager pinned to the personal project; forever-caches without invalidation) — same class as the agents review's scattered TmuxConfig.
7. **Pre-created vs fresh session-start path drift** — profile seeding, handoff variables, skip flags each live on exactly one path.
8. **The gated agent sits inside the trust boundary**: any agent with Bash can write the planned-shutdown marker, drop inbox files, set `GOBBY_HOOKS_DISABLED`, or `gobby stop`. Hook enforcement is advisory against the agent itself — worth one explicit doc paragraph.
9. **Tests mock the wrong contract** at the highest-severity seams: auto-heal dispatch mocks `{"success": True}` shapes production never produces; the transcript-fallback and session-end-unregister tests pin the bugs; no test covers STOP posture in the health gate, FAIL_SAFE timeout/exception behavior, rule-deny precedence in hold-open, or nested call_tool normalization.

## Verified non-bugs (cleared — don't re-chase)

- **Stop-gate core mechanics are sound on the happy path**: rules evaluate before handlers; `stop_attempts` increments on turn_end and resets on turn_start; the ≥8 escape hatch is deliberate; claude/codex Stop block-shape (`TOP_LEVEL_BLOCK` with `continue: true`) is the correct keep-working form, and the daemon's 20s Stop bound < ghook's 30s < claude's 60s, so the block wins the race when the daemon is healthy.
- **turn_start/turn_end mapping is consistent across CLIs** at the rule layer (`{before_agent}` / `{after_agent, stop, stop_failure}`); no bundled rule keys on raw per-CLI events that a CLI lacks.
- **Worker-safety block rules are immune to shell-parse evasion** — they regex the raw command string, not the canonical metadata.
- **Daemon-down loses decisions, not data**: ghook enqueues before POST (fsync + atomic rename), replays critical-first FIFO.
- **Concurrent registration of a new session cannot duplicate rows** (advisory-lock transaction + unique-index recovery); parent attribution survives both interleavings (COALESCE); handoff theft is guarded by positive terminal-context evidence; startup-context injection dedup is genuinely atomic.
- **Broadcast can never gate hook processing** (fire-and-forget after response built; per-client errors swallowed); no mcp_call recursion (`enforce_workflow=False`); no same-thread `run_coro_blocking` deadlock in current wiring.
- **`run_command` in the verification runner is fail-closed for commands that actually run** (returncode==0 strictly; timeout/exception → failure). The gaps are classification and absence-of-signal, not execution.
- **Claude mainline failed-Bash evidence is protected** via `post-tool-use-failure` → `is_failure` → `success=False`.
- **Project resolution refuses the daemon-cwd filesystem fallback**; request-scoped contextvar ordering is correct for HTTP hooks.
- **Inbox quarantine cannot lose envelope bytes** (copy-then-unlink); webhook retry arithmetic is correct (`retry_count + 1` attempts, no trailing sleep).
- **`%s` placeholders are correct** per repo convention (CLAUDE.md's `$N` mandate is stale doc drift).
