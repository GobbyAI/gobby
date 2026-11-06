# Coordinate through durable messages

Load when sending or reading cross-session work, blockers, or repository notices.
Use `gobby-agents:send_message`; terminal keystrokes are not a messaging transport.
Discover the intended session or agent run identity before selecting the target.

Target `parent` resolves the session that spawned the sender, forbids `target_id`,
and is available only to spawned agents. Spawned agents may omit `target`
(it defaults to `parent`) and cannot override `from_session`. The bundled rule
`scope-spawned-agent-send-message` limits a spawned agent to the target modes its
agent definition lists in `send_message_targets`; the default is `["parent"]`.
Root sessions may use every mode.
Targets `session` and `agent` require `target_id`. A targetless `project`
send coordinates the current repository; targetless `global` reaches other live
non-system sessions on the sender's machine across projects. Ordinary project
broadcasts derive project scope from the sender; do not override it. System sends
must supply project scope. Preserve the returned message IDs and inspect partial
broadcast/wake failures before retrying.

Content never implies wake. Omitting `wake` requests immediate processing for direct
`session`, `parent`, and `agent` targets. Fanout (`global`, and `project` with
`wake=true`) applies that same rule to each recipient, with no claimed-task filter. A
wake types `[Gobby] Check messages` only into an idle, paused, or waiting seat whose
composer the terminal ledger confirms empty and that has no open turn; the seat then
processes its queue without operator input. An active turn takes no typed input at any
priority: its message arrives through the hook mailbox on its next tool call. A draft,
held, or unconfirmed composer, and an interrupted, input-, approval-, or
handoff-waiting seat, take no typed input either, so the message stays queued until the
seat is safe to wake. The one handoff exception is the `get_handoff` continuation of an
awaiting-handoff terminal with a confirmed compact boundary and an empty composer.
Otherwise `project` fanout stays queued without live wakes, and `wake=false` queues any
target without a live wake. The daemon sends restart and cutover notices itself as
system globals: a hold before it stops and DAEMON BACK once healthy. Never compose them
by hand. A successful send is durable queuing, not evidence that the
recipient read or acted on it. Use `get_inter_session_message` for a message ID
and `get_inter_session_messages` for supported bounded mailbox filters. Read all
needed windows; lists are not automatically exhaustive.

## Deliver text artifacts across workspace boundaries

Send a review, report or plan document with `send_attachment`. `content` is the
message text: the task, the candidate SHA, and what the document is. `document` is the
complete UTF-8 text, `filename` names it, and `media_type` is `text/markdown` or
`text/plain`. The daemon stores the document in the database with its byte size and
SHA-256 digest. A document over 256 KiB is refused with `attachment_too_large`, and
nothing is stored; split it into numbered parts, one attachment each. Preserve the
returned message IDs in the handoff and attestation evidence. A sender's private
workspace path alone is not a delivery: recipients cannot read that folder.

The recipient calls `get_inter_session_message(message_id=...)` for each ID to see the
attachment's filename, size and digest, then `get_attachment(message_id=...)` for its
content. It checks the task, candidate and digest, and writes the exact content to a
file in its own session workspace. Choose the local filename there; never treat a
sender's path as a write destination. For numbered parts, fetch every part and verify
the assembled bytes against the digest named in the parts before acting. A notification
preview or mailbox list is not the full artifact. Confirm the received message IDs and
local artifact to the sender; the sender retains its original.

A `project` or `global` send with a document gives each recipient its own message ID
and copy, and each recipient reads its own copy. Only the sender and that message's
recipient can read it; anyone else sees `attachment_not_found`.

For a developer review, address the task's assigned developer through the declared
report route. Orchestrator rulings and dispositions go through Triage, which retrieves
the complete document with `get_attachment` and forwards it to the target seat with
`send_attachment` and `wake=true`. Forwarding creates new message IDs; give those
recipient-addressed IDs to the developer, since only each message's sender and recipient
may retrieve it.

For an operator report, send the complete document to the Assistant with
`send_attachment` through its declared route. The Assistant reads it with
`get_attachment`, reconstructs its own file, and uses `send_operator_attachment` with
`project_policy.operator_channel`; report the returned delivery message ID. This route
grants no access to another seat's folder.

For a coordinated hold, load the schema for `wait_for_coordination` and register
once against the owner session. Supply exactly one condition: a unique
`coordination_key`, a nonempty list of canonical session `statuses`, or
`reply=true`. Yield when the outcome is `waiting`. The owner releases a keyed
hold with a durable `coordination_release` message to you containing the
matching `metadata.coordination_key`; ordinary message text does not release a
keyed hold. A reply wait is the primitive for a requested parent reply: it
resolves `replied` on the next durable message that owner sends you after
registration. Daemon receipts written in the owner's name (`agent_started`,
`agent_recovery`) never resolve it. Omitting `owner_session` waits on the session that spawned
you. Messages already sent never resolve it, so send the question and register
in the same turn. A registered wait holds off idle and stuck cleanup until it
resolves, times out, or is cancelled.
To yield while your own provider background task runs (a background shell,
monitor or workflow that re-invokes you when it finishes), register
`background_task_id=<the provider's task id>` with no `owner_session` and a
timeout that covers the job's next report. Your next prompt clears it; expiry
wakes you to check the job and register again if it still runs.
The default timeout is 900 seconds, maximum 3600. While waiting, repeating the same
owner and condition returns the original wait and expiry. After completion,
repeating the same owner and condition creates a new wait. Preserve `wait_id` and
inspect the terminal outcome before registering again. Use a fresh key for a
separate keyed hold. Only the waiting session can use `cancel_coordination_wait`
to cancel its own wait.

When a reply may come from either of two owners, register one reply subscription
per owner in the same turn before yielding and retain both wait IDs. On the first
reply, cancel the other active subscription before processing it. Cancel stale
subscriptions before acting on an owner's message that arrives outside a wait,
and cancel all outstanding waits before ending. On timeout, cancel the remaining
subscription and report the unresolved dependency once. Do not automatically
rearm hourly timeout cycles; register fresh waits only for a new pending response.

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

_Version: 2.3.0_
_Last verified: 2026-11-06_
