# gclient startup timing and loading indicator

Research report for **#23356 — Measure where gclient's ~20 s startup goes and
recommend a loading-progress indicator**. Researcher 2, 2026-10-02 CT.

## Findings

The three most recent complete launches recorded in `gclient.log.20728` took
**8.88, 26.38 and 16.69 seconds** on the existing startup clock. The roster stage
took **8.76, 25.28 and 16.25 seconds**, respectively: **95.84–98.58%** of the sum of
the recorded stage durations. This reproduces the reported order of delay from
natural launches without opening another client or touching its panes.

Three subsequent isolated launches of the installed **gclient 0.1.18**, with
**29 real inert panes across four tabs (8/7/7/7)**, reached the first restored
screen in **10.475, 9.589 and 6.330 seconds** from hidden-PTY launch. Each made
**29 individual terminal metadata requests, with peak concurrency 1**. Their
serial windows consumed **6.579, 6.632 and 3.912 seconds**. These are observations
of the installed client against private synthetic state, not production latency
or a measured speedup from a fix. Detailed boundaries and raw timestamps follow.

The source confirms avoidable serial work inside that stage: unresolved pane IDs
are captured before the relist response is installed, then each ID is fetched
individually after the relist completes, including IDs already returned by that
relist. The current project's hidden tabs participate. Reusing the returned
metadata is the smallest correction to investigate first. The logs establish the
dominant stage. The isolated observations split out the individual request chain;
the historical production logs cannot attribute their roster time that precisely.
Source evidence is listed below.

Recommend showing the **actual phase, terminal completion count and elapsed
time** on the existing splash. A fixed countdown is unsupported by a sample
spanning 8.88–26.38 seconds. Use the existing stage state, with real completion
updates for its long roster phase. Josh approved the design through the Assistant
(message `7e1891cf-84b9-4dd3-8a3a-ba625e22d079`). L1 owns implementation; this report
changes no UI code.

## Historical measurements and boundaries

Read the existing log before any experimental launch. The
[extracted evidence](23356-gclient-startup-2026-10-02.json) preserves every selected
stage line and summary. Selection is the three most recent **complete** sequences
in that file, rather than three chosen slow launches. Integer milliseconds are
from `took_ms`; totals are the existing two-decimal summary. Small differences
between the sum and total reflect rounding and time between transitions.

| Existing log completion, CT | Daemon connection stage | Workspace attach | Roster and terminal metadata | First frame stage | Logged total |
| --- | ---: | ---: | ---: | ---: | ---: |
| Oct 2, 10:07:41 | 0.025 s | 0.025 s | 8.755 s | 0.076 s | 8.88 s |
| Oct 2, 18:51:05 | 0.579 s | 0.320 s | 25.281 s | 0.199 s | 26.38 s |
| Oct 2, 18:57:47 | 0.072 s | 0.282 s | 16.247 s | 0.090 s | 16.69 s |

Median logged total: **16.69 s**. Median roster duration: **16.247 s**. The daemon
connection and workspace attach together consumed 0.050, 0.899 and 0.354 seconds.
No arithmetic projects a guaranteed improvement or future completion time.

The installed binary read during research reports `gclient 0.1.18`. Existing
startup spans omit per-launch binary identity; the morning launch cannot be
asserted to use that version. They also omit pane count and tab distribution.
The task card reports **29 panes today**; that is context, not an observed count
for each historical launch.

