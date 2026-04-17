import { describe, it, expect, vi } from 'vitest'
import { fireEvent, render, screen, waitFor } from '@testing-library/react'

import { FileChangesTab } from '../FileChangesTab'

vi.mock('../../chat/artifacts/ResizeHandle', () => ({
  ResizeHandle: () => <div data-testid="resize-handle" />,
}))

vi.mock('../DiffView', () => ({
  DiffView: ({ diff, path }: { diff: string; path: string }) => (
    <div data-testid="diff-view">{path}:{diff}</div>
  ),
}))

describe('FileChangesTab', () => {
  it('ignores stale diff responses when selection changes quickly', async () => {
    let resolveFirst: ((value: string) => void) | undefined
    let resolveSecond: ((value: string) => void) | undefined
    const fetchDiff = vi.fn((path: string) => new Promise<string>((resolve) => {
      if (path === 'src/first.ts') {
        resolveFirst = resolve
      } else {
        resolveSecond = resolve
      }
    }))

    render(
      <FileChangesTab
        changedFiles={[
          { path: 'src/first.ts', status: 'W' },
          { path: 'src/second.ts', status: 'W' },
        ]}
        fetchDiff={fetchDiff}
      />,
    )

    fireEvent.click(screen.getByRole('button', { name: /first\.ts/i }))
    fireEvent.click(screen.getByRole('button', { name: /second\.ts/i }))

    expect(resolveSecond).toBeDefined()
    resolveSecond!('second diff')
    await waitFor(() => {
      expect(screen.getByTestId('diff-view').textContent).toBe('src/second.ts:second diff')
    })

    expect(resolveFirst).toBeDefined()
    resolveFirst!('first diff')
    await waitFor(() => {
      expect(screen.getByTestId('diff-view').textContent).toBe('src/second.ts:second diff')
    })
  })
})
