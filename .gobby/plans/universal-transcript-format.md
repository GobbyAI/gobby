# Universal Transcript Format & Renderer

**Plan ID:** universal-transcript-format

## Overview
`kind: framing`

Gobby parses five AI-coding-CLI transcript formats today through one Python
engine, but provider knowledge is dispersed across ~12 independent lists, three
parser dispatchers (one silently defaulting to Claude), and a wire contract
that leaks provider-native shapes into the web renderer (Codex argv-array
commands render blank; three drifting tool-allowlist copies; protocol-tag
regex duplicated in Python and TypeScript). This epic consolidates the Python
layer into a single canonical normalized transcript format with a
language-neutral contract (JSON Schema + golden fixtures), a declarative
ProviderSpec registry, a tightened server-side render contract, a consolidated
universal frontend renderer for the Watching panel and ChatPage, and one new
provider — Cursor CLI — integrated as validation that the architecture
handles non-JSONL (SQLite-backed) sources.

## Constraints
`kind: framing`

- **One bounded Rust change.** `crates/gwiki` keeps its current per-provider
  adapters; migration to the normalized contract is deferred to task #19207
  (section D1). `crates/ghook` gets exactly one required change: a `cursor` arm
  in `CliConfig::for_cli` (`crates/ghook/src/cli_config.rs:21`) plus its test.
  This is not conditional — `for_cli` matches a fixed CLI set and falls to
  `_ => None` for `cursor`, and `crates/ghook/src/dispatch.rs:25` answers `None`
  with `emit_empty_json()` and exit code 2, so without the arm every Cursor hook
  event dies before reaching the daemon. The arm's `critical_hooks` set comes
  from the §5.1 fired-event inventory. No other Rust change is permitted.
- **Raw transcripts stay byte-exact.** CLI-owned live transcripts are
  read-only, and `~/.gobby/session_transcripts/{external_id}.jsonl.gz` archives
  remain unmodified raw copies used by resume/restore. No persisted normalized
  artifact: normalization is produced on demand. **Cursor refinement:** Gobby
  never copies or restores Cursor's live WAL database, and never modifies
  Cursor's database or WAL or writes any transcript state into Cursor-owned
  paths. One measured exception is inherent to SQLite rather than to Gobby:
  opening a WAL database read-only when its `-shm` member is absent causes
  SQLite to create that 32 KiB WAL index alongside the database, where it
  survives connection close. `-shm` is a regenerable shared-memory index
  carrying no transcript content, and `.db`/`-wal` bytes and mtimes are
  unchanged (verified 2026-07-29). Resume stays Cursor-side via
  `--resume <chatId>`
  against Cursor's own store, which makes native resume same-machine-only by
  design (cross-machine handoff is transcript/summary handoff). Cursor
  archives are produced by the §5.2 import bridge: a message-level JSONL
  export of the decoded store content — each message's provider-native
  AI-SDK JSON, unmodified, in root order — riding the same archive path.
  This is a deliberate, Cursor-scoped exception to byte-exactness: the CLI
  artifact is a database schema Gobby doesn't own, so Gobby archives the
  extracted messages rather than the container.
- **No new Postgres tables.** The `sessions` aggregate model, `token_events`,
  and file-based window rendering remain the storage story.
