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
  must return the same message count and role sequence as without it.
- Hand-maintained production files stay under 1,000 lines. `src/gobby/memory/digest.py`
  is at 810 lines, `src/gobby/sessions/transcripts/claude.py` at 746, and
  `src/gobby/sessions/compact_continuation.py` at 952; put new helpers and new prompt
  text in the new modules named below rather than growing those files. Section 2.2 in
  particular keeps its `compact_continuation.py` delta to the `source` plumbing and a
  one-line delegation.
- Ledger budgets: `DIGEST_ACTIVITY_MAX_LINES = 80`, `DIGEST_ACTIVITY_MAX_CHARS = 6000`
  per assistant message, independent of Grok's `_PAIR_RESPONSE_CHAR_BUDGET = 4000`
  narration cap. Truncation is evidence-aware (1.1): failed calls, edited paths, task
  mutations, commit-producing calls, and the turn's last `DIGEST_ACTIVITY_TAIL_LINES = 10`
  calls survive the caps ahead of everything else.
- Successful tool output is retained only for commit-producing calls (shell `git commit`
  results and `commit_sha` arguments of `close_task`/`link_commit`); no other successful
  result text enters the ledger, the analyzer, or the digest.
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
- `src/gobby/sessions/transcripts/base.py::TranscriptParser.extract_last_messages`
- `src/gobby/sessions/transcripts/claude.py::ClaudeTranscriptParser.extract_last_messages`
- `src/gobby/sessions/transcripts/grok.py::GrokTranscriptParser.extract_last_messages`
- `src/gobby/sessions/transcripts/grok.py::_segment_pair_messages`
- `src/gobby/sessions/transcripts/codex.py::CodexTranscriptParser.extract_last_messages`
- `src/gobby/sessions/transcripts/qwen.py::QwenTranscriptParser.extract_last_messages`
- `src/gobby/sessions/transcripts/droid.py::DroidTranscriptParser.extract_last_messages`
- `src/gobby/hooks/_normalization_shell.py::is_shell_tool`
- `tests/sessions/transcripts/test_grok_parser.py::*` — scope-reason: add ledger and invariant cases for the Grok parser
- `tests/sessions/transcripts/test_qwen_transcript_parser.py::*` — scope-reason: add ledger and invariant cases for the Qwen parser
- `tests/sessions/transcripts/test_droid_parser.py::*` — scope-reason: add ledger and invariant cases for the Droid parser
- `tests/sessions/test_transcript_parsers.py::*` — scope-reason: add ledger and invariant cases for the Claude and Codex parsers
- `tests/sessions/transcripts/test_tool_activity.py`

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
each parser appends a ledger block to the assistant message that the tool calls belong
to. The returned message count and role sequence are identical with and without the
flag; tests assert this on every fixture in `tests/sessions/transcripts/fixtures/`
(including `grok_audit/10711/updates.jsonl` and `grok_audit/10725/updates.jsonl`).

Ledger attachment rule per parser:

- **Claude** (`message.content` blocks): collect `tool_use` blocks from assistant records
  and `tool_result` blocks (with `is_error`) from user records; attach the accumulated
  ledger to the next assistant record that has non-empty text (the message that already
  exists in today's output). If the turn ends with tool-only assistant records (no later
  text), attach to the previous text-bearing assistant message. Tool-only records never
  become new messages.
- **Grok** (`_segment_pair_messages`): within a segment, `tool_call` updates (`title`,
  `rawInput`; unwrap `use_tool{tool_name, tool_input}`) and `tool_call_update` records
  with `status == "failed"` (matched by `toolCallId`) feed the ledger; the ledger is
  appended to the segment's single accumulated assistant content. The ledger budget is
  separate from `_PAIR_RESPONSE_CHAR_BUDGET`.
- **Codex** (`response_item` payloads): `function_call` (name + `arguments` JSON string)
  and `function_call_output` error outputs feed the ledger; attach to the next
  `message` payload with role `assistant`, else the previous one.
