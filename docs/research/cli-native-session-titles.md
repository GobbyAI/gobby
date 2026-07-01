# CLI-Native Session Titles

Status: research artifact for #17417
Researched: 2026-06-30
Owner: Gobby session #7758

## Decision Rule

Use a stable, concise, CLI-generated session title as primary and keep Gobby title
synthesis as fallback. Keep Gobby synthesis when the native value is absent,
unavailable to Gobby, late without clean provenance, tool-level rather than
session-level, or low quality.

This spike makes no public API or schema changes.

## Summary

Claude is ready to treat native titles as authoritative after normalization.
Droid already exposes a native field, but source quality is mixed, so suppression
should stay conditional on passing `normalize_native_title()`. Codex, Grok
transcripts, Qwen typed transcripts, and AGY do not currently expose usable
session-title fields. ACP `session/list.title` is the right path for ACP-backed
Qwen/Grok sessions when a provider actually advertises list support and the title
is known to come from the provider, but Gobby currently records refreshed ACP
titles as `manual`, which is the wrong provenance.

Current repo behavior already has the right shared ingestion seam:
`ProcessorTranscriptMixin._extract_native_titles()` strips
`content_type="session_title"` metadata before rendering and calls
`update_title(..., title_source="native")`. The policy gap is downstream:
`_should_update_digest_title()` still allows `title_source="native"` to be
replaced by `title_source="llm"`.

## Current Gobby Behavior

- Transcript native titles:
  `src/gobby/sessions/processor_transcripts.py` extracts parsed
  `session_title` messages, normalizes them, updates the session with
  `title_source="native"`, and removes those messages from render/stat paths.
- Native-title validation:
  `src/gobby/memory/title_heuristics.py::normalize_native_title()` rejects empty
  strings, Droid's `"New Session"` placeholder, multiline values, values over
  200 raw chars, known response-dump markers, and template placeholders.
- Digest title ownership:
  `src/gobby/memory/digest.py::_should_update_digest_title()` currently returns
  true for `title_source in {"heuristic", "llm", "provisional", "native"}`.
  That means trusted native titles can still be overwritten by LLM digest
  synthesis.
- Native replacement policy:
  `_can_replace_with_native_title()` allows native titles to replace empty,
  provisional, heuristic, or prior native titles, but not manual or LLM titles.
- ACP mapping:
  `src/gobby/sessions/acp_session_mapping.py::map_session_info()` maps
  ACP `SessionInfo.title` to canonical `MappedSessionInfo.title`.
  `src/gobby/sessions/acp_lifecycle.py::_upsert()` refreshes only provisional
  existing titles, but uses `_ACP_TITLE_SOURCE = "manual"`.

## Matrix

