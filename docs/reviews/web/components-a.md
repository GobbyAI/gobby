# Review: web components A (`__tests__` → `chat`)

- **Scope:** `web/src/components/` subdirectories, first alphabetical half: `__tests__/`, `activity/`, `agents/`, `app/`, `auth/`, `chat/` (~46k lines incl. tests).
  **Split boundary:** components-a covers `__tests__` through `chat` alphabetically; **components-b covers `code/` through `workflows/` plus the root-level files** (`ConfigurationPage.*`, `CronJobsPage.tsx`, `FilesPage.tsx`, `ProjectSelector.tsx`, `Settings.tsx`).
- **Reviewer:** Claude Fable 5 — 6-agent fan-out (chat input/palette, chat messages/tool-cards, chat page/panels/styles, activity sessions, activity tasks, activity misc + agents/app/auth) + synthesizer verification of every Blocker against source
- **Commit / branch:** `0c6b371f4` / `0.5.0`
- **Summary:** 6 Blocker · 46 Important · 22 Nit — the token layer and newer surfaces are genuinely strong (no XSS surface anywhere in web/src, the retired `--color-agent` hue is fully gone, status dots use exemplary glyph+lightness deutan-safe design), but the defect mass concentrates in three classes: fetch lifecycles without abort/stale guards, loading-state-rendered-as-unmount that destroys in-progress user edits, and legacy chat CSS that predates the token ladder.

Verified-clean notes worth recording: no `dangerouslySetInnerHTML`/`rehype-raw` anywhere in `web/src` — model/tool output renders through react-markdown v9 with default URL sanitization; `grep -rn "color-agent" web/src` returns nothing; reduced-motion is globally enforced (`web/src/styles/base.css:80-89`); tool status icons are shape-differentiated with `aria-label`s and survive grayscale.

## Findings

### [BLOCKER] CommandPalette activation index diverges from rendered order — Enter/click/Backspace can act on (or delete) the wrong session

- **Where:** `web/src/components/chat/CommandPalette.tsx:83-88` (`allItems` built in `seq_num`-desc order), `:147-167` (`sessionIndexMap` built in Today→Week→Older bucket order), `:106-118` (`handleSelect` indexes `allItems`), `:134-141` (Backspace delete uses `allItems[selectedIndex]`)
- **Failure mode:** `allItems` is ordered by `seq_num` descending, but the rendered list and `sessionIndexMap` are ordered by recency bucket first. The orders diverge whenever an old-numbered session has a recent `updated_at` (resumed session) or a high-numbered session is stale: the map gives the visually highlighted row one index, but `allItems[thatIndex]` is a different session. Enter/click opens the wrong session, and the Backspace shortcut deletes a *different* web_chat session than the one highlighted. Existing tests never catch this: `CommandPalette.test.tsx:94-119` uses sessions sharing one `updated_at` (single bucket), so both orderings coincide.
- **Why it matters:** Wrong-target activation and wrong-target deletion — a data-loss path on the app's primary navigation surface.
- **Minimal fix:** Build `allItems` from the bucketed order (today→week→older→actions→navigate) in one memo and derive both render order and index map from it; add a test with sessions spanning buckets where seq order ≠ recency order.
- **Confidence:** high — synthesizer-verified both orderings in source.

### [BLOCKER] FilesTab serves read-failure placeholder as editable file content — Save overwrites the real file

- **Where:** `web/src/components/activity/FilesTab.tsx:207-215` (`openFile`), `:510-518` (Edit button), `:326-340` (`handleSaveEdit`)
- **Failure mode:** On a failed read, `openFile` resolves `{ content: 'Failed to load file' }` (`:208`) or catches into `setFileContent('Error loading file')` (`:214`), and seeds `setEditContent(content)` (`:212`) — rendered indistinguishably from real file content. The Edit button (`:512`) copies `fileContent` into the editor, and `handleSaveEdit` (`:329-333`) POSTs it to `/api/files/write`, replacing the real file's contents with the error string. A transient daemon restart or network blip plus Edit→Save destroys the file.
- **Why it matters:** Genuine data-loss path; error state and content state share one variable with no flag, and the UI actively offers Save on it.
- **Minimal fix:** Track read errors in a separate `fileError` state; render an error panel and disable/hide Edit when set; never seed `editContent` from an error.
- **Confidence:** high — synthesizer-verified the placeholder resolution, the `setEditContent` seed, and the unguarded write.

### [BLOCKER] FilesTab keeps all state across project switches — stale tree, raced fetches, cross-project write hazard

- **Where:** `web/src/components/activity/FilesTab.tsx:154-173` (only `gitStatus` + `rootEntries` refetch on `projectId` change), `:127` + `:175-190` (`childrenMap` cache, short-circuit `childrenMap.has(dirPath)` at `:176`), `:200-216` (`openFile`, no abort), `:326-340` (`handleSaveEdit`); mounted un-keyed via `ActivityPanel.tsx:311` / `ChatPage.tsx:187`
- **Failure mode:** When `projectId` changes, `childrenMap`, `expandedPaths`, `selectedFile`, `editContent`, `isEditing`, and selection persist. (1) `loadChildren` short-circuits on the cache, so a dir with the same relative path in the new project (e.g. `src/`) displays the *old project's* children. (2) If the user was editing when they switched, Save posts `project_id=<new>` with the old project's path and content — if that path exists in the new project, its file is overwritten with the other project's text. (3) None of the three fetches (git-status `:157`, root tree `:168`, `openFile` `:207`) is aborted or sequence-guarded, so an A→B switch can land A's slow response last and display project-A data under project B. No test in `__tests__/FilesTab.test.tsx` covers project switching.
- **Why it matters:** Cross-project data bleed in the UI and a concrete cross-project file-corruption path.
- **Minimal fix:** Reset all per-project state in the `projectId` effect (or render `<FilesTab key={projectId}>`), and add AbortController/stale-guards to the three fetches.
- **Confidence:** high — synthesizer-verified: no effect resets the cached state on project change.

### [BLOCKER] Task-ref range with default (empty) roles hides every session — client predicate contradicts the server default

- **Where:** `web/src/components/activity/sessionsFilters.ts:48` (default `taskRefRoles: new Set()`), `:151-167` (predicate) vs `src/gobby/storage/sessions/_query.py:100` (`roles = list(task_ref_roles) if task_ref_roles else ["claimed"]`)
- **Failure mode:** The dropdown's role checkboxes start unchecked. When a user types only a Task-ref min/max (the natural first action), the server defaults empty roles to `["claimed"]` and returns matches — but the client predicate loops `for (const role of filters.taskRefRoles)` over zero roles, leaves `anyMatch = false`, and returns `false` for **every** session. `getVisibleActivitySessions` (`activitySessionVisibility.ts:47`) re-applies this predicate on top of the server-filtered list, so the tab renders "No sessions match these filters" despite server matches. The serializer test (`sessionsFilters.test.ts:118-126`) pins that empty roles are *omitted* on the wire (server-default semantics); the predicate tests only exercise explicitly-set roles — the divergent case has no test.
- **Why it matters:** The default path of the Task-ref filter is fully broken.
- **Minimal fix:** Mirror the backend in `matchesSessionsFilters`: `const roles = filters.taskRefRoles.size > 0 ? filters.taskRefRoles : (["claimed"] as const)`; add a predicate test for range-set/roles-empty.
- **Confidence:** high — synthesizer-verified both sides of the divergence.

