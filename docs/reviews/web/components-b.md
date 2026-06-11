# Review: web components B (`code` → `workflows` + root files)

- **Scope:** `web/src/components/` second alphabetical half — `code/`, `code-graph/`, `command-browser/`, `cron/`, `dashboard/`, `icons/`, `integrations/`, `mcp/`, `memory/`, `projects/`, `rules/`, `shared/`, `skills/`, `source-control/`, `tasks/`, `traces/`, `ui/`, `workflows/` — plus the root-level files (`ConfigurationPage.*`, `CronJobsPage.tsx`, `FilesPage.tsx`, `ProjectSelector.tsx`, `Settings.tsx`, `ValidationDetectionEditor.tsx`, `Sidebar.tsx`, `ThemeToggle.tsx`, `index.ts`). ~38k lines incl. tests.
  **Split boundary:** complement of `components-a.md` (which covers `__tests__/` through `chat/`).
- **Reviewer:** Claude Fable 5 — 6-agent fan-out (workflows pages/pipelines; workflows reports/agents/rules/profiles; tasks dir; root pages; dashboard/shared/memory/skills; remaining small dirs) + synthesizer verification of every Blocker against source
- **Commit / branch:** `758c93048` / `0.5.0`
- **Summary:** 15 Blocker · 64 Important · 32 Nit — this half is markedly worse than components-a: editor surfaces lose user data through ordinary flows (pipeline editor, rules editor, config form, channel edit), one real XSS, hooks that swallow every HTTP failure behind null returns that callers never check, and a ~5,600-line dead parallel UI in `tasks/`. The shared primitives and token layer remain solid; adoption at the leaf level is the failure.

Verified-clean notes: secrets handling in ConfigurationPage is clean (password inputs, masked round-trip stripped server-side, export bundles names only); cron next-run timestamps are tz-aware and parsed correctly; Tailwind `text-xs`-class sizes map onto the token ladder via `tailwind-theme.css`; no surviving `--color-agent` usage.

## Findings — Blockers

### [BLOCKER] PipelineEditor KeyValueEditor "+ Add" is a structural no-op — MCP/pipeline arguments and workflow variables cannot be created

- **Where:** `web/src/components/workflows/PipelineEditor.tsx:556-562` (`setArgs` drops empty keys), `:607-614`, `:639-645` (same for invoke_pipeline / activate_workflow), `:786-800` (KeyValueEditor is fully controlled — `pairs`/`onChange` props, no local state)
- **Failure mode:** "+ Add" appends `{key:'', value:''}` via `onChange`, but every setter filters `if (p.key.trim())` before storing; the stored object is unchanged, the derived `pairs` prop is unchanged, the new row never renders. Deadlock: a row requires a non-empty key; a key requires a row. Backspacing an existing key to empty also instantly deletes that row and its value mid-edit.
- **Why it matters:** The entire key/value editing surface for MCP step arguments and workflow variables is unusable; editing an existing key destroys data in transit.
- **Minimal fix:** Hold pairs in KeyValueEditor local state (seeded from props) and normalize empty keys out only at commit.
- **Confidence:** high — synthesizer-verified both halves (stateless editor + filtering setters).

### [BLOCKER] PipelineEditor: editing a Step ID collapses and remounts the step per keystroke

- **Where:** `web/src/components/workflows/PipelineEditor.tsx:410` (`isExpanded = expandedId === step.id`), `:413` (`key={step.id}`), `:466` (`onChange` writes `step.id`)
- **Failure mode:** One character typed into Step ID changes `step.id`; `expandedId` still holds the old id, so the body (including the focused input) unmounts; `key={step.id}` also remounts the row, dropping focus. Duplicate IDs (legal until save) produce duplicate React keys.
- **Minimal fix:** Key rows and track expansion by a stable synthetic id or index; or update `expandedId` inside `updateStep` on id change.
- **Confidence:** high — synthesizer-verified.

### [BLOCKER] PipelineEditor: Condition input eats spaces and Tools input eats commas while typing

- **Where:** `web/src/components/workflows/PipelineEditor.tsx:697-701` (`value={stripTemplateWrapper(...)}` + `onChange` trims and wraps), `:723-727` (tools `join(', ')` ↔ `split(',')→trim→filter(Boolean)`)
- **Failure mode:** Trailing space/comma is normalized away on every keystroke, so `inputs.mode == 'x'` and multi-entry tool lists can't be typed left-to-right; a literal `${{ x }}` typed into Condition gets double-wrapped on store while the display hides it.
- **Minimal fix:** Raw string in local state; parse/normalize on blur or save.
- **Confidence:** high — synthesizer-verified.

### [BLOCKER] PipelineEditor: failed save reports success — dirty flag cleared, unsaved-changes guard defeated

- **Where:** `web/src/components/workflows/PipelineEditor.tsx:330-345` (`await updateWorkflow(...); setDirty(false)`; catch is dead), `web/src/hooks/useWorkflows.ts:119-138` (`updateWorkflow` catches everything and returns `null` — never throws), `web/src/components/workflows/PipelinesTab.tsx:283-285` (`handleYamlSave` ignores the return, then closes the editor)
- **Failure mode:** The hook's contract is null-on-failure; both call sites treat "didn't throw" as success. On a 4xx/5xx the editor clears `dirty`, the close guard believes everything saved, and YAML save closes the sidebar discarding the user's edits. Server validation errors are invisible.
- **Minimal fix:** Check the return: `if (!await updateWorkflow(...)) throw`; keep the editor open and surface the error in `handleYamlSave`.
- **Confidence:** high — synthesizer-verified the null-only contract and the unconditional `setDirty(false)`/close.

