# gobby-wiki: Parity + Research Pipeline Plan

## Status: READY FOR EPIC DECOMPOSITION (incorporates Codex review feedback)

## Decisions taken in Josh's absence (defaults; override any before execution)
1. **Session scope**: project vault + project filtering (this repo's sessions synthesize into the project vault; cross-project sessions go to `topic:sessions`). Resolves the Model A/B question in `.gobby/handoff-session-wiki-scope.md` toward Model A for this plan's scheduling.
2. **Digest link density** (revised per Codex review): the daemon handoff prompt stays resume-focused and untouched; wikilinked **Connections** extraction happens in gwiki's session-ingest summarize step (`crates/gwiki/src/ingest/session/summarize.rs`) — a bounded single-shot enrichment where the wiki consumes it, keeping entity-link density for upkeep clustering without touching the resume surface.
3. **Research agent default**: `claude/sonnet` (nightly-affordable; ad-hoc runs override provider/model via pipeline inputs).
4. **Codewiki nightly**: enable by default (`wiki.codewiki_nightly_enabled=True`); hash-reuse bounds steady-state cost; first full run is a one-time spend.

## Decisions from Josh's review feedback (incorporated)
5. **No legacy anything** (pre-0.5.0): delete the `wiki:research` sweep, delete the `.gobby/wiki` migration path, clean renames without aliases.
6. **Shared lint** lives in `gobby_core::vault` (both crates already depend on gcore; `markdown.rs` precedent) — not a copy into gwiki.
7. **Vault dir** reverts to `wiki/` default with `gobby-wiki/`, `gobby-wiki-001`… fallback chain, resolved in gcore, adopted everywhere.
8. **Session ingestion payoff** = nightly `gwiki recap` ("Recap of today's work") + Connections-driven concept synthesis + session-start overview injection; bakeoff evidence says build for humans AND agents (separate surfaces).
9. **Mermaid**: end state (Josh's direction) = LLM composes diagrams (Lane B) from supplied graph evidence; deterministic code supplies edges, validates syntax (mmdc repair loop), and verifies every drawn edge against evidence — same grounded contract as prose. Staged per Codex review: Phase 1 first suppresses fabricated deterministic flows + fixes escaping/syntax (strictly better than today); LLM composition ships as Phase 8 behind a tested evidence contract.
10. **Epic execution**: work lands as a gobby-tasks epic with the phase structure below, not 1–2 parallel shots.

## Context

Josh wants gobby-wiki to implement the Karpathy llm-wiki + auto-research paradigm in Rust, at or above the quality bar of https://github.com/AgriciDaniel/claude-obsidian. Two goals:

1. **Wiki quality parity**: gobby-wiki output should match/exceed claude-obsidian's vault quality (synthesis, linking, structure, maintenance).
2. **Research pipeline**: submit a research question (e.g., "pull every new arxiv paper from the last 24h relevant to gobby, summarize, outline how it would improve gobby, produce an agent investigation prompt") and have the wiki ingest results and follow instructions — possibly via daemon nightly cron for ingestion + a gobby pipeline for research requests.

## Live-state findings (verified via gobby-wiki MCP, 2026-07-01)

Vault: `/Users/josh/Projects/gobby/gobby-wiki` (postgres runtime; FalkorDB, Qdrant, LM Studio embeddings `nomic-embed-text` all configured and connected).

**What works:**
- Session ingestion: 149 session transcripts synced → `raw/` → per-source digests in `knowledge/sources/` (good quality: Summary / Key Claims / Key Quotes / Connections / Contradictions, entity wikilinks)
- Health/audit/trust machinery fully functional and correctly diagnosing problems
- MCP surface is rich: wiki_search, wiki_ask (RAG with citations), wiki_read, wiki_ingest (paths + **URL batches already supported**), wiki_compile, wiki_audit, wiki_trust, wiki_health, wiki_sync_sessions, wiki_list_sources, wiki_remove_source

**What's broken (trust_status: attention_required):**
- `knowledge/concepts/`: **0 pages**. `knowledge/topics/`: **0 pages**. `code/`: only stub INDEX.md. `_index.md`, `knowledge/INDEX.md`, `log.md`: empty stubs. → **No synthesis layer exists in practice.**
- 544 broken wikilinks — source digests link to concept pages (`[[Gobby]]`, `[[PostgreSQL]]`, `[[FalkorDB]]`…) that nothing ever creates. Case-duplicate targets (`gcode` vs `Gcode`, `Gwiki`) → no link-target canonicalization.
- 149/149 sources reported **uncompiled** (even though source digests exist — possible state-tracking mismatch: verify how "compiled" is recorded vs. digest generation).
- 4,783 **unsupported claims** from audit across 149 source contexts.
- (`_gwiki/source-manifest.lock` at 0 bytes is normal — it's a lock path; manifest data lives in `raw/INDEX.md`.)
- Codewiki output empty despite nightly-refresh infra (`src/gobby/code_index/codewiki_nightly.py`, disabled-by-default cron `gobby:codewiki-nightly:<project_id>`; `CodewikiRefreshService` in `src/gobby/code_index/codewiki_refresh.py`).

## Reference repo (claude-obsidian) findings

v1.9.2. **Prompt-engineering project, no runtime**: 15 Markdown skills + 3 sub-agents + 4 slash commands drive Claude Code; Bash/Python scripts only guard invariants (file locks, monotonic ID allocation, transport detection, opt-in BM25+rerank retrieval). Three layers: `.raw/` (immutable sources) → `wiki/` (LLM-owned) → `CLAUDE.md`/`WIKI.md` (schema/instructions).

**Quality mechanisms worth stealing:**
1. **Update-over-create enforced redundantly** in every skill (ingest/save/research/lint) + semantic near-dup detector (`tiling-check.py`, local embeddings, cosine bands ≥0.90 error / 0.80–0.90 review). Main defense against wiki bloat.
2. **Ingest fan-out contract**: one source touches 8–15 pages — source page + entity pages + concept pages + domain `_index.md` + `overview.md` + `index.md` + `hot.md` + top-of-`log.md` + contradiction check. *This is exactly the synthesis layer gobby-wiki lacks.*
3. **Two-layer context cache**: `hot.md` (<500 words, refreshed by Stop hook, injected by SessionStart/PostCompact hooks) + `index.md` master catalog with per-page one-liners and live totals; per-folder `_index.md` sub-indexes.
4. **Provenance structural**: immutable `.raw/`, `sources:` frontmatter, inline `(Source: [[Page]])` citations, `.manifest.json` hash-based dedup/delta, opt-in stable page addresses (`c-NNNNNN`, flock-guarded counter).
5. **Contradictions surfaced never silently resolved**: reciprocal `> [!contradiction]` callouts on both pages; human decides.
6. **Lint** (10 checks: orphans, dead links, stale claims, missing pages for multiply-mentioned concepts, missing backlinks, frontmatter gaps, empty sections, stale index, address validity, semantic tiling) → tiered report, shows before auto-fix, human-judgment items never auto-fixed.
7. **Frontmatter schema** per type (source/entity/concept/domain/comparison/question/overview/meta) with `status: seed|developing|mature|evergreen` lifecycle, `aliases` for terminology, quoted wikilinks in YAML.
8. **Writing style rules**: declarative present tense, cite every non-obvious claim, no hedging, `> [!gap]` for uncertainty, atomic notes 100–300 lines, "write the knowledge not the conversation".
9. **Autoresearch** (`/autoresearch [topic]`): ≤3 rounds (broad 3–5 angles × 2–3 WebSearch + WebFetch top 2–3 → gap-fill ≤5 searches → optional synthesis pass), caps (≤15 pages/session, ≤5 sources/round), user-editable "program" (source authority prefs, confidence scoring, domain presets: AI→arXiv+GitHub), egress hygiene (scheme/host filtering, strip script/iframe, escape `[[ ]]` to block wikilink injection, 50KB cap). Output = wiki pages ("The wiki is the product. Chat is just the interface") + master `questions/Research: [Topic].md` synthesis page (Overview/Key Findings cited/Entities/Concepts/Contradictions/Open Questions/Sources).
10. **Boundary-first topic selection**: `boundary_score = (out_degree − in_degree) × recency_weight` surfaces frontier pages as research candidates.

**What it LACKS (our differentiators):** no scheduling/cron (all human-invoked), no arxiv/RSS poller, no daemon, no code index/codewiki, no session-transcript ingestion, no multi-CLI. Its "recurring" work = manual lint + manual log-folds + hot-cache refresh hook.

## Karpathy paradigm findings

Canonical source: the **LLM Wiki gist** (gist.github.com/karpathy/442a6bf555914893e9891c11519de94f, Apr 2026 — Karpathy designates it "the improved version of the tweet"). Core tenets:
- "The LLM incrementally builds and maintains a persistent wiki … compiled once and *kept current*, not re-derived on every query." "The wiki is a persistent, compounding artifact."
- "Obsidian is the IDE; the LLM is the programmer; the wiki is the codebase." Human curates sources/directs/asks; LLM does everything else.
- Three layers: immutable raw sources → LLM-owned wiki (summaries, entity pages, concept pages, comparisons, overview, synthesis) → schema doc (CLAUDE.md/AGENTS.md) co-evolved with the human.
- `index.md` catalog (read first, then drill — "avoids the need for embedding-based RAG at ~100 sources/hundreds of pages") + append-only greppable `log.md`.
- Three operations: **Ingest / Query / Lint**. Ingest touches 10–15 pages. **Good query answers get filed back as wiki pages** ("shouldn't disappear into chat history"). Lint finds contradictions, stale claims, orphans, missing concept pages, missing cross-refs, "data gaps that could be filled with a web search."

**Correction on "auto-research":** Karpathy's `autoresearch` repo is an autonomous ML-training experimentation loop (agent edits train.py, 5-min budget, val_bpb keep/discard; human programs `program.md`) — NOT arxiv reading. The nightly-paper-watcher pattern is third-party (AutoSci `/daily-arxiv`, claude-obsidian `/autoresearch`). Josh's research-pipeline vision = LLM Wiki gist's "Research" use case + Lint's gap-filling + claude-obsidian's multi-round research loop, on gobby's scheduler. Transferable autoresearch ideas: human-programmed `program.md` as the standing-query definition; fixed budgets per run; append-only experiment/run log; keep-or-discard gating on a measurable check.

## Gobby implementation map

**Two Rust engines share the vault** (`crates/{gcode,gcore,ghook,gwiki}`):
- **CodeWiki** (`gcode codewiki`, `crates/gcode/src/commands/codewiki/`): code-derived docs from the Postgres code index. Grounded per-page prompts (`prompts/systems.rs`: FILE/MODULE/REPO/ARCHITECTURE/CONCEPT_PAGE/NARRATIVE_PAGE systems, "do not invent", cite file:line) + VERIFY_SYSTEM citation-auditor second pass. Lane A one-shot / Lane B agentic tool-loop. Hash-based reuse (`reuse.rs`), repair, purge, `--ai-depth`, aggregate profile default claude/opus@high → gpt-5.5@xhigh. Writes `code/files/`, `code/modules/`, handbook chapters. **Note: `gcode codewiki` defaults `--out` to `codewiki`, not the vault — callers must pass `--out <repo>/gobby-wiki` (daemon does; manual runs won't).**
- **gwiki** (`crates/gwiki/src/`): knowledge vault. Ingest handlers for url (+wayback), pdf, html/office, image+vision, audio+transcribe, video, mediawiki, git, sessions (codex/droid/grok/qwen/daemon), inbox collect. `SourceManifest` (content-hash dedup, `CompileStatus {Pending, Compiled}`, `IngestionMethod {Manual, Research}`). Synthesis: `SynthesisKind {Source, Concept, Topic}`, grounded `EXPLAINER_SYSTEM` (12K token budget, `[source: path]` citations). `ResearchSession` checkpoints (topic, accepted notes under `raw/research/`). `ask` = thin RAG. `audit`/`citation-quality`/`credibility`. CLI verbs incl. `compile [TOPIC] --kind {source,concept,topic} --source <id> --target <page> --outline <h> --write-intent`, `librarian`, `lint`, `link-suggest`, `normalize`, `health`, `graph-context`, `export`.
- **Python daemon orchestrates via gateways**: `GcodeGateway`, `GwikiGateway` (`src/gobby/gwiki_gateway.py`) shell the binaries; `gobby-wiki` MCP registry in `src/gobby/mcp_proxy/tools/wiki.py` (writes go through `WikiUpdateCoordinator` → re-index). HTTP routes `src/gobby/servers/routes/{wiki,code_index}.py`. Post-commit trigger (`codewiki_trigger.py`, gated `wiki.codewiki_on_commit` default **False**, debounced) and nightly cron (`codewiki_nightly.py`, `wiki.codewiki_nightly_enabled` default **False**). Wiki branch publishing: sibling `../<repo>-wiki` worktree on branch `wiki`, mirrored by pre-push hook (`src/gobby/cli/installers/wiki_branch_setup.py`).
- Code is mature — no TODO/stub markers; active work on AI routing, compile-via-Lane-B, purge, session-sync hardening.

**Root-cause diagnosis (why the vault is a shell):**
1. **No conductor for the back half.** Only `sync-sessions` ever ran. `compile` is per-topic/per-source and driven by research checkpoints; nothing iterates pending sources → concept/topic pages. All 149 sources: `compile_status: pending`.
2. **Both codewiki triggers disabled by default** and never enabled → `code/` empty.
3. **Index/log maintenance missing entirely**: `_index.md`, `knowledge/INDEX.md`, `log.md` are scaffolded stubs with no code path that populates them (Karpathy's index.md catalog + append-only log are core to the paradigm).
4. **Session digests emit `[[Entity]]` wikilinks but no step creates entity/concept pages** → 544 broken links; no link-target canonicalization (`gcode`/`Gcode`/`Gwiki` variants).
5. **Session digests carry no `[source: path]` citation markers** → audit counts every claim unsupported (4,783) and librarian flags all 149 pages `missing_citations`. The digest prompt (`ingest/session/summarize.rs`) and the audit contract disagree.
6. **Librarian service wiring broken**: standalone `gwiki librarian` reports `outdated_codewiki` (code graph unavailable), `semantic_gaps` (Qdrant/embeddings unavailable), `patch_suggestions` (model provider unavailable) — while `wiki_trust` from the daemon reports all three configured. Librarian doesn't resolve services the way ask/compile do.
7. Librarian check set (stale_pages, missing_citations, broken_links, weak_provenance, outdated_codewiki, semantic_gaps, patch_suggestions → task proposals + patches) is the germ of Karpathy's Lint op — it just never runs and its output goes nowhere (no task creation, no scheduled cadence).

## Daemon integration points (cron/pipelines/agents/LLM)

**Design constraint from Josh:** research sources are examples, not requirements — arxiv today, law reviews tomorrow. The primitive is a **source-agnostic research request** (NL instructions + output contract); recurring feeds are just saved requests on a cron schedule. No per-source fetcher code in the daemon.

Everything needed already exists:
- **Cron**: `CronScheduler` + `CronExecutor` (`src/gobby/scheduler/`), 5 action types: `agent_spawn`, `pipeline`, `shell`, `handler`, `dispatcher`. System-job registration pattern = `src/gobby/code_index/codewiki_nightly.py` (`register_*_cron` + reconcile helpers in `CronJobStorage`); wiring site `src/gobby/runner_init/orchestration.py:279–404`.
- **Pipelines**: `PipelineDefinition`/`PipelineStep` (`src/gobby/workflows/definitions.py:611/:565`), executor `pipeline_executor.py`. Step types: `exec`, `prompt` (LLMService `pipelines.prompt_step`), `mcp` (any MCP tool), `invoke_pipeline`, `wait` (block on spawned-agent run_id), plus `condition` and `approval` gates. **`src/gobby/install/shared/workflows/pipelines/nightly-fixes.yaml` is a near-exact blueprint**: re-entrancy guard → create_task → spawn_agent (provider/model pinned) → wait → condition on status. Bundled YAML auto-syncs to DB via `sync_pipelines.py`.
- **Agent spawning**: `spawn_agent_impl` (`src/gobby/mcp_proxy/tools/spawn_agent/_implementation.py:153`) — headless via tmux, works from cron and pipeline MCP steps, `isolation` none/worktree/clone, agents get WebSearch/WebFetch natively via their CLI. → **agent-side discovery, not daemon-side fetching.**
- **LLM service**: `LLMService.call_feature/call_json_feature` (`src/gobby/llm/service.py:88/:121`), feature→profile→provider mapping; `pipelines.prompt_step` feature already wired for pipeline prompt steps. (Caveat from docs/reviews/llm-prompts.md: some paths accept empty-string generations as success.)
- **Web fetch**: no arxiv/RSS code exists; httpx pervasive. `GwikiGateway.ingest_url` → `gwiki ingest-url` does its own fetching (no daemon-side fetch needed).
- **Wiki write path**: `gobby-wiki` MCP (`src/gobby/mcp_proxy/tools/wiki.py`) → `GwikiGateway` (`src/gobby/gwiki_gateway.py`) → `gwiki` binary. Tools: wiki_ingest (paths|urls), wiki_ask, wiki_search, wiki_read, wiki_compile, wiki_sync_sessions.
- **Memory**: `facade.create_memory(...)` (`src/gobby/memory/facade.py:117`); agents can use the memory MCP surface directly.
- **Tasks**: pipelines create follow-up tasks via `gobby-tasks/create_task` MCP step; returned id feeds `spawn_agent(task_id=...)` (nightly-fixes.yaml shows the exact pattern).

## Codewiki quality pipeline & vault-path findings (code-verified)

**"Codewiki linter" correction:** there is no standalone lint verb. Codewiki's quality machinery is generation-time prevention: code-citation grounding/validation (`text/citations.rs`), link sanitization explicitly tuned to pass `gwiki lint` (`text/sanitize.rs`), strict markdown normalization (`strict_markdown.rs`, already layered on shared `gobby_core::markdown::normalize_markdown`), AI grounded verify (`text/verify.rs`), mermaid gate, orphan GC in `DocSink::finish`, plus `gcode codewiki --repair-citations` (index-backed re-anchoring of stale `[file:line]` citations, no AI). gwiki has the report-style verbs (lint/audit/librarian/normalize/citation-quality). Net: **codewiki = prevention at write time + citation repair; gwiki = post-hoc vault reporting.**

**Shared lint core:** both crates depend on `gobby-core` (verified Cargo.tomls); precedent exists (`gcore/src/markdown.rs`, contract modules). Vault-generic checks to hoist into a new `gobby_core::vault`/`::lint` module: markdown hygiene (unify `strict_markdown` + gwiki `normalize`), frontmatter presence/shape, wikilink resolution + broken-link/orphan/backlink/duplicate-alias detection, mermaid validity (gwiki's `lint/diagrams.rs` is pure text — ideal hoist), index consistency. Engine-specific stays per crate behind a `CitationValidator` trait: gcode validates `[file:line]` against the symbol index; gwiki validates source citations against `raw/` + provenance. Vault layout constants (`STATE_ROOT="_gwiki"`, scope.json) currently live in `gwiki/src/vault.rs` while gcode's walker re-hardcodes the layout — move to gcore.

**Vault-dir rename inventory (`gobby-wiki` → `wiki` default + fallback chain):**
- Vault marker: a directory is a vault iff `_gwiki/scope.json` exists (`crates/gwiki/src/vault.rs:20,88`).
- Resolver spec (new, in gcore; adopted by gwiki `scope.rs:149` which today hardcodes `project_root.join("gobby-wiki")` with no fallback): (1) `<repo>/wiki` absent → use it (fresh init); (2) `<repo>/wiki` exists and is a vault → use it; (3) `<repo>/wiki` exists but is NOT a vault (collision) → `<repo>/gobby-wiki`, then `gobby-wiki-001`, `-002`… (same test each step). Note: `~/wiki` is the *topic hub* default (`scope.rs:187`) — different concept, unaffected.
- Rust touchpoints: `gwiki/src/scope.rs:149` (+tests), `gwiki/src/obsidian.rs:95-97` (gitignore glob), `gcode/src/index/walker/hidden.rs:7,137,147-178` (+tests), `gcode/src/commands/codewiki/paths.rs:83` (`is_core_file` self-exclusion). `gcode` codewiki's own `DEFAULT_OUT_DIR` is already `"codewiki"` — the daemon imposes `gobby-wiki`.
- Python touchpoints: `code_index/codewiki_refresh.py:14` (`DEFAULT_CODEWIKI_OUT_DIR`), `config/wiki_migration.py` (extend: `gobby-wiki` → `wiki` roots migration; delete the `.gobby/wiki` legacy path per no-backcompat), `memory/dream/truth_digest.py:26-27`, `cli/installers/wiki_branch_setup.py:11` (`GOBBY_WIKI_DIR`), `cli/installers/git_hooks.py:236-253` (pre-push shell script hardcodes `$REPO_ROOT/gobby-wiki` — installed hooks must be rewritten), `cli/_install_prompts.py:461-470` (echo strings), docstrings/comments in `codewiki_nightly.py:173`, `runner_init/orchestration.py:368`; docs `docs/guides/configuration.md:56-60`.
- Persisted state to handle: `wiki.roots` config values, the on-disk vault (physical rename), installed pre-push hooks, `.gitignore` entries, `truth_digest.json` path.
- NOT the vault dir (exclude from rename): the `gobby-wiki` Rust *package* name, crates.io/install references, MCP server name, tempfile prefixes.

## Mermaid diagram diagnosis (code-verified)

All mermaid is deterministic — every codewiki LLM prompt forbids fences/diagrams; the sole source is `crates/gcode/src/commands/codewiki/architecture_diagrams.rs` (+ `build_parts/curated_content.rs` for concept/narrative "conceptual flow"). Both paths gate output through `is_valid_mermaid` and omit failures, so blocks are syntactically valid but semantically wrong. (Note: `~/Projects/gobby-cli/gobby-wiki/` is currently empty; examples reconstructed from the generator's own unit-test assertions — current HEAD reproduces them.)

Root causes:
- **RC1 (the "garbage flow")**: `curated_flow_diagram` chains page members into one linear `s0 --> s1 --> …` in **arbitrary declaration order** — `order_components_by_hint`/`parse_flow_chain` only reorder when member summaries literally contain an `A -> B -> C` arrow chain, which LLM prose never does. The diagram asserts a data flow that doesn't exist; concept groupings are semantic, not sequential. Fix in `curated_content.rs:614/728/753` + `render_conceptual_flow` (`architecture_diagrams.rs:542-595`): emit a flow only when a genuine ordered flow is evidenced (e.g., from the code graph CALLS edges), otherwise emit a grouping diagram or nothing.
- **RC2**: `mermaid_label` (`architecture_diagrams.rs:398`) escapes to HTML numeric entities (`&#40;`) which render as literal garbage when htmlLabels is off (GitHub strict mode, some Obsidian setups); mermaid's own syntax is `#40;`. Also `role_phrase` (`curated_content.rs:816`) produces mid-thought 8-word label fragments.
- **RC3**: topology flowchart's "Runtime routing" subgraph (`cli{{…}}` node, `architecture_diagrams.rs:216-228`) is a disconnected island; dotted service edges form a hairball on large workspaces.
- **RC4**: `is_valid_mermaid` (`architecture_diagrams.rs:433-515`) is a shape check, not a grammar check — it green-lights modern-only syntax (`subgraph x ["title"]`, `([...])`, `{{...}}`) that older embedded renderers reject. Constrain emitted syntax to a broad-compatibility subset and/or validate with a real mermaid parser in tests.

## Wiki-bakeoff findings (llm-wiki, ~/Projects/wiki-bakeoff)

The Track-B entry (`llm-wiki`, adoption candidate C10) is the only bakeoff tool that ingested sessions. **It was useful to both humans and agents, via deliberately separate surfaces:**
- **Humans**: Obsidian Dataview dashboard (recent/by-confidence/by-lifecycle/orphans/open-questions), flat counted `index.md`, rolling `hot.md` = "last 10 session summaries" (closest analog to Josh's nightly recap), living `overview.md`, append-only greppable `log.md`, static HTML site with RSS + token/tool heatmaps.
- **Agents**: `ai-readme.md` (navigation guide for AI), `llms.txt`/`llms-full.txt` context dumps, `graph.jsonld` + per-page `.json` siblings, per-folder `_context.md` for token-frugal traversal, MCP query server ("what did I decide about X"), and a pending-prompt agent-delegate loop (synthesis piggybacks on the next agent turn).
- **Why it worked**: deterministic model-independent core (per-CLI transcript parsing, rich frontmatter metadata — model/tools/token totals/duration/branch, secret redaction, live-session guard, immutable raw layer) + thin bounded single-shot synthesis (Summary/Key Claims/Quotes/Connections with wikilinks — survives local models because it's one completion, not an agent loop). gwiki's existing digest pipeline already matches this shape.
- **Quality governance worth adopting**: 4-factor confidence score (`sources*0.3 + quality*0.3 + recency*0.2 + xrefs*0.2`) with content-type Ebbinghaus decay (bug facts 14d half-life, architecture 180d); 5-state lifecycle (draft→reviewed→verified→stale→archived); candidate quarantine (LLM-proposed entities untrusted until promoted); read-only `/wiki-reflect` cross-session pass (recurring themes → concept pages, co-occurring entities → comparisons, repeated open questions → question pages); contradiction preservation everywhere.
- Transferable from other entries: per-claim `EXTRACTED|INFERRED|AMBIGUOUS` provenance labels (Graphify C7), token-budget-capped query output (C8), LLM-named semantic clusters for digests (C5/C9).

## Gap analysis

### Where gobby already exceeds claude-obsidian
- Real runtime: daemon, cron scheduler, pipelines, headless agent spawning (claude-obsidian is prompt-files-only, zero scheduling).
- Multi-modal ingestion in Rust (URL+wayback, PDF, office/HTML, image+vision, audio, video, mediawiki, git, 5 session formats) vs. their WebFetch+defuddle.
- Grounded generation with a second-pass citation auditor (VERIFY_SYSTEM) and claim-level audit (`gwiki audit`, `citation-quality` contradictions) vs. their honor-system prompts.
- Search: BM25 (pg_search) + semantic (Qdrant) + graph boost (FalkorDB) vs. their opt-in local BM25+rerank.
- Content-hash SourceManifest dedup, session-transcript ingestion, codewiki (code-derived docs), wiki branch publishing, health/trust observability.

### Where claude-obsidian/Karpathy beat us today (the parity gaps)
1. **The synthesis loop never runs** — their ingest touches 8–15 pages (entities, concepts, indexes, log, contradictions); ours stops at the per-source digest. This is THE gap: 0 concepts/topics/entities, 544 broken links, empty indexes.
2. **No index.md/log.md discipline** — Karpathy's core navigation mechanism ("read index first, then drill"); ours are stubs with no maintaining code path.
3. **No update-vs-create enforcement / dedup at the concept layer** — theirs enforces redundantly + semantic near-dup detection; ours never creates the layer at all, and link targets aren't canonicalized (`gcode`/`Gcode`).
4. **No recurring lint→fix loop** — librarian/lint/audit exist but never run, propose-only, and file no tasks. Their lint runs every 10–15 ingests with tiered report + safe auto-fixes.
5. **Citation contract mismatch** — session digests don't emit `[source: path]` markers → 4,783 "unsupported claims" false positives drown real audit signal.
6. **No research loop** — they have /autoresearch (rounds, caps, egress hygiene, synthesis page with contradictions/open questions); our ResearchSession/compile plumbing exists but has no driver, no discovery, no scheduling.
7. **No session-context layer from the wiki** (their hot.md + SessionStart injection) — gobby has memory-recall injection already, so this may be partially covered; decide minimal correct integration.
8. **Librarian service wiring broken standalone** (semantic_gaps/patch_suggestions/outdated_codewiki degrade outside daemon context).

### Integration opportunities (gobby-wiki × codewiki × daemon)
- Codewiki pages + knowledge concepts share the vault but never cross-link; concept pages about code topics should link `[[code/modules/...]]` and vice versa (librarian's `outdated_codewiki` check hints this was intended).
- Session digests → concepts →← memory system: research findings as memories AND wiki pages (memory recall already injects at session start; wiki could be the durable, browsable layer over the same knowledge).
- Research follow-ups → gobby-tasks → `gobby build` dispatch: the wiki generates investigation prompts; the task system executes them. Closes Josh's "prompt to give to an agent" loop natively.
- Nightly: one maintenance window chaining codewiki refresh → synthesis conductor → librarian → health → task filing.

## Proposed plan

### Part B — Research pipeline (design complete, code-verified)

**Verified enablers:** `gwiki compile` already accepts explicit TOPIC + `--source <id>...` (converts manifest sources to accepted notes, replaces checkpoint state — hijack-safe when topic+sources always explicit) + `--target <page>`; accepted-note contract is plain markdown with `citation:`/`gap:`/`conflict:` line prefixes (`crates/gwiki/src/compile/collect.rs:99`); SourceManifest dedups unchanged URL re-fetches; gwiki URL fetch already has SSRF/scheme/size guards (missing only `[[ ]]` escaping); cron→pipeline plumbing complete incl. `expose_as_tool: true` auto-registering pipelines as MCP tools; `gobby pipelines run <name> --input k=v` and full cron CRUD exist.

**Decisions:**
- **Primitive**: research request = inputs of a new bundled `wiki-research` pipeline (`question` NL text incl. custom output contract, `topic_slug`, `max_sources=12`, `max_items=8`, `create_tasks`, `provider`, `model`). Ad-hoc = pipeline execution; **standing query = ordinary cron job** (`action_type: pipeline`) — no new tables/registries; cron already provides name/schedule/enable/history/CRUD.
- **Execution**: pipeline skeleton (re-entrancy guard → create_task → spawn `wiki-researcher` agent → wait → status gate, nightly-fixes.yaml pattern) + agent judgment (discovery via native WebSearch/WebFetch — source-agnostic, curation, note authoring) + gwiki mechanics (guarded fetch, dedup, grounded compile).
- **Agent outputs**: `wiki_ingest(urls)` for sources → per-item notes in accepted-note format with `## Summary` / `## How this improves gobby` / `## Investigation prompt` + `citation:` lines → `wiki_ingest(paths)` → `wiki_compile(topic, kind=topic, sources=[...], target=knowledge/topics/<slug>.md)` → run report page + `log.md` append → follow-up tasks via `gobby-tasks:create_task` (tag `wiki-research`, `allow_automation` untouched — no auto-dispatch storms).
- **Relevance scoping**: `gcode repo-outline` + README (works while synthesis layer still empty) + `wiki_ask` when vault has content.

**Steps (dependency order):**
1. Extend compile passthrough: `src/gobby/gwiki_gateway.py` (`compile` gains topic/kind/sources/outline/target/write_intent/ai) + `src/gobby/mcp_proxy/tools/wiki.py` (`wiki_compile` mirrors); tests in `tests/test_gwiki_gateway.py`, `tests/mcp_proxy/tools/test_wiki.py`.
2. New bundled skill `src/gobby/install/shared/skills/wiki-research/SKILL.md` (angles→discovery→curation→dedup check→ingest→note template→explicit-topic compile→tasks→run report; question's own output contract overrides default template). Regenerate `bundled_content_manifest.json`.
3. New bundled agent `src/gobby/install/shared/workflows/agents/wiki-researcher.yaml` (blueprints: nightly-linter.yaml + researcher.yaml; claim→load_skill→research→terminate; timeout 2700s, max_turns 120, no spawn/kill tools).
4. New bundled pipeline `src/gobby/install/shared/workflows/pipelines/wiki-research.yaml` (`expose_as_tool: true`, typed inputs).
5. E2E with Josh's canonical arxiv question verbatim; re-run to prove dedup; `wiki_audit` must not flag the topic page.
6. Standing query via `gobby cron add ... --action-type pipeline`; smoke `cron run`/`toggle`; docs.
7. Hardening (Rust): escape `[[ ]]` in fetched bodies (`crates/gwiki/src/ingest/url/render.rs`) — the one claude-obsidian egress control gwiki lacks.

### Part A — Synthesis/maintenance loop (design complete, code-verified)

**Additional verified root causes:**
- `compile_to_wiki_with_options` (`crates/gwiki/src/compile/mod.rs:111-222`) already does synthesis→page write→index update→provenance→mark-compiled; it's just never driven.
- Audit only honors provenance-graph links / inline markers / a codewiki-only frontmatter exemption — session digests' structural frontmatter provenance (`source_hash` etc.) isn't recognized (`crates/gwiki/src/audit/claims.rs:15-44,297-305`) → the 4,783 false positives.
- Link resolution is case-sensitive (`crates/gwiki/src/links.rs:352-383`) → `gcode`/`Gcode` split.
- `log::append_logs` has **zero production callers**; `_index.md` only ever gets compile's "Compiled Pages" line.
- Librarian bug: `commands/librarian.rs` passes `Options::default()` which hard-codes all three service flags `false` (never probes, unlike status/trust/compile); `semantic_gaps`/`patch_suggestions` push `Vec::new()` — never implemented.
- Compile would currently duplicate source pages (`synthesize_source_pages` writes new title-slugged stubs instead of reusing existing digests).
- A per-scope wiki cron skeleton **already exists and is enabled**: refresh (1h), health (30m), audit (24h), sync-sessions (24h) in `src/gobby/wiki/scheduled_jobs.py:144-214` — extend this, not codewiki_nightly.

**Decisions:** entities = concept pages with `tags: [entity]` + `aliases` (no new SynthesisKind); conductor = new **`gwiki upkeep`** subcommand (Rust; drains pending sources by clustering case-folded unresolved link targets, mention_count ≥ 2, budgeted `--max-pages 10`); update-over-create in 3 layers (exact/alias match → Qdrant near-dup ≥0.90 update / 0.80–0.90 create+flag → recompile same target with existing body in prompt); link canonicalization = case-insensitive lookup keys (no content rewriting) + observed variants → `aliases`; indexes/log = deterministic Rust `catalog::regenerate` (no LLM); citation fix = audit-side structural exemption for manifest-backed `knowledge/sources/` digests (don't pollute digest prompt with self-referential markers); librarian probes services via shared resolver + real `semantic_gaps` implementation; scheduling = add `upkeep` (24h) + `librarian` (24h) to existing wiki scheduled jobs, librarian `suggested_tasks` filed into gobby-tasks (category docs, label `wiki-librarian`, deduped); codewiki nightly default → enabled + `gcode codewiki` `DEFAULT_OUT_DIR` → `gobby-wiki`; hot cache = bounded `## Overview` block in regenerated `_index.md`, injected at session_start via new seeded `wiki_overview` variable + bundled rule (pattern: `_seed_memory_recall_vars`); upkeep creates Concepts only (topics stay curated).

**Steps (dependency-ordered):**
1. Audit structural provenance for source digests (`audit/claims.rs`) → unsupported claims 4,783 → ≈0.
2. Case-insensitive link resolution + suggestion clustering (`links.rs`, `lint.rs`, `graph/mod.rs`).
3. Deterministic `catalog.rs` (regenerate `_index.md`/`knowledge/INDEX.md`/`code/INDEX.md`) + wire `log::append_logs` into ingest/compile/upkeep.
4. Compile reuses existing source digests (`SynthesisSource.existing_page`; existing-body-aware explainer prompt; `aliases`/tags in frontmatter).
5. `gwiki upkeep` conductor (new `upkeep.rs` + command; shared select/probe modules extracted from compile).
6. Librarian service probing + real semantic_gaps (`commands/librarian.rs`, `librarian.rs`, new `support/services.rs`).
7. CLI/contract plumbing (main.rs, api.rs, contract.rs, docs/contracts/gwiki-cli.md).
8. Daemon gateway + MCP + HTTP (`gwiki_gateway.py` upkeep/librarian, `wiki.py` wiki_upkeep/wiki_librarian, routes, ignore_globs for `meta/{librarian,upkeep}/**`).
9. Nightly scheduling + task filing (`src/gobby/wiki/scheduled_jobs.py` + handlers; per-scope tests).
10. Codewiki activation (config default flip + Rust out-dir default + first full run).
11. Session-start wiki overview injection (seeder + bundled rule).
12. Rollout: rebuild/reinstall binaries → daemon restart → drain 149 pending via `gwiki upkeep` → first `gcode codewiki --ai auto` → acceptance gates via trust/audit/lint/librarian.

### Hooks mapping (claude-obsidian → gobby)
Their four CC lifecycle hooks all compensate for having no runtime: SessionStart (inject hot.md + reap stale locks), PostCompact (re-read hot.md), PostToolUse Write|Edit (git auto-commit of the vault = their only persistence/sync), Stop (LLM-regenerate hot.md). Gobby equivalents: session-start injection = the planned `wiki_overview` variable + bundled `inject_context` rule (Part A step 11) — and since post-compaction restarts flow through `session_start` with `session_source == "compact"` (verified `src/gobby/hooks/event_handlers/_session_start/flow.py`, `handoff.py`), the same rule covers PostCompact for free; auto-commit is unnecessary (Postgres + SourceManifest + wiki-branch pre-push publisher are the persistence/sync layer; writes serialize through WikiUpdateCoordinator); Stop-hook hot-cache regen is replaced by deterministic `catalog::regenerate` (upkeep/compile) + existing session-end handoff summaries and session sync. Net new work beyond step 11: none.

**Legacy removal (no backward compatibility — pre-0.5.0):**
- **Delete** the legacy `wiki:research:<scope>` disable-sweep in `src/gobby/wiki/scheduled_jobs.py:317-357` and hard-delete any remaining legacy research cron rows from the DB during reconcile (one-time removal, then the code path is gone). A `gwiki research` verb existed and was retired; do not reintroduce it — research is agent work. Standing queries are pipeline-type cron jobs with user-chosen names.
- **Delete** the `.gobby/wiki` → `gobby-wiki` config migration shim (`src/gobby/config/wiki_migration.py` + its call site in `config/_loading.py`): any remaining `wiki.roots` entries pointing at `.gobby/wiki` are invalid config, not a supported migration path.
- Part B step 1: **rename** `GwikiGateway.compile(output=...)` to `target=` cleanly — no alias; update the existing `{"output": ...}` tests to the new signature instead of preserving them.
- General rule for execution: when a change obsoletes a code path, delete the path and its tests in the same commit — no deprecation stubs, no compat aliases, no "keep the old row disabled".

## Cross-cutting decisions (mine)
- Part B step 7 (wikilink-escape hardening in `ingest/url/render.rs`) ships in this milestone — small, correct, closes the one egress control claude-obsidian has that we lack.
- Research follow-up tasks: cap = `max_items` (default 8), `allow_automation` off — enters normal backlog; auto-dispatch is a later per-task toggle.
- Execution order: superseded by the Epic structure below (signal → synthesis → prove → research → vault migration → shared lint → scheduling → mermaid LLM composition).

## Epic structure

Execute as an epic in gobby-tasks (Josh: "break the task into an epic so we're not trying to do it in 1-2 parallel shots"). Each leaf task is one agent-session-sized. Phases 1, 2, and 5 can start in parallel; 3 depends on 1–2; 4 depends on 3; 6 is rollout.

**Epic: gobby-wiki — Karpathy llm-wiki parity + research pipeline**

Sequencing principle (per Codex review): fix signal first, build synthesis on the *existing* vault path, prove trust improvements on the current vault, then migrate the vault directory as its own phase, then activate schedules, with mermaid LLM-composition late.

**Phase 1 — Signal & guards (independently landable)**
- 1.1 Audit structural provenance for manifest-backed source digests (Part A step 1) — 4,783 unsupported claims → ≈0.
- 1.2 Case-insensitive link canonicalization + suggestion clustering (Part A step 2).
- 1.3 Librarian service probing + real `semantic_gaps` (Part A step 6).
- 1.4 Watcher ignore globs FIRST (`wiki.ignore_globs` defaults gain `meta/librarian/**`, `meta/upkeep/**`, `_meta/**`) so later scheduled writers don't cause self-observed churn.
- 1.5 Mermaid stop-the-bleeding (deterministic): suppress fabricated conceptual flows (emit nothing without a genuine ordered-flow signal), mermaid-native label escaping (`#40;` not `&#40;`), broad-compat syntax subset, real-parser validation in tests (`npx -y @mermaid-js/mermaid-cli`; `no-npx` already disabled for this). LLM composition is Phase 8.
- 1.6 Wikilink-escape hardening in URL ingest (`crates/gwiki/src/ingest/url/render.rs`).
- 1.7 Compile passthrough end-to-end: `GwikiGateway.compile` (clean `output`→`target` rename), MCP `wiki_compile`, **HTTP route `servers/routes/wiki.py:177` + schema**, and all contract tests (Part B step 1 + Codex #1).

**Phase 2 — Synthesis conductor on the EXISTING `gobby-wiki/` path (Rust; depends on 1.1–1.3)**
- 2.1 Compile digest-reuse + existing-body-aware updates + aliases/tags in frontmatter (Part A step 4).
- 2.2 Deterministic `catalog::regenerate` + `log::append_logs` production wiring (Part A step 3).
- 2.3 `gwiki upkeep` conductor (Part A step 5).
- 2.4 `gwiki recap`: nightly "Recap of today's work" — deterministic selection of the day's session digests per scope → one bounded single-shot synthesis (llm-wiki bakeoff pattern) → `recaps/YYYY-MM-DD.md` + rolling "recent work" block in `_index.md` Overview. Serves humans (morning read) and agents (session-start injection).
- 2.5 Connections enrichment in gwiki session-ingest summarize step (revised decision 2 — daemon handoff prompt untouched).
- 2.6 CLI/contract plumbing for upkeep + recap (Part A step 7).

**Phase 3 — Prove it on the current vault (no code; gates before any migration)**
- 3.1 Manual `gwiki upkeep` runs drain the 149 pending sources; verify trust/audit/lint gates (Verification below) on the existing vault. Quality spot-check against claude-obsidian's bar before scheduling anything.

**Phase 4 — Research pipeline (needs 1.7 + Phase 3's clean audit signal)**
- 4.1 `wiki-research` skill (Part B step 2).
- 4.2 `wiki-researcher` agent (Part B step 3) — verify `max_turns` actually flows through `SpawnRequest` (`src/gobby/agents/spawn_models.py:16` lacks the field per Codex #5); wire it through or rely on timeout only; explicit tool allowlist (gobby-wiki, gobby-tasks, gobby-skills get_skill, end_agent_run; block spawn/kill/review tools).
- 4.3 `wiki-research` pipeline, `expose_as_tool: true` (Part B step 4).
- 4.4 E2E canonical arxiv question + dedup re-run + standing-query cron + docs (Part B steps 5–6).

**Phase 5 — Vault migration (separate phase, split per Codex #3)**
- 5.1 Vault-dir resolver + layout constants (`STATE_ROOT`, scope.json detection) in `gobby_core`: default `wiki/`, fallback `gobby-wiki/`, `gobby-wiki-001`… (spec above).
- 5.2 Rust adoption: gwiki `scope.rs`, `obsidian.rs`; gcode walker `hidden.rs`, codewiki `paths.rs`.
- 5.3 Python adoption: `DEFAULT_CODEWIKI_OUT_DIR`, `GOBBY_WIKI_DIR`/branch setup, `truth_digest.py`, installers/git hooks/gitignore, `_install_prompts.py`; `wiki_migration.py` becomes solely `gobby-wiki` roots → `wiki` (the `.gobby/wiki` branch is deleted outright).
- 5.4 Physical rename + hook re-install for this repo; `wiki.roots` migration verified live.

**Phase 6 — Shared lint core (gcore; after migration so layout constants land once)**
- 6.1 `gobby_core::vault`: shared `LintFinding` types + vault-generic checks (markdown normalize unification, links/orphans/backlinks/duplicate-aliases/frontmatter/mermaid/index consistency) behind a `CitationValidator` trait; gcode codewiki and gwiki lint/normalize adopt; codewiki keeps write-time prevention + `--repair-citations`, gwiki keeps report verbs — one core underneath.

**Phase 7 — Scheduling & activation (after resolver/path behavior is stable)**
- 7.1 Nightly upkeep + librarian + recap jobs in `src/gobby/wiki/scheduled_jobs.py` + librarian task filing; **delete** the legacy `wiki:research` sweep with a system-row-aware cleanup (normal deletion protects system jobs, `src/gobby/storage/cron.py:547` — use the removed-automation cleanup pattern / explicit prefix deletion).
- 7.2 Codewiki activation (`codewiki_nightly_enabled` default True) + first full codewiki run into the resolved vault.
- 7.3 Session-start `wiki_overview` injection rule (hooks mapping above).

**Phase 8 — Mermaid LLM composition (Josh's end-state direction; separate feature behind a tested evidence contract)**
- 8.1 Lane B composes flowcharts from supplied evidence (real CALLS/import/dependency edges from the index + `system_model.rs`); deterministic validate→repair loop (mmdc); **edge verification**: every drawn arrow must match a supplied evidence edge (diagram analog of citation grounding). `architecture_diagrams.rs` becomes evidence-supplier + validator; prompts' "no diagrams" rule becomes "diagrams from supplied evidence only"; pages with no evidenced flow carry no diagram.

**Deferred (filed as backlog tasks in the epic, with reason):** bakeoff governance extras — 4-factor confidence scoring with content-type decay, 5-state page lifecycle, candidate quarantine, llms.txt/graph.jsonld agent exports. Reason: valuable but not required for parity or the research pipeline; each needs its own design pass against gwiki's existing credibility/audit modules to avoid duplicating them.

## Verification

**Part A acceptance gates (measured by existing machinery, this repo's vault):**
- `gwiki trust --format json`: `trust_status` leaves `attention_required`; `uncompiled_source_count` 149 → 0.
- `gwiki audit`: unsupported claims 4,783 → ≈0 on source digests; new concept pages grounded (`[source:]` + provenance).
- `gwiki lint`: broken links 544 → ≥90% reduction (case-folding + created concept pages); remainder enumerated by librarian.
- `knowledge/concepts/` non-empty with `aliases` capturing case variants; `_index.md` (with Overview block), `knowledge/INDEX.md`, `code/INDEX.md`, `log.md` populated; idempotent catalog regeneration (byte-identical on re-run).
- `gwiki librarian`: all seven checks `available: true`; suggested tasks appear in gobby-tasks (label `wiki-librarian`) after one nightly cycle, deduped on re-run.
- `code/` populated after first codewiki run; cron rows `gobby:wiki-upkeep:*`, `gobby:wiki-librarian:*`, `gobby:codewiki-nightly:*` present and enabled.
- Session-start injection: new session shows the wiki overview block once `_index.md` is populated.
- Unit: `cargo test -p gobby-wiki` (audit/links/catalog/upkeep/librarian), `cargo test -p gobby-code codewiki`, `GOBBY_TEST_PROTECT=1 uv run pytest tests/wiki/ tests/mcp_proxy/tools/test_wiki.py tests/test_gwiki_gateway.py -v`.

**New-scope acceptance gates:**
- Vault resolver: fresh repo → `wiki/`; repo with non-vault `wiki/` → `gobby-wiki/`; both occupied by non-vaults → `gobby-wiki-001`; existing vault at any step wins. This repo's vault physically renamed to `wiki/`, `wiki.roots` migrated, pre-push hook re-installed and publishing from the new path, gcode index still walks the vault (`wiki/**/*.md` allowlisted, metadata excluded).
- Shared lint: `gobby_core::vault` checks used by both `gwiki lint`/`normalize` and codewiki write-path; no behavioral regression in either crate's tests; one mermaid validity implementation shared.
- Mermaid (Phase 1 gate): no fabricated conceptual flows emitted; labels render correctly with htmlLabels off; emitted syntax passes `mmdc` in tests. (Phase 8 gate): LLM-composed diagrams pass the Lane B validate→repair loop; every arrow verifiably maps to a supplied evidence edge; pages without evidenced flows carry no diagram; no disconnected islands.
- Recap: after a day with sessions, `recaps/YYYY-MM-DD.md` exists, cites its digests, appears in `_index.md` Overview recent-work block, and is injected at session start.

**Part B acceptance gates:**
- E2E: run Josh's canonical arxiv question verbatim via `gobby pipelines run wiki-research --input question="..."`. Assert: execution success; `gwiki sources` lists fetched URLs + note sources; `knowledge/topics/<slug>.md` exists with `[source:]` citations; run report page + `log.md` append; follow-up tasks in gobby-tasks each containing a ready-to-use investigation prompt; `wiki_audit` does not flag the topic page.
- Re-run same question: unchanged URLs dedup (no new manifest entries), new run report.
- Standing query: `gobby cron add` a nightly job → `cron run` manual trigger works → `cron runs` shows dispatched pipeline execution → `cron toggle` pauses. Legacy `wiki:research:<scope>` disable-sweep does not touch it.
- Re-entrancy: second concurrent submission fails fast via the guard.
- Small-budget smoke first (HN question, max_sources=4, max_items=2) before the full arxiv run.
