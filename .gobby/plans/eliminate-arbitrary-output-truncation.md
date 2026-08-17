Plan artifact: `.gobby/plans/eliminate-arbitrary-output-truncation.md`

# Eliminate Arbitrary Output Truncation

## Overview
`kind: framing`

Gobby still takes complete existing text (a tool result, a transcript field, a pane dump, a caption) and returns a prefix, dropping the rest. That is the bug. Token budgets, result-set limits, generation caps, pagination, offload-to-a-pointer, and an intentional log tail are not the bug.

This plan removes every remaining **arbitrary prefix-slice of an existing complete output**. When a payload is too large to inline, the code either returns it whole, offloads it and returns a pointer, omits a **whole** item that does not fit, or — only for logs — returns an intentional tail. It never keeps the first 10K of a 15K string and throws away the last 5K.

## Constraints
`kind: framing`

**Forbidden:** `existing_text[:N]` (or UTF-8-safe equivalent) on a complete payload that the caller is supposed to receive, including “marked” prefixes such as `text[:10000] + "\n... [truncated]"`.

**Allowed, do not touch:**

| Kind | Why it is not this bug |
| --- | --- |
| Token / hit budgets (`gcode --token-budget`, graph/grep `limit` + `truncated`) | Cardinality of a result *set*, not chopping one document |
| Generation caps | “Write a summary under 10K” bounds *new* text, not an existing output |
| Intentional log tails | Last N lines of a pane, last 20 lines of a capture excerpt, ACP live tail |
| Pagination (`limit`/`offset`, `get_tool_result` slices) | The rest is one call away |
| Offload envelopes | Inline pointer; full bytes stay in `ToolResultStore` |
| Progressive-discovery briefs (`safe_truncate` / `truncate_tool_brief`) | Catalog card; full description is `get_tool_schema` |
| Typed resource fails (`run_command` `output_limit`) | No partial success |
| Derived storage bounds (`summary_safety` 500-char *generated* summary, `clip_link_field` btree key) | Not forwarding an existing payload |
| UI / CLI ellipsis, progress-bar `truncate_left` | Display chrome |

**Conversion rule (only these four):**

1. **Return the full text.**
2. **Offload and point** — persist the full bytes; the inline value is an envelope / breadcrumb / path, not a chopped body.
3. **Omit a whole item** that does not fit (a whole memory, a whole message, a whole contributor). Never half-cut one item.
4. **Intentional tail** — last N lines of a log, labeled as a tail, and only when the product is “show the end of the stream.” If that tail would become the only durable copy, persist the full stream first.

Reuse existing stores and tools: `ToolResultOffloader` / `gobby-results`, `get_handoff_context`, `get_agent_capture`, `get_recall_memories`, `get_session_messages`. No new MCP server, no new overflow table, no repo-wide grep linter.

No backward compatibility. Every touched production `.py` / `.rs` file stays under 1,000 lines. Rust: load `rust`, `cargo test -p <crate>`, reinstall `~/.gobby/bin/{gcode,gdaemon,ghook,gwiki}`. Python: `GOBBY_TEST_PROTECT=1` and scoped pytest only.

Canonical contract: `docs/contracts/truncation.md`.

## P1: Contract and model-facing payload chops
`kind: framing`

**Goal**: Write the rule once, then stop the two places that feed a model a prefix of a complete tool or transcript payload.

### 1.1 Write the truncation contract
`kind: deliverable`

Targets:
- `docs/contracts/truncation.md`

The contract is the table in Constraints plus this test:

```text
If you already hold a complete string S, and a caller is meant to receive S,
you may not return S[:N] for N < len(S).

You may return S, a pointer to S, a whole-item omission list, or (logs only)
an intentional tail of S.
```

Cite #18364 as the first wave (silent only-copy destruction) and this plan as the residual “do not prefix-slice existing outputs” sweep. List the allowed kinds so later leaves do not “fix” token budgets or summarizer output caps.

**Acceptance:**

- 1.1.1 - Contract states the S[:N] ban, the four legal conversions, and the allowed-kinds table. file: `docs/contracts/truncation.md`.
- 1.1.2 - Contract distinguishes generation caps from forwarding chops. behavior: "generation caps bound new text" in `docs/contracts/truncation.md`.

### 1.2 Stop prefix-slicing gcore tool-loop results
`kind: deliverable` (depends: 1.1)

Targets:
- `crates/gcore/src/ai/generation/tool_loop.rs::truncate_utf8`
- `crates/gcore/src/ai/generation/tool_loop.rs::ToolLoopLimits`
- `crates/gcore/src/ai/generation/tests/tool_loop.rs`

