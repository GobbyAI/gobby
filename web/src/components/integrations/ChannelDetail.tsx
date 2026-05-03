import { useState, useEffect, useCallback } from 'react'
import type { Channel, ChannelType, ChannelStatus } from '../../hooks/useIntegrations'
import { PlatformIcon } from './IntegrationsPage'
import { CHANNEL_DISPLAY_NAMES, getChannelColorVar } from './channelMetadata'
import {
  TYPE_BADGE_CLS,
  STATUS_DOT_CLS,
  STATUS_DOT_ACTIVE_COLOR,
  STATUS_DOT_ERROR_COLOR,
  STATUS_DOT_INACTIVE_COLOR,
  MODAL_CLOSE_CLS,
  FORM_CHANGE_BTN_CLS,
  FORM_CANCEL_CLS,
} from './styles'

const WEBHOOK_TYPES: ChannelType[] = ['slack', 'telegram', 'discord', 'teams', 'sms']

const OVERLAY_CLS = 'fixed inset-0 z-[800] bg-[var(--surface-scrim)]'
const PANEL_CLS =
  'fixed bottom-0 right-0 top-0 z-[850] flex h-screen w-[400px] max-w-[90vw] flex-col overflow-y-auto border-l border-[var(--border)] bg-[var(--bg-primary)] [box-shadow:var(--shadow-panel-left,-4px_0_20px_oklch(0%_0_0_/_0.2))] max-md:w-screen max-md:max-w-none'

const HEADER_CLS = 'border-b border-[var(--border)] px-5 py-4'
const HEADER_INFO_CLS = 'mt-2 flex items-center gap-2'
const NAME_CLS = 'text-[length:var(--text-base)] font-semibold'

const SECTION_CLS = 'border-b border-[var(--border)] px-5 py-3.5'
const SECTION_TITLE_CLS =
  'm-0 mb-2.5 text-[length:var(--text-xs)] font-semibold uppercase tracking-[0.05em] text-[var(--text-secondary)]'

const GRID_CLS =
  'grid items-center gap-x-3 gap-y-1.5 text-[length:var(--text-sm)] [grid-template-columns:auto_1fr]'
const LABEL_CLS = 'text-[length:var(--text-xs)] text-[var(--text-secondary)]'
const VALUE_CLS = 'break-all text-[var(--text-primary)]'
const EMPTY_CLS = 'col-span-full italic text-[var(--text-secondary)]'
const LOADING_CLS = 'text-[length:var(--text-xs)] text-[var(--text-secondary)]'
const CONFIG_ROW_CLS = 'contents'

const WEBHOOK_CLS = 'flex items-center gap-2'
const WEBHOOK_URL_CLS =
  'flex-1 break-all rounded border border-[var(--border)] bg-[var(--bg-secondary)] px-2.5 py-1.5 font-mono text-[length:var(--text-xs)]'

const ACTIONS_CLS = 'flex flex-wrap gap-2 px-5 py-4'
const REMOVE_BTN_CLS =
  'cursor-pointer rounded-md border border-[var(--color-error)] bg-transparent px-4 py-2 text-[length:var(--text-sm)] text-[var(--color-error)] hover:bg-[var(--color-error-soft)] pointer-coarse:min-h-11'

interface ChannelDetailProps {
  channel: Channel | null
  onClose: () => void
  onEdit: (channel: Channel) => void
  onToggleEnabled: (channel: Channel) => void
  onRemove: (channel: Channel) => void
  fetchStatus: (channelId: string) => Promise<ChannelStatus | null>
}

