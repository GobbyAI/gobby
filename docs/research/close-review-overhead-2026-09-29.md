# Task-close reviewer and spawn overhead against standing seats

Task: #23114. Researcher gobby#14550, 2026-09-29.

Question: roughly 20 standing interactive seats run at once, yet task-close reviews are
held to one slot per project. Does a reviewer cost enough to justify that, or is the
single slot policy?

This is a read-only study. It spawned no workers and ran no load tests. Every number
below comes from the live hub database, `~/.gobby/logs/daemon.log{,.1}`, the reviewer's
Codex rollout, or its SRT violation log. Each finding is marked VERIFIED (read from
those sources) or INFERRED (reasoned from them).

## Answer

The single close slot is coordination policy. Measured cost does not force it.

- A reviewer is one more agent process. Its launch costs about 3 s of daemon time.
  After that, its per-call hook, proxy and DB cost is the same as or lower than a
  standing seat's.
- Over 2026-09-29 00:30 to 2026-09-30 00:30 UTC, the DB pool had no waiter.
  Per-bucket `rule_eval` p95 stayed at or below 10 ms, with up to 26 active sessions
  and up to 5 reviewer sessions in a 10-minute bucket (§5).
- In the 16 h after serialization, the one slot was busy 12.9% of the window (§6).

The measurements do not show reviewer load limiting close throughput. After
serialization, reviews fell from 6.31 to 2.13 per hour. Fleet activity also fell 41%
over the same windows. How much of the drop is the admission process and how much is
lower activity is unmeasured (§6).

Least mechanism justified by these measurements: restore a per-project reviewer cap
above 1, admitted under Josh's 5-minute load-below-24 rule. Lane2 already owns the
#23059 policy change; this study supplies evidence for it and duplicates none of it.

## Sources and versions

| Item | Value |
| --- | --- |
| Checkout | `0.5.0` at `cd5749ca41`. Serialization landed in `ef4668dc77` (2026-09-29 08:34 UTC). |
| Reviewer run | `5b9be15f-4437-4e1b-8ee8-914ec8d2228f` for #23107. Codex CLI 0.159.0, model `gpt-6.1-sol`, effort xhigh, SRT sandbox, tmux. Child session `bbfe4275` (gobby#14886). Parent: PD session `1fdb7576`. |
| Standing seats in that window | 10 `claude-opus-5-5` (Claude Code), 7 `gpt-6.1-sol`, 2 `deepseek/deepseek-v4.1-flash`, 1 `gpt-5.6-terra`, 1 `gpt-6-luna` (Codex). Total 21. |
| Other spawned runs in that window | 2 `gpt-6.1-sol` and 1 `gpt-5.6-sol` (Codex). The reviewer is one of them. |
| Latency source | `metrics_events` rows: `tool_call` is proxy MCP call latency; `rule_eval` is rule-engine hook latency. Snapshots come from `metric_snapshots`. |
| Launch source | `Spawn phase timings` log lines from `agents/spawn_executor.execute_spawn`. 1,551 spawns, from 2026-09-13 16:57 local time (the start of `daemon.log.1`) to 2026-09-29 19:30 local time (2026-09-30 00:30 UTC). |
| Observation boundaries | All windows are fixed and half-open, in UTC. §5 covers [2026-09-29 00:30, 2026-09-30 00:30). §6 compares [2026-09-28 16:34:06, 2026-09-29 08:34:06) with [2026-09-29 08:34:06, 2026-09-30 00:34:06), 16 h each on either side of `ef4668dc77`. §3's post-serialization table uses the §6 "after" window. The 7-day figures cover [2026-09-23 00:30, 2026-09-30 00:30). |

Model mix is a confound. Most reviewers before 2026-09-29 were `gpt-5.6-terra`. The
natural run studied here is `gpt-6.1-sol`. §3 gives the per-model medians as descriptive
figures only.

## 1. Launch cost (VERIFIED)

Timeline for reviewer `5b9be15f`:

| Phase | Time |
| --- | --- |
| Queue row created | 23:50:06.17 |
| Promoted, `started_at` | 23:50:09.56 (+3.4 s) |
| Daemon spawn phases (sum) | 2.78 s |
| First Codex rollout event | 23:50:16.84 (+10.7 s) |
| First tool call | +17.7 s after queue |
| Self-terminated | 23:58:08.19 (7m58s after `started_at`) |

The daemon spawn phases in ms:

- `_preflight_srt` 1,444
- `verify_srt_installation` 736
- `prepare_sandbox_run_paths` 419
- `prepare_terminal_spawn` 74
- `provider_post_sandbox` 54
- `compute_sandbox_paths` 37
- `runtime_prepare_spawn` 12
- code index 0

