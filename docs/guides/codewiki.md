# gcode Codewiki Guide

`gcode codewiki` generates vault-ready Markdown documentation from the code
index. It is the producer in the code-to-wiki workflow: gcode owns AST symbols,
source spans, and the FalkorDB graph; gwiki later indexes the generated Markdown
as normal vault documents.

## Run

Generate docs into the default `codewiki/` directory:

```bash
gcode codewiki
```

Write directly into a gwiki vault code-doc subtree:

```bash
gcode codewiki --out /path/to/vault
gwiki --project /path/to/project index
```

Limit generation to one or more indexed paths:

```bash
gcode codewiki --scope crates/gcode src
```

Tune prose verbosity and audience register independently of depth, and
incrementally regenerate only what changed since a git ref:

```bash
gcode codewiki --ai-prose-depth deep --ai-register newcomer
gcode codewiki --since HEAD~1
```

Repair stale `[file:line]` citations in an existing wiki without any AI calls:

```bash
gcode codewiki --repair-citations
```

Remove generated CodeWiki Markdown and metadata before a clean rebuild:

```bash
gcode codewiki --purge --out /path/to/vault --force
```

## Output Tree

The generated tree is hierarchical. It has three layers: a **structural layer**
(per-file and per-module reference docs), a **deep-wiki narrative layer**
(long-form source-grounded prose pages), and a **curated navigation layer**
(promoted entry points that route a reader through the other two).

Structural reference pages:

- `code/repo.md` is the repository overview.
- `code/modules/<module>.md` documents directory/module groups.
- `code/files/<path>.md` documents individual source files.

Deep-wiki narrative and curated navigation:

- `code/concepts/index.md` is the curated table of contents — a promoted
  navigation hub that names user-facing concepts and links into the
  structural reference pages instead of duplicating source detail.
- `code/concepts/<slug>.md` are concept pages that group related modules
  under a user-facing topic.
- `code/narrative/<slug>.md` are deep-wiki narrative pages: long-form,
  source-grounded prose chapters ordered as a learning path. The guided tour
  ("Start here") on the concepts index links chapter one onward — this is the
  "explain my codebase" entry point.

Deterministic projection pages (built without LLM calls, never degraded):

- `code/_architecture.md` is the architecture overview page; when a workspace
  SystemModel is available it seeds the page's model-derived Mermaid diagrams.
- `code/infrastructure.md` is the infra-stack page derived from the SystemModel
  (service boundaries, the adapter modules that pull each one in, and its
  degradation behavior).
- `code/features.md` is the contract-derived feature catalog (per-binary
  command/flag surface resolved from the pinned CLI contract JSONs).
- `code/deprecations.md` is the deprecation aggregate from a bounded source
  scan for `#[deprecated(...)]` attributes and `DEPRECATED` doc-comment
  markers; matching symbols also carry a deprecation badge on their file page.
- `code/_onboarding.md` is the "Start Here" onboarding page.
- `code/_hotspots.md` reports change hotspots.
- `code/_changes.md` reports index changes since the previous run.
- `code/_ownership.md` reports code ownership (git blame derived).

The deterministic projection pages are reachable via a reference appendix on
`code/repo.md`, which links only the audit/analysis pages that were actually
emitted (so a run with no SystemModel or no deprecations leaves no dead links).

Metadata:

- `_meta/codewiki.json` records the docs written in the last run for
  incremental regeneration, including each page's render version. Render
  versions are tracked by page category so a format change to architecture
  pages does not invalidate unrelated file pages.
- `_meta/ownership.json` records ownership data for repeat runs.
- `_meta/truth_digest.json` is the truth digest — a compact, machine-readable
  set of grounded structural facts (see Truth Digest below). Written on full
  (unscoped) runs only.

Documents use `[[wikilink]]` references between repo, module, and file pages.
Each page includes `provenance:` frontmatter with file paths and line ranges
derived from indexed symbol spans.

## Generation Pipeline

