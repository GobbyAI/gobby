# Brief-by-default MCP responses

**Plan ID:** mcp-brief-response-contract

Plan artifact: `.gobby/plans/mcp-brief-response-contract.md`

Planning task: #22909 Plan brief-by-default agent MCP responses to reduce compaction load.
Author: Researcher gobby#14550. Decisions confirmed by Josh in the #14550 terminal on 2026-09-25.
Evidence: `.gobby/plans/research/mcp-output-verbosity-2026-09-25.md` (measurement method, per-tool
sizes, and the "Decision: brief contract" section).

## Overview
`kind: framing`

Over seven days, 58 Claude Code transcripts in this project made 17,710 Gobby MCP calls. The
calls returned 38.0M characters, at least 9.5M tokens and likely 12–15M. Several agent-facing
tools return full records when the calling agent needs only a handle, an outcome, and the payload
it asked for:

- `review_task_memories` averages 8.7k chars per call, of which 6.3k is the full text of five
  candidate memories.
- `create_memory` echoes five full similar memories (median 8.6k) and often overflows into the
  offload envelope.
- `search_memories` returns full memory bodies with 20 fields per hit; 32% of calls overflow into
  offload.
- `close_task` lists every passed gate beside the one that failed.
- `wait_for_agent` repeats sandbox and live-output metadata on every wait.
- Every proxy result carries a full-precision float `response_time_ms` (1.3% of all output).

This plan makes `brief=true` the default for every in-scope tool. Brief keeps what the caller
needs for its next action and drops everything else. `brief=false` returns today's full shape and
is documented as debugging only.

## Decision Record
`kind: framing`

Josh confirmed these on 2026-09-25:

1. **Scope.** Every agent-facing MCP tool that returns more than a handle and an outcome accepts
   `brief: bool = True`. The in-scope tools are exactly the ones this plan's deliverables name.
   Tools that already default to `brief=True` (`get_task`, `send_message`, `get_skill`,
   `get_skill_file`, `list_pipeline_executions`) are audited against the same rules: their
   wording is aligned, and `get_task` drops its duplicate identity fields.
2. **Full mode is for debugging.** `brief=false` returns the current full shape. The one
   exception is `get_skill` / `get_skill_file`, where `brief=false` is also allowed for skill
   management (ids, `content_hash`, version), because skill edits need the hash.
3. **Enforcement is wording only.** Each tool's description (and `get_task`'s explicit parameter
   description) says `brief=false` is for debugging only. No lint test, rule, or hook enforces
   it. Decorator-registered tools get no per-parameter description from
   `InternalToolRegistry.tool` (`src/gobby/mcp_proxy/tools/internal.py`), so the sentence lives
   in the tool description: "Pass brief=false only for debugging; it returns the full record."
4. **Essential fields (brief keeps them):**
   - the handle for the next call;
   - the outcome: `success`, `status`, and for errors the code, message and required action;
   - the payload the tool exists to return, bounded.
5. **Non-essential fields (brief drops them):**
   - null, empty and default-valued fields;
   - duplicate identity next to a ref (UUID `id`, `seq_num`, `path_cache`, the caller's own
     `project_id`, `machine_id`, `external_id`);
   - audit and provenance fields (`created_by_agent`, `access_count`, `source_type`,
     `policy_hash`, fingerprints, reviewer provider/model, `transcript_path`, retained sandbox
     paths);
   - timestamps that do not affect the next step (staleness signals such as
     `progress_age_seconds` stay);
   - sub-checks that passed (a count replaces them);
   - echoes of the caller's input;
   - diagnostics (`score_range`, `threshold_axis`, ranking internals);
   - retrieval instructions the agent already knows (one pointer stays when content was cut);
   - the same data sent twice;
   - full bodies of secondary items (an index line instead).
   A field that an agent workflow, rule, or reference instructs agents to act on stays, even when
   it matches a category above. Each deliverable names those fields.
6. **Identity.** Brief output uses a ref when the entity has one. A UUID stays only for entities
   without a ref (memories, agent runs, messages), or when a documented next call takes that UUID
   as an argument; the deliverable names each such case.
7. **Memory index line.** Memory hits in `search_memories`, `review_task_memories`, and
   `create_memory.similar_existing` use `{id, type, summary, when}`. `summary` is
   `lead(content)` and `when` is `lead(rationale)`. `lead` is the whitespace-collapsed,
   word-bounded 160-character projection that the `<memory-index>` block already uses
   (`src/gobby/memory/surface_format.py::_lead`, made public). Duplicate-checking results
   (`create_memory`, `review_task_memories`) also keep `similarity`. Full text comes from
   `get_memory`.
8. **`response_time_ms` is rounded to an integer** on every proxy result. Josh chose this over
   dropping it: the web chat UI recognizes a proxy envelope by a numeric `response_time_ms`
   (`web/src/components/chat/ToolCallCard.helpers.ts`, `getMcpEnvelopeCandidate`), and an
   integer keeps that check true.
9. **`close_task`: `brief` replaces `response_detail`.** `brief=true` is today's `"concise"`
   plus the slim field set; `brief=false` is today's `"diagnostic"`. The persisted close-argument
   key is renamed from `response_detail` to `brief`. No backward compatibility is kept (0.5.0 has
   not shipped).
10. **Teaching-gate reloads are out of scope.** The per-compaction skill and reference reloads
    (38% of measured output) are a separate policy question for Josh and are not changed here.
    The offload envelope and the `set_handoff` schema description are also out of scope: neither
    is a brief view of a tool. The research note keeps them as separate findings.

## Constraints
`kind: framing`

- No production code changes in planning task #22909.
- Internal callers keep their behavior. Brief applies at the MCP tool boundary: the wrapper
  function the registry exposes. Shared builders used by internal paths keep returning the full
  shape; see `CloseEvaluation.response` in 1.6 and `_agent_result_payload` in 1.8.
- Every agent workflow YAML, rule, and reference that reads a field is either kept working (the
  field stays in brief) or updated in the same deliverable.
- Production files at 850 lines or more that must change are split in the same deliverable. That
  includes `src/gobby/mcp_proxy/tools/agents_query_tools.py`, which is already over the
  1,000-line ceiling at 1,004 lines (found work, fixed in 1.8).
- The PD routes implementation leaves to a free developer lane without diverting Lane 1 (Chrome)
  or daemon stability work. All leaves are independent except where a heading says `depends`.

## P1: Proxy envelope
`kind: framing`

### 1.1 Integer response_time_ms on proxy results [category: code]
`kind: deliverable`

Targets:
- `src/gobby/servers/routes/mcp/endpoints/execution.py::*` — scope-reason: every `response_time_ms` site in the call, list, schema, and proxy handlers switches to the shared helper, and the response-payload helpers move out
- `src/gobby/servers/routes/mcp/endpoints/_responses.py`
- `src/gobby/servers/routes/mcp/endpoints/discovery.py::*` — scope-reason: `list_all_mcp_tools`, `recommend_mcp_tools`, and `search_mcp_tools` set the field directly
- `src/gobby/servers/routes/mcp/endpoints/server.py::*` — scope-reason: the add, update, import, remove, and enable handlers set the field directly
- `src/gobby/servers/routes/mcp/endpoints/registry.py::*` — scope-reason: `embed_mcp_tools` and the status and refresh handlers set the field directly
- `tests/servers/routes/mcp_endpoints/test_response_timing.py`

Consumers unchanged:
- `web/src/components/chat/ToolCallCard.helpers.ts` — no-edit-reason: it checks `typeof response_time_ms === "number"`, which an integer satisfies.
- `src/gobby/mcp_proxy/stdio_proxy.py` — no-edit-reason: `DaemonProxy._request` returns the route JSON unchanged.
- `src/gobby/cli/extensions.py` — no-edit-reason: reads a webhook-test route's own field, not the MCP proxy.
- `src/gobby/mcp_proxy/services/tool_execution.py` — no-edit-reason: records its own `latency_ms` into metrics.

**Research context:**
- Agents reach tools through stdio: `mcp_proxy/stdio_tools.py::register_proxy_tools.call_tool`,
  then `mcp_proxy/stdio_proxy.py::DaemonProxy.call_tool`, which POSTs to
  `/api/mcp/{server}/tools/{tool}` or `/api/mcp/tools/call`. The agent-facing dict is built in
  the daemon HTTP layer, so rounding at the producer covers agents and the web UI together.
- Every site computes `(time.perf_counter() - start_time) * 1000` as a float:
  - `execution.py`: `_success_response_payload`, `_timeout_response_payload`,
    `_process_tool_proxy_result`, `_call_internal_tool`, `list_mcp_tools`, `get_tool_schema`,
    `call_mcp_tool`, `mcp_proxy`;
  - plus `discovery.py`, `server.py`, and `registry.py` handlers.
- Same-named fields in `routes/admin/*`, `routes/mcp/webhooks.py`, `routes/sessions/*`,
  `routes/mcp/hooks.py`, `mcp_proxy/models.py`, and `client_manager/health.py` are separate HTTP
  responses or server-health latency. They are not proxy results and stay unchanged.
