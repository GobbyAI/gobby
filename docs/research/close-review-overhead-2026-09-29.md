# Task-close reviewer and spawn overhead against standing seats

Task: #23114 (reviewer and spawn overhead). Researcher gobby#14550, 2026-09-29;
evidence-scope correction by Reviewer 4 gobby#14681, 2026-09-30.

Question: roughly 20 standing interactive seats run at once, yet task-close reviews are
held to one slot per project. Does a reviewer cost enough to justify that, or is the
single slot policy?

This is a read-only study. It spawned no workers and ran no load tests. Every number
below was reported from the live hub database, `~/.gobby/logs/daemon.log{,.1}`, the
reviewer's Codex rollout, or its SRT violation log. VERIFIED identifies the original
source observation; INFERRED identifies interpretation. The correction preserves
those values without a new fleet capture. Missing retained receipts and measurement
boundaries are identified as UNKNOWN below.

## Answer

The single close slot is explicit coordination policy (§6). The retained
measurements do not establish whether whole-hook latency or host load requires it.

- A reviewer is one more agent process. This natural run's daemon spawn phases
  took about 3 s. Its observed proxy-call percentiles were lower than the other
  sessions' (§4), with different tool mixes. Relative whole-hook and per-call DB
  cost were not measured.
- Over 2026-09-29 00:30 to 2026-09-30 00:30 UTC, retained DB pool gauges showed no
  waiter. Per-bucket blocked-rule `rule_eval` p95 stayed at or below 10 ms, with
  up to 26 active sessions and up to 5 reviewer sessions in a 10-minute bucket.
  Allowed-rule and whole-hook latency are absent from this population (§5).
- The original report gives 12.9% recorded slot occupancy in the 16 h after
  serialization. Its runtime query receipts are not retained, and the §3 and §6
  population/runtime definitions cannot be independently confirmed (§6).

The sampled proxy and pool observations show no reviewer-specific bottleneck;
whole-hook saturation and safe reviewer capacity remain unknown. After
serialization, reviews fell from 6.31 to 2.13 per hour. Fleet activity also fell 41%
over the same windows. How much of the drop is the admission process and how much is
lower activity is unmeasured (§6).

Least mechanism justified by these measurements: retain consistent seat counting
and record admission waits before attributing the throughput drop. Lane2 owns
#23059 (close-review admission policy) and can evaluate a cap above 1 under PD
sequencing and Josh's 5-minute load-below-24 rule. This study does not establish a
safe cap or justify changing admission from blocked-rule percentiles alone.

## Sources and versions

| Item | Value |
| --- | --- |
| Checkout | `0.5.0` at `cd5749ca41`. Serialization landed in `ef4668dc77` (2026-09-29 08:34 UTC). |
| Reviewer run | `5b9be15f-4437-4e1b-8ee8-914ec8d2228f` for #23107 (natural close-review task). Codex CLI 0.159.0, model `gpt-6.1-sol`, effort xhigh, SRT sandbox, tmux. Child session `bbfe4275` (gobby#14886). Parent: PD session `1fdb7576`. |
| Standing seats in that window | 10 `claude-opus-5-5` (Claude Code), 7 `gpt-6.1-sol`, 2 `deepseek/deepseek-v4.1-flash`, 1 `gpt-5.6-terra`, 1 `gpt-6-luna` (Codex). Total 21. |
| Other spawned runs in that window | 2 `gpt-6.1-sol` and 1 `gpt-5.6-sol` (Codex). The reviewer is one of them. |
| Latency source | `metrics_events` rows: `tool_call` is proxy MCP call latency; `rule_eval` is blocked-rule effect-path latency, excluding rule-level condition/context setup. It is not whole-hook latency. Snapshots come from `metric_snapshots`. |
| Launch source | `Spawn phase timings` log lines from `agents/spawn_executor.execute_spawn`. 1,551 spawns, from 2026-09-13 16:57 local time (the start of `daemon.log.1`) to 2026-09-29 19:30 local time (2026-09-30 00:30 UTC). |
| Observation boundaries | All windows are fixed and half-open, in UTC. §5 covers [2026-09-29 00:30, 2026-09-30 00:30). §6 compares [2026-09-28 16:34:06, 2026-09-29 08:34:06) with [2026-09-29 08:34:06, 2026-09-30 00:34:06), 16 h each on either side of `ef4668dc77`. §3's post-serialization table uses the §6 "after" window. The 7-day figures cover [2026-09-23 00:30, 2026-09-30 00:30). |

