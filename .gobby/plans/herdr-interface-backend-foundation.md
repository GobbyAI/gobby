# Herdr-Style Interface Backend Foundation

**Plan ID:** herdr-interface-backend-foundation

## O1: Overview

`kind: framing`

Research on `ogulcancelik/herdr` (terminal agent multiplexer, ~18k stars) identified the backend capabilities Gobby needs so a herdr-style "every agent at a glance" interface — a `gobby attach` TUI as a follow-up, and a richer web agent grid — can be built without backend rework. Herdr's interface contract, distilled: a client connects, gets a roster of every agent with live status (working / blocked / done / idle), streams status transitions and pane output, attaches to any pane, and answers blocked prompts.

Gobby 0.5.0 already has the tmux agent layer (`src/gobby/agents/tmux/`), a WS event bus with subscription filtering (`src/gobby/servers/websocket/broadcast.py`), a WS tmux bridge with per-run `terminal_output` streaming (`src/gobby/servers/websocket/tmux.py`, `src/gobby/runner_broadcasting.py`), session lifecycle statuses (`active`/`paused`/`handoff_ready`/`expired`/`completed`), and screen-buffer detection (`prompt_detector.py`, `idle_detector.py`, `stall_classifier.py`). What is missing: a published needs-attention state (detection today only feeds internal auto-dismissal), a structured prompt payload with a safe respond API, a single roster query, detection rules as updatable data, a wait-on-output coordination primitive, and expiring transient status metadata. This plan closes those gaps, plus one independent research outcome (GitHub-topic skill hub).

## C1: Constraints

`kind: framing`

- herdr is AGPL-3.0. Ideas only — no code, manifest files, or rule text copied from the herdr repo. All detection rules are authored clean-room against Gobby's own captured pane fixtures.
- The TUI client itself (`gobby attach`, new Rust crate) is a non-goal here; it is the follow-up consumer of P1–P2. The web agent grid beyond the minimal surfacing in 1.4 is likewise out of scope.
- tmux remains the terminal backend. No native VT/multiplexer engine.
- Session lifecycle statuses (`active`/`paused`/`handoff_ready`/`expired`/`completed`) are load-bearing for hook flows and handoff resolution; attention is an orthogonal dimension, never a new lifecycle status value, and `SessionStatus` unions in CLI/web stay lifecycle-only.
- No backward compatibility shims — 0.5.0 is unshipped.
- Database work uses the hub Postgres transaction boundary and psycopg `%s` placeholders per CLAUDE.md. Schema changes are numbered migration files under `src/gobby/storage/migrations/`; table creation never lives in runtime manager modules. Migration numbers in this plan (326/327) were renumbered 2026-07-21 after the originally planned 324/325 were taken on disk; re-check the next free number at implementation time and renumber again if concurrent work has landed migrations.
- Existing auto-dismissal behavior (trust prompts, loop prompts, queued continuation prompts) is preserved unchanged for spawned runs; `blocked` is only for prompts Gobby declines to auto-answer. On interactive (human-owned) panes, detection is report-only — Gobby never auto-answers there.
- Regexes evaluated on the daemon path (detection manifests from the DB, `wait_for_output` patterns) are data-controlled input and must run under bounded size and execution time.
- New HTTP surface lives in a dedicated `src/gobby/servers/routes/attention.py` module so `agents.py` and other existing modules stay below the 1,000-line source limit.

## P1: Attention State and Prompt Surface

`kind: framing`

**Goal**: Detection results become published, actionable state: a `blocked` attention signal with the prompt itself attached, one roster query, and a guarded respond path — for spawned agent runs and interactive tmux-backed sessions alike.

### 1.1 Persist and broadcast needs-attention episodes [category: code]

`kind: deliverable`

Targets: `src/gobby/storage/attention.py`, `src/gobby/storage/migrations/326_attention_states.sql`, `src/gobby/agents/idle_check_handler.py`, `src/gobby/agents/tmux/pane_monitor.py`, `src/gobby/servers/websocket/broadcast.py`

**Entry identity.** Every attention-capable surface has a stable `entry_id`: `run:<run_id>` for spawned agent runs, `session:<session_id>` for interactive tmux-backed sessions (the roster in 1.3 uses the same ids). This is what lets the follow-up TUI act on interactive sessions — "your desktop session is blocked while you're away" — not just spawned runs.

**Episode identity.** A fingerprint identifies prompt *content*; it does not identify an *episode* (the same approval prompt can recur after clearing). New storage module `src/gobby/storage/attention.py` (model + manager only) with schema in migration `src/gobby/storage/migrations/326_attention_states.sql`:

```sql
CREATE TABLE attention_states (
    entry_id TEXT PRIMARY KEY,            -- "run:<uuid>" | "session:<uuid>"; one active episode per entry
    attention_id TEXT NOT NULL,           -- opaque per-episode id; new on each None->blocked transition or fingerprint change
    state TEXT,                           -- NULL | 'blocked'
    reason TEXT,                          -- 'approval' | 'trust' | 'question' | 'stall'
    fingerprint TEXT,                     -- sha256 of the detected prompt (reuses PromptDetector fingerprinting)
    payload JSONB,                        -- structured prompt payload (1.2)
    since TIMESTAMPTZ,
    seen_at TIMESTAMPTZ                   -- reset on every episode boundary
);
```

The `entry_id` primary key is the CAS anchor: all mutations — detection sets blocked, injection clears, terminal status clears, respond/seen endpoints — go through **one conditional transition function** on the manager. It executes each transition as a single `UPDATE ... WHERE entry_id = %s AND attention_id = %s AND fingerprint = %s`-style conditional write inside the hub transaction boundary, so races between the monitor loop and API callers resolve deterministically; stale mutations affect zero rows and report failure.

**Ordering coordinator.** The transition function also owns broadcast ordering (contract details in 1.3): it holds the daemon's attention ordering lock across commit, sequence assignment, and event enqueue, so a later `seq` can never carry older state.

**Episode kinds.** An episode is `actionable` (a concrete prompt awaits an answer: reason `approval` / `trust` / `question`) or `non_actionable` (reason `stall` — the surface is stuck, nothing is asking). The kind derives from `reason`, is carried on events and roster entries, and gates the respond path (1.2): stalls surface for awareness, never for answering.

**Detection sources.** Spawned runs: `IdleCheckHandler` (`src/gobby/agents/idle_check_handler.py`) opens a blocked episode when `PromptDetector` finds a prompt it will not auto-answer (approval prompts outside auto-dismiss policy, dismissal escalation exhausted) or `StallClassifier` reports a sustained `PROVIDER_STALL`; it clears when a later capture no longer matches the fingerprint, after injection, or at terminal status. Interactive sessions: extend the tmux pane monitoring path (`src/gobby/agents/tmux/pane_monitor.py`) to evaluate the same detectors against interactive-session panes resolved from stored terminal context — report-only, auto-dismiss disabled (C1). The pane monitor's detectors come from the same shared DB-backed registry owned by the runner composition root and handed to the pane monitor at its lifespan construction (2.2's production composition), never bare construction.

**Publication.** Every transition broadcasts an agent event (`BroadcastMixin.broadcast_agent_event`) carrying `{epoch, seq, entry_id, run_id|null, session_id, attention_id, state, reason, kind, fingerprint, payload, since, seen_at}` — `epoch`/`seq` come from the 1.3 ordering coordinator, and the structured prompt payload (1.2) rides on every blocked/changed event so a client can construct a valid response from the event alone, without a roster refetch. On each `None -> blocked` transition, send one communications-channel notification through the existing communications router (`src/gobby/communications/`), deduplicated by `attention_id` (a genuinely recurring prompt is a new episode and notifies again; a re-captured unchanged prompt is the same episode and does not).

**Acceptance:**

- 1.1.1 - Migration creates `attention_states` with the one-active-episode-per-entry primary key; an isolated migration/startup test applies it cleanly. file: `src/gobby/storage/migrations/326_attention_states.sql`.
- 1.1.2 - The storage manager exposes a single conditional transition function; all attention mutations route through it and stale mutations (wrong `attention_id` or fingerprint) affect zero rows. file: `src/gobby/storage/attention.py`.
- 1.1.3 - `IdleCheckHandler` opens blocked episodes for non-auto-answered prompts and sustained provider stalls, and clears them on fingerprint mismatch, injection, or terminal status. file: `src/gobby/agents/idle_check_handler.py`.
- 1.1.4 - Interactive tmux-backed sessions get report-only attention detection with auto-dismiss disabled. file: `src/gobby/agents/tmux/pane_monitor.py`.
- 1.1.5 - Attention transitions emit agent events on the WS bus with entry and episode identity. test: `tests/agents/test_attention_state.py::test_blocked_transition_broadcasts_agent_event`.
- 1.1.6 - Channel notifications dedupe by `attention_id`: an unchanged episode never re-notifies, an identical prompt recurring after a clear notifies as a new episode. test: `tests/agents/test_attention_state.py::test_notification_dedupe_by_episode`.
- 1.1.7 - Stale mutations lose the race against the transition function under concurrent monitor/API access. test: `tests/agents/test_attention_state.py::test_stale_request_races`.

### 1.2 Structured prompt payload and respond API [category: code] (depends: 1.1)

`kind: deliverable`

Targets: `src/gobby/agents/prompt_detector.py`, `src/gobby/servers/routes/attention.py`, `src/gobby/servers/routes/__init__.py`, `src/gobby/servers/_app_routes.py`, `src/gobby/agents/tmux/text_injection.py`

