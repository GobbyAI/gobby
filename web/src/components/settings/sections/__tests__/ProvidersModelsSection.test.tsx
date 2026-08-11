import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { describe, it, expect, vi } from 'vitest'
import { ProvidersModelsSection } from '../ProvidersModelsSection'
import {
  SettingsSectionContext,
  type SettingsSectionContextValue,
} from '../SettingsSectionContext'
import type { UseSettingsReturn } from '../../../../hooks/useSettings'

// Deterministic catalog so the live provider/model selects render real options
// without hitting the network.
vi.mock('../../../../lib/providerModels', async (importOriginal) => {
  const actual =
    await importOriginal<typeof import('../../../../lib/providerModels')>()
  return {
    ...actual,
    fetchProviderModelCatalog: vi.fn(async () => [
      {
        provider: 'claude',
        available: true,
        source: 'static',
        models: [
          { value: 'opus', label: 'Claude Opus' },
          { value: 'sonnet', label: 'Claude Sonnet' },
        ],
      },
      {
        provider: 'codex',
        available: true,
        source: 'static',
        models: [{ value: 'gpt-5', label: 'GPT-5' }],
      },
    ]),
  }
})

// Minimal schema covering the rows the assertions touch: the shared
// FeatureProfile enum reached through a $ref, mirroring the real DaemonConfig.
const SCHEMA: Record<string, unknown> = {
  $defs: {
    FeatureProfile: {
      enum: ['feature_low', 'feature_mid', 'feature_high'],
      type: 'string',
    },
    RecommendToolsConfig: {
      type: 'object',
      properties: {
        profile: { $ref: '#/$defs/FeatureProfile' },
        candidates: { type: 'array', items: { type: 'string' } },
        enabled: { type: 'boolean' },
        prompt_path: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      },
    },
  },
  type: 'object',
  properties: {
    recommend_tools: { $ref: '#/$defs/RecommendToolsConfig' },
  },
}

function makeConfigValues(): Record<string, unknown> {
  return {
    recommend_tools: {
      profile: 'feature_mid',
      candidates: ['claude/sonnet'],
      enabled: true,
      prompt_path: null,
    },
    tool_summarizer: { profile: 'feature_low', candidates: [], enabled: false },
    import_mcp_server: { profile: 'feature_mid', candidates: [], enabled: true },
    project_verification_synthesis: {
      profile: 'feature_high',
      candidates: [],
      confidence_threshold: 0.7,
    },
    merge_resolution: { profile: 'feature_mid', candidates: [] },
    skill_description: { profile: 'feature_mid', candidates: [] },
    ai: {
      generation: {
        timeout_seconds: 600,
        candidate_timeout_seconds: 60,
        cli_candidate_timeout_seconds: 150,
        endpoints: {
          lmstudio: {
            protocol: 'lmstudio',
            wire_api: 'chat-completions',
            api_base: 'http://localhost:1234',
            model: 'gemma',
          },
        },
        profile_defaults: { feature_mid: ['claude/sonnet'] },
      },
    },
    context_window_overrides: { opus: 1000000 },
  }
}

function makeClientSettings(): UseSettingsReturn {
  return {
    settings: { model: 'opus' },
    updateModel: vi.fn(),
  } as unknown as UseSettingsReturn
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
    clientSettings: makeClientSettings(),
    providerSelection: { selectedProvider: 'claude', onSelectProvider: vi.fn() },
    ...overrides,
  }
}

function renderSection(ctx: SettingsSectionContextValue) {
  return render(
    <SettingsSectionContext.Provider value={ctx}>
      <ProvidersModelsSection />
    </SettingsSectionContext.Provider>,
  )
}

async function waitForProviderCatalog() {
  const provider = screen.getByLabelText('Default provider') as HTMLSelectElement
  await waitFor(() =>
    expect(provider.querySelector('option[value="codex"]')).not.toBeNull(),
  )
}

