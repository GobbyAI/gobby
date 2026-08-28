# gcode CLI Contract

The machine-readable contract lives at `crates/gcode/contract/gcode.contract.json`.
`gcode contract --format json` must emit the same contract version and contents.

## Version

`contract_version`: 7

Version 7 gives public path inputs explicit scope semantics, adds repeatable
`tree [PATH]...` filters, and replaces the ambiguous graph-view positional seed
with typed `--file`, `--module`, and `--symbol` selectors. Each command below
emits a stable, whitespace-compact JSON shape under `--format json`; the keys
are pinned in `gcode.contract.json` and asserted by drift tests.

### Query surfaces

- `contract`, `index` — project and index metadata
- `search`, `search-symbol`, `search-text`, `search-content` — paged ranked results
  (`project_id, total, offset, limit, next_offset, budget_exceeded, results[]`, each hit carrying `id, name,
  qualified_name, kind, language, file_path, line_start, line_end, signature,
  score`)
- `grep` — paged exact pattern matches with intact spans and context blocks
- `outline` — paged top-level subtrees containing `id, name, kind, line_start,
  line_end, signature` symbols
- `symbol` — a stored symbol record plus the on-disk `source` snippet
- `symbol-at` — same as `symbol`, plus a `lookup` block describing how the
  location resolved
- `symbols` — paged complete stored symbol records with bounded `source`, plus
  `missing_ids` and recovery guidance when edited files invalidate requested IDs
- `kinds` — paged kind strings
- `tree [PATH]...` — paged directory groups containing `file_path, language,
  symbol_count` rows; file, directory-prefix, and glob filters compose with OR
  semantics before pagination
- `callers`, `callees`, `usages` — call/import graph reads (the `graph_read_keys`
  envelope). Each relationship remains a complete page unit; callback references
  require `gcode grep -w`.
- `graph view` — scoped `fcg` / `mcg` / `class-hierarchy` dump. MCG requires
  `--file` or `--module`; FCG and class hierarchy require `--symbol`. JSON keys:
  `project_id, project_root, view, seed, depth, incoming_truncated,
  outgoing_truncated, hint, nodes, edges, communities, mermaid`. Mermaid is
  always present and is never character/token-sliced
- `imports`, `blast-radius` — the paged graph envelope (`project_id, total,
  offset, limit, next_offset, budget_exceeded, results[]`, each row carrying
  `id, name, file_path, line, confidence, relation, distance, metadata, hint`)
- `repo-outline` — paged directory summaries with complete file groups

Stored symbol records carry the AI `summary`, never the raw `docstring`.

## Scope

`--project <ROOT>` selects a project root. Without `--project`, gcode detects the
project from the current working directory. JSON output consumed by Gobby must
identify the resolved project with `project_id` and, where path context matters,
`project_root`.

Filesystem roots (`--project` and positional `index PATH`) use native absolute
or cwd-relative filesystem semantics. Project-file inputs use bare paths from
the project root, `./` or `../` paths from the current working directory, and
absolute paths mapped into the current checkout or overlay. Search, grep, and
tree filters follow the same intent rules while preserving glob syntax.

Paths that escape the current/overlay/parent project scope, including symlink
escapes and paths into another checkout, fail with exit `2` and the typed
`invalid_path_scope` error. When another project root is discoverable, the
error includes a `--project <ROOT>` recovery command. Read commands never
switch projects automatically.

## Format

Navigation commands default to compact text: `search`, `search-symbol`,
`search-text`, `search-content`, `grep`, `outline`, `symbol`, `symbol-at`,
`symbols`, `kinds`, `tree`, `repo-outline`, `callers`, `callees`, `usages`,
`imports`, `path`, and `blast-radius`. Nested structural graph and lifecycle
commands retain complete JSON defaults.

Use explicit `--format json` for daemon and programmatic calls. JSON is the
stable machine surface. Compact text omits UUIDs, scores, and ranking-lane
diagnostics; `--verbose` restores those fields in text where applicable.

Collection commands accept `--limit`, `--offset`, and `--token-budget`.
Compact text receives an automatic 2,000-token page budget. Explicit JSON is
token-paged only when requested and keeps existing command limits. Each budgeted
page chooses the largest complete prefix whose fully rendered response fits
`ceil(chars / 4)`, returns one complete oversized first item when necessary,
and exposes a retrieval path through `next_offset` or an exact shell-safe text
continuation command. `budget_exceeded` is present only when a complete item or
page metadata exceeds the requested budget. `grep -m/--max-count` remains an
alias for canonical `--limit`.

## Embeddings Doctor

`gcode embeddings doctor` emits these top-level JSON keys: `endpoint`, `model`,
`dim`, `probe_error`, `peer_error`, `api_key_present`, `api_key_fingerprint`,
`namespace_resolved`, `source`, `agrees`, and `drift`. A direct dimension-probe
failure populates `probe_error`; daemon peer transport or protocol failures
populate `peer_error`.

Exit codes are `0` for healthy configuration, `10` when neither local nor daemon
configuration resolves, `11` for local/daemon drift, and `20` for a probe or
daemon peer failure. An unreachable daemon with no local configuration exits
`20`, replacing the earlier `10` behavior for that case.

## Drift Checks

Both the CLI and daemon tests load this contract. New daemon-facing flags or JSON
keys should update this document, the JSON contract, and the corresponding drift
tests in the same change.
