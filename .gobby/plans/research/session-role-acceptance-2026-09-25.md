# Session role acceptance evidence — 2026-09-25

Task #22894 (Load per-session role files from default agent profile) uses role-named files for 14 standing sessions. A new session with no exact roster row keeps the generic default profile.

## Installed profile

After the 0.5.0 restart (daemon PID 4447), `gobby-workflows:get_agent_definition(name="default")` returned `source=installed`, `enabled=true`. Both `prompts.persona` and `prompts.agent` contained `roster.md`, `--git-common-dir`, and project ID `d45545c5-ded5-4335-b115-0245752edacf`.

After the 14-seat roster landed as `947d5d6be1`, the installed definition still returned `source=installed`, `enabled=true`, and surfaces `spawn` and `persona`. Its description, surfaces, rule selector, and both prompt texts matched bundled `src/gobby/install/shared/workflows/agents/default.yaml` (prompt lengths and Unicode fingerprints compared). That merge changed only Markdown role files; it did not replace the installed definition or restart the daemon.

## Current roster

The checked-in `.gobby/roles/roster.md` has exactly 14 rows, with unique exact `gobby#<seq>` refs and 14 existing role files. A direct `uv run python` assertion checked count, uniqueness, file existence, headings for the three changed seats, and absence of the old Monitor `gobby#14307` row. The changed rows are `| monitor.md | gobby#14573 |`, `| plan-writer.md | gobby#14578 |`, and `| plan-adversary.md | gobby#14579 |`. The other 11 rows remain mapped to their existing files.

Verified rows from the landed roster:

| Role file | Session |
| --- | --- |
| assistant.md | gobby#14069 |
| program-director.md | gobby#14543 |
| lane-1-gclient.md | gobby#14544 |
| lane-2-stability.md | gobby#14505 |
| lane-3-hooks.md | gobby#14531 |
| lane-4-backlog.md | gobby#14506 |
| rust-migration.md | gobby#14549 |
| lane-manager.md | gobby#14556 |
| code-reviewer.md | gobby#14527 |
| researcher.md | gobby#14550 |
| archivist.md | gobby#14308 |
| monitor.md | gobby#14573 |
| plan-writer.md | gobby#14578 |
| plan-adversary.md | gobby#14579 |

## Existing mapped interactive session

In existing session `gobby#14549`, `gobby-agents:apply_persona(agent="default")` succeeded. The next turn injected the updated `## Session role` block. From linked worktree `/Users/josh/.gobby/worktrees/gobby/task-22894-session-roles`, Git resolved common directory `/Users/josh/Projects/gobby/.git`. Both `.gobby/project.json` IDs matched the required Gobby project ID. The session read shared `.gobby/roles/_common.md`, then `roster.md`, found exact row `| rust-migration.md | gobby#14549 |`, and read only `rust-migration.md`, headed `# gobby#14549: Rust Migration`. The session reported this receipt to the Program Director, `gobby#14543`.

## Final-state mapped interactive sessions

After merge `947d5d6be1`, the following existing sessions invoked `gobby-agents:apply_persona(agent="default")` themselves and reported `mode=persona`, `persona_applied=default`. The tool reported that the updated persona would be injected on the next user turn.

- Monitor `gobby#14573` (session UUID `87328e48-ec9b-4b48-8bff-a5e32ffa1ded`) verified existing Git common directory `/Users/josh/Projects/gobby/.git` and matching session/shared project IDs `d45545c5-ded5-4335-b115-0245752edacf`. It read `_common.md`, then `roster.md`, then only exact-match `monitor.md`. It reported its 10-minute checks, `Systems nominal` or evidenced `EVENT=ALARM` reporting, alarm copy to the Assistant, and matched-window performance verdict boundary.
- Plan Writer `gobby#14578` (session UUID `808a0cfc-61ab-4fd7-8774-4d3e75bd228d`) verified the same existing Git common directory and matching current/shared project IDs. It read `_common.md`, then `roster.md`, then only exact-match `plan-writer.md`. It reported complex-plan drafting, sending drafts to the Adversary via `gobby-agents:send_message`, revised candidate and disagreements to the PD, no spawning, and no expansion or dispatch until the PD confirms Josh's approval.
- Plan Adversary `gobby#14579` (session UUID `3ce7b2cd-1a6c-4946-893c-cc4d5ac0fd9e`) verified the same existing Git common directory and matching current/shared project IDs. It read `_common.md`, then `roster.md`, then only exact-match `plan-adversary.md`. It reported complex-plan review, findings to the Writer, disagreement to the PD, questions through PD/Assistant, no spawning, and no expansion or dispatch until the PD confirms Josh's explicit approval.

## Newly spawned unrostered agent

Under the Program Director's limited GO, the Lane Manager spawned a five-minute read-only `default` agent in a linked worktree. Agent run `fa48a994-f15a-4f0c-90df-8d03b4b3adef` had its own session ref `gobby#14563`. Its recorded result reported the injected session-role block, Git common directory `/Users/josh/Projects/gobby/.git`, matching worktree/shared project IDs, and no exact row for `gobby#14563`. It used the generic-profile fallback and read no role file. The run was cancelled after recording this receipt.

`gobby#14563` remains unrostered after the 14-seat update, so this is a real default-agent fallback receipt against the current role lookup contract. The no-manual-spawn rule below remains in force; no additional spawn was used for the roster update.

Josh subsequently limited future spawns to the automated task-close reviewer. The shared role instructions now direct sessions to route work among existing sessions.

## Fixture preconditions

`tests/workflows/test_default_agent_role_contract.py` parses both installed-template prompt surfaces, creates a temporary older linked worktree whose shared checkout alone has role files, and checks exact lookup to `_common.md` and `rust-migration.md`. Its isolated negative fixtures cover missing Git checkout, invalid Git common directory, different project ID, missing `_common.md`, missing roster row, and missing role file. The test probes the documented Git and filesystem preconditions; the mapped and unrostered live receipts above verify agent behavior against the installed prompt.
