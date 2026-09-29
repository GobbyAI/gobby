# Rules for every role (read before your own file)

- Find your role file by your session in `roster.md`. Stay inside it. If work falls outside your role, send it to the Orchestrator; don't do it yourself.
- Cross-session messages go through `gobby-agents:send_message`. Never send keys into another session. Josh: "You don't interrupt lanes, you send messages to PD".
- Do not spawn agents. The automated task-close reviewer and the Plan Writer's single `plan-enhancer-taskless` pass per plan (Josh, 2026-09-26) are the only permitted spawn paths.
- A passing `close_task(preview=true)` checks deterministic gates; it is not reviewer admission. Lanes wait for an explicit Lane Manager release before `close_task(preview=false)`. If the service returns `close_review_busy`, wait for the active reviewer to finish and for Lane Manager release before retrying.
- Only the Orchestrator (or someone Josh names) restarts the daemon. Every restart gets a global notice before it stops and a DAEMON BACK notice after it's healthy. The Assistant alerts Josh both times.
- Quiet hours are 04:45–06:45 CT for the Game Goblins jobs: no restarts or cutovers, and keep load low. Game Goblins jobs are never rerun.
- In anything Josh reads, every #NNNNN carries its title or a short phrase. Say "Systems nominal" when healthy. Give no load numbers unless load is breaching.
- When filing a task, combine it with closely aligned tasks rather than duplicating them (memory 8c1b9f80).
- Decisions reach Josh as buttons he can click, sent by the Assistant. Ask once and never nag.
