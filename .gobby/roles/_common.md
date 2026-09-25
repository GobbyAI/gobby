# Rules for every role (read before your own file)

- Find your role file by your session in `roster.md`. Stay inside it. If work falls outside your role, send it to the PD; don't do it yourself.
- Cross-session messages go through `gobby-agents:send_message`. Never send keys into another session. Josh: "You don't interrupt lanes, you send messages to PD".
- Do not spawn agents. The automated task-close reviewer is the only permitted spawn path.
- Only the PD (or someone Josh names) restarts the daemon. Every restart gets a global notice before it stops and a DAEMON BACK notice after it's healthy. The Assistant alerts Josh both times.
- Quiet hours are 04:45–06:45 CT for the Game Goblins jobs: no restarts or cutovers, and keep load low. Game Goblins jobs are never rerun.
- In anything Josh reads, every #NNNNN carries its title or a short phrase. Say "Systems nominal" when healthy. Give no load numbers unless load is breaching.
- When filing a task, combine it with closely aligned tasks rather than duplicating them (memory 8c1b9f80).
- Decisions reach Josh as buttons he can click, sent by the Assistant. Ask once and never nag.
