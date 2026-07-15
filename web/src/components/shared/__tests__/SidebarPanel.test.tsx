import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { SidebarPanel } from '../SidebarPanel'

function renderPanel(isOpen: boolean, onClose = vi.fn()) {
  return render(
    <>
      <button type="button">Open panel</button>
      <SidebarPanel
        isOpen={isOpen}
        onClose={onClose}
        title="Edit agent"
        headerContent={<button type="button">Overview</button>}
        footer={<button type="button">Save</button>}
      >
        <input aria-label="Agent name" />
      </SidebarPanel>
      <button type="button">After panel</button>
    </>,
  )
}

function makeElementsVisible() {
  vi.spyOn(HTMLElement.prototype, 'getClientRects').mockReturnValue(
    [{ width: 1, height: 1 }] as unknown as DOMRectList,
  )
}

afterEach(() => {
  vi.restoreAllMocks()
})

describe('SidebarPanel accessibility', () => {
  it('keeps closed panel contents out of the tab order', () => {
    const { container } = renderPanel(false)

    expect(container.querySelector('[inert]')).toHaveAttribute('aria-hidden', 'true')
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
  })

  it('exposes a labelled modal dialog and traps focus while open', async () => {
    makeElementsVisible()
    const user = userEvent.setup()
    renderPanel(true)

    const dialog = screen.getByRole('dialog', { name: 'Edit agent' })
    expect(dialog).toHaveAttribute('aria-modal', 'true')
    const closeButton = screen.getByRole('button', { name: 'Close panel' })
    const saveButton = screen.getByRole('button', { name: 'Save' })
    await waitFor(() => expect(closeButton).toHaveFocus())

    saveButton.focus()
    await user.tab()
    expect(closeButton).toHaveFocus()

    closeButton.focus()
    await user.tab({ shift: true })
    expect(saveButton).toHaveFocus()
    expect(closeButton).toBeInTheDocument()
  })

  it('restores focus to the opener after closing', async () => {
    makeElementsVisible()
    const opener = document.createElement('button')
    document.body.append(opener)
    opener.focus()

    const { rerender } = render(
      <SidebarPanel isOpen onClose={vi.fn()} title="Edit agent">
        <button type="button">Panel action</button>
      </SidebarPanel>,
    )
    await waitFor(() => expect(screen.getByRole('button', { name: 'Close panel' })).toHaveFocus())

    rerender(
      <SidebarPanel isOpen={false} onClose={vi.fn()} title="Edit agent">
        <button type="button">Panel action</button>
      </SidebarPanel>,
    )

    expect(opener).toHaveFocus()
    opener.remove()
  })
})
