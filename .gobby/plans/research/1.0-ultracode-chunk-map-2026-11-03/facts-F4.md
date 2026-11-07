# F4 facts: the review-pause protocol (escalation, stop gate, readiness, close gates, live window)

Fact-finder F4 for task #25629 (session gobby#19401), 2026-11-03. This stage was read-only. Every
code claim cites a repo-relative path:line in /Users/josh/Projects/gobby. Skill references read:
gobby `references/tasks/overview.md`, `creation.md`, `closing.md` (2.1.0), `dependencies.md`,
`implementation.md` and `live-work.md` (1.0.1). Each was one page, with `next_cursor` null.

## Short answers

- (a) `escalate_task(task_id, reason)` turns the task `escalated` and releases its claim. Once the
  task is unclaimed, any same-project session can de-escalate it, reopen it or close it. Josh's
  answer becomes durable only through `de_escalate_task(reason)`, which appends
  `De-escalated: <reason>` to the task description, or through attestations that a session records.
  Gobby cannot prove that a decision came from Josh.
- (b) Yes. When the escalated task is the session's only claim, `task_claimed` becomes false and the
  stop gate does not apply. When the session holds other claims, each of them needs its own exit:
  closed, escalated, blocked by an open task, or covered by an active wait.
- (c) An escalated blocker still blocks. Only `closed_at` releases a dependent, for any close
  reason. So the review task must close on Josh's go, and a no-go never closes it. There is one
  exception. If Josh abandons the direction, re-plan the downstream edges first, because a
  `wont_do` close releases them too.
- (d) Yes. Give the review task category `manual`, `planning` or `research`. Then only the general
  policy applies: one approved `implementation` attestation, clean attributed files, and no commit.
- (e) A held session can keep a live window of several hours. It registers
  `wait_for_coordination(background_task_id=..., timeout=3600)`, the timeout wakes it each hour, and
  it registers again. Idle auto-compact keeps the session row, the claims and the waits. It resets
  schema leases and loaded skills. Cron is a poor substitute.

## (a) escalate_task

Arguments. `task_id` and `reason` are both required strings
(src/gobby/mcp_proxy/tools/tasks/_lifecycle_status.py:144-147, schema at :226-243). The call needs a
session context (:161-167). It also needs claim authority over the task (:181). On success it returns
`{}` (:224).

State after a first escalation:
- The storage write sets `claimed_by_session_id = NULL`, `escalated_at = now`,
  `escalation_reason = reason` and `is_escalated = TRUE`. It runs only on an open task that is not
  already escalated (src/gobby/storage/tasks/_transitions.py:431-445). A closed task is refused
  (_lifecycle_status.py:206-210; _transitions.py:448-454).
- The state bucket becomes `escalated`. That bucket outranks a hold, a blocker and a claim
  (crates/gcore/assets/schema/migrations/477_runbook_task_states.sql:42-50;
  src/gobby/tasks/state_semantics.py:79-91).
- Side effects (src/gobby/mcp_proxy/tools/tasks/_escalation_coordinator.py:55-86):
  - The task leaves the prior owner's `claimed_tasks` (:24-52, which calls `release_claimed_task`
    at :43). That merge sets `task_claimed = len(remaining claims) > 0`
    (src/gobby/workflows/task_claim_state.py:252-276, line 270). The edit attribution stays. This
    cleanup is best effort: a failure is logged at debug level only (_escalation_coordinator.py:52).
  - A best-effort WebSocket `task_progress` goes to the parent of an active agent run bound to the
    task. No such run exists for a root session (src/gobby/mcp_proxy/tools/tasks/_notifications.py:42-58).
  - The escalating session gets the session-task link `escalated` (_escalation_coordinator.py:103).
