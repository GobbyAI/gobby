import { readFileSync, readdirSync } from 'node:fs'
import { join } from 'node:path'
import { describe, expect, it } from 'vitest'

const cwd = process.cwd()

const TYPOGRAPHY_ROOTS = ['src/styles', 'src/components']
const SANCTIONED_TOKEN_FILES = new Set(['src/styles/tokens.css'])

function readSource(rel: string): string {
  return readFileSync(join(cwd, rel), 'utf8')
}

function readSessionsSurfaceSource(): string {
  return [
    'src/components/activity/SessionsTab.tsx',
    'src/components/activity/SessionsTabList.tsx',
    'src/components/activity/SessionsTabDetail.tsx',
  ]
    .map(readSource)
    .join('\n')
}

function readTsxSources(rel: string): Array<[string, string]> {
  return readdirSync(join(cwd, rel), { withFileTypes: true }).flatMap((entry) => {
    const child = join(rel, entry.name)
    if (entry.isDirectory()) {
      return entry.name === '__tests__' || entry.name === '__visual__' ? [] : readTsxSources(child)
    }
    return entry.name.endsWith('.tsx') ? [[child, readSource(child)]] : []
  })
}

function sourceFilesUnder(rel: string): string[] {
  return readdirSync(join(cwd, rel), { withFileTypes: true }).flatMap((entry) => {
    const child = join(rel, entry.name)
    if (entry.isDirectory()) {
      return entry.name === '__tests__' || entry.name === '__visual__' ? [] : sourceFilesUnder(child)
    }
    return /\.(?:css|ts|tsx)$/.test(entry.name) ? [child] : []
  })
}

