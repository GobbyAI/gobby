import { computeLineDiff } from '../chat/ToolCallCard.helpers'

export type DiffLineType = 'add' | 'remove' | 'keep' | 'hunk' | 'meta'

export type DiffLine = {
  type: DiffLineType
  /** Display text for this row (already prefixed for synthetic mode). */
  text: string
  oldLineNumber?: number
  newLineNumber?: number
}

function classifyUnifiedLine(line: string): DiffLineType {
  if (line.startsWith('@@')) return 'hunk'
  if (
    line.startsWith('+++') ||
    line.startsWith('---') ||
    line.startsWith('diff ') ||
    line.startsWith('index ')
  ) {
    return 'meta'
  }
  if (line.startsWith('+')) return 'add'
  if (line.startsWith('-')) return 'remove'
  return 'keep'
}

/**
 * Build a normalized DiffLine[] from before/after strings using
 * `computeLineDiff` for the synthetic chat-side path. Output rows are
 * pre-prefixed (`+ `/`- `/`  `) so the inline variant can render them
 * directly.
 */
export function computeSyntheticDiffLines(
  oldStr: string,
  newStr: string,
): DiffLine[] {
  const raw = computeLineDiff(oldStr, newStr)
  let oldLine = 1
  let newLine = 1
  return raw.map(({ type, line }) => {
    const prefix = type === 'add' ? '+ ' : type === 'remove' ? '- ' : '  '
    const out: DiffLine = { type, text: prefix + line }
    if (type !== 'add') {
      out.oldLineNumber = oldLine
      oldLine++
    }
    if (type !== 'remove') {
      out.newLineNumber = newLine
      newLine++
    }
    return out
  })
}

/**
 * Build a normalized DiffLine[] from raw unified-diff text (e.g. `git
 * diff` output). Each line is classified by its leading prefix; old/new
 * line numbers are tracked across hunk headers.
 */
export function parseUnifiedDiffLines(unified: string): DiffLine[] {
  const out: DiffLine[] = []
  let oldLine = 0
  let newLine = 0
  for (const text of unified.split('\n')) {
    if (text.startsWith('@@')) {
      const m = /@@\s+-(\d+)(?:,\d+)?\s+\+(\d+)(?:,\d+)?\s+@@/.exec(text)
      if (m) {
        oldLine = parseInt(m[1], 10)
        newLine = parseInt(m[2], 10)
      }
      out.push({ type: 'hunk', text })
      continue
    }
    const type = classifyUnifiedLine(text)
    if (type === 'meta') {
      out.push({ type, text })
      continue
    }
    if (type === 'add') {
      out.push({ type, text, newLineNumber: newLine })
      newLine++
      continue
    }
    if (type === 'remove') {
      out.push({ type, text, oldLineNumber: oldLine })
      oldLine++
      continue
    }
    out.push({ type, text, oldLineNumber: oldLine, newLineNumber: newLine })
    oldLine++
    newLine++
  }
  return out
}

export function tintForType(type: DiffLineType): string {
  if (type === 'add') {
    return 'color-mix(in srgb, var(--color-success-foreground) 15%, transparent)'
  }
  if (type === 'remove') {
    return 'color-mix(in srgb, var(--color-error) 25%, transparent)'
  }
  if (type === 'hunk') {
    return 'color-mix(in srgb, var(--color-info) 8%, transparent)'
  }
  return 'transparent'
}

export function gutterColorForType(type: DiffLineType): string {
  if (type === 'add') return 'var(--color-success-foreground)'
  if (type === 'remove') return 'var(--color-error)'
  if (type === 'hunk') return 'var(--color-info)'
  return 'var(--text-muted)'
}
