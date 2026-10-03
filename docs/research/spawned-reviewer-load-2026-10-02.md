# Spawned close reviewer versus pane reviewer seats: host load

Task: #23353. Researcher gobby#14550, 2026-10-02, 18:57-20:45 CT.

Question (Josh): "I can run 20 code reviwer sessions in gclient panes and not cause
much measurable load, but spawn_agent a task-close-reviewer and load spikes."

Evidence labels: VERIFIED is a direct measurement listed in §2; INFERRED is
interpretation; UNKNOWN was not measured. All captures were out of process. There
were no live storage reads, no full pytest and no daemon restart.

## 1. Answer

A spawned reviewer differs from a pane seat before its model ever runs. It also
differs again when its close is evaluated. The reviewer's own process tree is
cheap in both arms.

1. **Spawn setup is the dominant per-spawn host cost (candidate 2, RULED IN).**
   Every sandboxed spawn runs `prepare_sandbox_run_paths`, which runs
   `_prewarm_pre_commit_store` and `_schedule_pre_commit_store_spare` (#21730).
   - It consumes a pre-commit store spare and then APFS-clones a new one with
     `cp -c -R` and `chmod -R`. The store is `~/.cache/pre-commit`: 393 MB in
     17,245 files.
   - `sandbox_reaper` later runs `rmtree` on the per-run root.
   - One clone, chmod and remove measured 38.5 s wall and 7.0 s kernel CPU, and
     added about 0.9 cores of system CPU while it ran. A pane seat does none of
     this.
   - `_preflight_srt` adds a p50 of 3.9 s per spawn, with p90 8.9 s.
2. **Close evaluation decodes transcript evidence on the daemon event loop.**
   This explains the close and preview timeouts and the stalls in daemon health
   and hooks.
   - `decode_cooperatively` yields once per 256-record chunk.
   - On long transcripts the loop held the GIL in `_decode_steps` for up to
     12.4 s of one minute.
   - Hook adapter timeouts and health probes over 2 s fall in those minutes.
3. **Daemon worker threads hold the GIL 36-76% of every minute.**
   - The holders are the postgres pool's pure-Python paths, transcript
     decode/persist, embeddings parsing, and the codex installer TOML load.
   - The loop-only `/api/health` therefore waits: p50 0.41 s, max 4.7 s, and
     2 s timeouts for several seats.
   - This is ambient. A spawned reviewer adds to it through (2) and through
     transcript ingestion.

The host baseline amplifies all three. RAM is full: 290 MB free, 22 GB in the
compressor, about 400 MB/s of compressor churn, and the Docker VM at 30 GB resident.
On this host, 1-minute load swings between 13 and 46 while all-process CPU stays at
4.8-6.8 of 18 cores.

## 2. Measurements

### 2.1 Collectors

All collectors ran out of process. Times are CT on 2026-10-02.

| Collector | Window | What |
|---|---|---|
| 1 s process sampler (`ps` plus libproc rusage) | 18:57 to end | per-pid CPU, RSS, disk bytes, loadavg; its own cost is 0.11-0.13 cores |
| `iostat -w 1` | 19:16 to end | system us/sy/id, disk tps and MB/s |
| `top -l 0 -s 2` headers and `vm_stat 2` | 19:30 to end | running and stuck processes, threads, compressor |
| Josh's root capture: `fs_usage -f exec` plus `py-spy record --gil` per minute (20 Hz, 1,200 samples) on daemon pid 34198 | 19:06 to about 20:46 | exec storm and GIL holders; `fs_usage` itself costs up to 0.73 cores |
| `py-spy dump` (passwordless sudo rule) | 20:35-20:36 | 11 point-in-time daemon stacks |
| `/api/health` latency every 5 s | 20:33 to end | 62 or more probes |
| `daemon.log` "Spawn phase timings" and `mcp.log` close gates | all of today | 42 spawns, gate 13 timings |

Observer artifacts were excluded. The sampler's `ps` exec and its 8,605 failed
`execvp` PATH probes are its own. `fs_usage` prints a posix_spawn path as the
caller's cwd, so the "1,177 `gobby` execs" in the first count were claude
posix_spawns, not CLI start-ups.

### 2.2 Baseline and arms

| Arm | Window | Wall | Tree CPU | Pids | Disk written | Host during arm |
|---|---|---|---|---|---|---|
| Baseline | 18:57:30-19:01:30 | n/a | n/a | 1,489 new pids per 195 s, host-wide | n/a | all-process 5.65 cores, load1 mean 23.8 |
| A2: codex pane, gpt-5.6-terra xhigh, same rendered prompt | 19:02:07-19:18:53 | 15 m 7 s to verdict | 52.6 s (0.05 cores) | 176 | 133.5 MB | load1 mean 33.3 |
| A2 launch (first 60 s) | 19:02:07-19:03:07 | | 8.1 s | 13 | 63.3 MB | load1 mean 30.3 |
| A2 run | 19:03:07-19:18:53 | | 44.4 s: gcode 25.3, codex 16.4 | 170 | 69.2 MB | load1 mean 33.3, max 47.3 |
| A2 end (idle until quit) | 19:18:53-19:22:35 | | 0.1 s | 7 | 0.4 MB | load1 mean 27.6 |
| Natural B: reviewer spawned by close, srt root 58439 | born 19:08:47 | | 16.5 s | 42 | INFERRED small | see §2.3 |
| Natural B: srt root 65525 | born 19:10:48 | | 9.2 s | 12 | INFERRED small | see §2.3 |
| A1: Claude pane | NOT RUN | | | | | load hold |
| B: direct `spawn_agent`, key r-23353-armB-v1 | NOT RUN | | | | | load hold |

A1 and B did not run. Josh's load rule held from 19:09 through the end of the
capture window: 5-minute readings were 22.2-33.5, and none came in twice in a row
below 24. A2 launched before `fs_usage` tracing started; the natural B spawns
launched during it.

### 2.3 Natural spawns during the 19:05-19:12 spike: confounded

- Over 19:05-19:12, all-process CPU was 5.93 cores, against 5.65 at baseline.
  load1 mean was 35.6 (max 47.3), and load5 went from 28.9 to 36.5.
- The srt reviewer trees used 0.03-0.28 cores. In the same window, cargo/rustc
  used about 2 cores (19:06:40-19:08:20) and 4 close keys landed at once.
- At 19:14-19:15:40, load1 rose from 13.5 to 31.5 with no reviewer and no build
  running.
- INFERRED: the natural spike cannot be pinned on the reviewer tree itself.

### 2.4 Spawn setup (candidate 2): RULED IN

`daemon.log` "Spawn phase timings" for today's 42 codex spawns, in seconds:

| Phase | p50 | p90 | max |
|---|---|---|---|
| `prepare_sandbox_run_paths` | 1.30 | 4.63 | 22.69 |
| `_preflight_srt` | 3.87 | 8.86 | 11.75 |
| `verify_srt_installation` | 0.41 | 3.00 | 24.25 |
| `prepare_terminal_spawn` | 0.25 | 1.00 | 5.66 |
| `provider_post_sandbox` | 0.23 | 1.24 | 1.97 |

The reviewer spawns at 19:08:46 and 19:10:45 had preflight times of 9.13 s and
10.92 s.

One pre-commit store cycle, measured by hand at 20:28 with `/usr/bin/time`:

| Step | Wall | Kernel CPU | Other |
|---|---|---|---|
| `cp -c -R ~/.cache/pre-commit` | 28.05 s | 5.15 s | 22,551 involuntary context switches |
| `chmod -R u+rwX` | 1.02 s | 0.22 s | |
| `rm -rf` | 8.73 s | 1.67 s | |

From `top` during the clone (n=7, indicative), compared with the minute before:
- running processes rose from 20.8 to 25.6;
- stuck processes rose from 0.47 to 2.0;
- system CPU rose from 23.7% to 28.5%, about 0.9 cores.

INFERRED: APFS clone metadata work runs in kernel threads. The per-process
sampler does not attribute it, which is why per-process CPU barely moves while
load rises.

The exec trace shows the daemon's `/bin/cp` at 19:08:27 and its `chmod` at
19:09:11. That straddles the 19:08:46 reviewer spawn and load1's step from 39 to
43.

### 2.5 SRT and violation-log writes (candidate 1): RULED OUT

- The `log stream` violation watcher used about 1.7 CPU-s per minute.
- logd used about 0.01 cores.
- All srt-runner trees together wrote 42 MB over 19:16-19:29.
- The Docker VM wrote 3.66 GB in the same window.

The SRT preflight (§2.4) belongs to spawn setup.

### 2.6 Daemon-side run tracking (candidate 3): RULED OUT

- Daemon CPU was 0.36 cores in the spike window, against 0.33 at baseline.
- Daemon children were `ps` at about 16 per minute and gcode/git.
- No GIL top holder in any minute from 19:06 to 20:38 is run tracking.

### 2.7 Validation reruns (candidate 4): RULED OUT for load

- No pytest, ruff or mypy process was born under the daemon in any window.
- The close checklist's command parsing (`shell_lexing.parse_shell_command`,
  `command_equivalence`, `criterion_commands`) does appear among the GIL holders
  at 27-54 samples per minute. That is close-evaluation CPU (§2.8), not reruns.

