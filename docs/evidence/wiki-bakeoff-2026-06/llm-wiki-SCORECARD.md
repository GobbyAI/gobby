# llm-wiki (Pratiyush/llm-wiki) — bake-off scorecard

**Track B — sources → vault.** Comparator to gobby `gwiki`. Run: 2026-06-14.
Corpus: 8 native Claude Code session transcripts from **this gobby-cli wiki-fix program**
(~9 MB, `wiki-bakeoff/inputs/track-b-sessions/`), identical bytes to both tools. Synthesis
model: Ollama `gemma4:31b` (llm-wiki) / configured LM Studio `gemma-4-31b` (gwiki). Config:
endpoint + model only; tool defaults otherwise (see SETUP.md).

## Framing: this is a GAP ANALYSIS, not a parity contest
llm-wiki is a **vertical specialist** (AI coding-session transcripts → Karpathy vault). gwiki is
a **horizontal generalist** (multimodal research sources → vault). gwiki has **no
session-transcript feature at all** — so the question isn't "who's better," it's "what does the
specialist do that gobby doesn't, and is it worth building." Answer: yes → **adoption candidate C10**.

## Result: llm-wiki SUCCESS on its home turf; gwiki has no equivalent capability

| Axis (same 8 jsonl in) | gwiki 0.4.0 (generic) | llm-wiki v1.3.82 (specialist) |
|---|---|---|
| Format recognition | `kind: text`, **opaque** | per-CLI adapter (claude_code) parses internal schema |
| Per-session output | **525-byte stub** (frontmatter + "Original artifact stored under raw/assets/…") | full markdown: turn-by-turn conversation reconstruction |
| Content extracted | **0** (raw `.jsonl` copied to `raw/assets/`, never parsed) | every turn, tool call + truncated result, redacted |
| Session metadata | none | `model`, `user_messages`, `tool_calls`, `tools_used`, `tool_counts` histogram, `token_totals`, `turn_count`, `hour_buckets`, `duration_seconds`, `is_subagent`, `gitBranch`, `permissionMode` |
| Secret redaction | n/a (not parsed) | `/Users/josh`→`/Users/USER` (41×), `sk-` masked, email masked |
| LLM synthesis | `compile` **errored** — `read research checkpoint failed … research-session.json` (**reproduces #733**) | 8/8 source pages on gemma4:31b: Summary / Key Claims / Key Quotes / Connections, accurate, `[[wikilinks]]`, auto-tags |
| Knowledge graph | — | builtin wikilink graph (`graph.json`) |
| AI-consumable exports | — | `llms.txt` (spec), `llms-full.txt`, `graph.jsonld` (schema.org), `sitemap.xml`, `rss.xml`, `robots.txt`, `ai-readme.md`, `manifest.json` (SHA-256) |
| Quality governance | lint (broken links / hygiene) | 16 lint rules + confidence scoring + 5-state lifecycle; lint here flagged 50 dangling links + 8 orphans |

## Where llm-wiki is strong (model-independent design)
1. **Native per-CLI session adapters** (Claude Code / Codex / Cursor / Gemini) — it understands
   the transcript schema and reconstructs the conversation; gwiki sees an opaque blob.
2. **Deterministic session-metadata extraction** — model, per-tool call histogram, token totals
   (input/cache/output), duration, hourly activity, subagent flag. None of this needs an LLM.
3. **Built-in secret redaction** during conversion (username, `sk-` keys, emails).
4. **Synthesis robust to local models.** Its synth is a **single bounded summarization call per
   page** — no fragile multi-tool function-calling loop — so gemma4:31b produced clean, accurate
   pages **where the same gemma class sank CodeWiki and Graphify** (whose agent loops needed
   reliable tool-calling). Architecture choice that pays off on weak local models.
5. **Agent-consumable outputs** — `llms.txt`/`llms-full.txt`/`graph.jsonld` make the vault
   queryable by *other* agents, not just humans.
6. **Governance** — confidence (4-factor + decay), lifecycle states, 16 lint rules.

## Where gobby/gwiki is strong (the honest other direction)
This is **not** "gwiki is worse." Different domains:
- **gwiki is the general multimodal research engine** — URLs, PDFs, MediaWiki, git, audio/image/
  video. **llm-wiki can ingest none of these** (transcripts only).
- gwiki has a **deeper retrieval/grounding stack**: hybrid BM25 + semantic (Qdrant) + graph
  (FalkorDB) via RRF, provenance/citations/credibility tracking, hit-tied code citations.
  llm-wiki's graph is a static wikilink graph; its search is a client-side fuzzy index.
- Each tool is a specialist; neither covers the other's domain.

## Adoption candidate → C10
**Session-transcript ingestion + synthesis pipeline for gwiki/daemon.** Concrete sub-features
llm-wiki demonstrates: (a) per-CLI transcript adapters; (b) deterministic session-metadata
extraction (model/tools/tokens/duration/activity); (c) secret redaction on ingest; (d)
single-shot bounded per-session synthesis (robust to local models); (e) `[[wikilink]]` Karpathy
structure; (f) `llms.txt`/JSON-LD agent-consumable exports; (g) confidence + lifecycle governance.
**gobby already persists the raw material** — `~/.gobby/session_transcripts/*.jsonl.gz` (3,750
sessions, 1.3 GB, multi-CLI). The missing layer is parse → synthesize → vault, not capture.

## Honesty notes (about the bake-off)
- **Gap analysis, not parity** — gwiki has no comparable feature by design; stated up front so
  the verdict isn't mis-read as "gwiki failed."
- **Model delta**: llm-wiki ran on Ollama `gemma4:31b`; the battery baseline was LM Studio
  `gemma-4-26b-a4b-qat`. Both gemma-4. The axes that decided this (parsing, redaction, metadata,
  extraction) are model-independent; only the synth-prose quality depends on the model, and even
  there gemma4:31b was solid.
- **llm-wiki entity/concept pages not generated** — the Ollama backend synthesizes *source*
  pages only; the full entity/concept graph is the `agent_delegate` path (an external coding
  agent), not run here → 50 dangling `[[wikilinks]]` + 8 orphan pages (lint caught them). A
  fuller run would close them.
- **gwiki compile** is additionally blocked on a fresh vault by **#733** (reproduced here) — but
  even fixed, there were **no derived notes** to compile (ingest extracted none), so the gap holds.
- Config-ergonomics gotchas (split config file, live-session guard) logged in SETUP.md.

## Net verdict (Track B)
**llm-wiki decisively owns session-transcript → vault** — native per-CLI parsing, rich
deterministic metadata, redaction, local-model-robust synthesis, agent-consumable exports, and
quality governance — a capability **gobby has zero equivalent for** today. **gwiki owns general
multimodal research → vault**, which llm-wiki can't touch. The concrete, high-value, low-cost gap
for gobby is **C10**: a session-transcript pipeline, made cheap because gobby **already archives
every session transcript** — it just never synthesizes them.

## Artifacts
`outputs/llm-wiki/`: `SCORECARD.md`, `SETUP.md`, `synth.log`, `_probe_ollama.sh`,
`run/raw/sessions/` (8 conversions), `run/wiki/sources/gobby-cli/` (8 synth pages),
`run/graph/`, `run/site-exports/` (8 AI-consumable exports).
`outputs/gwiki-track-b/`: the 8 opaque `raw/src-*.md` stubs + vault index (gwiki's output).
