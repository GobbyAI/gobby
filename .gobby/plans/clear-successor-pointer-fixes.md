# Clear Successor Pointer Fixes

> **Plan ID:** clear-successor-pointer-fixes
>
> Replaces the shelved in-place clear-handoff plan
> (`/Users/josh/.claude/plans/in-place-clear-handoff-memoized-beaver.md` and the
> amended draft that followed it). The successor-row model stays; this plan fixes
> the two verified breaks it leaves behind.

## Context
`kind: framing`

`set_handoff(clear_session=true)` creates a successor session row (`#N+1`) and
already transfers task claims, arms the handoff pull, and carries the title
(`_bind_clear_successor` → `take_clear_handoff_marker`). Two pointers are left
dangling, both verified against the current tree:

1. **Spawned agents go deaf.** Nothing retargets `agent_runs.parent_session_id`
   when a coordinator clears itself, so `list_runs(parent=successor)` is empty and
   the mailbox's agent-target join still resolves the expired predecessor.
   `_select_agent_recipient` (`src/gobby/sessions/mailbox.py:764`) only accepts
   `active`/`paused` recipients, and a child addressing its parent by the baked
   `GOBBY_PARENT_SESSION_ID` UUID hits `_validate_direct_recipient`, which never
   redirects off an expired row — the message is undeliverable.
2. **Web chat reattaches to the corpse.** The browser's held conversation id *is*
   the predecessor's db session id. `_create_chat_session_inner`
   (`src/gobby/servers/websocket/chat/_session.py:601`) resolves
   `session_manager.get(conversation_id)` and reattaches to the expired
   `web_chat` row regardless of status, and nothing pushes a `session_info` frame
   after a clear, so the browser never learns the successor id.

Everything else about the in-place proposal (same `#N`, SessionEnd gating, probe
hoisting, epoch-aware transcript reader, archival sweeper, cumulative stats) is
deliberately dropped: history already survives per row, and stable session refs
are not worth five phases of mechanism.

## Overview
`kind: framing`

Three localized deliverables:

1. Retarget `agent_runs.parent_session_id` inside the existing marker-take
   transactions, and give the mailbox a clear-successor hop for direct UUID
   recipients (following `clear_attempt.consumed_by`).
2. Make web chat converge on the live successor: reattach a rebuilt wrapper via
   the same successor hop, and push a `session_info` frame with the successor id
   after the live-wrapper rewire.
3. Make terminal clear delivery honest: report success only on positive
   acknowledgment (a new provider session, or a SessionStart promoted to
   `source=clear`), restore the staged attempt on timeout, and park
   predecessor resume/rebind while a clear attempt is pending so
   auto-continuation cannot reclaim the interrupted provider thread
   (observed on Codex attempt 2a7c6eb37bea45be87e027cb78c89bfe: `/clear`
   dispatched, no new rollout, continuation resumed the old thread, handoff
   stranded on the predecessor).

## Constraints
`kind: framing`

Verified facts:

- `take_clear_handoff_marker` (`src/gobby/sessions/clear_continuation.py:181`)
  already runs `db.transaction_immediate(SessionLineageMutation())`, locks the
  successor row and the predecessor's variables `FOR UPDATE`, consumes the marker
  (`consumed_by = successor_id`), and writes `sessions.parent_session_id` on the
  successor. The web-chat equivalent is `_commit_web_chat_clear_successor_rows`
  (`clear_continuation.py:340`). Both are the correct transaction to carry the
  `agent_runs` retarget — no new writer.
- The clear lineage is recoverable without new state: the predecessor's
  `clear_attempt` marker records `consumed_by` (the successor id). A chain of
  clears is a `consumed_by` walk.
- Mailbox agent targets resolve child/parent ids from an `agent_runs` join
  (`_resolve_agent_target`, `_agent_recipient_session_ids`), so retargeting
  `agent_runs.parent_session_id` fixes those paths with no mailbox change.
  Direct recipients go through `_validate_direct_recipient`
  (`mailbox.py:365`), which fetches the row and validates project scope only —
  that is where the expired→successor hop belongs.