### 2.8 Close-path transcript decode on the event loop

These are loop-thread GIL samples in `tasks/transcript_evidence_transfer._decode_steps`.
The call path is `close_task`, then `_evaluate_close`, then
`derive_close_transcript_evidence`, then either `derive_prelink_runs` or
`derive_transcript_evidence`, then `decode_cooperatively`.

| Minute | Loop decode samples | Seconds | Coincident |
|---|---|---|---|
| 20:10:49 | 249 | 12.4 | gate 13 for #23227 (05d11691) completed at 20:12:11 |
| 20:15:52 | 65 | 3.3 | |
| 20:16:51 | 166 | 8.3 | `mcp.log` "Retrying hook after adapter timeout" and "daemon-not-ready gate", 20:17:05-20:17:41 |
| 20:33:52 | 57 | 2.9 | health probes at 2.6 s and 3.6 s (20:34:21, 20:34:29) |
| other minutes, 19:50-20:38 | 0-12 | ≤0.6 | |

#23227 close (PD question):
- VERIFIED: in each minute from 20:00 to 20:09, close-evaluation stacks held the
  GIL for at most 20 samples (1 s), on all threads.
- INFERRED: most of exec 837's 300 s budget was spent waiting without the GIL.
  Candidates are a git subprocess, the DB, or GIL contention.
