# Operator decisions for the 1.0 chunk map (2026-11-03, Josh, session gobby#19401)

Source: AskUserQuestion answers and chat messages in session gobby#19401.

1. No gobby-plan and no plan mode. The deliverable is a chunk map that ultracode can execute:
   each chunk is one Gobby task whose description is a self-contained brief.
2. Order: migrate away from the Python daemon first (runner-first; Rust owns the daemon
   process), so route families become libraries of the Rust daemon. Hub/node stays in scope.
3. Chunk sessions run locally with the hub, as default Gobby agents (the `default`
   definition), so runbook policies do not restrict them while they work.
4. Port branch: Josh will "probably start branch 0.9.0 when all the friction work is done";
   Claude creates a worktree off 0.9.0. Chunks land on 0.9.0, not 0.5.0.
5. One approval for the whole chunk map. Each chunk's workflow runs its own
   debt-not-to-port pass and adversarial design and code review (replaces the 10-24
   per-family approval stop for this port).
6. Before the port starts: wrap up #24238 (decision classifier adapter), #23591 (provider
   quota and usage ledger), #24821 (terminal and client), #24820 (code index), and the
   current worktrees (22 open on 2026-11-03). After the start, Python changes are limited
   to bug fixes and friction work.
7. Tests are rewritten from contracts, not translated (heavy test debt).
8. The tmux purge (#25486) is happening now on 0.5.0. It belongs to the start gate, so no
   0.9.0 chunk carries tmux.
9. R7 and other transitional mechanism: "I just want to get this done/away from python as
   efficiently and correctly as possible." Keep transitional machinery only where it is
   the cheapest way to keep correctness evidence at each chunk boundary.
10. Coordinator gobby#19309 relayed Josh's ruling (2026-11-03): no new tasks or scope
    additions until the friction queue is done. The chunk map is designed now, read-only;
    creating chunk tasks waits for Josh's map approval and his word on that ruling.

11. Cutover ownership (Josh, 2026-11-03 ~18:00 CT): "we're not building this with runbook
    agents, so a default agent (like you) would handle it". The cutover and any promotion are
    chunk work for a default-agent session, not the Release Manager seat (memory 928706b3 is
    about the runbook fleet).
12. Comms (Josh): "I don't know if those work. The goal is to have a unified gobby-comms
    interface that works with Telegram, Slack, Teams, email, SMS, iMessage, etc. Not sure what
    gobby_chat is or how it's used currently. Might be legacy code." The Rust target is one
    adapter contract for every channel. Whether each Python adapter works is unknown, so each
    one is verified before it can serve as an oracle.
13. Discord (Josh): "Discord if we can make it work, otherwise defer to after 1.0."
14. Deletion, checkpoints and rollback (Josh): "Yes, it can be deleted in one go, or we can
    strangler fig it. Setting checkpoints during development is probably a good idea, especially
    after we port the daemon runner, the mcp proxy, and the first family. My idea was that each
    route family would be its own rust binary or library, with gcore being the main library.
    Take gtask for example: Suppose we make an update to gtask that breaks it. We should have the
    previous version as a backup binary that we can roll back to and fix if needed."
    - FINAL (Josh, ~18:35-18:45 CT, three messages): "By checkpoints I mean we pause and check
      that we're going in the right direction. Once we build one route family that will serve as
      the template for others, for example." Then: "Checkpoints are review pauses, then cutover
      as you described above." Then: "Not a single end-of-port cutover."
      - Each checkpoint is a review pause (a go/no-go direction check with Josh) followed by a
        live cutover of what that stage built: promote binaries built from 0.9.0 (generation
        kept), flip the stage's runner or families to native, restart outside quiet hours with a
        global announcement, a bounded live observation window with named rollback triggers,
        and a rehearsed rollback.
      - Named checkpoints: after the daemon runner, after the MCP proxy, and after the first
        route family (the template for the rest); later ones per wave as the map proposes.
      - Settled item 15 and the R7 row are revised: promotion happens at checkpoint cutovers.
        There is no merge into 0.5.0 (ruling 15).
      - Python deletion: in one go at the end, or strangler; both are acceptable.
    - Per-family rollback (the gtask example) is an architecture requirement for the cutover and
      later. Proposed D36: families are library crates on gcore inside one gdaemon; promotion
      keeps the previous binary generation; a rollback verb restores it. Neither exists today
      (bin_set_coherence.py:88-153 replaces in place). Per-family processes rejected for 1.0:
      hot paths would cross IPC, and single-owner state would need a distributed owner. Awaiting
      Josh at map approval.
15. Branching (Josh, ~18:36 CT): "we're not merging back into 0.5.0. 0.9.0 will become the new
    working branch we'll merge into, and you'll be working in a worktree like 0.9.0-rust".
    - 0.9.0 becomes the integration branch: project_policy's integration branch moves, and the
      main checkout sits on 0.9.0.
    - Port sessions work in worktrees off 0.9.0 (named like 0.9.0-rust) and land each chunk into
      0.9.0 with land_commit. Close gates and blocked_by readiness work unchanged.
    - D1 is resolved with no code change, so options A and B and the start-gate item drop. The
      0.5.0 merge-forward step drops too.
    - The consequence is lost isolation: the live Python daemon runs from the 0.9.0 main checkout,
      so every port chunk must keep it working. That means no template moves or edits until
      retirement, and port migrations or schema inputs must not break the restart and cutover
      preflight (schema identity, `gdaemon schema plan`, installed set); verify how that
      preflight treats checkout schema inputs newer than the installed gdaemon. CI triggers
      must cover 0.9.0 (ci.yml:3-5, rust-ci.yml), and Python edits stay inert under D4.

16. The runner is a firm switchover (Josh, 2026-11-03 ~19:15 CT, two messages): "I think the
    runner needs to be a firm switchover. Either it works, it works enough that we can debug it,
    or we roll back to python and reassess." Then: "When we move the runner off python I want to
    be off the python runner for the remainder of the build."
    - CP-RUNNER's cutover is one-way. Its window ends in one of three outcomes:
      - it works;
      - it works well enough to debug, so the session stays on the Rust runner and fixes forward;
      - or it rolls back to the Python runner (the pinned pre-cutover generation), and Josh
        reassesses before any retry.
    - The runner gets no dual mode and no runtime runner switch. Ruling 9 stands for the runner,
      and R1 stays reduced. Before CP-RUNNER, R0-R6 land dormant through code defaults, and
      CP-RUNNER-CUT's own commit activates the Rust runner.
    - Once the window closes with the first or second outcome, the Python runner is retired for
      the rest of the build. Its launch path is removed, and every later rollback target is a
      Rust-runner generation. Python domain services keep running as a worker under the Rust
      runner until their families port.
    - Consequence: CP-RUNNER must cover the whole runner before CP-MCP and the first family,
      because nothing can fall back to the Python runner afterward. The whole runner is R0-R6:
      startup, readiness, recovery, periodic work, service lifecycle, shutdown and restart.
      Dependencies that put R2-R6 after A1 or Stage 2 are moved; for example, R4's native
      rotate_due registration moves into A1-RUNTIME.
    - During fix-forward, the rollback target is the pinned pre-cutover generation, never only
      the previous generation, because fix-forward promotions would push the previous one out.
    - Claude's default, not part of the ruling: apply the same three-outcome window and pinned
      target to every checkpoint cutover. Prefer code-default dormancy plus generation rollback
      over runtime switches everywhere; this is the least mechanism, and it matches Josh's gtask
      example ("the previous version as a backup binary").

17. Database backup at every Rust cutover (Josh, 2026-11-03 ~19:20 CT): "each rust cutover should
    include a database backup, in case we're making changes that can't be easily rolled back (like
    removing a lot of dead tables/columns from the database found as tech debt during the port)".
    - Every cutover chunk takes a verified backup of the hub database, plus any other datastore its
      changes touch. The backup comes before any migration or activation, and the rehearsal proves
      the restore.
    - It also allows destructive schema cleanup during the port. Dead tables and columns found as
      port tech debt can be dropped at a cutover instead of waiting for a post-port cleanup.
    - Claude's reading, not Josh's words: "dead" means that nothing reads or writes the table or
      column in the new generation, in the pinned rollback generation, or in Python while Python
      exists. A drop like that keeps the generation rollback valid.
    - A change that the pinned generation cannot run on makes the backup restore the rollback, and
      that restore loses every write made after the backup. The cutover names such a change as an
      irreversible step, and Josh gives his go for it in the review.
    - D22 changes from "expand-only until after the final window" to:
      - expand-only by default;
      - drops of verified-dead tables and columns allowed at a cutover, under its backup;
      - any other destructive change named as an irreversible step in the review packet.

- CONFIRMED (Josh, ~19:25 CT, "I agree with those"). These are now rulings, not Claude's
  defaults:
  - every checkpoint cutover gets the three-outcome window and a pinned pre-cutover generation;
  - dormancy works through code defaults plus generation rollback, with no runtime switches;
  - ruling 17's reading: "dead" means unused in the new generation, the pinned generation and
    Python; any other destructive change is an irreversible step that needs Josh's go at its
    checkpoint review.

- Operator direction on friction #25639 (Josh, ~19:55 CT): "I talked to 19309 about your friction.
  We're working on it. Trying to make default agents fairly unrestricted and handoff should never
  apply to subagents."
  - Josh and coordinator gobby#19309 own #25639. This session does not work on it.
  - For the map: chunk sessions are default agents (ruling 3), so the protocol preamble states
    policy only (for example, gobby-managed worktrees off 0.9.0, land_commit, the code-reviewer
    seat). It must not encode workarounds for default-agent restrictions, because those
    restrictions are being removed.
  - The "no set_handoff while workflow agents run" rule holds only until #25639 lands. The doc
    names #25639 as a start-gate dependency and does not put the workaround in the briefs.

18. Port machine (Josh, 2026-11-03 ~23:20 CT, direction): "I think when the agents done with all
    these fixes to runbooks etc., I'm going to back up everything and move us to a separate machine
    so this can run without game-goblins. I'll be running two independent gobby installs. I'll
    figure out a way to have the agents communicate over tailscale."
    Consequences Claude raised with Josh (open until he answers; fold into map-v2 after the fix
    agent, as start-gate items plus a topology decision):
    - A restore of everything gives the port machine A's comms bot tokens, webhooks, cron jobs and
      external MCP OAuth (rotating refresh tokens). Scrub or disable them before its first start;
      the comms chunks use test channels there (generalizes lens finding COEX-M3).
    - Identity: the port machine gets a fresh machine id, and its task and session numbering is
      offset after the restore, so a ref such as #25650 or gobby#19309 never names two things.
    - Branch on machine A after the cut: does 0.5.0 freeze (ruling 15), or keep landing fixes that
      must be forward-ported to 0.9.0? Claude's default: freeze, except fixes A needs to keep
      running, each cherry-picked to 0.9.0.
    - Cross-machine agent messaging is hub-node scope (NODEHUB, S4-NODE); any tailscale link built
      before them is a stopgap the port replaces.
    Josh's answer (~23:30 CT): "yes, I'm talking about a stopgap. I'll fork 0.9.0 here under a
    different branch, like 0.9.0-gg. If I have to make any gobby changes here I'll push them to this
    remote branch and you can cherry pick."
    So: the port runs on the new machine on 0.9.0; this machine forks 0.9.0 as 0.9.0-gg at the cut
    (0.5.0 freezes); this machine's gobby fixes are pushed to origin/0.9.0-gg; the port
    cherry-picks them into 0.9.0. Claude's proposed mechanics for the map: cherry-pick with -x;
    every checkpoint review lists pending ones with `git cherry 0.9.0 origin/0.9.0-gg` and picks
    them before its cutover (and on Josh's request in between); a fix to Python that a chunk has
    already ported goes to the owning chunk to port or drop; 0.9.0-gg's own commit that points its
    project_policy integration branch at 0.9.0-gg is never cherry-picked.
    Josh, same exchange: "but that way I can start upgrading gobby on the other machine as your
    fixes go live". Claude's reading (stated back to Josh for correction): the flow is two-way.
    This machine (0.9.0-gg) takes each checkpoint once the port machine closes it with outcome 1.
    Map consequences: an outcome-1 CP-*-CUT tags its activated commit on 0.9.0; every cutover
    recipe is written to replay on any install (its own verified backup, the same migrations,
    promotion and activation); this machine adopts by merging the tag into 0.9.0-gg and replaying
    the recipe. Concern 1 (rollback vs migrations) applies to that replay too.
    Josh: "problem is my commits will collide with gobby tasks on the porting machine". Claude's
    fix (proposed): the auto-linker scans the whole message for [project-#N], project-#N and
    fixes/closes/refs project-#N, validating the project name (src/gobby/tasks/commits.py:578-585),
    and new task and session numbers are MAX(seq_num)+1 per project (storage/tasks/_creation.py:175,
    storage/sessions/_crud.py:262). So, once after the restore and before the port machine creates
    any task or session, an operator SQL update gives one row seq_num 99999 in each table; the port
    machine then numbers from #100000 and no ref from either machine names a task on the other, in
    either direction, merges included. The cherry-pick step also puts the port task's own ref in
    the subject and rewrites this machine's refs to gobby-gg-#N, which the linker skips.
    Josh (~23:45 CT): "why wouldn't I do the offset on the non-port (this machine)? that way we keep
    git continuity on the gobby commits and don't have a massive jump in task numbers?" Claude
    agreed: SETTLED DIRECTION, offset THIS machine (0.9.0-gg), so the port line that becomes 1.0
    keeps natural numbering. Condition: the offset (one row to seq_num 99999 in tasks and in
    sessions, here; SUPERSEDED by item 21: 999999 on placeholder rows, after the backup) runs in
    the same daemon-stop window as the backup that seeds the port machine,
    so nothing is numbered here in between. Cherry-picks still put the port task's ref in the
    subject; this machine's refs (#100000+) can never resolve on the port machine. Disjoint ranges
    also let this machine's tasks be imported into the port DB later.
19. Rollback vs migrations (Josh, 2026-11-03 ~23:57 CT, on concern 1): "We should probably figure
    out a workaround". DIRECTION: build one. Claude's proposal, to fold into map-v2 as a D22
    rewrite plus a small chunk that ships at the first cutover (Josh approves it with the map):
    - Facts checked in code: schema_migrations stores (version, filename, checksum)
      (crates/gcore/src/schema/runner.rs:329; resolve_pending_migrations in runner_plan.rs).
      Destructive migrations already carry the `-- gobby:destructive` directive
      (runner_plan.rs:281). An older binary refuses a newer hub in at least three places:
      gdaemon's start check (crates/gdaemon/src/main.rs:286-291), validate_lineage
      (runner_plan.rs:153-169) and resolve_pending_migrations ("receipt v{version} has no matching
      embedded schema asset"). The chunk inventories every other gate that compares a hub head
      with a binary or pin, the Python restart and cutover preflight included.
    - Expand window: a destructive migration's filename carries a marker as well as the
      directive, and a test enforces the pairing both ways. Every gate accepts a hub head newer
      than the code head when each receipt above the code head has a non-destructive filename and
      each receipt at or below it matches the embedded assets as today. A destructive receipt
      above the head still refuses and is named. No new column is needed.
    - Bootstrap: the tolerance carries no schema step of its own and ships at the first cutover
      (CP-RUNNER-CUT), so its rollback target never meets a newer hub. From then on every retained
      generation tolerates expand steps, so rollback across one is binary-only (plus the
      activation revert of COEX-B1). Drops stay at CP-FINAL-CUT and POST-CLEAN with Josh's go,
      where rollback is that cut's backup restore. The same holds when 0.9.0-gg replays a
      checkpoint.
20. Binary promotion (Josh, ~23:57 CT, on concern 2): "promote binaries on checkpoints right? we
    don't want to run a full rust recompile every time a binary is updated". RULED: promote only
    at checkpoint cutovers (ruling 14 stands). Consequences for map-v2: chunk sessions may be
    spawned agents (start_gate[13] goes); the fix agent's separate per-landing schema steps
    (D22, start_gate[9], the standing go for seven expand steps) fold into the checkpoint
    cutovers: a schema-carrying chunk lands in its checkpoint's cut, before activation and under
    that cut's backup, never between checkpoints and never in a post-activation window.
21. Project rename (Josh, ~23:55 CT): "Should we rename the project here too so commits tasks can
    never collide?" Claude's answer: no. The offset (item 18) already keeps new numbers disjoint
    in both directions; every task numbered before the backup is the same task on both machines,
    so a ref to one linking on both is correct. Rename costs: .gobby/project.json is tracked (name "gobby"),
    so the rename is a 0.9.0-gg commit every cherry-pick must skip and every adoption merge may
    conflict on; AGENTS.md and docs say `[gobby-#NNNNN]`. `gobby projects rename` exists
    (src/gobby/cli/projects.py:281). The port machine needs about 74,000 more tasks to reach
    #100000; for more margin, start this machine at #1000000 with the same one-row step.
    Josh (~00:05 CT 2026-11-04): "we've accumulated 25K over the last year, so 1000000 is probably
    correct". SETTLED: no rename; this machine numbers tasks and sessions from 1000000 (this
    supersedes item 18's 99999). Start-gate recipe, operator only, this machine only:
    1. While the daemon runs, create one placeholder task ("numbering offset marker", closed)
       and open and end one throwaway session. Never re-number an existing row: that changes a
       real task's or session's ref.
    2. Stop the daemon and take the backup that seeds the port machine. The placeholders travel
       with their natural numbers, which is harmless there.
    3. After the backup, set the two placeholders' seq_num to 999999 here. Never before the
       backup: the port machine would inherit the offset.
    4. Start the daemon and confirm MAX(seq_num) is 999999 in both tables for the project.
    Prerequisite on 0.5.0, before step 3 and before the fork so 0.9.0 and 0.9.0-gg both carry
    it (Claude's scan of src, crates and web/src for digit-capped regexes found exactly these):
    - src/gobby/memory/dream/related.py:52 `#(\d{3,6})`, used at :787 to pull task refs from
      memory text: a seven-digit ref is read as its first six digits, which names another task.
    - src/gobby/voice/text_normalizer.py:28 `#(?=\d{1,5}(?!\d))`: refs of six or more digits
      skip the spoken-number rewrite.
    MET (landed) 2026-11-04: #25649 at 82b3f2806d on 0.5.0, closed 00:37 CT by gobby#19309; its
    sweep found no other capped pattern. Live on A from its next daemon start. Recipe additions
    (parent, from code): the placeholder task is a root task and step 3 sets path_cache to
    '999999' with its seq_num; six MAX(seq_num)+1 sites, over tasks and sessions only.
22. Six decisions approved (Josh, Telegram msg 015ab2bb, 2026-11-04 ~14:07 CT, relayed by the
    Orchestrator gobby#19309, message 60fdfca9): "I'm good with your recommendations for the port as
    stated." APPROVED as recommended, in the form the Orchestrator showed him: D53 (gclient config
    get|set in 1.0, chunk V1A); D54 (one listing, gobby nodes list with --release, plus gobby nodes
    update); D55 (release-gclient.yml on gclient-v* tags, release.yml deleted at CP-PREDELETE-CUT
    commit B); D41 (interim Python worker op); D47 (build the fence's 3-file Python half, skip
    #25057's 26 files); D48 (keep /api/files/user-md as a second path until POST-CLEAN). NOT covered,
    still open for the map approval request: D60, D61, D76, D91, D86 (Josh saw D60, D61, D76 and D91
    only as unverified numbers, and D86 not at all), and the later D54 and D55 amendments. #25685 goes
    to the #25662 non-blocking queue under Josh's friction paradigm; #25629 does not act on it.
    Also from Josh via the Orchestrator: no further scratch repos; cherrytest and cpx-probe-tmp stay
    for Josh, because hooks refused the Orchestrator's delete.
23. Map approved (Josh, 2026-11-05 ~09:05 CT, through the Assistant, relayed by the Orchestrator
    gobby#19309, message 9a491ad0): "chunk map is approved, but we can't begin until all worktrees
    except 0.5.0 and wiki are resolved" and "and the tmux purge is complete." The relay names no
    row answered differently, so the approval takes every default (ruling 5). It widens ruling 6's
    worktree condition: every linked worktree counts except 0.5.0 and wiki, whenever it was opened.
    No port work starts and no chunk task is created until the start gate clears.
24. Cut, machines and the tmux purge (Josh, 2026-11-12, in session gobby#19401's own pane). At
    ~10:00 CT: "Ok, I think we're done with as much as we're going to change. Need to update the 1.0
    port plan, update the task list, and prep the backup so we can get everthing off this laptop".
    His answers that day, in order:
    - On the start gate work still open (#25603, #23591, #24821, #24820 and the worktree
      fix/root-review-ci-e2e-25833): "waived, I'm pushing right now, will be a minute".
    - On the tmux purge, which had not run: "I did want to remove the tmux stuff. I thought agents
      were done with that already." On running it as the first port work on 0.9.0: "yeah that's
      fine, we'll leave it as part of the port".
    - On the machines: "I think I'm going to copy to the machine at Game Goblins first, so it can
      run independently. Then I'm going to copy everything to my Omarchy linux box with an AMD 395+
      and 128GB of memory, but for now the plan is port work on this machine, and 0.5.0 with game
      goblins". Later: "the game goblins copy will not be making edits to gobby"; "I really just
      want to set up the port and get it done without it interfering with game goblins' daily
      responsibilities, and I really need to get this on a separate machine so i can just let the
      porting agents work and not get distracted by sidequests."; and "I'm not going to be working
      on game-goblins on the port machine, and I'm not going to be connected to an agent via
      telegram on the game-goblins machine."
    - On the cut: "last push happening now, that will be the version", and "once the push is done,
      I can copy everything over thunderbolt to the macbook pro here at game goblins". That push
      left origin's 0.5.0 at 0256724f1f.
    - On where the chunk tasks are created: "Here, before the backup".
    - On one more piece of port work: "File a task for removing /clear handoffs after we get the move done
      and are working on the port." and "add it to the port work". The task is #25935 (D130).
    The map author's reading, which Josh corrects where it is wrong. 0256724f1f is the cut SHA, and
    nothing lands on 0.5.0 after it, so the commits the start gate planned for 0.5.0 before the cut
    become 0.9.0's first commits. Start gate items 1 and 2 are waived, and ruling 23's worktree
    condition with them. Ruling 8's purge moves onto 0.9.0 as its first chunk, TMUX-PURGE (D129).
    Ruling 18's two machines stay with these roles: P, the port machine, is this laptop now and the
    Linux machine later (D128); A is the Game Goblins MacBook Pro, restored from P's backup, on
    0.5.0 at the cut SHA, where nobody edits gobby. So no 0.9.0-gg branch is forked, A gets no
    policy commit, nothing flows from A to P, and ruling 21's numbering offset is not run unless
    Josh asks (D40). Nothing is scrubbed on either machine; on A the gobby project's seat-launching
    cron jobs and its Telegram channel are disabled before its first daemon start. That evening, on
    the cut: "create branch 0.9.0", then "you push." and "I'd like you to fix the task list for the
    1.0 port before pushing to origin. you have the ability to push to origin on my authorization."
    So 0.9.0 was created at the cut SHA that evening, its first commits and the chunk tasks come
    before its push, and the backup that seeds A is taken after the push, as Josh first answered;
    A's restored hub holds the chunk tasks, idle. Later that evening, on the closure rule that
    refuses a superseded close while a commit is linked (D31): "rule off". The rule is switched off
    for the disposition pass and on again before the push.
25. CI does not gate the port (Josh, 2026-11-13 ~14:00 CT, in session gobby#19401's own pane). On
    the red CI that 0.9.0 inherited at the cut: "why do we care about python ci if we're porting? I
    want to avoid sidequests." Then, to the proposal that CI on 0.9.0 be advisory and a chunk's gate
    be its own local validation: "agreed, we don't worry about CI until we get near the end of the
    port when we're validating it". Then: "do we have a capture of what failed at CI so the porting
    agents can discern whether it's a bad test or bad behavior?" That capture is
    docs/port/ci-baseline-at-cut.md.
    The map author's reading, which Josh corrects where it is wrong. Until the port's final
    validation, a red CI run on 0.9.0 blocks no landing, no close and no cutover, and nobody fixes
    CI as a task of its own. This overrides the map and the protocol wherever they make CI a
    condition: start gate item 9's 'both workflows treated as required' and item 11's 'fixed on
    0.9.0 before the first chunk lands', D34's default, D85, protocol step 8's CI check and each
    cutover's refusal to activate a commit whose CI is red. Their text is not rewritten. A chunk's
    gate is protocol step 6's local validation. A failure listed in the baseline is old; a chunk
    that works in the code behind a listed failure fixes it as found work, and a failure that is
    not listed and that the chunk caused is that chunk's to fix. Josh says when the final
    validation begins.