Extend `PromptDetector` to return a structured payload alongside detection: prompt kind, a bounded excerpt (last N lines of the matched region), detected options when the prompt is an enumerated choice (e.g. "1. Yes / 2. No"), and the existing sha256 fingerprint. The payload is stored on the 1.1 episode row.

Add a respond endpoint in the new `src/gobby/servers/routes/attention.py` module, keyed by entry:

```http
POST /api/attention/{entry_id}/respond
{"attention_id": "<id>", "fingerprint": "<sha256>", "answer": {"option": 1}}   # or {"text": "..."} or {"key": "escape"}
```

**Request contract.** `answer` carries exactly one variant; requests with zero or multiple variants are 422. `option` must be a member of the episode's detected options (422 otherwise). `text` is a literal (no control characters except newline) capped at 2,048 bytes. `key` comes from a fixed allowlist: `enter`, `escape`, `tab`, `up`, `down`. Unknown `entry_id` is 404. A respond aimed at a `non_actionable` episode (reason `stall`, 1.1) is 409 `not_actionable` — stalls are surfaced, not answerable; nothing is injected.

**Route wiring.** The module's router is exported from `src/gobby/servers/routes/__init__.py` and registered — with its service dependencies composed where the other routers get theirs — in the central route registry `src/gobby/servers/_app_routes.py`. An endpoint that exists but is unreachable through the real app is a wiring bug, pinned by the 1.3 app-level test.

**Injection actions (exact).** Executed via `src/gobby/agents/tmux/text_injection.py`:

- `option`: the option number's literal digit keys, then Enter (implicit Enter).
- `text`: the literal bytes as a paste-safe literal send, then Enter (implicit Enter).
- `key`: exactly the named tmux key (`enter`→`Enter`, `escape`→`Escape`, `tab`→`Tab`, `up`→`Up`, `down`→`Down`), with **no** implicit Enter.

**Injection protocol.** Server-side injections are serialized per entry (an asyncio lock keyed by `entry_id`). Inside that critical section the handler: re-captures the pane immediately adjacent to the send, recomputes the fingerprint, and injects only when the stored episode (`attention_id` + fingerprint), the recomputed fingerprint, and the request all agree. Mismatch against the stored episode is 409 `stale_episode` (current identity returned); recapture mismatch is 409 `prompt_changed`. Successful injection clears the episode through the 1.1 transition function.

**Failure handling (partial submission is reachable, so no blanket retry-safe promise).** `option` and `text` are two-step sends (payload, then Enter). If the *first* send fails before any byte lands (pane gone, tmux error), the episode is untouched and the response is 502 `injection_failed` with `stage: "none"` — genuinely retry-safe. If a later step fails after an earlier step succeeded (paste landed, Enter failed), the pane has changed while DB identity has not; the response is 502 `injection_indeterminate` with `stage: "partial"`, and the handler forces an episode boundary through the 1.1 transition function (episode cleared, `attention_id` retired) followed by an immediate re-detection pass — if the prompt still stands, a fresh episode with a new `attention_id` opens and notifies. Clients receiving `injection_indeterminate` refetch or wait for events; they never blind-retry.

**Race contract (attainable, stated honestly).** The CAS guarantees hold against all Gobby-mediated mutations: no stale client can answer a prompt that Gobby has observed changing. A human typing directly into the pane concurrently can still race the capture→send window; that window is minimized (capture adjacent to send, per-entry serialization) but is not zero and is documented as out of contract.

**Acceptance:**

- 1.2.1 - `PromptDetector` returns kind, excerpt, options, and fingerprint as a structured payload persisted on the episode. file: `src/gobby/agents/prompt_detector.py`.
- 1.2.2 - Respond validates exactly-one answer variant, option membership, text bounds, and the key allowlist; unknown entry is 404; a `non_actionable` (stall) episode is 409 `not_actionable` with nothing injected. file: `src/gobby/servers/routes/attention.py`.
- 1.2.3 - Respond injects only on episode + recapture + request agreement under per-entry serialization; returns 409 `stale_episode` / 409 `prompt_changed` with current identity on mismatch; each answer variant produces exactly its specified key/byte sequence and implicit-Enter behavior. test: `tests/servers/test_attention_respond.py::test_respond_cas_and_recurrence`.
- 1.2.4 - Prompt replacement between validation and send yields `prompt_changed` with no injection; a first-send failure returns `injection_failed` (`stage: none`) with the episode untouched; a paste-success/Enter-failure returns `injection_indeterminate` (`stage: partial`), retires the episode, and re-detection opens a fresh episode when the prompt persists. test: `tests/servers/test_attention_respond.py::test_partial_injection_and_stall_paths`.
- 1.2.5 - The attention router is exported and registered in the central route registry with composed service dependencies. file: `src/gobby/servers/_app_routes.py`.
- 1.2.6 - A client can answer an enumerated prompt using only the payload carried on the blocked WS event (no roster fetch). test: `tests/servers/test_attention_respond.py::test_event_driven_option_response`.

### 1.3 Attention roster endpoint [category: code] (depends: 1.1, 1.2)

`kind: deliverable`

Targets: `src/gobby/servers/routes/attention.py`, `src/gobby/servers/websocket/broadcast.py`, `src/gobby/sessions/tmux_context.py`

The roster query. The client handshake is subscribe-first, everywhere it is described: subscribe to WS events, buffer attention events, *then* fetch this roster and reconcile (full contract below) — the roster is never fetched before subscribing:

```http
GET /api/attention/roster
{
  "epoch": "b1f4...",        # broadcaster epoch UUID (new per daemon start)
  "seq": 18211024,           # monotonic ordering cursor within the epoch
  "entries": [
    {
      "entry_id": "run:...",             # or "session:..."
      "run_id": "...",                   # null for interactive entries
      "session_id": "...",
      "lifecycle_status": "active",
      "attention": {"attention_id": "...", "state": "blocked", "reason": "approval", "payload": {...}, "since": "...", "seen_at": null},
      "task": {"id": "...", "ref": "#18512", "stage": "development"},
      "provider": "claude", "model": "...",
      "tmux": {"socket_path": "...", "session_name": "...", "pane_pid": 12345},
      "last_activity_at": "..."
    }
  ]
}
```

Assemble from existing data: `agent_runs` (status, task/stage, provider, `tmux_session_name`), session lifecycle status, terminal/tmux context (`src/gobby/sessions/tmux_context.py` socket resolution), and the 1.1/1.2 attention rows. The tmux block is what lets a future native TUI run `tmux -S <socket> attach -t <name>` directly, while web clients keep the WS bridge.

**Ordering contract (explicit).** `BroadcastMixin` gains a broadcaster **epoch UUID** (regenerated on daemon start) and an in-process monotonic sequence counter. The 1.1 transition function is the single ordering coordinator: it holds one asyncio ordering lock across **state commit, seq assignment, and event enqueue**, so commit order, seq order, and enqueue order are identical by construction — two committed transitions can never enqueue in reverse order, and a later `seq` can never carry older state. Roster snapshots are atomic with their cursor: the handler acquires the same ordering lock and, inside the critical section, reads the current `seq`, the complete committed attention-state snapshot (one SELECT), **and** an immutable snapshot of the transient metadata store (3.2) — metadata `set()` events take seq under this same coordinator, so metadata is cursor-bounded state, never post-release enrichment; only order-independent enrichment (task/stage, provider, tmux coordinates, last activity) is assembled after release, and the serializer never re-reads attention or metadata state once the lock is released. The roster therefore can never contain attention or transient-metadata state newer than its cursor. Every attention event carries `epoch` and `seq` (1.1 Publication). The client handshake is subscribe-first: subscribe, buffer attention events, fetch the roster, discard buffered events with the roster's epoch and `seq <=` the roster's, apply the rest in order, and treat an epoch change as "refetch the roster." DB `updated_at` maxima are explicitly not the cursor — they share no ordering boundary with WS delivery.

Marking seen: `POST /api/attention/{entry_id}/seen` with `{"attention_id": "<id>"}` stamps `seen_at` for that episode (the herdr done-vs-idle bit) through the 1.1 transition function and broadcasts the update; a stale `attention_id` is a 409, so a seen aimed at a cleared episode can never mark its successor.

**Acceptance:**

- 1.3.1 - Roster returns entry/episode identity, lifecycle status, attention, task/stage, provider, tmux coordinates, and last activity for every live run and tmux-backed interactive session. file: `src/gobby/servers/routes/attention.py`.
- 1.3.2 - Commit, seq, and enqueue are atomic under the ordering coordinator: a deterministic interleaving test forces transition A to commit, B to commit and enqueue, then A to enqueue, and proves the observable stream cannot regress; a second forced interleaving commits a transition during roster construction and proves the roster's cursor still bounds its state; a third forced interleaving performs two metadata `set()`s during roster construction and proves the cursor bounds metadata too — replaying buffered events over the roster never regresses a newer chip to an older one; epoch change signals roster refetch. test: `tests/servers/test_attention_roster.py::test_ordering_coordinator_no_regression`.
- 1.3.3 - Seen endpoint stamps `seen_at` per episode, broadcasts, and rejects stale episode ids. test: `tests/servers/test_attention_roster.py::test_mark_seen_episode`.
- 1.3.4 - Interactive-pane end-to-end: a prompt on an interactive session's pane surfaces in the roster, responds via CAS, and stamps seen. test: `tests/servers/test_attention_roster.py::test_interactive_entry_end_to_end`.
- 1.3.5 - The real FastAPI app, composed through the central route registry, reaches respond, roster, and seen — implemented-but-unwired endpoints fail this test. test: `tests/servers/test_attention_routes_wiring.py::test_app_reaches_attention_endpoints`.