| Requested phase | What is measured | Remaining limit |
| --- | --- | --- |
| Daemon attach | Connection stage and workspace attach separately, for three launches | The connection stage includes its connection workflow; the label `daemon health` does not mean a standalone timed HTTP health probe. |
| gterm host handshake | Source places direct-host connection inside each pane's attach workflow | No separate handshake span; no measured host-only duration. |
| Roster | Full recorded roster stage, including attention and individual terminal metadata reads | No split between relist, attention and per-terminal reads. |
| Sidebar | Source queues initial sidebar work after first-frame completion | No independent fetch duration in these startup spans; it is outside the initial loading barrier. |
| Per-tab pane attachment, 29 panes | Source starts pane recovery futures after roster installation; the first-frame gate waits for the focused pane to settle | No measured per-tab/all-pane attach duration or verified per-launch pane count. Hidden-pane completion must not be equated with leaving the splash. |
| First render | First-frame stage: 0.076, 0.199 and 0.090 s | Includes readiness work; it is not CPU rendering time. The initial splash draw, process prelude and final terminal flush are not separately timed. |

The clock starts when `run_ready` constructs `StartupStages`, after terminal
construction. First-frame completion is logged before the final render call that
exposes the workspace. These measurements therefore describe the existing
startup barrier, **not a new measurement of process-spawn-to-visible-paint** [E7,
E8].

At the original 19:48 CT checkpoint, no synthetic launch had started: the 19:11 CT
close-queue freeze and load hold prevented it. The historical JSON retains its
null subphase measurements. The following isolated measurements were made later,
under LM's once-only key `research2-23356-resume-v1`; they do not fill historical
nulls or infer timings by dividing a roster duration by 29.

## Three isolated installed-client launches

The [isolated observation artifact](23356-gclient-isolated-2026-10-02.json)
contains nanosecond offsets, paired HTTP and daemon messages, all 29 host frame
lifecycles per sample, tab membership, native stage lines, binary identities and
cleanup results. No authentication values or response payloads are committed.

### Method and clocks

- Run serially against the PostgreSQL **test hub on 60892**, with
  `GOBBY_TEST_PROTECT=1`, a private schema, HOME/GOBBY_HOME, project, ports and
  gterm host. Use the actual installed gclient, gdaemon front door, Python runner
  and gterm. The 29 panes run inert shells that print `R2VISIBLE`, then sleep.
  These are real processes/services on synthetic state, rather than a replay or
  fake host.
- Start a monotonic clock immediately before the hidden PTY driver creates the
  client (120 columns by 40 rows). Observe HTTP/daemon WebSocket traffic through
  a transparent loopback proxy and gterm traffic through a transparent UNIX
  socket proxy. Timestamp the first reconstructed PTY screen containing the
  printed marker; octal escaping keeps the literal marker out of command
  metadata. This is an **observed restored paint**, including observer scheduling
  and PTY reads, not CPU rendering time or the exact instant of splash removal.
- Keep the private client alive for one second after paint, then select only
  still-unframed private tabs with the installed CLI. Preserve those selections
  in `tab_visits`. All 29 hosts reach hello, welcome, attached and first frame.
  Later tab selection is an observation intervention; it does not establish why
  a frame was late. Per-tab spans overlap and are not additive startup phases.
- Samples began Oct 2 CT at **21:43:42, 21:52:07 and 21:53:54** (UTC dates and
  offsets in the JSON). Sample 1 used one fresh private fixture; samples 2 and 3
  reused a second fixture serially. The installed set was identical throughout:
  gclient `0.1.18`, SHA-256
  `7e50983975eacb6b3b2c463cedf63c17a8de2f110862756b76f1ec5906f579ac`,
  device `16777231`, inode `546538317`. Other binary hashes are in the artifact.
- Existing native stages use a different boundary [E7, E8]. The label
  `daemon health` covers reconnect and configuration [E11]. The separate early
  `/api/health` probe is **not** that stage, and is labeled preflight below.
  Do not sum overlapping wire durations, host spans and native stages.

### External phase observations

All values below are milliseconds. Request durations are request-to-reply at the
observer; offsets are measured from the launch clock. JSON retains full precision.

