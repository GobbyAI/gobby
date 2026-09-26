# Memory Surfacing: Verbatim Retrieval, Fused Ranking, Pushed Memory Index

Plan artifact: `.gobby/plans/memory-surfacing.md`

> **Plan ID:** `memory-surfacing`
>
> **Owner task:** #22410 (becomes the epic root at expansion)
>
> Drafted in plan mode in session gobby#13699 and approved by the user on 2026-09-17. This file
> is the sole authority; validate it with
> `uv run gobby plans validate .gobby/plans/memory-surfacing.md -p .`.

## C1 Context
`kind: framing`

Task #22410 was filed after session gobby#13482 spent about fifteen minutes rediscovering two
facts that memory `002c13ae` already held. Nothing surfaced that memory during the work; it
appeared only at the end, in a duplicate-check search, ranked last of eight for a query naming
its exact subject.

Investigation in session gobby#13699 (2026-09-17) established four things.

1. **The task's premise for item 1 was wrong.** There is no dormant injection mechanism behind a
   flag. Automatic recall injection was deleted on 2026-08-26 (task #21009, commit `8358d7bba6`)
   for measured reasons: usefulness of 7.4% and 20.5% in the two judged cohorts, an LLM
   prompt classifier that cost 4.2 s at p50 and gated nothing, and the user prompt being a poor
   query. `memory.shadow_relevance_judging` only labels search results for a research cohort; it
   never shows anything to an agent. The user has since classified that cohort work as a
   sidequest that is out of scope here.
2. **Agents do not search on their own, and no reminder can fix that.** Searching requires
   suspecting that a memory exists. The session that needed `002c13ae` knew its code well, so
   the constant reminder ("search before touching unfamiliar code") never applied. In this
   session no memory was ever pushed; the one search that happened was caused by a blocking
   directive, and its top hit overturned the task's premise. Knowledge has to be pushed.
3. **Retrieval is degraded in two independent ways.**
   - `gobby-memory:search_memories` never passes `embed_text`, so
     `SearchService.search` (`src/gobby/memory/services/search.py`) runs YAKE keyword
     extraction on every query of six or more words and embeds the keyword bag instead of the
     query. The verbatim path was built for the deleted injection runner and lost its only
     prompt-facing caller.
   - `build_results` (`src/gobby/memory/services/_search_results.py`) sorts by
     `(has_score, undecayed, decayed, ranking_score)`. The fused semantic/keyword/graph score is
     the fourth tiebreak, so a keyword-confirmed hit gains nothing. #21010 made undecayed
     similarity primary to take age off the ranking axis; demoting the fused score was a side
     effect, not a decision.
4. **A 24-query graded cohort (226 hits, three Opus graders plus this session) shows neither
   existing order wins.** Raw similarity puts an A-grade memory at rank 1 in 13 of 22 queries and
   in the top 3 in 19 of 22. The fused score scores 16 of 22 and 17 of 22. Raw similarity buries
   keyword-confirmed answers (the evidence case lands 8th of 8; fused puts it 1st). Fused alone
   buries strong semantic-only answers. The cohort was measured on the degraded YAKE query path
   and its candidate pools were pre-selected by raw similarity, so it cannot pick the final
   policy; it is the fixture that will.

The intended outcome: memory search embeds the query as written and ranks on fused evidence;
Gobby pushes a compact ranked memory index at the moments an agent needs it; and the agent
instruction set tells agents what to do with that index.

## C2 Decision Record
`kind: framing`

| # | Decision | Source |
|---|---|---|
| D1 | Full plan, not a single task | user, 2026-09-17 |
| D2 | Three query signals drive surfacing: user prompts, the agent's own messages, and tool intent | user, 2026-09-17 |
| D3 | Surfacing goes live immediately; no shadow-first gate | user, 2026-09-17 |
| D4 | Ranking work is phase 1 and blocks surfacing | user, 2026-09-17 |
| D5 | Shadow judging, usefulness labels, per-caller cohort reports, and agent-verdict labels are out of scope | user, 2026-09-17 |
| D6 | Delivery is a ranked one-line index with an instruction to pull full text through `gobby-memory:get_memory`; full memory bodies are never injected | user proposal, 2026-09-17, accepted |
| D7 | The index shows rank, type, which searches found the hit, a content lead, and the rationale as a `when:` clause. Compact text lines, not JSON. Raw scores are omitted: the band is too narrow for an agent to read | recommendation in session; confirm at approval |
| D8 | Memories get no `name` column in this plan. The rationale is the teaser. Revisit only if agents still skip the fetch | recommendation in session; confirm at approval |
| D9 | YAKE is removed from memory search rather than bypassed per caller. `extract_keywords` has one production consumer, and 0.5.0 carries no compatibility burden | recommendation; confirm at approval |
| D10 | No LLM classifier anywhere on the surfacing path. Prompt triage is the deleted runner's cheap heuristics, recovered from git | follows D3 and the #21009 evidence |
| D11 | One search per turn start. A substantive prompt is the query; when the prompt is an acknowledgment or continuation, the previous turn's final assistant text is the query instead | recommendation; keeps hook latency to one search |
| D12 | The old item 4 (a blocking `require-memory-search` gate on `spawn_agent`) is dropped. Pushing the index before the spawn supersedes it | follows D2 |
| D13 | Adding `rationale` to the BM25 index is not in this plan. It costs a schema migration and a daemon cutover, and nothing measured yet attributes misses to it. The post-fix cohort in 1.2 is the evidence that would justify it | recommendation; confirm at approval |
| D14 | The sort policy is chosen by a stated rule over a closed candidate set, measured on the cohort re-collected after 1.1 is live | follows D4 |

## C3 Constraints
`kind: framing`

- No backward compatibility (0.5.0 unshipped). Retired rule names go to `RETIRED_RULES`; removed
  code is deleted, not flagged off.
- The three retired rule names `memory-recall-on-prompt`, `require-memory-recall-before-tool`,
  and `require-memory-recall-before-turn-end` stay retired. New rules use new names.
- Every new failure on the surfacing path degrades to injecting nothing. Surfacing never blocks
  a tool call or a turn.
- Claude Code's `Stop` hook carries no `additionalContext`
  (`src/gobby/adapters/claude_contract.py`), so nothing can be delivered at `turn_end`. The
  agent-message signal is therefore read at the next `turn_start`.
- Hook payloads carry no assistant text. The only source is `sessions.last_assistant_content`,
  the trailing 500 characters written by `compute_message_stats`
  (`src/gobby/sessions/message_stats.py`) on a 5-second transcript poll. That is fresh enough
  at `turn_start`, where a human reply has elapsed, and this plan does not change the poll or
  the 500-character clamp.
- Since #22481, hook context ships as labeled contributors and an oversized contributor is
  dropped whole. The memory index is one contributor of at most about 2,000 characters, so it
  can lose only itself. The overflow queue the old path needed is not rebuilt.
- The inline `mcp_call` path (`inject_result: true`, `background: false`) in
  `src/gobby/workflows/engine/effects.py` formats memory-backed results through
  `_MEMORY_RESULT_FORMATTERS` in `src/gobby/workflows/engine/delivery_formatting.py`. Only
  review-learning tools are registered there today; an unregistered tool's result is dumped as
  raw JSON and gets no per-session de-duplication.
- `injected_memory_ids` is still reset on `pre_compact` and on session start after clear,
  compact, or resume, and nothing owns it on the inline path. The new path adopts it.
- Web chat parity is out of scope. `docs/reviews/hooks.md` records that the web-chat lifecycle
  drops `inject_result`; whether #22481 changed that is unverified.
- Production files stay under 1,000 lines. Sizes at planning time:
  `mcp_proxy/tools/memory.py` 760, `memory/services/search.py` 521,
  `memory/services/_search_results.py` 196, `workflows/engine/delivery_formatting.py` 79,
  `workflows/engine/injection_tracking.py` 61, `workflows/safe_evaluator.py` 792,
  `memory/recall_fit.py` **1,018** (already over; see 1.3).
- Deployment is Python-only. Each phase is live after merge to `0.5.0` and a daemon restart from
  the main checkout, announced with a `global` `send_message` and timed for a window with no
  live spawned worker or close validator. Rule template changes reach the DB through startup
  sync; verify the installed rows, not the YAML.

## P1: Retrieval Quality
`kind: framing`

**Goal**: the query is embedded as written, results are ordered on fused evidence chosen by
measurement, and the offline replay models the same order as live search.

### 1.1 Embed memory search queries verbatim [category: code]
`kind: deliverable`

Targets:
- `src/gobby/memory/services/search.py::*` — scope-reason: remove the YAKE branch and its import from SearchService.search and update the docstring that describes it
- `src/gobby/search/keywords.py::*` — operation: delete — scope-reason: retire the entire file; its only production consumer is removed here
- `tests/search/test_keywords.py::*` — operation: delete — scope-reason: retire the entire file with the module it tests
- `pyproject.toml`
- `uv.lock`
- `tests/memory/test_search_ranking.py::*` — scope-reason: cases that assert the YAKE-derived embedding change to assert verbatim embedding
- `tests/mcp_proxy/tools/test_memory_review.py::*` — scope-reason: references extract_keywords; align with the removed module
- `tests/memory/test_recall_benchmark_e2e.py::*` — scope-reason: references extract_keywords; align with the removed module
- `docs/guides/memory.md`

**Research context:**

- Observed: `SearchService.search` computes `embed_query = embed_text` when the caller supplies
  it; otherwise it runs `extract_keywords(query)` off-thread and embeds `extracted or query`.
  `extract_keywords` (`src/gobby/search/keywords.py`) returns a keyword string of at most ten
  terms when the query has six or more words and YAKE shrinks it below 70% of its length.
- Observed callers that pass `embed_text`: `review_task_memories`
  (`src/gobby/mcp_proxy/tools/memory_review.py`) and the `similar_existing` probe in
  `src/gobby/mcp_proxy/tools/memory_write.py`. The MCP `search_memories` tool, the CLI `recall`
  command, and the HTTP search route do not, so all three embed keyword bags today.
- Observed: `gcode grep -w extract_keywords src/gobby` finds one consumer outside the module,
  `src/gobby/memory/services/search.py`. `yake>=0.7.3` is declared in `pyproject.toml`. Confirm
  with `gcode grep -i -w yake src/` before deleting; if another importer exists, keep the module
  and remove only the search call.
- The keyword (BM25) search receives `query` unchanged and is unaffected.
- Approach: embed `embed_text or query`. Keep the `embed_text` parameter, because
  `similar_existing` legitimately embeds content plus rationale while searching keywords on
  content. Delete `src/gobby/search/keywords.py` and `tests/search/test_keywords.py`, and remove
  the `yake` dependency from `pyproject.toml` and `uv.lock` with `uv remove yake`.
- Rejected: passing `embed_text=query` from each caller. It leaves the degraded default in
  place for the next caller, which is how this regression happened.
- Update the search description in `docs/guides/memory.md` wherever it describes keyword
  extraction of queries.
- After this lands and the daemon restarts, run `gobby memory reindex-embeddings` once.
  #21010 changed document embeddings to content plus rationale with no reindex in the commit, so
  rows last embedded before 2026-08-26 may still carry content-only vectors. This is an
  operation, recorded in Rollout, not a code change.
- Planned checks (not yet run):
  `DATABASE_URL=postgresql://gobby_test:gobby_test@127.0.0.1:60892/gobby_test GOBBY_TEST_PROTECT=1 uv run pytest tests/memory/test_search_ranking.py tests/mcp_proxy/tools/test_memory_review.py -q`,
  `uv run ruff check src/`, `uv run mypy src/`.

**Acceptance:**

- 1.1.1 - A query of six or more words reaches the embedding function unchanged when no `embed_text` is supplied. test: `tests/memory/test_search_ranking.py::test_query_is_embedded_verbatim`.
- 1.1.2 - A caller-supplied `embed_text` is still embedded in place of the query. test: `tests/memory/test_search_ranking.py::test_embed_text_overrides_query_for_embedding`.
- 1.1.3 - The keyword extraction module and its test file are gone and nothing imports them. file: `src/gobby/search/keywords.py`.
- 1.1.4 - `yake` is absent from the declared dependencies and the lockfile. file: `pyproject.toml`.
- 1.1.5 - The memory guide no longer describes keyword extraction of search queries. file: `docs/guides/memory.md`.

### 1.2 Graded ranking fixture and fused sort policy [category: code] (depends: 1.1)
`kind: deliverable`

Targets:
- `src/gobby/memory/services/_search_ranking.py`
- `src/gobby/memory/services/_search_results.py::build_results`
- `src/gobby/memory/services/search.py::*` — scope-reason: consumer sweep only; SearchService forwards to build_results and is expected to need no edit
- `src/gobby/mcp_proxy/tools/memory.py::*` — scope-reason: the search_memories tool description states the primary sort key and must match the shipped policy
- `tests/memory/fixtures/ranking_cohort.json`
- `tests/memory/test_ranking_policy.py`
- `tests/memory/test_search_ranking.py::*` — scope-reason: ordering cases pinned to undecayed-primary change to the shipped policy
- `tests/memory/test_manager_graph_search.py::*` — scope-reason: ordering cases pinned to undecayed-primary change to the shipped policy
- `tests/mcp_proxy/tools/test_memory_tools.py::*` — scope-reason: tool-payload ordering and description cases follow the shipped policy
- `docs/guides/memory.md`

**Research context:**

- Observed: `build_results` sorts `scored` with the key
  `(similarity is not None, undecayed, similarity, ranking_score)`, then runs
  `collapse_near_duplicates` and cuts to `limit`. `ranking_score` is the RRF value
  `sum(1 / (k + rank + 1))` over the semantic, keyword, and graph searches with `k = 60`, so a
  hit found by one search tops out near 0.0164 and a hit found by all three near 0.0492.
- Observed baseline on the planning cohort (Appendix A1; 24 queries, 22 with an A-grade hit):
  raw similarity A@1 13/22, A@3 19/22; fused score A@1 16/22, A@3 17/22. Raw similarity misses
  the top 3 on H1, H7, M1. Fused misses on H1, H7, T4, I1, I4. H7's only A-grade hit has the
  lowest raw similarity in its pool and was found by one search; no sort rescues it.
- The planning cohort is biased toward raw similarity: pools were the live top 10 by raw
  similarity and graders saw hits in that order. It was also measured on YAKE-bagged query
  embeddings. It supplies the queries, needs, and grades; the scores must be re-collected.
- Approach:
  1. Add `result_sort_key` as a pure function in the new module
     `src/gobby/memory/services/_search_ranking.py`. It takes undecayed similarity, fused score,
     decayed similarity, and has-score, and returns the tuple `build_results` sorts on.
     `build_results` calls it; no other ordering logic stays inline.
  2. After 1.1 is merged and live, re-run the 24 Appendix A1 queries through
     `gobby-memory:search_memories` with `limit=20` and record every hit's id, undecayed
     similarity, fused score, decayed similarity, and `search_via` in
     `tests/memory/fixtures/ranking_cohort.json` together with each query's need and style.
     Grades attach to `(query, memory id)`, so the Appendix A1 grades carry over. Grade any hit
     not in Appendix A1 against the recorded need on the same A to D scale before looking at its
     scores, and record the grader as `session`.
  3. `tests/memory/test_ranking_policy.py` evaluates this closed candidate set on the fixture:
     `cosine` (the current key); `fused` (fused score, raw similarity as tiebreak);
     `blend-0.1`, `blend-0.2`, `blend-0.3` (raw similarity plus lambda times fused score divided
     by 0.0492); and `interleave` (alternate the two orders, de-duplicated).
  4. Selection rule, applied once and recorded in the fixture's `policy` field: highest A@3;
     ties broken by highest A@1, then highest nDCG@5 with gains A=3, B=1, C=D=0, then the
     earliest entry in the order listed above. `result_sort_key` implements the selected policy.
  5. Record `surface_min_score` in the fixture: the largest value, rounded down to two decimals,
     at which at least 90% of A-grade hits in the re-collected cohort have undecayed similarity
     at or above it. Deliverable 2.1 consumes it.
- Executed differently, recorded 2026-09-17 in session gobby#13699. The Acceptance lines and
  the manifest keep their approved wording; tasks #22490 and #22491 carry the updated criteria.
  - The rule selected `interleave`, which depends on each hit's rank in two orders and so cannot
    be a per-hit key. Step 1's `result_sort_key` shipped as the list-level
    `order_results(hits, scores)` with the `HitScores` tuple, in the same module. Criteria 1.2.1
    and 1.3.2 name that symbol, and 1.3's replay orders rows through it.
  - Step 2's pool was unfaithful as written. `build_results` orders the whole merged pool (75 to
    108 ids at `limit=20`) and the tool returns only its top 20, so a fused-aware candidate can
    lift a hit the tool never returned. Each query's fixture pool is therefore the returned top
    20 plus every other pool member some candidate could lift into a top 5. Pool membership and
    fused scores come from the recall signal log entry of the same search; a missing similarity
    comes from the same query re-run with `tags_all` set to that memory's tags. The fixture's
    `source` field states this.
  - The fused score depends on `limit` (each search fetches `2 * limit`); similarity does not.
    The rule was applied at `limit=20` as planned. `interleave` also holds A@3 20 of 24 at
    limits 10 and 5.
  - New hits were graded by Opus subagents blind to scores (grader `opus-subagent`), not by this
    session. 46 hidden re-grades of Appendix A1 pairs agreed on A versus not-A in 39 cases, all
    differences one grade apart.
