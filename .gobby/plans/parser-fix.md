A previous agent produced the plan below to accomplish the user's task. Implement the plan in a fresh context. Treat
the plan as the source of user intent, re-read files as needed, and carry the work through implementation and
verification.

# Normalize Transcript Records Before Rendering

## Summary
- Root cause: Grok `hook_execution` records are parsed as synthetic `tool_result` messages with no matching `tool_use`,
so `transcript_renderer` emits `unknown` blocks and the UI shows `Unknown block: tool_result`.
- Current logs show three real parser gaps: Grok `hook_execution -> tool_result`, Codex `response_item/
tool_search_call|output`, and Droid `todo_state`.
- Fix by adding a shared transcript normalization pass after provider parsing and before rendering/indexing/stats, so
provider dialect noise never reaches React.

## Key Changes
- Add `src/gobby/sessions/transcript_normalization.py` with a single entry point such as
`normalize_transcript_records(records, source)`.
- Apply it in `_parse_lines`, JSON-session parsing, `TranscriptReader` window/native paths, and lifecycle transcript
processing before `render_transcript`, `render_window`, stats, and index building consume records.
- Grok: suppress successful `hook_execution` records with no output; render non-success/output-bearing hook executions
as `system` text feedback.
- Codex: recognize `tool_search_call` and `tool_search_output`; either map them to canonical `tool_use/tool_result`
pairs or suppress them as internal discovery metadata. Default: suppress successful client-side tool-search metadata.
- Droid: ignore top-level `todo_state` records as provider state metadata.
- Keep `transcript_renderer` as the canonical renderer for already-normalized `ParsedMessage` records; do not add a
React workaround for `UnknownBlockCard`.

## Interfaces
- No public API shape changes for session message responses.
- Internal contract becomes: parser output may be provider-shaped, but anything passed to render/index/stats is
canonical `ParsedMessage` or intentionally ignored.
- Unknown-block logging remains only for genuinely unsupported provider content after normalization.

## Test Plan
- Add focused unit tests for Grok hook execution:
  - successful hook records produce no rendered blocks;
  - failed/output-bearing hook records render as system text, not `unknown`.
- Add Codex parser/normalizer tests for `tool_search_call` and `tool_search_output` producing no parser-error log
entries.
- Add Droid test for `todo_state` producing no parser-error log entry.
- Add regression test that the Grok sample from `grok-parser-error.log` no longer renders `content_blocks[].type ==
"unknown"`.
- Run focused validation only:
  - `GOBBY_TEST_PROTECT=1 uv run pytest tests/sessions/transcripts/test_grok_parser.py tests/sessions/
test_transcript_renderer.py tests/sessions/test_transcript_window.py -v`
  - add Codex/Droid focused files once tests are added.

## Assumptions
- Successful hook execution metadata is internal and should be hidden from the user transcript.
- Hook failures or hook output are user-visible system feedback.
- The fix should avoid expanding `transcript_index.py`, which is already 979 lines; keep new logic in a small
normalization module.
