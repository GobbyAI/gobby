import { fireEvent, render, screen, within } from '@testing-library/react'
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
})