### [BLOCKER] PipelinesTab: Form and YAML views are two desynced copies — switching discards edits; stale YAML can overwrite a fresh form save

- **Where:** `web/src/components/workflows/PipelinesTab.tsx:257-263` (`sidebarYaml` fetched once at card open), `:269-300` (`handleYamlSave` PUTs the snapshot wholesale, then closes), `:456-470` (view tabs switch with no dirty check or regeneration; form view conditionally unmounts)
- **Failure mode:** (a) Form→YAML unmounts PipelineEditor and its local state — unsaved edits destroyed (`editorRef.current?.isDirty` is consulted only on close). (b) `sidebarYaml` is never refreshed after a form save: form-edit → Save → switch to YAML → Save overwrites the just-saved changes with the open-time snapshot. (c) Whichever view saves last wins, silently.
- **Minimal fix:** Single source of truth — serialize form→YAML on view switch (parse back on the reverse, blocking on parse errors), or block switching while dirty.
- **Confidence:** high — synthesizer-verified.

### [BLOCKER] RulesTab save silently strips `match`, top-level `tools`, and multi-`effects` from rule definitions

- **Where:** `web/src/components/workflows/RulesTab.tsx:54-66` (`formToDefinition` rebuilds from 8 fields only), `:201-208` (sidebar YAML seeded from the same lossy subset), `src/gobby/servers/routes/rules.py:288-305` (PUT with `definition` does full-body replacement: `fields["definition_json"] = json.dumps(definition)`)
- **Failure mode:** The rule schema carries top-level `match`/`tools` and plural `effects` that the form doesn't model. Opening any rule that uses them and clicking Save — form *or* YAML view, since both are built from the subset — silently deletes the scoping and reports success. A scoped enforcement rule becomes unscoped (over-blocking). Duplicate and Download are lossy the same way.
- **Minimal fix:** Seed YAML from the full GET body; merge form fields over the fetched definition (`{...detailBody, ...formToDefinition(form)}`); add a round-trip test for unknown fields.
- **Confidence:** high — synthesizer-verified the 8-field rebuild and the server's full replace.

### [BLOCKER] XSS via unescaped HTML in knowledge-graph node tooltips

- **Where:** `web/src/components/memory/KnowledgeGraph.tsx:454-464` (`nodeLabel` returns raw HTML interpolating `e.name`, `e.entity_type`, and property values with zero escaping)
- **Failure mode:** force-graph renders `nodeLabel` strings via innerHTML in its tooltip. Entity names/properties come from FalkorDB knowledge extraction over arbitrary session/code content; an entity named `<img src=x onerror=...>` executes on hover in the dashboard origin, which holds an authenticated channel to the local daemon (task/skill/agent mutation APIs).
- **Minimal fix:** HTML-escape every interpolated value, or return a plain-text label.
- **Confidence:** high — synthesizer-verified the template string.

### [BLOCKER] Memory create/edit form closes as success when the server rejects the request

- **Where:** `web/src/components/memory/MemoryPage.tsx:203-228` (`handleSave` awaits then unconditionally `setShowForm(false)`; catch only fires on throw), `web/src/hooks/useMemory.ts:144-188` (`createMemory`/`updateMemory` swallow `!response.ok` and return null — never throw)
- **Failure mode:** HTTP 4xx/5xx → form closes, typed content discarded, no toast, nothing saved.
- **Minimal fix:** Throw on `!response.ok` in the hook (as `useSkills.createSkill` does), or check the return before closing.
- **Confidence:** high — synthesizer-verified both sides.

### [BLOCKER] "Restore Defaults" for skills hits a non-existent URL — complete silent no-op

- **Where:** `web/src/hooks/useSkills.ts:325` (`${getBaseUrl()}/skills/restore-defaults` — missing `/api`), backend mounted at `prefix="/api/skills"` + `@router.post("/restore-defaults")` (`src/gobby/servers/routes/skills.py:124, 279`)
- **Failure mode:** 404 falls into the silent `!response.ok` path; the toolbar button does nothing with zero feedback.
- **Minimal fix:** Fix the URL; toast when `restoreDefaults()` returns null.
- **Confidence:** high — synthesizer-verified URL and route prefix.

### [BLOCKER] Metrics charts render "Invalid Date" for every axis tick and tooltip

- **Where:** `web/src/components/dashboard/MetricsChartsCard.tsx:47-50` (`new Date(ts + 'Z')`), snapshots serialized tz-aware by the hub (`datetime.isoformat()` → `...+00:00`, `src/gobby/storage/hub/postgres.py:374-376`)
- **Failure mode:** `...+00:00Z` → Invalid Date → every tick/tooltip in all four metrics charts is "Invalid Date". SQLite-era leftover; `TokenEfficiencyCard.tsx:38-44` parses correctly. The test suite mocks `useMetrics`, so nothing catches it.
- **Minimal fix:** Append `'Z'` only when the string has no offset.
- **Confidence:** high — synthesizer-verified the `+ 'Z'` and the tz-aware serialization.

### [BLOCKER] Config form serves stale values after save/reset — re-saving silently reverts persisted settings