- No test asserts a float. `tests/servers/test_mcp_routes.py` and
  `tests/servers/routes/mcp_endpoints/test_discovery_routes.py` check presence only.
- `execution.py` is 929 lines. The split: move `_json_safe_payload`,
  `_success_response_payload`, `_timeout_response_payload`,
  `_incompatible_stdio_wrapper_wait_result`, and `_process_tool_proxy_result` (lines 66–190) from
  `execution.py` into the new `src/gobby/servers/routes/mcp/endpoints/_responses.py`, together
  with the new `elapsed_ms(start_time: float) -> int` helper. `execution.py` imports them back.

**Implementation:**
- Add `elapsed_ms(start_time) -> int` returning `round((time.perf_counter() - start_time) * 1000)`.
- Replace every float site in the four endpoint files with `elapsed_ms(start_time)`.

**Acceptance:**
- 1.1.1 - `src/gobby/servers/routes/mcp/endpoints/_responses.py` defines `elapsed_ms` and holds the five moved response helpers; `execution.py` is below 850 lines. file: `src/gobby/servers/routes/mcp/endpoints/_responses.py`.
- 1.1.2 - No `response_time_ms` in `execution.py`, `discovery.py`, `server.py`, or `registry.py` is assigned from anything but `elapsed_ms`. file: `src/gobby/servers/routes/mcp/endpoints/execution.py`.
- 1.1.3 - test: `tests/servers/routes/mcp_endpoints/test_response_timing.py::test_proxy_results_report_integer_response_time_ms` drives every envelope through the route test client: `call_mcp_tool` (`execution.py:620`) on its success path, its timeout path (`_timeout_response_payload`, `execution.py:110`), and its unknown-tool error path; the `mcp_proxy` handler (`execution.py:777`); `list_mcp_tools` and `get_tool_schema`; `search_mcp_tools` (`discovery.py:468`); `set_mcp_server_enabled` (`server.py:471`); and `get_mcp_status` (`registry.py:99`). It asserts `isinstance(body["response_time_ms"], int)` on each response, error envelopes included.

**Verification:** run the new test plus `tests/servers/test_mcp_routes.py` and `tests/servers/routes/mcp_endpoints/` with `GOBBY_TEST_PROTECT=1`, then `uv run ruff check` and `uv run mypy` on the touched files.

## P2: Memory tools
`kind: framing`

### 1.2 Memory index line, brief search_memories and get_memory [category: code]
`kind: deliverable`

Targets:
- `src/gobby/memory/surface_format.py::*` — scope-reason: `_lead` becomes public `lead`, and the new `memory_index_line` projection is added beside `format_memory_index`
- `src/gobby/mcp_proxy/tools/memory.py::*` — scope-reason: `search_memories` and `get_memory` inside `create_memory_registry` gain `brief`
- `src/gobby/install/shared/skills/gobby/references/memory/search.md`
- `src/gobby/install/shared/skills/gobby/references/memory/scope.md`
- `docs/guides/memory.md`
- `tests/mcp_proxy/tools/test_memory_tools.py::*` — scope-reason: existing search and get assertions pass `brief=False`; new brief tests are added
- `tests/skills/test_memory_skill.py::*` — scope-reason: the pinned `search.md` wording changes
- `tests/workflows/test_memory_index_delivery.py::*` — scope-reason: imports the renamed `lead`

Consumers unchanged:
- `src/gobby/hooks/rule_evaluator.py` — no-edit-reason: `dedup_memory_results` reads `memories[].id` only.
- `src/gobby/hooks/dispatchers/mcp.py` — no-edit-reason: `format_discovery_result` dumps the result as JSON; a smaller result stays valid.
- `src/gobby/servers/routes/memory.py` — no-edit-reason: calls `MemoryManager` directly, not the tools.
- `src/gobby/cli/memory/crud.py` — no-edit-reason: calls `MemoryManager` directly.
- `src/gobby/workflows/engine/delivery_formatting.py` — no-edit-reason: calls `format_memory_index`, whose output is unchanged.
- `src/gobby/mcp_proxy/tools/memory_surface.py` — no-edit-reason: `surface_memories` keeps its own `_serialize` shape for the formatter.

**Research context:**
- `search_memories` (`memory.py`, `create_memory_registry.search_memories`) takes
  `query, limit=10, min_score, memory_type, tags_all, tags_any, tags_none`. It returns
  `{success, memories[], project_id, diagnostics}`, where each hit has 20
  fields (`id, content, rationale, type, created_at, updated_at, tags, project_id, is_global,
  source_task_id, created_by_agent, similarity, undecayed_similarity, search_via, ranking_score,
  raw_semantic_score, temporal_decay_factor, graph_confidence, ranking_mode,
  collapsed_duplicates`).
- `get_memory` returns `{success, memory{id, content, rationale, type, created_at, updated_at,
  project_id, is_global, source_type, source_task_id, created_by_agent, access_count, tags}}`.
- `references/memory/scope.md:4` says to inspect `get_memory` and the caller project before
  mutation, so `is_global` stays in brief.
- `references/memory/search.md` lines 12–13 and 19–21 tell agents to inspect content, rationale,
  similarity, diagnostics, and ranking provenance on search hits. That guidance moves to
  "use `get_memory` for text, and `brief=false` only when debugging ranking".
- `references/memory/overview.md` and the `<memory-index>` fetch instruction already assume
  `get_memory` returns full text; brief `get_memory` keeps `content` and `rationale`.
- `surface_format._lead` collapses whitespace and truncates on a word boundary at
  `LEAD_CHARS = 160` with an ellipsis. Reusing it keeps search hits identical to the pushed
  memory index.

**Implementation:**
- `surface_format.py`: rename `_lead` to `lead` and update `format_memory_index`. Add
  `memory_index_line(memory) -> dict` returning `{id, type, summary: lead(content), when:
  lead(rationale)}`, which reads attributes or keys.
- `search_memories(..., brief: bool = True)`: brief returns
  `{success, memories: [memory_index_line(hit)]}`. Full returns today's dict.
- `get_memory(memory_id, brief: bool = True)`: brief returns
  `{success, memory{id, type, content, rationale, tags, is_global}}`.
- Both descriptions end with the Decision Record 3 sentence.
- `search.md`, `scope.md`, and the `search_memories` / `get_memory` rows in
  `docs/guides/memory.md` describe the brief shapes and the debugging-only full mode.

**Acceptance:**
- 1.2.1 - `src/gobby/memory/surface_format.py::memory_index_line` returns exactly `{id, type, summary, when}` built with `lead`. symbol: `memory_index_line`. file: `src/gobby/memory/surface_format.py`.
- 1.2.2 - test: `tests/mcp_proxy/tools/test_memory_tools.py::test_search_memories_brief_returns_index_lines` asserts the default call returns only `success` and `memories`, and every hit has exactly the four index-line keys.
- 1.2.3 - test: `tests/mcp_proxy/tools/test_memory_tools.py::test_search_memories_brief_records_delivered_hits` asserts delivered-outcome rows are still written for brief results.
- 1.2.4 - test: `tests/mcp_proxy/tools/test_memory_tools.py::test_get_memory_brief_keeps_text_and_scope` asserts the default `get_memory` returns `id, type, content, rationale, tags, is_global` and no audit fields.
- 1.2.5 - Existing full-shape tests in `tests/mcp_proxy/tools/test_memory_tools.py` pass with `brief=False`. behavior: full-shape memory tool tests pass with `brief=False`.
- 1.2.6 - `src/gobby/install/shared/skills/gobby/references/memory/search.md` and `scope.md` describe the brief shapes; `tests/skills/test_memory_skill.py` matches the new wording. file: `src/gobby/install/shared/skills/gobby/references/memory/search.md`.

**Verification:** run `tests/mcp_proxy/tools/test_memory_tools.py`, `tests/skills/test_memory_skill.py`, `tests/workflows/test_memory_index_delivery.py`, and `tests/hooks/test_hook_manager_extra.py` with `GOBBY_TEST_PROTECT=1`; ruff and mypy on the touched sources.

### 1.3 Brief create_memory [category: code] (depends: 1.2)
`kind: deliverable`

Targets:
- `src/gobby/mcp_proxy/tools/memory_write.py::*` — scope-reason: `create_memory` inside `register_memory_write_tools` gains `brief` and builds `similar_existing` with the index line
- `src/gobby/install/shared/skills/gobby/references/memory/capture.md`
- `docs/guides/memory.md`
- `tests/mcp_proxy/tools/test_memory_tools.py::*` — scope-reason: the create tests that read `similar_existing` fields pass `brief=False` or assert the brief shape
- `tests/mcp_proxy/tools/test_memory.py::*` — scope-reason: `test_create_memory_requires_rationale` reads `memory.rationale`, which brief drops

