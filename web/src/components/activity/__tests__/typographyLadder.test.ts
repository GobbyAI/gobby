import { readFileSync } from 'node:fs'
import { join } from 'node:path'
import { describe, expect, it } from 'vitest'

const cwd = process.cwd()

function readSource(rel: string): string {
  return readFileSync(join(cwd, rel), 'utf8')
}

describe('activity-panel typography ladder (#14245)', () => {
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
    const activitySource = readSource('src/components/chat/styles/activity-panel.css')
    const taskSource = readSource('src/components/tasks/task-execution.css')
    const sessionsSource = readSource('src/components/activity/SessionsTab.tsx')

    expect(activitySource).toContain('--activity-panel-bar-height: 2.5rem')
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
    const sessions = readSource('src/components/activity/SessionsTab.tsx')
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
    const source = readSource('src/components/chat/styles/empty-state.css')

    expect(source).toMatch(
      /\.activity-tab-empty\s*{[^}]*font-size:\s*var\(--text-base\)[^}]*font-weight:\s*var\(--font-weight-normal\)/,
    )
    expect(source).toMatch(
      /\.activity-tab-empty__heading\s*{[^}]*font-size:\s*var\(--text-xl\)[^}]*color:\s*var\(--text-secondary\)/,
    )
    expect(source).toMatch(
      /\.activity-tab-empty__body\s*{[^}]*font-size:\s*var\(--text-base\)[^}]*color:\s*var\(--text-muted\)/,
    )
  })
})
