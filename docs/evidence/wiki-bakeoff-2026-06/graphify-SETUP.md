# Graphify (graphifyy 0.8.39) — bake-off setup

**Track A1 — code intelligence / graph query for agents.** Comparator to gobby `gcode`
(`search`/`search-symbol`/`callers`/`usages`/`blast-radius` + FalkorDB code graph), NOT to
`gcode codewiki`. Graphify's markdown/wiki export is a weak A2-adjacent side output, noted
but not the main comparison.

## Install (isolated, no env pollution)
```
uv tool install --upgrade "graphifyy[openai]"     # → graphify 0.8.39 on PATH
```
Used `uv tool` (not `pip`) per Graphify's own README guidance — isolates the package so the
runtime Python resolver in `graphify-out/.graphify_python` matches the install env.

## Fair config — endpoint + key + model ONLY (Josh's fairness rule)
Graphify reads custom LLM providers from `~/.graphify/providers.json` (global path = trusted,
no opt-in gate). One provider added, pointing at the SAME local model every tool in this
bake-off gets:
```json
{
  "lmstudio": {
    "base_url": "http://localhost:1234/v1",
    "default_model": "google/gemma-4-26b-a4b-qat",
    "env_key": "OPENAI_API_KEY",
    "model_env_key": "GRAPHIFY_LMSTUDIO_MODEL"
  }
}
```
- **Only** base_url + model id + which env var holds the key. **No** token/temperature/concurrency/
  chunking tuning — Graphify's own defaults (token-budget 60000, max-concurrency 4, api-timeout 600s,
  auto-sized everything else) are left untouched.
- The API key is **not** inlined in the config; `env_key` makes Graphify read `OPENAI_API_KEY`
  from the environment at runtime (sourced from `wiki-bakeoff/.env`, never printed/committed).
- `--backend lmstudio` is passed **explicitly**. Auto-detect would wrongly pick the real `openai`
  backend (api.openai.com) because `OPENAI_API_KEY` is set — but that key is an LM Studio token,
  not a real OpenAI key. Explicit backend selection = part of "configure the endpoint," not tuning.

## Corpus scope — `crates/` only (the Rust code)
Fed Graphify a clean `git archive HEAD` (`ea6a26c`) of gobby-cli → `inputs/gobby-cli/`
(1030 files, 19M, no 209GB `target/`), then scoped the build to `inputs/gobby-cli/crates`
(433 code files incl. 421 `.rs` + 12 crate READMEs/SKILL docs).

Rationale: A1 is a **code-graph** comparison against gcode's symbol/call graph, so Graphify
should see the same Rust source gcode indexes. Excluded the repo's `.gobby/wiki/` markdown
(hundreds of files) deliberately — that tree is **gobby's own generated wiki output**; feeding
it back into a competitor would be circular and would trigger needless LLM doc-extraction.
This scoping favors Graphify (clean code corpus, no noise), so it's fair.

## Build command
```
OPENAI_API_KEY=… graphify extract inputs/gobby-cli/crates \
  --backend lmstudio --cargo --out outputs/graphify
```
- AST extraction (433 files) runs **fully local** via tree-sitter (`tree-sitter-rust` bundled) —
  no LLM, no API key needed for the code graph itself.
- `--cargo` = Graphify's own Rust feature: extract crate→crate deps from `Cargo.toml`. A
  capability flag, not tuning; included to give Graphify its best Rust-aware graph.
- LLM (LM Studio gemma) used only for: semantic extraction of the 12 doc files + Leiden
  community **labeling** (short name per community).

## A1 query battery (run identically against gcode)
| # | Agent retrieval task | Graphify | gcode counterpart |
|---|---|---|---|
| Q1 | Concept: hybrid search RRF merge | `query "how does hybrid search merge BM25 semantic and graph results"` | `gcode search "reciprocal rank fusion merge ranked results"` |
| Q2 | Impact/blast-radius of a core type | `affected "Symbol"` | `gcode blast-radius Symbol` / `gcode usages <id>` |
| Q3 | Shortest path between two concepts | `path "indexer" "FalkorDB"` | gcode callers/usages chain |
| Q4 | Explain a node + neighbors | `explain "<rrf node>"` | `gcode outline` + `gcode symbol <id>` |
| Q5 | Who calls a function | `affected "<fn>"` (reverse) | `gcode callers <id>` |

Scored on: groundedness (does the answer cite real file:line / symbols?), navigability (can an
agent act on it?), correctness, honesty/audit trail. Captured to `SCORECARD.md`.
