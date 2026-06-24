# Review: agents

- **Scope:** `src/gobby/agents/` — spawn pipeline (`spawn_executor.py`, `spawn.py`,
  `spawn_models.py`, `spawn_cache_policy.py`, `spawners/`, `step_workflow.py`,
  `launcher_session.py`), isolation (`isolation.py`, `isolation_git_hygiene.py`,
  `worktree_reuse.py`, `python_env_seed.py`, `code_index.py`, `checkpoint_manager.py`),
  registry/runner core (`registry.py`, `runner*.py`, `session.py`, `sync.py`,
  `runtime_cleanup.py`, `run_completion.py`, `completion_subscribers.py`, `readiness.py`),
  lifecycle monitoring (`lifecycle_monitor.py`, `idle_check_handler.py`, `idle_detector.py`,
  `stall_classifier.py`, `terminal_prompt_monitor.py`, `prompt_detector.py`,
  `agent_health.py`, `loop_tracker.py`), termination/recovery (`kill.py`,
  `agent_cleanup.py`, `task_recovery.py`, `resume_executor.py`, `resume_metadata.py`),
  tmux subsystem (`tmux/`, `pty_reader.py`), and sandbox/trust/misc (`sandbox.py`,
  `trust.py`, `dry_run.py`, `reasoning.py`, `local_model.py`, `provider_rotation.py`,
  `constants.py`). Cross-seam reads into `mcp_proxy/tools/spawn_agent/`,
  `mcp_proxy/tools/agents.py`, `dispatch/`, `storage/agents/`, `runner_lifecycle_agents.py`,
  `runner_maintenance.py`, `hooks/session_coordinator.py`. **Split boundary:** the MCP
  tool / HTTP route / CLI surfaces for agents are covered by the mcp_proxy-tools and
  servers-routes review docs; `worktrees/` and `clones/` internals belong to #15791 and
  were read only as libraries. Note: the task description references `agents/definitions.py`,
  which does not exist — agent definition models live in `workflows/definitions.py`
  (CLAUDE.md drift; feed to #15799–#15801).
- **Reviewer:** Claude Fable 5 — 6-agent parallel fan-out (a 7th, for the API seam, was
  cancelled by the operator; that seam is covered by prior leaves), all Blockers
  synthesizer-verified link-by-link against source. Two reviewers verified tmux semantics
  empirically against tmux 3.6b on an isolated socket.
- **Commit / branch:** `0.5.0` @ HEAD `e93153946` (working tree clean at review time).
- **Summary:** 8 Blocker · 40 Important · 16 Nit — the watchdog stack kills healthy
  agents (the same wrong-liveness-signal class as the pipeline heartbeat, three
  independent instances), kill paths signal recorded PIDs with no identity check,
  tmux delivery silently corrupts multi-line prompts, credentials ride on argv, and
  per-spawn bookkeeping leaks forever. The recurring root: identity and liveness are
  trusted from stale records and never re-verified against the live system.

## Findings

### [BLOCKER] Idle watchdog Escape-interrupts and then kills healthy agents during long tool calls — liveness is transcript-lines only, pane evidence is ignored once stale, and the reprompt budget never resets
- **Where:** `src/gobby/agents/idle_check_handler.py:157-179` (staleness from `session.updated_at`; `:178` proceeds blind when capture fails but session is stale), `:193-195` (`if not session_stale and status == "active"` — pane "active" evidence cannot rescue a stale session), `:189-191`/`:205-254` (fail/reprompt paths; Escape sent before the reprompt text); `idle_detector.py:205-208` (`reset_idle` resets `first_idle_at` only — `reprompt_count` accrues for the lifetime of the run, pinned by `tests/agents/test_idle_detector.py:190-196`); the only DB liveness signal is transcript ingestion (`sessions/processor.py:840,947` `session_manager.touch` on *parsed transcript lines*); hook activity pulses go to an in-memory map only (`servers/routes/sessions/statusline_activity.py:24,45-51`) and are never consulted here.
- **Failure mode:** CLIs write a transcript line only when a message/tool-result *completes*. During one long tool call (pytest, build, mypy) or one long reasoning turn, zero lines appear; after `idle_timeout_seconds=60` the session is stale. Pane evidence showing a live spinner is then ignored (`:193` requires `not session_stale`). After ~5 minutes the handler sends **Escape** — cancelling the in-flight turn/tool — then "Continue working on your task." Each episode increments a reprompt budget that nothing ever resets; once it hits 3, the next 60s-stale check kills the agent ("idle after max reprompt attempts"), tmux destroyed, task recovered, in-flight work lost — even when pane capture failed entirely that tick. The adjacent `cleanup_stale_runs` (30-min inactivity on the same transcript signal) times out and then kills agents running legitimately long operations (this repo's own full test suite exceeds 30 minutes).
- **Why it matters:** Healthy agents doing exactly what they were asked are interrupted from ~6 minutes and killed within ~3 episodes. Same root class as the pipeline-heartbeat Blocker in workflows-engine.md: the liveness signal is one that long-running healthy work does not refresh.
- **Minimal fix:** Feed hook activity pulses into the idle decision (consult `last_session_activity` or persist a debounced touch); honor pane `status == "active"` even when the session row is stale (at minimum for the kill path); reset `reprompt_count` when activity resumes after a reprompt; skip destructive decisions on capture failure.
- **Confidence:** high (chain verified end-to-end; CLI transcript-write timing med-high).

### [BLOCKER] `context_full` insta-kill triggers on generic phrases anywhere on the visible screen
- **Where:** `idle_detector.py:66-71` (`CONTEXT_FULL_PATTERNS` include `would you like to continue` and `conversation is too long`), `:127-131` (searched across the full captured text); `idle_check_handler.py:189-191` (single sweep → `_fail_idle_agent`, no reprompt, no threshold, no checkpoint); capture is far wider than intended (see the `capture_pane -S` Important below).
- **Failure mode:** An agent whose visible transcript contains its own question "Would you like to continue?" — or quoted docs/tests containing these phrases — and whose session has been 60s without a new transcript line is killed on one sweep, misclassified as "context window exhausted". The wrong failure reason also misleads retry/rotation logic.
- **Minimal fix:** Restrict matching to the bottom few lines, require CLI-specific exact limit-error strings, and/or require consecutive hits.
- **Confidence:** high on mechanism; med on phrase frequency.

### [BLOCKER] Loop-prompt detector matches the agent's own prose; 3 ticks of static text ⇒ checkpoint+kill, with "y" keystrokes injected meanwhile
- **Where:** `prompt_detector.py:34-38` (`stuck in a loop`, `repeating myself`, `seems? to be (stuck|looping|repeating)` — no dialog-chrome requirement; contrast the stall classifier's own warning comment at `stall_classifier.py:43-48`), `:42` (`LOOP_DISMISS_KEYS = "y\n"`); `terminal_prompt_monitor.py:102-118` (`record_dismissal` increments on *every 30s tick* the text is visible — no fingerprint dedup, unlike approval prompts at `prompt_detector.py:148-154` — and injects `y`+Enter into the composer); `lifecycle_monitor.py:424-457` (`should_escalate` at 3 → checkpoint + kill as a "doom loop").
- **Failure mode:** Static text matching these patterns for 90 seconds (one long think with "the test seems to be stuck" visible) kills the agent. Agents working on Gobby itself display the pattern strings whenever they read `prompt_detector.py` — near-certain false kill in this repo. The first two ticks inject junk "y" messages into a healthy conversation. The checkpoint only partially mitigates (requires checkpoint storage + task_id + resolvable cwd).
- **Minimal fix:** Require actual CLI dialog chrome; add per-fingerprint dedup and a minimum interval before counting a second dismissal; only count when keys were actually sent.
- **Confidence:** high on mechanism; med-high on frequency.

### [BLOCKER] Kill paths signal recorded PIDs with zero identity verification — PID reuse kills the wrong process
- **Where:** `kill.py:303-351` (DB-pid path: liveness probe `os.kill(pid, 0)` then SIGTERM→SIGKILL, no cmdline/start-time check — the pgrep fallback at `:269-289` *does* verify cmdline; the primary path doesn't), `kill.py:144-158` (Strategy 3 SIGTERMs `terminal_context.parent_pid` with no probe at all), `agent_health.py:214-220` (raw `os.kill(run.pid, SIGTERM)`).
- **Failure mode:** `run.pid`/`parent_pid` are recorded at spawn and can be hours/days stale (agent crashed while the daemon was down; session row outliving its terminal). If the OS reused the PID, the probe succeeds against an unrelated process and the kill path SIGKILLs it. Reached from daemon shutdown over all active runs (`runner_lifecycle_agents.py:347-358`) and `stop_agent_run` whenever tmux is already gone. Tests pin the unverified behavior (`tests/agents/test_kill.py:189-197`).
- **Why it matters:** SIGKILL of an arbitrary user process is unrecoverable data loss.
- **Minimal fix:** Before signaling a recorded PID, verify identity (`ps -p <pid> -o args=` contains provider + session id — the check the pgrep disambiguator already does); refuse on mismatch.
- **Confidence:** high that no verification exists; med on real-world misfire frequency.

### [BLOCKER] Multi-line tmux text injection pastes without bracketed-paste — prompts split into premature submissions, reported as success
- **Where:** `tmux/text_injection.py:136-147` (`paste-buffer -d -b <buf> -t <target>` — no `-p`), `:118-119` (only *trailing* newlines stripped); consumers: wake/P2P delivery via `runner_init/orchestration.py:21-76` and `session_manager.send_keys:622`.
- **Failure mode:** Without `-p`, tmux emits no bracketed-paste guards and converts every LF to CR — agent TUIs treat raw CR as Enter. A multi-line inter-session message is delivered as N truncated submissions: line 1 + submit, line 2 + submit… The receiving agent acts on a truncated prompt; the sender sees success. Empirically verified against tmux 3.6b by the reviewing agent.
- **Minimal fix:** Add `-p` to the `paste-buffer` invocation.
- **Confidence:** high.

### [BLOCKER] API credentials ride on the tmux launcher argv via `-e KEY=VALUE` — readable by any local process
- **Where:** `spawn_executor.py:293` (`env["ANTHROPIC_AUTH_TOKEN"] = request.api_token`; siblings at `:421,543,651,885` for QWEN/XAI/FACTORY keys); `spawners/auth_env.py:95-131` (`terminal_env_passthrough` copies credential keys from `os.environ`); `tmux/spawner.py:132-140` → `tmux/session_manager.py:265-275` (every env pair appended as argv: `args.extend(["-e", f"{key}={val}"])`), executed via `create_subprocess_exec` (`:125`).
- **Failure mode:** `tmux new-session -e ANTHROPIC_AUTH_TOKEN=<secret> …` is world-readable via `ps -ww`/`/proc/<pid>/cmdline` for the duration of the spawn, and the value persists in the long-lived tmux server's session environment.
- **Why it matters:** Live API tokens exposed to any local user/process. On a single-user laptop the blast radius is small; on shared dev boxes or CI runners it is a real credential leak. Threat-model call is the operator's, but recording the mechanism is not optional.
- **Minimal fix:** Exclude credential-class keys from the `-e` set; deliver secrets via a `0600` env file sourced-and-deleted by a wrapper command (the prompt-file machinery already establishes the pattern).
- **Confidence:** high on mechanism; med on severity (threat model).

### [BLOCKER] Completion-registry entries and `completion_subscribers` rows are never cleaned up on normal completion — unbounded memory and DB growth
- **Where:** registration on every spawn (`dispatch/spawn.py:274`, `mcp_proxy/tools/spawn_agent/_factory.py:502` → `completion_subscribers.py:60-89`: in-memory `completion_registry.register` + durable insert via `storage/pipelines.py:813-826`); `notify()` stores results and wakes but removes nothing (`events/completion_registry.py:63-102`); `cleanup()` and `remove_completion_subscribers` are invoked only on restart paths (`runner_lifecycle_agents.py:49-51`, `runner_lifecycle_subsystems.py:441-442` — verified the only call sites); `AgentCleanupHandler.post_terminal_cleanup` (`agent_cleanup.py:104-189`) never touches either; restart recovery re-registers only *active* runs, so completed runs' rows are never deleted.
- **Failure mode:** Per spawned agent, the daemon permanently retains an `asyncio.Event`, result dict, subscriber list, and continuation prompt; `completion_subscribers` rows accumulate forever and survive restarts.
- **Minimal fix:** In `post_terminal_cleanup` (or after a successful terminal `notify()`), call `completion_registry.cleanup(run_id)` and `remove_completion_subscribers(run_id)`; add a startup sweep for rows whose run is terminal.
- **Confidence:** high.

### [BLOCKER] Droid agents record `sandbox_enabled=True` while no sandbox is ever applied
- **Where:** `mcp_proxy/tools/spawn_agent/_implementation.py:449` (sandbox config resolved for every provider, default `enabled=True` via `sandbox.py:85-116` + `config/daemon_sandbox.py:11-13`); `spawn_executor.py:832-923` (`_spawn_droid_terminal` never builds a resolver or sandbox args — `get_sandbox_resolver` (`sandbox.py:494-505`) has no droid entry, verified; the droid branch of `command_builder.py:149-160` emits no sandbox flags); `:864` records `sandbox_enabled=bool(request.sandbox_config and request.sandbox_config.enabled)` — intent, not enforcement — persisted to the session row.
- **Failure mode:** An operator enabling `agent_sandbox` gets zero containment for droid agents while session records and UI report them sandboxed. The same intent-based recording exists at all six spawn sites (`:232,379,505,613,732,864`), so any future resolver gap silently repeats this.
- **Minimal fix:** Record `sandbox_enabled` only when the resolver actually emitted args/env; warn or refuse when a sandbox is requested for a provider with no enforcement path.
- **Confidence:** high.

### [IMPORTANT] Periodic Enter auto-confirms any visible dialog, bypassing the approval-prompt gate
- **Where:** `terminal_prompt_monitor.py:200-232` (`check_periodic_enters` sends Enter to every active pane every 30s; `auto_enter_agent_terminals` defaults True, `config/tmux.py:103-115`) — no prompt detection, regardless of `auto_enter_approval_prompts=False` and the fingerprint-dedup machinery in `check_approval_prompts`.
- **Failure mode:** Any blocking dialog — permission prompt, destructive-action confirmation, trust selector — gets its highlighted default confirmed within 30s. The approval checker's careful gating is dead letter while periodic enters are on.
- **Minimal fix:** Capture the pane first and skip the Enter when any known dialog pattern is visible unless the corresponding gate is enabled.
- **Confidence:** high.

### [IMPORTANT] `health_check` kills the entire tmux server — every agent — on a single transient failure
- **Where:** `tmux/session_manager.py:182-207` — one `list-sessions` timeout (5s) or *any* exception → `kill-server`.
- **Failure mode:** A 5s stall is achievable while tmux is healthy (the event loop blocked by the sync spawn bridge below, host load). The recovery code becomes the outage: total loss of all running agents from one spurious probe.
- **Minimal fix:** Require N consecutive failures or a backoff re-probe; treat only timeouts (not arbitrary exceptions) as staleness evidence.
- **Confidence:** med on trigger frequency; mechanism certain.

### [IMPORTANT] tmux `-t <name>` prefix matching can target — and kill — the wrong session
- **Where:** `tmux/session_manager.py:411` (`kill-session -t name`), `:397,363,459,578,600` (all `-t` uses, none `=`-prefixed for exact match).
- **Failure mode:** tmux resolves targets exact-first then *by prefix*. Empirically verified (tmux 3.6b): with only `agent-157` alive, `kill-session -t agent-15` killed `agent-157` — and `kill_session` SIGTERM/SIGKILLs the matched session's pane process groups. Spawner-generated names can't prefix-collide, but user-named sessions (websocket `tmux_create_session`) and stale-name kills (`missing_ok=True` paths) can. `has_session` prefix-matching also falsely rejects valid new names at create.
- **Minimal fix:** Use `-t "={name}"` everywhere in `TmuxSessionManager`.
- **Confidence:** high (empirical); med on trigger frequency.

### [IMPORTANT] Env values ending in `;` break `new-session` argv parsing — spawn fails on plausible prompt content
- **Where:** `tmux/session_manager.py:265-275` (`-e key=val` pairs); prompt reaches env as `GOBBY_PROMPT` for prompts ≤4096 chars (`spawn.py:219-221`); no value sanitization (`spawners/base.py:30-65`).
- **Failure mode:** tmux treats a token with trailing unescaped `;` as a command separator — empirically verified: `new-session … -e 'FOO=bar;' 'cmd'` fails with `unknown command`, creating nothing. A prompt ending in `;` aborts the spawn with a baffling error. Not arbitrary-command exploitable (following tokens are Gobby-controlled), but a mainline spawn failure.
- **Minimal fix:** Neutralize trailing `;`/`\` in `-e` values, or route such prompts via `GOBBY_PROMPT_FILE`.
- **Confidence:** high (empirical).

### [IMPORTANT] Provider-stall kill triggered by agent-visible error text, without checkpoint
- **Where:** `stall_classifier.py:62-74` (bare `ECONNREFUSED|ECONNRESET|ETIMEDOUT`, `network\s+error`, `APIConnectionError`, `InternalServerError`, `anthropic\..*Error` — despite the module's own comment at `:43-48`), `agent_health.py:304-346` (two sweeps 30s apart → tmux killed + run failed, no checkpoint, unlike the doom-loop path).
- **Failure mode:** An agent debugging network code, or one that printed a failing-test traceback and then thinks for 60s, is killed; the failure is misattributed to the provider, polluting rotation.
- **Minimal fix:** Bottom-lines-only matching; error-context requirements for bare tokens (as rate-limit patterns already have); checkpoint before killing.
- **Confidence:** high on mechanism; med on frequency.

### [IMPORTANT] `capture_pane(lines=N)` actually captures N history lines plus the whole visible screen
- **Where:** `tmux/session_manager.py:566-584` (`-S-{lines}` with no `-E`; panes are 50 rows, `:252-256`); consumers assume "last N lines" (idle 15, stalls 8, trust/loop/approval 15, continuation 30).
- **Failure mode:** Every pattern consumer scans ~N+50 lines including scrolled-off history — the amplifier behind the three search-anywhere kill findings, and it widens `has_unsubmitted_input` matching to past `> `-prefixed user messages, which can permanently suppress idle handling (false-negative zombie until the 30-min reaper).
- **Minimal fix:** Slice to the last N lines in `capture_pane`, or fix the docstring and make consumers slice.
- **Confidence:** high.

### [IMPORTANT] NULL-child-session fallbacks probe and act on the parent — up to SIGTERMing the spawner's terminal and expiring the user's session
- **Where:** `idle_check_handler.py:158` and `agent_health.py:259` (liveness judged on `child_session_id or parent_session_id`); `kill.py:204,144-158` (`close_terminal` fallback SIGTERMs the *parent* session's `terminal_context.parent_pid` — the spawner's CLI); `agent_cleanup.py:114-118,135-152` (cancel/fail cleanup falls back to the parent: expires the parent session and releases the *parent's* worktrees — the success path explicitly opts out at `:323-335`, the cancel/fail paths don't); `task_recovery.py:136-137` (`expected_owner = child or claimed`; both NULL → `is_task_actively_claimed(task, None)` returns `bool(owner)` — any owner "matches", enabling claim theft by the 30s sweep).
- **Failure mode:** For runs with `child_session_id` unset (spawn-failure paths), dead agents look alive (parent fresh), init-timeout never fires for its target case, kill can terminate the user's interactive CLI, and cleanup can expire the user's live session — which makes `sweep_stale_claims` release every automation task that session holds.
- **Minimal fix:** Never fall back to the parent for liveness or destructive targeting; pass `allow_parent_session_fallback=False` on cancel/fail cleanup; treat NULL owner as unattributable in recovery.
- **Confidence:** high on mechanics; low-med on reachability (spawn currently always sets the child id — invariant held by data, not code).

### [IMPORTANT] Stale-run timeout releases task claim and dispatch mutex before — or without — killing the still-live process
- **Where:** `storage/agents/_cleanup.py:81-100` (marks `running→timeout` on inactivity, never kills); next tick `lifecycle_monitor.py:309-312` runs `recover_tasks_from_terminal_agents` (mutex delete + claim release, `task_recovery.py:202,241-251`) *before* `cleanup_terminal_tmux_sessions` kills tmux — and a failed tmux kill is only logged (`agent_cleanup.py:211-217`).
- **Failure mode:** A quiet-but-alive agent (long build) is marked timeout; its claim and mutex are freed while it still runs; the dispatcher spawns a second agent on the same task/worktree → concurrent writes. Contrast `check_unhealthy_agents`, which kills first and aborts cleanup on kill failure (`agent_health.py:198-212`).
- **Minimal fix:** Route timeouts through the kill-verify-then-cleanup path; recovery should verify process death before releasing ownership.
- **Confidence:** high on ordering; med on window width.

### [IMPORTANT] task_recovery mutates stage state and claims without the dispatch mutex and never verifies the agent is actually dead
- **Where:** `task_recovery.py:202` (deletes the run's lease first), `:216-251` (`fail_stage` + `release_task_claim` unprotected), `:142-261` (no liveness probe anywhere — trusts `agent_runs.status`); sweep every 30s (`:269-287`) concurrent with dispatcher heartbeats.
- **Failure mode:** A dispatcher heartbeat can acquire the freed mutex mid-recovery and act on half-mutated state (the `IllegalStageTransitionError` swallow at `:311-320` already compensates for this race). And any path that marks a run terminal without killing it feeds the sweep a "dead" agent that is still running — converting a status-marking bug anywhere into a two-agents-one-task hazard. Same gap as `pipeline_heartbeat.check_stale_tasks` (workflows-engine.md).
- **Minimal fix:** Acquire the task mutex (short TTL, holder="recovery") around recovery writes; probe pid/tmux liveness (and kill) before releasing ownership.
- **Confidence:** high.

### [IMPORTANT] Kill quality: TERM→KILL grace not honored on the tmux path; terminal-close "success" without verified death; single-PID fallback orphans children
- **Where:** `tmux/session_manager.py:432-439` (unconditional SIGKILL 0.5s after SIGTERM, regardless of the caller's `timeout=5.0` — daemon shutdown gives agents 500ms to flush); `kill.py:208-222` (tmux/terminal close short-circuits and returns success without checking the agent process died; `_close_terminal_window` accepts `signal_name`/`timeout` but hardcodes SIGTERM, `:63-68` vs `:157`); `kill.py:328,348` + `agent_health.py:214-220` (single-pid signals, no `os.killpg` — the tmux path kills process groups for a reason; the fallback orphans MCP-server/node children).
- **Failure mode:** Lost in-flight work on managed shutdowns; "successful" kills of SIGTERM-ignoring agents feed the cleanup chain (claim release) while the process lives; leaked child processes until reboot.
- **Minimal fix:** Thread `timeout` into `kill_session` and poll group exit before SIGKILL; after terminal-close success, fall through to the PID liveness wait; use `killpg` in the fallback.
- **Confidence:** high.

### [IMPORTANT] Failed kill permanently skips cleanup — dead-tmux runs with no resolvable PID hold a slot until the inactivity reaper
- **Where:** `agent_health.py:198-212` (`continue` on kill failure, re-skipped every sweep); `kill.py:303-304` ("No target PID found" reported as failure when there is nothing left to kill).
- **Failure mode:** tmux already gone + `run.pid` NULL → kill "fails" → cleanup skipped forever → the run stays `running`, holding an agent slot for up to its full `timeout_seconds`.
- **Minimal fix:** Treat tmux-dead + no-PID as `already_dead` (success), or proceed to cleanup when the failure reason is target-not-found.
- **Confidence:** med.

### [IMPORTANT] Default-config tmux construction scattered — a non-default socket breaks kill, restart reconciliation, wake delivery, and streaming while spawn still works
- **Where:** `kill.py:95-97,168-170` (default `TmuxConfig()`/`TmuxSessionManager()`); `runner_lifecycle_agents.py:110,319`; `mcp_proxy/tools/sessions/_terminal.py:191`; `runner_init/orchestration.py:31`; `runner_broadcasting.py:97`; singleton honors config only on first call (`tmux/__init__.py:55-62`). Spawning honors `daemon_config.tmux` (`spawn_executor.py:68-72`).
- **Failure mode:** With `socket_name`/`socket_path` set: restart reconciliation lists the wrong server, sees no sessions, and runs `_cleanup_missing_tmux_agent_run` for **every live agent** (`runner_lifecycle_agents.py:120-123`) — run records torn down while real sessions keep running orphaned; kills target the wrong server and silently degrade to the unverified-PID path; wake/P2P delivery and output streaming fail.
- **Minimal fix:** Inject the daemon `TmuxConfig` at one init point; forbid argument-less construction outside tests.
- **Confidence:** high on mechanism; conditional on non-default config.

### [IMPORTANT] Failed live-pane verification leaks a permanent tmux session
- **Where:** `tmux/spawner.py:158-180` (verification-failure returns skip `kill_session`; session created at `:152` with `remain-on-exit on`).
- **Failure mode:** Every failed spawn (bad CLI path, instant crash) leaves an orphaned tmux session no monitor will ever reap — the failure means no run record carries `tmux_session_name`, so pane monitor and restart reconciliation never match it. Dispatch retries compound it.
- **Minimal fix:** `kill_session(name, missing_ok=True)` in both failure returns and the except branch.
- **Confidence:** high.

### [IMPORTANT] Tmux output stream corrupts multi-byte UTF-8 at 4 KB read boundaries
- **Where:** `tmux/output_reader.py:256` (per-chunk `data.decode("utf-8", errors="replace")`); the sibling `pty_reader.py:133-137,168-172` uses an incremental decoder with a comment naming this exact bug.
- **Failure mode:** Characters straddling chunks render as U+FFFD in streamed terminal output — frequent with box-drawing TUI chrome.
- **Minimal fix:** Mirror pty_reader's `codecs.getincrementaldecoder`.
- **Confidence:** high.

### [IMPORTANT] `kill_session` raises `AttributeError` on Windows/WSL after killing the session
- **Where:** `tmux/session_manager.py:428,437` (`os.killpg(os.getpgid(...))` — neither exists on Windows; not in the caught tuple), on the platform `wsl_compat.py` exists to support; the PIDs are WSL-internal anyway.
- **Minimal fix:** Gate the killpg block on `not needs_wsl()` or signal inside WSL via `wsl --exec`.
- **Confidence:** med (platform-shipping question).

### [IMPORTANT] `start()` is the one unguarded agent_run transition — a cancelled/failed run can be resurrected to 'running'
- **Where:** `storage/agents/_lifecycle.py:116-127` (no status predicate; every other transition carries `WHERE status IN ('pending','running')`, `:188-321`); unconditional callers `mcp_proxy/tools/spawn_agent/_implementation.py:801,819`, `resume_executor.py:266`, etc.
- **Failure mode:** A cancel landing between spawn and `start()` (shutdown sweep, MCP cancellation) is overwritten back to `running` — a zombie consuming a slot until the 30-min reaper, plus a second terminalization cycle (duplicate notifications/recovery).
- **Minimal fix:** `AND status = 'pending'`, return None on zero rows, callers skip.
- **Confidence:** high.

### [IMPORTANT] Spawn failure after run-row creation leaks a 'pending' row that consumes a dispatcher slot, plus the child session and isolation
- **Where:** Row + child session created in `prepare_terminal_spawn` (`spawn.py:103-138`) before the tmux spawn; no try/except in the spawners or around `execute_spawn` (`_implementation.py:762`) calls `run_storage.fail`; `count_active_agents` counts `pending` (`dispatch/dispatcher.py:729-752`); no periodic reaper touches pending rows (`cleanup_stale_runs` filters `running`, `_cleanup.py:53`; `cleanup_stale_pending_runs` is startup/CLI-only, `runner_lifecycle_subsystems.py:254`); post-isolation failures also skip `cleanup_environment` and session teardown (`_implementation.py:602-611` vs `:962-967`).
- **Failure mode:** Intermittent spawner exceptions (tmux 30s timeout, trust-store IO) silently starve dispatch — leaked pending rows hold slots until restart, and each also keeps `pipeline_heartbeat._has_alive_agents` true, blocking stall recovery of the parent execution. Orphaned child sessions and isolation records accumulate.
- **Minimal fix:** Wrap `execute_spawn` to `fail()` the minted run on exception; add `cleanup_stale_pending_runs` to the periodic monitor loop; clean newly-created isolation and the child session on post-isolation spawn failure.
- **Confidence:** high.

### [IMPORTANT] The agent-slot cap is dispatcher-advisory only, and even there it's check-then-act
- **Where:** Cap checked only at `dispatch/dispatcher.py:169-178`; `spawn_agent_impl` never consults it; `dispatch_batch` fans out with unbounded `asyncio.gather` (`_factory.py:655`); concurrent heartbeat entry points (`build/dispatch_tick.py:36`, `scheduler/executor.py:325`, `system_automation.py:310`) share no lock, and the pending row that makes a spawn "count" is inserted deep inside the action.
- **Failure mode:** MCP/batch spawns blow past `max_active_agents` entirely; overlapping heartbeats can collectively overshoot it. (CLAUDE.md frames the cap as dispatcher-owned, so the MCP gap is arguably by design — but `dispatch_batch` is dispatch tooling and unbounded.)
- **Minimal fix:** Re-check the cap under a short lease inside `spawn_agent_impl`; bound `dispatch_batch` with a semaphore; serialize heartbeats with an `asyncio.Lock`.
- **Confidence:** high (omission); med (overshoot frequency).

### [IMPORTANT] Duplicate-spawn race for the same task on non-dispatcher surfaces
- **Where:** `_implementation.py:497-511` (dedup via `has_active_run_for_task`, no lock; the run row that would block the second caller is created much later).
- **Failure mode:** Two concurrent `spawn_agent` calls for one task both pass the check and both spawn — two agents, one task, one worktree. The dispatcher path is mutex-protected; direct MCP calls are not.
- **Minimal fix:** Take the per-task dispatch mutex around dedup-check→run-insert in `spawn_agent_impl`.
- **Confidence:** med.

### [IMPORTANT] Restart replay/rehydration loops paginate without an offset — only the first 500 rows are ever processed
- **Where:** `runner_lifecycle_agents.py:57-88,184-205,234-303` (each iteration re-issues `list_active(limit=500)`/`list_by_status(..., limit=500)` with no offset; second page is the first page again → loop exits). The correct cursor pattern exists 40 lines away in `lifecycle_monitor.py:357-372`.
- **Failure mode:** Beyond row 500, no completion-event rehydration, tmux reconnection, or cancellation replay after restart — silent partial recovery at scale.
- **Minimal fix:** Thread an offset through each loop.
- **Confidence:** high (shape); low-med (impact at default scale).

### [IMPORTANT] `subscribe_agent_completion` re-registers unconditionally — `register()` replaces the Event and drops existing waiters
- **Where:** `completion_subscribers.py:70-71`; `events/completion_registry.py:50-56` (replace-on-collision, warning only). Sibling call sites check `is_registered` first (`mcp_proxy/tools/tasks/_expansion.py`).
- **Failure mode:** A second subscription for an already-registered run discards the first registration's subscribers; a coroutine blocked in `wait()` on the old Event never wakes.
- **Minimal fix:** Check `is_registered` → `subscribe`, or make `register()` merge.
- **Confidence:** med (collisions rare today).

### [IMPORTANT] Bundled-agent orphan cleanup uses a weaker ownership filter than updates — soft-deletes rows sync doesn't own
- **Where:** `agents/sync.py:229-241` (orphan sweep: gobby-tagged + type only) vs `_is_sync_managed_bundled_agent` (`:43-49`: also requires `project_id IS NULL` + `source IN ('installed','template')`).
- **Failure mode:** Any gobby-tagged agent row not named on disk is deleted every sync — e.g., a force-renamed user customization (permitted via `_agents.py:240` `force=True`) is silently destroyed and the pristine bundled row recreated. Violates "User/project-owned rows are preserved." Same family as the sync wipes in workflows-rules/engine docs.
- **Minimal fix:** Apply the full ownership predicate before deleting.
- **Confidence:** med-high.

### [IMPORTANT] Resume executor spawns the process before persistence and claiming; claim conflicts are swallowed
- **Where:** `resume_executor.py:197-216` (tmux spawn at `:197-199`; `_persist_resume_runtime` and `_claim_task_for_resume` after; no try/except — caller `dispatch/daemon_resume.py:70-77` doesn't catch either); `:276-292` (claim failure logged, foreign-owner skip, resume still succeeds).
- **Failure mode:** A psycopg error after spawn leaves a live, untracked agent (pending row without pid/tmux name — eventually marked error *without being killed*); claim conflicts let the resumed agent work the task unclaimed or in parallel with the rightful owner.
- **Minimal fix:** Claim (or verify claimability) before spawning; kill the just-spawned session on persistence failure; treat claim conflict as resume failure.
- **Confidence:** med-high.

### [IMPORTANT] Resume candidates are never consumed or expired — a daemon-stop run can be resumed twice
- **Where:** `storage/agents/_queries.py:58-74` (selector has no resumed-marker exclusion or age bound); `resume_executor.py:119-122` (marks only the new run with `resumed_from_run_id`; the original is untouched).
- **Failure mode:** Run A resumed into B; B fails; the stage returns to ready; the next tick matches A again and replays a stale provider session into a worktree B already modified — re-running side effects from an old conversation point. Months-old snapshots stay eligible.
- **Minimal fix:** Stamp the original run as consumed on resume; exclude stamped rows; add a max age.
- **Confidence:** med-high.

### [IMPORTANT] Resumed agents reuse `worktree_id` without re-claiming the worktree row released at daemon stop
- **Where:** `resume_executor.py:205-213` (copies worktree_id into runtime only); release at daemon stop via `hooks/session_coordinator.py:675-703` → `agent_cleanup.py:135-139` (`agent_session_id` NULLed "so they can be reused").
- **Failure mode:** The resumed agent works in a directory whose ownership row is unclaimed — reuse logic can hand the same worktree to a concurrent spawn; the resumed run's eventual release finds nothing.
- **Minimal fix:** Re-claim the row before spawn; fail resume if claimed by someone else.
- **Confidence:** med.

### [IMPORTANT] Successful runs sit outside every recovery net for non-automation tasks
- **Where:** `task_recovery.py:17` (recoverable statuses exclude `completed`); `agent_cleanup.py:465-469` (recovery only when not success); `storage/tasks/_automation.py:112` (stale-claim sweep gated on `allow_automation`).
- **Failure mode:** An agent completes without closing/releasing its task; its session is expired; for manual (non-automation) spawns nothing ever frees the claim — the task stays claimed by an expired session until a human notices.
- **Minimal fix:** Extend the stale-claim sweep to claims held by non-active sessions regardless of `allow_automation`.
- **Confidence:** med.

### [IMPORTANT] Generated hook files deterministically break worktree reuse for droid agents
- **Where:** `isolation.py:732` (`.factory/hooks/hooks.json` written by repair), `:709` (CLI-dir copytree); neither path is in the hygiene excludes (`isolation_git_hygiene.py:32,45`) nor the reuse skip-list (`worktree_reuse.py:175-191`); gobby's own `.gitignore` ignores `.claude/`/`.gemini/`/`.codex/` but not `.factory/`.
- **Failure mode:** First spawn writes the hooks file; second spawn for the same task hits "Cannot reuse worktree with uncommitted changes" — every respawn fails until manual cleanup.
- **Minimal fix:** Register generated paths in both the hygiene excludes and `_blocking_status_lines`, mirroring `.mcp.json`.
- **Confidence:** high (droid); med (copytree case).

### [IMPORTANT] Stale worktree registration: recreate-at-same-path fails forever, and the post-prune retry silently corrupts `base_commit_sha`
- **Where:** `isolation.py:246-251` (stale record deleted; no `git worktree prune`, no branch deletion), `:268-279` (recreate at the same deterministic path → `fatal: missing but already registered worktree`, empirically verified); after an eventual prune, the branch-exists fallback (`worktrees/git/_lifecycle.py:107-117`) checks out the old branch tip and `_capture_base_commit_sha` (`isolation.py:297-308`) records the prior agent's last commit as "base" — evidence diffs (`plans/evidence.py:197-217`) silently exclude all previously committed task work.
- **Minimal fix:** Prune and delete the leftover branch in the stale-record path; never capture a reused branch tip as base (use merge-base with the base branch).
- **Confidence:** high.

### [IMPORTANT] Reuse-path rebase changes the effective base but never refreshes `task_artifacts.base_commit_sha` (non-dispatch spawns)
- **Where:** `isolation.py:222-244` and `mcp_proxy/tools/spawn_agent/_worktree_reuse.py:59-60` (fresh base returned in `extra` only; creation path persists, `:297-308`; dispatch path persists via `dispatch/spawn.py:917-960` — direct MCP reuse never does).
- **Failure mode:** After rebase, `git diff old_base..HEAD` includes unrelated upstream commits — inflated/wrong task evidence feeding validation gates.
- **Minimal fix:** Persist the refreshed sha inside the reuse path via `set_artifacts_atomic`.
- **Confidence:** med-high.

### [IMPORTANT] "Local" git-hygiene excludes are written to the main repo's shared `.git/info/exclude`
- **Where:** `isolation_git_hygiene.py:115-123` (`git rev-parse --git-path info/exclude` resolves to the **main** repository's shared exclude from inside a linked worktree — empirically verified), used at `:32,45,99-112`; duplicated in `code_index.py:170-201`. The docstring claims "intentionally local to each worktree".
- **Failure mode:** `.mcp.json`, `.gobby/project.json`, `.gobby/bin/` are excluded in the user's main checkout and every other worktree, persisting after the worktree is deleted — a real untracked `.mcp.json` silently vanishes from `git status` forever.
- **Minimal fix:** Drop the exclude write for worktrees (status-filtering already handles it) or scope patterns repo-wide deliberately and fix the docstring.
- **Confidence:** high (empirical).

### [IMPORTANT] Checkpoint creation destroys pre-existing staged versions that diverge from the worktree
- **Where:** `checkpoint_manager.py:61-66` (captures pre-staged *names*, then `git add -u` overwrites staged blobs before `write-tree`), `:128-133` (restore re-stages worktree versions).
- **Failure mode:** A staged-but-since-modified file's staged blob becomes a dangling object — unrecoverable through any ref or the checkpoint, in the component whose purpose is preserving work. Untracked files are skipped entirely.
- **Minimal fix:** Snapshot the index (`git write-tree`/`stash create`) before `add -u`; restore via `read-tree`.
- **Confidence:** high on mechanism; med on frequency.

### [IMPORTANT] Worktree-reuse hazards: rebase timeout bypasses abort; no concurrency guard against rebasing under a live agent
- **Where:** `worktree_reuse.py:83` (`timeout=120` raises `TimeoutExpired`, uncaught — skips `_abort_rebase` at `:85`, wedging the worktree mid-rebase and bypassing the conflict-typed fresh-retry fallback); `isolation.py:220-244` (reuse never checks `existing.agent_session_id` claim state — a second spawn can rebase the branch beneath a running agent whose tree is momentarily clean).
- **Minimal fix:** Catch `TimeoutExpired` → abort → re-raise; refuse reuse when the row is claimed by a live session (claim/release primitives already exist, `storage/worktrees.py:335-352`).
- **Confidence:** high / med.

### [IMPORTANT] `_patch_claude_json` replaces the whole per-project entry in `~/.claude.json` on every repair
- **Where:** `isolation.py:872-875` (`projects[isolated_path] = {"mcpServers": ...}` — wholesale, on creation and every reuse).
- **Failure mode:** Claude Code's per-project state (trust/onboarding acceptance, allowed tools, history) for the worktree path is wiped each respawn — re-triggering trust prompts and discarding permission grants.
- **Minimal fix:** `projects.setdefault(path, {})["mcpServers"] = ...`.
- **Confidence:** med.

### [IMPORTANT] Maintenance reaper runs `git worktree remove/prune/branch -D` with no cwd (cross-seam)
- **Where:** `runner_maintenance.py:738-743` (`subprocess.run` without `cwd`), used at `:577-595`.
- **Failure mode:** The daemon's cwd is generally not the repo: `worktree remove` fails, the `rmtree` fallback deletes the directory, then `prune` and `branch -D` run against the wrong repo — leaving the real repo with the registered-but-missing state that feeds the recreate-fails-forever finding above.
- **Minimal fix:** Resolve the project repo path and pass `cwd=`.
- **Confidence:** high on mechanism.

### [IMPORTANT] Local-model active-agent conflict detection is dead code — model swaps yank models from under running local agents
- **Where:** `mcp_proxy/tools/spawn_agent/_implementation.py:420-421` (`runner.registry if hasattr(...)` — `AgentRunner` has no `registry` attribute, `runner.py:30-88`; always None); `local_model.py:122-136` (with `registry=None`, `active_count=0`, the guard never fires; swap unloads all models); doubly broken even with a registry (`list_running` doesn't exist; `run.model == "local"` never matches the resolved name, `:63` vs `_implementation.py:412,421`).
- **Minimal fix:** Pass the `LocalAgentRunManager`, query active local runs properly, key on a stable `is_local` flag.
- **Confidence:** high.

### [IMPORTANT] Provider capability matrix drift: gemini reasoning reported "applied" but dropped; droid reasoning blocked though its CLI supports it
- **Where:** `reasoning.py:22` (`_TERMINAL_REASONING_PROVIDERS` includes gemini, omits droid) vs `command_builder.py:96-107` (gemini branch emits no reasoning flag) and `:149-160` (droid branch emits `--reasoning-effort`, unreachable); `reasoning_required=True` droid spawns rejected outright (`_implementation.py:377-382`).
- **Failure mode:** Users get "applied" with nothing applied (gemini) and hard spawn failures for a supported capability (droid). Three surfaces (reasoning, command_builder, sandbox resolvers) each hold their own provider matrix with no single source of truth — the droid sandbox Blocker is the same disease.
- **Minimal fix:** One provider→capability table consumed by all three.
- **Confidence:** med-high.

### [IMPORTANT] Trust-store seeding races: concurrent spawns lose trust entries → headless agents hang on interactive prompts
- **Where:** `trust.py:344-454` (load→mutate→`os.replace` of `~/.gemini/projects.json`, `trustedFolders.json`, `~/.codex/config.toml`); `pre_approve_directory:114-125` takes no lock — unlike `authorize_model_discovery_trust:155-168`, which guards the identical RMW.
- **Failure mode:** Up to 10 concurrent spawns; last writer wins; the dropped path's CLI shows an interactive trust prompt — the exact hang this module exists to prevent. Atomic writes prevent corruption, not lost updates.
- **Minimal fix:** Reuse the per-CLI lock on the spawn path.
- **Confidence:** med.

### [IMPORTANT] Qwen ACP sandbox sits at the exact 5-dir `--include-directories` limit; one extra write path raises `ValueError` mid-spawn
- **Where:** `sandbox.py:338-351` (raises >5; limit at `:57`); worktree agents hit exactly 5 by default (2 git-metadata + uv-cache + cargo-home + hook-inbox); uncaught at `spawn_executor.py:399`.
- **Failure mode:** A valid config (worktree isolation + one `extra_write_paths` entry) hard-crashes the spawn instead of degrading.
- **Minimal fix:** Aggregate/cap dirs or fail gracefully at the spawner boundary.
- **Confidence:** med.

### [IMPORTANT] Sync DB, git, and subprocess work on the asyncio event loop across the subsystem
- **Where (representative, all verified by reviewers):** spawn path (`spawn.py:164-213` DB writes; `tmux/spawner.py:88-101` — sync `spawn()` blocks the loop on `future.result(timeout=30)` from all six async provider spawners, and the ThreadPoolExecutor `__exit__` keeps blocking past the timeout); isolation (`isolation.py:215-489` — `create_worktree` fetch 60s, `create_clone` up to 600s, inline on the loop while the same methods wrap *other* calls in `to_thread`); kill (`kill.py:78-79,231`); resume (`resume_executor.py:128-199`); dry-run (`agents/dry_run.py:119,239`); dispatcher heartbeat storage access (`dispatcher.py:130-270`); cancellation fallback (`agent_cancellation.py:70`).
- **Failure mode:** Spawn bursts and clone-backed isolation stall every hook, WebSocket, and HTTP request for seconds to minutes — and can themselves trip the 5s tmux `health_check` (whose response is `kill-server`).
- **Minimal fix:** Await an async spawn path; wrap DB/git in `to_thread`/`run_db` (the helpers already exist in the same files).
- **Confidence:** high.

### [IMPORTANT] Load-bearing contracts pinned only by mocks
- **Where:** tmux suite is fully mock-based (`tests/agents/test_tmux.py` — the four empirically-demonstrated tmux bugs all pass it); no test for kill-before-release ordering, PID identity, mutex acquisition during recovery, resume claim-conflict/candidate consumption, start()-after-terminal, pending-row reaping, droid hooks blocking reuse, stale-registration recreate, checkpoint divergent-staged restore, or concurrent worktree reuse.
- **Minimal fix:** A small integration-marked tmux module against a throwaway `-L` socket (exact-match targeting, trailing-`;` env, multi-line paste, FIFO multi-byte streaming) plus storage-backed lifecycle tests for the contracts above.
- **Confidence:** high.

### [NIT] `registry.py` (840 lines) is dead production code with divergent kill/cleanup logic
- **Where:** zero `src/` importers (tests only; project memory confirms "ready for deletion"); its `cleanup_stale()` evicts by age regardless of liveness and `kill()` does sync DB in async. Delete before someone resurrects it.

### [NIT] `SpawnRequest.max_agent_depth` is informational only
- **Where:** `spawn.py:222,234` (env hint only); enforcement uses the manager's constructor value (`session.py:95,134`). Values coincide today (both 5); divergent defaults elsewhere (`runner.py:44` / `session.py:83` default 1 vs `constants.py:104` 5).

### [NIT] Long-prompt temp files cleaned only at process exit
- **Where:** `spawners/prompt_manager.py:89-94` (atexit only) — 0600 files accumulate in `/tmp/gobby-prompts` for the daemon's lifetime.

### [NIT] Idle/init heuristic fragility
- **Where:** `agent_health.py:263-274` (any daemon-side session write >5s after creation defeats init-timeout detection); `:146-148` (unparsable `started_at` skips the whole health check for that run); `lifecycle_monitor.py:273-297` (one failing sub-check aborts the remaining ten for that tick).

### [NIT] Monitor state hygiene
- **Where:** `IdleDetector._states` never cleared on normal completion (`agent_cleanup.py:130-133` clears four sibling trackers, not this one); `terminal_prompt_monitor.py:70-213` uses `assert` for control flow (vanishes under `-O`).

### [NIT] Kill-path oddments
- **Where:** `kill.py:63-68` (dead `signal_name`/`timeout` params); `:282-287` (multi-match disambiguation picks highest PID — wraparound-unsafe; prefer start-time).

### [NIT] Recovery sweep churn and asymmetries
- **Where:** `task_recovery.py:269-287` (re-processes the same ≤300 terminal runs every 30s forever — no recovered-marker); `agent_cleanup.py:350-357` (cancelled no-op path skips post_terminal_cleanup while the success no-op runs it); `:154-159` (the one unguarded await in a best-effort chain); `task_recovery.py:204` (provider errors never count toward `dispatch_failure_count` — a permanently bad API key respawns forever unless rotation backstops it).

### [NIT] `cancel_run` double session transition; fallback cancellation loses `terminal_reason`
- **Where:** `runner_queries.py:76-77` (sets `cancelled` after storage already set `expired`); `agent_cancellation.py:70` fallback passes no `terminal_reason`, hiding those runs from the `daemon_restart`-filtered replay (`runner_lifecycle_agents.py:257`).

### [NIT] Dispatcher `create_isolation` fabricates artifact pairs (cross-seam)
- **Where:** `dispatch/dispatcher.py:825-861` — relative path that never exists + uuid referencing no row; currently unreachable from bundled rules and sanitized downstream, but exported surface violating the artifact contract.

### [NIT] Path-generation collisions; checkpoint seq race; `task_seq_num=0` falsy
- **Where:** `isolation.py:378-382,592-596` (`feat/x` ≡ `feat-x`; namespace keyed on repo dir name, not project id — loud create failure, not corruption); `checkpoint_manager.py:97-99` (non-atomic seq → ref overwrite; rows keep both SHAs); `isolation.py:92`.

### [NIT] isolation.py at 981 lines — 19 under the monolith cap
- **Where:** `isolation.py`; the droid-hooks and MCP-config blocks are self-contained extraction candidates with sibling precedents.

### [NIT] tmux reader oddments
- **Where:** `output_reader.py:85` (unguarded `proc.kill()` in the timeout handler can raise `ProcessLookupError` and skip FIFO cleanup); `:249-253` (EOF branch busy-wakes every 0.2s forever); `text_injection.py:154-157` (`except Exception: pass`); `session_manager.py:147-150` (strict decode of subprocess output); per-stream executor-thread polling pins default-pool threads (use `loop.add_reader`).

### [NIT] `wsl_compat.convert_windows_path_to_wsl` mishandles UNC and drive-relative paths
- **Where:** `tmux/wsl_compat.py:19-47` — `\\wsl$\...`, `\\server\share`, `C:foo` pass through unchanged into `create_session` cwd.

### [NIT] Daemon-owned sandbox can never be restrictive; provider-rotation history capped at 20 runs
- **Where:** `sandbox.py:50-51,85-116` (mode hardcoded permissive; the restrictive resolver branches are dead for daemon-owned spawns); `provider_rotation.py:71-79` (`LIMIT 20` can resurrect an exhausted provider on long retry histories).

### [NIT] `TmuxPTYBridge.attach` duplicate-ID TOCTOU (latent)
- **Where:** `pty_bridge.py:75-112` — check and insert under separate lock acquisitions; a collision leaks an fd + tmux client. Unreachable with current uuid-based callers.

## Systemic patterns

1. **Identity and liveness are trusted from stale records, never re-verified against the live system.** PIDs signaled without identity checks; DB terminal status trusted without a death probe; resume snapshots replayed without consumption marks; worktree ownership copied without re-claiming; transcript-ingestion timestamps standing in for "is it alive". Five of the eight Blockers and a third of the Importants instantiate this.
2. **Terminal monitors confuse content the agent is *displaying* with chrome the agent is *blocked on*.** Search-anywhere regexes (context-full, loop, provider-stall, trust) over a capture window that is silently the whole screen (`-S` semantics), with patterns that match agent prose. The stall classifier's comment shows the team knows the risk; only rate-limit patterns got context guards.
3. **Destructive actions lack the gating benign actions have.** Approval-Enter has fingerprint dedup; loop dismissal (which escalates to kill) has none. Reprompts wait 300s; the post-budget kill waits 60s. Context-full kills on one sweep; provider stalls need two. Periodic Enter bypasses every prompt gate outright.
4. **NULL-child-session fallback to the parent** recurs in idle, init-timeout, kill, cleanup, and recovery — every instance either masks a dead child or aims a destructive action at the spawner. One schema-level invariant would close the class.
5. **Guards live at the dispatcher seam only** — slot cap, per-task mutex, duplicate-spawn dedup all bypassable from the MCP tool and batch surfaces that call the same implementation.
6. **Eager registration, terminal-path-only cleanup.** Completion events, subscriber rows, pending run rows, child sessions, and isolation records are created early; only specific happy/monitored paths clean them.
7. **Default-config object construction scattered** (tmux socket; `TmuxConfig()` in five+ call sites) — config honored at spawn, ignored at kill/reconcile/wake/stream.
8. **Sibling-fixed bugs unfixed in twins**: incremental UTF-8 decode (pty_reader yes, output_reader no), `proc.kill()` guards, `errors="replace"`, RMW locks (model-discovery trust yes, spawn trust no), pagination cursors (lifecycle_monitor yes, lifecycle_agents no).
9. **Provider capability matrices duplicated across three surfaces** (reasoning, command builder, sandbox resolvers) with no source of truth — silent drops, false "applied" reports, and wrong refusals.
10. **Sync DB/git/subprocess on the event loop** despite `_run_db`/`to_thread` existing in the same files — the known repo-wide systemic, at its worst here (600s clones inline).

## Verified non-bugs (cleared — don't re-chase)

- **Depth-5 limit holds.** Single chokepoint `can_spawn_child` (`session.py:118-165`), all spawn surfaces funnel through it, off-by-one correct (0→5 chain), depth computed from the parent's DB row — not spoofable via `extra_env` (reserved keys rejected, `spawn_executor.py:46-84`).
- **Atomic worktree/clone pair writes hold at every site** — `set_artifacts_atomic` merges and rejects torn pairs at the storage layer (`storage/tasks/_artifacts.py:98-213`); torn writes are impossible even for misbehaving callers.
- **No shell injection in spawn/kill argv**: list argv + `shlex.join` for tmux; terminal-context values regex-validated (`kill.py:24-36`); literal tmux injection uses `set-buffer -- <text>` (key syntax never interpreted) — the only defect is the missing `-p`.
- **Prompt files are secure** (0700 dir, 0600 atomic create, sanitized session id); OAuth token deliberately excluded from passthrough; resume metadata keeps only cache-path env (`resume_metadata.py:59-68`).
- **Terminal transitions other than `start()` are CAS-guarded** and all callers honor race losses; the monitor cannot clobber a concurrent completion.
- **No monitor double-fire** (single instance, guarded `start()`, sequential loop); per-row exception isolation in every sweep; monitor DB access correctly routed through `run_db`.
- **`_has_alive_agents`' status filter matches the live status vocabulary** (`list_by_parent` → `('running','pending')`) — the pipeline-heartbeat bug is which *session* it queries, not the filter.
- **Worktree release never deletes directories**; merge-artifact deletion correctly gated on merge-done; doom-loop kills checkpoint first; reuse refuses dirty trees and falls back to a fresh uuid-suffixed branch on rebase conflict.
- **gcode index collision across worktrees disproven empirically** (overlay project ids, tombstones — no clobbering of the main index).
- **python_env_seed is sound** (copy link-mode, offline+frozen, env cleared); per-workspace bootstrap DSN written 0600 with password redacted from error output.
- **FIFO streaming start ordering is safe** (mkfifo → pipe-pane → blocking `>>` open); websocket bridge attach/detach lifecycle pairs cleanup correctly.
- **Trust seeding only ever adds trust to Gobby-owned directories** — fail-open there is a hang risk, not a bypass; no capability/trust-level gating exists to escalate through.
- **`%s` placeholders are correct** per repo convention (CLAUDE.md's `$N` mandate is stale doc drift).
