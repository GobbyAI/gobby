# Exec Storms And Security-Service CPU (macOS)

When XprotectService, syspolicyd, trustd, tccd or an EDR extension are hot together,
something is launching processes fast. Find the launcher; the security services are the
symptom.

## Why They Light Up Together

EndpointSecurity clients (XProtect's behavioral service, a corporate EDR) subscribe to
the kernel's `exec`, `open` and `rename` events. Their CPU tracks the system-wide event
rate:

| Process | Work per event |
|---|---|
| XprotectService | Behavioral malware scan per launch |
| syspolicyd | Gatekeeper code-signing assessment per new binary |
| trustd, tccd | Certificate checks, privacy checks |
| EDR extension | Corporate policy per launch or open |
| kernel_task | Syscall and interrupt load |

A brand-new, unsigned binary costs much more than a repeat launch of a known one. Build
systems that link fresh test executables on every change pay the full price each time.

XProtect has no user exclusions, and you never disable or reconfigure security tools.
Reduce the event rate instead.

## Diagnose (Root: The Operator Runs These)

1. Who is taxed, and is it launch churn:

   ```bash
   sudo powermetrics --samplers tasks --show-process-energy -n 1 | head -45
   ```

   High `DEAD_TASKS` means many processes started and exited inside the sample. The
   security cluster elevated together is the storm signature.

2. Name the launcher. `eslogger` reads the same stream the security services read.
   In an `exec` event, `.process` is the launching process and
   `.event.exec.target` is the program it started:

   ```bash
   sudo eslogger exec 2>/dev/null | head -400 | jq -r '.process.executable.path' | sort | uniq -c | sort -rn | head -15
   sudo eslogger exec 2>/dev/null | head -400 | jq -r '.event.exec.target.executable.path' | sort | uniq -c | sort -rn | head -15
   ```

   If 400 lines arrive in a second or two, the rate itself is the problem. If the stream
   trickles while a security service stays hot, that service is wedged.

3. Without root, an estimate: count short-lived processes across a few `ps` snapshots,
   and read `pgrep -c` for known launchers.

## Common Generators On An Agent Host

| Generator | Fingerprint | Upstream fix |
|---|---|---|
| Hook binary per tool call (`ghook`) | One launch per agent action | Fewer concurrent agents, or a long-lived hook path |
| Test executables | Many distinct paths under a build root's `deps/` | One test program per crate; reuse builds across trees (sccache) |
| Build scripts and proc macros | `build-script-build`, many `rustc` | Shared caching; avoid rebuilding unchanged crates |
| Agent CLIs launching `git`, `node`, `uv` | Repeated short-lived tools | Collapse duplicate sessions; cache status polls |
| `uvx --from git+…` tools | Re-resolve a git source each launch | Install once with `uv tool install` |
| `brew` auto-update loops | `portable-ruby`, Homebrew `bash` | `HOMEBREW_NO_AUTO_UPDATE=1`; remove the poller |
| Status tools on a tight timer | Same command every second | Longer interval or a cache |

## Signing And Developer Tools

- Adding a terminal to System Settings > Developer Tools changes how Gatekeeper treats
  binaries that terminal launches. It does not stop XProtect's per-launch scan, so it
  rarely removes the load alone.
- Ad-hoc signing changes whether syspolicyd rejects a binary, not whether XProtect looks
  at it. Whether a Developer ID signature lowers scan cost is unverified; measure before
  recommending it.

## A Wedged Extension

When `eslogger` trickles while a security extension stays hot, the extension itself is
stuck. Don't restart, kill or reconfigure it. Report the evidence (its CPU, wakeups and
the quiet event stream) to the operator for IT or the vendor. Old versions listed by
`systemextensionsctl list` as waiting to uninstall use no CPU; they are never the cause.

## Output

Report the event rate, the top launchers with counts, the security services' CPU, and
the upstream change that lowers the rate. Before and after numbers use the same window.

_Version: 1.0.1_
_Last verified: 2026-11-06_
