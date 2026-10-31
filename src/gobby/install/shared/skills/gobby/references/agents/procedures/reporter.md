# reporter procedures

## Cadence and budget
Read report cadence and mode selection from the injected project policy or launch inputs. An explicit `Report mode: full` or `Report mode: delta` in the launch prompt wins. Without a configured cadence, produce a full report for this invocation; never invent a recurring schedule.
Aim to finish in 300 seconds, well inside the 900-second timeout. Spend at most 240 seconds and 35 tool calls gathering, including discovery; next_offset continuation calls (including offloaded-result pages and projection --offset calls) and bounded queue refills described below are exempt from the 35-call cap, but remain inside the time budget. Page one list to completion before starting the next. In full mode, gather each root epic's work groups, complete seat snapshot, queue projection and complete candidate list first; fill every policy-defined root current/next-five/candidate block before gathering supplementary evidence. Reuse these rows for the other sections. Collect every required section before prose. State a count as exact only when accumulated rows read equal total; otherwise write "N of total read" and mark coverage incomplete. If the gathering budget expires, retain the full section scaffold, label unavailable evidence explicitly, deliver it, and mark the run result incomplete. Do not wait for seats unless an explicit hold or unavailable daemon prevents required reads, as described below. Never repeatedly poll or paginate through historical data.

Retry a failed daemon call or health check once after 30-60 seconds, within the 900-second timeout and reserving time for delivery. On a second failure, write "GAP: <source> - <error>" in that source's report slot. A restart or cutover heads-up alone does not pause work. If a daemon call fails during an announced restart, continue independent gathering. When daemon unavailability prevents the remaining required reads, register a bounded coordination wait on the announcing session and yield the turn before retrying; retain affected source gaps if the daemon remains unavailable. Honor an explicit hold for quiet hours or an operation that can kill panes. A retry or recovery wait may exceed the normal 240-second gathering budget, but never the 900-second run timeout or delivery reserve.

