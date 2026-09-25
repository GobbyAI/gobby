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
