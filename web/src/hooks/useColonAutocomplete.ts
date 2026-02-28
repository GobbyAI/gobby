import { useState, useCallback, useMemo } from 'react'
import { COMMANDS } from './useSlashCommands'
import type { GobbySkill } from './useSkills'
import type { McpServer, McpTool, McpToolSchema } from './useMcp'

// --- Types ---

export interface PaletteCommand {
  kind: 'command'
  name: string
  description: string
  action: string
}

export interface PaletteSubItem {
  kind: 'sub_item'
  parentCommand: string
  name: string
  description: string
  serverName?: string
}

export type PaletteItem = PaletteCommand | PaletteSubItem

export interface ColonCommandParsed {
  command: 'skills' | 'gobby' | 'mcp'
  subItem: string
  intent: string
  serverName?: string
}

// --- Internal helpers ---

type ColonParent = 'skills' | 'gobby' | 'mcp'
const COLON_PARENTS = new Set<string>(['skills', 'gobby', 'mcp'])

interface ToolEntry {
  name: string
  brief: string
  serverName: string
  transport: string
}

function buildToolIndex(
  servers: McpServer[],
  toolsByServer: Record<string, McpTool[]>,
): ToolEntry[] {
  const entries: ToolEntry[] = []
  const serverTransport = new Map<string, string>()
  for (const s of servers) {
    serverTransport.set(s.name, s.transport)
  }
  for (const [serverName, tools] of Object.entries(toolsByServer)) {
    const transport = serverTransport.get(serverName) ?? 'unknown'
    for (const t of tools) {
      entries.push({
        name: t.name,
        brief: t.brief,
        serverName,
        transport,
      })
    }
  }
  return entries
}

// --- Hook ---

export function useColonAutocomplete(
  skills: GobbySkill[],
  servers: McpServer[],
  toolsByServer: Record<string, McpTool[]>,
  fetchToolSchema: (serverName: string, toolName: string) => Promise<McpToolSchema | null>,
) {
  const [paletteItems, setPaletteItems] = useState<PaletteItem[]>([])

  const toolIndex = useMemo(
    () => buildToolIndex(servers, toolsByServer),
    [servers, toolsByServer],
  )

  const filterInput = useCallback(
    (value: string) => {
      if (!value.startsWith('/')) {
        // Only update if not already empty — avoids re-render on every keystroke
        setPaletteItems((prev) => prev.length === 0 ? prev : [])
        return
      }

      const commandPart = value.slice(1).split(/\s/)[0]
      const colonIdx = commandPart.indexOf(':')

      if (colonIdx !== -1) {
        const prefix = commandPart.slice(0, colonIdx)
        if (!COLON_PARENTS.has(prefix)) {
          setPaletteItems([])
          return
        }

        const parent = prefix as ColonParent
        const afterColon = commandPart.slice(colonIdx + 1)

        // If there's a space after the completed item, hide palette
        const fullAfterSlash = value.slice(1)
        const spaceAfterCommand = fullAfterSlash.indexOf(' ')
        if (spaceAfterCommand !== -1 && spaceAfterCommand > 0) {
          setPaletteItems([])
          return
        }

        const query = afterColon.toLowerCase()
        let items: PaletteSubItem[] = []

        if (parent === 'skills') {
          items = skills
            .filter((s) => s.enabled && !s.deleted_at && s.name.toLowerCase().includes(query))
            .slice(0, 20)
            .map((s) => ({
              kind: 'sub_item' as const,
              parentCommand: 'skills',
              name: s.name,
              description: s.description,
            }))
        } else if (parent === 'gobby') {
          items = toolIndex
            .filter((t) => t.transport === 'internal' && t.name.toLowerCase().includes(query))
            .slice(0, 20)
            .map((t) => ({
              kind: 'sub_item' as const,
              parentCommand: 'gobby',
              name: t.name,
              description: t.brief,
              serverName: t.serverName,
            }))
        } else if (parent === 'mcp') {
          items = toolIndex
            .filter((t) => t.transport !== 'internal' && t.name.toLowerCase().includes(query))
            .slice(0, 20)
            .map((t) => ({
              kind: 'sub_item' as const,
              parentCommand: 'mcp',
              name: t.name,
              description: t.brief,
              serverName: t.serverName,
            }))
        }

        setPaletteItems(items)
      } else {
        // No colon — filter top-level commands (existing behavior)
        const search = commandPart.toLowerCase()
        if (!search) {
          setPaletteItems(
            COMMANDS.map((c) => ({ kind: 'command' as const, ...c })),
          )
        } else {
          setPaletteItems(
            COMMANDS.filter((c) => c.name.includes(search)).map((c) => ({
              kind: 'command' as const,
              ...c,
            })),
          )
        }
      }
    },
    [skills, toolIndex],
  )

  const parseColonCommand = useCallback(
    (input: string): ColonCommandParsed | null => {
      if (!input.startsWith('/')) return null
      const afterSlash = input.slice(1)
      const colonIdx = afterSlash.indexOf(':')
      if (colonIdx === -1) return null

      const prefix = afterSlash.slice(0, colonIdx)
      if (!COLON_PARENTS.has(prefix)) return null

      const command = prefix as ColonParent
      const afterColon = afterSlash.slice(colonIdx + 1)
      const spaceIdx = afterColon.indexOf(' ')

      let subItem: string
      let intent: string
      if (spaceIdx === -1) {
        subItem = afterColon
        intent = ''
      } else {
        subItem = afterColon.slice(0, spaceIdx)
        intent = afterColon.slice(spaceIdx + 1)
      }

      if (!subItem) return null

      // Resolve serverName for tool commands
      let serverName: string | undefined
      if (command === 'gobby' || command === 'mcp') {
        const isInternal = command === 'gobby'
        const match = toolIndex.find(
          (t) =>
            t.name === subItem &&
            (isInternal ? t.transport === 'internal' : t.transport !== 'internal'),
        )
        serverName = match?.serverName
      }

      return { command, subItem, intent, serverName }
    },
    [toolIndex],
  )

  const resolveInjectContext = useCallback(
    async (parsed: ColonCommandParsed): Promise<string | null> => {
      try {
        if (parsed.command === 'skills') {
          const skill = skills.find(
            (s) => s.name === parsed.subItem && s.enabled && !s.deleted_at,
          )
          if (skill) return skill.content
          console.warn(`Skill "${parsed.subItem}" not found`)
          return null
        }

        // gobby or mcp — fetch tool schema
        if (!parsed.serverName) {
          console.warn(`No server found for tool "${parsed.subItem}"`)
          return null
        }

        const schema = await fetchToolSchema(parsed.serverName, parsed.subItem)
        if (!schema) {
          console.warn(`Schema fetch failed for ${parsed.serverName}.${parsed.subItem}`)
          return null
        }

        return JSON.stringify({
          tool_hint: {
            server: parsed.serverName,
            tool: parsed.subItem,
            description: schema.description ?? '',
            inputSchema: schema.inputSchema,
          },
          instruction: `The user wants you to use the MCP tool "${parsed.serverName}.${parsed.subItem}". The schema is provided above. Construct and execute the appropriate tool call based on the user's intent.`,
        })
      } catch (e) {
        console.warn('resolveInjectContext failed:', e)
        return null
      }
    },
    [skills, fetchToolSchema],
  )

  return {
    paletteItems,
    filterInput,
    parseColonCommand,
    resolveInjectContext,
  }
}
