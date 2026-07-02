# WP3 — Regenerate + general-wiki re-verification (2026-06-14)

End-to-end clean regen on a wiped + freshly-init'd vault. Binaries: gcode 1.1.0 / gwiki 0.4.0 (release, HEAD `15972ef`, after reviewing the 8 newly-landed commits #714–#717,#726,#729,#730,#731). Daemon restarted to load the vision SDK-envelope fix (`gobby` commit `fad234366`); vision verified end-to-end (real mascot description, `vision_status: extracted`).

## Step 1 — Binaries
`cargo build --workspace --release` (green), atomic deploy of gcode+gwiki to `~/.gobby/bin` with refreshed `.{name}-version` sidecars (shasums match). Focused gates green: `nextest -p gobby-code -p gobby-wiki` = 1182 passed/21 skipped; `clippy -p gobby-code -p gobby-wiki -D warnings` clean.

## Step 2 — Codewiki regen (`wp3-codewiki-verify.md`)
- 372 file docs + 67 module docs + repo + 6 aggregate `_*` pages. `--ai daemon --ai-aggregate-profile feature_high --ai-depth files`.
- Per-symbol purposes come from PostgreSQL hub symbol summaries (free at `files` depth); reuse cache (`_meta/codewiki.json`, files-mode) preserved unchanged file pages, aggregate + degraded + changed-source pages regenerated fresh.
- **All 134 broken-AI-lane stubs healed**: `exposes N symbols`=0, `has no indexed API symbols`=0.
- #727 content-only markdown doc pages pruned (0 `*.md.md`).
- 55 mermaid diagrams (module level); architecture/repo pages narrate richly with grounded citations and emit no fabricated edge-free diagram (the crates are independent binaries).
- **Link hygiene: 0 real broken links** after a scoped re-run stripped 8 absolute-path markdown links (reused pre-#726 summaries) via #726+#715. 2 residual `wikilink` findings are false positives (symbol summaries quoting `[[…]]`-producing code) → filed **#732**.
- One honest-degraded page: `_ownership.md` (`codeowners_unavailable` — no CODEOWNERS file exists — + bounded blame). Honest, not a pipeline failure.

## Step 3 — General wiki "by the book"
- `init`/`status` clean (datastore-ready, postgres runtime).
- `ingest-url` Wikipedia (RAG), `ingest-file` README.md, `collect` inbox note, **multimodal** logo.png → `vision_status: extracted` (fixed daemon path), all `ingested`.
- `index`: 451 documents, **Degradations: none**.
- Hybrid `search`: RRF fusion with per-result `sources:[bm25,semantic]` + per-engine `explanations`, compact snippets, hit-tied `code_citations`; top hits are the real `rrf.rs` pages.
- `compile` WP2b explainer (`--ai daemon`): generated `knowledge/topics/hybrid-search-result-merging.md` with `synthesis_mode: daemon`, accurate prose grounded by inline `[[source]]` citations. **Gap filed #733**: post-research-removal, no CLI populates the compile checkpoint/`accepted_notes`, so compile is unreachable on a fresh vault without a seeded `.gwiki/research-session.json` (seeded here to exercise the synthesis engine).

## Step 4 — Retrieval Q&A (two ways, both routes)
| Q | search+read | ask daemon | ask direct (LM Studio gemma-4-26b) |
|---|---|---|---|
| ghook inbox enqueue fails | 0.73s, transport.rs | 15.8s, cited | **8.0s**, cited (<10s) |
| RRF hybrid merge | rrf.rs | cited | — |
| UUID5 make_id parity | snapshot.rs | **honest refusal** (insufficient evidence — no hallucination) | — |
| FalkorDB unavailable | graph/reads.rs | cited (degrade to hints/empty/None) | — |

Follow-up #779 (`wp3-ask-daemon-latency-followup.md`) confirmed the 15.8s daemon
ask timing is provider-generation latency, not retrieval/RRF overhead. The
direct/local <10s target remains valid; daemon-routed cited answers use a <20s
soft target while `feature_low` resolves to the current Codex provider.

Direct route wired via a temporary `~/.gobby/gcore.yaml` `text_generate` binding (`${OPENAI_BASE_URL}`/`${OPENAI_API_KEY}` env-interpolated, no secret committed), restored to original after. The honest-refusal answer is a parity win vs DeepWiki chat (which hallucinates).

## Step 5 — Agent research-deposit demo
`search` gsqz exit-0 → cited note → `ingest-file` (`ingested`) → `index` (451 docs, none degraded) → `lint` (only the 2 known wikilink false-positives; deposit added no breakage). Note registered as a source with provenance.

## Final hygiene
`health`: 0 stale pages/citations/duplicate-concepts; broken_links = the 2 documented wikilink false-positives. `audit`: sources carry citations/provenance.

## Follow-ups filed
- **#732** codewiki: neutralize inline `[[wikilink]]`/`[](…)` syntax in symbol-summary prose (nitpick).
- **#733** gwiki compile unreachable on fresh vault — no CLI populates the accepted-notes checkpoint after research removal (bug, priority 2).
