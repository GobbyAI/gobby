import { readFileSync } from 'node:fs'
import { join } from 'node:path'
import { describe, expect, it } from 'vitest'
import { render } from '@testing-library/react'
import {
  ActivityPanelEmpty,
  ArtifactsEmptyIcon,
  CanvasEmptyIcon,
  ChangesEmptyIcon,
  PlansEmptyIcon,
} from '../ActivityPanelEmpty'

const cwd = process.cwd()

function readSource(rel: string): string {
  return readFileSync(join(cwd, rel), 'utf8')
}

describe('ActivityPanelEmpty (#14246)', () => {
  it('renders icon, heading, and body slots inside .activity-tab-empty', () => {
    const { container } = render(
      <ActivityPanelEmpty
        icon={<svg data-testid="empty-icon" />}
        heading="Plans"
        body="Plans appear here when the agent proposes one for review"
      />,
    )

    const root = container.querySelector('.activity-tab-empty')
    expect(root).not.toBeNull()
    expect(root?.querySelector('.activity-tab-empty__icon')).not.toBeNull()
    expect(root?.querySelector('.activity-tab-empty__heading')?.textContent).toBe('Plans')
    expect(root?.querySelector('.activity-tab-empty__body')?.textContent).toBe(
      'Plans appear here when the agent proposes one for review',
    )
    expect(root?.querySelector('[data-testid="empty-icon"]')).not.toBeNull()
  })

  it('marks the icon decorative for assistive tech', () => {
    const { container } = render(
      <ActivityPanelEmpty
        icon={<svg />}
        heading="Artifacts"
        body="Artifacts appear here when code, text, or plans are generated"
      />,
    )

    const iconWrap = container.querySelector('.activity-tab-empty__icon')
    expect(iconWrap?.getAttribute('aria-hidden')).toBe('true')
  })

  it('exposes one icon per panel using consistent stroke styling', () => {
    const renders = [
      render(<PlansEmptyIcon />),
      render(<ArtifactsEmptyIcon />),
      render(<ChangesEmptyIcon />),
      render(<CanvasEmptyIcon />),
    ]

    for (const r of renders) {
      const svg = r.container.querySelector('svg')
      expect(svg).not.toBeNull()
      expect(svg?.getAttribute('stroke')).toBe('currentColor')
      expect(svg?.getAttribute('fill')).toBe('none')
      expect(svg?.getAttribute('width')).toBe('48')
      expect(svg?.getAttribute('height')).toBe('48')
    }
  })

  describe('panel migrations', () => {
    it('PlansTab uses ActivityPanelEmpty with the spec heading + body', () => {
      const source = readSource('src/components/activity/PlansTab.tsx')
      expect(source).toContain('ActivityPanelEmpty')
      expect(source).toContain('PlansEmptyIcon')
      expect(source).toContain('heading="Plans"')
      expect(source).toContain(
        'body="Plans appear here when the agent proposes one for review"',
      )
      expect(source).not.toMatch(/No plans yet/)
      expect(source).not.toMatch(/text-xs text-muted-foreground mt-1/)
    })

    it('ArtifactsTab uses ActivityPanelEmpty with the spec heading + body', () => {
      const source = readSource('src/components/activity/ArtifactsTab.tsx')
      expect(source).toContain('ActivityPanelEmpty')
      expect(source).toContain('ArtifactsEmptyIcon')
      expect(source).toContain('heading="Artifacts"')
      expect(source).toContain(
        'body="Artifacts appear here when code, text, or plans are generated"',
      )
      expect(source).not.toMatch(/No artifacts found/)
    })

    it('FileChangesTab uses ActivityPanelEmpty with the spec heading + body', () => {
      const source = readSource('src/components/activity/FileChangesTab.tsx')
      expect(source).toContain('ActivityPanelEmpty')
      expect(source).toContain('ChangesEmptyIcon')
      expect(source).toContain('heading="Changes"')
      expect(source).toContain(
        'body="Changes appear here as files are modified during the session"',
      )
      expect(source).not.toMatch(/No file changes detected/)
    })

    it('CanvasTab uses ActivityPanelEmpty with the spec heading + body and no inline icon', () => {
      const source = readSource('src/components/activity/CanvasTab.tsx')
      expect(source).toContain('ActivityPanelEmpty')
      expect(source).toContain('CanvasEmptyIcon')
      expect(source).toContain('heading="A2UI Canvas"')
      expect(source).toContain(
        'body="Interactive surfaces appear here when generated"',
      )
      expect(source).not.toMatch(/function CanvasEmptyIcon/)
      expect(source).not.toMatch(/chat-empty-state/)
    })

    it('strips the trailing period from every empty-state body line', () => {
      const sources = [
        readSource('src/components/activity/PlansTab.tsx'),
        readSource('src/components/activity/ArtifactsTab.tsx'),
        readSource('src/components/activity/FileChangesTab.tsx'),
        readSource('src/components/activity/CanvasTab.tsx'),
      ]
      for (const source of sources) {
        const match = source.match(/body="([^"]+)"/)
        expect(match).not.toBeNull()
        expect(match![1].endsWith('.')).toBe(false)
      }
    })

    it('keeps Sessions empty-state bylines punctuation-free', () => {
      const source = readSource('src/components/activity/SessionsTab.tsx')
      expect(source).toContain('hint = "Matching sessions will appear here"')
      expect(source).toContain('? "No sessions match these filters"')
      expect(source).not.toContain('Matching sessions will appear here.')
      expect(source).not.toContain('No sessions match these filters.')
    })
  })

  describe('typography ladder hierarchy', () => {
    it('matches the chat empty-state look: heading --text-xl / secondary, body --text-base / muted', () => {
      const source = readSource('src/components/chat/styles/empty-state.css')

      expect(source).toMatch(
        /\.activity-tab-empty__heading\s*{[^}]*font-size:\s*var\(--text-xl\)[^}]*color:\s*var\(--text-secondary\)/,
      )
      expect(source).toMatch(
        /\.activity-tab-empty__body\s*{[^}]*font-size:\s*var\(--text-base\)[^}]*color:\s*var\(--text-muted\)/,
      )
    })

    it('routes every panel through ActivityPanelEmpty (no loose .activity-tab-empty literals)', () => {
      const sources = [
        readSource('src/components/activity/TasksTab.tsx'),
        readSource('src/components/activity/SessionsTab.tsx'),
        readSource('src/components/activity/PipelinesTab.tsx'),
        readSource('src/components/activity/CronTab.tsx'),
        readSource('src/components/activity/FilesTab.tsx'),
        readSource('src/components/activity/TracesTab.tsx'),
        readSource('src/components/activity/FileChangesTab.tsx'),
        readSource('src/components/activity/PlansTab.tsx'),
        readSource('src/components/activity/ArtifactsTab.tsx'),
        readSource('src/components/activity/CanvasTab.tsx'),
      ]
      for (const source of sources) {
        expect(source).not.toMatch(/className="activity-tab-empty"/)
      }
    })
  })
})