| Observation | Sample 1 | Sample 2 | Sample 3 |
| --- | ---: | ---: | ---: |
| Launch to first PTY output | 230.779 | 156.345 | 49.647 |
| Preflight HTTP health duration | 108.767 | 11.489 | 17.256 |
| Daemon WebSocket accepted to connection-established reply | 14.185 | 8.743 | 7.624 |
| Initial configuration request | 11.477 | 8.253 | 4.573 |
| Workspace attach request to snapshot reply | 58.872 | 36.559 | 15.827 |
| Initial terminal inventory request | 968.482 | 576.126 | 247.285 |
| Initial attention roster request | 161.687 | 106.950 | 44.500 |
| Inventory and attention overlapping window | 968.482 | 576.715 | 247.285 |
| 29 serial metadata requests: first request to final reply | 6578.629 | 6632.169 | 3911.623 |
| Sum of those 29 request service durations | 6481.881 | 6495.408 | 3868.573 |
| gterm hello to welcome: median across 29 hosts | 0.457 | 0.148 | 0.504 |
| gterm hello to welcome: minimum / maximum | 0.091 / 8.249 | 0.039 / 0.594 | 0.050 / 32.114 |
| Host connection to first frame: median across 29 | 77.079 | 67.261 | 52.704 |
| Host connection to first frame: minimum / maximum | 9.521 / 106.945 | 10.032 / 124.270 | 18.338 / 1191.035 |
| Launch to first observed restored paint | 10475.253 | 9589.182 | 6329.839 |
| Launch to all 29 hosts' first frames | 11787.679 | 9615.490 | 7500.398 |
| First paint to all 29 hosts' first frames | 1312.426 | 26.308 | 1170.559 |

The serial metadata window is **62.80%, 69.16% and 61.80%** of external
launch-to-paint time. Request concurrency is 1 in all three samples. The native
roster still dominates its narrower clock, at **92.47%, 97.57% and 97.52%**.

| Native stage / external comparison, ms | Sample 1 | Sample 2 | Sample 3 |
| --- | ---: | ---: | ---: |
| Daemon health (reconnect/configuration workflow) | 60 | 42 | 35 |
| Workspace attach | 59 | 38 | 16 |
| Roster | 7564 | 7232 | 4162 |
| First frame readiness stage | 497 | 100 | 55 |
| Sum of native stage durations | 8180 | 7412 | 4268 |
| External paint minus native stage sum | 2295.253 | 2177.182 | 2061.839 |

The residual is measured **unlogged time/boundary difference**, not an attribution
to terminal construction. The observer sees preflight health finish at 220.711,
154.179 and 46.872 ms; daemon WebSocket acceptance occurs at 2261.940, 2179.868 and
2076.016 ms. That gap contains unobserved work. The observer does not establish
its cause. Native stages also end before final rendering and PTY observation.

### Every tab and sidebar

For each tab, the window runs from its earliest host connection to its last
host's first frame. Every pane is mapped by terminal/host identity. Tabs overlap;
some last frames arrive after the focused workspace has painted.

| Tab (actual panes) | Sample 1 window, ms | Sample 2 window, ms | Sample 3 window, ms |
| --- | ---: | ---: | ---: |
| Tab 1 (8) | 110.023 | 130.860 | 81.412 |
| Tab 2 (7) | 97.051 | 114.009 | 81.835 |
| Tab 3 (7) | 1375.260 | 103.607 | 1174.502 |
| Tab 4 (7) | 1382.572 | 113.063 | 1199.400 |

One second after initial paint, natural observations had 23/29, 29/29 and 23/29
first frames, respectively (connections: 23, 29 and 26). Sample 1 then selected
Tab 3; sample 3 selected Tabs 3 and 4. The longer Tab 3/4 windows therefore include
the observation delay and possible effects of selection; they are not independent
host service benchmarks. Raw connection/hello/welcome/attached/frame timestamps
allow those boundaries to be inspected separately.