`gcode codewiki` resolves the project context, loads indexed files and symbols
from PostgreSQL, filters out non-core files such as tests and generated/vendor
paths, and asks FalkorDB for call/import edges when graph configuration is
available.

The generator then builds docs bottom-up:

1. File docs are created from AST symbols, signatures, component IDs, and exact
   source spans.
2. Files are clustered into modules. With graph edges, connected call/import
   components are grouped under their common module. Without graph edges,
   directory and AST grouping are used.
3. Module docs aggregate direct files and child modules.
4. The curated navigation layer is planned over the module/file set: concept
   modules, a guided-tour learning path, and the deep-wiki narrative chapters
   (`code/concepts/`, `code/narrative/`). The plan links into the reference
   pages rather than duplicating source detail; without generation it falls
   back to a deterministic structural plan.
5. `repo.md` summarizes the top-level modules and root files and carries the
   reference appendix linking the deterministic projection pages.
6. Deterministic projection pages (architecture, infrastructure, feature
   catalog, deprecations) are emitted from the SystemModel, the pinned CLI
   contract, and the deprecation scan — no LLM calls.

When `ai.text_generate` is configured, gcode calls the shared generation route
for generated prose. Leaf pages use bounded one-shot generation. Aggregate
handbook pages can use the daemon-side agentic/tool-backed route, where the
model investigates through read-only gcode tools before answering and the page
frontmatter records lane, tool-call count, and turn count. Generated text is
citation-checked before write, normalized through the strict CodeWiki Markdown
normalizer, and — when a verifier profile is configured — passes a grounded
verification step (see Grounded Verification below). Empty or unavailable leaf
generation falls back to structural AST-only prose; failed agentic aggregate
generation is treated as a hard failure so unsupported handbook pages do not
silently degrade into skeleton text.

`--ai-depth` controls how deep AI prose generation reaches; gated tiers use
structural fallbacks:

- `sections` — architecture, module, and repo prose only (cheapest; a handful
  of LLM calls).
- `files` (default) — sections plus per-file summaries (one call per file).
- `symbols` — files plus per-symbol purposes. This issues one LLM call per
  indexed symbol and can take hours-to-days on large repos with local models;
  reserve it for small repos or scoped runs.

On the daemon route, aggregate docs (architecture, module, repo, curated
concept, and narrative prose) request a heavier daemon profile because they
synthesize many child summaries into one long grounded answer; file and symbol
docs stay on the daemon default profile. The aggregate writer chain defaults to
an opus-first profile; override it with `--ai-aggregate-profile <PROFILE>`.

### Prose Depth and Audience Register

Two dials shape generated prose independently of `--ai-depth` (which controls
*how far down the tree* AI prose reaches):

- `--ai-prose-depth brief|standard|deep` (default `standard`) tunes *verbosity*.
  `deep` raises the per-page token budget for longer, richer prose; `brief` is
  terser. This is orthogonal to `--ai-depth`.
- `--ai-register newcomer|maintainer|agent` selects the *audience voice*:
  `newcomer` is plain-language ELI5, `maintainer` foregrounds the "why" and
  trade-offs, and `agent` is terse build substrate. Omit it to keep the base
  voice. Grounding holds in every register — the dial changes phrasing, not
  which source facts are cited.

## SystemModel and Diagrams

### Deterministic SystemModel

On the CLI runtime, codewiki first builds a deterministic **SystemModel** of the
workspace: member crates, inter-crate dependency edges, service boundaries,
runtime modes, and per-crate `gobby-core` feature configurations. The model is
extracted from the project itself (not from the LLM), so it is a stable,
reproducible structural fact base.

The SystemModel feeds the deterministic projection pages:

- It seeds the architectural and conceptual-flow Mermaid diagrams on
  `code/_architecture.md`.
- It backs the `code/infrastructure.md` infra-stack page (service boundaries,
  the adapter modules that pull each one in, and degradation behavior).
- It produces a content digest used as the architecture/infrastructure
  invalidation key (see Trustworthy Incremental below).

