Plan artifact: `.gobby/plans/eliminate-arbitrary-output-truncation.md`

# Eliminate Arbitrary Output Truncation — Remaining Work

## Overview
`kind: framing`

Epic #20395. The contract is already in `docs/contracts/truncation.md`: do not prefix-slice a complete payload the caller is meant to receive. Seven leaves under that epic are closed. This plan is only the six remaining conversions.

## Constraints
`kind: framing`

Follow `docs/contracts/truncation.md`. When a payload is too large to inline, use exactly one of: return the full text; offload and point; omit a whole item; or (logs only) an intentional tail after persisting the full stream if that tail would be the only copy.

Do not add a new MCP server, overflow table, or repo-wide grep linter. Reuse `ToolResultOffloader` / `gobby-results`, `get_handoff_context`, `get_agent_capture`, `get_recall_memories`, `get_session_messages`.

No backward compatibility. Production `.py` / `.rs` files stay under 1,000 lines. Rust: load `rust`, `cargo test -p <crate>`, reinstall `~/.gobby/bin/{gcode,gdaemon,ghook,gwiki}`. Python: `GOBBY_TEST_PROTECT=1` and scoped pytest only.

Do not reopen closed work: contract (#20396), Qwen parse (#20397), dispatch `failure_context` (#20398), digest/expansion/validation/pipeline inputs (#20399), session-message fields (#20401), chat-history bodies (#20402), cron/gcode failure output (#20403).

## P1: Native tool-loop
`kind: framing`

**Goal**: gcore stops feeding the model a prefix of an oversized tool result.

### 1.2 Stop prefix-slicing gcore tool-loop results
`kind: deliverable`

Targets:
- `crates/gcore/src/ai/generation/tool_loop.rs::truncate_utf8`
- `crates/gcore/src/ai/generation/tool_loop.rs::ToolLoopLimits`
- `crates/gcore/src/ai/generation/tests/tool_loop.rs`

Today:

```rust
let result = truncate_utf8(result, limits.max_bytes_per_tool_result);
messages.push(ChatMessage::tool_result(call.id.clone(), result));
```

That keeps 16KB of a 20KB result and drops the rest. Delete that ingest chop.

If `result.len() <= max_bytes_per_tool_result`, push `result` unchanged. If larger, write the **full** result to a sidecar when `artifact_dir` is available and push a **pointer-only** tool message (path, byte count, “read the sidecar / re-query”). With no artifact directory, still do not prefix-slice: pointer/re-query text only.

`max_bytes_per_tool_result` is the inline budget, not a license to keep a prefix. `truncate_utf8` may remain as a char-boundary primitive; it must have no caller at the tool-result ingest site.

Add `artifact_dir: Option<PathBuf>` on the loop run context. gwiki/gdaemon pass their run directory.

Tests: under-cap unchanged and no sidecar; over-cap with `artifact_dir` writes bytes equal to the original and the chat message contains no prefix of the body; over-cap without `artifact_dir` is pointer text only.

Validate: `cargo test -p gobby-core tool_loop`, `cargo clippy -p gobby-core`. Reinstall binaries that embed the loop.

**Acceptance:**

- 1.2.1 - Oversized tool results are never prefix-sliced into the model message. symbol: `ToolLoopLimits`.
- 1.2.2 - Over-cap results persist in full to a sidecar when an artifact directory exists. file: `crates/gcore/src/ai/generation/tool_loop.rs`.
- 1.2.3 - Tests prove the model message has no prefix of the oversized body. test: `crates/gcore/src/ai/generation/tests/tool_loop.rs`.

## P2: Durable sinks
`kind: framing`

**Goal**: Persisted errors and retrievable tool results are complete. Previews may be intentional tails.

### 2.1 Persist full spawn-health pane output
`kind: deliverable`

Targets:
- `src/gobby/mcp_proxy/tools/spawn_agent/_health.py::_bounded_redacted_pane_output`
- `src/gobby/mcp_proxy/tools/agents_payloads.py::_agent_result_payload`
- `tests/mcp_proxy/tools/spawn_agent/test_failure_cleanup.py`

After the pane dies, `run.error` is the only copy, and it is a 1024-char tail. The tail is a legitimate log view; making it the only copy is not.

Persist the full redacted pane through the existing agent-capture store (`retrieval_tool: "get_agent_capture"`). `error` may keep an intentional tail plus `capture_id`. Do not add a table.

**Acceptance:**

- 2.1.1 - Health-check failures persist the full redacted pane on the capture path. symbol: `_bounded_redacted_pane_output`.
- 2.1.2 - `get_agent_capture` returns every redacted character after fail. test: `tests/mcp_proxy/tools/spawn_agent/test_failure_cleanup.py`.

### 2.3 Stop clipping the tool-result store
`kind: deliverable`

Targets:
- `src/gobby/mcp_proxy/services/result_offload.py::ToolResultOffloader._maybe_offload_sync`
- `src/gobby/config/features.py::ToolResultOffloadConfig`
- `tests/mcp_proxy/services/test_result_offload.py`

`stored_content = serialized.text[: self._config.max_stored_chars]` stores a prefix and then offers retrieval.

1. Persist the full serialized text via the existing chunker. On success, `stored_chars == total_chars`.
2. `max_stored_chars` is a typed hard cap: if `total_chars > max_stored_chars`, do **not** persist a prefix. Envelope is `stored: false`, `reason: "too_large"`, `total_chars`, no pretending `result_id`.
3. Keep fail-open envelopes when persist throws; they must say the tail is not retrievable.

**Acceptance:**

- 2.3.1 - Successful offloads persist the entire result. symbol: `ToolResultOffloader._maybe_offload_sync`.
- 2.3.2 - Over-cap results fail typed instead of storing a prefix. symbol: `ToolResultOffloadConfig`.
- 2.3.3 - Tests cover full persist and typed oversize. test: `tests/mcp_proxy/services/test_result_offload.py`.

## P3: Hook overflow
`kind: framing`

**Goal**: Provider ship limits are met by dropping whole contributors, not chopping bodies.

### 3.3 Do not prefix-slice additionalContext
`kind: deliverable` (depends: 2.3)

Targets:
- `src/gobby/llm/sdk_utils.py::truncate_additional_context`
- `src/gobby/llm/sdk_utils.py::_truncate_contributors`
- `src/gobby/adapters/degradation.py::truncate_context_for_adapter`
- `src/gobby/hooks/event_handlers/_session_start/handoff.py::_bound_handoff_summary`
- `tests/llm/test_sdk_utils.py`
- `tests/hooks/test_context_limits.py`

The ship limit is real. Assembling 15K and keeping the first 10K is still this bug. Memory already overflows to `get_recall_memories`. Handoff already has `get_handoff_context`.

For `truncate_additional_context` / `_truncate_contributors`:

1. Persist the original aggregate when session/project exist (`ToolResultStore.save`).
2. Fit the ship limit by dropping **whole contributors**, largest first. Never `part[:budget]` on a contributor body.
3. Ship the intact remaining contributors plus a breadcrumb: `omitted contributors=[...]; get_tool_result result_id=<id>`.
4. If even one contributor cannot fit whole, ship breadcrumb only.

`_bound_handoff_summary` already breadcrumbs `get_handoff_context` but still prefix-slices the summary. If it does not fit whole, ship only the breadcrumb.

`truncate_context_for_adapter` keeps `CONTEXT_TRUNCATED` telemetry and must not reintroduce a prefix.

**Acceptance:**

- 3.3.1 - `additionalContext` never contains a prefix of a contributor body. symbol: `_truncate_contributors`.
- 3.3.2 - Overflow persists and the breadcrumb names `get_tool_result` when session/project exist. symbol: `truncate_additional_context`.
- 3.3.3 - Oversized handoff summaries ship a pointer, not a chopped head. symbol: `_bound_handoff_summary`.
- 3.3.4 - Tests cover whole-contributor drop, persist, and breadcrumb-only when nothing fits. test: `tests/llm/test_sdk_utils.py`.

## P4: Wiki ingest
`kind: framing`

**Goal**: Session-ingest prompts omit whole items instead of `chars().take` mid-string.

### 4.2 gwiki session-ingest inputs omit whole items
`kind: deliverable`

Targets:
- `crates/gwiki/src/ingest/session/summarize.rs::truncate_chars`
- `crates/gwiki/src/ingest/session/connections.rs`
- `crates/gwiki/src/commands/code/relationship_facts.rs::bound_relations`

Keep the numeric budgets as how many whole items fit. Include whole messages until the char budget would be exceeded; then omit remaining whole messages with `omitted N messages`. Extraction body is the whole summary or a pointer, never `take`. `bound_relations` includes `omitted N` per direction.

Leave `bound_seed_prompt` / `bound_one_shot_prompt` — they already refuse to inline an oversized seed and tell the model to use tools.

Validate: `cargo test -p gobby-wiki`, `cargo clippy -p gobby-wiki`. Reinstall `gwiki`.

**Acceptance:**

- 4.2.1 - Session summarize prompts do not mid-message `chars().take`. symbol: `truncate_chars`.
- 4.2.2 - Entity extraction does not prefix-slice the summary body. file: `crates/gwiki/src/ingest/session/connections.rs`.
- 4.2.3 - Relation facts report omitted whole relations. symbol: `bound_relations`.

## P5: Telegram captions
`kind: framing`

**Goal**: Media captions send the complete converted text.

### 5.1 Send remaining Telegram caption chunks
`kind: deliverable`

Targets:
- `src/gobby/communications/adapters/telegram.py::TelegramAdapter.send_attachment`
- `src/gobby/communications/adapters/telegram_formatting.py::markdown_to_telegram_html_chunks`

`send_attachment` uses `markdown_to_telegram_html_chunks(...)[0]` and drops the rest. After a successful media send, send each remaining chunk as a follow-up text message on the existing outbound path. If a follow-up fails, report a partial caption; do not pretend the full caption landed.

ACP live tails stay out of this plan.

**Acceptance:**

- 5.1.1 - Attachment captions no longer keep only `chunks[0]`. symbol: `TelegramAdapter.send_attachment`.
- 5.1.2 - Remaining chunks are sent as follow-up messages. file: `src/gobby/communications/adapters/telegram.py`.

## V1 Verification
`kind: verification`

1. gcore: a 20 KiB tool result is fully on disk; the model message is a pointer, not bytes `0..16384` of the result.
2. Spawn-health pane > 1024 chars: `get_agent_capture` returns the full redacted pane.
3. MCP result 20 KiB: stored whole; over `max_stored_chars` is typed `too_large` with no clipped `result_id`.
4. Hook context over the ship limit: shipped payload contains only whole contributors plus a pointer.
5. Telegram image with a 2 KiB caption: photo plus follow-up text covering the rest.

Closed leaves stay closed: Qwen parse, session messages, chat history, dispatch failure_context, digest/expansion/validation/pipeline inputs, cron/gcode failure output.

## V1 Plan Changelog
`kind: framing`

Trimmed to remaining build work after #20396–#20399, #20401–#20403. Section IDs 1.2, 2.1, 2.3, 3.3, 4.2, and 5.1 are unchanged so coverage labels stay aligned with epic #20395.
