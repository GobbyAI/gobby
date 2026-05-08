import { describe, it, expect, vi } from 'vitest'
import { fireEvent, render, screen, waitFor } from '@testing-library/react'

import { FileChangesTab } from '../FileChangesTab'

vi.mock('../../chat/artifacts/ResizeHandle', () => ({
  ResizeHandle: () => <div data-testid="resize-handle" />,
}))

vi.mock('../../shared/DiffBlock', () => ({
  DiffBlock: ({
    lines,
    path,
  }: {
    lines: { text: string }[]
    path: string
  }) => (
    <div data-testid="diff-view">
      {path}:{lines.map((l) => l.text).join('\n')}
    </div>
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
    expect(fetchDiff).toHaveBeenCalledTimes(2)

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

  it('clears the diff view and logs when fetching a diff fails', async () => {
    const consoleError = vi.spyOn(console, 'error').mockImplementation(() => {})
    const fetchDiff = vi.fn().mockRejectedValue(new Error('boom'))

    render(
      <FileChangesTab
        changedFiles={[{ path: 'src/example.ts', status: 'W' }]}
        fetchDiff={fetchDiff}
      />,
    )

    fireEvent.click(screen.getByRole('button', { name: /example\.ts/i }))

    await waitFor(() => {
      expect(screen.getByTestId('diff-view').textContent).toBe('src/example.ts:')
    })
    expect(consoleError).toHaveBeenCalled()

    consoleError.mockRestore()
  })

  it('deselects a file when it is clicked twice', async () => {
    const fetchDiff = vi.fn().mockResolvedValue('example diff')

    render(
      <FileChangesTab
        changedFiles={[{ path: 'src/example.ts', status: 'W' }]}
        fetchDiff={fetchDiff}
      />,
    )

    const button = screen.getByRole('button', { name: /example\.ts/i })
    fireEvent.click(button)

    await waitFor(() => {
      expect(screen.getByTestId('diff-view').textContent).toBe('src/example.ts:example diff')
    })

    fireEvent.click(button)

    await waitFor(() => {
      expect(screen.queryByTestId('diff-view')).toBeNull()
    })
  })

  it('clears selection when the selected file disappears from changed files', async () => {
    const fetchDiff = vi.fn().mockResolvedValue('example diff')

    const { rerender } = render(
      <FileChangesTab
        changedFiles={[{ path: 'src/example.ts', status: 'W' }]}
        fetchDiff={fetchDiff}
      />,
    )

    fireEvent.click(screen.getByRole('button', { name: /example\.ts/i }))

    await waitFor(() => {
      expect(screen.getByTestId('diff-view').textContent).toBe('src/example.ts:example diff')
    })

    rerender(
      <FileChangesTab
        changedFiles={[{ path: 'src/other.ts', status: 'W' }]}
        fetchDiff={fetchDiff}
      />,
    )

    await waitFor(() => {
      expect(screen.queryByTestId('diff-view')).toBeNull()
    })
  })
})
