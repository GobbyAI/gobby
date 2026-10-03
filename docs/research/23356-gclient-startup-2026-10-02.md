# gclient startup timing and loading indicator

Research checkpoint for **#23356 — Measure where gclient's ~20 s startup goes and
recommend a loading-progress indicator**. Researcher 2, 2026-10-02 CT.

## Findings

The three most recent complete launches recorded in `gclient.log.20728` took
**8.88, 26.38 and 16.69 seconds** on the existing startup clock. The roster stage
took **8.76, 25.28 and 16.25 seconds**, respectively: **95.84–98.58%** of the sum of
the recorded stage durations. This reproduces the reported order of delay from
natural launches without opening another client or touching its panes.

The source confirms avoidable serial work inside that stage: unresolved pane IDs
are captured before the relist response is installed, then each ID is fetched
individually after the relist completes, including IDs already returned by that
relist. The current project's hidden tabs participate. Reusing the returned
metadata is the smallest correction to investigate first. The logs establish the
dominant stage; they do **not** measure the exact fraction attributable to these
individual requests. Source evidence is listed below.

Recommend showing the **actual phase, terminal completion count and elapsed
time** on the existing splash. A fixed countdown is unsupported by a sample
spanning 8.88–26.38 seconds. Use the existing stage state, with real completion
updates for its long roster phase. This design is for Josh's approval; it does
not authorize UI implementation.

## Measurements and boundaries

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

No synthetic launch was started. At 19:11 CT the Orchestrator announced both the
close-queue freeze and the active load hold. Respecting that hold prevented new
29-pane isolated runs. Missing subphase numbers remain missing; there are no
invented zeros or timings derived by dividing roster time by 29.

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

## Indicator design for approval

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

The smallest implementation direction, **after approval**, is the existing stage
model plus progress updates from metadata resolution and focused-pane readiness.
It needs no second startup subsystem, new log lines or new telemetry store. The
current splash renderer itself draws only the marks [E9].

## Defect ownership and next measurement

L1 `gobby#14909` received the source and timing finding in durable message
`46d6b97f-72cd-4d46-8c4b-db6ca0adf603` and explicitly acknowledged ownership of the
redundant serial lookup defect. L1 holds implementation under the close-queue
freeze/load hold. This research changes no Rust or UI code.

The note and sketch are ready for review. The stricter per-tab/host and external
paint measurement remains outstanding. Once an isolated-run window is allowed:

1. Use a temporary home, isolated test-hub schema, private ports and a separate
   gterm host, following `tests/e2e/conftest.py`; create no live workspace/panes.
2. Run the installed gclient inside its own hidden PTY, with 29 inert test panes
   across multiple tabs. A harness outside the process timestamps daemon
   connection, workspace reply, roster/attention requests, every terminal lookup,
   per-pane host connection/attach and the first non-splash output. Add no logs
   to production source. Run at least three complete launches.
3. Preserve exact binary identity and pane/tab counts, report overlapping spans,
   and compare the serial lookup chain with L1's corrected behavior only when
   that correction exists. A mock/replay run must be identified as such, not
   substituted for production service latency.
4. Report readiness separately from background attachment completion. Stop only
   research-owned test processes after coordination; never quit Josh's clients.

No live storage SELECT, restart, focus transfer, process termination, new log
line or production workspace/pane creation occurred during this checkpoint.

## Source evidence

Native `gcode evidence` reads below were complete, with no warnings, at source
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
