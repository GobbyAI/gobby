import { fireEvent, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { WorkflowsPage } from '../WorkflowsPage'

vi.mock('../PipelinesTab', () => ({
  PipelinesTab: () => <div data-testid="pipelines-tab" />,
}))

vi.mock('../AgentsTab', () => ({
  AgentsTab: () => <div data-testid="agents-tab" />,
}))

vi.mock('../RulesTab', () => ({
  RulesTab: () => <div data-testid="rules-tab" />,
}))

describe('WorkflowsPage toolbar', () => {
  afterEach(() => {
    vi.unstubAllGlobals()
  })

  it('does not show Install All under the Templates source filter', () => {
    vi.stubGlobal('fetch', vi.fn(async () => Response.json({ dev_mode: false })))

    render(<WorkflowsPage />)

    fireEvent.click(screen.getByRole('button', { name: 'Filter workflows' }))
    fireEvent.click(screen.getByRole('button', { name: 'Templates' }))

    expect(screen.queryByRole('button', { name: 'Install All' })).not.toBeInTheDocument()
  })
})
