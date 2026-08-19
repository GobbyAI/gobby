# Grok Turn Accounting and Transcript Truth

> **Plan ID:** grok-turn-accounting

## Overview
`kind: framing`

Planning sub-epic #20453 (parent epic #20442). Grok session accounting is structurally
wrong because `GrokTranscriptParser` treats every streamed ACP update as a standalone
conversation message and drops the one record that defines a turn. Consequences, verified
against the four audited transcripts on this machine (2026-08-18):

| Session | Records | True turns (`turn_completed`) | DB `turn_count` | Notes |
|---|---|---|---|---|
| #10695 | 10,812 | 55 (35 `end_turn` / 20 `cancelled`) | 163 at audit time | 17 compaction restarts, 17 daemon wakes |
| #10715 | 5,452 | 4 | 73 | marathon turn: 459 `modelCalls`, 676 tool calls, 50 text chunks, 11,079 chars |
| #10725 | 12,536 | 9 (7 `cancelled`) | 157 | every prompt synthetic (goal reminder / compaction continuations); digest holds 2 turns |
| #10711 | 1,626 | 8 (3 `cancelled`) | 27 | healthiest of the four; digest holds 5 turns |

Ground truth about the `updates.jsonl` stream (all four transcripts agree):

- Every record is `{"method", "params", "timestamp"}` with the update at `params.update`;
  methods are `session/update` (standard ACP) and `_x.ai/session/update` (extensions).
- `turn_completed` is the only content-stream record carrying `prompt_id` (one unique id
  per turn), plus `stop_reason` (`end_turn` | `cancelled`) and a turn-aggregate `usage`
  dict: `inputTokens`, `outputTokens`, `totalTokens`, `cachedReadTokens`,
  `cacheCreationTokens`, `reasoningTokens`, `modelCalls`, `apiDurationMs`,
  `costUsdTicks`, `modelUsage`. `hook_execution` records for `user_prompt_submit` and
  `stop` carry the same `prompt_id`, corroborating the keying.
- `agent_message_chunk` records are complete text blocks, never split deltas: in #10695
  all 285 chunks are singleton runs separated by thoughts/tool records. The inflation is
  that one turn contains ~5 assistant text blocks and the shared stat predicate counts
  each block as a completed turn.
