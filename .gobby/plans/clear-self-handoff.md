Plan artifact: `.gobby/plans/clear-self-handoff.md`

# Clear-Self Deliberate Context-Clear Handoff

> **Plan ID:** clear-self-handoff

## Overview
`kind: framing`

Add a `clear_self` MCP tool (gobby-sessions) that lets an agent deliberately clear
its own context with a durable, agent-authored handoff to its successor session.
Compaction (`compact_self`) preserves the session row; `/clear` creates an
independent session with no context transfer — today that boundary is absolute,
and the only artifact of the old 0.4.x clear-handoff path is a fossil rule that
injects an empty "Previous Session Context" block on every clear. `clear_self`
makes the clear boundary crossable exactly once, on purpose: the caller supplies
the handoff text, it is durably staged before `/clear` is ever sent, the
successor binds by terminal identity (terminal) or by row parentage (web chat),
inherits task claims, and receives the handoff on its first prompt. A plain
user-typed `/clear` without `clear_self` remains a hard boundary.

## Constraints
`kind: framing`

Confirmed decisions:

- Task claims held by the caller are reassigned to the successor session
  (revival of the 0.4.x filter-and-reassign logic, hardened with
  expected-owner compare-and-swap). No opt-out parameter.
- Web chat ships in v1 alongside terminal.
- The fossil rule `inject-previous-session-summary` is deleted, not repurposed;
  a new turn_start rule delivers the clear handoff.
- `handoff` is a required argument to `clear_self`. No digest or summarizer
  fallback — a missing/empty handoff is an error.
- Agent-run sessions are rejected by `clear_self` in v1 with a structured
  error: SessionEnd(reason='clear') terminalization would finish the agent
  run, delete workflow instances, and release worktrees underneath the
  successor. v1 supports interactive terminal and web-chat sessions only;
  agent-run ownership transfer is an explicit non-goal.

Named defaults:

- Marker TTL: 600 seconds (`CLEAR_HANDOFF_TTL_SECONDS = 600`). A marker older
  than the TTL is ignored and the successor degrades to plain startup.
- Ambiguous terminal-identity match (multiple unconsumed markers) degrades to
  plain startup with a structured warning. Clear never blocks session start —
  unlike compact, which blocks on ambiguity because it revives an existing row,
  a clear successor is an independent row and safe to start uninjected.
- Delivery is first-prompt injection: inline when the handoff fits
  `handoff_summary_inject_budget_for(source)`, otherwise a breadcrumb pointer
  to `get_handoff_context` carrying the predecessor session ref (the
  `_bound_handoff_summary` shape). No new retrieval tool is added;
  `get_handoff_context` is extended so a bound successor may dereference its
  direct predecessor after expiry (section 1.1).
- The handoff carries work context only — no persona/skill-reload freight. The
  successor session gets persona, wiki, and skill injections natively from the
  epoch re-arm that already treats clear as full context loss.
- Web chat clear is restart-based at the backend interface: stop the managed
  backend session and start a fresh one, preserving model and chat mode, with
  per-provider reset of continuation/resume identifiers. Codex keeps its
  existing thread-archive specialization.
- Clear staging is attempt-scoped, never status-based: the handoff text is
  stored via the summary fast path (`update_summary`) together with a
  one-shot `clear_attempt` marker keyed by a fresh `attempt_id`.
  `clear_self` never sets `handoff_ready` status — retrieval works via
  explicit session ref, compact's status-based parent discovery can never
  consume a staged clear attempt, and a failed attempt is cleaned by
  compare-and-clear that restores the prior summary state.
- Successor binding is an atomic take: a single transaction claims the
  marker for exactly one successor before any seeding, claim transfer, or
  injection side effect.
- A web-chat clear queued behind an active turn is durable: the staged
  attempt record (attempt_id, handoff ref, model, mode) is the queue entry
  of record. Concurrent `clear_self` calls coalesce deterministically to the
  pending attempt; a queued call reports `queued`, never final success.

Non-goals:

- No behavior change for user-typed `/clear` without a prior `clear_self` call.
- No change to compaction (`compact_self`) behavior or storage.
- No agent-run, workflow-instance, or worktree ownership transfer across the
  clear boundary in v1 (agent-run sessions are rejected).
- No backward compatibility shims — 0.5.0 has not shipped.

## P1: Clear marker and terminal clear_self
`kind: framing`

**Goal**: An agent can call `clear_self(handoff=...)` in a terminal session; the
handoff is durably staged under a clear-attempt record, a one-shot binding
marker is written, and `/clear` is delivered through the existing terminal
compaction sender.

### 1.1 Clear continuation module [category: code]
`kind: deliverable`

Targets:
- `src/gobby/sessions/clear_continuation.py`
- `src/gobby/mcp_proxy/tools/sessions/_handoff.py::get_handoff_context`
- `tests/sessions/test_clear_continuation.py`
- `tests/mcp_proxy/test_mcp_tools_session_messages.py::*` — scope-reason: handoff-retrieval authorization assertions extended for bound-successor access to an expired predecessor

Create `src/gobby/sessions/clear_continuation.py`, the clear analog of the
compact continuation module (`gobby.sessions.compact_continuation`), owning the
attempt lifecycle, successor-side resolution, variable seeding, and the
continuation prompt. Public surface:

```python
CLEAR_HANDOFF_TTL_SECONDS = 600


@dataclass
class ClearContinuationResolution:
    """Successor-side resolution of a pending clear handoff."""
    predecessor: Any | None      # session row or None
    attempt_id: str | None       # the staged attempt being bound
    degrade_reason: str | None   # set when a marker existed but was unusable


def stage_clear_attempt(
    db: HubDatabase,
    session_id: str,
    *,
    attempt_id: str,
    terminal_context: dict[str, Any] | None,
    chat_context: dict[str, Any] | None,
) -> dict[str, Any]:
    """Write the one-shot clear-attempt marker on the predecessor row.

    Marker is the session variable `clear_attempt`:
    {"attempt_id": ..., "created_at": <iso-utc>,
     "terminal_context": {...} | null, "chat": {"model": ..., "mode": ...} | null,
     "consumed_by": null}.
    Returns the prior summary state captured for failure restoration. The
    handoff text itself is stored via the summary fast path (update_summary)
    by the caller in 1.2 — handoff_ready status is never set, so compact's
    status-based parent discovery cannot see a staged clear attempt.
    There is exactly one marker writer: the clear_self tool body, immediately
    before the /clear send. Sender callbacks never write markers.
    """


def resolve_clear_continuation(
    db: HubDatabase,
    *,
    source: str,
    project_id: str,
    machine_id: str,
    terminal_context: dict[str, Any] | None,
    predecessor_hint: str | None,
) -> ClearContinuationResolution:
    """Find the predecessor for a SessionStart(source='clear').

    Candidate search is scoped to this machine_id + project_id + cli source
    with an unconsumed, in-TTL `clear_attempt` marker. Binding requires
    either the exact trusted predecessor id (predecessor_hint — the
    gobby_session_id carried by a same-process clear) or
    `terminal_process_contexts_match` (pid/tty/tmux-pane identity from
    gobby.sessions.handoff_identity — deliberately stronger than
    terminal_context_matches_session, which is insufficient against
    terminal-identity reuse). Zero matches, an expired marker, an identity
    mismatch, cross-project or cross-machine markers, and multiple matches
    all resolve to predecessor=None with a degrade_reason — clear never
    blocks session start.
    """


def take_clear_handoff_marker(
    db: HubDatabase,
    predecessor_id: str,
    *,
    attempt_id: str,
    successor_id: str,
) -> bool:
    """Atomic one-shot take: a single transaction compare-and-swaps
    consumed_by from null to successor_id where attempt_id matches, and
    writes parent_session_id on the successor row in that same
    transaction — parentage survives any later seeding failure.
    Exactly one of any number of concurrent takers wins; losers get False
    and proceed as plain startup. All successor side effects (seeding,
    claim transfer, injection scheduling) are gated on a successful take."""


def clear_failed_attempt(
    db: HubDatabase,
    session_id: str,
    *,
    attempt_id: str,
    prior_summary_state: dict[str, Any],
) -> bool:
    """Compare-and-clear cleanup after a failed clear attempt: remove the
    marker only if its attempt_id is unchanged and unconsumed, and restore
    the captured prior summary state. Returns False (no-op) when the
    attempt state changed underneath — restoration is conditional."""


def seed_clear_handoff_variables(
    session_manager: Any,
    successor_session_id: str,
    predecessor: Any,
) -> None:
    """Seed the successor row after a successful atomic take.

    - `handoff_summary_injectable`: predecessor summary_markdown bounded by
      `_bound_handoff_summary` semantics (inline when under
      handoff_summary_inject_budget_for; else the get_handoff_context
      breadcrumb carrying the predecessor's #N ref).
    - `clear_handoff_inject_pending`: true (consumed by the 2.3 rule).
    parent_session_id is NOT written here — the take transaction owns it.
    """


def build_clear_self_continue_prompt(*, predecessor_ref: str) -> str:
    """Continuation prompt sent to the successor terminal after /clear lands,
    instructing it to continue from the injected handoff context. Without this
    an autonomous successor idles at an empty prompt forever."""


def schedule_clear_self_continuation(...) -> bool:
    """Schedule delivery of the continue prompt to the successor terminal,
    mirroring the compact continuation scheduling shape (tmux send after the
    successor registers; Codex readiness handling reused where applicable)."""
```

Behavioral notes:

- The marker lives on the predecessor row and survives SessionEnd(clear)
  expiring that row; resolution matches on marker + scoped identity, never on
  session status.
- TTL and degrade semantics follow the 0.4.x find-parent-session contract:
  verify, and on any mismatch inject nothing and proceed as plain startup.
- No summarizer, digest, transcript-tail, or blocking-refresh machinery from
  the compact handoff path is carried over — the handoff text is always
  caller-supplied.
- Breadcrumb dereference must work after the predecessor expires: extend
  `get_handoff_context` in `src/gobby/mcp_proxy/tools/sessions/_handoff.py`
  so a same-project session whose parent_session_id is the requested session
  may read its direct predecessor's stored handoff regardless of the
  predecessor's status (today the getter requires handoff_ready except for
  self-access, so a bound successor could not dereference an oversized
  handoff).

**Acceptance:**

- 1.1.1 - Module exists with attempt staging, scoped resolution, atomic take, conditional cleanup, seeding, and prompt builders. file: `src/gobby/sessions/clear_continuation.py`.
- 1.1.2 - Resolution binds only within machine_id + project_id + source scope on an unconsumed, in-TTL marker, and requires the trusted predecessor hint or `terminal_process_contexts_match`; cross-project, cross-machine, expired, reused-terminal, and ambiguous cases degrade with a reason. symbol: `resolve_clear_continuation`.
- 1.1.3 - The marker take is a single-transaction compare-and-swap that also writes the successor's parent_session_id: under concurrent takers exactly one wins and losers degrade. symbol: `take_clear_handoff_marker`.
- 1.1.4 - Seeding writes bounded `handoff_summary_injectable` and `clear_handoff_inject_pending` on the successor; parentage is owned by the take transaction. symbol: `seed_clear_handoff_variables`.
- 1.1.5 - Failed-attempt cleanup compare-clears the marker and restores the prior summary state only when the attempt is unchanged and unconsumed. symbol: `clear_failed_attempt`.
- 1.1.6 - A bound successor can dereference its direct predecessor's stored handoff after the predecessor row is expired (oversized-handoff breadcrumb path). test: `tests/sessions/test_clear_continuation.py`.

### 1.2 clear_self terminal tool [category: code] (depends: 1.1)
`kind: deliverable`

Targets:
- `src/gobby/mcp_proxy/tools/sessions/_terminal.py::register_terminal_tools`
- `src/gobby/mcp_proxy/tools/sessions/_terminal_clear.py`
- `tests/mcp_proxy/tools/sessions/test_terminal_clear.py`

