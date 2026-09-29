# Hook response-phase stalls — no-fanout evidence pass (2026-09-28)

Task: `gobby#23065` (research spike, Lane 10 / DeepSeek trial). Read-only; no daemon
instrumentation, no fanout, no restart, no agent spawn. Companion to `gobby#23063`
(rule_engine latency, Lane 3-owned).

## Verdict

The `response` phase does **not** name a slow collaborator. It is the residual
`total_seconds − Σ(measured phases)` in `HookPhaseTimings.snapshot`, so it absorbs any
wall time the loop spent outside the metered phases — including event-loop starvation
caused by a *different* request. The 20:23:49 cluster is a coherent example: three hooks
on three different sessions stalled in the same second with **near-zero**
`admission_wait` and `executor_queue`, which rules out adapter-worker saturation and
points to a shared event-loop block. `rule_evaluation` and `rule_engine` are measured
phases and are small in every cluster sample, so #23063's rule-loop work is not the cause
here.

Strongest supported cause: a daemon-wide event-loop stall (not worker saturation, not
the rule engine). The precise blocker is **not** identified from existing logs; that is
the explicit measurement limit. Existing logs carry no per-hook timing rows (no
debug-level `Hook adapter timing` / `Hook executed` lines at the running log level), and
the `hook_phase_duration_seconds` histogram is aggregate-only, so no *identifying*
per-stall baseline exists — only the aggregate normal baseline used below.

## Samples — natural no-fanout cluster 2026-09-28 20:23:49 (CDT)

Source: `~/.gobby/logs/errors.log` lines 10425–10427 (also duplicated into `mcp.log`);
emitted by `servers.routes.mcp.hooks.execute_hook`, the slow-hook warning at
`src/gobby/servers/routes/mcp/hooks.py:980-995`.

| # | hook_type | source | total_s | dominant | dominant_s | admission_wait | executor_queue | session_resolution | rule_evaluation | rule_engine | response |
|---|-----------|--------|---------|----------|-----------|----------------|----------------|--------------------|-----------------|-------------|----------|
| 1 | PostToolUse | codex | 5.423 | response | 4.318 | 0.0000106 | 0.000375 | 0.264 | 0.674 | 0.016 | 4.318 |
| 2 | pre-tool-use | claude | 5.606 | response | 4.681 | 0.0000585 | 0.000193 | 0.269 | 0.465 | 0.024 | 4.681 |
| 3 | PreToolUse | codex | 5.363 | response | 3.352 | 0.0000363 | 0.000121 | 0.217 | 1.246 | 1.201 | 3.352 |

Session IDs: (1) `f4f077d9-…` (this lane), (2) `081766e5-…`, (3) `1fdb7576-…`.

Arithmetic (sample 1): total 5.42299 − (admission_wait 0.0000106 + executor_queue
0.000375 + session_resolution 0.26448 + rule_evaluation 0.67360 + handler_body 0.02276 +
persistence_broadcast 0.14327) = 4.31849 = reported `response`. The `response` value *is*
the uncovered residual, computed at `src/gobby/hooks/phase_timing.py:51-57`.

## Comparable normal sample (fast baseline)

No natural *fast* sample is logged: the running log level emits no per-hook timing rows,
so every `errors.log` record is a ≥5 s warning. To obtain a genuine normal sample, the
existing natural hook entry point was invoked directly — a real `ghook --gobby-owned`
dispatch (the same client the CLI hooks use) against `POST /api/hooks/execute`, one hook,
`source=droid` / `hook_type=PostToolUse`, no forced fanout, **no new instrumentation**.
Exact phase timings were read from the pre-existing `hook_phase_duration_seconds`
histogram on the daemon's existing `GET /api/admin/metrics` (Prometheus) endpoint;
`count=1` on every phase confirms the sample is exactly this one hook.

| phase | seconds |
|-------|---------|
| admission_wait | 0.0000348 |
| executor_queue | 0.0000417 |
| session_resolution | 0.021047 |
| rule_evaluation | 0.038664 |
| handler_body | 0.105129 |
| persistence_broadcast | 0.016216 |
| response (residual) | 0.016345 |

Timing window 2026-09-29T02:13:06Z → 02:13:07Z (2026-09-28 21:13:06 CDT); the hook
returned `{"continue": true}`. Σ(measured phases) = 0.181134 s, so total =
0.181134 + 0.016345 = **0.197 s**, and the residual `response` is 0.016 s — 8% of total,
below the dominant `handler_body` phase (0.105 s), and ~260× smaller than the 20:23:49
residual (4.318 s). Same arithmetic as the stalled cluster, but a normal hook has a
*small* residual: every phase is metered and accounted for. That is the baseline the
20:23:49 residual lacks.

## Other slow clusters (comparison shapes, not normal)

Same 2026-09-28 timeline, `errors.log`:

- **19:56:09** — lines 10402–10404, a second response-dominated no-fanout cluster (three
  hooks, two sources): codex PreToolUse total 5.378 `response` 4.951; claude
  message-display total 5.222 `response` 4.523; codex PreToolUse total 5.823 `response`
  4.255. Same shape: metered phases (0.43 s, 0.70 s, 1.57 s) small next to the residual.
- **20:11:39–20:11:40** — lines 10420–10424, the contrasting cluster: dominant phase was
  `rule_evaluation` (5.597 s) or `session_resolution` (2.56 s), with `response` small
  (0.126–2.56 s). This is the workers/rule-engine shape, distinct from the 20:23:49
  residual shape.

