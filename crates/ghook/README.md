# ghook

Sandbox-tolerant hook dispatcher for Gobby.

`ghook` is invoked by host AI CLIs (Claude Code, Codex, Gemini CLI, Qwen
CLI) on lifecycle and tool-use events. It enqueues an envelope to
`~/.gobby/hooks/inbox/` *before* attempting to POST to the local Gobby
daemon — so the daemon's drain worker replays any envelope whose POST
was lost to a sandbox FS-read denial, a network blip, or daemon restart.

## CLI surface

```
ghook --gobby-owned --cli=<c> --type=<t> [--critical] [--detach]
ghook --diagnose    --cli=<c> --type=<t>
ghook --version
```

Exit codes:

- `0` — delivered OR non-critical failure (envelope enqueued for replay)
- `2` — critical failure (envelope enqueued; signals the host CLI)

## Schemas

- `schemas/inbox-envelope.v1.schema.json` — what lands in the inbox.
- `schemas/diagnose-output.v1.schema.json` — what `--diagnose` prints.

Both are validated in unit tests.
