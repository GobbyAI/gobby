import type { ComponentProps, ReactNode } from 'react'
import { render, screen } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'

import type { OpenFile } from '../../hooks/useFiles'
import { FilesPage } from '../FilesPage'

vi.mock('../shared/CodeBlock', () => ({
  CodeBlock: ({ children }: { children: ReactNode }) => <pre>{children}</pre>,
}))

vi.mock('../shared/CodeMirrorEditor', () => ({
  CodeMirrorEditor: () => <textarea aria-label="File editor" />,
}))

const truncatedFile: OpenFile = {
  projectId: 'project-1',
  path: 'large.log',
  name: 'large.log',
  content: 'first megabyte',
  originalContent: 'first megabyte',
  editContent: 'first megabyte',
  language: 'plaintext',
  loading: false,
  saving: false,
  error: null,
  dirty: false,
  editing: false,
  image: false,
  binary: false,
  truncated: true,
  mime_type: 'text/plain',
  size: 1_048_577,
}

function makeProps(file: OpenFile): ComponentProps<typeof FilesPage> {
  return {
    projects: [],
    expandedDirs: new Map(),
    expandedProjects: new Set(),
    openFiles: [file],
    activeFileIndex: 0,
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
    gitStatuses: new Map(),
    onFetchDiff: vi.fn(async () => ''),
  }
}

describe('FilesPage truncated files', () => {
  it('shows the too-large state and disables editing and saving', () => {
    const { rerender } = render(<FilesPage {...makeProps(truncatedFile)} />)

    expect(screen.getByRole('status')).toHaveTextContent('File is too large to edit safely.')
    expect(screen.getByRole('button', { name: 'Edit' })).toBeDisabled()

    rerender(
      <FilesPage
        {...makeProps({ ...truncatedFile, editing: true, dirty: true })}
      />,
    )
    expect(screen.getByRole('button', { name: 'Save' })).toBeDisabled()
  })
})