Model mix is a confound. Most reviewers before 2026-09-29 were `gpt-5.6-terra`. The
natural run studied here is `gpt-6.1-sol`. §3 gives the per-model medians as descriptive
figures only.

At the cited checkout `cd5749ca417d7c2987542172d30b48f653379614`,
`src/gobby/workflows/engine/evaluation.py:593-678,770-782` starts each rule timer
after rule-level condition/context setup and writes `metrics_events.rule_eval`
only when a rule blocks, including blocking lookahead. The timer measures that
rule's effect path, not the full rule loop, hook prelude, hook-entry queue wait or
transport. `src/gobby/telemetry/rule_allow_audit.py:206-228` records matched allow
outcomes in process metrics and the separate allow audit, not this PostgreSQL
history (memory `44831b84-a775-5cbe-bbdf-b0ce731bf5f9`). Neither allow-audit timings
nor full-hook phase timings were included in this study.

The recovered checkpoint retains the report and its described query boundaries,
but no underlying cohort/runtime query receipt. Those descriptions are retained
methodology statements, not fresh verification. In particular, the §3 after-window
model medians and §6 after-window overall median do not establish their population
or runtime definitions. No distinct boundary is assumed to explain the summaries.

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
SRT verification premise: verification repeats on every spawn and is the largest
launch component. The estimated caching opportunity is about 2-7 s per spawn, under
2% of this reviewer's wall time. Its effect on close throughput was not measured.

## 2. SRT denials (VERIFIED, impact INFERRED)

The retained log
`~/.gobby/logs/sandbox-violations/5b9be15f-4437-4e1b-8ee8-914ec8d2228f.jsonl` has 102
denied operations. All of them fall between 23:50:09.18 and 23:50:14.18, which is the
first 5 s of launch. They are process start-up probes:

| Operation | Count | Processes |
| --- | --- | --- |
| `system-info vfs.disk-space` | 73 | Python, codex, git, uv, zsh, bash |
| `sysctl-read kern.iossupportversion` | 22 | bash, node, git, id, uv, sed, tr, wc, file, Python |
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
start-up probes; no denial-attributed launch cost was measured. The run succeeded
with a valid verdict. Any denials after them are unknown until the runner tracks `totalCount`.
The two `network-outbound` denials have no recorded target.

## 3. Where reviewer wall time goes (retained observations; cohort comparison UNKNOWN)

The rollout covers 470.5 s of Codex session time. Its 42 tool cells took 79.9 s of
tool wall time; the remaining ~390 s (83%) is time outside those tool cells. The
original report attributed it to model inference and generation; no retained phase
receipt separates that work from other waiting time.

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
| codex `gpt-5.6-terra` | 29 | 200 s | 287 s | 34 |
| codex `gpt-6.1-sol` | 4 | 613 s | 779 s | 49 |
| claude `sonnet` | 1 | 72 s | 72 s | 22 |

In the 7-day window, 446 `gpt-5.6-terra` reviewer runs succeeded with a mean of 265 s.
These medians describe what happened and are not a model comparison. The
`gpt-6.1-sol` sample is 4 runs, and the two models reviewed different task mixes and
diff sizes. Any conclusion about model speed needs a like-for-like sample.

The original report describes this table as project-scoped to gobby, using completed
reviews created in the fixed §6 "after" window, with runtime
`agent_runs.completed_at - agent_runs.started_at` and tool-call means from those
same runs (terra 976/29, sol 197/4, sonnet 22/1), rounded to whole calls. It describes
all-project figures and the 7-day mean as separate cohorts. The query receipts are
not retained. This table totals 34 runs, also the §6 after count; §6 reports an
overall p50 of 189 s and this table a 29-run terra p50 of 200 s. Per-model and pooled
medians can differ, so those values alone do not prove an inconsistency. The exact
shared population/runtime definition is UNKNOWN without the query receipts. Keep
these historical figures as reported; use neither summary as a verified comparison
across sections.

## 4. Proxy-call latency: reviewer against other sessions (VERIFIED observation)

In the reviewer's own window (23:50:00 to 23:58:10), from `tool_call` metrics:

| Population | Calls | p50 | p95 | Sessions |
| --- | --- | --- | --- | --- |
| Reviewer | 39 | 15 ms | 487 ms | 1 |
| All other sessions | 128 | 50 ms | 2,243 ms | 15 |

