Plan artifact: `.gobby/plans/runbooks.md`

# Runbooks: role-bound multi-agent tabs loaded from gclient

**Plan ID:** runbooks

## Context
`kind: framing`

Last night's overnight epic #22651 ran on a hand-built org chart: six gclient panes
(orchestrator, assistant, lane manager, researcher, reviewer, janitor; that file still
called the lane manager "dispatcher", a name Golden Path ruling 22 reserves for the
retired build dispatcher), each a persistent interactive session launched by hand with
a pasted prompt, wired together by a message protocol and standing rules written in
`~/.claude/plans/daemon-crashed-hard-last-imperative-shannon.md`. Every seat ran as the
`default` agent: no role-scoped rules, no tool restrictions, no skill selection, and the
role lived only in the prompt, so a `/clear` or a CLI relaunch lost it.

This plan productizes that pattern as a **runbook**: a shell script of `gclient` verbs
that launches each seat into the current workspace through the daemon's spawn path,
placed in a new tab or split beside a named pane, with the role's sandbox policy and
worktree binding coming from the role's definition (decision 20). The same launch is
reachable three ways with one result: `gobby-agents:spawn_agent` with `placement`,
`gobby agents spawn --tab|--split`, and `gclient launch` in command mode (`gclient
<verb>`, decision 12), so a script needs nothing but the pane env. Each pane row
remembers its role, and every SessionStart in that pane activates the matching agent
definition daemon-side, so first launch and `/clear` rebind without agent cooperation.
Roles are separate agent definitions specified field by field in 3.2 and 3.4 (decision
21): tools, sandbox block, rules, skills, worktree, messaging, continuity and step
workflow. Every seat but the program-director runs under Gobby's managed SRT sandbox;
the program-director is the one unsandboxed role and is held in by role-scoped hook
rules (decision 20).

Direction, now assumed (decision 19): Josh is retiring `gobby build` and stages. This
plan no longer preserves compatibility with `gobby build`, the stage registry or
`src/gobby/dispatch/`; it repoints or deletes their references the cheapest way that
keeps the tree green (3.2, 3.4). The retirement itself stays D2, and `src/gobby/build/`
and `epic-reviewer` are untouched.

