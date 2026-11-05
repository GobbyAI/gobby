# assistant procedures

A status tick arrives from the status-wake pipeline. Run this procedure at once; a tick never waits for the operator.

## Seat recovery

Never spawn seats. When the Orchestrator needs to be stopped or respawned, send
the operator the reason and supporting evidence, request that action, and wait.
The operator performs the shutdown and relaunch. Do not activate another
definition, cold-start, or restart the Orchestrator yourself.

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

You gather the report's evidence yourself with the read-only recipes below. No other seat runs
these reads, and you never request a new inventory from a seat.

Rules:

- Aim to finish each tick in 300 seconds. Spend at most 240 seconds and 35 tool calls gathering,
  including discovery. Continuation calls (`next_offset` pages and offloaded-result pages) and the
  roster session lookups are exempt from the 35-call cap, but stay inside the time budget.
- Page one list to completion before starting the next. Count a total as exact only when the
  accumulated rows equal the reported total; otherwise write "N of total read" and mark coverage
  incomplete.
- Retry a failed daemon call or health check once after 30 to 60 seconds. On a second failure,
  write `GAP: <source> - <error>` in that source's slot. When a daemon outage blocks a required
  read, register `gobby-agents:wait_for_coordination` on the announcing session and yield the turn
  before retrying. Honor an explicit hold for quiet hours or an operation that can kill panes; a
  restart heads-up alone does not pause gathering.
- Read no log files, scan no `~/.gobby/logs`, search no transcripts, capture no terminals, inspect
  no processes, and run no builds or tests.
- Run every SQL statement through `query_readonly`, one read-only SELECT or WITH per call.
- When the budget expires, deliver what you have with each gap labelled and the report marked
  incomplete.

Sources:

1. Seat snapshot. Lease `gobby-agents:list_running_agents`, then request `view="compact", limit=500`.
   Its rows carry every seat's current claims (ref, title, first recorded claim time, task path),
   status, activity age and worktree. Follow next_offset until null on this snapshot and verify the
   accumulated rows equal the total. An empty claims array means no current claim. A missing claim
   timestamp reads unknown, never the run start or the task's updated_at. Map each task path's epic
   to its root. If `query_limit_reached` is true, mark fleet coverage incomplete.
2. Task state. Lease `gobby-tasks:list_tasks` and `gobby-tasks:list_blocked_tasks`. For each root
   epic in `project_policy.roots`, use `parent_task_id=<root epic>, closed=false, view="compact"` for
   its work groups and `refs=[<root epic>]` for the root itself. Also use `claimed=true,
   closed=false, view="compact"` for claims absent from seats, `escalated=true, closed=false,
   view="compact"` for escalations, and `closed=true, updated_since=<30-minute cutoff>,
   view="compact"` for recent closures. Use `list_blocked_tasks(view="compact")` for dependency
   blockers and resolve their titles. Follow each list's next_offset until null and verify the rows
   equal the total. For a counted list offloaded to a result_id, page it with
   `gobby-results:get_tool_result` through every next_offset; never sample it with
   search_tool_result. Inspect closed_at and escalated_at to classify events.
3. Close backlog. Follow `gobby-tasks:list_tasks(candidate=true, closed=false, view="compact")` to
   completion. Exclude planning and operator-decision parks, then compare the count with the cap in
   `project_policy`. Compact rows carry candidate_sent_at and receipts (kind, SHA, recorded_at); use
   them for candidate phase and for landings in the last 30 minutes. An older approval does not
   establish the latest rework's phase. Keep every open candidate in its root block.
4. Operator decisions. Follow `gobby-tasks:list_tasks(label="needs-decision", closed=false,
   view="compact")` and the open escalated list to completion. Deduplicate refs. Decision age uses
   escalated_at, else the recorded opened time, else unknown. Closed or de-escalated parks count as
   resolved; drop them.
5. Git. At report time read `git rev-parse --short <integration branch>` and
   `git log -1 --format='%h %cI' <integration branch>`, with the integration branch from
   `project_policy`. Take the tip from these reads, never from a receipt. Run one
   `git log <integration branch> --since="30 minutes ago" -n 50` for recent context. Verify each
   landing receipt from the last 30 minutes with `git merge-base --is-ancestor <landing SHA>
   <integration branch>`; a failed or unavailable check is an explicit gap. Git proves landing,
   never activation or closure.
