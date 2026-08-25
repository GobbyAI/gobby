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

**Activation identity is durable and monotonic.** The canonical session row's
reserved `_activation_epoch` variable is the sole namespace for lifecycle
briefing IDs, copied webhook/pipeline delivery keys, committed one-shots, and
compact/clear watermarks. Fresh deferred rows start at epoch 1; epochless
pre-deploy rows bootstrap once to epoch 1 before their first post-deploy
transition; resume, clear, and explicit re-materialization advance it
through one CAS-owned transition.
Compact and ordinary hook retries stay inside the current epoch.

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

**Coordinator-owned collision cleanup, one preflight and two recoverable phases.** The
coordinator session that carries this plan through final approval executes
the cleanup. Every step references resources that exist at the time it runs,
and every amendment to the registered compact-summary-fidelity plan goes
through the plans service. `update_plan_hash` commits the registry hash before
its separate managed-manifest generation call, so generation is always
repeated explicitly through
`gobby-plans:regenerate_coverage_manifest(plan_id="compact-summary-fidelity")`
and verified before any task mutation.

**Read-only migration observation — immediately after final approval and before
the first coordinator mutation:** run `observe_frozen_migration_408` from the
approved checkout before root creation, dependency attachment, Phase-A artifact
edits, #20635 migration, or obsolete-leaf closure:

```bash
git rev-parse HEAD
git ls-tree -r --name-only HEAD -- crates/gcore/assets/schema/migrations | perl -ne 'next unless m{/([0-9]+)_.*[.]sql$}; $v = $1 + 0; $latest = $v if !defined($latest) || $v > $latest; $count++ if $v == 408; END { printf "latest=%d 408_count=%d\n", $latest, $count // 0 }'
```

The coordinator snapshots task, plan-registry, companion-ledger,
managed-manifest, and dependency identities before the command, then durably
records one observation-evidence object containing the approved HEAD, exact
command, exit status, complete stdout, and those pre-observation identity
digests. Only exit 0 with stdout ending `latest=407 408_count=0` passes. A
command/parse failure, HEAD mismatch, 408 occupancy, or predecessor other than
407 returns the plan to review. The mismatch branch executes no mutation and
must prove the post-observation identity digests byte-for-byte equal the saved
digests. Phase B re-runs this read-only operation and checks the same approved
HEAD immediately before `create_plan` and again immediately before expansion;
either later mismatch performs no further mutation and returns to review. No
renumber or re-seal path exists.

**Preflight — after the read-only observation passes, before Phase A:** inventory
the complete logical identity namespace before the exact-current-label branch.
Query every task state in this project for all labels matching
`plan-root:grok-hook-deferred-materialization:*`, and independently read every
active or archived plan-registry binding for Plan ID
`grok-hook-deferred-materialization` and for every root found by that namespace
query. Then derive the exact current label
`plan-root:grok-hook-deferred-materialization:<approved-plan-hash>`. Recovery is
phase-aware. Reuse exactly one exact-current-label task in either of two states:
(a) the expected open, unbound epic with this plan title,
`category="planning"`, and `allow_automation=false`; or (b) that same open
paused epic bound exclusively by the `grok-hook-deferred-materialization` plan
row whose `root_task_ref`, approved plan hash, and plan path all match this
checkpoint. Any old-hash namespace match — unbound, bound, or terminal — is a
superseded root and fails closed before create; the coordinator must return it
to review for an explicit retirement decision. A current or superseded Plan-ID
row bound to another root/hash/path, a root bound to another Plan ID, an extra
binding, more than one namespace match, or one mismatched match also fails
closed. Zero exact-current matches permits creation only when the *entire*
namespace inventory and both registry-binding inventories are empty; otherwise
zero is an error, never permission to create.

The bound branch reads back the sealed companion ledger and managed coverage
manifest and requires their plan id, project id, root ref, plan hash, five
expected leaves, and complete acceptance set to match. If the exact plan row
committed but its create response was lost or managed-manifest generation
failed, call
`gobby-plans:regenerate_coverage_manifest(plan_id="grok-hook-deferred-materialization")`,
verify those same identities, and resume at the first incomplete Phase-B step;
never create another root or call `create_plan` again. Only the proven-empty
namespace branch permits one `gobby-tasks:create_task` with the expected
fields, `claim=false`, and the exact current label; follow creation with a full
namespace query, both binding inventories, and full task readback.
`create_task` has no automation-enabling argument, so the created or recovered
root remains paused and unbound until Phase B.

Immediately after resolving exactly one paused root, execute the idempotent
coordinator operation `seal_companion_ledger_root`. On its initial transition,
parse
`.gobby/plans/grok-hook-deferred-materialization.coverage-ledger.yaml`, require
the reviewed Plan ID/project ID, the already install-time-sealed approved
`plan_hash`, exactly sections 1.1/2.1/3.1/4.1/5.1 with the complete
identity/text/expected-leaf inventory, and exactly
`root_task_ref: "SEALED-BY-COORDINATOR"`; atomically replace only that scalar
with the normalized recovered epic ref. Reparse the resulting bytes, record
their SHA-256 plus the full identity/acceptance inventory in the durable
Preflight checkpoint, and read back the same bytes and hash. The companion
ledger's `plan_hash` sentinel is sealed separately by the coordinator at plan
install time and this operation must never modify it. After any response loss
or coordinator restart, the sealing retry accepts only an already-sealed
`root_task_ref` equal to that recovered epic and re-verifies the recorded hash
and inventory; a remaining sentinel, foreign root, hash change, missing/extra
item, or any other byte change fails closed. The exact sealed ledger is a hard
precondition to `create_plan`.

Attach #20635's blocked-by edge to that exact root and read #20635 back while
all old edges to #20733–#20735 remain; retrying Preflight reuses the same root,
sealed ledger, and edge. The crash-recovery probe deliberately discards the
create response after its insert commits, reruns Preflight, and must recover the
same epic ID with exactly one matching provenance label and one #20635
replacement edge. Separate response-loss probes around root-ledger sealing
must recover the exact sealed root and ledger hash without accepting a sentinel
or rewriting any other ledger field.

**Phase A — after final approval, before expansion** (existing resources
only):

1. **Synchronize the governing artifacts before any task mutation.** Save the
   exact pre-edit bytes and hashes of
   `.gobby/plans/compact-summary-fidelity.md`, its companion coverage ledger,
   and its managed coverage manifest. Finish all compact-summary-fidelity
   edits together: remove deliverables 2.1–2.3, their three M1 entries, their
   companion-ledger rows, and the now-empty P2 phase; rewrite §4's live Grok
   check to this plan's deny-reason/Stop delivery; and rewrite §3 as
   superseded-pending-delivery by plan
   `grok-hook-deferred-materialization`, with this plan's hook criteria
   summarized, `task_ref: "#20635"`, original acceptance item `3.1`,
   provenance, and parentage unchanged. Call
   `gobby-plans:update_plan_hash(plan_id="compact-summary-fidelity")`; whether
   it returns normally or reports post-hash generation failure, explicitly
   call `gobby-plans:regenerate_coverage_manifest` and verify the plan row,
   file hash, companion ledger, and managed manifest agree before continuing.
   A retry calls `regenerate_coverage_manifest` directly. If regeneration
   cannot succeed, restore all saved bytes, call `update_plan_hash` to restore
   the prior row hash, explicitly regenerate and verify the prior managed
   manifest, and stop Phase A with no task mutations. Require
   `gobby-plans:validate_plan` to pass at the successful new-artifact
   checkpoint.
2. **#20635 stays open and its complete persisted identity migrates before any
   old owner closes.** Through `gobby-tasks:update_task`, replace its title with
   "Give Grok a real context delivery channel: correct capability registry and
   deliver pending context through PreToolUse denial and Stop/SubagentStop
   blocking", and replace its MCP-result description and validation criteria
   with this plan's hook contract while retaining its
   `deferred-from:compact-summary-fidelity:3` label, #20724 parentage, and
   explicit provenance text "Deferred from plan compact-summary-fidelity
   section 3, acceptance item 3.1". The replacement criteria require: the
   corrected Grok capability/docs matrix; durable briefing and turn-context
   components delivered through PreToolUse deny or Stop/SubagentStop block +
   `additionalContext`; one-shot and P2P delivered state changing only on
   ghook delivery acknowledgment; the focused 3.1/4.1 tests; and a live Grok
   proof where the first MCP call is no longer the delivery event, the first
   tool receives the startup/wiki/profile/skill briefing in its deny reason,
   and a real Stop gate carries `additionalContext`. Read #20635 back through
   `gobby-tasks:get_task(brief=false)` and compare title, description, every
   persisted criterion, labels, parentage, provenance, and dependency edges
   before continuing. Section 3's `task_ref` remains #20635.
3. Re-read #20635 and the paused replacement epic and verify Preflight's one
   blocked-by edge still exists together with every old edge to
   #20733–#20735. This replacement edge must remain present before and during
   removal of any old edge.
4. Remove every `covers:compact-summary-fidelity:2.*:*` label and
   obsolete-leaf dependency from #20733–#20735; then close #20733–#20735 and
   sub-epic #20727 as `obsolete`.
5. Drop #20724's blocked-by dependency on #20727 and #20635's blocked-by
   dependencies on #20733–#20735 through `gobby-tasks`. Keep #20724's
   dependency on the open #20635 tail carrier and keep #20635's replacement
   blocked-by edge throughout.
6. Run `uv run gobby plan coverage --plan
   .gobby/plans/compact-summary-fidelity.md --plan-id
   compact-summary-fidelity --plan-hash <new-hash> --task-tree db --root-task
   '#20724' --project-id d45545c5-ded5-4335-b115-0245752edacf --evidence
   none`; exit 0 and every remaining row `status: covered` are the Phase-A
   gate.

**Phase B — bind the paused root, verify the reviewed companion ledger,
expand, then opt in.** The coordinator uses only supported operations in this
order:

1. Immediately before binding, re-run `observe_frozen_migration_408` and require
   its command, exit status, complete stdout, and HEAD to match the durable
   pre-mutation observation evidence exactly. Its mismatch path is read-only
   and records a zero-diff comparison against the current task, plan, ledger,
   manifest, and dependency checkpoint before returning to review. Then require
   `.gobby/plans/grok-hook-deferred-materialization.coverage-ledger.yaml` to
   exist before binding. Its `plan_id`, install-time-sealed `plan_hash`, project
   id, Preflight-sealed normalized `root_task_ref`, five unique expected leaves,
   complete acceptance-item text/set, and recorded sealed-ledger SHA-256 must
   exactly match the approved plan and recovered root; any sentinel, foreign
   root, missing/extra item, stale hash, or byte mismatch blocks Phase B and
   expansion. No coordinator mutation may occur between this successful
   revalidation and `create_plan`.
2. Run the phase-aware provenance resolver above. When the Plan ID has no row,
   call `gobby-plans:create_plan(plan_id="grok-hook-deferred-materialization",
   plan_path="<canonical-plan-path>", plan_kind="implementation",
   root_task_ref="#<epic>")`. After either a normal response or response loss,
   read the plan row, sealed ledger, and managed coverage artifact back through
   the bound recovery branch. If the row committed while coverage generation
   failed, explicitly regenerate the managed manifest and repeat the readback.
   Require the exact approved hash, plan path, project id, root task ref, five
   leaves, and complete acceptance set before resuming at step 3. This is the
   sole binding step; an exact existing binding is reused, a foreign or
   mismatched binding fails closed, and no plan-file build path is used.
3. Immediately before expansion, re-run the same read-only migration
   observation, require the approved HEAD and `latest=407 408_count=0`, and
   compare it with the durable pre-mutation evidence. A mismatch performs no
   expansion or other mutation and returns to review. On success, run
   `/gobby expand #<epic> <canonical-plan-path>` against that existing paused
   root. Require expansion QA and coverage-manifest generation to finish while
   the root and descendants remain `allow_automation=false`.
4. Read back #20635 and verify the Preflight epic edge still exists together
   with its open state, migrated title/description/criteria, provenance label,
   #20724 parentage, and unmoved §3 `task_ref`. Re-run
   `gobby-plans:validate_plan` and the complete covered-row check for
   compact-summary-fidelity.
5. Only after steps 1–4 succeed, run `gobby build '#<epic>'`. This is the sole
   automation opt-in and the first point any descendant may become
   dispatch-eligible.

When this plan's epic completes, #20635 closes as
`completed`/`already_implemented` — never before.

#20726 (digest/summary fidelity) does not overlap and stays. Expansion must
not begin until Phase A completes; the `gobby build` opt-in must not run
until Phase B steps 1–4 complete; V2 item 6 verifies the preflight and both phases
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
     context — #20724 P2 payload); wiki/profile/persona first-shot; pending
     P2P messages admitted in deterministic bounded batches. The 16-slot
     buffer holds at most seven finite components across queued, claimed, and
     canonical-replay-owned state: one each for startup packet, agent
     instructions, wiki overview, user profile, and persona, plus at most one
     resident compact generation and one resident clear generation. Define
     `FINITE_BRIEFING_IN_BUFFER_MAX = FINITE_BRIEFING_RESERVED_SLOTS = 7`,
     leaving the static
     `P2P_MAX_COMPONENTS = P2P_MAX_CANDIDATES = 16 - 7 = 9` ceiling. A newer
     compact or clear generation keeps its distinct generation-keyed component
     ID while durably staged at its source; acknowledgment of the resident
     generation releases that class slot and triggers admission in existing
     generation order. No general retry or backpressure subsystem is added.
     Arm
     compact briefing from `handle_post_compact` on Grok (no SessionStart).
     Flush deny-once on the next PreToolUse in crash-free delivery. If that
     cycle has no tools, flush **once** on Stop as `additionalContext` (Grok
     continues once). Route commit is a delivery attempt; ghook maps the
     provider action, writes and flushes it to provider stdout, and only then
     deletes the originating inbox file. That disappearance is the durable
     acknowledgment. Mapping/emission failure, post-commit crash, or disconnect
     retains the file, stays at-least-once, and may repeat the deny/block until
     acknowledgment exists. The daemon drain retains/skips a
     provider-ack-pending Grok file even on its processed-marker branch; only
     ghook may remove that file after mapping and stdout write/flush succeed.
     `wiki_overview_injected`, `_startup_context_injected`, and compact
     one-shots are set **only on confirmed flush**, never on stash.
   - **Turn-context-class** (`grok_pending_turn_context`): brevity
     reminders, later memory recall, pressure nudges, tool-error recovery.
     Never deny solely to deliver. **Never Stop-flush by itself** — that
     plus turn_start re-arm is a continuation loop (`wants_continuation()`
     is true whenever `additional_context` is non-empty). Concatenate onto
     an already-blocking stop-gate only. If Stop is allowing, drop the
     remainder (debug log).
   - Canonical replay preserves these component classes. A replay containing
     briefing may create its one delivery gate. Turn-context-only replay may
     piggyback only on a current real PreToolUse deny or Stop/SubagentStop
     block; ungated PreToolUse leaves it pending, and allowing Stop follows the
     explicit debug-logged drop policy without executing acknowledgment
     mutations.
5. Grok Stop gates (`require-task-close`, `tool_block_pending`, …) emit
   `decision: "block"` with `continue: true`, never `deny`.
6. Stash Grok observe `response.context` in
   `HookManager._complete_response` via `gobby.hooks.grok_pending_context`
   **before** adapter translation, then clear `response.context` so
   `record_unsupported_response_fields` does not `dropped_field`-spam
   every UPS. Delete `GROK_SYSTEM_MESSAGE_CONTEXT_HOOKS`; do not keep an
   empty frozenset. Lifecycle one-shots are classified at their producers,
   never from `_session_just_materialized` on an aggregate response: startup
   packet, agent instructions, wiki overview, user profile, and persona each
   enqueue a briefing component with stable id
   `briefing:lifecycle:<session-id>:<activation-epoch>:<producer-class>`;
   compact and clear enqueue their existing generation-keyed briefing IDs.
   Those producer contributions bypass aggregate stash. Any non-lifecycle
   `response.context` that reaches stash is genuine turn-context and appends
   to `grok_pending_turn_context`; `_session_just_materialized` remains an
   activation coordination fact and never reclasses that aggregate text.
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
   result or takes the typed failure/deadline short-circuit before its live
   rules and handler (3.1). The **async
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
   event; it never waits on the async phase. The owner remains `running`
   through actual async execution and only a successful terminal completion
   callback marks `done`; failure/crash stays takeover-retryable. Copied
   custom `run_command` and unknown effects are rejected atomically before
   copied effects execute. Injects stay gated by
   delivered-state vars, not by `_materialize_activation_done`.

   `_activation_epoch` is a reserved object on the canonical session-variable
   row and has one mutation owner: the connection-aware activation-rollover
   helper running under `SessionVariableMutation(session_id)`. It stores the
   current integer epoch, the last stable transition key, and an optional
   pending transition. Same-key retries return the already-selected epoch;
   concurrent different-key writers compare the expected epoch and only the
   CAS winner advances it. Section 3.1 defines initialization and transitions;
   section 4.1 defines rollover of queued, claimed, replay-owned, committed, and
   watermark state.

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
- `src/gobby/hooks/event_handlers/_session_start/__init__.py::*` — scope-reason: SessionStart mixin delegates the extracted materialization flow before the deferred cutover extends the same module
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

### 3.1 Materialize on first BEFORE_AGENT and inject startup context [category: code] (depends: 1.1, 2.1)
`kind: deliverable`

Targets:
- `src/gobby/hooks/session_materialize.py`
- `src/gobby/hooks/event_handlers/_session_start/materialize.py`
- `src/gobby/hooks/event_handlers/_session_start/__init__.py::*` — scope-reason: deferred SessionStart cutover, response stripping, and compact-continuation exports share this mixin module
- `src/gobby/hooks/hook_manager.py::HookManager._handle_after_daemon_ready`
- `src/gobby/adapters/acp_hook_adapter.py::*` — scope-reason: immutable route-deadline and request-handle metadata transport changes translation now, while 4.1 adds the exhaustive typed worker-result boundary in the same module
- `src/gobby/servers/routes/mcp/hooks.py::*` — scope-reason: create and enforce one immutable outer monotonic route deadline, transport it into copied processing, and fence cancellation before fallback
- `tests/servers/test_mcp_routes.py::*` — scope-reason: controlled-clock short/equal/long outer-timeout and late-worker cancellation fencing cases span the route suite
- `tests/adapters/test_acp_hook_integration.py::*` — scope-reason: immutable outer-deadline metadata transport is asserted through native translation
- `src/gobby/hooks/session_lookup.py::*` — scope-reason: the new public SessionResolutionOutcome/status boundary and exhaustive construction across every SessionLookupService resolution exit must change as one coherent file-local contract
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
- `src/gobby/workflows/reserved_variables.py::*` — scope-reason: reserve the `_materialize_activation_done`, `_copied_session_start_state`, `_deferred_materialization`, and `_activation_epoch` control markers this leaf introduces
- `tests/mcp_proxy/test_top_level_variables.py::*` — scope-reason: set_variable rejection cases for the four reserved control markers
- `src/gobby/install/bundled_content_manifest.json::*` — scope-reason: manifest digests refresh for this leaf's compact-handoff template change
- `tests/install/test_bundled_content_manifest.py::*` — scope-reason: tree-equality regression after this leaf's template change
- `tests/workflows/test_context_handoff_rules.py::*` — scope-reason: compact arming/delivery split cases for non-Grok CLIs
- `tests/servers/routes/test_session_variables.py::*` — scope-reason: prove HTTP set_variable rejects runtime-reserved materialization markers
- `tests/workflows/test_hooks.py::*` — scope-reason: prove non-internal set_variable rule effects reject runtime-reserved materialization markers
- `src/gobby/hooks/grok_pending_context.py`
- `src/gobby/storage/sessions/_manager.py::SessionManager.register_session`
- `tests/storage/sessions/test_storage_sessions_registration.py::*` — scope-reason: deferred-materialization discriminator persists atomically with the row insert
- `tests/adapters/test_codex_call_tool_session_id.py::*` — scope-reason: direct SessionLookupService.resolve fake adopts SessionResolutionOutcome
- `tests/hooks/test_call_tool_session_id_refs.py::*` — scope-reason: direct SessionLookupService.resolve fake adopts SessionResolutionOutcome
- `src/gobby/install/shared/workflows/rules/context-handoff/clear-pending-context-reset-on-start.yaml::*` — scope-reason: pending_context_reset clearing moves from SessionStart to the confirmed delivery owner
- `tests/hooks/test_webhooks.py::*` — scope-reason: session_start webhook replay-at-first-activity policy cases
- `src/gobby/hooks/dispatchers/webhook.py::*` — scope-reason: CopiedGateResult, endpoint outcomes, and their exhaustive sync/async dispatch constructors and return signatures span the webhook dispatcher module
- `src/gobby/hooks/dispatchers/mcp.py::dispatch_mcp_calls`
- `src/gobby/workflows/engine/effects.py::*` — scope-reason: phase-aware copied-effect classification and the existing delivery-ack extraction share this effect engine
- `src/gobby/hooks/dispatchers/__init__.py::*` — scope-reason: dispatcher exports follow webhook completion-result and MCP completion-handle signatures
- `src/gobby/servers/websocket/chat/_lifecycle.py::*` — scope-reason: direct dispatch_mcp_calls consumer adapts to the completion-bearing result
- `tests/hooks/test_block_observability.py::*` — scope-reason: evaluate_blocking_webhooks consumer adopts typed copied-gate results without changing live-event observability
- `tests/workflows/test_context_handoff_fencing.py::*` — scope-reason: EffectsMixin._apply_effect consumer exercises phase-aware invocation
- `tests/workflows/test_run_command_effect.py::*` — scope-reason: EffectsMixin._apply_effect consumer covers copied run_command rejection separately from live execution
- `tests/hooks/test_dispatch_mcp_calls.py::*` — scope-reason: copied background MCP completion handles and asynchronous terminal outcomes
- `tests/hooks/test_mcp_dispatch_async.py::*` — scope-reason: background MCP scheduling and callback completion boundaries
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
- `crates/gcore/assets/schema/migrations/408_pipeline_execution_idempotency_key.sql`
- `crates/gcore/src/schema/assets.rs::*` — scope-reason: embed frozen migration 408 and its checksum for pipeline execution idempotency
- `crates/gcore/assets/schema/catalog.manifest.json::*` — scope-reason: regenerated catalog manifest carries frozen migration 408 and its digest
- `crates/gcore/src/grant/bundle.rs::*` — scope-reason: derived schema-bundle carrier refreshes with frozen migration 408
- `crates/gcore/tests/schema_contract.rs::*` — scope-reason: schema contract test tracks the new migration and catalog digest
- `crates/gdaemon/tests/cli_contract.rs::*` — scope-reason: daemon CLI contract test tracks the regenerated schema identity
- `src/gobby/storage/schema_expected_identity.json::*` — scope-reason: regenerated expected schema identity after frozen migration 408
- `tests/storage/test_pipeline_storage.py::*` — scope-reason: concurrent idempotency-key uniqueness and existing-run reuse cover storage behavior
- `src/gobby/storage/sessions/_crud.py::_SessionCRUDMixin.register`
- `src/gobby/hooks/session_types.py::HookSessionManager.register_session`
- `src/gobby/hooks/session_coordinator.py::*` — scope-reason: consumer of HookSessionManager.register_session; call sites pass the initial-variable seed while retaining the string return
- `tests/hooks/test_session_lookup_metadata.py::*` — scope-reason: public resolve returns SessionResolutionOutcome, including cache-hit, exclusion, and registration-failure cases
- `crates/gcore/src/schema/runner_tests.rs::*` — scope-reason: prove frozen migration 408 is exactly one above committed predecessor 407 and advance embedded inventory/count assertions through 408
- `crates/gcore/src/grant/tests.rs::expected_schema_identity_tracks_catalog_head`
- `tests/runtime_grants/golden/brokered_datastores.json::*` — scope-reason: regenerate and re-sign the complete positive runtime-grant vector for frozen schema identity 408
- `tests/runtime_grants/golden/direct_datastores.json::*` — scope-reason: regenerate and re-sign the complete positive runtime-grant vector for frozen schema identity 408
- `tests/runtime_grants/golden/old_client_new_grant.json::*` — scope-reason: regenerate and re-sign the complete positive runtime-grant vector for frozen schema identity 408
- `tests/runtime_grants/golden/unavailable_datastores.json::*` — scope-reason: regenerate and re-sign the complete positive runtime-grant vector for frozen schema identity 408

