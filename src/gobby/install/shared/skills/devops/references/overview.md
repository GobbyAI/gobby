# DevOps Overview

How this seat works on any machine and project, the load model, and which topic to
load for each symptom.

## Three Layers

Every machine and project differs. Keep the layers apart:

| Layer | Holds | Lives in |
|---|---|---|
| This skill | Method, tools, Gobby runtime behavior | Bundled; the same for every user |
| Host profile | This machine: OS, chips, memory, disks, volumes, toolchains, container runtime, measured baselines | Your persistent workspace, `host-profile.md`; never committed |
| Project facts | What the project builds and runs: languages, build commands, compose files, build docs | The repository's own docs and config; read, never assumed |

Start every assignment by loading `host-profile.md` from this skill and reading or
refreshing your host profile. Never assume a path, volume, tool or service exists
until the profile or a command shows it.

## Gobby Runtime On Any Host

These come with Gobby itself, so they hold on every machine:

| Component | Load it creates |
|---|---|
| Daemon (`gdaemon` front door plus Python) | CPU per hook and MCP call; database queries |
| `ghook` | One process launch per agent tool call |
| `gcode` | A launch per search; indexing CPU after commits |
| `gterm` / `gclient` | PTY traffic; gclient lag usually shows a daemon stall |
| Agent CLIs under the sandbox | Launch `git`, `node`, `uv` and build tools |
| Optional Docker services (Postgres, FalkorDB, Qdrant) | Container VM CPU and memory where Docker runs them |

Gobby paths: logs in `~/.gobby/logs/`, binaries in `~/.gobby/bin/`, worktrees in
`~/.gobby/worktrees/<project>/`, agent build caches under `~/.gobby/cache/` unless the
project configures another build root. Never read `~/.gobby/bootstrap.yaml`,
`~/.gobby/local_cli_token` or any `.secret_kek`.

## The Load Model

Host load under an agent fleet is mostly per-event work, multiplied by the agent count:

1. **Process launch rate.** On macOS, XProtect, syspolicyd and trustd inspect each
   launch; on Linux, an EDR or auditd does the same. Load `exec-storms.md`.
2. **New executables.** Compiled test programs and build scripts are fresh, unsigned
   binaries. Their first launch costs a full scan on macOS.
3. **File-system churn.** Build output is GBs of short-lived files. File watchers,
   indexers (Spotlight, fseventsd) and backup tools follow every write. Load
   `rust-worktree-builds.md` for Rust; the same reasoning applies to other toolchains.
4. **Container VM.** It reserves host memory and burns CPU. Load `docker-local.md`.

When the host is slow, find which of these dominates right now, with numbers, then name
the upstream generator.

## Symptom To Topic

| Symptom | Load |
|---|---|
| Fans up, everything slow, unknown cause | `macos-performance-triage.md` |
| Security services hot together | `exec-storms.md` |
| File-watcher or indexer CPU; builds slow; build folders huge | `rust-worktree-builds.md` |
| Disk filling; free space and `du` disagree | `disk-usage.md` |
| "Did the change help?"; ramping concurrent teams | `benchmark.md` |
| Container slow, out of memory, or stale containers | `docker-local.md` |

The macOS references name macOS tools. On Linux use the equivalents: `top`/`pidstat`
for CPU, `perf` for profiles, `iostat` and `df` for disk, `auditd` or the EDR's own
console for launch storms.

## Report Shape

Send findings to the seat that assigned the work:

```markdown
## Finding: <one line>
- Measured: <command>, <window>, <local time>: <numbers>
- Cause: <generator and evidence>
- Fix: <upstream change>, owner <seat or operator>
- Needs operator: <root commands to run with !, or approvals>
```

Keep raw captures in your workspace and send the numbers, not the dump. Record a new
baseline or host fact in your host profile.

## Limits

- Root-only probes go to the operator to run with `!`. Give the exact command with a
  bounded sample (`-n`, `head`, `timeout`).
- Experiments run only in your workspace or on a scratch location the host profile
  names as approved.
- Stopping containers, pruning, deleting build folders or killing processes needs the
  operator's approval for each item.
- No system settings, security tools, keychains, certificates or credentials.
- No repository source edits. Recommend fixes; a developer team implements them.

_Version: 1.0.0_
_Last verified: 2026-11-06_