- Content records (`user_message_chunk`, `agent_message_chunk`, `agent_thought_chunk`,
  `tool_call`, `tool_call_update`) carry no usage keys (0 of 6,696 in #10695), so grok
  sessions currently persist zero token usage — the dropped `turn_completed` aggregate is
  the only usage source.
- The `stop` hook fires only on `end_turn` (35 of 55 turns in #10695); cancelled turns
  produce no turn-end digest trigger. Group-A leaf A4 (already landed on 0.5.0:
  `catch_up` drain in `src/gobby/memory/digest.py`, the backlog sweep in
  `src/gobby/sessions/lifecycle.py`, and the synthetic-prompt filter in
  `src/gobby/memory/synthetic_prompts.py`) provides the catch-up machinery, but its
  sweep predicate `turn_count - last_digested_pair_index >= threshold` is incoherent
  while `turn_count` counts chunks, and pair extraction still emits chunk-fragment pairs
  where most responses have no user anchor.
- Compaction restarts inject synthetic user prompts ("Continue where you last left off…"
  matching `looks_like_wait_directive`, and "Message from Gobby daemon: New activity
  available." matching the wake-prompt prefix). These render as consecutive Human turns
  with no real user text and must not anchor digest pairs by themselves.

This plan reworks the grok parser around `prompt_id`/`turn_completed`, corrects
`turn_count` derivation, makes digest pair extraction turn-keyed with sub-segmentation
for marathon single-prompt turns, pins the behavior with fixtures replicating the four
audited transcripts, and recomputes user-anchored coverage as acceptance.

## Constraints
`kind: framing`

- Grounded on the local `0.5.0` branch state (A4's `catch_up`/backlog-sweep/synthetic
  filter changes are present there; `src/gobby/sessions/transcripts/` is identical on
  `0.4.127` and `0.5.0`).
- No backward compatibility: 0.5.0 has not shipped. No migration or backfill of
  historical `sessions.turn_count` values; the lifecycle expiry path recomputes full
  transcripts, so stale rows self-correct when sessions are reprocessed.
- Non-goals: qwen/droid/AGY parser changes (qwen shares the ACP shape but is owned by
  its own work; AGY is owned by agy-full-integration); the compact_session redesign
  (separate plan); digest trigger rule changes (A4's turn-start catch-up and daemon
  backlog sweep already cover cancelled turns once pair extraction is truthful); grok
  watchdog stream reading (`src/gobby/agents/watchdog/grok.py` parses `turn_completed`
  independently and stays untouched).
- `src/gobby/memory/digest.py` is intentionally not modified: `_extract_digest_pairs`
  walks whatever `extract_last_messages` returns, so the grok fix lands entirely in the
  parser and the shared stat predicate. Consumers `summary_generation.py` and
  `summary_context.py` (both call `extract_last_messages(..., num_pairs=2)`) receive
  strictly better input (whole-turn responses instead of the last two text fragments)
  with no call-site changes.
- Operational note, not a plan gate: the installed `gcode` binary currently rejects the
  live grant file ("malformed grant: unknown field `credential_generation`") — the known
  08-17 window tracked by sibling planning sub-epic C (#20442). Draft validation runs
  without project symbol validation; expansion-time symbol validation needs the rebuilt
  binary that sub-epic C / normal binary reinstall produces.

## P1: Turn-model parser and stats
`kind: framing`

**Goal**: `GrokTranscriptParser` emits explicit turn boundaries keyed on
`turn_completed`/`prompt_id`, the shared stat predicate counts grok turns from those
boundaries, and digest pair extraction becomes turn-keyed with marathon sub-segmentation.

### 1.1 Emit turn_completed boundary records in the grok parser [category: code]
`kind: deliverable`

Targets:
- `src/gobby/sessions/transcripts/grok.py::GrokTranscriptParser.parse_line`
- `src/gobby/sessions/transcripts/base.py::*` — scope-reason: the only edit adds "turn_completed" to the module-level RENDER_SKIP_CONTENT_TYPES frozenset; gcode indexes Python functions/classes only, so the constant has no indexed symbol to reference
- `tests/sessions/transcripts/test_grok_parser.py::*` — scope-reason: the turn-model rework rewrites the metadata-suppression parametrization and message-shape expectations across this suite

Stop dropping `turn_completed` in `GrokTranscriptParser.parse_line`
(`src/gobby/sessions/transcripts/grok.py`). Emit a turn-boundary `ParsedMessage`:

```python
if update_type == "turn_completed":
    return _message(
        index,
        "assistant",
        "",
        "turn_completed",
        timestamp,
        data,
        message_id=_message_id("grok", self.session_id, index, update.get("prompt_id")),
        usage=_turn_usage(update),
    )
```

Add a module-level helper that maps the turn-aggregate usage exactly (do not reuse
`_extract_usage`, whose key list does not include grok's `cachedReadTokens` spelling):

```python
def _turn_usage(update: dict[str, Any]) -> TokenUsage | None:
    usage = update.get("usage")
    if not isinstance(usage, dict):
        return None
    cache_read = _count(usage.get("cachedReadTokens"))
    cache_creation = _count(usage.get("cacheCreationTokens"))
    input_tokens = max(0, _count(usage.get("inputTokens")) - cache_read - cache_creation)
    output_tokens = _count(usage.get("outputTokens"))
    if input_tokens == output_tokens == cache_read == cache_creation == 0:
        return None
    return TokenUsage(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cache_creation_tokens=cache_creation,
        cache_read_tokens=cache_read,
    )
```

Behavioral spec:

- The boundary record's `raw_json` is the full JSONL record, so `prompt_id`,
  `stop_reason`, `modelCalls`, `reasoningTokens`, and `modelUsage` stay available to
  downstream consumers without new fields. `content` is the empty string.
- Turns without usage (e.g. #10715's local `/goal status` turn completed with no model
  calls) yield `usage=None`; a missing `prompt_id` falls back to the index-only
  `message_id` form already implemented by `_message_id`.
- Because content records carry no usage (verified across all four audited transcripts),
  the boundary usage is the sole grok token source: session usage totals become the sum
  of turn aggregates with cache reads/creations split out, fixing the current
  all-zeros usage rows.
- `retry_state` stays suppressed. Extend the same suppression (return `None`) to the
  known metadata update types that today fall through to `_unknown_block_message` and
  pollute `message_count` and the parser-error log: `compaction_checkpoint`,
  `auto_compact_completed`, `task_backgrounded`, `task_completed`,
  `current_mode_update`, and `hook_annotation`. `plan` and genuinely unknown types keep
  the unknown-block sentinel path. The agent watchdog reads the raw stream itself and is
  unaffected by parser suppression.
- In `src/gobby/sessions/transcripts/base.py`, extend `RENDER_SKIP_CONTENT_TYPES` with
  `"turn_completed"` so boundary records never render as chat cards and are excluded
  from `message_count`/flat output via the existing `NON_MESSAGE_CONTENT_TYPES` union.
- `parse_line` stays stateless (no buffering, no `snapshot_state` additions): chunks are
  complete blocks, and the boundary record is emitted on the `turn_completed` line
  itself, so streaming, resume, and the windowed index are untouched.
- In `tests/sessions/transcripts/test_grok_parser.py`, move `turn_completed` out of the
  suppressed-metadata parametrization and pin: boundary record shape (role, content
  type, empty content, `message_id` containing the `prompt_id`), `_turn_usage` mapping
  including the cache-split arithmetic and the usage-absent case, and suppression of the
  six metadata update types above.

**Acceptance:**

- 1.1.1 - Parsing a real-shape `turn_completed` line yields a `ParsedMessage` with `content_type == "turn_completed"`, empty content, and mapped `TokenUsage` splitting `cachedReadTokens`/`cacheCreationTokens` out of `inputTokens`. symbol: `GrokTranscriptParser.parse_line`.
- 1.1.2 - A `turn_completed` update without a usable usage dict yields `usage=None` and still emits the boundary record. test: `tests/sessions/transcripts/test_grok_parser.py::test_grok_turn_completed_without_usage_still_emits_boundary`.
- 1.1.3 - `compaction_checkpoint`, `auto_compact_completed`, `task_backgrounded`, `task_completed`, `current_mode_update`, and `hook_annotation` updates return `None` from `parse_line` and no longer reach the unknown-block sentinel. test: `tests/sessions/transcripts/test_grok_parser.py::test_grok_protocol_metadata_records_are_suppressed`.
- 1.1.4 - `"turn_completed"` is a render-skip content type, so boundary records are excluded from rendering and `message_count`. file: `src/gobby/sessions/transcripts/base.py`.

### 1.2 Count grok turns from boundary records in message stats [category: code] (depends: 1.1)
`kind: deliverable`

Targets:
- `src/gobby/sessions/message_stats.py::compute_message_stats`
- `src/gobby/sessions/message_stats.py::MessageProtocol`
- `src/gobby/sessions/transcript_index.py::TranscriptIndexAppender.append_raw_lines`
- `tests/sessions/test_message_stats.py::*` — scope-reason: adds boundary-counting, source-gating, and merge cases across the suite alongside the existing predicate tests
- `tests/sessions/test_transcript_index.py::*` — scope-reason: adds grok boundary-record stats cases (append, rebuild, hydrate) alongside the existing appender suite

Rework the shared stat predicate in `src/gobby/sessions/message_stats.py` so
`turn_count` means "completed turns" for sources that emit explicit boundaries, without
changing any other CLI's semantics:

```python
TURN_BOUNDARY_CONTENT_TYPE = "turn_completed"
TURN_BOUNDARY_SOURCES: frozenset[str] = frozenset({"grok"})
```

In `compute_message_stats`, order matters:

```python
for msg in messages:
    content_type = _message_attr(msg, "content_type")
    if content_type == TURN_BOUNDARY_CONTENT_TYPE:
        # Explicit turn boundary: counts one turn, is not a conversation message.
        turn_count += 1
        continue
    if content_type in NON_MESSAGE_CONTENT_TYPES:
        continue
    message_count += 1
    role = _message_attr(msg, "role")
    if role == "assistant" and content_type == "text":
        if _message_attr(msg, "source") not in TURN_BOUNDARY_SOURCES:
            turn_count += 1
        content = _message_attr(msg, "content")
        if isinstance(content, str) and content.strip():
            last_assistant_content = content.strip()[-_LAST_ASSISTANT_CONTENT_LIMIT:]
    if _message_attr(msg, "tool_name"):
        tool_call_count += 1
```

Behavioral spec:

- The boundary branch runs before the `NON_MESSAGE_CONTENT_TYPES` skip because
  `"turn_completed"` joins `RENDER_SKIP_CONTENT_TYPES` in 1.1 — boundaries count turns
  but never `message_count`.
- Assistant text from a `TURN_BOUNDARY_SOURCES` message still updates
  `last_assistant_content` (and `message_count`/`tool_call_count` are unaffected); only
  the per-text-block turn increment is gated. The predicate stays per-message and
  order-free, so `merge_message_stats` accumulation across live poll batches remains
  correct.
- `MessageProtocol` gains `source: str | None`; `_message_attr` already tolerates absent
  attributes, so ad-hoc callers keep working. `ParsedMessage.source` is populated by
  `annotate_record_source` on every parse path (batch `parse_lines` and streaming
  `iter_parse_events`), so live and expiry stat writers see the same values.
- `TranscriptIndexAppender.append_raw_lines` is the third stats writer and currently
  excludes NON_MESSAGE records from `stats_messages` before calling
  `accumulate_message_stats`. Because `"turn_completed"` joins
  `RENDER_SKIP_CONTENT_TYPES` (and therefore `NON_MESSAGE_CONTENT_TYPES`) in 1.1,
  route boundary records into the stats batch in a separate branch ahead of the
  NON_MESSAGE guard — still excluded from `parsed_message_count`, role counts, tool
  bookkeeping, and rendering. Without this, a sidecar rebuild writes
  `session_stats.turn_count == 0` for grok and
  `ProcessorLifecycleMixin._hydrate_registration_from_sidecar` pushes that zero into
  `session_manager.update_stats` on the next registration, clobbering the
  expiry-corrected count.
- Expected corrected values on the audited-replica fixtures: #10695-shape → 55,
  #10715-shape → 4, #10725-shape → 9, #10711-shape → 8. Claude/codex/droid/qwen
  fixtures in the existing suites keep their current expected counts (they emit no
  boundary records and are not in `TURN_BOUNDARY_SOURCES`).
- A live grok session's `turn_count` now lags the in-flight turn until its
  `turn_completed` arrives; this is the truthful reading and makes the A4 backlog-sweep
  predicate `turn_count - last_digested_pair_index >= threshold` coherent (pairs can
  exceed turns via sub-segmentation, which only keeps the sweep idle — the lag signal
  fires on positive backlog only).

**Acceptance:**

- 1.2.1 - `compute_message_stats` counts one turn per `turn_completed` boundary record and excludes boundaries from `message_count`. symbol: `compute_message_stats`.
- 1.2.2 - Assistant `text` messages with `source == "grok"` do not increment `turn_count` but still update `last_assistant_content`; non-grok assistant text keeps incrementing. test: `tests/sessions/test_message_stats.py::test_turn_boundary_source_gates_assistant_text_turns`.
- 1.2.3 - `MessageProtocol` declares `source`, and merge/accumulate paths preserve boundary-derived counts across batches. symbol: `MessageProtocol`.
- 1.2.4 - `append_raw_lines` feeds `turn_completed` boundary records into `accumulate_message_stats` while keeping them out of `parsed_message_count` and role counts; a rebuilt-then-hydrated grok sidecar preserves the boundary-derived `turn_count` through `session_manager.update_stats`. test: `tests/sessions/test_transcript_index.py::test_grok_boundary_records_feed_sidecar_stats`.

### 1.3 Turn-keyed digest pair extraction with marathon sub-segmentation [category: code] (depends: 1.1)
`kind: deliverable`

Targets:
- `src/gobby/sessions/transcripts/grok.py::GrokTranscriptParser.extract_last_messages`
- `tests/sessions/transcripts/test_grok_parser.py::*` — scope-reason: replaces the chunk-walk extraction expectations with turn-keyed pair expectations across the suite

Rewrite `GrokTranscriptParser.extract_last_messages`
(`src/gobby/sessions/transcripts/grok.py`) to be turn-keyed. It receives raw JSONL
record dicts (`_extract_update` already handles the `params.update` nesting) and must
return `{"role", "content"}` dicts whose user→assistant adjacency defines digest pairs
for `_extract_digest_pairs` in `src/gobby/memory/digest.py` (consumer, unchanged).

Algorithm:

1. Segment records into turns: a turn is every record up to and including its
   `turn_completed`. A trailing open segment (records after the last `turn_completed`,
   i.e. the in-flight turn) is included — A4's `catch_up` path already drops the
   trailing unanswered pair so the cursor never consumes an active turn.
2. Walk each segment's records in stream order, emitting:
   - each `user_message_chunk` text as a `user` message (this preserves mid-turn user
     injections such as #10715's "The user sent a message while you were working"
     records as their own anchors, in chronological position);
   - accumulated `agent_message_chunk` text, flushed as a single `assistant` message
     whenever a `user_message_chunk` follows (so pre-injection output pairs with the
     prompt that produced it), whenever the accumulated text would exceed
     `_PAIR_RESPONSE_CHAR_BUDGET`, and at the end of the segment.
3. Sub-segmentation budget: module constant `_PAIR_RESPONSE_CHAR_BUDGET = 4000`,
   flushing at `agent_message_chunk` boundaries only (blocks are never split
   mid-chunk; a single oversized block flushes alone). Audited calibration: normal
   turns join to ≤4.5K chars → one pair; #10715's 11,079-char marathon turn → 3 pairs
   `(prompt, seg1), ("", seg2), ("", seg3)`, giving the digest batching and the pair
   cursor intra-turn granularity.
4. `num_pairs` keeps its existing contract (bounded lookback): build the turn-keyed
   message list from the newest segment backwards and return once
   `len(messages) >= num_pairs * 2`, preserving order. Callers:
   `_extract_digest_pairs` passes `num_pairs=max(1, len(turns))` (effectively
   everything), `_read_last_turn_from_transcript`, `summary_generation.py`, and
   `summary_context.py` pass 1–2 and now receive whole-turn responses instead of the
   last two stream fragments.
5. Thought chunks, tool records, hook executions, and metadata updates contribute
   nothing to pairs (parity with claude/codex pairing, which also carries only
   user/assistant text).

Resulting pair semantics (with the unchanged `_extract_digest_pairs` walk):

- One completed turn with a real prompt and text output → `(prompt, joined_text)`.
- Cancelled turn with no assistant text → `(prompt, "")` — still user-anchored; A4's
  synthetic filter drops it only when the prompt itself is synthetic.
- Compaction-continuation and daemon-wake turns → synthetic prompts pair with whatever
  the continuation turn actually produced, so post-compaction work still digests; empty
  synthetic pairs are dropped by `synthetic_body_reason` (already landed).
- `extract_turns_since_clear` and `is_session_boundary` keep their current grok
  behavior (no boundaries; compaction is not a segment reset — the digest cursor spans
  compactions on the continuous `updates.jsonl`).

Pin in `tests/sessions/transcripts/test_grok_parser.py`: turn-keyed pairing over a
multi-turn stream, mid-turn injection anchoring, budget-driven sub-segmentation
(marathon shape), trailing open-segment inclusion, and `num_pairs=1` returning exactly
the last turn's pair.

**Acceptance:**

- 1.3.1 - `extract_last_messages` returns turn-keyed user/assistant messages: one assistant message per completed normal turn, joined from that turn's text blocks. symbol: `GrokTranscriptParser.extract_last_messages`.
- 1.3.2 - A marathon turn whose joined text exceeds 4,000 chars is flushed at chunk boundaries into multiple assistant messages, producing `(prompt, seg1)` then `("", segN)` pairs downstream. test: `tests/sessions/transcripts/test_grok_parser.py::test_grok_marathon_turn_sub_segmentation`.
- 1.3.3 - Mid-turn user injections become their own anchors with pre-injection output paired to the preceding prompt. test: `tests/sessions/transcripts/test_grok_parser.py::test_grok_mid_turn_injection_anchoring`.
- 1.3.4 - A trailing open segment is included and a cancelled turn without output yields a `(prompt, "")` pair. test: `tests/sessions/transcripts/test_grok_parser.py::test_grok_open_and_cancelled_turn_pairs`.

## P2: Audited-transcript fixtures and coverage recompute
`kind: framing`

**Goal**: The four audited sessions become deterministic fixtures, and user-anchored
coverage is recomputed over them as the plan's acceptance evidence.

### 2.1 Grok audited-stream fixture builders [category: test] (depends: P1)
`kind: deliverable`

Targets:
- `tests/sessions/transcripts/fixtures/grok_streams.py`
- `tests/sessions/transcripts/test_grok_turn_accounting.py`

Test infrastructure in its own right (not a TDD wrapper for P1): builder functions that
replicate the structural shape of the four audited transcripts with synthetic text
bodies, plus the accounting suite that runs the full parser pipeline over them.

`tests/sessions/transcripts/fixtures/grok_streams.py` (new file):

```python
def build_stream(turns: list[TurnSpec]) -> list[str]:
    """Render JSONL lines in the real updates.jsonl shape:
    {"method": "session/update" | "_x.ai/session/update",
     "params": {"sessionId": ..., "update": {...}}, "timestamp": ...}
    """

@dataclass(frozen=True)
class TurnSpec:
    prompt: str | None          # None = no user_message_chunk (open/edge shapes)
    injections: tuple[str, ...] = ()
    agent_blocks: tuple[str, ...] = ()
    thought_blocks: int = 0
    tool_calls: int = 0
    stop_reason: str | None = "end_turn"   # None = open segment (no turn_completed)
    usage: dict[str, int] | None = None    # inputTokens/outputTokens/cached*/modelCalls
    compaction_restart: bool = False       # prepend checkpoint + auto_compact_completed
    wake_prompt: bool = False              # daemon-wake user prompt body
```

Provide four canned builders mirroring the audited shapes exactly (counts from the
2026-08-18 audit):

- `session_10695_shape()` — 55 turns (35 `end_turn`, 20 `cancelled`), 17 compaction
  restarts each followed by a wake-prompt turn, 285 agent text blocks distributed as
  singleton runs, hook/tool records interleaved.
- `session_10715_shape()` — 4 turns; turn 0 is the marathon (459 `modelCalls` usage, 50
  agent blocks totalling >11K synthetic chars, 2 mid-turn injections); turn 1 is the
  local-command turn with `usage=None`; turns 2–3 small.
- `session_10725_shape()` — 9 turns (7 `cancelled`), every prompt synthetic under the
  actual `synthetic_body_reason` classifiers (which have no goal-reminder class): the
  goal-reminder turn uses the real daemon-wake body shape (`Message from Gobby
  daemon: …` → `daemon_wake_prompt`), and the continuations use `Continue where you
  last left off. … gobby-sessions.wait_for_summary … \`completed=false\` …` bodies
  (→ `wait_directive`). The builder must not invent a bare goal-reminder body — it
  would classify as a real prompt and break the all-synthetic invariant the 2.2
  metric relies on.
- `session_10711_shape()` — 8 turns (3 `cancelled`), 5 real prompts.

`tests/sessions/transcripts/test_grok_turn_accounting.py` (new file) parses each canned
stream with `GrokTranscriptParser.parse_lines` and asserts:

- `compute_message_stats` turn counts equal the true turn counts (55 / 4 / 9 / 8), with
  boundary records absent from `message_count`;
- summed `TokenUsage` equals the sum of the fixture turn aggregates with the cache
  split applied;
- pair extraction counts per stream match the deterministic expectation from the
  builders (documented as constants next to each builder);
- compaction-restart shapes produce no unknown-block sentinels.

**Acceptance:**

- 2.1.1 - Fixture builders render the real record envelope and cover all four audited shapes with deterministic counts. file: `tests/sessions/transcripts/fixtures/grok_streams.py`.
- 2.1.2 - The accounting suite pins corrected `turn_count`, usage totals, pair counts, and metadata suppression over all four canned streams. test: `tests/sessions/transcripts/test_grok_turn_accounting.py::test_audited_shapes_turn_and_usage_accounting`.

### 2.2 User-anchored coverage recompute [category: test] (depends: 2.1)
`kind: deliverable`

Targets:
- `tests/sessions/transcripts/test_grok_coverage_audit.py`

Behavior-pinning coverage suite (new file) that defines and recomputes the epic's
user-anchored coverage metric:

- **Metric**: for a transcript, `real_prompts` = `user_message_chunk` texts whose
  `strip_injected_context(...)` result is non-empty and whose
  `synthetic_body_reason(...)` (from `gobby.memory.synthetic_prompts`, imported, not
  modified) is `None`. `anchored` = real prompts appearing as the prompt of at least one
  extracted digest pair (`_extract_digest_pairs` from `gobby.memory.digest`, imported,
  not modified, over the grok parser). **User-anchored coverage = anchored /
  real_prompts**, with the empty denominator defined: when `real_prompts == 0`,
  coverage is `1.0` iff `anchored == 0` (vacuously covered) else `0.0`. The secondary
  response-completeness rate (anchored pairs with non-empty response / anchored
  pairs) is likewise `1.0` when `anchored == 0`.
- Fixture assertions: coverage is 100% on all four canned streams (every real prompt is
  the anchor of exactly one pair; interrupted-before-output prompts anchor `(prompt,
  "")` pairs), and the secondary response-completeness rate matches each builder's
  documented expectation. `session_10725_shape` asserts `real_prompts == 0` and pins
  the vacuous-coverage branch explicitly.
- Real-transcript replay: an opt-in test gated on
  `GOBBY_GROK_AUDIT_TRANSCRIPTS_DIR` (skipped when unset) replays actual
  `updates.jsonl` files from that directory through the same metric and prints
  per-transcript coverage — the recompute evidence for the four audited sessions
  without committing user transcript content.

**Acceptance:**

- 2.2.1 - The metric implementation and 100%-anchoring assertions over the four canned streams pass. test: `tests/sessions/transcripts/test_grok_coverage_audit.py::test_user_anchored_coverage_on_audited_shapes`.
- 2.2.2 - The env-gated replay computes and reports coverage for real transcripts and is skipped by default. test: `tests/sessions/transcripts/test_grok_coverage_audit.py::test_real_transcript_replay_opt_in`.

## E1: Epic Acceptance Verification
`kind: verification`

End-to-end acceptance for #20453's parent goal (grok digest coverage converging to the
claude/codex band):

1. Fixture evidence (deterministic, in CI): the 2.1 accounting suite and 2.2 coverage
   suite pass — corrected turn counts (55/4/9/8), usage totals, 100% user-anchored
   pair extraction on all four audited shapes.
2. Recompute on the audited sessions (operator step): run the 2.2 replay with
   `GOBBY_GROK_AUDIT_TRANSCRIPTS_DIR` pointing at the four audited `updates.jsonl`
   files; expect user-anchored coverage ≥92% per transcript (parity with the
   claude/codex band from the 2026-08-17 investigation; the only structural misses are
   prompts submitted and interrupted before any model output). A transcript with
   `real_prompts == 0` (like #10725) reports the 2.2 empty-denominator value — a
   vacuous 100% — rather than an undefined ratio.
3. Live convergence (epic_qa stage, after implementation lands and new grok sessions
   accumulate): re-run the 7-day digest coverage query per source from the
   investigation; expect grok to converge toward claude/codex ≥3-turn coverage ≥92%,
   with A4's backlog sweep now driven by a truthful
   `turn_count - last_digested_pair_index` lag signal.

## Task Mapping
`kind: framing`

<!-- Updated after task creation -->
| Plan Item | Task Ref | Status |
|-----------|----------|--------|

## V1 Plan Changelog
`kind: framing`

- Round 0 (2026-08-18, session #10770): initial narrative draft authored per plan-draft
  methodology; grounded against local 0.5.0 state and the four audited transcripts;
  no manifest (adversary-owned).

**Round 1** `kind: verification`

- reviewer_run: df6e778f-8237-459c-87f2-1c786f26e162
- reviewer_session: c926c32d-485b-473d-958e-458cb68c232a
- verdict: needs_review
- findings:
- F1/blocking/index-stats-boundary-filter: `TranscriptIndexAppender.append_raw_lines` drops NON_MESSAGE records before `accumulate_message_stats`, so a sidecar rebuild would write `turn_count 0` and `_hydrate_registration_from_sidecar` would clobber corrected session stats. Vote: accepted.
- F2/blocking/zero-denominator-coverage: user-anchored coverage `anchored/real_prompts` is undefined on the all-synthetic `session_10725_shape` fixture, and `synthetic_body_reason` has no goal-reminder class, so the fixture premise was internally inconsistent; E1's per-transcript replay inherited the same undefined ratio. Vote: accepted.
- resolution_notes: Both findings accepted after coordinator verification against `transcript_index.py`, `message_stats.py`, `processor_lifecycle.py`, and `synthetic_prompts.py`. Repairs: 1.2 gained the `append_raw_lines` Target, a boundary-routing behavioral spec bullet, a `tests/sessions/test_transcript_index.py::*` test target, and acceptance 1.2.4 (rebuild+hydrate preserves boundary-derived turn_count). 2.2's metric now defines the empty denominator (coverage 1.0 iff anchored == 0 when real_prompts == 0, same for the completeness rate) and `session_10725_shape` pins `real_prompts == 0` plus the vacuous branch; 2.1's 10725 builder now mandates classifier-true synthetic bodies (daemon-wake shape for the goal-reminder turn, wait-directive continuations) since no goal-reminder class exists; E1 step 2 reports the vacuous value for zero-real-prompt transcripts. The round's six dismissed candidates required no plan change.

```json plan-review-round
{"evidence_id":"e90fdd54-4e50-4b30-8883-ba54aae315e8","plan_hash":"f726c4b03380753e7f3c3a47b352901bc7f1473465822de03a9bc0e006712a20","round_number":1,"round_result":{"coverage_attestation":{"adjacent_variant_complete":true,"attestation_digest":"9611f99aaecacbfaec9824d2d3aaa3137bce7d0b7ea162f6fcea1c354f440463","cross_lane_interaction_complete":true,"disposition_counts":{"dismissed":6,"emitted_findings":2,"total":8},"evidence_id":"e90fdd54-4e50-4b30-8883-ba54aae315e8","lanes":[{"candidate_count":2,"lane_id":"requirements_traceability","status":"completed"},{"candidate_count":3,"lane_id":"repository_blast_radius","status":"completed"},{"candidate_count":3,"lane_id":"runtime_invariants","status":"completed"}],"shadow_manifest_status":{"entry_count":5,"manifest_digest":"6191fface4af42903926a19ad06ae0107a38bd39085ba6af44081125037789af","status":"valid"},"source_digest":"014ba1f7777eae720c00b5cf5113f1f9f71d869de07685b7d8e60aa2aadeb730","version":1},"findings":[{"category":"unhandled-edge","check_key":"index-stats-boundary-filter","description":"Section 1.1 adds turn_completed to RENDER_SKIP_CONTENT_TYPES so boundaries leave message_count. Section 1.2 special-cases those records only inside compute_message_stats. The third stats writer, TranscriptIndexAppender.append_raw_lines, filters NON_MESSAGE before accumulate_message_stats. Combined with source==grok gating, a rebuilt sidecar has turn_count 0. Expiry compute_message_stats is correct, then rebuild_and_persist_index poisons the sidecar; the next register_session hydrates that zero into session_manager, undoing the plan's self-correction claim.","finding_id":"F1","fix":"Keep excluding turn_completed from parsed_message_count, but pass those records into accumulate_message_stats (or compute stats before the NON_MESSAGE pre-filter). Add src/gobby/sessions/transcript_index.py::TranscriptIndexAppender.append_raw_lines as a 1.1 or 1.2 Target and pin a rebuild-plus-hydrate case on a grok fixture.","location":"Phase 1 / § 1.1 and 1.2","prevention":"When adding a content type to RENDER_SKIP, inventory every NON_MESSAGE consumer and keep stats writers on the compute_message_stats contract.","principle":"Every writer of session.turn_count must see turn_completed once that type joins RENDER_SKIP.","root_cause":"TranscriptIndexAppender.append_raw_lines drops NON_MESSAGE records before accumulate_message_stats. After 1.1 adds turn_completed to RENDER_SKIP, the index rebuild writes turn_count 0; 1.2 source-gating also skips grok assistant text. _hydrate_registration_from_sidecar then overwrites the expiry-corrected session.turn_count.","section_id":"1.1","severity":"blocking"},{"category":"unhandled-edge","check_key":"zero-denominator-coverage","description":"2.2 defines user-anchored coverage as anchored/real_prompts and requires 100% on all four canned streams, including session_10725_shape whose every prompt is synthetic. real_prompts uses synthetic_body_reason, which has wait_directive and empty_prompt but no goal-reminder class, so either 10725 has zero real prompts (divide-by-zero) or the all-synthetic claim is false. The secondary completeness rate is likewise 0/0 when anchored==0. E1 step 2 inherits the same undefined ratio on the real #10725 replay.","finding_id":"F2","fix":"Define coverage when real_prompts==0 as 1.0 iff anchored==0 else 0.0, apply that definition to 2.2.1, 2.2.2, and the E1 replay, define completeness when anchored==0 the same way, and pin the 10725 goal-reminder body against the actual synthetic_body_reason classifiers without modifying synthetic_prompts.py.","location":"Phase 2 / § 2.2 and E1","prevention":"For every ratio in acceptance, define the empty-denominator case and fixture it.","principle":"An acceptance metric used on every named fixture and replay transcript must be defined for every denominator those inputs can produce.","root_cause":"Coverage is defined as anchored/real_prompts. session_10725_shape is specified as all-synthetic, so real_prompts is 0 and the ratio is undefined. E1's per-transcript >=92% replay includes #10725.","section_id":"2.2","severity":"blocking"}],"round_number":1,"verdict":"needs_review"},"session_id":"15e5b0c6-7211-4308-ab2a-26720a8cb358"}
```

**Round 2** `kind: verification`

- reviewer_run: 3a52fb87-bc44-4c04-8500-bdff2af2d9ef
- reviewer_session: 1efece8d-bfbb-4a04-b8ee-c81a2177bcb7
- verdict: approved
- findings:
- none (six candidates raised across the three lanes, all dismissed on verification)
- resolution_notes: Round-1 repairs (append_raw_lines boundary routing + acceptance 1.2.4; empty-denominator coverage definition, classifier-true 10725 fixture bodies, vacuous-branch pinning in 2.2/E1) verified against the tree and accepted. The adversary supplied routing decisions (1.1/1.2/1.3 code-backend tdd; 2.1/2.2 test) and the five-entry M1 manifest covering all 16 acceptance IDs; the coordinator applied it via apply_plan_review_manifest (manifest_digest 27882e55523c0f4aaac2850c2613e9aeafe928a707291cf4c59bd5caeabf6adf). No plan-body changes this round.

```json plan-review-round
{"evidence_id":"c5f757db-2c36-43b5-adaa-9aba8bc74119","plan_hash":"b2f3e7504b66effb923ea7d7b20d83d076b5fdee5f46957bfe45b0bfdda6bb50","round_number":2,"round_result":{"coverage_attestation":{"adjacent_variant_complete":true,"attestation_digest":"263e3d188426e3e27c772a5ddd77bfd63d72a0a3ee511acb25b3f4ccfd3b880d","cross_lane_interaction_complete":true,"disposition_counts":{"dismissed":6,"emitted_findings":0,"total":6},"evidence_id":"c5f757db-2c36-43b5-adaa-9aba8bc74119","lanes":[{"candidate_count":2,"lane_id":"requirements_traceability","status":"completed"},{"candidate_count":2,"lane_id":"repository_blast_radius","status":"completed"},{"candidate_count":2,"lane_id":"runtime_invariants","status":"completed"}],"shadow_manifest_status":{"entry_count":5,"manifest_digest":"bf3c5a1f053f9b0079f3bfd50a9abd84fff08b04f2195fee4a68f084463439b7","status":"valid"},"source_digest":"8eaf2d506435625ed56d5761fa7c0c1eee9886ff1c161a6e1a21d71914edb7ac","version":1},"findings":[],"manifest_entries":[{"category":"code","depends_on":[],"implementation_domain":"backend","labels":["covers:grok-turn-accounting:1.1:1.1.1","covers:grok-turn-accounting:1.1:1.1.2","covers:grok-turn-accounting:1.1:1.1.3","covers:grok-turn-accounting:1.1:1.1.4"],"source_section":"1.1","task_type":"feature","tdd":true,"title":"Emit turn_completed boundary records in the grok parser","validation_criteria":"1.1.1: Parsing a real-shape `turn_completed` line yields a `ParsedMessage` with `content_type == \"turn_completed\"`, empty content, and mapped `TokenUsage` splitting `cachedReadTokens`/`cacheCreationTokens` out of `inputTokens`. symbol: `GrokTranscriptParser.parse_line`.\n1.1.2: A `turn_completed` update without a usable usage dict yields `usage=None` and still emits the boundary record. test: `tests/sessions/transcripts/test_grok_parser.py::test_grok_turn_completed_without_usage_still_emits_boundary`.\n1.1.3: `compaction_checkpoint`, `auto_compact_completed`, `task_backgrounded`, `task_completed`, `current_mode_update`, and `hook_annotation` updates return `None` from `parse_line` and no longer reach the unknown-block sentinel. test: `tests/sessions/transcripts/test_grok_parser.py::test_grok_protocol_metadata_records_are_suppressed`.\n1.1.4: `\"turn_completed\"` is a render-skip content type, so boundary records are excluded from rendering and `message_count`. file: `src/gobby/sessions/transcripts/base.py`."},{"category":"code","depends_on":["1.1"],"implementation_domain":"backend","labels":["covers:grok-turn-accounting:1.2:1.2.1","covers:grok-turn-accounting:1.2:1.2.2","covers:grok-turn-accounting:1.2:1.2.3","covers:grok-turn-accounting:1.2:1.2.4"],"source_section":"1.2","task_type":"feature","tdd":true,"title":"Count grok turns from boundary records in message stats","validation_criteria":"1.2.1: `compute_message_stats` counts one turn per `turn_completed` boundary record and excludes boundaries from `message_count`. symbol: `compute_message_stats`.\n1.2.2: Assistant `text` messages with `source == \"grok\"` do not increment `turn_count` but still update `last_assistant_content`; non-grok assistant text keeps incrementing. test: `tests/sessions/test_message_stats.py::test_turn_boundary_source_gates_assistant_text_turns`.\n1.2.3: `MessageProtocol` declares `source`, and merge/accumulate paths preserve boundary-derived counts across batches. symbol: `MessageProtocol`.\n1.2.4: `append_raw_lines` feeds `turn_completed` boundary records into `accumulate_message_stats` while keeping them out of `parsed_message_count` and role counts; a rebuilt-then-hydrated grok sidecar preserves the boundary-derived `turn_count` through `session_manager.update_stats`. test: `tests/sessions/test_transcript_index.py::test_grok_boundary_records_feed_sidecar_stats`."},{"category":"code","depends_on":["1.1"],"implementation_domain":"backend","labels":["covers:grok-turn-accounting:1.3:1.3.1","covers:grok-turn-accounting:1.3:1.3.2","covers:grok-turn-accounting:1.3:1.3.3","covers:grok-turn-accounting:1.3:1.3.4"],"source_section":"1.3","task_type":"feature","tdd":true,"title":"Turn-keyed digest pair extraction with marathon sub-segmentation","validation_criteria":"1.3.1: `extract_last_messages` returns turn-keyed user/assistant messages: one assistant message per completed normal turn, joined from that turn's text blocks. symbol: `GrokTranscriptParser.extract_last_messages`.\n1.3.2: A marathon turn whose joined text exceeds 4,000 chars is flushed at chunk boundaries into multiple assistant messages, producing `(prompt, seg1)` then `(\"\", segN)` pairs downstream. test: `tests/sessions/transcripts/test_grok_parser.py::test_grok_marathon_turn_sub_segmentation`.\n1.3.3: Mid-turn user injections become their own anchors with pre-injection output paired to the preceding prompt. test: `tests/sessions/transcripts/test_grok_parser.py::test_grok_mid_turn_injection_anchoring`.\n1.3.4: A trailing open segment is included and a cancelled turn without output yields a `(prompt, \"\")` pair. test: `tests/sessions/transcripts/test_grok_parser.py::test_grok_open_and_cancelled_turn_pairs`."},{"assigned_agent":"backend-developer","category":"test","depends_on":["1.1","1.2","1.3"],"labels":["covers:grok-turn-accounting:2.1:2.1.1","covers:grok-turn-accounting:2.1:2.1.2"],"source_section":"2.1","task_type":"feature","tdd":false,"title":"Grok audited-stream fixture builders","validation_criteria":"2.1.1: Fixture builders render the real record envelope and cover all four audited shapes with deterministic counts. file: `tests/sessions/transcripts/fixtures/grok_streams.py`.\n2.1.2: The accounting suite pins corrected `turn_count`, usage totals, pair counts, and metadata suppression over all four canned streams. test: `tests/sessions/transcripts/test_grok_turn_accounting.py::test_audited_shapes_turn_and_usage_accounting`."},{"assigned_agent":"backend-developer","category":"test","depends_on":["2.1"],"labels":["covers:grok-turn-accounting:2.2:2.2.1","covers:grok-turn-accounting:2.2:2.2.2"],"source_section":"2.2","task_type":"feature","tdd":false,"title":"User-anchored coverage recompute","validation_criteria":"2.2.1: The metric implementation and 100%-anchoring assertions over the four canned streams pass. test: `tests/sessions/transcripts/test_grok_coverage_audit.py::test_user_anchored_coverage_on_audited_shapes`.\n2.2.2: The env-gated replay computes and reports coverage for real transcripts and is skipped by default. test: `tests/sessions/transcripts/test_grok_coverage_audit.py::test_real_transcript_replay_opt_in`."}],"round_number":2,"routing_decisions":{"1.1":{"category":"code","implementation_domain":"backend","tdd":true},"1.2":{"category":"code","implementation_domain":"backend","tdd":true},"1.3":{"category":"code","implementation_domain":"backend","tdd":true},"2.1":{"category":"test","tdd":false},"2.2":{"category":"test","tdd":false}},"verdict":"approved"},"session_id":"15e5b0c6-7211-4308-ab2a-26720a8cb358"}
```

## M1 Task Manifest
`kind: manifest`

```yaml
- title: Emit turn_completed boundary records in the grok parser
  category: code
  task_type: feature
  depends_on: []
  validation_criteria: '1.1.1: Parsing a real-shape `turn_completed` line yields a
    `ParsedMessage` with `content_type == "turn_completed"`, empty content, and mapped
    `TokenUsage` splitting `cachedReadTokens`/`cacheCreationTokens` out of `inputTokens`.
    symbol: `GrokTranscriptParser.parse_line`.

    1.1.2: A `turn_completed` update without a usable usage dict yields `usage=None`
    and still emits the boundary record. test: `tests/sessions/transcripts/test_grok_parser.py::test_grok_turn_completed_without_usage_still_emits_boundary`.

    1.1.3: `compaction_checkpoint`, `auto_compact_completed`, `task_backgrounded`,
    `task_completed`, `current_mode_update`, and `hook_annotation` updates return
    `None` from `parse_line` and no longer reach the unknown-block sentinel. test:
    `tests/sessions/transcripts/test_grok_parser.py::test_grok_protocol_metadata_records_are_suppressed`.

    1.1.4: `"turn_completed"` is a render-skip content type, so boundary records are
    excluded from rendering and `message_count`. file: `src/gobby/sessions/transcripts/base.py`.'
  labels:
  - covers:grok-turn-accounting:1.1:1.1.1
  - covers:grok-turn-accounting:1.1:1.1.2
  - covers:grok-turn-accounting:1.1:1.1.3
  - covers:grok-turn-accounting:1.1:1.1.4
  tdd: true
  source_section: '1.1'
  implementation_domain: backend
- title: Count grok turns from boundary records in message stats
  category: code
  task_type: feature
  depends_on:
  - '1.1'
  validation_criteria: '1.2.1: `compute_message_stats` counts one turn per `turn_completed`
    boundary record and excludes boundaries from `message_count`. symbol: `compute_message_stats`.

    1.2.2: Assistant `text` messages with `source == "grok"` do not increment `turn_count`
    but still update `last_assistant_content`; non-grok assistant text keeps incrementing.
    test: `tests/sessions/test_message_stats.py::test_turn_boundary_source_gates_assistant_text_turns`.

    1.2.3: `MessageProtocol` declares `source`, and merge/accumulate paths preserve
    boundary-derived counts across batches. symbol: `MessageProtocol`.

    1.2.4: `append_raw_lines` feeds `turn_completed` boundary records into `accumulate_message_stats`
    while keeping them out of `parsed_message_count` and role counts; a rebuilt-then-hydrated
    grok sidecar preserves the boundary-derived `turn_count` through `session_manager.update_stats`.
    test: `tests/sessions/test_transcript_index.py::test_grok_boundary_records_feed_sidecar_stats`.'
  labels:
  - covers:grok-turn-accounting:1.2:1.2.1
  - covers:grok-turn-accounting:1.2:1.2.2
  - covers:grok-turn-accounting:1.2:1.2.3
  - covers:grok-turn-accounting:1.2:1.2.4
  tdd: true
  source_section: '1.2'
  implementation_domain: backend
- title: Turn-keyed digest pair extraction with marathon sub-segmentation
  category: code
  task_type: feature
  depends_on:
  - '1.1'
  validation_criteria: '1.3.1: `extract_last_messages` returns turn-keyed user/assistant
    messages: one assistant message per completed normal turn, joined from that turn''s
    text blocks. symbol: `GrokTranscriptParser.extract_last_messages`.

    1.3.2: A marathon turn whose joined text exceeds 4,000 chars is flushed at chunk
    boundaries into multiple assistant messages, producing `(prompt, seg1)` then `("",
    segN)` pairs downstream. test: `tests/sessions/transcripts/test_grok_parser.py::test_grok_marathon_turn_sub_segmentation`.

    1.3.3: Mid-turn user injections become their own anchors with pre-injection output
    paired to the preceding prompt. test: `tests/sessions/transcripts/test_grok_parser.py::test_grok_mid_turn_injection_anchoring`.

    1.3.4: A trailing open segment is included and a cancelled turn without output
    yields a `(prompt, "")` pair. test: `tests/sessions/transcripts/test_grok_parser.py::test_grok_open_and_cancelled_turn_pairs`.'
  labels:
  - covers:grok-turn-accounting:1.3:1.3.1
  - covers:grok-turn-accounting:1.3:1.3.2
  - covers:grok-turn-accounting:1.3:1.3.3
  - covers:grok-turn-accounting:1.3:1.3.4
  tdd: true
  source_section: '1.3'
  implementation_domain: backend
- title: Grok audited-stream fixture builders
  category: test
  task_type: feature
  depends_on:
  - '1.1'
  - '1.2'
  - '1.3'
  validation_criteria: '2.1.1: Fixture builders render the real record envelope and
    cover all four audited shapes with deterministic counts. file: `tests/sessions/transcripts/fixtures/grok_streams.py`.

    2.1.2: The accounting suite pins corrected `turn_count`, usage totals, pair counts,
    and metadata suppression over all four canned streams. test: `tests/sessions/transcripts/test_grok_turn_accounting.py::test_audited_shapes_turn_and_usage_accounting`.'
  labels:
  - covers:grok-turn-accounting:2.1:2.1.1
  - covers:grok-turn-accounting:2.1:2.1.2
  tdd: false
  source_section: '2.1'
  assigned_agent: backend-developer
- title: User-anchored coverage recompute
  category: test
  task_type: feature
  depends_on:
  - '2.1'
  validation_criteria: '2.2.1: The metric implementation and 100%-anchoring assertions
    over the four canned streams pass. test: `tests/sessions/transcripts/test_grok_coverage_audit.py::test_user_anchored_coverage_on_audited_shapes`.

    2.2.2: The env-gated replay computes and reports coverage for real transcripts
    and is skipped by default. test: `tests/sessions/transcripts/test_grok_coverage_audit.py::test_real_transcript_replay_opt_in`.'
  labels:
  - covers:grok-turn-accounting:2.2:2.2.1
  - covers:grok-turn-accounting:2.2:2.2.2
  tdd: false
  source_section: '2.2'
  assigned_agent: backend-developer
```