- `DELIVERABLE_SESSION_STATUSES = ("active", "paused")` (`mailbox.py:37`);
  `TERMINAL_SESSION_STATUSES = frozenset({"expired", "deleted"})`
  (`src/gobby/storage/sessions/_constants.py:36`).
- `sessions.parent_session_id` on child sessions is lineage (who spawned whom)
  and is not used for delivery; it stays untouched. Only the operational pointer
  (`agent_runs.parent_session_id`) is retargeted.
- The live web-chat wrapper is already rewired on clear:
  `_clear_commit.py` sets `session.db_session_id = successor.id` and
  `wire_db_persist_callbacks` repoints persistence. The gaps are the browser
  (no `session_info` frame on clear — the frame type exists and is sent
  request-scoped from `_streaming.py:320` with `db_session_id` and
  `session_ref`) and a wrapper rebuilt later for the old conversation id.
- Agent depth limit 5 bounds any successor-chain walk.

Decisions:

- The hop helper is `resolve_clear_successor(db, session_id)` in
  `clear_continuation.py`: while the row is terminal and its `clear_attempt`
  marker has a `consumed_by` naming a different row, step to it (max 5 hops);
  return the final live row id or None. Read-only, no locks.
- Direct-recipient redirect is silent but recorded: the stored message metadata
  gains `redirected_from: <original id>` so transcripts show the hop.
- A redirect that finds no live successor keeps today's behavior (deliver to the
  addressed row; it simply goes unread) — refusing would break senders that
  intentionally target paused-then-expired rows no worse than today.
- Web chat pushes exactly one `session_info` frame after a successful clear
  commit, from the commit hook where the transport lives; no other frames change.
- No backward compatibility concerns (0.5.0 unshipped).

Non-goals:

- No change to session identity, `#N` allocation, `SessionEnd(reason=clear)`,
  the clear probe, deferred materialization, titles, claims, or the transcript
  reader. The in-place model is shelved, not deferred.
- No retarget of child `sessions.parent_session_id` (lineage stays historical).
- No agent-run clear support (unchanged).

## P1: Parentage and mailbox
`kind: framing`

**Goal**: After a coordinator clears itself, its spawned runs list under the
successor and messages addressed to the predecessor land on the successor.

### 1.1 Retarget agent runs and hop the mailbox [category: code]
`kind: deliverable`

Targets:
- `src/gobby/sessions/clear_continuation.py::take_clear_handoff_marker`
- `src/gobby/sessions/clear_continuation.py::_commit_web_chat_clear_successor_rows`
- `src/gobby/sessions/mailbox.py::MailboxService._validate_direct_recipient`
- `src/gobby/sessions/mailbox.py::MailboxService.send`
- `tests/sessions/test_mailbox.py::*` — scope-reason: mailbox suite gains retarget, redirect, chain, and no-successor cases
- `tests/hooks/test_session_start_handlers.py::*` — scope-reason: consumer of `take_clear_handoff_marker`; existing clear-bind suites gain the agent-runs retarget assertion
- `src/gobby/hooks/event_handlers/_session_start/materialize.py::*` — scope-reason: consumer of `take_clear_handoff_marker`; call sites unchanged, revalidated by the clear-bind suite
- `src/gobby/communications/telegram_actions.py::*` — scope-reason: consumer of `MailboxService.send`; unchanged call sites, redirect metadata is additive
- `src/gobby/mcp_proxy/tools/agent_messaging.py::*` — scope-reason: consumer of `MailboxService.send`; unchanged call sites, redirect metadata is additive
- `src/gobby/servers/routes/tasks_assignment.py::*` — scope-reason: consumer of `MailboxService.send`; unchanged call sites, redirect metadata is additive
- `tests/communications/test_telegram_actions.py::*` — scope-reason: consumer suite of `MailboxService.send`; asserts existing sends stay redirect-free
- `tests/mcp_proxy/tools/test_agent_messaging_broadcast.py::*` — scope-reason: consumer suite of `MailboxService.send`; broadcast paths stay unredirected

