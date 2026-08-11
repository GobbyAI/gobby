import type { ComponentProps } from 'react'
import { render, screen } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'

import type { FileEntry, Project } from '../../hooks/useFiles'
import { FilesPage } from '../FilesPage'

vi.mock('../shared/CodeBlock', () => ({
  CodeBlock: () => <pre />,
}))

vi.mock('../shared/CodeMirrorEditor', () => ({
  CodeMirrorEditor: () => <textarea aria-label="File editor" />,
}))

const project: Project = { id: 'p1', name: 'gobby', repo_path: '/repo' }

const rootEntries: FileEntry[] = [
  { name: '.gobby', path: '.gobby', is_dir: true },
  { name: 'wiki', path: 'wiki', is_dir: true },
  { name: 'README.md', path: 'README.md', is_dir: false, extension: '.md' },
]

function makeProps(): ComponentProps<typeof FilesPage> {
  return {
    projects: [project],
    expandedDirs: new Map([['p1:', rootEntries]]),
    expandedProjects: new Set(['p1']),
    openFiles: [],
    activeFileIndex: -1,
    loadingDirs: new Set(),
    onExpandProject: vi.fn(),
    onExpandDir: vi.fn(),
    onOpenFile: vi.fn(),
    onCloseFile: vi.fn(),
    onSetActiveFile: vi.fn(),
    getImageUrl: vi.fn(() => ''),
    onToggleEditing: vi.fn(),
    onCancelEditing: vi.fn(),
    onUpdateEditContent: vi.fn(),
    onSaveFile: vi.fn(),
    onClearSaveError: vi.fn(),
    gitStatuses: new Map(),
    onFetchDiff: vi.fn(async () => ''),
  }
}

describe('FilesPage tree focus rings', () => {
  it('keeps Explorer row focus rings inset so the scroller cannot clip them (#20046)', () => {
    // The base-layer ring sits 2px outside the row; the Explorer pane's
    // overflow clipped it into a stray full-width accent line between rows.
    render(<FilesPage {...makeProps()} />)

    for (const name of ['.gobby', 'wiki', 'README.md']) {
      const row = screen.getByText(name).closest('[role="button"]')
      expect(row).not.toBeNull()
      expect(row).toHaveClass(
        'focus-visible:outline-2',
        'focus-visible:outline-accent',
        'focus-visible:outline-offset-[-2px]',
      )
    }

    const projectRow = screen.getByRole('button', { name: 'Collapse gobby' })
    expect(projectRow).toHaveClass(
      'focus-visible:ring-inset',
      'focus-visible:ring-offset-0',
    )
  })
})