The reviewer's maximum was the 59.8 s search in §3. Its observed proxy-call
percentiles are lower than the other sessions', and its calls are mostly short
`get_task_diff` and skill reads. This is a descriptive comparison of different tool
mixes, not a matched per-call resource-cost comparison. It does not measure relative
whole-hook latency, DB work or marginal reviewer load.

## 5. Activity against blocked-rule/proxy latency and DB gauges (VERIFIED observations)

Window: [2026-09-29 00:30, 2026-09-30 00:30) UTC, containing 144 possible intervals
of 10 minutes. The tables summarize the 130 intervals with a `tool_call` or
`rule_eval` event; 14 intervals have neither event and are omitted. Active sessions
in an observed bucket are the distinct sessions with one of those events in it.
Standing seats are sessions without an `agent_run_id`. Reviewers are sessions
whose run is `task-close-reviewer`. Workers are all other spawned runs. The same rule
counts every population, across every project. Per-bucket statistics are computed from
the raw events in the bucket.

The original report cites `buckets_fixed.sql` / `buckets_fixed.txt` as authority for
the table values. It states that their session join excludes the window's two
`skill_invoke` events with null session ids, and that an explicit `tool_call` /
`rule_eval` filter in `buckets_fixed2.sql` reproduces the same 130 session/call/latency
rows. These query receipts are not available in the recovered checkpoint; no query
was rerun for this correction. Original snapshot-derived CPU, pool and executor
columns are preserved because the earliest snapshots have since expired; they are
not recomputed from partial data.

How to read the table:

- "Bucket p95 median / max" is the median and the maximum, across the buckets in a row,
  of each bucket's own p95.
- "tool_call p50" is the median of the bucket p50s.
- "CPU %" is the median of the bucket means of `daemon_cpu_percent`.
- "Pool waiting" and "executor queue age" are maxima of `metric_snapshots` gauges.
- A dash means no `rule_eval` events fell in those buckets.
- Every `rule_eval` percentile below covers blocked rules only, with the timer
  exclusions described under Sources and versions. Allowed-rule and whole-hook
  latency are unmeasured.

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

- Across the 111 buckets with blocked-rule `rule_eval` events, the bucket p95 ranged
  0-10.0 ms (median 2.3 ms, 90th percentile 4.2 ms). The single 10.0 ms bucket had
  10-14 active sessions and no reviewer. These values do not establish absence of
  whole-hook or allowed-path saturation.
- #22729 (SRT verification and hook-latency evidence) reported `rule_eval` p95 at
  197.6 ms with 15 active sessions on 2026-09-22. No bucket here approaches that
  reported value. Whether stability work changed a saturation threshold is UNKNOWN:
  this is not a controlled comparison, the percentile definitions may differ, and
  neither figure establishes whole-hook latency.
- Retained DB pool gauges showed no waiter. The observed executor queue age peaked
  at 0.22 s. Sampled gauges do not measure every DB operation or hook phase.
- Daemon CPU grows with active sessions (8% to 38% median), and the median bucket
  `tool_call` p50 rises from 23 ms to 42 ms between 5-9 and 25-29 active sessions.
  The 0-4 row is higher (99 ms) on only 8 buckets. This observed trend is not specific
  to reviewers and does not isolate their marginal cost.
- Long-running tools such as spawns and searches can dominate bucket `tool_call`
  p95. Tool mix and daemon pressure are not separated by this comparison.

Counting caveat: reviewers that run one after another can share a 10-minute bucket, and
the metrics cover every project. The 4-5 reviewer buckets therefore do not mean 4-5
concurrent reviewers in this project.

## 6. Admission policy (source VERIFIED; retained runtime comparison UNKNOWN)