Split `src/gobby/hooks/hook_manager.py` by moving first-hook activation and
copied session-start rule evaluation into
`src/gobby/hooks/session_materialize.py` so `hook_manager.py` stays under the
1,000-line ceiling.

The pipeline-execution migration identity is frozen as
`408_pipeline_execution_idempotency_key.sql`, exactly one above its immediately
preceding committed catalog version 407. The Collision section runs the named
read-only `observe_frozen_migration_408` operation immediately after approval
and before the first coordinator mutation, durably binds its command, exit
status, stdout, and identity digests to the approved HEAD, and revalidates the
same evidence immediately before `create_plan` and expansion; only
`latest=407 408_count=0` passes. Every mismatch path is read-only and proves no
state change. Implementation never selects another number and defines no
re-seal operation. The migration, embedded
inventory/count, catalog identity, grant bundle and schema-head assertion,
expected identity, and all four signed positive goldens advance together to
408. `runner_tests.rs` remains the later implementation proof that embedded
408 follows committed 407; it is separate from the coordinator's pre-expansion
observation.

Keep thin `register_session` inside `_resolve_uncached_session_id` (lookup
lock). On create, set `event.metadata["_session_just_materialized"] = True`.
The `_deferred_materialization` discriminator and initial activation identity
are **persisted in the same storage write as the row insert**, never stamped
afterwards:
`SessionManager.register_session` gains an initial-variable seed so the
deferred-path marker exists if and only if the row exists. A crash at any
point after `register_session` returns therefore leaves a row that recovery
classifies unambiguously — deferred rows carry the marker from birth,
pre-deploy rows never do — and no crash window can make a new deferred row
look like a pre-deploy row and skip copied SessionStart processing forever.
For a fresh deferred row with no predecessor, that same insert seeds reserved
`_activation_epoch` as
`{value: 1, transition_key: "create:<platform-session-id>", status: "active", pending: null}`.
A clear successor instead seeds `value = predecessor.value + 1` with
`transition_key = "clear:<predecessor-session-id>:<clear-generation>"`; it
never transiently publishes epoch 1. Do not call activation from the lookup
service.

`advance_activation_epoch` in `session_materialize.py` is the sole writer after
insert. It calls the connection-aware `_mutate_variables` path, which owns
`SessionVariableMutation(session_id)`, and CASes the complete object against
the expected value/status/transition key. A successful SESSION_END for an
existing row first calls the same owner with stable key
`close:<durable-envelope-id>` to CAS `status: active → inactive` without
changing `value`; orphan SESSION_END remains a no-op. Exactly three lifecycle transitions
advance it: a live `SessionStart(source="resume")` uses stable key
`resume:<durable-envelope-id>`; clear-successor binding uses the predecessor
and clear-generation key above; and explicit re-materialization of an inactive
existing row uses `rematerialize:<platform-session-id>:<closed-epoch>` while
atomically changing `status: inactive → active`. Fresh first activity consumes
the insert-time epoch without incrementing it. Compact, retries of ordinary
hooks, activation recovery, and duplicate copies of the same transition do not
advance it. A retry with the last transition key returns the recorded value. A
concurrent racer with the same key gets that value; a different-key racer that
loses the expected-value CAS must re-read and may proceed only if its lifecycle
predicate still holds. No caller writes the integer directly. Existing
deferred rows created during cutover but missing the field initialize once to
1 under the same CAS before copied processing; pre-deploy rows remain excluded
from copied SessionStart effects as specified below.

An epochless existing row — a pre-deploy row whose variables contain no
`_activation_epoch` object — bootstraps exactly once through the same
session-lock CAS owner before any epoch-consuming transition applies. The
first post-deploy lifecycle touch (ordinary hook, resume, SESSION_END,
PostCompact, or explicit re-materialization) writes
`{value: 1, transition_key: "bootstrap:<platform-session-id>", status: <the
row's current active/inactive state>, pending: null}` together with an empty
committed-one-shot set and empty compact/clear watermark and resident-class
indexes. Bootstrap always precedes and never absorbs the triggering event:
after the bootstrap CAS commits, the triggering event applies as the next
transition under its normal rule — a resume advances 1 → 2 through
`resume:<durable-envelope-id>`, SESSION_END CASes `status` without changing
the value, compact and ordinary hooks stay inside epoch 1, and explicit
re-materialization of an inactive row advances through its normal key.
Duplicate bootstrap attempts are same-key CAS no-ops; a restart between
bootstrap and the triggering event's transition re-reads the bootstrapped
object and applies only the pending transition. Component IDs and watermarks
for pre-deploy rows therefore always resolve inside a deterministic epoch
namespace.

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
   completion itself; no claim, no wait. For Grok, each lifecycle one-shot
   producer classifies its own contribution as briefing and calls
   `enqueue_if_absent` with
   `briefing:lifecycle:<session-id>:<activation-epoch>:<producer-class>`;
   startup packet, agent instructions, wiki overview, user profile, and
   persona therefore retain one stable identity across both row-creation
   winner orientations. They do not write those contributions into the
   aggregate `response.context`. The atomic queued/claimed/replay/committed
   identity check yields exactly one component and one deferred ack mutation
   per producer. Non-Grok composition stays inline, while genuine live
   turn-context may still use the response aggregate. The live event keeps
   its original type so turn_start rules still see `BEFORE_AGENT`.
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
no forward dependency. `enqueue_if_absent` deduplicates each finite producer
identity against **queued, claimed, canonical-replay-owned, and committed**
identities in that same transaction: the buffer holds queued and claimed
components, and a per-activation-epoch **committed-one-shot set** records the
stable startup-packet, agent-instructions, wiki-overview, user-profile, and
persona component IDs. The set is replaced when the activation epoch changes;
P2P UUIDs and compact/clear generations never enter it. A stale activation or
copied-lifecycle racer whose guard passed before another request committed
therefore no-ops instead of re-enqueueing that producer component. 4.1
extends the same module with the distinct P2P delivered-row check and
generation-watermark mechanics, plus stash, flush, claim, and commit. An
interleaved schedule — UPS creates the row first,
PreToolUse finishes activation first, UPS stashes last — still yields
exactly one staged component; 4.1 owns flush and commit, so the deny-once
delivery and both row-creation winner orientations stay there while the
exactly-one-staged-component acceptance lives here. In the reverse schedule,
PreToolUse creates and commits first; a losing UPS before or after
acknowledgment reuses the same producer IDs, contributes no lifecycle text to
aggregate stash, and cannot create a turn-context duplicate.

`activate_materialized_session` classifies every step as **required** or
**best-effort**, each behind its own durable guard:

- Required: session-row registration (performed by the lookup layer; a
  failed or empty registration means there is no session to activate) and
  session-variable seeding (identity and delivered-state seeds).
- Best-effort: agent resolution (`None` is a valid absence; only an
  exception is a failure), code-index setup, wiki seeding, profile
  injection, transcript-processor setup. A best-effort failure is logged,
  its guard marks complete, and activation proceeds.

Registration failure is **typed at one public boundary, never
shape-collapsed**. `SessionManager.register_session` and
`HookSessionManager.register_session` keep their existing string return; the
only additive change there is the atomic initial-variable seed. Public
`SessionLookupService.resolve` returns `SessionResolutionOutcome(status,
session_id, reason)`, where status is `materialized` (cache hit, existing row,
or newly created row), `excluded` (SESSION_END, NOTIFICATION, missing project,
or ACP child: legitimate absence), or `failed` (unrecoverable registration
error). Internal `_resolve_session_id` helpers may still use string/None while
`resolve` translates every exit, including cache hits, into the public
outcome. `HookManager._handle_after_daemon_ready` is the sole production
consumer and branches on that outcome before copied processing, webhooks,
live rules, and handlers: `excluded` follows the ordinary sessionless path;
`failed` triggers the per-class outcomes below. Direct callers and fakes in
the listed tests migrate to the same type. A failed first PreToolUse can
never ride the sessionless allow path.

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

Reserve `_materialize_activation_done`, `_copied_session_start_state`,
`_deferred_materialization`, and `_activation_epoch` in
`src/gobby/workflows/reserved_variables.py` alongside the 4.1 Grok buffers.
They are runtime-owned control markers:
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
  `blocking` substate moves `pending → running → done` plus a typed
  `CopiedGateResult`. `evaluate_blocking_webhooks` returns that result with
  status `allowed`, `blocked`, `failed`, or `deadline_exhausted` and the
  endpoint outcomes that produced it. No matching endpoint is `allowed`.
  A fail-closed endpoint error is a policy-complete `blocked` result; a
  fail-open endpoint error is `failed` for retry while the current passive
  or Stop request still takes its explicit fail-open outcome. Only
  `allowed` and `blocked` persist `done`. `failed` and
  `deadline_exhausted` release the owner claim back to retryable `pending`
  and short-circuit the current request before live rules or its handler.
  `pending → running` is an atomic compare-and-swap claim carrying owner id,
  token, and a persisted UTC deadline. Every racer — owner or not — must
  observe `done` or take that typed short-circuit before its live rules and
  handler. A durable `blocked` result gates every racer's live event exactly
  as a live SessionStart block would.

  The route creates **one immutable outer monotonic deadline** from the
  effective per-request `adapter_timeout` before worker submission, uses that
  same deadline to bound `_run_adapter_hook`, and carries it through internal
  native-event metadata into copied processing. The gate also reuses the
  existing 15-second aggregate blocking-effect deadline created once by
  `new_blocking_effect_deadline`; it adds no independent clock. Named constants
  remain `COPIED_GATE_LIVE_RESERVE_SECONDS = 1.0` and
  `COPIED_GATE_POLL_SECONDS = 0.05`. Its terminal boundary is stated and
  implemented exactly as
  `gate_deadline = min(aggregate_deadline, outer_deadline - COPIED_GATE_LIVE_RESERVE_SECONDS)`.
  The owner
  dispatch, persisted UTC owner deadline, 50-ms loser polling, and token-fenced
  takeover all derive from that single boundary. If either remaining budget
  cannot preserve the reserve, no owner work starts and the current class
  takes its typed deadline short-circuit. Route timeout/cancellation fences and
  releases the current owner before returning fallback, so a late worker
  cannot persist `done` or cross the typed per-class outcome. At the boundary,
  passive and Stop/SubagentStop return their fail-open response without live
  rules/handler, while PreToolUse returns the retriable activation deny.
  Controlled-clock tests put `outer_deadline` below, at, and above
  `aggregate_deadline + COPIED_GATE_LIVE_RESERVE_SECONDS`; the above case
  preserves the full ratified 15-second aggregate window. They also cover
  owner-live, just-before and at expiry,
  takeover, daemon restart, route cancellation, late completion, and each hook
  class retain the reserve and one fence.
- **Async external-dispatch phase** (fire-and-forget side effects: the
  `pipeline-auto-run` rule's background `run_pipeline` mcp_call, plus
  non-blocking `session_start` webhook dispatch via
  `dispatch_webhooks_async`): `external` moves `pending → running → done`
  under a CAS-claim/takeover contract; losers skip and never wait.
  `dispatch_webhooks_async` and the background branch of
  `dispatch_mcp_calls` return a completion handle that represents scheduling,
  execution, and terminal endpoint/tool outcomes. The owner registers one
  callback carrying its claim token. Only a successful terminal attempt
  CAS-marks `done`; scheduling failure, asynchronous failure, cancellation,
  or owner crash leaves/reverts `external` to retryable state for takeover.
  A callback from a superseded owner loses the CAS and cannot mark a newer
  attempt done. A claim alone cannot make dispatch exactly-once across a
  crash between dispatch and `done`, so where Gobby owns the consumer the dispatch
  carries a **durable consumer-enforced idempotency key** — for
  `pipeline-auto-run`, `run_pipeline` gains an `idempotency_key` argument
  (key: session id + rule id + activation epoch) and the
  pipeline-execution store enforces uniqueness on it, returning the
  existing run instead of starting a duplicate. Webhook endpoints are
  external observers Gobby cannot deduplicate for: their dispatch —
  blocking re-evaluation after takeover included — is **at-least-once**,
  exactly once in crash-free operation, with a stable delivery key
  (session id + activation epoch) in every payload for receiver-side
  dedupe. Crash-recovery tests cover crash-after-claim-before-schedule,
  crash-after-schedule-before-execution, asynchronous failure, successful
  callback CAS, and crash-after-dispatch (pipeline redispatch
  consumer-rejected; webhook duplicate carries the same delivery key).

Copied evaluation is **phase-aware before applying any effect**.
`session_materialize.py` builds an effect matrix and passes the selected phase
into `EffectsMixin._apply_effect`: pure/idempotent arming and reset effects run
in the every-racer stateful phase; `block` and inline MCP effects with blocking
semantics feed the durable blocking gate; background MCP and non-blocking
webhook effects feed the single-owner external phase and expose completion
handles. A copied custom SessionStart `run_command` is rejected during copied
rule validation because arbitrary command side effects have no consumer
idempotency contract; the copied rule fails before any sibling effect runs.
Unknown effect types are rejected on the same all-or-nothing boundary. Tests
cover concurrent custom rules containing each supported class, inline
fail-open/fail-closed MCP outcomes, foreground and background `run_command`,
and an unknown effect. Auditing every installed SessionStart rule against this
matrix is part of this leaf; no general rule-engine invocation may execute an
unclassified copied effect.

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
- 3.1.9 - `_materialize_activation_done`, `_copied_session_start_state`, `_deferred_materialization`, and `_activation_epoch` are reserved; MCP `set_variable` and non-internal rule effects reject writes to them. test: `tests/mcp_proxy/test_top_level_variables.py`.
- 3.1.10 - Concurrent first UPS and PreToolUse both run activation to completion idempotently, and a simulated crash mid-activation is healed by the next hook. test: `tests/hooks/test_hooks_manager.py`.
- 3.1.11 - Every startup, compact, resume, clear, web-chat, and pre-created SessionStart response has empty context and system-message fields. test: `tests/hooks/test_session_events_coverage.py::test_session_start_never_emits_context`.
- 3.1.12 - Copied SessionStart processing follows the phase contract under concurrent first hooks: every racer completes the stateful phase and observes the durable blocking-gate decision before its live rules and handler, in activation → reconciliation → copied state → blocking gate → live evaluation order; the blocking-gate and async external-dispatch claims each have a single owner with crash takeover; a crash between activation completion and copied processing is recovered by the next hook; pre-deploy rows never replay resets, webhooks, or pipelines. test: `tests/hooks/test_hooks_manager.py`.
- 3.1.13 - Transient failures in registration, agent resolution, variable seeding, and transcript setup follow the required/best-effort classification with per-class current-hook outcomes: passive hooks and Stop fail open, PreToolUse returns a retriable deny, no copied processing or startup injects run, and the next hook (or retried call) recovers; best-effort failures log and let activation complete; public `SessionLookupService.resolve` returns `SessionResolutionOutcome` for cache hits, created/existing rows, every exclusion class (SESSION_END, NOTIFICATION, missing project, ACP-child), and registration failure while both register_session interfaces retain their string return, and a failed first PreToolUse never follows the sessionless allow path. test: `tests/hooks/test_hooks_manager.py`.
- 3.1.14 - Non-Grok compact: SessionStart emits nothing and arms pending state retaining the summary and skill lists; the first turn_start renders the complete continuation including skill reloads and clears only the fields it included. test: `tests/workflows/test_context_handoff_rules.py`.
- 3.1.15 - Bundled-content manifest exactly matches the committed shared-template tree after this leaf's template change. test: `tests/install/test_bundled_content_manifest.py::test_bundled_content_manifest_matches_tree`.
- 3.1.16 - Compact, resume, clear, web-chat, and pre-created SessionStart paths each bind or create the intended canonical row without duplication. test: `tests/hooks/test_session_events_coverage.py::test_session_start_binding_matrix`.
- 3.1.17 - HTTP session-variable writes reject every runtime-reserved materialization marker. test: `tests/servers/routes/test_session_variables.py`.
- 3.1.18 - Non-internal workflow set_variable effects reject every runtime-reserved materialization marker. test: `tests/workflows/test_hooks.py`.
- 3.1.19 - The structured-component `enqueue_if_absent` primitive exists in `grok_pending_context.py`, is wired as an activation step, and an interleaved first UPS/PreToolUse stages exactly one startup-briefing component. test: `tests/hooks/test_hooks_manager.py`.
- 3.1.20 - A sessionless startup SessionStart dispatches no session_start webhooks; first-activity copied processing dispatches blocking webhooks through the durable blocking-gate step and non-blocking webhooks through the async external-dispatch claim — exactly once in crash-free operation, at-least-once with the stable session-id + activation-epoch delivery key across crash takeover — and a durable blocking decision gates every racer's live event, proven by a two-racer test where the claim loser's PreToolUse cannot execute while the gate is blocked. test: `tests/hooks/test_webhooks.py`.
- 3.1.21 - The SessionStart output barrier strips handler- and rule-merged context on every SESSION_START path, and `pending_context_reset` survives an empty compact SessionStart, cleared only by the confirmed delivery owner. test: `tests/workflows/test_context_handoff_rules.py`.
- 3.1.22 - The `_deferred_materialization` discriminator and fresh `_activation_epoch` value 1 persist in the same storage write as row creation; a simulated crash immediately after `register_session` leaves a row recovery classifies as deferred with one durable epoch, never as pre-deploy or epochless. test: `tests/storage/sessions/test_storage_sessions_registration.py`.
- 3.1.23 - `run_pipeline` rejects a duplicate dispatch bearing the same idempotency key and returns the existing run; a crash-takeover redispatch yields exactly one pipeline execution. test: `tests/mcp_proxy/tools/workflows/test_mcp_proxy_tools_workflows_pipelines.py`.
- 3.1.24 - A parameterized provider matrix delivers first-activity startup context through the expected native channel for Claude, Qwen, Droid, and Codex, exercises the supported AGY shared-materialization path, and retains Grok as the pending-briefing exception. test: `tests/hooks/test_hooks_manager.py::test_first_activity_startup_context_provider_matrix`.
- 3.1.25 - Concurrent pipeline execution creation with one idempotency key persists exactly one execution and every caller receives that existing execution across process restart. test: `tests/storage/test_pipeline_storage.py::test_create_execution_idempotency_key_is_concurrent_and_durable`.
- 3.1.26 - The deferred discriminator is inserted inside the low-level fresh-row transaction while unique-conflict recovery and existing-row reuse never relabel pre-deploy rows. test: `tests/storage/sessions/test_storage_sessions_registration.py::test_deferred_seed_is_atomic_across_insert_conflict_and_reuse`.
- 3.1.27 - Migration 408 is frozen and the implemented embedded inventory/count and grant schema-head assertions prove it is exactly one above committed version 407; the separate coordinator-owned `observe_frozen_migration_408` evidence ran before every coordinator mutation and was revalidated immediately before plan binding and expansion, with each mismatch branch proving a zero state diff. test: `crates/gcore/src/schema/runner_tests.rs`.
- 3.1.28 - Every positive runtime-grant golden is regenerated and re-signed for frozen schema identity 408 while the intentional skew golden remains negative. test: `tests/runtime_grants/test_golden_vectors.py::test_config_revision_signed`.
- 3.1.29 - Copied non-blocking webhook and background MCP dispatches remain `external: running` through scheduling and execution and reach `done` only through the current owner's successful completion-handle callback; crash-before-schedule, crash-after-schedule-before-execution, asynchronous failure, and stale callbacks stay retryable without corrupting a takeover. test: `tests/hooks/test_dispatch_mcp_calls.py`.
- 3.1.30 - Copied blocking webhooks return endpoint-bearing `CopiedGateResult` values that distinguish allowed, blocked, failed, and deadline_exhausted; only allowed/blocked persist done, and fail-open versus fail-closed endpoint errors follow their explicit policy. test: `tests/hooks/test_webhooks.py`.
- 3.1.31 - Copied gate owner, loser, and takeover use `gate_deadline = min(aggregate_deadline, outer_deadline - COPIED_GATE_LIVE_RESERVE_SECONDS)` with the ratified 15-second aggregate deadline, 1-second live-rule reserve, and 50-ms polls; controlled-clock cases below, at, and above `aggregate_deadline + COPIED_GATE_LIVE_RESERVE_SECONDS` prove the above case retains the full 15 seconds, while route cancellation and late completion prove token-fenced release/takeover before every unresolved class short-circuits without live rules/handler. test: `tests/hooks/test_webhooks.py`. test: `tests/servers/test_mcp_routes.py`.
- 3.1.32 - The phase-aware copied evaluator classifies every installed and custom SessionStart effect before execution; supported stateful, blocking, and external effects run only in their owning phase, while run_command and unknown custom effects are rejected atomically under concurrent first hooks. test: `tests/workflows/test_hooks.py`.
- 3.1.33 - `_activation_epoch` is initialized atomically for fresh deferred and clear-successor rows; successful SESSION_END marks an existing row inactive without incrementing, and only stable-keyed resume, clear, and inactive-row re-materialization transitions advance through the single `SessionVariableMutation` CAS owner; same-key retries, daemon restart, and concurrent rollover racers select one epoch while compact and ordinary retries never advance it. test: `tests/hooks/test_session_activation_reconciliation.py`.
- 3.1.34 - An epochless pre-deploy row bootstraps `_activation_epoch` exactly once to `{value: 1, transition_key: "bootstrap:<platform-session-id>", status: <current row state>, pending: null}` with empty committed-one-shot, watermark, and resident-class state under the session-lock CAS before its triggering event applies as the next normal transition; each first post-deploy event class (ordinary hook, resume, SESSION_END, PostCompact, re-materialization), duplicate bootstrap attempts, and a restart between bootstrap and the pending transition yield one deterministic epoch namespace. test: `tests/hooks/test_session_activation_reconciliation.py`.