Consumers unchanged:
- `src/gobby/install/shared/workflows/agents/memory-curator.yaml` — no-edit-reason: reads `auto_superseded` ids and `similar_existing[].id`, both kept.
- `src/gobby/review_learning/service.py` — no-edit-reason: imports only `derive_memory_create_provenance`.
- `tests/mcp_proxy/tools/test_tool_verbosity.py` — no-edit-reason: asserts `content` is absent from `memory`, which stays true.

**Research context:**
- `create_memory` returns `{success, memory{id, project_id, is_global, rationale,
  source_task_id, created_by_agent}, similar_existing[], auto_superseded[]?}`.
  `similar_existing` is built in `memory_write.py` from up to `SIMILAR_EXISTING_LIMIT = 5`
  search hits as `{id, content, rationale, similarity, raw_semantic_score}`. `auto_superseded`
  appears only when non-empty, as `{id, similarity}`.
- The skip path `{success, skipped: True, reason: "ephemeral_implementation_note"}` is already
  minimal.
- `references/memory/capture.md:21-24` tells agents to inspect `similar_existing` and
  `auto_superseded` and treat similarity as a duplicate signal, so `similarity` stays.
- `memory.rationale` is the caller's own input echoed back.

**Implementation:**
- `create_memory(..., brief: bool = True)`: brief returns `{success, memory{id},
  similar_existing: [memory_index_line(hit) + {similarity}], auto_superseded?}`. Full returns
  today's dict.
- The probe search and auto-supersede logic are unchanged.
- `capture.md` and the `create_memory` row in `docs/guides/memory.md` describe the brief shape
  and say to call `get_memory` before merging into a similar memory.

**Acceptance:**
- 1.3.1 - test: `tests/mcp_proxy/tools/test_memory_tools.py::test_create_memory_brief_returns_index_line_similars` asserts the default result has `memory == {"id": ...}` and each `similar_existing` item has exactly `id, type, summary, when, similarity`.
- 1.3.2 - test: `tests/mcp_proxy/tools/test_memory_tools.py::test_create_memory_brief_keeps_auto_superseded` asserts `auto_superseded` ids survive brief.
- 1.3.3 - Existing create tests in `tests/mcp_proxy/tools/test_memory_tools.py` and `tests/mcp_proxy/tools/test_memory.py` pass with `brief=False` where they read full fields. behavior: full-shape create tests pass with `brief=False`.
- 1.3.4 - `src/gobby/install/shared/skills/gobby/references/memory/capture.md` describes the brief `similar_existing` shape and the `get_memory` step. file: `src/gobby/install/shared/skills/gobby/references/memory/capture.md`.

**Verification:** run `tests/mcp_proxy/tools/test_memory_tools.py`, `tests/mcp_proxy/tools/test_memory.py`, and `tests/mcp_proxy/tools/test_tool_verbosity.py` with `GOBBY_TEST_PROTECT=1`; ruff and mypy on `memory_write.py`.

### 1.4 Brief review_task_memories [category: code] (depends: 1.3)
`kind: deliverable`

Targets:
- `src/gobby/mcp_proxy/tools/memory_review.py::*` — scope-reason: `review_task_memories` inside `register_memory_review_tools` gains `brief`
- `src/gobby/install/shared/skills/gobby/references/memory/post-task.md`
- `docs/guides/memory.md`
- `tests/mcp_proxy/tools/test_memory_review.py::*` — scope-reason: brief-shape tests are added beside the existing candidate tests

Consumers unchanged:
- `src/gobby/install/shared/workflows/rules/memory-lifecycle/review-task-memories-after-close.yaml` — no-edit-reason: instructs the call by name only.
- `src/gobby/install/shared/workflows/rules/context-handoff/block-tools-after-handoff-compact.yaml` — no-edit-reason: matches the tool name only.

**Research context:**
- `review_task_memories(task_id, changes_summary, session_id)` returns `{success, task_id,
  task_ref, source_task_id, candidate_count, candidates[], pending_reviews_complete,
  pending_reviews}`. Candidates are built inline as `{id, content, rationale, type, tags,
  similarity}`.
- `_record_review` stores only `candidate_ids`, so the review record is unaffected.
- `references/memory/post-task.md:9-12` tells agents to read each candidate against the
  completed work, update stale content, and use the returned `source_task_id`.
  Lines 17–18 rely on `pending_reviews_complete` and `pending_reviews`.
- `source_task_id` is the UUID the agent passes to `create_memory(source_task_id=...)`, so it
  stays (Decision Record 6). `task_id` duplicates it.

**Implementation:**
- `review_task_memories(..., brief: bool = True)`: brief returns `{success, task_ref,
  source_task_id, candidate_count, candidates: [memory_index_line(c) + {similarity}],
  pending_reviews_complete, pending_reviews}`.
- `post-task.md` says to judge candidates from the index line and call `get_memory` for any
  candidate that may need an update or supersede.
- The `review_task_memories` row in `docs/guides/memory.md` matches.

**Acceptance:**
- 1.4.1 - test: `tests/mcp_proxy/tools/test_memory_review.py::test_review_brief_returns_index_line_candidates` asserts the default keys and that each candidate has exactly `id, type, summary, when, similarity`.
- 1.4.2 - test: `tests/mcp_proxy/tools/test_memory_review.py::test_review_brief_records_same_candidate_ids` asserts the stored review record equals the full-mode record for the same task.
- 1.4.3 - `src/gobby/install/shared/skills/gobby/references/memory/post-task.md` tells agents to fetch candidates with `get_memory` before updating them. file: `src/gobby/install/shared/skills/gobby/references/memory/post-task.md`.

**Verification:** run `tests/mcp_proxy/tools/test_memory_review.py` and `tests/workflows/test_memory_lifecycle_rules.py` with `GOBBY_TEST_PROTECT=1`; ruff and mypy on `memory_review.py`.

## P3: Task tools
`kind: framing`

### 1.5 get_task brief drops duplicate identity [category: code]
`kind: deliverable`

Targets:
- `src/gobby/mcp_proxy/tools/tasks/_formatters.py::*` — scope-reason: `task_summary_payload` drops `seq_num` and `path_cache`, and its dependency rows drop `id`
- `src/gobby/mcp_proxy/tools/tasks/_crud.py::*` — scope-reason: the `get_task` description and `brief` parameter description gain the debugging-only wording, and `build_task_tree` moves out
- `src/gobby/mcp_proxy/tools/tasks/_crud_tree.py`
- `tests/mcp_proxy/tools/test_tasks_crud_coverage.py::*` — scope-reason: `SUMMARY_TASK_KEYS` loses `seq_num` and `path_cache`, and the dependency-row `id` assertion changes
- `tests/mcp_proxy/tools/tasks/test_create_task.py::*` — scope-reason: imports `build_task_tree` from its new module

Consumers unchanged:
- `src/gobby/install/shared/workflows/agents/developer.yaml` — no-edit-reason: the `route_skills` and `submit` `get_task` handlers read `id`, `ref`, and `state`, all kept.
- `src/gobby/mcp_proxy/services/result_offload.py` — no-edit-reason: an offloaded card's envelope keeps `id` and `state` when the card carries them.
- `tests/mcp_proxy/tools/tasks/test_get_task_response_shape.py` — no-edit-reason: its `task_summary_payload` test asserts `ref`, `id`, `title`, and `validation_criteria`, all kept.
- `tests/mcp_proxy/services/test_result_offload.py` — no-edit-reason: `_summary_card` builds a `task_summary_payload` card, and `test_oversized_task_card_preserves_its_identity_and_flat_state` asserts the offloaded envelope keeps `id` and `state`, both kept.

**Research context:**
- `_formatters.py::task_summary_payload` emits `ref, id, seq_num, title, task_type, category,
  priority, path_cache, description, validation_criteria, labels, parent_task_id, created_at,
  updated_at, state{...}, dependencies{blocked_by, blocking}` (rows carry `ref, id, title, state,
  dep_type`), and `allow_automation, unattended, checkout_mode, assigned_agent,
  implementation_domain, additional_skills`.
- `ref` is derived from `seq_num`, so `seq_num` and `path_cache` duplicate it. No rule,
  observer, web, or skill consumer reads `seq_num` or `path_cache` from brief `get_task`, and
  none reads a dependency row's `id`.
- `id` stays in the brief card because the developer definition reads it. Since #22997, two
  `get_task` `on_mcp_success` handlers in
  `src/gobby/install/shared/workflows/agents/developer.yaml` match
  `(tool_output.get('result') or tool_output).get('id') == vars.get('assigned_task_id')`
  (approximate lines 198 and 395 on `0.5.0` at `f17a855761`):
  - `route_skills` matches it before it sets `assigned_task_ref` from the card's `ref`;
  - `submit`'s close reset matches it together with `state.is_closed`, then sets
    `task_claimed` to false after a reviewed close.
  `assigned_task_id` is the UUID `claim_task` returns as `task_id`. Decision Record 5 keeps a
  field that an agent workflow acts on, and the Constraints require every workflow reader to keep
  working, so this deliverable names `id` as kept.
