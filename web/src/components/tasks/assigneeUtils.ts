interface KnownAgent {
  id: string
  label: string
  type: 'agent' | 'human' | 'session'
}

export function getBaseUrl(): string {
  return ''
}

export function agentIcon(type: KnownAgent['type']): string {
  if (type === 'agent') return '\u2699'
  if (type === 'human') return '\u{1F464}'
  return '\u{1F4BB}'
}

export function shortId(id: string): string {
  if (id.startsWith('#')) return id
  return id.length > 12 ? `${id.slice(0, 8)}...` : id
}

export function formatAssigneeDisplay(assignee: string | null, agentName: string | null): string {
  if (!assignee) return 'Unassigned'
  if (assignee.includes('+')) {
    return assignee
      .split('+')
      .map((part) => shortId(part))
      .join(' + ')
  }
  return agentName || shortId(assignee)
}