- **Internal-first.** The contract is authored language-neutrally
  (JSON Schema + fixtures under `docs/contracts/`) so the post-0.5.0 public
  Rust crate port (#19209, section D3) is a port, not a redesign. No public
  API, bindings, or crates.io work in this epic.
- **No backward compatibility.** 0.5.0 has not shipped; existing wire shapes,
  frontend types, and fixture layouts may change freely.
- Frontend deliverables must read `.impeccable.md` before producing UI output.
- requirement-source: .gobby/plans/universal-transcript-format.requirements.md

## P1: Contract and ProviderSpec Foundation
`kind: framing`

**Goal**: One language-neutral normalized-message contract and one declarative
per-provider spec that the registry, parsers, discovery, and renderer all
derive from.

### 1.1 Author the normalized transcript contract and JSON Schema [category: docs]
`kind: deliverable`

Target: `docs/contracts/transcript-format.md`
Targets: `docs/contracts/schemas/normalized-transcript-message.schema.json`

Write the canonical contract for Gobby's normalized transcript message — the
wire shape produced by `RenderedMessage.to_dict()`
(`src/gobby/sessions/transcript_render_models.py`) and consumed by the web
renderer, the WebSocket `session_message` event, the HTTP messages window, and
(later, deferred) gwiki. The document defines, and the JSON Schema pins:

- **Message envelope**: `schema_version` (semver string, starts `"1.0.0"`),
  `id` (stable identity per §2.6), `role` (`user | assistant | system`),
  `timestamp` (ISO 8601), `source` (provider id), `model` (nullable),
  `usage` (nullable `TokenUsage`: `input_tokens`, `output_tokens`,
  `cache_creation_tokens`, `cache_read_tokens`), `complete` (boolean),
  `content_blocks` (ordered typed parts).
- **Closed block-type enum** (§2.2): `text`, `thinking`, `tool_chain`,
  `tool_reference`, `attachment`, `image`, `diff`, `protocol`,
  `compaction_summary`, `system_event`, `unknown`. `unknown` is reserved for
  record shapes the parser has never seen; every `unknown` carries the raw
  payload and is counted by observation telemetry.
- **Tool call shape** (§2.1/§2.3): `tool_type` (closed enum:
  `protocol | bash | read | edit | grep | glob | mcp | unknown` — the
  existing classifier vocabulary; provider-native names live only in
  `tool_name` and `arguments_raw`), canonical `arguments` (`command`,
  `file_path`, `pattern`, `query`, `url` — always strings when present),
  `arguments_raw` (provider-native passthrough), ACP-aligned `status`
  (`pending | in_progress | completed | failed`), optional `locations`
  (file paths touched), typed `result`
  (`kind: text | json | image | error | diff | file | search_results`,
  normalized `metadata`). Results are never truncated — the normalized
  format always carries the full payload.
- **Versioning policy**: additive fields bump minor; enum additions bump
  minor; removals/renames bump major. The schema file is the single source —
  Python models and generated TypeScript types (§4.2) must round-trip it.
- **Fixture law**: every provider ships version-pinned raw fixtures with
  committed expected normalized output (§4.1); a contract change without
  regenerated fixtures fails CI.

**Acceptance:**

- 1.1.1 - Contract document exists covering envelope, closed block enum, tool
  shape, versioning policy, and fixture law. file: `docs/contracts/transcript-format.md`.
- 1.1.2 - JSON Schema validates the normalized message envelope and all block
  types, and rejects unknown top-level fields. file: `docs/contracts/schemas/normalized-transcript-message.schema.json`.
- 1.1.3 - behavior: "normalized transcript message contract" documented in
  `docs/contracts/transcript-format.md` is referenced from `CLAUDE.md`'s
  contract reading order or `docs/contracts/plan-coverage.md`-style index so
  agents find it. behavior: "contract discoverable from docs index" in `docs/contracts/transcript-format.md`.

### 1.2 Add ProviderSpec model and registry [category: code] (depends: 1.1)
`kind: deliverable`

Target: `src/gobby/providers/spec.py`
Targets: `src/gobby/providers/registry.py`, `tests/providers/test_provider_spec.py`

Create the single declarative source of per-provider knowledge. One frozen
dataclass per provider, registered in an ordered mapping keyed by
`SessionSource` value:

```python
@dataclass(frozen=True)
class ProviderSpec:
    id: str                          # SessionSource value, e.g. "codex"
    display_name: str
    binary: str                      # executable name
    config_dir: str                  # e.g. "~/.codex"
    transcript_surfaces: tuple[str, ...]  # discovery globs; {cwd_hash}/{cwd_urlencoded} placeholders
    transcript_source_kind: Literal["file", "sqlite"]  # reader selection (§5.2)
    path_markers: tuple[str, ...]    # substrings identifying a transcript path
    record_sniff: Callable[[dict], bool]  # first-record detection predicate
    parser: str                      # import path of TranscriptParser subclass
    parser_requires_transcript_path: bool  # droid sidecar quirk, spec-driven not special-cased
    watchdog_reader: str             # import path of TranscriptWatchdogReader
    tool_aliases: Mapping[str, str]  # provider tool name -> canonical (e.g. "exec_command" -> "Bash")
    arg_key_map: Mapping[str, tuple[str, ...]]  # canonical field -> provider keys, e.g. "file_path": ("path",)
    suppressed_record_types: frozenset[str]     # server-side render suppression
    usage_source: str                # "transcript" | "sidecar" | "none" (drives window-only context set)
```

`transcript_surfaces` declares **content** sources only — the surfaces a
transcript is actually read from. Every provider declares surfaces of exactly
one kind, so `transcript_source_kind` is a single provider-level value rather
than a per-surface attribute: the five existing providers are `file`, and
cursor is `sqlite` with `chats/{cwd_hash_md5}/*/store.db` as its only content
surface (§5.3). Recognizing a path as a provider's is a separate concern
already served by `path_markers`, which is why Cursor's invocation-only
`projects/*/agent-transcripts/*.jsonl` is **not** a declared surface: it
carries no tool results and no reasoning, so making it discoverable would
invite a fallback read that silently produces gutted transcripts (§5.2, §5.4).

Registry: `PROVIDER_SPECS: dict[str, ProviderSpec]` in
`src/gobby/providers/registry.py` alongside the existing `ProviderMetadata`.
Import-time parity guards (module-level asserts, mirroring
`src/gobby/agents/watchdog/registry.py`'s `KNOWN_WATCHDOG_PROVIDERS` guard):

- `set(PROVIDER_SPECS) == {s.value for s in SessionSource} - {"agy", ...}` for
  transcript-capable sources (AGY has hooks but no transcripts; the spec
  carries transcript-capable providers only, and the guard documents the
  exclusion explicitly).
- `set(PROVIDER_SPECS) == set(PARSER_REGISTRY)` (`src/gobby/sessions/transcripts/__init__.py`).
- `set(PROVIDER_SPECS) == set(KNOWN_WATCHDOG_PROVIDERS)`.

The five existing specs (claude, codex, droid, grok, qwen) are populated from
today's scattered constants: path knowledge from
`src/gobby/sessions/transcript_paths.py`, sniffing from
`src/gobby/sessions/transcript_source.py`, tool aliases from
`src/gobby/sessions/transcript_tool_metadata.py` and
`gobby.hooks.normalization.is_shell_tool`, suppression sets from
`web/src/components/chat/RichContentBlocks.tsx` `IGNORED_PROTOCOL_BLOCK_TYPES`
(moving server-side), `usage_source` from
`src/gobby/sessions/transcript_processing.py` `_WINDOW_ONLY_CONTEXT_SOURCES`.

**Acceptance:**

- 1.2.1 - ProviderSpec dataclass exists with the fields above. symbol: `ProviderSpec`. file: `src/gobby/providers/spec.py`.
- 1.2.2 - Registry maps all five transcript-capable providers with import-time
  parity guards against `SessionSource`, `PARSER_REGISTRY`, and
  `KNOWN_WATCHDOG_PROVIDERS`. file: `src/gobby/providers/registry.py`.
- 1.2.3 - Guard failure is loud: removing a provider from any derived registry
  fails at import. test: `tests/providers/test_provider_spec.py::test_registry_parity_guards`.

### 1.3 Collapse parser dispatch to a single fail-closed path [category: refactor] (depends: 1.2)
`kind: deliverable`

Target: `src/gobby/sessions/transcripts/__init__.py`
Targets: `src/gobby/sessions/transcript_parsing.py`, `src/gobby/sessions/transcript_processing.py`, `src/gobby/sessions/processor_lifecycle.py`, `src/gobby/memory/digest.py`, `src/gobby/servers/routes/sessions/analytics.py`, `src/gobby/tasks/transcript_evidence.py`

Three dispatchers exist and disagree:

1. `get_parser` (`src/gobby/sessions/transcripts/__init__.py:33`) — registry +
   droid special case.
2. `_get_parser` (`src/gobby/sessions/transcript_parsing.py:27`) — private
   if/elif clone.
3. Inlined `if session.source == ...` chain in
   `src/gobby/sessions/transcript_processing.py` (~line 300) that **defaults
   unknown sources to the Claude parser** — a silent mis-parse for any future
   provider.

Collapse to one: `get_parser(source, transcript_path=None)` reads
`PROVIDER_SPECS[source].parser` (import via `importlib`), honors
`parser_requires_transcript_path` generically (removing the droid
special-case), and raises `ValueError` for any source without a spec. Delete
`_get_parser` and the inlined chain; both call sites use the canonical
function. No behavioral change for the five providers — pinned by existing
parser tests.

**Acceptance:**

- 1.3.1 - Single dispatch function driven by ProviderSpec; droid special-case
  replaced by the spec flag. symbol: `get_parser`. file: `src/gobby/sessions/transcripts/__init__.py`.
- 1.3.2 - `_get_parser` and the inlined source chain are deleted; expired-session
  batch processing uses the canonical dispatch. file: `src/gobby/sessions/transcript_processing.py`.
- 1.3.3 - Unknown source raises `ValueError` in every dispatch path (no Claude
  fallback). test: `tests/sessions/test_transcript_parsers.py::test_get_parser_unknown_source_fails_closed`.

### 1.4 Drive transcript discovery and sniffing from ProviderSpec [category: refactor] (depends: 1.2)
`kind: deliverable`

Target: `src/gobby/sessions/transcript_paths.py`
Targets: `src/gobby/sessions/transcript_source.py`, `src/gobby/hooks/event_handlers/_session_start/transcripts.py`, `src/gobby/tasks/transcript_evidence.py`

Replace the hardcoded per-CLI branches with spec-driven iteration:

- `find_transcript_on_disk` (`transcript_paths.py:13`) iterates
  `PROVIDER_SPECS`, expanding each spec's `transcript_surfaces` (placeholders:
  `{cwd_hash}` sha256(cwd), `{cwd_hash_md5}`, `{cwd_urlencoded}`,
  `{project_slug}`) instead of provider-named helper branches. Discovery
  yields `TranscriptCandidate(path, kind)`, stamping the owning spec's
  `transcript_source_kind` onto each hit so downstream reader selection (§5.2)
  never re-sniffs.
- `_detect_source_from_path` (`transcript_source.py:16`) matches
  `spec.path_markers`; `_detect_source_from_record` (`:50`) and
  `_detect_source_from_jsonl_lines` (`:92`) call `spec.record_sniff`
  predicates in registry order.
- `derive_transcript_path` (`_session_start/transcripts.py:16`) keeps its
  qwen/grok derivation logic but sources globs from the spec rather than
  local constants.

Behavior for the five providers is pinned by existing tests
(`tests/sessions/` path/source detection tests) — this is a pure
knowledge-relocation refactor.

**Acceptance:**

- 1.4.1 - Discovery iterates ProviderSpec globs; no provider-named branches
  remain in the function body. symbol: `find_transcript_on_disk`. file: `src/gobby/sessions/transcript_paths.py`.
- 1.4.2 - Path and record sniffing read spec markers/predicates. file: `src/gobby/sessions/transcript_source.py`.
- 1.4.3 - Existing detection behavior unchanged for all five providers. test: `tests/sessions/test_transcript_source.py`.

## P2: Server-Side Normalized Format
`kind: framing`

**Goal**: The wire shape emitted by the renderer satisfies the §1.1 contract —
canonical tool args, closed block enum, typed results, server-resolved
protocol tags, authoritative roles, stable IDs — so no consumer needs
provider knowledge.

### 2.1 Canonical tool-argument normalization [category: code] (depends: P1)
`kind: deliverable`

Target: `src/gobby/sessions/transcript_tool_metadata.py`
Targets: `src/gobby/sessions/transcript_render_models.py`, `src/gobby/sessions/transcript_render_blocks.py`

Add `normalize_tool_arguments(tool_name, arguments, spec) -> dict` in
`transcript_tool_metadata.py`:

- `command`: fold provider argv arrays to one string via `shlex.join` (Codex
  emits `command: ["uv", "run", ...]`); accept `command`/`cmd` keys.
- `file_path`: fold `path`, `filePath`, `absolute_path`, `file` per
  `spec.arg_key_map`.
- `pattern`, `query`, `url`: same folding mechanism.
- Canonical keys are always strings when present; unmapped provider keys pass
  through untouched.

`RenderedToolCall` gains `arguments` (canonical) and `arguments_raw`
(provider-native, verbatim). `_process_message_block`
(`transcript_render_blocks.py`) applies normalization at block build time.
`extract_result_metadata` reads canonical keys only. Tool-name aliasing
(`spec.tool_aliases`) resolves before classification so
`gobby.hooks.normalization.is_shell_tool` and `classify_tool` operate on
canonical names — one Python source of truth; the two frontend copies die in
§3.1.

`tool_type` on the wire is the closed §1.1 `ToolType` enum
(`protocol | bash | read | edit | grep | glob | mcp | unknown`), defined in
the JSON Schema, mirrored by the Python model, and flowing into the §4.2
generated TypeScript union. An unclassifiable provider spelling emits
`unknown` — never a raw provider string leaking through the renderer
boundary.

This fixes the committed-snapshot bugs: Codex shell calls rendering empty
summaries and Codex `read` calls falling to generic JSON dumps.

**Acceptance:**

- 2.1.1 - Canonical argument folding implemented with argv-array join and
  spec-driven key maps. symbol: `normalize_tool_arguments`. file: `src/gobby/sessions/transcript_tool_metadata.py`.
- 2.1.2 - `RenderedToolCall` carries `arguments` and `arguments_raw`; blocks are
  built with canonical args. file: `src/gobby/sessions/transcript_render_models.py`.
- 2.1.3 - Codex argv-array command and `path`-keyed read normalize to canonical
  `command`/`file_path`. test: `tests/sessions/test_transcript_tool_metadata.py::test_codex_argv_and_path_folding`.
- 2.1.4 - Every `ToolType` enum member is produced by at least one contract
  fixture (synthetic where no provider emits it naturally). test: `tests/sessions/test_transcript_tool_metadata.py::test_tool_type_enum_fixture_coverage`.

### 2.2 Closed block enum and server-side suppression [category: code] (depends: P1)
`kind: deliverable`

Target: `src/gobby/sessions/transcript_render_models.py`
Targets: `src/gobby/sessions/transcript_render_blocks.py`, `src/gobby/sessions/transcripts/claude.py`

- Define `BlockType` (StrEnum) with exactly the §1.1 contract members; 
  `ContentBlock.type` is a `BlockType`. `RenderedMessage.to_dict()` emits
  `schema_version`.
- Move frontend suppression server-side: the record types currently hidden by
  `web/src/components/chat/RichContentBlocks.tsx` `IGNORED_PROTOCOL_BLOCK_TYPES`
  (`file_history_snapshot`, `retry_state`, `turn_completed`, `ui_telemetry`)
  join `spec.suppressed_record_types` and are skipped during rendering (added
  to the render-skip path, not emitted as `unknown`).
- `unknown` is reserved for genuinely novel shapes; every emission still feeds
  `ObservationTracker.observe_block_type` so drift is visible at
  `GET /api/observations`.
- Fix the dead-image path: the Claude parser emits `image` blocks for image
  content in user/assistant messages (today pasted screenshots vanish with no
  diagnostic — `docs/reviews/sessions.md:221`). The stub `document` and
  `web_search_result` frontend branches are removed with their block types;
  if a provider emits them they surface as `unknown` + telemetry until
  modeled.

**Acceptance:**

- 2.2.1 - Closed `BlockType` enum; `to_dict()` emits `schema_version`. symbol: `BlockType`. file: `src/gobby/sessions/transcript_render_models.py`.
- 2.2.2 - The four suppression types are skipped server-side via ProviderSpec;
  none reach the wire. test: `tests/sessions/test_transcript_renderer.py::test_suppressed_record_types_skipped`.
- 2.2.3 - Claude image content renders as an `image` block. test: `tests/sessions/test_transcript_parsers.py::test_claude_image_block_emitted`.

### 2.3 Typed ToolResult and ACP-aligned lifecycle [category: code] (depends: 2.2)
`kind: deliverable`

Target: `src/gobby/sessions/transcript_render_models.py`
Targets: `src/gobby/sessions/transcript_tool_metadata.py`, `src/gobby/sessions/transcript_render_blocks.py`, `src/gobby/sessions/transcript_protocol.py`

- `ToolResult.kind` becomes a closed enum:
  `text | json | image | error | diff | file | search_results` (today it is
  only ever `"json"`/`"text"`). Kind selection uses tool classification +
  result-shape inspection in `extract_result_metadata`.
- **No truncation** (scope decision, enhancement round 1): the normalized
  format always carries full result payloads — matching today's behavior,
  where results serialize whole. The dead `ToolResult.truncated` field
  (declared, never populated) is removed from the model and never enters the
  contract. Collapsing long output is a renderer display concern, not a data
  concern.
- Normalize `metadata` for every provider: `exit_code`, `line_count`,
  `files_matched`, `match_count` — sourced from canonical args + provider
  result shapes (Codex nested-exec outcomes included).
- `RenderedToolCall.status` becomes the ACP-aligned enum
  `pending | in_progress | completed | failed`, and `locations`
  (list of file paths the call touched) is populated when derivable from
  canonical args. Grok's ACP-shaped updates map 1:1; other providers map from
  their pairing state.

**Acceptance:**

- 2.3.1 - Closed result-kind enum with shape-driven selection. symbol: `ToolResult`. file: `src/gobby/sessions/transcript_render_models.py`.
- 2.3.2 - `ToolResult` carries no truncation fields; a multi-megabyte fixture
  result round-trips byte-identical through normalization. test: `tests/sessions/test_transcript_renderer.py::test_tool_result_full_payload`.
- 2.3.3 - ACP-aligned status enum and locations populated across all five
  providers' fixtures. test: `tests/sessions/test_transcript_renderer.py::test_tool_status_acp_vocabulary`.

### 2.4 First-class diff blocks [category: code] (depends: 2.2)
`kind: deliverable`

Target: `src/gobby/sessions/transcript_render_blocks.py`
Targets: `src/gobby/sessions/transcript_render_models.py`

The frontend currently reverse-engineers diffs client-side
(`web/src/components/chat/ToolCallCard.tsx:90-98` synthesizes a unified diff
from `old_string`/`new_string` args; `computeSyntheticDiffLines`). The server
knows the real edit — emit it:

- Edit-family calls (canonical `file_path` + `old_string`/`new_string`),
  Codex `apply_patch` payloads, and Droid editor tools produce a
  `ContentBlock` of type `diff`: `{path, old_text, new_text}` attached to the
  tool chain (rendered result-side).
- The block is emitted alongside, not instead of, the tool call — the call's
  `arguments_raw` still carries the native shape.

**Acceptance:**

- 2.4.1 - Diff blocks emitted for Edit-family, `apply_patch`, and Droid editor
  calls. file: `src/gobby/sessions/transcript_render_blocks.py`.
- 2.4.2 - Diff block shape (`path`, `old_text`, `new_text`) matches the §1.1
  schema. test: `tests/sessions/test_transcript_renderer.py::test_diff_block_emission`.

### 2.5 Server-side protocol-tag resolution [category: code] (depends: 2.2)
`kind: deliverable`

Target: `src/gobby/sessions/transcript_protocol.py`
Targets: `src/gobby/sessions/transcript_render_blocks.py`

Gobby's `<gobby-*>` protocol/context tags are parsed twice — in Python
(`transcript_protocol.py`) and in 400 lines of mirroring TypeScript
(`web/src/components/chat/protocolContent.ts`) — with a documented
lockstep-fix bug class (Codex `<system_instructions>` leak,
`.gobby/plans/completed/task-12910-drawbridge-ui-batch.md:564`). Resolve
server-side only:

- Text containing protocol tags is split at render time into plain `text`
  blocks and `protocol` blocks `{tag, attributes, payload}`.
- The wire never carries unresolved tag markup inside `text` blocks.
- `protocolContent.ts` is deleted in §3.2; the TS regex mirror ceases to
  exist.

**Acceptance:**

- 2.5.1 - Renderer splits protocol tags into typed `protocol` blocks; `text`
  blocks on the wire contain no tag markup. file: `src/gobby/sessions/transcript_render_blocks.py`.
- 2.5.2 - Tag attributes and payload survive into the block. test: `tests/sessions/test_transcript_protocol.py::test_protocol_blocks_resolved_server_side`.

### 2.6 Authoritative roles, stable IDs, completeness [category: code] (depends: 2.2)
`kind: deliverable`

Target: `src/gobby/sessions/transcript_renderer.py`
Targets: `src/gobby/sessions/transcript_window.py`, `src/gobby/servers/routes/sessions/messages.py`

- **Roles**: fold the frontend's `normalizeChatRole` reclassification (hook
  feedback, bootstrap text, protocol-only content → `system`;
  `web/src/lib/chatMessageMapping.ts`) into the renderer's role
  classification so `role` is authoritative on the wire. The two frontend
  mappers collapse in §3.3.
