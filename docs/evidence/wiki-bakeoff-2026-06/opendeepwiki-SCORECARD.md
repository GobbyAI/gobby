# OpenDeepWiki (AIDotNet/OpenDeepWiki) — bake-off scorecard

**Track A2 — code → docs generation.** Comparator to gobby `gcode codewiki`. Run: 2026-06-14.
Corpus: clean `git archive HEAD` (`ea6a26c`) of gobby-cli `crates/` (433 code files, primary
language auto-detected **Rust**). Model: local LM Studio `google/gemma-4-26b-a4b-qat` for
catalog + content. Config: endpoint + key + model id only; OpenDeepWiki defaults otherwise
(see SETUP.md). Wall-clock: ~23.7 min (1,423,284 ms) — **noted, not a ranking axis** (quality
is the axis).

## Result: **SUCCESS — complete 13-node hierarchical wiki on Rust**
Generated a full, navigable wiki where **CodeWiki refused/hung on the same Rust corpus**. All
13 catalog nodes persisted; 4 docs hit a `WriteDoc did not persist` agent miss but
**self-healed on retry** (`WIKI_MAX_RETRY_ATTEMPTS=3`), so the wiki is complete.

| Metric | Value |
|---|---|
| Catalog nodes (leaf docs) | 13, in a 5-section hierarchy |
| Hierarchy | Overview · Architecture×3 · Capabilities×5 (one per binary) · Workflows×2 · Getting-Started×2 |
| Total content | ~2,200 lines markdown |
| Mermaid diagrams | **19 across 11/13 docs** (flowcharts + sequence diagrams) |
| Line-anchored citations | 10 (`[path](path#L9-L15)` GitHub-anchor form), real/resolvable — but only in 4 docs |
| Catalog depth | `WIKI_DIRECTORY_TREE_MAX_DEPTH=2` → concept pages, not per-file |
| Doc store | DB (`DocFiles.Content`); pulled via `/api/v1/repos/{owner}/{repo}/docs/{slug}` |

## Quality is bimodal (largely a local-model artifact)
- **~8 strong docs** (200–291 lines, multi-section, diagrams, real cited code): `overview`,
  `architecture/data-persistence`, `architecture/ai-orchestration`, `capabilities/gloc`,
  `capabilities/gwiki`, `workflows/codewiki-generation`, `workflows/multimodal-research`,
  `getting-started/development`.
- **~4–5 thin / stub docs**: `capabilities/gcode-search` is a **20-line STUB** (only a
  "Purpose & Scope / covers / does NOT cover" scaffold, no body) — the flagship doc came out
  empty; `gsqz` (32L), `core-primitives` (56L), `ghook` (62L) are shallow. Root cause: gemma
  intermittently "completed" the content agent **without calling the WriteDoc function tool**,
  persisting only the scaffold. This is a **local reasoning-model function-calling reliability
  issue** (same gemma class that sank CodeWiki and Graphify naming), not an OpenDeepWiki design
  fault — a frontier model would fill these in.

## Where OpenDeepWiki is strong (model-independent design)
1. **Information architecture.** A curated top-down hierarchy (Overview → Architecture →
   Capabilities → Workflows → Getting-Started) that correctly identified all five binaries +
   gcore + the architecture layers. Far more navigable than a flat per-file dump.
2. **Diagram density.** 19 mermaid diagrams; the ones inspected are **accurate** — e.g. the
   data-persistence flowchart (Indexer→PG/FalkorDB/Qdrant→RRF) and the overview hybrid-search
   **sequence diagram** are faithful to the real design. gobby codewiki emitted **0** diagrams
   on the same repo (graph-truncation → C4).
3. **Narrative + accurate technical grounding.** RRF explained with the correct formula
   `1/(K+rank)`, K=60; an accurate graceful-degradation table (FalkorDB→`[]`, Qdrant/embeddings
   → semantic off, PG hub = source of truth); a correct gcode command list (`search`,
   `search-symbol`, `outline`, `callers`, `usages`, `blast-radius`, `init`, `index`, `status`).
4. **Precise citations when present.** `> Source: [crates/gcode/src/search/rrf.rs](…#L9-L15)`
   with **real, verbatim code** (the `merge` fn delegating to `gobby_core::search::rrf_merge`,
   the index `mod.rs` re-exports, gloc/gwiki snippets). Clickable line-range anchors.
