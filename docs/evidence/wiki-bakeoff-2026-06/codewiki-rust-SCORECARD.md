# CodeWiki (FSoft-AI4Code, ACL 2026) — bake-off scorecard (target: gobby-cli)

**Track A (code → documentation).** Run: 2026-06-14. Benchmark commit `972dba9`. Model wired to local LM Studio `gemma-4-26b-a4b-qat` (connectivity validated). CodeWiki's own defaults for all generation settings (max_tokens 32768, per_module 36369, per_leaf 16000, max_depth 2).

## Result: **FAILED — Rust is unsupported (refuses before any LLM call)**
`codewiki generate` aborts at step [2/4] "Validating repository..." with `RepositoryError: No supported code files found`. CodeWiki's `SUPPORTED_EXTENSIONS` = `.py/.java/.js/.ts/.c/.cpp/.cs/.php/.kt`; the shipped image bundles tree-sitter grammars for python/c-sharp/cpp/c/java/javascript/typescript/kotlin/yaml — **no Rust grammar**. The documented `--include "*.rs"` flag does NOT help: repo-language validation runs *before* include patterns apply (verified empirically). gobby-cli is pure Rust → 0 eligible files → hard stop.

Setup itself fully succeeded (image `codewiki:0.0.1` built, config set, API validated, clean `git archive` copy fed read-only avoiding `target/`). The failure is structural, not a misconfiguration.

## Metrics
| Metric | Value |
|---|---|
| Pages produced | **0** |
| Diagrams | 0 |
| Citations | 0 |
| Coverage | **0 / 421 `.rs` files, 0 / 6 crates** |
| Hallucinations | none possible (no generation) |

## Human-reader axis: **N/A** (no output)
## Agent-consumer axis: **N/A** (no output)

## Honesty note (about the *bake-off*, not the tool)
The empty result is the honest, reportable outcome. The agent declined to patch CodeWiki's source to fake Rust support — that would fabricate a capability the product does not have.

## vs gobby + report finding
- **Gobby wins decisively on language coverage.** gobby's codewiki is AST-based (tree-sitter) but covers Rust as a **Tier-1** grammar (plus 12 other Tier-1 + Dart/Elixir Tier-2), so it documents all 6 crates. CodeWiki — a research-grade tool explicitly pitched at "large-scale codebases" — can't open the door on a Rust repo.
- **Architectural lesson for the report:** AST-gated documenters (CodeWiki, Graphify) are language-limited by their bundled grammars; LLM-file-reading documenters (DeepWiki, OpenDeepWiki) are language-agnostic but shallower/ungrounded. Gobby occupies the better quadrant: AST precision *with* broad Tier-1 language coverage. No adoption candidate from CodeWiki (it produced nothing), but the contrast is a Track-A proof point.
