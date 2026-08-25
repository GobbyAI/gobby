# Grok Hook Compatibility And Deferred Session Materialization

**Plan ID:** grok-hook-deferred-materialization

## Overview
`kind: framing`

Stop creating Gobby session rows on CLI `SessionStart`, and stop shipping
`additionalContext` on that event. Materialize the session on the first
non-`SESSION_END` / non-`NOTIFICATION` hook (usually `UserPromptSubmit`) for
every CLI. Fix
Gobby's Grok 1.0.5 wire contract so Stop/SubagentStop use `decision: "block"`
plus `additionalContext`. Deliver Grok observe injects through a pending
buffer: briefing-class deny-once on the next PreToolUse (including in-process
compact); turn-context never forces a Stop continuation loop. Supersedes
#20724 P2 / #20635 MCP-result delivery.

## Constraints
`kind: framing`

**Grok 1.0.5 is observe-only except PreToolUse deny and Stop/SubagentStop.**
Gobby's capability table is wrong today. Do not invent a Grok `systemMessage`
channel; Grok's PreToolUse JSON parser only reads `decision`, `reason`, and
`hookSpecificOutput.updatedInput`. Stop `additionalContext` is real and also
keeps the agent working.

**SessionStart(startup) with no pre-created row must not call
`SessionManager.register_session`.** Idle `grok` / `claude` processes must
leave `sessions` empty until the user submits a prompt. Compact, resume,
clear, web-chat, and pre-created (`gobby_session_id` / spawned agent) paths
still bind on SessionStart; they just stop returning `additionalContext`.

**Thin auto-register on first non-start hook is not activation.**
`SessionLookupService._resolve_uncached_session_id` already calls
`register_session` when SessionStart did not. Keep that **row create** inside
the lookup lock. Do **not** run agent/wiki/transcript activation under the
lock (`SessionLookupService` has no handler, and it would serialize every
lookup behind I/O). `HookManager._handle_after_daemon_ready` runs the
activation tail after `resolve()` returns. Concurrent UPS+PreToolUse: each
racer independently runs the idempotent activation to completion before its
own rules and handler proceed — no claim, no wait for the idempotent steps;
briefing staging and copied-rule evaluation carry their own sub-protocols
(decision 7).

**Idle vs any-event materialization.** SessionStart(startup) plus a later
SESSION_END with nothing in between creates no row (SESSION_END already skips
orphan auto-register). Any other hook for that `external_id` — UPS, PreToolUse,
PostToolUse, Stop, PostCompact, … — implies activity and materializes,
except `NOTIFICATION` (decision 3). That is the fallback, not a UPS-only
special case.

**Grok UserPromptSubmit stdout is ignored.** Moving SessionStart context to
UPS fixes Claude/Qwen/Droid/Codex and session-row timing for everyone. Grok
still needs a pending-variable flush (decision 4).

**Grok compact is in-process.** Probe #20633 and
`MiscEventHandlerMixin.handle_post_compact` already treat Grok as
PreCompact → PostCompact with no SessionStart. Compact continuation is
**briefing-class**, armed from PostCompact, not turn-context flushed at Stop.

**`src/gobby/hooks/event_handlers/_session_start/flow.py` is 934 lines.**
Split it: move register/activate/seed into
`src/gobby/hooks/event_handlers/_session_start/materialize.py` so `flow.py`
stays under the 1,000-line ceiling.

**`src/gobby/hooks/hook_manager.py` is 864 lines (≥850).** Split first-prompt
activation into `src/gobby/hooks/session_materialize.py` and Grok stash/flush
into `src/gobby/hooks/grok_pending_context.py` so neither 3.1 nor 4.1 grows
`hook_manager.py` across the ceiling.

### Collision with compact-summary-fidelity (#20724)
`kind: framing`

