// Style-debt ratchet allowlist — pure bans and pinned sanctioned floors for
// legacy styling idioms (see docs/guides/frontend-style-guide.md).
//
// Attrition contract: allowance entries may only be DELETED or DECREASED.
// Never add an entry or increase a count. When you migrate a file (onto
// components/ui primitives + Tailwind utilities), the stale-count check in
// styleRatchet.test.ts forces you to shrink its entry here so the ratchet can
// never loosen.

// `btn`/`btn-*` class tokens per file (string literals in ts/tsx). Empty:
// the .btn system is retired — this is a pure ban.
export const BTN_CLASS_ALLOWLIST: Record<string, number> = {};

export type RawElement = "button" | "input" | "select" | "textarea";

// Raw interactive JSX elements per file (components/ui itself is exempt —
// primitives have to render the real element).
export const RAW_ELEMENT_ALLOWLIST: Record<
  RawElement,
  Record<string, number>
> = {
  button: {
    "src/components/chat/ChatInput.tsx": 1, // moat 05198494: composer icon button
    "src/components/chat/ChatInputModelControls.tsx": 1, // moat 05198494: composer icon button
    "src/components/chat/ChatInputPrimaryButton.tsx": 1, // moat 05198494: composer icon button
    "src/components/chat/ChatInputQueuedFiles.tsx": 2, // moat 05198494: composer icon buttons
  },
  input: {},
  select: {},
  textarea: {},
};

// `const *_CLS =` style-constant declarations per file.
export const CLS_CONSTANT_ALLOWLIST: Record<string, number> = {};

// The complete recorded stylesheet set. New .css files are banned outright.
export const CSS_FILE_ALLOWLIST: readonly string[] = [
  "src/styles/accessibility.css",
  "src/styles/base.css",
  "src/styles/index.css",
  "src/styles/markdown.css",
  "src/styles/tailwind-theme.css",
  "src/styles/tokens.css",
];

// `!important` occurrences per file.
export const IMPORTANT_ALLOWLIST: Record<string, number> = {
  // The voice `animation: none` relocated in 5.2 beats inline animation styles.
  "src/styles/accessibility.css": 1,
  // Four reduced-motion overrides beat later-layer utilities and inline animations; the
  // tool-code-surface background relocated in 5.3 beats react-syntax-highlighter's inline style.
  "src/styles/base.css": 5,
};

// Exact total lines across the recorded infrastructure stylesheets. Any infra CSS change must
// update this pin consciously in the same commit.
export const CSS_TOTAL_LINE_PIN = 876;