- Decay stays off the primary ordering under every candidate, which preserves the #21010
  decision. `min_score` stays on the undecayed axis.
- Update memory `d2ae6cc2` (the search score contract) through `gobby-memory:update_memory` once
  the policy ships; it currently states that undecayed similarity is the primary sort key.
- Rejected: fitting lambda continuously. With 22 scored queries that is curve-fitting; a
  three-point grid with a stated tiebreak is the ceiling the data supports.
- Planned checks (not yet run):
  `DATABASE_URL=postgresql://gobby_test:gobby_test@127.0.0.1:60892/gobby_test GOBBY_TEST_PROTECT=1 uv run pytest tests/memory/test_ranking_policy.py tests/memory/test_search_ranking.py tests/memory/test_manager_graph_search.py tests/mcp_proxy/tools/test_memory_tools.py -q`.

**Acceptance:**

- 1.2.1 - `build_results` orders hits only through `result_sort_key`. symbol: `result_sort_key`. file: `src/gobby/memory/services/_search_ranking.py`.
- 1.2.2 - The fixture holds all 24 cohort queries with needs, styles, graded hits, re-collected scores, the selected `policy`, and `surface_min_score`. file: `tests/memory/fixtures/ranking_cohort.json`.
- 1.2.3 - The shipped policy equals the selection rule's winner on the fixture. test: `tests/memory/test_ranking_policy.py::test_shipped_policy_is_selection_rule_winner`.
- 1.2.4 - The shipped policy's A@3 and A@1 on the fixture are each at least the `cosine` candidate's. test: `tests/memory/test_ranking_policy.py::test_shipped_policy_does_not_regress_cosine`.
- 1.2.5 - Query M1 (the #22410 evidence query) returns memory `002c13ae` in the top 3 under the shipped policy. test: `tests/memory/test_ranking_policy.py::test_evidence_memory_reaches_top_three`.
- 1.2.6 - The `search_memories` tool description and the memory guide state the shipped ordering. file: `docs/guides/memory.md`.

### 1.3 Share the sort key with recall replay and split recall_fit [category: refactor] (depends: 1.2)
`kind: deliverable`

Targets:
- `src/gobby/memory/recall_fit.py::*` — scope-reason: move the replay row model and replayed_* helpers out and re-import them so the file drops under the ceiling
- `src/gobby/memory/recall_replay.py`
- `tests/memory/test_recall_fit.py::*` — scope-reason: the ordering case that asserts decayed-primary replay changes to the shared key

**Research context:**

- Found work, recorded during planning: `src/gobby/memory/recall_fit.py` is 1,018 lines, over
  the 1,000-line ceiling, and `replayed_sort_key` still models the pre-#21010 order (decayed
  similarity primary, fused score as tiebreak) while its docstring claims to mirror
  `build_results`. `tests/memory/test_recall_fit.py` pins that stale order. The offline replay,
  fit, and ship gate therefore model a ranking that no longer exists, and 1.2 would widen the
  gap.