- **Stable IDs**: one scheme — provider-native `message_id` when present,
  else a deterministic derivation from a shared `SourcePosition` input:
  stable transcript identity plus line number, byte offset, or SQLite record
  cursor — never an absolute filesystem path, so the same fixture normalized
  from two locations produces byte-identical output. Every reader constructs
  the same input (`FileTailSource`, `SQLiteStoreSource`, and path-only CLI
  normalization with no session row). Kills the racing dual scheme
  (`f"{session_id}-{role}-{timestamp}-{index}"` vs
  `stableFallbackMessageId` in `web/src/hooks/useSessionDetail/api.ts`).
- **Completeness**: `complete: bool` moves onto `RenderedMessage` itself
  (currently a WebSocket-envelope-only flag), and the HTTP window response
  (`get_rendered_window` → `render_window`) carries it per message, so a
  refetch can distinguish a partial tail turn from a finished one.

**Acceptance:**

- 2.6.1 - Role reclassification is server-side; wire `role` is authoritative.
  file: `src/gobby/sessions/transcript_renderer.py`.
- 2.6.2 - Single stable ID scheme on `RenderedMessage`; HTTP and WS emit
  identical IDs for the same record. test: `tests/sessions/test_transcript_window.py::test_stable_message_identity_http_ws_parity`.