## Sources
Gather only workspace, session, task, agent, git, and Archivist root-queue state:
- Lease `gobby-agents:list_running_agents`, then request `view="compact", limit=500`. Its columns/rows include every seat's current claims (ref, title, first recorded claim time, task path), status, activity age, and worktree. Follow next_offset only for this current snapshot until null. Verify accumulated rows equal total; an unread second page is a coverage failure. An empty claims array means no current claim; a missing claim timestamp means unknown, never substitute run start or task updated_at. Map task path's epic to its root and combine that root's work groups. Spawn-time task_ref is insufficient for standing seats.
- Lease `gobby-tasks:list_tasks` and `gobby-tasks:list_blocked_tasks`. Use compact task lists: `parent_task_id=<each project_policy.roots epic>, closed=false, view="compact"` for each root's work groups, and `refs=[<each root epic>]` for the root epics; `claimed=true, closed=false, view="compact"` for claims absent from seats; `escalated=true, closed=false, view="compact"` for escalations; `closed=true, updated_since=<30-min cutoff>, view="compact"` for recent closures. Inspect closed_at and escalated_at to classify events. Use `list_blocked_tasks(view="compact")` for dependency blockers and resolve their titles. Follow each list's next_offset until null and verify accumulated rows equal total. Compact next_offset pages are bounded current data, not historical offload paging. For any counted list offloaded to a result_id, use `gobby-results:get_tool_result` through every next_offset until null; never sample it with search_tool_result.
- Read Archivist context through three exact section projections: `uv run python -m gobby.agents.reporter_sources --section queues`, then `--section fleet`, then `--section decisions`. Run projections bare; never use head, tail, timeout wrappers, pipes or byte slices, or read the whole digest into model context. Finish each section before starting the next, repeating the same section with `--offset <next_offset>` until next_offset is null. Decisions returns one entry per bullet. If one entry exceeds 14,000 bytes, record "GAP: <section> offset <N> - <error>" and continue the same section at offset N+1; that skipped entry makes coverage incomplete. These commands select the exact current table, omit cumulative Seats and work cells, expand task ranges, and retain latest coordination evidence. Today's digest is preferred; when absent, the projection selects the latest prior dated digest. Preserve source_date, stale and age_days, and label fallback evidence stale. Use modified_at as source age and each evidence timestamp; source order alone cannot prove chronology across headings. Missing sections are explicit source gaps. Do not read unrelated files. The digest's decisions section supplies links only; it never establishes which decisions remain open.
- Resolve next-five queues incrementally, not the whole digest's queue and evidence history. Combine work groups in projection order and deduplicate refs within each root, preserving Archivist queue order. Cache task rows by ref across all sources. Skip known closed or currently claimed refs from that cache. For each root still short of five eligible tasks, take at most five unresolved queue refs; batch those frontiers across roots with `gobby-tasks:list_tasks(refs=[...], view="compact", limit=40)`, at most 40 refs per request. Finish that batch's next_offset pages and cache every row. Advance each root's queue cursor past every attempted ref, including errors; retain those gaps without repeatedly requesting the failed ref. Then refill only roots still short. Stop resolving a root when five eligible tasks are found or its queue is exhausted; do not resolve its remaining queue or parked refs. These bounded refills are queue continuation, not new inventories. Do not add closed/claimed filters to a refs request: refs resolution ignores filters. Expand ranges into individual refs before selecting frontiers. Resolve supplementary evidence refs only when needed for a required report slot and absent from the cache, in batches of at most five with limit=5. A linked commit alone is not a candidate. Count only explicit CANDIDATE/landing/activation/close evidence, deduplicate still-open tasks, and exclude planning/operator-decision parks. Keep stale evidence distinct from current counts.
- Decode compact rows by their returned columns, including the state object. Use is_closed, is_claimed, is_blocked and is_escalated to describe the task's known state. Never print "state ?". For ref_errors, omitted rows or unreadable state, write "GAP: Root <epic ref> <task ref and known title, or title unavailable> - <source and error>" in that root's block. An unreadable queue ref could precede the next eligible task: retain its ordered gap and mark next-five coverage incomplete even if later refs fill five slots. Do not silently skip the failed ref or substitute a guessed state.
- `gobby-tasks:list_tasks(candidate=true, closed=false, view="compact")` enumerates explicit persisted CANDIDATE events across the project. Follow next_offset to completion and exclude planning/operator-decision parks to compute the close backlog versus the cap in the live project operating policy. If incomplete, report "N of total read" rather than an exact filtered backlog. Compact task rows include candidate_sent_at and receipts (kind, SHA, recorded_at, facts pointer), plus latest_linked_commit. Use these receipts for root candidate phases and last-30-minute landing/activation; fetch get_task only when receipt facts are needed to reconcile source and integration SHAs. An older approval or activation does not establish the latest rework's phase. Keep every open candidate in its root block. This replaces guesses from a partial digest ledger.
- Operator decisions come from `gobby-tasks:list_tasks(label="needs-decision", closed=false, view="compact")` and the open escalated list, restricted to items awaiting the operator. Finish both lists, deduplicate refs, and use escalated_at for decision age (otherwise the recorded opened time, or unknown). Closed or de-escalated decision parks count as resolved; remove them rather than carrying them from the digest. Use the digest only to attach an existing decision link to a currently open task. Missing links or opened times stay unknown.
- At report time, read `git rev-parse --short <integration branch from project policy>` and `git log -1 --format='%h %cI' <integration branch from project policy>` for the fresh integration tip and timestamp. Never take the tip from the digest or receipt facts. One bounded `git log <integration branch from project policy> --since="30 minutes ago" -n 50` supplies recent Git context; it does not enumerate all landing receipts. Last-30-minute landings come from landing receipt timestamps, each verified with `git merge-base --is-ancestor <landing SHA> <integration branch from project policy>`; failed or unavailable ancestry checks are explicit gaps. Git proves landing, never activation or closure. project roadmap progress and policy-designated critical paths come from task dependencies and root-queue evidence.
Do not read log files, scan ~/.gobby/logs, search transcripts, capture terminals, inspect processes, or run builds/tests. Never infer activation from landing, invent queue order, or turn a missing field into a healthy claim. Reuse the snapshot across sections. If the digest lacks a fact, report "unknown" with its source gap; do not request a new inventory from seats.