- A second `escalate_task` on a task that is already escalated and claimed by the caller releases
  that claim and keeps the first reason (#23204, _lifecycle_status.py:186-197). If the caller does
  not hold the claim, the call is refused and points to `de_escalate_task` (:198-205).
- No `task_lifecycle_events` row is written for escalation or de-escalation. The only Python writers
  are close (src/gobby/storage/tasks/_close.py:127) and candidate repair
  (src/gobby/storage/tasks/_lifecycle.py:169). The schema has no trigger on that table
  (crates/gcore/assets/schema/baseline.sql:3742-3755, plus a grep of the migrations with zero hits).

Who can de-escalate, resolve or reopen:
- MCP `de_escalate_task(task_id, reason)`. The task creator's session and the delegator's session
  skip the authority check. Every other caller must pass `require_claim_authority`
  (_lifecycle_status.py:289-296). That check passes for anyone when the task is unclaimed
  (src/gobby/mcp_proxy/tools/tasks/_authorization.py:87-136). Escalation always leaves the task
  unclaimed, so in practice any same-project session can de-escalate it. A claimed escalated task
  also admits any unspawned root session whose definition grants `manage_task_claims`, and
  `default` grants it (_authorization.py:75-84; src/gobby/tasks/lifecycle_access.py:96-104;
  src/gobby/install/shared/workflows/agents/default.yaml:9).
  - The call refuses a task that is not escalated (_lifecycle_status.py:298-303).
  - It clears `escalated_at`, `escalation_reason` and `is_escalated`
    (src/gobby/storage/tasks/_de_escalation.py:11-31; src/gobby/storage/tasks/_updates.py:213-218 and :239-244).
  - It keeps whatever claim exists. After an escalation that is none, so the task returns to `open`.
  - The creation reference agrees: "Escalation releases your canonical ownership ... De-escalation
    leaves the task unclaimed; claim it again when ready to resume" (references/tasks/creation.md,
    section Metadata and recovery).
- Operator interfaces for Josh:
  - `gobby tasks de-escalate TASK --reason ...` calls storage directly, with no session and no
    authority check (src/gobby/cli/tasks/crud.py:331-348; src/gobby/cli/tasks/_crud_mutations.py:259-280).
  - The web UI posts `POST /api/tasks/{id}/de-escalate` with `decision_context`, which becomes the
    reason. That call records no session link and broadcasts `task_de_escalated`
    (src/gobby/servers/routes/tasks_lifecycle_routes.py:249-268).
  - Agents are meant to be kept off the mutating CLI, but see the found defect below.
- `reopen_task` also accepts an escalated task. It clears ownership, closure and escalation, and
  appends `[Reopened: reason]` (_lifecycle_status.py:47-117; _transitions.py:379-414). A rule blocks
  it for the caller's own claimed tasks (src/gobby/install/shared/workflows/rules/task-enforcement/block-reopen-task.yaml:9-25).
- `close_task` can close an escalated task directly. "A close whose caller read the escalated row
  ... is a deliberate resolution and clears escalation". It also NULLs `escalation_reason`
  (_close.py:80-96).
- `claim_task` also accepts an escalated task, because the storage claim has no escalation predicate
  (_transitions.py:142-234).

How Josh's answer is recorded and seen later:
- The task row keeps Josh's words only through de-escalation: `De-escalated: <reason>` is appended
  to the description (_de_escalation.py:23-29). The MCP path also links the calling session as
  `de_escalated` (_lifecycle_status.py:323-336). The CLI and HTTP paths record no author.
- An attestation can carry the decision as evidence. Its author must be a registered session with an
  enabled definition (src/gobby/tasks/attestations.py:213-223). Josh's shell has no session, so he
  cannot author one.
- A later session reads all of this through `get_task(brief=false)` (description, state, escalation
  fields), `list_attestations`, `get_task_sessions` (link actions), the close's lifecycle event and
  its `changes_summary`. Open review pauses can be listed with `list_tasks(escalated=true,
  closed=false)` (src/gobby/storage/tasks/_queries.py:146-153).
- Authentication limit: Gobby records the decision but cannot prove that Josh made it. Any unspawned
  root `default` session counts as an operator session
  (src/gobby/workflows/agent_resolver.py:61-63), and any session can de-escalate an unclaimed task.
  The provenance is the review session's transcript, where Josh's prompt appears, plus the note.
- The `needs_human` state is not an alternative. `hold_state` has no writer in storage, the CLI or
  MCP. It appears only in the model and reads (src/gobby/storage/tasks/_models.py:264,299,348;
  src/gobby/mcp_proxy/tools/hub.py:194). The `awaiting-human-review` label projects
  `needs_human` (state_semantics.py:48-54,94-101), but it satisfies neither the stop gate nor
  readiness. So `escalate_task` is the only human-decision exit an agent can reach.

## (b) The stop gate after escalate_task (require-task-close 1.1.0)

The gate condition is `not plan_mode and not is_spawned_agent and not has_durable_stop_wait() and
mode_level >= 1 and task_claimed`
(src/gobby/install/shared/workflows/rules/stop-gates/require-task-close.yaml:10). It has no
stop-attempt cap.

