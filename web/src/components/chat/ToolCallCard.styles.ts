import { oneDark } from 'react-syntax-highlighter/dist/esm/styles/prism'

export const highlighterTheme = {
  ...oneDark,
  'pre[class*="language-"]': {
    ...oneDark['pre[class*="language-"]'],
    background: 'var(--code-bg)',
    margin: '0',
    padding: '0.75rem',
    fontSize: '0.75rem',
  },
  'code[class*="language-"]': {
    ...oneDark['code[class*="language-"]'],
    background: 'transparent',
    fontFamily: "'SF Mono', 'Fira Code', 'JetBrains Mono', monospace",
  },
}

export const lineNumberStyle = {
  minWidth: '2.5em',
  paddingRight: '1em',
  textAlign: 'right' as const,
  userSelect: 'none' as const,
  color: 'var(--text-muted)',
}

export const TOOL_ERROR_PRE_CLASS =
  'bg-destructive/30 rounded p-2 whitespace-pre-wrap break-words ' +
  'overflow-x-hidden text-destructive-foreground'
