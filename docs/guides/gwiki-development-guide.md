# gwiki Development Guide

`gobby-wiki` owns wiki files and derived wiki indexes. It treats Gobby daemon services as required processing sources that can degrade at runtime. Runtime code must resolve the daemon base URL through `gobby_core::daemon_url::daemon_url()` or `daemon_url_at()` and must report unavailable required services as structured degradation metadata.

## PostgreSQL Search

`gwiki` PostgreSQL search depends on ParadeDB `pg_search` BM25 indexes. The standalone setup path in `crates/gwiki/src/setup.rs` preflights the `pg_search` extension before creating `gwiki_documents_search_bm25` and `gwiki_chunks_search_bm25`; do not remove that preflight or replace it with plain PostgreSQL full-text search unless the ranking contract changes.

Hybrid search fuses BM25, semantic, and graph hits by canonical wiki page key:
`<scope-kind>:<scope-id>:<wiki-page-path>`. Do not fuse by backend-specific hit
ID; chunk IDs, Qdrant point IDs, and graph document IDs for the same page must
merge into one search result while preserving all `sources`, per-source
`explanations`, and the emitted `fusion_key`.

## Retrieval Primitive: `search` and `ask`

`gwiki search` is the single bounded retrieval primitive for both humans and
agents. There is no standalone reason-act `research` loop; the calling agent
composes `search` + `read` itself. Do not reintroduce a `research` command. Each
search hit carries a bounded query-token snippet (never a full document body),
provenance (`wiki_page`, `source_path`, `result_type`), contributing `sources`
with per-source `explanations`, and hit-tied `code_citations`.

`gwiki ask` is a thin bounded-evidence RAG layer over `search`. It retrieves the
top-k hits, builds a query-centered evidence prompt capped at
`prompt_token_budget` (~12K estimated tokens), and — only with `--llm` — runs a
single completion. Keep `ask` thin: it must not grow into a multi-step research
agent. Without `--llm` it is a pure retrieval command with no AI route.
Synthesized answers are checked post-generation against the retrieved evidence;
the verdict lands in `synthesis.citation_check` (see
`crates/gwiki/src/output.rs`).

Both commands accept `--token-budget <N>`, which trims results/retrieval hits to
fit an approximate token budget and emits a narrowing hint when hits are dropped.
On `ask` this applies in addition to the `prompt_token_budget` evidence cap.

## Session Transcript Ingest (`sync-sessions`)

`gwiki sync-sessions` folds daemon-synthesized Gobby session wiki pages into the
vault. Raw `*.jsonl.gz` archives stay in the present-source reconciliation set,
but are parsed only when the caller explicitly passes `--raw` for legacy
fallback. The raw ingest path lives in `crates/gwiki/src/ingest/session.rs` with
per-CLI adapter submodules (`codex`, `grok`, `qwen`, `droid`, plus the Claude
Code adapter), a `metadata` module that emits deterministic session
frontmatter, and a `redaction` module that redacts secrets on ingest before any
derived Markdown is written. The CLI surface accepts `--archive-dir <PATH>`
(directory of `*.jsonl.gz` archives), `--wiki-dir <PATH>` (directory of
synthesized `*.md` pages), `--limit <N>`, and `--raw`; the command contract
(`crates/gwiki/src/contract.rs`) classifies it with `multimodal:
"session_transcript"` and the `embeddings`/`FalkorDB graph` degradation sources.
Keep redaction ahead of vault writes and keep session frontmatter deterministic
so re-syncing the same archive is idempotent.

## Vault Markdown Normalization (`normalize`)

Markdown is normalized in two places. `crate::markdown::normalize` normalizes at
write time, so it only fixes *new* writes; `gwiki normalize`
(`crates/gwiki/src/normalize.rs`, dispatched from
`crates/gwiki/src/commands/normalize.rs`) is the companion that repairs whitespace
in docs already on disk (markdownlint repair). Normalization targets authored
docs and must leave raw captures untouched. The `--check` flag is an exit-code
gate: `commands::normalize::execute` sets the outcome exit code from
`normalize::check_exit_code(&report)` so CI fails on un-normalized markdown,
while write mode always succeeds and must not mutate files in check mode.

## Lint Checks

`gwiki lint` (`crates/gwiki/src/lint.rs`) detects broken links and vault hygiene
issues, and additionally runs Mermaid diagram checks on curated wiki pages:

- **Mermaid validity** — malformed Mermaid blocks are flagged invalid, and
  diagram headers are checked against the supported set. `lint`'s `VALID_HEADERS`
  mirrors gcode's `architecture_diagrams::VALID_HEADERS`; keep the two in sync
  when the curated wiki generator's header vocabulary changes.
- **Mermaid grounding** — quoted node labels (`id["label"]`) are extracted from
  each Mermaid block and checked for support in the surrounding page text, so
  diagrams cannot assert structure the page does not back. Edge lines and
  `subgraph … ["Title"]` titles are handled separately from node labels.