Today:

```rust
let result = truncate_utf8(result, limits.max_bytes_per_tool_result);
messages.push(ChatMessage::tool_result(call.id.clone(), result));
```

That is exactly “you have 20K, keep 16K, drop 4K.” Delete that ingest chop.

Replacement: if `result.len() <= max_bytes_per_tool_result`, push `result` unchanged. If larger, write the **full** result to a sidecar when `artifact_dir` is available and push a **pointer-only** tool message (path, byte count, “read the sidecar / re-query”). If there is no artifact directory, still do **not** prefix-slice: push a pointer-only message that says the result was N bytes and the tool must be re-invoked with a narrower query.

`max_bytes_per_tool_result` remains the inline budget (what may be copied into the chat message), not a license to keep a prefix. `truncate_utf8` stays only as a char-boundary primitive if something else still needs it; it must have no unmarked caller at the tool-result ingest site.

Add `artifact_dir: Option<PathBuf>` on the loop run context. gwiki/gdaemon pass their run directory.

Tests:

- under-cap result is the full string, no sidecar
- over-cap with `artifact_dir` writes bytes equal to the original result; the chat message contains the path and does **not** contain a prefix of the result body
- over-cap without `artifact_dir` is pointer/re-query text only — still no prefix of the body

Validate: `cargo test -p gobby-core tool_loop`, `cargo clippy -p gobby-core`. Reinstall binaries that embed the loop.

**Acceptance:**

- 1.2.1 - Oversized tool results are never prefix-sliced into the model message. symbol: `ToolLoopLimits`.
- 1.2.2 - Over-cap results persist in full to a sidecar when an artifact directory exists. file: `crates/gcore/src/ai/generation/tool_loop.rs`.
- 1.2.3 - Tests prove the model message has no prefix of the oversized body. test: `crates/gcore/src/ai/generation/tests/tool_loop.rs`.

### 1.3 Stop silent Qwen tool-result content clipping
`kind: deliverable` (depends: 1.1)

Targets:
- `src/gobby/sessions/transcripts/qwen.py::_result_content`
- `tests/sessions/transcripts/test_qwen.py`

`_result_content` returns `value[:500]` / `json.dumps(...)[:500]`. The parsed `content` used by MCP, search, digest, and the UI is a prefix of the JSONL record.

Return the full string or the full JSON dump. No 500-char cap. Large MCP windows are offloaded later; that is not this function’s job.

**Acceptance:**

- 1.3.1 - `_result_content` returns the complete payload. symbol: `_result_content`.
- 1.3.2 - A tool result longer than 500 characters is present in parsed `content` in full. test: `tests/sessions/transcripts/test_qwen.py`.

## P2: Only-copy durable sinks
`kind: framing`

**Goal**: When Gobby persists an error or a retrievable result, the stored bytes are the full payload. Previews may be intentional tails.

### 2.1 Persist full spawn-health pane output
`kind: deliverable` (depends: 1.1)

Targets:
- `src/gobby/mcp_proxy/tools/spawn_agent/_health.py::_bounded_redacted_pane_output`
- `src/gobby/mcp_proxy/tools/agents_payloads.py::_agent_result_payload`
- `tests/mcp_proxy/tools/spawn_agent/test_failure_cleanup.py`

After the pane dies, `run.error` is the only copy, and it is a 1024-char tail. The tail is a legitimate log view; making it the only copy is not.

Persist the full redacted pane through the existing agent-capture store (`retrieval_tool: "get_agent_capture"`). `error` may keep an intentional tail plus `capture_id`. Do not add a table.

**Acceptance:**

- 2.1.1 - Health-check failures persist the full redacted pane on the capture path. symbol: `_bounded_redacted_pane_output`.
- 2.1.2 - `get_agent_capture` returns every redacted character after fail. test: `tests/mcp_proxy/tools/spawn_agent/test_failure_cleanup.py`.

### 2.2 Persist full cron-shell and gcode-preflight failure output
`kind: deliverable` (depends: 1.1)

Targets:
- `src/gobby/scheduler/executor.py::CronExecutor._execute_shell`
- `src/gobby/agents/code_index.py::_process_detail`
- `src/gobby/agents/code_index.py::_run_gcode`

```python
raise RuntimeError(f"Command exited with code {process.returncode}: {output[:2000]}")
# and
return detail[:500] or "<empty output>"
```