### [BLOCKER] TasksTab: every background refetch unmounts the whole tab, destroying in-progress edits, focus, and scroll

- **Where:** `web/src/components/activity/TasksTab.tsx:208` (`setLoading(true)` unconditionally in `fetchTasks`), `:848-850` (`if (loading) return <ActivityPanelEmpty body="Loading tasks…" />`), `:345-350` (every WS task event schedules a debounced `fetchTasks` 500ms later)
- **Failure mode:** Any `task_event` for any task in the project → debounced `fetchTasks` → `loading=true` → the early return replaces the entire tab (tree, detail panel, inline editors, open quick menu) with the loading placeholder. The user's own PATCH echo also triggers this. Unmounting `TaskTextAreaField` runs its cleanup (`TaskFieldEditors.tsx:141`), which clears a pending debounce *without committing* — uncommitted keystrokes are silently dropped. Tree scroll position and keyboard focus reset too.
- **Why it matters:** In an active system (agents/dispatcher constantly updating tasks) the tab flashes "Loading tasks…" repeatedly and typing into Description/Validation criteria is lossy — direct user-edit data loss, the exact class the optimistic-edit machinery was built to prevent.
- **Minimal fix:** Only early-return when `loading && tasks.length === 0`; keep the tree mounted during refetches (`TasksTabList` already accepts `isLoading`/`aria-busy` and currently can never receive `true`).
- **Confidence:** high — synthesizer-verified the unconditional `setLoading(true)`, the early return, and the WS-debounce path.

### [BLOCKER] TasksTab: detail pane refetches and unmounts its editors on every `tasks` identity change

- **Where:** `web/src/components/activity/TasksTab.tsx:371-396` (detail-fetch effect with deps `[selectedTaskId, tasks]`, sets `detailLoading=true`), `:911-915` (`detailLoading ? <p>Loading...</p> : <TasksTabDetailPanel …>` — unmount, not overlay), `:307-311` (`tasks` gets a new array identity on every WS event even when the event targets a different task)
- **Failure mode:** The effect re-runs on every `tasks` identity change → GET `/api/tasks/{selected}` → `setDetailLoading(true)` → the entire detail panel (including `TaskTextField`/`TaskTextAreaField`/`TaskTagsField`) unmounts. Drafts and pending debounced commits are destroyed (`TaskFieldEditors.tsx:134-141` cleanup clears the timer without committing). Even the user's own commit of one field triggers a refetch that can transiently clobber the optimistic value with a pre-PATCH GET response.
- **Why it matters:** Editing a task while *any* task in the project is being updated is effectively impossible; also triples request volume (effect GET + WS-path GET at `:326` + debounced list refetch per event).
- **Minimal fix:** Drop `tasks` from the effect deps (use the existing `tasksRef` for the `cached` lookup, as the WS path already does at `:332`); render the panel during refreshes (only show "Loading..." when `taskDetail === null`) so editors stay mounted and the prop-reconcile path they were designed for handles updates.
- **Confidence:** high — synthesizer-verified the deps array, the `setDetailLoading(true)`, and the unmount ternary.

### [IMPORTANT] Enter submits mid-IME-composition in the chat textarea

- **Where:** `web/src/components/chat/ChatInput.tsx:481, 498, 500` (all Enter branches in `handleKeyDown`)
- **Failure mode:** No `e.nativeEvent.isComposing` check on any Enter branch. For CJK/IME users, pressing Enter to *commit a composition* submits the half-composed message (or activates a palette item). The same directory has the correct guard — `PlanApprovalActions.tsx:82` — so this is intra-repo drift.
- **Minimal fix:** Early-return from `handleKeyDown` when `e.nativeEvent.isComposing` (or `e.keyCode === 229` for Safari).
- **Confidence:** high.

### [IMPORTANT] Swapped-session provider change: unhandled rejection, state committed before the await, stale captures across the confirm dialog

- **Where:** `web/src/components/chat/useChatPageProviderState.ts:195-244` (uncaught `await chat.continueSessionInChat(...)` at `:221`); fire-and-forget call site `useChatInputProviderSelection.ts:108-110` (`=> void` callback), wired via `ChatMainColumn.tsx:243-246`
- **Failure mode:** (1) The only call site discards the promise and, unlike sibling `handleResumeViewedSession` (`.catch` at `:179-181`), there is no try/catch — a daemon failure becomes an unhandled rejection with zero user feedback on a destructive, confirm-gated action. (2) Provider/model/reasoning prefs are committed at `:216-220` *before* the await, so on failure the UI no longer matches the still-running terminal session. (3) `chat.viewingSessionId`/`viewingMeta` are captured before `await confirm(...)` and can be stale by the time the user confirms.
- **Minimal fix:** try/catch mirroring `:179-181`; move the pref writes after a successful resume; re-validate `viewingSessionId` after confirm resolves.
- **Confidence:** high.

### [IMPORTANT] Hard-coded red recording ring on the chartreuse mic button — red-on-green, token bypass

- **Where:** `web/src/components/chat/ChatInput.tsx:701` (`ring-red-500/70` while `mic-recording`, on a `bg-accent` button, `:699`)
- **Failure mode:** Tailwind default `red-500` bypasses the token system (locked error token is hue-350 magenta-pink, `web/src/styles/tokens.css:65`) and puts a red ring directly around a green/chartreuse fill — the explicitly banned red-on-green adjacency. Under deutan vision the recording state's color cue collapses. This is the only `red-*` utility use in the repo (`grep -rn "red-500" web/src --include="*.tsx"`).
- **Minimal fix:** `ring-[var(--color-error)]/70` (hue 350 is deutan-distinguishable from hue 125), keeping `animate-pulse` (already neutralized under reduced-motion).
- **Confidence:** high.

### [IMPORTANT] Both input palettes lack listbox/combobox semantics; Cmd-K palette has no focus trap, aria-modal, or focus restore

- **Where:** Slash palette: `web/src/components/chat/ChatInput.tsx:731-760` (plain divs, no `role`, no `aria-activedescendant`/`aria-expanded`); Cmd-K: `CommandPalette.tsx:174` (`role="dialog"` without `aria-modal`), `:327/:355` (`role="option"` with no `listbox` ancestor — invalid ARIA), `:120-144` (Escape only on the input; Tab walks out of the open dialog), `:106-118` (no focus restore on close)
- **Failure mode:** Screen readers announce nothing as the highlighted option changes; `option` without `listbox` violates the ARIA spec; focus can leave the dialog while it visually blocks the page; close lands focus on `document.body`.
- **Minimal fix:** listbox/option ids + `aria-activedescendant` on both; `aria-modal="true"`, a minimal Tab trap, and restore-focus-on-close for Cmd-K.
- **Confidence:** high.

