// Style-debt ratchet allowlist — the recorded ceiling for legacy styling
// idioms (see docs/guides/frontend-style-guide.md).
//
// Attrition contract: entries may only be DELETED or DECREASED. Never add an
// entry, never increase a count, never raise the line ceiling. When you migrate
// a file (onto components/ui primitives + Tailwind utilities), the stale-count
// check in styleRatchet.test.ts forces you to shrink its entry here so the
// ratchet can never loosen.

// `btn`/`btn-*` class tokens per file (string literals in ts/tsx). Empty:
// the .btn system is retired — this is a pure ban.
export const BTN_CLASS_ALLOWLIST: Record<string, number> = {}

export type RawElement = 'button' | 'input' | 'select' | 'textarea'

// Raw interactive JSX elements per file (components/ui itself is exempt —
// primitives have to render the real element).
export const RAW_ELEMENT_ALLOWLIST: Record<RawElement, Record<string, number>> = {
  button: {
    'src/components/activity/wiki/WikiAskMode.tsx': 7, // 4.11 deferral: Ask surface replacement
    'src/components/chat/ChatInput.tsx': 1,
    'src/components/chat/ChatInputModelControls.tsx': 1,
    'src/components/chat/ChatInputPrimaryButton.tsx': 1,
    'src/components/chat/ChatInputQueuedFiles.tsx': 2,
  },
  input: {},
  select: {},
  textarea: {
    'src/components/activity/wiki/WikiAskMode.tsx': 1, // 4.11 deferral: Ask surface replacement
  },
}

// `const *_CLS =` style-constant declarations per file.
export const CLS_CONSTANT_ALLOWLIST: Record<string, number> = {}

// The complete recorded stylesheet set. New .css files are banned outright.
export const CSS_FILE_ALLOWLIST: readonly string[] = [
  'src/styles/accessibility.css',
  'src/styles/app-shell.css',
  'src/styles/base.css',
  'src/styles/index.css',
  'src/styles/markdown.css',
  'src/styles/settings-overlay.css',
  'src/styles/tailwind-theme.css',
  'src/styles/tokens.css',
]

// `!important` occurrences per file.
export const IMPORTANT_ALLOWLIST: Record<string, number> = {
  'src/styles/accessibility.css': 1,
  'src/styles/base.css': 5,
}

// Total lines across all recorded stylesheets. The ceiling only moves down;
// once actual drops more than the slack below it, the test demands a tighten.
export const CSS_TOTAL_LINE_CEILING = 1636
export const CSS_LINE_TIGHTEN_SLACK = 200
