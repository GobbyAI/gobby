# macOS Performance Triage

Find what is hot right now and attribute it to a cause (Apple Silicon).

## Method

Use the USE method for each resource: Utilization, Saturation, Errors. A CPU-sorted
`ps` answers utilization only. Also check saturation (load average versus core count,
thermal throttling) and errors (`log show`).

There is no eBPF on macOS, and SIP blocks DTrace on Apple-signed processes. Stay with
the tools below.

## Step 1: What Is Hot

```bash
ps -Aceo pid,pcpu,pmem,comm -r | head -20
uptime
sysctl -n hw.ncpu hw.perflevel0.physicalcpu hw.perflevel1.physicalcpu
vm_stat | head -10
memory_pressure | tail -1
```

Power and per-cluster load without root: `macmon pipe -s 1` (JSON), or the `macmon`
TUI. Install the sudo-free toolkit with `brew install macmon bottom samply hyperfine`
only after asking the operator.

Read the snapshot:

| Top of the list | Meaning | Next |
|---|---|---|
| XprotectService, syspolicyd, trustd, tccd together | Exec storm | `exec-storms.md` |
| fseventsd, mds, mds_stores, backupd | File churn | `rust-worktree-builds.md` |
| rustc, ld, cargo, many test binaries | Build load | `rust-worktree-builds.md` |
| com.apple.Virtualization.VirtualMachine | Docker VM | `docker-local.md` |
| python (daemon), postgres | Daemon or database work | daemon log, `docker-local.md` |
| kernel_task | Thermal management | Find the load it throttles |
| WindowServer | A client app is drawing | Step 2 |

Gobby-specific counts:

```bash
pgrep -c ghook; pgrep -c rustc; pgrep -c cargo; pgrep -fc 'claude|codex|grok'
```

## Step 2: Attribute A Hot WindowServer

WindowServer never starts its own work; a client app's drawing drives it. Per-process
GPU cost (root, give it to the operator):

```bash
sudo powermetrics --samplers tasks --show-process-gpu -n 1 -i 1000
```

A terminal rendering many busy agent panes counts as a client.

## Step 3: Profile A Process

`samply record --pid <pid>` samples a process you own, without root, and opens a flame
graph. For a daemon stall, also capture `sample <pid> 5` and compare against
`~/.gobby/logs/`. Python daemon stalls: `py-spy dump --pid <pid>` needs root on macOS,
so give it to the operator.

## Step 4: Saturation And Errors

```bash
sysctl -n vm.loadavg
pmset -g therm
log show --last 5m --predicate 'process == "kernel" AND eventMessage CONTAINS "throttl"' | tail -20
```

A load average far above the core count with low per-process CPU means work is
waiting on I/O or on one serialized resource. Look for a lock (cargo build lock, the
database) before adding hardware explanations.

## Tool Map

| Need | macOS tool | Root |
|---|---|---|
| Process CPU | `ps`, `top -o cpu`, `btm` | no |
| Power, P/E-core load, temperatures | `macmon` | no |
| Per-process energy, GPU, dead tasks | `powermetrics` | yes |
| Stack samples | `sample`, `spindump`, `samply` | own processes no |
| File-system calls | `fs_usage -w -f filesys <pid>` | yes |
| Command A/B timing | `hyperfine` | no |

## Output

Name the hot processes with CPU, power or GPU numbers, the attributed cause, the tool
tier that produced the verdict, and the recommended fix. Separate a genuine workload
floor (an active build) from a fixable runaway (an orphaned VM, a stuck loop).

_Version: 1.0.0_
_Last verified: 2026-11-06_