Register a `clear_self(handoff: str)` tool inside `register_terminal_tools`,
with the implementation body in a new `_terminal_clear.py` module (the host
module is already ~730 lines; the monolith ceiling forbids growing it near
1,000). Flow, mirroring `compact_self`'s resolution steps:

1. Reject empty/whitespace `handoff` with a clear error naming the requirement.
2. Resolve the calling session from MCP SessionContext
   (`_resolve_session_for_compaction`) and reject agent-run sessions with a
   structured error (`clear_self` is interactive-only in v1; a session bound
   to an agent run must not have its run terminalized underneath a
   successor).
3. Resolve and authorize the tmux target (`_resolve_tmux_target`,
   `_authorize_send_keys_target`), and backfill tmux context from a sibling
   when absent (`_backfill_tmux_context_from_sibling`).
4. Stage the attempt: generate a fresh `attempt_id`, store the handoff via
   the summary fast path (`update_summary` — never `handoff_ready` status),
   and write the `clear_attempt` marker with `stage_clear_attempt`,
   capturing the prior summary state. The tool body is the single marker
   writer and stages immediately before the terminal send — no race with
   the /clear, and sender callbacks never write markers.
5. Send `/clear` through `_send_terminal_compaction_command` with
   clear-specific continuation scheduling from `clear_continuation` (Codex
   interrupt handling comes free from the sender).
6. Web chat sessions: delegate to the web-chat clear fallback (section 3.3);
   until that lands, return a structured "web chat not yet supported" error.
7. Return `{success, session_id, attempt_id, handoff_staged: true,
   command_sent}`.

Error paths: no tmux target and not a live web-chat session → structured
error; storage failure → error before any terminal interaction (never send
/clear with an unstaged handoff); any failure after staging → `
clear_failed_attempt` compare-clears the marker and restores the prior
summary state, so a later compact or plain /clear can never consume the
leftovers of a failed attempt.

**Acceptance:**

- 1.2.1 - `clear_self` is registered on gobby-sessions and requires a non-empty handoff. symbol: `register_terminal_tools`. file: `src/gobby/mcp_proxy/tools/sessions/_terminal_clear.py`.
- 1.2.2 - Handoff staging and marker write both complete before `/clear` is sent; a storage failure aborts without terminal interaction. file: `src/gobby/mcp_proxy/tools/sessions/_terminal_clear.py`.
- 1.2.3 - Terminal delivery reuses the compaction sender with command `/clear`, including Codex interrupt handling. test: `tests/mcp_proxy/tools/sessions/test_terminal_clear.py`.
- 1.2.4 - An agent-run session calling `clear_self` receives a structured rejection and no state is staged. test: `tests/mcp_proxy/tools/sessions/test_terminal_clear.py`.
- 1.2.5 - A failure after staging compare-clears the attempt and restores the prior summary state. test: `tests/mcp_proxy/tools/sessions/test_terminal_clear.py`.

## P2: Successor binding and delivery
`kind: framing`

**Goal**: The session that starts after a `clear_self`-initiated `/clear` binds
to its predecessor, inherits task claims, and receives the handoff on its first
prompt; every failure mode degrades to today's independent-session behavior.

### 2.1 SessionStart clear binding and variable seeding [category: code] (depends: P1)
`kind: deliverable`

Targets:
- `src/gobby/hooks/event_handlers/_session_start/handoff.py::resolve_session_start_identity`
- `src/gobby/hooks/event_handlers/_session_start/handoff.py::SessionStartResolution`
- `src/gobby/hooks/event_handlers/_session_start/flow.py::handle_session_start`
- `tests/hooks/test_session_handoff_handlers.py::*` — scope-reason: clear-binding, fast-path-bypass, atomic-take, and degrade coverage added alongside the compact handoff suite

Today `resolve_session_start_identity` returns
`SessionStartResolution(session=None, ...)` unconditionally for
`session_source == "clear"`. Change the clear branch to call
`resolve_clear_continuation` with the incoming terminal context, project,
machine, and the trusted predecessor hint (the gobby_session_id a
same-process clear carries):

- Ordering: for `source == "clear"` the clear predecessor resolution runs
  BEFORE every existing-row lookup in the SessionStart flow, and every
  existing-row reuse path is skipped for clear: the inactive
  pre-created-row early returns, the live pre-created-row reuse via
  external_id (`_handle_pre_created_session`), the
  `gobby_session_id_from_env` remap — which otherwise updates external_id
  onto a STILL-LIVE predecessor row when SessionEnd(clear) has not landed
  yet — and the web-chat external_id reuse branch. The successor is always
  a NEW registered session row with an id distinct from the predecessor's,
  even while the predecessor row is still active.
- Resolution with a predecessor: create the new row, then in
  `handle_session_start` after registration perform the atomic take
  (`take_clear_handoff_marker`). Only the take winner proceeds:
  seed the successor (`seed_clear_handoff_variables`), reassign claims
  (section 2.2), and schedule the continuation prompt
  (`schedule_clear_self_continuation`). A losing take degrades to plain
  startup. The post-take side effects run in isolated failure domains: a
  seeding failure never skips claim transfer, and a claim-transfer failure
  never skips continuation scheduling — each is attempted for every
  winning take, logs loudly on failure, leaves the take consumed, and
  still yields a usable successor. Durable parentage does not depend on
  seeding: `take_clear_handoff_marker` writes parent_session_id inside the
  take transaction (1.1);
  any marker restoration is conditional on unchanged attempt state
  (`clear_failed_attempt` semantics). Carry the predecessor and attempt_id
  on `SessionStartResolution` (new optional fields, e.g. `clear_predecessor`,
  `clear_attempt_id`) so the flow can act after the row exists.
- Resolution without a predecessor (no marker, expired, mismatch, ambiguous,
  or any lookup exception): exactly today's behavior — independent row, no
  injection, no parent. Log the degrade_reason as a structured warning when a
  marker existed but was unusable.

The epoch injections (persona, wiki, skill-ledger reset) already treat clear as
full context loss and are not modified; the seeded handoff variables coexist
with them and are delivered at turn_start (section 2.3), ordered after the
session_start epoch injections.

**Acceptance:**

- 2.1.1 - The clear branch attempts marker resolution and carries the predecessor and attempt id through the resolution object. symbol: `resolve_session_start_identity`.
- 2.1.2 - For source=clear, predecessor resolution runs before every existing-row reuse path (inactive early returns, live pre-created-row reuse, `gobby_session_id_from_env` remap, web-chat external_id reuse), and the predecessor and successor have distinct session ids. symbol: `handle_session_start`.
- 2.1.3 - All successor side effects (seeding, claim transfer, continuation scheduling) are gated on a successful atomic take; two simultaneous SessionStarts produce exactly one bound successor. test: `tests/hooks/test_session_handoff_handlers.py`.
- 2.1.4 - Every unusable-marker path (missing, expired, identity mismatch, ambiguous, exception) yields today's independent-session behavior with no injection. symbol: `resolve_session_start_identity`.
- 2.1.5 - Successor binding is covered by hook-handler tests modeled on the compact handoff suite and the 0.4.x clear tests recoverable from git history. test: `tests/hooks/test_session_handoff_handlers.py`.
- 2.1.6 - A SessionStart(source=clear) that runs while the predecessor row is still active binds a distinct new row and never remaps onto the live predecessor. test: `tests/hooks/test_session_handoff_handlers.py`.
- 2.1.7 - A seeding failure after a winning take still transfers claims and preserves parentage: the successor is usable without injection and no claimed task remains on the expired predecessor. test: `tests/hooks/test_session_handoff_handlers.py`.

### 2.2 Task-claim reassignment [category: code] (depends: 2.1)
`kind: deliverable`

Targets:
- `src/gobby/hooks/event_handlers/_session_start/claims.py`
- `src/gobby/hooks/event_handlers/_session_start/flow.py::handle_session_start`
- `src/gobby/storage/tasks/_transitions.py::claim_task`
- `tests/hooks/test_session_start_claims.py`
- `tests/storage/tasks/test_claim_task_errors.py::*` — scope-reason: storage claim tests extended with expected-owner compare-and-swap coverage

Port the 0.4.x claim-transfer logic (removed in d2e273e6d9; recover with
`git show d2e273e6d9^:src/gobby/hooks/event_handlers/_session_start/handoff.py`)
into a new `claims.py` module in the `_session_start` package, hardened with
compare-and-swap ownership semantics:

- `preserve_task_claim_state(handler, sv_mgr, successor_id, predecessor_id, predecessor_vars)`:
  reads `task_claimed` / `claimed_tasks` / `session_had_task` from the
  predecessor's variables and merges the surviving claims into the successor
  — but only for transfers that actually committed.
- `filter_and_reassign_claimed_tasks(...)`: for each claimed task, transfer
  ownership with an expected-owner compare-and-swap instead of
  fetch-then-force: extend the storage-layer `claim_task` in
  `src/gobby/storage/tasks/_transitions.py` with an
  `expected_owner: str | None` parameter that claims only when the current
  effective claim owner still equals the predecessor at commit time (inside
  the transaction, per `get_effective_claim_owner`), with `force=False`. A
  task whose claim moved to a third session in the interim is skipped, never
  stolen back. Ownership transfer and the successor session-task link
  (relationship "claimed") commit together in one transaction, or the
  transfer is compensated (ownership rolled back) when linking fails —
  never a claim owned by the successor without its link, or vice versa.
  Per-task failures are logged and skipped — a partial transfer never
  aborts session start.

Called from `handle_session_start` only after a successful atomic take (2.1).
The stop-hook close gates then apply to the successor session, which is the
point: a mid-task `clear_self` hands the claim, the close checklist, and the
turn-hold to the continuation session instead of stranding them on an expired
row.

**Acceptance:**

- 2.2.1 - Claims held by the predecessor at clear time are owned by the successor after binding, including the session-task "claimed" link. file: `src/gobby/hooks/event_handlers/_session_start/claims.py`.
- 2.2.2 - Transfer uses expected-owner compare-and-swap inside the claim transaction: a claim concurrently moved to a third session is skipped, never overwritten. symbol: `claim_task`.
- 2.2.3 - Ownership and successor linkage commit atomically or compensate on link failure, and claim-state variables are merged only for committed transfers. symbol: `filter_and_reassign_claimed_tasks`.
- 2.2.4 - Per-task errors never abort session start; concurrency and link-failure paths are covered. test: `tests/hooks/test_session_start_claims.py`.

### 2.3 Rule templates: delete fossil, add clear-handoff injection [category: config] (depends: 2.1)
`kind: deliverable`

Targets:
- `src/gobby/install/shared/workflows/rules/context-handoff/inject-previous-session-summary.yaml::*` — scope-reason: fossil rule template deleted wholesale
- `src/gobby/install/shared/workflows/rules/context-handoff/inject-clear-handoff.yaml`
- `src/gobby/install/bundled_content_manifest.json::*` — scope-reason: manifest entries updated for the removed and added rule templates
- `src/gobby/workflows/engine/effects.py::EffectsMixin._apply_effect`
- `tests/hooks/test_session_handoff_handlers.py::*` — scope-reason: fossil session_start injection assertions replaced with orphan-pruning and clear-handoff coverage
- `tests/workflows/test_context_handoff_rules.py::*` — scope-reason: fossil-rule fixtures and inverse assertions replaced; new rule sync and one-shot assertions added
- `tests/workflows/test_context_handoff_fencing.py::*` — scope-reason: fencing assertions citing the deleted template retargeted to the new clear-handoff template

Delete `inject-previous-session-summary.yaml` (the fossil that renders an empty
"Previous Session Context" block on every `source == 'clear'` session start —
its variable feeders died with the 0.4.x removal). Add
`inject-clear-handoff.yaml` containing one rule:

