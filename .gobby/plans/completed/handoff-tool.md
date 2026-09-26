# handoff: One Tool, Two Modes — Structured, Deterministic Session Handoffs

**Plan ID:** handoff-tool

## Overview
`kind: framing`

Unify `clear_self` and `compact_self` into a single `handoff` tool with an explicit mode toggle, under a two-reset doctrine: **`mode="compact"` for context pressure** (same session, lossy in-place compression, no authoring burden required) and **`mode="clear"` for task boundaries** (new session, authored handoff, pull-verified delivery). Clear mode requires structured fields plus daemon-generated deterministic sections (tasks, plan, edited files, commits, unresolved errors) and is delivered pull-only — the successor's first required tool call is `get_handoff_context`, enforced by a rule gate replacing additionalContext injection. Compact mode accepts the same fields optionally; when provided they render into the same document and the existing compact injection delivers it (a compact-fidelity win), and when omitted the existing generated-summary path is the fallback.

Why: today's handoff quality is entirely caller discipline (one free string, "non-empty" is the only contract); delivery is push-based with a 4,500-char inline budget and no receipt; the reset choice (compact vs clear) is implicit in tool names rather than an explicit doctrine; and independent daemon pane-writers collide (observed: `/compact` and a wake message interleaved on one prompt line).

Plan artifact: `.gobby/plans/handoff-tool.md`

## Constraints
`kind: framing`

- No backward compatibility (0.5.0 unshipped); the `clear_self` and `compact_self` tool names both die in P1, unified into `handoff`.
- **Two-reset doctrine**: context pressure → `handoff(mode="compact")`; task switching / deliberate boundary → `handoff(mode="clear")`. The mode enum makes the choice explicit in the schema.
- **No compact machinery is removed.** `wait_for_summary`, the refresh cascade, passive `pre_compact`/`post_compact` handling, and the compact continuation/injection path all stay — they are the no-fields fallback and the CLI auto-compact safety net. Compact mode preserves `compact_self`'s current session-type support verbatim.
- Deterministic sections are DB-only — zero git subprocesses on the delivery critical path. The successor pulls its own live `git status`/`diff`; `get_session_commits` covers the predecessor's commit window on demand.
- The full handoff renders to one markdown document stored in `sessions.summary_markdown` at execute time; staging capture, failure-restore, and the `get_handoff_context` read path keep their current shapes. In compact mode the rendered document flows through the existing `prepare_compact_continuation_variables` injection unchanged.
- The pull gate (clear mode only — compact continues in the same session and keeps injection) fails open argument-agnostically (modeled on `gcode_fail_open`) and exempts discovery tools (`get_tool_schema`, `list_tools`, `list_mcp_servers`, `search_tools`, `recommend_tools` and their `mcp__gobby__` variants) — clear wipes schema leases and `require-current-context-schema-before-call` would otherwise deadlock the gate. It also exempts a pending memory-recall call (`is_pending_memory_recall_call`) to break the mutual deadlock with the memory gate: memory retrieval first, then handoff pull.
- Reference sweep excludes `docs/reviews/*`, `docs/research/*`, and `.gobby/plans/*` (point-in-time historical records).
- **Phase acceptance gates (explicit user approval, not just dependency order):** P2 work must not begin until the user has reviewed P1's live E2E evidence (the checks in section 4) and explicitly approved proceeding — including any proving period of daily use the user wants. P3 work must not begin until the user has reviewed P2's sweep verification and explicitly approved. Each gate is a stop: finish the phase, present the evidence, wait. No agent may self-approve a gate.
- Decision Record (2026-08-21, confirmed; amended same day): unified `handoff` tool with mode enum replacing the earlier rename-only decision; two-reset doctrine replacing the earlier full-removal milestone; 5 authored fields under a brevity-max prose contract (required in clear mode, optional in compact mode); DB-only deterministic sections; render-at-write storage; pull-only clear delivery with before_tool gate + turn_end twin + reserved pending flag; server-side receipt; web-chat parity for clear mode; lightweight plan; per-pane serialized daemon input.

## P1: Build and verify the handoff tool
`kind: framing`

