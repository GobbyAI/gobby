Plan artifact: `.gobby/plans/tasks-family.md`

# Gobby 1.0 Stage 2: Tasks family

**Plan ID:** tasks-family

## Overview
`kind: framing`

Implementation root: #21560 (Tasks family). Planning assignment: #25252. Phase P2
lands under #20822 (hub authority matrix) and phase P3 under #21561 (write path
and lifecycle). Rust owns task rows, reads, writes, lifecycle transitions, close
gates, landing, the hub authority matrix, the seventeen `/api/tasks` HTTP routes
and their WebSocket events. One statically linked family crate, `crates/gtasks`
(package `gobby-tasks`, lib `gobby_tasks`, `publish = false`), implements them on
#21559's RouteFamily, PostgreSQL pool and compare seams, using `crates/gruntime`
as its template. The hub owns task state and standalone runs the same owner.
Nodes route `/api/tasks` to the hub over the existing hub/node transport and hold
no database credential.

This work needs to exist: the tasks ledger fragment still names Python for all
121 live rows. Existing seams cover part of the problem and are reused: #24883's
tool descriptor contract, #24970's domain errors and transport adapters, #24963's
waiver manifest and readiness rules, the gcore advisory-lock transaction seam
and the native WebSocket broker. There is no second task store, no new queue
and no schema change. Authority grounds use the existing
`task_lifecycle_events` table. Close and landing policy stays declared in the
`task-closure` rules; gtasks evaluates it through a `ClosurePolicy` port that
#24151 implements over the workflow engine (C1).

Planning proceeds now. Implementation waits on R0 (#25468, task-25460 Decision 2
and ROADMAP decision 26): the P0 phase heading carries `(depends: #25468)`, so no
deliverable starts until R0's criteria in task-25460 1.1 are met.

Row accounting for the 121 live rows of `docs/contracts/parity-ledger-status.d/tasks.json`
(125 rows; 4 are already `dropped by decision #25449`, the retired step tools):

- 17 HTTP routes are ported here. 15 rows become `delegated` after native
  parity. `POST /api/tasks/{task_id}/claim` is built natively but stays
  `proxied` with a #21562 owner note until #21562 supplies the native
  assignment mailbox (Q11). `POST /api/tasks/{task_id}/close` is built natively
  but stays `proxied` with a #24151 owner note until #24151 supplies the native
  closure policy (C1).
- 46 MCP rows (35 `gobby-tasks`, 11 `gobby-tasks-ops`) get native descriptors and
  proven schema and result parity here. #21570 activates them; the rows stay
  `proxied` with #21570 owner notes.
- 33 CLI rows get a verb-to-public-API map here (Q12). #21571 builds the client
  commands over the public MCP tool route; the rows stay `proxied` with #21571
  owner notes.
- 4 additive `gobby-tasks-ops` tools (`doctor_tasks`, `get_task_stats`,
  `get_validation_history`, `restore_tasks`) give the operator-only verbs a
  public operation. They are native extensions with no Python row (Q12).
- 19 expansion rows (12 `gobby-tasks-ops` MCP tools and 7 CLI verbs) move to
  #21581, which consumes this plan's transactional write service.
- `MCP gobby-tasks:team_progress` moves to #21567 (D); Tasks ships no
  descriptor for it.
- 5 rows (`gobby tasks compact` and its three subcommands, `gobby tasks clean`)
  become `dropped by decision #25252`.

Native construction is passive in production until 4.3 switches key callers to
the native backend. Browser cookie callers follow in 4.4. Each deliverable below
is one independently verifiable outcome.

## Decision Record
`kind: framing`

