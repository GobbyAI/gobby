# Compact Summary Fidelity

**Plan ID:** compact-summary-fidelity

## Overview
`kind: framing`

Session #10854 (Grok, goal mode, 185 tool calls in one 22-minute turn) compacted and
received a handoff summary stating "No file changes, commits, test results, or unresolved
tool errors are recorded" — after it had claimed and closed #20544 and written tests.
Claude session #10849 (98 tool calls) carries the same disease: its digest Turn 11 reads
"No tools were used". Investigation in session #10860 found four independent defects:

- **A.** Grok discards hook stdout on SessionStart, UserPromptSubmit, PreToolUse(allow),
  PostToolUse, and Stop (`additionalContext`, `systemMessage`, and allow `reason` alike;
  verified by probe task #20633 and Grok docs). The `inject-compact-handoff-on-prompt`
  rule from #20243 therefore fires, wipes `compact_resume_required_skills`, and delivers
  nothing; the agent falls back to `wait_for_summary`.
- **B.** Digest turn records are built from user text plus assistant **text blocks only**
  for every CLI. Tool calls, edits, commands, and MCP calls never reach the
  `memory/turn_record` prompt, which demands exactly those facts, so the model truthfully
  writes "status narration only".
- **C.** `_generate_session_summary_core` reads the transcript only when no digest exists,
  so `TranscriptAnalyzer` sees `[]`, and the `handoff/session_end` prompt's "Ground
  Truth" section (files changed, git status, diff, commits) is blank. The model then
  asserts "no files were modified, no commits" as fact.
- **D.** The compaction summary can be generated before the turn-end digest lands
  (`session_summary_revisions` for #10854: 18:03:43 `full` at digest count 1, then
  18:04:54 `delta` at count 2 — after the continuation had already read the stale one).

Outcome: compaction summaries and handoffs name the files edited, commands run, MCP and
task operations, and commits; Grok continuations receive the skill-reload, MCP-ledger,
and task-context blocks through a channel Grok actually reads; the compaction summary
always includes the turn that triggered it.

## Constraints
`kind: framing`

- No backward compatibility (0.5.0 unshipped), except one invariant: persisted
  `last_digested_pair_index` cursors must stay valid, so pair extraction with the ledger
  must return the same message count, role sequence, and `content` strings as without
  it. The ledger is a side field (`tool_activity`) on the turn's user message, never
  message content, and it never creates or fills a message.
- Hand-maintained production files stay under 1,000 lines. `src/gobby/memory/digest.py`
  is at 810 lines, `src/gobby/sessions/transcripts/claude.py` at 746, and
  `src/gobby/sessions/compact_continuation.py` at 952; put new helpers and new prompt
  text in the new modules named below rather than growing those files. Section 2.2 in
  particular keeps its `compact_continuation.py` delta to the `source` plumbing and a
  one-line delegation.
- Ledger budgets: `DIGEST_ACTIVITY_MAX_LINES = 80`, `DIGEST_ACTIVITY_MAX_CHARS = 6000`
  per rendered ledger — one ledger per extracted user-to-user turn, aggregating every
  provider record inside that turn under one cap and one omission count — independent of
  Grok's `_PAIR_RESPONSE_CHAR_BUDGET = 4000` narration cap. Truncation is evidence-aware (1.1): failed calls, edited paths, task
  mutations, commit-producing calls, and the turn's last `DIGEST_ACTIVITY_TAIL_LINES = 10`
  calls survive the caps ahead of everything else.
- Successful tool output is retained only for commit-producing calls (shell `git commit`
  results and `commit_sha` arguments of `close_task`/`link_commit`); no other successful
  result text enters the ledger, the analyzer, or the digest. Success is still
  distinguishable from missing evidence: every parser's result correlation marks each
  entry resolved when any matching result record arrives, so a bare ledger line means
  the call completed successfully, `! failed:` means it failed, and
  `(no result recorded)` means the call was still in flight when the turn ended —
  status-only evidence, never retained output.
- Every rendered ledger field is control-character-escaped (`\n`, `\r`, `\t` and every
  other C0 byte become `\\n`, `\\r`, `\\t`, `\\xNN`) before truncation, and both caps are
  computed on the escaped text, so one tool call is always exactly one physical line.
- Summary ground truth reads at most the trailing `SUMMARY_ANALYZER_MAX_RECORDS = 20_000`
  transcript records per refresh (the refresh re-runs after every digest that passes the
  watermark); facts older than that window come from the digest narrative.
- Digest calls run on the daemon event loop only. `_serialize_session_digest` is an
  `asyncio.Lock` bound to that loop, so the dispatcher's `asyncio.run` fallback thread
  never digests.
- Do not change the `memory/turn_record` prompt's `required_variables`; the ledger rides
  inside `response_text`.
- `_read_last_turn_from_transcript` (feeds `last_turn` display) stays narration-only.
- Claude/Codex `SessionStart(source=compact)` handoff injection (`inject-compact-handoff`)
  is unchanged. Only the Grok-only `turn_start` rule is retired.
- `wait_for_summary`'s existing staleness behaviour from #20393 (`summary_is_stale` →
  `live_handoff_context`) is preserved; the new `continuation` field is additive.
- The Grok-wide context channel (every other `inject_context` effect) is out of scope;
  see the deferred section and task #20635.
- Tests run with `GOBBY_TEST_PROTECT=1 uv run pytest <path>`; never the full suite.

## P1: Digest and summary fidelity
`kind: framing`

**Goal**: Turn records and compaction summaries are built from what the agent actually
did — tool calls, edits, commands, MCP/task operations, commits — for every CLI, and the
summary generated at compaction includes the turn that triggered it.

### 1.1 Add a tool-activity ledger to transcript pair extraction [category: code]
`kind: deliverable`

Targets:
- `src/gobby/sessions/transcripts/tool_activity.py`
- `src/gobby/sessions/transcripts/base.py::*` — scope-reason: the `TranscriptParser.extract_last_messages` protocol gains `include_tool_activity`, and the module gains the shared `TranscriptReadError` exception class that the 1.2 and 1.3 readers raise
- `src/gobby/sessions/transcripts/claude.py::ClaudeTranscriptParser.extract_last_messages`
- `src/gobby/sessions/transcripts/grok.py::GrokTranscriptParser.extract_last_messages`
- `src/gobby/sessions/transcripts/grok.py::_segment_pair_messages`
- `src/gobby/sessions/transcripts/codex.py::*` — scope-reason: `extract_last_messages` gains the flag and its ledger collection consumes the shared `codex_item_activity` pre-scan; `_command_execution_outcomes` delegates its normalization to `codex_items.normalize_command_execution` with byte-identical output; `iter_parse_events` and `parsed_index` semantics stay byte-identical
- `src/gobby/sessions/transcripts/codex_items.py`
- `src/gobby/sessions/transcripts/qwen.py::QwenTranscriptParser.extract_last_messages`
- `src/gobby/sessions/transcripts/droid.py::DroidTranscriptParser.extract_last_messages`
- `tests/sessions/transcripts/test_grok_parser.py::*` — scope-reason: add ledger and invariant cases for the Grok parser
- `tests/sessions/transcripts/test_qwen_transcript_parser.py::*` — scope-reason: add ledger and invariant cases for the Qwen parser
- `tests/sessions/transcripts/test_droid_parser.py::*` — scope-reason: add ledger and invariant cases for the Droid parser
- `tests/sessions/test_transcript_parsers.py::*` — scope-reason: add ledger and invariant cases for the Claude and Codex parsers
- `tests/sessions/transcripts/test_tool_activity.py`
- `tests/sessions/test_transcript_read_error.py`
- `src/gobby/sessions/summary_context.py::*` — scope-reason: consumer-closure entry for the `extract_last_messages` protocol change; `_build_summary_prompt_context` calls it with the flag at its default and this module is otherwise unchanged here
- `src/gobby/sessions/summary_generation.py::*` — scope-reason: consumer-closure entry for the `extract_last_messages` protocol change; `generate_summary` calls it with the flag at its default and this module is otherwise unchanged
- `tests/sessions/transcripts/test_grok_turn_accounting.py::test_audited_shapes_turn_and_usage_accounting`

Every parser's `extract_last_messages` gains a keyword-only flag:

```python
def extract_last_messages(
    self,
    turns: list[dict[str, Any]],
    num_pairs: int = 2,
    *,
    include_tool_activity: bool = False,
) -> list[dict[str, Any]]:
```

Declare the flag on the `TranscriptParser` protocol in `base.py` and implement it in all
five parsers. With the flag off, behaviour is byte-identical to today. With the flag on,
each parser attaches the turn's rendered ledger as an extra key, `tool_activity`, on the
**user** message that opened the turn. `content` strings, message count, and role
sequence are byte-identical with and without the flag; a tool-only turn (no assistant
text) yields exactly what it yields today — for Claude, Codex, Qwen, and Droid a user
message with no assistant reply; for Grok a user message **plus the empty assistant
sentinel** that `_segment_pair_messages` already appends via
`flush(empty_if_pending=True)`, preserved verbatim with the flag on
(`_extract_digest_pairs` keeps normalising that sentinel to an empty response, exactly as
today) — with the ledger riding on that user message, so the compact-triggering turn's
evidence can never migrate into a pair behind the persisted cursor. Tests assert the identity on
every fixture in `tests/sessions/transcripts/fixtures/` (including
`grok_audit/10711/updates.jsonl` and `grok_audit/10725/updates.jsonl`). A turn spans
from one user text message to the next; tool-result records belong to the turn whose
calls they answer.

`base.py` also owns the shared corruption signal. `class TranscriptReadError(ValueError)`
carries `path: Path`, `byte_offset: int` (the file offset of the offending record's
first byte — computable by both readers), and `line_number: int | None` (the 1-based
physical line, supplied by the forward digest reader, which reads the file from its
first byte, and `None` from the reverse tail reader, which never reads the prefix that
holds the newline count), plus a message naming the path, the offset, and the line when
known. It is the single exception both transcript readers raise for a corrupt record —
1.2's `_read_undigested_turns` and 1.3's `_read_transcript_window` — and the only
exception 1.2's `build_turn_and_digest` maps to `error_kind: "transcript_read"`. A record
is corrupt when it is a malformed **interior** JSONL line, or when it decodes to a
non-object JSON value (a scalar or a list) at **any** position, the final line included:
every transcript record is an object, so a complete non-object line is a finished invalid
record, never an in-flight tail. Both readers classify these identically, so the digest
can never refuse bytes that the summary reader silently skips (today
`_read_transcript_window` skips non-dict values with a warning while
`_read_undigested_turns` appends them to the parser input). It lives here because both consumers already import
`transcripts.base` and neither may own a type the other depends on. No parser raises it
and `iter_parse_events` behaviour is unchanged. A malformed **final** line is classified by
its line termination, not by position alone: a JSONL writer emits a complete record and
then its newline, and a raw newline cannot appear inside a JSON string, so a malformed
final line that the file already terminates with `\n` is a finished invalid record —
appending bytes can only start a new record, never complete it — and raises
`TranscriptReadError` in both readers. Only a malformed **unterminated** final fragment
(the file does not end with `\n`) is a possibly in-flight partial write, and it alone
gets the bounded re-read and the withhold below. Both readers have the bit: 1.2's loop
reads the file in binary, and 1.3's reverse reader sees the tail bytes it collects.
Decoding is part of that classification and runs **first**. Both readers hold raw bytes
and decode each line as UTF-8, so a `UnicodeDecodeError` is reached before any JSON
parsing and is classified by the same termination rule: an **unterminated** final
fragment that fails to decode — a multibyte code point split by a partial write — is an
in-flight tail and takes the bounded re-read and the withhold, while a decode failure in
an interior line, or in a final record the file already terminates with `\n`, is durable
corruption and raises `TranscriptReadError` with that record's byte offset. The shared
per-line helper both readers call therefore owns three ordered outcomes — decode, JSON
parse, object shape — and applies the identical termination rule to each, so no decode
failure can reach the digest's generic error mapping instead of
`error_kind: "transcript_read"`, and neither reader can skip bytes the other refuses.

Ledger collection rule per parser:

- **Claude** (`message.content` blocks): collect `tool_use` blocks from assistant records
  and `tool_result` blocks (with `is_error`) from user records between two user text
  records. Tool-only records never become messages.
- **Grok** (`_segment_pair_messages`): within a segment, `tool_call` updates (`title`,
  `rawInput`; unwrap `use_tool{tool_name, tool_input}`) and `tool_call_update` records
  with `status == "failed"` (matched by `toolCallId`) feed the ledger; the ledger lands
  on the segment's user message. The ledger budget is separate from
  `_PAIR_RESPONSE_CHAR_BUDGET`.
- **Codex**, **Qwen**, **Droid**: run `iter_parse_events(raw_lines_from_texts(...))` on a
  **fresh scan parser** — `fresh_scan_parser(self)` from `tool_activity.py`, never
  `self` — over the turn's raw records and consume every item of every
  `ParseEvent.records`. The live instance is never iterated because these parsers
  mutate private incremental state on every event (Codex `_execution_chain` and
  `_pending_tool_search_use_ids`, Qwen `_last_tool_use_id`, Droid
  `_last_assistant_index`), Qwen's and Droid's `snapshot_state` do not cover those
  fields (so snapshot/restore cannot isolate a scan), and `_read_undigested_turns`
  calls `_extract_digest_pairs` twice on one parser (segment, then prefix): an
  observational scan must leave the handed parser byte-identical so its later parse
  behaviour equals a parser that never ran the scan.
  These parsers expand one native record into several normalized blocks, and
  `parse_line` returns only `expanded[0]`, so it is never used here. `ParsedMessage`
  records with `content_type == "tool_use"` open entries and `content_type ==
  "tool_result"` records close them by `tool_use_id`. Codex additionally consumes
  `ParseEvent.codex_exec_outcomes`: the current runtime records shell work as
  `functions.exec`/`exec_command` calls whose inner command sits in `arguments.cmd`
  (`command` for the direct `shell` form) and whose definitive result arrives as a
  `CodexNestedExecOutcome` (`command`, `result`). The outcome's `command` is the ledger
  primary and `result["success"] is False` marks the entry failed with the result's
  error text. Wrapped MCP calls (`mcp__gobby__call_tool`, Qwen/Droid `call_tool`) unwrap
  through `canonical_tool_name`. Qwen keeps today's bare `[Tool call: name]` content with
  the flag on and off.
- **Codex item stream**: the current Codex runtime hides most activity behind
  `custom_tool_call` `exec` records whose arguments are JS orchestration; the real
  activity is recorded as `item_completed` payloads (verified against live 2026-08
  transcripts: 149 `exec` calls, 0 direct MCP calls, 50 `McpToolCall` + 170
  `CommandExecution` items in one session; file edits in editing sessions appear as
  `FileChange` items — `{"type": "FileChange", "id": "exec-…", "changes": {"<abs path>":
  {"type": "update", "unified_diff": …}}}` — never as `apply_patch` commands). Item
  projection lives in a **shared bounded pre-scan**, not in the streaming parser:
  `tool_activity.py` gains `codex_item_activity(turns) -> list[ToolActivityEntry] | None`,
  one pass over the raw window's record dicts that returns entries when the window
  contains any `item_completed` tool item and `None` otherwise. Every returned entry is
  **source-positioned**: `record_index` is the index of the entry's `item_completed`
  record in the window, and both consumers partition entries into user-to-user turns by
  comparing `record_index` against the positions of user text records — item-derived
  activity always lands on the ledger of its originating turn, never pooled
  window-wide. `McpToolCall` items
  (`server`, `tool`, full wrapper `arguments`, `status`, `result`) feed
  `canonical_tool_name` so `{"server": "gobby", "tool": "call_tool", "arguments":
  {"server_name": "gobby-tasks", "tool_name": "close_task", ...}}` renders as
  `mcp gobby-tasks:close_task`. That entry is **failed** when the item reports
  `status == "failed"` **or** when its own `result` reports failure:
  `codex_items.mcp_item_failure(item) -> str | None` unwraps `result` the way the
  streaming parser already unwraps `mcp_tool_call_end` (`{"Err": …}` is a failure,
  `{"Ok": <payload>}` unwraps to its payload, a JSON-string payload is parsed, and a
  `structuredContent`/`content[0].text` envelope goes through the existing
  `hooks._normalization_mcp._unwrap_mcp_tool_output`) and reports failure for an
  `is_error`/`isError` flag or a `success is False` field, returning that failure's text.
  Transport completion and application success are separate signals: the MCP proxy answers
  an application-level failure with `{"success": false, "error": …}` inside a
  transport-successful call, so the structured result is the only thing that separates a
  failed `close_task` from a successful one in the item stream. A completed item with no
  failure signal is a resolved success, and an item carrying no `result` key stays terminal
  item evidence exactly as `FileChange` does. `CommandExecution` items become shell entries through the
  **one canonical normalizer** shared with the streaming parser — new module
  `transcripts/codex_items.py` exposes
  `normalize_command_execution(item) -> CommandExecutionOutcome | None`
  (`NamedTuple(command: str, exit_code: int | None, success: bool | None, output: str)`):
  `command` is `argv[-1]` when `len(argv) >= 3 and argv[-2] in {"-c", "-lc"}`, else
  `shlex.join(argv)`; `exit_code` is the item's integer (non-bool) `exit_code` or `None`;
  `success` is `exit_code == 0` when an integer exit code exists and `None` otherwise;
  `output` is `aggregated_output` or `stdout + stderr`. `codex.py::_command_execution_outcomes`
  is rewritten to call it and keeps byte-identical `CodexNestedExecOutcome` output (it still
  returns `[]` when `exit_code` is not an integer). The pre-scan's shell entry uses the
  normalized `command` as its primary, is failed when `success is False` (regardless of
  `status`), resolved when `success is True`, and unresolved — `(no result recorded)` —
  when `exit_code` is absent unless `status == "failed"`; `FileChange`
  items become one entry per key of `changes` with tool name `apply_patch` and
  `tool_input {"file_path": <path>}` so edited paths reach the ledger and (via 1.3) the
  analyzer's edit set; an `item_completed` edit is terminal evidence with no later result
  record, so each such entry is created with `resolved=True` and renders bare, or as a
  failed entry when the item reports `status == "failed"` — it never renders
  `(no result recorded)`, which is reserved for calls whose result has not landed. A failed entry (nonzero `exit_code`, `status == "failed"` for
  `McpToolCall`/exit-code-less items, or an `McpToolCall` whose structured result reports
  failure) carries the first 160 chars of the output — the structured failure text when
  that is the signal — and
  `commit_outcome` correlates on the item's own result.
  **Item-stream precedence is per call, not per window** — a hybrid or in-flight
  window can hold item projections for some calls and wrapper-only evidence for
  others. A wrapper derivation (a `codex_exec_outcomes` outcome, a direct
  `function_call` `exec` record, or a wrapper MCP `function_call`) is suppressed only
  when a matching item exists **in the same turn partition**. Item ids share no
  identity with wrapper `call_id`s (live 2026-08 rollouts: item ids are fresh
  `exec-<uuid>` values, wrapper ids are `call_…`; zero overlap across 166 wrappers in
  one session), so the join is content identity within the turn: a `CommandExecution`
  item matches an exec wrapper whose nested outcome has the same normalized `command`
  (both sides now come from `normalize_command_execution`, so `[/bin/zsh, -lc, cmd]` and
  the wrapper's `arguments.cmd` agree) **and** the same `success` value, so a failed
  retry never suppresses the successful run of the same command; an `McpToolCall`
  item matches a wrapper MCP call whose decoded `(server_name, tool_name, arguments)`
  equal the item's. Matching is one-to-one in source order: each item consumes at most
  the earliest unconsumed matching wrapper in its turn partition and each wrapper is
  suppressed by at most one item, so two identical commands with one landed item keep
  exactly one execution-chain entry. A wrapper with no matching item in its turn — a split tail whose
  item has not yet landed, or a call the item stream never covered — keeps its
  execution-chain derivation, so no call is dropped and none is double-counted. A
  `None` return (older transcripts, existing fixtures) keeps the execution-chain path
  unchanged for the whole window.
  **A pending wrapper is projected before it is rendered.** Until its output record
  lands, an exec wrapper is visible only as a `tool_use` named `exec` (a
  `custom_tool_call` whose `input` is JS orchestration, which `_parse_tool_payload`
  turns into `{"raw": <js>}`) or `exec_command` (the direct form, `arguments.cmd`), and
  `CodexNestedExecOutcome` — today's only carrier of the inner command — is derived by
  `_resolve_nested_exec_output` from the output record alone. `tool_activity.py`
  therefore gains `pending_exec_command(tool_name, tool_input) -> str | None`, which
  applies the existing fail-closed
  `gobby.adapters.codex_impl.execution_chain.extract_functions_exec_command` (exactly
  one `exec_command(...)` literal, or a `cmd` key) to the wrapper's original arguments
  (`tool_input["raw"]` for the JS form, the mapping itself for the direct form). Both
  consumers — the ledger collection here and the 1.3 adapter — emit an exec wrapper
  whose result never arrives in the window as a canonical shell entry whose primary is
  that command, keyed by the wrapper's `call_id` and positioned at the wrapper's
  `record_index`, rendered `(no result recorded)`; when the helper returns `None`
  (several nested commands, no literal) the entry keeps the raw wrapper name and input
  so nothing is dropped. The projected command joins the per-turn one-to-one match: a
  pending wrapper carries no `success` verdict, so it matches a `CommandExecution` item
  in its turn on normalized command alone (the item's verdict stands), which keeps a
  window cut between `item_completed` and the wrapper's output from rendering the same
  command twice. A wrapper whose output has landed is never projected — its nested
  outcome replaces the `exec` envelope as the wrapper derivation, exactly as for every
  resolved call.
  Because both new consumers — the Codex ledger collection here and the 1.3 analyzer
  adapter — call the same pre-scan over the same window, `iter_parse_events`,
  `parsed_index` assignment, and every existing `ParseEvent` consumer
  (`processor_transcripts`, `transcript_index`, `transcript_reader`,
  `transcript_window`) stay byte-identical: no new ParsedMessages are emitted, no
  lookahead is added to the parser, and resume boundaries are untouched.

Failure mapping per parser: Claude and Droid `tool_result.is_error`; Grok
`tool_call_update.status == "failed"`; Codex nested exec outcome `success is False`, or
for non-exec calls a `function_call_output`/`custom_tool_call_output` whose normalized
`tool_result` reports an error; Qwen `functionResponse` with `toolCallResult.status` in
`{"error", "cancelled"}` or a `response` dict carrying an `error` key, correlated by
`functionResponse.id` or `toolCallResult.callId`. Every failure appends
`! failed: <first 160 chars>` to its entry.

Successful-result correlation runs for every call in every parser: Claude and Droid
match non-error `tool_result` blocks by `tool_use_id`; Grok matches `tool_call_update`
records with `status == "completed"` by `toolCallId`
(`_extract_tool_result(update)["output"]`); Codex matches the nested exec outcome
(`function_call_output` by `call_id` for non-exec calls); Qwen matches
`functionResponse` by `id` or `toolCallResult.callId`. Every match flips the entry's
`resolved` flag. Output text is kept only where `is_commit_producing` is true — it goes
through `commit_outcome` and lands on the entry's `outcome`; output for any other call
is discarded unread at match time. Rendering makes the three outcomes distinguishable:
an entry with `error` carries `! failed: …`; an entry never matched by any result record
in the turn's window carries a trailing `(no result recorded)`; a resolved successful
entry renders bare. The motivating false handoff claimed "no test results" — with this
marking, a `uv run pytest` line with no annotation is authoritative evidence the tests
ran and completed successfully, and an in-flight call can never masquerade as a success.

Ledger format, produced by the new module `tool_activity.py`:

```python
DIGEST_ACTIVITY_MAX_LINES = 80
DIGEST_ACTIVITY_MAX_CHARS = 6000
ACTIVITY_HEADER = "[tool activity]"

DIGEST_ACTIVITY_TAIL_LINES = 10

@dataclass
class ToolActivityEntry:
    tool_name: str
    tool_input: dict[str, Any]
    tool_use_id: str | None = None
    error: str | None = None
    outcome: str | None = None  # commit evidence only; see commit_outcome
    resolved: bool = False  # any matching result record arrived (status only)
    record_index: int = -1  # source position in the raw window; -1 = unpositioned

def fresh_scan_parser(parser: Any) -> Any:
    """``type(parser)(session_id=parser.session_id)``: an unhydrated parser of the same
    class for observational ``iter_parse_events`` scans (the 1.1 ledger collection and
    the 1.3 analyzer adapter). Every registry parser accepts that constructor; Droid's
    ``transcript_path`` is deliberately omitted so a scan never loads a usage sidecar.
    A scan never reads or writes the live parser's private incremental state."""

def canonical_tool_name(tool_name: str | None, tool_input: Any) -> tuple[str, dict[str, Any]]:
    """Unwrap dispatchers and normalise aliases. Total over parser output.

    - ``ParsedMessage.tool_name``/``tool_input`` are nullable and Qwen can emit a
      ``functionCall`` whose ``name`` is null. A missing or non-string name normalises
      to the stable label ``"unknown-tool"``; a non-mapping input becomes ``{}``;
      rendered values are stringified safely. No native record shape may raise out of
      this function (``CallToolWrapperInputError`` is caught internally per the
      fallback below).
    - Grok ``use_tool`` -> ``tool_input["tool_name"]`` / ``tool_input["tool_input"]``.
    - MCP dispatchers (``call_tool``, ``mcp__gobby__call_tool``, ``gobby__call_tool``,
      ``mcp_call_tool``) -> ``"mcp <server_name>:<tool_name>"``. The wrapper is decoded by
      ``gobby.mcp_proxy._call_tool_wrapper.canonicalize_call_tool_wrapper`` (top-level or
      hoisted ``server_name``/``tool_name``, ``arguments`` or ``args``, dict or JSON string,
      nested routing fields) and its ``.arguments`` become the input; on
      ``CallToolWrapperInputError`` the line keeps the raw name with empty input.
    - Shell aliases via ``canonicalize_shell_tool_name``, preceded by the ledger-local
      alias table ``_LEDGER_SHELL_ALIASES = {"run_terminal_command": "Bash"}`` in this
      module; ACP names via ``normalize_acp_tool_name``. The global ``_SHELL_TOOLS`` set
      is deliberately unchanged: ``GrokAdapter.TOOL_MAP`` already maps the hook-side
      ``run_terminal_command`` to ``Bash`` before any hook consumer sees it, and the raw
      name is also emitted by ``acp_client_requests`` into pre-tool checks and stream
      events for every ACP client, so a global alias would change plan-mode shell
      blocking, approvals, progress and effect classification, analyzer output, and
      commit observation outside this plan. Only the raw-transcript ledger needs the
      alias, so only the ledger carries it.
    """

def commit_outcome(tool_name: str, tool_input: dict[str, Any], output: str | None) -> str | None:
    """Successful-result evidence, kept only for commit-producing calls.

    Canonical shell tool whose ``command`` is commit-producing per ``is_commit_producing``:
    parse ``[<branch> <sha>] <subject>`` from ``output`` and return ``commit <sha> <subject>``
    (subject capped at 80 chars). Canonical ``mcp gobby-tasks:close_task`` or
    ``mcp gobby-tasks:link_commit`` with ``commit_sha`` in its arguments: return
    ``commit <sha>`` (no output needed). Every other call returns ``None``; parsers
    never retain successful output for anything else.
    """

def is_commit_producing(tool_name: str, tool_input: dict[str, Any]) -> bool:
    """True when ``commit_outcome`` could be non-None. Parsers call this before
    looking up a call's successful result at all.

    A canonical shell call is commit-producing only when
    ``parse_git_commit_invocations(_without_shell_comments(tool_input["command"]))``
    returns at least one invocation, where ``_without_shell_comments`` is
    ``shlex.join(shlex.split(command, comments=True))`` and returns the raw command
    unchanged on ``ValueError`` (unbalanced quotes) — the repository's token-aware classifier in
    ``workflows/commit_guard.py`` (``shlex`` tokens, a ``git`` token with its global
    options skipped, the ``commit`` subcommand, shell control-token segmentation),
    imported inside the function because that module loads hook and workflow-state
    code the transcript parsers must not import at module scope. Substring matching
    is never used: ``echo "git commit"``, ``grep -n "git commit" notes.md``, and
    ``gcode grep -F "git commit" src`` are not commit-producing, while
    ``git -C /repo commit -m msg`` and ``cd x && git commit -am msg`` are. The comment
    strip is what keeps ``ls # git commit`` out: ``shlex.split`` leaves ``#`` as an
    ordinary token by default, so the shared classifier — correctly fail-safe for the
    commit guard, fail-open for output retention — would otherwise admit that command's
    stdout and read a forged ``[<branch> <sha>] <subject>`` line out of it. A ``git
    commit`` in an unreachable segment (``false && git commit -m x``) stays classified;
    that is deliberate, because a segment that never executes emits no commit-shaped
    stdout and shell reachability analysis buys nothing here. The task-tool branch
    (``close_task``/``link_commit`` with ``commit_sha``) is unchanged."""

def escape_ledger_text(value: str) -> str:
    """Escape ``\\n``, ``\\r``, ``\\t`` and every other C0 control byte as ``\\\\n``,
    ``\\\\r``, ``\\\\t``, ``\\\\xNN``. Applied to every rendered field (name, primary,
    outcome, error) before any length cap, so a call is always one physical line and a
    payload that contains ledger-shaped text cannot forge an entry."""

def format_tool_activity_line(entry: ToolActivityEntry) -> str:
    """One line: ``- <name> <primary>`` [+ `` → <outcome>``] [+ `` ! failed: <first 160 chars>``].

    Every field passes through ``escape_ledger_text`` first; the 160-char caps apply to
    the escaped text.

    Primary argument, first match wins: ``file_path`` | ``target_file`` | ``path`` |
    ``notebook_path`` | ``TargetFile`` -> the path; ``command`` -> first 160 chars;
    ``pattern`` | ``query`` -> quoted; skill ``name`` -> ``name=<value>``; MCP task tools
    -> ``task_id=<ref>``, ``commit_sha=<sha>``, and ``title=<first 60 chars>`` when present.
    """

def render_tool_activity(entries: list[ToolActivityEntry]) -> str:
    """Header + lines; consecutive identical lines collapse to ``<line> (xN)``.

    Truncation is evidence-aware. When the collapsed lines exceed either cap, mark as
    protected: every failed line, the first line per distinct edited path, every MCP
    task mutation (``claim_task``, ``close_task``, ``update_task``, ``link_commit``,
    ``create_task``), every commit-producing call, and the last
    ``DIGEST_ACTIVITY_TAIL_LINES`` lines. Keep protected lines in chronological order,
    fill the remaining line/char capacity with the other lines in chronological order,
    drop protected lines oldest-first only when they alone exceed the caps, and end
    with ``- … N more tool calls omitted`` where N counts every dropped underlying
    call: a dropped collapsed ``(xN)`` line contributes its full multiplicity, so the
    marker's count always equals the number of tool calls not represented by a
    rendered line. Both caps are measured on the final escaped lines.
    """
```

Illustrative ledger shaped on #10854's first turn (the two commit lines are added to
show outcome evidence; the native `run_terminal_command` records render under the
canonical `Bash` name through the ledger-local alias):

```text
[tool activity]
- read_file /Users/josh/.grok/sessions/.../goal/plan.md
- mcp gobby-tasks:get_task task_id=#20539
- mcp gobby-tasks:claim_task task_id=#20544
- Bash gcode outline src/gobby/sessions/compact_continuation.py; ech…
- search_replace /Users/josh/.gobby/worktrees/gobby/wt-task-20539-m1/tests/sessions/test_clear_continuation.py (x3)
- Bash cd /Users/josh/.gobby/worktrees/gobby/wt-task-20539-m1 && uv run pytest … ! failed: DATABASE_URL is not set
- Bash cd … && git commit -m "[gobby-#20544] feat: clear continuation tests" → commit 4f1c2ab [gobby-#20544] feat: clear continuation tests
- mcp gobby-tasks:close_task task_id=#20544 commit_sha=4f1c2ab → commit 4f1c2ab
```

Unit tests in the new `tests/sessions/transcripts/test_tool_activity.py` cover
`canonical_tool_name` (use_tool unwrap; MCP dispatcher unwrap for top-level, hoisted,
`args`-alias, JSON-string, and nested payload shapes including a `close_task` carrying
`commit_sha`; shell alias), `escape_ledger_text` (CR, LF, tab, `\x1b`, and a command whose
text contains a fake `- mcp gobby-tasks:close_task` line stays one physical line and
counts once against the caps), primary
argument selection, `(xN)` collapsing, failure annotation, `commit_outcome` for shell and
task-tool commits, malformed wrapper input (a JSON-string `arguments` payload that fails
to parse and a wrapper missing its routing fields both raise
`CallToolWrapperInputError`; the line keeps the raw dispatcher name with empty input and
still renders), and both caps — including a 120-entry list whose `search_replace` of a
new path, `git commit`, and `mcp gobby-tasks:close_task` entries sit after position 80
and survive both caps while the omission count equals the number of dropped underlying
calls, pinned with a dropped collapsed `(xN)` group that contributes its full
multiplicity.

**Acceptance:**

- 1.1.1 - `tool_activity.py` exposes `canonical_tool_name`, `format_tool_activity_line`, and `render_tool_activity` with the caps and collapsing described above. file: `src/gobby/sessions/transcripts/tool_activity.py`.
- 1.1.2 - Every parser accepts `include_tool_activity` and returns identical message counts, role sequences, and `content` strings with the flag on and off across all fixtures; the ledger appears only as `tool_activity` on user messages. test: `tests/sessions/test_transcript_parsers.py::test_tool_activity_flag_preserves_pair_shape`.
- 1.1.3 - Grok segments carry a ledger naming `search_replace` paths, `mcp gobby-tasks:claim_task`, and a failed canonical `Bash` entry (`- Bash <command> ! failed: …`) derived from a native `run_terminal_command` record — the native name never appears in a rendered line — from the `grok_audit` fixtures. test: `tests/sessions/transcripts/test_grok_parser.py::test_extract_last_messages_tool_activity_ledger`.
- 1.1.4 - A tool-only turn (user prompt followed only by tool-use/tool-result records) yields the same messages as today — including Grok's empty assistant sentinel, asserted with exact flag-off/flag-on role and content arrays and unchanged digest cursor movement on a Grok tool-only fixture — with its ledger on that turn's user message and never on the previous turn. test: `tests/sessions/test_transcript_parsers.py::test_tool_only_turn_ledger_stays_on_its_user_message`.
- 1.1.5 - `canonical_tool_name("run_terminal_command", {"command": "git status"})` returns the canonical `Bash` name with the command input, so the ledger line and `commit_outcome` treat it as a shell call, while `is_shell_tool("run_terminal_command")` and `canonicalize_shell_tool_name("run_terminal_command")` are unchanged from today (the alias is ledger-local). test: `tests/sessions/transcripts/test_tool_activity.py::test_grok_terminal_alias_is_ledger_local`.
- 1.1.6 - Truncation keeps failed calls, first-per-path edits, task mutations, commit-producing calls, and the last ten calls under both caps for a 120-entry list, and the omission marker counts dropped underlying calls — a dropped collapsed `(xN)` group counts N. test: `tests/sessions/transcripts/test_tool_activity.py::test_render_tool_activity_truncation_keeps_evidence`.
- 1.1.7 - Commit-producing calls carry `→ commit <sha>` from the correlated successful result or `commit_sha` argument, and no other call retains successful output: `is_commit_producing` is true for `git -C /repo commit -m msg` and `cd x && git commit -am msg` and false for `echo "git commit"`, `grep -n "git commit" notes.md`, and `gcode grep -F "git commit" src`, whose successful output (including a forged `[main abc1234] msg` line) never reaches the ledger, the analyzer adapter, or a prompt; `ls # git commit` is not commit-producing even when its stdout carries a forged `[main abc1234] msg` line, so no `outcome` is set and the output is discarded unread. test: `tests/sessions/transcripts/test_tool_activity.py::test_commit_outcome_from_shell_and_task_tools`.
- 1.1.8 - Control characters in any field are escaped before truncation; a multiline command renders as one physical line, and the caps and omission count are computed on the escaped text. test: `tests/sessions/transcripts/test_tool_activity.py::test_ledger_escapes_control_characters_before_caps`.
- 1.1.9 - `canonical_tool_name` decodes every wrapper shape `canonicalize_call_tool_wrapper` accepts (top-level, hoisted, `args` alias, JSON string, nested) and keeps `task_id`, `title`, and `commit_sha`. test: `tests/sessions/transcripts/test_tool_activity.py::test_canonical_tool_name_matches_call_tool_wrapper_shapes`.
- 1.1.10 - A Codex window without item records puts the execution-chain's inner `cmd` command and its failure text in the ledger; a window with `item_completed` tool items takes `McpToolCall` (canonical `mcp server:tool` line with task args), `CommandExecution` (canonical `normalize_command_execution` command, failure from nonzero `exit_code`), and `FileChange` (one `apply_patch <path>` entry per `changes` key) entries from the `codex_item_activity` pre-scan, attributed to their originating user-to-user turns via `record_index`, with per-call wrapper suppression; an `McpToolCall` item whose `status` is `completed` and whose `result` carries `{"success": false, "error": …}` — as a dict, as an `{"Ok": <json string>}` payload, and as a `structuredContent` envelope — renders `! failed:` with that error text in all three shapes, an `{"Err": …}` result renders failed likewise, and a completed item with a successful result or no `result` key renders bare — while `iter_parse_events` output, `parsed_index` assignment, and resume boundaries are byte-identical before and after this leaf. test: `tests/sessions/test_transcript_parsers.py::test_codex_item_stream_precedence_in_ledger`.
- 1.1.11 - Qwen `functionResponse` results with `toolCallResult.status` `error`/`cancelled` or an `error` response key annotate their entry as failed, correlated by `id` or `callId`. test: `tests/sessions/transcripts/test_qwen_transcript_parser.py::test_qwen_failed_function_response_in_ledger`.
- 1.1.12 - Malformed wrapper input (unparseable JSON-string `arguments`, missing routing fields) raises `CallToolWrapperInputError` inside `canonical_tool_name`, which keeps the raw dispatcher name with empty input and still renders a ledger line instead of breaking the digest. test: `tests/sessions/transcripts/test_tool_activity.py::test_canonical_tool_name_malformed_wrapper_falls_back`.
- 1.1.13 - `canonical_tool_name` is total over nullable parser output: a `tool_use` with `tool_name=None` (Qwen null `functionCall.name`), a non-string name, and a non-mapping `tool_input` each normalise to the `"unknown-tool"` label with empty input and render a ledger line, for malformed native Qwen, Droid, and Codex records. test: `tests/sessions/transcripts/test_tool_activity.py::test_canonical_tool_name_total_over_nullable_parser_output`.
- 1.1.14 - The three call outcomes are distinguishable on every provider: a matched successful call renders a bare line, a failed call renders `! failed:`, and a call with no matching result record renders `(no result recorded)` — proven with a successful test-command line, a failed call, and an in-flight final call in one turn for all five parsers. test: `tests/sessions/transcripts/test_tool_activity.py::test_ledger_distinguishes_success_failure_and_missing_result`.
- 1.1.15 - Per-call Codex precedence on a mixed window: a turn whose window holds an item-covered call and a wrapper-only call keeps both (item entry plus execution-chain entry, no double count, no drop); a split tail whose wrapper has neither `custom_tool_call_output` nor item in the window keeps the wrapper derivation projected through `pending_exec_command` — a `custom_tool_call` `exec` wrapper whose JS names `tail -f /var/log/widget.log` renders that command with `(no result recorded)`, never the raw JS; a window cut between a landed `CommandExecution` item and the wrapper's output renders that command exactly once; and a pending wrapper whose JS holds two `exec_command` literals keeps the raw `exec` name and input; item entries land on their originating turn's ledger, never a neighbor's. test: `tests/sessions/test_transcript_parsers.py::test_codex_mixed_window_and_split_tail_precedence`.
- 1.1.16 - `TranscriptReadError` is defined in `transcripts/base.py` with `path`, `byte_offset`, and `line_number`, and it is the exact class raised by both 1.2's `_read_undigested_turns` (global 1-based `line_number` and the record's `byte_offset`) and 1.3's `_read_transcript_window` (`line_number is None`, `byte_offset` equal to the malformed record's file offset, proven on a window that omits a prefix) for a malformed interior line, for a line at any position — the final line included — that decodes to a bare JSON scalar or list, for a malformed final line that the file terminates with `\n`, and for a line whose raw bytes are not valid UTF-8 in an interior position or in a newline-terminated final record (the offset names that record); a malformed final line with no trailing newline, and an unterminated final fragment ending in a split multibyte code point, each raise nothing in either reader and take the withhold path instead. The same transcript is classified identically by both readers in all six forms, fed as identical raw bytes. test: `tests/sessions/test_transcript_read_error.py::test_transcript_read_error_shared_by_digest_and_summary_readers`.
- 1.1.17 - `normalize_command_execution` and `_command_execution_outcomes` agree byte-for-byte on `[/bin/zsh, -lc, cmd]` (command is `cmd`), on a multi-part argv without a shell wrapper (`shlex.join`), and on a nonzero `exit_code` with no `status` key (entry failed, never bare); a mixed window holding two identical `uv run pytest -k widget` wrappers and one landed `CommandExecution` item renders exactly two ledger lines — one item-derived, one execution-chain — in `codex_item_activity` and in the 1.3 adapter. test: `tests/sessions/test_transcript_parsers.py::test_codex_item_canonicalization_matches_exec_adapter`.
- 1.1.18 - For Codex, Qwen, and Droid, a parser hydrated with non-empty private state (Codex: a pending execution-chain wrapper and a pending tool-search id; Qwen: a `_last_tool_use_id`; Droid: a `_last_assistant_index` and sidecar usage) that runs `extract_last_messages(..., include_tool_activity=True)` over a tool-heavy window has `snapshot_state()` and every private field equal to an untouched control parser hydrated identically, and continuing `iter_parse_events` over the following records on both yields identical records, `parsed_index` values, and `codex_exec_outcomes`; the ledger equals a fresh parser's; and `_extract_digest_pairs` called twice on one Codex parser (segment, then prefix) returns the same pair counts as two fresh parsers. Every symbol this item exercises exists before this leaf begins, so §1.1 closes on its own criteria; the identical claim for 1.3's adapter is 1.3.17, owned by the leaf that creates it. test: `tests/sessions/transcripts/test_tool_activity.py::test_observational_scans_leave_parser_state_untouched`.

### 1.2 Feed digest pairs with the ledger and teach the turn-record prompt [category: code] (depends: 1.1)
`kind: deliverable`

Targets:
- `src/gobby/memory/digest.py::*` — scope-reason: new module-level `DigestPair`, `UndigestedBatch`, and `ResolvedPairs` NamedTuples; changes to `_extract_digest_pairs`, `_build_turn_record_prompt`, `_read_undigested_turns`, `_resolve_undigested_pairs`, and `_build_turn_and_digest_serialized` (the `tail_withheld` propagation chain); `build_turn_and_digest` gains only the keyword-only `withheld_capture` pass-through
- `src/gobby/install/shared/prompts/memory/turn_record.md`
- `src/gobby/install/bundled_content_manifest.json::*` — scope-reason: regenerated checksum manifest for changed bundled content
- `tests/memory/test_digest.py::*` — scope-reason: add ledger-in-pair, cursor, prompt-instruction, partial-tail stable-read, and `tail_withheld` propagation cases, and migrate the 23 two-value `result, next_index = await _read_undigested_turns(...)` destructures (lines 1625–2230) to `UndigestedBatch` attributes
- `tests/prompts/test_prompt_sync.py::*` — scope-reason: add the isolated-DB `memory/turn_record` sync assertion
- `tests/sessions/transcripts/test_grok_coverage_audit.py::*` — scope-reason: migrate `compute_user_anchored_coverage` and `UserAnchoredCoverage.pairs` from two-value pair destructures to named `DigestPair` attributes
- `tests/sessions/transcripts/test_grok_parser.py::*` — scope-reason: migrate the four `_extract_digest_pairs` two-tuple equality assertions (lines 409, 437, 501, 538) to `DigestPair` values
- `src/gobby/sessions/lifecycle.py::SessionLifecycleManager._sweep_digest_backlogs`
- `tests/sessions/test_sessions_lifecycle.py::*` — scope-reason: add tail-withheld backlog-sweep termination coverage

`_extract_digest_pairs` calls
`parser.extract_last_messages(turns, num_pairs=max(1, len(turns)), include_tool_activity=True)`
and returns `list[DigestPair]`, a `NamedTuple(prompt: str, response: str, activity: str)`
where `response` is narration only and `activity` is the `tool_activity` side field of
the pair's user message (or `""`). Injected-context stripping and the lifecycle-prompt and
synthetic-noise filters are unchanged and evaluate narration only. `_read_undigested_turns`
keeps its cursor arithmetic in pairs; its catch-up in-flight check reads
`pairs[-1].response` (narration), so a tool-only active turn is still left for the
turn-end digest even though its ledger is non-empty; it returns `UndigestedBatch`, a
`NamedTuple(pairs: list[tuple[str, str]], next_pair_index: int, tail_withheld: bool,
tail_pair: DigestPair | None)`,
whose `pairs` are `(prompt, text)` tuples where `text` is `response` and `activity`
joined by one blank line and stripped, and whose `tail_pair` is the trailing
`DigestPair` exactly as extracted — prompt, narration so far, and the ledger of every
complete record of the turn, `response` and `activity` still separate fields — for
**every** non-empty batch: the withheld pair when `tail_withheld` is true, and otherwise
the last pair of `pairs` in its uncomposed form. Composition is lossy in one direction
only, so a batch that keeps the composed text alone can never render the prompt, ledger,
and narration as the separate sections 1.4's fallback requires; `tail_pair` is what keeps
a completed tail as recoverable as a withheld one. It is `None` only for a batch with no
undigested pairs at all (the
missing-file and missing-parser early returns produce
`UndigestedBatch([], digested_pair_index, False, None)`). That composed text is what
`last_digest_input_hash` hashes, so the hash changes when activity changes. Because the ledger rides on the user message of its own turn, the
compact-triggering turn's evidence always lands on the trailing pair.

`_read_last_turn_from_transcript` keeps calling `extract_last_messages(turns, num_pairs=1)`
without the flag.

The return-shape change has two existing consumers outside `digest.py`:
`tests/sessions/transcripts/test_grok_coverage_audit.py::compute_user_anchored_coverage`
destructures pairs as `(prompt, response)` in three comprehensions and stores the tuple
on `UserAnchoredCoverage.pairs` — migrate those comprehensions to named attributes
(`pair.prompt`, `pair.response`) and type `UserAnchoredCoverage.pairs` as
`tuple[DigestPair, ...]`, preserving the computed coverage and completeness metrics
exactly. And `tests/sessions/transcripts/test_grok_parser.py` asserts
`_extract_digest_pairs(parser, turns) == [(prompt, response), ...]` at four sites
(lines 409, 437, 501, 538); a three-field NamedTuple never equals a two-tuple, so
migrate each assertion to `DigestPair` values (activity `""` where the fixture has no
tool records). After both migrations, no two-field `_extract_digest_pairs` expectation
remains anywhere under `tests/`.

Stable transcript read (`_read_undigested_turns`): the current implementation
`json.loads`-es every line inside one `try` whose `except Exception` returns an empty
batch — during a concurrent transcript write, a partial trailing JSONL record makes the
pre-summary digest report nothing undigested, indistinguishable from a verified no-work
result. Replace it with a per-line policy: parse lines individually. A malformed
**final** non-blank line is a possibly in-flight partial write **only when the file does
not end with a newline** (1.1's termination rule; a malformed final line the file already
terminated is finished corruption and raises `TranscriptReadError` at once) — re-read the
tail once
after `TRANSCRIPT_TAIL_RETRY_DELAY_SECONDS = 0.2` (one bounded retry; writers finish a
line in milliseconds). If the line completes, proceed normally. If the re-read now finds
it newline-terminated and still malformed, the writer finished a bad record: raise
`TranscriptReadError`. If it is still unterminated and
malformed, drop it, process the complete prefix, and **withhold the trailing pair**
through the same leave-trailing-pair-undigested mechanism catch-up mode already uses:
the cursor never advances past the withheld pair, so when the partial record later
completes it enriches a still-undigested pair, `last_digest_input_hash` changes, and
the next digest persists the enriched pair — a dropped record can never be silently
lost behind an advanced cursor (a final line that decodes to a non-object value, and a
malformed final line the file already terminated with a newline, are both finished
records and raise the corruption error below instead). The digest outcome reports the withhold as
`tail_withheld: True` and carries the pair as `withheld_pair`, so 1.4's pre-summary
callers know the compact-triggering turn is not covered and hold the stable evidence of
it without a second transcript read. A malformed **interior** line, or a line at any position
that decodes to a non-object JSON value, raises the typed `TranscriptReadError`
(defined in `transcripts/base.py`, 1.1; this per-line loop reads the file in binary and
decodes each line as UTF-8, so it supplies both the global 1-based `line_number` and
the record's `byte_offset`) that `build_turn_and_digest` converts to
`{"error": …, "error_kind": "transcript_read"}`, so the 1.4 callers abort the summary
refresh on corruption instead of proceeding on an empty batch. Missing file and missing
parser keep returning the empty batch unchanged.

Propagation of `tail_withheld` is explicit at every boundary of the chain.
`_resolve_undigested_pairs` reads `batch = await _read_undigested_turns(...)`, carries
`batch.tail_withheld` and `batch.tail_pair`, and returns `ResolvedPairs`, a
`NamedTuple(pairs: list[tuple[str, str]], input_hash: str, next_pair_index: int,
tail_withheld: bool, tail_pair: DigestPair | None)`, or `None`. Every existing `None` return (catch-up with no pairs,
empty or lifecycle or synthetic `prompt_text`, duplicate `last_digest_input_hash`) stays
`None` when the batch is not withheld; when it is withheld those same branches return
`ResolvedPairs([], "", batch.next_pair_index, True, batch.tail_pair)` instead, and the `prompt_text`
fallback is not consulted for a withheld batch (the withheld pair *is* the active turn),
so a withhold is never lost behind a skip. `SessionLifecycleManager._sweep_digest_backlogs`
— the one `build_turn_and_digest` caller outside the hook and compact paths — classifies the
new outcome explicitly: its bounded per-session batch loop currently breaks on `None` or
`"error"` only, so it also breaks on `result.get("tail_withheld")`; a withheld outcome ends
that session's batch loop after any prefix progress has been persisted (another attempt
would reread the same in-flight tail), and the next sweep cycle or turn-end digest retries
once the tail changes. Ordinary progress and error termination are unchanged.
`_schedule_summary_refresh_if_stale` is skipped
whenever the resolved outcome carries `tail_withheld`: a prefix digest that withheld the
compact-triggering pair must not spawn a background summary from a digest that lacks it
(that summary would race 1.4's tail fallback), so the later digest that covers the pair is
the one that schedules the refresh; the shadow-judge scheduling is unchanged. `_build_turn_and_digest_serialized` unpacks
`ResolvedPairs` by attribute: a resolved value with empty `pairs` returns
`{"tail_withheld": True, "withheld_pair": {"prompt", "response", "activity"}}`
(`resolved.tail_pair` as a plain dict) before the LLM call and persists nothing; a non-empty
withheld batch digests the complete prefix normally and adds the same two keys to its
`{"turn_num", "turn_length", "digest_length", …}` result; a batch that is not withheld
never carries either key. `build_turn_and_digest` keeps that returned-dict contract — the flag reaches its callers
through the returned dict, which is what 1.4 branches on — and gains one keyword-only
parameter for the evidence that a failure would otherwise destroy.

Withheld evidence survives a failed attempt. The pair is extracted at resolution time,
before the prefix LLM call and before persistence, so any later failure of that same call —
a returned `{"error"}`/`{"cancelled"}`, a raised exception, or 1.4's outer deadline
destroying the task — would otherwise discard it and leave 1.4 with only the turn-blind
raw tail, which by its own construction cannot reach the opening prompt of a tool-heavy
turn. `build_turn_and_digest` and `_build_turn_and_digest_serialized` therefore take a
keyword-only `withheld_capture: dict[str, Any] | None = None`, and
`_build_turn_and_digest_serialized` writes `{"tail_withheld": <that resolution's own
flag>, "withheld_pair": <`resolved.tail_pair` as a plain dict, or `None` for an empty
batch>}` into it **immediately after** `_resolve_undigested_pairs` returns — on **every**
resolution, withheld or complete, before the LLM call, before persistence, and before any
branch that can fail. The dict belongs to the caller and outlives the call, so 1.4 reads
the evidence on every terminal path, including the one where nothing is returned at all.
Every *returned* terminal result of a withheld batch carries the same two keys as well:
the digested-prefix success already does, and the `{"error": …}` and `{"cancelled": …}`
returns of a withheld batch now do too, so a caller that passes no capture still sees the
withhold on every returned outcome. Each call of the refresh overwrites the capture with
its own resolution, so the capture always describes the latest attempt to reach resolution:
a withheld attempt followed by one whose tail has completed leaves a complete pair under
`tail_withheld: False`, and a failure after that point can no longer resurrect the
superseded pair or assert a withhold that has ended. A call that fails before resolution
writes nothing and leaves the prior attempt's evidence intact.

Persistence is a cancellation barrier. `_build_turn_and_digest_serialized` runs
`session_manager.persist_digest_state` through `_run_sync_io` (a worker thread) while
holding the per-session lock, and 1.4 runs the whole digest under an `asyncio.wait_for`
deadline; a thread cannot be cancelled, so a deadline that fires during persistence must
not release the lock while the worker is still writing. The persistence await becomes

```python
persist = asyncio.ensure_future(_run_sync_io(session_manager.persist_digest_state, ...))
try:
    updated_session = await asyncio.shield(persist)
except asyncio.CancelledError:
    while not persist.done():  # hold the lock until the worker has finished,
        try:                   # however many times this task is cancelled meanwhile
            await asyncio.wait({persist})
        except asyncio.CancelledError:
            continue
    if (exc := persist.exception()) is not None:
        logger.warning("digest persistence failed for %s during cancellation", session_id, exc_info=exc)
    raise
```

so a cancelled digest either persisted nothing (cancelled before the barrier — the LLM call
and the reads are plain awaits) or persisted the complete digest state before the
cancellation propagates; the lock is held for the whole write, so a concurrent
`build_turn_and_digest` for the same session observes the settled cursor. The cleanup
re-awaits the future on every further `CancelledError` rather than once, so a second
`cancel()` (daemon shutdown racing 1.4's deadline) cannot release the lock mid-write; the
worker's exception is retrieved and logged so a failed write is never silently lost behind
the cancellation; and the repeated requests need no bookkeeping here — the task's own
cancel count already makes `asyncio.wait_for` re-raise the cancellation instead of
converting it to `TimeoutError`. The only cost is
that a deadline firing mid-write is honoured after the write (milliseconds of DB work),
which 1.4's timeout branch accounts for by reloading the session before it chooses a
fallback. The 23 two-value
destructures of `_read_undigested_turns` in `tests/memory/test_digest.py`
(`result, next_index = await _read_undigested_turns(...)`, lines 1625–2230) migrate to
named attributes; no test patches `_resolve_undigested_pairs`, so
its new shape has no mocked consumer.

Add one paragraph to both the bundled prompt `turn_record.md` and the inline fallback in
`_build_turn_record_prompt`, placed directly before "turn_markdown must cover":

```text
The Agent Response may end with a `[tool activity]` ledger: one line per tool call in
order, with the primary argument (file path, command, query, MCP server:tool and task
ref) and ` ! failed:` annotations. Treat that ledger as the authoritative record of tools
used, files created or modified, commands run, commits, and task operations; narration
that contradicts it is wrong. A line with no annotation completed successfully — a bare
test command line means those tests ran and passed; ` ! failed:` means the call failed;
`(no result recorded)` means the call was still in flight when the turn ended.
```

Regenerate `bundled_content_manifest.json` with
`uv run python -c "from pathlib import Path; from gobby.install.manifest import write_bundled_content_manifest; write_bundled_content_manifest(Path('src/gobby/install'))"`
so the changed prompt syncs to the DB registry (rule 8: the DB row is the live prompt).

**Acceptance:**

- 1.2.1 - Digest pairs contain the ledger for tool-heavy turns while `_read_last_turn_from_transcript` output is unchanged. test: `tests/memory/test_digest.py::test_extract_digest_pairs_includes_tool_activity`.
- 1.2.2 - The inline fallback prompt and the bundled prompt both carry the ledger instruction and the bundled manifest checksum is updated. file: `src/gobby/install/shared/prompts/memory/turn_record.md`.
- 1.2.3 - Replaying the Grok audit fixture through `_extract_digest_pairs` yields a pair whose activity names `search_replace` and `mcp gobby-tasks:claim_task`. symbol: `_extract_digest_pairs`.
- 1.2.4 - After `sync_bundled_prompts` into an isolated database, the live `memory/turn_record` row carries the ledger instruction and its `required_variables` are unchanged. test: `tests/prompts/test_prompt_sync.py::test_turn_record_sync_carries_ledger_instruction`.
- 1.2.5 - With one pair already digested, a following user prompt with only tool records digests as a second pair whose text is that turn's ledger, the first pair's text is unchanged, the cursor advances by exactly one, and in catch-up mode the trailing tool-only pair is left undigested. test: `tests/memory/test_digest.py::test_tool_only_turn_ledger_stays_on_current_pair`.
- 1.2.6 - The Grok coverage-audit helper consumes `DigestPair` by named attribute and the four `test_grok_parser.py` equality assertions compare `DigestPair` values; no two-field `_extract_digest_pairs` expectation remains under `tests/`, and the coverage/completeness metrics for the audit fixtures are unchanged. file: `tests/sessions/transcripts/test_grok_parser.py`.
- 1.2.7 - With a partial trailing JSONL line that stabilizes on the bounded re-read, the digest proceeds normally; with a line still partial after the retry, the digest processes the complete prefix, withholds the trailing pair, does not advance the cursor past it, and reports `tail_withheld: True`; a malformed interior line yields `{"error": …, "error_kind": "transcript_read"}` instead of an empty batch, and so do a final line holding a bare JSON scalar or list, a malformed final line the file already terminates with `\n`, a malformed unterminated tail that the bounded re-read finds newline-terminated and still malformed, and invalid UTF-8 bytes in an interior line or in a newline-terminated final record (no withhold in any of the six); an unterminated final fragment cut inside a multibyte code point withholds exactly like a partial JSON line and is included once the remaining bytes land. test: `tests/memory/test_digest.py::test_partial_transcript_tail_withholds_trailing_pair`.
- 1.2.8 - A partial final tool-result record that completes after a withheld digest is included when the next digest runs: the enriched pair's ledger carries the completed call, the cursor advances only then, and exact cursor movement is asserted at every step. test: `tests/memory/test_digest.py::test_completed_tail_record_reaches_ledger_after_withhold`.
- 1.2.9 - With a persistently partial trailing line, the public `build_turn_and_digest` result carries `tail_withheld: True` both when a complete prefix was digested (beside `turn_num`) and when the withheld pair was the only undigested content (`{"tail_withheld": True}`, no persistence, no LLM call); `_resolve_undigested_pairs` returns `ResolvedPairs` with empty `pairs` and `tail_withheld=True` on its catch-up and duplicate-hash skips for a withheld batch; every withheld outcome also carries `withheld_pair` whose `prompt` is the trailing pair's prompt and whose `activity` lists every complete call of that turn with `(no result recorded)` for the in-flight one; and a run without a withhold carries neither key. With a withheld batch whose complete prefix is digested and the LLM call then failing, cancelled, or raising, and separately with `persist_digest_state` raising, the caller's `withheld_capture` dict holds `tail_withheld: True` and the exact `withheld_pair` in every case (written before the LLM call), and each returned `{"error": …}`/`{"cancelled": …}` result carries both keys too. A run without a withhold leaves the same capture holding `tail_withheld: False` beside the trailing complete pair, whose `prompt`, `response`, and `activity` equal the extracted `DigestPair` field for field and never the blank-line-joined text of `pairs[-1]`, while its *returned* result carries neither key, and two calls sharing one capture dict prove the overwrite in both directions: a withheld first call followed by a second whose tail has completed and whose LLM call then raises leaves `tail_withheld: False` and the complete pair (never the withheld one), and a complete first call followed by a withheld second leaves `tail_withheld: True` and the withheld pair; a call that raises inside `_read_undigested_turns` before resolution leaves the prior call's capture untouched. test: `tests/memory/test_digest.py::test_tail_withheld_propagates_to_public_outcome`.
- 1.2.10 - With `persist_digest_state` blocked on an event inside its worker thread and the digest task cancelled while it awaits that write, the per-session lock stays held until the event releases the worker, the digest state is fully persisted (markdown and cursor), and a concurrent `build_turn_and_digest` for the same session started during the cancellation observes the advanced cursor and digests nothing twice; a cancellation delivered during the LLM call persists nothing. test: `tests/memory/test_digest.py::test_cancelled_digest_holds_lock_through_persistence`.
- 1.2.11 - With a complete prefix digested past the summary watermark and the trailing pair withheld, `memory_manager.schedule_background_task` (spied) receives no `session-summary-refresh-*` task; the next digest that covers the pair schedules it. test: `tests/memory/test_digest.py::test_withheld_tail_suppresses_summary_refresh_scheduling`.
- 1.2.12 - The lifecycle backlog sweep stops the current per-session batch loop after any tail_withheld outcome, including one that also persisted a complete prefix, without spending another bounded attempt on the same in-flight trailing pair; ordinary progress and error termination remain unchanged. test: `tests/sessions/test_sessions_lifecycle.py::TestDigestBacklogSweep.test_sweep_stops_session_on_tail_withheld`.
- 1.2.13 - With `persist_digest_state` blocked on an event inside its worker thread and the digest task cancelled twice — once to enter the barrier and again while the barrier awaits the persistence future — the per-session lock stays held until the event releases the worker, the digest state is fully persisted, a concurrent same-session `build_turn_and_digest` started during the cancellations observes the advanced cursor, and the task finishes with `CancelledError`; with the worker raising instead of completing, the exception is logged with the session id and the cancellation still propagates. test: `tests/memory/test_digest.py::test_repeated_cancellation_holds_lock_until_persistence_settles`.

### 1.3 Ground summaries in transcript-derived structured data when a digest exists [category: code] (depends: 1.1)
`kind: deliverable`

Targets:
- `src/gobby/sessions/summarize.py::*` — scope-reason: new module-level `SummarySourceContext` NamedTuple and `build_summary_source_context` builder, plus `_generate_session_summary_core` rewritten to consume the builder
- `src/gobby/sessions/analyzer.py::TranscriptAnalyzer.extract_handoff_context`
- `src/gobby/sessions/analyzer.py::TranscriptAnalyzer._analyze_tool_use`
- `src/gobby/sessions/analyzer.py::TranscriptAnalyzer._format_tool_description`
- `src/gobby/cli/sessions.py::*` — scope-reason: unchanged call site of `TranscriptAnalyzer.extract_handoff_context` (positional `turns` only; `initial_goal` defaults to `None`), listed for consumer closure of that exact Target
- `src/gobby/sessions/analyzer_turns.py`
- `src/gobby/sessions/summary_transcripts.py::*` — scope-reason: new `TranscriptWindow` NamedTuple, the bounded reverse tail reader `_read_transcript_window`, the bounded forward first-goal scan `_read_first_user_goal`, and `_read_transcript` becoming a signature-preserving delegation
- `src/gobby/mcp_proxy/tools/sessions/_summary_metadata.py::*` — scope-reason: `compact_summary_metadata_matches` rewritten onto `build_summary_source_context`, and the module-level `_SUMMARY_METADATA_RECOMPUTE_ERRORS` tuple gains `TranscriptReadError`
- `tests/sessions/test_summarize.py::*` — scope-reason: add digest-present structured-context, truncated-window original-goal, reverse-tail I/O-bound, and shared source-context cases
- `tests/mcp_proxy/tools/sessions/test_summary_metadata.py`
- `tests/sessions/test_sessions_analyzer.py::*` — scope-reason: add adapter, canonical-name, retention-boundary, and adapter-observationality cases

Summary generation and compact freshness validation must hash the same source payload,
so the first half of `_generate_session_summary_core` — transcript window, analyzer
facts, git enrichment, prompt context, template, and hash — moves into one shared
builder in `summarize.py` that both producers call:

```python
class SummarySourceContext(NamedTuple):
    digest_markdown: str
    window: TranscriptWindow
    turns: list[dict[str, Any]]          # adapter output, Claude-shaped
    handoff_ctx: HandoffContext
    summary_context: dict[str, Any]
    prompt_template: str
    source_hash: str

async def build_summary_source_context(
    session: Any,
    *,
    db: Any,
    session_manager: Any,
    session_summary_config: Any,
    run_db: Any = None,
) -> SummarySourceContext | None:
    """Return the canonical summary source payload, or None when there is nothing to read.

    Reads the transcript whenever the file exists, not only when the digest is missing;
    returns None only when there is no transcript file and no digest. Raises
    TranscriptReadError on interior corruption (never swallowed here).
    """
    digest_markdown = _digest_markdown_for_summary(session)
    window = TranscriptWindow(turns=[], truncated=False)
    if path is not None and path.exists():
        window = await _read_transcript_window(
            path, source=source, max_records=SUMMARY_ANALYZER_MAX_RECORDS
        )
    elif not digest_markdown:
        return None
    initial_goal = (
        await _read_first_user_goal(path, source=source) if window.truncated else None
    )
    turns = analyzer_turns_from_transcript(parser, window.turns)  # identity for claude
    handoff_ctx = TranscriptAnalyzer().extract_handoff_context(turns, initial_goal=initial_goal)
    ...  # git enrichment, _build_summary_prompt_context, load_summary_prompt_template
    return SummarySourceContext(..., source_hash=source_context_hash(_source_hash_payload(...)))
```

`_generate_session_summary_core` calls it and keeps its existing `None` handling (the
"No transcript path" / "Transcript file not found" results) and everything after the
hash (`choose_summary_refresh`, LLM call, persistence). `compact_summary_metadata_matches`
is rewritten to call the same builder and compare `ctx.source_hash` with the stored
`summary_source_context_hash` — its current empty-turns / empty-`HandoffContext`
recomputation is deleted, because a summary generated with transcript-derived facts
would otherwise be judged stale the moment it was persisted. Both producers therefore
see the same bounded window and the same analyzer facts: one hash, two readers. The
matcher returns `False` (never raises) when the builder returns `None` or raises
`TranscriptReadError`; `TranscriptReadError` joins `_SUMMARY_METADATA_RECOMPUTE_ERRORS`.
The freshness identity is fact-level by design: `_source_hash_payload` hashes the digest,
the session's per-turn `last_turn_markdown` and `last_assistant_content`, the prompt
template, and the derived `summary_context` (analyzer facts plus git enrichment) — every
input the summary is built from, and nothing else. A transcript record that changes none
of those inputs (a successful non-commit result, whose text the retention boundary drops)
cannot change the summary, so it does not invalidate it; a record that changes any
analyzer fact (an edit, a task operation, a commit result) does. An ordered fingerprint of
the raw window is deliberately not part of the payload: it would make every
compaction-time match fail once the compaction's own records land, forcing regeneration
of a summary whose content would not change.

`SUMMARY_ANALYZER_MAX_RECORDS = 20_000` lives in `analyzer_turns.py`. The window read is
**O(window) in I/O, memory, and work** (below), and the adapter **materializes its output exactly
once as a list**: `TranscriptAnalyzer.extract_handoff_context` is a multi-pass consumer
— repeated forward scans plus two `reversed(turns)` scans (`analyzer.py` lines 180 and
192) — so a generator would either raise at `reversed()` or arrive exhausted at the
second pass. Two bounded lists per refresh (raw window + adapted turns), both
O(window). The refresh re-runs after every digest that passes the watermark
(`_schedule_summary_refresh_if_stale`) and at compaction, so per-refresh work **and
bytes read** are O(window) — the tail window plus, when it is truncated, the bounded
first-goal scan below — never O(transcript). Facts older than the window are carried by the digest
narrative, which the prompt already treats as the session history.

The digest still drives `transcript_summary` and `last_messages` inside
`_build_summary_prompt_context` (its `if digest_markdown:` branch is untouched), and the
builder passes it the **raw** `window.turns`, never the adapted list: its no-digest branch
selects the Grok, Codex, Qwen, or Droid parser by `session.source` and re-parses native
records, so Claude-shaped adapted turns would break that existing fallback. The adapted
list feeds only the analyzer, which populates `files_modified`, `git_commits`,
`task_progress`, `active_gobby_task`, and `recent_activity`, which in turn unlock
`git_status`, `file_changes`, and `git_diff_summary` via `has_session_edits`.

New module `analyzer_turns.py` exposes one adapter so the analyzer never learns per-CLI
envelopes:

```python
def analyzer_turns_from_transcript(parser: Any, turns: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Run parser.iter_parse_events over the bounded window and return Claude-shaped turns.

    Returns a materialized list because the analyzer re-iterates and reverses it.

    Feeds ``raw_lines_from_texts(json.dumps(record) for record in turns)`` to
    ``fresh_scan_parser(parser).iter_parse_events`` — the handed parser is never
    iterated, so its private incremental state is untouched (1.1's scan rule) — and
    consumes every item of every ``ParseEvent.records`` plus
    ``ParseEvent.codex_exec_outcomes``. Never ``parse_line``: on Qwen and Droid it
    returns only ``expanded[0]`` of a multi-block record.

    ParsedMessage(content_type="text", role in {"user","assistant"}) ->
        {"type": role, "message": {"role": role, "content": [{"type": "text", "text": ...}]}}
    ParsedMessage(content_type="tool_use") ->
        {"type": "assistant", "message": {"role": "assistant", "content": [
            {"type": "tool_use", "name": tool_name, "input": tool_input, "id": tool_use_id}]}}
    ParsedMessage(content_type="tool_result") ->
        {"type": "user", "message": {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": tool_use_id,
             "content": <retained text>, "is_error": bool(tool_result.get("is_error") or error)}]}}
        where <retained text> enforces the Constraints retention boundary: failed
        results keep their error text (first 500 escaped chars); successful results of
        commit-producing calls (matched by ``tool_use_id`` against a pending
        ``is_commit_producing`` map built from the preceding ``tool_use`` blocks) keep
        the output ``commit_outcome`` needs; every other successful result emits
        ``content: ""``. No successful read-only output text ever reaches the analyzer.
    CodexNestedExecOutcome ->
        a ``tool_use`` block {"name": "shell", "input": {"command": outcome.command},
        "id": outcome.identity} followed by a ``tool_result`` block with
        ``is_error = outcome.result.get("success") is False``
    Everything else is dropped. Blocks from one source record stay in one emitted turn.
    """
```

`_generate_session_summary_core` applies the adapter for every source except `claude`
(whose raw turns already match). Canonicalization happens once per block:
`extract_handoff_context` rewrites each `tool_use` block's `name`/`input` through
`canonical_tool_name` from 1.1 before dispatching to **both** consumers —
`_analyze_tool_use` and `_format_tool_description` — so structured facts and
`recent_activity` see the same canonical names; a Grok `use_tool` → `gobby__call_tool`
or Codex `call_tool` dispatch renders in recent activity as
`mcp gobby-tasks:close_task`, never as generic `use_tool`/`call_tool`.
`_analyze_tool_use` extends the edit set with `search_replace`, `write_file`, `write`,
`apply_patch`, `edit_file`, and `create_file`, and recognises MCP dispatch by the
canonical `mcp <server>:<tool>` form so Grok and Codex task operations populate
`task_progress`. For Codex windows the adapter calls the same `codex_item_activity`
pre-scan from 1.1: when it returns entries, the adapter emits their blocks —
`McpToolCall` items as canonical MCP `tool_use`/`tool_result` pairs, `CommandExecution`
items as shell `tool_use`/`tool_result` pairs, and `FileChange` items as one
`{"type": "tool_use", "name": "apply_patch", "input": {"file_path": <path>}}` block per
`changes` key so edited paths reach `files_modified` through the edit set (a `tool_use`
alone: the analyzer takes the edit from that block and has no in-flight notion for edits,
so the ledger's resolved status needs no mirrored `tool_result`) — placed in
their originating turns via each entry's `record_index`, and it suppresses exec-wrapper
derivations under 1.1's per-call precedence: only a wrapper whose matching item exists
in the same turn partition is suppressed, while an unmatched wrapper (split tail,
item-stream gap) keeps its execution-chain blocks — a wrapper whose output has not
landed in the window is projected through 1.1's `pending_exec_command` into a shell
`tool_use` block `{"name": "shell", "input": {"command": <inner command>}, "id":
<call_id>}` with no `tool_result` (raw wrapper name and input when the helper fails
closed), matched against items on command alone; a `None` return keeps the
execution-chain path for the whole window. The retention boundary above applies identically to item-derived
results.

Bounded-window provenance: the truncation signal is an exact, compatibility-preserving
API. `summary_transcripts.py` gains `TranscriptWindow` (a
`NamedTuple(turns: list[dict[str, Any]], truncated: bool)`) and
`_read_transcript_window(path, *, source, max_records) -> TranscriptWindow`, a
**bounded reverse tail reader**: it opens the file in binary, seeks to EOF, reads
backwards in 64 KiB chunks (`TRANSCRIPT_TAIL_CHUNK_BYTES = 65_536`), and splits complete
newline-delimited lines until it holds `max_records + 1` complete non-blank lines or
reaches the start of the file. It then parses the newest `max_records` lines in file
order with the per-line policy below, returning them as `turns`;
`truncated` is `True` when the extra (`max_records + 1`-th) complete line was collected
or any bytes remain before the collected lines. Bytes read are therefore bounded by the
size of the last `max_records + 1` records plus one chunk, independent of transcript
length. A line cut by a chunk boundary is completed by the next backward chunk before
it is parsed, so "interior" and "final" keep their file meaning. `_read_transcript`
keeps its exact signature and `list[dict[str, Any]]` return, becoming a delegation to
`_read_transcript_window(path, source=source, max_records=max_turns).turns` (an
unbounded `max_turns=None` delegates with the whole file as the window) — its two
existing production callers, `summarize.py` and `summary_generation.generate_summary`
(`summary_generation.py` line 412), are enumerated and deliberately untouched. Only
`build_summary_source_context` consumes `_read_transcript_window` directly. Boundary tests
pin exactly-`SUMMARY_ANALYZER_MAX_RECORDS` (20,000 records ⇒ `truncated is False`) and
one-over (20,001 ⇒ `truncated is True`). When the window is truncated,
`build_summary_source_context` recovers the session's true first prompt with
`_read_first_user_goal(path, *, source, max_records=SUMMARY_ANALYZER_MAX_RECORDS) -> str | None`:
a forward stream over `parser.iter_parse_events(raw_lines_from_file(path))` that retains
O(1) state, counts raw records as it consumes them, and returns the text of the first
provider-normalized `ParsedMessage(role="user", content_type="text")` — the same
normalization the parsers apply elsewhere, so injected context and bootstrap records are
skipped — stopping at that record, at EOF, or after `max_records` raw records, whichever
comes first; past the ceiling it returns `None` without reading further. The ceiling is
the same constant that bounds the tail, so one truncated refresh reads at most two
windows of the file (`2 × SUMMARY_ANALYZER_MAX_RECORDS` records plus one chunk) on every
refresh, including refreshes of a transcript with late or absent user text. A session
whose first prompt sits behind 20,000 non-user records is the explicit, documented
ceiling of this recovery (round 5 rejected a 200-record prefix because injected context
can plausibly exceed it; no provider emits 20,000 records before the first prompt), and
`initial_goal=None` there falls back to the window's first user record exactly as the
untruncated path does. The goal is passed as `initial_goal` to `extract_handoff_context`,
which prefers a provided goal over the window's first user record — so a tail window
never relabels a recent prompt as the session's original goal.

Transcript-read parity (shared corruption policy with 1.2): `_read_transcript_window`
replaces the current skip-every-malformed-line loop with the same per-line policy —
a malformed **final** non-blank line is dropped as a tolerated in-flight tail only when
the file does not end with a newline (the still-executing call has no result yet in any
case); a malformed final line the file already terminated is finished corruption and
raises, and so does a malformed **interior** line —
or a line at any position, the final one included, that decodes to a non-object JSON value
(today skipped with a warning) —
raises the shared `TranscriptReadError` from `transcripts/base.py` with
`line_number=None` and the record's `byte_offset` (the reverse reader knows each
collected line's file offset from its seek position and never reads the prefix, so a
global line number is not computable within its O(window) bound). On that error
`_generate_session_summary_core` returns a failed-refresh result without persisting any
summary revision, so a summary can never be silently built from a transcript whose
records were skipped while the digest reader refused the same bytes.

Commits get real hashes. For a shell `git commit`, `_analyze_tool_use` records
`{"hash": "", "message": command, "tool_use_id": id}`; `extract_handoff_context`, on the
matching non-error `tool_result` block, fills `hash` and `message` from `commit_outcome`
(1.1). For `close_task`/`link_commit` with `commit_sha`, `_analyze_tool_use` appends
`{"hash": commit_sha, "message": "<tool> <task_id>"}` directly. Unmatched or failed
commit commands keep the empty hash so the summary can still say a commit was attempted.

**Acceptance:**

- 1.3.1 - With a digest present and a transcript on disk, the summary prompt context has non-empty `structured_context` and `file_changes` for Claude, Grok, and Codex fixtures. test: `tests/sessions/test_summarize.py::test_summary_ground_truth_with_digest_present`.
- 1.3.2 - The adapter converts Grok `tool_call` and Codex `function_call` records into `tool_use` blocks the analyzer consumes. test: `tests/sessions/test_sessions_analyzer.py::test_analyzer_turns_from_grok_and_codex_transcripts`.
- 1.3.3 - `search_replace` and `gobby__call_tool` task operations populate `files_modified` and `task_progress`. symbol: `TranscriptAnalyzer._analyze_tool_use`.
- 1.3.4 - Shell `git commit` results and `close_task`/`link_commit` `commit_sha` arguments populate `git_commits` with real hashes; unmatched commits keep an empty hash. test: `tests/sessions/test_sessions_analyzer.py::test_git_commits_carry_hashes_from_results_and_task_tools`.
- 1.3.5 - A Qwen record whose `parts` mix text, `functionCall`, and `functionResponse`, and a Droid record mixing text and `tool_use`, reach the analyzer with every block. test: `tests/sessions/test_sessions_analyzer.py::test_adapter_consumes_every_block_of_multi_part_records`.
- 1.3.6 - Codex activity reaches the analyzer in both envelopes: a window without item records delivers execution-chain shell `tool_use` blocks carrying the nested outcome's command, and a window with `item_completed` records delivers `McpToolCall`/`CommandExecution`/`FileChange` blocks with per-call wrapper suppression — a mixed window keeps the unmatched wrapper's execution-chain blocks while item-covered calls appear exactly once — so edited paths (from `FileChange` `changes` keys), task operations, and commit SHAs populate `files_modified`, `task_progress`, and `git_commits` without double counting or dropped calls; an `McpToolCall` item with `status: completed` and a structured `{"success": false}` result reaches the analyzer as a failed `tool_result` block carrying its error text, so a failed task operation is never counted as completed `task_progress`. test: `tests/sessions/test_sessions_analyzer.py::test_codex_nested_exec_outcomes_reach_analyzer`.
- 1.3.7 - A 50,000-record transcript whose true first prompt and tail-window first prompt are distinct — with 1,000 non-user records (system, metadata, injected-context) ahead of that first prompt — feeds the analyzer at most `SUMMARY_ANALYZER_MAX_RECORDS` records (observed through a counting parser), the tail facts still reach `structured_context`, and `initial_goal` is the true first prompt, never the tail's. test: `tests/sessions/test_summarize.py::test_summary_ground_truth_window_is_bounded`.
- 1.3.8 - `recent_activity` renders wrapped Grok/Codex MCP dispatch with canonical `mcp <server>:<tool>` names, proved for `use_tool` and `call_tool` wrapper blocks. test: `tests/sessions/test_sessions_analyzer.py::test_recent_activity_uses_canonical_tool_names`.
- 1.3.9 - The adapter enforces the retention boundary: failed results keep bounded error text, successful commit-producing results keep commit output, and every other successful result reaches the analyzer with empty content, proved across all five CLIs. test: `tests/sessions/test_sessions_analyzer.py::test_adapter_drops_successful_noncommit_result_text`.
- 1.3.10 - `analyzer_turns_from_transcript` returns a materialized list that survives the analyzer's full traversal contract: the initial-goal forward pass, both `reversed(turns)` scans, and the forward decision pass all see every turn on one adapter output. test: `tests/sessions/test_sessions_analyzer.py::test_adapter_output_survives_multi_pass_analyzer`.
- 1.3.11 - `_read_transcript` keeps its exact list-valued signature (its `summarize.py` and `summary_generation.py` callers pass unmodified), while `_read_transcript_window` reports `truncated is False` at exactly `SUMMARY_ANALYZER_MAX_RECORDS` records and `truncated is True` at one over. test: `tests/sessions/test_summarize.py::test_read_transcript_window_truncation_boundary`.
- 1.3.12 - A malformed interior transcript line makes `_read_transcript_window` raise `TranscriptReadError` and `_generate_session_summary_core` returns a failed refresh with no summary revision persisted; a malformed final line with no trailing newline is dropped and the refresh proceeds; a final line holding a bare JSON list, and a malformed final line the file terminates with `\n`, each raise `TranscriptReadError` and fail the refresh the same way, as do invalid UTF-8 bytes in an interior line and in a newline-terminated final record, while an unterminated final fragment split inside a multibyte code point is dropped and the refresh proceeds — matching 1.2's classification of the same six transcripts byte for byte, from identical raw-byte fixtures. test: `tests/sessions/test_summarize.py::test_interior_corruption_aborts_summary_refresh`.
- 1.3.13 - With a digest present and a transcript carrying edits, a task operation, and a commit, a summary just persisted by `_generate_session_summary_core` satisfies `compact_summary_metadata_matches` immediately (same builder, same hash); appending a successful non-commit `tool_result` record (no analyzer fact changes) keeps it `True`, and appending an edit `tool_use` record (a new `files_modified` entry) makes it `False`; the matcher returns `False` without raising on interior corruption. test: `tests/mcp_proxy/tools/sessions/test_summary_metadata.py::test_fresh_summary_matches_metadata_with_transcript_facts`.
- 1.3.15 - `_read_first_user_goal` is bounded per call: for a 50,000-record transcript with no provider-normalized user text it consumes exactly `SUMMARY_ANALYZER_MAX_RECORDS` raw records (observed through a counting parser), returns `None`, and reads no more bytes than those records plus one chunk on each of three consecutive refreshes (counted through a wrapped file object); with the first user text at record 25,000 it returns `None` at the same ceiling and `build_summary_source_context` passes `initial_goal=None`; with the first user text at record 19,999 it returns that text. test: `tests/sessions/test_summarize.py::test_read_first_user_goal_scan_is_bounded`.
- 1.3.16 - With no digest on the session, `build_summary_source_context` for Grok, Codex, Qwen, and Droid fixtures yields the `transcript_summary` and `last_messages` that provider's native parser produces from the raw window (byte-identical to today's no-digest path) while `handoff_ctx` carries the adapted analyzer facts. test: `tests/sessions/test_summarize.py::test_no_digest_prompt_context_uses_native_parser_for_every_provider`.
- 1.3.14 - Bytes read by `_read_transcript_window` (counted through a wrapped file object) for a 5,000-record and a 50,000-record transcript with `max_records=100` are equal within one `TRANSCRIPT_TAIL_CHUNK_BYTES` chunk and never exceed the size of the last 101 records plus one chunk; a line straddling a chunk boundary parses intact. test: `tests/sessions/test_summarize.py::test_read_transcript_window_io_is_bounded_by_window`.
- 1.3.17 - `analyzer_turns_from_transcript` is observational on every parser it consumes: for Codex, Qwen, and Droid, a parser hydrated with the same non-empty private state as 1.1.18 that runs `extract_last_messages(..., include_tool_activity=True)` and then `analyzer_turns_from_transcript` over a tool-heavy window — the composed order the summary path uses — has `snapshot_state()` and every private field equal to an untouched control parser hydrated identically, continuing `iter_parse_events` over the following records on both yields identical records, `parsed_index` values, and `codex_exec_outcomes`, and the adapter output equals a fresh parser's. test: `tests/sessions/test_sessions_analyzer.py::test_adapter_scan_leaves_parser_state_untouched`.

### 1.4 Digest the ending turn before compaction summaries [category: code] (depends: 1.2)
`kind: deliverable`

Targets:
- `src/gobby/hooks/session_summary_dispatcher.py::SessionSummaryDispatcher.__init__`
- `src/gobby/hooks/session_summary_dispatcher.py::SessionSummaryDispatcher.dispatch`
- `src/gobby/hooks/session_summary_dispatcher.py::SessionSummaryDispatcher._dispatch_without_running_loop`
- `src/gobby/hooks/hook_manager.py::*` — scope-reason: `HookManager.__init__` retains `components.memory_manager`, `_dispatch_session_summaries` shrinks to the `build_session_summary_dispatcher` call, and the module-level `SessionSummaryDispatcher` import is replaced by the `session_summary_wiring` import
- `src/gobby/hooks/session_summary_wiring.py`
- `src/gobby/mcp_proxy/registries.py::*` — scope-reason: `setup_internal_registries` forwards its existing `memory_manager_resolver` into `create_session_messages_registry`; file-wide plumbing scope, so its HTTP, stdio, and embedding-switch callers (unchanged) need no exact-consumer inventory
- `src/gobby/mcp_proxy/tools/sessions/_factory.py::*` — scope-reason: `create_session_messages_registry` gains and forwards the `memory_manager_resolver` keyword (default `None`); file-wide plumbing scope
- `src/gobby/mcp_proxy/tools/sessions/_terminal.py::*` — scope-reason: `register_terminal_tools` gains the `memory_manager_resolver` keyword (default `None`) and `compact_self` resolves it per call and passes `compact_handoff_config`; file-wide plumbing scope, so the readiness, terminal, and clear suites that register these tools unchanged need no exact-consumer inventory
- `src/gobby/mcp_proxy/tools/sessions/_terminal_handoff.py::*` — scope-reason: the new module constant `COMPACT_HANDOFF_TAIL_RETRY_ATTEMPTS`, plus `_refresh_compact_handoff_context`, `_persist_compact_handoff_fallback`, `_compact_handoff_transcript_tail_markdown`, `_run_compact_handoff_background_refresh`, and `_schedule_compact_handoff_background_refresh` as described below
- `tests/hooks/test_session_summary_dispatcher.py`
- `tests/hooks/test_hook_manager_extra.py::*` — scope-reason: the existing `_dispatch_session_summaries` cases keep passing through the moved construction and gain a forwarding assertion
- `tests/mcp_proxy/tools/sessions/test_compact_self.py::*` — scope-reason: add digest-before-refresh and digest-failure-reason cases
- `tests/mcp_proxy/tools/sessions/test_mcp_proxy_tools_sessions_registration.py::*` — scope-reason: add memory-manager resolver forwarding cases
- `tests/mcp_proxy/test_registries.py::*` — scope-reason: assert the sessions registry receives the memory-manager resolver

`SessionSummaryDispatcher.__init__` gains `memory_manager: Any | None = None` and
`config: Any | None = None`. `HookManager.__init__` today accepts `memory_manager` and
forwards it into `HookManagerFactory.create` without retaining it — the factory's
`components.memory_manager` is never unpacked — so `__init__` additionally stores
`self._memory_manager = components.memory_manager`. `hook_manager.py` is at 864 lines,
so the dispatcher construction block (today inline in
`HookManager._dispatch_session_summaries`) is a move: split it into the new
`src/gobby/hooks/session_summary_wiring.py` as
`build_session_summary_dispatcher(*, session_manager, llm_service, session_summary_config,
database, loop, logger, memory_manager, config) -> SessionSummaryDispatcher`, which is the
only place `SessionSummaryDispatcher(...)` is constructed from hook-manager state.
`_dispatch_session_summaries` shrinks to one call that passes `self._memory_manager`,
`self._config`, and `self._current_llm_service()` and then dispatches; the existing
`tests/hooks/test_hook_manager_extra.py` cases patch `gobby.hooks.session_summary_dispatcher`
internals, so they are unaffected by where construction lives. In `dispatch._run`, before
`generate_session_summaries`:

```python
if pre_digest and self.memory_manager is not None and self.llm_service is not None:
    from gobby.memory.digest import build_turn_and_digest
    try:
        outcome = await build_turn_and_digest(
            memory_manager=self.memory_manager,
            session_manager=self.session_manager,
            session_id=session_id,
            llm_service=self.llm_service,
            db=self.database,
            config=self.config,
        )
    except Exception:
        self.logger.warning("pre-summary digest raised for %s", session_id, exc_info=True)
    else:
        if outcome is None:
            pass  # nothing undigested: the turn-end digest already landed
        elif outcome.get("error_kind") == "transcript_read":
            self.logger.warning(
                "pre-summary digest hit transcript corruption for %s: %s; refresh aborted",
                session_id, outcome,
            )
            return  # corruption: no summary generated, previous revision preserved
        elif outcome.get("tail_withheld"):
            self.logger.info(
                "pre-summary digest withheld the in-flight tail for %s; refresh deferred",
                session_id,
            )
            return  # compact-triggering pair not digested yet: keep the previous revision
        elif "error" in outcome or outcome.get("cancelled"):
            self.logger.warning("pre-summary digest failed for %s: %s", session_id, outcome)
        else:
            session = self.session_manager.get(session_id)  # reread the new digest
```

`build_turn_and_digest` returns `None` when nothing is undigested — and also when memory
or the digest feature is disabled or the session is missing: a disabled digest is an
operator opt-out of digest-backed coverage, so those `None`s deliberately take today's
refresh path and this deliverable claims no compact-turn coverage for them —
`{"error": …}` or `{"cancelled": True, "reason": …}` when it caught its own
failure, and `{"turn_num", "turn_length", "digest_length", …}` when it persisted a turn;
callers branch on that contract, never on exceptions alone. Two 1.2 additions ride on
it: error dicts carry `error_kind` (`"transcript_read"` for `TranscriptReadError`
corruption; absent otherwise), and any outcome may carry `tail_withheld: True` when the
trailing pair was withheld behind an unstable transcript tail. A `tail_withheld`
outcome aborts the dispatcher's summary generation exactly like corruption does — the
compact-triggering pair is deliberately absent from the digest, so a summary persisted
now would be the stale revision this deliverable exists to prevent; the previous
revision is preserved and the next digest (the turn-end digest, or compact_self's own
refresh with its `digest_fallback` route in 1.4.10) is the retry, because the withheld
pair's cursor position guarantees it is covered then. On the daemon loop the
per-session `asyncio.Lock` serialises it against the turn-end digest and
`last_digest_input_hash` dedupes an already-digested turn, so running it from PRE_COMPACT
costs nothing when the turn-end digest already landed. `pre_digest` is decided by
**loop identity**, never by the mere presence of a running loop. `dispatch` resolves
`running = asyncio.get_running_loop()` (or `None`) and `daemon = self.loop` before
creating the coroutine and schedules as follows:

```python
if daemon is not None and daemon.is_running() and running is daemon:
    create_background_task(_run(pre_digest=True), loop=daemon)
elif daemon is not None and daemon.is_running():
    # foreign running loop, or no running loop: the whole digest-and-summary
    # coroutine executes on the daemon loop, where the per-session lock lives
    coro = _run(pre_digest=True)
    try:
        asyncio.run_coroutine_threadsafe(coro, daemon)
    except Exception as exc:  # loop closed between is_running() and the submit
        coro.close()
        self.logger.warning("_dispatch_session_summaries: failed to schedule: %s", exc)
        if done_event:
            done_event.set()
elif running is not None:
    create_background_task(_run(pre_digest=False), loop=running)  # no daemon loop to lock on
else:
    self._dispatch_without_running_loop(_run(pre_digest=False), done_event)  # asyncio.run thread
```

Every `pre_digest=False` path logs `pre-summary digest skipped: no daemon loop`. The
`run_coroutine_threadsafe` branch of today's `_dispatch_without_running_loop` moves
into the second arm above **with its rejection path intact** — a submission that
raises (the daemon loop closing between the `is_running()` check and the call) closes
the never-scheduled coroutine so no "coroutine was never awaited" warning fires, logs
the rejection, and sets `done_event` so a waiting hook or shutdown path is released
exactly as today — so that helper keeps only the `asyncio.run` thread fallback. `HookManager` captures `self._loop` from `asyncio.get_running_loop()` at
construction, so in the daemon `daemon` is the hook loop and a hook call on that loop
takes the first arm; a caller on a second running loop (a test harness or a thread
with its own loop) takes the second arm instead of digesting off-daemon.

The compact_self half is wired end to end. `setup_internal_registries` already holds
`memory_manager_resolver` (the resolver the memory registry uses); it passes it to
`create_session_messages_registry(memory_manager_resolver=...)`, which forwards it to
`register_terminal_tools(memory_manager_resolver=...)`. Inside `compact_self`,
`memory_manager = memory_manager_resolver() if memory_manager_resolver else None` is
resolved per call and handed, with `config_resolver()`, to
`_refresh_compact_handoff_context` and — through
`_schedule_compact_handoff_background_refresh`, the sole scheduler that builds the
background coroutine and therefore gains and forwards the same two keyword
parameters — to `_run_compact_handoff_background_refresh`; both refresh functions gain
`memory_manager` and `config` keyword parameters. Those two call the same
`build_turn_and_digest` before their existing summary/fallback logic, on the daemon loop
they already run on, each under one explicit deadline: the background refresh's
`asyncio.wait_for` already encloses its whole body, while the foreground
`_refresh_compact_handoff_context` — awaited directly by `compact_self` with no timeout
today, because its current work is DB-bound — gains a `compact_handoff_config` keyword
parameter (passed from `compact_self`'s captured `operation_compact_handoff_config`)
and runs its entire pre-digest step (the first attempt plus every tail retry below)
inside `asyncio.wait_for(..., timeout=_compact_handoff_refresh_timeout_seconds(compact_handoff_config))`.
On `TimeoutError` the refresh reloads the session — 1.2's persistence barrier guarantees
the cancelled digest either persisted nothing or persisted its complete state before the
cancellation propagated, so the reload sees settled digest columns — and takes the
existing `digest_fallback` path with reason `"pre-summary digest timed out after
<timeout>s"`. `_refresh_compact_handoff_context` creates one `withheld_capture: dict[str, Any]`
outside the timed coroutine and passes it into every `build_turn_and_digest` call of that
refresh; 1.2 writes the resolved pair and that resolution's own `tail_withheld` flag into it at
resolution time, before the LLM call and before persistence, and each later attempt
overwrites both. The latest resolution of the refresh is therefore known even when the
attempt that reached it returned nothing at all, and a tail that completes between attempts
is recorded as completed rather than left behind a stale withhold. When the capture or a
returned outcome holds a pair, **every non-corruption
terminal failure of the same refresh** — the timeout, a returned `{"error"}` or
`{"cancelled"}` from any attempt, or a raised exception, the *first* attempt's own failure
included — builds its fallback from it (`withheld_pair=<that pair>`, `tail_withheld=<the
captured flag>`, that failure's text as `reason`), so prompt and ledger evidence is never
discarded by a hang or failure at any point after extraction, and the persisted watermark
states what the last resolution actually found; otherwise the fallback is the prior-digest
or raw-tail rendering. The corruption branch
below is the one exception: it persists nothing regardless of retained evidence. A hung digest provider can therefore never hold
`compact_self` open before the compaction command is sent, and the handoff is never marked
ready on an unfinished digest. Outcome handling: `None` or a digested result without `tail_withheld` proceeds to the
summary refresh. A result carrying `tail_withheld: True` never makes the handoff ready on
the prior digest: `_refresh_compact_handoff_context` re-runs `build_turn_and_digest` up to
`COMPACT_HANDOFF_TAIL_RETRY_ATTEMPTS = 3` further times (a module constant in
`_terminal_handoff.py`; each call already includes 1.2's 0.2 s tail re-read, so the
retries add well under two seconds inside the existing refresh budget) and continues with
the first outcome that does not carry `tail_withheld`. If every attempt is withheld it
calls `_persist_compact_handoff_fallback(..., reason="transcript tail in-flight",
tail_withheld=True, withheld_pair=outcome.get("withheld_pair"))`.
`_persist_compact_handoff_fallback` gains the keyword-only `tail_withheld: bool = False`
and `withheld_pair: dict[str, str] | None = None`, and the two are independent: the
rendering is chosen by the pair and the watermark is stamped from the flag. Whenever
`withheld_pair` is not `None` it builds
the fallback from `_compact_handoff_transcript_tail_markdown(session, reason=reason,
withheld_pair=withheld_pair)` **first** — the captured pair is the compact-triggering
turn's prompt and every record of it the last resolution held, which is the stable evidence
the failed attempt would otherwise take with it, whether the digest withheld the tail or
resolved it —
and falls back to the prior-digest markdown only when that returns `None`; and it records `"tail_withheld": <the flag>` beside `reason` and
`source` in the persisted `metadata_json` as the explicit coverage watermark
(`generation_mode="digest_fallback"` with `source_context_hash=None` already fails
`compact_summary_metadata_matches`, so this revision can never pass as fresh). Only after
that persistence is `handoff_ready` set, so the earliest `wait_for_summary` observes either
a digest-backed revision that covers the pair or the tail fallback that carries it — never
the prior digest alone. `_compact_handoff_transcript_tail_markdown` gains the
keyword-only `withheld_pair: dict[str, str] | None = None`. Its default path (no pair) is
unchanged: the last 80 raw lines, suffix-capped at `_COMPACT_HANDOFF_FALLBACK_MAX_CHARS`.
With a pair it reads no raw lines at all — the digest already extracted the turn, and the
raw tail (80 lines from a 256 KiB reader with no notion of turns) cannot reach the opening
prompt of a tool-heavy turn — and renders the pair with the prompt reserved and the
remaining sections under the `_COMPACT_HANDOFF_FALLBACK_MAX_CHARS` cap: a
`## Compact-triggering prompt` section carries `prompt` **whole and uncapped** — it is a
complete payload the model must receive, so `docs/contracts/truncation.md` forbids a marked
head; a prompt larger than the cap simply makes the fallback larger than the cap, and the
delivery tools already bound an oversized summary by reference (2.1's stub swap,
`get_handoff_context`, the proxy's retrieval envelope) — then
`## Tool activity (in flight)` carries the `activity` ledger filled newest-last from the
remaining capacity — when the ledger does not fit, the oldest lines are dropped behind one
`[N earlier ledger lines truncated]` line so the newest complete calls and the
`(no result recorded)` in-flight call survive — then `## Narration so far` carries
`response` only when it fits whole. 1.2's contract guarantees a pair on every withheld
outcome; the raw-tail path stays the defensive default for a withheld outcome without one.
`_run_compact_handoff_background_refresh` applies the same bounded
retry; if the pair is still withheld after its retries it returns without regenerating
(the foreground tail fallback stays in place and the next turn-end digest covers the
pair), otherwise it regenerates the summary from the digest that now contains the pair.
A result with `error_kind == "transcript_read"` persists **nothing** — the refresh
skips both the summary regeneration and the `digest_fallback` persistence, returns the
existing summary unchanged, and records the corruption as the refresh-failure reason,
because a fallback summary would be built by an analyzer read that skipped the very
record the digest refused; any other `{"error"}`/`{"cancelled"}` result, raised
exception, or timeout runs the existing `digest_fallback` path with that error text as
the recorded reason (built from the retained withheld outcome when an earlier attempt of
the same refresh captured one); a missing resolver or a resolver returning `None` skips the digest
step and runs the existing refresh unchanged. **Post-digest session reload**: every branch of
`_refresh_compact_handoff_context` reads the `session` object it was handed
(`compact_summary_metadata_matches(session=…)`, `getattr(session, "digest_markdown")`,
`_valid_existing_summary_markdown(session)`), and `persist_digest_state` returns a
freshly loaded Session — so after every non-error digest outcome, including `None`
(lock contention may mean another caller digested), both refresh functions re-fetch
`session = session_manager.get(session_id)` before their existing metadata, digest,
and fallback checks. Without the reload a session whose first digest just landed would
still take the "digest missing" fallback and a stale metadata match would use
pre-digest counts.

**Acceptance:**

- 1.4.1 - `dispatch` awaits `build_turn_and_digest` before `generate_session_summaries` when a memory manager is configured, and skips it cleanly when not. test: `tests/hooks/test_session_summary_dispatcher.py::test_dispatch_digests_before_summary`.
- 1.4.2 - compact_self's refresh no longer records `digest missing` for a turn whose transcript has an undigested pair: starting from an undigested compact-triggering turn, both refresh functions reload the Session after the digest and the persisted summary revision carries the new digest count and the turn's tool facts. test: `tests/mcp_proxy/tools/sessions/test_compact_self.py::test_refresh_digests_pending_turn_before_fallback`.
- 1.4.3 - `HookManager._dispatch_session_summaries` wires the memory manager and config into the dispatcher through `build_session_summary_dispatcher` in `session_summary_wiring.py`, and `hook_manager.py` no longer constructs `SessionSummaryDispatcher` directly. test: `tests/hooks/test_hook_manager_extra.py::test_dispatch_session_summaries_forwards_memory_manager_and_config`.
- 1.4.4 - `dispatch` treats a returned `{"error"}` or `{"cancelled"}` digest result without `error_kind == "transcript_read"` as a logged failure with the summary still generated, treats a `None` result as nothing-to-digest, and aborts the refresh entirely on `error_kind == "transcript_read"`. test: `tests/hooks/test_session_summary_dispatcher.py::test_dispatch_treats_returned_digest_errors_as_failures`.
- 1.4.5 - `pre_digest` follows loop identity: a call on the configured daemon loop digests in-loop; a call with no running loop while the daemon loop is running digests on the daemon loop via `run_coroutine_threadsafe`; a call with a running loop but no configured daemon loop, and the `asyncio.run` fallback thread, never call `build_turn_and_digest` and log the skip. test: `tests/hooks/test_session_summary_dispatcher.py::test_pre_digest_follows_daemon_loop_identity`.
- 1.4.6 - `setup_internal_registries` forwards `memory_manager_resolver` through `create_session_messages_registry` to `register_terminal_tools`, and `compact_self` resolves it per call. test: `tests/mcp_proxy/tools/sessions/test_mcp_proxy_tools_sessions_registration.py::test_sessions_registry_forwards_memory_manager_resolver`.
- 1.4.7 - compact_self's refresh records a returned digest error as the `digest_fallback` reason and skips the digest cleanly when no resolver is wired. test: `tests/mcp_proxy/tools/sessions/test_compact_self.py::test_refresh_records_digest_error_as_fallback_reason`.
- 1.4.8 - `HookManager.__init__` retains `components.memory_manager`, and a factory-created manager wires it through `_dispatch_session_summaries` into the dispatcher. test: `tests/hooks/test_session_summary_dispatcher.py::test_hook_manager_wires_memory_manager_into_dispatcher`.
- 1.4.9 - The scheduled background branch forwards `memory_manager` and `config` through `_schedule_compact_handoff_background_refresh` into `_run_compact_handoff_background_refresh`. test: `tests/mcp_proxy/tools/sessions/test_compact_self.py::test_scheduled_background_refresh_forwards_memory_manager`.
- 1.4.10 - With a transcript tail that stays partial across every attempt on a compact-triggering turn of 400 records and more than 20,000 characters of tool results (above both the 80-line and the `_COMPACT_HANDOFF_FALLBACK_MAX_CHARS` bounds), compact_self's refresh calls `build_turn_and_digest` `1 + COMPACT_HANDOFF_TAIL_RETRY_ATTEMPTS` times, persists a `digest_fallback` revision rendered from the outcome's `withheld_pair` — its text begins with the compact-triggering turn's full prompt, carries the newest ledger lines including the `(no result recorded)` in-flight call, and stays within the cap — with reason `"transcript tail in-flight"` and `metadata_json["tail_withheld"] is True`, and sets `handoff_ready` only after that persistence; with a tail that completes on the second attempt, no fallback is persisted and the normal refresh runs on the reloaded session. test: `tests/mcp_proxy/tools/sessions/test_compact_self.py::test_tail_withheld_retries_then_persists_transcript_tail_fallback`.
- 1.4.11 - A digest `TranscriptReadError` outcome (`error_kind == "transcript_read"`) is never followed by persistence of a summary revision in the same dispatch or refresh: the dispatcher generates nothing and compact_self's refresh returns the existing summary with the corruption recorded as the failure reason; the case is proven end to end for a malformed newline-terminated final record and for a newline-terminated final record holding invalid UTF-8 bytes, both of which reach the same branch as an interior one rather than the withhold path, while an unterminated fragment split inside a multibyte code point takes the withhold path and persists no corruption failure. test: `tests/hooks/test_session_summary_dispatcher.py::test_transcript_corruption_never_persists_a_summary`.
- 1.4.12 - On PRE_COMPACT, a digest outcome carrying `tail_withheld: True` makes the dispatcher return before `generate_session_summaries`: no summary revision is persisted, the prior revision and its metadata are unchanged, and the next dispatch whose digest includes the withheld pair persists a revision carrying that pair's facts. test: `tests/hooks/test_session_summary_dispatcher.py::test_tail_withheld_defers_summary_until_pair_digested`.
- 1.4.13 - With the daemon loop running in one thread and `dispatch` called from a second, different running loop, `build_turn_and_digest` executes on the daemon loop (asserted via `asyncio.get_running_loop()` identity inside a fake digest) and the per-session lock is acquired there, never on the caller's loop. test: `tests/hooks/test_session_summary_dispatcher.py::test_dispatch_from_foreign_loop_digests_on_daemon_loop`.
- 1.4.14 - Race: with the tail withheld on every foreground attempt, a `wait_for_summary` issued the moment `handoff_ready` is set returns a summary containing the compact-triggering turn's prompt and never the prior-digest fallback text; once the tail completes, the background refresh's digest covers the pair and the regenerated summary returned by a later `wait_for_summary` carries that pair's tool facts. test: `tests/mcp_proxy/tools/sessions/test_compact_self.py::test_immediate_wait_never_sees_prior_digest_fallback`.
- 1.4.15 - `_compact_handoff_transcript_tail_markdown` with a `withheld_pair` whose prompt alone exceeds `_COMPACT_HANDOFF_FALLBACK_MAX_CHARS` returns the complete prompt (no head clamp or `[prompt truncated]` marker exists) and no ledger; with a 2,000-character prompt and a 30,000-character ledger it returns the whole prompt followed by the newest ledger lines behind one `[N earlier ledger lines truncated]` line, within the cap; with `withheld_pair=None` its output is byte-identical to today's raw-tail rendering. test: `tests/mcp_proxy/tools/sessions/test_compact_self.py::test_withheld_pair_fallback_reserves_prompt_within_cap`.
- 1.4.16 - With the daemon loop configured and reported running but `asyncio.run_coroutine_threadsafe` patched to raise `RuntimeError`, `dispatch` from a foreign loop closes the unsubmitted coroutine (no `RuntimeWarning: coroutine ... was never awaited` is emitted under `warnings.catch_warnings(record=True)`), logs `failed to schedule`, sets `done_event`, and never calls `build_turn_and_digest` or `generate_session_summaries`. test: `tests/hooks/test_session_summary_dispatcher.py::test_rejected_daemon_submission_closes_coroutine_and_releases_waiter`.
- 1.4.17 - With `refresh_timeout_seconds` set to 0.2 and `build_turn_and_digest` patched to never return, `compact_self`'s foreground refresh returns within the deadline, the digest coroutine is cancelled, a `digest_fallback` revision is persisted whose reason names the timeout, `handoff_ready` is set only after that persistence, and the compaction command is still sent; with the hang on the second tail retry after a first attempt that returned `tail_withheld` with a `withheld_pair`, the same single deadline covers the retries and the persisted fallback begins with that pair's prompt, carries its newest ledger lines, and records `metadata_json["tail_withheld"] is True` beside the timeout reason; with the hang injected inside `persist_digest_state` (the worker released after the deadline), the refresh's reload observes the completed digest state before the fallback is chosen; parametrized over the other terminal branches, a second retry that instead returns `{"error": "boom"}`, returns `{"cancelled": True, "reason": "shutdown"}`, or raises `RuntimeError` after the same withheld first attempt persists a fallback that begins with the retained pair's prompt, carries its ledger, and records `metadata_json["tail_withheld"] is True` beside that branch's own failure text as `reason`, while a second retry returning `{"error": …, "error_kind": "transcript_read"}` persists nothing. The same holds when the **first** attempt itself fails after extracting the pair — the LLM call raising, being cancelled, or returning `{"error": "boom"}`, `persist_digest_state` raising, and the outer deadline firing mid-attempt so nothing is returned — each persists a fallback that begins with the captured pair's prompt, carries its ledger, and records `metadata_json["tail_withheld"] is True`, proving the fallback is built from `withheld_capture` rather than from a returned outcome. The withheld→complete→failure sequence is pinned over the same terminal branches: a first attempt that withholds pair A, a tail that then completes, and a second attempt that resolves the complete pair B and then fails — parametrized over its LLM call returning `{"error": "boom"}`, returning `{"cancelled": True, "reason": "shutdown"}`, raising `RuntimeError`, `persist_digest_state` raising, and the outer deadline firing mid-attempt so nothing is returned — persists in every case a fallback that begins with **B**'s prompt, renders B's `activity` ledger under `## Tool activity (in flight)` and B's narration under `## Narration so far` as separate sections (which only an uncomposed captured pair can supply), contains none of A's in-flight `(no result recorded)` line, and records `metadata_json["tail_withheld"] is False` beside that branch's own failure text as `reason`. test: `tests/mcp_proxy/tools/sessions/test_compact_self.py::test_foreground_refresh_digest_timeout_falls_back_within_deadline`.

### 1.5 Add the five-CLI activity golden-path parity suite [category: test] (depends: 1.2, 1.3)
`kind: deliverable`

Targets:
- `tests/sessions/test_activity_golden_path.py`
- `tests/sessions/transcripts/fixtures/golden_path/claude.jsonl`
- `tests/sessions/transcripts/fixtures/golden_path/codex.jsonl`
- `tests/sessions/transcripts/fixtures/golden_path/grok.jsonl`
- `tests/sessions/transcripts/fixtures/golden_path/qwen.jsonl`
- `tests/sessions/transcripts/fixtures/golden_path/droid.jsonl`
- `tests/sessions/transcripts/fixtures/golden_path/droid.settings.json`

One hand-written fixture per CLI in that CLI's native envelope (Claude `message.content`
blocks, Codex `response_item` payloads, Grok `_x.ai/session/update` records, Qwen
`message.parts`, Droid `message.content[]` with its `.settings.json` sidecar), each
telling the same two-turn story:

1. Turn 1 — user prompt; the assistant edits `src/pkg/widget.py` with the CLI's native
   edit tool (`Edit`, `apply_patch`, `search_replace`, `write_file`, Droid `tool_use`
   edit), runs `uv run pytest -k widget` with the native shell tool
   (`Bash`, `shell`, `run_terminal_command`, `run_shell_command`, Droid shell), calls
   `gobby-tasks:claim_task {"task_id": "#777"}` through the native MCP dispatcher
   (`mcp__gobby__call_tool`, `gobby__call_tool`, `use_tool`), runs
   `git commit -m "[gobby-#777] feat: widget"` whose successful result is
   `[0.5.0 abc1234] [gobby-#777] feat: widget`, calls
   `gobby-tasks:close_task {"task_id": "#777", "commit_sha": "abc1234"}`, runs
   `cat /nonexistent/widget.log`, which fails with `No such file or directory` in the CLI's
   own failure envelope (Claude/Droid `tool_result.is_error`, Grok
   `tool_call_update.status: "failed"`, Codex `CommandExecution` item `status: "failed"`,
   Qwen `toolCallResult.status: "error"`), runs one successful read-only call
   (`cat README.md`, native shell/read tool) whose output is the sentinel string
   `SENTINEL-README-OUTPUT`, issues one call whose result record never lands in the
   fixture (`tail -f /var/log/widget.log`, still in flight at turn end — its ledger
   line must carry `(no result recorded)` while the successful pytest line stays
   bare), then narrates "Done." The Codex fixture records its work
   the way the current runtime does (envelope verified against live 2026-08
   transcripts): `custom_tool_call` `exec` records carrying JS orchestration, with the
   actual activity in `item_completed` items — `McpToolCall` items (server `gobby`,
   tool `call_tool`, full wrapper `arguments`, `status`, `result`) for the task
   claim/close, one `FileChange` item (`changes: {"src/pkg/widget.py": {"type":
   "update", "unified_diff": …}}`) for the edit, and `CommandExecution` items (argv
   `command`, `status`, `stdout`/`aggregated_output`) for the pytest run, `git commit`
   (stdout `[0.5.0 abc1234] [gobby-#777] feat: widget`), the failed `cat`, and the
   sentinel read — never a direct `shell` or `mcp__gobby__call_tool` function call.
   The Codex fixture is deliberately a **mixed window**: its in-flight
   `tail -f /var/log/widget.log` call appears only as a `custom_tool_call` `exec`
   wrapper with neither a `custom_tool_call_output` record nor a matching
   `item_completed` record (split tail), so the golden assertions prove per-call
   precedence — the wrapper-only call keeps its execution-chain derivation, its inner
   command projected by `pending_exec_command` from the wrapper's JS arguments, and
   renders `(no result recorded)` while every item-covered call appears exactly once.
2. Turn 2 — user prompt "compact"; the assistant calls
   `gobby-sessions:compact_self` and says "Compacting."

`test_activity_golden_path.py` is parametrized over the five sources and does not call
an LLM: a recording fake `llm_service` returns a fixed turn record / summary and captures
every prompt it receives.

- `test_digest_pairs_carry_activity_for_every_cli`: `extract_last_messages` returns the
  same message count and role sequence with `include_tool_activity` on and off; the
  pairs from `_extract_digest_pairs` contain, in the Turn-1 response ledger,
  `src/pkg/widget.py`, `uv run pytest -k widget`,
  `mcp gobby-tasks:claim_task task_id=#777`,
  `mcp gobby-tasks:close_task task_id=#777 commit_sha=abc1234`, `→ commit abc1234`, and a
  `cat /nonexistent/widget.log` line annotated `! failed: ` with the native error text;
  and the Turn-2 response ledger contains `mcp gobby-sessions:compact_self`.
- `test_summary_ground_truth_for_every_cli`: with a digest present (seeded through the
  fake LLM) and the fixture on disk, `_generate_session_summary_core` passes the analyzer
  transcript turns, and the captured summary prompt contains `src/pkg/widget.py` under
  files changed, `abc1234` under commits, `#777` with `claim_task` and `close_task` under
  task progress, and `compact_self` in recent activity.
- `test_successful_readonly_output_excluded_everywhere`: for every CLI, the sentinel
  string `SENTINEL-README-OUTPUT` appears in no digest pair text, no analyzer
  `structured_context`, and no captured prompt (turn record or summary) — proving the
  Constraints retention boundary end to end — while the failed `cat`'s error text and
  the commit output do appear where 1.1 and 1.3 require them.

**Acceptance:**

- 1.5.1 - Five native-envelope fixtures exist, each with the edit, shell command, task claim/close, commit with result, one natively failed call, one successful sentinel read, one in-flight call with no result record, and final compact_self turn; the Codex fixture uses `custom_tool_call` `exec` orchestration with `item_completed` `McpToolCall`/`FileChange`/`CommandExecution` items carrying the actual activity, the edit as a `FileChange` `changes` entry for `src/pkg/widget.py`, its natively failed call as an `McpToolCall` item with `status: completed` and a structured `{"success": false, "error": …}` result (transport success, application failure), and its in-flight call as a wrapper-only split tail (a `custom_tool_call` `exec` with neither output record nor item). file: `tests/sessions/transcripts/fixtures/golden_path/grok.jsonl`.
- 1.5.2 - The parametrized parser→digest test asserts path, command, task ref/action, commit SHA, and final-turn fact in the ledger for every CLI with pair count and role sequence preserved. test: `tests/sessions/test_activity_golden_path.py::test_digest_pairs_carry_activity_for_every_cli`.
- 1.5.3 - The parametrized summary test asserts the same facts reach the summary prompt's ground truth for every CLI. test: `tests/sessions/test_activity_golden_path.py::test_summary_ground_truth_for_every_cli`.
- 1.5.4 - Every fixture's failed call is annotated `! failed:` with its native error text and, rendered under a forced five-line cap, survives ahead of the successful read-only calls. test: `tests/sessions/test_activity_golden_path.py::test_failed_call_annotated_and_protected_for_every_cli`.
- 1.5.5 - The sentinel read's output text appears in no digest pair, no analyzer structured context, and no captured prompt for any of the five CLIs, while failed-call error text and commit output do. test: `tests/sessions/test_activity_golden_path.py::test_successful_readonly_output_excluded_everywhere`.
- 1.5.6 - For every CLI, the successful `uv run pytest -k widget` line and the edit line (the Codex `FileChange` item included) render bare and the in-flight call's line renders `(no result recorded)` in the same Turn-1 ledger, so a passed test run is distinguishable from missing evidence end to end. test: `tests/sessions/test_activity_golden_path.py::test_success_and_missing_result_distinguishable_for_every_cli`.
- 1.5.7 - The Codex mixed window keeps the wrapper-only split-tail call as an execution-chain entry naming `tail -f /var/log/widget.log` (projected from the pending wrapper's JS arguments) alongside the item-derived entries, with no call dropped or double-counted in either the ledger or the analyzer turns. test: `tests/sessions/test_activity_golden_path.py::test_codex_mixed_window_keeps_unmatched_wrapper`.

## P2: Grok compact handoff delivery
`kind: framing`

**Goal**: After a Grok compaction, the continuation prompt leads straight to
`wait_for_summary`, whose response carries everything the retired turn_start injection
was supposed to deliver, and the one-shot variables are cleared only on delivery.

### 2.1 Deliver the compact continuation block through wait_for_summary and get_handoff_context [category: code]
`kind: deliverable`

Targets:
- `src/gobby/sessions/compact_handoff_block.py`
- `src/gobby/mcp_proxy/tools/sessions/_handoff.py::wait_for_summary`
- `src/gobby/mcp_proxy/tools/sessions/_handoff.py::get_handoff_context`
- `src/gobby/mcp_proxy/tools/sessions/_handoff.py::register_handoff_tools`
- `src/gobby/workflows/state_manager.py::*` — scope-reason: `SessionVariableManager` gains `claim_compact_continuation` and `is_compact_continuation_pending`, and the module gains the `if TYPE_CHECKING:` import of `ContinuationBlock`
- `src/gobby/mcp_proxy/services/result_offload.py::*` — scope-reason: add the module-level `_CONTINUATION_DELIVERY_TOOLS` tuple and the content-scoped exemption check in `_maybe_offload_sync` for a delivered `continuation`
- `src/gobby/hooks/event_handlers/_session_start/in_place_compact.py::*` — scope-reason: `apply_in_place_compact_context_loss` adds one key to the `updates` dict it already merges, clearing `compact_continuation_rendered` in the same write that arms the pending flag; the module has no other symbol and its signature is unchanged, so its `_misc.py` PostCompact caller needs no edit
- `tests/hooks/test_session_handoff_handlers.py::*` — scope-reason: add the replay-cache invalidation case to the existing `apply_in_place_compact_context_loss` coverage
- `tests/sessions/test_compact_handoff_block.py`
- `tests/workflows/test_session_variable_manager.py::*` — scope-reason: add claim_compact_continuation cases
- `tests/mcp_proxy/test_mcp_tools_session_messages.py::*` — scope-reason: add continuation-block and one-shot-clearing cases
- `tests/mcp_proxy/tools/test_handoff_coverage.py::*` — scope-reason: unchanged consumer of `register_handoff_tools`/`get_handoff_context`; its non-pending assertions keep passing with no `continuation` key, listed for consumer closure
- `tests/sessions/test_clear_continuation.py::*` — scope-reason: unchanged consumer of `register_handoff_tools`/`get_handoff_context`, listed for consumer closure
- `tests/mcp_proxy/services/test_result_offload.py::*` — scope-reason: add the delivery-tool exemption and over-threshold regression

New pure module `compact_handoff_block.py`:

```python
COMPACT_CONTINUATION_ONE_SHOT_VARIABLES = (
    "compact_handoff_inject_pending",
    "compact_resume_required_skills",
    "compact_resume_advisory_skills",
    "pending_context_reset",
)

class ContinuationBlock(NamedTuple):
    text: str
    required_by_reference: bool

def render_compact_continuation_block(
    variables: dict[str, Any],
    *,
    session_id: str,
    fits: Callable[[str], bool],
    allow_required_reference: bool = False,
) -> ContinuationBlock | None:
    """Render the same content as the retired inject-compact-handoff-on-prompt template.

    Sections, each omitted when empty, rendered in priority order:
    1. "## Required Skill Reload": skill_fetch_batch_directive(required - loaded_skills).
    2. variables["task_context"] verbatim.
    3. "## Durable Tool-Call Evidence": variables["mcp_calls"] as ``- `server`: tool, tool``
       with the same preamble sentence as the SessionStart template.
    4. "## Advisory Skill Reload": bullet list of advisory - loaded_skills.
    5. "## Global User Profile": variables["user_profile_content"] unless
       variables.get("is_spawned_agent").
    Wrapped in ``<!-- gobby:injected-context:begin -->`` / ``end`` markers under
    "## Continuation Context". Returns ``ContinuationBlock(text, required_by_reference)``:
    ``text`` is the block and ``required_by_reference`` is true only when section 1 was
    rendered in its reference form (below). ``ContinuationBlock(text="", ...)`` is returned
    only when the source variables hold **no** section at all (no required or advisory
    skills, no task context, no MCP ledger, no profile); present content that cannot be
    delivered returns ``None`` (below), never an empty block.

    ``fits`` is the **final serialized-size predicate** supplied by the caller: it
    answers whether the complete response object carrying a candidate block stays
    within the provider budget, ``_serialized_len({**result, "continuation":
    candidate}) <= inline_context_budget_for(source)``, where ``_serialized_len`` is
    ``len(json.dumps(value, ensure_ascii=False, default=str))`` — the size of the
    representation the MCP transport sends (the SDK serializes results with pydantic's
    ``model_dump_json``: compact, non-ASCII preserved) and the measure
    ``result_offload._serialized_size`` applies to retrieval envelopes — and
    ``inline_context_budget_for`` (from ``gobby.hooks.context_limits``) is the existing
    per-provider inline budget with live config overrides. The proxy's ``threshold_chars``
    decision measures ``_serialize_success_result`` (``indent=2``, ASCII-escaped), which is
    never smaller than the wire form, so a base the proxy serves inline is within
    ``threshold_chars`` on the wire too. Fitting the serialized response, not the raw
    block, means JSON escaping of quote-, backslash-, control-, and non-ASCII content is
    charged exactly; there is no framing margin constant.

    Section 1 (the required-skill directive) is rendered **in full** from the
    pre-clear variables and is never truncated: if ``fits`` rejects the block holding
    section 1 alone, the renderer returns ``None`` — nothing is delivered and nothing
    is cleared — unless ``allow_required_reference`` is true, in which case section 1 is
    rendered in its **reference form** instead: a fixed short paragraph naming the count
    of required skills and directing the model to read the list with
    ``get_variable(session_id="<session_id>", name="compact_resume_required_skills")``
    on the ``gobby`` proxy — ``session_id`` is the compacted session's id passed in by
    the caller, because both top-level ``get_variable`` tools require it and reject an
    empty one — and load each entry with ``get_skill`` in order before continuing
    (about 330 characters, no skill names); if ``fits`` rejects even that block the
    renderer returns ``None``.
    Otherwise sections 2–5 are added in priority order under
    ``docs/contracts/truncation.md``: each is a complete payload, so it is delivered
    **whole** or replaced by one pointer line, never prefix-cut. Sections 2, 3, and 5
    point at their still-readable variables — ``… task_context omitted: read it with
    get_variable(session_id="<session_id>", name="task_context")`` (``mcp_calls`` and
    ``user_profile_content`` likewise; none of the three is a one-shot, so the pointer
    stays valid after the claim). Section 4 is a list of whole items, delivered entirely
    or replaced by ``… N advisory skill reloads omitted`` (whole-item omission of
    advisory names; that list is consumed by the claim). A present section whose pointer
    or omission line does not fit either makes the renderer return ``None``: the block is
    all-or-nothing at the granularity of whole sections and pointer lines, so ``None`` is
    the single retryable outcome for present content and the caller leaves every
    one-shot armed. Every returned block therefore satisfies ``fits`` as serialized and
    delivers every present section whole or by reference.
    """
```

`skill_fetch_batch_directive` comes from `gobby.skills.formatting`;
`SessionVariableManager` (with `get_variables` and `merge_variables`) comes from
`gobby.workflows.state_manager`. Because `compact_handoff_block` imports
`SessionVariableManager` at runtime, `state_manager.py` imports `ContinuationBlock`
for the `render` annotation below under `if TYPE_CHECKING:` only and writes the
annotation as a string (`state_manager.py` has no `from __future__ import
annotations`), so it type-checks without an import cycle.

Capture-and-clear is one atomic mutation on `SessionVariableManager`, following the
`claim_startup_context` precedent (`_mutate_variables` runs under
`transaction_immediate` with the per-session `SessionVariableMutation` lock, so
concurrent callers serialise and exactly one wins):

```python
def claim_compact_continuation(
    self,
    session_id: str,
    render: "Callable[[dict[str, Any]], ContinuationBlock | None]",
    *,
    allow_replay: bool = False,
) -> str | None:
    """Atomically consume the one-shot compact continuation.

    When ``compact_handoff_inject_pending`` is truthy, render the block from the
    pre-clear variables inside the mutation. ``render`` returns a ``ContinuationBlock``
    (its ``text`` is the block, or ``""`` only when the variables hold no section) or
    ``None`` when present content — the required section, or any lower-priority
    section even as a pointer line — cannot be delivered. A block or ``""`` is a delivery — ``""``
    is the successful no-op: there is nothing to inject, so the one-shots are consumed
    exactly as for a block and the caller attaches no ``continuation`` key. For both: set
    ``compact_handoff_inject_pending=False``, ``compact_resume_required_skills=[]``,
    ``compact_resume_advisory_skills=[]``, and ``pending_context_reset=False`` in the
    same mutation and return ``block.text`` — except that a block delivered with
    ``required_by_reference`` leaves ``compact_resume_required_skills`` populated, because
    that variable is the delivery target the reference form points at. ``None`` is the
    single retryable outcome: it writes nothing and leaves every one-shot armed. The retired turn_start rule cleared
    ``pending_context_reset`` on delivery (PreCompact sets it, and
    ``observer_context_usage`` suppresses context-pressure guidance while it is true;
    no Grok session_start ever fires to run clear-pending-context-reset-on-start), so
    delivery — including the empty no-op — must clear it here too. Otherwise (not
    pending, or required section too large) return ``None`` and write nothing —
    timeouts, missing context, and errors leave every one-shot, including
    ``pending_context_reset``, untouched. Timeouts, missing context, errors, and present
    content that does not fit all leave the one-shots armed.

    A delivery is **replayable for the one tool whose result can be abandoned**. The MCP
    wait wrapper (`mcp_proxy/wait_tools.py::_await_with_guard`) shields ``wait_for_summary``,
    returns ``{"success": True, "completed": False, "background_call_continues": True}`` on
    its own deadline, and hands the still-running call's result to
    ``_consume_background_result``, which discards it — so a claim that commits after that
    deadline (or a claim cancelled in its worker thread) would otherwise consume the
    one-shots with nothing delivered. ``allow_replay`` is true only for ``wait_for_summary``,
    the only delivery path behind that wrapper: its consuming mutation also writes
    ``compact_continuation_rendered = {"text": block.text}``, and a later claim on a
    non-pending session returns that cached text and writes nothing. ``get_handoff_context``
    passes ``allow_replay=False`` and neither writes nor reads the cache, so it never
    re-delivers a block another call already delivered and the reference target stays free
    of continuation content. **Delivery cardinality is preserved for every mixed-tool
    sequence**: whichever tool claims first, the other returns no ``continuation``. The one
    bounded exception is two concurrent ``wait_for_summary`` calls, where the loser replays
    the identical block — a duplicated identical block is the deliberate trade against
    silent loss, and it is the only sequence in which two responses carry ``continuation``.

    The cache is superseded by the **next compaction**, not by a timestamp comparison:
    ``apply_in_place_compact_context_loss`` clears ``compact_continuation_rendered`` in the
    same ``merge_variables`` write that arms ``compact_handoff_inject_pending``, and clears
    it unconditionally so a session with ``auto_inject_handoff`` disabled cannot keep
    replaying an older generation either. While the flag is pending the replay branch is
    unreachable — the first branch renders fresh — so a claim can only ever replay a block
    from the current generation, and a daemon restart changes nothing because the cache is a
    session variable. Keying the cache on ``compact_notification_started_at`` would not
    hold: ``SessionNotificationRouter._clear_compact_marker`` blanks that variable on pause,
    expiry, and notification-deadline handling, which would both strand a live cache and let
    a later blanked generation match a stale one.
    """

    def mutate(variables: dict[str, Any]) -> tuple[str | None, bool]:
        if not variables.get("compact_handoff_inject_pending"):
            cached = variables.get("compact_continuation_rendered")
            if allow_replay and isinstance(cached, dict):
                return str(cached.get("text") or ""), False  # replay, no write
            return None, False
        block = render(dict(variables))
        if block is None:
            return None, False  # required section cannot fit: stay armed
        variables["compact_handoff_inject_pending"] = False
        if not block.required_by_reference:  # by reference: the list is the delivery target
            variables["compact_resume_required_skills"] = []
        variables["compact_resume_advisory_skills"] = []
        variables["pending_context_reset"] = False
        if allow_replay:  # wait_for_summary only: its result can be abandoned
            variables["compact_continuation_rendered"] = {"text": block.text}
        return block.text, True

    return self._mutate_variables(session_id, mutate)
```

`compact_handoff_block.py` also owns the helper both tools call (it is new code, and
`_handoff.py` keeps exact symbol targets):

```python
def attach_compact_continuation(
    result: dict[str, Any],
    db: Any,
    session_id: str,
    *,
    source: str | None,
    allow_base_stub: bool,
) -> None:
    budget = inline_context_budget_for(source)

    def fits(candidate: str) -> bool:
        return _serialized_len({**result, "continuation": candidate}) <= budget

    manager = SessionVariableManager(db)
    unstubbed = dict(result)
    try:
        block = manager.claim_compact_continuation(
            session_id,
            lambda variables: render_compact_continuation_block(
                variables, session_id=session_id, fits=fits
            ),
            allow_replay=allow_base_stub,  # wait_for_summary only
        )
        if block is None and allow_base_stub and manager.is_compact_continuation_pending(session_id):
            _swap_base_context_for_reference_stub(result)  # wait_for_summary only
            block = manager.claim_compact_continuation(
                session_id,
                lambda variables: render_compact_continuation_block(
                    variables, session_id=session_id, fits=fits, allow_required_reference=True
                ),
                allow_replay=True,
            )
            if block is None:  # not even the reference form fits: return the real summary
                logger.error("compact continuation budget cannot hold the stub for %s", session_id)
                result.clear()
                result.update(unstubbed)
    except Exception:
        # the claim's transaction_immediate rolled back: every one-shot is still armed
        logger.exception("compact continuation claim failed for %s; base handoff returned", session_id)
        result.clear()
        result.update(unstubbed)
        if allow_base_stub:  # bounded pending response: the retry signal must not be offloaded
            _swap_base_context_for_reference_stub(result)
        result["continuation_pending"] = True
        return
    if block and not fits(block) and allow_base_stub:
        _swap_base_context_for_reference_stub(result)  # replayed beside a larger base
    if block and fits(block):
        result["continuation"] = block
    elif block:  # a replayed block that cannot fit even beside the stub: retry later
        logger.warning("cached compact continuation does not fit for %s", session_id)
        result.clear()
        result.update(unstubbed)
```

Every block the helper attaches passes the **current** response's `fits` predicate. A
freshly rendered block already satisfies it (the renderer was handed the same closure over
the same live `result`), so the check is a no-op there; a **replayed** block was fitted
beside whatever base its original call carried, which can be smaller than the base it is
replayed beside, and the content-scoped offload exemption would otherwise let that combined
response past the provider budget unmeasured. `wait_for_summary` retries the fit after its
stub swap — the stub is ~330 characters, so a block that fitted once fits again — and a
block that still does not fit leaves the response un-stubbed and without `continuation`,
with the cache intact for a later call. `get_handoff_context` never replays, so the check
can only pass there.

A claim or render exception — a database lock or transaction failure, a renderer defect —
is caught outside the transaction (`_mutate_variables` has already rolled back, so all four
one-shots stay armed), logged with the session id, and answered with `continuation_pending:
true` and no `continuation`. **The base that carries that signal differs by tool, and the
difference is the point.** `wait_for_summary` (`allow_base_stub=True`) returns the same
~330-character reference stub it already owns, so its pending response is bounded whatever
the summary's size and the retry signal can never be replaced by a retrieval envelope; the
real summary stays retrievable through `get_handoff_context`, exactly as after a stub-swap
delivery. `get_handoff_context` (`allow_base_stub=False`) returns its complete base
byte-identically and is offloaded by the ordinary rules when oversized — the same bounded
reference path it already takes on its non-claim result. Either way the base handoff is
never lost, the delivery opportunity is not consumed, and the next `wait_for_summary` or
`get_handoff_context` call claims again. `continuation_pending` appears only on that path
and is the one retryable signal beside `completed: false` in 2.2's Grok directive.

That signal must survive the proxy. The offload exemption is keyed on a **delivered**
`continuation`, and `ToolResultOffloader._build_envelope` replaces an over-threshold
result with `{offloaded, server_name, tool_name, content_kind, total_chars, stored_chars,
retrieval_available, guidance, result_id, structure, preview}` — no `success`, `completed`,
`found`, or `continuation_pending`. So the pending response is made inherently bounded
rather than teaching the shared offloader per-tool status fields: `wait_for_summary`
(`allow_base_stub=True`) swaps in the same ~330-character reference stub it already owns,
so its pending response is far below `threshold_chars` and reaches the model intact with
the retry directive pointing at `get_handoff_context` meanwhile. `get_handoff_context`
(`allow_base_stub=False`) keeps its real base and offloads normally when oversized —
exactly the bounded reference path §2.1 already specifies for its non-claim result — and
because every one-shot is still armed, the next `wait_for_summary` delivers the
continuation in full.

`fits` closes over the live `result`, so after the base-stub swap the same predicate
measures the stubbed response. The first claim renders inside the mutation from the
pre-clear variables; when the required-skill section does not fit intact the renderer
returns `None`, `mutate` writes nothing, and the claim returns `None` with the one-shots
still armed; a lower-priority section that cannot fit even as its pointer line makes the
renderer return `None` the same way, so present content is never silently consumed. An
empty render (`""`, only when the variables hold no section) is terminal: the claim consumes
the one-shots and returns `""`, so the retry condition (`block is None`) is never met, the
base summary is never stubbed for a session with nothing to deliver, and
`pending_context_reset` clears on that call — the pending flag has exactly three exits:
delivered (every present section whole or by reference), consumed empty, or still armed
because present content does not fit beside the stub even by pointer. The
post-swap retry renders with `allow_required_reference=True`: when the intact directive
still does not fit, section 1 becomes the reference form, the claim consumes
`compact_handoff_inject_pending`, `compact_resume_advisory_skills`, and
`pending_context_reset` but leaves `compact_resume_required_skills` populated, and the
model reads the list through the existing `get_variable` proxy tool with the session id
the block spells out (the tool requires `session_id` and returns the named session
variable) and loads each skill with `get_skill` — the 2.2 Grok directive
already says to follow every instruction in `continuation` before continuing.
`persist_compact_resume_required_skills` rewrites the variable on the next `compact_self`,
so the retained list never leaks into a later compaction. The only `None` left after the
retry is a budget that cannot hold the stub plus a ~330-character block — an
`additional_context_limits` misconfiguration, logged at error level with the session id
and unreachable at the 9,500-character default. When that happens the swap is reverted so
the response carries the real summary; the stub is paid for only by a delivery. `is_compact_continuation_pending(session_id)` is a read
of `compact_handoff_inject_pending` that distinguishes "nothing pending" (no retry) from
"pending but did not fit" (retry after the swap). The base-stub swap
reuses the existing by-reference precedent from the SessionStart injection path: the
oversized summary/context value is replaced with the short pointer text ("pre-compaction
summary exceeds the inline handoff budget — call `get_handoff_context` with your session
ref to load the full summary"), which is tiny, so after a swap the required section
always fits for any realistic skill list (a required list whose full directive cannot fit even
beside the stub is delivered in reference form on that retry — acceptance 2.1.14 and
2.1.18). `allow_base_stub=True` is passed only by
`wait_for_summary` (its summary remains retrievable through `get_handoff_context`, which
by then carries no continuation — the one-shots were just claimed — so there is no
recursion). `get_handoff_context` passes `allow_base_stub=False`: it is the reference
target and never stubs its own payload; if its base alone leaves no room, it returns
without `continuation` and without consuming, and — because it is **not** offload-exempt
on that path (below) — the proxy offloads a base above `threshold_chars` to a bounded
`gobby-results` envelope and serves a base at or below it inline, exactly as it does for
any tool today. The bound on a non-claim `get_handoff_context` response is therefore
the proxy's `threshold_chars`, not `inline_context_budget_for(source)`: a base between
the two (above the inline budget, at or below the threshold) is returned inline without
`continuation` and without consuming — the inline budget governs only what this plan
adds to a response — and the still-armed one-shots are served by the next
`wait_for_summary`, whose stub swap guarantees a fit. The reference target is
non-recursive in every case: the reference form points at `get_variable`, never back at
a delivery tool. The armed state is served by the next delivery
opportunity — `wait_for_summary`'s swap path guarantees one exists, and the 2.2
continuation prompt directs Grok there first.

`wait_for_summary` is `async` and already keeps its own database read off the loop
(`await asyncio.to_thread(session_manager.get, resolved_id)`); the claim opens a
synchronous `transaction_immediate` and runs render probes, so it follows the same pattern:
`await asyncio.to_thread(attach_compact_continuation, result, session_manager.db,
resolved_id, source=session.source, allow_base_stub=True)` on both of its
`completed: true` returns (the stale → `live_handoff_context` branch and the
`summary_markdown` branch), after the response dict is built — the helper mutates `result`
in place inside the worker and the coroutine touches `result` only after the await
returns. `get_handoff_context` is a synchronous tool and calls the helper directly.
`get_handoff_context` calls it exactly once with `allow_base_stub=False`, on the final
`success: true, found: true, has_context: true` result after child-session validation,
with `parent_session.id` (the Option 2/3 lookups never define `resolved_id`). Nothing is
claimed on `completed: false` timeouts, on any `found: false` return, on the
`found: true, has_context: false` "no handoff context" return, or on the child-link error
returns. Sessions without the pending flag (Claude/Codex, or Grok sessions already
served) get no `continuation` key and no variable writes; a pending session whose block
renders empty gets no `continuation` key either, and its one-shots are consumed on that
call. `apply_in_place_compact_context_loss`
keeps arming the flag on Grok PostCompact and adds exactly one key to the `updates` dict it
already merges: `compact_continuation_rendered` is cleared there, unconditionally and in
the same write, so the replay cache never outlives the compaction that produced it.

Delivery must survive the proxy's result offloader. `ToolResultOffloader` replaces any
successful proxied result whose serialized text exceeds `threshold_chars` (default
15,000) with a retrieval envelope **after** the tool handler returns, which would strip
the just-claimed `continuation` while the one-shots are already cleared. The exemption
is **content-scoped, not tool-wide**: `result_offload.py` gains
`_CONTINUATION_DELIVERY_TOOLS = ("gobby-sessions/wait_for_summary",
"gobby-sessions/get_handoff_context")`, and `_maybe_offload_sync` returns the result
unchanged when `identity` is one of them **and** the result is a dict with a top-level
`continuation` key (checked right after the `_MANDATORY_EXEMPT_TOOLS` patterns; neither
tool joins that tuple). A delivered response already satisfies `fits`, so it is
model-visible at its full size with the claim-and-clear atomic; a non-claim response —
`get_handoff_context` with an oversized base, or either tool for a session with nothing
pending — is offloaded by the existing rules, which is exactly the bounded reference
path an oversized base needs. No offload/claim ordering contract is required because the
claim happens inside the handler and the offloader only ever sees a finished result.

**Acceptance:**

- 2.1.1 - `render_compact_continuation_block` renders MCP ledger, required/advisory skill directives, task context, and profile as `ContinuationBlock(text=<block>, required_by_reference=False)`, and returns `ContinuationBlock(text="", required_by_reference=False)` when all are empty — the bare `""` is returned only by `claim_compact_continuation`, which unwraps `block.text`. test: `tests/sessions/test_compact_handoff_block.py::test_render_compact_continuation_block_sections`.
- 2.1.2 - `wait_for_summary` returns `continuation` for a session with `compact_handoff_inject_pending` set and, for a direct (non-reference) delivery, clears the four one-shot variables in that one write; a second `wait_for_summary` call replays the identical `continuation` from `compact_continuation_rendered` with no further variable write, a `get_handoff_context` call after that delivery carries no `continuation` at all, and a call made after the next compaction — whose single arming write clears the cache and re-arms the flag — renders a fresh block instead of the cached one. test: `tests/mcp_proxy/test_mcp_tools_session_messages.py::test_wait_for_summary_delivers_compact_continuation_once`.
- 2.1.3 - `completed: false` and `found: false` responses never clear the one-shot variables. test: `tests/mcp_proxy/test_mcp_tools_session_messages.py::test_wait_for_summary_timeout_keeps_continuation_pending`.
- 2.1.4 - `get_handoff_context` carries the same block under the same conditions. symbol: `get_handoff_context`.
- 2.1.5 - `claim_compact_continuation` renders inside the mutation and returns the block while clearing the four one-shots in one write; with `allow_replay=True` that same write stores `compact_continuation_rendered`, and with `allow_replay=False` no cache is written at all; a pending session whose block renders empty has the same four one-shots cleared in one write, caches `""` only under `allow_replay=True`, and receives `""`; a following `allow_replay=True` call returns the cached text with no write while a following `allow_replay=False` call returns `None`; and it returns `None` with no write for sessions never pending, when the renderer returns `None`, and once `apply_in_place_compact_context_loss` has cleared the cache. test: `tests/workflows/test_session_variable_manager.py::test_claim_compact_continuation_is_one_shot`.
- 2.1.6 - Concurrent `wait_for_summary` and `get_handoff_context` calls (two threads, one pending session) yield exactly one response with `continuation` and the one-shots are cleared once, in **both** claim orders: the `get_handoff_context` loser reads no cache, and when `get_handoff_context` wins it writes none, so the `wait_for_summary` loser has nothing to replay. Two concurrent `wait_for_summary` calls are the one documented exception — the loser replays the identical block — and even there the one-shots are cleared once and both blocks are byte-identical. test: `tests/mcp_proxy/test_mcp_tools_session_messages.py::test_compact_continuation_consumed_exactly_once_under_concurrency`.
- 2.1.7 - With a stale `summary_markdown`, `wait_for_summary` returns the live handoff context, carries `continuation` exactly once, and clears the one-shots on that delivery. test: `tests/mcp_proxy/test_mcp_tools_session_messages.py::test_wait_for_summary_stale_live_branch_delivers_continuation_once`.
- 2.1.8 - A pending session whose variables hold no section at all gets no `continuation` key, keeps its full `summary_markdown` (no stub swap), and leaves the call with `compact_handoff_inject_pending` and `pending_context_reset` both false; a second call is a plain non-pending response. test: `tests/mcp_proxy/test_mcp_tools_session_messages.py::test_wait_for_summary_pending_empty_block_consumes_one_shots`.
- 2.1.9 - `get_handoff_context` leaves the one-shots untouched on the no-context, child-project-mismatch, and invalid-child returns, and claims with the parent session id on a project/source filtered lookup. test: `tests/mcp_proxy/test_mcp_tools_session_messages.py::test_get_handoff_context_claims_only_on_successful_delivery`.
- 2.1.10 - Delivery and the empty no-op both clear `pending_context_reset` in the same atomic claim, so context-pressure guidance resumes; every non-delivery branch (timeout, not found, required section too large) leaves it true. test: `tests/workflows/test_session_variable_manager.py::test_claim_compact_continuation_clears_pending_context_reset`.
- 2.1.11 - A `gobby-sessions/wait_for_summary` or `gobby-sessions/get_handoff_context` result carrying a top-level `continuation` key is never offloaded even above `threshold_chars`, while the same tools' results without that key above the threshold are offloaded to the normal retrieval envelope. test: `tests/mcp_proxy/services/test_result_offload.py::test_continuation_delivery_results_are_exempt_only_when_delivered`.
- 2.1.12 - With a maximum-size profile and a large MCP ledger, every block `render_compact_continuation_block` returns satisfies the supplied `fits` predicate as serialized: the required-skill directive survives intact, every lower-priority section is either present whole or replaced by its pointer/omission line with no section text prefix-cut, and delivery still clears the one-shots. test: `tests/sessions/test_compact_handoff_block.py::test_render_compact_continuation_block_respects_fit_predicate`.
- 2.1.13 - The complete serialized response is bounded for both delivery tools: with a maximum-size summary plus maximum profile and MCP ledger, `wait_for_summary`'s response (base + `continuation` + framing) measures within `inline_context_budget_for(source)` by `_serialized_len` via the base-stub swap with the one-shots claimed exactly once, and `get_handoff_context` with an oversized base returns without `continuation`, without consuming the one-shots, and — run through `ToolResultOffloader` — arrives as a retrieval envelope under `threshold_chars`. A replayed block is re-fitted against the response it is replayed beside, never against the one it was rendered for: after an abandoned claim on a stubbed response, a retry whose base is the full-size summary swaps in the stub and measures within the budget again, and with the budget lowered so even the stubbed response cannot hold the cached block the retry returns the real base with no `continuation` and the cache intact for a later call. test: `tests/mcp_proxy/test_mcp_tools_session_messages.py::test_delivery_response_total_stays_within_provider_budget`.
- 2.1.14 - A required-skill list whose full directive does not fit beside the base leaves the one-shots armed and delivers no `continuation` through `get_handoff_context`, while `wait_for_summary` delivers the complete directive (every skill named) after the base-stub swap when it fits beside the stub, and otherwise the reference form naming `get_variable`, the session id, and `compact_resume_required_skills`; no delivered block ever contains a truncated required-skill section. test: `tests/sessions/test_compact_handoff_block.py::test_required_skill_section_is_all_or_nothing`.
- 2.1.15 - Escape-heavy and non-ASCII maximum payloads (task context and profile made of quotes, backslashes, control characters, and astral-plane emoji, whose JSON form is more than twice their raw length) still produce a response within the provider budget as serialized by `_serialized_len`, and that response re-serialized with `json.dumps(indent=2)` (the proxy's `threshold_chars` measure) is never smaller than the wire measure, proving the fit is measured on the final wire response rather than raw characters. test: `tests/sessions/test_compact_handoff_block.py::test_fit_predicate_charges_json_escaping`.
- 2.1.16 - `get_handoff_context` for a session with nothing pending and an over-threshold context is offloaded by the proxy exactly as today, and its one-shot variables are untouched; for a pending session whose base measures above `inline_context_budget_for(source)` and at or below `threshold_chars` — including under a live `additional_context_limits` override that moves the inline budget — the response is served inline without `continuation`, the one-shots stay armed, and the following `wait_for_summary` delivers the block via the stub swap. test: `tests/mcp_proxy/test_mcp_tools_session_messages.py::test_get_handoff_context_non_claim_path_offloads_normally`.
- 2.1.17 - Repeated `wait_for_summary` calls against a pending session converge to a stable replayed result in one call for every render outcome, each later call re-returning the cached text without writing while an interleaved `get_handoff_context` never carries `continuation`: a non-empty block is delivered once; an empty block is consumed once with `summary_markdown` intact; a required section that cannot fit even beside the stub is delivered once in reference form with `compact_resume_required_skills` still holding every name and the other three one-shots cleared, and a second call replays that same reference block; only a budget below the stub plus the reference block leaves the one-shots armed with nothing cached, with each response carrying the real, unstubbed summary and an error logged; and no call sequence leaves `pending_context_reset` true after a delivered or empty outcome. test: `tests/mcp_proxy/test_mcp_tools_session_messages.py::test_compact_continuation_repeated_calls_reach_terminal_state`.
- 2.1.18 - End to end for a Grok session whose required-skill directive exceeds `inline_context_budget_for("grok")` beside the stub: `wait_for_summary` returns `continuation` in reference form within the budget as serialized and naming that session's id, the test extracts the rendered `get_variable(session_id="…", name="compact_resume_required_skills")` call from the block and invokes the proxy's `get_variable` tool with exactly those arguments, which returns every required skill name, and a following `get_handoff_context` carries no `continuation`; `claim_compact_continuation` with a reference-form renderer result clears `compact_handoff_inject_pending`, `compact_resume_advisory_skills`, and `pending_context_reset` in one write while leaving `compact_resume_required_skills` intact. test: `tests/mcp_proxy/test_mcp_tools_session_messages.py::test_oversized_required_directive_is_recoverable_by_reference`.
- 2.1.19 - With `_mutate_variables` blocked on an event inside `claim_compact_continuation`, an unrelated task on the same event loop advances while `wait_for_summary` awaits the claim, and the response carries `continuation` once the event releases; `get_handoff_context` performs the claim synchronously on its own thread. test: `tests/mcp_proxy/test_mcp_tools_session_messages.py::test_wait_for_summary_claim_runs_off_the_event_loop`.
- 2.1.20 - With no required skills and task, profile, and MCP-ledger content that cannot fit beside `get_handoff_context`'s base even as pointer lines, the renderer returns `None`, `get_handoff_context` delivers no `continuation` and leaves all four one-shots armed, and the next `wait_for_summary` delivers every section whole or by pointer after its stub swap; a session whose variables hold no section yields `ContinuationBlock(text="")` and is consumed as the empty no-op; a delivered `task_context` pointer line names `get_variable` with the session id and variable name, and invoking the proxy's `get_variable` with exactly those arguments returns the complete value. test: `tests/sessions/test_compact_handoff_block.py::test_present_content_is_delivered_whole_or_by_pointer`.
- 2.1.22 - The continuation survives every abandonment boundary of the MCP wait wrapper: with the handler made to complete its claim after `_await_with_guard`'s deadline (so the caller receives `completed: false` and `_consume_background_result` discards the real result), the next `wait_for_summary` returns the identical `continuation` text from the cache; with the `asyncio.to_thread` claim cancelled after its transaction commits, the same replay holds; with the cancellation delivered before the commit, nothing is consumed and the retry renders fresh. The replay's lifetime is pinned across the variable's live owners: a `get_handoff_context` call issued between the abandoned claim and the retry carries no `continuation` and leaves the cache intact for the next `wait_for_summary`; `SessionNotificationRouter._clear_compact_marker` blanking `compact_notification_started_at` between them changes nothing (the cache does not read it); the same replay survives a fresh `SessionVariableManager` on a restarted daemon; and once `apply_in_place_compact_context_loss` runs for a genuinely new compaction, the cache is gone in that same arming write and the next claim renders fresh from the newly armed variables rather than replaying the previous generation. test: `tests/mcp_proxy/test_mcp_tools_session_messages.py::test_continuation_survives_wrapper_timeout_and_cancellation`.
- 2.1.21 - With the session pending and, in turn, `SessionVariableManager.claim_compact_continuation` raising from inside its transaction and `render_compact_continuation_block` raising, both `wait_for_summary` (`completed: true`) and `get_handoff_context` (`has_context: true`) carry `continuation_pending: true` and no `continuation`, leave all four one-shots armed, and log the failure with the session id, and the following call with the fault removed delivers the block once. The base that carries the signal is asserted per tool at **both** response sizes: `wait_for_summary` returns the ~330-character reference stub whether its summary is below or above `threshold_chars` (byte-identical stub text in both), and that response passes through the real `ToolResultOffloader` unoffloaded with `success`, `completed`, and `continuation_pending` still readable at top level; `get_handoff_context` returns its `context` byte-identical to the no-pending response at both sizes, served inline below the threshold and offloaded above it to a `result_id` envelope whose retrieved payload is that complete base, with its one-shots armed either way. test: `tests/mcp_proxy/test_mcp_tools_session_messages.py::test_continuation_claim_failure_preserves_base_result`.

### 2.2 Route the Grok continuation prompt to wait_for_summary and retire the dead turn_start rule [category: code] (depends: 2.1, 1.2, 1.4)
`kind: deliverable`

Targets:
- `src/gobby/sessions/compact_handoff_block.py`
- `src/gobby/sessions/compact_continuation.py::*` — scope-reason: `build_compact_self_continue_prompt` gains `source`, `_build_wait_for_summary_directive` delegates to the moved directive body, and the module's imports are replaced to pull `wait_for_summary_directive` from `compact_handoff_block`
- `src/gobby/mcp_proxy/tools/sessions/_terminal.py::*` — scope-reason: `compact_self` passes `source=session.source` to the prompt builder; file-wide scope shared with 1.4's plumbing edits to the same module
- `src/gobby/servers/websocket/chat/session_registry.py::WebChatSessionRegistry.compact_session`
- `src/gobby/mcp_proxy/tools/sessions/_terminal_webchat.py::*` — scope-reason: unchanged call site of `WebChatSessionRegistry.compact_session`, listed for consumer closure
- `tests/mcp_proxy/tools/sessions/test_compact_self.py::*` — scope-reason: add the Grok marker-to-PostCompact directive case
- `tests/servers/websocket/chat/test_session_registry.py::*` — scope-reason: assert the web-chat builder passes `source=None`
- `src/gobby/install/shared/workflows/rules/context-handoff/inject-compact-handoff.yaml::*` — scope-reason: remove the inject-compact-handoff-on-prompt rule from the bundled rule file
- `src/gobby/install/bundled_content_manifest.json::*` — scope-reason: regenerated checksum manifest for changed bundled content
- `tests/sessions/test_compact_continuation.py::*` — scope-reason: add Grok-directive cases
- `tests/sessions/test_compact_handoff_block.py`
- `tests/workflows/test_context_handoff_rules.py::*` — scope-reason: retire the on-prompt rule tests and assert authoritative sync removes the row

`compact_continuation.py` is at 952 lines, so the directive text is a move out of it:
split the directive body into `compact_handoff_block.py` (created in 2.1) by adding:

```python
def wait_for_summary_directive(summary_session_id: str, *, source: str | None) -> str:
    """Directive body appended after COMPACT_SELF_CONTINUE_INTRO.

    source == "grok":
        'Call `gobby-sessions.wait_for_summary(session_id="<id>")` now. If it returns '
        '`success=true` with `completed=false` (a timeout) or with '
        '`continuation_pending=true`, repeat the same wait call, at most three more '
        'times in total. If it returns `success=false` or `found=false`, or the fourth '
        'call is still incomplete or pending, stop calling it, tell the user the handoff '
        'summary was unavailable, and continue from your own context. Once complete, use the '
        'returned `context` as your handoff and follow every instruction in the returned '
        '`continuation` block before continuing.'
    every other source: today's text verbatim — 'If startup context contains '
        '`<!-- gobby:injected-context:begin -->`, use that injected context directly and '
        'continue. Only if the injected context is missing or incomplete, call '
        '`gobby-sessions.wait_for_summary(session_id="<id>")`. If it returns '
        '`completed=false`, repeat the same wait call. Once complete, use the returned '
        '`context` and continue.'
    """
```

The Grok directive is the only path to the handoff, so it spells out the full
`wait_for_summary` response union: `success=true` with `completed=false` (the tool's own
timeout) or with `continuation_pending=true` (2.1's continuation claim failed after the
base was built; the one-shots are still armed) are the only retryable outcomes;
`success=false` (an unresolvable reference) and
`found=false` (a session that disappeared) are terminal and stop the loop at once; and
the loop is capped at four calls in total (four default 60-second timeouts), after
which the directive names one fallback action. The other-source text stays verbatim,
including its unbounded `completed=false` wording — changing it is outside this plan.

In `compact_continuation.py`, `_build_wait_for_summary_directive(summary_session_id, *,
source)` becomes `COMPACT_SELF_CONTINUE_INTRO + wait_for_summary_directive(...)` when a
session id is present (else `COMPACT_SELF_CONTINUE_PROMPT`, unchanged), and
`build_compact_self_continue_prompt(*, summary_session_id=None, source=None)` forwards
`source`. The prompt is built at exactly two sites, and both pass `source`: `compact_self`
in `_terminal.py` (`build_compact_self_continue_prompt(summary_session_id=resolved_session_id,
source=source)`, with the resolved session's `source` already in scope) and
`WebChatSessionRegistry.compact_session` (`source=None`; web-chat sessions are never Grok
terminals, so their directive is unchanged). The marker and scheduling functions
(`schedule_compact_self_continuation`, `consume_and_schedule_compact_self_continuation`,
`_take_same_terminal_compact_self_continuation_pending`) carry the already-built prompt
through the persisted marker untouched, so a Grok marker written by `compact_self` is the
exact prompt PostCompact types. The prompt stays a single line. Net change to
`compact_continuation.py` is the `source` plumbing plus the delegation; it must finish
under 1,000 lines.

Remove the `inject-compact-handoff-on-prompt` rule from `inject-compact-handoff.yaml`
(keep `inject-compact-handoff`), regenerate `bundled_content_manifest.json` with
`uv run python -c "from pathlib import Path; from gobby.install.manifest import write_bundled_content_manifest; write_bundled_content_manifest(Path('src/gobby/install'))"`,
and rely on authoritative sync to retire the DB row (precedent:
`test_authoritative_sync_retires_prepare_clear_handoff`). Delete the rule's tests and add
one asserting the rule is absent after sync. This leaf depends on 1.2 and 1.4 in
addition to 2.1 because it shares writable artifacts with both — the checksum manifest
with 1.2, and `_terminal.py::compact_self` plus `test_compact_self.py` with 1.4 — so
the edges serialize leaves that could otherwise become ready concurrently. As the last
ordered leaf regenerating bundled content, its validation also runs the
committed-manifest tree-equality regression.

**Acceptance:**

- 2.2.1 - The Grok directive contains no injected-context clause, names `continuation`, retries only on `success=true` with `completed=false` or with `continuation_pending=true` under one explicit cap of three further calls, and stops with the stated fallback on `success=false` or `found=false`; the Claude/Codex directive is unchanged. test: `tests/sessions/test_compact_continuation.py::test_grok_continue_prompt_routes_to_wait_for_summary`.
- 2.2.2 - Both builder call sites pass `source`; a Grok `compact_self` persists a marker whose prompt names `continuation` and omits the injected-context clause, and PostCompact types that exact prompt. test: `tests/mcp_proxy/tools/sessions/test_compact_self.py::test_grok_compact_self_marker_carries_wait_for_summary_directive`.
- 2.2.3 - `inject-compact-handoff-on-prompt` no longer exists in the bundled rules and authoritative sync retires its row. test: `tests/workflows/test_context_handoff_rules.py::test_authoritative_sync_retires_inject_compact_handoff_on_prompt`.
- 2.2.4 - `wait_for_summary_directive` lives in `compact_handoff_block.py` and `compact_continuation.py` stays under 1,000 lines. test: `tests/sessions/test_compact_handoff_block.py::test_wait_for_summary_directive_by_source`.
- 2.2.5 - `WebChatSessionRegistry.compact_session` builds the prompt with `source=None` and its directive is unchanged. test: `tests/servers/websocket/chat/test_session_registry.py::test_compact_session_prompt_source_none`.
- 2.2.6 - After this leaf's manifest regeneration, the committed checksum manifest equals the bundled source tree. test: `tests/install/test_bundled_content_manifest.py::test_bundled_content_manifest_matches_tree`.

### 2.3 Document Grok compact handoff delivery [category: docs] (depends: 2.2)
`kind: deliverable`

Targets:
- `docs/guides/sessions.md`
- `docs/guides/adapter-fidelity.md`
- `docs/contracts/session-boundary.md`
- `docs/guides/variables.md`
- `docs/guides/mcp-tools.md`

Rewrite the Grok paragraph in `sessions.md` (the one that currently says the next
`turn_start` fires `inject-compact-handoff-on-prompt`): Grok consumes no passive hook
stdout, so Grok `post_compact` arms `compact_handoff_inject_pending`, the daemon-typed
continuation prompt calls `wait_for_summary`, and that tool's response carries the
summary plus the `continuation` block (MCP ledger, skill reload, task context) and clears
the one-shots. In `adapter-fidelity.md`, annotate the Grok row's context-channel cell
with "verified 2026-08-20: ignored by Grok 1.0.5; see #20635" so the table stops
asserting delivery that does not happen.

Sweep every live doc that inventories the retired surface or the changed tool
responses: `docs/contracts/session-boundary.md` names
`inject-compact-handoff-on-prompt` in its rule inventory — replace it with the
`wait_for_summary`/`get_handoff_context` claim path, including the direct-versus-reference
clearing distinction below; the
`compact_handoff_inject_pending` row in `docs/guides/variables.md` still says the rule
injects on the next `turn_start` — rewrite it to the tool-response delivery and list
the exact variables each successful claim clears: a direct or empty delivery clears all
four (`compact_handoff_inject_pending`, `compact_resume_required_skills`,
`compact_resume_advisory_skills`, `pending_context_reset`), while a reference-form
delivery clears the other three and leaves `compact_resume_required_skills` populated
as the `get_variable` target until the next `compact_self` rewrites it; the
`wait_for_summary` row in `docs/guides/mcp-tools.md`
documents the response — add the optional `continuation` key to it and to the
`get_handoff_context` row, and state the delivery asymmetry both rows depend on:
`wait_for_summary` re-returns the identical block on a retry within the same compaction
(so an abandoned or timed-out call loses nothing) and answers a claim failure with
`continuation_pending: true` beside its bounded reference stub, while
`get_handoff_context` delivers the block at most once, never replays it, and keeps its
full context on a claim failure.

**Acceptance:**

- 2.3.1 - The sessions guide describes the wait_for_summary delivery path and no longer references the retired rule. behavior: "Grok compact continuation" in `docs/guides/sessions.md`.
- 2.3.2 - The adapter-fidelity table states the declared Grok context channels are verified ignored by Grok 1.0.5 (2026-08-20) and points at #20635 for the capability correction and replacement channel. behavior: "Grok context channel" in `docs/guides/adapter-fidelity.md`.
- 2.3.3 - No live doc still names `inject-compact-handoff-on-prompt`: the session-boundary contract and the variables guide describe the tool-response claim path with the four variables a direct or empty delivery clears and the three a reference-form delivery clears (naming `compact_resume_required_skills` as the preserved `get_variable` target), and the MCP-tools guide documents the optional `continuation` key on both delivery tools together with the retry semantics — `wait_for_summary` replaying the identical block within one compaction and answering a claim failure with `continuation_pending` beside its reference stub, `get_handoff_context` never replaying and keeping its full context. behavior: "compact continuation delivery" in `docs/contracts/session-boundary.md`.

## 3 Grok-wide context channel
`kind: deferred`

Task #20635 carries label `deferred-from:compact-summary-fidelity:3` and validation
criteria enumerating the deferred work: capability correction (`_grok_capabilities()` and
the adapter-fidelity docs declare `ContextChannel.NONE` for every Grok hook channel), a
per-session pending-context queue fed by `inject_context` effects on NONE-channel
providers, delivery through the MCP proxy `call_tool` result envelope under a size budget
with one-shot clearing, delivered-state variables (`wiki_overview_injected`,
`_startup_context_injected`, …) set only on confirmed delivery, and a live Grok proof
showing the role, wiki, and skill blocks in the model-visible history. The compact
handoff route itself is owned by 2.1 and 2.2 here and is not duplicated there. At
expansion the task is parented under this plan's epic as tail work with `blocked-by`
edges on the 2.1, 2.2, and 2.3 leaves — 2.3 included because #20635's capability
correction supersedes the same `docs/guides/adapter-fidelity.md` cell that 2.3 edits,
and the interim and superseding edits must not race.

```yaml
deferral:
  task_ref: "#20635"
  reason: "Every other inject_context effect (role, wiki, skill directives, memory recall, task context, tool-error recovery, context-pressure nudges) is equally undelivered on Grok. Fixing it means correcting _grok_capabilities() to ContextChannel.NONE and building a NONE-channel delivery path through MCP call_tool results — a separate design."
  owner: "gobby"
  original_acceptance_items:
    - "3.1"
```

## 4 Verification
`kind: verification`

- The 1.5 parity suite is the primary regression: one fixture per CLI through parser →
  digest → summary, asserting path, command, task ref/action, commit SHA, and final-turn
  fact, with pair count and role sequence preserved.
- Replay the existing `tests/sessions/transcripts/fixtures/grok_audit/10725/updates.jsonl`
  and `grok_audit/10695/updates.jsonl` fixtures through `build_turn_and_digest` against an
  isolated test database: the turn records must name the edited files, the task-tool
  calls, and the shell commands; regenerate the summary and confirm "Files Changed" and
  "What Was Accomplished" are populated. No new fixture is vendored (#10854's transcript
  is 18.7 MB); cap survival of task and commit lines is pinned by 1.1.6.
- Live: start a Grok session, edit two files, call `compact_self`; confirm the typed
  continuation prompt goes straight to `wait_for_summary`, the response has
  `continuation` with the skill directive and MCP ledger, and `get_variable(#N)` shows
  the one-shots cleared. Repeat with Claude: SessionStart injection unchanged, no
  `continuation` key.
- Live: Claude session with ~20 tool calls, `compact_self`; the compaction summary
  revision (`session_summary_revisions`) carries the digest count that includes the
  final turn.
- Gates per leaf: `uv run ruff format src/`, `uv run ruff check src/`, `uv run mypy src/`,
  `uv run gobby test-types audit tests/ --baseline .gobby/test-types-baseline.json --fail-on-new`,
  and the focused pytest paths named in each deliverable.

## V1 Plan Changelog
`kind: verification`

**Round 1** `kind: enhancement`

- enhancer_run: c95fbdd9-b19c-4ed7-9526-de8253e708c0
- enhancer_session: c7c3278e-a03d-4982-9b66-b3d0df91e3ad
- converged: false
- suggestions_presented: 4
- accepted:
  - E1 / better / evidence-aware ledger truncation (protected lines + tail, >80-call fixture)
  - E2 / better / five-CLI parser→digest→summary golden-path parity suite
  - E3 / better / commit outcome evidence (SHA from `git commit` results, `commit_sha` from task tools) in ledger and analyzer
  - E4 / better / single atomic `claim_compact_continuation` consume shared by both handoff tools, with concurrency test
- declined: none
- resolution_notes: E1 → `render_tool_activity` protected-line rule, `DIGEST_ACTIVITY_TAIL_LINES`, acceptance 1.1.6, Constraints and § 4 updated. E2 → new deliverable 1.5 with five native fixtures and two parametrized tests; § 4 names it the primary regression. E3 → `commit_outcome`/`is_commit_producing` and `outcome` field in 1.1, per-parser result correlation limited to commit-producing calls, `tool_result` adapter mapping and real-hash `git_commits` in 1.3, acceptance 1.1.7 and 1.3.4, Constraints bullet bounding retained output. E4 → `SessionVariableManager.claim_compact_continuation` plus `_attach_compact_continuation` in 2.1, acceptance 2.1.5 and 2.1.6.

**Round 1** `kind: verification`

- reviewer_run: bc8c8a74-3dd8-41f3-a07a-97722e914441
- reviewer_session: 863f447b-00e2-4f50-a375-8ae81e433b26
- verdict: needs_review
- findings:
- PR1-F01 / blocking / deferral §3 named active item 2.1.4 and #20635 lacked provenance, edges, and enumerated criteria
- PR1-F02 / blocking / one-shot continuation cleared before the block was known to be non-empty
- PR1-F03 / blocking / Qwen failure semantics and five-CLI failed-call parity unspecified
- PR1-F04 / blocking / no isolated-DB assertion that the live `memory/turn_record` row carries the ledger instruction
- PR1-F05 / blocking / stale→live `wait_for_summary` branch had no continuation test
- PR1-N01 / nit / acceptance 2.3.2 weakened a verified negative into "unverified"
- PR1-F06 / blocking / #10854 fixture copy lived only in §4 verification prose
- PR1-F07 / blocking / tool-only-turn ledger could attach to a pair behind the persisted cursor
- PR1-F08 / blocking / Codex `functions.exec` + nested exec outcomes not modelled
- PR1-F09 / blocking / MCP wrapper unwrap ignored `args`/string/nested shapes accepted by `canonicalize_call_tool_wrapper`
- PR1-F10 / blocking / adapter relied on `parse_line`, which drops Qwen/Droid multi-block records
- PR1-F11 / blocking / per-refresh whole-transcript read was unbounded
- PR1-F12 / blocking / digest `asyncio.Lock` does not serialise across the dispatcher's `asyncio.run` fallback loop
- PR1-F13 / blocking / callers ignored `build_turn_and_digest`'s returned `{error}`/`{cancelled}` results
- PR1-F14 / blocking / compact_self pre-digest had no memory-manager wiring path
- PR1-F15 / blocking / `get_handoff_context` claim fired on `found: true` error/no-context branches and an undefined `resolved_id`
- PR1-F16 / blocking / §2.2 targeted pass-through functions instead of the two real prompt-builder call sites
- PR1-F17 / blocking / ledger fields were not control-character-escaped before caps
- PR1-F18 / blocking / `_SHELL_TOOLS` edit lay outside the exact `is_shell_tool` target
- PR1-F19 / blocking / new `_handoff.py` helper lay outside every exact target
- votes: 20 presented, 20 accepted (F01 at the full fix; F06 remove variant, F07 side-field design, F11 bounded window, F12 loop routing, F19 placement variant), 0 declined
- resolution_notes: §3 now names item 3.1, records the expansion-time label/parenting/edges, and #20635 was relabelled `deferred-from:compact-summary-fidelity:3` with enumerated criteria (F01). §1.1: ledger is a `tool_activity` side field on the turn's user message with content/count/roles byte-identical (F07); Codex/Qwen/Droid collect through `iter_parse_events` with `codex_exec_outcomes` and `arguments.cmd` (F08, F10); `canonical_tool_name` reuses `canonicalize_call_tool_wrapper` (F09); Qwen failure mapping via `toolCallResult.status`/`error` (F03); `escape_ledger_text` with caps on escaped text (F17); `_normalization_shell.py::*` target (F18); acceptance 1.1.8–1.1.11. §1.2: `DigestPair(prompt, response, activity)`, `_read_undigested_turns` keeps in-flight detection on narration, prompt-sync test target, acceptance 1.2.4–1.2.5 (F04, F07). §1.3: `SUMMARY_ANALYZER_MAX_RECORDS = 20_000` bounded window with generator adapter over `iter_parse_events`, acceptance 1.3.5–1.3.7 (F08, F10, F11). §1.4: returned-result contract handling, `pre_digest` only on the daemon-loop path, `memory_manager_resolver` wired through `setup_internal_registries` → `create_session_messages_registry` → `register_terminal_tools` → `compact_self`, new Targets and acceptance 1.4.4–1.4.7 (F12, F13, F14). §1.5: one natively failed call per fixture, Codex fixture uses `functions.exec`, acceptance 1.5.4 (F03, F08). §2.1: `claim_compact_continuation(session_id, render)` renders inside the mutation and writes nothing when empty, `attach_compact_continuation` lives in `compact_handoff_block.py`, claim only on the final success/found/has_context result with `parent_session.id`, acceptance 2.1.7–2.1.9 (F02, F05, F15, F19). §2.2: Targets are `_terminal.py::compact_self` and `WebChatSessionRegistry.compact_session`, acceptance 2.2.2 rewritten and 2.2.5 added (F16). §2.3: 2.3.2 says verified ignored by Grok 1.0.5 (N01). §4: replay uses existing `grok_audit` fixtures, no 18.7 MB vendoring (F06). Constraints gained escaping, bounded-window, and daemon-loop-only bullets.

```json plan-review-round
{"evidence_id":"9939179d-f3ef-4884-8a73-6b70e801489b","plan_hash":"56442a26a1c275a0690fb0a9fe4df51fd05c065cd20cf066d0079125f01d88b6","round_number":1,"round_result":{"coverage_attestation":{"adjacent_variant_complete":true,"attestation_digest":"c2e02c682fd16b0f6add8c019200a7c4a7f57f38f85d61761fa233017f737c9b","cross_lane_interaction_complete":true,"disposition_counts":{"dismissed":5,"emitted_findings":20,"total":25},"evidence_id":"9939179d-f3ef-4884-8a73-6b70e801489b","lanes":[{"candidate_count":9,"lane_id":"requirements_traceability","status":"completed"},{"candidate_count":5,"lane_id":"repository_blast_radius","status":"completed"},{"candidate_count":11,"lane_id":"runtime_invariants","status":"completed"}],"shadow_manifest_status":{"entry_count":8,"manifest_digest":"3c81490879c172bab1742816f2bd6491b8cda2fe1ad52b6ec3fea911d00ed8c1","status":"valid"},"source_digest":"ae9b1fd25f7cc2382660f1b0f681681b027a1145dd949c9cf7192e12d04c94e5","version":1},"findings":[{"category":"gobby-format","check_key":"deferral-expansion-contract","description":"The referenced task fails the repository's expansion-time deferral contract, and `original_acceptance_items: [2.1.4]` names an active compact-handoff item instead of the wider NONE-channel work being deferred.","finding_id":"PR1-F01","fix":"Reference or normalize a dedicated execution deferral under the future plan epic, add `deferred-from:compact-summary-fidelity:3`, required ordering edges, and validation criteria for capability correction, queued MCP-result delivery, budgets, delivery-time accounting, and live Grok proof; update `original_acceptance_items` to the matching artifacts.","location":"§ 3 Grok-wide context channel","prevention":"Before approval, inspect every deferral target for active state, provenance label, artifact-duplicating criteria, and recovery-epic dependency closure.","principle":"Every typed deferral must resolve to an expansion-valid execution task with provenance, artifact parity, and dependency placement.","root_cause":"Section 3 points at #20635, a parentless planning task without `deferred-from:compact-summary-fidelity:3`, required dependency edges, or criteria duplicating acceptance 2.1.4.","section_id":"3","severity":"blocking"},{"category":"unhandled-edge","check_key":"continuation-empty-consume","description":"A pending session with no renderable sections is cleared even though `_attach_compact_continuation` adds no `continuation`, violating the P2 clear-on-delivery goal.","finding_id":"PR1-F02","fix":"Render and validate a non-empty block inside the atomic mutation, returning the block itself, or leave pending state unchanged when rendering is empty; add the pending-empty regression.","location":"§ 2.1 compact continuation claim","prevention":"Test pending state with empty, malformed, and fully populated payloads; require a continuation key before accepting the consume.","principle":"One-shot continuation state may be consumed only when a usable continuation is attached to a successful response.","root_cause":"`claim_compact_continuation` clears state before `render_compact_continuation_block`, while the renderer is explicitly allowed to return an empty string.","section_id":"2.1","severity":"blocking"},{"category":"unhandled-edge","check_key":"five-cli-failure-correlation","description":"Qwen failures can reach the ledger as successful or statusless results, and the golden fixtures contain only successful calls, so the protected-failure requirement is untestable across all five CLIs.","finding_id":"PR1-F03","fix":"Specify the Qwen status/error source and ID correlation, then add one failed native call to every golden fixture and assertions for the error text plus evidence-aware survival.","location":"§§ 1.1 and 1.5 activity ledger","prevention":"For each provider, enumerate success, failure, missing-result, and unmatched-result envelopes and prove failure lines survive both caps.","principle":"Every supported transcript provider needs an explicit native failure mapping before failed calls can receive protected ledger treatment.","root_cause":"The plan defines failure extraction for Claude, Grok, Codex, and Droid, while Qwen `functionResponse` failure semantics and five-CLI failure parity remain unspecified.","section_id":"1.1","severity":"blocking"},{"category":"weak-testability","check_key":"live-prompt-registry-sync","description":"The plan can pass while the template is correct and the active DB prompt remains stale or its required-variable schema drifts.","finding_id":"PR1-F04","fix":"Add the relevant install/sync test target and acceptance: sync into an isolated DB, fetch `memory/turn_record`, assert the ledger instruction is active, and assert `required_variables` is unchanged.","location":"§ 1.2 turn-record prompt","prevention":"For every bundled prompt edit, add an isolated authoritative-sync assertion covering live content and metadata.","principle":"Bundled prompt changes are complete only when authoritative sync proves the installed DB row carries the new behavior and preserves its metadata contract.","root_cause":"Acceptance 1.2.2 checks the two prompt bodies and checksum manifest without reading the live `memory/turn_record` registry row or its `required_variables`.","section_id":"1.2","severity":"blocking"},{"category":"weak-testability","check_key":"stale-live-continuation","description":"An implementation can attach continuation to the ordinary completion path and silently omit or prematurely consume it on stale live handoff responses.","finding_id":"PR1-F05","fix":"Add a stale-summary test proving live context remains selected, exactly one response carries `continuation`, and the one-shots clear on that successful delivery.","location":"§ 2.1 `wait_for_summary` stale/live branch","prevention":"Enumerate every completed, timeout, missing, stale, and error return before inserting a shared state mutation.","principle":"Every explicitly preserved response branch needs direct acceptance coverage when new one-shot mutation is added.","root_cause":"The stale `summary_is_stale → live_handoff_context` completed branch has a separate early return and no continuation test.","section_id":"2.1","severity":"blocking"},{"category":"traceability","check_key":"grok-doc-evidence-wording","description":"The acceptance criterion weakens a verified negative result into uncertainty, allowing the wrong table wording to pass.","finding_id":"PR1-N01","fix":"Rewrite 2.3.2 to require that the declared channels are verified ignored by Grok 1.0.5 and reference #20635 for the correction and replacement path.","location":"§ 2.3 adapter-fidelity documentation","prevention":"Compare documentation acceptance text with the evidence statement and task description for certainty drift.","principle":"Acceptance wording should preserve the certainty level of the governing evidence.","root_cause":"The section requires `verified 2026-08-20: ignored by Grok 1.0.5`, while acceptance 2.3.2 says `unverified-in-practice`.","section_id":"2.3","severity":"nit"},{"category":"traceability","check_key":"verification-owned-artifacts","description":"Expansion emits no task that owns the required #10854 fixture, so the prescribed replay cannot be performed from the expanded leaves.","finding_id":"PR1-F06","fix":"Add a concrete fixture path to § 1.5 Targets and acceptance with the replay facts, or remove the file-creation requirement and use an already-owned fixture.","location":"§ 4 replay verification / § 1.5 parity suite","prevention":"Sweep verification sections for change verbs and map each created artifact back to one manifest-backed deliverable.","principle":"Every file created by verification prose must belong to an expanding deliverable with a target and acceptance item.","root_cause":"The 185-call #10854 transcript copy appears only in non-expanding § 4 verification prose.","section_id":"1.5","severity":"blocking"},{"category":"bad-sequencing","check_key":"tool-only-turn-cursor-correlation","description":"The compact-triggering pair can retain an empty response while its ledger is moved into an already-digested pair; pre-summary digestion then repeats defect D with no current-turn evidence.","finding_id":"PR1-F07","fix":"Specify a turn-segment-aware ledger channel that `_extract_digest_pairs` places into the trailing pair response without changing raw message count or cursor coordinates; add the exact cursor regression.","location":"§§ 1.1, 1.2, and 1.4 digest boundary","prevention":"Test one digested pair followed by a user prompt and only tool-use/tool-result records at pre-summary time.","principle":"Tool activity from the current turn must remain on the current digest pair before the persisted pair cursor advances.","root_cause":"The fallback rule attaches a terminal tool-only sequence to the previous text-bearing assistant message, which may already be behind `last_digested_pair_index`.","section_id":"1.1","severity":"blocking"},{"category":"unhandled-edge","check_key":"codex-nested-exec-envelope","description":"Inner commands, failures, edited paths, and commit SHAs can disappear because the proposed ledger and analyzer only correlate direct `function_call` and `function_call_output` records.","finding_id":"PR1-F08","fix":"Define Codex extraction through `iter_parse_events` and nested exec outcomes, accept the inner `cmd` command field, recover wrapped MCP calls, and update the Codex golden fixture and assertions.","location":"§§ 1.1, 1.3, and 1.5 Codex path","prevention":"Build parity fixtures from current native envelopes and sweep parser-specific auxiliary event streams.","principle":"Golden fixtures and extraction rules must model the provider envelopes emitted by the current runtime.","root_cause":"The Codex plan models a direct native `shell` call, while current transcripts can record `functions.exec` plus separate `CodexNestedExecOutcome` events carrying the inner `exec_command` and result.","section_id":"1.1","severity":"blocking"},{"category":"unhandled-edge","check_key":"call-tool-wrapper-normalization","description":"Task IDs, titles, and `commit_sha` can vanish for valid wrapper shapes, breaking both ledger lines and structured summary commits.","finding_id":"PR1-F09","fix":"Make ledger/analyzer normalization reuse or exactly mirror `canonicalize_call_tool_wrapper`; add top-level, alias, string, and nested payload tests including `close_task` commit evidence.","location":"§§ 1.1, 1.3, and 1.5 MCP activity","prevention":"Reuse the canonical wrapper implementation or table-test every legal wrapper shape before adding another normalizer.","principle":"A new evidence normalizer must cover every serialization already accepted by the live call-tool wrapper.","root_cause":"`canonical_tool_name` only unwraps dictionary `arguments`, while the repository accepts `arguments` or `args`, JSON strings, and nested/hoisted routing fields.","section_id":"1.1","severity":"blocking"},{"category":"unhandled-edge","check_key":"multi-block-parser-consumption","description":"Multi-part Qwen and Droid records lose text, tool uses, or tool results before `TranscriptAnalyzer`, so summary ground truth remains incomplete.","finding_id":"PR1-F10","fix":"Define `analyzer_turns_from_transcript` with `raw_lines_from_texts` plus `iter_parse_events`, consume every `ParseEvent.records` item, group by source record, apply finalization adjustments, and add multi-block tests.","location":"§§ 1.3 and 1.5 analyzer adapter","prevention":"Before choosing a parser API, inspect whether it is single-record convenience or exhaustive event consumption; test mixed blocks in one envelope.","principle":"Adapters must consume the full parser event stream whenever one native record expands to multiple normalized blocks.","root_cause":"Qwen and Droid `parse_line` explicitly return only `expanded[0]`, contradicting the proposed promise that consecutive blocks stay in one analyzer turn.","section_id":"1.3","severity":"blocking"},{"category":"unhandled-edge","check_key":"summary-transcript-resource-bound","description":"Long sessions create repeated unbounded O(transcript-size) memory and time work during compaction summary refresh.","finding_id":"PR1-F11","fix":"Specify a streaming analyzer accumulator or another bounded design that preserves required all-session facts; add a large-transcript test for bounded resident records and fidelity.","location":"§§ 1.3 and 1.5 summary grounding","prevention":"For each whole-history read on a recurring path, state the bound or streaming design and add a large-input resource test.","principle":"A recurring digest-backed summary path needs an explicit memory and time policy for unbounded transcript input.","root_cause":"The plan changes every digest-present refresh to materialize the complete ever-growing JSONL and then builds a second full adapted list.","section_id":"1.3","severity":"blocking"},{"category":"bad-sequencing","check_key":"cross-loop-digest-serialization","description":"The claimed per-session lock does not guarantee mutual exclusion across dispatcher and daemon loops, so both callers can read and persist the same cursor concurrently.","finding_id":"PR1-F12","fix":"Route all digest calls onto one daemon loop or use a database-backed/session lock that spans loops and threads; add a two-thread, two-loop exact-once persistence and cursor test.","location":"§ 1.4 turn-end versus pre-summary digest","prevention":"Enumerate execution contexts for every new caller and race them in separate loops before relying on an async lock.","principle":"A serialization guarantee must span every thread and event loop that can execute the protected operation.","root_cause":"`_serialize_session_digest` uses a process-global `asyncio.Lock`, while `SessionSummaryDispatcher` can execute its coroutine in a new thread with `asyncio.run`.","section_id":"1.4","severity":"blocking"},{"category":"unhandled-edge","check_key":"digest-returned-failure-contract","description":"Returned digest failures proceed as if digestion completed, preventing the promised explicit fallback reason and weakening diagnostics.","finding_id":"PR1-F13","fix":"Define the accepted success/skip contract, detect returned error/cancellation results, route terminal refresh through existing fallback, log dispatcher failures, and test every outcome.","location":"§ 1.4 dispatcher and compact-self refresh","prevention":"For every boundary helper, enumerate success, skipped, returned error, cancellation, exception, and timeout results.","principle":"Callers must distinguish returned failure values from successful completion when a callee catches its own exceptions.","root_cause":"`build_turn_and_digest` returns error and provider-cancelled dictionaries for many failures, while the proposed callers only catch raised exceptions.","section_id":"1.4","severity":"blocking"},{"category":"traceability","check_key":"compact-self-memory-wiring","description":"Changing only `_terminal_handoff.py` cannot supply the same live memory manager used by memory tools, so the compact-self pre-digest path is unimplementable from this leaf.","finding_id":"PR1-F14","fix":"Add and specify targets/data flow through `setup_internal_registries`, `create_session_messages_registry`, `register_terminal_tools`, `compact_self`, config capture, and background scheduling; define absent/disabled behavior and registry-level tests.","location":"§ 1.4 compact-self digest wiring","prevention":"Run caller/constructor/fake sweeps for every added parameter and add each changed site to Targets.","principle":"Every newly required runtime dependency must be traced through constructors, factories, registrations, callers, and background schedulers.","root_cause":"The sessions registry factory and `register_terminal_tools` expose no memory-manager resolver, and the listed § 1.4 targets omit the wiring path.","section_id":"1.4","severity":"blocking"},{"category":"unhandled-edge","check_key":"handoff-success-consume-gate","description":"No-context, cross-project child, and invalid-child results can clear continuation state without delivery, and project/source lookup can use an undefined or wrong identifier.","finding_id":"PR1-F15","fix":"Gate claiming on `success=true`, `found=true`, and `has_context=true` after child validation, use `parent_session.id`, and add tests for each error/no-context/filtered branch.","location":"§ 2.1 `get_handoff_context` branches","prevention":"Enumerate result predicates and use the canonical returned session ID before placing a destructive one-shot claim.","principle":"One-shot continuation state must be claimed after all conditions for a successful usable handoff are satisfied.","root_cause":"The plan says `get_handoff_context` claims on every `found: true` return, which includes no-context and child-link error branches; filtered lookup paths also lack the named `resolved_id`.","section_id":"2.1","severity":"blocking"},{"category":"traceability","check_key":"source-aware-prompt-call-sites","description":"The Grok-specific directive can remain unreachable or be overwritten by a persisted generic prompt even when the listed functions are updated.","finding_id":"PR1-F16","fix":"Add the actual call sites and pass resolved source there, or persist source plus summary ID and build exactly once at consumption; add an end-to-end Grok marker-to-PostCompact assertion.","location":"§ 2.2 continuation prompt routing","prevention":"Resolve exact builder callers before assigning source plumbing and target every changed construction boundary.","principle":"Source-dependent behavior must be wired at the sites that actually construct or persist the value.","root_cause":"The three named continuation functions consume an already-built prompt; actual `build_compact_self_continue_prompt` callers include terminal `compact_self` and the web-chat registry, both absent from § 2.2 Targets.","section_id":"2.2","severity":"blocking"},{"category":"unhandled-edge","check_key":"ledger-control-character-escaping","description":"One tool call can occupy many physical lines, defeat the 80-line cap and omission count, and inject false ledger structure into the authoritative prompt evidence.","finding_id":"PR1-F17","fix":"Specify one escaping routine for every rendered field, cap after escaping, compute both budgets on final text, and add multiline/adversarial tests.","location":"§§ 1.1, 1.2, and 1.5 ledger rendering","prevention":"Test CR, LF, tab, control characters, and ledger-like injected text in every rendered field.","principle":"A line-oriented evidence format must escape control characters before truncation and budgeting.","root_cause":"Tool names, commands, paths, outcomes, and errors are interpolated as raw strings; normal shell and `functions.exec` payloads can contain newlines.","section_id":"1.1","severity":"blocking"},{"category":"gobby-format","check_key":"shell-alias-target-scope","description":"The real change lies outside the declared exact symbol target, violating the plan's symbol-scoped execution contract.","finding_id":"PR1-F18","fix":"Replace the exact target with a justified `src/gobby/hooks/_normalization_shell.py::*` entry or another contract-valid decomposition that covers `_SHELL_TOOLS`, `is_shell_tool`, and `canonicalize_shell_tool_name`.","location":"§ 1.1 shell alias target","prevention":"Compare implementation prose with indexed constants and every consumer before finalizing exact Targets.","principle":"Targets must name the actual indexed mutation scope for every existing symbol-bearing file.","root_cause":"Adding `run_terminal_command` changes module-level `_SHELL_TOOLS`, while the plan targets only `is_shell_tool`; `canonicalize_shell_tool_name` is the adjacent consumer.","section_id":"1.1","severity":"blocking"},{"category":"gobby-format","check_key":"new-helper-target-scope","description":"The new helper is outside every declared exact target, so the expanded leaf's authorized scope omits part of its own implementation.","finding_id":"PR1-F19","fix":"Add a justified `src/gobby/mcp_proxy/tools/sessions/_handoff.py::*` target, or redesign the helper inside an already targeted indexed symbol and state that placement.","location":"§ 2.1 `_handoff.py` helper","prevention":"For each proposed helper, identify its containing indexed scope and add a justified wildcard when no existing symbol contains it.","principle":"New top-level code in an existing symbol-bearing file requires declared file-wide scope or placement inside an existing exact target.","root_cause":"Section 2.1 adds module-level `_attach_compact_continuation`, while `_handoff.py` Targets name only existing functions.","section_id":"2.1","severity":"blocking"}],"reviewer_session":"863f447b-00e2-4f50-a375-8ae81e433b26","round":1,"round_number":1,"verdict":"needs_review"},"session_id":"9b24531b-013f-447f-a448-506cb5b3a818"}
```

**Round 2** `kind: verification`

- reviewer_run: 6d5cc8bc-bb0e-445e-bbd4-e479f729a910
- reviewer_session: 293f745c-e8a6-45cf-a65b-6a3a6b20c132
- verdict: needs_review
- findings:
- PR2-F01 / blocking / successful Grok delivery left `pending_context_reset` true, keeping context-pressure guidance suppressed
- PR2-F02 / blocking / the proxy result offloader could replace an over-threshold response after the one-shot claim, dropping `continuation`
- PR2-F03 / blocking / 2.2 could run concurrently with 1.2 (shared checksum manifest) and 1.4 (shared compact_self + tests)
- PR2-F04 / blocking / #20635 deferral edges omitted the 2.3 leaf that shares `docs/guides/adapter-fidelity.md`
- PR2-F05 / blocking / 2.2's manifest regeneration referenced 1.2 instead of carrying the command
- PR2-N01 / nit / neither manifest-regenerating leaf ran the committed tree-equality regression
- PR2-F06 / blocking / the `CallToolWrapperInputError` fallback had no named acceptance test
- PR2-F07 / blocking / three-field `DigestPair` breaks two-value destructures in `test_grok_coverage_audit.py`, absent from Targets
- PR2-F08 / blocking / module-level `DigestPair` lay outside digest.py's exact Targets
- PR2-F09 / blocking / `recent_activity` formatter kept raw wrapper names, so the five-CLI golden assertion could not pass
- PR2-F10 / blocking / `HookManager.__init__` never stores `components.memory_manager` and was untargeted
- PR2-F11 / blocking / `_schedule_compact_handoff_background_refresh` (sole scheduler caller) untargeted for the new kwargs
- PR2-F12 / blocking / session-boundary contract, variables guide, and MCP-tools guide still document the retired surface
- PR2-F13 / blocking / omission marker counted dropped lines, not underlying calls of collapsed `(xN)` groups
- PR2-F14 / blocking / the current Codex envelope hides activity in `item_completed` `McpToolCall`/`CommandExecution` items the plan never modeled
- votes: 15 presented, 15 accepted, 0 declined (unattended coordinator judgment; every finding verified against the repository before voting — F01 via the retired rule's `pending_context_reset` clear and `observer_context_usage.py` suppression; F02 via `ToolResultOffloader._maybe_offload_sync` wrapping every successful proxied result over `threshold_chars=15_000`; F07 via the three two-value comprehensions in `compute_user_anchored_coverage`; F10 via `hook_manager.py` never unpacking `components.memory_manager`; F14 conclusively via a live 2026-08-21 Codex transcript: 149 `exec` JS orchestration calls, 0 direct MCP function calls, 50 `McpToolCall` + 170 `CommandExecution` items, none parsed anywhere in src/gobby)
- resolution_notes: §2.1: `pending_context_reset` joined `COMPACT_CONTINUATION_ONE_SHOT_VARIABLES` and the atomic mutate — cleared only on successful delivery, untouched on every non-delivery branch — with acceptance 2.1.10, and 2.1.2/2.1.5 now count four one-shots (F01); `gobby-sessions/wait_for_summary` and `gobby-sessions/get_handoff_context` become mandatory-exempt in `result_offload.py::_MANDATORY_EXEMPT_TOOLS` (precedent `gobby-agents/get_inter_session_message`), with the offload targets, delivery-atomicity paragraph, and acceptance 2.1.11 (F02). §2.2: heading now `(depends: 2.1, 1.2, 1.4)` with the shared-artifact rationale in prose (F03); the exact `uv run python -c ... write_bundled_content_manifest` command is inlined (F05); acceptance 2.2.6 runs `test_bundled_content_manifest_matches_tree` as the last ordered manifest leaf (N01). §3: deferral edges now name 2.1, 2.2, and 2.3 with the adapter-fidelity race rationale (F04). §1.1: malformed-wrapper fallback added to the unit-test list with acceptance 1.1.12 (F06); the omission marker counts dropped underlying calls with `(xN)` multiplicity, in the `render_tool_activity` contract, test prose, and acceptance 1.1.6 (F13); `codex.py` target widened to `::*` and a Codex item-stream bullet added — `item_completed` `McpToolCall`/`CommandExecution` records surface as tool_use/tool_result ParsedMessages with an item-stream precedence rule suppressing exec-wrapper derivations per window; acceptance 1.1.10 rewritten to pin both envelopes without double counting (F14). §1.2: digest.py Targets collapsed to a justified `::*` naming `DigestPair` plus the three changed functions (F08); `test_grok_coverage_audit.py::*` target, attribute-migration prose, and acceptance 1.2.6 added (F07). §1.3: `_format_tool_description` targeted and canonicalization moved to once-per-block in `extract_handoff_context` ahead of both consumers, with acceptance 1.3.8; the adapter forwards item-derived Codex blocks and acceptance 1.3.6 covers both envelopes (F09, F14). §1.4: `HookManager.__init__` targeted to retain `components.memory_manager` with acceptance 1.4.8 (F10); `_schedule_compact_handoff_background_refresh` targeted to gain and forward `memory_manager`/`config` with acceptance 1.4.9 (F11). §2.3: `docs/contracts/session-boundary.md`, `docs/guides/variables.md`, and `docs/guides/mcp-tools.md` added to Targets with rewrite instructions (claim path, four cleared variables, optional `continuation` key) and acceptance 2.3.3 (F12). §1.5: the Codex golden fixture now uses the real envelope — `custom_tool_call` `exec` JS orchestration plus `item_completed` items carrying the edit, shell, task, commit, and failed-call activity — and acceptance 1.5.1 was updated (F14).

```json plan-review-round
{"evidence_id":"afb4c4bc-77e5-4749-b9cc-8d78475ce34b","plan_hash":"774d9925bddb698d2e01c51caeb3fa378bfb80b0d36089287cc5e6613e99f347","round_number":2,"round_result":{"coverage_attestation":{"adjacent_variant_complete":true,"attestation_digest":"59569025273bb8759970806592158be2e1580a9d4a63e3affb3bb611f83ff7fe","cross_lane_interaction_complete":true,"disposition_counts":{"dismissed":4,"emitted_findings":15,"total":19},"evidence_id":"afb4c4bc-77e5-4749-b9cc-8d78475ce34b","lanes":[{"candidate_count":7,"lane_id":"requirements_traceability","status":"completed"},{"candidate_count":7,"lane_id":"repository_blast_radius","status":"completed"},{"candidate_count":5,"lane_id":"runtime_invariants","status":"completed"}],"shadow_manifest_status":{"entry_count":8,"manifest_digest":"e6e3433f3781fd9af41ba7d17922c24ec07d2ba72f1793d400d680a98934db92","status":"valid"},"source_digest":"2e1a9da4004310a65f130cfc42249dd4809ccfff829e9e2f72ef7ff8f5b7ba7a","version":1},"findings":[{"category":"unhandled-edge","check_key":"compact-pending-reset-parity","description":"Successful Grok continuation delivery leaves pending_context_reset true. PreCompact sets it, the retired rule clears it, and context-pressure guidance remains suppressed while it is true.","finding_id":"PR2-F01","fix":"Include pending_context_reset in the same successful atomic claim-and-clear mutation; keep it pending on timeout, missing context, empty render, and errors, with a regression proving guidance resumes after delivery.","location":"P2 / §§ 2.1–2.2","prevention":"Diff all effects of a retired rule against its replacement and test success plus every non-delivery branch.","principle":"Replacing a one-shot delivery rule must preserve every state transition tied to successful delivery.","root_cause":"The atomic claim copied the pending flag and skill-list clears but omitted the retired rule's pending_context_reset clear.","section_id":"2.1","severity":"blocking"},{"category":"unhandled-edge","check_key":"outer-proxy-delivery-atomicity","description":"A summary plus continuation above 15,000 characters is offloaded after the claim, so Grok receives an envelope without the continuation key while the one-shots are already cleared.","finding_id":"PR2-F02","fix":"Add a decision-complete bounded outer-proxy contract for wait_for_summary/get_handoff_context that keeps continuation model-visible across offloading, target the required proxy/offload code, and add an over-threshold end-to-end test before clearing state.","location":"P2 / §§ 2.1–2.2","prevention":"Trace stateful delivery through every post-handler response transformation and test above each configured size threshold.","principle":"One-shot state may clear only after the final model-visible response preserves the delivered payload.","root_cause":"claim_compact_continuation clears inside the session tool before the wrapper's result offloader can replace an oversized response with a retrieval envelope.","section_id":"2.1","severity":"blocking"},{"category":"bad-sequencing","check_key":"shared-target-dependency-order","description":"Section 2.2 can run concurrently with 1.4 while both change compact_self and its tests, and with 1.2 while both regenerate bundled_content_manifest.json.","finding_id":"PR2-F03","fix":"Make 2.2 depend on 1.2 and 1.4 in addition to 2.1, and preserve those edges in the reviewed routing.","location":"§§ 1.2, 1.4, and 2.2","prevention":"Build a duplicate-target inventory and add ordering for every pair of leaves that can become ready concurrently.","principle":"Expanded leaves that own the same writable artifact must be serialized by explicit dependency edges.","root_cause":"Dependencies were derived from feature flow without sweeping duplicate Targets across phases.","section_id":"2.2","severity":"blocking"},{"category":"bad-sequencing","check_key":"deferred-shared-target-order","description":"Task #20635 and section 2.3 both change docs/guides/adapter-fidelity.md, but the deferral promises no dependency on 2.3, allowing the interim and superseding edits to race.","finding_id":"PR2-F04","fix":"Add an expansion-time blocked-by edge from #20635 to the 2.3 leaf; retaining explicit edges to 2.1 and 2.2 is valid.","location":"§ 3 deferral / § 2.3","prevention":"Compare every deferred task criterion and target with active deliverable Targets before fixing its expansion-time edges.","principle":"A deferred tail task must wait for every in-plan leaf whose artifact it supersedes or edits.","root_cause":"The promised #20635 blocker set names 2.1 and 2.2 while omitting the 2.3 documentation leaf.","section_id":"3","severity":"blocking"},{"category":"gobby-format","check_key":"self-contained-regeneration-command","description":"The bundled manifest regeneration instruction is not self-contained.","finding_id":"PR2-F05","fix":"Inline the exact uv regeneration command from 1.2 in section 2.2.","location":"§ 2.2","prevention":"Search deliverables for cross-section instructions and inline every required command, path, and contract.","principle":"Each expanded deliverable must be executable by an agent that receives only that section.","root_cause":"Section 2.2 says to use the command from 1.2 instead of carrying the command locally.","section_id":"2.2","severity":"blocking"},{"category":"weak-testability","check_key":"bundled-manifest-tree-validation","description":"Both leaves regenerate bundled_content_manifest.json, but neither acceptance path runs the existing tree-equality regression.","finding_id":"PR2-N01","fix":"Add tests/install/test_bundled_content_manifest.py::test_bundled_content_manifest_matches_tree to the later ordered leaf's validation.","location":"§§ 1.2 and 2.2","prevention":"Run the repository's manifest-tree equality test in the final leaf that regenerates bundled content.","principle":"Generated bundled-content manifests should be verified against the source tree after the final ordered edit.","root_cause":"The planned sync tests load source directories directly and do not enforce the committed checksum manifest.","section_id":"2.2","severity":"nit"},{"category":"weak-testability","check_key":"wrapper-error-fallback-coverage","description":"Malformed wrapper input can regress into a digest-breaking exception without failing any named acceptance test.","finding_id":"PR2-F06","fix":"Add a malformed JSON or missing-route wrapper acceptance case proving canonical_tool_name keeps the raw wrapper name with empty input and renders a ledger entry.","location":"§ 1.1 canonical_tool_name","prevention":"Pair each caught parser exception in the plan with a named malformed-input acceptance test.","principle":"Every explicit parser error fallback needs a regression for the failing input shape.","root_cause":"Unit cases enumerate accepted call_tool wrapper forms while omitting the planned CallToolWrapperInputError branch.","section_id":"1.1","severity":"blocking"},{"category":"traceability","check_key":"digest-pair-consumer-migration","description":"The planned three-field DigestPair makes tests/sessions/transcripts/test_grok_coverage_audit.py raise in two-value comprehensions, and that consumer is absent from Targets.","finding_id":"PR2-F07","fix":"Target that test file and migrate UserAnchoredCoverage.pairs plus its comprehensions to named DigestPair attributes while preserving the existing metrics.","location":"§ 1.2 / Grok coverage audit","prevention":"Run gcode callers and indexed content searches for tuple unpacking whenever a tuple-like return type changes.","principle":"A return-shape change must migrate every constructor, destructure, fake, and test seam.","root_cause":"The blast-radius pass stopped at digest.py and missed compute_user_anchored_coverage's two-value destructures.","section_id":"1.2","severity":"blocking"},{"category":"gobby-format","check_key":"new-symbol-target-coverage","description":"The new DigestPair declaration is outside all exact Targets.","finding_id":"PR2-F08","fix":"Replace the digest.py exact entries with, or add, src/gobby/memory/digest.py::* carrying a scope reason limited to DigestPair and the named functions.","location":"§ 1.2 digest.py Targets","prevention":"For each planned new symbol in an existing file, add a justified file wildcard before review.","principle":"Every new top-level symbol in an existing symbol-bearing file must fall under a justified wildcard target.","root_cause":"Section 1.2 introduces DigestPair at module scope while targeting only three existing digest.py functions.","section_id":"1.2","severity":"blocking"},{"category":"traceability","check_key":"recent-activity-canonicalization","description":"Grok/Qwen/Droid compact_self calls render as generic use_tool/call_tool activity, so the five-CLI golden recent-activity assertion cannot pass.","finding_id":"PR2-F09","fix":"Target TranscriptAnalyzer._format_tool_description and canonicalize there, or canonicalize blocks once before both analyzer passes; add wrapper-specific recent-activity assertions.","location":"§§ 1.3 and 1.5","prevention":"Enumerate every consumer of the normalized data shape, including formatting and observability paths.","principle":"Every consumer of provider tool names must share the same canonicalization boundary.","root_cause":"The plan canonicalizes _analyze_tool_use but leaves the separate recent_activity formatter on raw wrapper names.","section_id":"1.3","severity":"blocking"},{"category":"traceability","check_key":"memory-manager-constructor-forwarding","description":"HookManager._dispatch_session_summaries has no memory manager to pass to SessionSummaryDispatcher under the current class state.","finding_id":"PR2-F10","fix":"Target HookManager.__init__, retain components.memory_manager, pass it to the dispatcher, and extend the wiring test through the factory-created manager.","location":"§ 1.4 HookManager wiring","prevention":"Trace constructor-provided dependencies from factory output to final consumer and target every storage/forwarding node.","principle":"A new dependency must be retained and forwarded through every constructor boundary on its runtime path.","root_cause":"HookManagerFactory supplies memory_manager, but HookManager.__init__ does not store it and is absent from Targets.","section_id":"1.4","severity":"blocking"},{"category":"traceability","check_key":"background-refresh-forwarding","description":"_schedule_compact_handoff_background_refresh cannot pass the new memory-manager/config values to _run_compact_handoff_background_refresh because it is neither targeted nor described.","finding_id":"PR2-F11","fix":"Target the scheduler, extend its signature and forwarding, and test the scheduled background branch.","location":"§ 1.4 compact_self background refresh","prevention":"Walk callers for each changed signature through both direct and scheduled/background paths.","principle":"Every forwarding node on a changed call signature must be included in Targets and branch coverage.","root_cause":"The plan changes the background runner signature while omitting its sole scheduler caller.","section_id":"1.4","severity":"blocking"},{"category":"missing-requirement","check_key":"retired-rule-doc-inventory","description":"docs/contracts/session-boundary.md and docs/guides/variables.md still name inject-compact-handoff-on-prompt, and docs/guides/mcp-tools.md omits the new continuation response.","finding_id":"PR2-F12","fix":"Add those three files to 2.3 Targets and document the replacement delivery path, exact successful-clear variables, and optional one-shot continuation key.","location":"§ 2.3","prevention":"Before deleting a named runtime surface, search all live docs for the name, affected variables, and response fields.","principle":"Removing a runtime rule and changing a public tool response requires updating every live normative contract and guide that inventories them.","root_cause":"Documentation scope covered two narrative guides without searching exact references to the retired rule or handoff tool schemas.","section_id":"2.3","severity":"blocking"},{"category":"unhandled-edge","check_key":"collapsed-omission-cardinality","description":"Dropping a collapsed '(xN)' line reports one omitted tool call instead of N, contradicting the accurate omission-count acceptance.","finding_id":"PR2-F13","fix":"Carry multiplicity through collapse and sum dropped underlying calls for the marker; update the cap regression with a dropped repeated group.","location":"§ 1.1 render_tool_activity","prevention":"Test truncation with a repeated group that is collapsed and then omitted, asserting underlying-call cardinality.","principle":"An omission marker that claims a tool-call count must count underlying calls after aggregation.","root_cause":"Truncation counts dropped collapsed lines even though one line can represent N identical calls.","section_id":"1.1","severity":"blocking"},{"category":"unhandled-edge","check_key":"codex-nested-nonshell-activity","description":"Codex nested apply_patch and MCP/task calls remain behind the outer functions.exec record, so the stated every-CLI edit and task evidence is not proved by the direct-call golden fixture.","finding_id":"PR2-F14","fix":"Add a decision-complete projection for nested non-shell Codex tools, target the required execution-chain/parser code, and make the Codex golden fixture exercise actual functions.exec envelopes for edit, shell, and MCP/task calls.","location":"§§ 1.1, 1.3, and 1.5","prevention":"Compare every golden fixture call shape with the runtime execution-chain parser and cover nested non-shell tools explicitly.","principle":"Golden fixtures must use the current provider envelope for every behavior they claim to prove.","root_cause":"The plan handles command-bearing CodexNestedExecOutcome values but models edit and MCP/task activity as direct calls that current functions.exec orchestration can hide.","section_id":"1.1","severity":"blocking"}],"reviewer_session":"293f745c-e8a6-45cf-a65b-6a3a6b20c132","round":2,"round_number":2,"verdict":"needs_review"},"session_id":"9b24531b-013f-447f-a448-506cb5b3a818"}
```

**Round 3** `kind: verification`

- reviewer_run: bf329c98-50f9-4c02-bc6f-4d8aa888c8a7
- reviewer_session: 44f39b99-4a19-4af0-9f0b-9154c823dccf
- verdict: needs_review
- findings:
- PR3-F01 / blocking / §1.3 adapter forwarded every successful tool-result's output text, contradicting the Constraints retention boundary; no five-CLI exclusion sentinel
- PR3-F02 / blocking / four `_extract_digest_pairs` two-tuple equality assertions in `test_grok_parser.py` untargeted and unmigrated to `DigestPair`
- PR3-F03 / blocking / window-wide Codex item precedence was specified inside a forward-only streaming parser with no buffering/index contract
- PR3-F04 / blocking / compact_self refresh branched on the pre-digest Session object, so a just-landed digest could still take stale-summary/digest-missing paths
- PR3-F05 / blocking / the Codex golden fixture's CommandExecution edit could never populate `files_modified`; live rollouts show edits arrive as `FileChange` items the plan never modeled
- PR3-F06 / blocking / the bounded tail window relabelled its oldest retained prompt as `initial_goal` for transcripts over the record cap
- PR3-F07 / blocking / the offload-exempt continuation block had no total size budget while `user_profile_content` is read uncapped
- PR3-F08 / blocking / plan prose said a tool-only turn yields only a user message, but Grok's `_segment_pair_messages` appends an empty assistant sentinel
- PR3-F09 / blocking / `canonical_tool_name(str, dict)` was not total over nullable `ParsedMessage.tool_name`/`tool_input` (Qwen null `functionCall.name`)
- PR3-F10 / blocking / `_read_undigested_turns` collapses any malformed line to an empty batch, indistinguishable from nothing-undigested at pre-summary time
- PR3-F11 / blocking / #20635's stored validation criteria still promised blocked-by edges on 2.1 and 2.2 only, conflicting with the round-2 plan repair adding 2.3 (fixer-induced, round 2, PR2-F04)
- PR3-N01 / nit / ledger caps said "per assistant message" while the side-field design builds one ledger per user-to-user turn
- votes: 12 presented, 12 accepted, 0 declined (unattended coordinator judgment; every finding verified against the repository before voting — F02 via the two-tuple assertions at test_grok_parser.py:409/437/501/538; F03 via codex.py iter_parse_events' documented no-lookahead per-ParsedMessage-index contract and its four resume consumers (processor_transcripts, transcript_index, transcript_reader, transcript_window); F04 via `_refresh_compact_handoff_context` branching entirely on its passed-in `session` and `persist_digest_state` returning a fresh Session; F05 conclusively via live 2026-08 Codex rollouts — edits arrive as `item_completed` `FileChange` items with `changes: {path: {unified_diff}}` and the apply_patch-as-CommandExecution theory was a false positive; F06 via `_read_transcript`'s `deque(maxlen)` tail and the analyzer's first-user-record `initial_goal`; F07 via `read_user_profile_content` having no size cap; F08 via `_segment_pair_messages`'s `flush(empty_if_pending=True)`; F09 via `ParsedMessage.tool_name: str | None` and qwen.py's isinstance-else-None mapping; F10 via the catch-all `except` returning an empty batch; F11 via #20635's stored criteria. Three repairs amended by the coordinator with recorded rationale: F03 — item projection moved to a shared raw-window pre-scan `codex_item_activity` instead of respecifying parser index semantics, keeping `iter_parse_events` byte-identical; F05 — model `FileChange` items directly rather than parsing edit commands out of CommandExecution; F07 — reuse `inline_context_budget_for` with priority-ordered truncation markers instead of a new constant plus a keep-pending dead-letter branch)
- resolution_notes: Constraints: ledger caps re-worded to one ledger per extracted user-to-user turn (N01). §1.1: tool-only prose preserves Grok's empty assistant sentinel with exact-shape assertions folded into 1.1.4 (F08); `canonical_tool_name(tool_name: str | None, tool_input: Any)` made total with the `"unknown-tool"` fallback and acceptance 1.1.13 (F09); Codex item projection moved into `tool_activity.py::codex_item_activity`, a bounded raw-window pre-scan shared by the ledger and the 1.3 adapter, now also surfacing `FileChange` items as one `apply_patch <path>` entry per `changes` key, with `iter_parse_events`/`parsed_index` byte-identical and acceptance 1.1.10 rewritten (F03, F05). §1.2: `test_grok_parser.py::*` targeted with the four assertions migrated and a no-two-field-expectations clause in 1.2.6 (F02); `_read_undigested_turns` gains the stable-read policy — drop a malformed final line as an in-flight partial write, raise a typed `TranscriptReadError` for interior corruption routed to `{"error"}`/`digest_fallback` — with acceptance 1.2.7 (F10). §1.3: the adapter's tool_result content now enforces the retention boundary (failed → bounded error text, commit-producing → commit output, all else empty) with acceptance 1.3.9 (F01); `FileChange` blocks reach `files_modified` via the shared pre-scan with acceptance 1.3.6 updated (F05); `_read_transcript` targeted with a truncation signal plus a bounded first-goal scan (`SUMMARY_FIRST_GOAL_SCAN_RECORDS = 200`) so `initial_goal` is never the tail's first prompt, acceptance 1.3.7 extended (F06). §1.4: both compact_self refresh functions re-fetch the Session after every non-error digest outcome before their metadata/digest/fallback checks, acceptance 1.4.2 extended to the persisted-revision proof (F04). §1.5: the Codex fixture's edit is a `FileChange` item and every fixture gains a successful sentinel read proven excluded end to end by new acceptance 1.5.5 (F01, F05). §2.1: `render_compact_continuation_block` gains a `budget` from `inline_context_budget_for(session.source)` with priority-ordered sections and explicit truncation markers — the block always fits and delivery never strands pending state — with acceptance 2.1.12 (F07). §3/#20635: the task's stored validation criteria were updated via `update_task` to name blocked-by edges on 2.1, 2.2, and 2.3 with the adapter-fidelity race rationale, restoring plan/task parity (F11).

```json plan-review-round
{"evidence_id":"266c2bfd-26b7-4a4c-9917-9b178e4ee200","plan_hash":"37e029fe4ad7913e4be3a23c6ad162fdd798a9fef55b119d1eb546514169ff93","round_number":3,"round_result":{"coverage_attestation":{"adjacent_variant_complete":true,"attestation_digest":"ed501b3fd6a6e7e385884aee133d23647530bdd6f12cb2f56096758f867ca255","cross_lane_interaction_complete":true,"disposition_counts":{"dismissed":4,"emitted_findings":12,"total":16},"evidence_id":"266c2bfd-26b7-4a4c-9917-9b178e4ee200","lanes":[{"candidate_count":5,"lane_id":"requirements_traceability","status":"completed"},{"candidate_count":2,"lane_id":"repository_blast_radius","status":"completed"},{"candidate_count":9,"lane_id":"runtime_invariants","status":"completed"}],"shadow_manifest_status":{"entry_count":8,"manifest_digest":"b563a097c02635bc31d7fcf9ee39da381ce64e955d85c6df2cc69e583a69bab2","status":"valid"},"source_digest":"e9696cc218bfb2e2c6525f622736944b2a33f07726792b939bc4ead1631ed6b8","version":1},"findings":[{"category":"traceability","check_key":"successful-result-retention-parity","description":"Section 1.3 sends complete successful tool-result output into TranscriptAnalyzer, contradicting the Constraint that no successful non-commit result text enters the analyzer, digest, or summary. The five-CLI suite has no read-only sentinel proving exclusion.","finding_id":"PR3-F01","fix":"Specify correlation in analyzer_turns_from_transcript so failed results retain bounded error text, successful commit-producing calls retain only normalized commit evidence, and every other successful result has empty content. Add a five-CLI sentinel assertion that read-only output is absent from analyzer context, digest input, and summary prompt.","location":"P1 / §§ 1.3 and 1.5","prevention":"Trace success and failure payloads through ledger, analyzer, digest, and summary inputs, with both presence and absence assertions.","principle":"Every consumer of tool results must preserve the plan's explicit retention boundary.","root_cause":"The analyzer adapter forwards output text for every tool_result while the ledger path filters successful results to commit-producing calls.","section_id":"1.3","severity":"blocking"},{"category":"traceability","check_key":"digest-pair-test-consumer-migration","description":"tests/sessions/transcripts/test_grok_parser.py contains four _extract_digest_pairs equality assertions against two-tuples. Section 1.2 changes the function to three-field DigestPair values but neither targets nor migrates that file, so the existing tests will fail.","finding_id":"PR3-F02","fix":"Add tests/sessions/transcripts/test_grok_parser.py::* to §1.2 Targets, migrate all four expectations to named DigestPair fields or triples including activity, and add acceptance that no two-field _extract_digest_pairs expectations remain.","location":"P1 / § 1.2","prevention":"Run an indexed content sweep for every changed return symbol and enumerate production plus test consumers before finalizing Targets.","principle":"A tuple-like return-shape change must migrate every constructor, destructure, equality assertion, fake, and test seam.","root_cause":"The round-2 repair targeted the coverage-audit destructures but missed four direct two-tuple expectations in test_grok_parser.py.","section_id":"1.2","severity":"blocking"},{"category":"unhandled-edge","check_key":"codex-item-stream-buffering-index-contract","description":"Current iter_parse_events can emit an exec-wrapper outcome before a later item_completed record reveals that the wrapper must be suppressed, and it increments parsed_index once per native record. Emitting tool-use plus tool-result ParsedMessages therefore risks double counting, duplicate indexes, and broken resumable windows.","finding_id":"PR3-F03","fix":"Define one bounded pre-scan shared by ledger extraction and analyzer adaptation before any activity is emitted. Specify one parsed-index increment per emitted ParsedMessage with unique IDs and stable resume boundaries, and target tests/sessions/transcripts/test_streaming_parser.py plus transcript-window seams using real wrapper-before-item ordering.","location":"P1 / §§ 1.1 and 1.3","prevention":"For every one-to-many parser projection, test wrapper-before-authoritative-record ordering, index increments, stable IDs, chunk resumes, and all iter_parse_events consumers.","principle":"A streaming parser that expands native records must define lookahead, parsed-index, identity, and resume-boundary semantics before consumers rely on it.","root_cause":"The plan adds window-wide item precedence and multi-record item projection to a currently forward-only one-record Codex iterator without specifying the buffering or index contract.","section_id":"1.1","severity":"blocking"},{"category":"unhandled-edge","check_key":"post-digest-session-refresh","description":"Section 1.4 never reloads the foreground Session before its existing metadata, digest, and fallback checks. A successful pre-digest can therefore still select the stale-summary or digest-missing branch; the only final-turn digest-count proof remains non-manifest live verification.","finding_id":"PR3-F04","fix":"After every non-error digest outcome, including None after possible lock contention, reload the Session from session_manager before the foreground metadata/digest/fallback logic. Add a manifest-backed test starting with an undigested compact-triggering turn and asserting the persisted summary revision contains its new digest count and tool facts.","location":"P1 / § 1.4 foreground compact_self refresh","prevention":"At every digest-to-summary boundary, verify the post-lock Session object and add an integrated revision assertion for the triggering turn.","principle":"Code that branches on newly persisted state must reload that state after the mutation completes.","root_cause":"persist_digest_state returns a newly loaded Session, while the foreground compact_self refresh continues with the Session object captured before build_turn_and_digest.","section_id":"1.4","severity":"blocking"},{"category":"traceability","check_key":"codex-command-edit-path-projection","description":"The planned native Codex golden fixture cannot place src/pkg/widget.py under Files Modified or unlock file_changes because its authoritative edit record becomes a shell tool_use with no specified path projection.","finding_id":"PR3-F05","fix":"Specify a bounded structured path projection for supported CommandExecution edit commands, or project the real native record into a normalized edit block before analysis. Add an item-stream regression proving the path reaches files_modified and file_changes without a direct apply_patch function-call fixture.","location":"P1 / §§ 1.3 and 1.5","prevention":"Trace every native golden-fixture record through normalization and each structured summary field before claiming parity.","principle":"A golden fixture must use a provider envelope that can reach every structured fact it asserts.","root_cause":"The Codex fixture represents its edit as CommandExecution, while TranscriptAnalyzer adds files only for explicit edit-tool names and treats shell tools solely as possible commits.","section_id":"1.3","severity":"blocking"},{"category":"unhandled-edge","check_key":"bounded-tail-original-goal","description":"For transcripts over 20,000 records, structured context will assert a recent tail prompt as Original Goal, contradicting the digest narrative designated as history for older facts.","finding_id":"PR3-F06","fix":"Add src/gobby/sessions/summary_transcripts.py::_read_transcript to Targets and either return truncation metadata that suppresses tail-derived initial_goal or separately read the true first user record with bounded work. Extend the 50,000-record test with distinct first and tail prompts.","location":"P1 / § 1.3 bounded analyzer window","prevention":"Test bounded readers with distinct true-first and tail-first prompts and assert provenance-sensitive fields remain correct.","principle":"A tail window must never relabel its oldest retained prompt as the session's original goal.","root_cause":"The bounded transcript reader exposes no truncation metadata and TranscriptAnalyzer unconditionally uses the first supplied user record as initial_goal.","section_id":"1.3","severity":"blocking"},{"category":"unhandled-edge","check_key":"continuation-total-size-budget","description":"USER.md can legally approach 1 MiB, while normal result offloading begins near 15,000 characters and existing inline-context limits are much smaller. A valid profile or large MCP ledger can therefore create a model-hostile response after the continuation has been cleared.","finding_id":"PR3-F07","fix":"Use a fixed provider-safe COMPACT_CONTINUATION_MAX_CHARS or an existing Grok context limit, fit before claim-and-clear, and define priority as required skills, task context, durable MCP evidence, then profile text with explicit truncation markers. Test maximum-size profile and ledger inputs and keep state pending when the minimal required block cannot fit.","location":"P2 / § 2.1","prevention":"Compare every inline context aggregate with provider limits and maximum sizes of each contributing field.","principle":"A mandatory inline delivery channel needs a provider-safe total budget before one-shot state is consumed.","root_cause":"render_compact_continuation_block includes user_profile_content verbatim and the delivery tools bypass result offloading without any aggregate bound.","section_id":"2.1","severity":"blocking"},{"category":"unhandled-edge","check_key":"grok-tool-only-sentinel-parity","description":"The plan says a tool-only turn yields only a user message, but Grok _segment_pair_messages appends an empty assistant message before digest filtering removes it. Following the prose can alter parser message count and shift persisted cursor behavior.","finding_id":"PR3-F08","fix":"State that Grok extract_last_messages preserves its empty assistant sentinel while _extract_digest_pairs normalizes it to an empty response. Add a Grok tool-only fixture asserting exact flag-off/flag-on roles and content plus unchanged digest cursor movement.","location":"P1 / §§ 1.1 and 1.2","prevention":"Record exact pre/post-flag role and content arrays for each provider, including empty sentinels, before describing a shared invariant.","principle":"Parser-specific sentinel behavior must be preserved exactly when cursor validity depends on message shape.","root_cause":"The plan generalizes tool-only behavior across providers despite Grok's explicit empty assistant sentinel.","section_id":"1.1","severity":"blocking"},{"category":"unhandled-edge","check_key":"nullable-tool-normalization","description":"Qwen can emit a tool_use with name null when functionCall.name is present but null. One malformed native record can therefore abort ledger generation or summary analysis even though only malformed MCP wrappers have a fallback.","finding_id":"PR3-F09","fix":"Specify canonical_tool_name(tool_name: str | None, tool_input: Any) as total: normalize missing or non-string names to a stable unknown label, non-mapping inputs to an empty mapping, and rendered values safely to strings. Add malformed native Qwen, Droid, and Codex acceptance cases.","location":"P1 / §§ 1.1 and 1.3","prevention":"Feed missing, null, scalar, and malformed native tool fields from every parser through canonicalization and rendering.","principle":"Canonicalization must be total over every record shape admitted by provider parsers.","root_cause":"ParsedMessage permits null tool_name and tool_input values, while canonical_tool_name requires a string and mapping and both new consumers process every tool_use.","section_id":"1.1","severity":"blocking"},{"category":"unhandled-edge","check_key":"partial-transcript-digest-read","description":"During concurrent transcript writing, a partial final JSONL record can make the pre-summary digest report nothing undigested. Section 1.4 then proceeds without retry or fallback reason and can regenerate the same stale summary defect it is intended to close.","finding_id":"PR3-F10","fix":"Specify a bounded stable-read policy: retry a malformed trailing line, accept only the last complete prefix when safe, reject malformed interior records, and surface a typed error when stability is not reached so compact_self uses digest_fallback. Add a pending-pair-plus-partial-tail acceptance test.","location":"P1 / §§ 1.2 and 1.4","prevention":"Test concurrent transcript reads with partial trailing JSON and malformed interior records at every pre-summary entry point.","principle":"A transient or malformed transcript read must remain distinguishable from a verified no-work result.","root_cause":"_read_undigested_turns catches any JSON decode failure and returns an empty batch, which build_turn_and_digest collapses to None.","section_id":"1.2","severity":"blocking"},{"category":"traceability","causal_finding_id":"PR2-F04","causal_section_ids":["3"],"check_key":"deferred-task-contract-parity","description":"The plan now requires #20635 to wait for 2.1, 2.2, and 2.3 because both touch adapter-fidelity.md, while the task source of truth still omits 2.3. Expansion therefore has conflicting requirements.","finding_id":"PR3-F11","fix":"Before resubmission, have the coordinator update #20635's validation criteria to name 2.3, retain the §3 pre-expansion parenting instruction, and verify all three blocked-by edges are created atomically when the leaves exist.","introduced_in_round":2,"location":"§ 3 deferral / § 2.3","prevention":"After every accepted deferral-edge repair, re-read the referenced task and compare its criteria, parent, labels, and dependency set with the revised section.","principle":"The persisted deferred-task contract and the plan must agree on every expansion dependency.","root_cause":"The round-2 repair added 2.3 to §3 but did not update #20635's stored validation criteria, which still promise only 2.1 and 2.2.","section_id":"3","severity":"blocking"},{"category":"traceability","check_key":"ledger-budget-unit-wording","description":"The 80-line and 6,000-character reset boundary is terminologically inconsistent and can yield different implementations for turns containing multiple assistant records.","finding_id":"PR3-N01","fix":"Change the Constraint to one rendered ledger per extracted user-to-user turn and add a multi-assistant-record fixture proving one aggregate cap and one omission count.","location":"Constraints / §§ 1.1 and 1.2","prevention":"Name the aggregation key beside every line and character budget and test multiple provider records within that key.","principle":"Resource limits need one unambiguous aggregation unit.","root_cause":"Constraints say per assistant message while the side-field design constructs one ledger per user-to-user turn.","section_id":"1.1","severity":"nit"}],"reviewer_session":"44f39b99-4a19-4af0-9f0b-9154c823dccf","round":3,"round_number":3,"verdict":"needs_review"},"session_id":"9b24531b-013f-447f-a448-506cb5b3a818"}
```

**Round 4** `kind: verification`

- reviewer_run: 66da61a5-6504-4d8b-93a9-4c8bc2842b7f
- reviewer_session: 93c17ebd-80a6-4cae-9155-233f7d0957e1
- verdict: needs_review
- findings:
- PR4-F01 / blocking / §1.3 truncation signal had no exact API; `summary_generation.generate_summary` also consumes `_read_transcript`'s list contract (fixer-induced, round 3, PR3-F06)
- PR4-F02 / blocking / §1.4 lacked a dependency on 1.2, so its leaf could validate before digest pairs carry the tool facts acceptance 1.4.2 requires
- PR4-F03 / blocking / successful non-commit results carried no marker, so a passed test run was indistinguishable from a call whose result never arrived
- PR4-F04 / blocking / adapter declared Iterator while `extract_handoff_context` does repeated forward passes plus two `reversed(turns)` scans (fixer-induced, round 1, PR1-F10)
- PR4-F05 / blocking / dropping a malformed final line while advancing the pair cursor can permanently lose the record when it completes inside the already-digested final pair (fixer-induced, round 3, PR3-F10)
- PR4-F06 / blocking / corruption-policy split between digest (raises), `_read_transcript` (skips), and dispatcher (continues) permits a false-fresh summary (fixer-induced, round 3, PR3-F10)
- PR4-F07 / blocking / window-level Codex item precedence drops unmatched wrappers in hybrid windows and lacks source positions for per-turn attribution (fixer-induced, round 3, PR3-F03)
- PR4-F08 / blocking / budget bounded only the continuation string while the offload-exempt response also carries base context and framing (fixer-induced, round 3, PR3-F07)
- votes: 8 presented, 8 accepted, 0 declined (unattended coordinator judgment; every finding verified against the repository before voting — F01 via `_read_transcript`'s second production caller at summary_generation.py:412; F02 via the §1.4 heading carrying no depends clause; F03 via §1.1's discarded-unread success outputs; F04 via the two `reversed(turns)` scans at analyzer.py:180/192; F05 via §1.2's own pair-cursor arithmetic; F06 via `_read_transcript`'s skip-malformed-line loop and acceptance 1.4.4's summary-still-generated wording; F07 conclusively via live 2026-08 Codex rollouts — item ids are fresh `exec-<uuid>` values with zero overlap against 166 wrapper `call_…` ids, so id-linkage does not exist and per-call joins must use content identity; F08 via the offload exemption plus the 9KB+ pre-compaction summaries observed in practice. Four repairs amended by the coordinator with recorded rationale: F03 — a universal `(no result recorded)` marker from the existing result-correlation maps instead of a per-provider test-command recognition registry, keeping success evidence status-only; F05 — one bounded tail re-read then withhold-the-trailing-pair by reusing catch-up mode's existing leave-trailing-pair-undigested mechanism, instead of an open-ended retry loop; F07 — content-identity joins within `record_index`-derived turn partitions, because the live rollouts prove wrapper/item id linkage does not exist; F08 — remaining-capacity budgeting with a by-reference base-stub swap on wait_for_summary reusing the existing SessionStart injection pointer precedent, with get_handoff_context non-consuming as the reference target)
- resolution_notes: §1.4 heading now `(depends: 1.2)` (F02). §1.3: exact windowed-read API — new `TranscriptWindow` NamedTuple and `_read_transcript_window` own the read loop and the `truncated` flag while `_read_transcript` keeps its list contract by delegation with both existing callers enumerated and untouched; 20,000/20,001 boundary tests in acceptance 1.3.11 (F01). §1.3: `analyzer_turns_from_transcript` returns a materialized list surviving the analyzer's full traversal contract, acceptance 1.3.10 (F04). §1.2: stable-tail protocol — one bounded re-read (`TRANSCRIPT_TAIL_RETRY_DELAY_SECONDS = 0.2`), then withhold the trailing pair without advancing the cursor and report `tail_withheld: True`; acceptance 1.2.7 rewritten and 1.2.8 added for eventual-inclusion with exact cursor movement (F05). §§1.2–1.4: one corruption contract — shared `TranscriptReadError` lives in `transcripts/base.py`, `_read_transcript_window` raises on interior corruption, `build_turn_and_digest` maps it to `error_kind: transcript_read`, the dispatcher aborts the refresh, and compact_self's refresh persists nothing on corruption; acceptance 1.3.12, 1.4.4 amended, 1.4.10, 1.4.11 (F05, F06). §1.1: universal result correlation flips a `resolved` flag for every call; bare line = success, `! failed:` = failure, `(no result recorded)` = in-flight; Constraints and the turn_record prompt teach the semantics; acceptance 1.1.14, golden coverage 1.5.6 (F03). §1.1/§1.3: `codex_item_activity` entries are source-positioned (`record_index`), partitioned into user-to-user turns, with per-call wrapper suppression by content identity; unmatched wrappers keep their execution-chain derivation; acceptance 1.1.10 amended, 1.1.15, 1.3.6 amended, mixed-window/split-tail golden fixture and 1.5.7 (F07). §2.1: `attach_compact_continuation` budgets the whole serialized response — remaining capacity after base and framing, `CONTINUATION_FRAMING_MARGIN = 256`, base-stub swap on wait_for_summary, non-consuming return on get_handoff_context — acceptance 2.1.12 amended and 2.1.13 added (F08).

```json plan-review-round
{"evidence_id":"18d9c5a1-5563-4a87-be29-0dbe47884d85","plan_hash":"dbc23a63ec31ee2bc8b90f30c4147f0e4a9551a512ee3aebb4d1da53130f604d","round_number":4,"round_result":{"coverage_attestation":{"adjacent_variant_complete":true,"attestation_digest":"4953a47c1bf6ea44c2c281c0ca6e043529b55baa4f5dde341f2fc220bf420f80","cross_lane_interaction_complete":true,"disposition_counts":{"dismissed":0,"emitted_findings":8,"total":8},"evidence_id":"18d9c5a1-5563-4a87-be29-0dbe47884d85","lanes":[{"candidate_count":3,"lane_id":"requirements_traceability","status":"completed"},{"candidate_count":1,"lane_id":"repository_blast_radius","status":"completed"},{"candidate_count":4,"lane_id":"runtime_invariants","status":"completed"}],"shadow_manifest_status":{"entry_count":8,"manifest_digest":"0ce3156f72587476b971ea2f8ea01d79e350e3e4056cf7226f92411abc67a8f0","status":"valid"},"source_digest":"383e33299ea9416e810726793fc2aa7380826b1683426484f071fe8ebc246e5a","version":1},"findings":[{"category":"traceability","causal_finding_id":"PR3-F06","causal_section_ids":["1.3"],"check_key":"transcript-window-result-contract","description":"The plan is not decision-complete about how `_read_transcript` reports truncation, so an implementer can break the existing on-demand summary caller or omit the signal the new bounded-window logic needs.","finding_id":"PR4-F01","fix":"Specify an exact API, such as an opt-in metadata return that leaves ordinary calls list-valued, or a typed result with every caller migrated; target `summary_generation.generate_summary` when migration is required and add 20,000/20,001-record boundary tests.","introduced_in_round":3,"location":"§ 1.3 transcript window metadata","prevention":"Before changing a shared helper's return shape, enumerate all callers and state either their migrations or an explicit compatibility-preserving API.","principle":"A changed return contract must define its exact shape and account for every production consumer.","root_cause":"The round-3 truncation repair adds an unspecified signal to `_read_transcript`; the section's sample still treats the return as `list[dict]`, while `summary_generation.generate_summary` independently relies on that list contract.","section_id":"1.3","severity":"blocking"},{"category":"bad-sequencing","check_key":"pre-summary-ledger-dependency","description":"Section 1.4 has no dependency on 1.2, so its leaf may validate before digest pairs carry tool activity and cannot satisfy its own tool-fact assertion.","finding_id":"PR4-F02","fix":"Change the heading to include `(depends: 1.2)`; the existing 1.2→1.1 edge supplies the transitive parser prerequisite.","location":"§ 1.4 dependency declaration","prevention":"For each acceptance item, trace required runtime behavior back to its owning deliverable and add a dependency whenever the owner can otherwise execute later.","principle":"Manifest dependencies must include every leaf whose behavior is required by the dependent leaf's own acceptance criteria.","root_cause":"Acceptance 1.4.2 requires the persisted pre-compaction revision to contain tool facts, but section 1.4 can run before section 1.2, which is the leaf that feeds the ledger into `build_turn_and_digest`.","section_id":"1.4","severity":"blocking"},{"category":"missing-requirement","check_key":"successful-test-outcome-fidelity","description":"The initiating false handoff explicitly claimed no test results, yet the plan can only show that `pytest` was invoked. It cannot reliably report that tests passed.","finding_id":"PR4-F03","fix":"Add bounded status-only evidence for recognized test commands across all five providers, without retaining successful stdout, and extend the golden suite to assert pass/fail reaches the turn record and summary; alternatively make exclusion of successful test outcomes an explicit governing constraint.","location":"Overview / §§ 1.1 and 1.5","prevention":"Trace each fact named in the motivating failure through parser normalization, digest text, summary context, and a provider-parity assertion.","principle":"Every motivating fidelity fact must have an explicit representation and an acceptance test that distinguishes success, failure, and missing evidence.","root_cause":"The plan records test-command invocation and failed output, while successful non-commit results carry neither a success marker nor exit status; an unmatched call is indistinguishable from a successful test.","section_id":"1.1","severity":"blocking"},{"category":"traceability","causal_finding_id":"PR1-F10","causal_section_ids":["1.3"],"check_key":"analyzer-adapter-reiterability","description":"Passing the specified generator directly to the analyzer either raises at `reversed()` or exhausts records during the first pass, so non-Claude structured facts and recent activity cannot both be produced.","finding_id":"PR4-F04","fix":"Return a bounded `list`/`Sequence`, or materialize the adapter exactly once before calling the analyzer; update the annotation and acceptance to exercise initial-goal, reverse structured-fact, reverse recent-activity, and forward decision passes.","introduced_in_round":1,"location":"§ 1.3 analyzer adapter","prevention":"Inspect the full consumer traversal contract before choosing a streaming return type, including repeated and reverse iteration.","principle":"An adapter's collection type must satisfy every traversal performed by its concrete consumer.","root_cause":"`analyzer_turns_from_transcript` is declared as an `Iterator`, while `TranscriptAnalyzer.extract_handoff_context` performs multiple forward passes and two `reversed(turns)` scans.","section_id":"1.3","severity":"blocking"},{"category":"unhandled-edge","causal_finding_id":"PR3-F10","causal_section_ids":["1.2"],"check_key":"partial-tail-cursor-preservation","description":"The claim that a dropped final record is picked up by the next digest is false: once the pair cursor advances, completing the record creates no new pair and the evidence can be lost permanently.","finding_id":"PR4-F05","fix":"Retry until the tail is stable and valid, or withhold the affected final pair and its cursor increment; if stability is not reached within the pre-summary budget, raise `TranscriptReadError` and do not persist a summary from that incomplete turn.","introduced_in_round":3,"location":"§§ 1.2 and 1.4 partial transcript tail","prevention":"Test a partial final tool-result or assistant record that completes inside an already-user-anchored pair and assert exact cursor movement plus eventual ledger inclusion.","principle":"A digest cursor may advance only past evidence whose complete records were included in the persisted digest.","root_cause":"The round-3 repair drops a malformed final line, digests the complete prefix, and advances by pair count even when the completed line later enriches that same final pair.","section_id":"1.2","severity":"blocking"},{"category":"unhandled-edge","causal_finding_id":"PR3-F10","causal_section_ids":["1.2"],"check_key":"digest-summary-corruption-parity","description":"One corrupt interior record can fail the digest and then be silently omitted by the analyzer, producing an apparently fresh summary from incomplete evidence.","finding_id":"PR4-F06","fix":"Define one stable transcript-read contract: interior corruption aborts summary refresh and preserves the previous summary for retry; only a provably transient final partial may be retried or withheld. Add a dispatcher regression proving a digest `TranscriptReadError` cannot be followed by persistence of a summary that skipped the record.","introduced_in_round":3,"location":"§§ 1.2–1.4 transcript recovery","prevention":"Sweep every reader and fallback that consumes the same transcript, and test corruption at final, interior, digest-error, and summary-persistence boundaries.","principle":"All readers feeding one persisted summary must apply the same corruption policy and failure outcome.","root_cause":"The digest repair raises on malformed interior records, while `_read_transcript` still skips every malformed line and the dispatcher explicitly continues summary generation after a returned digest error.","section_id":"1.3","severity":"blocking"},{"category":"unhandled-edge","causal_finding_id":"PR3-F03","causal_section_ids":["1.1","1.3"],"check_key":"codex-precedence-correlation-scope","description":"A hybrid or in-flight window can contain item projections for some calls and wrapper-only evidence for another; window-wide suppression drops the unmatched wrapper and also lacks source positions needed to keep activity on its originating user turn.","finding_id":"PR4-F07","fix":"Make `codex_item_activity` return source-positioned, identity-keyed entries, partition them by user-to-user turn, and suppress only wrappers proven represented by an item. Add mixed-window and split-tail golden cases.","introduced_in_round":3,"location":"§§ 1.1, 1.3, and 1.5 Codex item stream","prevention":"Test mixed legacy/current envelopes, turn-boundary splits, and a wrapper whose matching item has not landed; dedupe each only by a stable call or command identity.","principle":"Deduplication precedence must operate at the identity scope where equivalence is proven.","root_cause":"The round-3 Codex pre-scan returns a flat window-level activity list and treats the presence of any item-completed tool record as authority for the whole window.","section_id":"1.1","severity":"blocking"},{"category":"unhandled-edge","causal_finding_id":"PR3-F07","causal_section_ids":["2.1"],"check_key":"continuation-response-total-budget","description":"A continuation near `inline_context_budget_for(source)` plus non-empty base context can exceed the intended ship limit even though the continuation alone passes its assertion.","finding_id":"PR4-F08","fix":"Compute remaining capacity after serializing or conservatively sizing the base response and framing, render continuation into that remainder, and define a non-consuming recovery when base context alone exceeds the cap. Test maximum summary plus maximum profile/MCP ledger for both delivery tools.","introduced_in_round":3,"location":"§§ 2.1–2.2 handoff delivery","prevention":"Budget every inline aggregate against the provider limit and test maximum values for every independently bounded component in combination.","principle":"A mandatory inline channel must bound the complete serialized response consumed by the provider.","root_cause":"The round-3 repair budgets only the new `continuation` string, while the offload-exempt tool response also contains summary or handoff context and serialization framing.","section_id":"2.1","severity":"blocking"}],"reviewer_session":"93c17ebd-80a6-4cae-9155-233f7d0957e1","round":4,"round_number":4,"verdict":"needs_review"},"session_id":"9b24531b-013f-447f-a448-506cb5b3a818"}
```

**Round 5** `kind: verification`

- reviewer_run: 5b34d0f9-2b60-45ff-8f4e-f25997c387df
- reviewer_session: a7796b25-e1fa-47bf-bf1c-ae623303ab7e
- verdict: needs_review
- findings:
- PR5-F01 / blocking / `TranscriptReadError` is consumed by §§1.2–1.3 but §1.1 only targets the exact `extract_last_messages` method, so the new sibling class in `base.py` has no manifest-backed owner (fixer-induced, round 4, PR4-F06)
- PR5-F02 / blocking / §1.3 adds `TranscriptWindow` and `_read_transcript_window` to `summary_transcripts.py` while its only Target for that file is the exact `_read_transcript` symbol (fixer-induced, round 4, PR4-F01)
- PR5-F03 / blocking / the Codex item pre-scan joined argv with spaces and read `status`, while the authoritative `_command_execution_outcomes` adapter unwraps `-c`/`-lc`, uses `shlex.join`, and derives success from integer `exit_code` — content-identity suppression could miss and a status-less nonzero exit could render as success (fixer-induced, round 4, PR4-F07)
- PR5-F04 / blocking / `compact_summary_metadata_matches` recomputes the source hash from empty turns and an empty `HandoffContext`, so a summary generated with the new transcript-derived facts is immediately judged stale
- PR5-F05 / blocking / the dispatcher outcome ladder had no `tail_withheld` branch, so a PRE_COMPACT digest that withheld the compact-triggering pair still fell through to summary generation (fixer-induced, round 4, PR4-F05)
- PR5-F06 / blocking / `pre_digest` was gated on "any running loop" while the per-session lock is bound to the configured daemon loop; a caller on a foreign loop could run the digest off-daemon (fixer-induced, round 1, PR1-F12)
- PR5-F07 / blocking / `_required_section_min()` was a fixed size, so a near-full response could truncate the actual required-skill list yet still clear the one-shots (fixer-induced, round 4, PR4-F08)
- PR5-F08 / blocking / continuation fit used raw rendered characters plus a fixed 256-char margin; JSON escaping of a large profile or ledger can exceed that margin (fixer-induced, round 4, PR4-F08)
- PR5-F09 / blocking / `SUMMARY_FIRST_GOAL_SCAN_RECORDS = 200` bounded the first-goal search by position although no provider guarantees the first user text within 200 records (fixer-induced, round 4, PR4-F01)
- PR5-F10 / blocking / the `deque(maxlen)` read loop is O(window) in memory but O(transcript) in I/O on every refresh, contradicting the plan's per-refresh bound (fixer-induced, round 4, PR4-F01)
- PR5-F11 / blocking / `get_handoff_context` was unconditionally offload-exempt yet returned its oversized base unchanged, so the reference target itself had no bounded delivery path and 2.1.13 could not hold for both tools (fixer-induced, round 4, PR4-F08)
- votes: 11 presented, 11 accepted, 0 declined (interactive user votes; every finding verified against the repository before presentation — F01 via `transcripts/base.py` carrying no `TranscriptReadError` and the exact method Target at §1.1; F02 via the exact `_read_transcript` Target at §1.3; F03 via `codex.py::_command_execution_outcomes` (`-c`/`-lc` unwrap, `shlex.join`, `exit_code == 0`) against §1.1's "argv joined with spaces, `status`"; F04 via `_summary_metadata.py` calling `extract_handoff_context([])` and `_build_summary_prompt_context(turns=[])`; F05 via the §1.4 ladder handling `tail_withheld` only in compact_self's refresh; F06 via `session_summary_dispatcher.py` consulting `self.loop` only in the no-running-loop branch; F07/F08 via §2.1's fixed `_required_section_min()` and `CONTINUATION_FRAMING_MARGIN = 256`; F09/F10 via §1.3's 200-record forward scan and full-file `deque(maxlen)` loop; F11 via §2.1's unconditional `_MANDATORY_EXEMPT_TOOLS` entry with `allow_base_stub=False`)
- resolution_notes: §1.1: `base.py` Target widened to `::*` with a scope reason naming the protocol flag and the shared `TranscriptReadError(ValueError)` (`path`, `line_number`), its contract stated in the body with acceptance 1.1.16 proving both §1.2 and §1.3 readers raise that class (F01); new shared module `transcripts/codex_items.py::normalize_command_execution` owns the canonical `CommandExecution` normalization (`-c`/`-lc` unwrap, `shlex.join`, integer `exit_code`, `success = exit_code == 0`), `codex.py::_command_execution_outcomes` delegates to it byte-identically, `codex_item_activity` uses it for shell entries and fails them on `success is False` regardless of `status`, wrapper↔item correlation keys on normalized command plus `success` and consumes matches one-to-one in source order, acceptance 1.1.10 amended and 1.1.17 added with the `[/bin/zsh, -lc, cmd]`, status-less nonzero `exit_code`, and duplicate-command regressions, 1.3.6 and 1.5.7 aligned (F03). §1.3: `summary_transcripts.py` Target widened to `::*` naming `TranscriptWindow`, `_read_transcript_window`, `_read_first_user_goal`, and the delegating `_read_transcript` (F02); `_read_transcript_window` is a bounded reverse tail reader — backward 64 KiB chunks, at most `max_records + 1` complete lines parsed, `truncated` derived from the extra record or remaining preceding bytes — with I/O-counting acceptance 1.3.14 independent of transcript length (F10); `SUMMARY_FIRST_GOAL_SCAN_RECORDS` deleted in favour of `_read_first_user_goal`, a forward `iter_parse_events` stream with O(1) retained state that stops at the first provider-normalized user text or EOF, acceptance 1.3.7 extended with a >200-record pre-user prefix (F09); new `summarize.py::build_summary_source_context` builds the canonical `SummarySourceContext` (window, analyzer facts, prompt context, template, `source_hash`) consumed by both `_generate_session_summary_core` and `compact_summary_metadata_matches`, the latter targeted and rewritten to compare the shared hash, acceptance 1.3.13 proves a just-generated summary matches immediately (F04). §1.4: `dispatch` decides `pre_digest` by loop identity — `running is daemon` ⇒ in-loop task with `pre_digest=True`; daemon running and `running is not daemon` (foreign loop or no loop) ⇒ the whole coroutine goes to the daemon loop via `run_coroutine_threadsafe` with `pre_digest=True`; no running daemon loop ⇒ `pre_digest=False` with the skip log — acceptance 1.4.5 rewritten and 1.4.13 adds the two-running-loop regression (F06); the dispatcher ladder gains a `tail_withheld` branch that returns before `generate_session_summaries`, preserving the prior revision, with acceptance 1.4.12 (F05). §2.1: `render_compact_continuation_block(variables, *, fits)` takes the final serialized-size predicate `_serialized_len({**result, "continuation": candidate}) <= inline_context_budget_for(source)` (same `json.dumps(ensure_ascii=False, default=str)` measure as `result_offload._serialized_size`), `CONTINUATION_FRAMING_MARGIN` and `_required_section_min()` are deleted, the required-skill section is rendered in full from pre-clear variables inside the atomic callback and never truncated — when it does not fit the callback returns `None` without mutation and flags the stub retry — acceptance 2.1.12/2.1.13 rewritten and 2.1.14/2.1.15 added for long required lists and escape-heavy payloads (F07, F08); both delivery tools leave `_MANDATORY_EXEMPT_TOOLS` in favour of a content-scoped exemption (`_CONTINUATION_DELIVERY_TOOLS` plus a top-level `continuation` key), so a delivered response is never offloaded while `get_handoff_context`'s oversized non-claim base offloads normally to a bounded `gobby-results` envelope with the one-shots still armed; acceptance 2.1.11 and 2.1.13 rewritten, 2.1.16 added (F11). Review cap (5) reached: no further adversary round is launched; continuation is through the explicit human-handoff tools.

```json plan-review-round
{"evidence_id":"ff31d7e2-3bf1-4634-9333-f7ab80b9cb2e","plan_hash":"05650fcd60ce8985966fc22ccd50de781d7a668a391bc686fd4037360fb498a0","round_number":5,"round_result":{"coverage_attestation":{"adjacent_variant_complete":true,"attestation_digest":"f6ff491c368869ccedc275677bbc223097397d8b818906d9be7adf85414966ab","cross_lane_interaction_complete":true,"disposition_counts":{"dismissed":2,"emitted_findings":11,"total":13},"evidence_id":"ff31d7e2-3bf1-4634-9333-f7ab80b9cb2e","lanes":[{"candidate_count":2,"lane_id":"requirements_traceability","status":"completed"},{"candidate_count":2,"lane_id":"repository_blast_radius","status":"completed"},{"candidate_count":9,"lane_id":"runtime_invariants","status":"completed"}],"shadow_manifest_status":{"entry_count":8,"manifest_digest":"adfcb90fb0f8bb0b8d9c97d1d9ecfb049b57b0b120450e967b90035c78940503","status":"valid"},"source_digest":"50679da7b5987363a4e2e13b6fc7c8e865e4582902efbec35fc8a82ddae2f088","version":1},"findings":[{"category":"gobby-format","causal_finding_id":"PR4-F06","causal_section_ids":["1.2","1.3","1.4"],"check_key":"shared-exception-target-ownership","description":"TranscriptReadError is required by the digest and summary readers but has no manifest-backed owner. The current base.py has no such symbol, and the exact method Target cannot authorize adding a sibling top-level class.","finding_id":"PR5-F01","fix":"Make §1.1 the explicit owner: replace the base.py exact Target with `src/gobby/sessions/transcripts/base.py::*` plus a scope reason naming the protocol change and TranscriptReadError, define the exception contract in the body, and add acceptance proving §§1.2 and 1.3 use it.","introduced_in_round":4,"location":"P1 / §§ 1.1–1.3 transcript corruption contract","prevention":"For every newly named shared type, verify its owning section defines it, its Target covers its indexed scope, and every consuming section depends on that owner.","principle":"Every new shared symbol in an existing symbol-bearing file needs one self-contained deliverable owner, a covering Target, and acceptance for its consumers.","root_cause":"The round-4 corruption repair refers to TranscriptReadError as a section 1.1 artifact, while section 1.1 targets only TranscriptParser.extract_last_messages and never defines or accepts the new sibling class.","section_id":"1.1","severity":"blocking"},{"category":"gobby-format","causal_finding_id":"PR4-F01","causal_section_ids":["1.3"],"check_key":"new-sibling-symbol-target-coverage","description":"Section 1.3 specifies two new top-level symbols in summary_transcripts.py, but its only Target for that file is `_read_transcript`. Expansion would hand an agent a scope that cannot cover the required siblings.","finding_id":"PR5-F02","fix":"Replace the exact Target with `src/gobby/sessions/summary_transcripts.py::*` and a scope reason naming TranscriptWindow, _read_transcript_window, and the compatibility-preserving _read_transcript delegation.","introduced_in_round":4,"location":"P1 / § 1.3 transcript-window API","prevention":"After introducing any top-level helper or type in an existing symbol-bearing file, compare the planned symbol inventory with every exact Target.","principle":"An exact symbol Target covers changes to that symbol; new peer symbols in the same existing file require their own valid scope.","root_cause":"The round-4 window API repair adds TranscriptWindow and _read_transcript_window beside _read_transcript while retaining only the exact _read_transcript Target.","section_id":"1.3","severity":"blocking"},{"category":"unhandled-edge","causal_finding_id":"PR4-F07","causal_section_ids":["1.1","1.3","1.5"],"check_key":"codex-command-execution-canonicalization","description":"The same CommandExecution can normalize differently in the new pre-scan and existing `codex_exec_outcomes`, defeating content-identity suppression and duplicating activity; a status-less nonzero exit can also render as success.","finding_id":"PR5-F03","fix":"Make codex_item_activity reuse `_command_execution_outcomes` or a shared canonical normalizer, correlate on normalized command plus result.success, consume identical matches one-to-one in source order, and add mixed-window regressions for `[/bin/zsh,-lc,cmd]`, nonzero exit_code without status, and duplicate commands with only one landed item.","introduced_in_round":4,"location":"P1 / §§ 1.1, 1.3, and 1.5 Codex item precedence","prevention":"Reuse the provider parser's canonical outcome adapter and test shell-wrapper argv, nonzero exit codes without status, repeated identical calls, and split-tail partial coverage.","principle":"One native event must have one canonical command and completion representation before cross-envelope deduplication.","root_cause":"The proposed item pre-scan joins argv with spaces and reads `status`, while the existing authoritative CommandExecution adapter unwraps `-c`/`-lc`, otherwise uses shlex.join, and derives success from integer exit_code.","section_id":"1.1","severity":"blocking"},{"category":"bad-sequencing","check_key":"summary-source-hash-parity","description":"A new summary containing files, tasks, commits, or recent activity will be hashed with those facts, then compared against a hash rebuilt without them. compact_self will treat the fresh revision as stale and route to fallback.","finding_id":"PR5-F04","fix":"Add `src/gobby/mcp_proxy/tools/sessions/_summary_metadata.py::compact_summary_metadata_matches` to §1.3 and share the bounded transcript/analyzer hash payload with generation, or persist one canonical payload both paths consume; add a digest-present tool-facts regression where a just-generated summary matches.","location":"P1 / §§ 1.3–1.4 summary generation and compact freshness","prevention":"When a persisted source hash gains an input, sweep every producer and recomputation consumer and prove a freshly generated artifact validates immediately.","principle":"Summary generation and freshness validation must hash the same canonical source-context payload.","root_cause":"Section 1.3 adds transcript-derived analyzer facts to generation, while the untargeted compact_summary_metadata_matches consumer still recomputes summary context from empty turns and an empty HandoffContext.","section_id":"1.3","severity":"blocking"},{"category":"unhandled-edge","causal_finding_id":"PR4-F05","causal_section_ids":["1.2","1.4"],"check_key":"tail-withheld-dispatcher-abort","description":"On PRE_COMPACT, a `tail_withheld: true` outcome falls through to generate_session_summaries even though the final pair is intentionally absent, recreating the stale-summary race.","finding_id":"PR5-F05","fix":"Branch on tail_withheld before dispatcher summary generation, preserve the prior revision, and retry after the transcript stabilizes; add a PRE_COMPACT test proving no revision is persisted until the next digest includes the withheld pair.","introduced_in_round":4,"location":"P1 / §§ 1.2 and 1.4 pre-summary digest","prevention":"Enumerate every field in a changed outcome contract at every caller and test each branch through persistence.","principle":"No summary may be persisted as current while the compact-triggering pair is deliberately withheld from the digest.","root_cause":"The dispatcher outcome ladder handles None, transcript corruption, generic failure, and ordinary success, but omits tail_withheld; only compact_self's separate refresh path handles it.","section_id":"1.4","severity":"blocking"},{"category":"unhandled-edge","causal_finding_id":"PR1-F12","causal_section_ids":["1.4"],"check_key":"daemon-loop-identity","description":"A caller running on a second event loop can still execute the pre-digest off-daemon, binding or awaiting the lock on the wrong loop and racing the normal turn-end digest.","finding_id":"PR5-F06","fix":"Gate pre_digest on loop identity. When the configured daemon loop is running and differs from the caller loop, schedule the entire digest-and-summary coroutine there with run_coroutine_threadsafe; add a two-running-loop regression.","introduced_in_round":1,"location":"P1 / § 1.4 SessionSummaryDispatcher scheduling","prevention":"Compare loop identity at every scheduling boundary and test caller-loop, daemon-loop, and no-loop cases.","principle":"A loop-bound per-session asyncio.Lock is safe only when every digest is scheduled on the same configured daemon event loop.","root_cause":"The plan equates the presence of any running loop with the daemon loop, while dispatch currently schedules on asyncio.get_running_loop and consults self.loop only when no loop is running.","section_id":"1.4","severity":"blocking"},{"category":"unhandled-edge","causal_finding_id":"PR4-F08","causal_section_ids":["2.1"],"check_key":"required-skill-section-atomicity","description":"A near-full response can satisfy the fixed minimum but lack room for the actual required-skill list. The plan then delivers a truncated directive and clears skills that were never named.","finding_id":"PR5-F07","fix":"Compute the complete required section from pre-clear variables inside the atomic render callback and never truncate it; return None without mutation when it does not fit, let wait_for_summary stub the base and retry, and add long-list tests proving intact delivery or no clearing.","introduced_in_round":4,"location":"P2 / § 2.1 compact continuation claim","prevention":"Fit the actual highest-priority section inside the atomic pre-clear callback and test long lists at the remaining-capacity boundary.","principle":"One-shot required-skill state may clear only after the complete required directive is model-visible.","root_cause":"_required_section_min has no current variables, while the required-skill directive grows with an uncapped skill list; the renderer may truncate that section, return a nonempty block, and trigger full clearing.","section_id":"2.1","severity":"blocking"},{"category":"unhandled-edge","causal_finding_id":"PR4-F08","causal_section_ids":["2.1"],"check_key":"serialized-response-budget","description":"JSON quoting can expand a large task context, profile, or MCP ledger by far more than 256 characters, so the offload-exempt response can exceed the provider budget while the raw block passes.","finding_id":"PR5-F08","fix":"Fit each candidate continuation with the final serialized-size predicate, equivalent to `_serialized_size({**result, \"continuation\": candidate}) <= inline_context_budget_for(source)`, and test escape-heavy maximum payloads.","introduced_in_round":4,"location":"P2 / § 2.1 continuation response budget","prevention":"Run the fit predicate on the final response object and include quote-, backslash-, and control-heavy boundary fixtures.","principle":"A mandatory-inline channel must fit the final serialized response, including payload-dependent escaping.","root_cause":"The plan subtracts serialized base size but fits continuation by raw rendered characters with a fixed 256-character margin.","section_id":"2.1","severity":"blocking"},{"category":"unhandled-edge","causal_finding_id":"PR4-F01","causal_section_ids":["1.3"],"check_key":"initial-goal-search-completeness","description":"A valid transcript with its first user record at position 201 or later yields no initial_goal, so the analyzer falls back to the tail window and mislabels a recent prompt as the session goal.","finding_id":"PR5-F09","fix":"Stream from the start until the first provider-normalized user text or EOF while retaining O(1) state, or persist the initial goal once discovered; add a fixture with more than 200 pre-user records.","introduced_in_round":4,"location":"P1 / § 1.3 bounded-window provenance","prevention":"Bound retained state rather than semantic search position and test long pre-user metadata prefixes.","principle":"A bounded transcript window must preserve the actual first provider-normalized user goal.","root_cause":"The plan searches only the first 200 raw records, although no provider contract guarantees the first user text appears within that prefix.","section_id":"1.3","severity":"blocking"},{"category":"unhandled-edge","causal_finding_id":"PR4-F01","causal_section_ids":["1.3"],"check_key":"summary-window-io-bound","description":"The proposed implementation is O(window) in memory and O(transcript) in work. Since refresh reruns after every digest, long sessions repeatedly rescan their entire JSONL history, contradicting the plan's explicit per-refresh bound.","finding_id":"PR5-F10","fix":"Specify a bounded reverse tail reader that parses at most SUMMARY_ANALYZER_MAX_RECORDS plus one complete record and derives truncation from the extra record or preceding bytes; add an I/O-counting regression independent of transcript length.","introduced_in_round":4,"location":"P1 / § 1.3 transcript window","prevention":"Measure source records or bytes read on transcripts much larger than the configured window.","principle":"An O(window) per-refresh requirement bounds transcript I/O as well as retained memory and analyzer iteration.","root_cause":"The specified deque(maxlen) current read loop and `appended > len(turns)` truncation test still scan every transcript record on every refresh.","section_id":"1.3","severity":"blocking"},{"category":"unhandled-edge","causal_finding_id":"PR4-F08","causal_section_ids":["2.1"],"check_key":"handoff-reference-target-budget","description":"wait_for_summary can point at get_handoff_context, yet the reference target can exceed the same provider inline budget and cannot offload because of its unconditional exemption. Acceptance 2.1.13 therefore cannot hold for both tools.","finding_id":"PR5-F11","fix":"Give get_handoff_context a bounded non-recursive reference path: persist the oversized base before any one-shot claim and return a compact gobby-results handle plus any fitting continuation, or leave the tool offloadable on the non-claim path; test bounded output and unchanged state unless continuation is visible.","introduced_in_round":4,"location":"P2 / § 2.1 get_handoff_context reference path","prevention":"Test base-alone-over-budget responses for every offload-exempt delivery tool and verify both serialized size and one-shot state.","principle":"A reference target for oversized context must itself have a non-recursive bounded delivery path.","root_cause":"The plan makes get_handoff_context mandatory-exempt from result offloading, forbids its base-stub swap, and returns its oversized base unchanged when continuation cannot fit.","section_id":"2.1","severity":"blocking"}],"reviewer_session":"a7796b25-e1fa-47bf-bf1c-ae623303ab7e","round":5,"round_number":5,"verdict":"needs_review"},"session_id":"6bc09ed1-7072-423d-a89f-1c7881928c8b"}
```

**Round 6** `kind: verification`

- reviewer_run: 6026dd88-0f0f-471e-9bf3-76a31150cf1f
- reviewer_session: 8ea9a9ae-c321-423f-b5be-f8dd982c41e4
- verdict: needs_review
- findings:
- PR6-F01 / blocking / traceability: acceptance 1.1.16 creates `tests/sessions/test_transcript_read_error.py`, which is absent from §1.1 Targets (round-5 repair PR5-F01 omitted it).
- PR6-F02 / blocking / unhandled-edge: `tail_withheld` has no propagation path — §1.2 keeps `_read_undigested_turns` on its two-value return and confines digest.py changes to four symbols while §1.4 branches on `build_turn_and_digest(...)["tail_withheld"]`; `_resolve_undigested_pairs` and `_build_turn_and_digest_serialized` contracts are unspecified.
- PR6-F03 / blocking / gobby-format: §1.3 mutates module-level `_SUMMARY_METADATA_RECOMPUTE_ERRORS` but its `_summary_metadata.py` Target is the exact symbol `compact_summary_metadata_matches` (round-5 repair PR5-F04).
- PR6-F04 / blocking / unhandled-edge: §1.4's compact_self `tail_withheld` branch reuses the `digest_fallback` path, which persists the prior digest as `summary_markdown` and marks `handoff_ready`, so an immediate `wait_for_summary` delivers a handoff missing the compact-triggering turn.
- PR6-F05 / blocking / unhandled-edge: §2.1 collapses an empty render and a required-section fit failure into `None`; `attach_compact_continuation` treats any pending `None` as retryable, so a pending session with nothing renderable swaps its real summary for a stub on every call and leaves `pending_context_reset` armed indefinitely.
- votes: unattended coordinator judgment under the user's direction (review cap raised from 5 to 15 rounds, stop on convergence, minimal scope creep). PR6-F01 accept — verified: the file does not exist and §1.1 Targets omit it; typed `add_targets` repair applied through `apply_plan_review_repairs`. PR6-F02 accept — verified against `src/gobby/memory/digest.py`: `_read_undigested_turns` returns `tuple[list, int]`, `_resolve_undigested_pairs` returns a three-tuple or `None`, and `_build_turn_and_digest_serialized` builds the public dict; no path carries the flag, and the withheld-only-pair case (empty batch) would collapse to `None` and lose the flag entirely. PR6-F03 accept — verified: the tuple mutation is named in §1.3 prose while the Target is exact-symbol. PR6-F04 accept — verified against `_terminal_handoff.py`: `_persist_compact_handoff_fallback` prefers `_compact_handoff_digest_fallback_markdown` (prior digest only) and calls `update_status(..., "handoff_ready")`, so the planned route makes the stale revision continuation-visible. PR6-F05 accept — verified from the §2.1 snippets: `mutate` returns `(None, False)` for `""`, and the attach helper's retry condition cannot tell `""` from `None`; 2.1.8 as written asserted the non-progress state.
- resolution_notes: Checkpoint appended and finalized on the reviewed (unrepaired) artifact first. Repairs then applied with the least mechanism that fully resolves each finding: PR6-F01 typed repair adds the bare test Target to §1.1. PR6-F02: `_read_undigested_turns` returns `UndigestedBatch(pairs, next_pair_index, tail_withheld)` and `_resolve_undigested_pairs` returns `ResolvedPairs(pairs, input_hash, next_pair_index, tail_withheld) | None` where a withheld batch with nothing left to digest still returns a `ResolvedPairs` with empty `pairs` so `_build_turn_and_digest_serialized` can return `{"tail_withheld": True}` without persisting; the four production destructures and the test destructures are enumerated in the §1.2 scope reason; acceptance 1.2.9 proves the flag reaches both §1.4 callers through the public `build_turn_and_digest`. PR6-F03: §1.3 Target becomes `_summary_metadata.py::*` with a scope reason naming the matcher rewrite and the tuple update. PR6-F04: compact_self's refresh keeps the handoff unready while `tail_withheld` is true and retries the digest a bounded number of times inside the existing budget; on a persistent withhold it persists a transcript-tail fallback (which carries the triggering turn's raw records) with `tail_withheld: True` in the summary metadata, never the prior-digest-only fallback; the background refresh retries the digest and regenerates only once the pair is digested. Acceptance 1.4.10 rewritten and 1.4.14 added as the race case. PR6-F05: `claim_compact_continuation` treats `""` as a successful no-op delivery — the one-shots, including `pending_context_reset`, are cleared in the same mutation and no `continuation` key is returned — and reserves `None` (required section does not fit) for the base-stub retry, reverting the stub when the retry still cannot fit; acceptance 2.1.5, 2.1.8, and 2.1.10 rewritten and 2.1.17 added for repeated calls. No new deliverables or Targets beyond `_persist_compact_handoff_fallback` in §1.4.

```json plan-review-round
{"evidence_id":"0f54736b-7f2c-4c09-9719-e8a4d79523e3","plan_hash":"ee40b42a8e9304eaf902413cfb6aea6fced5432e2c0d7026399e6048a4dbd9cc","round_number":6,"round_result":{"coverage_attestation":{"adjacent_variant_complete":true,"attestation_digest":"8da3cffa69f9c160f2e7ed3d8689fe86e4b92e265b76094e24882f926ee9975f","cross_lane_interaction_complete":true,"disposition_counts":{"dismissed":0,"emitted_findings":5,"total":5},"evidence_id":"0f54736b-7f2c-4c09-9719-e8a4d79523e3","lanes":[{"candidate_count":1,"lane_id":"requirements_traceability","status":"completed"},{"candidate_count":2,"lane_id":"repository_blast_radius","status":"completed"},{"candidate_count":2,"lane_id":"runtime_invariants","status":"completed"}],"shadow_manifest_status":{"entry_count":8,"manifest_digest":"a72498af9b6013141d01fcf941cc601733ef73537b87969641e31411e5821b15","status":"valid"},"source_digest":"3dd6fe533b803524f3f6a1df822b069d42c17819f53bbe19349e20996beb8517","version":1},"findings":[{"category":"traceability","causal_finding_id":"PR5-F01","causal_section_ids":["1.1"],"check_key":"acceptance-artifact-target-coverage","description":"Acceptance 1.1.16 creates `tests/sessions/test_transcript_read_error.py`, which does not exist and is absent from §1.1 Targets, so the derived leaf does not own a required artifact.","finding_id":"PR6-F01","fix":"Add the new test file as a bare §1.1 Target.","introduced_in_round":5,"location":"P1 / § 1.1 Targets and acceptance 1.1.16","prevention":"Compare every acceptance `file:` and `test:` path against the owning section's Targets and repository existence.","principle":"Every new artifact required by acceptance must be owned by the deliverable's Targets inventory.","repairs":[{"entries":["`tests/sessions/test_transcript_read_error.py`"],"kind":"add_targets","section_id":"1.1"}],"root_cause":"The round-5 shared-exception repair added a new regression-test artifact without adding that new file to §1.1 Targets.","section_id":"1.1","severity":"blocking"},{"category":"unhandled-edge","check_key":"outcome-propagation-completeness","description":"`tail_withheld` has no implementable propagation path in the plan. Current code destructures `_read_undigested_turns` into two values, `_resolve_undigested_pairs` returns a triple, and `_build_turn_and_digest_serialized` constructs the public outcome; all must change despite §1.2 declaring changes confined elsewhere.","finding_id":"PR6-F02","fix":"Define typed return objects carrying `tail_withheld` from `_read_undigested_turns` through `_resolve_undigested_pairs` into `_build_turn_and_digest_serialized`, update every production/test destructure, name those symbols in §1.2's `digest.py::*` scope reason, and add acceptance that proves the flag reaches both §1.4 callers.","location":"P1 / §§ 1.2 and 1.4 tail-withheld outcome","prevention":"For every new outcome field, walk the producer-to-public-result chain and enumerate each return type, destructure, wrapper, and test migration.","principle":"A new caller-visible status must have an explicit propagation contract through every producer, wrapper, destructure, and result boundary.","root_cause":"Section 1.2 says `_read_undigested_turns` still returns its pair/index tuple and confines digest.py changes to four named symbols, yet §1.4 branches on `build_turn_and_digest(...)[\"tail_withheld\"]`; the intervening `_resolve_undigested_pairs` and `_build_turn_and_digest_serialized` contracts are unspecified.","section_id":"1.2","severity":"blocking"},{"category":"gobby-format","causal_finding_id":"PR5-F04","causal_section_ids":["1.3"],"check_key":"new-sibling-symbol-target-coverage","description":"Section 1.3 must change `_SUMMARY_METADATA_RECOMPUTE_ERRORS`, but its exact `_summary_metadata.py::compact_summary_metadata_matches` Target cannot authorize that module-level tuple mutation.","finding_id":"PR6-F03","fix":"Replace the exact `_summary_metadata.py` Target with `src/gobby/mcp_proxy/tools/sessions/_summary_metadata.py::*` and a scope reason naming both the matcher rewrite and the `_SUMMARY_METADATA_RECOMPUTE_ERRORS` update; do not mix the wildcard with the existing exact Target.","introduced_in_round":5,"location":"P1 / § 1.3 `_summary_metadata.py` Target","prevention":"After adding a sibling constant, helper, or type, compare every planned mutation in that file with the section's exact symbol Targets.","principle":"An exact symbol Target covers that symbol only; module-level siblings changed by the same deliverable require file-wide or separate exact coverage.","root_cause":"The round-5 source-hash repair added `TranscriptReadError` to the module-level `_SUMMARY_METADATA_RECOMPUTE_ERRORS` tuple while retaining an exact Target for only `compact_summary_metadata_matches`.","section_id":"1.3","severity":"blocking"},{"category":"unhandled-edge","check_key":"tail-withheld-handoff-readiness","description":"An immediate `wait_for_summary` can return a stale old-digest fallback while the compact-triggering pair is intentionally absent, directly violating the plan outcome that every compaction handoff includes that turn.","finding_id":"PR6-F04","fix":"Keep the handoff unready while `tail_withheld` is true and perform a bounded digest retry before continuation delivery, or define a fallback carrying the withheld pair's stable evidence plus an explicit coverage watermark. Add a race acceptance proving immediate wait cannot consume the prior-digest fallback and eventual delivery contains the triggering pair.","location":"P1 / § 1.4 compact_self tail-withheld branch","prevention":"For every withheld, retry, and fallback transition, assert the digest watermark represented by `handoff_ready` and race an immediate `wait_for_summary` against the background refresh.","principle":"A compact handoff cannot become continuation-visible until its coverage watermark includes the compact-triggering pair.","root_cause":"The proposed `tail_withheld` branch reuses the existing digest fallback, which prefers the prior digest, persists it as `summary_markdown`, and marks the session `handoff_ready` before the retry covers the withheld pair.","section_id":"1.4","severity":"blocking"},{"category":"unhandled-edge","check_key":"empty-continuation-terminal-state","description":"A pending session with no renderable continuation enters permanent non-progress: every `wait_for_summary` can replace the real summary with a reference stub, return no `continuation`, and leave `compact_handoff_inject_pending` plus `pending_context_reset` armed, so context-pressure guidance remains suppressed indefinitely.","finding_id":"PR6-F05","fix":"Separate empty state from size failure. Avoid arming when no section is renderable or atomically consume the empty continuation as a successful no-op that preserves the full summary and clears `pending_context_reset`; reserve base-stub retry for the explicit too-large outcome. Add repeated-call acceptance proving a terminal state and restored guidance.","location":"P2 / § 2.1 empty pending continuation","prevention":"Model one-shot claims as explicit empty, retryable-too-large, and delivered outcomes, then test repeated calls and every variable transition.","principle":"Every armed one-shot state needs a terminal transition distinct from retryable delivery failure.","root_cause":"`claim_compact_continuation` maps both an empty render and a required-section fit failure to `None`; `attach_compact_continuation` interprets any pending `None` as retryable, swaps the valid base summary for a stub, and retries without changing state.","section_id":"2.1","severity":"blocking"}],"reviewer_session":"8ea9a9ae-c321-423f-b5be-f8dd982c41e4","round":6,"round_number":6,"verdict":"needs_review"},"session_id":"6bc09ed1-7072-423d-a89f-1c7881928c8b"}
```

**Round 7** `kind: verification`

- reviewer_run: 31c570a3-9978-4e91-8cce-8974ccf55b41
- reviewer_session: bde174ef-958e-4c51-be4c-53d1d6127ac4
- verdict: needs_review
- findings:
- PR7-F01 / blocking / §1.3 `_read_first_user_goal` forward scan is unbounded per refresh (late or absent user text rescans the prefix, or the whole transcript, on every truncated-window refresh) while the section claims O(window) per-refresh I/O.
- PR7-F02 / blocking / §1.4 the `tail_withheld` fallback claims to carry the compact-triggering prompt and every complete record, but `_compact_handoff_transcript_tail_markdown` keeps only the last 80 raw lines and a 20,000-char suffix, so a tool-heavy turn loses its opening prompt.
- PR7-F03 / blocking / §2.1 when the intact required-skill directive cannot fit even beside the base stub, `wait_for_summary` returns `completed=true` with no `continuation` while every one-shot stays armed; Grok proceeds without the required skills and 2.1.17 labels that non-progress state terminal.
- PR7-F04 / blocking / §1.3 `_source_hash_payload` is fact-level (digest, session last-turn fields, template, derived `summary_context`), so acceptance 1.3.13's claim that appending any transcript record makes `compact_summary_metadata_matches` return `False` is unprovable for a fact-neutral record.
- PR7-F05 / blocking / §1.1 three direct `extract_last_messages` consumers (`summary_context._build_summary_prompt_context`, `summary_generation.generate_summary`, `test_audited_shapes_turn_and_usage_accounting`) appear in no Targets inventory.
- votes: unattended coordinator judgment under the user's standing direction ("continue for 10 more rounds or until convergence, unattended, use your best judgment, keep scope creep to a minimum"). PR7-F01 accept — verified: the plan text bounds the scan only by the position of the first user text; the no-user-text case is a full pass per refresh. Repair chosen over the proposed persisted-goal column (new Session field plus migration): cap the forward scan at `SUMMARY_ANALYZER_MAX_RECORDS` records, the same constant that bounds the tail, so per-refresh I/O is at most two windows; round 5 rejected a 200-record prefix as a realistic miss, a 20,000-record prefix has no realistic miss and is recorded as the explicit ceiling. PR7-F02 accept — verified against `_terminal_handoff.py` (80 lines, `_COMPACT_HANDOFF_FALLBACK_MAX_CHARS = 20_000` suffix) and `_read_transcript_tail_lines` (256 KiB cap): the 185-call motivating turn exceeds every bound. Repair: the digest pipeline already holds the withheld trailing pair (prompt + ledger) when it withholds, so 1.2's `UndigestedBatch`/`ResolvedPairs`/outcome chain carries it as `withheld_pair` and 1.4 renders it prompt-first with the newest ledger lines filling the cap — exact, zero extra I/O, no new reverse reader. PR7-F03 accept — verified: 2.1 names the post-stub fit failure as "surfaced by acceptance 2.1.14" only, with no model-visible path. Repair: on the post-stub retry the required section is rendered in a bounded reference form that names `get_variable(name="compact_resume_required_skills")`, and the claim keeps that variable populated while consuming the other one-shots; the 2.2 Grok directive already instructs following every instruction in `continuation`. Pagination rejected as more mechanism for the same guarantee. PR7-F04 accept with a narrowed repair — the acceptance claim is wrong, the hash design is right: the freshness identity is the set of inputs the summary is built from (digest, per-turn session fields, template, analyzer facts, git enrichment); a record that changes none of them cannot change the summary, and the proposed window fingerprint would make every compaction-time match fail after the compaction's own records land, forcing regeneration. 1.3.13 now appends a fact-changing record and asserts a fact-neutral record keeps the match; the identity is stated in §1.3 prose. PR7-F05 accept — all three symbols verified in the index; typed `add_targets` repair applied unchanged.
- resolution_notes: §1.1 Targets gain the three consumers (typed). §1.3: `_read_first_user_goal` gains `max_records` (`SUMMARY_ANALYZER_MAX_RECORDS`), the per-refresh bound is restated as two windows, acceptance 1.3.15 pins the scan ceiling with byte/record counting for absent and late user text; 1.3.13 rewritten to the fact-level identity. §1.2: `UndigestedBatch` and `ResolvedPairs` gain `withheld_pair: DigestPair | None`, the outcome dict carries `withheld_pair` beside `tail_withheld`, 1.2.9 extended. §1.4: `_compact_handoff_transcript_tail_markdown` joins Targets and gains keyword-only `withheld_pair`, rendering the prompt first and the newest ledger lines within the cap; 1.4.10 tests a turn above both the line and character caps; 1.4.15 added for the over-window case. §2.1: `render_compact_continuation_block` gains `allow_required_reference`, `claim_compact_continuation` takes a `ContinuationBlock(text, required_by_reference)` renderer result and keeps `compact_resume_required_skills` in reference mode; 2.1.14 and 2.1.17 rewritten, 2.1.18 added (end-to-end Grok recovery through `get_variable`).

```json plan-review-round
{"evidence_id":"08fd22e3-cde5-4b44-9db5-fe886838172c","plan_hash":"ca408e3bf10699a70d8f65b1cc7a86e429adaaa38b64c0cfe53dd999850dafc9","round_number":7,"round_result":{"coverage_attestation":{"adjacent_variant_complete":true,"attestation_digest":"f46285c2f4fa852f67e68e44f107be4cf6e7285450cde1a450bbdf72f610f71f","cross_lane_interaction_complete":true,"disposition_counts":{"dismissed":1,"emitted_findings":5,"total":6},"evidence_id":"08fd22e3-cde5-4b44-9db5-fe886838172c","lanes":[{"candidate_count":0,"lane_id":"requirements_traceability","status":"completed"},{"candidate_count":2,"lane_id":"repository_blast_radius","status":"completed"},{"candidate_count":4,"lane_id":"runtime_invariants","status":"completed"}],"shadow_manifest_status":{"entry_count":8,"manifest_digest":"123555bbe4a4bf3d1c60ddf63a099b37c2d78ce36fdc6aa43dd16cc4bccaffa8","status":"valid"},"source_digest":"73945ef293262360dcfa379fce3bc2c8b76814d3f8859d685fbbece3e6609188","version":1},"findings":[{"category":"unhandled-edge","causal_finding_id":"PR5-F09","causal_section_ids":["1.3"],"check_key":"edge-case-coverage","description":"`_read_first_user_goal` can rescan an arbitrarily long prefix, or the entire transcript when no normalized user text exists, on every truncated-window refresh. This violates the explicit O(window), never O(transcript), per-refresh contract.","finding_id":"PR7-F01","fix":"Persist the first provider-normalized user goal when first observed and read that bounded metadata on refresh. If migration requires a fallback scan, run it once, persist its result, and add byte-counting tests for absent user text and a user record beyond 20,000 records.","introduced_in_round":5,"location":"P1 / §1.3 bounded-window first-goal recovery","prevention":"Account for every file pass in the I/O budget and test late-user plus no-user transcripts across repeated refreshes.","principle":"A per-refresh transcript bound covers every read performed by that refresh, including provenance recovery.","root_cause":"The round-5 completeness repair replaced a fixed 200-record prefix with a forward scan to the first provider-normalized user text or EOF, while the section still claims all per-refresh I/O is O(window).","section_id":"1.3","severity":"blocking"},{"category":"unhandled-edge","causal_finding_id":"PR6-F04","causal_section_ids":["1.4"],"check_key":"edge-case-coverage","description":"The plan says the persistent-tail fallback carries the compact-triggering prompt and every complete record before publishing `handoff_ready`, but the unchanged helper can discard the opening prompt of a tool-heavy turn. The motivating 185-call turn already exceeds the 80-line shape.","finding_id":"PR7-F02","fix":"Target `_compact_handoff_transcript_tail_markdown` and replace its fixed-line suffix with a turn-aware bounded representation that always reserves the compact-triggering prompt, then fills remaining capacity with the newest complete records or rendered activity ledger. Test a withheld turn above both the line and character caps before racing `wait_for_summary`.","introduced_in_round":6,"location":"P1 / §1.4 persistent `tail_withheld` fallback","prevention":"Trace every fallback through its lower-level truncation helpers and test motivating-scale turns above each independent line and character cap.","principle":"A readiness transition may cite a triggering turn only when the persisted fallback is structurally guaranteed to retain that turn's required anchor.","root_cause":"The round-6 fallback repair changes `_persist_compact_handoff_fallback` while leaving `_compact_handoff_transcript_tail_markdown` at its existing last-80-lines and final-character-suffix bounds.","section_id":"1.4","severity":"blocking"},{"category":"unhandled-edge","causal_finding_id":"PR5-F07","causal_section_ids":["2.1"],"check_key":"edge-case-coverage","description":"When the intact required-skill directive cannot fit even beside the base stub, repeated calls return the real summary with no `continuation` while all one-shots remain armed. Grok treats the call as complete and proceeds without the required skills, so acceptance 2.1.17 labels a permanent non-progress state as terminal.","finding_id":"PR7-F03","fix":"Return a small model-visible retry or retrieval state for the post-stub fit failure, or paginate the intact required directive with an explicit remaining cursor. Add an end-to-end Grok case proving an oversized-after-stub directive is either fully delivered or explicitly recoverable before work continues.","introduced_in_round":5,"location":"P2 / §§2.1–2.2 required-skill post-stub fit failure","prevention":"For every non-consuming fit outcome, trace the final response through the provider directive and prove either delivery or an explicit retry/reference path.","principle":"A mandatory one-shot delivery that remains armed must return a model-visible recovery state instead of an ordinary completed response.","root_cause":"The all-or-nothing required-skill repair preserves state when the directive cannot fit, but `wait_for_summary` still returns `completed=true`; the Grok prompt retries only `completed=false`.","section_id":"2.1","severity":"blocking"},{"category":"unhandled-edge","causal_finding_id":"PR5-F04","causal_section_ids":["1.3"],"check_key":"edge-case-coverage","description":"Appending a record such as a successful non-commit result can change transcript completion state without changing derived analyzer facts, leaving `source_hash` unchanged. Acceptance 1.3.13 therefore cannot guarantee that every appended record makes the persisted summary stale.","finding_id":"PR7-F04","fix":"Add an ordered fingerprint of the retained transcript window plus its truncation flag to the canonical payload used by both generation and metadata matching. Target the owning hash helper and test an appended successful non-commit result whose analyzer facts otherwise remain unchanged.","introduced_in_round":5,"location":"P1 / §1.3 shared source-hash payload","prevention":"Enumerate source-hash inputs separately from prompt-retention inputs and test appended records that intentionally leave analyzer facts unchanged.","principle":"Freshness identity must include every ordered input transition whose arrival can change the truth represented by a persisted summary.","root_cause":"The shared builder reuses `_source_hash_payload`, whose current contract hashes digest/session fields, template, and derived `summary_context`, without a bounded transcript-window fingerprint.","section_id":"1.3","severity":"blocking"},{"category":"traceability","check_key":"consumer-coverage","description":"`summary_context._build_summary_prompt_context`, `summary_generation.generate_summary`, and `test_audited_shapes_turn_and_usage_accounting` call the changed `extract_last_messages` protocol but appear in no deliverable Targets inventory.","finding_id":"PR7-F05","fix":"Add the three exact consumer symbols to §1.1 Targets so expansion owns their compatibility verification; retain the existing byte-identical default-path acceptance.","location":"P1 / §1.1 Targets","prevention":"After resolving each exact protocol Target, run indexed usages plus a literal method-call sweep and place every owned production and test consumer in a deliverable.","principle":"Every owned caller of a changed exact symbol must appear in a manifest-backed Targets inventory, including compatibility-only consumers.","repairs":[{"entries":["`src/gobby/sessions/summary_context.py::_build_summary_prompt_context`","`src/gobby/sessions/summary_generation.py::generate_summary`","`tests/sessions/transcripts/test_grok_turn_accounting.py::test_audited_shapes_turn_and_usage_accounting`"],"kind":"add_targets","section_id":"1.1"}],"root_cause":"The Targets inventory covers parser implementations and primary parser tests but omits three direct `extract_last_messages` consumers found by the literal indexed sweep.","section_id":"1.1","severity":"blocking"}],"reviewer_session":"bde174ef-958e-4c51-be4c-53d1d6127ac4","round":7,"round_number":7,"verdict":"needs_review"},"session_id":"6bc09ed1-7072-423d-a89f-1c7881928c8b"}
```

**Round 8** `kind: verification`

- reviewer_run: f91d0d07-ac3a-4728-955d-68f8b2a7db54
- reviewer_session: 88ee1db9-3aaa-4c33-a7cb-532f4fa9235f
- verdict: needs_review
- findings:
- PR8-F01 / blocking / §2.1 reference-form directive renders `get_variable(name=…)` although both top-level `get_variable` tools require `session_id`; following it literally fails schema validation after the pending flag is already cleared (fixer-induced by PR7-F03)
- PR8-F02 / blocking / §2.1 `claim_compact_continuation` annotates `render` as returning `str` while its body consumes `ContinuationBlock | None`; cannot pass the src-only mypy gate (fixer-induced by PR7-F03)
- PR8-F03 / blocking / §1.4 relocated foreign-loop arm shows a bare `run_coroutine_threadsafe` submission and drops today's rejection path (`coro.close()`, warning, `done_event.set()`), leaking an unawaited coroutine and blocking waiters on a closing loop
- PR8-F04 / blocking / §1.1 adds `run_terminal_command` to the global `_SHELL_TOOLS` set for ledger normalization without tracing the other consumers (progress classification, plan-mode shell blocking, effects, approvals, analyzer, transcript metadata, commit observation)
- votes: unattended coordinator judgment under the user's standing direction ("continue for 10 more rounds or until you reach convergence, unattended, use your best judgment on the findings, keep scope creep to a minimum"). PR8-F01 accept — verified `GobbyDaemonTools.get_variable` (`server.py:560`) and the stdio `get_variable` (`stdio_tools.py:341`) both require `session_id`, and `_variables.get_variable` rejects an empty one; the rendered reference must carry the compacted session id, and the caller already holds it. PR8-F02 accept — the plan's own `mutate` body reads `block.required_by_reference` and `block.text`, so `Callable[[dict[str, Any]], str]` is wrong; `compact_handoff_block` imports `SessionVariableManager` at runtime, so the reverse annotation import must be type-only. PR8-F03 accept — verified today's `_dispatch_without_running_loop` (`session_summary_dispatcher.py:71-94`) closes the coroutine, logs, and sets `done_event` when submission raises; the plan said that branch "moves" but rendered it without the guard. PR8-F04 accept, local-alias option — `GrokAdapter.TOOL_MAP` already maps `run_terminal_command` to `Bash` on the hook path, and the raw name is emitted by `acp_client_requests` into pre-tool checks and stream events for every ACP client, so a global `_SHELL_TOOLS` entry would change plan-mode blocking, approvals, progress and effect classification outside this plan; the ledger-local alias is the least mechanism that solves the ledger problem and adds no cross-consumer scope.
- resolution_notes: §2.1 `render_compact_continuation_block` gains a required keyword `session_id` and the reference form renders `get_variable(session_id="<that id>", name="compact_resume_required_skills")`; both claim lambdas pass `session_id`; acceptance 2.1.18 invokes the exact rendered call. §2.1 `claim_compact_continuation` annotates `render` as `Callable[[dict[str, Any]], ContinuationBlock | None]` under a `TYPE_CHECKING` import. §1.4 second dispatch arm keeps the try/except (close the unsubmitted coroutine, log the rejection, set `done_event`); new acceptance 1.4.16 covers a raising submission. §1.1 drops the `_normalization_shell.py` Target; `tool_activity.py` normalizes `run_terminal_command` to `Bash` through a module-local alias ahead of `canonicalize_shell_tool_name`, with `_SHELL_TOOLS` deliberately unchanged and the reason recorded; acceptance 1.1.5 rewritten as a ledger test that also pins `is_shell_tool("run_terminal_command")` unchanged. Round 9 review launched (cap 15).

```json plan-review-round
{"evidence_id":"461d41fb-33fb-43dc-8358-98ee43ba9b5d","plan_hash":"ff1cbf71fee9ed47a67f4d22b6613fc903d2e1ccb37fb6e694b4f505c14ef369","round_number":8,"round_result":{"coverage_attestation":{"adjacent_variant_complete":true,"attestation_digest":"22a8364a9ae2d753b0f3ffc0b1986033204934a530020ed4d736641575be7b97","cross_lane_interaction_complete":true,"disposition_counts":{"dismissed":3,"emitted_findings":4,"total":7},"evidence_id":"461d41fb-33fb-43dc-8358-98ee43ba9b5d","lanes":[{"candidate_count":0,"lane_id":"requirements_traceability","status":"completed"},{"candidate_count":5,"lane_id":"repository_blast_radius","status":"completed"},{"candidate_count":2,"lane_id":"runtime_invariants","status":"completed"}],"shadow_manifest_status":{"entry_count":8,"manifest_digest":"4ccebed47646d1512890a0476573b29952d8a32e5d458c9fa4649f9d51ac1f8c","status":"valid"},"source_digest":"fc861258f4e755d77081bda4b1b37a91e80f1346f9479e1b1a69f39888c731ec","version":1},"findings":[{"category":"unhandled-edge","causal_finding_id":"PR7-F03","causal_section_ids":["2.1"],"check_key":"edge-case-coverage","description":"The continuation directs Grok to call `get_variable(name=\"compact_resume_required_skills\")`. Following that instruction literally fails schema validation; the reference-form claim has already cleared its pending flag, so the mandatory skill reload cannot be redelivered.","finding_id":"PR8-F01","fix":"Pass the compacted session reference into the renderer and emit `get_variable(session_id=\"<summary-session-id>\", name=\"compact_resume_required_skills\")`. Make acceptance 2.1.18 invoke that exact rendered call.","introduced_in_round":7,"location":"P2 / §2.1 required-skill reference fallback","prevention":"Resolve every generated tool invocation against its live schema and execute the exact rendered call in an end-to-end test.","principle":"Model-visible recovery instructions must supply every required argument of the invoked tool.","root_cause":"The round-7 reference-form repair renders get_variable with name alone, although both top-level implementations require session_id.","section_id":"2.1","severity":"blocking"},{"category":"gobby-format","causal_finding_id":"PR7-F03","causal_section_ids":["2.1"],"check_key":"edge-case-coverage","description":"Implementing the stated signature and body cannot pass the repository's src-only mypy gate.","finding_id":"PR8-F02","fix":"Annotate the callback as `Callable[[dict[str, Any]], ContinuationBlock | None]`, using a TYPE_CHECKING forward import or equivalent cycle-safe typing, while retaining `str | None` as the method return type.","introduced_in_round":7,"location":"P2 / §2.1 claim_compact_continuation signature","prevention":"Type-check every plan code block against its surrounding prose and all described return branches.","principle":"Self-contained implementation signatures must express every value branch their bodies consume.","root_cause":"The callback is annotated as returning str while the specified body accepts None and accesses ContinuationBlock.text and ContinuationBlock.required_by_reference.","section_id":"2.1","severity":"blocking"},{"category":"unhandled-edge","check_key":"scheduler-submission-failure","description":"If `run_coroutine_threadsafe` raises, the planned branch leaks an unawaited coroutine and can leave shutdown or handoff waiters blocked indefinitely.","finding_id":"PR8-F03","fix":"Preserve the existing try/except around the relocated submission, close the unsubmitted coroutine, log the rejection, set done_event, and add a dispatcher regression where submission raises.","location":"P1 / §1.4 foreign-loop dispatch branch","prevention":"Enumerate successful submission, rejected submission, coroutine failure, and waiter completion whenever scheduling logic moves between helpers.","principle":"Moving an asynchronous scheduling boundary must preserve rejection cleanup and waiter completion.","root_cause":"The planned relocation shows a bare run_coroutine_threadsafe submission and omits the current exception path that closes the coroutine and sets done_event.","section_id":"1.4","severity":"blocking"},{"category":"traceability","check_key":"shared-normalizer-blast-radius","description":"Adding `run_terminal_command` to `_SHELL_TOOLS` changes progress classification, plan-mode shell blocking, workflow effect matching, analyzer handling, transcript metadata, approvals, and commit observation. The plan covers only ledger behavior.","finding_id":"PR8-F04","fix":"Either state the global alias expansion as an explicit requirement and add focused acceptance coverage for the affected classifiers, or normalize `run_terminal_command` locally in `tool_activity` when those global behavior changes are outside scope.","location":"P1 / §1.1 shared _SHELL_TOOLS update","prevention":"Run caller and semantic-consumer sweeps for shared registries and classify each changed outcome as required, preserved, or intentionally excluded.","principle":"A shared identity-table change must trace and validate every behaviorally affected consumer.","root_cause":"The plan uses the global shell-alias set for ledger normalization without specifying the resulting behavior across its other consumers.","section_id":"1.1","severity":"blocking"}],"reviewer_session":"88ee1db9-3aaa-4c33-a7cb-532f4fa9235f","round":8,"round_number":8,"verdict":"needs_review"},"session_id":"6bc09ed1-7072-423d-a89f-1c7881928c8b"}
```

**Round 9** `kind: verification`

- reviewer_run: 6d22bafc-aaf1-42ec-95e9-e48dcc7958e3
- reviewer_session: bfc46123-4e46-44ed-bfa4-4ed7967b259a
- verdict: needs_review
- findings:
- PR9-F01 / blocking / §1.1's illustrative ledger and acceptance 1.1.3 still render the native `run_terminal_command` name after round 8 made the ledger-local alias emit `Bash` (fixer-induced by PR8-F04).
- PR9-F02 / blocking / §2.3 and 2.3.3 tell the docs every successful claim clears all four one-shots, and 2.1.2 is unqualified, while a reference-form delivery preserves `compact_resume_required_skills` (fixer-induced by PR7-F03).
- PR9-F03 / blocking / acceptance 2.1.1 says the renderer returns the bare string `""` although its declared return is `ContinuationBlock | None` (fixer-induced by PR7-F03).
- PR9-F04 / blocking / a wrapper-only Codex split tail is only an `exec` `tool_use` with raw JS arguments until its output lands, so the promised canonical `tail -f … (no result recorded)` entry and the command-identity match are not derivable from the pre-scan as specified.
- PR9-F05 / blocking / `build_turn_and_digest` returns `None` for disabled memory/digest config and a missing session as well as for nothing-undigested, and §1.4 treats every `None` as safe to summarize.
- PR9-F06 / blocking / a `get_handoff_context` base above `inline_context_budget_for(source)` but at or below `threshold_chars` is neither given a continuation nor offloaded, contradicting §2.1's "always bounded" wording.
- PR9-F07 / blocking / the Grok `wait_for_summary` directive retries on every `completed=false`, which `wait_for_summary` also returns for terminal `found=false`/`success=false` failures, with no retry ceiling.
- PR9-F08 / blocking / `TranscriptReadError` requires a global 1-based `line_number` that the O(window) reverse tail reader cannot compute without scanning the omitted prefix.
- PR9-F09 / blocking / the foreground `_refresh_compact_handoff_context` is awaited by `compact_self` with no timeout wrapper, so the plan's "inside the existing timeout budget" claim is false for the new digest LLM work and tail retries on that path.
- votes: unattended coordinator judgment under the user's standing direction ("continue for 10 more rounds or until you reach convergence, unattended, use your best judgment on the findings, keep scope creep to a minimum"). PR9-F01 accept — verified against the current §1.1: `canonical_tool_name` emits `Bash` for the alias, so the example and 1.1.3 contradicted it; example lines and 1.1.3 now name the canonical `Bash` entry derived from the native record, 1.1.5 untouched. PR9-F02 accept — the `mutate` body in §2.1 preserves `compact_resume_required_skills` on `required_by_reference`; 2.1.2 is qualified to direct delivery and §2.3/2.3.3 now distinguish direct-or-empty (four cleared) from reference (three cleared, list preserved). PR9-F03 accept — 2.1.1 rewritten to `ContinuationBlock(text="", required_by_reference=False)`, with `""` reserved for `claim_compact_continuation`. PR9-F04 accept — verified in `codex.py`: `_parse_tool_call` yields only `exec` with `{"raw": <js>}` for a pending `custom_tool_call`, and `CodexNestedExecOutcome` is derived solely by `_resolve_nested_exec_output` from the output record; repaired with the least mechanism: a `pending_exec_command` helper in `tool_activity.py` that reuses the existing fail-closed `extract_functions_exec_command`, applied by both the ledger and the 1.3 adapter to a wrapper whose output never lands in the window, matched against items on command alone; 1.1.15, 1.3 prose, and the 1.5 fixture text now state the exact call-without-output envelope. PR9-F05 decline — a disabled memory or digest feature is an operator opt-out of digest-backed coverage and a missing session fails the summary path on its own; those `None`s take today's refresh path exactly as before this plan, `_digest_markdown_for_summary` already appends the latest `last_turn_markdown`/`last_assistant_content` when the digest lags, and the proposed typed result would change the `build_turn_and_digest` contract that §1.2 holds unchanged plus every caller — disproportionate to a configuration edge; the scope boundary is now pinned in one §1.4 sentence so the claim is explicit rather than implied. PR9-F06 accept with a narrower fix — the gap exists, but a response at or below `threshold_chars` is exactly what the proxy serves inline for every tool today, and the inline budget governs only what this plan adds; the prose now states the bound precisely (`threshold_chars`, non-recursive because the reference form points at `get_variable`) and 2.1.16 gains the between-budgets regression with a live override; the proposed source-aware forced-offload path is declined as new offload mechanism outside the plan's problem. PR9-F07 accept with a narrower fix — verified in `_handoff.py`: `found=false` returns carry `completed=false`; only the Grok directive (text this plan authors) gains the response-union policy: retry only `success=true`/`completed=false`, cap at three further calls, stop with one fallback action on `success=false`/`found=false`; the other-source text stays verbatim as §2.2 already requires; 2.2.1 asserts the new text. PR9-F08 accept — `_read_transcript_window` reads only the tail; `TranscriptReadError` now carries `byte_offset: int` (both readers) and `line_number: int | None` (`None` from the reverse reader); §1.2 pins that the forward per-line loop reads in binary so it supplies both; 1.1.16 pins each reader's coordinates. PR9-F09 accept with a variant — verified in `_terminal.py:582` that `compact_self` awaits `_refresh_compact_handoff_context` with no `wait_for`; the foreground pre-digest step (first attempt plus tail retries) now runs under one `asyncio.wait_for` deadline from `_compact_handoff_refresh_timeout_seconds(compact_handoff_config)` and a timeout takes the existing `digest_fallback` path with a timeout reason (the outcome §1.4 already defines for digest failure) instead of the proposed retryable `compact_self` error, because a hung provider must not hold compaction open across retries; new acceptance 1.4.17 pins the deadline and the fallback.
- resolution_notes: §1.1 example ledger renders `Bash` lines, 1.1.3 asserts a failed canonical `Bash` entry from a native `run_terminal_command` record; §1.1 Codex bullet gains the pending-wrapper projection (`pending_exec_command` over `extract_functions_exec_command`, command-only matching for pending wrappers), §1.3 adapter mirrors it, 1.1.15/1.5.1/1.5.7 and the §1.5 fixture prose name the `custom_tool_call`-without-output envelope; `TranscriptReadError` carries `path`, `byte_offset`, `line_number: int | None` with reader-specific coordinates in §1.1, §1.2, §1.3 and 1.1.16; §1.4 pins the disabled-feature/missing-session `None` boundary as today's path, bounds the foreground pre-digest step with the compact-handoff timeout and adds 1.4.17; §2.1 states the `threshold_chars` bound for non-claim `get_handoff_context` responses and 2.1.16 gains the between-budgets case; 2.1.1 and 2.1.2 corrected; §2.2 Grok directive rewritten with the bounded, outcome-specific retry policy and 2.2.1 extended; §2.3 prose and 2.3.3 distinguish direct-or-empty from reference-form clearing. PR9-F05's typed pre-digest result and PR9-F06's forced-offload path are declined as scope beyond the plan's problem.

```json plan-review-round
{"evidence_id":"85c225d5-5434-41ba-9e34-4e73f4ce0dd2","plan_hash":"ce17a2cf61509fd6c1d0c2b08d454125e3b14bc6e36b6b0d3cfcccc31586064d","round_number":9,"round_result":{"coverage_attestation":{"adjacent_variant_complete":true,"attestation_digest":"2e01b7bd5773a03b98dd4e42d69cec07e9839c39f1b1f25f1c5fec66b8408ad7","cross_lane_interaction_complete":true,"disposition_counts":{"dismissed":3,"emitted_findings":9,"total":12},"evidence_id":"85c225d5-5434-41ba-9e34-4e73f4ce0dd2","lanes":[{"candidate_count":3,"lane_id":"requirements_traceability","status":"completed"},{"candidate_count":2,"lane_id":"repository_blast_radius","status":"completed"},{"candidate_count":7,"lane_id":"runtime_invariants","status":"completed"}],"shadow_manifest_status":{"entry_count":8,"manifest_digest":"25c9f258a98488ca34ee9057d835b8b7991455db513bf05db25317d9b1190c75","status":"valid"},"source_digest":"49e47cf9c0cb6c76e03b94ebd152d8762d65039486f50344531509c3fa39f7f7","version":1},"findings":[{"category":"traceability","causal_finding_id":"PR8-F04","causal_section_ids":["1.1"],"check_key":"acceptance-observability","description":"Section 1.1 now requires canonical_tool_name(\"run_terminal_command\", ...) to return Bash, while its example still renders run_terminal_command and acceptance 1.1.3 requires a ledger naming failed run_terminal_command results. An implementation cannot satisfy both contracts.","finding_id":"PR9-F01","fix":"Render the illustrative Grok shell lines as Bash and rewrite acceptance 1.1.3 to assert a failed canonical Bash ledger entry originating from a native run_terminal_command record; keep acceptance 1.1.5's global-registry non-change assertion.","introduced_in_round":8,"location":"P1 / §1.1 canonical ledger identity and acceptance 1.1.3","prevention":"After changing an identity normalizer, sweep every rendered example and acceptance assertion for native-name versus canonical-name drift.","principle":"Implementation prose, rendered examples, and acceptance criteria must assert the same canonical output.","root_cause":"The round-8 repair moved run_terminal_command normalization into a ledger-local alias that emits Bash, but the illustrative ledger and Grok acceptance retained the native source name.","section_id":"1.1","severity":"blocking"},{"category":"traceability","causal_finding_id":"PR7-F03","causal_section_ids":["2.1"],"check_key":"edge-case-coverage","description":"Reference-form delivery is successful but clears only compact_handoff_inject_pending, compact_resume_advisory_skills, and pending_context_reset; it preserves compact_resume_required_skills. Section 2.3 and acceptance 2.3.3 still require the docs to say every successful claim clears all four, and acceptance 2.1.2 is unqualified.","finding_id":"PR9-F02","fix":"Qualify acceptance 2.1.2 as direct non-reference delivery. Rewrite §2.3 and acceptance 2.3.3 so the variables and session-boundary docs distinguish direct or empty delivery, which clears all four, from reference delivery, which clears three and preserves compact_resume_required_skills for get_variable.","introduced_in_round":7,"location":"P2 / §§2.1 and 2.3 reference-form variable clearing","prevention":"For every one-shot delivery branch, enumerate the exact values consumed, preserved, and retried, then mirror that table in documentation acceptance.","principle":"Documentation and acceptance must describe branch-specific one-shot state transitions exactly.","root_cause":"The round-7 reference delivery deliberately preserved compact_resume_required_skills as its retrieval target, while the documentation deliverable kept the pre-reference blanket claim that every successful claim clears all four variables.","section_id":"2.3","severity":"blocking"},{"category":"traceability","causal_finding_id":"PR7-F03","causal_section_ids":["2.1"],"check_key":"acceptance-observability","description":"render_compact_continuation_block returns ContinuationBlock | None, and an empty render is ContinuationBlock(text=\"\", required_by_reference=False). Acceptance 2.1.1 instead says the renderer returns the bare string \"\", which conflicts with the callback and mutation contract.","finding_id":"PR9-F03","fix":"Rewrite acceptance 2.1.1 and its named test to require ContinuationBlock(text=\"\", required_by_reference=False). Reserve the bare \"\" return for claim_compact_continuation, which unwraps block.text.","introduced_in_round":7,"location":"P2 / §2.1 renderer return contract and acceptance 2.1.1","prevention":"Type-check every acceptance statement against the section's declared signature and each code-block return path.","principle":"Acceptance criteria must assert the declared API value and type, including empty outcomes.","root_cause":"The round-7 ContinuationBlock return type replaced the former string-shaped renderer contract, but acceptance 2.1.1 still describes the empty result as a bare string.","section_id":"2.1","severity":"blocking"},{"category":"unhandled-edge","check_key":"edge-case-coverage","description":"The mixed-window fixture requires a wrapper-only tail -f call to survive as a canonical command with (no result recorded). Before custom_tool_call_output or item_completed lands, current Codex parsing exposes only exec with raw orchestration, so neither the ledger nor analyzer can derive that inner command or perform the described one-to-one command match.","finding_id":"PR9-F04","fix":"Specify a shared unresolved-exec projection for the ledger and analyzer that applies the existing extract_functions_exec_command helper to a pending exec wrapper's original arguments, emits a source-positioned unresolved shell entry under the wrapper call id, and participates in the same per-turn one-to-one item suppression. Add the exact custom_tool_call-without-output mixed-window regression.","location":"P1 / §§1.1, 1.3, and 1.5 Codex wrapper-only split tail","prevention":"For every provider's in-flight case, construct the exact call-without-result envelope and prove each asserted ledger field is observable before specifying acceptance.","principle":"A promised unresolved-call entry must be derivable from evidence available before its result record lands.","root_cause":"A pending Codex custom_tool_call named exec exposes raw JavaScript orchestration; the parser derives the inner normalized command only after the outer output, while the planned pre-scan consumes completed items only.","section_id":"1.1","severity":"blocking"},{"category":"unhandled-edge","check_key":"edge-case-coverage","description":"With an older digest and a new compact-triggering turn, disabling digest configuration makes build_turn_and_digest return None. Section 1.4 then proceeds, while §1.3 deliberately keeps transcript_summary and last_messages on the older digest branch, so final-turn narrative coverage is not guaranteed.","finding_id":"PR9-F05","fix":"Make the pre-digest result typed so up_to_date and persisted are distinct from skipped_disabled, missing_prerequisite, and missing_session. Permit digest-backed summary generation only for up_to_date or persisted; for skipped states require a transcript-grounded ending-pair source or abort without publishing readiness. Add the older-digest plus disabled-config regression.","location":"P1 / §§1.3–1.4 pre-digest None outcome","prevention":"Enumerate every return cause at an async boundary and test each against stale persisted state before assigning a shared sentinel.","principle":"A caller may treat an outcome as up to date only when the callee distinguishes that state from disabled or missing prerequisites.","root_cause":"build_turn_and_digest overloads None for no undigested work, disabled memory or digest configuration, absent prerequisites, and missing session; §1.4 collapses them into the safe-to-summarize branch.","section_id":"1.4","severity":"blocking"},{"category":"unhandled-edge","causal_finding_id":"PR7-F03","causal_section_ids":["2.1"],"check_key":"edge-case-coverage","description":"A get_handoff_context base response can exceed the provider inline budget while remaining at or below the result-offload threshold. In that interval continuation does not fit, the one-shots stay armed, and the unchanged base is not offloaded, contradicting §2.1's bounded non-recursive reference-path guarantee.","finding_id":"PR9-F06","fix":"Add a source-aware forced-offload path for wait_for_summary and get_handoff_context whenever the completed non-claim response exceeds inline_context_budget_for(source), even below the global threshold, while leaving one-shots untouched. Add a base-size-between-budgets regression and configuration-override cases.","introduced_in_round":7,"location":"P2 / §2.1 get_handoff_context no-stub size gap","prevention":"Compare every producer budget with downstream transport thresholds using below, equal, between, and above boundary cases plus live overrides.","principle":"A total response bound must cover every interval between independently configured size thresholds.","root_cause":"The reference-form repair measures continuation delivery against inline_context_budget_for(source), while ordinary offload begins only above a separate threshold_chars value and no cross-config invariant closes the gap.","section_id":"2.1","severity":"blocking"},{"category":"unhandled-edge","check_key":"edge-case-coverage","description":"Grok is told to repeat wait_for_summary whenever completed=false. Permanent lookup failures return success=false, found=false, completed=false just like the timeout branch's completed flag, and repeated timeouts have no attempt or elapsed-time ceiling, so the continuation can loop indefinitely.","finding_id":"PR9-F07","fix":"Rewrite wait_for_summary_directive to retry only success=true, completed=false timeout responses, stop and surface success=false, found=false, or error responses immediately, and cap retries by a stated attempt or elapsed-time bound with one explicit fallback action. Add invalid-reference, missing-session, repeated-timeout, and eventual-success tests.","location":"P2 / §2.2 Grok wait_for_summary directive","prevention":"For every model-visible retry instruction, map the full response union to retry, stop, and fallback actions and test a bounded exhaustion path.","principle":"Retry instructions must distinguish retryable outcomes from terminal failures and carry a finite stop policy.","root_cause":"The directive branches only on completed=false, while wait_for_summary uses that value for timeout, invalid resolution, and a session disappearing after resolution.","section_id":"2.2","severity":"blocking"},{"category":"unhandled-edge","check_key":"edge-case-coverage","description":"For malformed interior JSON inside a truncated reverse window, _read_transcript_window can know the window-relative record and byte offset but cannot know the global one-based line without scanning the omitted prefix. The plan simultaneously requires that global line_number and O(window) bytes independent of transcript length.","finding_id":"PR9-F08","fix":"Change the shared corruption contract to a globally computable byte_offset plus an optional or explicitly window-relative line_number for reverse-window reads, while the forward digest reader may still supply a global line. Update both reader tests and the shared-exception acceptance to pin those semantics.","location":"P1 / §§1.1 and 1.3 TranscriptReadError coordinates","prevention":"For every bounded random-access reader, prove each promised diagnostic field can be derived from the bytes read at the maximum truncation boundary.","principle":"A diagnostic coordinate must be computable within the reader's stated I/O bound.","root_cause":"The shared exception requires a global one-based physical line number, while the reverse tail reader deliberately omits the file prefix containing the newline count.","section_id":"1.3","severity":"blocking"},{"category":"unhandled-edge","check_key":"edge-case-coverage","description":"Section 1.4 says both refresh functions run pre-digest work inside the existing timeout budget, but the foreground _refresh_compact_handoff_context call has no timeout wrapper. A hung digest provider can block compact_self before the terminal compaction command is sent, and the plan defines no safe timeout outcome.","finding_id":"PR9-F09","fix":"Pass the compact-handoff timeout into the foreground path and wrap the entire pre-digest plus refresh operation in one deadline. On timeout, leave handoff unready, preserve the prior revision, return a retryable compact_self error without sending the compact command, and add never-returning-digest and timeout-during-tail-retry tests.","location":"P1 / §1.4 foreground compact refresh timeout","prevention":"Trace timeout ownership at every foreground and background call site and test a never-returning dependency before claiming work fits an existing budget.","principle":"Every newly added provider call and retry loop on a foreground tool path needs an explicit enclosing deadline and safe timeout transition.","root_cause":"The existing compact-handoff timeout helper wraps only the background refresh; compact_self directly awaits the foreground refresh that the plan expands with digest LLM work and up to four tail attempts.","section_id":"1.4","severity":"blocking"}],"reviewer_session":"bfc46123-4e46-44ed-bfa4-4ed7967b259a","round":9,"round_number":9,"verdict":"needs_review"},"session_id":"6bc09ed1-7072-423d-a89f-1c7881928c8b"}
```

**Round 10** `kind: verification`

- reviewer_run: ccc593d7-5697-41fc-80dd-ced6b6aa81b5
- reviewer_session: 3e4d4196-0cda-463d-a075-6bf3515b91ba
- verdict: needs_review
- findings:
- PR10-F01 / blocking / the round-9 `asyncio.wait_for` deadline can fire while `persist_digest_state` runs in its worker thread; the cancelled coroutine releases the per-session lock while the write still lands, so "a cancelled digest persists nothing" is false (fixer-induced by PR9-F09).
- PR10-F02 / blocking / a JSONL line that decodes to a scalar or list is skipped by the summary reader but fed to the digest parser, so the shared `TranscriptReadError` contract does not cover it and §1.4 can proceed past a record the summary silently omitted (fixer-induced by PR4-F06).
- PR10-F03 / blocking / the continuation fit predicate measures compact `ensure_ascii=False` JSON while the proxy's threshold serializer is indented and ASCII-escaped; near-budget non-ASCII content is underestimated and the delivered response is offload-exempt (fixer-induced by PR5-F08).
- PR10-F04 / blocking / a Codex `item_completed` `FileChange` entry is created with `resolved=False` and no result record ever arrives, so a completed edit renders `(no result recorded)` (fixer-induced by PR3-F05).
- PR10-F05 / blocking / `attach_compact_continuation` runs a synchronous `transaction_immediate` claim directly on the daemon event loop from async `wait_for_summary` (fixer-induced by PR1-F02).
- PR10-F06 / blocking / a prefix digest that withholds the trailing pair still reaches the unconditional `_schedule_summary_refresh_if_stale`, spawning a stale background summary that races §1.4's tail fallback (fixer-induced by PR6-F02).
- PR10-F07 / blocking / `_build_summary_prompt_context`'s no-digest branch re-parses native records with the provider parser, which the Claude-shaped adapted turn list breaks (fixer-induced by PR5-F04).
- PR10-F08 / blocking / the renderer collapses present-but-unfittable optional sections to the same `""` that `claim_compact_continuation` consumes as a successful no-op, clearing one-shots without delivering anything (fixer-induced by PR6-F05).
- PR10-F09 / blocking / the renderer prefix-cuts `task_context`/`user_profile_content` and the withheld-pair fallback prefix-cuts the prompt, which `docs/contracts/truncation.md` forbids for complete payloads.
- PR10-F10 / blocking / module-scope edits (an import move in `hook_manager.py`, `TYPE_CHECKING` imports in `state_manager.py`, import replacement in `compact_continuation.py`, a new constant in `_terminal_handoff.py`) are not owned by the exact-symbol Targets of §§1.4, 2.1, and 2.2.
- PR10-F11 / blocking / exact Targets across §§1.1, 1.3, 1.4, and 2.1 leave owned production and test consumers out of every deliverable's Targets, which standard validation warns about and expansion validation rejects.
- PR10-F12 / blocking / when a first attempt returns `tail_withheld` with a `withheld_pair` and a later retry hangs, the generic timeout fallback discards the captured prompt and ledger (fixer-induced by PR9-F09).
- votes: unattended coordinator judgment under the user's standing direction ("continue for 10 more rounds or until you reach convergence, unattended, use your best judgment on the findings, keep scope creep to a minimum"). PR10-F01 accept with a narrower variant — verified in `digest.py`: `persist_digest_state` runs through `asyncio.to_thread` under the per-session lock, so a cancellation mid-write releases the lock before the thread finishes; the repair is a five-line cancellation barrier around that single await (`asyncio.shield` plus `await asyncio.wait({persist})` on `CancelledError`, holding the lock until the worker finishes) in `_build_turn_and_digest_serialized` (already a §1.2 target), and §1.4's timeout branch reloads the session before choosing fallback; the proposed split deadline and concurrent-digest reconciliation machinery is declined as more mechanism than the race needs. PR10-F02 accept — verified: `_read_undigested_turns` appends any decoded value to the parser input while `_read_transcript_window` skips non-dict values with a warning; the shared contract now classifies a decoded non-object value at any position, the final line included, as `TranscriptReadError` in both readers, with 1.1.16, 1.2.7, and 1.3.12 extended. PR10-F03 accept in part — verified that `_serialize_success_result` is `indent=2` ASCII-escaped, but the MCP SDK sends results through pydantic `model_dump_json` (compact, non-ASCII preserved), so the plan's compact `ensure_ascii=False` measure is the transport representation and the indented threshold measure is never smaller; the prose is corrected to say exactly that and 2.1.15 gains the non-ASCII near-budget case; adopting the offloader's indented serializer as the fit measure is declined because it would overcharge non-ASCII content by up to twelve times. PR10-F04 accept — `FileChange` ledger entries are `resolved=True` (failed on `status == "failed"`), the §1.3 adapter note explains why no `tool_result` is needed, and 1.5.6 asserts the edit line renders bare. PR10-F05 accept — `wait_for_summary` already keeps `session_manager.get` off the loop with `asyncio.to_thread`; the claim follows the same pattern and `get_handoff_context` (a synchronous tool) calls the helper directly; new acceptance 2.1.19. PR10-F06 accept — verified the unconditional `_schedule_summary_refresh_if_stale` call after persistence; it is skipped for a `tail_withheld` outcome; new acceptance 1.2.11. PR10-F07 accept — verified in `summary_context.py`: the no-digest branch selects the native parser by source; the builder passes raw `window.turns` to `_build_summary_prompt_context` and the adapted list only to the analyzer; new acceptance 1.3.16. PR10-F08 and PR10-F09 accept together under one rule — verified against `docs/contracts/truncation.md`: sections 2–5 are delivered whole or replaced by a pointer line (`get_variable` for `task_context`, `mcp_calls`, `user_profile_content`, which are not one-shots) or a whole-item omission count (advisory skills); `ContinuationBlock(text="")` is returned only when the variables hold no section; present content that cannot fit even as pointer lines returns `None` so the one-shots stay armed; the withheld-pair prompt is rendered whole and uncapped because the delivery tools already bound oversized summaries by reference; 2.1.1/2.1.8/2.1.12/2.1.15, the `claim_compact_continuation` docstring, 1.4.15, and new 2.1.20 follow. PR10-F10 accept — the four files gain `::*` Targets naming the module-scope edits. PR10-F11 accept — the consumer-coverage warnings are errors under `--mode expansion`, so approval would fail on them regardless; exact Targets whose consumer graphs are wide plumbing become file-wide scope (`registries.py`, `_factory.py`, `_terminal.py`, `summary_context.py`, `summary_generation.py`), and the remaining owned consumers (`cli/sessions.py`, `_terminal_webchat.py`, `test_handoff_coverage.py`, `test_clear_continuation.py`) are listed with scope reasons stating they are unchanged call sites. PR10-F12 accept — the latest withheld outcome is retained outside the timed coroutine and a later timeout builds the fallback from it with `tail_withheld=True`; 1.4.17 extended.
- resolution_notes: §1.1: `TranscriptReadError` covers decoded non-object values at any position in both readers (1.1.16); `FileChange` ledger entries are resolved successes or failed on `status == "failed"` (1.5.6); `summary_context.py`/`summary_generation.py` Targets become consumer-closure `::*` entries. §1.2: persistence is a cancellation barrier (shield plus wait under the lock) and a `tail_withheld` outcome never schedules the background summary refresh; 1.2.7 extended, 1.2.10 and 1.2.11 added. §1.3: the builder passes raw `window.turns` to `_build_summary_prompt_context` and adapted turns only to the analyzer (1.3.16); the reverse reader raises on decoded non-object values (1.3.12); `cli/sessions.py` listed as an unchanged consumer; the adapter's `FileChange` block note added. §1.4: Targets for `hook_manager.py`, `registries.py`, `_factory.py`, `_terminal.py`, and `_terminal_handoff.py` are file-wide with scope reasons naming the import move, plumbing, and the new retry constant; the timeout branch reloads the session and builds the fallback from the last withheld outcome when one was captured; the withheld-pair prompt is rendered whole and uncapped; 1.4.15 and 1.4.17 rewritten. §2.1: `state_manager.py::*`, test consumers listed; the renderer docstring states the wire-form measure, the whole-or-pointer rule for sections 2–5, `""` only for no sections and `None` for unfittable present content; `wait_for_summary` awaits the claim through `asyncio.to_thread`; 2.1.1, 2.1.8, 2.1.12, 2.1.15 updated and 2.1.19, 2.1.20 added. §2.2: `compact_continuation.py::*`, `_terminal.py::*`, and `_terminal_webchat.py::*` listed. PR10-F01's split-deadline machinery and PR10-F03's shared indented serializer are declined as mechanism beyond the problem.

```json plan-review-round
{"evidence_id":"a783a71d-2987-4734-a14b-e3f65452e45e","plan_hash":"f52f5637d0377f2b2e45a249a7c7f580fa1cfabd931161fa04269cf18bf2ee8b","round_number":10,"round_result":{"coverage_attestation":{"adjacent_variant_complete":true,"attestation_digest":"27d4d73323e25a7e52191dab7924b72a410bff1af0690ae3ef387b58c9f77c5d","cross_lane_interaction_complete":true,"disposition_counts":{"dismissed":0,"emitted_findings":12,"total":12},"evidence_id":"a783a71d-2987-4734-a14b-e3f65452e45e","lanes":[{"candidate_count":0,"lane_id":"requirements_traceability","status":"completed"},{"candidate_count":4,"lane_id":"repository_blast_radius","status":"completed"},{"candidate_count":8,"lane_id":"runtime_invariants","status":"completed"}],"shadow_manifest_status":{"entry_count":8,"manifest_digest":"f1f9baaccf71668b713c2c0f6a87e55b84eb5e66172d25f5063517b34c1de05e","status":"valid"},"source_digest":"67ba1b9ac9978e1108914b81b4c4dbf3b7106f5c1cd841afad7df4e32798f7f7","version":1},"findings":[{"category":"unhandled-edge","causal_finding_id":"PR9-F09","causal_section_ids":["1.4"],"check_key":"digest-timeout-commit-barrier","description":"A timeout during persist_digest_state cancels the awaiting coroutine and releases the per-session asyncio lock, while the synchronous worker can continue and write later. The timeout fallback or a following digest may then race that late write, contradicting the claim that a cancelled digest persists nothing.","finding_id":"PR10-F01","fix":"Split deadline-controlled preparation from a cancellation-safe commit barrier, or shield an entered persistence phase so it retains the lock until the worker completes. Reconcile the actual digest outcome before choosing fallback, and add a timeout-during-persistence plus concurrent-digest regression.","introduced_in_round":9,"location":"§ 1.4 foreground compact_self pre-digest timeout","prevention":"Inject cancellation at LLM work, immediately before persistence, and during persist_digest_state; race each point with a second digest and assert cursor and digest state remain single-writer.","principle":"An async cancellation boundary must retain serialization ownership until any already-started synchronous persistence finishes or is reconciled.","root_cause":"The round-9 deadline wraps the monolithic digest coroutine, while digest/session persistence runs through asyncio.to_thread inside an asyncio.Lock.","section_id":"1.4","severity":"blocking"},{"category":"unhandled-edge","causal_finding_id":"PR4-F06","causal_section_ids":["1.1","1.2","1.3","1.4"],"check_key":"jsonl-nonobject-corruption-parity","description":"A syntactically valid scalar or list is accepted into the digest parser path but skipped by the current summary reader. The resulting generic digest error lacks error_kind=transcript_read, so § 1.4 may proceed to a summary that silently omitted the same record.","finding_id":"PR10-F02","fix":"Define every decoded non-object JSONL value as TranscriptReadError in both readers, including the final line because it is a complete invalid record. Add scalar/list parity tests proving digest and summary abort and no summary revision persists.","introduced_in_round":4,"location":"§§ 1.1–1.4 shared TranscriptReadError boundary","prevention":"Test interior and final JSONL records for malformed syntax, valid non-object values, and valid objects through both digest and summary readers.","principle":"All readers of one transcript must classify the same schema-invalid record identically before downstream refresh decisions.","root_cause":"The plan defines malformed JSON syntax while leaving valid JSON scalars and arrays outside the shared corruption contract.","section_id":"1.2","severity":"blocking"},{"category":"unhandled-edge","causal_finding_id":"PR5-F08","causal_section_ids":["2.1"],"check_key":"continuation-serialized-size-parity","description":"Near-budget emoji or other non-ASCII continuation content can be materially underestimated. The delivered response then receives the continuation exemption and bypasses offloading despite exceeding the serializer-visible budget.","finding_id":"PR10-F03","fix":"Use one exported serializer/size function for both continuation fitting and the proxy result path, or measure the actual MCP transport representation directly. Add a non-ASCII near-budget regression alongside the existing quote/backslash case.","introduced_in_round":5,"location":"§ 2.1 continuation response fit predicate and result-offload exemption","prevention":"Compare fit-predicate length with transport/offloader serialization for ASCII, escape-heavy, and non-ASCII payloads at one byte below, at, and above the budget.","principle":"A delivery budget must measure the exact serialization representation that crosses the bounded transport.","root_cause":"The plan equates compact ensure_ascii=False JSON length with proxy serialization, while the live success serializer uses indented JSON and default ensure_ascii=True.","section_id":"2.1","severity":"blocking"},{"category":"unhandled-edge","causal_finding_id":"PR3-F05","causal_section_ids":["1.1","1.3","1.5"],"check_key":"codex-filechange-terminal-status","description":"A completed Codex FileChange can render '(no result recorded)', falsely classifying authoritative completed edit evidence as in flight. The golden suite checks the path while omitting the edit line's terminal marker.","finding_id":"PR10-F04","fix":"Specify that item_completed FileChange entries are resolved successes unless the native item explicitly reports failure, mirror that terminal status in analyzer projection where needed, and assert the golden FileChange line is bare.","introduced_in_round":3,"location":"§ 1.1 Codex item_completed FileChange projection / § 1.5 golden fixture","prevention":"For every item_completed variant, assert success, failure, and missing-result rendering independently; require completed edit entries to render bare.","principle":"Every completed native activity record needs an explicit terminal status before ledger rendering.","root_cause":"FileChange creates ToolActivityEntry values from item_completed records without setting resolved, whose default is false and which has no later result record.","section_id":"1.1","severity":"blocking"},{"category":"unhandled-edge","causal_finding_id":"PR1-F02","causal_section_ids":["2.1"],"check_key":"async-continuation-claim-io","description":"The atomic continuation claim opens a synchronous database transaction and performs render probes on the daemon event loop. Lock contention can stall every concurrent waiter and session operation.","finding_id":"PR10-F05","fix":"Add an async attachment wrapper for wait_for_summary that runs the complete atomic claim in asyncio.to_thread and mutates its private result only after the await. Keep the synchronous path for get_handoff_context and add a blocked-transaction event-loop regression.","introduced_in_round":1,"location":"§ 2.1 wait_for_summary continuation attachment","prevention":"Instrument every sync database call added to an async handler and prove an unrelated loop task advances while the database operation is blocked.","principle":"Synchronous database transactions must leave the daemon event loop before an async wait path awaits further work.","root_cause":"The shared synchronous attachment helper calls SessionVariableManager._mutate_variables directly from async wait_for_summary.","section_id":"2.1","severity":"blocking"},{"category":"bad-sequencing","causal_finding_id":"PR6-F02","causal_section_ids":["1.2"],"check_key":"tail-withheld-auto-refresh-suppression","description":"When a complete prefix is digested and the trailing pair is withheld, _build_turn_and_digest_serialized still reaches its unconditional _schedule_summary_refresh_if_stale call. A stale summary can be spawned before the § 1.4 caller sees tail_withheld and aborts.","finding_id":"PR10-F06","fix":"Suppress automatic summary scheduling whenever the resolved outcome carries tail_withheld; the later digest that covers the pair should schedule it. Add a real scheduler-spy test for a persisted prefix plus withheld tail.","introduced_in_round":6,"location":"§ 1.2 _build_turn_and_digest_serialized to § 1.4 refresh handoff","prevention":"For every partial-success outcome, trace all internal side effects before the public return and assert they honor the same coverage watermark as callers.","principle":"A downstream summary refresh cannot start until the digest coverage watermark says the active tail is covered.","root_cause":"The plan propagates tail_withheld to callers while leaving the existing automatic summary-refresh scheduling after prefix persistence unchanged.","section_id":"1.2","severity":"blocking"},{"category":"unhandled-edge","causal_finding_id":"PR5-F04","causal_section_ids":["1.3"],"check_key":"summary-native-adapted-turn-boundary","description":"The no-digest branch of _build_summary_prompt_context selects the Grok, Codex, Qwen, or Droid parser and reparses its turns. Passing the planned adapted list breaks that existing fallback even though digest-present tests pass.","finding_id":"PR10-F07","fix":"Pass raw window.turns to _build_summary_prompt_context on the no-digest native-parser path and reserve the materialized adapted list for TranscriptAnalyzer. Add no-digest regressions for all four non-Claude providers.","introduced_in_round":5,"location":"§ 1.3 build_summary_source_context no-digest branch","prevention":"Test the shared builder with and without a digest for every provider, asserting the analyzer receives adapted turns and native parsers receive native turns.","principle":"A shared builder must preserve each consumer's required data shape across adaptation boundaries.","root_cause":"SummarySourceContext defines turns as Claude-shaped analyzer output without specifying that _build_summary_prompt_context still needs the raw native window when no digest exists.","section_id":"1.3","severity":"blocking"},{"category":"unhandled-edge","causal_finding_id":"PR6-F05","causal_section_ids":["2.1"],"check_key":"continuation-present-but-unfittable","description":"With no required skills and optional task/profile/MCP content that cannot fit beside get_handoff_context's base, the renderer can return ''. The atomic claim then clears all one-shots although none of the optional content was delivered, contradicting acceptance 2.1.16.","finding_id":"PR10-F08","fix":"Return ContinuationBlock(text='', ...) only when the source variables contain no sections. Return None when present content yields no fitting block or marker so get_handoff_context leaves state armed and wait_for_summary can retry after its stub. Add optional-only no-fit and truly-empty regressions.","introduced_in_round":6,"location":"§ 2.1 optional-only continuation fit and empty no-op claim","prevention":"Cross-product required present/absent, optional present/absent, base fits/does-not-fit, and marker fits/does-not-fit; assert consumption only after actual delivery or a genuinely empty source.","principle":"A truly empty one-shot payload and a present payload that cannot fit are distinct state-transition outcomes.","root_cause":"The renderer can collapse present optional sections to the same empty string claim_compact_continuation treats as successful no-op delivery.","section_id":"2.1","severity":"blocking"},{"category":"missing-requirement","check_key":"complete-payload-truncation-contract","description":"The renderer prefix-cuts task_context and user_profile_content, and the withheld fallback prefix-cuts an oversized original prompt. docs/contracts/truncation.md forbids these marked prefixes; only the ledger's intentional newest-tail treatment fits the allowed log-tail class.","finding_id":"PR10-F09","fix":"Deliver complete variable sections atomically, using the existing get_variable or gobby-results pointer paths when they do not fit and whole-item omission where valid. Preserve the withheld prompt whole behind a retrievable reference instead of copying its head.","location":"§ 2.1 lower-priority continuation sections and § 1.4 withheld-pair prompt","prevention":"For every cap, classify the value as complete payload, item collection, or intentional log tail before choosing whole delivery, pointer, whole-item omission, or tail semantics.","principle":"A complete payload may be delivered whole, referenced, or omitted as whole items; a marked prefix is still data loss.","root_cause":"The plan budgets complete task/profile/prompt strings with character-prefix truncation instead of applying the repository truncation contract.","section_id":"2.1","severity":"blocking"},{"category":"gobby-format","check_key":"module-scope-target-ownership","description":"hook_manager.py must move its SessionSummaryDispatcher import, state_manager.py must add TYPE_CHECKING imports, compact_continuation.py must replace module imports, and _terminal_handoff.py must add COMPACT_HANDOFF_TAIL_RETRY_ATTEMPTS. Their exact-symbol Targets do not own those edits.","finding_id":"PR10-F10","fix":"Replace the affected exact entries with justified ::* Targets for hook_manager.py in § 1.4, state_manager.py in § 2.1, compact_continuation.py in § 2.2, and _terminal_handoff.py in § 1.4, naming the module-scope import/constant edits and affected symbols in each scope reason.","location":"Targets in §§ 1.4, 2.1, and 2.2","prevention":"For every new import, removed import, constant, decorator, or type-checking guard, verify the owning Target covers module scope.","principle":"Imports and module constants changed by a deliverable require file scope that owns module-level edits.","root_cause":"Exact method/class Targets cover function bodies while the plan also requires module import removal/addition and a new module constant.","section_id":"1.4","severity":"blocking"},{"category":"traceability","check_key":"exact-target-consumer-closure","description":"Confirmed omissions include CLI/test consumers of generate_summary and TranscriptAnalyzer.extract_handoff_context, tests/sessions/test_machine_scoped_consumers.py for compact_summary_metadata_matches, HTTP/stdio/embedding-switch consumers of setup_internal_registries, and readiness/terminal/clear suites for register_terminal_tools.","finding_id":"PR10-F11","fix":"Complete the literal caller/import sweeps and add every owned production/test consumer to the appropriate deliverable. Remove exact Targets for symbols that remain unchanged, and use justified file-wide scope for multi-symbol plumbing where that accurately describes the edit.","location":"Exact Targets across §§ 1.1, 1.3, 1.4, and 2.1","prevention":"Run gcode usages or literal sweeps for every exact Target and repeat recursively for exact consumer Targets before review.","principle":"Every owned caller and import consumer of an exact Target must appear in a deliverable inventory or the exact Target must be removed when the symbol is deliberately unchanged.","root_cause":"Prior repairs added exact consumer symbols as Targets without closing their own caller graphs, and new registry/plumbing signatures were inventoried without their production and test consumers.","section_id":"1.4","severity":"blocking"},{"category":"unhandled-edge","causal_finding_id":"PR9-F09","causal_section_ids":["1.4"],"check_key":"tail-retry-timeout-evidence-retention","description":"If the first attempt returns tail_withheld with withheld_pair and a later retry times out, the generic timeout fallback prefers prior digest markdown and marks handoff_ready. The already-captured prompt and ledger can be discarded.","finding_id":"PR10-F12","fix":"Retain the latest withheld outcome outside the retry coroutine. On any later timeout, pass its withheld_pair with tail_withheld=true into _persist_compact_handoff_fallback, and extend acceptance 1.4.17's second-retry hang case to assert prompt and newest ledger survival.","introduced_in_round":9,"location":"§ 1.4 foreground tail retry deadline","prevention":"Cross each retry ordinal with completion, persistent withhold, timeout, cancellation, and corruption; assert the best previously captured evidence survives every terminal branch.","principle":"Once stable evidence for the compact-triggering turn is captured, every later retry outcome must preserve it until delivery.","root_cause":"The single timeout handler has no retained reference to an earlier tail_withheld outcome after a later retry hangs.","section_id":"1.4","severity":"blocking"}],"reviewer_session":"3e4d4196-0cda-463d-a075-6bf3515b91ba","round":10,"verdict":"needs_review"},"session_id":"6bc09ed1-7072-423d-a89f-1c7881928c8b"}
```

**Round 11** `kind: verification`

- reviewer_run: b948afed-bf8c-425b-a4b8-0b719694f06f
- reviewer_session: 6d81b424-d47c-4dcb-bfdb-da0cd4e8f7ca
- verdict: needs_review
- findings:
- PR11-F01 / blocking / `SessionLifecycleManager._sweep_digest_backlogs` breaks its bounded per-session loop only on `None` or `error`, so a `tail_withheld` outcome with no progress burns the remaining attempts rereading the same in-flight pair; the consumer and its test seam are absent from §1.2.
- PR11-F02 / blocking / the round-10 persistence barrier performs one interruptible `asyncio.wait`; a second `cancel()` during that wait releases the per-session lock while the worker thread is still writing (fixer-induced by PR10-F01).
- PR11-F03 / blocking / the retained `tail_withheld` evidence feeds only the timeout fallback; a later returned error, returned cancellation, or raised exception persists the prior-digest fallback with `tail_withheld=False` and discards the captured prompt and ledger (fixer-induced by PR10-F12).
- PR11-F04 / blocking / `attach_compact_continuation` has no exception boundary: a claim transaction or renderer failure after a valid base result is built turns the whole delivery into a tool failure, which the Grok directive treats as terminal while the one-shots stay armed.
- PR11-F05 / blocking / `is_commit_producing` is specified by `git commit` substring, so quoted, echoed, commented, or searched occurrences can cross the successful-output retention boundary.
- PR11-F06 / blocking / the Codex, Qwen, and Droid ledger and analyzer scans run `self.iter_parse_events` on the live parser, mutating private correlation state that the digest reader's second `_extract_digest_pairs` call and any later incremental use then inherit.
- votes: unattended coordinator judgment under the user's standing direction ("continue for 10 more rounds or until you reach convergence, unattended, use your best judgment on the findings, keep scope creep to a minimum"). PR11-F01 accept — verified in `lifecycle.py` lines 456–470: the batch loop tests `result is None or "error" in result` only, and `_expire_loop` is its sole caller; the typed repairs add the exact method Target plus the `tests/sessions/test_sessions_lifecycle.py::*` seam (the `TestDigestBacklogSweep` class already exists there) and acceptance 1.2.12, and the body states that a `tail_withheld` outcome ends the session's current batch loop after its prefix progress is persisted. PR11-F02 accept — the barrier's cleanup becomes a loop that re-awaits `asyncio.wait({persist})` on every further `CancelledError` until the future is done, retrieves the worker's exception so a failed write is logged rather than lost, and only then re-raises; extra cancellation requests need no bookkeeping because the task's own cancel count already makes `wait_for` re-raise cancellation instead of converting it to `TimeoutError`; 1.2.10 gains the double-cancel race as acceptance 1.2.13. PR11-F03 accept — the retained latest withheld outcome now feeds `_persist_compact_handoff_fallback` for every later non-corruption terminal failure of the same refresh (timeout, returned `{"error"}`, returned `{"cancelled"}`, raised exception) with that failure's text as `reason`; the corruption branch still persists nothing; 1.4.17 is rewritten to parametrize the four branches. PR11-F04 accept with the least mechanism — verified that the helper's claim runs a synchronous `transaction_immediate` whose rollback leaves the one-shots armed but whose exception escapes both delivery tools; the helper now catches `Exception` from either claim outside the transaction, logs it with the session id, restores the unstubbed base result, and sets `continuation_pending: true` so the response stays a valid base handoff; the Grok directive treats `continuation_pending=true` as retryable inside its existing four-call cap; acceptance 2.1.21 covers both delivery tools under transaction and renderer faults and 2.2.1 names the retry clause. PR11-F05 accept — verified that `workflows/commit_guard.py::parse_git_commit_invocations` already token-parses shell commands (`shlex.split`, `git` token with global-option skipping, `commit` subcommand, control-token segmentation); `is_commit_producing` classifies shell calls by that utility through a function-local import (`commit_guard` pulls in hook and workflow-state modules that must not load with the transcript parsers), and 1.1.7 gains the negative cases; the leak was already narrowed by `commit_outcome`'s `[<branch> <sha>]` parse, which is unchanged. PR11-F06 accept — verified that today's `extract_last_messages` is stateless for all three parsers while the plan's `self.iter_parse_events` scan would mutate `_execution_chain` and `_pending_tool_search_use_ids` (Codex), `_last_tool_use_id` (Qwen), and `_last_assistant_index` (Droid), and that Qwen's and Droid's `snapshot_state` do not cover those fields, so snapshot/restore is insufficient: every observational scan (the §1.1 ledger collection and the §1.3 adapter) runs on a fresh scan parser of the same class (`type(self)(session_id=self.session_id)`; Droid without `transcript_path`, so no sidecar load) and never touches the live instance; acceptance 1.1.18 proves state and output parity against an untouched control parser for the three providers and the digest reader's segment-plus-prefix double scan.
- resolution_notes: §1.1: `is_commit_producing` classifies shell commands with `parse_git_commit_invocations` (function-local import) and 1.1.7 adds quoted, echoed, commented, and search-pattern negatives; the Codex/Qwen/Droid ledger rule runs `iter_parse_events` on a fresh same-class scan parser, the §1.3 adapter follows the same rule, and 1.1.18 proves parser-state isolation. §1.2: `lifecycle.py::SessionLifecycleManager._sweep_digest_backlogs` and `tests/sessions/test_sessions_lifecycle.py::*` Targets added, the batch loop ends on `tail_withheld` after prefix progress, the persistence barrier loops until the worker settles under repeated cancellation and retrieves the worker exception, 1.2.12 and 1.2.13 added. §1.4: the retained withheld outcome feeds every later non-corruption terminal failure's fallback; 1.4.17 parametrizes timeout, returned error, returned cancellation, and raised exception. §2.1: `attach_compact_continuation` gains an exception boundary that preserves the base result, leaves the one-shots armed, and sets `continuation_pending: true`; 2.1.21 added. §2.2: the Grok directive retries on `continuation_pending=true` within the same cap; 2.2.1 extended.

```json plan-review-round
{"evidence_id":"e6d6640b-9958-43b4-9d0f-60f0d0b929a3","plan_hash":"4b7267ffc9ec509a9eaecdc9a2a60a4dc3e9a72d2c432dd0544174c8ec2c4939","round_number":11,"round_result":{"coverage_attestation":{"adjacent_variant_complete":true,"attestation_digest":"bde7bb37973fcf22e5f5c94c63b7d5b2e5109c3a124f513ca4f4dd114e52db2d","cross_lane_interaction_complete":true,"disposition_counts":{"dismissed":0,"emitted_findings":6,"total":6},"evidence_id":"e6d6640b-9958-43b4-9d0f-60f0d0b929a3","lanes":[{"candidate_count":0,"lane_id":"requirements_traceability","status":"completed"},{"candidate_count":1,"lane_id":"repository_blast_radius","status":"completed"},{"candidate_count":5,"lane_id":"runtime_invariants","status":"completed"}],"shadow_manifest_status":{"entry_count":8,"manifest_digest":"20b4f3a941c671eca9134e7678e733301365b3196d4d13d940172fbbe99c9600","status":"valid"},"source_digest":"1618230f8cd4e71a0a66a0ab2dd57998b2d23ac00a00df95f644b82ee343062f","version":1},"findings":[{"category":"traceability","check_key":"tail-withheld-backlog-sweep-consumer","description":"`build_turn_and_digest` can now return a successful `tail_withheld` result with no digest progress, while `_sweep_digest_backlogs` breaks only on `None` or `error`. The sweep therefore spends its remaining bounded attempts rereading the same incomplete trailing pair, and neither the consumer nor its test seam appears in §1.2.","finding_id":"PR11-F01","fix":"Add the lifecycle sweep and its test seam to §1.2. Make any `tail_withheld` result terminate that session's current backlog batch loop after preserving any prefix progress; a later sweep or turn-end digest can retry once the tail changes.","location":"P1 / § 1.2 public digest outcome and SessionLifecycleManager._sweep_digest_backlogs","prevention":"Search every call site of a changed result producer and enumerate how each loop, retry, wrapper, and pass-through classifies every new outcome.","principle":"Every semantic consumer of a changed public outcome must classify the new state explicitly.","repairs":[{"entries":["`src/gobby/sessions/lifecycle.py::SessionLifecycleManager._sweep_digest_backlogs`","`tests/sessions/test_sessions_lifecycle.py::*` — scope-reason: add tail-withheld backlog-sweep termination coverage"],"kind":"add_targets","section_id":"1.2"},{"items":[{"artifact":"test: `tests/sessions/test_sessions_lifecycle.py::TestDigestBacklogSweep.test_sweep_stops_session_on_tail_withheld`","prose":"The lifecycle backlog sweep stops the current per-session batch loop after any tail_withheld outcome, including one that also persisted a complete prefix, without spending another bounded attempt on the same in-flight trailing pair; ordinary progress and error termination remain unchanged."}],"kind":"add_acceptance","section_id":"1.2"}],"root_cause":"The plan traced tail_withheld through compact-summary callers but omitted the lifecycle backlog loop, whose progress predicate recognizes only None and error.","section_id":"1.2","severity":"blocking"},{"category":"unhandled-edge","causal_finding_id":"PR10-F01","causal_section_ids":["1.2","1.4"],"check_key":"digest-persistence-barrier-repeat-cancel","description":"A second `cancel()` while the barrier is awaiting the persistence future can release the per-session lock while the worker thread is still writing. That recreates the late-write race PR10-F01 was meant to remove.","finding_id":"PR11-F02","fix":"Specify cancellation-resilient cleanup that loops until the persistence future is done while the lock remains held, records additional cancellation requests, retrieves the worker's result or exception, and only then re-raises cancellation. Add a double-cancel regression raced with a second digest.","introduced_in_round":10,"location":"P1 / § 1.2 persistence cancellation barrier used by § 1.4 timeout","prevention":"Inject cancellation before persistence, during persistence, and repeatedly during cleanup; race each case with a second same-session digest and assert the lock is retained until the worker settles.","principle":"A cancellation barrier must retain serialization ownership until the uncancellable worker reaches a terminal state, even under repeated cancellation requests.","root_cause":"The proposed cleanup catches one CancelledError and then performs one interruptible `asyncio.wait`; a second cancel can abort that await before the persistence future finishes.","section_id":"1.2","severity":"blocking"},{"category":"unhandled-edge","causal_finding_id":"PR10-F12","causal_section_ids":["1.4"],"check_key":"tail-retry-terminal-outcome-evidence-retention","description":"After one retry captures `tail_withheld` and `withheld_pair`, a later non-corruption retry that returns an error/cancellation or raises can persist the prior-digest fallback with `tail_withheld=False`, mark `handoff_ready`, and discard the captured compact-triggering prompt and ledger.","finding_id":"PR11-F03","fix":"Retain the latest withheld outcome across the entire foreground retry state machine. Feed it into `_persist_compact_handoff_fallback` for every later non-corruption terminal failure, including returned errors, returned cancellation, and raised exceptions, and add one regression per branch.","introduced_in_round":10,"location":"P1 / § 1.4 compact_self foreground digest retry terminal branches","prevention":"Cross every retry ordinal with success, persistent withhold, timeout, returned error, returned cancellation, raised exception, and corruption, and assert the best previously captured evidence survives every eligible fallback.","principle":"Once stable evidence for the compact-triggering turn is captured, every later retry outcome must preserve it until a valid replacement is available.","root_cause":"The round-10 repair carries the latest withheld pair into the timeout handler only; generic returned errors, returned cancellation, and raised exceptions still take the old fallback without that evidence.","section_id":"1.4","severity":"blocking"},{"category":"unhandled-edge","check_key":"continuation-claim-exception-recovery","description":"If the variable transaction or renderer raises, `wait_for_summary` or `get_handoff_context` can fail after constructing a valid base handoff. Grok then follows the terminal failure branch and continues without the required continuation, although the one-shots remain pending.","finding_id":"PR11-F04","fix":"Catch claim/render failures outside the transaction, log them, preserve the original successful base result, and leave all one-shots armed. Add a bounded structured pending/error indicator and teach the Grok directive to retry that state within its existing four-call cap; cover both delivery tools with transaction and renderer faults.","location":"P2 / §§ 2.1–2.2 continuation attachment and Grok wait directive","prevention":"Fault-inject database-lock, transaction, and renderer exceptions after base-result construction for every delivery tool; assert the base result survives, one-shots remain armed, and the client receives a bounded retry signal.","principle":"Failure to attach retryable continuation metadata cannot erase an already-available base handoff or silently consume the only delivery opportunity.","root_cause":"Both planned attachment call sites invoke the synchronous claim/render path without an exception boundary, while the Grok directive treats a tool failure as terminal.","section_id":"2.1","severity":"blocking"},{"category":"unhandled-edge","check_key":"commit-producing-shell-classification","description":"A command that merely echoes, searches for, comments, or quotes `git commit` can be misclassified as commit-producing. Its successful output would then enter the ledger, analyzer, and prompts despite the plan's explicit rule that all successful non-commit output is discarded.","finding_id":"PR11-F05","fix":"Define shell commit classification with the repository's token-aware `parse_git_commit_invocations` utility rather than substring matching, and add negative retention tests for quoted, echoed, commented, and search-pattern occurrences across the shared helper and analyzer boundary.","location":"P1 / §§ 1.1, 1.3, and 1.5 successful-result retention boundary","prevention":"Test positive commit invocations and negative quoted, echoed, commented, and search-pattern occurrences before allowing successful shell output into ledgers or prompts.","principle":"Privileged output retention must be selected by a real command classification, never by payload substring coincidence.","root_cause":"`is_commit_producing` is specified as true when the command contains `git commit`, so non-commit commands carrying those words can cross the success-output retention boundary.","section_id":"1.1","severity":"blocking"},{"category":"unhandled-edge","check_key":"observational-parser-state-isolation","description":"Codex, Qwen, and Droid mutate private correlation/finalization state while iterating parse events. The new ledger and analyzer scans can therefore contaminate the digest reader's second extraction pass or any later incremental use, contradicting the claimed byte-identical cursor and parser semantics.","finding_id":"PR11-F06","fix":"Run every observational ledger/analyzer scan with isolated parser state: use a correctly configured fresh parser or snapshot and restore all private state in a `finally` block. Add non-empty-state continuation tests for Codex, Qwen, and Droid plus the digest segment/prefix double-scan.","location":"P1 / §§ 1.1 and 1.3 iter_parse_events observational scans","prevention":"For every parser projection, hydrate non-empty state, run the projection, continue incremental parsing, and compare state, identities, outcomes, adjustments, indexes, and resume boundaries with an untouched control.","principle":"A read-only transcript projection must preserve parser-private incremental state and produce the same later parse behavior as a control parser that never ran the projection.","root_cause":"The plan reuses stateful parser instances for observational `iter_parse_events` scans without snapshot/restore or a fresh equivalent; the digest reader also calls pair extraction twice on one parser for the segment and prefix offset.","section_id":"1.1","severity":"blocking"}],"reviewer_session":"6d81b424-d47c-4dcb-bfdb-da0cd4e8f7ca","round":11,"round_number":11,"verdict":"needs_review"},"session_id":"6bc09ed1-7072-423d-a89f-1c7881928c8b"}
```

**Round 12** `kind: verification`

- reviewer_run: 8399810c-efb5-4c93-a745-2f338e5a7968
- reviewer_session: 47410317-70fa-420e-9161-d36e2b74ac72
- verdict: needs_review
- findings:
- PR12-F01 / blocking / `parse_git_commit_invocations` splits with comments disabled, so `ls # git commit` classifies as commit-producing and a non-commit command emitting a commit-shaped line can retain a false commit (fixer-induced by PR11-F05).
- PR12-F02 / blocking / the shared corruption policy classifies a malformed final line by position alone; a newline-terminated malformed final record is durable corruption that cannot be completed by appending bytes, yet both readers treat it as an in-flight tail (fixer-induced by PR4-F06).
- PR12-F03 / blocking / the MCP wait wrapper shields `wait_for_summary`, returns `completed:false` on its own deadline, and discards the background result, so a destructive continuation claim that lands after that deadline clears every one-shot with nothing delivered.
- PR12-F04 / blocking / the claim-exception path restores a possibly oversized base with `continuation_pending` and no `continuation`, so the offloader replaces it with a retrieval envelope whose top level omits the retry signal (fixer-induced by PR11-F04).
- votes: unattended coordinator judgment under the user's standing direction ("continue for 10 more rounds or until you reach convergence, unattended, use your best judgment on the findings, keep scope creep to a minimum"). PR12-F01 accept in part — reproduced against the live classifier: `shlex.split("ls # git commit")` yields `['ls', '#', 'git', 'commit']` and `parse_git_commit_invocations` returns one invocation, while `shlex.split(..., comments=True)` yields `['ls']` and zero invocations; the already-covered quoted, echoed, and grep forms return zero either way. `is_commit_producing` therefore strips unquoted comments (`shlex.join(shlex.split(command, comments=True))`, raw command on `ValueError`) before calling the shared classifier, which leaves `commit_guard` untouched — a commented invocation is fail-safe for the commit guard and fail-open only for output retention. The unreachable-control-flow half (`false && git commit -m x`) is declined: a segment that never executes emits no commit-shaped stdout, and full shell reachability analysis is far more mechanism than the retention boundary needs; 1.1.7 gains the commented-plus-forged-stdout negative. PR12-F02 accept — the observation is sharp: a JSONL writer emits a complete record and then its newline, and a raw newline cannot appear inside a JSON string, so a malformed final line that is already newline-terminated can never be completed by appending bytes and is finished corruption; both readers have the bit (1.2's loop reads binary lines, 1.3's reverse reader reads the tail bytes). The shared contract now retries and withholds only a malformed **unterminated** EOF fragment and raises `TranscriptReadError` for a malformed newline-terminated final record, including when the bounded re-read returns a now-terminated but still malformed line; 1.1.16, 1.2.7, 1.3.12, and 1.4.11 updated. PR12-F03 accept — verified in `mcp_proxy/wait_tools.py`: `_await_with_guard` wraps `asyncio.shield(task)` in `asyncio.wait_for(timeout + WAIT_TOOL_WRAPPER_GRACE_SECONDS)` and on `TimeoutError` returns `{"success": True, "completed": False, "background_call_continues": True}` while the shielded handler keeps running and its result reaches only `_consume_background_result`, which discards it — so a claim that commits after the wrapper deadline destroys the one-shots with nothing delivered, and a cancelled `to_thread` claim has the same boundary. The repair makes the claim replayable within one compaction generation: the same mutation that consumes the one-shots also writes `compact_continuation_rendered` (the rendered text plus the current `compact_notification_started_at` stamp as its generation key), and a later claim on a non-pending session returns that cached text when the stamp still matches. No new write site is needed — `prepare_compact_continuation_variables` already restamps on every compact restart and the next generation's first claim overwrites the cache before any replay — so the fix costs one variable and no new Target; 2.1.2, 2.1.5, 2.1.17 reworded and 2.1.22 added for the wrapper-timeout and cancellation races. PR12-F04 accept with the least mechanism — verified `_build_envelope` returns `offloaded/server_name/tool_name/content_kind/total_chars/stored_chars/retrieval_available/guidance/result_id/structure/preview` and no status keys, and that the exemption is keyed on a delivered `continuation`; with `threshold_chars` at 15,000 against a 9,500-char inline budget an ordinary base stays inline, but an escape-heavy one need not. Rather than teach the shared offloader to preserve per-tool status fields, the pending response is made inherently bounded: on the claim exception `wait_for_summary` (`allow_base_stub=True`) swaps in the reference stub it already owns and returns `continuation_pending: true`, so the retry signal is never offloaded away; `get_handoff_context` (`allow_base_stub=False`) keeps its base and offloads normally exactly as PR5-F11 settled, with every one-shot still armed so the next `wait_for_summary` delivers. 2.1.21 extended and 2.2.1 already names the pending retry.
- resolution_notes: §1.1: `is_commit_producing` strips unquoted shell comments before the token-aware classifier and 1.1.7 adds the commented-invocation negative with forged commit-shaped stdout; the shared corruption contract distinguishes an unterminated EOF fragment (in-flight, retried then withheld) from a newline-terminated malformed final record (`TranscriptReadError`), with 1.1.16 rewritten. §1.2: the stable-read paragraph and 1.2.7 carry the termination rule, including the re-read that returns a terminated-but-malformed line. §1.3: the reverse reader applies the same rule and 1.3.12 is extended. §1.4: 1.4.11 adds the terminated-final-record corruption case end to end. §2.1: `claim_compact_continuation` caches the rendered block under `compact_continuation_rendered` with the `compact_notification_started_at` generation stamp in the consuming mutation and replays it for the same generation, so a wrapper timeout or cancellation after the claim can no longer lose the continuation; the claim-exception path stubs the base for `wait_for_summary` so `continuation_pending` survives offloading; 2.1.2, 2.1.5, 2.1.17, 2.1.21 updated and 2.1.22 added. PR12-F01's shell reachability analysis and PR12-F04's status-preserving offload envelope are declined as mechanism beyond the problem.

```json plan-review-round
{"evidence_id":"e811de4d-3f89-476d-bcfa-bcb421d77c8c","plan_hash":"50ea039ed26026599b0bcfed5eddbd1469e18c1823d17379a5f28c0d9ebcd249","round_number":12,"round_result":{"coverage_attestation":{"adjacent_variant_complete":true,"attestation_digest":"25d4ac41da4aab5afc9fbe46016bee0dbd8a65bb0b6319a70fbc9ff4e58f205a","cross_lane_interaction_complete":true,"disposition_counts":{"dismissed":0,"emitted_findings":4,"total":4},"evidence_id":"e811de4d-3f89-476d-bcfa-bcb421d77c8c","lanes":[{"candidate_count":0,"lane_id":"requirements_traceability","status":"completed"},{"candidate_count":0,"lane_id":"repository_blast_radius","status":"completed"},{"candidate_count":4,"lane_id":"runtime_invariants","status":"completed"}],"shadow_manifest_status":{"entry_count":8,"manifest_digest":"4fa3ddb4f7b38a525d2968ab72a038e2d53420d93a8e009b2774dce2fdc8c685","status":"valid"},"source_digest":"b3d0448eb34618b7f6bb87d6367cacf59f6f0b10ce554805e2f4cbe82c4bfab3","version":1},"findings":[{"category":"unhandled-edge","causal_finding_id":"PR11-F05","causal_section_ids":["1.1"],"check_key":"commit-output-command-provenance","description":"`parse_git_commit_invocations` uses `shlex.split(command)` with comments disabled and scans every token, so `ls # git commit` is classified as commit-producing. A successful non-commit command that emits `[branch sha] subject` can therefore retain a false commit and feed it through the analyzer and five-CLI parity path.","finding_id":"PR12-F01","fix":"Change § 1.1 to require an executable-position classifier that respects shell comments and control operators before stdout is admitted. Add negative cases for commented and unreachable git tokens paired with commit-shaped stdout, while preserving direct `git -C … commit` and `cd … && git commit` positives.","introduced_in_round":11,"location":"P1 / § 1.1 `is_commit_producing` and commit-output retention boundary","prevention":"Cross command classification with comments, unreachable control-flow segments, and forged commit-shaped stdout whenever output retention depends on shell syntax.","principle":"Retained successful output must be attributable to the executable command segment that produced it.","root_cause":"The round-11 repair reuses a lexical git-command scanner that treats unquoted shell comments and unreachable segments as invocations, while `commit_outcome` inspects stdout for the whole shell call.","section_id":"1.1","severity":"blocking"},{"category":"unhandled-edge","causal_finding_id":"PR4-F06","causal_section_ids":["1.1","1.2","1.3","1.4"],"check_key":"final-record-termination-corruption","description":"The plan says every malformed final nonblank line is retried and then withheld, including a final record already terminated by `\\n`. That record cannot be completed by appending bytes to it, so digest and summary readers may silently omit durable corruption until another record arrives instead of raising `TranscriptReadError` immediately.","finding_id":"PR12-F02","fix":"Preserve the EOF line-termination bit in both readers. Retry and withhold only a malformed unterminated EOF fragment; raise `TranscriptReadError` for a malformed newline-terminated final record. Add reader-parity tests for both forms and an end-to-end § 1.4 corruption test.","introduced_in_round":4,"location":"P1 / §§ 1.1–1.4 shared transcript-corruption policy","participating_section_ids":["1.1","1.2","1.3","1.4"],"prevention":"Test malformed final JSONL in both terminated and unterminated forms through each reader and every corruption/withhold caller branch.","principle":"Only an unterminated EOF fragment is plausibly in flight; a newline-terminated malformed JSONL record is durable corruption.","root_cause":"The shared policy classifies malformed data solely by final-record position and discards the physical line-termination signal available to both binary readers.","section_id":"1.1","severity":"blocking"},{"category":"unhandled-edge","check_key":"one-shot-wrapper-abandonment","description":"If summary readiness occurs near the wrapper deadline, or the synchronous claim blocks, the MCP wrapper can return `completed:false` while the background handler later clears all continuation one-shots. Grok retries as directed, but the continuation has disappeared; cancellation during the `to_thread` claim creates the same lost-delivery boundary.","finding_id":"PR12-F03","fix":"Make the rendered continuation replayable by compact generation: atomically cache the block while consuming its source variables, return the cached block on every retry, and supersede it only on the next compaction or another explicit proven-delivery boundary. Add wrapper-timeout and caller-cancellation regressions.","location":"P2 / §§ 2.1–2.2 `wait_for_summary` claim across the MCP wait wrapper","participating_section_ids":["2.1","2.2"],"prevention":"Race every destructive delivery claim with outer-wrapper timeout and caller cancellation, then retry and assert the same continuation remains observable.","principle":"A destructive one-shot claim must remain replayable until its result crosses the caller-visible delivery boundary.","root_cause":"The client guard shields `wait_for_summary`, returns a timeout envelope, and lets the handler continue; the planned worker-thread claim can clear state after that caller-visible timeout and its result is then consumed only by a background callback.","section_id":"2.1","severity":"blocking"},{"category":"unhandled-edge","causal_finding_id":"PR11-F04","causal_section_ids":["2.1","2.2"],"check_key":"pending-signal-offload-visibility","description":"An oversized claim-exception response is replaced by the ordinary retrieval envelope, whose top level omits `success`, `completed`, `found`, and `continuation_pending`. The new Grok directive therefore cannot see the only retry signal and stops with the one-shots still armed.","finding_id":"PR12-F04","fix":"Keep the exception response bounded and preserve its status through offloading. The smallest change is a delivery-tool envelope that copies `success`, `completed`, `found`, and `continuation_pending` at top level while leaving the base context retrievable by `result_id`; test both delivery tools with over-threshold bases and injected transaction/renderer failures.","introduced_in_round":11,"location":"P2 / §§ 2.1–2.2 claim-exception response through `ToolResultOffloader`","prevention":"Pass success, timeout, pending, and terminal variants of each delivery tool through the real offloader above and below its threshold.","principle":"A retry signal must survive every response transformation between the handler and the model.","root_cause":"The round-11 exception path restores the potentially oversized base and adds `continuation_pending` without `continuation`, while the planned offload exemption applies only when a `continuation` key exists.","section_id":"2.1","severity":"blocking"}],"reviewer_session":"47410317-70fa-420e-9161-d36e2b74ac72","round":12,"round_number":12,"verdict":"needs_review"},"session_id":"6bc09ed1-7072-423d-a89f-1c7881928c8b"}
```

**Round 13** `kind: verification`

- reviewer_run: 98427dbb-28d8-48fa-900c-02f7053183d3
- reviewer_session: f46b9399-23f5-4831-b4e5-ff39318c0011
- verdict: needs_review
- findings:
- PR13-F01 / blocking / the round-12 replay cache returns the block to every same-generation claimant, contradicting the exactly-once criteria, and replays a block fitted beside a different base past the content-scoped offload exemption (fixer-induced by PR12-F03).
- PR13-F02 / blocking / the claim-exception pseudocode always swaps `wait_for_summary` to the reference stub while the surrounding prose and 2.1.21 require the complete base byte-identically, so no implementation can satisfy both (fixer-induced by PR12-F04).
- PR13-F03 / blocking / the replay cache is keyed on `compact_notification_started_at`, which the notification router blanks on pause, expiry, and deadline handling, so a live cache is stranded and a later blanked generation can match a stale one (fixer-induced by PR12-F03).
- PR13-F04 / blocking / `tail_withheld` and `withheld_pair` ride only on successful returns, so a first attempt that extracts the pair and then fails during the LLM call, cancellation, or persistence discards the evidence and 1.4 falls back to the turn-blind raw tail (fixer-induced by PR4-F05).
- PR13-F05 / blocking / the shared corruption contract classifies JSON syntax, value shape, and line termination but never UTF-8 decoding, so a `UnicodeDecodeError` has no defined path in either reader (fixer-induced by PR12-F02).
- PR13-F06 / blocking / the Codex `McpToolCall` item projection marks failure from `status == "failed"` alone and resolves a transport-completed item whose structured result reports failure as a success (fixer-induced by PR2-F14).
- votes: unattended coordinator judgment under the user's standing direction ("continue for 10 more rounds or until you reach convergence, unattended, use your best judgment on the findings, keep scope creep to a minimum"). PR13-F01 accept — both halves hold against the round-12 text: the replay branch is reached by any non-pending claimant, so 2.1.6's "exactly one response with `continuation`" fails whenever `get_handoff_context` claims after a delivery, and §2.1's own non-recursion argument ("`get_handoff_context`, which by then carries no continuation") breaks with it; the cached text is also fitted against whatever base its original call carried. The repair keeps the replay and scopes it: `claim_compact_continuation` gains `allow_replay`, true only for `wait_for_summary` — the only delivery path behind `_await_with_guard`, whose shielded call is the abandonment boundary — so `get_handoff_context` neither writes nor reads the cache and cardinality holds in both claim orders; the single documented exception is two concurrent `wait_for_summary` calls, where the loser replays the identical block rather than losing it. Every attached block, replayed or fresh, now passes the current response's `fits` predicate (a no-op for a fresh block), with the stub swap retried and the block withheld with its cache intact when even that does not fit; 2.1.2, 2.1.5, 2.1.6, 2.1.13, 2.1.17 and §2.3 updated. PR13-F02 accept — the contradiction is real and mine: the round-12 pseudocode runs `_swap_base_context_for_reference_stub` on every `allow_base_stub` exception while the prose says "answered with the unstubbed base result" and 2.1.21 asserted `summary_markdown` byte-identical and "never the reference stub". Resolved toward the bounded contract the pseudocode already encodes, because it is the half that makes `continuation_pending` survive the offloader for any summary size: `wait_for_summary` returns the ~330-character stub on every claim/render exception (its summary stays retrievable through `get_handoff_context`, exactly as after a stub-swap delivery) and `get_handoff_context` keeps its complete base and offloads normally; the prose and both size branches of 2.1.21 were rewritten to that single contract. PR13-F03 accept, with a smaller repair than proposed — verified in `communications/session_notifications.py`: `_clear_compact_marker` writes `COMPACT_NOTIFICATION_STARTED_AT_VARIABLE` to `""` whenever the recorded stamp still matches, on the pause/expiry transition path, so the generation key is not stable for the retry lifetime. A dedicated `compact_continuation_generation` token would add a variable, a write site, and a comparison; instead the comparison is deleted and the cache is invalidated at its source. `apply_in_place_compact_context_loss` (the only site that arms `compact_handoff_inject_pending`, confirmed by search: the retired rule YAML is the only other writer and 2.2 retires it) clears `compact_continuation_rendered` in the same `merge_variables` call, unconditionally, so a disabled `auto_inject_handoff` cannot strand an old block either. That is sound because the replay branch is unreachable while the flag is pending — the first branch renders fresh — so a claim can only ever replay the current generation, and the cache survives a daemon restart like any session variable. §2.1 Targets gained `in_place_compact.py::*` (file-wide with a scope reason: its `_misc.py` caller is unchanged) and the existing `tests/hooks/test_session_handoff_handlers.py` coverage; 2.1.22 extended through the marker blanking, the restart, an interleaved `get_handoff_context`, and a genuinely new compaction. PR13-F04 accept — the gap is real and material: §1.2 attaches `tail_withheld`/`withheld_pair` to successful returns only, so a withheld batch whose prefix digest then fails or is cancelled loses the pair, and §1.4's fallback is the raw tail, which the plan itself says "cannot reach the opening prompt of a tool-heavy turn". The repair is one keyword-only `withheld_capture: dict[str, Any] | None` on `build_turn_and_digest`/`_build_turn_and_digest_serialized`, written immediately after `_resolve_undigested_pairs` returns a withheld resolution — before the LLM call, before persistence, before any failing branch — plus the same two keys on the `{"error"}`/`{"cancelled"}` returns of a withheld batch. Because the dict belongs to `_refresh_compact_handoff_context` and outlives the timed coroutine, the outer `asyncio.wait_for` deadline that destroys the task no longer destroys the evidence, which the finding's returned-result-only fix would still have lost; 1.2.9 and 1.4.17 extended with first-attempt failures, and the `digest.py` scope reason corrected (it claimed `build_turn_and_digest` was unchanged). PR13-F05 accept — the readers hold bytes and decode them, and the round-12 contract classifies only JSON syntax, value shape, and termination, so a split multibyte code point at EOF raises before the stated classification and durable invalid bytes have no defined reader parity. Decoding becomes the first of three ordered outcomes in the shared per-line helper (decode, JSON parse, object shape) under the identical termination rule: an unterminated fragment that fails to decode withholds; interior or newline-terminated invalid bytes raise `TranscriptReadError` at that record's offset; 1.1.16, 1.2.7, 1.3.12, and 1.4.11 now pin six byte-identical forms through both readers. PR13-F06 accept, narrowed — verified in the repository: `CodexTranscriptParser._parse_mcp_tool_call` already unwraps `Ok`/`Err` from `mcp_tool_call_end`, the plan's own failure mapping already reads non-exec Codex results as errors, and `hooks._normalization_mcp._unwrap_mcp_tool_output` already normalizes `structuredContent`, `content[0].text`, and JSON-string outputs — so the item stream is the one path where a transport-completed call with `{"success": false}` (how the proxy reports every application-level failure) renders as a success. `codex_items.mcp_item_failure(item)` reuses that unwrapper and classifies `Err`, `is_error`/`isError`, and `success is False`; the narrowing is that an item with no `result` key stays terminal item evidence exactly as `FileChange` does, rather than becoming unresolved, since items are the only record of their call. 1.1.10, 1.3.6, and the §1.5 Codex fixture (1.5.1) carry the case.
- resolution_notes: §1.1: UTF-8 decoding joins the shared corruption contract as its first ordered outcome, with the unterminated/terminated rule applied to decode failures and byte offsets on the raise (1.1.16); `McpToolCall` item entries are failed from `status == "failed"` or a structured result failure through the new `codex_items.mcp_item_failure`, reusing `_unwrap_mcp_tool_output`, with a completed no-signal result resolved and a `result`-less item terminal (1.1.10, and the failed-entry clause). §1.2: `withheld_capture` carries the withheld pair out of the call at resolution time, before the LLM call and persistence, and the `{"error"}`/`{"cancelled"}` returns of a withheld batch carry both keys (1.2.9); the `digest.py` scope reason now records the `build_turn_and_digest` pass-through; 1.2.7 covers both decode forms. §1.3: 1.3.6 pins the structured `McpToolCall` failure reaching the analyzer as a failed block, and 1.3.12 pins six-form byte parity with 1.2. §1.4: `_refresh_compact_handoff_context` owns the capture dict outside the timed coroutine and every non-corruption terminal failure — the first attempt's own included — builds its fallback from it (1.4.17); 1.4.11 adds the terminated invalid-UTF-8 case. §1.5: the Codex fixture's natively failed call is an `McpToolCall` item with transport success and a structured failure (1.5.1). §2.1: replay is scoped to `wait_for_summary` through `allow_replay`, `get_handoff_context` neither writes nor reads the cache, every attached block is re-fitted against the current response with the stub swap retried, the claim-exception contract is bounded for `wait_for_summary` and base-preserving for `get_handoff_context`, and the cache is invalidated by `apply_in_place_compact_context_loss` in the arming write instead of by a `compact_notification_started_at` comparison (Targets gained `in_place_compact.py::*` and its test file; 2.1.2, 2.1.5, 2.1.6, 2.1.13, 2.1.17, 2.1.21, 2.1.22 updated). §2.3: the MCP-tools rewrite and 2.3.3 state the per-tool retry semantics. PR13-F03's dedicated generation token and PR13-F04's returned-result-only propagation are declined in favour of the smaller repairs above, which close the same failures with less mechanism.

```json plan-review-round
{"evidence_id":"20494a0c-c027-4ff3-92c4-4be32e96d0fd","plan_hash":"268341580bdcad8f1ff7a7a4d58329d37b36269e7a8b8b750f8a59b55ba83474","round_number":13,"round_result":{"coverage_attestation":{"adjacent_variant_complete":true,"attestation_digest":"742b877a3f5b77a06e05b57aa078a20dd547e7e173af6d00ebe00aa6e97986ad","cross_lane_interaction_complete":true,"disposition_counts":{"dismissed":5,"emitted_findings":6,"total":11},"evidence_id":"20494a0c-c027-4ff3-92c4-4be32e96d0fd","lanes":[{"candidate_count":2,"lane_id":"requirements_traceability","status":"completed"},{"candidate_count":2,"lane_id":"repository_blast_radius","status":"completed"},{"candidate_count":7,"lane_id":"runtime_invariants","status":"completed"}],"shadow_manifest_status":{"entry_count":8,"manifest_digest":"16f307b2d065f3ffcd546e59a586263e3d3aa4d9a446d457e1b2bc7567b108b2","status":"valid"},"source_digest":"02272e784995b304756ef4409d6f0b57d644ad11ca702dc6e8f70307a80518c6","version":1},"findings":[{"category":"unhandled-edge","causal_finding_id":"PR12-F03","causal_section_ids":["2.1"],"check_key":"continuation-replay-cardinality-and-fit","description":"The cache makes every serialized same-generation claimant return `continuation`, contradicting the exactly-one and following-call-empty criteria. It also reuses a block fitted beside one response base beside a different base; the content-scoped offload exemption can then expose a combined response that never passed the current consumer's provider-budget check.","finding_id":"PR13-F01","fix":"Make replay eligibility delivery-state-aware so an abandoned wait-wrapper result is replayable while ordinary concurrent losers follow the chosen one-shot contract, and pass cached text through the current response's `fits` check before attachment. Retain an unfitting cache for a stubbed `wait_for_summary`, leave `get_handoff_context` offloadable, and align 2.1.2/2.1.5–2.1.7/2.1.13/2.1.17–2.1.18/2.1.22 plus §2.3 with the resulting contract.","introduced_in_round":12,"location":"P2 / § 2.1 generation replay branch and acceptances 2.1.6, 2.1.13, 2.1.18, 2.1.22","prevention":"Race every replay path across both delivery tools, both claim orders, and different base sizes; assert response cardinality, current-response fit, and offload eligibility together.","principle":"A retry cache must preserve the delivery cardinality and serialized-size invariant of the current consumer response.","root_cause":"The round-12 cache branch returns stored text without invoking the current render/fits callback or distinguishing an abandoned wrapper result from an ordinary concurrent or later caller.","section_id":"2.1","severity":"blocking"},{"category":"unhandled-edge","causal_finding_id":"PR12-F04","causal_section_ids":["2.1"],"check_key":"continuation-claim-exception-base-contract","description":"A normal-size `wait_for_summary` claim failure cannot satisfy the plan: the pseudocode always returns the stub, while 2.1.21 requires the unstubbed `summary_markdown` and says the reference stub is never used in that case. An implementer must violate either the method contract or its test.","finding_id":"PR13-F02","fix":"Adopt the round-12 pseudocode's bounded failure contract explicitly: `wait_for_summary` returns its reference stub plus `continuation_pending` on every claim/render exception, while `get_handoff_context` preserves its full base and remains offloadable. Rewrite the contradictory prose and both size branches of 2.1.21, and carry the same semantics into §2.2 and the MCP-tool documentation.","introduced_in_round":12,"location":"P2 / § 2.1 `attach_compact_continuation` exception branch and acceptance 2.1.21","prevention":"Build a branch table from implementation pseudocode and execute every acceptance case against it, including below-threshold and above-threshold bases for both delivery tools.","principle":"Pseudocode, prose, and acceptance criteria must specify the same observable failure result for every response-size branch.","root_cause":"The exception branch unconditionally swaps `wait_for_summary` to the reference stub whenever `allow_base_stub` is true, while the surrounding prose and the first half of 2.1.21 require an ordinary claim/render fault to return the complete base byte-identically.","section_id":"2.1","severity":"blocking"},{"category":"unhandled-edge","causal_finding_id":"PR12-F03","causal_section_ids":["2.1"],"check_key":"continuation-replay-generation-lifecycle","description":"After a claim commits and the caller loses the result, notification cleanup can replace `compact_notification_started_at` with an empty string. The cached entry still carries the original timestamp, so the next same-compaction retry misses the cache after all one-shots were cleared and the continuation is lost.","finding_id":"PR13-F03","fix":"Introduce a dedicated `compact_continuation_generation` token that notification code never clears, set or overwrite it atomically when `apply_in_place_compact_context_loss` arms the pending continuation, and key the replay cache to that token. Add the arming files to §2.1 Targets and extend 2.1.22 through marker cleanup, restart recovery, and a genuinely new compaction.","introduced_in_round":12,"location":"P2 / § 2.1 `compact_continuation_rendered` generation key","prevention":"Inventory every writer and clearer of a proposed generation field, then test replay across cancellation, pause, notification cleanup, daemon restart, and the next compaction.","principle":"A replay generation key must remain stable for the full retry lifetime and change only when a new generation supersedes it.","root_cause":"The cache reuses `compact_notification_started_at`, whose live owner clears it during pause, expiry, and notification-deadline handling before another compaction necessarily occurs.","section_id":"2.1","severity":"blocking"},{"category":"unhandled-edge","causal_finding_id":"PR4-F05","causal_section_ids":["1.2"],"check_key":"withheld-pair-post-resolution-failure","description":"A first compact-refresh attempt can extract a `withheld_pair` and then fail during turn-record generation or persistence before returning a successful withheld outcome. Section 1.4 retains only a returned withheld outcome, so it falls back to the prior digest or bounded raw tail and can again omit the compact-triggering prompt the withheld pair had already preserved.","finding_id":"PR13-F04","fix":"Carry `tail_withheld` and the exact `withheld_pair` on every post-resolution cancellation/error result and persistence-failure signal, using a typed outcome if needed. Extend 1.2.9 and 1.4.17 with first-attempt failures during the LLM call and `persist_digest_state`, proving the fallback begins with the extracted prompt and carries its ledger.","introduced_in_round":4,"location":"P1 / §§ 1.2 and 1.4 post-resolution digest failure paths","prevention":"Inject every failure point after evidence extraction and assert each returned or raised outcome still carries the fallback watermark and payload.","principle":"Once stable fallback evidence has been extracted, every later terminal outcome must preserve it until persistence or caller-visible recovery consumes it.","root_cause":"`tail_withheld` and `withheld_pair` are added to successful public results, while LLM cancellation, generic build failure, and persistence failure after `ResolvedPairs` exists use error paths that discard the resolved metadata.","section_id":"1.2","severity":"blocking"},{"category":"unhandled-edge","causal_finding_id":"PR12-F02","causal_section_ids":["1.1","1.2","1.3","1.4"],"check_key":"transcript-utf8-corruption-parity","description":"An incomplete multibyte code point in an unterminated final fragment raises during UTF-8 decoding before the stated JSON classification; it is a retryable in-flight tail. Invalid UTF-8 in an interior or newline-terminated record is durable corruption. Without an explicit shared rule, the digest catch-all and reverse summary reader can take different paths and bypass `error_kind: transcript_read`.","finding_id":"PR13-F05","fix":"Make UTF-8 decoding part of the shared per-line helper: retry and then withhold a decode failure only for an unterminated EOF fragment; raise `TranscriptReadError` with the byte offset everywhere else. Extend 1.1.16, 1.2.7, 1.3.12, and 1.4.11 with byte-level parity and end-to-end abort cases.","introduced_in_round":12,"location":"P1 / §§ 1.1–1.4 shared binary transcript-read contract","prevention":"Test identical raw byte fixtures through every reader: split multibyte EOF, durable invalid interior bytes, and durable invalid newline-terminated final bytes.","principle":"A byte-oriented corruption contract must classify decoding failures before JSON parsing and apply the same EOF rule in every reader.","root_cause":"The plan says both readers decode binary lines as UTF-8, yet the shared policy and acceptance tests cover JSON syntax, value shape, and newline termination without specifying `UnicodeDecodeError`.","section_id":"1.1","severity":"blocking"},{"category":"unhandled-edge","causal_finding_id":"PR2-F14","causal_section_ids":["1.1","1.3","1.5"],"check_key":"codex-mcp-structured-failure","description":"A Codex `McpToolCall` with transport status `completed` and a structured failed result renders as a bare successful ledger line and a non-error analyzer result. The live repository already normalizes dictionary and JSON-string outputs and has a canonical structured-result classifier, so this failure is observable and currently omitted from the plan.","finding_id":"PR13-F06","fix":"Normalize each `McpToolCall.result` in the shared item projection and classify failure from either provider failure status or canonical structured-result signals; only a completed result without a failure signal becomes a resolved success. Add status-completed/result-failed cases to §1.1 ledger tests, §1.3 analyzer tests, and the Codex fixture in §1.5.","introduced_in_round":2,"location":"P1 / §§ 1.1, 1.3, and 1.5 Codex `McpToolCall` item projection","prevention":"For every provider envelope, cross product transport status with structured success/error fields and run each case through ledger, analyzer, and golden-path assertions.","principle":"Transport completion and application success are separate signals; structured tool results must participate in the canonical outcome classification.","root_cause":"The item projector marks `McpToolCall` failures from `status == \"failed\"` and otherwise resolves them successfully, even though the item also carries a structured `result` that can report `success: false` or an error.","section_id":"1.1","severity":"blocking"}],"reviewer_session":"f46b9399-23f5-4831-b4e5-ff39318c0011","round":13,"round_number":13,"verdict":"needs_review"},"session_id":"6bc09ed1-7072-423d-a89f-1c7881928c8b"}
```

**Round 14** `kind: verification`

- reviewer_run: 2e604b63-8de9-4abf-b104-de9dcc226595
- reviewer_session: 49575938-6bd1-442e-9ea6-d83809f54fa8
- verdict: needs_review
- findings:
- PR14-F01 / blocking / acceptance 1.1.18 requires running `analyzer_turns_from_transcript`, which §1.3 creates and which §1.3 gates behind `(depends: 1.1)`, so the §1.1 leaf cannot close on its own criteria (fixer-induced by PR11-F06).
- PR14-F02 / blocking / the round-13 `withheld_capture` is written only on a withheld resolution, so a withheld→complete→failure retry sequence persists the stale incomplete pair and stamps `tail_withheld: true` after the tail resolved (fixer-induced by PR13-F04).
- votes: unattended coordinator judgment under the user's standing direction ("continue for 10 more rounds or until you reach convergence, unattended, use your best judgment on the findings, keep scope creep to a minimum"). PR14-F01 accept — verified in the artifact and the repository: `analyzer_turns_from_transcript` is defined in §1.3's new `src/gobby/sessions/analyzer_turns.py` (the plan's own §1.3 pseudocode carries its `def`), `gcode grep -w analyzer_turns_from_transcript src/ tests/` returns nothing today, and §1.3's heading reads `(depends: 1.1)`, so 1.1.18 as written asks the §1.1 leaf to exercise a symbol only its dependent creates. The split follows the finding: §1.1 keeps the state-isolation claim over `extract_last_messages(..., include_tool_activity=True)` — hydrated-versus-control private state, the continued `iter_parse_events` equality, ledger output equal to a fresh parser's, and the twice-called `_extract_digest_pairs` pair counts, all reachable with symbols that exist today — and the `analyzer_turns_from_transcript` half becomes 1.3.17 with its own test in the already-targeted `tests/sessions/test_sessions_analyzer.py`, where 1.3.10 already exercises that symbol. The §1.3 → §1.1 dependency is unchanged, and 1.3.17 asserts the composed scan (ledger first, adapter second) so the ordering the old item covered is not lost. PR14-F02 accept — the contradiction is real and mine: §1.4 states "each later attempt overwrites it" while §1.2 writes the capture only "immediately after `_resolve_undigested_pairs` returns a **withheld** resolution" and adds "written once per call and never cleared by a later failure", so a first attempt that withholds, a tail that then completes, and a second attempt that resolves completely and then fails leaves the capture describing attempt 1; §1.4's fallback rule (`tail_withheld=True, withheld_pair=<that pair>` on every non-corruption terminal failure) would then persist the superseded incomplete pair under a watermark asserting a withhold that no longer holds. The repair takes the finding's shape with less mechanism than a second structure: the existing single dict stays, and the write loses its condition rather than gaining one — `_build_turn_and_digest_serialized` writes `{"tail_withheld": <the resolution's own flag>, "withheld_pair": <the trailing pair as a plain dict>}` after **every** resolution, at the same point before the LLM call and persistence, so the last attempt to reach resolution always owns the capture and §1.4's overwrite claim becomes literally true. The second half of the finding is the coupling inside `_persist_compact_handoff_fallback`, where `tail_withheld=True` both selected the pair-based rendering and stamped the metadata watermark: those separate, so rendering keys on `withheld_pair is not None` and the watermark carries the captured boolean, which lets a completed pair be rendered under `"tail_withheld": False`. The returned-dict contract is untouched — a non-withheld return still carries neither key, which is what 1.4 branches on — because the capture is the out-of-band channel and widening the return would change the branch. 1.2.9 gains the withheld→complete overwrite, and 1.4.17 gains the withheld→complete→failure sequence across the returned error, cancellation, raised exception, timeout, and persistence-failure branches the finding enumerates.
- resolution_notes: §1.1: 1.1.18 is now the `extract_last_messages` state-isolation item alone (hydrated-versus-control private state, continued `iter_parse_events` parity, fresh-parser ledger equality, twice-called `_extract_digest_pairs` pair counts), with every `analyzer_turns_from_transcript` assertion removed. §1.3: new 1.3.17 owns the adapter half — the composed ledger-then-adapter scan over a hydrated parser leaves `snapshot_state()` and every private field equal to the control, continued parsing stays identical, and the adapter output equals a fresh parser's — tested in `tests/sessions/test_sessions_analyzer.py`, already a §1.3 Target. §1.2: the capture write loses its withheld-only condition and records the resolution's own `tail_withheld` flag with the trailing pair on every attempt, so each retry overwrites the prior attempt's evidence; the "written once per call" sentence is replaced by the overwrite rule, and 1.2.9 pins a withheld attempt followed by a complete one whose LLM call then fails. §1.4: the fallback consumes the capture's own flag instead of forcing `True`, `_persist_compact_handoff_fallback` renders from `withheld_pair` whenever one is present and stamps the `"tail_withheld"` watermark from the separate boolean, and 1.4.17 adds the withheld→complete→failure sequence over the returned-error, cancellation, raised-exception, timeout, and persistence-failure branches, asserting the complete pair and `metadata_json["tail_withheld"] is False`. No finding was declined.

```json plan-review-round
{"evidence_id":"ecab4278-2e8c-41d1-bbc5-ceb812cbf68c","plan_hash":"26378524e1189eebb15765d4aac9b967af7771513d3cf0b6dd8a295eb029004f","round_number":14,"round_result":{"coverage_attestation":{"adjacent_variant_complete":true,"attestation_digest":"ec85852b597dd93646b8906479c62c4f71372202a03a6a19af054637d04005cc","cross_lane_interaction_complete":true,"disposition_counts":{"dismissed":2,"emitted_findings":2,"total":4},"evidence_id":"ecab4278-2e8c-41d1-bbc5-ceb812cbf68c","lanes":[{"candidate_count":3,"lane_id":"requirements_traceability","status":"completed"},{"candidate_count":0,"lane_id":"repository_blast_radius","status":"completed"},{"candidate_count":1,"lane_id":"runtime_invariants","status":"completed"}],"shadow_manifest_status":{"entry_count":8,"manifest_digest":"ad053ea80cac12e1ed86aebc4046b88f2885d4a74d07295a4761efb08ca41ca7","status":"valid"},"source_digest":"c45c03a7e354c21cfb75556ed8103a138b4f522a91f539fe74b9e400fe1f8db1","version":1},"findings":[{"category":"bad-sequencing","causal_finding_id":"PR11-F06","causal_section_ids":["1.1","1.3"],"check_key":"leaf-close-dependency-order","description":"Acceptance 1.1.18 requires running `analyzer_turns_from_transcript`, but §1.3 creates that symbol and is blocked by `(depends: 1.1)`. The §1.1 leaf therefore cannot satisfy its own close criteria before its dependent is eligible.","finding_id":"PR14-F01","fix":"Split acceptance 1.1.18: keep the `extract_last_messages(..., include_tool_activity=True)` fresh-parser and state-isolation assertions in §1.1, and move every `analyzer_turns_from_transcript` assertion plus its test ownership into a new §1.3 acceptance item. Preserve the existing §1.3 → §1.1 dependency.","introduced_in_round":11,"location":"P1 / §1.1 acceptance 1.1.18 and §1.3 dependency","prevention":"For every acceptance item that names a symbol, verify the symbol is owned by that section or by an already-completed dependency; split cross-leaf tests at ownership boundaries.","principle":"Every dependency-gated leaf must be independently closable using only artifacts supplied by its prerequisites.","root_cause":"The PR11-F06 repair put one validation item across both observational consumers even though `analyzer_turns_from_transcript` is owned by the downstream leaf.","section_id":"1.1","severity":"blocking"},{"category":"unhandled-edge","causal_finding_id":"PR13-F04","causal_section_ids":["1.2","1.4"],"check_key":"edge-case-coverage","description":"If attempt 1 captures an incomplete withheld pair, the tail completes, and attempt 2 resolves the complete pair but then fails, the shared `withheld_capture` still describes attempt 1. §1.4 then persists stale evidence, omits the newly completed result, and records `tail_withheld: true` after the tail actually resolved, contradicting the claim that each retry overwrites the capture.","finding_id":"PR14-F02","fix":"Replace the withhold-only dict with an attempt-evidence capture updated immediately after every successful resolution. A non-withheld retry must overwrite the prior pair with the complete compact-triggering `DigestPair` and `tail_withheld: false` before the LLM call or persistence. Let fallback rendering consume the captured pair independently of the watermark, and add withheld→complete→failure regressions for returned error, cancellation, raised exception, timeout, and persistence failure.","introduced_in_round":13,"location":"P1 / §§1.2 and 1.4 withheld-tail retry transition","prevention":"Test every retry state transition—especially withheld→complete—followed by returned error, cancellation, raised exception, timeout, and persistence failure; assert the fallback pair and watermark come from the latest attempt.","principle":"Retry-scoped evidence must describe the latest resolved attempt before any downstream failure path can persist a fallback.","root_cause":"The Round 13 capture is written only when `_resolve_undigested_pairs` returns `tail_withheld=True`; a later non-withheld resolution never overwrites the shared dict.","section_id":"1.4","severity":"blocking"}],"reviewer_session":"49575938-6bd1-442e-9ea6-d83809f54fa8","round":14,"round_number":14,"verdict":"needs_review"},"session_id":"6bc09ed1-7072-423d-a89f-1c7881928c8b"}
```

**Round 15** `kind: verification`

- reviewer_run: 68d025d4-7029-4dfb-bafb-3b155b93ff9a
- reviewer_session: ee299ef8-c2b9-4625-8d35-95c32ced0d5f
- verdict: needs_review
- findings:
- PR15-F01 / blocking / unhandled-edge / §1.2 (`edge-case-coverage`) — the round-14 capture contract requires the trailing `DigestPair` after every resolution, but `UndigestedBatch` and `ResolvedPairs` carry a full `DigestPair` only for a withheld resolution; a complete batch retains only composed `(prompt, text)` pairs, so the withheld→complete→failure fallback cannot render B's prompt, ledger, and narration as separate sections. Fixer-induced by PR14-F02 (introduced in round 14), causal sections 1.2 and 1.4.
- votes: unattended coordinator judgment under the user's standing direction ("continue for 10 more rounds or until convergence, unattended, use your best judgment, keep scope creep to a minimum"). PR15-F01 accept — verified in the artifact: §1.2 defines `UndigestedBatch.pairs` as `list[tuple[str, str]]` whose text is `response` and `activity` joined by one blank line and stripped, and `withheld_pair` as the trailing `DigestPair` "whenever `tail_withheld` is true, else `None`", while §1.4's renderer splits `prompt`, `activity`, and `response` into three sections and 1.4.17 asserts B's ledger survives the failure; a composed two-tuple cannot supply that, so the round-14 write was unsatisfiable on the complete-resolution branch it was added for. Repair narrowed from the adversary's proposal: instead of adding a second pair field beside `withheld_pair`, the existing field generalizes to `tail_pair: DigestPair | None` on both NamedTuples, populated with the trailing extracted pair for every non-empty batch. The public returned-dict keys and §1.4's keyword stay `withheld_pair`, so the outcome contract, the fallback renderer, and the coverage watermark are untouched and only the producer side changes.
- resolution_notes: §1.2 — `UndigestedBatch` and `ResolvedPairs` rename `withheld_pair` to `tail_pair` and populate it for every non-empty batch: the withheld trailing pair when the tail is withheld, the last complete extracted `DigestPair` otherwise, `None` only for a batch with no undigested pairs; the withheld skip branches return `ResolvedPairs([], "", batch.next_pair_index, True, batch.tail_pair)`; the withheld outcome dict and the `withheld_capture` write both read `tail_pair`, so a complete resolution captures a three-field pair with `response` and `activity` intact. Public keys (`tail_withheld`, `withheld_pair`), the `_persist_compact_handoff_fallback` and `_compact_handoff_transcript_tail_markdown` signatures, and the returned-dict contract are unchanged. Acceptance 1.2.9 now pins the complete-resolution capture to separate `response` and `activity` values equal to the extraction (never the composed text), and 1.4.17's withheld→complete→failure case asserts B's ledger renders under `## Tool activity (in flight)` with B's narration under `## Narration so far`. The first round-15 attempt (`ad15bcdd-5e93-4642-8792-426f08660968`) stalled with no payload and left the plan untouched; its evidence was expired and the round re-prepared against an identical plan hash, so it never counted toward the cap.

```json plan-review-round
{"evidence_id":"fb059485-52ce-4193-807d-284e382e9095","plan_hash":"f1472003292d8d206c92929dabc8f9299af0fd29f9d667c77fce7a2ec07e175f","round_number":15,"round_result":{"coverage_attestation":{"adjacent_variant_complete":true,"attestation_digest":"d69bccf213042dd50478f1b5f85d0e432c0f24707bfb6732c448dad8321c3d3d","cross_lane_interaction_complete":true,"disposition_counts":{"dismissed":0,"emitted_findings":1,"total":1},"evidence_id":"fb059485-52ce-4193-807d-284e382e9095","lanes":[{"candidate_count":0,"lane_id":"requirements_traceability","status":"completed"},{"candidate_count":0,"lane_id":"repository_blast_radius","status":"completed"},{"candidate_count":1,"lane_id":"runtime_invariants","status":"completed"}],"shadow_manifest_status":{"entry_count":8,"manifest_digest":"6e36ce01a37f1319e1a89331857af2c0f5873c5216a6d270fe530c984db3cd30","status":"valid"},"source_digest":"48ac4404b87191303ef1d759fa73fe9279a8a51359260e3f55bb1fbf8b9c72e8","version":1},"findings":[{"category":"unhandled-edge","causal_finding_id":"PR14-F02","causal_section_ids":["1.2","1.4"],"check_key":"edge-case-coverage","description":"On the withheld→complete→failure path, `_build_turn_and_digest_serialized` cannot write the complete pair B required by the repaired contract. For a non-withheld batch, both `withheld_pair` fields are `None`, and `ResolvedPairs.pairs` contains only `(prompt, composed_text)` after narration and activity were joined. The fallback therefore cannot preserve B's separate `response` and `activity` fields as §1.4.17 requires.","finding_id":"PR15-F01","fix":"Carry the trailing source pair independently of the watermark—for example, add `tail_pair: DigestPair | None` to `UndigestedBatch` and `ResolvedPairs`, populated for every non-empty batch. Write `withheld_capture` from that field after every resolution, keep non-withheld returned results free of the two public keys, and extend 1.2.9/1.4.17 to prove the completed retry preserves `response` and `activity` separately through fallback rendering.","introduced_in_round":14,"location":"P1 / §§1.2 and 1.4 latest-resolution capture","prevention":"For every retry-evidence field, trace its full typed value through each producer and return boundary, then test both transition directions before specifying fallback consumers.","principle":"Every recovery payload must remain representable across each intermediate data shape that carries it.","root_cause":"Round 14 required the capture to retain the trailing DigestPair after every resolution, while UndigestedBatch and ResolvedPairs retain a full DigestPair only for withheld resolutions; a complete resolution collapses response and activity into a two-field composed pair.","section_id":"1.2","severity":"blocking"}],"reviewer_session":"ee299ef8-c2b9-4625-8d35-95c32ced0d5f","round":15,"round_number":15,"verdict":"needs_review"},"session_id":"6bc09ed1-7072-423d-a89f-1c7881928c8b"}
```

**Human handoff** `kind: verification`

- review_cap: 15 finalized adversary rounds (rounds 1-15); the cap is reached at round 15's `needs_review`
- final_round: 15 - one blocking finding (PR15-F01), accepted, repaired, and base-validated
- outstanding_findings: none - every finding of every round is either accepted and repaired or declined with its rationale recorded in that round's votes
- resolution_notes: The adversarial loop ends here under the cap rule and the user's explicit convergence direction ("we can converge here"), and no further adversary round is launched. The artifact carries the round-15 repair (`tail_pair` on `UndigestedBatch` and `ResolvedPairs` in 1.2, with acceptance 1.2.9 and 1.4.17 extended) and passes `uv run gobby plans validate`. Any continuation to expansion runs through the explicit human-handoff route - `derive_plan_handoff_manifest`, `apply_plan_handoff_manifest`, expansion-mode validation, then `gobby build --planning-seed-state approved --completed-plan-review-rounds 15` - which manufactures no adversary verdict, coverage attestation, or review evidence.

## Task Mapping
`kind: framing`

<!-- Updated after task creation -->
| Plan Item | Task Ref | Status |
|-----------|----------|--------|

## M1 Task Manifest
`kind: manifest`

```yaml
- title: Add a tool-activity ledger to transcript pair extraction
  category: code
  task_type: feature
  depends_on: []
  validation_criteria: "1.1.1: `tool_activity.py` exposes `canonical_tool_name`, `format_tool_activity_line`,\
    \ and `render_tool_activity` with the caps and collapsing described above. file:\
    \ `src/gobby/sessions/transcripts/tool_activity.py`.\n1.1.2: Every parser accepts\
    \ `include_tool_activity` and returns identical message counts, role sequences,\
    \ and `content` strings with the flag on and off across all fixtures; the ledger\
    \ appears only as `tool_activity` on user messages. test: `tests/sessions/test_transcript_parsers.py::test_tool_activity_flag_preserves_pair_shape`.\n\
    1.1.3: Grok segments carry a ledger naming `search_replace` paths, `mcp gobby-tasks:claim_task`,\
    \ and a failed canonical `Bash` entry (`- Bash <command> ! failed: \u2026`) derived\
    \ from a native `run_terminal_command` record \u2014 the native name never appears\
    \ in a rendered line \u2014 from the `grok_audit` fixtures. test: `tests/sessions/transcripts/test_grok_parser.py::test_extract_last_messages_tool_activity_ledger`.\n\
    1.1.4: A tool-only turn (user prompt followed only by tool-use/tool-result records)\
    \ yields the same messages as today \u2014 including Grok's empty assistant sentinel,\
    \ asserted with exact flag-off/flag-on role and content arrays and unchanged digest\
    \ cursor movement on a Grok tool-only fixture \u2014 with its ledger on that turn's\
    \ user message and never on the previous turn. test: `tests/sessions/test_transcript_parsers.py::test_tool_only_turn_ledger_stays_on_its_user_message`.\n\
    1.1.5: `canonical_tool_name(\"run_terminal_command\", {\"command\": \"git status\"\
    })` returns the canonical `Bash` name with the command input, so the ledger line\
    \ and `commit_outcome` treat it as a shell call, while `is_shell_tool(\"run_terminal_command\"\
    )` and `canonicalize_shell_tool_name(\"run_terminal_command\")` are unchanged\
    \ from today (the alias is ledger-local). test: `tests/sessions/transcripts/test_tool_activity.py::test_grok_terminal_alias_is_ledger_local`.\n\
    1.1.6: Truncation keeps failed calls, first-per-path edits, task mutations, commit-producing\
    \ calls, and the last ten calls under both caps for a 120-entry list, and the\
    \ omission marker counts dropped underlying calls \u2014 a dropped collapsed `(xN)`\
    \ group counts N. test: `tests/sessions/transcripts/test_tool_activity.py::test_render_tool_activity_truncation_keeps_evidence`.\n\
    1.1.7: Commit-producing calls carry `\u2192 commit <sha>` from the correlated\
    \ successful result or `commit_sha` argument, and no other call retains successful\
    \ output: `is_commit_producing` is true for `git -C /repo commit -m msg` and `cd\
    \ x && git commit -am msg` and false for `echo \"git commit\"`, `grep -n \"git\
    \ commit\" notes.md`, and `gcode grep -F \"git commit\" src`, whose successful\
    \ output (including a forged `[main abc1234] msg` line) never reaches the ledger,\
    \ the analyzer adapter, or a prompt; `ls # git commit` is not commit-producing\
    \ even when its stdout carries a forged `[main abc1234] msg` line, so no `outcome`\
    \ is set and the output is discarded unread. test: `tests/sessions/transcripts/test_tool_activity.py::test_commit_outcome_from_shell_and_task_tools`.\n\
    1.1.8: Control characters in any field are escaped before truncation; a multiline\
    \ command renders as one physical line, and the caps and omission count are computed\
    \ on the escaped text. test: `tests/sessions/transcripts/test_tool_activity.py::test_ledger_escapes_control_characters_before_caps`.\n\
    1.1.9: `canonical_tool_name` decodes every wrapper shape `canonicalize_call_tool_wrapper`\
    \ accepts (top-level, hoisted, `args` alias, JSON string, nested) and keeps `task_id`,\
    \ `title`, and `commit_sha`. test: `tests/sessions/transcripts/test_tool_activity.py::test_canonical_tool_name_matches_call_tool_wrapper_shapes`.\n\
    1.1.10: A Codex window without item records puts the execution-chain's inner `cmd`\
    \ command and its failure text in the ledger; a window with `item_completed` tool\
    \ items takes `McpToolCall` (canonical `mcp server:tool` line with task args),\
    \ `CommandExecution` (canonical `normalize_command_execution` command, failure\
    \ from nonzero `exit_code`), and `FileChange` (one `apply_patch <path>` entry\
    \ per `changes` key) entries from the `codex_item_activity` pre-scan, attributed\
    \ to their originating user-to-user turns via `record_index`, with per-call wrapper\
    \ suppression; an `McpToolCall` item whose `status` is `completed` and whose `result`\
    \ carries `{\"success\": false, \"error\": \u2026}` \u2014 as a dict, as an `{\"\
    Ok\": <json string>}` payload, and as a `structuredContent` envelope \u2014 renders\
    \ `! failed:` with that error text in all three shapes, an `{\"Err\": \u2026}`\
    \ result renders failed likewise, and a completed item with a successful result\
    \ or no `result` key renders bare \u2014 while `iter_parse_events` output, `parsed_index`\
    \ assignment, and resume boundaries are byte-identical before and after this leaf.\
    \ test: `tests/sessions/test_transcript_parsers.py::test_codex_item_stream_precedence_in_ledger`.\n\
    1.1.11: Qwen `functionResponse` results with `toolCallResult.status` `error`/`cancelled`\
    \ or an `error` response key annotate their entry as failed, correlated by `id`\
    \ or `callId`. test: `tests/sessions/transcripts/test_qwen_transcript_parser.py::test_qwen_failed_function_response_in_ledger`.\n\
    1.1.12: Malformed wrapper input (unparseable JSON-string `arguments`, missing\
    \ routing fields) raises `CallToolWrapperInputError` inside `canonical_tool_name`,\
    \ which keeps the raw dispatcher name with empty input and still renders a ledger\
    \ line instead of breaking the digest. test: `tests/sessions/transcripts/test_tool_activity.py::test_canonical_tool_name_malformed_wrapper_falls_back`.\n\
    1.1.13: `canonical_tool_name` is total over nullable parser output: a `tool_use`\
    \ with `tool_name=None` (Qwen null `functionCall.name`), a non-string name, and\
    \ a non-mapping `tool_input` each normalise to the `\"unknown-tool\"` label with\
    \ empty input and render a ledger line, for malformed native Qwen, Droid, and\
    \ Codex records. test: `tests/sessions/transcripts/test_tool_activity.py::test_canonical_tool_name_total_over_nullable_parser_output`.\n\
    1.1.14: The three call outcomes are distinguishable on every provider: a matched\
    \ successful call renders a bare line, a failed call renders `! failed:`, and\
    \ a call with no matching result record renders `(no result recorded)` \u2014\
    \ proven with a successful test-command line, a failed call, and an in-flight\
    \ final call in one turn for all five parsers. test: `tests/sessions/transcripts/test_tool_activity.py::test_ledger_distinguishes_success_failure_and_missing_result`.\n\
    1.1.15: Per-call Codex precedence on a mixed window: a turn whose window holds\
    \ an item-covered call and a wrapper-only call keeps both (item entry plus execution-chain\
    \ entry, no double count, no drop); a split tail whose wrapper has neither `custom_tool_call_output`\
    \ nor item in the window keeps the wrapper derivation projected through `pending_exec_command`\
    \ \u2014 a `custom_tool_call` `exec` wrapper whose JS names `tail -f /var/log/widget.log`\
    \ renders that command with `(no result recorded)`, never the raw JS; a window\
    \ cut between a landed `CommandExecution` item and the wrapper's output renders\
    \ that command exactly once; and a pending wrapper whose JS holds two `exec_command`\
    \ literals keeps the raw `exec` name and input; item entries land on their originating\
    \ turn's ledger, never a neighbor's. test: `tests/sessions/test_transcript_parsers.py::test_codex_mixed_window_and_split_tail_precedence`.\n\
    1.1.16: `TranscriptReadError` is defined in `transcripts/base.py` with `path`,\
    \ `byte_offset`, and `line_number`, and it is the exact class raised by both 1.2's\
    \ `_read_undigested_turns` (global 1-based `line_number` and the record's `byte_offset`)\
    \ and 1.3's `_read_transcript_window` (`line_number is None`, `byte_offset` equal\
    \ to the malformed record's file offset, proven on a window that omits a prefix)\
    \ for a malformed interior line, for a line at any position \u2014 the final line\
    \ included \u2014 that decodes to a bare JSON scalar or list, for a malformed\
    \ final line that the file terminates with `\\n`, and for a line whose raw bytes\
    \ are not valid UTF-8 in an interior position or in a newline-terminated final\
    \ record (the offset names that record); a malformed final line with no trailing\
    \ newline, and an unterminated final fragment ending in a split multibyte code\
    \ point, each raise nothing in either reader and take the withhold path instead.\
    \ The same transcript is classified identically by both readers in all six forms,\
    \ fed as identical raw bytes. test: `tests/sessions/test_transcript_read_error.py::test_transcript_read_error_shared_by_digest_and_summary_readers`.\n\
    1.1.17: `normalize_command_execution` and `_command_execution_outcomes` agree\
    \ byte-for-byte on `[/bin/zsh, -lc, cmd]` (command is `cmd`), on a multi-part\
    \ argv without a shell wrapper (`shlex.join`), and on a nonzero `exit_code` with\
    \ no `status` key (entry failed, never bare); a mixed window holding two identical\
    \ `uv run pytest -k widget` wrappers and one landed `CommandExecution` item renders\
    \ exactly two ledger lines \u2014 one item-derived, one execution-chain \u2014\
    \ in `codex_item_activity` and in the 1.3 adapter. test: `tests/sessions/test_transcript_parsers.py::test_codex_item_canonicalization_matches_exec_adapter`.\n\
    1.1.18: For Codex, Qwen, and Droid, a parser hydrated with non-empty private state\
    \ (Codex: a pending execution-chain wrapper and a pending tool-search id; Qwen:\
    \ a `_last_tool_use_id`; Droid: a `_last_assistant_index` and sidecar usage) that\
    \ runs `extract_last_messages(..., include_tool_activity=True)` over a tool-heavy\
    \ window has `snapshot_state()` and every private field equal to an untouched\
    \ control parser hydrated identically, and continuing `iter_parse_events` over\
    \ the following records on both yields identical records, `parsed_index` values,\
    \ and `codex_exec_outcomes`; the ledger equals a fresh parser's; and `_extract_digest_pairs`\
    \ called twice on one Codex parser (segment, then prefix) returns the same pair\
    \ counts as two fresh parsers. Every symbol this item exercises exists before\
    \ this leaf begins, so \xA71.1 closes on its own criteria; the identical claim\
    \ for 1.3's adapter is 1.3.17, owned by the leaf that creates it. test: `tests/sessions/transcripts/test_tool_activity.py::test_observational_scans_leave_parser_state_untouched`."
  labels:
  - covers:compact-summary-fidelity:1.1:1.1.1
  - covers:compact-summary-fidelity:1.1:1.1.2
  - covers:compact-summary-fidelity:1.1:1.1.3
  - covers:compact-summary-fidelity:1.1:1.1.4
  - covers:compact-summary-fidelity:1.1:1.1.5
  - covers:compact-summary-fidelity:1.1:1.1.6
  - covers:compact-summary-fidelity:1.1:1.1.7
  - covers:compact-summary-fidelity:1.1:1.1.8
  - covers:compact-summary-fidelity:1.1:1.1.9
  - covers:compact-summary-fidelity:1.1:1.1.10
  - covers:compact-summary-fidelity:1.1:1.1.11
  - covers:compact-summary-fidelity:1.1:1.1.12
  - covers:compact-summary-fidelity:1.1:1.1.13
  - covers:compact-summary-fidelity:1.1:1.1.14
  - covers:compact-summary-fidelity:1.1:1.1.15
  - covers:compact-summary-fidelity:1.1:1.1.16
  - covers:compact-summary-fidelity:1.1:1.1.17
  - covers:compact-summary-fidelity:1.1:1.1.18
  tdd: true
  source_section: '1.1'
  implementation_domain: backend
- title: Feed digest pairs with the ledger and teach the turn-record prompt
  category: code
  task_type: feature
  depends_on:
  - '1.1'
  validation_criteria: "1.2.1: Digest pairs contain the ledger for tool-heavy turns\
    \ while `_read_last_turn_from_transcript` output is unchanged. test: `tests/memory/test_digest.py::test_extract_digest_pairs_includes_tool_activity`.\n\
    1.2.2: The inline fallback prompt and the bundled prompt both carry the ledger\
    \ instruction and the bundled manifest checksum is updated. file: `src/gobby/install/shared/prompts/memory/turn_record.md`.\n\
    1.2.3: Replaying the Grok audit fixture through `_extract_digest_pairs` yields\
    \ a pair whose activity names `search_replace` and `mcp gobby-tasks:claim_task`.\
    \ symbol: `_extract_digest_pairs`.\n1.2.4: After `sync_bundled_prompts` into an\
    \ isolated database, the live `memory/turn_record` row carries the ledger instruction\
    \ and its `required_variables` are unchanged. test: `tests/prompts/test_prompt_sync.py::test_turn_record_sync_carries_ledger_instruction`.\n\
    1.2.5: With one pair already digested, a following user prompt with only tool\
    \ records digests as a second pair whose text is that turn's ledger, the first\
    \ pair's text is unchanged, the cursor advances by exactly one, and in catch-up\
    \ mode the trailing tool-only pair is left undigested. test: `tests/memory/test_digest.py::test_tool_only_turn_ledger_stays_on_current_pair`.\n\
    1.2.6: The Grok coverage-audit helper consumes `DigestPair` by named attribute\
    \ and the four `test_grok_parser.py` equality assertions compare `DigestPair`\
    \ values; no two-field `_extract_digest_pairs` expectation remains under `tests/`,\
    \ and the coverage/completeness metrics for the audit fixtures are unchanged.\
    \ file: `tests/sessions/transcripts/test_grok_parser.py`.\n1.2.7: With a partial\
    \ trailing JSONL line that stabilizes on the bounded re-read, the digest proceeds\
    \ normally; with a line still partial after the retry, the digest processes the\
    \ complete prefix, withholds the trailing pair, does not advance the cursor past\
    \ it, and reports `tail_withheld: True`; a malformed interior line yields `{\"\
    error\": \u2026, \"error_kind\": \"transcript_read\"}` instead of an empty batch,\
    \ and so do a final line holding a bare JSON scalar or list, a malformed final\
    \ line the file already terminates with `\\n`, a malformed unterminated tail that\
    \ the bounded re-read finds newline-terminated and still malformed, and invalid\
    \ UTF-8 bytes in an interior line or in a newline-terminated final record (no\
    \ withhold in any of the six); an unterminated final fragment cut inside a multibyte\
    \ code point withholds exactly like a partial JSON line and is included once the\
    \ remaining bytes land. test: `tests/memory/test_digest.py::test_partial_transcript_tail_withholds_trailing_pair`.\n\
    1.2.8: A partial final tool-result record that completes after a withheld digest\
    \ is included when the next digest runs: the enriched pair's ledger carries the\
    \ completed call, the cursor advances only then, and exact cursor movement is\
    \ asserted at every step. test: `tests/memory/test_digest.py::test_completed_tail_record_reaches_ledger_after_withhold`.\n\
    1.2.9: With a persistently partial trailing line, the public `build_turn_and_digest`\
    \ result carries `tail_withheld: True` both when a complete prefix was digested\
    \ (beside `turn_num`) and when the withheld pair was the only undigested content\
    \ (`{\"tail_withheld\": True}`, no persistence, no LLM call); `_resolve_undigested_pairs`\
    \ returns `ResolvedPairs` with empty `pairs` and `tail_withheld=True` on its catch-up\
    \ and duplicate-hash skips for a withheld batch; every withheld outcome also carries\
    \ `withheld_pair` whose `prompt` is the trailing pair's prompt and whose `activity`\
    \ lists every complete call of that turn with `(no result recorded)` for the in-flight\
    \ one; and a run without a withhold carries neither key. With a withheld batch\
    \ whose complete prefix is digested and the LLM call then failing, cancelled,\
    \ or raising, and separately with `persist_digest_state` raising, the caller's\
    \ `withheld_capture` dict holds `tail_withheld: True` and the exact `withheld_pair`\
    \ in every case (written before the LLM call), and each returned `{\"error\":\
    \ \u2026}`/`{\"cancelled\": \u2026}` result carries both keys too. A run without\
    \ a withhold leaves the same capture holding `tail_withheld: False` beside the\
    \ trailing complete pair, whose `prompt`, `response`, and `activity` equal the\
    \ extracted `DigestPair` field for field and never the blank-line-joined text\
    \ of `pairs[-1]`, while its *returned* result carries neither key, and two calls\
    \ sharing one capture dict prove the overwrite in both directions: a withheld\
    \ first call followed by a second whose tail has completed and whose LLM call\
    \ then raises leaves `tail_withheld: False` and the complete pair (never the withheld\
    \ one), and a complete first call followed by a withheld second leaves `tail_withheld:\
    \ True` and the withheld pair; a call that raises inside `_read_undigested_turns`\
    \ before resolution leaves the prior call's capture untouched. test: `tests/memory/test_digest.py::test_tail_withheld_propagates_to_public_outcome`.\n\
    1.2.10: With `persist_digest_state` blocked on an event inside its worker thread\
    \ and the digest task cancelled while it awaits that write, the per-session lock\
    \ stays held until the event releases the worker, the digest state is fully persisted\
    \ (markdown and cursor), and a concurrent `build_turn_and_digest` for the same\
    \ session started during the cancellation observes the advanced cursor and digests\
    \ nothing twice; a cancellation delivered during the LLM call persists nothing.\
    \ test: `tests/memory/test_digest.py::test_cancelled_digest_holds_lock_through_persistence`.\n\
    1.2.11: With a complete prefix digested past the summary watermark and the trailing\
    \ pair withheld, `memory_manager.schedule_background_task` (spied) receives no\
    \ `session-summary-refresh-*` task; the next digest that covers the pair schedules\
    \ it. test: `tests/memory/test_digest.py::test_withheld_tail_suppresses_summary_refresh_scheduling`.\n\
    1.2.12: The lifecycle backlog sweep stops the current per-session batch loop after\
    \ any tail_withheld outcome, including one that also persisted a complete prefix,\
    \ without spending another bounded attempt on the same in-flight trailing pair;\
    \ ordinary progress and error termination remain unchanged. test: `tests/sessions/test_sessions_lifecycle.py::TestDigestBacklogSweep.test_sweep_stops_session_on_tail_withheld`.\n\
    1.2.13: With `persist_digest_state` blocked on an event inside its worker thread\
    \ and the digest task cancelled twice \u2014 once to enter the barrier and again\
    \ while the barrier awaits the persistence future \u2014 the per-session lock\
    \ stays held until the event releases the worker, the digest state is fully persisted,\
    \ a concurrent same-session `build_turn_and_digest` started during the cancellations\
    \ observes the advanced cursor, and the task finishes with `CancelledError`; with\
    \ the worker raising instead of completing, the exception is logged with the session\
    \ id and the cancellation still propagates. test: `tests/memory/test_digest.py::test_repeated_cancellation_holds_lock_until_persistence_settles`."
  labels:
  - covers:compact-summary-fidelity:1.2:1.2.1
  - covers:compact-summary-fidelity:1.2:1.2.2
  - covers:compact-summary-fidelity:1.2:1.2.3
  - covers:compact-summary-fidelity:1.2:1.2.4
  - covers:compact-summary-fidelity:1.2:1.2.5
  - covers:compact-summary-fidelity:1.2:1.2.6
  - covers:compact-summary-fidelity:1.2:1.2.7
  - covers:compact-summary-fidelity:1.2:1.2.8
  - covers:compact-summary-fidelity:1.2:1.2.9
  - covers:compact-summary-fidelity:1.2:1.2.10
  - covers:compact-summary-fidelity:1.2:1.2.11
  - covers:compact-summary-fidelity:1.2:1.2.12
  - covers:compact-summary-fidelity:1.2:1.2.13
  tdd: true
  source_section: '1.2'
  implementation_domain: backend
- title: Ground summaries in transcript-derived structured data when a digest exists
  category: code
  task_type: feature
  depends_on:
  - '1.1'
  validation_criteria: "1.3.1: With a digest present and a transcript on disk, the\
    \ summary prompt context has non-empty `structured_context` and `file_changes`\
    \ for Claude, Grok, and Codex fixtures. test: `tests/sessions/test_summarize.py::test_summary_ground_truth_with_digest_present`.\n\
    1.3.2: The adapter converts Grok `tool_call` and Codex `function_call` records\
    \ into `tool_use` blocks the analyzer consumes. test: `tests/sessions/test_sessions_analyzer.py::test_analyzer_turns_from_grok_and_codex_transcripts`.\n\
    1.3.3: `search_replace` and `gobby__call_tool` task operations populate `files_modified`\
    \ and `task_progress`. symbol: `TranscriptAnalyzer._analyze_tool_use`.\n1.3.4:\
    \ Shell `git commit` results and `close_task`/`link_commit` `commit_sha` arguments\
    \ populate `git_commits` with real hashes; unmatched commits keep an empty hash.\
    \ test: `tests/sessions/test_sessions_analyzer.py::test_git_commits_carry_hashes_from_results_and_task_tools`.\n\
    1.3.5: A Qwen record whose `parts` mix text, `functionCall`, and `functionResponse`,\
    \ and a Droid record mixing text and `tool_use`, reach the analyzer with every\
    \ block. test: `tests/sessions/test_sessions_analyzer.py::test_adapter_consumes_every_block_of_multi_part_records`.\n\
    1.3.6: Codex activity reaches the analyzer in both envelopes: a window without\
    \ item records delivers execution-chain shell `tool_use` blocks carrying the nested\
    \ outcome's command, and a window with `item_completed` records delivers `McpToolCall`/`CommandExecution`/`FileChange`\
    \ blocks with per-call wrapper suppression \u2014 a mixed window keeps the unmatched\
    \ wrapper's execution-chain blocks while item-covered calls appear exactly once\
    \ \u2014 so edited paths (from `FileChange` `changes` keys), task operations,\
    \ and commit SHAs populate `files_modified`, `task_progress`, and `git_commits`\
    \ without double counting or dropped calls; an `McpToolCall` item with `status:\
    \ completed` and a structured `{\"success\": false}` result reaches the analyzer\
    \ as a failed `tool_result` block carrying its error text, so a failed task operation\
    \ is never counted as completed `task_progress`. test: `tests/sessions/test_sessions_analyzer.py::test_codex_nested_exec_outcomes_reach_analyzer`.\n\
    1.3.7: A 50,000-record transcript whose true first prompt and tail-window first\
    \ prompt are distinct \u2014 with 1,000 non-user records (system, metadata, injected-context)\
    \ ahead of that first prompt \u2014 feeds the analyzer at most `SUMMARY_ANALYZER_MAX_RECORDS`\
    \ records (observed through a counting parser), the tail facts still reach `structured_context`,\
    \ and `initial_goal` is the true first prompt, never the tail's. test: `tests/sessions/test_summarize.py::test_summary_ground_truth_window_is_bounded`.\n\
    1.3.8: `recent_activity` renders wrapped Grok/Codex MCP dispatch with canonical\
    \ `mcp <server>:<tool>` names, proved for `use_tool` and `call_tool` wrapper blocks.\
    \ test: `tests/sessions/test_sessions_analyzer.py::test_recent_activity_uses_canonical_tool_names`.\n\
    1.3.9: The adapter enforces the retention boundary: failed results keep bounded\
    \ error text, successful commit-producing results keep commit output, and every\
    \ other successful result reaches the analyzer with empty content, proved across\
    \ all five CLIs. test: `tests/sessions/test_sessions_analyzer.py::test_adapter_drops_successful_noncommit_result_text`.\n\
    1.3.10: `analyzer_turns_from_transcript` returns a materialized list that survives\
    \ the analyzer's full traversal contract: the initial-goal forward pass, both\
    \ `reversed(turns)` scans, and the forward decision pass all see every turn on\
    \ one adapter output. test: `tests/sessions/test_sessions_analyzer.py::test_adapter_output_survives_multi_pass_analyzer`.\n\
    1.3.11: `_read_transcript` keeps its exact list-valued signature (its `summarize.py`\
    \ and `summary_generation.py` callers pass unmodified), while `_read_transcript_window`\
    \ reports `truncated is False` at exactly `SUMMARY_ANALYZER_MAX_RECORDS` records\
    \ and `truncated is True` at one over. test: `tests/sessions/test_summarize.py::test_read_transcript_window_truncation_boundary`.\n\
    1.3.12: A malformed interior transcript line makes `_read_transcript_window` raise\
    \ `TranscriptReadError` and `_generate_session_summary_core` returns a failed\
    \ refresh with no summary revision persisted; a malformed final line with no trailing\
    \ newline is dropped and the refresh proceeds; a final line holding a bare JSON\
    \ list, and a malformed final line the file terminates with `\\n`, each raise\
    \ `TranscriptReadError` and fail the refresh the same way, as do invalid UTF-8\
    \ bytes in an interior line and in a newline-terminated final record, while an\
    \ unterminated final fragment split inside a multibyte code point is dropped and\
    \ the refresh proceeds \u2014 matching 1.2's classification of the same six transcripts\
    \ byte for byte, from identical raw-byte fixtures. test: `tests/sessions/test_summarize.py::test_interior_corruption_aborts_summary_refresh`.\n\
    1.3.13: With a digest present and a transcript carrying edits, a task operation,\
    \ and a commit, a summary just persisted by `_generate_session_summary_core` satisfies\
    \ `compact_summary_metadata_matches` immediately (same builder, same hash); appending\
    \ a successful non-commit `tool_result` record (no analyzer fact changes) keeps\
    \ it `True`, and appending an edit `tool_use` record (a new `files_modified` entry)\
    \ makes it `False`; the matcher returns `False` without raising on interior corruption.\
    \ test: `tests/mcp_proxy/tools/sessions/test_summary_metadata.py::test_fresh_summary_matches_metadata_with_transcript_facts`.\n\
    1.3.15: `_read_first_user_goal` is bounded per call: for a 50,000-record transcript\
    \ with no provider-normalized user text it consumes exactly `SUMMARY_ANALYZER_MAX_RECORDS`\
    \ raw records (observed through a counting parser), returns `None`, and reads\
    \ no more bytes than those records plus one chunk on each of three consecutive\
    \ refreshes (counted through a wrapped file object); with the first user text\
    \ at record 25,000 it returns `None` at the same ceiling and `build_summary_source_context`\
    \ passes `initial_goal=None`; with the first user text at record 19,999 it returns\
    \ that text. test: `tests/sessions/test_summarize.py::test_read_first_user_goal_scan_is_bounded`.\n\
    1.3.16: With no digest on the session, `build_summary_source_context` for Grok,\
    \ Codex, Qwen, and Droid fixtures yields the `transcript_summary` and `last_messages`\
    \ that provider's native parser produces from the raw window (byte-identical to\
    \ today's no-digest path) while `handoff_ctx` carries the adapted analyzer facts.\
    \ test: `tests/sessions/test_summarize.py::test_no_digest_prompt_context_uses_native_parser_for_every_provider`.\n\
    1.3.14: Bytes read by `_read_transcript_window` (counted through a wrapped file\
    \ object) for a 5,000-record and a 50,000-record transcript with `max_records=100`\
    \ are equal within one `TRANSCRIPT_TAIL_CHUNK_BYTES` chunk and never exceed the\
    \ size of the last 101 records plus one chunk; a line straddling a chunk boundary\
    \ parses intact. test: `tests/sessions/test_summarize.py::test_read_transcript_window_io_is_bounded_by_window`.\n\
    1.3.17: `analyzer_turns_from_transcript` is observational on every parser it consumes:\
    \ for Codex, Qwen, and Droid, a parser hydrated with the same non-empty private\
    \ state as 1.1.18 that runs `extract_last_messages(..., include_tool_activity=True)`\
    \ and then `analyzer_turns_from_transcript` over a tool-heavy window \u2014 the\
    \ composed order the summary path uses \u2014 has `snapshot_state()` and every\
    \ private field equal to an untouched control parser hydrated identically, continuing\
    \ `iter_parse_events` over the following records on both yields identical records,\
    \ `parsed_index` values, and `codex_exec_outcomes`, and the adapter output equals\
    \ a fresh parser's. test: `tests/sessions/test_sessions_analyzer.py::test_adapter_scan_leaves_parser_state_untouched`."
  labels:
  - covers:compact-summary-fidelity:1.3:1.3.1
  - covers:compact-summary-fidelity:1.3:1.3.2
  - covers:compact-summary-fidelity:1.3:1.3.3
  - covers:compact-summary-fidelity:1.3:1.3.4
  - covers:compact-summary-fidelity:1.3:1.3.5
  - covers:compact-summary-fidelity:1.3:1.3.6
  - covers:compact-summary-fidelity:1.3:1.3.7
  - covers:compact-summary-fidelity:1.3:1.3.8
  - covers:compact-summary-fidelity:1.3:1.3.9
  - covers:compact-summary-fidelity:1.3:1.3.10
  - covers:compact-summary-fidelity:1.3:1.3.11
  - covers:compact-summary-fidelity:1.3:1.3.12
  - covers:compact-summary-fidelity:1.3:1.3.13
  - covers:compact-summary-fidelity:1.3:1.3.15
  - covers:compact-summary-fidelity:1.3:1.3.16
  - covers:compact-summary-fidelity:1.3:1.3.14
  - covers:compact-summary-fidelity:1.3:1.3.17
  tdd: true
  source_section: '1.3'
  implementation_domain: backend
- title: Digest the ending turn before compaction summaries
  category: code
  task_type: feature
  depends_on:
  - '1.2'
  validation_criteria: "1.4.1: `dispatch` awaits `build_turn_and_digest` before `generate_session_summaries`\
    \ when a memory manager is configured, and skips it cleanly when not. test: `tests/hooks/test_session_summary_dispatcher.py::test_dispatch_digests_before_summary`.\n\
    1.4.2: compact_self's refresh no longer records `digest missing` for a turn whose\
    \ transcript has an undigested pair: starting from an undigested compact-triggering\
    \ turn, both refresh functions reload the Session after the digest and the persisted\
    \ summary revision carries the new digest count and the turn's tool facts. test:\
    \ `tests/mcp_proxy/tools/sessions/test_compact_self.py::test_refresh_digests_pending_turn_before_fallback`.\n\
    1.4.3: `HookManager._dispatch_session_summaries` wires the memory manager and\
    \ config into the dispatcher through `build_session_summary_dispatcher` in `session_summary_wiring.py`,\
    \ and `hook_manager.py` no longer constructs `SessionSummaryDispatcher` directly.\
    \ test: `tests/hooks/test_hook_manager_extra.py::test_dispatch_session_summaries_forwards_memory_manager_and_config`.\n\
    1.4.4: `dispatch` treats a returned `{\"error\"}` or `{\"cancelled\"}` digest\
    \ result without `error_kind == \"transcript_read\"` as a logged failure with\
    \ the summary still generated, treats a `None` result as nothing-to-digest, and\
    \ aborts the refresh entirely on `error_kind == \"transcript_read\"`. test: `tests/hooks/test_session_summary_dispatcher.py::test_dispatch_treats_returned_digest_errors_as_failures`.\n\
    1.4.5: `pre_digest` follows loop identity: a call on the configured daemon loop\
    \ digests in-loop; a call with no running loop while the daemon loop is running\
    \ digests on the daemon loop via `run_coroutine_threadsafe`; a call with a running\
    \ loop but no configured daemon loop, and the `asyncio.run` fallback thread, never\
    \ call `build_turn_and_digest` and log the skip. test: `tests/hooks/test_session_summary_dispatcher.py::test_pre_digest_follows_daemon_loop_identity`.\n\
    1.4.6: `setup_internal_registries` forwards `memory_manager_resolver` through\
    \ `create_session_messages_registry` to `register_terminal_tools`, and `compact_self`\
    \ resolves it per call. test: `tests/mcp_proxy/tools/sessions/test_mcp_proxy_tools_sessions_registration.py::test_sessions_registry_forwards_memory_manager_resolver`.\n\
    1.4.7: compact_self's refresh records a returned digest error as the `digest_fallback`\
    \ reason and skips the digest cleanly when no resolver is wired. test: `tests/mcp_proxy/tools/sessions/test_compact_self.py::test_refresh_records_digest_error_as_fallback_reason`.\n\
    1.4.8: `HookManager.__init__` retains `components.memory_manager`, and a factory-created\
    \ manager wires it through `_dispatch_session_summaries` into the dispatcher.\
    \ test: `tests/hooks/test_session_summary_dispatcher.py::test_hook_manager_wires_memory_manager_into_dispatcher`.\n\
    1.4.9: The scheduled background branch forwards `memory_manager` and `config`\
    \ through `_schedule_compact_handoff_background_refresh` into `_run_compact_handoff_background_refresh`.\
    \ test: `tests/mcp_proxy/tools/sessions/test_compact_self.py::test_scheduled_background_refresh_forwards_memory_manager`.\n\
    1.4.10: With a transcript tail that stays partial across every attempt on a compact-triggering\
    \ turn of 400 records and more than 20,000 characters of tool results (above both\
    \ the 80-line and the `_COMPACT_HANDOFF_FALLBACK_MAX_CHARS` bounds), compact_self's\
    \ refresh calls `build_turn_and_digest` `1 + COMPACT_HANDOFF_TAIL_RETRY_ATTEMPTS`\
    \ times, persists a `digest_fallback` revision rendered from the outcome's `withheld_pair`\
    \ \u2014 its text begins with the compact-triggering turn's full prompt, carries\
    \ the newest ledger lines including the `(no result recorded)` in-flight call,\
    \ and stays within the cap \u2014 with reason `\"transcript tail in-flight\"`\
    \ and `metadata_json[\"tail_withheld\"] is True`, and sets `handoff_ready` only\
    \ after that persistence; with a tail that completes on the second attempt, no\
    \ fallback is persisted and the normal refresh runs on the reloaded session. test:\
    \ `tests/mcp_proxy/tools/sessions/test_compact_self.py::test_tail_withheld_retries_then_persists_transcript_tail_fallback`.\n\
    1.4.11: A digest `TranscriptReadError` outcome (`error_kind == \"transcript_read\"\
    `) is never followed by persistence of a summary revision in the same dispatch\
    \ or refresh: the dispatcher generates nothing and compact_self's refresh returns\
    \ the existing summary with the corruption recorded as the failure reason; the\
    \ case is proven end to end for a malformed newline-terminated final record and\
    \ for a newline-terminated final record holding invalid UTF-8 bytes, both of which\
    \ reach the same branch as an interior one rather than the withhold path, while\
    \ an unterminated fragment split inside a multibyte code point takes the withhold\
    \ path and persists no corruption failure. test: `tests/hooks/test_session_summary_dispatcher.py::test_transcript_corruption_never_persists_a_summary`.\n\
    1.4.12: On PRE_COMPACT, a digest outcome carrying `tail_withheld: True` makes\
    \ the dispatcher return before `generate_session_summaries`: no summary revision\
    \ is persisted, the prior revision and its metadata are unchanged, and the next\
    \ dispatch whose digest includes the withheld pair persists a revision carrying\
    \ that pair's facts. test: `tests/hooks/test_session_summary_dispatcher.py::test_tail_withheld_defers_summary_until_pair_digested`.\n\
    1.4.13: With the daemon loop running in one thread and `dispatch` called from\
    \ a second, different running loop, `build_turn_and_digest` executes on the daemon\
    \ loop (asserted via `asyncio.get_running_loop()` identity inside a fake digest)\
    \ and the per-session lock is acquired there, never on the caller's loop. test:\
    \ `tests/hooks/test_session_summary_dispatcher.py::test_dispatch_from_foreign_loop_digests_on_daemon_loop`.\n\
    1.4.14: Race: with the tail withheld on every foreground attempt, a `wait_for_summary`\
    \ issued the moment `handoff_ready` is set returns a summary containing the compact-triggering\
    \ turn's prompt and never the prior-digest fallback text; once the tail completes,\
    \ the background refresh's digest covers the pair and the regenerated summary\
    \ returned by a later `wait_for_summary` carries that pair's tool facts. test:\
    \ `tests/mcp_proxy/tools/sessions/test_compact_self.py::test_immediate_wait_never_sees_prior_digest_fallback`.\n\
    1.4.15: `_compact_handoff_transcript_tail_markdown` with a `withheld_pair` whose\
    \ prompt alone exceeds `_COMPACT_HANDOFF_FALLBACK_MAX_CHARS` returns the complete\
    \ prompt (no head clamp or `[prompt truncated]` marker exists) and no ledger;\
    \ with a 2,000-character prompt and a 30,000-character ledger it returns the whole\
    \ prompt followed by the newest ledger lines behind one `[N earlier ledger lines\
    \ truncated]` line, within the cap; with `withheld_pair=None` its output is byte-identical\
    \ to today's raw-tail rendering. test: `tests/mcp_proxy/tools/sessions/test_compact_self.py::test_withheld_pair_fallback_reserves_prompt_within_cap`.\n\
    1.4.16: With the daemon loop configured and reported running but `asyncio.run_coroutine_threadsafe`\
    \ patched to raise `RuntimeError`, `dispatch` from a foreign loop closes the unsubmitted\
    \ coroutine (no `RuntimeWarning: coroutine ... was never awaited` is emitted under\
    \ `warnings.catch_warnings(record=True)`), logs `failed to schedule`, sets `done_event`,\
    \ and never calls `build_turn_and_digest` or `generate_session_summaries`. test:\
    \ `tests/hooks/test_session_summary_dispatcher.py::test_rejected_daemon_submission_closes_coroutine_and_releases_waiter`.\n\
    1.4.17: With `refresh_timeout_seconds` set to 0.2 and `build_turn_and_digest`\
    \ patched to never return, `compact_self`'s foreground refresh returns within\
    \ the deadline, the digest coroutine is cancelled, a `digest_fallback` revision\
    \ is persisted whose reason names the timeout, `handoff_ready` is set only after\
    \ that persistence, and the compaction command is still sent; with the hang on\
    \ the second tail retry after a first attempt that returned `tail_withheld` with\
    \ a `withheld_pair`, the same single deadline covers the retries and the persisted\
    \ fallback begins with that pair's prompt, carries its newest ledger lines, and\
    \ records `metadata_json[\"tail_withheld\"] is True` beside the timeout reason;\
    \ with the hang injected inside `persist_digest_state` (the worker released after\
    \ the deadline), the refresh's reload observes the completed digest state before\
    \ the fallback is chosen; parametrized over the other terminal branches, a second\
    \ retry that instead returns `{\"error\": \"boom\"}`, returns `{\"cancelled\"\
    : True, \"reason\": \"shutdown\"}`, or raises `RuntimeError` after the same withheld\
    \ first attempt persists a fallback that begins with the retained pair's prompt,\
    \ carries its ledger, and records `metadata_json[\"tail_withheld\"] is True` beside\
    \ that branch's own failure text as `reason`, while a second retry returning `{\"\
    error\": \u2026, \"error_kind\": \"transcript_read\"}` persists nothing. The same\
    \ holds when the **first** attempt itself fails after extracting the pair \u2014\
    \ the LLM call raising, being cancelled, or returning `{\"error\": \"boom\"}`,\
    \ `persist_digest_state` raising, and the outer deadline firing mid-attempt so\
    \ nothing is returned \u2014 each persists a fallback that begins with the captured\
    \ pair's prompt, carries its ledger, and records `metadata_json[\"tail_withheld\"\
    ] is True`, proving the fallback is built from `withheld_capture` rather than\
    \ from a returned outcome. The withheld\u2192complete\u2192failure sequence is\
    \ pinned over the same terminal branches: a first attempt that withholds pair\
    \ A, a tail that then completes, and a second attempt that resolves the complete\
    \ pair B and then fails \u2014 parametrized over its LLM call returning `{\"error\"\
    : \"boom\"}`, returning `{\"cancelled\": True, \"reason\": \"shutdown\"}`, raising\
    \ `RuntimeError`, `persist_digest_state` raising, and the outer deadline firing\
    \ mid-attempt so nothing is returned \u2014 persists in every case a fallback\
    \ that begins with **B**'s prompt, renders B's `activity` ledger under `## Tool\
    \ activity (in flight)` and B's narration under `## Narration so far` as separate\
    \ sections (which only an uncomposed captured pair can supply), contains none\
    \ of A's in-flight `(no result recorded)` line, and records `metadata_json[\"\
    tail_withheld\"] is False` beside that branch's own failure text as `reason`.\
    \ test: `tests/mcp_proxy/tools/sessions/test_compact_self.py::test_foreground_refresh_digest_timeout_falls_back_within_deadline`."
  labels:
  - covers:compact-summary-fidelity:1.4:1.4.1
  - covers:compact-summary-fidelity:1.4:1.4.2
  - covers:compact-summary-fidelity:1.4:1.4.3
  - covers:compact-summary-fidelity:1.4:1.4.4
  - covers:compact-summary-fidelity:1.4:1.4.5
  - covers:compact-summary-fidelity:1.4:1.4.6
  - covers:compact-summary-fidelity:1.4:1.4.7
  - covers:compact-summary-fidelity:1.4:1.4.8
  - covers:compact-summary-fidelity:1.4:1.4.9
  - covers:compact-summary-fidelity:1.4:1.4.10
  - covers:compact-summary-fidelity:1.4:1.4.11
  - covers:compact-summary-fidelity:1.4:1.4.12
  - covers:compact-summary-fidelity:1.4:1.4.13
  - covers:compact-summary-fidelity:1.4:1.4.14
  - covers:compact-summary-fidelity:1.4:1.4.15
  - covers:compact-summary-fidelity:1.4:1.4.16
  - covers:compact-summary-fidelity:1.4:1.4.17
  tdd: true
  source_section: '1.4'
  implementation_domain: backend
- title: Add the five-CLI activity golden-path parity suite
  category: test
  task_type: feature
  depends_on:
  - '1.2'
  - '1.3'
  validation_criteria: "1.5.1: Five native-envelope fixtures exist, each with the\
    \ edit, shell command, task claim/close, commit with result, one natively failed\
    \ call, one successful sentinel read, one in-flight call with no result record,\
    \ and final compact_self turn; the Codex fixture uses `custom_tool_call` `exec`\
    \ orchestration with `item_completed` `McpToolCall`/`FileChange`/`CommandExecution`\
    \ items carrying the actual activity, the edit as a `FileChange` `changes` entry\
    \ for `src/pkg/widget.py`, its natively failed call as an `McpToolCall` item with\
    \ `status: completed` and a structured `{\"success\": false, \"error\": \u2026\
    }` result (transport success, application failure), and its in-flight call as\
    \ a wrapper-only split tail (a `custom_tool_call` `exec` with neither output record\
    \ nor item). file: `tests/sessions/transcripts/fixtures/golden_path/grok.jsonl`.\n\
    1.5.2: The parametrized parser\u2192digest test asserts path, command, task ref/action,\
    \ commit SHA, and final-turn fact in the ledger for every CLI with pair count\
    \ and role sequence preserved. test: `tests/sessions/test_activity_golden_path.py::test_digest_pairs_carry_activity_for_every_cli`.\n\
    1.5.3: The parametrized summary test asserts the same facts reach the summary\
    \ prompt's ground truth for every CLI. test: `tests/sessions/test_activity_golden_path.py::test_summary_ground_truth_for_every_cli`.\n\
    1.5.4: Every fixture's failed call is annotated `! failed:` with its native error\
    \ text and, rendered under a forced five-line cap, survives ahead of the successful\
    \ read-only calls. test: `tests/sessions/test_activity_golden_path.py::test_failed_call_annotated_and_protected_for_every_cli`.\n\
    1.5.5: The sentinel read's output text appears in no digest pair, no analyzer\
    \ structured context, and no captured prompt for any of the five CLIs, while failed-call\
    \ error text and commit output do. test: `tests/sessions/test_activity_golden_path.py::test_successful_readonly_output_excluded_everywhere`.\n\
    1.5.6: For every CLI, the successful `uv run pytest -k widget` line and the edit\
    \ line (the Codex `FileChange` item included) render bare and the in-flight call's\
    \ line renders `(no result recorded)` in the same Turn-1 ledger, so a passed test\
    \ run is distinguishable from missing evidence end to end. test: `tests/sessions/test_activity_golden_path.py::test_success_and_missing_result_distinguishable_for_every_cli`.\n\
    1.5.7: The Codex mixed window keeps the wrapper-only split-tail call as an execution-chain\
    \ entry naming `tail -f /var/log/widget.log` (projected from the pending wrapper's\
    \ JS arguments) alongside the item-derived entries, with no call dropped or double-counted\
    \ in either the ledger or the analyzer turns. test: `tests/sessions/test_activity_golden_path.py::test_codex_mixed_window_keeps_unmatched_wrapper`."
  labels:
  - covers:compact-summary-fidelity:1.5:1.5.1
  - covers:compact-summary-fidelity:1.5:1.5.2
  - covers:compact-summary-fidelity:1.5:1.5.3
  - covers:compact-summary-fidelity:1.5:1.5.4
  - covers:compact-summary-fidelity:1.5:1.5.5
  - covers:compact-summary-fidelity:1.5:1.5.6
  - covers:compact-summary-fidelity:1.5:1.5.7
  tdd: false
  source_section: '1.5'
  assigned_agent: backend-developer
- title: Deliver the compact continuation block through wait_for_summary and get_handoff_context
  category: code
  task_type: feature
  depends_on: []
  validation_criteria: "2.1.1: `render_compact_continuation_block` renders MCP ledger,\
    \ required/advisory skill directives, task context, and profile as `ContinuationBlock(text=<block>,\
    \ required_by_reference=False)`, and returns `ContinuationBlock(text=\"\", required_by_reference=False)`\
    \ when all are empty \u2014 the bare `\"\"` is returned only by `claim_compact_continuation`,\
    \ which unwraps `block.text`. test: `tests/sessions/test_compact_handoff_block.py::test_render_compact_continuation_block_sections`.\n\
    2.1.2: `wait_for_summary` returns `continuation` for a session with `compact_handoff_inject_pending`\
    \ set and, for a direct (non-reference) delivery, clears the four one-shot variables\
    \ in that one write; a second `wait_for_summary` call replays the identical `continuation`\
    \ from `compact_continuation_rendered` with no further variable write, a `get_handoff_context`\
    \ call after that delivery carries no `continuation` at all, and a call made after\
    \ the next compaction \u2014 whose single arming write clears the cache and re-arms\
    \ the flag \u2014 renders a fresh block instead of the cached one. test: `tests/mcp_proxy/test_mcp_tools_session_messages.py::test_wait_for_summary_delivers_compact_continuation_once`.\n\
    2.1.3: `completed: false` and `found: false` responses never clear the one-shot\
    \ variables. test: `tests/mcp_proxy/test_mcp_tools_session_messages.py::test_wait_for_summary_timeout_keeps_continuation_pending`.\n\
    2.1.4: `get_handoff_context` carries the same block under the same conditions.\
    \ symbol: `get_handoff_context`.\n2.1.5: `claim_compact_continuation` renders\
    \ inside the mutation and returns the block while clearing the four one-shots\
    \ in one write; with `allow_replay=True` that same write stores `compact_continuation_rendered`,\
    \ and with `allow_replay=False` no cache is written at all; a pending session\
    \ whose block renders empty has the same four one-shots cleared in one write,\
    \ caches `\"\"` only under `allow_replay=True`, and receives `\"\"`; a following\
    \ `allow_replay=True` call returns the cached text with no write while a following\
    \ `allow_replay=False` call returns `None`; and it returns `None` with no write\
    \ for sessions never pending, when the renderer returns `None`, and once `apply_in_place_compact_context_loss`\
    \ has cleared the cache. test: `tests/workflows/test_session_variable_manager.py::test_claim_compact_continuation_is_one_shot`.\n\
    2.1.6: Concurrent `wait_for_summary` and `get_handoff_context` calls (two threads,\
    \ one pending session) yield exactly one response with `continuation` and the\
    \ one-shots are cleared once, in **both** claim orders: the `get_handoff_context`\
    \ loser reads no cache, and when `get_handoff_context` wins it writes none, so\
    \ the `wait_for_summary` loser has nothing to replay. Two concurrent `wait_for_summary`\
    \ calls are the one documented exception \u2014 the loser replays the identical\
    \ block \u2014 and even there the one-shots are cleared once and both blocks are\
    \ byte-identical. test: `tests/mcp_proxy/test_mcp_tools_session_messages.py::test_compact_continuation_consumed_exactly_once_under_concurrency`.\n\
    2.1.7: With a stale `summary_markdown`, `wait_for_summary` returns the live handoff\
    \ context, carries `continuation` exactly once, and clears the one-shots on that\
    \ delivery. test: `tests/mcp_proxy/test_mcp_tools_session_messages.py::test_wait_for_summary_stale_live_branch_delivers_continuation_once`.\n\
    2.1.8: A pending session whose variables hold no section at all gets no `continuation`\
    \ key, keeps its full `summary_markdown` (no stub swap), and leaves the call with\
    \ `compact_handoff_inject_pending` and `pending_context_reset` both false; a second\
    \ call is a plain non-pending response. test: `tests/mcp_proxy/test_mcp_tools_session_messages.py::test_wait_for_summary_pending_empty_block_consumes_one_shots`.\n\
    2.1.9: `get_handoff_context` leaves the one-shots untouched on the no-context,\
    \ child-project-mismatch, and invalid-child returns, and claims with the parent\
    \ session id on a project/source filtered lookup. test: `tests/mcp_proxy/test_mcp_tools_session_messages.py::test_get_handoff_context_claims_only_on_successful_delivery`.\n\
    2.1.10: Delivery and the empty no-op both clear `pending_context_reset` in the\
    \ same atomic claim, so context-pressure guidance resumes; every non-delivery\
    \ branch (timeout, not found, required section too large) leaves it true. test:\
    \ `tests/workflows/test_session_variable_manager.py::test_claim_compact_continuation_clears_pending_context_reset`.\n\
    2.1.11: A `gobby-sessions/wait_for_summary` or `gobby-sessions/get_handoff_context`\
    \ result carrying a top-level `continuation` key is never offloaded even above\
    \ `threshold_chars`, while the same tools' results without that key above the\
    \ threshold are offloaded to the normal retrieval envelope. test: `tests/mcp_proxy/services/test_result_offload.py::test_continuation_delivery_results_are_exempt_only_when_delivered`.\n\
    2.1.12: With a maximum-size profile and a large MCP ledger, every block `render_compact_continuation_block`\
    \ returns satisfies the supplied `fits` predicate as serialized: the required-skill\
    \ directive survives intact, every lower-priority section is either present whole\
    \ or replaced by its pointer/omission line with no section text prefix-cut, and\
    \ delivery still clears the one-shots. test: `tests/sessions/test_compact_handoff_block.py::test_render_compact_continuation_block_respects_fit_predicate`.\n\
    2.1.13: The complete serialized response is bounded for both delivery tools: with\
    \ a maximum-size summary plus maximum profile and MCP ledger, `wait_for_summary`'s\
    \ response (base + `continuation` + framing) measures within `inline_context_budget_for(source)`\
    \ by `_serialized_len` via the base-stub swap with the one-shots claimed exactly\
    \ once, and `get_handoff_context` with an oversized base returns without `continuation`,\
    \ without consuming the one-shots, and \u2014 run through `ToolResultOffloader`\
    \ \u2014 arrives as a retrieval envelope under `threshold_chars`. A replayed block\
    \ is re-fitted against the response it is replayed beside, never against the one\
    \ it was rendered for: after an abandoned claim on a stubbed response, a retry\
    \ whose base is the full-size summary swaps in the stub and measures within the\
    \ budget again, and with the budget lowered so even the stubbed response cannot\
    \ hold the cached block the retry returns the real base with no `continuation`\
    \ and the cache intact for a later call. test: `tests/mcp_proxy/test_mcp_tools_session_messages.py::test_delivery_response_total_stays_within_provider_budget`.\n\
    2.1.14: A required-skill list whose full directive does not fit beside the base\
    \ leaves the one-shots armed and delivers no `continuation` through `get_handoff_context`,\
    \ while `wait_for_summary` delivers the complete directive (every skill named)\
    \ after the base-stub swap when it fits beside the stub, and otherwise the reference\
    \ form naming `get_variable`, the session id, and `compact_resume_required_skills`;\
    \ no delivered block ever contains a truncated required-skill section. test: `tests/sessions/test_compact_handoff_block.py::test_required_skill_section_is_all_or_nothing`.\n\
    2.1.15: Escape-heavy and non-ASCII maximum payloads (task context and profile\
    \ made of quotes, backslashes, control characters, and astral-plane emoji, whose\
    \ JSON form is more than twice their raw length) still produce a response within\
    \ the provider budget as serialized by `_serialized_len`, and that response re-serialized\
    \ with `json.dumps(indent=2)` (the proxy's `threshold_chars` measure) is never\
    \ smaller than the wire measure, proving the fit is measured on the final wire\
    \ response rather than raw characters. test: `tests/sessions/test_compact_handoff_block.py::test_fit_predicate_charges_json_escaping`.\n\
    2.1.16: `get_handoff_context` for a session with nothing pending and an over-threshold\
    \ context is offloaded by the proxy exactly as today, and its one-shot variables\
    \ are untouched; for a pending session whose base measures above `inline_context_budget_for(source)`\
    \ and at or below `threshold_chars` \u2014 including under a live `additional_context_limits`\
    \ override that moves the inline budget \u2014 the response is served inline without\
    \ `continuation`, the one-shots stay armed, and the following `wait_for_summary`\
    \ delivers the block via the stub swap. test: `tests/mcp_proxy/test_mcp_tools_session_messages.py::test_get_handoff_context_non_claim_path_offloads_normally`.\n\
    2.1.17: Repeated `wait_for_summary` calls against a pending session converge to\
    \ a stable replayed result in one call for every render outcome, each later call\
    \ re-returning the cached text without writing while an interleaved `get_handoff_context`\
    \ never carries `continuation`: a non-empty block is delivered once; an empty\
    \ block is consumed once with `summary_markdown` intact; a required section that\
    \ cannot fit even beside the stub is delivered once in reference form with `compact_resume_required_skills`\
    \ still holding every name and the other three one-shots cleared, and a second\
    \ call replays that same reference block; only a budget below the stub plus the\
    \ reference block leaves the one-shots armed with nothing cached, with each response\
    \ carrying the real, unstubbed summary and an error logged; and no call sequence\
    \ leaves `pending_context_reset` true after a delivered or empty outcome. test:\
    \ `tests/mcp_proxy/test_mcp_tools_session_messages.py::test_compact_continuation_repeated_calls_reach_terminal_state`.\n\
    2.1.18: End to end for a Grok session whose required-skill directive exceeds `inline_context_budget_for(\"\
    grok\")` beside the stub: `wait_for_summary` returns `continuation` in reference\
    \ form within the budget as serialized and naming that session's id, the test\
    \ extracts the rendered `get_variable(session_id=\"\u2026\", name=\"compact_resume_required_skills\"\
    )` call from the block and invokes the proxy's `get_variable` tool with exactly\
    \ those arguments, which returns every required skill name, and a following `get_handoff_context`\
    \ carries no `continuation`; `claim_compact_continuation` with a reference-form\
    \ renderer result clears `compact_handoff_inject_pending`, `compact_resume_advisory_skills`,\
    \ and `pending_context_reset` in one write while leaving `compact_resume_required_skills`\
    \ intact. test: `tests/mcp_proxy/test_mcp_tools_session_messages.py::test_oversized_required_directive_is_recoverable_by_reference`.\n\
    2.1.19: With `_mutate_variables` blocked on an event inside `claim_compact_continuation`,\
    \ an unrelated task on the same event loop advances while `wait_for_summary` awaits\
    \ the claim, and the response carries `continuation` once the event releases;\
    \ `get_handoff_context` performs the claim synchronously on its own thread. test:\
    \ `tests/mcp_proxy/test_mcp_tools_session_messages.py::test_wait_for_summary_claim_runs_off_the_event_loop`.\n\
    2.1.20: With no required skills and task, profile, and MCP-ledger content that\
    \ cannot fit beside `get_handoff_context`'s base even as pointer lines, the renderer\
    \ returns `None`, `get_handoff_context` delivers no `continuation` and leaves\
    \ all four one-shots armed, and the next `wait_for_summary` delivers every section\
    \ whole or by pointer after its stub swap; a session whose variables hold no section\
    \ yields `ContinuationBlock(text=\"\")` and is consumed as the empty no-op; a\
    \ delivered `task_context` pointer line names `get_variable` with the session\
    \ id and variable name, and invoking the proxy's `get_variable` with exactly those\
    \ arguments returns the complete value. test: `tests/sessions/test_compact_handoff_block.py::test_present_content_is_delivered_whole_or_by_pointer`.\n\
    2.1.22: The continuation survives every abandonment boundary of the MCP wait wrapper:\
    \ with the handler made to complete its claim after `_await_with_guard`'s deadline\
    \ (so the caller receives `completed: false` and `_consume_background_result`\
    \ discards the real result), the next `wait_for_summary` returns the identical\
    \ `continuation` text from the cache; with the `asyncio.to_thread` claim cancelled\
    \ after its transaction commits, the same replay holds; with the cancellation\
    \ delivered before the commit, nothing is consumed and the retry renders fresh.\
    \ The replay's lifetime is pinned across the variable's live owners: a `get_handoff_context`\
    \ call issued between the abandoned claim and the retry carries no `continuation`\
    \ and leaves the cache intact for the next `wait_for_summary`; `SessionNotificationRouter._clear_compact_marker`\
    \ blanking `compact_notification_started_at` between them changes nothing (the\
    \ cache does not read it); the same replay survives a fresh `SessionVariableManager`\
    \ on a restarted daemon; and once `apply_in_place_compact_context_loss` runs for\
    \ a genuinely new compaction, the cache is gone in that same arming write and\
    \ the next claim renders fresh from the newly armed variables rather than replaying\
    \ the previous generation. test: `tests/mcp_proxy/test_mcp_tools_session_messages.py::test_continuation_survives_wrapper_timeout_and_cancellation`.\n\
    2.1.21: With the session pending and, in turn, `SessionVariableManager.claim_compact_continuation`\
    \ raising from inside its transaction and `render_compact_continuation_block`\
    \ raising, both `wait_for_summary` (`completed: true`) and `get_handoff_context`\
    \ (`has_context: true`) carry `continuation_pending: true` and no `continuation`,\
    \ leave all four one-shots armed, and log the failure with the session id, and\
    \ the following call with the fault removed delivers the block once. The base\
    \ that carries the signal is asserted per tool at **both** response sizes: `wait_for_summary`\
    \ returns the ~330-character reference stub whether its summary is below or above\
    \ `threshold_chars` (byte-identical stub text in both), and that response passes\
    \ through the real `ToolResultOffloader` unoffloaded with `success`, `completed`,\
    \ and `continuation_pending` still readable at top level; `get_handoff_context`\
    \ returns its `context` byte-identical to the no-pending response at both sizes,\
    \ served inline below the threshold and offloaded above it to a `result_id` envelope\
    \ whose retrieved payload is that complete base, with its one-shots armed either\
    \ way. test: `tests/mcp_proxy/test_mcp_tools_session_messages.py::test_continuation_claim_failure_preserves_base_result`."
  labels:
  - covers:compact-summary-fidelity:2.1:2.1.1
  - covers:compact-summary-fidelity:2.1:2.1.2
  - covers:compact-summary-fidelity:2.1:2.1.3
  - covers:compact-summary-fidelity:2.1:2.1.4
  - covers:compact-summary-fidelity:2.1:2.1.5
  - covers:compact-summary-fidelity:2.1:2.1.6
  - covers:compact-summary-fidelity:2.1:2.1.7
  - covers:compact-summary-fidelity:2.1:2.1.8
  - covers:compact-summary-fidelity:2.1:2.1.9
  - covers:compact-summary-fidelity:2.1:2.1.10
  - covers:compact-summary-fidelity:2.1:2.1.11
  - covers:compact-summary-fidelity:2.1:2.1.12
  - covers:compact-summary-fidelity:2.1:2.1.13
  - covers:compact-summary-fidelity:2.1:2.1.14
  - covers:compact-summary-fidelity:2.1:2.1.15
  - covers:compact-summary-fidelity:2.1:2.1.16
  - covers:compact-summary-fidelity:2.1:2.1.17
  - covers:compact-summary-fidelity:2.1:2.1.18
  - covers:compact-summary-fidelity:2.1:2.1.19
  - covers:compact-summary-fidelity:2.1:2.1.20
  - covers:compact-summary-fidelity:2.1:2.1.22
  - covers:compact-summary-fidelity:2.1:2.1.21
  tdd: true
  source_section: '2.1'
  implementation_domain: backend
- title: Route the Grok continuation prompt to wait_for_summary and retire the dead
    turn_start rule
  category: code
  task_type: feature
  depends_on:
  - '2.1'
  - '1.2'
  - '1.4'
  validation_criteria: '2.2.1: The Grok directive contains no injected-context clause,
    names `continuation`, retries only on `success=true` with `completed=false` or
    with `continuation_pending=true` under one explicit cap of three further calls,
    and stops with the stated fallback on `success=false` or `found=false`; the Claude/Codex
    directive is unchanged. test: `tests/sessions/test_compact_continuation.py::test_grok_continue_prompt_routes_to_wait_for_summary`.

    2.2.2: Both builder call sites pass `source`; a Grok `compact_self` persists a
    marker whose prompt names `continuation` and omits the injected-context clause,
    and PostCompact types that exact prompt. test: `tests/mcp_proxy/tools/sessions/test_compact_self.py::test_grok_compact_self_marker_carries_wait_for_summary_directive`.

    2.2.3: `inject-compact-handoff-on-prompt` no longer exists in the bundled rules
    and authoritative sync retires its row. test: `tests/workflows/test_context_handoff_rules.py::test_authoritative_sync_retires_inject_compact_handoff_on_prompt`.

    2.2.4: `wait_for_summary_directive` lives in `compact_handoff_block.py` and `compact_continuation.py`
    stays under 1,000 lines. test: `tests/sessions/test_compact_handoff_block.py::test_wait_for_summary_directive_by_source`.

    2.2.5: `WebChatSessionRegistry.compact_session` builds the prompt with `source=None`
    and its directive is unchanged. test: `tests/servers/websocket/chat/test_session_registry.py::test_compact_session_prompt_source_none`.

    2.2.6: After this leaf''s manifest regeneration, the committed checksum manifest
    equals the bundled source tree. test: `tests/install/test_bundled_content_manifest.py::test_bundled_content_manifest_matches_tree`.'
  labels:
  - covers:compact-summary-fidelity:2.2:2.2.1
  - covers:compact-summary-fidelity:2.2:2.2.2
  - covers:compact-summary-fidelity:2.2:2.2.3
  - covers:compact-summary-fidelity:2.2:2.2.4
  - covers:compact-summary-fidelity:2.2:2.2.5
  - covers:compact-summary-fidelity:2.2:2.2.6
  tdd: true
  source_section: '2.2'
  implementation_domain: backend
- title: Document Grok compact handoff delivery
  category: docs
  task_type: feature
  depends_on:
  - '2.2'
  validation_criteria: "2.3.1: The sessions guide describes the wait_for_summary delivery\
    \ path and no longer references the retired rule. behavior: \"Grok compact continuation\"\
    \ in `docs/guides/sessions.md`.\n2.3.2: The adapter-fidelity table states the\
    \ declared Grok context channels are verified ignored by Grok 1.0.5 (2026-08-20)\
    \ and points at #20635 for the capability correction and replacement channel.\
    \ behavior: \"Grok context channel\" in `docs/guides/adapter-fidelity.md`.\n2.3.3:\
    \ No live doc still names `inject-compact-handoff-on-prompt`: the session-boundary\
    \ contract and the variables guide describe the tool-response claim path with\
    \ the four variables a direct or empty delivery clears and the three a reference-form\
    \ delivery clears (naming `compact_resume_required_skills` as the preserved `get_variable`\
    \ target), and the MCP-tools guide documents the optional `continuation` key on\
    \ both delivery tools together with the retry semantics \u2014 `wait_for_summary`\
    \ replaying the identical block within one compaction and answering a claim failure\
    \ with `continuation_pending` beside its reference stub, `get_handoff_context`\
    \ never replaying and keeping its full context. behavior: \"compact continuation\
    \ delivery\" in `docs/contracts/session-boundary.md`."
  labels:
  - covers:compact-summary-fidelity:2.3:2.3.1
  - covers:compact-summary-fidelity:2.3:2.3.2
  - covers:compact-summary-fidelity:2.3:2.3.3
  tdd: false
  source_section: '2.3'
  assigned_agent: tech-writer
```
