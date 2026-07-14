import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { QuickCaptureTask } from '../QuickCaptureTask'
import { TaskCreateForm } from '../TaskCreateForm'

describe('task creation forms', () => {
  afterEach(() => {
    vi.restoreAllMocks()
    vi.unstubAllGlobals()
  })

  it('shows TaskCreateForm submission failures and keeps the form open', async () => {
    const onClose = vi.fn()
    const onSubmit = vi.fn().mockRejectedValue(new Error('Task service unavailable'))

    render(
      <TaskCreateForm
        isOpen
        tasks={[]}
        onSubmit={onSubmit}
        onClose={onClose}
      />,
    )

    await userEvent.type(screen.getByPlaceholderText('Task title...'), 'New task')
    await userEvent.click(screen.getByRole('button', { name: 'Create Task' }))

    expect(await screen.findByRole('alert')).toHaveTextContent('Task service unavailable')
    expect(onClose).not.toHaveBeenCalled()
  })

  it('closes TaskCreateForm after a successful submission', async () => {
    const onClose = vi.fn()
    const onSubmit = vi.fn().mockResolvedValue({ id: 'task-1' })

    render(
      <TaskCreateForm
        isOpen
        tasks={[]}
        onSubmit={onSubmit}
        onClose={onClose}
      />,
    )

    await userEvent.type(screen.getByPlaceholderText('Task title...'), 'New task')
    await userEvent.click(screen.getByRole('button', { name: 'Create Task' }))

    await waitFor(() => expect(onClose).toHaveBeenCalledOnce())
    expect(screen.getByPlaceholderText('Task title...')).toHaveValue('')
  })

  it('shows QuickCaptureTask request failures and keeps the form open', async () => {
    const onClose = vi.fn()
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response(null, { status: 503 })))

    render(<QuickCaptureTask isOpen onClose={onClose} />)

    await userEvent.type(screen.getByPlaceholderText('Task title...'), 'Quick task')
    await userEvent.click(screen.getByRole('button', { name: 'Create' }))

    expect(await screen.findByRole('alert')).toHaveTextContent('Failed to create task (503)')
    expect(onClose).not.toHaveBeenCalled()
  })

  it('closes QuickCaptureTask after a successful request', async () => {
    const onClose = vi.fn()
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response(null, { status: 201 })))

    render(<QuickCaptureTask isOpen onClose={onClose} />)

    await userEvent.type(screen.getByPlaceholderText('Task title...'), 'Quick task')
    await userEvent.click(screen.getByRole('button', { name: 'Create' }))

    await waitFor(() => expect(onClose).toHaveBeenCalledOnce())
    expect(screen.queryByRole('alert')).toBeNull()
  })
})
