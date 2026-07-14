import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it, vi } from 'vitest'

import type { OpenFile } from '../../hooks/useFiles'
import { FilesPage } from '../FilesPage'

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