`ef4668dc77` (#23059 close-review admission policy) did the following:

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
according to the original report, which states that no review is censored and that
runtime is clipped to each window for the reviewer-seconds row. Under that stated
definition, 0.359 is about 36% of one slot's worth of time before the change; 0.129
is the busy fraction of the single slot after it. These values are preserved, but
the missing query receipts and unconfirmed §3/§6 population/runtime definitions
prevent a newly verified capacity or runtime comparison. The 202 s / 189 s p50 row
must not be used to claim a measured runtime improvement.

- Queue wait is now zero because contention no longer reaches the queue. A second
  caller is rejected with `close_review_busy`, or waits for Lane Manager release, before
  any row exists. That wait is not persisted.
- Under the old default of 3, peak overlap in the 7-day window was 3 reviews.
- The original report's occupancy figure implies 87.1% idle time in the "after"
  window, subject to the runtime-evidence limitation above.
- Fleet activity fell 41% (by `tool_call` events) between the windows, and the review
  rate fell 66%. Normalized per 1,000 `tool_call` events, reviews fell 43%.
- INFERRED: part of the drop tracks lower activity. The remainder is consistent with
  the admission process (Lane Manager release plus busy rejection), but release waits
  are not persisted, so that attribution is not measured. Reported average occupancy
  does not establish whether the slot bound bursts of simultaneous close requests.

## 7. Hypothesis ledger

| Hypothesis | Verdict |
| --- | --- |
| Reviewers cost more per call than standing seats. | Unknown as a resource-cost comparison. This run's proxy-call percentiles were lower with a different tool mix; whole-hook and per-call DB costs were not compared. §4. |
| Reviewer concurrency saturates hooks or the DB. | Unknown for whole hooks and allowed paths. Blocked-rule bucket p95 stayed at or below 10 ms, and sampled pool gauges showed no waiter, up to 26 active sessions per bucket. These observations do not establish simultaneous reviewer capacity or exclude saturation outside the measured population. §5. |
| #22729: SRT verification dominates launch. | Supported: 78% of this launch. Across the fleet, the per-spawn sum of SRT verification and preflight has p50 3.103486 s (3.1 s rounded). It is a small share of reviewer wall time. |
| #22629 (all-seat concurrency counting): all-seat counting must include reviewers. | Supported as an accounting rule: reviewers are agent processes and should be counted consistently. §5 does not isolate their marginal load or establish an equal cost per seat. A separate reviewer penalty is not supported by these measurements. |
| The single slot is justified by measured load. | Unsupported by the retained observations; whether host or whole-hook load requires it remains unknown. §5 and §6. |
| Reviewer model choice drives close latency. | Unsupported by this data. The medians differ (613 s against 200 s), but the task mixes are unequal and the `gpt-6.1-sol` sample is 4 runs. §3. |
| SRT denials slow reviews. | Unknown. The log captures only the first 100 main-run denials, all of them start-up probes. §2. |

## 8. Recommendation

1. Count reviewers in the all-seat total consistently and persist Lane Manager
   release waits to measure the admission process's share of the throughput drop
   (§6). Lane2 owns #23059 (close-review admission policy), including evaluation of
   a cap above 1 under PD sequencing and Josh's 5-minute load-below-24 rule. This
   study supplies bounded proxy/pool observations, not a safe cap: historical host
   load and allowed/full-hook latency are unmeasured, and runtime cohorts remain
   unconfirmed. No policy change is performed here.
2. #22729 (SRT verification caching) stays worthwhile for spawn-heavy fanout. Its
   measured launch share is small in this natural review; an effect on close
   throughput is not established.
3. Reviewer model speed: no recommendation. §3 records the per-model medians without
   comparing them.

## 9. Found work

1. SRT violation capture stops after 100 main-run records (§2). Filed as #23118
   (SRT capture) and delegated to L5 #14768 alongside #22729 (SRT verification).
2. `gobby-sessions:search_session_messages`
   (`src/gobby/mcp_proxy/tools/sessions/_messages.py:101-190`) scans rendered
   transcript windows linearly. In the 7-day window: 56 calls, p50 14.4 s, p90 200 s,
   max 1,397 s. A miss reads every window of every candidate session. Filed as #23117
   (bounded transcript search) and delegated to L2 #14828 after #23113
   (preceding L2 stability work).

## 10. Unknowns

- Allowed-rule and full-hook latency were not measured. Block-only per-rule
  `metrics_events.rule_eval` percentiles cannot exclude slow allowed effects,
  rule-level setup, hook prelude, hook-entry queue wait or transport (§5).
- Cohort/runtime query receipts are not retained. The §3 model table and §6 overall
  after-window p50 do not confirm a shared population/timer; no difference is assumed.
  The occupancy and runtime figures remain historical reported values (§3, §6).
- The proxy-call comparison uses different tool mixes. Relative per-call DB work,
  whole-hook cost and marginal reviewer load remain unmeasured (§4).
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
