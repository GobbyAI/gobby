/**
 * Task-category color palette for AgentPortfolioPage and related surfaces.
 *
 * Source of truth: this module exports the typed `{ dark, light }` OKLCH
 * pairs. The same values are mirrored as `--category-*` CSS custom
 * properties in `web/src/styles/tokens.css`, which lets consumers read the
 * theme-active variant via `var(--…)` and lets the browser cascade pick
 * the right one for the current `[data-theme]`.
 *
 * Each pair was sized for WCAG 2.2 AA contrast against the page
 * backgrounds (`--bg-primary`). Dark variants land at L 70-78 (~6-8:1);
 * light variants at L 40-50 (~5-7:1).
 *
 * Color is supplementary in this UI — every consumer pairs the segment
 * or dot with a text label and tooltip. The hue is the fourth signal,
 * never the sole identifier.
 */

export interface CategoryColorPair {
  /** OKLCH value used when `[data-theme]` is the default (dark). */
  dark: string;
  /** OKLCH value used when `[data-theme="light"]` is active. */
  light: string;
}

/** Typed map of task-category → {dark, light} OKLCH pairs.
 *  Mirrored by `--category-<key>` tokens in styles/tokens.css. */
export const CATEGORY_COLORS: Record<string, CategoryColorPair> = {
  code: { dark: "oklch(70% 0.18 250)", light: "oklch(45% 0.20 250)" },
  test: { dark: "oklch(72% 0.20 145)", light: "oklch(45% 0.20 145)" },
  docs: { dark: "oklch(72% 0.22 295)", light: "oklch(45% 0.22 295)" },
  config: { dark: "oklch(78% 0.16 75)", light: "oklch(50% 0.18 75)" },
  refactor: { dark: "oklch(72% 0.13 200)", light: "oklch(45% 0.15 200)" },
  research: { dark: "oklch(72% 0.22 350)", light: "oklch(48% 0.22 350)" },
  planning: { dark: "oklch(60% 0.005 125)", light: "oklch(40% 0.005 125)" },
  manual: { dark: "oklch(72% 0.18 50)", light: "oklch(48% 0.20 50)" },
  default: { dark: "oklch(50% 0.005 125)", light: "oklch(45% 0.005 125)" },
};

/**
 * Resolve a task category to its theme-aware color.
 *
 * Returns a `var(--category-<key>)` reference; the active `[data-theme]`
 * selects the dark or light variant declared above and mirrored by
 * `web/src/styles/tokens.css`. Unknown categories collapse to
 * `var(--category-default)`.
 */
export function getCategoryColorVar(category: string): string {
  return CATEGORY_COLORS[category] ? `var(--category-${category})` : "var(--category-default)";
}
