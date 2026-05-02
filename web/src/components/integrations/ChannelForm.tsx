import { useState, useCallback } from 'react'
import type { Channel, ChannelType } from '../../hooks/useIntegrations'
import { PlatformIcon } from './IntegrationsPage'
import { CHANNEL_DISPLAY_NAMES, PLATFORM_COLORS } from './channelMetadata'
import {
  MODAL_OVERLAY_CLS,
  MODAL_CLS,
  MODAL_HEADER_CLS,
  MODAL_HEADER_TITLE_CLS,
  MODAL_CLOSE_CLS,
  MODAL_BODY_CLS,
  MODAL_FOOTER_CLS,
  FORM_FIELD_CLS,
  FORM_LABEL_CLS,
  FORM_INPUT_CLS,
  FORM_REQUIRED_CLS,
  FORM_HELP_CLS,
  FORM_ERROR_CLS,
  FORM_CANCEL_CLS,
  FORM_SUBMIT_CLS,
  FORM_CHANGE_BTN_CLS,
  EMPTY_CARD_CLS,
} from './styles'

const FORM_TYPE_GRID_CLS =
  'grid gap-2 [grid-template-columns:repeat(auto-fill,minmax(140px,1fr))]'
const FORM_STATIC_CLS = 'py-2 text-[length:var(--text-sm)] text-[var(--text-primary)]'
const FORM_INPUT_WRAP_CLS = 'relative [&>input]:pr-9'
const FORM_EYE_BTN_CLS =
  'absolute right-2 top-1/2 -translate-y-1/2 cursor-pointer border-0 bg-transparent p-0.5 text-[length:var(--text-md)] text-[var(--text-secondary)] hover:text-[var(--text-primary)] pointer-coarse:h-11 pointer-coarse:w-11'
const FORM_SECRET_CONFIGURED_CLS =
  'flex items-center gap-2 py-2 text-[length:var(--text-sm)] text-[var(--text-secondary)]'

interface FieldDef {
  key: string
  label: string
  secret?: boolean
  required?: boolean
  placeholder?: string
  type?: string
}

const CHANNEL_TYPE_FIELDS: Record<ChannelType, FieldDef[]> = {
  slack: [
    { key: 'bot_token', label: 'Bot Token', secret: true, required: true },
    { key: 'signing_secret', label: 'Signing Secret', secret: true, required: true },
    { key: 'channel_id', label: 'Channel ID', placeholder: 'C0123456789' },
  ],
  telegram: [
    { key: 'bot_token', label: 'Bot Token', secret: true, required: true },
    { key: 'chat_id', label: 'Chat ID', placeholder: '-1001234567890' },
  ],
  discord: [
    { key: 'bot_token', label: 'Bot Token', secret: true, required: true },
    { key: 'channel_id', label: 'Channel ID', placeholder: '1234567890' },
  ],
  teams: [
    { key: 'app_id', label: 'App ID', secret: true, required: true },
    { key: 'app_password', label: 'App Password', secret: true, required: true },
  ],
  email: [
    { key: 'password', label: 'Password', secret: true, required: true },
    { key: 'smtp_host', label: 'SMTP Host', required: true, placeholder: 'smtp.gmail.com' },
    { key: 'smtp_port', label: 'SMTP Port', required: true, type: 'number', placeholder: '587' },
    { key: 'imap_host', label: 'IMAP Host', required: true, placeholder: 'imap.gmail.com' },
    { key: 'imap_port', label: 'IMAP Port', required: true, type: 'number', placeholder: '993' },
    { key: 'from_address', label: 'From Address', required: true, type: 'email', placeholder: 'you@example.com' },
  ],
  sms: [
    { key: 'auth_token', label: 'Auth Token', secret: true, required: true },
    { key: 'account_sid', label: 'Account SID', required: true, placeholder: 'AC...' },
    { key: 'from_number', label: 'From Number', required: true, placeholder: '+15551234567' },
  ],
  gobby_chat: [],
}