SRT verify plus preflight is 2.18 s, or 78% of this launch. Across all 1,551 retained
spawns, the total is p50 5.7 s and p90 15.4 s. `verify_srt_installation` is p50 1.45 s
and p90 5.7 s; `_preflight_srt` is p50 1.3 s and p90 3.3 s. That confirms the #22729
premise: SRT verification repeats on every spawn and is the largest launch component.
Caching it saves about 2-7 s per spawn. That is under 2% of this reviewer's wall time,
so it matters for spawn-heavy fanout and does not explain close throughput.

## 2. SRT denials (VERIFIED, impact INFERRED)

The retained log
`~/.gobby/logs/sandbox-violations/5b9be15f-4437-4e1b-8ee8-914ec8d2228f.jsonl` has 102
denied operations. All of them fall between 23:50:09.18 and 23:50:14.18, which is the
first 5 s of launch. They are process start-up probes:

| Operation | Count | Processes |
| --- | --- | --- |
| `system-info vfs.disk-space` | 73 | Python, codex, git, uv, zsh, bash |
| `sysctl-read kern.iossupportversion` | 21 | bash, node, git, id, uv, sed, tr, wc, file, Python |
| `system-info net.link.addr` | 3 | codex |
| `mach-lookup com.apple.SystemConfiguration.configd` | 2 | codex |
| `network-outbound` (no target recorded) | 2 | codex |

The count of 102 is a capture ceiling (VERIFIED):

- SRT 0.0.76's `SandboxViolationStore` is a 100-entry ring buffer (`maxSize = 100`,
  `slice(-100)`). It keeps a separate `totalCount`.
- `src/gobby/agents/srt_runner.mjs:33-42,76-78` appends `violations.slice(seen)` and
  stores `violations.length` as the new `seen`. Once the buffer is full, the length
  stays at 100, so nothing else is ever written.
- 102 is the 2-line `node --version` preflight plus 100 main-run lines.
- 17 of the 40 newest violation logs have exactly 102 lines and 3 have 101. Each one
  ends 4-11 s after launch, including 5-minute Claude runs and this 8-minute reviewer.

As a result, denials during review work are unobservable. The first 100 are
start-up probes and add no measurable launch cost; the run succeeded with a valid
verdict. Any denials after them are unknown until the runner tracks `totalCount`.
The two `network-outbound` denials have no recorded target.

## 3. Where reviewer wall time goes (VERIFIED)

The rollout covers 470.5 s of Codex session time. Its 42 tool cells took 79.9 s of
tool wall time; the remaining ~390 s (83%) is model inference and generation.

Tool time breakdown:

- One `gobby-sessions:search_session_messages` call took 59.8 s (daemon metric). The
  reviewer made it while cross-checking inter-session messages, which the review
  contract does not require.
- `end_agent_run` took 6.1 s daemon-side. `submit_close_review` took 2.9 s.
- The other MCP calls: `get_task_diff` ×11 (avg 130 ms), skills, results and sessions
  calls under 220 ms each.
- Shell work (`ocr delegate preview`, `gcode grep`, `gcode symbol-at`): about 10 s
  total.

Successful reviewer runs created in the §6 "after" window, by model:

| Provider / model | Runs | p50 | p90 | Mean tool calls |
| --- | --- | --- | --- | --- |
| codex `gpt-5.6-terra` | 29 | 184 s | 265 s | 35 |
| codex `gpt-6.1-sol` | 4 | 613 s | 779 s | 49 |
| claude `sonnet` | 1 | 72 s | 72 s | 22 |

In the 7-day window, 446 `gpt-5.6-terra` reviewer runs succeeded with a mean of 265 s.
These medians describe what happened and are not a model comparison. The
`gpt-6.1-sol` sample is 4 runs, and the two models reviewed different task mixes and
diff sizes. Any conclusion about model speed needs a like-for-like sample.

## 4. Per-call cost: reviewer against standing seats (VERIFIED)

In the reviewer's own window (23:50:00 to 23:58:10), from `tool_call` metrics:

| Population | Calls | p50 | p95 | Sessions |
| --- | --- | --- | --- | --- |
| Reviewer | 39 | 15 ms | 487 ms | 1 |
| All other sessions | 128 | 50 ms | 2,243 ms | 15 |

The reviewer's maximum was the 59.8 s search in §3. Excluding it, the reviewer's calls
are cheaper than the seats' calls, because they are mostly short `get_task_diff` and
skill reads.

## 5. Concurrency against hook and DB latency (VERIFIED)