### 1.4 Surface attention in CLI and web activity [category: code] (depends: 1.3)

`kind: deliverable`

Targets: `src/gobby/cli/sessions.py`, `web/src/components/activity/sessionsFilters.ts`, `web/src/components/activity/SessionsFilterDropdown.tsx`, `web/src/components/activity/SessionsTab.tsx`, `web/src/components/activity/SessionsTab.helpers.tsx`, `web/src/hooks/useAgentRuns.ts`, `.impeccable.md`

Attention stays out of the lifecycle unions (C1): the web `SessionStatus` type in `web/src/components/activity/sessionsFilters.ts` keeps lifecycle values only, and a session row may aggregate multiple roster entries. Both surfaces derive a **blocked count per session** from roster entries and live agent events, clearing only when the last blocked entry for that session clears.

CLI: `gobby sessions` gains an attention column (glyph + reason, count when > 1) sourced from roster data; the lifecycle status icon map at `src/gobby/cli/sessions.py:159` is untouched — the attention glyph is a separate column. Web: the filter UI (`SessionsFilterDropdown.tsx`), row badges and composition (`SessionsTab.helpers.tsx`, `SessionsTab.tsx`), and the agent-event hook (`web/src/hooks/useAgentRuns.ts`) gain an independent blocked-attention filter and count badge beside the lifecycle filters, driven by the 1.1 agent events. The event hook implements the 1.3 handshake, not fetch-then-subscribe: subscribe first, buffer attention events, fetch the roster, discard buffered events at or below the roster cursor (same epoch), apply the rest in order, and refetch on epoch change. Read `.impeccable.md` before choosing badge styling; the blocked badge must be deutan-safe and not rely on color alone.

**Acceptance:**

- 1.4.1 - `gobby sessions` renders a separate attention column with per-session blocked count; lifecycle icons unchanged. file: `src/gobby/cli/sessions.py`.
- 1.4.2 - Web adds an independent blocked filter and count badge across the filter dropdown, row badges, and event hook; `SessionStatus` remains lifecycle-only. file: `web/src/components/activity/SessionsFilterDropdown.tsx`.
- 1.4.3 - Frontend test: a session with two runs, one blocked, shows blocked with count, updates live from agent events, and clears only when the last blocked entry clears. test: `web/src/__tests__/sessions-attention.test.tsx`.
- 1.4.4 - The hook reconciles subscribe-first: an attention transition delivered during initial subscription, before the roster response lands, is neither lost nor applied out of order, and an epoch change triggers refetch. test: `web/src/__tests__/attention-reconciliation.test.tsx`.

## P2: Detection Manifests as Data

`kind: framing`

**Goal**: Detection rules become versioned per-provider data synced to the DB, so a CLI UI change ships as a manifest update, not a code release. Detection quality is the ceiling on interface quality.

### 2.1 Manifest schema and compiled matcher [category: code]

`kind: deliverable`

Targets: `src/gobby/agents/detection/schema.py`, `src/gobby/agents/detection/matcher.py`, `src/gobby/agents/detection/safe_regex.py`, `pyproject.toml` (new package `src/gobby/agents/detection/`)

Define a TOML manifest schema and loader:

```toml
id = "claude"            # provider id
version = "1"            # dotted-numeric, monotonic per provider
engine = 1               # schema engine version; loader rejects newer engines

[[rules]]
id = "approval_prompt"
state = "blocked"        # blocked | idle | working | stall
reason = "approval"      # feeds attention reason for blocked rules
priority = 900           # highest matching priority wins
region = "bottom_non_empty_lines(8)"   # whole_recent | bottom_non_empty_lines(N) | prompt_box
contains = ["do you want to proceed?"]
line_regex = ['^\s*❯?\s*1\.\s*yes\b']
not = [{ contains = ["esc to interrupt"] }]
```

`src/gobby/agents/detection/schema.py` (pydantic models, engine/version validation), `src/gobby/agents/detection/matcher.py` (compile rules once per manifest **content fingerprint** — sha256 of the manifest content, so an edit that changes rules without bumping `version` still recompiles; evaluate against a captured pane snapshot; return highest-priority match with rule id and state). Region set covers only what the current detectors need — `whole_recent`, `bottom_non_empty_lines(N)`, `prompt_box` — not herdr's full region vocabulary. Rules are authored clean-room from Gobby's own pane fixtures in `tests/agents/fixtures/`.

**Bounded regex execution.** Manifest patterns come from the DB (and, later, a remote catalog) — data-controlled input. Add the timeout-capable `regex` package as a direct dependency (`pyproject.toml`) and one small helper, `src/gobby/agents/detection/safe_regex.py`, enforcing fixed pattern-size and execution-time limits. This deliverable owns `safe_regex.py`; 3.1 imports it read-only. The matcher compiles and searches only through this helper; a pattern that fails to compile or exceeds its time budget yields a controlled `invalid_pattern` / `pattern_timeout` outcome for that rule (rule skipped, manifest flagged), never an unbounded search on the event loop.

**Acceptance:**

- 2.1.1 - Manifest schema validates id, version, engine, and rule shapes, rejecting unknown regions and newer engines. file: `src/gobby/agents/detection/schema.py`.
- 2.1.2 - Matcher evaluates prioritized rules with contains/line_regex/not combinators over region-scoped pane text. file: `src/gobby/agents/detection/matcher.py`.
- 2.1.3 - Matcher behavior is pinned by fixture-driven tests per region and combinator. test: `tests/agents/detection/test_matcher.py::test_priority_and_regions`.
- 2.1.4 - Bounded regex helper enforces size and execution-time limits; a pathological pattern yields `pattern_timeout` without stalling evaluation. test: `tests/agents/detection/test_safe_regex.py::test_pathological_pattern_bounded`.

### 2.2 Port detectors to manifest-driven rules [category: refactor] (depends: 2.3)

`kind: deliverable`

Targets: `src/gobby/agents/prompt_detector.py`, `src/gobby/agents/idle_detector.py`, `src/gobby/agents/stall_classifier.py`, `src/gobby/agents/idle_check_handler.py`, `src/gobby/agents/lifecycle_monitor.py`, `src/gobby/runner_init/orchestration.py`, `src/gobby/runner_init/servers.py`, `src/gobby/runner.py`, `src/gobby/app_context.py`, `src/gobby/servers/http.py`, `src/gobby/servers/_app_lifecycle.py`, `src/gobby/agents/tmux/pane_monitor.py`, `src/gobby/agents/provider_rotation.py`, `src/gobby/mcp_proxy/registries.py`, `src/gobby/mcp_proxy/tools/agents_registry.py`, `src/gobby/mcp_proxy/tools/agents_context.py`, `src/gobby/mcp_proxy/tools/agents_spawn_tools.py`, `src/gobby/mcp_proxy/tools/spawn_agent/_factory.py`

Move the hardcoded per-provider regexes in `PromptDetector`, `IdleDetector`, and `StallClassifier` into per-provider manifest files consumed through the 2.1 matcher via the 2.3 registry (hence the 2.3 dependency).

**Provider routing (explicit data path).** The registry exposes `for_provider(provider_id) -> CompiledManifest | None`. Detector instances are constructed per provider and cached, but they hold the *registry*, never a captured `CompiledManifest`: every detect call resolves the current compiled manifest via `for_provider` (a dict lookup against the registry cache), so a registry refresh or explicit reload is visible on the very next detection pass with no detector-level invalidation protocol — the registry (2.3) is the single staleness boundary. `IdleCheckHandler` resolves the provider from `run.provider` for spawned runs; the interactive pane-monitoring path (1.1) resolves it from the session's source/provider field. The *public* detect interfaces (pane-text in, result out) and fingerprinting/consecutive-hit logic in `StallClassifier` are otherwise unchanged. A missing or unknown provider yields no manifest: detection is skipped for that pane, state stays `unknown`, no attention episode opens, and the condition is logged once per provider.

