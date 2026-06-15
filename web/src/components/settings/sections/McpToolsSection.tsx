import { SettingsSection } from './SettingsSection'

// Filled in by leaf #17121.
const OWNED_PATHS: readonly string[] = []

export function McpToolsSection() {
  return (
    <SettingsSection sectionId="mcp-tools" ownedPaths={OWNED_PATHS}>
      {() => (
        <p className="settings-section__pending">
          MCP and tool settings are being migrated into this section.
        </p>
      )}
    </SettingsSection>
  )
}
