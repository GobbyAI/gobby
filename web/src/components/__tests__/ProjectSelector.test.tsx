import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'
import { ProjectSelector } from '../ProjectSelector'
import type { ProjectOption } from '../../types/chat'

const PROJECTS: ProjectOption[] = [
  { id: 'personal', name: 'Personal' },
  { id: 'project-gobby', name: 'gobby' },
  { id: 'project-demo', name: 'demo' },
]

function renderSelector(
  selectedProjectId: string | null = 'project-gobby',
  onProjectChange = vi.fn(),
) {
  render(
    <ProjectSelector
      projects={PROJECTS}
      selectedProjectId={selectedProjectId}
      onProjectChange={onProjectChange}
    />,
  )
  return { onProjectChange }
}

describe('ProjectSelector', () => {
  it('keeps the desktop segmented project scope control', () => {
    renderSelector()

    const group = screen.getByRole('radiogroup', { name: 'Project scope' })
    expect(within(group).getByRole('radio', { name: 'Personal' })).toBeInTheDocument()
    expect(within(group).getByRole('radio', { name: 'gobby' })).toBeInTheDocument()
  })

  it('opens the one-item mobile project selector with Personal in the list', () => {
    const { onProjectChange } = renderSelector()

    fireEvent.click(screen.getByRole('button', { name: 'Project scope: gobby' }))

    const listbox = screen.getByRole('listbox', { name: 'Project scope options' })
    expect(within(listbox).getByRole('option', { name: 'Personal' })).toBeInTheDocument()
    expect(within(listbox).getByRole('option', { name: 'gobby' })).toHaveAttribute(
      'aria-selected',
      'true',
    )

    fireEvent.click(within(listbox).getByRole('option', { name: 'Personal' }))
    expect(onProjectChange).toHaveBeenCalledWith('personal')
  })

  it('links project search combobox ARIA to the highlighted option', () => {
    const { onProjectChange } = renderSelector()
    const group = screen.getByRole('radiogroup', { name: 'Project scope' })

    fireEvent.click(within(group).getByRole('radio', { name: 'gobby' }))

    const input = screen.getByRole('combobox')
    const listbox = screen.getByRole('listbox', { name: 'Project search results' })
    const gobbyOption = within(listbox).getByRole('option', { name: 'gobby' })
    const demoOption = within(listbox).getByRole('option', { name: 'demo' })

    expect(input).toHaveAttribute('aria-controls', listbox.id)
    expect(input).toHaveAttribute('aria-owns', listbox.id)
    expect(input).toHaveAttribute('aria-activedescendant', gobbyOption.id)

    fireEvent.keyDown(input, { key: 'ArrowDown' })

    expect(input).toHaveAttribute('aria-activedescendant', demoOption.id)

    fireEvent.keyDown(input, { key: 'Enter' })

    expect(onProjectChange).toHaveBeenCalledWith('project-demo')
  })

  it('toggles the compact selector from keyboard and restores focus on Escape', async () => {
    renderSelector()
    const trigger = screen.getByRole('button', { name: 'Project scope: gobby' })

    fireEvent.keyDown(trigger, { key: 'Enter' })

    const listbox = screen.getByRole('listbox', { name: 'Project scope options' })
    expect(trigger).toHaveAttribute('aria-expanded', 'true')
    expect(trigger).toHaveAttribute('aria-controls', listbox.id)

    fireEvent.keyDown(listbox, { key: 'Escape' })

    expect(screen.queryByRole('listbox', { name: 'Project scope options' })).toBeNull()
    await waitFor(() => expect(trigger).toHaveFocus())
  })

  it('selects from the compact selector with arrow keys and Enter', async () => {
    const { onProjectChange } = renderSelector('personal')

    fireEvent.click(screen.getByRole('button', { name: 'Project scope: Personal' }))

    const listbox = screen.getByRole('listbox', { name: 'Project scope options' })
    const gobbyOption = within(listbox).getByRole('option', { name: 'gobby' })
    await waitFor(() => expect(listbox).toHaveFocus())

    fireEvent.keyDown(listbox, { key: 'ArrowDown' })
    expect(listbox).toHaveAttribute('aria-activedescendant', gobbyOption.id)

    fireEvent.keyDown(listbox, { key: 'Enter' })

    expect(onProjectChange).toHaveBeenCalledWith('project-gobby')
  })
})
