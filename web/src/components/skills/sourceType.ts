export type SourceType = 'github' | 'zip' | 'local' | 'unknown'

function isGithubHttpUrl(source: string): boolean {
  try {
    const url = new URL(source)
    return (url.protocol === 'https:' || url.protocol === 'http:') && url.hostname === 'github.com'
  } catch {
    return false
  }
}

export function detectSourceType(source: string): SourceType {
  const s = source.trim()
  if (s.startsWith('github:') || isGithubHttpUrl(s)) return 'github'
  if (s.endsWith('.zip')) return 'zip'
  if (s.startsWith('/') || s.startsWith('./') || s.startsWith('../') || s.startsWith('~')) return 'local'
  if (s.includes('://')) return 'unknown'
  if (s.includes('/') && !s.startsWith('http')) return 'github'
  return 'unknown'
}