| CLI source | Native title field/protocol | Transcript or session-list availability | Current Gobby consumption path | Title quality | Evidence | Suppress Gobby synthesis? | Recommendation |
| --- | --- | --- | --- | --- | --- | --- | --- |
| `claude` | JSONL record `{ "type": "ai-title", "aiTitle": "..." }` | JSONL transcripts under the Claude parser path | `ClaudeTranscriptParser` emits `content_type="session_title"`; processor stores normalized title as `title_source="native"`; latest title message wins | Good: concise CLI-generated title; empty values are dropped | `src/gobby/sessions/transcripts/claude.py`; `tests/sessions/test_transcript_parsers.py::TestClaudeRecordEnvelopes`; `tests/sessions/test_sessions_processor_unit.py::TestExtractNativeTitles` | Yes | Treat accepted Claude `aiTitle` as primary. Keep synthesis only when no usable native title exists. Implement #17454 so digest synthesis does not overwrite trusted native titles. |
| `codex` | None found in current transcript parser contract | JSONL transcripts are available and parsed from Codex session files | `CodexTranscriptParser` parses `response_item` conversation content and token-count events; `session_meta` is a boundary, not a title source | N/A | `src/gobby/sessions/transcripts/codex.py`; `src/gobby/sessions/transcript_paths.py`; `src/gobby/sessions/transcript_source.py` | No | Keep Gobby synthesis. Add a parser task only if Codex introduces a stable session-title field or list protocol. |
| `droid` | JSONL `session_start.sessionTitle` | JSONL transcripts under Factory Droid session path | `DroidTranscriptParser` emits `content_type="session_title"`; processor stores normalized accepted values as `title_source="native"` | Mixed: can be concise, but documented as sometimes being a long assistant response dump; placeholder `"New Session"` is rejected by normalization | `src/gobby/sessions/transcripts/droid.py`; `src/gobby/memory/title_heuristics.py`; `tests/sessions/transcripts/test_droid_parser.py`; `tests/sessions/test_sessions_processor_unit.py::test_rejects_garbage_native_title` | No | Do not suppress synthesis source-wide. Skip synthesis only for an accepted normalized Droid title; keep synthesis for missing, placeholder, multiline, overlong, or response-dump values. |
| `qwen` | Typed transcript: none. ACP: optional `session/list` `SessionInfo.title` | Typed JSON/JSONL transcripts are parsed. ACP lifecycle supports `session/list` when advertised, but current qwen 0.15.6 real fixtures advertise `sessionCapabilities: {}`; session-list fixture is synthetic | Transcript parser has no title path. ACP mapping carries `title`, but lifecycle uses `manual` provenance for provisional refreshes | Transcript: N/A. ACP title quality should be provider-native when list is real, but current availability is gated | `src/gobby/sessions/transcripts/qwen.py`; `src/gobby/sessions/transcripts/typed_json.py`; `src/gobby/adapters/acp_session_state.py`; `tests/fixtures/acp_contract/README.md`; `tests/fixtures/acp_contract/qwen-0.15.6-session-list.stdout.jsonl`; `src/gobby/sessions/acp_lifecycle.py` | No | Keep synthesis for Qwen transcripts today. For ACP-backed Qwen sessions, use `SessionInfo.title` as native only when list support/title provenance is real; implement #17455. |
| `grok` | Transcript `update.title` exists only for tool calls; no session-title field found | Grok `updates.jsonl` transcripts are parsed. ACP web-chat exists, but no current evidence of real `session/list.title` availability | `GrokTranscriptParser` uses `update.title` as tool-call name metadata, not a session title. ACP title mapping would work only if provider list support supplies a session title | Tool-level title is unsuitable for session naming; ACP title quality unproven in current fixtures | `src/gobby/sessions/transcripts/grok.py`; `tests/sessions/transcripts/test_grok_parser.py`; `tests/fixtures/acp_contract/grok-0.1.216-session-load-tool-prompt.stdout.jsonl`; ACP mapping files above | No | Keep synthesis for Grok transcripts today. If Grok later exposes `session/list.title`, route it through #17455 with native provenance; do not consume tool-call `title`. |
| `agy` | None found | Current Gobby integration is hook-only; no parser registry entry and no ACP server path | No title consumption path | N/A | `docs/research/cli-integration-matrix.md`; `src/gobby/adapters/agy.py`; `src/gobby/adapters/capabilities.py`; no `SessionSource.GEMINI` hits in current source | No | Keep Gobby synthesis. Treat Gemini as historical AGY/Antigravity context, not a separate current source, unless new evidence shows a supported transcript or ACP surface. |

## ACP Title Provenance

ACP `session/list.title` should be considered native only at the registration or
update point where Gobby knows the value came from `SessionInfo.title`. Using
`manual` for provider titles conflates agent-generated names with user renames
and blocks later policy decisions. The follow-up should:

- use existing `title_source="native"` for provider-generated ACP titles;
- keep `manual` exclusively for user title edits;
- preserve the current guard that existing non-provisional titles are not
  clobbered by discovery;
- keep ACP title use gated on actual lifecycle/list support and provider title
  provenance.

## Digest Policy

Trusted native titles should prevent digest title synthesis from overwriting the
session title. Gobby can still build and persist digest content; it should skip
the title update when the current title is a trusted native title. This avoids
duplicating work already done by the CLI while preserving Gobby fallback
synthesis for sources without a native title.

Implementation should use the existing `title_source="native"` value rather than
adding a new public title source. If a source needs finer trust handling, add
internal policy around source/provenance, not a user-visible schema change.

## Follow-Up Tasks

- #17454 Completed: digest synthesis preserves trusted native session titles.
- #17455 Completed: ACP session-list titles are recorded with native provenance.

No parser implementation task is recommended now. Claude and Droid parser
support already exists; Codex, Qwen typed transcripts, Grok transcripts, and AGY
do not expose usable session-title fields in current repo evidence.

## Validation Plan

Focused baseline checks for the existing parser and native-title behavior:

```bash
GOBBY_TEST_PROTECT=1 uv run pytest tests/sessions/test_sessions_processor_unit.py::TestExtractNativeTitles tests/sessions/test_transcript_parsers.py::TestClaudeRecordEnvelopes tests/sessions/transcripts/test_droid_parser.py tests/memory/test_digest.py::TestNormalizeNativeTitle -q
```

Focused ACP checks for mapping and lifecycle title claims:

```bash
GOBBY_TEST_PROTECT=1 uv run pytest tests/sessions/test_acp_session_mapping.py tests/sessions/test_acp_lifecycle_service.py -q
```
