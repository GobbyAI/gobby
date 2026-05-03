import type { Channel } from '../../hooks/useIntegrations'
import { PlatformIcon } from './IntegrationsPage'
import { CHANNEL_DISPLAY_NAMES, getChannelColorVar } from './channelMetadata'
import {
  STATUS_DOT_ACTIVE_COLOR,
  STATUS_DOT_CLS,
  STATUS_DOT_INACTIVE_COLOR,
  TYPE_BADGE_CLS,
} from './styles'
import { cn } from '../../lib/utils'

const CARD_CLS =
  'flex cursor-pointer flex-col gap-2 rounded-lg border border-[var(--border)] bg-[var(--bg-secondary)] px-4 py-3.5 transition-colors duration-150 hover:border-[var(--accent)] hover:bg-[rgba(255,255,255,0.05)]'
const CARD_DISABLED_CLS = 'opacity-70'
const CARD_HEADER_CLS = 'flex items-center gap-2'
const CARD_NAME_CLS =
  'min-w-0 flex-1 overflow-hidden text-ellipsis whitespace-nowrap text-[length:var(--text-sm)] font-medium'
const CARD_STATUS_CLS = 'flex items-center gap-1.5'
const STATUS_TEXT_CLS = 'text-[length:var(--text-xs)] text-[var(--text-secondary)]'
const CARD_FOOTER_CLS = 'flex justify-end gap-1'
const CARD_ACTION_CLS =
  'cursor-pointer rounded border-0 bg-transparent px-1.5 py-1 text-[length:var(--text-sm)] text-[var(--text-secondary)] transition-colors duration-150 hover:bg-[rgba(255,255,255,0.1)] hover:text-[var(--text-primary)] pointer-coarse:h-11 pointer-coarse:w-11'
const CARD_ACTION_DANGER_CLS = 'hover:text-[var(--color-error)]'

interface ChannelCardProps {
  channel: Channel
  onSelect: (channel: Channel) => void
  onEdit: (channel: Channel) => void
  onToggleEnabled: (channel: Channel) => void
  onRemove: (channel: Channel) => void
}

export function ChannelCard({ channel, onSelect, onEdit, onToggleEnabled, onRemove }: ChannelCardProps) {
  const color = getChannelColorVar(channel.channel_type)
  const disabled = !channel.enabled

  return (
    <div
      className={cn(CARD_CLS, disabled && CARD_DISABLED_CLS)}
      onClick={() => onSelect(channel)}
      style={{
        borderLeftWidth: 3,
        borderLeftStyle: 'solid',
        borderLeftColor: color,
      }}
    >
      <div className={CARD_HEADER_CLS}>
        <PlatformIcon type={channel.channel_type} />
        <span className={CARD_NAME_CLS}>{channel.name}</span>
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
      <div className={CARD_STATUS_CLS}>
        <span
          className={STATUS_DOT_CLS}
          style={{ background: channel.enabled ? STATUS_DOT_ACTIVE_COLOR : STATUS_DOT_INACTIVE_COLOR }}
        />
        <span className={STATUS_TEXT_CLS}>
          {channel.enabled ? 'Enabled' : 'Disabled'}
        </span>
      </div>
      <div className={CARD_FOOTER_CLS}>
        <button
          className={CARD_ACTION_CLS}
          title="Edit"
          onClick={e => { e.stopPropagation(); onEdit(channel) }}
        >
          &#9998;
        </button>
        <button
          className={CARD_ACTION_CLS}
          title={channel.enabled ? 'Disable' : 'Enable'}
          onClick={e => { e.stopPropagation(); onToggleEnabled(channel) }}
        >
          {channel.enabled ? '⏸' : '▶'}
        </button>
        <button
          className={cn(CARD_ACTION_CLS, CARD_ACTION_DANGER_CLS)}
          title="Remove"
          onClick={e => { e.stopPropagation(); onRemove(channel) }}
        >
          &times;
        </button>
      </div>
    </div>
  )
}
