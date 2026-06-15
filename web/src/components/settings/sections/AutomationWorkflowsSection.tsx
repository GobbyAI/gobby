import { SettingsSection } from './SettingsSection'

// Filled in by leaf #17120.
const OWNED_PATHS: readonly string[] = []

export function AutomationWorkflowsSection() {
  return (
    <SettingsSection sectionId="automation-workflows" ownedPaths={OWNED_PATHS}>
      {() => (
        <p className="settings-section__pending">
          Automation and workflow settings are being migrated into this section.
        </p>
      )}
    </SettingsSection>
  )
}
