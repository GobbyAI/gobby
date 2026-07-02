# CodeWiki (Python / `src/gobby/agents`) — bake-off scorecard

**Track A2 (code → documentation).** Run: 2026-06-14. CodeWiki updated to upstream HEAD `06e6d01` (3 commits past the benchmark pin; C/C++/Java parser changes only, zero Python impact). Model: local LM Studio `gemma-4-26b-a4b-qat`. Scope: clean `git archive` of `src/gobby/agents` (65 `.py`) as a standalone mini-repo. CodeWiki's OWN defaults (max_tokens 32768, depth 2; only url/key/3-model-roles set).

## Result: **FAILED — 0 doc pages (600s LLM timeout incompatible with slow local model)**
- **Phase 1 analysis: clean.** 65/65 files, 0 failed, 334 functions, 2596 relationships, 67 leaf nodes. `repo_dependency_graph.json` (826KB) is the only substantive artifact.
- **Phase 2 clustering: 0 modules** (52,224 tokens, 349s) → CodeWiki logged "Processing whole repo because repo can fit in the context window" → issued ONE whole-repo doc-generation request.
- **Phase 3 fatal:** that request hit the **OpenAI SDK default 600s timeout**. `src/be/llm_services.py::create_main_model()` builds `OpenAIProvider(base_url, api_key)` with **no `timeout`/`http_client`**, and CodeWiki exposes no flag to raise it. gemma cannot emit 32768 tokens in 600s → pydantic-ai `FallbackModel` retried the (same) fallback → second 600s timeout → chain exhausted. ~30 min Phase-3 wall clock. Confirmed a true timeout (ESTABLISHED TCP stream to LM Studio with active recv queue throughout), not a hang.

## Metrics
| Metric | Value |
|---|---|
| Doc pages | **0** (`module_tree.json` = `{}`, no `overview.md`, no per-module pages) |
| Diagrams | 0 |
| Citations (in practice) | **N/A** — no pages emitted to carry them |
| Citation mechanism (would-be) | file + line-range + verbatim `source_code` + `docstring` + `depends_on`, keyed `file::symbol` (e.g. `checkpoint_manager.py::CheckpointManager` L25-156) — resolves to real files/lines |
| Coverage | analyzed 65/65; **documented 0/65** |
| Hallucinations | none possible (no prose generated) |

## Human / agent axes: **N/A** (no documentation produced)

## Finding (fair, stands on its own)
**CodeWiki is unusable with slow local models out of the box.** Its hard-coded 600s LLM timeout + no override + reliance on a fast cloud model emitting 32K tokens means a local LM Studio model yields zero output. This is a real Track-A2 competitive finding: gobby runs codewiki/gwiki against the same local gemma successfully. Combined with the Rust gate (the other CodeWiki run), CodeWiki documented gobby-cli (Rust) = refused, and gobby-Python = timed out → **no CodeWiki documentation obtained under fair local-model conditions.**

## Patched re-run (1800s timeout — to obtain a quality sample) — Josh approved
One labeled non-shipping patch: raised the OpenAI request timeout 600s→1800s (timeout only; no quality settings touched). Value derived from measured gemma throughput (107.5 tok/s → full 32K output ≈305s; 1800s makes the timeout non-binding, as on a fast cloud model).

**Outcome: PARTIAL — clustering + 1 of 6 module overviews, then doc-gen HUNG (~50 min on the `spawn` sub-module, killed).**
- **Clustering now succeeds and is genuinely good:** 6 *semantic concept-modules* with descriptive cross-file names — `agent_execution`, `isolation_and_sandboxing`, `monitoring_and_detection`, `recovery_and_cleanup`, `terminal_interface`, `agent_context_and_utilities` (`module_tree.json`). This is a real CodeWiki strength vs gobby's 3 *directory*-based modules → adoption candidate C5.
- **Module-overview doc quality (the one that completed, `agent_execution.md`): good.** Narrative overview + a **mermaid architecture diagram** (graph TD w/ subgraphs; minor: a stray unmatched `end`) + numbered workflow + links to sub-module docs (`runner.md`/`session.md`/`spawn.md`).
- **Grounding gap:** the module overview references **symbol NAMES only (`AgentRunner`, `prepare_terminal_spawn`), no `file:line`.** File-level grounding (which the 826KB dependency graph clearly has — `file::symbol` + line ranges) would live in the leaf/sub-module docs — which **never generated** (hung).
- **Why it hung:** gemma (reasoning model) went into continuous-streaming runaway on the `spawn` sub-module prompt; httpx's per-read timeout never trips while tokens keep streaming, so the 1800s cap didn't fire. ~50 min, no progress, killed.

## Final CodeWiki verdict (Track A2)
**Three attempts, zero complete wiki under fair local-model conditions:** Rust → validator refuses `.rs`; Python/600s → clustering-collapse + timeout; Python/1800s → good clustering + 1 module overview, then generation hang. **DeepWiki ran the identical local gemma to a full 10-page wiki**, so this is a CodeWiki *robustness* gap, not merely a slow model. Assessable strengths captured (semantic clustering, narrative+diagram overviews); its file:line grounding could not be observed in practice (leaf docs never completed). Net for the matrix: CodeWiki is **impractical with local models** and, even when coaxed, did not finish — gobby produced a complete, file:line-grounded wiki on the same scope.