The new `resolve_clear_successor` helper lands in
`src/gobby/sessions/clear_continuation.py` (already targeted above; the symbol
does not exist in the index yet).

In `take_clear_handoff_marker`, after the successor `parent_session_id` write and
inside the same transaction:

```python
conn.execute(
    "UPDATE agent_runs SET parent_session_id = %s WHERE parent_session_id = %s",
    (successor_id, predecessor_id),
)
```

Add the identical statement to `_commit_web_chat_clear_successor_rows` (web-chat
coordinators spawn agents too).

Add the hop helper:

```python
def resolve_clear_successor(db: HubDatabase, session_id: str) -> str | None:
    """Follow clear_attempt.consumed_by from a terminal row to its live successor.

    Walk at most 5 hops: stop and return the current id when its status is not
    in TERMINAL_SESSION_STATUSES; step when the row's clear_attempt marker has a
    consumed_by naming a different session; otherwise return None.
    """
```

In `_validate_direct_recipient`: when the fetched recipient's status is in
`TERMINAL_SESSION_STATUSES`, call `resolve_clear_successor`; on a hit, re-fetch
and validate the successor instead and return its id. `send` records
`redirected_from` in the message metadata when the validated id differs from the
addressed one. Agent-target and broadcast paths are untouched (the `agent_runs`
retarget already fixes them at the source).

Tests (`tests/sessions/test_mailbox.py` plus the clear-bind suite): after a
staged clear and `take_clear_handoff_marker`, `agent_runs` rows point at the
successor; a direct send to the expired predecessor delivers to the successor
with `redirected_from` set; a two-clear chain resolves transitively; a terminal
row with no consumed marker keeps today's behavior; an active recipient is never
redirected; the web-chat commit performs the same retarget.

**Acceptance:**

- 1.1.1 - Both clear-commit transactions retarget `agent_runs.parent_session_id` from predecessor to successor atomically with the marker take. symbol: `take_clear_handoff_marker`.
- 1.1.2 - A direct message addressed to a cleared-and-expired session delivers to its live successor (transitively across chained clears) with `redirected_from` metadata; non-clear terminal rows and live recipients behave as today. symbol: `MailboxService._validate_direct_recipient`.
- 1.1.3 - `resolve_clear_successor` walks `consumed_by` with a 5-hop cap and returns None when no live successor exists. symbol: `resolve_clear_successor`.
- 1.1.4 - Retarget, redirect, chain, and no-successor cases are covered. test: `tests/sessions/test_mailbox.py`.

## P2: Web chat convergence
`kind: framing`

**Goal**: After a web-chat clear, the browser and any rebuilt wrapper hold the
live successor id.

### 2.1 Successor reattach and session_info push [category: code] (depends: 1.1)
`kind: deliverable`

Targets:
- `src/gobby/servers/websocket/chat/_clear_commit.py::*` — scope-reason: commit hook gains the session_info push and the shared frame-derivation helper call
- `src/gobby/servers/websocket/chat/_reattach.py` — new module: terminal-candidate successor resolution helper, keeping `_session.py` (948 lines) under the production ceiling
- `src/gobby/servers/websocket/chat/_session.py::ChatSessionMixin._create_chat_session_inner`
- `tests/servers/websocket/chat/test_clear_session.py::*` — scope-reason: commit suite gains the session_info push assertion
- `tests/servers/websocket/chat/test_servers_websocket_chat_session.py::*` — scope-reason: consumer of `_create_chat_session_inner`; expired-predecessor reattach cases added

