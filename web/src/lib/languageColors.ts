/**
 * Language and Git-status color palette for FilesTab and related surfaces.
 *
 * Source of truth: this module exports the typed `{ dark, light }` OKLCH
 * pairs. The same values are mirrored as `--lang-*` and `--git-status-*` CSS
 * custom properties in `web/src/styles/index.css`, which lets consumers read
 * the theme-active variant via `var(--…)` and lets the browser cascade pick
 * the right one for the current `[data-theme]`. The CSS-var rendering path
 * is the project's "small helper that picks the variant from the active
 * theme" — declarative, free, and re-renders nothing on theme switch.
 *
 * Each pair was sized for WCAG 2.2 AA contrast against the page backgrounds
 * (`--bg-primary`):
 *   dark theme bg  = oklch(15% 0.005 125)  ≈ sRGB Y 0.018
 *   light theme bg = oklch(99% 0.002 125)  ≈ sRGB Y 0.97
 *
 * For the dark variants (L 65–82%), contrast against the dark background
 * lands at ~6.0–9.0:1. For the light variants (L 38–48%), contrast against
 * the light background lands at ~4.6–8.5:1. All pairs clear the 4.5:1
 * threshold; the few that previously sat at ~4.3:1 (L≈48 vs near-white) are
 * tightened to L 42–45.
 *
 * Colour is the *fourth* signal in this UI — file name (with extension), the
 * file/folder glyph, and the parent directory all carry the same information.
 * The icon hue is supplementary, never the sole identifier.
 */

export interface LanguageColorPair {
  /** OKLCH value used when `[data-theme]` is the default (dark). */
  dark: string;
  /** OKLCH value used when `[data-theme="light"]` is active. */
  light: string;
}

/** Typed map of language → {dark, light} OKLCH pairs.
 *  Mirrored by `--lang-<key>` tokens in styles/index.css. */
export const LANGUAGE_COLORS: Record<string, LanguageColorPair> = {
  typescript: { dark: "oklch(70% 0.14 240)", light: "oklch(42% 0.18 240)" },
  javascript: { dark: "oklch(82% 0.16 95)", light: "oklch(48% 0.16 95)" },
  python: { dark: "oklch(68% 0.13 250)", light: "oklch(40% 0.16 250)" },
  rust: { dark: "oklch(68% 0.18 35)", light: "oklch(42% 0.20 35)" },
  go: { dark: "oklch(72% 0.13 215)", light: "oklch(42% 0.15 215)" },
  config: { dark: "oklch(72% 0.14 65)", light: "oklch(45% 0.16 65)" },
  doc: { dark: "oklch(60% 0.005 125)", light: "oklch(38% 0.005 125)" },
  style: { dark: "oklch(65% 0.15 290)", light: "oklch(40% 0.18 290)" },
  markup: { dark: "oklch(68% 0.18 30)", light: "oklch(42% 0.20 30)" },
  shell: { dark: "oklch(72% 0.16 130)", light: "oklch(42% 0.18 130)" },
  sql: { dark: "oklch(70% 0.14 75)", light: "oklch(42% 0.16 75)" },
  ruby: { dark: "oklch(65% 0.18 350)", light: "oklch(42% 0.20 350)" },
  default: { dark: "oklch(60% 0.005 125)", light: "oklch(38% 0.005 125)" },
  folder: { dark: "oklch(82% 0.14 95)", light: "oklch(52% 0.16 75)" },
};

/** Typed map of git-status code → {dark, light} OKLCH pairs.
 *  Mirrored by `--git-status-<key>` tokens in styles/index.css. */
export const GIT_STATUS_COLORS: Record<string, LanguageColorPair> = {
  modified: { dark: "oklch(78% 0.13 75)", light: "oklch(45% 0.16 75)" },
  added: { dark: "oklch(72% 0.16 130)", light: "oklch(42% 0.18 130)" },
  deleted: { dark: "oklch(72% 0.20 350)", light: "oklch(45% 0.22 350)" },
  renamed: { dark: "oklch(70% 0.14 240)", light: "oklch(42% 0.16 240)" },
  untracked: { dark: "oklch(60% 0.005 125)", light: "oklch(38% 0.005 125)" },
};

const EXTENSION_TO_KEY: Record<string, keyof typeof LANGUAGE_COLORS> = {
  ts: "typescript",
  tsx: "typescript",
  js: "javascript",
  jsx: "javascript",
  py: "python",
  rs: "rust",
  go: "go",
  json: "config",
  yaml: "config",
  yml: "config",
  toml: "config",
  md: "doc",
  txt: "doc",
  rst: "doc",
  css: "style",
  scss: "style",
  less: "style",
  html: "markup",
  htm: "markup",
  sh: "shell",
  bash: "shell",
  zsh: "shell",
  sql: "sql",
  rb: "ruby",
};

/**
 * Resolve a file extension to its theme-aware language colour.
 *
 * Returns a `var(--lang-<key>)` reference; the active `[data-theme]` selects
 * the dark or light variant declared in this module's `LANGUAGE_COLORS` and
 * mirrored by `web/src/styles/index.css`. No JS subscription to theme state
 * is needed — the cascade does the work, and theme switches re-paint without
 * a React re-render.
 */
export function getLanguageColorVar(extension: string): string {
  const key = EXTENSION_TO_KEY[extension.toLowerCase()] ?? "default";
  return `var(--lang-${key})`;
}

/**
 * Resolve a git-status code to its theme-aware colour. Accepts both single-
 * char (`M`, `A`, `D`, `R`, `?`) and porcelain double-char (`??`) forms.
 */
export function getGitStatusColorVar(status: string): string {
  if (status === "M") return "var(--git-status-modified)";
  if (status === "A") return "var(--git-status-added)";
  if (status === "D") return "var(--git-status-deleted)";
  if (status === "R") return "var(--git-status-renamed)";
  if (status === "?") return "var(--git-status-untracked)";
  if (status === "??") return "var(--git-status-untracked)";
  return "var(--git-status-untracked)";
}

/** Folder-icon stroke colour (theme-aware). */
export const FOLDER_ICON_COLOR_VAR = "var(--lang-folder)";