- Approach: split the replay model (`ReplayRow`, `ReplayParams`, `replay_row_from_signal_row`,
  `_replayed_decay`, `_replayed_edge_blend_ratio`, `replayed_similarity`,
  `_replayed_graph_synthetic`, `replayed_sort_key`) out of `recall_fit.py` and move it into the
  new module `src/gobby/memory/recall_replay.py`. `replayed_sort_key` delegates to
  `result_sort_key` from 1.2 so live and replayed order cannot drift again.
- Sweep consumers before moving: `gcode grep -w replayed_sort_key src tests`,
  `gcode grep -w ReplayRow src tests`, `gcode grep -w replayed_similarity src tests`. Expected
  importers include `src/gobby/memory/recall_refit.py`, `src/gobby/cli/memory/signals.py`, and
  `src/gobby/memory/recall_ship_gate_run.py`; update imports to the new module rather than
  re-exporting from `recall_fit.py`.
- This deliverable changes no fitting behavior beyond the ordering fix. The research cohort
  itself stays out of scope (D5).
- Planned checks (not yet run):
  `DATABASE_URL=postgresql://gobby_test:gobby_test@127.0.0.1:60892/gobby_test GOBBY_TEST_PROTECT=1 uv run pytest tests/memory/test_recall_fit.py tests/memory/test_recall_benchmark.py -q`,
  `uv run mypy src/`.

**Acceptance:**

- 1.3.1 - `recall_fit.py` is under 1,000 lines. file: `src/gobby/memory/recall_fit.py`.
- 1.3.2 - `replayed_sort_key` delegates to `result_sort_key`. symbol: `replayed_sort_key`. file: `src/gobby/memory/recall_replay.py`.
- 1.3.3 - Replay ordering matches live ordering for the same score inputs. test: `tests/memory/test_recall_fit.py::test_replayed_order_matches_live_sort_key`.

## P2: Surfacing (depends: P1)
`kind: framing`

**Goal**: at the moments named in D2, Gobby pushes a ranked one-line memory index into agent
context, de-duplicated per context epoch, able to push nothing.

Index format (one contributor, labeled `mcp:gobby-memory/surface_memories`):

```text
<memory-index trigger="spawn_agent">
1. 002c13ae [pattern; semantic+keyword+graph; updated 2026-09-11] Codex installs a matcherless native SessionEnd hook: ghook --gobby-owned ... | when: changing Codex hooks or agent termination
2. ...
Fetch full text before acting on a match: gobby-memory:get_memory(memory_id="<id>")
</memory-index>
```

### 2.1 surface_memories tool and memory-index delivery [category: code]
`kind: deliverable`

Targets:
- `src/gobby/mcp_proxy/tools/memory_surface.py`
- `src/gobby/memory/surface_format.py`
- `src/gobby/mcp_proxy/tools/memory.py::*` — scope-reason: create_memory_registry registers the new tool module beside register_memory_review_tools
- `src/gobby/workflows/engine/delivery_formatting.py::*` — scope-reason: register the surface_memories formatter in _MEMORY_RESULT_FORMATTERS and add its formatter method
- `src/gobby/workflows/engine/injection_tracking.py::*` — scope-reason: add the injected_memory_ids filter-and-stage method beside the review-lesson one
- `tests/mcp_proxy/tools/test_memory_surface.py`
- `tests/workflows/test_memory_index_delivery.py`
- `docs/guides/memory.md`
- `docs/reference-audit/memory.json::*` — scope-reason: the audit is one JSON document; add the surface_memories tool entry

**Research context:**

- Pattern to copy end to end is review-lesson injection: rule
  `review-learning/inject-review-lessons-for-touched-files.yaml` fires an inline `mcp_call`;
  `effects.py` awaits the dispatcher; `DeliveryFormattingMixin._format_memory_backed_result`
  routes by `(server, tool)` through `_MEMORY_RESULT_FORMATTERS`;
  `InjectionTrackingMixin._filter_and_track_new_review_lessons` filters against committed and
  staged ids and stages the new ones; `_is_empty_inject_payload` suppresses an empty result; the
  text lands as a labeled contributor.