Before delivery, check this full-report scaffold has evidence or a specific source gap in every slot: every root defined by the injected project policy; Close backlog / policy cap; Hold state (restart/cutover, quiet hours); Blocked or escalated; Operator decisions; Stalled or idle; Worktrees versus developers; Workspace seat roster; Consolidator last review; project roadmap progress and policy-designated dependency paths; In-flight tasks (stage, time in stage, claim-to-close); Last 30 minutes landed / activated / closed / escalated. Each root must have epic, current claims with seat and time, next five, and candidates. Count every seat from the complete compact snapshot. Never replace several missing sections with a generic unknowns paragraph. Delivery success and report completeness are separate results.
If query_limit_reached=true, the seat query hit its 500-row ceiling: mark fleet coverage incomplete. A null next_offset alone does not prove coverage beyond that query limit. Flatten worktree arrays before comparing unique paths to developer seats; absent paths are missing evidence. Use task dependency records for policy-designated critical paths, and parent task queries for stage progress; identify any unresolved dependency ref+title explicitly. Include report_complete and seats_read/total in the run result.

## In-flight tasks: stage, time in stage, claim-to-close
Every full and delta report carries this section. It covers each open task under a policy root (`project_policy.roots`, matched by `path_cache` prefix) that is claimed or has a step record, plus each task closed in the report window.

Who runs each query. `query_readonly` is Assistant- and Orchestrator-only (`query-readonly-seat-scope`). The Assistant runs both queries below. A Reporter seat cannot run them: it writes `GAP: in-flight stage - query_readonly is Assistant-only` in this section's slot and sends the need to its Dev Manager or Triage with `gobby-agents:send_message`. Claim age per seat comes from the `list_running_agents` snapshot (first recorded claim time), so a Reporter still reports time claimed.

Sources until #25377 lands. Claim intervals are `task_claim_intervals` (`claimed_at`, `released_at`), written by a trigger on `tasks.claimed_by_session_id` and released when the task closes; claims made before that tracking have no interval. Step evidence is `task_comments` rows with `author_type = 'task_step'`: the JSON `body` carries `step` (test, implementation, review, closing), `status` (pending, in_progress, approved, rejected) and `sequence`, and the row's `created_at` is the record time. Test Writers hold no claim today, so the test-writing start comes from a step record, not a claim, and the section labels it that way.

