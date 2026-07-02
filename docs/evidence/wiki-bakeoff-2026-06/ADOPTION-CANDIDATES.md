# Wiki bake-off — adoption candidates (running log)

## EVALUATION TRACKS (Josh, 2026-06-14) — the bake-off tests gobby's wiki/code surfaces; Track A has two sub-capabilities
- **Track A1 — code intelligence / graph query for agents.** Gobby surface: `gcode` (search, outline, symbol, graph callers/callees, hybrid BM25+semantic+graph retrieval; FalkorDB code graph). Comparator: **Graphify** (tree-sitter AST → code knowledge graph w/ Leiden communities → `query/path/explain` CLI + MCP; agent-oriented, ships `skill-*.md`). Evaluate by whether `explain`/`path`/`query` give an agent grounded, navigable answers the way `gcode search`/`graph`/`symbol` do — NOT against codewiki's markdown. (Graphify's markdown export is a weak A2-adjacent side output, noted but not the main comparison.)
- **Track A2 — code → documentation generation.** Gobby surface: `gcode codewiki` (460-page repo wiki, in `crates/gcode/src/commands/codewiki/`). Baseline: 460 pages (372 file + 67 module + 6 aggregate), 55 mermaid, frontmatter provenance ranges. Comparators: **DeepWiki-Open, CodeWiki, OpenDeepWiki** (repo→wiki generators).
- **Track B — sources → knowledge vault.** Gobby surface: `gwiki` (ingest multimodal sources → cited markdown vault → hybrid search → thin RAG `ask` → `compile` explainers, in `crates/gwiki/`). Baseline = WP3 evidence (`/Users/josh/Projects/gobby-cli/docs/evidence/wiki-parity-2026-06/`): 451 docs indexed zero-degradation, hybrid RRF search, direct/local ask <10s with citations, daemon ask cited with the latency target relaxed to <20s after #779 provider-baseline follow-up, compile explainer, honest UUID5 refusal. Comparator: **llm-wiki** (Karpathy drop-sources self-maintaining knowledge base), plus DeepWiki RAG-chat as a partial Q&A comparator.

Score and report each track separately. A tool can win one track and be irrelevant to another. Each adoption candidate tags which gobby surface it maps into.

## OUT OF SCOPE (Josh, 2026-06-14): UI/UX — deliberate follow-up, NOT a gobby loss
This bake-off evaluates **backend quality only** (content depth, coverage, grounding precision, agent-consumability, honesty). UI/UX is a separate future phase — backend must be solid first. Gobby's current presentation layer is **Obsidian-vault compatibility** (backlinks, graph view, search for free), which is sufficient today. Competitors with polished web UIs (DeepWiki, OpenDeepWiki) present better on UX, but the report marks that dimension **out of scope / follow-up** so the matrix isn't misread as gobby "losing UX" when that phase simply hasn't been built.

---


Durable working log of competitor strengths worth adopting into gobby's codewiki (`crates/gcode/src/commands/codewiki/`) and gwiki (`crates/gwiki/`). Accumulated during WP4 runs; at WP5 this becomes (a) the "What competitors do better → what to adopt" deep analysis in the bake-off report and (b) an **epic with one leaf task per candidate** (created once, evidence-backed, deduped across tools).

Each entry: source tool + evidence path · what it does · **why it's better** · gobby mapping · rough effort/risk · dedupe notes.

Status legend: 🔍 observed · ✅ confirmed across multiple pages/tools · ⏳ pending verification · ❌ rejected after analysis

---

## Candidates