Window: [2026-09-29 00:30, 2026-09-30 00:30) UTC, in 130 buckets of 10 minutes. Active
sessions in a bucket are the distinct sessions with a `tool_call` or `rule_eval` event
in it. Standing seats are sessions without an `agent_run_id`. Reviewers are sessions
whose run is `task-close-reviewer`. Workers are all other spawned runs. The same rule
counts every population, across every project. Per-bucket statistics are computed from
the raw events in the bucket.

How to read the table:

- "Bucket p95 median / max" is the median and the maximum, across the buckets in a row,
  of each bucket's own p95.
- "tool_call p50" is the median of the bucket p50s.
- "CPU %" is the median of the bucket means of `daemon_cpu_percent`.
- "Pool waiting" and "executor queue age" are maxima of `metric_snapshots` gauges.
- A dash means no `rule_eval` events fell in those buckets.

None of these values is a pooled percentile over all the events in a row.

| Active sessions | Buckets | rule_eval bucket p95, median / max | tool_call bucket p50, median | tool_call bucket p95, median / max | CPU % | Pool waiting, max | Executor queue age, max |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 0-4 | 8 | - / - | 99 ms | 305 / 2,290 ms | 8 | 0 | 0 s |
| 5-9 | 27 | 2.1 / 6.2 ms | 23 ms | 2,140 / 2,552 ms | 14 | 0 | 0 s |
| 10-14 | 36 | 2.2 / 10.0 ms | 23 ms | 2,202 / 3,403 ms | 26 | 0 | 0 s |
| 15-19 | 35 | 2.5 / 6.3 ms | 27 ms | 2,385 / 4,021 ms | 34 | 0 | 0.22 s |
| 20-24 | 22 | 2.2 / 6.7 ms | 34 ms | 2,255 / 4,894 ms | 38 | 0 | 0.15 s |
| 25-29 | 2 | 4.1 / 4.3 ms | 42 ms | 2,564 / 2,905 ms | 38 | 0 | 0 s |

By reviewer sessions active in the bucket (same statistics):

| Reviewers | Buckets | rule_eval bucket p95, median / max | tool_call bucket p50, median | tool_call bucket p95, median / max | CPU % |
| --- | --- | --- | --- | --- | --- |
| 0 | 63 | 2.2 / 10.0 ms | 25 ms | 2,157 / 4,021 ms | 19 |
| 1 | 38 | 2.5 / 6.3 ms | 27 ms | 2,232 / 3,949 ms | 29 |
| 2 | 20 | 2.2 / 5.2 ms | 30 ms | 2,612 / 4,894 ms | 35 |
| 3 | 7 | 2.3 / 3.5 ms | 25 ms | 2,619 / 3,403 ms | 32 |
| 4-5 | 2 | 2.5 / 3.1 ms | 27 ms | 3,077 / 3,926 ms | 40 |

Standing seats per bucket: median 13, maximum 23. Maximum total active sessions: 26.

Findings:

- The hook path did not saturate. Across the 111 buckets with `rule_eval` events, the
  bucket p95 ranged 0-10.0 ms (median 2.3 ms, 90th percentile 4.2 ms). The single
  10.0 ms bucket had 10-14 active sessions and no reviewer.
- #22729 measured `rule_eval` p95 at 197.6 ms with 15 active sessions on 2026-09-22.
  No bucket in this window comes near it. INFERRED: the stability work since then
  moved the knee. This is not a controlled comparison, and #22729 may have computed
  its percentile differently.
- The DB pool never had a waiter. The executor queue age peaked at 0.22 s.
- Daemon CPU grows with active sessions (8% to 38% median), and the median bucket
  `tool_call` p50 rises from 23 ms to 42 ms between 5-9 and 25-29 active sessions.
  The 0-4 row is higher (99 ms) on only 8 buckets. This is the only measurable trend,
  and it is not specific to reviewers.
- Bucket `tool_call` p95 is dominated by long-running tools such as spawns and
  searches, so it tracks the tool mix more than daemon pressure.

Counting caveat: reviewers that run one after another can share a 10-minute bucket, and
the metrics cover every project. The 4-5 reviewer buckets therefore do not mean 4-5
concurrent reviewers in this project.

## 6. Admission policy (VERIFIED)

