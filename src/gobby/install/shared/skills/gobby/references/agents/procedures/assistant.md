# assistant procedures

A status tick arrives as a message from the status-wake pipeline. Run this procedure at once.
A tick never waits for the operator.

## Cadence and mode

Read `project_policy` with `get_variable(name="project_policy")` on every tick. Use
`report_cadence_seconds` as the report window. Read `role_guidance.assistant` by reference; never
paste it into the report.

- Mode is `full` when the tick's UTC minute is below `report_cadence_seconds / 60`, and `delta`
  otherwise. An explicit `Report mode: full` or `Report mode: delta` in the tick wins.
- Without `report_cadence_seconds`, deliver a full report and say the cadence is unset. Never
  invent a recurring schedule.
- Write every time in US Central (America/Chicago). Convert each UTC timestamp before writing it.

## Gather

Use the query recipe in `gobby:references/agents/procedures/reporter.md`, sections "Cadence and
budget", "Sources" and "In-flight tasks: stage, time in stage, claim-to-close". Load it completely
through `gobby-skills:get_skill_file(name="gobby", path="references/agents/procedures/reporter.md")`.
Fetch its schema first when unleased, and follow every page.next_cursor using only cursor until
null. For a full report, load `references/agents/procedures/reporter-full-report.md` the same way
for the workspace roster and the full sections.

Those files supply queries and budgets only. You deliver the report. No Reporter seat runs.
You run the two in-flight queries with `query_readonly`; no Reporter seat can.
Honor the gathering budget. When it expires, deliver what you have with each gap labelled and the
report marked incomplete.

## Report

Open with one line: `Status, <full|delta>, <start>-<end> CT`.

- Full: every section the gather procedure requires, including in-flight tasks by stage with time
  in stage and claim-to-close. Each root shows its epic, current claims with seat and time, next
  five, and candidates. Then close backlog against cap, holds, blocked or
  escalated items, operator decisions, stalled or idle seats, worktrees against developers,
  the workspace roster, the last Consolidator review, roadmap progress with its critical path, and
  landed, activated, closed and escalated changes since the last report.
- Delta: changes since the last report (landed, activated and closed listed separately), the complete
  current in-flight list every tick (every task with its stage and time in stage, unchanged stages
  included, with stage moves and closures flagged with claim-to-close), stuck items with durations,
  alarms with sources, decisions needed, current holds, and cap breaches. Name the affected roots.
  Do not repeat the full root table.

Rules for every report:

- Name each #NNNNN with its title or a short task phrase. State its real condition. When no work
  is active, say so.
- Write "Systems nominal" only when the evidence establishes that nothing is stuck, alarming or
  breaching.
- List unknowns and source gaps under the section they affect. Never add a generic unknowns
  paragraph.
- Include no log lines, log paths or file paths. Your message says what happened and when.
- Keep it to structure, not prose.

## Deliver

1. Resolve the operator channel with
   `gobby-communications:resolve_operator_conversation(channel="gobby-telegram")`, unless project
   policy names another channel. If it is missing or ambiguous, report the delivery gap to the
   Orchestrator and stop. Never guess a destination.
2. Check `gobby-communications:list_conversation_attachments` and branch on whether the operator
   channel is attached to this pane, meaning this session holds the attachment. Each tick takes
   exactly one of these paths:
   - Attached: type the whole markdown report once as your reply in this pane. Automatic mirroring
     carries it to the operator channel. Do not call `send_operator_message` for this tick, and
     do not announce a send.
   - Not attached: send the whole markdown report once with
     `gobby-communications:send_operator_message(channel=<channel>, content=<report>)`. Inspect
     success and keep the message_id. Type nothing in this pane for this tick.
3. On the send path, if the send fails, retry once after 30 to 60 seconds. If the retry fails too,
   send the exact error to the Orchestrator. On the pane path the report is typed once, and no
   retry applies.
4. On the send path, never resend a confirmed delivery.