export function ChannelDetail({
  channel,
  onClose,
  onEdit,
  onToggleEnabled,
  onRemove,
}: ChannelDetailProps) {
  const [status, setStatus] = useState<ChannelStatus | null>(null)
  const [statusLoading, setStatusLoading] = useState(false)
  const [copied, setCopied] = useState(false)

  useEffect(() => {
    if (!channel) {
      setStatus(null)
      return
    }
    setStatusLoading(true)
    setStatus(null)

    const baseUrl = ''
    fetch(`${baseUrl}/api/comms/channels/${encodeURIComponent(channel.id)}/status`)
      .then(r => r.ok ? r.json() : null)
      .then(data => setStatus(data))
      .catch(() => setStatus(null))
      .finally(() => setStatusLoading(false))
  }, [channel])

  const handleCopyWebhook = useCallback(async (url: string) => {
    try {
      await navigator.clipboard.writeText(url)
      setCopied(true)
      setTimeout(() => setCopied(false), 2000)
    } catch {
      // fallback
    }
  }, [])

  const formatDate = (iso: string) => {
    try {
      const d = new Date(iso)
      const now = new Date()
      const diff = now.getTime() - d.getTime()
      const mins = Math.floor(diff / 60000)
      if (mins < 1) return 'just now'
      if (mins < 60) return `${mins}m ago`
      const hours = Math.floor(mins / 60)
      if (hours < 24) return `${hours}h ago`
      const days = Math.floor(hours / 24)
      return `${days}d ago`
    } catch {
      return iso
    }
  }

  if (!channel) return null

  const color = getChannelColorVar(channel.channel_type)
  const webhookUrl = WEBHOOK_TYPES.includes(channel.channel_type)
    ? `${window.location.origin}/api/comms/webhooks/${channel.name}`
    : null

  return (
    <>
      <div className={OVERLAY_CLS} onClick={onClose} />
      <div className={PANEL_CLS}>
        {/* Header */}
        <div className={HEADER_CLS}>
          <button className={MODAL_CLOSE_CLS} onClick={onClose}>&times;</button>
          <div className={HEADER_INFO_CLS}>
            <PlatformIcon type={channel.channel_type} size={20} />
            <span className={NAME_CLS}>{channel.name}</span>
            <span
              className={TYPE_BADGE_CLS}
              style={{
                background: `${color}1F`,
                color: color.startsWith('var(') ? 'var(--text-secondary)' : color,
              }}
            >
              {CHANNEL_DISPLAY_NAMES[channel.channel_type]}
            </span>
          </div>
        </div>

        {/* Status */}
        <div className={SECTION_CLS}>
          <h4 className={SECTION_TITLE_CLS}>Status</h4>
          {statusLoading ? (
            <span className={LOADING_CLS}>Loading...</span>
          ) : status ? (
            <div className={GRID_CLS}>
              <span className={LABEL_CLS}>Active</span>
              <span>
                <span
                  className={STATUS_DOT_CLS}
                  style={{ background: status.active ? STATUS_DOT_ACTIVE_COLOR : STATUS_DOT_ERROR_COLOR }}
                />
                {' '}{status.active ? 'Active' : 'Inactive'}
              </span>
              <span className={LABEL_CLS}>Enabled</span>
              <span>{status.enabled ? 'Yes' : 'No'}</span>
              {status.supports_webhooks != null && (
                <>
                  <span className={LABEL_CLS}>Webhooks</span>
                  <span>{status.supports_webhooks ? 'Supported' : 'Not supported'}</span>
                </>
              )}
              {status.supports_polling != null && (
                <>
                  <span className={LABEL_CLS}>Polling</span>
                  <span>
                    {status.supports_polling ? (status.is_polling ? 'Active' : 'Supported') : 'Not supported'}
                  </span>
                </>
              )}
            </div>
          ) : (
            <div className={GRID_CLS}>
              <span className={LABEL_CLS}>Enabled</span>
              <span>
                <span
                  className={STATUS_DOT_CLS}
                  style={{ background: channel.enabled ? STATUS_DOT_ACTIVE_COLOR : STATUS_DOT_INACTIVE_COLOR }}
                />
                {' '}{channel.enabled ? 'Yes' : 'No'}
              </span>
            </div>
          )}
        </div>

        {/* Webhook URL */}
        {webhookUrl && (
          <div className={SECTION_CLS}>
            <h4 className={SECTION_TITLE_CLS}>Webhook URL</h4>
            <div className={WEBHOOK_CLS}>
              <code className={WEBHOOK_URL_CLS}>{webhookUrl}</code>
              <button
                className={FORM_CHANGE_BTN_CLS}
                onClick={() => handleCopyWebhook(webhookUrl)}
              >
                {copied ? 'Copied!' : 'Copy'}
              </button>
            </div>
          </div>
        )}

        {/* Configuration */}
        <div className={SECTION_CLS}>
          <h4 className={SECTION_TITLE_CLS}>Configuration</h4>
          <div className={GRID_CLS}>
            {Object.entries(channel.config_json).map(([key, value]) => (
              <div key={key} className={CONFIG_ROW_CLS}>
                <span className={LABEL_CLS}>{key}</span>
                <span className={VALUE_CLS}>
                  {typeof value === 'string' && value.startsWith('$secret:')
                    ? 'Configured'
                    : String(value ?? 'Not set')}
                </span>
              </div>
            ))}
            {Object.keys(channel.config_json).length === 0 && (
              <span className={EMPTY_CLS}>No configuration fields</span>
            )}
          </div>
        </div>

        {/* Metadata */}
        <div className={SECTION_CLS}>
          <h4 className={SECTION_TITLE_CLS}>Metadata</h4>
          <div className={GRID_CLS}>
            <span className={LABEL_CLS}>Created</span>
            <span>{formatDate(channel.created_at)}</span>
            <span className={LABEL_CLS}>Updated</span>
            <span>{formatDate(channel.updated_at)}</span>
          </div>
        </div>

        {/* Actions */}
        <div className={ACTIONS_CLS}>
          <button className={FORM_CANCEL_CLS} onClick={() => onEdit(channel)}>Edit</button>
          <button
            className={FORM_CANCEL_CLS}
            onClick={() => onToggleEnabled(channel)}
          >
            {channel.enabled ? 'Disable' : 'Enable'}
          </button>
          <button
            className={REMOVE_BTN_CLS}
            onClick={() => onRemove(channel)}
          >
            Remove
          </button>
        </div>
      </div>
    </>
  )
}