```yaml
rules:
  inject-clear-handoff-on-prompt:
    description: "Deliver the clear_self handoff on the successor's first prompt"
    event: turn_start
    enabled: true
    priority: 11
    when: "variables.get('clear_handoff_inject_pending')"
    effects:
      - type: inject_context
        template: |
          <!-- gobby:injected-context:begin -->
          ## Continuation Context (deliberate clear)
          *Injected by Gobby session handoff*

          {{ handoff_summary_injectable or '' }}
          <!-- gobby:injected-context:end -->
      - type: set_variable
        variable: clear_handoff_inject_pending
        value: false
```

Modeled on `inject-compact-handoff-on-prompt` (same event, priority, and
one-shot pending-variable shape) but deliberately without the compact
template's durable-tool-call-evidence and skill-reload sections: the successor
is a fresh session whose mcp_calls ledger starts empty and whose skill rules
re-fire natively. Work context only.

Update `bundled_content_manifest.json` for the removed and added template
files, and fix the stale comment in the workflow engine's inject_context
branch that cites the deleted template as its fencing example. Confirm on a
synced install that the DB registry row for `inject-previous-session-summary`
is removed or disabled by the sync (templates are not live config; the
installed row is the source of truth) and the new rule's row is present and
enabled.

The existing test inventory asserts the behavior being removed and must be
updated in the same leaf: `tests/hooks/test_session_handoff_handlers.py`,
`tests/workflows/test_context_handoff_rules.py`, and
`tests/workflows/test_context_handoff_fencing.py` carry fossil-rule fixtures,
inverse assertions (empty "Previous Session Context" on clear), and fencing
examples citing the deleted template. Replace them with orphan-pruning
assertions (the fossil registry row is gone or disabled after sync), one-shot
turn_start injection assertions for the new rule, and fencing coverage on the
new template.

**Acceptance:**

- 2.3.1 - The fossil template is gone and the bundled-content manifest no longer references it. file: `src/gobby/install/bundled_content_manifest.json`.
- 2.3.2 - The new rule injects the handoff exactly once on the successor's first turn_start and clears its pending variable. file: `src/gobby/install/shared/workflows/rules/context-handoff/inject-clear-handoff.yaml`.
- 2.3.3 - After template sync, the installed registry rows reflect the deletion and the addition. test: `tests/workflows/test_context_handoff_rules.py`.
- 2.3.4 - The engine comment citing the deleted template is corrected. symbol: `EffectsMixin._apply_effect`.
- 2.3.5 - No test still asserts the fossil injection behavior: handler, rule, and fencing suites are updated to the new template. test: `tests/workflows/test_context_handoff_fencing.py`.

## P3: Web chat clear
`kind: framing`

**Goal**: `clear_self` works for live web-chat sessions across all six backends
via a restart-based backend clear, with lifecycle parity: the backend is
cleared first, then the old row ends with SessionEnd(reason='clear'), a new
row is created with parentage, claims transfer, and the handoff is delivered
on the successor's first prompt.

### 3.1 Backend clear_context interface [category: code] (depends: P1)
`kind: deliverable`

Targets:
- `src/gobby/servers/chat_session_base.py::ChatSessionProtocol`
- `src/gobby/servers/chat_session.py::ChatSession`
- `src/gobby/servers/websocket/chat/backends/base.py::ManagedChatSessionBase`
- `src/gobby/servers/websocket/chat/backends/codex.py::CodexManagedChatSession.clear_context`
- `tests/servers/websocket/chat/test_codex_clear_context.py::*` — scope-reason: existing Codex clear tests updated to the shared signature and contract
- `tests/servers/websocket/chat/test_clear_context_contract.py`

The web-chat registry is typed to `ChatSessionProtocol`, and the native
Claude `ChatSession` does not inherit `ManagedChatSessionBase` — so the
shared operation must live on the protocol, not only on the managed base.
Declare `async def clear_context(self) -> bool` on `ChatSessionProtocol` and
implement it for every backend the registry can hold:

- `ManagedChatSessionBase` gets a restart-based default: stop the backend
  session and start a fresh one, preserving the selected model and chat mode,
  and explicitly resetting per-provider continuation/resume state (thread,
  turn, transcript, resume identifiers) so a generic stop/start cannot
  silently resume the previous context. The Codex override keeps its
  thread-archive specialization aligned to the shared signature.
- The native Claude `ChatSession` (SDK-backed) implements clear_context by
  dropping its SDK continuation/resume identifiers and starting a fresh SDK
  session with the same model and mode.
- Audit the remaining backends (acp, droid, grok, qwen) against the restart
  default with per-provider freshness semantics: verify each one's stop/start
  cycle yields a genuinely fresh context (no resumed ACP session ids or
  provider continuation tokens) and re-applies model and mode, adding a thin
  override only where the default provably misbehaves. Delivering `/clear`
  as an in-band chat message is explicitly rejected as the mechanism — it is
  unverified per backend and Codex proves it cannot work everywhere.
- Add a cross-backend contract test suite that exercises clear_context for
  all six backends (claude native, codex, acp, droid, grok, qwen) and
  asserts: new backend session identity, no reused continuation/resume
  identifiers, model preserved, mode preserved.

**Acceptance:**

- 3.1.1 - `clear_context` is declared on the registry protocol and the managed base provides a restart-based default returning success/failure. symbol: `ChatSessionProtocol`.
- 3.1.2 - Codex retains thread-archive behavior under the shared signature. symbol: `CodexManagedChatSession.clear_context`.
- 3.1.3 - All six backends pass the fresh-context contract suite (new backend session, no reused continuation identifiers, model and mode preserved), with overrides only where the default fails. test: `tests/servers/websocket/chat/test_clear_context_contract.py`.
- 3.1.4 - The native Claude session resets its SDK continuation/resume identifiers while preserving model and mode. symbol: `ChatSession`.

### 3.2 Web-chat clear orchestration and row swap [category: code] (depends: 2.2, 2.3, 3.1)
`kind: deliverable`

Targets:
- `src/gobby/servers/websocket/chat/session_registry.py::WebChatSessionRegistry`
- `src/gobby/servers/websocket/chat/_session.py::ChatSessionMixin`
- `src/gobby/servers/websocket/chat/_session.py::ChatSessionMixin._create_chat_session_inner`
- `src/gobby/servers/websocket/chat/_session.py::ChatSessionMixin._fire_session_end`
- `src/gobby/servers/websocket/server.py::WebSocketServer.__init__`
- `tests/servers/websocket/chat/test_clear_session.py`
- `tests/servers/websocket/chat/test_session_registry.py::*` — scope-reason: registry gains clear orchestration, typed lifecycle hooks, and durable queue semantics
- `tests/servers/websocket/chat/test_session.py::*` — scope-reason: chat session mixin tests updated for the force-new successor path and typed lifecycle hooks

Add `WebChatSessionRegistry.clear_session(session_id, *, attempt_id,
continuation_prompt)`. The registry cannot reach the chat layer's DB
session-boundary operations by itself, so give it an explicit typed seam: a
small `ClearLifecycleHooks` protocol with a single commit operation
(`commit_clear_successor(...)`) implemented by `ChatSessionMixin` and bound
onto the registry in `WebSocketServer.__init__`, where the server owns both
the registry and the mixin. No hidden coupling, no reach-through attributes.
Commit is one hook on purpose: splitting predecessor termination and
successor creation into separately-invoked hooks reopens a crash window
between them that leaves a cleared backend bound to an expired row.

The clear is a prepare/clear/commit state machine — irreversible predecessor
termination happens only after the fallible backend clear succeeds:

1. **Prepare**: the staged clear-attempt record (written by 3.3: handoff via
   update_summary plus the `clear_attempt` marker carrying attempt_id,
   model, and mode) is the durable intent. The predecessor row is not
   terminalized and no successor exists yet. If the conversation has an
   active turn, stop here: the durable attempt record is the queue entry of
   record (the in-memory queue map holds only a pointer to it), and the
   caller receives `{queued: true, attempt_id}` — never final success. On
   turn end the registry executes the pending attempt from the durable
   record. On daemon restart or session unregister, a pending attempt is
   explicitly failed via `clear_failed_attempt` (compare-and-clear +
   summary restore) — never silently dropped. A second `clear_self` while
   an attempt is pending coalesces deterministically: it returns the
   pending attempt_id instead of overwriting.
2. **Clear**: call the backend's `clear_context()` (3.1) and require an
   acknowledged success result. On failure: the predecessor row is untouched
   and still live, the attempt is failed via `clear_failed_attempt`, and a
   structured error is returned — no half-cleared state.
3. **Commit**: call the single `commit_clear_successor` hook. Its database
   work is ONE hub transaction: expire the predecessor row (reason='clear'),
   insert the force-new successor row via the dedicated path in
   `_create_chat_session_inner` — a fresh runtime external identity that can
   never reuse the conversation's stable existing row and never starts a
   second backend — and persist parent_session_id plus the seeded
   clear-handoff variables (`handoff_summary_injectable` bounded,
   `clear_handoff_inject_pending`). A crash can therefore never leave an
   expired predecessor without a successor. Non-row SESSION_END side effects
   (`_fire_session_end` event fan-out) are emitted only after the
   transaction commits. If the commit transaction fails, the predecessor row
   is still live: the already-cleared backend keeps serving under the
   predecessor id and the attempt is failed via `clear_failed_attempt` —
   degrade, never wedge. After commit, transfer task claims with the shared
   helpers from section 2.2 (same expected-owner compare-and-swap contract)
   — after the successor row exists, before the continuation prompt. Then
   rebind the already-running live wrapper to the successor: db_session_id,
   message sequence number, and every live callback use the successor id;
   exactly one backend process remains.
4. **Continue**: send the continuation prompt as the successor's first
   message so the turn_start injection rule (2.3) fires and the agent
   resumes.

Failure containment after a successful backend clear: a commit-transaction
failure leaves the predecessor row live and bound to the cleared backend,
logs loudly, and fails the attempt via `clear_failed_attempt` — a usable
fresh-context chat without handoff (degrade, never wedge the chat). Web chat needs no terminal-identity
marker — the chat layer knows both rows at creation time; the attempt record
still gates one-shot consumption and failure cleanup.

**Acceptance:**

- 3.2.1 - `clear_session` orchestrates through typed lifecycle hooks bound at server construction; the registry never reaches into chat internals without the seam. symbol: `WebChatSessionRegistry`.
- 3.2.2 - The backend clear precedes predecessor termination: a failed `clear_context` leaves the predecessor live and untouched and fails the attempt. test: `tests/servers/websocket/chat/test_clear_session.py`.
- 3.2.3 - Commit is a single `commit_clear_successor` hook whose row work is one transaction: predecessor expiry, force-new successor insert, parentage, and seeded variables commit together; old and new ids differ and exactly one backend process remains. symbol: `ChatSessionMixin._create_chat_session_inner`.
- 3.2.4 - The live wrapper is rebound to the successor: sequence numbers and every live callback use the successor id. test: `tests/servers/websocket/chat/test_clear_session.py`.
- 3.2.5 - Task claims transfer on the web path via the shared 2.2 helpers after the successor row exists and before continuation, covering transferred claims, per-task failures, session-task links, and merged variables. test: `tests/servers/websocket/chat/test_clear_session.py`.
- 3.2.6 - A clear queued behind an active turn is durable and coalescing: restart/unregister explicitly fails the pending attempt, duplicate requests return the pending attempt_id, and a queued call never reports final success. test: `tests/servers/websocket/chat/test_clear_session.py`.
- 3.2.7 - A commit-transaction failure after a successful backend clear leaves the predecessor row live and serving the cleared backend, with the attempt failed. test: `tests/servers/websocket/chat/test_clear_session.py`.

### 3.3 clear_self web-chat branch [category: code] (depends: 3.2)
`kind: deliverable`

Targets:
- `src/gobby/mcp_proxy/tools/sessions/_terminal_webchat.py::*` — scope-reason: the module gains the clear fallback sibling alongside the existing compact fallback and shared lookup flow
- `src/gobby/mcp_proxy/tools/sessions/_terminal_clear.py`