describe('activity-panel typography ladder (#14245)', () => {
  it('keeps live component typography on the shared token ladder', () => {
    const offLadder = TYPOGRAPHY_ROOTS.flatMap(sourceFilesUnder)
      .filter((rel) => !SANCTIONED_TOKEN_FILES.has(rel))
      .flatMap((rel) => {
        const source = readSource(rel)
        const patterns = [
          /font-size\s*:\s*(?:calc\(|var\(--font-size-base\)|[0-9])/gi,
          /fontSize\s*:\s*['"](?:calc\(|[0-9])/g,
          /text-\[(?:length:calc\(|[0-9])/g,
        ]
        return patterns.flatMap((pattern) =>
          Array.from(source.matchAll(pattern), (match) => `${rel}:${match[0]}`),
        )
      })

    expect(offLadder).toEqual([])
  })

  it('exposes shared row-title and row-meta utility classes locked to the ladder', () => {
    const source = readSource('src/components/chat/styles/activity-panel.css')

    expect(source).toMatch(
      /\.activity-row-title\s*{[^}]*font-size:\s*var\(--text-base\)[^}]*font-weight:\s*var\(--font-weight-medium\)/,
    )
    expect(source).toMatch(
      /\.activity-row-meta\s*{[^}]*font-size:\s*var\(--text-sm\)[^}]*font-weight:\s*var\(--font-weight-normal\)/,
    )
  })

  it('uses one shared activity status-bar height and readable title size', () => {
    const rootSource = readSource('src/styles/tokens.css')
    const activitySource = readSource('src/components/chat/styles/activity-panel.css')
    const taskSource = readSource('src/components/tasks/task-execution.css')
    const sessionsSource = readSessionsSurfaceSource()

    // Canonical token lives in :root (src/styles/tokens.css) so .command-bar,
    // .agent-status-bar, .voice-status-bar, and the activity-panel bars all
    // inherit the same height. Inner scopes must not redeclare it.
    expect(rootSource).toContain('--activity-panel-bar-height: 2.75rem')
    expect(activitySource).not.toMatch(/\.activity-panel\s*{[^}]*--activity-panel-bar-height:/)
    expect(activitySource).toMatch(
      /\.activity-panel-status-bar\s*{[^}]*min-height:\s*var\(--activity-panel-bar-height\)/,
    )
    expect(activitySource).toMatch(
      /\.activity-panel-status-bar__title\s*{[^}]*font-size:\s*var\(--text-base\)[^}]*font-weight:\s*var\(--font-weight-medium\)/,
    )
    expect(taskSource).toMatch(
      /\.activity-task-pane-bar\s*{[^}]*min-height:\s*var\(--activity-panel-bar-height,\s*2\.5rem\)/,
    )
    expect(sessionsSource).toContain('activity-panel-status-bar__title')
  })

  it('keeps status-bar controls at desktop visual height on touch devices', () => {
    const rootSource = readSource('src/styles/tokens.css')
    const activitySource = readSource('src/components/chat/styles/activity-panel.css')
    const layoutSource = readSource('src/components/chat/styles/layout.css')
    const statusBarSource = readSource('src/components/chat/AgentStatusBar.tsx')

    expect(rootSource).toContain('--status-bar-control-height: 1.75rem')
    // Status-bar session actions encode the desktop-height-on-touch contract
    // via the Button `dense` prop (min-h-7 with no pointer-coarse promotion).
    expect(statusBarSource).toMatch(/variant="accent"\s+size="sm"\s+dense/)
    expect(layoutSource).toMatch(
      /\.command-bar-btn\s*{[^}]*min-height:\s*var\(--status-bar-control-height\)/,
    )
    expect(activitySource).toMatch(
      /\.activity-panel-tabs\s*{[^}]*padding:\s*0 0\.75rem/,
    )
    expect(activitySource).toMatch(
      /\.activity-filter-button\s*{[^}]*margin-left:\s*auto/,
    )
  })

  it('locks the tasks row title to --text-base / medium', () => {
    const source = readSource('src/components/tasks/task-execution.css')

    expect(source).toMatch(
      /\.activity-task-row-title\s*{[^}]*font-size:\s*var\(--text-base\)[^}]*font-weight:\s*var\(--font-weight-medium\)/,
    )
    expect(source).toMatch(
      /\.activity-task-row-ref\s*{[^}]*font-size:\s*var\(--text-sm\)/,
    )
    expect(source).toMatch(
      /\.activity-task-row\s*{[^}]*font-size:\s*var\(--text-base\)/,
    )
  })

  it('keeps high/critical priority tasks bold while raising the default to medium', () => {
    const source = readSource('src/components/activity/TasksTabModel.ts')

    const match = source.match(
      /PRIORITY_TEXT_WEIGHTS:\s*Record<number,\s*string>\s*=\s*{([\s\S]*?)}/,
    )
    expect(match).not.toBeNull()
    const body = match![1]

    expect(body).toMatch(/0:\s*'var\(--font-weight-semibold\)'/)
    expect(body).toMatch(/1:\s*'var\(--font-weight-semibold\)'/)
    expect(body).toMatch(/2:\s*'var\(--font-weight-medium\)'/)
    expect(body).toMatch(/3:\s*'var\(--font-weight-medium\)'/)
    expect(body).toMatch(/4:\s*'var\(--font-weight-medium\)'/)
    expect(body).not.toMatch(/var\(--font-weight-normal\)/)
  })

  it('routes Sessions/Pipelines/Cron row titles through activity-row-title', () => {
    const sessions = readSessionsSurfaceSource()
    const pipelines = readSource('src/components/activity/PipelinesTab.tsx')
    const cron = readSource('src/components/activity/CronTab.tsx')

    expect(sessions).toContain('activity-row-title')
    expect(pipelines).toContain('activity-row-title')
    expect(cron).toContain('activity-row-title')

    expect(sessions).not.toMatch(/className="text-sm text-foreground truncate"/)
    expect(pipelines).not.toMatch(/className="text-sm text-foreground truncate"/)
    expect(cron).not.toMatch(/className="text-sm text-foreground truncate"/)
  })

  it('routes Pipelines/Cron meta timestamps through activity-row-meta', () => {
    const pipelines = readSource('src/components/activity/PipelinesTab.tsx')
    const cron = readSource('src/components/activity/CronTab.tsx')

    expect(pipelines).toContain('activity-row-meta')
    expect(cron).toContain('activity-row-meta')

    expect(pipelines).not.toMatch(/text-\[10px\] text-muted-foreground shrink-0/)
    expect(cron).not.toMatch(/text-\[10px\] text-muted-foreground tabular-nums/)
  })

  it('locks cron run rows to the meta token', () => {
    const source = readSource('src/components/chat/styles/cron-tab.css')

    expect(source).toMatch(
      /\.cron-tab-run\s*{[^}]*font-size:\s*var\(--text-sm\)[^}]*font-weight:\s*var\(--font-weight-normal\)/,
    )
  })

  it('locks files-tab tree rows to --text-base and meta size to --text-sm', () => {
    const source = readSource('src/components/chat/styles/files-tab.css')

    expect(source).toMatch(
      /\.file-tree-entry\s*{[^}]*font-size:\s*var\(--text-base\)[^}]*font-weight:\s*var\(--font-weight-medium\)/,
    )
    expect(source).toMatch(
      /\.files-tree-item\s*{[^}]*font-size:\s*var\(--text-base\)[^}]*font-weight:\s*var\(--font-weight-medium\)/,
    )
    expect(source).toMatch(
      /\.file-tree-size\s*{[^}]*font-size:\s*var\(--text-sm\)/,
    )
    expect(source).toMatch(
      /\.files-tree-loading\s*{[^}]*font-size:\s*var\(--text-sm\)/,
    )
  })

  it('locks the activity-tab-empty body and provides heading helpers (chat empty-state parity)', () => {
    const source = readSource('src/components/activity/ActivityPanelEmpty.tsx')

    expect(source).toContain('text-[length:var(--text-base)]')
    expect(source).toContain('font-[var(--font-weight-normal)]')
    expect(source).toContain('text-[length:var(--text-xl)]')
    expect(source).toContain('text-[var(--text-secondary)]')
    expect(source).toContain('text-[var(--text-muted)]')
  })

  it('locks the chat empty-state title and copy to the same utility ladder', () => {
    const source = readSource('src/components/chat/MessageList.tsx')
    const commandPaletteSource = readSource('src/components/chat/CommandPalette.tsx')

    expect(source).toContain('chat-empty-state flex flex-col items-center gap-3 text-center')
    expect(source).toContain(
      'chat-empty-state__title text-[length:var(--text-xl)] text-[var(--text-secondary)]',
    )
    expect(source).toContain(
      'chat-empty-state__copy max-w-[26rem] text-[length:var(--text-base)] text-[var(--text-muted)]',
    )
    expect(commandPaletteSource).toContain(
      'command-palette-empty p-6 text-center text-[length:var(--text-sm)] text-[var(--text-muted)]',
    )
  })

  it('keeps TSX typography on the shared ladder', () => {
    for (const [path, source] of readTsxSources('src')) {
      expect(source, path).not.toMatch(/text-\[\d+(?:\.\d+)?px\]/)
      expect(source, path).not.toMatch(
        /fontSize:\s*(?:["']\d+(?:\.\d+)?(?:px|rem)["']|\d+)/,
      )
    }
  })
})