Test and AI-off entry points pass no model, in which case the model-derived
diagram and infrastructure pages are omitted.

### Diagrams

`code/_architecture.md` carries SystemModel-derived diagrams: an architectural
dependency view of the workspace and conceptual-flow diagrams that trace the
documented data flow. Curated concept pages can also carry a bounded
conceptual-flow diagram derived from the documented module summaries.

When FalkorDB graph edges are available, module docs additionally include
bounded Mermaid diagrams:

- Dependency diagrams from import edges.
- Call sequence diagrams from call edges.

All diagrams are intentionally bounded around the documented module or
subsystem. They are not full-graph dumps. Graph-derived diagrams are omitted or
replaced with a `degraded: graph-unavailable` marker when FalkorDB is down (see
Graph-Degraded Output).

## Feature Catalog and Deprecations

`code/features.md` is a **contract-derived feature catalog**. It is a pure,
deterministic projection of the pinned per-binary CLI contract JSONs: for each
Gobby binary it lists every command, a short summary, the key flags, and the
resolved handler file and entry symbol. No LLM is involved, so the catalog
changes exactly when the CLI command/flag surface changes.

`code/deprecations.md` is the **deprecation aggregate**. A bounded source scan
inspects the lines above each indexed symbol for a `#[deprecated(...)]`
attribute or a `DEPRECATED` doc-comment marker (an attribute reason wins over a
doc-comment reason). Every matching symbol also gets a deprecation badge on its
file page, so the markers surface both in aggregate and in context. Like the
feature catalog, this page is deterministic and never degraded.

Both pages are gated on their input: the feature catalog is omitted when no
contract is resolved, and the deprecations page is omitted when no audit
context is supplied (for example AI-off or test entry points).

## Citations

Every generated claim is grounded against indexed source spans. Invalid
citations are stripped, and prose with no valid citation gets a bounded set of
representative fallback citations (at most a handful, spread across distinct
source files) rather than the full span list. `repo.md` resolves citations
through numbered `[N]` markers, and its References section lists only the
markers that actually appear in the page. This keeps prose tied to source
files and line ranges that gcode has actually indexed without letting broad
pages accumulate citation walls.

### Citation Repair

Citations drift when source files are re-indexed and line ranges shift.
`--repair-citations` is a repair-only mode: it re-anchors existing pages'
`[file:line]` citations against the current index and exits. It runs **no**
generation and makes **no** AI/LLM calls — it ignores generation flags
(`--ai`, `--scope`, `--ai-depth`, …) and honors only `--out`/`--format`. Use it
to keep an existing wiki's citations accurate after a reindex without paying to
regenerate prose.

## Grounded Verification

When a verifier profile is configured, generated prose passes a second,
grounded verification pass before write. The verifier splits a page into blocks
and checks each block against the same symbol/source evidence the page was
generated from. Crucially, verification is **non-destructive**: it does not
rewrite or strip prose. Blocks the verifier judges unsupported are recorded as a
frontmatter-only `verify_notes` audit trail; the prose itself is kept intact.
If the verifier is routed off, unavailable, or returns a malformed verdict, the
pass is skipped cleanly and the page is emitted undegraded.

`--ai-verify-profile <PROFILE>` (default `feature_mid`) selects the daemon
profile used for this pass. `--ai-verify-scope aggregates|all` controls which
pages run verification: `aggregates` is the default and verifies curated and
handbook pages only; `all` also verifies per-file leaves and is substantially
slower on large repos.

## Purge Flow

`gcode codewiki --purge --out <DIR> --force` is a destructive output cleanup
mode. It removes generated CodeWiki Markdown under `code/` and CodeWiki metadata
under `_meta/`, then exits. It does not touch raw wiki sources, authored
knowledge pages, PostgreSQL index rows, FalkorDB graph data, or Qdrant vectors.

`--purge` conflicts with generation, repair, scope, AI, and graph flags; only
`--out`, `--format`, and `--force` are relevant. Use it when you want to discard
generated files before a full rebuild:

