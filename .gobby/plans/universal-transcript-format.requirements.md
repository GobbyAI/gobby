# Requirements: Universal Transcript Format & Renderer

Confirmed Decision Record for plan `universal-transcript-format`. This is the
authoritative requirements source for adversarial review of that plan; the plan
designates it with a `requirement-source:` marker in its `## Constraints`
section.

Owner: josh. Confirmed 2026-07-28, amended 2026-07-29 (see Decision Log).

## Problem

Gobby parses five AI-coding-CLI transcript formats through one Python engine,
but provider knowledge is dispersed and the wire contract leaks provider-native
shapes into the web renderer. Concretely, as of 2026-07-28:

- Provider knowledge lives in roughly twelve independent lists across
  `src/gobby/sessions/transcript_paths.py`, `transcript_source.py`,
  `transcript_tool_metadata.py`, `src/gobby/agents/watchdog/registry.py`, and
  three TypeScript copies under `web/src/`.
- Three parser dispatchers disagree: `get_parser`
  (`src/gobby/sessions/transcripts/__init__.py:33`), the private `_get_parser`
  clone (`src/gobby/sessions/transcript_parsing.py:27`), and an inlined
  source chain in `src/gobby/sessions/transcript_processing.py` that **defaults
  unknown sources to the Claude parser**, silently mis-parsing any new provider.
- The renderer boundary leaks: Codex argv-array commands render blank summaries,
  Codex `read` calls fall back to generic JSON dumps, the `<gobby-*>` protocol
  grammar is reimplemented in ~400 lines of TypeScript that must be fixed in
  lockstep with Python, and diffs are reverse-engineered client-side from
  `old_string`/`new_string`.

## Requirements

- **R1 — One canonical normalized message contract.** A single normalized
  transcript message shape, authored language-neutrally as a JSON Schema plus
  golden fixtures under `docs/contracts/`, versioned with an explicit policy.
  Every consumer reads that shape and no provider-native shape.
- **R2 — One declarative provider registry.** A single `ProviderSpec` source of
  per-provider knowledge (paths, sniffing, parser, watchdog reader, tool
  aliases, argument key maps, render suppression, usage source), with
  import-time parity guards against `SessionSource`, the parser registry, and
  the watchdog registry, so a provider cannot be half-registered.
- **R3 — One fail-closed parser dispatch.** Exactly one dispatch function
  driven by the registry. An unknown source raises rather than falling back to
  Claude.
- **R4 — A renderer contract with zero provider knowledge in TypeScript.**
  Canonical tool arguments, a closed content-block enum, a closed tool-type
  enum, typed tool results, server-resolved protocol tags, first-class diff
  blocks, server-authoritative roles, one stable message-ID scheme, and
  per-message completeness on both transports.
- **R5 — One universal frontend renderer.** The Watching panel and ChatPage
  share one message pipeline and one virtualized list, and render identical
  output for identical payloads. Per-message provenance (model, token usage)
  reaches the UI.
- **R6 — Drift fails loudly.** Version-pinned raw fixtures with committed
  expected normalized output for every provider, validated in CI against the
  schema; TypeScript types are generated from the schema and a staleness check
  fails when the schema moves without regeneration.
- **R7 — Cursor CLI as provider six.** Cursor is integrated end to end —
  provider spec, transcript source, parser, hooks adapter, watchdog reader,
  spawn support, feature-tier registration — as proof that a non-JSONL,
  SQLite-backed source works through the new architecture rather than around it.

## Constraints

- **C1 — Normalization is never persisted.** It is produced on demand. No
  normalized artifact on disk, no new Postgres tables. The `sessions` aggregate
  model, `token_events`, and file-based window rendering remain the storage
  story.
- **C2 — Raw transcripts stay byte-exact**, with one deliberate Cursor-scoped
  exception. CLI-owned live transcripts are read-only and archives remain
  unmodified raw copies. For Cursor, Gobby archives the extracted messages
  rather than the container, because the container is a database schema Gobby
  does not own.
- **C3 — Gobby never modifies Cursor-owned state.** No copy or restore of
  Cursor's live WAL database, no write-back into `~/.cursor`. Read access is
  read-only snapshot reads. The one measured exception is inherent to SQLite:
  a read-only open of a WAL database whose `-shm` member is absent causes
  SQLite to create that ephemeral index; `.db` and `-wal` bytes are untouched.
