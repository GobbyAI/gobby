import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { describe, it, expect, vi } from 'vitest'
import { McpToolsSection } from '../McpToolsSection'
import {
  SettingsSectionContext,
  type SettingsSectionContextValue,
} from '../SettingsSectionContext'

// Schema covering the rows the assertions touch. The two enum selects
// (`mcp_client_proxy.search_mode`, `skills.injection_format`) prove nested
// `$ref` traversal through the real DaemonConfig shape, and `tool_timeouts` /
// `hubs` cover the two map "fix" rows from the configuration audit.
const SCHEMA: Record<string, unknown> = {
  $defs: {
    MCPClientProxyConfig: {
      type: 'object',
      properties: {
        search_mode: { enum: ['llm', 'semantic', 'hybrid'], type: 'string' },
        min_similarity: { type: 'number' },
        top_k: { type: 'integer' },
        tool_timeouts: {
          type: 'object',
          additionalProperties: { type: 'number' },
        },
      },
    },
    HubConfig: {
      type: 'object',
      properties: {
        type: {
          enum: ['clawdhub', 'skillsmp', 'github-collection', 'claude-plugins'],
          type: 'string',
        },
      },
      required: ['type'],
    },
    SkillsConfig: {
      type: 'object',
      properties: {
        injection_format: { enum: ['summary', 'full', 'none'], type: 'string' },
        hubs: {
          type: 'object',
          additionalProperties: { $ref: '#/$defs/HubConfig' },
        },
      },
    },
  },
  type: 'object',
  properties: {
    mcp_client_proxy: { $ref: '#/$defs/MCPClientProxyConfig' },
    skills: { $ref: '#/$defs/SkillsConfig' },
  },
}

function makeConfigValues(): Record<string, unknown> {
  return {
    mcp_client_proxy: {
      enabled: true,
      connect_timeout: 30,
      proxy_timeout: 45,
      tool_timeout: 20,
      tool_timeouts: { 'slow-tool': 90 },
      search_mode: 'llm',
      min_similarity: 0.3,
      top_k: 10,
      refresh_on_server_add: true,
      refresh_timeout: 300,
    },
    skills: {
      inject_core_skills: true,
      core_skills_path: null,
      injection_format: 'summary',
      hubs: {
        clawd: {
          type: 'clawdhub',
          base_url: 'https://hub.example',
          repo: null,
          branch: null,
          path: null,
          auth_key_name: null,
        },
      },
    },
  }
}

function makeContext(
  overrides: Partial<SettingsSectionContextValue> = {},
): SettingsSectionContextValue {
  return {
    schema: SCHEMA,
    configValues: makeConfigValues(),
    secretKeys: [],
    isLoading: false,
    saveConfig: vi.fn(async () => ({ ok: true })),
    registerDirtyGuard: () => () => {},
    ...overrides,
  }
}

function renderSection(ctx: SettingsSectionContextValue) {
  return render(
    <SettingsSectionContext.Provider value={ctx}>
      <McpToolsSection />
    </SettingsSectionContext.Provider>,
  )
}

describe('McpToolsSection', () => {
  it('reads MCP proxy scalar rows', () => {
    renderSection(makeContext())

    expect(screen.getByRole('switch', { name: 'Enable MCP proxy' })).toBeChecked()
    expect(screen.getByLabelText('MCP connect timeout')).toHaveValue(30)
    expect(screen.getByLabelText('MCP proxy call timeout')).toHaveValue(45)
    expect(screen.getByLabelText('Tool schema timeout')).toHaveValue(20)
    expect(screen.getByLabelText('Minimum semantic similarity')).toHaveValue(0.3)
    expect(screen.getByLabelText('Semantic result count')).toHaveValue(10)
    expect(
      screen.getByRole('switch', { name: 'Refresh embeddings on server add' }),
    ).toBeChecked()
    expect(screen.getByLabelText('Tool refresh timeout')).toHaveValue(300)
  })

  it('resolves the search-mode enum select through a nested $ref', () => {
    renderSection(makeContext())

    const mode = screen.getByLabelText('Tool search mode')
    expect(mode).toHaveValue('llm')
    expect(within(mode).getAllByRole('option')).toHaveLength(3)
  })

  it('edits the tool_timeouts map of numbers', () => {
    renderSection(makeContext())

    expect(screen.getByLabelText('Per-tool timeout key 1')).toHaveValue(
      'slow-tool',
    )
    expect(
      screen.getByLabelText('Per-tool timeout slow-tool value'),
    ).toHaveValue(90)
  })

  it('reads skills rows including the injection-format enum select', () => {
    renderSection(makeContext())

    expect(
      screen.getByRole('switch', { name: 'Advertise core skills' }),
    ).toBeChecked()
    expect(screen.getByLabelText('Core skills path')).toHaveValue('')

    const format = screen.getByLabelText('Skill manifest format')
    expect(format).toHaveValue('summary')
    expect(within(format).getAllByRole('option')).toHaveLength(3)
  })

  it('renders the skills.hubs map as a typed hub sub-form', () => {
    renderSection(makeContext())

    expect(screen.getByLabelText('Skill hub key 1')).toHaveValue('clawd')
    const type = screen.getByLabelText('clawd hub type')
    expect(type).toHaveValue('clawdhub')
    expect(within(type).getAllByRole('option')).toHaveLength(4)
    expect(screen.getByLabelText('clawd hub base URL')).toHaveValue(
      'https://hub.example',
    )
    expect(screen.getByLabelText('clawd hub repository')).toHaveValue('')
    expect(screen.getByLabelText('clawd hub branch')).toHaveValue('')
    expect(screen.getByLabelText('clawd hub path')).toHaveValue('')
    expect(screen.getByLabelText('clawd hub auth key name')).toHaveValue('')
  })

  it('persists an edited draft row through the section Save', async () => {
    const ctx = makeContext()
    renderSection(ctx)

    fireEvent.click(screen.getByRole('switch', { name: 'Enable MCP proxy' }))
    const save = screen.getByRole('button', { name: 'Save' })
    await waitFor(() => expect(save).toBeEnabled())
    fireEvent.click(save)

    await waitFor(() => expect(ctx.saveConfig).toHaveBeenCalledTimes(1))
    expect(ctx.saveConfig).toHaveBeenCalledWith(
      expect.objectContaining({ 'mcp_client_proxy.enabled': false }),
    )
  })
})
