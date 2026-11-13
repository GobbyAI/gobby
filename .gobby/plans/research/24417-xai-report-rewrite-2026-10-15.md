# Grok 1.0.52 on macOS hangs on a mutex when WindowServer is unavailable, instead of failing gracefully

Draft for Josh's review (Orchestrator gobby#15977, 2026-10-15 19:05 CDT). This is the narrower rewrite he chose. Nothing has been filed. It replaces the earlier draft (`24417-grok-post-compaction-ui-freeze-report.md`, Lane 7), which left out our sandbox.

## Summary

We run the Grok TUI inside a macOS Seatbelt sandbox that denies `mach-lookup` for `com.apple.windowserver.active`. Grok's main thread sometimes calls the CoreGraphics event-source flags query (`CGEventSourceFlagsState`). When the WindowServer lookup is denied, that call doesn't return an error. The main thread blocks on a mutex inside SkyLight and the UI freezes for good: no rendering and no input handling. Escape and Ctrl+C don't recover it. Only stopping and resuming the process does.

A terminal app can run without WindowServer access in a sandbox, over SSH, or in other headless sessions. We'd expect Grok to skip or tolerate that query when WindowServer is unavailable, not hang the UI thread.

## Environment

- Grok CLI 1.0.52 stable, macOS arm64.
- Binary UUID `2ECEEAE1-EBE9-3ADA-8375-1192ADB20736`, matching the installed executable (`dwarfdump --uuid`).
- macOS 27.0.1, build `26A434`.
- Interactive TUI in a PTY, launched under a Seatbelt profile (Anthropic sandbox-runtime 0.0.79). The profile has no `allowMachLookup` entry for WindowServer, so `com.apple.windowserver.active` is denied.

## What we observed

- Four UI freezes, each within about one second of a logged sandbox denial of `mach-lookup com.apple.windowserver.active`.
- All four came after a native `/compact`, in long sessions. We don't know whether compaction is what triggers the flags query, or whether it only happens around the same time.
- In the captured occurrence, all 3,897 main-thread samples showed the same stack:

```text
Grok main thread (com.apple.main-thread)
  [stripped Grok frames]
  SLEventSourceFlagsState                 (SkyLight)
  CGSEventSourceForID                     (SkyLight)
  CGSEventSourceShutdown                  (SkyLight)
  std::mutex::lock()                      (libc++)
  _pthread_mutex_firstfit_lock_slow
  _pthread_mutex_firstfit_lock_wait
  __psynch_mutexwait
```

- Process state at the time: `SN+`, 0% CPU. The last agent turn had ended cleanly. SIGTERM ended the process after about ten seconds.
- The sample has no second SkyLight stack showing who holds the mutex.

## What we have not proved

- That the denied lookup causes the mutex wait. The timing correlation is strong, but we haven't caused a hang deliberately.
- That every freeze had this stack. We sampled only one.
- Which Grok code path makes the flags query. The Grok frames are stripped.

## Expected behavior

Grok's TUI shouldn't need WindowServer to render or read input. If a modifier-key or event-source query can't reach WindowServer, Grok should treat it as unavailable and carry on, and never block the main thread.

## Our workaround

For now we'll add a sandbox exception allowing exactly `com.apple.windowserver.active` for Grok sessions. We'd rather not keep it, because it widens what a sandboxed agent can observe.

## Request to xAI

1. Symbolize the main-thread stack for this binary and identify the caller of `CGEventSourceFlagsState`.
2. Make that call conditional or non-blocking, or skip it, when WindowServer is unavailable.
3. Tell us whether a supported setting avoids it.

We can share the redacted full process sample, the sandbox denial log lines and the timing.