- 2.6.3 - `complete` present per message on both transports. test: `tests/servers/test_session_messages_routes.py::test_completeness_on_http_window`.
- 2.6.4 - Path independence: the same fixture normalized from two filesystem
  locations yields byte-identical output. test: `tests/sessions/test_transcript_window.py::test_message_identity_path_independent`.
- 2.6.5 - A partial message keeps its ID when it becomes complete, across WS
  delivery and HTTP-window overlap. test: `tests/sessions/test_transcript_window.py::test_partial_message_id_stable_on_completion`.

### 2.7 Normalize-on-demand CLI and HTTP surface [category: code] (depends: 2.6)
`kind: deliverable`

Target: `src/gobby/servers/routes/sessions/messages.py`
Targets: `src/gobby/cli/commands_transcripts.py` (new), `src/gobby/sessions/transcript_reader.py`

The normalized format is never persisted — consumers request it:

- `GET /api/sessions/{id}/normalized` streams the full session as normalized
  message JSONL (contract §1.1 shape), reading the live transcript or gzip
  archive via the existing `TranscriptReader` path.
- `uv run gobby transcripts normalize <session-ref|path> [--out FILE]` — CLI
  wrapper over the same code path, for gwiki's future consumption (#19207)
  and offline debugging.
- Both surfaces validate output against the JSON Schema in tests; this is the
  executable proof of the contract and the seam the deferred gwiki migration
  and Rust port build on.

**Acceptance:**

- 2.7.1 - HTTP endpoint streams schema-valid normalized JSONL for live and
  archived sessions. file: `src/gobby/servers/routes/sessions/messages.py`.
- 2.7.2 - CLI command produces identical output to the endpoint for the same
  session. file: `src/gobby/cli/commands_transcripts.py`.
- 2.7.3 - Output for every provider's golden fixture validates against the
  contract schema. test: `tests/servers/test_session_messages_routes.py::test_normalized_export_schema_valid`.

## P3: Universal Renderer Consolidation
`kind: framing`

**Goal**: The web renderer consumes only the contract — zero provider
knowledge in TypeScript — and the Watching panel and ChatPage share one
message pipeline. All frontend deliverables read `.impeccable.md` first.

### 3.1 Consume canonical tool arguments in the renderer [category: code] (depends: P2)
`kind: deliverable`

Target: `web/src/components/chat/ToolCallCard.helpers.ts`
Targets: `web/src/types/chat.ts`, `web/src/components/chat/ToolCallCard.tsx`

- Delete `classifyTool` (`web/src/types/chat.ts:95`) and `SHELL_ALIAS_NAMES`
  (`ToolCallCard.helpers.ts:9`) — the server's `tool_type` and canonical
  `arguments` are authoritative.
- `getShellCommand`/`getToolSummary` read canonical `command`/`file_path`
  only; the expanded card shows `arguments_raw`.
- This closes the committed-snapshot bugs: Codex shell headers show the real
  command, Codex reads render the file card.
- `web/src/components/__tests__/providerSourceAllowlists.test.ts` (the
  drift-canary for the deleted allowlists) is removed with them.

**Acceptance:**

- 3.1.1 - No tool-name allowlists remain in `web/src`; summaries key on
  server `tool_type` + canonical args. file: `web/src/components/chat/ToolCallCard.helpers.ts`.
- 3.1.2 - Codex fixture renders non-empty shell summary and read card. test: `web/src/__visual__/transcripts/transcripts.test.tsx`.

### 3.2 Render protocol blocks; delete the TS protocol parser [category: code] (depends: P2)
`kind: deliverable`

Target: `web/src/components/chat/RichContentBlocks.tsx`
Targets: `web/src/components/chat/protocolContent.ts`, `web/src/components/chat/MessageItem.tsx`

- Render server-emitted `protocol` blocks as the existing collapsed Protocol
  card; `ProtocolAwareText` client-side splitting and the entire
  `protocolContent.ts` module are deleted.
- `IGNORED_PROTOCOL_BLOCK_TYPES` (`RichContentBlocks.tsx:15`) is deleted —
  suppression moved server-side in §2.2. Remaining `unknown` blocks always
  render the `UnknownBlockCard` (raw JSON details), by design.
- Dead `document`/`web_search_result` branches removed per §2.2.

**Acceptance:**

- 3.2.1 - `protocolContent.ts` deleted; protocol cards render from typed
  blocks. file: `web/src/components/chat/RichContentBlocks.tsx`.
- 3.2.2 - No frontend suppression set remains; unknown blocks always render
  the raw-JSON card. test: `web/src/components/chat/__tests__/RichContentBlocks.test.tsx`.

### 3.3 Single role mapper and message pipeline [category: code] (depends: P2)
`kind: deliverable`

Target: `web/src/lib/chatMessageMapping.ts`
Targets: `web/src/components/activity/WatchingTranscript.tsx`, `web/src/hooks/useSessionDetail/api.ts`

