import type { CSSProperties } from 'react'

function normalizeGitHubColor(color?: string | null): string | undefined {
  const value = color?.trim().replace(/^#/, '')
  return value && /^[0-9a-fA-F]{6}$/.test(value) ? `#${value}` : undefined
}

export function issueLabelStyle(color?: string | null): CSSProperties {
  const hex = normalizeGitHubColor(color)
  return hex
    ? {
        backgroundColor: `${hex}20`,
        borderColor: `${hex}40`,
        color: hex,
      }
    : {}
}

export function pullRequestLabelStyle(color?: string | null): CSSProperties {
  const hex = normalizeGitHubColor(color)
  return hex ? { borderColor: hex } : {}
}