- **Qwen** (`message.parts[].functionCall`): replace today's bare `[Tool call: name]`
  label with the full ledger line when the flag is on; with the flag off keep the bare
  label exactly as today.
- **Droid** (`message.content[]` blocks of type `tool_use`/`tool_result`): same rule as
  Claude.

Successful-result correlation is the same for every parser and limited to calls where
`is_commit_producing` is true: Claude and Droid match non-error `tool_result` blocks by
`tool_use_id`; Grok matches `tool_call_update` records with `status == "completed"` by
`toolCallId` (`_extract_tool_result(update)["output"]`); Codex matches
`function_call_output` by `call_id`; Qwen matches `functionResponse` by `id`. The output
text goes through `commit_outcome` and lands on the entry's `outcome`; output for any
other call is discarded unread.

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

def canonical_tool_name(tool_name: str, tool_input: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    """Unwrap dispatchers and normalise aliases.

    - Grok ``use_tool`` -> ``tool_input["tool_name"]`` / ``tool_input["tool_input"]``.
    - MCP dispatchers (``call_tool``, ``mcp__gobby__call_tool``, ``gobby__call_tool``,
      ``mcp_call_tool``) -> ``"mcp <server_name>:<tool_name>"`` with ``arguments`` as input.
    - Shell aliases via ``canonicalize_shell_tool_name`` (add ``run_terminal_command``
      to ``_SHELL_TOOLS``); ACP names via ``normalize_acp_tool_name``.
    """

def commit_outcome(tool_name: str, tool_input: dict[str, Any], output: str | None) -> str | None:
    """Successful-result evidence, kept only for commit-producing calls.

    Canonical shell tool whose ``command`` contains ``git commit``: parse
    ``[<branch> <sha>] <subject>`` from ``output`` and return ``commit <sha> <subject>``
    (subject capped at 80 chars). Canonical ``mcp gobby-tasks:close_task`` or
    ``mcp gobby-tasks:link_commit`` with ``commit_sha`` in its arguments: return
    ``commit <sha>`` (no output needed). Every other call returns ``None``; parsers
    never retain successful output for anything else.
    """

def is_commit_producing(tool_name: str, tool_input: dict[str, Any]) -> bool:
    """True when ``commit_outcome`` could be non-None. Parsers call this before
    looking up a call's successful result at all."""

def format_tool_activity_line(entry: ToolActivityEntry) -> str:
    """One line: ``- <name> <primary>`` [+ `` → <outcome>``] [+ `` ! failed: <first 160 chars>``].

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
    with ``- … N more tool calls omitted`` counting every dropped line.
    """
```

Illustrative ledger shaped on #10854's first turn (the two commit lines are added to
show outcome evidence):

```text
[tool activity]
- read_file /Users/josh/.grok/sessions/.../goal/plan.md
- mcp gobby-tasks:get_task task_id=#20539
- mcp gobby-tasks:claim_task task_id=#20544
- run_terminal_command gcode outline src/gobby/sessions/compact_continuation.py; ech…
- search_replace /Users/josh/.gobby/worktrees/gobby/wt-task-20539-m1/tests/sessions/test_clear_continuation.py (x3)
- run_terminal_command cd /Users/josh/.gobby/worktrees/gobby/wt-task-20539-m1 && uv run pytest … ! failed: DATABASE_URL is not set
- run_terminal_command cd … && git commit -m "[gobby-#20544] feat: clear continuation tests" → commit 4f1c2ab [gobby-#20544] feat: clear continuation tests
- mcp gobby-tasks:close_task task_id=#20544 commit_sha=4f1c2ab → commit 4f1c2ab
```

Unit tests in the new `tests/sessions/transcripts/test_tool_activity.py` cover
`canonical_tool_name` (use_tool unwrap, MCP dispatcher unwrap, shell alias), primary
argument selection, `(xN)` collapsing, failure annotation, `commit_outcome` for shell and
task-tool commits, and both caps — including a 120-entry list whose `search_replace` of a
new path, `git commit`, and `mcp gobby-tasks:close_task` entries sit after position 80
and survive both caps while the omission count equals the number of dropped lines.

**Acceptance:**

- 1.1.1 - `tool_activity.py` exposes `canonical_tool_name`, `format_tool_activity_line`, and `render_tool_activity` with the caps and collapsing described above. file: `src/gobby/sessions/transcripts/tool_activity.py`.
- 1.1.2 - Every parser accepts `include_tool_activity` and returns identical message counts and role sequences with the flag on and off across all fixtures. test: `tests/sessions/test_transcript_parsers.py::test_tool_activity_flag_preserves_pair_shape`.
- 1.1.3 - Grok segments carry a ledger naming `search_replace` paths, `mcp gobby-tasks:claim_task`, and failed `run_terminal_command` results from the `grok_audit` fixtures. test: `tests/sessions/transcripts/test_grok_parser.py::test_extract_last_messages_tool_activity_ledger`.
- 1.1.4 - Claude tool-only assistant records attach to the neighbouring text message and never create new messages. test: `tests/sessions/test_transcript_parsers.py::test_claude_tool_activity_attaches_to_text_message`.
- 1.1.5 - `run_terminal_command` is recognised as a shell tool. symbol: `is_shell_tool`.
- 1.1.6 - Truncation keeps failed calls, first-per-path edits, task mutations, commit-producing calls, and the last ten calls under both caps for a 120-entry list, with an accurate omission count. test: `tests/sessions/transcripts/test_tool_activity.py::test_render_tool_activity_truncation_keeps_evidence`.
- 1.1.7 - Commit-producing calls carry `→ commit <sha>` from the correlated successful result or `commit_sha` argument, and no other call retains successful output. test: `tests/sessions/transcripts/test_tool_activity.py::test_commit_outcome_from_shell_and_task_tools`.

### 1.2 Feed digest pairs with the ledger and teach the turn-record prompt [category: code] (depends: 1.1)
`kind: deliverable`

Targets:
- `src/gobby/memory/digest.py::_extract_digest_pairs`
- `src/gobby/memory/digest.py::_build_turn_record_prompt`
- `src/gobby/install/shared/prompts/memory/turn_record.md`
- `src/gobby/install/bundled_content_manifest.json::*` — scope-reason: regenerated checksum manifest for changed bundled content
- `tests/memory/test_digest.py::*` — scope-reason: add ledger-in-pair and prompt-instruction cases

`_extract_digest_pairs` calls
`parser.extract_last_messages(turns, num_pairs=max(1, len(turns)), include_tool_activity=True)`.
Everything else in the pairing logic (injected-context stripping, lifecycle-prompt and
synthetic-noise filtering, cursor arithmetic in `_read_undigested_turns`) is unchanged;
the ledger is part of the assistant `content`, so `last_digest_input_hash` changes
naturally when activity changes.

`_read_last_turn_from_transcript` keeps calling `extract_last_messages(turns, num_pairs=1)`
without the flag.

Add one paragraph to both the bundled prompt `turn_record.md` and the inline fallback in
`_build_turn_record_prompt`, placed directly before "turn_markdown must cover":

```text
The Agent Response may end with a `[tool activity]` ledger: one line per tool call in
order, with the primary argument (file path, command, query, MCP server:tool and task
ref) and ` ! failed:` annotations. Treat that ledger as the authoritative record of tools
used, files created or modified, commands run, commits, and task operations; narration
that contradicts it is wrong.
```

Regenerate `bundled_content_manifest.json` with
`uv run python -c "from pathlib import Path; from gobby.install.manifest import write_bundled_content_manifest; write_bundled_content_manifest(Path('src/gobby/install'))"`
so the changed prompt syncs to the DB registry (rule 8: the DB row is the live prompt).

**Acceptance:**

- 1.2.1 - Digest pairs contain the ledger for tool-heavy turns while `_read_last_turn_from_transcript` output is unchanged. test: `tests/memory/test_digest.py::test_extract_digest_pairs_includes_tool_activity`.
- 1.2.2 - The inline fallback prompt and the bundled prompt both carry the ledger instruction and the bundled manifest checksum is updated. file: `src/gobby/install/shared/prompts/memory/turn_record.md`.
- 1.2.3 - Replaying the Grok audit fixture through `_extract_digest_pairs` yields a pair whose response names `search_replace` and `mcp gobby-tasks:claim_task`. symbol: `_extract_digest_pairs`.

### 1.3 Ground summaries in transcript-derived structured data when a digest exists [category: code] (depends: 1.1)
`kind: deliverable`

Targets:
- `src/gobby/sessions/summarize.py::_generate_session_summary_core`
- `src/gobby/sessions/analyzer.py::TranscriptAnalyzer.extract_handoff_context`
- `src/gobby/sessions/analyzer.py::TranscriptAnalyzer._analyze_tool_use`
- `src/gobby/sessions/analyzer_turns.py`
- `tests/sessions/test_summarize.py::*` — scope-reason: add digest-present structured-context cases
- `tests/sessions/test_sessions_analyzer.py::*` — scope-reason: add adapter and canonical-name cases

In `_generate_session_summary_core`, read the transcript whenever the file exists, not
only when the digest is missing:

```python
digest_markdown = _digest_markdown_for_summary(session)
turns: list[dict[str, Any]] = []
if path is not None and path.exists():
    turns = await _read_transcript(path, source=source)
elif not digest_markdown:
    return ...  # existing "No transcript path" / "Transcript file not found" results
```

The digest still drives `transcript_summary` and `last_messages` inside
`_build_summary_prompt_context` (its `if digest_markdown:` branch is untouched); the
transcript turns feed only the analyzer, which populates `files_modified`, `git_commits`,
`task_progress`, `active_gobby_task`, and `recent_activity`, which in turn unlock
`git_status`, `file_changes`, and `git_diff_summary` via `has_session_edits`.

New module `analyzer_turns.py` exposes one adapter so the analyzer never learns per-CLI
envelopes:

```python
def analyzer_turns_from_transcript(parser: Any, turns: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Run parser.parse_line over raw records and emit Claude-shaped turns.

    ParsedMessage(content_type="text", role in {"user","assistant"}) ->
        {"type": role, "message": {"role": role, "content": [{"type": "text", "text": ...}]}}
    ParsedMessage(content_type="tool_use") ->
        {"type": "assistant", "message": {"role": "assistant", "content": [
            {"type": "tool_use", "name": tool_name, "input": tool_input, "id": tool_use_id}]}}
    ParsedMessage(content_type="tool_result") ->
        {"type": "user", "message": {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": tool_use_id,
             "content": <output text>, "is_error": bool(tool_result.get("is_error") or error)}]}}
    Everything else is dropped. Consecutive blocks from one source record stay in one turn.
    """
```

`_generate_session_summary_core` applies the adapter for every source except `claude`
(whose raw turns already match). `_analyze_tool_use` routes names through
`canonical_tool_name` from 1.1 before its `Edit`/`Write`/MCP/shell branches, extends the
edit set with `search_replace`, `write_file`, `write`, `apply_patch`, `edit_file`, and
`create_file`, and recognises MCP dispatch by the canonical `mcp <server>:<tool>` form so
Grok (`use_tool` → `gobby__call_tool`) and Codex (`mcp__gobby__call_tool`) task
operations populate `task_progress`.

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

### 1.4 Digest the ending turn before compaction summaries [category: code]
`kind: deliverable`

Targets:
- `src/gobby/hooks/session_summary_dispatcher.py::SessionSummaryDispatcher.__init__`
- `src/gobby/hooks/session_summary_dispatcher.py::SessionSummaryDispatcher.dispatch`
- `src/gobby/hooks/hook_manager.py::HookManager._dispatch_session_summaries`
- `src/gobby/mcp_proxy/tools/sessions/_terminal_handoff.py::_refresh_compact_handoff_context`
- `src/gobby/mcp_proxy/tools/sessions/_terminal_handoff.py::_run_compact_handoff_background_refresh`
- `tests/hooks/test_session_summary_dispatcher.py`
- `tests/mcp_proxy/tools/sessions/test_compact_self.py::*` — scope-reason: add digest-before-refresh cases

`SessionSummaryDispatcher.__init__` gains `memory_manager: Any | None = None` and
`config: Any | None = None`; `HookManager._dispatch_session_summaries` passes the
manager it was constructed with and `self._config`. In `dispatch._run`, before
`generate_session_summaries`:

```python
if self.memory_manager is not None and self.llm_service is not None:
    from gobby.memory.digest import build_turn_and_digest
    try:
        await build_turn_and_digest(
            memory_manager=self.memory_manager,
            session_manager=self.session_manager,
            session_id=session_id,
            llm_service=self.llm_service,
            db=self.database,
            config=self.config,
        )
    except Exception:
        self.logger.warning("pre-summary digest failed for %s", session_id, exc_info=True)
```

`build_turn_and_digest` is idempotent: the per-session lock serialises it against the
turn-end digest and `last_digest_input_hash` dedupes an already-digested turn, so running
it from PRE_COMPACT costs nothing when the turn-end digest already landed. Refresh the
session object after it returns so `generate_session_summaries` reads the new digest.

In `_terminal_handoff.py`, `_refresh_compact_handoff_context` and
`_run_compact_handoff_background_refresh` receive `memory_manager` and `config` from the
compact_self tool wiring (the same objects the memory MCP tools use) and call the same
`build_turn_and_digest` before their existing summary/fallback logic, inside the existing
`_compact_handoff_refresh_timeout_seconds` budget. When the digest call fails or times
out, the existing fallback path runs unchanged.

**Acceptance:**

- 1.4.1 - `dispatch` awaits `build_turn_and_digest` before `generate_session_summaries` when a memory manager is configured, and skips it cleanly when not. test: `tests/hooks/test_session_summary_dispatcher.py::test_dispatch_digests_before_summary`.
- 1.4.2 - compact_self's refresh no longer records `digest missing` for a turn whose transcript has an undigested pair. test: `tests/mcp_proxy/tools/sessions/test_compact_self.py::test_refresh_digests_pending_turn_before_fallback`.
- 1.4.3 - `HookManager._dispatch_session_summaries` wires the memory manager and config into the dispatcher. symbol: `HookManager._dispatch_session_summaries`.

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
   `gobby-tasks:close_task {"task_id": "#777", "commit_sha": "abc1234"}`, then narrates
   "Done."
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
  `mcp gobby-tasks:close_task task_id=#777 commit_sha=abc1234`, and `→ commit abc1234`;
  and the Turn-2 response ledger contains `mcp gobby-sessions:compact_self`.
- `test_summary_ground_truth_for_every_cli`: with a digest present (seeded through the
  fake LLM) and the fixture on disk, `_generate_session_summary_core` passes the analyzer
  transcript turns, and the captured summary prompt contains `src/pkg/widget.py` under
  files changed, `abc1234` under commits, `#777` with `claim_task` and `close_task` under
  task progress, and `compact_self` in recent activity.

**Acceptance:**

- 1.5.1 - Five native-envelope fixtures exist, each with the edit, shell command, task claim/close, commit with result, and final compact_self turn. file: `tests/sessions/transcripts/fixtures/golden_path/grok.jsonl`.
- 1.5.2 - The parametrized parser→digest test asserts path, command, task ref/action, commit SHA, and final-turn fact in the ledger for every CLI with pair count and role sequence preserved. test: `tests/sessions/test_activity_golden_path.py::test_digest_pairs_carry_activity_for_every_cli`.
- 1.5.3 - The parametrized summary test asserts the same facts reach the summary prompt's ground truth for every CLI. test: `tests/sessions/test_activity_golden_path.py::test_summary_ground_truth_for_every_cli`.

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
- `src/gobby/workflows/state_manager.py::SessionVariableManager`
- `tests/sessions/test_compact_handoff_block.py`
- `tests/workflows/test_session_variable_manager.py::*` — scope-reason: add claim_compact_continuation cases
- `tests/mcp_proxy/test_mcp_tools_session_messages.py::*` — scope-reason: add continuation-block and one-shot-clearing cases

New pure module `compact_handoff_block.py`:

```python
COMPACT_CONTINUATION_ONE_SHOT_VARIABLES = (
    "compact_handoff_inject_pending",
    "compact_resume_required_skills",
    "compact_resume_advisory_skills",
)

def render_compact_continuation_block(variables: dict[str, Any]) -> str:
    """Render the same content as the retired inject-compact-handoff-on-prompt template.

    Sections, each omitted when empty:
    - "## Durable Tool-Call Evidence": variables["mcp_calls"] as ``- `server`: tool, tool``
      with the same preamble sentence as the SessionStart template.
    - "## Required Skill Reload": skill_fetch_batch_directive(required - loaded_skills).
    - "## Advisory Skill Reload": bullet list of advisory - loaded_skills.
    - variables["task_context"] verbatim.
    - "## Global User Profile": variables["user_profile_content"] unless
      variables.get("is_spawned_agent").
    Wrapped in ``<!-- gobby:injected-context:begin -->`` / ``end`` markers under
    "## Continuation Context". Returns "" when every section is empty.
    """
```

`skill_fetch_batch_directive` comes from `gobby.skills.formatting`;
`SessionVariableManager` (with `get_variables` and `merge_variables`) comes from
`gobby.workflows.state_manager`.

Capture-and-clear is one atomic mutation on `SessionVariableManager`, following the
`claim_startup_context` precedent (`_mutate_variables` runs under
`transaction_immediate` with the per-session `SessionVariableMutation` lock, so
concurrent callers serialise and exactly one wins):

```python
def claim_compact_continuation(self, session_id: str) -> dict[str, Any] | None:
    """Atomically consume the one-shot compact continuation.

    When ``compact_handoff_inject_pending`` is truthy: snapshot the variables, set
    ``compact_handoff_inject_pending=False``, ``compact_resume_required_skills=[]``,
    ``compact_resume_advisory_skills=[]`` in the same mutation, and return the
    pre-clear snapshot. Otherwise return ``None`` and write nothing.
    """

    def mutate(variables: dict[str, Any]) -> tuple[dict[str, Any] | None, bool]:
        if not variables.get("compact_handoff_inject_pending"):
            return None, False
        snapshot = dict(variables)
        variables["compact_handoff_inject_pending"] = False
        variables["compact_resume_required_skills"] = []
        variables["compact_resume_advisory_skills"] = []
        return snapshot, True

    return self._mutate_variables(session_id, mutate)
```

`_handoff.py` gets one module-level helper that both tools share:

```python
def _attach_compact_continuation(result: dict[str, Any], session_manager: Any, resolved_id: str) -> None:
    variables = SessionVariableManager(session_manager.db).claim_compact_continuation(resolved_id)
    if variables is None:
        return
    block = render_compact_continuation_block(variables)
    if block:
        result["continuation"] = block
```

`wait_for_summary` calls it on every `completed: true` return (including the stale/live
branch) and `get_handoff_context` on every `found: true` return, after the response dict
is built. Nothing is claimed on `completed: false` timeouts or `found: false`. Sessions
without the pending flag (Claude/Codex, or Grok sessions already served) get no
`continuation` key and no variable writes. `apply_in_place_compact_context_loss` keeps
arming the flag on Grok PostCompact; nothing else changes there.

**Acceptance:**

- 2.1.1 - `render_compact_continuation_block` renders MCP ledger, required/advisory skill directives, task context, and profile, and returns "" when all are empty. test: `tests/sessions/test_compact_handoff_block.py::test_render_compact_continuation_block_sections`.
- 2.1.2 - `wait_for_summary` returns `continuation` once for a session with `compact_handoff_inject_pending` set and clears the three one-shot variables; a second call returns no `continuation`. test: `tests/mcp_proxy/test_mcp_tools_session_messages.py::test_wait_for_summary_delivers_compact_continuation_once`.
- 2.1.3 - `completed: false` and `found: false` responses never clear the one-shot variables. test: `tests/mcp_proxy/test_mcp_tools_session_messages.py::test_wait_for_summary_timeout_keeps_continuation_pending`.
- 2.1.4 - `get_handoff_context` carries the same block under the same conditions. symbol: `get_handoff_context`.
- 2.1.5 - `claim_compact_continuation` returns the pre-clear snapshot once, clears the three one-shots in the same mutation, and returns `None` (with no write) afterwards and for sessions that were never pending. test: `tests/workflows/test_session_variable_manager.py::test_claim_compact_continuation_is_one_shot`.
- 2.1.6 - Concurrent `wait_for_summary` and `get_handoff_context` calls (two threads, one pending session) yield exactly one response with `continuation` and the one-shots are cleared once. test: `tests/mcp_proxy/test_mcp_tools_session_messages.py::test_compact_continuation_consumed_exactly_once_under_concurrency`.

### 2.2 Route the Grok continuation prompt to wait_for_summary and retire the dead turn_start rule [category: code] (depends: 2.1)
`kind: deliverable`

Targets:
- `src/gobby/sessions/compact_handoff_block.py`
- `src/gobby/sessions/compact_continuation.py::build_compact_self_continue_prompt`
- `src/gobby/sessions/compact_continuation.py::_build_wait_for_summary_directive`
- `src/gobby/sessions/compact_continuation.py::schedule_compact_self_continuation`
- `src/gobby/sessions/compact_continuation.py::consume_and_schedule_compact_self_continuation`
- `src/gobby/sessions/compact_continuation.py::_take_same_terminal_compact_self_continuation_pending`
- `src/gobby/install/shared/workflows/rules/context-handoff/inject-compact-handoff.yaml::*` — scope-reason: remove the inject-compact-handoff-on-prompt rule from the bundled rule file
- `src/gobby/install/bundled_content_manifest.json::*` — scope-reason: regenerated checksum manifest for changed bundled content
- `tests/sessions/test_compact_continuation.py::*` — scope-reason: add Grok-directive cases
- `tests/sessions/test_compact_handoff_block.py`
- `tests/workflows/test_context_handoff_rules.py::*` — scope-reason: retire the on-prompt rule tests and assert authoritative sync removes the row

`compact_continuation.py` is at 952 lines, so the directive text moves out of it. Add
to `compact_handoff_block.py` (created in 2.1):

```python
def wait_for_summary_directive(summary_session_id: str, *, source: str | None) -> str:
    """Directive body appended after COMPACT_SELF_CONTINUE_INTRO.

    source == "grok":
        'Call `gobby-sessions.wait_for_summary(session_id="<id>")` now. If it returns '
        '`completed=false`, repeat the same wait call. Once complete, use the returned '
        '`context` as your handoff and follow every instruction in the returned '
        '`continuation` block before continuing.'
    every other source: today's text verbatim — 'If startup context contains '
        '`<!-- gobby:injected-context:begin -->`, use that injected context directly and '
        'continue. Only if the injected context is missing or incomplete, call '
        '`gobby-sessions.wait_for_summary(session_id="<id>")`. If it returns '
        '`completed=false`, repeat the same wait call. Once complete, use the returned '
        '`context` and continue.'
    """
```

In `compact_continuation.py`, `_build_wait_for_summary_directive(summary_session_id, *,
source)` becomes `COMPACT_SELF_CONTINUE_INTRO + wait_for_summary_directive(...)` when a
session id is present (else `COMPACT_SELF_CONTINUE_PROMPT`, unchanged), and
`build_compact_self_continue_prompt(*, summary_session_id=None, source=None)` forwards
`source`. The three callers that build the prompt (`schedule_compact_self_continuation`,
`consume_and_schedule_compact_self_continuation`,
`_take_same_terminal_compact_self_continuation_pending`) pass the session's `source`.
The prompt stays a single line. Net change to `compact_continuation.py` is the `source`
plumbing plus the delegation; it must finish under 1,000 lines.

Remove the `inject-compact-handoff-on-prompt` rule from `inject-compact-handoff.yaml`
(keep `inject-compact-handoff`), regenerate `bundled_content_manifest.json` with the
command from 1.2, and rely on authoritative sync to retire the DB row (precedent:
`test_authoritative_sync_retires_prepare_clear_handoff`). Delete the rule's tests and add
one asserting the rule is absent after sync.

**Acceptance:**

- 2.2.1 - The Grok directive contains no injected-context clause and names `continuation`; the Claude/Codex directive is unchanged. test: `tests/sessions/test_compact_continuation.py::test_grok_continue_prompt_routes_to_wait_for_summary`.
- 2.2.2 - Every continuation sender passes the session source into the prompt builder. symbol: `schedule_compact_self_continuation`.
- 2.2.3 - `inject-compact-handoff-on-prompt` no longer exists in the bundled rules and authoritative sync retires its row. test: `tests/workflows/test_context_handoff_rules.py::test_authoritative_sync_retires_inject_compact_handoff_on_prompt`.
- 2.2.4 - `wait_for_summary_directive` lives in `compact_handoff_block.py` and `compact_continuation.py` stays under 1,000 lines. test: `tests/sessions/test_compact_handoff_block.py::test_wait_for_summary_directive_by_source`.

### 2.3 Document Grok compact handoff delivery [category: docs] (depends: 2.2)
`kind: deliverable`

Targets:
- `docs/guides/sessions.md`
- `docs/guides/adapter-fidelity.md`

Rewrite the Grok paragraph in `sessions.md` (the one that currently says the next
`turn_start` fires `inject-compact-handoff-on-prompt`): Grok consumes no passive hook
stdout, so Grok `post_compact` arms `compact_handoff_inject_pending`, the daemon-typed
continuation prompt calls `wait_for_summary`, and that tool's response carries the
summary plus the `continuation` block (MCP ledger, skill reload, task context) and clears
the one-shots. In `adapter-fidelity.md`, annotate the Grok row's context-channel cell
with "verified 2026-08-20: ignored by Grok 1.0.5; see #20635" so the table stops
asserting delivery that does not happen.

**Acceptance:**

- 2.3.1 - The sessions guide describes the wait_for_summary delivery path and no longer references the retired rule. behavior: "Grok compact continuation" in `docs/guides/sessions.md`.
- 2.3.2 - The adapter-fidelity table flags the Grok context channels as unverified-in-practice with the task reference. behavior: "Grok context channel" in `docs/guides/adapter-fidelity.md`.

## 3 Grok-wide context channel
`kind: deferred`

```yaml
deferral:
  task_ref: "#20635"
  reason: "Every other inject_context effect (role, wiki, skill directives, memory recall, task context, tool-error recovery, context-pressure nudges) is equally undelivered on Grok. Fixing it means correcting _grok_capabilities() to ContextChannel.NONE and building a NONE-channel delivery path through MCP call_tool results — a separate design."
  owner: "gobby"
  original_acceptance_items:
    - 2.1.4
```

## 4 Verification
`kind: verification`

- The 1.5 parity suite is the primary regression: one fixture per CLI through parser →
  digest → summary, asserting path, command, task ref/action, commit SHA, and final-turn
  fact, with pair count and role sequence preserved.
- Replay `tests/sessions/transcripts/fixtures/grok_audit/10725/updates.jsonl` (and a
  copy of #10854's 185-call transcript added as a fixture) through `build_turn_and_digest`
  against an isolated test database: the turn record must name the edited test file, the
  `claim_task`/`close_task` calls, and the pytest command, and the `close_task` line must
  survive the 80-line cap; regenerate the summary and confirm "Files Changed" and "What
  Was Accomplished" are populated.
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

## Task Mapping
`kind: framing`

<!-- Updated after task creation -->
| Plan Item | Task Ref | Status |
|-----------|----------|--------|
