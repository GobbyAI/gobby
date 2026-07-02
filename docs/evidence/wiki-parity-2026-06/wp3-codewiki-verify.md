# WP3 codewiki regen — verification (2026-06-14)

Binaries: gcode 1.1.0 / gwiki 0.4.0 (release, HEAD `15972ef`). Routing: `--ai daemon --ai-aggregate-profile feature_high --ai-depth files`. Vault wiped + re-init'd; per-symbol pages reused from `_meta/codewiki.json` (files-mode cache), aggregate + degraded + changed-source pages regenerated fresh.

Regen: exit 0 — wrote **372 file docs, 67 module docs, repo.md** (`wp3-codewiki-regen.txt`).

## Zero-degradation criteria (WP3 step 2)

| Check | Result |
|---|---|
| `exposes N symbols` structural fallback | **0** |
| `has no indexed API symbols` | **0** |
| content-only `*.md.md` doc pages (#727 exclusion) | **0** (pruned) |
| AI-lane degraded pages (the 134) | **0 — all healed** |
| `degraded_sources` non-empty frontmatter | **1 page** (ownership, see below) |

The 134 broken-AI-lane structural-fallback stubs are fully healed. The 11 raw `degraded_sources` grep hits are false positives: aggregate pages carry an empty `degraded_sources: []` field, and code-doc pages reference a symbol literally named `model_degraded_sources`.

## Honest-degraded exception: `code/_ownership.md`

`degraded: true`, sources `codeowners_unavailable`, `git_blame_errors`, `git_blame_partial`.

- **The repo has no CODEOWNERS file** (verified: none under `/`, `.github/`, `docs/`). `codeowners_unavailable` therefore permanently forces `degraded: true` regardless of blame — by design (`ownership.rs::degraded_sources`).
- `git_blame_partial`/`errors` come from the bounded blame budget (`blame_file_cap`/`blame_timeout`), cached incrementally in `_meta/ownership.json`.
- This is **honest reporting**, not a pipeline failure: ownership is genuinely partial (derived from bounded blame, no CODEOWNERS). Fabricating a CODEOWNERS to clear the flag would be dishonest. Kept as-is.

## Aggregate-page quality (DeepWiki bar)

- `_architecture.md`: layered multi-section narrative (foundation/gcore → domain tools → interaction layer), grounded `[path:lines]` citations per paragraph, full Subsystems enumeration with per-crate responsibility+collaboration prose.
- `repo.md`: Overview + Modules + References; frontmatter capped (`provenance_truncated: 342`, 30 files).
- **55 mermaid diagrams** emitted across module pages where bounded call/import edges exist.
- No top-level cross-subsystem mermaid: correct — the crates are independent binaries (only shared dep is gcore lib); the architecture narrative states the supplied edges show no cross-subsystem dependencies, so per WP2 "never fabricate edge-free diagrams" none is emitted.
- Frontmatter provenance cap applied per WP2 (top-30 files + `provenance_truncated`).

Note (minor): `docs/evidence/` appears as an architecture "subsystem" because the evidence dir is indexed top-level content. Accurate but slightly circular; harmless.

## Link hygiene (`gwiki lint`)

Initial lint surfaced **10 broken links**:
- **8 markdown** absolute-path links to `/Users/josh/Projects/gobby/...` (wrong repo) — embedded in cached pre-#726 file summaries that were reused, then assembled verbatim into module pages. #726 (`sanitize_model_markdown_links`) sanitizes at generation, not at render-time assembly of cached summaries.
- **2 wikilink** false-positives — symbol-summary prose quoting link-producing code: `wiki_link`'s purpose quotes `[[relative_path|title]]`; a compile test's purpose quotes `[[knowledge/topics/exact|Exact]]`. Sourced from PostgreSQL symbol summaries; honest descriptions, not navigation.

**Fix:** deleted the 4 affected file pages + 2 module pages and re-ran codewiki `--scope crates/gcode/src/index/import_resolution/parser crates/gcode/src/index/parser/calls` (scoped run preserves aggregates via #715). File summaries regenerated through `ground_text`+#726 → absolute-path links stripped; module pages rebuilt from clean summaries. Result: **0 real broken links** (`wp3-codewiki-scoped.txt`, `wp3-codewiki-scoped2.txt`).

The 2 wikilink false-positives remain (DB-summary-driven, not regen-fixable). Filed **gobby-cli #732** (codewiki should escape inline `[[...]]`/`[](...)` syntax in symbol-summary prose). They are honest renderings of code descriptions, not dangling navigation.

The remaining lint findings (`missing_backlink`, `orphan_pages` for the aggregate `_*` pages, `missing_frontmatter` on the two `INDEX.md`) are hygiene warnings, not broken links.