In `_clear_commit.py`, after the wrapper rewire (`session.db_session_id =
successor.id` and `wire_db_persist_callbacks`), send one `session_info` frame
through the session's transport with the updated `db_session_id` and
`session_ref`, mirroring the request-scoped frame built in `_streaming.py:320`
(reuse its field derivation; factor a small helper rather than duplicating the
dict). Best-effort: a send failure logs and never fails the commit.

Move the candidate-redirect logic out of `_session.py` into the new
`src/gobby/servers/websocket/chat/_reattach.py`: a small helper
(`redirect_terminal_web_chat_candidate`) takes the resolved candidate row and
the storage handle; when the candidate is a `web_chat` row whose status is in
`TERMINAL_SESSION_STATUSES`, it calls `resolve_clear_successor` (1.1) and
returns the fetched successor row on a hit (logging
`web_chat_reattach_redirected`), else the original candidate.
`_create_chat_session_inner` calls the helper at candidate resolution so
provider, project, and mode derivation read the live row — a one-line change
that keeps `_session.py` (948 lines) under the production ceiling. No hit →
today's behavior.

Tests: commit pushes exactly one `session_info` frame with the successor id and
a transport failure leaves the commit successful; a wrapper rebuilt for the
predecessor's conversation id after a clear binds `db_session_id` to the
successor and appends messages there (regression for the expired-predecessor
reattach); an expired non-clear web-chat row keeps today's behavior.

**Acceptance:**

- 2.1.1 - A successful web-chat clear pushes one `session_info` frame carrying the successor `db_session_id` and `session_ref`; frame failure never fails the commit. file: `src/gobby/servers/websocket/chat/_clear_commit.py`.
- 2.1.2 - A wrapper rebuilt for a cleared conversation id reattaches to the live successor row. symbol: `ChatSessionMixin._create_chat_session_inner`.
- 2.1.3 - Reattach redirect, non-clear expired fallback, and frame push are covered. test: `tests/servers/websocket/chat/test_clear_session.py`.

## P3: Clear acknowledgment and continuation parking
`kind: framing`

**Goal**: `set_handoff(clear_session=true)` on a terminal session either
verifiably binds a successor or fails loudly with the attempt restored; no
auto-continuation path can reclaim the provider thread while the clear is
pending.

Verified facts:

