 Wiki parity+ program: fix LLM lanes, DeepWiki-grade codewiki, general-wiki re-verification, competitor bake-off

 Execution model (Josh's call, 2026-06-12): a gpt-5.5 xhigh worker (Codex) executes all work packages in interactive
 /goal sessions; Claude (this session) authors the spec, audits each gate, and verifies closed tasks. Bake-off set: Graphify,
 DeepWiki-Open, llm-wiki (no OpenDeepWiki), running against the local LM Studio endpoint (http://localhost:1234/v1, model
 google/gemma-4-26b-a4b-qat).

 Handoff step (Claude, first action after approval): write the merged execution spec — this plan plus the binding
 clarifications below — to /Users/josh/Projects/gobby-cli/.gobby/plans/wiki-fix.md, the path the worker's goal addendum
 expects.

 Binding clarifications (from Codex's goal addendum, accepted):
 - /Users/josh/Projects/gobby = Python daemon repo, WP1 only. /Users/josh/Projects/gobby-cli = Rust CLI/wiki repo,
 WP2–WP5.
 - Task refs are project-qualified: #720 means gobby-cli #720 (gwiki narration regression) and is closed/verified only
 via project_id=gobby-cli; a daemon-project #720 is irrelevant.
 - fable-repo-analysis.md = /Users/josh/Projects/gobby-cli/fable-repo-analysis.md.
 - .env files used by the work: mode 600; never print secret values.
 - Worker creates/claims separate MCP tasks in the correct project before edits (daemon task for WP1; gobby-cli task for
 WP2–WP5 unless an open task is explicitly reused). Explicit paths, explicit git staging, no push (Josh pushes). Focused
 validations only — no full pytest; prefix pytest with GOBBY_TEST_PROTECT=1.
 - Claude's gate checks are verification/cleanup, not blockers on the worker's ownership of the full task.
 - Decomposition assumption corrected: no repo-level file-size decomposition rule exists in either AGENTS.md; in-scope
 files run 41–955 lines (largest: adapters/codex_impl/client.py 955, ai/text_generation.py 815, adapters/acp_client.py
 815, codewiki/text.rs 784). Do NOT speculatively decompose; if a worker-side gate forces a split, do the minimal
 mechanical extraction inside the same task and commit.

 API key placement: create ~/Projects/wiki-bakeoff/.env (chmod 600, never committed) — Josh pastes the LM Studio API
 token there as OPENAI_API_KEY=<token> plus OPENAI_BASE_URL=http://localhost:1234/v1. Each competitor tool's config/.env
 sources or copies from it during WP4 setup. (LM Studio on this machine requires a token — same one the daemon's
 ai.embeddings secret wraps; do not echo it into logs or chat.)

 Context (verified this session)

 1. gwiki ask slowness — root cause found. The daemon runs every "one-shot" text generation as a full agent-harness turn.
 Codex adapter (/Users/josh/Projects/gobby/src/gobby/ai/text_generation.py:487-512) spawns a fresh Codex app-server per
 request and runs a default-effort agentic turn — never passes effort/sandbox/approvalPolicy though the client supports
 them (adapters/codex_impl/client.py:270,456). Gemini/ACP adapter same (text_generation.py:435-446). No per-candidate
 timeout (text_generation.py:176-223), so stalls block to the Rust client's 300s limit instead of falling back. Measured:
 codex/gpt-5.4-mini 20.2s vs claude/haiku 5.8s for a "Reply OK" probe; real calls 40–147s; daemon JSON features
 (memory.kg.*, tasks.validation) at 100% failure for 14h (agent prose/empty → json.loads fails at char 0). The haiku lane
 is the template (src/gobby/llm/claude.py:430-495: max_turns=1, tools=[], allowed_tools=[], mcp_servers={}).
 2. Codewiki output is thin by design, not by failure. Verified in .gobby/wiki/code/: repo.md/_architecture.md are
 directory listings with one-sentence blurbs; gwiki module page = 34 body lines under 2,956 frontmatter lines; 46/525
 pages have mermaid; 196 "has no indexed API symbols" stubs; citations attach alphabetically-first files
 (elixir_dependency_roots.json:2 for platform claims). Generator causes (crates/gcode/src/commands/codewiki/): prompts
 demand "a short overview"/"one sentence" (prompts.rs:11-120), 600-char child excerpts (prompts.rs:177), citation
 fallback picks first-per-file from a BTreeSet (text.rs:277-298), no frontmatter cap (text.rs:486-533), architecture
 diagram can't render because top-level subsystems are crates/docs/scripts with no cross-edges. The thin prompts were
 rational at 40–147s/call — WP1 makes rich generation affordable.
 3. Parity+ scope = codewiki AND general wiki (docs/guides raw ingestion → valid AI outputs), per the original Phase 3
 "by the book" verification in fable-repo-analysis.md:371-559 (init → setup → ingest-url/file/collect → hybrid search →
 codewiki → ask/research/compile), now plus live competitor outputs for side-by-side comparison.

 Standing decisions: tune codex/gemini to the haiku one-shot contract — no candidate reorder, gpt-5.4-mini stays primary.
 Future direction (not in scope): possibly drop LLM ask/research from gwiki in favor of active-agent interpretation over
 extractive output.

 ---
 WP1 — Daemon: bounded one-shot LLM lanes

 Repo: /Users/josh/Projects/gobby (Python). STATUS: implemented by Claude in-session 2026-06-12 (daemon task #17061),
 per Josh's revised call — the Codex worker's scope is WP2–WP5. Commits [gobby-#NNNNN] fix: …; explicit-path staging;
 no push (Josh pushes).

 1. Codex adapter one-shot contract (CodexAppServerTextGenerateAdapter.generate): start_thread(...,
 approval_policy="never", sandbox="readOnly"); append a direct-answer/no-tools/no-narration directive to
 context_prefix. Reasoning effort stays auto — no effort override (Josh: "that's what we did for haiku"); the latency
 win comes from removing the agentic turn, not clamping reasoning. approvalPolicy/sandbox at thread/start is the
 tool-restriction surface the app-server protocol offers (codex_impl/client_api.py).
 2. Gemini ACP adapter, same contract (ACPTextGenerateAdapter.generate): deny-all pre_tool_callback passed through
 ACPClient.send (ACP permission requests answer {"outcome": "cancelled"} instead of auto-approve; mcpServers already
 []); same directive via _compose_prompt. Applies to all ACP lanes (gemini/grok/qwen); managed web-chat sessions
 (pre_tool_callback=None) keep auto-approve.
 3. Per-candidate timeout in _try_generate_result_candidates AND _try_generate_json_candidates: asyncio.wait_for(...,
 timeout≈60s) (single config knob); on timeout, log and continue to next candidate.
 4. Unit tests (tests/ai/): adapters pass the one-shot options (fake client factories); slow candidate times out into
 fallback; timeout knob plumbs.
 5. Restart daemon (current PID 68265 predates any change).

 Acceptance criteria (audit gate 1 — Claude): focused uv run pytest + lint green; tiny codex probe drops from 20.2s
 baseline (target single digits); real gwiki ask --llm --require-ai < 10s; ~/.gobby/logs/gobby.log shows
 tasks.validation/memory.kg.* success=True. On a clean fast live ask: Claude closes #720 (commit 93d126f already
 done/deployed; set_variable memory_review_completed → close_task).

 WP2 — Codewiki content parity upgrade (DeepWiki-grade pages)

 Repo: gobby-cli, crates/gcode/src/commands/codewiki/. New gobby-cli task (not #720). Gates: cargo fmt --check, clippy -D
 warnings, cargo nextest run -p gobby-code.

 1. Prompts → documentation briefs (prompts.rs): Aggregate-tier pages get structured multi-paragraph briefs (module:
 responsibilities, key flows, how submodules collaborate; repo: what the system is, how pieces fit, where to start;
 architecture: layered subsystem-interaction narrative). Symbol purpose stays one sentence.
 CHILD_SUMMARY_EXCERPT_MAX_CHARS 600 → ~2000. Keep no-fences + grounding contract.
 2. Real architecture page (build_parts/architecture.rs, render.rs): subsystem decomposition starts at meaningful units
 (the six crates, not crates/docs/scripts) so the cross-subsystem mermaid can render; enumerate top 1–2 module levels
 only; add the layered narrative section.
 3. Front page (render.rs): system narrative + crate-level dependency mermaid; drop "has no indexed API symbols" filler
 from Files lists.
 4. Non-code file stubs (build_parts/file.rs, render.rs:716): markdown/config files are content-indexed — generate
 Purpose from leading content chunks instead of the stub; structural fallback unchanged.
 5. Citation relevance (text.rs:277-298): replace alphabetical fallback with lexical-overlap scoring (sentence text vs
 span file path + symbol names); deprioritize asset/data files unless sole provenance; keep MAX_FALLBACK_CITATIONS=5.
 6. Frontmatter provenance cap (text.rs:486-533): cap per-page (e.g. top ~30 files by span count + provenance_truncated:
 N marker).
 7. Diagram coverage: with (2), verify dependency/call mermaid emits wherever bounded edges exist; keep hops/edges bounds
 — never fabricate edge-free diagrams.

 Unit tests for new prompt builders, citation scoring, provenance cap, architecture clustering.

 Acceptance criteria (audit gate 2 — Claude): gates green; diff review (no contract/schema breaks, no hub mutations);
 golden-style tests demonstrate the new page anatomy.

 WP3 — Regenerate + general-wiki re-verification

 1. cargo build --workspace --release; deploy gcode/gwiki to ~/.gobby/bin (atomic cp→mv + version sidecars).
 2. Regenerate codewiki (gcode codewiki, daemon routing) with WP1 lanes live; gwiki collect/index; zero degraded pages.
 3. General wiki "by the book" re-run (mirror fable-repo-analysis.md Phase 3): init/setup/status sanity on the existing
 vault, then fresh ingest-url (Wikipedia page), ingest-file (README.md or a docs/guides page), collect inbox drop, plus
 one multimodal source if convenient; index with zero degradations; hybrid search (bm25+semantic+graph sources all
 present); AI-derived outputs from raw ingestion: compile and a bounded research run produce valid, cited, non-narration
 output.
 4. Live ask set: 4–5 questions incl. the #720 regression question ("What happens when the ghook inbox enqueue fails?") —
 clean text, route: daemon, citations, <10s each.

 Acceptance criteria (audit gate 3 — Claude): spot-check regenerated repo.md/_architecture.md/gwiki-module page against
 the DeepWiki bar (multi-section narrative, diagrams, relevant citations, no filler, bounded frontmatter); gwiki lint
 zero broken links; status/health/audit clean; ask transcripts pass.

 WP4 — Competitor bake-off (Graphify, DeepWiki-Open, llm-wiki)

 Workspace: ~/Projects/wiki-bakeoff/ with per-tool clones + the shared .env (see API key placement above). Run each tool
 via its Docker/docker-compose setup when the repo ships one (Josh's preference — avoid local installs of one-off
 software); fall back to local install only if no container path exists, and note it in SETUP.md. Containers reach LM
 Studio on the host via `OPENAI_BASE_URL=http://host.docker.internal:1234/v1` (macOS Docker); non-container runs use
 `http://localhost:1234/v1`. Model: google/gemma-4-26b-a4b-qat. Mount the gobby-cli repo read-only into the container and
 bind-mount outputs to `~/Projects/wiki-bakeoff/outputs/<tool>/`. Record per-tool config quirks (model-name mapping,
 embedding settings) in a SETUP.md per clone. After the bake-off, containers/images are disposable — list them in
 SETUP.md for cleanup.

 1. Run each tool against the gobby-cli repo; capture full generated output per tool under
 `~/Projects/wiki-bakeoff/outputs/<tool>/`.
 2. Record per-tool: wall-clock, pages generated, diagrams count, citation/grounding mechanism (if any), broken links,
 coverage (files/modules documented vs total).
 3. Fairness note in the artifact: competitors run gemma-4-26b locally while gobby runs its configured lanes — comparison
 is about structure/coverage/grounding, with model-quality caveat stated.

 Acceptance criteria (audit gate 4 — Claude): outputs exist and are complete runs (not partial failures); metrics table
 filled; no tool misconfigured in a way that sandbagging it (audit each tool's logs for errors).

 WP5 — Parity+ proof artifact

 Append a dated "Live parity+ proof (2026-06)" section to fable-repo-analysis.md: the 7-dimension matrix re-scored with
 fresh evidence — gobby codewiki/general-wiki outputs (WP3) side-by-side against the three competitor outputs (WP4), with
 file-path pointers into ~/Projects/wiki-bakeoff/outputs/ and command transcripts. Honest verdict per dimension,
 including any dimension where a competitor still wins.

 Acceptance criteria (final audit — Claude): every claim in the section traceable to an artifact on disk; close the
 daemon task, the codewiki task, and #720 (if not already closed at gate 1); Josh pushes both repos.

 Verification summary

 1. WP1: pytest/lint green; probe collapse (20.2s → single digits); JSON features recover; #720 live proof.
 2. WP2: fmt/clippy/nextest green; page-anatomy tests.
 3. WP3: regenerated wiki passes DeepWiki-bar spot-checks; lint/health/audit clean; ask <10s clean+cited.
 4. WP4: three complete competitor outputs + metrics.
 5. WP5: re-scored matrix with disk-traceable evidence.
