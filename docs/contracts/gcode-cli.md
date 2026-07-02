# gcode CLI Contract

The machine-readable contract lives at `crates/gcode/contract/gcode.contract.json`.
`gcode contract --format json` must emit the same contract version and contents.

## Version

`contract_version`: 2

Version 2 marks the full daemon-consumed query surface. Each command below emits
a stable JSON shape under `--format json`; the keys are pinned in
`gcode.contract.json` and asserted by the drift tests.

### Query surfaces

- `contract`, `index` — project and index metadata
- `search`, `search-symbol`, `search-text`, `search-content` — ranked results
  (`project_id, total, offset, limit, results[]`, each hit carrying `id, name,
  qualified_name, kind, language, file_path, line_start, line_end, signature,
  score`)
- `grep` — exact pattern matches with spans
- `outline` — `id, name, kind, line_start, line_end, signature` per symbol
- `symbol` — a stored symbol record plus the on-disk `source` snippet
- `symbol-at` — same as `symbol`, plus a `lookup` block describing how the
  location resolved
- `symbols` — the stored symbol record (no `source`)
- `tree` — `file_path, language, symbol_count` per file
- `callers`, `usages` — graph reads (the `graph_read_keys` envelope)
- `imports`, `blast-radius` — the paged graph envelope (`project_id, total,
  offset, limit, results[]`, each row carrying `id, name, file_path, line,
  confidence, relation, distance, metadata, hint`)
- `codewiki` — a generation run-summary, or under `--repair-citations` the
  citation-repair summary

Stored symbol records carry the AI `summary`, never the raw `docstring`.

### Citation repair

`gcode codewiki --repair-citations` re-anchors existing pages' `[file:line]`
citations against the current index and rewrites only the pages whose citations
changed. It runs no generation and makes no AI/LLM calls. The JSON summary keys
are `pages_scanned`, `pages_repaired`, `citations_repaired`, and
`citations_unresolved`.

## Scope

`--project <ROOT>` selects a project root. Without `--project`, gcode detects the
project from the current working directory. JSON output consumed by Gobby must
identify the resolved project with `project_id` and, where path context matters,
`project_root`.

## Format

Use `--format json` for daemon calls. Text output is for humans and is not a
stable integration surface.

## Drift Checks

Both the CLI and daemon tests load this contract. New daemon-facing flags or JSON
keys should update this document, the JSON contract, and the corresponding drift
tests in the same change.
