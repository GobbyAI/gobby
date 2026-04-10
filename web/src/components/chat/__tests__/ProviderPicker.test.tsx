import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { ProviderPicker } from '../ProviderPicker'

describe('ProviderPicker', () => {
  const originalFetch = global.fetch

  beforeEach(() => {
    global.fetch = vi.fn().mockResolvedValue({
      ok: true,
      json: async () => ({
        providers: [
          {
            provider: 'claude',
            available: true,
            models: [
              { value: 'opus', label: 'Opus' },
              { value: 'sonnet', label: 'Sonnet' },
            ],
            source: 'static',
          },
          {
            provider: 'gemini',
            available: true,
            models: [
              { value: 'gemini-3.1-pro-preview', label: 'pro-3.1' },
              { value: 'gemini-3-flash-preview', label: 'flash-3' },
            ],
            source: 'static',
          },
          {
            provider: 'codex',
            available: true,
            models: [
              { value: 'gpt-5.4', label: 'codex-5.4' },
              { value: 'gpt-5.4-mini', label: 'mini-5.4' },
              { value: 'gpt-5.3-codex', label: 'codex-5.3' },
            ],
            source: 'static',
          },
        ],
      }),
    }) as typeof fetch
  })

  afterEach(() => {
    global.fetch = originalFetch
    vi.clearAllMocks()
  })

  it('shows friendly Gemini and Codex labels from the catalog', async () => {
    render(
      <ProviderPicker
        open={true}
        onClose={vi.fn()}
        currentProvider="claude"
        currentModel="opus"
        availableProviders={['claude', 'gemini', 'codex']}
        onModelChange={vi.fn()}
        onProviderChange={vi.fn()}
        onSwitchProvider={vi.fn()}
        hasMessages={false}
      />,
    )

    await waitFor(() => {
      expect(screen.getByText('pro-3.1')).toBeTruthy()
      expect(screen.getByText('flash-3')).toBeTruthy()
      expect(screen.getByText('codex-5.4')).toBeTruthy()
      expect(screen.getByText('mini-5.4')).toBeTruthy()
      expect(screen.getByText('codex-5.3')).toBeTruthy()
    })
  })

  it('switches provider, model, and conversation when picking a new provider before first send', async () => {
    const onModelChange = vi.fn()
    const onProviderChange = vi.fn()
    const onSwitchProvider = vi.fn()

    render(
      <ProviderPicker
        open={true}
        onClose={vi.fn()}
        currentProvider="claude"
        currentModel="opus"
        availableProviders={['claude', 'gemini', 'codex']}
        onModelChange={onModelChange}
        onProviderChange={onProviderChange}
        onSwitchProvider={onSwitchProvider}
        hasMessages={false}
      />,
    )

    await userEvent.click(await screen.findByText('codex-5.4'))

    expect(onProviderChange).toHaveBeenCalledWith('codex')
    expect(onModelChange).toHaveBeenCalledWith('gpt-5.4')
    expect(onSwitchProvider).toHaveBeenCalledWith('codex')
  })
})
