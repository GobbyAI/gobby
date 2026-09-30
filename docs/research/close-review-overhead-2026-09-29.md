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
- In the last 24 hours the daemon hook and DB path showed no saturation, at up to 25
  active sessions and up to 5 reviewer sessions in a 10-minute bucket.
- Since serialization, the one slot has been busy only 17% of wall time.

Close throughput is limited by the admission process around the slot, reviewer model
time, and fleet activity. It is not limited by reviewer load on the daemon.

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
| Launch source | `Spawn phase timings` log lines from `agents/spawn_executor.execute_spawn`. 1,551 spawns, 2026-09-13 16:57 to now. |

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

Successful reviewer runs since serialization, by model:

| Provider / model | Runs | p50 | p90 | Mean tool calls |
| --- | --- | --- | --- | --- |
| codex `gpt-5.6-terra` | 29 | 184 s | 265 s | 35 |
| codex `gpt-6.1-sol` | 3 | 746 s | 784 s | 55 |
| claude `sonnet` | 1 | 72 s | 72 s | 22 |

Over 7 days, 446 `gpt-5.6-terra` reviewer runs succeeded with a mean of 265 s. These
medians describe what happened and are not a model comparison. The `gpt-6.1-sol` sample
is 3 runs, and the two models reviewed different task mixes and diff sizes. Any
conclusion about model speed needs a like-for-like sample.

## 4. Per-call cost: reviewer against standing seats (VERIFIED)

In the reviewer's own window (23:50:00 to 23:58:10), from `tool_call` metrics:

| Population | Calls | p50 | p95 | Sessions |
| --- | --- | --- | --- | --- |
| Reviewer | 39 | 15 ms | 487 ms | 1 |
| All other sessions | 128 | 50 ms | 2,243 ms | 15 |

The reviewer's maximum was the 59.8 s search in §3. Excluding it, the reviewer's calls
are cheaper than the seats' calls, because they are mostly short `get_task_diff` and
skill reads.

## 5. Concurrency against hook and DB latency, last 24 h (VERIFIED)

There are 131 buckets of 10 minutes each. Active sessions in a bucket are the distinct
sessions with a `tool_call` or `rule_eval` event. Standing seats are sessions without
an `agent_run_id`. Reviewers are sessions whose run is `task-close-reviewer`. Workers
are all other spawned runs. The same rule counts every population.

| Active sessions | Buckets | rule_eval p95 | tool_call p50 | tool_call p95 | Daemon CPU % | Pool waiting max | Executor queue age max |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 5-9 | 27 | 2.1 ms | 23 ms | 2,140 ms | 14 | 0 | 0 s |
| 10-14 | 37 | 2.2 ms | 23 ms | 2,202 ms | 27 | 0 | 0 s |
| 15-19 | 35 | 2.5 ms | 27 ms | 2,385 ms | 33 | 0 | 0.22 s |
| 20-24 | 22 | 2.2 ms | 34 ms | 2,255 ms | 38 | 0 | 0.15 s |
| 25-29 | 2 | 4.1 ms | 42 ms | 2,564 ms | 39 | 0 | 0 s |

By reviewers active in the bucket:

| Reviewers | Buckets | rule_eval p95 | tool_call p50 | tool_call p95 | CPU % |
| --- | --- | --- | --- | --- | --- |
| 0 | 64 | 2.2 ms | 25 ms | 2,157 ms | 20 |
| 1 | 38 | 2.5 ms | 27 ms | 2,232 ms | 30 |
| 2 | 20 | 2.2 ms | 30 ms | 2,612 ms | 36 |
| 3 | 7 | 2.3 ms | 25 ms | 2,619 ms | 32 |
| 4-5 | 2 | 2.0-3.1 ms | 22-32 ms | 2,228-3,926 ms | 37-44 |

Standing seats per bucket: median 13, maximum 23.

Findings:

- The hook path did not saturate. `rule_eval` p95 stayed between 2 and 4 ms across the
  whole range.
- #22729's 2026-09-22 measurement put the knee at `rule_eval` p95 197.6 ms with 15
  active sessions. That knee no longer reproduces; the stability work since then moved
  it. INFERRED: this is not a controlled comparison.
- The DB pool never had a waiter. The executor queue age peaked at 0.22 s.
- Daemon CPU grows with active sessions (14% to 39%), and `tool_call` p50 rises gently
  (23 ms to 42 ms). This is the only measurable trend, and it is not specific to
  reviewers.

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

Close reviews in project gobby, 24 hours either side of the change:

| Period | Reviews | Reviews per hour | Slot busy fraction | Run p50 | Queue wait p50 / max |
| --- | --- | --- | --- | --- | --- |
| Before `ef4668dc77` | 117 | 5.9 | 0.34 of one slot | 206 s | 0 s / 42 s |
| After | 33 | 2.8 | 0.17 | 188 s | 0 s / 0 s |

- Queue wait is now zero because contention no longer reaches the queue. A second
  caller is rejected with `close_review_busy`, or waits for Lane Manager release, before
  any row exists. That wait is not persisted.
- Under the old default of 3, peak overlap in the retained 7 days was 3 reviews.
- The one slot is idle 83% of wall time. INFERRED: halved throughput with a mostly idle
  slot points to the admission process (LM release plus the busy rejection) and lower
  fleet activity. Slot capacity is not the cause.

## 7. Hypothesis ledger

| Hypothesis | Verdict |
| --- | --- |
| Reviewers cost more per call than standing seats. | Rejected. §4. |
| Reviewer concurrency saturates hooks or the DB. | Rejected for the last 24 h. No saturation up to 25 active sessions. §5. |
| #22729: SRT verification dominates launch. | Supported: 78% of this launch, p50 about 2.8 s across the fleet. It is a small share of reviewer wall time. |
| #22629: all-seat counting must include reviewers. | Supported as an accounting rule. Reviewers were exempt from the per-project cap. §5 shows reviewers add a load increment comparable to a seat, so count them like seats, with no separate penalty. |
| The single slot is justified by measured load. | Unsupported. §5 and §6. |
| Reviewer model choice drives close latency. | Unsupported by this data. The medians differ (746 s against 184 s), but the task mixes are unequal and the `gpt-6.1-sol` sample is 3 runs. §3. |
| SRT denials slow reviews. | Unknown. The log captures only the first 100 main-run denials, all of them start-up probes. §2. |

## 8. Recommendation

1. Restore a per-project reviewer cap above 1, admitted when the 5-minute load average
   is below 24. Count reviewers in the all-seat total the same way standing seats are
   counted. Drop the Lane Manager release step, or make it apply only when admission
   refuses. Lane2 owns this under #23059.
2. #22729 SRT verification caching stays worthwhile for spawn-heavy fanout. It does not
   change close throughput.
3. Reviewer model speed: no recommendation. §3 records the per-model medians without
   comparing them.

## 9. Found work

1. SRT violation capture stops after 100 main-run records (§2). Filed as #23118 and
   delegated to L5 #14768 alongside #22729.
2. `gobby-sessions:search_session_messages`
   (`src/gobby/mcp_proxy/tools/sessions/_messages.py:101-190`) scans rendered
   transcript windows linearly. Over 7 days: 56 calls, p50 14.4 s, p90 200 s,
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
