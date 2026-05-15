import { readFileSync } from 'node:fs'
import { join } from 'node:path'
import { describe, expect, it } from 'vitest'

const cwd = process.cwd()

function readSource(rel: string): string {
  return readFileSync(join(cwd, rel), 'utf8')
}

describe('mobile chrome CSS', () => {
  it('loads the app shell stylesheet after base button styles', () => {
    const source = readSource('src/main.tsx')

    expect(source.indexOf("import './styles/buttons.css'")).toBeLessThan(
      source.indexOf("import './styles/app-shell.css'"),
    )
  })

  it('keeps the top app chrome compact on mobile touch viewports', () => {
    const appSource = readSource('src/App.tsx')
    const shellSource = readSource('src/styles/app-shell.css')

    expect(appSource).toContain('className="app-header"')
    expect(appSource).toContain('className="app-brand"')
    expect(appSource).toContain('variant="ghost"')
    expect(appSource).toContain('className="app-menu-button"')
    expect(appSource).toContain('className="app-brand-logo"')
    expect(appSource).toContain('className="app-brand-title"')
    expect(appSource).toContain('className="app-header-actions"')
    expect(appSource).toContain('className="app-health-badge')

    expect(shellSource).toMatch(
      /\.app-menu-button\s*{[^}]*width:\s*2\.25rem[^}]*min-height:\s*2\.25rem[^}]*border-color:\s*transparent/,
    )
    expect(shellSource).toMatch(
      /\.app-header-actions\s*{[^}]*--control-row-height:\s*var\(--status-bar-control-height\)[^}]*flex-wrap:\s*nowrap/,
    )
    expect(shellSource).toMatch(
      /@media \(max-width:\s*768px\)\s*{[\s\S]*\.app-brand-title\s*{[^}]*font-size:\s*1\.25rem/,
    )
  })

  it('keeps activity panel filter toolbars to one compact row', () => {
    const source = readSource('src/components/chat/styles/activity-panel.css')

    expect(source).toMatch(
      /\.activity-panel-toolbar\s*{[^}]*--control-row-height-sm:\s*var\(--status-bar-control-height\)[^}]*flex-wrap:\s*nowrap/,
    )
    expect(source).toMatch(
      /\.activity-panel-search\s*{[^}]*flex:\s*1 1 9rem[^}]*min-width:\s*0[^}]*min-height:\s*var\(--control-row-height-sm\)/,
    )
    expect(source).toMatch(
      /\.activity-panel-toolbar \.btn-sm\s*{[^}]*min-height:\s*var\(--status-bar-control-height\)/,
    )
    expect(source).toMatch(
      /\.activity-panel-toolbar-segmented > \.segmented-control__option\s*{[^}]*padding-inline:\s*0\.55rem/,
    )
    expect(source).toMatch(
      /\.activity-panel-toolbar-segmented\s*{[^}]*font-size:\s*var\(--text-sm\)/,
    )
  })
})
