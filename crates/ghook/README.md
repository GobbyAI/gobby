# ghook

Sandbox-tolerant hook dispatcher for Gobby.

`ghook` is invoked by host AI CLIs (Claude Code, Codex, Gemini CLI, Qwen
CLI, Factory droid) on lifecycle and tool-use events. It enqueues an envelope to
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
- `schemas/diagnose-output.v2.schema.json` — what `--diagnose` prints. Adds `install_method` and `install_source_url` fields sourced from an installer-written sidecar (`.ghook-install.json`, next to the binary).
- `schemas/diagnose-output.v1.schema.json` — frozen historical schema for the 0.1.x and 0.2.x diagnose output. Kept for tooling that pinned to v1.

The active schemas (envelope v1, diagnose v2) are validated in unit tests.

## Install provenance

`ghook --diagnose` reads an optional sidecar file named `.ghook-install.json` from the same directory as the running binary. When present, its `install_method` and `install_source_url` fields surface in the diagnose output so bug reports can identify how a given binary got installed (GitHub release, `cargo-binstall`, `cargo install`, etc.).

The Gobby installer writes this sidecar atomically every time it places a `ghook` binary. Manual installs (e.g. plain `cargo install gobby-hooks`) leave both fields as `null`. See `docs/guides/ghook-development-guide.md` for the contract.