**Production composition (concrete).** Detector construction has three production roots, and all three resolve to one shared DB-backed registry (2.3) owned by the runner composition seam. `AgentLifecycleMonitor` is instantiated during GobbyRunner initialization — `init_orchestration` in `src/gobby/runner_init/orchestration.py` — before the FastAPI lifespan in `src/gobby/servers/_app_lifecycle.py` ever runs, so the registry is constructed there, not in the lifespan: `init_orchestration` builds exactly one DB-backed registry from `runner.database` immediately before `AgentLifecycleMonitor`, stores it on the runner (`runner.detection_registry` — `GobbyRunner` in `src/gobby/runner.py` declares every phase-initialized attribute explicitly so mypy can see it, so it gains the phase-3 declaration `detection_registry: DetectionManifestRegistry` with the corresponding import, beside the existing `agent_lifecycle_monitor: AgentLifecycleMonitor | None`; non-optional because construction is unconditional, matching `completion_registry: CompletionEventRegistry`), and passes it into (a) `AgentLifecycleMonitor.__init__` (`src/gobby/agents/lifecycle_monitor.py`), which today constructs `IdleDetector()`, `PromptDetector()`, and `StallClassifier()` bare and hands them to the terminal-prompt, cleanup, health, and idle-check handlers — the registry becomes a required constructor argument injected into every detector it builds. The other two roots receive the same object through the existing `ServiceContainer` bridge — `server.services` is a `ServiceContainer` (`src/gobby/app_context.py`) built by `init_servers` (`src/gobby/runner_init/servers.py`), not the runner — so `ServiceContainer` gains a `detection_registry` field populated by `init_servers` from `runner.detection_registry`, exactly as `agent_lifecycle_monitor` already rides that container. (b) The FastAPI lifespan reads `server.services.detection_registry` and wires it into `TmuxPaneMonitor` (`src/gobby/agents/tmux/pane_monitor.py`), constructed in that lifespan, whose 1.1 interactive detection path builds its detectors from that same registry. (c) Provider rotation (`src/gobby/agents/provider_rotation.py`): `get_failed_providers_for_task` and `select_next_provider` drop their bare `StallClassifier()` default — the classifier parameter becomes required — and because `create_spawn_agent_registry` receives an `AgentRunner`, not the GobbyRunner, the registry is threaded explicitly along the chain that already carries `agent_lifecycle_monitor`: `HTTPServer.__init__` (`src/gobby/servers/http.py`) forwards `services.detection_registry` into `setup_internal_registries` (`src/gobby/mcp_proxy/registries.py`), which passes it to `create_agents_registry` and onto `AgentsRegistryContext` (`src/gobby/mcp_proxy/tools/agents_registry.py`, `src/gobby/mcp_proxy/tools/agents_context.py`); `register_agent_spawn_tools` (`src/gobby/mcp_proxy/tools/agents_spawn_tools.py`) hands `ctx.detection_registry` to `create_spawn_agent_registry` (`src/gobby/mcp_proxy/tools/spawn_agent/_factory.py`), which constructs the registry-backed classifier it passes to rotation. One object, three consumers, two concrete bridges. Constructing a detector without a registry is not a supported production path anywhere; tests may inject fixture registries.

Initial manifest coverage: `claude`, `codex`, `gemini`, `qwen`, `droid` — the five CLIs Gobby integrates. Anything else is the explicit unsupported fallback above.

No behavior change for covered providers: the existing prompt/idle/stall test suites pass unchanged, now exercising the manifest path.

**Acceptance:**

- 2.2.1 - `PromptDetector`, `IdleDetector`, and `StallClassifier` source all patterns from registry-served compiled manifests resolved per detect call — no long-lived compiled-manifest capture — with unchanged public detect interfaces. file: `src/gobby/agents/prompt_detector.py`.
- 2.2.2 - Provider routing: spawned runs use `run.provider`, interactive entries use the session's provider; unknown providers skip detection with state `unknown` and a once-per-provider log. test: `tests/agents/test_provider_routing.py::test_multi_and_unknown_provider`.
- 2.2.3 - Existing detector test suites pass unchanged against the manifest-driven implementations. test: `tests/agents/test_prompt_detector.py`.
- 2.2.4 - The runner composition root wires the shared DB-backed registry into `AgentLifecycleMonitor.__init__` at construction: a warm `AgentLifecycleMonitor` (constructed handlers, cached detectors) observes a content-only manifest edit on its next detection pass across all its detector paths. test: `tests/agents/test_lifecycle_monitor_registry.py::test_warm_monitor_sees_content_edit`.
- 2.2.5 - A warm `TmuxPaneMonitor` (running poll loop, cached interactive-path detectors) observes a content-only manifest edit on its next interactive detection pass. test: `tests/agents/tmux/test_pane_monitor_registry.py::test_warm_pane_monitor_sees_content_edit`.
- 2.2.6 - Rotation provider-error classification resolves through the shared registry with the bare-default constructor removed: a content-only manifest edit changes how `get_failed_providers_for_task` classifies a recorded provider error, with no reconstruction. test: `tests/agents/test_provider_rotation_registry.py::test_rotation_classifier_sees_content_edit`.
- 2.2.7 - Composition order and identity: `init_orchestration` constructs exactly one DB-backed registry before `AgentLifecycleMonitor` and stores it on the runner as the declared phase-3 attribute `detection_registry: DetectionManifestRegistry` (`src/gobby/runner.py`, typed so the assignment and reads pass strict mypy); `init_servers` exposes that instance as `ServiceContainer.detection_registry`; the registry held by the warm `AgentLifecycleMonitor`, the one the lifespan reads from `server.services.detection_registry` for `TmuxPaneMonitor`, and the one threaded through `setup_internal_registries` → `AgentsRegistryContext` → `create_spawn_agent_registry` to back the rotation classifier are the same object. test: `tests/runner_init/test_detection_registry_composition.py::test_one_registry_across_all_roots`.

### 2.3 Bundle and sync manifests to the DB [category: code] (depends: 2.1)

`kind: deliverable`

Targets: `src/gobby/install/shared/detection/claude.toml`, `src/gobby/install/bundled_content_manifest.json`, `src/gobby/agents/detection/registry.py`, `src/gobby/storage/migrations/327_detection_manifests.sql`, `src/gobby/mcp_proxy/tools/workflows/_import.py`

Ship per-provider manifests as bundled templates under `src/gobby/install/shared/detection/<provider>.toml` — one file each for `claude`, `codex`, `gemini`, `qwen`, and `droid` (`claude.toml` is the representative target) — registered in `src/gobby/install/bundled_content_manifest.json`. Schema lives in migration `src/gobby/storage/migrations/327_detection_manifests.sql`: a `detection_manifests` table keyed by provider id with `version`, `engine`, `content`, `source` (`bundled` | `user`), and timestamps.

Startup sync follows the existing bundled-content pattern: templates seed on first install; Gobby-owned (`source='bundled'`) rows refresh on definition drift; a manually edited row is stamped `source='user'` and is never clobbered by sync. The DB is the source of truth.

**Cache/update boundary (one invalidation surface).** `src/gobby/agents/detection/registry.py` defines `DetectionManifestRegistry` (the registry class referenced throughout §1.1 and §2.2), which serves compiled manifests from an in-process cache keyed by **content fingerprint** (2.1) — declared `version` is metadata, never the cache key, so a DB edit that changes content without bumping `version` still invalidates. Two refresh triggers: a bounded staleness check (compare stored content fingerprints against the DB at most every 30 seconds, reload on change) and an explicit reload API wired into the existing workflows `reload_cache` surface (`src/gobby/mcp_proxy/tools/workflows/_import.py`). Because detectors resolve `for_provider` per detect call (2.2), the registry is the *only* invalidation boundary — there is no second, staler detector-level cache to coordinate. A remote catalog is an explicit later extension, not built here.

**Acceptance:**

- 2.3.1 - Bundled manifests for all five providers exist and are hash-registered. file: `src/gobby/install/shared/detection/claude.toml`.
- 2.3.2 - Migration creates `detection_manifests` with version, engine, and ownership fields; an isolated migration/startup test applies it cleanly. file: `src/gobby/storage/migrations/327_detection_manifests.sql`.
- 2.3.3 - Startup sync seeds and drift-refreshes bundled rows while preserving `source='user'` rows. file: `src/gobby/agents/detection/registry.py`.
- 2.3.4 - Detection through an already-cached `IdleCheckHandler` (warm registry, constructed detectors) observes a DB row edit — including a content change with no `version` bump — within the bounded staleness window and immediately on explicit `reload_cache`, changing detection outcomes without code changes or handler reconstruction. test: `tests/agents/detection/test_registry.py::test_cache_boundary_and_user_ownership`.

## P3: Coordination and Metadata Extras

`kind: framing`

**Goal**: Close the two small primitive gaps: waiting on pane output, and transient status that expires on its own.

### 3.1 wait_for_output MCP tool [category: code] (depends: 2.1)

`kind: deliverable`

Targets: `src/gobby/mcp_proxy/tools/agents_query_tools.py`, `src/gobby/agents/detection/safe_regex.py`

Add `wait_for_output` beside the existing `wait_for_agent` (`src/gobby/mcp_proxy/tools/agents_query_tools.py:85`): block until a run's pane output matches a regex or a timeout expires. Args: `run_id`, `pattern` (regex), `timeout_seconds` (capped like `wait_for_agent`), `poll_interval_seconds` (default 2). Each poll captures the pane via the run's tmux session (`capture_pane`) and searches the tail window. Caller-supplied patterns are data-controlled input: compile and search exclusively through the bounded helper owned by 2.1 (`src/gobby/agents/detection/safe_regex.py`, imported read-only — the 2.1 dependency sequences this).

**Branch semantics with deterministic precedence.** Pre-flight, before any polling: unknown `run_id` → error `invalid_run`; run has no tmux coordinates → error `no_terminal`; pattern fails bounded compilation → error `invalid_pattern`; non-finite, non-positive, or absurd numeric inputs → clamped to documented bounds or rejected with `invalid_argument` (NaN/inf always rejected). Each poll then evaluates in this fixed order — the first condition that holds wins, so colliding conditions in the same poll have exactly one outcome:

1. Caller cancellation: polling stops promptly; no leaked capture task survives the call.
2. Capture the pane; on success, search the tail window. Match: `{matched: true, excerpt}` — a match beats a terminal status observed in the same poll.
3. Run at terminal status: `{matched: false, reason: "terminal", status}`.
4. Pane gone (tmux session/pane no longer exists): `{matched: false, reason: "pane_lost", status}` — its own reason, never conflated with `terminal`; `status` is the run's current, possibly nonterminal, status.
5. Capture failed (error/timeout): increment the consecutive-failure counter; at 3, error `capture_failed` — this outranks a deadline expiring on the same poll.
6. Overall deadline exceeded: `{matched: false, reason: "timeout", status}`.