## P4: Grok pending-context flush
`kind: framing`

**Goal**: Grok models actually see startup and per-turn injects.

### 4.1 Stash observe context and flush briefing without a Stop loop [category: code] (depends: 1.1, 2.1, 3.1)
`kind: deliverable`

Targets:
- `src/gobby/hooks/grok_pending_context.py`
- `src/gobby/hooks/hook_manager.py::HookManager._complete_response`
- `src/gobby/hooks/event_enrichment.py::*` — scope-reason: Grok pending-message routing leaves messages unclaimed until confirmed delivery; `mark_delivered_batch` moves into component ack mutations
- `src/gobby/adapters/acp_hook_adapter.py::*` — scope-reason: `ACPHookAdapter.handle_native` becomes translation-only and the module adds exhaustive payload and payload-plus-opaque-claim-token worker result types; no worker path commits or releases
- `src/gobby/adapters/agy.py::*` — scope-reason: ACP translate_to_hook_event override/consumer preserves AGY metadata behavior with the immutable request context
- `tests/adapters/test_acp_hook_integration.py::*` — scope-reason: handle_native consumer; typed ordinary-payload versus payload-plus-claim-token translation cases extend the file
- `tests/adapters/test_qwen.py::*` — scope-reason: translate_to_hook_event consumer preserves Qwen event metadata while adding the immutable Grok request context
- `src/gobby/servers/routes/mcp/hooks.py::*` — scope-reason: `execute_hook` and `_run_adapter_hook` own exhaustive typed-result handling plus post-deadline commit-or-release so a late worker cannot commit
- `src/gobby/hooks/inbox.py::_post_envelope`
- `crates/ghook/src/dispatch.rs::*` — scope-reason: delivered_action mapping and run_gobby_owned provider emission must retain the inbox envelope until stdout write and flush succeed
- `crates/ghook/src/action.rs::emit_action`
- `crates/ghook/src/output.rs::*` — scope-reason: make stdout write/flush result-bearing while preserving stderr behavior in the two-function output shim
- `crates/ghook/tests/contract.rs::*` — scope-reason: mapping failure, stdout write/flush failure, successful deletion, and replay retention cross the ghook process contract suite
- `src/gobby/hooks/event_handlers/_misc.py::MiscEventHandlerMixin.handle_post_compact`
- `src/gobby/sessions/compact_continuation.py::consume_and_schedule_compact_self_continuation`
- `src/gobby/sessions/compact_staging.py`
- `src/gobby/hooks/event_handlers/_session_start/flow.py::*` — scope-reason: arm clear-successor briefing inside _bind_clear_successor
- `src/gobby/hooks/event_handlers/_session_start/__init__.py::*` — scope-reason: compact continuation API exports follow the non-destructive stage-before-take split
- `src/gobby/hooks/event_handlers/__init__.py::*` — scope-reason: composes handle_post_compact into EventHandlers; Grok briefing arming reaches it through the mixin
- `src/gobby/workflows/reserved_variables.py::*` — scope-reason: reserve grok_pending_briefing, grok_pending_turn_context, and the activation-epoch rollover state they namespace
- `src/gobby/install/shared/workflows/rules/context-handoff/inject-compact-handoff.yaml::*` — scope-reason: exclude Grok from inject-compact-handoff-on-prompt turn_start delivery; PostCompact briefing owns Grok
- `src/gobby/install/shared/workflows/rules/context-handoff/inject-clear-handoff.yaml::*` — scope-reason: exclude Grok from inject-clear-handoff-on-prompt; clear-successor briefing owns Grok
- `src/gobby/install/bundled_content_manifest.json::*` — scope-reason: manifest digests refresh for both changed context-handoff rule templates
- `docs/contracts/session-boundary.md`
- `docs/guides/sessions.md`
- `docs/guides/variables.md`
- `tests/workflows/test_context_handoff_rules.py::*` — scope-reason: one-owner proof; Grok exclusion cases for both turn_start handoff rules
- `tests/hooks/test_misc_handlers.py::*` — scope-reason: direct handle_post_compact tests gain Grok briefing-arming cases
- `tests/hooks/test_session_handoff_handlers.py::*` — scope-reason: direct clear-successor tests gain briefing-arming cases
- `tests/hooks/test_session_start_handlers.py::*` — scope-reason: compact continuation caller fakes follow the stage-before-take API
- `tests/sessions/test_compact_continuation.py::*` — scope-reason: current-row and sibling source staging crash boundaries
- `tests/hooks/test_hook_manager.py::*` — scope-reason: _complete_response stash/flush integration asserts, including preserve_original gates
- `tests/hooks/test_pending_message_provider_contracts.py::*` — scope-reason: provider contract cases for queued/replay-owned delivery and acknowledgment-time accounting
- `tests/hooks/test_grok_pending_context.py`
- `tests/install/test_bundled_content_manifest.py::*` — scope-reason: validate regenerated bundled-content digests against the committed shared-template tree
- `src/gobby/workflows/engine/delivery_formatting.py::finalize_staged_memory_delivery`
- `tests/workflows/test_delivery_pipeline.py::*` — scope-reason: consumer of finalize_staged_memory_delivery; Grok commit-time recording cases update
- `src/gobby/hooks/event_handlers/_agent.py::AgentEventHandlerMixin._inject_agent_instructions_if_needed`
- `src/gobby/install/shared/workflows/rules/context-handoff/inject-wiki-overview.yaml::*` — scope-reason: defer Grok wiki delivered-state mutation until ghook delivery acknowledgment
- `src/gobby/workflows/engine/effects.py::*` — scope-reason: route delivered-marker effects through the extracted deferred-ack seam; the extraction itself lands in the new module below
- `src/gobby/workflows/engine/delivery_ack_effects.py`
- `tests/hooks/test_event_enrichment.py::*` — scope-reason: update piggyback membership, enqueue-without-ack, formatting failure, and retained non-Grok delivery cases
- `tests/servers/test_mcp_routes.py::*` — scope-reason: cover the real hook executor timeout and fallback boundary for claim release
- `tests/hooks/test_envelope_marker_retention.py::*` — scope-reason: same-ID terminal replay and Grok pending-replay precedence share one progression contract
- `tests/hooks/test_inbox.py::*` — scope-reason: daemon inbox replay identifies itself and cannot consume provider-visible Grok delivery
- `tests/workflows/test_hook_evaluation_timeout.py::*` — scope-reason: preserve bounded-worker and timeout recovery behavior while adding Grok claims
- `src/gobby/hooks/inbox.py::_drain_hook_inbox_once_locked`
- `src/gobby/storage/inter_session_messages.py::InterSessionMessageManager.get_undelivered_messages`
- `src/gobby/storage/inter_session_messages.py::InterSessionMessageManager.mark_delivered_batch`
- `tests/storage/test_inter_session_messages.py::*` — scope-reason: P2P delivered-row lock/recheck participates in Grok component enqueue and commit
- `tests/hooks/test_inline_mcp_dispatcher.py::*` — scope-reason: direct get_undelivered_messages consumer preserves inline MCP message behavior while Grok selection becomes bounded
- `tests/mcp_proxy/tools/test_agent_messaging.py::*` — scope-reason: mark_delivered_batch consumer retains existing behavior through the transaction-aware seam
- `src/gobby/workflows/state_manager.py::SessionVariableManager._mutate_variables`
- `src/gobby/storage/hub/protocol.py::*` — scope-reason: the no-initial-lock immediate transaction contract and LockTarget/Transaction protocol declarations must remain coherent for the shared SessionVariableMutation advisory lock
- `src/gobby/storage/hub/postgres.py::*` — scope-reason: the Postgres hub transaction facade exposes no-initial-lock immediate mode while preserving every existing transaction entry point
- `tests/workflows/test_state_manager.py::*` — scope-reason: transaction-aware connection injection, rollback, and deterministic lock-order cases span the state-manager test module
- `tests/adapters/test_adapters_agy.py::*` — scope-reason: direct AgyAdapter.handle_native consumer must adopt the typed AdapterWorkerResult contract

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
and `response.context` is non-empty. By construction that aggregate contains
only genuine turn-context, so stash appends it to
`grok_pending_turn_context` and clears `response.context` before
`record_unsupported_response_fields`. It never consults
`_session_just_materialized` and never promotes aggregate text to briefing.

Lifecycle one-shots bypass aggregate stash at their producers. Startup packet,
agent instructions, wiki overview, user profile, and persona call
`enqueue_if_absent` with their stable
`briefing:lifecycle:<session-id>:<activation-epoch>:<producer-class>` IDs and
deferred ack mutations. This applies to every racer, including a UPS that lost
row creation, both before and after another hook's acknowledgment. The two
successor producers use their existing generation-keyed briefing IDs:

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
  briefing `component_id`. When that class already has a queued, claimed, or
  canonical-replay-owned resident component, a second compact or clear keeps
  its distinct component ID in an ordered durable source-staging entry and is
  not inserted into the 16-slot destination buffer. At most one compact and
  one clear generation are resident across those three buffer states.
  Acknowledgment of the resident generation releases its class slot and
  invokes the existing generation-ordered destination take, which admits at
  most the next eligible staged ID; same-generation retries no-op and a
  watermark-superseded older stage retires under the existing ordering rule.
  Source clearing compare-and-swaps against its own generation, so an older
  commit can never clear or re-render a newer continuation. This is a bounded
  admission refinement of stage-before-take, with no general retry queue or
  producer backpressure state machine.

`src/gobby/sessions/compact_continuation.py` is currently 952 lines. Split it
by moving source resolve/peek, source staging, destination take,
generation-CAS consume, acknowledgment-triggered next-generation admission,
and generation-keyed scheduling-state helpers into
the new `src/gobby/sessions/compact_staging.py`; keep
`compact_continuation.py` as the thin public orchestrator and require it below
850 lines after the split. `consume_and_schedule_compact_self_continuation`
owns the destructive current-or-sibling take and prompt scheduling today, so
the orchestrator preserves both effects explicitly. Its four ordered durable
transfer operations are: non-destructive source resolve/peek returning source
row, payload, and generation; source-row generation-keyed
`briefing_staging_pending` write; the destination's idempotent enqueue/take
keyed by source + generation when that class has no resident; and
generation-CAS source consume after the destination confirms the take. A newer
staged compact/clear generation remains source-side until
acknowledgment-triggered release of the one resident class slot.

Prompt scheduling is a separate explicit operation keyed by the same source
session and generation. The source staging record tracks its retryable
schedule state. Scheduling is **at-least-once across crash uncertainty**: a
crash after the external tmux send but before durable scheduled recording may
re-send the same generation-keyed prompt, while destination enqueue and source
consume remain idempotent for that key. No exactly-once delivery is claimed.
The source marker is generation-CAS-consumed only after destination take and a
successful schedule are durably recorded. A crash before scheduling or a
scheduling failure retains the staged briefing and original source marker; a
crash after scheduling before recording retries with the same key; a duplicate
retry cannot create a second briefing component or consume a newer generation.
The public function returns the resolved staging/scheduling result to
`handle_post_compact`; its old bool-only destructive path cannot remain for
Grok. Current-row and sibling selection both stage before any `_take_*` helper
removes the marker. Tests cover every transfer and scheduling boundary plus a
second generation racing an older queued/claimed component.

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

Both buffers are managed through `SessionVariableManager._mutate_variables`
(atomic read-modify-write). Add a caller-supplied hub-connection seam to
`_mutate_variables`: existing callers omit it and retain the current owned
`transaction_immediate(SessionVariableMutation(session_id))` behavior. A
supplied connection never opens, commits, or rolls back a nested transaction;
before its first session-variable read, `_mutate_variables` calls
`conn.acquire_additional_lock(SessionVariableMutation(session_id))`, so every
legacy and P2P writer serializes through the same advisory-lock domain.
`HubDatabase.transaction_immediate` and `PostgresHubDatabase.transaction_immediate`
gain the already-supported no-initial-lock form, allowing the P2P owner to open
an immediate-capable transaction and run a two-stage selection. Stage 1 selects
at most the static `P2P_MAX_CANDIDATES` earliest undelivered rows in
`sent_at ASC, id ASC` order, acquires exactly that bounded candidate set's row
locks in UUID order, and rechecks row eligibility without reading session
capacity. Stage 2 enters `_mutate_variables`, which acquires
`SessionVariableMutation(session_id)` and only then reads queued, claimed, and
canonical-replay-owned occupancy, recomputes live reservation-aware P2P
capacity, rechecks locked-row component ownership, and enqueues the earliest
fitting prefix in the original `sent_at`/id order. The rest of the locked
candidate rows receive no mutation. The global order is UUID-sorted message row locks →
`SessionVariableMutation(session_id)` advisory lock → session-variable
read/write. Legacy `merge_variables` acquires only that session advisory lock
and never waits on a message row, so the order has no reverse edge.

Acknowledgment mirrors enqueue with its own explicit two-stage protocol. First,
peek the canonical replay token and its P2P component UUIDs without taking a
session or message-row lock; this is candidate discovery only and authorizes no
mutation. Open a no-initial-lock immediate transaction, UUID-lock exactly those
message rows, and recheck their recipient and undelivered eligibility. Then
call connection-aware `_mutate_variables`, which acquires
`SessionVariableMutation(session_id)` before its first session-variable read,
and revalidate the exact replay token plus ownership of every acknowledged
component against the locked canonical replay. A stale peek, changed token, or
ownership mismatch rolls back or no-ops and retries discovery; it never
acknowledges the peeked rows. Only after this revalidation does the same
transaction call the optional-connection `mark_delivered_batch`, execute every
component ack mutation, advance or clear canonical replay, release any resident
compact/clear class slot. All those changes commit atomically. After commit,
the acknowledgment owner synchronously invokes the existing generation-ordered
staged take for each released class; the durable source-staging entry itself is
the crash-recovery trigger, so a crash before that call is recovered by normal
stage-before-take reconciliation on the next hook without a new retry or
backpressure state machine.
The resulting total order for both enqueue and acknowledgment is UUID-sorted
message-row locks → `SessionVariableMutation(session_id)` → revalidated
session-variable mutation. Injected failure after each operation rolls the
entire unit back. Acknowledgment-versus-enqueue on the same and overlapping
UUID sets, acknowledgment-versus-legacy `merge_variables`, and
enqueue-versus-legacy `merge_variables` races prove no stale peek, deadlock,
delivered-row loss, or buffer/replay overwrite.
Bounds are **class-aware with exact numeric caps**, defined as named
constants in `grok_pending_context.py`. Sizes are UTF-8 byte lengths of the
serialized component payload; truncation and chunk boundaries always fall on
character boundaries, never inside a multibyte sequence:

- `briefing`: deduplicated by stable key via enqueue-if-absent and **never
  evicted by a cap**. Finite lifecycle producers use the stable producer IDs
  above; compact/clear use source-generation IDs.
  `BRIEFING_MAX_COMPONENTS = 16` is the defensive in-buffer invariant. The
  finite resident bound is exactly five lifecycle one-shots plus one compact
  and one clear:
  `FINITE_BRIEFING_IN_BUFFER_MAX = FINITE_BRIEFING_RESERVED_SLOTS = 7`.
  Compact and clear each have `RESIDENT_MAX = 1` across queued, claimed, and
  canonical-replay-owned state. A newer generation retains its distinct ID in
  durable source staging and is admitted only after acknowledgment releases
  that class's resident slot; it is never treated as a seventeenth buffer key.
  Therefore
  `P2P_MAX_COMPONENTS = P2P_MAX_CANDIDATES = BRIEFING_MAX_COMPONENTS -
  FINITE_BRIEFING_RESERVED_SLOTS = 16 - 7 = 9`. The seven-slot reservation is
  withheld before every P2P selection regardless of current finite occupancy.
  Finite admission uses only these seven in-buffer slots and the existing
  stage-before-take generation ordering, with no general retry or backpressure
  state machine. An attempted insert that violates a lifecycle one-shot or
  compact/clear resident bound is an invariant error; a valid newer compact or
  clear generation stays staged. P2P is also briefing-class, but its durable
  producer is unbounded. Under the session advisory lock, its live capacity is
  `max(0, min(P2P_MAX_COMPONENTS - occupied_p2p,
  BRIEFING_MAX_COMPONENTS - occupied_total))`, with both occupancy counts
  including queued, claimed, and canonical-replay-owned components. Zero
  available P2P slots is normal backpressure, leaves message rows untouched,
  and never reports corruption.
  **Every briefing flush fits one provider-budget response — there
  are no continuation parts.** A briefing whose rendered payload exceeds
  the provider delivery budget uses a dedicated
  `fit_grok_briefing_response` helper in `grok_pending_context.py`; the
  shared `truncate_context_for_adapter` stays unchanged because it budgets
  characters and may drop every contributor. The Grok helper persists the
  complete canonical payload once and returns one stable persisted-context
  reference, then computes the largest non-empty character-boundary head whose
  **complete serialized native-response UTF-8 bytes** fit the receiving
  PreToolUse `reason` or Stop/SubagentStop `additionalContext` budget. Existing
  real-gate reason/context and retry framing are part of that envelope budget.
  It never slices encoded bytes. One contributor and many contributors use the
  same aggregate path. Every component-class-eligible different-class replay calls this same
  helper with the persisted full canonical content and the same stable
  reference, refitting against the receiving class instead of reusing the
  origin class's head or reference. If initial persistence fails, reference
  reuse fails, or the receiving budget cannot hold at least one payload
  character plus the reference, the failure path depends on ownership state:
  a newly created pre-commit claim is CAS-released through its `ClaimHandle`
  (components requeued, `claim_id` invalidated) before flush returns the
  per-class retryable fallback, so the next eligible request can select the
  same components instead of hitting the live-claim in-flight denial, while
  an already committed canonical replay whose receiving-class
  refit/retranslation fails is preserved unchanged — replay stays pending
  with its identity and stable reference intact. Neither path advances
  request identity; no component or ack mutation commits. The component
  commits on that one fitted response —
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

*Request-scoped handle.* Before submitting the worker, `execute_hook`
creates a thread-safe `ClaimHandle` in `grok_pending_context.py` and puts its
opaque handle token, immutable effective `timeout_seconds`, the one outer
monotonic deadline created before worker submission, and durable
request/envelope id into internal native-event metadata.
`ACPHookAdapter.translate_to_hook_event` copies those values into
`HookEvent.metadata`; no shared HookManager timeout field exists. When flush
creates a claim, it binds `(platform_session_id, claim_id)` to the handle.
`ACPHookAdapter.handle_native` is translation-only. It returns the exhaustive
typed union `AdapterWorkerResult = PayloadResult(payload) |
ClaimedPayloadResult(payload, opaque_claim_token)`; the claimed constructor is
available only when translation produced a bound claim, and ordinary provider
responses use the tokenless constructor. The opaque token exposes no commit
method to the worker. `_run_adapter_hook` returns this typed value to
`execute_hook`; the async route must pattern-match both variants, so no tuple,
nullable field, or truthiness path can silently discard or commit a claim.
The route's `asyncio.wait_for` consumes only the remaining duration from that
same outer deadline. Route timeout/cancellation calls
`ClaimHandle.cancel_and_release`: before
binding it records cancellation so a later bind immediately releases; after
binding it CAS-releases that exact claim. Worker completion can only return a
token; route-owned commit checks the same handle and loses after cancellation.
This covers timeout before first-materialization lookup, after claim, during
translation, and late worker completion. A request without a durable id may
claim for translation but route completion releases it instead of committing.

*Lease.* The lease deadline is a persisted UTC timestamp computed at claim
time as the immutable per-request **effective outer hook-route timeout
carried by the ClaimHandle metadata plus
`GROK_CLAIM_LEASE_MARGIN_SECONDS = 30`** — a
named constant in `grok_pending_context.py`. The lease is therefore
strictly longer than every legitimate in-flight response and still bounds
daemon-death recovery. A claim whose lease has expired — daemon death
after claim, a stranded executor, any abandoned in-flight request — is
requeued by the next flush's stale-takeover check, so no payload or
acknowledgment is ever stranded. Controlled-clock tests cover just-before
and just-after expiry, timeout release, recovery across daemon restart,
and a timeout-config override changing the computed lease. Concurrent
requests with different overrides prove each claim uses its own immutable
value and cannot race through shared mutable state.

*Live claim.* At most one live claim or canonical replay exists per session
across all hook classes; the claim CAS is the single serialization point and
a second claim attempt from any class loses. A second gating hook arriving
while a claim is live (unexpired, uncommitted) must not treat in-flight work
as empty work: its PreToolUse returns a **retriable non-payload deny**
("briefing delivery in flight — retry"), never an allow, so no tool crosses
before the owner commits, releases, or expires. A racing real
Stop/SubagentStop follows its normal per-class arm for its own gate (it
never blocks solely to wait) but must not claim or attach pending components
while another claim is live: its block emits only its own real-gate content,
leaving pending components to the owner, a takeover after lease expiry, or a
later eligible request. Loser behavior and owner-timeout takeover are tested
separately from the single payload-bearing denial, with concurrent
PreToolUse-versus-Stop/SubagentStop races in both completion orders and
every release/takeover branch.

