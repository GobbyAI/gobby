# Graphify (graphifyy 0.8.39) — bake-off scorecard

**Track A1 — code intelligence / graph query for agents.** Comparator to gobby `gcode`
(`search`/`search-symbol`/`callers`/`usages`/`blast-radius` + FalkorDB code graph). Run:
2026-06-14. Corpus: clean `git archive HEAD` (`ea6a26c`) of gobby-cli `crates/` (433 code
files, 421 `.rs`). Model: local LM Studio `gemma-4-26b-a4b-qat` (community naming + 12 doc
semantic extraction only). Config: endpoint+model+key-env only, Graphify's own defaults
otherwise (see SETUP.md).

## Result: **SUCCESS — complete, grounded code graph + working agent query surface**
Built a **10,842-node / 25,859-edge / 403-community** knowledge graph (`graph.json`, 13.5MB)
in one run. The code graph itself is **100% local** (tree-sitter AST, no LLM, no API key);
the LLM is used only for community *naming* and the 12 crate-README doc nodes.

| Metric | Value |
|---|---|
| Nodes | 10,842 (10,808 AST-extracted + 34 from docs) |
| Edges | 25,859 |
| Edge relations | references 12,102 · calls 6,176 · contains 5,849 · method 900 · imports_from 578 · implements 231 · conceptually_related_to 14 · defines 8 · inherits 1 |
| Communities (Leiden) | 403 |
| Grounding | **file:line on every node AND every edge** (`source_file` + `source_location`) |
| Audit trail | every edge tagged `EXTRACTED` / `INFERRED` / `AMBIGUOUS` + `confidence_score` |
| Build cost | 19,935 in / 44,912 out tokens (~$0 on local) |

## A1 query battery (identical tasks vs gcode)

### Q1 — concept retrieval: "how does hybrid search merge BM25/semantic/graph"
- **Graphify `query`** (BFS depth 2, `--budget 1200`): seeded on `merge()/search()`, returned a
  **101-node grounded subgraph** — `NODE search() [src=gcode/src/commands/search.rs loc=L25 community=55]`
  … — correctly spanning **both** gcode AND gwiki search modules. Token-budget-capped with an
  explicit "narrow with context_filter=['call']" hint. **Grounded + good recall, but a node
  *neighborhood* with no per-node purpose** — the agent gets where things are, not what they do.