- VERIFIED: a 20:36:02 dump shows a close waiting in
  `_task_scope.collect_net_commit_paths_async`, `commits._net_commit_patch` and
  then `daemon_git`. Another dump shows `close_checklist.evaluate_validation_commands`
  calling `realpath` per command run (`command_equivalence.runs_outside_root`).
- UNKNOWN: a per-gate wall-time split. `mcp.log` logs only gate 13.
- So the decode is the largest GIL burst, and it lands at the end, but it does not
  explain the whole 300 s.

Short-transcript closes still fit. Gate 13 completed in 14-196 ms for other tasks
throughout 19:54-20:35 (`mcp.log`), and those closes did not time out.
UNKNOWN: their total wall time.

### 2.9 Health starved of the GIL

- `/api/health` (`servers/routes/admin/_health.py`) does no executor hop (#20839).
- Per-minute GIL share, 19:30-20:38: 36-76% in total, but only 2-36% on the loop
  thread.
- Top off-loop holders are `storage/hub/postgres_pool` (`transaction_context`,
  `assert_runtime_role`, `_normalize_value`, `fetchall`), `sessions/transcripts`
  decode and persist, `ai/embeddings._parse_embeddings_response` (up to 485
  samples), `cli/installers/codex._load_toml_config` (263 samples at 20:08) and
  `config/runtime_models.active` (117 samples at 20:34:54).
- Health latency from 20:33 on: p50 0.41 s, p90 1.92 s, max 4.70 s. 5 of 62 probes
  took over 2 s, and seats received DAEMON_UNAVAILABLE.

### 2.10 Ambient host state

- `top` at 19:28: 126 GB used, 290 MB free, 22 GB compressor.
- `vm_stat`: about 46k decompressions and 51k compressions per 2 s (16 KB pages),
  and about 1.1k pageins.
- Resident memory: the Docker VM 30 GB (Josh added 8 GB at 18:39), claude 7.8 GB
  over 33 processes, Comet 7.5 GB.
- Disk reads over 19:16-19:29 totaled 15.7 GB. Codex pane seats read 0.3-0.84 GB
  each, the daemon 0.7 GB, and its multiprocessing workers 0.5-0.7 GB each.
- INFERRED: page-cache thrash. It makes every cold start and every metadata-heavy
  operation slower.

## 3. Owners and fix directions

| # | Finding | Owning surface | Fix direction |
|---|---|---|---|
| 1 | Per-spawn pre-commit prewarm: clone, chmod and rm of 17k files | `agents/sandbox_policy.py` `prepare_sandbox_run_paths`, `_prewarm_pre_commit_store`, `_schedule_pre_commit_store_spare`, `_clone_pre_commit_store`; `agents/sandbox_reaper.py` (#21730) | Skip the prewarm for runs that never run pre-commit, such as read-only close reviewers. For committing runs, share one writable run cache across runs, or point pre-commit at the operator store, rather than a per-run clone plus spare. Recommended: the skip, which is the least mechanism and removes the whole cost from reviewers. |
| 2 | Close evidence decode on the event loop | `tasks/transcript_evidence_transfer.decode_cooperatively`, called from `mcp_proxy/tools/tasks/_close_evaluation_support.derive_close_transcript_evidence` (made cooperative on the loop by #23256) | A thread executor alone only moves the decode into finding 3. Decode-once caching is recommended: persist decoded evidence keyed by transcript and offset, and decode only the new tail on each close or preview. A process pool and time-sliced yields cap stalls but still repeat the whole decode per call. |
| 3 | Loop starved of the GIL by worker threads | the off-loop holders listed in §2.9 | Cut the pure-Python per-row work in `postgres_pool._normalize_value` and `assert_runtime_role` per transaction. Cache `codex._load_toml_config`. Move the transcript ingest decode and embeddings parsing to a process. Health is already loop-only. |

These are handed to the PD (gobby#14972) for Josh's routing during the close-queue
freeze. No seat claims new tasks. #23353 changes no code.

## 4. Unknowns

- Arms A1 and B: held by Josh's load rule for the whole capture window.
- Per-gate close wall time: `mcp.log` logs only gate 13.
- What exec 837 waited on between 20:01 and 20:10, beyond GIL-held time.
- Whether macOS load average counts APFS lock waits. INFERRED from the rise in
  the running and stuck counts during the clone.
- The kernel-thread CPU of APFS clones, beyond the system-wide `sy` rise.

## 5. Re-measure plan

After fix 1 lands, run arm B (one direct `spawn_agent` of `task-close-reviewer`)
and a pane arm under Josh's rule. Compare the spawn phase timings,
`top` running and stuck counts, and `sy%` over the 60 s after spawn. After fix 2,
rerun the close preview of a long-transcript task under `py-spy --gil` and
confirm that loop-held `_decode_steps` falls to near zero.