- New tool `gobby-memory:surface_memories(text, trigger, session_id)` lives in the new module
  `src/gobby/mcp_proxy/tools/memory_surface.py` and registers through a
  `register_memory_surface_tools(registry, memory_manager, *, session_manager)` function shaped
  like `register_memory_review_tools`. It calls `manager.search_memories` with
  `query=text`, `embed_text=text` (both truncated to 2,000 characters), `limit=5`,
  `tags_none=["review-lesson"]`, `min_score=SURFACE_MIN_SCORE`, `caller="memory.surface"`, the
  caller's `session_id`, and a fresh `recall_request_id`. It returns
  `{"trigger": ..., "count": n, "memories": [...]}` where each entry carries id, type,
  `search_via`, `updated_at`, content, and rationale. Any exception returns
  `{"count": 0, "memories": []}` and logs at warning level.
- `SURFACE_MIN_SCORE` is defined in `memory_surface.py` and must equal `surface_min_score` in
  `tests/memory/fixtures/ranking_cohort.json` from 1.2.
- `trigger` is one of `turn`, `spawn_agent`, `task`, `handoff`. In this deliverable all four
  pass `text` through unchanged; 2.3 adds the `turn` triage.
- `caller="memory.surface"` keeps automated surfacing out of the agent-search research cohort,
  because `SHADOW_ELIGIBLE_CALLERS` (`src/gobby/storage/recall_shadow_signals.py`) does not list
  it. This plan does not add it.
- Review lessons are excluded because the graded cohort showed them swamping queries written in
  generic engineering prose (six of ten hits on one query, none relevant), and they already have
  their own injection path.
- Renderer `format_memory_index(trigger, memories)` lives in the new module
  `src/gobby/memory/surface_format.py`. Each line: rank, first eight id characters, type,
  `search_via` with `|` rendered as `+`, update date, the first 160 characters of content on
  one line, and `| when: ` plus the first 160 characters of rationale when one exists. No
  scores (D7). The block ends with the `get_memory` instruction.
- Add `_filter_and_track_new_memories` to `InjectionTrackingMixin`, reading and staging
  `injected_memory_ids` exactly as the review-lesson method handles
  `injected_review_lesson_ids`. Register `("gobby-memory", "surface_memories")` in
  `_MEMORY_RESULT_FORMATTERS` with a formatter that filters, returns nothing when no new hit
  remains, and otherwise renders the index.
- `docs/reference-audit/memory.json` lists every `gobby-memory` tool with its symbol; add the
  new tool there and in the tool table of `docs/guides/memory.md`.
- Rejected: reusing `search_memories` directly from rules. Its results would be recorded under
  the agent-search caller, the turn trigger needs server-side triage anyway, and one tool keeps
  the cap, floor, and exclusions in one place.
- Rejected: rebuilding the old overflow queue and `get_recall_memories`. The contributor model
  from #22481 removed the reason it existed.
- Planned checks (not yet run):
  `DATABASE_URL=postgresql://gobby_test:gobby_test@127.0.0.1:60892/gobby_test GOBBY_TEST_PROTECT=1 uv run pytest tests/mcp_proxy/tools/test_memory_surface.py tests/workflows/test_memory_index_delivery.py tests/skills -q`.

**Granularity:** one outcome: a callable tool whose inline result reaches agent context as a
de-duplicated index. The tool, renderer, and formatter registration are not independently
useful, so they stay in one leaf.

**Acceptance:**

- 2.1.1 - `surface_memories` searches with the text embedded verbatim, a limit of 5, review lessons excluded, the fixture floor, and caller `memory.surface`. test: `tests/mcp_proxy/tools/test_memory_surface.py::test_surface_search_arguments`.
- 2.1.2 - A search failure returns an empty result and raises nothing. test: `tests/mcp_proxy/tools/test_memory_surface.py::test_surface_fails_open`.
- 2.1.3 - `SURFACE_MIN_SCORE` equals the fixture's `surface_min_score`. test: `tests/mcp_proxy/tools/test_memory_surface.py::test_floor_matches_ranking_fixture`.
- 2.1.4 - The rendered index carries rank, id, type, searches, date, content lead, and the `when:` clause, no scores, and the `get_memory` instruction. test: `tests/workflows/test_memory_index_delivery.py::test_index_line_format`.
- 2.1.5 - A memory already in `injected_memory_ids` is not shown again, and newly shown ids are staged. test: `tests/workflows/test_memory_index_delivery.py::test_index_dedupes_and_stages_ids`.
- 2.1.6 - When every hit is filtered out, nothing is injected. test: `tests/workflows/test_memory_index_delivery.py::test_empty_index_injects_nothing`.
- 2.1.7 - The tool is listed in the reference audit and the memory guide. file: `docs/reference-audit/memory.json`.

### 2.2 Tool-intent surfacing rules [category: code] (depends: 2.1)
`kind: deliverable`

Targets:
- `src/gobby/install/shared/workflows/rules/memory-lifecycle/surface-memories-on-tool-intent.yaml`
- `src/gobby/install/shared/workflows/rules/memory-lifecycle/search-memories-on-claim.yaml::*` — operation: delete — scope-reason: retire the entire file; the nudge is replaced by real surfacing
- `src/gobby/mcp_proxy/tools/tasks/_lifecycle_claim.py::*` — scope-reason: claim_task is a closure inside register_claim_task; its success payload gains the task title
- `tests/workflows/test_memory_lifecycle_rules.py::*` — scope-reason: replace the claim-nudge cases with the four tool-intent rule cases
- `tests/workflows/test_retired_bundled_definitions.py::*` — scope-reason: add search-memories-on-claim to RETIRED_RULES
- `src/gobby/install/shared/workflows/rules/AGENTS.md`
- `docs/guides/memory.md`

**Research context:**

- Observed: for a proxied call, rule conditions and templates see the inner arguments as
  `tool_input` with `server_name` and `tool_name` overlaid, and `event.data` carries
  `mcp_server` and `mcp_tool`. `spawn_agent` takes `prompt`; `create_task` takes `title` and
  `claim`; `claim_task` takes only `task_id` and `force` and returns only `success` and
  `task_id`; `get_handoff` returns the text as `tool_output["handoff"]`, or under
  `tool_output["result"]` through the proxy wrapper.
- Observed defect: `search-memories-on-claim.yaml` interpolates `tool_input.get('title')`, which
  is always empty for `claim_task`, so its nudge prints the literal placeholder.
- `PreToolUse` and `PostToolUse` both carry `additionalContext` on Claude Code, so both
  `before_tool` and `after_tool` rules deliver in the same turn.
