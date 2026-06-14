import { render, screen, waitFor } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'

import { KnowledgeGraph } from '../KnowledgeGraph'

class MockResizeObserver {
  observe() {}
  unobserve() {}
  disconnect() {}
}

vi.mock('react-force-graph-3d', () => ({
  default: () => <div data-testid="force-graph" />,
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
})
