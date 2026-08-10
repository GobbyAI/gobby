import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, describe, it, expect, vi } from 'vitest'
import { SecretsAuthSection } from '../SecretsAuthSection'
import {
  SettingsSectionContext,
  type SettingsSectionContextValue,
} from '../SettingsSectionContext'
import type { SecretInfo } from '../../../../hooks/useConfiguration'

// The section owns no schema-backed selects, so a minimal schema suffices; the
// secret-typed rows render through SecretConfigField (masked text + reveal),
// and `auth.username` is the one plain row.
const SCHEMA: Record<string, unknown> = { type: 'object', properties: {} }

// `/api/config/values` masks secret-valued keys as "********" (MASKED_SECRET).
// A set secret arrives masked; an unset one arrives null/empty.
function makeConfigValues(): Record<string, unknown> {
  return {
    auth: { username: 'admin', password: '********' },
    ai: { embeddings: { api_key: '********' } },
    databases: {
      qdrant: { api_key: '********' },
      falkordb: { password: null },
    },
  }
}

function makeSecrets(): SecretInfo[] {
  return [
    {
      id: 'sec-1',
      name: 'anthropic_key',
      category: 'api',
      description: 'Claude API key',
      created_at: '2026-06-15T00:00:00Z',
      updated_at: '2026-06-15T00:00:00Z',
    },
  ]
}

function makeContext(
  overrides: Partial<SettingsSectionContextValue> = {},
): SettingsSectionContextValue {
  return {
    schema: SCHEMA,
    configValues: makeConfigValues(),
    secretKeys: [
      'auth.password',
      'ai.embeddings.api_key',
      'databases.qdrant.api_key',
      'databases.falkordb.password',
    ],
    isLoading: false,
    saveConfig: vi.fn(async () => ({ ok: true })),
    registerDirtyGuard: () => () => {},
    secrets: makeSecrets(),
    secretCategories: ['general', 'api'],
    saveSecret: vi.fn(async () => true),
    deleteSecret: vi.fn(async () => true),
    ...overrides,
  }
}

function renderSection(ctx: SettingsSectionContextValue) {
  return render(
    <SettingsSectionContext.Provider value={ctx}>
      <SecretsAuthSection />
    </SettingsSectionContext.Provider>,
  )
}

afterEach(() => {
  vi.restoreAllMocks()
})

