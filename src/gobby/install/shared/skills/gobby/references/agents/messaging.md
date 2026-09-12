# Coordinate through durable messages

Load when sending or reading cross-session work, blockers, or repository notices.
Use `gobby-agents:send_message`; terminal keystrokes are not a messaging transport.
Discover the intended session/run/build identity before selecting the target.

Targets `session`, `agent`, and `build` require `target_id`. A targetless `project`
send coordinates the current repository; targetless `global` reaches other live
non-system sessions on the sender's machine across projects. Ordinary project
broadcasts derive project scope from the sender; do not override it. System sends
must supply project scope. Preserve the returned message IDs and inspect partial
broadcast/wake failures before retrying.

Content never implies wake. Set `wake=true` only for intended immediate processing;
interrupted or input/approval/handoff-waiting sessions retain the message without
unsafe daemon input. A successful send is durable queuing, not evidence that the
recipient read or acted on it. Use `get_inter_session_message` for a message ID
and `get_inter_session_messages` for supported bounded mailbox filters. Read all
needed windows; lists are not automatically exhaustive.

For a task blocker, send the parent the failing command, diagnostics, paths,
impact, and exact task identity in `metadata.task_id`. In configured worker steps,
a successful matching `message_type="task_blocker"` sets the blocker transition;
then call `end_agent_run` with the structured blocker handoff when the termination
step permits it. The message itself is not a universal process-exit operation.
For a question that keeps the worker alive, use `message_type="message"` and the
applicable wait/turn boundary. Parent recovery must inspect retained work before
respawning with the answer.

Guide: [Coordination](../../../../../../../../docs/guides/agents.md#runtime-tools)
and [blocked children](../../../../../../../../docs/guides/agents.md#blocked-child-communication).

_Last verified: 2026-09-12_