*Commit.* The opaque claim token rides the typed result through adapter
translation, while the executor worker **never commits or releases**:
`ACPHookAdapter.handle_native` runs inside the worker thread and cannot know
whether the route's outer `asyncio.wait_for` returned in budget. After
`_run_adapter_hook` returns, `execute_hook` reads the same immutable outer
monotonic deadline and makes the sole commit-or-release decision. A
`PayloadResult` returns normally. A `ClaimedPayloadResult` commits only when the
await completed and the immediate deadline check is still in budget; timeout,
cancellation, missing durable request ID, or an exhausted check releases the
exact token and returns the safe fallback. **Commit runs only at this async
route boundary**, the last boundary the daemon can prove before emission;
commit is honest about what remains
(response emission) and covers it with replay. Commit is a
compare-and-swap on `claim_id` executed in **one `_mutate_variables`
transaction** that removes the components from the selectable buffer and
persists one **canonical replay record** containing their deferred ack
mutations: origin request id, originating hook class, each component's
`briefing` or `turn_context` class, full canonical content by class, the one
stable persisted-context reference, acknowledged component ids, and replay
state. Lifecycle producer IDs stay attached to their `briefing` class; replay
never reinterprets them through aggregate-stash or turn-context policy. Native
PreToolUse/Stop JSON is not the durable form. Replay eligibility is
component-class-aware before wire translation. A record containing briefing
may create its one delivery gate: PreToolUse gets deny + reason + retry
instruction, while Stop/SubagentStop gets block +
`hookSpecificOutput.additionalContext`; accompanying turn-context may ride that
briefing gate if it fits. A turn-context-only record may piggyback only on a
**current real** PreToolUse deny or Stop/SubagentStop block. An ungated
PreToolUse leaves that replay, its inbox file, and current request identity
pending while returning the current ungated result. An allowing Stop follows
Locked decision 4's explicit drop arm: debug-log and clear the replay without
executing ack mutations, leaving durable producer state undelivered/retryable.
Passive hooks are incompatible and leave replay pending. A compatible current
real gate takes precedence and receives eligible replay content prepended to
its own reason/context. Before a different-class response is emitted or its
current request identity advances, `fit_grok_briefing_response` refits the full
eligible combined envelope to the receiving class's byte budget using that
stable reference. Refit/reference failure leaves the prior identity and
canonical replay unchanged. No new component selection occurs until the replay
is acknowledged or the explicit turn-context drop arm clears it.

The existing envelope terminal-response file is a same-ID cache derived from
that canonical record, not a second replay state machine: same origin/current
request ID replays its stored native response first; a different compatible
ID retranslates the canonical record. Originating and current processed
markers are retired together when the canonical record advances. The durable
ack is Gobby's existing local inbox protocol. `delivered_action` maps a 2xx
body without deleting the inbox envelope. Because `emit_action` and
`output::stdout` currently discard write errors, they gain a result-bearing
write-and-flush path; `run_gobby_owned` removes
`~/.gobby/hooks/inbox/<envelope-id>.json` only after the mapped provider action
has been written to stdout and flushed successfully. Mapping failure,
stdout-write failure, flush failure, or a crash before deletion retains the
same envelope and returns failure; deletion failure is conservative
post-emission uncertainty and also retains replay. On the next request,
absence of the record's current inbox file acknowledges crash-free delivery;
one
acknowledgment transaction executes the deferred ack mutations, advances the
bounded terminal guards, clears the canonical replay, and permits normal
handling — preserving deny-once. File
presence means post-commit delivery is uncertain, so same-ID retry or the
next compatible different-ID request replays at least once. After a
different-ID replay, the canonical record advances to that current ID and
the superseded origin marker/file may be retired; acknowledgment is then the
current inbox file's disappearance.

Daemon inbox drain is not provider delivery. `_post_envelope` marks internal
replay explicitly; if processing such a request would commit or replay Grok
delivery content, the route releases/retains the canonical state and returns
retryable 503 so the drain keeps the inbox file. In addition,
`_drain_hook_inbox_once_locked` checks the canonical replay owner before its
already-processed-marker branch unlinks anything: when that envelope ID is the
current provider-ack-pending Grok record, it retains/skips the file even though
the processed marker exists. Only ghook may unlink that file, after successful
provider-action mapping and stdout write/flush. A direct POST that commits and
writes the processed marker therefore cannot race the daemon drain into
deleting the file before ghook later fails its stdout write. This prevents an
internal ASGI replay from becoming a false acknowledgment. Disconnect, mapping failure,
stdout-write/flush failure, deletion failure, stale reclaim, same-ID retry,
different-ID PreToolUse→Stop, Stop→PreToolUse, SubagentStop, passive
incompatibility, and inbox-drain cases share this one precedence table. A
request without a durable request id releases the claim instead of
committing (retryable, no replay record). If the
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
takeover after simulated daemon death, crash-after-commit-before-emission,
mapping and stdout-write/flush failures with envelope retention, crash after
provider receipt before marker cleanup, receiving-class refit/retranslation,
and CAS-loss payload suppression.

**Activation-epoch rollover.** Every queued component, claim, and canonical
replay record stores the epoch observed at selection. `advance_activation_epoch`
never changes namespaces underneath undelivered state. If resume or explicit
re-materialization arrives while the current epoch owns queued, claimed, or
replay-owned components, the single session-lock mutation records that stable
transition key in `_activation_epoch.pending` and returns `pending_drain`
without incrementing the value. The pending state is a request-level
boundary: new lifecycle enqueue, copied webhook/pipeline dispatch, new
compact/clear admission, new P2P component enqueue, new turn-context stash,
and new staged-memory selection all remain fenced while that transition is
pending, so no producer can replenish the draining namespace; only old-epoch
release, replay, acknowledgment, explicit drop, and lease recovery proceed,
and existing epoch content follows its normal delivery, release,
lease-takeover, replay-acknowledgment, or explicit turn-context drop path.
Claims retain their token/lease and canonical replay retains its inbox-file
ack contract across daemon restart. No deferred ack mutation runs merely
because rollover was requested.

The mutation that releases the last claim, acknowledges or explicitly drops
the replay, or removes the last queued component re-enters the same CAS helper.
When queued, claimed, and replay-owned occupancy for the old epoch are all
empty, it atomically increments the value once, records the pending transition
as `transition_key`, clears `pending`, replaces the finite committed-one-shot
set with an empty set for the new epoch, and prunes compact/clear generation
watermarks and resident-class indexes to the new destination epoch. P2P rows
remain authoritative through `delivered_at` and need no epoch tombstone;
unacknowledged rows remain eligible after the transition. Source-staged
compact/clear generations retain their distinct source+generation IDs and may
be admitted only under the new epoch's empty resident-class index. A clear
successor has no destination-owned prior buffer, claim, or replay: its atomic
insert/bind initializes directly to predecessor epoch + 1 and resets the same
sets/indexes in that write. Same-key restart recovery returns the completed
epoch; a stale completion or acknowledgment carrying an older epoch/token
loses the CAS and cannot clear new-epoch state. When the final drain CAS
advances the epoch inside a delivery acknowledgment completing during
`_complete_response`, the current hook's own uncommitted selection is
re-evaluated under the new epoch before any new enqueue or claim: old-epoch
content it staged is re-selected under the new epoch's namespace or the hook
returns its class's typed retryable fallback — a stale-epoch commit is
impossible because the commit CAS carries the epoch observed at selection.
Tests suspend at every queued,
claimed, replay-owned, acknowledgment, and final-CAS boundary, restart the
daemon, race two rollover hooks with delivery acknowledgment, and enqueue
fresh P2P and turn-context work continuously through a pending drain to
prove the fenced producers cannot starve rollover.

**Bounded terminal dedupe.** No collection retains every committed component
for a long activation epoch. The committed-one-shot set contains only the
finite stable startup-packet, agent-instructions, wiki-overview, user-profile,
and persona producer identities (bounded by the five lifecycle slots) and is
replaced on activation-epoch rollover.
Compact/clear identities use a per-source highest-committed-generation
watermark, overwriting the prior generation instead of appending it; entries
exist only for the current row, its active same-terminal sibling sources, and
one clear predecessor, and are pruned when that source relationship ends or
the destination activation epoch rolls. P2P UUIDs use the message row itself
as terminal state and never enter either structure: the specialized enqueue
locks and rechecks `delivered_at` in the same hub transaction that locks and
mutates the destination variable row. Enqueue also checks component IDs held
by the one pending canonical replay record. The delivery-acknowledgment
transaction uses the two-stage token/ID peek, UUID row locks, session lock, and
exact replay-token/component-ownership revalidation before it calls the
transaction-aware `mark_delivered_batch` and clears replay on that same
connection. Deterministic lock order is message rows by UUID, then the
`SessionVariableMutation(session_id)` advisory lock before the revalidated
session-variable read/write. A stale selector arriving before acknowledgment
sees the replay-owned ID, and one arriving afterwards sees `delivered_at`;
neither can re-enqueue. Tests cover thousands of sequential P2P and compact
generations, active-source pruning, daemon restart, and activation-epoch
rollover without re-enqueue.

**Pending P2P messages.** P2P is briefing-class and rides the same one-time
PreToolUse briefing denial or text-only briefing Stop arm; it never uses the
turn-context-only policy. `EventEnricher` selects delivery candidates only
from BEFORE_AGENT, BEFORE_TOOL, and AFTER_TOOL, so 1.1's channel flip alone
would strand queued Grok messages. This leaf extends
`src/gobby/hooks/event_enrichment.py`: for Grok, pending messages stay
unclaimed until they ride an actual delivery — enqueued as components whose
ack mutation is the `mark_delivered_batch` call — included in a briefing
PreToolUse deny or an already-blocking Stop, and marked delivered only by
that component's commit. Because selection is non-claiming, two concurrent
eligible hooks can read the same `delivered_at IS NULL` rows: message
enqueue is therefore the transaction-aware **atomic upsert keyed by a stable
message-derived `component_id`** described above. It checks queued/claimed
state, the pending replay record, and the locked message row's terminal
`delivered_at`, so both a concurrent selector and a
commit-before-late-enqueue selector no-op. Selection uses the two stages above.
First choose at most `P2P_MAX_CANDIDATES` undelivered rows in
`sent_at ASC, id ASC` order and UUID-lock that bounded set before acquiring any
session advisory lock. Then enter `_mutate_variables`, acquire
`SessionVariableMutation(session_id)`, recompute queued/claimed/replay-owned
`occupied_p2p` and `occupied_total`, apply the seven-component finite in-buffer
reservation formula, recheck `delivered_at` plus component ownership, and
enqueue only the earliest fitting eligible prefix. A finite producer may fill
reserved headroom while the candidate locks are held and before the session
lock is acquired; the inside-lock recomputation shrinks the prefix accordingly.
Locked excess rows and rows beyond the bounded candidate set remain untouched,
and P2P capacity exhaustion is normal backpressure. At the nine-component P2P
ceiling, the five lifecycle one-shots plus at most one compact and one clear
still enter their seven reserved slots. A second compact or clear generation
retains its distinct ID in durable source staging until acknowledgment releases
that class slot and triggers the existing ordered take; it adds no retry or
backpressure machinery. After finite and P2P acknowledgment free admissible
slots, subsequent eligible cycles select later P2P batches in the same order
until every message is delivered.
After ghook acknowledgment, delivered marking removes a message from selection
without adding an unbounded UUID tombstone. A pending message may create the
same briefing delivery gate and never forces an allowing Stop through the
turn-context arm. Tests cover zero capacity, exactly the cap, cap-plus-one,
transaction rollback, concurrent selectors, later-batch ordering, and eventual
delivery without duplicate components or corruption logs.

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

In every arm, route commit (Claim and commit above) removes the claimed
components from the selectable buffer and persists their deferred ack
mutations (`wiki_overview_injected`, `_startup_context_injected`, compact
one-shots, message delivered marking) in the canonical replay record; only
the later inbox-absence acknowledgment transaction executes those mutations.
No ack mutation ever runs at claim, and none at stash.

Reserve `grok_pending_briefing`, `grok_pending_turn_context`, and the shared
`_activation_epoch` rollover object in
`src/gobby/workflows/reserved_variables.py`.

**Acceptance:**

- 4.1.1 - Grok startup, agent, wiki, profile, and persona lifecycle producers enqueue stable briefing component IDs directly, while genuine UPS aggregate context appends only to `grok_pending_turn_context`; neither returns additionalContext. test: `tests/hooks/test_grok_pending_context.py`.
- 4.1.2 - First Grok PreToolUse after briefing denies once with that reason; delivered-state vars remain pending through route commit and flip only when ghook's inbox-file deletion acknowledges that response. test: `tests/hooks/test_grok_pending_context.py`.
- 4.1.3 - Turn-context concatenates onto an already-blocking Stop and is dropped (debug-logged) when Stop allows; Grok Stop with only turn-context and no stop-gate allows the stop. test: `tests/hooks/test_grok_pending_context.py`.
- 4.1.4 - Grok PostCompact and clear-successor bind arm briefing-class continuation. symbol: `MiscEventHandlerMixin.handle_post_compact`. symbol: `_bind_clear_successor`.
- 4.1.5 - Grok wire envelopes for flush outputs (deny reason, Stop block + additionalContext) are asserted at the adapter layer only. test: `tests/adapters/test_acp_hook_translation.py`.
- 4.1.6 - On Grok, `inject-compact-handoff-on-prompt` and `inject-clear-handoff-on-prompt` do not fire on turn_start and the pending flags survive until confirmed flush; non-Grok CLIs keep the turn_start route. test: `tests/workflows/test_context_handoff_rules.py`.
- 4.1.7 - Briefing and turn-context merge into workflow-block and webhook-block responses under `preserve_original=True`; the adapter-visible response carries them. test: `tests/hooks/test_grok_pending_context.py`.
- 4.1.8 - Buffer bounds enforce the named numeric caps: turn-context is drop-oldest at 32 components / 16384 serialized UTF-8 bytes and rejects a single component over 8192 bytes at enqueue; briefing components are deduplicated by stable key and never cap-evicted; the 16-slot buffer reserves a seven-component finite bound (five lifecycle one-shots, one resident compact, one resident clear) and leaves `P2P_MAX_COMPONENTS = P2P_MAX_CANDIDATES = 9`; and an oversize briefing degrades at flush through the dedicated Grok helper to a non-empty UTF-8-safe head plus its persisted-context reference within the serialized native-response byte budget. test: `tests/hooks/test_grok_pending_context.py`.
- 4.1.9 - A no-UPS first PreToolUse stages the startup packet as briefing before flush and deny-once delivers it on that same event. test: `tests/hooks/test_grok_pending_context.py`.
- 4.1.10 - Regenerated bundled-content manifest exactly matches the committed shared-template tree. test: `tests/install/test_bundled_content_manifest.py::test_bundled_content_manifest_matches_tree`.
- 4.1.11 - `ACPHookAdapter.handle_native` only translates and returns `PayloadResult` or `ClaimedPayloadResult(payload, opaque_claim_token)`; `execute_hook` exhaustively distinguishes them and, after the immutable deadline check, commits the exact claim or releases it on timeout/cancellation/error, so a late worker cannot commit and no delivered flag flips until ghook acknowledgment executes deferred mutations exactly once. test: `tests/hooks/test_grok_pending_context.py`.
- 4.1.12 - Grok pending messages stay queued or replay-owned until a briefing deny or already-blocking Stop is acknowledged; `mark_delivered_batch` runs only in the acknowledgment transaction, and an allowing Stop never carries one. test: `tests/hooks/test_pending_message_provider_contracts.py`.
- 4.1.13 - An interleaved first UPS and PreToolUse (UPS creates the row first, PreToolUse completes activation first, UPS stashes last) stages exactly one startup-briefing component and yields exactly one deny-once delivery. test: `tests/hooks/test_grok_pending_context.py`.
- 4.1.14 - Grok staged-memory IDs remain retryable through stash, drop, route commit, and replay and are recorded only by ghook delivery acknowledgment. test: `tests/hooks/test_grok_pending_context.py`.
- 4.1.15 - Agent, wiki, profile, and startup one-shot markers remain unset through Grok composition, stash, route commit, and replay and flip only on ghook delivery acknowledgment. test: `tests/hooks/test_grok_pending_context.py`.
- 4.1.16 - EventEnricher preserves non-Grok inline delivery while Grok enqueues without acknowledgment across formatting and retry failures. test: `tests/hooks/test_event_enrichment.py`.
- 4.1.17 - A request-scoped ClaimHandle exists before worker submission and exposes only an opaque token to the typed worker result: timeout before materialization, after claim, during translation, and before late worker completion returns fallback, while `execute_hook` releases now or on later bind after checking the immutable deadline and prevents every late commit; a no-request-ID result releases instead of committing. test: `tests/servers/test_mcp_routes.py`.
- 4.1.18 - Claims persist owner, timestamp, and a UTC lease deadline computed from the immutable per-request effective route timeout carried with the one outer monotonic deadline through ACP event metadata plus `GROK_CLAIM_LEASE_MARGIN_SECONDS`; a claim orphaned by simulated daemon death is requeued only after lease expiry under a controlled clock, concurrent different timeout overrides compute independent deadlines and leases, commit executes only in the async route after an await within that same deadline, and CAS-loss discards the payload. test: `tests/hooks/test_grok_pending_context.py`.
- 4.1.19 - Two concurrent eligible hooks selecting the same undelivered P2P row upsert exactly one component by message-derived component_id and produce exactly one delivery. test: `tests/hooks/test_pending_message_provider_contracts.py`.
- 4.1.20 - Compact and clear arming survive injected crashes at every boundary: same-row arming completes or retries in one transaction; the `compact_continuation.py` public orchestrator remains thin and below 850 lines while `compact_staging.py` owns resolve/peek, source stage, destination take, generation-CAS consume, acknowledgment-triggered admission, and generation-keyed scheduling state; crash before schedule, crash after send before durable recording, duplicate retry, and scheduling failure preserve the staged briefing and source marker under explicit at-least-once scheduling semantics; predecessor→successor clear and a racing second generation retain distinct IDs while only one generation per class is resident. test: `tests/sessions/test_compact_continuation.py`.
- 4.1.21 - One- and many-contributor oversize briefings deliver in one response through `fit_grok_briefing_response`: full serialized PreToolUse reason and Stop/SubagentStop additionalContext envelopes stay within their asymmetric UTF-8 byte budgets, retain a non-empty character-safe head, full canonical content, and one stable persisted reference; every different-class replay refits with that helper including multibyte text and real-gate prepending, while persistence/reference failure leaves replay identity pending without acknowledgment or denial payload. test: `tests/hooks/test_grok_pending_context.py`.
- 4.1.22 - Commit atomically persists full canonical delivery content, one stable reference, component classes, origin class, request id, and replay state; same-ID native replay has precedence, every eligible different-class replay calls `fit_grok_briefing_response` for the receiving class before advancing request identity, passive hooks leave it pending, and a real receiving gate is included in the refit budget. Ghook inbox-file absence acknowledges only a mapped provider action whose stdout write and flush succeeded before deletion; mapping/emission/deletion failure retains the envelope, commit-before-emission crash/disconnect replays at least once, and `_drain_hook_inbox_once_locked` retains a provider-ack-pending Grok file on its processed-marker branch. A controlled direct-POST commit → processed marker → daemon drain → ghook stdout-failure race leaves the file and replay pending. test: `tests/hooks/test_grok_pending_context.py`. test: `tests/hooks/test_inbox.py`.
- 4.1.23 - A second PreToolUse arriving during a live unexpired claim returns a retriable non-payload deny and never allows its tool; after the owner commits or releases, the next PreToolUse proceeds normally; owner-timeout takeover is tested separately from the payload-bearing denial. test: `tests/hooks/test_grok_pending_context.py`.
- 4.1.24 - A stale racer reaching its enqueue step after the startup component committed finds its finite one-shot identity in the activation-epoch set and does not re-enqueue: the commit-before-late-stash interleaving yields no second briefing denial. test: `tests/hooks/test_grok_pending_context.py`.
- 4.1.25 - PreToolUse→Stop, Stop→PreToolUse, SubagentStop, same-ID retry, compatible different-ID replay, passive incompatibility, real-gate precedence, asymmetric receiving-class refit, persistence/reference failure, disconnect, ghook mapping/stdout-write/flush failure, stale reclaim, and post-receipt/pre-ack crash follow one canonical replay precedence table with envelope retention, at-least-once emission, and exactly-once ack mutations. test: `tests/servers/test_mcp_routes.py`.
- 4.1.26 - Terminal dedupe stays bounded over a long session: only five finite lifecycle one-shots occupy the current epoch set, compact/clear retain one generation watermark per active source relation, P2P rechecks delivered_at in the same transaction as enqueue/acknowledgment, and thousands of sequential commits plus activation-epoch rollover neither grow an unbounded collection nor re-enqueue. test: `tests/hooks/test_grok_pending_context.py`.
- 4.1.27 - Session-boundary contract, sessions guide, and variables guide document Grok PreToolUse/Stop delivery, compact/clear single ownership, and acknowledgment-time clearing semantics. file: `docs/contracts/session-boundary.md`, `docs/guides/sessions.md`, `docs/guides/variables.md`.
- 4.1.28 - P2P acknowledgment first peeks the replay token and P2P IDs without locks, then opens a no-initial-lock immediate transaction, UUID-locks and eligibility-checks those rows, acquires `SessionVariableMutation(session_id)` through connection-aware `_mutate_variables`, revalidates the exact replay token/component ownership, and only then atomically runs `mark_delivered_batch`, all ack mutations, and replay advance/clear; stale peeks and injected failures acknowledge nothing. test: `tests/workflows/test_state_manager.py`.
- 4.1.29 - Replay eligibility preserves component class: Stop(real block + turn-context only) → ungated PreToolUse leaves replay pending without creating a deny, and PreToolUse(real deny + turn-context only) → allowing Stop follows the explicit drop arm without creating a block or executing ack mutations; briefing-containing replay may create its one delivery gate in both directions. test: `tests/hooks/test_grok_pending_context.py`.
- 4.1.30 - P2P is briefing-class and never exceeds its nine-slot static ceiling: nine P2P plus five lifecycle one-shots, one resident compact, and one resident clear fill exactly 16; second compact and clear arrivals while the resident is queued, claimed, or replay-owned retain distinct source-staged IDs, enter only on acknowledgment-triggered class-slot release, and later P2P batches drain in `sent_at`/id order without duplicate components, corruption logs, or general retry/backpressure machinery. test: `tests/hooks/test_pending_message_provider_contracts.py`.
- 4.1.31 - P2P selection first UUID-locks at most nine earliest candidates, then acquires `SessionVariableMutation(session_id)`, recomputes reservation-aware live capacity inside `_mutate_variables`, and enqueues only the earliest fitting eligible prefix; a finite-source enqueue between candidate selection and session-lock acquisition shrinks that prefix while excess rows remain untouched. test: `tests/hooks/test_pending_message_provider_contracts.py`. test: `tests/workflows/test_state_manager.py`.
- 4.1.32 - In the reverse first-activity interleaving, PreToolUse creates the row and commits briefing first while a losing UPS reaches each lifecycle producer before and after acknowledgment; stable producer IDs yield one briefing identity, zero aggregate-stash turn-context duplicate, and exactly one acknowledgment mutation. test: `tests/hooks/test_grok_pending_context.py`.
- 4.1.33 - Resume, clear, and inactive-row re-materialization roll activation epoch through one CAS owner: queued, claimed, and replay-owned old-epoch state drains under its existing token/ack contract before a pending rollover advances once; daemon restart plus concurrent rollover/acknowledgment races preserve one epoch, clear committed-one-shot and watermark state only at the atomic advance, keep undelivered P2P rows eligible, and reject stale old-epoch completion. test: `tests/hooks/test_grok_pending_context.py`.
- 4.1.34 - P2P acknowledgment-versus-enqueue and acknowledgment-versus-legacy-merge races obey the common UUID-row-lock → `SessionVariableMutation` order; replay-token revalidation prevents stale ownership acknowledgment, and rollback preserves delivered rows, ack mutations, canonical replay, and buffer state as one unit. test: `tests/storage/test_inter_session_messages.py`.
- 4.1.35 - Adapter worker-result constructors and async-route pattern matching exhaustively distinguish ordinary payload from payload plus opaque claim token; `handle_native` has no commit/release path, and only `execute_hook` commits or releases after the immutable deadline check. test: `tests/adapters/test_acp_hook_integration.py`.
- 4.1.36 - Direct AGY handle_native consumer exhaustively unwraps PayloadResult while preserving the native payload contract. test: `tests/adapters/test_adapters_agy.py`.
- 4.1.37 - While `_activation_epoch.pending` is set, new P2P component enqueue, turn-context stash, and staged-memory selection are fenced alongside lifecycle, copied-dispatch, and compact/clear producers — only old-epoch release, replay, acknowledgment, explicit drop, and lease recovery proceed; continuous P2P and turn-context arrivals through a pending drain cannot starve rollover, and a final-acknowledgment epoch advance during `_complete_response` re-evaluates the current hook's uncommitted selection under the new epoch or returns its typed per-class retryable fallback. test: `tests/hooks/test_grok_pending_context.py`.
- 4.1.38 - At most one live claim or canonical replay exists per session across all hook classes: concurrent PreToolUse-versus-Stop/SubagentStop races in both completion orders prove the losing class never claims or attaches pending components, a real Stop/SubagentStop still emits its own gate content, and every release, timeout, and lease-takeover branch recovers the losing claim without stranding a claimed set. test: `tests/hooks/test_grok_pending_context.py`.
- 4.1.39 - Controlled pre-token persistence, reference, and budget failures CAS-release the newly created claim through `ClaimHandle` before the retryable fallback returns — no live claim remains and the next eligible hook selects the same components — while receiving-class refit/retranslation failure on an already committed canonical replay preserves the replay, its identity, and its stable reference unchanged. test: `tests/hooks/test_grok_pending_context.py`.

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
   and `reason` contains the briefing; after ghook receives/maps that response,
   writes and flushes the provider stdout action successfully, and only then
   deletes its inbox envelope (the delivery acknowledgment), the second
   identical PreToolUse is allow unless another gate fires. A simulated
   mapping error, stdout write/flush failure, disconnect, or crash before
   deletion retains the envelope and replays the denial at least once. The
   E2E also forces direct POST commit and processed-marker creation, runs the
   daemon drain before ghook emission, then fails ghook stdout; the drain must
   retain the provider-ack-pending file and the next request must replay it.