- Four rules in the new file, each one inline `mcp_call` to `gobby-memory/surface_memories` with
  `background: false` and `inject_result: true`, and none using `block_on_failure` or
  `block_on_success`:
  - `surface-memories-before-spawn`: `before_tool`, `spawn_agent`, text from
    `tool_input.get('prompt')`, trigger `spawn_agent`.
  - `surface-memories-before-claiming-create`: `before_tool`, `create_task` with a truthy
    `claim`, text from `tool_input.get('title')`, trigger `task`.
  - `surface-memories-after-claim`: `after_tool`, `claim_task` whose returned id is in
    `variables.claimed_tasks` (reuse the existing rule's both-shapes condition), text from the
    returned title, trigger `task`.
  - `surface-memories-after-handoff`: `after_tool`, `get_handoff`, text from the returned
    handoff, trigger `handoff`.
- Add `title` to the `claim_task` success payload in
  `src/gobby/mcp_proxy/tools/tasks/_lifecycle_claim.py`; find its test module with
  `gcode grep -F "claim_task" tests/mcp_proxy -m 20` and extend the success-payload case.
- Delete `search-memories-on-claim.yaml` and add `search-memories-on-claim` to `RETIRED_RULES`
  in `tests/workflows/test_retired_bundled_definitions.py`.
- The `memory-lifecycle` row of the table in
  `src/gobby/install/shared/workflows/rules/AGENTS.md` goes from 9 rules to 12 (minus one, plus
  four); update its Purpose text and the bundled-rules table in `docs/guides/memory.md`.
- After deploy, confirm the four rows are installed and enabled and the retired row is
  soft-deleted with `uv run gobby rules list`; the template is not the live state.
- Planned checks (not yet run):
  `DATABASE_URL=postgresql://gobby_test:gobby_test@127.0.0.1:60892/gobby_test GOBBY_TEST_PROTECT=1 uv run pytest tests/workflows/test_memory_lifecycle_rules.py tests/workflows/test_retired_bundled_definitions.py tests/workflows/test_worker_safety_rules.py -q`.

**Acceptance:**

- 2.2.1 - Each of the four rules fires on its tool and event, passes the stated text and trigger, and injects inline. test: `tests/workflows/test_memory_lifecycle_rules.py::TestToolIntentSurfacing`.
- 2.2.2 - `claim_task` returns the claimed task's title. file: `src/gobby/mcp_proxy/tools/tasks/_lifecycle_claim.py`.
- 2.2.3 - `search-memories-on-claim` is retired and absent from bundled templates. test: `tests/workflows/test_retired_bundled_definitions.py::test_retired_rules_are_absent_from_bundled_templates`.
- 2.2.4 - The rule-group table and the memory guide list the new rules and the new count. file: `src/gobby/install/shared/workflows/rules/AGENTS.md`.

### 2.3 Turn-start surfacing from the prompt or the agent's last message [category: code] (depends: 2.2)
`kind: deliverable`

Targets:
- `src/gobby/memory/prompt_triage.py`
- `src/gobby/mcp_proxy/tools/memory_surface.py`
- `src/gobby/install/shared/workflows/rules/memory-lifecycle/surface-memories-on-turn-start.yaml`
- `tests/memory/test_prompt_triage.py`
- `tests/mcp_proxy/tools/test_memory_surface.py`
- `tests/workflows/test_memory_lifecycle_rules.py::*` — scope-reason: add the turn-start rule cases
- `src/gobby/install/shared/workflows/rules/AGENTS.md`
- `docs/guides/memory.md`

**Research context:**

- The deleted runner's `_hard_skip_reason`
  (`git show 8358d7bba6^:src/gobby/memory/recall.py`) is the cheap triage D10 asks for. Recover
  its text heuristics into the new module `src/gobby/memory/prompt_triage.py` as
  `is_substantive_prompt(text) -> bool`: empty prompt, the acknowledgment word set,
  continuation, wait, the status-question pattern, slash and skill-load commands, and short
  lifecycle commands all return false. Session-state checks from the old ladder stay in the
  rule's `when`, not in this function.
- `surface_memories` with trigger `turn`: if `is_substantive_prompt(text)`, search on the
  prompt. Otherwise read `last_assistant_content` for the session through the registry's
  `session_manager` (the pattern `_assistant_message` uses in
  `src/gobby/workflows/found_work_gate.py`) and search on that; if it is empty, return the empty
  result. This is D11: an "ok, go" turn is exactly where the agent's previous message states
  the intent.
- The assistant text is the trailing 500 characters and may be up to five seconds stale; both
  are acceptable at `turn_start` and neither is changed here.
- Rule `surface-memories-on-turn-start` in the new file: `event: turn_start`, parent sessions
  only (`not variables.get('is_spawned_agent')`), `parent_turn_seq` present, and a once-per-turn
  guard `variables.get('_memory_surface_turn_seq') != variables.get('parent_turn_seq')` set with
  `delivery: on_receipt`, mirroring the deleted `memory-recall-on-prompt` rule. The text
  argument is `event.data.get('prompt') or variables.get('_current_user_prompt') or ''`
  (`capture_turn_prompt` already writes that variable). Priority 12, after
  `increment-parent-turn-seq` (priority 1).
- `turn_start` maps to `BEFORE_AGENT`, which is Claude Code's `UserPromptSubmit` and carries
  `additionalContext`.
- The `memory-lifecycle` row in `src/gobby/install/shared/workflows/rules/AGENTS.md` goes from
  12 to 13; add the rule to the table in `docs/guides/memory.md`.
- Latency: one inline search per parent turn. Observed search times in planning were 0.5 to
  2.6 seconds. No second search is added for the agent message (D11).
- Planned checks (not yet run):
  `DATABASE_URL=postgresql://gobby_test:gobby_test@127.0.0.1:60892/gobby_test GOBBY_TEST_PROTECT=1 uv run pytest tests/memory/test_prompt_triage.py tests/mcp_proxy/tools/test_memory_surface.py tests/workflows/test_memory_lifecycle_rules.py -q`.

**Acceptance:**

- 2.3.1 - Acknowledgments, continuations, status questions, slash commands, and short lifecycle commands are non-substantive; a task-describing prompt is substantive. test: `tests/memory/test_prompt_triage.py::test_triage_matrix`.
- 2.3.2 - The turn trigger searches on a substantive prompt. test: `tests/mcp_proxy/tools/test_memory_surface.py::test_turn_trigger_uses_substantive_prompt`.
- 2.3.3 - The turn trigger searches on the session's last assistant text when the prompt is non-substantive, and returns empty when there is none. test: `tests/mcp_proxy/tools/test_memory_surface.py::test_turn_trigger_falls_back_to_assistant_text`.
- 2.3.4 - The rule fires once per parent turn, never for spawned agents, and commits its guard on receipt. test: `tests/workflows/test_memory_lifecycle_rules.py::TestTurnStartSurfacing`.
- 2.3.5 - The rule-group table and the memory guide list the rule and the new count. file: `src/gobby/install/shared/workflows/rules/AGENTS.md`.

## P3: Guidance (depends: P2)
`kind: framing`

**Goal**: the instruction set agents reliably follow tells them what the index is and what to do
with it, and the constant reminder that carried no information is gone.

### 3.1 Working Rule 14 and retirement of the constant reminder [category: docs] (depends: 2.3)
`kind: deliverable`

Targets:
- `AGENTS.md`
- `src/gobby/install/shared/workflows/rules/memory-lifecycle/layered-memory-guidance.yaml::*` — scope-reason: remove the later-turn reminder rule and keep the initial-stop gate in the same document
- `tests/workflows/test_memory_lifecycle_rules.py::*` — scope-reason: drop the later-turn reminder cases; keep the initial-stop gate cases
- `tests/workflows/test_retired_bundled_definitions.py::*` — scope-reason: add remind-memory-guidance-on-later-turns to RETIRED_RULES
- `src/gobby/install/shared/workflows/rules/AGENTS.md`
- `src/gobby/install/shared/skills/gobby/references/memory/overview.md`
- `src/gobby/install/shared/skills/gobby/references/memory/search.md`
- `docs/guides/memory.md`
- `docs/reference-audit/memory.json::*` — scope-reason: the audit is one JSON document; update the renamed retrieval anchor
- `docs/contracts/memory-usefulness-label.md`

**Research context:**

- Observed: `AGENTS.md` Working Rules are numbered 1 to 13, and no test asserts the count or
  mirrors the text. The four tests that read the root `AGENTS.md` check the Plans section, the
  progressive-discovery wording, and four wait-guidance phrases. Adding rule 14 is a single-file
  edit.
- Rule text to add, in the voice of rules 9 and 12:
  "14. Memory is pushed as an index; read it and pull what applies. At turn start, before
  `spawn_agent`, on task claim, and on handoff resume, Gobby may inject a `<memory-index>` of
  ranked one-line hits. Each line ends in a `when:` clause. Fetch any hit whose clause matches
  your situation with `gobby-memory:get_memory` before acting, even when the code is familiar:
  what pays off is usually a prior decision or an observed runtime behavior rather than code.
  Search yourself with `gobby-memory:search_memories` before characterizing provider or runtime
  behavior and before recording a finding. Record durable knowledge with a rationale written as
  the `when:` clause a future session would match; most turns need no write."
- The "even when the code is familiar" clause is load-bearing: it closes the exemption session
  gobby#13482 granted itself.
- Remove the rule `remind-memory-guidance-on-later-turns` from `layered-memory-guidance.yaml`
  and keep `check-memory-guidance-on-initial-stop`. Add the removed name to `RETIRED_RULES`.
  `TestLayeredMemoryGuidance` in `tests/workflows/test_memory_lifecycle_rules.py` asserts the
  exact reminder string and both `on_receipt` effects; delete those cases. The
  `"Memory reminder."` fixtures in `tests/workflows/test_hooks.py` and
  `tests/workflows/test_rule_engine.py` are self-contained templates and need no change.
- The `memory-lifecycle` row in `src/gobby/install/shared/workflows/rules/AGENTS.md` goes from
  13 to 12.
- `docs/guides/memory.md` states twice that no rule injects memories and has a
  `## Retrieval Is Agent-Driven` section whose anchor is enforced through
  `docs/reference-audit/memory.json` (`tests/skills/reference_library_helpers.py` fails on a
  missing audited anchor). Rename the section to `## Retrieval: Pushed Index and Agent Search`,
  rewrite it to describe both paths, and update the audited anchor in the same change.
- `docs/contracts/memory-usefulness-label.md` says the live cohort is agent-driven search only.
  Add one sentence: surfacing uses caller `memory.surface`, which is not shadow-eligible.
- Update the bundled `gobby` skill's memory `overview.md` and `search.md` references (full
  paths in Targets) to describe the index and the `when:`-style rationale, and refresh their
  `_Last verified_` dates.