- Path 1, the review task is the only claim. After escalation `task_claimed` is false
  (task_claim_state.py:270), so the condition is false and the turn ends. No wait is needed.
- Path 2, the session holds other claims. `task_claimed` stays true, and the turn can end only when
  `has_durable_stop_wait()` is true. The engine computes that value as "an active agent wait, or an
  active coordination wait, or every claimed task has a durable wait state"
  (src/gobby/workflows/engine/core.py:345-395). A claimed task has a durable wait state when it has
  an open `blocks` blocker or is escalated with a nonblank reason
  (src/gobby/workflows/condition_helpers.py:663-687). `get_task` hydrates `active_blocked_by` for that
  check (src/gobby/storage/tasks/_read.py:44-51; src/gobby/storage/tasks/_blocking.py:29-67).
  - So a second claim, for example the cutover task, keeps the gate armed unless it has its own
    exit. The simplest rule: claim the cutover task only after Josh's go closes the review task.
- Fallback: if the best-effort claim cleanup failed, the escalated task left in `claimed_tasks` still
  counts as a durable wait, because its reason is nonblank (condition_helpers.py:679-687).
- The gate's own list of legal exits (require-task-close.yaml:14-21):
  - close the task;
  - `wait_for_agent`;
  - `wait_for_coordination` on another session's release key, statuses or reply;
  - `wait_for_coordination(background_task_id=<id>, timeout<=3600)` on the session's own provider
    background task;
  - `add_dependency(task_id=<claimed>, depends_on=<blocker>)`;
  - `escalate_task`.
  - Plan mode, `mode_level` 0 and spawned sessions skip the gate. Spawned sessions fall under
    `require-step-completion` instead, and only when they have a `current_step`
    (src/gobby/install/shared/workflows/rules/stop-gates/require-step-completion.yaml:9-15).
- Escalation does not exempt the found-work gates. `block-terminal-validation-failure` and
  `block-unclaimed-found-work` yield only to an active agent wait or an active coordination wait
  (src/gobby/install/shared/workflows/rules/stop-gates/enforce-found-work-ladder.yaml:40-46,63-74).
  So a review session with an unresolved failing validation command cannot end its turn just by
  escalating.
- An in-turn question, such as AskUserQuestion, is legal while the session holds a claim, because
  the turn has not ended. But it is not durable. Memory d7f65147 says "interactive questions and
  approvals remain inside the provider turn until answered, so human input creates no cross-turn
  stop boundary or stop-gate exemption". If the terminal dies, the question is lost and nothing is
  recorded. Escalation survives the death of the session: an escalated task has no claim, and the
  stale-claim sweep skips escalated tasks (src/gobby/storage/tasks/_automation.py:89-148, lines 115-116).

## (c) blocked_by readiness and the cleanest gating

- A `blocks` edge resolves only when `blocker.closed_at IS NOT NULL`
  (src/gobby/storage/tasks/_queries.py:25-50, line 32; _blocking.py:43). Escalation does not set
  `closed_at`, so an escalated blocker still blocks. Its dependents project as `blocked`
  (_queries.py:53-61) and stay off the ready list (_queries.py:408-426).
- De-escalation does not release dependents either. Only a close does, and the close reason does
  not matter: `done`, `moot`, `superseded` and `wont_do` all set `closed_at`. The database allows
  only those four reasons (477_runbook_task_states.sql:34-37). So "no-go" cannot be a close reason,
  and closing a no-go in any form makes the next stage ready.
- Parent-chain trap: the ready list recurses only through parents whose state is `open` or
  `in_progress` (_queries.py:414,422). The blocker check also ignores a blocker that is an ancestor
  of the dependent (_queries.py:33-48). So the cutover task and the next-stage chunks must be
  siblings of the review task, linked by `blocks` edges. They must never be its children.
- Readiness is advisory. `claim_task` has no blocker check (decisions.md:111; the storage claim SQL
  at _transitions.py:162-188 has no blocker predicate). Sessions keep the order because they pick
  from `list_ready_tasks` or `suggest_next_task`.
- Recommended gating:
  - The review task closes `done` when Josh says go. The decision goes into the de-escalation note
    and into an approved `implementation` attestation.
  - On a no-go the review task stays open, with the fixes as its blockers. It never closes on a
    no-go. The one exception is Josh abandoning the direction, and then the downstream edges are
    re-planned before a `wont_do` close.
  - Do not use "stays escalated until Josh resolves it" as the gate. Downstream would stay blocked
    after Josh's go until some session closes the task anyway, and a CLI de-escalation alone
    releases nothing.

