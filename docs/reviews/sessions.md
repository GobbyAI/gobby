# Review: sessions

- **Scope:** `src/gobby/sessions/` — processor (`processor.py`), transcript parsers
  (`transcripts/`: claude, gemini, codex, grok, droid, qwen, base, hook_assembler),
  lifecycle (`lifecycle.py`, `liveness_monitor.py`, `terminal_kill.py`,
  `handoff_identity.py`, `tmux_context.py`), summary/handoff content (`summarize.py`,
  `analyzer.py`, `compact_continuation.py`, `context_usage.py`, `formatting.py`,
  `message_stats.py`, `token_tracker.py`, `token_usage.py`, `model_family.py`),
  transcript indexing/serving (`transcript_index.py`, `gzip_seek_index.py`,
  `transcript_window.py`, `transcript_reader.py`, `transcript_index_resume.py`,
  `transcript_io.py`), rendering/mailbox/misc (`mailbox.py`, `transcript_renderer.py`,
  `transcript_status.py`, `transcript_normalization.py`, `transcript_paths.py`,
  `transcript_source.py`, `transcript_parsing.py`, `transcript_archive.py`,
  `transcript_search.py`), plus the sessions↔storage seam (`storage/sessions/`,
  `storage/session_lifecycle.py`, `storage/inter_session_messages.py`,
  `storage/chat_messages.py`, `storage/session_resolution.py`). Cross-seam reads into
  hooks session layer, agents lifecycle, servers routes, runner maintenance.
  **Split boundary:** hooks-side session handling was reviewed in #15778; adapters in
  #15781. Several reviewers verified parser behavior **empirically against real
  on-disk transcripts** (`~/.claude/projects`, `~/.qwen/projects`, `~/.gemini/tmp`,
  `~/.codex/sessions`, `~/.grok/sessions`, `~/.factory/sessions`).
- **Reviewer:** Claude Fable 5 — 7-agent parallel fan-out, all Blockers synthesizer-verified.
- **Commit / branch:** `0.5.0` @ HEAD `bad39fcba` (working tree clean at review time).
- **Summary:** 10 Blocker · 44 Important · 20 Nit — the transcript parsers were built
  against assumed formats rather than captured ones (queued user prompts invisible,
  Qwen parsing entirely broken, Gemini token usage 100% lost, Codex tokens
  double-counted), the processor's commit points straddle failure boundaries, the
  liveness/kill layer repeats the wrong-signal and unverified-PID classes from the
  agents review, and the mailbox/storage seam has no atomic claim, no status state
  machine, and seq_num integrity only at INSERT.

## Findings

### [BLOCKER] Queued user prompts are invisible — `attachment`/`queued_command` entries silently dropped by the Claude parser
- **Where:** `sessions/transcripts/claude.py:370-524` (`_expand_line`) and `:542-659` (`parse_line`) handle only `user`/`assistant`/`tool_result` (+ the dead `hook_blocking_error` shape); `attachment` has zero handling (verified: no match in the file); no `log_unknown_block` call anywhere in claude.py.
- **Failure mode:** A message the user types while the agent is working is recorded by Claude Code 2.1.x as `queue-operation` + `attachment/queued_command` — there is **no** `type:"user"` entry. Empirically verified against this repo's own transcripts: the operator directive "I'm going to bed. Just keep the work in Fable please." exists only in those shapes; 4 such delivered prompts found across 120 recent transcripts. Gobby's turn counts, digests, handoffs, summaries, and `extract_last_messages` all omit real user instructions the model acted on.
- **Why it matters:** A whole class of genuine user input is invisible to every downstream consumer.
- **Minimal fix:** Handle `attachment.type=="queued_command"` → user-role ParsedMessage from `attachment["prompt"]`; route unknown types through `error_log.log_unknown_block` (codex and droid already do).
- **Confidence:** high (empirical).