Sidebar work is queued after the focused-pane first-frame barrier [E7]. Sample 3
observed `/api/projects` at **7503.257–7511.929 ms** (8.672 ms), then
`/api/source-control/status` at **7512.595–7577.740 ms** (65.144 ms), both after
paint at 6329.839 ms. Samples 1 and 2 have **no observed sidebar request** in their
bounded capture; their sidebar durations are null, not zero. This study establishes
sidebar ordering and one observed post-paint fetch pair, not three sidebar latency
samples or a bound on later sidebar completion.

### Limits and cleanup

Synthetic idle panes and a private project differ from Josh's live workspace.
Proxy/parser/PTY scheduling adds observation overhead; no overhead calibration
was made. Sequential reuse can warm caches. Sample 1 completed all measurements
before an observer teardown EOF assertion; the teardown ordering was corrected
before samples 2/3. Its recovered artifact has complete ordered events and no
owned survivors. Two incomplete setup/tab-selection diagnostic attempts are
excluded from the three samples.

The LM acknowledged END of the heavy key at 21:55 CT. Research-owned PIDs were
checked against captured identity, arguments and private root immediately before
TERM, with bounded KILL only for survivors. All tracked owned processes are gone;
the two private schemas were removed. The test hub and production daemon remained
running. Josh's client was never quit, restarted or focused. No live storage
SELECT, daemon restart, new production log line, source instrumentation or binary
promotion was used.

## Ordering, overlap and avoidable work

1. Connection, workspace attach, roster and first-frame preparation are staged
   serially [E3]. The workspace snapshot supplies the project/tab/pane identity
   required by later work; blindly parallelizing those dependencies is unsound.
2. Relist and attention already overlap through `tokio::try_join!` [E1]. Their
   overlap must be retained rather than counting both durations additively.
3. The metadata fetch loop is serial **after** that join [E1]. Its captured IDs
   cover every tab of the selected project [E2]. The relist rows are only applied
   after the entire startup future resolves [E3], so the loop does not subtract
   IDs that the relist already supplied. Reuse returned rows first; only genuine
   misses require lookup. If misses remain, focused-tab priority and bounded
   concurrency are proposals to measure, not proven speedups.
4. Pane attach work is collected as futures [E4], then placed in the live loop's
   recovery set after roster installation [E3]. Direct attachment first obtains
   the daemon attachment reply, then connects to the host [E5]. A single global
   host-handshake timer would misrepresent those per-pane workflows.
5. The loading barrier tests the focused pane, not every pane. A first frame or
   an explicit detached status settles it [E6, E7]. Initial sidebar fetches are
   queued afterward [E7]. Sidebar/source-control warnings after startup cannot
   by themselves explain the 16–26 second roster delay.

Prior memory `782f0b6c` identifies a projection race when early workspace events
arrive before pane metadata. Any L1 correction must retain unresolved projection
retries and snapshot/generation ordering. The existing regression is
`crates/gclient/tests/startup_latency.rs::focused_tab_panes_are_drawn_after_startup_opens_them`.

## Approved indicator design

**Visitor mode: Operate.** The developer has opened a daily terminal workspace and
needs to distinguish useful work from a stalled launch. Keep the established
goblin/wordmark and terminal typography. Add a compact status block beneath them;
use the current theme and palette. The sketch's numbers are illustrative.

```text
                         [existing Gobby mark]

                     Restoring workspace
                     Reading terminal details · 18 of 29
                     12 s elapsed

                     ✓ Connected to daemon
                     ✓ Workspace loaded
                     • Reading terminal details
                     · Waiting for the focused terminal
```

On a narrow/short terminal, omit the checklist and mark before truncating the
meaningful status:

```text
Restoring workspace · 18 of 29 terminal details · 12 s elapsed
```

- **State comes from completed work.** Connection and snapshot completion use
  existing `StartupStages`. During metadata restoration, count resolved or
  explicitly unavailable required terminal records, out of the unique records
  needed for this launch. The denominator comes from the workspace, never the
  constant 29. Distinguish unavailable terminals in the text. A skipped request
  because metadata was already returned counts as resolved, not simulated work.
