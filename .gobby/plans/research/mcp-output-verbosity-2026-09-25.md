# MCP tool output verbosity: token wasters

Date: 2026-09-25. Researcher gobby#14550, for Josh.

## Method

- **Source.** Every `mcp__gobby__*` tool result in the 58 Claude Code transcripts for this project modified in the last 7 days. Codex, Grok and other CLIs are not included.
- **Mapping.** Each result is joined to its `tool_use` block (server, tool, arguments). Lengths are in characters of the result text as the model received it.
- **Scripts.** Scratchpad `mcpsize.py` (per-tool size table), `sample.py` (median-sized sample), `keys.py` (per-key size breakdown).
- **Volume.** 17,710 calls; 38.0M chars. That is at least 9.5M tokens at 4 chars per token. JSON with UUIDs, quotes and braces usually tokenizes at 2.5–3 chars per token, so the real figure is likely 12–15M tokens per week (INFERRED). The 9.5M is a floor.
- **Headline.** Per-epoch ceremony is 54% of all MCP output: skill reloads plus the handoff cycle after each compaction. That is the structural waster. The per-tool fixes below are real but smaller.
- **Epochs.** A context epoch starts at a `compact_boundary`. There were 343 compaction epochs in 7 days.

## Where the chars go

| Cluster | Chars / 7 d | Share | Nature |
| --- | ---: | ---: | --- |
| Skill and reference loads (`get_skill`, `get_skill_file`) | 14.6M | 38% | 83% are the same load repeated in a later epoch after compaction |
| Handoff cycle (`handoffs.md`, the `set_handoff` schema, `get_handoff`) | 6.1M | 16% | Paid once per compaction |
| Everything else | 17.3M | 46% | Per-call payload |

Only 1.19M chars (3%) are identical calls repeated inside one context epoch. Most of that is agent polling, not tool design: 207 identical `list_running_agents{"status":"running"}` calls and 58 identical `wait_for_coordination` calls. So the waste lies either in per-epoch ceremony or in individual tools whose default payload is too large, not in agents repeating themselves.

## Findings, ranked by savings

All sizes below are VERIFIED from transcripts. The fixes are proposals.

1. **The per-compaction handoff ceremony costs about 12k chars before any work starts.**
   - The cost:
     - `references/sessions/handoffs.md` (6.3k chars, 357 loads, 2.26M total).
     - The `set_handoff` schema (5.8k chars, 349 loads, 1.97M total).
   - The schema's description is 3.8k of authoring prose: the 10,000-char limit, section semantics and found_work rules.
   - Its source is `src/gobby/install/shared/prompts/handoff/authoring.md`, loaded into the tool description at `src/gobby/mcp_proxy/tools/sessions/_terminal.py:693`.
   - It is a separate file from `handoffs.md`, but it restates the same rules: `handoffs.md:10` states the same 10,000-char limit and `:31` the same found_work rule (VERIFIED). AGENTS.md requires loading both before every handoff.
   - Fix: stop loading `handoff/authoring` into the description, and keep one line plus the hard limit. `handoffs.md` stays the single source. That saves about 3.5k × 349 = ~1.2M chars/week.
   - `get_handoff` also repeats `found_work` twice: once as Notes bullets in the markdown and once as the structured `found_work` array. Small, but it happens on every resume.

2. **Hooks force reference loads after every compaction.**
   - Hook-forced reloads per epoch:

     | Load | Size | Loads |
     | --- | ---: | ---: |
     | `brevity` | 3.4k | 324 |
     | `restraint` | 3.4k | 371 |
     | `code-index/overview.md` | 3.3k | 340 |
     | `memory/overview.md` | 2.8k | 321 |
     | `skills/loading.md` | 1.8k | 316 |
     | `code-review` | 10.0k | 177 |
     | `development/obligations.md` | 5.1k | 177 |
     | `tasks/closing.md` | 4.6k | 141 |

   - `skills/loading.md` exists to teach pagination, which the proxy's own server instructions already state.
   - This is a policy question for Josh, not a bug. Each reload is the teaching gate working as designed. The cost is about 6.6M chars/week for the listed items.
   - Options, if wanted:
     - Serve a compact "already known" digest to a session that loaded the file in an earlier epoch.
     - Drop the `skills/loading.md` gate.
     - Trim `code-review` (10k), the largest per-load skill in the list.

