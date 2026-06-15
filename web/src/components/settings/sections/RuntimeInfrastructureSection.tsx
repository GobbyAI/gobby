import { SettingsSection } from './SettingsSection'

// Filled in by leaf #17128.
const OWNED_PATHS: readonly string[] = []

export function RuntimeInfrastructureSection() {
  return (
    <SettingsSection sectionId="runtime-infrastructure" ownedPaths={OWNED_PATHS}>
      {() => (
        <p className="settings-section__pending">
          Runtime and infrastructure settings are being migrated into this section.
        </p>
      )}
    </SettingsSection>
  )
}
