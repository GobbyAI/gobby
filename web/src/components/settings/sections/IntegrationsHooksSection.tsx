import { SettingsSection } from './SettingsSection'

// Filled in by leaf #17127.
const OWNED_PATHS: readonly string[] = []

export function IntegrationsHooksSection() {
  return (
    <SettingsSection sectionId="integrations-hooks" ownedPaths={OWNED_PATHS}>
      {() => (
        <p className="settings-section__pending">
          Integration and hook settings are being migrated into this section.
        </p>
      )}
    </SettingsSection>
  )
}