### [IMPORTANT] Latched voice recording loses its only stop affordance once the user types

- **Where:** `web/src/components/chat/ChatInput.tsx:538-546` (`resolvePrimaryButtonKind` returns `'send'` when `hasInput`, even while `isRecording`); `ChatInputVoiceControls.tsx:63,111` (toolbar mic disabled while recording)
- **Failure mode:** Tap-to-latch starts recording; typing any character flips the primary button to Send while the toolbar mic stays disabled. Recording continues with no visible stop control; the only escape (window-Escape, `:568-578`) *cancels* — discarding the recording — rather than stopping/transcribing.
- **Minimal fix:** Keep `mic-recording` as the primary kind while `isRecording` regardless of `hasInput`, or surface a stop control in the voice toolbar during recording.
- **Confidence:** med — intent may be "voice fills the textarea, then send", but the stop-vs-cancel asymmetry remains.

### [IMPORTANT] Tool-card expand/collapse is mouse-only — no keyboard parity

- **Where:** `web/src/components/chat/ToolCallCard.tsx:364-366` (single-card header), `:746-748` (group header)
- **Failure mode:** Both headers are plain divs with `onClick` — no `role="button"`, `tabIndex`, or `onKeyDown`. Keyboard users cannot reach arguments, results, or error bodies at all. The correct pattern ships in the same directory (`ThinkingBlock.tsx:24-35`: role/tabIndex/aria-expanded/Enter+Space).
- **Minimal fix:** Apply the ThinkingBlock pattern or use a real `<button>` row.
- **Confidence:** high.

### [IMPORTANT] `text-destructive` used as text color — resolves to a surface-band token, unreadable in both themes

- **Where:** `web/src/components/chat/BranchIndicator.tsx:210`; `web/src/components/activity/WikiTab.tsx:199, 271`; `web/src/components/activity/WikiSourceRemovalDialog.tsx:68`
- **Failure mode:** `--color-destructive` is the *surface* token (`oklch(28% 0.10 350)` dark / `oklch(95% 0.05 350)` light, `tokens.css:63/291`); on `--bg-primary` (15% / 96.5%) that's ~1.2–1.5:1 contrast — error messages (branch-checkout failure, wiki removal errors, the Remove button label) are near-invisible. Sibling code uses the correct `text-destructive-foreground` (`ToolCallCard.tsx:139`; the same dialog's Confirm button at `:98`).
- **Minimal fix:** `text-destructive-foreground` (or `text-error`) at the four sites. (Found independently by two reviewers.)
- **Confidence:** high.

### [IMPORTANT] Blind casts on protocol payloads crash per-message rendering (boundary catches it; the message is lost)

- **Where:** `web/src/components/chat/ToolCallCard.tsx:485-507` (`parseAnsweredValues` casts `result.content as Record<string, string>`), `:527-528` (`answer.split(', ')`), `:187,197-198` + `ToolCallCard.helpers.ts:185` (`arguments?.file_path as string` → `.split()`)
- **Failure mode:** Non-string answer values (arrays from multi-select) or non-string `file_path` throw TypeError; `MessageErrorBoundary` replaces the whole message with a "Render error" card — content loss. Payloads come from four different CLI transports; types are not validation. The codebase already guards correctly elsewhere (`ToolCallCard.tsx:75,95`).
- **Minimal fix:** Coerce with `typeof v === 'string' ? v : JSON.stringify(v)`; guard `typeof args.file_path === 'string'` at the three read sites.
- **Confidence:** high (mechanism), med (frequency).

### [IMPORTANT] Approval card buttons stay live after a decision — double-submit / contradictory-decision race

- **Where:** `web/src/components/chat/ToolCallCard.tsx:420-427` (`handleDecision` sets no submitted state), `:466-476` (buttons)
- **Failure mode:** The card only disappears when the next `tool_status` WS event flips `call.status`; in that window the user can click Approve twice, or Approve then Reject, sending contradictory decisions for the same `tool_call_id`. The sibling `AskUserQuestionCard` guards exactly this with `submitted` state (`:514,582,594`).
- **Minimal fix:** Add a `decided` state on successful send; disable all three buttons.
- **Confidence:** high.

### [IMPORTANT] Streaming tool-status updates drop late `arguments`

- **Where:** `web/src/hooks/useChat/handlers.ts:263-270` (direct upstream of the cards)
- **Failure mode:** Updates spread `{ ...existing, status, result, error }` — `status.arguments` is never merged. If the initial event carried partial/missing arguments and a later event carries the full set, the card permanently renders without them; summaries and Write/Edit/Plan renderers degrade to the JSON fallback.
- **Minimal fix:** `arguments: status.arguments ?? existing.arguments`.
- **Confidence:** med.

### [IMPORTANT] `formatJsonForDisplay` corrupts literal backslashes in displayed/copyable JSON

- **Where:** `web/src/components/chat/ToolResultBlocks.tsx:97-109` (`serialized.replace(/\\n/g, '\n').replace(/\\t/g, '\t')` on the serialized text)
- **Failure mode:** The unescape can't distinguish an escaped newline from an escaped backslash followed by `n` — `C:\new` renders as `C:\` + newline + `ew`, regex sources mangle, and the block becomes syntactically invalid JSON so copy-paste is corrupted. Error payloads route here too (`ToolCallCard.tsx:140-141`).
- **Minimal fix:** Unescape per string value during traversal (parse, then custom stringify emitting real newlines inside values).
- **Confidence:** high.

### [IMPORTANT] `unwrapMcpResultEnvelope` silently discards all content blocks after the first

- **Where:** `web/src/components/chat/ToolCallCard.helpers.ts:511-516`
- **Failure mode:** Only `content[0].text` becomes the body; `content[1..]` (more text blocks, images, resources) are dropped and excluded from `meta` (the whole `content` key is destructured out). The card shows "success" with a truncated result and no truncation indicator.
- **Minimal fix:** Join all text blocks for `primary`; surface a `+N more blocks` marker.
- **Confidence:** med.

### [IMPORTANT] message.css is two near-duplicate rule copies; the shadowed copy references undefined tokens

- **Where:** `web/src/components/chat/styles/message.css:1-151` vs `:153-330`
- **Failure mode:** The entire markdown ruleset appears twice with conflicting token choices; the first block uses `var(--bg-code)` and `var(--bg-muted)` — **neither defined** in `tokens.css` (only `--code-bg`/`--bg-tertiary` exist). The later block wins at equal specificity, so rendering is correct by accident; edits to the first block silently no-op, and a reorder/partial-delete flips code blocks to `background: transparent` via invalid-var fallback.
- **Minimal fix:** Delete lines 1-151 (the second copy is the superset with valid tokens).
- **Confidence:** high.

### [IMPORTANT] Zero component tests for the two interactive tool cards (approval + AskUserQuestion)