Add `_clear_live_web_chat_fallback(web_chat_session_registry, handoff, *session_ids)`
mirroring `_compact_live_web_chat_fallback`: resolve the live session through
the registry, stage the attempt durably first (same staging path as terminal —
update_summary plus the `clear_attempt` marker, extended with the session's
current model and chat mode so a queued attempt can be executed later from
the durable record alone), then call `clear_session` with the attempt_id and
the built continuation prompt. Replace the "web chat not yet supported" stub
in the `clear_self` tool body with this delegation; the registry dependency
is already threaded into the terminal tool registration. A queued result
(`{queued: true, attempt_id}`) is propagated to the caller as queued — the
tool never reports final success for a deferred clear.

**Acceptance:**

- 3.3.1 - A live web-chat `clear_self` call clears the backend context and hands off through the registry path. file: `src/gobby/mcp_proxy/tools/sessions/_terminal_webchat.py`.
- 3.3.2 - The attempt (handoff, model, mode, attempt_id) is durably staged before the backend clear begins. file: `src/gobby/mcp_proxy/tools/sessions/_terminal_clear.py`.
- 3.3.3 - A clear deferred behind an active turn returns queued with the attempt id, never final success. file: `src/gobby/mcp_proxy/tools/sessions/_terminal_webchat.py`.

## P4: Contract documentation
`kind: framing`

**Goal**: The session-boundary contract — including the new deliberate-clear
exception — lives in a versioned repo doc, and the project memory that
currently carries it is amended to match.

### 4.1 Session-boundary contract doc and memory amendment [category: docs] (depends: P2, P3)
`kind: deliverable`

Targets:
- `docs/contracts/session-boundary.md`

Write `docs/contracts/session-boundary.md` documenting the full boundary
contract, today known only from project memory: compaction preserves the
session row (identity, variables, claims, parentage, agent-run ownership);
SessionEnd(reason='clear') expires the row; SessionStart(source='clear')
creates an independent session — EXCEPT when an unconsumed, in-TTL
`clear_attempt` marker with matching scoped identity exists, in which case
the new row binds to the predecessor via an atomic take (parent_session_id,
seeded handoff variables, compare-and-swap claim transfer) and receives the
handoff at first prompt; every unusable marker degrades to the
independent-session behavior; agent-run sessions are rejected by
`clear_self` in v1; web chat achieves parity through the
prepare/clear/commit row swap with backend `clear_context` and a durable
queued-attempt record. Document the attempt-record shape, the
single-marker-writer rule, the failure-restore semantics, and the successor
authorization exception on `get_handoff_context`.

As part of this deliverable, amend the project memory that documents the
boundary contract (memory_id 0d223c8f-3df1-5fd7-847a-4820ff8631b5 and its
compaction siblings) via gobby-memory so it states the clear_self exception and
points at the new doc as canonical.

**Acceptance:**

- 4.1.1 - The doc exists and states both the default boundary and the clear_self exception with its degrade rules, the agent-run rejection, and the web prepare/clear/commit contract. file: `docs/contracts/session-boundary.md`.
- 4.1.2 - The project memory carrying the boundary contract is updated to match and reference the doc. behavior: "memory amendment" in `docs/contracts/session-boundary.md`.

## Verification Strategy
`kind: framing`

End-to-end verification, in addition to per-leaf TDD evidence:

- Terminal round-trip on an isolated test daemon: agent session calls
  `clear_self(handoff=...)`; assert the attempt is staged (summary +
  marker, no handoff_ready status) before the tmux send; successor
  SessionStart(source='clear') binds via atomic take, inherits a claimed
  task, and its first turn_start injects the handoff exactly once; second
  turn injects nothing.
- Degrade matrix: no marker / consumed marker / TTL-expired marker / terminal
  mismatch / cross-project marker / cross-machine marker / two competing
  markers — each yields an independent session with no injection and no
  claim transfer.
- Concurrency matrix: two simultaneous SessionStart(source='clear') events —
  exactly one successor binds; claim moved to a third session mid-transfer —
  skipped, not stolen; seed failure after a successful take — successor
  usable, claims still transferred, parentage intact from the take
  transaction, restoration conditional on unchanged attempt state; successor
  starting while the predecessor row is still active — distinct new row,
  no live-row remap.
- Failure-restore: a failed clear attempt (storage, send, or backend
  failure) compare-clears the marker and restores the prior summary state;
  a subsequent compact or plain /clear consumes nothing from it.
- Agent-run rejection: `clear_self` from a session bound to an agent run
  returns a structured error and stages nothing.
- Oversized handoff: a bound successor dereferences the breadcrumb through
  `get_handoff_context` after the predecessor row is expired.
- Web chat round-trip per backend where an isolated backend is available
  (minimum: SDK and Codex): `clear_self` on a live chat session clears the
  backend first, ends the old row with reason='clear', creates a bound
  force-new successor with transferred claims, and the backend context is
  demonstrably fresh; queued-clear durability (restart/unregister fails the
  pending attempt; duplicates coalesce) and commit-transaction failure
  (predecessor stays live serving the cleared backend) are covered on the
  registry suite.
- Fossil check: a plain user `/clear` (no clear_self) produces no injected
  block at all — the empty "Previous Session Context" header is gone.
- Hook-handler tests live beside the compact suite; recover and adapt the
  deleted 0.4.x clear tests from git history for the binding and claim paths.

## V1 Plan Changelog
`kind: verification`

**Round 1** `kind: enhancement`

- enhancer_run: 5a4c9e91-f3ed-4add-87eb-a03f05378723
- enhancer_session: 551b1819-77e4-4512-92b6-7ddf0c5be349
- converged: true
- suggestions_presented: 0
- accepted:
  - (none)
- declined:
  - (none)
- resolution_notes: Enhancer reviewed evidence 8525d11f-035d-406c-a5af-30d4ba82b6c2
  (plan_hash 00a71a66fe0ba816653df1460b2b9dd18ee59e6296aac675df1f0796891741f7) and
  converged with zero suggestions; no plan changes.

**Round 2** `kind: verification`

- reviewer_run: 27886636-0ab0-4ad5-a12c-f7037ec8d24e
- reviewer_session: f6835ac2-d0e0-4b1e-aa84-5f0bd803970c
- verdict: needs_review
- findings:
- acceptance-artifact-ownership/blocking/three leaves used the future 4.1 contract doc as their acceptance oracle — accepted
- web-claim-continuity/blocking/web row-swap path skipped the 2.2 claim transfer — accepted
- phase-dependency-parity/blocking/3.2 and 4.1 missing dependency edges on consumed contracts — accepted
- agent-run-continuity/blocking/SessionEnd(clear) terminalization could destroy agent-run state — accepted (resolved by rejecting clear_self for agent-run sessions in v1)
- oversize-handoff-retrieval/blocking/successor could not dereference the breadcrumb after predecessor expiry — accepted
- clear-start-fast-path/blocking/inactive pre-created-row early returns bypassed the clear resolver — accepted
- clear-identity-scope/blocking/resolver lacked machine/project/source scoping and strong terminal-process identity — accepted
- stale-test-inventory/blocking/existing handoff/rule/fencing tests asserting the fossil behavior absent from Targets — accepted
- backend-interface-and-freshness/blocking/clear_context not on ChatSessionProtocol; native Claude and provider freshness unscoped — accepted
- registry-orchestration-seam/blocking/registry had no typed seam to chat session-boundary operations — accepted
- distinct-web-successor-row/blocking/no force-new successor row or live-wrapper rebinding — accepted
- web-clear-state-machine/blocking/predecessor terminalized before the fallible backend clear — accepted
- clear-attempt-state/blocking/handoff_ready staging leaked into compact discovery and failed attempts left stale state — accepted
- atomic-successor-binding/blocking/split resolve/register/consume raced concurrent SessionStarts — accepted
- claim-transfer-cas/blocking/fetch-then-force claim transfer had a TOCTOU window and split ownership/link writes — accepted
- durable-web-clear-queue/blocking/queued clear was volatile last-write-wins while its attempt state was durable — accepted
- resolution_notes: All 16 blocking findings accepted by the user (16-0). Repairs applied across every deliverable: acceptance oracles moved onto leaf-owned tests/symbols; agent-run sessions rejected in v1 (new constraint and non-goal); staging reworked to attempt-ID clear_attempt records with no handoff_ready status, single marker writer, and compare-and-clear restore; resolver scoped to machine/project/source with trusted-hint-or-terminal_process_contexts_match binding; successor binding made an atomic take gating all side effects; claim transfer hardened with expected_owner CAS on storage claim_task plus atomic-or-compensating linkage; clear_context promoted to ChatSessionProtocol with native Claude implementation and six-backend contract tests; web clear rebuilt as prepare/clear/commit with typed ClearLifecycleHooks bound at WebSocketServer construction, force-new successor row with live-wrapper rebinding, shared claim transfer, and a durable coalescing queued-attempt record; get_handoff_context authorizes bound-successor dereference after predecessor expiry; dependency edges corrected (3.2 depends 2.2+2.3+3.1; 4.1 depends P2+P3); stale test inventories added to Targets in 1.1, 2.1, 2.2, 2.3, 3.1, 3.2 after a whole-plan sweep; Verification Strategy extended with concurrency, failure-restore, rejection, oversized-handoff, and queued-clear durability matrices. Base validation (draft parse + semantic lint) passes on the revised artifact.