The Orchestrator ruled these through Triage (gobby#17480) on 2026-10-28:

- **Q1 planning task.** #25252 is the planning task under #21560; the plan is
  drafted in the main checkout.
- **Q2 expansion rows.** The 19 expansion rows move to #21581 and the ledger
  notes point there. This plan exports the transactional write service that
  #21581's expansion apply consumes, named as an explicit contract in 3.1.
- **Q3 MCP descriptors.** This plan ships the `gobby-tasks` and `gobby-tasks-ops`
  descriptors and proves schema and result parity (Cron Decision 6 precedent).
  #21570 activates them. The #21566 -> #21560 edge stays.
- **Q4 authority window.** HTTP enforces the native authority matrix when 4.3
  activates it. CLI enforces it when #21571 ports the verbs. MCP stays on the
  Python guards until #21570 flips `gobby-tasks` and `gobby-tasks-ops` to the
  native backend. Constraints states the window and its end condition. No
  throwaway loopback client is built.
- **Q5 debt.** Drop the four compact rows; drop `gobby tasks clean` and the
  doctor orphan-dependency check (task_dependencies foreign keys are
  `ON DELETE CASCADE DEFERRABLE`, so orphans cannot persist); doctor keeps the
  cycle and invalid-project checks. Operator subtree protection becomes a
  `protected_subtrees` entry in the project policy rule, not a label and not a
  hardcoded constant. *Superseded in part on 2026-11-01 (ruling B):* #25228
  already moved protection into `.gobby/project.json`'s
  `task_protection_policy`, so the protection mechanism follows B and 2.2 is a
  native port only. The drops stand.
- **Q6 CLI ownership.** CLI rows stay `proxied` with #21571 owner notes. This
  plan exports a documented contract for all 33 verbs, which Q12 makes a
  verb-to-public-API map. The
  Python CLI remains a direct-storage writer until #21571 replaces it.
- **Q7 lock keys.** #25262 gives every hub `LockTarget` an explicit key and makes
  the repr fallback raise. Deliverables that take Python-shared locks depend on
  #25262; this plan carries no copy of that fix.
- **Q8 takeover grounds.** A takeover of a live claim needs one recorded ground:
  `owner_dead`, `delegated`, `coordinator` or `operator`, written to
  `task_lifecycle_events`. `owner_idle` is not adopted: no daemon-verifiable
  idle state exists (`src/gobby/storage/sessions/_constants.py::LIVE_SESSION_STATUSES`
  has none). An owner cedes through `hand_off_task_claim`. The audit cites
  #20818 for these grounds, but #20818 is a closed, unrelated attribution fix,
  so this plan owns them. *Amended 2026-11-01 (ENH-STALENESS-2):* the
  `coordinator` ground is admitted by a declared capability
  (`coordinate_tasks` on a local root session, or `manage_task_claims` within
  `task_claim_scope`), never by a definition name.
- **Q9 gate-bearing labels.** The seven gate-bearing labels are `tdd:required`,
  `needs-decision`, `needs-planning`, `clean-window`, `live-session`,
  `deferred-by-josh` and `security-review`. Adding one follows the task's
  ordinary mutation authority. Removing one requires the operator, the
  Orchestrator or the daemon. `deferred-by-josh` add and remove are
  operator-only. `live-session` keeps its existing terminal, non-spawned guard
  on top. Each affected MCP surface gets its own #24963 waiver. *Amended
  2026-11-01 (ENH-STALENESS-2):* "the Orchestrator" means a session whose
  definition declares `coordinate_tasks` (2.1's coordinator relationship).
- **Q10 epic-close plan archival.** Closing an epic as done or moot archives its
  linked plan today (`src/gobby/storage/tasks/_close.py::_schedule_ancestor_epic_archive`
  through `src/gobby/storage/plans.py::LocalPlanManager.archive_plan`). gtasks
  owns the terminal-epic after-commit hook and ships the narrow native archive
  step in 3.7: move the plan file, update the `plans` row, roll back the move on
  failure, and do nothing when no plan is linked. Cross-plan edge: #21581 later
  moves `crates/gtasks/src/plan_archive.rs` into the Plans crate and rewires the
  hook. 6.1 records that handoff in #21581's ledger notes.
- **Q11 claim assignment notice.** HTTP claim sends a post-commit
  `task_assignment` mailbox message with a live wake through
  `src/gobby/servers/routes/tasks_assignment.py::TaskAssignmentNotifier.send`
  and `src/gobby/sessions/mailbox.py::MailboxService.send`. No native mailbox
  exists, and its owner #21562 is blocked by #21560, so a Tasks leaf cannot
  depend on it. The `AssignmentNotifier` port lives in gtasks (4.1). After 4.3,
  `POST /api/tasks/{task_id}/claim` stays proxied, and its ledger row carries
  the #21562 owner note. Cross-plan edge: #21562 implements the port over its
  native mailbox and activates the route with its own compare evidence.
- **Q12 CLI transport.** #21571 carries every `gobby tasks` verb in the Rust
  client over the public `POST /api/mcp/{server}/tools/{tool}` route, the route
  its read slice already uses. A DB-free client or node never calls gtasks in
  process. 5.5 publishes the verb-to-public-API map. `doctor`, `stats`,
  `validation-history` and `restore` have no public operation today, so 5.4
  adds them as operator-only `gobby-tasks-ops` tools with #24963
  native-extension entries; restore reads only the hub's default backup path.
  #21571's inventory row now names #21571 (ad3f8ab), ending the two plans'
  mutual deferral.
- **Q13 residual #20822 requirements.**
  - (a) Dependency edges: `add_dependency`, `remove_dependency` and
    `create_task(blocks=[...])` need claim authority over each task whose
    readiness changes. That is `task_id` for an add or remove, and each blocked
    task for `blocks`. Unclaimed tasks stay open to project sessions.
  - (b) `auto_link_commits` with a `task_id` needs claim authority over that
    task. A project-wide scan without one is coordinator-lane: `Operator`, the
    coordinator (`coordinate_tasks`), the claim manager (`manage_task_claims`
    within `task_claim_scope`) or `Daemon` (amended 2026-11-01,
    ENH-STALENESS-2).
  - (c) Sessions keep `backup_tasks` to the default path only, which removes
    the audit's any-caller-path hazard; restore stays operator-only.
  - (d) The expansion runtime (audit item 3) transfers to #21581 with Q2's rows.
    3.1's write service takes the caller's principal, and expansion apply
    passes the run's initiating session rather than `Daemon`, so 2.1's table
    refuses an auto-apply under a foreign live claim. #21581 owns cancel and
    resume scoping.
  The other #20822 items are dispositioned in 2.1 and accepted.

The Orchestrator ruled the 2026-11-01 staleness revision through Triage
(c54509e0) and accepted the enhancer's ENH-STALENESS edits. Each ruling
supersedes the earlier text only where it says so:

- **A. Node checkout git (2026-11-01).** ROADMAP decision 26 moved node work to
  Stage 4, so 3.8 becomes `kind: deferred` with a typed deferral to #21546.
  Stage 2 keeps hub-local git (`crates/gtasks/src/git.rs` runs git at the
  stored checkout path, as Python does) and 3.7.5's refusal of a remote-session
  close. The node-checkout-git waivers are dropped, 3.9 depends on 3.7, and the
  4.1a node reader moves into the deferral as 3.8.5. The Orchestrator labels
  #21546 `deferred-from:tasks-family:3.8` at approval.
- **B. Task protection (2026-11-01).** Q5's protection mechanism is superseded
  by #25228 (1277cc93eb). Protection is `.gobby/project.json`
  `task_protection_policy` (`foreign_claims`, `labels`, `subtree_roots`), read
  by `src/gobby/config/tasks.py::load_project_task_protection_policy` and
  applied by `src/gobby/tasks/lifecycle_access.py::task_protection_refusal` to
  definitions that declare `respect_task_protections`. 2.2 is a native port only
  and edits no Python, template or policy file. D3 is done in Python.
- **C1. Closure policy port (2026-11-01).** #25449 (727cd66893) moved close and
  landing decisions into declared `task-closure` rules evaluated by
  `src/gobby/tasks/closure_policy.py::evaluate_closure_policy` over a
  `ClosureSnapshot` that `src/gobby/tasks/closure_snapshot.py::prepare_closure`
  builds. gtasks defines a
  `ClosurePolicy` port (`ClosureSnapshot` in, `ClosureDecision` out) and builds
  the snapshot and its facts, applies the pre-policy refusals, finalizes,
  auto-closes ancestors, archives plans and lands; it does not port the rule
  evaluator. Proof is a scripted decision plus snapshot parity against Python
  (0.3, 3.7, 3.10). `POST /api/tasks/{task_id}/close` is built natively but
  stays `proxied` with a #24151 owner note, on the Q11 precedent. Cross-plan
  edge: #24151 implements the port over gworkflows (its s2-11 Tasks seam
  adapter, amended by the Orchestrator), proves decision parity and activates
  the route. MCP `close_task`, `land_commit` and repair activate with #21570,
  which waits on #24151.
- **D1. Team progress (2026-11-01).** `MCP gobby-tasks:team_progress` is a team
  read, so it is re-homed to #21567 and Tasks ships no descriptor for it. 6.1
  records the re-home.
- **ENH-STALENESS-1.** Attestations (`record_attestation`, `list_attestations`)
  replace the four retired `record_*_step` tools in 3.7, 5.2 and the parity
  table.
- **ENH-STALENESS-2.** 2.1's inputs are principals plus declared capabilities
  and scopes. No native code compares a definition name other than `default`
  (rule 16). Q8, Q9 and Q13b are amended in place to match.
- **ENH-STALENESS-3.** The P0 phase heading carries `(depends: #25468)`.
  Planning proceeds now; implementation waits on R0, and this plan cites only
  R0's criteria in task-25460 1.1.

Earlier hub-node rulings for Stage 2 families apply: macOS and Linux only
(Windows is #25249), and no latency gates.

Planning choices recorded here, each checked against the code:

- **Transcript evidence stays with #21567.** `src/gobby/tasks/transcript_*` is
  consumed only by workflow code (found-work gate, validation cover, condition
  helpers, task tool bindings, Rust test evidence), not by any tasks ledger
  surface. Close gates here read the existing session variables and git state.
- **Session-variable claim bookkeeping.** Claims and releases update
  `claimed_tasks`, `task_claimed`, `task_has_commits`, `active_task_id` and
  `task_selection_history` in the same transaction as the task row, under the
  `SessionVariableMutation` lock. gtasks implements this against the
  `session_variables` table. The seam is named in 3.5 so #21562 and #21567
  inherit it rather than reimplement it; neither is a dependency, because both
  wait on #21560.
- **Definition identity.** `_agent_type` is trusted only as the key that
  resolves the session's enabled installed definition
  (`src/gobby/workflows/agent_resolver.py::session_agent_definition`, which also
  returns the content digest). Authority comes from that definition's declared
  fields, never from its name; `default` is the only name gtasks resolves. The
  variable is listed in `src/gobby/workflows/reserved_variables.py::RESERVED_WORKFLOW_VARIABLES`,
  so `set_variable` refuses it, and spawned sessions refuse definition changes
  (`src/gobby/mcp_proxy/tools/apply_agent_definition.py::activation_decision`).
  The audit's "spawn-time-only role" pattern needs no native change.
- **Landing scope.** Native `land_commit` lands onto the target that
  `src/gobby/tasks/land_commit.py::task_landing_target` returns, exactly as
  Python does: a team leaf lands in its epic team's worktree and branch (read
  through `src/gobby/agents/team_lifecycle.py::task_team` and
  `src/gobby/agents/team_admission_records.py::team_binding`), and any other
  task lands on the hub-local main checkout from `main_checkout_target`. It
  keeps Python's refusals when no target exists or the epic worktree changed
  branch.
- **Task change listeners.** `LocalTaskManager.add_change_listener` has no
  production registrant, so it is not ported.
- **Rollback.** Python task routes stay mounted as the proxy rollback target
  until #21574 deletes the Python daemon package. Rollback before that deletion
  is the same binaries with a routing edit.

## Debt dispositions and shared conventions
`kind: framing`

Each disposition is either removed before porting or corrected in native code
under an exact #24963 waiver. No whole live surface is dropped except D1 and D2.

- **D1 compaction.** `gobby tasks compact` and its `analyze`, `apply` and
  `stats` subcommands plus `src/gobby/storage/compaction.py::TaskCompactor` are
  retired in 0.1. Only the CLI and its tests use TaskCompactor.
- **D2 orphan cleanup.** `gobby tasks clean` and the doctor orphan-dependency
  section are retired in 0.1. Doctor keeps cycles and invalid projects.
- **D3 protected subtrees.** Done in Python by #25228 (ruling B):
  `OPERATOR_SUBTREE_SEQ_NUMS` is gone, protection reads `.gobby/project.json`
  `task_protection_policy`, and `references/tasks/closing.md` and
  `workflows/agents/consolidator.yaml` no longer name #22978. 2.2 ports the
  policy natively. One bundled template line still names #22978 and #22225
  (`src/gobby/install/shared/skills/gobby/references/agents/procedures/orchestrator.md`,
  line 12); it is outside this plan and was reported to the Orchestrator as
  found work.
- **D4 lock keys.** Done by #25262: every hub lock class now has an explicit key
  in `src/gobby/storage/hub/postgres_pool.py::advisory_lock_keys`, and an
  undeclared class raises `TypeError`. 3.1, 3.2 and 3.5 keep their #25262
  heading edges, which are already satisfied.
- **D5 UUID reference scoping.** `src/gobby/mcp_proxy/tools/tasks/_resolution.py::resolve_task_id_for_mcp`
  scopes `#N`, numeric and dotted refs to the caller's project, but a raw UUID is
  a global lookup. Native mutations resolve every reference through one policy,
  the one `src/gobby/tasks/lifecycle_access.py::resolve_lifecycle_task` already
  applies to landing and attestations: local refs resolve normally, and foreign
  refs resolve only for a local coordinator (a descendant owning the task, or a
  `coordinate_tasks` definition on a root session). Reads keep their explicit
  cross-project selection through `all_projects` and `project`.
- **D6 claim takeover.** `claim_task(force=True)` overrides a live claim with no
  liveness check or consent record. Native takeover needs a Q8 ground.
- **D7 gate-bearing labels.** Only `live-session` has a guard today. Native label
  changes follow Q9.
- **D8 parallel lanes.** HTTP routes reach storage with operator authority and
  no session identity. Native HTTP, MCP and CLI lanes call one service that
  checks one transition table. HTTP callers are the operator principal, so HTTP
  results do not change; the ground `operator` is recorded where a transition
  is exceptional.
- **D9 dependency readiness authority.** `add_dependency`, `remove_dependency`
  and `create_task(blocks=[...])` change another task's readiness with no check
  today (`src/gobby/mcp_proxy/tools/task_dependencies.py::create_dependency_registry`).
  Native edges need claim authority over each task whose readiness changes
  (Q13a).
- **D10 auto-link lane.** `auto_link_commits` links any task its scan finds,
  with no claim or lane check. Native auto-link follows Q13b.

Already resolved, ported unchanged: fail-open session resolution left with the
stage tools under #23341, and revoke-instead-of-deny was fixed by #20821's
deny-first ordering in `src/gobby/mcp_proxy/tools/tasks/_authorization.py::require_claim_authority`.

**Waiver inventory.** 0.3 adds these entries to `docs/contracts/parity-waivers.json`.
IDs use `<family>.<surface-slug>.<behaviour-slug>`. A tool slug is the tool name
with `_` replaced by `-`; a CLI verb slug joins its words with `-`. Each entry
names one exact ledger surface, a one-sentence behaviour, decision
`.gobby/plans/tasks-family.md#<owning section>` and the required native
replacement. No wildcard surface is valid.

- D5 `tasks.mcp-<tool-slug>.project-scoped-refs`, decision `#2.1`, for MCP
  gobby-tasks `add_dependency`, `add_label`, `auto_link_commits`, `close_task`,
  `create_task`, `de_escalate_task`, `delegate_task`, `delete_task`,
  `escalate_task`, `hand_off_task_claim`, `link_commit`, `link_task_to_session`,
  `release_task_claim`, `remove_dependency`, `remove_label`, `reopen_task`,
  `repair_closed_candidate`, `unlink_commit`, `update_observed_files`,
  `update_task`, and MCP gobby-tasks-ops `append_description_section`,
  `clear_isolation_pair`, `set_affected_files`, `set_artifact`,
  `set_artifacts_atomic`. An entry is added only when 0.3's recorded Python
  vector resolves a foreign-project UUID on that surface; a surface that already
  refuses keeps ordinary parity and gets no entry. `claim_task` already refuses
  cross-project claims, and `land_commit`, `record_attestation` and
  `list_attestations` already use `resolve_lifecycle_task`, so they are
  excluded.
- D6 `tasks.mcp-claim-task.takeover-grounds`, decision `#3.5`.
- D7 `tasks.mcp-create-task.gate-label-authority`,
  `tasks.mcp-update-task.gate-label-authority`,
  `tasks.mcp-add-label.gate-label-authority` and
  `tasks.mcp-remove-label.gate-label-authority`, decision `#2.1`.
- D9 `tasks.mcp-add-dependency.readiness-authority`,
  `tasks.mcp-remove-dependency.readiness-authority` and
  `tasks.mcp-create-task.readiness-authority`, decision `#2.1`.
- D10 `tasks.mcp-auto-link-commits.lane-authority`, decision `#2.1`.
- Q12 CLI paths: `tasks.cli-backup.default-path-only` and
  `tasks.cli-restore.default-path-only`, decision `#5.5`. Python's `--output`
  and `--input` options name any local path; over the public API, backup and
  restore use only the hub's default path.
- No node-checkout-git entries: native git runs at the stored path on the hub,
  as Python does, so those surfaces keep ordinary parity. Node checkouts are
  deferred to #21546 (3.8, ruling A).
- S3 MCP: `tasks.mcp-<tool-slug>.internal-error-redaction`, decision `#5.1`,
  `#5.2`, `#5.3` or `#5.4` by owning section, one per tool for all 46 tools
  enumerated in 5.1-5.4. Python's proxy surfaces raw unexpected exception text;
  native returns #24970's safe internal message.
- S3 CLI: `tasks.cli-<verb-slug>.safe-errors`, decision `#5.5`, for the 29
  domain verbs `backup`, `blocked`, `close`, `commit-auto`, `commit-link`,
  `commit-list`, `commit-unlink`, `create`, `de-escalate`, `delete`, `dep-add`,
  `dep-cycles`, `dep-remove`, `dep-tree`, `diff`, `doctor`, `label-add`,
  `label-remove`, `list`, `ready`, `reindex`, `reopen`, `restore`, `search`,
  `show`, `stats`, `suggest`, `update` and `validation-history`. The four group
  rows (`gobby tasks`, `commit`, `dep`, `label`) change no domain behaviour and
  get no entry. #21571 asserts these when it delivers the verbs.

HTTP already returns the safe `{"detail":"Internal server error"}` on unexpected
failures (`src/gobby/servers/routes/tasks.py::create_tasks_router`), so HTTP
rows need no safe-error waiver.

**Shared conventions.** Use #24970 as the single native domain-error owner:
validation, missing, forbidden, conflict, capacity, unavailable and internal map
onto its HTTP, MCP and CLI adapters, and deliberate Python envelopes are declared
through those adapters. Internal errors expose only the shared safe message.
Use one presence-preserving partial-update convention across HTTP, MCP and CLI:
Absent leaves a field unchanged, Clear is explicit null, Set(value) supplies a
value; field-specific nullability follows the frozen Python behaviour from 0.3.
Use #24963's readiness rule: at least one ordinary Passed case, explicit
expected-native assertions for every cited Waived case, stable coverage of every
required case, and failure for an all-Skipped report. Use #24964's shared outer
CORS and preflight handling, with no family-local CORS code. A conflict with
another family's convention goes through Triage to the Orchestrator before
review.

## Constraints
`kind: framing`

- **Authority window (Q4).** The native matrix governs HTTP from 4.3 and CLI from
  #21571's delivery. Until #21570 flips `gobby-tasks` and `gobby-tasks-ops` to
  the native backend, MCP calls keep the Python guards in
  `src/gobby/mcp_proxy/tools/tasks/_authorization.py`, including the ungated
  `force=True` claim. The window ends when #21570's flip lands; no Python guard
  is extended to imitate the native matrix in the meantime. Python already
  applies 2.2's protection policy (#25228), so no shared reader is needed.
- **R0 gate (ENH-STALENESS-3).** Planning proceeds now. Implementation waits on
  R0 (#25468): the P0 heading's `(depends: #25468)` blocks every leaf until R0's
  criteria in `.gobby/plans/task-25460.md` 1.1 hold (task-25460 Decision 2,
  ROADMAP decision 26).
- **Dual writers.** Python `LocalTaskManager` and native gtasks both write task
  rows during the transition. Native writes take the same advisory locks with
  the same key strings as Python's
  `src/gobby/storage/hub/postgres_pool.py::advisory_lock_keys`, in the same
  priority order as the classes in `src/gobby/storage/hub/protocol.py`:
  `main_checkout_landing:{project_id}` (MainCheckoutLanding, 25),
  `agent_task_claim:{session_id}` (AgentTaskClaimMutation, 100),
  `task_seq:{project_id}` (TaskSeqAllocation, 200),
  `expansion_apply:{parent_task_id}` (ExpansionApplyMutation, 250),
  `task_dependency_mutation` (TaskDependencyMutation, global, 275),
  `dispatch_mutex:{task_id}` (DispatchMutexRow, 300),
  `task_subtree_cascade:{project_id}` (TaskSubtreeCascade, 400),
  `task_lifecycle:{task_id}` (TaskLifecycleMutation, 450),
  `session_variable:{session_id}` (SessionVariableMutation, 950), and
  SessionVariableTransfer (950), which takes both `session_variable` keys in
  sorted session order. gtasks implements each as a
  `crates/gcore/src/postgres_pool/transaction.rs::LockTarget` (priority plus
  keys), whose nested acquisition already refuses a non-increasing priority.
  The dual-writer window ends when the last Python writer is ported: MCP by
  #21570, CLI by #21571, workflows by #21567 and expansion by #21581, all
  before #21574 deletes the Python package.
- Every production file stays below 1,000 lines; split before 850. Rust tests
  live in `crates/gtasks/tests/` or `<module>/tests.rs`, never inline.
  `crates/gdaemon/src/serve.rs` is 828 lines; family registration adds only the
  construction, push, broker-local and bind lines.
- Each production Rust leaf carries its release: bump every affected shipped
  binary by one patch from its executor's starting tip, update `Cargo.lock` and
  `src/gobby/install/version_pins.py::MANAGED_BIN_VERSION_PINS`, and validate the
  pins. Root workspace changes affect gcode, gdaemon and ghook; changes confined
  to gtasks or gdaemon affect gdaemon once gdaemon links gtasks (4.1a onward).
  Before 4.1a gtasks is not linked into any binary, so leaves 1.2 through 4.1
  bump nothing. Unpublished gclient, gmcp and gterm get no release bumps.
- The release owner rebuilds every affected dependent and promotes through
  `promote_workspace_binary_set`. Only the Orchestrator delegates live cutover;
  announce globally and wait for no live worker or close validator first. This
  plan's author runs no live cutover.
- All Python commands use uv. Pytest uses the isolated hub:
  `DATABASE_URL=postgresql://gobby_test:gobby_test@127.0.0.1:60892/gobby_test GOBBY_TEST_PROTECT=1 uv run pytest <focused-path>`.
  Never run the full suite. Native tests use isolated temporary state and ports.
  Cargo runs require the heavy-run grant, set `CARGO_BUILD_JOBS=3`, name the
  package and run through `cargo nextest run`, the runner `crates/AGENTS.md`
  mandates (its `.config/nextest.toml` serializes DB-backed tests; a bare
  `cargo test` flakes them). Descriptor consumers run the whole
  `cargo nextest run -p gobby-core --test mcp_contract` target.
- New native APIs keep existing authentication and project authorization. Nodes
  route to the hub over the existing hub/node transport and never open the
  database. macOS and Linux only; no latency gates.
- No invented workflow event names: rule integration uses semantic events such
  as `turn_start` and `turn_end`.
- Scoped literal sweeps observed on the main 0.5.0 checkout on 2026-11-01:
  `gcode grep -F '#21560' docs/contracts/parity-ledger-status.d/ -m 200`;
  `gcode grep -n "22978" src .gobby/workflows docs -m 30` (consumers: the
  project-local `.gobby/workflows/rules/project-operating-policy.yaml` and the
  bundled `references/agents/procedures/orchestrator.md` line 12, reported as
  found work under D3);
  `gcode grep -n "tasks compact|tasks clean|TaskCompactor" docs src/gobby/install/shared README.md -m 30`
  (consumers: the ledger, `docs/guides/cli-commands.md`,
  `docs/reference-audit/tasks.json`; `docs/plans/completed/TASKS.md` is history
  and stays); `gcode grep -l "TaskCompactor" src tests -m 30` (main.py,
  compaction.py and two test files).

## Parity ownership
`kind: framing`

The 2026-11-01 tasks fragment has exactly these 121 live rows. HTTP rows change
from `proxied` to `delegated` only after native parity passes. Statuses are
exactly `proxied`, `delegated` or `dropped by decision #N`; assignment to a
future owner does not mark a row done. Every waiver-affected row cites its exact
#24963 ID in its owner note.

| Ledger row | Outcome owner |
| --- | --- |
| CLI gobby tasks | #21571; group dispatch; 5.5 contract |
| CLI gobby tasks backup | #21571; 3.11 and 5.5 contract |
| CLI gobby tasks blocked | #21571; 1.2 and 5.5 contract |
| CLI gobby tasks clean | dropped by decision #25252; 0.1 |
| CLI gobby tasks close | #21571; 3.7 and 5.5 contract |
| CLI gobby tasks commit | #21571; group dispatch; 5.5 contract |
| CLI gobby tasks commit auto | #21571; 3.9 and 5.5 contract |
| CLI gobby tasks commit link | #21571; 3.9 and 5.5 contract |
| CLI gobby tasks commit list | #21571; 3.9 and 5.5 contract |
| CLI gobby tasks commit unlink | #21571; 3.9 and 5.5 contract |
| CLI gobby tasks compact | dropped by decision #25252; 0.1 |
| CLI gobby tasks compact analyze | dropped by decision #25252; 0.1 |
| CLI gobby tasks compact apply | dropped by decision #25252; 0.1 |
| CLI gobby tasks compact stats | dropped by decision #25252; 0.1 |
| CLI gobby tasks create | #21571; 3.1 and 5.5 contract |
| CLI gobby tasks de-escalate | #21571; 3.6 and 5.5 contract |
| CLI gobby tasks delete | #21571; 3.1 and 5.5 contract |
| CLI gobby tasks dep | #21571; group dispatch; 5.5 contract |
| CLI gobby tasks dep add | #21571; 3.2 and 5.5 contract |
| CLI gobby tasks dep cycles | #21571; 1.2 and 5.5 contract |
| CLI gobby tasks dep remove | #21571; 3.2 and 5.5 contract |
| CLI gobby tasks dep tree | #21571; 1.2 and 5.5 contract |
| CLI gobby tasks diff | #21571; 1.3 and 5.5 contract |
| CLI gobby tasks doctor | #21571; 3.11 and 5.5 contract |
| CLI gobby tasks expand | #21581; expansion surface |
| CLI gobby tasks expand apply | #21581; consumes 3.1 write service |
| CLI gobby tasks expand compile | #21581; expansion surface |
| CLI gobby tasks expand reset | #21581; expansion surface |
| CLI gobby tasks expand resume | #21581; expansion surface |
| CLI gobby tasks expand status | #21581; expansion surface |
| CLI gobby tasks expand validate-plan | #21581; expansion surface |
| CLI gobby tasks label | #21571; group dispatch; 5.5 contract |
| CLI gobby tasks label add | #21571; 3.1 and 5.5 contract |
| CLI gobby tasks label remove | #21571; 3.1 and 5.5 contract |
| CLI gobby tasks list | #21571; 1.2 and 5.5 contract |
| CLI gobby tasks ready | #21571; 1.2 and 5.5 contract |
| CLI gobby tasks reindex | #21571; 3.11 and 5.5 contract |
| CLI gobby tasks reopen | #21571; 3.6 and 5.5 contract |
| CLI gobby tasks restore | #21571; 3.11 and 5.5 contract |
| CLI gobby tasks search | #21571; 1.2 and 5.5 contract |
| CLI gobby tasks show | #21571; 1.3 and 5.5 contract |
| CLI gobby tasks stats | #21571; 3.11 and 5.5 contract |
| CLI gobby tasks suggest | #21571; 1.3 and 5.5 contract |
| CLI gobby tasks update | #21571; 3.1 and 5.5 contract |
| CLI gobby tasks validation-history | #21571; 3.11 and 5.5 contract |
| HTTP DELETE /api/tasks/{task_id} | 4.1.5 |
| HTTP DELETE /api/tasks/{task_id}/comments/{comment_id} | 4.1.8 |
| HTTP DELETE /api/tasks/{task_id}/dependencies/{depends_on_id} | 4.1.11 |
| HTTP GET /api/tasks | 4.1.1 |
| HTTP GET /api/tasks/{task_id} | 4.1.3 |
| HTTP GET /api/tasks/{task_id}/comments | 4.1.6 |
| HTTP GET /api/tasks/{task_id}/dependencies | 4.1.9 |
| HTTP PATCH /api/tasks/{task_id} | 4.1.4 |
| HTTP POST /api/tasks | 4.1.2 |
| HTTP POST /api/tasks/{task_id}/claim | #21562; 4.1.12 native route, activation by #21562 (Q11) |
| HTTP POST /api/tasks/{task_id}/close | #24151; 4.1.14 native route, activation by #24151 (C1) |
| HTTP POST /api/tasks/{task_id}/comments | 4.1.7 |
| HTTP POST /api/tasks/{task_id}/de-escalate | 4.1.17 |
| HTTP POST /api/tasks/{task_id}/dependencies | 4.1.10 |
| HTTP POST /api/tasks/{task_id}/escalate | 4.1.15 |
| HTTP POST /api/tasks/{task_id}/release-claim | 4.1.13 |
| HTTP POST /api/tasks/{task_id}/reopen | 4.1.16 |
| MCP gobby-tasks-ops:append_description_section | #21570; 5.4.1 |
| MCP gobby-tasks-ops:cancel_expansion_run | #21581; expansion surface |
| MCP gobby-tasks-ops:check_expansion_qa_result | #21581; expansion surface |
| MCP gobby-tasks-ops:clear_isolation_pair | #21570; 5.4.2 |
| MCP gobby-tasks-ops:find_file_overlaps | #21570; 5.4.3 |
| MCP gobby-tasks-ops:get_affected_files | #21570; 5.4.4 |
| MCP gobby-tasks-ops:get_artifacts | #21570; 5.4.5 |
| MCP gobby-tasks-ops:get_expansion_run | #21581; expansion surface |
| MCP gobby-tasks-ops:get_latest_expansion_run | #21581; expansion surface |
| MCP gobby-tasks-ops:land_commit | #21570; 5.4.6 |
| MCP gobby-tasks-ops:reindex_tasks | #21570; 5.4.7 |
| MCP gobby-tasks-ops:reset_expansion_output | #21581; expansion surface |
| MCP gobby-tasks-ops:resume_expansion_run | #21581; expansion surface |
| MCP gobby-tasks-ops:run_expansion_qa_coverage | #21581; expansion surface |
| MCP gobby-tasks-ops:save_expansion_qa_result | #21581; expansion surface |
| MCP gobby-tasks-ops:set_affected_files | #21570; 5.4.8 |
| MCP gobby-tasks-ops:set_artifact | #21570; 5.4.9 |
| MCP gobby-tasks-ops:set_artifacts_atomic | #21570; 5.4.10 |
| MCP gobby-tasks-ops:set_landing_freeze | #21570; 5.4.11 |
| MCP gobby-tasks-ops:start_expansion_run | #21581; expansion surface |
| MCP gobby-tasks-ops:validate_expansion_run | #21581; expansion surface |
| MCP gobby-tasks-ops:validate_plan_file | #21581; expansion surface |
| MCP gobby-tasks-ops:wire_affected_files_from_run | #21581; consumes 3.3 affected-file service |
| MCP gobby-tasks:add_dependency | #21570; 5.3.6 |
| MCP gobby-tasks:add_label | #21570; 5.3.4 |
| MCP gobby-tasks:auto_link_commits | #21570; 5.3.10 |
| MCP gobby-tasks:backup_tasks | #21570; 5.3.12 |
| MCP gobby-tasks:check_dependency_cycles | #21570; 5.1.1 |
| MCP gobby-tasks:claim_task | #21570; 5.2.1 |
| MCP gobby-tasks:close_task | #21570; 5.2.9 |
| MCP gobby-tasks:create_task | #21570; 5.3.1 |
| MCP gobby-tasks:de_escalate_task | #21570; 5.2.7 |
| MCP gobby-tasks:delegate_task | #21570; 5.2.4 |
| MCP gobby-tasks:delete_task | #21570; 5.3.3 |
| MCP gobby-tasks:escalate_task | #21570; 5.2.6 |
| MCP gobby-tasks:get_dependency_tree | #21570; 5.1.2 |
| MCP gobby-tasks:get_session_tasks | #21570; 5.1.3 |
| MCP gobby-tasks:get_task | #21570; 5.1.4 |
| MCP gobby-tasks:get_task_diff | #21570; 5.1.5 |
| MCP gobby-tasks:get_task_sessions | #21570; 5.1.6 |
| MCP gobby-tasks:hand_off_task_claim | #21570; 5.2.3 |
| MCP gobby-tasks:link_commit | #21570; 5.3.8 |
| MCP gobby-tasks:link_task_to_session | #21570; 5.2.5 |
| MCP gobby-tasks:list_blocked_tasks | #21570; 5.1.7 |
| MCP gobby-tasks:list_attestations | #21570; 5.2.12 |
| MCP gobby-tasks:list_ready_tasks | #21570; 5.1.8 |
| MCP gobby-tasks:list_tasks | #21570; 5.1.9 |
| MCP gobby-tasks:record_attestation | #21570; 5.2.11 |
| MCP gobby-tasks:release_task_claim | #21570; 5.2.2 |
| MCP gobby-tasks:remove_dependency | #21570; 5.3.7 |
| MCP gobby-tasks:remove_label | #21570; 5.3.5 |
| MCP gobby-tasks:reopen_task | #21570; 5.2.8 |
| MCP gobby-tasks:repair_closed_candidate | #21570; 5.2.10 |
| MCP gobby-tasks:search_tasks | #21570; 5.1.10 |
| MCP gobby-tasks:suggest_next_task | #21570; 5.1.11 |
| MCP gobby-tasks:team_progress | #21567; re-homed, Tasks ships no descriptor (D) |
| MCP gobby-tasks:unlink_commit | #21570; 5.3.9 |
| MCP gobby-tasks:update_observed_files | #21570; 5.3.11 |
| MCP gobby-tasks:update_task | #21570; 5.3.2 |

## P0: Retire debt and freeze the family contract (depends: #25468)
`kind: framing`

R0 (#25468, "Worker boundary and frozen lifecycle contract") gates every Stage 2
family (`.gobby/plans/task-25460.md` Decision 2; ROADMAP decision 26), so every
deliverable in this phase, and through it the whole plan, waits on R0.
Planning proceeds now. This plan relies only on R0's validation criteria
(task-25460 section 1.1, row R0): a committed lifecycle inventory, a versioned
private command protocol with a Python facade, the unchanged Python runner, and
no claimed Rust runner completion. It names no protocol API.

### 0.1 Retire compaction and orphan-cleanup surfaces [category: code]
`kind: deliverable`

Targets:
- `src/gobby/cli/tasks/main.py::compact_cmd`
- `src/gobby/cli/tasks/main.py::compact_analyze`
- `src/gobby/cli/tasks/main.py::compact_apply`
- `src/gobby/cli/tasks/main.py::compact_stats`
- `src/gobby/cli/tasks/main.py::clean_cmd`
- `src/gobby/cli/tasks/main.py::doctor_cmd`
- `src/gobby/storage/compaction.py::*` — operation: delete — scope-reason: retire TaskCompactor, used only by the dropped CLI group and its tests
- `tests/storage/test_compaction.py::*` — operation: delete — scope-reason: tests of the retired TaskCompactor
- `src/gobby/utils/validation.py::TaskValidator.check_orphan_dependencies`
- `src/gobby/utils/validation.py::TaskValidator.clean_orphans`
- `src/gobby/utils/validation.py::TaskValidator.validate_all`
- `tests/utils/test_db_validation.py::test_orphan_dependencies`
- `tests/utils/test_input_validation.py::TestCheckOrphanDependencies`
- `tests/utils/test_input_validation.py::TestCleanOrphans`
- `tests/utils/test_input_validation.py::TestValidateAll`
- `tests/cli/test_tasks_cli.py::TestCompactAnalyzeCommand`
- `tests/cli/test_tasks_cli.py::TestCompactApplyCommand`
- `tests/cli/test_tasks_cli.py::TestCompactStatsCommand`
- `tests/cli/test_tasks_cli.py::TestDoctorCommand`
- `tests/cli/test_tasks_cli.py::TestCleanCommand`
- `docs/guides/cli-commands.md`
- `docs/reference-audit/tasks.json::*` — scope-reason: drop the compact and clean command entries and restate doctor's two checks
- `docs/contracts/parity-ledger-status.d/tasks.json::*` — scope-reason: mark the four compact rows and the clean row dropped by decision #25252
- `docs/contracts/parity-ledger.md`

**Research context:** Debt-first: these surfaces are removed before the corpus
freezes so no oracle, fixture or native code characterizes them (D1, D2, Q5).
`src/gobby/cli/tasks/main.py::compact_cmd` groups `compact_analyze`,
`compact_apply` and `compact_stats`, which construct
`src/gobby/storage/compaction.py::TaskCompactor`. `gcode grep -l "TaskCompactor" src tests -m 30`
finds only main.py, compaction.py, `tests/cli/test_tasks_cli.py` (the three
`TestCompact*Command` classes) and `tests/storage/test_compaction.py`. `clean_cmd`
calls `src/gobby/utils/validation.py::TaskValidator.clean_orphans`. `doctor_cmd`
prints three sections from `TaskValidator.validate_all` and ends with
"Run 'gobby tasks clean' to fix fixable issues." Orphan dependencies cannot
persist: the two `task_dependencies` foreign keys in
`crates/gcore/assets/schema/baseline.sql` are `ON DELETE CASCADE DEFERRABLE`.

Delete the compact group, its three subcommands, `clean_cmd`, `TaskCompactor`
and its test module. Remove `TaskValidator.check_orphan_dependencies` and
`TaskValidator.clean_orphans`; `validate_all` returns only `invalid_projects`
and `cycles`. `doctor_cmd` keeps those two sections and exit code 1 when either
finds issues, and drops the orphan section and the clean hint. Replace
`tests/utils/test_db_validation.py::test_orphan_dependencies` with a
`validate_all` shape test and remove the compact and clean CLI test classes.
In `tests/utils/test_input_validation.py`, retire `TestCheckOrphanDependencies`
and `TestCleanOrphans` and narrow `TestValidateAll` to the two keys.
`TestDoctorCommand` asserts the two remaining sections.

Set the five ledger rows to `dropped by decision #25252` with a note naming D1
or D2, then regenerate `docs/contracts/parity-ledger.md` with
`uv run python scripts/generate_parity_ledger.py`.
`scripts/generate_parity_ledger.py::render_ledger` raises for absent keys that
are not dropped, so the status edit and the command removal land together.
Remove the compact and clean entries from `docs/guides/cli-commands.md` and
`docs/reference-audit/tasks.json`; keep the doctor entries with the two-check
description. `docs/plans/completed/TASKS.md` is history and stays.
Observed: source inspection and the literal sweeps in Constraints.
Planned: the focused pytest files below, `uv run ruff check src/` and
`uv run mypy src/`.

**Acceptance:**
- 0.1.1 - `gobby tasks compact` and `gobby tasks clean` no longer exist and `gobby.storage.compaction` is not importable. test: `tests/cli/test_tasks_cli.py::test_retired_task_commands_are_absent`.
- 0.1.2 - `gobby tasks doctor` reports cycles and invalid projects, exits 1 when either is found, and prints no orphan section or clean hint. test: `tests/cli/test_tasks_cli.py::TestDoctorCommand.test_doctor_reports_cycles_and_invalid_projects`.
- 0.1.3 - `TaskValidator.validate_all` returns exactly `invalid_projects` and `cycles`. test: `tests/utils/test_db_validation.py::test_validate_all_reports_projects_and_cycles`.
- 0.1.4 - The five retired rows read `dropped by decision #25252` and the regenerated ledger matches the fragment. file: `docs/contracts/parity-ledger.md`.
- 0.1.5 - The CLI guide and reference audit list no retired command and describe doctor's two checks. file: `docs/guides/cli-commands.md`.

### 0.2 Characterize the HTTP, browser and event surface [category: test] (depends: #24963, #24970)
`kind: deliverable`

Targets:
- `tests/contracts/http/tasks.json`
- `tests/contracts/http/tasks-browser.json`
- `tests/contracts/http/tasks-events.json`
- `tests/contracts/http/manifest.json::*` — scope-reason: register the Tasks corpus with Python origin
- `tests/contracts/test_tasks_oracle.py`

**Research context:** The Python oracles are
`src/gobby/servers/routes/tasks.py::create_tasks_router` for the five task
routes, `src/gobby/servers/routes/tasks_comment_routes.py::register_task_comment_routes`,
`src/gobby/servers/routes/tasks_dependency_routes.py::register_task_dependency_routes`
and `src/gobby/servers/routes/tasks_lifecycle_routes.py::register_task_lifecycle_routes`
for the six lifecycle routes. The isolated
harness is `tests/contracts/http_corpus.py` with
`tests/contracts/test_http_corpus.py`; `tests/contracts/http/manifest.json`
already registers `families.tasks` as parity proxy with Python origin and holds
one case, `tests/contracts/http/tasks_list.json`, which stays.

Record every request field and the omitted versus null distinction, status,
body and the resulting rows for writes. Cover project scoping, ref forms
(`#N`, numeric, dotted path, UUID), 400 `ValueError` details, 404s, the safe 500
detail, delete cascade and unlink flags, the claim request's `session_id` and
`force`, close with and without `commit_sha`, close `reason` and `session_id`
(`src/gobby/servers/routes/tasks_lifecycle_routes.py::TaskCloseRequest` has no
`force`), a policy-refused close (the
`src/gobby/tasks/closure_snapshot.py::TaskClosurePolicyError` detail
`Task closure blocked: ...` returned as 400), and `closed_in_session_id`.
Normalize only generated IDs, clock fields and
durations with existing corpus mechanisms; never normalize statuses, errors,
ownership or links.

Events: the nested `_broadcast_task` helper in `create_tasks_router` emits
`task_created`, `task_updated`, `task_deleted`, `task_claimed`,
`task_claim_released`, `task_escalated`, `task_closed`, `task_reopened`,
`task_de_escalated` and `task_comment_added`; claim also notifies through
`src/gobby/servers/routes/tasks_assignment.py::TaskAssignmentNotifier`. Record
each route's event name, payload and its ordering relative to commit, plus the
`warnings` field a failed broadcast adds to the response.

Browser: record the ordinary and browser-testing `gobby_session` cookie classes
across all seventeen routes, including mutation effects, selector mismatches,
expiry or revocation, absent or invalid Authorization and missing credentials.
Pin the current credentialed CORS and preflight responses for allowed and
disallowed origins and the absence of any Tasks CSRF-token or Origin check.
Observed: source inspection only; these tests have not run.
Planned: isolated pytest of the oracle and corpus recording and replay.

**Acceptance:**
- 0.2.1 - All seventeen routes and their error branches are recorded with Python origin, rows and normalization limited to IDs and clocks. test: `tests/contracts/test_tasks_oracle.py::test_http_vectors_match_python`.
- 0.2.2 - Both cookie classes, credential failures and CORS policy are recorded for all seventeen routes without normalizing selectors or effects. test: `tests/contracts/test_tasks_oracle.py::test_browser_vectors_match_python`.
- 0.2.3 - Each mutating route's event name, payload, post-commit ordering, assignment notification and broadcast-failure warning are recorded. test: `tests/contracts/test_tasks_oracle.py::test_task_events_match_python`.
- 0.2.4 - The manifest registers the three new fixtures under `families.tasks` with Python origin beside the existing list case. file: `tests/contracts/http/manifest.json`.

### 0.3 Characterize services, authority, MCP and CLI [category: test] (depends: 0.1, 0.2)
`kind: deliverable`

Targets:
- `tests/contracts/tasks/services.json`
- `tests/contracts/tasks/authority.json`
- `tests/contracts/tasks/mcp.json`
- `tests/contracts/tasks/cli.json`
- `tests/contracts/test_tasks_oracle.py` — edit the file created by 0.2
- `docs/contracts/parity-waivers.json::*` — scope-reason: add the Tasks waiver inventory entries to the shared manifest

**Research context:** Service oracles are `src/gobby/storage/tasks/` (queries,
creation, updates, transitions, close, artifacts, lifecycle events, search) and
the operations below. MCP oracles are the registries in
`src/gobby/mcp_proxy/tools/tasks/`, `src/gobby/mcp_proxy/tools/task_dependencies.py::create_dependency_registry`,
`src/gobby/mcp_proxy/tools/task_commits.py` and
`src/gobby/mcp_proxy/tools/task_readiness.py::create_readiness_registry`. CLI
oracles are the click callbacks under `src/gobby/cli/tasks/`, for example
`src/gobby/cli/tasks/crud.py::list_tasks`, `src/gobby/cli/tasks/commits.py::diff_cmd`
and `src/gobby/cli/tasks/ai.py::suggest_cmd`.

Service vectors use a frozen clock and UUIDs and cover every read and write in
P1 and P3: list filters and views, readiness and blocking, search ranking,
dependency trees and cycles, creation with parent, blocks, depends_on, labels
and claim, partial updates, deletes, comments, artifacts, affected files and
overlaps, claim, release, hand-off, delegation and session links, escalation,
de-escalation and reopen, claim transfer, the close pre-policy refusals, close
finalization and ancestor auto-close, repair, attestations, commit links,
landing, backup, restore, doctor, stats, validation history and reindex. Write
vectors record the resulting task rows, `task_lifecycle_events` rows,
attestation rows and session-variable bookkeeping.

Closure vectors (C1): for every close and land vector, record the Python
`src/gobby/tasks/closure_policy.py::ClosureSnapshot` that
`src/gobby/tasks/closure_snapshot.py::prepare_closure` built (event, task,
caller, reason, attestations and facts), the `ClosureDecision` that
`evaluate_closure_policy` returned, and the resulting rows. Cover explicit,
ancestor and landing closure intents, a sessionless close resolved through the
`default` definition, and at least one allowed and one blocked decision per
bundled `task-closure` rule file. 3.7 and 3.10 replay these as snapshot parity
plus a scripted decision.

Authority vectors record, for each transition, the Python result for each caller
relationship: the owner; a delegation-lineage session
(`has_delegated_agent_run`); a local root session declaring `coordinate_tasks`
(`is_operator_coordinator`); a session declaring `manage_task_claims` inside and
outside its `task_claim_scope`; the holder declaring `release_own_claim`; an
epic-team seat (the team read 3.5 ports); a session declaring `land_tasks`
inside and outside its `task_land_scope`; a session declaring
`respect_task_protections` against a protected task; the operator's own
terminal session (`is_operator_session`); a foreign session; a call with no
session context; and the HTTP operator. They include the D5 foreign-project
UUID case on each candidate surface, `claim_task(force=True)` over a live owner
(D6), every gate-bearing label add and remove (D7), and the protection-policy
refusals.

MCP vectors record all 46 tool names, descriptions, briefs, input schemas,
definition hashes, read-only classification, valid results and validation
failures, plus an injected unexpected exception per tool. CLI vectors record
the 33 verbs' arguments, text and JSON output, exit codes and errors.

Add every entry from the waiver inventory in Debt dispositions and shared
conventions. Rule-based entries (D5) are added exactly when the recorded vector
shows the stated Python behaviour; the vector is kept
as the evidence. Original Python observations stay in the fixtures unchanged;
the manifest entry carries the required native replacement.
Observed: source inspection only. Planned: isolated pytest of the oracle.

**Granularity:** Four fixtures characterize one cross-language parity
interface; no implementation moves in this leaf.

**Acceptance:**
- 0.3.1 - Service vectors reproduce Python reads, writes, lifecycle effects, lifecycle-event rows and session-variable bookkeeping at a frozen instant. test: `tests/contracts/test_tasks_oracle.py::test_service_vectors_match_python`.
- 0.3.2 - Authority vectors record every transition for each enumerated caller relationship, including the D5, D6 and D7 Python observations. test: `tests/contracts/test_tasks_oracle.py::test_authority_vectors_match_python`.
- 0.3.3 - MCP vectors record catalog, schema, definition hash, classification, results, validation failures and unexpected-error text for all 46 tools. test: `tests/contracts/test_tasks_oracle.py::test_mcp_vectors_match_python`.
- 0.3.4 - CLI vectors record arguments, output, exit codes and errors for all 33 verbs. test: `tests/contracts/test_tasks_oracle.py::test_cli_vectors_match_python`.
- 0.3.5 - Every inventory entry exists with its exact ledger surface, behaviour, decision and replacement, and each rule-based entry cites its recorded vector. file: `docs/contracts/parity-waivers.json`.

## P1: Native read path
`kind: framing`

### 1.1 Family crate, row model and scoped references [category: code] (depends: 0.3, #21559)
`kind: deliverable`

Targets:
- `Cargo.toml`
- `Cargo.lock`
- `crates/gtasks/Cargo.toml`
- `crates/gtasks/src/lib.rs`
- `crates/gtasks/src/model.rs`
- `crates/gtasks/src/refs.rs`
- `crates/gtasks/tests/refs.rs`
- `crates/gcode/Cargo.toml`
- `crates/gdaemon/Cargo.toml`
- `crates/ghook/Cargo.toml`
- `src/gobby/install/version_pins.py::*` — scope-reason: release pins for the root workspace member change

**Research context:** Create `crates/gtasks` from the `crates/gruntime`
template: package `gobby-tasks`, lib `gobby_tasks`, `publish = false`, version
`0.5.0`, edition, rust-version, license and repository inherited from
`[workspace.package]`, and `gobby-core` with features `postgres`,
`postgres-pool` and `route-family`. Its other dependencies mirror gruntime's
direct ones (`tokio-postgres`, `serde`, `serde_json`, `uuid`, `thiserror`,
`tokio`), with `tokio`'s `process` and `time` features for the git port; 4.1
adds `axum` and 5.1 adds `schemars`. Add it to the root `members` list.

The row model mirrors `src/gobby/storage/tasks/_models.py::Task` and its
`to_dict` and `to_brief` projections field for field, using 0.3's vectors as the
oracle. Reference resolution mirrors `src/gobby/storage/tasks/_id.py::resolve_task_reference`
(`#N`, numeric seq, dotted path, UUID within a project) and
`src/gobby/tasks/lifecycle_access.py::resolve_lifecycle_task`
(`<project>#N` and foreign refs only for a local coordinator). `refs.rs`
exposes two entry points: `resolve_read` keeps Python's read behaviour, and
`resolve_mutation` applies D5's single policy. Missing refs map to #24970
`missing`; a foreign ref without coordinator standing maps to `forbidden`, with
the same message `CrossProjectTaskForbidden` carries.

The root manifest change alters every binary's workspace inputs, so this leaf
bumps gcode, gdaemon and ghook by one patch and updates the pins.
Planned: `CARGO_BUILD_JOBS=3 cargo nextest run -p gobby-tasks --test refs`.

**Acceptance:**
- 1.1.1 - Task rows round-trip from PostgreSQL to the `to_dict` and `to_brief` shapes in 0.3's vectors. test: `crates/gtasks/tests/refs.rs::row_projections_match_python`.
- 1.1.2 - Read resolution matches Python for every ref form; mutation resolution refuses foreign refs except for a local coordinator, with the D5 entries asserted. test: `crates/gtasks/tests/refs.rs::reference_resolution_matches_policy`.
- 1.1.3 - gcode, gdaemon and ghook receive this leaf's patch bump, lock and pins. test: `tests/install/test_version_pins.py::test_managed_bin_pins_match_crate_versions`.

### 1.2 Listing, readiness, search and dependency reads [category: code] (depends: 1.1)
`kind: deliverable`

Targets:
- `crates/gtasks/src/query.rs`
- `crates/gtasks/src/search.rs`
- `crates/gtasks/src/graph.rs`
- `crates/gtasks/src/lib.rs` — edit the file created by 1.1
- `crates/gtasks/tests/reads.rs`

**Research context:** Oracles: `src/gobby/storage/tasks/_queries.py::list_tasks`,
`src/gobby/storage/tasks/_queries.py::list_ready_tasks`,
`src/gobby/storage/tasks/_queries.py::list_blocked_tasks`, the MCP projections
in `src/gobby/mcp_proxy/tools/tasks/_listing.py` (default and compact views,
`refs`, `updated_since`, `state`, `candidate`, `next_offset`) and
`src/gobby/mcp_proxy/tools/task_readiness.py::create_readiness_registry`.
Search uses the pg_search BM25 backend chosen by
`src/gobby/search/keyword.py::pick_search_backend` through
`src/gobby/storage/tasks/_search.py::TaskSearchBackend`; native issues the same
BM25 query and returns the same scores and `min_score` filtering. Dependency
reads mirror `src/gobby/mcp_proxy/tools/task_dependencies.py::create_dependency_registry`
(`get_dependency_tree` with `direction`, `check_dependency_cycles`) and the CLI
`dep tree` and `dep cycles` renderings. Every query is read-only and
project-scoped with the explicit `all_projects` and `project` selections.
Planned: `CARGO_BUILD_JOBS=3 cargo nextest run -p gobby-tasks --test reads`.

**Acceptance:**
- 1.2.1 - Listing filters, sort orders, default and compact views and pagination match 0.3's vectors. test: `crates/gtasks/tests/reads.rs::list_matches_python`.
- 1.2.2 - Ready and blocked sets match Python, including parent scoping. test: `crates/gtasks/tests/reads.rs::readiness_matches_python`.
- 1.2.3 - BM25 search returns Python's ranking, scores and filters. test: `crates/gtasks/tests/reads.rs::search_matches_python`.
- 1.2.4 - Dependency trees in each direction and cycle reports match Python. test: `crates/gtasks/tests/reads.rs::dependency_graph_matches_python`.

### 1.3 Task detail, links and suggestion reads [category: code] (depends: 1.2)
`kind: deliverable`

Targets:
- `crates/gtasks/src/detail.rs`
- `crates/gtasks/src/git.rs`
- `crates/gtasks/src/suggest.rs`
- `crates/gtasks/src/lib.rs` — edit the file created by 1.1
- `crates/gtasks/tests/detail.rs`

**Research context:** Oracles: `get_task` (actionable card and `brief=false`)
in `src/gobby/mcp_proxy/tools/tasks/_crud.py`, `get_task_sessions` and
`get_session_tasks` in `src/gobby/mcp_proxy/tools/tasks/_session.py`, the
comment list in `src/gobby/servers/routes/tasks_comment_routes.py::register_task_comment_routes`,
`get_artifacts` in `src/gobby/mcp_proxy/tools/tasks/_artifacts.py`,
`get_affected_files` and `find_file_overlaps` in
`src/gobby/mcp_proxy/tools/tasks/_affected_files.py`,
`src/gobby/mcp_proxy/tools/task_commits.py::get_task_diff_tool`, `suggest_next_task`
in `create_readiness_registry` (proximity boost and scoring) and
`src/gobby/cli/tasks/ai.py::suggest_cmd`.

`git.rs` defines the one checkout-git port the family uses for diffs, commit
verification and close evidence. Its one implementation runs read-only git at
the checkout path stored for the project, on the hub, exactly as Python does,
so a path the hub cannot open fails the way Python's git call fails. Node-hosted
checkouts are deferred to #21546 (3.8, ruling A); the port stays so #21546 can
add a node implementation without changing callers. Diff output matches Python
byte for byte.
Planned: `CARGO_BUILD_JOBS=3 cargo nextest run -p gobby-tasks --test detail`.

**Acceptance:**
- 1.3.1 - Task cards, full records, session links and comment lists match Python. test: `crates/gtasks/tests/detail.rs::task_detail_matches_python`.
- 1.3.2 - Artifacts, affected files and file overlaps match Python. test: `crates/gtasks/tests/detail.rs::files_and_artifacts_match_python`.
- 1.3.3 - Task diffs match Python, including a stored checkout path the hub cannot open, through the git port. test: `crates/gtasks/tests/detail.rs::task_diff_matches_python`.
- 1.3.4 - Suggestions match Python's ordering and proximity scoring. test: `crates/gtasks/tests/detail.rs::suggestion_matches_python`.

## P2: Hub authority matrix (#20822)
`kind: framing`

### 2.1 Caller principals and the transition table [category: code] (depends: 1.3)
`kind: deliverable`

Targets:
- `crates/gtasks/src/principal.rs`
- `crates/gtasks/src/authority.rs`
- `crates/gtasks/src/labels.rs`
- `crates/gtasks/src/lib.rs` — edit the file created by 1.1
- `crates/gtasks/tests/authority.rs`
- `docs/contracts/task-authority.md`

**Research context:** One service-level check guards every native mutation, so
HTTP, MCP and CLI lanes share one contract (D8). The audit
`docs/design/task-authority-audit.md` is the design input; Q4, Q8 and Q9 are the
rulings.

Principals, resolved once per call from authenticated context, never from
request fields:
- `Operator`: an HTTP request carrying `x-gobby-key-id`
  (`crates/gruntime/src/lib.rs::Caller`), a verified browser cookie after 4.4,
  and #21571's CLI (Q12).
- `Session`: an MCP call whose `crates/gcore/src/mcp.rs::ToolContext` carries a
  `session_id`, with `project_id`, `agent_run_id` and `machine_id`. Its
  authority comes from the installed definition that the reserved `_agent_type`
  variable resolves to, read the way
  `src/gobby/workflows/agent_resolver.py::session_agent_definition` reads it:
  the declared `capabilities` and the `task_claim_scope`, `task_land_scope` and
  `task_close_scope` fields of
  `src/gobby/workflows/agent_models.py::AgentDefinitionBody`. No code path
  compares a definition name (rule 16); `default` is the only name gtasks
  resolves, for a sessionless or operator close (3.7).
- `Daemon`: in-process callers such as live-session recovery and 3.7's
  terminal-epic archive hook. Expansion apply is not `Daemon`: it passes the
  run's initiating session (Q13d).
- `Anonymous`: an MCP call with no session context.

A call over `POST /api/mcp/{server}/tools/{tool}` is `Session` when its
`ToolContext` carries a `session_id`. Otherwise it is `Operator` only when the
front door verified an operator key, which is how #21571's CLI calls. A call
with neither is `Anonymous`.

Relationships of a `Session` to a task port the Python predicates exactly:
- owner: `claimed_by_session_id` equals the caller;
- delegation lineage:
  `src/gobby/mcp_proxy/tools/tasks/_authorization.py::has_delegated_agent_run`;
- coordinator: a local root session (no agent run, depth 0, this machine) whose
  definition declares `coordinate_tasks`
  (`src/gobby/tasks/lifecycle_access.py::is_operator_coordinator`);
- claim manager: a session whose definition declares `manage_task_claims` and
  whose `task_claim_scope` admits the task
  (`src/gobby/mcp_proxy/tools/tasks/_claim_release.py::_may_manage_claim`,
  `_can_release`, and the root-session form
  `src/gobby/mcp_proxy/tools/tasks/_authorization.py::can_manage_task_claims`);
- own-claim releaser: the holder whose definition declares `release_own_claim`
  within its `task_claim_scope`
  (`src/gobby/mcp_proxy/tools/tasks/_claim_release.py::_may_release_own_claim`);
- lander: a definition declaring `land_tasks` within `task_land_scope`
  (`src/gobby/tasks/land_commit.py::_manages_task`);
- protection-bound: a definition declaring `respect_task_protections`, which
  2.2's policy applies to;
- foreign, and unclaimed.

Scope checks port `src/gobby/tasks/lifecycle_access.py::task_scope_refusal`
(`task_types`, `categories`, `own_claim_only`, `assigned_only`). Close-only
declarations (`task_close_scope`, `task_close_reasons`, `close_without_work`,
`direct_integration_commit`, `task_attestation_writes`,
`task_attestation_scope`) are read by 3.7 and 3.10, not by this table. A
supplied session reference that does not resolve is refused as validation;
there is no fallback actor.

`authority.rs` holds a static table with one row per mutation: create, field
update, reparent, delete, label add, label remove, dependency add and remove,
affected files, artifacts, description section, isolation pair, comment add and
delete, claim, takeover, release, hand-off, delegate, session link, escalate,
de-escalate, reopen, close, candidate repair, attestation record, attestation
list, claim transfer, commit link, unlink and auto-link, observed files, land,
landing freeze, backup, restore and reindex. It also has rows for the operator-only doctor, stats and
validation-history reads (Q12). Each row lists its allowed principals and relationships and its
daemon-verifiable preconditions. A pair not in the table is refused as
`forbidden`. Rows reproduce today's checks: `require_claim_authority` guards
`link_commit`, `unlink_commit`, `set_affected_files`, the artifact setters,
`delete_task`, `add_label`, `remove_label`, `reopen_task`, `escalate_task`,
`de_escalate_task` and `link_task_to_session`, admitting the owner, delegation
lineage and the root-session claim manager. `update_task` has no claim check
today, and its row keeps that. `land_commit` keeps the lander relationship from
`src/gobby/tasks/land_commit.py::land_candidate`. Attestation record and list
resolve the task through `src/gobby/tasks/lifecycle_access.py::resolve_lifecycle_task`
and admit writers by `task_attestation_writes` and `task_attestation_scope`
(`src/gobby/tasks/attestations.py::record_attestation`); their rows carry no
claim check, as Python does. Restore stays operator-only
(#24797), and sessions keep `backup_tasks` to the default path (Q13c). The four
Q12 tools (`doctor_tasks`, `get_task_stats`, `get_validation_history` and
`restore_tasks`) are operator-only.

Two rows change today's checks:
- Dependency add and remove, and each task named in `create_task`'s `blocks`,
  need claim authority over the task whose readiness changes (Q13a, D9). That
  is the owner, delegation lineage, the coordinator, the claim manager,
  `Operator` or `Daemon`. An unclaimed task stays open to sessions in its
  project. For `create_task`, a refused `blocks` entry refuses the whole
  create.
- Auto-link with a `task_id` needs claim authority over that task. A scan
  without one is limited to `Operator`, the coordinator, the claim manager and
  `Daemon` (Q13b, D10).

`Operator` is allowed
on every HTTP transition with its existing HTTP semantics, so HTTP results do
not change. `Anonymous` is allowed exactly where Python allows a call without
session context: unclaimed tasks, never a claimed one.

Exceptional transitions record their ground in the existing
`task_lifecycle_events` table (`from_state`, `to_state`, `reason`, `by_actor`),
with `reason` set to `<transition>:<ground>:<evidence>` and `by_actor` set to
the principal. Grounds are `owner_dead`, `delegated`, `coordinator` and
`operator` (Q8); 3.5 uses them for takeover. The evidence is what the daemon
verified. For `owner_dead`, it is the owner session id and its status. For
`delegated`, it is the agent run id. For `coordinator`, it is the caller's
session id and the declared capability that admitted it (`coordinate_tasks` or
`manage_task_claims`) with the definition content digest. For `operator`, it
is the key id. The free-text `reason`
column carries it, so the schema does not change.

The table settles every #20822 item:
- Item 1 (takeover): Q8 grounds plus the evidence above.
- Items 2 and 7 (stage and registry tools): those tools left with #23341.
- Item 3 (expansion runtime): moves to #21581 (Q13d).
- Item 4 (backup and restore): Q13c.
- Item 5 (coordinator-lane tools): only `auto_link_commits` survives, under
  Q13b.
- Item 6 (dependency edges): Q13a.
- Item 8 (resolution and lanes): D5 and D8.
- Item 9 (gate-bearing labels): Q9.
- Item 10 (attribution): already enforced. `release_task_claim` and
  `hand_off_task_claim` refuse while task-attributed files are dirty
  (`src/gobby/mcp_proxy/tools/tasks/_claim_release.py::_uncommitted_refusal`),
  and 3.5 ports that refusal.

`labels.rs` applies Q9. The gate-bearing set is `tdd:required`,
`needs-decision`, `needs-planning`, `clean-window`, `live-session`,
`deferred-by-josh` and `security-review`. An add follows the row's ordinary
authority; a removal needs `Operator`, the coordinator or `Daemon`;
`deferred-by-josh` add and remove need `Operator`. `live-session`
additionally keeps `src/gobby/mcp_proxy/tools/tasks/_live_session_label.py::live_session_label_change_error`
(terminal, non-spawned session with live-work loaded). The check applies to
`create_task` labels, `update_task` label replacement, `add_label` and
`remove_label`, under the four D7 entries.

`docs/contracts/task-authority.md` publishes the principal list, the full table
and the grounds, and replaces the audit's proposal as the contract of record.
Planned: `CARGO_BUILD_JOBS=3 cargo nextest run -p gobby-tasks --test authority`.

**Granularity:** Seven items prove one static table and the principal resolver
that feeds it. Splitting them would ship a table with unproven rows.

**Acceptance:**
- 2.1.1 - Every transition reproduces 0.3's Python result for every caller relationship 0.3 enumerates, except the cited D5, D6, D7, D9 and D10 replacements. test: `crates/gtasks/tests/authority.rs::table_matches_python_relationships`.
- 2.1.2 - Unlisted principal and transition pairs, unresolvable session references and claimed tasks without session context are refused with no write. test: `crates/gtasks/tests/authority.rs::unlisted_pairs_fail_closed`.
- 2.1.3 - Gate-bearing label adds and removals follow Q9 for every principal, with `live-session` keeping its extra guard. test: `crates/gtasks/tests/authority.rs::gate_labels_follow_q9`.
- 2.1.4 - Exceptional transitions write one `task_lifecycle_events` row naming the transition, ground, verified evidence and principal. test: `crates/gtasks/tests/authority.rs::grounds_are_recorded`.
- 2.1.5 - The authority contract documents principals, the full table, grounds, label rules and the disposition of every #20822 item. file: `docs/contracts/task-authority.md`.
- 2.1.6 - Dependency add and remove, `create_task` `blocks`, and auto-link with and without a `task_id` follow Q13a and Q13b for every relationship, and each refusal writes nothing. test: `crates/gtasks/tests/authority.rs::readiness_and_auto_link_authority`.
- 2.1.7 - A tool-route call resolves to `Session` with a session context, to `Operator` only with a verified operator key, and to `Anonymous` otherwise, and the restore, doctor, stats and validation-history rows admit only `Operator`. test: `crates/gtasks/tests/authority.rs::tool_route_principals`.

### 2.2 Task protection policy, ported native [category: code] (depends: 2.1)
`kind: deliverable`

Targets:
- `crates/gtasks/src/policy.rs`
- `crates/gtasks/src/lib.rs` — edit the file created by 1.1
- `crates/gtasks/tests/policy.rs`

**Research context:** D3 and Q5, as superseded by #25228 (Decision Record,
2026-11-01, ruling B). Python already reads protection from project
configuration, so this leaf ports it and edits no Python, template or policy
YAML. The policy is the `task_protection_policy` object in each project's
`.gobby/project.json`: `foreign_claims` (default true), `labels` and
`subtree_roots`. `src/gobby/config/tasks.py::TaskProtectionPolicyConfig`
forbids extra keys and normalizes each root to `#N` or a canonical UUID.
`src/gobby/config/tasks.py::load_project_task_protection_policy` reads the
marker under the project's primary checkout root. A missing marker gives the
default policy, and a malformed policy raises `ValueError`. The root comes from
`src/gobby/storage/project_checkouts.py::require_root` for the hub machine.

`src/gobby/tasks/lifecycle_access.py::task_protection_refusal` walks the task
and every ancestor, loading each node's own project policy once and giving the
projects in `src/gobby/storage/projects.py::CHECKOUT_FREE_PROJECT_IDS` the
default policy. It refuses on the first node that has a foreign claim (when
`foreign_claims` is set), carries a protected label, or is a listed subtree
root by `#N` or id, and names the task or ancestor in its message. Only
definitions that declare `respect_task_protections` are subject to it:
- `src/gobby/mcp_proxy/tools/tasks/_authorization.py::task_mutation_error`
  applies it to `update_task` for the task and for a proposed new parent. No
  session context passes. A missing session or an invalid or disabled
  definition refuses. An operator terminal session
  (`src/gobby/workflows/agent_resolver.py::is_operator_session`) passes, and so
  does a definition without the opt-in. A policy read error refuses with
  `Task protection policy unavailable: ...`.
- `src/gobby/mcp_proxy/tools/tasks/_lifecycle_close.py::_evaluate_close` refuses
  a close with `task_protection_policy_unavailable` or `task_protected` in the
  same position.

`policy.rs` ports the loader, the normalization, the ancestor walk and both
refusal shapes with Python's message text, and 2.1's table calls it for the
`update_task`, reparent and close rows. The oracles are the 0.3 authority
vectors and the protection cases in
`tests/mcp_proxy/tools/tasks/test_close_authority.py`, for example
`test_ancestor_uses_its_own_project_protection_policy` and
`test_invalid_project_protection_refuses_mutation`. The native tests write a
temporary `.gobby/project.json` per project, as those tests do.
Planned: `CARGO_BUILD_JOBS=3 cargo nextest run -p gobby-tasks --test policy`.

**Acceptance:**
- 2.2.1 - Native protection refuses exactly where Python does: a foreign claim, a protected label or a listed subtree root on the task or any ancestor, each checked under its own project's policy, with Python's messages. test: `crates/gtasks/tests/policy.rs::protection_matches_python`.
- 2.2.2 - Only definitions declaring `respect_task_protections` are checked. No session context and operator terminal sessions pass, and a missing session or an invalid definition refuses. test: `crates/gtasks/tests/policy.rs::protection_follows_declared_opt_in`.
- 2.2.3 - A missing marker yields the default policy, checkout-free projects use the default, and a malformed policy refuses the mutation or close as unavailable with no write. test: `crates/gtasks/tests/policy.rs::policy_loading_matches_python`.

## P3: Write path and lifecycle (#21561)
`kind: framing`

### 3.1 Task writes and the expansion write service [category: code] (depends: 2.2, #25262)
`kind: deliverable`

Targets:
- `crates/gtasks/src/write.rs`
- `crates/gtasks/src/locks.rs`
- `crates/gtasks/src/lib.rs` — edit the file created by 1.1
- `crates/gtasks/tests/write.rs`
- `docs/contracts/tasks-services.md`

**Research context:** Oracles: `src/gobby/storage/tasks/_creation.py::_create_task_in_transaction`
(seq allocation under `TaskSeqAllocation`, parent lock in `_lock_open_parent`,
`blocks` and `depends_on`, labels, claim on create), the update facade in
`src/gobby/storage/tasks/_update_facade.py` and `src/gobby/storage/tasks/_updates.py`,
the MCP `create_task` and `update_task` in `src/gobby/mcp_proxy/tools/tasks/_crud.py`,
`src/gobby/mcp_proxy/tools/tasks/_lifecycle_delete.py` (`cascade` and `unlink`),
`src/gobby/mcp_proxy/tools/tasks/_lifecycle_labels.py` and
`append_description_section` in `src/gobby/mcp_proxy/tools/tasks/_artifacts.py`.
Partial updates use Absent, Clear and Set; `escalation_reason`, dates and
`assigned_agent` keep Python's nullability from 0.3.

`locks.rs` maps each write to
`crates/gcore/src/postgres_pool/transaction.rs::LockTarget` with the Python key
strings and priorities listed in Constraints. #25262 (closed) gave every Python
class those explicit keys in `src/gobby/storage/hub/postgres_pool.py::advisory_lock_keys`;
the heading edge records that dependency and is already satisfied.

The write service is the boundary #21581 consumes. Its contract, documented in
`docs/contracts/tasks-services.md`, is one transaction that takes
`ExpansionApplyMutation` on the parent and `TaskSeqAllocation` on the project,
then creates each task, sets its labels, sets affected files with source
`expansion`, adds `blocks` dependencies, and sets the `target_branch` and
`expansion_run_id` artifacts. That is the same sequence
`src/gobby/tasks/expansion/_apply.py::apply_run` performs today. The service
takes the caller's principal and checks 2.1's table for each write. Expansion
apply passes the run's initiating session, never `Daemon` (Q13d), so an
auto-apply under a foreign live claim is refused with no write. #21581 owns
the expansion run's cancel and resume scoping. Creating tasks, edges, labels,
affected files and artifacts outside expansion uses the same functions with
the caller's principal. `create_task`'s `blocks` applies Q13a to each blocked
task before any write.
Planned: `CARGO_BUILD_JOBS=3 cargo nextest run -p gobby-tasks --test write`.

**Acceptance:**
- 3.1.1 - Create, update, reparent, delete with cascade and unlink, labels and description sections match 0.3's rows and errors. test: `crates/gtasks/tests/write.rs::task_writes_match_python`.
- 3.1.2 - Native writes take Python's lock keys in priority order and serialize against a concurrent Python writer. test: `crates/gtasks/tests/write.rs::writes_share_python_locks`.
- 3.1.3 - The expansion write service creates a tree with labels, affected files, edges and artifacts in one transaction and rolls back whole on failure. test: `crates/gtasks/tests/write.rs::expansion_write_service_is_atomic`.
- 3.1.4 - The write-service contract names the operations, locks, the caller-principal parameter and transaction boundary #21581 consumes, and states that expansion apply passes the initiating session. file: `docs/contracts/tasks-services.md`.
- 3.1.5 - An expansion apply whose initiating session lacks authority over a foreign live-claimed parent, and a `create_task` whose `blocks` names a task the caller cannot change, are refused with no task written. test: `crates/gtasks/tests/write.rs::write_service_checks_caller_principal`.

### 3.2 Dependency edges [category: code] (depends: 3.1, #25262)
`kind: deliverable`

Targets:
- `crates/gtasks/src/deps.rs`
- `crates/gtasks/src/lib.rs` — edit the file created by 1.1
- `crates/gtasks/tests/deps.rs`

**Research context:** Oracles: `add_dependency` and `remove_dependency` in
`src/gobby/mcp_proxy/tools/task_dependencies.py::create_dependency_registry`
(`dep_type`), the HTTP dependency routes in
`src/gobby/servers/routes/tasks_dependency_routes.py::register_task_dependency_routes`
and the CLI callbacks `src/gobby/cli/tasks/deps.py::dep_add` and
`src/gobby/cli/tasks/deps.py::dep_remove`. Edges are written under the
`TaskDependencyMutation` lock with its global `task_dependency_mutation` key
(explicit since #25262), and the cycle check
from 1.2 runs inside the same transaction. Self-edges and duplicate edges keep
Python's errors. Before any write, each add or remove checks 2.1's Q13a row
against the task whose readiness changes (`task_id`). The MCP surfaces assert
their D9 entries. HTTP and CLI callers are `Operator`, so their results do not
change.
Planned: `CARGO_BUILD_JOBS=3 cargo nextest run -p gobby-tasks --test deps`.

**Acceptance:**
- 3.2.1 - Edge adds and removes match 0.3's rows and errors on every lane. test: `crates/gtasks/tests/deps.rs::dependency_edges_match_python`.
- 3.2.2 - An edge that would close a cycle is refused inside the locked transaction, including under a concurrent Python writer. test: `crates/gtasks/tests/deps.rs::cycles_refused_under_lock`.
- 3.2.3 - A session without claim authority over a claimed `task_id` cannot add or remove its edges, writes nothing, and gets the D9 refusal; an unclaimed task stays open. test: `crates/gtasks/tests/deps.rs::edge_readiness_authority`.

### 3.3 Affected files, artifacts and isolation pairs [category: code] (depends: 3.2)
`kind: deliverable`

Targets:
- `crates/gtasks/src/files.rs`
- `crates/gtasks/src/artifacts.rs`
- `crates/gtasks/src/lib.rs` — edit the file created by 1.1
- `crates/gtasks/tests/files.rs`

**Research context:** Oracles: `set_affected_files` in
`src/gobby/mcp_proxy/tools/tasks/_affected_files.py` (source tags, claim
authority), `set_artifact`, `set_artifacts_atomic` and `clear_isolation_pair`
in `src/gobby/mcp_proxy/tools/tasks/_artifacts.py`, and the storage in
`src/gobby/storage/tasks/_artifacts.py`. `set_artifacts_atomic` writes all
fields or none. The affected-file service is the one #21581's
`wire_affected_files_from_run` will call.
Planned: `CARGO_BUILD_JOBS=3 cargo nextest run -p gobby-tasks --test files`.

**Acceptance:**
- 3.3.1 - Affected-file writes with each source tag match Python. test: `crates/gtasks/tests/files.rs::affected_files_match_python`.
- 3.3.2 - Single and atomic artifact writes and isolation-pair clears match Python, and an atomic write with one invalid field writes nothing. test: `crates/gtasks/tests/files.rs::artifacts_match_python`.

### 3.4 Comments [category: code] (depends: 3.3)
`kind: deliverable`

Targets:
- `crates/gtasks/src/comments.rs`
- `crates/gtasks/src/lib.rs` — edit the file created by 1.1
- `crates/gtasks/tests/comments.rs`

**Research context:** Oracle: `src/gobby/servers/routes/tasks_comment_routes.py::register_task_comment_routes`
(list, add, remove) over the `task_comments` table. Comments have no MCP or CLI
surface. Adds and deletes keep Python's author fields, ordering and 404s.
Evidence rows share `task_comments`. Python's add refuses with 403 any comment
whose author type is a close receipt, `task_attestation`
(`src/gobby/tasks/attestations.py::AUTHOR_TYPE`), the retired `task_step`, or
`system` with a `migration:` author, and its delete leaves those rows in place
with a 403 naming the kind; native keeps both refusals and their details. 3.7
and 3.10 own the writes of those rows.
Planned: `CARGO_BUILD_JOBS=3 cargo nextest run -p gobby-tasks --test comments`.

**Acceptance:**
- 3.4.1 - Comment adds, lists and deletes match 0.3's rows and errors, including the reserved-author refusals on add and delete. test: `crates/gtasks/tests/comments.rs::comments_match_python`.

### 3.4a Task attestations [category: code] (depends: 3.4)
`kind: deliverable`

Targets:
- `crates/gtasks/src/attestations.rs`
- `crates/gtasks/src/lib.rs` — edit the file created by 1.1
- `crates/gtasks/tests/attestations.rs`

**Research context:** Attestations replaced the four step tools (#25449,
ENH-STALENESS-1). Oracles: `src/gobby/tasks/attestations.py::record_attestation`,
`list_attestations`, `attestations_by_task`, `restore_attestations` and
`validate_attestation_history`, with the models `AttestationSubmission`,
`EvidenceReference` and `TaskAttestation`, and the MCP resolver in
`src/gobby/mcp_proxy/tools/tasks/_attestations.py::create_attestation_registry`.

`record_attestation` locks the task `FOR UPDATE`, requires a registered session
in the task's project with an enabled installed definition
(`session_agent_definition`), returns the existing record unchanged for a
repeated `idempotency_key` from the same session with the same content (and
refuses different content under that key), requires a writer grant in
`task_attestation_writes` (`"*"` or the kind) and a `task_attestation_scope`
that admits the task, refuses a closed task, and lets only the original author
supersede a record of the same kind, once. It stores the record in
`task_comments` with author type `task_attestation`, the next `sequence`, the
definition name, content digest and capabilities, and bumps `tasks.updated_at`
in the same transaction. No kind, result or reference scheme has special meaning
here; close and landing rules give them meaning (3.7, 3.10). The MCP resolver
requires an authenticated project session and resolves the task through
`resolve_lifecycle_task`. `restore_attestations` appends trusted backup history
after `validate_attestation_history` and returns `extended`, `unchanged`,
`ahead` or `diverged` without replacing stored evidence; 3.11 consumes it.
3.7's snapshot carries the full list; superseded records are filtered by the
policy evaluator, which stays with #24151 (C1).
Planned: `CARGO_BUILD_JOBS=3 cargo nextest run -p gobby-tasks --test attestations`.

**Acceptance:**
- 3.4a.1 - Recording matches 0.3's rows and refusals for writer grants, scope, closed tasks, idempotency keys, supersession by the original author only, sequence and the `updated_at` bump, and a refusal writes nothing. test: `crates/gtasks/tests/attestations.rs::record_attestation_matches_python`.
- 3.4a.2 - Listing returns Python's records in sequence order, and history validation and restore append or classify a backup's history as `extended`, `unchanged`, `ahead` or `diverged` exactly as Python does. test: `crates/gtasks/tests/attestations.rs::attestation_history_matches_python`.

### 3.5 Claims, takeover grounds and session links [category: code] (depends: 3.4a, #25262)
`kind: deliverable`

Targets:
- `crates/gtasks/src/claim.rs`
- `crates/gtasks/src/session_vars.rs`
- `crates/gtasks/src/lib.rs` — edit the file created by 1.1
- `crates/gtasks/tests/claim.rs`

**Research context:** Oracles: `claim_task` in
`src/gobby/mcp_proxy/tools/tasks/_lifecycle_claim.py` (cross-project refusal,
`confirm_claiming_session_activity`, closed refusal, the epic-team seat rule via
`epic_team_above` and `seat_team`, delegated claims with `expected_owner`,
`record_claimant_state`); `src/gobby/storage/tasks/_transitions.py::claim_task_for_agent`;
`release_task_claim` and `hand_off_task_claim` in
`src/gobby/mcp_proxy/tools/tasks/_claim_release.py` (`_can_release`,
`_may_manage_claim`, `_may_release_own_claim`, `_uncommitted_refusal`), and its
`transfer_task_claim`, which moves a claim as one locked transfer: it takes
`AgentTaskClaimMutation` on the recipient, rechecks the holder, the caller's
claim-manager authority and the recipient's project and machine, takes
`SessionVariableTransfer` over both sessions, applies the dirty-file refusal,
reclaims with `expected_owner`, and records a `claim:handed_off` lifecycle event,
returning `ClaimTransfer` (task and integer `event_id`) or raising
`ClaimTransferRefused` with no partial move; `delegate_task` and
`link_task_to_session` in `src/gobby/mcp_proxy/tools/tasks/_session.py`;
`src/gobby/storage/tasks/_delegation.py`; the HTTP claim and release-claim
routes; and `src/gobby/servers/routes/tasks_assignment.py::TaskAssignmentNotifier`.
The claim service returns the assigned session and the task card; the HTTP
adapter delivers the assignment notice through 4.1's `AssignmentNotifier` port
(Q11).

Takeover (D6, Q8): a claim over a live owner succeeds only on a recorded ground.
`owner_dead` holds when the owner fails the existing
`src/gobby/storage/tasks/_owner_shield.py::OWNER_MAY_RESUME_GUARD_SQL` predicate
(not in a live status, no fresh compact-continue marker, no contested terminal
expiry, no operator claim hold); native reuses that predicate's SQL text.
`delegated` is the agent-run lineage. `coordinator` is either of 2.1's
relationships, as Q8 admits: the coordinator (a local root session declaring
`coordinate_tasks`) or the claim manager (`manage_task_claims` within
`task_claim_scope`); the evidence names the capability that admitted it.
`operator` is a claim from 2.1's `Operator` principal. Without a ground, `force=True` is refused with
`TASK_CLAIM_CONFLICT` and the sanctioned-path message, under
`tasks.mcp-claim-task.takeover-grounds`. The ground is written per 2.1.

Session-variable bookkeeping is the seam #21562 and #21567 inherit. In the claim
transaction, under the `SessionVariableMutation` lock (or
`SessionVariableTransfer` for a hand-off), native performs what `src/gobby/workflows/state_manager.py::SessionVariableManager.select_task_claim`
and `SessionVariableManager.release_task_claim` do today: update
`claimed_tasks`, `task_claimed`, `task_has_commits`, `active_task_id` and
`task_selection_history` (as `src/gobby/workflows/task_claim_state.py::record_task_selection`
writes it), queue or withdraw the memory reviews Python queues on claim and
release. The selection fence is not part of the claim transaction: a
`before_tool` rule refuses the claim through the
`src/gobby/workflows/condition_helpers_selection.py::pending_selection_fence_calls`
helper, so native claims inherit no fence check (#25621). A displaced owner's variables are cleared in the same transaction, as
`release_claimed_task` does.
Planned: `CARGO_BUILD_JOBS=3 cargo nextest run -p gobby-tasks --test claim`.

**Granularity:** Claim, release, hand-off, transfer and delegation share one
owner state machine and one session-variable seam; splitting them would
duplicate the seam.

**Acceptance:**
- 3.5.1 - Claim, release, hand-off, delegation and session links match 0.3's rows and refusals, and claims return the assignment target. test: `crates/gtasks/tests/claim.rs::claim_lifecycle_matches_python`.
- 3.5.2 - Takeover of a live claim succeeds only on `owner_dead`, `delegated`, `coordinator` or `operator`, records the ground and its evidence, and otherwise refuses with no write. The `coordinator` cases are a local root declaring only `coordinate_tasks` (admitted), a `manage_task_claims` caller inside its `task_claim_scope` (admitted) and outside it (refused), and a spawned or other-machine caller declaring only `coordinate_tasks` (refused). test: `crates/gtasks/tests/claim.rs::takeover_requires_recorded_ground`.
- 3.5.3 - Claim and release update the five session variables and memory-review queue in the task's transaction, and a failure rolls back both. test: `crates/gtasks/tests/claim.rs::session_variables_commit_with_claims`.
- 3.5.4 - Release, hand-off and transfer keep Python's dirty-file refusal, holder and recipient checks, and the `manage_task_claims`, `task_claim_scope` and `release_own_claim` rules, and a refused transfer moves nothing. test: `crates/gtasks/tests/claim.rs::release_rules_match_python`.

### 3.6 Escalation, de-escalation and reopen [category: code] (depends: 3.5)
`kind: deliverable`

Targets:
- `crates/gtasks/src/status.rs`
- `crates/gtasks/src/lib.rs` — edit the file created by 1.1
- `crates/gtasks/tests/status.rs`

**Research context:** Oracles: `escalate_task`, `de_escalate_task` and
`reopen_task` in `src/gobby/mcp_proxy/tools/tasks/_lifecycle_status.py`, with
`clear_prior_claim_session_variables` and `link_escalating_session` from
`src/gobby/mcp_proxy/tools/tasks/_escalation_coordinator.py`;
`src/gobby/storage/tasks/_de_escalation.py`; `src/gobby/storage/tasks/_lifecycle.py`;
the three HTTP routes; and the CLI callbacks `src/gobby/cli/tasks/crud.py::de_escalate_cmd`
and `src/gobby/cli/tasks/crud.py::reopen_task_cmd`. Escalating an already
escalated task that the caller holds releases the claim and keeps the original
escalation (#23204). Each transition writes Python's `task_lifecycle_events`
row and clears the prior owner's claim variables through 3.5's seam. The
`task_progress` notice Python sends to the parent session is emitted by the MCP
adapter in 5.2 from the transition outcome.
Planned: `CARGO_BUILD_JOBS=3 cargo nextest run -p gobby-tasks --test status`.

**Acceptance:**
- 3.6.1 - Escalate, de-escalate and reopen match 0.3's rows, lifecycle events and errors, including re-escalation releasing the claim. test: `crates/gtasks/tests/status.rs::status_transitions_match_python`.
- 3.6.2 - Each transition returns the outcome the adapters need for events and parent notices. test: `crates/gtasks/tests/status.rs::status_outcomes_carry_notice_data`.

### 3.7 Close snapshot, closure policy port, finalization and repair [category: code] (depends: 3.6)
`kind: deliverable`

Targets:
- `crates/gtasks/src/close.rs`
- `crates/gtasks/src/closure.rs`
- `crates/gtasks/src/landing_target.rs`
- `crates/gtasks/src/plan_archive.rs`
- `crates/gtasks/src/lib.rs` — edit the file created by 1.1
- `crates/gtasks/tests/close.rs`

**Research context:** #25449 moved every close decision into declared
`task-closure` rules (`src/gobby/install/shared/workflows/rules/task-closure/`
`coding`, `epic`, `general`, `independent-review`, `landing` and `security`, plus
`roles/closure-authority.yaml`), evaluated by
`src/gobby/tasks/closure_policy.py::evaluate_closure_policy`. Ruling C1 splits
the work: gtasks builds the snapshot and owns everything around the decision;
the decision itself comes through a port.

`closure.rs` defines the port: `ClosurePolicy::evaluate(&ClosureSnapshot) ->
ClosureDecision`, with both types mirroring
`src/gobby/tasks/closure_policy.py::ClosureSnapshot` (`event` of `task_close` or
`task_land`, `task`, `caller`, `reason`, `attestations`, `facts`) and
`ClosureDecision` (`allowed`, `blockers`, `policy_revision`,
`snapshot_revision`). gtasks does not port the evaluator; #24151 implements the
port over gworkflows. `closure.rs` also ports the snapshot builder,
`src/gobby/tasks/closure_snapshot.py::prepare_closure`: lock the task
`FOR NO KEY UPDATE` and check `expected_updated_at`; lock the session and its
`session_variables` row `FOR SHARE`; take
`LOCK TABLE agent_definitions, rule_definitions IN SHARE MODE`; resolve the task
through `resolve_lifecycle_task`; take the caller's definition from
`session_agent_definition`, or, for no session or an operator session
(`src/gobby/workflows/agent_resolver.py::is_operator_session`), the `default`
definition, refusing any other session without one; lock children
`FOR SHARE SKIP LOCKED` and raise the stale-state error when the set is
incomplete; and fill `caller` (`session_id`, `definition`, `definition_hash`,
`capabilities`, `project_id`, `machine_id`), the attestation list from 3.4a, and
the facts (`closure_intent`, caller facts, subtree facts from
`src/gobby/tasks/subtree_snapshot.py::capture_subtree_facts`, `children`,
`children_closed` and `claimants` from
`src/gobby/storage/session_tasks.py::claimant_sessions`). The locks stay held
through the closing transaction, as `PreparedClosure` holds them in Python.

`close.rs` ports the pre-policy refusals of
`src/gobby/mcp_proxy/tools/tasks/_lifecycle_close.py::_evaluate_close` in order:
`invalid_close_reason`, `task_not_found`, `no_session_context`,
`invalid_session`, `session_resolution_failed`, `task_close_scope_forbidden`
(the definition's `task_close_scope`), `task_protection_policy_unavailable` and
`task_protected` (2.2, for `respect_task_protections`), `invalid_project_path`,
`remote_session_ownership`, `invalid_commit_sha` and
`closure_snapshot_unavailable`. It derives the same caller facts:
`superseded_by`, `repository_available`, `commit_shas`, `candidate_commit_sha`,
`candidate_covers_linked_commits`, `attribution_complete`, `task_files_clean`,
`dirty_checkouts`, `unavailable_checkouts` and `integration_ancestry` (through
the landing target below and a merge-base check). Attribution ports
`src/gobby/mcp_proxy/tools/tasks/_close_context.py::capture_attribution`
(`task_edited_file_set`, `task_live_checkout_files`, git committable paths and
foreign-owned dirty paths) over 1.3's git port; an attribution lookup failure
leaves those facts false, as Python does. The preview ports
`src/gobby/mcp_proxy/tools/tasks/_lifecycle_close_preview.py::CloseEvaluation`.

`landing_target.rs` ports `src/gobby/tasks/land_commit.py::task_landing_target`
and `main_checkout_target`, reading teams through
`src/gobby/agents/team_lifecycle.py::task_team` and
`src/gobby/agents/team_admission_records.py::team_binding` the way 3.5 ports the
epic-team seat read. A task on a pod lands in its parent team's binding, and a
pod whose parent team has no binding is refused. A leaf of an epic team, other
than the epic's own task, lands in that team. Either team target is its
worktree and branch, refused when the worktree is missing or on another
branch. Any other task lands on the main checkout's branch from the
project's registered checkout, with no target when that checkout is detached.
Close turns a refusal or a missing target into `integration_ancestry` false, as
`_lifecycle_close.py` does; 3.10 reuses the same read for landing.

Finalization ports `src/gobby/storage/tasks/_close.py::_close_task_in_txn`: the
decision must be allowed, `expected_closure_revision` must equal the decision's
`snapshot_revision`, and a blocked decision raises Python's
`Task closure blocked: <blockers>` error (`TaskClosurePolicyError`, a 400 on
HTTP). Ancestor auto-close follows `close_eligible_parent_chain`, and each
ancestor goes through the same port with `closure_intent` `ancestor`. The
after-commit work ports
`src/gobby/mcp_proxy/tools/tasks/_lifecycle_close_finalization.py::commit_close`
(closed ancestors, claim cleanup, close memory review). HTTP close keeps its
request shape (`reason`, `commit_sha`, `session_id`; no `force`), calls the same
service, and without a session resolves the `default` definition's policy.
Candidate repair ports `repair_closed_candidate` in
`src/gobby/mcp_proxy/tools/tasks/_lifecycle_candidate_repair.py` and
`src/gobby/storage/tasks/_lifecycle.py::repair_closed_candidate`: it corrects the
closed marker against the original close review and does not evaluate the
policy. Close keeps Python's `remote_session_ownership` refusal for a session on
another machine; node checkouts are deferred to #21546 (3.8).

Closing an epic as done or moot triggers plan archival after commit, as
`src/gobby/storage/tasks/_close.py::_schedule_ancestor_epic_archive` does
through `src/gobby/storage/plans.py::LocalPlanManager.archive_plan`.
`plan_archive.rs` is a narrow native implementation of that step behind a
terminal-epic hook, and it keeps every observable effect of `archive_plan`:
- An already archived row returns unchanged, so a repeated archive is a no-op.
- The destination keeps the path below `.gobby/plans/`, so
  `.gobby/plans/a/b.md` moves to `.gobby/plans/completed/a/b.md`. A path outside
  `.gobby/plans/` keeps its whole relative path.
- A missing source file refuses before any move or row write, because
  `src/gobby/storage/plans.py::LocalPlanManager._relative_plan_path` raises
  `FileNotFoundError` ahead of the move. A file outside the project root
  refuses the same way with `ValueError`.
- An existing destination file refuses the archive before any move, as Python's
  `FileExistsError` does, and nothing is written.
- The `plans` row records `state` archived, the new `plan_path`, `archived_at`
  and `updated_at`. If that update fails, the move is rolled back.
- After the row commits, the coverage manifest at
  `src/gobby/plans/coverage_manifest.py::coverage_manifest_path` is removed for
  every plan kind except `strategy`, as
  `src/gobby/storage/plans.py::LocalPlanManager._remove_coverage_artifacts`
  does. A missing manifest is not an error.
- Nothing happens when no plan is linked.
- Archival runs after the close commits, so a refusal never fails the close.
  As `src/gobby/hooks/event_handlers/_plan.py::on_epic_terminal` does, a
  missing plan or missing source file is a silent no-op and any other refusal
  is logged.
The oracles are `tests/storage/test_plans_manager.py`, whose archive test
asserts manifest removal and a repeated archive, and
`tests/hooks/event_handlers/test_plan.py` for the hook. #21581 later moves this implementation
into the Plans crate and rewires the hook; 6.1 records that handoff.

Proof is snapshot parity plus a scripted decision (C1): tests bind a scripted
`ClosurePolicy` that returns 0.3's recorded Python `ClosureDecision` for each
vector, and assert that the native snapshot equals 0.3's recorded Python
`ClosureSnapshot`. #24151 proves the evaluator's decision parity.
Lock lifetime is proven apart from parity, because a frozen-instant snapshot
cannot show that the locks survive evaluation through the write. Those tests
open independent connections to the test hub and pause the scripted policy
inside `evaluate`. While it is paused, each concurrent writer either waits for
the close to commit or makes the close stale, and no close commits on an
obsolete allowed decision. The writers are an attestation append, a child
attachment and an agent or rule definition write. The scripted port is the only
test seam.
Planned: `CARGO_BUILD_JOBS=3 cargo nextest run -p gobby-tasks --test close`.

**Granularity:** The refusals, snapshot, decision and finalization form one
close state machine around one port; repair and archival are its after-close
edges, the landing target feeds one of its facts, and all share its fixtures.
Twelve items prove that one machine.

**Acceptance:**
- 3.7.1 - Every pre-policy refusal matches 0.3's result in `_evaluate_close` order, and a refused close writes nothing. test: `crates/gtasks/tests/close.rs::close_refusals_match_python`.
- 3.7.2 - An allowed close writes Python's rows, lifecycle events, claim cleanup and ancestor auto-closes, each ancestor through the port with `closure_intent` `ancestor`, and HTTP close keeps its request shape and default-definition policy. test: `crates/gtasks/tests/close.rs::close_finalization_matches_python`.
- 3.7.3 - Candidate repair matches Python, including SHA resolution and the original-review proof, and evaluates no policy. test: `crates/gtasks/tests/close.rs::repair_matches_python`.
- 3.7.7 - For every 0.3 close vector the native `ClosureSnapshot` equals Python's, including caller facts, subtree facts, attestations, the `default` fallback for no session or an operator session, and the refusal of a non-operator session without a definition. test: `crates/gtasks/tests/close.rs::closure_snapshot_matches_python`.
- 3.7.8 - Under a scripted policy, a blocked decision refuses with Python's `Task closure blocked` error and writes nothing, an allowed one finalizes, and a changed `snapshot_revision` against `expected_closure_revision` is refused as stale. test: `crates/gtasks/tests/close.rs::scripted_decision_drives_close`.
- 3.7.4 - Closing an epic as done or moot archives its linked plan after commit, rolls back on failure and is a no-op without a plan. test: `crates/gtasks/tests/close.rs::epic_close_archives_plan`.
- 3.7.6 - Native archival matches `archive_plan`: nested paths map below `completed/`, the coverage manifest is removed except for strategy plans, a repeated archive changes nothing, a missing source, a source outside the project root or an existing destination refuses with no move and no row write, and the epic close still succeeds. test: `crates/gtasks/tests/close.rs::plan_archive_matches_python`.
- 3.7.5 - A session on another machine is still refused with Python's remote-session error. test: `crates/gtasks/tests/close.rs::remote_session_close_refused_locally`.
- 3.7.9 - The landing target matches `task_landing_target` for a pod task, an epic-team leaf, the epic's own task and a top-level task, returns none for a detached main checkout, refuses an unbound pod parent and a missing or re-branched team worktree, and close records `integration_ancestry` false for each refusal or missing target. test: `crates/gtasks/tests/close.rs::landing_target_matches_python`.
- 3.7.10 - While the scripted policy is paused in `evaluate`, a concurrent attestation append, child attachment, and agent or rule definition write on independent connections each wait for the close to commit or make it stale, and no close commits on an obsolete allowed decision. test: `crates/gtasks/tests/close.rs::close_locks_hold_through_write`.
- 3.7.11 - A close whose child set is incomplete under `FOR SHARE SKIP LOCKED` refuses as stale with no write, and two sibling leaves closing concurrently under one parent both commit without deadlock. test: `crates/gtasks/tests/close.rs::incomplete_children_refuse_stale`.
- 3.7.12 - An ancestor whose `ancestor`-intent decision is blocked or stale stays open while the leaf's close commits, as `_close_task_in_txn` catches `TaskClosurePolicyError` and `TaskStaleStateError` for ancestors. test: `crates/gtasks/tests/close.rs::blocked_ancestor_keeps_leaf_closed`.

### 3.8 Node checkout git evidence
`kind: deferred`

Ruling A (Decision Record, 2026-11-01). ROADMAP decision 26 moved node work to
Stage 4. #24521 (the node `checkout.read` handler) and #24532 (live node reads)
now sit under #21546, which waits on #21574, which waits on #21544, and #21544
contains #21560. A Stage 2 leaf blocked by either could never become ready, so
the node git port, including gdaemon's node reader (formerly 4.1a.2), is
deferred to #21546. Stage 2 native git runs at the stored checkout path on the
hub exactly as Python does (1.3), and close keeps Python's remote-session
refusal (3.7.5). Native matches Python on every surface, so no node waiver
exists. The heading names no dependency: #21546 already waits on all of
#21560 through #21574. Ownership therefore routes through #21546's cited
parent, never a dependency edge: a #21560 to #21546 edge would cycle through
#21574 and #21544 and block Stage 2 on Stage 4.
`src/gobby/plans/deferral.py::validate_deferral` accepts that form, which the
`project-checkout-identity` plan used for its D1. At approval the Orchestrator
labels #21546 `deferred-from:tasks-family:3.8` and `cited-parent:#21542` (its
parent, the 1.0 root), labels #21542 `out-of-scope-for:#21560`, checks that
#21542 is open and outside #21560's dependency closure, and copies 3.8.1 to
3.8.5 into #21546's validation criteria.

```yaml
deferral:
  task_ref: "#21546"
  reason: "ROADMAP decision 26 moved node work, including #24521 and #24532, under Stage 4 (#21546), which waits on #21574 and #21544; a Stage 2 leaf blocked by them would deadlock, so Stage 2 keeps Python's hub-local git and the remote-session refusal."
  owner: "orchestrator"
  original_acceptance_items:
    - 3.8.1
    - 3.8.2
    - 3.8.3
    - 3.8.4
    - 3.8.5
```

The deferred design, kept for #21546's planner. Python runs git at the stored project path on the hub,
which fails for a checkout that lives on a node. The node `checkout.read`
command (`docs/contracts/node-commands.md`, contract types in
`crates/gdaemon/src/node_commands/checkout_read.rs::GitQuery`: `args`, `stdin`,
`timeout_ms`) runs read-only git inside a registered checkout; #24521 lands the
handler and #24532 proves live node reads.

`node_git.rs` implements 1.3's git port over an injected checkout reader that
sends one `checkout.read` git query and returns its result. gtasks cannot
depend on gdaemon, so a gdaemon bridge supplies the reader over
`crates/gdaemon/src/nodes/mod.rs::HubNodes`: it looks up the session's machine
with `crates/gdaemon/src/nodes/registry.rs::Registry::command_sender` and sends
one query with `crates/gdaemon/src/nodes/registry.rs::CommandSender::request`.
Standalone mode binds no reader. The target checkout is the
calling or named session's `machine_id` and `workspace_path` when that machine
is not the hub, and otherwise the hub-local path Python uses. With this port the
close gates accept node sessions, lifting 3.7's refusal, and diff, commit
verification, auto-link, attestation SHA references and observed-file updates
work for node checkouts. Each changed surface then needs a node-checkout-git
waiver, which #21546 adds. A node that is offline or refuses the query
returns #24970 `unavailable` with no write.

The node's reply is untrusted evidence, so `node_git.rs` validates it before
any caller reads stdout. The reply shape is
`crates/gdaemon/src/node_commands/checkout_read.rs::CheckoutReadResult` (`root`,
`project_id`, `git`) with
`crates/gdaemon/src/node_commands/checkout_read.rs::GitResult` (`exit_code`,
`timed_out`, `stdout`, `stderr`, `truncated`). The node caps a reply at 8 MiB
(`REPLY_BUDGET`). The gdaemon bridge's reader (3.8.5) maps these fields into
the port's own type unchanged. Each of these is refused as `unavailable`, with no task, attestation,
link or observed-file write:
- a missing `git` result;
- a `project_id` other than the task's project, or a `root` that is neither
  the queried `cwd` (the session's `workspace_path`) nor an ancestor of it;
- `timed_out`;
- `truncated`, so a capped diff, status or log never becomes close, commit or
  observed-file evidence;
- an `exit_code` of none, meaning git died on a signal.
A numeric exit code, zero or not, goes to the same per-operation
interpretation the local port applies. A non-ancestor answer from
`merge-base --is-ancestor`, for example, stays a domain result. No retry or
paging is added.

- 3.8.1 - Close evidence for a node session's checkout is read through `checkout.read` and matches the local result for the same repository state.
- 3.8.2 - Diff, commit verification, auto-link, attestation SHA references and observed files resolve against the node checkout under the node-checkout-git waivers #21546 adds.
- 3.8.3 - An offline or refusing node yields `unavailable` and no write.
- 3.8.4 - A reply that is truncated, timed out, signal-killed, missing its git result, or from another project or root yields `unavailable` with no write. A nonzero exit code gets the same interpretation as the local port.
- 3.8.5 - A registered node channel answers the gdaemon bridge's `checkout.read` git query with the output of local git on the same repository, and an unconnected or refusing node yields `unavailable`.

### 3.9 Commit links and observed files [category: code] (depends: 3.7)
`kind: deliverable`

Targets:
- `crates/gtasks/src/commits.rs`
- `crates/gtasks/src/lib.rs` — edit the file created by 1.1
- `crates/gtasks/tests/commits.rs`

**Research context:** Oracles: `link_commit`, `unlink_commit`,
`auto_link_commits` and the commit list in
`src/gobby/mcp_proxy/tools/task_commits.py`, `update_observed_files` (with
`require_commits`) in `src/gobby/mcp_proxy/tools/tasks/_affected_files.py`, and
the CLI callbacks `src/gobby/cli/tasks/commits.py::link_commit`,
`src/gobby/cli/tasks/commits.py::unlink_commit`,
`src/gobby/cli/tasks/commits.py::auto_link` and
`src/gobby/cli/tasks/commits.py::list_commits`. Links keep the candidate flag
and the claim-authority rule from 2.1. Auto-link scans the same git log range
for task refs through the git port, after 2.1's Q13b row admits the caller.
With a `task_id`, that means claim authority over that task. A project-wide
scan is coordinator-lane only, and `auto_link_commits` asserts its D10 entry.
Planned: `CARGO_BUILD_JOBS=3 cargo nextest run -p gobby-tasks --test commits`.

**Acceptance:**
- 3.9.1 - Link, unlink, list and auto-link match 0.3's rows and errors. test: `crates/gtasks/tests/commits.rs::commit_links_match_python`.
- 3.9.2 - Observed-file updates derive the same files from linked commits and honor `require_commits`. test: `crates/gtasks/tests/commits.rs::observed_files_match_python`.
- 3.9.3 - Auto-link refuses a session without authority over its `task_id`, and refuses a project-wide scan outside the coordinator lane, with no link written. test: `crates/gtasks/tests/commits.rs::auto_link_lane_authority`.

### 3.10 Landing freeze and land_commit [category: code] (depends: 3.9)
`kind: deliverable`

Targets:
- `crates/gtasks/src/landing.rs`
- `crates/gtasks/src/lib.rs` — edit the file created by 1.1
- `crates/gtasks/tests/landing.rs`

**Research context:** Oracles: `set_landing_freeze` and `land_commit` in
`src/gobby/mcp_proxy/tools/tasks/_landing.py` and
`src/gobby/tasks/land_commit.py::land_candidate`. Its order, which native keeps:
refuse a missing session (`session_required`) and a non-full SHA; resolve the
candidate; take `MainCheckoutLanding` (`main_checkout_landing:{project_id}`,
priority 25); resolve the target through 3.7's `landing_target.rs` (an epic team's
worktree and branch for a team leaf, otherwise `main_checkout_target`, refusing
`main_checkout_detached` when none exists); then, for up to three attempts,
re-read the tip and receipts and check the lander relationship
(`_manages_task`: `land_tasks` within `task_land_scope` on this machine). Each
attempt builds a `task_land` snapshot through 3.7's builder and the
`ClosurePolicy` port, with facts `closure_intent` `landing`,
`candidate_commit_sha`, `candidate_paths` (pinned by `_candidate_grant_paths`),
`integration_branch`, `integration_base` and
`candidate_contains_integration_base`, plus the derived
`integration_path_grant` from the definition's
`covers_direct_integration_paths`. The decision's blockers join
`landing_authority_required` and `candidate_not_linked`, and an admitted attempt
records `admission_policy_revision` and `admission_snapshot_revision`. After
admission it runs `git merge-tree` against the tip, then checks
`missing_schema_carriers` (`schema_carriers_missing`), `classify_paths` for the
activation class, the freeze (`read_freeze`, `landing_frozen`) and paths the
tip also changed (`base_update_required`); `merge_conflict` and
`base_update_required` alone escalate through `_escalate`. A tip moved by
disjoint paths lands a generated two-parent merge (`commit-tree` with the
`chore: land <sha10> for #<seq>` subject); otherwise it fast-forwards with
`GIT_REFLOG_ACTION` provenance and a 120-second timeout. Reflog recovery is
`_recover_landing`, the receipt and messages are `_record_and_notify`, then
`_refresh_index` and `_mark_task_worktree`. When a receipt is created,
`_record_and_notify` writes one durable `inter_session_messages` row per
recipient (claimant, creator and delegator, except the caller) through
`src/gobby/storage/inter_session_messages.py::InterSessionMessageManager.create_message`,
with no wake, and returns unreached recipients as `notification_pending`;
native inserts the same rows. Security approval is a `security-review`
attestation with result `approved` whose author declares
`approve_security_review` and whose git reference is the candidate SHA, which
the `security` rule file requires through the port; native carries no
security-specific code. The team-target read is 3.7's; 3.10 adds no second
port of it. Git writes run in the hub-local process at the target path.
Planned: `CARGO_BUILD_JOBS=3 cargo nextest run -p gobby-tasks --test landing`.

**Acceptance:**
- 3.10.1 - Landing reproduces Python's results, receipts and landing-message rows for clean, already-landed, disjoint-tip merge, conflict, base-update, schema-carrier, frozen and policy-blocked candidates on a temporary repository, with the scripted policy returning 0.3's decisions. test: `crates/gtasks/tests/landing.rs::land_commit_matches_python`.
- 3.10.2 - An interrupted landing recovers from the reflog exactly as Python does and never double-lands. test: `crates/gtasks/tests/landing.rs::landing_recovers_from_reflog`.
- 3.10.3 - The `task_land` snapshot, including the landing facts and `integration_path_grant`, equals 0.3's Python snapshot, and a team leaf lands on its epic worktree's branch while any other task lands on the main checkout. test: `crates/gtasks/tests/landing.rs::landing_snapshot_and_target_match_python`.

### 3.11 Backup, restore, doctor, stats, history and reindex [category: code] (depends: 3.10)
`kind: deliverable`

Targets:
- `crates/gtasks/src/ops.rs`
- `crates/gtasks/src/lib.rs` — edit the file created by 1.1
- `crates/gtasks/tests/ops.rs`

**Research context:** Oracles: `src/gobby/sync/tasks.py::TaskBackupManager`
(export to `~/.gobby/backups/<project-uuid>/tasks.jsonl`, restore), the MCP
`backup_tasks` in `src/gobby/mcp_proxy/tools/tasks/_backup.py` (default path
only), the CLI callbacks `src/gobby/cli/tasks/main.py::backup_tasks`,
`src/gobby/cli/tasks/main.py::restore_tasks` (operator-only, refused to agent
shells), `src/gobby/cli/tasks/main.py::doctor_cmd` after 0.1,
`src/gobby/cli/tasks/crud.py::task_stats`,
`src/gobby/cli/tasks/crud.py::validation_history_cmd` over
`task_validation_history`, and `reindex_tasks` in
`src/gobby/mcp_proxy/tools/tasks/_search.py`, which returns statistics because
pg_search maintains the BM25 index. Backup JSONL is byte-compatible with
Python's so either side restores the other's file. Each exported task carries
its attestation history from `src/gobby/tasks/attestations.py::attestations_by_task`,
and restore appends it through 3.4a's `restore_attestations`, keeping Python's
`extended`, `unchanged`, `ahead` and `diverged` outcomes. Backup runs on demand only.
Native backup and restore take no path. Both use the hub's default
`~/.gobby/backups/<project-uuid>/tasks.jsonl` (Q12, Q13c), so the CLI's
`--output` and `--input` become the two default-path-only waivers.
Planned: `CARGO_BUILD_JOBS=3 cargo nextest run -p gobby-tasks --test ops`.

**Acceptance:**
- 3.11.1 - Native backup writes Python's JSONL, including attestation history, to the default path only, and native and Python restore each other's files identically, attestation outcomes included. test: `crates/gtasks/tests/ops.rs::backup_round_trips_with_python`.
- 3.11.2 - Restore reads only the hub's default backup path and is refused to every principal except `Operator`. test: `crates/gtasks/tests/ops.rs::restore_is_operator_only`.
- 3.11.3 - Doctor's two checks, stats, validation history and reindex statistics match Python. test: `crates/gtasks/tests/ops.rs::ops_reports_match_python`.

## P4: HTTP family and activation
`kind: framing`

### 4.1 Tasks HTTP routes, events and the assignment port [category: code] (depends: 3.11)
`kind: deliverable`

Targets:
- `crates/gtasks/src/family.rs`
- `crates/gtasks/src/events.rs`
- `crates/gtasks/src/http/mod.rs`
- `crates/gtasks/src/http/tasks.rs`
- `crates/gtasks/src/http/relations.rs`
- `crates/gtasks/src/http/lifecycle.rs`
- `crates/gtasks/src/lib.rs` — edit the file created by 1.1
- `crates/gtasks/Cargo.toml` — edit the file created by 1.1 to add `axum`
- `Cargo.lock`
- `crates/gtasks/tests/http.rs`

**Research context:** Oracles: `src/gobby/servers/routes/tasks.py::create_tasks_router`
(list, create, get, patch, delete and the nested `_broadcast_task` helper),
`src/gobby/servers/routes/tasks_comment_routes.py::register_task_comment_routes`,
`src/gobby/servers/routes/tasks_dependency_routes.py::register_task_dependency_routes`
and `src/gobby/servers/routes/tasks_lifecycle_routes.py::register_task_lifecycle_routes`
(claim, release-claim, escalate, close, reopen, de-escalate, with
`broadcast_with_warning` and `_warning_from_exception`), with 0.2's corpus as the
recorded result. The family template is #21559's
`crates/gcore/src/route_family.rs::RouteFamily` as
`crates/gruntime/src/lib.rs::RuntimeFamily` implements it: a static method and
path table (`NATIVE_ROUTES`, `path_matches`) and `Caller::of`. Add `axum`
0.8 with default features off, as `crates/gruntime/Cargo.toml` declares it.
gdaemon does not link gtasks yet, so this leaf bumps no binary.

`TasksFamily` is named `tasks`, has prefix `/api/tasks`, and claims exactly the
seventeen method and path pairs for `Caller::Operator`. Cookie requests and
unlisted paths keep proxying until 4.4, and `default_backend` stays Proxy until
4.3. Handlers decode bodies with Absent, Clear and Set, take principal
`Operator`, call the P1 and P3 services, and map failures through #24970's HTTP
adapter, keeping Python's 400 `detail` text, 404s and the safe 500 body.

`events.rs` defines two ports. `TaskEventSink` publishes the frame
`src/gobby/servers/websocket/broadcast.py::BroadcastMixin.broadcast_task_event`
builds (`type` `task_event`, `event`, `task_id`, `timestamp` and the task
fields) for the ten HTTP events recorded in 0.2 and for `task_progress`.
Adapters publish only after commit, in Python's order, and a failed publish
adds Python's `warnings` entry without failing the request.
`AssignmentNotifier` delivers the claim's `task_assignment` notice with the
content, priority, metadata and wake that `TaskAssignmentNotifier.send` uses
(Q11). `TasksFamily::bind` accepts the sink, an optional notifier and an
optional 3.7 `ClosurePolicy` once; a request before binding returns #24970
`unavailable`. While no notifier is bound, `claims` excludes
`POST /api/tasks/{task_id}/claim`, and while no closure policy is bound, it
excludes `POST /api/tasks/{task_id}/close`, so both routes keep proxying. This
plan binds neither: #21562 binds the notifier (Q11) and #24151 binds the policy
(C1). Both handlers are still complete. The claim test uses a recording
notifier, including the `assignment_notification` warning when delivery fails;
the close test uses 3.7's scripted policy.

Tests run the router in-process against the isolated test database with
recording sink and notifier fakes, the scripted policy and 0.2's fixtures.
Planned: `CARGO_BUILD_JOBS=3 cargo nextest run -p gobby-tasks --test http`.

**Granularity:** Seventeen items are one per ledger route over one HTTP adapter;
the event and route-boundary items are that adapter's own seams. Each route's
service state machine and tests live in P1 to P3.

**Acceptance:**
- 4.1.1 - `GET /api/tasks` matches 0.2's filters, ordering, pagination and errors. test: `crates/gtasks/tests/http.rs::list_route_matches_python`.
- 4.1.2 - `POST /api/tasks` matches 0.2's rows, response, errors and events. test: `crates/gtasks/tests/http.rs::create_route_matches_python`.
- 4.1.3 - `GET /api/tasks/{task_id}` matches every ref form, the 404 and the response. test: `crates/gtasks/tests/http.rs::get_route_matches_python`.
- 4.1.4 - `PATCH /api/tasks/{task_id}` preserves omitted versus null fields and matches rows, errors and events. test: `crates/gtasks/tests/http.rs::patch_route_matches_python`.
- 4.1.5 - `DELETE /api/tasks/{task_id}` matches the cascade and unlink flags, rows, errors and events. test: `crates/gtasks/tests/http.rs::delete_route_matches_python`.
- 4.1.6 - `GET /api/tasks/{task_id}/comments` matches ordering, authors and 404s. test: `crates/gtasks/tests/http.rs::list_comments_route_matches_python`.
- 4.1.7 - `POST /api/tasks/{task_id}/comments` matches rows, response, errors and events. test: `crates/gtasks/tests/http.rs::add_comment_route_matches_python`.
- 4.1.8 - `DELETE /api/tasks/{task_id}/comments/{comment_id}` matches rows, response and 404s. test: `crates/gtasks/tests/http.rs::delete_comment_route_matches_python`.
- 4.1.9 - `GET /api/tasks/{task_id}/dependencies` matches the dependency projection and 404s. test: `crates/gtasks/tests/http.rs::list_dependencies_route_matches_python`.
- 4.1.10 - `POST /api/tasks/{task_id}/dependencies` matches edge rows, response and the cycle, self-edge and duplicate errors. test: `crates/gtasks/tests/http.rs::add_dependency_route_matches_python`.
- 4.1.11 - `DELETE /api/tasks/{task_id}/dependencies/{depends_on_id}` matches rows, response and 404s. test: `crates/gtasks/tests/http.rs::remove_dependency_route_matches_python`.
- 4.1.12 - `POST /api/tasks/{task_id}/claim` matches session resolution, `force`, rows and events, and delivers the assignment notice through a recording notifier, with the `assignment_notification` warning on failure. test: `crates/gtasks/tests/http.rs::claim_route_matches_python`.
- 4.1.13 - `POST /api/tasks/{task_id}/release-claim` matches rows, response, errors and events. test: `crates/gtasks/tests/http.rs::release_claim_route_matches_python`.
- 4.1.14 - `POST /api/tasks/{task_id}/close` matches close with and without `commit_sha`, `reason`, `closed_in_session_id`, the policy-refused 400, rows and events, under the scripted policy. test: `crates/gtasks/tests/http.rs::close_route_matches_python`.
- 4.1.15 - `POST /api/tasks/{task_id}/escalate` matches rows, response, errors and events. test: `crates/gtasks/tests/http.rs::escalate_route_matches_python`.
- 4.1.16 - `POST /api/tasks/{task_id}/reopen` matches rows, response, errors and events. test: `crates/gtasks/tests/http.rs::reopen_route_matches_python`.
- 4.1.17 - `POST /api/tasks/{task_id}/de-escalate` matches rows, response, errors and events. test: `crates/gtasks/tests/http.rs::de_escalate_route_matches_python`.
- 4.1.18 - Events publish after commit in Python's order with Python's frames, and a failed publish adds the `warnings` entry without changing the result. test: `crates/gtasks/tests/http.rs::events_follow_commit_like_python`.
- 4.1.19 - The family claims exactly the listed pairs for key callers, never cookie requests, unlisted paths, the claim route without a bound notifier or the close route without a bound closure policy, and an unbound family returns `unavailable`. test: `crates/gtasks/tests/http.rs::claims_are_exact_and_key_only`.

### 4.1a Link gtasks into gdaemon with event wiring [category: code] (depends: 4.1)
`kind: deliverable`

Targets:
- `crates/gdaemon/src/tasks_bridge.rs`
- `crates/gdaemon/src/lib.rs`
- `crates/gdaemon/src/serve.rs::*` — scope-reason: construct, bind and register the Tasks family in the unix run entry point
- `crates/gdaemon/tests/tasks_bridge.rs`
- `crates/gdaemon/Cargo.toml`
- `Cargo.lock`
- `src/gobby/install/version_pins.py::*` — scope-reason: release pin for gdaemon linking gtasks

**Research context:** `crates/gdaemon/src/serve.rs` (828 lines) builds the
family list from `crates/gdaemon/src/front_door/routes.rs::FAMILIES` and pushes
`RuntimeFamily` when a pool exists. Later in the same `run` it constructs the
`BroadcastBroker` passed to `NativeWs::new`. Add `gobby-tasks` as a path
dependency of gdaemon. Construct `TasksFamily` with
the pool beside `RuntimeFamily` and push it. Create the `BroadcastBroker` as a
local before the listeners bind, pass one clone to `NativeWs::new` and one to
the bridge, and call `TasksFamily::bind` before serving starts. serve.rs grows by
the construction, push and bind lines only.

`tasks_bridge.rs` implements `TaskEventSink` by publishing the task frame through
`crates/gdaemon/src/websocket/broker.rs::BroadcastBroker::publish` with the
filter Python's `src/gobby/servers/websocket/native_publish.py::NativePublisher`
sends for task events, as `crates/gdaemon/src/websocket/publish.rs::publish`
does for Python. No `AssignmentNotifier` (Q11) and no `ClosurePolicy` (C1) are
bound. The node checkout reader is deferred with 3.8 (3.8.5).

Linking gtasks changes gdaemon's crate closure, so this leaf bumps gdaemon by
one patch and updates the lock and pin. The family default stays Proxy, so
production behaviour does not change.
Planned: `CARGO_BUILD_JOBS=3 cargo nextest run -p gobby-daemon --test tasks_bridge`.

**Granularity:** Three of the seven targets are release carriers; the outcome is
one wiring seam.

**Acceptance:**
- 4.1a.1 - A subscribed native WebSocket client receives bridge-published Tasks frames exactly as it receives Python-published ones. test: `crates/gdaemon/tests/tasks_bridge.rs::task_events_reach_broker_subscribers`.
- 4.1a.2 - `gdaemon serve` registers the Tasks family with a Proxy default and binds it before serving, leaving the claim and close routes unclaimed. test: `crates/gdaemon/tests/tasks_bridge.rs::tasks_family_registers_passive`.
- 4.1a.3 - gdaemon receives this leaf's patch bump, lock and pin. test: `tests/install/test_version_pins.py::test_managed_bin_pins_match_crate_versions`.

### 4.2 Side-effect-safe comparison and native corpus replay [category: code] (depends: 4.1a, #24963)
`kind: deliverable`

Targets:
- `crates/gtasks/src/compare.rs`
- `crates/gtasks/src/family.rs` — edit the file created by 4.1 to implement the proxied-result check
- `crates/gtasks/src/lib.rs` — edit the file created by 1.1
- `crates/gtasks/tests/compare.rs`
- `tests/contracts/http/manifest.json::*` — scope-reason: mark only proven Tasks HTTP cases native with Python origin
- `tests/e2e/test_tasks_family.py`
- `crates/gdaemon/Cargo.toml`
- `Cargo.lock`
- `src/gobby/install/version_pins.py::*` — scope-reason: release pin for the Tasks compare verifier

**Research context:** #21559's Compare dispatch
(`crates/gdaemon/src/front_door/compare.rs`) runs native read legs, proxies
writes once, then calls `RouteFamily::check_proxied` with a `ProxiedCheck`.

State can change between the two read legs, and task `updated_at` alone cannot
see it. `task_dependencies` has no `updated_at` (`id`, `task_id`, `depends_on`,
`dep_type`, `created_at`). Comment inserts and deletes in
`src/gobby/servers/routes/tasks_comment_routes.py` touch only `task_comments`.
A new list member changes no existing row. So compare reads a stability set
before the first leg and after the second, in one read transaction each. The
set is the bounded logical result the route's native projection consumes:
- the ordered identities of a list or search result;
- each consumed task row's identity and `updated_at`;
- the identity and content of each consumed dependency edge, comment and label.
Any difference marks the case Skipped with a bounded reason and is never a
match. A case whose result is unbounded or larger than the set limit cannot be
proven stable, so it is Skipped conservatively.

For writes, Python executes once. Another writer can commit between Python's
commit and `check_proxied`'s first read, so the post-image must be tied to the
response before it is compared. `check_proxied` reads the post-image (the task
row, `task_lifecycle_events`, dependency, comment and session-variable rows) in
one read transaction, and the check uses only that snapshot. Each operation
then proves linkage through an anchor its response already carries, as
`crates/gruntime/src/verify.rs::verify` does with `config_revision`:
- Task-row writes (`POST /api/tasks`, `PATCH`, reopen, escalate, de-escalate
  and release-claim) return
  `src/gobby/storage/tasks/_models.py::Task.to_dict`. The anchor is its `id`
  and `updated_at`. A matching row is compared in full, so a difference is a
  Mismatch. A different `updated_at` or a missing row means a later write, and
  the case is Skipped.
- Comment add returns the inserted row and dependency add returns
  `src/gobby/storage/task_dependencies.py::TaskDependency.to_dict`. The anchor
  is the row `id`. A present row is compared in full; a missing row means a
  later delete, and the case is Skipped.
- Task delete and comment delete name a UUID. Absence is Verified. A present
  comment is a Mismatch, because no path recreates a comment UUID. A present
  task is Skipped, because 3.11's restore re-inserts tasks by `id`
  (`src/gobby/sync/tasks.py` inserts with `ON CONFLICT (id) DO NOTHING`).
- Dependency remove returns only the pair, and a later add can restore the
  same pair under a new edge `id`. Absence is Verified; a present pair is
  Skipped.
- Effects the response does not carry are lifecycle events and
  session-variable keys. They are linked only when they are
  exactly the effects native projects from the request and the anchored row,
  and the task's newest events are those events. Anything else is Skipped,
  because `src/gobby/mcp_proxy/tools/tasks/_claim_release.py` and
  `src/gobby/tasks/commit_unlink.py` append events without touching the task
  row.
- A refused write commits nothing and carries no anchor. It is Verified when
  native refuses the same request identically against the snapshot, and
  otherwise Skipped, because a later write may have removed the refusing
  condition.
Skipped never counts as a pass:
`crates/gdaemon/src/front_door/compare.rs::Report::passes` fails a report with
any unwaived Skipped outcome. An isolated, undisturbed case reproduces every
anchor and still Passes. A test reproduces the race deterministically without
a new seam: it runs the Python write, keeps response A, commits a competing
write B, then calls `check_proxied` with A. No mutation is repeated, and these
checks add no schema version column and no retry. The compare log
stores IDs and reasons, never descriptions, comments or commit content. The
claim and close routes are unclaimed (Q11, C1), so compare never sees them;
4.1.12 and 4.1.14 cover them.

Replay the fifteen claimed cases from 0.2's corpus against the native family in
isolated key-caller mode, keeping Python origin. Cookie cases stay a recorded
Python oracle until 4.4. Use #24963's shared loader and readiness rule: at least
one ordinary Passed case, explicit native assertions for each cited Waived case,
stable coverage of every claimed route, and failure for all-Skipped or unwaived
Skipped reports. Skips never substitute for a waiver. The e2e suite also pairs a node
with the hub through the `pair_hub` and `pair_node` fixtures in
`tests/e2e/test_hub_node_pair.py`, proving the node forwards `/api/tasks` to
the hub's native family and holds no database credential.
Planned: `CARGO_BUILD_JOBS=3 cargo nextest run -p gobby-tasks --test compare` and
`DATABASE_URL=postgresql://gobby_test:gobby_test@127.0.0.1:60892/gobby_test GOBBY_TEST_PROTECT=1 uv run pytest tests/e2e/test_tasks_family.py`.

**Granularity:** Release and corpus carriers raise the target count; the single
outcome is trustworthy compare and replay evidence for this family.

**Acceptance:**
- 4.2.1 - Read and write comparisons detect a tampered response or post-image, skip concurrent changes and never repeat a mutation. test: `crates/gtasks/tests/compare.rs::compare_is_side_effect_free_and_race_aware`.
- 4.2.6 - A dependency add or remove, a comment add or delete, or a new list member injected between read legs marks the case Skipped. So does a competing write committed after Python's write and before the first post-image read: a task update, a delete of the added comment or edge, a re-add of the removed edge, a default-backup restore after a task delete, or a change that lifts a refusal. A comment still present after a delete response is a Mismatch, and an undisturbed case still Passes. test: `crates/gtasks/tests/compare.rs::relation_and_membership_races_skip`.
- 4.2.2 - The fifteen claimed cases replay with ordinary parity or exact cited #24963 replacements, keep Python origin and need no Python backend for native legs. test: `tests/e2e/test_tasks_family.py::test_native_tasks_corpus_matches_python`.
- 4.2.3 - Controlled compare covers every claimed route with stable evidence, at least one ordinary Passed case and zero unwaived mismatches, and all-Skipped or unwaived-Skipped readiness fails. test: `tests/e2e/test_tasks_family.py::test_compare_has_coverage_without_double_effects`.
- 4.2.4 - A paired node forwards `/api/tasks` requests to the hub's native family without a database credential. test: `tests/e2e/test_tasks_family.py::test_node_routes_tasks_to_hub`.
- 4.2.5 - gdaemon receives this leaf's patch bump, lock and pin. test: `tests/install/test_version_pins.py::test_managed_bin_pins_match_crate_versions`.

### 4.3 Native default for key callers and coherent release [category: code] (depends: 4.2, #24964)
`kind: deliverable`

Targets:
- `crates/gtasks/src/family.rs` — edit the file created by 4.1
- `crates/gdaemon/tests/tasks_cutover.rs`
- `tests/e2e/test_tasks_family.py` — edit the file created by 4.2
- `docs/contracts/tasks-native.md`
- `crates/gdaemon/Cargo.toml`
- `Cargo.lock`
- `src/gobby/install/version_pins.py::*` — scope-reason: release pin for the Tasks activation

**Research context:** `TasksFamily::default_backend` returns Native. The
bootstrap `front_door.routes.tasks` setting still selects Proxy or Compare, so
rollback is a routing edit with the same binaries; Python task routes stay
mounted until #21574. Activation requires a passing #24963 readiness result from
4.2; zero-Passed, all-Skipped, unwaived Skipped, unknown or stale IDs and
unasserted waivers refuse it. From this leaf HTTP enforces the native authority
matrix (Q4). HTTP callers are `Operator`, so results do not change. Native
responses reach browser-originated key callers from this leaf, so #24964's
shared outer CORS and preflight boundary must be live first. Cookie
requests, `POST /api/tasks/{task_id}/claim` (#21562, Q11) and
`POST /api/tasks/{task_id}/close` (#24151, C1) keep proxying;
`docs/contracts/tasks-native.md` records the three remainders, the rollback
setting and the activation evidence.

This leaf bumps gdaemon. The release owner rebuilds gdaemon and promotes the
coherent gcode, gdaemon and ghook set through `promote_workspace_binary_set`
(`src/gobby/install/bin_set_coherence.py`), reading installed hashes from
`~/.gobby/bin/` after promotion. Only the Orchestrator delegates the cutover,
after a global announcement with no live worker or close validator. No push is
authorized here.
Planned: `CARGO_BUILD_JOBS=3 cargo nextest run -p gobby-daemon --test tasks_cutover`
and the isolated e2e suite.

**Granularity:** Release carriers raise the target count; the outcome is one
activation state change.

**Acceptance:**
- 4.3.1 - Activation is refused unless 4.2's readiness report passes. test: `crates/gdaemon/tests/tasks_cutover.rs::readiness_gates_tasks_activation`.
- 4.3.2 - Native, Compare and Proxy selection affect only the Tasks family, and rollback to Proxy leaves native writes readable by Python. test: `crates/gdaemon/tests/tasks_cutover.rs::tasks_backend_switch_is_scoped`.
- 4.3.3 - With the Python backend unavailable, the fifteen key-caller routes work natively, while claim, close and cookie requests still proxy. test: `tests/e2e/test_tasks_family.py::test_native_routes_without_python_backend`.
- 4.3.4 - The activation record names build and installed hashes, readiness evidence, the rollback setting, the announcement and the claim, close and cookie remainders. behavior: "Tasks activation evidence" in `docs/contracts/tasks-native.md`.
- 4.3.5 - gdaemon receives this leaf's patch bump, lock and pin. test: `tests/install/test_version_pins.py::test_managed_bin_pins_match_crate_versions`.

### 4.4 Tasks browser admission through the shared verifier [category: code] (depends: 5.4, #24967, #24964)
`kind: deliverable`

Targets:
- `crates/gtasks/src/family.rs` — edit the file created by 4.1
- `crates/gdaemon/tests/tasks_browser.rs`
- `docs/contracts/tasks-native.md` — edit the file created by 4.3
- `crates/gdaemon/Cargo.toml`
- `Cargo.lock`
- `src/gobby/install/version_pins.py::*` — scope-reason: release pin for Tasks browser admission

**Research context:** #24967 owns cookie parsing, verification, credential
precedence, revocation, caller class and selector extraction. This leaf
consumes its typed browser caller and registers the fifteen claimed Tasks routes
for it; it adds no verifier, session store, issuer or policy. A verified cookie
caller is the `Operator` principal (2.1). Compatibility references:
`src/gobby/servers/auth_service.py::AuthService.authenticate`,
`src/gobby/servers/browser_sessions.py::browser_route_allowed` and
`src/gobby/servers/middleware/auth.py::AuthMiddleware.dispatch`. Ordinary
`gobby_session` cookies keep their current admission on all fifteen routes.
`browser-testing.` cookies keep the live-capability, loopback and selector
checks, reach only GET (`browser_route_allowed` admits GET, HEAD and OPTIONS),
and are refused with the existing 403 on every mutation before any effect.
Preserve the credentialed CORS, preflight and refusal headers 0.2 recorded
through #24964's single outer boundary; add no family-local CORS, Origin or
CSRF rule. Denials from the shared verifier never fall through to a native
handler. Cookie claim and close requests keep proxying (Q11, C1).

**Expansion obligation:** Immediately after expansion, the Orchestrator applies
`security-review` to the leaf for source section 4.4 and checks the persisted
label before dispatch. A missing label holds dispatch. Cookie activation stays
held until a security reviewer records a `security-review` attestation with
result `approved`, from a definition declaring `approve_security_review`, whose
git reference is the exact implementation SHA, which the `security` rules
(`closure-requires-security-approval` and `landing-requires-security-approval`
in `src/gobby/install/shared/workflows/rules/task-closure/security.yaml`)
require; a later implementation change needs a renewed attestation.
Planned: `CARGO_BUILD_JOBS=3 cargo nextest run -p gobby-daemon --test tasks_browser`,
composing the real front door, the shared verifier and the Tasks handlers with
isolated session state and a counting Python backend stub.

**Acceptance:**
- 4.4.1 - Ordinary cookies reproduce 0.2's admission, payloads, errors and effects on the fifteen claimed routes through #24967's verifier only. test: `crates/gdaemon/tests/tasks_browser.rs::ordinary_cookie_routes_match_python`.
- 4.4.2 - Browser-testing cookies keep loopback, live-capability and selector checks, reach the GET routes, and are refused on every mutation before any effect. test: `crates/gdaemon/tests/tasks_browser.rs::managed_cookie_authority_matches_python`.
- 4.4.3 - Missing, invalid, expired or revoked credentials gain no Tasks authority, key callers and other families are unchanged, and cookie claim and close requests still proxy. test: `crates/gdaemon/tests/tasks_browser.rs::browser_admission_fails_closed`.
- 4.4.4 - Both cookie classes keep credentialed CORS, preflight and refusal headers through #24964's outer boundary, with no new Origin or CSRF rule. test: `crates/gdaemon/tests/tasks_browser.rs::cors_matches_python`.
- 4.4.5 - The activation record names the persisted `security-review` label, the pre-dispatch check and the approved `security-review` attestation for the exact shipping SHA. behavior: "Tasks browser admission security gate" in `docs/contracts/tasks-native.md`.
- 4.4.6 - gdaemon receives this leaf's patch bump, lock and pin. test: `tests/install/test_version_pins.py::test_managed_bin_pins_match_crate_versions`.

## P5: Front-door service handoffs
`kind: framing`

### 5.1 gobby-tasks read descriptors [category: code] (depends: 4.3, #24883, #24970)
`kind: deliverable`

Targets:
- `crates/gtasks/src/mcp/mod.rs`
- `crates/gtasks/src/mcp/reads.rs`
- `crates/gtasks/src/lib.rs` — edit the file created by 1.1
- `crates/gtasks/Cargo.toml` — edit the file created by 1.1 to add `schemars`
- `crates/gtasks/tests/mcp_reads.rs`
- `crates/gdaemon/Cargo.toml`
- `Cargo.lock`
- `src/gobby/install/version_pins.py::*` — scope-reason: release pin for the gobby-tasks read descriptors

**Research context:** #24883's contract in `crates/gcore/src/mcp.rs`: a family
implements `ToolFamily::server` and returns one `ToolServer` of
`ToolDefinition`s; handlers receive `ToolContext` (session, project, principal,
agent run, machine, intent, origin). `ToolRegistry::new` refuses a duplicate
server name. gtasks publishes two families: `gobby-tasks` (35 tools, 5.1 to 5.3)
and `gobby-tasks-ops` (11 tools, 5.4). `team_progress` is not among them; it is
re-homed to #21567 (D1). `mcp/mod.rs` assembles both servers and
holds the shared argument decoding (Absent, Clear, Set). Every handler resolves
its caller with 2.1's resolver in `crates/gtasks/src/principal.rs`, unchanged,
and reads only `ToolContext`: `Session` when it carries a `session_id`;
otherwise `Operator` only when its `principal` is the operator principal the
gateway projected from the front door's verified `x-gobby-key-id`
(`crates/gruntime/src/lib.rs::Caller::Operator`); otherwise `Anonymous`. A
missing or unverified principal never elevates, and tool arguments never set
the principal. 5.2 to 5.5 use the same resolver. Names,
descriptions, briefs, input schemas, definition hashes and read-only
classification match 0.3's MCP vectors; schemas normalize to JSON-object
equality with Python. Errors go through #24970's MCP adapter. An unexpected
failure returns its safe message under the tool's
`tasks.mcp-<tool-slug>.internal-error-redaction` entry, and deliberate Python
envelopes are declared through the adapter. The shared gateway supplies policy,
leases, context injection, timeouts and result offloading; nothing here
duplicates them. #21570 registers and activates both servers; this plan
registers neither in gdaemon (Q3). Request types derive their schemas with
`schemars` at the version `crates/gcore/Cargo.toml` uses, added to gtasks here.
This phase runs before 4.4 so descriptor work does not wait on #24967.

The read oracles are `src/gobby/mcp_proxy/tools/task_dependencies.py::create_dependency_registry`,
`src/gobby/mcp_proxy/tools/task_readiness.py::create_readiness_registry`,
`src/gobby/mcp_proxy/tools/task_commits.py::get_task_diff_tool` and the
`_crud.py`, `_listing.py`, `_search.py` and `_session.py` modules in
`src/gobby/mcp_proxy/tools/tasks/`. Handlers call 1.2 and 1.3 and keep the
`all_projects` and `project` selections; `get_task_diff` runs git at the stored
checkout path through 1.3's port, as Python does.
Planned: `CARGO_BUILD_JOBS=3 cargo nextest run -p gobby-tasks --test mcp_reads` and
`CARGO_BUILD_JOBS=3 cargo nextest run -p gobby-core --test mcp_contract` (the whole
target, never a name filter).

**Granularity:** Eleven tool items, a catalog item, a safe-error item and a
principal item exercise one descriptor adapter; each behaviour's state machine
lives in P1.

**Acceptance:**
- 5.1.1 - `check_dependency_cycles` matches 0.3's results and errors. test: `crates/gtasks/tests/mcp_reads.rs::check_dependency_cycles_matches_python`.
- 5.1.2 - `get_dependency_tree` matches every direction and depth. test: `crates/gtasks/tests/mcp_reads.rs::get_dependency_tree_matches_python`.
- 5.1.3 - `get_session_tasks` matches session resolution and links. test: `crates/gtasks/tests/mcp_reads.rs::get_session_tasks_matches_python`.
- 5.1.4 - `get_task` matches the actionable card and the `brief=false` record. test: `crates/gtasks/tests/mcp_reads.rs::get_task_matches_python`.
- 5.1.5 - `get_task_diff` matches Python's diffs and git failures. test: `crates/gtasks/tests/mcp_reads.rs::get_task_diff_matches_python`.
- 5.1.6 - `get_task_sessions` matches the session list. test: `crates/gtasks/tests/mcp_reads.rs::get_task_sessions_matches_python`.
- 5.1.7 - `list_blocked_tasks` matches blocking sets and parent scoping. test: `crates/gtasks/tests/mcp_reads.rs::list_blocked_tasks_matches_python`.
- 5.1.8 - `list_ready_tasks` matches readiness and parent scoping. test: `crates/gtasks/tests/mcp_reads.rs::list_ready_tasks_matches_python`.
- 5.1.9 - `list_tasks` matches filters, views, `refs`, `next_offset` and project selection. test: `crates/gtasks/tests/mcp_reads.rs::list_tasks_matches_python`.
- 5.1.10 - `search_tasks` matches ranking, scores and filters. test: `crates/gtasks/tests/mcp_reads.rs::search_tasks_matches_python`.
- 5.1.11 - `suggest_next_task` matches ordering and proximity scoring. test: `crates/gtasks/tests/mcp_reads.rs::suggest_next_task_matches_python`.
- 5.1.12 - The eleven tools' catalog entries, schemas, definition hashes, classifications and validation failures match 0.3, and malformed arguments make no service call. test: `crates/gtasks/tests/mcp_reads.rs::read_catalog_matches_python`.
- 5.1.13 - Each of the eleven tools returns the safe internal message under its own redaction entry, and deliberate errors keep ordinary parity. test: `crates/gtasks/tests/mcp_reads.rs::read_internal_errors_are_safe`.
- 5.1.14 - gdaemon receives this leaf's patch bump, lock and pin. test: `tests/install/test_version_pins.py::test_managed_bin_pins_match_crate_versions`.
- 5.1.15 - Handlers resolve callers through 2.1's resolver: a context with a `session_id` is `Session` even when it also carries a principal, a sessionless context with the verified operator principal is `Operator`, and a sessionless context with no principal or an unverified one, or with principal-like tool arguments, is `Anonymous` and never `Operator`. test: `crates/gtasks/tests/mcp_reads.rs::read_tools_resolve_principals`.

### 5.2 gobby-tasks lifecycle descriptors [category: code] (depends: 5.1)
`kind: deliverable`

Targets:
- `crates/gtasks/src/mcp/lifecycle.rs`
- `crates/gtasks/src/mcp/mod.rs` — edit the file created by 5.1
- `crates/gtasks/tests/mcp_lifecycle.rs`
- `crates/gdaemon/Cargo.toml`
- `Cargo.lock`
- `src/gobby/install/version_pins.py::*` — scope-reason: release pin for the gobby-tasks lifecycle descriptors

**Research context:** The adapter conventions are 5.1's: #24883 descriptors,
2.1 principals, Absent, Clear and Set decoding, #24970's MCP adapter and one
redaction entry per tool, with activation by #21570. Oracles are
`_lifecycle_claim.py`, `_claim_release.py`, `_session.py`,
`_lifecycle_status.py`, `_lifecycle_close.py`, `_lifecycle_close_tool.py`
(`register_close_task`), `_lifecycle_candidate_repair.py` and `_attestations.py`
(`create_attestation_registry`, which serves `record_attestation` and
`list_attestations`) in `src/gobby/mcp_proxy/tools/tasks/`. Handlers call 3.4a
and 3.5 to 3.7; `hand_off_task_claim` goes through 3.5's locked claim transfer.
`close_task` reaches 3.7's `ClosurePolicy` port, and its tests bind the scripted
policy; `repair_closed_candidate` evaluates no policy, as in Python. #21570
activates the lifecycle tools only after #24151 supplies the evaluator (C1). Each tool asserts its waiver entries where
0.3 recorded them: `tasks.mcp-claim-task.takeover-grounds` and the
project-scoped-refs entries.

The adapter emits `task_progress` through 4.1's `TaskEventSink` after escalate,
de-escalate and close commit, as
`src/gobby/mcp_proxy/tools/tasks/_notifications.py::notify_parent_on_task_state_change`
does from `coordinate_task_escalation`, `de_escalate_task` and `commit_close`: it
reads the newest pending or running `agent_runs` row for the task and publishes
`task_id`, `state`, `ref`, `parent_session_id` and `run_id`. No row or no parent
session publishes nothing, and a publish failure never fails the call.
Planned: `CARGO_BUILD_JOBS=3 cargo nextest run -p gobby-tasks --test mcp_lifecycle`
and `CARGO_BUILD_JOBS=3 cargo nextest run -p gobby-core --test mcp_contract`.

**Granularity:** Twelve tool items, the notice item, a catalog item and a
safe-error item exercise one descriptor adapter over 3.4a and 3.5 to 3.7.

**Acceptance:**
- 5.2.1 - `claim_task` matches 0.3 and asserts `tasks.mcp-claim-task.takeover-grounds`. test: `crates/gtasks/tests/mcp_lifecycle.rs::claim_task_matches_python`.
- 5.2.2 - `release_task_claim` matches release rules and refusals. test: `crates/gtasks/tests/mcp_lifecycle.rs::release_task_claim_matches_python`.
- 5.2.3 - `hand_off_task_claim` matches the locked transfer's holder and recipient checks, the dirty-file refusal and its lifecycle event. test: `crates/gtasks/tests/mcp_lifecycle.rs::hand_off_task_claim_matches_python`.
- 5.2.4 - `delegate_task` matches delegation rows and refusals. test: `crates/gtasks/tests/mcp_lifecycle.rs::delegate_task_matches_python`.
- 5.2.5 - `link_task_to_session` matches links and claim authority. test: `crates/gtasks/tests/mcp_lifecycle.rs::link_task_to_session_matches_python`.
- 5.2.6 - `escalate_task` matches escalation, re-escalation release and lifecycle events. test: `crates/gtasks/tests/mcp_lifecycle.rs::escalate_task_matches_python`.
- 5.2.7 - `de_escalate_task` matches the recorded decision and events. test: `crates/gtasks/tests/mcp_lifecycle.rs::de_escalate_task_matches_python`.
- 5.2.8 - `reopen_task` matches reopen rows and refusals. test: `crates/gtasks/tests/mcp_lifecycle.rs::reopen_task_matches_python`.
- 5.2.9 - `close_task` matches every pre-policy refusal, the closure snapshot and the scripted decision's outcome. test: `crates/gtasks/tests/mcp_lifecycle.rs::close_task_matches_python`.
- 5.2.10 - `repair_closed_candidate` matches repair results and SHA resolution. test: `crates/gtasks/tests/mcp_lifecycle.rs::repair_closed_candidate_matches_python`.
- 5.2.11 - `record_attestation` matches records, idempotent replays, supersession and refusals. test: `crates/gtasks/tests/mcp_lifecycle.rs::record_attestation_matches_python`.
- 5.2.12 - `list_attestations` matches the task's attestation history. test: `crates/gtasks/tests/mcp_lifecycle.rs::list_attestations_matches_python`.
- 5.2.13 - Escalate, de-escalate and close publish Python's `task_progress` frame after commit only when an active run has a parent session. test: `crates/gtasks/tests/mcp_lifecycle.rs::task_progress_notices_match_python`.
- 5.2.14 - The twelve tools' catalog entries, schemas, definition hashes, classifications and validation failures match 0.3, and malformed arguments make no service call. test: `crates/gtasks/tests/mcp_lifecycle.rs::lifecycle_catalog_matches_python`.
- 5.2.15 - Each of the twelve tools returns the safe internal message under its own redaction entry with no partial write. test: `crates/gtasks/tests/mcp_lifecycle.rs::lifecycle_internal_errors_are_safe`.
- 5.2.16 - gdaemon receives this leaf's patch bump, lock and pin. test: `tests/install/test_version_pins.py::test_managed_bin_pins_match_crate_versions`.

### 5.3 gobby-tasks mutation descriptors [category: code] (depends: 5.2)
`kind: deliverable`

Targets:
- `crates/gtasks/src/mcp/mutations.rs`
- `crates/gtasks/src/mcp/mod.rs` — edit the file created by 5.1
- `crates/gtasks/tests/mcp_mutations.rs`
- `crates/gdaemon/Cargo.toml`
- `Cargo.lock`
- `src/gobby/install/version_pins.py::*` — scope-reason: release pin for the gobby-tasks mutation descriptors

**Research context:** The adapter conventions are 5.1's: #24883 descriptors,
2.1 principals, Absent, Clear and Set decoding, #24970's MCP adapter and one
redaction entry per tool, with activation by #21570. Oracles are `_crud.py`,
`_lifecycle_delete.py`, `_lifecycle_labels.py`, `_affected_files.py` and
`_backup.py` in `src/gobby/mcp_proxy/tools/tasks/`,
`src/gobby/mcp_proxy/tools/task_dependencies.py::create_dependency_registry`
and `src/gobby/mcp_proxy/tools/task_commits.py`. Handlers call 3.1, 3.2, 3.9 and
3.11. Label changes on `create_task`, `update_task`, `add_label` and
`remove_label` assert their gate-label-authority entries (Q9). Commit tools and
`update_observed_files` run git at the stored checkout path, as Python does, and
each tool asserts its project-scoped-refs entry where 0.3 recorded one.
`backup_tasks` writes only the default path, attestation history included.
Planned: `CARGO_BUILD_JOBS=3 cargo nextest run -p gobby-tasks --test mcp_mutations`
and `CARGO_BUILD_JOBS=3 cargo nextest run -p gobby-core --test mcp_contract`.

**Granularity:** Twelve tool items, a catalog item and a safe-error item exercise
one descriptor adapter over 3.1, 3.2, 3.9 and 3.11.

**Acceptance:**
- 5.3.1 - `create_task` matches rows and errors and asserts its gate-label-authority entry. test: `crates/gtasks/tests/mcp_mutations.rs::create_task_matches_python`.
- 5.3.2 - `update_task` preserves field presence, matches rows and asserts its gate-label-authority entry. test: `crates/gtasks/tests/mcp_mutations.rs::update_task_matches_python`.
- 5.3.3 - `delete_task` matches cascade, unlink and claim authority. test: `crates/gtasks/tests/mcp_mutations.rs::delete_task_matches_python`.
- 5.3.4 - `add_label` matches rows and asserts its gate-label-authority entry. test: `crates/gtasks/tests/mcp_mutations.rs::add_label_matches_python`.
- 5.3.5 - `remove_label` matches rows and asserts its gate-label-authority entry. test: `crates/gtasks/tests/mcp_mutations.rs::remove_label_matches_python`.
- 5.3.6 - `add_dependency` matches edges and cycle errors. test: `crates/gtasks/tests/mcp_mutations.rs::add_dependency_matches_python`.
- 5.3.7 - `remove_dependency` matches edge removal. test: `crates/gtasks/tests/mcp_mutations.rs::remove_dependency_matches_python`.
- 5.3.8 - `link_commit` matches links, commit verification and claim authority. test: `crates/gtasks/tests/mcp_mutations.rs::link_commit_matches_python`.
- 5.3.9 - `unlink_commit` matches unlinks and claim authority. test: `crates/gtasks/tests/mcp_mutations.rs::unlink_commit_matches_python`.
- 5.3.10 - `auto_link_commits` matches scanned links and asserts its lane-authority entry. test: `crates/gtasks/tests/mcp_mutations.rs::auto_link_commits_matches_python`.
- 5.3.11 - `update_observed_files` matches derived files and `require_commits`. test: `crates/gtasks/tests/mcp_mutations.rs::update_observed_files_matches_python`.
- 5.3.12 - `backup_tasks` writes Python's JSONL to the default path only. test: `crates/gtasks/tests/mcp_mutations.rs::backup_tasks_matches_python`.
- 5.3.13 - The twelve tools' catalog entries, schemas, definition hashes, classifications and validation failures match 0.3, and malformed arguments make no service call. test: `crates/gtasks/tests/mcp_mutations.rs::mutation_catalog_matches_python`.
- 5.3.14 - Each of the twelve tools returns the safe internal message under its own redaction entry with no partial write. test: `crates/gtasks/tests/mcp_mutations.rs::mutation_internal_errors_are_safe`.
- 5.3.15 - gdaemon receives this leaf's patch bump, lock and pin. test: `tests/install/test_version_pins.py::test_managed_bin_pins_match_crate_versions`.

### 5.4 gobby-tasks-ops descriptors [category: code] (depends: 5.3)
`kind: deliverable`

Targets:
- `crates/gtasks/src/mcp/ops.rs`
- `crates/gtasks/src/mcp/mod.rs` — edit the file created by 5.1
- `crates/gtasks/tests/mcp_ops.rs`
- `crates/gdaemon/Cargo.toml`
- `Cargo.lock`
- `src/gobby/install/version_pins.py::*` — scope-reason: release pin for the gobby-tasks-ops descriptors

**Research context:** The adapter conventions are 5.1's: #24883 descriptors,
2.1 principals, Absent, Clear and Set decoding, #24970's MCP adapter and one
redaction entry per tool, with activation by #21570. Oracles are `_artifacts.py`,
`_affected_files.py`, `_landing.py` and `_search.py` in
`src/gobby/mcp_proxy/tools/tasks/`. Handlers call 1.3, 3.1, 3.3, 3.10 and 3.11.
Setters assert their project-scoped-refs entries where 0.3 recorded one.
`land_commit` reaches 3.10's landing service, which evaluates the `task_land`
event through 3.7's `ClosurePolicy` port; its tests bind the scripted policy,
and #21570 activates it only after #24151 supplies the evaluator (C1).

Python's `gobby-tasks-ops` server also carries the twelve expansion tools that
move to #21581 (Q2), and `ToolRegistry::new` refuses two families with one
server name. `mcp/mod.rs` therefore builds the `gobby-tasks-ops` server from
these eleven tools, Q12's four tools and any `ToolDefinition`s the composer
passes in.
Cross-plan edge: #21581 passes its twelve expansion definitions through that
constructor, and #21570 activates the full 27-tool server. This leaf proves the
eleven tools' parity and that passed-in definitions join the same server.

Q12 adds four operator-only tools to this server, so #21571's CLI reaches every
verb over the public tool route: `doctor_tasks`, `get_task_stats`,
`get_validation_history` and `restore_tasks`. Each calls its 3.11 service. Its
arguments are the CLI verb's options without any path: project scope for
`doctor_tasks`, `get_task_stats` and `restore_tasks`, and `task_id` for
`get_validation_history`. Results carry the service's data, and the CLI
renders them. `restore_tasks` reads only the hub's default backup path. 2.1's
rows admit only `Operator`, which 5.1's resolver yields for a sessionless call
carrying the verified operator principal, so a session or anonymous call is refused as
`forbidden` before any read or write. They are native extensions with no
Python counterpart. Their parity is to the 3.11 services' data, which 3.11
already proves against Python, and 6.1 registers their native-extension
entries.
Planned: `CARGO_BUILD_JOBS=3 cargo nextest run -p gobby-tasks --test mcp_ops` and
`CARGO_BUILD_JOBS=3 cargo nextest run -p gobby-core --test mcp_contract`.

**Granularity:** Fifteen tool items, a server-assembly item, a catalog item and
a safe-error item exercise one descriptor adapter. The four Q12 tools share
that adapter and the same server assembly.

**Acceptance:**
- 5.4.1 - `append_description_section` matches section writes. test: `crates/gtasks/tests/mcp_ops.rs::append_description_section_matches_python`.
- 5.4.2 - `clear_isolation_pair` matches pair clears. test: `crates/gtasks/tests/mcp_ops.rs::clear_isolation_pair_matches_python`.
- 5.4.3 - `find_file_overlaps` matches overlap results. test: `crates/gtasks/tests/mcp_ops.rs::find_file_overlaps_matches_python`.
- 5.4.4 - `get_affected_files` matches file lists and sources. test: `crates/gtasks/tests/mcp_ops.rs::get_affected_files_matches_python`.
- 5.4.5 - `get_artifacts` matches artifact reads. test: `crates/gtasks/tests/mcp_ops.rs::get_artifacts_matches_python`.
- 5.4.6 - `land_commit` matches landing results and refusals under the scripted policy. test: `crates/gtasks/tests/mcp_ops.rs::land_commit_matches_python`.
- 5.4.7 - `reindex_tasks` returns Python's statistics. test: `crates/gtasks/tests/mcp_ops.rs::reindex_tasks_matches_python`.
- 5.4.8 - `set_affected_files` matches writes, source tags and claim authority. test: `crates/gtasks/tests/mcp_ops.rs::set_affected_files_matches_python`.
- 5.4.9 - `set_artifact` matches single artifact writes. test: `crates/gtasks/tests/mcp_ops.rs::set_artifact_matches_python`.
- 5.4.10 - `set_artifacts_atomic` writes all fields or none. test: `crates/gtasks/tests/mcp_ops.rs::set_artifacts_atomic_matches_python`.
- 5.4.11 - `set_landing_freeze` matches freeze state and authority. test: `crates/gtasks/tests/mcp_ops.rs::set_landing_freeze_matches_python`.
- 5.4.12 - Passed-in definitions join the `gobby-tasks-ops` server beside the eleven tools, and a duplicate tool name is refused. test: `crates/gtasks/tests/mcp_ops.rs::ops_server_accepts_expansion_definitions`.
- 5.4.13 - The eleven tools' catalog entries, schemas, definition hashes, classifications and validation failures match 0.3, and malformed arguments make no service call. test: `crates/gtasks/tests/mcp_ops.rs::ops_catalog_matches_python`.
- 5.4.14 - Each of the eleven tools returns the safe internal message under its own redaction entry with no partial write. test: `crates/gtasks/tests/mcp_ops.rs::ops_internal_errors_are_safe`.
- 5.4.15 - gdaemon receives this leaf's patch bump, lock and pin. test: `tests/install/test_version_pins.py::test_managed_bin_pins_match_crate_versions`.
- 5.4.16 - `doctor_tasks` returns 3.11's cycle and invalid-project checks to `Operator` only. test: `crates/gtasks/tests/mcp_ops.rs::doctor_tasks_is_operator_only`.
- 5.4.17 - `get_task_stats` returns 3.11's statistics to `Operator` only. test: `crates/gtasks/tests/mcp_ops.rs::get_task_stats_is_operator_only`.
- 5.4.18 - `get_validation_history` returns 3.11's history for a task to `Operator` only. test: `crates/gtasks/tests/mcp_ops.rs::get_validation_history_is_operator_only`.
- 5.4.19 - `restore_tasks` restores only from the hub's default backup path, takes no path argument, and refuses every non-`Operator` caller with no write. test: `crates/gtasks/tests/mcp_ops.rs::restore_tasks_is_operator_only`.

### 5.5 CLI verb-to-public-API map [category: code] (depends: 5.4)
`kind: deliverable`

Targets:
- `docs/contracts/tasks-services.md` — edit the file created by 3.1
- `crates/gtasks/tests/cli_api_map.rs`

**Research context:** The Python CLI under `src/gobby/cli/tasks/` (`main.py`,
`crud.py`, `commits.py`, `deps.py`, `labels.py`, `search.py`, `ai.py`) writes
storage directly through `LocalTaskManager` (Q6). Under Q12, #21571's Rust
client carries every verb over the public
`POST /api/mcp/{server}/tools/{tool}` route with an operator key. That is the
route its read slice (`.gobby/plans/client-operator-verbs.md` 1.3) already
uses, so a DB-free client or node needs no in-process access. The hub's native
handlers serve the route once #21570 activates both servers.

Extend the 3.1 contract with a verb-by-verb map for all 33 CLI rows: the 29
domain verbs and the four group rows (`gobby tasks`, `commit`, `dep`, `label`),
which dispatch only. Each verb's entry names:
- its tool;
- the explicit arguments that reproduce the Python verb's defaults;
- the projection from the tool result to the verb's data;
- its typed errors;
- principal `Operator`;
- its waiver IDs: `tasks.cli-<verb-slug>.safe-errors` and the two Q12
  default-path entries.

The map, `gobby-tasks` unless marked ops:
- `backup`: `backup_tasks`
- `blocked`: `list_blocked_tasks`
- `close`: `close_task`
- `commit-auto`: `auto_link_commits`
- `commit-link`: `link_commit`
- `commit-list`: `get_task` with `brief=false`, projecting `commits`
- `commit-unlink`: `unlink_commit`
- `create`: `create_task`
- `de-escalate`: `de_escalate_task`
- `delete`: `delete_task`
- `dep-add`: `add_dependency`
- `dep-cycles`: `check_dependency_cycles`
- `dep-remove`: `remove_dependency`
- `dep-tree`: `get_dependency_tree`
- `diff`: `get_task_diff`
- `doctor`: ops `doctor_tasks`
- `label-add`: `add_label`
- `label-remove`: `remove_label`
- `list`: `list_tasks`
- `ready`: `list_ready_tasks`
- `reindex`: ops `reindex_tasks`
- `reopen`: `reopen_task`
- `restore`: ops `restore_tasks`
- `search`: `search_tasks`
- `show`: `get_task`
- `stats`: ops `get_task_stats`
- `suggest`: `suggest_next_task`
- `update`: `update_task`
- `validation-history`: ops `get_validation_history`

The P1 and P3 services choose semantics by principal, as 3.7 does for HTTP
close, so an `Operator` call runs the verb's Python operator semantics with no
handler branch. For example, `close` gets 3.7's sessionless close, which skips
the session-bound pre-policy refusals and evaluates the `default` definition's
policy through the `ClosurePolicy` port, as Python's direct-storage close does;
the contract test binds the scripted policy.

#21571 owns argument parsing, text and JSON rendering, removal of the Python
callbacks, and asserting each verb's waiver entries when it delivers that verb.
The contract test drives 0.3's CLI vectors through the native tool handlers. It
calls each mapped tool in process through the native `ToolRegistry` with an
`Operator` context, which is the handler the public route invokes. It then
compares the projected data and typed errors. Rendering is #21571's.
Planned: `CARGO_BUILD_JOBS=3 cargo nextest run -p gobby-tasks --test cli_api_map`.

**Acceptance:**
- 5.5.1 - Every CLI verb's data result and typed error in 0.3's vectors is reproduced by its mapped tool, called as `Operator` with the mapped arguments and projection, except the cited default-path waivers. test: `crates/gtasks/tests/cli_api_map.rs::cli_verbs_match_public_tools`.
- 5.5.2 - The contract maps all 33 verbs to a public tool, arguments, projection, principal, errors and waiver IDs, and states #21571's transport, rendering and removal ownership. file: `docs/contracts/tasks-services.md`.

## P6: Parity ledger
`kind: framing`

### 6.1 Publish Tasks parity ownership [category: code] (depends: 4.4, 5.5)
`kind: deliverable`

Targets:
- `docs/contracts/parity-ledger-status.d/tasks.json::*` — scope-reason: set statuses and owner notes for the 116 live Tasks rows not dropped in 0.1
- `docs/contracts/parity-waivers.json::*` — scope-reason: add the four Q12 native-extension entries
- `scripts/generate_parity_ledger.py::ADDITIVE_SURFACES`
- `docs/contracts/parity-ledger.md`

**Research context:** `docs/contracts/parity-ledger.md` is generated by
`uv run python scripts/generate_parity_ledger.py`
(`scripts/generate_parity_ledger.py::render_ledger`). Statuses are exactly
`proxied`, `delegated` or `dropped by decision #N`. After 4.4's cookie admission
and its exact-SHA security attestation, set the fifteen claimed HTTP rows to
`delegated` with their 4.1 items, compare evidence and waiver IDs. The rest stay
`proxied` with owner notes:
- `POST /api/tasks/{task_id}/claim`: #21562 binds `AssignmentNotifier` and
  activates the route (Q11).
- `POST /api/tasks/{task_id}/close`: #24151 binds the `ClosurePolicy`, proves
  decision parity and activates the route (C1), naming 4.1.14.
- The 46 MCP rows: #21570, naming the 5.1 to 5.4 item, the descriptor and the
  waiver IDs; the close and landing rows also name #24151.
- `MCP gobby-tasks:team_progress`: #21567, re-homed with no Tasks descriptor
  (D1).
- The 33 CLI rows: #21571, naming 5.5's verb-to-public-API map, the
  safe-errors and default-path IDs, and #21571's inventory re-point (ad3f8ab,
  Q12).
- The 19 expansion rows: #21581 (Q2). The notes name 3.1's write service, 3.3's
  affected-file service and 5.4's `gobby-tasks-ops` server assembly. They also
  record Q10: #21581 moves `crates/gtasks/src/plan_archive.rs` into the Plans
  crate and rewires the terminal-epic hook. They also record Q13d: #21581 owns
  the expansion runtime's authority and passes the initiating session to 3.1's
  write service.

The four Q12 tools are additive surfaces with no Python row. 6.1 adds
`MCP gobby-tasks-ops:doctor_tasks`, `MCP gobby-tasks-ops:get_task_stats`,
`MCP gobby-tasks-ops:get_validation_history` and
`MCP gobby-tasks-ops:restore_tasks` to `ADDITIVE_SURFACES`. Beside each it
adds a `tasks.mcp-<tool-slug>.native-extension` entry in
`docs/contracts/parity-waivers.json`, with decision `#5.4` and 5.4's test as
native readiness evidence. The generator renders them as `Native extension`
rows and refuses a status mapping for them.
No other family's rows change. Regenerate and confirm a clean second run.
Planned: the generator command above, run twice.

Consumers unchanged:
- `tests/contracts/http_corpus.py` — no-edit-reason: It passes `ADDITIVE_SURFACES` to `load_waivers` unchanged, so the four new surfaces load with their native-extension entries.

**Acceptance:**
- 6.1.1 - Exactly fifteen HTTP rows are `delegated`, and the claim, close, MCP, team-progress, CLI and expansion rows stay `proxied` with the owner notes and waiver IDs above. behavior: "Tasks parity ownership" in `docs/contracts/parity-ledger-status.d/tasks.json`.
- 6.1.2 - The regenerated ledger matches the fragment, and a second run changes nothing. file: `docs/contracts/parity-ledger.md`.
- 6.1.3 - The four Q12 tools appear as `Native extension` rows, each with its native-extension entry and no status mapping. file: `docs/contracts/parity-waivers.json`.

## E1 End-to-end verification
`kind: verification`

Planning proceeds now; implementation waits on R0 (#25468) through the P0
heading's edge, citing only R0's criteria in task-25460 1.1. 0.1 retires the
five dropped rows. 4.1's seventeen route items own every HTTP row; 4.2 proves
compare and replay, 4.3 activates fifteen routes for key callers, and 4.4
admits cookies. 5.1 to 5.4 own the 46 MCP rows and the four Q12 additive tools,
and 5.5 owns the 33 CLI rows' verb-to-public-API map. The 19 expansion rows go
to #21581 (Q2), which consumes 3.1, 3.3 and 5.4, and `team_progress` goes to
#21567 (D1). 3.8 is deferred to #21546 (A). 6.1 records each status. Every deliverable that shares a target file is
dependency-ordered. The chain runs 0.1 to 4.3, then 5.1 to 5.4, so descriptor
work proceeds while #24967 lands. It then splits: 4.4 waits on #24967, and 5.5
maps the CLI onto the finished tools (Q12). The two share no target and run in
parallel. 6.1 joins them.

Cross-plan edges:
- #21581: consumes the write service, the affected-file service and the
  `gobby-tasks-ops` assembly, and moves `plan_archive.rs` (Q10). It owns the
  expansion runtime's authority and passes the initiating session to the
  write service (Q13d).
- #21562: implements `AssignmentNotifier` and activates the claim route (Q11).
- #24151: implements 3.7's `ClosurePolicy` port over gworkflows (its s2-11
  Tasks seam adapter, which the Orchestrator amends), proves decision parity
  and activates the close route (C1).
- #21562 and #21567: inherit 3.5's session-variable seam.
- #21567: owns `team_progress` (D1).
- #21546: receives 3.8's deferred node checkout git, labeled
  `deferred-from:tasks-family:3.8` (A) and `cited-parent:#21542`, with
  `out-of-scope-for:#21560` on #21542 (3.8); no edge joins #21560 and #21546.
- #25468 (R0): gates the P0 phase (ENH-STALENESS-3).
- #21570: activates both MCP servers, ending the Q4 authority window; its close,
  repair and landing tools wait on #24151.
- #21571: delivers every CLI verb over the public tool route, using 5.5's map
  (Q12). Its inventory row names #21571 (ad3f8ab).
- #21574: deletes the Python routes.

Shared conventions come from #24970 (errors), #24963 (waivers and readiness),
#24964 (CORS) and #24967 (cookie verifier). No section leaves a product choice
to its executor.

Draft validation command:
`uv run gobby plans validate .gobby/plans/tasks-family.md -p /Users/josh/Projects/gobby`.
Descriptor consumers run the whole
`CARGO_BUILD_JOBS=3 cargo nextest run -p gobby-core --test mcp_contract` target.
Later leaves keep editing shared gtasks modules, family wiring, `Cargo.lock`
and gdaemon dependencies, so each leaf's focused command is not enough on its
own. Once 4.4 and 5.5 have both landed, one cumulative sweep runs against the
final assembled bytes: `CARGO_BUILD_JOBS=3 cargo nextest run -p gobby-tasks`; the
gdaemon targets `CARGO_BUILD_JOBS=3 cargo nextest run -p gobby-daemon --test tasks_bridge --test tasks_cutover --test tasks_browser`;
and
`DATABASE_URL=postgresql://gobby_test:gobby_test@127.0.0.1:60892/gobby_test GOBBY_TEST_PROTECT=1 uv run pytest tests/e2e/test_tasks_family.py`.
The sweep reruns existing tests only and adds no deliverable.
Expansion validation is unrun for this narrative draft and becomes mandatory
after the Adversary derives and applies M1. No implementation test, cutover or
expansion has been performed during planning.

## V1 Plan Changelog
`kind: framing`

2026-10-28 — Initial narrative by Plan Writer gobby#18281 for #25252. It records
the Orchestrator's rulings Q1 to Q11, the 122-row ownership (16 HTTP rows
delegated here, the claim route handed to #21562, 48 MCP rows to #21570, 33 CLI
rows to #21571, 19 expansion rows to #21581, 5 dropped), the debt dispositions
and the waiver inventory. Enhancement and adversarial review are pending; no
consensus is claimed.

2026-10-28 — Applied Plan Enhancer gobby#18286's three edits, which the
Orchestrator accepted through Triage. ENH-1: 5.5 depends on 3.11, 4.4 on 5.4,
and 6.1 joins 4.4 and 5.5. ENH-2: 2.2 proves the shipped policy sync through
`test_shipped_project_operating_policy_imports` (2.2.4). ENH-3: E1 adds one
cumulative rerun after the last shared-file edit. Adversarial review is
pending.

2026-10-28 — Adversary gobby#18284's first pass on ffcf2bb raised ADV-1 through
ADV-5. The Orchestrator ruled Q12 and Q13 through Triage.
- ADV-1: 3.7 keeps every `archive_plan` effect (3.7.6).
- ADV-2: 3.8 refuses incomplete or mismatched node git replies (3.8.4).
- ADV-3 and Q12: #21571 carries the CLI over the public tool route, and 5.5
  becomes the verb-to-public-API map, depending on 5.4 again. 5.4 adds four
  operator-only tools, which 6.1 registers as native extensions. #21571's
  inventory row was re-pointed in ad3f8ab.
- ADV-4 and Q13: 2.1 settles every #20822 item and records ground evidence.
  It adds D9 (3.2.3, 3.1.5) and D10 (3.9.3), keeps backup on the default path
  for sessions, and moves the expansion runtime to #21581.
- ADV-5: 4.2's stability check covers relation tables and list membership
  (4.2.6).
No consensus is claimed.

2026-10-28 — Adversary gobby#18284's second pass on dddb6fd found two residuals,
both accepted without a ruling.
- ADV-1: a missing source file now refuses before any move or row write, as
  `_relative_plan_path` does, and the terminal-epic hook keeps
  `on_epic_terminal`'s handling of refusals (3.7.6).
- ADV-5: each write ties its post-image to an anchor its response already
  carries, and a write committed between Python's commit and the first
  post-image read marks the case Skipped (4.2.6). This replaces the two-read
  bracket. On re-review, a task still present after a delete is Skipped,
  because restore can re-insert its UUID; comment-delete presence stays a
  Mismatch.
No consensus is claimed.

2026-10-28 — Consensus. Plan Writer gobby#18281 and Plan Adversary gobby#18284
agree on this narrative as of a6937cec36. ADV-1 through ADV-5 are resolved,
the Orchestrator's rulings Q1 to Q13 are recorded in the Decision Record, and
no blocking finding or open decision remains. Project-aware base validation
passes. The adversary derives `## M1 Task Manifest` from the commit that adds
this entry. Consensus is not implementation approval; operator approval
precedes expansion.

2026-11-01 — Staleness revision (Plan Writer gobby#19103, #25478). Re-verified
against 0.5.0 at 8c2ae3f8b8 and recorded the Orchestrator's rulings A, B, C1 and
D1 and the accepted ENH-STALENESS-1/2/3 edits as a dated Decision Record block.
3.8 is deferred to #21546 and its node waivers are dropped; 2.2 is a native port
of #25228's protection policy; 3.7 builds the closure snapshot behind a
`ClosurePolicy` port that #24151 implements, so HTTP close stays proxied; new
3.4a ports attestations, which replace the retired step tools; `team_progress`
moves to #21567; 2.1 uses declared capabilities and scopes; P0 depends on R0
(#25468). Counts are now 121 live rows, 15 delegated HTTP routes and 46 MCP
tools, lock keys follow #25262, landing follows `task_landing_target`, and
Planned cargo runs use nextest. The prior M1 is stale and is withdrawn in the
next commit. No consensus is claimed.

2026-11-01 — Withdrew the stale `## M1 Task Manifest` (it named the now-deferred 3.8 and pre-revision bytes). The Plan Adversary re-derives M1 after consensus.

2026-11-01 — Plan Adversary findings ADV-S1 to ADV-S4 on f98d5b8bf9fc490f1f463112555919b5850ceb88 accepted. 5.1 now calls 2.1's principal resolver unchanged, so a sessionless call with the verified operator principal is `Operator`, and 5.1.15 tests the three paths. 3.5's `coordinator` ground admits both of 2.1's relationships (`coordinate_tasks` local root or `manage_task_claims` within scope), and 3.5.2 lists the cases. 3.7 now owns `landing_target.rs`, which 3.10 reuses, removing the 3.7-before-3.10 ordering gap (3.7.9). 3.7.10 to 3.7.12 prove the close locks hold through evaluation and write, the `SKIP LOCKED` stale refusal and the blocked-ancestor case. No consensus is claimed.

2026-11-01 — Consensus. The Plan Writer (gobby#19103) and the Plan Adversary (622c9e82-2b4a-4825-b93e-829e87957a57) reached narrative consensus on 9a3cc958934b6fd5718bfba09eb70062009ef911 after the adversary re-reviewed the ADV-S1 to ADV-S4 repairs and found no remaining blocking finding or unresolved decision. The Plan Adversary derives and applies `## M1 Task Manifest` from the commit that records this entry.

## M1 Task Manifest
`kind: manifest`

```yaml
- title: Retire compaction and orphan-cleanup surfaces
  category: code
  task_type: feature
  depends_on: []
  validation_criteria: '0.1.1: `gobby tasks compact` and `gobby tasks clean` no longer
    exist and `gobby.storage.compaction` is not importable. test: `tests/cli/test_tasks_cli.py::test_retired_task_commands_are_absent`.

    0.1.2: `gobby tasks doctor` reports cycles and invalid projects, exits 1 when
    either is found, and prints no orphan section or clean hint. test: `tests/cli/test_tasks_cli.py::TestDoctorCommand.test_doctor_reports_cycles_and_invalid_projects`.

    0.1.3: `TaskValidator.validate_all` returns exactly `invalid_projects` and `cycles`.
    test: `tests/utils/test_db_validation.py::test_validate_all_reports_projects_and_cycles`.

    0.1.4: The five retired rows read `dropped by decision #25252` and the regenerated
    ledger matches the fragment. file: `docs/contracts/parity-ledger.md`.

    0.1.5: The CLI guide and reference audit list no retired command and describe
    doctor''s two checks. file: `docs/guides/cli-commands.md`.'
  labels:
  - covers:tasks-family:0.1:0.1.1
  - covers:tasks-family:0.1:0.1.2
  - covers:tasks-family:0.1:0.1.3
  - covers:tasks-family:0.1:0.1.4
  - covers:tasks-family:0.1:0.1.5
  tdd: true
  source_section: '0.1'
  implementation_domain: backend
- title: Characterize the HTTP, browser and event surface
  category: test
  task_type: chore
  depends_on: []
  validation_criteria: '0.2.1: All seventeen routes and their error branches are recorded
    with Python origin, rows and normalization limited to IDs and clocks. test: `tests/contracts/test_tasks_oracle.py::test_http_vectors_match_python`.

    0.2.2: Both cookie classes, credential failures and CORS policy are recorded for
    all seventeen routes without normalizing selectors or effects. test: `tests/contracts/test_tasks_oracle.py::test_browser_vectors_match_python`.

    0.2.3: Each mutating route''s event name, payload, post-commit ordering, assignment
    notification and broadcast-failure warning are recorded. test: `tests/contracts/test_tasks_oracle.py::test_task_events_match_python`.

    0.2.4: The manifest registers the three new fixtures under `families.tasks` with
    Python origin beside the existing list case. file: `tests/contracts/http/manifest.json`.'
  labels:
  - covers:tasks-family:0.2:0.2.1
  - covers:tasks-family:0.2:0.2.2
  - covers:tasks-family:0.2:0.2.3
  - covers:tasks-family:0.2:0.2.4
  tdd: false
  source_section: '0.2'
- title: Characterize services, authority, MCP and CLI
  category: test
  task_type: chore
  depends_on:
  - '0.1'
  - '0.2'
  validation_criteria: '0.3.1: Service vectors reproduce Python reads, writes, lifecycle
    effects, lifecycle-event rows and session-variable bookkeeping at a frozen instant.
    test: `tests/contracts/test_tasks_oracle.py::test_service_vectors_match_python`.

    0.3.2: Authority vectors record every transition for each enumerated caller relationship,
    including the D5, D6 and D7 Python observations. test: `tests/contracts/test_tasks_oracle.py::test_authority_vectors_match_python`.

    0.3.3: MCP vectors record catalog, schema, definition hash, classification, results,
    validation failures and unexpected-error text for all 46 tools. test: `tests/contracts/test_tasks_oracle.py::test_mcp_vectors_match_python`.

    0.3.4: CLI vectors record arguments, output, exit codes and errors for all 33
    verbs. test: `tests/contracts/test_tasks_oracle.py::test_cli_vectors_match_python`.

    0.3.5: Every inventory entry exists with its exact ledger surface, behaviour,
    decision and replacement, and each rule-based entry cites its recorded vector.
    file: `docs/contracts/parity-waivers.json`.'
  labels:
  - covers:tasks-family:0.3:0.3.1
  - covers:tasks-family:0.3:0.3.2
  - covers:tasks-family:0.3:0.3.3
  - covers:tasks-family:0.3:0.3.4
  - covers:tasks-family:0.3:0.3.5
  tdd: false
  source_section: '0.3'
- title: Family crate, row model and scoped references
  category: code
  task_type: feature
  depends_on:
  - '0.3'
  validation_criteria: '1.1.1: Task rows round-trip from PostgreSQL to the `to_dict`
    and `to_brief` shapes in 0.3''s vectors. test: `crates/gtasks/tests/refs.rs::row_projections_match_python`.

    1.1.2: Read resolution matches Python for every ref form; mutation resolution
    refuses foreign refs except for a local coordinator, with the D5 entries asserted.
    test: `crates/gtasks/tests/refs.rs::reference_resolution_matches_policy`.

    1.1.3: gcode, gdaemon and ghook receive this leaf''s patch bump, lock and pins.
    test: `tests/install/test_version_pins.py::test_managed_bin_pins_match_crate_versions`.'
  labels:
  - covers:tasks-family:1.1:1.1.1
  - covers:tasks-family:1.1:1.1.2
  - covers:tasks-family:1.1:1.1.3
  tdd: true
  source_section: '1.1'
  implementation_domain: backend
- title: Listing, readiness, search and dependency reads
  category: code
  task_type: feature
  depends_on:
  - '1.1'
  validation_criteria: '1.2.1: Listing filters, sort orders, default and compact views
    and pagination match 0.3''s vectors. test: `crates/gtasks/tests/reads.rs::list_matches_python`.

    1.2.2: Ready and blocked sets match Python, including parent scoping. test: `crates/gtasks/tests/reads.rs::readiness_matches_python`.

    1.2.3: BM25 search returns Python''s ranking, scores and filters. test: `crates/gtasks/tests/reads.rs::search_matches_python`.

    1.2.4: Dependency trees in each direction and cycle reports match Python. test:
    `crates/gtasks/tests/reads.rs::dependency_graph_matches_python`.'
  labels:
  - covers:tasks-family:1.2:1.2.1
  - covers:tasks-family:1.2:1.2.2
  - covers:tasks-family:1.2:1.2.3
  - covers:tasks-family:1.2:1.2.4
  tdd: true
  source_section: '1.2'
  implementation_domain: backend
- title: Task detail, links and suggestion reads
  category: code
  task_type: feature
  depends_on:
  - '1.2'
  validation_criteria: '1.3.1: Task cards, full records, session links and comment
    lists match Python. test: `crates/gtasks/tests/detail.rs::task_detail_matches_python`.

    1.3.2: Artifacts, affected files and file overlaps match Python. test: `crates/gtasks/tests/detail.rs::files_and_artifacts_match_python`.

    1.3.3: Task diffs match Python, including a stored checkout path the hub cannot
    open, through the git port. test: `crates/gtasks/tests/detail.rs::task_diff_matches_python`.

    1.3.4: Suggestions match Python''s ordering and proximity scoring. test: `crates/gtasks/tests/detail.rs::suggestion_matches_python`.'
  labels:
  - covers:tasks-family:1.3:1.3.1
  - covers:tasks-family:1.3:1.3.2
  - covers:tasks-family:1.3:1.3.3
  - covers:tasks-family:1.3:1.3.4
  tdd: true
  source_section: '1.3'
  implementation_domain: backend
- title: Caller principals and the transition table
  category: code
  task_type: feature
  depends_on:
  - '1.3'
  validation_criteria: '2.1.1: Every transition reproduces 0.3''s Python result for
    every caller relationship 0.3 enumerates, except the cited D5, D6, D7, D9 and
    D10 replacements. test: `crates/gtasks/tests/authority.rs::table_matches_python_relationships`.

    2.1.2: Unlisted principal and transition pairs, unresolvable session references
    and claimed tasks without session context are refused with no write. test: `crates/gtasks/tests/authority.rs::unlisted_pairs_fail_closed`.

    2.1.3: Gate-bearing label adds and removals follow Q9 for every principal, with
    `live-session` keeping its extra guard. test: `crates/gtasks/tests/authority.rs::gate_labels_follow_q9`.

    2.1.4: Exceptional transitions write one `task_lifecycle_events` row naming the
    transition, ground, verified evidence and principal. test: `crates/gtasks/tests/authority.rs::grounds_are_recorded`.

    2.1.5: The authority contract documents principals, the full table, grounds, label
    rules and the disposition of every #20822 item. file: `docs/contracts/task-authority.md`.

    2.1.6: Dependency add and remove, `create_task` `blocks`, and auto-link with and
    without a `task_id` follow Q13a and Q13b for every relationship, and each refusal
    writes nothing. test: `crates/gtasks/tests/authority.rs::readiness_and_auto_link_authority`.

    2.1.7: A tool-route call resolves to `Session` with a session context, to `Operator`
    only with a verified operator key, and to `Anonymous` otherwise, and the restore,
    doctor, stats and validation-history rows admit only `Operator`. test: `crates/gtasks/tests/authority.rs::tool_route_principals`.'
  labels:
  - covers:tasks-family:2.1:2.1.1
  - covers:tasks-family:2.1:2.1.2
  - covers:tasks-family:2.1:2.1.3
  - covers:tasks-family:2.1:2.1.4
  - covers:tasks-family:2.1:2.1.5
  - covers:tasks-family:2.1:2.1.6
  - covers:tasks-family:2.1:2.1.7
  tdd: true
  source_section: '2.1'
  implementation_domain: backend
- title: Task protection policy, ported native
  category: code
  task_type: feature
  depends_on:
  - '2.1'
  validation_criteria: '2.2.1: Native protection refuses exactly where Python does:
    a foreign claim, a protected label or a listed subtree root on the task or any
    ancestor, each checked under its own project''s policy, with Python''s messages.
    test: `crates/gtasks/tests/policy.rs::protection_matches_python`.

    2.2.2: Only definitions declaring `respect_task_protections` are checked. No session
    context and operator terminal sessions pass, and a missing session or an invalid
    definition refuses. test: `crates/gtasks/tests/policy.rs::protection_follows_declared_opt_in`.

    2.2.3: A missing marker yields the default policy, checkout-free projects use
    the default, and a malformed policy refuses the mutation or close as unavailable
    with no write. test: `crates/gtasks/tests/policy.rs::policy_loading_matches_python`.'
  labels:
  - covers:tasks-family:2.2:2.2.1
  - covers:tasks-family:2.2:2.2.2
  - covers:tasks-family:2.2:2.2.3
  tdd: true
  source_section: '2.2'
  implementation_domain: backend
- title: Task writes and the expansion write service
  category: code
  task_type: feature
  depends_on:
  - '2.2'
  validation_criteria: '3.1.1: Create, update, reparent, delete with cascade and unlink,
    labels and description sections match 0.3''s rows and errors. test: `crates/gtasks/tests/write.rs::task_writes_match_python`.

    3.1.2: Native writes take Python''s lock keys in priority order and serialize
    against a concurrent Python writer. test: `crates/gtasks/tests/write.rs::writes_share_python_locks`.

    3.1.3: The expansion write service creates a tree with labels, affected files,
    edges and artifacts in one transaction and rolls back whole on failure. test:
    `crates/gtasks/tests/write.rs::expansion_write_service_is_atomic`.

    3.1.4: The write-service contract names the operations, locks, the caller-principal
    parameter and transaction boundary #21581 consumes, and states that expansion
    apply passes the initiating session. file: `docs/contracts/tasks-services.md`.

    3.1.5: An expansion apply whose initiating session lacks authority over a foreign
    live-claimed parent, and a `create_task` whose `blocks` names a task the caller
    cannot change, are refused with no task written. test: `crates/gtasks/tests/write.rs::write_service_checks_caller_principal`.'
  labels:
  - covers:tasks-family:3.1:3.1.1
  - covers:tasks-family:3.1:3.1.2
  - covers:tasks-family:3.1:3.1.3
  - covers:tasks-family:3.1:3.1.4
  - covers:tasks-family:3.1:3.1.5
  tdd: true
  source_section: '3.1'
  implementation_domain: backend
- title: Dependency edges
  category: code
  task_type: feature
  depends_on:
  - '3.1'
  validation_criteria: '3.2.1: Edge adds and removes match 0.3''s rows and errors
    on every lane. test: `crates/gtasks/tests/deps.rs::dependency_edges_match_python`.

    3.2.2: An edge that would close a cycle is refused inside the locked transaction,
    including under a concurrent Python writer. test: `crates/gtasks/tests/deps.rs::cycles_refused_under_lock`.

    3.2.3: A session without claim authority over a claimed `task_id` cannot add or
    remove its edges, writes nothing, and gets the D9 refusal; an unclaimed task stays
    open. test: `crates/gtasks/tests/deps.rs::edge_readiness_authority`.'
  labels:
  - covers:tasks-family:3.2:3.2.1
  - covers:tasks-family:3.2:3.2.2
  - covers:tasks-family:3.2:3.2.3
  tdd: true
  source_section: '3.2'
  implementation_domain: backend
- title: Affected files, artifacts and isolation pairs
  category: code
  task_type: feature
  depends_on:
  - '3.2'
  validation_criteria: '3.3.1: Affected-file writes with each source tag match Python.
    test: `crates/gtasks/tests/files.rs::affected_files_match_python`.

    3.3.2: Single and atomic artifact writes and isolation-pair clears match Python,
    and an atomic write with one invalid field writes nothing. test: `crates/gtasks/tests/files.rs::artifacts_match_python`.'
  labels:
  - covers:tasks-family:3.3:3.3.1
  - covers:tasks-family:3.3:3.3.2
  tdd: true
  source_section: '3.3'
  implementation_domain: backend
- title: Comments
  category: code
  task_type: feature
  depends_on:
  - '3.3'
  validation_criteria: '3.4.1: Comment adds, lists and deletes match 0.3''s rows and
    errors, including the reserved-author refusals on add and delete. test: `crates/gtasks/tests/comments.rs::comments_match_python`.'
  labels:
  - covers:tasks-family:3.4:3.4.1
  tdd: true
  source_section: '3.4'
  implementation_domain: backend
- title: Task attestations
  category: code
  task_type: feature
  depends_on:
  - '3.4'
  validation_criteria: '3.4a.1: Recording matches 0.3''s rows and refusals for writer
    grants, scope, closed tasks, idempotency keys, supersession by the original author
    only, sequence and the `updated_at` bump, and a refusal writes nothing. test:
    `crates/gtasks/tests/attestations.rs::record_attestation_matches_python`.

    3.4a.2: Listing returns Python''s records in sequence order, and history validation
    and restore append or classify a backup''s history as `extended`, `unchanged`,
    `ahead` or `diverged` exactly as Python does. test: `crates/gtasks/tests/attestations.rs::attestation_history_matches_python`.'
  labels:
  - covers:tasks-family:3.4a:3.4a.1
  - covers:tasks-family:3.4a:3.4a.2
  tdd: true
  source_section: 3.4a
  implementation_domain: backend
- title: Claims, takeover grounds and session links
  category: code
  task_type: feature
  depends_on:
  - 3.4a
  validation_criteria: '3.5.1: Claim, release, hand-off, delegation and session links
    match 0.3''s rows and refusals, and claims return the assignment target. test:
    `crates/gtasks/tests/claim.rs::claim_lifecycle_matches_python`.

    3.5.2: Takeover of a live claim succeeds only on `owner_dead`, `delegated`, `coordinator`
    or `operator`, records the ground and its evidence, and otherwise refuses with
    no write. The `coordinator` cases are a local root declaring only `coordinate_tasks`
    (admitted), a `manage_task_claims` caller inside its `task_claim_scope` (admitted)
    and outside it (refused), and a spawned or other-machine caller declaring only
    `coordinate_tasks` (refused). test: `crates/gtasks/tests/claim.rs::takeover_requires_recorded_ground`.

    3.5.3: Claim and release update the five session variables and memory-review queue
    in the task''s transaction, and a failure rolls back both. test: `crates/gtasks/tests/claim.rs::session_variables_commit_with_claims`.

    3.5.4: Release, hand-off and transfer keep Python''s dirty-file refusal, holder
    and recipient checks, and the `manage_task_claims`, `task_claim_scope` and `release_own_claim`
    rules, and a refused transfer moves nothing. test: `crates/gtasks/tests/claim.rs::release_rules_match_python`.'
  labels:
  - covers:tasks-family:3.5:3.5.1
  - covers:tasks-family:3.5:3.5.2
  - covers:tasks-family:3.5:3.5.3
  - covers:tasks-family:3.5:3.5.4
  tdd: true
  source_section: '3.5'
  implementation_domain: backend
- title: Escalation, de-escalation and reopen
  category: code
  task_type: feature
  depends_on:
  - '3.5'
  validation_criteria: '3.6.1: Escalate, de-escalate and reopen match 0.3''s rows,
    lifecycle events and errors, including re-escalation releasing the claim. test:
    `crates/gtasks/tests/status.rs::status_transitions_match_python`.

    3.6.2: Each transition returns the outcome the adapters need for events and parent
    notices. test: `crates/gtasks/tests/status.rs::status_outcomes_carry_notice_data`.'
  labels:
  - covers:tasks-family:3.6:3.6.1
  - covers:tasks-family:3.6:3.6.2
  tdd: true
  source_section: '3.6'
  implementation_domain: backend
- title: Close snapshot, closure policy port, finalization and repair
  category: code
  task_type: feature
  depends_on:
  - '3.6'
  validation_criteria: '3.7.1: Every pre-policy refusal matches 0.3''s result in `_evaluate_close`
    order, and a refused close writes nothing. test: `crates/gtasks/tests/close.rs::close_refusals_match_python`.

    3.7.2: An allowed close writes Python''s rows, lifecycle events, claim cleanup
    and ancestor auto-closes, each ancestor through the port with `closure_intent`
    `ancestor`, and HTTP close keeps its request shape and default-definition policy.
    test: `crates/gtasks/tests/close.rs::close_finalization_matches_python`.

    3.7.3: Candidate repair matches Python, including SHA resolution and the original-review
    proof, and evaluates no policy. test: `crates/gtasks/tests/close.rs::repair_matches_python`.

    3.7.7: For every 0.3 close vector the native `ClosureSnapshot` equals Python''s,
    including caller facts, subtree facts, attestations, the `default` fallback for
    no session or an operator session, and the refusal of a non-operator session without
    a definition. test: `crates/gtasks/tests/close.rs::closure_snapshot_matches_python`.

    3.7.8: Under a scripted policy, a blocked decision refuses with Python''s `Task
    closure blocked` error and writes nothing, an allowed one finalizes, and a changed
    `snapshot_revision` against `expected_closure_revision` is refused as stale. test:
    `crates/gtasks/tests/close.rs::scripted_decision_drives_close`.

    3.7.4: Closing an epic as done or moot archives its linked plan after commit,
    rolls back on failure and is a no-op without a plan. test: `crates/gtasks/tests/close.rs::epic_close_archives_plan`.

    3.7.6: Native archival matches `archive_plan`: nested paths map below `completed/`,
    the coverage manifest is removed except for strategy plans, a repeated archive
    changes nothing, a missing source, a source outside the project root or an existing
    destination refuses with no move and no row write, and the epic close still succeeds.
    test: `crates/gtasks/tests/close.rs::plan_archive_matches_python`.

    3.7.5: A session on another machine is still refused with Python''s remote-session
    error. test: `crates/gtasks/tests/close.rs::remote_session_close_refused_locally`.

    3.7.9: The landing target matches `task_landing_target` for a pod task, an epic-team
    leaf, the epic''s own task and a top-level task, returns none for a detached main
    checkout, refuses an unbound pod parent and a missing or re-branched team worktree,
    and close records `integration_ancestry` false for each refusal or missing target.
    test: `crates/gtasks/tests/close.rs::landing_target_matches_python`.

    3.7.10: While the scripted policy is paused in `evaluate`, a concurrent attestation
    append, child attachment, and agent or rule definition write on independent connections
    each wait for the close to commit or make it stale, and no close commits on an
    obsolete allowed decision. test: `crates/gtasks/tests/close.rs::close_locks_hold_through_write`.

    3.7.11: A close whose child set is incomplete under `FOR SHARE SKIP LOCKED` refuses
    as stale with no write, and two sibling leaves closing concurrently under one
    parent both commit without deadlock. test: `crates/gtasks/tests/close.rs::incomplete_children_refuse_stale`.

    3.7.12: An ancestor whose `ancestor`-intent decision is blocked or stale stays
    open while the leaf''s close commits, as `_close_task_in_txn` catches `TaskClosurePolicyError`
    and `TaskStaleStateError` for ancestors. test: `crates/gtasks/tests/close.rs::blocked_ancestor_keeps_leaf_closed`.'
  labels:
  - covers:tasks-family:3.7:3.7.1
  - covers:tasks-family:3.7:3.7.2
  - covers:tasks-family:3.7:3.7.3
  - covers:tasks-family:3.7:3.7.7
  - covers:tasks-family:3.7:3.7.8
  - covers:tasks-family:3.7:3.7.4
  - covers:tasks-family:3.7:3.7.6
  - covers:tasks-family:3.7:3.7.5
  - covers:tasks-family:3.7:3.7.9
  - covers:tasks-family:3.7:3.7.10
  - covers:tasks-family:3.7:3.7.11
  - covers:tasks-family:3.7:3.7.12
  tdd: true
  source_section: '3.7'
  implementation_domain: backend
- title: Commit links and observed files
  category: code
  task_type: feature
  depends_on:
  - '3.7'
  validation_criteria: '3.9.1: Link, unlink, list and auto-link match 0.3''s rows
    and errors. test: `crates/gtasks/tests/commits.rs::commit_links_match_python`.

    3.9.2: Observed-file updates derive the same files from linked commits and honor
    `require_commits`. test: `crates/gtasks/tests/commits.rs::observed_files_match_python`.

    3.9.3: Auto-link refuses a session without authority over its `task_id`, and refuses
    a project-wide scan outside the coordinator lane, with no link written. test:
    `crates/gtasks/tests/commits.rs::auto_link_lane_authority`.'
  labels:
  - covers:tasks-family:3.9:3.9.1
  - covers:tasks-family:3.9:3.9.2
  - covers:tasks-family:3.9:3.9.3
  tdd: true
  source_section: '3.9'
  implementation_domain: backend
- title: Landing freeze and land_commit
  category: code
  task_type: feature
  depends_on:
  - '3.9'
  validation_criteria: '3.10.1: Landing reproduces Python''s results, receipts and
    landing-message rows for clean, already-landed, disjoint-tip merge, conflict,
    base-update, schema-carrier, frozen and policy-blocked candidates on a temporary
    repository, with the scripted policy returning 0.3''s decisions. test: `crates/gtasks/tests/landing.rs::land_commit_matches_python`.

    3.10.2: An interrupted landing recovers from the reflog exactly as Python does
    and never double-lands. test: `crates/gtasks/tests/landing.rs::landing_recovers_from_reflog`.

    3.10.3: The `task_land` snapshot, including the landing facts and `integration_path_grant`,
    equals 0.3''s Python snapshot, and a team leaf lands on its epic worktree''s branch
    while any other task lands on the main checkout. test: `crates/gtasks/tests/landing.rs::landing_snapshot_and_target_match_python`.'
  labels:
  - covers:tasks-family:3.10:3.10.1
  - covers:tasks-family:3.10:3.10.2
  - covers:tasks-family:3.10:3.10.3
  tdd: true
  source_section: '3.10'
  implementation_domain: backend
- title: Backup, restore, doctor, stats, history and reindex
  category: code
  task_type: feature
  depends_on:
  - '3.10'
  validation_criteria: '3.11.1: Native backup writes Python''s JSONL, including attestation
    history, to the default path only, and native and Python restore each other''s
    files identically, attestation outcomes included. test: `crates/gtasks/tests/ops.rs::backup_round_trips_with_python`.

    3.11.2: Restore reads only the hub''s default backup path and is refused to every
    principal except `Operator`. test: `crates/gtasks/tests/ops.rs::restore_is_operator_only`.

    3.11.3: Doctor''s two checks, stats, validation history and reindex statistics
    match Python. test: `crates/gtasks/tests/ops.rs::ops_reports_match_python`.'
  labels:
  - covers:tasks-family:3.11:3.11.1
  - covers:tasks-family:3.11:3.11.2
  - covers:tasks-family:3.11:3.11.3
  tdd: true
  source_section: '3.11'
  implementation_domain: backend
- title: Tasks HTTP routes, events and the assignment port
  category: code
  task_type: feature
  depends_on:
  - '3.11'
  validation_criteria: '4.1.1: `GET /api/tasks` matches 0.2''s filters, ordering,
    pagination and errors. test: `crates/gtasks/tests/http.rs::list_route_matches_python`.

    4.1.2: `POST /api/tasks` matches 0.2''s rows, response, errors and events. test:
    `crates/gtasks/tests/http.rs::create_route_matches_python`.

    4.1.3: `GET /api/tasks/{task_id}` matches every ref form, the 404 and the response.
    test: `crates/gtasks/tests/http.rs::get_route_matches_python`.

    4.1.4: `PATCH /api/tasks/{task_id}` preserves omitted versus null fields and matches
    rows, errors and events. test: `crates/gtasks/tests/http.rs::patch_route_matches_python`.

    4.1.5: `DELETE /api/tasks/{task_id}` matches the cascade and unlink flags, rows,
    errors and events. test: `crates/gtasks/tests/http.rs::delete_route_matches_python`.

    4.1.6: `GET /api/tasks/{task_id}/comments` matches ordering, authors and 404s.
    test: `crates/gtasks/tests/http.rs::list_comments_route_matches_python`.

    4.1.7: `POST /api/tasks/{task_id}/comments` matches rows, response, errors and
    events. test: `crates/gtasks/tests/http.rs::add_comment_route_matches_python`.

    4.1.8: `DELETE /api/tasks/{task_id}/comments/{comment_id}` matches rows, response
    and 404s. test: `crates/gtasks/tests/http.rs::delete_comment_route_matches_python`.

    4.1.9: `GET /api/tasks/{task_id}/dependencies` matches the dependency projection
    and 404s. test: `crates/gtasks/tests/http.rs::list_dependencies_route_matches_python`.

    4.1.10: `POST /api/tasks/{task_id}/dependencies` matches edge rows, response and
    the cycle, self-edge and duplicate errors. test: `crates/gtasks/tests/http.rs::add_dependency_route_matches_python`.

    4.1.11: `DELETE /api/tasks/{task_id}/dependencies/{depends_on_id}` matches rows,
    response and 404s. test: `crates/gtasks/tests/http.rs::remove_dependency_route_matches_python`.

    4.1.12: `POST /api/tasks/{task_id}/claim` matches session resolution, `force`,
    rows and events, and delivers the assignment notice through a recording notifier,
    with the `assignment_notification` warning on failure. test: `crates/gtasks/tests/http.rs::claim_route_matches_python`.

    4.1.13: `POST /api/tasks/{task_id}/release-claim` matches rows, response, errors
    and events. test: `crates/gtasks/tests/http.rs::release_claim_route_matches_python`.

    4.1.14: `POST /api/tasks/{task_id}/close` matches close with and without `commit_sha`,
    `reason`, `closed_in_session_id`, the policy-refused 400, rows and events, under
    the scripted policy. test: `crates/gtasks/tests/http.rs::close_route_matches_python`.

    4.1.15: `POST /api/tasks/{task_id}/escalate` matches rows, response, errors and
    events. test: `crates/gtasks/tests/http.rs::escalate_route_matches_python`.

    4.1.16: `POST /api/tasks/{task_id}/reopen` matches rows, response, errors and
    events. test: `crates/gtasks/tests/http.rs::reopen_route_matches_python`.

    4.1.17: `POST /api/tasks/{task_id}/de-escalate` matches rows, response, errors
    and events. test: `crates/gtasks/tests/http.rs::de_escalate_route_matches_python`.

    4.1.18: Events publish after commit in Python''s order with Python''s frames,
    and a failed publish adds the `warnings` entry without changing the result. test:
    `crates/gtasks/tests/http.rs::events_follow_commit_like_python`.

    4.1.19: The family claims exactly the listed pairs for key callers, never cookie
    requests, unlisted paths, the claim route without a bound notifier or the close
    route without a bound closure policy, and an unbound family returns `unavailable`.
    test: `crates/gtasks/tests/http.rs::claims_are_exact_and_key_only`.'
  labels:
  - covers:tasks-family:4.1:4.1.1
  - covers:tasks-family:4.1:4.1.2
  - covers:tasks-family:4.1:4.1.3
  - covers:tasks-family:4.1:4.1.4
  - covers:tasks-family:4.1:4.1.5
  - covers:tasks-family:4.1:4.1.6
  - covers:tasks-family:4.1:4.1.7
  - covers:tasks-family:4.1:4.1.8
  - covers:tasks-family:4.1:4.1.9
  - covers:tasks-family:4.1:4.1.10
  - covers:tasks-family:4.1:4.1.11
  - covers:tasks-family:4.1:4.1.12
  - covers:tasks-family:4.1:4.1.13
  - covers:tasks-family:4.1:4.1.14
  - covers:tasks-family:4.1:4.1.15
  - covers:tasks-family:4.1:4.1.16
  - covers:tasks-family:4.1:4.1.17
  - covers:tasks-family:4.1:4.1.18
  - covers:tasks-family:4.1:4.1.19
  tdd: true
  source_section: '4.1'
  implementation_domain: backend
- title: Link gtasks into gdaemon with event wiring
  category: code
  task_type: feature
  depends_on:
  - '4.1'
  validation_criteria: '4.1a.1: A subscribed native WebSocket client receives bridge-published
    Tasks frames exactly as it receives Python-published ones. test: `crates/gdaemon/tests/tasks_bridge.rs::task_events_reach_broker_subscribers`.

    4.1a.2: `gdaemon serve` registers the Tasks family with a Proxy default and binds
    it before serving, leaving the claim and close routes unclaimed. test: `crates/gdaemon/tests/tasks_bridge.rs::tasks_family_registers_passive`.

    4.1a.3: gdaemon receives this leaf''s patch bump, lock and pin. test: `tests/install/test_version_pins.py::test_managed_bin_pins_match_crate_versions`.'
  labels:
  - covers:tasks-family:4.1a:4.1a.1
  - covers:tasks-family:4.1a:4.1a.2
  - covers:tasks-family:4.1a:4.1a.3
  tdd: true
  source_section: 4.1a
  implementation_domain: backend
- title: Side-effect-safe comparison and native corpus replay
  category: code
  task_type: feature
  depends_on:
  - 4.1a
  validation_criteria: '4.2.1: Read and write comparisons detect a tampered response
    or post-image, skip concurrent changes and never repeat a mutation. test: `crates/gtasks/tests/compare.rs::compare_is_side_effect_free_and_race_aware`.

    4.2.6: A dependency add or remove, a comment add or delete, or a new list member
    injected between read legs marks the case Skipped. So does a competing write committed
    after Python''s write and before the first post-image read: a task update, a delete
    of the added comment or edge, a re-add of the removed edge, a default-backup restore
    after a task delete, or a change that lifts a refusal. A comment still present
    after a delete response is a Mismatch, and an undisturbed case still Passes. test:
    `crates/gtasks/tests/compare.rs::relation_and_membership_races_skip`.

    4.2.2: The fifteen claimed cases replay with ordinary parity or exact cited #24963
    replacements, keep Python origin and need no Python backend for native legs. test:
    `tests/e2e/test_tasks_family.py::test_native_tasks_corpus_matches_python`.

    4.2.3: Controlled compare covers every claimed route with stable evidence, at
    least one ordinary Passed case and zero unwaived mismatches, and all-Skipped or
    unwaived-Skipped readiness fails. test: `tests/e2e/test_tasks_family.py::test_compare_has_coverage_without_double_effects`.

    4.2.4: A paired node forwards `/api/tasks` requests to the hub''s native family
    without a database credential. test: `tests/e2e/test_tasks_family.py::test_node_routes_tasks_to_hub`.

    4.2.5: gdaemon receives this leaf''s patch bump, lock and pin. test: `tests/install/test_version_pins.py::test_managed_bin_pins_match_crate_versions`.'
  labels:
  - covers:tasks-family:4.2:4.2.1
  - covers:tasks-family:4.2:4.2.6
  - covers:tasks-family:4.2:4.2.2
  - covers:tasks-family:4.2:4.2.3
  - covers:tasks-family:4.2:4.2.4
  - covers:tasks-family:4.2:4.2.5
  tdd: true
  source_section: '4.2'
  implementation_domain: backend
- title: Native default for key callers and coherent release
  category: code
  task_type: feature
  depends_on:
  - '4.2'
  validation_criteria: '4.3.1: Activation is refused unless 4.2''s readiness report
    passes. test: `crates/gdaemon/tests/tasks_cutover.rs::readiness_gates_tasks_activation`.

    4.3.2: Native, Compare and Proxy selection affect only the Tasks family, and rollback
    to Proxy leaves native writes readable by Python. test: `crates/gdaemon/tests/tasks_cutover.rs::tasks_backend_switch_is_scoped`.

    4.3.3: With the Python backend unavailable, the fifteen key-caller routes work
    natively, while claim, close and cookie requests still proxy. test: `tests/e2e/test_tasks_family.py::test_native_routes_without_python_backend`.

    4.3.4: The activation record names build and installed hashes, readiness evidence,
    the rollback setting, the announcement and the claim, close and cookie remainders.
    behavior: "Tasks activation evidence" in `docs/contracts/tasks-native.md`.

    4.3.5: gdaemon receives this leaf''s patch bump, lock and pin. test: `tests/install/test_version_pins.py::test_managed_bin_pins_match_crate_versions`.'
  labels:
  - covers:tasks-family:4.3:4.3.1
  - covers:tasks-family:4.3:4.3.2
  - covers:tasks-family:4.3:4.3.3
  - covers:tasks-family:4.3:4.3.4
  - covers:tasks-family:4.3:4.3.5
  tdd: true
  source_section: '4.3'
  implementation_domain: backend
- title: Tasks browser admission through the shared verifier
  category: code
  task_type: feature
  depends_on:
  - '5.4'
  validation_criteria: '4.4.1: Ordinary cookies reproduce 0.2''s admission, payloads,
    errors and effects on the fifteen claimed routes through #24967''s verifier only.
    test: `crates/gdaemon/tests/tasks_browser.rs::ordinary_cookie_routes_match_python`.

    4.4.2: Browser-testing cookies keep loopback, live-capability and selector checks,
    reach the GET routes, and are refused on every mutation before any effect. test:
    `crates/gdaemon/tests/tasks_browser.rs::managed_cookie_authority_matches_python`.

    4.4.3: Missing, invalid, expired or revoked credentials gain no Tasks authority,
    key callers and other families are unchanged, and cookie claim and close requests
    still proxy. test: `crates/gdaemon/tests/tasks_browser.rs::browser_admission_fails_closed`.

    4.4.4: Both cookie classes keep credentialed CORS, preflight and refusal headers
    through #24964''s outer boundary, with no new Origin or CSRF rule. test: `crates/gdaemon/tests/tasks_browser.rs::cors_matches_python`.

    4.4.5: The activation record names the persisted `security-review` label, the
    pre-dispatch check and the approved `security-review` attestation for the exact
    shipping SHA. behavior: "Tasks browser admission security gate" in `docs/contracts/tasks-native.md`.

    4.4.6: gdaemon receives this leaf''s patch bump, lock and pin. test: `tests/install/test_version_pins.py::test_managed_bin_pins_match_crate_versions`.'
  labels:
  - covers:tasks-family:4.4:4.4.1
  - covers:tasks-family:4.4:4.4.2
  - covers:tasks-family:4.4:4.4.3
  - covers:tasks-family:4.4:4.4.4
  - covers:tasks-family:4.4:4.4.5
  - covers:tasks-family:4.4:4.4.6
  tdd: true
  source_section: '4.4'
  implementation_domain: backend
- title: gobby-tasks read descriptors
  category: code
  task_type: feature
  depends_on:
  - '4.3'
  validation_criteria: '5.1.1: `check_dependency_cycles` matches 0.3''s results and
    errors. test: `crates/gtasks/tests/mcp_reads.rs::check_dependency_cycles_matches_python`.

    5.1.2: `get_dependency_tree` matches every direction and depth. test: `crates/gtasks/tests/mcp_reads.rs::get_dependency_tree_matches_python`.

    5.1.3: `get_session_tasks` matches session resolution and links. test: `crates/gtasks/tests/mcp_reads.rs::get_session_tasks_matches_python`.

    5.1.4: `get_task` matches the actionable card and the `brief=false` record. test:
    `crates/gtasks/tests/mcp_reads.rs::get_task_matches_python`.

    5.1.5: `get_task_diff` matches Python''s diffs and git failures. test: `crates/gtasks/tests/mcp_reads.rs::get_task_diff_matches_python`.

    5.1.6: `get_task_sessions` matches the session list. test: `crates/gtasks/tests/mcp_reads.rs::get_task_sessions_matches_python`.

    5.1.7: `list_blocked_tasks` matches blocking sets and parent scoping. test: `crates/gtasks/tests/mcp_reads.rs::list_blocked_tasks_matches_python`.

    5.1.8: `list_ready_tasks` matches readiness and parent scoping. test: `crates/gtasks/tests/mcp_reads.rs::list_ready_tasks_matches_python`.

    5.1.9: `list_tasks` matches filters, views, `refs`, `next_offset` and project
    selection. test: `crates/gtasks/tests/mcp_reads.rs::list_tasks_matches_python`.

    5.1.10: `search_tasks` matches ranking, scores and filters. test: `crates/gtasks/tests/mcp_reads.rs::search_tasks_matches_python`.

    5.1.11: `suggest_next_task` matches ordering and proximity scoring. test: `crates/gtasks/tests/mcp_reads.rs::suggest_next_task_matches_python`.

    5.1.12: The eleven tools'' catalog entries, schemas, definition hashes, classifications
    and validation failures match 0.3, and malformed arguments make no service call.
    test: `crates/gtasks/tests/mcp_reads.rs::read_catalog_matches_python`.

    5.1.13: Each of the eleven tools returns the safe internal message under its own
    redaction entry, and deliberate errors keep ordinary parity. test: `crates/gtasks/tests/mcp_reads.rs::read_internal_errors_are_safe`.

    5.1.14: gdaemon receives this leaf''s patch bump, lock and pin. test: `tests/install/test_version_pins.py::test_managed_bin_pins_match_crate_versions`.

    5.1.15: Handlers resolve callers through 2.1''s resolver: a context with a `session_id`
    is `Session` even when it also carries a principal, a sessionless context with
    the verified operator principal is `Operator`, and a sessionless context with
    no principal or an unverified one, or with principal-like tool arguments, is `Anonymous`
    and never `Operator`. test: `crates/gtasks/tests/mcp_reads.rs::read_tools_resolve_principals`.'
  labels:
  - covers:tasks-family:5.1:5.1.1
  - covers:tasks-family:5.1:5.1.2
  - covers:tasks-family:5.1:5.1.3
  - covers:tasks-family:5.1:5.1.4
  - covers:tasks-family:5.1:5.1.5
  - covers:tasks-family:5.1:5.1.6
  - covers:tasks-family:5.1:5.1.7
  - covers:tasks-family:5.1:5.1.8
  - covers:tasks-family:5.1:5.1.9
  - covers:tasks-family:5.1:5.1.10
  - covers:tasks-family:5.1:5.1.11
  - covers:tasks-family:5.1:5.1.12
  - covers:tasks-family:5.1:5.1.13
  - covers:tasks-family:5.1:5.1.14
  - covers:tasks-family:5.1:5.1.15
  tdd: true
  source_section: '5.1'
  implementation_domain: backend
- title: gobby-tasks lifecycle descriptors
  category: code
  task_type: feature
  depends_on:
  - '5.1'
  validation_criteria: '5.2.1: `claim_task` matches 0.3 and asserts `tasks.mcp-claim-task.takeover-grounds`.
    test: `crates/gtasks/tests/mcp_lifecycle.rs::claim_task_matches_python`.

    5.2.2: `release_task_claim` matches release rules and refusals. test: `crates/gtasks/tests/mcp_lifecycle.rs::release_task_claim_matches_python`.

    5.2.3: `hand_off_task_claim` matches the locked transfer''s holder and recipient
    checks, the dirty-file refusal and its lifecycle event. test: `crates/gtasks/tests/mcp_lifecycle.rs::hand_off_task_claim_matches_python`.

    5.2.4: `delegate_task` matches delegation rows and refusals. test: `crates/gtasks/tests/mcp_lifecycle.rs::delegate_task_matches_python`.

    5.2.5: `link_task_to_session` matches links and claim authority. test: `crates/gtasks/tests/mcp_lifecycle.rs::link_task_to_session_matches_python`.

    5.2.6: `escalate_task` matches escalation, re-escalation release and lifecycle
    events. test: `crates/gtasks/tests/mcp_lifecycle.rs::escalate_task_matches_python`.

    5.2.7: `de_escalate_task` matches the recorded decision and events. test: `crates/gtasks/tests/mcp_lifecycle.rs::de_escalate_task_matches_python`.

    5.2.8: `reopen_task` matches reopen rows and refusals. test: `crates/gtasks/tests/mcp_lifecycle.rs::reopen_task_matches_python`.

    5.2.9: `close_task` matches every pre-policy refusal, the closure snapshot and
    the scripted decision''s outcome. test: `crates/gtasks/tests/mcp_lifecycle.rs::close_task_matches_python`.

    5.2.10: `repair_closed_candidate` matches repair results and SHA resolution. test:
    `crates/gtasks/tests/mcp_lifecycle.rs::repair_closed_candidate_matches_python`.

    5.2.11: `record_attestation` matches records, idempotent replays, supersession
    and refusals. test: `crates/gtasks/tests/mcp_lifecycle.rs::record_attestation_matches_python`.

    5.2.12: `list_attestations` matches the task''s attestation history. test: `crates/gtasks/tests/mcp_lifecycle.rs::list_attestations_matches_python`.

    5.2.13: Escalate, de-escalate and close publish Python''s `task_progress` frame
    after commit only when an active run has a parent session. test: `crates/gtasks/tests/mcp_lifecycle.rs::task_progress_notices_match_python`.

    5.2.14: The twelve tools'' catalog entries, schemas, definition hashes, classifications
    and validation failures match 0.3, and malformed arguments make no service call.
    test: `crates/gtasks/tests/mcp_lifecycle.rs::lifecycle_catalog_matches_python`.

    5.2.15: Each of the twelve tools returns the safe internal message under its own
    redaction entry with no partial write. test: `crates/gtasks/tests/mcp_lifecycle.rs::lifecycle_internal_errors_are_safe`.

    5.2.16: gdaemon receives this leaf''s patch bump, lock and pin. test: `tests/install/test_version_pins.py::test_managed_bin_pins_match_crate_versions`.'
  labels:
  - covers:tasks-family:5.2:5.2.1
  - covers:tasks-family:5.2:5.2.2
  - covers:tasks-family:5.2:5.2.3
  - covers:tasks-family:5.2:5.2.4
  - covers:tasks-family:5.2:5.2.5
  - covers:tasks-family:5.2:5.2.6
  - covers:tasks-family:5.2:5.2.7
  - covers:tasks-family:5.2:5.2.8
  - covers:tasks-family:5.2:5.2.9
  - covers:tasks-family:5.2:5.2.10
  - covers:tasks-family:5.2:5.2.11
  - covers:tasks-family:5.2:5.2.12
  - covers:tasks-family:5.2:5.2.13
  - covers:tasks-family:5.2:5.2.14
  - covers:tasks-family:5.2:5.2.15
  - covers:tasks-family:5.2:5.2.16
  tdd: true
  source_section: '5.2'
  implementation_domain: backend
- title: gobby-tasks mutation descriptors
  category: code
  task_type: feature
  depends_on:
  - '5.2'
  validation_criteria: '5.3.1: `create_task` matches rows and errors and asserts its
    gate-label-authority entry. test: `crates/gtasks/tests/mcp_mutations.rs::create_task_matches_python`.

    5.3.2: `update_task` preserves field presence, matches rows and asserts its gate-label-authority
    entry. test: `crates/gtasks/tests/mcp_mutations.rs::update_task_matches_python`.

    5.3.3: `delete_task` matches cascade, unlink and claim authority. test: `crates/gtasks/tests/mcp_mutations.rs::delete_task_matches_python`.

    5.3.4: `add_label` matches rows and asserts its gate-label-authority entry. test:
    `crates/gtasks/tests/mcp_mutations.rs::add_label_matches_python`.

    5.3.5: `remove_label` matches rows and asserts its gate-label-authority entry.
    test: `crates/gtasks/tests/mcp_mutations.rs::remove_label_matches_python`.

    5.3.6: `add_dependency` matches edges and cycle errors. test: `crates/gtasks/tests/mcp_mutations.rs::add_dependency_matches_python`.

    5.3.7: `remove_dependency` matches edge removal. test: `crates/gtasks/tests/mcp_mutations.rs::remove_dependency_matches_python`.

    5.3.8: `link_commit` matches links, commit verification and claim authority. test:
    `crates/gtasks/tests/mcp_mutations.rs::link_commit_matches_python`.

    5.3.9: `unlink_commit` matches unlinks and claim authority. test: `crates/gtasks/tests/mcp_mutations.rs::unlink_commit_matches_python`.

    5.3.10: `auto_link_commits` matches scanned links and asserts its lane-authority
    entry. test: `crates/gtasks/tests/mcp_mutations.rs::auto_link_commits_matches_python`.

    5.3.11: `update_observed_files` matches derived files and `require_commits`. test:
    `crates/gtasks/tests/mcp_mutations.rs::update_observed_files_matches_python`.

    5.3.12: `backup_tasks` writes Python''s JSONL to the default path only. test:
    `crates/gtasks/tests/mcp_mutations.rs::backup_tasks_matches_python`.

    5.3.13: The twelve tools'' catalog entries, schemas, definition hashes, classifications
    and validation failures match 0.3, and malformed arguments make no service call.
    test: `crates/gtasks/tests/mcp_mutations.rs::mutation_catalog_matches_python`.

    5.3.14: Each of the twelve tools returns the safe internal message under its own
    redaction entry with no partial write. test: `crates/gtasks/tests/mcp_mutations.rs::mutation_internal_errors_are_safe`.

    5.3.15: gdaemon receives this leaf''s patch bump, lock and pin. test: `tests/install/test_version_pins.py::test_managed_bin_pins_match_crate_versions`.'
  labels:
  - covers:tasks-family:5.3:5.3.1
  - covers:tasks-family:5.3:5.3.2
  - covers:tasks-family:5.3:5.3.3
  - covers:tasks-family:5.3:5.3.4
  - covers:tasks-family:5.3:5.3.5
  - covers:tasks-family:5.3:5.3.6
  - covers:tasks-family:5.3:5.3.7
  - covers:tasks-family:5.3:5.3.8
  - covers:tasks-family:5.3:5.3.9
  - covers:tasks-family:5.3:5.3.10
  - covers:tasks-family:5.3:5.3.11
  - covers:tasks-family:5.3:5.3.12
  - covers:tasks-family:5.3:5.3.13
  - covers:tasks-family:5.3:5.3.14
  - covers:tasks-family:5.3:5.3.15
  tdd: true
  source_section: '5.3'
  implementation_domain: backend
- title: gobby-tasks-ops descriptors
  category: code
  task_type: feature
  depends_on:
  - '5.3'
  validation_criteria: '5.4.1: `append_description_section` matches section writes.
    test: `crates/gtasks/tests/mcp_ops.rs::append_description_section_matches_python`.

    5.4.2: `clear_isolation_pair` matches pair clears. test: `crates/gtasks/tests/mcp_ops.rs::clear_isolation_pair_matches_python`.

    5.4.3: `find_file_overlaps` matches overlap results. test: `crates/gtasks/tests/mcp_ops.rs::find_file_overlaps_matches_python`.

    5.4.4: `get_affected_files` matches file lists and sources. test: `crates/gtasks/tests/mcp_ops.rs::get_affected_files_matches_python`.

    5.4.5: `get_artifacts` matches artifact reads. test: `crates/gtasks/tests/mcp_ops.rs::get_artifacts_matches_python`.

    5.4.6: `land_commit` matches landing results and refusals under the scripted policy.
    test: `crates/gtasks/tests/mcp_ops.rs::land_commit_matches_python`.

    5.4.7: `reindex_tasks` returns Python''s statistics. test: `crates/gtasks/tests/mcp_ops.rs::reindex_tasks_matches_python`.

    5.4.8: `set_affected_files` matches writes, source tags and claim authority. test:
    `crates/gtasks/tests/mcp_ops.rs::set_affected_files_matches_python`.

    5.4.9: `set_artifact` matches single artifact writes. test: `crates/gtasks/tests/mcp_ops.rs::set_artifact_matches_python`.

    5.4.10: `set_artifacts_atomic` writes all fields or none. test: `crates/gtasks/tests/mcp_ops.rs::set_artifacts_atomic_matches_python`.

    5.4.11: `set_landing_freeze` matches freeze state and authority. test: `crates/gtasks/tests/mcp_ops.rs::set_landing_freeze_matches_python`.

    5.4.12: Passed-in definitions join the `gobby-tasks-ops` server beside the eleven
    tools, and a duplicate tool name is refused. test: `crates/gtasks/tests/mcp_ops.rs::ops_server_accepts_expansion_definitions`.

    5.4.13: The eleven tools'' catalog entries, schemas, definition hashes, classifications
    and validation failures match 0.3, and malformed arguments make no service call.
    test: `crates/gtasks/tests/mcp_ops.rs::ops_catalog_matches_python`.

    5.4.14: Each of the eleven tools returns the safe internal message under its own
    redaction entry with no partial write. test: `crates/gtasks/tests/mcp_ops.rs::ops_internal_errors_are_safe`.

    5.4.15: gdaemon receives this leaf''s patch bump, lock and pin. test: `tests/install/test_version_pins.py::test_managed_bin_pins_match_crate_versions`.

    5.4.16: `doctor_tasks` returns 3.11''s cycle and invalid-project checks to `Operator`
    only. test: `crates/gtasks/tests/mcp_ops.rs::doctor_tasks_is_operator_only`.

    5.4.17: `get_task_stats` returns 3.11''s statistics to `Operator` only. test:
    `crates/gtasks/tests/mcp_ops.rs::get_task_stats_is_operator_only`.

    5.4.18: `get_validation_history` returns 3.11''s history for a task to `Operator`
    only. test: `crates/gtasks/tests/mcp_ops.rs::get_validation_history_is_operator_only`.

    5.4.19: `restore_tasks` restores only from the hub''s default backup path, takes
    no path argument, and refuses every non-`Operator` caller with no write. test:
    `crates/gtasks/tests/mcp_ops.rs::restore_tasks_is_operator_only`.'
  labels:
  - covers:tasks-family:5.4:5.4.1
  - covers:tasks-family:5.4:5.4.2
  - covers:tasks-family:5.4:5.4.3
  - covers:tasks-family:5.4:5.4.4
  - covers:tasks-family:5.4:5.4.5
  - covers:tasks-family:5.4:5.4.6
  - covers:tasks-family:5.4:5.4.7
  - covers:tasks-family:5.4:5.4.8
  - covers:tasks-family:5.4:5.4.9
  - covers:tasks-family:5.4:5.4.10
  - covers:tasks-family:5.4:5.4.11
  - covers:tasks-family:5.4:5.4.12
  - covers:tasks-family:5.4:5.4.13
  - covers:tasks-family:5.4:5.4.14
  - covers:tasks-family:5.4:5.4.15
  - covers:tasks-family:5.4:5.4.16
  - covers:tasks-family:5.4:5.4.17
  - covers:tasks-family:5.4:5.4.18
  - covers:tasks-family:5.4:5.4.19
  tdd: true
  source_section: '5.4'
  implementation_domain: backend
- title: CLI verb-to-public-API map
  category: code
  task_type: feature
  depends_on:
  - '5.4'
  validation_criteria: '5.5.1: Every CLI verb''s data result and typed error in 0.3''s
    vectors is reproduced by its mapped tool, called as `Operator` with the mapped
    arguments and projection, except the cited default-path waivers. test: `crates/gtasks/tests/cli_api_map.rs::cli_verbs_match_public_tools`.

    5.5.2: The contract maps all 33 verbs to a public tool, arguments, projection,
    principal, errors and waiver IDs, and states #21571''s transport, rendering and
    removal ownership. file: `docs/contracts/tasks-services.md`.'
  labels:
  - covers:tasks-family:5.5:5.5.1
  - covers:tasks-family:5.5:5.5.2
  tdd: true
  source_section: '5.5'
  implementation_domain: backend
- title: Publish Tasks parity ownership
  category: code
  task_type: feature
  depends_on:
  - '4.4'
  - '5.5'
  validation_criteria: '6.1.1: Exactly fifteen HTTP rows are `delegated`, and the
    claim, close, MCP, team-progress, CLI and expansion rows stay `proxied` with the
    owner notes and waiver IDs above. behavior: "Tasks parity ownership" in `docs/contracts/parity-ledger-status.d/tasks.json`.

    6.1.2: The regenerated ledger matches the fragment, and a second run changes nothing.
    file: `docs/contracts/parity-ledger.md`.

    6.1.3: The four Q12 tools appear as `Native extension` rows, each with its native-extension
    entry and no status mapping. file: `docs/contracts/parity-waivers.json`.'
  labels:
  - covers:tasks-family:6.1:6.1.1
  - covers:tasks-family:6.1:6.1.2
  - covers:tasks-family:6.1:6.1.3
  tdd: true
  source_section: '6.1'
  implementation_domain: backend
```