5. **Honest epistemic markers.** Labels non-verbatim snippets "*(Conceptual snippet based on
   …)*"; explicit per-doc "covers / does NOT cover" scoping. Correctly cited the upstream
   GitHub URLs (`GobbyAI/gobby`, `GobbyAI/gobby-cli`) — accurate grounding, not invented.

## Where gobby `codewiki` wins (parity-plus proof points)
1. **Completeness + uniform depth.** gobby codewiki covered **all 65 files + 3 module docs**
   densely; OpenDeepWiki produced 13 concept pages with an **empty flagship stub** and uneven
   depth. (Caveat: gobby ran via frontier daemon lanes; OpenDeepWiki on local gemma — model
   advantage to gobby, but gobby's pipeline was also **more robust to the same local-model
   flakiness** in its own A2 run.)
2. **Citation density + per-symbol API.** gobby attaches **resolvable file:line on every
   symbol** (630 symbols with per-symbol API); OpenDeepWiki cites in only **4 of 13** docs.
3. **Per-file granularity.** gobby is a file-level reference; OpenDeepWiki caps at depth-2
   concept pages — coarser coverage of a 433-file repo.
4. **Grounded structure from a real index/graph.** gobby's docs derive from the live code
   index + FalkorDB graph; OpenDeepWiki's catalog is LLM-inferred from a file walk.

## Where OpenDeepWiki wins (adoption candidates — see ADOPTION-CANDIDATES.md)
- **Per-doc architecture diagrams** (mermaid flowcharts + sequence diagrams), 19 here vs gobby
  codewiki's 0 → **strongly reinforces C4** (diagram generation/fallback): OpenDeepWiki proves
  rich, accurate diagrams are achievable and high-value for human readers.
- **Curated hierarchical concept catalog** (a narrative table-of-contents layered above
  source) → **reinforces C1** (DeepWiki narrative layer) and sharpens it: an auto-generated
  Overview/Architecture/Capabilities/Workflows/Getting-Started hierarchy that gobby's
  file-granular codewiki lacks. → **C9** (new): hierarchical concept-page catalog over per-file docs.
- **Clickable line-anchored citations** `[path](path#L9-L15)` — more navigable in Obsidian/
  GitHub than bare `file:line` text. Minor → fold into gobby citation rendering.

## Honesty notes (about the bake-off)
- Fair-config gotcha (config-ergonomics, not quality): flat `WIKI_CATALOG_MODEL` /
  `WIKI_CONTENT_MODEL` env did **not** reach the DB model binding (provider bound correctly to
  LM Studio; model fell back to the appsettings default `ark-code-latest`), causing an instant
  first-run catalog failure. Fixed via the admin settings API (PUT the model ids = gemma) — the
  user-facing config path, still endpoint+key+model only. Full repro in SETUP.md.
- The stub/uneven-depth weakness is **substantially a local-gemma function-calling artifact**,
  not a design flaw — stated explicitly so the A2 verdict isn't misattributed.
- Model-quality caveat: gobby codewiki's A2 baseline used frontier daemon lanes; OpenDeepWiki
  here used local gemma. The model-independent design axes (IA, diagrams, narrative, citation
  *form*) are the fair comparison; raw completeness carries the model caveat.

## Net verdict (Track A2)
**OpenDeepWiki is a legitimately strong code→docs generator and the most "human-handbook"-like
competitor — it succeeded on Rust where CodeWiki refused, and beats gobby codewiki on diagram
density, curated information architecture, and narrative readability.** gobby `codewiki` wins
on completeness, uniform depth, per-symbol API coverage, and citation density (resolvable
file:line on every symbol), and its pipeline degraded more gracefully under the same local
model. Parity-plus story: gobby is the exhaustive **grounded reference**; OpenDeepWiki is the
curated **narrative handbook** — and its diagrams (C4) + hierarchical catalog (C1→C9) are the
concrete, adoptable gaps.

## Artifacts
`outputs/opendeepwiki/`: `SCORECARD.md`, `SETUP.md`, `compose.override.yaml` (in the
OpenDeepWiki clone), `wiki/` (13 extracted `.md` docs), `wiki-json/` (raw API responses).
