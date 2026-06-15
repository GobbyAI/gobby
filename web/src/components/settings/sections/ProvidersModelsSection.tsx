import { SettingsSection } from './SettingsSection'

// Filled in by leaf #17117.
const OWNED_PATHS: readonly string[] = []

export function ProvidersModelsSection() {
  return (
    <SettingsSection sectionId="providers-models" ownedPaths={OWNED_PATHS}>
      {() => (
        <p className="settings-section__pending">
          Provider and model settings are being migrated into this section.
        </p>
      )}
    </SettingsSection>
  )
}
