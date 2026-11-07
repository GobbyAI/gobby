---
name: devops
description: "Host performance and build-infrastructure diagnostics for any machine running a Gobby agent fleet (macOS tools first, Linux equivalents noted). Attributes load (CPU, exec storms, security services), measures build churn and disk use, and checks local container services. Use when the host is hot or slow under agent load, builds stall, disk fills, or a container misbehaves."
version: "1.0.0"
category: operations
triggers: host load, high cpu, xprotect, syspolicyd, fseventsd, exec storm, eslogger, powermetrics, disk full, cargo target, sccache, build churn, docker, container, falkordb, qdrant, postgres container
sources:
  - "Adapted from laurigates/claude-plugins (MIT, Copyright 2026 Lauri Gates) at 34c704bb: macos-plugin endpoint-security-cpu, macos-performance-triage, macos-performance-benchmark, macos-disk-usage; rust-plugin cargo-worktree-builds; container-plugin container-development. See NOTICE.md."
metadata:
  gobby:
    audience: all
---

# devops

Diagnose and measure the machine Gobby runs on. Load comes from many agents at once:
each tool call runs `ghook`, builds launch compilers and test executables, and the
daemon talks to its database and optional container services. Find what drives the
load, measure it, and report evidence with a recommended fix.

This skill is the same for every user. Your machine's details live in your host
profile, and the project's in its own docs and config; never assume either.

## Ground Rules

- Measure before and after. Every claim names its command, sample window and local time.
- Read-only by default. Commands that need root go to the operator to run with `!`.
- Delete, stop, prune or kill only with the operator's approval for that item.
- Never change system settings, keychains, certificates, MDM or security tools.


## Topics

Load each with `get_skill_file(name="devops", path="references/<file>")`. Always load
`overview.md` and `host-profile.md` first.

| File | Load for |
|---|---|
| `overview.md` | The three layers, the load model, symptom-to-topic map, report shape |
| `host-profile.md` | Discovering and recording this machine |
| `macos-performance-triage.md` | What is hot right now |
| `exec-storms.md` | Security services hot together; naming the launcher |
| `rust-worktree-builds.md` | Build roots, sccache, debug info, test programs, build volume |
| `disk-usage.md` | APFS measurement and tiered cleanup |
| `benchmark.md` | Baselines, A/B runs, the concurrent-agent ramp test |
| `docker-local.md` | Local container resources, health and stale containers |
