import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { FilesTab } from '../FilesTab'
import { useIsMobile } from '../../../hooks/useIsMobile'

vi.mock('../../../hooks/useIsMobile', () => ({
  useIsMobile: vi.fn(),
}))

vi.mock('../../chat/artifacts/ResizeHandle', () => ({
  ResizeHandle: ({
    direction,
    horizontalAnchor,
  }: {
    direction?: string
    horizontalAnchor?: string
  }) => (
    <div
      data-testid="resize-handle"
      data-direction={direction ?? 'horizontal'}
      data-horizontal-anchor={horizontalAnchor ?? 'right'}
    />
  ),
}))

vi.mock('../../shared/CodeMirrorEditor', () => ({
  CodeMirrorEditor: () => null,
}))

vi.mock('react-syntax-highlighter', () => ({
  Prism: () => null,
}))

vi.mock('react-markdown', () => ({
  default: () => null,
}))

vi.mock('remark-gfm', () => ({
  default: () => () => undefined,
}))

vi.mock('../../shared/MarkdownComponents', () => ({
  markdownComponents: {},
}))

vi.mock('../../shared/codeTheme', () => ({
  codeTheme: {},
}))

vi.mock('../../../hooks/useConfirmDialog', () => ({
  useConfirmDialog: () => ({
    confirm: vi.fn(async () => true),
    ConfirmDialogElement: null,
  }),
}))

const fetchMock = vi.fn(async (_input?: RequestInfo | URL) =>
  new Response(JSON.stringify([]), {
    status: 200,
    headers: { 'Content-Type': 'application/json' },
  }),
)

describe('FilesTab', () => {
  beforeEach(() => {
    fetchMock.mockClear()
    vi.stubGlobal('fetch', fetchMock)
    window.localStorage.clear()
  })

  afterEach(() => {
    vi.unstubAllGlobals()
    vi.mocked(useIsMobile).mockReset()
  })

  // FilesTab renders a "Loading files..." placeholder until the initial fetch
  // settles. Project null short-circuits the fetch and flips loading=false on
  // the next tick, but we still need waitFor so the layout div mounts.
  async function getRoot(container: HTMLElement) {
    return waitFor(() => {
      const root = container.firstElementChild as HTMLElement | null
      if (!root || root.className.includes('activity-tab-empty')) {
        throw new Error('still loading')
      }
      return root
    })
  }

  it('uses the column layout by default regardless of viewport (ActivityPanel contract)', async () => {
    vi.mocked(useIsMobile).mockReturnValue(false)
    const { container } = render(<FilesTab projectId="test-project" />)

    const root = await getRoot(container)
    expect(root.className).toContain('flex-col')
    expect(root.className).not.toContain('flex-row')
  })

  it('uses the column layout in responsive-split mode on mobile viewports', async () => {
    vi.mocked(useIsMobile).mockReturnValue(true)
    const { container } = render(
      <FilesTab projectId="test-project" layout="responsive-split" />,
    )

    const root = await getRoot(container)
    expect(root.className).toContain('flex-col')
    expect(root.className).not.toContain('flex-row')
  })

  it('uses the row layout in responsive-split mode on desktop', async () => {
    vi.mocked(useIsMobile).mockReturnValue(false)
    const { container } = render(
      <FilesTab projectId="test-project" layout="responsive-split" />,
    )

    const root = await getRoot(container)
    expect(root.className).toContain('flex-row')
    expect(root.className).not.toContain('flex-col')
  })

  it('lets the tree fill the pane when no file is selected (no width style)', async () => {
    vi.mocked(useIsMobile).mockReturnValue(false)
    const { container } = render(
      <FilesTab projectId="test-project" layout="responsive-split" />,
    )

    const root = await getRoot(container)
    const tree = root.firstElementChild as HTMLElement | null
    expect(tree).not.toBeNull()
    expect(tree?.className).toContain('flex-1')
    expect(tree?.style.width).toBe('')
    expect(tree?.style.flex).toBe('')
  })

  it('uses a left-anchored horizontal resize handle for the desktop split path', async () => {
    vi.mocked(useIsMobile).mockReturnValue(false)
    fetchMock.mockImplementation(async (input?: RequestInfo | URL) => {
      const url = String(input)
      if (url.includes('/api/files/git-status')) {
        return new Response(JSON.stringify({ files: {} }), {
          status: 200,
          headers: { 'Content-Type': 'application/json' },
        })
      }
      if (url.includes('/api/files/tree')) {
        return new Response(JSON.stringify([
          { name: 'index.ts', path: 'src/index.ts', is_dir: false, extension: 'ts' },
        ]), {
          status: 200,
          headers: { 'Content-Type': 'application/json' },
        })
      }
      if (url.includes('/api/files/read')) {
        return new Response(JSON.stringify({ content: 'console.log("hello")' }), {
          status: 200,
          headers: { 'Content-Type': 'application/json' },
        })
      }
      return new Response(JSON.stringify([]), {
        status: 200,
        headers: { 'Content-Type': 'application/json' },
      })
    })

    render(<FilesTab projectId="test-project" layout="responsive-split" />)

    await waitFor(() => {
      expect(screen.getByText('index.ts')).toBeTruthy()
    })

    fireEvent.click(screen.getByText('index.ts'))

    await waitFor(() => {
      expect(screen.getByTestId('resize-handle')).toHaveAttribute('data-direction', 'horizontal')
    })
    expect(screen.getByTestId('resize-handle')).toHaveAttribute('data-horizontal-anchor', 'left')
  })
})
