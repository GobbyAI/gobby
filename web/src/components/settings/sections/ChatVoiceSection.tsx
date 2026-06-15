import { SettingsSection } from './SettingsSection'

// Filled in by leaf #17118.
const OWNED_PATHS: readonly string[] = []

export function ChatVoiceSection() {
  return (
    <SettingsSection sectionId="chat-voice" ownedPaths={OWNED_PATHS}>
      {() => (
        <p className="settings-section__pending">
          Chat and voice settings are being migrated into this section.
        </p>
      )}
    </SettingsSection>
  )
}