## (d) Close gates for a non-coding review task

- `default` selects three closure rule tags: `task-closure-general`, `task-closure-coding` and
  `task-security`. It excludes `unassigned-session-no-task-close`, so even a spawned `default`
  session may close (default.yaml:119-130).
- The coding policy applies only to categories `code`, `config`, `docs`, `refactor` and `test`
  (src/gobby/install/shared/workflows/rules/task-closure/coding.yaml:10,24,39,58). Those categories
  need TDD attestations, a linked commit, candidate references and `integration_ancestry`
  (coding.yaml:5-65). The attestation shapes apply only to those categories as well (:76-81,120-123).
  A `docs`-category review task would therefore need all of that.
- Categories outside that list are `manual`, `planning` and `research`
  (src/gobby/mcp_proxy/tools/tasks/_crud.py:388-392). For them only the general policy applies
  (src/gobby/install/shared/workflows/rules/task-closure/general.yaml):
  - a close as `done` needs an approved `implementation` attestation (:30-43);
  - attributed files must be clean (:5-12);
  - children must be closed (:45-52);
  - a no-work close needs an approved `disposition` attestation whose `evidence.reason` matches the
    close reason, and `superseded` also needs a reference (:54-75).
- Security (src/gobby/install/shared/workflows/rules/task-closure/security.yaml:5-15,25-50): a close
  as `done` needs `project_policy_available`, and that holds when no conflicting writer exists
  (src/gobby/tasks/closure_snapshot.py:34-81). The candidate-path checks apply only with a candidate
  commit or the `security-review` label.
- No commit is needed. `integration_ancestry` is computed only when commits exist
  (src/gobby/mcp_proxy/tools/tasks/_lifecycle_close.py:177,218-249), and only the coding rule reads
  it. The closing reference says "For non-Git work, omit the commit and repository parameters" and
  "Default can supply its own required evidence ... and close without a manager or a general
  independent reviewer" (references/tasks/closing.md).
- Attestation kinds: `default` writes `["*"]` and declares no kinds (default.yaml:16). So any kind
  and any result is recordable (attestations.py:240-247). Closed tasks refuse new attestations, so
  record everything before the close.
- No-edit requirement: `require-commit-before-status` blocks `close_task` and `de_escalate_task` when
  the task has attributed edits and no commit
  (src/gobby/install/shared/workflows/rules/task-enforcement/require-commit-before-status.yaml:9-18).
  So keep the review packet out of the checkout. Put it in attestation `evidence` or in a scratch
  file.
- No validation run is required by policy for these categories. The close finalization has no
  hard-coded validation gate (src/gobby/mcp_proxy/tools/tasks/_lifecycle_close_finalization.py:21-131).
- Protections: `default` respects task protections (default.yaml:17; _lifecycle_close.py:95-104).
  This project protects the label `deferred-by-josh` and the subtree roots #22978 and #22225
  (.gobby/project.json `task_protection_policy`;
  src/gobby/tasks/lifecycle_access.py:60-93). So do not place review tasks under those roots.
- `task_close_policy.enforce_step_records: true` in .gobby/project.json has no consumer. gcode greps
  of src/, crates/, tests/ and docs/ found zero hits for `enforce_step_records` and
  `task_close_policy`. It adds no requirement; it is dead config (see found work).
- Josh cannot close a review task alone from his shell. The CLI close is evaluated by the policy
  under the `default` fallback (src/gobby/cli/tasks/crud.py:266-293;
  src/gobby/tasks/closure_snapshot.py:150-170), and it still needs the `implementation` attestation,
  which only a session can author.

## (e) Holding a live observation window of several hours

Limits of wait_for_coordination:
- The default timeout is 900 s and the maximum is 3600 s, enforced at registration
  (src/gobby/mcp_proxy/tools/coordination.py:42; src/gobby/storage/coordination_waits.py:70).
- `background_task_id` must be 1-256 characters, the waiter must be its own owner, and
  `owner_session` must be omitted (coordination_waits.py:54-61; coordination.py:72-75). Nothing
  checks it against a real provider task.
- Re-registration: while a wait is waiting, the same condition returns the original wait and its
  expiry (coordination_waits.py:81-87). After a terminal outcome, the same condition creates a new
  wait (coordination.py:42-45).
