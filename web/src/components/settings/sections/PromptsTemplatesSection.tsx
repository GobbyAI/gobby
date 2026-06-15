import { SettingsSection } from './SettingsSection'

// Filled in by leaf #17126.
const OWNED_PATHS: readonly string[] = []

export function PromptsTemplatesSection() {
  return (
    <SettingsSection sectionId="prompts-templates" ownedPaths={OWNED_PATHS}>
      {() => (
        <p className="settings-section__pending">
          Prompt and template settings are being migrated into this section.
        </p>
      )}
    </SettingsSection>
  )
}