Pattern execution exceeding its time budget mid-poll: error `pattern_timeout` from the bounded helper.

**Acceptance:**

- 3.1.1 - `wait_for_output` implements every branch above under the fixed per-poll precedence order, with capped timeout and bounded pattern execution through the 2.1 helper. file: `src/gobby/mcp_proxy/tools/agents_query_tools.py`.
- 3.1.2 - Scripted-pane tests cover match, timeout, terminal, unknown run, missing pane, pane loss (`reason: pane_lost` with nonterminal status), consecutive capture failures, invalid numerics, pathological patterns, and cancellation without leaked tasks — plus the collision boundaries: match and terminal status in the same poll resolves to match, and the deadline expiring on the third consecutive capture failure resolves to `capture_failed`. test: `tests/mcp_proxy/test_wait_for_output.py::test_wait_branches`.

### 3.2 Transient status metadata with absolute expiry [category: code] (depends: P1)

`kind: deliverable`

Targets: `src/gobby/agents/attention_metadata.py`, `src/gobby/agents/idle_check_handler.py`, `src/gobby/hooks/event_handlers/_agent.py`, `src/gobby/servers/routes/attention.py`, `src/gobby/servers/websocket/broadcast.py`

Transient status chips ("compacting", "retrying provider") that expire without a clearing write. New in-memory daemon-side store `src/gobby/agents/attention_metadata.py` keyed by `entry_id`: `set(entry_id, text, ttl_ms)`, tracked internally on the daemon's monotonic clock and swept lazily on read. Deliberately not persisted — transient by definition; a daemon restart clears it.

**Publication on set (immediate, ordered).** A chip can be set with no accompanying attention transition (compaction, self-report), so each successful `set()` immediately emits an agent metadata-update event through the 1.3 ordering coordinator — `epoch` and `seq` assigned like any attention event, broadcast via `BroadcastMixin` — so subscribed clients learn about the chip when it is set, not at the next unrelated event or roster refetch. Expiry stays client-side from `expires_at`: no clearing event is ever sent.

**Input bounds.** `text` is ≤ 120 UTF-8 characters with control characters rejected; `ttl_ms` is a finite positive integer ≤ 600,000 (10 minutes). Anything else — NaN/inf, zero or negative TTLs, oversized text or payload — is rejected at the boundary (422 on API paths; dropped with a warning log on hook self-report paths) and never enters the store.

**Producer sites (concrete).** Detector-side: `IdleCheckHandler` (`src/gobby/agents/idle_check_handler.py`) sets chips for provider-stall retries and dismissal escalation. Hook-side: the agent hook event handlers (`src/gobby/hooks/event_handlers/_agent.py`) set chips for compaction (`PRE_COMPACT` — "compacting") and accept agent self-reports carrying `ttl_ms`, mirroring herdr's report semantics.

**Publication sites (concrete).** The roster serializer in `src/gobby/servers/routes/attention.py` consumes the metadata snapshot captured inside the 1.3 ordering-lock critical section — never a post-release re-read, so the roster cursor bounds metadata state (1.3). The agent-event payload assembly in `src/gobby/servers/websocket/broadcast.py` reads the store at emit time, under the ordering coordinator that assigns the event's seq.

**Client contract: absolute expiry, not durations.** Every roster entry and agent-event payload carrying transient metadata includes a server-computed `expires_at` (wall-clock ISO timestamp derived at serialization time from the monotonic deadline). Clients evict the chip at `expires_at` on their own — no follow-up server event is required, and a roster fetched mid-TTL sees the remaining lifetime, not the original duration. Producers speak `ttl_ms`; `expires_at` is the only thing clients see.

**Acceptance:**

- 3.2.1 - TTL store sets, serves, and lazily expires transient metadata per entry on a monotonic clock. file: `src/gobby/agents/attention_metadata.py`.
- 3.2.2 - Named producer sites write chips: stall retries and dismissal escalation from `IdleCheckHandler`, compaction and `ttl_ms` self-reports from the agent hook handlers. file: `src/gobby/hooks/event_handlers/_agent.py`.
- 3.2.3 - Roster and agent events carry server-computed `expires_at`; a mid-TTL roster snapshot shows remaining lifetime. test: `tests/agents/test_attention_metadata.py::test_expires_at_contract`.
- 3.2.4 - A chip with no follow-up server event is client-evictable purely from `expires_at`, and expired metadata never serializes. test: `tests/agents/test_attention_metadata.py::test_expiry_without_followup_event`.
- 3.2.5 - A `set()` with no concurrent attention transition (PRE_COMPACT, self-report) delivers a live, seq-ordered metadata event to subscribers; invalid or oversized `text`/`ttl_ms` values are rejected at the boundary and never stored or broadcast. test: `tests/agents/test_attention_metadata.py::test_live_emit_and_validation`.

## P4: GitHub-Topic Skill Hub

`kind: framing`

**Goal**: Zero-submission skill marketplace — the distribution mechanism behind herdr's plugin ecosystem, applied to Gobby skills. Independent of P1–P3.

### 4.1 github-topic hub provider [category: code]

`kind: deliverable`

Targets: `src/gobby/skills/hubs/github_topic.py`, `src/gobby/config/skills.py`, `src/gobby/skills/hubs/manager.py`, `src/gobby/skills/hubs/base.py`, `src/gobby/skills/hubs/github_collection.py`, `src/gobby/mcp_proxy/tools/skills/install_skill.py`

New `GitHubTopicProvider(HubProvider)` (`src/gobby/skills/hubs/base.py` ABC): `discover`/`search` query the GitHub search API for public repos tagged with a configurable topic (default `gobby-skill`); `list_skills`/`get_skill_details` reuse the tree-walking primitives from `GitHubCollectionProvider` (`src/gobby/skills/hubs/github_collection.py`) under the caps below.

**Identity and pinning.** A bare skill slug is ambiguous across a whole topic. The canonical topic-hub item ID is `owner/repo:path`, normalized and confined (reject `..`, absolute paths, and anything escaping the repo root; extraction is confined to the destination directory). Each discovery hit is resolved to the head commit SHA of the repo's default branch at discovery time and persisted as a discovery record containing `item_id`, `repo`, `path`, and `sha`. `get_skill_details`, `download_skill`, and install resolve that persisted record and carry its SHA through every step; a missing record or SHA mismatch returns a stable `item_unavailable` error instead of re-resolving a branch. Caches and content fetches are keyed by `(item_id, sha)`, and content is fetched at that SHA via tarball/contents-at-ref, never a branch ref and never a full clone, so a branch moving between review and install cannot swap the content. Install provenance records the exact persisted `repo`, `path`, and `sha`.

**Untrusted-input hardening.** A public topic is untrusted and unbounded. Hard caps, enforced in the provider: search results ≤ 3 pages / 100 repos per refresh; ≤ 4 concurrent repo probes; per-repo tree traversal depth ≤ 3 and ≤ 200 entries; ≤ 50 skills per repo; manifest/skill files ≤ 256 KB each; downloaded archive ≤ 10 MB compressed (streamed with early abort). All HTTP calls carry timeouts. On 403/rate-limit: exponential backoff and serve the cached discovery set. A repo deleted or made private after discovery yields a clear `item_unavailable` error, not a crash. Over-cap repos are skipped with a logged reason, never partially ingested.

**Safe extraction.** Archives expand through a safe extractor, never a stock `extractall`: only regular files and directories are accepted — symlink, hardlink, device, and FIFO members are rejected, as are absolute paths and any `..` traversal; member count ≤ 512 and aggregate uncompressed bytes ≤ 40 MB regardless of compressed size, enforced while streaming with early abort, so a small archive cannot bomb the disk or escape the destination through link semantics; any violation or failure removes the partially extracted destination before the error surfaces.

Config: add `"github-topic"` to the `HubConfig.type` Literal in `src/gobby/config/skills.py` with fields `topic` and optional `auth_token_env` (default `GITHUB_TOKEN`; unauthenticated search is rate-limited to 10 requests/min, so discovery results are cached with a configurable TTL, default 30 minutes). Register the factory in `HubManager._create_provider` (`src/gobby/skills/hubs/manager.py`) and add a default `gobby-topic` hub entry.

**Install-path parsing.** The hub-reference parser in `src/gobby/mcp_proxy/tools/skills/install_skill.py` currently accepts only `hub:slug` with slash-separated `[A-Za-z0-9_-]` segments, so the canonical topic item ID is rejected before the provider is ever consulted — `gobby-topic:owner/repo:path` carries a second colon, and GitHub owner/repo names may contain dots. Extend the parser to additionally accept the confined `hub:owner/repo:path` form for hub references: owner and repo restricted to GitHub-valid name characters, path validated by the same identity rules above (reject `..`, absolute paths, anything escaping the repo root), and the full `owner/repo:path` item ID passed through to the provider's `download_skill`. Everything downstream — the review-before-install trust model, the loader — is unchanged.

**Acceptance:**