- **Where:** `web/src/components/ConfigurationPage.tsx:125-141` (`handleSave`/`handleReset` never refetch), `web/src/hooks/useConfiguration.ts:123, 177-184` (`fetchConfigValues` runs only on mount; `saveConfig`/`resetToDefaults` don't update `configValues`), `ConfigurationPage.tsx:905` (`ConfigFormTab` unmounts on tab switch and re-seeds from stale data)
- **Failure mode:** After a save, returning to the Config tab re-seeds `localValues` from pre-save data; the next Save PUTs the stale object, reverting the earlier save. "Reset to Defaults" clears the backend but the form keeps showing old values; a subsequent Save re-writes the pre-reset config — reset appears to do nothing, then is undone, all with success reported.
- **Minimal fix:** Refetch (or fold submitted values into `configValues`) after save and reset succeed.
- **Confidence:** high — synthesizer-verified the mount-only fetch and no-refetch handlers.

### [BLOCKER] FilesPage save failure bricks the editor and strands unsaved edits

- **Where:** `web/src/hooks/useFiles.ts:397-401` (save catch sets `error` on the tab), `web/src/components/FilesPage.tsx:270` (toolbar gated on `!activeFile.error`), `:544-545` (`file.error` renders a static error view before the editing branch)
- **Failure mode:** A failed `POST /api/files/write` (network blip, permission) replaces the editor and Save/Cancel with a static "Error:" panel; nothing clears `error`; the only exit is closing the tab, which discards `editContent`.
- **Minimal fix:** Keep `editing: true` with a separate `saveError` shown alongside the editor; allow retry.
- **Confidence:** high — synthesizer-verified the gating chain.

### [BLOCKER] Cron job creation omits `project_id` — jobs persist unscoped, vanish from the UI, keep running

- **Where:** `web/src/components/CronJobsPage.tsx:181-196` (`formValuesToCreateRequest` never sets it), `web/src/hooks/useCronJobs.ts:47-57` (`CreateCronJobRequest` lacks the field), backend default `src/gobby/servers/routes/cron.py:25` (`project_id: str = ""`), list filter `src/gobby/storage/cron.py:250-252`
- **Failure mode:** The page is project-scoped but create POSTs without `project_id` → persisted with `""`. The optimistic insert shows it once; any refetch drops it from the filtered list while the scheduler still runs it — invisible-but-active scheduled shell/agent jobs.
- **Minimal fix:** Add `project_id` to the request type and thread `projectId` through create.
- **Confidence:** high — synthesizer-verified all four links.

### [BLOCKER] Channel edit silently drops new secrets and wipes existing secret refs from config

- **Where:** `web/src/components/integrations/IntegrationsPage.tsx:263-268` (edit handler discards `_secrets`; sends config only), `web/src/hooks/useIntegrations.ts` (`updateChannel` has no secrets field — secrets exist only on the create path at `:89,100`), `src/gobby/servers/routes/communications.py:130` (`channel.config_json = request.config` — wholesale replace), secrets stored as `$secret:NAME` refs inside `config_json` (`src/gobby/communications/manager.py:573-589`)
- **Failure mode:** (1) The edit form's "Change" flow collects new secret values that are thrown away — user "changes" a bot token, gets success, token never updates. (2) Editing any plain field rebuilds `config` from non-secret fields and the server replaces `config_json` wholesale — deleting the `$secret:` refs and breaking the channel's credentials, success reported.
- **Minimal fix:** Thread secrets through `updateChannel`/`ChannelUpdateRequest` reusing the create-path ref logic; merge (not replace) `config_json` server-side or echo back untouched `$secret:` refs.
- **Confidence:** high — synthesizer-verified the discarded `_secrets` param and the wholesale server replace.

### [BLOCKER] ProjectSettings keeps the previous project's state — Save writes it to the newly selected project

- **Where:** `web/src/components/projects/ProjectSettings.tsx:54-68` (one-shot `useState(project.…)` init), `web/src/components/projects/ProjectsPage.tsx:86-92` (rendered without `key`), save target `ProjectsPage.tsx:65-71`
- **Failure mode:** Form state (`githubUrl`, `linearTeamId`, `approvalRules`, `validationDetection`, …) never resets when the `project` prop changes. Switch the selected project with Settings open → project A's values render under project B's heading; Save calls `updateProject(B.id, A's values)`. `approval_rules` gate tool auto-approval, so this is a security-adjacent wrong-target write.
- **Minimal fix:** `<ProjectSettings key={activeProject.id} …/>`.
- **Confidence:** high — synthesizer-verified the un-keyed render and one-shot init.

## Findings — Important

### Workflows surface

- **[IMPORTANT] YAML-view close bypasses the unsaved-changes guard** — `PipelinesTab.tsx:299-300` scopes the guard to `sidebarView === 'form'`; Cancel/Escape/overlay-click from the YAML view discards CodeMirror edits with no dirty tracking at all. (high)
- **[IMPORTANT] Top-level tab switch unmounts a dirty editor** — `WorkflowsPage.tsx:213-217, 343-358`; switching Pipelines→Agents destroys all unsaved form/YAML state; the page never consults the editor's dirty flag. (high)
- **[IMPORTANT] Out-of-order `exportYaml` can attach pipeline A's YAML to pipeline B** — `PipelinesTab.tsx:257-263` + `useWorkflows.ts:225-237`, no abort/sequence guard; worst case `handleYamlSave` writes A's content into B. (high)
- **[IMPORTANT] Invalid-YAML save fails silently** — `handleYamlSave` throws by design (`PipelinesTab.tsx:271-276`) but both call sites (`:478, :492`) invoke it bare → unhandled rejection, Save does nothing visibly. (high)
- **[IMPORTANT] Approve/Reject failures are unhandled rejections** — `PipelineExecutionsView.tsx:167-183` (try/finally, no catch) over throwing hooks (`usePipelineExecutions.ts:150,166`); an expired approval token looks like a dead button and can stall an execution. (high)
- **[IMPORTANT] StagesTab: single-click delete with no confirm; restore/delete failures unhandled; Restore shown for non-deleted stages** — `StagesTab.tsx:140-147, 380-394`. Stage registry rows drive the dispatch manifest daemon-wide. (high)
- **[IMPORTANT] StagesTab: selecting another stage silently discards draft edits** — `StagesTab.tsx:160-163, :198` (key-remount with no dirty tracking). (high)
- **[IMPORTANT] Step status is hue-only (timeline) or absent (cards); the fixing icon set is dead code** — `execution-utils.tsx:123-146, 196-199` (8px color-only dots, `aria-hidden`); `StepStatusIcon` (`:208`) exported, zero usages; completed-green vs waiting-amber fails grayscale/deutan. `PipelineStatusDot` (`:248-257`) shows the correct glyph+lightness delegation. (high)
- **[IMPORTANT] Step-type badge background is invalid CSS** — `PipelineEditor.tsx:420` `getTypeColor(type) + '22'` concatenates hex-alpha onto a `var()` reference → dropped declaration; the intended tinted chip never renders in any theme. Use `color-mix()` or `--step-type-*-soft` tokens. (high)
- **[IMPORTANT] Zero tests for PipelineEditor/PipelinesTab editing paths** — `workflows/__tests__/` covers AgentsTab/pagination/toolbar only; every Blocker above shipped through untested code. (high)
- **[IMPORTANT] RulesTab sidebar fetch race can save rule A's body into rule B** — `RulesTab.tsx:193-219, 223-238`; `openSidebar` sets `sidebarRule` synchronously then awaits detail with no token; stale resolution + Save = wrong-target write. (high)
- **[IMPORTANT] Rule enable/disable toggle: silent failure + no keyboard access** — `RulesTab.tsx:181-183, 536-544` ignores `toggleRule`'s boolean (`useRules.ts:88-107` returns false, never throws) and renders a `div onClick` with no `role="switch"`/tabIndex; ProfilesTab does it correctly (`ProfilesTab.tsx:244-259`). Found independently by two reviewers. (high)
- **[IMPORTANT] AgentsTab writes unsaved edit-form changes into the definitions cache** — `AgentsTab.tsx:268-320`; cancel (`:492`) doesn't refetch, so abandoned edits render as persisted and can be saved later. (high)
- **[IMPORTANT] Agent update payload can't clear workflows/steps; wholesale `workflows` rebuild drops unknown keys and wildcard skills** — `AgentsTab.payloads.ts:38-48, 108, 174-177` vs server merge semantics (`routes/agents.py:361, 394-411`): deleting the last rule/step is a silent no-op; any edit destroys `skill_selectors.exclude`/custom keys and filters `'*'`. (high)
- **[IMPORTANT] Duplicating an agent drops steps, blocked tools, sandbox, and lifecycle config** — `AgentsTab.payloads.ts:114-142`; the copy succeeds with weakened guardrails (`blocked_tools` gone); no test covers `buildDuplicateAgentBody`. (high)
- **[IMPORTANT] ProfilesTab: un-confirmed Delete; `save`/`action` failures are unhandled rejections** — `ProfilesTab.tsx:499-507, 368-380`; soft-delete keeps it sub-Blocker. (high)
- **[IMPORTANT] ProfilesTab editor holds a stale draft; Save writes stale fields back** — `ProfilesTab.tsx:266` (key = id, never changes), `:313`, `:336-349` (full-body PUT incl. `enabled`) — toggling enabled from the card then saving the open editor silently reverts the toggle. (med-high)
- **[IMPORTANT] ReportsPage approve/reject/cancel failures are console-only** — `ReportsPage.tsx:238-269` over throwing hooks; no error surface exists on the page. (high)
- **[IMPORTANT] RulesTab move/restore actions fail silently** — `RulesTab.tsx:312-349`: `if (res.ok)` with no else; 409/404 produce nothing. (high)
- **[IMPORTANT] RuleEditForm Form↔YAML tabs are two unsynced sources of truth** — `rules/RuleEditForm.tsx:221` picks `onYamlSave` vs `onSave` by visible tab; `sidebarYaml` is only set at open (`RulesTab.tsx:196,428-429`), so saving from the other tab silently discards the edits made in the first. Same class as the PipelinesTab Blocker, independent instance. (high)
- **[IMPORTANT] Custom mcp_call argument key input remounts per keystroke; key collisions silently merge** — `RuleEditForm.tsx:795-825` (`key={argName}` while the name is being edited; rename onto an existing key overwrites its value). (high)
- **[IMPORTANT] ExpressionBuilder parses `in`/`not in` before `==`/`!=`** — `rules/ExpressionBuilder.tsx:29-66`; `source == "not in scope"` decomposes wrongly and one touch of any builder field commits the corrupted string. (high mechanism, med frequency)

### Tasks directory

- **[IMPORTANT] ~80% of `tasks/` (25 of 31 files, ~5,600 lines) is unreachable dead code** — only `QuickCaptureTask`, `TaskCreateForm`, `TaskBadges`, `priorityGlyphPaths`, `taskModalStyles`, `task-execution.css` have live import chains (entry points: `App.tsx:37`, `activity/TaskTreeRow.tsx:3`, `activity/TasksTab.tsx:50`, `activity/useTaskActions.ts:3`). The dead set — launch dialogs, Gantt/dependency charts, comments, memories, audit log, oversight/permission panels — has passing tests that create false confidence. Notable defects waiting in the dead set, should any of it be rewired: placebo safety controls persisting to localStorage nothing reads (`OversightSelector.tsx:35-53`, `PermissionOverrides.tsx:27,93-97` — would be a Blocker live); LaunchAgentDialog spawning with a stale/wrong prompt (`:132-158, 177-186, 201`) and double-launch via backdrop close; TaskComments destroying drafts on failed POST (`:368-372`); Gantt grid/bar misalignment, DST off-by-one, drag-opens-task (`GanttChart.tsx:165-186, 53-55, 225-231`); "dependency" visualizations actually rendering parent/child hierarchy (`buildArrows` on `parent_task_id`); AuditLog fabricating entries from timestamps; `ready`/`needs_review` sharing one color in `TASK_STATE_COLORS` (`lib/taskState.ts:95-102` — this map IS live). Decide wire-in vs delete per component. (high)
- **[IMPORTANT] TaskCreateForm: backdrop click / Escape destroys a fully-typed form** — `TaskCreateForm.tsx:83-93, :144` + `useDialogFocus.ts:55-58`; no confirm, no draft retention. [live] (high)
- **[IMPORTANT] TaskCreateForm/QuickCaptureTask: create failures are console-only** — `TaskCreateForm.tsx:132-134`, `QuickCaptureTask.tsx:66-74`; form stays open with zero feedback. [live] (high)

### Root pages

- **[IMPORTANT] Array-typed config fields fall through to a plain text input — type corruption on edit** — `ConfigurationPage.SchemaField.tsx:113-141` has no array branch; `list[str]` fields render `String(arr)` and save a raw string → opaque 400 or uneditable fields. (high)
- **[IMPORTANT] FilesPage shows the previous file's diff after switching tabs** — `FilesPage.tsx:200-211`; `showDiff`/`diffContent` not reset on `activeFileIndex` change and no in-flight guard. (high)
- **[IMPORTANT] useFiles keys async updates by positional index across awaits** — `useFiles.ts:332-404`; closing a lower-index tab mid-save targets the wrong tab or none (stuck `saving: true`). `openFile` already shows the correct `projectId+path` identity. (high)
- **[IMPORTANT] Closing a dirty file tab discards edits with no confirmation** — `FilesPage.tsx:255-262` + `useFiles.ts:311-319`; the Cancel path confirms, the common gesture doesn't. (high)
- **[IMPORTANT] useCronJobs: no fetch aborts; selection survives project switch** — `useCronJobs.ts:98-133, 238-269`; cross-project list clobber and a detail panel that can Run Now/Delete a job from the previous project. (high)
- **[IMPORTANT] Cron error paths are dead code; invalid cron expressions accepted and never run** — `CronJobsPage.tsx:567-597` (alert branches unreachable — hooks catch internally), `:236-244` + `storage/cron.py:91-97` (typo'd expr → `next_run_at=None`, create succeeds, job never fires; only clue is "Next Run: -"). (high)
- **[IMPORTANT] PromptsTab: stale-response race and silent failures on select/save/revert** — `ConfigurationPage.tsx:439-465`; wrong-target prompt overrides are a daemon-behavior misconfiguration hazard. (high)
- **[IMPORTANT] VariablesTab: every request path fails silently** — `ConfigurationPage.tsx:571-648`; daemon down renders "No variable definitions found" as truth. (high)
- **[IMPORTANT] ValidationDetectionEditor: invalid JSON doesn't gate the form Save** — `ValidationDetectionEditor.tsx:57-69` + `ConfigurationPage.tsx:264-267`; Save persists the last valid parse while the screen shows different text. (med)
- **[IMPORTANT] Settings dialog: no Escape, no focus trap, no initial focus** — `Settings.tsx:33-50` (`aria-modal` promised, not delivered; FilesPage's confirm dialog shows the in-repo pattern). (high)

### Dashboard / shared / memory / skills

- **[IMPORTANT] Skill edit saves silently discarded on HTTP error** — `useSkills.ts:199-220` (`updateSkill` returns null where `createSkill` throws) + `SkillsPage.tsx:159-171` (closes either way). (high)
- **[IMPORTANT] Memory table Delete is one un-gated click; failed deletes silent** — `MemoryTable.tsx:196-206` → `MemoryPage.tsx:231-243`; `MemoryDetail.tsx:122-126` confirms the same operation. (high)
- **[IMPORTANT] Auto-switch to knowledge view is dead** — `MemoryPage.tsx:139-154`: the persist effect writes `gobby-memory-view` on mount before FalkorDB status resolves, so the `!localStorage.getItem(...)` check is always false. (high)
- **[IMPORTANT] "To Project" skill action is unreachable** — `SkillsGrid.tsx:156` gates on `projectId` which `SkillsPage` (sole importer) never passes. (high)
- **[IMPORTANT] SkillDetail shows the previous skill's safety-scan verdict** — `SkillDetail.tsx:42-44, 152-153`; scan state not keyed/reset on skill change — "SAFE" can render for a never-scanned skill. (high)
- **[IMPORTANT] Skills search: stale debounce overwrites cleared/changed queries; results re-filtered to hide project skills** — `SkillsPage.tsx:117-123, 212-220` + `useSkills.ts:253-280` (no abort/sequence). (high)
- **[IMPORTANT] Dashboard polling hooks wipe data to null on one failed poll** — `useDashboard.ts:78-86`, `useTimeStats.ts:37-44`, `useUsage.ts:34-42`, `useSavings.ts:29-37`; a transient hiccup collapses the dashboard to "Failed to load" for 30s. (high)
- **[IMPORTANT] Memory list hard-capped at 100; client search over the truncated page; server `searchMemories` never called** — `useMemory.ts:122` + `MemoryPage.tsx:172-190`; false-negative search at exactly the scale where it matters. (high)
- **[IMPORTANT] Knowledge-graph limit input clamps on every keystroke** — `MemoryFilters.tsx:94-99`; typing "150" emits 1→clamped to 50; effectively spinner-only. (high)
- **[IMPORTANT] SidebarPanel (shared, 5+ consumers): closed panel stays tabbable; open panel has no dialog role/trap/restore** — `shared/SidebarPanel.tsx:33-62`; keyboard users tab into off-screen content. (high)
- **[IMPORTANT] KnowledgeGraph reports fetch failure as "No entities found"** — `KnowledgeGraph.tsx:260-268, 433-444` over a null-swallowing hook; error conflated with empty. (high)
- **[IMPORTANT] TabBar (shared) has no tablist/tab/aria-selected semantics** — `shared/TabBar.tsx:24-53`; used by Projects/Code/Workflows pages. (high)
- **[IMPORTANT] Skill export fails silently** — `SkillsPage.tsx:185-196` + `useSkills.ts:309-320`. (high)
- **[IMPORTANT] Memory edit form offers a Type select then silently drops the change** — `MemoryForm.tsx:117-130` + `useMemory.ts:84-88` (`UpdateMemoryParams` has no `memory_type`). (high)
- **[IMPORTANT] TasksCard donut: Blocked vs Escalated same hue (350), Ready vs Approved same family (125); legend dots hue-only while arcs use dash patterns** — `TasksCard.tsx:22-29, 78-84`; arc identity unrecoverable even unimpaired. (high)
- **[IMPORTANT] Memory type→color mapping is swapped between dashboard and memory page** — `MemoryCard.tsx:16-21` vs `MemoryFilters.tsx:18-23`/`MemoryTable.tsx:34-42`; destroys the learned color code for a deutan user. (high)
- **[IMPORTANT] Efficiency badge meaning encoded in green-vs-amber hue alone** — `dashboardStyles.ts:188-196` + `TokenEfficiencyCard.tsx:112-116`; 125-vs-75 is a deutan confusion pair. (high)

### Small dirs (integrations / mcp / source-control / traces / rules / projects / command-browser / code-graph / ui)

- **[IMPORTANT] Traces drawer reopens itself forever after deep-link navigation** — `TracesPage.tsx:40-44` (effect refires whenever `selectedTraceId` goes null; `App.tsx:218-232` never clears `initialTraceId`); the drawer cannot be dismissed after "navigate to trace". (high)
- **[IMPORTANT] Source-control destructive actions fail silently — error paths unreachable** — `SourceControlView.tsx:113-133`, `ResourceCard.tsx:18-37`, `IssuesView.tsx:42-60` catch throws while `useSourceControl.ts:364-460` returns `false`/`null`/`[]`; failed worktree/clone deletes look successful. (high)
- **[IMPORTANT] Stale-async races across five selection surfaces** — `McpPage.tsx:127-134`, `ToolBrowserModal.tsx:73-83` (B's panel renders A's schema → wrong tool invocations), `ChannelDetail.tsx:67-81`, `IssuesView.tsx:42-60`, `CodeGraphExplorer.tsx:278-289` (limit slider amplifies); `BranchDetail.tsx:23-40` shows the in-repo `cancelled`-flag pattern. (high)
- **[IMPORTANT] ToolArgumentForm JSON fields reformat under the cursor and submit raw strings on invalid JSON** — `ToolArgumentForm.tsx:150-179` + `ToolBrowserModal.tsx:85-104`. (high)
- **[IMPORTANT] McpPage/TracesPage hue-only status dots** — `McpPage.tsx:44-50,279` (health dot has no text/title/aria at all; the adjacent badge shows a different datum), `TracesPage.tsx:16-20,95`, `TraceWaterfall.tsx:24-31`; `ProjectSummary.StatusGlyph` is the shape-coded house pattern. (high)
- **[IMPORTANT] State palette used as category colors on MCP transport badges; wrong toast foreground** — `McpPage.tsx:54-61` (SSE permanently wears destructive magenta next to a real state dot), `:14` (`--accent-foreground` on `--color-error`; `--text-on-error` exists). (med)
- **[IMPORTANT] GitHub label colors: raw upstream hex as text color** — `githubLabelStyles.ts:8-17`; arbitrary user colors bypass contrast/deutan guarantees (`#ffff00` on dark ≈ unreadable); the PR variant (border-only) is the safe pattern. (med)
- **[IMPORTANT] Emoji/dingbat glyphs instead of the SVG icon system** — `ChannelCard.tsx:75-89` (`✎`, `⏸`/`▶`, `×`), `ChannelForm.tsx:307`, `McpPage.tsx:227,301`; `⏸`/`▶` are emoji-presentation-eligible (breaks `currentColor`). (med)
- **[IMPORTANT] Switch primitive (ui/) is 28px tall with no pointer-coarse promotion** — `ui/Switch.tsx:25`; a multiplying primitive below the 44px floor. (high)

### Cross-slice consolidations

- **[IMPORTANT] Clickable divs/rows/SVG without keyboard parity** — `WorkflowsPage.tsx:287-300` (bulk Enable-All toggle), `PipelinesTab.tsx:409-415` (enable toggle), `PipelineEditor.tsx:414-417` (step header), `ReportsPage.tsx:550-591, 607-672, 687-726` (sortable headers + row selection, no `aria-sort`), `ConfigurationPage.tsx:475-489, 529-533` (prompt categories/cards), `FilesPage.tsx:249-262, 480-521` (file tabs + tree rows), `SkillsGrid.tsx:109-153` (card + enable toggle), `IntegrationsPage.tsx:218-225`, `ChannelForm.tsx:199-208`, `ChannelCard.tsx:38-46`, `MessageList.tsx:122-126`, `McpPage.tsx:275-316`, `IssuesView.tsx:97-101`, `PullRequestsView.tsx:73-77`, `TraceWaterfall.tsx:183-194` (span detail is SVG-click-only). The correct pattern ships nearby in every case (`role="switch"` buttons, `SourceControlView.tsx:139-145`, `RunHistoryTable.tsx:207-214`). (high; found by five reviewers, merged)
- **[IMPORTANT] `outline-none` focus suppression with 1px border-color as the only cue** — `ReportsPage.styles.ts:17, 171`, `ConfigurationPage.styles.ts:44-47`, `CronJobsPage.tsx:30, 104-106`, `KnowledgeGraph.tsx:23` (nothing at all), `MemoryForm.tsx:174`, `task-execution.css:379-382`, `QuickCaptureTask.tsx:17`; the repo's `inputFocusCls` (`shared/focusStyles.ts:13-15`) exists for exactly this. (high; merged)
- **[IMPORTANT] Off-ladder typography at scale** — `execution-utils.tsx:16-121`, `PipelineExecutionsView.tsx:31-64`, `pipelines-reporting.css:29-355` (`*0.6/0.65` — below the 2xs floor), `ReportsPage.styles.ts` (23 sites), `task-execution.css:540-731` + `:173` (rem literal that ignores the Settings slider), `TaskCreateForm.tsx:53-69`, `dashboardStyles.ts:13-118` + SVG `fontSize` literals (`TasksCard.tsx:73-75` etc.), `McpHealthCard.tsx:54,77`; the ladder guard test pins only three selectors. (high; merged from four reviewers)
- **[IMPORTANT] Sub-44px touch targets without pointer-coarse promotion** — `workflows-styles.ts:130-137` (rules/profiles enable toggle ~32×18), `ui/Switch.tsx:25`, `task-execution.css:190, 552-565`, `QuickCaptureTask.tsx:21-24`, `DashboardPage.tsx:48-55`, `ModelBreakdownList.tsx:31-48`, `shared/DiffBlock.tsx:147-155` (also missing `type="button"`), `dashboardStyles.ts:141-142`, `shared/SidebarPanel.css:19-29`; the house pattern (`buttonVariants.ts:25-28`, `--control-row-height` coarse promotion) is skipped. (high; merged)

## Findings — Nits

- **[NIT] `window.alert`/`window.prompt` instead of in-app dialogs** — `PipelineEditor.tsx:325, 342`, `PipelinesTab.tsx:200`; both files already use `useConfirmDialog` elsewhere.
- **[NIT] AddStepButton dropdown lacks outside-click/Escape dismissal** — `PipelineEditor.tsx:864-881`.
- **[NIT] Dead/odd styling in PipelineExecutionsView** — `:42` max-height class neutralized at `:207`; outputs `<pre>` uses `font-[inherit]` while step outputs use `font-mono`.
- **[NIT] StagesTab task-type fetch wipes the Defaults textarea on transient failure and shares the save-error slot** — `StagesTab.tsx:229-252`.
- **[NIT] ReportingTab.tsx (713 lines) + pipelines-reporting.css are dead code** — superseded by ReportsPage; only self-references remain; it also ignores its own `refreshKey` prop.
- **[NIT] AgentsTab `yamlAgent` modal flow unreachable; its non-DB save branch would discard edited YAML while toasting success** — `AgentsTab.tsx:85, 345-348, 464-473`, `AgentsTab.actions.ts:219-261`.
- **[NIT] Sort handlers call setState inside another updater** — `ReportsPage.tsx:104-124`; breaks under StrictMode double-invocation.
- **[NIT] "Waiting" filter trap after switching report sub-tabs** — `ReportsPage.tsx:167, 230-232, 339-342`; filter stays applied while its chip disappears.
- **[NIT] Toast timer collisions (multiple surfaces) and missing `role="status"`/`alert`** — `AgentsTab.tsx:104-107`, `MemoryPage.tsx:159-163, 268-275`, `SkillsPage.tsx:109-112`, `IntegrationsPage.tsx:77-80`, `McpPage.tsx:113-116`; none clear the prior timer.
- **[NIT] `importAgentDefinition`/`downloadAgentDefinition`/`saveYamlAgentDefinition` interpolate agent names into URLs unencoded** — `AgentsTab.actions.ts:418, 203, 250`.
- **[NIT] Rule priority-conflict warning counts template and deleted rules** — `RulesTab.tsx:372-383`; false positives, duplicate names.
- **[NIT] Deep-link to an execution outside the 50-row window does nothing** — `ReportsPage.tsx:139-145` + `usePipelineExecutions.ts:57`.
- **[NIT] TaskCreateForm dead "Clone Task" plumbing with a latent reset-on-identity-change clobber** — `TaskCreateForm.tsx:18-27, 99-108, 155`.
- **[NIT] `taskConstants.ts` zero consumers; `components/index.ts` barrel zero importers** — delete both.
- **[NIT] ApprovalRulesTab calls `setSaveError` inside `setLocalRules` updaters** — `ConfigurationPage.tsx:345-383`; updaters must be pure.
- **[NIT] Token drift on accent/error surfaces** — `CronJobsPage.tsx:80` (`text-[var(--bg-primary)]` on accent; `--accent-foreground` exists), `FilesPage.tsx:90` (`--accent-foreground` on an error surface), `BACKEND_SECRET_MASK` exported from a styles module.
- **[NIT] Frontend/backend secret-pattern divergence; "Personal" project matched by display name; color-only cron status dot** — `ConfigurationPage.helpers.ts:15-22` vs `config_store.py:29-41` (`_auth` missing client-side), `ProjectSelector.tsx:30-43`, `CronJobsPage.tsx:46-51`.
- **[NIT] Pin toggle destroys stored importance** — `MemoryTable.tsx:65-69` (unpin hard-resets to 0.5).
- **[NIT] Triple-duplicated 30s polling** — `TasksCard`/`SessionsCard`/`MemoryCard` each call `useTimeStats`; `SavingsCard` + `TokenEfficiencyCard` each call `useSavings`+`useUsage`; 5× identical requests per tick.
- **[NIT] McpHealthCard and PipelinesCard are dead code** — zero importers.
- **[NIT] Misc one-liners (dashboard/memory/shared)** — `SourceIcon.tsx:74-92` (switch with only default), `sourceType.ts:17-18` (dead branch), `SystemHealthCard.tsx:15-24` ("1h 60m" via `Math.round`), `KnowledgeGraph.tsx:91-96` (raw `hsl()` edge hues), `MemoryForm` no Escape-close, `MemoryTable.tsx:101-103` (nested interactive inside `role="button"`).
- **[NIT] Three dead project components (~270 lines) + unreachable "graph" tab branch** — `ProjectCard/ProjectOverview/ProjectDetailView`, `ProjectsPage.tsx:118-120` renders a tab `TABS` never offers.
- **[NIT] Channel type-badge tint is invalid CSS (`var(...)1F`); `CHANNEL_COLOR_PAIRS` dead export** — `ChannelCard.tsx:53-54`, `ChannelDetail.tsx:130-131`, `channelMetadata.ts:8-16`.
- **[NIT] ChannelDetail ignores its `fetchStatus` prop and re-implements the fetch inline** — `ChannelDetail.tsx:53-80`.
- **[NIT] Numeric edge cases** — `SourceControlView.tsx:193` (cleanup-hours can go `NaN` into a destructive call), `MessageList.tsx:189-191` ("Showing 51–50"), `cron/formatters.ts:18-19` (negative duration; `RunHistoryTable` guards it).
- **[NIT] CodeGraphExplorer: failed expansion permanently marks node expanded; legend `slice(0,7)` hides entries; webhook URL unencoded** — `CodeGraphExplorer.tsx:331-342, 637`, `ChannelDetail.tsx:113-115`.

## Systemic patterns

1. **Hooks return failure values; components only catch throws.** `useWorkflows`, `useMemory`, `useSkills` (update/export/move/restore), `useCronJobs`, `useConfiguration`, `useFiles`, `useSourceControl`, `useRules`, `useIntegrations` all swallow `!res.ok` into `null`/`false` returns — and the view layer wraps calls in try/catch whose error branches are therefore unreachable. Five of the fifteen Blockers and a dozen Importants share this single root cause. Pick one contract repo-wide: hooks throw with backend `detail`, or lint that every nullable mutation result is checked.

2. **Reconstruct-and-replace payloads lose fields the form doesn't model.** RulesTab (match/tools/effects → Blocker), AgentsTab update/duplicate (workflows unknown keys, wildcard skills, blocked_tools), channel edit (secret refs → Blocker), config form (stale full-object PUT → Blocker). Servers doing full replacement plus clients rebuilding from form state means every new server-side field silently regresses. Save paths must merge over fetched truth.

3. **Two views, two sources of truth.** PipelinesTab Form/YAML (Blocker) and RuleEditForm Form/YAML (Important) independently implement the same broken shape: a snapshot taken at open, never regenerated on view switch, whichever saves last wins. Same family as components-a's `allItems`/`sessionIndexMap` Blocker.

4. **Prop-seeded `useState` without `key` remount.** ProjectSettings (Blocker), ProfilesTab editor, SkillDetail scan state, TaskCreateForm's latent `defaults` clobber. The `key=` idiom already used by `ValidationDetectionEditor` should be the standard answer.

5. **Normalize-on-keystroke through derived controlled values.** PipelineEditor KV/Condition/Tools (three Blockers), ToolArgumentForm JSON, MemoryFilters limit input, RuleEditForm custom arg keys — user input serialized through a lossy transform per keystroke and re-derived for display. Fix once as a shared "raw local state, commit on blur" field primitive.

6. **No stale-response guards on selection-driven fetches.** RulesTab sidebar, PipelinesTab exportYaml, PromptsTab, McpPage, ToolBrowserModal, ChannelDetail, IssuesView, CodeGraphExplorer, skills search, FilesPage diff, useCronJobs — while `BranchDetail`/`PullRequestDetail`/dashboard hooks demonstrate the correct cancelled-flag/AbortController pattern in the same tree. A `useAbortableFetch` helper closes the class.

7. **Dead surface at scale.** ~5,600 lines in `tasks/`, ReportingTab + its CSS (~1,100), three project components, two dashboard cards, the AgentsTab YAML modal flow, dead barrels/constants — much of it with green tests, some of it (placebo safety toggles, fabricated audit log) actively dangerous if rewired. The repo needs a dead-code sweep with deletion tasks.

8. **The design system's remedies exist and are skipped at the offending sites.** `StepStatusIcon` dead while step status is hue-only; `inputFocusCls` beside `outline-none`; `buttonVariants` coarse-pointer promotion beside 18px toggles; `--text-on-error` beside `--accent-foreground`-on-error; shape-coded `StatusGlyph` beside bare dots; `color-mix`-able tokens beside invalid `var()+hex` concatenations (two independent instances: step-type badges, channel badges). Enforcement is the gap, not design.