- **gcode `search`**: returned **ranked** hits (`merge_delegates_to_gobby_core_rrf`, `final_rank_score`…)
  each with a **rich LLM summary** ("Returns the sum of the exact tier score and reciprocal rank
  fusion score…"), file:line, and RRF relevance scores.
- **Edge: gcode** for "what does this do / which is most relevant"; **Graphify** for "show me the
  whole connected neighborhood, bounded to N tokens."

### Q2 — impact/blast-radius
- **Graphify `affected "merge()" --depth 2`**: 7 reverse edges, each `name() [calls] file:Lnn`.
- **gcode `callers`/`blast-radius merge`**: the **same 7 caller edges** with `line caller -> merge`.
- **Edge: parity.** Both give file:line-grounded reverse call traversal. gcode adds `[distance=N]`;
  Graphify adds a relation legend + per-edge confidence.

### Q3 — shortest path between two concepts
- **Graphify `path "merge()" "WikiSearchResult"`**: honest **"No path found"** + ambiguity warning
  (different crates, genuinely no call path). A **primitive gcode does not have** — gcode offers
  callers/usages/blast-radius but no two-endpoint shortest-path.
- **Edge: Graphify** (unique capability; honest when no path exists).

### Q4 — explain a node + neighbors
- **Graphify `explain "merge()"`**: structured card — ID, `Source: gcode/src/search/rrf.rs L15`,
  community, degree 11, then **all 11 connections** with direction arrows + relation + `[EXTRACTED]`
  audit label. **Deterministic, zero hallucination surface** (no LLM prose).
- **gcode `outline` + `symbol <uuid>`**: returns the **actual source code** of the symbol plus its
  structural map and LLM summary.
- **Edge: split** — gcode retrieves the real code + purpose; Graphify gives the audited edge-level
  neighborhood faster and with explicit provenance confidence.

## Human vs agent axes (Josh's quality axis)
- **Agent-consumability: STRONG.** Token-budget-capped subgraphs, file:line on every node/edge,
  explicit EXTRACTED/INFERRED/AMBIGUOUS confidence, an MCP server (`--mcp`), and ships per-CLI
  `skill-*.md` so an agent knows to "query first." This is a genuinely well-designed agent
  retrieval surface — arguably more *honest about edge provenance* than gcode (per-edge confidence).
- **Human-readability: MEDIUM** (and a deliberate A1, not A2, axis). `explain`/`query` output is a
  structured node/edge listing, not narrative. Graphify's human layer is `GRAPH_REPORT.md` +
  named communities + interactive HTML (the `cluster-only` pass) — captured separately below.

## Where gcode wins (parity-plus proof points)
1. **Per-symbol purpose.** gcode attaches an LLM summary to every symbol ("what it does"); Graphify
   nodes are label+location only. For an agent deciding *which* hit to open, gcode's summaries +
   RRF ranking are higher-signal.
2. **Real source retrieval.** `gcode symbol <uuid>` returns the actual code by byte offset; Graphify
   hands you `file:Lnn` to go open yourself.
3. **Hybrid semantic search.** gcode fuses BM25 + vector + graph (RRF); Graphify `query` is pure
   graph BFS/DFS off label-match seeds — no semantic/embedding recall for fuzzy wording.
4. **Live incremental index** over a persistent PG+FalkorDB+Qdrant hub vs Graphify's self-contained
   `graph.json` (Graphify counters with `--watch`/`update` + cross-repo `global` merge).

## Where Graphify wins (adoption candidates — see ADOPTION-CANDIDATES.md)
1. **Shortest-path-between-two-concepts** primitive (gcode has no equivalent). → candidate.
2. **Per-edge confidence/audit trail** (EXTRACTED/INFERRED/AMBIGUOUS) surfaced in every answer.
   gcode's graph edges are AST-derived but don't expose a confidence dimension to callers. → candidate.
3. **Token-budget-capped query output** (`--budget N`) — first-class agent ergonomics. → candidate.
4. **Leiden semantic communities** as a navigable layer (403 here) → reinforces C5.

## Honesty notes (about the bake-off)
- The `--cargo` crate-dep flag **crashed** when scoped to a subdirectory: `introspect_cargo`
  expects a workspace-root `Cargo.toml` in the scan dir and raised `FileNotFoundError` on
  `crates/Cargo.toml` (the real one is one level up), aborting before `graph.json` was written.
  Re-ran without `--cargo` (minor: loses crate→crate dep edges, not the symbol/call graph). Minor
  robustness nit, logged for fairness.
- Doc semantic extraction showed gemma JSON-validity wobble ("LLM returned invalid JSON, skipping
  chunk") but Graphify **recovered** by recursive half-splitting and completed — notably more robust
  to a flaky local model than CodeWiki (which never finished).

## Net verdict (Track A1)
**Graphify is a legitimate, well-built agent code-graph tool and the closest competitor to gcode's
retrieval surface — it succeeded cleanly on Rust where CodeWiki refused and DeepWiki doesn't operate.**
On the same scope, **gcode wins overall** for agent retrieval (semantic ranking + per-symbol purpose
+ real source retrieval + hybrid search), while **Graphify wins three specific, adoptable features**
(two-endpoint path, per-edge confidence audit, budget-capped output) and matches gcode on
file:line-grounded call/impact traversal. This is the parity-plus story: gcode is ahead, and the
honest gaps are concrete and small.

## Community naming + GRAPH_REPORT.md (cluster-only pass)
Ran `graphify cluster-only … --no-viz --backend lmstudio`. Outcome: **Leiden clustering succeeded
(404 communities, 393 shown / 11 thin omitted) and `GRAPH_REPORT.md` (114KB) generated; semantic
community *naming* FAILED on local gemma** and fell back to `Community N` placeholders.

- **Why naming failed:** all 5 naming batches (100 communities each) returned
  `Expecting value: line 1 column 1 (char 0)` — gemma (a reasoning model) returned empty/non-JSON
  `content`, breaking Graphify's JSON-label contract. **Same gemma JSON-contract failure class that
  sank CodeWiki**, but **Graphify degraded gracefully** (placeholders, finished cleanly) rather than
  hanging. A frontier model would name fine (as DeepWiki's pages did on the same gemma). Per the
  fairness rule I did NOT tune the model (no `reasoning_effort`/budget overrides).
- **GRAPH_REPORT.md quality (structure, model-independent):** strong.
  - Corpus-level honesty stat surfaced up front: **`95% EXTRACTED · 5% INFERRED · 0% AMBIGUOUS ·
    INFERRED: 1188 edges (avg confidence 0.8)`** — reinforces C7; gobby has no equivalent corpus
    confidence summary.
  - **Obsidian `[[wikilink]]` community-hub navigation** — the *same vault model gobby uses*
    (backlinks/graph view for free). Parity, not a win for either; confirms Obsidian-compat is the
    right shared presentation substrate (consistent with the UI/UX-out-of-scope note).
  - 393 per-community sections with member nodes — the navigable Leiden layer (C5), here unnamed.
- **Net:** the structural clustering (C5 evidence) is real and reinforced; the *named* concept-module
  view couldn't be visually evaluated under local gemma. Does not change the A1 verdict — the
  graph + query/affected/path/explain surface (the actual A1 value) works fully without naming.

## Artifacts
`outputs/graphify/`: `SCORECARD.md`, `SETUP.md`, `extract.log` (the `--cargo` crash), `extract2.log`
(successful build), `cluster.log` (naming failure + graceful fallback),
`graphify-out/graph.json` (10,842 nodes / 25,859 edges, 13.5MB),
`graphify-out/GRAPH_REPORT.md` (114KB), `graphify-out/.graphify_analysis.json`.
