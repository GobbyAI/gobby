# gcode CLI Contract

The machine-readable contract lives at `crates/gcode/contract/gcode.contract.json`.
`gcode contract --format json` must emit the same contract version and contents.

## Version

`contract_version`: 5

Version 5 pins exit codes, adds `callees`, and adds `graph view`. Each command below emits
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
- `callers`, `callees`, `usages` — graph reads (the `graph_read_keys` envelope).
  `callees` is caller-parity `limit`/`offset` only; it has no output-clip
  `--token-budget`
- `graph view` — scoped `fcg` / `mcg` / `class-hierarchy` dump. JSON keys:
  `project_id, project_root, view, seed, depth, incoming_truncated,
  outgoing_truncated, hint, nodes, edges, communities, mermaid`. Mermaid is
  always present and is never character/token-sliced
- `imports`, `blast-radius` — the paged graph envelope (`project_id, total,
  offset, limit, results[]`, each row carrying `id, name, file_path, line,
  confidence, relation, distance, metadata, hint`)

Stored symbol records carry the AI `summary`, never the raw `docstring`.

## Scope

`--project <ROOT>` selects a project root. Without `--project`, gcode detects the
project from the current working directory. JSON output consumed by Gobby must
identify the resolved project with `project_id` and, where path context matters,
`project_root`.

## Format

Use `--format json` for daemon calls. Text output is for humans and is not a
stable integration surface.

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
