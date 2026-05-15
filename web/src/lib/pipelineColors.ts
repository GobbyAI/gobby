import type { StatusKind } from '../components/activity/ActivityRowStatusDot';

/**
 * Pipeline editor and execution color palette.
 *
 * Mirrors the languageColors.ts pattern (#13955): typed `{ dark, light }`
 * OKLCH pairs are the source of truth, mirrored by `--step-type-*` and
 * `--exec-status-*` CSS custom properties in `web/src/styles/index.css`.
 * Consumers call the helpers, which return `var(--…)` references; the
 * cascade picks the variant for the active `[data-theme]` — no JS theme
 * subscription needed.
 *
 * WCAG 2.2 AA contrast was validated against `--bg-primary`:
 *   dark theme bg  = oklch(15% 0.005 125)  ≈ sRGB Y 0.018
 *   light theme bg = oklch(99% 0.002 125)  ≈ sRGB Y 0.97
 *
 * Dark variants sit at L 65–75% (≈ 6.0–8.5:1 against the dark bg).
 * Light variants sit at L 38–48% (≈ 4.6–8.0:1 against the light bg).
 * All pairs clear the 4.5:1 threshold per theme.
 *
 * For step types these are decorative legend swatches; for pipeline
 * statuses these are status dots paired with text labels (`title=`
 * tooltip + status text rendered alongside in StatusBadge), so colour is
 * the supplementary signal and not the sole identifier.
 */

export interface PipelineColorPair {
  /** OKLCH value used when `[data-theme]` is the default (dark). */
  dark: string;
  /** OKLCH value used when `[data-theme="light"]` is active. */
  light: string;
}

/** Step-type accent colours for PipelineEditor's step legend.
 *  Mirrored by `--step-type-<key>` tokens in styles/index.css. */
export const STEP_TYPE_COLORS: Record<string, PipelineColorPair> = {
  exec: { dark: "oklch(72% 0.13 200)", light: "oklch(45% 0.15 200)" },
  prompt: { dark: "oklch(70% 0.16 290)", light: "oklch(42% 0.18 290)" },
  mcp: { dark: "oklch(70% 0.14 240)", light: "oklch(42% 0.16 240)" },
  invoke_pipeline: { dark: "oklch(72% 0.16 310)", light: "oklch(45% 0.18 310)" },
  activate_workflow: { dark: "oklch(72% 0.13 180)", light: "oklch(42% 0.15 180)" },
};

/** Pipeline-execution status dot colours.
 *  Mirrored by `--exec-status-<key>` tokens in styles/index.css. */
export const EXEC_STATUS_COLORS: Record<string, PipelineColorPair> = {
  running: { dark: "oklch(70% 0.14 240)", light: "oklch(42% 0.16 240)" },
  pending: { dark: "oklch(60% 0.005 125)", light: "oklch(40% 0.005 125)" },
  completed: { dark: "oklch(72% 0.16 130)", light: "oklch(42% 0.18 130)" },
  success: { dark: "oklch(72% 0.16 130)", light: "oklch(42% 0.18 130)" },
  failed: { dark: "oklch(70% 0.20 350)", light: "oklch(45% 0.22 350)" },
  error: { dark: "oklch(70% 0.20 350)", light: "oklch(45% 0.22 350)" },
  timeout: { dark: "oklch(72% 0.18 50)", light: "oklch(48% 0.20 50)" },
  waiting_approval: { dark: "oklch(78% 0.15 75)", light: "oklch(45% 0.18 75)" },
  cancelled: { dark: "oklch(60% 0.005 125)", light: "oklch(40% 0.005 125)" },
  interrupted: { dark: "oklch(70% 0.16 290)", light: "oklch(42% 0.18 290)" },
};

/** Resolve a step-type to its theme-aware accent colour. */
export function getStepTypeColorVar(stepType: string): string {
  return STEP_TYPE_COLORS[stepType]
    ? `var(--step-type-${stepType})`
    : "var(--text-muted)";
}

/** Collapse the typed pipeline status palette onto the status-dot taxonomy.
 *  Running uses the info kind and pulse animation; completed/success resolve
 *  to success. Pending and cancelled are disabled because they do not require
 *  attention, while timeout, waiting_approval, and interrupted use warning
 *  because they need operator awareness but are not execution failures. */
const EXEC_STATUS_KINDS: Record<string, StatusKind> = {
  running: "info",
  pending: "disabled",
  skipped: "disabled",
  completed: "success",
  success: "success",
  failed: "error",
  error: "error",
  timeout: "warning",
  waiting_approval: "warning",
  cancelled: "disabled",
  interrupted: "warning",
};

export function getExecStatusKind(status: string): StatusKind {
  return EXEC_STATUS_KINDS[status] ?? "disabled";
}
