# Web Contrast Audit — WCAG 2.2 AA, non-chat surfaces

**Audited**: 2026-05-05 against `web/src/styles/index.css` design tokens
(post-fix). Method: OKLCH → linear sRGB → relative luminance → WCAG contrast
ratio per [W3C WCAG 2.2 §1.4.3 / §1.4.11](https://www.w3.org/TR/WCAG22/).

WCAG bands referenced below:

- **AA normal text**: ≥ 4.5:1
- **AA large text** (≥ 18pt or 14pt bold) and **AA UI / non-text**: ≥ 3:1
- **AAA normal text**: ≥ 7:1

Verification script: inline `node` invocation under `[gobby-#13680]`. The
algorithm is the standard OKLCH-OKLab matrix → linear sRGB → 0.2126·R +
0.7152·G + 0.0722·B luminance. The audit covers tokens used by all non-chat
surfaces: workflows (pipelines/agents/rules), tasks, sessions, projects,
files, memory, skills, configuration, cron jobs, settings, source-control,
agent portfolio, and the page chrome (toolbar, modals, popovers).

---

## 1. Text-on-background contrast (post-fix)

| Foreground | bg-primary | bg-secondary | bg-tertiary | min-band |
|---|---|---|---|---|
| **DARK** | | | | |
| text-primary | 15.53 | 14.58 | 13.32 | AAA |
| text-secondary | 6.84 | 6.41 | 5.86 | AA |
| text-muted **(post-fix L=0.62)** | 5.41 | 5.07 | 4.64 | AA |
| accent | 11.77 | 11.04 | 10.09 | AA |
| color-info | 7.89 | 7.41 | 6.76 | AA |
| color-warning-foreground | 9.60 | 9.01 | 8.21 | AA |
| color-success-foreground | 11.54 | 10.83 | 9.88 | AA |
| color-error | 7.19 | 6.74 | 6.15 | AA |
| **LIGHT** | | | | |
| text-primary | 12.94 | 11.81 | 10.42 | AAA |
| text-secondary | 8.18 | 7.46 | 6.59 | AA |
| text-muted **(post-fix L=0.51)** | 5.58 | 5.11 | 4.54 | AA |
| accent | 5.50 | 5.04 | 4.47 | AA* |
| color-info | 5.79 | 5.29 | 4.69 | AA |
| color-warning-foreground | 13.40 | 12.26 | 10.88 | AA |
| color-success-foreground | 15.40 | 14.12 | 12.52 | AA |
| color-error | 5.28 | 4.84 | 4.29 | AA* |

`AA*` = passes 3:1 for large text and UI components but is below the 4.5:1
threshold for normal text on the worst-case background.

### 1.1 Pre-fix vs post-fix delta

| Token | Theme | L (pre) | L (post) | worst ratio (pre) | worst ratio (post) |
|---|---|---|---|---|---|
| --text-muted | dark | 0.48 | **0.62** | 2.58:1 (FAIL even AA UI) | 4.64:1 (AA) |
| --text-muted | light | 0.60 | **0.51** | 3.11:1 (AA UI only) | 4.54:1 (AA) |

Fix landed at commit `5e34a5b34` (this task).

---

## 2. Focus ring (`:focus-visible` → `outline: 2px solid var(--accent)`)

Focus indicators are UI components per WCAG 2.2 §1.4.11 — required ratio
≥ 3:1 against the surface they sit on.

| Surface | DARK | LIGHT |
|---|---|---|
| accent on bg-primary | 11.77:1 ✓ | 5.50:1 ✓ |
| accent on bg-secondary | 11.04:1 ✓ | 5.04:1 ✓ |
| accent on bg-tertiary | 10.09:1 ✓ | 4.47:1 ✓ |

The shared `.btn:focus-visible` rule lives in `web/src/styles/buttons.css:36`.
Surface-specific overrides (`session-delete-btn`, `lifecycle-card`,
`activity-filter-button`, `activity-task-detail-parent-link`, `workflows-search`)
all derive from `var(--accent)` so they inherit the same ratio.

**Result**: focus ring passes AA UI on every non-chat surface in both themes.

---

## 3. Borderline cases — formal exceptions

Two token-on-background pairs land between 3:1 (AA UI/large) and 4.5:1
(AA normal text):

| Pair | Ratio | Exception rationale |
|---|---|---|
| **light** accent on bg-tertiary | 4.47:1 | `--accent` is used predominantly as a **background** color for buttons/badges (paired with `--accent-foreground`) and as a **focus ring**. As a non-text UI component on bg-tertiary it clears the 3:1 threshold. The codebase has no surface where `--accent` renders as normal-size text on bg-tertiary; if such usage is introduced, the call site must use `--text-primary` or `--accent-hover` (5.32:1 on bg-tertiary in light) instead. |
| **light** color-error on bg-tertiary | 4.29:1 | `--color-error` is paired with `--color-error-soft` (12% alpha tint) for error chips/banners; the tinted background sits on bg-primary or bg-secondary, not bg-tertiary, and the foreground in those chips is `--text-on-error` (a guaranteed-AA pairing). The bare-text usage on bg-tertiary surfaces is limited to the inline error message under YAML editors — large text (≥ 18pt) by typography scale, where 4.29:1 clears AA Large (3:1). |

Both exceptions are conditional on usage staying within the documented
patterns; any new code path that puts `--accent` or `--color-error` text at
normal size on bg-tertiary is a regression and must be flagged in review.

---

## 4. Grayscale / deutan-safety — state palette

Per `.impeccable.md` Color Constraints: state colors must remain
distinguishable when desaturated (lightness + redundant cue, not hue alone).
We audit pairwise oklch L deltas (`ΔL`) and pairwise WCAG contrast on
relative-luminance values (a proxy for grayscale separation):

### 4.1 DARK theme

| Pair | ΔL | Grayscale ratio | Verdict |
|---|---|---|---|
| color-info ↔ color-warning-fg | 0.06 | 1.22:1 | distinguishable by L (warning lighter) |
| color-info ↔ color-success-fg | 0.10 | 1.46:1 | distinguishable by L (success lighter) |
| **color-info ↔ color-error** | **0.00** | **1.10:1** | **collision** — both L=0.72 |
| color-warning-fg ↔ color-success-fg | 0.04 | 1.20:1 | borderline — Δ < 0.05 |
| color-warning-fg ↔ color-error | 0.06 | 1.10:1 | distinguishable by L |
| color-success-fg ↔ color-error | 0.10 | 1.32:1 | distinguishable by L |

### 4.2 LIGHT theme

| Pair | ΔL | Grayscale ratio | Verdict |
|---|---|---|---|
| color-info ↔ color-warning-fg | 0.10 | 1.46:1 | distinguishable by L |
| **color-info ↔ color-success-fg** | **0.00** | **1.01:1** | **collision** — both L=0.48 |
| color-info ↔ color-error | 0.04 | 1.03:1 | borderline |
| color-warning-fg ↔ color-success-fg | 0.10 | 1.45:1 | distinguishable by L |
| color-warning-fg ↔ color-error | 0.06 | 1.42:1 | distinguishable by L |
| color-success-fg ↔ color-error | 0.04 | 1.03:1 | borderline |

### 4.3 Mitigation — label and shape redundancy

State colors in the codebase are never the sole carrier of meaning. Every
status indicator pairs the color with a redundant cue:

- **`StatusBadge`** (`tasks/TaskBadges.tsx`, `workflows/execution-utils.tsx`,
  `source-control/StatusBadge.tsx`) — colored chip + the status word
  (`completed`, `failed`, `running`, `waiting_approval`, …) as visible label.
- **`PipelineStatusDot` / `ActivityRowStatusDot`** — colored dot that always
  appears next to the status word in its row context.
- **`pipeline-step--*` icons** (`PipelineExecutionsView.tsx`) — each step
  state has a distinct shape (check / x / spinner / clock) layered on the
  color.

Because every state-color use site carries either an explicit label or an
icon shape redundant to the hue, the L-collisions at info↔error (dark) and
info↔success (light) do **not** introduce a deutan-vision failure mode.

### 4.4 Recommendation (deferred)

If the design system is later extended with a state-only indicator (no
label, no icon), the colliding pairs must be re-spaced before that
indicator ships. Suggested L shifts that preserve current hue intent
(unverified — would need re-running this audit):

- DARK `color-error`: L 0.72 → 0.66 (split from `color-info` L 0.72)
- LIGHT `color-success-foreground`: L 0.48 → 0.42 (split from `color-info`
  L 0.48)

These are not applied in this task because the redundant-cue pattern is
already universal across the non-chat surfaces.

---

## 5. Surfaces verified

The audit covers every token consumer reachable from the non-chat surface
roots. Surfaces enumerated by routing entry / page component:

- workflows: `WorkflowsPage`, `PipelinesTab`, `AgentsTab`, `RulesTab`,
  `PipelineEditor`, `PipelineExecutionsView`, `ReportingTab`, `ReportsPage`
  + variants
- tasks: `TasksPage`, `task-execution.css` overrides, `TaskBadges`,
  `CapabilityScope`, `PermissionOverrides`, `RawTraceView`, `SessionViewer`
- sessions: `SessionsPage`, `SessionDetail`, `SessionLineage`,
  `MobileSessionDrawer`
- projects: `ProjectsPage`, `ProjectCard`, `ProjectDetailView`,
  `ProjectOverview`, `ProjectSettings`, `ProjectSummary`
- files: `FilesPage`
- memory: `MemoryPage`, `MemoryDetail`
- skills: `SkillsPage`, `SkillDetail`, `SkillForm`, `SkillHubBrowser`,
  `SkillImportModal`, `SkillScanPanel`, `SkillsFilters`, `SkillsGrid`
- agents (portfolio): `AgentPortfolioPage`, `AgentEditForm`,
  `AgentRulesEditor`, `AgentSkillsEditor`, `AgentVariablesEditor`,
  `AgentToolBlocksEditor`, `IsolationTargetSelector`
- chrome: `ConfigurationPage`, `CronJobsPage`, `SettingsPage`,
  `lifecycle-board`, `source-control`, shared toolbar/modal patterns

All consume the shared tokens defined in `web/src/styles/index.css`
(`:root` block for dark, `[data-theme="light"]` block for light). The
audit is therefore complete by transitivity once the token layer passes —
the specific TSX file enumerated above is intentional for review
provenance, not because each is independently audited.

---

## 6. Re-running the audit

Re-run after any change to the `--bg-*`, `--text-*`, `--border`, `--accent`,
or `--color-*` tokens in `web/src/styles/index.css`. The script lives
inline in commit `5e34a5b34`'s task close summary and can be re-executed
by pasting the `node -e '...'` block into a shell from the repo root.
A future task may extract it into `web/scripts/contrast-audit.js`; tracked
under follow-on cleanup.
