# AI Configuration

gcode and gwiki use **daemon-only AI routing**. A live Gobby daemon issues a
runtime grant before any datastore or AI work. Capability truth is the grant:
`capabilities.{embed,text_generate,tool_chat,vision_extract,audio_transcribe}`
are each `daemon` or `unavailable`. There is no Auto or Direct route, no local
provider fallback, and no probe of daemon status endpoints to decide
availability.

With no grant and no daemon, both binaries fail with the typed **daemon
required** error and never open PostgreSQL, FalkorDB, or Qdrant. Connection
material comes only from the grant.

`--no-ai` is the only user-facing routing switch. It maps to `AiRouting::Off`
for that invocation. Per-capability transport overrides are gone.

## Outage semantics

An unexpired grant still authorizes direct datastore construction when the
daemon is down. AI does not: explicit AI commands fail typed once the daemon is
unreachable. Hybrid search is the single degrade path — lexical and graph
results remain, and the semantic lane is omitted with a `warnings` entry
(`lane=semantic`, `cause=daemon_unreachable`). After the grant expires,
everything fails typed until a new handshake.

`gcode outline` is structural only. It does not call text generation.

## Defaults

Routing is daemon when `--no-ai` is absent. Per-capability models, profiles, and
candidate pins (`--ai-aggregate-profile`, `--ai-aggregate-candidate`) still
shape *which* daemon provider/model runs; they do not choose a transport.

```yaml
ai:
  routing: daemon
  max_concurrency: 1
```

Valid routing values are `daemon` and `off`. `auto` and `direct` are rejected.

## Privacy Path

Use `--no-ai` when a command must not call the daemon for AI:

```bash
gwiki ingest-file media/private-recording.mp3 --no-ai
gwiki ask --no-ai "What does the vault say about leases?"
gwiki code --no-ai
```

`--no-ai` forces embeddings, transcription, translation, vision, and text
generation off for that command. gwiki still stores the source as a raw asset
and records degraded derived output where applicable. `gwiki ask --llm` and
`--deep` cannot be combined with `--no-ai`.

## Profiles and candidates

When text generation routes through the daemon without an explicit
provider/model pair, requests carry a daemon feature profile (`feature_low`
unless configured). Set `ai.text_generate.profile` to change that default.
`--ai-aggregate-candidate` accepts `provider/model[@effort]` only — not a
provider URL or `api_base`.

Daemon-side agentic generation uses the same profile routing plus a tool policy
from the caller. Production CodeWiki generation remains operationally paused.

_Last verified: 2026-08-13_