```json plan-review-round
{"evidence_id":"ea598f97-5ea1-4961-ac81-f243281f7aec","plan_hash":"d1c32b8d8980b6f41a69355d7a1166c9aaf0d8922f2ce84cdb23ac108e855ea7","round_number":1,"round_result":{"coverage_attestation":{"adjacent_variant_complete":true,"attestation_digest":"3f47c96764f82936328927cbf9bcb16f01a03ed008b0e48f24d472ac52cfa84b","cross_lane_interaction_complete":true,"disposition_counts":{"dismissed":10,"emitted_findings":16,"total":26},"evidence_id":"ea598f97-5ea1-4961-ac81-f243281f7aec","lanes":[{"candidate_count":6,"lane_id":"requirements_traceability","status":"completed"},{"candidate_count":9,"lane_id":"repository_blast_radius","status":"completed"},{"candidate_count":11,"lane_id":"runtime_invariants","status":"completed"}],"shadow_manifest_status":{"entry_count":9,"manifest_digest":"a7cb9688f581394bd67529fd8493d80132f36049a261ab7471cd35f4764b10b0","status":"valid"},"source_digest":"95aff65cd4cfede6cd508b56733c8cf4a36315d78f4d55768478ec7c2ae6b388","version":1},"findings":[{"category":"traceability","check_key":"traceability.acceptance-artifact-ownership","description":"Three implementation leaves use a future documentation artifact as their acceptance oracle, so their completion criteria cannot be evaluated when those leaves finish.","finding_id":"acceptance-artifact-ownership","fix":"Replace the early documentation references with direct symbol and test assertions owned by those leaves, and keep the documentation acceptance under 4.1; alternatively retarget and resequence the document so it is an explicit predecessor.","location":"Sections 1.2, 2.3, 3.1, and 4.1","prevention":"Trace every acceptance artifact to the same leaf's Targets or to an explicit acyclic predecessor dependency.","principle":"Every expanded leaf must own or depend on the artifact that proves its acceptance.","root_cause":"Items 1.2.3, 2.3.3, and 3.1.3 cite docs/contracts/session-boundary.md even though only later section 4.1 targets and writes that file.","section_id":"1.2","severity":"blocking"},{"category":"missing-requirement","check_key":"requirements.web-claim-continuity","description":"A web clear can create a successor without reassigning the predecessor's claimed tasks, leaving claims owned by an expired session.","finding_id":"web-claim-continuity","fix":"Add a shared claim-transfer step after the web successor row exists and before continuation, and add web tests for transferred claims, per-task failures, session-task links, and copied variables.","location":"Sections 2.2 and 3.2","prevention":"Trace each global continuity requirement through every terminal and managed-chat successor constructor.","principle":"Every supported successor path must preserve the same declared task-continuity contract.","root_cause":"The plan requires all successors to inherit predecessor task claims, but the web-chat row-swap path does not invoke or depend on the claim-transfer work in section 2.2.","section_id":"3.2","severity":"blocking"},{"category":"bad-sequencing","check_key":"dependencies.phase-parity","description":"The manifest permits web orchestration and documentation work to run before contracts they rely on are complete.","finding_id":"phase-dependency-parity","fix":"Make 3.2 depend explicitly on 2.2, 2.3, and 3.1, and make 4.1 depend on each relevant phase-2 and phase-3 deliverable; then verify the derived task edges.","location":"Sections 2.2, 2.3, 3.2, and 4.1","prevention":"Derive dependency edges from every cross-section contract reference and verify the expanded manifest preserves them.","principle":"Declared dependencies must include every earlier deliverable whose contract or artifact a leaf consumes.","root_cause":"Section 3.2 depends only on 3.1 while consuming the claim-transfer and injection contracts from 2.2 and 2.3; section 4.1 documents phase-2 behavior but depends only on phase 3.","section_id":"3.2","severity":"blocking"},{"category":"missing-requirement","check_key":"requirements.agent-run-continuity","description":"clear_self can destroy agent-run, workflow, or worktree state underneath the successor session.","finding_id":"agent-run-continuity","fix":"Either rebind agent-run, workflow-instance, and worktree ownership to the successor without terminalizing the logical run, or explicitly reject clear_self for agent-run sessions; cover both behavior and cleanup with tests.","location":"Sections 1.2 and 2.1","prevention":"Audit all SessionEnd side effects for continuation reasons and specify ownership transfer or a supported-session restriction.","principle":"A context clear that continues the same logical agent must preserve or explicitly reject every resource owned by that agent run.","root_cause":"The planned SessionEnd(reason=clear) path uses normal terminalization semantics that can finish the agent run, delete workflow instances, and release worktrees even though a successor session continues.","section_id":"2.1","severity":"blocking"},{"category":"unhandled-edge","check_key":"runtime.oversize-handoff-retrieval","description":"An oversized handoff can be replaced with a breadcrumb that the newly bound successor cannot dereference.","finding_id":"oversize-handoff-retrieval","fix":"Target _handoff.py::get_handoff_context and authorize a same-project bound child to read its direct predecessor's stored handoff; add an oversized-handoff test after predecessor expiration.","location":"Section 1.1","prevention":"Exercise reference-based fallback retrieval after predecessor expiration under the successor identity.","principle":"Every fallback reference emitted for oversized durable state must remain readable under the successor's authorization rules.","root_cause":"The planned breadcrumb points the successor to get_handoff_context on the predecessor, but the predecessor is expired and the getter normally requires handoff_ready except for self-access.","section_id":"1.1","severity":"blocking"},{"category":"bad-sequencing","check_key":"runtime.clear-start-fast-path","description":"The main same-process terminal clear path can silently reuse or retain the wrong row instead of binding a distinct successor.","finding_id":"clear-start-fast-path","fix":"Make source=clear bypass the inactive pre-created-row returns and run clear predecessor resolution first; test that the expired predecessor and successor have distinct IDs.","location":"Section 2.1","prevention":"Test the clear source against every existing SessionStart early return and pre-created-row branch.","principle":"Continuation-specific predecessor resolution must run before generic inactive-session fast paths that would bypass it.","root_cause":"The current SessionStart flow returns early for an inactive pre-created row before resolver logic, so a same-process clear carrying predecessor gobby_session_id never reaches the new clear resolver.","section_id":"2.1","severity":"blocking"},{"category":"unhandled-edge","check_key":"runtime.clear-identity-scope","description":"A stale or cross-scope terminal marker could bind the wrong predecessor after terminal identity reuse.","finding_id":"clear-identity-scope","fix":"Add explicit machine_id, project_id, and source filters and require either the exact trusted predecessor ID or terminal_process_contexts_match; test cross-project, cross-machine, expired, and reused-terminal cases.","location":"Section 1.1","prevention":"Require project, machine, source, expiry, and trusted terminal-process identity in resolver signatures and adversarial tests.","principle":"A one-shot predecessor resolver must scope matches to trusted identity dimensions and reject ambiguous reused terminal identities.","root_cause":"The proposed resolver API lacks explicit machine and project inputs despite the requirement, and terminal_context_matches_session is weaker than the pane, tty, and process identity needed for safe binding.","section_id":"1.1","severity":"blocking"},{"category":"weak-testability","check_key":"tests.stale-handoff-inventory","description":"The plan can implement the new turn_start behavior while leaving contradictory tests and stale rule fixtures behind.","finding_id":"stale-test-inventory","fix":"Add tests/hooks/test_session_handoff_handlers.py, tests/workflows/test_context_handoff_rules.py, and tests/workflows/test_context_handoff_fencing.py to Targets and replace the old inverse, fossil-rule, and fencing assertions with orphan-pruning and one-shot tests.","location":"Section 2.3","prevention":"Search for the replaced rule, action name, and inverse assertions and list every affected test file in Targets.","principle":"A behavior replacement must enumerate and update existing tests that assert the behavior being removed.","root_cause":"Existing handoff handler, workflow rule, and fencing tests explicitly assert the fossil session_start injection behavior, but those files are absent from the plan's Targets.","section_id":"2.3","severity":"blocking"},{"category":"missing-requirement","check_key":"requirements.backend-interface-freshness","description":"clear_context is not implementable or reliably fresh across all six managed backends as currently scoped.","finding_id":"backend-interface-and-freshness","fix":"Target ChatSessionProtocol and native Claude ChatSession, define per-provider reset of continuation and resume state while preserving model and mode, and add contract tests for all six backend implementations.","location":"Section 3.1","prevention":"Inventory the registry protocol plus every concrete backend before adding a shared lifecycle method.","principle":"A shared managed-backend operation must be defined on the actual registry interface and specify provider-specific freshness semantics for every implementation.","root_cause":"The registry is typed to ChatSessionProtocol, native Claude ChatSession does not inherit the proposed managed base, and generic stop/start can resume ACP or Claude continuation identifiers. The plan targets only the base and Codex implementation.","section_id":"3.1","severity":"blocking"},{"category":"bad-sequencing","check_key":"architecture.registry-orchestration-seam","description":"The proposed registry workflow cannot invoke the required database session-boundary operations without hidden coupling or unplanned constructor changes.","finding_id":"registry-orchestration-seam","fix":"Place orchestration on the chat host that owns both registry and session mixin, or inject typed prepare/commit callbacks into the registry and target the constructor wiring; test the queue through that seam.","location":"Section 3.2","prevention":"Trace each orchestration call to a concrete receiver or injected callback in the current object graph.","principle":"The component assigned orchestration must have an explicit typed seam to every lifecycle action it invokes.","root_cause":"The chat registry owns the proposed clear queue but has no access to the ChatSessionMixin session-end and successor-row methods the plan says it will call.","section_id":"3.2","severity":"blocking"},{"category":"unhandled-edge","check_key":"runtime.distinct-web-successor-row","description":"The web clear path can reuse the predecessor row or leak a second backend instead of producing one successor session.","finding_id":"distinct-web-successor-row","fix":"Add a dedicated force-new clear-successor constructor with a fresh runtime external identity, then rebind db_session_id, sequence number, and callbacks on the already-cleared live wrapper; test distinct IDs and no backend leak.","location":"Section 3.2","prevention":"Assert old and new database IDs differ, only one backend process remains, and every live callback uses the successor ID.","principle":"A logical clear boundary must create exactly one distinct successor identity and rebind the already-running transport to it.","root_cause":"Ordinary chat creation and registration reuse a conversation's stable existing row or can start another backend, while the plan does not define a force-new row path or complete live-wrapper rebinding.","section_id":"3.2","severity":"blocking"},{"category":"bad-sequencing","check_key":"runtime.web-clear-state-machine","description":"A backend clear failure can leave an already-expired predecessor, contrary to the plan's stated recovery behavior.","finding_id":"web-clear-state-machine","fix":"Specify a prepare/clear/commit state machine: stage the summary without terminalizing, clear the backend, then atomically expire the predecessor and create and seed the successor; require acknowledged results and define partial-failure recovery.","location":"Section 3.2","prevention":"Model prepare, backend action, commit, and recovery states and test each failure boundary.","principle":"Irreversible predecessor termination must occur only after fallible backend clearing succeeds, with explicit recovery for every partial state.","root_cause":"The plan fires SessionEnd(reason=clear) before clear_context even though clear failure is supposed to leave the old row live and untouched, and the current session-end helper is best-effort and suppresses failures.","section_id":"3.2","severity":"blocking"},{"category":"unhandled-edge","check_key":"runtime.clear-attempt-state","description":"A failed clear_self can make ordinary compact discovery or a later plain /clear consume stale handoff and predecessor state.","finding_id":"clear-attempt-state","fix":"Introduce clear-specific attempt-ID staged state excluded from compact discovery, designate a single marker writer immediately before /clear, and compare-clear the marker while restoring the prior handoff status on every failure.","location":"Sections 1.1 and 1.2","prevention":"Use one attempt owner, an attempt identifier, compare-and-clear cleanup, and tests for every failure and retry path.","principle":"A fallible clear attempt needs isolated staged state that cannot be consumed by unrelated compaction or later clear operations.","root_cause":"set_handoff_context marks handoff_ready before clear succeeds, the plan prewrites a terminal marker while also assigning marker ownership to sender callbacks, and failures can leave both stale status and marker state.","section_id":"1.2","severity":"blocking"},{"category":"unhandled-edge","check_key":"runtime.atomic-successor-binding","description":"Concurrent starts can both inherit the predecessor, or a seed failure can permanently consume the only marker without a valid successor.","finding_id":"atomic-successor-binding","fix":"Replace the split flow with an atomic take or attempt-ID lease transaction; only the winner may seed, transfer claims, or inject, and safe restoration must be conditional on unchanged attempt state.","location":"Sections 1.1 and 2.1","prevention":"Test two simultaneous SessionStarts plus failure after acquisition, and require all side effects to follow a successful atomic take.","principle":"A one-shot successor bind must atomically grant a single consumer before any seed, claim, or prompt side effect.","root_cause":"Resolve, register, and consume are separate operations, so concurrent SessionStarts can both register successors; the plan also does not gate downstream effects on consume success, and consuming before seeding can burn the one-shot on failure.","section_id":"2.1","severity":"blocking"},{"category":"unhandled-edge","check_key":"runtime.claim-transfer-cas","description":"Concurrent reassignment can be overwritten, and a partial transfer can leave ownership and session-task linkage inconsistent.","finding_id":"claim-transfer-cas","fix":"Add claim_task(expected_owner=predecessor_id, force=false), make ownership and successor linkage atomic or compensating, and copy variables only for successfully committed transfers; add concurrency and link-failure tests.","location":"Section 2.2","prevention":"Require expected-owner compare-and-swap semantics and concurrency tests covering ownership and link failures.","principle":"Ownership transfer must compare against the expected predecessor and commit ownership and linkage consistently.","root_cause":"The ported flow fetches the current owner and later calls claim_task(force=...), creating a time-of-check/time-of-use window that can steal a claim moved by another actor; task ownership and the session-task link are separate writes.","section_id":"2.2","severity":"blocking"},{"category":"unhandled-edge","check_key":"runtime.durable-web-clear-queue","description":"Restart, unregister, or a second request can lose or overwrite the queued clear while leaving stale durable attempt state, and the original caller cannot receive the later failure.","finding_id":"durable-web-clear-queue","fix":"Persist a clear-attempt queue record keyed by attempt ID with immutable handoff reference, model, and mode, or explicitly reject or deterministically coalesce concurrent requests with recovery; do not report final success for a volatile queue.","location":"Sections 3.2 and 3.3","prevention":"Test restart, unregister, duplicate requests, and delayed execution before reporting a deferred clear as accepted.","principle":"A deferred destructive action acknowledged to a caller must survive process lifecycle events or be explicitly rejected until it can execute synchronously.","root_cause":"A clear_self call during an active web tool turn must queue, but the proposed queue is a volatile last-write-wins dictionary while its handoff and session status are already durable.","section_id":"3.2","severity":"blocking"}],"reviewer_session":"f6835ac2-d0e0-4b1e-aa84-5f0bd803970c","round":1,"verdict":"needs_review"},"session_id":"0fa2de2c-98d7-433d-afac-be698cd14c01"}
```

