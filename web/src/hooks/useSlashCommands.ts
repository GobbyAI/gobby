import { useState, useCallback } from 'react'

export interface CommandInfo {
  name: string
  description: string
  action: string
}

export const COMMANDS: CommandInfo[] = [
  { name: 'act', description: 'Switch from plan mode to act mode', action: 'exit_plan_mode' },
  { name: 'clear', description: 'Clear chat history', action: 'clear_history' },
  { name: 'compact', description: 'Compact conversation history', action: 'compact_chat' },
  { name: 'gobby', description: 'Browse internal Gobby tools', action: 'open_gobby' },
  { name: 'mcp', description: 'Browse external MCP tools', action: 'open_mcp' },
  { name: 'plan', description: 'Enter plan mode or show plan', action: 'show_plan' },
  { name: 'restart', description: 'Restart the Gobby daemon', action: 'restart_daemon' },
  { name: 'resume', description: 'Resume a previous session', action: 'resume_session' },
  { name: 'settings', description: 'Open settings panel', action: 'open_settings' },
  { name: 'skills', description: 'Browse and run skills', action: 'open_skills' },
]

export function useSlashCommands() {
  const [filteredCommands, setFilteredCommands] = useState<CommandInfo[]>([])

  const filterCommands = useCallback((query: string) => {
    if (!query.startsWith('/')) {
      setFilteredCommands([])
      return
    }

    const search = query.slice(1).toLowerCase().split(/\s/)[0]
    if (!search) {
      setFilteredCommands(COMMANDS)
      return
    }

    setFilteredCommands(COMMANDS.filter((c) => c.name.includes(search)))
  }, [])

  return {
    commands: COMMANDS,
    filteredCommands,
    filterCommands,
  }
}