- 4.1.1 - `GitHubTopicProvider` discovers and searches repos by topic with TTL caching and optional token auth. file: `src/gobby/skills/hubs/github_topic.py`.
- 4.1.2 - `github-topic` is a valid configured hub type with a registered factory and default entry. file: `src/gobby/config/skills.py`.
- 4.1.3 - Items are identified as normalized, confined `owner/repo:path`; discovery persists the resolved SHA, and details, download, and install consume that same record without branch re-resolution, with exact SHA provenance recorded on install. test: `tests/skills/hubs/test_github_topic.py::test_sha_pinned_identity`.
- 4.1.4 - Duplicate slugs across repos resolve unambiguously, and a default branch moving between discovery and download does not change installed content. test: `tests/skills/hubs/test_github_topic.py::test_duplicate_slugs_and_moving_branch`.
- 4.1.5 - Caps and failure fixtures: oversized/adversarial repos are skipped at their caps; archive-bomb fixtures (member-count and expansion-ratio) and symlink/hardlink-escape fixtures are rejected with partial extraction cleaned up; rate-limiting backs off and serves cache; a repo disappearing after discovery yields `item_unavailable`. test: `tests/skills/hubs/test_github_topic.py::test_caps_rate_limit_and_disappearing_repo`.
- 4.1.6 - End-to-end install: `install_skill("gobby-topic:owner/repo:path")` parses the topic item ID (second colon, dotted repo names), rejects unconfined paths, loads the persisted discovery record, downloads at that record's SHA, installs, and records its exact `repo`/`path`/`sha` provenance; a missing record or mismatch fails closed. test: `tests/skills/hubs/test_github_topic.py::test_install_skill_topic_reference_end_to_end`.

## E1: End-to-End Verification [category: test] (depends: P1, P2, P3, P4)

`kind: deliverable`

Target: `tests/e2e/test_herdr_backend_foundation.py`