Query 1, stage. Params, in order: the project id, the root path regex `^(<root path>|<root path>)\.`, where each root path is its epic's `path_cache` (for example `24818`), and the keyset cursor (`0` on the first call).
```sql
WITH in_flight AS (
  SELECT t.id, t.seq_num, t.title, t.review_epoch, t.commits ->> -1 AS tip
  FROM tasks t
  WHERE t.project_id = %s
    AND t.closed_at IS NULL
    AND t.path_cache ~ %s
    AND t.seq_num > %s
    AND (
      t.claimed_by_session_id IS NOT NULL
      OR EXISTS (SELECT 1 FROM task_comments c
                 WHERE c.task_id = t.id AND c.author_type = 'task_step')
    )
),
steps AS (
  SELECT s.task_id, s.created_at, s.step, s.status, s.commit_sha, s.seq
  FROM (
    SELECT f.id AS task_id, f.review_epoch, c.created_at,
           c.body::jsonb ->> 'step' AS step,
           c.body::jsonb ->> 'status' AS status,
           c.body::jsonb ->> 'commit_sha' AS commit_sha,
           (c.body::jsonb ->> 'sequence')::int AS seq,
           (c.body::jsonb ->> 'epoch')::int AS epoch,
           c.body::jsonb ->> 'supplemental_review' AS supplemental
    FROM in_flight f
    JOIN task_comments c ON c.task_id = f.id
    WHERE c.author_type = 'task_step'
  ) s
  WHERE s.epoch = s.review_epoch
    AND (s.step <> 'review' OR s.supplemental = 'false')
),
decisive AS (
  SELECT DISTINCT ON (task_id) task_id, step, status, commit_sha, created_at
  FROM steps
  WHERE status IN ('approved', 'rejected')
  ORDER BY task_id, seq DESC
),
first_step AS (
  SELECT DISTINCT ON (task_id) task_id, step, created_at
  FROM steps
  ORDER BY task_id, seq ASC
),
claims AS (
  SELECT ci.task_id, min(ci.claimed_at) AS first_claimed_at
  FROM task_claim_intervals ci
  JOIN in_flight f ON f.id = ci.task_id
  GROUP BY ci.task_id
)
SELECT f.seq_num, f.title,
       CASE
         WHEN d.step = 'test' AND d.status = 'rejected' THEN 'test-writing'
         WHEN d.step = 'test' AND d.status = 'approved' THEN 'development'
         WHEN d.step = 'implementation' AND d.status = 'approved' THEN 'review'
         WHEN d.step = 'implementation' AND d.status = 'rejected' THEN 'development'
         WHEN d.step = 'review' AND d.status = 'rejected' THEN 'development'
         WHEN d.step IN ('review', 'closing') AND d.status = 'approved'
              AND starts_with(d.commit_sha, f.tip) THEN 'waiting to land'
         WHEN d.step IS NULL AND fs.step = 'test' THEN 'test-writing'
         WHEN d.step IS NULL AND fs.step = 'implementation' THEN 'development'
         WHEN d.step IS NULL AND fs.step = 'review' THEN 'review'
         WHEN d.step IS NULL AND fs.step IS NULL THEN 'development'
         ELSE 'unknown'
       END AS stage,
       CASE WHEN d.task_id IS NOT NULL THEN 'decisive step record'
            WHEN fs.task_id IS NOT NULL THEN 'first step record'
            ELSE 'claim time' END AS stage_basis,
       COALESCE(d.created_at, fs.created_at, cl.first_claimed_at) AS stage_since,
       cl.first_claimed_at,
       count(*) OVER () AS total
FROM in_flight f
LEFT JOIN decisive d ON d.task_id = f.id
LEFT JOIN first_step fs ON fs.task_id = f.id
LEFT JOIN claims cl ON cl.task_id = f.id
ORDER BY f.seq_num
```