```bash
gcode codewiki --purge --out /path/to/vault --force
gcode codewiki --out /path/to/vault
gwiki --project /path/to/project index
```

## Truth Digest

On full (unscoped) runs, codewiki writes `_meta/truth_digest.json` — a compact,
machine-readable summary of grounded structural facts derived from the
SystemModel. It carries a schema version and generation timestamp, a structural
repo summary, and the service stack: each service boundary's kind, the adapter
module that pulls it in, what pulls it in, a summary, and its degradation
behavior, plus a `key_paths` map and a `stack_authority` flag (`complete` vs
`partial`). The digest is bounded and deterministic, intended as a small,
trustworthy fact set agents can consume directly without parsing prose pages.
Scoped runs (`--scope`) do not write it.

## Trustworthy Incremental

`gcode codewiki` hashes source files referenced by each generated document and,
on later runs, preserves pages whose inputs are unchanged. Invalidation is
**per page type**, so each page rebuilds on exactly the input that can change
its content:

- **File docs** invalidate on their source-file hash. A changed file also
  invalidates its owning module doc and `repo.md`. File docs additionally
  record their cross-file neighbor set (callers / import targets), so an edit to
  a neighbor invalidates the page even when the file itself is unchanged.
- **Module docs** invalidate through their member files' hashes (member-set
  plus members hash), recorded as page provenance.
- **Architecture and infrastructure** invalidate on the SystemModel content
  digest, not their source-file set. A function-body edit leaves the model
  (crates, edges, service boundaries, runtime modes, features) unchanged and the
  page is kept; a `Cargo.toml` dependency or feature change shifts the digest
  and rebuilds it.
- **Feature catalog** invalidates on a digest of its rendered output, which
  changes exactly when the pinned CLI contract surface changes.
- **Deprecations** invalidate on a digest of the rendered deprecation set.

`_meta/codewiki.json` records the generated set for audit and repeat runs, along
with the AI mode of the last run (`off`, `sections`, `files`, or `symbols`);
changing the AI route or `--ai-depth` invalidates every doc, since prose content
depends on the generation mode rather than source hashes alone.

`--since <GIT_REF>` is the incremental driver. Instead of a full content-hash
scan, it uses `git diff --name-only <ref>` to select the changed file set and
regenerates only the pages whose sources or cross-file neighbors changed, plus
aggregate pages whose model digest changed. Out-of-scope pages and `_meta` are
preserved either way; omitting `--since` runs the full content-hash scan.

## Graph-Degraded Output

FalkorDB is required for graph-derived codewiki structure. In explicitly
degraded paths, or when a configured FalkorDB service is unreachable, codewiki
still produces valid docs:

- Clustering degrades to directory and AST-only grouping.
- Component IDs remain `file_path::name`.
- Non-core filtering still applies.
- Module, file, and repo prose is still emitted and citation-grounded.
- Graph-derived Mermaid diagrams are omitted or replaced with the
  `degraded: graph-unavailable` marker.
- The deterministic projection pages (architecture, infrastructure, feature
  catalog, deprecations) and the truth digest are built from the SystemModel,
  CLI contract, and source scan, so they are unaffected by graph availability.

This degraded mode is the expected standalone behavior for projects that have
PostgreSQL code-index data but no graph service.

## gcode to gwiki Ingest

The intended handoff is a file workflow, not a crate dependency:

1. Run `gcode codewiki --out <vault>`.
2. Run `gwiki --project <project-root> index`.
3. gwiki's vault index walk discovers `code/**/*.md`, preserves
`provenance:` frontmatter, extracts `[[wikilinks]]`, and indexes changed
   docs incrementally.

Because gcode writes vault-ready Markdown, gwiki only needs to classify and
index the generated subtree. Re-running codewiki rewrites only changed docs, and
the next gwiki index run picks up only those changed Markdown files.

_Last verified: 2026-07-01_