- **C4 — One bounded Rust change.** `crates/gwiki` is untouched. `crates/ghook`
  receives exactly one required change — a `cursor` arm in
  `CliConfig::for_cli` plus its test — because `for_cli` otherwise returns
  `None` for `cursor` and `dispatch.rs` answers `None` with exit code 2,
  dropping every Cursor hook event.
- **C5 — Internal-first.** No public API, bindings, or crates.io work. The
  contract is authored language-neutrally so the later public Rust crate port
  is a port rather than a redesign.
- **C6 — No backward compatibility.** 0.5.0 has not shipped; wire shapes,
  frontend types, and fixture layouts may change freely.
- **C7 — Design gate.** Frontend deliverables read `.impeccable.md` before
  producing UI output.

## Out of scope

Deferred with typed sections in the plan and open tasks:

- gwiki migration to the normalized contract — task #19207 (plan section D1).
  gwiki keeps its per-provider adapters until the contract has survived this
  epic's fixture gauntlet.
- OpenTelemetry GenAI export projection — task #19208 (section D2). The OTel
  GenAI agent conventions are still in Development status.
- Public Rust crate port — task #19209 (section D3). Post-0.5.0.

Also explicitly out of scope: native cross-machine resume of a Cursor session.
Cursor resume takes only a chatId against Cursor's own store, so native resume
is same-machine-only by design; cross-machine handoff is transcript/summary
handoff, as with any provider.

## Success criteria

- **S1** — Adding a new provider requires a spec entry, a parser, a watchdog
  reader, and a fixture corpus, with no edits to discovery, dispatch, or any
  frontend file.
- **S2** — Every provider's golden fixtures parse, render, validate against the
  schema, and byte-compare to committed expected output, with zero `unknown`
  blocks for pinned fixtures.
- **S3** — No tool-name allowlist, protocol-tag parser, role reclassifier, or
  block-suppression set remains anywhere under `web/src`.
- **S4** — The same normalized payload renders identically in the Watching panel
  and ChatPage.
- **S5** — The committed-snapshot bugs are closed: Codex shell calls show real
  command summaries, Codex reads render file cards, diffs come from the server,
  and Claude image content renders instead of vanishing.
- **S6** — A Cursor session is discovered, parsed with full fidelity (thinking,
  tool calls, complete tool results, diff metadata), rendered, archived, and
  spawnable.

## Decision log

Decisions taken during elicitation and review, with the reasoning that settled
them. These are closed; reopening any of them is a scope change.

- **Python is the canonical parsing layer this epic; Rust is deferred.**
  Migrating gwiki against an unproven contract would churn Rust twice.
- **Hybrid ProviderSpec** — declarative surface knowledge plus code parsers.
  Fully declarative parsing cannot express the per-provider pairing logic.
- **Cursor's SQLite store is the only content source.** The
  `~/.cursor/projects/*/agent-transcripts/*.jsonl` surface is invocation-only —
  user/assistant text and `tool_use` calls, no tool results, no reasoning
  (verified on both the CLI and IDE writers) — so it is not a declared
  transcript surface and is never read for content. A fallback read would
  produce a gutted transcript that looks complete.
- **Archive format is a message-level JSONL export**, not a database copy and
  not a logical dump: a decoded-meta header record followed by each message's
  provider-native AI-SDK JSON, unmodified, in root-blob order. Byte-for-byte
  database preservation was rejected because Gobby does not own the schema and
  will not reconstruct it.
- **No restore tool.** Resume is Cursor's job via `--resume <chatId>`.
- **No truncation anywhere.** The normalized format always carries full result
  payloads; collapsing long output is a renderer display concern.
- **Read-through is empirically validated** (2026-07-29, SQLite 3.50.4):
  `mode=ro` reads WAL-only committed rows with a concurrent holder, correctly
  hides an uncommitted transaction, and reads an abandoned session with no
  `-shm`. `immutable=1` is prohibited — it reports `journal_mode: delete` and
  ignores the WAL, silently dropping the newest messages.
- **Reader kind is provider-level**, not per-surface. Every provider declares
  content surfaces of exactly one kind, so a per-surface attribute would be
  mechanism without a present-day justification.