**Goal**: `handoff` ships with both modes — clear mode with structured fields, deterministic DB-only sections, pull-gated delivery; compact mode with optional fields feeding the existing injection — and collision-free pane input.

### 1.1 Add deterministic handoff renderer [category: code]
`kind: deliverable`

Targets:
- `src/gobby/sessions/handoff_render.py`
- `tests/sessions/test_handoff_render.py`

New module with two functions (TDD):

1. `collect_deterministic_handoff_context(db, session)` — DB-only: task refs via `fetch_task_refs_by_session`, titles/states/blockers via `SessionTaskManager.get_session_tasks`, plan pointer (claimed task → `task_artifacts.plan_file_path`, else `plans.root_task_ref` matched against the session's task refs), edited files from the `session_edited_files`/`task_edited_files` session variables, task-linked commits from `tasks.commits`/`closed_commit_sha`, unresolved tool errors from the `open_tool_errors` variable (≤10, rendered like `format_unresolved_errors`), session facts (git_branch, model, turn/tool counters) from the sessions row.
2. `render_handoff_document(agent_fields, ctx)` — agent sections first (`## Current State`, `## Next Steps` numbered list, optional `## Context`, `## Dead Ends`, `## Notes`), deterministic sections appended, one markdown document, plus a footer telling the successor to pull live `git status`/`diff` itself and use `get_session_commits` for the predecessor's commit window. Agent fields may all be absent (compact mode with no fields skips rendering entirely — the generated path handles it — but partial fields render whatever is present).

Style precedent: `format_handoff_as_markdown` / `_format_deterministic_summary`. Edge cases: omit empty sections entirely (no empty headers); tolerate absent variables on fresh sessions; gate git-derived facts on DB attribution variables, never transcript-derived `has_session_edits` (do not inherit the `summary_context.py` wart); read-only — never call into the summary pipeline.

**Acceptance:**

- 1.1.1 - Renderer module exists with both functions. file: `src/gobby/sessions/handoff_render.py`.
- 1.1.2 - Deterministic collection issues zero git subprocesses and only DB reads. test: `tests/sessions/test_handoff_render.py`.
- 1.1.3 - Rendered document contains agent sections first, deterministic sections, and the live-git footer; empty sections omitted; partial agent fields render what is present. test: `tests/sessions/test_handoff_render.py`.

### 1.2 Unify clear_self and compact_self into the handoff tool [category: code] (depends: 1.1)
`kind: deliverable`

Targets:
- `src/gobby/mcp_proxy/tools/sessions/_terminal_clear.py::*` — scope-reason: rename the whole clear tool surface into handoff clear mode with structured fields and execute-time rendering
- `src/gobby/mcp_proxy/tools/sessions/_terminal.py::register_terminal_tools`
- `src/gobby/mcp_proxy/tools/sessions/_terminal.py::compact_self`
- `tests/mcp_proxy/tools/sessions/test_terminal_clear.py::*` — scope-reason: rewrite tool-surface tests for the unified structured tool
- `tests/mcp_proxy/tools/sessions/test_compact_self.py::*` — scope-reason: retarget to handoff compact mode
- `tests/mcp_proxy/tools/sessions/test_mcp_proxy_tools_sessions_registration.py::*` — scope-reason: update registration wiring expectations

One tool `handoff` replaces both registrations. Params: `mode: str` (required, `"clear"` or `"compact"`), `current_state: str | None`, `next_steps: list[str] | None`, `context`, `dead_ends`, `notes`. Mode invariants validated in the tool body (the `create_task` invariant-helper pattern): clear mode requires non-empty `current_state` and non-empty `next_steps` with non-empty entries; compact mode accepts all fields optional. Description states the doctrine (compact = context pressure, clear = task boundary), the brevity-max prose contract, and keeps the existing "the daemon interrupts your turn; the rejected call is not a refusal" warning.

Clear mode keeps `execute_clear_self`'s shape: validate → resolve session/tmux → `stage_clear_attempt` → render via 1.1 → `update_summary(sid, summary_markdown=rendered)` → deliver `/clear` → restore on failure. `_clear_web_chat_self` takes the same fields (web-chat parity). Compact mode dispatches to the existing `compact_self` execute path unchanged, with one addition: when any agent field is provided, render via 1.1 and store to `summary_markdown` before delivering `/compact`, so `prepare_compact_continuation_variables` injects the authored document instead of relying solely on the generated cascade; with no fields, behavior is byte-for-byte today's `compact_self`. Validation failures return before any staging (no state mutation). A renderer exception must not abort delivery: fall back to agent-fields-only rendering (clear) or the generated path (compact) and log. Agent-run rejection preserved verbatim in both modes.

**Acceptance:**

- 1.2.1 - Tool `handoff` registered with the mode enum and 5-field schema; `clear_self` and `compact_self` gone. file: `src/gobby/mcp_proxy/tools/sessions/_terminal_clear.py`.
- 1.2.2 - Clear mode rejects empty/missing `current_state` or `next_steps` before staging; compact mode accepts zero fields. test: `tests/mcp_proxy/tools/sessions/test_terminal_clear.py`.
- 1.2.3 - Clear mode stores the fully rendered document in `sessions.summary_markdown`. test: `tests/mcp_proxy/tools/sessions/test_terminal_clear.py`.
- 1.2.4 - Compact mode with fields stores the rendered document; without fields it matches existing compact_self behavior. test: `tests/mcp_proxy/tools/sessions/test_compact_self.py`.
- 1.2.5 - Web-chat clear path accepts the same fields and stages the same rendered document. test: `tests/servers/websocket/chat/test_clear_session.py`.

### 1.3 Switch clear successor seeding to pull variables and remove injection [category: code] (depends: 1.2)
`kind: deliverable`

Targets:
- `src/gobby/sessions/clear_continuation.py::_clear_handoff_seed_payload`
- `src/gobby/sessions/clear_continuation.py::seed_clear_handoff_variables`
- `src/gobby/sessions/clear_continuation.py::build_clear_self_continue_prompt`
- `src/gobby/sessions/clear_continuation.py::_bound_clear_handoff_summary`
- `src/gobby/sessions/clear_continuation.py::_commit_web_chat_clear_successor_rows`
- `src/gobby/hooks/event_handlers/_session_start/flow.py::_bind_clear_successor`
- `src/gobby/hooks/event_handlers/_session_start/clear_bind.py`
- `src/gobby/install/shared/workflows/rules/context-handoff/inject-clear-handoff.yaml::*` — scope-reason: delete the injection rule template; registry sync soft-deletes the installed row
- `tests/sessions/test_clear_continuation.py::*` — scope-reason: reseed tests around pull variables, delete bounding/breadcrumb tests
- `tests/hooks/test_session_handoff_handlers.py::*` — scope-reason: rebind successor tests to pull-variable seeding
- `tests/workflows/test_context_handoff_rules.py::*` — scope-reason: drop clear-injection rule tests
- `tests/workflows/test_context_handoff_fencing.py::*` — scope-reason: drop clear-template fencing coverage

`_clear_handoff_seed_payload(predecessor)` returns `{"handoff_pull_pending": True, "handoff_predecessor_ref": "#<seq_num>"}` replacing `handoff_summary_injectable`/`clear_handoff_inject_pending`. This single producer arms both paths: terminal seeding via `seed_clear_handoff_variables` (from `_bind_clear_successor`) and web-chat seeding transactionally inside `_commit_web_chat_clear_successor_rows`. `build_clear_self_continue_prompt` becomes a single directive: call `get_handoff_context(session_id="#N")` (keep the one-line daemon-interrupt warning). Delete `_bound_clear_handoff_summary` (unused once injection is gone). Compact-side injection (the compact handoff rule template and `prepare_compact_continuation_variables`) is untouched — compact continues in the same session. Because `flow.py` is near the production line ceiling, move `_bind_clear_successor` and its clear-only helpers into the new `clear_bind.py` module as part of this deliverable; `flow.py` keeps only the call site. `_bind_clear_successor` keeps its per-step fail-isolation; because a seed-armed successor may miss the typed prompt, the gate's block reason (1.4) carries the full recovery instruction so it self-heals. Degraded resolution (marker take fails/missing) stays fail-open: nothing seeded, nothing typed, successor starts fresh; the rendered document persists in `sessions.summary_markdown` and remains manually pullable.

**Acceptance:**

- 1.3.1 - Seed payload carries `handoff_pull_pending` + `handoff_predecessor_ref`; the injectable/bounding variables are gone. symbol: `_clear_handoff_seed_payload`. file: `src/gobby/sessions/clear_continuation.py`.
- 1.3.2 - Continuation prompt is the single get_handoff_context directive with the predecessor ref. test: `tests/sessions/test_clear_continuation.py`.
- 1.3.3 - inject-clear-handoff template removed and no rule renders clear handoff content into additionalContext; compact injection still works. test: `tests/workflows/test_context_handoff_rules.py`.
- 1.3.4 - Web-chat successor rows are seeded with the pull variables transactionally. test: `tests/servers/websocket/chat/test_clear_session.py`.
- 1.3.5 - Clear successor binding lives in the new module; `flow.py` only calls it. file: `src/gobby/hooks/event_handlers/_session_start/clear_bind.py`.

### 1.4 Add the handoff pull gate [category: code] (depends: 1.3)
`kind: deliverable`

Targets:
- `src/gobby/install/shared/workflows/rules/context-handoff/require-handoff-pull.yaml`
- `src/gobby/workflows/handoff_conditions.py`
- `src/gobby/workflows/safe_evaluator.py::*` — scope-reason: register the three new condition helpers in the allowed-funcs block
- `src/gobby/workflows/reserved_variables.py::is_reserved_workflow_variable`
- `tests/workflows/test_handoff_pull_gate.py`

One YAML, three rules (TDD; modeled on the memory-recall gate):

1. `require-handoff-pull-before-tool` — before_tool, priority 10. `when: pending_handoff_pull_ref() and not is_handoff_pull_call(tool_input) and not is_handoff_gate_exempt(event.data.get('tool_name'), tool_input) and not is_pending_memory_recall_call(tool_input)`. Block reason embeds the exact recovery call: `call_tool("gobby-sessions", "get_handoff_context", {"session_id": "<ref>"})`, issued alone, not in a parallel batch.
2. `require-handoff-pull-turn-end` — turn_end twin, same predicate minus the tool checks.
3. `clear-handoff-pull-pending` — after_tool, argument-agnostic fail-open: fires on any `gobby-sessions`/`get_handoff_context` return (no `is_error` check) and sets `handoff_pull_pending` false.

Helpers live in the new `handoff_conditions.py` module (`condition_helpers.py` is near the production line ceiling) and are registered in the allowed-funcs block: `pending_handoff_pull_ref(variables)` returns `handoff_predecessor_ref` when `handoff_pull_pending` is truthy; `is_handoff_pull_call(tool_input)` matches server+tool only (deliberately no session_id match); `is_handoff_gate_exempt` covers the discovery tools listed in Constraints. Helpers must be exception-free: block `when` evaluates fail-closed, so a throwing helper blocks rather than disarms. Add `handoff_pull_pending` to the reserved workflow variables checked by `is_reserved_workflow_variable` (installed rules still write it via `is_internal_rule`). The gate cannot fail itself open: `policy_denied` outcomes are never tracked as open tool errors and produce no after_tool event. Plan mode needs no special-casing: the permitted call is a read and is never plan-blocked.

**Acceptance:**

- 1.4.1 - Gate blocks every native and MCP tool except the permitted call and exemptions while pending. test: `tests/workflows/test_handoff_pull_gate.py`.
- 1.4.2 - Any get_handoff_context return clears the pending flag (success and error paths). test: `tests/workflows/test_handoff_pull_gate.py`.
- 1.4.3 - Memory-gate coexistence: with both gates pending, `get_recall_memories` passes the handoff gate and `get_handoff_context` is not memory-blocked after recall completes. test: `tests/workflows/test_handoff_pull_gate.py`.
- 1.4.4 - `handoff_pull_pending` is reserved against agent `set_variable` writes. symbol: `is_reserved_workflow_variable`. file: `src/gobby/workflows/reserved_variables.py`.

### 1.5 Server-side receipt in get_handoff_context [category: code] (depends: 1.3)
`kind: deliverable`

Targets:
- `src/gobby/mcp_proxy/tools/sessions/_handoff.py::get_handoff_context`
- `src/gobby/mcp_proxy/tools/sessions/_handoff.py::register_handoff_tools`
- `tests/mcp_proxy/tools/sessions/test_handoff_receipt.py`

On every return path (success, found=False, error) — a small finally-style helper — resolve the caller via `get_current_session_id()`; if the caller's variables hold a truthy `handoff_pull_pending`, merge it to False via `SessionVariableManager.merge_variables`. Receipt is a DB fact even with rules disabled and mirrors the rule's argument-agnostic semantics. `_is_bound_clear_successor` authorization and the `sessions.summary_markdown` read path are untouched (1.2 stores the fully rendered document there). A no-arg call that misses the expired predecessor still clears pending (deliberate fail-open; prompt and block reason both carry the exact `#N` to make this rare). Web-chat callers share the code path.

**Acceptance:**

- 1.5.1 - Pending flag cleared on found, not-found, and error returns. test: `tests/mcp_proxy/tools/sessions/test_handoff_receipt.py`.
- 1.5.2 - Bound clear successor still reads the expired predecessor's rendered document. test: `tests/mcp_proxy/tools/sessions/test_handoff_receipt.py`.

### 1.6 Serialize daemon pane input [category: code] (depends: 1.3)
`kind: deliverable`

Targets:
- `src/gobby/agents/tmux/pane_lock.py`
- `src/gobby/agents/tmux/terminal_prompt.py`
- `src/gobby/mcp_proxy/tools/sessions/_terminal_tmux.py::_send_terminal_compaction_command`
- `src/gobby/events/wake.py::WakeDispatcher._dispatch_live_wake_unlocked`
- `src/gobby/sessions/compact_continuation.py::schedule_compact_self_continuation`
- `src/gobby/sessions/compact_continuation.py::_send_compact_self_continuation`
- `src/gobby/sessions/compact_continuation.py::_schedule_coroutine`
- `src/gobby/sessions/compact_continuation.py::_run_coroutine_thread`
- `src/gobby/sessions/clear_continuation.py::schedule_clear_self_continuation`
- `src/gobby/mcp_proxy/tools/sessions/_terminal.py::send_keys`
- `tests/agents/tmux/test_pane_lock.py`

New module-level registry `pane_lock(socket, target) -> asyncio.Lock` keyed by (tmux socket path, normalized pane target) (TDD). `_send_terminal_compaction_command` (the real `_terminal_tmux.py` implementation, not the thin `_terminal.py` wrapper) holds the lock across the entire interrupt → settle → capture → command → verify sequence — this fixes the observed `/compact` + wake-message collision and protects both handoff modes' delivery. Move the continuation typing path (`schedule_compact_self_continuation`, `_send_compact_self_continuation`, `_schedule_coroutine`, `_run_coroutine_thread`) out of the near-ceiling `compact_continuation.py` into the new `terminal_prompt.py` as a neutral `schedule_terminal_prompt` that acquires the lock around its writes; `schedule_clear_self_continuation` and the compact continuation callers use it directly. The MCP `send_keys` tool acquires the lock around its write. `WakeDispatcher._dispatch_live_wake_unlocked` uses non-blocking acquire and defers via its existing debounce/retry machinery when held. `TmuxSessionManager.send_keys` stays lock-free — asyncio.Lock is non-reentrant and the delivery sequence calls it while holding the lock; locking lives at the orchestration layer only. Depends on 1.3 because of shared edits to the terminal tool module and clear continuation; otherwise independent of 1.4-1.5.

**Acceptance:**

- 1.6.1 - Lock registry exists, keyed by socket+pane, and delivery holds it across the full sequence. file: `src/gobby/agents/tmux/pane_lock.py`.
- 1.6.2 - Concurrent wake dispatch during a delivery sequence defers instead of typing into the pane. test: `tests/agents/tmux/test_pane_lock.py`.
- 1.6.3 - Continuation typing path lives in the new module and `compact_continuation.py` no longer types into panes. file: `src/gobby/agents/tmux/terminal_prompt.py`.

### 1.7 Update the session-boundary contract [category: docs] (depends: 1.4)
`kind: deliverable`

Targets:
- `docs/contracts/session-boundary.md`

Rewrite the clear-boundary sections: unified `handoff` tool shape (mode enum, 5 fields, mode invariants, deterministic sections), the two-reset doctrine, marker semantics unchanged, clear delivery now pull-only (seeded pull variables, gate rules, server-side receipt, degraded-path fail-open), compact delivery unchanged plus the optional authored-document path, pane-input serialization. Remaining compact_self mentions elsewhere in this file are P2's sweep; this deliverable owns the clear-boundary and tool-shape content.

**Acceptance:**

- 1.7.1 - Contract describes the unified tool, doctrine, pull delivery flow, gate, receipt, and degraded fail-open path. behavior: "handoff tool delivery contract" in `docs/contracts/session-boundary.md`.

## P2: Sweep guidance to handoff modes by intent
`kind: framing`

**Goal**: no live guidance names `clear_self` or `compact_self`; every reset directive names `handoff` with the doctrine-correct mode. (depends: P1)

**Gate**: starts only after the user reviews P1's live E2E evidence and explicitly approves.

### 2.1 Convert auto-compact-after-task-close to inject-only auto-clear [category: config] (depends: P1)
`kind: deliverable`

Targets:
- `src/gobby/install/shared/workflows/rules/context-handoff/auto-compact-after-task-close.yaml::*` — scope-reason: delete; replaced by the inject-only auto-clear rule
- `src/gobby/install/shared/workflows/rules/context-handoff/auto-clear-after-task-close.yaml`
- `tests/workflows/test_context_handoff_rules.py::*` — scope-reason: retarget auto-close rule tests

Task close is a boundary, so this rule moves to clear mode. A daemon-initiated `mcp_call` cannot author the required clear-mode fields, so the background-call effect and its `_auto_compact_after_task_close_queued_for` queue plumbing die; the existing fallback `inject_context` is promoted to the sole effect, directing the agent to call `handoff(mode="clear")` with its five fields.

**Acceptance:**

- 2.1.1 - New rule injects the clear-mode handoff directive after task close; no mcp_call effect remains. file: `src/gobby/install/shared/workflows/rules/context-handoff/auto-clear-after-task-close.yaml`.
- 2.1.2 - Rule tests cover the injected directive and the removed queue plumbing. test: `tests/workflows/test_context_handoff_rules.py`.

### 2.2 Sweep remaining references and retarget the pressure nudge [category: docs] (depends: 2.1)
`kind: deliverable`

Targets:
- `src/gobby/install/shared/skills/plan/SKILL.md`
- `src/gobby/install/shared/skills/build-coordinator/SKILL.md`
- `src/gobby/install/shared/skills/goal/SKILL.md`
- `src/gobby/install/shared/skills/bridge/SKILL.md`
- `src/gobby/install/shared/workflows/agents/goal-taskmaster.yaml::*` — scope-reason: retarget compaction directive prose
- `src/gobby/workflows/observer_context_usage.py::detect_context_compact_guidance`
- `src/gobby/workflows/observer_context_usage.py::detect_mid_turn_context_compact_guidance`
- `src/gobby/workflows/observer_context_usage.py::_set_guidance`
- `tests/workflows/test_context_pressure_thresholds.py::*` — scope-reason: retarget guidance-text assertions to handoff compact mode
- `tests/hooks/test_misc_handlers.py::*` — scope-reason: retarget guidance-text assertions to handoff compact mode
- `docs/contracts/session-boundary.md`
- `docs/guides/mcp-tools.md`
- `docs/guides/sessions.md`

Verified inventory: 16 references across the skills/agents/docs files. Each is classified by intent: post-spawn context frees (plan skill's three sites, build-coordinator including its get_tool_schema/call_tool example, goal, bridge, goal-taskmaster) are context-pressure continuity → `handoff(mode="compact")`; boundary directives → `handoff(mode="clear")` with the five fields. The context-pressure guidance text produced by the observer symbols retargets to `handoff(mode="compact")` (the nudge rule template itself keeps its name and wiring). Interrupt-warning prose quoted from `COMPACT_SELF_INTERRUPT_WARNING` is rewritten wherever skills carry it. Excluded: `docs/reviews/*`, `docs/research/*`, `.gobby/plans/*`.

**Acceptance:**

- 2.2.1 - Zero clear_self/compact_self matches remain under `src/gobby/install/shared/` and `docs/guides/`/`docs/contracts/`. behavior: "legacy reset tool names absent from live guidance" in `docs/guides/mcp-tools.md`.
- 2.2.2 - Plan skill's post-spawn steps direct handoff compact mode. file: `src/gobby/install/shared/skills/plan/SKILL.md`.
- 2.2.3 - Context-pressure guidance names handoff compact mode and fires on the existing bands. symbol: `detect_context_compact_guidance`. file: `src/gobby/workflows/observer_context_usage.py`.

## P3: Codify the two-reset doctrine
`kind: framing`

**Goal**: the doctrine is durable, discoverable guidance — not tribal knowledge. (depends: P2)

**Gate**: starts only after the user reviews P2's sweep verification and explicitly approves.

### 3.1 Write the doctrine into guides and skills [category: docs] (depends: P2)
`kind: deliverable`

Targets:
- `docs/guides/sessions.md`
- `docs/guides/mcp-tools.md`
- `src/gobby/install/shared/skills/loading-skills/SKILL.md`

A doctrine section in the sessions guide (when to compact vs clear, what each mode guarantees, what the successor/continuation experience looks like, the pull gate and receipt semantics), a matching entry in the MCP tools guide, and a one-line pointer from the always-loaded skill surface so agents discover the doctrine without being told. Also record the explicit deferral of any future compact-machinery removal decision: revisit only after clear mode has proven out in daily use and compact-summary-fidelity has landed — as prose context here, deliberately not a plan commitment.

**Acceptance:**

- 3.1.1 - Doctrine section exists with mode-selection guidance and guarantees. behavior: "two-reset doctrine" in `docs/guides/sessions.md`.
- 3.1.2 - MCP tools guide documents the handoff tool with both modes and the gate. file: `docs/guides/mcp-tools.md`.

## 4 End-to-End Verification
`kind: verification`

P1 focused tests: `GOBBY_TEST_PROTECT=1 uv run pytest tests/mcp_proxy/tools/sessions/test_terminal_clear.py tests/mcp_proxy/tools/sessions/test_compact_self.py tests/sessions/test_clear_continuation.py tests/hooks/test_session_handoff_handlers.py tests/workflows/test_context_handoff_rules.py tests/workflows/test_context_handoff_fencing.py tests/servers/websocket/chat/test_clear_session.py tests/sessions/test_handoff_render.py tests/workflows/test_handoff_pull_gate.py tests/agents/tmux/test_pane_lock.py tests/mcp_proxy/tools/sessions/test_handoff_receipt.py -v`.

P1 live E2E, clear mode (mirrors the verification run that validated ef8e611a5d): in a gobby-managed tmux session with a claimed task and one edited file, call `handoff(mode="clear")` with all five fields; verify (a) `/clear` delivered without wake collision, (b) the successor's typed prompt is exactly the `get_handoff_context(session_id="#N")` directive, (c) a probe tool call before the pull is policy-blocked with the recovery reason, (d) `get_handoff_context` returns the rendered document containing task refs, plan pointer, edited files, and session facts, (e) `handoff_pull_pending` is false afterward, (f) `sessions.summary_markdown` holds the full document.

P1 live E2E, compact mode: call `handoff(mode="compact", current_state=..., next_steps=[...])`; verify the rendered document lands in `summary_markdown` and the same-session continuation injection delivers it; then call `handoff(mode="compact")` with no fields and verify byte-equivalent legacy compact behavior (generated path).

P2: `gcode grep -w clear_self src/gobby/install docs/guides docs/contracts` and `gcode grep -w compact_self src/gobby/install docs/guides docs/contracts` return zero matches; daemon restart syncs skill/rule registries without drift errors; pushing `context_usage_ratio` past the soft band injects the compact-mode nudge.

P3: doctrine sections present and consistent with the shipped tool schema; a fresh session can answer "when do I compact vs clear" from installed guidance alone.

## Task Mapping
`kind: framing`

<!-- Updated after task creation -->
| Plan Item | Task Ref | Status |
|-----------|----------|--------|
