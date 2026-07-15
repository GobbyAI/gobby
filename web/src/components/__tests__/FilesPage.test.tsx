import { act, fireEvent, render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import type { ComponentProps, ReactNode } from 'react'
import { describe, expect, it, vi } from 'vitest'

import type { FileEntry, GitStatus, OpenFile, Project } from '../../hooks/useFiles'
import { FilesPage } from '../FilesPage'

vi.mock('../shared/CodeBlock', () => ({
  CodeBlock: ({ children }: { children: ReactNode }) => <pre>{children}</pre>,
}))

vi.mock('../shared/CodeMirrorEditor', () => ({
  CodeMirrorEditor: ({ content }: { content: string }) => (
    <textarea aria-label="File editor" value={content} readOnly />
  ),
}))

const FAILED_FILE: OpenFile = {
  projectId: 'project-1',
  path: 'notes.txt',
  name: 'notes.txt',
  content: 'original',
  originalContent: 'original',
  editContent: 'unsaved draft',
  language: 'text',
  loading: false,
  saving: false,
  error: null,
  saveError: 'Error: temporarily unavailable',
  dirty: true,
  editing: true,
  image: false,
  binary: false,
  mime_type: 'text/plain',
  size: 8,
}

describe('FilesPage save errors', () => {
  it('keeps the editor and retry controls visible and dismisses the error', async () => {
    const user = userEvent.setup()
    const onClearSaveError = vi.fn()
    const onSaveFile = vi.fn()

    render(
      <FilesPage
        projects={[]}
        expandedDirs={new Map()}
        expandedProjects={new Set()}
        openFiles={[FAILED_FILE]}
        activeFileIndex={0}
        loadingDirs={new Set()}
        onExpandProject={vi.fn()}
        onExpandDir={vi.fn()}
        onOpenFile={vi.fn()}
        onCloseFile={vi.fn()}
        onSetActiveFile={vi.fn()}
        getImageUrl={vi.fn(() => '')}
        onToggleEditing={vi.fn()}
        onCancelEditing={vi.fn()}
        onUpdateEditContent={vi.fn()}
        onClearSaveError={onClearSaveError}
        onSaveFile={onSaveFile}
        gitStatuses={new Map()}
        onFetchDiff={vi.fn(async () => '')}
      />,
    )

    expect(screen.getByRole('alert')).toHaveTextContent(
      'Save failed: Error: temporarily unavailable',
    )
    expect(screen.getByRole('textbox', { name: 'File editor' })).toHaveValue(
      'unsaved draft',
    )

    await user.click(screen.getByRole('button', { name: 'Save' }))
    expect(onSaveFile).toHaveBeenCalledWith(0)

    await user.click(screen.getByRole('button', { name: 'Dismiss save error' }))
    expect(onClearSaveError).toHaveBeenCalledWith(0)
  })
})

describe('FilesPage tab closing', () => {
  it('confirms dirty tabs, preserves them on cancel, and closes clean tabs directly', async () => {
    const user = userEvent.setup()
    const onCloseFile = vi.fn()
    const cleanFile: OpenFile = {
      ...FAILED_FILE,
      path: 'clean.txt',
      name: 'clean.txt',
      editContent: 'original',
      dirty: false,
      editing: false,
      saveError: null,
    }

    render(
      <FilesPage
        projects={[]}
        expandedDirs={new Map()}
        expandedProjects={new Set()}
        openFiles={[FAILED_FILE, cleanFile]}
        activeFileIndex={0}
        loadingDirs={new Set()}
        onExpandProject={vi.fn()}
        onExpandDir={vi.fn()}
        onOpenFile={vi.fn()}
        onCloseFile={onCloseFile}
        onSetActiveFile={vi.fn()}
        getImageUrl={vi.fn(() => '')}
        onToggleEditing={vi.fn()}
        onCancelEditing={vi.fn()}
        onUpdateEditContent={vi.fn()}
        onClearSaveError={vi.fn()}
        onSaveFile={vi.fn()}
        gitStatuses={new Map()}
        onFetchDiff={vi.fn(async () => '')}
      />,
    )

    await user.click(screen.getByRole('button', { name: 'Close notes.txt' }))
    expect(screen.getByRole('dialog')).toBeInTheDocument()
    expect(onCloseFile).not.toHaveBeenCalled()

    await user.click(screen.getByRole('button', { name: 'Keep Editing' }))
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
    expect(onCloseFile).not.toHaveBeenCalled()

    await user.click(screen.getByRole('button', { name: 'Close notes.txt' }))
    await user.click(screen.getByRole('button', { name: 'Discard' }))
    expect(onCloseFile).toHaveBeenCalledWith(0)

    await user.click(screen.getByRole('button', { name: 'Close clean.txt' }))
    expect(onCloseFile).toHaveBeenLastCalledWith(1)
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
  })
})

describe('FilesPage keyboard operation', () => {
  it('activates open-file tabs and tree rows with Enter and Space', async () => {
    const user = userEvent.setup()
    const onSetActiveFile = vi.fn()
    const onExpandDir = vi.fn()
    const project: Project = { id: 'project-1', name: 'Gobby', repo_path: '/tmp/gobby' }
    const rootEntries: FileEntry[] = [
      { name: 'src', path: 'src', is_dir: true },
      { name: 'README.md', path: 'README.md', is_dir: false, extension: '.md' },
    ]
    const secondFile: OpenFile = {
      ...FAILED_FILE,
      path: 'other.txt',
      name: 'other.txt',
      dirty: false,
    }

    render(
      <FilesPage
        projects={[project]}
        expandedDirs={new Map([[`${project.id}:`, rootEntries]])}
        expandedProjects={new Set([project.id])}
        openFiles={[FAILED_FILE, secondFile]}
        activeFileIndex={0}
        loadingDirs={new Set()}
        onExpandProject={vi.fn()}
        onExpandDir={onExpandDir}
        onOpenFile={vi.fn()}
        onCloseFile={vi.fn()}
        onSetActiveFile={onSetActiveFile}
        getImageUrl={vi.fn(() => '')}
        onToggleEditing={vi.fn()}
        onCancelEditing={vi.fn()}
        onUpdateEditContent={vi.fn()}
        onClearSaveError={vi.fn()}
        onSaveFile={vi.fn()}
        gitStatuses={new Map()}
        onFetchDiff={vi.fn(async () => '')}
      />,
    )

    const tab = screen.getByRole('tab', { name: /other\.txt/i })
    tab.focus()
    await user.keyboard('{Enter}')
    expect(onSetActiveFile).toHaveBeenCalledWith(1)

    const directory = screen.getByRole('button', { name: /src/i })
    directory.focus()
    await user.keyboard(' ')
    expect(onExpandDir).toHaveBeenCalledWith('project-1', 'src')
  })
})

const DIFF_FILES: OpenFile[] = [
  {
    ...FAILED_FILE,
    path: 'src/first.ts',
    name: 'first.ts',
    content: 'first file content',
    originalContent: 'first file content',
    editContent: null,
    language: 'typescript',
    saveError: null,
    dirty: false,
    editing: false,
    size: 18,
  },
  {
    ...FAILED_FILE,
    path: 'src/second.ts',
    name: 'second.ts',
    content: 'second file content',
    originalContent: 'second file content',
    editContent: null,
    language: 'typescript',
    saveError: null,
    dirty: false,
    editing: false,
    size: 19,
  },
]

const DIFF_GIT_STATUSES = new Map<string, GitStatus>([
  ['project-1', { branch: 'main', files: { 'src/first.ts': 'M', 'src/second.ts': 'M' } }],
])

function createDiffProps(
  activeFileIndex: number,
  onFetchDiff: (projectId: string, path: string) => Promise<string>,
): ComponentProps<typeof FilesPage> {
  return {
    projects: [],
    expandedDirs: new Map(),
    expandedProjects: new Set(),
    openFiles: DIFF_FILES,
    activeFileIndex,
    loadingDirs: new Set(),
    onExpandProject: vi.fn(),
    onExpandDir: vi.fn(),
    onOpenFile: vi.fn(),
    onCloseFile: vi.fn(),
    onSetActiveFile: vi.fn(),
    getImageUrl: vi.fn(),
    onToggleEditing: vi.fn(),
    onCancelEditing: vi.fn(),
    onUpdateEditContent: vi.fn(),
    onClearSaveError: vi.fn(),
    onSaveFile: vi.fn(),
    gitStatuses: DIFF_GIT_STATUSES,
    onFetchDiff,
  }
}

describe('FilesPage diff state', () => {
  it('clears the visible diff when the active file changes', async () => {
    const onFetchDiff = vi.fn((_projectId: string, path: string) =>
      Promise.resolve(path === 'src/first.ts' ? 'first file diff' : 'second file diff'),
    )
    const { rerender } = render(<FilesPage {...createDiffProps(0, onFetchDiff)} />)

    fireEvent.click(screen.getByRole('button', { name: 'Diff' }))
    expect(await screen.findByText('first file diff')).toBeInTheDocument()

    rerender(<FilesPage {...createDiffProps(1, onFetchDiff)} />)

    expect(screen.queryByText('first file diff')).not.toBeInTheDocument()
    expect(screen.getByText('second file content')).toBeInTheDocument()

    fireEvent.click(screen.getByRole('button', { name: 'Diff' }))
    expect(await screen.findByText('second file diff')).toBeInTheDocument()
    expect(onFetchDiff).toHaveBeenLastCalledWith('project-1', 'src/second.ts')
  })

  it('ignores a delayed diff response from the previous active file', async () => {
    let resolveFirst: ((value: string) => void) | undefined
    const onFetchDiff = vi.fn(
      (_projectId: string, path: string) =>
        new Promise<string>((resolve) => {
          if (path === 'src/first.ts') resolveFirst = resolve
        }),
    )
    const { rerender } = render(<FilesPage {...createDiffProps(0, onFetchDiff)} />)

    fireEvent.click(screen.getByRole('button', { name: 'Diff' }))
    rerender(<FilesPage {...createDiffProps(1, onFetchDiff)} />)
    await act(async () => resolveFirst?.('stale first file diff'))

    expect(screen.queryByText('stale first file diff')).not.toBeInTheDocument()
    expect(screen.getByText('second file content')).toBeInTheDocument()
  })
})