Constraint that shaped the cut: ROADMAP.md moves the daemon to Rust family crates
(Stage 2) and makes the client carry operator verbs (S3.1). Stage 1 (#21543, #21551) is
open and unclaimed and `gdaemon` serves no routes yet, so the runbook logic lives in
gclient over the public WS and HTTP API, and Python changes are limited to seams the
client cannot reach (schema, workspace rows, SessionStart, the spawn tool's placement
and sandbox parameters, and the run lifecycle of a pane-bound seat). No web UI, no
DB-backed runbook registry.

## Decision Record
`kind: framing`

Confirmed with Josh on 2026-09-21 during elicitation.

1. **Role binding.** New nullable `role` column on `workspace_panes` and nullable
   `runbook` on `workspace_tabs`. SessionStart resolves the session's bound terminal to
   its pane and passes `pane.role` as `agent_name_override`, activating the full
   definition (rules, blocked tools, skills, variables, persona prompt). The binding
   survives `move_pane`/`swap_panes` and is lost only on `close_pane`. Recovery and
   manual switching use a new `gobby-agents:apply_agent_definition` tool that applies
   the same full delta SessionStart uses (unlike `apply_persona`, which is prompt and
   skills only). The `apply_agent_definition` sentence is superseded by decision 20 on
   2026-09-24 (round file, R6 position g): the launch is the only writer of a pane's
   role, and a role change is a relaunch.
2. **Runbook store.** Superseded by decision 12 on 2026-09-22: a runbook is a shell
   script of `gclient` verbs. The bundled `orchestration-v1.sh` lives in
   `src/gobby/install/shared/workflows/runbooks/` and is run by path; user scripts live
   wherever the user keeps them. No YAML format in 1.x, no DB kind, no sync, no MCP
   CRUD, no web UI (deferred to a Stage 2 family crate).
3. **Noun.** "runbook" everywhere: menu item "Load runbook…", `runbooks/` directories,
   `workspace_tabs.runbook`, `workspace_panes.role`.
4. **Runbook shape.** Superseded by decision 12: a runbook is an ordered script of
   verbs (`new-tab`, `split`, `resize`, `title`, `wait-for-output`, `send-keys`). A
   pane with no launch line is a bare shell; a launch line without a role is a tool
   pane (nvim, a text browser); a role (bound with the `--role` flags once 1.2 lands)
   requires an agent CLI launch line. Commands stay literal shell strings (provider,
   model, effort, sandbox flags all live there), so no Rust port of the Python command
   builder. Launch waits for the shell prompt with `wait-for-output` and types the
   command through `pane.send_text {submit: true}`, leaving a live shell under the CLI
   to resume in. Amended by decision 20 on 2026-09-24: a seat's launch line is a
   `gclient launch` verb (or the spawn tool with `placement`), so provider, model and
   effort become spawn parameters, the daemon builds and sandboxes the argv, and no
   prompt wait, typed launch line or shell under the CLI remains; `send-keys` and
   `wait-for-output` stay for tool panes.
5. **Python delta, thin seams only.** One gcore migration; `role`/`runbook` accepted on
   `pane.split`/`tab.create` plus a `pane.set_role` op; `role`, `runbook`, and the bound
   `session_ref` exposed on `get_workspace`, the WS snapshot, and events; SessionStart
   role resolution; `apply_agent_definition`. Nothing else in Python. Amended by
   decision 20 on 2026-09-24: `pane.set_role` and `apply_agent_definition` are gone, and
   the Python delta is 1.1, 1.2, 1.3 and 1.5.
6. **Roster.** `gobby-workspaces:get_workspace` is the roster: agents find peers by
   role and message with `send_message(target="session", target_id=<session_ref>)`.
   The kickoff prompt carries the runbook name, the agent's own pane ref, and the tab
   refs. Amended by decision 20 on 2026-09-24: the launch's prompt is composed before
   the pane exists, so the seat reads its own ref from `GOBBY_PANE_REF` (the identity
   env 1.5 sets before the CLI starts) and the tab refs from `get_workspace`; the
   kickoff prompt carries the runbook name and, for the council, the plan path.
7. **Restrictions as rules.** New rule group `runbook/`: a hard before_tool block on
   source-code writes for `agent_scope: [dispatcher, monitor, researcher, assistant]`
   (the seat names of 2026-09-21: ruling 22 renames the seat `lane-manager` and decision
   13 sets the roster and 3.1's scope; assistant keeps docs and plan paths), and a
   once-per-context-epoch orchestrator gate
   using `acknowledge_variable` with the standard clear/compact reset. Role definitions
   cherry-pick rule groups and skills; the default core-skill bootstrap is excluded for
   non-coding seats.
8. **Seats.** Superseded by decision 13 on 2026-09-23; the original text follows for
   the audit trail. Five persistent seats: `orchestrator`, `assistant`, `dispatcher`
   (now `lane-manager`, ruling 22), `monitor` (the janitor, renamed), `researcher`
   (existing definition). No persistent
   reviewer: per-leaf review stays with `task-close-reviewer` (unchanged), and a new
   spawn-only `post-epic-reviewer` definition, derived from `epic-reviewer`, is spawned
   by the orchestrator once per sub-epic after its commits land. Provider, model, and
   effort live in the runbook's literal commands.
9. **Default agent untouched.** Runbook panes never activate `default`; its rules apply
   to a role only where that role's selectors include them.
10. **Bundled `orchestration-v1`.** Superseded by decision 14 on 2026-09-23; the original
    text follows for the audit trail. The live tab-0 layout with the reviewer pane
    removed: outer horizontal split (ratio 0.44); left column vertical split
    orchestrator over assistant (0.51); right column vertical split (0.35) with
    dispatcher (now the lane manager, ruling 22) | researcher on top (0.50) and monitor
    alone below. Reporting lines as they settled last night: the lane manager and
    monitor report to the assistant; the assistant and alarms reach the orchestrator.
11. **Deferred to the Rust port.** DB-backed runbooks (Stage 2 family crate); Rust
    activation and roster (S2.8, S2.11); `apply_agent_definition` in Rust (S2.12;
    dropped by decision 20, so the S2.12 item is the launch's placement seam instead);
    retiring `gobby build` in favor of runbooks.
12. **Command mode replaces the loader.** The orchestrator's design, sent on Josh's
    word on 2026-09-22 ("send it to them, it's good"): `gclient <verb> [args]` with
    no TUI connects to the daemon the way the TUI does, sends one workspace op, prints
    the reply's `result`, and exits non-zero on a refused op. Every verb maps to an
    existing op, so the daemon gains nothing (amended by decision 20 on 2026-09-24: the
    `launch` verb posts the daemon's HTTP tool endpoint for `spawn_agent`, which gains
    1.5's `placement` and `sandbox` parameters; every other verb still maps to an
    existing op). Inside a gclient pane the defaults come
    from the pane env (`GOBBY_PANE_REF`, `GOBBY_WORKSPACE_ID`), so a runbook script
    needs no flags; the daemon URL comes from bootstrap through
    `gobby_core::daemon_url`, because the pane env deliberately carries no
    `GOBBY_DAEMON_URL`. The "Load runbook…" menu item, picker dialog, YAML parser,
    and replay state machine of the first draft are dropped. Auth is the local token,
    so remote targets wait for #20202 (S4.3). Work order: 1.4, then 2.1, then 1.1 to
    1.3, then P3 (amended by decision 20: 1.1, 1.2, 1.5, then 2.1, then 1.3, then P3).
13. **Roster and council flow** (supersedes 8; Program Director rulings Q1 and Q6 of
    2026-09-23, message `62c8f875`, applying #22691 criterion 10, Golden Path rulings 15,
    21, 22 and 23, and the flow Josh wrote into #22808). Its council part (the three
    persona blocks on existing definitions, the researcher lookup pane and the verifier
    checks in the adversary's report) is superseded by decision 18 on 2026-09-24; the
    text follows for the audit trail. Eight persona definitions:
    `program-director`, `assistant`, `lane-manager`, `researcher`, `reviewer`, `archivist`,
    `log-monitor`, `elicitor`. The plan writer, enhancer and adversary are persona blocks on
    the existing `planner`, `plan-enhancer-taskless` and `plan-adversary-taskless`. No
    `post-epic-reviewer` (ruling 23: the Program Director reviews and lands every
    candidate) and no `plan-verifier`: its mechanical checks (`gobby plans validate`,
    `git diff --check`, the plan sha256, the coverage-contract checks) move into the
    adversary's finalization report. Council flow: the writer, enhancer and adversary
    debate to consensus with no round cap; the adversary finalizes to the Program
    Director, who may send it back; the assistant takes it to Josh; expansion waits for
    Josh. The fourth council pane is a read-only lookup helper on the `researcher`
    definition. Provider, model and effort stay in the launch lines (decision 4). 3.1's
    `agent_scope` follows this roster: every role except `program-director`, whose seat
    carries the delegate nudge; the assistant keeps its `docs/` and `.gobby/plans/`
    markdown carve-out and the planner and elicitor get a `.gobby/plans/` one.
14. **Layout** (supersedes 10; ruling Q2 of the same message). Two bundled scripts on
    2.1's verbs, both mirroring the live workspace of 2026-09-23: `orchestration-v1.sh`
    builds a `control` tab (program director | assistant, horizontal 0.50) and a
    `monitors` tab (log monitor over archivist, vertical 0.50); `plan-council-v1.sh <plan>`
    builds one council tab named after the plan with four panes (writer on top, vertical
    0.50; below it enhancer | adversary | mechanic at equal widths; the mechanic pane
    replaced the researcher lookup pane in decision 18). Lane developer
    panes stay out of both scripts: the Program Director opens them per lane with
    `/goal` (ruling 23).
15. **Pending rulings Q3-Q5** (same message). #22713 folds as no block: a claiming
    session may amend its own task's `description`, `validation_criteria` and `labels`,
    the claim-time values must reach the close reviewer durably, and the disclosure order
    is injected at the edit; the durable carrier is chosen from the gobby#14332 lookup
    (an existing task history or audit surface first), and `task-close-reviewer.yaml`
    leaves the Non-goals only if the reviewer does not already receive that surface.
    Terminal backend (#22691 criteria 1-7, ruling 16): the 3.2 test that no definition
    names a backend and the registration text fold now; the runbook-script gterm
    assertion is dropped if workspace panes cannot fall back to tmux at all. #22695 stays
    2.1's leaf and keeps its gclient-lane slot: expansion adopts it if
    `start_expansion_run` can adopt an existing leaf, otherwise 2.1 cites it as existing
    work outside expansion.
16. **Q4 and Q5 resolved by lookup** (gobby#14332, message `1e77d597`, 2026-09-23).
    Q4: workspace panes cannot fall back to tmux. `WorkspaceOps._pane_source` resolves
    the native gterm host unconditionally and a missing host is refused as
    `terminal_failed`; only `spawn_agent` resolves a backend. So no script asserts a
    backend, and criteria 1-7 map as the Constraints entry "Terminal backend" says.
    Q5: `apply_run` always mints children and cannot adopt an existing leaf, so 2.1 stays
    a deliverable; when expansion mints 2.1's task the Program Director closes #22695
    (gclient command mode) as superseded by it and the gclient-lane slot passes to the
    minted task. Writer position, named to the Program Director with the hash: the
    alternative, 2.1 as `kind: deferred` with `task_ref: "#22695"`, would drop 2.1's
    acceptance coverage. Q3: no durable surface records edits to a task's
    `description`, `validation_criteria` or `labels` (`task_lifecycle_events` holds state
    transitions, `task_validation_history` holds run outcomes) and the close reviewer
    receives none, so 1.1's migration adds the trigger-written
    `task_artifacts.claim_snapshot` and 3.3 surfaces it; `task-close-reviewer.yaml`
    leaves the Non-goals for that one step. Writer position, named with the hash: the
    column and trigger ride migration 451 rather than a 452 of their own, so V1 step 2
    stays one cutover.
17. **Execution model, as-is and to-be** (Josh, 2026-09-24 09:3x CDT, via the
    assistant; memory 556ec801; the D2 review that reopened #22808 as R5). Josh,
    verbatim: "Everything is an interactive session now unless it's a one-shot (like
    feature_low/mid/high). Everything is an agent running in a pane except one-shots.
    Therefore, every agent needs the ability to execute a step workflow." As-is: step
    programs run only for spawned sessions (`build_persona_changes` sets
    `step_workflow_complete` only when `is_spawned`; `activate_default_agent` and 1.4's
    `apply_agent_definition` both call it with `is_spawned=False`). To-be: every
    non-one-shot agent, the interactive roles included, executes a step workflow.
    Lifting the `is_spawned` gate is an engine-wide capability and belongs to a sibling
    plan (interactive step-workflow execution); this plan does not grow into it. The
    lane-manager and council roles ship as persona prompts in v1 and adopt step
    workflows when the sibling plan lands, so no role definition here assumes it stays
    persona-only. Plans keep current behavior (as-is) apart from target design (to-be):
    a code fact never stands as a non-goal or constraint when it contradicts the to-be
    model, which is why the former non-goal "No `step_workflow` on the interactive
    roles" is gone.
18. **Council definitions** (Josh, 2026-09-24 09:4x CDT, via the Program Director;
    supersedes the council part of 13 and the verifier part of ruling Q6). Josh,
    verbatim: "The runbooks plan should retire plan-enhancer-taskless and
    plan-adversary-taskless, or rename them. We need plan-writer, plan-enhancer,
    plan-adversary, and plan-mechanic." The council roster is exactly those four
    definitions, delivered by 3.4. On HEAD, `plan-enhancer.yaml` and `plan-adversary.yaml`
    already exist as gobby build's stage-native planning definitions (task-bound `claim`
    and `terminate` steps; `stages.yaml` line 38 names `plan-adversary` as the planning
    stage's reviewer), `planner` is that stage's `default_agent` (line 37), and
    `plan-writer` and `plan-mechanic` have no definition to build on (`plan-mechanic` is
    today only a folded skill name, `catalog.json` line 1041, that resolves to
    `references/plan/repair.md`). So `plan-writer` and `plan-mechanic` are new
    persona-surface definitions (the writer's body carries `planner`'s persona and the
    book's section 9; `planner` stays untouched for the spawned drafting runs and the
    build stage, Non-goal 1; amended by decision 19: `planner.yaml` is renamed to
    `plan-writer.yaml` and rewritten); the council enhancer and adversary are persona
    blocks on the existing `plan-enhancer` and `plan-adversary` (already `surfaces:
    [spawn, persona]`; amended by decision 19: both are rewritten as council definitions
    without their stage-native step workflows); and `plan-enhancer-taskless` and
    `plan-adversary-taskless` are retired, every reference migrated (3.4 lists them). `plan-mechanic` owns the mechanical
    checks that ruling Q6 had moved into the adversary's finalization report: `uv run
    gobby plans validate`, `git diff --check`, the plan sha256 against the committed
    blob, the coverage manifest and coverage-contract checks, and re-verifying citations
    and literals against HEAD; the adversary keeps the substantive attack and cites the
    mechanic's pass lines in its finalization. The mechanic takes the fourth council
    pane; `researcher` leaves the council and keeps its chart seat. To-be, deferred past
    v1 (Josh, verbatim: "mechanic could also be used for codebase research. that way we
    don't have four planning agents all doing codebase exploration for their parts. idea
    at least. we'll have time to optimize it after we get it working."): the mechanic
    produces one shared evidence pack (file and symbol map, line citations, current
    behavior) that the writer and enhancer consume; the Program Director's constraint,
    recorded with it: the adversary may use the pack but keeps independent code
    spot-checks, so a shared wrong premise cannot pass every role. Not built in v1.
19. **Retirement of `gobby build` assumed** (Josh, 2026-09-24 10:2x CDT via the
    assistant, relayed by the Program Director in message `a8a51d12`; memory 6102cd1d).
    Josh, verbatim: "assume we're retiring gobby build and stages (we won't actually
    until we have runbooks where we need them, but I'm the only user and I haven't
    touched gobby build in months). if we need old definitions we can grab them from git
    history." The Program Director's reading, folded in full: compatibility with `gobby
    build`, the stage registry and `src/gobby/dispatch/` is no longer a constraint,
    while the retirement itself stays D2 (v1 deletes nothing of `gobby build`); the
    council roster is exactly `plan-writer`, `plan-enhancer`, `plan-adversary` and
    `plan-mechanic`, each shaped for the council (3.4): `planner.yaml` is renamed to
    `plan-writer.yaml` and rewritten, the stage-native `plan-enhancer.yaml` and
    `plan-adversary.yaml` are rewritten as council definitions, and the two `-taskless`
    files are deleted; old bodies stay recoverable from git history, so nothing is kept
    for reference; where `stages.yaml`, `src/gobby/dispatch/` or tests still name a
    changed or removed definition, the reference is repointed or deleted the cheapest
    way that keeps sync, validation and the named tests passing (each touched consumer
    is a 3.2 or 3.4 target); preserving `gobby build`'s stage behavior is not required.
    The same reading reaches `researcher.yaml` (3.2, round file position f): its
    discovery-stage step workflow goes with the stage behavior. Non-goal 1 is restated
    as as-is/to-be, and every workaround it forced (the "keep the stage-native bodies"
    rule, the `PROMPT_BUILDERS` consumers-unchanged inventory, the planner split, the
    taskless-mode reasoning) is redone plainly in 3.4. The Program Director's R5 note
    about a council-mode step program apart from the task-bound build one was never
    recorded in this plan or the round file, so there is nothing to drop.
20. **Runbook launch through the spawn path: placement, sandbox, worktrees, the one
    unsandboxed role, existing workflows** (Josh, 2026-09-24 10:3x to 11:0x CDT, five
    directives relayed by the Program Director; each recorded verbatim in the round
    file, R6). Josh, verbatim: "The runbooks need to be able to launch clis in sandboxed
    panes, just like spawn_agent does." "sandbox restrictions need to be part of the
    runbooks". "for now, you'll be the un-sandboxed agent. your the only one that can
    write code in 0.5.0 (every other agent makes changes inside a worktree when the
    runbooks are in place)." "I don't want to design the new workflows yet, I just want
    to be able to launch panes through spawn_agent or through the cli." "and have
    existing workflows work since 1 is true".
    As-is: only `spawn_agent` launches a CLI under the managed SRT sandbox
    (`prepare_sandbox_launch`, wrapped once before backend dispatch); `spawn_agent` has
    no pane placement (a spawned terminal is not a workspace pane; workspaces decision
    record #10); a workspace pane is a `zsh` the daemon spawns (`WorkspaceOps._fill`),
    and the R5 runbook typed each seat's launch line into that shell, unsandboxed; a
    session's step program is instantiated only when it is spawned (decision 17).
    To-be, delivered by this plan (1.5, 2.1, 3.1, 3.2, 3.4): a runbook pane launches
    through `gobby-agents:spawn_agent` or through the CLI (`gobby agents spawn`, and
    `gclient launch` in command mode), and both give the same result: the role's
    definition, the sandbox policy the role declares, its worktree binding, and
    placement in the requested tab or split. The two paths share one implementation,
    `spawn_agent_impl`, which gains `placement` and `sandbox` parameters; the CLI paths
    post the daemon's HTTP tool endpoint for that tool. A launched seat is a spawned
    session, so its definition's existing step workflow runs exactly as it does for
    `spawn_agent` today, and a definition with no step workflow stays persona-only; no
    new workflow is designed here, decision 17 stays as-is/to-be, and every role block's
    step-workflow field reads "persona prompt in v1". Sandbox: every launch starts the
    CLI under the managed SRT sandbox through the spawn path's own resolution (memories
    827ab8eb, f08d0c93, fe8aa2a8, 4a00633f); a role's restrictions (read and write deny
    roots, extra read or write roots, network and package-registry allowances) are
    declared next to the role in its definition's `sandbox` block, using
    `SandboxConfig`'s own field names and no new format, and a launch line may add or
    narrow them; a launch with no block gets the same default policy as `spawn_agent`;
    the exemption is declared only in a definition's block, `enabled: false` with a
    `reason`, and no launch line can switch a sandbox off (adversary A1, R7); only
    the bundled template sync writes that key or a row that carries it, every other
    definition write path refuses a body carrying it and any write to a row that
    carries it, and a launch honors it only from a row the sync owns and only for a
    caller that is not itself sandboxed (PD S1, R8). The
    one unsandboxed role is `program-director`, with the reason in its block: daemon
    restart, cutover, binary promotion into `~/.gobby/bin`, pushes, and read-only hub
    `psql`; a sandbox is inherited by child processes, so a daemon restarted from a
    sandboxed pane would start inside it or fail. It is held in instead by role-scoped
    hook rules in 3.1's runbook group (`agent_scope: [program-director]`): a claimed
    task before commits, explicit-path commits only (no `git add -A`, no stash), no SQL
    writes against the hub, restarts and cutovers only through the gobby CLI, push only
    to `origin`, and no directory deletion. Worktrees: only the program-director writes
    code on 0.5.0 in the main checkout; every other role that changes code works in its
    own worktree, which the launch creates or reuses through the existing `isolation:
    worktree` path; the twelve runbook roles change no code, so their worktree field is
    "none", and lane developer panes (`/goal`) are the worktree case that 1.5's
    acceptance proves. To-be, recorded and not designed because Josh is still shaping
    it: typed privileged commands, roughly `gobby ops restart|cutover|promote|push`,
    that a sandboxed caller can trigger and that run outside the sandbox; once they
    exist the program-director can be sandboxed too; no deliverable. The Non-goals "No
    pane placement for `spawn_agent`" and "No change to the gclient direct-input
    keystroke path" are restated as as-is/to-be; decision 1's `apply_agent_definition`
    sentence, decision 4's typed launch lines, decision 5's `pane.set_role` and decision
    12's "the daemon gains nothing" are amended in place; deliverable 1.4 is a retired
    slot and the launch is 1.5.
21. **Per-role specification and continuity** (Josh, 2026-09-24 10:5x and 11:04 CDT,
    relayed by the Program Director; #22660 folded on Josh's decision). Josh, verbatim:
    "and the runbook plan needs to include all of the definitions for each role, what
    they can do, how their sandbox works, rules they use, etc." Every role a runbook can
    launch (3.2's eight chart roles and 3.4's four council roles) has one block with the
    same ten fields, and nothing about a role lives only in prompt prose: (1)
    definition: name, surfaces, isolation, and the launch line's provider, model and
    effort; (2) persona: purpose and what it owns, with the prompt-book divergence,
    registration and creation lines of 3.2; (3) can do: allowed and blocked MCP tools,
    blocked native tools, and the code-edit right (only the program-director on 0.5.0;
    a coding role in its worktree); (4) sandbox: the restriction block, or unsandboxed
    with the reason; (5) rules: the selector lists, every scoped or named rule that
    applies with its `agent_scope`, existing and new, and the names excluded; (6) skills
    loaded and whether the core-skill bootstrap is on; (7) worktree binding; (8)
    messaging: who it reports to and wakes, and what it may send Josh; (9) step
    workflow: persona prompt in v1, step workflow to-be (decision 17); (10) continuity
    (#22660): whether the role compacts or clears between tasks, when, and how it
    recovers its operating instructions after `set_handoff(clear_session=true)`. The
    continuity mechanism is the same for every role: the operating instructions (role
    prompt, reporting lines, `EVENT=` and `VERDICT=` formats) are the definition's
    prompt text, re-applied at the clear successor's SessionStart by 1.3 from the pane
    row and delivered once on the successor's first turn through the `persona` surface;
    the handoff carries state only (what was in flight), never instructions. Chosen
    over the handoff carrying the prompt (agent-cooperative, lost on a crash relaunch,
    and a second copy of the text under the exactly-once delivery contract of memories
    81ab8aca and 27fe9c9f, which delivers one continuation pull and nothing else) and
    over a runbook file the successor must read (cooperative again, and a fresh session
    has no reason to know the file). Roles that clear between tasks: `researcher` after
    each delivered `EVENT=REPORT`, `reviewer` after each `EVENT=CANDIDATE_VERDICT`,
    `plan-mechanic` after each final-hash report, and the council writer, enhancer and
    adversary between plans, never mid-round; the program-director, assistant,
    lane-manager, log-monitor, archivist and elicitor compact and never clear between
    tasks, because their context is their state. Josh's standing preferences fold into
    the roles they govern: the assistant's memories 39b1c075 and a620b843, the
    program-director's hook-rule set (decision 20). 3.2 and 3.4 each carry a validation
    acceptance item proving every shipped definition matches its block, and 3.2 carries
    the live clear-successor check #22660 asks for.

## Non-goals
`kind: framing`

- `gobby build`, as-is and to-be (decision 19): as-is, `gobby build`,
  `src/gobby/dispatch/` and the stage registry still exist and this plan deletes none
  of them; to-be, runbooks replace them (D2). Compatibility with them is not a
  constraint: 3.2 and 3.4 repoint or delete every reference to a changed or removed
  definition the cheapest way that keeps the tree green. `epic-reviewer.yaml` and
  `default.yaml` are untouched; `task-close-reviewer.yaml` changes only by 3.3's
  amendment step.
- No web UI, no runbook CRUD over MCP or REST, no runbook rows in PostgreSQL.
- Pane placement for `spawn_agent`, as-is and to-be (decision 20): as-is,
  `spawn_agent` places nothing (workspaces decision record #10 described that state);
  to-be, 1.5 gives it `placement` (a new tab, or a split beside a named pane) and both
  CLI paths use it.
- The gclient direct-input keystroke path, as-is and to-be (decision 20): as-is, the R5
  scripts typed launch lines through the daemon's write coordinator; to-be, a launch
  types nothing (the daemon spawns the CLI into the pane) and `send-keys` stays a
  tool-pane verb on the same coordinator. The keystroke path itself is unchanged.
- No multi-node runbooks; tabs load into the current node's current workspace.
- No YAML runbook format, picker dialog, or menu item in 1.x (decision 12).
- No new step workflows and no looping or cyclic step programs (decision 20);
  interactive step-workflow execution stays decision 17's sibling plan.

## Constraints
`kind: framing`

- **Size guard.** Hand-maintained production files stay under 1,000 lines and the hook
  blocks threshold-crossing writes. Measured 2026-09-24 at `f7b9ccc54c`:
  `src/gobby/terminals/workspace_ops.py` 973, `src/gobby/storage/workspaces.py` 973,
  `src/gobby/mcp_proxy/tools/spawn_agent/_implementation.py` 964,
  `src/gobby/agents/spawn_executor.py` 962, `src/gobby/agents/lifecycle_monitor.py` 956,
  `crates/gclient/src/daemon/live.rs` 984, `crates/gclient/src/app/live_loop/menu.rs` 970,
  `crates/gclient/src/app/mod.rs` 950, `crates/gclient/src/app/live_loop.rs` 939,
  `crates/gclient/src/daemon/mod.rs` 924, `crates/gclient/src/app/live_loop/actions.rs`
  895. Deliverable 1.2 carries genuine splits of the two 973-line modules; 1.5 adds call
  lines only to `_implementation.py` and `lifecycle_monitor.py` and puts its logic in
  new modules (`spawn_agent/_placement.py`, `spawn_agent/_sandbox_block.py`,
  `agents/pane_runs.py`, `terminals/workspace_launch.py`); `spawn_executor.py` is not
  targeted; 2.1 lives in new `command` modules and touches `startup.rs` (691 lines) by
  two symbols and `daemon/rest.rs` (317) by one helper. gclient enforces the ceiling in
  `crates/gclient/tests/source_size.rs`.
- **Schema authority is gcore.** A migration is live only after the five derived
  carriers are refreshed (docs/contracts/plan-coverage.md derived-carriers row) and the
  binary set is rebuilt and promoted via `uv run gobby cutover` from the main checkout,
  announced with a `global` send_message first (memory f71268af).
- **WS op discovery.** `WORKSPACE_OPS` in `src/gobby/servers/websocket/workspace_ws.py`
  is derived by `inspect.getmembers(WorkspaceOps, inspect.iscoroutinefunction)` (inherited
  members included), so a mixin base class keeps every op name; new keyword params need
  plain class or `X | None` hints (`_fields` rejects anything else at import).
- **Daemon order of activation.** In `activate_materialized_session` the native
  terminal bind (`discover_and_bind_external_terminal`, then `retry_native_terminal_bind`)
  runs before `_activate_default_agent`, so a terminals-row lookup at activation time
  sees the deferred bind too.
- **Client couples through the public API only** (ROADMAP target architecture). gclient
  reads runbook files locally, issues ordinary workspace ops over the WS API, and, for
  `launch`, posts the daemon's HTTP tool endpoint
  (`POST /api/mcp/gobby-agents/tools/spawn_agent`, registered at
  `src/gobby/servers/routes/mcp/tools.py` line 53) with the local token, the same REST
  surface its TUI already uses for project lookups (decision 20); nothing else.
- **Daemon `_place` semantics** (`src/gobby/storage/workspaces.py` `_place`,
  `_with_ratio`): `pane.split` replaces the `beside` leaf with `split(axis, 0.5,
  [beside, NEW])`, so the new pane is always the second child, and `pane.resize` sets
  the ratio of the split whose direct child is the named pane. Each resize must follow
  its split while both children are still leaves.
- **Validation commands.** Python: `DATABASE_URL=postgresql://gobby_test:gobby_test@127.0.0.1:60892/gobby_test GOBBY_TEST_PROTECT=1 uv run pytest <file>`,
  `uv run ruff check src/ tests/`, `uv run mypy src/`. Rust: `cargo nextest run -p gobby-client`,
  `cargo nextest run -p gobby-core --features postgres -E 'test(schema)'` with
  `GOBBY_SCHEMA_TEST_DATABASE_URL` set to the test-hub DSN named in 1.1's carrier procedure
  (gcore's default features are empty and its schema tests are
  `#![cfg(feature = "postgres")]`, so without the feature the command compiles no test
  and passes vacuously),
  `cargo clippy -p gobby-client`, `cargo fmt -p gobby-client -- --check`. Never the full
  pytest suite.
- **Terminal backend (#22691 criteria 1-7).** Every fresh workspace pane is created by
  `WorkspaceOps._pane_source` (`src/gobby/terminals/workspace_ops.py` 754-790), which
  resolves the native gterm host unconditionally (771); when the host is absent, `_fill`
  (823-873) surfaces `WorkspaceOpError("terminal_failed", ...)` and nothing falls back
  (`docs/guides/gterminal-development-guide.md` 91-190). Only `spawn_agent` resolves a
  backend (`resolve_terminal_backend`, `src/gobby/agents/spawn_models.py` 26-38). So:
  criterion 1 is 3.2's test that no definition names a backend; 3 is 3.2's registration
  line reading 1.2's `backend` pane field; 5 holds by construction, because a persistent
  role's pane cannot come up on tmux and a refused `new-tab` or `split` prints
  `terminal_failed: <reason>` and exits 1 (2.1.3, V1 step 7); 2, 4, 6 and 7 describe
  `spawn_agent`'s fallback, which is #21565's (Golden Path ruling 16) and outside this
  plan. The one tmux path into a runbook pane is `terminal_id` adoption of a
  pre-existing terminal (`_adoptable`, 792), which the registration line reports. No
  script asserts a backend. 1.5's launch forces the native backend whenever `placement`
  is given (a pane needs the gterm host) and refuses `terminal_backend: tmux` with
  placement; without placement `spawn_agent` is unchanged.

## P1: Daemon seams
`kind: framing`

Thin Python plus one gcore migration. 1.1 is schema only; 1.2 is the workspace rows
and the derived roster fields; 1.3 is SessionStart activation for the `/clear`
successor; 1.4 is a retired slot (its tool was superseded by the launch, decision 20);
1.5 is the launch itself: `spawn_agent` placement, the role's sandbox block, and the
lifecycle of a pane-bound run. Work order: 1.1, 1.2, 1.5, then 2.1, then 1.3, then P3.

### 1.1 Migration 451: pane role, tab runbook and claim snapshot columns [category: code]
`kind: deliverable`

Targets:
- `crates/gcore/assets/schema/migrations/451_add_runbook_roles.sql`
- `crates/gcore/src/schema/assets.rs::MIGRATIONS`
- `crates/gcore/assets/schema/catalog.manifest.json::*` — scope-reason: regenerated by the manifest freshness test
- `crates/gcore/src/grant/bundle.rs::*` — scope-reason: golden checksum, root hash, and latest_version literals move together
- `crates/gcore/tests/schema_contract.rs::*` — scope-reason: the identity literals move and the byte-limit test is added beside them
- `crates/gdaemon/tests/cli_contract.rs::version_json_reports_exact_schema_identity_contract`
- `src/gobby/storage/schema_expected_identity.json::*` — scope-reason: regenerated from the rebuilt gdaemon
- `tests/runtime_grants/golden/brokered_datastores.json::*` — scope-reason: re-signed at identity 451 (`schema_identity`, `payload_checksum`, `signature`)
- `tests/runtime_grants/golden/direct_datastores.json::*` — scope-reason: re-signed at identity 451
- `tests/runtime_grants/golden/old_client_new_grant.json::*` — scope-reason: re-signed at identity 451
- `tests/runtime_grants/golden/payload_skew_unknown_field.json::*` — scope-reason: re-signed at identity 451; its unknown field stays outside the signed payload
- `tests/runtime_grants/golden/unavailable_datastores.json::*` — scope-reason: re-signed at identity 451

Add migration 451: `ALTER TABLE workspace_panes ADD COLUMN role text` and
`ALTER TABLE workspace_tabs ADD COLUMN runbook text`, each with an `octet_length`
CHECK whose shape is copied from `workspace_panes_label_byte_limit` in migration 440
(`octet_length(label) <= 1024`, `440_add_workspaces.sql` 49-50). The limits themselves
are new, with no 128-byte precedent to copy: 128 bytes for `role`
(`workspace_panes_role_byte_limit`), 1024 for `runbook`
(`workspace_tabs_runbook_byte_limit`); `label` stays at 1024. No new GRANT: 440 already
granted DML on the three tables. The byte-limit test applies the embedded migrations
to the scratch database the way `catalog_manifest_freshness` does and inserts
over-limit rows.

The same migration carries 3.3's #22713 carrier: `ALTER TABLE task_artifacts ADD COLUMN
claim_snapshot text` (no CHECK; the value is trigger-written JSON) and the trigger pair
`tasks_claim_snapshot_ai AFTER INSERT ON tasks FOR EACH ROW WHEN
(NEW.claimed_by_session_id IS NOT NULL)` and `tasks_claim_snapshot_au AFTER UPDATE OF
claimed_by_session_id ON tasks FOR EACH ROW WHEN (NEW.claimed_by_session_id IS NOT NULL
AND NEW.claimed_by_session_id IS DISTINCT FROM OLD.claimed_by_session_id)`, both
executing the new `snapshot_task_claim()` (`LANGUAGE plpgsql`, the shape of
`refresh_task_state_bucket_from_task` and its `tasks_state_bucket_ai`/`_au` pair,
baseline.sql 5338-5340): `INSERT INTO task_artifacts (task_id, claim_snapshot) VALUES
(NEW.id, jsonb_build_object('claimed_at', now(), 'session_id', NEW.claimed_by_session_id,
'description', NEW.description, 'validation_criteria', NEW.validation_criteria,
'labels', NEW.labels)::text) ON CONFLICT (task_id) DO UPDATE SET claim_snapshot =
EXCLUDED.claim_snapshot, updated_at = now()`. A row of only `task_id` and
`claim_snapshot` satisfies `task_artifacts_check` (every isolation pair NULL). The
trigger covers every claim path with no Python call site: the insert with a claimant
(`src/gobby/storage/tasks/_creation.py`), `claim_task` and `claim_task_for_agent`
(`_transitions.py` 177-251), `update_task`'s `claimed_by_session_id` parameter
(`_manager.py` 348), and the stage transitions (`_stage_state_transitions.py`
638-650). It rides this migration so V1 step 2 stays one cutover (decision 16).

**Granularity:** more than six production Target files: one migration with the identity, contract and manifest artifacts the schema pipeline regenerates from it; none is closeable without the others.

Research context:
- Precedent commit `541320efc2` (migration 444) shows the carrier procedure; there is no
  single regenerate command. Order: (1) append an `EmbeddedMigration { version: 451,
  filename, checksum: <sha256 of the file bytes>, sql: include_str!(...) }` entry to
  `MIGRATIONS` in `crates/gcore/src/schema/assets.rs`; (2) regenerate the catalog
  manifest with the env-gated test
  `GOBBY_SCHEMA_TEST_DATABASE_URL=postgresql://gobby_test:gobby_test@127.0.0.1:60892/gobby_test UPDATE_GCORE_SCHEMA_MANIFEST=1 cargo test -p gobby-core --features postgres --test catalog_manifest_freshness catalog_manifest_is_fresh_for_embedded_assets`
  (the test creates a scratch database on that server, applies baseline plus migrations,
  writes `catalog.manifest.json` and drops the database; without the DSN it skips with
  an eprintln, and without `--features postgres` it compiles no test at all and the
  manifest is never rewritten, because gcore's default features are empty
  (`crates/gcore/Cargo.toml`), `pub mod schema` is gated on `postgres` (`lib.rs` line
  46), and `schema_contract.rs` and `catalog_manifest_freshness.rs` are
  `#![cfg(feature = "postgres")]`);
  (3) `cargo build -p gobby-daemon` then `target/debug/gdaemon schema version --json`
  (the `schema` subcommands are `apply`, `plan`, `sweep-test-schemas`, `verify` and
  `version`; `schema-identity` is gcode's)
  prints `latest_checksum` and `assets_root_hash` (`root_hash()` covers baseline,
  migrations, seed, and manifest, so it is final only after step 2); (4) update
  `GOLDEN_LATEST_CHECKSUM`, `GOLDEN_ASSETS_ROOT_HASH`, and `latest_version: 451` in
  `crates/gcore/src/grant/bundle.rs`, the literals in `schema_contract.rs`, and
  `latest_version` in `cli_contract.rs`; (5)
  `uv run python scripts/generate_schema_expected_identity.py --gdaemon target/debug/gdaemon`
  rewrites `src/gobby/storage/schema_expected_identity.json` (CI re-derives and compares);
  (6) re-sign the five golden grant vectors under `tests/runtime_grants/golden/`: they
  embed the signed schema identity, so every identity bump regenerates them
  (`tests/runtime_grants/test_golden_vectors.py` lines 3-6; precedents `2b7cf60e79` for
  449 and `ec61574731` for 450, one line in each of the five files). For each file set
  `schema_identity` to the new identity, recompute `payload_checksum` and re-sign with
  `sign_grant(..., GOLDEN_SECRET)` (`gobby.runtime_grants`; `GOLDEN_SECRET` is
  `tests/runtime_grants/support.py` line 13); only the identity and those two fields
  change, and the skew vector's unknown field (`future_capability_probe`, the one
  `NEGATIVE_GOLDENS` entry) stays outside the signed payload.
- The newest committed migration on 0.5.0 is `450_drop_session_heuristic_title.sql`
  (#22740, drops `sessions.heuristic_title`; `b9303f8183`, merged in `8de59a666f` on
  2026-09-24); 449 added `workspaces.default_project_id` and refs became zero-based in
  442. This plan takes 451 (Program Director, 2026-09-24). On HEAD the
  `#[cfg(not(feature = "postgres"))]` fallback in `crates/gcore/src/grant/bundle.rs`
  carries `latest_version: 450` (line 218, set by #22740 in `b9303f8183`) beside the 450
  `GOLDEN_LATEST_CHECKSUM` and `GOLDEN_ASSETS_ROOT_HASH` (lines 15-18); step (4) moves
  the three together from 450 to 451, and `schema_contract.rs` moves four (the version,
  the newest file name, the latest checksum and the assets root hash). Its guard test
  `grant::tests::expected_schema_identity_tracks_catalog_head` exercises the fallback only
  under gcore's default features, so V1 step 1 runs it without `--features postgres`
  (memory 24090e86).
- Live: the migration applies at the next `uv run gobby cutover`; `cutover` refuses
  uncommitted schema inputs, so commit the migration and carriers first.
- `task_artifacts` is keyed by `task_id` (`task_artifacts_pkey`, baseline.sql
  4617-4618) with an `ON DELETE CASCADE` foreign key to `tasks` (5709-5710) and
  `SELECT, INSERT, DELETE, UPDATE` granted to `gobby_daemon_runtime` (6460), so the
  trigger's upsert needs no new grant. `tasks.labels` is `jsonb`, `description` and
  `validation_criteria` are `text`, and there is no `claimed_at` column, so the snapshot
  stamps `now()`.
- Rejected: reusing `label` as the role (display text is not a definition name);
  storing the role on the terminals row (a pane outlives CLI relaunches, a terminal row
  does not necessarily).

**Acceptance:**

- 1.1.1 - Migration 451 adds nullable `role` on `workspace_panes` and nullable `runbook`
  on `workspace_tabs`, with `octet_length` checks of 128 bytes on `role` and 1024 on
  `runbook`: a 129-byte role and a 1025-byte runbook are rejected, and `label` still
  accepts 1024. file: `crates/gcore/assets/schema/migrations/451_add_runbook_roles.sql`.
  test: `crates/gcore/tests/schema_contract.rs::migration_451_enforces_role_and_runbook_byte_limits`.
- 1.1.2 - `MIGRATIONS` embeds version 451 with its checksum and the catalog manifest
  lists the three columns and the trigger function. symbol: `MIGRATIONS`. file: `crates/gcore/assets/schema/catalog.manifest.json`.
- 1.1.3 - The schema identity contract tests pass with `latest_version` 451. test:
  `crates/gcore/tests/schema_contract.rs::embedded_assets_publish_a_complete_schema_identity`.
  test: `crates/gdaemon/tests/cli_contract.rs::version_json_reports_exact_schema_identity_contract`.
- 1.1.4 - `schema_expected_identity.json` matches the rebuilt gdaemon's identity.
  file: `src/gobby/storage/schema_expected_identity.json`.
- 1.1.5 - Claiming a task writes `task_artifacts.claim_snapshot` holding the claim-time
  `description`, `validation_criteria` and `labels` with the claiming session id; a
  claim by another session replaces it; an update that changes `description` without
  changing the claimant leaves it untouched; an unclaimed insert writes no row. file:
  `crates/gcore/assets/schema/migrations/451_add_runbook_roles.sql`. test:
  `crates/gcore/tests/schema_contract.rs::migration_451_snapshots_task_fields_on_claim`.
- 1.1.6 - The five golden grant vectors carry identity 451 and verify against
  `GOLDEN_SECRET`. test: `tests/runtime_grants/test_golden_vectors.py::test_grant_vectors_round_trip`.
  test: `tests/runtime_grants/test_golden_vectors.py::test_config_revision_signed`.

### 1.2 Workspace rows carry role, runbook and the derived roster fields [category: code] (depends: 1.1)
`kind: deliverable`

Targets:
- `src/gobby/storage/workspaces.py::*` — scope-reason: dataclasses, the tab and pane inserts, the list/lookup queries, and a split of the layout helpers all change
- `src/gobby/storage/workspace_layout.py`
- `src/gobby/terminals/workspace_ops.py::*` — scope-reason: a split of the pane I/O methods; no op gains a parameter
- `src/gobby/terminals/workspace_pane_io.py`
- `src/gobby/mcp_proxy/tools/workspaces/registry.py::*` — scope-reason: the `get_workspace` description names the roster contract
- `src/gobby/cli/workspaces.py::*` — scope-reason: `_pane_line` prints the new fields
- `docs/contracts/gterm-protocols.md`
- `docs/guides/cli-commands.md`
- `docs/guides/gclient-user-guide.md`
- `src/gobby/install/shared/skills/gobby/references/sessions/workspaces.md`
- `tests/storage/test_workspaces.py::*` — scope-reason: add role, runbook and derived-field tests
- `tests/cli/test_workspaces.py::*` — scope-reason: add the pane line test

Storage: `WorkspaceTab` gains a trailing `runbook: str | None = None`; `WorkspacePane`
gains trailing `role: str | None = None` and three derived fields, `session_ref: str |
None = None`, `backend: str | None = None` and `sandbox: dict | None = None` (read from
the row only when the keys are present), so existing constructor sites, including
`tests/servers/test_terminal_ws_golden.py`, are untouched. `_insert_pane` and
`WorkspaceManager.create_tab` accept `role` (and `runbook` on the tab insert) and
`WorkspaceManager.add_pane` accepts `role`; the only caller that passes them is 1.5's
reserve step, because the launch is the only writer of a pane's role and a tab's
runbook (decision 20). The derived fields come from one JOIN in the two read paths that
feed snapshots and activation, `list_panes` and `get_pane_for_terminal`:
`LEFT JOIN terminals tm ON tm.id = p.terminal_id AND tm.state IN ('pending','live')
LEFT JOIN sessions s ON s.id = tm.session_id LEFT JOIN projects pr ON pr.id = s.project_id`,
selecting `COALESCE(NULLIF(btrim(pr.name), ''), s.project_id::text) || '#' || s.seq_num::text`
as `session_ref` (mirroring `Session.ref` in `src/gobby/storage/session_models.py`,
including its strip of `project_name`, so the roster ref is the one `send_message`
resolves), `tm.backend` as `backend` (#22691 criterion 3, with no definition naming one,
criterion 1), and `s.sandbox_enabled` with `s.sandbox_policy_hash` as `sandbox`
(`{"enabled": bool, "policy_hash": str | None}`, the flags 1.5's launch records on the
session row and 1.5 copies to a `/clear` successor), so the roster shows which seats are
sandboxed (V1 step 5). `RETURNING *` paths leave the derived fields None, which is
correct at insert. `to_dict()` stays `asdict` for the existing fields and drops `role`,
`runbook`, `session_ref`, `backend` and `sandbox` when they are None (existing nulls
such as `title`, `terminal_id`, and `label` stay, because the fixtures already contain
them), so the golden corpus in `tests/fixtures/terminal_ws_golden/` is byte-identical
and every event, snapshot, and MCP payload carries the new fields only when set;
gclient ignores unknown keys on decode. `move_pane` and `swap_panes` are not rewritten
to reinsert the pane row: `swap_panes` remaps layout leaves and a cross-tab `move_pane`
updates `tab_id` and `ref` only, so the role column survives both (decision 1) and is
lost only with the row on `remove_pane`. Split: move the layout helpers (`LayoutLeaf`,
`LayoutSplit`, `validate_layout`, `layout_pane_ids`, `_place`, `_with_ratio`,
`mint_pane_id`) out of `src/gobby/storage/workspaces.py` into the new
`src/gobby/storage/workspace_layout.py` and update importers, so the 973-line module
stays under the ceiling.

Ops: no workspace op gains a parameter and no `pane.set_role` exists (decision 20,
round file position g): `tab.create` and `pane.split` keep creating shell and tool
panes without a role, and 1.5 reserves and binds role-bearing panes through the storage
calls above. Split: move the pane I/O surface (`PaneOutputWait`,
`IDEMPOTENCY_KEY_PATTERN`, the `WAIT_CAPTURE_*` constants, `pane_send_text`,
`pane_send_keys`, `pane_read`, `pane_wait_for_output`, `_write`, `_pane_terminal`,
`_runtime`) out of `src/gobby/terminals/workspace_ops.py` into a mixin base class in
the new `src/gobby/terminals/workspace_pane_io.py`; `WORKSPACE_OPS` still discovers the
inherited coroutines, which is also what lets 1.5 add its launch mixin. `PaneWrite` and
`write_workspace_pane` already live in `src/gobby/terminals/workspace_writes.py`
(#22722) and stay there; `_write` keeps delegating to them.

Surfaces: the WS snapshot and events carry the new pane and tab fields with no op
change; the MCP `get_workspace` description names the roster contract (tabs carry
`runbook`; panes carry `role`, `session_ref`, `backend` and `sandbox`); `_pane_line`
prints `role`, `session_ref`, `backend` and `sandbox.enabled` when present. Docs: the
snapshot row fields in `docs/contracts/gterm-protocols.md`; the pane line in
`docs/guides/cli-commands.md`; a "Tabs and panes" paragraph in
`docs/guides/gclient-user-guide.md`; the roster fields in the gobby skill's
`references/sessions/workspaces.md`. The golden corpus fixtures are not regenerated.

**Granularity:** more than six production Target files: the new columns and every reader of the layout share one derived roster, and a reader without its column, or the column without its readers, is not verifiable.

Research context:
- Entry points: `WorkspaceOps.tab_create` (372-402), `pane_split` (460-490), `_fill`
  (823-873, returns the `set_pane_terminal` row, which already carries `role`), `_emit`
  (721-738); `WorkspaceManager.create_tab` (600-638), `add_pane` (722-735), `list_panes`
  (570-580), `get_pane_for_terminal` (888-893); `TerminalManager.bind_session` writes
  `terminals.session_id` (`src/gobby/storage/terminals.py` 663).
  `WorkspaceOps.workspace_snapshot` (332-368) reads through `list_tabs`/`list_panes`,
  so snapshots carry the new fields without further edits. Line numbers are 2026-09-23
  navigation hints only; `workspace_ops.py` is 973 lines at `f7b9ccc54c`.
- `sessions.sandbox_enabled` (baseline.sql 3505) and `sessions.sandbox_policy_hash`
  (3506) are written by `_record_actual_sandbox_enforcement`
  (`src/gobby/agents/spawn_executor_support.py` 245-262) on every spawn.
- Only external constructor site of the dataclasses: `tests/servers/test_terminal_ws_golden.py`
  (`WorkspaceTab(` / `WorkspacePane(` around 500-582); it compares `to_dict()` to the
  fixtures, and `crates/gclient/tests/ws_golden.rs` re-encodes the same files byte for
  byte, which is why the new keys are omitted when None rather than emitted as nulls.
- `Session.ref` (`src/gobby/storage/session_models.py`) strips `project_name` before
  falling through to `project_id`; the SQL `btrim` mirrors that.
- `swap_panes` (`workspaces.py` 752-764) remaps layout leaves only; `move_pane`
  (766-805) updates `tab_id` and `ref`; `remove_pane` (737-750) deletes the row.
- Rejected: a bind-time workspace event for `session_ref` (roster consumers read a
  snapshot); a roster service or dedicated `gobby-runbooks` MCP server (get_workspace
  already is the roster); `role`/`runbook` parameters on `tab.create` and `pane.split`
  and a `pane.set_role` op (R5's design; a role written by anything but the launch would
  bind a pane to a definition its process was never launched as, decision 20).
- Planned checks: `DATABASE_URL=... GOBBY_TEST_PROTECT=1 uv run pytest tests/storage/test_workspaces.py tests/terminals/test_workspace_ops.py tests/servers/test_workspace_ws.py tests/cli/test_workspaces.py tests/servers/test_terminal_ws_golden.py`,
  `wc -l` on both split modules under 1,000, ruff, mypy.

Removed acceptance items (decision 20, round file position g): 1.2.2 (role and runbook
parameters on `tab.create` and `pane.split`), 1.2.3 (`pane.set_role`), 1.2.4 (`gobby
panes set-role`) and 1.2.7 (the scripts' `--role` and `--runbook` flags); the numbers
stay retired.

**Acceptance:**

- 1.2.1 - `WorkspaceTab.runbook` and `WorkspacePane.role` round-trip through
  `create_tab(runbook=...)` and `add_pane(role=...)`; `list_panes` derives
  `session_ref` as `<project>#<seq>` for a pane whose terminal is bound to a session,
  with the project name stripped and an empty name falling through to `project_id`,
  `backend` from the same terminal row, and `sandbox` from the session's two flags;
  `to_dict()` includes a set role and runbook and omits the five keys when None; a role
  survives `swap_panes` and a cross-tab `move_pane` and is gone after `remove_pane`.
  test: `tests/storage/test_workspaces.py::test_role_and_runbook_round_trip_through_inserts`.
  test: `tests/storage/test_workspaces.py::test_list_panes_derives_session_ref_backend_and_sandbox`.
  test: `tests/storage/test_workspaces.py::test_to_dict_omits_unset_role_runbook_and_derived_fields`.
  test: `tests/storage/test_workspaces.py::test_role_survives_swap_and_move_and_dies_with_remove`.
- 1.2.5 - Layout helpers live in `workspace_layout.py` and pane I/O in
  `workspace_pane_io.py`; both original modules stay under 1,000 lines and the golden
  corpus stays byte-identical with the fixtures unchanged. file: `src/gobby/storage/workspace_layout.py`.
  file: `src/gobby/terminals/workspace_pane_io.py`.
  test: `tests/servers/test_terminal_ws_golden.py::test_python_matches_terminal_ws_golden_corpus`.
- 1.2.6 - Protocol, CLI, and user-guide docs describe `role`, `runbook`,
  `session_ref`, `backend` and `sandbox` as fields the launch writes and the roster
  reads, and `_pane_line` prints them. behavior: "Workspace messages" in
  `docs/contracts/gterm-protocols.md`. test:
  `tests/cli/test_workspaces.py::test_pane_line_prints_role_session_ref_backend_and_sandbox`.

### 1.3 SessionStart activates the pane's role [category: code] (depends: 1.2, 1.5)
`kind: deliverable`

Targets:
- `src/gobby/hooks/event_handlers/_session_start/terminal_runtime.py::*` — scope-reason: add the new pane_role_for_session helper beside the bind helpers
- `src/gobby/hooks/event_handlers/_session_start/materialize.py::activate_materialized_session`
- `src/gobby/hooks/event_handlers/_session_start/flow.py::handle_pre_created_session`
- `docs/guides/agents.md`
- `docs/guides/sessions.md`
- `tests/hooks/test_pane_role_activation.py`
- `tests/hooks/test_session_start_handlers.py::*` — scope-reason: add the native-bind role activation test
- `tests/hooks/test_session_materialize.py::*` — scope-reason: add the retry-bind role activation test

New helper `pane_role_for_session(handler, session_id) -> str | None` (new symbol):
`handler.terminal_manager.get_live_for_session(session_id)` (one indexed query), then
`WorkspaceManager(terminal_manager.db).get_pane_for_terminal(terminal.id)`, returning
`pane.role`. At the two activation call sites the override becomes
`input_data.get("agent_name_override") or pane_role_for_session(handler, session_id)`,
so an explicit override (web-chat launch) still wins and the pane role beats an
existing `_agent_type` and the config default through `resolve_agent_name`. Both call
sites already sit after `retry_native_terminal_bind`, so the deferred-bind path is
covered; a session whose bind was refused twice stays unbound and resolves normally.
Document the known limitation that a tmux-inside-gterm session binds its tmux row, not
the pane's native row, so no role applies there.

Research context:
- `discover_and_bind_external_terminal` (`terminal_runtime.py` 98-134) returns a
  pending tuple only when the bind was refused and `None` on success, so it is not a
  "bound" signal; the terminals row is the cheapest correct source
  (`TerminalManager.get_live_for_session`, `src/gobby/storage/terminals.py` 624-635).
- Call sites: `activate_materialized_session` (`materialize.py` ~397-405) and
  `handle_pre_created_session` (`flow.py` ~665-673), both behind
  `skip_default_agent_activation`.
- `activate_default_agent` (`_session_start/agents.py` 96-241) resolves the name via
  `resolve_agent_name` (override, existing `_agent_type` if not default, config,
  default) and applies `build_persona_changes(is_spawned=False)`: `_agent_type`,
  `_active_rule_names`, `_active_skill_names`, `_skill_format`, definition variables,
  `_agent_blocked_tools`, `_agent_blocked_mcp_tools`. The persona prompt is prepended on
  the first before_agent by `_inject_agent_instructions_if_needed` (`_agent.py`
  256-348) using `prompts.persona`, so every role definition needs the `persona`
  surface (enforced by 1.2's validation).
- Failure mode motivating 1.2's validation: an unresolvable override makes
  `activate_default_agent` log and return None, merging nothing; reconcile later
  backfills `default` with no active rules.
- Compaction keeps the role (same row, same variables); `/clear` creates a successor
  in the same pane, whose SessionStart resolves the same pane row. A launched seat
  (1.5) is a spawned session whose definition was applied at spawn; its `/clear`
  successor is an interactive session with no run link (`_bind_clear_successor`,
  `materialize.py` 152, copies task claims only), so this helper is what re-applies the
  role there, and `_inject_agent_instructions_if_needed` (`src/gobby/hooks/event_handlers/_agent.py`
  254) then delivers `prompts.persona` once on the successor's first turn (the spawned
  predecessor received `prompts.agent`; the two keys carry one anchored text, 3.2), which
  is decision 21's continuity mechanism. The seat's run stays alive across the clear:
  the native session ends as `paused`, never `expired` (`_session_end.py` 62-83), so
  the run is not completed, and its `terminal_id` still names the live terminal. 1.5
  copies the session's sandbox flags to the successor; `get_handoff` is unchanged.
- Rejected: the single edit inside `resolve_agent_name` (would add two queries to every
  reconcile repair); stamping the role into the handoff markdown (agent-cooperative and
  lost on crash relaunch).
- Planned checks: the three test files with the isolated hub DSN; mypy.

**Acceptance:**

- 1.3.1 - A session bound to a pane with a role activates that definition; an explicit
  `agent_name_override` beats the pane role; an unbound session ignores it. symbol:
  `pane_role_for_session`. test: `tests/hooks/test_pane_role_activation.py::test_pane_role_is_used_as_override_when_none_given`.
  test: `tests/hooks/test_pane_role_activation.py::test_explicit_override_beats_pane_role`.
  test: `tests/hooks/test_pane_role_activation.py::test_unbound_session_ignores_pane_role`.
- 1.3.2 - End to end through the native bind: after `create_tab(role=...)` and a
  SessionStart carrying that terminal id, `_agent_type` equals the role. test:
  `tests/hooks/test_session_start_handlers.py::test_pane_role_activates_definition_after_native_bind`.
- 1.3.3 - The role also applies when the native bind lands on the retry path. test:
  `tests/hooks/test_session_materialize.py::test_pane_role_applies_on_retry_bind_path`.
- 1.3.4 - Guides describe pane-bound roles, the `/clear` behavior, that a pane's role
  is written only by the launch (1.5) so a role change is a relaunch, and the tmux
  limitation. behavior: "pane-bound roles" in `docs/guides/agents.md`.
- 1.3.5 - The `/clear` successor of a launched seat (predecessor session carrying
  `is_spawned_agent` and `_agent_type` equal to the pane role) activates the same role
  with `is_spawned_agent` absent and receives the definition's persona text exactly
  once on its first turn. test:
  `tests/hooks/test_pane_role_activation.py::test_clear_successor_of_launched_seat_reactivates_role_and_gets_persona_once`.

### 1.4 Retired slot: gobby-agents:apply_agent_definition (superseded by 1.5, decision 20)
`kind: framing`

R5's 1.4 delivered a live full-definition switch for a running session. Decision 20
makes the launch the only writer of a pane's role (round file, R6 position g), so a
role change is a relaunch and no live switch, `pane.set_role`, `--role` flag or `gobby
panes set-role` exists. Acceptance items 1.4.1 to 1.4.5 are removed with it; the number
is not reused, so D1 and the round file keep resolving.

### 1.5 Runbook launch: spawn_agent placement, the role's sandbox block, pane-bound runs [category: code] (depends: 1.1, 1.2, 1.6)
`kind: deliverable`

Targets:
- `src/gobby/mcp_proxy/tools/spawn_agent/_placement.py`
- `src/gobby/mcp_proxy/tools/spawn_agent/_sandbox_block.py`
- `src/gobby/mcp_proxy/tools/spawn_agent/_implementation.py::spawn_agent_impl`
- `src/gobby/mcp_proxy/tools/spawn_agent/_factory.py::*` — scope-reason: the tool schema gains `placement` and `sandbox`, forwards them, defaults the parent to the system session for a caller with no session, forwards the request context's verified launcher flag beside `caller_session_id`, and the provider fallback branch re-resolves the selected name with the row
- `src/gobby/terminals/workspace_launch.py`
- `src/gobby/terminals/workspace_ops.py::*` — scope-reason: inherits the launch mixin and `pane_close` terminates a pane-bound run
- `src/gobby/terminals/actor_scope.py::resolve_actor_scope`
- `src/gobby/agents/pane_runs.py`
- `src/gobby/agents/idle_check_handler.py::IdleCheckHandler._get_active_terminal_runs`
- `src/gobby/agents/terminal_prompt_monitor.py::*` — scope-reason: its run listing filters pane-bound runs by the shared predicate
- `src/gobby/agents/lifecycle_monitor.py::*` — scope-reason: the stuck and completed-task checks skip pane-bound runs by the shared predicate; call lines only
- `src/gobby/hooks/event_handlers/_session_start/materialize.py::_bind_clear_successor`
- `src/gobby/servers/routes/mcp/endpoints/request_context.py::_set_context_for_request`
- `src/gobby/servers/auth_service.py::*` — scope-reason: add the local-CLI-token predicate the seam asks beside `request_principal`, which reports the local bearer, the local-token header and a web session cookie alike (PD N1)
- `tests/servers/routes/mcp_endpoints/test_execution_context.py::*` — scope-reason: add the launcher flag verification tests, the cookie case included
- `tests/servers/test_auth_service.py::*` — scope-reason: add the grant-token-without-session refusal on the tool route that 1.5.16 relies on
- `src/gobby/cli/agents.py::spawn_agent_cmd`
- `docs/guides/agents.md`
- `docs/guides/sandboxing.md`
- `docs/guides/mcp-tools.md`
- `docs/guides/cli-commands.md`
- `tests/mcp_proxy/tools/spawn_agent/test_spawn_placement.py`
- `tests/mcp_proxy/tools/spawn_agent/test_sandbox_block.py`
- `tests/terminals/test_workspace_launch.py`
- `tests/agents/test_pane_run_lifecycle.py`
- `tests/cli/test_cli_agents.py::*` — scope-reason: add the placement and sandbox flag tests
- `tests/hooks/test_session_materialize.py::*` — scope-reason: add the sandbox-flag copy test

Tool contract. `gobby-agents:spawn_agent` gains two optional parameters. `placement`
is `{"tab": {"workspace": REF | null, "project": NAME | ID, "title": str | null,
"runbook": str | null}}` (a new tab in the node's current workspace, or the named one,
whose first pane is the seat) or `{"split": {"pane": REF, "axis": "horizontal" |
"vertical"}}` (a new pane beside the named one, the daemon's `_place` semantics per
Constraints). `sandbox` is a restriction block in `SandboxConfig`'s field names
(`extra_read_paths`, `extra_write_paths`, `extra_deny_read_paths`,
`extra_deny_write_paths`, `allowed_domains`, `denied_domains`, `allow_git_network`,
`allow_package_registries`, `allow_unix_sockets`); `backend`, `mode`, `allow_network` and
`enabled` are refused in the call block: the first three are daemon-owned, and only a
definition's block may switch the sandbox off (decision 20, adversary A1). The pane's
role is the `agent` name; a `placement` without `agent` is refused, and so is
`terminal_backend: tmux` with placement. The definition's own `sandbox` block (the
optional `AgentDefinitionBody.sandbox` that 1.6 adds, the same shape, validated by
`coerce_sandbox_config`'s rules) is applied first and the call's block on top: lists
append, booleans override, and `enabled: false` comes only from the definition's block,
where the validator requires a `reason`, and is honored only when the resolved row is
sync-managed (`is_sync_managed_bundled_agent`: global, `source` installed, tag `gobby`,
the predicate `sync_bundled_agents` already uses to decide which rows it owns) and the
caller is not itself sandboxed (the row of the session the proxy authenticated, the
`caller_session_id` the tool already receives beside `parent_session_id` for
`authorize_write_grant`, has `sandbox_enabled` false; a body-supplied
`parent_session_id`, which the proxy passes through untouched, never qualifies; a call
with no caller session qualifies only when the request authenticated with the local
CLI token, which the daemon-owned policy already denies to every sandboxed run
(`_credential_roots` in `sandbox_policy.py` lists `~/.gobby/local_cli_token` and feeds
`compute_sandbox_paths`' deny-read roots; `read_local_api_token` catches the resulting
`PermissionError`), so a token-only call proves an unsandboxed process: gclient's
`launch` verb (2.1) and `gobby agents spawn` run by the operator. `_set_context_for_request`,
which seeds `session_id` None for such a call today, records a `trusted_launcher` flag
beside it only for local-CLI-token authentication with no session header or body
session, asked of the auth service through a predicate beside `request_principal` that
answers true only for a `verify_bearer`-accepted `Authorization` bearer or
`X-Gobby-Local-Token` header, because `_accepted_bearer` reports the local bearer, the
local-token header and a `gobby_session` cookie alike as the operator principal `None`,
so a cookie-authenticated request never gets the flag (PD N1); a call authenticated by
any other credential the endpoint accepts (a managed
run's grant token) never sets it and cannot arrive without a session anyway, because
`_AGENT_CAPABILITY_MATRIX` marks `POST /api/mcp/*/tools/*` `bind_identity` and
`_agent_identity_matches` refuses a grant-token request whose `X-Gobby-Session-Id` is
absent or resolves to a session other than the token's (adversary A6); `_factory.py`
forwards the flag to
`spawn_agent_impl` beside `caller_session_id` (enhancer E2, fail closed); no body field
or header names a session that confers it, and a sandboxed authenticated caller is
never honored); from any other row, for a sandboxed caller, or for a no-session call
without the flag, the launch ignores the exemption
and stays sandboxed, on the precedent of `authorize_write_grant`, where a managed caller may only
narrow what it holds. No block means the spawn path's default policy.

Order inside the tool (`_placement.py`, called from `spawn_agent_impl` by one branch
after the definition resolves): (1) resolve the definition through
`resolve_agent_with_row`, so the row's provenance travels with the body; when the
provider fallback in `_factory.py` (its `_load_agent_body` loop, which today loads a
body only) selects another definition, the branch re-resolves the selected name with
`resolve_agent_with_row`, so `definition_row` is always the row of the body that
launches (adversary A3) (`spawn_agent_impl` gains `definition_row`, default None, so
the direct callers in `ask/` and `feedback/` and the HTTP `/api/agents/spawn` route,
which pass a body only, never honor an exemption); it must declare the `spawn`
surface (`prompts.agent`) and, for a runbook role, the `persona` surface too, because
the `/clear` successor is delivered `prompts.persona` (1.3); (2) reserve the pane row
with `role` and, for a new tab, `runbook` and `title`, through the new
`WorkspaceOps` mixin `src/gobby/terminals/workspace_launch.py` (`reserve_launch_pane`,
`bind_launch_pane`, `release_launch_pane`, on the `_fill` pattern: insert the pane row
with `mark_spawn_in_flight`, no shell), so the pane's identity env
(`_identity_env`: `GOBBY_NODE_ID`, `GOBBY_WORKSPACE_ID`, `GOBBY_TAB_ID`,
`GOBBY_PANE_ID`, `GOBBY_PANE_REF`, `GOBBY_NODE_REF`) exists before the provider
process does; (3) merge the sandbox blocks into `agent_sandbox_config(daemon_config)`
at the existing seam (`_sandbox_block.py`, replacing the plain
`apply_write_grant(agent_sandbox_config(...), write_grant)` value when a block is
present), honoring the definition's `enabled: false` only when `definition_row` passes
`is_sync_managed_bundled_agent` and the `caller_session_id` row (never the `parent_session_id` argument), read
through the session manager the tool already holds, has `sandbox_enabled` false, or
there is no caller session and the request context's `trusted_launcher` flag is set,
and record the
reason, when any, as the launched session's initial variable `_launch_sandbox_reason`; (4) run the existing spawn with `terminal_backend` forced to
`native`, `extra_env` set to the identity env, `parent_session_id` equal to the caller's
session or, for a caller with none (the CLI paths), the machine system session
(`system_session_id()`, the scheduler's precedent), `project_path` set to the tab's
checkout (or the worktree path), `notify_parent_on_completion` false and no `timeout`
(the definition's `timeout` stays 0, so the run row has no `timeout_seconds`); (5) bind
the pane to the spawned terminal (`set_pane_terminal(owns_terminal=True)`), emit
`tab.created` or `pane.added` with the row, and label the pane with the role when no
title was given; on any failure between (2) and the spawn's return, release the reserved row and let
the spawn's own rollback run; on any failure after the spawn returns (the bind, the
event, the label), terminate the spawned run through the existing termination path
`pane_close` uses, which reaps its sandbox (`reap_terminal_sandbox_run`), then release
the reserved row, so a failed launch leaves no active run, terminal or pane behind
(enhancer E1). The reply gains `pane_ref`, `tab_ref`, `terminal_id`, `sandbox`
(`{"enforced": bool, "policy_hash": str | None, "reason": str | None}`) and
`worktree_path`.

Size guard (Constraints): `src/gobby/mcp_proxy/tools/spawn_agent/_implementation.py`
(964 lines) is split: the placement branch moves into
`src/gobby/mcp_proxy/tools/spawn_agent/_placement.py` and the sandbox-block merge
into `src/gobby/mcp_proxy/tools/spawn_agent/_sandbox_block.py`, leaving call lines;
`src/gobby/agents/lifecycle_monitor.py` (956) is split: the pane-bound predicate and
the exemption checks move into `src/gobby/agents/pane_runs.py`;
`src/gobby/terminals/workspace_ops.py` (973) is split: the launch pane ops move into
`src/gobby/terminals/workspace_launch.py` as a mixin base.

Worktree binding is the existing `isolation: worktree` path (`worktree_id`,
`branch_name`, `base_branch` on the tool): the spawn creates or reuses the worktree,
`SpawnRequest.cwd` is its path, and a new tab created for such a launch is bound to it
through `tab_create`'s existing `worktree_id`. The twelve runbook roles are `isolation:
none`; a lane developer pane is the worktree case (decision 20).

Lifecycle of a pane-bound run (`src/gobby/agents/pane_runs.py`, one predicate
`is_pane_bound_run(run)`: the run's `terminal_id` is the `terminal_id` of a
`workspace_panes` row): `resolve_actor_scope` admits a caller whose live terminal is
pane-bound as `session:<ref>` (today every caller with an `agent_run_id` is refused,
`actor_scope.py` 64-90), so the assistant seat can create tabs and launch seats;
`IdleCheckHandler._get_active_terminal_runs`, the prompt monitor's periodic Enter, and
`LifecycleMonitor`'s `check_autonomous_stuck_agents` and `check_completed_task_agents`
skip pane-bound runs (a persistent seat idles by design and is never reprompted, failed,
stuck-intervened, typed into or completed for it); the stale sweep already skips runs
with a live terminal (`_cleanup.py` 36-72). `pane_close` on a pane-bound run terminates
the run through the existing termination path and reaps its sandbox
(`reap_terminal_sandbox_run`); a CLI that exits by itself ends its session as `expired`
and completes the run today. `_bind_clear_successor` copies `sandbox_enabled` and
`sandbox_policy_hash` from the predecessor, because the successor runs in the same
sandboxed process (1.2's roster field stays true across a `/clear`).

CLI. `gobby agents spawn` gains `--tab TITLE --project NAME|ID [--workspace REF]
[--runbook NAME]` or `--split REF --right|--down`, `--sandbox JSON` and `--agent`;
`--session` becomes optional (the system session is the parent
when it is absent); with placement the command omits `timeout` and sends
`notify_parent_on_completion: false`. It posts the same
`POST /api/mcp/gobby-agents/tools/spawn_agent` it posts today. gclient's `launch` verb
(2.1) posts the same endpoint with the same body, so the three callers share one
implementation and one reply.

**Granularity:** more than six acceptance items and production Target files, and two lifecycle owners (the launch and the pane-bound run). Kept as one leaf: a pane-bound run exists only through this launch, the pane-bound predicate is the launch's contract with the monitors, and its tests need the launch's pane binding. The definition side is split off as 1.6. Alternative for the PD: the pane-bound run lifecycle (1.5.9) as a leaf of its own that 1.5 depends on.

Research context:
- `spawn_agent_impl` (`src/gobby/mcp_proxy/tools/spawn_agent/_implementation.py`, 964
  lines at `f7b9ccc54c`): the definition's `prompts.agent` is required at 147; the
  sandbox seam is `apply_write_grant(agent_sandbox_config(daemon_config), write_grant)`
  (~397); `parent_session_id is required` (~400) and `runner.can_spawn(parent)` (~403;
  `agents/session.py` 118-143, depth under 5; the system session is depth 0);
  `effective_timeout` (324-328: `None` with a definition `timeout` of 0 means no
  timeout). Tool schema and registration: `_factory.py` (784 lines; `timeout: float |
  None = None` at 334, `name="spawn_agent"` at 307). `_resolve_spawn_project_context`
  (`_factory.py` 182-235): an explicit `project_path` wins, which is why the launch
  passes the tab's checkout.
- The CLI path is already the tool over HTTP: `gobby agents spawn` (`src/gobby/cli/agents.py`
  176-307) posts `{daemon}/api/mcp/gobby-agents/tools/spawn_agent` with the local token
  and today requires `--session` (180) and always sends `--timeout` (198, default 120).
  Route: `router.post("/{server_name}/tools/{tool_name}")(mcp_proxy)` in
  `src/gobby/servers/routes/mcp/tools.py` 53. System session: `system_session_id()`
  (`src/gobby/storage/sessions/_constants.py` 77-80), `ensure_system_session` (126-196);
  precedent `src/gobby/scheduler/executor.py` 304.
- Spawn internals: `SpawnRequest` (`src/gobby/agents/spawn_models.py` 88-147: `cwd`,
  `extra_env`, `sandbox_config`, `terminal_backend`, `timeout_seconds`, `worktree_id`);
  `_default_backend` (`spawn_executor.py` 118-122); the native backend spawns argv on
  the gterm host with env, cwd, rows and cols (`terminals/native_runtime.py` 380-452;
  `spawn_web_terminal`, `terminals/web_spawn.py` 69-274: create pending row, prepare,
  promote); `build_cli_command` (`agents/spawners/command_builder.py` 12-219) in the
  default `agent` mode launches the provider's TUI with the prompt (`--session-id`,
  `--model`, `--effort`, `--dangerously-skip-permissions` for claude), so a spawn-path
  launch is the same TUI a typed launch line gave. Sandbox: `prepare_sandbox_launch`
  (`agents/srt_runtime.py` 472-669) refuses `allow_network: true` under SRT;
  `_sandbox_requested` and `_unsupported_sandbox_request_error`
  (`spawn_executor_support.py` 218-243); `_record_actual_sandbox_enforcement` (245-262)
  writes `sessions.sandbox_enabled` and `sandbox_policy_hash`; deny roots are emitted
  literal and canonical (`sandbox_policy.py` `deny_paths` 258-268) and sandbox-runtime
  gives `denyWrite` precedence over `allowWrite` (`sensitive_write_roots` docstring,
  302-320), so a read-only seat's `extra_deny_write_paths: ["."]` denies every write
  under its checkout; `compute_sandbox_paths` (`sandbox.py` 249-371) appends
  `config.extra_*` to the daemon-owned roots.
- Pane path: `tab_create` (`workspace_ops.py` 372-402) and `pane_split` (460-490)
  adopt an existing terminal through `_pane_source` (754-790) and `_adoptable`
  (792-807), and `_fill` (823-873) inserts the pane row before it spawns `zsh` with
  `_identity_env` (228-242); adopting after the spawn would leave the CLI without
  `GOBBY_PANE_REF`, which is why the launch reserves first. `_adoptable` requires the
  terminal to be admitted to the actor scope.
- Lifecycle: runs join terminals by `agent_runs.terminal_id` (`_cleanup.py` 36-72;
  `services.terminal_for(run)` in `idle_check_handler.py` `_get_active_terminal_runs`);
  `check_idle_agents` (109-135) reprompts and fails idle runs;
  `check_periodic_enters` (`terminal_prompt_monitor.py`) types Enter into every active
  spawned terminal; `check_autonomous_stuck_agents` (`lifecycle_monitor.py` 603-697),
  `check_completed_task_agents` (792-797); `check_unhealthy_agents`
  (`agent_health.py` 179-216) times out only runs with `timeout_seconds` set. Session
  end (`_session_end.py` 62-110): a native terminal session ends as `paused` for every
  reason but compact, and only `expired` completes the run. Step state at spawn:
  `initial_step_state_for_spawn` and `persist_initial_step_instance_if_resolved`
  (`_step_state.py` 124-185) instantiate the definition's existing step program.
- Rejected: a `pane.launch` workspace op that types a sandboxed launcher into the
  pane's shell (R6's first draft; two launch implementations and no step program);
  typing the launch through `send-keys` (unsandboxed, decision 20); a second sandbox
  resolver; a new policy format; adopting a spawned terminal into a pane after the
  spawn (no identity env); an HTTP route of its own for the launch (the tool endpoint
  exists).
- Auth: `_accepted_bearer` (`src/gobby/servers/auth_service.py` 365-383) returns `None`
  for a `verify_bearer`-accepted `Authorization` bearer, for the `X-Gobby-Local-Token`
  header and for a `gobby_session` cookie alike, and `request_principal` (359) returns
  it unchanged; the seam seeds that principal lazily (`_seed_request_principal`), so the
  launcher flag asks the auth service which credential the request carried (PD N1).
- Planned checks: the four new test files and the three extended ones with the isolated
  hub DSN; `tests/agents/test_srt_spawn.py` unchanged and passing; mypy; `wc -l` on
  `_implementation.py`, `lifecycle_monitor.py` and `workspace_ops.py` under 1,000.

Consumers unchanged:
- `src/gobby/dispatch/spawn.py` — no-edit-reason: calls `spawn_agent_impl` with the existing keywords; `placement` and `sandbox` default to None.
- `src/gobby/workflows/pipeline_executor_steps.py` — no-edit-reason: same defaulted call; pipelines never place a pane.
- `src/gobby/scheduler/executor.py` — no-edit-reason: same; it already parents on the system session.
- `src/gobby/servers/routes/agent_spawn.py` — no-edit-reason: forwards its own request model to the same implementation; the two new keywords default to None.
- `src/gobby/servers/routes/mcp/tools.py` — no-edit-reason: the tool endpoint forwards whatever arguments the tool schema accepts.
- `src/gobby/servers/routes/mcp/endpoints/discovery.py` — no-edit-reason: calls `_set_context_for_request` with the same signature; the flag is a new context field it never reads.
- `src/gobby/servers/routes/mcp/endpoints/execution.py` — no-edit-reason: calls `_set_context_for_request` with the same signature and passes the resolved session id as today; the flag travels in the request context.
- `tests/servers/routes/mcp_endpoints/test_execution_session_end_cleanup.py` — no-edit-reason: exercises session-bearing requests, for which the flag is false and unread.
- `tests/servers/test_mcp_execution_context.py` — no-edit-reason: asserts the existing context fields; the new flag defaults to false.
- `src/gobby/ask/agents.py` — no-edit-reason: calls `spawn_agent_impl` with the existing keywords; `placement` and `sandbox` default to None. It reads no new body field.
- `src/gobby/feedback/agent.py` — no-edit-reason: calls `spawn_agent_impl` with the existing keywords; `placement` and `sandbox` default to None.
- `tests/agents/test_backend_ingress.py` — no-edit-reason: existing spawn test; the new keywords default and the placement branch is not entered, so it passes unchanged.
- `tests/agents/test_local_context_setup.py` — no-edit-reason: existing spawn test; the new keywords default and the placement branch is not entered, so it passes unchanged.
- `tests/mcp_proxy/tools/spawn_agent/test_factory.py` — no-edit-reason: existing spawn test; the new keywords default and the placement branch is not entered, so it passes unchanged.
- `tests/mcp_proxy/tools/spawn_agent/test_initial_variables.py` — no-edit-reason: existing spawn test; the new keywords default and the placement branch is not entered, so it passes unchanged.
- `tests/mcp_proxy/tools/test_agents_spawn_tools.py` — no-edit-reason: existing spawn test; the new keywords default and the placement branch is not entered, so it passes unchanged.
- `tests/mcp_proxy/tools/test_spawn_agent_impl_provider.py` — no-edit-reason: existing spawn test; the new keywords default and the placement branch is not entered, so it passes unchanged.
- `tests/workflows/test_step_snapshot_semantics.py` — no-edit-reason: existing spawn test; the new keywords default and the placement branch is not entered, so it passes unchanged.
- `src/gobby/mcp_proxy/tools/sessions/_terminal_send_keys.py` — no-edit-reason: calls `resolve_actor_scope` with the same signature; the pane-bound admission lives inside the resolver.
- `src/gobby/terminals/termination.py` — no-edit-reason: calls `resolve_actor_scope` with the same signature; the pane-bound admission lives inside the resolver.
- `tests/terminals/test_workspace_ops.py` — no-edit-reason: its refusal cases are headless runs, which stay refused; the admission is tested in `tests/terminals/test_workspace_launch.py`.
- `docs/evidence/wiki-bakeoff-code-2026-09/test_ask_cohort.py` — no-edit-reason: an evidence snapshot, not a maintained consumer.
- `src/gobby/mcp_proxy/tools/spawn_agent/_step_state.py` — no-edit-reason: reads existing `AgentDefinitionBody` fields; the new optional `sandbox` field defaults to None. 1.5.8 relies on its existing behavior.
- `src/gobby/workflows/agent_resolver.py` — no-edit-reason: `resolve_agent_with_row` already returns the row beside the body.

**Acceptance:**

- 1.5.1 - `spawn_agent` with `placement.tab` creates a tab whose first pane carries
  `role` equal to `agent` and the given `runbook`, and with `placement.split` a pane
  beside the named one; the pane row exists before the provider spawns, the spawned
  process receives the pane's identity env, and the pane is bound to the spawned
  terminal with `tab.created` or `pane.added` emitted; the reply carries `pane_ref`,
  `tab_ref`, `terminal_id`, `sandbox` and `worktree_path`. symbol: `spawn_agent_impl`.
  file: `src/gobby/mcp_proxy/tools/spawn_agent/_placement.py`. test:
  `tests/mcp_proxy/tools/spawn_agent/test_spawn_placement.py::test_tab_placement_reserves_role_pane_before_spawn_and_binds_it`.
  test: `tests/mcp_proxy/tools/spawn_agent/test_spawn_placement.py::test_split_placement_lands_beside_named_pane_with_identity_env`.
- 1.5.2 - The MCP path and the CLI path give the same result: for one definition and
  one placement, `gobby agents spawn --split` and a direct `spawn_agent` call produce
  pane rows with the same `role`, sessions with the same `sandbox_policy_hash`, the
  same `cwd`, and the pane in the requested tab or split; the CLI omits `timeout`,
  sends `notify_parent_on_completion: false`, and parents on the system session when
  `--session` is absent. test:
  `tests/mcp_proxy/tools/spawn_agent/test_spawn_placement.py::test_cli_and_mcp_paths_produce_identical_role_sandbox_cwd_and_placement`.
  test: `tests/cli/test_cli_agents.py::test_spawn_placement_flags_post_placement_without_timeout_and_parent`.
- 1.5.3 - A runbook-launched CLI's argv and environment carry the same SRT wrapping as
  a `spawn_agent` launch of that provider, for every managed provider. test:
  `tests/terminals/test_workspace_launch.py::test_runbook_launch_wraps_argv_like_spawn_agent`.
- 1.5.4 - A restriction declared in the definition's `sandbox` block or the call's
  `sandbox` argument reaches the launched CLI's SRT policy (`assets/settings.json`
  carries the deny root literal and canonical), the two blocks merge as specified, and
  `allow_network`, `backend` or `mode` in a block is refused. file:
  `src/gobby/mcp_proxy/tools/spawn_agent/_sandbox_block.py`. test:
  `tests/mcp_proxy/tools/spawn_agent/test_sandbox_block.py::test_declared_restriction_reaches_srt_policy`.
  test: `tests/mcp_proxy/tools/spawn_agent/test_sandbox_block.py::test_definition_and_call_blocks_merge_lists_append_booleans_override`.
  test: `tests/mcp_proxy/tools/spawn_agent/test_sandbox_block.py::test_daemon_owned_fields_are_refused`.
- 1.5.5 - A launch with no restriction block gets the same `policy_hash` as a
  `spawn_agent` launch of that provider with no block. test:
  `tests/mcp_proxy/tools/spawn_agent/test_sandbox_block.py::test_launch_without_block_matches_spawn_agent_default_policy`.
- 1.5.6 - A call block that carries an `enabled` key is refused, whatever its value; a
  launch of a sync-managed bundled definition (a row written through `upsert_from_sync`
  with `source` installed and tag `gobby`) with `enabled: false` and a `reason`, by a
  caller whose session row has `sandbox_enabled` false or with no caller session,
  records `sandbox_enabled` false on the session row, `_launch_sandbox_reason` holds the
  reason, and the reply's `sandbox.enforced` is false with the reason (the negative
  cases are 1.5.12 to 1.5.14; the validation of the block is 1.6.1). test:
  `tests/mcp_proxy/tools/spawn_agent/test_sandbox_block.py::test_call_block_enabled_key_is_refused`.
  test:
  `tests/mcp_proxy/tools/spawn_agent/test_sandbox_block.py::test_unsandboxed_definition_requires_reason_and_records_it`.
- 1.5.7 - A pane launched for a definition with `isolation: worktree` starts with `cwd`
  under the worktree path and its new tab is bound to that worktree; it never starts in
  the main checkout. test:
  `tests/mcp_proxy/tools/spawn_agent/test_spawn_placement.py::test_worktree_role_pane_starts_in_its_worktree`.
- 1.5.8 - A definition that carries a step workflow, launched with placement by the MCP
  path and by the CLI path, gets `step_workflow_complete` false and its persisted step
  instance exactly as a plain `spawn_agent` launch gives it; a definition without one
  gets neither. test:
  `tests/mcp_proxy/tools/spawn_agent/test_spawn_placement.py::test_stepful_definition_gets_step_instance_by_each_path`.
- 1.5.9 - A pane-bound run is admitted as a terminal actor and is skipped by the idle
  check, the periodic Enter, the stuck check and the completed-task check while a
  headless run of the same shape is not; `pane_close` terminates the run and reaps its
  sandbox; the run row has no `timeout_seconds`. symbol: `is_pane_bound_run`. test:
  `tests/agents/test_pane_run_lifecycle.py::test_pane_bound_run_is_admitted_and_exempt_from_idle_stuck_enter_and_completion`.
  test: `tests/agents/test_pane_run_lifecycle.py::test_pane_close_terminates_run_and_reaps_sandbox`.
- 1.5.10 - `_bind_clear_successor` copies the predecessor's `sandbox_enabled` and
  `sandbox_policy_hash`. test: `tests/hooks/test_session_materialize.py::test_clear_successor_inherits_sandbox_flags`.
- 1.5.11 - The agents, sandboxing, MCP-tools and CLI guides document placement, the
  restriction block, the unsandboxed exemption with reason and its sync-only
  provenance, worktree binding, the pane-bound lifecycle and the three callers.
  behavior: "Launching a runbook pane" in `docs/guides/agents.md`. behavior: "Runbook
  restriction blocks" in `docs/guides/sandboxing.md`.
- 1.5.12 - The provider fallback carries the row: a launch whose first definition
  is the sync-managed program-director row but whose provider fails over to a fallback
  definition that is not sync-managed and carries `sandbox: {enabled: false, reason: ...}`
  launches with the default policy and a reply whose `sandbox.enforced` is true, and the
  session row records `sandbox_enabled` true. test:
  `tests/mcp_proxy/tools/spawn_agent/test_sandbox_block.py::test_fallback_into_unmanaged_definition_stays_sandboxed`.
- 1.5.13 - A row that is not sync-managed never launches unsandboxed: a `user`-tagged
  row carrying `sandbox: {enabled: false, reason: ...}`, inserted by the test through
  `upsert_from_sync` as the stand-in for a pre-existing row, launches with the default
  policy and a reply whose `sandbox.enforced` is true; so does a row created under the
  name `program-director` after the sync-managed row was deleted (the recreated row
  cannot carry the key, 1.6.5), and a launch that resolves no row (the name deleted and
  not recreated) honors nothing. test:
  `tests/mcp_proxy/tools/spawn_agent/test_sandbox_block.py::test_unmanaged_definition_exemption_is_ignored`.
  test:
  `tests/mcp_proxy/tools/spawn_agent/test_sandbox_block.py::test_recreated_bundled_name_launches_sandboxed`.
- 1.5.14 - A sandboxed caller never gets an unsandboxed pane: a launch of the
  program-director definition (sync-managed, `enabled: false` with a reason) from a
  caller whose session row has `sandbox_enabled` true launches with the default policy
  and a reply whose `sandbox.enforced` is true, whatever `parent_session_id` the call
  supplies, while the same launch with no caller session behind
  the local CLI token honors the exemption. test:
  `tests/mcp_proxy/tools/spawn_agent/test_sandbox_block.py::test_sandboxed_caller_launch_of_exempt_definition_stays_sandboxed`.
- 1.5.15 - A failed launch leaves no seat behind: with a bind failure injected after the
  spawn returns, the tool terminates the spawned run through the termination path
  `pane_close` uses, reaps its sandbox, releases the reserved pane row, and the reply is
  an error; afterwards no active run, no terminal and no pane row remain for that
  launch. test:
  `tests/mcp_proxy/tools/spawn_agent/test_spawn_placement.py::test_bind_failure_after_spawn_terminates_run_and_releases_pane`.
- 1.5.16 - A no-session launch is honored only behind the local CLI token: a request to
  the tool endpoint authenticated by that token with no session header or body session
  seeds `session_id` None and `trusted_launcher` true, and its launch of the
  program-director definition honors the exemption; the same request authenticated by a
  `gobby_session` cookie alone seeds `session_id` None and `trusted_launcher` false, and
  its launch stays sandboxed (PD N1); a managed run's grant token with no
  `X-Gobby-Session-Id` is refused by the auth service (HTTP 401, `bind_identity` on
  `POST /api/mcp/*/tools/*`) before the endpoint runs; the same grant token with its
  bound session header seeds `session_id` to that session and `trusted_launcher` false,
  and its launch takes the caller check of 1.5.14; the seam called directly with no
  caller session and the flag unset stays sandboxed; the daemon-owned policy of a
  sandboxed run denies reading `~/.gobby/local_cli_token`. test:
  `tests/servers/routes/mcp_endpoints/test_execution_context.py::test_trusted_launcher_is_set_only_for_local_token_without_session`.
  test:
  `tests/servers/test_auth_service.py::test_grant_token_without_session_header_is_rejected_on_tool_route`.
  test:
  `tests/mcp_proxy/tools/spawn_agent/test_sandbox_block.py::test_no_session_launch_without_trusted_launcher_stays_sandboxed`.
  test: `tests/agents/test_sandbox_policy.py::test_local_cli_token_is_denied_to_sandboxed_runs`.

### 1.6 Definition sandbox block: `AgentDefinitionBody.sandbox`, the write guard and the sync-owned exemption [category: code]
`kind: deliverable`

Targets:
- `src/gobby/workflows/agent_models.py::AgentDefinitionBody`
- `src/gobby/storage/definitions/agents.py::AgentDefinitionManager.create`
- `src/gobby/storage/definitions/agents.py::AgentDefinitionManager.upsert_with_steps`
- `src/gobby/storage/definitions/agents.py::AgentDefinitionManager._write_update`
- `src/gobby/storage/definitions/agents.py::AgentDefinitionManager.restore`
- `src/gobby/storage/definitions/agents.py::AgentDefinitionManager.toggle_enabled`
- `src/gobby/storage/definitions/agents.py::AgentDefinitionManager.move_to_project`
- `src/gobby/storage/definitions/agents.py::AgentDefinitionManager.move_to_global`
- `src/gobby/storage/definitions/agents.py::AgentDefinitionManager.set_step_workflow`
- `src/gobby/storage/definitions/agents.py::_find_live`
- `src/gobby/agents/sync.py::_is_sync_managed_bundled_agent`
- `src/gobby/agents/sync.py::sync_bundled_agents`
- `src/gobby/skills/reference_migration.py::migrate_instruction_requirements`
- `src/gobby/mcp_proxy/tools/workflows/_agents.py::create_agent_definition`
- `src/gobby/mcp_proxy/tools/workflows/_agents.py::update_agent_step_workflow`
- `src/gobby/servers/routes/agents.py::create_definition`
- `src/gobby/servers/routes/agents_definition_models.py`
- `tests/workflows/test_agent_models.py::*` — scope-reason: add the `sandbox` field tests
- `tests/storage/definitions/test_agents_manager.py::*` — scope-reason: add the sandbox-key, exempt-row, step-workflow and delete-then-recreate guard tests
- `tests/mcp_proxy/tools/test_mcp_proxy_tools_agent_definitions.py::*` — scope-reason: add the create, patch and step-workflow refusal tests
- `tests/servers/routes/test_agents_routes.py::*` — scope-reason: add the create, update, import, restore and patch refusal tests
- `tests/workflows/test_imports.py::*` — scope-reason: add the project-YAML refusal test
- `tests/agents/test_agents_sync.py::*` — scope-reason: the new-row branch moves to `upsert_from_sync`

The field. `AgentDefinitionBody` gains an optional `sandbox` block in `SandboxConfig`'s
field names, validated by `coerce_sandbox_config`'s rules, that refuses the daemon-owned
fields (`backend`, `mode`, `allow_network`) and accepts `enabled: false` only with a
`reason`; it is the only place `enabled: false` may be written (decision 20). 1.5's
launch applies it under the call's block and honors the exemption only from a row this
leaf's marker owns.

Size guard (Constraints): `src/gobby/servers/routes/agents.py` (901 lines) is split: the
definition request models (`CreateAgentDefinitionRequest`, the update request model and
their validators) move into `src/gobby/servers/routes/agents_definition_models.py`,
leaving imports, before `create_definition` gains its one line.

Definition write paths (PD S1). Every path that writes an agent definition row goes
through `AgentDefinitionManager`, whose sync-only entry points already exist
(`upsert_from_sync`, and `update_from_sync` with `from_sync=True`). One guard there,
`_refuse_unsynced_exempt_write(stored_body, incoming_body)`, is called by `create`,
by both branches of `upsert_with_steps` (the existing-row branch reads the live row's
stored body inside the same transaction, so `_find_live` returns it), by
`_write_update` when `from_sync` is false, whatever the fields, by `set_step_workflow`
unless its new `from_sync` keyword is true (it locks and reads the parent's stored body
inside its transaction before `_write_child`, so a refusal leaves the child row
unchanged; the sync's step-workflow re-write in `sync_bundled_agents` and the reference
migration's in `migrate_instruction_requirements` pass `from_sync=True`; PD B1), by
`toggle_enabled` (the HTTP enable and disable route; the exempt row is enabled by its
template), by
`move_to_project` and `move_to_global` (a moved exempt row would lose the global
scope the marker requires and escape the sync's re-write and sweep; no caller in
`src/` today) and by `restore`, which reads the soft-deleted row inside its
transaction before `restore_definition`: it raises `ValueError` when the incoming
parent body carries the `sandbox.enabled` key, or when the stored body does. A row that
carries the key is therefore immutable outside the sync: no rename (which would carry
the program-director row and its exemption under a new name until the next start's
sweep), no description edit, no rule or variable patch, no step-workflow replace or
clear (PD B1), no restore, no move, and no re-tag through the existing-row branch of
`upsert_with_steps`, which overwrites
`source` and `tags` from its arguments; the role changes only through its template and
a restart, which is already how the key itself changes. A row without the key takes
the same edits as today, except that it cannot gain the key. `delete` and `hard_delete`
stay unguarded: deleting the exempt row confers nothing, because a row recreated under
the bundled name cannot carry the key, `restore` of the deleted row is refused, a launch
that resolves no row honors nothing (`_load_agent_body` reads rows only; there is no
disk fallback), and the next start re-inserts the row through `upsert_from_sync`. The
sync entry points skip the guard (the sync never restores a soft-deleted row;
`upsert_from_sync` inserts a new one), and `sync_bundled_agents`'s new-row branch moves
from `upsert_with_steps` to `upsert_from_sync`, which already inserts when no live row
exists. The paths that reach the guard, unchanged except the first three:
`gobby-workflows:create_agent_definition` and `gobby-workflows:update_agent_step_workflow`
(`_agents.py`, which gain an `except ValueError` returning `success: false`); the HTTP
`create_definition`, which today
declares `sandbox_config` on its request model and drops it, and now passes
`sandbox=request.sandbox_config` into `AgentDefinitionBody(...)` as `update_definition`
maps it onto `sandbox` today, so a restriction block is stored and a block carrying
`enabled` is refused with 400 through the route's existing `except (TypeError,
ValueError)` (adversary A4); the HTTP update, import and restore routes
(`update_definition` maps `ValueError` to 400 today and calls `set_step_workflow`
before `update`, so a step-workflow-only update of the exempt row is refused by the
child write with the child row unchanged (PD B1), `import_definition` of a bundled
YAML that carries the exemption is refused the same way, `restore_definition` maps
`ValueError` to 404 today); the MCP rule and variable patches and the HTTP `patch_*`
routes (they re-save the stored body through `_write_update`, so they pass on every row
but the exempt one); `_upsert_agent` in the workflows import module behind the
`gobby-workflows` import tool (a project YAML carrying the key is that file's error);
and `duplicate` (no caller in `src/`). No CLI writes a definition row: `gobby agents`
lists, shows and spawns, and `gobby sync` is the sync path. The sync reads
`get_bundled_agents_path()` under `get_install_dir()`, the daemon refuses to start
from a linked worktree, and `gobby sync` verifies bundled integrity outside dev mode,
so `sandbox.enabled` reaches a row only from the bundled agents directory in the main
checkout: a reviewed merge, or the program-director's own edit. The marker
(`is_sync_managed_bundled_agent`, made public: global row, `source` installed, tag
`gobby`) is sound after the first start following the cutover (V1 step 2): the sync
re-writes every managed row whose body differs from its template and sweeps
`gobby`-tagged rows with no template, so a `gobby` tag set through the HTTP `tags`
field buys nothing without the key the guard refuses.

**Granularity:** more than six production Target files. One guard, its call sites, the
model field and the sync marker are one outcome: the invariant (no non-sync write
reaches a row that carries `sandbox.enabled`) holds only with every call site in
place, so a call site alone is not closeable. The launch that honors the row is 1.5.

Research context:
- Storage: `AgentDefinitionManager` (`src/gobby/storage/definitions/agents.py` 264-740):
  `create` 270-316, `update` 350-355 and `update_from_sync` 357-358 into `_write_update`
  360-400 (locks the live row first), `toggle_enabled` 402-417, `delete` 419-446
  (soft; `sync_orphan` marks a sweep deletion), `restore` 461-465, `move_to_project`
  513-524, `move_to_global` 526-537, `duplicate` 539-552 (into `upsert_with_steps`
  with `source="custom"`), `upsert_with_steps` 554-626, `upsert_from_sync` 628-713;
  `_find_live` 109-120. Neither `move_to_*` nor `duplicate` has a caller in `src/`.
  `set_step_workflow` 715-732 writes the `agent_step_workflows` child row through
  `_write_child` after a parent-existence select, with no body read and no guard; its
  callers are the MCP `update_agent_step_workflow` (`_agents.py` 396-416), the HTTP
  `update_definition` (before `update`), `sync_bundled_agents` (`sync.py` 254, a managed
  row whose only difference is the step workflow) and `migrate_instruction_requirements`
  (`reference_migration.py` 145) (PD B1).
- Sync: `sync_bundled_agents` (`src/gobby/agents/sync.py` 129-299) decides ownership
  with `_is_sync_managed_bundled_agent`, stamps `tags=["gobby"]`, re-writes managed
  rows whose body differs from the template, refuses a shadowing unmanaged row, sweeps
  `gobby`-tagged rows with no template, and today creates new rows through
  `upsert_with_steps` (its one non-sync write). `get_bundled_agents_path()` is
  `get_install_dir()/shared/workflows/agents`.
- Routes and tools: `CreateAgentDefinitionRequest.sandbox_config` is declared
  (`src/gobby/servers/routes/agents.py` 116) and `create_definition` (378-433) builds
  the body field by field without it; `update_definition` (436-) maps `sandbox_config`
  onto `body_dict["sandbox"]` (491-492) and keeps `sandbox` in `preserved_extra_fields`;
  `restore_definition` maps `(DefinitionNotFoundError, ValueError)` to 404; the request
  models accept `tags`, so the tag alone is forgeable. `create_agent_definition`
  (`src/gobby/mcp_proxy/tools/workflows/_agents.py` 148) validates through
  `AgentDefinitionBody` and writes `upsert_with_steps(source="installed",
  tags=["user"])`. `_upsert_agent` (`src/gobby/workflows/imports.py`) writes `update`
  or `upsert_with_steps`. `_load_agent_body` (`_factory.py` 238-260) calls
  `resolve_agent`, a row lookup with no disk fallback.
- Rejected: a guard that only kept the key identical (a renamed or re-prompted exempt
  row would keep its exemption until the next sweep); a launch-time equality check
  against the bundled YAML (a second mechanism for the same fact, failing closed on an
  unsynced template edit); a provenance column (a migration for a fact the sync
  already decides); guarding `delete` (it confers nothing and the sync re-inserts).
- Planned checks: the six extended test files with the isolated hub DSN; mypy.

Consumers unchanged:
- `src/gobby/mcp_proxy/tools/workflows/_import.py` — no-edit-reason: calls `sync_imported_workflows`; a refused file surfaces as that file's error, as today.
- `src/gobby/workflows/definitions.py` — no-edit-reason: reads existing `AgentDefinitionBody` fields; the new optional `sandbox` field defaults to None.
- `src/gobby/workflows/imports.py` — no-edit-reason: `_upsert_agent` writes through `update` and `upsert_with_steps`, so the storage guard refuses a project YAML that carries `sandbox.enabled` without a change here.
- `src/gobby/workflows/step_instances.py` — no-edit-reason: reads existing `AgentDefinitionBody` fields; the new optional `sandbox` field defaults to None.
- `tests/agents/test_merge_orchestrator_contract.py` — no-edit-reason: builds definition bodies without a `sandbox` key; the optional field changes nothing it asserts.
- `tests/mcp_proxy/tools/skills/test_list_skills.py` — no-edit-reason: builds definition bodies without a `sandbox` key; the optional field changes nothing it asserts.
- `tests/workflows/test_agent_workflow_completion.py` — no-edit-reason: builds definition bodies without a `sandbox` key; the optional field changes nothing it asserts.
- `tests/workflows/test_handler_route_lint.py` — no-edit-reason: builds definition bodies without a `sandbox` key; the optional field changes nothing it asserts.
- `tests/workflows/test_step_instances.py` — no-edit-reason: builds definition bodies without a `sandbox` key; the optional field changes nothing it asserts.
- `tests/workflows/test_workflows_dry_run.py` — no-edit-reason: builds definition bodies without a `sandbox` key; the optional field changes nothing it asserts.
- `src/gobby/mcp_proxy/tools/workflows/__init__.py` — no-edit-reason: registers `create_agent_definition`; its signature and reply shape are unchanged.
- `tests/agents/test_agents_dry_run.py` — no-edit-reason: builds fixture definitions without a `sandbox.enabled` key, which the storage guard passes unchanged.
- `tests/agents/test_lifecycle_monitor.py` — no-edit-reason: builds fixture definitions without a `sandbox.enabled` key, which the storage guard passes unchanged.
- `tests/agents/test_lifecycle_monitor_extra.py` — no-edit-reason: builds fixture definitions without a `sandbox.enabled` key, which the storage guard passes unchanged.
- `tests/agents/test_merge_lifecycle.py` — no-edit-reason: builds fixture definitions without a `sandbox.enabled` key, which the storage guard passes unchanged.
- `tests/hooks/test_provider_launch_guard.py` — no-edit-reason: builds fixture definitions without a `sandbox.enabled` key, which the storage guard passes unchanged.
- `tests/hooks/test_session_coordinator.py` — no-edit-reason: builds fixture definitions without a `sandbox.enabled` key, which the storage guard passes unchanged.
- `tests/mcp_proxy/tools/spawn_agent/test_fallback_agent.py` — no-edit-reason: builds fixture definitions without a `sandbox.enabled` key, which the storage guard passes unchanged.
- `tests/mcp_proxy/tools/spawn_agent/test_load_agent_body.py` — no-edit-reason: builds fixture definitions without a `sandbox.enabled` key, which the storage guard passes unchanged.
- `tests/mcp_proxy/tools/test_agents_spawn_evaluation.py` — no-edit-reason: builds fixture definitions without a `sandbox.enabled` key, which the storage guard passes unchanged.
- `src/gobby/skills/sync.py` — no-edit-reason: calls `migrate_instruction_requirements` with the same signature; the `from_sync=True` keyword is on the migration's own `set_step_workflow` call.
- `tests/skills/test_reference_migration.py` — no-edit-reason: builds fixture definitions without a `sandbox.enabled` key, which the storage guard passes unchanged, and the migration's `set_step_workflow` call passes `from_sync=True` (PD B1).
- `tests/tasks/test_tasks_expansion_1.py` — no-edit-reason: builds fixture definitions without a `sandbox.enabled` key, which the storage guard passes unchanged.
- `tests/workflows/test_agent_definitions_v2.py` — no-edit-reason: builds fixture definitions without a `sandbox.enabled` key, which the storage guard passes unchanged.
- `tests/workflows/test_agent_resolver.py` — no-edit-reason: builds fixture definitions without a `sandbox.enabled` key, which the storage guard passes unchanged.
- `tests/workflows/test_agent_workflow_runtime_cleanup.py` — no-edit-reason: builds fixture definitions without a `sandbox.enabled` key, which the storage guard passes unchanged.
- `tests/workflows/test_command_position_patterns.py` — no-edit-reason: builds fixture definitions without a `sandbox.enabled` key, which the storage guard passes unchanged.
- `tests/workflows/test_rule_engine.py` — no-edit-reason: builds fixture definitions without a `sandbox.enabled` key, which the storage guard passes unchanged.
- `tests/workflows/test_step_enforcement_audit.py` — no-edit-reason: builds fixture definitions without a `sandbox.enabled` key, which the storage guard passes unchanged.
- `tests/workflows/test_step_error_codes.py` — no-edit-reason: builds fixture definitions without a `sandbox.enabled` key, which the storage guard passes unchanged.
- `tests/workflows/test_step_runtime_transitions.py` — no-edit-reason: builds fixture definitions without a `sandbox.enabled` key, which the storage guard passes unchanged.
- `tests/dispatch/test_skill_composition.py` — no-edit-reason: builds fixture definitions without a `sandbox.enabled` key, which the storage guard passes unchanged.
- `tests/build/test_dispatcher_stage_wake.py` — no-edit-reason: runs the bundled sync for its rows; the new-row branch writes the same rows through `upsert_from_sync`.
- `tests/build_pipeline/test_automation_readiness.py` — no-edit-reason: runs the bundled sync for its rows; the new-row branch writes the same rows through `upsert_from_sync`.
- `tests/dispatch/test_delivery_chain.py` — no-edit-reason: runs the bundled sync for its rows; the new-row branch writes the same rows through `upsert_from_sync`.
- `tests/dispatch/test_spawn_forwarding.py` — no-edit-reason: runs the bundled sync for its rows; the new-row branch writes the same rows through `upsert_from_sync`.
- `tests/hooks/test_staged_effects_receipt_route.py` — no-edit-reason: runs the bundled sync for its rows; the new-row branch writes the same rows through `upsert_from_sync`.
- `tests/storage/tasks/test_stage_registry_default_agent_fk.py` — no-edit-reason: runs the bundled sync for its rows; the new-row branch writes the same rows through `upsert_from_sync`.
- `tests/storage/test_stage_review_findings.py` — no-edit-reason: runs the bundled sync for its rows; the new-row branch writes the same rows through `upsert_from_sync`.
- `src/gobby/workflows/agent_resolver.py` — no-edit-reason: `resolve_agent_with_row` already returns the row beside the body; 1.5 consumes it.
- `src/gobby/ask/agents.py` — no-edit-reason: reads existing `AgentDefinitionBody` fields; the new optional `sandbox` field defaults to None.
- `src/gobby/mcp_proxy/tools/spawn_agent/_step_state.py` — no-edit-reason: reads existing `AgentDefinitionBody` fields; the new optional `sandbox` field defaults to None.
- `tests/mcp_proxy/tools/spawn_agent/test_initial_variables.py` — no-edit-reason: builds definition bodies without a `sandbox` key; the optional field changes nothing it asserts.
- `tests/workflows/test_step_snapshot_semantics.py` — no-edit-reason: builds definition bodies without a `sandbox` key and writes them through `upsert_with_steps`, which the guard passes unchanged.
- `docs/evidence/wiki-bakeoff-code-2026-09/test_ask_cohort.py` — no-edit-reason: an evidence snapshot, not a maintained consumer.

**Acceptance:**

- 1.6.1 - `AgentDefinitionBody.sandbox` parses the restriction shape, rejects the
  daemon-owned fields, and a block with `enabled: false` and no `reason` fails
  validation. symbol: `AgentDefinitionBody`. test:
  `tests/workflows/test_agent_models.py::test_definition_sandbox_block_parses_and_rejects_daemon_owned_fields`.
  test: `tests/workflows/test_agent_models.py::test_unsandboxed_definition_block_requires_reason`.
- 1.6.2 - Every non-sync manager write refuses the key and the exempt row, one case per
  path: `create` and the new-row `upsert_with_steps` refuse a body carrying
  `sandbox.enabled`; `_write_update` (a rename, a description edit and a rule patch
  each), the existing-row `upsert_with_steps` (a re-tag), `toggle_enabled`,
  `move_to_project`, `move_to_global`, `duplicate`, `set_step_workflow` (a replace and a
  clear) and `restore` refuse on a row whose stored body carries it; `upsert_from_sync`,
  `update_from_sync` and `set_step_workflow(from_sync=True)` accept both; a row
  without the key takes the same edits as today. test:
  `tests/storage/definitions/test_agents_manager.py::test_non_sync_writes_refuse_sandbox_enabled_key`.
  test:
  `tests/storage/definitions/test_agents_manager.py::test_exempt_row_refuses_every_non_sync_write`.
- 1.6.3 - Every ingress surfaces the refusal, one case per path: the MCP
  `create_agent_definition`, `update_agent_step_workflow` and the MCP rule and variable
  patches on the exempt row return `success: false`; the HTTP create (a `sandbox_config`
  carrying `enabled`), update (a step-workflow-only update included, after which the
  exempt row's child row is unchanged), import (a YAML carrying the key) and `patch_*`
  routes on the exempt row return 400 and the HTTP restore of the deleted exempt row
  returns 404; the HTTP create with a
  `sandbox_config` without `enabled` stores it as the row's `sandbox` block; a project
  YAML carrying the key is reported as that file's import error. test:
  `tests/mcp_proxy/tools/test_mcp_proxy_tools_agent_definitions.py::test_create_and_patch_refuse_sandbox_enabled`.
  test:
  `tests/servers/routes/test_agents_routes.py::test_definition_routes_refuse_sandbox_enabled_writes`.
  test: `tests/servers/routes/test_agents_routes.py::test_create_stores_restriction_block`.
  test: `tests/workflows/test_imports.py::test_project_yaml_with_sandbox_enabled_is_refused`.
- 1.6.4 - The sync's new-row branch writes through `upsert_from_sync`, and a bundled
  template carrying `enabled: false` with a reason lands in a global row with `source`
  installed and tag `gobby` that `is_sync_managed_bundled_agent` accepts. test:
  `tests/agents/test_agents_sync.py::test_new_bundled_row_is_written_through_upsert_from_sync`.
- 1.6.5 - Delete-then-recreate confers nothing: after the exempt row is soft-deleted, a
  `create` under the same name with the key is refused, a `create` without it yields a
  row without the key, `restore` of the deleted row is refused, and a following
  `upsert_from_sync` of the template re-inserts the exempt row. test:
  `tests/storage/definitions/test_agents_manager.py::test_delete_then_recreate_under_bundled_name_confers_nothing`.

## P2: gclient command mode
`kind: framing`

One Rust leaf. `gclient <verb>` runs without the TUI, sends one workspace op over the
same authenticated WebSocket the TUI uses (or, for `launch`, one HTTP tool call to the
daemon's `spawn_agent` endpoint, decision 20), prints the reply, and exits. A runbook
is a shell script of these verbs, so the loader, picker, and YAML format of the first
draft are gone (decision 12). Rust conventions per `crates/AGENTS.md` (load the `rust`
skill; tests in `<module>/tests.rs`). Work order: after 1.5 and before 1.3 (decision
12).

### 2.1 gclient command mode and the bundled runbook scripts [category: code] (depends: 1.5)
`kind: deliverable`

Targets:
- `crates/gclient/src/command.rs`
- `crates/gclient/src/command/verbs.rs`
- `crates/gclient/src/command/tests.rs`
- `crates/gclient/src/lib.rs::*` — scope-reason: one `pub mod command;` line in a module list that has no indexed symbols
- `crates/gclient/src/startup.rs::run`
- `crates/gclient/src/startup.rs::USAGE`
- `crates/gclient/src/daemon/rest.rs::*` — scope-reason: one `pub(crate) async fn spawn_agent_tool(url, token, body)` beside the existing REST helpers, posting `/api/mcp/gobby-agents/tools/spawn_agent`; `daemon/mod.rs` (924 lines) is untouched because the command module calls the helper directly
- `crates/gclient/tests/command_mode.rs`
- `crates/gclient/tests/mock_daemon/workspace.rs::*` — scope-reason: `WorkspaceSim::apply` answers `pane.read` and `pane.wait_for_output` for the verb tests, and `WorkspaceSim::adopt_pane` mirrors each canned launch reply into the simulated tree for the script tests
- `src/gobby/install/shared/workflows/runbooks/orchestration-v1.sh`
- `src/gobby/install/shared/workflows/runbooks/plan-council-v1.sh`
- `docs/guides/gclient-user-guide.md`

Entry. `startup::run` checks the first argument before `parse_args`: when it names a
verb, `command::dispatch(args, env)` runs it and `run` returns its exit code; otherwise
TUI startup is unchanged. `USAGE` gains one line pointing at `gclient help`. Connection
reuses the TUI's resolution: `gobby_core::daemon_url::daemon_url()` for the URL and
`~/.gobby/local_cli_token` through `gobby_core::local_token::read_local_cli_token` for
the token, with `--daemon-url` and `--token-file` overrides, then `LiveDaemon::connect`
and `Daemon::workspace_op` (both already public through the `daemon` module) for every
verb but `launch`, which posts the daemon's HTTP tool endpoint through the `rest.rs`
helper with the same URL and token (decision 20); the socket closes on exit. The pane
env carries `GOBBY_NODE_ID`, `GOBBY_NODE_REF`,
`GOBBY_WORKSPACE_ID`, `GOBBY_TAB_ID`, `GOBBY_PANE_ID`, `GOBBY_PANE_REF`, and
`GOBBY_TERMINAL_ID` and, by design, no `GOBBY_DAEMON_URL` or `GOBBY_PROJECT_ID`
(`_identity_env` in `src/gobby/terminals/workspace_ops.py`; finding F4 of the
gclient-workspaces plan). Defaults therefore are: REF omitted means `$GOBBY_PANE_REF`,
`--workspace` omitted means `$GOBBY_WORKSPACE_ID`, and the daemon URL comes from
bootstrap. Refs use the daemon's grammar (`WorkspaceManager.resolve_reference`): a uuid,
or `w`, `n:w`, `n:w:t`, `n:w:t:p`; the client passes them through untouched and only
counts segments to choose the tab or pane form of a verb.

Verbs. Each maps to one existing op, except `launch`, which posts 1.5's tool; `--json`
prints the reply's `result` verbatim (the WS reply that `workspace_ws.py` already
returns synchronously, or the tool reply), so the daemon gains nothing beyond 1.5.
- `list [--workspace REF]` sends `workspace_attach` (the existing attach request; the
  snapshot is the roster and the subscription dies with the socket). Plain output is
  one line per tab (`ref  title`) and per pane (`ref  label  terminal_id`).
- `new-tab --project NAME|ID [--name TITLE] [--workspace REF]` sends `tab.create` for a
  shell or tool tab; a project name resolves through the REST lookup the TUI uses
  (`startup::resolve_project_at`). Plain output: `<tab ref> <first pane ref>`. No role
  or runbook flag: a role is bound only by `launch` (decision 20, position g).
- `split REF --right|--down [--cmd TEXT]` sends `pane.split` with the `LayoutAxis`
  spelling (`horizontal` for `--right`, `vertical` for `--down`) for a shell or tool
  pane; `--cmd` chains one `pane.send_text {submit: true}` to the new pane. Plain
  output: the new pane ref.
- `launch --agent ROLE (--tab TITLE --project NAME|ID [--workspace REF] [--runbook NAME]
  | --split REF --right|--down) [--sandbox JSON] [--provider P]
  [--model M] [--effort E] [--isolation none|worktree] [--json] -- PROMPT` posts
  `POST /api/mcp/gobby-agents/tools/spawn_agent` with the local token and 1.5's
  contract: `agent`, `prompt`, `placement` (`{"tab": {"workspace", "project",
  "title", "runbook"}}` or `{"split": {"pane", "axis"}}` with the `LayoutAxis`
  spelling), `sandbox` (the `--sandbox` block; an `enabled` key in it is a usage error,
  because only a definition may switch the sandbox off), `provider`, `model`, `reasoning_effort`,
  `isolation`, `terminal_backend: "native"`, `notify_parent_on_completion: false`, no
  `timeout` and no `parent_session_id` (the daemon parents the seat on the system
  session); the project name resolves as for `new-tab`. Plain output: `<tab ref> <pane
  ref>` from the reply; `--json` prints the tool reply. A reply whose `success` is false
  prints its `error` on stderr and exits 1; an HTTP or token failure exits 3. The
  daemon labels the pane with the role, so no `title` line follows a launch.
- `resize REF RATIO` sends `pane.resize` (the ratio of the split whose direct child is
  REF, per Constraints).
- `title REF TEXT` sends `pane.rename {label}` for a four-segment ref and
  `tab.rename {title}` for three.
- `select REF` sends `workspace.set_focus_hints {workspace, project_id: null, tab, pane}`,
  the seed the next gclient window focuses from; the TUI already sends this op.
- `send-keys REF TEXT [--enter]` sends `pane.send_text {text, submit}`.
- `capture-pane REF [--lines N]` sends `pane.read {lines}` (default 50) and prints the
  snapshot text. No `--ansi`: the runtime snapshot (`SnapshotResult`) is plain text.
- `wait-for-output REF --pattern REGEX [--timeout S] [--interval S]` sends
  `pane.wait_for_output {pattern, timeout_seconds, poll_interval_seconds}`; the daemon
  polls, so there is no client loop. The verb sets its own request deadline of the
  timeout plus a margin, because `LiveDaemon::request` applies a per-request deadline.
  Exit 0 on `matched`, 1 on `timeout`, 2 on `pane_lost`.
- `kill REF` sends `pane.close` (four segments) or `tab.close` (three).
- `help` prints the verb table.
Exit codes: 0 success; 1 refused op, printed as `code: reason` on stderr from the
`workspace_error` reply, or a `launch` reply with `success` false, printed as its
`error`; 2 usage (`launch` without `--agent`, without exactly one of `--tab` and
`--split`, or with an `enabled` key in `--sandbox`); 3 connection,
token, HTTP, or protocol failure.

Scripts. Two bundled scripts (`#!/usr/bin/env bash`, `set -euo pipefail`) reproduce
decision 14 with refs captured from plain output. Each seat is one `launch` line: it
creates the seat's tab or split, spawns the CLI under the role's sandbox block, and
submits the kickoff prompt as the spawn prompt; nothing waits for a shell prompt,
nothing is typed, and no ready pattern exists (decision 20). Each successful `launch
--tab` is followed by `echo "CREATED_TAB=<tab ref>" >&2`, so when a later line fails
(the script stops under `set -e` with the panes created so far in place, 2.1.3) stderr
names the tabs to remove with `gclient kill <tab ref>`; there is no rollback. Provider,
model and effort are `launch` flags per seat, following the prompt book's assignment
and the resume file `~/Desktop/gobby-team-resume-2026-09-22.md` (decision 4 as
amended: the daemon builds the argv through `build_cli_command`, so the flag spellings
of the R5 launch lines are gone); the user edits the scripts freely. A `kickoff ROLE`
shell function in each script returns the prompt: the runbook name, the seat's role,
that its own pane ref is `GOBBY_PANE_REF` in its environment and the roster is
`gobby-workspaces:get_workspace` (decision 6 as amended), and, for the council, the
plan path. `orchestration-v1.sh [project]` (`P="${1:-gobby}"`, `RB=orchestration-v1`):
`launch --agent program-director --tab control --project "$P" --runbook "$RB"
--provider claude --model 'claude-fable-5-1[1m]' -- "$(kickoff program-director)"`
(unsandboxed by its definition's block, 3.2; the reply echoes the reason) gives `$ctl` and
`$pd`; `launch --agent assistant --split "$pd" --right --provider claude --model
'claude-opus-5[1m]' -- "$(kickoff assistant)"` gives `$asst`; `resize "$pd" 0.50`;
`launch --agent log-monitor --tab monitors --project "$P" --runbook "$RB" --provider
codex --model gpt-5.6-luna --effort medium -- "$(kickoff log-monitor)"` gives `$mon`
and `$logmon`; `launch --agent archivist --split "$logmon" --down --provider codex
--model gpt-5.6-terra --effort medium -- "$(kickoff archivist)"` gives `$arch`;
`resize "$logmon" 0.50`. `plan-council-v1.sh <plan> [project]` (`RB=plan-council-v1`):
`launch --agent plan-writer --tab "$(basename "$1" .md)" --project "${2:-gobby}"
--runbook "$RB" --provider claude --model 'claude-fable-5-1[1m]' -- "$(kickoff
plan-writer "$1")"` gives `$tab` and `$writer`; `launch --agent plan-enhancer --split
"$writer" --down --provider codex --model gpt-5.6-sol --effort xhigh -- "$(kickoff
plan-enhancer "$1")"` gives `$enh`, `resize "$writer" 0.50`; `launch --agent
plan-adversary --split "$enh" --right --provider grok --model grok-4.7 --effort xhigh
-- "$(kickoff plan-adversary "$1")"` gives `$adv`, `resize "$enh" 0.3333`; `launch
--agent plan-mechanic --split "$adv" --right --provider claude --model
'claude-opus-5[1m]' -- "$(kickoff plan-mechanic "$1")"` gives `$mech`, `resize "$adv"
0.50` (each resize follows its split while both children are leaves, per Constraints,
so the three lower panes come out at equal widths). The Fable and Opus seats pass no
`--effort` (the resume file's Claude lines carry none); the book names no provider or
effort for the adversary (section 11, lines 645-681, and "The plan council", 519-535,
carry none), so `grok-4.7` at `xhigh` is the live council seat's, and the mechanic
takes the lookup pane's line (decision 18). No script carries a `--sandbox` block: the
restriction blocks and the program-director's exemption live in the definitions (3.2,
3.4), where Josh reads them in the spec blocks; no launch line can switch a sandbox off
(adversary A1). No `title` lines:
the launch labels each pane with its role (1.5).

**Granularity:** more than six acceptance items and production Target files: the command mode and its two bundled scripts are one outcome, because the scripts are the mode's acceptance vehicles against the mock daemon and the mode without a runbook script has no user. Alternative for the PD: the two scripts as a dependent leaf.

Research context:
- `startup::run` (`crates/gclient/src/startup.rs` 674-691) parses args, probes health,
  and starts the TUI; `resolve_probe_env` (450-468) holds the URL and token defaults to
  reuse. `LiveDaemon::connect` (`daemon/live.rs` 190), `Daemon::workspace_op`
  (`daemon/mod.rs` 459), `op_request` (`daemon/workspace.rs` 355-361), and
  `workspace_op_live` (`daemon/live_workspace.rs` 33) are the whole send path;
  `attach_request(node, workspace, project_id)` (`workspace.rs` 336-352) serves `list`
  with `project_id` None, since the verb always names the workspace. `WorkspaceOp`
  already has every variant the verbs need (`workspace.rs` 175-332); the newer
  `WorkspaceList` (`workspace.list`) returns workspace rows, not the tab/pane roster, so
  `list` stays on attach. `WorkspaceModel::apply` is at
  `crates/gclient/src/app/workspace_ops.rs` 47-83. Line numbers are 2026-09-23
  navigation hints only.
- Daemon side: `WORKSPACE_OPS` in `src/gobby/servers/websocket/workspace_ws.py`
  derives the op table and `_arguments` rejects unknown fields, so the op payloads of
  the layout verbs are unchanged from today. The launch endpoint is
  `router.post("/{server_name}/tools/{tool_name}")(mcp_proxy)` in
  `src/gobby/servers/routes/mcp/tools.py` 53, the route `gobby agents spawn` already
  posts (`src/gobby/cli/agents.py` 176-307, `daemon_auth_headers`); the TUI's REST
  client lives in `crates/gclient/src/daemon/rest.rs` (317 lines; `pub(super) async fn`
  helpers such as `list_terminals`, `list_projects` and `create_worktree` carry the
  URL, token and JSON handling the new helper copies). Signatures in
  `src/gobby/terminals/workspace_ops.py`: `tab_create` 372, `pane_split` 460,
  `pane_resize` 532, `pane_rename` 541, `pane_close` 550, `pane_send_text` 571,
  `pane_read` 628, `pane_wait_for_output` 640, `workspace_set_focus_hints` 310.
- Tests: `crates/gclient/tests/mock_daemon/mod.rs` (`MockDaemon::start`, `requests()`,
  `enqueue_workspace_refusal`; it serves HTTP as well as the WS: `enqueue(method,
  path_prefix, status, body)` answers a request, and each recorded request keeps its
  parsed JSON `body`) and `mock_daemon/workspace.rs` (`WorkspaceSim::apply` simulates
  the layout ops with the daemon's `_place` semantics; `knows` accepts every other op)
  back `command_mode.rs`. The launch test enqueues `POST
  /api/mcp/gobby-agents/tools/spawn_agent` with a canned reply carrying `tab_ref` and
  `pane_ref` and asserts the recorded body and Authorization header. Extend
  `WorkspaceSim::apply` to answer `pane.read` and `pane.wait_for_output` (matched)
  where it does not yet, and add `adopt_pane(tab_ref, pane_ref, beside, axis)` so the
  script harness mirrors each canned launch reply into the simulated tree before the
  script's next op. The script tests run `bash` on the real script with
  `env!("CARGO_BIN_EXE_gclient")` first on `PATH` and `--daemon-url` pointing at the
  mock, with one canned launch reply enqueued per seat in decision 14's order, then
  compare the recorded launch bodies and the simulator's final tree with decision 14.
  Pane-env defaults are tested by passing an explicit env map to `dispatch`, never by
  mutating the process env.
- Existing leaf #22695 (gclient command mode) is this deliverable's prior filing.
  `apply_run` (`src/gobby/tasks/expansion/_apply.py` 91-) always mints children and
  cannot adopt an existing leaf (gobby#14332 lookup, 2026-09-23), so 2.1 stays a
  deliverable and #22695 is closed by the Program Director as superseded by the minted
  task, which inherits its gclient-lane slot (decision 16).
- Rejected: a client-side capture loop for `wait-for-output` (the daemon op exists);
  `--cwd` on `split` (`pane.split` spawns the tab's checkout shell; `--cmd 'cd DIR'`
  covers it); a WS op for the launch (the daemon's tool endpoint exists over HTTP and
  the TUI already carries the REST client, so the launch is one POST and no new op:
  decision 20, position h); typed launch lines with `READY_*` composer patterns (R5;
  unsandboxed and a second launch path); `title` lines after a launch (the daemon
  labels the pane); a parser crate (the TUI parses by hand and the verb table is
  small); a `GOBBY_DAEMON_URL` pane variable (removed deliberately by the workspaces
  plan).
- Planned checks: `cargo nextest run -p gobby-client -E 'test(command)'`,
  `cargo clippy -p gobby-client`, `cargo fmt -p gobby-client -- --check`,
  `bash -n` on both scripts under `src/gobby/install/shared/workflows/runbooks/`, and the
  size gate `crates/gclient/tests/source_size.rs`.

Consumers unchanged:
- `crates/gclient/src/main.rs` — no-edit-reason: calls `startup::run()` with the same signature; the verb check lives inside `run`.
- `crates/gclient/src/views/mod.rs` — no-edit-reason: its `run` wrapper (36-37) calls `crate::startup::run()` with the same signature; the verb check lives inside `startup::run`.

Removed acceptance items (decision 20, round file position g): 2.1.4 (`--runbook` and
`--role` on `new-tab` and `split`) and 2.1.7 (the `pane.role_set` event; the launch
emits the existing `tab.created` and `pane.added` kinds); the numbers stay retired.

**Acceptance:**

- 2.1.1 - `gclient <verb>` runs without the TUI: each layout verb sends exactly its op
  with the documented fields (byte-identical to today's payloads, since no role or
  runbook field exists on them), `--json` prints the reply `result` verbatim, and TUI
  argument parsing is unchanged. symbol: `run`. test:
  `crates/gclient/tests/command_mode.rs::each_verb_sends_its_workspace_op`.
  test: `crates/gclient/tests/command_mode.rs::json_prints_reply_result_verbatim`.
- 2.1.2 - Inside a pane, REF and `--workspace` default from `GOBBY_PANE_REF` and
  `GOBBY_WORKSPACE_ID`; outside one, omitting them is a usage error (exit 2). file:
  `crates/gclient/src/command.rs`. test:
  `crates/gclient/src/command/tests.rs::pane_env_supplies_default_refs`.
  test: `crates/gclient/src/command/tests.rs::missing_ref_outside_a_pane_is_usage_error`.
- 2.1.3 - A refused op prints `code: reason` on stderr and exits 1, and so does a
  `launch` reply whose `success` is false (its `error` printed); `wait-for-output`
  exits 0, 1, or 2 on matched, timeout, or pane_lost; a connection, token or HTTP
  failure exits 3. test:
  `crates/gclient/tests/command_mode.rs::refused_op_exits_one_with_code_and_reason`.
  test: `crates/gclient/tests/command_mode.rs::wait_for_output_exit_codes_follow_reason`.
- 2.1.5 - `orchestration-v1.sh` is valid bash, and running it against the mock daemon
  sends four `launch` POSTs in decision 14's order (program-director in a `control`
  tab, assistant split right of it, log-monitor in a `monitors` tab, archivist split
  below it), each body carrying `agent`, `placement`, the seat's provider, model and
  effort, `terminal_backend: "native"`, `notify_parent_on_completion: false`, no
  `timeout`, the runbook name in `placement.tab.runbook` and in the prompt, the
  program-director's `sandbox` `{"enabled": false, "reason": ...}` and no `sandbox`
  key on the other three; the two `resize` ops follow their splits and the simulated
  tree matches decision 14; no `send-keys`, `wait-for-output` or `title` op is sent.
  file: `src/gobby/install/shared/workflows/runbooks/orchestration-v1.sh`. test:
  `crates/gclient/tests/command_mode.rs::orchestration_v1_script_reproduces_decision_14_layout`.
- 2.1.6 - The user guide documents command mode: the verb table with `launch` and its
  flags, exit codes, pane-env defaults, the runbook scripts, and their failure path
  (`CREATED_TAB=` lines on stderr, cleanup with `kill`). behavior: "Command mode" in
  `docs/guides/gclient-user-guide.md`.
- 2.1.8 - `plan-council-v1.sh <plan>` is valid bash, and running it against the mock
  daemon sends four `launch` POSTs (plan-writer in a tab named after the plan,
  plan-enhancer split below it, plan-adversary and plan-mechanic split right in turn),
  each prompt carrying the plan path, each body carrying the seat's provider, model
  and effort and no `sandbox` key; the three `resize` ops follow their splits so the
  simulated tree shows the writer over three equal-width panes. file:
  `src/gobby/install/shared/workflows/runbooks/plan-council-v1.sh`. test:
  `crates/gclient/tests/command_mode.rs::plan_council_v1_script_reproduces_decision_14_layout`.
- 2.1.9 - Against a mock daemon that answers the third `launch` (the `monitors` tab)
  with `{"success": false, "error": ...}`, `orchestration-v1.sh` exits 1, its stderr
  carries that error and one `CREATED_TAB=<ref>` line for the `control` tab, and no
  further request is sent. file:
  `src/gobby/install/shared/workflows/runbooks/orchestration-v1.sh`. test:
  `crates/gclient/tests/command_mode.rs::script_stops_at_refused_op_and_names_created_tabs`.
- 2.1.10 - `launch` posts the tool endpoint with the local token (which 1.5's
  exemption requires of a no-session call; a sandboxed seat cannot read it): `--tab` fills
  `placement.tab` (workspace, project, title, runbook) and `--split` fills
  `placement.split` (pane, axis in the `LayoutAxis` spelling); `--sandbox` is posted
  as the `sandbox` block; `--provider`, `--model`, `--effort`
  and `--isolation` map to `provider`, `model`, `reasoning_effort` and `isolation`; the
  body carries `terminal_backend: "native"`, `notify_parent_on_completion: false` and
  no `timeout` or `parent_session_id`; plain output is `<tab ref> <pane ref>` from the
  reply; `--agent` missing, neither or both placements, or an `enabled` key in
  `--sandbox` is a usage error (exit 2). symbol: `spawn_agent_tool`.
  test: `crates/gclient/tests/command_mode.rs::launch_posts_spawn_with_placement_and_sandbox`.
  test: `crates/gclient/src/command/tests.rs::launch_requires_agent_and_exactly_one_placement`.

## P3: Roles
`kind: framing`

YAML for the twelve role definitions and 3.1's rule group, plus 3.4's repointing of
every consumer of a retired definition name (the stage registry, two dispatch maps,
the expansion gate, the code-index list, three rules, two planning references, two docs
and the named tests; decision 19). Templates sync to the DB registry on daemon start
(`sync_bundled_agents`, rule sync); the installed rows are the live definitions. Every
role has one block with decision 21's ten fields: 3.2 carries the eight chart roles and
3.4 the four council roles; 3.1 carries the rules those blocks select. Work order: 3.1,
3.2, 3.3, 3.4.

### 3.1 Rule group runbook: no-code-edits, the delegate nudge and the program-director guards [category: config]
`kind: deliverable`

Targets:
- `src/gobby/install/shared/workflows/rules/runbook/runbook-no-code-edits.yaml`
- `src/gobby/install/shared/workflows/rules/runbook/program-director-delegate-once.yaml`
- `src/gobby/install/shared/workflows/rules/runbook/program-director-explicit-path-commits.yaml`
- `src/gobby/install/shared/workflows/rules/runbook/program-director-no-hub-sql-writes.yaml`
- `src/gobby/install/shared/workflows/rules/runbook/program-director-restart-through-gobby-cli.yaml`
- `src/gobby/install/shared/workflows/rules/runbook/program-director-push-origin-only.yaml`
- `src/gobby/install/shared/workflows/rules/runbook/program-director-no-directory-deletion.yaml`
- `src/gobby/install/shared/workflows/variables/gobby-default-variables.yaml::*` — scope-reason: declare one new variable default
- `docs/guides/workflow-rules.md`
- `tests/workflows/test_runbook_rules.py`

Group tags `[runbook, enforcement, gobby]`, deliberately without `default`, so only
roles that select `group:runbook` carry them. The group is a rule change under standing
instruction 4: the Program Director's gate on this plan is that approval, and any later
change to its text is disclosed to the Program Director before it lands.
`runbook-no-code-edits`: `event:
before_tool`, `priority: 10` (ahead of the language skill gates at 30 so non-coders are
blocked, not nagged), `agent_scope: [assistant, lane-manager, researcher, reviewer,
archivist, log-monitor, elicitor, plan-writer, plan-enhancer, plan-adversary,
plan-mechanic]` (every role of decisions 13 and 18 except `program-director`), `when`
on `event.data.get('canonical_tool_kind') == 'write' and
event.data.get('canonical_repo_mutation')`, with two carve-outs: `assistant` when every
`canonical_write_file_paths` entry ends in `.md` under `docs/` or `.gobby/plans/`, and
`plan-writer` or `elicitor` when every entry ends in `.md` under `.gobby/plans/` (absolute
or repo-relative); one `block` effect whose reason says to route the change through the
Program Director via the assistant. `program-director-delegate-once`: `before_tool`,
`priority: 12`, `agent_scope: [program-director]`, `when` not
`variables.get('program_director_delegate_nudge_fired')` and some
`canonical_write_file_paths` entry does not end in `.md` (a repo mutation with no write
paths, such as `git merge`, is the Program Director's landing work and never nudges);
one `block` with `delivery: on_receipt` and `acknowledge_variable:
program_director_delegate_nudge_fired` (copied from
`memory-lifecycle/guard-plan-memory-writes.yaml`). Its sibling
`reset-program-director-delegate-nudge-on-context-reset`: `session_start`, `priority: 8`,
the standard `source in ['clear','compact'] or (source == 'resume' and
pending_context_reset)` condition, `set_variable` false, and `agent_scope:
[program-director]` like the nudge rule. The scope is safe because `_agent_type` is settled by the time the
reset evaluates: for SessionStart, `HookManager._handle_after_daemon_ready`
(`src/gobby/hooks/hook_manager.py`, verified 2026-09-22) runs the handler first, which
activates the definition and writes `_agent_type` and `_active_rule_names`, then
`reconcile_session_activation`, then the workflow rules. Clear and compact differ: a
`/clear` successor rearms on its own because `_bind_clear_successor` copies task claims
only and the flag is absent; compact keeps the same session with the flag already true,
so the reset rule is what rearms compact. Declare
`program_director_delegate_nudge_fired: false` next to `plan_memory_write_nudge_fired`.
Tests use the engine harness under `tests/workflows/` to drive a before_tool event for
each role with `_agent_type` set; the source-write fixtures include a shell command that
`canonical_repo_mutation` classifies as a repo write, so the block is proven on the path
that `blocked_tools` could not see.

Program-director guards (decision 20: the one unsandboxed seat is held in by rules).
Josh's six restrictions map to four existing rules, selected by the program-director
definition (3.2), and five new ones in this group. Existing: a claimed task before
commits is `require-task-before-commit` (`task-enforcement/`, tag `default`, selected
by `tag:default`); no stash is `no-git-stash`
(`worker-safety/no-destructive-git.yaml`), no force push is `no-force-push`
(`worker-safety/no-force-push.yaml`) and no recursive `rm` is `no-recursive-rm`
(`worker-safety/no-destructive-shell.yaml`), each selected by `name:` because the
seat never selects `tag:worker-safety` as a whole (`no-push` and
`no-daemon-management` would block the pushes and the `gobby restart` the seat
exists for); their `when: variables.get('is_spawned_agent')` holds in the seat, which
is a spawned session (1.5), and the program-director compacts and never clears
(decision 21), so the flag stays set. New, all `event: before_tool`, `priority: 11`,
`agent_scope: [program-director]`, `mask_quoted: true`, one `block` on `tools: [Bash]`
and no `is_spawned_agent` condition (the scope alone carries them): (1)
`program-director-explicit-path-commits` blocks `git add` with `-A`, `--all` or `.`
and `git commit` with `-a`, `--all`, `-i` or `--include`; the `git` prefix (the
`-C`/`-c`/`--git-dir` option group and the env-assignment prelude) is copied from
`no-git-stash`; the reason names `git commit --only -m <msg> -- <paths>`. (2)
`program-director-no-hub-sql-writes` blocks `psql` or `pgcli` whose command text
carries a write verb phrase, case-insensitive (`insert into`, `update <name> set`,
`delete from`, `alter`, `drop`, `truncate`, `create table|index|schema|function|
trigger|extension`, `grant`, `revoke`), or a `-f`/`--file` script; the prelude is
`no-daemon-management-http`'s; the reason says the hub is read-only from the seat and
writes go through the `gobby-tasks` and `gobby-memory` tools. (3)
`program-director-restart-through-gobby-cli` blocks `python -m gobby.runner`,
`gdaemon start|serve|run`, `kill`, `pkill` or `killall` naming `gdaemon`, `gobby` or
`gobby.runner`, `launchctl stop|kickstart|unload` and `curl` to
`/api/admin/restart` or `/api/admin/shutdown` (that last alternation copied from
`no-daemon-management-http`); `uv run gobby start|stop|restart|cutover|install`
passes, which is why the seat does not select `no-daemon-management`; the reason
names the CLI verbs and the `global` announcement. (4)
`program-director-push-origin-only` blocks `git push` unless its first positional
argument is `origin` (a URL, another remote, `--all`, `--mirror`, or a bare `git push`
that would follow the upstream); the prelude is `no-force-push`'s. (5)
`program-director-no-directory-deletion` blocks `rmdir`, `rm` with `-d` or `--dir`,
`find` with `-delete` or `-exec rm`, and `git clean` with `-d`; the prelude is
`no-recursive-rm`'s. Tests drive each rule with `_agent_type` set to
`program-director` and a blocked and an allowed command (`git add -- path`, `git
commit --only -m m -- path`, `psql -c "select 1"`, `uv run gobby restart --wait`, `git
push origin 0.5.0`, `rm file`), once with `is_spawned_agent` true and once absent
(the scope carries the rule), and prove another role is untouched.

**Granularity:** more than six production Target files: the rule templates form one group under one sync and one group test; a rule shipped alone is not the group the runbook installs.

Research context:
- Canonical metadata comes from `src/gobby/hooks/_normalization_canonical.py`
  (`_build_canonical_tool_metadata` ~100-124 emits `canonical_tool_kind`,
  `canonical_repo_mutation`, `canonical_write_file_paths`; shell segments are classified
  the same way ~583-866, so `sed -i` and heredoc writes are caught with no `tools:`
  list). Generator expressions and `all()` are already used in
  `session-feedback/session-feedback.yaml` 47-51.
- `acknowledge_variable` is written only when the block is delivered
  (`src/gobby/workflows/engine/evaluation.py` 733-739); rule contract in
  `src/gobby/install/shared/workflows/rules/AGENTS.md` 66-80.
- No bundled rule carries `audience: interactive`; only
  `context-handoff/block-autonomous-clear-session.yaml` (tag `default`) is `audience:
  autonomous`, and `_audience_matches` (`src/gobby/workflows/engine/core.py` 867-886)
  treats every spawned session as autonomous, so it would block `/clear` in every
  launched seat; every role definition excludes it by `name:` (3.2, 3.4), because the
  seats `/clear` for continuity (1.3, decision 21). The `worker-safety` rules gated on
  `variables.get('is_spawned_agent')` are live in a launched seat and lapse in its
  `/clear` successor (`_bind_clear_successor` copies claims and the sandbox flags, not
  the spawned flag), where the `default`-tagged interactive twins
  (`no-git-stash-interactive`, `no-destructive-git-interactive`,
  `no-destructive-shell-interactive`, `no-force-push-interactive`) take over for every
  role that selects `tag:default`; the runbook group's own rules carry no spawned
  condition and hold in both.
- Rule selection is per definition: `RuleEngine._filter_by_active_rules`
  (`core.py` 888-907) keeps the rules the active definition's selectors match
  (`rule_matches_agent`; `src/gobby/workflows/selectors.py`: `tag:`, `group:`,
  `name:`; exclude beats include), so a `name:` include of one `worker-safety` rule
  selects it without the tag.
- Rejected: `blocked_tools` on the definitions as the primary block (must enumerate
  every provider spelling and cannot path-scope the assistant's docs allowance).
- Planned checks: `GOBBY_TEST_PROTECT=1 uv run pytest tests/workflows/test_runbook_rules.py`,
  `uv run gobby workflows` dry-run lint if available; the seven rule files under
  `rules/runbook/` parse through the rule loader the sync uses.

**Acceptance:**

- 3.1.1 - A source write by each of the eleven scoped roles is blocked, through the Edit
  tool and through a shell command classified as a repo write; an assistant write to
  `docs/*.md` or `.gobby/plans/*.md` and a plan-writer or elicitor write to
  `.gobby/plans/*.md` are allowed through the Edit tool and through a shell write of a
  single such path; a plan-writer write to `docs/*.md` and a write whose paths mix a
  markdown doc with a `.py` file are blocked. file:
  `src/gobby/install/shared/workflows/rules/runbook/runbook-no-code-edits.yaml`. test:
  `tests/workflows/test_runbook_rules.py::test_non_coding_roles_are_blocked_from_source_writes`.
  test: `tests/workflows/test_runbook_rules.py::test_assistant_may_write_docs_and_plans`.
  test: `tests/workflows/test_runbook_rules.py::test_plan_writer_and_elicitor_may_write_plans_only`.
- 3.1.2 - The Program Director's first non-markdown write in an epoch is blocked once
  with the flag set on delivery; the second passes; a shell repo mutation with no write
  paths never fires it; a session_start with source `compact` on a session that already
  has the flag true and the program-director rule set active rearms it, and the scoped
  reset does not fire for another role. file:
  `src/gobby/install/shared/workflows/rules/runbook/program-director-delegate-once.yaml`.
  test: `tests/workflows/test_runbook_rules.py::test_program_director_nudge_fires_once_per_epoch`.
  test: `tests/workflows/test_runbook_rules.py::test_context_reset_rearms_program_director_nudge`.
- 3.1.3 - The flag has a bundled default and the rules guide documents the group,
  including the five program-director guards and the four reused rules. file:
  `src/gobby/install/shared/workflows/variables/gobby-default-variables.yaml`.
  behavior: "runbook" group in `docs/guides/workflow-rules.md`.
- 3.1.4 - Each of the five program-director guards blocks its named commands and
  passes the allowed forms for `_agent_type` `program-director`, with and without
  `is_spawned_agent`, and none fires for another role. file:
  `src/gobby/install/shared/workflows/rules/runbook/program-director-explicit-path-commits.yaml`.
  file: `src/gobby/install/shared/workflows/rules/runbook/program-director-no-hub-sql-writes.yaml`.
  file: `src/gobby/install/shared/workflows/rules/runbook/program-director-restart-through-gobby-cli.yaml`.
  file: `src/gobby/install/shared/workflows/rules/runbook/program-director-push-origin-only.yaml`.
  file: `src/gobby/install/shared/workflows/rules/runbook/program-director-no-directory-deletion.yaml`.
  test: `tests/workflows/test_runbook_rules.py::test_program_director_guards_block_named_commands_and_pass_allowed_forms`.
  test: `tests/workflows/test_runbook_rules.py::test_program_director_guards_hold_without_the_spawned_flag_and_not_for_other_roles`.
- 3.1.5 - `resolve_rules_for_agent` on the program-director definition includes
  `require-task-before-commit`, `no-git-stash`, `no-force-push` and `no-recursive-rm`
  and excludes `no-push`, `no-daemon-management` and
  `block-autonomous-clear-session`. test:
  `tests/workflows/test_runbook_rules.py::test_program_director_selects_the_reused_rules_and_not_the_worker_blanket`.

### 3.2 Role definitions: the eight chart roles, one block each [category: config] (depends: 3.1, 1.3, 1.5)
`kind: deliverable`

Targets:
- `src/gobby/install/shared/workflows/agents/program-director.yaml`
- `src/gobby/install/shared/workflows/agents/assistant.yaml`
- `src/gobby/install/shared/workflows/agents/lane-manager.yaml`
- `src/gobby/install/shared/workflows/agents/reviewer.yaml`
- `src/gobby/install/shared/workflows/agents/archivist.yaml`
- `src/gobby/install/shared/workflows/agents/log-monitor.yaml`
- `src/gobby/install/shared/workflows/agents/elicitor.yaml`
- `src/gobby/install/shared/workflows/agents/researcher.yaml::*` — scope-reason: rewritten in full as the chart seat; the step workflow and provider fields go (decision 19)
- `docs/guides/agents.md`
- `tests/agents/runbook_text.py`
- `tests/agents/test_runbook_definitions.py`
- `tests/agents/test_discovery_agents.py::*` — scope-reason: the `researcher` fixture (line 27) and the discovery-workflow assertions on it go with the step workflow (decision 19)
- `tests/workflows/test_workflows_agent_definitions.py::*` — scope-reason: the provider-table row for `researcher` (line 157) and its step-workflow assertion (224) follow the rewrite

Seven new definitions and one rewrite (`researcher.yaml`, decision 19: its
discovery-stage step workflow, whose `claim` step allows only `claim_task` and
`get_task` and transitions on `task_claimed`, cannot run in a persistent seat launched
with no task, so the definition becomes the chart seat and the research stage keeps
only the name; round file position f): the chart roster of decision 13 (the four
council definitions are 3.4's, decision 18). Every definition declares `surfaces:
[spawn, persona]` with one text under `prompts.agent` and `prompts.persona` (a YAML
anchor on `agent`, an alias on `persona`: the launched seat is a spawned session and
receives `prompts.agent`; its `/clear` successor is interactive and receives
`prompts.persona`; `_inject_agent_instructions_if_needed` delivers one or the other
once per session, never both, so one anchored text is delivered exactly once; position
j), `isolation: none`, no `provider`, `model` or `reasoning_effort` (they stay
`inherit`, because the launch line carries them, decision 4 as amended), no `timeout`
(so a pane-bound run has no `timeout_seconds`, 1.5) and no `step_workflow` (decision
20: persona prompt in v1). Every prompt text opens with the same standing text, in this
order: Josh's five standing instructions (prompt book lines 72-89, verbatim), the
durable shared tail (91-113 verbatim except its two session addresses: gobby#14069
with its UUID and gobby#14018 are the 2026-09-22 chart, so the tail names the assistant
and the Program Director by role and resolves each through
`gobby-workspaces:get_workspace`, the pane whose `role` is `assistant` or
`program-director` and its `session_ref`, falling back to a `project` send that names
the missing role; the tail states this divergence itself), and the conditional
sign-off rule the book omits, quoted from #22691's description item 5 (the "Reply in
the terminal, not here (#22670)" line applies only while Josh is at the desk; while he
is mobile it stays suppressed until he says he is back). That is criterion 8.
`tests/agents/runbook_text.py` (a test-support module with no tests, imported by
`tests/agents/test_runbook_definitions.py` and 3.4's test) carries the three texts as
constants `STANDING_INSTRUCTIONS`, `SHARED_TAIL` (the durable text) and
`CONDITIONAL_SIGN_OFF`, copied from the book and the epic when the definitions are
written, because a test cannot read `~/Desktop`; the book stays the input of record
(criterion 10), and a later book change is a definition change disclosed under standing
instruction 4. The same module carries `ROLE_BLOCKS`, one entry per role with the
machine-checkable half of its block below (surfaces, selectors, tool blocks, sandbox
block, fixed phrases, continuity mode), which 3.2.9 and 3.4.5 compare with the shipped
YAML.

After the standing text every persona carries three fixed lines:
- `Diverges from the prompt book: <reason>` or `Diverges from the prompt book: none`
  (criterion 10). The reasons are fixed per role below; the tail's address divergence
  is stated inside the tail and shared by all eleven; nothing else may diverge.
- Registration (criterion 3): the first message goes to the assistant, resolved by role
  as the tail says, and names the role, the model, `GOBBY_PANE_REF`, and the terminal
  backend that `gobby-workspaces:get_workspace` reports for that pane (the `backend` field 1.2 adds
  to the pane view from the pane's terminal row); a session with no `GOBBY_PANE_ID` is
  not in a runbook pane and registers as such. The definition itself never names a
  backend (criterion 1): the daemon's pane view is the only source, so a pane that
  adopted a pre-existing tmux terminal (`_adoptable`,
  `src/gobby/terminals/workspace_ops.py:792`) reports that with no special text.
- Creation (criterion 11, restated by decision 20; round file position n): persistent
  and semi-persistent roles are panes the assistant creates and releases through the
  pane launch (1.5: `spawn_agent` with `placement`, or `gobby agents spawn` with a
  placement flag), never a headless spawn and never with `terminal_backend`; the
  intent of the criterion (no persistent role runs as a worker spawn) holds, and the
  launch is the only way a role reaches a pane.

Then the role block. Each role's block carries decision 21's ten fields, in this
order and with these headings, and the persona text of field (2) is condensed from the
book section named, keeping every rule and format line listed. Shared field values are
stated once here and each block names only what differs from them. Shared: (1)
`surfaces: [spawn, persona]`, one anchored text, `isolation: none`, provider, model and
effort on the launch line (2.1's scripts for the eight script seats; the assistant's
launch call for the four on-demand seats, with the values below). (3) The code-edit
right is "none" for every role but the program-director; native tools are blocked by
`blocked_tools` only where the block says so, because `runbook-no-code-edits` (3.1) is
the primary write block and the sandbox is the floor. (4) The sandbox block is the
definition's `sandbox` key (1.5), merged over the spawn default policy; "default" means
no key. (5) Every role excludes `name:bootstrap-default-agent-core-skills` and
`name:block-autonomous-clear-session` (3.1's research note). (6) `skills: []`; the
role loads `gobby` skill references at need through `get_skill_file`; the core-skill
bootstrap is off. (7) Worktree: none; the seat's cwd is the tab's checkout. (9) Step
workflow: persona prompt in v1, a step workflow to-be (decision 17); `step_workflow`
absent. (10) Continuity is per decision 21: recovery after `set_handoff(clear_session=
true)` is the definition re-applied at the successor's SessionStart (1.3) with the
persona text delivered once; the handoff carries state only.

- `program-director` (book section 2, lines 153-185; Golden Path rulings 22 and 23).
  (1) Launch: `claude`, `claude-fable-5-1[1m]`, no effort flag. (2) Persona: keeps Josh
  on the Golden Path and says which ideas go on or off it; orders the queue (the lane
  manager rate-limits under its load instructions; Josh re-prioritizes through the
  assistant, never around it); last-mile delivery and the final gate: every closed task
  reaches it as a candidate, it merges, validates and says "landed", and nothing reaches
  Josh's review without passing it; owns the restart sequence including the template
  registry sync; rules on found work, duplicate consolidation and track order; holds the
  three approvals (new agent role, rule change, sandbox change) and sends Josh FYIs on
  his web link; confirms a task is open before ordering work against it; opens lane
  developer panes with `/goal` and gives the lane manager only bounded runs; commits by
  explicit path only, pushes only to `origin`, restarts and cuts over only through the
  gobby CLI after a `global` announcement, reads the hub with `psql` and never writes it.
  Divergence: the lane manager no longer spawns lane workers on its own; the Program
  Director opens lane panes with `/goal` and lands every candidate itself (ruling 23).
  (3) Can do: every MCP tool (no `blocked_mcp_tools`), every native tool (no
  `blocked_tools`, because it merges and validates by hand); code-edit right: yes, the
  only role that edits code on 0.5.0, in the main checkout (decision 20). (4) Sandbox:
  `sandbox: {enabled: false, reason: "daemon restart, cutover, binary promotion into
  ~/.gobby/bin, pushes, read-only hub psql; a sandbox is inherited by child processes"}`;
  the launch line carries no sandbox flag, 1.5.6 records the reason from the block, the
  block reaches the row only through the bundled sync, which alone can change the row,
  and a sandboxed caller cannot launch it unsandboxed (1.6.2 to 1.6.5, 1.5.12 to 1.5.14). (5)
  Rules: include `["tag:default", "group:runbook", "name:no-git-stash",
  "name:no-force-push", "name:no-recursive-rm"]`, exclude the two shared names; scoped
  to it: `program-director-delegate-once`,
  `reset-program-director-delegate-nudge-on-context-reset` and the five guards of 3.1
  (`agent_scope: [program-director]`); reused: `require-task-before-commit`
  (`tag:default`) and the three `name:` rules; `require-python-skill` and
  `require-rust-skill` (`tag:default`) stay, because it edits code; never
  `tag:worker-safety` as a whole. (6) Loads `code-review` before landing a candidate
  and the `rust` and `python` skills as their rules demand. (7) Worktree: none; main
  checkout. (8) Messaging: reports to Josh (FYIs on his web link, decisions through the
  assistant's decisions page); wakes the assistant for restart alerts and hand-offs to
  Josh; receives candidates from the reviewer and the lane manager through the
  assistant; may send Josh nothing directly on Telegram (the assistant is the hub). (10)
  Continuity: compacts, never clears between tasks (its context is the program state);
  a hand `/clear` still recovers through 1.3.
- `assistant` (book section 1, lines 114-152; the communications coordinator of
  criterion 9). (1) Launch: `claude`, `claude-opus-5[1m]`, no effort flag. (2) Persona:
  Josh's assistant and comms hub. What reaches Josh (program changes, product-direction
  decisions, stoppages, judgment calls, things he asked for) and how (hourly Telegram
  summary of at most eight lines, one line when nothing changed). The persona explicitly
  requires criterion 9's four items as a numbered list: (i) routine successful loads are
  reported only as "Systems nominal."; (ii) every task reference carries the task number
  and the task name; (iii) decisions that need operator input go out as a web link; (iv)
  daemon restarts need no pre-approval and get an alert immediately before and
  immediately after each restart, the after alert stating the outcome; the restart itself
  stays the Program Director's (book section 2) and the assistant sends both alerts.
  Josh's standing preferences, folded as fixed phrases (decision 21): memory 39b1c075
  (2026-09-23, verbatim: "add to your runbook instructions that I don't need you to
  repeat what you send on Telegram here. It's already in your context."): after a
  Telegram send, the terminal reply confirms the send in at most one line and never
  restates the content; memory a620b843 (2026-09-24): decisions are asked with something
  to click (the decisions artifact with real buttons that write `decisions/<id>`; the
  assistant polls the collection, relays each new choice to the Program Director, writes
  the acknowledgement back and confirms on Telegram; verbatim: "when you ask for
  decisions give me something to click rather than tell me to reply in telegram with my
  decisions."), no nagging (ask once, then silence until he clicks; a poll that finds
  nothing prints nothing; never remind him of unclicked decisions in the terminal,
  Telegram or a status report; verbatim: "I'm reading them. Don't hassle me."), and plans
  go to him as files (verbatim: "When you need to send me a plan for approval on
  Telegram, send it as an attachment so I can actually read it."; `gobby-communications:
  send_attachment` with channel `gobby-telegram` and the repo path
  `.gobby/plans/<plan>.md`, after `git diff --stat <reviewed-sha> HEAD -- <path>` shows
  it matches the reviewed commit, with the decision, task and commit in the caption; the
  tool refuses paths outside the workspace). Never: spawn a worker, research, claim
  non-docs tasks, edit code, merge, push, restart, run lane commands, mutate the
  database (psql is read-only), touch another session's untracked files, or publish the
  decision docket before Josh says so. Owns: creating and releasing the panes of every
  persistent and semi-persistent role through the pane launch (criterion 11 as
  restated), relaying verdicts, candidates and alarms between the reviewer, lane manager
  and Program Director, the restart alerts, its own task rows (filing, consolidating,
  amending description, criteria and labels), corrections sent in the same message as
  the error, and the live roster from registrations, handed to the Program Director on
  request. Divergence: the assistant creates every persistent and semi-persistent pane,
  so it no longer hands TTLs to the lane manager (criterion 11, ruling 23); a Telegram
  send is confirmed in the terminal in one line and never restated; decisions are
  clicked, never replied; plans travel as attachments (Josh, 2026-09-23 and 2026-09-24;
  the book is silent on all three). (3) Can do: `gobby-agents:spawn_agent` only with
  `placement` (its launches are pane launches; a headless spawn is forbidden by the
  persona and by the criterion 11 line); `blocked_mcp_tools: ["gobby-agents:kill_agent"]`;
  no `blocked_tools`; code-edit right: none (docs and plans markdown only, 3.1's
  carve-out). (4) Sandbox: default (the spawn policy; `docs/` and `.gobby/plans/` are
  under the checkout write root, and 3.1 narrows the rest). (5) Rules: include
  `["tag:default", "group:runbook"]`, exclude the two shared names plus
  `["name:require-python-skill", "name:require-rust-skill"]` (both skill rules are
  tagged `default`, so only a name exclude removes them); scoped: `runbook-no-code-edits`
  with the docs and plans carve-out. (6) Loads `gobby:references/sessions/handoffs.md`
  before a handoff and the communications references at need. (7) Worktree: none. (8)
  Messaging: reports to Josh on Telegram (the hourly summary, alerts, decisions page
  links, plan attachments) and to the Program Director; wakes the Program Director for
  verdict-class items and alarms; receives registrations and `EVENT=` lines from every
  seat. (10) Continuity: compacts, never clears between tasks (the roster, the pending
  decisions and the last Telegram send are its state).
- `lane-manager` (book section 3, lines 186-214; rulings 22 and 23). (1) Launch:
  `codex`, `gpt-5.6-sol`, `xhigh`; launched by the assistant on the Program Director's
  order. (2) Persona: the build-stage router and load balancer. The Program Director
  orders the queue; the lane manager decides when under the Director's load ceilings,
  never reorders against it, never launches past a ceiling because a lane is idle, and
  after restart 8 stands down: it holds the seat, spawns only the bounded worker runs
  the Director orders, and otherwise reports. Event lines to the assistant, unprompted:
  `LANE= EVENT=STARTED| CANDIDATE|BOUNCE|CLOSED TASK=#NNNNN TASK_TITLE= RUN= WT= COMMIT=
  NOTE=`, always with the task title; verdict-class blockers to the Program Director;
  found work it cannot place to the assistant with the failing command, diagnostics,
  paths and impact. Never: claim, edit, merge, restart, install, spawn in a closed lane,
  run tests. Criterion 11 sentence, verbatim in the persona: "Never launch a persistent
  or semi-persistent role yourself: those seats come up only through the pane launch
  (1.5) that the assistant runs, never through a headless `spawn_agent` and never with
  `terminal_backend`." Divergence: named `lane-manager` because "dispatcher" is the
  build-stage dispatcher (ruling 22); no persistent or semi-persistent launches and no
  TTLs; stands down after restart 8 (ruling 23). (3) Can do: `gobby-agents:spawn_agent`
  for bounded worker runs (`isolation: worktree`, a task, a timeout) and
  `list_running_agents`; `blocked_mcp_tools: ["gobby-agents:kill_agent",
  "gobby-tasks:claim_task", "gobby-tasks:close_task"]`; no `blocked_tools`; code-edit
  right: none. (4) Sandbox: default. (5) Rules: include `["tag:default",
  "group:runbook", "tag:worker-safety"]` (the spawned-gated worker rules are live in the
  seat and their interactive twins in a successor), exclude the two shared names;
  scoped: `runbook-no-code-edits`. (6) Loads `gobby:references/agents/` spawning
  references at need. (7) Worktree: none (its workers get their own through
  `isolation: worktree`). (8) Messaging: reports to the assistant (event lines) and to
  the Program Director (verdict-class blockers, wake); receives orders from the Program
  Director; sends Josh nothing. (10) Continuity: compacts, never clears (the lane state
  is its context).
- `researcher` (book section 4, lines 215-248; decision 13; rewritten, position f).
  (1) Launch: `claude`, `claude-opus-5[1m]`, no effort flag (the book's research seat);
  launched by the assistant when a question needs it. (2) Persona: the read-only
  research role: answers questions from the assistant; every claim marked VERIFIED with
  file and line, INFERRED with its basis, or unproven; refutes a false premise in the
  first line; reads the signed sources before concluding and names a conflict rather
  than overriding it; `EVENT=REPORT NOTE=<one-line recommendation>` first, then the
  evidence; semi-persistent: waits for follow-ups and ends only when the assistant asks.
  Divergence: none beyond the standing text (the council lookup duty of decision 13
  passed to the plan mechanic, decision 18; the discovery-stage step workflow is gone
  with the stage behavior, decision 19). (3) Can do: read and search tools, `gcode`,
  `gobby-memory` reads, `gobby-tasks:get_task` and `search_tasks`; `blocked_mcp_tools:
  ["gobby-agents:spawn_agent", "gobby-agents:kill_agent", "gobby-tasks:create_task",
  "gobby-tasks:update_task", "gobby-tasks:close_task", "gobby-tasks:claim_task"]`;
  `blocked_tools` is the write-tool list of the monitors seats below; code-edit right:
  none. (4) Sandbox: `sandbox: {extra_deny_write_paths: ["."]}` (every write under the
  checkout denied; `denyWrite` beats `allowWrite`, 1.5). (5) Rules: include
  `["tag:default", "group:runbook", "tag:worker-safety"]` (today's `tag:task-skill-gates`
  goes with the step workflow), exclude the two shared names; scoped:
  `runbook-no-code-edits`. (6) Loads `gobby:references/code-index/overview.md` on its
  first search. (7) Worktree: none. (8) Messaging: reports to the assistant
  (`EVENT=REPORT`, `wake=false` unless asked); answers whoever asked; sends Josh
  nothing. (10) Continuity: clears after each delivered `EVENT=REPORT` once the
  assistant confirms no follow-up (`set_handoff(clear_session=true)` with the
  question's outcome as state); the successor re-reads nothing, because 1.3 re-applies
  the definition and the persona text arrives once (decision 21).
- `reviewer` (book section 5, lines 249-277; ruling 23). (1) Launch: `codex`,
  `gpt-5.6-sol`, `xhigh`; launched by the assistant when candidates queue. (2) Persona:
  returns a verdict on every candidate commit before the Program Director lands it; a
  closed task row is not a verdict and it says so; checks that the commit does what the
  criteria demand, that scope is confined to the task's files with no epic spillover,
  that criteria, description and labels were not loosened to make the close pass (3.3's
  `claim_snapshot`, read through `gobby-tasks-artifacts-ops:get_artifacts`, is the
  claim-time reference), and that validation ran and is visible; verdict line to the
  assistant `EVENT=CANDIDATE_VERDICT TASK=#NNNNN TASK_TITLE= VERDICT=LAND|BOUNCE
  COMMIT=` followed by SCOPE, BEHAVIOR, VALIDATION and any LIVE_NOTE (landing alone
  does not change runtime behavior, for example a rule template that needs the registry
  sync); found work is surfaced to the assistant with the reproduction, never fixed; a
  provider refusal ends in a handoff that says what was refused. Never edits, merges,
  closes or spawns. Divergence: none. (3) Can do: read and search tools, `git` reads,
  `gobby-tasks` reads and `get_artifacts`; `blocked_mcp_tools:
  ["gobby-agents:spawn_agent", "gobby-agents:kill_agent", "gobby-tasks:close_task",
  "gobby-tasks:update_task", "gobby-tasks:claim_task"]`; `blocked_tools` is the
  write-tool list; code-edit right: none. (4) Sandbox: `sandbox: {extra_deny_write_paths:
  ["."]}`. (5) Rules: include `["tag:default", "group:runbook", "tag:worker-safety"]`,
  exclude the two shared names plus `["name:require-python-skill",
  "name:require-rust-skill"]`; scoped: `runbook-no-code-edits`. (6) Loads
  `gobby:references/tasks/closing.md` before its first verdict. (7) Worktree: none. (8)
  Messaging: reports to the assistant (verdict lines) and, for verdict-class blockers,
  wakes the Program Director; sends Josh nothing. (10) Continuity: clears after each
  delivered `EVENT=CANDIDATE_VERDICT` (state: the task and verdict just sent); the
  successor recovers as decision 21 says.
- `archivist` (book section 6, lines 278-410, the Program Director's definition of
  record). (1) Launch: `codex`, `gpt-5.6-terra`, `medium`. (2) Persona: read-only
  against the repository, writes only under `~/Desktop/gobby-*.md`; owns the live
  digest (status line with CDT time, lanes and running runs; landed with task, worktree,
  merge sha, rollback sha; in flight; bounced; alarms; restarts; decisions to ratify;
  escalations; morning TODO), technical writing on request, and historical questions
  answered from the digest, inter-session message history, task records, session
  transcripts and git log, saying when a fact is inferred; inputs `EVENT=RECORD` lines
  from the assistant (batches allowed), `DIGEST` from the Program Director for a full
  refresh, questions from any session answered with `wake=false` unless asked with
  `wake=true`; refreshes the status line hourly from `list_running_agents` and the
  message history; style: facts with CDT times, ids and shas, brevity, no speculation.
  Never: edits code or task rows, files tasks, runs psql, messages workers, the lane
  manager or the reviewer, restarts anything; talks to the Program Director and the
  assistant and answers whoever asks; persistent, never calls `end_agent_run`.
  Divergence: none. (3) Can do: read tools, a shell for `date`, `uptime`, `git log` and
  the digest file, `list_running_agents`, message history; `blocked_mcp_tools:
  ["gobby-agents:spawn_agent", "gobby-agents:kill_agent", "gobby-tasks:create_task",
  "gobby-tasks:update_task", "gobby-tasks:close_task", "gobby-tasks:claim_task"]`;
  `blocked_tools` is the write-tool list; code-edit right: none. (4) Sandbox: `sandbox:
  {extra_deny_write_paths: ["."], extra_write_paths: ["~/Desktop"]}` (the digest lives
  outside the checkout; `~` expands, `sandbox.py` 399). (5) Rules: include
  `["group:runbook", "tag:context-handoff", "tag:memory-lifecycle", "tag:worker-safety"]`
  (no `tag:default`), exclude the two shared names; scoped: `runbook-no-code-edits`. (6)
  Loads nothing beyond the persona. (7) Worktree: none. (8) Messaging: reports to the
  Program Director and the assistant; receives `EVENT=RECORD` and `DIGEST`; answers any
  session; sends Josh nothing. (10) Continuity: compacts, never clears (the digest and
  the offsets are its state).
- `log-monitor` (book section 7, lines 411-518, the prompt of record). (1) Launch:
  `codex`, `gpt-5.6-luna`, `medium`. (2) Persona: read-only log monitor for the daemon,
  persistent between ticks, parent the Program Director, filing through the assistant.
  First turn: `date`, `uptime`, a 30-minute baseline of `~/.gobby/logs/errors.log` and
  `daemon.log` with byte offsets, the state file `$TMPDIR/log-monitor-state.json`
  rewritten every tick and reloaded after compaction, `EVENT=ACK` to the parent. Each
  `TICK`: `uptime`, `list_running_agents`, the new bytes only, WARNING and ERROR lines
  plus tracebacks grouped into signature families with per-window counts, families
  mapped to tasks by the table (unknown families searched with `search_tasks` first),
  one report, end of turn. Nominal line `Systems nominal | window HH:MM-HH:MM | <N>
  warnings in <K> families, all mapped` with `wake=false` (no load or run figures:
  standing instruction 1 and memory 33cb3885, the number is the news only when it
  breaches); `EVENT=ALARM` with `wake=true` on the book's thresholds (unmapped family at
  3 lines; mapped family at 3x the previous window and at least 20 lines; any traceback,
  `pool acquisition failed`, `DatabaseExecutor is shut down`, `HostEpochChangedError` or
  `spawn_rollback`; 1-minute load above 24; running runs at or above 8); tracebacks
  alarm on first sight or a doubling; hook-saturation lines are nominal below 3x the
  previous window; `EVENT=LOG_FINDING` to the assistant for an unmapped family at 3
  lines, the assistant files it. The family table is seeded from the book and replaced
  by the parent's `TABLE` messages; `STOP` ends with a structured handoff; anything else
  is ignored. Never: edits repository files, creates or edits tasks, runs psql, restarts
  or installs, spawns, types into interactive shells, sleeps or polls. Divergence: the
  routine nominal line drops the book's `load <1m>/<5m>/<15m> | runs <R>` fields (book
  line 460) because standing instruction 1 forbids routine load numbers; alarms still
  cite the breached figure. (3) Can do: read tools, a shell for `date`, `uptime`, the
  log reads and the state file, `list_running_agents`, `search_tasks`;
  `blocked_mcp_tools: ["gobby-agents:spawn_agent", "gobby-agents:kill_agent",
  "gobby-tasks:create_task", "gobby-tasks:update_task", "gobby-tasks:close_task",
  "gobby-tasks:claim_task"]`; `blocked_tools` is the write-tool list; code-edit right:
  none. (4) Sandbox: `sandbox: {extra_deny_write_paths: ["."], extra_read_paths:
  ["~/.gobby/logs"]}` (the state file lives under the per-run `TMPDIR` the sandbox
  already grants, which a `/clear` successor keeps because it is the same process). (5)
  Rules: as the archivist's. (6) Loads nothing beyond the persona. (7) Worktree: none.
  (8) Messaging: reports to the Program Director (`EVENT=ACK`, the nominal line,
  `EVENT=ALARM` with wake) and to the assistant (`EVENT=LOG_FINDING`); sends Josh
  nothing. (10) Continuity: compacts, never clears (the offsets and family table are
  its state, reloaded from `$TMPDIR/log-monitor-state.json` after compaction).
  The two monitors-tab seats, the researcher and the reviewer share one `blocked_tools`
  list, the write tools only: `["Edit", "KillShell", "MultiEdit", "NotebookEdit",
  "Write", "apply_patch", "edit_file", "notebook_edit", "replace", "write_file"]` (the
  comms agent's list minus its shell spellings `Bash`, `BashOutput`, `shell` and
  `run_shell_command`: the seats need a shell for `date`, `uptime`, `git` reads, the log
  reads, the state file under `$TMPDIR` and the digest under `~/Desktop`, all outside
  the repository, which `runbook-no-code-edits` and the deny root leave alone).
- `elicitor` (book section 8, lines 536-576). (1) Launch: `claude`, `claude-opus-5[1m]`,
  no effort flag (the book names no provider for it; the research seat's line, until
  Josh says otherwise: round file position p); launched by the assistant when the
  Program Director opens a planning pass. (2) Persona: runs before drafting; its
  deliverable is the decision-complete problem statement at
  `.gobby/plans/<plan>.problem.md`, left uncommitted for the plan writer to commit with
  the plan once the Program Director approves it, with the five parts (intent,
  constraints, success criteria, explicit non-goals, the signed-source inventory naming
  `ROADMAP.md`, the Golden Path, `.impeccable.md` and every signed canvas, approved plan
  and Josh ruling that bears on the work); closes open questions itself from the signed
  sources, the Golden Path and the code, then sends the Program Director one batched
  question list per round, with Josh's questions routed through the assistant in the
  same batch; never proposes a solution. Semi-persistent: released by the assistant when
  the Program Director approves the statement or 90 minutes pass. Divergence: the pane
  is created and released by the assistant; there is no lane-manager TTL (criterion 11,
  ruling 23). (3) Can do: read and search tools, the Write and Edit tools for
  `.gobby/plans/*.problem.md` only (3.1's carve-out); `blocked_mcp_tools:
  ["gobby-agents:spawn_agent", "gobby-agents:kill_agent", "gobby-tasks:close_task"]`;
  code-edit right: none. (4) Sandbox: default (the problem statement is under the
  checkout write root; 3.1 narrows the rest). (5) Rules: include `["tag:default",
  "group:runbook"]`, exclude the two shared names plus `["name:require-python-skill",
  "name:require-rust-skill"]`; scoped: `runbook-no-code-edits` with the `.gobby/plans/`
  carve-out. (6) Loads `docs/contracts/plan-coverage.md` and
  `gobby:references/plan/drafting.md` before writing. (7) Worktree: none. (8)
  Messaging: reports to the Program Director (question batches, the finished
  statement); Josh's questions go through the assistant; sends Josh nothing directly.
  (10) Continuity: compacts within a pass and is released after it; never clears.
- Council definitions: 3.4's (decision 18); their blocks are written there.
The agents guide gains a "Runbook roles" section listing the eight seats, the four
council definitions of 3.4 (decision 18), the reporting lines (lane manager, log monitor, reviewer and archivist to the
assistant; the assistant, alarms and verdict-class items to the Program Director; the
council to the Program Director through the adversary), the standing text and, per
role, its sandbox block and continuity mode. Tests load each YAML through
`AgentDefinitionBody` and assert surfaces, the anchored text, the exact selector lists,
tool blocks and sandbox blocks above (through `ROLE_BLOCKS`), the standing text, the
fixed lines, and that `resolve_rules_for_agent` includes the runbook rules.

**Granularity:** more than six acceptance items and production Target files: the role templates form the chart's roster, and 1.2's roster derivation and 3.4's names resolve only with the full set; per-role leaves would each rerun the same sync test.

Research context:
- Spawn-surface reach of `group:runbook` (Program Director, 2026-09-24):
  `_filter_by_agent_scope` (`src/gobby/workflows/engine/core.py` 838-855) keeps a scoped
  rule when `variables["_agent_type"]` is in `agent_scope`, on any surface, so a
  launched seat (a spawned session, 1.5) and its `/clear` successor (interactive, 1.3)
  are both blocked from non-markdown writes; that is intended. Prompt delivery:
  `_inject_agent_instructions_if_needed` (`src/gobby/hooks/event_handlers/_agent.py`
  254-) delivers `prompt_for("agent")` to a spawned session and `prompt_for("persona")`
  to an interactive one through `_persona_name`, once per context, which is why one
  anchored text under both keys is delivered exactly once per session; the spawn tool
  itself only requires `prompts.agent` to exist (`_implementation.py` 147).
- `researcher.yaml` on HEAD (160 lines): `surfaces: [spawn, persona]`, `provider:
  codex`, `gpt-5.6-sol`, `xhigh`, `rule_selectors` `tag:default`, `tag:worker-safety`,
  `tag:task-skill-gates`, and a `step_workflow` that is the discovery one-shot (`claim`
  with only `claim_task` and `get_task`, then `load_skill`, `draft`, `end_agent_run`).
  Consumers of the name that stay unchanged because they look the slug up and the
  research stage keeps the name: `src/gobby/install/shared/registry/stages.yaml` 16
  (`default_agent: researcher`), `src/gobby/dispatch/prompts.py` 125-130 and 366,
  `src/gobby/dispatch/_rule_actions.py` 37, `src/gobby/agents/sync.py`, the
  `research/SKILL.md` skill; stage behavior is not preserved (decision 19). Tests that
  load the definition and assert its step workflow are targets above; the fixture-only
  uses of the string elsewhere in `tests/` are not consumers.
  Coverage YAML is written by the daemon's `regenerate_coverage_manifest`
  (`src/gobby/mcp_proxy/tools/plans/__init__.py` 224-255; `coverage_manifest_path` in
  `src/gobby/plans/coverage_manifest.py` 57) and by `expansion-qa` (`expansion-qa.yaml`
  31), which is outside the rule's `agent_scope`; no runbook or council session writes
  it through Write.
- Schema `AgentDefinitionBody` (`src/gobby/workflows/agent_models.py` 78-219):
  `require_surface_prompt_blocks` demands one prompt block per declared surface;
  `reject_legacy_step_keys` rejects `role`/`goal`/`personality`/`instructions`; no
  field names a terminal backend, so criterion 1 is a prose check (no key `backend` or
  `terminal_backend` anywhere in a loaded tree, no word `tmux` or `gterm` in any string
  under `prompts` once backtick-quoted spans are removed, since the log-monitor family
  table quotes a `gterm` log signature; `native` is not checked because the shared tail says "native
  subagents"). Selectors (`src/gobby/workflows/selectors.py`): `tag:`, `group:` (the
  directory name set by sync), `name:`; exclude beats include. `default.yaml` includes
  `tag:default`.
- Precedents: `comms-agent.yaml` 39-56 (blocked_tools list); `researcher.yaml`
  (surfaces line 9, persona 22-23, rule_selectors 69-73).
- Sync: `sync_bundled_agents` (`src/gobby/agents/sync.py` 129-299) writes
  `source="installed"`, preserves the user's enabled toggle.
- Standing-instruction 4: the eight roles are on the chart or planned in #22691 and the
  roster is decision 13, so the definitions need disclosure, not approval; the rule
  group is 3.1's and its change is under Program Director approval.
- Rejected: `post-epic-reviewer` and `plan-verifier` (decision 13; the verifier's checks
  are the plan mechanic's, decision 18); persona-only definitions (`surfaces:
  [persona]`, R5): the launch is a spawn and requires `prompts.agent`, and the
  successor needs `prompts.persona`, so both surfaces with one anchored text; a
  standing text under `persona` and a shorter one under `agent` (two copies to keep in
  step, and the seat and its successor would differ); reusing `epic-reviewer`
  (build-lifecycle bound); keeping the researcher's discovery step workflow (a seat
  launched with no task would sit in `claim` forever); provider, model and effort in
  the definitions (decision 4 keeps them on the launch line, where Josh edits them);
  the restriction block on the launch line only (a runbook that forgets it launches an
  unrestricted seat; the definition carries it and the launch may narrow: position o).
- Deferred, not rejected: a `step_workflow` on the twelve definitions. They ship as
  persona prompts in v1 (decision 20: no new workflows) and adopt step workflows when
  decision 17's sibling plan lands; nothing in their definitions assumes persona-only,
  and 1.5.8 proves a stepful definition runs its program when launched into a pane.
- Planned checks: `GOBBY_TEST_PROTECT=1 uv run pytest tests/agents/test_runbook_definitions.py tests/agents/test_discovery_agents.py tests/workflows/test_workflows_agent_definitions.py`;
  after daemon restart, `gobby-workflows:list_agent_definitions` shows the seven new
  names and the rewritten `researcher`.

**Acceptance:**

- 3.2.1 - Each of the eight definitions parses, declares `surfaces: [spawn, persona]`
  with `prompts.agent` and `prompts.persona` equal, `isolation: none`, `provider`
  `inherit`, no `timeout` and no `step_workflow`, and carries exactly the selector
  lists, tool blocks and sandbox block of its block above (the read-only seats deny
  `.`, the archivist adds `~/Desktop`, the log-monitor reads `~/.gobby/logs`, the
  program-director is `enabled: false` with its reason, the assistant, lane-manager
  and elicitor carry no `sandbox` key; the write-only `blocked_tools` list has no
  shell spelling). file:
  `src/gobby/install/shared/workflows/agents/program-director.yaml`. file:
  `src/gobby/install/shared/workflows/agents/assistant.yaml`. file:
  `src/gobby/install/shared/workflows/agents/lane-manager.yaml`. file:
  `src/gobby/install/shared/workflows/agents/reviewer.yaml`. file:
  `src/gobby/install/shared/workflows/agents/archivist.yaml`. file:
  `src/gobby/install/shared/workflows/agents/log-monitor.yaml`. file:
  `src/gobby/install/shared/workflows/agents/elicitor.yaml`. file:
  `src/gobby/install/shared/workflows/agents/researcher.yaml`. test:
  `tests/agents/test_runbook_definitions.py::test_runbook_definitions_parse_with_both_surfaces_and_select_runbook_rules`.
- 3.2.2 - Every one of the eight texts embeds the five standing instructions, the
  durable shared tail and the conditional sign-off rule verbatim, in that order,
  followed by a `Diverges from the prompt book:` line; no routine nominal template
  carries `load <` or `runs <` (the log-monitor line); every definition excludes
  `block-autonomous-clear-session` and `bootstrap-default-agent-core-skills` by name.
  file: `src/gobby/install/shared/workflows/agents/researcher.yaml`. test:
  `tests/agents/test_runbook_definitions.py::test_every_runbook_text_carries_the_standing_text_and_the_shared_excludes`.
- 3.2.3 - No definition names a backend: no key `backend` or `terminal_backend` in any
  loaded tree and no `tmux` or `gterm` in any prompt string outside a backtick-quoted
  log signature (the log-monitor family table quotes the book's
  `terminals.host_manager._health_loop - gterm control probe failed` row, book line 495;
  a quoted signature selects nothing); every text's registration line names
  `GOBBY_PANE_REF` and the pane view's backend. test:
  `tests/agents/test_runbook_definitions.py::test_no_runbook_definition_names_a_backend`.
- 3.2.4 - The assistant text requires criterion 9's four items and Josh's three
  standing preferences (a Telegram send confirmed in at most one line and never
  restated; decisions asked with something to click and never nagged; plans sent as
  `send_attachment` files after the `git diff --stat` check) by their fixed phrases,
  and its creation line names the pane launch. file:
  `src/gobby/install/shared/workflows/agents/assistant.yaml`. test:
  `tests/agents/test_runbook_definitions.py::test_assistant_requires_the_comms_items_and_joshs_preferences`.
- 3.2.5 - The reviewer blocks `close_task`, `update_task` and `claim_task`, denies
  writes under the checkout, carries the verdict line format, and says a closed row is
  not a verdict. file: `src/gobby/install/shared/workflows/agents/reviewer.yaml`. test:
  `tests/agents/test_runbook_definitions.py::test_reviewer_is_verdict_only`.
- 3.2.6 - The lane-manager text carries the restated criterion 11 sentence verbatim
  (the sentence itself names the `terminal_backend` parameter and the headless spawn it
  forbids) and its loaded tree has no mapping key `backend` or `terminal_backend`. file:
  `src/gobby/install/shared/workflows/agents/lane-manager.yaml`. test:
  `tests/agents/test_runbook_definitions.py::test_lane_manager_never_launches_persistent_roles`.
- 3.2.7 - `researcher` has no `step_workflow`, selects the runbook group and
  `tag:worker-safety` and not `tag:task-skill-gates`, names the assistant as its
  reporting line, and the two named tests that asserted its discovery workflow no
  longer do. file: `src/gobby/install/shared/workflows/agents/researcher.yaml`. test:
  `tests/agents/test_runbook_definitions.py::test_researcher_is_the_chart_seat_without_a_step_workflow`.
- 3.2.8 - The agents guide documents the seats, the four council definitions, the
  reporting lines, and each role's sandbox block and continuity mode. behavior:
  "Runbook roles" in `docs/guides/agents.md`.
- 3.2.9 - Every shipped chart definition matches its block: for each role,
  `ROLE_BLOCKS` (surfaces, selector lists, `blocked_tools`, `blocked_mcp_tools`,
  `sandbox`, `skills`, `isolation`, the continuity mode phrase and the fixed phrases)
  equals what the loaded YAML carries and what `resolve_rules_for_agent` selects,
  including each program-director guard and reused rule of 3.1. file:
  `tests/agents/runbook_text.py`. test:
  `tests/agents/test_runbook_definitions.py::test_each_definition_matches_its_plan_block`.
- 3.2.10 - Live (V1 step 6; #22660): a researcher seat and a reviewer seat that each
  ran `set_handoff(clear_session=true)` after delivering their line come back in the
  same pane as the same role, with `get_workspace` showing the new `session_ref` and the
  same `sandbox`, their first turn carrying the definition's text once, and their next
  answer using the `EVENT=REPORT` and `EVENT=CANDIDATE_VERDICT` formats without being
  told them; a source write in the successor is still blocked. behavior: V1 step 6 in
  this plan, recorded in the archivist's digest.

### 3.3 Task amendment disclosure (#22713) [category: code] (depends: 1.1)
`kind: deliverable`

Targets:
- `src/gobby/storage/tasks/_artifacts.py::*` — scope-reason: `TaskArtifacts` gains the trigger-owned field and `from_row` reads it
- `src/gobby/mcp_proxy/tools/tasks/_artifacts.py::*` — scope-reason: `_artifact_payload` adds the read-only field beside its `_ARTIFACT_MUTATION_FIELDS` filter
- `src/gobby/install/shared/workflows/rules/task-enforcement/disclose-task-amendment.yaml`
- `src/gobby/install/shared/workflows/agents/task-close-reviewer.yaml::*` — scope-reason: the review workflow gains the amendment step and one allowed tool
- `docs/guides/tasks.md`
- `tests/mcp_proxy/tools/tasks/test_get_artifacts_claim_snapshot.py`
- `tests/workflows/test_disclose_task_amendment.py`
- `tests/agents/test_task_close_reviewer_amendment.py`

Ruling B1 on #22713: a claiming session may amend its own task's `description`,
`validation_criteria` and `labels`; the amendment requires pre-close review, disclosed
to the close reviewer with the diff and judged against the task's intent; enforcement
lives in the agent definitions. 1.1's trigger keeps the claim-time values in
`task_artifacts.claim_snapshot`; this leaf surfaces them and closes the loop.
- Storage: `TaskArtifacts` gains `claim_snapshot: str | None = None`, read by
  `from_row`. It stays out of `_ARTIFACT_FIELDS`, so `set_artifact` and
  `set_artifacts_atomic` refuse it through `_validate_field_names` and only the trigger
  writes it; `get_artifacts` returns the JSON text unchanged.
- MCP: `gobby-tasks-artifacts-ops:get_artifacts` returns `claim_snapshot` (the JSON
  text, or None) because `_artifact_payload` (`mcp_proxy/tools/tasks/_artifacts.py`
  47-51) adds it beside its `_ARTIFACT_MUTATION_FIELDS` filter; the mutation set is
  unchanged, so `set_artifact` keeps refusing the field at both layers. `get_task` is
  not touched: `_crud.py` sits at 996 lines.
- Rule `task-enforcement/disclose-task-amendment.yaml` (tags `[task-enforcement,
  enforcement, tasks, gobby, default]` like its siblings): `event: after_tool`,
  `priority: 30`, `when` copied from `block-reopen-task.yaml` 10-19 for `gobby-tasks`
  `update_task` with the `claimed_tasks` match on the UUID key, the `#N` value or the
  bare number, and any of `description`, `validation_criteria` or `labels` present in
  `tool_input`; one `inject_context` effect whose template is ruling B1's standing
  order: the amendment is reviewed before close; name it and why in the close summary;
  keep the task's intent. A path-form `task_id` is not matched (the variable holds
  UUIDs and `#N`); the reviewer's diff below catches that case regardless.
- Reviewer: `task-close-reviewer.yaml` leaves the Non-goals for one allowed tool,
  `gobby-tasks-artifacts-ops:get_artifacts`, and one step in `prompts.agent` after the
  `get_task` read: call `get_artifacts(task_id)`; when it carries `claim_snapshot`,
  diff its `description`, `validation_criteria` and `labels` against
  the current values (`get_task_diff` is the code diff and does not help here); a
  non-empty diff is an amendment. The reviewer judges it against the intent the
  snapshot captured and passes it only when the closure summary names it; an unnamed
  or loosening amendment is a blocking finding in `submit_close_review`, so the close
  bounces. The blocked list is unchanged; `get_artifacts` is read-only. The persistent
  `reviewer` persona (3.2) reads the same field.
- Docs: `docs/guides/tasks.md` gains "Amending a claimed task": what the snapshot
  holds, when it is taken, how the disclosure order arrives, what the close reviewer
  does with it.
The `needs-decision` label on #22713 is the documented gate workaround (defect #22726);
this leaf is the follow-up implementation task its criterion names, with these tests.

Research context:
- Lookup L3 (gobby#14332, message `1e77d597`, 2026-09-23): no durable surface records
  task-field edits; `task_lifecycle_events` (baseline.sql 3742-3752) holds state
  transitions with a text reason; `task_validation_history` (3845-3858) holds run
  outcomes; the close reviewer's allowed tools are `get_task`, `get_task_diff`,
  `list_tasks`, `submit_close_review` and read-only memory and rule lookups
  (`task-close-reviewer.yaml` 238-246), and `get_task(brief=false)` returns no
  artifacts today.
- Rule engine: `tool_input.server_name`/`tool_name` plus the promoted nested args
  (`src/gobby/workflows/engine/enforcement_checks.py` 930-940); `claimed_tasks` is
  `{uuid: '#N'}` written by the `detect_task_claim` observer; effects available are
  set_variable, inject_context, observe, mcp_call, rewrite_input, load_skill,
  run_command and block (no warn), so the order is an `inject_context`; `after_tool`
  precedents `reviewer-lifecycle/terminal-verdict-after-validation.yaml` 33 and
  `error-recovery/inject-tool-error-recovery.yaml` 6; `inject_context` template
  precedent `task-enforcement/track-task-claim.yaml` 38-42.
- `_artifacts.py`: `_ARTIFACT_FIELDS` 13-32, `TaskArtifacts.from_row` 91-111,
  `_validate_field_names` 114-119, `TaskArtifactManager.get_artifacts` 274-278;
  `clear_artifacts` has no caller outside the package export, so the snapshot lives as
  long as the task row. MCP precedent `gobby-tasks-artifacts-ops:get_artifacts`
  (`src/gobby/mcp_proxy/tools/tasks/_artifacts.py` 298). `get_task` (`_crud.py`
  489-525) builds the full record from `task.to_dict()` plus dependencies.
- Rejected: a session-variable carrier (dies with the session; the reviewer never sees
  it); a `block` on the three fields (Josh rejected option a); a `task_amendments`
  table (the artifact row already is the per-task 1:1 carrier); Python writes in the
  four claim paths (the trigger covers all four with no call site); extending
  `get_task(brief=false)` (`_crud.py` is at 996 lines and `get_artifacts` already
  serves the artifact row).
- Planned checks: the three test files with the isolated hub DSN; `uv run mypy src/`;
  `uv run gobby workflows` dry-run lint if available.

**Acceptance:**

- 3.3.1 - `TaskArtifacts` carries `claim_snapshot` read from the row and refuses it as a
  writable field; `get_artifacts` returns it and `set_artifact` refuses it. symbol:
  `_artifact_payload`. file: `src/gobby/storage/tasks/_artifacts.py`.
  test: `tests/mcp_proxy/tools/tasks/test_get_artifacts_claim_snapshot.py::test_get_artifacts_carries_claim_snapshot`.
  test: `tests/mcp_proxy/tools/tasks/test_get_artifacts_claim_snapshot.py::test_set_artifact_refuses_claim_snapshot`.
- 3.3.2 - After a successful `update_task` that touches any of the three fields on a
  claimed task, matched by UUID, `#N` or bare number, the disclosure order is
  injected; other fields, other tasks and other tools stay silent. file:
  `src/gobby/install/shared/workflows/rules/task-enforcement/disclose-task-amendment.yaml`.
  test: `tests/workflows/test_disclose_task_amendment.py::test_amendment_on_claimed_task_injects_disclosure_order`.
  test: `tests/workflows/test_disclose_task_amendment.py::test_other_fields_tasks_and_tools_stay_silent`.
- 3.3.3 - The close reviewer's agent prompt names the snapshot diff step and the bounce
  condition; its allowed list gains only `gobby-tasks-artifacts-ops:get_artifacts` and
  its blocked list is unchanged. file:
  `src/gobby/install/shared/workflows/agents/task-close-reviewer.yaml`. test:
  `tests/agents/test_task_close_reviewer_amendment.py::test_close_reviewer_checks_claim_snapshot_and_allows_get_artifacts`.
- 3.3.4 - The tasks guide documents amending a claimed task. behavior: "Amending a
  claimed task" in `docs/guides/tasks.md`.

### 3.4 Council definitions: plan-writer, plan-enhancer, plan-adversary, plan-mechanic, and the retired planning-stage names [category: code] (depends: 3.1, 3.2, 2.1)
`kind: deliverable`

Targets:
- `src/gobby/install/shared/workflows/agents/plan-writer.yaml`
- `src/gobby/install/shared/workflows/agents/plan-mechanic.yaml`
- `src/gobby/install/shared/workflows/agents/plan-enhancer.yaml::*` — scope-reason: rewritten in full as the council enhancer; the stage-native step workflow goes (decision 19)
- `src/gobby/install/shared/workflows/agents/plan-adversary.yaml::*` — scope-reason: rewritten in full as the council adversary; the stage-native step workflow goes (decision 19)
- `src/gobby/install/shared/workflows/agents/planner.yaml::*` — scope-reason: `git mv` to `plan-writer.yaml`, then rewritten (decision 19)
- `src/gobby/install/shared/workflows/agents/plan-enhancer-taskless.yaml::*` — scope-reason: deleted (decision 18)
- `src/gobby/install/shared/workflows/agents/plan-adversary-taskless.yaml::*` — scope-reason: deleted (decision 18)
- `src/gobby/install/shared/registry/stages.yaml::*` — scope-reason: line 37 `default_agent: planner` becomes `plan-writer` (line 38 `reviewer_agent: plan-adversary` stays)
- `src/gobby/dispatch/_rule_actions.py::_STAGE_AGENT_SLUGS`
- `src/gobby/dispatch/prompts.py::PROMPT_BUILDERS`
- `src/gobby/dispatch/spawn_artifacts.py::_TASKLESS_MAIN_CONTEXT_AGENT_SLUGS`
- `src/gobby/tasks/expansion/_plan_gate.py::PLANNING_AGENTS`
- `src/gobby/tasks/expansion/_plan_gate.py::PLAN_REPAIR_AGENTS`
- `src/gobby/mcp_proxy/tools/spawn_agent/_code_index.py::_PLANNING_CODE_INDEX_AGENTS`
- `src/gobby/install/shared/workflows/rules/review-learning/inject-planner-lessons.yaml::*` — scope-reason: `agent_scope` (line 10) names `plan-writer`
- `src/gobby/install/shared/workflows/rules/review-learning/inject-plan-enhancer-lessons.yaml::*` — scope-reason: `agent_scope` (line 10) drops the retired name
- `src/gobby/install/shared/workflows/rules/review-learning/inject-plan-reviewer-lessons.yaml::*` — scope-reason: `agent_scope` (line 10) drops the retired name
- `src/gobby/install/shared/workflows/rules/memory-lifecycle/guard-plan-memory-writes.yaml::*` — scope-reason: its agent list (lines 13-19) names `plan-writer` and drops the two retired names
- `src/gobby/install/shared/workflows/rules/plan-mode/reset-plan-mode-on-session-start.yaml::*` — scope-reason: the `_agent_type != 'planner'` guard (line 11) names `plan-writer`
- `src/gobby/install/shared/skills/gobby/references/plan/enhancement.md`
- `src/gobby/install/shared/skills/gobby/references/plan/review.md`
- `docs/contracts/plan-coverage.md::*` — scope-reason: the taskless-reviewer sentences name the council adversary
- `docs/guides/plans-and-plan-mode.md::*` — scope-reason: the three-roles paragraph (line 34) and the interactive enhancement and review paragraphs name the council
- `tests/agents/test_council_definitions.py`
- `tests/agents/test_plan_adversary_taskless_definition.py::*` — scope-reason: deleted with its definition
- `tests/agents/test_plan_enhancer_agents.py::*` — scope-reason: the stage-native path (29) reads the council body; the taskless half is dropped
- `tests/agents/test_plan_adversary_internal_research_definition.py::*` — scope-reason: the name tuples (16-17, 37, 191) drop the retired names
- `tests/agents/test_plan_adversary_loads_plan_review.py::*` — scope-reason: loads `plan-adversary.yaml`; adapts to the council body
- `tests/agents/test_plan_adversary_no_edits_on_reject.py::*` — scope-reason: line 49 asserts "planner" in the text; adapts to the council body
- `tests/agents/test_plan_adversary_self_check.py::*` — scope-reason: loads `plan-adversary.yaml`; adapts to the council body
- `tests/agents/test_plan_adversary_manifest.py::*` — scope-reason: loads `plan-adversary.yaml`; adapts to the council body
- `tests/agents/test_planner_loads_plan_draft.py::*` — scope-reason: asserts the step names `claim`, `load_skill`, `plan`, `terminate` (42-97); rewritten to assert `plan-writer`'s text loads `drafting.md`
- `tests/agents/test_discovery_agents.py::*` — scope-reason: the `plan-adversary` selectors (211-212) and the `planner` prompt assertions (219-225) follow the council bodies
- `tests/agents/test_agents_sync.py::*` — scope-reason: line 86 and 894-895 name the retired definitions
- `tests/agents/watchdog/test_completed_turn_mcp_gate.py::*` — scope-reason: the fixture (42, 47) names `plan-adversary` instead
- `tests/dispatch/test_dispatch_prompts.py::*` — scope-reason: the names (37-41), the alias rows and assertions (51, 75-77) and the `planner` key (125) follow the rename
- `tests/dispatch/test_spawn_isolation.py::*` — scope-reason: `planner` (23, 56) becomes `plan-writer`; the taskless cases (70, 84, 103) go with the branch
- `tests/dispatch/test_rules.py::*` — scope-reason: the stage map rows (17, 323) name `plan-writer`
- `tests/dispatch/test_rules_stage_native.py::*` — scope-reason: the fixtures (36, 70, 119, 122) name `plan-writer`
- `tests/dispatch/test_planning_enhancement.py::*` — scope-reason: the fixture registries (32, 65) name `plan-writer`
- `tests/dispatch/test_dispatcher.py::*` — scope-reason: the parametrize (2234) names `plan-writer`
- `tests/build_pipeline/test_build_pipeline_service.py::*` — scope-reason: `agent_lookup_name == "planner"` (865) becomes `plan-writer`
- `tests/build_pipeline/test_build_pipeline_cascade.py::*` — scope-reason: `assigned_agent="planner"` (126) becomes `plan-writer`
- `tests/tasks/test_plan_gate.py::*` — scope-reason: the frozenset assertion (146) and the planner names (175-473) follow the rename
- `tests/e2e/test_build_dispatcher_autonomy.py::*` — scope-reason: `complete_agent("planner")` (490, 533) and `resolve_agent("planner")` (917, 958) name `plan-writer`; the harness fakes completion, so a definition without a step workflow still resolves
- `tests/mcp_proxy/tools/spawn_agent/test_error_handling.py::*` — scope-reason: the stage pairs (58, 1006, 1113) name `plan-writer`
- `tests/workflows/test_retired_bundled_definitions.py::*` — scope-reason: `RETIRED_AGENTS` (50-56) gains `planner`, `plan-enhancer-taskless` and `plan-adversary-taskless`; `test_planner_enables_surviving_plan_mode_write_guard` (164-174) loads `plan-writer.yaml`
- `tests/workflows/test_step_enforcement.py::*` — scope-reason: the fixture (631, 643) names `plan-adversary` instead
- `tests/workflows/test_planner_grammar_prompt.py::*` — scope-reason: the path (13) and the table (63-67, taskless rows 65 and 67) follow the rename and the retirement
- `tests/workflows/test_plan_mode_rules.py::*` — scope-reason: the parametrize (258) names `plan-writer`
- `tests/workflows/test_review_learning_rules.py::*` — scope-reason: the expected scope lists (359, 363, 365) follow the three rules
- `tests/workflows/test_workflows_agent_definitions.py::*` — scope-reason: the provider table (141-158) drops the retired rows and asserts `inherit` for the four council rows; the `planner` agent-prompt test (604-608) goes
- `tests/workflows/test_memory_lifecycle_rules.py::*` — scope-reason: the agent lists (797-801, 851) follow the guard rule
- `tests/skills/test_plan_skill_grammar.py::*` — scope-reason: lines 22-23 and 93-94 read the writer's text by its new name
- `tests/skills/test_plan_adversary_rejection.py::*` — scope-reason: line 17 names the council adversary
- `tests/skills/test_plan_review_skill.py::*` — scope-reason: the parametrize (82) keeps `plan-adversary` only
- `tests/skills/test_plan_skill_delegated_mode.py::*` — scope-reason: names the retired definition
- `tests/skills/test_review_learning_skill.py::*` — scope-reason: line 286 loads `plan-writer.yaml`
- `tests/mcp_proxy/test_stage_review_schema.py::*` — scope-reason: line 11 loads `plan-adversary.yaml`; the taskless name goes

Decision 18's roster, shaped by decision 19: four council definitions with the same
ten-field blocks as 3.2 and the same shared values (both surfaces with one anchored
text; `isolation: none`; provider, model and effort on the launch line; `skills: []`;
no worktree; no `step_workflow`; the two shared excludes; continuity per decision 21),
each carrying 3.2's standing text (criterion 8), the registration line (criterion 3),
the restated creation line (criterion 11) and the council sentence (wait between rounds
rather than looking for work; the assistant releases the pane). `planner.yaml` is
renamed to `plan-writer.yaml` (`git mv`, so history follows) and rewritten;
`plan-enhancer.yaml` and `plan-adversary.yaml` are rewritten as council definitions
with their stage-native step workflows removed; `plan-mechanic.yaml` is new; the two
taskless definitions are deleted; every consumer of a retired name is repointed
(decision 19; the old bodies stay in git history).
- `plan-writer` (from `planner.yaml`; book section 9, lines 577-618). (1) Launch:
  `claude`, `claude-fable-5-1[1m]`, no effort flag. (2) Persona: owns the plan file
  and its hash and is the only member that edits it; claims a real planning task first;
  reads `docs/contracts/plan-coverage.md` and `references/plan/drafting.md` before
  drafting; drafts against the approved problem statement, or against the Decision
  Record when the Program Director waives the elicitor, and says when a statement does
  not cover something; `uv run gobby plans validate` passes before review opens;
  commits by explicit path only, the plan and the problem statement together; answers
  every finding with Fold (say what changed) or Contest (reasoning; "out of scope" only
  by pointing at a non-goal); a signed design artifact outranks the adversary; records
  every position and move in the council log (the round file beside the plan); hands
  each published hash to the mechanic and reads its pass lines before sending the hash
  on. Divergence: no round cap; the council debates to consensus and the adversary
  finalizes to the Program Director, who may send it back (decision 13, #22808). (3)
  Can do: the Write and Edit tools for plan markdown under the plans directory only
  (3.1's carve-out),
  `git commit --only` by path, `gobby-plans` reads and `update_plan_hash`,
  `gobby-tasks` claim and close of its own planning task; `blocked_mcp_tools`:
  `planner`'s existing list kept, plus `gobby-agents:spawn_agent` and
  `gobby-agents:kill_agent`; code-edit right: none. (4) Sandbox: default (the plan and
  the commit are under the checkout write root; 3.1 narrows the rest). (5) Rules:
  `planner`'s existing `rule_selectors` kept, plus include `"group:runbook"` and
  `"tag:worker-safety"` and the two shared excludes; `workflows.variables.plan_mode:
  true` kept, so the plan-mode rules apply and
  `reset-plan-mode-on-session-start` (repointed) spares it; scoped:
  `runbook-no-code-edits` with the `.gobby/plans/` carve-out, `inject-planner-lessons`
  and `guard-plan-memory-writes` (both repointed to the new name). (6) Loads
  `docs/contracts/plan-coverage.md`, `gobby:references/plan/drafting.md` and
  `references/plan/overview.md` before editing a plan, and the `code-review` skill
  before each commit. (7) Worktree: none. (8) Messaging: publishes each hash to the
  mechanic, the enhancer and the adversary by `session_ref` from `get_workspace`
  (wake for a hash they must act on); reports to the Program Director with a no-wake
  summary; sends Josh nothing. (10) Continuity: clears between plans, never mid-round
  (state: the plan path, the last hash and the open findings).
- `plan-enhancer` (rewritten; book section 10, lines 619-644). (1) Launch: `codex`,
  `gpt-5.6-sol`, `xhigh`. (2) Persona: reviews only the hash it was given and stops if
  the file differs; numbered `cr-N` items, each stating change, benefit and cost,
  ranked; looks for a cheaper mechanism, a missing edge state, a sequencing change or
  an existing pattern being reinvented, never style, non-goal scope or a contradiction
  of a signed source. Divergence: none beyond the council sentence. (3) Can do: read
  and search tools, `gcode`, `git show`; `blocked_tools` is 3.2's write-tool list (the
  existing `Edit`/`Write` block widened to every spelling); `blocked_mcp_tools`: the
  existing list kept, plus `gobby-agents:spawn_agent`, `gobby-agents:kill_agent`,
  `gobby-plans:apply_plan_review_repairs`, `gobby-plans:record_plan_enhancement`;
  code-edit right: none. (4) Sandbox: `sandbox: {extra_deny_write_paths: ["."]}`.
  (5) Rules: include `["tag:default", "group:runbook", "tag:worker-safety"]`, exclude
  the two shared names plus `["name:require-python-skill", "name:require-rust-skill"]`;
  scoped: `runbook-no-code-edits`, `inject-plan-enhancer-lessons`,
  `guard-plan-memory-writes`. (6) Loads `gobby:references/plan/enhancement.md` before
  its first round. (7) Worktree: none. (8) Messaging: `cr-N` items to the writer by
  `send_message` into the council log; sends Josh nothing. (10) Continuity: clears
  between plans, never mid-round.
- `plan-adversary` (rewritten; book section 11, lines 645-681). (1) Launch: `grok`,
  `grok-4.7`, `xhigh`. (2) Persona: attacks only the hash it was given; every finding
  carries `check_keys`; a blocking defect is a step that cannot work, a missing
  dependency, a vacuous criterion, a contract violation, an unhandled expensive failure
  mode or work a lane cannot do, never a preference or an excluded scope; a signed
  design artifact outranks it (the chrome Edit-menu case is quoted); it withdraws or
  holds each contested finding; it may use the mechanic's evidence but keeps its own
  code spot-checks (decision 18); and it finalizes to the Program Director citing the
  mechanic's pass lines for the final hash (validate, `git diff --check`, sha256,
  coverage) instead of running those checks itself. Divergence: no round cap and the
  finalization report (decisions 13 and 18). (3) Can do: read and search tools,
  `gcode`, `git show`; `blocked_tools` is the write-tool list; `blocked_mcp_tools`: the
  existing list kept, plus `gobby-agents:spawn_agent`, `gobby-agents:kill_agent` and
  the stage review verbs it no longer records through (`gobby-tasks-ops:approve_review`,
  `reject_review`); code-edit right: none. (4) Sandbox: `sandbox:
  {extra_deny_write_paths: ["."]}`. (5) Rules: include `["tag:default",
  "group:runbook", "tag:worker-safety"]`, exclude the two shared names plus
  `["name:require-python-skill", "name:require-rust-skill", "tag:task-skill-gates"]`
  (the existing exclusion stays: the council adversary claims no task); scoped:
  `runbook-no-code-edits`, `inject-plan-reviewer-lessons`, `guard-plan-memory-writes`.
  (6) Loads `gobby:references/plan/review.md` before its first round. (7) Worktree:
  none. (8) Messaging: findings to the writer into the council log; the finalization
  report to the Program Director (wake); sends Josh nothing. (10) Continuity: clears
  between plans, never mid-round.
- `plan-mechanic` (new; the book has no section for it, so the persona is written
  here). (1) Launch: `claude`, `claude-opus-5[1m]`, no effort flag (the lookup pane's
  line, decision 18). (2) Persona: the council's mechanical checker and its fourth
  pane. On every hash the writer publishes, and on request from any member: `uv run
  --no-sync gobby plans validate <plan>` (both modes when a manifest is present;
  `--no-sync` because its sandbox denies writes under the checkout and a sync would
  write `.venv`), `git diff --check`, the plan's sha256 against the committed blob
  (`git show <commit>:<path> | sha256sum`), the coverage manifest of a registered plan
  (`gobby-plans:regenerate_coverage_manifest`, or `update_plan_hash` when the row's
  hash is stale; memory 5538e963) with the coverage-contract checks of
  `docs/contracts/plan-coverage.md`, and re-verification of citations and literals
  (paths, symbols, line numbers, migration numbers, commit shas) against HEAD with
  `gcode`. It reports `CHECK=<name> RESULT=PASS|FAIL HASH=<sha256> NOTE=` lines to the
  requester and, for a final hash, to the adversary and the writer. Validator residue
  is reported, never repaired: the writer is the only editor. Semi-persistent: released
  by the assistant with the council. To-be, not built here: decision 18's shared
  evidence pack. (3) Can do: read and search tools, `gcode`, `git` reads, the
  `gobby-plans` tools named above; `blocked_tools` is the write-tool list;
  `blocked_mcp_tools: ["gobby-plans:apply_plan_review_repairs",
  "gobby-plans:apply_plan_handoff_manifest", "gobby-agents:spawn_agent",
  "gobby-agents:kill_agent"]`; code-edit right: none. (4) Sandbox: `sandbox:
  {extra_deny_write_paths: ["."]}` (the manifest is written by the daemon through the
  tool, not by the seat). (5) Rules: include `["tag:default", "group:runbook",
  "tag:worker-safety"]`, exclude the two shared names plus
  `["name:require-python-skill", "name:require-rust-skill"]`; scoped:
  `runbook-no-code-edits`. (6) Loads `gobby:references/plan/repair.md` (the
  `plan-mechanic` skill name resolves to it, `catalog.json` line 1041) and
  `references/plan/coverage.md` before its first check. (7) Worktree: none. (8)
  Messaging: `CHECK=` lines to the requester; final-hash lines to the adversary and the
  writer; sends Josh nothing. (10) Continuity: clears after each final-hash report
  (state: the plan path and the hash just checked).
- Retirement (decision 19). `planner.yaml` moves to `plan-writer.yaml`;
  `plan-enhancer-taskless.yaml` and `plan-adversary-taskless.yaml` are deleted;
  `sync_bundled_agents`'s orphan sweep soft-deletes the three registry rows at the next
  start (`src/gobby/agents/sync.py` 5-6 and 135-136). Consumers repointed to
  `plan-writer` the cheapest way that keeps sync, validation and the named tests
  passing: `stages.yaml` line 37; `_STAGE_AGENT_SLUGS` (`_rule_actions.py` 40);
  `PROMPT_BUILDERS` (`prompts.py` 360 key and the `_planner` builder's contract string
  at 76; the alias keys 356 and 358 go); `PLANNING_AGENTS` and `PLAN_REPAIR_AGENTS`
  (`_plan_gate.py` 30-31); `_PLANNING_CODE_INDEX_AGENTS` (`_code_index.py` 7); the
  `inject-planner-lessons` scope, the `guard-plan-memory-writes` list and the
  `reset-plan-mode-on-session-start` guard. `_TASKLESS_MAIN_CONTEXT_AGENT_SLUGS`
  (`spawn_artifacts.py` 43-45) and its branch (508-509) go, because no slug is left in
  the set; the other two review-learning rules drop the taskless names. Rule-template
  changes are disclosed to the Program Director under standing instruction 4.
  `enhancement.md` line 6 and `review.md` line 6 stop spawning a taskless agent: an
  interactive round runs in the council tab (`plan-council-v1.sh <plan>`, decision 14),
  opens when the writer publishes a hash to the enhancer or adversary pane, and returns
  by `send_message` into the council log; `prepare_plan_review_round` and the
  review-evidence store stay for the tool surface. The two docs say the same.
  `RETIRED_AGENTS` in `tests/workflows/test_retired_bundled_definitions.py` gains
  `planner`, `plan-enhancer-taskless` and `plan-adversary-taskless`, so the existing
  parametrized tests prove the files are gone and the sync soft-deletes their rows.
  Historical mentions in `docs/research/`, `docs/plans/`, `docs/evidence/`,
  `.gobby/plans/completed/`, the `tests/adapters/test_claude_code_adapter.py` comment
  (line 1058) and the `crates/gclient/tests/parity/sidebar.rs` sample name (823-830)
  stay; the word "planner" as a role noun in prose stays wherever it is not the slug.
- Tests: `tests/agents/test_council_definitions.py` loads the four YAMLs through
  `AgentDefinitionBody`, imports 3.2's `runbook_text` constants and `ROLE_BLOCKS`, and
  asserts surfaces, the anchored text, selectors, tool blocks, sandbox blocks, the
  standing text, the fixed lines, that no definition names a backend (3.2.3's scan)
  and that `resolve_rules_for_agent` includes the runbook rules; the listed existing
  tests lose their taskless rows and cases and follow the rename.

**Granularity:** more than six production Target files: the council definitions, their stage registry rows and the retired names are one rename that the stage registry validates as a set.

Research context:
- Names on HEAD (2026-09-24, `f7b9ccc54c`): `planner.yaml` (257 lines; `surfaces:
  [spawn, persona]`, `provider: codex`, `gpt-5.6-sol`, `blocked_mcp_tools` 14,
  `step_workflow` 133, `rule_selectors` ~127, `workflows.variables.plan_mode: true`),
  `plan-enhancer.yaml` (281; `claim` and `terminate` steps; `record_plan_enhancement`;
  `blocked_tools` `Edit`/`Write` 18-20; `blocked_mcp_tools` 21) and
  `plan-adversary.yaml` (464; `claim` and `terminate`; the stage review verbs;
  `blocked_mcp_tools` 15; `rule_selectors` excluding `tag:task-skill-gates`) are gobby
  build's planning-stage definitions; `plan-enhancer-taskless.yaml` (208) and
  `plan-adversary-taskless.yaml` (296) were the `/gobby plan` spawn variants
  (`send_message` then `end_agent_run`; `tests/agents/test_plan_enhancer_agents.py` 1-14
  states the split). `plan-writer` and `plan-mechanic` exist nowhere; `plan-verifier`
  never was a definition. `AgentDefinitionBody` ties no `step_workflow` to a surface,
  so removing the step workflows changes nothing in the schema.
- Live consumers of `planner` (all repointed above; none outside `gobby build` needs
  the old name): `stages.yaml` 37, `_rule_actions.py` 40, `prompts.py` 71-76 and 360,
  `_plan_gate.py` 30-31, `_code_index.py` 7, the three rules; `_rule_state.py` 48-49
  (`_agent_dispatchable` is `_has_agent(context, slug) and slug in PROMPT_BUILDERS`)
  and `rules.py` look the slug up and need no edit. `web/` has no hit.
- Spawn-surface reach of `group:runbook` on the council definitions
  (`_filter_by_agent_scope`, `src/gobby/workflows/engine/core.py` 838-855, matches
  `_agent_type` on any surface): every council seat is a launched (spawned) session and
  its `/clear` successor an interactive one; the group blocks source writes in both,
  the deny root blocks every checkout write for the three read-only seats, and the
  `.gobby/plans/` carve-out names `plan-writer`.
- Rejected: keeping the stage-native bodies and adding council persona blocks beside
  them (R5; a Non-goal 1 workaround, decision 19); a taskless mode on `plan-enhancer`
  and `plan-adversary` (a second transition out of `claim`; same reason); leaving
  `planner` in place and adding `plan-writer` beside it (two drafting definitions with
  one text; R5 position b redone: nothing outside `gobby build` needs the name);
  re-running the discovery or planning stage under the new names (decision 19:
  preserving `gobby build`'s stage behavior is not required).
- Planned checks: `GOBBY_TEST_PROTECT=1 uv run pytest tests/agents/test_council_definitions.py tests/workflows/test_retired_bundled_definitions.py tests/agents/test_plan_enhancer_agents.py tests/dispatch/test_dispatch_prompts.py tests/dispatch/test_spawn_isolation.py tests/tasks/test_plan_gate.py tests/dispatch/test_rules.py`
  and the remaining listed test files one by one; a repository grep for `planner` as a
  slug and for the two taskless names over `src/gobby/install/shared`,
  `src/gobby/dispatch`, `src/gobby/tasks/expansion`,
  `src/gobby/mcp_proxy/tools/spawn_agent` and the listed tests is empty; after
  restart, `gobby-workflows:list_agent_definitions` shows `plan-writer` and
  `plan-mechanic` and neither `planner` nor a taskless name.

Consumers unchanged:
- `src/gobby/dispatch/_planning_enhancement.py` — no-edit-reason: `PLAN_ENHANCER_AGENT = "plan-enhancer"` (line 26) names a surviving slug.
- `src/gobby/dispatch/spawn.py` — no-edit-reason: its `!= "plan-adversary"` check (96) names a surviving slug.
- `src/gobby/dispatch/_rule_state.py` — no-edit-reason: `_agent_dispatchable` looks the slug up in `PROMPT_BUILDERS`; the renamed key is found the same way.
- `src/gobby/dispatch/rules.py` — no-edit-reason: same lookup by live slug; the mapping's signature is unchanged.
- `src/gobby/hooks/event_handlers/_session_end.py` — no-edit-reason: line 160 is a comment naming `plan-adversary`, which survives.
- `tests/storage/test_stage_registry_loader.py` — no-edit-reason: it asserts `reviewer_agent: plan-adversary` (52, 264), which stays true.

**Acceptance:**

- 3.4.1 - The four council definitions parse, declare `surfaces: [spawn, persona]` with
  `prompts.agent` and `prompts.persona` equal, `provider` `inherit`, no `step_workflow`
  and no `timeout`, select `group:runbook` and `tag:worker-safety`, carry the standing
  text, the selector lists, tool blocks and sandbox blocks written above (the three
  read-only seats deny `.`; the writer has no `sandbox` key), and none names a
  backend; `plan-writer.yaml` is the renamed `planner.yaml` (`git log --follow` shows
  the move) and no `planner.yaml` exists. file:
  `src/gobby/install/shared/workflows/agents/plan-writer.yaml`. file:
  `src/gobby/install/shared/workflows/agents/plan-mechanic.yaml`. file:
  `src/gobby/install/shared/workflows/agents/plan-enhancer.yaml`. file:
  `src/gobby/install/shared/workflows/agents/plan-adversary.yaml`. test:
  `tests/agents/test_council_definitions.py::test_council_definitions_parse_with_both_surfaces_and_select_runbook_rules`.
- 3.4.2 - The mechanic text names its checks by their fixed phrases (`uv run --no-sync
  gobby plans validate`, `git diff --check`, `sha256`, `regenerate_coverage_manifest`,
  `against HEAD`) and the `CHECK=` report line, says residue is reported and never
  repaired, and its loaded tree blocks `gobby-plans:apply_plan_review_repairs`. file:
  `src/gobby/install/shared/workflows/agents/plan-mechanic.yaml`. test:
  `tests/agents/test_council_definitions.py::test_mechanic_owns_the_mechanical_checks_and_never_repairs`.
- 3.4.3 - The adversary text's finalization sentence cites the mechanic's pass lines
  and its spot-check sentence keeps independent code checks (fixed phrases). file:
  `src/gobby/install/shared/workflows/agents/plan-adversary.yaml`. test:
  `tests/agents/test_council_definitions.py::test_adversary_finalizes_on_the_mechanics_pass_lines`.
- 3.4.4 - Neither taskless file nor `planner.yaml` exists; `RETIRED_AGENTS` names all
  three and the existing parametrized tests pass for them; no bundled rule, registry
  row template, dispatch map, expansion gate, code-index list, skill reference or
  definition-loading test under `src/gobby/install/shared`, `src/gobby/dispatch`,
  `src/gobby/tasks/expansion`, `src/gobby/mcp_proxy/tools/spawn_agent` and the
  listed tests names `planner`, `plan-enhancer-taskless` or `plan-adversary-taskless`
  as a slug; `stages.yaml`, `_STAGE_AGENT_SLUGS`, `PROMPT_BUILDERS`,
  `PLANNING_AGENTS`, `PLAN_REPAIR_AGENTS` and `_PLANNING_CODE_INDEX_AGENTS` name
  `plan-writer`; and the two planning references describe the council round
  (`plan-council-v1.sh`) with no spawn of a taskless agent. file:
  `src/gobby/install/shared/skills/gobby/references/plan/enhancement.md`. file:
  `src/gobby/install/shared/skills/gobby/references/plan/review.md`. file:
  `src/gobby/install/shared/registry/stages.yaml`. test:
  `tests/agents/test_council_definitions.py::test_retired_planning_names_are_gone_everywhere`.
  test: `tests/workflows/test_retired_bundled_definitions.py::test_retired_agent_yaml_is_absent`.
- 3.4.5 - Every shipped council definition matches its block: for each of the four,
  `ROLE_BLOCKS` (surfaces, selector lists, `blocked_tools`, `blocked_mcp_tools`,
  `sandbox`, `skills`, `isolation`, the continuity mode phrase and the fixed phrases)
  equals what the loaded YAML carries and what `resolve_rules_for_agent` selects
  (the table is 3.2's `runbook_text` module). test:
  `tests/agents/test_council_definitions.py::test_each_definition_matches_its_plan_block`.

## V1 Verification
`kind: verification`

1. Unit and contract suites per deliverable (Constraints lists the commands): 1.5's four
   new test files and the extended ones of 1.5 and 1.6, 3.1's rule tests, 3.2's and 3.4's definition
   tests; the gcore schema tests with `--features postgres` and a scratch database
   (`GOBBY_SCHEMA_TEST_DATABASE_URL`; without the feature they compile to nothing) and
   `cargo test -p gobby-core --lib grant::tests` without `--features postgres` (the
   no-postgres identity guard, memory 24090e86);
   `DATABASE_URL=postgresql://gobby_test:gobby_test@127.0.0.1:60892/gobby_test GOBBY_TEST_PROTECT=1 uv run pytest tests/runtime_grants/test_golden_vectors.py`
   (the five re-signed goldens verify against `GOLDEN_SECRET`, 1.1.6);
   `cargo nextest run -p gobby-client` including `source_size.rs` and the `launch` and
   script tests; ruff and mypy on `src/`; `wc -l` on the size-guard files.
2. Cutover from the main checkout after a `global` announcement: commit 1.1, then
   `uv run gobby cutover` (proves `gdaemon schema plan`, promotes the binary set, applies
   migration 451, restarts the daemon that serves the launch; that start's bundled agent
   sync re-writes or sweeps every `gobby`-tagged definition row, which is what makes
   1.5's sync-managed marker sound). Rebuild gclient
   (`cargo build --release -p gobby-client`) and promote it via
   `uv run gobby install --no-interactive` in the same window.
3. Live smoke, both paths: from a shell inside a gclient pane, run
   `bash src/gobby/install/shared/workflows/runbooks/orchestration-v1.sh`. Expect two
   new tabs, `control` (program director | assistant) and `monitors` (log monitor over
   archivist), four panes labeled with their roles in decision 14's splits, each CLI
   started by the daemon with its kickoff prompt submitted into the composer and no
   shell under it. Then
   `bash src/gobby/install/shared/workflows/runbooks/plan-council-v1.sh .gobby/plans/runbooks.md`:
   one tab named `runbooks` with the plan writer over enhancer | adversary | mechanic.
   From any session: `gobby-workspaces:get_workspace` shows each tab's `runbook` and each
   pane's `role`, `session_ref`, `backend` `native` and `sandbox` (`enforced` true with a
   `policy_hash` for eleven seats; `enforced` false with the reason for the
   program-director), and the assistant has one registration message per seat naming
   its pane ref and backend (criterion 3). The other launch paths, once each (1.5.2
   live): from the shell, `gobby agents spawn --agent researcher --split <assistant pane
   ref> --right -- "<kickoff>"` lands a researcher pane beside the assistant; from the
   assistant pane, `gobby-agents:spawn_agent` with `placement.split` beside the
   researcher lands a reviewer pane; `get_workspace` shows both with their roles, the
   researcher's and reviewer's `sandbox.policy_hash` equal to each other (same deny
   root, same provider defaults) and each seat's `cwd` (from `pwd` in the pane) equal to
   the tab's checkout.
4. Role activation and rules: in the log-monitor pane, `gobby-workflows:get_variable
   _agent_type` (or the first prompt's injected text) shows `log-monitor`; an Edit on a
   `.py` file is blocked by `runbook-no-code-edits`. In the plan-writer pane an Edit on
   `.gobby/plans/runbooks.md` passes and one on `docs/guides/agents.md` is blocked. In
   the program-director pane, the first `.py` edit is blocked once with the delegate
   nudge and the retry passes; a `git merge` is never nudged; `/clear` then a fresh
   `.py` edit is nudged again; the five guards block before execution, so harmless forms
   prove them: `git add -A` (blocked), `git commit --only -m x -- README.md` (passes the
   guard and then fails on the claimed-task rule until a task is claimed), `psql -c
   "select 1"` (passes) and `psql -c "delete from sessions where false"` (blocked),
   `python -m gobby.runner --help` (blocked) and `uv run gobby status` (passes), `git push
   nowhere HEAD` (blocked) and `git push --dry-run origin HEAD` (passes), `rmdir
   /nonexistent` (blocked) and `rm -f /tmp/nonexistent` (passes); `/clear` in that pane
   and the same probes hold in the successor.
5. Sandbox: `get_workspace` shows `enforced` true for every seat but the program-director
   (1.5.6 live). In the researcher pane `touch .gobby/probe` and `touch src/probe.py`
   are refused by the sandbox (the deny root), and its
   `~/.gobby/runtime/managed-executions/<run-id>/assets/settings.json` carries the
   checkout as a deny-write root literal and canonical; in the assistant pane `touch
   ~/.gobby/bootstrap.yaml` is refused (a protected root, memory fe8aa2a8) and `touch
   docs/probe.md` succeeds (then remove it); in the archivist pane a write to
   `~/Desktop/gobby-probe.md` succeeds (then remove it) and one under the checkout is
   refused; in the mechanic pane `uv run --no-sync gobby plans validate
   .gobby/plans/runbooks.md` exits 0.
6. Continuity (#22660; 1.3.5 and 3.2.10 live): `/clear` in the assistant pane; the
   successor's first prompt carries the assistant text once and `get_workspace` shows
   the new `session_ref` with the same `sandbox`. In the researcher pane, ask a question,
   receive `EVENT=REPORT`, then the seat runs `set_handoff(clear_session=true)`; in the
   reviewer pane, hand it a scratch candidate, receive `EVENT=CANDIDATE_VERDICT`, then
   the same; each successor comes back in its pane as its role, answers a follow-up in
   its format without being told it, and has a source write blocked; the archivist's
   digest records both.
7. Failure path: a script line whose launch is refused (a `--project` name that does not
   exist) prints the tool's error on stderr and exits 1, so `set -e` stops the script
   with the panes created so far left in place and the `CREATED_TAB=` lines on stderr
   name the tabs to remove with `gclient kill`; a `launch --split` on a pane ref that does
   not exist exits 1 the same way; `wait-for-output` past its timeout on a tool pane
   exits 1. With the gterm host stopped, `orchestration-v1.sh` is refused at its first
   launch with `terminal_failed`, exits 1, and no role comes up on another backend
   (criterion 5; the launch forces `native`). A seat whose CLI was quit by hand:
   `get_workspace` shows the pane with no `session_ref`; `gclient kill <pane ref>` reaps
   it and the seat's launch line, run again, brings it back with the same role.
8. Amendment disclosure (3.3): claim a scratch task and `update_task` its
   `validation_criteria`; the next turn carries the disclosure order;
   `gobby-tasks-artifacts-ops:get_artifacts` shows `claim_snapshot` with the claim-time
   text; a
   `close_task` whose summary names the amendment passes the close review, and one
   that omits it is bounced with the amendment as the finding.
9. Existing workflows and worktrees (decision 20; 1.5.7 and 1.5.8 live): launch a
   bundled definition that carries a step workflow and both surfaces (`architect`, or
   `analyst` or `task-close-reviewer` with a scratch task) into a scratch tab, once with
   `gobby agents spawn --agent architect --tab scratch --project gobby -- "<prompt>"` and
   once with `spawn_agent` and `placement.split` from the assistant pane; in each pane
   `gobby-workflows:get_variable step_workflow_complete` is false and the run's
   `agent_step_instances` row exists, exactly as a headless `spawn_agent` of the same
   definition gives; then open a lane developer pane with `/goal` (its definition's
   `isolation: worktree`) and confirm `pwd` in the pane is under the worktree path and
   `get_workspace` shows the pane's `worktree_path`; kill the scratch tabs.

**Enhancement round 1 of 1** (kind: enhancement). enhancer_run
`fa6aa37e-ab77-43cd-a1e5-1b9af49c8803` (plan-enhancer-taskless, grok/grok-4.7/high,
child session 65491917), round 1 of cap 1, converged: false, suggestions_presented: 11,
delivered 2026-09-22 06:43Z. Votes by Josh on 2026-09-22 ("fold as read"), folded by
gobby#14156 the same day; rb-1's 2.1 half and rb-7 addressed the YAML loader that
decision 12 had already removed.
- rb-1 (sequencing, 1.2 and 2.1): accept the 1.2 half, decline the 2.1 half. Omitting
  the three keys when None keeps the golden corpus byte-identical and lets either leaf
  close without the other; the 2.1 half targeted row fields the command-mode 2.1 no
  longer adds.
- rb-2 (testability, 2.1): accept. A closed event enum drops the socket on
  `pane.role_set`; one variant lands in the file 2.1 already targets.
- rb-3 (clarity, 3.2): accept. A literal copy of `epic-reviewer` would complete
  `epic_qa`, which the non-goals forbid.
- rb-4 (clarity, 1.4): accept. The seven keys were unnamed, and a stale `_persona_name`
  defeats the recovery path the tool exists for.
- rb-5 (clarity, 3.2): accept. Full selector lists are decision-complete and remove a
  guess the planned tests could not catch.
- rb-6 (clarity, 1.2): accept. `btrim` keeps the roster ref identical to what
  `send_message` resolves.
- rb-7 (clarity, 2.3): decline as moot. The replay state machine it fixed was removed
  by decision 12.
- rb-8 (testability, 3.1): accept. The shell path is the reason the rule uses canonical
  metadata, so the tests exercise it.
- rb-9 (clarity, 3.1): accept. The handler-then-rules order was verified in
  `hook_manager.py` on 2026-09-22; the plan's ordering claim was reversed.
- rb-10 (testability, 1.2): accept. Pins decision 1's survival guarantee in the section
  expansion copies.
- rb-11 (clarity, 1.1): accept. Removes an ambiguity that would have put 1024 on `role`.

## D1 Rust port of the runbook seams (depends: 1.2, 1.3, 1.5)
`kind: deferred`

```yaml
deferral:
  task_ref: "TBD-runbooks-rust-port"
  reason: "The workspace ops, hook ingress, and MCP seams move to Rust family crates at S2.8 (#21565), S2.11 (#21569), and S2.12 (#21570); the launch's placement seam (1.5) moves with the spawn tool at S2.12, runbook scripts can move to a DB-backed registry then, and the command-mode verbs move behind the family crate's API."
  owner: "gobby-1.0"
  original_acceptance_items:
    - 1.2.1
    - 1.3.1
    - 1.5.1
```

## D2 Retire gobby build in favor of runbooks
`kind: deferred`

Decision 19 assumes the retirement: compatibility with `gobby build` is no longer a
constraint, 3.2 and 3.4 already reshape its planning-stage definitions and repoint its
name maps, and v1 deletes none of `gobby build`, `src/gobby/dispatch/` or the stage
registry. What remains is the retirement itself. Its criteria, carried into the
deferred task's validation criteria at expansion (no live deliverable of this plan
delivers them; ruling 23 dropped the post-epic-reviewer that 3.2 once carried for
`epic_qa`):
- D2.1 - A planning pass, after orchestration-v1 has run at least one full night (V1
  step 3 and the archivist's record), names what replaces `epic_qa` and the build
  coordinator for stage dispatch under runbooks, or keeps them, as a plan of its own.
- D2.2 - `gobby build` and stage-manifest dispatch are retired or re-scoped by that
  plan, and the memory that build runs autonomously is retired or rewritten to match.

```yaml
deferral:
  task_ref: "TBD-retire-gobby-build"
  reason: "Josh assumes the retirement (decision 19: 'assume we're retiring gobby build and stages'); v1 deletes nothing of it, and the retirement needs its own planning pass (what replaces epic_qa and the build coordinator, the stage registry and dispatch code to remove, and the memory that build runs autonomously) after orchestration-v1 has run a real night."
  owner: "josh"
  original_acceptance_items:
    - D2.1
    - D2.2
```