- An oversized card is offloaded. `ToolResultOffloader` then keeps `id` and `state` in the
  envelope, but only when the card carries them (`result_offload.py::_SCALAR_FIELD_PRIORITY`,
  `_summarize_scalar_fields`). The `submit` close reset relies on that for large cards.
- Rejected: moving both handlers to `ref`. `claim_task` returns only `task_id` and `title`, and
  `assigned_task_ref` is set only after the first matching `get_task`, so a `ref` comparison has
  nothing to compare against unless `claim_task`'s payload changes. That change is outside this
  plan.
- The other agents' `get_task` `on_mcp_success` observers read `success`,
  `result.state.is_closed`, and `result.state.current_stage`. The `track-task-claim` and
  `disclose-claimed-task-extra-skills` rules read `id` only from `claim_task` and `create_task`
  output.
- `parent_task_id` stays: it is a UUID used as an argument, and a parent ref is not in the payload.
- `_crud.py` is 969 lines on `0.5.0` at `f17a855761`. The split: move the module-level
  `build_task_tree` (approximately lines 890–969) from `_crud.py` into the new
  `src/gobby/mcp_proxy/tools/tasks/_crud_tree.py`. Its only importer is
  `tests/mcp_proxy/tools/tasks/test_create_task.py`.

**Implementation:**
- Remove `seq_num` and `path_cache` from `task_summary_payload` and `id` from its dependency
  rows. The card keeps `ref` and `id`. The full view is unchanged.
- In `get_task`, the `brief` description becomes: "If true (default), return the actionable
  card. Set false only for debugging; it adds seq_num, path_cache, project and session ids,
  commits, closure, validation, merge and dispatch details, escalation fields, links, dates, and
  full dependency rows."

**Acceptance:**
- 1.5.1 - test: `tests/mcp_proxy/tools/test_tasks_crud_coverage.py::test_get_task_brief_has_no_duplicate_identity` asserts brief `get_task` has `ref` and `id`, has neither `seq_num` nor `path_cache`, and dependency rows carry no `id`.
- 1.5.2 - `src/gobby/mcp_proxy/tools/tasks/_crud_tree.py` holds `build_task_tree`, and `_crud.py` is below 950 lines. file: `src/gobby/mcp_proxy/tools/tasks/_crud_tree.py`.
- 1.5.3 - The `get_task` `brief` parameter description in `src/gobby/mcp_proxy/tools/tasks/_crud.py` contains "only for debugging". file: `src/gobby/mcp_proxy/tools/tasks/_crud.py`.

**Verification:** run `tests/mcp_proxy/tools/test_tasks_crud_coverage.py`, `tests/mcp_proxy/tools/tasks/`, `tests/mcp_proxy/tools/test_tasks_schema_coverage.py`, and `tests/mcp_proxy/services/test_result_offload.py::test_oversized_task_card_preserves_its_identity_and_flat_state` with `GOBBY_TEST_PROTECT=1`; ruff and mypy on the touched sources.

### 1.6 Brief close_task replaces response_detail [category: code]
`kind: deliverable`

Targets:
- `src/gobby/mcp_proxy/tools/tasks/_lifecycle_close_tool.py::*` — scope-reason: `close_task` swaps `response_detail` for `brief` and projects the brief shape after the evaluation returns
- `src/gobby/mcp_proxy/tools/tasks/_lifecycle_close.py::*` — scope-reason: `_evaluate_close` takes `brief` instead of `response_detail`, and three helpers move out
- `src/gobby/mcp_proxy/tools/tasks/_lifecycle_close_helpers.py`
- `src/gobby/mcp_proxy/tools/tasks/_lifecycle_close_preview.py::*` — scope-reason: `CloseEvaluation` stores `brief` and derives diagnostic sections from `not brief`
- `src/gobby/mcp_proxy/tools/tasks/_lifecycle_close_orchestration.py::*` — scope-reason: persisted close arguments use `brief`, and the review-required and pending-review responses get the brief projection
- `src/gobby/install/shared/skills/gobby/references/tasks/closing.md`
- `tests/mcp_proxy/tools/tasks/test_close_task_flow.py::*` — scope-reason: shape assertions pass `brief=False` or assert the brief shape
- `tests/mcp_proxy/tools/tasks/test_mcp_close_checklist.py::*` — scope-reason: gate-list assertions use `brief=False`
- `tests/tasks/test_close_evidence_bounds.py::*` — scope-reason: the diagnostic-payload test switches from `response_detail="diagnostic"` to `brief=False`
- `tests/e2e/test_task_close_checklist_e2e.py::*` — scope-reason: two calls pass `response_detail`
- `tests/mcp_proxy/services/test_argument_validation.py::*` — scope-reason: the fixture schema names `response_detail`
- `tests/mcp_proxy/tools/test_tasks_schema_coverage.py::*` — scope-reason: the close_task schema field list changes

Consumers unchanged:
- `src/gobby/workflows/observer_utils.py` — no-edit-reason: `_successful_close_result` reads `error`, `status`, and `closed`, all kept.
- `src/gobby/mcp_proxy/services/result_offload.py` — no-edit-reason: summarizes `success`, `closed`, `can_close`, `task_id`, `status` when present.
- `src/gobby/tasks/close_checklist.py` — no-edit-reason: `CloseChecklist.summary()` is unchanged; brief filters its output.

**Research context:**
- `close_task(task_id, reason, changes_summary, skip_validation, override_justification,
  scope_justification, commit_sha, project_path, preview, response_detail)` lives in
  `_lifecycle_close_tool.py::register_close_task.close_task`. It adds `preview` and `can_close`
  after the evaluation.
- `CloseEvaluation.response` (`_lifecycle_close_preview.py`) returns `success, preview,
  can_close, closed, task_id, commit_shas, gates`, and on error `error, message,
  blocking_reasons, required_actions`, plus `validation_status`, `verdict`, and `extra`
  entries (fingerprints arrive through `extra`). Diagnostic mode adds `checklist`,
  `transcript_evidence`, and `validation_feedback`.
- `gates` is `CloseChecklist.summary()`: `{item, name, status, message}` for every gate.
  `checklist` (diagnostic only) is the detailed copy. The HTTP layer wraps a `success: False`
  result under `result`, so a single response never carries both `gates` and `result.gates`.
- `CloseGateResult.passed` is `status != "failed"`, so `skipped` and `not_run` count as passed.
  `closing.md:29-31` tells agents that preview reports gate 13 as `not_run`, so brief lists
  every gate whose status is not `passed`.
- The review-required response (`_lifecycle_close_orchestration.py::launch_close_review` /
  `_launch_promoted_review`) and `pending_review_response` build their own dicts:
  `error="close_review_required", review_id, reviewer_run_id, review_fingerprint,
  deterministic_evidence_fingerprint, review_status, reviewer_provider, reviewer_model,
  prompt_chars, prompt_limit, criterion_count, criterion_indexes, manifest_count, excerpt_chars,
  close_review_duration_ms`.
- Hard dependency: `closing.md:33-35` and the developer, tech-writer, and merge-orchestrator
  prompts tell agents to `wait_for_agent` on `reviewer_run_id`.
- `submit_close_review` (`agentic_close_review.py::build_terminal_review_payload`) consumes
  `CloseEvaluation.response()` internally. Brief must apply in the tool wrapper and the review
  response builders, never inside `response()`.
- `response_detail` is persisted with close arguments and validated in
  `_lifecycle_close_orchestration.py` ("Persisted close argument 'response_detail' is invalid"),
  and threaded through `_lifecycle_close.py::_evaluate_close`.
- `_lifecycle_close.py` is 863 lines. The split: move `_acceptance_root_diagnostic`,
  `_is_deliberate_close`, and `_apply_escalated_close_gate` from `_lifecycle_close.py` into the
  new `src/gobby/mcp_proxy/tools/tasks/_lifecycle_close_helpers.py`.

**Implementation:**
- Replace `response_detail: Literal["concise","diagnostic"] = "concise"` with
  `brief: bool = True` everywhere it is threaded. Diagnostic sections appear when `brief` is
  false. The persisted argument key becomes `brief` (a bool).
- Add one projection, `brief_close_payload(result) -> dict`, applied by the `close_task`
  wrapper and by the review-required and pending-review responses when `brief` is true. It keeps:
  - `success, closed, can_close, preview`;
  - `task_ref` in place of the `task_id` UUID;
  - `commit_shas`;
  - `gates` filtered to entries whose status is not `passed`, plus `gates_passed: <count>`;
  - `error, message, blocking_reasons, required_actions`;
  - `validation_status, verdict`;
  - on review responses: `review_id, reviewer_run_id, review_status`.
  It drops fingerprints, reviewer provider and model, prompt and excerpt sizes, criterion and
  manifest counts, and durations.
