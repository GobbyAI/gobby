# ghook User Guide

ghook receives lifecycle and tool-use events from Claude Code, Codex, Factory
Droid, Grok, Qwen CLI, and AGY. Managed hook dispatch normally enqueues an
envelope to `$GOBBY_HOME/hooks/inbox/` before attempting delivery, so the daemon
can replay an interrupted delivery. If enqueue fails, the live path attempts a
bounded direct POST; `--enqueue-only` cannot use that fallback.

You don't usually invoke ghook directly. The Gobby installer wires it into each host CLI's hook configuration. This guide explains what it does, how to verify it's working, and how to wire it manually if you need to.

## Installation

The Gobby installer installs ghook and wires the selected supported AI CLIs.
Verify the installed binary and provider configuration after installation.

Use the managed [installation procedure](./admin-operations.md) for normal
operation. Contributors can build the package from this checkout:

```bash
cargo build --release -p gobby-hooks
```

The binary is named `ghook`. A contributor build is not an installation: use
the repository's coordinated native cutover and new-inode installation procedure.

### First run on macOS

If macOS refuses to execute the installed binary, inspect the reported signing or
quarantine error and follow the managed [installation and recovery guide](./admin-operations.md).
Do not assume every launch failure is a quarantine problem. Contributor installs
must replace the binary through a new inode, as described above.

## How It Works

```text
host AI CLI fires hook
  └─ runs ghook --gobby-owned --cli=<c> --type=<t>
      ├─ reject unsupported CLI; honor environment/project hook disable
      ├─ resolves project root (walk up from cwd to .gobby/project.json or .gobby/gcode.json)
      ├─ reads stdin (the host CLI's hook payload)
      ├─ resolve payload workspace fallback; skip unmanaged projects
      ├─ Stop/pre-compact: fresh shutdown marker + unreachable daemon → provider skip
      ├─ stamps machine_id + os, or machine_id_error when unavailable
      ├─ enriches input_data with terminal_context (for lifecycle hooks)
      ├─ writes envelope atomically to ~/.gobby/hooks/inbox/
      └─ POSTs Python-compatible hook payload to the Gobby daemon
          ├─ 2xx → map provider action → write + flush stdout → settle inbox file
          │        └─ mapping or stdout failure → retain inbox file for recovery
          └─ failure → leave inbox file, return Python-dispatcher-compatible stdout/stderr/exit
                       └─ daemon's drain worker replays on next tick
```

Replay cannot retroactively deliver a decision to a finished host invocation.
The live invocation follows the provider response protocol. A daemon delivery
receipt replaces the original envelope only after stdout succeeds; the drain
worker then acknowledges that receipt. Without a receipt, successful delivery
removes the file. Mapping, stdout, or receipt-write failures retain the original.

Dispatch skips unmanaged directories without enqueueing. A project marker,
managed environment identity, or payload project identity establishes managed
context; AGY can resolve a marker from `workspacePaths`. Environment project ID
takes precedence over marker ID and payload project ID. `GOBBY_HOOKS_DISABLED=1`
skips before any dispatch side effects. `gobby hooks disable` and `enable`, run
at the project root, set/remove the boolean `hooks_disabled` in `project.json`;
the dispatcher also finds this flag from nested directories and AGY workspaces.

### Planned Shutdown Fail-Open Handling

When Gobby intentionally stops or restarts the daemon, a host CLI may fire a
Stop or pre-compact hook after the daemon has already exited. For Stop and the
three registered pre-compact spellings (`pre-compact`, `PreCompact`, and
`pre_compact`), `ghook` checks
`$GOBBY_HOME/shutdown_intent_active.json` after project/stdin resolution and
managed-context checks, before terminal-context injection or enqueue.

A marker is accepted when its `timestamp` is fresh and either its `intent` is
`stop` or `restart`, or its `source` starts with `cli_`, `http_`, `service_`, or
`mcp_`. An `intent` of `maintenance` uses a 24-hour freshness window; the hub
maintenance CLI refreshes it on run/resume and clears it when the campaign is
released or aborted.
If `{daemon_url}/api/health` is unreachable during that fresh window,
`ghook` emits the provider's skip response and exits 0. This check is an aliveness
probe only: any HTTP response from the endpoint counts as reachable, including
4xx/5xx, and does not imply the daemon is healthy.

If the daemon dies after enqueue but before the live POST completes,
`ghook` suppresses only `Connect` and `Timeout` failures with a fresh marker. It
deletes the just-enqueued Stop or pre-compact envelope first; delete failures,
stale markers, HTTP errors, and other hooks keep the normal fail-closed
behavior.

Environment knobs:

- `GOBBY_DAEMON_URL` overrides the daemon URL used for Stop preflight, live
  POSTs, and statusline POSTs.
- `GOBBY_PORT` overrides only the port, dialed on `127.0.0.1`, when
  `GOBBY_DAEMON_URL` is not set.
- `GOBBY_HOME` controls marker lookup; default is `~/.gobby`.
- `GOBBY_SHUTDOWN_HOOK_ALLOW_SECONDS` overrides freshness when it is a positive
  number; default is 120 seconds. Maintenance markers use their fixed 24-hour
  window.