- Delete the `toChatMessage` fork (`WatchingTranscript.tsx:70`) — the panel
  uses `mapRenderedMessageToChatMessage`, now trivial because role, IDs, and
  completeness are server-authoritative (§2.6). Same payload renders
  identically in the Watching panel and ChatPage observe path.
- Delete `stableFallbackMessageId` in `web/src/hooks/useSessionDetail/api.ts`
  (single ID scheme).
- Fix the raw-JSON substring bug: `chatMessageMapping.ts:476` matches
  `"tool_result"` as a substring over serialized content, dropping user
  messages that merely mention it (`docs/reviews/web/core.md:68`) — replace
  with typed-block inspection.
- `ChatMessage` gains `model` and `usage` so provenance survives to §3.4.

**Acceptance:**

- 3.3.1 - One mapper for both surfaces; the fork is deleted. file: `web/src/lib/chatMessageMapping.ts`.
- 3.3.2 - Messages mentioning "tool_result" in prose render as user text. test: `web/src/lib/__tests__/chatMessageMapping.test.ts::mentions_tool_result_in_prose`.

### 3.4 Single virtualized transcript list with provenance [category: code] (depends: 3.3)
`kind: deliverable`

Target: `web/src/components/chat/TranscriptList.tsx`
Targets: `web/src/components/activity/WatchingTranscript.tsx`, `web/src/components/chat/MessageList.tsx`, `web/src/components/activity/SessionsTab.tsx`

- Extract one virtualized list component (react-virtuoso) from the two
  parallel implementations (`WatchingTranscript.tsx`, `MessageList.tsx`),
  keeping `WatchingTranscript`'s `computeItemKey` + reverse-infinite-scroll +
  tail-anchoring behavior (the review at `docs/reviews/web/components-a.md:410`
  flags `MessageList` as the lagging copy). Both surfaces mount it.
- Render per-message provenance now that it survives: model name and token
  usage on assistant turns (fields already transported, currently dropped).
  Read `.impeccable.md` before styling.
- Fix the selection bug: when the watched session drops out of the filtered
  list, `SessionsTab` reassigns selection to `entries[0]`, silently switching
  the transcript mid-read (`docs/reviews/web/components-a.md:225`) — keep the
  selection with an explicit "session ended" state instead.

**Acceptance:**

- 3.4.1 - One list component mounted by both the Watching panel and ChatPage.
  file: `web/src/components/chat/TranscriptList.tsx`.
- 3.4.2 - Model and usage render on assistant turns. test: `web/src/components/activity/__tests__/SessionsTabDetail.test.tsx::renders_provenance`.
- 3.4.3 - Watched-session dropout preserves selection with an ended state. test: `web/src/components/activity/__tests__/SessionsTab.test.tsx::selection_preserved_on_dropout`.

### 3.5 Visual fixture parity across providers [category: test] (depends: 3.4)
`kind: deliverable`

Target: `web/src/__visual__/transcripts/`
Targets: `web/src/__visual__/transcripts/grok.json`, `web/src/__visual__/transcripts/cursor.json`

- Add the missing `grok.json` visual fixture (grok is the only provider with
  no snapshot coverage) and a `cursor.json` fixture once §5 lands.
- Regenerate all snapshots against the new contract; the snapshot suite
  asserts the previously-buggy cases: Codex shell summary non-empty, Codex
  read card rendered, diff blocks displayed, protocol cards from typed
  blocks, provenance line present.

**Acceptance:**

- 3.5.1 - Grok fixture and snapshot exist. file: `web/src/__visual__/transcripts/grok.json`.
- 3.5.2 - Snapshot suite passes with contract-shaped fixtures for all
  providers. test: `web/src/__visual__/transcripts/transcripts.test.tsx`.

## P4: Contract Enforcement
`kind: framing`

**Goal**: Format drift — upstream CLI changes or our own contract edits —
fails loudly in CI, and TypeScript types cannot diverge from the schema.

### 4.1 Golden fixture corpus and drift gate [category: test] (depends: P2)
`kind: deliverable`

Target: `tests/fixtures/provider_contracts/`
Targets: `tests/sessions/test_contract_fixtures.py`

Adopt the golden-fixture discipline every surviving multi-format parser uses:

- Reorganize `tests/fixtures/provider_contracts/<provider>/` so each raw
  fixture (version-pinned filename, e.g.
  `codex/terminal-functions-exec-rollout-0.144.6.jsonl`) has a committed
  expected normalized output
  (`<name>.normalized.jsonl`).
- `test_contract_fixtures.py` parametrizes over every pair: parse raw →
  render → (a) validate each message against
  `docs/contracts/schemas/normalized-transcript-message.schema.json`,
  (b) byte-compare to the committed expected output, (c) assert zero
  `unknown` blocks for pinned fixtures.
- A `--regen-golden` pytest flag regenerates expected files intentionally;
  unreviewed regeneration shows as a diff in code review.
- New-provider onboarding requires a corpus: the test fails if a provider in
  `PROVIDER_SPECS` has no fixture directory.

**Acceptance:**

- 4.1.1 - Every provider has raw + expected normalized fixture pairs. file: `tests/fixtures/provider_contracts/`.
- 4.1.2 - Drift gate validates schema, byte-equality, and zero unknown blocks;
  missing corpus for a registered provider fails. test: `tests/sessions/test_contract_fixtures.py`.

### 4.2 Generate TypeScript types from the contract schema [category: code] (depends: 1.1)
`kind: deliverable`

Target: `web/src/types/transcript.generated.ts`
Targets: `web/package.json`, `web/src/types/chat.ts`

- Generate `transcript.generated.ts` from
  `docs/contracts/schemas/normalized-transcript-message.schema.json` via
  `json-schema-to-typescript`, wired as a `web` package script
  (`npm run gen:transcript-types`); the generated file is committed.
- Hand-written duplicates in `web/src/types/chat.ts` and
  `web/src/hooks/useSessionDetail/types.ts` (`SessionMessage`,
  `ContentBlock`, `ToolCall`, `ToolResult`, `TokenUsage`) are replaced by
  imports from the generated module.
- A vitest check regenerates in-memory and fails if the committed file is
  stale, so schema edits force a visible type diff.

**Acceptance:**

- 4.2.1 - Generated types are the single frontend source for transcript
  shapes. file: `web/src/types/transcript.generated.ts`.
- 4.2.2 - Staleness check fails when the schema changes without regeneration.
  test: `web/src/types/__tests__/transcriptTypesGenerated.test.ts`.

## P5: Cursor CLI Integration
`kind: framing`

**Goal**: Cursor CLI (`agent` / `cursor-agent`) becomes provider #6 through
the new architecture — ProviderSpec entry, SQLite-capable transcript source,
parser, hooks adapter, watchdog reader — proving the "adding a CLI is now
cheap" claim against a non-JSONL provider.

Facts this phase builds on — research plus a local probe session verified
against `cursor-agent 2026.07.23-e383d2b` (July 2026): hooks are beta,
configured via `.cursor/hooks.json` / `~/.cursor/hooks.json` with a
`"version": 1` manifest and direct command entries (Claude-style matcher
groups do not fire). The CLI fires `sessionStart`, `stop`, `postToolUse`,
`afterFileEdit`, `beforeShellExecution`, `afterShellExecution`; payloads carry
`conversation_id`, `model`, `workspace_roots`, and `transcript_path`. CLI
sessions persist to `~/.cursor/chats/{md5(cwd)}/{session-uuid}/store.db` —
SQLite tables `meta` + `blobs`. `meta` holds hex-encoded JSON (`agentId`,
`latestRootBlobId`, `name`, `mode`, `blobEncryptionKey`). Message blobs are
plaintext JSON in AI-SDK shape: roles `system`/`user`/`assistant`/`tool`;
assistant content mixes `redacted-reasoning` (opaque payload), `text`, and
tool calls; `tool` records carry full `tool-result` parts plus structured
`providerOptions.cursor.highLevelToolCallResult` (file sizes, line counts,
paths). The root blob is a protobuf-framed ordered list of 32-byte child
hashes — the message ordering. The parallel
`~/.cursor/projects/*/agent-transcripts/*.jsonl` surface is invocation-only:
user/assistant text and `tool_use` calls, **no tool results, no reasoning**
(verified on both the CLI and IDE writers). Resume works only by chatId
against Cursor's own store (`--resume <chatId>` — no path input), and the CLI
has no export command (verified against the full subcommand list): the
agent-transcripts JSONL is its only export surface, so full fidelity is
reachable only by reading the store. The store layout is unofficial
and staff have signaled eventual IDE/CLI storage unification — the
golden-fixture gate (§4.1) is the defense.