describe('SecretsAuthSection', () => {
  it('renders the plain username row and the masked password row', () => {
    renderSection(makeContext())

    expect(screen.getByRole('textbox', { name: 'Web UI username' })).toHaveValue(
      'admin',
    )

    const password = screen.getByLabelText('Web UI password')
    expect(password).toHaveValue('********')
    expect(password).toHaveAttribute('type', 'password')
  })

  it('reveals and re-hides a masked secret value through the toggle', () => {
    renderSection(makeContext())

    const password = screen.getByLabelText('Web UI password')
    expect(password).toHaveAttribute('type', 'password')

    fireEvent.click(screen.getByRole('button', { name: 'Show Web UI password' }))
    expect(password).toHaveAttribute('type', 'text')

    fireEvent.click(screen.getByRole('button', { name: 'Hide Web UI password' }))
    expect(password).toHaveAttribute('type', 'password')
  })

  it('renders the service-credential secret rows, masked when set and empty when unset', () => {
    renderSection(makeContext())

    expect(screen.getByLabelText('Embeddings API key')).toHaveValue('********')
    expect(screen.getByLabelText('Qdrant API key')).toHaveValue('********')
    expect(screen.getByLabelText('FalkorDB password')).toHaveValue('')
  })

  it('persists an edited secret-config row through the section Save', async () => {
    const ctx = makeContext()
    renderSection(ctx)

    fireEvent.change(screen.getByLabelText('Embeddings API key'), {
      target: { value: 'sk-new-embeddings' },
    })
    const save = screen.getByRole('button', { name: 'Save' })
    await waitFor(() => expect(save).toBeEnabled())
    fireEvent.click(save)

    await waitFor(() => expect(ctx.saveConfig).toHaveBeenCalledTimes(1))
    expect(ctx.saveConfig).toHaveBeenCalledWith(
      expect.objectContaining({ 'ai.embeddings.api_key': 'sk-new-embeddings' }),
    )
  })

  it('lists existing secret-store entries with masked values', () => {
    renderSection(makeContext())

    expect(screen.getByText('anthropic_key')).toBeInTheDocument()
    expect(screen.getByText(/Claude API key/)).toBeInTheDocument()
    expect(
      screen.getByRole('button', { name: 'Edit secret anthropic_key' }),
    ).toBeInTheDocument()
    expect(
      screen.getByRole('button', { name: 'Delete secret anthropic_key' }),
    ).toBeInTheDocument()
  })

  it('shows guidance when the secret store is empty', () => {
    renderSection(makeContext({ secrets: [] }))

    expect(screen.getByText(/No secrets stored yet/)).toBeInTheDocument()
  })

  it('adds a secret through the store form, separate from the config draft', async () => {
    const ctx = makeContext()
    renderSection(ctx)

    fireEvent.click(screen.getByRole('button', { name: 'Add secret' }))
    fireEvent.change(screen.getByRole('textbox', { name: 'Secret name' }), {
      target: { value: 'openai_key' },
    })
    fireEvent.change(screen.getByLabelText('Secret value'), {
      target: { value: 'sk-live-value' },
    })
    fireEvent.click(screen.getByRole('button', { name: 'Save secret' }))

    await waitFor(() => expect(ctx.saveSecret).toHaveBeenCalledTimes(1))
    expect(ctx.saveSecret).toHaveBeenCalledWith(
      'openai_key',
      'sk-live-value',
      'general',
      undefined,
    )
    expect(ctx.saveConfig).not.toHaveBeenCalled()
  })

  it('does not submit the store form without both a name and a value', () => {
    const ctx = makeContext()
    renderSection(ctx)

    fireEvent.click(screen.getByRole('button', { name: 'Add secret' }))
    fireEvent.change(screen.getByRole('textbox', { name: 'Secret name' }), {
      target: { value: 'lonely_name' },
    })
    fireEvent.click(screen.getByRole('button', { name: 'Save secret' }))

    expect(ctx.saveSecret).not.toHaveBeenCalled()
  })

  it('edits an existing secret with the name locked and the value re-entered', async () => {
    const ctx = makeContext()
    renderSection(ctx)

    fireEvent.click(
      screen.getByRole('button', { name: 'Edit secret anthropic_key' }),
    )
    const name = screen.getByRole('textbox', { name: 'Secret name' })
    expect(name).toHaveValue('anthropic_key')
    expect(name).toBeDisabled()

    fireEvent.change(screen.getByLabelText('Secret value'), {
      target: { value: 'sk-rotated' },
    })
    fireEvent.click(screen.getByRole('button', { name: 'Update secret' }))

    await waitFor(() => expect(ctx.saveSecret).toHaveBeenCalledTimes(1))
    expect(ctx.saveSecret).toHaveBeenCalledWith(
      'anthropic_key',
      'sk-rotated',
      'api',
      'Claude API key',
    )
  })

  it('deletes a secret only after confirmation', async () => {
    const ctx = makeContext()
    const confirmSpy = vi.spyOn(window, 'confirm').mockReturnValue(false)
    renderSection(ctx)

    fireEvent.click(
      screen.getByRole('button', { name: 'Delete secret anthropic_key' }),
    )
    expect(ctx.deleteSecret).not.toHaveBeenCalled()

    confirmSpy.mockReturnValue(true)
    fireEvent.click(
      screen.getByRole('button', { name: 'Delete secret anthropic_key' }),
    )
    await waitFor(() =>
      expect(ctx.deleteSecret).toHaveBeenCalledWith('anthropic_key'),
    )
  })

  it('falls back gracefully when the secret store surface is unavailable', () => {
    renderSection(makeContext({ saveSecret: undefined, deleteSecret: undefined }))

    expect(
      screen.queryByRole('button', { name: 'Add secret' }),
    ).not.toBeInTheDocument()
    expect(
      screen.getByText('The secret store is unavailable.'),
    ).toBeInTheDocument()
  })

  it('still renders the auth rows when the secret store is unavailable', () => {
    renderSection(makeContext({ saveSecret: undefined, deleteSecret: undefined }))

    expect(
      screen.getByRole('textbox', { name: 'Web UI username' }),
    ).toBeInTheDocument()
  })
})