### Terminal Context

For normalized `SessionStart`, `SessionEnd`, `Stop`, `AfterAgent`, and
`PostInvocation` aliases, `ghook` adds `input_data.terminal_context`. Tool
hooks remain unenriched. Existing provider fields are preserved, and
`gobby_agent_run_id` is stamped from the trusted `GOBBY_AGENT_RUN_ID`
environment variable.

When `TMUX` is set and `TMUX_PANE` matches `^%\d+$`, the pane ID is passed
through exactly as provided by tmux. Missing or invalid tmux data produces null
tmux fields while the remaining process context is still captured.

## CLI Surface

Select the mode matching the operation:

```text
ghook --gobby-owned --cli=<c> --type=<t> [--detach] [--enqueue-only]
ghook --diagnose    --cli=<c> --type=<t>
ghook --version
ghook schema-identity --json
```

| Flag | Mode | Purpose |
|------|------|---------|
| `--gobby-owned` | dispatch | Normal hook invocation. Reads stdin, enqueues, attempts POST. |
| `--diagnose` | introspection | Prints a JSON snapshot of what *would* happen. No network, no envelope write. |
| `--version` | metadata | Prints version and writes `~/.gobby/bin/.ghook-runtime.json` for the daemon. |
| `--cli` | required for dispatch/diagnose | Host CLI name: `claude`, `codex`, `qwen`, `droid`, `grok`, `agy`. Case-insensitive. |
| `--type` | required for dispatch/diagnose | Hook type. CLI-specific (e.g. `session-start` for Claude, `SessionStart` for Codex/Qwen, `PreInvocation`/`PreToolUse` for AGY, `PostToolUse`, `Stop`, `pre-compact`, `session-end`). |
| `--detach` | dispatch | After enqueue and project-root walk-up, call `setsid(2)` to escape the host CLI's process group before the POST. Useful for hooks where the host CLI tears down its session immediately. |
| `--enqueue-only` | dispatch | Durably queue the event and return the provider skip response without live POST. Cannot supply a synchronous decision. |
| `schema-identity --json` | metadata | Print the embedded datastore schema contract; no runtime stamp write. |

### Exit Codes

| Code | Meaning |
|------|---------|
| `0` | Success, including all structured Qwen allow/block responses and non-Stop Codex deny/block responses returned as JSON. |
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

Lifecycle hook criticality (`session-start`, `session-end`, `pre-compact`) comes from ghook's per-CLI registry. Tool-use hooks are non-critical — the envelope still spools, but a transient daemon outage won't block your tool call. Turn-level `Stop` is never critical, so a daemon outage does not freeze the CLI on every turn.

### Codex, Qwen, Droid, Grok, AGY

Same pattern with different `--cli` and `--type` values. ghook's per-CLI
registry (see `crates/ghook/src/cli_config.rs`) defines which hooks are
critical. Terminal-context enrichment is CLI-agnostic and only depends on valid
tmux pane env vars.

| CLI | Critical hooks |
|-----|----------------|
| `claude` | `session-start`, `session-end`, `pre-compact` |
| `codex` | `SessionStart`, `SessionEnd`, `PreCompact` |
| `qwen` | `SessionStart`, `SessionEnd`, `PreCompact` |
| `droid` | `SessionStart`, `SessionEnd`, `PreCompact` |
| `grok` | `session_start`, `session_end`, `pre_compact` |
| `agy` | none |

Grok uses native snake_case hook types (e.g. `session_start`, `session_end`,
`pre_compact`, `stop`, `pre_tool_use`) — distinct from Claude's hyphenated
names and the PascalCase names the other CLIs use. Its malformed-JSON exit code
is `2`. Successful blocking `stop` and `subagent_stop` responses keep the full
provider JSON on stdout with exit `0`, including
`hookSpecificOutput.additionalContext`.

For every CLI, a daemon 2xx becomes acknowledged only after the mapped provider
action has been written and stdout has flushed. ghook then settles the inbox
file through the receipt-or-removal procedure above. Mapping errors, stdout
errors, and crashes before settlement leave the file for daemon recovery.

Droid uses PascalCase hook types (`SessionStart`, `PreToolUse`, `PostToolUse`, `UserPromptSubmit`, `Notification`, `Stop`, `SubagentStop`, `PreCompact`, `SessionEnd`) and ghook forwards droid's stdin payload unchanged to the daemon with `source: "droid"`. Droid-specific block handling differs slightly from the other CLIs: daemon responses containing `continue:false` exit 2, while other meaningful response JSON is written to stdout with exit 0.

AGY uses exactly five PascalCase hook types: `PreInvocation`, `PreToolUse`,
`PostToolUse`, `PostInvocation`, and `Stop`. ghook forwards them with
`source: "agy"`. The registry marks all five non-critical; AGY has no native
`SessionStart` or `UserPromptSubmit` hook. Every AGY failure fails open,
including malformed stdin: ghook exits `0` with the skip JSON on stdout
(`{"decision":"allow"}` for `PreToolUse`, `{}` otherwise) and the diagnostic
on stderr, because a non-zero exit would block the tool call.