6. Grok `stop` with a real gate (`require-task-close` / `tool_block_pending`)
   → `decision: "block"` (not `"deny"`) and `continue: true`. Grok `stop`
   with only leftover turn-context and no gate → allow (no continuation loop).
7. Compact/pre-created: SessionStart with `session_start_source="compact"` on
   an existing row does not create a second row. Grok `post_compact` on an
   existing row arms `grok_pending_briefing`; next PreToolUse denies once
   with the continuation block.
8. After ghook acknowledges a successful briefing response,
   `wiki_overview_injected` / `_startup_context_injected` are true; after
   stash or route-commit-without-ack they are still false.
9. With nine P2P components plus the five lifecycle one-shots, one compact,
   and one clear resident, a second compact and clear generation stay durably
   source-staged with distinct IDs while the first is queued, claimed, or
   replay-owned. Successful ghook acknowledgment releases each resident class
   slot, admits the next staged generation in order, and later drains P2P.
10. Restart with a resume epoch transition pending behind queued, claimed, and
    replay-owned state. The old epoch survives until normal release/delivery
    acknowledgment, then advances once; a concurrent rollover hook cannot
    advance it twice, and a clear successor starts at predecessor epoch + 1.
11. Force acknowledgment to race both P2P enqueue and legacy variable merge.
    The stale replay-token branch acknowledges nothing, while the winning
    branch marks rows delivered, runs ack mutations, and advances replay in one
    transaction. Adapter integration also proves `handle_native` returns the
    typed opaque-token result and the async route alone commits after its
    immutable deadline check.

Keep it one file, no full pytest. Command:

