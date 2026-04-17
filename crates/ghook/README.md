# ghook

Sandbox-tolerant hook dispatcher for Gobby.

`ghook` is invoked by host AI CLIs (Claude Code, Codex, Gemini CLI, Qwen
CLI) on lifecycle and tool-use events. It enqueues an envelope to
`~/.gobby/hooks/inbox/` *before* attempting to POST to the local Gobby
daemon — so the daemon's drain worker replays any envelope whose POST
was lost to a sandbox FS-read denial, a network blip, or daemon restart.

## CLI surface

```text
ghook --gobby-owned --cli=<c> --type=<t> [--critical] [--detach]
ghook --diagnose    --cli=<c> --type=<t>
ghook --version
```

Exit codes:

- `0` — success, including non-Stop deny/block responses that are returned as JSON
- `1` — non-critical hook failure, returned as JSON error output
- `2` — critical hook failure or blocked critical hook, returned as stderr

## Schemas

- `schemas/inbox-envelope.v1.schema.json` — what lands in the inbox.
- `schemas/diagnose-output.v1.schema.json` — what `--diagnose` prints.

Both are validated in unit tests.
