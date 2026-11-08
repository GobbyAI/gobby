# Benchmarks And Before/After Measurement

Prove a change helped with the same measurement before and after, and find how many
concurrent agents a host carries.

## Baseline First

Record an idle baseline in the host profile before any experiment:

```bash
uptime
ps -Aceo pcpu,comm -r | head -15
macmon pipe -s 5 -i 1000 2>/dev/null | tail -1
memory_pressure | tail -1
df -h
```

Note the time, what was running (agent count, builds), and the container VM's CPU.

## A/B A Build Change

Compare one setting at a time on a quiet host:

1. Pick a fixed build: the same commit, crate set and command.
2. Clean the target directory, or point each run at a fresh one.
3. Run each variant at least twice and keep both numbers.

```bash
hyperfine --warmup 1 --runs 3 --prepare 'rm -rf <scratch-root>' \
  'CARGO_TARGET_DIR=<scratch-root> <variant A command>' \
  'CARGO_TARGET_DIR=<scratch-root> <variant B command>'
```

Also capture, per variant: wall time, build-folder size,
executable count in `deps/`, and peak CPU of the security services and the file
watcher (sampled with `ps` every few seconds into your workspace).

## Concurrent-Agent Ramp Test

To find the host's real capacity under agent load, step the load up and hold each step:

| Step | Load | Hold |
|---|---|---|
| 0 | Idle | 5 minutes |
| 1 | N teams building | Until each finishes one build and one test run |
| 2 | 2N teams | Same |
| 3 | 3N teams | Same |

At each step record: load average versus core count, security-service CPU, file-watcher
CPU, container VM CPU, daemon latency (time a cheap MCP call or health request), build
and test wall times, and memory pressure. Stop the ramp when daemon latency or build
time degrades by more than half; the step before is the safe capacity.

Pick N with the operator. A ramp changes the fleet, so the coordinator runs it; you
measure and report.

## Scoring

- A change helps when the same workload shows lower cost on two runs each.
- Report raw numbers with the spread; never one run.
- Separate measurement from inference: say which numbers you saw and which you
  concluded.
- Store each run's results in your workspace with the date and command, and add the
  baseline to the host profile.

_Version: 1.0.1_
_Last verified: 2026-11-07_
