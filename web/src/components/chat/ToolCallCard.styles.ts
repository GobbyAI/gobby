/**
 * Tool-result and tool-arg blocks pack denser than the canonical chat
 * code block so a long Bash/grep run doesn't dominate the chat. Padding
 * and font are tightened via `customStyle`; the gutter geometry comes
 * from the shared `CodeBlock` component.
 */
export const TOOL_RESULT_CUSTOM_STYLE = {
  margin: 0,
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
