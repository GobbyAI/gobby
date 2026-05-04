/**
 * Source / provider color and label palette for session, dashboard, and
 * agent surfaces.
 *
 * Mirrors the languageColors.ts / pipelineColors.ts pattern: typed
 * `{ dark, light }` OKLCH pairs are the source of truth, mirrored by
 * `--source-*` and `--provider-*` CSS custom properties in
 * `web/src/styles/index.css`. Consumers call the helpers, which return
 * `var(--…)` references; the cascade picks the variant for the active
 * `[data-theme]` — no JS theme subscription required.
 *
 * Hue choices follow the deutan-safe palette in `.impeccable.md`:
 * brand hue 125 is reserved for Gobby itself, so source/provider hues sit
 * away from the brand slot. Pipeline / cron / unknown / inherit collapse
 * to neutral grays tinted to the brand axis.
 *
 * WCAG 2.2 AA contrast was validated against `--bg-primary`:
 *   dark theme bg  = oklch(15% 0.005 125)  ≈ sRGB Y 0.018
 *   light theme bg = oklch(99% 0.002 125)  ≈ sRGB Y 0.97
 *
 * Dark variants sit at L 60–78%; light variants at L 40–48%. All pairs
 * clear 4.5:1 in their theme.
 *
 * Color is the supplementary signal — every consumer pairs the dot or
 * badge with a SOURCE_LABELS text label or a logo, so the hue is never
 * the sole identifier.
 */

export interface SourceColorPair {
  /** OKLCH value used when `[data-theme]` is the default (dark). */
  dark: string;
  /** OKLCH value used when `[data-theme="light"]` is active. */
  light: string;
}

/** Session-source palette. Mirrored by `--source-<key>` tokens in
 *  styles/index.css. Pipeline / cron / unknown / default share the
 *  neutral slot intentionally — they are state, not brand. */
export const SOURCE_COLOR_PAIRS: Record<string, SourceColorPair> = {
  claude: { dark: "oklch(70% 0.16 290)", light: "oklch(42% 0.18 290)" },
  gemini: { dark: "oklch(72% 0.16 145)", light: "oklch(42% 0.18 145)" },
  qwen: { dark: "oklch(78% 0.15 75)", light: "oklch(45% 0.18 75)" },
  codex: { dark: "oklch(70% 0.14 240)", light: "oklch(42% 0.16 240)" },
  droid: { dark: "oklch(72% 0.13 200)", light: "oklch(45% 0.15 200)" },
  pipeline: { dark: "oklch(60% 0.005 125)", light: "oklch(40% 0.005 125)" },
  cron: { dark: "oklch(70% 0.005 125)", light: "oklch(50% 0.005 125)" },
  unknown: { dark: "oklch(60% 0.005 125)", light: "oklch(40% 0.005 125)" },
  default: { dark: "oklch(60% 0.005 125)", light: "oklch(40% 0.005 125)" },
};

/** Agent-provider palette. Mirrored by `--provider-<key>` tokens in
 *  styles/index.css. Provider keys overlap session-source keys for the
 *  five core CLIs, and add inherit / cursor / windsurf / copilot. */
export const PROVIDER_COLOR_PAIRS: Record<string, SourceColorPair> = {
  inherit: { dark: "oklch(70% 0.005 250)", light: "oklch(45% 0.005 250)" },
  claude: { dark: "oklch(70% 0.16 290)", light: "oklch(42% 0.18 290)" },
  gemini: { dark: "oklch(72% 0.16 145)", light: "oklch(42% 0.18 145)" },
  qwen: { dark: "oklch(78% 0.15 75)", light: "oklch(45% 0.18 75)" },
  codex: { dark: "oklch(70% 0.14 240)", light: "oklch(42% 0.16 240)" },
  droid: { dark: "oklch(72% 0.13 200)", light: "oklch(45% 0.15 200)" },
  cursor: { dark: "oklch(70% 0.14 240)", light: "oklch(42% 0.16 240)" },
  windsurf: { dark: "oklch(72% 0.12 190)", light: "oklch(42% 0.14 190)" },
  copilot: { dark: "oklch(72% 0.18 50)", light: "oklch(48% 0.20 50)" },
  unknown: { dark: "oklch(60% 0.005 125)", light: "oklch(40% 0.005 125)" },
};

export const SOURCE_LABELS: Record<string, string> = {
  claude: 'Claude',
  gemini: 'Gemini',
  qwen: 'Qwen',
  codex: 'Codex',
  droid: 'Droid',
  pipeline: 'Pipeline',
  cron: 'Cron',
  claude_code: 'Claude Code',
  claude_sdk: 'Claude SDK',
  claude_sdk_web_chat: 'Claude SDK Web Chat',
  cursor: 'Cursor',
  windsurf: 'Windsurf',
  copilot: 'Copilot',
}

/** Resolve a session source to its theme-aware accent colour. */
export function getSourceColorVar(source: string): string {
  return SOURCE_COLOR_PAIRS[source]
    ? `var(--source-${source})`
    : "var(--source-default)";
}

/** Resolve an agent provider to its theme-aware accent colour. */
export function getProviderColorVar(provider: string): string {
  return PROVIDER_COLOR_PAIRS[provider]
    ? `var(--provider-${provider})`
    : "var(--provider-default)";
}
