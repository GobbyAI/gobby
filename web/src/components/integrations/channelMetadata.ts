import type { ChannelType } from '../../hooks/useIntegrations'

export const PLATFORM_COLORS: Record<ChannelType, string> = {
  slack: '#611f69',
  telegram: '#229ED9',
  discord: '#5865F2',
  teams: '#6264A7',
  email: '#D44638',
  sms: '#25D366',
  gobby_chat: 'var(--text-secondary)',
}

export const CHANNEL_DISPLAY_NAMES: Record<ChannelType, string> = {
  slack: 'Slack',
  telegram: 'Telegram',
  discord: 'Discord',
  teams: 'Teams',
  email: 'Email',
  sms: 'SMS',
  gobby_chat: 'Gobby Chat',
}