- Planned checks (not yet run):
  `DATABASE_URL=postgresql://gobby_test:gobby_test@127.0.0.1:60892/gobby_test GOBBY_TEST_PROTECT=1 uv run pytest tests/workflows/test_memory_lifecycle_rules.py tests/workflows/test_retired_bundled_definitions.py tests/docs tests/skills -q`.

**Granularity:** more than six Target files, all prose or template edits expressing one change
(the guidance now describes a pushed index). Splitting would leave the instruction set
contradicting the docs between leaves, so it stays one leaf.

**Acceptance:**

- 3.1.1 - `AGENTS.md` carries Working Rule 14 with objective triggers and the familiar-code clause. file: `AGENTS.md`.
- 3.1.2 - `remind-memory-guidance-on-later-turns` is retired and absent from bundled templates. test: `tests/workflows/test_retired_bundled_definitions.py::test_retired_rules_are_absent_from_bundled_templates`.
- 3.1.3 - The initial-stop memory gate still passes its cases. test: `tests/workflows/test_memory_lifecycle_rules.py::TestLayeredMemoryGuidance`.
- 3.1.4 - The memory guide describes the pushed index and agent search, and its audited anchor resolves. file: `docs/reference-audit/memory.json`.
- 3.1.5 - The bundled memory references describe the index and the `when:`-style rationale. file: `src/gobby/install/shared/skills/gobby/references/memory/overview.md`.
- 3.1.6 - The usefulness-label contract names the `memory.surface` caller as not shadow-eligible. file: `docs/contracts/memory-usefulness-label.md`.

## R1 Rollout
`kind: framing`

1. Merge 1.1. Announce a Python-only restart with a `global` `send_message`, wait for a window
   with no live spawned worker or close validator, and run `uv run gobby restart --wait` from
   the main checkout.
2. Run `uv run gobby memory reindex-embeddings` once so every row carries a content plus
   rationale vector.
3. Re-collect the cohort against the live daemon and complete 1.2; then 1.3.
4. Merge P2 and restart as in step 1. Confirm the installed rule rows with
   `uv run gobby rules list`.
