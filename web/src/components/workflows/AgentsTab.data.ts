import { fetchProviderModelCatalog, type ProviderModelEntry } from '../../lib/providerModels'
import type { AgentDefInfo, AgentSourceFilter } from './AgentsTab.types'
import { getBaseUrl } from './AgentsTab.types'

export interface AgentFilterOptions {
  sourceFilter: AgentSourceFilter
  filterProvider: string
  searchText: string
  hideGobby?: boolean
  hideInstalled?: boolean
  tagFilter?: string | null
}

export interface BranchStatus {
  branches?: string[]
  isGitProject?: boolean
}

export async function loadAgentDefinitions(includeDeleted = false): Promise<AgentDefInfo[] | null> {
  const params = includeDeleted ? '?include_deleted=true' : ''
  const res = await fetch(`${getBaseUrl()}/api/agents/definitions${params}`)
  const data = await res.json()
  if (data.status === 'success') {
    return Array.isArray(data.definitions) ? data.definitions : []
  }
  return null
}

export async function loadBranchStatus(projectId?: string): Promise<BranchStatus> {
  const params = new URLSearchParams()
  if (projectId) params.set('project_id', projectId)
  const [branchResult, statusResult] = await Promise.allSettled([
    fetch(`${getBaseUrl()}/api/source-control/branches?${params}`),
    fetch(`${getBaseUrl()}/api/source-control/status?${params}`),
  ])

  const status: BranchStatus = {}
  if (branchResult.status === 'fulfilled' && branchResult.value.ok) {
    const data = await branchResult.value.json() as {
      branches?: { name: string; is_remote: boolean }[]
    }
    status.branches = (data.branches || [])
      .filter((branch) => !branch.is_remote)
      .map((branch) => branch.name)
  }
  if (statusResult.status === 'fulfilled' && statusResult.value.ok) {
    const data = await statusResult.value.json() as { repo_path?: string }
    status.isGitProject = !!data.repo_path
  }
  return status
}

export async function loadProviderCatalog(): Promise<ProviderModelEntry[]> {
  return fetchProviderModelCatalog()
}

export async function loadPipelineList(): Promise<{ id: string; name: string }[]> {
  const res = await fetch(`${getBaseUrl()}/api/workflows?workflow_type=pipeline`)
  if (!res.ok) return []
  const data = await res.json()
  if (!data?.workflows) return []
  return data.workflows
    .filter((workflow: { deleted_at?: string | null }) => !workflow.deleted_at)
    .map((workflow: { id: string; name: string }) => ({
      id: workflow.id,
      name: workflow.name,
    }))
}

export function getInstalledNames(definitions: AgentDefInfo[]): Set<string> {
  const names = new Set<string>()
  for (const definition of definitions) {
    if (definition.source === 'installed' && !definition.deleted_at) {
      names.add(definition.definition.name)
    }
  }
  return names
}

export function getAgentNames(definitions: AgentDefInfo[], currentName: string): string[] {
  const nameMap = new Map<string, string>()
  for (const definition of definitions) {
    if (definition.deleted_at) continue
    if (definition.source !== 'installed' && definition.source !== 'project') continue
    if (!nameMap.has(definition.definition.name) || definition.source === 'project') {
      nameMap.set(definition.definition.name, definition.source)
    }
  }
  return Array.from(nameMap.keys()).filter((name) => name !== currentName).sort()
}

export function filterAgentDefinitions(
  definitions: AgentDefInfo[],
  installedNames: Set<string>,
  options: AgentFilterOptions,
): AgentDefInfo[] {
  return definitions.filter((definition) => {
    if (options.hideGobby && definition.tags && definition.tags.includes('gobby')) return false
    if (options.sourceFilter === 'installed') {
      if (
        definition.source === 'template' ||
        definition.source === 'project' ||
        definition.deleted_at
      ) return false
    } else if (options.sourceFilter === 'project') {
      if (definition.source !== 'project' || definition.deleted_at) return false
    } else if (options.sourceFilter === 'templates') {
      if (definition.source !== 'template' || definition.deleted_at) return false
    } else if (options.sourceFilter === 'deleted') {
      if (!definition.deleted_at) return false
    }

    if (options.hideInstalled && installedNames.has(definition.definition.name)) return false
    if (
      options.filterProvider !== 'all' &&
      definition.definition.provider !== options.filterProvider
    ) return false
    if (options.tagFilter && !(definition.tags && definition.tags.includes(options.tagFilter))) {
      return false
    }
    if (options.searchText.trim()) {
      const query = options.searchText.toLowerCase()
      if (
        !definition.definition.name.toLowerCase().includes(query) &&
        !(
          definition.definition.description &&
          definition.definition.description.toLowerCase().includes(query)
        ) &&
        !(definition.definition.role && definition.definition.role.toLowerCase().includes(query)) &&
        !definition.definition.provider.toLowerCase().includes(query)
      ) return false
    }
    return true
  })
}

export function getProviders(definitions: AgentDefInfo[]): string[] {
  return [...new Set(definitions.map((definition) => definition.definition.provider))].sort()
}

export function getAllTags(definitions: AgentDefInfo[]): string[] {
  const tags = new Set<string>()
  for (const definition of definitions) {
    if (definition.tags) {
      for (const tag of definition.tags) tags.add(tag)
    }
  }
  return [...tags].sort()
}