Query 2, claim-to-close. Params, in order: the project id, the window start as an ISO timestamp (now minus the report window), the root path regex, and the keyset cursor (`0` on the first call).
```sql
WITH closed AS (
  SELECT t.id, t.seq_num, t.title, t.closed_at
  FROM tasks t
  WHERE t.project_id = %s
    AND t.closed_at >= %s
    AND t.path_cache ~ %s
    AND t.seq_num > %s
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

Both queries run with `limit=2000`, the `query_readonly` maximum. Each row carries `total`, the count of matching rows after the cursor, computed in the same snapshot before the limit applies. The first page's `total` is the full count, so it is the authoritative total. Accumulate rows across pages, and call again with the cursor set to the last row's `seq_num` while accumulated rows are below that total. A zero-row first page means nothing is in flight or closed. Write "N of total read" for a section whose accumulated rows fall short of its total.

Stage. Only step rows of the task's current `review_epoch` count, and a review row counts only when its `supplemental_review` is `false`. The stage follows the decisive record: the latest approved or rejected step row by `seq`. Its start is that row's record time. A review or closing approval counts only on the task's latest linked commit (the last entry of `tasks.commits`); on any other commit it is `unknown`. The query's `stage` column applies this table.

| Decisive record | Stage | Stage start |
| --- | --- | --- |
| test rejected | test-writing | decisive_at |
| test approved | development | decisive_at |
| implementation approved | review | decisive_at |
| implementation rejected, or review rejected | development | decisive_at |
| review approved, or closing approved, on the latest linked commit | waiting to land | decisive_at |
| review approved, or closing approved, on any other commit | unknown | decisive_at |

With no decisive record, the stage is the first step's stage (`test` is test-writing, `implementation` is development, `review` is review), starting at `first_step_at`. With no step row, the stage is development, starting at `first_claimed_at`, labelled "from claim time, no step record". A task with neither a claim nor a step row is not in flight.

Time in stage is now minus the stage start. Claim age is now minus `first_claimed_at`. Claim-to-close is `closed_at` minus `first_claimed_at`; a null `first_claimed_at` reads "untimed: no claim interval". Label any stage whose start is untimed or inferred.

Format, grouped by root epic:
- `<task title or phrase> (#NNNNN): <stage>, <duration> in stage since <HH:MM CT>; claimed since <HH:MM CT>`
- Closed in window: `<task title or phrase> (#NNNNN): claim-to-close <duration>`

Write "In flight: N of total read" and "Closed in window: N of total read". A failed query writes `GAP: in-flight stage - <error>` or `GAP: closures claim-to-close - <error>` in that slot.

When #25377 lands, its timestamped claim hand-offs replace the step-record starts for test-writing and development. Update this section then.

## Full report and roster procedures
For a full report, or a launch that requests a roster, load `gobby:references/agents/procedures/reporter-full-report.md` completely through `gobby-skills:get_skill_file(name="gobby", path="references/agents/procedures/reporter-full-report.md")` before gathering. Follow every page.next_cursor using only cursor until null. It carries the workspace seat roster and full report sections; a delta report does not need it.

## Delta report
Keep it short: Last 30 minutes changes (landed/activated/closed separately), the complete current in-flight list every tick (every task with its stage and time in stage, unchanged stages included, with stage moves and closures flagged with claim-to-close), stuck items and durations, alarms with sources, and decisions needed. Name affected roots and include idle/stalled changes. Carry current holds and cap breaches; do not repeat the full root table.
Every #NNNNN carries its title or a short task phrase. Say "Systems nominal" only when the evidence establishes that nothing is stuck, alarming, or breaching. List unknowns when evidence is incomplete.

## Delivery
Resolve the operator channel from project policy or communications configuration with `gobby-communications:resolve_operator_conversation`. If it is missing or ambiguous, report the delivery gap; never guess a destination.
Call `gobby-communications:send_operator_message(channel=<configured operator channel>, content=<whole markdown report>)`. This tool resolves the enrolled operator destination, targets it explicitly, and uses your installed operator_messaging grant; it does not depend on a live Assistant or alter conversation attachments. Never invent a chat ID, use a channel default, or attach/detach a conversation. Inspect the returned result: only success=true confirms delivery. Retain its message_id in your run result. Send the whole report once; the configured adapter handles message limits.
If direct delivery fails or is unavailable, read `operator_relay_agent` from your definition's workflow variables. Resolve that configured definition with `gobby-sessions:list_sessions(agent_type=<operator_relay_agent>, project_id=<your project>)`. Require a complete result (`total == count`); if truncated, increase `limit` or report a delivery gap. Skip expired or archived rows; if exactly one live recipient remains, send the same markdown and exact delivery error with `gobby-agents:send_message(target="session", target_id=<relay session ref>)`, requesting one relay through the configured operator channel. Missing configuration, no live recipient or multiple live recipients is a delivery gap; never guess. The definition must permit the session target. Inspect that result too; an accepted fallback is queued for relay, not proof that the operator received it. Do not retry a confirmed direct send or relay it again.
End with `gobby-agents:end_agent_run`, passing current_state containing mode, elapsed time, direct message_id or fallback message_id, delivery status, and any gaps; next_steps names the next tick only when a configured cadence supplies it. If both delivery paths fail, return the report and errors as the run result and mark delivery failed; never claim success for an unread run result. Leave other optional handoff fields empty.

## Messages
Reach seats through `gobby-agents:send_message`. Register `gobby-agents:wait_for_coordination` when waiting on another seat.

## Continuity
Continuity: compact, never /clear. Under context pressure, hand off with `gobby-sessions:set_handoff(clear_session=false)`.

## Platform Context
Gobby unifies coding assistants through persistent sessions, tasks, memory, rules, workflows, agents, pipelines, skills, and an MCP proxy. Git is authoritative for code; PostgreSQL is authoritative for tasks and memories.

## Skills
Use gobby-skills for tracked skill discovery and loading. Use the native Skill tool only for CLI-owned skills.