On cron shell failure, write the **full** decoded stdout to `CronRun.output`. `error` may be an intentional tail plus “full output on the cron run (N chars).” Success already returns full output — leave that.

On gcode preflight failure, keep URL redaction. Put the full redacted detail in `IndexInventoryError.details["output"]` (or equivalent). The raised message may be an intentional tail plus omitted count. `detail[:500]` must not be the only string that exists.

**Acceptance:**

- 2.2.1 - Cron shell failures store complete command output on the run. symbol: `CronExecutor._execute_shell`.
- 2.2.2 - gcode preflight failures retain full redacted detail next to any tail preview. symbol: `_process_detail`.

### 2.3 Stop clipping the tool-result store
`kind: deliverable` (depends: 1.1)

Targets:
- `src/gobby/mcp_proxy/services/result_offload.py::ToolResultOffloader._maybe_offload_sync`
- `src/gobby/config/features.py::ToolResultOffloadConfig`
- `tests/mcp_proxy/services/test_result_offload.py`

`stored_content = serialized.text[: self._config.max_stored_chars]` stores a prefix and then offers retrieval.

Rules:

1. Persist the full serialized text via the existing chunker. On success, `stored_chars == total_chars`.
2. `max_stored_chars` is a typed hard cap: if `total_chars > max_stored_chars`, do **not** persist a prefix. Envelope is `stored: false`, `reason: "too_large"`, `total_chars`, no pretending `result_id`.
3. Keep fail-open envelopes when persist throws; they must say the tail is not retrievable.

**Acceptance:**

- 2.3.1 - Successful offloads persist the entire result. symbol: `ToolResultOffloader._maybe_offload_sync`.
- 2.3.2 - Over-cap results fail typed instead of storing a prefix. symbol: `ToolResultOffloadConfig`.
- 2.3.3 - Tests cover full persist and typed oversize. test: `tests/mcp_proxy/services/test_result_offload.py`.

## P3: Agent-facing views and hook overflow
`kind: framing`

**Goal**: MCP and hook surfaces stop rewriting complete fields into prefixes. Whole-item omission and offload stay.

### 3.1 Remove MCP session-message field slicing
`kind: deliverable` (depends: 1.1, 1.3)

Targets:
- `src/gobby/mcp_proxy/tools/sessions/_messages.py::get_session_messages`
- `src/gobby/mcp_proxy/tools/sessions/_messages.py::search_session_messages`
- `src/gobby/mcp_proxy/tools/sessions/_messages.py::_truncate_session_message`
- `src/gobby/sessions/transcript_search.py::_truncate_message`
- `tests/mcp_proxy/tools/test_session_messages_coverage.py`
- `tests/mcp_proxy/test_mcp_tools_session_messages.py`

Default `full_content=False` rewrites `content` / tool inputs / results / blocks to 500/200. That is a prefix of a complete message.

Delete `_truncate_session_message` and the mutation path. Default `full_content=True`. Keep `limit`/`offset` windowing. Large windows go through `ToolResultOffloader`.

Search may keep a separate `snippet` field. It must not overwrite `content` / `content_blocks`. Delete `_truncate_message` or confine it to building `snippet`.

No compat alias. Update tests that expect `"... (truncated)"` on default calls.

**Acceptance:**

- 3.1.1 - Default `get_session_messages` returns complete rendered fields. symbol: `get_session_messages`.
- 3.1.2 - `_truncate_session_message` is removed. symbol: `_truncate_session_message`.
- 3.1.3 - Search does not mutate message content into a prefix. symbol: `_truncate_message`.
- 3.1.4 - Coverage tests no longer require default field slicing. test: `tests/mcp_proxy/tools/test_session_messages_coverage.py`.

### 3.2 Omit whole chat-history messages, never slice bodies
`kind: deliverable` (depends: 3.1)

Targets:
- `src/gobby/servers/chat_session_messages.py::ChatSessionMessagesMixin._load_history_context`
- `tests/servers/test_chat_session.py::TestHistoryInjection.test_load_history_context_truncates_long_messages`

Today a long message becomes `content[:max_msg_chars] + "..."`, and leftover messages are dropped with no count.

Stop slicing bodies. Include whole messages until the total budget is exhausted. If the next whole message does not fit, stop and append:

```text
[omitted N messages to fit history budget; get_session_messages session_id=<id>]
```

If a *single* message is larger than the entire budget, omit that body entirely (do not prefix-slice it) and point at `get_session_messages`. Tiny budgets still return `None` when not even the omission line fits.

