import { SettingsSection } from './SettingsSection'

// Filled in by leaf #17122.
const OWNED_PATHS: readonly string[] = []

export function MemoryKnowledgeSection() {
  return (
    <SettingsSection sectionId="memory-knowledge" ownedPaths={OWNED_PATHS}>
      {() => (
        <p className="settings-section__pending">
          Memory and knowledge settings are being migrated into this section.
        </p>
      )}
    </SettingsSection>
  )
}
