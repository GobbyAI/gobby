# Wiki Gap Closure

**Plan ID:** wiki-gap-closure

## Overview

`kind: framing`

The June 2026 wiki bake-off's ten adoption candidates (C1–C10) all shipped; a July 2026
re-audit of ./wiki/ against the current field (hosted DeepWiki, Google Code Wiki, LangChain
OpenWiki, Grok-Wiki, Sourcegraph Deep Search) shows gobby leads on coverage, grounding, and
incremental regeneration but has six root-caused gaps: near-zero diagram emission, four
wiki_ask defects, rotting agent exports, no commit anchoring, junk knowledge concepts, and
no deep-research mode. This epic closes all six and retires the Lane-A/Lane-B codenames.
Every fix reuses existing machinery: the shared gcore sanitizer, the agentic tool-loop
transport, deterministic renderers recoverable from `git show cfb49261c^`, and the #17727
candidate/quarantine lifecycle.

## Constraints

`kind: framing`

- Pre-0.5.0: no backward compatibility anywhere; rename and drop freely.
- Evidence-contract honesty is non-negotiable: no fabricated diagram edges; deterministic
  diagrams render only verified AST/graph facts; fallback diagrams carry explicit
  "structure, not runtime flow" captions.
- The frontmatter field `lane: tool_loop` keeps its name — only letter codenames
  (lane_a/lane_b/LaneB) are retired.
- Tests: scoped `cargo test -p gobby-core -p gobby-wiki -p gobby-code`, never bare
  workspace; `GOBBY_TEST_PROTECT=1` on all pytest; no full pytest suite.
- Rust changes are live only after rebuild + reinstall to `~/.gobby/bin/`.
- Render-version bumps are scoped per tier and land at most once per tier in this epic
  (module bump carried by 4.3 alone — see WGC-MODULE-RENDER-BUMP-ORDER; curated bump
  by 4.4).

## P1: Ask retrieval and synthesis fixes

`kind: framing`

**Goal**: wiki_ask/wiki_search handle natural-language queries, keep hub pages out of
results, feed clean evidence to synthesis, and authenticate the direct AI route.

### 1.1 Harden shared pg_search sanitizer and surface backend errors [category: code]

`kind: deliverable`

Target: `crates/gcore/src/search.rs`, `crates/gwiki/src/search/bm25.rs`, `crates/gwiki/src/search/mod.rs`

Root cause: `sanitize_pg_search_query` (crates/gcore/src/search.rs:133-169) escapes
brackets/parens/booleans but passes Tantivy metachars (`? ' * : ^ ~ { } / !`) through to
the pg_search `@@@` operator → parse error → `SearchError::Backend` → hard failure of
search AND ask (BM25 errors propagate with `?` in crates/gwiki/src/search/mod.rs:186-266,
unlike semantic/graph which degrade). gcode shares the same function, same bug. The error
mapping at bm25.rs:212,219 uses `error.to_string()` which yields opaque "db error".

Changes:
- Extend the escape pass in `sanitize_pg_search_query` to cover `? ' * : ^ ~ { } / !`,
  with a quote-aware tracker reusing `neutralize_boolean_operators`'s unescaped-quote
  logic (same file :171) so balanced phrase interiors keep working; pre-count unescaped
  `"` and if odd, escape all of them (deterministic dangling-quote repair). Do NOT adopt
  the strip-to-alnum Python variant (src/gobby/search/keyword.py:391) — it would destroy
  quoted-phrase and `foo::bar` support pinned by crates/gcode/src/search/fts/tests.rs:39-75.
- Add `backend_error(postgres::Error) -> SearchError` in crates/gwiki/src/search/mod.rs
  using `as_db_error()` (pattern: crates/gwiki/src/store/types.rs:135-143); replace the
  SEVEN lossy postgres `SearchError::Backend(error.to_string())` sites in bm25.rs
  (:212, :219, :251, :263, :271, :281, :288). Scoped in review round 2
  (WGC-BACKEND-ERROR-TYPE): only the bm25.rs sites receive `postgres::Error`;
  semantic.rs:371 receives an `AiError` from `embed_via_daemon` and keeps its existing
  mapping — `AiError`'s Display already preserves bounded HTTP status/body detail and
  semantic.rs:317-321 special-cases `HttpStatus`.
- Leave BM25 error propagation loud (no degradation path): with the sanitizer fixed, a
  backend error is a real outage.