- **Where:** `web/src/components/chat/__tests__/` — interactive-card grep matches only pure-helper suites
- **Failure mode:** No render/interaction tests for decision dispatch, disconnect path, option toggling, `__other__` free-text, submit guard, or malformed `parseAnsweredValues` payloads — the only components in the slice that *send* data back to the agent ship regressions blind (the double-submit gap above is exactly that).
- **Minimal fix:** Add `ToolCallCard.interactive.test.tsx` covering approve/reject dispatch + disabled-after-decision, disconnect error, multi-select + Other payload shape, array-valued answers.
- **Confidence:** high.

### [IMPORTANT] Wiki tab is selectable but rejected by the persistence validator

- **Where:** `web/src/components/activity/useActivityPanel.ts:8-18` (`VALID_TABS` omits `'wiki'`) vs `ActivityPanelTabs.tsx:3-13, 68-78`
- **Failure mode:** Selecting Wiki persists `'wiki'`; on reload `normalizeStoredTab` rejects it and bounces to Sessions. (Found independently by two reviewers.)
- **Minimal fix:** Derive `VALID_TABS` from `ACTIVITY_PANEL_TABS.map(t => t.id)` so the lists can't drift.
- **Confidence:** high.

### [IMPORTANT] `integrations` missing from APP_VALID_TABS and APP_NAV_PAGES — reload on #integrations bounces to chat; palette can't navigate there

- **Where:** `web/src/components/app/appNavigation.tsx:22-43` (allowlist + palette pages omit it), `:60` (nav item exists); consumed at `App.tsx:208, 916`
- **Failure mode:** Same enum-drift class as the wiki bug, on the app-level router: deep links and reload state silently lost for a first-class page; the command palette's navigate section (`useAppCommandPalette.ts:216`) can't reach it.
- **Minimal fix:** Add `"integrations"` to both (or derive from the nav registry).
- **Confidence:** high.

### [IMPORTANT] Running-agents poll converts HTTP errors into "no agents" and trusts the payload shape (crash vector)

- **Where:** `web/src/components/activity/SessionsTab.tsx:200-213` (`response.ok ? await response.json() : { agents: [] }`; `setAgents(data.agents ?? data ?? [])`; `setFetchError(null)`), crash site `:281` (`agents.reduce`)
- **Failure mode:** A 500/503 renders as zero agents with `fetchError` cleared — fabricated empty state on the primary live-monitoring surface, refreshed every 5s. And `?? data` accepts any truthy JSON: a 200 with a non-array body is stored, then `agents.reduce` throws and takes down the tab (no boundary above it). (Found independently by two reviewers.)
- **Minimal fix:** On `!response.ok` keep prior agents + set `fetchError`; `Array.isArray(data?.agents)` before `setAgents` (mirror `parseChangedFiles`, `useFileChanges.ts:44-59`).
- **Confidence:** high.

### [IMPORTANT] ResumeSessionModal fetch has no cancellation — out-of-order responses clobber newer state; refetch churn while open

- **Where:** `web/src/components/chat/ResumeSessionModal.tsx:28-67`
- **Failure mode:** Toggling "Subagents" refires the fetch with no AbortController/stale flag — a slow earlier response can land last and show the wrong filter state. Deps include the `sessions` prop whose identity changes on every catalog refresh, so the modal refetches repeatedly while open.
- **Minimal fix:** AbortController keyed on `[isOpen, showSubagents]`; read `sessions` through a ref.
- **Confidence:** high.

### [IMPORTANT] One accidental keystroke permanently deletes a session with no confirmation or undo

- **Where:** `web/src/components/chat/useChatPageCommandPalette.ts:58-65` → `CommandPalette.tsx:134-140` → `App.tsx:531-557`
- **Failure mode:** Backspace with an empty palette query deletes the highlighted web-chat session immediately — no confirm (ChatPage already wires `useConfirmDialog` for provider switches), no undo; users habitually hit Backspace believing the search box has text. Compounded by the index-divergence Blocker: the deleted session may not even be the highlighted one.
- **Minimal fix:** Route the palette delete through the existing `confirm()` affordance, or ship soft-delete/undo first.
- **Confidence:** med-high.

### [IMPORTANT] Activity-panel search suppresses the global focus ring

- **Where:** `web/src/components/chat/styles/activity-panel.css:336-339` (`outline: none` + 1px border-color shift, unlayered so it beats the `@layer base` ring in `accessibility.css:5-16`)
- **Failure mode:** Keyboard focus on the Sessions/Tasks/Traces search inputs is signaled only by a 1px border hue shift — far below the mandated 2px brand-accent ring.
- **Minimal fix:** `.activity-panel-search:focus-visible { outline: 2px solid var(--accent); outline-offset: 1px; }`.
- **Confidence:** med-high.

### [IMPORTANT] Swap to a web-chat session silently no-ops after already parking the current chat

- **Where:** `web/src/components/chat/useChatPageSessionRouting.ts:95-115`
- **Failure mode:** `handleSwapSession` parks the current session and dismisses the mobile panel *before* checking the target exists in `conversations.sessions` — a different store than the session catalog feeding the list. On miss it returns silently: sessions tab flashes open, nothing else happens.
- **Minimal fix:** Look up the target first; only park + dismiss when the swap will happen.
- **Confidence:** med.

### [IMPORTANT] Corrupt custom-date in localStorage crashes the whole app on every load

- **Where:** `web/src/components/activity/sessionsFilters.ts:283-284` (deserializer accepts any string), `:89-98` (`new Date("junkT00:00:00.000Z").toISOString()` throws on the render path)
- **Failure mode:** With `datePreset: "custom"` and a non-date string, `resolveDateRange` throws RangeError inside `matchesSessionsFilters` → `AppErrorBoundary` trips → App re-hydrates the same payload on reload (`App.tsx:288`) → wedged until storage is manually cleared. Violates the function's own contract comment ("a corrupt storage entry should never wedge the app", `:238-241`); every enum field is validated *except* the only two that can throw.
- **Minimal fix:** Validate `^\d{4}-\d{2}-\d{2}$` + `Number.isFinite(new Date(...).getTime())` in the deserializer; return null bounds from `resolveDateRange` on invalid dates.
- **Confidence:** high (mechanism); trigger requires storage corruption/version drift.

### [IMPORTANT] Custom date range filters on UTC days, not the user's local days

- **Where:** `web/src/components/activity/sessionsFilters.ts:87-99` (`${date}T00:00:00.000Z`)
- **Failure mode:** The native date input picks a *local* calendar day, but bounds expand at UTC midnight. A UTC-7 user picking "to Jun 9" excludes sessions created Jun 9 17:00–23:59 local; "from Jun 9" includes Jun 8 17:00+ local.
- **Minimal fix:** Expand via local-midnight construction (`new Date(y, m-1, d)`) then `.toISOString()` for both bounds.
- **Confidence:** high.

### [IMPORTANT] Live refresh and search transients clobber the watched-session selection and its persisted id

- **Where:** `web/src/components/activity/SessionsTab.tsx:364-370, 419-428, 439-449`
- **Failure mode:** Whenever the watched session drops out of `entries` — a mid-typing debounce window, a catalog refresh flipping its status bucket, a transient zero-entry state — selection silently reassigns to `entries[0]` and the persist effect overwrites `gobby-watching-session-id`. The user's transcript switches to a different session mid-read; clearing the search does not restore it.
- **Minimal fix:** Keep (or null) `selectedSessionId` when `stillPresent` goes false; persist only on explicit `handleSelect`.
- **Confidence:** med-high.