**Acceptance:**

- 3.2.1 - History injection never prefix-slices a message body. symbol: `ChatSessionMessagesMixin._load_history_context`.
- 3.2.2 - Omitted messages are whole-item omissions with a retrieval pointer. test: `tests/servers/test_chat_session.py::TestHistoryInjection.test_load_history_context_truncates_long_messages`.

### 3.3 Do not prefix-slice additionalContext
`kind: deliverable` (depends: 1.1, 2.3)

Targets:
- `src/gobby/llm/sdk_utils.py::truncate_additional_context`
- `src/gobby/llm/sdk_utils.py::_truncate_contributors`
- `src/gobby/adapters/degradation.py::truncate_context_for_adapter`
- `src/gobby/hooks/event_handlers/_session_start/handoff.py::_bound_handoff_summary`
- `tests/llm/test_sdk_utils.py`
- `tests/hooks/test_context_limits.py`

The ship limit is real (providers hard-cut). Assembling 15K and keeping the first 10K is still this bug. Memory already overflows to `get_recall_memories`. Handoff already has `get_handoff_context`.

New behavior for `truncate_additional_context` / `_truncate_contributors`:

1. Persist the original aggregate when session/project exist (`ToolResultStore.save`).
2. Fit the ship limit by dropping **whole contributors** from the end of the priority list (largest first). Never `part[:budget]` on a contributor body.
3. Ship the intact remaining contributors plus a breadcrumb: `omitted contributors=[...]; get_tool_result result_id=<id>`.
4. If even one contributor cannot fit whole, ship breadcrumb only.

`_bound_handoff_summary` already breadcrumbs `get_handoff_context` but still prefix-slices the summary via `head_with_breadcrumb` / `allocate_section_budget`. Stop shipping a chopped summary: if it does not fit whole, ship only the existing breadcrumb (the full summary stays on the session).

`truncate_context_for_adapter` keeps `CONTEXT_TRUNCATED` telemetry and must not reintroduce a prefix.

**Acceptance:**

- 3.3.1 - `additionalContext` never contains a prefix of a contributor body. symbol: `_truncate_contributors`.
- 3.3.2 - Overflow persists and the breadcrumb names `get_tool_result` when session/project exist. symbol: `truncate_additional_context`.
- 3.3.3 - Oversized handoff summaries ship a pointer, not a chopped head. symbol: `_bound_handoff_summary`.
- 3.3.4 - Tests cover whole-contributor drop, persist, and breadcrumb-only when nothing fits. test: `tests/llm/test_sdk_utils.py`.

### 3.4 Point dispatch prompts at stored failure context
`kind: deliverable` (depends: 1.1)

Targets:
- `src/gobby/dispatch/prompts.py::_bounded_failure_context`

`failure_context[:2000] + "\n[truncated]"` is a prefix of a complete stored string. If it fits whole, inline it. If not, inline a pointer to the task record / `get_task` only. No 2000-char head.

**Acceptance:**

- 3.4.1 - Oversized `failure_context` is not prefix-sliced into the spawn prompt. symbol: `_bounded_failure_context`.

## P4: Stop prefix-slicing prompt inputs
`kind: framing`

**Goal**: Internal LLM *inputs* that are existing files or transcript turns are forwarded whole, pointed at, or omitted as whole items. Synthesized *outputs* may stay capped.

### 4.1 Digest, expansion, validation, and pipeline inputs
`kind: deliverable` (depends: 1.1)

Targets:
- `src/gobby/memory/digest.py::_build_turn_record`
- `src/gobby/tasks/expansion/_common.py::_read_text_if_exists`
- `src/gobby/tasks/validation.py::_bound_text`
- `src/gobby/mcp_proxy/tools/workflows/_pipeline_query.py::_step_summary`

| Helper | Today | Required |
| --- | --- | --- |
| `_build_turn_record` | `p[:4000]`, `r[:8000]` | Do not slice turns. Pass whole pairs, or omit whole pairs with “see session transcript.” The *generated* `turn_markdown` may stay schema-bounded. |
| `_read_text_if_exists` | `text[:max_chars]` | Return the full file or return `None` and let the prompt name the path. Delete `max_chars` (callers in `_compile.py` that pass 3500/1500 stop passing it). |
| `_bound_text` | `value[:max] + "..."` | Do not chop. If `changes_summary` / checklist facts do not fit with the rest of the prompt, raise the existing `ValidationPromptTooLarge` (typed fail). |
| `_step_summary` | `step.error[:200]` | Omit the error body from the *brief* list (`error_present: true`, `error_chars: N`). Detail/get-one-step tools return the full `step.error`. |

