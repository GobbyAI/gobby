import { cleanup, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { createElement } from 'react'
import { AppearanceSection } from '../components/settings/sections/AppearanceSection'
import {
  SettingsSectionContext,
  type SettingsSectionContextValue,
} from '../components/settings/sections/SettingsSectionContext'
import type { Settings, UseSettingsReturn } from '../hooks/useSettings'

afterEach(cleanup)

function renderAppearanceSlider(): HTMLElement {
  const settings: Settings = {
    fontSize: 16,
    model: 'opus',
    chatMode: 'plan',
    theme: 'dark',
    defaultChatMode: 'plan',
    sttEnabled: false,
    ttsEnabled: false,
    voiceInputMode: 'ptt',
    planPendingVariant: 'info',
    density: 'comfortable',
  }
  const clientSettings: UseSettingsReturn = {
    settings,
    updateFontSize: vi.fn(),
    updateModel: vi.fn(),
    updateChatMode: vi.fn(),
    updateTheme: vi.fn(),
    updateDefaultChatMode: vi.fn(),
    updateSttEnabled: vi.fn(),
    updateTtsEnabled: vi.fn(),
    updateVoiceInputMode: vi.fn(),
    updatePlanPendingVariant: vi.fn(),
    updateDensity: vi.fn(),
    resetSettings: vi.fn(),
  }
  const context: SettingsSectionContextValue = {
    schema: null,
    configValues: {},
    secretKeys: [],
    isLoading: false,
    saveConfig: async () => ({ ok: true }),
    registerDirtyGuard: () => () => {},
    clientSettings,
  }

  render(
    createElement(
      SettingsSectionContext.Provider,
      { value: context },
      createElement(AppearanceSection),
    ),
  )
  return screen.getByRole('slider', { name: 'Font size' })
}

describe('settings slider focus treatment', () => {
  it('carries the accent focus-visible ring utilities with no resting outline', () => {
    const slider = renderAppearanceSlider()

    slider.focus()
    expect(slider).toHaveFocus()

    expect(slider).toHaveClass(
      'focus-visible:outline-2',
      'focus-visible:outline-accent',
      'focus-visible:outline-offset-[3px]',
      'accent-accent',
      'cursor-pointer',
    )
    const restingOutlineTokens = slider.className
      .split(/\s+/)
      .filter((token) => token.startsWith('outline'))
    expect(restingOutlineTokens).toEqual([])
    expect(slider.className).not.toContain('appearance-font-size__range')
  })
})