Run against an isolated test daemon (`GOBBY_TEST_PROTECT=1`, temporary state and ports — never the user's daemon). Drive scripted tmux panes — one spawned run, one interactive session — through approval-prompt, recurrence, stall, idle, and completion transitions: assert the episode lifecycle end-to-end (attention rows, WS agent events with epoch/seq under the ordering coordinator, notification dedupe by episode, roster contents including tmux coordinates with the roster cursor bounding both attention and transient-metadata state under forced interleaving, all three attention endpoints reachable through the real app, CAS respond including recurrence rejection, `not_actionable` stalls, and partial-injection indeterminacy, seen-stamping, per-session blocked aggregation with subscribe-first reconciliation). Validate P2 by editing a DB manifest row — including content-only changes with no version bump — and observing changed detection through every production composition root — a warm `AgentLifecycleMonitor`, a warm `TmuxPaneMonitor` interactive pass, and rotation provider-error classification — at the cache boundary with no code change, by the unchanged pre-existing detector suites, by multi-provider and unknown-provider routing, and by pathological-pattern bounds. Validate 3.1 with a pane that prints delayed output plus the failure branches and collision boundaries. Validate 3.2 by fetching the roster mid-TTL, evicting on `expires_at` with no follow-up event, and observing live metadata events from producers with no attention transition. Validate 4.1 against recorded GitHub API fixtures including a moved default branch, rate-limiting, over-cap repos, archive-bomb/link-escape archives, and an end-to-end `install_skill` of a `gobby-topic:owner/repo:path` reference with pinned-SHA provenance. CLI/web surfacing (1.4) is checked by rendering the roster of the test daemon in `gobby sessions` and the web activity page.

**Acceptance:**

- E1.1 - One isolated-daemon verification run exercises every P1-P4 contract above, including persisted discovery-SHA continuity through details, download, and install, and all focused backend, CLI, and web checks pass without contacting user daemon state. test: `tests/e2e/test_herdr_backend_foundation.py::test_backend_foundation_end_to_end`.

## V1 Plan Changelog

`kind: verification`

**Round 1** `kind: enhancement`

- enhancer_run: 8009b2f1-c311-45e5-8153-5bfacfcbeca4
- enhancer_session: e3b49fbc-ccbc-4718-b504-790dc3994d30
- converged: false
- suggestions_presented: 7
- accepted:
  - E1 / better / attention kept orthogonal to lifecycle; per-session blocked count aggregation in CLI/web
  - E2 / better / server-computed absolute `expires_at` on transient metadata; clients evict without follow-up events
  - E3 / better / bounded regex execution (`regex` dep + safe_regex helper) for manifest matcher and wait_for_output
  - E4 / better / topic-hub items identified as `owner/repo:path`, SHA-pinned discovery→download, install provenance
  - E5 / better / episode-scoped `attention_id` beyond fingerprint; single conditional transition function; episode-keyed CAS, seen, and notification dedupe
  - E6 / better / broadcaster epoch UUID + locked monotonic seq shared by roster and event enqueue; seq assigned post-commit
  - E7 / bigger / stable `entry_id` (`run:`/`session:`) making interactive tmux-backed sessions first-class attention surfaces
- declined: (none)
- resolution_notes: All seven folded in. 1.1 restructured around an `attention_states` store keyed by `entry_id` with episode identity (E5+E7), 1.3 ordering contract pinned to epoch+seq (E6), 1.4 rewritten to keep `SessionStatus` lifecycle-only with blocked-count aggregation (E1), 2.1/3.1 route all data-controlled regexes through a bounded helper (E3), 3.2 exposes `expires_at` (E2), 4.1 gains SHA-pinned identity and provenance (E4). Re-validated with `uv run gobby plans validate`.

**Round 1** `kind: verification`

- reviewer_run: a07e9f1e-6fd1-4b67-b5a1-e53d7397c8ef
- reviewer_session: "#9118" (0216c85b-1827-472a-807c-cca29e5c64ed)
- verdict: needs_review
- findings:
  - R1-ORDER-001 / blocking / epoch+seq lock excluded state commit, allowing reverse-order enqueue of committed transitions
  - R1-RESPOND-002 / blocking / respond contract left answer-variant conflicts, bounds, injection failure, and capture races undefined
  - R1-PROVIDER-003 / blocking / no provider-routing data path for manifest selection; single-provider manifest coverage undefined for the rest
  - R1-DEPS-004 / blocking / 2.2 consumed the 2.3 registry while depending only on 2.1; 3.1 imported safe_regex with no dependency
  - R1-SCHEMA-005 / blocking / new Postgres tables lacked concrete migration-file targets and migration acceptance
  - R1-TARGETS-006 / blocking / 1.4 targeted the wrong frontend files; 3.2 omitted producer and serializer targets
  - R1-CACHE-007 / blocking / registry cache had no update boundary beyond startup sync; user-owned row semantics undefined
  - R1-WAIT-008 / blocking / wait_for_output left lookup/capture/pane-loss/numeric/cancellation branches unspecified
  - R1-HUB-009 / blocking / topic hub had no caps for fan-out, traversal, sizes, or downloads over an untrusted topic
- resolution_notes: All nine resolved in this revision. Ordering coordinator moved into the 1.1 transition function holding one lock across commit+seq+enqueue (R1-ORDER-001, tested by forced interleaving). 1.2 respond contract fully specified: exactly-one variant, option membership, 2,048-byte text bound, key allowlist, per-entry injection serialization with capture adjacent to send, stable 404/409 stale_episode/409 prompt_changed/502 injection_failed semantics preserving the episode, and an honest external-typing race exclusion (R1-RESPOND-002). Provider routing defined via registry `for_provider` with per-provider detector construction, run.provider / session-source wiring, unknown-provider skip + once-per-provider log, and five bundled manifests (R1-PROVIDER-003). Dependencies corrected: 2.2 depends on 2.3; 3.1 depends on 2.1 with safe_regex owned by 2.1 and imported read-only (R1-DEPS-004). Concrete migrations 326_attention_states.sql and 327_detection_manifests.sql with isolated migration tests; table creation kept out of managers (R1-SCHEMA-005). 1.4 retargeted to SessionsFilterDropdown/SessionsTab/helpers/useAgentRuns with a frontend aggregation test; 3.2 names producer sites (IdleCheckHandler, agent hook handlers) and serializer sites (attention routes, broadcast) (R1-TARGETS-006). Registry cache boundary: 30s staleness check + explicit reload + source='user' preservation, pinned by 2.3.4 (R1-CACHE-007). wait_for_output branch semantics enumerated including capture-failure retry, pane loss as terminal, numeric clamping, and prompt cancellation (R1-WAIT-008). Topic hub hardened with explicit caps, SHA tarball fetch instead of clone, path confinement, backoff/cache on rate limits, and item_unavailable semantics (R1-HUB-009). New HTTP surface consolidated in routes/attention.py to respect the 1,000-line limit. Re-validated with `uv run gobby plans validate`.

**Round 2** `kind: verification`

- reviewer_run: e6ed2c76-3f68-4eaf-a66b-89ed05ebefca
- reviewer_session: "#9125" (f8ac111a-1c2b-43b6-854b-8836b510544a)
- verdict: needs_review
- findings:
  - R2-ORDER-010 / blocking / roster released the ordering lock between cursor read and state read, so the roster could hold state newer than its cursor; 1.4 lacked the subscribe-first reconciliation requirement
  - R2-RESPOND-011 / blocking / respond path undefined for non-actionable stall episodes, per-variant injection actions, partial submission (paste ok / Enter fails), and blocked events omitted the prompt payload
  - R2-CACHE-012 / blocking / detector-held CompiledManifests survived registry refreshes, and version-keyed compilation missed content-only DB edits
  - R2-ROUTES-013 / blocking / attention router never wired into the FastAPI route registry (_app_routes.py / routes __init__ untargeted) — endpoints implementable yet unreachable
  - R2-WAIT-014 / blocking / wait_for_output branches lacked collision precedence; pane loss overloaded reason=terminal with a possibly nonterminal status
  - R2-META-015 / blocking / metadata set() published no event, so chips without a concurrent transition were invisible until unrelated traffic; text/ttl_ms unbounded
  - R2-HUB-016 / blocking / archive cap covered compressed bytes only — no member-count/expanded-bytes caps, link/device members not rejected
- resolution_notes: All seven resolved in this revision. Roster snapshots now read seq and the full committed attention state inside one ordering-lock critical section (order-independent enrichment assembled after release), attention events carry epoch+seq, and 1.3.2 adds a forced interleaving during roster construction; 1.4 requires the subscribe-first buffer/discard/apply handshake with epoch-change refetch, pinned by new 1.4.4 (R2-ORDER-010). 1.1 defines actionable vs non_actionable episode kinds; respond returns 409 not_actionable for stalls; injection actions specified per variant (digits+Enter, paste+Enter, named key without Enter); failure split into stage:none injection_failed (retry-safe) vs stage:partial injection_indeterminate that retires the episode and forces re-detection; blocked events carry the structured payload, pinned by new 1.2.4/1.2.6 (R2-RESPOND-011). Detectors hold the registry and resolve for_provider on every detect call — no captured manifests — and compilation/caching key on manifest content fingerprint, not version; 2.3.4 now exercises a warm IdleCheckHandler and the reload_cache surface target `src/gobby/mcp_proxy/tools/workflows/_import.py` is added (R2-CACHE-012). 1.2 targets and wires the router through `src/gobby/servers/routes/__init__.py` + `src/gobby/servers/_app_routes.py` (new 1.2.5) and 1.3.5 adds a real-app reachability test over all three endpoints, with 1.3 now depending on 1.2 (R2-ROUTES-013). wait_for_output gains a fixed per-poll precedence (cancellation > match > terminal > pane_lost > capture-failure threshold > timeout), pane loss gets its own `pane_lost` reason with current status, and 3.1.2 adds the match+terminal and timeout+third-failure collision tests (R2-WAIT-014). 3.2 emits a seq-ordered metadata event on every successful set() with client-side-only expiry retained, bounds text (≤120 chars, no control chars) and ttl_ms (finite positive ≤600,000), and adds 3.2.5 for live delivery + rejection (R2-META-015). 4.1 specifies a safe extractor — regular files/dirs only, symlink/hardlink/device/FIFO and traversal rejected, ≤512 members, ≤40 MB expanded, streaming enforcement, partial-extraction cleanup — with archive-bomb and link-escape fixtures in 4.1.5 (R2-HUB-016). Re-validated with `uv run gobby plans validate`.

**Round 3** `kind: verification`

- reviewer_run: 89f7be01-4958-4473-94ef-f40ead786eda
- reviewer_session: "#9129" (efc04723-e9aa-4706-b946-116abe16c129)
- verdict: needs_review
- findings:
  - R3-ORDER-017 / blocking / §1.3's opening still instructed fetch-roster-before-subscribe, contradicting the subscribe-first ordering contract and §1.4 and permitting a transition between roster response and subscription to be lost
  - R3-ORDER-018 / blocking / transient metadata was read at serialization time after the ordering lock released, so a roster's cursor did not bound its metadata state and buffered replay could regress a newer chip to an older one
  - R3-CACHE-019 / blocking / the production detector composition point (`AgentLifecycleMonitor` constructing detectors directly) was untargeted, so detector consumers beyond the tested `IdleCheckHandler` could remain detached from the DB registry
  - R3-HUB-020 / blocking / the canonical `owner/repo:path` topic item ID was unreachable through `install_skill`, whose hub-reference parser rejects a second colon (and dotted repo names) before the provider is consulted
- resolution_notes: All four resolved in this revision. §1.3's opening now states the subscribe-first handshake wherever the roster fetch is instructed — subscribe, buffer, fetch, discard same-epoch events at or below the roster seq, apply the rest in order, refetch on epoch change (R3-ORDER-017). Roster construction snapshots transient metadata together with attention state and seq inside the ordering-lock critical section and enriches only from that immutable snapshot; §3.2's publication sites read the locked snapshot (roster) or emit under the coordinator (events); 1.3.2 adds a two-metadata-set forced interleaving proving the cursor bounds metadata with no chip regression on replay (R3-ORDER-018). 2.2 targets `src/gobby/agents/lifecycle_monitor.py` as the production composition point: one DB-backed registry constructed there and injected into every detector consumer, per-call `for_provider` resolution retained, pinned by new 2.2.4 warm-monitor content-edit test (R3-CACHE-019). 4.1 targets `src/gobby/mcp_proxy/tools/skills/install_skill.py`, extends hub-reference parsing to the confined `hub:owner/repo:path` form with the full item ID passed through to `download_skill`, and adds 4.1.6 end-to-end pinned-SHA install-with-provenance test (R3-HUB-020). E1 updated to match. Re-validated with `uv run gobby plans validate`.

**Round 4** `kind: verification`

- reviewer_run: d6df36f4-d127-4dc5-9a68-0a718ee9127e
- reviewer_session: "#9133" (1a0b1e7b-1cc2-4e27-9a41-d8bc07a7c015)
- verdict: needs_review
- findings:
  - R4-CACHE-021 / blocking / the single-composition-point claim was incomplete: `TmuxPaneMonitor` is independently constructed in `_app_lifecycle.py` and becomes a detector consumer under §1.1, and `provider_rotation.py` constructs `StallClassifier()` bare when no classifier is supplied — neither path received the DB-backed registry nor was exercised by 2.2.4
- resolution_notes: Resolved in this revision. §2.2's production composition now names all three detector construction roots and moves registry ownership to the daemon composition root: the lifespan in `src/gobby/servers/_app_lifecycle.py` constructs exactly one DB-backed registry and injects it into `AgentLifecycleMonitor.__init__`, into `TmuxPaneMonitor` for the 1.1 interactive detection path, and into provider rotation — `get_failed_providers_for_task` / `select_next_provider` drop the bare `StallClassifier()` default (classifier required) and the production caller `src/gobby/mcp_proxy/tools/spawn_agent/_factory.py` passes a registry-backed classifier. All four files added to 2.2 Targets. New acceptance 2.2.5 (warm `TmuxPaneMonitor` observes a content-only manifest edit on its next interactive pass) and 2.2.6 (rotation classification through the shared registry observes a content-only edit, bare default removed); §1.1's detection-sources paragraph cross-references the shared-registry composition. E1 now validates the content-only edit through every composition root. Re-validated with `uv run gobby plans validate`.

**Round 5** `kind: verification`

- reviewer_run: 3a737270-1913-45a4-8146-4c823310afcd
- reviewer_session: "#9138" (302d8775-3d77-43b4-8127-a295d5a27012)
- verdict: needs_review
- findings:
  - R5-CACHE-022 / blocking / the round-4 resolution placed registry ownership in the FastAPI lifespan, but `AgentLifecycleMonitor` is instantiated earlier — in `init_orchestration` (`src/gobby/runner_init/orchestration.py`) during GobbyRunner initialization — so lifespan-owned constructor injection was unreachable and that call site was untargeted
- resolution_notes: Resolved in this revision. Registry ownership moved to the actual runner composition seam: `init_orchestration` (`src/gobby/runner_init/orchestration.py`, added to 2.2 Targets) constructs exactly one DB-backed registry immediately before `AgentLifecycleMonitor`, passes it into `AgentLifecycleMonitor.__init__` as a required argument, and stores it on the runner as `runner.detection_registry`; the later FastAPI lifespan reaches the identical instance through `server.services` and wires it into `TmuxPaneMonitor`, and the rotation classifier passed by `_factory.py` is backed by the same runner-owned registry. §2.2's production-composition paragraph now states this concrete object path, 2.2.4 asserts constructor injection at the runner root, and new 2.2.7 pins composition order and same-object identity of the registry across all three roots. §1.1's cross-reference updated to runner-owned registry. Re-validated with `uv run gobby plans validate`.

**Round 6** `kind: verification`

- reviewer_run: bbbda028-3cf4-450c-8f30-63126c67fae7
- reviewer_session: "#9157" (b0953124-d3e0-4c1b-953b-f29c4aaa7904)
- verdict: needs_review
- findings:
  - R6-CACHE-023 / blocking / the runner-owned registry had no reachable handoff to two claimed roots: `server.services` is a `ServiceContainer` built by `init_servers`, not the GobbyRunner, and had no `detection_registry` field; `create_spawn_agent_registry` receives an `AgentRunner`, not the GobbyRunner — so neither the lifespan nor the rotation classifier could obtain the claimed same object, leaving 2.2.7 unimplementable as written
- resolution_notes: Resolved in this revision. Both bridges made concrete via the `ServiceContainer` idiom that already carries `agent_lifecycle_monitor`: `ServiceContainer` (`src/gobby/app_context.py`) gains a `detection_registry` field populated by `init_servers` (`src/gobby/runner_init/servers.py`) from `runner.detection_registry`; the lifespan reads `server.services.detection_registry` for `TmuxPaneMonitor`; for rotation the field is threaded `HTTPServer.__init__` (`src/gobby/servers/http.py`) → `setup_internal_registries` (`src/gobby/mcp_proxy/registries.py`) → `create_agents_registry` / `AgentsRegistryContext` (`src/gobby/mcp_proxy/tools/agents_registry.py`, `src/gobby/mcp_proxy/tools/agents_context.py`) → `register_agent_spawn_tools` (`src/gobby/mcp_proxy/tools/agents_spawn_tools.py`) → `create_spawn_agent_registry` (`src/gobby/mcp_proxy/tools/spawn_agent/_factory.py`), which constructs the registry-backed classifier. All seven touched composition files added to 2.2 Targets; 2.2.7 now asserts same-object identity through these concrete paths; runner-before-`AgentLifecycleMonitor` construction order retained. Re-validated with `uv run gobby plans validate`.

**Round 7** `kind: verification`

- reviewer_run: 7073ef3d-00d2-4766-b32b-73f63bdf0479
- reviewer_session: "#9162" (97b1bbaf-fd64-4dc2-9e50-fff07afff539)
- verdict: needs_review
- findings:
  - R7-TARGET-024 / blocking / the runner bridge requires `runner.detection_registry`, but §2.2 omitted `src/gobby/runner.py`: `GobbyRunner` explicitly declares every phase-initialized attribute so mypy can see it and has no `detection_registry` field, so assigning it in `init_orchestration` and reading it in `init_servers` would produce attr-defined errors under the repository's strict typing
- resolution_notes: Resolved in this revision. `src/gobby/runner.py` added to §2.2 Targets. The 2.3 registry class is now pinned as `DetectionManifestRegistry` (defined in `src/gobby/agents/detection/registry.py`), and `GobbyRunner` gains the phase-3 declaration `detection_registry: DetectionManifestRegistry` with its import, beside the existing `agent_lifecycle_monitor: AgentLifecycleMonitor | None` — non-optional because `init_orchestration` constructs it unconditionally, matching the `completion_registry: CompletionEventRegistry` precedent. §2.2's production-composition paragraph and 2.2.7 state the typed declaration explicitly; construction order and same-object identity assertions unchanged. Re-validated with `uv run gobby plans validate`.

**Round 8** `kind: verification`

- reviewer_run: 23cff1bc-7cce-44cb-9ef7-fe99f19babcb
- reviewer_session: "#9167" (e0b95aef-56c3-40e1-8b66-78e72802e6a1)
- verdict: approved
- findings: none
- resolution_notes: Verification-only pass over the four R7-TARGET-024 resolution points (§2.2 Targets includes `src/gobby/runner.py`; typed phase-3 declaration `detection_registry: DetectionManifestRegistry` with the `completion_registry` precedent; class name pinned in §2.3; 2.2.7 asserts the typed declaration with construction order and same-object identity unchanged). Zero findings — plan approved at round 8.

## M1 Task Manifest

`kind: manifest`

```yaml
- title: Persist and broadcast needs-attention episodes
  category: code
  task_type: feature
  depends_on: []
  validation_criteria: src/gobby/storage/migrations/326_attention_states.sql
  labels:
  - covers:herdr-interface-backend-foundation:1.1:1.1.1
  - covers:herdr-interface-backend-foundation:1.1:1.1.2
  - covers:herdr-interface-backend-foundation:1.1:1.1.3
  - covers:herdr-interface-backend-foundation:1.1:1.1.4
  - covers:herdr-interface-backend-foundation:1.1:1.1.5
  - covers:herdr-interface-backend-foundation:1.1:1.1.6
  - covers:herdr-interface-backend-foundation:1.1:1.1.7
  tdd: true
  source_section: '1.1'
  implementation_domain: backend
- title: Structured prompt payload and respond API
  category: code
  task_type: feature
  depends_on:
  - '1.1'
  validation_criteria: src/gobby/agents/prompt_detector.py
  labels:
  - covers:herdr-interface-backend-foundation:1.2:1.2.1
  - covers:herdr-interface-backend-foundation:1.2:1.2.2
  - covers:herdr-interface-backend-foundation:1.2:1.2.3
  - covers:herdr-interface-backend-foundation:1.2:1.2.4
  - covers:herdr-interface-backend-foundation:1.2:1.2.5
  - covers:herdr-interface-backend-foundation:1.2:1.2.6
  tdd: true
  source_section: '1.2'
  implementation_domain: backend
- title: Attention roster endpoint
  category: code
  task_type: feature
  depends_on:
  - '1.1'
  - '1.2'
  validation_criteria: src/gobby/servers/routes/attention.py
  labels:
  - covers:herdr-interface-backend-foundation:1.3:1.3.1
  - covers:herdr-interface-backend-foundation:1.3:1.3.2
  - covers:herdr-interface-backend-foundation:1.3:1.3.3
  - covers:herdr-interface-backend-foundation:1.3:1.3.4
  - covers:herdr-interface-backend-foundation:1.3:1.3.5
  tdd: true
  source_section: '1.3'
  implementation_domain: backend
- title: Surface attention in CLI and web activity
  category: code
  task_type: feature
  depends_on:
  - '1.3'
  validation_criteria: src/gobby/cli/sessions.py
  labels:
  - covers:herdr-interface-backend-foundation:1.4:1.4.1
  - covers:herdr-interface-backend-foundation:1.4:1.4.2
  - covers:herdr-interface-backend-foundation:1.4:1.4.3
  - covers:herdr-interface-backend-foundation:1.4:1.4.4
  tdd: true
  source_section: '1.4'
  implementation_domain: fullstack
- title: Manifest schema and compiled matcher
  category: code
  task_type: feature
  depends_on: []
  validation_criteria: src/gobby/agents/detection/schema.py
  labels:
  - covers:herdr-interface-backend-foundation:2.1:2.1.1
  - covers:herdr-interface-backend-foundation:2.1:2.1.2
  - covers:herdr-interface-backend-foundation:2.1:2.1.3
  - covers:herdr-interface-backend-foundation:2.1:2.1.4
  tdd: true
  source_section: '2.1'
  implementation_domain: backend
- title: Port detectors to manifest-driven rules
  category: refactor
  task_type: feature
  depends_on:
  - '2.3'
  validation_criteria: src/gobby/agents/prompt_detector.py
  labels:
  - covers:herdr-interface-backend-foundation:2.2:2.2.1
  - covers:herdr-interface-backend-foundation:2.2:2.2.2
  - covers:herdr-interface-backend-foundation:2.2:2.2.3
  - covers:herdr-interface-backend-foundation:2.2:2.2.4
  - covers:herdr-interface-backend-foundation:2.2:2.2.5
  - covers:herdr-interface-backend-foundation:2.2:2.2.6
  - covers:herdr-interface-backend-foundation:2.2:2.2.7
  tdd: false
  source_section: '2.2'
  assigned_agent: backend-developer
- title: Bundle and sync manifests to the DB
  category: code
  task_type: feature
  depends_on:
  - '2.1'
  validation_criteria: src/gobby/install/shared/detection/claude.toml
  labels:
  - covers:herdr-interface-backend-foundation:2.3:2.3.1
  - covers:herdr-interface-backend-foundation:2.3:2.3.2
  - covers:herdr-interface-backend-foundation:2.3:2.3.3
  - covers:herdr-interface-backend-foundation:2.3:2.3.4
  tdd: true
  source_section: '2.3'
  implementation_domain: backend
- title: wait_for_output MCP tool
  category: code
  task_type: feature
  depends_on:
  - '2.1'
  validation_criteria: src/gobby/mcp_proxy/tools/agents_query_tools.py
  labels:
  - covers:herdr-interface-backend-foundation:3.1:3.1.1
  - covers:herdr-interface-backend-foundation:3.1:3.1.2
  tdd: true
  source_section: '3.1'
  implementation_domain: backend
- title: Transient status metadata with absolute expiry
  category: code
  task_type: feature
  depends_on:
  - '1.1'
  - '1.2'
  - '1.3'
  - '1.4'
  validation_criteria: src/gobby/agents/attention_metadata.py
  labels:
  - covers:herdr-interface-backend-foundation:3.2:3.2.1
  - covers:herdr-interface-backend-foundation:3.2:3.2.2
  - covers:herdr-interface-backend-foundation:3.2:3.2.3
  - covers:herdr-interface-backend-foundation:3.2:3.2.4
  - covers:herdr-interface-backend-foundation:3.2:3.2.5
  tdd: true
  source_section: '3.2'
  implementation_domain: backend
- title: github-topic hub provider
  category: code
  task_type: feature
  depends_on: []
  validation_criteria: src/gobby/skills/hubs/github_topic.py
  labels:
  - covers:herdr-interface-backend-foundation:4.1:4.1.1
  - covers:herdr-interface-backend-foundation:4.1:4.1.2
  - covers:herdr-interface-backend-foundation:4.1:4.1.3
  - covers:herdr-interface-backend-foundation:4.1:4.1.4
  - covers:herdr-interface-backend-foundation:4.1:4.1.5
  - covers:herdr-interface-backend-foundation:4.1:4.1.6
  tdd: true
  source_section: '4.1'
  implementation_domain: backend
- title: Verify Herdr backend foundation end to end
  category: test
  task_type: task
  depends_on:
  - '1.1'
  - '1.2'
  - '1.3'
  - '1.4'
  - '2.1'
  - '2.2'
  - '2.3'
  - '3.1'
  - '3.2'
  - '4.1'
  validation_criteria: Run the isolated-daemon E1 verification and all focused backend, CLI, and web checks.
  labels:
  - covers:herdr-interface-backend-foundation:E1:E1.1
  tdd: false
  source_section: 'E1'
  assigned_agent: backend-developer
```
