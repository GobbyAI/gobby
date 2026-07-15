import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'

import { KnowledgeGraph } from '../KnowledgeGraph'

type ForceGraphProps = {
  graphData?: { nodes: Array<{ entity: unknown }>; links: Array<{ type?: string; color?: string }> }
  nodeLabel?: (node: { entity: unknown }) => string
  linkLabel?: (link: { type?: string }) => string
  linkColor?: (link: { color?: string }) => string
}

class MockResizeObserver {
  observe() {}
  unobserve() {}
  disconnect() {}
}

vi.mock('react-force-graph-3d', () => ({
  default: (props: ForceGraphProps) => {
    const nodeLabel = props.graphData?.nodes[0]
      ? props.nodeLabel?.(props.graphData.nodes[0])
      : undefined
  const linkLabel = props.graphData?.links[0]
    ? props.linkLabel?.(props.graphData.links[0])
    : undefined
  const linkColor = props.graphData?.links[0]
    ? props.linkColor?.(props.graphData.links[0])
    : undefined

  return (
    <div
      data-link-color={linkColor}
      data-link-label={linkLabel}
        data-node-label={nodeLabel}
        data-testid="force-graph"
      />
    )
  },
}))

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

describe('KnowledgeGraph', () => {
  it('shows a retryable error when the graph fetch fails', async () => {
    vi.stubGlobal('ResizeObserver', MockResizeObserver)
    const fetchKnowledgeGraph = vi.fn()
      .mockRejectedValueOnce(new Error('network unavailable'))
      .mockResolvedValueOnce({ entities: [], relationships: [] })

    render(
      <KnowledgeGraph
        fetchKnowledgeGraph={fetchKnowledgeGraph}
        fetchEntityNeighbors={vi.fn()}
      />,
    )

    await waitFor(() => {
      expect(screen.getByText('Failed to load knowledge graph')).toBeInTheDocument()
    })
    expect(screen.queryByText('No entities found')).not.toBeInTheDocument()

    fireEvent.click(screen.getByRole('button', { name: 'Retry' }))

    await waitFor(() => expect(screen.getByText('No entities found')).toBeInTheDocument())
    expect(fetchKnowledgeGraph).toHaveBeenCalledTimes(2)
  })

  it('mentions FalkorDB in the empty-state copy', async () => {
    vi.stubGlobal('ResizeObserver', MockResizeObserver)

    render(
      <KnowledgeGraph
        fetchKnowledgeGraph={vi.fn().mockResolvedValue({ entities: [], relationships: [] })}
        fetchEntityNeighbors={vi.fn()}
      />,
    )

    await waitFor(() => expect(screen.getByText('No entities found')).toBeInTheDocument())

    expect(
      screen.getByText('Connect a FalkorDB instance to explore knowledge graph entities and relationships.'),
    ).toBeInTheDocument()
  })

  it('escapes dynamic HTML in graph tooltips', async () => {
    vi.stubGlobal('ResizeObserver', MockResizeObserver)

    render(
      <KnowledgeGraph
        fetchKnowledgeGraph={vi.fn().mockResolvedValue({
          entities: [
            {
              entity_key: 'entity-1',
              name: '<img src=x onerror=alert(1)>',
              entity_type: 'person<script>',
              project_id: null,
              properties: {
                '<b>role</b>': '"admin" & <script>alert(1)</script>',
              },
            },
            {
              entity_key: 'entity-2',
              name: 'target',
              entity_type: 'file',
              project_id: null,
              properties: {},
            },
          ],
          relationships: [
            {
              source_key: 'entity-1',
              target_key: 'entity-2',
              type: '<script>alert(1)</script>',
              properties: {},
            },
          ],
        })}
        fetchEntityNeighbors={vi.fn()}
      />,
    )

    await waitFor(() => expect(screen.getByTestId('force-graph')).toBeInTheDocument())

    const forceGraph = screen.getByTestId('force-graph')
    const nodeTooltip = forceGraph.getAttribute('data-node-label') ?? ''
    const linkTooltip = forceGraph.getAttribute('data-link-label') ?? ''
    const linkColor = forceGraph.getAttribute('data-link-color') ?? ''

    expect(nodeTooltip).toContain('&lt;img src=x onerror=alert(1)&gt;')
    expect(nodeTooltip).toContain('&lt;b&gt;role&lt;/b&gt;')
    expect(nodeTooltip).toContain('&quot;admin&quot; &amp; &lt;script')
    expect(nodeTooltip).not.toContain('<img src=x')
    expect(nodeTooltip).not.toContain('<b>role</b>')
    expect(linkTooltip).toBe('&lt;script&gt;alert(1)&lt;/script&gt;')
    expect(linkColor).toMatch(/^var\(--(?:color-|accent)/)
    expect(linkColor).not.toContain('hsl(')
  })
})
