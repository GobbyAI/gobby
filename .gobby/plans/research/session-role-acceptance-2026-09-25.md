# Session role acceptance evidence — 2026-09-25

Task #22894 (Load per-session role files from default agent profile) uses role-named files for 12 standing sessions. A new session with no exact roster row keeps the generic default profile.

## Installed profile

After the 0.5.0 restart (daemon PID 4447), `gobby-workflows:get_agent_definition(name="default")` returned `source=installed`, `enabled=true`. Both `prompts.persona` and `prompts.agent` contained `roster.md`, `--git-common-dir`, and project ID `d45545c5-ded5-4335-b115-0245752edacf`.

## Existing mapped interactive session

In existing session `gobby#14549`, `gobby-agents:apply_persona(agent="default")` succeeded. The next turn injected the updated `## Session role` block. From linked worktree `/Users/josh/.gobby/worktrees/gobby/task-22894-session-roles`, Git resolved common directory `/Users/josh/Projects/gobby/.git`. Both `.gobby/project.json` IDs matched the required Gobby project ID. The session read shared `.gobby/roles/_common.md`, then `roster.md`, found exact row `| rust-migration.md | gobby#14549 |`, and read only `rust-migration.md`, headed `# gobby#14549: Rust Migration`. The session reported this receipt to the Program Director, `gobby#14543`.

## Newly spawned unrostered agent

Under the Program Director's limited GO, the Lane Manager spawned a five-minute read-only `default` agent in a linked worktree. Agent run `fa48a994-f15a-4f0c-90df-8d03b4b3adef` had its own session ref `gobby#14563`. Its recorded result reported the injected session-role block, Git common directory `/Users/josh/Projects/gobby/.git`, matching worktree/shared project IDs, and no exact row for `gobby#14563`. It used the generic-profile fallback and read no role file. The run was cancelled after recording this receipt.

Josh subsequently limited future spawns to the automated task-close reviewer. The shared role instructions now direct sessions to route work among existing sessions.

## Fixture preconditions

`tests/workflows/test_default_agent_role_contract.py` parses both installed-template prompt surfaces, creates a temporary older linked worktree whose shared checkout alone has role files, and checks exact lookup to `_common.md` and `rust-migration.md`. Its isolated negative fixtures cover missing Git checkout, invalid Git common directory, different project ID, missing `_common.md`, missing roster row, and missing role file. The test probes the documented Git and filesystem preconditions; the two live receipts above verify agent behavior against the installed prompt.
