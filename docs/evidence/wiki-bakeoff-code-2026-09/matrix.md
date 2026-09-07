# Code-wiki bakeoff matrix and casebook

This document is the executable evidence contract for the Graphify/gcode comparison in
[the approved bakeoff plan](../../../.gobby/plans/wiki-code-bakeoff.md). It defines the corpus,
questions, fixtures, measurements, and scoring before either comparator runs. It is not a result,
a recommendation, or a canonical wiki.

## Frozen inputs and authority

| Item | Frozen identity | Role |
| --- | --- | --- |
| Game Goblins baseline | `0216f1e33f05962d49467d95fe84609041c6dba8` | C0-C2 and the parent for C4-C9 |
| Game Goblins committed change | `8b24ac26699aac8b24254a647aa70b208287b492` | C3 only: name-prefix store-minimum behavior |
| Game Goblins checkout | `/Users/josh/Projects/game-goblins` | Read-only object source; never switch or modify this checkout |
| Graphify | [`c9f99018774e2e0380e9f65b3959944559a0d5f6`](https://github.com/Graphify-Labs/graphify/tree/c9f99018774e2e0380e9f65b3959944559a0d5f6), package `0.9.55` | Comparator A |
| gcode | `7394b97c1d88c82f685e788e798de2cfd728ad15`, `1.7.0`, CLI contract v8 | Comparator B |
| Isolated runtime root | `/Users/josh/Projects/wiki-bakeoff-code-2026-09` | All copies, indexes, logs, and generated output |
| External answer key | This file, Q01-Q14 and D/I/A labels below | Scoring only; never copied into a comparator corpus |

The frozen Game Goblins commits, not an index, define gold. Source code and tests establish
implemented behavior. Tracked documentation and plans establish stated policy or intent only when
their wording is consistent with code; otherwise the discrepancy is scored explicitly. Claims
about each comparator come from its frozen source, help, and machine contract. A command missing
from an older installed version is not evidence that the frozen version lacks it.

### Corpus construction

Run this only from the environment task after the pinned binaries and isolated runtime exist. The
commands read Git objects and do not alter the Game Goblins checkout.

```bash
export BAKEOFF_ROOT=/Users/josh/Projects/wiki-bakeoff-code-2026-09
export SOURCE_REPO=/Users/josh/Projects/game-goblins
export BASE_SHA=0216f1e33f05962d49467d95fe84609041c6dba8
export CHANGE_SHA=8b24ac26699aac8b24254a647aa70b208287b492
install -d "$BAKEOFF_ROOT/templates/baseline" "$BAKEOFF_ROOT/templates/change"
git -C "$SOURCE_REPO" cat-file -e "$BASE_SHA^{commit}"
git -C "$SOURCE_REPO" cat-file -e "$CHANGE_SHA^{commit}"
git -C "$SOURCE_REPO" status --porcelain=v1 -uall > "$BAKEOFF_ROOT/source-status.before"
git -C "$SOURCE_REPO" archive "$BASE_SHA" | tar -x -C "$BAKEOFF_ROOT/templates/baseline"
git -C "$SOURCE_REPO" archive "$CHANGE_SHA" | tar -x -C "$BAKEOFF_ROOT/templates/change"
rm -f "$BAKEOFF_ROOT/templates/baseline/.gobby/project.json"
rm -f "$BAKEOFF_ROOT/templates/baseline/.gobby/mcp/servers/lightspeed.yaml"
rm -f "$BAKEOFF_ROOT/templates/change/.gobby/project.json"
rm -f "$BAKEOFF_ROOT/templates/change/.gobby/mcp/servers/lightspeed.yaml"
git -C "$SOURCE_REPO" status --porcelain=v1 -uall > "$BAKEOFF_ROOT/source-status.after"
cmp "$BAKEOFF_ROOT/source-status.before" "$BAKEOFF_ROOT/source-status.after"
```

The archive includes the shared `src/game_goblins` platform, `Restocks`, `Buylist`, tests,
manifests, config examples, and relevant tracked documentation and plans. Remove only the two
listed operational Gobby identity/connection files. Git excludes the checkout's untracked ZIPs,
untracked plan, and `var/`; the harness must also reject real `.env` files, credentials, caches,
generated wiki output, comparator state, and answer-key material. `.env.example` files remain
eligible after a secret-pattern review because they document configuration without values.

Before making any case copy, write `corpus-manifest.json` containing the commit, exclusions, every
relative file path, byte size, mode, and SHA-256. Compute `input_tree_sha256` as SHA-256 over the
UTF-8 relative path, NUL, mode, NUL, byte length, NUL, and file bytes for each file in sorted path
order. Recompute it after every fixture edit. Copy no file from this evidence directory into
`$BAKEOFF_ROOT/corpora`.

Each tool gets an independent copy:

```bash
export CASE_ID=C1
export TOOL_ID=graphify
export CASE_ROOT="$BAKEOFF_ROOT/corpora/$TOOL_ID/$CASE_ID"
install -d "$(dirname "$CASE_ROOT")"
test ! -e "$CASE_ROOT"
cp -R "$BAKEOFF_ROOT/templates/baseline" "$CASE_ROOT"
test ! -e "$CASE_ROOT/docs/evidence/wiki-bakeoff-code-2026-09/matrix.md"
```

Repeat with `TOOL_ID=gcode`. C3 copies `templates/change`; all other cases copy baseline and apply
only their specified fixture. Tool state and outputs live under
`$BAKEOFF_ROOT/state/<tool>/<case>` and `$BAKEOFF_ROOT/results/<tool>/<case>`, never inside a source
copy unless the native tool cannot separate them; any native in-tree output is excluded from the
next input hash.

## Source-grounded question key

### Domain inventory

| Domain | Gold summary | Frozen evidence |
| --- | --- | --- |
| Shared platform boundary | The local-first shared package owns Lightspeed synchronization, normalized PostgreSQL mirrors, forecasting, plans, review artifacts, and guarded application. The standalone Restocks and Buylist tools remain authoritative within their legacy boundaries until an explicit cutover. | [README lines 1-6](https://github.com/GobbyAI/game-goblins/blob/0216f1e33f05962d49467d95fe84609041c6dba8/README.md#L1-L6), [architecture lines 3-95](https://github.com/GobbyAI/game-goblins/blob/0216f1e33f05962d49467d95fe84609041c6dba8/docs/architecture.md#L3-L95) |
| Synchronization | Every sync run and resource is tracked. Each page atomically persists raw records, normalized records, counts, and an advancing numeric watermark. Runs fail if required resources do not all succeed. | [sync store lines 37-167](https://github.com/GobbyAI/game-goblins/blob/0216f1e33f05962d49467d95fe84609041c6dba8/src/game_goblins/replenishment/sync_store.py#L37-L167), [run completion lines 230-292](https://github.com/GobbyAI/game-goblins/blob/0216f1e33f05962d49467d95fe84609041c6dba8/src/game_goblins/replenishment/sync_store.py#L230-L292) |
| Daily/weekly planning | Daily planning serves only the warehouse-to-Little-Rock lane. Weekly planning orders Conway return, Conway/Little Rock seeds, store replenishment, network target, and vendor purchasing while respecting floors and available stock. | [planner lines 251-498](https://github.com/GobbyAI/game-goblins/blob/0216f1e33f05962d49467d95fe84609041c6dba8/src/game_goblins/replenishment/planner.py#L251-L498), [daily execution lines 273-381](https://github.com/GobbyAI/game-goblins/blob/0216f1e33f05962d49467d95fe84609041c6dba8/src/game_goblins/replenishment/daily.py#L273-L381) |
| Writes and approvals | Shadow mode writes review artifacts without external mutation. Publish preflights write scopes, applies and reads back settings/transfers, and uploads the vendor workbook only after transfer verification. | [README lines 71-73](https://github.com/GobbyAI/game-goblins/blob/0216f1e33f05962d49467d95fe84609041c6dba8/README.md#L71-L73), [weekly writes lines 316-389](https://github.com/GobbyAI/game-goblins/blob/0216f1e33f05962d49467d95fe84609041c6dba8/src/game_goblins/replenishment/weekly_writes.py#L316-L389), [transfer verification lines 558-675](https://github.com/GobbyAI/game-goblins/blob/0216f1e33f05962d49467d95fe84609041c6dba8/src/game_goblins/replenishment/weekly_writes.py#L558-L675) |
| Recovery | Sync records failures while retaining prior committed pages. Publish applications record a run, completed steps, transfer IDs, status, and incomplete summary. Stable transfer names plus readback make reruns reconcilable, but this is point-specific recovery, not a claim of universal automatic resume. | [sync failure lines 230-292](https://github.com/GobbyAI/game-goblins/blob/0216f1e33f05962d49467d95fe84609041c6dba8/src/game_goblins/replenishment/sync_store.py#L230-L292), [apply tracking lines 769-853](https://github.com/GobbyAI/game-goblins/blob/0216f1e33f05962d49467d95fe84609041c6dba8/src/game_goblins/replenishment/weekly_writes.py#L769-L853) |
| Vendor workbook | A versioned workbook contains Orders, Transfers, Sales, Demand, Bands, and Exceptions sheets. Publication is gated by transfer verification. | [writer lines 54-171](https://github.com/GobbyAI/game-goblins/blob/0216f1e33f05962d49467d95fe84609041c6dba8/src/game_goblins/replenishment/vendor_workbook/writer.py#L54-L171), [weekly writes lines 360-383](https://github.com/GobbyAI/game-goblins/blob/0216f1e33f05962d49467d95fe84609041c6dba8/src/game_goblins/replenishment/weekly_writes.py#L360-L383) |
| Restocks | The standalone script pages Lightspeed products, inventory, and prior-day sales; it produces needs-image and restock/missing-inventory CSVs and uploads the reports to Slack. Pull quantities are capped by available stock. | [Restocks lines 122-276](https://github.com/GobbyAI/game-goblins/blob/0216f1e33f05962d49467d95fe84609041c6dba8/Restocks/restockmaster.py#L122-L276), [main lines 324-420](https://github.com/GobbyAI/game-goblins/blob/0216f1e33f05962d49467d95fe84609041c6dba8/Restocks/restockmaster.py#L324-L420) |
| Buylist | The standalone Buylist builds per-game operational and unpriced CSVs from a retained catalog plus current pricing, then updates internal/public Google Sheets and reports aggregate status to Slack. Daily feed changes hydrate through TCGplayer; direct reconciliation is attempted weekly; a failed refresh can retain a usable prior snapshot. | [Buylist README lines 1-23](https://github.com/GobbyAI/game-goblins/blob/0216f1e33f05962d49467d95fe84609041c6dba8/Buylist/README.md#L1-L23), [pipeline lines 426-456](https://github.com/GobbyAI/game-goblins/blob/0216f1e33f05962d49467d95fe84609041c6dba8/Buylist/buylist_automation.py#L426-L456), [catalog sync lines 755-872](https://github.com/GobbyAI/game-goblins/blob/0216f1e33f05962d49467d95fe84609041c6dba8/Buylist/buylist_catalog.py#L755-L872) |
| C3 change | Product-name prefix rules are loaded independently of category rules. A case-sensitive longest-prefix match wins before the category minimum for automatic, non-excluded products. Tracked policy adds Hobby Supplies minimum 2 and `Sleeves: ` minimum 4. | [settings at changed commit lines 197-214](https://github.com/GobbyAI/game-goblins/blob/8b24ac26699aac8b24254a647aa70b208287b492/src/game_goblins/platform/settings.py#L197-L214), [target selection lines 41-75](https://github.com/GobbyAI/game-goblins/blob/8b24ac26699aac8b24254a647aa70b208287b492/src/game_goblins/replenishment/store_targets.py#L41-L75), [tracked policy lines 83-95](https://github.com/GobbyAI/game-goblins/blob/8b24ac26699aac8b24254a647aa70b208287b492/config/replenishment.toml#L83-L95) |

### Common questions and answer keys

`D` means directly stated or mechanically observable, `I` means a bounded inference from linked
implementation, and `A` means the source itself leaves a material ambiguity. Scoring requires the
right conclusion, the D/I/A label, and at least one supporting file and line span. A system must
not convert an `A` answer into false certainty.

| ID | Question | Gold answer | Class |
| --- | --- | --- | --- |
| Q01 | What is the shared platform, and which systems remain standalone? | The shared local-first package owns sync/normalized data, replenishment forecasting/planning, and guarded writes. Restocks and Buylist remain standalone legacy systems until explicit cutover. | D |
| Q02 | Which data source is authoritative and what is PostgreSQL's role? | Lightspeed remains the system of record; PostgreSQL holds normalized local mirrors and workflow state. | D |
| Q03 | How does a paged synchronization protect progress? | Raw and normalized rows, counts, and numeric watermark advance in one transaction per page; the run succeeds only if all required resources succeed. | D |
| Q04 | What exactly does the daily replenishment cadence do? | It plans the warehouse-to-Little-Rock lane, writes transfer/refacing review files in shadow, and in publish mode preflights, applies, verifies, and publishes guarded results. | D |
| Q05 | What is the weekly lane order and why does order matter? | Conway return, Conway-only/Little-Rock seed behavior, store replenishment, network target, then vendor purchasing; the implementation implies order matters because earlier moves change available/projected stock and floors constrain later moves. | I |
| Q06 | What prevents an unreviewed run from mutating Lightspeed? | Shadow is the default non-mutating path; publish requires explicit mode/baseline behavior, scope preflight, a publish lock, readback verification, and tracked application state. | D |
| Q07 | What is in the vendor workbook and when is it uploaded? | Orders, Transfers, Sales, Demand, Bands, and Exceptions; publish uploads it only when transfer application is publication-ready. | D |
| Q08 | What recovery behavior is implemented after interruption? | Committed sync pages and watermarks survive and failures are recorded. Stable transfer names/readbacks and persisted application status support bounded reconciliation, but the source does not specify one universal automatic-resume path for every interruption point. The exact operator action after a partial external settings write is ambiguous. | A |
| Q09 | What does Restocks read, calculate, and publish? | It reads paged products/inventory and prior-day store sales, builds image/restock/missing-stock CSVs, caps pulls to stock where implemented, and uploads reports to Slack. | D |
| Q10 | What does Buylist produce and where does it publish? | Per-game operational and Unpriced CSVs for Magic, Pokemon, One Piece, and Riftbound, then internal/public Google Sheets plus aggregate Slack status. | D |
| Q11 | How does Buylist catalog refresh and failure retention work? | TCGCSV identifies daily changes, TCGplayer hydrates changed products/all SKUs, direct reconciliation is weekly, and a failed refresh retains a usable complete snapshot when one exists. | D |
| Q12 | Is Buylist already part of the shared platform? | No. The standalone Buylist is implemented and operational; shared-platform Buylist integration is still described as future work. | D |
| Q13 | Which artifacts express intent rather than implemented truth? | Tracked plans and historical docs may express intent; current code/tests and current operational docs determine implementation. A mismatch must be surfaced, not blended. | D |
| Q14 | What changed at the C3 commit? | Eight existing files changed and one test file was added. Name-prefix rules were added; the case-sensitive longest matching prefix overrides category/default minimum for automatic, non-excluded products. Policy sets Hobby Supplies to 2 and `Sleeves: ` to 4. | D |

Q14's exact changed-file key is:

```text
M config/replenishment.toml
M docs/replenishment.md
M src/game_goblins/platform/settings.py
M src/game_goblins/replenishment/daily.py
M src/game_goblins/replenishment/planner.py
M src/game_goblins/replenishment/store_targets.py
M tests/platform/test_settings.py
M tests/replenishment/test_planning_store.py
A tests/replenishment/test_store_targets.py
```

## Comparator capability audit

This table prevents the bakeoff from erasing delivered gcode capability or imputing features to
either tool. `Native` means the pinned tool owns the surface. Graphify statements were audited
directly from the `graphifyy 0.9.55` source distribution (SHA-256
`8135a5a22b6b78745aa3ab040cb3f5cecd7126eef5b4e89764404e6e75b58568`); its package metadata names
the frozen repository. C0 must still prove that the installed executable is the planned
commit/package pair. The machine's older installed `0.9.34` is not capability evidence.

| Capability | Graphify `0.9.55` disposition | gcode `1.7.0` disposition |
| --- | --- | --- |
| Code structure | Native multi-language AST extraction, including Python | Native tree-sitter symbols plus safe text chunks |
| Incremental refresh | Native manifest-gated extract and code-only update/watch/check-update; stale-source pruning and shrink guards are explicit | Native hash-based stale/orphan detection, per-file indexing transactions, index/invalidate/prune |
| Symbol/text retrieval | Native graph query/explanation family; `query` is bounded BFS/DFS traversal rather than ranked text search | Native search, search-symbol, search-text, search-content, grep, outline, symbol, tree |
| Call/import paths and impact | Native path, explain, and reverse `affected` traversal | Native callers/callees/usages/imports/path/blast-radius |
| Communities and architecture | Native clustering, model-optional labels, hubs, graph report, HTML, and tree | Native MCG Leiden communities, FCG/MCG/class views, project graph reports, Mermaid |
| Provenance/confidence | Native `EXTRACTED`, `INFERRED`, `AMBIGUOUS` and numeric confidence; report/wiki/export preserve the fields | Native `EXTRACTED`, `INFERRED`, `AMBIGUOUS` label plus optional numeric/source metadata |
| Rationale and docs | Python/JavaScript extraction emits typed rationale nodes/`rationale_for` and document-reference `cites`; semantic extraction covers documents | Docs/comments/config are content-searchable; exhaustive graph schema has no typed rationale/doc-reference edge |
| Native natural-language answer synthesis | No native Ask synthesis: `query` returns a bounded graph traversal selected from question terms | No native Ask/synthesis command in contract v8; retrieval evidence is scored, not a fabricated answer |
| Native presentation/export | Native graph JSON/report, HTML, call-flow HTML, tree, Obsidian, Markdown wiki, SVG, and GraphML | Stable JSON/text, Mermaid graph views, and project graph report are native; no complete wiki renderer in contract v8 |

gcode's frozen documentation describes the index and hybrid stores
([README lines 23-51](https://github.com/GobbyAI/gobby/blob/7394b97c1d88c82f685e788e798de2cfd728ad15/crates/gcode/README.md#L23-L51)), commands
([README lines 107-192](https://github.com/GobbyAI/gobby/blob/7394b97c1d88c82f685e788e798de2cfd728ad15/crates/gcode/README.md#L107-L192)), content coverage and security exclusions
([README lines 283-306](https://github.com/GobbyAI/gobby/blob/7394b97c1d88c82f685e788e798de2cfd728ad15/crates/gcode/README.md#L283-L306)), and graceful degradation
([README lines 264-281](https://github.com/GobbyAI/gobby/blob/7394b97c1d88c82f685e788e798de2cfd728ad15/crates/gcode/README.md#L264-L281)). Contract v8 specifies stable query fields, graph views, communities, and Mermaid
([CLI contract lines 22-54](https://github.com/GobbyAI/gobby/blob/7394b97c1d88c82f685e788e798de2cfd728ad15/docs/contracts/gcode-cli.md#L22-L54)). Graph results preserve categorical and numeric provenance
([models lines 14-121](https://github.com/GobbyAI/gobby/blob/7394b97c1d88c82f685e788e798de2cfd728ad15/crates/gcode/src/models.rs#L14-L121),
[models lines 609-628](https://github.com/GobbyAI/gobby/blob/7394b97c1d88c82f685e788e798de2cfd728ad15/crates/gcode/src/models.rs#L609-L628)).

Graphify's frozen CLI exposes versioning, traversal, extraction, update, and export surfaces
([entry point lines 528-666](https://github.com/Graphify-Labs/graphify/blob/c9f99018774e2e0380e9f65b3959944559a0d5f6/graphify/__main__.py#L528-L666)). Its query implementation is bounded graph traversal
([CLI lines 1202-1322](https://github.com/Graphify-Labs/graphify/blob/c9f99018774e2e0380e9f65b3959944559a0d5f6/graphify/cli.py#L1202-L1322)); update is a no-LLM code rebuild
([CLI lines 2400-2458](https://github.com/Graphify-Labs/graphify/blob/c9f99018774e2e0380e9f65b3959944559a0d5f6/graphify/cli.py#L2400-L2458)). Python extraction creates typed rationale edges and source locations
([extract lines 1235-1258](https://github.com/Graphify-Labs/graphify/blob/c9f99018774e2e0380e9f65b3959944559a0d5f6/graphify/extract.py#L1235-L1258)); JavaScript extraction also emits rationale and document-reference edges
([extract lines 1690-1750](https://github.com/Graphify-Labs/graphify/blob/c9f99018774e2e0380e9f65b3959944559a0d5f6/graphify/extract.py#L1690-L1750)). The report exposes communities and confidence distributions
([report lines 98-179](https://github.com/Graphify-Labs/graphify/blob/c9f99018774e2e0380e9f65b3959944559a0d5f6/graphify/report.py#L98-L179)), while native exports include Markdown wiki and call-flow HTML
([CLI lines 2779-3047](https://github.com/Graphify-Labs/graphify/blob/c9f99018774e2e0380e9f65b3959944559a0d5f6/graphify/cli.py#L2779-L3047)). Graph/report JSON writes are atomic against process interruption
([paths lines 29-101](https://github.com/Graphify-Labs/graphify/blob/c9f99018774e2e0380e9f65b3959944559a0d5f6/graphify/paths.py#L29-L101)).

## Controls and measurements

### Execution controls

- Use a fresh copy and tool-owned state for every `(tool, case)` pair. Never point a comparator at
  the read-only source checkout, the other tool's copy, or this answer key.
- Run local work serially. Hosted generation uses at most two concurrent calls. Record native
  child-process and model-call concurrency separately; do not infer it from wall time.
- Use the approved hosted models: Terra at medium reasoning for the primary hosted run, Luna only
  for the declared calibration, and Qwen at xhigh only for the declared local comparison when the
  tool supports the required adapter. Record requested and effective provider/model/reasoning.
- Use `nomic-ai/nomic-embed-text-v1.5`, 768 dimensions, the required query/document prefixes, and
  the pinned context policy wherever semantic embeddings are enabled. C0 must compare requested
  and effective values. A fallback, dimension drift, missing prefix, or changed model invalidates
  semantic results; deterministic lanes may remain valid if separately measured.
- Measure deterministic extraction/indexing separately from Graphify semantic extraction,
  community labeling, or any model-derived summaries. For gcode, separate AST/BM25/graph work from
  embedding calls. Never assign generation tokens to a native retrieval-only command.
- Per operation: soft observation at 15 minutes, diagnostic capture at 60 minutes, hard stop at
  120 minutes. Allow one diagnosed retry using the same input and controls. Preserve the failed
  attempt; do not report only the retry.
- No live/global config, hooks, daemon restart, published source, external writes, or global graph
  merge. The environment task owns isolated provisioning and signed project grants.

### Required run record

Write one JSON Lines record per attempt. Unknown quantities are the literal string `"unknown"`,
not zero. Currency estimates never substitute for provider quota or token counts.

| Group | Required fields |
| --- | --- |
| Identity | `run_id`, `case_id`, `tool`, `attempt`, `source_commit`, `corpus_snapshot_id`, `input_tree_sha256`, `executable_sha256`, `version`, `lock_or_image_hash` |
| Invocation | `command_argv`, redacted `environment_diff`, `working_directory`, `state_directory`, `output_directory`, `started_at`, `ended_at`, `wall_seconds`, `exit_code`, `termination_signal` |
| Model | `stage`, `requested_provider`, `requested_model`, `requested_reasoning`, `effective_provider`, `effective_model`, `effective_reasoning`, `model_calls`, `input_tokens`, `output_tokens`, `cache_tokens`, `retry_count`, `native_concurrency` |
| Embeddings | `requested_embedding_model`, `effective_embedding_model`, `dimension`, `query_prefix`, `document_prefix`, `context_policy`, `probe_status`, redacted `endpoint_fingerprint` |
| Index | `files_scanned`, `files_indexed`, `files_skipped`, `nodes`, `edges`, `symbols`, `imports`, `calls`, `unresolved`, `chunks`, `communities`, `tombstones`, `changed`, `unchanged`, `removed`, `degraded_sources` |
| Retrieval/export | `question_id`, `query_id`, `rank`, `path`, `line_start`, `line_end`, `score`, `relation`, `distance`, `provenance`, `output_inventory_sha256`, `output_file_count`, `output_bytes` |
| Outcome | `support_disposition`, `pass_fail`, `evidence_paths`, `warnings`, `fallbacks`, `notes` |

`support_disposition` is one of `supported`, `demonstrated-unsupported`, `blocked-preflight`, or
`not-applicable`. Demonstrated unsupported requires pinned source/contract plus the frozen binary's
command inventory; a missing result, old binary, configuration failure, or degraded dependency is
only blocked/fail. A fallback must be visible and makes the affected semantic measurement fail.

### Common scoring

- Retrieval: for every Q01-Q14, record reciprocal rank of the first gold-supporting span, recall at
  5/10/20, citation precision at 10, wrong-domain collisions, D/I/A classification accuracy, and
  unsupported/ambiguity honesty. The scorer reads the external key only after outputs are sealed.
- Structure: compare symbols and relations against explicit case assertions. Report precision,
  recall, false merges, missing edges, and provenance preservation by relation kind.
- Change: report changed-file precision/recall, stale artifact count, deleted-symbol residue,
  identity continuity, full-rebuild equivalence, and bytes/time/tokens for incremental work.
- Presentation: inventory native outputs, broken relative links, source-line resolvability,
  question coverage, graph/report determinism, and whether D/I/A or provenance survives export.
- Efficiency: report cold and incremental wall time, peak RSS when available, state/output bytes,
  model and embedding calls, tokens, retries, and failure rate. Do not collapse deterministic and
  model-derived stages into one number.

## C0-C9 executable cases

The environment task records exact absolute binary paths as `GRAPHIFY_BIN` and `GCODE_BIN` and
verifies their SHA-256 values. Examples below use frozen command names; if pinned Graphify source
proves a syntax change, C0 records the exact replacement in `command-manifest.json` before any
comparison. That manifest is then immutable. A missing command becomes demonstrated unsupported,
not a hand-authored substitute.

### C0 — compatibility and effective configuration

**Inputs.** Fresh baseline copies, pinned executables, lock/image evidence, isolated state, and no
answer-key material.

**Execute.** Save stdout, stderr, exit code, and environment diff for:

```bash
"$GRAPHIFY_BIN" --version
"$GRAPHIFY_BIN" --help
"$GRAPHIFY_BIN" extract "$CASE_ROOT" --code-only --no-cluster --out "$CASE_STATE"
"$GCODE_BIN" --version
"$GCODE_BIN" --format json contract
"$GCODE_BIN" schema-identity --json
"$GCODE_BIN" --format json embeddings doctor
"$GCODE_BIN" --format json status
```

Also verify Graphify's executable/package resolves to `c9f990...`/`0.9.55` and gcode resolves to
`7394b97...`/`1.7.0` with contract v8. Capture Graphify's top-level command inventory and pinned
source for `extract`, `update`, `path`, `explain`, `query`, `affected`, `cluster-only`, `label`,
`check-update`, `tree`, `benchmark`, and `export callflow-html`. Do not use the machine's observed
Graphify `0.9.34` as the comparator.

**Expected observations and metrics.** Executable hashes, versions, contract/schema identity, provider/model,
embedding model/dimension/prefix/context, dependency health, output locations/schemas, command
availability, and any fallback. Preserve redacted effective config.

**Pass/fail.** Pass only when both exact pins run against isolated copies and all requested versus
effective controls agree. A required native surface absent at the pin is demonstrated unsupported
for its later case. Wrong version, signed-grant mismatch, unavailable backend, or silent fallback
fails C0 and blocks affected later measurements; it does not prove a capability gap.

**Known preflight diagnostics.** During casebook authoring, Game Goblins `gcode repo-outline` and
`gcode search` returned `malformed grant: grant project does not match local project`; `gcode
projects` from Gobby returned `daemon_required`. Fetching frozen Graphify source from GitHub was
blocked by HTTP/tunnel 403, and `uv tool list` could not create a temporary file in the user tool
directory. Those are navigation/environment observations, not comparator results. C0 under the
isolated signed grant must resolve them.

### C1 — cold baseline construction

**Inputs.** Fresh baseline copy and empty per-case tool state.

**Execute.** Run each tool's validated cold-build commands, preserving deterministic and semantic
stages separately:

```bash
"$GRAPHIFY_BIN" extract "$CASE_ROOT" --code-only --no-cluster --out "$CASE_STATE/code-only"
"$GRAPHIFY_BIN" extract "$CASE_ROOT" --backend "$GRAPHIFY_BACKEND" --model "$GRAPHIFY_MODEL" --max-concurrency 2 --out "$CASE_STATE/semantic"
"$GCODE_BIN" --format json index "$CASE_ROOT" --full --sync-projections
"$GCODE_BIN" --format json status
```

If C0 shows `--full` is represented by a separately pinned invalidate+index operation, use that
recorded argv; do not mix old and new state. Export the native node/edge/file inventories.

**Expected observations and metrics.** Required run record, coverage by domain and file type, symbol/relation
counts, skipped-file reasons, communities, provenance, deterministic-versus-model work, and state
hashes. Verify that shared platform, Restocks, Buylist, tests, manifests, config examples, and docs
all appear in the expected structural or content lane.

**Pass/fail.** Pass when a clean build completes without hidden fallback, all required domains are
discoverable, exclusions remain absent, and repeatable inventories are sealed. A tool may classify
Markdown/config as content rather than AST; omission is a failure unless its pinned contract
explicitly demonstrates the input type is unsupported.

### C2 — unchanged rerun

**Inputs.** Sealed C1-equivalent baseline copy and its completed native state, with an identical
`input_tree_sha256`.

**Execute.** Copy the C1 state into a new C2 state using the environment's documented snapshot
mechanism, then run:

```bash
GRAPHIFY_OUT="$CASE_STATE/code-only/graphify-out" "$GRAPHIFY_BIN" update "$CASE_ROOT" --no-cluster
"$GRAPHIFY_BIN" extract "$CASE_ROOT" --backend "$GRAPHIFY_BACKEND" --model "$GRAPHIFY_MODEL" --max-concurrency 2 --out "$CASE_STATE/semantic"
GRAPHIFY_OUT="$CASE_STATE/semantic/graphify-out" "$GRAPHIFY_BIN" check-update "$CASE_ROOT"
"$GCODE_BIN" --format json index "$CASE_ROOT" --sync-projections
"$GCODE_BIN" --format json status
```

**Expected observations and metrics.** Files rescanned/reindexed, changed/unchanged counts, model/embedding calls,
wall time, state delta, output hash, warnings, and whether stable nodes retain identity.

**Pass/fail.** Pass when both tools recognize no source change, preserve the complete C1 graph and
retrieval behavior, leave no stale artifacts, and avoid semantic/model work not required by their
documented unchanged path. A mandatory full rebuild is supported but loses incremental-efficiency
credit; claiming incrementality while rebuilding fails.

### C3 — committed multi-file behavior change

**Inputs.** Fresh copy of changed commit `8b24ac...`, plus independent state built from baseline for
the same tool. Do not synthesize this diff.

**Execute.** Confirm the exact nine-path key above with `git -C "$SOURCE_REPO" diff --name-status
"$BASE_SHA" "$CHANGE_SHA"`, then run the same native incremental commands as C2. Query Q14 and the
symbols/config keys `NamePrefixRuleSettings`, `for_name`, `compute_store_targets`,
`name_prefix_rules`, `Hobby Supplies`, and `Sleeves: `.

**Expected observations and metrics.** Changed-file precision/recall, added/modified symbols and relations,
documentation/config retrieval, community churn, model calls, stale facts, and result provenance.

**Pass/fail.** Pass when the tool detects all nine changed paths, retrieves the exact Q14 behavior,
connects policy/loading/planning/target-selection evidence where its native schema permits, and
does not retain the baseline-only category-rule conclusion. Typed rationale edges are not required
from a tool whose C0 contract demonstrates no such native relation; relevant content still must be
retrievable.

### C4 — controlled rationale/comment edit

**Inputs.** Fresh baseline copy.

**Execute.** Apply exactly one replacement:

```bash
uv run python - "$CASE_ROOT" <<'PY'
from pathlib import Path
import sys

path = Path(sys.argv[1]) / "src/game_goblins/replenishment/product_types.py"
old = '"""Classify a catalog name using its final colon-delimited segment."""'
new = '"""Classify from the final colon-delimited segment so set names do not mask product form."""'
text = path.read_text(encoding="utf-8")
assert text.count(old) == 1 and new not in text
path.write_text(text.replace(old, new), encoding="utf-8")
PY
```

Run `uv run python -m compileall -q "$CASE_ROOT/src" "$CASE_ROOT/tests"`, seal the input hash, then
run the native incremental commands from C2. Query the new exact sentence, `derive_demand_type`,
and why the final colon-delimited segment is used.

**Expected observations and metrics.** Changed-file detection, comment/doc indexing, rationale retrieval rank,
symbol identity, graph churn, semantic calls, and stale old sentence count.

**Pass/fail.** Pass when the single file is detected, the new rationale is retrievable with its
source span, the old sentence is absent, behavior/structure are not falsely reported as changed,
and output labels rationale as direct prose rather than inferred code behavior. A pinned contract
that excludes comments may demonstrate unsupported comment indexing; a missed hit alone cannot.

### C5 — behavior-preserving symbol rename

**Inputs.** Fresh baseline copy.

**Execute.** Rename all and only 12 exact identifier occurrences across four files:

```bash
uv run python - "$CASE_ROOT" <<'PY'
from pathlib import Path
import sys

root = Path(sys.argv[1])
expected = {
    "src/game_goblins/replenishment/product_types.py": 3,
    "src/game_goblins/replenishment/set_sell_through.py": 4,
    "src/game_goblins/replenishment/vendor_workbook/queries.py": 2,
    "tests/replenishment/test_product_types.py": 3,
}
old, new = "derive_demand_type", "classify_demand_type"
for relative, count in expected.items():
    path = root / relative
    text = path.read_text(encoding="utf-8")
    assert text.count(old) == count and new not in text
    path.write_text(text.replace(old, new), encoding="utf-8")
assert sum(expected.values()) == 12
PY
```

Run compileall, seal the input hash, then the C2 incremental commands. Resolve the renamed symbol,
its callers/usages, imports, source, and exact old-name grep.

**Expected observations and metrics.** Four-file detection, old/new node identities, caller/import preservation,
false add/delete versus rename evidence, stale old-name hits, affected paths, and rebuild
equivalence.

**Pass/fail.** Pass when all four paths update, the new symbol has the correct definition and
references, no old symbol/reference remains, and no unrelated relation changes. Rename identity
continuity earns separate credit; delete+add is allowed only when the tool's pinned identity model
documents it and no stale fact remains.

### C6 — helper removal with inlined behavior

**Inputs.** Fresh baseline copy.

**Execute.** Replace the sole `_ceil` call and remove the sole helper block:

```bash
uv run python - "$CASE_ROOT" <<'PY'
from pathlib import Path
import sys

path = Path(sys.argv[1]) / "src/game_goblins/replenishment/product_types.py"
text = path.read_text(encoding="utf-8")
old_call = "    units = _ceil(rate.daily_rate * Decimal(horizon))"
new_call = (
    "    units = int(\n"
    "        (rate.daily_rate * Decimal(horizon)).to_integral_value(\n"
    "            rounding=ROUND_CEILING\n"
    "        )\n"
    "    )"
)
old_helper = (
    "\n\ndef _ceil(value: Decimal) -> int:\n"
    "    return int(value.to_integral_value(rounding=ROUND_CEILING))\n"
)
assert text.count(old_call) == 1 and text.count(old_helper) == 1
text = text.replace(old_call, new_call).replace(old_helper, "")
path.write_text(text, encoding="utf-8")
PY
```

Run compileall, seal the input hash, then the C2 incremental commands. Query `_ceil`, its former
caller, and `ROUND_CEILING`.

**Expected observations and metrics.** Removed node/edge/tombstone counts, stale graph/search/vector hits, caller
source correctness, changed-file scope, model work, and full-rebuild equivalence.

**Pass/fail.** Pass when `_ceil` and its call edge disappear from every native surface, the inlined
rounding remains retrievable, no unrelated facts change, and the incremental result equals a clean
build modulo documented nondeterministic IDs/timestamps. A native update that refuses a smaller
graph must use its pinned deletion/refactor flag and record that cost.

### C7 — interruption and native recovery

**Inputs.** Fresh baseline copy, fresh state, and `command-manifest.json` entries containing each
exact C1 deterministic argv plus an observed progress-marker regex or state-file glob. Establish
the marker during the C0 command-inventory probe; it must precede successful completion and must
not alter source. If normal execution exposes neither kind of marker, use only a pinned,
documented fault-injection/test seam and record it in the manifest. Do not inflate or mutate the
corpus.

**Execute.** Set `INTERRUPT_ARGV_JSON` to the expanded JSON argv from the immutable manifest. Set at
least one of `INTERRUPT_MARKER_RE` (matched against combined stdout/stderr bytes) or
`INTERRUPT_MARKER_GLOB` (an absolute tool-state glob), then run this event-driven wrapper once per
tool. It starts a separate process group, logs the first observable marker, sends exactly one
planned `SIGTERM`, and uses `SIGKILL` only as a reported safety cleanup if the process ignores the
30-second termination deadline.

```bash
export INTERRUPT_LOG="$BAKEOFF_ROOT/results/$TOOL_ID/C7/interrupted.log"
export INTERRUPT_RECORD="$BAKEOFF_ROOT/results/$TOOL_ID/C7/interruption.json"
export INTERRUPT_TIMEOUT_SECONDS=900
install -d "$(dirname "$INTERRUPT_LOG")"
uv run python - <<'PY'
from __future__ import annotations

import glob
import json
import os
from pathlib import Path
import re
import selectors
import signal
import subprocess
import time

argv = json.loads(os.environ["INTERRUPT_ARGV_JSON"])
assert isinstance(argv, list) and argv and all(isinstance(value, str) for value in argv)
marker_pattern = os.environ.get("INTERRUPT_MARKER_RE")
marker_glob = os.environ.get("INTERRUPT_MARKER_GLOB")
assert marker_pattern or marker_glob, "configure an observed output regex or state-file glob"
marker = re.compile(marker_pattern.encode()) if marker_pattern else None
deadline = time.monotonic() + int(os.environ["INTERRUPT_TIMEOUT_SECONDS"])
log_path = Path(os.environ["INTERRUPT_LOG"])
record_path = Path(os.environ["INTERRUPT_RECORD"])
process = subprocess.Popen(
    argv,
    stdout=subprocess.PIPE,
    stderr=subprocess.STDOUT,
    start_new_session=True,
)
assert process.stdout is not None
selector = selectors.DefaultSelector()
selector.register(process.stdout, selectors.EVENT_READ)
tail = b""
observed = None
sigterm_at = None
forced_kill = False
started_at = time.time()
with log_path.open("wb") as log:
    while process.poll() is None and time.monotonic() < deadline:
        for key, _ in selector.select(timeout=0.25):
            chunk = os.read(key.fileobj.fileno(), 65536)
            if chunk:
                log.write(chunk)
                log.flush()
                tail = (tail + chunk)[-131072:]
                if marker and marker.search(tail):
                    observed = {"kind": "output-regex", "value": marker_pattern}
                    break
        if observed is None and marker_glob:
            matches = sorted(glob.glob(marker_glob))
            if matches:
                observed = {"kind": "state-glob", "value": marker_glob, "match": matches[0]}
        if observed is not None:
            sigterm_at = time.time()
            os.killpg(process.pid, signal.SIGTERM)
            break

    if observed is None:
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGKILL)
            forced_kill = True
        process.wait()
    else:
        try:
            process.wait(timeout=30)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            forced_kill = True
            process.wait()

    remainder = process.stdout.read()
    if remainder:
        log.write(remainder)

record = {
    "argv": argv,
    "started_at_unix": started_at,
    "marker": observed,
    "sigterm_at_unix": sigterm_at,
    "exit_code": process.returncode,
    "forced_kill": forced_kill,
}
record_path.write_text(json.dumps(record, indent=2, sort_keys=True) + "\n", encoding="utf-8")
assert observed is not None, "operation completed or timed out before the configured marker"
assert sigterm_at is not None and process.returncode != 0 and not forced_kill
PY
```

Preserve partial state and logs. Recover exactly once, then create clean comparison state:

```bash
# Graphify: INTERRUPT_ARGV_JSON was the C1 code-only extract with
# --out "$CASE_STATE/interrupted".
GRAPHIFY_OUT="$CASE_STATE/interrupted/graphify-out" "$GRAPHIFY_BIN" update "$CASE_ROOT" --no-cluster
GRAPHIFY_OUT="$CASE_STATE/interrupted/graphify-out" "$GRAPHIFY_BIN" check-update "$CASE_ROOT"
"$GRAPHIFY_BIN" extract "$CASE_ROOT" --code-only --no-cluster --out "$CASE_STATE/clean"

# gcode: INTERRUPT_ARGV_JSON was the exact C1 index argv. The environment task binds
# RECOVERY_CASE_ROOT to its interrupted isolated project and CLEAN_CASE_ROOT to an untouched
# baseline copy with fresh isolated state.
"$GCODE_BIN" --format json index "$RECOVERY_CASE_ROOT" --sync-projections
"$GCODE_BIN" --format json status
"$GCODE_BIN" --format json index "$CLEAN_CASE_ROOT" --full --sync-projections
```

**Expected observations and metrics.** Interruption point, termination status, corrupt/partial files, locks,
incomplete records, recovery command, rescanned/reused work, model calls, final inventory hash,
retrieval parity, and clean-build equivalence.

**Pass/fail.** Pass when native recovery detects the incomplete state and produces a queryable
result equivalent to clean C1 without manual database/file repair. If pinned source and command
inventory show no recovery path, record demonstrated unsupported and retain the partial-state
evidence. A hang, undetected partial success, or manual repair is fail, not unsupported.

### C8 — native retrieval and Ask behavior

**Inputs.** Sealed C1 baseline states and external Q01-Q14 key held outside comparator inputs.

**Execute.** Run every common question through the tool's native question/query surface. Also run
this structural battery:

| Query ID | Intent | Graphify native surface | gcode native surface |
| --- | --- | --- | --- |
| R01 | Find `derive_demand_type` definition | `query`/`explain` | `search-symbol`, `outline`, `symbol` |
| R02 | Find all direct callers/references | `affected`/`query` | `callers`, `usages`, plus `grep -w` for callback/text references |
| R03 | Path from demand classification to vendor workbook query | `path` | `path` after resolving endpoint IDs/names |
| R04 | Product-types import dependencies | `query`/`explain` | `imports src/game_goblins/replenishment/product_types.py` |
| R05 | Architecture/community around replenishment | `query`, hubs/community output | `graph view mcg --file src/game_goblins/replenishment/product_types.py` and graph report |
| R06 | Exact shadow/publish policy prose | `query` | `search-content` and `grep` over README/docs |
| R07 | Restocks prior-day report behavior | `query` | `search`, `search-content`, `symbol-at` |
| R08 | Buylist daily versus weekly refresh | `query` | `search`, `search-content`, `symbol-at` |

Use a 2,000-token native budget where supported and paginate to collect the complete top 20 without
truncating individual results. Capture raw results before scoring. For gcode, do not wrap retrieval
in an external LLM and call it native Ask: contract v8 has no Ask/synthesis command. Score its
native ranked evidence and mark answer synthesis demonstrated unsupported from the exhaustive
contract. Graphify `query` is likewise retrieval-only BFS/DFS traversal, so score its evidence and
record native Ask synthesis as demonstrated unsupported; do not upgrade traversal prose into Ask.

**Expected observations and metrics.** Common scoring metrics, query latency/tokens, top supporting span, direct
versus inferred classification, false certainty, path validity, relation provenance, pagination,
and degradation warnings.

**Pass/fail.** Pass retrieval when all domains have gold-supporting evidence in the sealed result
set, structural paths resolve only real source relationships, and ambiguity is preserved. Pass Ask
only when a native command returns a sourced answer with the right conclusion/D-I-A label. A
source-backed absence is demonstrated unsupported; empty or degraded retrieval is fail/blocked.

### C9 — native export and presentation

**Inputs.** Sealed C1 state; no answer key, hand-written bridge, or post-processed generated pages.

**Execute.** Invoke the frozen native Graphify exports below and the C0-confirmed gcode JSON/text,
`graph view` Mermaid, and project graph-report commands. Do not invoke Graphify's Neo4j/FalkorDB
push sinks. Record exact argv in the immutable command manifest and run each deterministic export
twice from unchanged state.

```bash
export GRAPH="$CASE_STATE/semantic/graphify-out/graph.json"
export LABELS="$CASE_STATE/semantic/graphify-out/.graphify_labels.json"
export REPORT="$CASE_STATE/semantic/graphify-out/GRAPH_REPORT.md"
"$GRAPHIFY_BIN" export wiki --graph "$GRAPH" --labels "$LABELS"
"$GRAPHIFY_BIN" export html --graph "$GRAPH" --labels "$LABELS"
"$GRAPHIFY_BIN" export obsidian --graph "$GRAPH" --labels "$LABELS" --dir "$CASE_STATE/obsidian"
"$GRAPHIFY_BIN" export svg --graph "$GRAPH" --labels "$LABELS"
"$GRAPHIFY_BIN" export graphml --graph "$GRAPH"
"$GRAPHIFY_BIN" tree --graph "$GRAPH" --output "$CASE_STATE/GRAPH_TREE.html" --root "$CASE_ROOT"
"$GRAPHIFY_BIN" export callflow-html --graph "$GRAPH" --labels "$LABELS" --report "$REPORT" --output "$CASE_STATE/callflow.html" --lang en
```

**Expected observations and metrics.** File inventory/hash/bytes, link and source-span resolution, Q01-Q14 domain
coverage, navigation, graph readability, provenance/D-I-A survival, deterministic diff, model
calls, and time. Keep Graphify community labels separate from deterministic graph output.

**Pass/fail.** Pass a native artifact when it opens without broken internal references, traces
claims to frozen source, represents all claimed domains accurately, and reproduces modulo declared
timestamps/IDs. Graphify's native Markdown wiki is required and must not be omitted merely because
older local help lacked it. gcode receives credit for native Mermaid and graph-report presentation
even though its facts facade does not own a complete wiki renderer.

## Result table to complete after execution

Do not prefill outcomes. Every cell links to raw evidence and names its disposition.

| Case | Graphify disposition/result | gcode disposition/result | Cross-tool note |
| --- | --- | --- | --- |
| C0 | Pending | Pending | Pin and effective-config gate |
| C1 | Pending | Pending | Cold coverage and cost |
| C2 | Pending | Pending | Unchanged incrementality |
| C3 | Pending | Pending | Real committed change |
| C4 | Pending | Pending | Rationale/comment-only edit |
| C5 | Pending | Pending | Symbol rename |
| C6 | Pending | Pending | Deletion and stale-fact cleanup |
| C7 | Pending | Pending | Interruption/recovery |
| C8 | Pending | Pending | Retrieval versus native Ask |
| C9 | Pending | Pending | Native export/presentation |

The final comparison must report each case independently, list every fallback and blocked
dependency, preserve raw evidence, and resist converting missing capabilities into zero-quality
scores. It must not modify the historical bakeoff or checkpoint and must not publish Game Goblins
source.

_Last verified: 2026-09-07_
