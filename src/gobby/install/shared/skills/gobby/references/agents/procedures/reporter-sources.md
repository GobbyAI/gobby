# reporter sources

## Sources
Gather only workspace, session, task, agent, git, and Archivist root-queue state:
- Lease `gobby-agents:list_running_agents`, then request `view="compact", limit=500`. Its columns/rows include every seat's current claims (ref, title, first recorded claim time, task path), status, activity age, and worktree. Follow next_offset only for this current snapshot until null. Verify accumulated rows equal total; an unread second page is a coverage failure. An empty claims array means no current claim; a missing claim timestamp means unknown, never substitute run start or task updated_at. Map task path's epic to its root and combine that root's work groups. Spawn-time task_ref is insufficient for standing seats.
- Lease `gobby-tasks:list_tasks` and `gobby-tasks:list_blocked_tasks`. Use compact task lists: `parent_task_id=<each project_policy.roots epic>, closed=false, view="compact"` for each root's work groups, and `refs=[<each root epic>]` for the root epics; `claimed=true, closed=false, view="compact"` for claims absent from seats; `escalated=true, closed=false, view="compact"` for escalations; `closed=true, updated_since=<30-min cutoff>, view="compact"` for recent closures. Inspect closed_at and escalated_at to classify events. Use `list_blocked_tasks(view="compact")` for dependency blockers and resolve their titles. Follow each list's next_offset until null and verify accumulated rows equal total. Compact next_offset pages are bounded current data, not historical offload paging. For any counted list offloaded to a result_id, use `gobby-results:get_tool_result` through every next_offset until null; never sample it with search_tool_result.
- Read Archivist context through the configured projections below. Pass the YAML mapping below inline: `uv run python -m gobby.agents.source_projection --spec '<YAML mapping below>' --section queues`, then use the identical `--spec` argument with `--section fleet`, then `--section decisions`. Do not write a specification file. Run projections bare; never use head, tail, timeout wrappers, pipes or byte slices, or read the whole digest into model context. Finish each section before starting the next, repeating the same section with `--offset <next_offset>` until next_offset is null. If one entry exceeds the page budget, record "GAP: <section> offset <N> - <error>" and continue at N+1; coverage is incomplete. Preserve source_date, stale and age_days; label fallback evidence stale. Use modified_at as source age and each evidence timestamp; source order alone cannot prove chronology across headings. Record missing headings and unread entries as source gaps. Digest prose supplies evidence and links only; verify current task state and open decisions through live tools.
- Resolve next-five queues incrementally, from the policy-selected queue evidence. Combine work groups in source order and deduplicate refs within each root, preserving configured queue order. Cache task rows by ref across all sources. Skip known closed or currently claimed refs from that cache. For each root still short of five eligible tasks, take at most five unresolved queue refs; batch those frontiers across roots with `gobby-tasks:list_tasks(refs=[...], view="compact", limit=40)`, at most 40 refs per request. Finish that batch's next_offset pages and cache every row. Advance each root's queue cursor past every attempted ref, including errors; retain those gaps without repeatedly requesting the failed ref. Then refill only roots still short. Stop resolving a root when five eligible tasks are found or its queue is exhausted; do not resolve its remaining queue or parked refs. These bounded refills are queue continuation, not new inventories. Do not add closed/claimed filters to a refs request: refs resolution ignores filters. Expand ranges into individual refs before selecting frontiers. Resolve supplementary evidence refs only when needed for a required report slot and absent from the cache, in batches of at most five with limit=5. A linked commit alone is not a candidate. Count only explicit CANDIDATE/landing/activation/close evidence, deduplicate still-open tasks, and exclude planning/operator-decision parks. Keep stale evidence distinct from current counts.
- Decode compact rows by their returned columns, including the state object. Use is_closed, is_claimed, is_blocked and is_escalated to describe the task's known state. Never print "state ?". For ref_errors, omitted rows or unreadable state, write "GAP: Root <epic ref> <task ref and known title, or title unavailable> - <source and error>" in that root's block. An unreadable queue ref could precede the next eligible task: retain its ordered gap and mark next-five coverage incomplete even if later refs fill five slots. Do not silently skip the failed ref or substitute a guessed state.
- `gobby-tasks:list_tasks(candidate=true, closed=false, view="compact")` enumerates explicit persisted CANDIDATE events across the project. Follow next_offset to completion and exclude planning/operator-decision parks to compute the close backlog. If incomplete, report "N of total read" rather than an exact filtered backlog. Compact task rows include candidate_sent_at and receipts (kind, SHA, recorded_at, facts pointer), plus latest_linked_commit. Use these receipts for root candidate phases and last-30-minute landing/activation; fetch get_task only when receipt facts are needed to reconcile source and integration SHAs. An older approval or activation does not establish the latest rework's phase. Keep every open candidate in its root block. This replaces guesses from a partial digest ledger.
- Operator decisions come from `gobby-tasks:list_tasks(label="needs-decision", closed=false, view="compact")` and the open escalated list, restricted to items awaiting the operator. Finish both lists, deduplicate refs, and use escalated_at for decision age (otherwise the recorded opened time, or unknown). Closed or de-escalated decision parks count as resolved; remove them rather than carrying them from the digest. Use the digest only to attach an existing decision link to a currently open task. Missing links or opened times stay unknown.
- At report time, read `git rev-parse --short <integration branch from project policy>` and `git log -1 --format='%h %cI' <integration branch from project policy>` for the fresh integration tip and timestamp. Never take the tip from the digest or receipt facts. One bounded `git log <integration branch from project policy> --since="30 minutes ago" -n 50` supplies recent Git context; it does not enumerate all landing receipts. Last-30-minute landings come from landing receipt timestamps, each verified with `git merge-base --is-ancestor <landing SHA> <integration branch from project policy>`; failed or unavailable ancestry checks are explicit gaps. Git proves landing, never activation or closure. project roadmap progress and policy-designated critical paths come from task dependencies and root-queue evidence.
Do not read log files, scan ~/.gobby/logs, search transcripts, capture terminals, inspect processes, or run builds/tests. Never infer activation from landing, invent queue order, or turn a missing field into a healthy claim. Reuse the snapshot across sections. If the digest lacks a fact, report "unknown" with its source gap; do not request a new inventory from seats.