- The wait protects the stop only while it is unresolved and unexpired, and only while its owner's
  status is not terminal (coordination_waits.py:18-36).

What ends the wait:
- The next prompt to the session cancels self-owned waits and marks them delivered, so no wake
  follows. That prompt is either the provider's completion notification or Josh typing
  (src/gobby/hooks/event_handlers/_agent.py:188-195, inside `handle_before_agent` at :138-271;
  coordination_waits.py:143-158).
- On timeout the delivery service wakes the session with `Coordination wait <id>: timeout`
  (src/gobby/events/coordination_waits.py:113-149, wake at :139-144).
- This session has already run the hourly cycle on a Workflow task id: wait 931125bd timed out at
  the 3,600 s cap (state.md, entries 16:45 and 17:46).
- Daemon restarts are safe. At daemon start, a wait whose deadline passed during downtime gets a
  fresh 900 s grace (coordination_waits.py:175-189; events/coordination_waits.py:40).

Seat type:
- A headless spawned run cannot register the wait at all (coordination.py:64-69;
  src/gobby/mcp_proxy/tools/headless_waits.py:34-68). So review and cutover sessions must be root
  interactive terminal sessions.

Session liveness:
- Sessions go idle after 30 minutes and expire after 24 hours of inactivity
  (src/gobby/config/sessions.py:145-152; src/gobby/storage/session_lifecycle.py:177-237).
- `idle` is a live status (src/gobby/storage/sessions/_constants.py:43-52). The stale-claim sweep
  releases only the claims of sessions that are not live (_automation.py:89-148). So a held claim
  survives an idle window, and the hourly wakes keep the session active.

Idle auto-compact:
- Provider compaction is in place: "The same session row, external identity, task claims, workflow
  state, and terminal ownership survive" (docs/contracts/session-boundary.md:5-7). The wait row is
  keyed by the session row, so it survives too.
- Compaction fires `PreCompact` and `SessionStart(compact)`, not a prompt (memory 1d61ae44). The
  only caller that clears the wait is the prompt handler (gcode grep: one caller,
  _agent.py:191), so compaction does not cancel the wait.
- Compaction does reset schema leases and loaded skills (memory 1d61ae44). So a woken session must
  re-fetch `get_tool_schema` before it calls `wait_for_coordination` again.
- The window's plan has to live in durable places: the task's attestations and the watch log file.
- decisions.md:94-98 already says chunk sessions run with idle auto-compact off and must survive one
  anyway.

Alternatives, and why each is worse:
- Escalate the cutover task for the window. That frees the claim and stops the hourly wakes, but it
  is not a human decision. creation.md allows escalation only for "a concrete unresolved decision or
  blocker".
- `add_dependency` on a "window" task. That needs something to close the window task on time, so it
  adds mechanism.
- A Gobby cron one-shot job (`schedule_type="once"`, `run_at`). The action can be `agent_spawn` or
  `pipeline`; `shell` is refused to agent callers
  (src/gobby/mcp_proxy/tools/cron.py:98-182, line 132). The run happens in a fresh, system-parented
  session with no cutover context, and the default agent timeout is 300 s
  (docs/guides/cron-scheduler.md:13-16,104-105). It cannot hold the cutover task's claim, and it
  cannot react to a rollback trigger before the window ends. At most it is a backstop if the held
  session dies.
- Provider-side scheduling, such as Claude Code's /loop or CronCreate, was not tested here and is
  not needed. The hourly timeout wake already gives a tick.

Quiet hours are guidance only. No code reads a `quiet_hours` value. The only bundled mention is
src/gobby/install/shared/workflows/rules/roles/inject-seat-common.yaml:21. So the protocol must
schedule a window to end before quiet hours, or get a standing ruling from Josh for an emergency
rollback restart during them.

## Found work for the parent session (not fixed: this stage is read-only)

1. Defect. `block-gobby-tasks-cli` 1.1.0 lists `de_escalate`
   (src/gobby/install/shared/workflows/rules/task-enforcement/block-gobby-tasks-cli.yaml:19). The real
   CLI subcommand is `de-escalate` (src/gobby/cli/tasks/crud.py:331; tests/cli/test_validation_cli.py:41).
   - Regex check with `python3 -I`: `gobby tasks de-escalate #5 -r go` gives False, and
     `uv run gobby tasks de-escalate #5 --reason go` gives False. `gobby tasks de_escalate #5` (not a
     real command) gives True.
   - So an agent can de-escalate through the CLI, with no session, no authority check and no
     `require-commit-before-status` (_crud_mutations.py:259-280).
   - The fix needs a rule-17 version bump and a test. No test covers this rule today.
