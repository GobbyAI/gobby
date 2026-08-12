import {
  oneDark,
  oneLight,
} from "react-syntax-highlighter/dist/esm/styles/prism";

/**
 * Code-chrome palette for syntax-highlighted blocks and the CodeMirror
 * editor.
 *
 * Source of truth: this module exports the typed `{ dark, light }` OKLCH
 * pairs in `CODE_CHROME` and the matching `var(--…)` refs in
 * `CODE_CHROME_VARS`. The same values are mirrored as `--code-*` CSS
 * custom properties in `web/src/styles/tokens.css`, which lets consumers
 * read the theme-active variant via `var(--…)` and lets the browser
 * cascade pick the right one for the current `[data-theme]`.
 *
 * Each pair was sized for WCAG 2.2 AA syntax-highlight contrast against
 * the code background. Dark-theme code panels sit at L 11-13 (slightly
 * darker than `--bg-primary` at L 15) to differentiate code chrome from
 * page chrome; light-theme panels sit at L 95-97 against the L 99 page
 * background for the same reason.
 *
 * `gutterText` is the one *text* token here, so it carries the 4.5:1 AA
 * floor (vs the surface/divider tokens, which do not). It is set to the
 * `--text-muted` lightness in each theme — dark L 62 ≈ 5.6:1, light L 48
 * ≈ 6.0:1 against `--code-bg` — so the CodeMirror gutter reads identically
 * to the muted line numbers used elsewhere. See `lib/colorContrast.ts` and
 * the `codeBlockTheme.contrast` test, which gate this. (#17153)
 */
export interface CodeChromePair {
  dark: string;
  light: string;
}

export const CODE_CHROME: Record<string, CodeChromePair> = {
  bg: { dark: "oklch(11% 0.005 125)", light: "oklch(97% 0.003 125)" },
  bgBlock: { dark: "oklch(13% 0.005 125)", light: "oklch(95% 0.005 125)" },
  gutterBorder: { dark: "oklch(28% 0.005 125)", light: "oklch(85% 0.008 125)" },
  gutterText: { dark: "oklch(62% 0.005 125)", light: "oklch(48% 0.005 125)" },
  activeLineBg: { dark: "oklch(18% 0.005 125)", light: "oklch(92% 0.005 125)" },
};

/** Theme-aware `var(--code-*)` refs for the keys in `CODE_CHROME`. */
export const CODE_CHROME_VARS = {
  bg: "var(--code-bg)",
  bgBlock: "var(--code-bg-block)",
  gutterBorder: "var(--code-gutter-border)",
  gutterText: "var(--code-gutter-text)",
  activeLineBg: "var(--code-active-line-bg)",
} as const;

/**
 * Canonical typography + chrome geometry for code surfaces. Both the
 * view-only `CodeBlock` (Prism via react-syntax-highlighter) and the
 * editing `CodeMirrorEditor` widget read from this so code opened in
 * CodeMirror and the same code rendered inline via CodeBlock are
 * visually indistinguishable except for editing affordances.
 */
export const CODE_CHROME_TYPOGRAPHY = {
  // The design-system mono token (JetBrains Mono Variable first), so file
  // viewing, editing, and markdown code fences all render the same face —
  // a hand-rolled stack here put SF Mono first and diverged from fences.
  fontFamily: "var(--font-mono)",
  fontSize: "var(--text-base)",
  padding: "1rem",
  borderRadius: "0",
} as const;

/**
 * Canonical line-number gutter style for view-only code blocks. File
 * viewers such as FilesTab override `minWidth` to `3em` via the
 * `lineNumberMinWidth` prop on `CodeBlock` to fit 4-digit line numbers.
 */
export const lineNumberStyle = {
  minWidth: "2.5em",
  paddingRight: "1em",
  textAlign: "right" as const,
  userSelect: "none" as const,
  color: "var(--text-muted)",
  fontStyle: "italic" as const,
};

type PrismStyle = Record<string, Record<string, string>>;

/**
 * Canonical Prism theme for view-only code blocks. The token palette is
 * theme-aware: `oneDark` under `[data-theme="dark"]`, `oneLight` under
 * `[data-theme="light"]` — without this, dark-theme grays render on the
 * light code background and become unreadable. The brand-tinted
 * background stays a `var(--code-bg)` ref so it still cascades per theme.
 * Tool-result and tool-arg blocks pass `customStyle` overrides for
 * denser padding/font; the line-number gutter style is shared.
 */
function buildCodeBlockTheme(base: PrismStyle): PrismStyle {
  return {
    ...base,
    'pre[class*="language-"]': {
      ...base['pre[class*="language-"]'],
      background: CODE_CHROME_VARS.bg,
      margin: "0",
      padding: CODE_CHROME_TYPOGRAPHY.padding,
      borderRadius: CODE_CHROME_TYPOGRAPHY.borderRadius,
      fontSize: CODE_CHROME_TYPOGRAPHY.fontSize,
      // One Dark/Light ship a `0 1px` glyph text-shadow that reads as a
      // muddy drop shadow on code; the design system bans decorative
      // shadows (hierarchy from type/space, not effects).
      textShadow: "none",
    },
    'code[class*="language-"]': {
      ...base['code[class*="language-"]'],
      background: "transparent",
      fontFamily: CODE_CHROME_TYPOGRAPHY.fontFamily,
      textShadow: "none",
      // One Dark/Light pin `white-space: pre` on the inner <code>, which beats
      // any pre-wrap a consumer sets on the surrounding <pre> — mono content
      // then lays out at full line width and gets clipped by overflow-hidden
      // ancestors (#19186). Inherit instead, so each surface's pre-level
      // customStyle decides the wrap policy (tool cards wrap; file viewers and
      // desktop fences keep `pre` + scroll).
      whiteSpace: "inherit",
    },
  };
}

export const codeBlockThemeDark = buildCodeBlockTheme(oneDark as PrismStyle);
export const codeBlockThemeLight = buildCodeBlockTheme(oneLight as PrismStyle);

/** Resolve the Prism theme for the active app theme. */
export function getCodeBlockTheme(theme: "light" | "dark"): PrismStyle {
  return theme === "light" ? codeBlockThemeLight : codeBlockThemeDark;
}

/** Back-compat default (dark). Prefer `getCodeBlockTheme(resolvedTheme)`. */
export const codeBlockTheme = codeBlockThemeDark;