**Round 3** `kind: verification`

- reviewer_run: 43cf6d2e-20a5-4d11-8cec-90ceca925834
- reviewer_session: b7f17542-9fc5-4471-84fe-14c2754b337e
- verdict: needs_review
- findings:
- live-precreated-reuse/blocking/source=clear could remap onto a still-live predecessor row via the external_id and gobby_session_id_from_env reuse paths — accepted
- post-take-claim-recovery/blocking/a seeding failure after the irreversible marker take stranded claims and parentage on the expired predecessor — accepted
- web-commit-atomicity/blocking/commit split across fire_session_end and create_clear_successor hooks left a crash window wedging a cleared backend on an expired row — accepted
- resolution_notes: All 3 blocking findings accepted unattended by the coordinator (3-0) after code verification of the live reuse paths in handle_session_start. Repairs: 2.1 ordering now skips every existing-row reuse path for source=clear (inactive early returns, live pre-created reuse, env-id remap, web-chat reuse) with new acceptance 2.1.6 covering the still-live-predecessor race; post-take side effects made isolated failure domains with parent_session_id moved into the take transaction (1.1 take/seed contracts, acceptance 1.1.3/1.1.4/2.1.7); 3.2 ClearLifecycleHooks reduced to a single commit_clear_successor hook whose row work (predecessor expiry, force-new successor insert, parentage, seeded variables) is one hub transaction with post-commit SESSION_END fan-out and predecessor-stays-live semantics on transaction failure (acceptance 3.2.3/3.2.7). Verification Strategy extended with the live-predecessor, claims-despite-seed-failure, and commit-failure matrices. Two adversary launch attempts for this round died to harness issues (native-tool enforcement kill; session ended mid-review) and were expired without counting; this finalized round reviewed evidence 4f393b75-c781-454f-a8af-412a7fa80928.

```json plan-review-round
{"evidence_id":"4f393b75-c781-454f-a8af-412a7fa80928","plan_hash":"0df79fbe9450de00f0ee81b3c53d9f8daf1e08c9a1a076d38bbbdba9da702f60","round_number":2,"round_result":{"coverage_attestation":{"adjacent_variant_complete":true,"attestation_digest":"4838b68155cdea3393d0d4bdec150bdb1534475b79db465099dacc450328a966","cross_lane_interaction_complete":true,"disposition_counts":{"dismissed":9,"emitted_findings":3,"total":12},"evidence_id":"4f393b75-c781-454f-a8af-412a7fa80928","lanes":[{"candidate_count":3,"lane_id":"requirements_traceability","status":"completed"},{"candidate_count":5,"lane_id":"repository_blast_radius","status":"completed"},{"candidate_count":4,"lane_id":"runtime_invariants","status":"completed"}],"shadow_manifest_status":{"entry_count":9,"manifest_digest":"ee38ecc0447cedc34c53f2f242bdc3605c95efb0f62733a20b3b713fc96dd5f7","status":"valid"},"source_digest":"ea9626e831d847ea5ac5bff5e3dc77d8d6f84b3c61078cdde506e14f667155ff","version":1},"findings":[{"category":"unhandled-edge","check_key":"runtime.live-precreated-reuse","description":"The successor can bind to the still-live predecessor instead of a distinct new row. After /clear, SessionStart often carries the predecessor gobby_session_id in terminal_context; if that row is not yet expired, the live branch updates external_id onto it and returns _handle_pre_created_session. That violates 2.1's distinct-id rule and is the live dual of the inactive fast-path finding from round 1.","finding_id":"live-precreated-reuse","fix":"For source=clear, skip every existing-row reuse path (inactive early return, live _handle_pre_created_session, and gobby_session_id_from_env remap). Always register a new session row, then take. Add a test where SessionStart(source=clear) runs while the predecessor is still active and assert distinct ids plus a successful take.","location":"Section 2.1 / handle_session_start","participating_section_ids":["2.1"],"prevention":"For source=clear, enumerate every existing-row lookup (inactive return, live pre-created, env-id remap, web-chat reuse) and require a new row before take.","principle":"A source=clear SessionStart must never reuse or remap onto the predecessor row, including while that row is still active.","root_cause":"2.1.2 only names the inactive pre-created-row early returns. handle_session_start still remaps a live predecessor found via gobby_session_id_from_env into _handle_pre_created_session, so adding clear to the resume/compact exception set still reuses the predecessor when SessionEnd has not landed.","section_id":"2.1","severity":"blocking"},{"category":"unhandled-edge","check_key":"runtime.post-take-claim-recovery","description":"A mid-task clear_self exists so claims are not stranded on an expired row. If seed_clear_handoff_variables fails after take_clear_handoff_marker succeeds, the plan stops before filter_and_reassign_claimed_tasks, restoration cannot un-consume the marker, and the predecessor is already gone from /clear. Constraints require successor ownership with no opt-out.","finding_id":"post-take-claim-recovery","fix":"Commit claim transfer (and parent_session_id) in the same transaction as the take, or always run the 2.2 helpers after a winning take even when seed fails. Test seed-failure-after-take: successor may lack injection, but claimed tasks must not remain on the expired predecessor.","location":"Sections 2.1 and 2.2","prevention":"Put take, parent_session_id, and claim transfer in one transaction, or run 2.2 on every winning take before any fallible seed/schedule work.","principle":"After a winning one-shot take, claim transfer must complete even if later seed or prompt steps fail.","root_cause":"2.1 sequences take, then seed, then 2.2 claims. clear_failed_attempt is a no-op once the marker is consumed. Verification names seed-failure-after-take as successor-usable degrade but never recovers claims from the already-expired predecessor.","section_id":"2.1","severity":"blocking"},{"category":"unhandled-edge","check_key":"runtime.web-commit-atomicity","description":"Prepare/clear correctly delay terminalization until backend clear succeeds. Commit then splits irreversible SessionEnd from successor creation across two hooks the registry invokes separately. That is the remaining half of the round-1 state-machine finding: failure after clear_context but before a successor row leaves a cleared backend bound to an expired id, which is a wedged chat rather than the specified degrade.","finding_id":"web-commit-atomicity","fix":"Replace fire_session_end plus create_clear_successor with one commit_clear_successor hook that expires the predecessor and inserts the force-new successor in a single transaction, then rebinds db_session_id, sequence, and callbacks. If that transaction fails, the predecessor row must still be live so the already-cleared backend can keep serving without handoff.","location":"Section 3.2 / ClearLifecycleHooks","prevention":"Model commit as a single hook whose DB work is one transaction, then rebind the live wrapper outside that transaction.","principle":"Predecessor expiry and successor insert must be one database transaction after a successful backend clear.","root_cause":"3.2.3 claims an atomic commit, but ClearLifecycleHooks is two methods (fire_session_end and create_clear_successor). A crash between them expires the predecessor with no successor after clear_context has already wiped the live backend. clear_failed_attempt cannot un-expire a row.","section_id":"3.2","severity":"blocking"}],"round_number":2,"verdict":"needs_review"},"session_id":"c685ee35-4aad-4e0a-a50f-88bf6e82d840"}
```

**Round 4** `kind: verification`

- reviewer_run: 5be4c069-75e7-40c7-ba92-6bc7f4555e0d
- reviewer_session: e2fbfc91-6f8b-4476-98fb-88c7b5748d74
- verdict: approved
- findings:
- none — zero emitted findings; all three coverage lanes completed (requirements_traceability 2, repository_blast_radius 4, runtime_invariants 5 candidates) with all 11 internal candidates dismissed; cross-lane interaction and adjacent-variant analysis complete; shadow manifest valid with 9 entries
- resolution_notes: No repairs required. Adversary round 3 approved the plan as revised after round 2 (still-live predecessor reuse paths, take-owned parentage with isolated post-take failure domains, single-hook single-transaction web commit). Approval manifest applied via apply_plan_review_manifest: 9 M1 entries, manifest_digest e284eaeaea8f931bb9ed23d996ba92417205e603326f4f6ee4170c3d489c2c85. Convergence reached at 3 finalized adversary rounds of the 12-round cap.

