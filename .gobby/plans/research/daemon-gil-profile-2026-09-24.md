# Daemon GIL baseline, 2026-09-24

PID 22056, CPython 3.14.7. [GIL flamegraph](daemon-gil-profile-2026-09-24.svg) records 2,871 GIL-holder samples over 60 seconds with `py-spy record --gil --threads --nonblocking`.

| Signal | Baseline |
| --- | ---: |
| Loop-lag watchdog events in one day | 372 |
| Mean / maximum lag | 0.71 s / 3.1 s |
| Events over 1 s | 135 |
| `GET /api/agents/runs` share of GIL samples | 23% |
| Main-loop share | 23% |
| Hooks and rule engine share | about 15% |

The main-loop share includes about 9% under the gzip middleware frame; 191 of those 254 samples serialize the agents/runs body through Pydantic. Attention and roster snapshots account for about 7%, and the WebSocket proxy relay and canonical JSON for about 4%. The native sample showed roughly 60 threads in 15 pools, with Python workers waiting in `take_gil`.

| `/api/admin/metrics` at about one hour uptime | Calls | Mean wall time | Total wall time |
| --- | ---: | ---: | ---: |
| `agents/runs` | 2,023 | 1.64 s | 3,310 s |
| `hooks/execute` | 3,486 | 0.94 s | 3,288 s |

Polling reads across agents/runs, attention/roster, sessions and source-control accounted for about 11,000 of 16,000 requests. One `GET /api/agents/runs?project_id=…` poll returned 8.6 MB for 50 rows, 48 finished. The list carried about 3 MB of result, 2.5 MB of resume metadata and 2 MB of prompt, plus summary markdown. D0 changes the list query and response contract; G1 repeats this profile and metrics under comparable load.

## After D0, 2026-09-25

The restarted daemon (PID 22988) served the same project-filtered 50-row poll in 83,303 bytes, about 99% below the 8.6 MB baseline. No list row or sampled detail response contained the child session's `summary_markdown`. A five-call `limit=200` check returned 200 rows in about 333 KB, with a 74 ms mean local round trip. Ten `limit=50` calls averaged 95 ms and peaked at 246 ms, all HTTP 200. Prometheus recorded 104 successful agents/runs calls totaling 24.64 s, or 237 ms per call, versus 1.64 s in the baseline hour. The post-restart sample covers about 20 minutes, so the aggregate comparison is indicative rather than matched for uptime or request mix.

The requested 60-second GIL flamegraph remains outstanding: `sudo -n py-spy` required an administrator password, and unprivileged `py-spy` reported that root is required on macOS. A five-second native `sample` succeeded, but it cannot establish a comparable Python GIL-holder percentage. G1 must not infer that `load_runs` fell near zero from the response and wall-time improvements alone. The post-restart loop-lag histogram observed 133 beats at or above 0.25 s in about 20 minutes; its threshold and sampling differ from the baseline watchdog count, so these counts are not directly comparable.