## Static Agent Exports (`graph`)

`gwiki graph` (`crates/gwiki/src/exports.rs`) writes the unified graph artifacts
(`graph.json`, `GRAPH_REPORT.md`) and, alongside them, three static
agent-context exports under `outputs/`: `llms.txt` (a portable link-indexed
table of contents over the vault document graph), `llms-full.txt` (the same
index expanded with each present document's full Markdown body), and
`graph.jsonld` (a schema.org JSON-LD representation of the vault document graph).
Export code should surface invalid graph input as typed errors and reuse the
shared graph analytics metrics rather than recomputing them; the export set is
written and rolled back as a unit so a partial failure does not leave stale
agent artifacts on disk.

## Trust Layer

`gwiki trust` is the shared status surface for agents and humans. It composes
runtime service configuration, index counts, health findings, link hygiene,
freshness, graph coverage, and audit results into one JSON report.

PostgreSQL, FalkorDB, Qdrant, and embeddings are required for full search and
graph-backed behavior. Missing services must surface as explicit degradations.
The command may fall back to a memory vault scan only to report counts when
PostgreSQL is unavailable; that fallback must set `index_counts.backend` to
`memory` and keep the datastore degradation visible.

Current Trust Layer limits:

- Graph metrics report wiki link coverage and FalkorDB configuration only.
  Community detection, centrality, bridge nodes, graph exports, and architecture
  hotspot analytics remain roadmap work.
- Audit state reports unsupported claim counts from the current local audit.
  Citation quality scoring, contradiction detection, and source credibility
  scoring remain roadmap work.
- Freshness is based on existing stale page and stale citation checks. Automatic
  librarian loops and change-triggered refresh remain roadmap work.

## Source Lifecycle

`gwiki index` rebuilds derived search rows from the vault files already present on disk. It must not re-fetch remote sources or mutate raw source records. After PostgreSQL indexing, it synchronously updates Qdrant vectors and the FalkorDB graph for the same scope; do not add daemon sync flags or delayed daemon-owned index work.

`gwiki purge` is the explicit reset path for generated/indexed state. It
requires a project or topic scope plus `--yes`; global purge is rejected. The
command deletes scoped PostgreSQL rows first, then clears scoped Qdrant vectors
and FalkorDB wiki graph data when those backends are configured. Keep purge
scoped and summary-producing so operators can verify which backend state was
removed.

`gwiki refresh` is the source-maintenance path. It refreshes manifest records from `raw/INDEX.md`, using existing global scope flags (`--project` or `--topic <name>`), repeated `--id <SOURCE_ID>` selectors, and `--dry-run`. Do not add `--scope`.

URL refresh compares fetched response bytes to the existing manifest `content_hash`. Local `ingest-file` replay compares the current source file bytes to `content_hash` using the stored local path and effective ingest options. Unchanged sources are reported without rewriting raw files, rerunning AI/media extraction, or indexing. Changed sources get a new content-hash-derived source ID, replace the old manifest entry, remove superseded `raw/<old_id>.md` and `raw/assets/<old_id>.*` paths, then run indexing once after the changed batch.

Structured JSON output must preserve stdout even for all-failed explicit refreshes. The response includes `command`, `scope`, `status`, `dry_run`, `planned`, `refreshed`, `unchanged`, `failed`, `skipped`, `indexed`, `index_status`, and `degradations`; `index_status.index_required` is always false after the CLI exits. Daemon, MCP, and web refresh surfaces should call the real CLI command and pass only the existing scope flags.

Refresh must remain manifest-driven: do not scan directories, expand globs, or infer replay behavior from raw assets. Stdin, research notes, MediaWiki, Wayback, and Git repository records stay unsupported until they have explicit replay contracts. Missing replay metadata on local file/media records is a structured failure.

## Daemon Capability Probe

`crates/gwiki/src/daemon.rs` defines the current probe contract. It returns a `DaemonCapabilityReport` with one availability record per capability plus a top-level `degraded` list. A 2xx response marks an endpoint available. `405 Method Not Allowed` also marks a route as present for mutating endpoints probed without a body. `404`, `422`, auth failures, unexpected statuses, and transport failures become `DaemonDegradation` entries with capability, endpoint, reason, status, message, and fallback.

## Verified Endpoint Contracts

The endpoint shapes below were verified from the daemon source under `src/gobby/servers/routes` and `src/gobby/servers/websocket`.

| Capability | Endpoint | Request Shape | Response Shape | Degraded Behavior |
|---|---|---|---|---|
| Embeddings maintenance | `POST /api/memories/embeddings/reindex` | Query `project_id?`; no JSON body. | Object from `memory_manager.reindex_embeddings`, including generated/total counts or error detail on failure. | Do not use this as arbitrary text embedding for gwiki. Disable daemon-backed embedding generation and keep lexical/wiki indexing paths available until a dedicated request-time embedding endpoint exists. |
| Synthesis provider discovery | `GET /api/providers/models` | No body. | `{ "providers": [ { "provider", "available", "models", "source", "startup_error", ... } ] }`. | Skip daemon-assisted synthesis and return source/manually-authored wiki content. |
| Synthesis stream | `WS /ws` message `chat_message` | JSON frame: `type: "chat_message"`, `conversation_id?`, `content?`, `content_blocks?`, `model?`, `provider?`, `project_id?`, `request_id?`, `reasoning_effort?`, `attachments?`, `tts_enabled?`. | Frames include `connection_established`, `session_info`, `chat_stream` chunks with `done`, `chat_thinking`, `tool_status`, and `chat_error`. Final `chat_stream` may include usage, context window, session ref, and SDK session id. | Degrade synthesis, keep source collection/indexing results, and do not assume a provider-native completion schema. |
| Vision attachment ingress | `POST /api/chat/attachments` | Multipart form: `file`, `draft_id?`, `project_id?`. | Attachment object with `id`, `project_id`, `filename`, `mime_type`, `size_bytes`, local storage metadata, and timestamps. | Store raw image assets and surface filename/metadata only. Visual extraction stays skipped. |
| Vision prompt path | `WS /ws` message `chat_message` | Use `content_blocks` and/or `attachments` with stored attachment IDs. | Same streaming frames as synthesis. The daemon does not expose a separate image-analysis REST endpoint. | Treat vision as unavailable when attachment upload or model support is absent. Never drop the raw asset. |
| Transcription status | `GET /api/voice/status?want_stt=true` | Query `want_stt?`, `want_tts?`. | Voice status object with `enabled`, `stt_enabled`, `stt_available`, `stt_reason`, warmup fields, `voice_ready`, and TTS fields. | Keep raw audio assets and require supplied transcripts. |
| Transcription request | `POST /api/voice/transcribe` | Multipart form: `file`. | `{ "text", "bytes", "content_type" }` on success or `{ "error", "text": "" }` when disabled/unavailable/failing. | Keep raw audio assets; mark transcript generation degraded. |
| Agent dispatch | `POST /api/agents/spawn` | JSON `AgentSpawnRequest`: `task_id`, `agent_name?`, `prompt?`, `web_chat?`, `isolation?`, `provider?`, `model?`, `reasoning_effort?`, `reasoning_required?`, `workflow?`, `branch_name?`, `base_branch?`, `timeout?`, `max_turns?`. | `AgentSpawnResponse`: `success`, optional `run_id`, `child_session_id`, `conversation_id`, `isolation`, `branch_name`, `pid`, `message`, `reasoning`, or `error`. | Return dispatch degradation metadata. `gwiki` must not spawn or manage internal subprocesses. |
| Build/lifecycle dispatch | `POST /api/build` | JSON `BuildRequest`: `input_ref`, `project_id?`, `cwd?`, `quick?`, `skip_stages?`, `isolation?`, `workspace_backend?`, `agent?`, `stage?`, `max_active_agents?`, `max_retries?`, and related build options. | Build result object containing lifecycle run state and dispatcher summary fields. | Use only as daemon integration for higher-level build automation; no local process fallback. |
| Session list and monitoring degradation | `GET /api/sessions` | Query filters such as `source?`, `project_id?`, `status?`, `limit?`, `offset?`. | Session listing object with session rows and count/pagination fields. | Disable live monitoring and rely on explicit command output. |
| Session messages | `GET /api/sessions/{session_id}/messages` | Query `limit?`, `offset?`, `role?`, `format?`. | `{ "status": "success", "messages", "total_count", "response_time_ms", "format" }`. | Polling history is unavailable; surface no live session transcript. |
| Session/event stream | `WS /ws` | Client frames include `subscribe`, `unsubscribe`, `continue_in_chat`, `attach_to_session`, `detach_from_session`, and `send_to_cli_session`. | Broadcast frames include task/session/chat events plus request-correlated responses. Initial connection sends `connection_established`. | Use REST polling where available; otherwise mark monitoring degraded. |

## Integration Rules

- Endpoint availability is a probe result, not an implementation permission. Mutating daemon routes must still be called only by the feature that owns the mutation.
- `POST /api/memories/embeddings/reindex` is memory maintenance. It is not a request-time embedding API for arbitrary wiki text.
- Image and audio ingestion must write raw assets before calling required daemon processing services. Missing vision or transcription records required-service degradation and preserves raw files.
- Agent dispatch belongs to the daemon: `gwiki` sends requests to `/api/agents/spawn` or `/api/build` and records daemon responses. It must not shell out to Codex, Claude, tmux, or subprocess runners directly.
