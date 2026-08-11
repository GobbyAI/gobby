import { readdirSync, readFileSync, statSync } from 'node:fs'
import { join } from 'node:path'
import { describe, expect, it } from 'vitest'

// #20044: every activity tab follows one toolbar template modeled on Sessions —
// the tab registers selector / Filter / Search / New via
// useRegisterActivityActions and the shared panel header renders them. The only
// in-tab toolbar row allowed is the shared hidden-by-default search bar
// (ActivityToolbarSearchRow in ActivityPanelSearch.tsx).

const ACTIVITY_DIR = join(__dirname, '..')

function walkTsx(dir: string): string[] {
  const files: string[] = []
  for (const entry of readdirSync(dir)) {
    if (entry === '__tests__') continue
    const full = join(dir, entry)
    if (statSync(full).isDirectory()) {
      files.push(...walkTsx(full))
    } else if (entry.endsWith('.tsx')) {
      files.push(full)
    }
  }
  return files
}

describe('activity toolbar template (#20044)', () => {
  it('allows an inline activity-panel-toolbar row only in the shared search bar', () => {
    const offenders = walkTsx(ACTIVITY_DIR)
      .filter((file) => !file.endsWith('ActivityPanelSearch.tsx'))
      .filter((file) =>
        readFileSync(file, 'utf8').includes('className="activity-panel-toolbar"'),
      )
    expect(offenders).toEqual([])
  })

  it('retires the toolbar-segmented variant class entirely', () => {
    const offenders = walkTsx(ACTIVITY_DIR).filter((file) =>
      readFileSync(file, 'utf8').includes('activity-panel-toolbar-segmented'),
    )
    expect(offenders).toEqual([])
  })

  it('registers header actions from every tab that previously owned a toolbar', () => {
    for (const tab of [
      'TracesTab.tsx',
      'AgentsTab.tsx',
      'StagesTab.tsx',
      'PipelinesTab.tsx',
      'TasksTab.tsx',
    ]) {
      const source = readFileSync(join(ACTIVITY_DIR, tab), 'utf8')
      expect(source, tab).toMatch(/useRegisterActivityActions(?:<[^>]+>)?\(/)
    }
  })

  // #19187: labeled toolbar buttons collapse to icon-only through ONE shared
  // mechanism — descendant rules on the panel root in activityPanelClassName.
  // The mobile: rule collapses at the mobile tier (viewport-driven, the
  // .impeccable.md mobile toolbar pattern); the container rule keeps the
  // collapse when the panel is resized narrow on desktop. Label spans carry
  // only the marker class.
  it('collapses toolbar labels from the panel root at the mobile tier and in narrow panels', () => {
    const panelSource = readFileSync(join(ACTIVITY_DIR, 'ActivityPanel.tsx'), 'utf8')
    expect(panelSource).toContain(
      String.raw`mobile:[&_.activity-panel-action-btn\_\_label]:hidden`,
    )
    expect(panelSource).toContain(
      String.raw`@max-[479px]/activity-panel:[&_.activity-panel-action-btn\_\_label]:hidden`,
    )
  })

  it('bans per-surface label-collapse CSS on the label spans themselves', () => {
    // The root rule targets the label via an arbitrary variant
    // (`…/activity-panel:[&_.…\_\_label]:hidden`), so a plain
    // `…/activity-panel:hidden` token can only be a per-span one-off.
    const offenders = walkTsx(ACTIVITY_DIR).filter((file) =>
      readFileSync(file, 'utf8').includes('@max-[479px]/activity-panel:hidden'),
    )
    expect(offenders).toEqual([])
  })
})
