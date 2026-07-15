import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { Settings } from '../Settings'

const baseSettings = {
  fontSize: 16,
  model: 'opus',
  chatMode: 'plan' as const,
  theme: 'dark' as const,
  defaultChatMode: 'plan' as const,
  sttEnabled: false,
  ttsEnabled: false,
  voiceInputMode: 'ptt' as const,
  planPendingVariant: 'info' as const,
  density: 'comfortable' as const,
}

describe('Settings', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    vi.spyOn(HTMLElement.prototype, 'getClientRects').mockReturnValue({ length: 1 } as DOMRectList)
  })

  afterEach(() => {
    vi.restoreAllMocks()
  })

  it('does not render the legacy model selector', () => {
    render(
      <Settings
        isOpen={true}
        onClose={vi.fn()}
        settings={baseSettings}
        onFontSizeChange={vi.fn()}
        onThemeChange={vi.fn()}
        onDefaultChatModeChange={vi.fn()}
        onReset={vi.fn()}
      />,
    )

    expect(screen.queryByLabelText('Model')).toBeNull()
    expect(screen.queryByText('Claude Opus')).toBeNull()
  })

  it('labels setting groups and marks selected options as pressed', () => {
    render(
      <Settings
        isOpen={true}
        onClose={vi.fn()}
        settings={baseSettings}
        onFontSizeChange={vi.fn()}
        onThemeChange={vi.fn()}
        onDefaultChatModeChange={vi.fn()}
        onReset={vi.fn()}
      />,
    )

    expect(screen.getByRole('dialog', { name: 'Settings' })).toBeInTheDocument()
    expect(screen.getByRole('group', { name: 'Theme' })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Dark', pressed: true })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Light', pressed: false })).toBeInTheDocument()
    expect(screen.getByRole('group', { name: 'Default Mode' })).toBeInTheDocument()
    // The post-plan preference was removed; mode is chosen at approval time.
    expect(screen.queryByRole('group', { name: 'After Approved Plan' })).toBeNull()
  })

  it('does not render voice controls from Settings', () => {
    render(
      <Settings
        isOpen={true}
        onClose={vi.fn()}
        settings={baseSettings}
        onFontSizeChange={vi.fn()}
        onThemeChange={vi.fn()}
        onDefaultChatModeChange={vi.fn()}
        onReset={vi.fn()}
      />,
    )

    expect(screen.queryByText('Voice')).toBeNull()
    expect(screen.queryByLabelText('Enable speech to text')).toBeNull()
    expect(screen.queryByLabelText('Enable text to speech')).toBeNull()
    expect(screen.queryByText('Push to Talk')).toBeNull()
    expect(screen.queryByText('VAD')).toBeNull()
  })

  it('keeps voice controls absent even when persisted voice settings are enabled', () => {
    render(
      <Settings
        isOpen={true}
        onClose={vi.fn()}
        settings={{ ...baseSettings, sttEnabled: true, ttsEnabled: true, voiceInputMode: 'vad' }}
        onFontSizeChange={vi.fn()}
        onThemeChange={vi.fn()}
        onDefaultChatModeChange={vi.fn()}
        onReset={vi.fn()}
      />,
    )

    expect(screen.queryByText('Voice')).toBeNull()
    expect(screen.queryByLabelText('Enable speech to text')).toBeNull()
    expect(screen.queryByLabelText('Enable text to speech')).toBeNull()
    expect(screen.queryByText('VAD')).toBeNull()
  })

  it('focuses the first control and closes on Escape', async () => {
    const onClose = vi.fn()
    render(
      <Settings
        isOpen={true}
        onClose={onClose}
        settings={baseSettings}
        onFontSizeChange={vi.fn()}
        onThemeChange={vi.fn()}
        onDefaultChatModeChange={vi.fn()}
        onReset={vi.fn()}
      />,
    )

    const dialog = screen.getByRole('dialog', { name: 'Settings' })
    const closeButton = screen.getByRole('button', { name: 'Close settings' })
    await waitFor(() => expect(closeButton).toHaveFocus())

    fireEvent.keyDown(dialog, { key: 'Escape' })

    expect(onClose).toHaveBeenCalledOnce()
  })

  it('traps forward and backward focus inside the dialog', async () => {
    render(
      <Settings
        isOpen={true}
        onClose={vi.fn()}
        settings={baseSettings}
        onFontSizeChange={vi.fn()}
        onThemeChange={vi.fn()}
        onDefaultChatModeChange={vi.fn()}
        onReset={vi.fn()}
      />,
    )

    const dialog = screen.getByRole('dialog', { name: 'Settings' })
    const first = screen.getByRole('button', { name: 'Close settings' })
    const last = screen.getByRole('button', { name: 'Reset to Defaults' })
    await waitFor(() => expect(first).toHaveFocus())

    last.focus()
    fireEvent.keyDown(dialog, { key: 'Tab' })
    expect(first).toHaveFocus()

    fireEvent.keyDown(dialog, { key: 'Tab', shiftKey: true })
    expect(last).toHaveFocus()
  })

  it('restores focus when the dialog closes', async () => {
    const trigger = document.createElement('button')
    document.body.appendChild(trigger)
    trigger.focus()

    const { rerender } = render(
      <Settings
        isOpen={true}
        onClose={vi.fn()}
        settings={baseSettings}
        onFontSizeChange={vi.fn()}
        onThemeChange={vi.fn()}
        onDefaultChatModeChange={vi.fn()}
        onReset={vi.fn()}
      />,
    )
    await waitFor(() => expect(screen.getByRole('button', { name: 'Close settings' })).toHaveFocus())

    rerender(
      <Settings
        isOpen={false}
        onClose={vi.fn()}
        settings={baseSettings}
        onFontSizeChange={vi.fn()}
        onThemeChange={vi.fn()}
        onDefaultChatModeChange={vi.fn()}
        onReset={vi.fn()}
      />,
    )

    expect(trigger).toHaveFocus()
    trigger.remove()
  })
})