### [BLOCKER] Qwen parser does not match the Qwen CLI's actual on-disk format — assistant content, tool calls, and usage all dropped
- **Where:** `transcripts/qwen.py:1-24` (inherits everything from Gemini); `transcripts/gemini.py:203-345` (`parse_line` dispatch: `type:"assistant"` falls to the else-branch → None; `type:"user"` reads `data.get("content")` but qwen stores `message.parts`; `type:"tool_result"` reads keys that don't exist).
- **Failure mode:** Real qwen chat files (verified on disk, CLI 0.14.4 and 0.17.0) use a Claude-style envelope `{type, message:{role, parts:[{text}|{functionCall}|{functionResponse}]}, usageMetadata, model}`. The inherited parser yields zero assistant content, zero tool calls, zero usage, `turn_count=0`, empty `extract_last_messages` for every qwen session — summaries, handoffs, stats, liveness, cost all systematically wrong. The only qwen tests use a synthetic shape the CLI never writes.
- **Minimal fix:** Give `QwenTranscriptParser` a real `parse_line` for the envelope; add a fixture captured from a real `~/.qwen/projects/*/chats/*.jsonl`.
- **Confidence:** high (empirical, two CLI versions).

### [BLOCKER] Gemini native session-JSON token usage is 100% lost — the `tokens` shape is unmapped
- **Where:** `transcripts/gemini.py:347-354` (`_extract_usage`: `data.get("usageMetadata") or data.get("tokens")`) feeding `token_usage.py:33-67` (`gemini_token_usage` reads only `promptTokenCount`/`cachedContentTokenCount`/`candidatesTokenCount`/`thoughtsTokenCount`).
- **Failure mode:** Real Gemini session files store usage as `tokens: {input, output, cached, thoughts, tool, total}` — surveyed 3,083 real messages: **zero** had `usageMetadata`. The fallback exists for exactly this shape but feeds an incompatible key reader → `TokenUsage(0,0,0,0)` → consumers skip all-zero usage → no token events are ever recorded from Gemini/Qwen transcripts (the backfill/recovery path records nothing). Relations verified on real data: `total = input + output + thoughts`, `cached ⊆ input`.
- **Minimal fix:** Map the session-file shape (`input - cached`, `cache_read=cached`, `output + thoughts`); add a real-shape test.
- **Confidence:** high (empirical + two independent reviewer reads).

### [BLOCKER] Codex output tokens double-count reasoning tokens
- **Where:** `transcripts/codex.py:337-342` — `output_tokens=output_tokens + reasoning_output_tokens` (verified at `:339`).
- **Failure mode:** In real Codex `token_count` events, `output_tokens` already includes reasoning (verified: `total = input + output` exactly, with reasoning a subset of output). Every reasoning-bearing turn over-reports output; reasoning-heavy turns nearly double it. The unit test codifies the wrong math (`tests/sessions/test_transcript_parsers.py:1539-1560`).
- **Minimal fix:** Use `output_tokens` as-is; fix the test.
- **Confidence:** high (empirical).

### [BLOCKER] Processor mid-batch exception permanently drops the batch and desynchronizes the index streams — dedup then eats the next batch's data
- **Where:** `processor.py:797-799` (byte offset advanced before any fallible work — verified with its comment), `:840-853` (sync DB writes + `_persist_usage_events` can raise), `:879` (`_message_indices` advanced only after), exception swallowed per-session at `:719-721`; sidecar appender's independent counter advances at `:805-810`.
- **Failure mode:** A transient DB error mid-batch: bytes already consumed → the batch's token events, rendered messages, and broadcasts are permanently lost; `_message_indices` didn't advance but the appender's counter did → the two index streams diverge permanently; the next batch re-uses index ranges already assigned, and fallback `message_id`s collide → `ON CONFLICT ... DO NOTHING` silently drops the *next* batch's usage events too. The `.json` path has the opposite defect: stats accumulate before the fallible write and re-accumulate on retry (double-counted permanently, `:943` vs `:947-987`).
- **Why it matters:** One DB hiccup converts to permanent token-event loss plus corrupted index identity, with the dedup mechanism amplifying the loss.
- **Minimal fix:** Advance `_message_indices` at the same point as `_byte_offsets`; commit offset+index after `_persist_usage_events` (or snapshot/restore accumulators on exception).
- **Confidence:** high (mechanism); med (trigger frequency).

### [BLOCKER] `_list_tmux_panes` treats tmux non-zero exit as "zero live panes" — mass-expiry of live sessions
- **Where:** `liveness_monitor.py:343-368` — `subprocess.run` (verified `:356-361`) with `returncode` never inspected anywhere in the file (verified: zero matches); only exceptions return the `None` sentinel; `_check_sessions:152-168` guards only `None`.
- **Failure mode:** tmux exiting non-zero with empty stdout (socket permission error, client/server version mismatch after a brew upgrade, transient connect failure) returns an **empty set** — treated as authoritative — and every tmux-backed session on that socket is expired in one 30s sweep, with summaries dispatched against live sessions and statuses stomped. The test suite simulates "server not running" as an exception, which is not what tmux actually does.
- **Minimal fix:** Treat `returncode != 0` as failure → `None`; add a `returncode=1, stdout=""` test.
- **Confidence:** high (mechanics); med (trigger frequency).

### [BLOCKER] `kill_terminal_session` PID fallback SIGTERMs an unverified, possibly recycled PID
- **Where:** `terminal_kill.py:74-86` (verified: tmux kill-pane first, then `parent_pid = terminal_ctx.get("parent_pid")` → `os.kill(pid, SIGTERM)` with no identity verification); fired automatically by the web-chat resume flow (`servers/websocket/handlers/session_observe.py:284`) as well as the kill route.
- **Failure mode:** `parent_pid` can be days old; PID recycling means the daemon SIGTERMs an unrelated process. The tmux path falls through to the PID kill on any non-"pane gone" tmux failure (wrong socket), so the unverified kill can fire while the pane is alive. Same unverified-PID family as the agents review Blocker; this is the sessions-side instance, with automated invocation.
- **Minimal fix:** Record process identity (create_time/name) at capture and verify before kill; refuse the fallback when a pane is recorded but its kill failed for a non-"gone" reason.
- **Confidence:** high (mechanics); low-med (per-kill probability).

### [BLOCKER] `deliver_pending_messages` lets any caller drain another session's mailbox — destructive cross-session read plus message loss
- **Where:** `mcp_proxy/tools/agent_messaging.py:257-271` (verified: arbitrary `target_session_id` resolved, undelivered fetched, each `mark_delivered`, full content returned to the caller); `storage/inter_session_messages.py:364-386` (`mark_delivered(message_id)` — no recipient scoping, no `AND delivered_at IS NULL`).
- **Failure mode:** No check that the caller *is* the target. The rightful recipient's delivery paths all key on `delivered_at IS NULL`, so drained messages are permanently lost to them — P2P command results and coordination messages silently vanish, disclosed to the wrong session.
- **Minimal fix:** Default `target_session_id` to the caller's session and reject mismatches; add a recipient-scoped claim API.
- **Confidence:** high.

### [BLOCKER] `bulk_move_sessions` ignores seq_num uniqueness — one collision rolls back the whole batch while reporting success
- **Where:** `servers/routes/sessions/lifecycle.py:110-133` (verified `:117-121`: raw `UPDATE sessions SET project_id` per session, no renumbering, per-session `except` inside one shared `db.transaction()`); unique index `idx_sessions_seq_num(project_id, seq_num)` (schema:262).
- **Failure mode:** seq_nums are dense per-project from 1, so a moved session's seq almost certainly exists in the destination → `UniqueViolation` → in Postgres the whole transaction is aborted; subsequent statements raise `InFailedSqlTransaction` (swallowed), earlier "successful" moves roll back at block exit — yet the route returns `{"status": "success", "moved": N}` and broadcasts `session_updated` for sessions that never moved.
- **Minimal fix:** Per-session renumbering under `SessionSeqMutation` with savepoints (or one transaction per session); report only committed moves.
- **Confidence:** high.

### [BLOCKER] `register()` cross-project recovery moves a session without renumbering — registration fails and tracking is silently dropped
- **Where:** `storage/sessions/_crud.py:145-149` (verified: `UPDATE sessions SET project_id = %s` keeping the old seq_num; the seq lock exists only in the INSERT branch); the conflict matcher recognizes only `idx_sessions_unique`, so the seq violation isn't recovered; `_recover_registered_session_after_failure` rejects the personal-project row and returns `""` — the session-start hook proceeds with no tracked session.
- **Failure mode:** The designed recovery path (session seen pre-project-init, then recovered into the real project) silently loses session tracking whenever the destination project has ≥ old-seq sessions.
- **Minimal fix:** Acquire `SessionSeqMutation(project_id)` and assign `MAX(seq_num)+1` in the recovery UPDATE.
- **Confidence:** high.

### [IMPORTANT] No truncation/replacement detection on the JSONL tail — silent stall, misaligned reads, and a durably poisoned index sidecar
- **Where:** `processor.py:739-799` (no `st_size < last_offset` check; appender stat at `:803-811` has the data, no shrink branch); downstream `transcript_index.py:809-853` (sidecar validated only on `(mtime_ns, size, ...)`) and `transcript_window.py:342-346` (seeks).
- **Failure mode:** File shrinks → seek past EOF → empty reads forever (no log). File replaced with longer content → seek lands mid-line of unrelated bytes → garbage/misattributed ingestion, and once `valid_offset == st_size` the processor persists a sidecar whose stat matches the live file but whose boundaries point into old-content positions — readers then serve torn JSON as transcript pages, durably (survives restarts) until the file changes again.
- **Minimal fix:** Stat before reading; on shrink/mtime regression, reset offsets/parser/appender and discard the sidecar.

### [IMPORTANT] `register_session` ignores a changed `transcript_path`; `flush_session` offers no guarantees and has zero tests
- **Where:** `processor.py:628-629` (early return keeps polling the old path while `flow.py:518-521` updated the DB row); `:660-668` (flush: silent no-op when unregistered; an unterminated final line is never processed — no EOF mode; exceptions propagate raw; no test exercises it).
- **Failure mode:** Web-chat reattach/resume points the session at a new file the processor never reads; SESSION_END flush callers read stale stats believing them flushed.
- **Minimal fix:** Re-register on path change; return a result; add `at_eof=True` handling; test it.

### [IMPORTANT] Processor concurrency: flush racing the poll loop double-ingests (.json) and corrupts index streams (.jsonl); post-await state resurrection after unregister
- **Where:** `processor.py:853-879/959-987` (state committed after awaits with no per-session lock), `:670-684`.
- **Failure mode:** Latent today only because the lone flush call site is the dead coordinator path filed in #15778 — **fixing that filed bug activates this one.** A per-session `asyncio.Lock` around `_process_session` closes the class.

### [IMPORTANT] Claude duplicate-tool_result skip breaks at poll-batch boundaries; `parser_safe` poisoning defeats windowed rendering
- **Where:** `claude.py:720-722,753-792` (1-line lookahead can't see across batches → the duplicate ingests next poll: inflated counts, shifted indices); `claude.py:772` (`parser_safe=not peek` marks every non-final event unsafe → cold-built indices have no mid-file resume points; `transcript_window.py:124-129` falls back to group 0 → O(file) re-renders for the most common source).
- **Minimal fix:** Hold back the final line when `max_lookahead > 0` (or commit only through `parser_safe` events); yield `parser_safe=not skip_next`.

### [IMPORTANT] Compaction is unhandled by the Claude parser; `isMeta` and system entries pollute or vanish
- **Where:** `claude.py:429-456` (no `isCompactSummary` check — the multi-KB continuation blob parses as a genuine user message; `compact_boundary` system entries dropped with their `preTokens` signal); same lines (`isMeta` hook-feedback/caveat entries counted as real user messages — `analyzer.py:174-178` takes them as the session's initial goal); `:471-496` (system `api_error`/`model_refusal_fallback`/assistant `fallback` blocks dropped — a retry storm looks idle; model switches invisible).
- **Minimal fix:** Reclassify `isCompactSummary`/`isMeta`; surface compact_boundary as a boundary; emit system records for error/fallback shapes.

### [IMPORTANT] `is_session_boundary` substring match fires on quoted `/clear` markers inside tool results
- **Where:** `claude.py:265-289` (`"<command-name>/clear</command-name>" in str(content)` over the whole block list — any session that reads/greps claude.py itself, like review sessions, creates a false boundary truncating digests/handoffs).
- **Minimal fix:** Match only string-content user entries; anchor with the command-message sibling.

### [IMPORTANT] Shape-level malformed lines wedge a session's processing permanently
- **Where:** `claude.py:341-364` (`_parse_data` catches only JSONDecodeError; non-string timestamp → AttributeError; `"text": null` → TypeError at join), `gemini.py:407-427` (session-JSON loop has no per-entry tolerance; same timestamp gap); consumer `processor.py:786` parses before advancing the offset → re-crash every poll forever.
- **Minimal fix:** Broaden the catch tuples; guard per-entry; coerce `block.get("text") or ""`.

### [IMPORTANT] Dead hook-block machinery validated by an invented fixture; `HookTranscriptAssembler` never invoked
- **Where:** `claude.py:85-142,720-792` (top-level `hook_blocking_error` shape does not exist in Claude Code 2.x — zero hits in 200 real transcripts; the real shape is an `attachment`; the collapse/lookahead machinery serves a dead case and its test fabricates the shape); `hook_assembler.py` (constructed at `factory.py:240`, `process_event` has zero call sites; if revived: no tool_use_id, in-memory index restarts collide with message_id-keyed dedup).
- **Minimal fix:** Re-target detection at the attachment shape; delete or wire the assembler; rewrite fixtures from captured transcripts.

### [IMPORTANT] Droid sidecar usage: zero-usage latch in live sessions; cumulative totals re-recorded per batch on resume
- **Where:** `droid.py:74-76` (cache guard latches `TokenUsage(0,0,0,0)` when the sidecar has model but null tokenUsage — verified real sidecars are null mid-session → live droid sessions record no usage until expiry), `:287-292` (finalize attaches cumulative totals to the last assistant message of *each* batch → re-added once per batch after restart/resume); `:106-111` (`thinkingTokens` dropped entirely — 58% undercount in a sampled real session).
- **Minimal fix:** Don't latch zero; re-read per call; emit deltas; fold thinkingTokens into output.

### [IMPORTANT] Codex synthetic `token_count` messages inflate turn/message stats ~3x; `extract_last_messages` returns instruction dumps
- **Where:** `codex.py:350-363` (each token_count event becomes an empty assistant "text" message — `message_stats.py` counts every one as a turn; real rollout: 32 events vs 17 actual turns); `codex.py:111-118` (system/developer roles and `<user_instructions>`/AGENTS.md synthetic user dumps pass the last-messages filter).
- **Minimal fix:** Dedicated `content_type="usage"` excluded from stats and rendering; filter wrapper messages.

### [IMPORTANT] Gemini session-JSON tool results hardcode `status:"success"`; Grok emits a tool_result per status update
- **Where:** `gemini.py:566` (328 real failed tool calls misreported as success — no is_error equivalent); `grok.py:104-117` (2-6 updates per call each emit a full tool_result → stats inflated, partial outputs recorded; terminal-status filtering absent).
- **Minimal fix:** Carry `tc["status"]`; emit only terminal updates.

### [IMPORTANT] `agy` source missing from the parser registries — live processing uses the Claude parser on Gemini-format files
- **Where:** `transcripts/__init__.py:26-55` and `transcript_parsing.py:30-53` (no agy entry; fallback ClaudeTranscriptParser) vs `lifecycle.py:541-542,609` (correctly maps agy → Gemini). Live stats/rendering broken for agy; the expiry backfill disagrees with the live path. Both registries also silently default unknown sources to Claude — a new CLI mis-parses quietly.
- **Minimal fix:** Map agy in both; fail loudly on unknown sources.

### [IMPORTANT] `mark_recently_handled` is dead code — the session_end/liveness dedup guard never runs
- **Where:** `liveness_monitor.py:109-115` (zero production callers); `_expire_session:399` writes `expired` with no status recheck.
- **Failure mode:** The monitor can dispatch a duplicate summary racing the hook's own, and overwrite a just-set `handoff_ready` with `expired` — breaking child handoff pickup in the normal-exit path the guard was designed for.
- **Minimal fix:** Wire it from the hook session_end path; make expiry a conditional UPDATE (`WHERE status IN ('active','paused')`).

### [IMPORTANT] Lifecycle summary gaps: transcript-missing skips the digest-backed summary; `skip_llm` decided from stale pre-parse stats
- **Where:** `lifecycle.py:397-405` (file-missing branch marks processed without attempting `_generate_summaries_if_needed`, contradicting the docstring contract at `:362-369`); `:386-389` (`turn_count` derived only from digest_markdown — digest-less crash sessions get no summary even though Step 1 just parsed real turns).
- **Minimal fix:** Attempt summary generation in the file-missing branch; base skip_llm on refreshed turn_count.

### [IMPORTANT] `backup_transcript` failure cleanup deletes a known-good archive; `restore_transcript` misses `EOFError`/`zlib.error`
- **Where:** `transcript_archive.py:63-69` (the writer is atomic, so `dest` at except-time is the *previous good archive* or a *newly completed* one — the unlink destroys it; found independently by two reviewers); `:103-114` (truncated gzip raises EOFError — not caught — after partial members were already written to `target`: the partial "live" file then permanently shadows the intact-prefix archive via the `target.is_file()` no-op guard).
- **Minimal fix:** Drop the unlink; catch `(EOFError, zlib.error, BadGzipFile, OSError)` so cleanup runs.

### [IMPORTANT] Empty-session fast-expire + hard prune can delete real sessions whose stats were never recorded
- **Where:** `storage/session_lifecycle.py:150-230`; the only pre-expiry `message_count` writer is the processor (gated on message tracking + a registered transcript), and the post-expiry backfill early-returns before `update_stats` when zero messages parse (`lifecycle.py:570-578`) — a parser regression makes every affected session "empty", then pruned (history, summaries, lineage hard-deleted).
- **Minimal fix:** Require `transcript_path IS NULL` (or `transcript_processed`) for pruning.

### [IMPORTANT] Age-based expiry of context-less terminal sessions targets recently-active sessions by construction
- **Where:** `storage/session_lifecycle.py:61-80` — the second disjunct (`terminal_context IS NULL AND created_at > 24h`) ignores `updated_at`, so its *marginal* effect is exclusively sessions with fresh activity (headless `claude -p`, CI, no-tty agents) — expired mid-use every sweep, then status-thrashed by revival paths. Found independently by two reviewers.
- **Minimal fix:** AND the branch with the `updated_at` staleness predicate.

### [IMPORTANT] Revival racing transcript finalization permanently clobbers `transcript_processed=FALSE`
- **Where:** `lifecycle.py:393-433` (multi-second pipeline ends in unconditional `mark_transcript_processed`) vs `_field_update.py:65-77` (revive sets FALSE). A session revived mid-finalization that gets no further prompt never has its tail processed — silent summary/memory loss.
- **Minimal fix:** `... SET transcript_processed = TRUE WHERE id = %s AND status = 'expired'`.

### [IMPORTANT] The summary "validity gate" accepts any non-empty text; `handoff_ready` set even on total failure
- **Where:** `summary_validity.py:5-24` (rejects only empty + two legacy sentinel prefixes — refusals, truncations, provider garbage all pass and get injected into the next session); `summarize.py:209-219` (when both LLM and deterministic fallback produce nothing, `update_status("handoff_ready")` still runs and `{"success": True, "full_length": 0}` is returned — the child consumes a stale or absent summary as the handoff).
- **Minimal fix:** Length floor + refusal prefixes + required structural markers; gate handoff_ready and `success` on validity.

### [IMPORTANT] Validated-path git context is wrong-repo twice over
- **Where:** `summarize.py:186-187` (`cwd = transcript.parent` — `~/.claude/projects/...`, not a repo) and `:514-515` (`get_file_changes()`/`get_git_diff_summary()` with no path → daemon cwd). Handoffs lose or fabricate the load-bearing git facts on every run; distinct call sites from the summary_actions instance filed in #15776.
- **Minimal fix:** Thread the session's project path into both.

### [IMPORTANT] Analyzer is blind to gobby-task activity on the primary CLI; consumer schema/ordering drift renders wrong content
- **Where:** `analyzer.py:250` (`tool_name == "mcp_call_tool"` only — Claude transcripts record `mcp__gobby__call_tool`, which `:324` knows; `active_gobby_task` is always None, the handoff "Active Task" section never renders); `:196-205` vs `formatting.py:130` (`recent_activity` is newest-first; the `[-5:]` slice selects the five *oldest* of the recent ten); `:298-305` vs `formatting.py:89-90` (`git_commits` `{command,timestamp}` vs consumer `{hash,message}` → blank bullets).
- **Minimal fix:** Match both tool names; pick one ordering convention; align the commit schema.

### [IMPORTANT] Static context-window table contradicts real model limits
- **Where:** `llm/context_windows.py:73-82` (`claude-sonnet-4-6: 200_000` — Sonnet 4.6 is 1M per current docs; the `[1m]` marker stripper resolves explicit 1M tiers to the small tier, contradicting its own comment; `claude-fable-5` has no entry and no family token → None), consumed by `context_usage.py:79-115` and the backfill.
- **Failure mode:** With an empty registry (fresh install, offline), Sonnet sessions over-report context usage 5× → spurious compaction pressure; the backfill can never repair them.
- **Minimal fix:** Correct the entries; resolve `[1m]` to the 1M tier; add fable.

### [IMPORTANT] Mailbox/messages storage: no atomic claim, no ordering, no retention, fence bypass, non-transactional fanout
- **Where:** `storage/inter_session_messages.py:288-386` (get-then-mark with no `AND delivered_at IS NULL`, no rowcount — three concurrent consumers double- or zero-deliver; found by two reviewers); `:224-233` (`get_messages` has no ORDER BY — the only sibling without one); no `DELETE FROM inter_session_messages` exists anywhere (unbounded growth; the zombie sweep merely stamps delivered); `mailbox.py:216-231,458-471` (`target='all'` selects every session in **every** project — the one selector that bypasses the cross-project fence the module otherwise enforces); `mailbox.py:156-170` (per-recipient INSERTs outside a transaction — partial fanout, duplicate rows on retry); `servers/websocket/chat/_pending_messages.py:39-70` (second un-filed instance of mark-before-deliver — a formatting exception permanently loses marked messages).
- **Minimal fix:** Atomic claim (`UPDATE ... WHERE to_session=%s AND delivered_at IS NULL RETURNING *`); ORDER BY; retention sweep; project-scope `all`; transactional fanout; mark-after-build.

### [IMPORTANT] `session_coordinator` result fallback queries a nonexistent column — silently dead
- **Where:** `hooks/session_coordinator.py:418-426` — `ORDER BY created_at` on `inter_session_messages`, which has no `created_at` column (schema:845-856). Always raises UndefinedColumn, swallowed at debug; the "agent result from last send_message" fallback never fires, degrading to tmux-scrollback scraping.
- **Minimal fix:** `ORDER BY sent_at DESC`.

### [IMPORTANT] sessions.status is free text with zero transition guards; registration resurrects terminal sessions
- **Where:** `storage/sessions/_field_update.py:32-49` (blind UPDATE; arbitrary strings persist and make rows invisible to every status-IN sweep — immortal sessions), `_bulk_update.py:94-95` (status unvalidated while sibling fields ARE validated); `_upsert.py:47-65` (`status='active'` hardcoded in the merge — re-registration revives `deleted`/`expired` rows without resetting `transcript_processed`, unlike the carefully guarded `revive_expired_terminal_session`).
- **Minimal fix:** Validate against an allowed set; guard terminal states; restrict implicit revival and reset transcript_processed on it. Same disease as the pipelines storage (workflows-engine review).

### [IMPORTANT] `parent_session_id` (and friends) are permanently sticky — COALESCE merge makes fields unclearable
- **Where:** `_upsert.py:53` (COALESCE keeps a wrong parent forever; `update_parent_session_id` (`_field_update.py:335-357`) cannot write NULL — when sanitize rejects a cyclic parent it silently *keeps* the old wrong attribution). Lineage features consume the wrong edge permanently. Title, transcript_path, git_branch, summary fields share the unclearable pattern.
- **Minimal fix:** Sentinel-based "clear" semantics; nullable update signature.

### [IMPORTANT] `recalculate_stats` queries a table that does not exist
- **Where:** `storage/sessions/_bulk_update.py:179-203` — four subqueries `FROM session_messages`; no such table in the schema or migrations. Any real call raises UndefinedTable; the only test exercises the missing-session short-circuit. The stale "purge DB messages" comment at `lifecycle.py:436` is residue of the same removed table.
- **Minimal fix:** Delete or reimplement from the transcript index.

### [IMPORTANT] `chat_messages.save_message`: MAX(seq)+1 race with no unique constraint
- **Where:** `storage/chat_messages.py:31-58` (concurrent saves duplicate seq — silent), `:75` (ORDER BY seq with no tiebreaker; `after_seq` pagination skips a duplicate at a page boundary — permanent display loss for that client).
- **Minimal fix:** `UNIQUE(conversation_id, seq)` + retry, or an advisory lock; add an id tiebreaker.

### [IMPORTANT] Recovery/terminal-context seams: unreachable ambiguity guard; replace-vs-merge drops backfilled keys
- **Where:** `storage/sessions/_registration_cache.py:37-40,241-253` (the rank tuple ends with the unique session id, so the "ambiguous cross-source recovery" refusal can never fire — recovery silently binds a winner); `_upsert.py:54` + `_registration_cache.py:295-328` (registration wholesale-replaces `terminal_context` JSON while backfill merges — a daemon-restart re-registration discards the backfilled `tmux_pane`, losing the liveness/kill target; backfill itself is an unserialized RMW).
- **Minimal fix:** Compare scores not full tuples; merge in SQL (`|| %s::jsonb`) on both paths.

### [IMPORTANT] Archive/status serving: seek-mode thrash, strict UTF-8 on archive paths, build-lock leak
- **Where:** `transcript_status.py:126-141` (status path materializes the whole decompressed archive *and* writes a `seek_mode="line"` sidecar that the window path (`gzip-block`) invalidates — both rebuild O(archive) on alternating calls); `transcript_io.py:49,62` + `transcript_index.py:204` (strict UTF-8 decode where every indexed reader uses `errors="replace"` — one invalid byte makes a session's archive permanently 500 while the live path renders it fine); `transcript_index.py:930-995` (`_BUILD_LOCKS` entry leaks on build exception — unbounded for persistently failing sessions).
- **Minimal fix:** Share the gzip-block index for counts; `errors="replace"`; try/finally the lock pop.

### [IMPORTANT] Rendering fidelity: images/documents dropped before the renderer's safety net; late tool results never re-broadcast
- **Where:** `claude.py:432-504` (no parser emits `content_type="image"`/`"document"` — the renderer's full handling at `transcript_renderer.py:891-897` is dead code; pasted screenshots vanish with no diagnostic because the parser drops them before the unknown-type error log); `transcript_renderer.py:264-266,781-791` + `processor.py:856-874` (a tool_result arriving after its turn flushed mutates the server-side object but no event reaches websocket clients — UI shows the tool pending until a full refresh).
- **Minimal fix:** Emit image/document ParsedMessages; surface late pairings for re-broadcast.

### [IMPORTANT] Sync DB/file/subprocess I/O on the event loop across the package
- **Where (verified by four reviewers):** processor poll loop (sync reads, per-message sync `record`, fsync-per-poll sidecar persist, serial across sessions — worst case daemon-wide stalls during restart re-tails); `lifecycle.py:296-322,514-516,640-720` (full transcript `f.read()`, sweeps, per-message records); `liveness_monitor.py:222-415` (sync fetchall + subprocess per 30s tick); `summarize.py:451-515` (git subprocesses + prompt-loader DB in async); `mailbox.py:109-196` (recursive-CTE queries + N inserts on the loop); `servers/routes/sessions/messages.py:229` (full gzip restore in the route).
- **Minimal fix:** `to_thread`/run_db throughout — the adjacent code already demonstrates the pattern.

### [IMPORTANT] Fire-and-forget compact-continuation task + un-bracketed multi-line paste (consumer instances)
- **Where:** `compact_continuation.py:291` (`create_task` result discarded — GC can collect the task during its 1-3s sleep, silently losing the continuation prompt); `:237` → `text_injection.py` paste without `-p` (the agents-review tmux Blocker applies here: the multi-line continuation prompt submits in fragments).
- **Minimal fix:** Task-set retention; `-p` (filed fix) or single-line prompt.

### [NIT] Processor small items
- **Where:** `processor.py:101,172-232` (dead `_hook_manager` + `_build_codex_hook_event`); `:896-898` (`.json` mtime `<=` gate misses same-tick writes on coarse-mtime filesystems); gemini `.json` history shrink (e.g. `/compress`) permanently stalls ingestion (`:934-940` — index high-water filter; latent).

### [NIT] Parser small items
- **Where:** `claude.py:596-600` (multi-tool assistant line keeps only the last tool_use — latent for 2.x); `:468-505` (thinking blocks re-ordered to the end); `:240-256` (dead boundary-recheck loop); `:527-540` (non-text tool_result sub-blocks fall back to Python repr; `tool_reference` exists in real data); duplicate parser dispatch (`__init__.py` vs `transcript_parsing.py`); inconsistent `log_unknown_block` usage across parsers; timestamp `.replace` AttributeError family in gemini/claude/codex; gemini functionCall parts get no tool_use_id; codex `web_search_call` never pairs a result.

### [NIT] Analyzer/summarize small items
- **Where:** `analyzer.py:175-178` (initial_goal takes command-wrapper/hook-context turns verbatim); `:181-242` (dead `found_active_task` param); `formatting.py` `files_modified` uncapped; `summarize.py:267-296` (whole JSONL loaded before caps); `token_tracker.py:56-103` (fallback shape drift).

### [NIT] Index/window small items
- **Where:** `transcript_index.py:222-238` (final line can overrun the size snapshot — docstring overclaims); `transcript_window.py:90` (dead `boundaries_used`), `:132-171` (no lookback byte abort; O(page×groups) scans); `gzip_seek_index.py:109-170` (dead duplicate except clause; sidecar persisted without fsync); `transcript_index.py` at 997 lines — 3 under the monolith cap; next editor must create the refactor task per repo rule 2.

### [NIT] Mailbox/storage small items
- **Where:** `inter_session_messages.py:264-286` (`mark_read`/`read_at` dead — `unread_only` filters are no-ops); `transcript_search.py:36-48` (casefold offset drift on non-ASCII); `transcript_renderer.py:37` (`ToolResult.truncated` never set — multi-MB results serialized whole), `:823-832` (bootstrap-heading heuristic can flip a real user message to system), `:361-364` (nested same-tag protocol regex mis-nests); `pending_tool_calls` never popped after pairing; `_detect_source_from_path` classifies any `.json` as gemini before the `.claude` check; zombie sweep stamps `delivered_at` on never-delivered messages; `restore_session_transcript` MCP tool writes to arbitrary `target_path`; `storage/sessions/_query.py:50-60` (`statuses=["deleted"]` can never match); `session_resolution.py:62-140` (LIKE metacharacters unescaped); `_bulk_update.py:120-134` (conflict redirect applies caller's values to a different session; dead NULL-project branch); `_discovery.py:101-114` (fetchone without ORDER BY across terminal/web_chat twins), `:254-263` (terminal-context scan capped at 250); `_registration_cache.py:108-150` (find_parent_session pins a worker thread with time.sleep up to 30s).

## Systemic patterns

1. **Parsers built against assumed formats, not captured ones.** Every parser Blocker traces to fixture-vs-reality divergence: qwen's envelope, gemini's `tokens` shape, codex's reasoning-inclusive output, claude's attachment/queue/compaction/isMeta shapes, the invented hook_blocking_error fixture. There is no captured-transcript corpus test ("every line either parses or is intentionally classified"); hand-written fixtures assert the parser's own assumptions back at it.
2. **Commit points straddle failure boundaries with no per-session mutual exclusion.** The processor advances different state at different points around awaits and fallible writes; JSONL is at-most-once, JSON is at-least-once, and the index-identity streams (offsets, message indices, appender counters) desynchronize under any failure — converting the `ON CONFLICT` dedup from protection into data loss.
3. **The wrong-liveness-signal and unverified-OS-handle classes recur** (third subsystem in a row): tmux probe trusting empty output, PID kills without identity, `handoff_identity` accepting bare pid/tty equality, stale pane ids across server restarts.
4. **"Delivered" means "marked", not "received"** — no claim/ack primitive in storage; every consumer reimplements get-then-mark, three of four mark before delivering; authorization enforced at resolution, fully unscoped at storage.
5. **Status is a free-text column, not a state machine** (same disease as pipelines storage): one guarded transition exists (`revive_expired_terminal_session`), everything else is blind last-writer-wins, including resurrection of terminal states by registration.
6. **seq_num integrity enforced only at INSERT** — every project-mutation path (bulk move, register recovery, `update(project_id=)`) bypasses the advisory lock and renumbering; `_renumber.py` exists with no callers on those paths.
7. **COALESCE-merge makes fields unclearable** — parent attribution, transcript paths, titles can never return to NULL through any storage API.
8. **Cleanup asymmetry** — every adjacent store has a retention sweep; `inter_session_messages` only has a "stop pretending" sweep; `_BUILD_LOCKS`, processor state maps, and coordinator registration sets grow monotonically.
9. **Live path vs expiry-backfill path disagree per CLI** (agy parser choice, droid usage, qwen content) — dashboards and final records diverge, self-correcting only at expiry.
10. **Compensations live in the renderer while LLM-facing surfaces consume raw parser output** — the UI looks right; digests, handoffs, and stats ingest the noise.

## Verified non-bugs (cleared — don't re-chase)

- **Torn lines at poll boundaries (JSONL) are handled** — only `\n`-terminated lines are consumed; incomplete tails re-read next poll.
- **Token double-counting across multi-line Claude messages is correctly deduped** (`ON CONFLICT (session_id, message_id)`; byte-identical usage verified 341/341 real message ids); restart full re-reads are idempotent in the happy path.
- **Sidecar/index read-path discipline is strong**: keyed on `(abspath, source, seek_mode, mtime_ns, size)`; appends always change size; persistence is tmp+fsync+replace atomic; double-checked build locking correct; gzip member math verified including edge cases; windows-equal-full-render is parametrized across sources in tests.
- **No XSS through rendered transcripts** — the web UI uses ReactMarkdown without rehype-raw and no dangerouslySetInnerHTML; rendered output is structured JSON end-to-end.
- **Gzip archive writes are atomic** (tmp + `os.replace`); path traversal in transcript lookup is guarded (`glob.escape`, separator rejection).
- **Mailbox fanout dedupe/ordering, coordinator cache TTL/invalidations, and cross-project direct-send fences are correct and tested**; concurrent session registration cannot duplicate rows (advisory lock + unique-index recovery); parent attribution survives interleavings (COALESCE on that axis is correct).
- **`pause_inactive_active_sessions` not bumping `updated_at` is intentional** (preserves the real-activity expiry clock); lifecycle sweeps are single-instance with bounded, exception-safe shutdown.
- **Codex `reasoning` items carry only encrypted content** (nothing user-visible lost); grok chunk coalescing is correct for the persisted format; codex `last_token_usage` preference is correct delta semantics.
- **`%s` placeholders are correct** per repo convention (CLAUDE.md's `$N` mandate is stale doc drift).