5. Merge P3; rule changes reach the DB at the next start.
6. Update memory `d2ae6cc2` and supersede memory `cfb24953` ("Automatic project-memory recall
   injection is retired"), which becomes false once P2 is live. Give the replacement a
   `when:`-style rationale.
7. Close #22410 with the decision on its item 1 recorded: injection is rebuilt as a pushed
   index; the shadow cohort stays off.

## V1 Verification
`kind: verification`

- Per-deliverable pytest commands are listed in each section. Repo gates before each commit:
  `uv run ruff format src/`, `uv run ruff check src/`, `uv run mypy src/`,
  `uv run gobby test-types audit tests/ --baseline .gobby/test-types-baseline.json --fail-on-new`.
- Live check after P1: `gobby-memory:search_memories` with the query
  "droid SessionEnd hook teardown enqueue-only exit code" returns `002c13ae` in the top 3.
- Live check after P2, in a fresh Claude Code session in this repo: send a substantive prompt
  and confirm a `<memory-index trigger="turn">` contributor arrives; send "ok" after an agent
  message that states an intent and confirm the index reflects that message; call
  `spawn_agent` and `claim_task` and confirm their indexes; repeat a trigger and confirm
  already-shown ids do not reappear; run `/compact` and confirm they can reappear.
- Live check after P3: a new session shows no constant memory reminder line on later turns.

## F1 Found Work
`kind: framing`

- `src/gobby/memory/recall_fit.py` over the line ceiling with a stale replay order: owned by 1.3.
- `search-memories-on-claim.yaml` printing a placeholder for `claim_task`: owned by 2.2.
- Not owned by this plan: during planning, `require-task-before-edit` blocked a read-only
  `python3 -c` analysis three times, including as the last stage of a pipe, because the shell
  classifier in `src/gobby/hooks/_normalization_canonical.py` marked it a repo mutation of
  unknown scope. The exact trigger was not diagnosed. The session executing this plan fixes it
  under the found-work ladder or hands it to the owner of that classifier.
- Corpus gaps the graders hit, for the next memory hygiene pass rather than this plan: no memory
  records the ACP `PreCompress` alias matrix or that a new config field requires regenerating
  `runtime_config_contract.json`; memory `ce5c5559` (a binary install recipe) is corrected by
  `8303e661` but is not marked superseded; `677e9367` and `1dc55056`, and `8264a7bb` and
  `aa72a7df`, are uncollapsed near-duplicates.

## A1 Appendix: Planning Cohort
`kind: framing`

Grades: A directly answers the need; B useful related context; C same topic, not useful; D
irrelevant. Entries are `id8:grade` in the order the live tool returned them on 2026-09-17.
Scores are deliberately omitted; 1.2 re-collects them after 1.1 is live.

- **H1** intent. "I'll spawn a droid worker and wait for its SessionEnd hook to confirm teardown". Need: whether Droid emits a SessionEnd Gobby treats as conclusive teardown, and what releases the run's resources. `97ca4732:B 91afd454:B 717b9191:C c4b613c1:C b9eb3e98:A b580a16f:C 46fa1b6a:B ac7eb132:A 7a3fa61b:D fdc7fb5c:C`
- **H2** intent. "I'm about to inject additionalContext into a Codex PostToolUse hook response and need to know the character budget before it gets truncated". Need: the additionalContext budget and overflow behavior for Codex, and which Codex channels the model reads. `cf0ec085:C 677e9367:A 1874988a:C d39ec121:C 67b7c348:C c90a6610:C 89f8093c:D 1dc55056:A 002c13ae:C b16c17c5:A`
- **H3** keyword. "grok denied tool call additionalContext dropped". Need: Grok discards additionalContext on a denied PreToolUse, so context must be re-queued. `e7f747b1:B 7285554f:A a39d0411:C 4fac2a88:C 039316e6:C 92088eed:D c444b26a:C 2ecbc6f6:C e8c49be1:C 5252a7f8:D`
- **H4** intent. "I'm wiring ghook envelope delivery to the daemon and want to make sure hook events are not lost when the daemon is down or slow". Need: ghook retain-versus-dequeue rules and status codes. `71ecb55a:A de3911bd:B 24214c83:B f091327c:A 1a2a5733:B c3abb06e:A 5881108a:A 46fa1b6a:B fa9ff965:B ec2dc36a:C`
- **H5** keyword. "PreCompact hook ACP PreCompress event mapping". Need: how PreCompact is named and aliased across providers. `ebe4a8c3:C b1aad18e:B e527586b:D df805fe8:D 69c97104:D 91afd454:B 04ae2792:D 347b5968:D 95f63a99:D 1b776ce9:C`
- **H6** intent. "I need to read a session's transcript JSONL file and detect which provider CLI wrote it before parsing the turns". Need: how Gobby decides a transcript's provider, and whether hand-rolled parsing is sanctioned. `f97e7e37:A 67216ada:A 578b8217:A 1135dd46:D 9cbef626:B c45984b1:B b921d898:A c9f5467c:D b21266d1:D 9d8d7be3:C`
- **H7** keyword. "AGY antigravity adapter hook integration". Need: AGY's native hook events and its fail-open contract. `02a919ca:B 374f608d:B 9d5aa860:B 7e08a956:D c2a3fe82:B cda2f50a:C cc95987b:C 46fa1b6a:A b1aad18e:C 87612053:B`
- **T1** intent. "I'm about to close this task and the close gate keeps rejecting it even though I ran the validation commands". Need: what makes a validation command get credited by the close gate. `90a44391:A 3d4ad376:A da7571f4:A c37771cd:B 31a73e88:C fa4194a1:C 108cfcf4:C bada0783:B 124fb7fd:A 378504e1:C`
- **T2** intent. "I'm going to spawn three worker agents into separate git worktrees and merge their branches back into the parent when they finish". Need: isolation-selection rules and the authoritative branch-landing tool. `c3f5e1b9:B ad6c2edc:C d5da342e:A ddc7fc76:C d3217ac4:C 437e8c2b:A e93893f1:B 35e282a4:A c6eff715:B d31e7115:D`
- **T3** keyword. "stage manifest ordering gobby build dispatch stages". Need: how the current stage is resolved and when stage rows exist. `ece78b09:A bfabc795:C 60428679:A 4644eba6:C 7019fb57:B ee31bd59:A adc3f5a2:C 4e95be13:C 4d0d0325:B ad72c465:B`
- **T4** keyword. "commit guard task tag in commit message enforcement". Need: the tag format that auto-links a commit and what guard can block it. `1e96fbc3:D f1a6ffa7:A 2288f282:B 7df22c7b:D e46a2704:D bada0783:A 7082f568:C f5e40789:C 1906132b:D 7ed219fc:B`
- **T5** intent. "I want to write a new workflow rule that blocks the Stop event and injects context back to the agent until it records validation evidence". Need: semantics of block and inject_context on Stop, and how stop gates decide they have evidence. `90a44391:C fa4194a1:A a8a4f7dc:D 82bd8935:A e060758f:C 8397a9a3:D 637ed1e5:D 274dc6fd:D c48544da:D b1d3ea6e:D`
- **T6** intent. "I'm about to expand my approved plan file into child leaf tasks under an epic and start dispatching them". Need: the ordered expansion procedure, accepted categories, and whether dispatch may follow. `4430ab84:A c9629485:A f950ecb4:A 6c1cce47:B ad72c465:B 4644eba6:A 0267f262:C 21799deb:D f3e64ab4:B 6c0a33dc:C`
- **T7** keyword. "cross-session message delivery between agent sessions send_message inbox". Need: send_message target and authorization rules and when a message reaches the model. `e8d7cb8a:A d883cafa:A 8b5835e9:B b16c17c5:A f257e8c9:D d4b1ccc8:D cc4cb25a:D c678180b:C 01307e9d:D 3cf7db95:B`
- **I1** intent. "I changed gobby-core so I'll rebuild gcode and copy the new binary into ~/.gobby/bin". Need: installing a rebuilt binary is a staged, signed, whole-set promotion with an identity stamp. `8303e661:A ce5c5559:C 1c3d3c89:B b8048fc3:B 18e85f24:C c81175e4:D ba45d7a1:C 90a67a22:B 47019dba:D ae51ebe3:D`
- **I2** intent. "I'm about to restart the gobby daemon after changing the database schema so the new code takes effect". Need: restart after a schema change is gated by schema identity and must be announced and timed. `f71268af:A 57f76b4a:A 91ec3d77:A fcb9361f:B f71dbc04:C 49cf2d3f:B eb61aee9:C 8303e661:B 9ca82b73:B 921f56c4:D`
- **I3** intent. "I'm adding a new field to the Gobby config models and registry, so I need to update the runtime config contract". Need: a new config field must be registered and the contract carrier regenerated. `cf04e425:B e92cc5f9:C aa8f3cf6:C 2818e2a2:D f08120b7:D a894afae:B df9a7ca1:C 4bd4fa6c:D c81175e4:D 4430ab84:D`
- **I4** intent. "I'm going to run the pytest suite locally on this machine while the real daemon and hub database are running". Need: local pytest must use an isolated database and is protected from the live hub. `c6f40bcd:D 224a3d8e:B 8264a7bb:A b5b528fc:C f1289299:D 707a1909:C 70099c0b:C f42307b4:D a02233a2:B aa72a7df:A`
- **I5** keyword. "MCP proxy schema lease progressive tool discovery". Need: what a schema lease covers, what grants one, and who is exempt. `0b4c62d6:A 1e3259fa:A ff600a62:C c13ec7d9:A 06635b3c:B f0c7093f:D 95c19455:D 3981bf02:C 52ea92c2:D 4d03509a:D`
- **I6** keyword. "tool result offloading result_id get_tool_result threshold". Need: when a result is offloaded, when it is not stored, and how to page it back. `fec5d734:A c6432d0d:A e08ff442:A 480ac804:D a4261f14:C 2e392c09:D 7a601bfd:A d39ec121:D 02c11027:B c444b26a:D`
- **I7** keyword. "PostgreSQL hub migration numbering and schema head". Need: where hub schema lives, who may apply DDL, and how to choose the next migration number. `b112c78f:A f1a2fbbf:A 9839a0b7:A c6f40bcd:C f4ea9539:B 2cc64106:C 25a60724:C b6416695:B 793053e3:D fc668c97:A`
- **M1** keyword. "droid SessionEnd hook teardown enqueue-only exit code". Need: why spawned droid runs produce no SessionEnd teardown (the #22410 evidence case). `97ca4732:B 717b9191:D 91afd454:B c4b613c1:D b580a16f:C 46fa1b6a:B fdc7fb5c:D 002c13ae:A`
- **M2** keyword. "memory search result ordering fused RRF score versus cosine similarity sort key". Need: the score-axis contract for memory search ordering. `d2ae6cc2:A edd1e453:C 63498675:D 7fdb369c:B 92e7346d:D 6a85ec83:B c9b68310:D 49525dbc:D 86fa0615:D 12be4c5b:D`
- **M3** intent. "rule mcp_call inject_result delivers tool result into agent context additionalContext which hook events". Need: how an inline mcp_call result reaches agent context and which hook events carry it. `adee6f7c:A 0f8bd0d2:A d82cd5f1:B c6432d0d:C e21978ee:A 04ae2792:D c13ec7d9:D 677e9367:A`

## M1 Task Manifest
`kind: manifest`

```yaml
- title: Embed memory search queries verbatim
  category: code
  task_type: feature
  depends_on: []
  validation_criteria: '1.1.1: A query of six or more words reaches the embedding
    function unchanged when no `embed_text` is supplied. test: `tests/memory/test_search_ranking.py::test_query_is_embedded_verbatim`.

    1.1.2: A caller-supplied `embed_text` is still embedded in place of the query.
    test: `tests/memory/test_search_ranking.py::test_embed_text_overrides_query_for_embedding`.

    1.1.3: The keyword extraction module and its test file are gone and nothing imports
    them. file: `src/gobby/search/keywords.py`.

    1.1.4: `yake` is absent from the declared dependencies and the lockfile. file:
    `pyproject.toml`.

    1.1.5: The memory guide no longer describes keyword extraction of search queries.
    file: `docs/guides/memory.md`.'
  labels:
  - covers:memory-surfacing:1.1:1.1.1
  - covers:memory-surfacing:1.1:1.1.2
  - covers:memory-surfacing:1.1:1.1.3
  - covers:memory-surfacing:1.1:1.1.4
  - covers:memory-surfacing:1.1:1.1.5
  tdd: true
  source_section: '1.1'
  implementation_domain: backend
- title: Graded ranking fixture and fused sort policy
  category: code
  task_type: feature
  depends_on:
  - '1.1'
  validation_criteria: '1.2.1: `build_results` orders hits only through `result_sort_key`.
    symbol: `result_sort_key`. file: `src/gobby/memory/services/_search_ranking.py`.

    1.2.2: The fixture holds all 24 cohort queries with needs, styles, graded hits,
    re-collected scores, the selected `policy`, and `surface_min_score`. file: `tests/memory/fixtures/ranking_cohort.json`.

    1.2.3: The shipped policy equals the selection rule''s winner on the fixture.
    test: `tests/memory/test_ranking_policy.py::test_shipped_policy_is_selection_rule_winner`.

    1.2.4: The shipped policy''s A@3 and A@1 on the fixture are each at least the
    `cosine` candidate''s. test: `tests/memory/test_ranking_policy.py::test_shipped_policy_does_not_regress_cosine`.

    1.2.5: Query M1 (the #22410 evidence query) returns memory `002c13ae` in the top
    3 under the shipped policy. test: `tests/memory/test_ranking_policy.py::test_evidence_memory_reaches_top_three`.

    1.2.6: The `search_memories` tool description and the memory guide state the shipped
    ordering. file: `docs/guides/memory.md`.'
  labels:
  - covers:memory-surfacing:1.2:1.2.1
  - covers:memory-surfacing:1.2:1.2.2
  - covers:memory-surfacing:1.2:1.2.3
  - covers:memory-surfacing:1.2:1.2.4
  - covers:memory-surfacing:1.2:1.2.5
  - covers:memory-surfacing:1.2:1.2.6
  tdd: true
  source_section: '1.2'
  implementation_domain: backend
- title: Share the sort key with recall replay and split recall_fit
  category: refactor
  task_type: feature
  depends_on:
  - '1.2'
  validation_criteria: '1.3.1: `recall_fit.py` is under 1,000 lines. file: `src/gobby/memory/recall_fit.py`.

    1.3.2: `replayed_sort_key` delegates to `result_sort_key`. symbol: `replayed_sort_key`.
    file: `src/gobby/memory/recall_replay.py`.

    1.3.3: Replay ordering matches live ordering for the same score inputs. test:
    `tests/memory/test_recall_fit.py::test_replayed_order_matches_live_sort_key`.'
  labels:
  - covers:memory-surfacing:1.3:1.3.1
  - covers:memory-surfacing:1.3:1.3.2
  - covers:memory-surfacing:1.3:1.3.3
  tdd: false
  source_section: '1.3'
  assigned_agent: backend-developer
- title: surface_memories tool and memory-index delivery
  category: code
  task_type: feature
  depends_on:
  - '1.1'
  - '1.2'
  - '1.3'
  validation_criteria: '2.1.1: `surface_memories` searches with the text embedded
    verbatim, a limit of 5, review lessons excluded, the fixture floor, and caller
    `memory.surface`. test: `tests/mcp_proxy/tools/test_memory_surface.py::test_surface_search_arguments`.

    2.1.2: A search failure returns an empty result and raises nothing. test: `tests/mcp_proxy/tools/test_memory_surface.py::test_surface_fails_open`.

    2.1.3: `SURFACE_MIN_SCORE` equals the fixture''s `surface_min_score`. test: `tests/mcp_proxy/tools/test_memory_surface.py::test_floor_matches_ranking_fixture`.

    2.1.4: The rendered index carries rank, id, type, searches, date, content lead,
    and the `when:` clause, no scores, and the `get_memory` instruction. test: `tests/workflows/test_memory_index_delivery.py::test_index_line_format`.

    2.1.5: A memory already in `injected_memory_ids` is not shown again, and newly
    shown ids are staged. test: `tests/workflows/test_memory_index_delivery.py::test_index_dedupes_and_stages_ids`.

    2.1.6: When every hit is filtered out, nothing is injected. test: `tests/workflows/test_memory_index_delivery.py::test_empty_index_injects_nothing`.

    2.1.7: The tool is listed in the reference audit and the memory guide. file: `docs/reference-audit/memory.json`.'
  labels:
  - covers:memory-surfacing:2.1:2.1.1
  - covers:memory-surfacing:2.1:2.1.2
  - covers:memory-surfacing:2.1:2.1.3
  - covers:memory-surfacing:2.1:2.1.4
  - covers:memory-surfacing:2.1:2.1.5
  - covers:memory-surfacing:2.1:2.1.6
  - covers:memory-surfacing:2.1:2.1.7
  tdd: true
  source_section: '2.1'
  implementation_domain: backend
- title: Tool-intent surfacing rules
  category: code
  task_type: feature
  depends_on:
  - '2.1'
  - '1.1'
  - '1.2'
  - '1.3'
  validation_criteria: '2.2.1: Each of the four rules fires on its tool and event,
    passes the stated text and trigger, and injects inline. test: `tests/workflows/test_memory_lifecycle_rules.py::TestToolIntentSurfacing`.

    2.2.2: `claim_task` returns the claimed task''s title. file: `src/gobby/mcp_proxy/tools/tasks/_lifecycle_claim.py`.

    2.2.3: `search-memories-on-claim` is retired and absent from bundled templates.
    test: `tests/workflows/test_retired_bundled_definitions.py::test_retired_rules_are_absent_from_bundled_templates`.

    2.2.4: The rule-group table and the memory guide list the new rules and the new
    count. file: `src/gobby/install/shared/workflows/rules/AGENTS.md`.'
  labels:
  - covers:memory-surfacing:2.2:2.2.1
  - covers:memory-surfacing:2.2:2.2.2
  - covers:memory-surfacing:2.2:2.2.3
  - covers:memory-surfacing:2.2:2.2.4
  tdd: true
  source_section: '2.2'
  implementation_domain: backend
- title: Turn-start surfacing from the prompt or the agent's last message
  category: code
  task_type: feature
  depends_on:
  - '2.2'
  - '1.1'
  - '1.2'
  - '1.3'
  validation_criteria: '2.3.1: Acknowledgments, continuations, status questions, slash
    commands, and short lifecycle commands are non-substantive; a task-describing
    prompt is substantive. test: `tests/memory/test_prompt_triage.py::test_triage_matrix`.

    2.3.2: The turn trigger searches on a substantive prompt. test: `tests/mcp_proxy/tools/test_memory_surface.py::test_turn_trigger_uses_substantive_prompt`.

    2.3.3: The turn trigger searches on the session''s last assistant text when the
    prompt is non-substantive, and returns empty when there is none. test: `tests/mcp_proxy/tools/test_memory_surface.py::test_turn_trigger_falls_back_to_assistant_text`.

    2.3.4: The rule fires once per parent turn, never for spawned agents, and commits
    its guard on receipt. test: `tests/workflows/test_memory_lifecycle_rules.py::TestTurnStartSurfacing`.

    2.3.5: The rule-group table and the memory guide list the rule and the new count.
    file: `src/gobby/install/shared/workflows/rules/AGENTS.md`.'
  labels:
  - covers:memory-surfacing:2.3:2.3.1
  - covers:memory-surfacing:2.3:2.3.2
  - covers:memory-surfacing:2.3:2.3.3
  - covers:memory-surfacing:2.3:2.3.4
  - covers:memory-surfacing:2.3:2.3.5
  tdd: true
  source_section: '2.3'
  implementation_domain: backend
- title: Working Rule 14 and retirement of the constant reminder
  category: docs
  task_type: feature
  depends_on:
  - '2.3'
  - '2.1'
  - '2.2'
  validation_criteria: '3.1.1: `AGENTS.md` carries Working Rule 14 with objective
    triggers and the familiar-code clause. file: `AGENTS.md`.

    3.1.2: `remind-memory-guidance-on-later-turns` is retired and absent from bundled
    templates. test: `tests/workflows/test_retired_bundled_definitions.py::test_retired_rules_are_absent_from_bundled_templates`.

    3.1.3: The initial-stop memory gate still passes its cases. test: `tests/workflows/test_memory_lifecycle_rules.py::TestLayeredMemoryGuidance`.

    3.1.4: The memory guide describes the pushed index and agent search, and its audited
    anchor resolves. file: `docs/reference-audit/memory.json`.

    3.1.5: The bundled memory references describe the index and the `when:`-style
    rationale. file: `src/gobby/install/shared/skills/gobby/references/memory/overview.md`.

    3.1.6: The usefulness-label contract names the `memory.surface` caller as not
    shadow-eligible. file: `docs/contracts/memory-usefulness-label.md`.'
  labels:
  - covers:memory-surfacing:3.1:3.1.1
  - covers:memory-surfacing:3.1:3.1.2
  - covers:memory-surfacing:3.1:3.1.3
  - covers:memory-surfacing:3.1:3.1.4
  - covers:memory-surfacing:3.1:3.1.5
  - covers:memory-surfacing:3.1:3.1.6
  tdd: false
  source_section: '3.1'
  assigned_agent: backend-developer
```
