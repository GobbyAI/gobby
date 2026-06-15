import { SettingsSection } from './SettingsSection'

// Filled in by leaf #17123.
const OWNED_PATHS: readonly string[] = []

export function ObservabilitySection() {
  return (
    <SettingsSection sectionId="observability" ownedPaths={OWNED_PATHS}>
      {() => (
        <p className="settings-section__pending">
          Observability settings are being migrated into this section.
        </p>
      )}
    </SettingsSection>
  )
}