3. **`review_task_memories` returns five full memories: 8.7k average, 116 calls, 1.0M chars.**
   - `candidates[].content` averages 6.3k of the 8.7k.
   - The reviewer only needs to decide whether to update, supersede or skip. The id, similarity, the first line of content and the `when:` rationale are enough, followed by `get_memory` on the one or two it acts on.
   - Serialization is at `src/gobby/mcp_proxy/tools/memory_review.py:216-226`.
   - Estimated cut: ~75%.

4. **`create_memory` echoes five full similar memories: median 8.6k, 49 calls, 419k chars.**
   - `similar_existing` carries full `content` and `rationale` for 5 hits (`memory_write.py:284-292`). It often pushes the response past the 15k offload threshold, so a write turns into an offload envelope.
   - Fix: id, similarity, raw_semantic_score and a one-line summary. `auto_superseded` already covers the near-duplicate case.

5. **`search_memories` defaults to full content, and 32% of calls overflow into offload.**
   - Median 7.3k; 139 calls, 930k chars; 44 of 139 were offloaded.
   - An offload adds a round trip through `gobby-results:get_tool_result` / `search_tool_result` (157 calls, 1.03M chars, 7.5k pages).
   - Fix: a brief default of id, type, the first line and `when:`, with `brief=false` or `get_memory` for full text. That matches the `<memory-index>` format agents already use.

6. **`close_task` lists every gate, including passes: 415 calls, 944k chars, p90 3.6k, max 48.7k.**
   - Ten or more `{"item","name","status":"passed","message"}` gates precede the one that failed (`src/gobby/tasks/close_checklist.py:122`).
   - Fix: report non-passed gates in full plus a passed count. Keep the full list for `preview=true` or `brief=false`.
   - The 48.7k maximum comes from failure detail and needs a separate look, INFERRED to be the validation-command transcript evidence.

7. **`response_time_ms` is a full-precision float on every call.**
   - For example `"response_time_ms":1532.2366670006886`. It totals 481k chars/week, 1.3% of all output.
   - Round it to an integer, or drop it from agent-facing results; the metrics tables already record latency.
   - Set in `src/gobby/servers/routes/mcp/endpoints/execution.py:78,83,114,175,185` and the sibling endpoints (`discovery.py`, `server.py`).
   - A one-line fix at each site.

8. **The offload envelope sends the same data twice.**
   - Scalar values appear at top level and again inside `preview`, which is `indent=2` JSON. Key names appear a third time in `structure.keys`, a key-to-type map. See `result_offload.py:282-311`, text built at `:380`.
   - 162 envelopes, 578k chars.
   - Fix: drop the scalars already visible in `preview`, and build the preview from compact JSON. `indent=2` costs 4–12% by tool; small.

## Minor items (list for completeness, low value)

- `get_session`: 6.1k average over 41 calls, carrying `handoff_markdown`, `summary_markdown` and `preview` together.
- `wait_for_agent`: about 340 of its 1.4k average is a `sandbox` block (policy hash, retained paths) and `live_output` retrieval metadata, repeated on every wait (194 calls).
- Schemas loaded per epoch:
  - `create_task` / `update_task`: about 3k each; the `category` description alone is 561 chars, duplicated in both.
  - `send_message`: 2.4k description.
  - `feedback`: 2.0k.
- `list_mcp_servers`: 7.9k per call, but only 16 calls. `list_tools chrome-devtools`: 19k per call, only 2.
- Envelope overhead: JSON escaping of markdown adds 1.2%, and `\uXXXX` escapes add ~0%.

## Already lean (no action)

- `send_message`: median 235.
- `create_task` result: 369.
- `update_task`: 66.
- `claim_task`: 243.
- `set_handoff` result: 309.
- `communications:send_message`: 129.
- `get_task`: its size is the description content itself; the default brief view has no dead weight beyond `id`, `seq_num` and `path_cache` sitting next to `ref`.

## Recommended order

