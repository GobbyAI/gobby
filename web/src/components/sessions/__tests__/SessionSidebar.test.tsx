import { describe, expect, it, vi } from 'vitest'
import { render, screen } from '@testing-library/react'
import { SessionSidebar } from '../SessionSidebar'

describe('SessionSidebar', () => {
  it('preserves the session ref for untitled sessions', () => {
    render(
      <SessionSidebar
        sessions={[
          {
            id: 'session-1',
            ref: 'abc123',
            seq_num: null,
            title: '   ',
            source: 'claude',
            updated_at: '2026-04-14T12:00:00Z',
            session_type: 'terminal',
          } as any,
        ]}
        projects={[]}
        filters={{ source: null, projectId: null, search: '', sortOrder: 'newest' }}
        onFiltersChange={vi.fn()}
        activeSessionId={null}
        isLoading={false}
        onNewChat={vi.fn()}
        onSelectSession={vi.fn()}
        isOpen={true}
        onToggle={vi.fn()}
      />,
    )

    expect(screen.getByText('abc123: New Session')).toBeTruthy()
  })
})