### [IMPORTANT] "Send Context" always fails when no web-chat session is active

- **Where:** `web/src/components/activity/SessionInteractionModal.tsx:162` (`from_session: fromSessionId ?? ""`)
- **Failure mode:** With no active chat, `from_session: ""` reaches `resolve_session_reference`, which raises `ValueError("Empty session reference")` (`src/gobby/storage/session_resolution.py:49-50`) — the backend's SessionContext default only applies when the field is `None` (`agent_messaging.py:137-148`). Every attempt fails with a cryptic error.
- **Minimal fix:** Omit `from_session` when undefined; disable the menu item with a tooltip when no source session exists.
- **Confidence:** high.

### [IMPORTANT] Session context menu is not keyboard-dismissable and has no menu semantics

- **Where:** `web/src/components/activity/SessionsTab.tsx:590-595, 904-950`
- **Failure mode:** Closes only via window/backdrop click — no Escape, no `role="menu"`, no focus move/restore; renders at the container end so Tab walks the whole list before reaching the items. "Expire Session" (destructive) lives here; a keyboard-only user can open the menu but cannot close it without activating something.
- **Minimal fix:** Focus first item on open; Escape → close + restore; menu roles with arrow-key nav.
- **Confidence:** high.

### [IMPORTANT] Filter dropdown declares `role="dialog"`/`aria-modal` but manages no focus

- **Where:** `web/src/components/activity/SessionsFilterDropdown.tsx:171-177` (dead `panelRef` at `:85`)
- **Failure mode:** Mobile renders a centered `aria-modal` panel without moving, trapping, or restoring focus — AT users are told a modal is open while focus stays behind the full-viewport overlay.
- **Minimal fix:** Focus into `panelRef` on mount, restore on close, trap Tab for the modal variant (the repo has `useDialogFocus.ts`).
- **Confidence:** high.

### [IMPORTANT] Build/Stop/Resume on the selected task blanks the detail pane to "Task not found"

- **Where:** `web/src/components/activity/TasksTab.tsx:580-587` (`applyRawTaskUpdate` treats `null` as "clear detail"), `:651-652`, `:709-756` (build operations deliberately resolve `null`)
- **Failure mode:** `null` is overloaded — "no payload returned" vs "task gone". Running any build lifecycle action on the selected task flashes "Task not found" until the follow-up refetch repopulates.
- **Minimal fix:** Only call `applyRawTaskUpdate` when `rawTask !== null`.
- **Confidence:** high.

### [IMPORTANT] Any WS event for the selected task silently swallows an in-flight PATCH failure

- **Where:** `web/src/components/activity/TasksTab.tsx:317-321` (reconcile on *every* event), `useTaskInlineEdit.ts:118-125` (reconcile bumps all generations + clears errors), `:151-160` (stale-generation failure → no rollback, no error)
- **Failure mode:** An unrelated `task_updated` (stage heartbeat) arrives while a PATCH is in flight; reconcile bumps the generation; the PATCH then fails and the catch path returns early — no banner, no rollback; the next refetch restores the old value. The edit silently evaporates. The hook's test suite pins this as desired but conflates "WS carried truth for this field" with "any event touched this task".
- **Minimal fix:** Surface the error even on stale-generation rejection (skip only the rollback), or scope reconcile to fields present in the WS payload.
- **Confidence:** med.

### [IMPORTANT] Transient fetch failure silently wipes the entire task list

- **Where:** `web/src/components/activity/TasksTab.tsx:228` (`if (!response.ok) return []`), `:246-248` (`.catch(... setTasks([]))`)
- **Failure mode:** A single failed refetch (daemon restart) replaces a populated list with `[]`, clears selection, and shows the misleading "Tasks appear here as they are created" empty state with no error anywhere — repeatedly, given the 500ms-debounced refetch loop.
- **Minimal fix:** Keep previous `tasks` on failure + non-blocking error indicator.
- **Confidence:** high.

### [IMPORTANT] Detail pane bar shows the previous task's ref while the new selection loads

- **Where:** `web/src/components/activity/TasksTab.tsx:543` (`headerRef = taskDetail?.ref ?? selectedTaskSummary?.ref`); effect never clears `taskDetail` on selection change
- **Failure mode:** Click task B while A is shown: the bar renders "Task #A" until B's fetch resolves — wrong identity at the moment the user confirms what they opened.
- **Minimal fix:** Prefer `selectedTaskSummary?.ref`, or clear `taskDetail` on selection change.
- **Confidence:** high.

### [IMPORTANT] No virtualization and non-memoized rows on a 500+ item tree that re-renders per WS event

- **Where:** `web/src/components/activity/TasksTabList.tsx:120-135` (plain map), `TaskTreeRow.tsx:74` (no `memo`), fetch limit 500 + ancestors (`TasksTab.tsx:234`)
- **Failure mode:** Up to ~520 DOM rows re-render (with per-row state derivation) on every `tasks`/selection/pending change — continuous under automation. Compounds the refetch-churn Blockers.
- **Minimal fix:** `memo(TaskTreeRow)` now; windowing as the real fix.
- **Confidence:** med.

### [IMPORTANT] Missing load-bearing test: draft survival across a live refresh

- **Where:** `web/src/components/activity/__tests__/TasksTab.events.test.tsx`; `__tests__/TaskFieldEditors.test.tsx`
- **Failure mode:** No test types into a detail editor, fires a WS event/refetch, and asserts the draft survives — precisely the regression class both TasksTab Blockers live in; the suite is green while the behavior is broken.
- **Minimal fix:** Events-suite case: select task → type (no blur) → dispatch `task_updated` for another task + run the debounced refetch (fake timers) → assert the textarea holds the draft and one PATCH fires.
- **Confidence:** high.

### [IMPORTANT] PipelinesTab 3s poll wipes pagination, desyncs offset, and races filter changes

- **Where:** `web/src/components/activity/PipelinesTab.tsx:107-117, 51-73, 86-93`
- **Failure mode:** While any execution runs, the interval refetches with no offset and `setExecutions(fetched)` — discarding every Load More page each 3s; `offset` isn't reset, so the next Load More skips pages/duplicates. Poll fetches carry no AbortSignal; an old-filter response can land after a filter change and show wrong rows.
- **Minimal fix:** Poll with `offset=0&limit=executions.length` (or merge by id); reset `offset` on non-append fetches; route polls through the same abort/stale guard.
- **Confidence:** high.

### [IMPORTANT] PipelinesTab detail fetch has no stale guard — detail pane can show a different execution than selected

- **Where:** `web/src/components/activity/PipelinesTab.tsx:96-105, 119-122, 127-150`
- **Failure mode:** `fetchDetail(id)` sets `detailExec` unconditionally; rapid selection changes / auto-select racing a click / the 3s poll resolve out of order, last writer wins; render never checks `detailExec.id === selectedId`.
- **Minimal fix:** Request-id guard (like `schemaRequestIdRef`) or only set when `data.id === selectedIdRef.current`.
- **Confidence:** med.

