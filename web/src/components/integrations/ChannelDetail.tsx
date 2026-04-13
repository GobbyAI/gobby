import { useState, useEffect, useCallback } from 'react'
import type { Channel, ChannelType, ChannelStatus } from '../../hooks/useIntegrations'
import { PlatformIcon } from './IntegrationsPage'
import { CHANNEL_DISPLAY_NAMES, PLATFORM_COLORS } from './channelMetadata'
import './IntegrationsPage.css'

const WEBHOOK_TYPES: ChannelType[] = ['slack', 'telegram', 'discord', 'teams', 'sms']

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

  const color = PLATFORM_COLORS[channel.channel_type] || 'var(--border-color)'
  const webhookUrl = WEBHOOK_TYPES.includes(channel.channel_type)
    ? `${window.location.origin}/api/comms/webhooks/${channel.name}`
    : null

  return (
    <>
      <div className="intg-detail-overlay" onClick={onClose} />
      <div className="intg-detail-panel">
        {/* Header */}
        <div className="intg-detail-header">
          <button className="intg-modal-close" onClick={onClose}>&times;</button>
          <div className="intg-detail-header-info">
            <PlatformIcon type={channel.channel_type} size={20} />
            <span className="intg-detail-name">{channel.name}</span>
            <span
              className="intg-type-badge"
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
        <div className="intg-detail-section">
          <h4 className="intg-detail-section-title">Status</h4>
          {statusLoading ? (
            <span className="intg-detail-loading">Loading...</span>
          ) : status ? (
            <div className="intg-detail-grid">
              <span className="intg-detail-label">Active</span>
              <span>
                <span
                  className="intg-status-dot"
                  style={{ background: status.active ? '#22c55e' : '#ef4444' }}
                />
                {' '}{status.active ? 'Active' : 'Inactive'}
              </span>
              <span className="intg-detail-label">Enabled</span>
              <span>{status.enabled ? 'Yes' : 'No'}</span>
              {status.supports_webhooks != null && (
                <>
                  <span className="intg-detail-label">Webhooks</span>
                  <span>{status.supports_webhooks ? 'Supported' : 'Not supported'}</span>
                </>
              )}
              {status.supports_polling != null && (
                <>
                  <span className="intg-detail-label">Polling</span>
                  <span>
                    {status.supports_polling ? (status.is_polling ? 'Active' : 'Supported') : 'Not supported'}
                  </span>
                </>
              )}
            </div>
          ) : (
            <div className="intg-detail-grid">
              <span className="intg-detail-label">Enabled</span>
              <span>
                <span
                  className="intg-status-dot"
                  style={{ background: channel.enabled ? '#22c55e' : 'var(--text-secondary)' }}
                />
                {' '}{channel.enabled ? 'Yes' : 'No'}
              </span>
            </div>
          )}
        </div>

        {/* Webhook URL */}
        {webhookUrl && (
          <div className="intg-detail-section">
            <h4 className="intg-detail-section-title">Webhook URL</h4>
            <div className="intg-detail-webhook">
              <code className="intg-detail-webhook-url">{webhookUrl}</code>
              <button
                className="intg-form-change-btn"
                onClick={() => handleCopyWebhook(webhookUrl)}
              >
                {copied ? 'Copied!' : 'Copy'}
              </button>
            </div>
          </div>
        )}

        {/* Configuration */}
        <div className="intg-detail-section">
          <h4 className="intg-detail-section-title">Configuration</h4>
          <div className="intg-detail-grid">
            {Object.entries(channel.config_json).map(([key, value]) => (
              <div key={key} className="intg-detail-config-row">
                <span className="intg-detail-label">{key}</span>
                <span className="intg-detail-value">
                  {typeof value === 'string' && value.startsWith('$secret:')
                    ? 'Configured'
                    : String(value ?? 'Not set')}
                </span>
              </div>
            ))}
            {Object.keys(channel.config_json).length === 0 && (
              <span className="intg-detail-empty">No configuration fields</span>
            )}
          </div>
        </div>

        {/* Metadata */}
        <div className="intg-detail-section">
          <h4 className="intg-detail-section-title">Metadata</h4>
          <div className="intg-detail-grid">
            <span className="intg-detail-label">Created</span>
            <span>{formatDate(channel.created_at)}</span>
            <span className="intg-detail-label">Updated</span>
            <span>{formatDate(channel.updated_at)}</span>
          </div>
        </div>

        {/* Actions */}
        <div className="intg-detail-actions">
          <button className="intg-form-cancel" onClick={() => onEdit(channel)}>Edit</button>
          <button
            className="intg-form-cancel"
            onClick={() => onToggleEnabled(channel)}
          >
            {channel.enabled ? 'Disable' : 'Enable'}
          </button>
          <button
            className="intg-detail-remove-btn"
            onClick={() => onRemove(channel)}
          >
            Remove
          </button>
        </div>
      </div>
    </>
  )
}
