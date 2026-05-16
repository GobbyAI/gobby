/**
 * Tool-result and tool-arg blocks pack denser than the canonical chat
 * code block so a long Bash/grep run doesn't dominate the chat. Padding
 * and font are tightened via `customStyle`; the gutter geometry comes
 * from the shared `CodeBlock` component.
 *
 * `background: transparent` overrides the shared code theme's
 * `var(--code-bg)` pre fill so the result sits directly on the bordered
 * tool card rather than painting a second, off-shade slab on top of it.
 * The theme-aware syntax palette and `textShadow: none` come from
 * `buildCodeBlockTheme` and are unaffected. File viewers keep the code
 * surface — they don't use this constant.
 */
export const TOOL_RESULT_CUSTOM_STYLE = {
  margin: 0,
  background: 'transparent',
  padding: '0.75rem',
  fontSize: '0.75rem',
  borderRadius: '0.25rem',
  maxHeight: '24rem',
  overflowY: 'auto' as const,
  overflowX: 'hidden' as const,
  whiteSpace: 'pre-wrap' as const,
  overflowWrap: 'anywhere' as const,
}

export const TOOL_ERROR_PRE_CLASS =
  'bg-destructive/30 rounded p-2 whitespace-pre-wrap break-words ' +
  'overflow-x-hidden text-destructive-foreground'
