import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import { TaskCreateForm } from '../TaskCreateForm'

const confirmMock = vi.hoisted(() => vi.fn())

vi.mock('../../../hooks/useConfirmDialog', () => ({
  useConfirmDialog: () => ({ confirm: confirmMock, ConfirmDialogElement: null }),
}))

function renderForm(onClose = vi.fn()) {
  render(
    <TaskCreateForm
      isOpen
      tasks={[]}
      onSubmit={vi.fn()}
      onClose={onClose}
    />,
  )
  const dialog = screen.getByRole('dialog')
  return { onClose, dialog, backdrop: dialog.previousElementSibling as HTMLElement }
}

describe('TaskCreateForm dismissal', () => {
  beforeEach(() => {
    confirmMock.mockReset()
  })

  it.each(['Escape', 'backdrop'])('dismisses a clean form via %s without confirmation', async (method) => {
    const { onClose, dialog, backdrop } = renderForm()

    if (method === 'Escape') fireEvent.keyDown(dialog, { key: 'Escape' })
    else await userEvent.click(backdrop)

    expect(confirmMock).not.toHaveBeenCalled()
    await waitFor(() => expect(onClose).toHaveBeenCalledOnce())
  })

  it.each(['Escape', 'backdrop'])('keeps a dirty form open when %s discard is cancelled', async (method) => {
    confirmMock.mockResolvedValue(false)
    const { onClose, dialog, backdrop } = renderForm()
    await userEvent.type(screen.getByLabelText(/Title/), 'Keep this draft')

    if (method === 'Escape') fireEvent.keyDown(dialog, { key: 'Escape' })
    else await userEvent.click(backdrop)

    await waitFor(() => expect(confirmMock).toHaveBeenCalledOnce())
    expect(confirmMock).toHaveBeenCalledWith({
      title: 'Discard task draft?',
      description: 'Your task draft has unsaved changes.',
      confirmLabel: 'Discard',
      destructive: true,
    })
    expect(onClose).not.toHaveBeenCalled()
  })

  it('closes a dirty form after discard is confirmed', async () => {
    confirmMock.mockResolvedValue(true)
    const { onClose, dialog } = renderForm()
    await userEvent.type(screen.getByLabelText(/Title/), 'Discard this draft')

    fireEvent.keyDown(dialog, { key: 'Escape' })

    await waitFor(() => expect(onClose).toHaveBeenCalledOnce())
  })
})