**Acceptance:**

- 4.1.1 - Turn-record prompts do not prefix-slice transcript turns. symbol: `_build_turn_record`.
- 4.1.2 - Expansion file reads return complete file text or a path, never `text[:max_chars]`. symbol: `_read_text_if_exists`.
- 4.1.3 - Close-review no longer prefix-slices `changes_summary`; oversize fails typed. symbol: `_bound_text`.
- 4.1.4 - Pipeline briefs do not prefix-slice `step.error`. symbol: `_step_summary`.

### 4.2 gwiki session-ingest inputs omit whole items
`kind: deliverable` (depends: 1.1)

Targets:
- `crates/gwiki/src/ingest/session/summarize.rs::truncate_chars`
- `crates/gwiki/src/ingest/session/connections.rs`
- `crates/gwiki/src/commands/code/relationship_facts.rs::bound_relations`

`chars().take(PER_MESSAGE_CHAR_CAP)` / `EXTRACTION_BODY_BUDGET` prefix-slices existing message and summary bodies. `bound_relations` silently keeps 5 relations.

Keep the numeric budgets as *how many whole items fit*. Include whole messages until the char budget would be exceeded; then omit remaining whole messages with `omitted N messages`. Same for extraction body (whole summary or pointer, never `take`). `bound_relations` includes `omitted N` per direction.

`bound_seed_prompt` / `bound_one_shot_prompt` already refuse to inline an oversized seed and tell the model to use tools — that is conversion (2), leave them.

Validate: `cargo test -p gobby-wiki`, `cargo clippy -p gobby-wiki`. Reinstall `gwiki`.

**Acceptance:**

- 4.2.1 - Session summarize prompts do not mid-message `chars().take`. symbol: `truncate_chars`.
- 4.2.2 - Entity extraction does not prefix-slice the summary body. file: `crates/gwiki/src/ingest/session/connections.rs`.
- 4.2.3 - Relation facts report omitted whole relations. symbol: `bound_relations`.

## P5: Channel leftover
`kind: framing`

**Goal**: Telegram media captions send the complete converted text. ACP live tails stay: they are intentional log tails.

### 5.1 Send remaining Telegram caption chunks
`kind: deliverable` (depends: 1.1)

Targets:
- `src/gobby/communications/adapters/telegram.py::TelegramAdapter.send_attachment`
- `src/gobby/communications/adapters/telegram_formatting.py::markdown_to_telegram_html_chunks`

`send_attachment` uses `markdown_to_telegram_html_chunks(...)[0]` and drops the rest. After a successful media send, send each remaining chunk as a follow-up text message on the existing outbound path. If a follow-up fails, report a partial caption; do not pretend the full caption landed.

ACP `outputByteLimit` tails are allowed (intentional log tail). Do not change them in this epic.

**Acceptance:**

- 5.1.1 - Attachment captions no longer keep only `chunks[0]`. symbol: `TelegramAdapter.send_attachment`.
- 5.1.2 - Remaining chunks are sent as follow-up messages. file: `src/gobby/communications/adapters/telegram.py`.

## V1 Verification
`kind: verification`

1. gcore: a 20 KiB tool result is fully on disk; the model message is a pointer, not bytes `0..16384` of the result.
2. Qwen 2 KiB tool result: parsed `content` is 2 KiB; `get_session_messages` default returns it whole (offload envelope allowed).
3. Spawn-health pane > 1024 chars: `get_agent_capture` returns the full redacted pane.
4. Cron shell failure 5 KiB: `CronRun.output` is 5 KiB.
5. MCP result 20 KiB: stored whole; 3 MiB over `max_stored_chars` is typed `too_large` with no clipped `result_id`.
6. Hook context over the ship limit: shipped payload contains only whole contributors plus a pointer; no contributor body is prefix-sliced.
7. History inject: a 20K user message is omitted whole or retrieved via `get_session_messages`, never `msg[:N] + "..."`.
8. Telegram image with a 2 KiB caption: photo plus follow-up text covering the rest.
9. Token budgets, generation caps, ACP tails, tool briefs, and `run_command` typed overflow still behave as they do today.

## V1 Plan Changelog
`kind: framing`

User-approved policy: token budgets and generated-summary caps are in; arbitrary prefix-slices of existing complete outputs are out. Intentional log tails stay. “Mark the prefix and persist the rest” is not enough — the inline value must not be a chopped body.