```json plan-review-round
{"evidence_id":"0cb7873b-ebc4-479d-a229-8d6be9079323","plan_hash":"9f1512a13166c7baad7fd506f20ba8ea9351726600f9a9426ccec90a402493bc","round_number":3,"round_result":{"coverage_attestation":{"adjacent_variant_complete":true,"attestation_digest":"a5aa7001c89a51aad46c0fd10fb7b05637b1b571f71a85adee670c3cd67ace8f","cross_lane_interaction_complete":true,"disposition_counts":{"dismissed":11,"emitted_findings":0,"total":11},"evidence_id":"0cb7873b-ebc4-479d-a229-8d6be9079323","lanes":[{"candidate_count":2,"lane_id":"requirements_traceability","status":"completed"},{"candidate_count":4,"lane_id":"repository_blast_radius","status":"completed"},{"candidate_count":5,"lane_id":"runtime_invariants","status":"completed"}],"shadow_manifest_status":{"entry_count":9,"manifest_digest":"2001f7f873017e1b121a0f27534b71e8f9b4ee9abbf9e10809ea5b1f1d5ddbc4","status":"valid"},"source_digest":"50e9263ad859790be1aa202056256700b24922f61c114bcc675ff3c68cc3085e","version":1},"findings":[],"manifest_entries":[{"category":"code","depends_on":[],"implementation_domain":"backend","labels":["covers:clear-self-handoff:1.1:1.1.1","covers:clear-self-handoff:1.1:1.1.2","covers:clear-self-handoff:1.1:1.1.3","covers:clear-self-handoff:1.1:1.1.4","covers:clear-self-handoff:1.1:1.1.5","covers:clear-self-handoff:1.1:1.1.6"],"source_section":"1.1","task_type":"feature","tdd":true,"title":"Clear continuation module","validation_criteria":"1.1.1: Module exists with attempt staging, scoped resolution, atomic take, conditional cleanup, seeding, and prompt builders. file: `src/gobby/sessions/clear_continuation.py`.\n1.1.2: Resolution binds only within machine_id + project_id + source scope on an unconsumed, in-TTL marker, and requires the trusted predecessor hint or `terminal_process_contexts_match`; cross-project, cross-machine, expired, reused-terminal, and ambiguous cases degrade with a reason. symbol: `resolve_clear_continuation`.\n1.1.3: The marker take is a single-transaction compare-and-swap that also writes the successor's parent_session_id: under concurrent takers exactly one wins and losers degrade. symbol: `take_clear_handoff_marker`.\n1.1.4: Seeding writes bounded `handoff_summary_injectable` and `clear_handoff_inject_pending` on the successor; parentage is owned by the take transaction. symbol: `seed_clear_handoff_variables`.\n1.1.5: Failed-attempt cleanup compare-clears the marker and restores the prior summary state only when the attempt is unchanged and unconsumed. symbol: `clear_failed_attempt`.\n1.1.6: A bound successor can dereference its direct predecessor's stored handoff after the predecessor row is expired (oversized-handoff breadcrumb path). test: `tests/sessions/test_clear_continuation.py`."},{"category":"code","depends_on":["1.1"],"implementation_domain":"backend","labels":["covers:clear-self-handoff:1.2:1.2.1","covers:clear-self-handoff:1.2:1.2.2","covers:clear-self-handoff:1.2:1.2.3","covers:clear-self-handoff:1.2:1.2.4","covers:clear-self-handoff:1.2:1.2.5"],"source_section":"1.2","task_type":"feature","tdd":true,"title":"clear_self terminal tool","validation_criteria":"1.2.1: `clear_self` is registered on gobby-sessions and requires a non-empty handoff. symbol: `register_terminal_tools`. file: `src/gobby/mcp_proxy/tools/sessions/_terminal_clear.py`.\n1.2.2: Handoff staging and marker write both complete before `/clear` is sent; a storage failure aborts without terminal interaction. file: `src/gobby/mcp_proxy/tools/sessions/_terminal_clear.py`.\n1.2.3: Terminal delivery reuses the compaction sender with command `/clear`, including Codex interrupt handling. test: `tests/mcp_proxy/tools/sessions/test_terminal_clear.py`.\n1.2.4: An agent-run session calling `clear_self` receives a structured rejection and no state is staged. test: `tests/mcp_proxy/tools/sessions/test_terminal_clear.py`.\n1.2.5: A failure after staging compare-clears the attempt and restores the prior summary state. test: `tests/mcp_proxy/tools/sessions/test_terminal_clear.py`."},{"category":"code","depends_on":["1.1","1.2"],"implementation_domain":"backend","labels":["covers:clear-self-handoff:2.1:2.1.1","covers:clear-self-handoff:2.1:2.1.2","covers:clear-self-handoff:2.1:2.1.3","covers:clear-self-handoff:2.1:2.1.4","covers:clear-self-handoff:2.1:2.1.5","covers:clear-self-handoff:2.1:2.1.6","covers:clear-self-handoff:2.1:2.1.7"],"source_section":"2.1","task_type":"feature","tdd":true,"title":"SessionStart clear binding and variable seeding","validation_criteria":"2.1.1: The clear branch attempts marker resolution and carries the predecessor and attempt id through the resolution object. symbol: `resolve_session_start_identity`.\n2.1.2: For source=clear, predecessor resolution runs before every existing-row reuse path (inactive early returns, live pre-created-row reuse, `gobby_session_id_from_env` remap, web-chat external_id reuse), and the predecessor and successor have distinct session ids. symbol: `handle_session_start`.\n2.1.3: All successor side effects (seeding, claim transfer, continuation scheduling) are gated on a successful atomic take; two simultaneous SessionStarts produce exactly one bound successor. test: `tests/hooks/test_session_handoff_handlers.py`.\n2.1.4: Every unusable-marker path (missing, expired, identity mismatch, ambiguous, exception) yields today's independent-session behavior with no injection. symbol: `resolve_session_start_identity`.\n2.1.5: Successor binding is covered by hook-handler tests modeled on the compact handoff suite and the 0.4.x clear tests recoverable from git history. test: `tests/hooks/test_session_handoff_handlers.py`.\n2.1.6: A SessionStart(source=clear) that runs while the predecessor row is still active binds a distinct new row and never remaps onto the live predecessor. test: `tests/hooks/test_session_handoff_handlers.py`.\n2.1.7: A seeding failure after a winning take still transfers claims and preserves parentage: the successor is usable without injection and no claimed task remains on the expired predecessor. test: `tests/hooks/test_session_handoff_handlers.py`."},{"category":"code","depends_on":["2.1"],"implementation_domain":"backend","labels":["covers:clear-self-handoff:2.2:2.2.1","covers:clear-self-handoff:2.2:2.2.2","covers:clear-self-handoff:2.2:2.2.3","covers:clear-self-handoff:2.2:2.2.4"],"source_section":"2.2","task_type":"feature","tdd":true,"title":"Task-claim reassignment","validation_criteria":"2.2.1: Claims held by the predecessor at clear time are owned by the successor after binding, including the session-task \"claimed\" link. file: `src/gobby/hooks/event_handlers/_session_start/claims.py`.\n2.2.2: Transfer uses expected-owner compare-and-swap inside the claim transaction: a claim concurrently moved to a third session is skipped, never overwritten. symbol: `claim_task`.\n2.2.3: Ownership and successor linkage commit atomically or compensate on link failure, and claim-state variables are merged only for committed transfers. symbol: `filter_and_reassign_claimed_tasks`.\n2.2.4: Per-task errors never abort session start; concurrency and link-failure paths are covered. test: `tests/hooks/test_session_start_claims.py`."},{"assigned_agent":"backend-developer","category":"config","depends_on":["2.1"],"labels":["covers:clear-self-handoff:2.3:2.3.1","covers:clear-self-handoff:2.3:2.3.2","covers:clear-self-handoff:2.3:2.3.3","covers:clear-self-handoff:2.3:2.3.4","covers:clear-self-handoff:2.3:2.3.5"],"source_section":"2.3","task_type":"feature","tdd":true,"title":"Rule templates: delete fossil, add clear-handoff injection","validation_criteria":"2.3.1: The fossil template is gone and the bundled-content manifest no longer references it. file: `src/gobby/install/bundled_content_manifest.json`.\n2.3.2: The new rule injects the handoff exactly once on the successor's first turn_start and clears its pending variable. file: `src/gobby/install/shared/workflows/rules/context-handoff/inject-clear-handoff.yaml`.\n2.3.3: After template sync, the installed registry rows reflect the deletion and the addition. test: `tests/workflows/test_context_handoff_rules.py`.\n2.3.4: The engine comment citing the deleted template is corrected. symbol: `EffectsMixin._apply_effect`.\n2.3.5: No test still asserts the fossil injection behavior: handler, rule, and fencing suites are updated to the new template. test: `tests/workflows/test_context_handoff_fencing.py`."},{"category":"code","depends_on":["1.1","1.2"],"implementation_domain":"backend","labels":["covers:clear-self-handoff:3.1:3.1.1","covers:clear-self-handoff:3.1:3.1.2","covers:clear-self-handoff:3.1:3.1.3","covers:clear-self-handoff:3.1:3.1.4"],"source_section":"3.1","task_type":"feature","tdd":true,"title":"Backend clear_context interface","validation_criteria":"3.1.1: `clear_context` is declared on the registry protocol and the managed base provides a restart-based default returning success/failure. symbol: `ChatSessionProtocol`.\n3.1.2: Codex retains thread-archive behavior under the shared signature. symbol: `CodexManagedChatSession.clear_context`.\n3.1.3: All six backends pass the fresh-context contract suite (new backend session, no reused continuation identifiers, model and mode preserved), with overrides only where the default fails. test: `tests/servers/websocket/chat/test_clear_context_contract.py`.\n3.1.4: The native Claude session resets its SDK continuation/resume identifiers while preserving model and mode. symbol: `ChatSession`."},{"category":"code","depends_on":["2.2","2.3","3.1"],"implementation_domain":"backend","labels":["covers:clear-self-handoff:3.2:3.2.1","covers:clear-self-handoff:3.2:3.2.2","covers:clear-self-handoff:3.2:3.2.3","covers:clear-self-handoff:3.2:3.2.4","covers:clear-self-handoff:3.2:3.2.5","covers:clear-self-handoff:3.2:3.2.6","covers:clear-self-handoff:3.2:3.2.7"],"source_section":"3.2","task_type":"feature","tdd":true,"title":"Web-chat clear orchestration and row swap","validation_criteria":"3.2.1: `clear_session` orchestrates through typed lifecycle hooks bound at server construction; the registry never reaches into chat internals without the seam. symbol: `WebChatSessionRegistry`.\n3.2.2: The backend clear precedes predecessor termination: a failed `clear_context` leaves the predecessor live and untouched and fails the attempt. test: `tests/servers/websocket/chat/test_clear_session.py`.\n3.2.3: Commit is a single `commit_clear_successor` hook whose row work is one transaction: predecessor expiry, force-new successor insert, parentage, and seeded variables commit together; old and new ids differ and exactly one backend process remains. symbol: `ChatSessionMixin._create_chat_session_inner`.\n3.2.4: The live wrapper is rebound to the successor: sequence numbers and every live callback use the successor id. test: `tests/servers/websocket/chat/test_clear_session.py`.\n3.2.5: Task claims transfer on the web path via the shared 2.2 helpers after the successor row exists and before continuation, covering transferred claims, per-task failures, session-task links, and merged variables. test: `tests/servers/websocket/chat/test_clear_session.py`.\n3.2.6: A clear queued behind an active turn is durable and coalescing: restart/unregister explicitly fails the pending attempt, duplicate requests return the pending attempt_id, and a queued call never reports final success. test: `tests/servers/websocket/chat/test_clear_session.py`.\n3.2.7: A commit-transaction failure after a successful backend clear leaves the predecessor row live and serving the cleared backend, with the attempt failed. test: `tests/servers/websocket/chat/test_clear_session.py`."},{"category":"code","depends_on":["3.2"],"implementation_domain":"backend","labels":["covers:clear-self-handoff:3.3:3.3.1","covers:clear-self-handoff:3.3:3.3.2","covers:clear-self-handoff:3.3:3.3.3"],"source_section":"3.3","task_type":"feature","tdd":true,"title":"clear_self web-chat branch","validation_criteria":"3.3.1: A live web-chat `clear_self` call clears the backend context and hands off through the registry path. file: `src/gobby/mcp_proxy/tools/sessions/_terminal_webchat.py`.\n3.3.2: The attempt (handoff, model, mode, attempt_id) is durably staged before the backend clear begins. file: `src/gobby/mcp_proxy/tools/sessions/_terminal_clear.py`.\n3.3.3: A clear deferred behind an active turn returns queued with the attempt id, never final success. file: `src/gobby/mcp_proxy/tools/sessions/_terminal_webchat.py`."},{"assigned_agent":"tech-writer","category":"docs","depends_on":["2.1","2.2","2.3","3.1","3.2","3.3"],"labels":["covers:clear-self-handoff:4.1:4.1.1","covers:clear-self-handoff:4.1:4.1.2"],"source_section":"4.1","task_type":"feature","tdd":false,"title":"Session-boundary contract doc and memory amendment","validation_criteria":"4.1.1: The doc exists and states both the default boundary and the clear_self exception with its degrade rules, the agent-run rejection, and the web prepare/clear/commit contract. file: `docs/contracts/session-boundary.md`.\n4.1.2: The project memory carrying the boundary contract is updated to match and reference the doc. behavior: \"memory amendment\" in `docs/contracts/session-boundary.md`."}],"round_number":3,"routing_decisions":{"1.1":{"category":"code","implementation_domain":"backend","tdd":true},"1.2":{"category":"code","implementation_domain":"backend","tdd":true},"2.1":{"category":"code","implementation_domain":"backend","tdd":true},"2.2":{"category":"code","implementation_domain":"backend","tdd":true},"2.3":{"category":"config","tdd":true},"3.1":{"category":"code","implementation_domain":"backend","tdd":true},"3.2":{"category":"code","implementation_domain":"backend","tdd":true},"3.3":{"category":"code","implementation_domain":"backend","tdd":true},"4.1":{"category":"docs","tdd":false}},"verdict":"approved"},"session_id":"c685ee35-4aad-4e0a-a50f-88bf6e82d840"}
```

## M1 Task Manifest
`kind: manifest`