- `closing.md` describes the brief shape ("reports failed, skipped, and not-run gates plus a
  passed count; `brief=false` shows every gate, for debugging only").

**Acceptance:**
- 1.6.1 - test: `tests/mcp_proxy/tools/tasks/test_close_task_flow.py::test_brief_blocked_close_lists_only_unpassed_gates` asserts a blocked close returns only non-`passed` gates plus `gates_passed`, with `error`, `blocking_reasons`, and `required_actions` present.
- 1.6.2 - test: `tests/mcp_proxy/tools/tasks/test_close_task_flow.py::test_brief_review_required_keeps_reviewer_run_id` asserts the review-required brief response carries `error == "close_review_required"`, `review_id`, `reviewer_run_id`, and no fingerprint fields.
- 1.6.3 - test: `tests/tasks/test_close_evidence_bounds.py::test_diagnostic_close_payload_carries_each_section_once_within_bound` passes with `brief=False`.
- 1.6.4 - No `response_detail` identifier remains under `src/gobby/mcp_proxy/tools/tasks/`; persisted close arguments carry `brief`. file: `src/gobby/mcp_proxy/tools/tasks/_lifecycle_close_tool.py`.
- 1.6.5 - `src/gobby/mcp_proxy/tools/tasks/_lifecycle_close_helpers.py` holds the three moved helpers, and `_lifecycle_close.py` is below 850 lines. file: `src/gobby/mcp_proxy/tools/tasks/_lifecycle_close_helpers.py`.
- 1.6.6 - `src/gobby/install/shared/skills/gobby/references/tasks/closing.md` describes the brief close shape. file: `src/gobby/install/shared/skills/gobby/references/tasks/closing.md`.

**Granularity:** one deliverable. The parameter swap, the persisted key, and the projection are one switch; splitting them would leave `response_detail` and `brief` coexisting between leaves, which Decision Record 9 rules out.

**Verification:** run `tests/mcp_proxy/tools/tasks/`, `tests/tasks/test_close_evidence_bounds.py`, `tests/tasks/test_close_checklist.py`, `tests/mcp_proxy/tools/test_task_lifecycle_coverage.py`, `tests/mcp_proxy/tools/test_tasks_schema_coverage.py`, `tests/mcp_proxy/services/test_argument_validation.py`, and `tests/agents/watchdog/test_close_review_parked_caller.py` with `GOBBY_TEST_PROTECT=1`; ruff and mypy on the touched sources. The e2e file runs only if the lane has an isolated e2e daemon.

### 1.7 Retire get_task(brief=false) guidance [category: config] (depends: 1.5)
`kind: deliverable`

Targets:
- `src/gobby/install/shared/workflows/agents/analyst.yaml::*` — scope-reason: step and prompt prose prescribes `get_task(brief=false)`
- `src/gobby/install/shared/workflows/agents/architect.yaml::*` — scope-reason: step and prompt prose prescribes `get_task(brief=false)`
- `src/gobby/install/shared/workflows/agents/tech-writer.yaml::*` — scope-reason: step and prompt prose prescribes `get_task(brief=false)`
- `src/gobby/install/shared/workflows/agents/product-manager.yaml::*` — scope-reason: step and prompt prose prescribes `get_task(brief=false)`
- `src/gobby/install/shared/workflows/agents/merge-orchestrator.yaml::*` — scope-reason: step and prompt prose prescribes `get_task(brief=false)`
- `src/gobby/install/shared/skills/gobby/references/tasks/overview.md`
- `src/gobby/install/shared/skills/gobby/references/tasks/implementation.md`
- `src/gobby/install/shared/skills/gobby/references/build/starting.md`
- `src/gobby/install/shared/skills/gobby/references/review/epic.md`
- `src/gobby/mcp_proxy/tools/spawn_agent/_step_state.py::*` — scope-reason: step guidance text prescribes `get_task(brief=false)`
- `src/gobby/storage/tasks/_models.py::*` — scope-reason: a model docstring or guidance string prescribes `get_task(brief=false)`
- `tests/workflows/test_workflows_agent_definitions.py::*` — scope-reason: a guard test asserting no bundled definition prescribes `get_task(brief=false)` is added beside the existing bundled-definition checks

**Research context:**
- The caller audit, re-run on `0.5.0` at `f17a855761`, finds `get_task(brief=false)` prescribed
  for ordinary task reading in five agent definitions, four skill references,
  `spawn_agent/_step_state.py` (near line 104), and `storage/tasks/_models.py` (near lines 454
  and 458, spelled `brief=False`). #22997 replaced `frontend-developer.yaml`,
  `fullstack-developer.yaml`, and `backend-developer.yaml` with one `developer.yaml`, which reads
  the card with plain `get_task`. `researcher.yaml` and `_lifecycle_claim.py` no longer prescribe
  full mode.
- The brief card already carries description, validation criteria, labels, state, execution
  settings, and dependency summaries, which is everything those instructions ask agents to read.
  Under Decision Record 2, prescribing full mode for normal work contradicts the contract.
- Bundled templates sync to the DB on the next daemon start; the implementer does not run sync.
- `tests/workflows/test_workflows_dry_run.py` holds unit tests of the dry-run evaluator only;
  the bundled-definition loader (`AGENTS_DIR`, `_load_yaml`) lives in
  `tests/workflows/test_workflows_agent_definitions.py`, so the guard test goes there.

**Implementation:** replace each prescription with plain `get_task` and say the card includes
dependencies and acceptance criteria. Keep any wording that is explicitly about debugging.

**Acceptance:**
- 1.7.1 - `gcode grep -F "brief=false" src/gobby/install/shared/workflows/agents -m 50` returns no `get_task` prescription. behavior: no bundled agent definition prescribes `get_task(brief=false)`.
- 1.7.2 - `gcode grep -F "brief=false" src/gobby/install/shared/skills/gobby/references -m 50` returns no `get_task` prescription outside debugging wording. behavior: no skill reference prescribes `get_task(brief=false)` for ordinary reads.
- 1.7.3 - `src/gobby/mcp_proxy/tools/spawn_agent/_step_state.py` and `src/gobby/storage/tasks/_models.py` no longer prescribe `get_task(brief=false)` in either spelling. file: `src/gobby/mcp_proxy/tools/spawn_agent/_step_state.py`.
- 1.7.4 - test: `tests/workflows/test_workflows_agent_definitions.py::test_no_bundled_definition_prescribes_full_get_task` loads every bundled agent YAML and asserts no prompt or step text contains `get_task(brief=false)`; the module's existing bundled-definition tests pass for every edited definition.

**Granularity:** more than six Target files, all one mechanical text change with one reason. Splitting by file type would create per-file chores, which the drafting rules forbid.

**Verification:** run `tests/workflows/test_workflows_agent_definitions.py` and `tests/skills/test_reference_library.py` with `GOBBY_TEST_PROTECT=1`, and rerun the two `gcode grep` checks.

## P4: Agent and session tools
`kind: framing`

### 1.8 Brief wait_for_agent and get_agent_result [category: code]
`kind: deliverable`

Targets:
- `src/gobby/mcp_proxy/tools/agents_query_tools.py::*` — scope-reason: `_result_payload` gains the brief projection, `get_agent_result` gains `brief`, and `wait_for_agent` and `wait_for_output` move out
- `src/gobby/mcp_proxy/tools/agents_wait_tools.py`
- `src/gobby/mcp_proxy/tools/agents_registry.py::*` — scope-reason: registers the moved wait tools
- `src/gobby/install/shared/skills/gobby/references/agents/lifecycle.md`
- `tests/mcp_proxy/tools/test_agents.py::*` — scope-reason: `test_public_signature_only_accepts_run_id` changes to `run_id` plus `brief`, and full-shape assertions pass `brief=False`
- `tests/mcp_proxy/tools/test_agent_capture_results.py::*` — scope-reason: sandbox and capture assertions pass `brief=False` or assert the brief pointer
- `tests/mcp_proxy/tools/test_agent_live_output.py::*` — scope-reason: live-output advertisement assertions change
- `tests/mcp_proxy/tools/test_agent_live_stats.py::*` — scope-reason: timestamp assertions pass `brief=False`
- `tests/agents/test_headless_coordination_waits.py::*` — scope-reason: imports `register_agent_query_tools` to reach the wait tools

Consumers unchanged:
- `src/gobby/mcp_proxy/tools/agents_payloads.py` — no-edit-reason: `_agent_result_payload` keeps the full shape for completion wakes; brief filters in `_result_payload`.
- `tests/runner_init/test_detection_registry_composition.py` — no-edit-reason: patches `agents_registry.register_agent_query_tools`, which still exists.

**Research context:**
- `wait_for_agent(run_id)` and `get_agent_result(run_id, include_prompt=False)` share
  `_result_payload`, which calls `agents_payloads.py::_agent_result_payload` and
  `agent_live_output.py::live_output_reference`. The payload carries:
  - liveness: `child_status, wait_kind, blocked_on_parent, last_progress_at,
    progress_age_seconds, stall_suspected`;
  - `run_id, status, result, error, provider, model, tool_calls_count, turns_used, started_at,
    completed_at, result_at, child_session_id, terminal_reason`;
  - optionally `dirty_paths, prompt, external_write_grant, sandbox{...}, capture{...}`;
  - `live_output{available, source, ordering, line_limit, char_limit, retrieval_tool[, reason]}`.
  `wait_for_agent` adds `success, completed, notification_registered`, plus
  `notification_session_id` while active.
- Hard dependencies:
  - `merge-orchestrator.yaml:451-472` reads `run_id`;
  - `references/agents/lifecycle.md:11` requires fetching `get_agent_capture` when a result was
    truncated, and truncation is signalled only by `capture.prefix_truncated`.
- `dirty_paths` feeds the public `status` derivation, so it stays when non-empty.
- Nothing reads `notification_registered` or `notification_session_id` from output; the passive
  wait gates read database subscriptions.
- `agents_query_tools.py` is 1,004 lines, already over the ceiling. The split: move
  `wait_for_agent` and `wait_for_output` (lines 374–729) from `agents_query_tools.py` into the
  new `src/gobby/mcp_proxy/tools/agents_wait_tools.py` as `register_agent_wait_tools(registry,
  ctx)`, called from `agents_registry.py` next to `register_agent_query_tools`. Lift
  `_lookup_run` and `_result_payload` out of the closure into module-level functions that take
  `ctx`, so both modules share them.

**Implementation:**
- `wait_for_agent(run_id, brief: bool = True)` and `get_agent_result(run_id, include_prompt,
  brief: bool = True)`.
- Brief keeps:
  - `success, run_id, status, completed, result, error, terminal_reason`;
  - `child_status, progress_age_seconds, stall_suspected, blocked_on_parent`;
  - `recovery_pending` when present, and `dirty_paths` when non-empty;
  - `capture` only when `prefix_truncated` or `malformed` is true;
  - `live_output: {available: true}` only while the run is active and live output is available;
  - `prompt` whenever `include_prompt` is true: `_agent_result_payload` adds it at
    `agents_payloads.py:168` and the brief filter never removes it, so the flag means the
    same in both modes.
- Brief drops `sandbox`, `external_write_grant`, `provider`, `model`, counters, `started_at`,
  `completed_at`, `result_at`, `last_progress_at`, `wait_kind`, `child_session_id`,
  `notification_*`, and the rest of `live_output`.
- Both descriptions keep advertising `get_agent_live_output` and add the Decision Record 3
  sentence.
- `lifecycle.md` states that a brief result carries `capture` only when the text was truncated.

**Acceptance:**
- 1.8.1 - test: `tests/mcp_proxy/tools/test_agents.py::test_wait_for_agent_brief_shape` asserts the default completed-run result has exactly the kept keys and no `sandbox`.
- 1.8.2 - test: `tests/mcp_proxy/tools/test_agent_capture_results.py::test_brief_result_keeps_capture_pointer_when_truncated` asserts `capture` is present when `prefix_truncated` is true and absent otherwise.
- 1.8.3 - test: `tests/mcp_proxy/tools/test_agent_live_output.py::test_wait_for_agent_brief_advertises_live_output_availability` asserts an active run carries `live_output == {"available": True}`.
- 1.8.4 - `src/gobby/mcp_proxy/tools/agents_wait_tools.py` registers `wait_for_agent` and `wait_for_output`; `agents_query_tools.py` is below 850 lines. file: `src/gobby/mcp_proxy/tools/agents_wait_tools.py`.
- 1.8.5 - test: `tests/mcp_proxy/tools/test_agents.py::TestWaitForAgent::test_public_signature_only_accepts_run_id` asserts the parameters are exactly `run_id` and `brief`.
- 1.8.6 - test: `tests/mcp_proxy/tools/test_agents.py::TestGetAgentResult::test_prompt_opt_in_survives_brief` sits beside `test_prompt_is_opt_in` (`:242`) and asserts all four combinations: `prompt` is absent for `include_prompt=False` and present for `include_prompt=True`, with `brief` true and with `brief=False`.

**Verification:** run `tests/mcp_proxy/tools/test_agents.py`, `test_agent_capture_results.py`, `test_agent_live_output.py`, `test_agent_live_stats.py`, `tests/agents/test_headless_coordination_waits.py`, `tests/events/test_wake_wiring.py`, `tests/agents/watchdog/test_close_review_parked_caller.py`, and `tests/runner_init/test_detection_registry_composition.py` with `GOBBY_TEST_PROTECT=1`; ruff and mypy on the touched sources.

### 1.9 Brief get_session [category: code]
`kind: deliverable`

Targets:
- `src/gobby/mcp_proxy/tools/sessions/_crud.py::*` — scope-reason: `get_session` inside `register_crud_tools` gains `brief`
- `src/gobby/install/shared/skills/gobby/references/sessions/relationships.md`
- `tests/mcp_proxy/tools/test_sessions_query_tools.py::*` — scope-reason: a brief-shape test is added

Consumers unchanged:
- `src/gobby/storage/session_models.py` — no-edit-reason: `Session.to_dict` keeps the full record; brief projects it in the tool.

**Research context:**
- `get_session(session_id)` returns `{"found": True, **session.to_dict()}` after
  `_apply_task_refs`, which is more than 70 keys. The large ones are `handoff_markdown`,
  `summary_markdown`, `original_prompt`, `last_assistant_content`, and `terminal_context`.
- No YAML, hook, web, crate, or CLI reader exists.
- `references/sessions/relationships.md:4-14` starts parentage tracing from `get_session` and
  reads `parent_session_id`, `agent_depth`, `agent_run_id`, and `spawned_by_agent_id`. Those
  stay, and the reference changes from "use the actual session UUID" to "use the session ref".
  `get_session` accepts refs such as `gobby#13821`.
- `tests/mcp_proxy/tools/test_sessions_query_tools.py::test_list_sessions_and_get_session_agree_on_task_refs`
  requires `claimed_task_refs`, `created_task_refs`, and `closed_task_refs`.

**Implementation:**
- `get_session(session_id, brief: bool = True)`: brief returns `{found, ref, status, title,
  source, model, git_branch, last_activity, claimed_task_refs, created_task_refs,
  closed_task_refs, parent_session_id, agent_depth, agent_run_id, spawned_by_agent_id}`,
  omitting null and empty values.
- `relationships.md` is updated as above, and the description gets the Decision Record 3 sentence.

**Acceptance:**
- 1.9.1 - test: `tests/mcp_proxy/tools/test_sessions_query_tools.py::test_get_session_brief_keeps_identity_tasks_and_lineage` asserts the default result has the kept keys and none of `handoff_markdown`, `summary_markdown`, `terminal_context`, `transcript_path`, or `original_prompt`.
- 1.9.2 - test: `tests/mcp_proxy/tools/test_sessions_query_tools.py::test_list_sessions_and_get_session_agree_on_task_refs` passes unchanged.
- 1.9.3 - `src/gobby/install/shared/skills/gobby/references/sessions/relationships.md` traces parentage by session ref. file: `src/gobby/install/shared/skills/gobby/references/sessions/relationships.md`.

**Verification:** run `tests/mcp_proxy/tools/test_sessions_query_tools.py` with `GOBBY_TEST_PROTECT=1`; ruff and mypy on `sessions/_crud.py`.

### 1.10 Brief get_handoff strips duplicated found-work notes [category: code]
`kind: deliverable`

Targets:
- `src/gobby/mcp_proxy/tools/sessions/_handoff.py::*` — scope-reason: `get_handoff` inside `register_handoff_tools` gains `brief` and strips the rendered found-work notes on the consumed path
- `src/gobby/install/shared/skills/gobby/references/sessions/handoffs.md`
- `docs/contracts/session-boundary.md`
- `tests/sessions/test_handoff_found_work.py::*` — scope-reason: brief and full round-trip assertions

Consumers unchanged:
- `src/gobby/install/shared/workflows/rules/memory-lifecycle/surface-memories-on-tool-intent.yaml` — no-edit-reason: requires `handoff` to be truthy, and brief keeps it non-empty.
- `src/gobby/workflows/observer_context_usage.py` — no-edit-reason: reads `found` only.
- `src/gobby/sessions/handoff_records.py` — no-edit-reason: stored markdown keeps the found-work notes for native delivery; brief filters only the tool result.
- `src/gobby/workflows/enforcement/blocking.py` — no-edit-reason: `ARGUMENTLESS_PROXY_TOOLS` matches the tool name regardless of arguments.

**Research context:**
- `get_handoff(agent_run_id=None)` has four response shapes. On the consumed path it returns
  `{success, found, session_id, handoff, found_work[{finding, disposition, ref}],
  found_work_gate_armed}`.
- The duplication is stored: `sessions/handoff_records.py` merges each entry's `as_note()`
  ("Found work: …") into `notes` before rendering. The consumed markdown already contains those
  bullets beside the structured array.
- On the `agent_run_id` path the markdown is the only copy, so brief never strips there.
- `docs/contracts/session-boundary.md:237` says `get_handoff` "accepts no lookup arguments", and
  `handoffs.md:35` says entries render under Notes. Both need the brief note.

**Implementation:**
- `get_handoff(agent_run_id=None, brief: bool = True)`.
- On the consumed path with brief true, strip the found-work notes inside the `## Notes`
  region only. `build_handoff_payload` (`handoff_records.py:74-144`) renders them first under
  `## Notes` as `- ` bullets, `FoundWorkEntry.as_note` (`:29`) is
  `Found work: {finding} ({disposition} {ref})`, and a finding may span lines, so a note is a
  bullet block: the `- ` line plus every following line up to the next `- ` bullet or
  heading. For each returned `found_work` entry the block whose text equals
  `"- " + entry.as_note()` is removed; nothing is matched by prefix or outside the region, so
  a look-alike line elsewhere in the markdown stays. When the region is left with no bullets
  its `## Notes` heading goes too. `ConsumedHandoff` (`sessions/handoff.py:117`) carries only
  `markdown` and `found_work`, so the projection is a pure function of those two values.
- `handoff` never becomes empty. Every other byte of the markdown and the `agent_run_id` path
  are unchanged.
- Docs note that `brief` is optional and that found work appears once, structured.

**Acceptance:**
- 1.10.1 - test: `tests/sessions/test_handoff_found_work.py::test_brief_get_handoff_lists_found_work_once` asserts the consumed brief `handoff` contains no found-work note block and `found_work` still lists every entry.
- 1.10.2 - test: `tests/sessions/test_handoff_found_work.py::test_found_work_renders_as_notes_and_round_trips` passes with `brief=False`.
- 1.10.3 - `docs/contracts/session-boundary.md` and `src/gobby/install/shared/skills/gobby/references/sessions/handoffs.md` describe the optional `brief` argument. file: `docs/contracts/session-boundary.md`.
- 1.10.4 - test: `tests/sessions/test_handoff_found_work.py::test_brief_get_handoff_strips_only_notes_blocks` covers a multiline finding, a look-alike `Found work:` line outside `## Notes` that survives, a handoff with no found work returned byte-identical, an emptied `## Notes` heading dropped, `brief=False` unchanged, and the `agent_run_id` path unchanged.

**Verification:** run `tests/sessions/test_handoff_found_work.py`, `tests/sessions/test_handoff.py`, `tests/hooks/test_stop_handoff_pending.py`, `tests/workflows/test_memory_lifecycle_rules.py`, and `tests/workflows/test_progressive_discovery_rules.py` with `GOBBY_TEST_PROTECT=1`; ruff and mypy on `_handoff.py`.

## P5: Workflow and skill wording
`kind: framing`

### 1.11 list_rules defaults to brief [category: code]
`kind: deliverable`

Targets:
- `src/gobby/mcp_proxy/tools/workflows/_rules.py::*` — scope-reason: `list_rules` defaults `brief=True` and its description gains the debugging-only wording
- `src/gobby/mcp_proxy/tools/workflows/__init__.py::*` — scope-reason: the `_list_rules` registry wrapper defaults `brief=True`
- `src/gobby/servers/routes/rules.py::*` — scope-reason: `list_rules_endpoint` passes `brief=False` explicitly for the web UI
- `tests/mcp_proxy/tools/test_rule_tools.py::*` — scope-reason: default-call assertions change
- `tests/mcp_proxy/tools/workflows/test_workflow_project_scope.py::*` — scope-reason: the `list_rules` call reads the default shape
- `tests/servers/routes/test_rules_routes.py::*` — scope-reason: a full-summary assertion for `GET /api/rules` is added beside the class-based `test_list_all_rules` (`:119`)

Consumers unchanged:
- `src/gobby/cli/rules.py` — no-edit-reason: has its own listing implementation.
- `web/src/hooks/useRules.ts` — no-edit-reason: reads `GET /api/rules`, which keeps the full shape.

**Research context:**
- The `list_rules` MCP tool is registered in `workflows/__init__.py` (`_list_rules`, near line
  254) and wraps `_rules.py::list_rules`. Both default `brief=False`. Brief returns
  `{name, event, group, enabled}` (`_rule_brief`); full returns `_rule_summary`.
- `GET /api/rules` (`servers/routes/rules.py::list_rules_endpoint`) calls `_rules.list_rules`
  without `brief`, and the web UI needs the full fields, so the route must pass `brief=False`.
- `references/rules/diagnostics.md` and `overview.md` already say `list_rules(brief=true)`.

**Acceptance:**
- 1.11.1 - test: `tests/mcp_proxy/tools/test_rule_tools.py::test_list_rules_defaults_to_brief` asserts a no-argument call returns entries with exactly `name, event, group, enabled`.
- 1.11.2 - test: `tests/servers/routes/test_rules_routes.py::test_list_rules_endpoint_returns_full_summary` asserts `GET /api/rules` still returns every `_rule_summary` field.
- 1.11.3 - `src/gobby/mcp_proxy/tools/workflows/_rules.py::list_rules` description says `brief=false` is only for debugging. file: `src/gobby/mcp_proxy/tools/workflows/_rules.py`.

**Verification:** run `tests/mcp_proxy/tools/test_rule_tools.py`, `tests/mcp_proxy/tools/workflows/test_workflow_project_scope.py`, and `tests/servers/routes/test_rules_routes.py` with `GOBBY_TEST_PROTECT=1`; ruff and mypy on the touched sources.

### 1.12 Align existing brief wording [category: code]
`kind: deliverable`

Targets:
- `src/gobby/mcp_proxy/tools/skills/get_skill.py::*` — scope-reason: the `get_skill` and `get_skill_file` descriptions state the debugging and skill-management exception
- `src/gobby/mcp_proxy/tools/agent_messaging.py::*` — scope-reason: the `send_message` description states debugging-only full mode
- `src/gobby/mcp_proxy/tools/workflows/_pipelines.py::*` — scope-reason: the `list_pipeline_executions` description states debugging-only full mode, and `_auto_subscribe_lineage` moves out
- `src/gobby/mcp_proxy/tools/workflows/_pipeline_lineage.py`
- `tests/events/test_wake_wiring.py::*` — scope-reason: two tests import `_auto_subscribe_lineage` from its new module
- `tests/mcp_proxy/tools/workflows/test_mcp_proxy_tools_workflows_pipelines.py::*` — scope-reason: two tests import `_auto_subscribe_lineage` from its new module
- `src/gobby/mcp_proxy/instructions.py::*` — scope-reason: the server instructions describe brief as debugging or skill management only
- `src/gobby/install/shared/prompts/mcp/progressive-discovery.md`
- `src/gobby/install/shared/skills/gobby/references/skills/loading.md`
- `src/gobby/install/shared/skills/gobby/references/skills/lifecycle.md`
- `tests/mcp_proxy/test_instructions.py::*` — scope-reason: a test asserting the brief sentence is added beside `test_instructions_name_gated_skill_file_tools` (`:161`)

**Research context:**
- Existing wording:
  - `send_message`: "Pass brief=false for the full message, selector, wake, and broadcast
    diagnostics."
  - `get_skill` / `get_skill_file`: "using a brief projection by default".
  - `list_pipeline_executions`: "Use brief=True (default) for compact output."
  - `instructions.py:33`, `progressive-discovery.md:26`, `loading.md:6`, and `lifecycle.md:4`
    say `brief=false` is for management work.
- Josh's exception (Decision Record 2): `get_skill` / `get_skill_file` allow `brief=false` for
  skill management (ids, `content_hash`, version). Every other tool is debugging only.
- `_pipelines.py` is 855 lines on `0.5.0` at `f17a855761`, over the 850-line split trigger in
  the Constraints. The split: move the module-level `_auto_subscribe_lineage` (approximately
  lines 291–340) into the new `src/gobby/mcp_proxy/tools/workflows/_pipeline_lineage.py`, beside
  the existing `_pipeline_discovery`, `_pipeline_execution`, `_pipeline_exposed`, and
  `_pipeline_query` siblings. `_pipelines.py` imports it for its three uses in
  `register_pipeline_tools`: the `auto_subscribe_lineage=` argument and the calls in
  `_run_pipeline` and `_resume_pipeline`. The function is self-contained: it uses a module
  logger and lazy imports of `ChildSessionManager` and `CompletionSubscriberManager`. Its only
  test importers are `tests/events/test_wake_wiring.py` and
  `tests/mcp_proxy/tools/workflows/test_mcp_proxy_tools_workflows_pipelines.py`, and neither
  patches it through `_pipelines`.

**Acceptance:**
- 1.12.1 - The `send_message` and `list_pipeline_executions` descriptions contain "only for debugging". file: `src/gobby/mcp_proxy/tools/agent_messaging.py`.
- 1.12.2 - The `get_skill` and `get_skill_file` descriptions, `src/gobby/mcp_proxy/instructions.py`, `src/gobby/install/shared/prompts/mcp/progressive-discovery.md`, `references/skills/loading.md`, and `references/skills/lifecycle.md` say `brief=false` is only for debugging or skill management (ids, hash, version). file: `src/gobby/mcp_proxy/instructions.py`.
- 1.12.3 - test: `tests/mcp_proxy/test_instructions.py::test_instructions_describe_brief_contract` asserts the server instructions say `brief=false` is only for debugging or skill management.
- 1.12.4 - `src/gobby/mcp_proxy/tools/workflows/_pipeline_lineage.py` holds `_auto_subscribe_lineage`, and `_pipelines.py` is below 850 lines. file: `src/gobby/mcp_proxy/tools/workflows/_pipeline_lineage.py`.

**Verification:** run `tests/mcp_proxy/test_instructions.py`, `tests/events/test_wake_wiring.py`, `tests/mcp_proxy/tools/workflows/`, and the skills and messaging tool tests with `GOBBY_TEST_PROTECT=1`; ruff and mypy on the touched sources.

## P6: Verification
`kind: framing`

### 1.13 Paired brief/full parity check [category: test] (depends: 1.1, 1.2, 1.3, 1.4, 1.5, 1.6, 1.8, 1.9, 1.10, 1.11)
`kind: deliverable`

Targets:
- `tests/mcp_proxy/test_brief_parity.py`

**Research context:**
- Each leaf pins its own exact brief key set. This leaf adds the one cross-tool check the
  per-leaf tests cannot express: for every in-scope tool, brief and full come from the same call
  inputs, and brief keeps the handle and outcome, adds nothing beyond its declared derived
  fields, drops every Decision Record 5 field, and is smaller by a stated bound.
- It runs on isolated fixtures and never against the daemon or the user's database. The
  fixtures are the ones the leaf tests already build: `create_memory_registry(lambda:
  mock_memory_manager)` with `MockMemory` rows (`tests/mcp_proxy/tools/test_memory_tools.py:35`,
  `:115`) for `search_memories`, `get_memory`, and `create_memory`; the same factory with mocked
  task and session managers (`tests/mcp_proxy/tools/test_memory_review.py::_registry`, `:55`)
  for `review_task_memories`; `create_task_registry(mock_task_manager)` with `sample_task`
  (`tests/mcp_proxy/tools/conftest.py:98`, `:106`) for `get_task`; `create_agents_registry`
  over a mock runner with a terminal run built as `_make_mock_agent_run` builds one
  (`tests/mcp_proxy/tools/test_agents.py:59`, `:119`) for `wait_for_agent` and
  `get_agent_result`; `register_crud_tools` over a mock session manager
  (`tests/mcp_proxy/tools/test_sessions_query_tools.py:24`, `:46`) for `get_session`; a
  `SessionManager` over `temp_db` with a staged handoff
  (`tests/sessions/test_handoff_found_work.py:68`, `:109`) for `get_handoff`, staged twice
  with the same `_payload` because the no-argument call consumes it: `set_handoff`, consume
  with `brief=False`, `set_handoff` again, consume with `brief` defaulted; and
  `list_rules(def_manager, ...)` over `temp_db` rules
  (`tests/mcp_proxy/tools/test_rule_tools.py:25-76`) for `list_rules`. `close_task` pairs the
  1.6 projection `brief_close_payload` against a diagnostic-mode `CloseEvaluation.response()`
  literal built from the 1.6 field list with one `failed` gate and the rest `passed`; that the
  wrapper applies the projection is 1.6.1's and 1.6.2's.
- Fixtures are sized to the research note's medians: ten search hits, five similar and five
  review candidates with 600-character bodies, a completed run with a 200-character `result`
  carrying `sandbox`, `capture`, `live_output`, provider and model, counters, timestamps, and
  the notification fields (brief keeps `result` verbatim, so the bound depends on the metadata
  outweighing it), a session with `handoff_markdown`, `summary_markdown`, `original_prompt`,
  `last_assistant_content`, and `terminal_context` populated, a handoff with three found-work
  entries, and twenty rules.
- `temp_db` (`tests/conftest.py:371`) is the isolated test hub, so the two DB-backed cases run
  with `DATABASE_URL` pointed at it like every other storage test.

**Implementation:**
- `PARITY_CASES`: one entry per in-scope tool with `tool`, `build` (returns the callable and
  its call kwargs from the fixtures above), `must_match` (the handle and outcome values, list
  items by id: `success` everywhere; `memories[].id`; `memory.id`, `similar_existing[].id`,
  `auto_superseded`; `candidates[].id`, `source_task_id`, `pending_reviews_complete`; `ref`,
  `id`, `state.is_closed`; `closed`, `can_close`, `error`, `reviewer_run_id`; `run_id`, `status`,
  `result`, `error`, `terminal_reason`; `found`, `claimed_task_refs`, `parent_session_id`,
  `agent_run_id`; `found_work`, `found_work_gate_armed`; the `name` of every listed rule),
  `derived` (keys brief may add: `summary`, `when`, `gates_passed`, `task_ref`, `ref`), and
  `max_ratio`.
- `NEVER_IN_BRIEF`: `seq_num`, `path_cache`, `created_by_agent`, `access_count`,
  `source_type`, `policy_hash`, `transcript_path`, `score_range`, `threshold_axis`,
  `reviewer_provider`, `reviewer_model`, `notification_registered`,
  `notification_session_id`, `checklist`, `transcript_evidence`, `validation_feedback`,
  `sandbox`, `external_write_grant`.
- Measurement envelope: each side is the dict the tool function returns, serialized as
  `json.dumps(value, default=str)`, before the proxy adds `response_time_ms` or an offload
  wrapper; that is the same text `mcpsize.py` measured in transcripts, minus the envelope.
- Size bound: `len(json.dumps(brief, default=str)) <= max_ratio * len(json.dumps(full,
  default=str))`, with `max_ratio = 0.5` where the leaf replaces bodies or lists with index
  lines or counts (`search_memories`, `create_memory`, `review_task_memories`, `close_task`,
  `wait_for_agent`, `get_agent_result`, `get_session`, `list_rules`) and `max_ratio = 0.99`
  where brief only drops duplicates (`get_memory`, `get_task`, `get_handoff`).
- The test calls each tool twice with identical inputs, `brief` defaulted and `brief=False`,
  except `close_task`, whose pair is `brief_close_payload(literal)` against the literal, and
  asserts the four invariants; a second test pins the case table to the in-scope set.

**Acceptance:**
- 1.13.1 - test: `tests/mcp_proxy/test_brief_parity.py::test_brief_projections_are_subsets_and_smaller` is parametrized over `PARITY_CASES`, obtains each pair as the Implementation states (two calls per tool; the projection over the literal for `close_task`), and asserts, per tool, that every `must_match` value is equal in brief and full, every brief key is a full key or a declared derived key, no `NEVER_IN_BRIEF` key appears anywhere in brief, and the size bound holds.
- 1.13.2 - test: `tests/mcp_proxy/test_brief_parity.py::test_parity_cases_cover_every_in_scope_tool` asserts the case table's tool names equal `{search_memories, get_memory, create_memory, review_task_memories, get_task, close_task, wait_for_agent, get_agent_result, get_session, get_handoff, list_rules}`, so a tool added to the brief contract later is a deliberate test edit.

**Verification:** `DATABASE_URL=postgresql://gobby_test:gobby_test@127.0.0.1:60892/gobby_test GOBBY_TEST_PROTECT=1 uv run pytest tests/mcp_proxy/test_brief_parity.py -q`; ruff on the test module.

## V2: Verification
`kind: verification`

Run after every leaf and again before the PD lands the branch: each leaf's own **Verification**
line, then `uv run ruff format --check src/ && uv run ruff check src/ && uv run mypy src/`, then
`uv run gobby plans validate .gobby/plans/mcp-brief-response-contract.md -p /Users/josh/Projects/gobby`.
Do not run the full pytest suite.

Live check, after the PD-owned daemon restart that publishes the change (announced globally
before and after, outside quiet hours): the PD files a direct task (`category: manual`) for the
Researcher or takes it directly. From a fresh session, call each read-only in-scope tool once
with `brief` defaulted: `search_memories` with a fixed query and `limit=10`, `get_memory` on one
returned hit, `get_task` on a known ref, `get_session` on the caller's own ref, `list_rules`, and
`get_agent_result` on an existing terminal run. Measure each result as the character length of
the proxy's JSON result text (the `mcpsize.py` measure), record it beside the baseline median in
`.gobby/plans/research/mcp-output-verbosity-2026-09-25.md`, and judge it by the 1.13 bound: at or
below half the baseline median for the tools that replace bodies or lists, below the baseline for
the tools that only drop duplicates. The mutating and consuming tools (`create_memory`,
`review_task_memories`, `close_task`, `wait_for_agent`, `get_handoff`) are checked only by 1.13
on isolated fixtures, never against live state. A tool over its bound is a bug in its leaf, found
work for the implementing lane, not a plan revision.

Seven-day re-measurement: after seven days of transcripts on the new daemon, rerun the research
note's `mcpsize.py` method and append total MCP output characters and each in-scope tool's
median against the baseline. It is a report, not a gate: teaching-gate reloads (Decision Record
10) and the offload envelope stay outside this plan, so the total is expected to fall by the
in-scope share only.
