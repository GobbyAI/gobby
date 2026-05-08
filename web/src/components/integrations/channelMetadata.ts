import type { ChannelType } from '../../hooks/useIntegrations'

export interface ChannelColorPair {
  dark: string
  light: string
}

export const CHANNEL_COLOR_PAIRS: Record<ChannelType, ChannelColorPair> = {
  slack: { dark: 'oklch(72% 0.16 320)', light: 'oklch(38% 0.16 320)' },
  telegram: { dark: 'oklch(72% 0.13 235)', light: 'oklch(45% 0.16 235)' },
  discord: { dark: 'oklch(72% 0.16 270)', light: 'oklch(45% 0.20 270)' },
  teams: { dark: 'oklch(70% 0.12 285)', light: 'oklch(42% 0.14 285)' },
  email: { dark: 'oklch(70% 0.16 25)', light: 'oklch(45% 0.20 25)' },
  sms: { dark: 'oklch(74% 0.15 145)', light: 'oklch(45% 0.16 145)' },
  gobby_chat: { dark: 'oklch(70% 0.005 125)', light: 'oklch(45% 0.005 125)' },
}

export function getChannelColorVar(channel: ChannelType): string {
  return `var(--channel-${channel})`
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
