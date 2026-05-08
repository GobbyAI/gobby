import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { AgentsTab } from '../AgentsTab'

vi.mock('../../../lib/providerModels', async importOriginal => ({
  ...(await importOriginal<typeof import('../../../lib/providerModels')>()),
  fetchProviderModelCatalog: vi.fn(async () => []),
}))

function templateAgent() {
  return {
    definition: {
      name: 'template-agent',
      description: null,
      surfaces: ['spawn'],
      role: null,
      goal: null,
      personality: null,
      instructions: null,
      provider: 'claude',
      model: 'sonnet',
      is_local: false,
      fallback_agent: null,
      mode: 'inherit',
      isolation: null,
      base_branch: 'main',
      timeout: 0,
      max_turns: 0,
      workflows: null,
    },
    source: 'template',
    source_path: null,
    db_id: null,
    enabled: true,
    overridden_by: null,
    deleted_at: null,
    tags: null,
    has_template_update: false,
  }
}

function installedAgent(name: string) {
  return {
    ...templateAgent(),
    definition: {
      ...templateAgent().definition,
      name,
    },
    source: 'installed',
    db_id: `agent-${name}`,
  }
}

describe('AgentsTab card actions', () => {
  afterEach(() => {
    vi.unstubAllGlobals()
  })

  it('posts install when a template card install button is clicked', async () => {
    const fetchStub = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input)
      if (url === '/api/agents/definitions/template-agent/install') {
        return Response.json({ status: 'success', method: init?.method })
      }
      if (url.includes('/api/agents/definitions')) {
        return Response.json({ status: 'success', definitions: [templateAgent()] })
      }
      if (url.includes('/api/source-control/branches')) {
        return Response.json({ branches: [] })
      }
      if (url.includes('/api/source-control/status')) {
        return Response.json({ repo_path: null })
      }
      if (url.includes('/api/workflows')) {
        return Response.json({ workflows: [] })
      }
      return Response.json({})
    })
    vi.stubGlobal('fetch', fetchStub)

    render(
      <AgentsTab
        searchText=""
        sourceFilter="templates"
        devMode={false}
        showCreateForm={false}
        onToggleCreateForm={() => {}}
        filterProvider="all"
        onProvidersChange={() => {}}
      />,
    )

    await waitFor(() => expect(screen.getByText('template-agent')).toBeInTheDocument())
    fireEvent.click(screen.getByRole('button', { name: 'Install' }))

    await waitFor(() => {
      expect(fetchStub).toHaveBeenCalledWith(
        '/api/agents/definitions/template-agent/install',
        { method: 'POST' },
      )
    })
  })

  it('blocks duplicate save when the new name already exists', async () => {
    const fetchStub = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input)
      if (url.includes('/api/agents/definitions')) {
        return Response.json({
          status: 'success',
          definitions: [installedAgent('source-agent'), installedAgent('existing-agent')],
        })
      }
      if (url.includes('/api/source-control/branches')) {
        return Response.json({ branches: [] })
      }
      if (url.includes('/api/source-control/status')) {
        return Response.json({ repo_path: null })
      }
      if (url.includes('/api/workflows')) {
        return Response.json({ workflows: [] })
      }
      return Response.json({ status: 'success', method: init?.method })
    })
    vi.stubGlobal('fetch', fetchStub)

    render(
      <AgentsTab
        searchText=""
        sourceFilter="installed"
        devMode={false}
        showCreateForm={false}
        onToggleCreateForm={() => {}}
        filterProvider="all"
        onProvidersChange={() => {}}
      />,
    )

    await waitFor(() => expect(screen.getByText('source-agent')).toBeInTheDocument())
    fireEvent.click(screen.getAllByRole('button', { name: 'Duplicate agent' })[0])
    fireEvent.change(screen.getByLabelText('New name'), { target: { value: 'existing-agent' } })
    fireEvent.click(screen.getByRole('button', { name: 'Duplicate' }))

    expect(await screen.findByText('Agent "existing-agent" already exists')).toBeInTheDocument()
    expect(fetchStub).not.toHaveBeenCalledWith(
      '/api/agents/definitions',
      expect.objectContaining({ method: 'POST' }),
    )
  })
})