- Backend-boundary proof (enhancement E4): one pg_search-backed regression executing the
  exact apostrophe-bearing crash query ("How does the MCP proxy's progressive tool
  discovery work and why does it exist?"), the full metachar corpus, a balanced phrase,
  and a dangling quote against the real backend, asserting success with no
  `SearchError::Backend` — string-level escape tests alone don't prove Tantivy accepts
  the output.

**Acceptance:**

- 1.1.1 - Queries containing `? ' * : ^ ~ { } / !` return results instead of erroring;
  metachar/quote-balance/phrase cases covered. test: `crates/gcore/src/search.rs::sanitize_escapes_tantivy_metachars`.
- 1.1.2 - Escape-pass extension lands in the shared sanitizer. symbol: `sanitize_pg_search_query`. file: `crates/gcore/src/search.rs`.
- 1.1.3 - All seven bm25.rs postgres error mappings preserve server detail via the new
  helper; the semantic.rs `AiError` mapping is unchanged. symbol: `backend_error`. file: `crates/gwiki/src/search/mod.rs`.
- 1.1.4 - gcode fts sanitizer expectations updated for the new escapes. test: `crates/gcode/src/search/fts/tests.rs`.
- 1.1.5 - pg_search-backed regression runs the crash query + metachar corpus + dangling
  quote without `SearchError::Backend`. test: `crates/gwiki/src/search/bm25.rs`.

### 1.2 Strip frontmatter and empty excerpts from ask evidence [category: code]

`kind: deliverable`

Target: `crates/gwiki/src/commands/search.rs`, `crates/gwiki/src/commands/ask/evidence.rs`

Root cause: `run_search_with_backends` (commands/search.rs:221-309) pushes the raw
`result.snippet` (full page body including `---` frontmatter; empty string for graph hits)
as evidence; `query_window` (:337-357) does no stripping; `plan_evidence`
(ask/evidence.rs:31-83) keeps empty excerpts, so graph-only hits burn evidence slots in the
12K-token prompt with `excerpt_chars: 0`.

Changes:
- In `run_search_with_backends`, before both snippet windowing and evidence push, slice off
  frontmatter with `gobby_core::markdown::frontmatter_body_start` (crates/gcore/src/markdown.rs:129)
  — offset-only, no YAML parse; chunk snippets without frontmatter are a no-op.
- In `plan_evidence`, `continue` on an empty trimmed excerpt — skip without consuming an
  evidence slot or emitting an `AskEvidenceOutput`; `break` stays reserved for budget
  overflow.
- Alignment by provenance, not index (enhancement E2): each evidence item carries its
  result/page provenance (source path) rather than relying on `evidence[i]`↔`results[i]`
  positional pairing, so skipping an empty hit can never shift a later citation onto the
  wrong page.

**Acceptance:**

- 1.2.1 - Document-hit snippets and evidence contain body text, never `---` frontmatter. test: `crates/gwiki/src/commands/search.rs::document_hit_evidence_strips_frontmatter`.
- 1.2.2 - Empty excerpts are skipped without consuming budget; later hits still included. test: `crates/gwiki/src/commands/ask/evidence.rs::empty_excerpts_are_skipped_without_consuming_budget`.
- 1.2.3 - Existing budget invariant stays green. test: `crates/gwiki/src/commands/ask/evidence.rs::prompt_never_exceeds_token_budget`.
- 1.2.4 - Regression: empty graph hit followed by a document hit — surviving evidence and
  citation still reference the document's original page/source. test: `crates/gwiki/src/commands/ask/evidence.rs`.

### 1.3 Down-weight hub backlinks and hydrate titles in graph boost [category: code]

`kind: deliverable`

Target: `crates/gwiki/src/search/graph_boost.rs`

Root cause: `rank_link_neighborhood` (:200-266) gives every backlink source
`seed_score * 0.8` regardless of how many pages it links to, so hub pages
(code/INDEX.md, code/_ownership.md) accumulate score from any code-page seed and surface in
every query's top-10; `graph_result` (:281-303) hardcodes `title: None, snippet: ""` even
though `GraphBoostDocument.title` is already loaded by `query_documents`
(crates/gwiki/src/falkor_graph/boost.rs:37-67), and RRF `merge_hit_metadata`
(search/rrf.rs:98-108) cannot enrich graph-only hits.

Changes:
- Precompute per-source outdegree over resolvable links; backlink arm becomes
  `seed_score * 0.8 / outdegree`. Outbound-from-seed arm unchanged. Keep graph-only hits
  (related-pages discovery is the feature); no snippet hydration (1.2 makes empty snippets
  harmless).
- Thread document titles through `graph_boost_hits`/`graph_result` (both
  `FalkorGraphBoostBackend::search_graph_boost` and `MemoryGraphBoostBackend` call sites)
  so graph hits carry real titles.

**Acceptance:**

- 1.3.1 - A hub linking to 20 docs ranks below a single-link page for the same seed. test: `crates/gwiki/src/search/graph_boost.rs::hub_backlink_source_is_downweighted_by_outdegree`.
- 1.3.2 - Graph hits carry document titles. test: `crates/gwiki/src/search/graph_boost.rs::graph_hits_carry_document_titles`.
- 1.3.3 - Integration guard: hub pages stay out of top results for unrelated queries. test: `crates/gwiki/src/search/mod.rs::graph_linked_pages_enter_search_results`.

### 1.4 Add conventional-env api_key fallback to the direct AI route [category: code]

`kind: deliverable`

Target: `crates/gcore/src/ai/generation/profile.rs`

Root cause: the direct route's api_key resolves only from config
`ai.text_generate[.<profile>].api_key` (profile.rs:49-87; env consulted only via `${VAR}`
interpolation, config/resolve.rs:24-75), so an exported `OPENAI_API_KEY` is never read →
request sent without Authorization header (generation/transport.rs:75-103) → 401. Route
fallback is selection-time only (ai/mod.rs:79-113) — correct, keep it; direct is only
chosen when the daemon is unavailable or forced, so NO execution-time direct→daemon retry.
Existing prerequisite (review round 2 WGC-HTTP-ERROR-STALE): `AiError::HttpStatus`'s
Display already renders status + a bounded 400-char escaped body snippet
(crates/gcore/src/ai_types.rs:228-244, landed in ba7a40f3e) and flows into gwiki's
`ai.error` via `mark_ai_unavailable` — no Display change in this plan; 1.4.3 pins the
behavior as a regression only.

Changes:
- In `resolve_direct_generation_target`, when config api_key is None fall back to
  `default_env_api_key(provider)` for EXPLICITLY RECOGNIZED providers only:
  anthropic→ANTHROPIC_API_KEY, openai→OPENAI_API_KEY, openrouter→OPENROUTER_API_KEY,
  groq→GROQ_API_KEY (trimmed, non-empty). Absent or unrecognized providers resolve NO env
  key (review round 2 WGC-APIKEY-ORIGIN): `DirectChatTransport` is provider-neutral and
  sends the resolved key as a Bearer header to the configured api_base, so a
  catch-all→OPENAI_API_KEY default would leak the user's OpenAI credential to custom or
  local endpoints. `${VAR}` interpolation untouched.

**Acceptance:**

- 1.4.1 - Unset config + exported env key ⇒ direct target carries the key for each
  recognized provider; absent/unrecognized provider with a custom/local api_base and
  `OPENAI_API_KEY` exported resolves NO key (credential-leak regression). test: `crates/gcore/src/ai/generation/tests.rs`.
- 1.4.2 - Env fallback implemented at target resolution. symbol: `resolve_direct_generation_target`. file: `crates/gcore/src/ai/generation/profile.rs`.
- 1.4.3 - Regression pin on existing behavior: HTTP failures surface status + bounded body snippet in `ai.error`. test: `crates/gwiki/src/commands/ask/synthesis.rs::ask_model_unavailable_marks_degraded`.

## P2: Generation-lane naming cleanup

`kind: framing`

**Goal**: retire widget_a-style Lane-A/Lane-B codenames for semantic names before the deep
ask work builds on those modules.

### 2.1 Rename Lane A/Lane B identifiers to semantic names [category: refactor]

`kind: deliverable`

Target: `crates/gcore/src/ai/generation/one_shot.rs`, `crates/gcore/src/ai/generation/mod.rs`, `crates/gcore/src/ai/generation/tests.rs`, `crates/gwiki/src/commands/generation_routes.rs` (renamed from `lanes.rs`), `crates/gwiki/src/commands/compile.rs`, `crates/gwiki/src/commands/upkeep.rs`, `crates/gcode/src/commands/codewiki/build_parts/curated_content/tool_loop_dump.rs` (renamed from `lane_b_dump.rs`), `crates/gcode/src/commands/codewiki/build_parts/curated_content.rs`, `crates/gcode/src/commands/codewiki/generation.rs`, `crates/gcode/src/commands/codewiki/frontmatter.rs`, `crates/gcode/src/commands/codewiki/text/frontmatter.rs`, `crates/gcode/src/commands/codewiki/build_parts/concepts/render.rs`, `crates/gcode/src/commands/codewiki/render/overview.rs`, `crates/gcode/src/commands/codewiki/render/repo.rs`, `crates/gcode/src/commands/codewiki/run.rs`, `crates/gcode/src/commands/codewiki/tests/concepts.rs`, `crates/gcode/src/commands/codewiki/tests/io_safety.rs`

Pure rename, no behavior change. Lane A = one-shot completion; Lane B = agentic tool-loop
generation. Blast radius (verified by grep):
- gcore: `ai/generation/lane_a.rs` → `one_shot.rs`; `generation/mod.rs` docs + re-exports;
  `generation/tests.rs` mod rename.
- gwiki: `commands/lanes.rs` → `commands/generation_routes.rs`; `run_lane_b` →
  `run_agentic_generation`; `resolve_lane_b_generator` → `resolve_tool_loop_generator`;
  call sites `commands/compile.rs:73`, `commands/upkeep.rs:84`.
- gcode codewiki: `FrontmatterLaneB` → `FrontmatterToolLoop` (text/frontmatter.rs:117-192
  and all `lane_b` locals in build_parts/concepts/render.rs, render/overview.rs,
  render/repo.rs); `build_parts/curated_content/lane_b_dump.rs` → `tool_loop_dump.rs` with
  dump dir `_meta/lane_b/` → `_meta/tool_loop/` (update generation.rs:68 doc comment,
  tests/concepts.rs:244-247, tests/io_safety.rs:41-51 orphan-GC references); the module's
  owner `build_parts/curated_content.rs` updates its `#[path = "curated_content/lane_b_dump.rs"]`
  declaration, `mod lane_b_dump`, the `pub(crate) use lane_b_dump::resolve_lane_b_dump_dir`
  re-export, and the `lane_b_dump::maybe_dump_lane_b_failure(` call site
  (curated_content.rs:20-26, :157 — review round 7 WGC-TARGET-COVERAGE-SWEEP); local
  `lane_a` in run.rs:431-440 → descriptive name; `lane_a` test local in the
  lane-observability test at codewiki/frontmatter.rs:300-301 → descriptive name (distinct
  file from text/frontmatter.rs; review round 7).
- Keep: frontmatter field `lane: tool_loop`, `LANE_TOOL_LOOP` constant,
  `lane_observability_from_content` — values/fields already descriptive.
- Repo-wide sweep (enhancement E9): the identifier sweep covers all current repo-owned
  source, config, and docs (not just crates/), with an explicit allowlist for historical
  fixtures, plan records, and evidence documents.
- Direct rename, no migration (review round 6 WGC-LANE-RENAME-BACKCOMPAT): the runtime
  dump path and its env override rename in place — `_meta/lane_b/` → `_meta/tool_loop/`
  and `resolve_lane_b_dump_dir`'s override variable to tool_loop naming
  (lane_b_dump.rs:5-15) — with NO legacy-path or legacy-env fallback and no migration
  handling, per the pre-0.5.0 no-backward-compatibility constraint. Old `lane_b` text
  survives only in the documented historical-artifact allowlist.

**Acceptance:**

- 2.1.1 - No `lane_a`/`lane_b`/`LaneB` identifiers remain in current repo-owned source, config, or docs (grep-clean outside the documented historical allowlist); scoped crate tests pass unchanged. behavior: "identifier rename is behavior-neutral" in `crates/`.
- 2.1.2 - One-shot module renamed with docs updated. file: `crates/gcore/src/ai/generation/one_shot.rs`.
- 2.1.3 - Tool-loop dump directory renamed and orphan-GC test updated. file: `crates/gcode/src/commands/codewiki/build_parts/curated_content/tool_loop_dump.rs`. test: `crates/gcode/src/commands/codewiki/tests/io_safety.rs`.

## P3: Deep-research ask

`kind: framing`

**Goal**: `gwiki ask --deep` runs a bounded agentic research loop over the vault with
grounded citations, exposed through daemon, MCP, and HTTP.

### 3.1 Implement gwiki ask --deep agentic research mode [category: code] (depends: 1.2, 1.4, 2.1, 3.3)

`kind: deliverable`

Target: `crates/gwiki/src/commands/ask.rs`, `crates/gwiki/src/commands/ask/deep.rs`, `crates/gwiki/src/commands/ask/synthesis.rs`, `crates/gwiki/src/commands/ask/assembly.rs`, `crates/gwiki/src/commands/ask/render.rs`, `crates/gwiki/src/commands/generation_routes.rs`, `crates/gwiki/src/commands/vault_tools.rs`, `crates/gwiki/src/commands/mod.rs`, `crates/gwiki/src/cli.rs`, `crates/gwiki/src/cli/mapping.rs`, `crates/gwiki/src/api.rs`, `crates/gwiki/src/output.rs`, `crates/gwiki/tests/cli_contract.rs`, `crates/gcore/src/ai/generation/transport.rs`, `crates/gcore/src/ai/generation/tests/daemon_agentic.rs`, `crates/gcode/src/commands/codewiki/text/generation.rs`, `crates/gcode/src/commands/codewiki/build_parts/concepts/render.rs`, `crates/gcode/src/commands/codewiki/text/frontmatter.rs`

Architecture: "agentic generation for ask" — the loop already exists server-side
(`daemon_agentic_chat`, crates/gcore/src/ai/generation/transport.rs:172-220, read-only
ToolPolicy) and client-side (`run_tool_loop` + `VaultToolExecutor`, ToolLoopLimits::default
= 8 turns/24 tool calls/16KB/180s). No new orchestration.

Changes:
- New `ask/deep.rs`: round 0 = existing `search::retrieve` + `plan_evidence` (seed evidence
  + honest degradation payload). Resolve route for ToolChat capability via the renamed
  generation-routes helpers. Daemon arm: `daemon_agentic_chat(context, profile_for_tier(ASK_TIER),
  …, gwiki_readonly_tool_policy(), messages, Some(DEEP_MAX_TURNS=8), None)`. Direct arm:
  factor the shared agentic body out of `run_agentic_generation` and reuse it
  (`run_tool_loop` + `VaultToolExecutor`). Off/unresolved: standard ask payload +
  `deep_unavailable` warning via the warning-token-parameterized `mark_ai_unavailable`
  (see the synthesis-seam bullet); error only under `--require-ai`. The ask module owner
  `commands/ask.rs` wires the path (review round 8 WGC-TARGET-COVERAGE-SWEEP-R8): a
  `mod deep;` declaration beside the existing submodule declarations (ask.rs:1-6), the
  `execute` signature (:20-50) gains the deep flag threaded from cli/mapping, and
  dispatch routes deep requests through `deep.rs` in place of the standard
  synthesis arm.
- Limit-exit observability (review round 4 WGC-DEEP-LIMIT-OBSERVABILITY): the daemon route
  already returns `investigation.stop_reason` (src/gobby/servers/routes/llm.py:307), but
  `DaemonAgenticResult` (transport.rs:131-142) omits it and `parse_daemon_agentic`
  (transport.rs:289-314) discards it — add `stop_reason: Option<String>` to
  `DaemonAgenticResult` and parse it, so deep surfaces whatever stop reason the daemon
  reports. Cross-crate struct-literal consumer (review round 6
  WGC-DEEP-STOP-REASON-STRUCT-LITERAL): the gcode test helper `daemon_agentic_result`
  (crates/gcode/src/commands/codewiki/text/generation.rs:1663-1679) constructs
  `DaemonAgenticResult` exhaustively and would stop compiling — update the helper with
  an explicit `stop_reason: None` for existing fixtures and keep the affected
  gobby-code tests in scoped validation. The Rust consumer must also MAP the value,
  not merely compile past it (review round 8 WGC-DAEMON-LANE-STOP-REASON):
  `GenerationOutcome::from_daemon_agentic` (generation.rs:673-699) hardcodes
  `stop_reason: Some(StopReason::Completed)`, so daemon limit exits masquerade as
  natural completion at the lane consumer — map canonical daemon strings into
  `StopReason` (`completed` → Completed, `max_turns` → MaxTurns, `max_tool_calls` →
  MaxToolCalls, `timeout` → Timeout), propagate missing or unrecognized values as
  `None`, and reject known non-completed reasons with
  `GenerationFailureCause::Unavailable` exactly as `from_tool_loop` (:640) does via
  `is_completed()`. Turn provenance rides the same seam (review round 8
  WGC-SPAWN-TURN-PROVENANCE): `DaemonAgenticResult.turns` (transport.rs:139, parsed
  at :311) and `GenerationObservability.turns` (generation.rs:514) become
  `Option<usize>` — the daemon reports turns only from provider-native counts after
  3.3 — with constructors updated (`Some(1)` at :550/:567, tool-loop pass-through at
  :635, daemon verbatim at :677, `{:?}` in the failure log :1147) and the lane
  frontmatter field `FrontmatterLaneB.turns` (text/frontmatter.rs:43) matching
  `Option<usize>` so the writer at :192 flattens via `and_then` (the outer
  `Frontmatter.turns` at :33 and persisted meta at types.rs:620 are already
  `Option`; the production construction sites in concepts/render.rs,
  render/overview.rs, and render/repo.rs pass `observability.turns` through in
  lockstep with no textual edits, but concepts/render.rs's lane-observability
  test builds a direct `GenerationObservability` literal — `turns: 4` at :593
  becomes `Some(4)` and the `turns = 1` reassignment at :616 becomes `Some(1)`
  with the `turns: 4` frontmatter assertions retained — so that file is a
  mandatory 3.1 target and 4.1 serializes behind 3.1 for it (review round 9
  WGC-RUST-OPTION-TURNS-TARGET-R9)). Parser coverage lives at the transport
  boundary (review round 5
  WGC-DEEP-STOP-REASON-PARSER-TEST): `parse_daemon_agentic` is `pub(crate)`, so the
  existing daemon-JSON regression in
  `crates/gcore/src/ai/generation/tests/daemon_agentic.rs` asserts both a present
  `investigation.stop_reason` value and the missing/null → `None` default, and the
  same fixtures pin the optional turn count (review round 9
  WGC-OPTION-TURNS-PARSER-TEST-R9): `parse_daemon_agentic` stops collapsing absent
  counts through the shared `unwrap_or(0)` helper (transport.rs:298-305, assigned
  at :311) — a native `turns: 4` parses to `Some(4)`, missing/null parses to
  `None` (never `Some(0)` from absence), `tool_use_count` keeps its zero default,
  and the existing assertions of `turns` 4/0 at daemon_agentic.rs:144/:190 become
  `Some(4)`/`None` — deep.rs
  tests only cover mapping the already-parsed value into deep status/warnings.
  Daemon candidate timeouts abort server-side
  (src/gobby/ai/_tool_chat_service.py:159-167 raises `_CandidateTimeoutError` from
  `asyncio.wait_for`), so a daemon timeout surfaces as the existing AiError
  degradation (or `--require-ai` error) with partial turns/tool_use_count/usage
  unavailable — each route reports what it can honestly provide; no cancellation
  orchestration is added.
- System prompt: investigate with read-only gwiki tools, cite `[[wiki pages]]`, state
  what could not be verified. Tool parity (review round 4 WGC-DEEP-TOOL-PARITY; made
  route-name-accurate in review round 6 WGC-DEEP-TOOL-NAME-PARITY): the two routes have
  DIFFERENT exact tool names and one shared name contract is impossible without renaming
  the direct tools consumed by existing compile/upkeep generation — parity is SEMANTIC
  (four read-only capabilities per route), never textual. Direct arm:
  `VaultToolExecutor` implements only `search_vault` and `read_document` today
  (vault_tools.rs:114-166) — add bounded read-only `backlinks` and `sources`
  schemas/executors beside them, keeping the existing `search_vault`/`read_document`
  names. Daemon arm: a deep-specific `ToolPolicy` naming exactly `search`, `read`,
  `backlinks`, `sources` (the daemon whitelist names, src/gobby/ai/_tool_chat_tools.py:69-79)
  instead of reusing the broader compile/upkeep policy, which stays untouched. The deep
  system prompt names the tools appropriate to the route it runs on.
- Record through `record_synthesis` with a deep-variant citation check validating `[[page]]`
  citations against vault page existence (excerpt-substring checking would false-flag pages
  the agent read after round 0); same `AskCitationCheck` output type. Synthesis seam
  (review round 5 WGC-DEEP-SYNTHESIS-SEAM): the seam does not exist yet — `ASK_TIER`
  (synthesis.rs:21) and `mark_ai_unavailable` (synthesis.rs:152) are private to
  synthesis.rs, and `record_synthesis` (synthesis.rs:116-150) hardwires the
  excerpt-substring `citation_check` with no way to supply an alternate. Expose
  `ASK_TIER` and `mark_ai_unavailable` at `pub(super)`, and refactor `record_synthesis`
  to accept a precomputed `AskCitationCheckOutput` (standard ask passes the existing
  excerpt-substring result; deep passes the vault-page-existence result) — least
  mechanism: one injected value, no checker trait. The existing `record_synthesis`
  callers in ask/render.rs (:83, :101) migrate to the new signature, passing the
  excerpt-substring result they compute today (review round 7
  WGC-TARGET-COVERAGE-SWEEP). Warning-token seam (review round 6
  WGC-DEEP-WARNING-SEAM): `mark_ai_unavailable` (synthesis.rs:152-184) today takes no
  warning-kind input and unconditionally pushes `ai_unavailable`, so visibility alone
  cannot produce the `deep_unavailable` warning — refactor the helper to accept the
  explicit warning token (standard ask passes `ai_unavailable`, deep passes
  `deep_unavailable`; degraded_sources/status/require-ai behavior shared) and update
  every existing caller.
- Output: `AskOutput.deep: Option<AskDeepOutput>` {route, model, turns: Option,
  tool_use_count, max_turns, usage, stop_reason: Option<String>} (review round 7
  WGC-DEEP-STOP-REASON-OUTPUT: without the field, the value 3.1.5 surfaces has nowhere
  to live and dies before reaching ask users). `turns` is optional end-to-end (review
  round 8 WGC-SPAWN-TURN-PROVENANCE): the direct arm always has the real tool-loop
  count; the daemon arm carries the provider-native count and null when the provider
  reports none. Direct arm maps `StopReason` variants to
  canonical strings (Completed → `completed`, MaxTurns → `max_turns`, MaxToolCalls →
  `max_tool_calls`, Timeout → `timeout`); daemon arm propagates the parsed
  `investigation.stop_reason` verbatim, `None` when the daemon omits it. CLI: `AskArgs
  --deep` implies synthesis; `--deep --ai off` rejected like `--llm --ai off`; thread
  through api.rs, cli/mapping.rs, commands/mod.rs.
- NOT building: client-side round loop, research-session.json coupling, new MCP tools, web
  retrieval, streaming, intermediate-round persistence, or external cancellation of an
  in-flight deep loop (review round 2, WGC-ASK-CANCEL): `StopReason` has only Completed,
  MaxTurns, MaxToolCalls, and Timeout, and none is added — CLI cancel is process
  termination and the daemon gateway's timeout kill already bounds the MCP/HTTP surface,
  so a cooperative cancellation token would be new orchestration with no consumer.

**Acceptance:**

- 3.1.1 - Deep module exists with daemon + direct + degraded arms. file: `crates/gwiki/src/commands/ask/deep.rs`.
- 3.1.2 - Off-route degrades to the standard ask payload with `deep_unavailable`; daemon arm passes max turns + the deep-specific four-capability readonly policy; deep citation check flags nonexistent pages. test: `crates/gwiki/src/commands/ask/deep.rs`.
- 3.1.3 - `deep` output keys covered by contract assertions constructed from the output model, including `stop_reason` (direct-mapped value, daemon-verbatim value, and missing daemon stop_reason → null; review round 7 WGC-DEEP-STOP-REASON-OUTPUT). test: `crates/gwiki/tests/cli_contract.rs`.
- 3.1.4 - `--deep` parses and rejects `--ai off`. test: `crates/gwiki/src/cli.rs`.
- 3.1.5 - Limit-exit contracts (enhancement E6, narrowed in review round 2 WGC-ASK-CANCEL,
  made route-accurate in review round 4 WGC-DEEP-LIMIT-OBSERVABILITY, made
  adapter-accurate in review round 5 WGC-DEEP-DAEMON-STOP-REASON-MATRIX, made uniform by
  the 3.3 normalization — user-directed fold after round 5): the direct arm pins all
  `StopReason` variants (Completed, MaxTurns, MaxToolCalls, Timeout) with
  warning/status, turns, tool_use_count, citations, and usage. The daemon arm surfaces
  the parsed `investigation.stop_reason` verbatim in `AskDeepOutput.stop_reason` (review
  round 7 WGC-DEEP-STOP-REASON-OUTPUT) and pins only the values the selected
  adapter produces after 3.3: `openai_compatible` and Claude emit
  `max_turns`/`max_tool_calls`/`completed`; grok emits `max_turns`/`completed`/null —
  absent or unrecognized CLI `stopReason` values propagate as null, never `completed`
  (review round 8 WGC-GROK-UNKNOWN-STOP-REASON); qwen emits
  `max_tool_calls`/`max_turns`/`timeout`/`completed` (review round 8
  WGC-QWEN-STDERR-ONLY-LIMITS / WGC-QWEN-EXIT55-AMBIGUITY: exit 53 maps to
  `max_turns`; exit 55 is shared by the tool-call and wall-clock budgets and is
  disambiguated by the CLI's own budget diagnostic into `max_tool_calls` or
  `timeout`); codex/droid are
  wall-time-bounded only and emit `completed` — deep never synthesizes an exit the
  adapter did not report. Daemon
  timeout is pinned as the existing AiError degradation/`--require-ai` error with
  partial metrics unavailable. Transport-boundary parsing pinned separately for BOTH
  optional fields (review round 9 WGC-OPTION-TURNS-PARSER-TEST-R9): `stop_reason`
  present value and missing/null → `None`; `turns` native count → `Some(n)` and
  missing/null → `None`, never `Some(0)` from absence.
  test: `crates/gwiki/src/commands/ask/deep.rs`. test: `crates/gcore/src/ai/generation/tests/daemon_agentic.rs`.
- 3.1.6 - Capability parity and mutation denial (review round 4 WGC-DEEP-TOOL-PARITY,
  route-name-accurate per review round 6 WGC-DEEP-TOOL-NAME-PARITY): the direct
  executor advertises and executes exactly `search_vault`/`read_document`/`backlinks`/`sources`
  and rejects unknown or mutating tool names; the deep daemon policy names exactly
  `search`/`read`/`backlinks`/`sources` with `allow_mutation: false`; the semantic
  capability mapping between the two name sets is pinned, and existing compile/upkeep
  direct consumers keep their tool names unchanged. test: `crates/gwiki/src/commands/vault_tools.rs`.
- 3.1.7 - Citation-mode and warning seam (review round 5 WGC-DEEP-SYNTHESIS-SEAM,
  warning token per review round 6 WGC-DEEP-WARNING-SEAM): `record_synthesis` with the
  excerpt-substring result preserves standard-ask unsupported-claim warnings; with the
  vault-page-existence result deep flags nonexistent `[[page]]` citations and does not
  false-flag existing pages read after round 0; `mark_ai_unavailable` accepts the
  explicit warning token — standard ask emits exactly `ai_unavailable`, deep emits
  exactly `deep_unavailable`, shared degraded_sources/status behavior and `--require-ai`
  error pinned for both tokens across synthesis.rs and deep.rs tests.
  test: `crates/gwiki/src/commands/ask/synthesis.rs`. test: `crates/gwiki/src/commands/ask/deep.rs`.
- 3.1.8 - `from_daemon_agentic` maps canonical daemon stop-reason strings into
  `StopReason` (completed/max_turns/max_tool_calls/timeout), propagates missing or
  unrecognized values as `None`, rejects known non-completed reasons with
  `Unavailable` exactly like `from_tool_loop`, and threads `Option` turns (a `None`
  daemon count is preserved, never defaulted); cases pinned: completed, max_turns,
  max_tool_calls, timeout — asserting `observability.stop_reason ==
  Some(StopReason::Timeout)` and rejection with `GenerationFailureCause::Unavailable`
  (review round 9 WGC-DAEMON-TIMEOUT-ACCEPTANCE-R9) — missing, and unknown (review
  round 8 WGC-DAEMON-LANE-STOP-REASON, WGC-SPAWN-TURN-PROVENANCE).
  test: `crates/gcode/src/commands/codewiki/text/generation.rs`.

### 3.2 Expose deep ask through daemon gateway, MCP, and HTTP [category: code] (depends: 3.1)

`kind: deliverable`

Target: `src/gobby/gwiki_gateway.py`, `src/gobby/mcp_proxy/tools/wiki.py`, `src/gobby/servers/routes/wiki.py`

Changes:
- `GwikiGateway.ask(…, deep: bool = False)` appends `--deep`; deep takes the generation
  timeout path (like llm=True). Gateway validation switches to
  `generation_requested = llm or deep` (review round 3 WGC-DEEP-IMPLIED-SYNTHESIS): the
  current guard (gwiki_gateway.py:188) rejects `ai`/`require_ai` whenever `llm` is
  false, which would bounce `deep=true` + `ai=…`/`require_ai=true` (with `llm` at its
  default false) before gwiki ever runs. `--ai`/`--require-ai` forward whenever
  generation is requested; `ai`/`require_ai` with NEITHER `llm` nor `deep` still raise.
- `wiki_ask` MCP tool + `GET /api/wiki/ask` gain `deep: bool = False`;
  `resolve_ask_timeout` treats generation_requested (llm OR deep) as generation-backed
  (keep the <300s invariant; wiki_ask is already in EXTENDED_TIMEOUT_TOOL_NAMES).

**Acceptance:**

- 3.2.1 - MCP schema + timeout routing for deep covered; `deep:true` with explicit `ai`/`require_ai` and `llm` omitted passes validation and forwards `--deep` with the AI flags. test: `tests/mcp_proxy/tools/test_wiki.py`.
- 3.2.2 - Gateway validates on `generation_requested = llm or deep` and maps deep to `--deep` with generation timeout. symbol: `GwikiGateway.ask`. file: `src/gobby/gwiki_gateway.py`.
- 3.2.3 - CLI contract mapping updated. test: `tests/test_cli_contracts.py`.
- 3.2.4 - HTTP route accepts `deep=true` with an explicit AI route/`require_ai` and no `llm`; `ai`/`require_ai` with neither `llm` nor `deep` still rejected (the gwiki CLI's `--deep --ai off` rejection stays pinned by 3.1.4). test: `tests/servers/routes/test_wiki_routes.py`.

### 3.3 Normalize daemon tool-chat adapter stop-reason reporting [category: code]

`kind: deliverable`

Target: `src/gobby/ai/_tool_chat_contracts.py`, `src/gobby/ai/_tool_chat_adapters.py`, `src/gobby/ai/_tool_chat_spawn.py`, `src/gobby/ai/_text_generation_adapters.py`, `tests/ai/test_tool_chat_adapters.py`, `tests/ai/test_tool_chat_spawn.py`, `tests/ai/test_text_generation.py`

Root cause (user-directed fold after review round 5 WGC-DEEP-DAEMON-STOP-REASON-MATRIX;
source claims corrected in review round 7 WGC-GROK-LIMIT-SOURCE and
WGC-QWEN-LIMIT-EXIT-PATH): daemon adapters report limit exits inconsistently, so
identical tool_chat requests stop differently per provider and consumers (deep ask, lane
observability) cannot trust `stop_reason`. `ClaudeToolChatAdapter.chat` threads
`budget_exhausted=runtime.budget_exhausted` into its result but hardcodes
`stop_reason="completed"` on the non-exception path (_tool_chat_adapters.py:312-326).
Grok's `_build_command` ALREADY passes `--max-turns` via `_resolve_max_turns(request,
default=30)` (_tool_chat_spawn.py:588, :602) with `request.max_turns` intentionally
taking precedence over `request.limits.max_turns` (_tool_chat_contracts.py:103-108) —
the gap is reporting only: `chat` hardcodes `completed` and never reads the CLI result
JSON's top-level `stopReason` field (present in the installed CLI's `--output-format
json` shape), while `parse_grok_session_signals` records tool-call count only, which the
adapter aliases to `turns`. Qwen forwards `--max-tool-calls` (:714-715), but the
installed CLI aborts limit overruns with dedicated exit codes — 55 for the tool-call
budget, 53 for `--max-session-turns` (documented in the CLI's bundled headless docs) —
and the shared `_run_cli_text_generation_command`
(_text_generation_adapters.py:146-197) raises on EVERY nonzero return before
`parse_qwen_stream` sees stdout, so a limit exit today surfaces as a RuntimeError, never
as a result; the adapter also fails to forward `--max-session-turns` at all. `codex
exec` and `droid exec` expose no turn/tool-call limit flags (verified against the
installed CLIs). Round 8 corrections, verified against the installed CLIs'
bundled source (WGC-QWEN-STDERR-ONLY-LIMITS, WGC-QWEN-EXIT55-AMBIGUITY,
WGC-SPAWN-TURN-PROVENANCE): qwen's stream-json mode DOES emit a terminal stdout
result event on limit aborts — `{type: "result", subtype: "error_during_execution",
is_error: true, num_turns, usage, error: {message}}`, with NO narrative `result`
text field — before the budget handler writes the diagnostic to stderr and exits;
exit 55 (`FatalBudgetExceededError`) covers BOTH `--max-tool-calls` AND
`--max-wall-time` (only `--max-session-turns` gets 53), distinguished solely by the
enforcer's message ("tool-call budget of N exceeded (--max-tool-calls)" vs
"wall-clock budget of Ns exceeded (--max-wall-time)"); and all four spawn adapters
alias `turns` to `tool_use_count` (_tool_chat_spawn.py:420, :546, :648, :764) even
though grok session records carry a distinct `turnCount` beside `toolCallCount`,
qwen terminal result events carry `num_turns` (success and error variants), and
droid's json result envelope carries `num_turns` (`numTurns` on its stream-json
`completion` event); codex exec emits no final turn count (its `num_turns` strings
are thread-rollback/sub-agent fields only).

Changes:
- Claude adapter: success path reports `"max_tool_calls" if runtime.budget_exhausted else
  "completed"` — the signal is already threaded; only the label lies today. `max_turns`
  via `ClaudeSDKMaxTurns` unchanged.
- Grok spawn: KEEP `_build_command`'s existing `_resolve_max_turns`-based `--max-turns`
  pass-through and its `request.max_turns` > `request.limits.max_turns` precedence
  untouched (review round 7 WGC-GROK-LIMIT-SOURCE — the prior text here would have
  regressed that contract). The reporting fix: parse the CLI result JSON's top-level
  `stopReason` alongside the existing `text`/`sessionId` reads and map ONLY verified
  values — `EndTurn` → `completed`, `MaxTurnRequests` → `max_turns` (the CLI
  serializes its ACP `StopReason` variants in PascalCase; `MaxTokens`, `Refusal`, and
  `Cancelled` also exist in the binary). Absent or unrecognized values propagate as
  `None` — `ToolChatResult.stop_reason` is already `str | None = None`
  (_tool_chat_contracts.py:130), so no contract edit is needed for it (review round
  9 WGC-TOOL-CHAT-RESULT-CONTRACT-R9 corrects the earlier "becomes optional"
  claim) — because missing provider
  data is no evidence of natural completion and the normalized contract reserves
  `completed` for verified natural finishes (review round 8
  WGC-GROK-UNKNOWN-STOP-REASON); all five observed strings plus the absent case are
  pinned in tests. NO stop-reason
  inference from `parse_grok_session_signals` tool-call counts — that helper records
  tool calls, not turns; it DOES additionally parse the session records' native
  `turnCount` for turn provenance (see the turn-provenance bullet).
- Qwen spawn: forward `--max-session-turns` resolved via `_resolve_max_turns` beside the
  existing `--max-tool-calls`/`--max-wall-time`. Give the shared runner in
  _text_generation_adapters.py a limit-aware path (review round 7
  WGC-QWEN-LIMIT-EXIT-PATH; corrected in review round 8) that, for caller-declared
  accepted exit codes, returns stdout, STDERR, and the exit code instead of raising —
  stderr carries the CLI's budget diagnostic and must survive into classification
  (review round 8 WGC-QWEN-EXIT55-AMBIGUITY); the Qwen adapter declares 55 and 53.
  Classification uses the exit code plus the CLI's own diagnostic, never a count
  heuristic: exit 53 → `max_turns`; exit 55 is shared by two budgets, so read the
  terminal stdout error result event's `error.message` (falling back to the stderr
  diagnostic) — the `--max-tool-calls` message → `max_tool_calls`, the
  `--max-wall-time` message → `timeout`, and an exit 55 with neither recognizable
  diagnostic raises naming the ambiguity (honest error, no fabricated reason).
  `parse_qwen_stream` additionally parses the error-variant terminal result event
  (review round 8 WGC-QWEN-STDERR-ONLY-LIMITS: limit aborts emit `is_error: true`
  events carrying `num_turns`/`usage`/`error.message` and NO `result` text), so a
  limit exit yields a typed limit-only result — content `None`, the mapped
  `stop_reason`, native `num_turns`, tool provenance, and usage — instead of raising
  for missing narrative text; the daemon route reports it and 3.1's consumers
  classify it honestly (degradation with the real stop_reason; error only under
  `--require-ai`). Natural completions parse exactly as today; every other nonzero
  exit keeps raising. NO tool-count-reaches-cap inference — a natural completion at
  exactly the cap is indistinguishable from a limit exit.
- Codex/Droid spawn: no CLI limit mechanism exists — the wall-time timeout stays the only
  bound; `completed` remains correct for natural finishes, and each adapter logs one
  warning per call that `request.limits` is unenforceable for the provider, so the gap is
  observable instead of silent. No fabricated stop reasons.
- Turn provenance (review round 8 WGC-SPAWN-TURN-PROVENANCE): `turns` stops aliasing
  `tool_use_count` in all four spawn adapters (:420, :546, :648, :764) and becomes
  optional end-to-end — the shared result contract changes with it (review round 9
  WGC-TOOL-CHAT-RESULT-CONTRACT-R9): `ToolChatResult.turns: int = 0`
  (_tool_chat_contracts.py:126) becomes `int | None = None`, and
  `ToolChatResult.text: str` (:121) becomes `str | None` so the qwen typed
  limit-only result carries no fabricated narrative — alongside the daemon route's
  investigation payload and the Rust carriers 3.1 threads. Grok parses the session records'
  native `turnCount` (distinct from `toolCallCount`) via
  `parse_grok_session_signals`; qwen takes `num_turns` from the terminal result
  event (success and error variants); droid takes its result envelope's `num_turns`
  (`numTurns` on the stream-json `completion` event); codex has no trustworthy final
  turn count and reports `None`. `tool_use_count` keeps its existing meaning
  everywhere. Tests include a fixture where turn and tool-call counts differ, so the
  de-aliasing is pinned.
- Contract after this change: every adapter reports `max_turns`/`max_tool_calls`/
  `timeout` whenever the limit exit is observable, `completed` only for verified
  natural completion, and `None` when the provider supplies no trustworthy stop
  signal; `turns` carries provider-native counts or `None`, never an alias — the
  matrix 3.1.5 pins.

**Acceptance:**

- 3.3.1 - Claude adapter reports `max_tool_calls` when the tool budget is exhausted and `completed` otherwise; `max_turns` via `ClaudeSDKMaxTurns` unchanged. test: `tests/ai/test_tool_chat_adapters.py`.
- 3.3.2 - Grok `_build_command` retains `--max-turns` with `request.max_turns` precedence over `request.limits.max_turns` pinned; result-JSON `stopReason` `EndTurn` reports `completed`, `MaxTurnRequests` reports `max_turns`, and `MaxTokens`/`Refusal`/`Cancelled`/absent/unrecognized propagate `None`; no tool-call-count stop-reason inference. test: `tests/ai/test_tool_chat_spawn.py`.
- 3.3.3 - Qwen command passes `--max-session-turns` and `--max-tool-calls`; exit 53 reports `max_turns`; exit 55 with the tool-call diagnostic reports `max_tool_calls`, with the wall-clock diagnostic reports `timeout`, and with neither raises naming the ambiguity; limit exits yield typed limit-only results (content `None`, native `num_turns`, usage) without requiring narrative text — pinned against the `ToolChatResult.text: str | None` contract (review round 9 WGC-TOOL-CHAT-RESULT-CONTRACT-R9); other nonzero exits raise. test: `tests/ai/test_tool_chat_spawn.py`.
- 3.3.4 - Shared runner accepts caller-declared exit codes and returns stdout + stderr + code for them while raising on all others; default behavior (no declared codes) unchanged for every other caller. test: `tests/ai/test_text_generation.py`.
- 3.3.5 - Codex/Droid adapters log the limits-unenforceable warning and keep `completed` on natural finish. test: `tests/ai/test_tool_chat_spawn.py`.
- 3.3.6 - `turns` is optional and provider-native across adapters: grok reports the session records' `turnCount`, qwen reports the result event's `num_turns`, droid reports its envelope count, codex reports `None`; a fixture where turn and tool-call counts differ pins the de-aliasing; the `ToolChatResult.turns: int | None = None` contract default is pinned (review round 9 WGC-TOOL-CHAT-RESULT-CONTRACT-R9); `tool_use_count` unchanged. test: `tests/ai/test_tool_chat_spawn.py`.

## P4: Diagram restoration

`kind: framing`

**Goal**: module pages get deterministic dependency (and where evidenced, call-sequence)
diagrams; every narrative chapter and concept page draws; diagram suppression is observable.

### 4.1 Typed DiagramOutcome and per-run diagram stats [category: code] (depends: 2.1, 3.1)

`kind: deliverable`

Target: `crates/gcode/src/commands/codewiki/diagram_compose.rs`, `crates/gcode/src/commands/codewiki/architecture_diagrams.rs`, `crates/gcode/src/commands/codewiki/build_parts/curated_content.rs`, `crates/gcode/src/commands/codewiki/build_parts/concepts/render.rs`, `crates/gcode/src/commands/codewiki/generation.rs`, `crates/gcode/src/commands/codewiki/run.rs`, `crates/gcode/src/commands/codewiki/types.rs`, `crates/gcode/src/commands/codewiki/io.rs`

Root cause: `compose_flowchart` (:552) returns `Option<String>` and its gate
(`is_sparse()` = <2 nodes || 0 edges, or no generator) yields silent `None` — never logged
anywhere, so near-zero emission went unnoticed.

Depends on 3.1 solely to serialize edits to
`build_parts/concepts/render.rs` — 3.1's Option-turns change rewrites that file's
lane-observability test literals, and this deliverable threads stats through the
same file (review round 9 WGC-RUST-OPTION-TURNS-TARGET-R9).

Changes:
- `compose_flowchart` returns `DiagramOutcome` {Emitted(String), SparseEvidence,
  NoGenerator, Rejected} — silent None becomes impossible at the type level.
- `DiagramStats` {emitted, sparse_evidence, no_generator, rejected} with
  `record(page_path, &outcome, &mut CodewikiProgress)`; persisted as optional
  `diagram_stats` on `CodewikiMeta` (types.rs:557) via DocSink. Thread through
  architecture_diagrams.rs, curated_content.rs, concepts/render.rs, generation.rs,
  run.rs. No render-version bump.
- Stats semantics (enhancement E7, re-keyed in review round 2 WGC-DIAGRAM-OBSERVABILITY):
  `DiagramStats` counts FINAL outcomes per diagram SLOT — (page, kind) with kind ∈
  {module_dependency, module_call_sequence, curated_flow} — not per page and not pass
  attempts. One log line per attempted slot (`diagram <page> [<kind>]: <outcome>`) naming
  the winning pass; the one-final-outcome rule collapses only fallback passes for the
  SAME slot (4.4), so an emitted dependency diagram can never mask a suppressed
  call-sequence slot on the same page; internal pass attempts stay out of the aggregate.

**Acceptance:**

- 4.1.1 - Composer returns the typed outcome; per-variant cases covered. symbol: `compose_flowchart`. test: `crates/gcode/src/commands/codewiki/diagram_compose.rs`.
- 4.1.2 - Build log carries one final `diagram <page> [<kind>]: <outcome>` line per attempted slot and `_meta/codewiki.json` carries `diagram_stats`. behavior: "diagram outcomes are observable per run" in `crates/gcode/src/commands/codewiki/`.
- 4.1.3 - Invariant: emitted + sparse_evidence + no_generator + rejected equals unique
  attempted diagram slots (page × kind) and matches final log lines. test: `crates/gcode/src/commands/codewiki/diagram_compose.rs`.

### 4.2 Deterministic module dependency flowcharts [category: code] (depends: 4.1)

`kind: deliverable`

Target: `crates/gcode/src/commands/codewiki/render/diagrams.rs`, `crates/gcode/src/commands/codewiki/render/pages.rs`, `crates/gcode/src/commands/codewiki/build_parts/modules.rs`, `crates/gcode/src/commands/codewiki/generation.rs`, `crates/gcode/src/commands/codewiki/types.rs`, `crates/gcode/src/commands/codewiki/stubs.rs`, `crates/gcode/src/commands/codewiki/architecture_diagrams.rs`, `crates/gcode/src/commands/codewiki/tests/modules.rs`, `crates/gcode/src/commands/codewiki/build_parts/curated_content/tests.rs`

Root cause: #17521 (commit cfb49261c) deleted the module-tier renderers
(`render_module_dependency_mermaid`, `bounded_module_dependency_edges`,
`aggregate_module_for_page`, `simplified_diagram_note`, MAX_MERMAID_HOPS/EDGES) and never
replaced them — `build_module_docs_with_filter` (modules.rs:27-220) uses graph_edges only
for prose. 375 pages, zero diagrams by construction.

Changes:
- Restore-and-adapt the deleted code from `git show cfb49261c^`: union Import+Call edges
  rolled up to module level (old code was Import-only; Rust module clusters often have call
  edges but few file-import edges), hops=2, edge cap 20, `is_valid_mermaid`-gated.
  Deterministic — no LLM, cannot hallucinate; evidence contract satisfied by construction
  and ~375 LLM calls avoided.
- `ModuleDoc.dependency_diagram: Option<String>`; rendered as `## Dependencies` with the
  "Simplified diagram: showing top N of M…" note when bounded (+ "; source graph was
  truncated." keyed off `CodewikiGraphAvailability::Truncated`, threaded via a new
  `graph_availability` param from generation.rs). Omit at 0 edges — logged via 4.1 stats.
- Diagram edges intentionally NOT in reuse keys (bounded, order-unstable FalkorDB sample;
  same staleness envelope as relationship-facts prose — documented at the builder).
- Determinism under reordering (enhancement E1): canonicalize and deduplicate stages,
  participants, and edges (stable sort) BEFORE ranking, BFS, and capping in the
  dependency, call-sequence (4.3), and containment (4.4) renderers, so equivalent graph
  inputs in different orders emit byte-identical mermaid and captions.
- RENDER_VERSION_MODULE stays at 20 in this deliverable (review round 6
  WGC-MODULE-RENDER-BUMP-ORDER): module reuse rejects only on exact version inequality
  (reuse.rs:139-145), so a bump here would let pages regenerated between 4.2 and 4.3
  be reused after 4.3 without the call-sequence section. The single 20 → 21 bump lands
  in 4.3 once both renderers exist; the new `## Dependencies` section reaches
  regenerated pages when that bump lands.
- Update struct-literal sites: stubs.rs:189, `crates/gcode/src/commands/codewiki/tests/modules.rs`,
  `crates/gcode/src/commands/codewiki/build_parts/curated_content/tests.rs`.
  `ModuleDoc` lives at types.rs:215-228; module-page section rendering at
  render/pages.rs:5-51 (review round 6 WGC-TARGET-COVERAGE-SWEEP). The
  `build_module_docs_with_filter` caller at generation.rs:304 passes the new
  `graph_availability` argument (review round 7 WGC-TARGET-COVERAGE-SWEEP; the
  tests/modules.rs:75 caller is already targeted).

**Acceptance:**

- 4.2.1 - Modules with edges render a valid dependency fence; 0 edges → no section; bounds produce the Simplified note; Truncated availability appends the truncation suffix. test: `crates/gcode/src/commands/codewiki/tests/modules.rs`.
- 4.2.2 - Renderer restored with union edge roll-up. symbol: `render_module_dependency_mermaid`. file: `crates/gcode/src/commands/codewiki/render/diagrams.rs`.
- 4.2.3 - Real-parser test extended with a deterministic dependency block. test: `crates/gcode/src/commands/codewiki/architecture_diagrams.rs::emitted_mermaid_blocks_pass_real_mermaid_parser`.
- 4.2.4 - Permutation-invariance fixture: same fact set in different input orders yields
  byte-identical mermaid and captions. test: `crates/gcode/src/commands/codewiki/tests/modules.rs`.

### 4.3 Deterministic module call-sequence diagrams [category: code] (depends: 4.2)

`kind: deliverable`

Target: `crates/gcode/src/commands/codewiki/render/diagrams.rs`, `crates/gcode/src/commands/codewiki/render/pages.rs`, `crates/gcode/src/commands/codewiki/types.rs`, `crates/gcode/src/commands/codewiki/mod.rs`, `crates/gcode/src/commands/codewiki/tests/modules.rs`, `crates/gcode/src/commands/codewiki/tests/architecture.rs`, `crates/gcore/src/vault/mermaid.rs`

No sequence emitter exists anywhere; the validator already accepts `sequenceDiagram`
(crates/gcore/src/vault/mermaid.rs:12). Call edges are verified facts — an LLM composer
would add prompt+verification machinery for a diagram fully determined by evidence.

Changes:
- `render_module_call_sequence` adapted from the deleted `render_module_call_mermaid` +
  `bounded_component_edges`: seeds = in-page components with no in-page incoming call edge
  (fallback: all); emit ONLY when BFS finds a call chain of depth ≥2 (flat stars get
  nothing — no fake ordering); caps 8 participants/12 messages/2 hops with the Simplified
  note; caption "Static call sequence — indexed call edges ordered by call depth…; not a
  recorded execution trace." `ModuleDoc.call_sequence_diagram`; `## Call sequence` section.
- Update the stale comment on the architecture aggregate page's sequence-absence pin
  (tests/architecture.rs:488) — the pin stays scoped to that page.
- `RENDER_VERSION_MODULE` 20 → 21 lands HERE, not in 4.2 (review round 6
  WGC-MODULE-RENDER-BUMP-ORDER; constant at crates/gcode/src/commands/codewiki/mod.rs:86-93,
  keyed through `render_version_for_path`): the single module-tier bump happens only
  after BOTH the dependency and call-sequence renderers exist, regenerating ~375 module
  briefs exactly once with both sections.

**Acceptance:**

- 4.3.1 - Depth≥2 chain emits a valid sequence fence; flat star emits nothing; caps produce the Simplified note. test: `crates/gcode/src/commands/codewiki/tests/modules.rs`.
- 4.3.2 - Sequence renderer exists. symbol: `render_module_call_sequence`. file: `crates/gcode/src/commands/codewiki/render/diagrams.rs`.
- 4.3.3 - Validator fixture matches the emitted sequence body shape. test: `crates/gcore/src/vault/mermaid.rs`.
- 4.3.4 - Permutation-invariance fixture for the sequence renderer (per 4.2's
  canonicalization rule). test: `crates/gcode/src/commands/codewiki/tests/modules.rs`.
- 4.3.5 - Masking regression (review round 2 WGC-DIAGRAM-OBSERVABILITY): a module whose
  dependency slot emits while its eligible call-sequence slot is suppressed records BOTH
  slot outcomes in stats and log lines. test: `crates/gcode/src/commands/codewiki/tests/modules.rs`.
- 4.3.6 - The module render-version bump is carried by this deliverable alone: 4.2
  leaves `RENDER_VERSION_MODULE` at 20 and this deliverable moves it to 21.
  file: `crates/gcode/src/commands/codewiki/mod.rs`.

### 4.4 Curated two-pass evidence and containment fallback diagrams [category: code] (depends: 4.1, 4.3)

`kind: deliverable`

Target: `crates/gcode/src/commands/codewiki/build_parts/curated_content.rs`, `crates/gcode/src/commands/codewiki/mod.rs`, `crates/gcode/src/commands/codewiki/build_parts/curated_content/tests.rs`

Depends on 4.3 to serialize edits to `codewiki/mod.rs`, where both tier render-version
constants live (review round 6 WGC-UNSERIALIZED-SHARED-FILES).

Root cause: `curated_flow_diagram` (:637-687) builds evidence only from prose arrow chains
(rare) + cross-member call/import edges (curated_flow_evidence :696-748); broad semantic
groupings (all 10 narrative chapters, 10/11 concept pages) have no cross-member edges →
sparse → silent None.

Changes:
- Pass 2 (when pass-1 evidence is sparse): re-run `resolve_flow_stages` at child
  granularity — expand each member module into direct children (child_modules +
  direct_files), cap 10 stages ranked by direct-file count; recompute evidence (children
  almost always have edges); caption names the roll-up granularity.
- Pass 3 (EVERY non-Emitted final outcome from passes 1–2 — sparse evidence, no
  generator, or model output Rejected by verification; review round 2
  WGC-DIAGRAM-REJECT-FALLBACK): deterministic containment structure map
  (`flowchart TD`, page → members → children, cap 12 nodes). Caption is REASON-AWARE
  (review round 2 WGC-DIAGRAM-FALLBACK-CAPTION — a fixed no-edges claim would be false
  on the NoGenerator/Rejected branches, violating the evidence-honesty constraint):
  each begins "Structure map — containment from the module tree;" then per triggering
  outcome — SparseEvidence: "no cross-member call/import edges were found in the
  index."; NoGenerator: "no diagram generator was available."; Rejected: "the generated
  flow diagram failed verification and was discarded." — and every variant ends "This
  shows structure, not runtime flow." Factual (module tree), no LLM — honesty preserved
  while guaranteeing every narrative chapter and concept page draws, including when a
  non-sparse pass produces output that fails verification.
- Record one final DiagramOutcome per curated_flow slot; progress line names which pass won.
- `RENDER_VERSION_CURATED` 21 → 22 (constant in codewiki/mod.rs; ~21 curated pages
  regenerate).

**Acceptance:**

- 4.4.1 - Sparse top-level page with child edges gets a pass-2 diagram (scripted generator); edge-free page gets the containment map with the SparseEvidence caption (pinned text) and no LLM call; map bounded at 12 nodes; all fences pass `is_valid_mermaid`. test: `crates/gcode/src/commands/codewiki/build_parts/curated_content/tests.rs`.
- 4.4.2 - Two-pass + fallback implemented. symbol: `curated_flow_diagram`. file: `crates/gcode/src/commands/codewiki/build_parts/curated_content.rs`.
- 4.4.3 - Containment map is permutation-invariant (canonicalized stages/nodes) and stats
  record one final per-slot outcome naming the winning pass. test: `crates/gcode/src/commands/codewiki/build_parts/curated_content/tests.rs`.
- 4.4.4 - Invalid-generator-output regression (review round 2 WGC-DIAGRAM-REJECT-FALLBACK):
  a scripted generator returning unverifiable mermaid is Rejected and the page still
  receives the containment fallback carrying the Rejected caption (pinned text — asserts
  it does NOT claim no edges were found), with exactly one final slot outcome recorded;
  a NoGenerator branch case pins its caption likewise.
  test: `crates/gcode/src/commands/codewiki/build_parts/curated_content/tests.rs`.

## P5: Agent export pipeline

`kind: framing`

**Goal**: outputs/pages JSON, graph.jsonld, llms.txt, and llms-full.txt stay fresh
automatically, and health flags them when they are not.

### 5.1 Daemon scheduled exports job and gateway methods [category: code] (depends: 3.2)

`kind: deliverable`

Target: `src/gobby/wiki/scheduled_jobs.py`, `src/gobby/gwiki_gateway.py`, `tests/wiki/test_scheduled_jobs.py`

Depends on 3.2 to serialize edits to `GwikiGateway` (review round 6
WGC-UNSERIALIZED-SHARED-FILES).

Root cause: `gwiki export pages` (exports/pages.rs:27-57) and file-writing `gwiki graph`
(which since 2026-07-10 emits graph.jsonld/llms.txt/llms-full.txt via
`export_agent_artifacts`, exports/graph.rs:81-113) are manual-only; the daemon's sole graph
call passes `--stdout`, which early-returns before writing artifacts
(commands/graph.rs:57-69). outputs/pages is 9 days stale with ghost pages.

Changes:
- Gateway: `export_pages()` → `gwiki export pages`; `graph_artifacts()` → `gwiki graph`
  (no `--stdout`). Both join `SERIALIZED_WRITE_COMMANDS`; existing stdout `graph()` method
  untouched. Writes land only under outputs/** (machine artifacts, not indexed pages) —
  read-only-for-index treatment justified; no WikiUpdateCoordinator routing.
- New scheduled spec tuple `("exports", "Scheduled agent export refresh",
  WIKI_EXPORTS_INTERVAL_SECONDS = 6h, None, create_wiki_exports_handler(...))` following
  the health-handler shape; per-step degrade (one failing step still runs the other, history
  records per-step ok/error; raise only when both fail). Registration machinery picks the
  tuple up unchanged.
- Add both methods to `WikiGatewayProtocol` (scheduled_jobs.py:69-101).

**Acceptance:**

- 5.1.1 - Exports job registered per scope; handler calls both gateway methods; single-step failure degrades, double failure raises; history shape covered. test: `tests/wiki/test_scheduled_jobs.py`.
- 5.1.2 - Gateway methods exist and are serialized. symbol: `GwikiGateway.graph_artifacts`. file: `src/gobby/gwiki_gateway.py`.

### 5.2 Health stale-exports check and ai-readme freshness note [category: code]

`kind: deliverable`

Target: `crates/gwiki/src/health.rs`, `crates/gwiki/src/vault.rs`

Changes:
- Per-artifact evaluation (enhancement E3): `HealthReport.stale_exports` evaluates each
  artifact family independently — `outputs/pages/`, `outputs/graph.jsonld`,
  `outputs/llms.txt`, `outputs/llms-full.txt` — reporting the missing/stale artifact
  NAMES, so a refreshed pages export can never mask a stale or missing graph/llms
  artifact from the partially-failed job step.
- Staleness rule per artifact: "missing" when absent; "stale" when the newest vault .md
  mtime exceeds that artifact's mtime by `EXPORT_STALENESS_SLACK_SECONDS = 24h` (4× the
  6h cadence; health stays daemon-config-agnostic). Wire into report build + text render.
- One-sentence `AI_README_TEMPLATE` update (vault.rs:75-77): the daemon refreshes
  outputs/ on a schedule.

**Acceptance:**

- 5.2.1 - Missing artifacts → "missing" with names; fresh → None; page newer than an artifact beyond slack → "stale" naming that artifact. test: `crates/gwiki/src/health.rs`.
- 5.2.2 - Check implemented and rendered. symbol: `stale_exports`. file: `crates/gwiki/src/health.rs`.
- 5.2.3 - Single-step-success fixture: fresh pages export + stale graph artifacts →
  graph/llms named stale; and the converse. test: `crates/gwiki/src/health.rs`.

## P6: Commit anchoring and concept hygiene

`kind: framing`

**Goal**: pages say which commit generated them and when (in human-readable local time), a
CI diff mode consumes those stamps, and the knowledge concept space stops accumulating junk
and undetected duplicates.

### 6.1 Commit stamping in codewiki output [category: code] (depends: 4.3, 3.1)

`kind: deliverable`

Target: `crates/gcode/src/commands/codewiki/run.rs`, `crates/gcode/src/commands/codewiki/io.rs`, `crates/gcode/src/commands/codewiki/types.rs`, `crates/gcode/src/commands/codewiki/doc_paths.rs`, `crates/gcode/src/commands/codewiki/text/frontmatter.rs`, `crates/gcode/src/commands/codewiki/truth_digest.rs`, `crates/gcode/src/commands/codewiki/tests/reuse.rs`, `crates/gcode/src/commands/codewiki/tests/truth_digest.rs`

Depends on 4.3 to serialize edits to the run.rs/io.rs/types.rs files shared with the
diagram deliverables (review round 6 WGC-UNSERIALIZED-SHARED-FILES); metadata structs
live at types.rs:557-637 and the refresh helpers at doc_paths.rs:93-114 (review round 6
WGC-TARGET-COVERAGE-SWEEP). Depends on 3.1 solely to serialize edits to
text/frontmatter.rs, which 3.1 also touches for the `Option` turns field (review
round 8 WGC-SPAWN-TURN-PROVENANCE fold; same shared-file rule as the 6.2 → 4.4
edge).

Root cause: no git SHA anywhere in output — CodewikiMeta/CodewikiDocMeta/
CodewikiIndexSnapshot (types.rs:557-634, 797-819) are content-hash only; truth_digest has
wall-clock only; git is used only for `--since` selection (run.rs:527-549) and blame.

Changes:
- `capture_commit_stamp(root) -> Option<CommitStamp {sha, dirty}>` beside
  `git_changed_files` (rev-parse HEAD + `git status --porcelain` non-empty ⇒ dirty);
  errors → None, never fail the run; captured once in `run()`.
- Stamp at the DocSink write site only (`persist_with_ai_outcome`, write branch):
  `stamp_commit`/`strip_commit_lines` frontmatter helpers insert `commit:` (+
  `commit_dirty: true`) and a human-readable `generated: <local time with UTC offset>`
  line (Josh, 2026-07-18: files must show UTC or local time for human readers — prefer
  local) before the closing `---`. Reused pages keep their ORIGINAL commit —
  honest "generated at commit X" semantics; the unchanged branch already clones previous
  meta, zero extra code. Make `content_sensitive_target_matches` and
  `refresh_doc_if_needed` (doc_paths.rs:93-114) commit-line-insensitive so content-keyed
  docs don't churn every commit; normalization-drift rewrites re-apply the file's original
  stamp.
- NO render-version bump: a bump would force ~2,988 AI-lane regenerations for a cosmetic
  backfill; there is no frontmatter-only rewrite lane (reuse is byte-for-byte or full
  rebuild). Pages acquire `commit:` as they naturally regenerate.
- Mirror fields on `CodewikiDocMeta`, `CodewikiMeta`, and the truth digest
  (`build_truth_digest` gains the stamp param); the existing `build_truth_digest`
  call sites at tests/truth_digest.rs:31, :64, :73 update to the new signature
  (review round 8 WGC-TARGET-COVERAGE-SWEEP-R8).

**Acceptance:**

- 6.1.1 - Stamped page with unchanged sources is reused (not rewritten) at a different HEAD; unchanged pages keep the original commit in `_meta/codewiki.json`; no churn on no-change re-run. test: `crates/gcode/src/commands/codewiki/tests/reuse.rs`.
- 6.1.2 - Capture helper exists with dirty detection and non-git-root None. symbol: `capture_commit_stamp`. file: `crates/gcode/src/commands/codewiki/run.rs`.
- 6.1.3 - Round-trip: `strip_commit_lines(stamp_commit(x)) == x` (covering both `commit:` and `generated:` lines) and stamped output survives markdown normalization. test: `crates/gcode/src/commands/codewiki/text/frontmatter.rs`.
- 6.1.4 - Truth digest carries commit + dirty, omitted for non-git roots; every `build_truth_digest` call site updated (review round 8 WGC-TARGET-COVERAGE-SWEEP-R8). file: `crates/gcode/src/commands/codewiki/truth_digest.rs`. test: `crates/gcode/src/commands/codewiki/tests/truth_digest.rs`.

### 6.2 Add gcode codewiki --compare-to diff summary [category: code] (depends: 6.1, 4.4)

`kind: deliverable`

Target: `crates/gcode/src/cli.rs`, `crates/gcode/src/commands/codewiki/compare.rs` (new), `crates/gcode/src/commands/codewiki/mod.rs`, `crates/gcode/src/commands/codewiki/tests/incremental.rs`

Implementation lands in a new `compare.rs` module beside run.rs, wired through
codewiki/mod.rs (review round 6 WGC-TARGET-COVERAGE-SWEEP — a directory is not an
implementation target). Depends on 4.4 solely to serialize edits to codewiki/mod.rs
(review round 7 WGC-UNSERIALIZED-SHARED-FILES: 4.4 bumps `RENDER_VERSION_CURATED` in the
same file, and without the edge both leaves are dispatchable concurrently after 4.3).

CI diff mode consuming 6.1's stamps:
`gcode codewiki --compare-to <ref>[:<meta-path>]`, where `<ref>` is a Git ref in
the source repository and `<meta-path>` is an optional repository-relative path
inside that Git tree. Baseline definition (review round 2
WGC-COMPARE-BASELINE): omitting the path loads the output-relative
`_meta/codewiki.json` snapshot (`git show <ref>:<output-rel>/_meta/codewiki.json`);
an explicit path addresses publication trees whose vault root differs from the
source worktree.

In this repository, generated source-worktree output stays ignored under
`wiki/`, so current metadata is `wiki/_meta/codewiki.json` and source history
does not track the vault. The pre-push publication flow mirrors the contents of
that directory into the root of the orphan `wiki` branch, where committed
metadata is addressed as `wiki:_meta/codewiki.json`. CI consumers fetch the
remote publication branch first with
`git fetch origin wiki:refs/remotes/origin/wiki`, then compare against
`origin/wiki:_meta/codewiki.json`.

The command validates the baseline before use and diffs its path-keyed doc
metadata (commit stamps + source hashes) against the current snapshot without
regenerating pages. `removed` comes from baseline keys absent in the current
snapshot — the committed baseline snapshot is the complete data source;
current-page stamps alone cannot identify removed docs. Absent or malformed
baseline metadata at the ref is a DISTINCT error exit (separate from
ref-resolution failure), never an empty no-change report. Read-only; exit 0 on
success. Scope deliberately minimal — a reporting consumer, not a second
regeneration driver (`--since` already drives regeneration).

Output contract (enhancement E5), pinned for CI consumers: JSON with base/current commit
metadata; path-sorted `added`/`removed`/`changed` record arrays; dirty and unstamped pages
explicitly represented (unstamped ⇒ `commit: null`, never silently dropped). Golden
fixtures per state (no-change, added, removed, changed, unstamped, dirty, bad-ref,
absent/malformed baseline metadata), committing BOTH the baseline and current snapshots.

**Acceptance:**

- 6.2.1 - `--compare-to` emits a changed-docs summary without writing pages. test: `crates/gcode/src/commands/codewiki/tests/incremental.rs`.
- 6.2.2 - Flag wired as `GIT_REF[:META_PATH]` in the codewiki CLI. file:
  `crates/gcode/src/cli.rs`.
- 6.2.3 - Golden fixtures commit baseline + current snapshots and pin the deterministic
  JSON contract incl. unstamped/dirty pages, removed-doc detection from the baseline
  snapshot, and the bad-ref vs absent/malformed-baseline vs no-change distinctions.
  test: `crates/gcode/src/commands/codewiki/tests/incremental.rs`.
- 6.2.4 - Source-worktree metadata at `wiki/_meta/codewiki.json` compares
  against publication-branch metadata at `_meta/codewiki.json`; invalid
  explicit paths are rejected and comparison leaves Git status clean.

### 6.3 Concept worthiness gate and recurring archive pass [category: code]

`kind: deliverable`

Target: `crates/gwiki/src/links.rs`, `crates/gwiki/src/upkeep.rs`, `crates/gwiki/src/librarian.rs`, `crates/gwiki/src/librarian/semantic.rs`

Root cause: the only gates between a `[[token]]` and a concept page are `is_entity_key`
(non-empty, no '/' — links.rs:18-20) and mentions ≥2 (upkeep.rs:42,332) — no stopwords, no
quality filter → awk/page/log/action/designconstraint802/issue861/task-16289 all minted
pages. Near-dup checking runs only at new-cluster creation and skips entirely when Qdrant
is down.

Changes:
- `is_concept_worthy(key)` in links.rs beside the structural predicate. Ordered
  deterministic rules: reject len<2, bare numerics, and keys starting with a
  non-alphanumeric character (structural artifacts like `_context`); artifact-ID patterns
  restricted to explicit artifact-like prefix families + digits
  (task/issue/pr/bug/ticket/designconstraint, with or without a separator — catches
  designconstraint802, issue861, task-16289). NO generic trailing-digit rule (review
  round 2 WGC-CONCEPT-DIGIT-RULE: a trailing digit-run ≥3 test would junk legitimate
  technical concepts sha256, x509, iso8601, rfc3339 — and via the every-match-key
  archive rule, archive their pages); `-concept`/`-page` suffixes (concept-ness is
  positional, never part of a name); a hardcoded ~50-entry generic-word stoplist (page,
  log, action, error, file, list, item, note, plan, status, and generic unix text tools
  awk/sed/grep, …) — deterministic and testable, deliberately excluding vault-real words
  (session, daemon, vault, agy).
- Apply at upkeep's candidate-formation gate (:332), librarian broken-link classification
  (librarian.rs:418), and semantic gap-scan cluster formation
  (`unresolved_link_clusters`, librarian/semantic.rs:298-325) — junk neither mints pages
  nor generates proposals.
- Recurring `archive_unworthy_concepts` step in `run_with_clock` after `govern_candidates`:
  concept pages whose every match key fails the predicate get
  `apply_lifecycle_transition(Archived)` (reuses the #17727 lifecycle; idempotent;
  reversible; reported via `UpkeepReport.unworthy_archived`, dry-run aware). Fuzzy merges
  stay proposal-only via existing librarian proposals.
- Review round 2 (WGC-CONCEPT-AGY): the AGY concept page is RETAINED — agy is the
  Antigravity CLI, a real ghook install target (`--cli=agy`,
  src/gobby/cli/installers/agy.py) that round-1 triage wrongly called junk. NO manual
  deletions remain in this plan: `awk` falls to the stoplist and `_context` to the
  leading non-alphanumeric rule, so both archive through the auditable lifecycle pass
  with recorded reasons. Inbound links to archived pages surface through the librarian
  broken-link classification (which shares the predicate) for cleanup, and regenerated
  indexes stop listing archived pages.

**Acceptance:**

- 6.3.1 - Junk keys never form candidates; worthy keys still cluster; archive pass archives pattern junk, leaves fts5/bm25/falkordb, no-ops on re-run, and reports in dry-run. test: `crates/gwiki/src/upkeep.rs`.
- 6.3.2 - Worthiness predicate with table-driven cases: positive cases include sha256,
  x509, iso8601, rfc3339, fts5, bm25, http2; junk cases include designconstraint802,
  issue861, task-16289, `_context`, awk. symbol: `is_concept_worthy`. file: `crates/gwiki/src/links.rs`.
- 6.3.3 - Librarian gates share the predicate. file: `crates/gwiki/src/librarian.rs`.
- 6.3.4 - Auditable reasons (enhancement E8): the predicate exposes a compact rejection
  reason; dry-run and applied upkeep reports list page/key/reason per archived candidate,
  identical across dry-run and application, idempotent on re-run. test: `crates/gwiki/src/upkeep.rs`.
- 6.3.5 - Real-vault junk coverage (review round 2 WGC-CONCEPT-AGY): `awk` (stoplist) and
  `_context` (leading non-alphanumeric) archive via rules with recorded reasons while
  `agy` stays active; archived pages' inbound links are reported through the librarian
  broken-link path and regenerated indexes exclude archived pages. test: `crates/gwiki/src/upkeep.rs`.

### 6.4 Alias- and prefix-aware duplicate concept detection in health [category: code] (depends: 5.2, 6.3)

`kind: deliverable`

Target: `crates/gwiki/src/health.rs`, `crates/gwiki/src/librarian.rs`, `crates/gwiki/src/librarian/semantic.rs`

Root cause: `duplicate_concepts` (health.rs:745-765) flags only exact lowercased-title
collisions — falkor/falkordb and claude-concept/claudecode pass silently while the
librarian's 0.90-cosine scan misses thin pages.

Changes:
- Rebuild the check on shared `page_match_keys` (lint.rs:110 — stem + title + aliases) so
  alias collisions flag; add alphanumeric-stripped proper-prefix pairs with prefix ≥5
  (catches falkor/falkordb, session/sessionmanager). No edit distance — its only known
  extra catch dies to 6.3's `-concept` rule.
- Suppress known-distinct pairs by reusing `load_distinct_pairs` (librarian/semantic.rs:203).
  `semantic` is a PRIVATE module of librarian (`mod semantic;`, librarian.rs:19), so a
  pub(crate) item alone stays unreachable from health.rs (review round 3
  WGC-DISTINCT-PAIRS-VISIBILITY): make the fn pub(crate) AND re-export it from
  librarian.rs (`pub(crate) use semantic::load_distinct_pairs;`) — least visibility
  surface, the module itself stays private; health.rs consumes the re-export. Add
  `reason` field (exact_title | shared_key | title_prefix) to `DuplicateConcept`,
  rendered in text output.
- Depends on 5.2 to serialize edits to health.rs and on 6.3 to serialize edits to
  librarian.rs/librarian/semantic.rs (review round 6 WGC-UNSERIALIZED-SHARED-FILES).

**Acceptance:**

- 6.4.1 - Shared-alias pages flagged; falkor/falkordb flagged with reason title_prefix; distinct-pairs entries suppress; exact-title behavior preserved. test: `crates/gwiki/src/health.rs`.
- 6.4.2 - Reworked check with reasons, consuming `load_distinct_pairs` via the `pub(crate)` re-export in librarian.rs. symbol: `duplicate_concepts`. file: `crates/gwiki/src/health.rs`.

### 6.5 Render human-readable local timestamps in gwiki page bodies [category: code]

`kind: deliverable`

Target: `crates/gwiki/src/support/time.rs`, `crates/gwiki/src/recap.rs`, `crates/gwiki/src/citations.rs`, `crates/gwiki/src/log.rs`

Root cause (Josh, 2026-07-18): gwiki knowledge pages render citation timestamps verbatim
as `Fetched: unix-ms:<epoch>` — `citations.rs:70` pushes `entry.fetched_at` raw. Agent-
parseable, useless for human readers; codewiki pages meanwhile show no dates at all
(fixed by 6.1's `generated:` stamp). Files must display UTC or local time — prefer local.

Changes:
- Shared owner decided (review round 2 WGC-TIMESTAMP-SHARED-OWNER): hoist the
  multi-format parsing now private in recap.rs (`parse_instant`, recap.rs:281) into
  `crates/gwiki/src/support/time.rs` as a precision-aware parsed value —
  Instant(DateTime<Utc>) | DateOnly | DatePrefix | Unparseable — plus the offset-injected
  formatter below. DateOnly = the trimmed input IS exactly a bare `YYYY-MM-DD`;
  DatePrefix (review round 3 WGC-TIMESTAMP-PREFIX-CONTRACT) = a valid `YYYY-MM-DD`
  prefix with trailing annotation — recap.rs:292 slices `value.get(..10)` and recap's
  regression (recap.rs:703-708) pins `2026-07-04 (approximate)` parsing, so the shared
  parser must retain prefix acceptance, carrying the parsed date AND the original
  string. recap.rs consumes the shared parser, converting DateOnly and DatePrefix to
  midnight UTC at its OWN call site (recency ordering only, never display), preserving
  its behavior.
- In the citation renderer, parse `entry.fetched_at` with that shared parser
  (`unix-ms:<millis>` | RFC 3339 | bare `YYYY-MM-DD`) into the PRECISION-AWARE value
  (review round 2 WGC-TIMESTAMP-DATE). Instant-precision inputs (unix-ms, RFC 3339) render
  `Fetched: <YYYY-MM-DD HH:MM ±HH:MM> (unix-ms:<epoch>)` — local time with UTC offset as
  the primary human display, raw epoch retained in parentheses for agents. Date-only
  inputs (bare `YYYY-MM-DD`) render unchanged as `YYYY-MM-DD`: converting a bare date to
  midnight UTC and reformatting in a negative local offset would display the previous
  calendar day and invent precision. DatePrefix and Unparseable values render VERBATIM —
  the full original string including its annotation (`2026-07-04 (approximate)`) is
  data, never dropped or truncated to the date. The shared formatter takes the UTC
  offset as a parameter — production passes the local offset; tests inject fixed
  positive AND negative offsets.
- Sweep other body-rendered raw timestamps for the same treatment (log.md renderer emits
  `- unix-ms:<epoch> …` lines — log.rs:329) using the same shared formatter. Frontmatter
  and JSON exports keep machine formats unchanged.

**Acceptance:**

- 6.5.1 - Citation blocks render local time with offset for instant-precision values,
  epoch retained in parentheses; bare `YYYY-MM-DD` renders date-only (no invented time);
  annotated date-prefix values (`2026-07-04 (approximate)`) and unparseable values
  render verbatim with the full original string preserved; all accepted formats covered
  under a fixed NEGATIVE UTC offset (previous-calendar-day guard) and a positive one.
  symbol: `render_citations`. test: `crates/gwiki/src/citations.rs`.
- 6.5.2 - Precision-aware parser + formatter live in the shared support module with
  offset injected as a parameter; recap.rs, citations.rs, and the log renderer all
  consume it (no duplicated parsing); recap recency behavior unchanged, including the
  annotated-prefix acceptance regression (`2026-07-04 (approximate)` still parses to its
  date for ordering). file: `crates/gwiki/src/support/time.rs`. test: `crates/gwiki/src/recap.rs`.

## V2 End-to-End Verification

`kind: verification`

- Scoped tests: `cargo test -p gobby-core -p gobby-wiki -p gobby-code`;
  `GOBBY_TEST_PROTECT=1 uv run pytest tests/wiki/ tests/mcp_proxy/tools/test_wiki.py
  tests/ai/test_tool_chat_adapters.py tests/ai/test_tool_chat_spawn.py
  tests/ai/test_text_generation.py -v`.
- Rebuild + reinstall `~/.gobby/bin/{gcode,gwiki}`; restart daemon.
- Ask: MCP `wiki_ask` with the exact query that crashed ("How does the MCP proxy's
  progressive tool discovery work and why does it exist?", llm:true) returns a synthesized
  cited answer; INDEX/_ownership absent from unrelated top-10s; no 0-char evidence
  excerpts; `deep:true` on a multi-hop question returns a cited report with turns/usage.
- Diagrams: scoped codewiki build over crates/gcode/src/search asserts most module pages
  carry Dependencies, chain-bearing modules carry Call sequence, every concept/narrative
  page draws (flow or structure map), and `_meta/codewiki.json` diagram_stats matches the
  per-page build-log lines; mermaid-cli parser test green.
- Exports: trigger the exports job; outputs/pages matches the page tree (no build_backend
  ghosts); graph.jsonld/llms.txt/llms-full.txt exist; health stale_exports is None.
- Freshness: regenerated pages carry `commit:` = HEAD; unchanged pages keep their original;
  `--compare-to` lists exactly the changed docs.
- Hygiene: upkeep archives pattern junk and leaves fts5/bm25; health flags falkor/falkordb.
- Full-repo wiki regeneration; spot-read narrative chapters for diagrams + honest captions.

## V1 Plan Changelog

`kind: verification`

**Round 1** `kind: enhancement`

- enhancer_run: 6e42d64e-685a-42d2-842f-fb5a98307297
- enhancer_session: 98530752-9ddb-4b8b-b321-d76c46021a23
- converged: false
- suggestions_presented: 9
- accepted:
  - E1 / better / canonicalize+dedup diagram inputs; permutation-invariance fixtures (4.2-4.4)
  - E2 / better / evidence↔result alignment by carried provenance (1.2)
  - E3 / better / per-artifact export staleness incl. llms.txt; slack fixed to 24h=4×6h (5.2)
  - E4 / better / pg_search-backed sanitizer regression with the real crash query (1.1)
  - E5 / better / pinned --compare-to JSON contract with golden fixtures (6.2)
  - E6 / better / deep-ask limit-exit and cancellation contract tests (3.1)
  - E7 / better / DiagramStats = final per-page outcomes + sum invariant (4.1)
  - E8 / better / per-page rejection reasons in archive dry-run/reports (6.3)
  - E9 / better / lane-name sweep extended repo-wide with historical allowlist (2.1)
- declined: none
- resolution_notes: All nine folded into their sections with new acceptance items (1.1.5,
  1.2.4, 3.1.5, 4.1.3, 4.2.4, 4.3.4, 4.4.3, 5.2.3, 6.2.3, 6.3.4; 2.1.1 amended). Josh
  additionally directed (2026-07-18): human-readable timestamps in page bodies — added
  `generated:` local-time stamp to 6.1 and new deliverable 6.5 (local-time citation/log
  rendering in gwiki, raw epoch retained). Enhancer reported converged:false at the
  1-round cap.

**Round 2** `kind: verification`

- reviewer_run: 1ae0aec8-892f-463d-acfb-59fa09188506
- reviewer_session: "#9124" (40b32f2d-9c85-4788-b7e1-7e4fcfdb2e6b)
- review_round: 1 of max 3
- verdict: needs_review
- findings: 6 blocking — WGC-ASK-CANCEL (3.1.5 demanded cancellation exits that
  `StopReason` cannot produce), WGC-DIAGRAM-OBSERVABILITY (per-page stats mask a
  suppressed second diagram slot), WGC-DIAGRAM-REJECT-FALLBACK (Rejected outcomes
  escaped the containment fallback, breaking the every-page-draws guarantee),
  WGC-COMPARE-BASELINE (no defined baseline doc-set source; removed docs undetectable),
  WGC-CONCEPT-AGY (agy is the real Antigravity CLI concept, wrongly slated for manual
  deletion outside the auditable lifecycle), WGC-TIMESTAMP-DATE (bare dates reformatted
  via midnight UTC render the previous calendar day in negative offsets).
- resolution_notes: All six folded in. 3.1 scope-outs now name external cancellation with
  rationale and 3.1.5 narrowed to max-turn/max-tool-call/timeout — DIVERGES from the
  reviewer's suggested resolution (adding cancellation machinery) on least-mechanism
  grounds: no consumer exists (CLI cancel = process kill; gateway timeout kill bounds
  MCP/HTTP). 4.1 stats re-keyed per slot (page × kind) with new 4.3.5 masking regression.
  4.4 routes every non-Emitted pass outcome incl. Rejected to the containment map, new
  4.4.4 regression. 6.2 baseline defined as the ref's committed `_meta/codewiki.json`
  snapshot with distinct absent/malformed-baseline error and both-snapshot golden
  fixtures (6.2.3 amended). 6.3 retains agy; awk/`_context` now fall to stoplist +
  leading-non-alphanumeric rules so zero manual deletions remain, new 6.3.5. 6.5
  formatter made precision-aware (bare dates stay date-only) with injected-offset
  negative-UTC tests (6.5.1/6.5.2 amended).

**Round 3** `kind: verification`

- reviewer_run: 7d57b88f-19f0-41f5-a0b3-090a8ae5f1a5
- reviewer_session: "#9131" (ffa39f74-b226-4aca-8803-00f186c6ba5e)
- review_round: 2 of max 3
- verdict: needs_review
- findings: 6 blocking — WGC-BACKEND-ERROR-TYPE (backend_error(postgres::Error) cannot
  cover semantic.rs:371, which receives AiError; the eight-site claim was type-invalid),
  WGC-APIKEY-ORIGIN (catch-all else→OPENAI_API_KEY would send the OpenAI credential as
  Bearer to custom/local api_base endpoints), WGC-HTTP-ERROR-STALE (the HttpStatus
  Display change already landed in ai_types.rs via ba7a40f3e; plan named the wrong file
  and a dead root cause), WGC-DIAGRAM-FALLBACK-CAPTION (fixed no-edges caption is false
  on the NoGenerator/Rejected fallback branches), WGC-CONCEPT-DIGIT-RULE (trailing
  digit-run ≥3 junks sha256/x509/iso8601/rfc3339 and archives their pages),
  WGC-TIMESTAMP-SHARED-OWNER ('hoist or reuse' left undecided; targets omitted recap.rs
  and the shared time module).
- resolution_notes: All six folded in, none diverged. 1.1 scoped to the seven bm25.rs
  postgres sites, semantic AiError mapping retained (1.1.3 amended). 1.4 retitled to
  env-fallback only; recognized-provider allowlist with no-key default for
  absent/unknown providers and a credential-leak regression (1.4.1 amended); stale
  Display step replaced by an existing-prerequisite note citing ai_types.rs:228-244,
  1.4.3 kept as a regression pin; target `crates/gcore/src/ai/mod.rs` dropped. 4.4
  captions made reason-aware per triggering outcome, pinned in 4.4.1/4.4.4. 6.3 numeric
  rejection restricted to explicit artifact prefix families; generic trailing-digit rule
  removed; positive cases sha256/x509/iso8601/rfc3339 added (6.3.2 amended). 6.5 owner
  decided: `crates/gwiki/src/support/time.rs` hosts the precision-aware parser +
  offset-injected formatter; recap.rs/citations.rs/log.rs consume it; targets updated
  (6.5.2 amended). Round-1's WGC-ASK-CANCEL narrowing stands as flagged in Round 2's
  notes.

**Round 4** `kind: verification`

- reviewer_run: bd2d53dc-3b48-479e-ba62-80fccaec698b
- reviewer_session: "#9137" (3d35b9ae-0b77-4c2c-8d82-e316df46caaa)
- review_round: 3 (no round cap — iterate to convergence)
- verdict: needs_review
- findings: 3 blocking — WGC-DEEP-IMPLIED-SYNTHESIS (GwikiGateway.ask rejects
  ai/require_ai whenever llm is false, so deep=true with an AI route or require_ai and
  llm at its default would be bounced before gwiki runs; HTTP-route regression missing),
  WGC-DISTINCT-PAIRS-VISIBILITY (librarian.rs declares `mod semantic;` privately, so a
  pub(crate) load_distinct_pairs alone is unreachable from health.rs — stated
  implementation would not compile), WGC-TIMESTAMP-PREFIX-CONTRACT (recap's pinned
  contract accepts annotated date prefixes like `2026-07-04 (approximate)`; a strict
  bare-YYYY-MM-DD DateOnly breaks recap, while a permissive renderer could silently drop
  the annotation).
- resolution_notes: All three folded in, none diverged. 3.2 validation invariant defined
  as `generation_requested = llm or deep`, threaded through gateway, resolve_ask_timeout,
  MCP, and HTTP; 3.2.1/3.2.2 amended and new 3.2.4 HTTP-route regression in
  tests/servers/routes/test_wiki_routes.py (CLI `--deep --ai off` rejection remains
  pinned by 3.1.4). 6.4 targets gained librarian.rs + librarian/semantic.rs; chosen
  mechanism is the reviewer-preferred pub(crate) re-export from librarian.rs with the
  module staying private (6.4.2 amended). 6.5 parsed value gained a DatePrefix variant
  carrying parsed date AND original string: recap keeps prefix acceptance for ordering
  (recap.rs:703-708 regression stays green against the shared parser); citations/log
  render DatePrefix verbatim, never truncating the annotation (6.5.1/6.5.2 amended).

**Round 5** `kind: verification`

- reviewer_run: 57e52e08-4509-46ab-a18f-2eedca144bbe
- reviewer_session: "#9158" (61fc63e3-6a55-481a-95bc-b9fec4b09e22)
- review_round: 4 (no round cap — iterate to convergence)
- verdict: needs_review
- findings: 2 blocking — WGC-DEEP-LIMIT-OBSERVABILITY (the daemon route returns
  investigation.stop_reason at servers/routes/llm.py:307 but DaemonAgenticResult
  omits it and parse_daemon_agentic discards it, and daemon candidate timeouts abort
  server-side with no partial metrics — 3.1.5's daemon/direct limit-exit parity was
  untestable from the listed targets), WGC-DEEP-TOOL-PARITY (VaultToolExecutor
  implements only search_vault/read_document, so the direct arm cannot serve the
  planned search/read/backlinks/sources prompt; vault_tools.rs was absent from the
  target list).
- resolution_notes: Both folded in, none diverged. 3.1 targets gained
  crates/gcore/src/ai/generation/transport.rs and
  crates/gwiki/src/commands/vault_tools.rs. New limit-exit-observability bullet:
  DaemonAgenticResult gains stop_reason: Option<String>, parse_daemon_agentic parses
  it; daemon timeout stays the existing AiError degradation/require-ai error with
  partial metrics unavailable; no cancellation orchestration. 3.1.5 made
  route-accurate (max-turn/max-tool-call structured on both arms via the parsed
  stop_reason; StopReason::Timeout structured on direct only). New tool-parity
  bullet: bounded read-only backlinks + sources executors added beside search/read; a
  deep-specific daemon ToolPolicy names exactly the same four capabilities, broader
  compile/upkeep policy untouched. 3.1.2 amended (deep-specific four-capability
  policy) and new 3.1.6 pins capability parity + mutation denial in
  vault_tools.rs tests.

**Round 6** `kind: verification`

- reviewer_run: b77dc51f-7789-411c-9b38-8e18115b0bc2
- reviewer_session: "#9166" (b01de7d8-90f2-4c2c-ba3e-3b23f8eecb42)
- review_round: 5 (no round cap — iterate to convergence)
- verdict: needs_review
- standing_divergence: WGC-ASK-CANCEL accepted by the reviewer — no
  cooperative-cancellation consumer exists (CLI cancellation terminates the process;
  GwikiGateway timeout/caller cancellation terminates then kills the gwiki
  subprocess). Divergence closed; the 3.1 NOT-building bullet stands as written.
- findings: 3 blocking — WGC-DEEP-SYNTHESIS-SEAM (ASK_TIER at synthesis.rs:21 and
  mark_ai_unavailable at :152 are private to synthesis.rs and record_synthesis
  :116-150 hardwires the excerpt-substring citation_check, so a sibling deep.rs
  cannot use the named seam; synthesis.rs was absent from 3.1 targets),
  WGC-DEEP-DAEMON-STOP-REASON-MATRIX (the universal BOTH-arm max-turn/max-tool-call
  claim is contradicted by daemon adapters: openai_compatible emits
  max_turns/max_tool_calls/completed, the Claude adapter emits max_turns only via
  ClaudeSDKMaxTurns and otherwise completed even when budget-exhausted, spawn
  adapters always emit completed), WGC-DEEP-STOP-REASON-PARSER-TEST (the only named
  acceptance test was deep.rs while parse_daemon_agentic is pub(crate) — deep tests
  could pass with the JSON parser still discarding stop_reason; the transport
  regression tests/daemon_agentic.rs exercises the daemon JSON shape without
  stop_reason).
- resolution_notes: All three folded, none diverged. 3.1 targets gained
  crates/gwiki/src/commands/ask/synthesis.rs. Synthesis-seam work added to the
  record_synthesis bullet: ASK_TIER + mark_ai_unavailable exposed at pub(super);
  record_synthesis refactored to accept a precomputed AskCitationCheckOutput
  (standard ask passes the excerpt-substring result, deep passes vault-page-existence;
  one injected value, no checker trait). New 3.1.7 pins both citation modes and
  deep_unavailable/ai_unavailable warning behavior in synthesis.rs tests. 3.1.5
  rewritten adapter-accurate: direct pins all StopReason variants; daemon surfaces
  the parsed stop_reason verbatim, pinning only adapter-produced values
  (openai_compatible full matrix at _tool_chat_adapters.py:76-121; Claude max_turns
  via ClaudeSDKMaxTurns else completed at :299-323; spawn adapters completed-only);
  universal BOTH-arm wording removed. Transport-boundary parsing pinned in
  crates/gcore/src/ai/generation/tests/daemon_agentic.rs (present value and
  missing/null → None), added to 3.1.5's test list and the observability bullet.

**Round 7** `kind: verification`

- reviewer_run: 8c141040-1e82-4bf1-9280-64c1dd8d38bc
- reviewer_session: "#9176" (53690d63-6f77-49f4-9e80-5a6ed06a5518)
- review_round: 6 (no round cap — iterate to convergence)
- verdict: needs_review
- findings: 7 blocking — WGC-DEEP-WARNING-SEAM (mark_ai_unavailable takes no
  warning-kind input and unconditionally pushes ai_unavailable, so pub(super) alone
  cannot emit deep_unavailable), WGC-DEEP-TOOL-NAME-PARITY (direct executor names are
  search_vault/read_document vs daemon search/read/backlinks/sources — one shared
  exact-name contract impossible without renaming existing direct consumers),
  WGC-DEEP-STOP-REASON-STRUCT-LITERAL (gcode's exhaustive daemon_agentic_result
  struct literal at text/generation.rs:1663-1679 breaks compilation when stop_reason
  is added; file was absent from 3.1 targets), WGC-TARGET-COVERAGE-SWEEP (Target
  lines across 2.1/3.1/4.2-4.4/6.1-6.3 omitted mandatory implementation files; 6.2
  targeted a directory), WGC-MODULE-RENDER-BUMP-ORDER (bump in 4.2 lets pages
  regenerated before 4.3 be reused without call-sequence sections — reuse keys on
  exact version equality), WGC-UNSERIALIZED-SHARED-FILES (independent leaves edit
  shared files without dependency edges), WGC-LANE-RENAME-BACKCOMPAT (2.1's
  _meta/lane_b/ migration handling contradicts the pre-0.5.0 no-backcompat
  constraint).
- resolution_notes: All seven folded, none diverged. mark_ai_unavailable refactored to
  accept the explicit warning token with all callers updated (3.1.7 amended). Tool
  parity redefined as semantic with route-specific exact name sets pinned (3.1.6
  amended). text/generation.rs added to 3.1 targets with stop_reason: None fixture
  note. Whole-plan target sweep: 2.1 lists all rename sources/destinations and call
  sites; 3.1 gains the CLI/output/assembly threading files; 4.2/4.3 gain
  types.rs/render/pages.rs (+ mod.rs on 4.3); 4.4 gains mod.rs and its tests file;
  6.1 gains types.rs/doc_paths.rs/tests/reuse.rs; 6.2 replaced its directory target
  with a new compare.rs wired through mod.rs; 6.3 gains librarian/semantic.rs with
  the unresolved_link_clusters gate cited. Module render bump moved wholly to 4.3
  (new 4.3.6; 4.2 pins version-unchanged; Constraints amended). Serialization edges
  added: 4.1→2.1, 5.1→3.2, 4.4→+4.3, 6.1→4.3, 6.4→+6.3. 2.1 lane rename made direct
  with no legacy path/env fallback. Additionally (Josh, 2026-07-18, user-directed
  scope): new deliverable 3.3 normalizes daemon tool-chat adapter stop-reason
  reporting so all providers are consistent — Claude reports max_tool_calls on
  budget exhaustion, grok passes and reports --max-turns, qwen surfaces its
  limit-termination subtype, codex/droid log the limits-unenforceable gap honestly;
  3.1 now depends on 3.3 and 3.1.5 pins the post-normalization matrix.

**Round 8** `kind: verification`

- reviewer_run: a94004d0-4772-4aa7-85c7-4c2ddeb6fca7
- reviewer_session: "#9183" (e662b275-5df9-405f-a015-fb342e24ff70)
- review_round: 7 (no round cap — iterate to convergence)
- verdict: needs_review
- findings: 5 blocking — WGC-TARGET-COVERAGE-SWEEP (four more mandatory edit sites
  absent from targets: curated_content.rs owns the lane_b_dump module decl/re-export/
  call, codewiki/frontmatter.rs has a lane_a test local, ask/render.rs calls
  record_synthesis whose signature 3.1 changes, generation.rs calls
  build_module_docs_with_filter and must pass 4.2's graph_availability),
  WGC-UNSERIALIZED-SHARED-FILES (4.4 and 6.2 both edit codewiki/mod.rs with no edge —
  concurrently dispatchable after 4.3), WGC-DEEP-STOP-REASON-OUTPUT (AskDeepOutput
  omitted stop_reason, so the value 3.1.5 surfaces had nowhere to live),
  WGC-QWEN-LIMIT-EXIT-PATH (qwen limit overruns abort with exit codes 55/53 and the
  shared runner raises on every nonzero exit before parse_qwen_stream sees stdout;
  count-reaches-cap inference dishonest; --max-session-turns unforwarded),
  WGC-GROK-LIMIT-SOURCE (grok _build_command already passes --max-turns via
  _resolve_max_turns with request.max_turns precedence — the planned change would have
  regressed it; turns is an alias of tool-call count).
- resolution_notes: All five verified against source and folded, none diverged. My
  round-6-window claim that grok lacked --max-turns was wrong — corrected from
  _tool_chat_spawn.py:588/:602 and _tool_chat_contracts.py:103-108; grok fix is now
  reporting-only (parse result-JSON stopReason, no count inference, precedence
  pinned in 3.3.2). Qwen path redesigned around exit codes: shared runner in
  _text_generation_adapters.py gains caller-declared accepted exit codes (new 3.3.4),
  qwen maps 55 → max_tool_calls / 53 → max_turns, forwards --max-session-turns, and
  no-text limit exits raise naming the limit; 3.1.5 matrix now lists qwen
  max_tool_calls/max_turns/completed. AskDeepOutput gains stop_reason with direct
  StopReason→string mapping and daemon verbatim propagation (3.1.3/3.1.5 pin it,
  missing → null). Targets: 2.1 + curated_content.rs + codewiki/frontmatter.rs (body
  cites :20-26/:157 and :300-301), 3.1 + ask/render.rs (:83/:101 caller migration),
  4.2 + generation.rs (:304 caller passes graph_availability). 6.2 now depends on 4.4
  (codewiki/mod.rs serialization; manifest must mirror 6.2 → [6.1, 4.4]).

**Round 9** `kind: verification`

- reviewer_run: 57b0d67d-2c23-4ad0-8941-c9ca71c95440
- reviewer_session: "#9204" (c4ef1405-57b3-4b37-8103-cb0d0fd30f0c)
- review_round: 8 (no round cap — iterate to convergence)
- verdict: needs_review
- findings: 6 blocking — WGC-TARGET-COVERAGE-SWEEP-R8 (commands/ask.rs owns the ask
  submodule declarations/execute/dispatch that wire deep.rs; tests/truth_digest.rs
  :31/:64/:73 call build_truth_digest), WGC-QWEN-STDERR-ONLY-LIMITS (limit
  diagnostics go to stderr and the planned no-text raise branch could never report
  max_tool_calls/max_turns), WGC-QWEN-EXIT55-AMBIGUITY (exit 55 shared by
  --max-tool-calls and --max-wall-time; blanket 55 → max_tool_calls fabricates the
  reason), WGC-GROK-UNKNOWN-STOP-REASON (absent/unrecognized stopReason mapped to
  completed contradicts the contract reserving completed for natural completion),
  WGC-SPAWN-TURN-PROVENANCE (all four spawn adapters alias turns to tool_use_count
  while native turn signals exist), WGC-DAEMON-LANE-STOP-REASON
  (from_daemon_agentic hardcodes StopReason::Completed so daemon limit exits
  masquerade as natural completion at the Rust lane consumer).
- resolution_notes: All six verified against source and the installed CLIs' bundled
  code, and folded. One mechanism refinement strengthens rather than weakens the
  qwen finding: the stream-json catch path DOES emit a terminal stdout result event
  on limit aborts (is_error: true, subtype error_during_execution, with
  num_turns/usage/error.message but NO `result` text) before the stderr diagnostic
  and exit — the fold encodes that verified shape, using error.message (stderr
  fallback) to disambiguate exit 55 into max_tool_calls vs timeout and yielding a
  typed limit-only result instead of raising. Grok's stopReason enum pinned from
  the binary (EndTurn, MaxTurnRequests, MaxTokens, Refusal, Cancelled; PascalCase);
  EndTurn → completed, MaxTurnRequests → max_turns, everything else/absent → None.
  Turns de-aliased: grok turnCount, qwen num_turns, droid num_turns/numTurns
  (verified in each binary), codex None (verified absent); turns Optional
  end-to-end with the Rust ripple pinned (transport.rs:139/:311,
  generation.rs:514 + constructors, FrontmatterLaneB at text/frontmatter.rs:43/:192;
  types.rs:620 and io.rs:415 already Option — verified no edit). from_daemon_agentic
  now maps canonical strings, None for missing/unknown, and rejects non-completed
  via is_completed() like from_tool_loop (new 3.1.8). New serialization edge 6.1 →
  3.1 (text/frontmatter.rs shared; manifest must mirror 6.1 → [4.3, 3.1]).

**Round 10** `kind: verification`

- reviewer_run: 8f6f04f0-8ceb-46bb-adae-f9ba18de7845
- reviewer_session: "#9208" (0aad6892-9c84-4e19-b752-9faad2c4ef18)
- review_round: 9 (no round cap — iterate to convergence)
- verdict: needs_review
- findings: 5 blocking — WGC-TOOL-CHAT-RESULT-CONTRACT-R9 (the round-8 redesign
  needs shared-contract edits absent from 3.3's Target: ToolChatResult.text is
  `str` and turns is `int = 0` at _tool_chat_contracts.py:121/:126, while
  stop_reason at :130 is already optional so the "becomes optional" prose was
  stale), WGC-RUST-OPTION-TURNS-TARGET-R9 (concepts/render.rs holds a direct
  GenerationObservability test literal `turns: 4` at :593 and a `turns = 1`
  reassignment at :616 that the Option change must rewrite; the file is also a 4.1
  target, exposing an unordered shared-file collision), WGC-DAEMON-TIMEOUT-
  ACCEPTANCE-R9 (3.1.8's pinned-case list omitted timeout even though its mapping
  clause named it; StopReason::Timeout at tool_loop.rs:254-263 with is_completed()
  true only for Completed at :276-278), WGC-OPTION-TURNS-PARSER-TEST-R9
  (parse_daemon_agentic collapses absent counts via unwrap_or(0) at
  transport.rs:298-305/:311 and existing fixtures assert 4/0 at
  daemon_agentic.rs:144/:190, so Option turns could regress to Some(0) without a
  transport pin), WGC-MANIFEST-SCHEMA-R9 (the approval-time manifest recipe
  stopped at 6.4 while deliverable 6.5 exists, violating the 1:1 invariant at
  docs/contracts/plan-coverage.md:203-204, and blanket tdd: true violated the
  refactor rule at :185-186 for 2.1).
- resolution_notes: All five verified against source and folded. 3.3 Target gains
  _tool_chat_contracts.py; ToolChatResult.text → `str | None` and turns →
  `int | None = None` with both contract points pinned in acceptance 3.3.3/3.3.6;
  the stale stop_reason prose now states it is already optional. 3.1 Target gains
  build_parts/concepts/render.rs (test literals :593 → Some(4), :616 → Some(1),
  frontmatter assertions retained) and 4.1 now depends on [2.1, 3.1] to serialize
  the shared file. 3.1.8 pins the timeout case (Some(StopReason::Timeout) +
  GenerationFailureCause::Unavailable). 3.1.5's transport pin covers both optional
  fields: turns native → Some(n), missing/null → None, never Some(0);
  daemon_agentic.rs:144/:190 fixtures become Some(4)/None with tool_use_count's
  zero default retained. Corrected manifest recipe for the approval round: one
  entry per deliverable INCLUDING 6.5 (code/backend/tdd true, depends_on []);
  2.1 is category refactor with tdd false; 4.1 → [2.1, 3.1]; all other
  dependencies unchanged (3.1 → [1.2, 1.4, 2.1, 3.3], 3.2 → [3.1], 4.2 → [4.1],
  4.3 → [4.2], 4.4 → [4.1, 4.3], 5.1 → [3.2], 6.1 → [4.3, 3.1],
  6.2 → [6.1, 4.4], 6.4 → [5.2, 6.3], rest []).

## M1 Task Manifest

`kind: manifest`

```yaml
- title: "Harden pg_search sanitization and backend errors"
  category: code
  task_type: bug
  depends_on: []
  validation_criteria: "Tantivy metachar queries succeed through pg_search, all PostgreSQL BM25 errors retain server detail, and focused Rust regressions pass."
  labels:
    - "covers:wiki-gap-closure:1.1:1.1.1"
    - "covers:wiki-gap-closure:1.1:1.1.2"
    - "covers:wiki-gap-closure:1.1:1.1.3"
    - "covers:wiki-gap-closure:1.1:1.1.4"
    - "covers:wiki-gap-closure:1.1:1.1.5"
  implementation_domain: backend
  tdd: true
  source_section: "1.1"
- title: "Clean and align ask evidence"
  category: code
  task_type: bug
  depends_on: []
  validation_criteria: "Ask evidence strips document frontmatter, skips empty excerpts without consuming budget, preserves source alignment, and focused Rust regressions pass."
  labels:
    - "covers:wiki-gap-closure:1.2:1.2.1"
    - "covers:wiki-gap-closure:1.2:1.2.2"
    - "covers:wiki-gap-closure:1.2:1.2.3"
    - "covers:wiki-gap-closure:1.2:1.2.4"
  implementation_domain: backend
  tdd: true
  source_section: "1.2"
- title: "Improve graph-boost ranking metadata"
  category: code
  task_type: bug
  depends_on: []
  validation_criteria: "Graph boost down-weights high-outdegree backlink hubs, carries document titles, and keeps unrelated hubs out of top results in focused tests."
  labels:
    - "covers:wiki-gap-closure:1.3:1.3.1"
    - "covers:wiki-gap-closure:1.3:1.3.2"
    - "covers:wiki-gap-closure:1.3:1.3.3"
  implementation_domain: backend
  tdd: true
  source_section: "1.3"
- title: "Resolve direct-generation provider API keys from environment"
  category: code
  task_type: bug
  depends_on: []
  validation_criteria: "Recognized providers receive trimmed conventional environment keys, custom endpoints receive no leaked fallback key, and focused generation tests pass."
  labels:
    - "covers:wiki-gap-closure:1.4:1.4.1"
    - "covers:wiki-gap-closure:1.4:1.4.2"
    - "covers:wiki-gap-closure:1.4:1.4.3"
  implementation_domain: backend
  tdd: true
  source_section: "1.4"
- title: "Rename generation lanes to semantic names"
  category: refactor
  task_type: feature
  depends_on: []
  validation_criteria: "Current source, config, and docs are clean of Lane-A/Lane-B identifiers outside the historical allowlist, renamed paths are direct, and scoped tests pass."
  labels:
    - "covers:wiki-gap-closure:2.1:2.1.1"
    - "covers:wiki-gap-closure:2.1:2.1.2"
    - "covers:wiki-gap-closure:2.1:2.1.3"
  tdd: false
  source_section: "2.1"
- title: "Implement deep agentic wiki ask"
  category: code
  task_type: feature
  depends_on: ["1.2", "1.4", "2.1", "3.3"]
  validation_criteria: "gwiki ask --deep supports daemon, direct, and degraded routes with read-only tool parity, honest citations, optional turns, normalized stop reasons, and focused Rust contract tests passing."
  labels:
    - "covers:wiki-gap-closure:3.1:3.1.1"
    - "covers:wiki-gap-closure:3.1:3.1.2"
    - "covers:wiki-gap-closure:3.1:3.1.3"
    - "covers:wiki-gap-closure:3.1:3.1.4"
    - "covers:wiki-gap-closure:3.1:3.1.5"
    - "covers:wiki-gap-closure:3.1:3.1.6"
    - "covers:wiki-gap-closure:3.1:3.1.7"
    - "covers:wiki-gap-closure:3.1:3.1.8"
  implementation_domain: backend
  tdd: true
  source_section: "3.1"
- title: "Expose deep ask through gateway, MCP, and HTTP"
  category: code
  task_type: feature
  depends_on: ["3.1"]
  validation_criteria: "Gateway, MCP, and HTTP accept deep requests, route generation flags and timeouts correctly, reject invalid non-generation AI options, and focused Python tests pass."
  labels:
    - "covers:wiki-gap-closure:3.2:3.2.1"
    - "covers:wiki-gap-closure:3.2:3.2.2"
    - "covers:wiki-gap-closure:3.2:3.2.3"
    - "covers:wiki-gap-closure:3.2:3.2.4"
  implementation_domain: backend
  tdd: true
  source_section: "3.2"
- title: "Normalize tool-chat stop reasons and turn provenance"
  category: code
  task_type: bug
  depends_on: []
  validation_criteria: "All tool-chat adapters report verified stop reasons and provider-native optional turns, Qwen limit exits retain typed provenance, and focused adapter tests pass."
  labels:
    - "covers:wiki-gap-closure:3.3:3.3.1"
    - "covers:wiki-gap-closure:3.3:3.3.2"
    - "covers:wiki-gap-closure:3.3:3.3.3"
    - "covers:wiki-gap-closure:3.3:3.3.4"
    - "covers:wiki-gap-closure:3.3:3.3.5"
    - "covers:wiki-gap-closure:3.3:3.3.6"
  implementation_domain: backend
  tdd: true
  source_section: "3.3"
- title: "Add typed diagram outcomes and run statistics"
  category: code
  task_type: feature
  depends_on: ["2.1", "3.1"]
  validation_criteria: "Every attempted diagram slot records one typed final outcome in logs and metadata, aggregate invariants hold, and focused Rust tests pass."
  labels:
    - "covers:wiki-gap-closure:4.1:4.1.1"
    - "covers:wiki-gap-closure:4.1:4.1.2"
    - "covers:wiki-gap-closure:4.1:4.1.3"
  implementation_domain: backend
  tdd: true
  source_section: "4.1"
- title: "Restore deterministic module dependency diagrams"
  category: code
  task_type: feature
  depends_on: ["4.1"]
  validation_criteria: "Eligible modules render bounded deterministic dependency diagrams with honest truncation notes, zero-edge modules omit the section, and parser/permutation tests pass."
  labels:
    - "covers:wiki-gap-closure:4.2:4.2.1"
    - "covers:wiki-gap-closure:4.2:4.2.2"
    - "covers:wiki-gap-closure:4.2:4.2.3"
    - "covers:wiki-gap-closure:4.2:4.2.4"
  implementation_domain: backend
  tdd: true
  source_section: "4.2"
- title: "Add deterministic module call-sequence diagrams"
  category: code
  task_type: feature
  depends_on: ["4.2"]
  validation_criteria: "Depth-bearing call chains render bounded deterministic sequence diagrams, flat stars remain omitted, slot observability is preserved, and the module render version advances once."
  labels:
    - "covers:wiki-gap-closure:4.3:4.3.1"
    - "covers:wiki-gap-closure:4.3:4.3.2"
    - "covers:wiki-gap-closure:4.3:4.3.3"
    - "covers:wiki-gap-closure:4.3:4.3.4"
    - "covers:wiki-gap-closure:4.3:4.3.5"
    - "covers:wiki-gap-closure:4.3:4.3.6"
  implementation_domain: backend
  tdd: true
  source_section: "4.3"
- title: "Add curated evidence and containment fallback diagrams"
  category: code
  task_type: feature
  depends_on: ["4.1", "4.3"]
  validation_criteria: "Curated pages use child evidence or deterministic reason-aware containment fallback diagrams, record one final slot outcome, and focused validation tests pass."
  labels:
    - "covers:wiki-gap-closure:4.4:4.4.1"
    - "covers:wiki-gap-closure:4.4:4.4.2"
    - "covers:wiki-gap-closure:4.4:4.4.3"
    - "covers:wiki-gap-closure:4.4:4.4.4"
  implementation_domain: backend
  tdd: true
  source_section: "4.4"
- title: "Schedule agent export refreshes"
  category: code
  task_type: feature
  depends_on: ["3.2"]
  validation_criteria: "The scheduled exports job invokes both serialized gateway methods, degrades on one failed step, raises on two failed steps, and focused Python tests pass."
  labels:
    - "covers:wiki-gap-closure:5.1:5.1.1"
    - "covers:wiki-gap-closure:5.1:5.1.2"
  implementation_domain: backend
  tdd: true
  source_section: "5.1"
- title: "Report stale agent exports in wiki health"
  category: code
  task_type: feature
  depends_on: []
  validation_criteria: "Wiki health independently reports each missing or stale export family, partial refreshes cannot mask stale artifacts, and focused Rust tests pass."
  labels:
    - "covers:wiki-gap-closure:5.2:5.2.1"
    - "covers:wiki-gap-closure:5.2:5.2.2"
    - "covers:wiki-gap-closure:5.2:5.2.3"
  implementation_domain: backend
  tdd: true
  source_section: "5.2"
- title: "Stamp codewiki output with commit provenance"
  category: code
  task_type: feature
  depends_on: ["4.3", "3.1"]
  validation_criteria: "Regenerated pages and metadata carry commit, dirty, and local generated-time provenance while reused pages keep original stamps without churn; focused Rust tests pass."
  labels:
    - "covers:wiki-gap-closure:6.1:6.1.1"
    - "covers:wiki-gap-closure:6.1:6.1.2"
    - "covers:wiki-gap-closure:6.1:6.1.3"
    - "covers:wiki-gap-closure:6.1:6.1.4"
  implementation_domain: backend
  tdd: true
  source_section: "6.1"
- title: "Add codewiki compare-to summaries"
  category: code
  task_type: feature
  depends_on: ["6.1", "4.4"]
  validation_criteria: "gcode codewiki --compare-to emits deterministic read-only JSON diffs from validated baseline/current snapshots and distinguishes all pinned error and page states."
  labels:
    - "covers:wiki-gap-closure:6.2:6.2.1"
    - "covers:wiki-gap-closure:6.2:6.2.2"
    - "covers:wiki-gap-closure:6.2:6.2.3"
  implementation_domain: backend
  tdd: true
  source_section: "6.2"
- title: "Gate and archive unworthy concepts"
  category: code
  task_type: feature
  depends_on: []
  validation_criteria: "Unworthy keys are rejected or archived with auditable reasons while legitimate technical concepts remain active, dry-run/application agree, and focused Rust tests pass."
  labels:
    - "covers:wiki-gap-closure:6.3:6.3.1"
    - "covers:wiki-gap-closure:6.3:6.3.2"
    - "covers:wiki-gap-closure:6.3:6.3.3"
    - "covers:wiki-gap-closure:6.3:6.3.4"
    - "covers:wiki-gap-closure:6.3:6.3.5"
  implementation_domain: backend
  tdd: true
  source_section: "6.3"
- title: "Detect alias and prefix duplicate concepts"
  category: code
  task_type: feature
  depends_on: ["5.2", "6.3"]
  validation_criteria: "Health detects exact, shared-key, and proper-prefix duplicates with reasons, respects distinct-pair suppressions, and focused Rust tests pass."
  labels:
    - "covers:wiki-gap-closure:6.4:6.4.1"
    - "covers:wiki-gap-closure:6.4:6.4.2"
  implementation_domain: backend
  tdd: true
  source_section: "6.4"
- title: "Render human-readable local wiki timestamps"
  category: code
  task_type: bug
  depends_on: []
  validation_criteria: "Instant timestamps render in local time with offsets and raw epochs, date-only and annotated values preserve precision and text, all named consumers share the parser, and focused Rust tests pass."
  labels:
    - "covers:wiki-gap-closure:6.5:6.5.1"
    - "covers:wiki-gap-closure:6.5:6.5.2"
  implementation_domain: backend
  tdd: true
  source_section: "6.5"
```

## Task Mapping

`kind: framing`

<!-- Updated after task creation -->
| Plan Item | Task Ref | Status |
|-----------|----------|--------|