### 5.1 Capture the Cursor fixture corpus [category: test]
`kind: deliverable`

Target: `tests/fixtures/provider_contracts/cursor/`

Run the installed Cursor CLI locally and capture version-pinned, sanitized
fixtures — the empirical ground truth the rest of P5 parses against:

- A `store.db` copy from `~/.cursor/chats/` covering: user turns
  (`<user_query>`/`<user_info>` wrappers), assistant turns (AI-SDK content
  arrays with `redacted-reasoning`, `text`, and tool calls), `tool` records
  with full results and `highLevelToolCallResult` metadata, the root/tree
  blobs, and the session `meta` row.
- The matching `~/.cursor/projects/*/agent-transcripts/*.jsonl` transcript
  (and any `agent-tools/*.txt` sidecar) for the same session — captured as
  the invocation-only secondary surface and to settle which surface
  `transcript_path` names.
- Hook payload captures for each CLI-fired event (`sessionStart`, `stop`,
  `postToolUse`, `afterFileEdit`, shell events).
- Filenames pin the CLI version (date-based, e.g.
  `session-2026.06.11.store.db`), matching the corpus convention.

Record findings (surface layout, which events actually fired, payload shapes)
in a `README.md` inside the fixture directory.

**Acceptance:**

- 5.1.1 - Version-pinned store.db, transcript, and hook-payload fixtures are
  committed. file: `tests/fixtures/provider_contracts/cursor/`.
- 5.1.2 - behavior: "captured surface layout and fired-event inventory"
  documented in `tests/fixtures/provider_contracts/cursor/README.md`.

### 5.2 TranscriptSource abstraction and Cursor import bridge [category: code] (depends: P1)
`kind: deliverable`

Target: `src/gobby/sessions/transcript_reader.py`
Targets: `src/gobby/sessions/processor_transcripts.py`, `src/gobby/sessions/transcript_archive.py`

The live-tail machinery assumes a tailable JSONL file path end to end. Cursor
breaks that assumption: probe evidence (P5 framing) shows the JSONL
agent-transcript is invocation-only, while full fidelity — tool results,
reasoning, per-message model — exists only in the session `store.db`. The
SQLite store is Cursor's canonical transcript source, but Gobby never copies
or restores that database: file-copying a live WAL database is unsafe
(committed records can live only in the WAL, and the SHM member is a shared
memory map), and resume is Cursor's own job via `--resume <chatId>` against
its store. Gobby reads *through* SQLite and archives into its own container:

- A `TranscriptSource` protocol with two implementations: `FileTailSource`
  (extracted existing behavior — byte offsets, `.idx` sidecar, gzip archive)
  and `SQLiteStoreSource` (read-only snapshot reads of a live `store.db` via
  `file:...?mode=ro` — consistent under WAL, never copies files — walking
  `meta.latestRootBlobId` → root-blob hash list → message blobs into record
  dicts; monotonic record cursor instead of byte offset).
- The read path is empirically settled (probed 2026-07-29 against the live
  store, SQLite 3.50.4). `mode=ro` reads a WAL database correctly in every
  case the bridge faces: rows committed only to the WAL are visible while
  another connection holds the database open; an uncommitted write txn in
  flight is correctly invisible and appears immediately after its commit; and
  an abandoned session (WAL with content, `-shm` absent, no holder) still
  reads. Writes on the connection are refused by SQLite itself. **`immutable=1`
  is prohibited**: it reports `journal_mode: delete` and ignores the WAL
  entirely, which silently drops the newest messages — precisely the hazard
  that motivated the superseded E6 archive bundle.
- Source selection reads the discovered candidate: discovery (§1.4) yields
  `TranscriptCandidate(path, kind)` by stamping the owning spec's
  `transcript_source_kind` (§1.2) onto each hit, and the processor, reader, and
  archival consume the candidate directly — no second registry, no runtime
  sniffing. Cursor declares `store.db` as its only content surface, so a store
  read that fails has no fallback surface to degrade into.
- **Import bridge for archival**: at session expiry (and on demand), an
  importer drains the store through `SQLiteStoreSource` and writes a
  **message-level JSONL export**: one decoded-meta header record (session
  title, mode, model, agentId — meaningful fields, not raw hex), then each
  message's decoded provider-native AI-SDK JSON, unmodified, one per line in
  root-blob order — thinking, tool calls, full tool results,
  `highLevelToolCallResult` metadata, everything the store carries per
  message. The artifact flows through the existing
  `~/.gobby/session_transcripts/{external_id}.jsonl.gz` archive path like
  every other provider. Export is deterministic and idempotent: re-runs over
  a grown store append only new messages and never rewrite existing lines.
  No database file copies, no dump of rows Gobby doesn't own, no normalized
  artifact, no new tables — the archive holds provider-native message
  shapes, and normalization stays on-demand.
- **No restore tool, by design**: Gobby never writes transcript state into
  Cursor-owned paths, so native `--resume` of an archived Cursor session works
  only where Cursor's own store still exists (same machine, same cwd).
  Cross-machine handoff is transcript/summary handoff, as with any provider —
  Gobby does not attempt to reconstruct a database schema it doesn't own.
- Cursor's JSONL agent-transcript surface is **not** a declared transcript
  surface and is never read for content: it is invocation-only, so a fallback
  read would yield a transcript stripped of tool results and reasoning while
  looking superficially complete. Recognizing the path as Cursor's rides
  `spec.path_markers` (§1.2), and §5.4 resolves an incoming hook
  `transcript_path` to the owning store before session registration, keeping
  the JSONL path only as correlation evidence.

**Acceptance:**

- 5.2.1 - Source protocol with file and sqlite implementations; selection
  driven by the discovered candidate's kind. symbol: `TranscriptSource`. file: `src/gobby/sessions/transcript_reader.py`.
- 5.2.2 - SQLite source yields stable-ordered records and non-duplicated
  message IDs across repeated snapshot reads of a growing store, including a
  record committed only in the WAL, and never opens the store with
  `immutable=1`. test: `tests/sessions/test_transcript_sources.py::test_sqlite_source_stable_ordering`.
- 5.2.3 - Exported JSONL carries every message's full decoded content
  (thinking, tool calls, results, diff metadata) in root order; re-export is
  idempotent, and normalizing the exported artifact yields byte-identical
  output to normalizing the live store. test: `tests/sessions/test_transcript_sources.py::test_cursor_import_bridge_round_trip`.

### 5.3 Cursor transcript parser and ProviderSpec entry [category: code] (depends: 5.2)
`kind: deliverable`

Target: `src/gobby/sessions/transcripts/cursor.py`
Targets: `src/gobby/providers/registry.py`

Parses the §5.1 store fixtures into normalized messages. The captured JSONL
agent-transcript is corpus evidence for path correlation only (§5.2) and is
never a parser input:

