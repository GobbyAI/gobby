import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen } from '@testing-library/react'
import { Settings } from '../Settings'

const baseSettings = {
  fontSize: 16,
  model: 'opus',
  chatMode: 'plan' as const,
  theme: 'dark' as const,
  defaultChatMode: 'plan' as const,
  postPlanChatMode: 'normal' as const,
  sttEnabled: false,
  ttsEnabled: false,
  voiceInputMode: 'ptt' as const,
}

describe('Settings voice section', () => {
  beforeEach(() => {
    vi.clearAllMocks()
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
        onPostPlanChatModeChange={vi.fn()}
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
        onPostPlanChatModeChange={vi.fn()}
        onReset={vi.fn()}
      />,
    )

    expect(screen.getByRole('dialog', { name: 'Settings' })).toBeInTheDocument()
    expect(screen.getByRole('group', { name: 'Theme' })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Dark', pressed: true })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Light', pressed: false })).toBeInTheDocument()
    expect(screen.getByRole('group', { name: 'Default Mode' })).toBeInTheDocument()
    expect(screen.getByRole('group', { name: 'After Approved Plan' })).toBeInTheDocument()
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
        onPostPlanChatModeChange={vi.fn()}
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
        onPostPlanChatModeChange={vi.fn()}
        onReset={vi.fn()}
      />,
    )

    expect(screen.queryByText('Voice')).toBeNull()
    expect(screen.queryByLabelText('Enable speech to text')).toBeNull()
    expect(screen.queryByLabelText('Enable text to speech')).toBeNull()
    expect(screen.queryByText('VAD')).toBeNull()
  })
})
