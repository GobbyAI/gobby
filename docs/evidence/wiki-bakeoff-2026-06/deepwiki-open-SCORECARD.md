# DeepWiki-Open — bake-off scorecard (target: gobby-cli)

Run: 2026-06-14. Model: local LM Studio `google/gemma-4-26b-a4b-qat` (chat) + `nomic-embed-text-v1.5` (768-dim). Benchmark commit `43ed8a2`. Ingest: local read-only `git archive` of gobby-cli HEAD `ea6a26c` (1030 files, 421 `.rs`), avoiding the 209GB `target/`. DeepWiki's own defaults for all inference/chunking/retrieval (only endpoint+key+2 model ids were set). Generation: 10 pages, 821s on local gemma (~82s/page) — wall-clock is contended/model-bound, NOT a quality signal.

## What it produced
- **10 curated pages, 5 sections**: Project Introduction · Architecture Overview · Data Flow & Indexing Pipeline · per-crate pages (gcode, gwiki, gsqz, gloc, ghook) · Hybrid Search & Datastore Integration · Setup & Configuration.
- `wiki_combined.md` (49.5KB), `wiki_full.json` (60.4KB, machine-readable structure+content), `wiki_structure.xml`, per-page `.md`.

## Metrics
| Metric | Value | Notes |
|---|---|---|
| Pages | 10 | curated narrative, not per-file |
| Diagrams (mermaid) | 16 | all 10 pages have ≥1; accurate data-flow/arch graphs |
| Tables | 114 | command tables, data-model tables — very scannable |
| `Sources:` citations | 63 | `file:line` labels |
| Distinct source files cited | **27** / ~420 `.rs` | shallow file coverage — breadth via curation, not enumeration |
| Broken/empty citation links | **63/63** | every `Sources: [path:line]()` has an EMPTY `()` href (W1) |
| Hallucinations found | **0** | grounded to README/source; "refresh" is real, "research vault" framing matches CLAUDE.md |

## Human-reader axis: **STRONG**
Narrative, sectioned, newcomer-first. 114 tables + 16 diagrams + collapsible "Relevant source files" headers make it highly scannable. A newcomer learns gobby-cli's shape (6 crates, data flow, hybrid search) fast. This is DeepWiki's real strength and a legit win over browsing gobby's 460 reference pages top-down.

## Agent-consumer axis: **MEDIUM**
- `file:line` provenance is parseable, and `wiki_full.json` is structured — usable by an agent.
- BUT citation link targets are empty `()` (non-navigable, unverifiable as real ranges), only 10 pages → coarse targeted retrieval, and **no agent-native retrieval primitive** (DeepWiki ships a human chat UI, not a search/read/ask API an agent composes). Coverage of 27/420 files means most symbols are unreachable.

## vs gobby (honest)
- **Gobby wins:** coverage (460 pages vs 10; every code file + module + aggregates), diagram count (55 vs 16), citation *navigability* (real frontmatter ranges + lint-checked links vs empty `()`), agent-native retrieval (search/read/ask + hybrid RRF), honest-degradation contract.
- **DeepWiki wins:** human onboarding narrative, table density, and a deliberate *curated top-level structure* (→ adoption candidates C1, C2, C3).
- **Tie/accuracy:** both faithful; DeepWiki showed zero hallucination in this sample (grounded-to-source).
