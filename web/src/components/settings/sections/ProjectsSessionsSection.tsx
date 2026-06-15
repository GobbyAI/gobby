import { SettingsSection } from './SettingsSection'

// Filled in by leaf #17119.
const OWNED_PATHS: readonly string[] = []

export function ProjectsSessionsSection() {
  return (
    <SettingsSection sectionId="projects-sessions" ownedPaths={OWNED_PATHS}>
      {() => (
        <p className="settings-section__pending">
          Project and session settings are being migrated into this section.
        </p>
      )}
    </SettingsSection>
  )
}
