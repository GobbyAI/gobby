# ghook User Guide

ghook is the sandbox-tolerant hook dispatcher Gobby uses to receive lifecycle and tool-use events from host AI CLIs (Claude Code, Codex, Qwen CLI, Factory droid, Grok, Antigravity CLI). It enqueues an envelope to `~/.gobby/hooks/inbox/` *before* attempting to POST to the local Gobby daemon — so the daemon's drain worker can replay any envelope whose POST was lost to a sandbox FS-read denial, a network blip, or a daemon restart.

You don't usually invoke ghook directly. The Gobby installer wires it into each host CLI's hook configuration. This guide explains what it does, how to verify it's working, and how to wire it manually if you need to.

## Installation

If you use [Gobby](https://github.com/GobbyAI/gobby), ghook is already installed and wired into your supported AI CLIs.

Otherwise, install from a release binary or crates.io:

```bash
cargo install gobby-hooks
```

The binary is named `ghook` (the package is `gobby-hooks` to disambiguate from singular use; the binary stays short).

### First run on macOS

Binaries downloaded from a GitHub release archive via a browser land with the `com.apple.quarantine` extended attribute, which Gatekeeper rejects on first run with "ghook cannot be opened because the developer cannot be verified." The binaries are already ad-hoc signed by the Rust linker; the quarantine flag is the blocker, not the signature. Clear it with:

```bash
xattr -d com.apple.quarantine ~/.gobby/bin/ghook
```

Or right-click the binary in Finder and choose *Open* once to approve it. `cargo install gobby-hooks` is unaffected — locally compiled binaries never get the quarantine attribute. Archives fetched via `curl`/`wget` also skip it.

## How It Works

```text
host AI CLI fires hook
  └─ runs ghook --gobby-owned --cli=<c> --type=<t>
      ├─ Stop only: planned-shutdown marker + daemon health preflight
      │   └─ fresh marker + unreachable daemon → {"continue":true}; no stdin/enqueue
      ├─ resolves project root (walk up from cwd to .gobby/project.json)
      ├─ reads stdin (the host CLI's hook payload)
      ├─ stamps machine_id + os, or machine_id_error when unavailable
      ├─ enriches input_data with terminal_context (when TMUX_PANE is valid)
      ├─ writes envelope atomically to ~/.gobby/hooks/inbox/
      └─ POSTs Python-compatible hook payload to the Gobby daemon
          ├─ 2xx → delete inbox file, return Python-dispatcher-compatible stdout/stderr/exit
          └─ failure → leave inbox file, return Python-dispatcher-compatible stdout/stderr/exit
                       └─ daemon's drain worker replays on next tick
```

Spool-first ordering is the whole point. If anything between ghook and the daemon goes wrong (sandbox FS denial, network blip, daemon restart), the envelope is already on disk and the daemon will pick it up on its next drain pass. Replay is invisible to the host CLI; the host-visible result follows the current per-CLI hook protocol.

### Planned Shutdown Stop Handling

When Gobby intentionally stops or restarts the daemon, a host CLI may fire a
Stop hook after the daemon has already exited. For Stop hooks only, `ghook`
checks `$GOBBY_HOME/shutdown_intent_active.json` before project lookup, stdin
reads, terminal-context injection, or enqueue.

A marker is accepted when its `timestamp` is fresh and either its `intent` is
`stop` or `restart`, or its `source` starts with `cli_`, `http_`, `service_`, or
`mcp_`.
If `{daemon_url}/api/admin/health` is unreachable during that fresh window,
`ghook` prints `{"continue":true}` and exits 0. This check is an aliveness
probe only: any HTTP response from the endpoint counts as reachable, including
4xx/5xx, and does not imply the daemon is healthy.

If the daemon dies after enqueue but before the live Stop POST completes,
`ghook` suppresses only `Connect` and `Timeout` failures with a fresh marker. It
deletes the just-enqueued Stop envelope first; delete failures, stale markers,
HTTP errors, and non-Stop hooks keep the normal fail-closed behavior.

Environment knobs:

- `GOBBY_DAEMON_URL` overrides the daemon URL used for Stop preflight, live
  POSTs, and statusline POSTs.
- `GOBBY_PORT` overrides only the port, dialed on `127.0.0.1`, when
  `GOBBY_DAEMON_URL` is not set.
- `GOBBY_HOME` controls marker lookup; default is `~/.gobby`.
- `GOBBY_SHUTDOWN_HOOK_ALLOW_SECONDS` overrides freshness when it is a positive
  number; default is 120 seconds.

### Terminal Context

When `TMUX` is set and `TMUX_PANE` matches `^%\d+$`, `ghook` adds
`input_data.terminal_context.tmux_pane` to the envelope for every `--cli` value.
The pane ID is passed through exactly as provided by tmux. If `TMUX_PANE` is
unset, empty, or invalid, `terminal_context` is omitted.

## CLI Surface

ghook has three modes. Exactly one must be selected.

```text
ghook --gobby-owned --cli=<c> --type=<t> [--detach]
ghook --diagnose    --cli=<c> --type=<t>
ghook --version
```

| Flag | Mode | Purpose |
|------|------|---------|
| `--gobby-owned` | dispatch | Normal hook invocation. Reads stdin, enqueues, attempts POST. |
| `--diagnose` | introspection | Prints a JSON snapshot of what *would* happen. No network, no envelope write. |
| `--version` | metadata | Prints version and writes `~/.gobby/bin/.ghook-runtime.json` for the daemon. |
| `--cli` | required for dispatch/diagnose | Host CLI name: `claude`, `codex`, `qwen`, `droid`, `grok`, `agy`. Case-insensitive. |
| `--type` | required for dispatch/diagnose | Hook type. CLI-specific (e.g. `session-start` for Claude, `SessionStart` for Codex/Qwen/Agy, `PreToolUse`, `PostToolUse`, `Stop`, `pre-compact`, `session-end`). |
| `--detach` | dispatch | After enqueue and project-root walk-up, call `setsid(2)` to escape the host CLI's process group before the POST. Useful for hooks where the host CLI tears down its session immediately. |

### Exit Codes

| Code | Meaning |
|------|---------|
| `0` | Success, including non-Stop deny/block responses returned as JSON for Codex/Qwen. |
| `1` | Non-critical hook failure returned as JSON error output. |
| `2` | Critical hook failure or blocked critical hook returned as stderr. |

The inbox/replay path is still enqueue-first, but host-visible stdout/stderr/exit behavior follows the current per-CLI hook protocol rather than exposing transport details.

## Wiring ghook into Claude Code

Most users get this configured automatically by the Gobby installer. To wire it manually, add hook entries to your Claude Code `settings.json`:

```json
{
  "hooks": {
    "SessionStart": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "ghook --gobby-owned --cli=claude --type=session-start"
          }
        ]
      }
    ],
    "SessionEnd": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "ghook --gobby-owned --cli=claude --type=session-end"
          }
        ]
      }
    ],
    "PreToolUse": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "ghook --gobby-owned --cli=claude --type=PreToolUse"
          }
        ]
      }
    ],
    "PostToolUse": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "ghook --gobby-owned --cli=claude --type=PostToolUse"
          }
        ]
      }
    ],
    "PreCompact": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "ghook --gobby-owned --cli=claude --type=pre-compact"
          }
        ]
      }
    ]
  }
}
```

Claude Code uses lowercase-hyphenated names internally for some hooks (`session-start`, `pre-compact`, `session-end`) and PascalCase for others (`PreToolUse`, `PostToolUse`). ghook treats `--type` as an opaque string, so pass the exact identifier the daemon expects for that CLI.

Lifecycle hook criticality (`session-start`, `session-end`, `pre-compact`) comes from ghook's per-CLI registry. Tool-use hooks are non-critical — the envelope still spools, but a transient daemon outage won't block your tool call.

### Codex, Qwen, Droid, Grok, Antigravity CLI

Same pattern with different `--cli` and `--type` values. ghook's per-CLI
registry (see `crates/ghook/src/cli_config.rs`) defines which hooks are
critical. Terminal-context enrichment is CLI-agnostic and only depends on valid
tmux pane env vars.

| CLI | Critical hooks |
|-----|----------------|
| `claude` | `session-start`, `session-end`, `pre-compact` |
| `codex` | `SessionStart`, `Stop` |
| `qwen` | `SessionStart` |
| `droid` | none |
| `grok` | `session_start`, `session_end`, `pre_compact`, `stop` |
| `agy` | `SessionStart`, `Stop` |

Grok uses native snake_case hook types (e.g. `session_start`, `session_end`, `pre_compact`, `stop`, `pre_tool_use`) — distinct from Claude's hyphenated names and the PascalCase names the other CLIs use. Its malformed-JSON exit code is `2`.

Droid uses PascalCase hook types (`SessionStart`, `PreToolUse`, `PostToolUse`, `UserPromptSubmit`, `Notification`, `Stop`, `SubagentStop`, `PreCompact`, `SessionEnd`) and ghook forwards droid's stdin payload unchanged to the daemon with `source: "droid"`. Droid-specific block handling differs slightly from the other CLIs: daemon responses containing `continue:false` exit 2, while other meaningful response JSON is written to stdout with exit 0.

Antigravity CLI uses PascalCase hook types (`SessionStart`, `UserPromptSubmit`, `PreToolUse`, `PostToolUse`, `Stop`) and ghook forwards them with `source: "agy"`. `SessionStart` and `Stop` are fail-closed critical hooks; prompt and tool hooks are non-critical.

Gemini CLI support has been removed. Diagnose mode reports `--cli=gemini` as unrecognized, and stale Gobby-owned Gemini hook invocations no-op with `{}` and exit 0 before enqueue or POST.

Unknown `--cli` values fall back to conservative Claude-like dispatch behavior on the live path. Diagnose mode still reports unknown CLIs as unrecognized.

## Diagnose Mode

`ghook --diagnose` is the fastest way to confirm a hook is wired correctly. It runs the same configuration resolution as `--gobby-owned` but skips the network and the envelope write — pure introspection.

```bash
$ ghook --diagnose --cli=claude --type=session-start
{
  "schema_version": 2,
  "ghook_version": "0.7.0",
  "cli": "claude",
  "hook_type": "session-start",
  "source": "claude",
  "critical": true,
  "terminal_context_enabled": true,
  "daemon_url": "http://127.0.0.1:60887",
  "daemon_host": "127.0.0.1",
  "daemon_port": 60887,
  "project_root": "/Users/josh/Projects/gobby-cli",
  "project_id": "3bf57fe7-2a0c-4074-8912-a83d9cd4df01",
  "terminal_context_preview": {
    "parent_pid": 72441,
    "tty": "/dev/ttys005",
    "tmux_pane": "%179",
    "tmux_socket_path": "/private/tmp/tmux-501/default",
    "term_program": "tmux",
    "...": "..."
  },
  "cli_recognized": true,
  "install_method": "github-release",
  "install_source_url": "https://github.com/GobbyAI/gobby-cli/releases/download/ghook-v0.7.0/ghook-aarch64-apple-darwin.tar.gz"
}
```

Look for:

- **`cli_recognized: true`** — confirms ghook knows about this CLI explicitly. Unknown CLIs fall back to conservative Claude-like live dispatch behavior.
- **`critical: true/false`** — does ghook consider this hook type critical under the current per-CLI hook protocol?
- **`terminal_context_enabled: true`** — this recognized CLI can receive terminal context. `terminal_context_preview` is populated only when the current process has `TMUX` and a valid `TMUX_PANE`.
- **`daemon_url`** — where will the POST go? If this is wrong, check
  `GOBBY_DAEMON_URL`, `GOBBY_PORT`, then `~/.gobby/bootstrap.yaml`.
- **`project_root` / `project_id`** — did ghook correctly walk up from cwd to the project? `null` means no `.gobby/project.json` was found — daemon will receive the envelope without an `X-Gobby-Project-Id` header.
- **`install_method` / `install_source_url`** — how this `ghook` binary got installed (e.g. `github-release`, `crates-binstall`, `cargo-install`). Both are `null` when the binary was installed without a sidecar-writing installer (e.g. plain `cargo install gobby-hooks`). Useful in bug reports — it tells maintainers exactly which install path a user is on.

The diagnose JSON is validated against `crates/ghook/schemas/diagnose-output.v2.schema.json` in tests, so the schema is stable.

### Machine Identity

Every dispatched JSON object gets the local machine identity before enqueue:

- `machine_id` — value from the local Gobby machine identity file.
- `os` — normalized local OS name.
- `machine_id_error` — present instead of `machine_id`/`os` when the identity
  file is missing, empty, or unreadable.

This stamp replaces any stale `machine_id` or `os` supplied by the host CLI so
the daemon can route machine-scoped sessions and diagnostics consistently.

## Inbox & Replay

Envelopes spool to `~/.gobby/hooks/inbox/<prefix>-<ts13>-<uuid>.json`:

| Filename part | Meaning |
|---------------|---------|
| `prefix` | `c` (critical) or `n` (non-critical) — lets the drain worker prioritize critical hooks first |
| `ts13` | 13-digit zero-padded ms since epoch — gives lex-sortable filenames so drain order matches enqueue order |
| `uuid` | Random v4 — disambiguates within the same millisecond |
| `.tmp` suffix | Intermediate write; never a valid replay target. `atomic_write` does write→fsync→rename so the drain only ever sees fully-written envelopes. |

**Don't touch this directory by hand.** The daemon's drain worker owns it. If you need to clear stuck envelopes, stop the daemon first, delete the files, then start it again.

### Quarantine

Malformed stdin (the host CLI sent something that isn't valid JSON) lands in `~/.gobby/hooks/inbox/quarantine/` as a pair of files:

- `<stem>.json` — body containing the raw stdin bytes, base64-encoded.
- `<stem>.meta.json` — sidecar with `reason: "malformed_stdin"`, the JSON parse error, and the same base64 payload.

The drain never replays quarantined envelopes — they surface via `gobby status` and daemon logs so you can investigate.

## Troubleshooting

### `ghook: no mode specified`

You ran ghook without `--gobby-owned`, `--diagnose`, or `--version`. Pick one. The host CLI's hook command should always include `--gobby-owned`.

### `--gobby-owned requires --cli and --type`

Both flags are mandatory in dispatch mode. Check the hook entry in your host CLI's `settings.json`.

### Hook fires but daemon never receives it

1. `ghook --diagnose --cli=<c> --type=<t>` — confirm `daemon_url` is right and the CLI is recognized.
2. `ls ~/.gobby/hooks/inbox/` — if envelopes are piling up here, ghook is enqueuing fine but the daemon isn't draining. Check that the daemon is running.
3. If the inbox is empty too, the host CLI may not be invoking ghook at all. Check the host CLI's hook log/output.

### Hook returns exit 2 unexpectedly

The hook matched a critical dispatcher path and failed or was blocked. The envelope is still spooled — check `~/.gobby/hooks/inbox/` for a `c-...json` file. The daemon will replay it.

### Sandbox FS-read denials (macOS)

The whole point of ghook's design is that this case is survivable. The envelope is written before the POST is attempted, and project-root walk-up happens before any potential `--detach`. If you see the daemon receive the envelope on the *next* hook fire instead of immediately, that's the drain worker doing its job — not a bug.

### Schema version mismatch

Envelopes carry `schema_version: 1`. If the daemon rejects envelopes for being a newer version than it understands, the daemon needs updating. ghook's `--version` command writes `~/.gobby/bin/.ghook-runtime.json` so the daemon can detect this.