- `CursorTranscriptParser(BaseTranscriptParser)`: store records →
  `ParsedMessage`. Ordering comes from the root blob's hash list (P5
  framing); message blobs decode as AI-SDK-shaped JSON. One decode path
  serves both inputs: live `SQLiteStoreSource` records and the §5.2 exported
  archive artifact (same decoded records, same order, different container). Strip
  `<user_query>`/`<user_info>` wrappers; `redacted-reasoning` maps to an
  opaque thinking block; tool calls pair with `tool` records by
  `toolCallId`, preferring `highLevelToolCallResult` structured output for
  result metadata; `meta.name` → `session_title`; model from
  `providerOptions.cursor.modelName`; unrecognized shapes →
  `unmodeled_record` + observation telemetry (storage-unification churn is a
  known hazard — tolerance over strictness).
- ProviderSpec entry `cursor`: binary `agent` (alias `cursor-agent`),
  config dir `~/.cursor`, `transcript_source_kind: "sqlite"` with
  `chats/{cwd_hash_md5}/*/store.db` as the sole declared content surface, path
  markers covering both `chats/` stores and `projects/*/agent-transcripts/`
  paths so hook payloads naming the JSONL still resolve to cursor,
  record-sniff predicate, tool aliases and arg-key maps from fixture evidence,
  `usage_source` per what fixtures show.
- `SessionSource` gains `cursor`; the §1.2 parity guards force the watchdog
  and parser registries in the same change.

**Acceptance:**

- 5.3.1 - Parser normalizes the fixture corpus from both containers of store
  content — live store records and the §5.2 exported archive — producing
  identical decoded records in identical order.
  symbol: `CursorTranscriptParser`. file: `src/gobby/sessions/transcripts/cursor.py`.
- 5.3.2 - ProviderSpec entry registered; parity guards pass with the new
  provider. file: `src/gobby/providers/registry.py`.
- 5.3.3 - Cursor golden fixtures pass the §4.1 drift gate (schema-valid, zero
  unknown blocks). test: `tests/sessions/test_contract_fixtures.py`.

### 5.4 Cursor hooks adapter, capabilities, and install assets [category: code] (depends: 5.1)
`kind: deliverable`

Target: `src/gobby/adapters/cursor.py`
Targets: `src/gobby/adapters/capabilities.py`, `src/gobby/install/cursor/`

- Declare `_cursor_capabilities()` first (the adapter-fidelity gate in
  `docs/guides/adapter-fidelity.md` requires capabilities before adapter
  behavior), reflecting the partial CLI event coverage: `sessionStart`,
  `stop`, `postToolUse`, `afterFileEdit`, `beforeShellExecution`,
  `afterShellExecution`. No `sessionEnd` — session expiry rides the existing
  watchdog/expiry path as with other providers lacking end events.
- Hook adapter translates Cursor payloads (`conversation_id` → external id,
  `transcript_path`, `model`, `workspace_roots`) to `HookEvent`s. Cursor's
  `transcript_path` names the invocation-only JSONL, so `sessionStart`
  **resolves it to the owning `store.db`** — via `conversation_id` against the
  ProviderSpec store glob — and registers that store path with
  `register_session`. Registering the JSONL verbatim would hand every
  downstream reader a path that can never yield tool results. The JSONL path is
  retained only as correlation evidence for matching a payload to a session.
  When `transcript_path` is absent, registration falls back to the same
  store discovery.
- Install assets write the project/user `hooks.json` (`"version": 1`, direct
  command entries — not Claude-style matcher groups, which silently never
  fire) pointing at ghook, and add the required `cursor` arm to
  `CliConfig::for_cli` plus its test — without it `ghook --cli=cursor` exits 2
  and drops every event (see Constraints).

**Acceptance:**

- 5.4.1 - Capabilities declared before adapter behavior; event coverage
  matches the fixture-verified fired set. file: `src/gobby/adapters/capabilities.py`.
- 5.4.2 - Adapter normalizes captured hook payloads into `HookEvent`s, and a
  `sessionStart` payload whose `transcript_path` names the JSONL registers the
  resolved `store.db` path instead. symbol: `CursorAdapter`. file: `src/gobby/adapters/cursor.py`.
- 5.4.3 - Install writes a valid version-1 hooks.json manifest. test: `tests/install/test_cursor_install.py::test_hooks_manifest_shape`.
- 5.4.4 - `ghook --cli=cursor` dispatches instead of exiting 2. test: `crates/ghook/src/cli_config.rs` cursor arm test.

### 5.5 Cursor watchdog reader [category: code] (depends: 5.3)
`kind: deliverable`

Target: `src/gobby/agents/watchdog/cursor.py`

Structural-only liveness classification over the Cursor session store,
matching the existing reader pattern (`classify(line_num, data) ->
ScanVerdict` fed to the shared scan loop; snapshots stay content-free per the
redaction boundary). Registered in `_READERS` and
`KNOWN_WATCHDOG_PROVIDERS` together (the import-time guard hard-fails
otherwise).

**Acceptance:**

- 5.5.1 - Reader classifies activity/idle/capacity from fixture stores.
  file: `src/gobby/agents/watchdog/cursor.py`.
- 5.5.2 - Registry and known-providers guard updated together. test: `tests/agents/watchdog/test_registry.py`.

### 5.6 Cursor documentation and integration-matrix promotion [category: docs] (depends: 5.4)
`kind: deliverable`

Target: `docs/research/cli-integration-matrix.md`
Targets: `docs/guides/adapter-fidelity.md`, `docs/guides/providers-and-models.md`

Update the provider documentation surfaces the integration-matrix protocol
requires: the matrix baseline row and 3-surface readiness scores for cursor,
the `docs/guides/adapter-fidelity.md` provider table, and
`docs/guides/providers-and-models.md`. Note the known instability signals
(beta hooks, storage-unification plans, date-based versioning) and the
fixture-gate defense.

**Acceptance:**

- 5.6.1 - Matrix row with readiness scores for cursor exists. file: `docs/research/cli-integration-matrix.md`.
- 5.6.2 - Adapter-fidelity provider table includes cursor. file: `docs/guides/adapter-fidelity.md`.

### 5.7 Cursor spawn support and feature-tier registration [category: code] (depends: 5.4)
`kind: deliverable`

Target: `src/gobby/agents/spawners/command_builder.py`
Targets: `src/gobby/agents/provider_capabilities.py`, `src/gobby/agents/spawners/auth_env.py`, `src/gobby/providers/registry.py`, `src/gobby/config/feature_candidate_defaults.py`

Full integration means Gobby can spawn cursor agents, not just observe them
(scope decision, enhancement round 1):

- `ProviderMetadata("cursor", "cursor-agent", "Cursor", ".cursor")` joins the
  registry in `src/gobby/providers/registry.py`.
- `build_command` gains a `cursor` arm in
  `src/gobby/agents/spawners/command_builder.py`: headless spawn via
  `cursor-agent -p --output-format stream-json --force`, model via
  `--model`, workspace via `--workspace`, resume via `--resume <chatId>`.
- `ProviderCapabilities` entry in
  `src/gobby/agents/provider_capabilities.py`: sandbox supported
  (`--sandbox enabled|disabled`); no reasoning-effort flag family — effort
  is encoded in Cursor's model names (`-high`, `-xhigh` variants).
- Auth env passthrough in `src/gobby/agents/spawners/auth_env.py`
  (`CURSOR_API_KEY`).
- `feature_low`/`feature_mid`/`feature_high` candidate defaults in
  `src/gobby/config/feature_candidate_defaults.py` gain cursor entries from
  the CLI's live catalog (`cursor-agent --list-models`, verified July 2026:
  `composer-2.5` for low/mid, `claude-opus-5-thinking-high` /
  `gpt-5.6-sol-high` class models for high — exact picks pinned at
  implementation time against the then-current catalog).

**Acceptance:**

- 5.7.1 - Command builder produces a headless cursor invocation with model,
  workspace, and resume support. test: `tests/agents/spawners/test_command_builder.py::test_cursor_command`.
- 5.7.2 - Provider metadata, capabilities, and auth-env entries registered
  together. file: `src/gobby/agents/provider_capabilities.py`.