- **Name the barrier accurately.** Metadata retrieval says `Reading terminal
  details`; actual focused-pane attachment says `Connecting focused terminal`;
  waiting for its frame says `Waiting for the focused terminal`. After that pane
  settles, show the workspace immediately. Remaining background attachments may
  report status in normal chrome; they must not restore a full-screen splash.
- **Communicate progress honestly.** Completed counts advance on real outcomes.
  Elapsed time uses a fresh monotonic instant; the stored stage clock alone does
  not advance while a stage runs [E10]. Do not turn four completed-phase markers
  into a time-weighted percentage. Ship this measured progress first, with no
  fixed remaining-time promise. A later approximate ETA needs enough recent,
  comparable samples and a confidence policy; the three traces do not supply it.
- **Handle slow and failed work.** While a request remains pending, retain the
  phase and elapsed time; never animate fake completion. Use the existing
  unreachable/retry state on disconnect. On explicit terminal failure, name the
  unavailable terminal and allow the existing settled-error path [E6]. On an
  empty workspace, omit the pane counter and open the empty state. Preserve
  existing keyboard behavior; this proposal adds no new buttons.
- **Remain readable.** Text plus position and distinct markers carry status in
  monochrome. Accent reinforces the active row; error uses the established
  destructive palette. Apply the same hierarchy in dark and light themes. A
  static active marker works with reduced motion; extra animation is unnecessary.

The smallest approved implementation direction is the existing stage
model plus progress updates from metadata resolution and focused-pane readiness.
It needs no second startup subsystem, new log lines or new telemetry store. The
current splash renderer itself draws only the marks [E9].

## Defect ownership and disposition

L1 `gobby#14909` received the source and timing finding in durable message
`46d6b97f-72cd-4d46-8c4b-db6ca0adf603` and explicitly acknowledged ownership of the
redundant serial lookup defect (ownership ACK
`9d8b7504-883b-4a17-8e6e-35cc7053377c`). The three-launch empirical update was
sent in `e11d1cae-2e9d-4dc2-be01-4e85bc0d4ddb`. L1 owns the fix and approved
indicator implementation in its queue. This research changes no Rust or UI code.

The historical evidence, isolated observations and approved sketch are ready for
independent review by R6. Three launches now cover external paint, inventory,
metadata, per-host handshakes and every tab with observed pane counts. Sidebar
latency retains the bounded capture limitation above. The task remains claimed
pending review, Merge Manager landing and close direction; its original card has
not been amended by this follow-up. No before/after performance comparison is
claimed: L1's correction was not present in these installed-client measurements.

## Source evidence

Native `gcode evidence` reads E1–E10 were complete, with no warnings, at source
HEAD `227a186d8c92f117bc1ac083cd2992419d8873c9`, tree
`13b32d980c232acff76b5cca28bb663fae5fab02`. Each ID records the exact excerpt hash.
The paths are relative to the repository root.

