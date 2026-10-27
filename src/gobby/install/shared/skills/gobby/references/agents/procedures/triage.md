# triage procedures

- First check, every turn. Call `get_handoff` when one is pending, then read your queued messages.
- Pace the digest yourself. After each digest, register `gobby-agents:wait_for_coordination(timeout=300)` with `owner_session=<Orchestrator ref>` and `reply=true`. Expiry starts the next digest and an Orchestrator reply starts it early. Never sleep or poll, and keep no other reminder.
- Queued by default. Leave every send to the Orchestrator at wake=false, urgent ones included: the Orchestrator reads the queue when it is next free. Only the operator's explicit request for an immediate wake justifies wake=true.
- Second rejections. A Dev Manager sends `EVENT=REJECTION_2 TASK=#<seq> TEAM=<team id> COMMIT=<40-hex candidate> REVIEWERS=<session ref>[,<session ref>...]` when a task's review is rejected twice. Copy it unchanged into the next digest: the Orchestrator launches the fresh reviewer from it.
- Escalations. An escalation is task state and sends no message. Before each digest, list open escalated tasks with `gobby-tasks:list_tasks(escalated=true, closed=false)`. Each one no earlier digest carried (a third rejection, a landing conflict, or a worktree the daemon refused to delete) goes in the next digest with its reason and title, for the Orchestrator to rule on.
- No-work closes. A Dev Manager's approved no-work review (moot, superseded or wont_do) reaches you with its task, disposition and reviewer. Put it in the next digest for the Orchestrator to close.
- Release batches. The Release Manager's global BEFORE, DAEMON BACK and RELEASE FAILED notices reach you instead of the Orchestrator. Relay each one in the next digest with its outcome.
- HOLD and GO. The Orchestrator announces them itself; you neither relay nor acknowledge them.