```yaml
- title: Clear continuation module
  category: code
  task_type: feature
  depends_on: []
  validation_criteria: '1.1.1: Module exists with attempt staging, scoped resolution,
    atomic take, conditional cleanup, seeding, and prompt builders. file: `src/gobby/sessions/clear_continuation.py`.

    1.1.2: Resolution binds only within machine_id + project_id + source scope on
    an unconsumed, in-TTL marker, and requires the trusted predecessor hint or `terminal_process_contexts_match`;
    cross-project, cross-machine, expired, reused-terminal, and ambiguous cases degrade
    with a reason. symbol: `resolve_clear_continuation`.

    1.1.3: The marker take is a single-transaction compare-and-swap that also writes
    the successor''s parent_session_id: under concurrent takers exactly one wins and
    losers degrade. symbol: `take_clear_handoff_marker`.

    1.1.4: Seeding writes bounded `handoff_summary_injectable` and `clear_handoff_inject_pending`
    on the successor; parentage is owned by the take transaction. symbol: `seed_clear_handoff_variables`.

    1.1.5: Failed-attempt cleanup compare-clears the marker and restores the prior
    summary state only when the attempt is unchanged and unconsumed. symbol: `clear_failed_attempt`.

    1.1.6: A bound successor can dereference its direct predecessor''s stored handoff
    after the predecessor row is expired (oversized-handoff breadcrumb path). test:
    `tests/sessions/test_clear_continuation.py`.'
  labels:
  - covers:clear-self-handoff:1.1:1.1.1
  - covers:clear-self-handoff:1.1:1.1.2
  - covers:clear-self-handoff:1.1:1.1.3
  - covers:clear-self-handoff:1.1:1.1.4
  - covers:clear-self-handoff:1.1:1.1.5
  - covers:clear-self-handoff:1.1:1.1.6
  tdd: true
  source_section: '1.1'
  implementation_domain: backend
- title: clear_self terminal tool
  category: code
  task_type: feature
  depends_on:
  - '1.1'
  validation_criteria: '1.2.1: `clear_self` is registered on gobby-sessions and requires
    a non-empty handoff. symbol: `register_terminal_tools`. file: `src/gobby/mcp_proxy/tools/sessions/_terminal_clear.py`.

    1.2.2: Handoff staging and marker write both complete before `/clear` is sent;
    a storage failure aborts without terminal interaction. file: `src/gobby/mcp_proxy/tools/sessions/_terminal_clear.py`.

    1.2.3: Terminal delivery reuses the compaction sender with command `/clear`, including
    Codex interrupt handling. test: `tests/mcp_proxy/tools/sessions/test_terminal_clear.py`.

    1.2.4: An agent-run session calling `clear_self` receives a structured rejection
    and no state is staged. test: `tests/mcp_proxy/tools/sessions/test_terminal_clear.py`.

    1.2.5: A failure after staging compare-clears the attempt and restores the prior
    summary state. test: `tests/mcp_proxy/tools/sessions/test_terminal_clear.py`.'
  labels:
  - covers:clear-self-handoff:1.2:1.2.1
  - covers:clear-self-handoff:1.2:1.2.2
  - covers:clear-self-handoff:1.2:1.2.3
  - covers:clear-self-handoff:1.2:1.2.4
  - covers:clear-self-handoff:1.2:1.2.5
  tdd: true
  source_section: '1.2'
  implementation_domain: backend
- title: SessionStart clear binding and variable seeding
  category: code
  task_type: feature
  depends_on:
  - '1.1'
  - '1.2'
  validation_criteria: '2.1.1: The clear branch attempts marker resolution and carries
    the predecessor and attempt id through the resolution object. symbol: `resolve_session_start_identity`.

    2.1.2: For source=clear, predecessor resolution runs before every existing-row
    reuse path (inactive early returns, live pre-created-row reuse, `gobby_session_id_from_env`
    remap, web-chat external_id reuse), and the predecessor and successor have distinct
    session ids. symbol: `handle_session_start`.

    2.1.3: All successor side effects (seeding, claim transfer, continuation scheduling)
    are gated on a successful atomic take; two simultaneous SessionStarts produce
    exactly one bound successor. test: `tests/hooks/test_session_handoff_handlers.py`.

    2.1.4: Every unusable-marker path (missing, expired, identity mismatch, ambiguous,
    exception) yields today''s independent-session behavior with no injection. symbol:
    `resolve_session_start_identity`.

    2.1.5: Successor binding is covered by hook-handler tests modeled on the compact
    handoff suite and the 0.4.x clear tests recoverable from git history. test: `tests/hooks/test_session_handoff_handlers.py`.

    2.1.6: A SessionStart(source=clear) that runs while the predecessor row is still
    active binds a distinct new row and never remaps onto the live predecessor. test:
    `tests/hooks/test_session_handoff_handlers.py`.

    2.1.7: A seeding failure after a winning take still transfers claims and preserves
    parentage: the successor is usable without injection and no claimed task remains
    on the expired predecessor. test: `tests/hooks/test_session_handoff_handlers.py`.'
  labels:
  - covers:clear-self-handoff:2.1:2.1.1
  - covers:clear-self-handoff:2.1:2.1.2
  - covers:clear-self-handoff:2.1:2.1.3
  - covers:clear-self-handoff:2.1:2.1.4
  - covers:clear-self-handoff:2.1:2.1.5
  - covers:clear-self-handoff:2.1:2.1.6
  - covers:clear-self-handoff:2.1:2.1.7
  tdd: true
  source_section: '2.1'
  implementation_domain: backend
- title: Task-claim reassignment
  category: code
  task_type: feature
  depends_on:
  - '2.1'
  validation_criteria: '2.2.1: Claims held by the predecessor at clear time are owned
    by the successor after binding, including the session-task "claimed" link. file:
    `src/gobby/hooks/event_handlers/_session_start/claims.py`.

    2.2.2: Transfer uses expected-owner compare-and-swap inside the claim transaction:
    a claim concurrently moved to a third session is skipped, never overwritten. symbol:
    `claim_task`.

    2.2.3: Ownership and successor linkage commit atomically or compensate on link
    failure, and claim-state variables are merged only for committed transfers. symbol:
    `filter_and_reassign_claimed_tasks`.

    2.2.4: Per-task errors never abort session start; concurrency and link-failure
    paths are covered. test: `tests/hooks/test_session_start_claims.py`.'
  labels:
  - covers:clear-self-handoff:2.2:2.2.1
  - covers:clear-self-handoff:2.2:2.2.2
  - covers:clear-self-handoff:2.2:2.2.3
  - covers:clear-self-handoff:2.2:2.2.4
  tdd: true
  source_section: '2.2'
  implementation_domain: backend
- title: 'Rule templates: delete fossil, add clear-handoff injection'
  category: config
  task_type: feature
  depends_on:
  - '2.1'
  validation_criteria: '2.3.1: The fossil template is gone and the bundled-content
    manifest no longer references it. file: `src/gobby/install/bundled_content_manifest.json`.

    2.3.2: The new rule injects the handoff exactly once on the successor''s first
    turn_start and clears its pending variable. file: `src/gobby/install/shared/workflows/rules/context-handoff/inject-clear-handoff.yaml`.

    2.3.3: After template sync, the installed registry rows reflect the deletion and
    the addition. test: `tests/workflows/test_context_handoff_rules.py`.

    2.3.4: The engine comment citing the deleted template is corrected. symbol: `EffectsMixin._apply_effect`.

    2.3.5: No test still asserts the fossil injection behavior: handler, rule, and
    fencing suites are updated to the new template. test: `tests/workflows/test_context_handoff_fencing.py`.'
  labels:
  - covers:clear-self-handoff:2.3:2.3.1
  - covers:clear-self-handoff:2.3:2.3.2
  - covers:clear-self-handoff:2.3:2.3.3
  - covers:clear-self-handoff:2.3:2.3.4
  - covers:clear-self-handoff:2.3:2.3.5
  tdd: true
  source_section: '2.3'
  assigned_agent: backend-developer
- title: Backend clear_context interface
  category: code
  task_type: feature
  depends_on:
  - '1.1'
  - '1.2'
  validation_criteria: '3.1.1: `clear_context` is declared on the registry protocol
    and the managed base provides a restart-based default returning success/failure.
    symbol: `ChatSessionProtocol`.

    3.1.2: Codex retains thread-archive behavior under the shared signature. symbol:
    `CodexManagedChatSession.clear_context`.

    3.1.3: All six backends pass the fresh-context contract suite (new backend session,
    no reused continuation identifiers, model and mode preserved), with overrides
    only where the default fails. test: `tests/servers/websocket/chat/test_clear_context_contract.py`.

    3.1.4: The native Claude session resets its SDK continuation/resume identifiers
    while preserving model and mode. symbol: `ChatSession`.'
  labels:
  - covers:clear-self-handoff:3.1:3.1.1
  - covers:clear-self-handoff:3.1:3.1.2
  - covers:clear-self-handoff:3.1:3.1.3
  - covers:clear-self-handoff:3.1:3.1.4
  tdd: true
  source_section: '3.1'
  implementation_domain: backend
- title: Web-chat clear orchestration and row swap
  category: code
  task_type: feature
  depends_on:
  - '2.2'
  - '2.3'
  - '3.1'
  validation_criteria: '3.2.1: `clear_session` orchestrates through typed lifecycle
    hooks bound at server construction; the registry never reaches into chat internals
    without the seam. symbol: `WebChatSessionRegistry`.

    3.2.2: The backend clear precedes predecessor termination: a failed `clear_context`
    leaves the predecessor live and untouched and fails the attempt. test: `tests/servers/websocket/chat/test_clear_session.py`.

    3.2.3: Commit is a single `commit_clear_successor` hook whose row work is one
    transaction: predecessor expiry, force-new successor insert, parentage, and seeded
    variables commit together; old and new ids differ and exactly one backend process
    remains. symbol: `ChatSessionMixin._create_chat_session_inner`.

    3.2.4: The live wrapper is rebound to the successor: sequence numbers and every
    live callback use the successor id. test: `tests/servers/websocket/chat/test_clear_session.py`.

    3.2.5: Task claims transfer on the web path via the shared 2.2 helpers after the
    successor row exists and before continuation, covering transferred claims, per-task
    failures, session-task links, and merged variables. test: `tests/servers/websocket/chat/test_clear_session.py`.

    3.2.6: A clear queued behind an active turn is durable and coalescing: restart/unregister
    explicitly fails the pending attempt, duplicate requests return the pending attempt_id,
    and a queued call never reports final success. test: `tests/servers/websocket/chat/test_clear_session.py`.

    3.2.7: A commit-transaction failure after a successful backend clear leaves the
    predecessor row live and serving the cleared backend, with the attempt failed.
    test: `tests/servers/websocket/chat/test_clear_session.py`.'
  labels:
  - covers:clear-self-handoff:3.2:3.2.1
  - covers:clear-self-handoff:3.2:3.2.2
  - covers:clear-self-handoff:3.2:3.2.3
  - covers:clear-self-handoff:3.2:3.2.4
  - covers:clear-self-handoff:3.2:3.2.5
  - covers:clear-self-handoff:3.2:3.2.6
  - covers:clear-self-handoff:3.2:3.2.7
  tdd: true
  source_section: '3.2'
  implementation_domain: backend
- title: clear_self web-chat branch
  category: code
  task_type: feature
  depends_on:
  - '3.2'
  validation_criteria: '3.3.1: A live web-chat `clear_self` call clears the backend
    context and hands off through the registry path. file: `src/gobby/mcp_proxy/tools/sessions/_terminal_webchat.py`.

    3.3.2: The attempt (handoff, model, mode, attempt_id) is durably staged before
    the backend clear begins. file: `src/gobby/mcp_proxy/tools/sessions/_terminal_clear.py`.

    3.3.3: A clear deferred behind an active turn returns queued with the attempt
    id, never final success. file: `src/gobby/mcp_proxy/tools/sessions/_terminal_webchat.py`.'
  labels:
  - covers:clear-self-handoff:3.3:3.3.1
  - covers:clear-self-handoff:3.3:3.3.2
  - covers:clear-self-handoff:3.3:3.3.3
  tdd: true
  source_section: '3.3'
  implementation_domain: backend
- title: Session-boundary contract doc and memory amendment
  category: docs
  task_type: feature
  depends_on:
  - '2.1'
  - '2.2'
  - '2.3'
  - '3.1'
  - '3.2'
  - '3.3'
  validation_criteria: '4.1.1: The doc exists and states both the default boundary
    and the clear_self exception with its degrade rules, the agent-run rejection,
    and the web prepare/clear/commit contract. file: `docs/contracts/session-boundary.md`.

    4.1.2: The project memory carrying the boundary contract is updated to match and
    reference the doc. behavior: "memory amendment" in `docs/contracts/session-boundary.md`.'
  labels:
  - covers:clear-self-handoff:4.1:4.1.1
  - covers:clear-self-handoff:4.1:4.1.2
  tdd: false
  source_section: '4.1'
  assigned_agent: tech-writer
```