| ID | Source and quoted evidence | Excerpt hash |
| --- | --- | --- |
| E1 | `crates/gclient/src/app/live_loop/startup.rs:46–59`: `48\| let unresolved = workspace.unresolved_terminal_ids();`; `51\| let (rows, attention) = tokio::try_join!(relist, daemon.attention_roster())?;`; `53\| for terminal_id in unresolved {`; `54\| if let Ok(row) = daemon.terminal(&terminal_id).await {` | `730849356c55efa5f1fba4f5401b197cb7cb0071c79ab107669ad87dc108a63c` |
| E2 | `crates/gclient/src/app/live_workspace.rs:98–115`: `106\| .panes()`; `110\| .is_some_and(\|tab\| tab.project_id == project)`; `113\| .filter(\|terminal_id\| self.pane_for_terminal(terminal_id).is_none())` | `9a1a30ca40fb9cf03ba21366988d237092fa59e9b96ee9316b1f9d52fb60c72b` |
| E3 | `crates/gclient/src/app/live_loop.rs:245–321`: `251\| startup_job = Some(startup::attach(daemon.clone(), workspace.attach_target().clone()));`; `270\| startup_job = Some(startup::roster(workspace));`; `273\| workspace.apply_relist(relist);`; `274\| workspace.install_unresolved_rows(unresolved);`; `300\| recoveries.extend(workspace.start_due_attaches(Instant::now(), true));` | `a82e304ed5dfe47a171d23dc134188ea24663f846bf0ba03c723788861a300f0` |
| E4 | `crates/gclient/src/app/live_attach.rs:486–546`: `526\| let retry: RecoveryFuture = Box::pin(async move {`; `543\| started.push(retry);` | `8971ccbecc85048a1e5c3ccf32baa1fdc5d42f4a3766a2f21a11240f3bf4c278` |
| E5 | `crates/gclient/src/app/live_attach.rs:670–714`: `678\| "type": "terminal_attach",`; `685\| .await?;`; `700\| match connect_direct_reply(gobby_home, &reply).await {` | `cb25f2988ca329ce78b0468a78439a5dd2213fef191101ae06f9e73525c76d12` |
| E6 | `crates/gclient/src/app/pane.rs:422–425`: `423\| self.frames_rendered > 0`; `424\| \|\| (matches!(self.attach, AttachState::Detached) && self.status_message.is_some())` | `f3e972afd7682dcb8bab1cf915f6acc0bf8c3797a058936800499e04b2ece429` |
| E7 | `crates/gclient/src/app/live_loop.rs:554–576`: `561\| .is_none_or(\|pane_id\| workspace.pane(pane_id).first_frame_settled())`; `563\| startup::mark_done(chrome, StartupStage::FirstFrame);`; `564\| if let Err(error) = render_live_workspace(terminal, workspace, chrome) {`; `568\| workspace.queue_initial_sidebar_fetch();`; `569\| launch_pending = false;` | `ec109a51d516e6ee44bea1c1e518455ff4cf37d6aefdbce17e11ce14e37bae9f` |
| E8 | `crates/gclient/src/views/mod.rs:71–82`: `71\| let mut terminal = Terminal::new(CrosstermBackend::new(std::io::stdout()))?;`; `77\| stages: Some(StartupStages::begin(now)),` | `0aa19f61a4a43501409d03477a08b1f8931bb56343bc5b8ab833130a252b69e8` |
| E9 | `crates/gclient/src/ui/splash.rs:21–45`: `22\| let goblin = marks::goblin_large();`; `23\| let wordmark = marks::wordmark();`; `38\| marks::render_mark(frame, (x, y), goblin, &palette);`; `39\| marks::render_mark(frame, (x + WORDMARK_X, y + WORDMARK_Y), wordmark, &palette);` | `fc61e5f871c93cebc891cc6a5262fb33aa247d5f3a04e1065e6d09e6d9921496` |
| E10 | `crates/gclient/src/app/startup_stages.rs:62–105`: `102\| StageState::Running { since } => Some(self.now.saturating_duration_since(since)),` | `5374c3296e10d09d453e3f9a43e76e6822fbf150aa03e587b80eb229650a0967` |
| E11 | `crates/gclient/src/app/live_loop/startup.rs:24–30`: `26\| daemon.reconnect(Generation(0)).await?;`; `27\| let version = daemon.config_version().await?;` | `e790697abe8c7b9271ab64a2c89f9a82e8600368320903e29a588a74a26bbcb0` |

E11 was read with complete native evidence, no warnings, from main HEAD
`f0e2cf61797bcc47bafed2149bd2221576761fbb`, tree
`408491a845c8e8e2a7e0de859ed504aa504ecbc4`; its file SHA-256 is
`5557615f661a9cda69ba2ffd67987032cd431bf1503fdf4caa55d3a78cc4cf99`.
That source read explains the stage boundary; it does not identify the build
commit of the installed binary.

_Last verified: 2026-10-02_
