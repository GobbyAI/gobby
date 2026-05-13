import type { Settings, Theme } from '../hooks/useSettings'
import type { ChatMode } from '../types/chat'
import { CHAT_MODES } from '../types/chat'

interface SettingsProps {
  isOpen: boolean
  onClose: () => void
  settings: Settings
  onFontSizeChange: (size: number) => void
  onThemeChange: (theme: Theme) => void
  onDefaultChatModeChange: (mode: ChatMode) => void
  onPostPlanChatModeChange: (mode: 'normal' | 'bypass') => void
  onReset: () => void
}

export function Settings({
  isOpen,
  onClose,
  settings,
  onFontSizeChange,
  onThemeChange,
  onDefaultChatModeChange,
  onPostPlanChatModeChange,
  onReset,
}: SettingsProps) {
  if (!isOpen) return null

  return (
    <>
      <div className="settings-overlay" onClick={onClose} />
      <div className="settings-panel">
        <div className="settings-header">
          <h2>Settings</h2>
          <button className="close-button" onClick={onClose}>
            &times;
          </button>
        </div>

        <div className="settings-content">
          <div className="setting-item">
            <label htmlFor="font-size">
              Font Size: {settings.fontSize}px
            </label>
            <input
              id="font-size"
              type="range"
              min="12"
              max="48"
              step="1"
              value={settings.fontSize}
              onChange={(e) => onFontSizeChange(Number(e.target.value))}
              className="slider"
            />
            <div className="slider-labels">
              <span>12px</span>
              <span>48px</span>
            </div>
          </div>

          <div className="setting-item">
            <label>Theme</label>
            <div className="theme-selector">
              {(['dark', 'light', 'system'] as const).map((t) => (
                <button
                  key={t}
                  className={`theme-option${settings.theme === t ? ' active' : ''}`}
                  onClick={() => onThemeChange(t)}
                >
                  {t.charAt(0).toUpperCase() + t.slice(1)}
                </button>
              ))}
            </div>
          </div>

          <div className="setting-item">
            <label>Default Mode</label>
            <div className="theme-selector">
              {CHAT_MODES.map((m) => (
                <button
                  key={m.id}
                  className={`theme-option${settings.defaultChatMode === m.id ? ' active' : ''}`}
                  onClick={() => onDefaultChatModeChange(m.id)}
                  title={m.description}
                >
                  {m.label}
                </button>
              ))}
            </div>
          </div>

          <div className="setting-item">
            <label>After Approved Plan</label>
            <div className="theme-selector">
              {CHAT_MODES.filter((m) => m.id === 'normal' || m.id === 'bypass').map((m) => (
                <button
                  key={m.id}
                  className={`theme-option${settings.postPlanChatMode === m.id ? ' active' : ''}`}
                  onClick={() => onPostPlanChatModeChange(m.id as 'normal' | 'bypass')}
                  title={m.description}
                >
                  {m.label}
                </button>
              ))}
            </div>
          </div>

          <div className="settings-actions">
            <button className="reset-button" onClick={onReset}>
              Reset to Defaults
            </button>
          </div>
        </div>
      </div>
    </>
  )
}

// Settings icon SVG
export function SettingsIcon() {
  return (
    <svg
      width="20"
      height="20"
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="2"
      strokeLinecap="round"
      strokeLinejoin="round"
    >
      <circle cx="12" cy="12" r="3" />
      <path d="M12 1v4M12 19v4M4.22 4.22l2.83 2.83M16.95 16.95l2.83 2.83M1 12h4M19 12h4M4.22 19.78l2.83-2.83M16.95 7.05l2.83-2.83" />
    </svg>
  )
}