describe('ProvidersModelsSection', () => {
  it('wires the live provider and model selects to App state and client settings', async () => {
    const ctx = makeContext()
    renderSection(ctx)

    const provider = screen.getByLabelText('Default provider') as HTMLSelectElement
    expect(provider).toHaveValue('claude')
    // Provider options derive from the (async) catalog.
    await waitForProviderCatalog()
    fireEvent.change(provider, { target: { value: 'codex' } })
    expect(ctx.providerSelection?.onSelectProvider).toHaveBeenCalledWith('codex')

    // Model reads from the shared useSettings instance.
    expect(screen.getByLabelText('Default model')).toHaveValue('opus')
  })

  it('renders a feature profile select bound to nested config with enum options', async () => {
    renderSection(makeContext())
    await waitForProviderCatalog()

    const profile = screen.getByLabelText('Tool recommendation profile')
    // Proves nested configValues are read by dotted path (pickPaths traversal).
    expect(profile).toHaveValue('feature_mid')
    expect(within(profile).getAllByRole('option')).toHaveLength(3)
  })

  it('reads feature candidates, toggles, generation numbers, and maps from nested config', async () => {
    renderSection(makeContext())
    await waitForProviderCatalog()

    expect(screen.getByLabelText('Tool recommendation candidates item 1')).toHaveValue(
      'claude/sonnet',
    )
    expect(
      screen.getByRole('switch', { name: 'Tool recommendation enabled' }),
    ).toBeChecked()
    expect(screen.getByLabelText('Generation timeout (seconds)')).toHaveValue(600)
    expect(
      screen.getByLabelText('Verification synthesis confidence threshold'),
    ).toHaveValue(0.7)

    // Structured map editors surface their nested entries.
    expect(screen.getByLabelText('Context window override key 1')).toHaveValue('opus')
    expect(screen.getByLabelText('Generation endpoint key 1')).toHaveValue('lmstudio')
    expect(screen.getByLabelText('API base (lmstudio)')).toHaveValue('http://localhost:1234')
    expect(screen.getByLabelText('Profile default key 1')).toHaveValue('feature_mid')
  })

  it('persists an edited config row through the section draft Save', async () => {
    const ctx = makeContext()
    renderSection(ctx)

    fireEvent.change(screen.getByLabelText('Tool recommendation profile'), {
      target: { value: 'feature_high' },
    })

    const save = screen.getByRole('button', { name: 'Save' })
    await waitFor(() => expect(save).toBeEnabled())
    fireEvent.click(save)

    await waitFor(() => expect(ctx.saveConfig).toHaveBeenCalledTimes(1))
    expect(ctx.saveConfig).toHaveBeenCalledWith(
      expect.objectContaining({ 'recommend_tools.profile': 'feature_high' }),
    )
  })

  it('decodes stored context-window keys for display and re-encodes on save', async () => {
    const ctx = makeContext({
      configValues: {
        ...makeConfigValues(),
        // `context_window_overrides.{model_match}` keys are dynamic
        // segments, stored encoded ("gpt-4.1" contains a dot).
        context_window_overrides: { 'gpt-4%2E1': 200000 },
      },
    })
    renderSection(ctx)

    const key = screen.getByLabelText('Context window override key 1')
    expect(key).toHaveValue('gpt-4.1')

    fireEvent.change(key, { target: { value: 'gpt-4.2' } })
    const save = screen.getByRole('button', { name: 'Save' })
    await waitFor(() => expect(save).toBeEnabled())
    fireEvent.click(save)

    await waitFor(() => expect(ctx.saveConfig).toHaveBeenCalledTimes(1))
    const payload = vi.mocked(ctx.saveConfig).mock.calls[0][0]
    expect(payload['context_window_overrides']).toEqual({ 'gpt-4%2E2': 200000 })
  })

  it('degrades gracefully when client settings and provider selection are absent', () => {
    renderSection(
      makeContext({ clientSettings: undefined, providerSelection: undefined }),
    )

    expect(screen.queryByLabelText('Default provider')).toBeNull()
    expect(
      screen.getByText(/Model and provider selection is unavailable/i),
    ).toBeInTheDocument()
    // The config-backed controls still render without the client surface.
    expect(screen.getByLabelText('Tool recommendation profile')).toHaveValue(
      'feature_mid',
    )
  })
})
