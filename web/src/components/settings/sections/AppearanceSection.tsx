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
    <div className="flex flex-row flex-wrap items-center justify-between gap-4">
      <div className="flex min-w-0 flex-col gap-1">
        {htmlFor ? (
          <label
            className="text-base font-medium leading-[1.3] text-foreground"
            htmlFor={htmlFor}
          >
            {label}
          </label>
        ) : (
          <span className="text-base font-medium leading-[1.3] text-foreground">
            {label}
          </span>
        )}
        <p className="max-w-[48ch] text-sm leading-[1.4] text-muted-foreground">
          {description}
        </p>
      </div>
      <div className="flex shrink-0 items-center">{children}</div>
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
        <div className="flex items-center gap-3">
          <Input
            id={FONT_SIZE_FIELD_ID}
            wrapperClassName="w-auto"
            className="h-auto max-w-full cursor-pointer rounded-none border-0 bg-transparent p-0 accent-accent focus-visible:outline-2 focus-visible:outline-accent focus-visible:outline-offset-[3px] pointer-coarse:min-h-11"
            type="range"
            min={FONT_SIZE_MIN}
            max={FONT_SIZE_MAX}
            step={1}
            value={settings.fontSize}
            onChange={(event) => updateFontSize(Number(event.target.value))}
          />
          <span
            className="min-w-[3.5ch] text-right text-sm leading-[1.6] text-muted-foreground tabular-nums"
            aria-hidden="true"
          >
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
          <p className="rounded-lg border border-border bg-muted px-5 py-4 text-sm leading-[1.5] text-foreground-muted">
            Appearance settings are unavailable — the settings provider is not mounted.
          </p>
        )
      }
    </SettingsSection>
  )
}