### C1 — Curated narrative wiki layer ("determine structure → ~10 readable pages") ✅
- **Source:** DeepWiki-Open · `outputs/deepwiki-open/page_01_*.md`, `wiki_structure.xml`
- **What it does:** One LLM call determines a wiki *structure* (title + ~10 pages grouped into ~5 sections), then generates each page as a multi-section narrative with a diagram and a CLI/table. The reader gets a curated, top-down tour, not a file dump.
- **Why it's better (human axis):** A newcomer browsing 10 curated pages ("Project Introduction", "Architecture Overview", "Data Flow & Indexing Pipeline", per-crate pages) learns the system faster than browsing gobby's 460 reference pages, where top-down browsing hits per-file/per-symbol tiers first. Gobby's `_architecture`/`_onboarding`/`repo.md` aggregates gesture at this but aren't a deliberate, sectioned, reader-first *view*.
- **Gobby mapping:** codewiki already has the inputs (symbol/module summaries, graph, provenance). Add a curated "guided wiki" view: an LLM structure pass over the module/aggregate layer → N sectioned narrative pages that LINK INTO the existing reference pages (best of both: curated entry + exhaustive depth). Reuse `ground_text` grounding + bounded prompts.
- **Effort/risk:** Medium. New build_part + structure prompt; reuse existing retrieval. Risk: another aggregate AI tier (cost/latency) — gate behind a flag/profile.
- **Dedupe:** ✅ **Confirmed in OpenDeepWiki** (`outputs/opendeepwiki/wiki/`) — it generated a curated 5-section hierarchy (Overview → Architecture → Capabilities → Workflows → Getting-Started) as the primary view. Two tools (DeepWiki + OpenDeepWiki) independently lead with a curated narrative layer → C1 high-confidence. The *hierarchical catalog* refinement is split out as **C9**.

### C2 — Per-page collapsible "Relevant source files" provenance header 🔍
- **Source:** DeepWiki-Open · top of every `page_*.md`
- **What it does:** Each page opens with a `<details><summary>Relevant source files</summary>` block listing the source files used as context, as links.
- **Why it's better (human + agent axis):** Immediate, scannable provenance at the top of the page without scrolling frontmatter; collapsible so it doesn't clutter. Signals grounding up front.
- **Gobby mapping:** codewiki already tracks per-page provenance in frontmatter (`provenance: file/ranges`). Render a collapsible source-files header from that same data — and gobby would do it **strictly better**: our links resolve to real ranges, whereas DeepWiki's citation hrefs are empty `()` (see weakness W1).
- **Effort/risk:** Low. Pure render-layer addition from existing frontmatter.
- **Dedupe:** —

### C3 — High table density for scannable reference (command tables, data-model tables) 🔍
- **Source:** DeepWiki-Open · 114 tables across 10 pages (`outputs/deepwiki-open/page_*.md`)
- **What it does:** Renders command surfaces, data models, config keys, and capability matrices as Markdown tables instead of prose paragraphs.
- **Why it's better (human axis):** Tables are far more scannable than gobby's prose-heavy module narratives — a reader finds "what does `gwiki compile` do" in a table row vs hunting a paragraph. DeepWiki used a `| Command | Description |` table for the whole CLI on the intro page.
- **Gobby mapping:** codewiki module/aggregate prompts could request a structured table for enumerable facts (command lists, public API surface, config keys) alongside narrative. Data already available (symbol summaries, CLI contract JSON).
- **Effort/risk:** Low-medium. Prompt/template change in aggregate build_parts; keep grounding.
- **Dedupe:** Check OpenDeepWiki/CodeWiki for the same.

