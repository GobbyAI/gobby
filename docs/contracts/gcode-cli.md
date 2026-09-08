# gcode CLI Contract

The machine-readable contract lives at `crates/gcode/contract/gcode.contract.json`.
`gcode contract --format json` must emit the same contract version and contents.

## Version

`contract_version`: 9

Version 9 adds `evidence --request-json`, a JSON-only, exact-commit read surface
for deterministic source citations, commit metadata, indexed search, and graph
facts. It returns the evidence schema v1 response unchanged from the Rust
evidence library and exposes every library failure as a typed exit-2 error.

Version 8 adds `schema-identity --json`, which prints the schema identity embedded
in the binary (`baseline_version`, `latest_version`, `baseline_checksum`,
`latest_checksum`, `assets_root_hash`, `runner_protocol`) — the same JSON
`gdaemon schema version --json` prints — so the installer can prove that every
native binary in `~/.gobby/bin` was built from one schema.

Version 7 gives public path inputs explicit scope semantics, adds repeatable
`tree [PATH]...` filters, and replaces the ambiguous graph-view positional seed
with typed `--file`, `--module`, and `--symbol` selectors. Each command below
emits a stable, whitespace-compact JSON shape under `--format json`; the keys
are pinned in `gcode.contract.json` and asserted by drift tests.

### Query surfaces

- `contract`, `index` — project and index metadata
- `search` — paged hybrid symbol results from symbol BM25, semantic symbol vectors,
  and graph lanes; content chunks are never merged into its ranking or pagination
- `search-symbol`, `search-text` — paged symbol results for exact-first and BM25
  symbol-metadata lookup
- `search-content` — paged BM25 results over repository content chunks

All four ranked query surfaces use the `project_id, total, offset, limit,
next_offset, budget_exceeded, results[]` envelope. Each hit carries `id, name,
qualified_name, kind, language, file_path, line_start, line_end, signature,
score`.
- `grep` — paged exact pattern matches with intact spans and context blocks
- `outline` — AST-only paged top-level subtrees for parser-backed source files,
  containing `id, name, kind, line_start, line_end, signature` symbols. Markdown
  and other content-only files return success with no symbols and a stderr
  redirect; use `gcode grep '^#{1,6} ' <FILE> -m 200` for Markdown headings
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

## Deterministic Evidence

Invoke the machine surface as:

```text
gcode --project <ROOT> evidence --request-json '<EVIDENCE_REQUEST_JSON>'
```

`--request-json` is required and accepts exactly one evidence schema v1 object.
Unknown fields, operation tags, selector tags, and enum values are rejected as
`invalid_evidence_request`. Output is always compact JSON; explicit
`--format text` returns `unsupported_evidence_format`. `--allow-stale` is
explicitly forbidden and returns `stale_admission_bypass_forbidden`—evidence
never weakens exact-snapshot admission.

Every request contains `schema_version: 1`, a complete `binding`, one flattened
operation, `max_bytes` (default 16384), and an optional opaque `continuation`.
The binding contains `project_id`, exact `commit_oid`, `tree_oid`,
`inventory_digest`, and commit provenance: parent OIDs, comparison parent/kind,
changed-path digest, and complete changed-path records. The CLI resolves the
managed checkout selected by global `--project` (or cwd), prepares that exact
commit from local Git objects, and compares the whole supplied binding to the
prepared snapshot. A mismatch never falls back to HEAD or another project.

Operations and selector semantics are:

- `{"operation":"read","read":...}`: `range` uses a safe tracked path and
  one-based inclusive `start_line`/`end_line`; `symbol` requires one exact
  `path` plus `qualified_name` indexed at the snapshot hash; `commit_metadata`
  emits one record per changed path (or one empty-change record).
- `{"operation":"search","search":...}`: lanes are `symbol` (exact name or
  qualified name), `lexical_symbol`, `literal`, `regex`, `content`, and
  `hybrid`. `paths` are safe snapshot-relative scopes; `kind` is valid only for
  symbol lanes. Empty results carry
  `search_absence_not_repository_negative`; they are not represented as proof
  of a repository-wide negative.
- `{"operation":"graph","graph":...}`: queries are `callers`, `callees`,
  `usages`, `imports`, `directed_path`, and `scoped_view`, with typed source and
  target selectors, bounded `depth`, relations, direction, and result limit.
  Missing or unavailable graph state is an explicit `graph_unavailable` error.

The response echoes the canonical request (with continuation removed), its
fingerprint, the verified snapshot binding, contract identity, whole evidence
items, completeness state, applied bounds, exclusions, warnings, and an
optional continuation. Source citations include exact blob/content/excerpt
hashes plus line and byte bounds. Commit metadata is derived from the bound Git
commit. Graph evidence carries a hash-verified source citation, owner content
hash, endpoints, direction/relation, and `extracted`, `inferred`, or
`unresolved` provenance. Indexed facts only locate evidence: source bytes are
read from the bound Git blob and symbol ranges/hashes are reverified before
return.

Pagination sorts and deduplicates semantic items, then admits the largest whole
prefix whose sum of serialized item bytes fits `max_bytes`. It never slices an
item. If any remaining item is itself too large, the request fails with
`narrowing_required` and returns no partial success. A continuation is bound to
the canonical request fingerprint; changing the binding, selector, limits, or
byte budget returns `continuation_mismatch`. `complete`, `completeness`,
`returned_items`, `total_items`, `serialized_item_bytes`, index truncation, and
graph traversal truncation describe the actual page and upstream bounds.

Evidence reads never invoke agent/model orchestration, update source files,
write facts, autoindex, or retry against stale data. Hybrid selectors require a
complete semantic identity. The CLI currently installs no audited semantic
provider, so a valid hybrid request fails explicitly with `semantic_failure`
rather than falling back to lexical results; missing or malformed identity is
`semantic_identity_required`, and any future provider must match it exactly.

All evidence contract errors exit `2`, write one JSON object to stderr, and
leave stdout empty. Codes include `snapshot_binding_mismatch`,
`missing_git_object`, `invalid_object_id`, `invalid_selector`, `unsafe_path`,
`path_not_tracked`, `fact_snapshot_mismatch`, `index_incomplete`,
`index_unavailable`, `graph_unavailable`, `semantic_failure`,
`semantic_identity_mismatch`, `continuation_mismatch`, `narrowing_required`,
and `unsupported_schema`. Recovery text tells callers whether to correct the
request, restore a managed store, fetch the exact Git object, or repair index
facts; the CLI performs none of those mutations automatically.

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

Search diagnostics do not change result ordering, pagination, keys, or exit
codes. Snake_case inputs redirect to shell-safe `search-symbol` and `grep -w`
commands; literal-like inputs redirect to `grep -F`; empty symbol results and
content-only paths redirect to `search-content`. Text diagnostics go to stderr
and honor `--quiet`; JSON keeps the redirect in the existing `hint` field.

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