6. In-flight tasks. Run the two queries below with `query_readonly`, each with `limit=2000`. The
   first page's `total` is authoritative. Accumulate rows, continuing from the last `seq_num`, until
   the total is reached. A zero-row first page means no matching tasks.

   Query 1, evidence activity. Params: project ID, root path regex `^(<root path>|<root path>)\.`,
   then the keyset cursor (0 initially).

   ```sql
   WITH in_flight AS (
     SELECT t.id, t.seq_num, t.title, t.claimed_by_session_id
     FROM tasks t
     WHERE t.project_id = %s AND t.closed_at IS NULL
       AND t.path_cache ~ %s AND t.seq_num > %s
       AND (
         t.claimed_by_session_id IS NOT NULL
         OR EXISTS (
           SELECT 1 FROM task_comments c
           WHERE c.task_id = t.id AND c.author_type = 'task_attestation'
         )
       )
   ),
   active AS (
     SELECT c.task_id, c.created_at, c.body::jsonb AS evidence
     FROM task_comments c
     JOIN in_flight f ON f.id = c.task_id
     WHERE c.author_type = 'task_attestation'
       AND NOT EXISTS (
         SELECT 1 FROM task_comments correction
         WHERE correction.task_id = c.task_id
           AND correction.author_type = 'task_attestation'
           AND correction.body::jsonb ->> 'supersedes_id' = c.body::jsonb ->> 'id'
       )
   ),
   latest AS (
     SELECT DISTINCT ON (task_id) task_id, created_at, evidence
     FROM active
     ORDER BY task_id, (evidence ->> 'sequence')::bigint DESC
   ),
   claims AS (
     SELECT ci.task_id, min(ci.claimed_at) AS first_claimed_at
     FROM task_claim_intervals ci
     JOIN in_flight f ON f.id = ci.task_id
     GROUP BY ci.task_id
   )
   SELECT f.seq_num, f.title, f.claimed_by_session_id,
          l.evidence ->> 'kind' AS evidence_kind,
          l.evidence ->> 'result' AS evidence_result,
          l.evidence ->> 'summary' AS evidence_summary,
          l.evidence -> 'reference' AS evidence_reference,
          l.created_at AS evidence_recorded_at,
          cl.first_claimed_at, count(*) OVER () AS total
   FROM in_flight f
   LEFT JOIN latest l ON l.task_id = f.id
   LEFT JOIN claims cl ON cl.task_id = f.id
   ORDER BY f.seq_num
   ```

   Query 2, claim-to-close. Params: project ID, report-window start ISO timestamp, root path regex,
   then the keyset cursor (0 initially).

   ```sql
   WITH closed AS (
     SELECT t.id, t.seq_num, t.title, t.closed_at
     FROM tasks t
     WHERE t.project_id = %s AND t.closed_at >= %s
       AND t.path_cache ~ %s AND t.seq_num > %s
   )
   SELECT c.seq_num, c.title, c.closed_at,
          min(ci.claimed_at) AS first_claimed_at,
          c.closed_at - min(ci.claimed_at) AS claim_to_close,
          count(*) OVER () AS total
   FROM closed c
   LEFT JOIN task_claim_intervals ci ON ci.task_id = c.id
   GROUP BY c.id, c.seq_num, c.title, c.closed_at
   ORDER BY c.seq_num
   ```

   Group rows by root. Label each open task `<title> (#NNNNN): latest evidence <kind>/<result>,
   recorded <duration> ago; claimed since <time>`. With no attestation, write "no attestation" and
   do not infer development. Closed in the window: `<title> (#NNNNN): claim-to-close <duration>`.
   Attestations describe evidence; never reconstruct fixed test, development or review rounds from
   counters or retired evidence. A correction supersedes the record it names. A missing claim time
   reads "untimed: no claim interval". Record query failures as source gaps.
7. Workspace seat roster, for full reports only.
   - Resolve your project_id and machine_id from `gobby-sessions:get_session` for your session ref.
     Lease `gobby-workspaces:list_workspaces`, select the workspace whose default_project_id matches
     that project on that machine, then call `gobby-workspaces:get_workspace(workspace=<selected
     id>, node=<machine id>)`. Never assume the projectless workspace named default is the
     project's workspace. If it is missing or inaccessible, keep the heading with a source gap;
     never create a workspace.
   - List every tab in tab order and every pane under its tab in layout order. Join each pane's
     terminal_id to `gobby-sessions:get_session` terminal_context.gobby_terminal_id. Reuse the
     session refs from the agent snapshot, and lease `gobby-sessions:list_sessions` for the machine
     with limit=500 to find interactive seats absent from agent runs. Ignore expired and archived
     sessions. When several live sessions match one terminal, report the ambiguity rather than
     choose. If the session inventory reaches its limit, mark coverage incomplete.
   - Read session details through `gobby-results:search_tool_result`, taking terminal_context,
     source, model, reasoning_effort, status and current activity only. Never read session
     summaries, handoffs or transcripts for the roster.
   - For each pane give its label (or unlabeled), seat and role, provider, model, reasoning effort,
     state, and every current claimed task ref with title (or none). Use live agent rows for role,
     state and claims. Use session source, model and reasoning effort for missing provider, model
     and effort, never inferred defaults; missing metadata stays unknown with its source gap. State
     is stalled when stall_suspected is true, waiting when a wait is recorded, working when current
     activity proves it, and idle when inactivity is recorded. Otherwise it is unknown. A pane with
     no live session is empty.
   - Format one tab heading, then one line per pane: `label | seat | provider / model / reasoning
     effort | state | task ref and title or none`. Example: `unlabeled | empty | none / none / none | no seat | task none`. Never omit empty panes or unclaimed seats. Report tabs_read/total and panes_read/total.

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

_Version: 3.0.1_
_Last verified: 2026-11-04_
