export const SOURCE_COLORS: Record<string, string> = {
  claude: '#c084fc',
  gemini: '#4ade80',
  qwen: '#f59e0b',
  codex: '#3b82f6',
  droid: '#22d3ee',
  pipeline: '#737373',
  cron: '#a3a3a3',
  unknown: '#737373',
  default: '#737373',
}

export const SOURCE_LABELS: Record<string, string> = {
  claude: 'Claude',
  gemini: 'Gemini',
  qwen: 'Qwen',
  codex: 'Codex',
  droid: 'Droid',
  pipeline: 'Pipeline',
  cron: 'Cron',
  claude_code: 'Claude Code',
  claude_sdk: 'Claude SDK',
  claude_sdk_web_chat: 'Claude SDK Web Chat',
  cursor: 'Cursor',
  windsurf: 'Windsurf',
  copilot: 'Copilot',
}

export const PROVIDER_COLORS: Record<string, string> = {
  inherit: '#9ca3af',
  claude: SOURCE_COLORS.claude,
  gemini: SOURCE_COLORS.gemini,
  qwen: SOURCE_COLORS.qwen,
  codex: SOURCE_COLORS.codex,
  droid: SOURCE_COLORS.droid,
  cursor: '#3b82f6',
  windsurf: '#14b8a6',
  copilot: '#f97316',
  unknown: SOURCE_COLORS.unknown,
}
