import type { Channel } from '../../hooks/useIntegrations'
import { PlatformIcon } from './IntegrationsPage'
import { CHANNEL_DISPLAY_NAMES, PLATFORM_COLORS } from './channelMetadata'
import './IntegrationsPage.css'

interface ChannelCardProps {
  channel: Channel
  onSelect: (channel: Channel) => void
  onEdit: (channel: Channel) => void
  onToggleEnabled: (channel: Channel) => void
  onRemove: (channel: Channel) => void
}

export function ChannelCard({ channel, onSelect, onEdit, onToggleEnabled, onRemove }: ChannelCardProps) {
  const color = PLATFORM_COLORS[channel.channel_type] || 'var(--border-color)'
  const disabled = !channel.enabled

  return (
    <div
      className={`intg-card ${disabled ? 'intg-card--disabled' : ''}`}
      onClick={() => onSelect(channel)}
      style={{
        borderLeftWidth: 3,
        borderLeftStyle: 'solid',
        borderLeftColor: color,
        opacity: disabled ? 0.7 : 1,
      }}
    >
      <div className="intg-card-header">
        <PlatformIcon type={channel.channel_type} />
        <span className="intg-card-name">{channel.name}</span>
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
      <div className="intg-card-status">
        <span
          className="intg-status-dot"
          style={{ background: channel.enabled ? '#22c55e' : 'var(--text-secondary)' }}
        />
        <span className="intg-status-text">
          {channel.enabled ? 'Enabled' : 'Disabled'}
        </span>
      </div>
      <div className="intg-card-footer">
        <button
          className="intg-card-action"
          title="Edit"
          onClick={e => { e.stopPropagation(); onEdit(channel) }}
        >
          &#9998;
        </button>
        <button
          className="intg-card-action"
          title={channel.enabled ? 'Disable' : 'Enable'}
          onClick={e => { e.stopPropagation(); onToggleEnabled(channel) }}
        >
          {channel.enabled ? '\u23F8' : '\u25B6'}
        </button>
        <button
          className="intg-card-action intg-card-action--danger"
          title="Remove"
          onClick={e => { e.stopPropagation(); onRemove(channel) }}
        >
          &times;
        </button>
      </div>
    </div>
  )
}
