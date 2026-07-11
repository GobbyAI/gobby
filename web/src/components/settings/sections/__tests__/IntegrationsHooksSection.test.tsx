import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { describe, it, expect, vi } from 'vitest'
import { IntegrationsHooksSection } from '../IntegrationsHooksSection'
import {
  SettingsSectionContext,
  type SettingsSectionContextValue,
} from '../SettingsSectionContext'

// The section composes raw config-field wrappers plus the StringListField and
// TypedListField primitives, so none of its rows consult the JSON schema for
// enum options. A trivial schema is enough; the draft is sourced from
// configValues by the SettingsSection shell.
const SCHEMA: Record<string, unknown> = { type: 'object', properties: {} }

function makeConfigValues(): Record<string, unknown> {
  return {
    communications: {
      enabled: true,
      webhook_base_url: 'https://gobby.example/webhooks',
      channel_defaults: {
        rate_limit_per_minute: 30,
        burst: 5,
        retry_count: 3,
        poll_interval_seconds: 30,
        retention_days: 90,
      },
      inbound_enabled: true,
      outbound_enabled: false,
      auto_create_sessions: true,
    },
    hook_extensions: {
      websocket: {
        enabled: true,
        broadcast_events: ['session-start', 'post-tool-use'],
        include_payload: true,
      },
      webhooks: {
        enabled: true,
        endpoints: [
          {
            name: 'ci-bridge',
            url: 'https://ci.example/hook',
            events: ['post-tool-use'],
            headers: { 'X-Token': 'abc' },
            timeout: 12,
            retry_count: 2,
            retry_delay: 1.5,
            can_block: false,
            fail_closed: true,
            enabled: true,
          },
        ],
        default_timeout: 10,
        async_dispatch: true,
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
      <IntegrationsHooksSection />
    </SettingsSectionContext.Provider>,
  )
}

describe('IntegrationsHooksSection', () => {
  it('reads the communications scalar rows', () => {
    renderSection(makeContext())

    expect(
      screen.getByRole('switch', { name: 'Enable communications' }),
    ).toBeChecked()
    expect(screen.getByLabelText('Communications webhook base URL')).toHaveValue(
      'https://gobby.example/webhooks',
    )
    expect(
      screen.getByRole('switch', { name: 'Enable inbound communications' }),
    ).toBeChecked()
    expect(
      screen.getByRole('switch', { name: 'Enable outbound communications' }),
    ).not.toBeChecked()
    expect(
      screen.getByRole('switch', {
        name: 'Auto-create sessions for inbound messages',
      }),
    ).toBeChecked()
  })

  it('reads the channel-default number rows', () => {
    renderSection(makeContext())

    expect(screen.getByLabelText('Channel rate limit per minute')).toHaveValue(30)
    expect(screen.getByLabelText('Channel burst allowance')).toHaveValue(5)
    expect(screen.getByLabelText('Channel retry count')).toHaveValue(3)
    expect(screen.getByLabelText('Channel poll interval (seconds)')).toHaveValue(
      30,
    )
    expect(
      screen.getByLabelText('Channel message retention (days)'),
    ).toHaveValue(90)
  })

  it('renders broadcast_events as an editable string list, not a text fallback', () => {
    renderSection(makeContext())

    expect(
      screen.getByRole('switch', { name: 'Enable WebSocket broadcasting' }),
    ).toBeChecked()
    expect(screen.getByLabelText('Broadcast event item 1')).toHaveValue(
      'session-start',
    )
    expect(screen.getByLabelText('Broadcast event item 2')).toHaveValue(
      'post-tool-use',
    )
    expect(
      screen.getByRole('switch', { name: 'Include event payload in broadcasts' }),
    ).toBeChecked()
  })

  it('reads the webhooks scalar rows', () => {
    renderSection(makeContext())

    expect(
      screen.getByRole('switch', { name: 'Enable webhook dispatch' }),
    ).toBeChecked()
    expect(
      screen.getByLabelText('Default webhook timeout (seconds)'),
    ).toHaveValue(10)
    expect(
      screen.getByRole('switch', { name: 'Dispatch webhooks asynchronously' }),
    ).toBeChecked()
  })

  it('renders webhooks.endpoints as a structured typed-list editor', () => {
    renderSection(makeContext())

    expect(screen.getByLabelText('ci-bridge name')).toHaveValue('ci-bridge')
    expect(screen.getByLabelText('ci-bridge URL')).toHaveValue(
      'https://ci.example/hook',
    )
    expect(screen.getByLabelText('ci-bridge events item 1')).toHaveValue(
      'post-tool-use',
    )
    expect(screen.getByLabelText('ci-bridge headers key 1')).toHaveValue(
      'X-Token',
    )
    expect(screen.getByLabelText('Value for X-Token')).toHaveValue('abc')
    expect(screen.getByLabelText('ci-bridge timeout (seconds)')).toHaveValue(12)
    expect(screen.getByLabelText('ci-bridge retry count')).toHaveValue(2)
    expect(screen.getByLabelText('ci-bridge retry delay (seconds)')).toHaveValue(
      1.5,
    )
    expect(
      screen.getByRole('switch', { name: 'ci-bridge can block the action' }),
    ).not.toBeChecked()
    expect(
      screen.getByRole('switch', { name: 'ci-bridge fail closed' }),
    ).toBeChecked()
    expect(
      screen.getByRole('switch', { name: 'ci-bridge enabled' }),
    ).toBeChecked()
  })

  it('adds a new webhook endpoint row', () => {
    renderSection(makeContext())

    fireEvent.click(
      screen.getByRole('button', { name: 'Add webhook endpoint' }),
    )

    // The blank endpoint exposes its own name/url inputs labelled by position.
    expect(screen.getByLabelText('endpoint 2 name')).toHaveValue('')
    expect(screen.getByLabelText('endpoint 2 URL')).toHaveValue('')
  })

  it('persists an edited broadcast_events list through the section Save', async () => {
    const ctx = makeContext()
    renderSection(ctx)

    fireEvent.change(screen.getByLabelText('Broadcast event item 1'), {
      target: { value: 'session-end' },
    })
    const save = screen.getByRole('button', { name: 'Save' })
    await waitFor(() => expect(save).toBeEnabled())
    fireEvent.click(save)

    await waitFor(() => expect(ctx.saveConfig).toHaveBeenCalledTimes(1))
    expect(ctx.saveConfig).toHaveBeenCalledWith(
      expect.objectContaining({
        'hook_extensions.websocket.broadcast_events': [
          'session-end',
          'post-tool-use',
        ],
      }),
    )
  })

  it('persists an edited communications scalar through the section Save', async () => {
    const ctx = makeContext()
    renderSection(ctx)

    fireEvent.click(
      screen.getByRole('switch', { name: 'Enable outbound communications' }),
    )
    const save = screen.getByRole('button', { name: 'Save' })
    await waitFor(() => expect(save).toBeEnabled())
    fireEvent.click(save)

    await waitFor(() => expect(ctx.saveConfig).toHaveBeenCalledTimes(1))
    expect(ctx.saveConfig).toHaveBeenCalledWith(
      expect.objectContaining({ 'communications.outbound_enabled': true }),
    )
  })

  it('persists an edited webhook endpoint field through the section Save', async () => {
    const ctx = makeContext()
    renderSection(ctx)

    fireEvent.change(screen.getByLabelText('ci-bridge URL'), {
      target: { value: 'https://ci.example/v2' },
    })
    const save = screen.getByRole('button', { name: 'Save' })
    await waitFor(() => expect(save).toBeEnabled())
    fireEvent.click(save)

    await waitFor(() => expect(ctx.saveConfig).toHaveBeenCalledTimes(1))
    const saved = (ctx.saveConfig as ReturnType<typeof vi.fn>).mock.calls[0][0]
    expect(saved['hook_extensions.webhooks.endpoints']).toEqual([
      expect.objectContaining({
        url: 'https://ci.example/v2',
        fail_closed: true,
      }),
    ])
  })

  it('does not render the legacy pending placeholder', () => {
    renderSection(makeContext())

    expect(
      screen.queryByText(/being migrated into this section/),
    ).not.toBeInTheDocument()
    expect(screen.getByText('Endpoints')).toBeInTheDocument()
  })
})
