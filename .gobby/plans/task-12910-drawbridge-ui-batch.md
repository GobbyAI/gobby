# Drawbridge UI batch — 11 pending annotations + 1 reported bug

> **Round 2 revision** — addresses Round 1 adversary findings F1–F8 (recorded in #12911 description). Key changes:
>
> - **§1.5 split** — user's "chip on Sessions tab when local model" ask stays as §1.5 (Sessions tab), and #11199's "Local chip on agent cards" becomes new **§1.6** targeting `AgentsTab.tsx` + backend `is_local`.
> - **§2.1 root cause replaced** — live `/api/admin/usage` returns `by_source: {claude: ...}` only AND `by_model` includes an `unknown` bucket, despite 512 codex sessions. Bug is in source/model attribution in `SessionTokenTracker.get_usage_summary()` aggregation, not "Codex parser returns usage=None" (that hypothesis was wrong — `_parse_event_msg` does emit `TokenUsage`). Plan now requires lifecycle-level repro.
> - **§2.1 / §2.2 endpoint URLs corrected** — `/api/admin/usage` and `/api/admin/tokens/timeseries` (previous draft cited non-existent `/api/sessions/usage-breakdown` and `/api/admin/token-timeseries`).
> - **§3.2 strengthened** — captures the offending Codex developer-message body as a committed test fixture; targets `splitProtocolContent` in `protocolContent.ts` (the actual collapsed-Protocol code path), not just `Markdown.tsx`'s stripping regex; flags backend `transcript_renderer.py` for parallel review.
> - **§3.3 + §1.6 add dependency on #12917** — `ToolCallCard.tsx` (1008 lines) and `AgentsTab.tsx` (1050 lines) violate the <1000-line rule; refactor task #12917 was created by the adversary under #12730.
> - **§4.1 root cause confirmed** — `web/src/components/chat/ChatPage.tsx:169-172` has the offending `useEffect(() => { if (isMobile && isPinned) setIsPinned(false) }, [isMobile, isPinned, setIsPinned])`. Fix: gate the close on the desktop→mobile *transition* via `useRef` so user-initiated open isn't immediately reverted. Test target moved from `ActivityPanel.test.tsx` to a `ChatPage` integration harness.
> - **§1.2 alias cleanup** — explicit deletion step folded into §1.6 wrap-up.

## Overview

Land the 10 pending Drawbridge freeform annotations from `.moat/moat-tasks-detail.json` (rows 115–124 in `.moat/moat-tasks.md`), the user-asked Sessions-tab LOCAL chip, gobby-task **#11199** (Local chip on agent cards in the Workflows tab), and one user-reported bug (mobile activity panel auto-close when toggled). All work targets the Vite/React frontend at `web/src/`, with one backend touch each for #4 (token-event source/model attribution) and #1.6 (`is_local` field on the agents API). Verification is end-to-end via Chrome DevTools at `http://localhost:60889/#chat`.

## Constraints

- Per-Drawbridge-task lifecycle: flip `.moat/moat-tasks-detail.json` `status: "to do" → "doing"` before edits, `→ "done"` after edits, and tick `[x]` in `.moat/moat-tasks.md`. Batch JSON updates per Gobby task to avoid 10 separate writes.
- Chip work introduces ONE reusable `.chip` class consumed by Tasks list (§1.3), Sessions tab `renderBadges` (§1.5), and agent cards (§1.6). No parallel chip implementations.
- §1.2's `.session-kind-badge` aliases are temporary scaffolding; §1.6 deletes them in its wrap-up step (not a separate follow-up).
- Token Efficiency chart fix (§2.2) preserves visual design; only the green-tooltip-shows-0 bug is fixed by dropping `stackId="tokens"`.
- Granularity selector (§2.3) is **deleted**, not hidden; granularity derives from `hours` (≤6h → `30m`, ≤168h → `1h`, else `1d`).
- Sessions filter dropdown blue (§3.1) applies only to the collapsed trigger; expanded menu styling stays.
- §3.3 (ToolCallCard wrap) and §1.6 (AgentsTab) touch files over 1000 lines. Each declares a `depends: refactor #12917` so the line-cap rule is satisfied at execution time. The implementing agent must NOT inflate either file further; a narrow extraction (e.g., a `JsonBlock` component for §3.3, an `AgentCard` row component for §1.6) lands in the same commit if #12917 hasn't merged yet.
- Backend changes (§2.1 attribution fix, §1.6 `is_local` field) ship in their own commits.
- DO NOT run the full pytest suite (CLAUDE.md). Run scoped tests only.
- Verification must use Chrome DevTools at `http://localhost:60889/#chat` (Vite dev server port; the daemon HTTP API is at 60887).

---

## Phase 1: Chat input + chip standard

**Goal**: Restore the original chat-input footprint, normalize TaskTree font size, introduce a single `.chip` class, and ship LOCAL chips on both Sessions tab rows (user ask) and Workflows agent cards (#11199).

### 1.1 Restore one-line textarea and 36px send button [category: code]

Target: `web/src/components/chat/ChatInput.tsx`

Commit `8efb1a9a0` ("surface Speaker + Microphone toggles in chat input") inflated the textarea minimum height and the primary button. Revert those specific Tailwind class values without touching the auto-grow `useEffect` or the speaker/mic toggles themselves.

Specific edits:

- Line ~721 — textarea className contains `min-h-[52px]`. Change `min-h-[52px]` → `min-h-[36px]`.
- Line ~591 — `primaryButtonClassName` `cn(...)` first argument starts with `'inline-flex h-[52px] w-[52px] self-start ...'`. Change `h-[52px] w-[52px] self-start` → `h-[36px] w-[36px] self-end`. (The `self-start` came in with the inflation; `self-end` keeps the small button bottom-aligned with the textarea, matching pre-#12070 behavior.)

Leave the auto-grow effect intact:

```ts
useEffect(() => {
  const textarea = textareaRef.current
  if (textarea) {
    textarea.style.height = '0'
    textarea.style.height = `${Math.min(textarea.scrollHeight, 200)}px`
    textarea.scrollTop = textarea.scrollHeight
  }
}, [input])
```

The textarea grows to fit content up to 200px; that behavior is unchanged.

**Validation criteria**: Open `http://localhost:60889/#chat`. The chat input textarea is one line tall on initial render. Send button is 36×36px (inspect via DevTools — computed `height`/`width`). Typing several lines still grows the textarea up to 200px max. Speaker/mic icons in the toolbar remain present.

### 1.2 Introduce shared `.chip` class and migrate session-kind badges [category: refactor]

Target: `web/src/components/chat/styles/sessions-tab.css`, `web/src/components/activity/SessionsTab.tsx`, `web/src/components/chat/AgentStatusBar.tsx`

Promote the existing `.session-kind-badge` rules into a generic `.chip` + `.chip--<variant>` system so Tasks list (§1.3), the Sessions LOCAL variant (§1.5), and the agent-card LOCAL variant (§1.6) all share one CSS contract. Backwards-compatible aliases stay until §1.6 deletes them.

In `web/src/components/chat/styles/sessions-tab.css` (around line 271), refactor:

```css
/* shared chip base */
.chip {
  display: inline-flex;
  align-items: center;
  padding: 0 0.5rem;
  height: 1.25rem;
  font-size: 0.625rem;
  font-weight: 600;
  letter-spacing: 0.05em;
  border-radius: 9999px;
  text-transform: uppercase;
}

.chip--tmux  { background: color-mix(in srgb, #14b8a6 15%, transparent); color: #14b8a6; }
.chip--web   { background: color-mix(in srgb, #0284c7 15%, transparent); color: #0284c7; }
.chip--auto  { background: color-mix(in srgb, #f97316 15%, transparent); color: #f97316; }
.chip--sandbox { /* keep existing color */ }

/* TEMPORARY alias — deleted by §1.6 once all callers emit `chip chip--*` */
.session-kind-badge { /* same rules as .chip */ }
.session-kind-badge--tmux { /* same as .chip--tmux */ }
/* etc */
```

Update `SessionsTab.tsx` `getSessionTypeBadge` / `getSandboxBadge` / `getAgentBadge` (lines 75–99) and `renderBadges` (line 101) to emit `chip chip--<variant>` className strings. Update `AgentStatusBar.tsx` likewise.

**Validation criteria**: `gcode search-content "session-kind-badge"` returns no consumers in `.tsx` files (only the CSS aliases remain). Sessions tab still renders TMUX/WEB/SB chips identically (visual diff via Chrome DevTools screenshot before/after — pixel-equal). `chip` class is exported and ready for §1.3, §1.5, §1.6.

### 1.3 Convert Tasks list highlighted-text labels to chips [category: code] (depends: 1.2)

Target: `web/src/components/tasks/TaskTree.tsx`, `web/src/components/tasks/TaskBadges.tsx`, `web/src/components/tasks/task-execution.css`

The right-side state/priority/category labels on Tasks list rows currently render as plain highlighted text (visible in the screenshot for Drawbridge task `df52f9bf-f43a-43b7-9490-f2a5c266d997`: "Ready", "Medium", "refactor"). Convert these to `chip` instances using the class introduced in §1.2.

In `TaskBadges.tsx`:

- `StatusBadge` (lines 61–73) — already a chip-like span; change className to `chip chip--state-{status}` and add CSS rules for state variants (Ready=blue, In Progress=amber, Done=green, etc.) in `task-execution.css`.
- Add `PriorityBadge` and `TypeBadge` if they don't already export — if they do, similarly convert to `chip chip--priority-{n}` and `chip chip--type-{kind}`.

In `TaskTree.tsx` node renderer (around the row body), wrap the right-side label spans in the new chip components. Confirm the row layout still aligns (chips shouldn't push the title off-screen at narrow widths; test in DevTools mobile viewport at 375px).

**Validation criteria**: Tasks list rows render labels as rounded chips visually matching TMUX/WEB/SB. Hover/focus styling intact. No layout regression at 375px viewport.

### 1.4 Match Tasks list font size to Sessions list [category: code]

Target: `web/src/components/tasks/task-execution.css`

Drop the `0.85` multiplier on `.tree-node` font-size so Tasks list rows match the Sessions list at the default base size.

```css
.tree-node {
  /* was: font-size: calc(var(--font-size-base) * 0.85); */
  font-size: var(--font-size-base);
}
.tree-node-title { font-size: inherit; }
.tree-node-ref   { font-size: inherit; }
```

Re-test row height: a base-sized row in `.tree-node` may need vertical padding adjusted to match Sessions list row height. If Sessions list uses a specific row-height utility, mirror it in `.tree-node`. Compare side-by-side in DevTools.

**Validation criteria**: Open Activity panel → switch between Sessions tab and Tasks tab. Row text is the same size (DevTools computed `font-size` matches). Row vertical rhythm matches; no overlap or excessive whitespace.

### 1.5 LOCAL chip variant on Sessions tab rows [category: code] (depends: 1.2)

Target: `web/src/components/activity/SessionsTab.tsx`, `web/src/components/chat/styles/sessions-tab.css`

User ask (separate from #11199): Sessions tab rows running against a local model get a `LOCAL` chip alongside TMUX/WEB/SB. `WatchingSessionEntry.provider: string` is already plumbed (line 42 of `SessionsTab.tsx`).

Local detection rules — confirm against `src/gobby/llm/` provider definitions on first edit and use whatever the catalog actually emits. Initial implementation:

- If `provider` ∈ `{"lmstudio", "ollama", "llamacpp", "local"}` → LOCAL.
- If `provider` ∈ `{"openai", "openai_compatible"}` AND `entry.label` (the displayed model name) starts with `gpt-oss-` (currently `gpt-oss-120b` is in the sessions table) → LOCAL.

If only `gpt-oss-*` model-name detection is reliable in this repo, ship with that and a TODO to revisit when explicit local providers land.

```ts
const LOCAL_PROVIDERS = new Set(['lmstudio', 'ollama', 'llamacpp', 'local'])

function getLocalBadge(entry: WatchingSessionEntry): { label: string; className: string } | null {
  if (LOCAL_PROVIDERS.has(entry.provider)) {
    return { label: 'LOCAL', className: 'chip--local' }
  }
  if (entry.label?.toLowerCase().includes('gpt-oss')) {
    return { label: 'LOCAL', className: 'chip--local' }
  }
  return null
}
```

Add to `renderBadges`:

```ts
const badges = [
  getSessionTypeBadge(entry.sessionType),
  getSandboxBadge(entry.sandboxEnabled),
  getAgentBadge(entry.agentRunId),
  getLocalBadge(entry),
].filter(...).sort(...)
```

CSS rule:

```css
.chip--local {
  background: color-mix(in srgb, #8b5cf6 15%, transparent);
  color: #8b5cf6;
}
```

(Violet to differentiate from cyan TMUX and blue WEB.)

**Validation criteria**: Start a session against an LM Studio or `gpt-oss-*` model. The Sessions tab row for that session shows a `LOCAL` chip alongside TMUX/WEB. Cloud-model sessions (Claude, Codex with cloud GPT, Gemini cloud) do NOT show the chip.

### 1.6 LOCAL chip on Workflows agent cards [category: code] (depends: 1.2, refactor #12917)

Linked gobby-task: **#11199** ("Add Local chip to agent cards in web UI"). Claim and link this commit.

Target backend: `src/gobby/agents/registry.py` and the route(s) in `src/gobby/servers/routes/` that emit running-agent and agent-definition payloads (grep `agent_run` / `running_agents` / agent-definitions endpoints in `src/gobby/servers/routes/`).

Target frontend: `web/src/components/workflows/AgentsTab.tsx` (currently 1050 lines — see refactor caveat below) and any subcomponents that render an agent card.

Backend changes:

1. Add a computed `is_local: bool` field to the agent-run/agent-definition payloads. Source the model from the agent-run record (or, for agent-defs, from the configured provider/model). Use the same detection ruleset as §1.5 (`LOCAL_PROVIDERS` set + `gpt-oss-*` model-name fallback) — define the predicate once in Python under `src/gobby/llm/` (e.g., `is_local_model(provider: str | None, model: str | None) -> bool`) and import it from the routes.
2. The matching frontend type (the `AgentDefInfo` interface in `AgentsTab.tsx:20`, plus the running-agent shape) gets `isLocal: boolean` (camelCase on the wire, mapped from the JSON `is_local`).

Frontend changes:

3. In `AgentsTab.tsx` agent-card render (find the JSX block that renders one card; likely uses `AgentDefInfo`), add the `LOCAL` chip when `def.isLocal === true`. Use the same `chip chip--local` className as §1.5 — no parallel CSS.

Refactor caveat:

`AgentsTab.tsx` is 1050 lines, over the CLAUDE.md 1000-line cap. Refactor task **#12917** (under #12730) covers the broader monolith breakup. This task declares `depends: #12917`. If #12917 hasn't merged when this task is picked up, the implementing agent extracts a single `AgentCard.tsx` row component as part of this commit (move the ~50–80 lines of card rendering JSX into a new file, import it back into `AgentsTab.tsx`) so the resulting file lands under 1000 lines. Do NOT add to `AgentsTab.tsx` without that extraction.

Wrap-up step (in this same commit, after the chip ships):

4. Delete the `.session-kind-badge*` aliases introduced in §1.2 from `web/src/components/chat/styles/sessions-tab.css`. Confirm `gcode search-content "session-kind-badge"` returns ZERO matches across the repo (TSX consumers were migrated in §1.2, §1.3, §1.5; this commit completes the cleanup).

**Validation criteria**:
- Backend `GET /api/agents/<...>` (or whatever the running-agents/agent-definitions route is — confirm with `gcode search "agent route"` on first edit) returns `is_local: true` for an LM Studio / `gpt-oss-*` agent and `is_local: false` for a Claude / cloud agent.
- Workflows tab → Agents shows a `LOCAL` chip on local-model agent cards. Cloud agents do NOT show the chip.
- `wc -l web/src/components/workflows/AgentsTab.tsx` returns < 1000 (extracted via `AgentCard.tsx` if #12917 hasn't merged).
- `gcode search-content "session-kind-badge"` returns 0 matches anywhere in the repo (CSS aliases deleted, no TSX consumers).
- gobby-task #11199 closed via this commit.

---

## Phase 2: Dashboard cards

**Goal**: Surface non-Claude usage by fixing source/model attribution, fix the Token Efficiency chart's misleading hover values, and remove the redundant granularity selector.

### 2.1 Fix `by_source` / `by_model` attribution so non-Claude usage appears [category: code]

Target: `src/gobby/sessions/token_tracker.py` (the `SessionTokenTracker.get_usage_summary()` aggregator that populates `usage_by_source` / `usage_by_model`), `src/gobby/servers/routes/admin/_usage.py` (the `/api/admin/usage` route), plus whichever transcript/lifecycle code path attributes the per-session source and model.

**Live diagnostic** (curl against the running daemon):

```text
$ curl -s http://localhost:60887/api/admin/usage | jq '.by_source, .by_model'
{
  "claude": { "input_tokens": 823092, "output_tokens": 33727353, "cache_read_tokens": 16545761935, "cache_creation_tokens": 792847992, "session_count": 1119 }
}
{
  "claude-opus-4-7": { ... },
  "unknown": { "input_tokens": ... }
}
```

Despite `sessions` table showing 512 codex + 91 gemini sessions:

```text
$ sqlite3 ~/.gobby/gobby-hub.db "SELECT source, COUNT(*) FROM sessions GROUP BY source"
claude|1215
codex|512
cron|1
gemini|91
pipeline|37
qwen|1
system|1
```

The `by_source` aggregation only emits `claude`, AND the `by_model` aggregation has an `unknown` bucket — meaning attribution is partially happening but mapping non-Claude sources to their proper labels is broken. The previous draft's "Codex `_parse_message` returns `usage=None`" hypothesis was **wrong**: `CodexTranscriptParser._parse_event_msg` (codex.py:271) emits a `ParsedMessage` with `TokenUsage` for `event_msg` `token_count` payloads, and `lifecycle.py` records any message with `usage` present.

Implementation:

1. **Pin the actual point of failure** by writing a failing end-to-end test before any fix:
   - Test path: `tests/sessions/test_token_tracker_attribution.py` (new file).
   - Fixture: a Codex `event_msg` `token_count` line and a Gemini usage line, fed through `SessionLifecycleManager._process_session_transcript` against an in-memory DB.
   - Assertion: `SessionTokenTracker.get_usage_summary()` returns `usage_by_source["codex"]` non-zero and `usage_by_model["gpt-5.2-codex"]` (or whatever model the fixture uses) non-zero.
   - Expected: this test FAILS before the fix.

2. **Trace the failure** to one of:
   - The `sessions.source` column not propagating to `token_events` rows (re-read `lifecycle.py:541` — confirm `session_source` actually reads the right column).
   - `SessionTokenTracker.get_usage_summary()` filtering or normalizing source values (e.g., grouping all non-`claude` rows into something hidden).
   - Model name attribution being dropped at the parser → TokenEvent boundary (Codex models stored as NULL → "unknown" in the route).
   - Backfill issue: token_events table actually has no codex rows (current data: `SELECT source, COUNT(*) FROM token_events GROUP BY source` returns `claude|9364`). Confirm whether real Codex sessions at runtime DO emit token_events but the existing data is from a pre-fix era — if so, the fix lands forward-only and the validation criteria below will exercise it on a fresh Codex session.

3. **Apply the narrow fix** indicated by step 2. Most likely one of:
   - Wire missing source/model fields in the lifecycle insertion path.
   - Remove a `WHERE source = 'claude'` (or equivalent) filter in the aggregator.
   - Ensure Codex/Gemini sessions trigger transcript processing at all (verify the lifecycle actually picks them up — check `_process_session_transcript`'s source dispatch).

4. **Backfill is OUT OF SCOPE** for this commit. The fix must work for new sessions; existing pre-fix data stays as-is. Note this in the PR description.

**Validation criteria**:
- Failing test from step 1 turns green after the fix.
- Run a new Codex session against any cloud GPT model. After it completes a turn, `curl -s http://localhost:60887/api/admin/usage | jq '.by_source'` includes `codex` with non-zero tokens.
- `sqlite3 ~/.gobby/gobby-hub.db "SELECT source, model, SUM(input_tokens) FROM token_events GROUP BY source, model"` shows non-zero rows for `codex` (and `gemini` if a Gemini session ran), with model names attributed (no `unknown` for new rows).
- DevTools: dashboard `UsageCard.tsx` lists at least one non-Claude row (e.g. `gpt-5.4`, `gpt-5.2-codex`).

### 2.2 Fix Token Efficiency chart green-tooltip-shows-0 bug [category: code]

Target: `web/src/components/dashboard/TokenEfficiencyCard.tsx`

`TokenEfficiencyCard.tsx` lines 148–163 stack two `<Area>`s with `stackId="tokens"`. Recharts stacked Areas report stacked-endpoint values in tooltip payload, so the upper series ("Tokens Saved", green) shows the offset (often 0 in idle buckets) instead of its true magnitude.

Fix: drop `stackId="tokens"` from both Areas so they render independently. Saved and Spent are not additive halves of one whole; overlapping rendering is more honest.

```tsx
<Area
  type="monotone"
  dataKey="tokens_spent"
  name="tokens_spent"
  stroke="#3b82f6"
  fill="rgba(59,130,246,0.2)"
/>
<Area
  type="monotone"
  dataKey="tokens_saved"
  name="tokens_saved"
  stroke="#22c55e"
  fill="rgba(34,197,94,0.2)"
/>
```

Optional follow-up if visual overlap looks bad: switch the lower series to `<Line>` instead of `<Area>` so the upper Area sits cleanly on top of a baseline line. Defer to Chrome DevTools verification.

**Validation criteria**: Hover over green spikes in the chart. Tooltip "Saved" value matches the visible spike magnitude (non-zero where the line is non-zero). Cross-check by comparing the tooltip value to a `tokens_saved` value from the live `/api/admin/tokens/timeseries` Network response (e.g. `curl -s 'http://localhost:60887/api/admin/tokens/timeseries?hours=24&granularity=1h' | jq '.buckets[0]'`) — they should match for the same bucket timestamp.

### 2.3 Remove GranularityToggle; derive granularity from hours [category: refactor]

Target: `web/src/components/dashboard/TokenEfficiencyCard.tsx`, `web/src/components/dashboard/GranularityToggle.tsx` (delete)

The 30m/1h/1d granularity selector is redundant with the page-level `TimeRangePills` (1h/6h/12h/24h/7d/30d/All). Derive granularity from the `hours` prop and delete the local toggle.

Edits to `TokenEfficiencyCard.tsx`:

- Remove `useState` for granularity (line 79).
- Remove `import { GranularityToggle }` (line 18).
- Compute granularity inline: `const granularity: TimeSeriesGranularity = hours <= 6 ? '30m' : hours <= 168 ? '1h' : '1d'`.
- Remove the `<GranularityToggle ... />` element from the `action` prop (lines 113–116).
- The `tokenEventsEnabled && modelBreakdown.length > 0` block at line 179 stays — it gates the per-model breakdown, not the toggle.

After this, `gcode search-content "GranularityToggle"` returns only the file itself. Delete `web/src/components/dashboard/GranularityToggle.tsx`.

**Validation criteria**: GranularityToggle file is deleted (`ls` confirms). `gcode search "GranularityToggle"` returns no usages. Page-level `TimeRangePills` still works; switching from 1h → 7d → All re-buckets the chart sensibly without a separate selector. No console errors.

---

## Phase 3: Watching panel + transcript

**Goal**: Visual fixes plus the Codex Protocol-tag leak (with a real fixture) and the wrap/scroll-on-click UX issues in the Watching panel.

### 3.1 Sessions filter dropdown collapsed-trigger blue [category: code]

Target: the dropdown trigger in the activity-panel header that switches between "Sessions / Tasks / Plans / Changes / Files / A2UI / Canvas / Pipelines" — confirm location with `gcode search "ActivityPanel tab dropdown"` and grep `'Tasks ▼'` (its accessible label per the live page snapshot at uid `_313`).

Match the project-selector active-state blue (`bg-accent text-accent-foreground` from `ProjectSelector.tsx:91`) on the COLLAPSED dropdown trigger button only. Expanded menu items keep their current styling.

Apply `bg-accent text-accent-foreground` to the trigger's className. Confirm by inspecting the project selector's "gobby" pill styling in DevTools, then matching the same hex/CSS-var on the dropdown trigger.

**Validation criteria**: Collapsed Sessions filter dropdown trigger has the same blue background and text color as the `ProjectSelector.tsx` "gobby" active pill (DevTools computed `background-color` matches). Open the dropdown — menu items display in their previous (non-blue) style. Switch the filter to "Tasks" — the trigger remains blue.

### 3.2 Codex transcript Protocol-tag leak — capture fixture, fix at `splitProtocolContent` [category: code]

Targets:
- `web/src/components/chat/protocolContent.ts` — primary: this is where `splitProtocolContent` extracts `<system_instructions>…</system_instructions>` blocks from raw text into the collapsed Protocol segments. The leak means the splitter doesn't match this body.
- `web/src/components/chat/Markdown.tsx` — secondary: the `blockProtocolTagRe` here is the fallback for any leftover text that bypasses `splitProtocolContent`. May or may not need updating depending on diagnosis.
- `src/gobby/sessions/transcript_renderer.py` — backend mirror: server-side renderer has its own `_PROTOCOL_TOOL_TAGS` and pattern. If the leak is rooted in tag-naming or pattern mismatch, this file may need a parallel fix to keep frontend/backend rendering consistent. Flag for review during implementation; only edit if necessary.

User-visible symptom: Codex sessions in the Watching panel render `<system_instructions>`-style content as raw markdown text outside the collapsed "Protocol" block (live-confirmed via Chrome DevTools MCP at viewport 375×800, screenshot for Drawbridge task `e49d8f0c-9907-43f0-9728-73a2c24f1ee3`).

**Capture the offending body as a committed fixture (mandatory first step):**

1. Read `~/.codex/sessions/2026/04/24/rollout-2026-04-24T01-49-53-019dbe40-89d2-7e73-a654-326b775e6035.jsonl` (confirmed contains `<permissions instructions>`, `<INSTRUCTIONS>`, and `skills_instructions`).
2. Extract the full content of the offending `developer`-role message that produces the leak. Use `jq` to filter `select(.payload.role == "developer")` and pull the `content[].text` body.
3. Commit the body as `web/src/components/chat/__tests__/fixtures/codex-protocol-leak.txt` (the file becomes the test fixture for steps below — DO NOT skip; the plan author confirmed the JSONL exists locally but it is not in the repo, so without this commit the bug is not reproducible).

**Diagnose and fix:**

4. Write a failing Vitest in `web/src/components/chat/__tests__/protocolContent.test.ts`:
   ```ts
   import { splitProtocolContent } from '../protocolContent'
   import fixture from './fixtures/codex-protocol-leak.txt?raw'

   it('collapses leaked Codex system_instructions into a single protocol segment', () => {
     const segments = splitProtocolContent(fixture)
     const visible = segments.filter(s => s.kind === 'text').map(s => s.text).join('\n')
     // Body contains "request_user_input availability" only inside the protocol block;
     // visible text must not contain that phrase.
     expect(visible).not.toContain('request_user_input availability')
     expect(segments.filter(s => s.kind === 'protocol').length).toBeGreaterThanOrEqual(1)
   })
   ```
5. Run the test — it should fail. The error message tells you whether the leak originates in `splitProtocolContent` (most likely — the visible text contains the protocol body) or somewhere downstream. If `splitProtocolContent` is the culprit:
   - The non-greedy regex in the splitter terminates early when the body contains a triple-backtick code fence with `</tag>` text inside, OR when nested same-name tags appear.
   - Switch the offending regex to a balanced tokenizer: walk forward from each `<tag…>` opening, count `<tag>` / `</tag>` depth (ignoring text inside `` `…` `` inline code and triple-backtick fences), close on depth 0.
6. If the leak is downstream (i.e. `splitProtocolContent` returns the right segments but `Markdown.tsx` re-renders the protocol body as visible text), apply the same balanced-tokenizer fix to `blockProtocolTagRe` in `Markdown.tsx`.
7. Audit `src/gobby/sessions/transcript_renderer.py` `_PROTOCOL_TOOL_TAG_PATTERN` for the same vulnerability. If the backend renderer has the equivalent regex and the same issue, fix it in parallel (separate logical commit if it touches Python — keep frontend and backend consistent).

**Validation criteria**:
- New fixture file `web/src/components/chat/__tests__/fixtures/codex-protocol-leak.txt` exists.
- Vitest in `protocolContent.test.ts` (and `Markdown.test.tsx` if §3.2.6 fired) passes — visible text does not leak protocol content.
- Open a Codex session (use one from `~/.codex/sessions/2026/04/24/`) in the Watching panel via Chrome DevTools at `http://localhost:60889/#chat`. The `system_instructions` / `permissions instructions` content stays inside the collapsed "Protocol" block. Expanding the block reveals the content; collapsing hides it.
- If §3.2.7 fired, backend renderer test (e.g. `tests/sessions/test_transcript_renderer.py`) also asserts the captured body produces a single tool_chain block.

### 3.3 Wrap tool-call args/result instead of horizontal scroll [category: code] (depends: refactor #12917)

Target: `web/src/components/chat/ToolCallCard.tsx` (currently 1008 lines — see refactor caveat below) and `web/src/components/chat/UnknownBlockCard.tsx`.

Find the `<pre>` blocks that render tool-call args and result JSON. Per `gcode search-content "overflow-x"`, both files have `overflow-x` on JSON-display blocks. Replace `overflow-x-auto` with `whitespace-pre-wrap break-words` Tailwind utilities (or `break-all` if URLs/long identifiers overflow). Keep the `<pre>` tag for monospace and indentation.

Don't strip `overflow-x` from layout containers (panels, tabs); only from JSON/code display blocks.

Refactor caveat:

`ToolCallCard.tsx` is 1008 lines, just over the CLAUDE.md 1000-line cap. This task declares `depends: #12917` (refactor of the chat monoliths). If #12917 hasn't merged when this task is picked up, the implementing agent extracts a `JsonBlock` component (the `<pre>` + className logic for args/result JSON; ~30–60 lines including any related copy-button or syntax-highlight wrapper) into a new file `web/src/components/chat/JsonBlock.tsx` as part of the same commit. The refactor must bring `ToolCallCard.tsx` below 1000 lines.

**Validation criteria**:
- Open a tool call with long args/result JSON (e.g. a `gobby-tasks.list_tasks` call with a long response). No horizontal scrollbar appears in the args/result block. Long lines wrap.
- Resize the activity panel narrow (DevTools responsive mode at 600px width) — wrap holds.
- `gcode search-content "overflow-x"` in `ToolCallCard.tsx` returns no matches in JSON-display contexts.
- `wc -l web/src/components/chat/ToolCallCard.tsx` returns < 1000.

### 3.4 Scroll Watching panel to bottom on session click [category: code]

Target: `web/src/components/activity/SessionsTab.tsx`

The auto-scroll `useEffect` (around lines 462–469) currently fires only on `[chatMessages.length, contentMode]` changes. When the user clicks a different session in the sidebar, `chatMessages` changes asynchronously (after the new session's transcript loads) and the effect may not scroll for the user's expectation.

Fix: add the watched session id to the dependency array so the effect fires the moment the user picks a new session, and use `behavior: "auto"` for instant jump (smooth-scroll across a long transcript is slow):

```ts
useEffect(() => {
  if (contentMode === "transcript") {
    const messagesEnd = messagesEndRef.current;
    if (messagesEnd?.scrollIntoView) {
      messagesEnd.scrollIntoView({ behavior: "auto" });
    }
  }
}, [chatMessages.length, contentMode, watchedSessionId]); // add watchedSessionId
```

`watchedSessionId` is whatever prop or state holds the currently-watched session — confirm by reading `SessionsTabProps` lines 26–36 and use the actual prop name (likely `selectedSessionId` or similar).

**Validation criteria**: Click between several sessions in the sidebar. Each click jumps the Watching panel to the most recent message instantly. Scroll up in a session, click a different session, click back — landing on the most recent message both times. Streaming new messages still scrolls to bottom (the `chatMessages.length` dep still fires).

---

## Phase 4: Mobile activity panel auto-close (depends: Phase 3)

**Goal**: Fix the user-reported bug where clicking the activity-panel toggle on mobile opens the panel for ~6ms then auto-closes it.

### 4.1 Scope `ChatPage.tsx` mobile auto-close to viewport transitions only [category: code]

Target: `web/src/components/chat/ChatPage.tsx` (specifically lines 169–172 — the `useEffect` that auto-closes the panel on every isPinned change while isMobile is true).

User-reported bug: "the activity panel is flickering and disappearing when i click the button on the chat ui — but only on mobile."

**Diagnosis confirmed (Round 1 adversary + live verification):**

`web/src/components/chat/ChatPage.tsx:169-172` contains:

```tsx
useEffect(() => {
  if (isMobile && isPinned) {
    setIsPinned(false);
  }
}, [isMobile, isPinned, setIsPinned]);
```

Because `isPinned` is in the deps, the effect fires every time the user toggles `isPinned` to true on mobile, immediately setting it back to false. This matches the live MutationObserver evidence from Chrome DevTools at viewport 375×800:

```text
t=0      ms  "Show activity panel"   (closed)
t=64879  ms  "Hide activity panel"   (toggle ran — opened)
t=64885  ms  "Show activity panel"   (effect fired — closed again, 5.8ms later)
```

The intent of the effect is "auto-close the panel when the viewport shrinks from desktop to mobile" — which only needs to fire on the `isMobile` *transition*, not on every `isPinned` change. The previous draft's "click-outside racing" theory was wrong; the only `mousedown` listener in `ActivityPanel.tsx` is for the mobile tab menu, not the panel itself.

**Important — preserve intentional auto-closes elsewhere:**

`ChatPage.tsx` also contains intentional `if (isMobile && isPinned) setIsPinned(false)` calls at lines ~201–210 and ~618–628. Those are inside callbacks (e.g., after attaching to a session, after approving a plan) — those should auto-close on mobile. Do NOT touch those callbacks. Only the standalone `useEffect` at lines 169–172 is wrong.

Fix: gate the auto-close on the desktop→mobile transition by tracking previous `isMobile` via a ref. Read `isPinned` via ref so it's not in the effect's deps:

```tsx
const prevIsMobileRef = useRef(isMobile);
const isPinnedRef = useRef(isPinned);
useEffect(() => {
  isPinnedRef.current = isPinned;
}, [isPinned]);

useEffect(() => {
  // Auto-close the pinned panel only on the desktop → mobile transition,
  // not on every user-initiated isPinned change.
  if (!prevIsMobileRef.current && isMobile && isPinnedRef.current) {
    setIsPinned(false);
  }
  prevIsMobileRef.current = isMobile;
}, [isMobile, setIsPinned]);
```

Test target: the integration test must mount enough of `ChatPage` to reproduce the cascade — an `ActivityPanel`-only render cannot trigger this effect because the effect lives in `ChatPage`. Use `web/src/components/chat/__tests__/ChatPage.test.tsx` (already exists per `gcode search-content "useActivityPanel"` showing the file's mock setup). Add a new `it()` block that:

- Mocks `useIsMobile` to return `true` from initial render.
- Renders `ChatPage` with the mocked `useActivityPanel` providing `isPinned=false`, `setIsPinned=spy`.
- Fires `userEvent.click` on the toggle button (`aria-label="Show activity panel"`).
- After the click resolves, asserts `setIsPinned` was called once with `true`, then NOT called again with `false` within `await waitFor(() => ..., { timeout: 100 })`.
- Asserts the toggle's `aria-label` ends as "Hide activity panel" and stays there.

Then add a second `it()` covering the desktop→mobile transition: starts with `useIsMobile=false` and `isPinned=true`, then re-renders with `useIsMobile=true`, asserts `setIsPinned(false)` IS called (so the intentional auto-close behavior is preserved when the user resizes the window).

**Validation criteria**:
- Open `http://localhost:60889/#chat` in Chrome DevTools mobile emulation (viewport `375x800x2,mobile,touch`, `isMobile=true`).
- Click the "Show activity panel" button in the chat input area. The button label flips to "Hide activity panel" and stays. Panel becomes visible and stays.
- Click again → label flips back to "Show activity panel", panel hides cleanly.
- Repeat 5× — no flicker, no disappearance.
- Re-run the diagnostic MutationObserver script (the one used to confirm the bug originally): timeline shows 1 flip per click, not 2.
- Resize browser from desktop (1100px) to mobile (375px) with the panel pinned — the panel auto-closes (intentional behavior preserved).
- New `ChatPage.test.tsx` `it()` blocks pass.
- Same manual test at 768px viewport (tablet) — no regression.

---

## Task Mapping

| Plan Item | Drawbridge ID / Gobby ref | Status |
|-----------|---------------------------|--------|
| 1.1 | drawbridge `9533…2d63` (Make textarea one line, smaller send) | open |
| 1.2 | (refactor — no Drawbridge ref) | open |
| 1.3 | drawbridge `df52…d997` (chips not highlighted text) | open |
| 1.4 | drawbridge `8651…d6ac` (font size match Sessions) | open |
| 1.5 | (user ask — Sessions tab LOCAL chip; no Drawbridge ref) | open |
| 1.6 | gobby-task **#11199** (LOCAL chip on agent cards), depends: refactor #12917 | open |
| 2.1 | drawbridge `82e5…1604` (usage stats only Claude — attribution fix) | open |
| 2.2 | drawbridge `dd0a…1b28` (chart hover green=0) | open |
| 2.3 | drawbridge `e53b…f561` (granularity tied to time range) | open |
| 3.1 | drawbridge `ef1c…df97` (dropdown blue) | open |
| 3.2 | drawbridge `e49d…1ee3` (Codex Protocol leak — fixture-driven) | open |
| 3.3 | drawbridge `70c1…4d80` (wrap not scroll), depends: refactor #12917 | open |
| 3.4 | drawbridge `aab7…ac30` (scroll bottom on session click) | open |
| 4.1 | (user-reported, no Drawbridge ref) — `ChatPage.tsx:169-172` fix | open |

## Verification Checklist

```text
Plan Verification:
✓ No explicit test tasks found (TDD wrappers will be auto-inserted)
✓ Dependency tree is valid:
    - 1.3, 1.5 depend on 1.2 (chip class)
    - 1.6 depends on 1.2 (chip class) and refactor #12917 (file size)
    - 3.3 depends on refactor #12917 (file size)
    - Phase 4 depends on Phase 3 (sequencing only — no shared files)
    - No cycles
✓ Categories assigned correctly (code/refactor only)
✓ Phase headings use canonical `## Phase N: Name` form
✓ Task sections are self-contained: every section names exact files, line numbers, code snippets, and validation steps
```

Ready for review.