Qwen uses its current PascalCase terminal-hook names. Malformed input and
transport failures exit `2` for its three critical lifecycle hooks and `1` for
its other hooks, including `Stop`. Successful Qwen responses, including a
blocking `Stop`, are serialized to stdout with exit `0` so Qwen can consume the
structured decision and reason.

Unknown `--cli` values return `{}` with exit 2 before dispatch side effects.
Diagnose mode reports them as unrecognized.

## Diagnose Mode

`ghook --diagnose` inspects endpoint, CLI and current-directory configuration
without dispatching a hook. It does not prove the host invokes ghook, resolve
stdin workspace identities, or report the effective project disable flag.
The following is an illustrative subset of its output; versions and local values vary.

```bash
$ ghook --diagnose --cli=claude --type=session-start
{
  "schema_version": 2,
  "ghook_version": "<installed version>",
  "cli": "claude",
  "hook_type": "session-start",
  "source": "claude",
  "critical": true,
  "terminal_context_enabled": true,
  "daemon_url": "http://127.0.0.1:60887",
  "daemon_host": "127.0.0.1",
  "daemon_port": 60887,
  "project_root": "/path/to/project",
  "project_id": "<project UUID>",
  "terminal_context_preview": {
    "parent_pid": 72441,
    "tty": "/dev/ttys005",
    "tmux_pane": "%179",
    "tmux_socket_path": "/private/tmp/tmux-501/default",
    "term_program": "tmux",
    "...": "..."
  },
  "cli_recognized": true,
  "install_method": null,
  "install_source_url": null
}
```

Look for:

- **`cli_recognized: true`** — confirms ghook knows this CLI; unknown CLIs are rejected by live dispatch.
- **`critical: true/false`** — does ghook consider this hook type critical under the current per-CLI hook protocol?
- **`terminal_context_enabled: true`** — this recognized CLI lifecycle hook
  receives terminal context. `terminal_context_preview` shows the captured
  values; unavailable tmux fields are `null`.
- **`daemon_url`** — where will the POST go? If this is wrong, check
  `GOBBY_DAEMON_URL`, `GOBBY_PORT`, then `~/.gobby/bootstrap.yaml`.
- **`project_root` / `project_id`** — marker lookup from the current directory. Diagnose does not consume provider stdin, so this view does not include payload workspace fallback. No marker and no other managed identity makes live dispatch skip.
- **`local_token_file_present` / `auth_401_remediation`** — local token availability and recovery instructions; never print the token itself.
- **`failure_dir` / `recent_failure_count` / `recent_failures`** — recorded delivery failure diagnostics.
- **`install_method` / `install_source_url`** — how this `ghook` binary got installed (e.g. `github-release`, `crates-binstall`, `cargo-install`). Both are `null` when the binary was installed without a sidecar-writing installer (e.g. plain `cargo install gobby-hooks`). Useful in bug reports — it tells maintainers exactly which install path a user is on.

The complete diagnose JSON is validated against
`crates/ghook/schemas/diagnose-output.v2.schema.json` in tests.

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

The daemon owns this queue. Preserve original envelopes and receipts when
investigating failures; deleting them loses recovery evidence and may lose
undelivered events. Diagnose and repair the receiver first. Any operator queue
repair needs a coordinated daemon stop and a backup of the affected files.

### Quarantine

Malformed stdin (the host CLI sent something that isn't valid JSON) lands in `~/.gobby/hooks/inbox/quarantine/` as a pair of files:

- `<stem>.json` — body containing the raw stdin bytes, base64-encoded.
- `<stem>.meta.json` — sidecar with `reason: "malformed_stdin"`, the JSON parse error, and the same base64 payload.

The drain only scans immediate inbox files, so it never replays the quarantine
subdirectory. Inspect the quarantine pair and hook diagnostics when investigating
malformed input; daemon-side quarantine failures are logged. Quarantine retention
is bounded, so preserve relevant evidence before it expires.

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

Inspect stderr and diagnose output. Exit 2 can mean a critical delivery failure,
invalid arguments, unsupported CLI, malformed input, or provider-specific denial.
Only successfully enqueued envelopes can replay; malformed input uses quarantine,
and a successfully delivered denial may already have settled its inbox file.

### Sandbox FS-read denials (macOS)

Check which path was denied. A successfully enqueued envelope remains available
for daemon replay after a delivery failure. An inbox write failure instead uses
the bounded direct-POST fallback, except in `--enqueue-only` mode. If both paths
fail, no durable recovery is guaranteed. Project-root lookup runs before detach;
detaching does not grant filesystem access.

### Schema version mismatch

Envelopes carry `schema_version: 1`. If the daemon rejects envelopes for being a newer version than it understands, the daemon needs updating. ghook's `--version` command writes `~/.gobby/bin/.ghook-runtime.json` so the daemon can detect this.

_Last verified: 2026-09-13_
