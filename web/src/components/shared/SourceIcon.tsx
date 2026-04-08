import { SOURCE_COLORS } from './sourceTheme'

export type SourceType = 'claude' | 'gemini' | 'codex' | (string & {})

interface SourceIconProps {
  source: SourceType
  size?: number
}

export function SourceIcon({ source, size = 14 }: SourceIconProps) {
  const color = SOURCE_COLORS[source] || SOURCE_COLORS.default
  const titleId = `source-icon-${source}`

  switch (source) {
    case 'claude':
      return (
        <svg width={size} height={size} viewBox="0 0 24 24" fill="none" className="source-icon source-icon-claude" aria-labelledby={titleId}>
          <title id={titleId}>Claude</title>
          <path d="M12 2L14.5 8.5L21 6L16.5 12L21 18L14.5 15.5L12 22L9.5 15.5L3 18L7.5 12L3 6L9.5 8.5L12 2Z" fill={color} />
        </svg>
      )
    case 'gemini':
      return (
        <svg width={size} height={size} viewBox="0 0 24 24" fill="none" className="source-icon source-icon-gemini" aria-labelledby={titleId}>
          <title id={titleId}>Gemini</title>
          <path d="M12 2L17 7L22 12L17 17L12 22L7 17L2 12L7 7L12 2Z" fill={color} />
        </svg>
      )
    case 'codex':
      return (
        <svg width={size} height={size} viewBox="0 0 24 24" fill="none" className="source-icon source-icon-codex" stroke={color} strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round" aria-labelledby={titleId}>
          <title id={titleId}>Codex</title>
          <polyline points="7 8 3 12 7 16" />
          <polyline points="17 8 21 12 17 16" />
        </svg>
      )
    default:
      return (
        <svg width={size} height={size} viewBox="0 0 24 24" fill="none" className="source-icon" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
          <circle cx="12" cy="12" r="10" />
        </svg>
      )
  }
}

export function sourceColor(source: SourceType): string {
  return SOURCE_COLORS[source] || SOURCE_COLORS.default
}