### C4 — Diagram fallback instead of full suppression when the graph is large ✅ (gobby self-improvement, surfaced by comparison)
- **Source:** contrast — DeepWiki-Open emitted a mermaid diagram on **all 10 pages**; gobby's `agents.md` shows `## Dependency Diagram → degraded: graph-truncated` (0 diagrams) because the module's call/import graph exceeded codewiki's diagram edge/hop bounds, so it suppressed entirely.
- **What's better about the competitor:** DeepWiki always gives the reader *a* visual, even if simplified.
- **Why adopt (with gobby's honesty preserved):** instead of suppressing when `graph-truncated`, emit a **bounded** diagram — e.g. top-N most-central nodes/edges or module-level (not symbol-level) — explicitly labeled "simplified / top-N of M edges." Keeps the honesty contract (no fabricated edge-free diagram, truncation disclosed) while restoring visual coverage. gobby's full-repo run got 55 diagrams, so the suppression bites hardest exactly on dense, important modules.
- **Gobby mapping:** `crates/gcode/src/commands/codewiki/` graph/diagram emission (the `graph-truncated` degradation path) + `cluster.rs`. Add a truncated-graph fallback renderer.
- **Effort/risk:** Medium. New bounded-diagram path; must keep edge/hop bounds + truncation labeling.
- **Dedupe:** ✅ **Confirmed in OpenDeepWiki** — 19 mermaid diagrams across 11/13 docs on the same gobby-cli `crates/` scope (accurate flowcharts + sequence diagrams), vs gobby codewiki's **0** diagrams on the same scope (graph-truncated). The contrast is now demonstrated head-to-head: competitors reliably emit a visual; gobby suppresses. Highest-confidence reader-facing gap. Adopt the bounded-diagram fallback.

### C5 — Semantic concept-module clustering (names that cut across directories) 🔍
- **Source:** CodeWiki (patched Python run) · `outputs/CodeWiki-python/docs/module_tree.json`, `agent_execution.md`
- **What it does:** clusters the call/dependency graph into **semantically-named concept-modules** — `monitoring_and_detection`, `recovery_and_cleanup`, `isolation_and_sandboxing`, `terminal_interface`, etc. — grouping by *what the code does*, not which folder it lives in. The 65 `src/gobby/agents` files → 6 concept-modules, each with a narrative overview + architecture diagram + links to sub-module docs.
- **Why it's better (human axis):** "monitoring_and_detection" tells a reader the *purpose* of a cluster; gobby's directory-modules (`agents`, `agents/spawners`, `agents/tmux`) only reflect file layout. Concept-modules give a conceptual map of the subsystem.
- **Gobby mapping:** codewiki already clusters (`crates/gcode/src/commands/codewiki/cluster.rs`) and has the call/import graph (FalkorDB). Add a semantic-naming/grouping pass over clusters (LLM names a cluster from its members' purposes) to produce concept-modules ALONGSIDE the directory modules — best of both: conceptual entry + path-faithful reference.
- **Effort/risk:** Medium. Clustering exists; add a naming/labeling step + an aggregate "concept modules" view. Risk: extra AI calls (gate by profile).
- **Caveat / gobby still wins overall:** CodeWiki's module overviews are grounded by symbol NAME only (no `file:line`); gobby cites `file:line` everywhere. Adopt the *clustering/naming* idea, keep gobby's stronger grounding.
- **Dedupe:** ✅ **Confirmed in a SECOND tool — Graphify** (`outputs/graphify/`, 403 Leiden communities over the gobby-cli code graph). Two independent competitors converge on semantic graph-community clustering as the navigation layer → C5 is the highest-confidence adoption candidate. gobby already clusters (`cluster.rs`); the gap is the *semantic naming* pass + a concept-module view.

### C6 — Shortest-path-between-two-concepts primitive (`graphify path "A" "B"`) 🔍
- **Source:** Graphify · `outputs/graphify/SCORECARD.md` Q3
- **What it does:** computes the shortest graph path between two named nodes and prints the connecting chain; honestly returns "No path found" when none exists (verified — `merge()` → `WikiSearchResult` across crates correctly returned no path).
- **Why it's better (agent axis):** gcode has `callers`/`usages`/`blast-radius` (single-source traversal) but **no two-endpoint path query**. "How does module A reach module B?" is a common agent question gobby can't answer in one call today.
- **Gobby mapping:** `crates/gcode/src/` graph layer (FalkorDB already stores the typed edge graph) — add a `gcode path <symbol-a> <symbol-b>` BFS/Dijkstra over the projection. Cite file:line on each hop (gobby's grounding advantage).
- **Effort/risk:** Low-medium. Graph already exists in FalkorDB; this is a new traversal command + output formatter.
- **Dedupe:** Graphify-unique among the tools tested.

### C7 — Per-edge confidence / audit trail in query answers (EXTRACTED / INFERRED / AMBIGUOUS) 🔍
- **Source:** Graphify · every `explain`/`query` edge tagged `[EXTRACTED]` + `confidence_score`
- **What it does:** every edge in the graph (and in every answer) carries a provenance-confidence label — `EXTRACTED` (AST-certain), `INFERRED` (LLM-guessed), `AMBIGUOUS` — so a consumer knows which relationships are facts vs inferences.
- **Why it's better (agent + honesty axis):** gobby's code-graph edges are AST-derived and trustworthy, but the **confidence dimension isn't surfaced to callers**. When gobby later adds inferred/semantic edges, an explicit per-edge confidence label keeps the honesty contract gobby already values (cf. degradation markers, honest UUID5 refusal).
- **Gobby mapping:** `crates/gcode/src/graph/` edge model + `graph`/`callers`/`usages` output — add a `confidence` field (AST edges = certain; future semantic edges = inferred). Render it in text/JSON.
- **Effort/risk:** Low for the AST-certain case (constant label); grows when inferred edges land.
- **Dedupe:** Graphify-unique; complements gobby's existing degradation-honesty patterns.

### C8 — Token-budget-capped retrieval output (`--budget N`) 🔍
- **Source:** Graphify · `query --budget 1200` capped the subgraph at ~1200 tokens with an explicit "narrow with context_filter=['call']" hint
- **What it does:** the agent sets a token ceiling on a retrieval call; Graphify returns the highest-priority subgraph within budget and tells the agent how to narrow further.
- **Why it's better (agent axis):** gobby's `search`/`graph` use result-count limits (`--limit`), not **token** budgets. Agents reason in tokens; a token-aware cap (with a narrowing hint) is better context-window ergonomics for large traversals like `blast-radius`.
- **Gobby mapping:** `gcode search`/`blast-radius`/`usages` — add an optional `--token-budget N` that trims output to fit and emits a "refine with …" hint. Pairs naturally with gsqz's token-optimization ethos.
- **Effort/risk:** Low-medium. Output-layer trimming + a token estimator; no graph changes.
- **Dedupe:** Graphify-unique; conceptually aligned with gsqz.

### C9 — Auto-generated hierarchical concept-page catalog over per-file docs ✅
- **Source:** OpenDeepWiki · `outputs/opendeepwiki/wiki/` + `tree` API (`status:2 Completed`, 13 leaf nodes in a 5-section hierarchy)
- **What it does:** an LLM "catalog" pass builds a **multi-level table of contents** — sections (Overview / Architecture / Capabilities / Workflows / Getting-Started) each containing concept leaf-pages — then generates one narrative+diagram page per node. It correctly identified all five binaries + gcore + the architecture layers from a file walk alone. This is C1's "curated layer" made **navigable/hierarchical** (a real tree, not a flat ~10-page list).
- **Why it's better (human axis):** gobby codewiki is **file-granular** (372 file + 67 module pages) with strong reference depth but **no curated top-down tree** — a newcomer has no "start here → drill down by concept" path. OpenDeepWiki's hierarchy is the missing navigation spine; layered over gobby's per-file reference it gives curated entry + exhaustive depth.
- **Gobby mapping:** codewiki aggregate tier (`crates/gcode/src/commands/codewiki/build_parts/` + `cluster.rs`). Add a catalog/structure pass that emits a sectioned concept-page tree LINKING INTO existing file/module pages (reuse module summaries + graph + provenance; keep `file:line` grounding gobby already wins on). Pairs with C1 (narrative) and C5 (concept-module naming) — same aggregate pass can produce all three.
- **Effort/risk:** Medium. One structure/catalog AI pass over the module layer + a tree renderer; gate behind a profile/flag (cost). Shares machinery with C1/C5 — implement together.
- **Dedupe:** Refines C1 (DeepWiki flat structure) into a hierarchy; converges with C5 (concept-modules). Treat C1+C5+C9 as one "curated navigation layer" epic cluster, not three separate builds.

### C10 — Session-transcript ingestion + synthesis pipeline (gwiki/daemon) ✅
- **Source:** llm-wiki · `outputs/llm-wiki/` (SCORECARD, `run/raw/sessions/`, `run/wiki/sources/`, `run/site-exports/`) vs `outputs/gwiki-track-b/` (gwiki's 8 opaque stubs)
- **What it does:** ingests AI **coding-session transcripts** (`.jsonl` from Claude Code / Codex / Cursor / Gemini) and turns them into a Karpathy knowledge vault. Demonstrated sub-features: (a) **per-CLI transcript adapters** that parse the internal schema; (b) **deterministic session-metadata extraction** — `model`, per-tool `tool_counts` histogram, `token_totals` (input/cache/output), `duration_seconds`, `hour_buckets`, `is_subagent`, `gitBranch`; (c) **secret redaction** on ingest (`/Users/josh`→`/Users/USER`, `sk-` keys, emails); (d) **single-shot bounded per-session LLM synthesis** (Summary / Key Claims / Key Quotes / Connections) that is **robust to local models** (clean gemma4:31b output where agent-loop tools sank on the same gemma class); (e) **agent-consumable exports** (`llms.txt`, `llms-full.txt`, `graph.jsonld`); (f) confidence + 5-state lifecycle + 16 lint-rule governance.
- **Why it's better (human + agent axis):** gobby has **no session-transcript capability** — feed gwiki the same 8 `.jsonl` and it stores **525-byte opaque stubs** (`kind: text`, raw asset pointer, 0 content extracted), and `compile` errors on a fresh vault (reproduces **#733**). Every coding session is dormant institutional knowledge (decisions, dead-ends, tool usage); a vault over them is a genuinely new gobby surface. **Strategic multiplier:** gobby **already archives every attached-project transcript** at `~/.gobby/session_transcripts/*.jsonl.gz` (3,750 sessions, 1.3 GB, multi-CLI) — the raw material exists; only the parse→synthesize→vault layer is missing.
- **Gobby mapping:** new ingest modality in `crates/gwiki/src/ingest/` (a `session`/`transcript` source type with per-CLI adapters) → derived per-session markdown with the metadata frontmatter → existing gwiki index/compile/search stack (which is *stronger* than llm-wiki's: hybrid BM25+semantic+graph vs static wikilink graph). Daemon owns the archive at `~/.gobby/session_transcripts/`, so a daemon-triggered "sync sessions → vault" job is the natural home. Note the gobby archive is a **wrapped envelope** (`{payload, timestamp, type}`) + multi-CLI → the adapter needs an unwrap + per-CLI dispatch step.
- **Effort/risk:** Medium-high (new modality + per-CLI adapters + metadata extraction + redaction). Lower than it looks because the index/compile/search half already exists and is gwiki's strength; this is an **ingest-side** build. Risk: transcript schemas drift across CLI versions (adapters need versioning).
- **Dedupe:** llm-wiki-unique (only Track B specialist). Distinct from C1/C5/C9 (those are code→docs navigation for codewiki; C10 is a new gwiki *input domain*). Pairs with gwiki's existing compile/search — once sessions are ingested, `ask`/`search`/`compile` work over them for free.

---

## Gobby baseline — `src/gobby/agents` scope (for the same-scope head-to-head vs CodeWiki)
`outputs/gobby-codewiki-agents/`: 65 file docs (100% of 65 files) + 3 module docs, 630 symbols with per-symbol purposes/signatures/UUIDs/ranges. Dense inline `[file:line]` citations that resolve + `[[wikilink]]` nav. Diagrams: 0 (graph-truncated, see C4). AI: frontier daemon lanes (vs CodeWiki on local gemma — model-quality caveat).

---

## Competitor weaknesses (gobby wins — for the report's other half)

### W1 — DeepWiki citation links are empty `()` 🔍
- **Source:** DeepWiki-Open · `Sources: [crates/gwiki/README.md:1-3]()` throughout page 1.
- DeepWiki emits `file:line` provenance labels but the markdown link target is empty — parseable by an agent, **non-navigable for a human**, and not verifiable as a real range. Gobby's frontmatter ranges + lint-checked links are a parity win. Keep for the proof matrix (grounding-precision dimension).
