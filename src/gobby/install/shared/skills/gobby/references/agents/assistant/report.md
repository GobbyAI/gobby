# Assistant: report and deliver

Load on a status tick after gathering, and before delivering another seat's report.

## Report handoff from another seat

To deliver another seat's report, use the full-content
[artifact transport](../messaging.md#deliver-text-artifacts-across-workspace-boundaries).
Retrieve each complete message by ID, verify all parts and the content digest,
then write the exact text into your own workspace folder. Read
`project_policy.operator_channel`, then call
`gobby-communications:send_operator_attachment(channel=<operator_channel>, file_path=<local copy>)`.
Keep the returned message ID and acknowledge delivery to the sender. A source path
in another seat's private folder is a reference, never a readable attachment.
Report missing parts or delivery errors.

## Report

Open with one line: `Status, <full|delta>, <start>-<end> CT`.

- Full: every section the gather procedure requires, including in-flight tasks by latest evidence
  with evidence age and claim-to-close. Each root shows its epic, current claims with seat and
  time, and candidates waiting on landing, activation or close. Then close backlog against cap,
  holds, blocked or escalated items, operator decisions, stalled or idle seats, worktrees against
  developers, the workspace roster, roadmap progress with its critical path, and landed, activated,
  closed and escalated changes since the last report. State once that the next-five queue order and
  the last Consolidator review are not carried until the digest lands (#25661).
- Delta: changes since the last report (landed, activated and closed listed separately), the complete
  current in-flight list every tick (every task with its stage and time in stage, unchanged stages
  included, with stage moves and closures flagged with claim-to-close), stuck items with durations,
  alarms with sources, decisions needed, current holds, and cap breaches. Name the affected roots.
  Do not repeat the full root table.

Rules for every report:

- A live run bound to an open task is never an orphan. Check its assigned task and
  current claims against open task state. Idle, waiting, stale activity or an old
  parent alone never establishes orphanhood. Report missing ownership evidence as
  a source gap; route actual cleanup to the Orchestrator.

- Name each #NNNNN with its title or a short task phrase. State its real condition. When no work
  is active, say so.
- Write "Systems nominal" only when the evidence establishes that nothing is stuck, alarming or
  breaching.
- List unknowns and source gaps under the section they affect. Never add a generic unknowns
  paragraph.
- Include no log lines, log paths or file paths; say what happened and when.
- Keep it to structure, not prose.

## Deliver

1. Resolve the operator channel with
   `gobby-communications:resolve_operator_conversation(channel="gobby-telegram")`, unless project
   policy names another channel. If it is missing or ambiguous, report the delivery gap to the
   Orchestrator and stop. Never guess a destination.
2. Check `gobby-communications:list_conversation_attachments` to see whether this session holds the
   operator channel attachment. Each tick takes exactly one path:
   - Attached: type the whole markdown report once in this pane; automatic mirroring carries it to
     the operator channel. Do not call `send_operator_message` or announce a send.
   - Not attached: send the whole markdown report once with
     `gobby-communications:send_operator_message(channel=<channel>, content=<report>)`. Inspect
     success and keep the message_id. Type nothing in this pane for this tick.
3. On the send path, if the send fails, retry once after 30 to 60 seconds. If the retry fails too,
   send the exact error to the Orchestrator. On the pane path the report is typed once, and no
   retry applies.
4. On the send path, never resend a confirmed delivery.

_Version: 1.0.0_

_Last verified: 2026-11-05_
