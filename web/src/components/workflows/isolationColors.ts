/**
 * Agent-isolation color palette for AgentsTab and related surfaces.
 *
 * Source of truth: this module exports the typed `{ dark, light }` OKLCH
 * pairs. The same values are mirrored as `--isolation-*` CSS custom
 * properties in `web/src/styles/tokens.css`, which lets consumers read the
 * theme-active variant via `var(--…)` and lets the browser cascade pick
 * the right one for the current `[data-theme]`.
 *
 * Each pair was sized for WCAG 2.2 AA contrast against the page
 * backgrounds (`--bg-primary`). Dark variants land at L 70-78; light
 * variants at L 45-58.
 *
 * Color is supplementary in this UI — every consumer pairs the chip with
 * the literal isolation mode label ("clone", "worktree", "none",
 * "inherit"). The hue is the fourth signal, never the sole identifier.
 *
 * Semantic intent:
 *   clone     — full repo copy, highest blast radius (red/destructive)
 *   worktree  — separate working tree on shared object DB (amber/warning)
 *   none      — no isolation, runs in caller's tree (neutral)
 *   inherit   — defers to parent agent's setting (muted slate)
 */

export interface IsolationColorPair {
  /** OKLCH value used when `[data-theme]` is the default (dark). */
  dark: string;
  /** OKLCH value used when `[data-theme="light"]` is active. */
  light: string;
}

/** Typed map of isolation-mode → {dark, light} OKLCH pairs.
 *  Mirrored by `--isolation-<key>` tokens in styles/tokens.css. */
export const ISOLATION_COLORS: Record<string, IsolationColorPair> = {
  clone: { dark: "oklch(72% 0.20 350)", light: "oklch(52% 0.22 350)" },
  worktree: { dark: "oklch(78% 0.16 75)", light: "oklch(58% 0.16 75)" },
  none: { dark: "oklch(60% 0.005 125)", light: "oklch(45% 0.005 125)" },
  inherit: { dark: "oklch(70% 0.005 250)", light: "oklch(45% 0.005 250)" },
};

/**
 * Resolve an isolation mode to its theme-aware color.
 *
 * Returns a `var(--isolation-<key>)` reference; the active `[data-theme]`
 * selects the dark or light variant declared above and mirrored by
 * `web/src/styles/tokens.css`. Unknown modes collapse to
 * `var(--text-muted)`.
 */
export function getIsolationColorVar(mode: string): string {
  return ISOLATION_COLORS[mode] ? `var(--isolation-${mode})` : "var(--text-muted)";
}
