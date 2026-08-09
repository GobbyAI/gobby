import { cleanup, render, screen } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { createElement } from 'react'
import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import { AppearanceSection } from '../components/settings/sections/AppearanceSection'
import {
  SettingsSectionContext,
  type SettingsSectionContextValue,
} from '../components/settings/sections/SettingsSectionContext'
import type { Settings, UseSettingsReturn } from '../hooks/useSettings'

let styleElement: HTMLStyleElement

beforeEach(() => {
  styleElement = document.createElement('style')
  styleElement.textContent = readFileSync(
    resolve(process.cwd(), 'src/styles/settings-overlay.css'),
    'utf8',
  )
  document.head.appendChild(styleElement)
})

afterEach(() => {
  cleanup()
  styleElement.remove()
})

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
  it('has no resting outline and shows the accent ring on focus-visible', () => {
    const slider = renderAppearanceSlider()

    expect(getComputedStyle(slider).outline).toBe('')

    slider.focus()
    expect(slider).toHaveFocus()
    const focusRingRule = Array.from(styleElement.sheet?.cssRules ?? []).find(
      (rule): rule is CSSStyleRule =>
        rule instanceof CSSStyleRule &&
        rule.selectorText === '.appearance-font-size__range:focus-visible',
    )
    expect(focusRingRule).toBeDefined()
    expect(slider.matches(focusRingRule?.selectorText ?? '')).toBe(true)
    expect(focusRingRule?.style.outline).toBe('2px solid var(--accent)')
  })
})
