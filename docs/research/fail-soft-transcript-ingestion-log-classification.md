# Fail-soft transcript ingestion log classification

Captured for task #17402 on 2026-06-27 from
`~/.gobby/logs/*-parser-error.log*`, including active files and rotations.

## Summary

Total parser-error log size is 59M. Codex accounts for nearly all of that:
`codex-parser-error.log` plus rotations total about 57M.

| Bucket | Count | Notes |
| --- | ---: | --- |
| parser-unknown | 17,427 | Codex `response_item/tool_search_*` and Droid `todo_state` |
| renderer-unknown | 2,420 | Orphan `tool_result`; no `mcp_tool_result` found in current logs |
| malformed-probe | 353 | Literal invalid/non-JSON probes and non-object JSON |
| other-unknown | 103 | Claude `random_type`, likely test-generated |
| truncated-JSON | 2 | Claude partial JSON records with unterminated strings |

## Provider buckets

| Provider | Bucket | Count |
| --- | --- | ---: |
| codex | parser-unknown | 17,197 |
| codex | malformed-probe | 36 |
| droid | parser-unknown | 230 |
| claude | renderer-unknown | 1,448 |
| claude | malformed-probe | 217 |
| claude | other-unknown | 103 |
| claude | truncated-JSON | 2 |
| grok | renderer-unknown | 972 |
| gemini | malformed-probe | 96 |
| cursor | malformed-probe | 3 |
| qwen | malformed-probe | 1 |

## Top details

| Provider | Bucket | Detail | Count |
| --- | --- | --- | ---: |
| codex | parser-unknown | `response_item/tool_search_output` | 8,599 |
| codex | parser-unknown | `response_item/tool_search_call` | 8,598 |
| claude | renderer-unknown | `tool_result` | 1,448 |
| grok | renderer-unknown | `tool_result` | 972 |
| droid | parser-unknown | `todo_state` | 230 |
| claude | malformed-probe | `Expecting value` | 216 |
| claude | other-unknown | `random_type` | 103 |
| gemini | malformed-probe | `Expecting value` | 96 |
| codex | malformed-probe | `Expecting value` | 18 |
| codex | malformed-probe | `Line is not a JSON object` | 18 |

## Fixture payload findings

Machine-readable samples live in
`tests/sessions/transcripts/fixtures/fail_soft_unknown_payloads.json`.

Codex `tool_search_call` current shape, from Codex session #7689:

- `payload.type == "tool_search_call"`
- Current records have both `payload.id` and `payload.call_id`
- Older parser-error samples only have `payload.call_id`
- Query lives at `payload.arguments.query`
- Limit lives at `payload.arguments.limit`
- No `action` field was present in captured samples

Codex `tool_search_output` current shape, from Codex session #7689:

- `payload.type == "tool_search_output"`
- `payload.call_id` is present
- No `payload.id` is present
- Output lives at `payload.tools`
- No top-level `payload.output` field was present in captured samples

Droid `todo_state` current shape, from Droid session #6968:

- `record.type == "todo_state"`
- `record.id` is present and stable across repeated snapshots
- Captured `record.todos` is an object wrapper, not a list:
  `{"todos": "<newline-delimited status text>"}`
- This differs from the plan's assumed `todos: [{content,status}]` shape, so
  downstream Droid parsing should support the observed wrapper/string form and
  only add array handling if a real transcript provides that shape.

## Implications for downstream tasks

- T5 should derive Codex tool-search IDs from `payload.id` when present, then
  `payload.call_id`; outputs pair by `payload.call_id`.
- T5 should map tool-search input from `payload.arguments`, especially
  `arguments.query`, not from `function_call` fields.
- T5 should preserve `payload.tools` as the normalized result payload for
  `tool_search_output`.
- T6 should not assume Droid todos are already structured arrays. The current
  transcript evidence is a text snapshot wrapped in `todos.todos`.
- T2's renderer orphan fallback should target `tool_result` first; the current
  logs did not contain `mcp_tool_result`.