### [IMPORTANT] ActivityMcpTab tool execution result can attach to the wrong tool

- **Where:** `web/src/components/activity/ActivityMcpTab.tsx:254-274` (unguarded), vs `:160-176` (schema fetch *is* guarded)
- **Failure mode:** Execute a slow tool, click another tool; when the stale call resolves, its result renders under the newly selected tool's "Result" section — misattributed output presented as authoritative.
- **Minimal fix:** Capture selection/request-id before the await; discard on mismatch.
- **Confidence:** med-high.

### [IMPORTANT] FilesTab root-level delete/rename/move/duplicate never updates the visible tree

- **Where:** `web/src/components/activity/FilesTab.tsx:242-247, 272-274, 292-294, 322-323` (refresh writes `childrenMap['']`) vs `:464-471` (root renders from `rootEntries`)
- **Failure mode:** For root entries, handlers invalidate `childrenMap['']`, but the root renders from `rootEntries`, never refetched — a deleted root file stays (clickable, then errors); a renamed one keeps its old name indefinitely.
- **Minimal fix:** When `parentPath === ''`, refetch the root tree into `rootEntries`.
- **Confidence:** high.

### [IMPORTANT] File tree and agent step cards are mouse-only; destructive actions live solely in a right-click menu

- **Where:** `web/src/components/activity/FilesTab.tsx:374-378, 407-417` (plain `div onClick`), `:557-575` (menu reachable only via `onContextMenu`); `web/src/components/agents/AgentStepsEditor.tsx:377-384`
- **Failure mode:** Keyboard users cannot expand folders, open files, or reach Rename/Move/Delete/Add-to-chat at all; agent step cards can't be expanded. `ActivityMcpTab` shows the correct pattern (`<button>` rows, keyboard-navigable menu).
- **Minimal fix:** `role="treeitem"`/buttons with Enter/Space; a focus-revealed per-row actions button opening the same menu.
- **Confidence:** high.

### [IMPORTANT] LoginPage inputs disable the focus ring with inline `outline: 'none'`

- **Where:** `web/src/components/auth/LoginPage.tsx:146` (applied at `:45, :57`)
- **Failure mode:** Inline style beats the `@layer base` focus treatment; username/password fields show no focus indicator and no replacement — on the first surface every authenticated user touches.
- **Minimal fix:** Drop `outline: 'none'` or add an explicit `:focus-visible` treatment via a class.
- **Confidence:** high.

### [IMPORTANT] AgentPortfolioPage refresh button renders the literal text `↻`

- **Where:** `web/src/components/agents/AgentPortfolioPage.tsx:526-533`
- **Failure mode:** The glyph is raw JSX text; JSX text nodes don't process JS escapes, so the button displays the six characters `↻` instead of ↻.
- **Minimal fix:** `{'↻'}` — or reuse the shared `RefreshGlyph` SVG (`ActivityActionsContext.tsx:22`).
- **Confidence:** high.

### [IMPORTANT] AgentPortfolioPage task-breakdown shows "Escalated" twice with conflicting styling

- **Where:** `web/src/components/agents/AgentPortfolioPage.tsx:243-254`
- **Failure mode:** Two adjacent rows both labeled "Escalated", both rendering `tasksEscalated.length`, one `--danger` and one `--warn` — a copy-paste artifact (the second presumably meant validation failures, computed at `:420-426`).
- **Minimal fix:** Delete one row or repoint the second at the intended metric.
- **Confidence:** high.

### [IMPORTANT] AgentStepsEditor "Advanced" JSON textareas silently drop in-progress edits

- **Where:** `web/src/components/agents/AgentStepsEditor.tsx:304-322`
- **Failure mode:** The textarea is controlled by `JSON.stringify(step[key])` but `onChange` only commits when the text parses as a JSON array — almost every intermediate keystroke is invalid, so any re-render snaps the textarea back and typed JSON vanishes; non-array valid JSON is silently ignored.
- **Minimal fix:** Hold raw text in local state, parse on blur/apply, show an invalid-JSON indicator.
- **Confidence:** med-high.

### [IMPORTANT] AgentRulesEditor applies server-rejected selector updates locally; rule/variable failures are fully silent

- **Where:** `web/src/components/agents/AgentRulesEditor.tsx:141-200` (on `!res.ok` falls through to the optimistic apply), `:93-133`; same pattern `AgentVariablesEditor.tsx:43-69`
- **Failure mode:** A non-OK selector PATCH still applies the optimistic update — the chip appears changed while the server kept the old state (resurrects on reload). Rule/variable failures produce no UI change *and* no error.
- **Minimal fix:** Only fall back locally when there's no `definitionId`; surface errors (the MCP tab's `actionError` pattern exists).
- **Confidence:** med-high.

### [IMPORTANT] Renaming a step orphans transitions in other steps

- **Where:** `web/src/components/agents/AgentStepsEditor.tsx:399-407` (rename), `:251-262` (transition `to` options)
- **Failure mode:** Rename updates only the renamed step; other steps' `transitions[].to` keep the stale name — the dropdown shows an empty selection and the saved workflow references a nonexistent step. Invalid definition produced through normal UI use, no warning.
- **Minimal fix:** Map all steps' transitions old→new on rename.
- **Confidence:** med.

### [IMPORTANT] Touch targets below the 44×44 floor across the slice

