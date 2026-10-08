# Native evidence

Load when a finding, review, task description, or report cites source lines,
callers, or commits, or when a claim about code must be checked before it is
written down. `gcode evidence` returns hash-verified JSON citations with no
agent or model in the loop; `gcode evidence --help` prints example requests.

Use navigation commands (`gcode search`, `outline`, `symbol-at`) to find code.
Use evidence to cite it: each source item carries `path`, line and byte bounds,
`content_hash`, `excerpt_hash`, and a `numbered_excerpt` whose `N| ` prefixes
give exact line numbers. Quote those lines and hashes; never retype a line
number from memory or invent a hash or ID.

Send exactly one schema v1 object through `--request-json`. Omit `binding` for
working-tree reads; it resolves from `--project` or the current directory.
An explicit `binding` pins source bytes to that commit's Git blobs. Bound range
reads can retrieve paths absent from the current checkout or index.

Recover the current caller identity with this unbound metadata read (it does
not depend on a source path existing in the current checkout):

```bash
gcode evidence --request-json '{"schema_version":1,"operation":"read","read":{"kind":"commit_metadata"}}'
```

Bound requests require all three fields inside `binding`:

| Field | Value |
| --- | --- |
| `project_id` | `response.binding.project_id` from an unbound request in the same checkout |
| `commit_oid` | Full commit OID from `git rev-parse <commit>` |
| `tree_oid` | Tree OID from `git rev-parse '<commit>^{tree}'` |

Replace the uppercase placeholders in this complete example:

```bash
gcode evidence --request-json '{"schema_version":1,"operation":"read","binding":{"project_id":"PROJECT_ID","commit_oid":"COMMIT_OID","tree_oid":"TREE_OID"},"read":{"kind":"range","path":"src/lib.rs","start_line":1,"end_line":1}}'
```

Use the current persisted caller identity; task project IDs or a binding copied
from another checkout may differ. On `repository_binding_mismatch`, run an
unbound request in the intended checkout to recover `binding.project_id`, then
pin the desired commit and its tree. Keep all fields inside `binding`; a
top-level `project_id` or commit-only binding is rejected.

| Operation | Selector |
| --- | --- |
| `search` | `lane` (`symbol`, `lexical_symbol`, `literal`, `regex`, `content`, `hybrid`), `query`, optional `paths` (file or directory scopes), `language`, `kind`, `limit` |
| `read` | `kind`: `range` (`path`, `start_line`, `end_line`), `symbol` (`path`, `qualified_name`), or `commit_metadata` (optional `commit_oid`) |
| `graph` | `query`: `callers`, `callees`, `usages`, `imports`, `scoped_view` with a `source` entity, or `directed_path` with `source` and `target`; entities are `symbol_id`, `symbol`, or `path` |
| `communities` | optional `community_id`, `label`, or `path`, plus `min_size`, `limit`, `max_members` |

```bash
gcode evidence --request-json '{"schema_version":1,"operation":"read","read":{"kind":"symbol","path":"src/gobby/workflows/safe_evaluator.py","qualified_name":"_assistant_response_text"}}'
gcode evidence --request-json '{"schema_version":1,"operation":"graph","graph":{"query":"callers","source":{"kind":"symbol","path":"src/gobby/workflows/safe_evaluator.py","qualified_name":"_assistant_response_text"}}}'
```

Unbound source bytes come from the working tree and must match the indexed content
hash, so indexed uncommitted edits are citable; a mismatch fails as
`stale_range` or `fact_mismatch`. The binding's `commit_oid` and `tree_oid`
record HEAD as provenance only when `binding` is omitted from the request.
`commit_metadata` reads Git. Community items orient; cite `read` items from
their members. `--allow-stale` is rejected.

Read `complete`, `completeness`, `bounds`, and `warnings` before claiming
coverage: a truncated or empty result does not prove absence. Pass an opaque
`continuation` back with the same request to page. Errors exit 2 with one JSON
object carrying `error` and `recovery`; follow the recovery text.

## When to cite

Cite through `gcode evidence` by default whenever a review finding, plan
finding, close verdict, or task description claims what specific code does:
give the `path`, the quoted `N| ` lines the claim rests on, and the item's
`excerpt_hash`. A claim that rests on no particular source needs no citation:
design judgment, a missing requirement, scope, proportionality, or style.

When evidence cannot serve the claim, say why in the claim and fall back:

- Unindexed or non-code source, or a request failing with `stale_range`,
  `fact_mismatch`, or an outage error: read the file directly and cite
  `path:start-end`.
- Behavior shown by a test or a run: cite the exact command and its output.

Contract: [gcode evidence](../../../../../../../../docs/contracts/gcode-cli.md#deterministic-evidence).

_Last verified: 2026-10-02_