const ALL_TYPES: ChannelType[] = ['slack', 'telegram', 'discord', 'teams', 'email', 'sms', 'gobby_chat']

interface ChannelFormProps {
  mode: 'add' | 'edit'
  channel?: Channel
  presetType?: ChannelType | null
  onSubmit: (channelType: string, name: string, config: Record<string, unknown>, secrets?: Record<string, unknown>) => Promise<boolean>
  onClose: () => void
}

export function ChannelForm({ mode, channel, presetType, onSubmit, onClose }: ChannelFormProps) {
  const [selectedType, setSelectedType] = useState<ChannelType | null>(
    mode === 'edit' ? (channel?.channel_type ?? null) : (presetType ?? null)
  )
  const [name, setName] = useState(channel?.name ?? '')
  const [values, setValues] = useState<Record<string, string>>(() => {
    if (mode === 'edit' && channel) {
      const init: Record<string, string> = {}
      const fields = CHANNEL_TYPE_FIELDS[channel.channel_type] || []
      for (const f of fields) {
        if (!f.secret) {
          const v = channel.config_json[f.key]
          if (v != null) init[f.key] = String(v)
        }
      }
      return init
    }
    return {}
  })
  const [showSecrets, setShowSecrets] = useState<Set<string>>(new Set())
  const [changingSecrets, setChangingSecrets] = useState<Set<string>>(new Set())
  const [error, setError] = useState<string | null>(null)
  const [saving, setSaving] = useState(false)

  const fields = selectedType ? CHANNEL_TYPE_FIELDS[selectedType] : []

  const setValue = useCallback((key: string, val: string) => {
    setValues(prev => ({ ...prev, [key]: val }))
  }, [])

  const toggleShowSecret = useCallback((key: string) => {
    setShowSecrets(prev => {
      const next = new Set(prev)
      if (next.has(key)) next.delete(key)
      else next.add(key)
      return next
    })
  }, [])

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault()
    if (!selectedType) { setError('Select a channel type'); return }
    if (!name.trim()) { setError('Name is required'); return }

    // Validate required fields
    for (const f of fields) {
      if (f.required) {
        if (mode === 'edit' && f.secret && !changingSecrets.has(f.key)) continue
        if (!values[f.key]?.trim()) {
          setError(`${f.label} is required`)
          return
        }
      }
    }

    setSaving(true)
    setError(null)

    const config: Record<string, unknown> = {}
    const secrets: Record<string, unknown> = {}

    for (const f of fields) {
      const val = values[f.key]?.trim()
      if (!val) continue
      if (f.secret) {
        if (mode === 'edit' && !changingSecrets.has(f.key)) continue
        secrets[f.key] = val
      } else {
        config[f.key] = f.type === 'number' ? Number(val) : val
      }
    }

    const ok = await onSubmit(
      selectedType,
      name.trim(),
      config,
      Object.keys(secrets).length > 0 ? secrets : undefined,
    )
    setSaving(false)
    if (ok) onClose()
    else setError('Failed to save channel')
  }

  // Type selection grid (add mode, no preset)
  if (mode === 'add' && !selectedType) {
    return (
      <div className={MODAL_OVERLAY_CLS} onClick={onClose}>
        <div className={MODAL_CLS} onClick={e => e.stopPropagation()}>
          <div className={MODAL_HEADER_CLS}>
            <h3 className={MODAL_HEADER_TITLE_CLS}>Add Integration</h3>
            <button className={MODAL_CLOSE_CLS} onClick={onClose}>&times;</button>
          </div>
          <div className={MODAL_BODY_CLS}>
            <p className={FORM_HELP_CLS}>Select a platform:</p>
            <div className={FORM_TYPE_GRID_CLS}>
              {ALL_TYPES.map(type => (
                <div
                  key={type}
                  className={EMPTY_CARD_CLS}
                  onClick={() => setSelectedType(type)}
                  style={{ borderLeftWidth: 3, borderLeftColor: PLATFORM_COLORS[type] }}
                >
                  <PlatformIcon type={type} />
                  {CHANNEL_DISPLAY_NAMES[type]}
                </div>
              ))}
            </div>
          </div>
        </div>
      </div>
    )
  }

  return (
    <div className={MODAL_OVERLAY_CLS} onClick={onClose}>
      <form className={MODAL_CLS} onClick={e => e.stopPropagation()} onSubmit={handleSubmit}>
        <div className={MODAL_HEADER_CLS}>
          <h3 className={MODAL_HEADER_TITLE_CLS}>{mode === 'add' ? 'Add' : 'Edit'} {selectedType ? CHANNEL_DISPLAY_NAMES[selectedType] : ''} Channel</h3>
          <button type="button" className={MODAL_CLOSE_CLS} onClick={onClose}>&times;</button>
        </div>

        <div className={MODAL_BODY_CLS}>
          {error && <div className={FORM_ERROR_CLS}>{error}</div>}

          {/* Name field */}
          <div className={FORM_FIELD_CLS}>
            <label className={FORM_LABEL_CLS}>Name</label>
            <input
              className={FORM_INPUT_CLS}
              value={name}
              onChange={e => setName(e.target.value)}
              placeholder="my-channel"
              disabled={mode === 'edit'}
              autoFocus={mode === 'add'}
            />
          </div>

          {/* Type display (edit mode) */}
          {mode === 'edit' && selectedType && (
            <div className={FORM_FIELD_CLS}>
              <label className={FORM_LABEL_CLS}>Type</label>
              <div className={FORM_STATIC_CLS}>{CHANNEL_DISPLAY_NAMES[selectedType]}</div>
            </div>
          )}

          {/* No config needed */}
          {selectedType === 'gobby_chat' && (
            <p className={FORM_HELP_CLS}>No additional configuration required.</p>
          )}

          {/* Dynamic fields */}
          {fields.map(f => {
            const isEditing = mode === 'edit'
            const isSecret = f.secret
            const isChanging = changingSecrets.has(f.key)

            // In edit mode, secret fields show "Configured" with Change button
            if (isEditing && isSecret && !isChanging) {
              return (
                <div key={f.key} className={FORM_FIELD_CLS}>
                  <label className={FORM_LABEL_CLS}>{f.label}</label>
                  <div className={FORM_SECRET_CONFIGURED_CLS}>
                    <span>Configured</span>
                    <button
                      type="button"
                      className={FORM_CHANGE_BTN_CLS}
                      onClick={() => setChangingSecrets(prev => new Set(prev).add(f.key))}
                    >
                      Change
                    </button>
                  </div>
                </div>
              )
            }

            return (
              <div key={f.key} className={FORM_FIELD_CLS}>
                <label className={FORM_LABEL_CLS}>
                  {f.label}
                  {f.required && <span className={FORM_REQUIRED_CLS}> *</span>}
                </label>
                <div className={FORM_INPUT_WRAP_CLS}>
                  <input
                    className={FORM_INPUT_CLS}
                    type={isSecret && !showSecrets.has(f.key) ? 'password' : (f.type || 'text')}
                    value={values[f.key] || ''}
                    onChange={e => setValue(f.key, e.target.value)}
                    placeholder={f.placeholder}
                  />
                  {isSecret && (
                    <button
                      type="button"
                      className={FORM_EYE_BTN_CLS}
                      onClick={() => toggleShowSecret(f.key)}
                      title={showSecrets.has(f.key) ? 'Hide' : 'Show'}
                    >
                      {showSecrets.has(f.key) ? '◉' : '◎'}
                    </button>
                  )}
                </div>
              </div>
            )
          })}
        </div>

        <div className={MODAL_FOOTER_CLS}>
          <button type="button" className={FORM_CANCEL_CLS} onClick={onClose}>Cancel</button>
          <button type="submit" className={FORM_SUBMIT_CLS} disabled={saving}>
            {saving ? 'Saving...' : mode === 'add' ? 'Add Channel' : 'Save Changes'}
          </button>
        </div>
      </form>
    </div>
  )
}