```bash
GOBBY_TEST_PROTECT=1 uv run pytest tests/e2e/test_grok_session_deferral.py tests/adapters/test_acp_hook_translation.py tests/adapters/test_acp_hook_integration.py tests/adapters/test_capabilities.py tests/hooks/test_session_events_coverage.py tests/hooks/test_grok_pending_context.py tests/hooks/test_pending_message_provider_contracts.py tests/workflows/test_state_manager.py -q
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
- 5.1.5 - After ghook maps the briefing denial, successfully writes and flushes its provider stdout action, and then acknowledges by deleting its inbox envelope, a second PreToolUse allows absent another gate and Stop with only turn-context also allows; mapping, emission, disconnect, or pre-deletion failure retains the envelope and replays the denial at least once, and the direct-POST commit → processed marker → daemon drain → ghook stdout-failure race retains the provider-ack-pending file. test: `tests/e2e/test_grok_session_deferral.py`.
- 5.1.6 - Compact binding creates no duplicate row and delivered-state markers remain false before flush and after route commit, then become true after ghook acknowledgment. test: `tests/e2e/test_grok_session_deferral.py`.
- 5.1.7 - All four existing CLIEventSimulator SessionStart consumer suites pass after their row and context assumptions move to first activity. test: `GOBBY_TEST_PROTECT=1 uv run pytest tests/e2e/test_full_workflow.py tests/e2e/test_session_tracking.py tests/e2e/test_stateless_ambient_session.py tests/e2e/test_worktrees_e2e.py -q`.
- 5.1.8 - Saturation smoke proves 9 P2P + 5 lifecycle + one compact + one clear occupy exactly 16 slots; second compact/clear generations remain distinctly staged for queued, claimed, and replay-owned residents and enter only after acknowledgment-triggered release. test: `tests/e2e/test_grok_session_deferral.py`.
- 5.1.9 - Restart and rollover smoke proves resume/re-materialization waits for old queued, claimed, and replay-owned state, advances exactly once after acknowledgment, and initializes a clear successor to predecessor epoch + 1 without stale completion crossing epochs. test: `tests/e2e/test_grok_session_deferral.py`.
- 5.1.10 - Focused concurrency and adapter contracts prove acknowledgment's peek/UUID-lock/session-lock/revalidate protocol against enqueue and legacy merge, plus translation-only typed worker results and async-route-only commit-or-release after the immutable deadline check. test: `tests/workflows/test_state_manager.py`. test: `tests/adapters/test_acp_hook_integration.py`.

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
4. Focused: session coverage + hook manager tests, plus all four existing
   CLIEventSimulator SessionStart consumer suites after their row/context
   assumptions move to first activity.
5. Optional live: idle `grok` then first prompt, confirm session list timing
   and one-shot briefing deny; `compact_self` then first tool shows the
   continuation block in a deny reason, not via `wait_for_summary`.
6. Verify the preflight and both phases of the coordinator-owned collision
   cleanup (Constraints, Collision section). Immediately after approval and
   before any root, dependency, plan-artifact, ledger, manifest, #20635, or
   obsolete-leaf mutation, `observe_frozen_migration_408` recorded exit 0,
   `latest=407 408_count=0`, the exact approved HEAD/command/stdout, and the
   pre-observation state digests; each mismatch case returned to review with a
   byte-identical task/plan/ledger/manifest/dependency snapshot. Preflight then
   inventoried every `plan-root:grok-hook-deferred-materialization:*` label and
   every Plan-ID/root registry binding before the exact-current-label branch.
   Old-hash unbound, bound, and terminal roots and foreign/mismatched bindings
   all failed closed without create; only a completely empty namespace created
   one paused replacement epic. The crash-after-insert/before-response probe
   recovered that same epic, and #20635's one replacement edge read back while
   every old edge remained. The coordinator's root-ledger sealing probe
   replaced only `root_task_ref: "SEALED-BY-COORDINATOR"` with the normalized
   recovered epic, while the separate install-time `plan_hash` seal stayed
   untouched; response-loss retry accepted only the already-sealed root and
   reproduced the recorded ledger hash, five section identities, acceptance
   text/set, and expected-leaf inventory. A sentinel, foreign root, or byte
   mismatch blocked `create_plan`.
   Restart probes after the new plan-row commit, lost `create_plan` response,
   and managed-manifest generation boundary recovered the same bound root by
   exact Plan ID/root/hash/path, regenerated only a missing or stale managed
   manifest, rejected every foreign/mismatched binding, and resumed at the
   first incomplete Phase-B step without a duplicate root or plan row.
   Phase A completed before
   expansion began: compact-summary-fidelity file/M1/companion-ledger edits
   completed first; `update_plan_hash` plus explicit
   `regenerate_coverage_manifest`, validation, and recovery checks established
   one synchronized artifact checkpoint before any task mutation; #20635 then
   stayed open with migrated title, hook-delivery description and criteria,
   `deferred-from` provenance, original §3/3.1 reference, and #20724 parentage;
   its replacement blocked-by edge existed before old edges were removed; old
   P2 deliverables, M1 entries, ledger rows, covers labels, and obsolete-leaf
   edges were removed; #20727/#20733–#20735 closed `obsolete`; §3 retained its
   `task_ref`, §4 moved off `wait_for_summary`, and DB-backed coverage ended
   with every remaining row covered. Phase B completed before the `gobby build`
   automation opt-in: the companion coverage ledger existed and matched the
   sealed plan hash, all acceptance items, and five expected leaves;
   `observe_frozen_migration_408` revalidated the same approved-HEAD evidence
   immediately before `create_plan` and immediately before expansion; both
   mismatch paths performed no further mutation. The later runner test
   independently proved
   implemented 408 follows embedded 407; the
   approved plan and managed coverage artifact were bound through
   `gobby-plans:create_plan`, manual expansion completed on that paused root,
   #20635's pre-existing replacement edge read back clean, and only then
   `gobby build '#<epic>'`. #20635 closes
   `completed`/`already_implemented` only when the epic delivers. #20726
   untouched.
7. Exercise the repaired runtime ownership boundaries together. The durable
   `_activation_epoch` row object initializes fresh deferred rows at 1 and
   clear successors at predecessor + 1; stable-keyed resume, clear, and
   re-materialization transitions have one CAS winner across restart, queued,
   claimed, replay-owned, acknowledgment, committed-one-shot, and watermark
   races. A fully occupied buffer contains exactly 9 P2P + 5 lifecycle + 1
   compact + 1 clear component; newer compact/clear generations retain
   distinct source-staged IDs until acknowledgment-triggered admission, and
   later P2P drains in order. P2P acknowledgment peeks token/IDs, UUID-locks
   rows, acquires `SessionVariableMutation`, revalidates ownership, and commits
   delivered rows + ack mutations + replay advance atomically against enqueue
   and legacy-merge racers. `ACPHookAdapter.handle_native` returns only the
   typed payload or payload-plus-opaque-token result; `execute_hook` owns the
   post-await immutable-deadline commit-or-release decision. Copied-gate tests
   retain
   `gate_deadline = min(aggregate_deadline, outer_deadline - COPIED_GATE_LIVE_RESERVE_SECONDS)`
   with the 15-second aggregate deadline, 1-second reserve, and 50-ms loser
   polling.

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

**Round 6** `kind: verification`

- reviewer_run: 2c1024d5-207a-4612-b722-710bc3678df3
- reviewer_session: 7ed2c179-0e3a-4f56-9eaa-a2ce919600d6
- verdict: needs_review
- findings:
- GHDM-R6-F01/blocking: required companion `.coverage-ledger.yaml` bootstrap ledger is absent; Phase B expansion lacks its reviewed-parity precondition. Vote: accepted — every sibling implementation plan ships one; file missing on disk.
- GHDM-R6-F02/blocking: #20635's title still names the superseded MCP-result design after the Phase A criteria migration. Vote: accepted — title joins the persisted readback comparison.
- GHDM-R6-F03/blocking: Phase A removes the old blocked-by edges and closes their owners before Phase B creates the replacement epic; a crash between phases leaves the carrier with no durable prerequisite. Vote: accepted — create and attach the paused epic before removing old edges.
- GHDM-R6-F04/blocking: coverage generation failing after `update_plan_hash` commits the new hash leaves the registered plan paired with stale coverage, and the retry skips generation on an unchanged hash; retirements must follow a verified sync checkpoint. Vote: accepted.
- GHDM-R6-F05/blocking: three required §4.1 contract/guide rewrites have no acceptance item among 4.1.1–4.1.26. Vote: accepted — typed repair supplied.
- GHDM-R6-F06/blocking: ghook deletes the inbox envelope on 2xx mapping failure and, on success, before provider-stdout emission — so file-absence-as-ack can commit deferred mutations for a denial Grok never received. Vote: accepted — refines the ratified F12 ack (deletion moves after a successful provider-stdout write/flush; the inbox-file mechanism itself stands).
- GHDM-R6-F07/blocking: migration 405 already exists in committed assets (live head 407); `405_pipeline_execution_idempotency_key.sql` cannot land as written. Vote: accepted — renumber to the next free head at execution (currently 408) and sweep every derived carrier.
- GHDM-R6-F08/blocking: §4.1's one-transaction P2P invariant lacks a connection-aware `SessionVariableManager._mutate_variables` seam; the state manager always opens its own transaction. Vote: accepted — typed repairs supplied.
- GHDM-R6-F09/blocking: `compact_continuation.py` is already 952 lines and cannot absorb the four-operation refactor under the 1,000-line ceiling. Vote: accepted — support-module split target required.
- GHDM-R6-F10/blocking: compatible different-class replay never refits canonical content through `fit_grok_briefing_response` for the receiving class's serialization budget. Vote: accepted — refines the ratified F11 retranslation.
- GHDM-R6-F11/blocking: the four-operation continuation refactor omits the existing continuation-prompt scheduling side effect from the crash table. Vote: accepted.
- GHDM-R6-F12/blocking: an `adapter_timeout` override below 15 seconds cancels the route before the copied gate's reserve boundary, escaping the typed short-circuit and takeover fence. Vote: accepted — refines the ratified F06 deadline (gate boundary = lesser of the 15-second aggregate deadline and remaining outer budget minus the reserve).
- resolution_notes: All 12 findings accepted. 9 of 12 are fixer-induced by round-5 repairs; F01/F05 are coverage gaps and F07 is environment drift (schema head advanced through 407 after the plan pinned 405). None reverse the four user-ratified round-5 forks — F06/F10/F12 refine them inside their ratified mechanisms. Repairs delegated to a second reviewer-authored candidate sitting (typed F05/F08 entries verbatim in the candidate); the deterministic gate (base + expansion validate) reruns on the merged canonical before round 7. F01's ledger file is coordinator-created from the candidate's enumeration once the repaired plan bytes seal.

```json plan-review-round
{"evidence_id":"d73dd2e1-da6a-4520-998a-6ef6bb31b926","plan_hash":"c0c827f06d1c0c8eff1ec68ae2e0b847b4218aacfdd0e84d79b640ebfea47936","round_number":6,"round_result":{"coverage_attestation":{"adjacent_variant_complete":true,"attestation_digest":"2e64ff494e94e9561950ebd2c61af6463a04dd7ef187b67c2ce72c29dd697a58","cross_lane_interaction_complete":true,"disposition_counts":{"dismissed":2,"emitted_findings":12,"total":14},"evidence_id":"d73dd2e1-da6a-4520-998a-6ef6bb31b926","lanes":[{"candidate_count":5,"lane_id":"requirements_traceability","status":"completed"},{"candidate_count":4,"lane_id":"repository_blast_radius","status":"completed"},{"candidate_count":5,"lane_id":"runtime_invariants","status":"completed"}],"shadow_manifest_status":{"entry_count":5,"manifest_digest":"2f6f8d46f980b172de92b273669d598767be42a255c9b3d877654f8c0138056e","status":"valid"},"source_digest":"003169ff0fead9cd1a44c2ae6c63d26dc9a03ba39643f6f888f01c13a61e9658","version":1},"findings":[{"category":"missing-requirement","check_key":"requirement-owner","description":"The required `.gobby/plans/grok-hook-deferred-materialization.coverage-ledger.yaml` is absent. Phase B binds and expands using only the managed coverage artifact, so expansion lacks the separately required reviewed bootstrap ledger.","finding_id":"GHDM-R6-F01","fix":"Add the companion coverage ledger with Plan ID/hash, all 73 acceptance items, and the five expected leaves; include it in the next immutable review snapshot and require its parity before Phase B expansion.","location":"Plan bootstrap / Phase B expansion precondition","prevention":"Before approving any new epic plan, verify `<plan>.coverage-ledger.yaml` exists, matches the sealed plan hash, enumerates every acceptance item, and maps every expected leaf.","principle":"Every new implementation epic must ship the adversary-reviewed bootstrap coverage ledger required by the plan-coverage contract.","root_cause":"The plan treats the managed DB-backed coverage manifest created during binding as if it also satisfied the separate companion-ledger requirement.","section_id":"5.1","severity":"blocking"},{"category":"traceability","causal_finding_id":"GHDM-R5-F02","causal_section_ids":["Collision with compact-summary-fidelity (#20724)","V2"],"check_key":"requirement-owner","description":"#20635 remains titled “Give Grok a real context delivery channel … via MCP results,” while this plan makes MCP-result delivery a non-goal and eventually closes that carrier as completed/already_implemented.","finding_id":"GHDM-R6-F02","fix":"Update #20635's title in the same Phase A mutation to name PreToolUse denial and Stop/SubagentStop delivery, then include title in the persisted readback comparison.","introduced_in_round":5,"location":"Collision cleanup / Phase A step 1 carrier migration","prevention":"When a deferral changes delivery architecture, compare title, description, criteria, artifacts, provenance, parentage, and dependency edges in one readback checklist.","principle":"A migrated deferred carrier's persisted identity and criteria must describe one coherent delivery contract.","root_cause":"The repair migrated #20635's description and validation criteria while leaving its title on the superseded MCP-result design.","section_id":"Collision with compact-summary-fidelity (#20724)","severity":"blocking"},{"category":"bad-sequencing","causal_finding_id":"GHDM-R5-F01","causal_section_ids":["Collision with compact-summary-fidelity (#20724)","V2"],"check_key":"atomic-leaf-cutover","description":"Phase A removes #20635's edges to #20733–#20735 and closes those owners before Phase B creates the replacement epic and attaches it. A crash between phases leaves the carrier open with no durable implementation prerequisite.","finding_id":"GHDM-R6-F03","fix":"Create and read back the paused replacement epic immediately after approval; add #20635's blocked-by edge to it before removing old edges or closing obsolete tasks. Bind and expand the already-paused epic after Phase A synchronization.","introduced_in_round":5,"location":"Collision cleanup / boundary between Phase A steps 2–3 and Phase B steps 1–4","prevention":"For ownership migrations, create and verify the replacement owner and attach its dependency before removing the final old owner edge.","principle":"A deferred external prerequisite must remain represented by a durable blocked-by edge throughout a resumable migration.","root_cause":"The replacement epic is created only after Phase A removes every old #20635 blocker.","section_id":"Collision with compact-summary-fidelity (#20724)","severity":"blocking"},{"category":"bad-sequencing","causal_finding_id":"GHDM-R5-F03","causal_section_ids":["Collision with compact-summary-fidelity (#20724)","V2"],"check_key":"edge-case-coverage","description":"Phase A edits the old plan/M1/ledger and mutates labels, edges, and closures before synchronization. If coverage generation fails after `update_plan_hash` commits the new hash, retrying `update_plan_hash` sees no hash change and skips generation, leaving the registered plan paired with stale coverage and already-retired tasks.","finding_id":"GHDM-R6-F04","fix":"Finish all compact-plan/M1/ledger edits first, run `update_plan_hash`, explicitly regenerate and verify the managed manifest, and define retry/restore recovery for a post-hash generation failure. Only after that checkpoint may labels, dependencies, and old tasks be mutated or closed.","introduced_in_round":5,"location":"Collision cleanup / Phase A plan-row and coverage synchronization","prevention":"Trace file, registry hash, companion ledger, managed manifest, labels, dependencies, and closures through success, generation failure, retry, and rollback before scheduling retirement.","principle":"Irreversible task retirement must follow a recoverable, verified synchronization of the governing plan row and coverage artifacts.","root_cause":"The plan postpones plan-hash synchronization until after task mutations and assumes update_plan_hash atomically regenerates coverage, while the implementation commits the hash before a separate generation call.","section_id":"Collision with compact-summary-fidelity (#20724)","severity":"blocking"},{"category":"weak-testability","check_key":"acceptance-observability","description":"`docs/contracts/session-boundary.md`, `docs/guides/sessions.md`, and `docs/guides/variables.md` are required §4.1 changes, yet none of 4.1.1–4.1.26 validates their PreToolUse/Stop route, single-owner compact/clear behavior, or acknowledgment-time clearing semantics.","finding_id":"GHDM-R6-F05","fix":"Add a §4.1 acceptance item covering all three documents and those exact contract assertions.","location":"P4 / §4.1 documentation targets and acceptance","prevention":"Map every documentation Target and every normative prose rewrite to at least one acceptance item before deriving manifest labels.","principle":"Every required changed artifact needs an observable acceptance outcome.","repairs":[{"items":[{"artifact":"file: `docs/contracts/session-boundary.md`, `docs/guides/sessions.md`, `docs/guides/variables.md`","prose":"Session-boundary contract, sessions guide, and variables guide document Grok PreToolUse/Stop delivery, compact/clear single ownership, and acknowledgment-time clearing semantics"}],"kind":"add_acceptance","section_id":"4.1"}],"root_cause":"The body requires three contract/guide rewrites while all 26 acceptance items focus on code and tests.","section_id":"4.1","severity":"blocking"},{"category":"unhandled-edge","causal_finding_id":"GHDM-R5-F12","causal_section_ids":["Locked decisions","4.1","5.1"],"check_key":"edge-case-coverage","description":"Inbox-file absence is unsound as specified: ghook removes the file when a 2xx body cannot be mapped, and on success removes it before provider stdout emission. A mapping error or crash/write failure after deletion can make the next request execute deferred ack mutations and clear replay even though Grok never received the denial/block.","finding_id":"GHDM-R6-F06","fix":"Target `crates/ghook/src/dispatch.rs` and `crates/ghook/tests/contract.rs`; retain the envelope through mapping and delete it only after a successful provider-stdout write/flush. Retain it on mapping/emission failure, then test every crash boundary with same-ID drain and different-ID compatible replay.","introduced_in_round":5,"location":"P4–P5 / ghook inbox-file acknowledgment and replay envelope","prevention":"Trace response commit, action mapping, stdout write/flush, inbox deletion, provider receipt, daemon drain, and replay as one ordered crash table.","principle":"A durable acknowledgment marker may advance delivery state only after the provider-facing action has been emitted successfully.","root_cause":"The repair assumed current ghook deletion followed successful action delivery, but `delivered_action` deletes on mapping failure and deletes successful envelopes before `run_gobby_owned` writes the action to stdout.","section_id":"4.1","severity":"blocking"},{"category":"unhandled-edge","check_key":"edge-case-coverage","description":"The plan creates `405_pipeline_execution_idempotency_key.sql`, but migration 405 already exists and committed assets include 406 and 407 with `latest_version: 407`. The named migration cannot be added or embedded as written.","finding_id":"GHDM-R6-F07","fix":"Renumber it to the next free version at execution—currently 408—and replace every 405 target, inventory/count assertion, schema identity, grant bundle, signed positive golden, and acceptance reference with the actual new head.","location":"P3 / pipeline-execution idempotency migration","prevention":"Resolve the live schema head immediately before sealing a plan and sweep every derived carrier when assigning the next migration number.","principle":"A new schema migration must use a unique version strictly above the committed catalog head.","root_cause":"The plan pinned migration 405 while the repository advanced through 407.","section_id":"3.1","severity":"blocking"},{"category":"traceability","causal_finding_id":"GHDM-R5-F14","causal_section_ids":["3.1","4.1"],"check_key":"targets-complete","description":"`SessionVariableManager._mutate_variables` accepts only decoded variables and always opens its own transaction; `mark_delivered_batch` independently queries through the database wrapper. Without changing the state-manager seam, §4.1 cannot implement its UUID-sorted message-lock then session-row mutation on one connection.","finding_id":"GHDM-R6-F08","fix":"Add a caller-supplied/connection-aware `_mutate_variables` seam that preserves existing callers, target its tests, and prove injected failures roll back message delivery, canonical replay, and buffer mutation together.","introduced_in_round":5,"location":"P3–P4 / P2P delivered-row and replay acknowledgment transaction","prevention":"For every multi-storage atomicity claim, trace connection ownership through each helper and target the first helper that opens an independent transaction.","principle":"A stated same-transaction invariant must own the lowest shared transaction seam and its rollback tests.","repairs":[{"entries":["`src/gobby/workflows/state_manager.py::SessionVariableManager._mutate_variables`","`tests/workflows/test_state_manager.py::*` — scope-reason: transaction-aware connection injection, rollback, and deterministic lock-order cases span the state-manager test module"],"kind":"add_targets","section_id":"4.1"},{"items":[{"artifact":"test: `tests/workflows/test_state_manager.py`","prose":"P2P enqueue and acknowledgment use one caller-supplied hub transaction with UUID-sorted message locks before session-row mutation, and injected failures roll back delivered state, canonical replay, and buffers together"}],"kind":"add_acceptance","section_id":"4.1"}],"root_cause":"The repair requires one hub connection across message-row locks, delivered marking, replay clearing, and session-variable mutation while targeting only mark_delivered_batch; `_mutate_variables` still opens its own transaction.","section_id":"4.1","severity":"blocking"},{"category":"gobby-format","causal_finding_id":"GHDM-R5-F13","causal_section_ids":["4.1"],"check_key":"targets-complete","description":"`src/gobby/sessions/compact_continuation.py` is already 952 lines. §4.1 adds multiple operations around private destructive helpers without a new support-module Target, making ceiling-compliant implementation infeasible.","finding_id":"GHDM-R6-F09","fix":"Create and target a support module such as `src/gobby/sessions/compact_staging.py`, move the peek/stage/take/generation-CAS implementation and relevant helpers there, keep the public orchestrator thin, and require `compact_continuation.py` below 850 lines.","introduced_in_round":5,"location":"P4 / compact-continuation decomposition","prevention":"Spot-check current line counts for every newly targeted production file and name a support-module split whenever a file is at or above 850 lines.","principle":"A plan must decompose any hand-maintained production file before planned growth can cross the 1,000-line ceiling.","root_cause":"The repair added peek/stage/take/generation-CAS machinery to a 952-line module and targeted only its public wrapper.","section_id":"4.1","severity":"blocking"},{"category":"unhandled-edge","causal_finding_id":"GHDM-R5-F11","causal_section_ids":["4.1"],"check_key":"edge-case-coverage","description":"`fit_grok_briefing_response` fits the original PreToolUse reason or Stop additionalContext, while compatible different-class replay merely “retranslates canonical content.” The plan never requires a receiving-class refit, so a PreToolUse→Stop or Stop→PreToolUse replay can exceed its envelope budget or reuse a head/reference computed for the wrong class.","finding_id":"GHDM-R6-F10","fix":"Persist full canonical content plus one stable reference and call `fit_grok_briefing_response` for every different-class replay before advancing current request identity. Add asymmetric PreToolUse↔Stop/SubagentStop byte-budget tests with one/many contributors, multibyte text, real-gate prepending, and persistence failure.","introduced_in_round":5,"location":"P4 / canonical-content cross-class replay","prevention":"Cross every replay producer and receiver with asymmetric budgets, full-envelope serialization, multibyte boundaries, persistence reuse/failure, and real-gate prepending.","principle":"A canonical payload retransmitted through a different native response class must be revalidated against that class's complete serialization budget.","root_cause":"The repair separates original fitting from cross-class retranslation without connecting the receiving replay path to the fitting helper.","section_id":"4.1","severity":"blocking"},{"category":"unhandled-edge","causal_finding_id":"GHDM-R5-F13","causal_section_ids":["4.1"],"check_key":"edge-case-coverage","description":"`consume_and_schedule_compact_self_continuation` currently schedules the continuation prompt after taking the marker. The planned resolve/peek, source-stage, destination-take, and generation-CAS consume operations—and acceptance 4.1.20—never specify when scheduling occurs or how scheduling failure/duplicate retry composes with those durable writes.","finding_id":"GHDM-R6-F11","fix":"Add prompt scheduling as an explicit generation-keyed operation with stated at-least-once/idempotency semantics. Cover crash before schedule, crash after schedule before durable recording, retry duplicate, and scheduling failure while preserving the staged briefing and source marker.","introduced_in_round":5,"location":"P4 / compact continuation four-operation refactor","prevention":"Before refactoring a destructive orchestrator, list each read, write, scheduled side effect, return value, and recovery action and place every one in the new crash table.","principle":"A crash-safe refactor must preserve every externally visible side effect of the public operation.","root_cause":"The repair enumerated durable staging operations while omitting the existing continuation-prompt scheduling effect.","section_id":"4.1","severity":"blocking"},{"category":"unhandled-edge","causal_finding_id":"GHDM-R5-F06","causal_section_ids":["Locked decisions","3.1"],"check_key":"edge-case-coverage","description":"`adapter_timeout` accepts overrides below 15 seconds and `_run_adapter_hook` enforces them with `asyncio.wait_for`. Such an override can cancel the route before the copied gate reaches its one-second reserve boundary, leaving its worker/owner claim to complete outside the plan's typed per-class short-circuit and takeover fence.","finding_id":"GHDM-R6-F12","fix":"Carry one immutable outer monotonic deadline into copied processing and derive the gate boundary from the lesser of the ratified 15-second aggregate deadline and remaining outer budget minus the reserve, or enforce a proven configuration minimum. Add controlled-clock short/equal/long overrides and route-cancellation fencing tests.","introduced_in_round":5,"location":"P3–P4 / copied blocking-gate deadline versus outer route timeout","prevention":"Test nested deadlines with outer budgets shorter than, equal to, and just above the inner window, including late worker completion and owner takeover.","principle":"An inner owner/loser/takeover protocol must resolve or be fenced before its enclosing route can time out.","root_cause":"The repair fixes the copied gate at a 15-second aggregate window while the outer adapter timeout remains any positive per-request value.","section_id":"3.1","severity":"blocking"}],"reviewer_session":"7ed2c179-0e3a-4f56-9eaa-a2ce919600d6","round":6,"round_number":6,"verdict":"needs_review"},"session_id":"ec1cd52b-590d-4658-9bbd-6a37a5ddb086"}
```

**Round 7** `kind: verification`

- reviewer_run: ed12aa04-4d10-46c4-bf50-feaf846971a2
- reviewer_session: 27cf8cc2-338c-4eba-95cd-f8a7d368ad7f
- verdict: needs_review
- findings:
- GHDM-R7-F01/blocking: four existing CLIEventSimulator SessionStart E2E consumer suites whose assumptions §3.1 removes are absent from §5.1 acceptance and its focused command. Vote: accepted — typed repair supplied.
- GHDM-R7-F02/blocking: new public `SessionResolutionOutcome` type and constructors fall outside the three exact `session_lookup.py` Targets. Vote: accepted — rescope ownership.
- GHDM-R7-F03/blocking: new `CopiedGateResult` and endpoint-outcome symbols fall outside the exact `webhook.py` function Targets. Vote: accepted — rescope ownership.
- GHDM-R7-F04/blocking: daemon `_drain_hook_inbox_once_locked` can unlink a still-live inbox file on the processed-marker branch before ghook maps/emits, falsely acknowledging undelivered content. Vote: accepted — drain must retain/skip provider-ack-pending Grok records; only ghook unlinks after successful stdout write/flush.
- GHDM-R7-F05/blocking: connection-aware `_mutate_variables` seam locks the session row while legacy writers serialize via `SessionVariableMutation` advisory locks, allowing a lost update. Vote: accepted — supplied transaction acquires the same advisory lock, ordered after UUID-sorted message locks.
- GHDM-R7-F06/blocking: preflight paused-root creation is non-idempotent; crash-after-commit-before-response loses the only root ID and a rerun creates a duplicate epic. Vote: accepted — deterministic provenance label + query-and-reuse before create.
- GHDM-R7-F07/blocking: exact `408_...sql` Target combined with permission to renumber after approval reopens the 405-style drift as an unreviewed deviation. Vote: accepted — freeze 408 and add a pre-expansion occupancy check that returns to review (minimal option; no new re-seal mechanism).
- GHDM-R7-F08/blocking: §3.1 prose is implementable as `min(aggregate, outer) - reserve`, shrinking the ratified 15-second aggregate window to 14 under a long outer budget. Vote: accepted — state `gate_deadline = min(aggregate_deadline, outer_deadline - COPIED_GATE_LIVE_RESERVE_SECONDS)` verbatim; restores the user-ratified budget exactly.
- GHDM-R7-F09/blocking: cross-class replay eligibility ignores component classes; turn-context-only replay can manufacture a deny on an ungated PreToolUse (or block an allowing Stop), contradicting Locked decision 4. Vote: accepted — persist component classes; briefing may create its one gate; turn-context-only replay piggybacks only on a current real deny/block.
- GHDM-R7-F10/blocking: unbounded `get_undelivered_messages` collides with the 16-key briefing cap; per-message enqueue can roll back or strand a cap-plus-one batch. Vote: accepted — classify P2P as briefing-class with deterministic bounded slot-ordered batching (`sent_at`/UUID), later batches drained without treating capacity as corruption; coherent with the existing P2P-rides-briefing-denial promise.
- resolution_notes: All 10 findings accepted. 6 of 10 fixer-induced by round-6 repairs, 2 by round-5 target scoping, 1 latent from round 1 (P2P capacity), 1 new E2E acceptance gap. F08 restores the exact user-ratified deadline algebra the round-6 prose drifted. No product-scope forks: F07 takes the minimal freeze-plus-occupancy-check option, F09 realigns with Locked decision 4, F10 follows the plan's existing delivery promise. Repairs delegated to a third reviewer-authored candidate sitting (typed F01 verbatim); the deterministic gate (base + expansion validate) reruns on the merged canonical, and the coverage ledger is regenerated and re-sealed to the new plan hash before round 8.

```json plan-review-round
{"evidence_id":"27a8fea8-9191-4bd1-987e-15be5185e863","plan_hash":"0adc4e976ce12f5c9d8bb9ac51e3cb3e0b8a5685bd1379725c32768099a05161","round_number":7,"round_result":{"coverage_attestation":{"adjacent_variant_complete":true,"attestation_digest":"ef7f427facdf845da15f34096db8c4e2260039dc9952238b99d31ae6094af623","cross_lane_interaction_complete":true,"disposition_counts":{"dismissed":0,"emitted_findings":10,"total":10},"evidence_id":"27a8fea8-9191-4bd1-987e-15be5185e863","lanes":[{"candidate_count":1,"lane_id":"requirements_traceability","status":"completed"},{"candidate_count":3,"lane_id":"repository_blast_radius","status":"completed"},{"candidate_count":6,"lane_id":"runtime_invariants","status":"completed"}],"shadow_manifest_status":{"entry_count":5,"manifest_digest":"7f2d4e84222ccf60cbcb70c278ae63565f2192c89a995bba6fc6111f5d545361","status":"valid"},"source_digest":"d02db4baa532171ea29832b9be4b06677483d18a6e573c76635cb6f01ddb510a","version":1},"findings":[{"category":"weak-testability","check_key":"acceptance-observability","description":"`test_stateless_ambient_session.py` requires a row immediately after startup SessionStart and `test_worktrees_e2e.py` extracts the internal ID from SessionStart context, both behaviors §3.1 removes. Neither suite is covered by §5.1 acceptance or its focused command, so the new smoke can pass while known consumers remain broken.","finding_id":"GHDM-R7-F01","fix":"Add one §5.1 acceptance item that runs all four listed E2E consumer suites and requires their SessionStart assumptions to move to first activity, or move those repairs and their exact validation command into §3.1.","location":"P3-P5 / existing E2E consumers of deferred SessionStart","prevention":"For every compatibility Target, map its changed assertion to an acceptance item and include the suite in a runnable validation command.","principle":"Every known consumer whose assumptions change at a behavioral cutover needs an observable acceptance gate.","repairs":[{"items":[{"artifact":"test: `GOBBY_TEST_PROTECT=1 uv run pytest tests/e2e/test_full_workflow.py tests/e2e/test_session_tracking.py tests/e2e/test_stateless_ambient_session.py tests/e2e/test_worktrees_e2e.py -q`","prose":"All four existing CLIEventSimulator SessionStart consumer suites pass after their row and context assumptions move to first activity"}],"kind":"add_acceptance","section_id":"5.1"}],"root_cause":"The Targets inventory recognizes four existing E2E consumers whose row/context assumptions shift, while §5.1 acceptance and its focused command cover only the new smoke and lower-level suites.","section_id":"5.1","severity":"blocking"},{"category":"traceability","causal_finding_id":"GHDM-R5-F15","causal_section_ids":["3.1"],"check_key":"targets-complete","description":"§3.1 requires a new public `SessionResolutionOutcome` and status representation, but `session_lookup.py` is scoped only to `_resolve_uncached_session_id`, `_resolve_session_id`, and `resolve`; the type definition and constructors are outside the reviewed scope.","finding_id":"GHDM-R7-F02","fix":"Replace the three exact `session_lookup.py` Targets with one justified `::*` Target that owns the outcome type and all resolution methods, or move the new type to a new explicitly targeted module.","introduced_in_round":5,"location":"P3 / `SessionResolutionOutcome` ownership","prevention":"Compare every named new class, enum, alias, and factory against exact symbol Targets before expansion validation.","principle":"Every new production symbol and its constructors must be owned by the deliverable's Targets.","root_cause":"The round-5 boundary repair added a new public outcome type in `session_lookup.py` while retaining exact Targets for only the three existing resolve methods.","section_id":"3.1","severity":"blocking"},{"category":"traceability","causal_finding_id":"GHDM-R5-F05","causal_section_ids":["3.1"],"check_key":"targets-complete","description":"`evaluate_blocking_webhooks` must return a new endpoint-bearing `CopiedGateResult` with four statuses, yet `webhook.py` is targeted only at existing functions. The new type and endpoint outcome representation are outside the reviewed scope.","finding_id":"GHDM-R7-F03","fix":"Replace the exact `webhook.py` Targets with one justified `::*` Target, or put `CopiedGateResult` and its endpoint-outcome representation in a new explicitly targeted module and retain exact function Targets.","introduced_in_round":5,"location":"P3 / `CopiedGateResult` ownership","prevention":"When a repair replaces sentinel returns with a typed result, target the type definition, every constructor, exports, and exhaustive consumers.","principle":"A changed function signature and its new exhaustive result type must share explicit source ownership.","root_cause":"The round-5 typed-gate repair targeted the existing webhook functions but omitted the new top-level result and endpoint-outcome symbols they construct.","section_id":"3.1","severity":"blocking"},{"category":"unhandled-edge","causal_finding_id":"GHDM-R6-F06","causal_section_ids":["Locked decisions","4.1","5.1"],"check_key":"bounded-atomic-pending-context-delivery","description":"After the route commits a canonical replay and writes its processed marker, the daemon drain can delete the still-live inbox file before ghook maps or emits the response. A later mapping/write/flush failure then leaves the file absent, falsely acknowledging content Grok never received and allowing deferred ack mutations to commit.","finding_id":"GHDM-R7-F04","fix":"Target and change `_drain_hook_inbox_once_locked` so a provider-ack-pending Grok record makes the processed-marker branch retain or skip the file; only ghook may unlink it after successful mapping and stdout write/flush. Add the direct-POST commit → drain → stdout-failure race to unit and E2E coverage.","introduced_in_round":6,"location":"P4-P5 / ghook inbox acknowledgment crash table","prevention":"Trace direct POST, processed-marker creation, concurrent drain, action mapping, stdout write, flush, unlink, and next-request acknowledgment in one interleaving table.","principle":"Only the provider-emission owner may remove the durable marker whose absence acknowledges delivery.","root_cause":"The round-6 repair changes `_post_envelope` and ghook deletion timing, while `_drain_hook_inbox_once_locked` still unlinks any file with an already-processed marker before calling `_post_envelope`.","section_id":"4.1","severity":"blocking"},{"category":"unhandled-edge","causal_finding_id":"GHDM-R6-F08","causal_section_ids":["4.1"],"check_key":"bounded-atomic-pending-context-delivery","description":"A P2P transaction can hold the session row lock while a legacy writer reads the pre-P2P value under the separate advisory lock; after P2P commits, that legacy UPDATE can overwrite the buffer or replay mutation with its stale snapshot. Joint rollback within the P2P transaction does not prevent this lost update.","finding_id":"GHDM-R7-F05","fix":"Require the supplied transaction to acquire the same `SessionVariableMutation(session_id)` lock before reading session variables, with a documented global order after UUID-sorted message locks, then add a concurrent P2P-versus-legacy `merge_variables` test plus rollback cases.","introduced_in_round":6,"location":"P4 / P2P transaction-aware `_mutate_variables` seam","prevention":"Race each new connection-aware mutation against every legacy writer and verify both acquire one shared serialization lock before reading.","principle":"Every writer of one read-modify-write value must serialize through the same lock domain.","root_cause":"The supplied-connection repair proposes a session-row lock, while legacy `_mutate_variables` callers serialize with `SessionVariableMutation` advisory locking and then use a plain MVCC SELECT.","section_id":"4.1","severity":"blocking"},{"category":"bad-sequencing","causal_finding_id":"GHDM-R6-F03","causal_section_ids":["Collision with compact-summary-fidelity (#20724)","V2"],"check_key":"atomic-leaf-cutover","description":"A crash after the paused replacement epic commits but before its response/readback loses the only root ID. Rerunning preflight creates another epic, leaving #20635 edge attachment and Phase-B plan binding ambiguous even though the old prerequisite edges are still intact.","finding_id":"GHDM-R7-F06","fix":"Create the root with a deterministic plan/evidence provenance label and make preflight query-and-reuse exactly one matching paused epic before creating. Attach/read back #20635's replacement edge while old edges remain, and test crash-after-insert-before-response recovery.","introduced_in_round":6,"location":"Collision cleanup / replacement-root Preflight","prevention":"Inject a crash after every resource-creation commit and before response receipt; require retry to locate exactly one durable resource.","principle":"A crash-resumable create step needs a deterministic durable identity that retries can discover and reuse.","root_cause":"The reordered preflight uses non-idempotent `create_task` and reads the returned ID only after commit, without a provenance label, uniqueness key, or recovery query.","section_id":"Collision with compact-summary-fidelity (#20724)","severity":"blocking"},{"category":"unhandled-edge","causal_finding_id":"GHDM-R6-F07","causal_section_ids":["3.1"],"check_key":"edge-case-coverage","description":"If the catalog advances after approval, choosing 409 as instructed changes a file absent from the sealed Targets while the plan, ledger, manifest, and coverage evidence still name 408. The same environment drift that invalidated 405 can therefore recur as an unreviewed implementation deviation.","finding_id":"GHDM-R7-F07","fix":"Freeze 408 and add a pre-expansion check that returns to review if it is occupied, or define a supported re-seal/re-review operation that updates the Target, acceptance text, ledger, plan hash, and manifest together before any leaf runs. Test that the selected migration is exactly one above the immediately preceding committed version.","introduced_in_round":6,"location":"P3 / next-free migration resolution","prevention":"Resolve numeric migration identity before expansion and test drift between approval, branch checkout, and leaf execution.","principle":"An immutable reviewed plan must name the same concrete changed file that implementation and coverage evidence will own.","root_cause":"The round-6 renumber repair combines an exact `408_...sql` Target with permission to choose a different filename after approval, without a supported re-seal or re-review transition.","section_id":"3.1","severity":"blocking"},{"category":"unhandled-edge","causal_finding_id":"GHDM-R6-F12","causal_section_ids":["3.1","4.1"],"check_key":"edge-case-coverage","description":"The accepted boundary is `min(aggregate_deadline, outer_deadline - reserve)`, while §3.1 says the lesser of aggregate and outer deadlines minus the reserve, implementable as `min(aggregate_deadline, outer_deadline) - reserve`. With a long outer budget, that shortens the ratified 15-second aggregate window to 14 seconds.","finding_id":"GHDM-R7-F08","fix":"State `gate_deadline = min(aggregate_deadline, outer_deadline - COPIED_GATE_LIVE_RESERVE_SECONDS)` verbatim in the body and acceptance. Add controlled-clock cases below, at, and above aggregate-plus-reserve so a long outer budget retains the full 15 seconds.","introduced_in_round":6,"location":"P3 / copied blocking-gate deadline","prevention":"Write deadline formulas algebraically and test values that distinguish every plausible subtraction order.","principle":"Nested deadline arithmetic must state one unambiguous formula that preserves each ratified budget.","root_cause":"The round-6 repair prose moved the reserve outside the `min` expression even though its accepted formula subtracts the reserve only from the outer route deadline.","section_id":"3.1","severity":"blocking"},{"category":"unhandled-edge","causal_finding_id":"GHDM-R6-F10","causal_section_ids":["4.1"],"check_key":"bounded-atomic-pending-context-delivery","description":"A real Stop gate can commit only turn-context; after emission uncertainty, the next ungated PreToolUse retranslates that record into a deny solely to deliver turn-context. The reverse path can block an otherwise allowing Stop. Both contradict Locked decision 4.","finding_id":"GHDM-R7-F09","fix":"Persist component classes in replay eligibility. Briefing may create its one delivery gate; turn-context-only replay may piggyback only on a current real deny/block and otherwise remains pending or follows the explicit drop policy. Add Stop(real gate + turn only) → ungated PreToolUse and the reverse test.","introduced_in_round":6,"location":"P4 / cross-class canonical replay eligibility","prevention":"Cross every origin and receiving hook class with briefing-only, turn-context-only, mixed, real-gated, and ungated content.","principle":"Replay may change wire encoding while preserving the originating content class's gating policy.","root_cause":"The round-6 refit repair treats every PreToolUse/Stop pair as compatible based only on wire shape and receiving budget, without checking whether the replay contains briefing or turn-context.","section_id":"4.1","severity":"blocking"},{"category":"unhandled-edge","causal_finding_id":"GHDM-R1-F10","causal_section_ids":["1.1","4.1"],"check_key":"pending-message-stop-consumer","description":"`get_undelivered_messages` returns an unbounded set, while briefing rejects a 17th distinct key as a bug. Per-message briefing enqueue can roll back or strand a cap-plus-one batch; classifying P2P as turn-context instead conflicts with the promise that it rides a briefing denial because the briefing-only flush arm explicitly leaves turn-context untouched.","finding_id":"GHDM-R7-F10","fix":"Choose P2P's class and define deterministic bounded selection. If it is briefing-class, enqueue only available slots in `sent_at`/UUID order, commit that batch, leave excess message rows untouched, and drain later batches without treating capacity as corruption. Add cap, cap-plus-one, rollback, concurrency, and eventual-delivery tests.","introduced_in_round":1,"location":"P4 / pending P2P component classification and capacity","prevention":"Test zero, cap, cap-plus-one, oversized, concurrent, and transaction-failure batches for every durable producer.","principle":"A durable unbounded producer needs an explicit bounded batching and backpressure policy at the finite delivery buffer.","root_cause":"The round-1 P2P repair specifies message-derived component IDs and safe gates without assigning P2P to a buffer class or reconciling unbounded selection with the briefing cap.","section_id":"4.1","severity":"blocking"}],"reviewer_session":"#11086","round":7,"round_number":7,"verdict":"needs_review"},"session_id":"ec1cd52b-590d-4658-9bbd-6a37a5ddb086"}
```

**Round 8** `kind: verification`

- reviewer_run: d770e37c-c36e-43ce-bc71-cfe682a1cffc
- reviewer_session: #11088
- verdict: needs_review
- findings:
- GHDM-R8-F01/blocking/unhandled-edge — Preflight-to-Phase-B restart recovery: provenance recovery accepts only an unbound paused root, but Phase B binds the root at `create_plan` before coverage generation; a restart after the plan-row commit rediscovers a bound root that Preflight rejects. Fix: phase-aware recovery accepting the unbound paused root or a root bound exclusively to this exact Plan ID, root ref, and approved hash, with specified resume points after plan-row commit, response loss, and coverage-generation failure.
- GHDM-R8-F02/blocking/unhandled-edge — Frozen migration 408 Phase-B gate: no executable read-only pre-expansion observation proves latest=407 and 408_count=0; acceptance 3.1.27 runs only post-implementation. Fix: named read-only coordinator preflight enumerating committed migration prefixes with recorded evidence and a mismatch-returns-to-review transition.
- GHDM-R8-F03/blocking/unhandled-edge — P2P bounded selection vs global lock order: reading briefing capacity before the session advisory lock uses stale capacity; taking the advisory lock first creates the reverse lock-order edge. Fix: two-stage algorithm — UUID-lock a bounded candidate set, acquire SessionVariableMutation(session_id), recompute live capacity inside _mutate_variables, enqueue only the earliest fitting prefix; add the occupancy-change race test.
- GHDM-R8-F04/blocking/unhandled-edge — Finite briefing arrival under P2P saturation: cap-full P2P occupancy makes a later non-evictable finite briefing the seventeenth key with only an error path and no retry transition. Accepted direction: reserve explicit finite-source capacity before P2P selection; test cap-full queued and replay-owned P2P followed by compact/clear generations proving finite delivery and later P2P draining.
- GHDM-R8-F05/blocking/unhandled-edge — Reverse concurrent first-activity interleaving (latent, round 2): when PreToolUse creates the row and commits the startup briefing, the losing UPS lacks _session_just_materialized and its first-shot aggregate is misclassified as turn-context, bypassing briefing one-shot dedupe. Accepted direction: class lifecycle one-shots at their producers with stable briefing component IDs, leaving aggregate stash for genuine turn-context; add the reverse-interleaving acceptance proving one briefing identity, zero turn-context duplicate, one ack mutation.
- resolution_notes: All five findings accepted (coordinator vote, unattended per user directive; no product-scope forks — F04 and F05 fix directions chosen by coordinator as least-mechanism refinements consistent with ratified decisions and R7-F09 component-class realignment). Four findings are round-7 fixer-induced (F01←R7-F06, F02←R7-F07, F03/F04←R7-F10); F05 is a latent round-2 defect (←R2-F05) surfaced by adjacent-variant analysis. Repairs applied by the round-8 author sitting on a coordinator-staged snapshot; deterministic gate (base + expansion) rerun on the merged canonical before round 9.

```json plan-review-round
{"evidence_id":"cb4cd7a7-aeff-4323-a714-7c6311c61704","plan_hash":"cf39f60cdcefc8695f53a43333029ddda17a3a6dd272ef7cc83905530ca6edf1","round_number":8,"round_result":{"coverage_attestation":{"adjacent_variant_complete":true,"attestation_digest":"331519b670b2daa3aba4d052a24bbcc012b9babeede6d813cc3ed46f759b3716","cross_lane_interaction_complete":true,"disposition_counts":{"dismissed":1,"emitted_findings":5,"total":6},"evidence_id":"cb4cd7a7-aeff-4323-a714-7c6311c61704","lanes":[{"candidate_count":2,"lane_id":"requirements_traceability","status":"completed"},{"candidate_count":1,"lane_id":"repository_blast_radius","status":"completed"},{"candidate_count":3,"lane_id":"runtime_invariants","status":"completed"}],"shadow_manifest_status":{"entry_count":5,"manifest_digest":"8857090f845c7ad88d2e3666acc8fc1855acfefab80e469a3ae25b3aa8394bb7","status":"valid"},"source_digest":"f0277fd43d80ad751da57cd7e93fad3113a676b5f665355421f8d4378121c3cf","version":1},"findings":[{"category":"unhandled-edge","causal_finding_id":"GHDM-R7-F06","causal_section_ids":["Collision with compact-summary-fidelity (#20724)"],"check_key":"atomic-leaf-cutover","description":"A restart after `create_plan` commits the plan row can rediscover the provenance-labelled epic only as a bound root, which Preflight rejects. Because coverage generation occurs after the plan-row commit, the coordinator can lose its Phase-B resume point even though retrying `create_plan` with the same root is supported.","finding_id":"GHDM-R8-F01","fix":"Make provenance recovery phase-aware: accept either the expected unbound paused root or a root bound exclusively to this exact Plan ID, root ref, and approved hash after reading back the plan row, sealed ledger, and managed manifest. Specify the resume point after plan-row commit, response loss, or coverage-generation failure; reject foreign or mismatched bindings.","introduced_in_round":7,"location":"Collision cleanup / Preflight-to-Phase-B restart recovery","prevention":"Inject coordinator restarts after each plan-row, managed-manifest, and response boundary and require recovery from durable identifiers at every resulting state.","principle":"A durable recovery identity must remain discoverable after every later durable state transition.","root_cause":"The round-7 provenance repair accepts the labelled root only while it is unbound, while Phase B binds that same root before coverage generation and response receipt; no restart branch consults the plan registry for the now-bound root.","section_id":"Collision with compact-summary-fidelity (#20724)","severity":"blocking"},{"category":"unhandled-edge","causal_finding_id":"GHDM-R7-F07","causal_section_ids":["3.1"],"check_key":"edge-case-coverage","description":"Phase B owns the 408 occupancy check, yet the plan names no command, tool call, catalog read, or output artifact that proves `latest=407` and `408_count=0` before expansion. Acceptance 3.1.27 runs only after section 3.1 implements migration 408, so it cannot serve as the approval-to-expansion guard.","finding_id":"GHDM-R8-F02","fix":"Add a read-only coordinator preflight operation that enumerates committed migration prefixes and records `latest=407` and `408_count=0` before `create_plan` or expansion, with any mismatch returning the plan to review. Keep `runner_tests.rs` as the later proof that implemented 408 is embedded immediately after 407.","introduced_in_round":7,"location":"P3 / frozen migration 408 Phase-B gate","prevention":"For every frozen migration identity, name the exact pre-expansion catalog/filesystem query, expected values, recorded evidence, and mismatch transition.","principle":"A pre-expansion drift guard needs an executable read-only observation and recorded result before any leaf runs.","root_cause":"The round-7 freeze repair states the desired 407/408 predicate and adds a post-implementation runner test, while leaving the coordinator's pre-expansion observation unspecified.","section_id":"3.1","severity":"blocking"},{"category":"unhandled-edge","causal_finding_id":"GHDM-R7-F10","causal_section_ids":["4.1"],"check_key":"bounded-atomic-pending-context-delivery","description":"Available briefing slots live in queued, claimed, and replay-owned session variables. Reading them before the session advisory lock can select from stale capacity; taking the advisory lock first and then message-row locks creates the reverse edge the plan declares absent.","finding_id":"GHDM-R8-F03","fix":"Specify a two-stage algorithm: select and UUID-lock at most the bounded maximum candidate set first, acquire `SessionVariableMutation(session_id)`, recompute live capacity inside `_mutate_variables`, and enqueue only the earliest fitting prefix while leaving excess rows untouched. Add a race where occupancy changes between candidate selection and session-lock acquisition.","introduced_in_round":7,"location":"P4 / P2P bounded selection and global lock order","prevention":"For every computed batch, list each read, candidate lock, serialization lock, recheck, mutation, and commit in one total order, then race capacity changes at every boundary.","principle":"Every capacity decision must be made under a lock order that protects its inputs without creating a reverse edge.","root_cause":"The round-7 bounded-batching repair says to compute current session-buffer capacity before choosing message rows, while the lock-order repair requires UUID-sorted message-row locks before the session advisory lock; the plan supplies no two-stage selection that satisfies both.","section_id":"4.1","severity":"blocking"},{"category":"unhandled-edge","causal_finding_id":"GHDM-R7-F10","causal_section_ids":["4.1"],"check_key":"bounded-atomic-pending-context-delivery","description":"A cap-full queued, claimed, or replay-owned P2P batch can be followed by compact, clear, wiki, profile, persona, or startup briefing. That finite source is non-evictable yet becomes the seventeenth component, and the plan defines only an error—not how its durable source remains pending and is retried after acknowledgment frees a slot.","finding_id":"GHDM-R8-F04","fix":"Define finite-source arrival under full P2P occupancy as durable backpressure: preserve its source/staging marker and name the retry trigger after acknowledgment, or reserve explicit finite-source capacity before P2P selection. Test cap-full queued and replay-owned P2P followed by compact and clear generations, then prove finite delivery and later P2P draining without corruption.","introduced_in_round":7,"location":"P4 / finite briefing arrival under P2P saturation","prevention":"Fill every buffer state with each producer class, then inject every other class and prove preservation, retry ownership, and eventual delivery.","principle":"A bounded shared buffer must define priority, durable backpressure, and eventual progress for every producer-class ordering.","root_cause":"The round-7 P2P repair permits P2P to occupy every currently available briefing slot, while finite briefing producers treat a seventeenth distinct key as an error and have no specified retry transition after capacity frees.","section_id":"4.1","severity":"blocking"},{"category":"unhandled-edge","causal_finding_id":"GHDM-R2-F05","causal_section_ids":["3.1","4.1"],"check_key":"first-activity-activation-and-briefing-barrier","description":"If PreToolUse creates the row and commits the startup briefing while a concurrent UPS loses lookup creation, that UPS lacks `_session_just_materialized`. Its later first-shot agent/wiki/profile aggregate is therefore classified as turn-context, bypassing briefing one-shot dedupe and potentially delivering or dropping a duplicate under the turn-context policy.","finding_id":"GHDM-R8-F05","fix":"Class lifecycle one-shots at their producers with stable briefing component IDs, leaving aggregate stash for genuine turn-context, or propagate a durable first-activation-cycle classification to every concurrent racer. Add the reverse interleaving where PreToolUse creates/commits first and UPS stashes both before and after acknowledgment, proving one briefing identity, zero turn-context duplicate, and one ack mutation.","introduced_in_round":2,"location":"P3-P5 / reverse concurrent first-activity interleaving","prevention":"Enumerate both row-creation winners and stash timing before and after acknowledgment, asserting component class, stable identity, and exactly-once ack mutation.","principle":"Lifecycle one-shots must retain briefing class and one-shot identity for every concurrent first-hook winner orientation.","root_cause":"The round-2 interleaving repair gives `_session_just_materialized` only to the row-creation winner and defaults every unflagged stash to turn-context; its acceptance covers UPS creating first, not PreToolUse creating first.","section_id":"3.1","severity":"blocking"}],"reviewer_session":"#11088","round":8,"round_number":8,"verdict":"needs_review"},"session_id":"ec1cd52b-590d-4658-9bbd-6a37a5ddb086"}
```

**Round 9** `kind: verification`

- reviewer_run: 5e7a2c72-fdaf-48e7-a41a-8f69c5222f35
- reviewer_session: #11090
- verdict: needs_review
- findings:
- GHDM-R9-F01/blocking: companion-ledger root_task_ref sentinel never sealed by any coordinator step, making phase-aware recovery's first bound readback unrealizable
- GHDM-R9-F02/blocking: Preflight queries only the current-hash provenance label, so a superseded old-hash unbound root is invisible and a duplicate root can be created
- GHDM-R9-F03/blocking: finite-source reservation counts 7 classes as 7 max components, but overlapping compact/clear generations produce a seventeenth key with only an error defined
- GHDM-R9-F04/blocking: observe_frozen_migration_408 runs at Phase B after Preflight/Phase-A mutations, so its return-to-review branch is not side-effect-free
- GHDM-R9-F05/blocking: activation epoch used for dedupe/watermark rollover has no durable field, initialization, transition owner, or CAS semantics
- GHDM-R9-F06/blocking: P2P acknowledgment path has no two-stage lock protocol; reading replay-owned IDs under the session lock creates the forbidden reverse edge
- GHDM-R9-F07/blocking: 4.1 Target annotation still names ACPHookAdapter.handle_native as the commit boundary, contradicting the ClaimHandle ownership move
- resolution_notes: Coordinator (unattended, Josh asleep) accepted all 7 findings with zero user forks; none reverses a locked ratification. Direction for F03: bounded-generation branch — at most one compact and one clear component inside the 16-slot buffer, newer generations durably staged for acknowledgment-triggered admission; no retry/backpressure machinery, composing with the R8-F04 reservation branch. Repairs applied in an author sitting on a coordinator-staged snapshot; changelog byte-frozen during repair; merged and gated before round 10 (the review cap).

```json plan-review-round
{"evidence_id":"5a1169a1-6682-4996-830c-2465ee06050a","plan_hash":"af96f705c81e7c36d0ad9285ee460bac7442d5a5b29605ff254b46a607da8187","round_number":9,"round_result":{"coverage_attestation":{"adjacent_variant_complete":true,"attestation_digest":"3bcdbdd7090e39cfd23a109a6df9a50bbfe16c1884eeb33e811376702cde4bc8","cross_lane_interaction_complete":true,"disposition_counts":{"dismissed":2,"emitted_findings":7,"total":9},"evidence_id":"5a1169a1-6682-4996-830c-2465ee06050a","lanes":[{"candidate_count":2,"lane_id":"requirements_traceability","status":"completed"},{"candidate_count":4,"lane_id":"repository_blast_radius","status":"completed"},{"candidate_count":3,"lane_id":"runtime_invariants","status":"completed"}],"shadow_manifest_status":{"entry_count":5,"manifest_digest":"ae27d747bffa4f92d14828df0c74c6c3016d08827035e91e11d7fa7659b55d57","status":"valid"},"source_digest":"107714f63d1acdcbc3809f651e412c0a17a591dcca3b1f1afc774711d720c56c","version":1},"findings":[{"category":"unhandled-edge","causal_finding_id":"GHDM-R8-F01","causal_section_ids":["Collision with compact-summary-fidelity (#20724)"],"check_key":"atomic-leaf-cutover","description":"Phase-aware recovery requires the companion ledger root ref to equal the recovered epic, while the reviewed ledger still contains `root_task_ref: \"SEALED-BY-COORDINATOR\"`. Preflight and Phase B never replace or verify that sentinel, and `create_plan` generates the managed manifest without sealing the companion ledger, so the first bound readback is unrealizable.","finding_id":"GHDM-R9-F01","fix":"After Preflight resolves exactly one paused root, add an idempotent coordinator step that replaces only the ledger sentinel with that normalized epic ref, records and verifies the resulting ledger hash and full identity/acceptance inventory, and requires the exact sealed ledger before `create_plan`. Retries must accept only that already-sealed root and reject sentinel or foreign values.","introduced_in_round":8,"location":"Collision cleanup / Preflight companion-ledger sealing","prevention":"For every recovery tuple, list the operation that writes each field, its retry behavior, and the readback that proves the tuple before binding.","principle":"Every recovery identity required at readback must be durably constructed before the operation that relies on it.","root_cause":"The round-8 recovery repair added exact root-ref verification for the companion ledger without adding the coordinator operation that replaces its SEALED-BY-COORDINATOR sentinel.","section_id":"Collision with compact-summary-fidelity (#20724)","severity":"blocking"},{"category":"unhandled-edge","causal_finding_id":"GHDM-R8-F01","causal_section_ids":["Collision with compact-summary-fidelity (#20724)","V2"],"check_key":"atomic-leaf-cutover","description":"Preflight searches only `plan-root:grok-hook-deferred-materialization:<current-hash>`. An older unbound root carrying the same Plan-ID prefix with an old hash can therefore survive a return-to-review, produce zero current-label matches, and allow a second root when no plan row exists; the promised superseded-root rejection is absent.","finding_id":"GHDM-R9-F02","fix":"Query all provenance labels with the Plan-ID prefix plus every Plan-ID/root registry binding before exact-current-label recovery. Fail closed or explicitly retire a superseded root before any create, and add old-hash unbound, bound, and terminal recovery cases.","introduced_in_round":8,"location":"Collision cleanup / superseded-root recovery","prevention":"Test recovery with current-hash and old-hash roots in unbound, bound, terminal, duplicate, and response-loss states before allowing a zero-match create branch.","principle":"Provenance recovery must inventory the whole logical identity namespace before treating an exact-label miss as permission to create.","root_cause":"The round-8 phase-aware repair queries only the current approved-hash label, so an older unbound root with the same Plan ID is invisible when no plan row claims it.","section_id":"Collision with compact-summary-fidelity (#20724)","severity":"blocking"},{"category":"unhandled-edge","causal_finding_id":"GHDM-R8-F04","causal_section_ids":["Locked decisions","4.1"],"check_key":"bounded-atomic-pending-context-delivery","description":"Nine P2P components plus the five lifecycle one-shots, one compact, and one clear fill all 16 slots. Section 4.1 also requires a second compact or clear generation racing an older queued or claimed generation to receive a distinct component ID; that next finite arrival becomes the seventeenth key, where the plan specifies only an error and disclaims retry/backpressure machinery.","finding_id":"GHDM-R9-F03","fix":"Keep at most one compact and one clear component inside the 16-slot buffer while newer generations remain durably staged for acknowledgment-triggered admission, or define explicit durable finite-source overflow/backpressure with ordered retry. Add the exact 9 P2P + 5 lifecycle + compact + clear saturation case followed by second compact and clear arrivals for queued, claimed, and replay-owned states.","introduced_in_round":8,"location":"P4 / finite briefing reservation under repeated compact or clear generations","prevention":"Compute buffer bounds from producer cardinality and race every multi-generation producer at queued, claimed, replay-owned, and saturated states.","principle":"A non-evictable bounded buffer must reserve capacity by maximum reachable concurrent components, including multiple generations from one producer class.","root_cause":"The round-8 saturation repair equates seven producer classes with seven maximum outstanding components even though compact and clear explicitly create distinct keys for overlapping generations.","section_id":"4.1","severity":"blocking"},{"category":"unhandled-edge","causal_finding_id":"GHDM-R8-F02","causal_section_ids":["Collision with compact-summary-fidelity (#20724)","V2"],"check_key":"edge-case-coverage","description":"`observe_frozen_migration_408` is executable and currently returns `latest=407 408_count=0`, yet its mismatch branch is not side-effect-free: it runs after Preflight may create a root and edge and after Phase A rewrites artifacts, migrates #20635, removes dependencies, and closes old tasks. A mismatch therefore returns to review with material coordinator state already changed.","finding_id":"GHDM-R9-F04","fix":"Run and durably record `observe_frozen_migration_408` before root creation, dependency attachment, Phase-A artifact edits, or task mutation; bind the evidence to the approved HEAD and revalidate that identity immediately before `create_plan`/expansion. The mismatch branch must leave task, plan, ledger, manifest, and dependency state unchanged.","introduced_in_round":8,"location":"Collision cleanup / observe_frozen_migration_408 ordering","prevention":"Place every pre-expansion drift observation before the first coordinator mutation and test its mismatch branch against a zero-diff task, plan, and dependency snapshot.","principle":"A read-only drift guard that returns work to review must run before the coordinator mutations it is supposed to prevent.","root_cause":"The round-8 migration observation was inserted at Phase B even though Preflight and Phase A already mutate the root/dependency graph, registered plan artifacts, #20635, and obsolete leaves.","section_id":"Collision with compact-summary-fidelity (#20724)","severity":"blocking"},{"category":"unhandled-edge","causal_finding_id":"GHDM-R5-F14","causal_section_ids":["3.1","4.1"],"check_key":"edge-case-coverage","description":"Sections 3.1 and 4.1 key lifecycle IDs, webhook/pipeline idempotency, committed-one-shot replacement, and watermark pruning to an `activation epoch`, but never define its durable field, initialization, rollover events, or mutation owner. Implementers cannot determine when an old committed identity stops suppressing a legitimate new lifecycle briefing or how racers agree on the same epoch.","finding_id":"GHDM-R9-F05","fix":"Define one reserved durable activation-epoch field and its atomic owner. Specify initialization for fresh deferred rows, the exact resume/clear/re-materialization transitions that advance it, CAS behavior under concurrent hooks, and treatment of queued, claimed, replay-owned, committed-one-shot, and compact/clear watermark state. Add restart and rollover races.","introduced_in_round":5,"location":"P3–P4 / activation-epoch lifecycle","prevention":"For each durable epoch, specify initialization, storage, owner, CAS transition, restart behavior, and migration or clearing of queued, claimed, replay, committed, and watermark state.","principle":"A durable dedupe epoch needs a defined source, atomic transition owner, and rollover treatment for every state it namespaces.","root_cause":"The round-5 tombstone-bounding repair says sets and watermarks roll over by activation epoch without defining the epoch field or any transition that changes it.","section_id":"3.1","severity":"blocking"},{"category":"unhandled-edge","causal_finding_id":"GHDM-R8-F03","causal_section_ids":["4.1"],"check_key":"bounded-atomic-pending-context-delivery","description":"The plan declares the global order UUID message-row locks → `SessionVariableMutation(session_id)`, but only enqueue explains how to obtain row IDs before the session lock. Acknowledgment must read P2P IDs from canonical replay stored in session variables before calling `mark_delivered_batch`; taking the advisory lock for that read creates the forbidden reverse edge, while an unprotected peek without replay-token revalidation can acknowledge stale ownership.","finding_id":"GHDM-R9-F06","fix":"Specify acknowledgment as a two-stage protocol: peek the replay token and P2P IDs, open a no-initial-lock immediate transaction, UUID-lock and eligibility-check those rows, acquire `SessionVariableMutation` through connection-aware `_mutate_variables`, revalidate the exact replay token/component ownership, then mark delivered, execute ack mutations, and clear or advance replay atomically. Add acknowledgment-versus-enqueue and acknowledgment-versus-legacy-merge races.","introduced_in_round":8,"location":"P4 / P2P acknowledgment lock order","prevention":"Trace enqueue and acknowledgment separately through candidate discovery, UUID row locks, session advisory lock, token recheck, mutation, rollback, and commit; race both against legacy session-only writers.","principle":"Every transaction that touches two lock domains needs a complete discovery, lock, recheck, mutation, and commit order.","root_cause":"The round-8 two-stage repair specifies message-row-first ordering for enqueue while acknowledgment must discover message IDs from replay stored behind the session lock and receives no equivalent two-stage protocol.","section_id":"4.1","severity":"blocking"},{"category":"traceability","causal_finding_id":"GHDM-R5-F10","causal_section_ids":["4.1"],"check_key":"targets-complete","description":"The 4.1 Target says `ACPHookAdapter.handle_native` is the commit boundary where claimed components commit after translation. The same section later requires that executor workers never commit and that only the async route commits after an in-budget await. Implementing the Target annotation literally reintroduces the late-worker timeout race.","finding_id":"GHDM-R9-F07","fix":"Rewrite the Target annotation and API contract so `handle_native` only translates and returns an opaque claim token, while `execute_hook`/the `_run_adapter_hook` caller owns commit-or-release after the immutable deadline check. Specify a typed worker result so constructors and tests exhaustively distinguish ordinary payloads from payload-plus-claim-token.","introduced_in_round":5,"location":"P4 / ACPHookAdapter.handle_native Target annotation","prevention":"After moving an ownership or commit boundary, sweep Targets, API descriptions, acceptance items, and timeout tests for the old owner.","principle":"Semantically annotated Targets must agree with the deliverable's authoritative runtime ownership and timeout boundary.","root_cause":"The round-5 ClaimHandle repair moved commit out of the executor worker but left the earlier Target annotation naming `handle_native` as the commit boundary.","section_id":"4.1","severity":"blocking"}],"reviewer_session":"#11090","round":9,"round_number":9,"verdict":"needs_review"},"session_id":"ec1cd52b-590d-4658-9bbd-6a37a5ddb086"}
```

**Round 10** `kind: verification`

- reviewer_run: 93a78e64-c152-4ca2-892e-f3243ce784c1
- reviewer_session: #11099
- verdict: needs_review
- findings:
- GHDM-R10-F01/blocking/§4.1 flush-arm paragraph still executes acknowledgment mutations at route commit, contradicting Locked decision 4 ghook inbox-deletion ownership (fixer-induced, r6)
- GHDM-R10-F02/blocking/direct AgyAdapter.handle_native consumer tests/adapters/test_adapters_agy.py missing from 4.1 Targets after the r9 typed-result repair (fixer-induced, r9)
- GHDM-R10-F03/blocking/superseded-root Preflight label-prefix inventory has no completeness-preserving read path in current task MCP surfaces (fixer-induced, r9)
- GHDM-R10-F04/blocking/observe_frozen_migration_408 durable observation record has no named persistent owner, key, schema, or authorized read/write interface (fixer-induced, r9)
- GHDM-R10-F05/blocking/activation-epoch pending-drain fences omit P2P, turn-context, and staged-memory producers; final-acknowledgment epoch advance during _complete_response unhandled (fixer-induced, r9)
- GHDM-R10-F06/blocking/cross-class concurrent live claims (PreToolUse vs real Stop/SubagentStop) lack serialization, overwrite rules, and losing-claim recovery (r4 lineage)
- GHDM-R10-F07/blocking/pre-commit claim left pending on retryable persistence/reference/budget failure, denying immediate retry until route timeout plus lease expiry (r5 lineage)
- GHDM-R10-F08/blocking/pre-deploy epochless session rows lack a deterministic _activation_epoch bootstrap and first-transition rule (fixer-induced, r9)
- votes: all 8 accepted (coordinator-judged, unattended; zero declined)
- resolution_notes: Review cap (10 rounds) reached without approval; no further adversary rounds. Disposition: F02 typed repairs via apply_plan_review_repairs; F01/F05/F06/F07/F08 prose fixes hand-applied by the coordinator after finalization; F03/F04 accepted but their fixes require user decisions (label-prefix task-inventory read API; durable owner for the 408 observation record) and are deferred to the human-handoff entry. Base validation rerun after repairs.

```json plan-review-round
{"evidence_id":"63ee4af3-27a1-42da-8b73-335ad1c00240","plan_hash":"34db9aa832135166003472eb36e5916c8ab5f22d51dd021b7e33f6019097dde0","round_number":10,"round_result":{"coverage_attestation":{"adjacent_variant_complete":true,"attestation_digest":"f6f7883362c00d8d543b23e763215c9a6bca6ad9dc6f683bea49b1d5db7dd2ed","cross_lane_interaction_complete":true,"disposition_counts":{"dismissed":0,"emitted_findings":8,"total":8},"evidence_id":"63ee4af3-27a1-42da-8b73-335ad1c00240","lanes":[{"candidate_count":1,"lane_id":"requirements_traceability","status":"completed"},{"candidate_count":3,"lane_id":"repository_blast_radius","status":"completed"},{"candidate_count":4,"lane_id":"runtime_invariants","status":"completed"}],"shadow_manifest_status":{"entry_count":5,"manifest_digest":"3cc27f47fbf5632b39e18f6f85e3f793cecac420c23d32bb25b80daf2f777ccd","status":"valid"},"source_digest":"65170a0f2faf020539a09d05b5ec247170fd09f898cb7ecaec169d2ed41b48be","version":1},"findings":[{"category":"unhandled-edge","causal_finding_id":"GHDM-R6-F06","causal_section_ids":["Locked decisions","4.1","5.1"],"check_key":"bounded-atomic-pending-context-delivery","description":"Section 4.1 says claimed components and their acknowledgment mutations execute at route commit. The same section, Locked decision 4, 4.1.2/4.1.11/4.1.22, and 5.1.6 require route commit to persist canonical replay while delivered markers stay false until ghook maps, writes, flushes, and deletes the inbox envelope. An implementer cannot satisfy both ownership points.","finding_id":"GHDM-R10-F01","fix":"Rewrite the final flush-arm paragraph so route commit removes claimed components from selectable state and persists their deferred mutations in canonical replay; only the later inbox-absence acknowledgment transaction executes those mutations. Keep the never-at-claim and never-at-stash guarantees.","introduced_in_round":6,"location":"P4 / §4.1 final flush-arm mutation paragraph","prevention":"After moving a commit boundary, compare every state-transition verb across Locked decisions, body prose, acceptance, smoke, and V2.","principle":"Producer delivery state may advance only after the provider-visible action has been emitted and durably acknowledged.","root_cause":"The round-6 ghook-acknowledgment repair moved delivery ownership after route commit but left the older flush-arm sentence executing acknowledgment mutations at commit.","section_id":"4.1","severity":"blocking"},{"category":"traceability","causal_finding_id":"GHDM-R9-F07","causal_section_ids":["4.1","5.1","V2"],"check_key":"targets-complete","description":"`tests/adapters/test_adapters_agy.py` directly awaits `AgyAdapter.handle_native` and indexes the current dict result. Section 4.1 changes the inherited result to `PayloadResult | ClaimedPayloadResult`, but the file is absent from Targets and acceptance, so the planned implementation leaves a known consumer failing.","finding_id":"GHDM-R10-F02","fix":"Add the AGY test file to 4.1 Targets and require its direct call to exhaustively unwrap `PayloadResult` while preserving the native AGY payload contract.","introduced_in_round":9,"location":"P4 / §4.1 ACPHookAdapter.handle_native consumer inventory","prevention":"Run exact and literal direct-call sweeps for every changed return type, including inherited adapter test consumers, then add each hit to Targets or record why it is shape-neutral.","principle":"Every direct consumer of a changed return-shape contract must be targeted and validated.","repairs":[{"entries":["`tests/adapters/test_adapters_agy.py::*` — scope-reason: direct AgyAdapter.handle_native consumer must adopt the typed AdapterWorkerResult contract"],"kind":"add_targets","section_id":"4.1"},{"items":[{"artifact":"test: `tests/adapters/test_adapters_agy.py`","prose":"Direct AGY handle_native consumer exhaustively unwraps PayloadResult while preserving the native payload contract"}],"kind":"add_acceptance","section_id":"4.1"}],"root_cause":"The round-9 translation-only typed-result repair swept the async route and named adapter tests but omitted the direct AGY inherited handle_native consumer.","section_id":"4.1","severity":"blocking"},{"category":"unhandled-edge","causal_finding_id":"GHDM-R9-F02","causal_section_ids":["Collision with compact-summary-fidelity (#20724)","V2"],"check_key":"atomic-leaf-cutover","description":"Preflight requires every task state carrying `plan-root:grok-hook-deferred-materialization:*` before the zero-match create branch. Current `gobby-tasks:list_tasks` supports exact label presence only, exposes no completeness-preserving offset/total contract, and its discovery payload omits labels; `search_tasks` does not search labels. The plan-registry side is enumerable, while the task-label namespace side is not, so the repaired old-hash check is not executable through authorized tools.","finding_id":"GHDM-R10-F03","fix":"Name a current completeness-preserving read path. If none exists, land a narrowly scoped prerequisite task API with label-prefix filtering, stable pagination, total count, and task refs before this plan can finalize; then specify its exact Preflight calls and pair them with active/archived plan-registry enumeration.","introduced_in_round":9,"location":"Collision cleanup / superseded-root namespace Preflight","prevention":"Before ratifying a recovery query, verify its exact filter, pagination, total-count, and returned identity fields against the installed read API.","principle":"A fail-closed recovery inventory must name an executable read path that proves namespace completeness.","root_cause":"The round-9 superseded-root repair requires a label-prefix inventory that the current task MCP surfaces cannot perform exhaustively.","section_id":"Collision with compact-summary-fidelity (#20724)","severity":"blocking"},{"category":"missing-requirement","causal_finding_id":"GHDM-R9-F04","causal_section_ids":["Collision with compact-summary-fidelity (#20724)","3.1","V2"],"check_key":"requirement-owner","description":"The coordinator must durably record approved HEAD, command, exit status, stdout, and identity digests before root creation and reread that same object at two Phase-B boundaries. No repository path, registry field, key schema, or MCP operation owns it: PlanRecord and PlanReviewEvidence lack this post-approval checkpoint. The unanswered questions are where it persists, which authorized call writes it, and how a restarted coordinator retrieves the exact record.","finding_id":"GHDM-R10-F04","fix":"Specify one existing durable surface and exact read/write calls, or add a prerequisite storage/tool contract. Define the stable key and full object schema, then add restart tests proving both later observation checks read the same immutable record and every mismatch leaves the named state snapshot unchanged.","introduced_in_round":9,"location":"Collision cleanup / observe_frozen_migration_408 durable evidence","prevention":"For every cross-restart checkpoint, name the table or file, stable key, object schema, writer, reader, and recovery readback before declaring the sequence implementable.","principle":"Restart-dependent evidence needs a named durable owner, key, schema, and authorized read/write interface.","root_cause":"The round-9 ordering repair defines the observation object's contents and comparison rules without assigning it to any persistent surface.","section_id":"Collision with compact-summary-fidelity (#20724)","severity":"blocking"},{"category":"unhandled-edge","causal_finding_id":"GHDM-R9-F05","causal_section_ids":["Constraints","Locked decisions","3.1","4.1","5.1","V2"],"check_key":"edge-case-coverage","description":"While `_activation_epoch.pending` waits for queued, claimed, and replay-owned occupancy to empty, new P2P and turn-context producers can still add old-epoch components. This can starve rollover. The last acknowledgment can also advance the epoch during `_complete_response` after the current hook already produced old-epoch content, with no rule for reclassification or retry.","finding_id":"GHDM-R10-F05","fix":"Define a request-level pending-drain boundary. Permit only old-epoch release, replay, acknowledgment, explicit drop, and lease recovery; fence every new producer including P2P, turn-context, and staged memory. After the final drain CAS, re-evaluate the current hook under the new epoch or return a typed per-class retry outcome, and add continuous-arrival race tests.","introduced_in_round":9,"location":"P3-P5 / activation-epoch pending-drain producer boundary","prevention":"Enumerate every producer against active, pending_drain, and post-advance states, then test continuous arrivals and acknowledgment during response completion.","principle":"Once a namespace rollover is pending, new work must have one explicit epoch owner and cannot replenish the draining namespace indefinitely.","root_cause":"The round-9 activation-epoch repair fences lifecycle, copied external, and compact/clear producers while omitting P2P enqueue, turn-context stash, staged-memory selection, and current-hook behavior when the final acknowledgment advances the epoch.","section_id":"4.1","severity":"blocking"},{"category":"unhandled-edge","causal_finding_id":"GHDM-R4-F09","causal_section_ids":["4.1"],"check_key":"first-activity-activation-and-briefing-barrier","description":"A PreToolUse can hold one live claim while a real-blocking Stop/SubagentStop follows its normal arm and claims different remaining components. Route commit persists one canonical replay record, so two successful claim commits have no specified serialization, overwrite rule, or losing-claim recovery and can strand one claimed set.","finding_id":"GHDM-R10-F06","fix":"Enforce at most one live claim or canonical replay per session across all hook classes. A real Stop/SubagentStop may emit its existing gate while another claim is live, but must not attach pending components. Add concurrent PreToolUse-versus-Stop/SubagentStop tests for both completion orders and every release/takeover branch.","introduced_in_round":4,"location":"P4 / cross-class concurrent live claims","prevention":"Cross every live claim owner with concurrent PreToolUse, Stop, and SubagentStop in both completion orders, including release, timeout, and lease takeover.","principle":"A singular canonical replay owner requires a singular live claim owner across all hook classes.","root_cause":"The live-claim repair defines only a second PreToolUse loser; it explicitly lets a racing Stop follow its normal arm without defining whether that arm may claim remaining components.","section_id":"4.1","severity":"blocking"},{"category":"unhandled-edge","causal_finding_id":"GHDM-R5-F09","causal_section_ids":["Locked decisions","4.1"],"check_key":"edge-case-coverage","description":"Initial persistence/reference/budget failure returns a retryable fallback while leaving the pre-commit claim pending. The immediate retry then sees a live claim and returns the in-flight denial until route timeout plus the 30-second lease expires. Receiving-class retranslation failure should preserve committed replay; the pre-commit path needs release.","finding_id":"GHDM-R10-F07","fix":"CAS-release any newly created claim through `ClaimHandle` before returning a persistence/reference/budget fallback. Preserve an already committed canonical replay unchanged on receiving-class retranslation failure. Add controlled pre-token failures proving no live claim remains and the next eligible hook can select the same components.","introduced_in_round":5,"location":"P4 / oversize persistence and reference-fit failure","prevention":"For every helper failure, state whether ownership is queued, claimed, or replay-owned and assert the immediately following eligible request can make progress.","principle":"A retryable pre-commit failure must release its claim immediately; committed replay failures preserve replay identity.","root_cause":"The oversize repair uses one 'leave pending' rule for two different states: a newly created pre-commit claim and an already committed canonical replay.","section_id":"4.1","severity":"blocking"},{"category":"unhandled-edge","causal_finding_id":"GHDM-R9-F05","causal_section_ids":["Constraints","Locked decisions","3.1","4.1","5.1","V2"],"check_key":"edge-case-coverage","description":"Pre-deploy rows bypass the insert-time epoch seed but can later resume, end, compact, or re-materialize. The plan excludes them from copied SessionStart effects and says activation seeds may heal them, yet it never defines their exact `_activation_epoch` object or whether a first resume initializes epoch 1, advances to 2, or is excluded. Component IDs and watermarks therefore have no deterministic namespace.","finding_id":"GHDM-R10-F08","fix":"Specify one session-lock CAS bootstrap for an epochless existing row, including value, status, transition key, pending state, committed-one-shot set, and watermark/resident indexes. Define whether the triggering ordinary, resume, SESSION_END, PostCompact, or re-materialization event is folded into bootstrap or applied as the next transition, with restart and duplicate-event tests.","introduced_in_round":9,"location":"P3-P5 / epochless pre-deploy row bootstrap","prevention":"Test each legacy row class with every possible first post-deploy lifecycle event and record whether bootstrap absorbs or precedes that transition.","principle":"Every existing durable row entering a new namespace contract needs one deterministic bootstrap and first-transition rule.","root_cause":"The round-9 activation-epoch repair specifies fresh deferred, cutover-deferred, and clear-successor initialization while leaving ordinary pre-deploy rows to unspecified activation seeding.","section_id":"3.1","severity":"blocking"}],"reviewer_session":"#11099","round":10,"round_number":10,"verdict":"needs_review"},"session_id":"ec1cd52b-590d-4658-9bbd-6a37a5ddb086"}
```

**Human handoff** `kind: verification`

- reason: review cap reached — round 10 (final round) returned needs_review with 8 blocking findings; per the cap rule no further adversary rounds were launched.
- verdict_history: rounds 1-10 blocking-finding counts 11, 12, 20, 14, 16, 12, 10, 5, 7, 8; every round needs_review.
- round_10_disposition: all 8 findings accepted (coordinator-judged, unattended; zero declined). F02 typed repairs applied via apply_plan_review_repairs (4.1 Target + acceptance 4.1.36). The coordinator hand-applied the adversary-specified prose fixes for F01 (route-commit persists deferred ack mutations; only the inbox-absence acknowledgment transaction executes them), F05 (request-level pending-drain producer fence including P2P, turn-context, and staged memory, plus final-acknowledgment re-evaluation; acceptance 4.1.37), F06 (at most one live claim or canonical replay per session across hook classes; acceptance 4.1.38), F07 (pre-commit claim CAS-release versus committed-replay preservation; acceptance 4.1.39), and F08 (epochless pre-deploy row bootstrap; Constraints plus acceptance 3.1.34). These hand-applied repairs follow the fixes verbatim in intent but have not been adversarially re-reviewed.
- open_decision GHDM-R10-F03: the superseded-root Preflight label-prefix inventory has no completeness-preserving read path in current task MCP surfaces. Recommended: add a narrowly scoped prerequisite deliverable extending the gobby-tasks read surface with label-prefix filtering, stable pagination, and total count, then specify the exact Preflight calls. Alternative: enumerate historical plan hashes from the V1 changelog fences plus plan-registry bindings, accepting that unrecorded-hash roots stay undetectable.
- open_decision GHDM-R10-F04: the observe_frozen_migration_408 observation record has no durable owner. Recommended: a coordinator-written sidecar file `.gobby/plans/grok-hook-deferred-materialization.observation-408.json` mirroring the companion coverage-ledger pattern (no new APIs), holding the record schema the plan already defines, with restart-readback tests. Alternative: a new plans-registry field or MCP tool (heavier mechanism).
- next_step: resolve both open decisions, then authorize a fresh adversary round to review the hand-applied repairs and the two decision fixes. The M1 manifest remains unwritten and expansion remains blocked until an approved round; no build handoff occurred.
