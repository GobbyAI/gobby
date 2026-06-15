import { SettingsSection } from './SettingsSection'

// Filled in by leaf #17125.
const OWNED_PATHS: readonly string[] = []

export function SecretsAuthSection() {
  return (
    <SettingsSection sectionId="secrets-auth" ownedPaths={OWNED_PATHS}>
      {() => (
        <p className="settings-section__pending">
          Secrets and auth settings are being migrated into this section.
        </p>
      )}
    </SettingsSection>
  )
}
