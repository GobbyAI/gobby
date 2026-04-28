import { render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { AgentsTab } from '../AgentsTab'
import { createAgentsFetchStub } from './agentsTestUtils'

vi.mock('../../../lib/providerModels', async importOriginal => ({
  ...(await importOriginal<typeof import('../../../lib/providerModels')>()),
  fetchProviderModelCatalog: vi.fn(async () => []),
}))

function agentDefinition(name: string, isLocal: boolean) {
  return {
    definition: {
      name,
      description: null,
      surfaces: ['spawn'],
      role: null,
      goal: null,
      personality: null,
      instructions: null,
      provider: isLocal ? 'lmstudio' : 'claude',
      model: isLocal ? 'qwen2.5-coder' : 'sonnet',
      is_local: isLocal,
      fallback_agent: null,
      mode: 'inherit',
      isolation: null,
      base_branch: null,
      timeout: 0,
      max_turns: 0,
      workflows: null,
    },
    source: 'installed',
    source_path: null,
    db_id: name,
    enabled: true,
    overridden_by: null,
    deleted_at: null,
    tags: null,
    has_template_update: false,
  }
}

describe('AgentsTab local chip', () => {
  afterEach(() => {
    vi.unstubAllGlobals()
  })

  it('shows LOCAL only on local agent cards', async () => {
    const fetchStub = createAgentsFetchStub({
      definitions: [
        agentDefinition('local-agent', true),
        agentDefinition('cloud-agent', false),
      ],
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

    await waitFor(() => expect(screen.getByText('local-agent')).toBeInTheDocument())

    const localCard = screen.getByText('local-agent').closest('.agent-def-card')
    const cloudCard = screen.getByText('cloud-agent').closest('.agent-def-card')

    expect(localCard).not.toBeNull()
    expect(cloudCard).not.toBeNull()
    expect(within(localCard as HTMLElement).getByText('LOCAL')).toHaveClass(
      'chip',
      'chip--local',
    )
    expect(within(cloudCard as HTMLElement).queryByText('LOCAL')).not.toBeInTheDocument()
  })
})