Before delivery, check this full-report scaffold has evidence or a specific source gap in every slot: every root defined by the injected project policy; Close backlog; Hold state (restart/cutover, quiet hours); Blocked or escalated; Operator decisions; Stalled or idle; Worktrees versus developers; Workspace seat roster; Consolidator last review; project roadmap progress and policy-designated dependency paths; In-flight tasks (latest evidence, evidence age, claim-to-close); Last 30 minutes landed / activated / closed / escalated. Each root must have epic, current claims with seat and time, next five, and candidates. Count every seat from the complete compact snapshot. Never replace several missing sections with a generic unknowns paragraph. Delivery success and report completeness are separate results.
If query_limit_reached=true, the seat query hit its 500-row ceiling: mark fleet coverage incomplete. A null next_offset alone does not prove coverage beyond that query limit. Flatten worktree arrays before comparing unique paths to developer seats; absent paths are missing evidence. Use task dependency records for policy-designated critical paths, and parent task queries for stage progress; identify any unresolved dependency ref+title explicitly. Include report_complete and seats_read/total in the run result.

## Projection specification

This runbook supplies the document policy. Other runbooks provide their own YAML;
the projection command has no built-in headings, seat names, paths or topics.

```yaml
source_glob: .gobby/reports/gobby-digest-*.md
date_pattern: 'gobby-digest-(\d{4}-\d{2}-\d{2})'
timezone: America/Chicago
sections:
  queues:
    mode: table
    headings: [Epic queues — current]
    columns: {queue: 0, epic: 1, worktree: 2, queue_refs: 4, parked_refs: 5}
    ref_columns: [queue_refs, parked_refs]
    row_pattern: '^[1-8](?:[A-Z]|\b)'
  fleet:
    mode: topics
    heading_prefixes: [Orchestrator updates —, Terminal and daemon status —]
    trim_marker: 'Earlier:'
    last: 2
    topics:
      backlog: 'close backlog|backlog.*?/10'
      holds: 'drain|hold|DAEMON BACK|restart.*?retimed'
      consolidator: 'Consolidator|consolidation.*?review'
      roadmap: 'ROADMAP|Story B|critical path'
      activation: 'activat|DAEMON BACK'
      candidates: 'CANDIDATE|waiting.*?(?:landing|activation|close)'
  decisions:
    mode: bullets
    headings: [Decisions needed (Josh), Operator actions (Josh), Gaps and contradictions]
```

Queue entries are under `evidence.entries`; topic lists retain their configured
keys. `evidence.gaps` contains missing headings or malformed rows and must be
reported. Configuration and all sources stay in the project/workspace; never
substitute unrelated files for missing evidence.

_Version: 2.0.2_

_Last verified: 2026-11-02_
