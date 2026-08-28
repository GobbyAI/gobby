# ghook

Sandbox-tolerant hook dispatcher for Gobby.

`ghook` is invoked by host AI CLIs (Claude Code, Codex, Qwen CLI, Factory
droid, Grok, Antigravity CLI) on lifecycle and tool-use events. It enqueues an envelope to
`~/.gobby/hooks/inbox/` *before* attempting to POST to the local Gobby
daemon — so the daemon's drain worker replays any envelope whose POST
was lost to a sandbox FS-read denial, a network blip, or daemon restart.

## CLI surface

```text
ghook --gobby-owned --cli=<c> --type=<t> [--detach]
ghook --diagnose    --cli=<c> --type=<t>
ghook --version
```

## Terminal context

For session-start, prompt-start (`UserPromptSubmit`, `BeforeAgent`, and
`PreInvocation`), and terminal-completion hooks, when `TMUX` is set and
`TMUX_PANE` matches `^%\d+$`, `ghook` injects
`input_data.terminal_context.tmux_pane` into the envelope. The pane ID is passed
through verbatim. If the pane variable is missing, empty, or invalid, the tmux
fields are emitted as `null`. High-frequency tool hooks omit terminal context.

Exit codes:

- `0` — success, including all structured Qwen allow/block responses and non-Stop Codex deny/block responses returned as JSON
- `1` — non-critical hook failure, returned as JSON error output
- `2` — critical hook failure or blocked critical hook, returned as stderr

Qwen treats `SessionStart`, `SessionEnd`, `PreCompact`, and `Stop` as critical.
Its structured responses always use exit `0`, including a blocking `Stop`.
Transport or malformed-input failures use exit `2` for those critical events
and exit `1` otherwise. Qwen `Stop` deliberately fails closed during daemon
outages so `turn_end` gates cannot be bypassed; the Qwen session can remain
active until the daemon recovers or hooks are disabled. Claude's Stop posture
remains fail open.

## Planned shutdown Stop handling

For Stop hooks only, `ghook` recognizes fresh daemon shutdown markers in
`$GOBBY_HOME` or `~/.gobby`. When a marker carries `intent: "stop"` /
`"restart"` or an allowed source prefix (`cli_`, `http_`, `service_`, `mcp_`)
and the daemon health endpoint is unreachable, `ghook` returns
`{"continue":true}` before reading stdin or enqueueing.

`GOBBY_DAEMON_URL` overrides the daemon URL for health probes, live POSTs, and
the statusline POST.
`GOBBY_SHUTDOWN_HOOK_ALLOW_SECONDS` overrides marker freshness when it is a
positive number; the default is 120 seconds.

After enqueue, the same fresh-window rule, including
`GOBBY_SHUTDOWN_HOOK_ALLOW_SECONDS`, is applied to Stop live POST failures. The
live POST URL uses `GOBBY_DAEMON_URL` when set. Only `Connect` and `Timeout`
failures are suppressed; HTTP responses stay on the normal daemon-response path.
When suppression applies, `ghook` deletes the just-enqueued envelope and returns
`{"continue":true}`.

## Schemas

- `schemas/inbox-envelope.v1.schema.json` — what lands in the inbox.
- `schemas/diagnose-output.v2.schema.json` — what `--diagnose` prints. Adds `install_method` and `install_source_url` fields sourced from an installer-written sidecar (`.ghook-install.json`, next to the binary).

The schemas are validated in unit tests.

## Install provenance

`ghook --diagnose` reads an optional sidecar file named `.ghook-install.json` from the same directory as the running binary. When present, its `install_method` and `install_source_url` fields surface in the diagnose output so bug reports can identify how a given binary got installed (GitHub release, `cargo-binstall`, `cargo install`, etc.).

The Gobby installer writes this sidecar atomically every time it places a `ghook` binary. Manual installs (e.g. plain `cargo install gobby-hooks`) leave both fields as `null`. See `docs/guides/ghook-development-guide.md` for the contract.
