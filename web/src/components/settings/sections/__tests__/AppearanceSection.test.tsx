import { cleanup, fireEvent, render, screen, within } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { AppearanceSection } from '../AppearanceSection'
import { SettingsSectionContext } from '../SettingsSectionContext'
import type { SettingsSectionContextValue } from '../SettingsSectionContext'
import type { Settings, UseSettingsReturn } from '../../../../hooks/useSettings'

afterEach(cleanup)

function makeSettings(overrides: Partial<Settings> = {}): Settings {
  return {
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
    ...overrides,
  }
}

function makeClient(settings: Settings): UseSettingsReturn {
  return {
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
}

function renderSection(clientSettings: UseSettingsReturn | undefined): void {
  const ctx: SettingsSectionContextValue = {
    schema: null,
    configValues: {},
    secretKeys: [],
    isLoading: false,
    saveConfig: async () => ({ ok: true }),
    registerDirtyGuard: () => () => {},
    clientSettings,
  }
  render(
    <SettingsSectionContext.Provider value={ctx}>
      <AppearanceSection />
    </SettingsSectionContext.Provider>,
  )
}

describe('AppearanceSection', () => {
  it('reflects the active theme and routes a change to updateTheme', () => {
    const client = makeClient(makeSettings({ theme: 'dark' }))
    renderSection(client)

    const themeGroup = screen.getByRole('radiogroup', { name: 'Theme' })
    expect(within(themeGroup).getByRole('radio', { name: 'Dark' })).toHaveAttribute(
      'aria-checked',
      'true',
    )

    fireEvent.click(within(themeGroup).getByRole('radio', { name: 'Light' }))
    expect(client.updateTheme).toHaveBeenCalledWith('light')
  })

  it('routes a density change to updateDensity', () => {
    const client = makeClient(makeSettings({ density: 'comfortable' }))
    renderSection(client)

    const densityGroup = screen.getByRole('radiogroup', { name: 'Density' })
    expect(within(densityGroup).getByRole('radio', { name: 'Comfortable' })).toHaveAttribute(
      'aria-checked',
      'true',
    )

    fireEvent.click(within(densityGroup).getByRole('radio', { name: 'Compact' }))
    expect(client.updateDensity).toHaveBeenCalledWith('compact')
  })

  it('bounds the font-size slider to 12-24 and routes changes to updateFontSize', () => {
    const client = makeClient(makeSettings({ fontSize: 16 }))
    renderSection(client)

    const slider = screen.getByRole('slider', { name: 'Font size' })
    // The audit's missing-validation fix: the slider must cap at 24, not 48.
    expect(slider).toHaveAttribute('min', '12')
    expect(slider).toHaveAttribute('max', '24')

    fireEvent.change(slider, { target: { value: '20' } })
    expect(client.updateFontSize).toHaveBeenCalledWith(20)
  })

  it('exposes a plan-pending control wired to updatePlanPendingVariant (dead-backend fix)', () => {
    const client = makeClient(makeSettings({ planPendingVariant: 'info' }))
    renderSection(client)

    const group = screen.getByRole('radiogroup', { name: 'Plan-pending highlight' })
    expect(within(group).getByRole('radio', { name: 'Info' })).toHaveAttribute(
      'aria-checked',
      'true',
    )

    fireEvent.click(within(group).getByRole('radio', { name: 'Amber' }))
    expect(client.updatePlanPendingVariant).toHaveBeenCalledWith('amber')
  })

  it('renders a graceful fallback when no settings instance is available', () => {
    renderSection(undefined)
    expect(screen.getByText(/unavailable/i)).toBeInTheDocument()
    expect(screen.queryByRole('radiogroup', { name: 'Theme' })).toBeNull()
  })
})