`ef4668dc77` (#23059) did the following:

- Removed `close_review_max_concurrency_per_project` (default 3) from
  `src/gobby/config/tasks.py`.
- Hardcoded `claim_queued(project_id=..., max_concurrency=1)` in
  `_lifecycle_close_orchestration.py:482-485`.
- Added `TaskCloseReviewBusyError`, which rejects admission for a second task under a
  `FOR UPDATE` lock on the project row.
- Added a role rule in `.gobby/roles/_common.md`: lanes wait for an explicit Lane
  Manager release before `close_task(preview=false)`.

Close reviews in project gobby, in two equal 16 h windows either side of the change:

| Measure | Before: [09-28 16:34:06, 09-29 08:34:06) | After: [09-29 08:34:06, 09-30 00:34:06) |
| --- | --- | --- |
| Reviews created (all launched and finished) | 101 | 34 |
| Reviews per hour (count / 16 h) | 6.31 | 2.13 |
| Reviewer-seconds per window-second (review runtime clipped to the window / 57,600 s) | 0.359 | 0.129 |
| Review run p50 | 202 s | 189 s |
| Queue wait, max | 42 s | 0 s |
| Project `tool_call` events | 15,983 | 9,377 |
| Distinct sessions with events | 134 | 71 |
| `close_task` calls | 256 | 88 |
| Reviews per 1,000 `tool_call` events | 6.3 | 3.6 |

The "after" window closes at 00:34:06, after the last review it contains had finished,
so no review is censored. Reviewer-seconds per window-second is capacity-neutral. Before
the change the cap was 3, and 0.359 means about 36% of one slot's worth of time. After
the change it is the busy fraction of the single slot.

- Queue wait is now zero because contention no longer reaches the queue. A second
  caller is rejected with `close_review_busy`, or waits for Lane Manager release, before
  any row exists. That wait is not persisted.
- Under the old default of 3, peak overlap in the 7-day window was 3 reviews.
- The single slot was idle 87.1% of the "after" window.
- Fleet activity fell 41% (by `tool_call` events) between the windows, and the review
  rate fell 66%. Normalized per 1,000 `tool_call` events, reviews fell 43%.
- INFERRED: part of the drop tracks lower activity. The remainder is consistent with
  the admission process (Lane Manager release plus busy rejection), but release waits
  are not persisted, so that attribution is not measured. Slot capacity was not
  binding: the slot was idle most of the window.

## 7. Hypothesis ledger

| Hypothesis | Verdict |
| --- | --- |
| Reviewers cost more per call than standing seats. | Rejected. §4. |
| Reviewer concurrency saturates hooks or the DB. | Rejected for the §5 window. Bucket `rule_eval` p95 stayed at or below 10 ms and the pool had no waiter, up to 26 active sessions. §5. |
| #22729: SRT verification dominates launch. | Supported: 78% of this launch, p50 about 2.8 s across the fleet. It is a small share of reviewer wall time. |
| #22629: all-seat counting must include reviewers. | Supported as an accounting rule. Reviewers were exempt from the per-project cap. §5 shows reviewers add a load increment comparable to a seat, so count them like seats, with no separate penalty. |
| The single slot is justified by measured load. | Unsupported. §5 and §6. |
| Reviewer model choice drives close latency. | Unsupported by this data. The medians differ (746 s against 184 s), but the task mixes are unequal and the `gpt-6.1-sol` sample is 3 runs. §3. |
| SRT denials slow reviews. | Unknown. The log captures only the first 100 main-run denials, all of them start-up probes. §2. |

## 8. Recommendation

1. Restore a per-project reviewer cap above 1, admitted when the 5-minute load average
   is below 24. Count reviewers in the all-seat total the same way standing seats are
   counted. Persist Lane Manager release waits so the admission process's share of the
   throughput drop (§6) can be measured before that step is changed. Lane2 owns the cap
   under #23059.
2. #22729 SRT verification caching stays worthwhile for spawn-heavy fanout. It does not
   change close throughput.
3. Reviewer model speed: no recommendation. §3 records the per-model medians without
   comparing them.

## 9. Found work

1. SRT violation capture stops after 100 main-run records (§2). Filed as #23118 and
   delegated to L5 #14768 alongside #22729.
2. `gobby-sessions:search_session_messages`
   (`src/gobby/mcp_proxy/tools/sessions/_messages.py:101-190`) scans rendered
   transcript windows linearly. In the 7-day window: 56 calls, p50 14.4 s, p90 200 s,
   max 1,397 s. A miss reads every window of every candidate session. Filed as #23117
   (bounded transcript search) and delegated to L2 #14828 after #23113.

## 10. Unknowns

- Host load average is not retained. `metric_snapshots` holds daemon CPU and pool
  gauges only. Josh's load-below-24 rule cannot be checked against history.
- How long lanes wait for Lane Manager release is not persisted, so the real admission
  delay since `ef4668dc77` is unmeasured.
- Only one natural `gpt-6.1-sol` reviewer was studied in depth. The per-model table
  (§3) comes from agent_run rows, not rollouts.
- Subprocess-heavy work a reviewer might trigger (cargo, full pytest) did not occur in
  this run. Reviewers use transcript-derived command facts and do not re-run suites
  (memory 94c5736d).
- The targets of the 2 `network-outbound` SRT denials were not recorded.
- SRT denials after the first 100 of any run are not captured (§2).