- `execute_clear_session`
  (`src/gobby/mcp_proxy/tools/sessions/_terminal_clear.py:57`) stages the
  attempt, shields delivery from caller cancellation (e2cc38e, #21356), and
  already has `restore_failed_attempt()` for the immediate-rejection path — but
  `deliver_clear` reports `command_sent: true` on successful tmux key dispatch
  alone. `_send_terminal_compaction_command`
  (`_terminal_tmux.py:178`) checks only for immediate rejection; there is no
  positive `/clear` acknowledgment.
- The successor binds when the provider emits a fresh session and SessionStart
  promotes it via `resolve_matching_clear_continuation`
  (`src/gobby/hooks/event_handlers/_session_start/handoff.py:232`); a resumed
  provider thread instead reaches `rebind_resumed_session_start` (`:326`) and
  reclaims the predecessor, leaving `clear_attempt.consumed_by` null and the
  handoff stranded (Codex attempt 2a7c6eb37bea45be87e027cb78c89bfe).
- The staged marker already carries `attempt_id` and an expiry
  (`_marker_expired`), so "pending clear attempt" is a readable predicate on
  the predecessor's variables — no new state is required for parking.

### 3.1 Positive clear acknowledgment and parked resume [category: code]
`kind: deliverable`

Targets:
- `src/gobby/mcp_proxy/tools/sessions/_terminal_clear.py::execute_clear_session`
- `src/gobby/mcp_proxy/tools/sessions/_terminal_tmux.py::_send_terminal_compaction_command`
- `src/gobby/hooks/event_handlers/_session_start/handoff.py::resolve_matching_clear_continuation`
- `src/gobby/hooks/event_handlers/_session_start/handoff.py::rebind_resumed_session_start`
- `tests/mcp_proxy/tools/sessions/test_terminal_clear.py::*` — scope-reason: existing clear suite mocks `_send_terminal_compaction_command`; gains acknowledgment, timeout-restore, and result-contract cases
- `tests/sessions/test_clear_acknowledgment.py`
- `src/gobby/mcp_proxy/tools/sessions/_terminal.py::*` — scope-reason: consumer of `_send_terminal_compaction_command`; call sites adapt to the acknowledgment-bearing result contract
- `src/gobby/hooks/session_materialize.py::*` — scope-reason: consumer of `resolve_matching_clear_continuation`; promotion call sites revalidated under the parked-resume predicate
- `src/gobby/hooks/event_handlers/_session_start/flow.py::*` — scope-reason: consumer of `rebind_resumed_session_start`; resume flow honors the park
- `tests/hooks/test_session_materialize.py::*` — scope-reason: consumer suite of `resolve_matching_clear_continuation`; promotion cases extended for pending-attempt parking
- `tests/hooks/test_session_events_coverage.py::*` — scope-reason: consumer suite of `rebind_resumed_session_start`; resume cases extended for pending-attempt parking

After dispatching `/clear`, `execute_clear_session` waits (bounded) for
positive acknowledgment: the predecessor's `clear_attempt` marker consumed
(`consumed_by` set by a SessionStart promoted to `source=clear`), or a new
provider-native session observed for the pane (fresh external id / Codex
rollout). On acknowledgment it returns success naming the bound successor when
known; on timeout it calls `restore_failed_attempt()` and returns a failure
result — never `command_sent: true` as a success proxy (the field may remain
as diagnostic detail).

While the predecessor has an unconsumed, unexpired `clear_attempt`,
`rebind_resumed_session_start` must not rebind a resumed provider thread to
the predecessor: the resume parks (returns without reclaiming, logged) so the
pending clear stays winnable; promotion through
`resolve_matching_clear_continuation` remains the only path that consumes the
marker. A marker that expires or is restored lifts the park.

`tests/sessions/test_clear_acknowledgment.py` (new) covers the lifecycle on an
isolated hub: staged attempt → interrupt → dispatched clear → new provider
session → successor binding → one-shot `get_handoff()`; plus timeout-restore
and the parked-resume race (a resume arriving while the attempt is pending
does not reclaim the predecessor).

**Acceptance:**

- 3.1.1 - Clear success is reported only on positive acknowledgment (marker consumed or new provider session); tmux dispatch alone never reports success. symbol: `execute_clear_session`.
- 3.1.2 - Acknowledgment timeout restores the staged attempt and returns a failure result. symbol: `execute_clear_session`.
- 3.1.3 - A resumed provider thread cannot rebind the predecessor while its clear attempt is unconsumed and unexpired; promotion to `source=clear` remains the only consumer. symbol: `rebind_resumed_session_start`.
- 3.1.4 - Lifecycle, timeout-restore, and parked-resume cases run against an isolated hub. test: `tests/sessions/test_clear_acknowledgment.py`.

## Verification Strategy
`kind: framing`

Per-leaf TDD evidence plus, on an isolated test daemon (`DATABASE_URL` at the
test hub, `GOBBY_TEST_PROTECT=1`):

- Coordinator clear: `#N` spawns agent B, calls
  `set_handoff(..., clear_session=true)`; after the successor binds, assert
  `list_runs(parent=#N+1)` lists B, B's `send_message` to the baked
  `GOBBY_PARENT_SESSION_ID` UUID lands in `#N+1`'s mailbox with
  `redirected_from = #N`, and a second clear chains the redirect to `#N+2`.
- Web chat: clear a live chat; assert the browser receives a `session_info`
  frame with the successor id; drop the wrapper, reopen the old conversation id,
  and assert the rebuilt wrapper binds and persists to the successor row.
- Regressions: a direct send to an active session carries no redirect metadata;
  `test_clear_session.py` and `test_session_start_handlers.py` clear suites pass
  unchanged apart from the new assertions.
- Static gates: `uv run ruff format src/`, `uv run ruff check src/`,
  `uv run mypy src/`, the test-types audit, and
  `uv run gobby plans validate .gobby/plans/clear-successor-pointer-fixes.md`.

## M1 Task Manifest
`kind: manifest`

```yaml
- title: Retarget agent runs and hop the mailbox
  category: code
  task_type: feature
  depends_on: []
  validation_criteria: '1.1.1: Both clear-commit transactions retarget `agent_runs.parent_session_id`
    from predecessor to successor atomically with the marker take. symbol: `take_clear_handoff_marker`.

    1.1.2: A direct message addressed to a cleared-and-expired session delivers to
    its live successor (transitively across chained clears) with `redirected_from`
    metadata; non-clear terminal rows and live recipients behave as today. symbol:
    `MailboxService._validate_direct_recipient`.

    1.1.3: `resolve_clear_successor` walks `consumed_by` with a 5-hop cap and returns
    None when no live successor exists. symbol: `resolve_clear_successor`.

    1.1.4: Retarget, redirect, chain, and no-successor cases are covered. test: `tests/sessions/test_mailbox.py`.'
  labels:
  - covers:clear-successor-pointer-fixes:1.1:1.1.1
  - covers:clear-successor-pointer-fixes:1.1:1.1.2
  - covers:clear-successor-pointer-fixes:1.1:1.1.3
  - covers:clear-successor-pointer-fixes:1.1:1.1.4
  tdd: true
  source_section: '1.1'
  implementation_domain: backend
- title: Successor reattach and session_info push
  category: code
  task_type: feature
  depends_on:
  - '1.1'
  validation_criteria: '2.1.1: A successful web-chat clear pushes one `session_info`
    frame carrying the successor `db_session_id` and `session_ref`; frame failure
    never fails the commit. file: `src/gobby/servers/websocket/chat/_clear_commit.py`.

    2.1.2: A wrapper rebuilt for a cleared conversation id reattaches to the live
    successor row. symbol: `ChatSessionMixin._create_chat_session_inner`.

    2.1.3: Reattach redirect, non-clear expired fallback, and frame push are covered.
    test: `tests/servers/websocket/chat/test_clear_session.py`.'
  labels:
  - covers:clear-successor-pointer-fixes:2.1:2.1.1
  - covers:clear-successor-pointer-fixes:2.1:2.1.2
  - covers:clear-successor-pointer-fixes:2.1:2.1.3
  tdd: true
  source_section: '2.1'
  implementation_domain: backend
- title: Positive clear acknowledgment and parked resume
  category: code
  task_type: feature
  depends_on: []
  validation_criteria: '3.1.1: Clear success is reported only on positive acknowledgment
    (marker consumed or new provider session); tmux dispatch alone never reports success.
    symbol: `execute_clear_session`.

    3.1.2: Acknowledgment timeout restores the staged attempt and returns a failure
    result. symbol: `execute_clear_session`.

    3.1.3: A resumed provider thread cannot rebind the predecessor while its clear
    attempt is unconsumed and unexpired; promotion to `source=clear` remains the only
    consumer. symbol: `rebind_resumed_session_start`.

    3.1.4: Lifecycle, timeout-restore, and parked-resume cases run against an isolated
    hub. test: `tests/sessions/test_clear_acknowledgment.py`.'
  labels:
  - covers:clear-successor-pointer-fixes:3.1:3.1.1
  - covers:clear-successor-pointer-fixes:3.1:3.1.2
  - covers:clear-successor-pointer-fixes:3.1:3.1.3
  - covers:clear-successor-pointer-fixes:3.1:3.1.4
  tdd: true
  source_section: '3.1'
  implementation_domain: backend
```
