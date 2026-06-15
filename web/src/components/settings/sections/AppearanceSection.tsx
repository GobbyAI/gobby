import { SettingsSection } from './SettingsSection'

// Filled in by leaf #17116. Until then the shell renders the section's
// header and a pending note; the owned-path list and fields land with the leaf.
const OWNED_PATHS: readonly string[] = []

export function AppearanceSection() {
  return (
    <SettingsSection sectionId="appearance" ownedPaths={OWNED_PATHS}>
      {() => (
        <p className="settings-section__pending">
          Appearance settings are being migrated into this section.
        </p>
      )}
    </SettingsSection>
  )
}
