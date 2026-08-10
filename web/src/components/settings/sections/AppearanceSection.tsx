import type { ReactNode } from 'react'
import { SegmentedControl } from '../../ui/SegmentedControl'
import { Button } from '../../ui/Button'
import { Input } from '../../ui/Input'
import type { SegmentedControlOption } from '../../ui/SegmentedControl'
import type { Density, Theme, UseSettingsReturn } from '../../../hooks/useSettings'
import type { PlanPendingVariant } from '../../chat/planPendingSurface'
import { useSettingsSectionContext } from './SettingsSectionContext'
import { SettingsSection } from './SettingsSection'

// Appearance is driven entirely by the shared `useSettings` instance (client
// ui_settings applied live), not the dotted-path config draft, so it owns no
// draft paths and renders no Save/Discard footer — changes apply immediately.
const OWNED_PATHS: readonly string[] = []

const THEME_OPTIONS: readonly SegmentedControlOption<Theme>[] = [
  { value: 'dark', label: 'Dark' },
  { value: 'light', label: 'Light' },
  { value: 'system', label: 'System' },
]

const DENSITY_OPTIONS: readonly SegmentedControlOption<Density>[] = [
  { value: 'comfortable', label: 'Comfortable' },
  { value: 'compact', label: 'Compact' },
]

const PLAN_PENDING_OPTIONS: readonly SegmentedControlOption<PlanPendingVariant>[] = [
  { value: 'info', label: 'Info' },
  { value: 'amber', label: 'Amber' },
]

// The backend stores fontSize as a bare int with no range check and the old
// dialog allowed 12–48; the audit's missing-validation fix caps the control at
// the documented 12–24 band the typography ladder is tuned for.
const FONT_SIZE_MIN = 12
const FONT_SIZE_MAX = 24
const FONT_SIZE_FIELD_ID = 'appearance-font-size'

interface AppearanceFieldProps {
  label: string
  description: string
  /** When the control is a single labelable input, associate the label to it. */
  htmlFor?: string
  children: ReactNode
}

function AppearanceField({ label, description, htmlFor, children }: AppearanceFieldProps) {
  return (
    <div className="settings-field settings-field--appearance">
      <div className="settings-field__text">
        {htmlFor ? (
          <label className="settings-field__label" htmlFor={htmlFor}>
            {label}
          </label>
        ) : (
          <span className="settings-field__label">{label}</span>
        )}
        <p className="settings-field__hint">{description}</p>
      </div>
      <div className="settings-field__control">{children}</div>
    </div>
  )
}

function AppearanceControls({ client }: { client: UseSettingsReturn }) {
  const {
    settings,
    updateTheme,
    updateDensity,
    updateFontSize,
    updatePlanPendingVariant,
    resetSettings,
  } = client

  return (
    <>
      <AppearanceField label="Theme" description="Match your system or pin a fixed appearance.">
        <SegmentedControl<Theme>
          ariaLabel="Theme"
          value={settings.theme}
          options={THEME_OPTIONS}
          onChange={updateTheme}
        />
      </AppearanceField>

      <AppearanceField
        label="Density"
        description="Compact tightens control and list-row heights across the app."
      >
        <SegmentedControl<Density>
          ariaLabel="Density"
          value={settings.density}
          options={DENSITY_OPTIONS}
          onChange={updateDensity}
        />
      </AppearanceField>

      <AppearanceField
        label="Font size"
        htmlFor={FONT_SIZE_FIELD_ID}
        description="Base size the whole type scale derives from."
      >
        <div className="appearance-font-size">
          <Input
            id={FONT_SIZE_FIELD_ID}
            wrapperClassName="w-auto"
            className="appearance-font-size__range h-auto rounded-none border-0 bg-transparent p-0"
            type="range"
            min={FONT_SIZE_MIN}
            max={FONT_SIZE_MAX}
            step={1}
            value={settings.fontSize}
            onChange={(event) => updateFontSize(Number(event.target.value))}
          />
          <span className="appearance-font-size__value" aria-hidden="true">
            {settings.fontSize}px
          </span>
        </div>
      </AppearanceField>

      <AppearanceField
        label="Plan-pending highlight"
        description="Color used while a plan is awaiting your review."
      >
        <SegmentedControl<PlanPendingVariant>
          ariaLabel="Plan-pending highlight"
          value={settings.planPendingVariant}
          options={PLAN_PENDING_OPTIONS}
          onChange={updatePlanPendingVariant}
        />
      </AppearanceField>

      <Button type="button" variant="secondary" onClick={resetSettings}>
        Reset to defaults
      </Button>
    </>
  )
}

export function AppearanceSection() {
  const { clientSettings } = useSettingsSectionContext()

  return (
    <SettingsSection sectionId="appearance" ownedPaths={OWNED_PATHS}>
      {() =>
        clientSettings ? (
          <AppearanceControls client={clientSettings} />
        ) : (
          <p className="settings-section__pending">
            Appearance settings are unavailable — the settings provider is not mounted.
          </p>
        )
      }
    </SettingsSection>
  )
}
