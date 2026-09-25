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

Send exactly one schema v1 object through `--request-json`. Omit `binding`;
it resolves from `--project` or the current directory.

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

Source bytes come from the working tree and must match the indexed content
hash, so indexed uncommitted edits are citable; a mismatch fails as
`stale_range` or `fact_mismatch`. The binding's `commit_oid` and `tree_oid`
record HEAD as provenance and do not pin the bytes to that commit.
`commit_metadata` reads Git. Community items orient; cite `read` items from
their members. `--allow-stale` is rejected.

Read `complete`, `completeness`, `bounds`, and `warnings` before claiming
coverage: a truncated or empty result does not prove absence. Pass an opaque
`continuation` back with the same request to page. Errors exit 2 with one JSON
object carrying `error` and `recovery`; follow the recovery text. MCP callers
get the same four operations through `gobby-ask` `evidence`; see
[ask](ask.md) for Ask runs and worker admission.

Contract: [gcode evidence](../../../../../../../../docs/contracts/gcode-cli.md#deterministic-evidence).

_Last verified: 2026-09-24_