Log window: `errors.log` covers 2026-09-24 14:26 → 2026-09-28 20:51 (current file);
`mcp.log` holds the same slow-hook records. No `Hook adapter timing` (debug) rows exist at
the running log level, so there is no per-hook baseline beyond the ≥5 s warnings
(`SLOW_HOOK_THRESHOLD_SECONDS = 5.0`, `phase_timing.py:26`).

## Path traced

1. `src/gobby/servers/routes/mcp/hooks.py` — `execute_hook` (:376-995). `start_time` set
   at entry (:389); `phase_timings = HookPhaseTimings()` at :390.
2. `src/gobby/servers/routes/mcp/hooks.py:682` — `_run_adapter_hook` → `run_adapter_hook`.
3. `src/gobby/hooks/adapter_execution.py:176-306` — `run_adapter_hook` measures
   `admission_wait`, `executor_queue`, and (via offload) execution; the worker runs the
   adapter under `hook_phase_timing_scope` (:226).
4. `src/gobby/hooks/hook_manager.py` measures `session_resolution` (:383-418),
   `handler_body` (:516-616), `persistence_broadcast` (:684), `rule_evaluation` (:804).
5. `src/gobby/hooks/phase_timing.py:51-57` — `snapshot` sets
   `durations["response"] = max(0, total − Σ other measured)`.
6. `src/gobby/servers/routes/mcp/hooks.py:974-995` — `observe_hook_phase_timings` picks
   the max phase and logs it when `total_seconds >= 5.0`.

Because `response` is a residual, a slow-hook warning naming it means "some unmetered
wall time", not "response assembly is slow". In each cluster sample the metered phases
account for `total − response` — 1.105 s of 5.423 s (sample 1), 0.925 s of 5.606 s
(sample 2), 2.011 s of 5.363 s (sample 3) — and the matching residual (4.318 s, 4.681 s,
3.352 s) is the unmetered wall time outside the phase meters. That unmetered share is
62–84% of each hook, far more than the metered phases explain.

## Corroborating loop evidence (same file, same window)

- `~/.gobby/logs/daemon.log:15959` (20:23:37) → `:15960` (20:23:53): a 16 s gap in the
  daemon's own INFO log-spine brackets the stall; no long job is logged during it.
- `~/.gobby/logs/daemon.log:15956` (20:23:34) — a terminal **wake dispatch** took
  `duration_ms=3707.5`; `:15964` (20:23:56) another took `duration_ms=3296.4`. Terminal
  wakes run on the loop, so multi-second wake dispatch and multi-second hook residuals
  co-occur in the same interval.
- Near the era, `~/.gobby/logs/daemon.log:16034` (20:27:11) records an in-process
  `code_index_index: 15343.945 ms` during a spawn (`:15939`, 20:21:43, was `0.0`). This
  shows multi-second in-process work can run on the daemon; no matching index block is
  logged at 20:23:49 itself.

## Competing explanations and what the evidence favors

1. **Adapter-worker saturation** — rejected. `admission_wait` ≤ 59 µs and
   `executor_queue` ≤ 0.4 ms across all three cluster samples. The eight-worker pool was
   not the constraint.
2. **Rule engine / rule loop** (#23063 territory) — not the cause here.
   `rule_evaluation` ≤ 1.25 s and `rule_engine` ≤ 1.20 s; both well under the residual.
   The cluster sample with the largest `rule_engine` (3) also has the *smallest* residual —
   inverted from a rule-engine explanation.
3. **Slow response assembly (hold-open / receipt persistence)** — not supported.
   `persistence_broadcast` ≤ 0.167 s; no hold-open interaction was created (no web-chat
   session). The residual has no metered owner.
4. **Daemon-wide event-loop stall** — favored. Three independent sessions across two
   sources (codex, claude), same second, near-zero admission/queue, large unmetered
   residual, bracketed by a daemon log-spine gap and concurrent multi-second loop work
   (terminal wake dispatch).
   A per-session or per-worker cause cannot produce simultaneity across three sessions.

## Measurement limit

Existing logs cannot name the blocker. The running level emits neither the
`Hook adapter timing` debug row (`adapter_execution.py:296-308`) nor the
`Hook executed` debug row, so the only per-hook log records are the ≥5 s warnings. The
`hook_phase_duration_seconds` histogram supplies an *aggregate* baseline — enough to
prove a normal hook leaves a small residual (see the fast sample above) — but it is not
per-hook-labeled or timestamped, so it cannot attribute the *stalled* second itself. A
natural stall would need a one-shot out-of-process sample (e.g. `py-spy dump --pid
<daemon>`) at the instant of a stall; none occurred during this single evidence pass, and
no daemon instrumentation was added. Per task constraints, the pass stops here.

## Handoff

No concrete defect was found in a Lane 3-owned hook path: the rule engine and rule loop
are not implicated (`rule_evaluation`/`rule_engine` are small; see #23063 for the separate
rule-loop work). The residual's source is daemon event-loop scheduling, which is not
Lane 3's in-flight `src/gobby/workflows/engine/*` scope — so no Lane 3 defect handoff is
warranted from this pass. The finding is recorded here for the daemon-stability queue;
the actionable candidates (bound terminal-wake loop work; emit the existing debug timing
rows in production on a sampled basis) are options, not defects proven in this pass.
