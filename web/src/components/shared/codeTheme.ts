import { oneDark } from 'react-syntax-highlighter/dist/esm/styles/prism'

/**
 * Code-chrome palette for syntax-highlighted blocks and the CodeMirror
 * editor.
 *
 * Source of truth: this module exports the typed `{ dark, light }` OKLCH
 * pairs in `CODE_CHROME` and the matching `var(--…)` refs in
 * `CODE_CHROME_VARS`. The same values are mirrored as `--code-*` CSS
 * custom properties in `web/src/styles/index.css`, which lets consumers
 * read the theme-active variant via `var(--…)` and lets the browser
 * cascade pick the right one for the current `[data-theme]`.
 *
 * Each pair was sized for WCAG 2.2 AA syntax-highlight contrast against
 * the code background. Dark-theme code panels sit at L 11-13 (slightly
 * darker than `--bg-primary` at L 15) to differentiate code chrome from
 * page chrome; light-theme panels sit at L 95-97 against the L 99 page
 * background for the same reason.
 */
export interface CodeChromePair {
  dark: string
  light: string
}

export const CODE_CHROME: Record<string, CodeChromePair> = {
  bg: { dark: 'oklch(11% 0.005 125)', light: 'oklch(97% 0.003 125)' },
  bgBlock: { dark: 'oklch(13% 0.005 125)', light: 'oklch(95% 0.005 125)' },
  gutterBorder: { dark: 'oklch(28% 0.005 125)', light: 'oklch(85% 0.008 125)' },
  gutterText: { dark: 'oklch(45% 0.005 125)', light: 'oklch(55% 0.005 125)' },
  activeLineBg: { dark: 'oklch(18% 0.005 125)', light: 'oklch(92% 0.005 125)' },
}

/** Theme-aware `var(--code-*)` refs for the keys in `CODE_CHROME`. */
export const CODE_CHROME_VARS = {
  bg: 'var(--code-bg)',
  bgBlock: 'var(--code-bg-block)',
  gutterBorder: 'var(--code-gutter-border)',
  gutterText: 'var(--code-gutter-text)',
  activeLineBg: 'var(--code-active-line-bg)',
} as const

// Custom theme matching the app — shared between FilesTab and FilesPage
export const codeTheme = {
  ...oneDark,
  'pre[class*="language-"]': {
    ...oneDark['pre[class*="language-"]'],
    background: CODE_CHROME_VARS.bg,
    margin: '0',
    padding: '1rem',
    borderRadius: '0',
    fontSize: '0.9em',
  },
  'code[class*="language-"]': {
    ...oneDark['code[class*="language-"]'],
    background: 'transparent',
    fontFamily: "'SF Mono', 'Fira Code', 'JetBrains Mono', monospace",
  },
}
