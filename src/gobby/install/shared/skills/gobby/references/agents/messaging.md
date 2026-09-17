# Coordinate through durable messages

Load when sending or reading cross-session work, blockers, or repository notices.
Use `gobby-agents:send_message`; terminal keystrokes are not a messaging transport.
Discover the intended session/run/build identity before selecting the target.

Target `parent` resolves the session that spawned the sender, forbids `target_id`,
and is available only to spawned agents. Spawned agents may omit `target`
(it defaults to `parent`); they may use only `target="parent"` and cannot
override `from_session`.
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

For a coordinated hold, load the schema for `wait_for_coordination` and register
once against the owner session. Supply exactly one condition: a unique
`coordination_key`, a nonempty list of canonical session `statuses`, or
`reply=true`. Yield when the outcome is `waiting`. The owner releases a keyed
hold with a durable `coordination_release` message to you containing the
matching `metadata.coordination_key`; ordinary message text does not release a
keyed hold. A reply wait is the primitive for a requested parent reply: it
resolves `replied` on the next durable message that owner sends you after
registration, and omitting `owner_session` waits on the session that spawned
you. Messages already sent never resolve it, so send the question and register
in the same turn. A registered wait holds off idle and stuck cleanup until it
resolves, times out, or is cancelled.
The default timeout is 900 seconds, maximum 3600. Repeating the same condition
returns the original wait and expiry, even after completion; use a fresh key
for a new hold. Preserve `wait_id` and inspect the terminal outcome. Only the
waiting session can use `cancel_coordination_wait` to cancel its own wait.

For a task blocker, use `target="parent"` (or omit `target`) and send the failing command, diagnostics, paths,
impact, and exact task identity in `metadata.task_id`. In configured worker steps,
a successful matching `message_type="task_blocker"` sets the blocker transition;
then call `end_agent_run` with the structured blocker handoff when the termination
step permits it. The message itself is not a universal process-exit operation.
For a question that keeps the worker alive, use `message_type="message"` and
register a reply wait in the same turn, then yield. Parent recovery must inspect
retained work before respawning with the answer.

Guide: [Coordination](../../../../../../../../docs/guides/agents.md#runtime-tools)
and [blocked children](../../../../../../../../docs/guides/agents.md#blocked-child-communication).

_Last verified: 2026-09-17_