2. Stale memories.
   - 4747269e says that for de-escalation "unrelated sessions remain denied". That holds only while
     the escalated task is claimed. Escalation always unclaims it, so any session passes
     (_authorization.py:87-136).
   - 0ea212cb says that "close_task needs override_justification". The live `close_task` schema has no
     such parameter, and the close clears escalation (_close.py:80-96).
3. Dead config. `.gobby/project.json` `task_close_policy.enforce_step_records` has no consumer
   anywhere in src/, crates/, tests/ or docs/.

## Recommended protocol (the implications)

Two sibling tasks per checkpoint, linked by `blocks` edges. One task per checkpoint is the fallback
if the map wants fewer nodes.

1. A review task, for example CP-RUNNER-REVIEW (the restructure picks the names).
   - Category `manual`. Validation criteria: Josh's go or no-go is recorded verbatim in a
     de-escalation note and in an approved `implementation` attestation, and a packet attestation
     exists.
   - It depends on every chunk of its stage.
2. A cutover task, for example CP-RUNNER-CUTOVER.
   - Category `manual` when it commits nothing. If it must commit, for example version bumps, the
     full coding policy applies, so land the bumps with the stage's last code chunk instead.
   - It depends on the review task.
3. Next-stage chunks depend on the cutover task by default.
   - A failed live window then holds the fan-out.
   - Landings stay frozen while the window runs, because any restart promotes whatever has landed.
   - The restructure may point an individual edge at the review task where the live flip is not an
     input.
4. Why two tasks:
   - Any close releases dependents, so Josh's go needs a close of its own.
   - A rollback must not erase the go record.
   - A no-go never makes a cutover ready.
   - The cutover gets a fresh session and context.

Review session (a root interactive `default` session):
1. Claim the review task.
2. Build the packet. Record it with `record_attestation(kind="review-packet", result="submitted",
   summary=..., evidence={...})`. Any kind and result is allowed for `default`.
3. Call `escalate_task(task_id, reason="needs_human: go or no-go for <checkpoint>; packet in
   attestation <id>")`.
4. Present the packet and the question in the final message, then end the turn. The gate allows it,
   because the claim is gone.
5. Josh answers in that terminal. If that session is gone, he answers in any later `default`
   session, or with `gobby tasks de-escalate`, or in the web UI.
6. On go:
   - `de_escalate_task(reason="GO from Josh <timestamp>: <his words>")`;
   - `claim_task`;
   - `record_attestation(kind="implementation", result="approved", evidence={decision, quote,
     answered_at, packet_attestation_id})`;
   - `close_task(reason="done", changes_summary=..., preview=true)`, then the same call without
     preview. Pass no `commit_sha`.
7. On no-go:
   - `de_escalate_task(reason="NO-GO from Josh ...: <his words>")`.
   - Keep the task open.
   - Link the fixes Josh names with `add_dependency(task_id=<review>, depends_on=<fix>)`. Label any
     new filings `operator-directed`, which the found-work gate exempts
     (enforce-found-work-ladder.yaml:67). A kept claim with open blockers is a durable wait.
   - After the fixes close, build a new packet and escalate again.
   - Never close a no-go. If Josh abandons the direction, re-plan the downstream edges before any
     `wont_do` close, because that close releases them too.

Cutover session (a root interactive `default` session):
1. Claim the cutover task only after the review task is closed.
2. Run the cutover steps. Fact-finders F1 to F3 cover them.
3. Record the window plan as an attestation before the window starts: start, end, named rollback
   triggers and the rollback command.
4. Start a bounded watch as a provider background task. It samples the triggers, logs to a file and
   exits early on a trigger. The background task can be a shell or a Workflow.
5. Call `wait_for_coordination(background_task_id=<its task id>, timeout=3600)` and end the turn.
6. On each timeout wake:
   - re-fetch the schema if the context was compacted;
   - read the watch log;
   - register the same wait again;
   - end the turn.
   A prompt from Josh cancels the wait, so register again before that turn ends.
7. When the watch exits:
   - On a trigger: roll back, keep the task open, and escalate the incident to Josh.
   - Otherwise: record an approved `implementation` attestation with the window evidence, and close
     the task with no commit.
