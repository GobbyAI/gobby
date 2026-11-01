# reporter procedures

## Cadence and budget
Read `report_cadence_seconds` and mode selection from the injected project policy or launch inputs. The seat yields between ticks on that cadence; it does not end after one report. An explicit `Report mode: full` or `Report mode: delta` in the launch prompt wins. Without a configured cadence, produce a full report for this invocation; never invent a recurring schedule.
Aim to finish each tick in 300 seconds, well inside the cadence. Spend at most 240 seconds and 35 tool calls gathering, including discovery; next_offset continuation calls (including offloaded-result pages and projection --offset calls) and bounded queue refills described below are exempt from the 35-call cap, but remain inside the time budget. Page one list to completion before starting the next. In full mode, gather each root epic's work groups, complete seat snapshot, queue projection and complete candidate list first; fill every policy-defined root current/next-five/candidate block before gathering supplementary evidence. Reuse these rows for the other sections. Collect every required section before prose. State a count as exact only when accumulated rows read equal total; otherwise write "N of total read" and mark coverage incomplete. If the gathering budget expires, retain the full section scaffold, label unavailable evidence explicitly, deliver it, and mark the run result incomplete. Do not wait for seats unless an explicit hold or unavailable daemon prevents required reads, as described below. Never repeatedly poll or paginate through historical data.

Retry a failed daemon call or health check once after 30-60 seconds, within the tick's budget and reserving time for delivery. On a second failure, write "GAP: <source> - <error>" in that source's report slot. A restart or cutover heads-up alone does not pause work. If a daemon call fails during an announced restart, continue independent gathering. When daemon unavailability prevents the remaining required reads, register a bounded coordination wait on the announcing session and yield the turn before retrying; retain affected source gaps if the daemon remains unavailable. Honor an explicit hold for quiet hours or an operation that can kill panes. A retry or recovery wait may exceed the normal 240-second gathering budget, but never the tick's delivery reserve.

## Sources
Before gathering, load `gobby:references/agents/procedures/reporter-sources.md` completely through `gobby-skills:get_skill_file(name="gobby", path="references/agents/procedures/reporter-sources.md")`. Fetch its schema first when unleased; follow every page.next_cursor using only cursor until null. It defines the root-queue, fleet, Archivist and Git source rules that every section below reuses.

## In-flight tasks: evidence activity and claim-to-close
Every full and delta report carries this section. Cover open tasks under the policy roots
(`project_policy.roots`, matched by `path_cache` prefix) that are claimed or have task
attestations, plus tasks closed in the report window.

Use `query_readonly` only when your installed definition permits it. Otherwise report
`GAP: in-flight evidence - query_readonly unavailable to this seat` and send the need
to the configured coordinator. Claim age remains available from the complete
`list_running_agents` snapshot.

Attestations describe evidence; their kinds and results do not imply a universal workflow
stage. Report the latest active attestation's kind, result, summary, reference and time.
A correction supersedes its named earlier record. Never reconstruct fixed test/development/
review rounds from historical counters or retired evidence. If selected policy supplies
a stage interpretation, name that policy and distinguish its interpretation from observed
activity. Without one, label this column "latest evidence", not "completion stage".

Query 1, evidence activity. Params: project ID, root path regex
`^(<root path>|<root path>)\.`, then the keyset cursor (0 initially).
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

Query 2, claim-to-close. Params: project ID, report-window start ISO timestamp,
root path regex, then the keyset cursor (0 initially).
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

Run each query with `limit=2000`. The first page's `total` is authoritative.
Accumulate rows, continuing from the last `seq_num` until that total is reached.
A zero-row first page means no matching tasks. Report "N of total read" whenever
coverage is incomplete.

Group by policy root:
- `<task title> (#NNNNN): latest evidence <kind>/<result>, recorded <duration> ago; claimed since <time>`.
- With no evidence: "no attestation"; do not infer development.
- Closed in window: `<task title> (#NNNNN): claim-to-close <duration>`.

Evidence age measures time since a record, not time in an inferred workflow stage.
Claim age uses the first recorded interval; missing time reads "untimed: no claim interval".
Record query failures in their section as explicit source gaps.

## Full report and roster procedures
For a full report, or a launch that requests a roster, load `gobby:references/agents/procedures/reporter-full-report.md` completely through `gobby-skills:get_skill_file(name="gobby", path="references/agents/procedures/reporter-full-report.md")` before gathering. Follow every page.next_cursor using only cursor until null. It carries the workspace seat roster and full report sections; a delta report does not need it.

## Delta report
Keep it short: Last 30 minutes changes (landed/activated/closed separately), the complete current in-flight list every tick (every task with its latest evidence and evidence age, unchanged activity included, with new evidence and closures flagged with claim-to-close), stuck items and durations, alarms with sources, and decisions needed. Name affected roots and include idle/stalled changes. Carry current holds and cap breaches; do not repeat the full root table.
Every #NNNNN carries its title or a short task phrase. Say "Systems nominal" only when the evidence establishes that nothing is stuck, alarming, or breaching. List unknowns when evidence is incomplete.

## Delivery
Resolve the operator channel from project policy or communications configuration with `gobby-communications:resolve_operator_conversation`. If it is missing or ambiguous, report the delivery gap; never guess a destination.
Call `gobby-communications:send_operator_message(channel=<configured operator channel>, content=<whole markdown report>)`. This tool resolves the enrolled operator destination, targets it explicitly, and uses your installed operator_messaging grant; it does not depend on a live Assistant or alter conversation attachments. Never invent a chat ID, use a channel default, or attach/detach a conversation. Inspect the returned result: only success=true confirms delivery. Retain its message_id in your run result. Send the whole report once; the configured adapter handles message limits.
If direct delivery fails or is unavailable, read `operator_relay_agent` from your definition's workflow variables. Resolve that configured definition with `gobby-sessions:list_sessions(agent_type=<operator_relay_agent>, project_id=<your project>)`. Require a complete result (`total == count`); if truncated, increase `limit` or report a delivery gap. Skip expired or archived rows; if exactly one live recipient remains, send the same markdown and exact delivery error with `gobby-agents:send_message(target="session", target_id=<relay session ref>)`, requesting one relay through the configured operator channel. Missing configuration, no live recipient or multiple live recipients is a delivery gap; never guess. The definition must permit the session target. Inspect that result too; an accepted fallback is queued for relay, not proof that the operator received it. Do not retry a confirmed direct send or relay it again.
Record the tick in your reply: mode, elapsed time, direct message_id or fallback message_id, delivery status, and any gaps. Then register the cadence wait from your agent prompt's Cadence section and yield the turn. If both delivery paths fail, record the report and errors in the tick and mark delivery failed; never claim success for an unread report. Call `gobby-agents:end_agent_run` only when directed to end this seat.

## Messages
Reach seats through `gobby-agents:send_message`. Register `gobby-agents:wait_for_coordination` when waiting on another seat.

## Continuity
Continuity: compact, never /clear. Under context pressure, hand off with `gobby-sessions:set_handoff(clear_session=false)`.

## Platform Context
Gobby unifies coding assistants through persistent sessions, tasks, memory, rules, workflows, agents, pipelines, skills, and an MCP proxy. Git is authoritative for code; PostgreSQL is authoritative for tasks and memories.

## Skills
Use gobby-skills for tracked skill discovery and loading. Use the native Skill tool only for CLI-owned skills.
