import { act, fireEvent, render, screen } from '@testing-library/react'
import type { ForwardedRef } from 'react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { CodeGraphExplorer } from '../CodeGraphExplorer'

const codeGraphMock = vi.hoisted(() => ({
  fetchFileGraph: vi.fn(),
  expandFile: vi.fn(),
  expandSymbol: vi.fn(),
  fetchBlastRadius: vi.fn(),
  searchSymbols: vi.fn(),
}))

vi.mock('../../../hooks/useCodeGraph', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../../../hooks/useCodeGraph')>()),
  useCodeGraph: () => codeGraphMock,
}))

vi.mock('react-force-graph-3d', async () => {
  const React = await import('react')
  return {
    default: React.forwardRef(function MockForceGraph(
      props: { graphData: { nodes: Array<{ id: string; name: string }> } },
      ref: ForwardedRef<unknown>,
    ) {
      React.useImperativeHandle(ref, () => ({
        d3Force: () => null,
        d3ReheatSimulation: vi.fn(),
        graphData: () => props.graphData,
        cameraPosition: vi.fn(),
        zoomToFit: vi.fn(),
      }))
      return (
        <div data-testid="force-graph">
          {props.graphData.nodes.map((node) => <span key={node.id}>{node.name}</span>)}
        </div>
      )
    }),
  }
})

vi.mock('three-spritetext', () => ({
  default: class SpriteText {
    color = ''
    fontFace = ''
    textHeight = 0
    backgroundColor = ''
    borderColor = ''
    borderWidth = 0
    borderRadius = 0
    padding: unknown[] = []
    scale = { clone: () => ({}), copy: vi.fn(), set: vi.fn() }
  },
}))

class MockResizeObserver {
  observe() {}
  disconnect() {}
}

function deferred<T>() {
  let resolve!: (value: T) => void
  const promise = new Promise<T>((resolvePromise) => {
    resolve = resolvePromise
  })
  return { promise, resolve }
}

const graph = (id: string, name: string) => ({
  nodes: [{ id, name, type: 'function' }],
  links: [],
})

describe('CodeGraphExplorer', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    localStorage.removeItem('gobby-cg-limit')
    vi.stubGlobal('ResizeObserver', MockResizeObserver)
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue({ ok: false }))
    codeGraphMock.expandFile.mockResolvedValue(null)
    codeGraphMock.expandSymbol.mockResolvedValue(null)
    codeGraphMock.fetchBlastRadius.mockResolvedValue(null)
    codeGraphMock.searchSymbols.mockResolvedValue([])
  })

  afterEach(() => {
    vi.useRealTimers()
    vi.unstubAllGlobals()
    localStorage.removeItem('gobby-cg-limit')
  })

  it('uses the persisted graph limit without fetching configuration', () => {
    localStorage.setItem('gobby-cg-limit', '80')
    codeGraphMock.fetchFileGraph.mockResolvedValue(graph('base', 'Base node'))

    render(<CodeGraphExplorer projectId="project" />)

    expect(codeGraphMock.fetchFileGraph).toHaveBeenCalledWith('project', 80)
    expect(fetch).not.toHaveBeenCalled()
  })

  it('keeps the selected project graph when an older request resolves last', async () => {
    const projectA = deferred<ReturnType<typeof graph>>()
    const projectB = deferred<ReturnType<typeof graph>>()
    codeGraphMock.fetchFileGraph.mockImplementation((projectId: string) =>
      projectId === 'project-a' ? projectA.promise : projectB.promise,
    )

    const { rerender } = render(<CodeGraphExplorer projectId="project-a" />)
    rerender(<CodeGraphExplorer projectId="project-b" />)

    await act(async () => {
      projectB.resolve(graph('b', 'Project B node'))
    })
    expect(screen.getByText('Project B node')).toBeInTheDocument()

    await act(async () => {
      projectA.resolve(graph('a', 'Project A node'))
    })
    expect(screen.getByText('Project B node')).toBeInTheDocument()
    expect(screen.queryByText('Project A node')).not.toBeInTheDocument()
  })

  it('keeps search results for the newest query', async () => {
    vi.useFakeTimers()
    codeGraphMock.fetchFileGraph.mockResolvedValue(graph('base', 'Base node'))
    const queryA = deferred<Array<{ id: string; name: string; type: string }>>()
    const queryB = deferred<Array<{ id: string; name: string; type: string }>>()
    codeGraphMock.searchSymbols.mockImplementation((_projectId: string, query: string) =>
      query === 'alpha' ? queryA.promise : queryB.promise,
    )
    render(<CodeGraphExplorer projectId="project" />)
    const search = screen.getByPlaceholderText('Search')

    fireEvent.change(search, { target: { value: 'alpha' } })
    await act(async () => {
      vi.advanceTimersByTime(300)
    })
    fireEvent.change(search, { target: { value: 'beta' } })
    await act(async () => {
      vi.advanceTimersByTime(300)
      queryB.resolve([{ id: 'b', name: 'Beta result', type: 'function' }])
    })
    expect(screen.getByText('Beta result')).toBeInTheDocument()

    await act(async () => {
      queryA.resolve([{ id: 'a', name: 'Alpha result', type: 'function' }])
    })
    expect(screen.getByText('Beta result')).toBeInTheDocument()
    expect(screen.queryByText('Alpha result')).not.toBeInTheDocument()
  })
})