- **Where:** `web/src/components/chat/ChatInput.tsx:792` (16×16 attachment-remove — below even WCAG 2.5.8's 24px), `:697-702` (36px primary send/mic/stop with no coarse-pointer floor; size baked into `__tests__/ChatInput.phase1-red.test.tsx:6-16`); `ToolCallCard.tsx:380-389` + `CodeBlockRenderers.tsx:70-86` (~23-26px artifact/copy buttons; the ToolCallCard one also lacks an aria-label); `web/src/components/chat/styles/sessions-tab.css:404-411` (`.session-more-btn`/`.task-more-btn` 20×20 — the documented "tap-reachable action surface on touch", `TasksTabList.tsx:28`); `web/src/components/tasks/task-execution.css:212-219` (24px expand toggle); `web/src/components/chat/styles/activity-panel.css:262-276` (30px mobile menu items); `FilterPrimitives.tsx:39` + `SessionsFilterDropdown.tsx:228-233` (12px checkboxes); `SessionsTab.tsx:857-859` (24px error dismiss)
- **Failure mode:** The design contract sets a 44×44 touch floor and the house pattern exists (`--control-row-height` promotes to 2.75rem on coarse pointers, `tokens.css:232-237`; `buttonVariants.ts:25-28` enforces `pointer-coarse:min-h-11`) — these hand-rolled controls opted out. They are the primary per-row/per-message actions on touch devices. (Found by four reviewers across the slice; merged.)
- **Minimal fix:** Keep small glyphs but promote hit areas via `@media (pointer: coarse)` padding/pseudo-elements or wrap in `.btn-icon`/`buttonVariants`; add the missing aria-label.
- **Confidence:** high.

### [IMPORTANT] Typography off the locked `--text-*` ladder; raw px ignores the user's font-size setting

- **Where:** `web/src/components/chat/ResumeSessionModal.tsx:95,112,121,140,144,167,190` (inline `fontSize: "12px"/"13px"/"14px"` — does not respond to the Settings slider at all); `web/src/components/chat/styles/layout.css:44,54,126,147,362,372,389,403,435,456,467` (`calc(var(--font-size-base) * 0.65/0.7/0.8/0.85/0.9)` — off-ladder multipliers); `web/src/components/chat/styles/sessions-tab.css:292,381,437,454` (same pattern); `web/src/components/activity/TracesTab.tsx:111,114,156` + `FileChangesTab.tsx:170` (`text-[10px]` fixed px, below the smallest sanctioned step; the existing `typographyLadder.test.ts` only audits CSS files so TSX literals escape it); `LoginPage.tsx:121-162` + `AppErrorBoundary.tsx:52-110` (fixed rem sizes)
- **Failure mode:** The ladder (`tokens.css:6-15`) is the single authoring site and the whole UI rescales through `--font-size-base`; these sites either drift from the locked scale or stop scaling entirely — a functional accessibility regression for users who raise the base size. (Found by four reviewers; merged.)
- **Minimal fix:** Snap to nearest ladder tokens (`--text-xs/sm/md/base`); extend the ladder test to scan TSX for `text-[Npx]` and inline `fontSize`.
- **Confidence:** high.

### [NIT] ~520 dead lines in input.css anchored to a zero-importer duplicate CommandPalette; orphaned MobileChatDrawer; 30–50% dead selectors in three more CSS files

- **Where:** `web/src/components/chat/styles/input.css:1-548` (most blocks; per-class greps returned zero TS/TSX consumers except `.command-name`, which resolves to `web/src/components/shared/CommandPalette.tsx` — itself imported nowhere); `web/src/components/chat/MobileChatDrawer.tsx` (entire 189-line component, zero importers); `web/src/components/chat/styles/sessions-tab.css:1-187, 239-308`; `styles/layout.css:1-76, 200-214, 309-314`; `styles/activity-panel.css:20-24, 53-110, 167-178`
- **Note:** The dead CSS masks regressions (the token-correct `.ptt-button.recording` styling shows the old surface used `--color-error` before the live path regressed to `ring-red-500`) and carries latent violations that will resurface if rewired (warning-hue identity dots, hover-only-reveal controls); the orphaned drawer still contains the unguarded delete handler; the duplicate `CommandPalette.tsx` name invites edits to the wrong file. Delete the component(s) and listed blocks. (Found by two reviewers; merged.)

### [NIT] ChatInput clears input without notifying `onInputChange`, desyncing parent autocomplete state

- **Where:** `web/src/components/chat/ChatInput.tsx:348, 373, 469` (`setInput('')` without `onInputChange?.('')`); parent state in `useColonAutocomplete.ts:75` via `App.tsx:661-666`
- **Note:** Harmless today (palette visibility also gates on local input) but a stale-state trap. Route clears through `handleChange('')`.

### [NIT] Dead identical `sub_item` branch; redundant provider refetch with unvalidated payload

- **Where:** `web/src/components/chat/ChatInput.tsx:484-491` (both branches call `handlePaletteSelect(selected)`); `useChatPageProviderState.ts:286-310` (refetch on every provider change though only the catch uses it; elements cast without validating `name`)
- **Note:** Collapse the branch; fetch once, filter `typeof p?.name === 'string'`.

### [NIT] Truncated comment block in input.css suggests a lost rule

- **Where:** `web/src/components/chat/styles/input.css:568-572`
- **Note:** Unclosed comment merges with the next one; the dangling text implies a Mode-row rule was deleted mid-comment. Close or remove it.

### [NIT] ToolApprovalCard "Approved"/"Rejected" badges are unreachable dead branches

- **Where:** `web/src/components/chat/ToolCallCard.tsx:431-438` vs gate at `:350-351` (card only renders when `status === 'pending_approval'`)
- **Note:** Either route completed/error approval calls to the card for historical replay or delete the branches.

### [NIT] MessageList omits `computeItemKey`; Virtuoso identity is index-based

- **Where:** `web/src/components/chat/MessageList.tsx:303-319`; contrast `WatchingTranscript.tsx:315`
- **Note:** Non-append changes (history prepend, session swap) force full remount/remeasure of visible rows. `computeItemKey={(_, m) => m.id}`.

### [NIT] `Footer` component identity churns on every streaming chunk

- **Where:** `web/src/components/chat/MessageList.tsx:216-230, 276-279` (callback depends on the `messages` array identity)
- **Note:** Footer subtree remounts per chunk, defeating the advertised memoization. Depend on derived primitives instead.

### [NIT] Badge `info` variant renders brand accent (hue 125) instead of the locked Info hue (250)

- **Where:** `web/src/components/chat/ui/Badge.tsx:14` (`info: 'bg-accent/20 text-accent'`); unused `--color-info` at `tokens.css:54`
- **Note:** Semantic drift against the locked palette; misleads the next person reaching for an info badge. Map to the info tokens.

### [NIT] BranchIndicator dropdown lacks Escape-close and real listbox keyboard semantics

- **Where:** `web/src/components/chat/BranchIndicator.tsx:76-85, 203-208`
- **Note:** `role="listbox"` advertises arrow-key behavior that doesn't exist; Escape doesn't dismiss; focus isn't managed. Implement or drop the role.

### [NIT] Plan-ready / artifact-event callbacks never unregistered on unmount

- **Where:** `web/src/components/chat/useChatPageArtifacts.ts:202-204, 216-218`; ref store `useChat/callbacksState.ts:37-39`
- **Note:** Stale closures invoked after ChatPage unmounts (events silently swallowed); effects also re-run every render via the unstable `chat` identity. Register null on cleanup (the `showPlanRef` cleanup at `:266-268` shows the pattern).

### [NIT] ActivityDropdown announces `role="menu"` semantics it doesn't implement

- **Where:** `web/src/components/activity/ActivityPanel.tsx:150-166, 235-260`
- **Note:** No arrow-key nav, no focus move/restore; mousedown-only outside-dismiss. Drop to a plain disclosure or implement roving focus.

### [NIT] `.session-modal-btn` uses `--bg-primary` as text on an accent fill

- **Where:** `web/src/components/chat/styles/sessions-tab.css:448-456`
- **Note:** Works only because bg-primary happens to invert with the accent across themes; the contract pairs `--accent` with `--accent-foreground`.

### [NIT] Interactive button nested inside a `role="button"` row

- **Where:** `web/src/components/activity/SessionsTab.tsx:705-757`
- **Note:** Invalid ARIA — some AT flattens the subtree and the kebab becomes unreachable. Restructure as listitem + inner activation button.

### [NIT] ResumeSessionModal lacks a Radix `DialogTitle`

- **Where:** `web/src/components/chat/ResumeSessionModal.tsx:90-94`
- **Note:** Unnamed dialog for screen readers + Radix warning. Wrap the heading in `DialogTitle` or pass `aria-label`.

### [NIT] `SessionsFilters.models` is a dead field kept alive only by tests

- **Where:** `web/src/components/activity/sessionsFilters.ts:27, 224, 256`
- **Note:** Never serialized, always `[]`, ignored by the predicate; no consumer repo-wide. Drop it.

### [NIT] Date-bound matching uses lexicographic string comparison against a format the backend doesn't emit

- **Where:** `web/src/components/activity/sessionsFilters.ts:170-175` (bounds `....000Z` vs backend `+00:00` isoformat, `storage/hub/postgres.py:376`)
- **Note:** `'+' < '.'` misorders equal-instant/sub-second cases; any precision change breaks silently. Compare epoch millis via the existing `parseTimestamp`.

### [NIT] Active custom date filter displays as "All"; an empty custom range still counts as an active filter

- **Where:** `web/src/components/activity/SessionsFilterDropdown.tsx:250, 271, 280`; `sessionsFilters.ts:63`
- **Note:** Segmented control highlights "All" while a custom range is applied; clearing both inputs leaves `datePreset: "custom"` inflating the funnel badge.

### [NIT] Polling fetches lack cancellation/ordering (agents poll, modal pane capture)

- **Where:** `web/src/components/activity/SessionsTab.tsx:200-227`; `SessionInteractionModal.tsx:118-152`
- **Note:** Stale responses can land out of order or after unmount; self-heals next tick. Request-epoch ref or AbortController in cleanup.

### [NIT] `any` escapes on untrusted envelopes

- **Where:** `web/src/components/activity/SessionInteractionModal.tsx:68, 159`; `web/src/components/activity/toolCallStatus.ts:1, 14`
- **Note:** Unchecked field access on MCP envelopes whose whole job is interpreting untrusted shapes. Type as `unknown` and narrow.

### [NIT] Context-menu geometry: hard-coded width disagrees with CSS; quick-menu y never clamped

- **Where:** `web/src/components/activity/SessionsTab.tsx:582-583` (`menuWidth = 160` vs CSS `min-width: 150px`, no max); `TasksTab.tsx:674-688` (x clamped, y not) with `TaskQuickMenu.tsx:67-71`
- **Note:** Long labels overlap the trigger; bottom-edge rows render the menu off-screen. Measure or clamp both axes.

### [NIT] TasksTab small drift cluster: dead `userSelectedRef`; `isLoading`/`aria-busy` can never be true + `aria-live` spam; stage dots always info-blue; `#737373` fallback; `TaskTagsField` blur commits without local set

- **Where:** `TasksTab.tsx:195,292,561,569,841` (write-only ref, verified repo-wide); `TasksTabList.tsx:106-107` (busy semantics never received; `aria-live="polite"` on hundreds of rows); `TasksTabFilters.tsx:96-100` + `taskNormalization.ts` (registry rows default `state: 'ready'` → every stage dot `--color-info`); `TasksTabModel.ts:208` (raw hex fallback); `TaskFieldEditors.tsx:338-345` (blur `commit(nextTags)` without `setTags(nextTags)`)
- **Note:** Each is one-line-ish; the `aria-live` move (to a small status region) matters most for AT users.

### [NIT] FilesTab/agents small drift cluster: untracked modifier class never matches; in-place sort of memoized array; unencoded `projectId` in query strings; `window.prompt` for Move; git status never refreshed after mutations; dead `stepNames` prop; emoji icons

- **Where:** `FilesTab.tsx:432` (`'??'.replace('?')` replaces only the first → `untracked?` matches nothing; the badge at `:582` does it right); `AgentPortfolioPage.tsx:457-476` (`result.sort()` mutates the other memo's value); `AgentRulesEditor.tsx:62` + `AgentSkillsEditor.tsx:30` (no `encodeURIComponent`); `FilesTab.tsx:280` (`window.prompt` bypasses the design-system dialog used for Delete); `FilesTab.tsx:154-161` (badges stale after save/rename/delete); `AgentStepsEditor.tsx:68` (declared, never read); `AgentPortfolioPage.tsx:72-78` (✨/📦/🤖 emoji instead of the SVG icon system)
- **Note:** Mechanical fixes; the emoji and prompt items are design-language consistency.

## Systemic patterns

1. **Fetch-then-set without abort/stale guards is the dominant defect class.** FilesTab (3 fetches), PipelinesTab (list/detail/poll), ActivityMcpTab (execution), SessionsTab (agents poll), ResumeSessionModal, agent editors. The house patterns exist — `schemaRequestIdRef`, `isStale` closures, AbortController, `removalPreviewRequestRef` — and are applied inconsistently, sometimes within one file. A shared `useAbortableFetch`/request-id helper would close the class.

2. **Loading-state-as-unmount destroys user state.** Both TasksTab Blockers and the detail-pane churn share one root: "loading" is rendered by *replacing* the live subtree instead of overlaying it. Policy fix: placeholders only when there is no data yet; keep children mounted otherwise.

3. **Two hand-maintained structures for one concept drift.** `allItems` vs `sessionIndexMap` (the CommandPalette Blocker), `ActivityTab` vs `VALID_TABS` (wiki), nav registry vs `APP_VALID_TABS` (integrations), session catalog vs `conversations.sessions` (silent swap no-op). Derive one from the other; drift becomes unrepresentable.

4. **Client re-implementations of server contracts diverge.** The sessions filter predicate (empty-roles Blocker, lexicographic timestamps, UTC-day bounds) re-implements `_query.py` semantics by hand. Either delete the client mirror for server-covered axes (the file's own docstring says this was the plan) or add client/server parity tests per axis.

5. **Error state conflated with content state; silent `!res.ok` paths.** FilesTab stores error strings where file content lives (Blocker); the agents poll fabricates "no agents" from a 500; TasksTab renders a truthful-looking empty list on fetch failure; agent editors apply rejected writes or do nothing silently.

6. **Casts-as-validation on transport payloads.** `as Record<string, string>`, `as string`, `as GobbyTaskDetail`, `data.agents ?? data` — external payloads (four CLI transports, internal APIs) are trusted at the type level. Small narrowing helpers (`asString`, `Array.isArray` gates, payload normalizers) would remove a crash class.

7. **Interactive affordances built on divs while the correct pattern ships nearby.** ToolCallCard headers vs `ThinkingBlock`; FilesTab tree vs `ActivityMcpTab`'s button rows; approval card vs `AskUserQuestionCard`'s submit guard; MessageList vs `WatchingTranscript`'s `computeItemKey`. The right primitive exists in-repo each time and wasn't propagated — review/lint enforcement, not new design work.

8. **Legacy chat CSS predates the token system; newer surfaces are exemplary.** Dead graveyards (input.css, sessions-tab.css, layout.css, activity-panel.css), off-ladder font multipliers, sub-44 targets, and `outline: none` overrides concentrate in older files, while recently built surfaces (status dots, pipelines rows, empty states, tokens.css itself) follow the contract closely. A one-pass migration of the legacy chat CSS would close most design-system findings at once.