#20724 is already expanded. This plan **supersedes** its P2 Grok compact
handoff leaves (#20727 / #20733–#20735) and deferred #20635 (section 3), which
mandate `ContextChannel.NONE` on every Grok hook and MCP `call_tool`
delivery. Stop/SubagentStop *do* have `additionalContext`.

**Coordinator-owned collision cleanup, two realizable phases.** The
coordinator session that carries this plan through final approval executes
the cleanup. Every step references resources that exist at the time it runs,
and every amendment to the registered compact-summary-fidelity plan goes
through the plans service — edit the file, then
`gobby-plans:update_plan_hash(plan_id="compact-summary-fidelity")` (recomputes
the registry hash and regenerates the managed coverage manifest), then
`gobby-plans:validate_plan` must pass — never a freehand file edit that
leaves the plans row and coverage ledger stale.

**Phase A — after final approval, before expansion** (existing resources
only):

1. **#20635 stays open.** It is compact-summary-fidelity §3's deferral
   carrier: it keeps its `deferred-from:compact-summary-fidelity:3`
   provenance and its #20724 parentage, and §3's `task_ref` keeps pointing at
   it for the whole cleanup. Closing it before delivery (any disposition
   other than `completed`/`already_implemented`) orphans the registered
   deferral and fails `validate_plan`.
2. Close #20733–#20735 and sub-epic #20727 as `obsolete`.
3. Drop #20724's blocked-by dependency on #20727 (`gobby-tasks` calls).
4. Amend compact-summary-fidelity: rewrite §4's live Grok check off
   `wait_for_summary` (deny-reason continuation block instead), and annotate
   §3 as superseded-pending-delivery by plan
   `grok-hook-deferred-materialization` (a plan-id reference that exists
   now; `task_ref` unchanged). Run the update_plan_hash + validate_plan
   amendment sequence above.

**Phase B — after expansion creates this plan's epic task, before the
automation opt-in.** The pre-dispatch interval is mechanically enforced by
splitting expansion from dispatch: expansion alone never dispatches —
`gobby build` is the explicit opt-in to state dispatch. The coordinator (a)
expands this plan through the manual expansion path (`/gobby expand` /
expand-task) with automation disabled, obtaining the concrete epic task id;
(b) adds a blocked-by dependency from #20635 onto that epic via
`gobby-tasks`, so the carrier's obligation is formally owed by this plan's
delivery (carrier still open, provenance and parentage intact, §3 `task_ref`
unmoved); (c) re-runs `gobby-plans:validate_plan` on
compact-summary-fidelity; and only then (d) runs `gobby build '#<epic>'` to
opt the epic into dispatch. No descendant is dispatch-eligible before step
(d), so no prose-only interposition window exists. When this plan's epic
completes, #20635 closes as `completed`/`already_implemented` — never
before.

#20726 (digest/summary fidelity) does not overlap and stays. Expansion must
not begin until Phase A completes; the `gobby build` opt-in must not run
until Phase B steps (a)–(c) complete; V2 item 6 verifies both phases
retrospectively. This plan absorbs: compact continuation block as
briefing-class; delivered-state variables set only on confirmed flush;
`docs/guides/adapter-fidelity.md` Grok row corrected in 1.1.

### Grok references for the implementing agent
`kind: framing`

Read these before touching adapters. Installed CLI is Grok Build TUI 1.0.5
(`~/.grok/bin/grok`, `~/.grok/version.json`).

Local user guide (same text Grok ships):

- `~/.grok/docs/user-guide/10-hooks.md`
  - Hook Events table: `PostToolUse` Blocking? **No**; `PreToolUse` can deny;
    `Stop`/`SubagentStop` can block the stop
  - "How a Hook Resolves" step 3: every event other than PreToolUse/Stop is
    passive — output recorded, control flow unchanged
  - "Passive Hooks": "For events like `SessionStart` or `PostToolUse`, stdout
    is ignored."
  - Stop Decision Control: `{"decision":"block","reason":"..."}` keeps working;
    `hookSpecificOutput.additionalContext` also keeps working;
    `{"continue":false,"stopReason":"..."}` force-stops
  - Porting list: "**UserPromptSubmit is observe-only**: grok ignores its
    exit code and its stdout"
  - PreToolUse output: allow / deny+reason / `updatedInput` only
  - Envelope field for tool output is `toolResult`, not Claude `tool_response`

Public docs:

- https://docs.x.ai/build/features/hooks
  - "For passive events, stdout is ignored; exit 0 on success."
  - Lists PreToolUse as the only blocking event (user guide is more complete:
    Stop/SubagentStop also gate)

Grok-build source of truth (matches 1.0.5 strings in `~/.grok/bin/grok`):

- https://github.com/xai-org/grok-build/blob/main/crates/codegen/xai-grok-hooks/src/event.rs
  - `PostToolUse` traits: `(Observe, Tested, true)`
  - `UserPromptSubmit` traits: `(Observe, Ignored, true)`
  - `Stop` / `SubagentStop` traits: `(Stop, …)`
  - `GateKind::Observe` comment: "Hook output recorded, decisions ignored."
  - `GateKind::Stop` comment: "Stop decision control (`block`, `continue: false`, `additionalContext`)."
- https://github.com/xai-org/grok-build/blob/main/crates/codegen/xai-grok-hooks/src/dispatcher.rs
  - `dispatch_non_blocking` return is `Vec<HookRunResult>` only; Allow/Deny/Stop
    arms collapse to Success
  - `StopDispatchResult.wants_continuation()` is true when `blocks` or
    `additional_context` is non-empty
- https://github.com/xai-org/grok-build/blob/main/crates/codegen/xai-grok-hooks/src/runner/mod.rs
  - `GateHookJson` fields: `decision`, `reason`, `hookSpecificOutput.updatedInput`
  - `StopHookJson` fields: `decision`, `reason`, `continue`, `stopReason`,
    `hookSpecificOutput.additionalContext`
  - Unknown Stop `decision` (including `"deny"`) is `Err` → fail-open

Gobby symbols that currently lie about that contract:

- `GROK_ADDITIONAL_CONTEXT_HOOKS` = `{session_start, user_prompt_submit, post_tool_use, subagent_stop}`
- `GROK_SYSTEM_MESSAGE_CONTEXT_HOOKS` = `{pre_tool_use, pre_compact, stop}`
- `GrokAdapter.translate_from_hook_response` special-cases SubagentStop as
  `decision: "block"` but leaves Stop to
  `ACPHookAdapter.translate_from_hook_response`, which maps `block` → `deny`
  + `continue: false`
- `tests/adapters/test_acp_hook_translation.py::TestBlockToDenyMapping.test_block_maps_to_deny_hard_stop`
  encodes that Stop mapping as intended behavior — it is the bug

### Consumer sweep (index usages empty; literal)
`kind: framing`

Run from repo root; hit lists used to populate Targets:

- `gcode grep -w handle_session_start src/gobby tests`
- `gcode grep -F "GROK_ADDITIONAL_CONTEXT_HOOKS" src tests`
- `gcode grep -F "GROK_SYSTEM_MESSAGE_CONTEXT_HOOKS" src tests`
- `gcode grep -F "_resolve_uncached_session_id" src tests`
- `gcode grep -F "test_block_maps_to_deny_hard_stop" tests`
- `gcode grep -F "register_session.assert_called" tests/hooks`
- `gcode grep -F "user_prompt_submit" tests/e2e/conftest.py`

Owned consumers that change with this work: `HookManager._handle_after_daemon_ready`,
`SessionStartMixin.handle_session_start`, `tests/hooks/test_session_events_coverage.py`,
`tests/hooks/test_hooks_manager.py`, `tests/adapters/test_acp_hook_translation.py`,
`tests/adapters/test_capabilities.py`, `tests/e2e/conftest.py`.

### Locked decisions
`kind: framing`

1. Defer **row creation**, not SessionStart hook install. `ghook` still runs;
   Gobby returns allow and writes nothing for `source in {startup, new, ""}`
   when there is no pre-created row.
2. SessionStart **never** emits `additionalContext` / `systemMessage` context
   on any CLI. Compact/resume/PostCompact set pending variables; first
   context-capable event injects.
3. **Any-event materialize.** Idle SessionStart(startup) creates no row.
   The first hook for that `external_id` other than `SESSION_END` and
   `NOTIFICATION` creates the row (thin `register_session` under the lookup
   lock) and HookManager runs the activation tail outside the lock. UPS is
   the common case; PreToolUse / Stop / PostCompact also count. SESSION_END
   without a row still skips. **NOTIFICATION is excluded unconditionally**
   (idle-rows invariant). The 3.1 Claude/Grok notification-timing probe is
   observational only: its result is recorded in the leaf's validation
   evidence and never changes this exclusion or any acceptance outcome.
   Concurrent first hooks: see decision 7.
4. **Grok briefing vs turn-context — no Stop-flush loop.**
   - **Briefing-class** (`grok_pending_briefing`): first-prompt startup
     packet; compact/clear continuation (skill-reload, MCP ledger, task
     context — #20724 P2 payload); wiki/profile/persona first-shot. Arm
     compact briefing from `handle_post_compact` on Grok (no SessionStart).
     Flush deny-once on the next PreToolUse. If that cycle has no tools,
     flush **once** on Stop as `additionalContext` (Grok continues once).
     `wiki_overview_injected`, `_startup_context_injected`, and compact
     one-shots are set **only on confirmed flush**, never on stash.
   - **Turn-context-class** (`grok_pending_turn_context`): brevity
     reminders, later memory recall, pressure nudges, tool-error recovery.
     Never deny solely to deliver. **Never Stop-flush by itself** — that
     plus turn_start re-arm is a continuation loop (`wants_continuation()`
     is true whenever `additional_context` is non-empty). Concatenate onto
     an already-blocking stop-gate only. If Stop is allowing, drop the
     remainder (debug log).
5. Grok Stop gates (`require-task-close`, `tool_block_pending`, …) emit
   `decision: "block"` with `continue: true`, never `deny`.
6. Stash Grok observe `response.context` in
   `HookManager._complete_response` via `gobby.hooks.grok_pending_context`
   **before** adapter translation, then clear `response.context` so
   `record_unsupported_response_fields` does not `dropped_field`-spam
   every UPS. Delete `GROK_SYSTEM_MESSAGE_CONTEXT_HOOKS`; do not keep an
   empty frozenset. Classifier (no content-sniffing):
   - `event.metadata["_session_just_materialized"]` → briefing
   - Compact continuation: arm `grok_pending_briefing` in
     `handle_post_compact`, **bypass stash**
   - Clear continuation: arm `grok_pending_briefing` in
     `_bind_clear_successor` (clear successor already has a row, so first
     UPS is not just-materialized)
   - Everything else that reaches stash → turn-context
7. **Activation race — independent idempotent completion, with two
   carved-out sub-protocols.** No claim, no wait, no timeout for the
   idempotent activation steps: every racer calls
   `activate_materialized_session` and runs it to completion before its own
   rule evaluation and handler proceed. Each activation step carries its own
   done-guard, so a concurrent double-run is a cheap no-op and no racer ever
   crosses a rule or tool boundary against a half-activated session.
   `_materialize_activation_done` is written on completion (idempotent); a
   crash mid-activation is healed by the next hook re-running the same
   guarded steps. Two pieces of first-activity work are not plain
   no-op-on-rerun and carry their own protocols: (a) startup-briefing
   staging (Grok) executes as an atomic enqueue-if-absent of a single
   component keyed by session and activation epoch, so racers deduplicate
   instead of double-enqueueing — 3.1 creates the structured-component
   enqueue-if-absent primitive in `grok_pending_context.py` and wires it as
   an activation step; 4.1 extends that module with stash/flush/claim/commit
   mechanics. (b) Copied SessionStart processing splits into a **stateful
   phase** and an **external-dispatch phase**. The stateful phase — rule
   predicate evaluation whose effects are arming-only variable writes and
   idempotent resets (3.1 converts every retained SessionStart inject
   producer to arming/state-only) — is idempotent, so every racer runs it to
   completion before its own live rules and handler, exactly like the
   activation steps: no claim, no wait. Non-idempotent external side
   effects split by whether their result gates the session. The
   **blocking-gate step** — `session_start` webhook endpoints registered as
   blocking — is a durable pre-live barrier: a single owner evaluates them
   and persists the decision, and **every** racer observes that durable
   result before its live rules and handler (3.1). The **async
   external-dispatch phase** — the `pipeline-auto-run` rule's background
   `run_pipeline` call and non-blocking webhook dispatch — is single-owner:
   a durable compare-and-swap claim with crash takeover. Where Gobby owns
   the consumer, dispatch carries a consumer-enforced idempotency key
   (`run_pipeline`; the pipeline-execution store rejects a takeover
   duplicate). Webhook endpoints are external observers Gobby cannot
   deduplicate for: their dispatch is **at-least-once** — exactly once in
   crash-free operation, a possible duplicate only across crash takeover —
   and every webhook payload carries a stable delivery key (session id +
   activation epoch) so receivers can deduplicate. A racer that loses the
   async-phase claim skips external dispatch and proceeds with its own live
   event; it never waits on the async phase. Injects stay gated by
   delivered-state vars, not by `_materialize_activation_done`.

Non-goals: changing Grok itself; MCP `call_tool` result injection (the
#20635 design); Codex app-server follow-up messages; AGY PreInvocation
beyond using the shared materialize path.

## P1: Grok wire contract
`kind: framing`

**Goal**: Gobby's Grok adapter and capability table match Grok 1.0.5.

### 1.1 Align Grok capabilities and Stop translation [category: code]
`kind: deliverable`

Targets:
- `src/gobby/adapters/capabilities.py::*` — scope-reason: rewrite `_grok_capabilities` plus the top-level `GROK_ADDITIONAL_CONTEXT_HOOKS` constant and delete `GROK_SYSTEM_MESSAGE_CONTEXT_HOOKS`
- `src/gobby/adapters/grok.py::GrokAdapter.translate_from_hook_response`
- `docs/guides/adapter-fidelity.md`
- `tests/adapters/test_acp_hook_translation.py::*` — scope-reason: flip Grok Stop expectations and add observe-hook no-context cases across classes
- `tests/adapters/test_capabilities.py::*` — scope-reason: Grok capability-table expectations change across the file
- `src/gobby/servers/routes/mcp/hooks.py::*` — scope-reason: consumer of translate_from_hook_response; verify the route makes no Stop-deny assumptions
- `tests/hooks/test_context_limits.py::*` — scope-reason: consumer of translate_from_hook_response; truncation cases for Stop additionalContext may update

Delete `GROK_SYSTEM_MESSAGE_CONTEXT_HOOKS` (nothing else references it). Rewrite
`_grok_capabilities` so context channels match GateKind:

```python
GROK_ADDITIONAL_CONTEXT_HOOKS = frozenset({"stop", "subagent_stop"})
```

Do not edit `ACPHookAdapter.translate_from_hook_response`; Grok Stop is handled
in `GrokAdapter` only. Other ACP CLIs keep the existing `block` → `deny` map.

Per-event `decision_style`:

- `pre_tool_use`: `PRE_TOOL_USE` (deny / `updatedInput` only)
- `stop`, `subagent_stop`: `TOP_LEVEL_BLOCK` (Grok vocab `block`, not hard-stop `deny`)
- `session_start`, `user_prompt_submit`, `post_tool_use`, `post_tool_use_failure`,
  `pre_compact`, `post_compact`, `notification`, `permission_denied`,
  `stop_failure`, `subagent_start`, `session_end`: `NONE`, `ContextChannel.NONE`

`GrokAdapter.translate_from_hook_response` must handle `stop` the same way it
already handles `subagent_stop`:

```python
if canonical_hook in {"stop", "subagent_stop"} and response.decision in {"deny", "block"}:
    result = {
        "continue": True,
        "decision": "block",
        "reason": reason or "Blocked by Gobby hook",
    }
    if response.context:
        result["hookSpecificOutput"] = {
            "hookEventName": "Stop" if canonical_hook == "stop" else "SubagentStop",
            "additionalContext": truncate_context_for_adapter(...),
        }
    return result
```

There is no force-stop translation branch. `HookResponse` has no
continuation field, and `force_allow_stop` consumers produce an ordinary
`decision: "allow"` outcome, which translates to a plain allowing Stop.
Gobby never emits `{"continue": false, "stopReason": ...}`. Translation
tests cover precedence across allow, block-with-context, and SubagentStop.

Pending P2P messages are out of scope for this leaf: the Grok delivery
consumer, the `EventEnricher` routing change, and their tests live in 4.1,
which owns every Grok delivery path, so this leaf's acceptance is satisfiable
from this section alone. The interim state between this leaf and 4.1 is
explicit and acceptable: Grok pending messages stay queued and unclaimed.
Today they are marked delivered on observe channels whose stdout Grok
ignores — a false delivery this leaf stops; 4.1 installs the real consumer
and the delivered accounting.

Update the Grok row in `docs/guides/adapter-fidelity.md` (currently claims
`additionalContext` on `session_start`/`user_prompt_submit`/`post_tool_use`
and `systemMessage` on `pre_tool_use`/`pre_compact`/`stop`). The live
contract is: observe stdout ignored; PreToolUse deny/`updatedInput`; Stop
and SubagentStop `block` + `additionalContext`.

Flip `test_block_maps_to_deny_hard_stop` so Grok `stop` expects
`decision == "block"` and `continue is True`. Add cases: Stop with
`response.context` ships `hookSpecificOutput.additionalContext`; allowing
Stop stays a plain allow (no `continue: false`, no `stopReason`);
`user_prompt_submit` / `post_tool_use` with `response.context` do **not**
emit `additionalContext`.

**Acceptance:**

- 1.1.1 - Grok capability table lists additionalContext only on stop and subagent_stop. symbol: `_grok_capabilities`.
- 1.1.2 - Grok Stop block emits `decision: "block"` and `continue: true`. test: `tests/adapters/test_acp_hook_translation.py::TestBlockToDenyMapping.test_block_maps_to_deny_hard_stop`.
- 1.1.3 - Grok observe hooks with context do not emit additionalContext. test: `tests/adapters/test_acp_hook_translation.py`.
- 1.1.4 - Adapter-fidelity Grok row matches the 1.0.5 wire contract. file: `docs/guides/adapter-fidelity.md`.
- 1.1.5 - Grok system-message compatibility constant is removed and every passive Grok hook exposes ContextChannel.NONE with neither additionalContext nor systemMessage output. test: `tests/adapters/test_capabilities.py`.
- 1.1.6 - Grok PreToolUse emits only deny reason or updatedInput fields and never additionalContext or systemMessage. test: `tests/adapters/test_acp_hook_translation.py`.

## P2: Extract session materialization
`kind: framing`

**Goal**: `flow.py` shrinks; startup vs bind paths become callable from first prompt.

### 2.1 Split SessionStart flow and extract activation helpers [category: refactor]
`kind: deliverable`

Targets:
- `src/gobby/hooks/event_handlers/_session_start/materialize.py`
- `src/gobby/hooks/event_handlers/_session_start/flow.py::*` — scope-reason: handle_session_start loses register/activate body to materialize.py so the 934-line file can shrink
- `src/gobby/hooks/event_handlers/_session_start/__init__.py::SessionStartMixin.handle_session_start`
- `tests/hooks/test_session_start_handlers.py::*` — scope-reason: consumer of handle_session_start; patch/import seams update for symbols moved to materialize.py
- `tests/hooks/test_transcript_path_derivation.py::*` — scope-reason: consumer of handle_session_start; drives it for transcript derivation
- `tests/hooks/event_handlers/test_session_variable_preservation.py::*` — scope-reason: monkeypatches flow symbols that move to materialize.py; patch/import seams update
- `tests/hooks/test_session_handoff_handlers.py::*` — scope-reason: monkeypatches flow symbols that move to materialize.py; patch/import seams update

This leaf is a **behavior-preserving extraction**: after it lands, the
runtime behaves exactly as before — startup SessionStart still registers,
still activates, and still returns its current context. The
registration-timing cutover, response stripping, and deferral wiring all
land atomically in 3.1, so no committed state exists where SessionStart
behavior changed but first-hook activation does not yet exist.

Split `src/gobby/hooks/event_handlers/_session_start/flow.py` by moving
register/activate/seed into
`src/gobby/hooks/event_handlers/_session_start/materialize.py` so `flow.py`
stays under the 1,000-line ceiling.

Create `src/gobby/hooks/event_handlers/_session_start/materialize.py` with:

```python
STARTUP_SOURCES = frozenset({"startup", "new", ""})

def session_start_should_defer(event, existing_session, session_source: str) -> bool:
    """True when this SessionStart must not create a sessions row."""
    ...

def activate_materialized_session(handler, event, session_id: str) -> None:
    """Agent, wiki/profile/memory seeds, code index, transcript processor.

    Literal extraction of the current SessionStart activation body,
    behavior-identical. This leaf adds no guards, no markers, and no new
    state; it only relocates the code.
    """
```

This module owns **activation**, not row insert. `session_start_should_defer`
is introduced here with unit coverage but is **not consulted by the live path
in this leaf**: `handle_session_start` keeps identity resolution
(`resolve_session_start_identity`, compact/resume/pre-created / ACP-child
skips) and keeps calling `register_session` and the activation body exactly
as today, now through the extracted helpers. `_compose_session_response` is
untouched in this leaf. `activate_materialized_session` is a **literal
extraction** of the current activation body: this leaf introduces no per-step
guards, no idempotency machinery, and no completion marker —
`_materialize_activation_done`, the durable per-step guards, and the
required/best-effort classification are specified and implemented entirely
in 3.1, which rewrites the extracted body into that guarded idempotent form.
A 2.1-only implementer needs nothing from any downstream section.

`session_start_should_defer` is true iff all of:

- `session_source` in `STARTUP_SOURCES` (treat missing `source` as startup)
- no live pre-created / web-chat / `gobby_session_id` row
- not ACP-child / nested-CLI (those already return early)

3.1 wires it in: deferred startup SessionStart returning
`HookResponse(decision="allow")` with no `_platform_session_id` and no
`register_session` call, and `_compose_session_response` stripped of
SessionStart context, are 3.1 deliverables listed there.

After the split, `flow.py` must be under 850 lines (it is 934 now). The new
module stays well under the ceiling.

**Acceptance:**

- 2.1.1 - `session_start_should_defer` and `activate_materialized_session` exist with unit coverage; the live SessionStart path still registers and activates exactly as before the split. file: `src/gobby/hooks/event_handlers/_session_start/materialize.py`.
- 2.1.2 - `flow.py` is under 850 lines after the move. file: `src/gobby/hooks/event_handlers/_session_start/flow.py`.
- 2.1.3 - The existing SessionStart test surface passes unchanged (behavior-preserving proof), including ACP-child and pre-created paths. test: `tests/hooks/test_session_events_coverage.py`.

## P3: First-prompt materialization (all CLIs)
`kind: framing`

**Goal**: First UserPromptSubmit creates the session and carries former SessionStart context.

### 3.1 Materialize on first BEFORE_AGENT and inject startup context [category: code] (depends: 2.1)
`kind: deliverable`

Targets:
- `src/gobby/hooks/session_materialize.py`
- `src/gobby/hooks/event_handlers/_session_start/materialize.py`
- `src/gobby/hooks/event_handlers/_session_start/__init__.py::SessionStartMixin.handle_session_start`
- `src/gobby/hooks/event_handlers/_session_start/__init__.py::SessionStartMixin._compose_session_response`
- `src/gobby/hooks/hook_manager.py::HookManager._handle_after_daemon_ready`
- `src/gobby/hooks/session_lookup.py::SessionLookupService._resolve_uncached_session_id`
- `src/gobby/hooks/session_lookup.py::SessionLookupService._resolve_session_id`
- `src/gobby/hooks/event_handlers/_agent.py::AgentEventHandlerMixin.handle_before_agent`
- `src/gobby/hooks/event_handlers/_session_start/context.py::classify_session_start_context`
- `src/gobby/install/shared/workflows/rules/context-handoff/inject-compact-handoff.yaml::*` — scope-reason: keep session_start trigger while guaranteeing the pending flag is set without SessionStart context
- `src/gobby/install/shared/workflows/rules/context-handoff/inject-task-context-on-start.yaml::*` — scope-reason: fires via copied first-activity evaluation instead of CLI boot
- `src/gobby/install/shared/workflows/rules/context-handoff/inject-user-profile.yaml::*` — scope-reason: fires via copied first-activity evaluation instead of CLI boot
- `src/gobby/install/shared/workflows/rules/pipeline-enforcement/auto-run-pipeline.yaml::*` — scope-reason: fires via copied first-activity evaluation instead of CLI boot
- `tests/hooks/test_session_events_coverage.py::*` — scope-reason: SessionStart registration assertions change across the session-start test classes
- `tests/hooks/test_hooks_manager.py::*` — scope-reason: registration assertions move from SessionStart to first-hook materialization
- `tests/hooks/test_hook_manager.py::*` — scope-reason: consumer of _handle_after_daemon_ready; materialization ordering asserts change
- `src/gobby/hooks/event_handlers/__init__.py::*` — scope-reason: composes handle_before_agent into EventHandlers; verify no boot-time register path remains
- `tests/hooks/test_agent_events_coverage.py::*` — scope-reason: consumer of handle_before_agent; first-activity injection asserts change
- `tests/hooks/test_session_start_handlers.py::*` — scope-reason: consumer of classify_session_start_context; classification cases extend for deferral
- `src/gobby/workflows/reserved_variables.py::*` — scope-reason: reserve the `_materialize_activation_done`, `_copied_session_start_state`, and `_deferred_materialization` control markers this leaf introduces
- `tests/mcp_proxy/test_top_level_variables.py::*` — scope-reason: set_variable rejection cases for the three reserved control markers
- `src/gobby/install/bundled_content_manifest.json::*` — scope-reason: manifest digests refresh for this leaf's compact-handoff template change
- `tests/install/test_bundled_content_manifest.py::*` — scope-reason: tree-equality regression after this leaf's template change
- `tests/workflows/test_context_handoff_rules.py::*` — scope-reason: compact arming/delivery split cases for non-Grok CLIs
- `tests/servers/routes/test_session_variables.py::*` — scope-reason: prove HTTP set_variable rejects runtime-reserved materialization markers
- `tests/workflows/test_hooks.py::*` — scope-reason: prove non-internal set_variable rule effects reject runtime-reserved materialization markers
- `src/gobby/hooks/grok_pending_context.py`
- `src/gobby/storage/sessions/_manager.py::SessionManager.register_session`
- `tests/storage/sessions/test_storage_sessions_registration.py::*` — scope-reason: deferred-materialization discriminator persists atomically with the row insert
- `src/gobby/install/shared/workflows/rules/context-handoff/clear-pending-context-reset-on-start.yaml::*` — scope-reason: pending_context_reset clearing moves from SessionStart to the confirmed delivery owner
- `tests/hooks/test_webhooks.py::*` — scope-reason: session_start webhook replay-at-first-activity policy cases
- `tests/hooks/test_session_activation_reconciliation.py::*` — scope-reason: prove reconciliation linearizes before copied stateful evaluation and live rules
- `src/gobby/mcp_proxy/tools/workflows/_pipeline_execution.py::run_pipeline`
- `src/gobby/mcp_proxy/tools/workflows/_pipelines.py::*` — scope-reason: consumer wrapper of run_pipeline; passes the idempotency key through
- `src/gobby/runner_lifecycle_subsystems.py::*` — scope-reason: consumer registering run_pipeline; signature registration follows the new argument
- `tests/events/test_mcp_tool_changes.py::*` — scope-reason: consumer of run_pipeline; tool-change expectations follow the signature change
- `tests/mcp_proxy/tools/workflows/test_mcp_proxy_tools_workflows_pipelines.py::*` — scope-reason: idempotency-key dedup cases for duplicate copied-effect dispatch
- `tests/storage/test_sessions_import.py::*` — scope-reason: consumer of register_session; import seams follow the initial-variable seed
- `src/gobby/mcp_proxy/tools/workflows/_pipeline_execution.py::PipelineExecutionManager.create_execution`
- `src/gobby/storage/pipeline_executions.py::PipelineExecutionStorageMixin.create_execution`
- `src/gobby/workflows/pipeline_state.py::*` — scope-reason: `PipelineExecution` gains an optional `idempotency_key` field with a `None` default; construction and read sites elsewhere are unaffected by the additive field
- `crates/gcore/assets/schema/migrations/405_pipeline_execution_idempotency_key.sql`
- `crates/gcore/src/schema/assets.rs::*` — scope-reason: embed migration 405 and checksum for pipeline execution idempotency
- `crates/gcore/assets/schema/catalog.manifest.json::*` — scope-reason: regenerated catalog manifest carries the migration 405 entry and digest
- `crates/gcore/src/grant/bundle.rs::*` — scope-reason: derived schema-bundle carrier refreshes with migration 405
- `crates/gcore/tests/schema_contract.rs::*` — scope-reason: schema contract test tracks the new migration and catalog digest
- `crates/gdaemon/tests/cli_contract.rs::*` — scope-reason: daemon CLI contract test tracks the regenerated schema identity
- `src/gobby/storage/schema_expected_identity.json::*` — scope-reason: regenerated expected schema identity after migration 405
- `tests/storage/test_pipeline_storage.py::*` — scope-reason: concurrent idempotency-key uniqueness and existing-run reuse cover storage behavior
- `src/gobby/storage/sessions/_crud.py::_SessionCRUDMixin.register`
- `src/gobby/hooks/session_types.py::HookSessionManager.register_session`
- `src/gobby/hooks/session_coordinator.py::*` — scope-reason: consumer of HookSessionManager.register_session; call sites follow the typed-outcome signature and initial-variable seed
- `tests/hooks/test_session_lookup_metadata.py::*` — scope-reason: consumer of register_session; typed-outcome expectations update
- `crates/gcore/src/schema/runner_tests.rs::*` — scope-reason: advance embedded migration inventory/count and latest-migration assertions through 405
- `crates/gcore/src/grant/tests.rs::expected_schema_identity_tracks_catalog_head`
- `tests/runtime_grants/golden/brokered_datastores.json::*` — scope-reason: positive signed golden regenerated for schema identity 405
- `tests/runtime_grants/golden/direct_datastores.json::*` — scope-reason: positive signed golden regenerated for schema identity 405
- `tests/runtime_grants/golden/old_client_new_grant.json::*` — scope-reason: positive signed golden regenerated for schema identity 405
- `tests/runtime_grants/golden/unavailable_datastores.json::*` — scope-reason: positive signed golden regenerated for schema identity 405

Split `src/gobby/hooks/hook_manager.py` by moving first-hook activation and
copied session-start rule evaluation into
`src/gobby/hooks/session_materialize.py` so `hook_manager.py` stays under the
1,000-line ceiling.

Keep thin `register_session` inside `_resolve_uncached_session_id` (lookup
lock). On create, set `event.metadata["_session_just_materialized"] = True`.
The `_deferred_materialization` discriminator is **persisted in the same
storage write as the row insert**, never stamped afterwards:
`SessionManager.register_session` gains an initial-variable seed so the
deferred-path marker exists if and only if the row exists. A crash at any
point after `register_session` returns therefore leaves a row that recovery
classifies unambiguously — deferred rows carry the marker from birth,
pre-deploy rows never do — and no crash window can make a new deferred row
look like a pre-deploy row and skip copied SessionStart processing forever.
Do not call activation from the lookup service.

This leaf performs the atomic cutover that 2.1 deliberately defers: wire
`session_start_should_defer` into `handle_session_start` (deferred startup
returns `HookResponse(decision="allow")` with no `_platform_session_id` and
no `register_session` call; `HookManager._handle_after_daemon_ready` already
returns before session-start rules when `_platform_session_id` is absent —
keep that), and strip `_compose_session_response` so SessionStart never
emits context or system-message fields on any path (startup, compact,
resume, clear, web-chat, pre-created — session banner and claimed-task lines
move to first-activity composition; compact/resume SessionStart may still
mutate session variables). Registration timing, after-lookup activation, and
copied SessionStart evaluation all land in this single leaf, so no committed
state exists where SessionStart is deferred but first-hook activation is
missing.

After `resolve()` returns, `HookManager._handle_after_daemon_ready` calls
`session_materialize.activate_if_needed(handler, event)` **outside** the
lock. Every racer observes this exact linearization before its own live
rules and handler run:

1. **Materialization activation.** If `_session_just_materialized` (or a row
   exists but `_materialize_activation_done` is unset — including pre-deploy
   rows), run `activate_materialized_session`. Idempotent no-op if already
   done.
2. **Activation reconciliation.** Existing `reconcile_session_activation`
   runs (idempotent) so copied evaluation and live rules observe the
   resolved agent/workflow state, never a pre-reconciliation snapshot.
3. **Copied SessionStart processing — stateful phase.** If this is the first
   activity cycle, evaluate session-start rules on a **copied** event whose
   type is `SESSION_START` (synthetic schema below). Stateful effects —
   arming-only variable writes and idempotent resets (plan-mode, discovery,
   skill, task tracking) — are idempotent, so every racer runs this phase to
   completion itself; no claim, no wait. One-shot inject contributions to
   the racer's own live response are claimed by an atomic test-and-set on
   their delivered-state variable inside `_mutate_variables`, so concurrent
   racers yield exactly one composer per one-shot. The live event keeps its
   original type so turn_start rules still see `BEFORE_AGENT`.
4. **Copied SessionStart processing — blocking-gate step.** Every racer
   observes the durable blocking-webhook decision (barrier semantics below)
   before its live rules and handler.
5. **Copied SessionStart processing — async external-dispatch phase**
   (single-owner, may complete asynchronously), then the normal handler +
   rules for the live event.

A racer never crosses into step 3 before steps 1–2 complete in its own call
stack, and never runs live rules or its handler before steps 3–4 complete,
so a claim loser cannot observe half-applied resets, pre-reconciliation
state, or an unresolved blocking gate.

UPS+PreToolUse racing: no claim, no wait — each racer runs
`activate_materialized_session` to completion before its own copied-rule
evaluation and live-event handling proceed (decision 7). Per-step done-guards
make the double-run a cheap no-op, so neither racer crosses a rule or tool
boundary against a half-activated session. `_materialize_activation_done` is
written on completion (idempotent); a crash mid-activation is healed by the
next hook re-running the same guarded steps. Startup injects are gated by
delivered-state vars, not by a wait.

Startup-briefing staging (Grok) is itself an activation step and completes
before any racer's rules or tool handling: each racer performs an atomic
enqueue-if-absent of the single startup-briefing component keyed by session
and activation epoch. **This leaf creates the primitive it needs**:
`src/gobby/hooks/grok_pending_context.py` is created here with the
structured-component model (stable `component_id`, class, payload/source
reference, ack mutations) and the atomic `enqueue_if_absent` operation over
`SessionVariableManager._mutate_variables`, and `session_materialize.py`
wires it as an activation step — so 3.1 is implementable and testable with
no forward dependency. `enqueue_if_absent` deduplicates against **queued,
claimed, and committed** identities in that same transaction: the buffer
holds queued and claimed components, and a per-epoch **committed-identity
set** (the tombstone 4.1's commit writes when it removes a component)
records what already delivered — so a stale racer whose guard passed before
another request committed and removed the component no-ops instead of
re-enqueueing the same activation-epoch identity and causing a second
briefing denial. 4.1 extends the same module with stash, flush, claim, and
commit mechanics. An interleaved schedule — UPS creates the row first,
PreToolUse finishes activation first, UPS stashes last — still yields
exactly one staged component; 4.1 owns flush and commit, so the deny-once
delivery and commit-before-late-stash acceptance stay there while the
exactly-one-staged-component acceptance lives here.

`activate_materialized_session` classifies every step as **required** or
**best-effort**, each behind its own durable guard:

- Required: session-row registration (performed by the lookup layer; a
  failed or empty registration means there is no session to activate) and
  session-variable seeding (identity and delivered-state seeds).
- Best-effort: agent resolution (`None` is a valid absence; only an
  exception is a failure), code-index setup, wiki seeding, profile
  injection, transcript-processor setup. A best-effort failure is logged,
  its guard marks complete, and activation proceeds.

Registration failure is **typed, never shape-collapsed**: session
lookup/registration returns a typed outcome — `materialized` (row exists or
was created), `excluded` (SESSION_END, NOTIFICATION, missing project,
ACP-child: legitimate absence), or `failed` (unrecoverable registration
error) — instead of the empty-ID shape that today serves both absence and
failure. `HookManager` consumes that outcome before copied processing,
webhooks, live rules, and handlers: `excluded` follows the ordinary
sessionless path, while `failed` triggers the per-class outcomes below, so
a failed first PreToolUse can never ride the sessionless allow path. Tests
cover `failed` separately from each exclusion class.

A required-step failure leaves that guard incomplete, never writes
`_materialize_activation_done`, skips copied SessionStart processing and
startup injects (they stay pending), and keeps all state retryable — the
next hook re-runs the guarded steps. The **current-hook outcome is
per-class**, because fail-open on a gating hook would let a tool cross the
half-activated state this plan forbids:

- **Passive hooks** (UserPromptSubmit, PostToolUse, PostCompact, and every
  other observe event): fail-open allow, matching the existing daemon-error
  contract.
- **PreToolUse**: a **retriable deny** — `decision: "deny"` with a reason
  stating startup activation is incomplete and the same call should be
  retried. The tool never executes before required startup state exists;
  the retry (or any next hook) re-runs activation and proceeds normally on
  success.
- **Stop / SubagentStop**: fail-open allow — blocking a stop cannot repair
  activation and would force an unactivated session to keep running. No
  delivery happens, so no ack mutation can fire; owed briefing state stays
  pending for the next cycle.

`_materialize_activation_done` is written only when every required guard is
complete and every best-effort guard is complete or logged-failed. Tests
cover the same-event failure outcome for each class and next-hook recovery.

The Claude/Grok Notification-timing probe is **observational only**: record
what it shows in the leaf's validation evidence. The NOTIFICATION exclusion
is unconditional (decision 3); no probe result changes normative behavior or
any acceptance outcome.

Reserve `_materialize_activation_done`, `_copied_session_start_state`, and
`_deferred_materialization` in `src/gobby/workflows/reserved_variables.py`
alongside the 4.1 Grok buffers. They are runtime-owned control markers:
public `set_variable` (MCP and HTTP) and non-internal rule effects must
reject writes to them, otherwise a stray write suppresses activation or
replays non-idempotent session-start effects.

The copied session-start evaluation uses an explicit synthetic SessionStart
schema, never a re-typed prompt/tool envelope: `event_type = SESSION_START`,
`data = {"source": "startup"}` plus the deferred identity fields
(external_id, cwd, transcript path), and **no** live prompt or tool payload.
Startup-sensitive predicates must behave exactly as on a real startup
SessionStart — `reset-plan-mode-on-session-start` requires
`event.data['source'] == 'startup'` and silently stops firing without it.
Audit every installed SessionStart lifecycle rule plus one custom
session-start rule against the synthetic payload.

Copied SessionStart processing carries **durable state in
`_copied_session_start_state`**, separate from activation, with the three
phases from the linearization above tracked independently:

- **Stateful phase** (idempotent arming/reset effects): every racer runs it
  to completion; a `stateful: done` field is written afterwards so later
  hooks skip re-evaluation. A crash before that write is harmless — the next
  hook re-runs the idempotent phase. Activation completion never implies
  copied-processing completion.
- **Blocking-gate step** (blocking `session_start` webhook endpoints): a
  `blocking` substate moves `pending → running → done` plus the **durable
  decision** (allow, or block with reason). `pending → running` is an
  atomic compare-and-swap claim carrying owner id and timestamp; the owner
  runs `evaluate_blocking_webhooks` on the synthetic event and writes the
  decision with `done` in one mutation. Every racer — owner or not — must
  observe `done` before its live rules and handler: a non-owner waits with
  a bounded poll, and a stale `running` past the takeover threshold is
  taken over (re-evaluation is the at-least-once duplicate documented
  below). If the bound expires unresolved, the racer applies the per-class
  required-failure outcome (passive and Stop fail open, PreToolUse
  retriable deny). A durable `block` decision gates **every** racer's live
  event exactly as a live SessionStart block would — a losing first
  PreToolUse cannot execute its tool while the copied SessionStart is
  blocked.
- **Async external-dispatch phase** (fire-and-forget side effects: the
  `pipeline-auto-run` rule's background `run_pipeline` mcp_call, plus
  non-blocking `session_start` webhook dispatch via
  `dispatch_webhooks_async`): `external` moves `pending → running → done`
  under the same CAS-claim/takeover contract; losers skip and never wait.
  A claim alone cannot make dispatch exactly-once across a crash between
  dispatch and `done`, so where Gobby owns the consumer the dispatch
  carries a **durable consumer-enforced idempotency key** — for
  `pipeline-auto-run`, `run_pipeline` gains an `idempotency_key` argument
  (key: session id + rule id + activation epoch) and the
  pipeline-execution store enforces uniqueness on it, returning the
  existing run instead of starting a duplicate. Webhook endpoints are
  external observers Gobby cannot deduplicate for: their dispatch —
  blocking re-evaluation after takeover included — is **at-least-once**,
  exactly once in crash-free operation, with a stable delivery key
  (session id + activation epoch) in every payload for receiver-side
  dedupe. Marking `done` only after a successful dispatch call cannot lose
  a failed dispatch. Crash-recovery tests cover crash-before-dispatch
  (takeover dispatches once), crash-after-dispatch (pipeline redispatch
  consumer-rejected; webhook duplicate carries the same delivery key), and
  dispatch failure (state stays retryable).

Auditing the installed SessionStart rule inventory to classify each effect
as stateful, blocking-gate, or async-external is part of this leaf.

Pre-deploy rows: only rows the deferred path created (the
`_deferred_materialization` marker persisted at row creation) are candidates
for copied SessionStart processing. A row predating this deploy already
received a real SessionStart evaluation at CLI boot; treat it as complete
and never replay resets, webhooks, or pipelines against it. Idempotent
activation seeds may still heal such rows harmlessly.

**SessionStart webhook policy: parity at first activity.** Deferring the
lifecycle event covers webhooks, not just rules. A sessionless startup
SessionStart dispatches **no** `session_start` webhooks — there is no
session identity to report. The copied first-activity processing dispatches
them on the canonical synthetic SessionStart event: blocking endpoints run
in the blocking-gate step via `evaluate_blocking_webhooks`, and the durable
decision gates **every** racer's live event exactly as it would gate a live
SessionStart response today; non-blocking endpoints fire via
`dispatch_webhooks_async` inside the async external-dispatch phase. The
relative order between copied rules and webhook dispatch matches today's
live SessionStart order. Delivery is **at-least-once**: exactly one
dispatch in crash-free operation via the CAS claims, a duplicate only
across crash takeover, and every payload carries the stable
session-id + activation-epoch delivery key so receivers can deduplicate —
Gobby never claims exactly-once for consumers it does not own. Compact,
resume, clear, and pre-created SessionStart paths keep dispatching their
webhooks on the live event, unchanged. Non-Grok and Grok behave
identically; `tests/hooks/test_webhooks.py` proves the sessionless-skip,
crash-free-single-dispatch, delivery-key, and blocking-gates-every-racer
cases.

**SessionStart output barrier.** Stripping `_compose_session_response` is
necessary but not sufficient: rule-generated workflow context merges into
the response after handler composition. This leaf adds an explicit barrier
at the single point where a live `SESSION_START` response leaves hook
processing — after handler composition **and** rule-evaluation merge — that
strips context and system-message fields for every SESSION_START path. Every
retained live-SessionStart inject producer (`inject-compact-handoff`,
`inject-task-context-on-start`, `inject-user-profile`, wiki overview,
persona) converts to arming/state-only behavior on SESSION_START; their
payloads deliver through the first-activity composition (non-Grok) or the
Grok briefing buffer (4.1). State-clearing follows delivery, not arming:
`clear-pending-context-reset-on-start` stops clearing
`pending_context_reset` on SessionStart — the confirmed delivery owner (the
non-Grok turn_start delivery rule; Grok component commit in 4.1) clears it
together with the fields it delivered, so an empty compact SessionStart can
no longer strand part of the continuation.

Session-start YAML inject rules keep `event: session_start` and run via the
copied evaluation at first activity, not at CLI boot. Compact SessionStart
(existing row, Claude) still evaluates them; Grok compact uses PostCompact
instead (4.1).

Compact handoff splits into **arming** and **delivery** phases. The bundled
`inject-compact-handoff` session_start rule becomes arming-only: it sets
`compact_handoff_inject_pending` and **retains** `handoff_summary_injectable`
plus the required-skill and advisory-skill lists — it no longer renders the
continuation or clears any of those fields, and SessionStart emits no
context. `inject-compact-handoff-on-prompt` (turn_start, non-Grok) becomes
the sole non-Grok delivery point: it renders the complete continuation —
summary plus the skill-reload content that today renders at SessionStart —
and atomically clears exactly the fields it included. Grok delivery stays
owned by PostCompact briefing arming (4.1). This leaf edits the
`inject-compact-handoff.yaml` template accordingly and refreshes
`src/gobby/install/bundled_content_manifest.json`; the tree-equality
regression proves the manifest matches the committed templates after this
leaf alone, without waiting for 4.1.

Update `test_handle_session_start_basic`: startup SessionStart must
**not** call `register_session`. Add
`test_first_user_prompt_submit_registers_session` (SessionStart then
BEFORE_AGENT → one `register_session` on the UPS) and
`test_first_pre_tool_use_without_ups_registers_session` (any-event
fallback).

Update `tests/hooks/test_hooks_manager.py` assertions that today require
`register_session` on SessionStart.

Do not add SessionStart additionalContext back in `ACPHookAdapter` /
`ClaudeCodeAdapter`.

**Acceptance:**

- 3.1.1 - Startup SessionStart does not create a sessions row. test: `tests/hooks/test_session_events_coverage.py::TestSessionStartAndHelpers::test_handle_session_start_basic`.
- 3.1.2 - First BEFORE_AGENT creates the row; activation runs outside the lookup lock. file: `src/gobby/hooks/session_materialize.py`.
- 3.1.3 - Compact/pre-created SessionStart still binds the existing row. test: `tests/hooks/test_session_events_coverage.py::TestSessionStartAndHelpers::test_handle_session_start_pre_created`.
- 3.1.4 - First PreToolUse without UPS also materializes. test: `tests/hooks/test_hooks_manager.py`.
- 3.1.5 - `hook_manager.py` does not grow; first-prompt orchestration lives in `session_materialize.py`. file: `src/gobby/hooks/session_materialize.py`.
- 3.1.6 - Notification does not materialize an idle session; the exclusion is unconditional and the recorded Claude/Grok probe is observational evidence only. test: `tests/hooks/test_hooks_manager.py`.
- 3.1.7 - A parameterized first-hook matrix materializes every supported non-SessionStart event except SESSION_END and NOTIFICATION, and both exclusions leave an idle startup row absent. test: `tests/hooks/test_hooks_manager.py::test_first_hook_materialization_matrix`.
- 3.1.8 - Copied evaluation uses the synthetic SessionStart schema (`data.source == 'startup'`, deferred identity fields, no prompt/tool payload); every installed SessionStart lifecycle rule plus one custom session-start rule fires identically to a real startup SessionStart. test: `tests/hooks/test_hooks_manager.py`.
- 3.1.9 - `_materialize_activation_done`, `_copied_session_start_state`, and `_deferred_materialization` are reserved; MCP `set_variable` and non-internal rule effects reject writes to them. test: `tests/mcp_proxy/test_top_level_variables.py`.
- 3.1.10 - Concurrent first UPS and PreToolUse both run activation to completion idempotently, and a simulated crash mid-activation is healed by the next hook. test: `tests/hooks/test_hooks_manager.py`.
- 3.1.11 - Every startup, compact, resume, clear, web-chat, and pre-created SessionStart response has empty context and system-message fields. test: `tests/hooks/test_session_events_coverage.py::test_session_start_never_emits_context`.
- 3.1.12 - Copied SessionStart processing follows the phase contract under concurrent first hooks: every racer completes the stateful phase and observes the durable blocking-gate decision before its live rules and handler, in activation → reconciliation → copied state → blocking gate → live evaluation order; the blocking-gate and async external-dispatch claims each have a single owner with crash takeover; a crash between activation completion and copied processing is recovered by the next hook; pre-deploy rows never replay resets, webhooks, or pipelines. test: `tests/hooks/test_hooks_manager.py`.
- 3.1.13 - Transient failures in registration, agent resolution, variable seeding, and transcript setup follow the required/best-effort classification with per-class current-hook outcomes: passive hooks and Stop fail open, PreToolUse returns a retriable deny, no copied processing or startup injects run, and the next hook (or retried call) recovers; best-effort failures log and let activation complete; the typed registration outcome distinguishes `failed` from every exclusion class (SESSION_END, NOTIFICATION, missing project, ACP-child), and a failed first PreToolUse never follows the sessionless allow path. test: `tests/hooks/test_hooks_manager.py`.
- 3.1.14 - Non-Grok compact: SessionStart emits nothing and arms pending state retaining the summary and skill lists; the first turn_start renders the complete continuation including skill reloads and clears only the fields it included. test: `tests/workflows/test_context_handoff_rules.py`.
- 3.1.15 - Bundled-content manifest exactly matches the committed shared-template tree after this leaf's template change. test: `tests/install/test_bundled_content_manifest.py::test_bundled_content_manifest_matches_tree`.
- 3.1.16 - Compact, resume, clear, web-chat, and pre-created SessionStart paths each bind or create the intended canonical row without duplication. test: `tests/hooks/test_session_events_coverage.py::test_session_start_binding_matrix`.
- 3.1.17 - HTTP session-variable writes reject every runtime-reserved materialization marker. test: `tests/servers/routes/test_session_variables.py`.
- 3.1.18 - Non-internal workflow set_variable effects reject every runtime-reserved materialization marker. test: `tests/workflows/test_hooks.py`.
- 3.1.19 - The structured-component `enqueue_if_absent` primitive exists in `grok_pending_context.py`, is wired as an activation step, and an interleaved first UPS/PreToolUse stages exactly one startup-briefing component. test: `tests/hooks/test_hooks_manager.py`.
- 3.1.20 - A sessionless startup SessionStart dispatches no session_start webhooks; first-activity copied processing dispatches blocking webhooks through the durable blocking-gate step and non-blocking webhooks through the async external-dispatch claim — exactly once in crash-free operation, at-least-once with the stable session-id + activation-epoch delivery key across crash takeover — and a durable blocking decision gates every racer's live event, proven by a two-racer test where the claim loser's PreToolUse cannot execute while the gate is blocked. test: `tests/hooks/test_webhooks.py`.
- 3.1.21 - The SessionStart output barrier strips handler- and rule-merged context on every SESSION_START path, and `pending_context_reset` survives an empty compact SessionStart, cleared only by the confirmed delivery owner. test: `tests/workflows/test_context_handoff_rules.py`.
- 3.1.22 - The `_deferred_materialization` discriminator persists in the same storage write as row creation; a simulated crash immediately after `register_session` leaves a row recovery classifies as deferred, never as pre-deploy. test: `tests/storage/sessions/test_storage_sessions_registration.py`.
- 3.1.23 - `run_pipeline` rejects a duplicate dispatch bearing the same idempotency key and returns the existing run; a crash-takeover redispatch yields exactly one pipeline execution. test: `tests/mcp_proxy/tools/workflows/test_mcp_proxy_tools_workflows_pipelines.py`.
- 3.1.24 - A parameterized provider matrix delivers first-activity startup context through the expected native channel for Claude, Qwen, Droid, and Codex, exercises the supported AGY shared-materialization path, and retains Grok as the pending-briefing exception. test: `tests/hooks/test_hooks_manager.py::test_first_activity_startup_context_provider_matrix`.
- 3.1.25 - Concurrent pipeline execution creation with one idempotency key persists exactly one execution and every caller receives that existing execution across process restart. test: `tests/storage/test_pipeline_storage.py::test_create_execution_idempotency_key_is_concurrent_and_durable`.
- 3.1.26 - The deferred discriminator is inserted inside the low-level fresh-row transaction while unique-conflict recovery and existing-row reuse never relabel pre-deploy rows. test: `tests/storage/sessions/test_storage_sessions_registration.py::test_deferred_seed_is_atomic_across_insert_conflict_and_reuse`.
- 3.1.27 - Embedded migration inventory and grant schema-head assertions advance through migration 405. test: `crates/gcore/src/schema/runner_tests.rs`.
- 3.1.28 - Every positive runtime-grant golden is regenerated and re-signed for schema identity 405 while the intentional skew golden remains negative. test: `tests/runtime_grants/test_golden_vectors.py::test_config_revision_signed`.

## P4: Grok pending-context flush
`kind: framing`

**Goal**: Grok models actually see startup and per-turn injects.

### 4.1 Stash observe context and flush briefing without a Stop loop [category: code] (depends: 1.1, 2.1, 3.1)
`kind: deliverable`

Targets:
- `src/gobby/hooks/grok_pending_context.py`
- `src/gobby/hooks/hook_manager.py::HookManager._complete_response`
- `src/gobby/hooks/event_enrichment.py::*` — scope-reason: Grok pending-message routing leaves messages unclaimed until confirmed delivery; `mark_delivered_batch` moves into component ack mutations
- `src/gobby/adapters/acp_hook_adapter.py::ACPHookAdapter.handle_native` — commit boundary: claimed components commit only after Grok translation succeeds here
- `tests/adapters/test_acp_hook_integration.py::*` — scope-reason: handle_native consumer; commit-boundary integration cases extend the file
- `src/gobby/servers/routes/mcp/hooks.py::*` — scope-reason: `_run_adapter_hook` executor-timeout path must atomically release claims so a late worker cannot commit
- `src/gobby/hooks/event_handlers/_misc.py::MiscEventHandlerMixin.handle_post_compact`
- `src/gobby/hooks/event_handlers/_session_start/flow.py::*` — scope-reason: arm clear-successor briefing inside _bind_clear_successor
- `src/gobby/hooks/event_handlers/__init__.py::*` — scope-reason: composes handle_post_compact into EventHandlers; Grok briefing arming reaches it through the mixin
- `src/gobby/workflows/reserved_variables.py::*` — scope-reason: reserve grok_pending_briefing and grok_pending_turn_context
- `src/gobby/install/shared/workflows/rules/context-handoff/inject-compact-handoff.yaml::*` — scope-reason: exclude Grok from inject-compact-handoff-on-prompt turn_start delivery; PostCompact briefing owns Grok
- `src/gobby/install/shared/workflows/rules/context-handoff/inject-clear-handoff.yaml::*` — scope-reason: exclude Grok from inject-clear-handoff-on-prompt; clear-successor briefing owns Grok
- `src/gobby/install/bundled_content_manifest.json::*` — scope-reason: manifest digests refresh for both changed context-handoff rule templates
- `docs/contracts/session-boundary.md`
- `docs/guides/sessions.md`
- `docs/guides/variables.md`
- `tests/workflows/test_context_handoff_rules.py::*` — scope-reason: one-owner proof; Grok exclusion cases for both turn_start handoff rules
- `tests/hooks/test_misc_handlers.py::*` — scope-reason: direct handle_post_compact tests gain Grok briefing-arming cases
- `tests/hooks/test_session_handoff_handlers.py::*` — scope-reason: direct clear-successor tests gain briefing-arming cases
- `tests/hooks/test_hook_manager.py::*` — scope-reason: _complete_response stash/flush integration asserts, including preserve_original gates
- `tests/hooks/test_pending_message_provider_contracts.py::*` — scope-reason: provider contract cases for queued-until-confirmed-delivery and commit-time delivered accounting
- `tests/hooks/test_grok_pending_context.py`
- `tests/install/test_bundled_content_manifest.py::*` — scope-reason: validate regenerated bundled-content digests against the committed shared-template tree
- `src/gobby/workflows/engine/delivery_formatting.py::finalize_staged_memory_delivery`
- `tests/workflows/test_delivery_pipeline.py::*` — scope-reason: consumer of finalize_staged_memory_delivery; Grok commit-time recording cases update
- `src/gobby/hooks/event_handlers/_agent.py::AgentEventHandlerMixin._inject_agent_instructions_if_needed`
- `src/gobby/install/shared/workflows/rules/context-handoff/inject-wiki-overview.yaml::*` — scope-reason: defer Grok wiki delivered-state mutation until confirmed component commit
- `src/gobby/workflows/engine/effects.py::*` — scope-reason: route delivered-marker effects through the extracted deferred-ack seam; the extraction itself lands in the new module below
- `src/gobby/workflows/engine/delivery_ack_effects.py`
- `tests/hooks/test_event_enrichment.py::*` — scope-reason: update piggyback membership, enqueue-without-ack, formatting failure, and retained non-Grok delivery cases
- `tests/servers/test_mcp_routes.py::*` — scope-reason: cover the real hook executor timeout and fallback boundary for claim release
- `tests/workflows/test_hook_evaluation_timeout.py::*` — scope-reason: preserve bounded-worker and timeout recovery behavior while adding Grok claims

Split Grok stash/flush out of `src/gobby/hooks/hook_manager.py` into
`src/gobby/hooks/grok_pending_context.py` so `hook_manager.py` does not grow
across the 1,000-line ceiling. That module is created in 3.1 with the
structured-component model and the `enqueue_if_absent` primitive; this leaf
**extends** it with the stash, flush, claim, and commit mechanics.

The delivered-marker effect seam splits out of
`src/gobby/workflows/engine/effects.py` the same way: move the Grok
deferred-ack effect handling into a new
`src/gobby/workflows/engine/delivery_ack_effects.py` so `effects.py` (897
lines) does not grow across the ceiling.

`flow.py` is near the ceiling too: put the briefing build/store helper in
`src/gobby/hooks/grok_pending_context.py` so 4.1 adds only a call
inside `_bind_clear_successor`; the `flow.py` body split itself (register/
activate into `materialize.py`) is 2.1's deliverable.

There is no `before_tool` / `_tool.py` flush site. Stash and flush both live
in `gobby.hooks.grok_pending_context`, invoked from
`HookManager._complete_response` (stash before adapter translate; flush when
the native hook is PreToolUse or Stop).

`stash(event, response)` runs when `source == GROK`, the hook is observe-only,
and `response.context` is non-empty. Classify with metadata flags, never by
sniffing the merged string:

- `event.metadata["_session_just_materialized"]` → `grok_pending_briefing`
- else → append `grok_pending_turn_context`

Then clear `response.context` so `record_unsupported_response_fields` does
not emit `dropped_field` on every UPS.

Bypass stash for the two successor packets (they bind a row that is not
just-materialized):

- `handle_post_compact` already runs `apply_in_place_compact_context_loss`
  and `consume_and_schedule_compact_self_continuation`. After that, arm
  `grok_pending_briefing` with the compact continuation block.
- `_bind_clear_successor` arms `grok_pending_briefing` with the clear
  handoff when the successor row is bound.

**Arming is crash-safe.** Consuming a durable source marker and enqueuing
its briefing reference must never straddle an unprotected crash window, and
the transaction boundary is chosen per row topology because
`_mutate_variables` is a **single-row** read-modify-write:

- **Same-row arming** (in-process compact: source markers and the briefing
  buffer live on the current session's variable row): the source-marker
  take and the `enqueue_if_absent` run in one `_mutate_variables`
  transaction, as before.
- **Cross-row arming** (clear: predecessor state seeds the successor's
  buffer; compact sibling-recovery: another session's row feeds the current
  one) never pretends to span rows atomically. The source-side arming
  writes a **generation-keyed staging record on the source row** — a
  monotonic per-source arming generation plus the payload or durable
  reference, written without destroying the source fields. The destination
  side performs an idempotent **take**: `enqueue_if_absent` into its own
  buffer keyed by (source session, arming generation) — a repeat take
  no-ops on that key — then marks the source staging record consumed in a
  separate retryable source-row mutation. Every write is single-row; a
  crash between the two writes leaves either an unconsumed staging record
  (the next hook completes the take) or an already-taken record whose
  consume-mark retries harmlessly. The arming generation is part of the
  briefing `component_id`, so a second compact or clear arriving while an
  older component is queued or claimed produces a distinct component, and
  source clearing compare-and-swaps against its own generation — an older
  commit can never clear or re-render a newer continuation.

Where a step consumes state outside the variable domain
(`consume_and_schedule_compact_self_continuation`), the arming writes a
retryable `briefing_staging_pending` marker holding the durable payload
reference **before** that consumption and clears it in the same mutation as
the successful enqueue — a crash between consumption and enqueue leaves the
marker, and the next hook completes staging from it. Crash-window tests
cover the PostCompact same-row path, the predecessor→successor clear
transfer, and the sibling→current compact recovery at each injected failure
point, plus a second-generation arming racing an older queued/claimed
component.

**Single delivery owner.** The turn-start prompt-injection rules must not
race this path on Grok. `inject-compact-handoff-on-prompt` (in
`inject-compact-handoff.yaml`) and `inject-clear-handoff-on-prompt` (in
`inject-clear-handoff.yaml`) fire on `turn_start` and clear
`compact_handoff_inject_pending` / `clear_handoff_inject_pending` — on Grok
that destroys the payload without delivery (UPS stdout is ignored) and would
double-deliver against this flush after a bundle sync. Exclude Grok in both
rules' `when` conditions; the PostCompact / clear-successor briefing arming
above is the only Grok owner. Refresh
`src/gobby/install/bundled_content_manifest.json` for the template change,
prove one owner with rule-registry tests, and update
`docs/contracts/session-boundary.md`, `docs/guides/sessions.md`, and
`docs/guides/variables.md` from the turn-start route to the PreToolUse/Stop
delivery contract.

**Buffer contract.** Both variables hold **structured components**, never
appended strings. Each component carries a stable `component_id`, a class
(`briefing` or `turn_context`), its payload (or, for briefing, a stable
reference to durable source state such as the handoff summary variable), and
its **ack mutations** — the delivered-state flips and producer-side delivery
marking (`mark_delivered_batch` for pending P2P messages,
`wiki_overview_injected`, `_startup_context_injected`, compact one-shots)
that must execute if and only if the component was actually delivered.
Producers never mark delivery at enqueue or stash time.

Both buffers are managed only through
`SessionVariableManager._mutate_variables` (atomic read-modify-write).
Bounds are **class-aware with exact numeric caps**, defined as named
constants in `grok_pending_context.py`. Sizes are UTF-8 byte lengths of the
serialized component payload; truncation and chunk boundaries always fall on
character boundaries, never inside a multibyte sequence:

- `briefing`: deduplicated by stable key (session, source one-shot,
  activation epoch) via enqueue-if-absent; **never evicted by a cap**. The
  source one-shot set is finite by construction, so cardinality is bounded:
  `BRIEFING_MAX_COMPONENTS = 16` is a defensive invariant — an enqueue of a
  17th distinct key is rejected with an error log (it signals a bug, not
  load). **Every briefing flush fits one provider-budget response — there
  are no continuation parts.** A briefing whose rendered payload exceeds
  the provider delivery budget for the target channel degrades through the
  existing `truncate_context_for_adapter` persisted-context mechanism: the
  response carries the in-budget head plus the stable persisted-context
  reference to the complete payload, in that single response. Flush renders
  both PreToolUse `reason` and Stop `additionalContext` through that same
  budget-and-degrade helper. The component commits on that one response —
  deny-once (Locked decision 4) holds for every size class, and no briefing
  content is lost: the oversize remainder stays retrievable through its
  persisted reference.
- `turn_context`: bounded drop-oldest (debug log) at
  `TURN_CONTEXT_MAX_COMPONENTS = 32` components and
  `TURN_CONTEXT_MAX_TOTAL_BYTES = 16384` serialized bytes. A single
  incoming component over `TURN_CONTEXT_MAX_COMPONENT_BYTES = 8192` is
  rejected at enqueue (debug log) rather than evicting older components.

Overflow tests cover startup, compact, and clear briefings, the
single-component-oversize case for each class, multibyte boundary
truncation, and the oversize-briefing single-response degradation
(in-budget head plus persisted reference, exactly one denial, commit on
that response).

**Claim and commit.** Flush claims components inside one atomic mutation:
read, select what fits the Grok reason/`additionalContext` truncation
budget, stamp the claimed set with a fresh `claim_id` **persisted with
owner/request id, a claimed-at timestamp, and a lease deadline**, and write
back the remainder.

*Lease.* The lease deadline is a persisted UTC timestamp computed at claim
time as the **effective outer hook-route timeout read from live config
(so overrides propagate) plus `GROK_CLAIM_LEASE_MARGIN_SECONDS = 30`** — a
named constant in `grok_pending_context.py`. The lease is therefore
strictly longer than every legitimate in-flight response and still bounds
daemon-death recovery. A claim whose lease has expired — daemon death
after claim, a stranded executor, any abandoned in-flight request — is
requeued by the next flush's stale-takeover check, so no payload or
acknowledgment is ever stranded. Controlled-clock tests cover just-before
and just-after expiry, timeout release, recovery across daemon restart,
and a timeout-config override changing the computed lease.

*Live claim.* A second gating hook arriving while a claim is live
(unexpired, uncommitted) must not treat in-flight work as empty work: its
PreToolUse returns a **retriable non-payload deny** ("briefing delivery in
flight — retry"), never an allow, so no tool crosses before the owner
commits, releases, or expires; a racing Stop follows its normal per-class
arm (it never blocks solely to wait). Loser behavior and owner-timeout
takeover are tested separately from the single payload-bearing denial.

*Commit.* The claim token rides the canonical response through adapter
translation, but the executor worker **never commits**:
`ACPHookAdapter.handle_native` runs inside the worker thread and cannot
know whether the route's outer `asyncio.wait_for` returned in budget. The
worker returns the translated payload plus the opaque claim token, and
**commit runs only in the async hook route** (`_run_adapter_hook`'s
caller), after the await completed within budget — the last boundary the
daemon can prove before emission; commit is honest about what remains
(response emission) and covers it with replay. Commit is a
compare-and-swap on `claim_id` executed in **one `_mutate_variables`
transaction** that (a) runs the claimed components' ack mutations, (b)
removes them from the buffer, writing their identities to the per-epoch
committed set, and (c) persists the **exact translated payload as a replay
envelope keyed by the hook request/envelope id**. The route then emits the
response and marks the envelope emitted in a follow-up mutation. A crash
or disconnect between commit and emission leaves an unemitted envelope:
the next eligible Grok request for that session **replays that envelope
verbatim before selecting new components**, so delivered markers never
flip for content Grok can no longer obtain — emission is at-least-once,
ack mutations exactly-once. A request without a durable request id
releases the claim instead of committing (retryable, no envelope). If the
commit CAS loses (lease expired and a takeover requeued the components),
the route **discards its stale payload** and returns the safe retriable
fallback — content is never returned to Grok unless its commit succeeded.
On translation error, or when the route times out and returns its fallback
while the executor thread is still running, the claim is atomically
released (components requeued, `claim_id` invalidated) — a late worker
cannot commit at all (commit lives in the route), and a near-boundary race
resolves to the route's single decision: in-budget result → commit;
timeout fallback → release. Focused failure tests cover translation error,
executor timeout with a late worker, requeue-then-redeliver, lease-expiry
takeover after simulated daemon death, crash-after-commit-before-emission
replay, and CAS-loss payload suppression.

**Pending P2P messages.** `EventEnricher` selects delivery candidates only
from BEFORE_AGENT, BEFORE_TOOL, and AFTER_TOOL, so 1.1's channel flip alone
would strand queued Grok messages. This leaf extends
`src/gobby/hooks/event_enrichment.py`: for Grok, pending messages stay
unclaimed until they ride an actual delivery — enqueued as components whose
ack mutation is the `mark_delivered_batch` call — included in a briefing
PreToolUse deny or an already-blocking Stop, and marked delivered only by
that component's commit. Because selection is non-claiming, two concurrent
eligible hooks can read the same `delivered_at IS NULL` rows: message
enqueue is therefore an **atomic upsert keyed by a stable message-derived
`component_id`** (the message UUID), so the second selector no-ops against
the existing component whether it is queued or claimed, and a message can
never ride two denials or two blocked Stops. After commit, its delivered
marking removes it from selection. A pending message never forces an
allowing Stop to carry context (the continuation loop this section
forbids). Tests cover allowance (message stays queued), gated delivery,
retry after a failed flush, commit-time delivered accounting, and two
concurrent hooks selecting the same undelivered row yielding exactly one
component and one delivery.

**Canonical delivery response.** The canonical response is the exact object
returned to the adapter. Under `preserve_original=True` (workflow and
webhook gates), enrichment writes to an observer copy and the original
returns untouched — so **stash harvests context from the enriched copy into
the buffers**, and **flush mutates only the canonical original**.
Delivery-relevant content always travels through the buffers; nothing
depends on enrichment output surviving on the returned object.
Observer/broadcast copies derive from the canonical response after flush.
Flushing an observer copy never reaches Grok; the preserve_original tests
must prove both the harvest and the flush target.

**First-activity gating events.** A first PreToolUse (or Stop) with no prior
UPS materializes on that same event. Startup-briefing staging is an
activation step (decision 7): every racer performs the atomic
enqueue-if-absent of the single startup-briefing component keyed by session
and activation epoch **before** its rules or tool handling proceed, and the
flush step claims that single key atomically — so the same PreToolUse
deny-once delivers the briefing on the no-UPS path, an interleaved
UPS/PreToolUse schedule stages exactly one component, and no schedule
produces two denials or zero briefings.

Flush (same module, still from `_complete_response`):

1. **PreToolUse with non-empty `grok_pending_briefing`:** deny once; `reason`
   is the briefing plus "Retry the same tool call." Claim the briefing
   components. Do not consume turn-context.
2. **PreToolUse deny from a real gate:** prepend leftover briefing (then
   turn-context if it fits) to `reason`; claim exactly what was prepended.
3. **Stop / SubagentStop with leftover briefing and no prior PreToolUse
   flush** (text-only turn): `decision: "block"`, `additionalContext` =
   briefing, continue once, claim the briefing components.
4. **Stop / SubagentStop with only turn-context:** if a real stop-gate
   already blocked, concatenate turn-context onto `reason` /
   `additionalContext` and claim it. If Stop is allowing, **drop**
   turn-context (debug log). Never block Stop solely to deliver
   turn-context.

In every arm, removing claimed components and executing their ack mutations
(`wiki_overview_injected`, `_startup_context_injected`, compact one-shots,
message delivered marking) happen at **commit** (Claim and commit above),
never at claim and never at stash.

Reserve `grok_pending_briefing` and `grok_pending_turn_context` in
`src/gobby/workflows/reserved_variables.py`.

**Acceptance:**

- 4.1.1 - Grok UPS on a just-materialized session writes `grok_pending_briefing`; later Grok UPS context appends `grok_pending_turn_context`; neither returns additionalContext. test: `tests/hooks/test_grok_pending_context.py`.
- 4.1.2 - First Grok PreToolUse after briefing denies once with that reason; delivered-state vars flip only then. test: `tests/hooks/test_grok_pending_context.py`.
- 4.1.3 - Turn-context concatenates onto an already-blocking Stop and is dropped (debug-logged) when Stop allows; Grok Stop with only turn-context and no stop-gate allows the stop. test: `tests/hooks/test_grok_pending_context.py`.
- 4.1.4 - Grok PostCompact and clear-successor bind arm briefing-class continuation. symbol: `MiscEventHandlerMixin.handle_post_compact`. symbol: `_bind_clear_successor`.
- 4.1.5 - Grok wire envelopes for flush outputs (deny reason, Stop block + additionalContext) are asserted at the adapter layer only. test: `tests/adapters/test_acp_hook_translation.py`.
- 4.1.6 - On Grok, `inject-compact-handoff-on-prompt` and `inject-clear-handoff-on-prompt` do not fire on turn_start and the pending flags survive until confirmed flush; non-Grok CLIs keep the turn_start route. test: `tests/workflows/test_context_handoff_rules.py`.
- 4.1.7 - Briefing and turn-context merge into workflow-block and webhook-block responses under `preserve_original=True`; the adapter-visible response carries them. test: `tests/hooks/test_grok_pending_context.py`.
- 4.1.8 - Buffer bounds enforce the named numeric caps: turn-context is drop-oldest at 32 components / 16384 serialized UTF-8 bytes and rejects a single component over 8192 bytes at enqueue; briefing components are deduplicated by stable key, never cap-evicted, and an oversize briefing degrades at flush to an in-budget head plus its persisted-context reference within one response; enqueue/flush interleavings lose nothing. test: `tests/hooks/test_grok_pending_context.py`.
- 4.1.9 - A no-UPS first PreToolUse stages the startup packet as briefing before flush and deny-once delivers it on that same event. test: `tests/hooks/test_grok_pending_context.py`.
- 4.1.10 - Regenerated bundled-content manifest exactly matches the committed shared-template tree. test: `tests/install/test_bundled_content_manifest.py::test_bundled_content_manifest_matches_tree`.
- 4.1.11 - Ack mutations run only at the route-owned commit: claimed components requeue on translation error or executor timeout, a worker result arriving after the route's timeout fallback can never commit (commit lives in the async route and the released claim fails compare-and-swap), and no delivered flag flips for a response Grok never saw. test: `tests/hooks/test_grok_pending_context.py`.
- 4.1.12 - Grok pending messages stay queued until a briefing deny or already-blocking Stop includes them; `mark_delivered_batch` runs only inside a delivered component's commit; an allowing Stop never carries one. test: `tests/hooks/test_pending_message_provider_contracts.py`.
- 4.1.13 - An interleaved first UPS and PreToolUse (UPS creates the row first, PreToolUse completes activation first, UPS stashes last) stages exactly one startup-briefing component and yields exactly one deny-once delivery. test: `tests/hooks/test_grok_pending_context.py`.
- 4.1.14 - Grok staged-memory IDs remain retryable through stash and drop and are recorded only by confirmed component commit. test: `tests/hooks/test_grok_pending_context.py`.
- 4.1.15 - Agent, wiki, profile, and startup one-shot markers remain unset through Grok composition and stash and flip only on confirmed component commit. test: `tests/hooks/test_grok_pending_context.py`.
- 4.1.16 - EventEnricher preserves non-Grok inline delivery while Grok enqueues without acknowledgment across formatting and retry failures. test: `tests/hooks/test_event_enrichment.py`.
- 4.1.17 - The real route timeout returns fallback, releases the exact Grok claim, and a late worker result cannot commit or exceed worker bounds. test: `tests/servers/test_mcp_routes.py`.
- 4.1.18 - Claims persist owner, timestamp, and a UTC lease deadline computed as the live effective route timeout plus `GROK_CLAIM_LEASE_MARGIN_SECONDS`; a claim orphaned by simulated daemon death is requeued by stale takeover only after lease expiry under a controlled clock (just-before expiry holds, just-after requeues, a timeout override changes the computed lease); commit executes only in the async route after an in-budget await, and a route whose commit CAS loses discards its payload and returns the retriable fallback. test: `tests/hooks/test_grok_pending_context.py`.
- 4.1.19 - Two concurrent eligible hooks selecting the same undelivered P2P row upsert exactly one component by message-derived component_id and produce exactly one delivery. test: `tests/hooks/test_pending_message_provider_contracts.py`.
- 4.1.20 - Compact and clear arming survive injected crashes at every boundary: same-row arming completes or retries in one transaction; cross-row transfers (predecessor→successor clear, sibling→current compact recovery) complete through the generation-keyed staging record and idempotent take with no loss or duplication; a second-generation arming racing an older queued or claimed component yields distinct components and generation-scoped source clearing. test: `tests/hooks/test_misc_handlers.py`.
- 4.1.21 - An oversize briefing delivers in exactly one response — in-budget head plus persisted-context reference through the shared budget-and-degrade helper for both PreToolUse reason and Stop additionalContext — with exactly one denial and ack mutations on that single commit. test: `tests/hooks/test_grok_pending_context.py`.
- 4.1.22 - Commit atomically persists the exact translated payload as a replay envelope keyed by the hook request id; a simulated crash between commit and emission replays that envelope verbatim on the next eligible request before new selection, a crash after emission marks it emitted without re-delivery, and a request without a durable id releases instead of committing. test: `tests/hooks/test_grok_pending_context.py`.
- 4.1.23 - A second PreToolUse arriving during a live unexpired claim returns a retriable non-payload deny and never allows its tool; after the owner commits or releases, the next PreToolUse proceeds normally; owner-timeout takeover is tested separately from the payload-bearing denial. test: `tests/hooks/test_grok_pending_context.py`.
- 4.1.24 - A stale racer reaching its enqueue step after the startup component committed finds its identity in the per-epoch committed set and does not re-enqueue: the commit-before-late-stash interleaving yields no second briefing denial. test: `tests/hooks/test_grok_pending_context.py`.

## P5: Contract tests and smoke
`kind: framing`

**Goal**: Prove deferred creation and Grok delivery without a live TUI, then
document the live check.

### 5.1 Isolated-daemon smoke plus unit contracts [category: test] (depends: 4.1)
`kind: deliverable`

Targets:
- `tests/e2e/test_grok_session_deferral.py`
- `tests/e2e/conftest.py::*` — scope-reason: Grok UPS mapping plus pre_tool_use, stop, and post_compact simulator helpers change across CLIEventSimulator
- `tests/adapters/test_acp_hook_translation.py::*` — scope-reason: Grok flush unit cases land across the translation test classes
- `tests/adapters/test_capabilities.py::*` — scope-reason: capability contract cases extend the file
- `tests/hooks/test_session_events_coverage.py::*` — scope-reason: deferral assertions extend the session-start test classes
- `tests/hooks/test_hooks_manager.py::*` — scope-reason: materialization assertions extend the file
- `tests/e2e/test_full_workflow.py::*` — scope-reason: consumer of CLIEventSimulator.session_start; row expectations shift to first prompt
- `tests/e2e/test_session_tracking.py::*` — scope-reason: consumer of CLIEventSimulator.session_start; row expectations shift to first prompt
- `tests/e2e/test_stateless_ambient_session.py::*` — scope-reason: consumer of CLIEventSimulator.session_start; row expectations shift to first prompt
- `tests/e2e/test_worktrees_e2e.py::*` — scope-reason: consumer of CLIEventSimulator.session_start; row expectations shift to first prompt

Add `"grok": "user_prompt_submit"` to
`CLIEventSimulator.user_prompt_submit`'s `hook_type_by_source` (it is missing
today; smoke cannot speak Grok UPS without it). Add a Grok `pre_tool_use` /
`stop` helpers if not already present on the simulator.

New isolated-daemon test `tests/e2e/test_grok_session_deferral.py` (marker
`e2e`, prefix `GOBBY_TEST_PROTECT=1`). It is the smoke:

1. `cli_events.session_start(external_id, cli_source="grok", cwd=project)` →
   HTTP 200, `decision` allow, **zero** `sessions` rows for that
   `external_id`.
2. Repeat with `cli_source="claude"` — same: no row.
3. `cli_events.user_prompt_submit(..., source="claude", prompt="hello")` →
   exactly one row; response `hookSpecificOutput.additionalContext` (or
   Claude equivalent) is non-empty (session briefing).
4. `cli_events.user_prompt_submit(..., source="grok", prompt="hello")` on a
   fresh external_id → one row; response has **no** `additionalContext`;
   session variable `grok_pending_briefing` is non-empty.
5. Grok `pre_tool_use` (`read_file` / `gobby__list_tools`) → `decision: deny`
   and `reason` contains the briefing; second identical PreToolUse is allow
   (briefing consumed) unless another gate fires.
6. Grok `stop` with a real gate (`require-task-close` / `tool_block_pending`)
   → `decision: "block"` (not `"deny"`) and `continue: true`. Grok `stop`
   with only leftover turn-context and no gate → allow (no continuation loop).
7. Compact/pre-created: SessionStart with `session_start_source="compact"` on
   an existing row does not create a second row. Grok `post_compact` on an
   existing row arms `grok_pending_briefing`; next PreToolUse denies once
   with the continuation block.
8. After a successful briefing flush, `wiki_overview_injected` /
   `_startup_context_injected` are true; after stash-only they are still
   false.

Keep it one file, no full pytest. Command:

```bash
GOBBY_TEST_PROTECT=1 uv run pytest tests/e2e/test_grok_session_deferral.py tests/adapters/test_acp_hook_translation.py tests/adapters/test_capabilities.py tests/hooks/test_session_events_coverage.py -q
```

Live operator check (not CI): `grok` in this repo, `gobby sessions list`
shows no new idle row; first prompt creates `#N`; first tool may show a
one-time briefing deny; `gobby sessions` after Ctrl+C without typing stays
unchanged.

**Acceptance:**

- 5.1.1 - Smoke file asserts Grok/Claude SessionStart does not insert a sessions row. test: `tests/e2e/test_grok_session_deferral.py`.
- 5.1.2 - Simulator maps grok UPS to `user_prompt_submit`. symbol: `CLIEventSimulator.user_prompt_submit`.
- 5.1.3 - Smoke asserts Grok first PreToolUse deny carries briefing, Stop `block` is only for real gates, and PostCompact arms briefing. test: `tests/e2e/test_grok_session_deferral.py`.
- 5.1.4 - Claude first prompt returns startup context while Grok UPS returns none and leaves a pending briefing. test: `tests/e2e/test_grok_session_deferral.py`.
- 5.1.5 - After the one briefing denial, a second PreToolUse allows absent another gate and Stop with only turn-context also allows. test: `tests/e2e/test_grok_session_deferral.py`.
- 5.1.6 - Compact binding creates no duplicate row and delivered-state markers remain false before flush then become true after commit. test: `tests/e2e/test_grok_session_deferral.py`.

## V2 End-to-end verification
`kind: verification`

After all leaves:

1. Unit: Grok capabilities + Stop `block` + observe stdout empty;
   `docs/guides/adapter-fidelity.md` Grok row matches.
2. Unit: startup SessionStart does not register; first UPS or first
   PreToolUse does; activation is outside the lookup lock.
3. Smoke: `GOBBY_TEST_PROTECT=1 uv run pytest tests/e2e/test_grok_session_deferral.py -q`
   against an isolated daemon (includes Stop-loop guard and PostCompact
   briefing).
4. Focused: session coverage + hook manager tests that previously assumed
   SessionStart registration.
5. Optional live: idle `grok` then first prompt, confirm session list timing
   and one-shot briefing deny; `compact_self` then first tool shows the
   continuation block in a deny reason, not via `wait_for_summary`.
6. Verify both phases of the coordinator-owned collision cleanup
   (Constraints, Collision section). Phase A completed before expansion
   began: #20635 still open with its `deferred-from` provenance and #20724
   parentage, #20727/#20733–#20735 closed `obsolete`, #20724's blocked-by
   on #20727 dropped, compact-summary-fidelity §4 rewritten off
   `wait_for_summary` and §3 annotated superseded-pending-delivery with
   `task_ref` unmoved, with `update_plan_hash` + `validate_plan` clean
   after each amendment. Phase B completed before the `gobby build`
   automation opt-in: the epic created through the automation-disabled
   expansion path, #20635 blocked-by that epic, `validate_plan` clean, and
   only then `gobby build '#<epic>'`. #20635 closes
   `completed`/`already_implemented` only when the epic delivers. #20726
   untouched.

## V1 Plan Changelog
`kind: verification`

**Round 1** `kind: verification`

- reviewer_run: 1e1bf190-395b-4ae4-bd66-c25c1eb0f8bf
- reviewer_session: #11030 (fb11a707-1cce-473f-a4b3-962d45ac244f)
- verdict: needs_review
- findings:
- GHDM-R1-F01/blocking/traceability — duplicate compact/clear delivery owners: legacy Grok turn-start injection rules and stale docs conflict with the new PreToolUse/Stop briefing flush
- GHDM-R1-F02/blocking/weak-testability — acceptance 4.1.1 overclaims (any UPS writes briefing) and 4.1.2 asserts state at an adapter-only seam that cannot observe stash/flush
- GHDM-R1-F03/blocking/weak-testability — lifecycle acceptance lacks a first-hook event matrix and SessionStart-never-emits-context assertions (typed repairs)
- GHDM-R1-F04/blocking/gobby-format — Targets miss top-level Grok constants, moved flow patch seams, direct PostCompact/clear tests, and simulator helpers
- GHDM-R1-F05/blocking/unhandled-edge — `_materialize_activation_done` not reserved; public writes could skip activation
- GHDM-R1-F06/blocking/traceability — force-stop translation branch keys on nonexistent `response.continue`; no reachable producer
- GHDM-R1-F07/blocking/unhandled-edge — activation race contradiction (Constraints say loser waits, Locked Decision 7 says proceed) and missing briefing on no-UPS first-PreToolUse path
- GHDM-R1-F08/blocking/unhandled-edge — pending buffers lack atomic bounded enqueue/claim, budget serialization, remainder retention, and failure recovery
- GHDM-R1-F09/blocking/unhandled-edge — flush target undefined under `preserve_original=True`; observer-copy flush never reaches Grok
- GHDM-R1-F10/blocking/traceability — pending P2P messages stranded once Grok candidate events become ContextChannel.NONE; EventEnricher has no Stop/briefing path
- GHDM-R1-F11/blocking/unhandled-edge — synthetic SessionStart payload lacks `data.source='startup'` and carries prompt/tool fields, breaking startup-sensitive rule predicates
- resolution_notes: All 11 findings accepted by user vote (accept-all). F03 typed repairs applied via apply_plan_review_repairs (with the repair artifact filename corrected from test_hooks_manager.py to test_hook_manager.py); the remaining ten findings hand-repaired as prose/target edits per each finding's fix, choosing the remove-unreachable-branch arm for F06 and the independent-idempotent-completion arm for F07. Review cap is 1: no further adversary rounds this cycle.

```json plan-review-round
{"evidence_id":"f8c1140a-c5fe-42ed-be02-db0f617eac00","plan_hash":"f220bb24387ef138596554d50bf4a5ecc24f48b6ea3c9c5c4b2e5ea8b0457f5b","round_number":1,"round_result":{"coverage_attestation":{"adjacent_variant_complete":true,"attestation_digest":"823c2b22258ba1eff83388dd74ef5a265cdbd3021e2c51680013cdcd3b637803","cross_lane_interaction_complete":true,"disposition_counts":{"dismissed":11,"emitted_findings":11,"total":22},"evidence_id":"f8c1140a-c5fe-42ed-be02-db0f617eac00","lanes":[{"candidate_count":6,"lane_id":"requirements_traceability","status":"completed"},{"candidate_count":7,"lane_id":"repository_blast_radius","status":"completed"},{"candidate_count":9,"lane_id":"runtime_invariants","status":"completed"}],"shadow_manifest_status":{"entry_count":5,"manifest_digest":"c2feb7b039d06b2413f0578838cf49b55d721ac9cfe68c9d1315d9681c13b883","status":"valid"},"source_digest":"f5bfe988bb0ad3f1b9a16ea579bea268a3714130e5cd013f4646955fb289e204","version":1},"findings":[{"category":"traceability","check_key":"single-owner-compact-clear-delivery","description":"`inject-compact-handoff-on-prompt` still injects and clears Grok compact state on `turn_start`, and `inject-clear-handoff-on-prompt` can do the same for clear successors, while §4.1 also arms `grok_pending_briefing` for PreToolUse/Stop. A bundle sync therefore creates duplicate payloads and clears one-shots before the claimed confirmed flush; `session-boundary.md`, `sessions.md`, and `variables.md` still prescribe the old route.","finding_id":"GHDM-R1-F01","fix":"Make §4.1 retire the Grok compact turn-start rule and exclude Grok from the shared clear turn-start rule, update `src/gobby/install/bundled_content_manifest.json`, add rule-registry tests proving one owner, and update `docs/contracts/session-boundary.md`, `docs/guides/sessions.md`, and `docs/guides/variables.md` to the PreToolUse/Stop contract.","location":"P4 / §4.1 bundled rules and documentation","prevention":"Sweep bundled and installed rule inventories plus canonical docs whenever a delivery channel changes.","principle":"Every one-shot payload must have one delivery owner and one clearing point.","root_cause":"The new briefing path was added without retiring or narrowing the existing Grok turn-start delivery rules absorbed from the superseded work.","section_id":"4.1","severity":"blocking"},{"category":"weak-testability","check_key":"pending-context-classification-and-state-tests","description":"Acceptance 4.1.1 says any Grok UPS writes `grok_pending_briefing`, while the body classifies only `_session_just_materialized` context as briefing and routes later UPS context to `grok_pending_turn_context`. Acceptance 4.1.2 points at `test_acp_hook_translation.py`, which cannot observe HookManager stash/flush, session variables, or delivered-state flags.","finding_id":"GHDM-R1-F02","fix":"Narrow 4.1.1 to just-materialized UPS, add separate ordinary turn-context enqueue and delivery/drop acceptance, and place pending-variable plus delivered-state assertions in dedicated `grok_pending_context`/HookManager tests; keep adapter tests focused on the final wire envelope.","location":"P4 / §4.1 Acceptance 4.1.1-4.1.2","prevention":"Map each acceptance claim to the lowest test layer capable of observing every referenced state mutation.","principle":"Acceptance must distinguish each state class and execute the layer that owns its transition.","root_cause":"The acceptance text collapses first-activity and ordinary UPS context, then assigns durable state assertions to an adapter-only test seam.","section_id":"4.1","severity":"blocking"},{"category":"weak-testability","check_key":"lifecycle-contract-acceptance-matrix","description":"The plan requires every first hook except SESSION_END and NOTIFICATION to materialize for every CLI, and requires every SessionStart path to emit no context. Acceptance covers BEFORE_AGENT, PreToolUse, one NOTIFICATION negative, and selected binding survival, so expansion can validate while other first events create no row or compact/resume/clear/pre-created SessionStart still leaks context.","finding_id":"GHDM-R1-F03","fix":"Add a parameterized first-hook matrix with explicit idle SESSION_END/NOTIFICATION negatives and representative provider parity, plus startup/compact/resume/clear/web-chat/pre-created assertions that SessionStart returns empty context and system-message fields.","location":"P2-P3 / §2.1 and §3.1 acceptance","prevention":"Build an event-type × provider/path matrix from Overview and Locked Decisions before finalizing acceptance.","principle":"A class-wide lifecycle invariant needs a bounded matrix that covers every branch class.","repairs":[{"items":[{"artifact":"test: `tests/hooks/test_session_events_coverage.py::test_session_start_never_emits_context`","prose":"Every startup, compact, resume, clear, web-chat, and pre-created SessionStart response has empty context and system-message fields"}],"kind":"add_acceptance","section_id":"2.1"},{"items":[{"artifact":"test: `tests/hooks/test_hooks_manager.py::test_first_hook_materialization_matrix`","prose":"A parameterized first-hook matrix materializes every supported non-SessionStart event except SESSION_END and NOTIFICATION, and both exclusions leave an idle startup row absent"}],"kind":"add_acceptance","section_id":"3.1"}],"root_cause":"Acceptance samples the common prompt/tool paths while leaving the generic any-event rule and non-deferred SessionStart output unasserted.","section_id":"3.1","severity":"blocking"},{"category":"gobby-format","check_key":"targets-cover-changed-symbol-and-patch-seams","description":"`capabilities.py::_grok_capabilities` excludes the two top-level Grok constants that §1.1 changes; §2.1 omits session-variable and handoff tests that patch moved `flow` symbols; §4.1 omits direct PostCompact/clear-successor tests; §5.1 exact simulator targets exclude the required PreToolUse, Stop, and PostCompact helpers.","finding_id":"GHDM-R1-F04","fix":"Use a justified `capabilities.py::*` Target, add `test_session_variable_preservation.py` and `test_session_handoff_handlers.py` to §2.1, add `test_misc_handlers.py` and handoff-handler tests to §4.1, and replace the two exact `conftest.py` Targets with a justified wildcard or every changed simulator helper.","location":"§1.1, §2.1, §4.1, and §5.1 Targets","prevention":"For every exact Target, sweep enclosing top-level assignments, monkeypatch strings, direct handler tests, and helper methods used by acceptance.","principle":"Targets must cover every changed symbol scope and every patch seam that moves with it.","root_cause":"The inventory names primary functions while missing top-level constants, moved import/patch seams, direct state tests, and simulator helpers.","section_id":"1.1","severity":"blocking"},{"category":"unhandled-edge","check_key":"reserve-materialization-control-marker","description":"`_materialize_activation_done` suppresses activation when truthy, yet it is absent from the planned reserved-variable update. Public variable tools or ordinary rule effects can set it and skip agent/wiki/profile/transcript activation.","finding_id":"GHDM-R1-F05","fix":"Reserve `_materialize_activation_done` alongside the Grok variables and add MCP `set_variable` plus non-internal rule-effect rejection tests.","location":"P3 / §3.1 activation state","prevention":"Add every new runtime control key to the reserved-variable sweep and rejection tests.","principle":"Runtime-owned control markers must be protected from public and rule-authored writes.","root_cause":"The plan reserves the two Grok buffers while omitting `_materialize_activation_done`, which gates required activation.","section_id":"3.1","severity":"blocking"},{"category":"traceability","check_key":"force-stop-input-exists","description":"The force-stop precedence branch has no defined input, so an implementing agent cannot distinguish the claimed force-stop from natural Stop allowance or continuation block. The related acceptance can pass isolated wire cases without a reachable producer.","finding_id":"GHDM-R1-F06","fix":"Either remove the unreachable force-stop branch and preserve the current allow outcome, or add a clearly named unified Stop outcome with a concrete producer and Targets for `HookResponse` plus all consumers; then test precedence across allow, block with context, SubagentStop, and the real force-stop producer.","location":"P1 / §1.1 Grok Stop translation","prevention":"Trace each proposed native output backward through HookResponse and every producer before adding translation behavior.","principle":"Every adapter branch must trace to a real unified-model field and a real producer.","root_cause":"The plan specifies `response.continue is False`, but `HookResponse` has no continuation field; `force_allow_stop` produces an ordinary `decision='allow'` outcome.","section_id":"1.1","severity":"blocking"},{"category":"unhandled-edge","check_key":"first-activity-activation-and-briefing-barrier","description":"A racing PreToolUse can proceed while another caller seeds activation, so rules and the tool may run before the briefing exists. Even without a race, a first PreToolUse materializes and produces startup context in that same response, while §4.1 stashes only observe hooks and flushes only an already-pending briefing, leaving the no-UPS first-tool path without its one-time briefing.","finding_id":"GHDM-R1-F07","fix":"Choose a decision-complete activation protocol: an atomic claim with barrier and crash takeover, or independent idempotent completion by every racer before proceeding. Stage same-event just-materialized PreToolUse/Stop context as briefing before flush, and add concurrent UPS/PreToolUse, crash-after-claim, and first-PreToolUse-without-UPS tests.","location":"P3-P4 / §3.1 activation race and §4.1 first flush","prevention":"Walk concurrent first-event interleavings, including crash-after-claim and a gating event as the first hook.","principle":"The first event must not cross rule or tool boundaries before prerequisite activation and briefing state are complete.","root_cause":"Constraints say the loser waits, Locked Decision 7 says it proceeds immediately, and no atomic claim/barrier/crash-takeover protocol resolves the contradiction.","section_id":"3.1","severity":"blocking"},{"category":"unhandled-edge","check_key":"bounded-atomic-pending-context-delivery","description":"Concurrent stash/PostCompact/clear/flush operations can lose, resurrect, or duplicate context. Repeated observe context grows without a bound; PreToolUse reasons are untruncated, while Stop truncates after §4.1 says to clear the full buffer and flip all one-shots. Translation failure or executor timeout can also occur after pending state changed.","finding_id":"GHDM-R1-F08","fix":"Use the existing `SessionVariableManager._mutate_variables` seam for bounded atomic enqueue and claim, serialize against the Grok provider budget, retain any remainder, and commit only the components included at the closest reliable response boundary. Add enqueue/flush interleaving, oversize, translation-error, timeout, and replay/requeue tests.","location":"P4 / §4.1 pending buffers","prevention":"For every durable queue, specify enqueue, claim, commit/requeue, bounds, concurrent interleavings, and component-level accounting.","principle":"Durable pending delivery requires an atomic bounded claim that retains undelivered state.","root_cause":"The plan treats buffers as appended strings and clears them at HookManager flush without defining transaction, output budget, retained remainder, or failure recovery.","section_id":"4.1","severity":"blocking"},{"category":"unhandled-edge","check_key":"preserve-original-delivery-target","description":"Real PreToolUse/Stop gates use the preserve-original path. Flushing the observer copy never reaches Grok; flushing only the original can omit enrichment contributions and diverge from broadcasts.","finding_id":"GHDM-R1-F09","fix":"Define one canonical Grok delivery response after all delivery-relevant enrichment, derive observer/broadcast copies from it, and add tests for briefing and turn-context merged into workflow and webhook gates with `preserve_original=True`.","location":"P4 / §4.1 HookManager._complete_response","prevention":"Trace response identity through normal, workflow-block, and webhook-block exits before locating delivery logic.","principle":"A delivery mutation must target the exact response object returned to the adapter.","root_cause":"`preserve_original=True` enriches an observer copy, restores its decision/reason, and returns the untouched original; §4.1 never chooses which object stash/flush owns.","section_id":"4.1","severity":"blocking"},{"category":"traceability","check_key":"pending-message-stop-consumer","description":"After §1.1 makes all three current Grok candidate events `ContextChannel.NONE`, pending messages remain undelivered indefinitely because Stop is absent from candidate selection. Adding Stop naively would also make an allowing Stop carry context and create the continuation behavior §4.1 forbids.","finding_id":"GHDM-R1-F10","fix":"Target `src/gobby/hooks/event_enrichment.py` and specify a Grok pending-message path that leaves messages unclaimed until inclusion in a safe briefing PreToolUse denial or an already-blocking Stop, with tests for allowance, gating, retry, and delivered accounting.","location":"P1-P4 / piggyback message handoff","prevention":"Trace each queued producer through candidate selection, capability filtering, claim, and delivered accounting.","principle":"Changing capability metadata cannot route data through a consumer that excludes the new event.","root_cause":"The plan says pending messages will wait for Stop, while `EventEnricher` considers only BEFORE_AGENT, BEFORE_TOOL, and AFTER_TOOL candidates.","section_id":"1.1","severity":"blocking"},{"category":"unhandled-edge","check_key":"synthetic-session-start-payload-parity","description":"Startup-sensitive rules such as `reset-plan-mode-on-session-start` require `event.data['source'] == 'startup'`; first-prompt envelopes do not supply that SessionStart field. Resets can silently disappear, other predicates take accidental branches, and custom rules see prompt/tool data they never received on SessionStart.","finding_id":"GHDM-R1-F11","fix":"Define an explicit synthetic SessionStart schema with `data.source='startup'`, preserved deferred identity fields, and no live prompt/tool payload; audit and test all installed SessionStart lifecycle rules plus one custom rule against it.","location":"P3 / §3.1 copied SessionStart evaluation","prevention":"Inventory every lifecycle rule predicate and construct a minimal canonical synthetic payload before reusing rule evaluation.","principle":"Synthetic lifecycle events must reproduce the canonical payload contract used by every rule.","root_cause":"The plan changes only the copied event type, while first-prompt data lacks SessionStart `data.source` and contains unrelated prompt/tool fields.","section_id":"3.1","severity":"blocking"}],"reviewer_session":"#11030","round":1,"verdict":"needs_review"},"session_id":"6dcc8341-5b1c-4682-a578-2c012b601e65"}
```

**Round 2** `kind: verification`

- reviewer_run: b3dc5ac4-305b-408e-a786-cb4f5b2a1c09
- reviewer_session: #11036
- verdict: needs_review
- findings:
- GHDM-R2-F01 / blocking / acceptance 1.1.5's pending-message delivery consumer lives only in dependent §4.1, so the §1.1 leaf cannot satisfy its own validation
- GHDM-R2-F02 / blocking / §3.1 NOTIFICATION probe branch contradicts acceptance 3.1.6–3.1.7's unconditional exclusion
- GHDM-R2-F03 / blocking / §2.1 strips SessionStart behavior before §3.1 installs after-lookup activation, leaving an invalid runtime between leaves
- GHDM-R2-F04 / blocking / delivery claim/commit protocol ignores preserve_original copies, adapter-stage translation, executor timeout races, and producer-side delivered markers
- GHDM-R2-F05 / blocking / concurrent first UPS and PreToolUse can flush without a briefing or enqueue duplicate startup packets
- GHDM-R2-F06 / blocking / copied SessionStart evaluation lacks its own durable exactly-once state, crash takeover, and pre-deploy-row policy
- GHDM-R2-F07 / blocking / `_materialize_activation_done` has no required/best-effort failure classification, so partial activation can be marked done or block forever
- GHDM-R2-F08 / blocking / empty compact SessionStart retains only the pending flag, stranding skill-reload fields the non-Grok turn-start delivery needs
- GHDM-R2-F09 / blocking / collision cleanup (#20635/#20727/#20733–#20735, #20724 dependency and live check) has no manifest-producing owner
- GHDM-R2-F10 / blocking / bundled-content manifest regeneration lacks the tree-equality regression test in Targets and acceptance
- GHDM-R2-F11 / blocking / capability acceptance misses deletion of the Grok system-message constant and the full passive-hook no-context matrix
- GHDM-R2-F12 / blocking / single drop-oldest buffer policy can evict mandatory one-shot briefings, breaking deny-once/at-least-once guarantees
- resolution_notes: All 12 findings accepted by the user. Typed repairs (F10, F11) applied via apply_plan_review_repairs; prose repairs hand-applied for F01–F09 and F12. Chosen arms: F02 keeps NOTIFICATION excluded unconditionally with an observational-only probe; F03 makes §2.1 behavior-preserving and moves the registration-timing cutover atomically into §3.1; F09 assigns collision cleanup to an explicit coordinator-owned pre-expansion transaction recorded in the Collision section. Review cap extended by the user to 10 rounds or approval, with an early stop for consultation if a round's findings are entirely fixer-induced.

```json plan-review-round
{"evidence_id":"51e30e3b-5268-46b6-8cff-e33360216bce","plan_hash":"9ea47f7eaae6c0463b09ba832971bb7b953e74b4b4309e6030437e97e4aef47d","round_number":2,"round_result":{"coverage_attestation":{"adjacent_variant_complete":true,"attestation_digest":"a8454eeb02e24b34688c3bb0e93d84291d0545c2619f3706730b64f66773f79b","cross_lane_interaction_complete":true,"disposition_counts":{"dismissed":10,"emitted_findings":12,"total":22},"evidence_id":"51e30e3b-5268-46b6-8cff-e33360216bce","lanes":[{"candidate_count":11,"lane_id":"requirements_traceability","status":"completed"},{"candidate_count":3,"lane_id":"repository_blast_radius","status":"completed"},{"candidate_count":8,"lane_id":"runtime_invariants","status":"completed"}],"shadow_manifest_status":{"entry_count":5,"manifest_digest":"e053c12a253ed62b29587837bdfd0e0caf66287bbbf0582f96f8bd6ae3c62976","status":"valid"},"source_digest":"aa01e9bdfffaa760d4ecdeb82ae64080f6894daf2e2a6cb3a2d6cabe0596e5a0","version":1},"findings":[{"category":"bad-sequencing","causal_finding_id":"GHDM-R1-F10","causal_section_ids":["1.1","4.1"],"check_key":"edge-case-coverage","description":"Acceptance 1.1.5 requires pending messages to remain queued until a briefing PreToolUse denial or already-blocking Stop confirms inclusion, but §4.1 creates those delivery paths and depends on §1.1. The 1.1 leaf therefore cannot satisfy its own validation before its dependent exists.","finding_id":"GHDM-R2-F01","fix":"Move EventEnricher’s Grok pending-message routing, its tests, and acceptance 1.1.5 into §4.1. Keep §1.1 limited to capability and Stop translation work, or introduce an earlier delivery-interface leaf that both sections depend on.","introduced_in_round":1,"location":"P1 / §1.1 pending-message acceptance and P4 dependency","prevention":"Trace every acceptance outcome through the dependency DAG and reject any leaf whose required consumer appears only downstream.","principle":"Every leaf must be independently satisfiable from its own section and completed predecessors.","root_cause":"The F10 repair placed the pending-message producer and acceptance in 1.1 while leaving the only safe delivery consumer in dependent section 4.1.","section_id":"1.1","severity":"blocking"},{"category":"unhandled-edge","causal_finding_id":"GHDM-R1-F03","causal_section_ids":["3.1"],"check_key":"acceptance-observability","description":"The body says to add NOTIFICATION materialization if Claude and Grok probes show it never fires before a prompt, while acceptance 3.1.6 and 3.1.7 always require NOTIFICATION to remain excluded. A favorable probe result makes a conforming implementation fail its own acceptance and conflicts with the plan’s locked exclusion.","finding_id":"GHDM-R2-F02","fix":"Choose the policy in the plan now. The simpler consistent form is to keep NOTIFICATION excluded unconditionally and make the probe observational only; otherwise define a durable probe artifact and branch all normative text and acceptance on its recorded result.","introduced_in_round":1,"location":"P3 / §3.1 Notification probe and acceptance 3.1.6-3.1.7","prevention":"For every implementation-time probe, enumerate each possible result and confirm the normative decision plus acceptance agree for every branch.","principle":"A decision-bearing probe and its acceptance criteria must describe the same reachable outcomes.","root_cause":"The repaired matrix made NOTIFICATION an unconditional negative while the implementation prose retained a branch that may add NOTIFICATION after probing.","section_id":"3.1","severity":"blocking"},{"category":"bad-sequencing","check_key":"atomic-leaf-cutover","description":"Section 2.1 stops SessionStart row creation and context delivery. Until §3.1 lands, the existing lookup path can auto-register the first non-start hook, but HookManager has no after-lookup activation or copied-rule path, so that hook proceeds against an unactivated session. The refactor-category leaf is also behavior-changing.","finding_id":"GHDM-R2-F03","fix":"Make §2.1 a behavior-preserving extraction that retains current registration and activation. Make §3.1 atomically switch registration timing and install after-lookup activation plus copied SessionStart evaluation in the same leaf, or merge the two leaves.","location":"P2-P3 / §2.1 to §3.1 integration boundary","participating_section_ids":["2.1","3.1"],"prevention":"Simulate runtime behavior after each leaf independently, including the interval before any dependent leaf is applied.","principle":"Every committed leaf must leave the runtime internally valid before dependent leaves begin.","root_cause":"Behavioral deferral was bundled into the extraction leaf, while the activation tail required by deferred sessions was postponed to the next leaf.","section_id":"2.1","severity":"blocking"},{"category":"unhandled-edge","causal_finding_id":"GHDM-R1-F08","causal_section_ids":["4.1"],"check_key":"edge-case-coverage","description":"`preserve_original=True` enriches an observer copy and returns the untouched original, yet the plan calls that original both untouched and enriched. Buffer claims happen in HookManager; Grok translation happens later in `ACPHookAdapter.handle_native`; `asyncio.wait_for` may return a fallback while the executor thread continues. Wiki, agent-instruction, memory, and pending-message producers can also mark delivery before this boundary. Without stable component and claim IDs, timeout nack can race a late commit, messages can duplicate, and delivered flags can lie.","finding_id":"GHDM-R2-F04","fix":"Define one canonical adapter-visible response and structured buffered components carrying stable IDs plus their acknowledgment mutations. Persist a claim ID, derive observer/broadcast copies from the canonical response, commit via compare-and-swap only after successful translation and executor completion, and atomically release on translation error or timeout so a late worker cannot commit. Target the wiki/agent/memory/message producers, adapter handle path, route timeout path, and focused failure tests.","introduced_in_round":1,"location":"P4 / §4.1 canonical response, component claim, translation, and executor timeout","prevention":"Trace response identity and durable state through normal, preserve_original, translation-error, timeout, and late-worker exits before choosing claim and commit points.","principle":"Durable delivery state must be committed at the last boundary that can prove the exact provider-visible response succeeded.","root_cause":"The repair defines claim/requeue inside HookManager even though response enrichment, adapter translation, executor timeout, and producer-side delivered markers span several owners outside the section’s Targets.","section_id":"4.1","severity":"blocking"},{"category":"unhandled-edge","causal_finding_id":"GHDM-R1-F07","causal_section_ids":["3.1","4.1"],"check_key":"edge-case-coverage","description":"UPS can create the row and still be composing its response while a racing PreToolUse independently completes activation and reaches flush with no briefing. If both racers stage the startup packet, append-only atomic mutation can enqueue two copies because delivered state remains false until flush, producing two denials.","finding_id":"GHDM-R2-F05","fix":"Make startup briefing staging a prerequisite of activation for every racer. Atomically enqueue-if-absent one component keyed by session and activation epoch before live rules or tool handling, then atomically claim that single key at flush. Add a controlled interleaving test where UPS creates first, PreToolUse finishes first, and UPS stashes last.","introduced_in_round":1,"location":"P3-P4 / concurrent first UPS and PreToolUse briefing staging","prevention":"Walk deterministic UPS-first, tool-first, and interleaved first-event schedules, checking both missing and duplicate one-shot delivery.","principle":"Every first gating event must observe exactly one fully staged startup briefing before it crosses the tool boundary.","root_cause":"Independent activation completion does not establish a single atomic startup-briefing owner.","section_id":"3.1","severity":"blocking"},{"category":"unhandled-edge","causal_finding_id":"GHDM-R1-F07","causal_section_ids":["3.1"],"check_key":"edge-case-coverage","description":"Two first-hook racers can both evaluate copied SessionStart rules, including `auto-run-pipeline`; a crash after `_materialize_activation_done` but before copied rules can skip them forever; and treating an old row with no new marker as first activity can replay resets and pipelines. Acceptance samples only one installed rule and one custom rule, leaving the remaining installed lifecycle inventory unproved.","finding_id":"GHDM-R2-F06","fix":"Give copied SessionStart evaluation separate durable pending/running/done state, an idempotency key for background effects, crash takeover, and an explicit pre-deploy-row policy. Activation completion must not imply copied-rule completion. Test real-versus-synthetic parity for every installed SessionStart rule plus one custom rule under concurrent, crash, and replay cases.","introduced_in_round":1,"location":"P3 / §3.1 copied SessionStart lifecycle","prevention":"Inventory every installed lifecycle effect, classify idempotency, and test double-run, crash windows, replay, and migration before synthesizing the event.","principle":"Synthetic lifecycle effects require their own durable exactly-once and crash-recovery protocol.","root_cause":"Copied rule evaluation was tied informally to activation completion even though its installed effects include non-idempotent background MCP calls and independent state resets.","section_id":"3.1","severity":"blocking"},{"category":"unhandled-edge","causal_finding_id":"GHDM-R1-F07","causal_section_ids":["2.1","3.1"],"check_key":"edge-case-coverage","description":"Registration can return an empty ID after failure; code-index and wiki setup swallow exceptions; agent resolution can return `None`; profile and transcript setup are best effort. The plan never says which failures permit `_materialize_activation_done`. Marking done loses required retries, while leaving it unset and allowing the current hook violates the promise that no rule or tool boundary sees half-activation.","finding_id":"GHDM-R2-F07","fix":"Define per-step durable guards and required/best-effort classification. A required failure must leave its guard incomplete and return a retryable or fail-safe hook outcome before copied or live rules run; best-effort failures may log and permit completion. Add transient-failure tests for registration, agent resolution, variable seeding, and transcript setup.","introduced_in_round":1,"location":"P2-P3 / materialization activation completion","prevention":"Classify every activation step as required or best-effort and specify success, retry, and current-hook outcome before adding a global done marker.","principle":"A completion marker is valid only after every required step succeeds; best-effort steps need an explicit separate policy.","root_cause":"Existing activation constituents swallow or transform failures differently, while the repaired protocol treats activation as one undifferentiated completed operation.","section_id":"3.1","severity":"blocking"},{"category":"unhandled-edge","causal_finding_id":"GHDM-R1-F03","causal_section_ids":["2.1","3.1"],"check_key":"edge-case-coverage","description":"The current compact SessionStart rule renders the continuation and immediately clears required/advisory skill lists. After SessionStart output becomes empty, merely retaining `compact_handoff_inject_pending` leaves the later non-Grok turn-start packet without skill-reload content. Allowing normal enrichment would violate the new no-SessionStart-context contract.","finding_id":"GHDM-R2-F08","fix":"Split compact handling into arming and delivery phases. Compact SessionStart must set pending state and retain summary, required-skill, and advisory-skill fields without emitting context. The first non-Grok context-capable event renders and atomically clears only included fields; Grok remains owned by PostCompact. Add a non-Grok compact test for empty SessionStart followed by complete continuation.","introduced_in_round":1,"location":"P2-P3 / compact SessionStart arming and later non-Grok delivery","prevention":"For every deferred packet, inventory every variable read, cleared, or marked by the original delivery point and prove the successor sees the complete set.","principle":"An ignored lifecycle response must retain every source field needed by the later confirmed delivery owner.","root_cause":"The repaired plan strips SessionStart context but says only to preserve the pending flag; the current SessionStart rule also consumes required and advisory skill lists.","section_id":"3.1","severity":"blocking"},{"category":"missing-requirement","check_key":"requirement-owner","description":"Repointing the older plan, closing or obsoleting #20635/#20727/#20733-#20735, removing #20724’s dependency, and rewriting its live check are required, but none of the five manifest leaves owns them. The referenced tasks remain open and retain the conflicting tree, so expansion has no executable step that performs the cleanup.","finding_id":"GHDM-R2-F09","fix":"Assign collision cleanup to an explicit coordinator-owned pre-expansion transaction, or add a typed deferred/deliverable section whose task owns the old-plan amendment and task/dependency retirement. Remove the requirement from V2 prose only after a real owner exists.","location":"Collision framing and V2 item 6","participating_section_ids":["Collision with compact-summary-fidelity (#20724)","V2"],"prevention":"Map every repository and Gobby-state requirement to a deliverable, coordinator transaction, or valid deferred section before deriving the manifest.","principle":"Every required task/dependency migration needs a manifest-producing owner or typed deferral.","root_cause":"Collision cleanup is written as post-epic prose under non-deliverable sections.","section_id":"Collision with compact-summary-fidelity (#20724)","severity":"blocking"},{"category":"traceability","check_key":"acceptance-observability","description":"Rule behavior tests can pass with stale bundled-content digests. Nothing in §4.1 acceptance or V2 requires `test_bundled_content_manifest_matches_tree`, so the generated carrier can remain inconsistent with the changed templates.","finding_id":"GHDM-R2-F10","fix":"Add the bundled-manifest regression test Target and a dedicated acceptance item requiring exact equality after regeneration.","location":"P4 / §4.1 bundled-template manifest refresh","prevention":"Whenever a template change updates a generated manifest, add its canonical equality test to Targets, acceptance, and the focused validation command.","principle":"Every generated carrier changed by a deliverable needs an explicit validation artifact.","repairs":[{"entries":["`tests/install/test_bundled_content_manifest.py::*` — scope-reason: validate regenerated bundled-content digests against the committed shared-template tree"],"kind":"add_targets","section_id":"4.1"},{"items":[{"artifact":"test: `tests/install/test_bundled_content_manifest.py::test_bundled_content_manifest_matches_tree`","prose":"Regenerated bundled-content manifest exactly matches the committed shared-template tree"}],"kind":"add_acceptance","section_id":"4.1"}],"root_cause":"The plan targets `bundled_content_manifest.json` and requires regeneration, while acceptance and V2 omit the repository’s exact tree-equality regression.","section_id":"4.1","severity":"blocking"},{"category":"weak-testability","check_key":"acceptance-observability","description":"Acceptance 1.1.1 checks only additionalContext, and 1.1.3 checks that observe hooks do not emit additionalContext. The existing `GROK_SYSTEM_MESSAGE_CONTEXT_HOOKS` constant and passive-hook systemMessage mappings can survive while all current acceptance passes.","finding_id":"GHDM-R2-F11","fix":"Add acceptance requiring deletion of the Grok system-message constant and a complete passive-hook matrix showing neither additionalContext nor systemMessage is emitted.","location":"P1 / §1.1 capability acceptance","prevention":"Derive an acceptance row for every changed capability dimension and every deleted compatibility constant.","principle":"Acceptance must cover every independent provider channel the implementation removes.","repairs":[{"items":[{"artifact":"test: `tests/adapters/test_capabilities.py`","prose":"Grok system-message compatibility constant is removed and every passive Grok hook exposes ContextChannel.NONE with neither additionalContext nor systemMessage output"}],"kind":"add_acceptance","section_id":"1.1"}],"root_cause":"The capability repair added full body prose but retained acceptance focused on additionalContext.","section_id":"1.1","severity":"blocking"},{"category":"unhandled-edge","causal_finding_id":"GHDM-R1-F08","causal_section_ids":["4.1"],"check_key":"edge-case-coverage","description":"Startup, compact, and clear briefings are mandatory one-shot components whose delivered flags stay false until flush. Dropping the oldest briefing can permanently erase a component no producer recreates, contradicting the plan’s deny-once and at-least-once guarantees; a single component larger than the cap is also unspecified.","finding_id":"GHDM-R2-F12","fix":"Use class-aware bounds. Keep briefing as stable deduplicated references to durable source state, compact or reject an incoming oversize component without evicting an older required one, and reserve drop-oldest for disposable turn context. Add overflow tests across startup, compact, clear, and single-component oversize cases.","introduced_in_round":1,"location":"P4 / §4.1 buffer bounds","prevention":"Define bounds separately by delivery class, including cap units, deduplication, overflow, and single-component oversize behavior.","principle":"A bounded mandatory-delivery queue must preserve every undelivered one-shot or explicitly reject it without evicting older required state.","root_cause":"The repair applies one drop-oldest policy to disposable turn context and mandatory briefing components.","section_id":"4.1","severity":"blocking"}],"reviewer_session":"#11036","round":2,"verdict":"needs_review"},"session_id":"6dcc8341-5b1c-4682-a578-2c012b601e65"}
```

**Round 3** `kind: verification`

- reviewer_run: 04270f05-dba4-4064-aaa3-47abe30065fb
- reviewer_session: #11047
- verdict: needs_review
- findings:
- GHDM-R3-F01/blocking/2.1 extraction still carries 3.1 guard and `_materialize_activation_done` semantics — accepted (unattended vote, standing accept-all directive): correct leaf-independence defect; 2.1 becomes a literal extraction, all guarded idempotent transformation moves to 3.1.
- GHDM-R3-F02/blocking/3.1 requires briefing enqueue whose only primitive and acceptance live in 4.1 — accepted: 3.1 will own the minimal structured-component enqueue-if-absent primitive plus activation wiring; 4.1 extends it with stash/flush/commit.
- GHDM-R3-F03/blocking/preserved SessionStart sources lack binding acceptance beyond compact/pre-created — accepted; typed repair applied.
- GHDM-R3-F04/blocking/collision transaction references the epic task before expansion creates it and edits a registered plan freehand — accepted: transaction split into a realizable pre-expansion phase (existing-task retirements via service calls) and a post-expansion repoint phase once the epic id exists, with registry/coverage revalidation.
- GHDM-R3-F05/blocking/5.1 smoke branches missing stable acceptance — accepted; typed repair applied.
- GHDM-R3-F06/blocking/no PreToolUse channel acceptance for Grok capability correction — accepted; typed repair applied.
- GHDM-R3-F07/blocking/reserved-variable rejection tested only at MCP entry point — accepted; typed repair applied (HTTP + workflow-effect seams).
- GHDM-R3-F08/blocking/no policy for session_start webhooks under deferral — accepted: plan will state an explicit webhook policy with ordering, blocking propagation, exactly-once, targets, and tests.
- GHDM-R3-F09/blocking/`finalize_staged_memory_delivery` marks memory IDs before Grok flush commit — accepted; typed repair applied.
- GHDM-R3-F10/blocking/agent+wiki delivered-marker producers absent from 4.1 targets — accepted; typed repair applied.
- GHDM-R3-F11/blocking/direct EventEnricher/route-timeout suites missing from 4.1 targets — accepted; typed repair applied.
- GHDM-R3-F12/blocking/SessionStart cannot be empty while HookManager merges rule context; `clear-pending-context-reset-on-start` still clears state early — accepted: explicit SessionStart output barrier plus arming-only conversion of retained inject producers.
- GHDM-R3-F13/blocking/fail-open on first gating hook lets tools run over half-activated state — accepted: retriable deny for gating hooks until required activation completes; fail-open retained for passive hooks only.
- GHDM-R3-F14/blocking/copied-rule claim losers proceed over stale state; reconciliation unordered — accepted: stateful phase linearized before any racer's live evaluation (activation → reconciliation → copied state → live rules/handler), background phase async.
- GHDM-R3-F15/blocking/copied `auto-run-pipeline` cannot be exactly-once via pending→running→done — accepted: durable idempotency key / transactional-outbox contract for external copied effects.
- GHDM-R3-F16/blocking/worker-side commit cannot observe route timeout; claims lack durable lease — accepted: commit moves to the async route after await success; claims persist owner, timestamp, lease with stale takeover requeue.
- GHDM-R3-F17/blocking/concurrent hooks can enqueue duplicate P2P components — accepted: atomic upsert by stable message-derived component_id.
- GHDM-R3-F18/blocking/no numeric buffer caps; oversize briefing truncation acks undelivered tail — accepted: exact count/byte caps, briefing coalescing bound, remainder retained until fully delivered.
- GHDM-R3-F19/blocking/compact/clear source markers consumed before enqueue with no retryable handoff — accepted: source-marker take + durable reference + enqueue in one transaction/lock domain or retryable scheduled-pending marker.
- GHDM-R3-F20/blocking/`_deferred_materialization` stamped after `register_session` leaves an unclassifiable crash window — accepted: deferred marker initialized atomically with row creation.
- resolution_notes: All 20 findings accepted under the user's standing accept-all directive for this review loop (unattended mode; coordinator judged each finding legitimate and material). Twelve findings are fixer-induced from round-2 repairs (F01, F02, F04, F09, F10, F13, F14, F15, F16, F17, F18, F20 per causal metadata); eight are net-new, so the all-fixer-induced early-stop rule does not fire. Typed repairs for F03, F05, F06, F07, F09, F10, F11 applied via apply_plan_review_repairs after this checkpoint; remaining prose fixes hand-applied to §§1.1, 2.1, 3.1, 4.1, 5.1, the Collision section, Locked decisions, and V2 before base validation and round 4.

```json plan-review-round
{"evidence_id":"a15aefd1-163c-41a4-a460-fb642893e884","plan_hash":"4f31b22114d158ab8a1fa1af6b3a14ee31ddf91689a1d3111231179ed6c03495","round_number":3,"round_result":{"coverage_attestation":{"adjacent_variant_complete":true,"attestation_digest":"762f512dbbb2fbe2a6aeba9c372999c351909ee9bf77a2987d950c96cddcfc73","cross_lane_interaction_complete":true,"disposition_counts":{"dismissed":4,"emitted_findings":20,"total":24},"evidence_id":"a15aefd1-163c-41a4-a460-fb642893e884","lanes":[{"candidate_count":8,"lane_id":"requirements_traceability","status":"completed"},{"candidate_count":6,"lane_id":"repository_blast_radius","status":"completed"},{"candidate_count":10,"lane_id":"runtime_invariants","status":"completed"}],"shadow_manifest_status":{"entry_count":5,"manifest_digest":"b2888ad276e1f1bd2eb75de0a540d9cdcb0e95b1dfd63a5438bf94601701daa0","status":"valid"},"source_digest":"66fcef7111850e8d76cc256e189cd6fd6ab78b7f25916484f4be83ccc3a896ee","version":1},"findings":[{"category":"bad-sequencing","causal_finding_id":"GHDM-R2-F03","causal_section_ids":["2.1","3.1"],"check_key":"atomic-leaf-cutover","description":"Section 2.1 says runtime behavior stays identical, yet `activate_materialized_session` already promises per-step durable guards and writes `_materialize_activation_done`, whose semantics live in 3.1. A 2.1-only agent must either change durable behavior early or consult a downstream section.","finding_id":"GHDM-R3-F01","fix":"Make 2.1 a literal extraction of the current activation body with no new guards or completion marker. Specify and implement the guarded idempotent transformation entirely in 3.1.","introduced_in_round":2,"location":"P2-P3 / §2.1 activation helper contract","prevention":"Simulate each leaf alone and remove every downstream marker, guard, or protocol from extraction-only sections.","principle":"A behavior-preserving extraction must not require downstream state semantics or introduce their durable markers.","root_cause":"The round-2 atomic-cutover repair left 3.1 guard and completion semantics in the helper that 2.1 must implement independently.","section_id":"2.1","severity":"blocking"},{"category":"bad-sequencing","causal_finding_id":"GHDM-R2-F05","causal_section_ids":["3.1","4.1"],"check_key":"atomic-leaf-cutover","description":"Section 3.1 requires atomic Grok startup-briefing enqueue before any first hook proceeds, while `grok_pending_context.py` and the buffer mechanics exist only in downstream 4.1; 4.1 also omits `session_materialize.py`, where the prerequisite must be wired. Neither leaf independently owns the complete behavior.","finding_id":"GHDM-R3-F02","fix":"Have 3.1 create and target the minimal structured-component enqueue-if-absent primitive plus activation wiring, with 4.1 extending it for stash/flush/commit, or merge 3.1 and 4.1.","introduced_in_round":2,"location":"P3-P4 / startup-briefing activation prerequisite","prevention":"For every cross-leaf call, verify the callee module, target, and acceptance exist in the caller leaf or an earlier dependency.","principle":"A prerequisite mutation must be owned by the leaf that needs it or by a completed predecessor.","root_cause":"The race repair assigns activation-time enqueue to 3.1 while assigning its only primitive and acceptance to dependent 4.1.","section_id":"3.1","severity":"blocking"},{"category":"weak-testability","check_key":"acceptance-observability","description":"The constraint preserves compact, resume, clear, web-chat, and pre-created SessionStart binding. Acceptance 3.1.3 covers only compact/pre-created binding, while 3.1.11 checks only empty output for the larger matrix, so resume, clear, or web-chat identity can regress while every criterion passes.","finding_id":"GHDM-R3-F03","fix":"Add a parameterized acceptance item proving every preserved SessionStart source binds or creates the intended canonical row without duplication.","location":"P3 / §3.1 preserved SessionStart binding matrix","prevention":"Cross product each preserved SessionStart source with row identity, duplicate prevention, and output-channel assertions.","principle":"Every preserved lifecycle branch needs acceptance for both identity binding and response output.","repairs":[{"items":[{"artifact":"test: `tests/hooks/test_session_events_coverage.py::test_session_start_binding_matrix`","prose":"Compact, resume, clear, web-chat, and pre-created SessionStart paths each bind or create the intended canonical row without duplication"}],"kind":"add_acceptance","section_id":"3.1"}],"root_cause":"The broader acceptance matrix asserts empty output while only compact/pre-created paths assert binding.","section_id":"3.1","severity":"blocking"},{"category":"bad-sequencing","causal_finding_id":"GHDM-R2-F09","causal_section_ids":["Collision with compact-summary-fidelity (#20724)","V2"],"check_key":"requirement-owner","description":"The transaction repoints compact-summary-fidelity §3 to “this epic” before expansion, although that epic task is created by expansion. It also amends the registered old plan without requiring its plans-row hash and managed coverage ledger to be regenerated and validated. The stated pre-expansion gate cannot complete consistently.","finding_id":"GHDM-R3-F04","fix":"Define a realizable service-owned sequence that establishes a valid new task reference, amends the old plan through `gobby-plans`, updates its registry hash and managed coverage/ledger, validates them, retires conflicting tasks/dependencies, then releases expansion.","introduced_in_round":2,"location":"Collision transaction / steps 1-5 and V2 item 6","prevention":"Resolve task-reference creation order and enumerate plan row, file hash, managed coverage, ledger, and dependency mutations before declaring a transaction executable.","principle":"A pre-expansion migration must reference resources that already exist and update every managed representation atomically.","root_cause":"The coordinator repair assumes the new epic task exists before build and treats an already-registered plan as a freehand file.","section_id":"Collision with compact-summary-fidelity (#20724)","severity":"blocking"},{"category":"weak-testability","check_key":"acceptance-observability","description":"Acceptance omits Claude first-prompt context, Grok UPS stash/no-output, second PreToolUse allowance, allowing Stop with turn-context only, compact no-duplicate binding, and false-before/true-after delivered-state timing. The leaf can close while those stated smoke behaviors are absent.","finding_id":"GHDM-R3-F05","fix":"Add stable acceptance items for the omitted smoke outcomes.","location":"P5 / §5.1 smoke steps 3-8","prevention":"Map every numbered smoke branch to a stable acceptance ID before manifest derivation.","principle":"Manifest validation criteria must cover every independently falsifiable smoke branch stated in the leaf.","repairs":[{"items":[{"artifact":"test: `tests/e2e/test_grok_session_deferral.py`","prose":"Claude first prompt returns startup context while Grok UPS returns none and leaves a pending briefing"},{"artifact":"test: `tests/e2e/test_grok_session_deferral.py`","prose":"After the one briefing denial, a second PreToolUse allows absent another gate and Stop with only turn-context also allows"},{"artifact":"test: `tests/e2e/test_grok_session_deferral.py`","prose":"Compact binding creates no duplicate row and delivered-state markers remain false before flush then become true after commit"}],"kind":"add_acceptance","section_id":"5.1"}],"root_cause":"Eight smoke steps were compressed into three criteria that omit several state and retry outcomes.","section_id":"5.1","severity":"blocking"},{"category":"weak-testability","check_key":"acceptance-observability","description":"Grok PreToolUse accepts only decision, reason, and updatedInput, but acceptance 1.1.5 covers only passive hooks. An implementation can retain or reintroduce systemMessage/additionalContext on PreToolUse while all current criteria pass.","finding_id":"GHDM-R3-F06","fix":"Add explicit PreToolUse capability and translation acceptance proving no systemMessage or additionalContext output.","location":"P1 / §1.1 PreToolUse channel matrix","prevention":"Derive an acceptance row for every event-class and context-channel pair in the provider matrix.","principle":"Acceptance must cover each independent provider channel removed by a capability correction.","repairs":[{"items":[{"artifact":"test: `tests/adapters/test_acp_hook_translation.py`","prose":"Grok PreToolUse emits only deny reason or updatedInput fields and never additionalContext or systemMessage"}],"kind":"add_acceptance","section_id":"1.1"}],"root_cause":"The system-message deletion criterion covers passive hooks, while PreToolUse is active and has a separate native envelope.","section_id":"1.1","severity":"blocking"},{"category":"traceability","check_key":"acceptance-observability","description":"Acceptance 3.1.9 claims rejection through MCP, HTTP, and non-internal rule effects, but only `tests/mcp_proxy/test_top_level_variables.py` is targeted. The HTTP route and `EffectsMixin._apply_set_variable` can remain writable while the criterion passes.","finding_id":"GHDM-R3-F07","fix":"Target the HTTP and workflow-effect test seams and add separate acceptance for both.","location":"P3 / §3.1 reserved-variable write boundaries","prevention":"Inventory every public and internal mutation entry point for reserved state and attach a direct test artifact to each.","principle":"A cross-entry-point security boundary needs direct validation at every independent write surface.","repairs":[{"entries":["`tests/servers/routes/test_session_variables.py::*` — scope-reason: prove HTTP set_variable rejects runtime-reserved materialization markers","`tests/workflows/test_hooks.py::*` — scope-reason: prove non-internal set_variable rule effects reject runtime-reserved materialization markers"],"kind":"add_targets","section_id":"3.1"},{"items":[{"artifact":"test: `tests/servers/routes/test_session_variables.py`","prose":"HTTP session-variable writes reject every runtime-reserved materialization marker"},{"artifact":"test: `tests/workflows/test_hooks.py`","prose":"Non-internal workflow set_variable effects reject every runtime-reserved materialization marker"}],"kind":"add_acceptance","section_id":"3.1"}],"root_cause":"The body names MCP, HTTP, and rule effects, while Targets and acceptance exercise only the MCP entry point.","section_id":"3.1","severity":"blocking"},{"category":"missing-requirement","check_key":"requirement-owner","description":"A sessionless startup returns before SessionStart webhooks, and the copied first-activity path mentions only rules. The plan never answers whether configured blocking and non-blocking `session_start` webhooks replay on first activity, whether a replayed block gates the live event, or whether these webhook semantics are intentionally retired.","finding_id":"GHDM-R3-F08","fix":"Choose and document the webhook policy. For parity, evaluate both webhook classes on the canonical synthetic event with explicit ordering, blocking propagation, exactly-once behavior, Targets, and tests; for retirement, state and test the breaking contract.","location":"P3 / deferred SessionStart webhook lifecycle","prevention":"Inventory rules, blocking webhooks, broadcasts, and external observers whenever a lifecycle event is deferred or synthesized.","principle":"Deferring a lifecycle event requires an explicit preserve-or-retire decision for every existing observer and gate.","root_cause":"The copied path replays rules only, while current SessionStart also runs blocking and non-blocking webhooks.","section_id":"3.1","severity":"blocking"},{"category":"traceability","causal_finding_id":"GHDM-R2-F04","causal_section_ids":["4.1"],"check_key":"targets-complete","description":"`finalize_staged_memory_delivery` marks recall IDs injected before Grok turn-context is flushed. An allowing Stop can drop that context after the IDs already suppress retry, and the producer is absent from 4.1 Targets.","finding_id":"GHDM-R3-F09","fix":"Move Grok memory ID mutation into component ack commit and add drop/retry coverage while preserving non-Grok inline delivery.","introduced_in_round":2,"location":"P4 / staged memory recall acknowledgment","prevention":"Trace each context producer through selection, render, marker mutation, drop, retry, and confirmed commit.","principle":"Every producer-side delivery marker must move to the confirmed commit boundary with its producer in Targets.","repairs":[{"entries":["`src/gobby/workflows/engine/delivery_formatting.py::finalize_staged_memory_delivery`"],"kind":"add_targets","section_id":"4.1"},{"items":[{"artifact":"test: `tests/hooks/test_grok_pending_context.py`","prose":"Grok staged-memory IDs remain retryable through stash and drop and are recorded only by confirmed component commit"}],"kind":"add_acceptance","section_id":"4.1"}],"root_cause":"The claim/commit repair inventories P2P and several one-shots but misses `injected_memory_ids`.","section_id":"4.1","severity":"blocking"},{"category":"traceability","causal_finding_id":"GHDM-R2-F04","causal_section_ids":["4.1"],"check_key":"targets-complete","description":"Agent instructions set `_agent_context_injected` during composition, and the wiki rule sets `wiki_overview_injected` while producing ignored Grok UPS context. Those producers are absent from 4.1 Targets, so races can suppress staging before confirmed delivery.","finding_id":"GHDM-R3-F10","fix":"Target each producer or a shared effect-to-ack seam, defer Grok mutations until component commit, retain current non-Grok behavior, and extract from `effects.py` if needed to preserve the source-size ceiling.","introduced_in_round":2,"location":"P3-P4 / agent and wiki delivered markers","prevention":"Search every delivered/injected marker assignment and place its producer or a shared ack seam in Targets.","principle":"A commit-only delivery contract must inventory and defer every marker mutated during composition.","repairs":[{"entries":["`src/gobby/hooks/event_handlers/_agent.py::AgentEventHandlerMixin._inject_agent_instructions_if_needed`","`src/gobby/install/shared/workflows/rules/context-handoff/inject-wiki-overview.yaml::*` — scope-reason: defer Grok wiki delivered-state mutation until confirmed component commit","`src/gobby/workflows/engine/effects.py::*` — scope-reason: introduce or extract the shared Grok deferred-ack effect seam without crossing the production monolith ceiling"],"kind":"add_targets","section_id":"4.1"},{"items":[{"artifact":"test: `tests/hooks/test_grok_pending_context.py`","prose":"Agent, wiki, profile, and startup one-shot markers remain unset through Grok composition and stash and flip only on confirmed component commit"}],"kind":"add_acceptance","section_id":"4.1"}],"root_cause":"The repair names `_startup_context_injected` and `wiki_overview_injected` but omits their real marker-producing helpers and `_agent_context_injected`.","section_id":"4.1","severity":"blocking"},{"category":"traceability","check_key":"targets-complete","description":"4.1 changes EventEnricher’s piggyback accounting and the route executor timeout boundary, but omits the suites that hard-code immediate `mark_delivered_batch`, formatting retries, bounded workers, timeout fallback, and late execution.","finding_id":"GHDM-R3-F11","fix":"Add the direct EventEnricher, route, and timeout suites with Grok enqueue-without-ack and late-worker claim assertions.","location":"P4 / EventEnricher and executor timeout test seams","prevention":"For every changed exact symbol, locate its direct unit/timeout tests and target them before relying on higher-layer coverage.","principle":"Changed branches and concurrency boundaries require their direct existing regression suites in Targets.","repairs":[{"entries":["`tests/hooks/test_event_enrichment.py::*` — scope-reason: update piggyback membership, enqueue-without-ack, formatting failure, and retained non-Grok delivery cases","`tests/servers/test_mcp_routes.py::*` — scope-reason: cover the real hook executor timeout and fallback boundary for claim release","`tests/workflows/test_hook_evaluation_timeout.py::*` — scope-reason: preserve bounded-worker and timeout recovery behavior while adding Grok claims"],"kind":"add_targets","section_id":"4.1"},{"items":[{"artifact":"test: `tests/hooks/test_event_enrichment.py`","prose":"EventEnricher preserves non-Grok inline delivery while Grok enqueues without acknowledgment across formatting and retry failures"},{"artifact":"test: `tests/servers/test_mcp_routes.py`","prose":"The real route timeout returns fallback, releases the exact Grok claim, and rejects a late worker commit without exceeding worker bounds"}],"kind":"add_acceptance","section_id":"4.1"}],"root_cause":"Provider-contract tests were added while EventEnricher and `_run_adapter_hook` direct suites were omitted.","section_id":"4.1","severity":"blocking"},{"category":"unhandled-edge","check_key":"edge-case-coverage","description":"Stripping `_compose_session_response` cannot make SessionStart empty because HookManager later merges rule-generated workflow context from compact/task/profile rules. Separately, `clear-pending-context-reset-on-start` still clears `pending_context_reset` during the empty compact SessionStart, so deferred non-Grok delivery loses part of the continuation.","finding_id":"GHDM-R3-F12","fix":"Add an explicit SessionStart output barrier, convert every retained inject producer to arming/state-only behavior, and move `pending_context_reset` clearing to the confirmed non-Grok delivery owner. Target and test the full rule inventory.","location":"P3-P4 / SessionStart output barrier and compact retained fields","prevention":"Inventory all lifecycle rule injects and clears, then trace their output and state through the new delivery owner.","principle":"Deferring response context requires every rule-side producer and clear mutation to become arming-only until confirmed delivery.","root_cause":"The repair strips handler composition and edits the main compact rule, while HookManager still merges workflow context and a separate rule still clears pending reset state.","section_id":"3.1","severity":"blocking"},{"category":"unhandled-edge","causal_finding_id":"GHDM-R2-F07","causal_section_ids":["3.1"],"check_key":"first-activity-activation-and-briefing-barrier","description":"On first PreToolUse, a required activation/seed failure returns allow even though the plan promises no tool boundary crosses half-activated state and the startup briefing remains owed. The tool can execute before required startup state exists.","finding_id":"GHDM-R3-F13","fix":"Keep state retryable and use a retriable deny/block for first gating hooks until required activation completes; retain fail-open only for passive hooks. Add same-event failure and next-hook recovery tests.","introduced_in_round":2,"location":"P3-P4 / required activation failure on first gating hook","prevention":"Specify the current-hook outcome separately for passive, PreToolUse, and Stop failure branches.","principle":"A gating hook must fail safely when prerequisites required before the tool or stop boundary are incomplete.","root_cause":"The failure-classification repair applies one fail-open outcome to passive and gating hooks.","section_id":"3.1","severity":"blocking"},{"category":"unhandled-edge","causal_finding_id":"GHDM-R2-F06","causal_section_ids":["3.1"],"check_key":"first-activity-activation-and-briefing-barrier","description":"A copied-rule claim loser proceeds while the winner may still be applying plan-mode, discovery, skill, or task resets. Its live rules/handler can observe stale state; copied rules may also run before existing `reconcile_session_activation` has exposed the resolved agent/workflow state.","finding_id":"GHDM-R3-F14","fix":"Split copied processing into a stateful phase that every racer must complete or help before live evaluation and a background phase that may continue asynchronously. Specify and test materialization activation → reconciliation → copied state → live rules/handler.","introduced_in_round":2,"location":"P3 / copied SessionStart loser and reconciliation ordering","prevention":"Walk winner/loser schedules and name the exact linearization order for materialization, reconciliation, copied state effects, live rules, and handler execution.","principle":"All stateful startup resets and reconciliation must linearize before any racer evaluates live rules or crosses a handler boundary.","root_cause":"The single-owner repair lets claim losers proceed immediately and does not place existing activation reconciliation relative to copied evaluation.","section_id":"3.1","severity":"blocking"},{"category":"unhandled-edge","causal_finding_id":"GHDM-R2-F06","causal_section_ids":["3.1"],"check_key":"edge-case-coverage","description":"`pending → running → done` cannot make `auto-run-pipeline` exactly once. A crash after the background call is scheduled and before `done` permits duplicate takeover; marking done first can lose a failed dispatch. A local one-shot variable has the same atomicity gap.","finding_id":"GHDM-R3-F15","fix":"Give external copied effects a durable consumer-accepted idempotency key or a transactional outbox with pending/running/succeeded state and crash-recovery tests.","introduced_in_round":2,"location":"P3 / copied auto-run-pipeline crash window","prevention":"For every copied rule effect, classify external side effects and test crash-before-dispatch, crash-after-dispatch, failure, and takeover.","principle":"A claim around non-idempotent external work does not provide exactly-once semantics without a consumer idempotency key or transactional outbox.","root_cause":"The repair equates rule-evaluator return with completion even though `auto-run-pipeline` schedules a background MCP call.","section_id":"3.1","severity":"blocking"},{"category":"unhandled-edge","causal_finding_id":"GHDM-R2-F04","causal_section_ids":["4.1"],"check_key":"bounded-atomic-pending-context-delivery","description":"`ACPHookAdapter.handle_native` runs inside the worker and cannot know whether outer `asyncio.wait_for` returned in budget. A near-boundary timeout can return fallback after worker commit. Claims also lack an in-flight lease, so daemon death after claim can strand payload and acknowledgments.","finding_id":"GHDM-R3-F16","fix":"Return translated payload plus an opaque durable claim token from the worker; commit only in the async route after await succeeds. Persist claimed components with owner/request ID, timestamp, and lease so timeout, crash, and stale takeover requeue the exact claim. Target the real route timeout suites.","introduced_in_round":2,"location":"P4 / adapter executor claim commit and crash recovery","prevention":"Trace claim ownership through translation, await success, timeout, late worker, daemon death, and stale takeover before choosing commit points.","principle":"Delivery acknowledgment must linearize after the last boundary that knows the response succeeded and must recover abandoned in-flight claims.","root_cause":"The claim/commit repair assigns commit to the executor worker even though timeout success is known only by the async route and gives claims no durable lease.","section_id":"4.1","severity":"blocking"},{"category":"unhandled-edge","causal_finding_id":"GHDM-R2-F04","causal_section_ids":["4.1"],"check_key":"bounded-atomic-pending-context-delivery","description":"Concurrent hooks can read the same `delivered_at IS NULL` P2P rows and enqueue duplicate components. A later `mark_delivered_batch` cannot retract duplicates already buffered, so the same message can ride multiple denials or blocked Stops.","finding_id":"GHDM-R3-F17","fix":"Atomically upsert message components by a stable message-derived `component_id`, or introduce a durable message claim lease tied to the delivery claim. Add controlled two-hook and stale-buffer tests.","introduced_in_round":2,"location":"P4 / concurrent pending P2P selection","prevention":"Test two concurrent eligible hooks selecting the same undelivered row and trace duplicates through timeout, requeue, and later commit.","principle":"A deferred message may have multiple observing hooks, so enqueue must deduplicate at the message identity boundary.","root_cause":"The repair makes message selection non-claiming and delays acknowledgment but limits enqueue-if-absent to briefing one-shots.","section_id":"4.1","severity":"blocking"},{"category":"unhandled-edge","causal_finding_id":"GHDM-R2-F12","causal_section_ids":["4.1"],"check_key":"bounded-atomic-pending-context-delivery","description":"The plan gives no numeric caps or byte definition, leaves never-evicted briefing state unbounded, and says an oversize briefing is truncated then fully acknowledged. The omitted tail is never delivered even though acceptance 4.1.8 says interleavings lose nothing.","finding_id":"GHDM-R3-F18","fix":"Define exact component and serialized-byte caps, bounded briefing coalescing/cardinality, and provider reason/context budgets. Split or retain truncated remainder and run final ack only after the full mandatory component is delivered.","introduced_in_round":2,"location":"P4 / buffer caps and oversize briefing","prevention":"Specify numeric count/size limits, serialization units, coalescing, multibyte behavior, and partial-delivery acknowledgment.","principle":"A bounded mandatory-delivery buffer must define exact units and preserve every undelivered remainder until acknowledgment.","root_cause":"The class-aware repair removes any total briefing bound and treats provider truncation as full component delivery.","section_id":"4.1","severity":"blocking"},{"category":"unhandled-edge","check_key":"edge-case-coverage","description":"PostCompact consumes compact source state before briefing enqueue, and clear marks the predecessor handoff consumed before successor seeding/arming completes. A crash or write failure between those steps leaves no retriable marker and no briefing.","finding_id":"GHDM-R3-F19","fix":"Put source-marker take, durable reference creation, and enqueue in one transaction/lock domain, or retain a retryable scheduled-pending marker until enqueue succeeds. Add crash-window tests for both paths.","location":"P3-P4 / compact and clear briefing arming","prevention":"Inject failures after every marker take and before every enqueue for compact and clear successors.","principle":"Consuming a durable source marker and enqueuing its delivery reference must share a transaction or a retryable handoff state.","root_cause":"Current compact/clear flows consume or mark the source before the later planned buffer enqueue.","section_id":"4.1","severity":"blocking"},{"category":"unhandled-edge","causal_finding_id":"GHDM-R2-F06","causal_section_ids":["3.1"],"check_key":"edge-case-coverage","description":"A crash after `register_session` inserts the row and before `_deferred_materialization` is stamped leaves a new deferred row indistinguishable from a pre-deploy row. The next hook treats copied SessionStart rules as already done and can skip resets or pipeline startup forever.","finding_id":"GHDM-R3-F20","fix":"Initialize the deferred marker in the same database transaction as row creation, or persist an atomic creation-epoch/schema field that recovery can classify unambiguously. Target the row-creation owner and add crash recovery coverage.","introduced_in_round":2,"location":"P3 / deferred-row pre-deploy classification","prevention":"Enumerate crash points between row insert, cache publication, metadata stamp, and next-hook recovery.","principle":"A recovery discriminator must be persisted atomically with the state transition it classifies.","root_cause":"The pre-deploy policy relies on a session variable stamped after `register_session` returns.","section_id":"3.1","severity":"blocking"}],"reviewer_session":"#11047","round":3,"verdict":"needs_review"},"session_id":"6dcc8341-5b1c-4682-a578-2c012b601e65"}
```

**Round 4** `kind: verification`

- reviewer_run: 6a58d9b7-f02e-4d99-b4eb-39b5da855ea8
- reviewer_session: #11049
- verdict: needs_review
- findings:
- GHDM-R4-F01/blocking/Phase A closes #20635 as duplicate while compact-summary-fidelity §3 still points at it; Phase B repoint lacks provenance — accepted (unattended): the carrier stays open; §3 task_ref never moves, #20635 gains a dependency on this plan's epic and closes as completed/already_implemented only after delivery.
- GHDM-R4-F02/blocking/Phase B has no enforceable pre-dispatch interval — accepted (unattended): Phase B rewritten on the expansion-without-automation path; `gobby build` is the explicit automation opt-in, so expansion runs first, Phase B completes and validates, then `gobby build` arms dispatch.
- GHDM-R4-F03/blocking/webhook exactly-once vs takeover-duplicate contradiction — accepted (unattended): webhooks become at-least-once with a stable activation-epoch delivery key in the payload; Locked decision 7 scopes consumer-enforced idempotency to the internal run_pipeline consumer; 3.1.20 revised.
- GHDM-R4-F04/blocking/blocking-webhook claim loser can cross the live boundary — accepted (unattended): blocking-webhook evaluation moves into a durable pre-live barrier substate every racer observes with bounded takeover; only non-blocking webhooks stay in the asynchronous single-owner phase.
- GHDM-R4-F05/blocking/multipart briefing contradicts deny-once — accepted (unattended): multipart removed; every briefing fits one provider-budget response via persisted-context reference degradation, preserving exactly one denial.
- GHDM-R4-F06/blocking/provider-matrix acceptance missing — accepted (unattended): typed repair adds the parameterized provider-matrix acceptance for Claude, Qwen, Droid, Codex, and AGY with Grok as the pending-briefing exception.
- GHDM-R4-F07/blocking/idempotency-store seams outside Targets — accepted (unattended): typed repair adds the execution protocol/store/model, migration 405, embedded-schema, and storage-test targets plus the concurrency acceptance.
- GHDM-R4-F08/blocking/atomic seed unrealizable from the wrapper — accepted (unattended): typed repair adds `_SessionCRUDMixin.register` and the `HookSessionManager` protocol; the seed is inserted inside the fresh-row transaction with conflict/reuse coverage.
- GHDM-R4-F09/blocking/live-claim branch undefined for a second PreToolUse — accepted (unattended): a claim loser returns a retriable non-payload deny until the owner commits or releases; owner-timeout and loser behavior tested separately.
- GHDM-R4-F10/blocking/enqueue_if_absent lacks a committed tombstone — accepted (unattended): delivered/committed component identities are checked inside the same enqueue transaction; commit-before-late-stash interleaving test added.
- GHDM-R4-F11/blocking/registration failure indistinguishable from legitimate absence — accepted (unattended): registration returns a typed outcome distinct from sessionless/excluded shapes, consumed before copied/live phases, tested apart from the exclusion classes.
- GHDM-R4-F12/blocking/cross-row transfer atomicity — accepted (unattended): arming becomes a generation-keyed staging record on the source row with an idempotent destination CAS take; every write is single-row; predecessor→successor and sibling→current crash tests added.
- GHDM-R4-F13/blocking/commit boundary vs response emission — accepted (unattended): commit persists the exact translated payload in the same variable mutation as component acknowledgment, keyed by the hook request id, and replays it on duplicate ingress; requests without a durable id release and stay retryable.
- GHDM-R4-F14/blocking/lease has no duration or clock rule — accepted (unattended): lease is a persisted UTC deadline of the effective outer provider timeout plus a 30-second margin, recomputed from live config at claim time; commit CAS on the claim token gates returning payload content; expiry, restart, and override tests added.
- resolution_notes: All 14 findings accepted under the standing unattended accept-all directive. 13 of 14 carry fixer-induced metadata from round 3; GHDM-R4-F06 is a reviewer-scope gap, so the all-fixer-induced stop condition does not fire. Typed repairs applied for F06/F07/F08. Prose repairs: collision cleanup rewritten so §3's task_ref never moves and expansion precedes the `gobby build` automation opt-in; webhooks documented at-least-once with delivery keys and a durable blocking-webhook barrier; multipart briefing replaced by single-response reference degradation; claim lifecycle gains live-claim loser denial, committed-identity dedup, typed registration outcomes, generation-keyed arming staging, request-id-keyed payload replay at commit, and a numeric lease bound to the effective timeout.

```json plan-review-round
{"evidence_id":"071494f2-59c7-4a47-8e10-4a0d9b2cf07d","plan_hash":"0495143b59ac820c682318a421e8df7409afacbc08e798a6da65f5c91da1ee7a","round_number":4,"round_result":{"coverage_attestation":{"adjacent_variant_complete":true,"attestation_digest":"a0de32e4402525d5de29a67daacdc82fd626d4c2f3109aaaaf6b7c8e3dae0858","cross_lane_interaction_complete":true,"disposition_counts":{"dismissed":6,"emitted_findings":14,"total":20},"evidence_id":"071494f2-59c7-4a47-8e10-4a0d9b2cf07d","lanes":[{"candidate_count":7,"lane_id":"requirements_traceability","status":"completed"},{"candidate_count":4,"lane_id":"repository_blast_radius","status":"completed"},{"candidate_count":9,"lane_id":"runtime_invariants","status":"completed"}],"shadow_manifest_status":{"entry_count":5,"manifest_digest":"5c7c7f9bb3a5ca5e8e5cd466b93c093367d90380e6ba595dbf52c1118205d0d1","status":"valid"},"source_digest":"71bbd860cd0f419875ff17e2fd8aed5389ed644b7687cbfead10dc9c259215c4","version":1},"findings":[{"category":"bad-sequencing","causal_finding_id":"GHDM-R3-F04","causal_section_ids":["Collision with compact-summary-fidelity (#20724)","V2"],"check_key":"requirement-owner","description":"Phase A closes #20635 as `duplicate` while compact-summary-fidelity §3 still points to it, so the required `validate_plan` call must fail. Phase B then points §3 at this plan's external epic, which lacks `deferred-from:compact-summary-fidelity:3` provenance and is not parented under #20724.","finding_id":"GHDM-R4-F01","fix":"Keep #20635 open as the old plan's tail carrier, or create a replacement provenance-labelled carrier under #20724 before closing it. Put the dependency on this plan's epic on that carrier and close it only as `completed` or `already_implemented` after delivery.","introduced_in_round":3,"location":"Collision cleanup / Phase A and Phase B deferral amendments","prevention":"Validate every intermediate amendment against deferral closure, provenance, parentage, and external-prerequisite rules before declaring a multi-step cleanup realizable.","principle":"A registered deferral must continuously name a valid provenance-labelled owner until its obligation is delivered.","root_cause":"The Phase A repair closes the named carrier with an invalid disposition, then Phase B points the deferral at an external epic that cannot serve as the old plan's tail carrier.","section_id":"Collision with compact-summary-fidelity (#20724)","severity":"blocking"},{"category":"bad-sequencing","causal_finding_id":"GHDM-R3-F04","causal_section_ids":["Collision with compact-summary-fidelity (#20724)","V2"],"check_key":"requirement-owner","description":"Phase B has no manifest owner or lifecycle gate. `gobby build` creates the root and immediately invokes the dispatcher before the coordinator receives the root ID, so the coordinator has no guaranteed interval to re-point and validate the old plan before a leaf becomes dispatchable.","finding_id":"GHDM-R4-F02","fix":"Define an enforceable paused-build/lifecycle checkpoint: create the root with automation stopped, perform and validate Phase B using the concrete root ID, record checkpoint completion, and only then resume expansion/dispatch.","introduced_in_round":3,"location":"Collision cleanup / Phase B pre-dispatch checkpoint","prevention":"Trace build-root creation, immediate dispatcher ticks, expansion, and child automation inheritance whenever a plan requires work between root creation and leaf dispatch.","principle":"A required pre-dispatch action needs a service-enforced checkpoint before automation can make descendants eligible.","root_cause":"The repair treats a prose handoff as an interposition point even though `build_plan_file` enables automation and ticks the dispatcher before returning the new root ID.","section_id":"Collision with compact-summary-fidelity (#20724)","severity":"blocking"},{"category":"unhandled-edge","causal_finding_id":"GHDM-R3-F08","causal_section_ids":["3.1"],"check_key":"edge-case-coverage","description":"Locked decision 7 says every external dispatch carries a consumer-enforced idempotency key, but §3.1 expressly tolerates session_start webhook takeover duplicates while acceptance 3.1.20 requires one dispatch. The contracts are incompatible.","finding_id":"GHDM-R4-F03","fix":"Give blocking and non-blocking webhook dispatch a durable activation-epoch idempotency key enforced by an outbox/consumer store, or consistently specify at-least-once webhook behavior and revise Locked decision 7 plus acceptance 3.1.20.","introduced_in_round":3,"location":"P3 / copied SessionStart external-dispatch takeover","prevention":"For each external effect, test crash-before-send, crash-after-send, takeover, and duplicate rejection at the consumer.","principle":"A crash-takeover claim cannot provide exactly-once external effects without consumer idempotency or a durable outbox.","root_cause":"The repair adds consumer idempotency only to `run_pipeline` while treating the webhook claim itself as exactly-once and simultaneously tolerating takeover duplicates.","section_id":"3.1","severity":"blocking"},{"category":"unhandled-edge","causal_finding_id":"GHDM-R3-F08","causal_section_ids":["3.1"],"check_key":"first-activity-activation-and-briefing-barrier","description":"A copied external-phase claim loser proceeds with its live event while the owner evaluates the blocking session_start webhook. A racing PreToolUse can therefore execute even when the owner's copied SessionStart is blocked, violating first-activity parity.","finding_id":"GHDM-R4-F04","fix":"Move blocking webhook evaluation into a durable pre-live result barrier that every racer completes, helps, waits on within a defined bound, or converts to a retriable gate. Keep only non-blocking webhooks in the asynchronous skip-and-proceed phase.","introduced_in_round":3,"location":"P3 / copied blocking-webhook claim loser","prevention":"Walk owner/loser schedules separately for blocking observers and fire-and-forget observers.","principle":"Every racer must observe the startup blocking result before crossing its live handler or tool boundary.","root_cause":"Blocking webhooks were grouped into a skip-and-proceed single-owner phase designed for asynchronous external effects.","section_id":"3.1","severity":"blocking"},{"category":"unhandled-edge","causal_finding_id":"GHDM-R3-F18","causal_section_ids":["4.1"],"check_key":"bounded-atomic-pending-context-delivery","description":"An oversize briefing necessarily leaves a remainder for a later eligible event, causing another PreToolUse denial or Stop continuation. Overview, Locked decision 4, and acceptance 5.1.5 instead require one briefing denial followed by allowance absent another gate.","finding_id":"GHDM-R4-F05","fix":"Choose one contract: guarantee every briefing fits one provider-budget response, or define one denial/block per part and update 4.1/5.1 so retries continue until final-part commit and only the first post-final retry allows.","introduced_in_round":3,"location":"P4-P5 / multipart briefing and deny-once smoke","prevention":"For each size class, trace every part through claim, deny/block, retry, commit, and final allowance.","principle":"Oversize delivery behavior and retry acceptance must describe the same number of gating cycles.","root_cause":"The multipart repair retained remainder across eligible events without revising the one-denial contract.","section_id":"4.1","severity":"blocking"},{"category":"weak-testability","check_key":"acceptance-observability","description":"The plan says moving startup context to first activity fixes Claude, Qwen, Droid, and Codex, yet no acceptance proves Qwen, Droid, or Codex receive that packet; AGY's shared materialization behavior is also unasserted.","finding_id":"GHDM-R4-F06","fix":"Add a parameterized provider matrix naming the expected first-activity native response channel for Claude, Qwen, Droid, Codex, and the supported AGY path, retaining Grok as the pending-briefing exception.","location":"P3-P5 / first-activity startup-context provider coverage","prevention":"Build a provider × first-event × native-channel acceptance matrix from Overview before finalizing leaf criteria.","principle":"A provider-wide delivery requirement needs acceptance for every independent native response channel.","repairs":[{"items":[{"artifact":"test: `tests/hooks/test_hooks_manager.py::test_first_activity_startup_context_provider_matrix`","prose":"A parameterized provider matrix delivers first-activity startup context through the expected native channel for Claude, Qwen, Droid, and Codex, exercises the supported AGY shared-materialization path, and retains Grok as the pending-briefing exception"}],"kind":"add_acceptance","section_id":"3.1"}],"root_cause":"Acceptance proves generic row materialization and the Claude/Grok branches while omitting Qwen, Droid, Codex, and the supported AGY shared path.","section_id":"3.1","severity":"blocking"},{"category":"traceability","causal_finding_id":"GHDM-R3-F15","causal_section_ids":["3.1"],"check_key":"targets-complete","description":"`run_pipeline` cannot atomically reject concurrent duplicate dispatches inside the targeted function alone. The concrete pipeline-execution store has no idempotency field or uniqueness constraint, and every seam that would add one is outside §3.1 Targets.","finding_id":"GHDM-R4-F07","fix":"Target the execution protocol/store/model, add an embedded unique idempotency-key migration, and cover concurrent create/reuse behavior at the storage layer.","introduced_in_round":3,"location":"P3 / run_pipeline consumer-enforced idempotency store","prevention":"Trace idempotency keys from API signature through protocol, model, unique index, insert conflict path, and concurrency tests.","principle":"A concurrency guarantee must target the storage boundary and schema constraint that enforce uniqueness.","repairs":[{"entries":["`src/gobby/mcp_proxy/tools/workflows/_pipeline_execution.py::PipelineExecutionManager.create_execution`","`src/gobby/storage/pipeline_executions.py::PipelineExecutionStorageMixin.create_execution`","`src/gobby/workflows/pipeline_state.py::PipelineExecution`","`crates/gcore/assets/schema/migrations/405_pipeline_execution_idempotency_key.sql`","`crates/gcore/src/schema/assets.rs::*` — scope-reason: embed migration 405 and checksum for pipeline execution idempotency","`tests/storage/test_pipeline_storage.py::*` — scope-reason: concurrent idempotency-key uniqueness and existing-run reuse cover storage behavior"],"kind":"add_targets","section_id":"3.1"},{"items":[{"artifact":"test: `tests/storage/test_pipeline_storage.py::test_create_execution_idempotency_key_is_concurrent_and_durable`","prose":"Concurrent pipeline execution creation with one idempotency key persists exactly one execution and every caller receives that existing execution across process restart"}],"kind":"add_acceptance","section_id":"3.1"}],"root_cause":"The repair targets `run_pipeline` and wrappers while omitting its `create_execution` protocol, concrete insert, persisted model, migration inventory, and direct storage tests.","section_id":"3.1","severity":"blocking"},{"category":"traceability","causal_finding_id":"GHDM-R3-F20","causal_section_ids":["3.1"],"check_key":"targets-complete","description":"The promised same-write `_deferred_materialization` seed is unrealizable from current Targets. Seeding at the wrapper recreates the crash window; the low-level CRUD transaction and `HookSessionManager` protocol are omitted.","finding_id":"GHDM-R4-F08","fix":"Pass the initial-variable seed through the hook protocol and manager wrapper into `_SessionCRUDMixin.register`, insert it inside the fresh-row transaction, and preserve classification across unique-conflict and existing-row reuse branches.","introduced_in_round":3,"location":"P3 / atomic deferred-row discriminator","prevention":"Resolve transaction ownership and signature protocols before assigning an atomic persistence requirement to Targets.","principle":"An atomic seed must be owned by the function whose transaction inserts the row and by every protocol whose signature changes.","repairs":[{"entries":["`src/gobby/storage/sessions/_crud.py::_SessionCRUDMixin.register`","`src/gobby/hooks/session_types.py::HookSessionManager.register_session`"],"kind":"add_targets","section_id":"3.1"},{"items":[{"artifact":"test: `tests/storage/sessions/test_storage_sessions_registration.py::test_deferred_seed_is_atomic_across_insert_conflict_and_reuse`","prose":"The deferred discriminator is inserted inside the low-level fresh-row transaction while unique-conflict recovery and existing-row reuse never relabel pre-deploy rows"}],"kind":"add_acceptance","section_id":"3.1"}],"root_cause":"The repair targets the `SessionManager.register_session` wrapper, which receives the row only after `_SessionCRUDMixin.register` has committed.","section_id":"3.1","severity":"blocking"},{"category":"unhandled-edge","causal_finding_id":"GHDM-R3-F16","causal_section_ids":["4.1"],"check_key":"first-activity-activation-and-briefing-barrier","description":"When one PreToolUse has claimed the startup briefing but has not committed, a second PreToolUse can see no unclaimed briefing and allow its tool before delivery succeeds or times out.","finding_id":"GHDM-R4-F09","fix":"Define active-claim behavior: a claim loser returns a retriable non-payload deny or participates in a bounded per-session barrier until the owner commits/releases. Test owner timeout and loser behavior separately from the single payload-bearing denial.","introduced_in_round":3,"location":"P4 / concurrent active briefing claim","prevention":"Walk two simultaneous gating hooks through queued, claimed, committed, released, and expired states.","principle":"A gating request must distinguish empty work from work already in flight.","root_cause":"The claim contract specifies queued selection, commit, release, and stale takeover without defining the live-claim branch for another PreToolUse.","section_id":"4.1","severity":"blocking"},{"category":"unhandled-edge","causal_finding_id":"GHDM-R3-F02","causal_section_ids":["3.1","4.1"],"check_key":"first-activity-activation-and-briefing-barrier","description":"If PreToolUse commits and removes the startup component before a late just-materialized UPS reaches its enqueue step, `enqueue_if_absent` can insert the same activation-epoch component again and cause a second briefing denial.","finding_id":"GHDM-R4-F10","fix":"Persist a committed identity/tombstone or atomically couple the activation-step guard with enqueue so stale racers check queued, claimed, committed, and delivered states. Add a commit-before-late-stash interleaving test.","introduced_in_round":3,"location":"P3-P4 / enqueue_if_absent after committed removal","prevention":"Test a racer that passed its guard before another request commits and removes the shared component.","principle":"Deduplication must cover queued, claimed, and terminally committed identities across stale racers.","root_cause":"The new primitive checks buffer presence but defines no committed tombstone or atomic activation-guard check.","section_id":"3.1","severity":"blocking"},{"category":"unhandled-edge","causal_finding_id":"GHDM-R3-F13","causal_section_ids":["3.1"],"check_key":"first-activity-activation-and-briefing-barrier","description":"HookManager cannot reliably emit the repaired passive/PreToolUse/Stop outcomes because lookup receives no distinct signal for failed first-event registration. A failed PreToolUse can follow the ordinary sessionless allow path.","finding_id":"GHDM-R4-F11","fix":"Return a typed lookup/materialization result or set an explicit request-local registration-failure marker, consume it before copied/live rules, webhooks, or handlers, and test it separately from SESSION_END, NOTIFICATION, missing-project, and ACP-child exclusions.","introduced_in_round":3,"location":"P3 / registration failure classification","prevention":"Trace each required failure through the return type and metadata consumed by HookManager before specifying divergent outcomes.","principle":"Per-hook failure policy requires a typed failure signal distinct from legitimate absence.","root_cause":"`register_session` converts unrecoverable failure to the same empty-ID shape used by sessionless/excluded hooks.","section_id":"3.1","severity":"blocking"},{"category":"unhandled-edge","causal_finding_id":"GHDM-R3-F19","causal_section_ids":["4.1"],"check_key":"edge-case-coverage","description":"Clear consumes predecessor state and seeds a successor row; compact recovery can pop state from a sibling session. One `_mutate_variables` transaction cannot cover those transfers, and the continuation owners needed for a durable staging alternative are outside the described implementation.","finding_id":"GHDM-R4-F12","fix":"Specify and own a cross-row transaction/lock protocol for clear and compact, or use non-destructive source reads plus a durable copied staging record and CAS take. Add crash tests for predecessor→successor and sibling→current transfers.","introduced_in_round":3,"location":"P4 / compact and clear crash-safe arming","prevention":"Map the row/lock owner for every source take and destination enqueue before selecting a transaction boundary.","principle":"A single-row read-modify-write cannot atomically transfer state across predecessor, successor, or sibling session rows.","root_cause":"The repair assumes all source markers and buffers share one session-variable row.","section_id":"4.1","severity":"blocking"},{"category":"unhandled-edge","causal_finding_id":"GHDM-R3-F16","causal_section_ids":["4.1"],"check_key":"bounded-atomic-pending-context-delivery","description":"After `_run_adapter_hook` returns, route denial handling, envelope persistence, and response emission still remain. A crash or disconnect after commit can flip delivered markers while Grok receives nothing, contradicting confirmed delivery and at-least-once claims.","finding_id":"GHDM-R4-F13","fix":"Either define commit honestly as a translation-complete delivery attempt and revise delivered-state guarantees, or persist an exact provider-response outbox/replay record atomically with commit and replay it by request/envelope ID before selecting components again.","introduced_in_round":3,"location":"P4 / route-owned commit boundary","prevention":"Trace translation, route post-processing, persistence, ASGI emission, disconnect, and retry before choosing the commit point.","principle":"Acknowledgment semantics must name the last boundary they can actually prove and preserve replay across later failure.","root_cause":"The repair equates worker completion within `asyncio.wait_for` with the provider receiving the HTTP response.","section_id":"4.1","severity":"blocking"},{"category":"unhandled-edge","causal_finding_id":"GHDM-R3-F16","causal_section_ids":["4.1"],"check_key":"bounded-atomic-pending-context-delivery","description":"The plan has configurable 105-second adapter and 120-second provider timeouts, but no lease duration or clock rule. A short lease permits takeover while the original response is live; an arbitrarily long lease defeats bounded daemon-death recovery.","finding_id":"GHDM-R4-F14","fix":"Define a named persisted-UTC lease duration tied to the effective adapter/provider timeout plus safety margin, or add an owner heartbeat/terminal-state fence. Test just-before/after expiry, timeout release, daemon restart, and timeout overrides.","introduced_in_round":3,"location":"P4 / durable claim lease","prevention":"Bind lease math to effective timeout configuration and test expiry boundaries, overrides, release, and restart.","principle":"A takeover lease must be longer than every legitimate owner lifetime and short enough to bound crash recovery.","root_cause":"The repair adds a lease deadline without a numeric duration, clock policy, heartbeat, or relationship to configurable hook timeouts.","section_id":"4.1","severity":"blocking"}],"reviewer_session":"#11049","round":4,"verdict":"needs_review"},"session_id":"6dcc8341-5b1c-4682-a578-2c012b601e65"}
```

**Round 5** `kind: verification`

- reviewer_run: 2474fdbd-9f18-4758-a700-8b6ee1f1d98b
- reviewer_session: 8748bd63-37f3-40d8-8aa4-e33f68959fc9
- verdict: needs_review
- findings:
- GHDM-R5-F01/blocking: Phase B cannot obtain a paused epic — `/gobby expand` requires an existing task id and plan-file `gobby build` dispatches immediately. Vote: accepted — command trace is correct; the interposition window as written is unobtainable.
- GHDM-R5-F02/blocking: #20635's live criteria still demand MCP-result delivery this plan makes a non-goal, so the carrier cannot close `completed`. Vote: accepted — criteria migration was omitted from the round-4 collision repair.
- GHDM-R5-F03/blocking: obsoleting #20733–#20735/#20727 leaves compact-summary-fidelity P2 deliverables and M1 entries live and contradictory. Vote: accepted — cleanup traced only §§3–4, never P2/M1/covers/ledger.
- GHDM-R5-F04/blocking: async external dispatch can mark done at schedule time; no completion protocol resolves `external: running`. Vote: accepted — both named dispatchers return pre-execution.
- GHDM-R5-F05/blocking: `evaluate_blocking_webhooks` collapses allow/error/deadline to `None`; the durable gate cannot persist a sound decision. Vote: accepted.
- GHDM-R5-F06/blocking: blocking-gate bounded wait and takeover have no numeric deadline/cadence/clock policy. Vote: accepted — unimplementable as written.
- GHDM-R5-F07/blocking: custom SessionStart `run_command`/MCP effects can execute once per racer inside the stateful phase; the rule surface is unconstrained. Vote: accepted.
- GHDM-R5-F08/blocking: migration-405 carriers omit `runner_tests.rs` inventory, the grant schema-head test, and four positive signed goldens. Vote: accepted — typed repairs applied via `apply_plan_review_repairs`.
- GHDM-R5-F09/blocking: `truncate_context_for_adapter` cannot produce the promised head-plus-reference; character vs UTF-8-byte unit mismatch. Vote: accepted — round 4 misread the helper's behavior.
- GHDM-R5-F10/blocking: the route timeout owner cannot release a claim whose identity never reaches it; a request-scoped ClaimHandle is missing. Vote: accepted.
- GHDM-R5-F11/blocking: replay envelope lacks hook-class compatibility; verbatim cross-class replay corrupts deny/block semantics. Vote: accepted.
- GHDM-R5-F12/blocking: deny-once and at-least-once emission cannot both hold across crashes without provider acknowledgment. Vote: accepted — the guarantee must be scoped to crash-free delivery.
- GHDM-R5-F13/blocking: stage-before-take is not implementable from `handle_post_compact`; `consume_and_schedule_compact_self_continuation` owns the destructive take. Vote: accepted.
- GHDM-R5-F14/blocking: the committed-identity tombstone set is unbounded (P2P UUIDs, generations) with no retention or rollover owner. Vote: accepted.
- GHDM-R5-F15/blocking: the typed registration outcome never chose a public boundary; `SessionLookupService.resolve` stays `str | None`. Vote: accepted — reviewer's recommended boundary (typed `resolve`, `register_session` unchanged) noted for course-correction.
- GHDM-R5-F16/blocking: the worker-side claim cannot compute `effective route timeout + 30`; the live value is never plumbed past `_run_adapter_hook`. Vote: accepted.
- resolution_notes: Final round by user directive (2026-08-24). All 16 findings are fixer-induced by round-4 repairs (16/16), firing both the standing early-stop rule and the flat/rising-blockers replan trigger. Votes are disposition-only accepts: per the user's decision-seat directive, accept-all accepts the defect and never selects the architecture. The F08 typed repairs are applied after this checkpoint via apply_plan_review_repairs. The 15 prose fixes — several embedding genuine design forks (F06 deadline budgets, F09 helper ownership, F11 replay-compatibility strategy, F12 delivery-guarantee scoping, F13 relocation vs API refactor, F14 tombstone bounding, F15 boundary choice) — are held for course-correction with fork resolution by the user rather than freehand coordinator repair; the observed fixer-induced defect rate (0/8/12/13/16 across rounds 1–5) is the recorded reason. No further adversary rounds; the loop closes with human handoff.

```json plan-review-round
{"evidence_id":"4e7529b1-eea3-4951-a142-cde57189dde1","plan_hash":"78991b16b2b6241bd208957f5e65e7761a303b1e473efb7b8d51acc32c2f55ef","round_number":5,"round_result":{"coverage_attestation":{"adjacent_variant_complete":true,"attestation_digest":"2ef30c7f9f11b96eb88ebd075de7b0256486dedc210fa0d9fc8f4e315d08a633","cross_lane_interaction_complete":true,"disposition_counts":{"dismissed":3,"emitted_findings":16,"total":19},"evidence_id":"4e7529b1-eea3-4951-a142-cde57189dde1","lanes":[{"candidate_count":3,"lane_id":"requirements_traceability","status":"completed"},{"candidate_count":6,"lane_id":"repository_blast_radius","status":"completed"},{"candidate_count":10,"lane_id":"runtime_invariants","status":"completed"}],"shadow_manifest_status":{"entry_count":5,"manifest_digest":"03b3eaa07add2fad26cc3d4e4325f9cb0218e2d9005af5429c78b6168e1e1ed9","status":"valid"},"source_digest":"c3a7f55fbb3f6b55706c821741625a383e9ad33edb2ea9da509a4d8c3c026d20","version":1},"findings":[{"category":"bad-sequencing","causal_finding_id":"GHDM-R4-F02","causal_section_ids":["Collision with compact-summary-fidelity (#20724)","V2"],"check_key":"edge-case-coverage","description":"Phase B cannot obtain the promised paused epic: the installed `/gobby expand` workflow requires an existing task ID, while `gobby build <plan-file>` creates the root with automation enabled and ticks dispatch immediately.","finding_id":"GHDM-R5-F01","fix":"Add an explicit supported step that creates and registers the root epic with `allow_automation=false`, binds this approved plan and coverage ledger to it, then runs `/gobby expand #<epic> <plan-file>`. Add #20635's edge and validate before `gobby build '#<epic>'`, or name an existing paused plan-build entrypoint with exact arguments.","introduced_in_round":4,"location":"Collision cleanup / Phase B manual expansion","prevention":"Trace each cleanup command through its required inputs, created IDs, automation state, and dispatcher transition before claiming an interposition window.","principle":"A pre-dispatch checkpoint must name an operation that creates every resource it later references.","root_cause":"The repair treats `/gobby expand` as a root-epic creator even though it expands an existing target; the plan-file build path creates an automation-enabled root and immediately dispatches.","section_id":"Collision with compact-summary-fidelity (#20724)","severity":"blocking"},{"category":"traceability","causal_finding_id":"GHDM-R4-F01","causal_section_ids":["Collision with compact-summary-fidelity (#20724)","V2"],"check_key":"acceptance-observability","description":"#20635 still requires pending-context delivery through MCP `call_tool` results and a first-MCP-call live proof. This plan makes that route a non-goal, so adding a blocked-by edge and completing the new epic cannot justify `completed` or `already_implemented` under the carrier's live criteria.","finding_id":"GHDM-R5-F02","fix":"Make Phase A update #20635's description and validation criteria through `gobby-tasks` to the replacement hook-delivery outcomes and concrete tests, while preserving `deferred-from:compact-summary-fidelity:3`, parentage, and the original §3 artifact reference. Amend compact-summary-fidelity §3 consistently and verify the migrated criteria before eventual closure.","introduced_in_round":4,"location":"Collision cleanup / #20635 carrier close","prevention":"After changing a deferral's delivery design, compare the referenced task's description, criteria, artifacts, provenance, parentage, and closure reason against the replacement plan.","principle":"A deferred carrier may close completed only when its persisted criteria describe outcomes the delivering work actually satisfies.","root_cause":"The repair preserved #20635 as carrier without migrating its MCP-result criteria to this plan's hook-denial/Stop delivery contract.","section_id":"Collision with compact-summary-fidelity (#20724)","severity":"blocking"},{"category":"traceability","causal_finding_id":"GHDM-R4-F01","causal_section_ids":["Collision with compact-summary-fidelity (#20724)","V2"],"check_key":"acceptance-observability","description":"compact-summary-fidelity P2 deliverables 2.1–2.3 and their M1 entries remain live and still prescribe `wait_for_summary`, `get_handoff_context`, and the retired rule/doc route. `update_plan_hash` would regenerate that contradictory contract after the old tasks are closed obsolete.","finding_id":"GHDM-R5-F03","fix":"Expand Phase A to retire or replace old P2 and its M1 entries, reconcile the obsolete tasks' `covers:compact-summary-fidelity:2.x:*` labels, regenerate the managed coverage manifest, and require complete covered-status validation in addition to syntax validation. Keep §§3–4 aligned with the resulting canonical P2 contract.","introduced_in_round":4,"location":"Collision cleanup / old P2 and M1","prevention":"For every obsolete expanded leaf, trace its source deliverable, M1 entry, covers labels, coverage ledger row, and verification requirement through the cleanup.","principle":"Retiring expanded tasks requires retiring or replacing the canonical deliverables and coverage entries they implement.","root_cause":"Phase A obsoletes #20733–#20735 and #20727 while naming amendments only to compact-summary-fidelity §§3–4.","section_id":"Collision with compact-summary-fidelity (#20724)","severity":"blocking"},{"category":"unhandled-edge","causal_finding_id":"GHDM-R4-F03","causal_section_ids":["3.1","Locked decisions"],"check_key":"edge-case-coverage","description":"`dispatch_webhooks_async` and background MCP dispatch can return before the webhook or `run_pipeline` call executes. Marking done then loses crash-before-execution and late failures; leaving running requires completion signaling the plan never defines.","finding_id":"GHDM-R5-F04","fix":"Make both dispatch seams return a completion handle and register a callback that CAS-marks done only after a terminal attempt, leaving failure/crash retryable for takeover. Add crash-after-claim-before-schedule, crash-after-schedule-before-execution, and asynchronous failure tests.","introduced_in_round":4,"location":"P3 / async copied external-dispatch completion","prevention":"Trace claim, schedule, execution start, asynchronous failure, success callback, crash, stale takeover, and done persistence for every external dispatcher.","principle":"A durable side-effect claim may reach done only at a retry-safe completion boundary.","root_cause":"Both named dispatchers return after scheduling, while the repaired state machine defines no callback or result protocol that resolves `external: running`.","section_id":"3.1","severity":"blocking"},{"category":"unhandled-edge","causal_finding_id":"GHDM-R4-F04","causal_section_ids":["3.1","Locked decisions"],"check_key":"edge-case-coverage","description":"The owner cannot tell whether `None` is a durable allow or an unresolved evaluation failure. Persisting done can silently allow a failed blocking webhook; retaining running has no typed cause for the per-class fallback.","finding_id":"GHDM-R5-F05","fix":"Introduce a typed copied-gate result carrying `allowed`, `blocked`, `failed`, and `deadline_exhausted` plus endpoint outcomes. Persist done only for allowed/blocked; keep failed/exhausted retryable and map the current hook through its explicit per-class outcome. Target the dispatcher seam and test fail-open and fail-closed endpoints.","introduced_in_round":4,"location":"P3 / copied blocking-webhook decision","prevention":"Enumerate no-endpoint, allow, block, fail-open error, fail-closed error, deadline, owner crash, and takeover outcomes before persisting a gate result.","principle":"A durable gate must distinguish completed allow/block decisions from dispatch failure and deadline exhaustion.","root_cause":"The named `evaluate_blocking_webhooks` helper collapses no endpoints, successful allow, exception, and deadline failure to `None`.","section_id":"3.1","severity":"blocking"},{"category":"unhandled-edge","causal_finding_id":"GHDM-R4-F04","causal_section_ids":["3.1","Locked decisions"],"check_key":"edge-case-coverage","description":"An early threshold can duplicate a still-live blocking call; a full-budget wait can exhaust the hook before live rules run; a shorter passive wait can proceed while a valid block is computing. The plan provides no implementable policy.","finding_id":"GHDM-R5-F06","fix":"Specify one shared deadline policy: owner dispatch budget, loser poll cadence/bound, persisted stale-owner deadline and clock, CAS takeover fence, and reserved live-rule budget. State whether each unresolved class short-circuits before live rules and test every controlled-clock boundary.","introduced_in_round":4,"location":"P3 / blocking-gate bounded wait and takeover","prevention":"Assign numeric budgets and controlled-clock tests to owner-live, just-before-expiry, expiry, takeover, unresolved passive, unresolved PreToolUse, and unresolved Stop schedules.","principle":"Owner evaluation, loser waiting, takeover, and remaining live work need one explicit deadline budget.","root_cause":"The repair says bounded poll and stale takeover without a duration, cadence, clock, or relationship to the existing blocking-effect and outer route deadlines.","section_id":"3.1","severity":"blocking"},{"category":"unhandled-edge","causal_finding_id":"GHDM-R4-F04","causal_section_ids":["3.1","Locked decisions"],"check_key":"edge-case-coverage","description":"A custom SessionStart `run_command` or MCP effect can execute once per racer inside the stateful phase, and an inline blocking MCP result can bypass the durable blocking-webhook gate. Auditing one custom rule does not constrain the rule surface.","finding_id":"GHDM-R5-F07","fix":"Define and target a phase-aware evaluator that classifies every effect before applying it: pure/idempotent effects may run per racer, blocking effects feed the durable gate, external effects feed the single-owner phase, and unsupported custom effects are rejected or the whole rule is durably claimed. Add concurrent custom-effect tests.","introduced_in_round":4,"location":"P3 / copied custom SessionStart effects","prevention":"Build an effect-type matrix for installed and custom SessionStart rules and assign each effect to stateful, blocking, external, or rejected before execution.","principle":"Every-racer execution is safe only for effects proven idempotent; all other effects require an owning claim.","root_cause":"The three-phase repair classifies named installed effects but still invokes a general rule engine whose custom rules may run commands, inline MCP calls, background MCP calls, and injections.","section_id":"3.1","severity":"blocking"},{"category":"traceability","causal_finding_id":"GHDM-R4-F07","causal_section_ids":["3.1"],"check_key":"acceptance-observability","description":"Migration 405 changes the embedded schema identity, yet §3.1 omits `runner_tests.rs`' migration inventory, `grant/tests.rs`' hard-coded schema head, and four positive runtime-grant goldens whose identity, checksum, and signature must be regenerated.","finding_id":"GHDM-R5-F08","fix":"Add the missing Rust tests and four positive goldens to §3.1 Targets. Require the embedded inventory and grant head to reach 405 and the regenerated signed vectors to pass their canonical test; retain `payload_skew_unknown_field.json` as the intentional negative golden.","introduced_in_round":4,"location":"P3 / migration 405 derived carriers","prevention":"For each gcore migration, sweep embedded inventory counts, catalog-head assertions, schema identity, grant payload signatures, and positive/negative golden classifications.","principle":"Every schema-identity consumer and signed fixture must advance with a new embedded migration.","repairs":[{"entries":["`crates/gcore/src/schema/runner_tests.rs::*` — scope-reason: advance embedded migration inventory/count and latest-migration assertions through 405","`crates/gcore/src/grant/tests.rs::expected_schema_identity_tracks_catalog_head`","`tests/runtime_grants/golden/brokered_datastores.json`","`tests/runtime_grants/golden/direct_datastores.json`","`tests/runtime_grants/golden/old_client_new_grant.json`","`tests/runtime_grants/golden/unavailable_datastores.json`"],"kind":"add_targets","section_id":"3.1"},{"items":[{"artifact":"test: `crates/gcore/src/schema/runner_tests.rs`","prose":"Embedded migration inventory and grant schema-head assertions advance through migration 405"},{"artifact":"test: `tests/runtime_grants/test_golden_vectors.py::test_config_revision_signed`","prose":"Every positive runtime-grant golden is regenerated and re-signed for schema identity 405 while the intentional skew golden remains negative"}],"kind":"add_acceptance","section_id":"3.1"}],"root_cause":"The round-4 target repair followed the linted carrier table but omitted hard-coded migration/head tests and positive signed grant goldens.","section_id":"3.1","severity":"blocking"},{"category":"unhandled-edge","causal_finding_id":"GHDM-R4-F05","causal_section_ids":["4.1","Locked decisions"],"check_key":"edge-case-coverage","description":"A single oversized briefing cannot produce the promised in-budget non-empty head plus persisted reference through the existing helper. The plan's serialized UTF-8 byte guarantee also differs from the helper's character-length budget.","finding_id":"GHDM-R5-F09","fix":"Target and extend the shared helper, or add a dedicated Grok helper, to persist the full payload and retain a UTF-8-safe non-empty head plus reference within the actual PreToolUse reason and Stop additionalContext budgets. Add one/many-contributor, persistence-failure, and multibyte boundary tests.","introduced_in_round":4,"location":"P4 / oversize briefing degradation","prevention":"Execute every size class through the named helper with one contributor, many contributors, multibyte text, persistence failure, and each destination channel.","principle":"A plan that reuses a helper must match the helper's actual truncation unit and output shape.","root_cause":"The multipart repair assumes `truncate_context_for_adapter` retains a payload head, while its callee drops whole contributors or returns only a breadcrumb and measures characters.","section_id":"4.1","severity":"blocking"},{"category":"unhandled-edge","causal_finding_id":"GHDM-R4-F13","causal_section_ids":["4.1"],"check_key":"edge-case-coverage","description":"When `asyncio.wait_for` times out, the route has no worker result, resolved session, or claim token. It therefore cannot atomically release the exact claim before the worker returns, contrary to the repaired contract; recovery falls back to lease expiry.","finding_id":"GHDM-R5-F10","fix":"Define a request-scoped `ClaimHandle` visible to route and worker before or as the claim is created, with route-owned cancel/release and worker-owned CAS commit. Specify first-materialization lookup and no-request-ID handling, then test timeout before claim, after claim, during translation, and late completion.","introduced_in_round":4,"location":"P4 / route timeout claim release","prevention":"Trace claim identity availability at timeout-before-claim, timeout-after-claim, translation failure, late completion, first materialization, and absent request ID.","principle":"A timeout owner can release only a claim whose identity is visible outside the timed worker.","root_cause":"The claim is created after adapter translation starts in the executor, while `_run_adapter_hook` receives a token only on successful worker return.","section_id":"4.1","severity":"blocking"},{"category":"unhandled-edge","causal_finding_id":"GHDM-R4-F13","causal_section_ids":["4.1"],"check_key":"edge-case-coverage","description":"PreToolUse requires deny/reason semantics, while Stop/SubagentStop requires block plus `hookSpecificOutput.additionalContext`. Verbatim replay across those classes can deny or continue the wrong event and lose the briefing.","finding_id":"GHDM-R5-F11","fix":"Persist the originating canonical hook class and replay verbatim only to a compatible same-class request, or persist canonical delivery content and retranslate for the receiving hook. Define incompatible-request precedence and add PreToolUse→Stop, Stop→PreToolUse, and SubagentStop tests.","introduced_in_round":4,"location":"P4 / replay-envelope compatibility","prevention":"Cross every replay producer with PreToolUse, Stop, SubagentStop, passive hooks, same-ID retries, and incompatible next requests.","principle":"A stored native response may be replayed verbatim only to a request with the same response contract.","root_cause":"The repair keys replay by original request ID but serves it on an undefined next eligible request without recording compatible hook class.","section_id":"4.1","severity":"blocking"},{"category":"unhandled-edge","causal_finding_id":"GHDM-R4-F13","causal_section_ids":["4.1"],"check_key":"edge-case-coverage","description":"A crash after Grok receives a denial but before any emitted marker is durable must replay and deny again; marking emitted before return recreates the lost-response window. This contradicts unconditional deny-once/second-PreToolUse allowance and leaves existing same-envelope replay ordering undefined.","finding_id":"GHDM-R5-F12","fix":"State briefing denial as at-least-once across post-commit emission uncertainty and scope deny-once/second-request acceptance to crash-free delivery unless a provider acknowledgment exists. Integrate one replay source of truth with same-ID precedence, retire originating/current markers consistently, and test disconnect, stale reclaim, same-ID retry, and different-ID replay.","introduced_in_round":4,"location":"P4-P5 / replay, emitted marker, and deny-once","prevention":"Trace database commit, file-envelope marker, ASGI return/send, disconnect, same-ID retry, different-ID replay, and stale-marker reclaim as one protocol.","principle":"Without provider acknowledgment, crash recovery can promise at-least-once emission or one wire denial, but cannot guarantee both.","root_cause":"The repair adds an emitted marker after a route-level commit even though `execute_hook` can persist only before returning to ASGI and already has a separate same-envelope terminal-response replay.","section_id":"4.1","severity":"blocking"},{"category":"unhandled-edge","causal_finding_id":"GHDM-R4-F12","causal_section_ids":["4.1"],"check_key":"edge-case-coverage","description":"`handle_post_compact` cannot know the sibling source row, payload, or generation before the current destructive function consumes it. The proposed `briefing_staging_pending` marker is therefore not implementable from the targeted seam.","finding_id":"GHDM-R5-F13","fix":"Move the stage-before-take protocol into `compact_continuation.py` or refactor its API into non-destructive resolve/peek, source-stage, destination-take, and generation-CAS consume steps; add that symbol to §4.1 ownership and test current-row and sibling crash boundaries.","introduced_in_round":4,"location":"P4 / compact sibling stage-before-take","prevention":"Resolve the exact symbol that owns every destructive take and inject crashes before source stage, after stage, after destination take, and before source consume.","principle":"Crash-safe transfer must stage the selected source payload before the function that destroys it.","root_cause":"`consume_and_schedule_compact_self_continuation` selects and consumes current/sibling state internally and returns only bool, while §4.1 changes only its caller.","section_id":"4.1","severity":"blocking"},{"category":"unhandled-edge","causal_finding_id":"GHDM-R4-F10","causal_section_ids":["3.1","4.1"],"check_key":"edge-case-coverage","description":"Queue component/byte caps do not bound the committed set: every P2P and generation identity accumulates for the activation epoch. §3.1 defines the terminal-state format while §4.1 writes it, yet neither owns a cap, watermark, or epoch cleanup.","finding_id":"GHDM-R5-F14","fix":"Restrict committed tombstones to the finite stale-racer one-shot identities that need them, or define bounded retention/watermarks and an explicit activation-epoch rollover owned by §4.1. Test many sequential P2P/turn commits and epoch transition without re-enqueue.","introduced_in_round":4,"location":"P3-P4 / committed-identity tombstone lifecycle","prevention":"For every durable dedupe set, enumerate producer cardinality, lifetime, pruning owner, restart behavior, and epoch transition under a long session.","principle":"A dedupe tombstone collection needs a bounded producer domain or an explicit retention and rollover policy.","root_cause":"The repair applies one per-epoch committed set to finite one-shots, unbounded P2P message UUIDs, and generation-derived continuations without pruning.","section_id":"3.1","severity":"blocking"},{"category":"unhandled-edge","causal_finding_id":"GHDM-R4-F11","causal_section_ids":["3.1"],"check_key":"edge-case-coverage","description":"Implementers can either change `register_session` and break storage/protocol fakes or wrap only private lookup helpers and leave HookManager unable to distinguish failed from excluded. The current Targets do not resolve that choice.","finding_id":"GHDM-R5-F15","fix":"Keep `SessionManager.register_session` returning a string while adding the initial-variable seed, and define a typed `SessionResolutionOutcome` as the return of public `SessionLookupService.resolve`; target `resolve`, all direct callers/fakes, cache-hit behavior, and HookManager consumption. If registration itself changes type, inventory every implementation and fake instead.","introduced_in_round":4,"location":"P3 / typed registration result boundary","prevention":"Choose the public boundary first, then sweep its implementations, protocols, callers, mocks, empty-string sentinels, and cached paths.","principle":"A new outcome type needs one declared producer/consumer boundary and a complete caller/fake migration.","root_cause":"The repair mentions lookup/registration generically, targets internal resolve helpers and the registration protocol, while HookManager actually consumes public `SessionLookupService.resolve`, which remains `str | None`.","section_id":"3.1","severity":"blocking"},{"category":"unhandled-edge","causal_finding_id":"GHDM-R4-F14","causal_section_ids":["4.1"],"check_key":"edge-case-coverage","description":"The worker-side claim cannot compute `effective route timeout + 30` from the live request value. Reading shared HookManager state would race concurrent requests, so the numeric lease repair remains unrealizable.","finding_id":"GHDM-R5-F16","fix":"Plumb immutable `timeout_seconds` per request from `execute_hook` through adapter handling into the Grok claim, using internal event metadata or an explicit request context. Forbid shared mutable timeout state and test concurrent requests with different overrides.","introduced_in_round":4,"location":"P4 / live timeout-derived claim lease","prevention":"Trace configuration values from resolution through thread/executor boundaries to every consumer and race concurrent per-request overrides.","principle":"A lease derived from live request configuration must transport that immutable value to the claim site.","root_cause":"The route resolves `server.config.hooks.adapter_timeout` and passes it only to `_run_adapter_hook`; adapter, HookManager, and `_complete_response` never receive it.","section_id":"4.1","severity":"blocking"}],"reviewer_session":"8748bd63-37f3-40d8-8aa4-e33f68959fc9","round":5,"round_number":5,"verdict":"needs_review"},"session_id":"6dcc8341-5b1c-4682-a578-2c012b601e65"}
```

**Human handoff** `kind: verification`

- reason: user directive (2026-08-24) closed the review loop after round 5; the standing early-stop rule (all findings fixer-induced) and the flat/rising-blockers replan trigger both fired on the round-5 result (16/16 fixer-induced from round-4 repairs; blocker trajectory 11/12/20/14/16).
- state at handoff: round-5 rejection checkpoint appended and finalized on evidence 4e7529b1-eea3-4951-a142-cde57189dde1; F08 typed repairs applied (plan_hash ceb2655edfff5e27db36c6fc817272a28e6d08eaa3ae8863a925213d2c3a67d8 before the golden-target form fix); base validation clean.
- unrepaired accepted findings: GHDM-R5-F01 through F07 and F09 through F16 (15 prose findings). Their fixes are recorded verbatim in the round-5 fence; several embed design forks reserved for the user or a designated decision authority (F06 deadline budgets, F09 helper ownership, F11 replay-compatibility strategy, F12 delivery-guarantee scoping, F13 relocation vs API refactor, F14 tombstone bounding, F15 typed-boundary choice).
- next step: course-correction/replanning with fork resolution outside the adversary loop, per the reviewer-authored-candidate protocol tracked in task #20881. No further adversary rounds on this artifact without a fresh user directive.