The biggest lever is per-epoch ceremony: item 2 (Josh's policy call) and item 1 (a contained edit, ~1.2M chars/week).

Item 6 is a contained edit with moderate savings. Item 7 is trivial to fix but small (1.3%).

Items 3–5 are one memory-output change and should be decided together: a brief default shape for memory hits across `review_task_memories`, `create_memory` and `search_memories`.

## Decision: brief contract (Josh, 2026-09-25)

Every agent-facing tool that returns more than a handle and an outcome takes a `brief` parameter, and `brief=true` is the default. Agents pass `brief=false` only for debugging, and each brief parameter's schema description says so. Enforcement is that schema wording only: no lint test, no rule.

**Essential (brief keeps it):**
1. The handle for the next call. A ref only when one exists; a UUID only for entities without a ref (memories, agent runs).
2. The outcome: `success`, `status`, and for errors the code, message and required action.
3. The payload the tool exists to return, bounded.

**Non-essential (brief drops it):**
- Null, empty and default-valued fields.
- Duplicate identity: an `id` or UUID next to a `ref`, `seq_num`, `path_cache`, the caller's own `project_id`, `machine_id`, `external_id`.
- Audit and provenance fields: `created_by_agent`, `access_count`, `source_type`, `policy_hash`, fingerprints, reviewer/validator run ids, `transcript_path`, retained sandbox paths.
- Timestamps that don't affect the next step. Staleness signals such as `progress_age_seconds` stay.
- Sub-checks that passed; a count replaces them.
- Echoes of the caller's input.
- Diagnostics: `response_time_ms`, `score_range`, `threshold_axis`, `recall_request_id`.
- Retrieval instructions the agent already knows, keeping one pointer only when content was truncated.
- The same data sent twice: `close_task` `gates`, `checklist` and `result.gates`; `get_handoff` `found_work`.
- Full bodies of secondary items: id plus a first-line summary.

**Memory hits use the index-line shape** (id, type, first line, `when:`) in `search_memories`, `review_task_memories` and `create_memory.similar_existing`. Full text comes from `get_memory`.

**Per-tool application:**

| Tool | Brief keeps | Brief drops |
| --- | --- | --- |
| `search_memories` | index lines | full content, `diagnostics`, `recall_request_id`, `project_id` |
| `create_memory` | `memory.id`, `auto_superseded`, `similar_existing[]` index lines with similarity | full similar content and rationale, `project_id`, `created_by_agent`, `is_global` |
| `review_task_memories` | `task_ref`, `candidates[]` index lines with similarity | full `content`, `tags`, `task_id`/`source_task_id` |
| `get_memory` | `id`, `type`, `content`, `rationale`, `tags` | `access_count`, `source_type`, `created_by_agent`, `project_id`, `is_global`, timestamps |
| `close_task` | `closed`, `can_close`, non-passed gates in full, passed count, `required_actions`, `blocking_reasons` | passed gates, duplicate `checklist`/`gates`, fingerprints, reviewer/validator run ids |
| `wait_for_agent` / `get_agent_result` | `status`, `completed`, `result`, `error`, `terminal_reason`, `progress_age_seconds`, `stall_suspected`, `blocked_on_parent` | `sandbox` block, `live_output` metadata, `notification_*`, `capture`, start/result timestamps |
| `get_session` | `ref`, `status`, `title`, `model`, task refs, `last_activity` | `handoff_markdown`, `summary_markdown`, `terminal_context`, `transcript_path`, `external_id`, `machine_id`, `startup_claim_*`, summary hashes |
| `get_handoff` | handoff markdown, structured `found_work` | the `found_work` bullets repeated in the markdown |
| every call (proxy envelope) | — | `response_time_ms` |
| `list_rules`, `workflows/__init__.py:254` | — | flip the default from `brief=False` to `True` |

`get_task`, `send_message`, `get_skill` and the pipeline queries already default to `brief=True` and are the reference pattern. For tasks, see `tasks/_formatters.py`. Their existing brief views should be checked against the rules above. For example, brief `get_task` still returns `id`, `seq_num` and `path_cache` next to `ref`. The `get_skill` note "brief=false for management work" should be reworded to "debugging only" or kept as a stated exception; that is for the planner to settle.
