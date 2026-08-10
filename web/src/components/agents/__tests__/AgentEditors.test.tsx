import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { AgentEditForm, type AgentFormData } from '../AgentEditForm'
import { AgentRulesEditor } from '../AgentRulesEditor'
import { AgentVariablesEditor } from '../AgentVariablesEditor'

const originalFetch = globalThis.fetch

const panelForm: AgentFormData = {
  name: 'reviewer',
  description: '',
  surfaces: ['spawn'],
  role: '',
  goal: '',
  personality: '',
  instructions: '',
  provider: 'inherit',
  model: '',
  reasoning_effort: 'auto',
  reasoning_required: false,
  mode: 'inherit',
  isolation: 'inherit',
  base_branch: 'inherit',
  timeout: 0,
  pipeline: '',
  fallback_agent: '',
}

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  })
}

function installRejectedAgentPatch(detail: string): ReturnType<typeof vi.fn> {
  const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
    const url = typeof input === 'string' ? input : input.toString()
    if (url.startsWith('/api/agents/definitions/')) {
      return jsonResponse({ detail }, 409)
    }
    if (url === '/api/rules/tags') return jsonResponse({ tags: [] })
    if (url === '/api/rules/groups') return jsonResponse({ groups: [] })
    if (url.startsWith('/api/rules')) return jsonResponse({ rules: [] })
    return jsonResponse({ detail: `Unhandled ${url}` }, 404)
  })
  globalThis.fetch = fetchMock as unknown as typeof fetch
  return fetchMock
}

function makeElementsVisible() {
  vi.spyOn(HTMLElement.prototype, 'getClientRects').mockReturnValue(
    [{ width: 1, height: 1 }] as unknown as DOMRectList,
  )
}

function agentEditForm(isOpen: boolean, onCancel = vi.fn()) {
  return (
    <AgentEditForm
      isOpen={isOpen}
      form={panelForm}
      onChange={vi.fn()}
      onSave={vi.fn()}
      onCancel={onCancel}
      isEditing
      providerCatalog={[]}
    />
  )
}

afterEach(() => {
  globalThis.fetch = originalFetch
  vi.restoreAllMocks()
})

describe('agent definition editors', () => {
  it('exposes a labelled modal dialog and traps focus in the agent editor', async () => {
    makeElementsVisible()
    const user = userEvent.setup()
    render(agentEditForm(true))

    const dialog = screen.getByRole('dialog', { name: 'Edit Agent' })
    expect(dialog).toHaveAttribute('aria-modal', 'true')
    const closeButton = screen.getByRole('button', { name: 'Close panel' })
    const saveButton = screen.getByRole('button', { name: 'Save' })
    await waitFor(() => expect(closeButton).toHaveFocus())
    expect(closeButton).toHaveClass('relative')

    saveButton.focus()
    await user.tab()
    expect(closeButton).toHaveFocus()

    closeButton.focus()
    await user.tab({ shift: true })
    expect(saveButton).toHaveFocus()
  })

  it('closes the agent editor on Escape', async () => {
    makeElementsVisible()
    const onCancel = vi.fn()
    const user = userEvent.setup()
    render(agentEditForm(true, onCancel))

    await waitFor(() => expect(screen.getByRole('button', { name: 'Close panel' })).toHaveFocus())
    await user.keyboard('{Escape}')

    expect(onCancel).toHaveBeenCalledOnce()
  })

  it('restores focus to the opener after the agent editor closes', async () => {
    makeElementsVisible()
    const opener = document.createElement('button')
    document.body.append(opener)
    opener.focus()

    const { rerender } = render(agentEditForm(true))
    await waitFor(() => expect(screen.getByRole('button', { name: 'Close panel' })).toHaveFocus())

    rerender(agentEditForm(false))

    expect(opener).toHaveFocus()
    opener.remove()
  })

  it('keeps server-backed selectors unchanged and surfaces rejected updates', async () => {
    installRejectedAgentPatch('Selector update rejected')
    const onRuleSelectorsChange = vi.fn()
    const user = userEvent.setup()

    render(
      <AgentRulesEditor
        definitionId="agent-1"
        rules={[]}
        onRulesChange={vi.fn()}
        ruleSelectors={{ include: ['tag:stable'], exclude: [] }}
        onRuleSelectorsChange={onRuleSelectorsChange}
      />,
    )

    await user.click(screen.getByTitle('Remove tag:stable'))

    expect(await screen.findByText('Selector update rejected')).toBeInTheDocument()
    expect(screen.getByText('tag:stable')).toBeInTheDocument()
    expect(onRuleSelectorsChange).not.toHaveBeenCalled()
  })

  it('surfaces rejected explicit rule updates', async () => {
    installRejectedAgentPatch('Rule update rejected')
    const onRulesChange = vi.fn()
    const user = userEvent.setup()

    render(
      <AgentRulesEditor
        definitionId="agent-1"
        rules={['guard-rule']}
        onRulesChange={onRulesChange}
      />,
    )

    await user.click(screen.getByTitle('Remove guard-rule'))

    expect(await screen.findByText('Rule update rejected')).toBeInTheDocument()
    expect(onRulesChange).not.toHaveBeenCalled()
  })

  it('surfaces rejected variable updates without changing variables', async () => {
    installRejectedAgentPatch('Variable update rejected')
    const onVariablesChange = vi.fn()
    const user = userEvent.setup()

    render(
      <AgentVariablesEditor
        definitionId="agent-1"
        variables={{ mode: 'safe' }}
        onVariablesChange={onVariablesChange}
      />,
    )

    await user.click(screen.getByTitle('Remove mode'))

    expect(await screen.findByText('Variable update rejected')).toBeInTheDocument()
    await waitFor(() => expect(onVariablesChange).not.toHaveBeenCalled())
  })

  it('keeps variable editing behavior on primitive coarse-hit controls', async () => {
    const onVariablesChange = vi.fn()
    const user = userEvent.setup()

    render(
      <AgentVariablesEditor
        variables={{}}
        onVariablesChange={onVariablesChange}
      />,
    )

    const openButton = screen.getByRole('button', { name: '+ Add Variable' })
    await user.click(openButton)
    const keyInput = screen.getByPlaceholderText('Key')
    const valueInput = screen.getByPlaceholderText('Value')
    await user.type(keyInput, 'mode')
    await user.type(valueInput, 'safe')
    await user.click(screen.getByRole('button', { name: 'Add' }))

    expect(onVariablesChange).toHaveBeenCalledWith({ mode: 'safe' })
    expect(openButton).toHaveClass('relative')
    expect(keyInput.parentElement).toHaveClass('relative')
    expect(valueInput.parentElement).toHaveClass('relative')
  })
})
