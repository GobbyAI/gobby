# llm-wiki (Pratiyush/llm-wiki) — bake-off setup / repro

**Track B — sources → vault.** Comparator to gobby `gwiki`. Run: 2026-06-14.
Tool: [Pratiyush/llm-wiki](https://github.com/Pratiyush/llm-wiki) (Andrej Karpathy "LLM Wiki" pattern), v1.3.82. Python stdlib + `markdown`, no DB/servers.

## What llm-wiki is (scoping)
A **vertical specialist**: it ingests **AI coding-session transcripts** (Claude Code,
Codex CLI, Cursor, Gemini CLI `.jsonl`) and synthesizes a Karpathy-style vault
(`sources/ entities/ concepts/ syntheses/ comparisons/ questions/` with `[[wikilinks]]`,
confidence scoring, 5-state lifecycle, 16 lint rules, static site + AI-consumable exports).
It does **not** ingest arbitrary URLs/PDFs — so the fair shared corpus with gwiki had to be
a *transcript* corpus, and Track B is a **gap analysis, not a parity contest** (gwiki has no
comparable session-transcript feature; see SCORECARD.md).

## Corpus (frozen, identical bytes to both tools)
8 native Claude Code session transcripts from **this gobby-cli wiki-fix program**, frozen at
`wiki-bakeoff/inputs/track-b-sessions/*.jsonl` (~9 MB, 8 sessions). These are llm-wiki's
flagship adapter format.

- Source of truth: `~/.claude/projects/-Users-josh-Projects-gobby-cli/<uuid>.jsonl` (native).
- Note: gobby **also** archives every attached-project transcript at
  `~/.gobby/session_transcripts/<uuid>.jsonl.gz` (3,750 files, 1.3 GB) — but that archive is a
  **gobby envelope** (`{payload, timestamp, type}`) wrapping each CLI's native record, and is
  **multi-CLI** (most archived sessions here are Codex `codex-tui`, not Claude). Used the native
  unwrapped `~/.claude` copies for clean adapter parsing. The wrapped archive is itself the
  adoption-relevant raw material (a real gwiki/daemon pipeline would read it; see C10).

## Run isolation
- `git -C wiki-bakeoff/llm-wiki archive HEAD | tar -x -C /tmp/llmwiki-run` — clean tree, no
  `.git`/gitignored `raw/`.
- Moved the committed demo `wiki/` and `site/` aside (`*_demo_orig`) for a clean-vault run.
- **Input scoping via fake `$HOME`**: `claude_code.py` reads `Path.home()/.claude/projects`,
  so input was scoped by `HOME=/tmp/tb-home`, with only the 8 files under
  `/tmp/tb-home/.claude/projects/-Users-josh-Projects-gobby-cli/`. uv's resource dirs
  (`~/.local/share/uv`, `~/.cache/uv`) were symlinked into the fake home so uv still resolved
  Python/cache normally. File mtimes backdated so the live-session(60 min) guard never skipped them.

## Invocation (require-uv hook blocks bare python)
```
cd /tmp/llmwiki-run
env HOME=/tmp/tb-home uv run --no-project --with 'markdown>=3.9' --python 3.13 \
    python -m llmwiki <command>
```
Pipeline: `init` → `sync --include-current --force` → `synthesize --force` → `all` (build → graph → export → lint).

## Fair config (endpoint + model only; tool defaults otherwise)
- **Synthesis backend = Ollama**, model **`gemma4:31b`** @ `http://127.0.0.1:11434` (Ollama
  already had `gemma4:31b` pulled — no re-download). Same **gemma-4 family** as the battery's
  LM Studio baseline. Model delta noted: battery baseline was `google/gemma-4-26b-a4b-qat` (26B
  A4B QAT) vs this 31B dense — the comparison axes that matter here (parsing, redaction,
  metadata, structure) are model-independent.
- gwiki side configured for LM Studio `google/gemma-4-31b` for Track-B parity — but gwiki
  extracted no content from the jsonl, so the synth model was never exercised (see SCORECARD).

## Config-ergonomics gotchas (noted, not quality issues)
1. **Split config sources**: `sync`/`convert` read `./config.json` (preferred over the shipped
   `examples/sessions_config.json`), but `cmd_synthesize` **hardcodes**
   `REPO_ROOT/examples/sessions_config.json` and ignores `./config.json`. The `synthesis` block
   (backend/model) had to be set in `examples/sessions_config.json`; `synthesize --check` then
   reported `OllamaSynthesizer / Available: True`.
2. **Live-session guard**: freshly-copied corpus files look "live" (<60 min mtime) and get
   skipped — set `filters.live_session_minutes: 0`, pass `--include-current`, and backdate mtimes.

## Backends discovered
- `llmwiki/synth/base.py`: `DummySynthesizer` (default, offline) + ABC.
- `llmwiki/synth/ollama.py`: `OllamaSynthesizer` — stdlib `urllib` POST to Ollama `/api/generate`,
  `stream:false`, retries. Source-page synthesis is **one LLM call per session** (Summary / Key
  Claims / Key Quotes / Connections). The full entity/concept graph is the `agent_delegate` path
  (driven by an external coding agent) — not run here.
