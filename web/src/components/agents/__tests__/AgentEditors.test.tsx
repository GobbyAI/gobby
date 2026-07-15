import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { AgentRulesEditor } from '../AgentRulesEditor'
import { AgentVariablesEditor } from '../AgentVariablesEditor'

const originalFetch = globalThis.fetch

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

afterEach(() => {
  globalThis.fetch = originalFetch
  vi.restoreAllMocks()
})

describe('agent definition editors', () => {
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
})