- 5.7.3 - Cursor models present in feature-tier candidate defaults.
  file: `src/gobby/config/feature_candidate_defaults.py`.

## D1 Deferred: gwiki migration to the normalized contract
`kind: deferred`

The Rust wiki-ingest layer (`crates/gwiki/src/ingest/session*.rs`, five
per-provider adapters with an order-dependent selection chain) keeps parsing
raw archives unchanged during this epic. Once the contract stabilizes and the
gwiki refactor is underway, gwiki consumes §2.7's normalize-on-demand surface
through a single reader and the per-provider adapters are deleted. Cursor CLI
wiki ingest intentionally waits for that migration.

```yaml
deferral:
  task_ref: "#19207"
  reason: "gwiki is entering its own monorepo refactor; migrating it against an unproven contract would churn Rust twice. The contract must stabilize through this epic's fixture gauntlet first."
  owner: "josh"
  original_acceptance_items:
    - D1.1
    - D1.2
```

## D2 Deferred: OpenTelemetry GenAI export projection
`kind: deferred`

Observability-standard export (OTel GenAI spans/events) is a projection over
the normalized format, not a storage schema. It waits for the OTel GenAI agent
conventions to stabilize and for this epic's contract to ship.

```yaml
deferral:
  task_ref: "#19208"
  reason: "OTel GenAI agent conventions are still in Development status; exporting an unstable projection of a brand-new contract compounds two moving targets."
  owner: "josh"
  original_acceptance_items:
    - D2.1
```

## D3 Deferred: public Rust crate port
`kind: deferred`

The post-0.5.0 port of the canonical parsing layer to a standalone public
Rust crate ("serde for agent CLI transcripts"), implementing the
language-neutral contract with provable fixture parity. Internal-first was an
explicit scope decision for this epic.

```yaml
deferral:
  task_ref: "#19209"
  reason: "Pre-0.5.0 the pain is internal dispersion and the renderer contract; a public API freeze now buys nothing and slows the refactor. The language-neutral contract and fixtures built here make the port mechanical later."
  owner: "josh"
  original_acceptance_items:
    - D3.1
```

## V1 Plan Changelog
`kind: verification`

**Round 1** `kind: enhancement`

- enhancer_run: 51c63307-52ed-4ed3-a691-119c429f647b (codex/gpt-5.6-sol, xhigh)
- enhancer_session: da8f6971-07da-4b8b-b1ce-c6ead5e25286
- converged: false (round cap max_enhancement_rounds=1 reached)
- suggestions_presented: 6
- votes_collected: 2026-07-29 (see resolution_notes — the round-1 report was
  applied before the user had voted on it; every item below was presented
  verbatim and voted individually on 2026-07-29)
- accepted:
  - E2 closed ToolType enum / clarity — `tool_type` pinned to the closed
    classifier vocabulary in the schema, Python model, and generated TS union;
    per-member fixture coverage (1.1, 2.1, 4.2). Vote: keep accepted.
  - E5 portable identity rule / testability — shared `SourcePosition` input
    across readers, path-independence + partial→complete ID acceptance
    (2.6, 5.2). Vote: keep accepted.
- superseded:
  - E4 per-surface source kinds / clarity — its premise ("Cursor has two
    concrete transcript surfaces") was removed by the user: the invocation-only
    JSONL is no longer a declared surface at all, since it carries no tool
    results and a fallback read would silently produce gutted transcripts. With
    one content surface per provider, `TranscriptSurface(glob, kind)` was
    dropped for a provider-level `transcript_source_kind`; discovery still
    stamps `TranscriptCandidate(path, kind)`. Path recognition moved to
    `spec.path_markers`, and §5.4 now resolves a hook `transcript_path` to the
    owning store before `register_session` (1.2, 1.4, 5.2, 5.3, 5.4)
  - E6 WAL-safe SQLite archive / testability — the db+WAL/SHM byte bundle was
    superseded by the import bridge, but E6's underlying hazard was verified
    and kept: a 2026-07-29 probe (SQLite 3.50.4) confirmed `mode=ro` reads
    WAL-only committed rows with a concurrent holder, correctly hides an
    uncommitted txn, and reads an abandoned session with no `-shm`; and
    `immutable=1` reports `journal_mode: delete` and ignores the WAL entirely.
    Acceptance 5.2.2 keeps the WAL-only-record requirement and now also forbids
    `immutable=1` (5.2)
- declined:
  - E1 remove conditional ghook Rust exception / clarity — user decision: full
    Cursor integration is in scope ("the works"); the bounded single-arm
    exception stands. Follow-on: §5.7 added (spawn support, provider
    metadata, feature-tier candidates). The conditional wording was corrected
    to a requirement: `CliConfig::for_cli` (`cli_config.rs:21`) falls to
    `_ => None` for cursor and `dispatch.rs:25` answers `None` with
    `emit_empty_json()` + exit 2, so the arm is mandatory, not contingent
  - E3 pin truncation byte semantics / clarity — mooted by user decision "no
    truncation anywhere": §2.3's wire cap and the dead `truncated` field were
    struck instead; the normalized format always carries full payloads
- resolution_notes: Mid-round the user redirected two designs with empirical
  verification. A local probe against cursor-agent 2026.07.23-e383d2b decoded
  the store.db format (meta hex-JSON → protobuf-framed root hash list →
  plaintext AI-SDK message blobs; JSONL surface confirmed invocation-only on
  both CLI and IDE writers) — P5 framing, 5.1, 5.2, and 5.3 updated to the
  verified format, settling the E4 surface question with store.db as
  canonical. The user also expanded scope to full Cursor integration
  (spawn + feature tiers, new §5.7). Plan re-validated after edits.
  Post-round addendum (pre-adversary, user decision 2026-07-28): E6's
  archival mechanism was superseded — copying a live WAL database (db+WAL/SHM
  bundle) is unsafe, and since resume is Cursor-side by chatId, Gobby never
  needs the database file. §5.2 was redesigned to an import bridge: read-only
  snapshot reads through SQLite plus an idempotent raw-blob JSONL archive
  artifact riding the standard archive path; acceptance 5.2.3 now pins import
  idempotency and live-vs-imported normalization equivalence instead of the
  WAL member bundle. Constraints, P5 framing, and §5.3 updated to match.
  Second ruling same day: the archive is a message-level JSONL export of the
  decoded store content — provider-native AI-SDK message JSON in root order
  plus a decoded-meta header — with thinking, tool calls, full results, and
  diff metadata intact. No byte-for-byte dump, no restore tool, no
  reconstruction ambitions: Gobby never writes into Cursor-owned state, does
  not preserve rows of a database schema it doesn't own, and native
  cross-machine resume is explicitly out of scope (transcript/summary
  handoff instead).
  Vote correction (2026-07-29): the round-1 report had been folded into the
  plan without the contract-required per-item vote, so the six suggestions were
  re-presented verbatim and voted individually. E2 and E5 confirmed as
  accepted; E1 and E3 confirmed as declined; E4 and E6 recorded as superseded
  per the dispositions above. Three plan corrections came out of that pass.
  (a) The ghook exception is not conditional — verified against
  `cli_config.rs:21` and `dispatch.rs:25` — so Constraints and §5.4 now state
  the cursor arm as required, with acceptance 5.4.4. (b) The invocation-only
  JSONL was removed from `transcript_surfaces` entirely and `TranscriptSurface`
  collapsed into a provider-level `transcript_source_kind`; §5.4 resolves hook
  `transcript_path` to the owning store before registration. (c) `mode=ro`
  read-through was empirically validated rather than assumed, `immutable=1` was
  prohibited in acceptance 5.2.2, and the "never writes into Cursor-owned
  state" constraint was corrected to match measured behavior: a read-only WAL
  open creates the ephemeral 32 KiB `-shm` index when absent, while `.db` and
  `-wal` bytes and mtimes stay unchanged.
