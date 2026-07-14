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
  CodeMirrorEditor: ({
    content,
    onChange,
  }: {
    content: string
    onChange: (content: string) => void
  }) => (
    <textarea
      aria-label="File contents"
      value={content}
      onChange={(event) => onChange(event.target.value)}
    />
  ),
}))

vi.mock('react-syntax-highlighter', () => ({
  Prism: () => null,
}))

vi.mock('../../shared/MarkdownBody', () => ({
  MarkdownBody: ({ content }: { content: string }) => (
    <div data-testid="markdown-body">{content}</div>
  ),
}))

vi.mock('../../../hooks/useConfirmDialog', () => ({
  useConfirmDialog: () => ({
    confirm: vi.fn(async () => true),
    ConfirmDialogElement: null,
  }),
}))

const defaultFetchImpl = async (_input?: RequestInfo | URL) =>
  new Response(JSON.stringify([]), {
    status: 200,
    headers: { 'Content-Type': 'application/json' },
  })

const fetchMock = vi.fn(defaultFetchImpl)

describe('FilesTab', () => {
  beforeEach(() => {
    // mockReset wipes BOTH call history AND any per-test mockImplementation
    // overrides so the custom /api/files/tree and /api/files/read handlers set
    // in one test don't leak into the next. Re-apply the default empty-list
    // implementation afterwards so tests that rely on the no-op stub still pass.
    fetchMock.mockReset()
    fetchMock.mockImplementation(defaultFetchImpl)
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

  it('isolates project state and aborts project-scoped reads on project switch', async () => {
    vi.mocked(useIsMobile).mockReturnValue(false)
    const requestSignals = new Map<string, AbortSignal>()
    let resolveChildTree: ((response: Response) => void) | undefined
    let resolveFileRead: ((response: Response) => void) | undefined

    fetchMock.mockImplementation(async (input?: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input)
      if (init?.signal) requestSignals.set(url, init.signal)

      if (url.includes('project_id=alpha') && url.endsWith('path=src')) {
        return new Promise<Response>((resolve) => { resolveChildTree = resolve })
      }
      if (url.includes('/api/files/read') && url.includes('project_id=alpha')) {
        return new Promise<Response>((resolve) => { resolveFileRead = resolve })
      }
      if (url.includes('/api/files/git-status')) {
        return Response.json({ files: {} })
      }
      if (url.includes('project_id=alpha') && url.endsWith('path=')) {
        return Response.json([
          { name: 'src', path: 'src', is_dir: true },
          { name: 'alpha.ts', path: 'alpha.ts', is_dir: false, extension: 'ts' },
        ])
      }
      if (url.includes('project_id=beta') && url.endsWith('path=')) {
        return Response.json([
          { name: 'beta.ts', path: 'beta.ts', is_dir: false, extension: 'ts' },
        ])
      }
      return Response.json([])
    })

    const { rerender } = render(<FilesTab projectId="alpha" />)
    await screen.findByText('alpha.ts')

    fireEvent.click(screen.getByText('src'))
    fireEvent.click(screen.getByText('alpha.ts'))
    await waitFor(() => {
      expect(resolveChildTree).toBeTypeOf('function')
      expect(resolveFileRead).toBeTypeOf('function')
    })

    rerender(<FilesTab projectId="beta" />)
    await screen.findByText('beta.ts')

    const alphaSignals = [...requestSignals]
      .filter(([url]) => url.includes('project_id=alpha'))
      .map(([, signal]) => signal)
    expect(alphaSignals).toHaveLength(4)
    alphaSignals.forEach((signal) => expect(signal.aborted).toBe(true))

    resolveChildTree?.(Response.json([
      { name: 'stale.ts', path: 'src/stale.ts', is_dir: false, extension: 'ts' },
    ]))
    resolveFileRead?.(Response.json({ content: 'stale alpha content' }))

    await waitFor(() => {
      expect(screen.queryByText('alpha.ts')).not.toBeInTheDocument()
      expect(screen.queryByText('stale.ts')).not.toBeInTheDocument()
      expect(screen.queryByRole('button', { name: 'Edit' })).not.toBeInTheDocument()
    })
    expect(fetchMock.mock.calls.some(([url]) => String(url).includes('/api/files/write'))).toBe(false)
  })

  it('does not carry an edited file into the next project', async () => {
    vi.mocked(useIsMobile).mockReturnValue(false)
    fetchMock.mockImplementation(async (input?: RequestInfo | URL) => {
      const url = String(input)
      if (url.includes('/api/files/git-status')) return Response.json({ files: {} })
      if (url.includes('/api/files/read')) return Response.json({ content: 'alpha content' })
      if (url.includes('project_id=alpha') && url.endsWith('path=')) {
        return Response.json([
          { name: 'alpha.ts', path: 'alpha.ts', is_dir: false, extension: 'ts' },
        ])
      }
      if (url.includes('project_id=beta') && url.endsWith('path=')) {
        return Response.json([
          { name: 'beta.ts', path: 'beta.ts', is_dir: false, extension: 'ts' },
        ])
      }
      return Response.json([])
    })

    const { rerender } = render(<FilesTab projectId="alpha" />)
    fireEvent.click(await screen.findByText('alpha.ts'))
    fireEvent.click(await screen.findByRole('button', { name: 'Edit' }))
    fireEvent.change(screen.getByRole('textbox', { name: 'File contents' }), {
      target: { value: 'edited alpha content' },
    })
    expect(screen.getByRole('button', { name: 'Save' })).toBeInTheDocument()

    rerender(<FilesTab projectId="beta" />)
    await screen.findByText('beta.ts')

    expect(screen.queryByText('alpha.ts')).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Save' })).not.toBeInTheDocument()
    expect(fetchMock.mock.calls.some(([url]) => String(url).includes('/api/files/write'))).toBe(false)
  })
})
