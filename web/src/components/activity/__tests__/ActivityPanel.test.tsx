import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it, vi } from 'vitest'

import { ActivityPanel } from '../ActivityPanel'

vi.mock('../../chat/artifacts/ResizeHandle', () => ({
  ResizeHandle: () => null,
}))

vi.mock('../PlansTab', () => ({
  PlansTab: () => <div>Plans Tab</div>,
}))

vi.mock('../ArtifactsTab', () => ({
  ArtifactsTab: ({ artifacts }: { artifacts: Map<string, { id: string; title: string }> }) => (
    <div>
      <div>Artifacts Tab</div>
      {Array.from(artifacts.values()).map((artifact) => (
        <span key={artifact.id}>{artifact.title}</span>
      ))}
    </div>
  ),
}))

vi.mock('../FileChangesTab', () => ({
  FileChangesTab: () => <div>Changes Tab</div>,
}))

vi.mock('../CanvasTab', () => ({
  CanvasTab: () => <div>Canvas Tab</div>,
}))

vi.mock('../SessionsTab', () => ({
  SessionsTab: () => <div>Sessions Tab</div>,
}))

vi.mock('../PipelinesTab', () => ({
  PipelinesTab: () => <div>Pipelines Tab</div>,
}))

vi.mock('../TasksTab', () => ({
  TasksTab: () => <div>Tasks Tab</div>,
}))

vi.mock('../FilesTab', () => ({
  FilesTab: () => <div>Files Tab</div>,
}))

describe('ActivityPanel', () => {
  it('uses a dropdown menu instead of the mobile icon strip', async () => {
    const onTabChange = vi.fn()

    render(
      <ActivityPanel
        isPinned={true}
        onPinnedChange={vi.fn()}
        panelWidth={320}
        onWidthChange={vi.fn()}
        activeTab="sessions"
        onTabChange={onTabChange}
        artifacts={new Map()}
        activeArtifact={null}
        onOpenArtifact={vi.fn()}
        onCloseArtifact={vi.fn()}
        onSetArtifactVersion={vi.fn()}
        canvasState={null}
        onCloseCanvas={vi.fn()}
        isMobile={true}
      />,
    )

    expect(screen.queryByRole('tablist')).toBeNull()
    expect(screen.getByRole('button', { name: /sessions/i })).toBeInTheDocument()

    await userEvent.click(screen.getByRole('button', { name: /sessions/i }))

    expect(screen.getByRole('menu')).toBeInTheDocument()
    await userEvent.click(screen.getByRole('menuitemradio', { name: /pipelines/i }))

    expect(onTabChange).toHaveBeenCalledWith('pipelines')
    expect(screen.queryByRole('menu')).toBeNull()
  })

  it('uses the same dropdown selector in the pinned desktop panel', async () => {
    const onTabChange = vi.fn()

    render(
      <ActivityPanel
        isPinned={true}
        onPinnedChange={vi.fn()}
        panelWidth={320}
        onWidthChange={vi.fn()}
        activeTab="sessions"
        onTabChange={onTabChange}
        artifacts={new Map()}
        activeArtifact={null}
        onOpenArtifact={vi.fn()}
        onCloseArtifact={vi.fn()}
        onSetArtifactVersion={vi.fn()}
        canvasState={null}
        onCloseCanvas={vi.fn()}
        isMobile={false}
      />,
    )

    expect(screen.queryByRole('tablist')).toBeNull()

    await userEvent.click(screen.getByRole('button', { name: /sessions/i }))
    await userEvent.click(screen.getByRole('menuitemradio', { name: /tasks/i }))

    expect(onTabChange).toHaveBeenCalledWith('tasks')
  })

  it('renders generated artifacts under the Artifacts tab', () => {
    render(
      <ActivityPanel
        isPinned={true}
        onPinnedChange={vi.fn()}
        panelWidth={320}
        onWidthChange={vi.fn()}
        activeTab="artifacts"
        onTabChange={vi.fn()}
        artifacts={new Map([
          [
            'artifact-1',
            {
              id: 'artifact-1',
              type: 'image',
              title: 'logo.png',
              language: 'png',
              versions: [{ content: 'data:image/png;base64,abc', timestamp: new Date() }],
              currentVersionIndex: 0,
            },
          ],
        ])}
        activeArtifact={null}
        onOpenArtifact={vi.fn()}
        onCloseArtifact={vi.fn()}
        onSetArtifactVersion={vi.fn()}
        canvasState={null}
        onCloseCanvas={vi.fn()}
        isMobile={false}
      />,
    )

    expect(screen.getByText('Artifacts Tab')).toBeInTheDocument()
    expect(screen.getByText('logo.png')).toBeInTheDocument()
  })

  it('renders file diffs under the Changes tab', () => {
    render(
      <ActivityPanel
        isPinned={true}
        onPinnedChange={vi.fn()}
        panelWidth={320}
        onWidthChange={vi.fn()}
        activeTab="changes"
        onTabChange={vi.fn()}
        artifacts={new Map()}
        activeArtifact={null}
        onOpenArtifact={vi.fn()}
        onCloseArtifact={vi.fn()}
        onSetArtifactVersion={vi.fn()}
        canvasState={null}
        onCloseCanvas={vi.fn()}
        changedFiles={[{ path: 'src/example.ts', status: 'M' }]}
        isMobile={false}
      />,
    )

    expect(screen.getByText('Changes Tab')).toBeInTheDocument()
  })
})
