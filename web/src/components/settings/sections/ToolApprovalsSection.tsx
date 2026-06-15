import { SettingsSection } from './SettingsSection'

// Filled in by leaf #17124.
const OWNED_PATHS: readonly string[] = []

export function ToolApprovalsSection() {
  return (
    <SettingsSection sectionId="tool-approvals" ownedPaths={OWNED_PATHS}>
      {() => (
        <p className="settings-section__pending">
          Tool approval settings are being migrated into this section.
        </p>
      )}
    </SettingsSection>
  )
}
