import type { AgentFormData } from '../agents/AgentEditForm'
import type { WorkflowStep } from '../agents/AgentStepsEditor'
import { DEFAULT_FORM, getBaseUrl, type AgentDefInfo, type RuleSelectors } from './AgentsTab.types'
import {
  buildCreateAgentBody,
  buildDuplicateAgentBody,
  buildUpdateAgentBody,
  buildYamlDefinitionBody,
  parseYamlObject,
} from './AgentsTab.payloads'

export type ToastType = 'success' | 'error'

export interface ConfirmOptions {
  title: string
  description?: string
  confirmLabel: string
  destructive?: boolean
}

interface AgentMutationContext {
  form: AgentFormData
  rules: string[]
  ruleSelectors: RuleSelectors | null
  variables: Record<string, unknown>
  skills: string[]
  steps: WorkflowStep[]
  blockedTools: string[]
  blockedMcpTools: string[]
  fetchDefinitions: (includeDeleted?: boolean) => void | Promise<void>
  showToast: (text: string, type: ToastType) => void
}

interface CreateContext extends AgentMutationContext {
  onToggleCreateForm: (show: boolean) => void
  setCreateForm: (form: AgentFormData) => void
}

interface UpdateContext extends CreateContext {
  editingId: string | null
  setEditingId: (id: string | null) => void
}

interface YamlSaveContext {
  yamlAgent: AgentDefInfo | null
  yamlContent: string
  fetchDefinitions: (includeDeleted?: boolean) => void | Promise<void>
}

interface SidebarYamlSaveContext {
  editingId: string | null
  sidebarYamlContent: string
  form: AgentFormData
  fetchDefinitions: (includeDeleted?: boolean) => void | Promise<void>
  onToggleCreateForm: (show: boolean) => void
  setEditingId: (id: string | null) => void
  setCreateForm: (form: AgentFormData) => void
  showToast: (text: string, type: ToastType) => void
}

function toPayloadState(context: AgentMutationContext) {
  return {
    form: context.form,
    rules: context.rules,
    ruleSelectors: context.ruleSelectors,
    variables: context.variables,
    skills: context.skills,
    steps: context.steps,
    blockedTools: context.blockedTools,
    blockedMcpTools: context.blockedMcpTools,
  }
}

export async function createAgentDefinition(context: CreateContext): Promise<void> {
  try {
    const body = buildCreateAgentBody(toPayloadState(context))
    const res = await fetch(`${getBaseUrl()}/api/agents/definitions`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    })
    if (res.ok) {
      context.onToggleCreateForm(false)
      context.setCreateForm({ ...DEFAULT_FORM })
      context.fetchDefinitions(true)
      context.showToast(`Agent "${context.form.name}" created`, 'success')
    } else {
      context.showToast('Failed to create agent definition', 'error')
    }
  } catch (e) {
    console.error('Failed to create agent definition:', e)
    context.showToast('Failed to create agent definition', 'error')
  }
}

export async function updateAgentDefinition(context: UpdateContext): Promise<void> {
  if (!context.editingId) return
  try {
    const body = buildUpdateAgentBody(toPayloadState(context))
    const res = await fetch(`${getBaseUrl()}/api/agents/definitions/${context.editingId}`, {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    })
    if (res.ok) {
      context.onToggleCreateForm(false)
      context.setEditingId(null)
      context.setCreateForm({ ...DEFAULT_FORM })
      context.fetchDefinitions(true)
    } else {
      context.showToast('Failed to update agent definition', 'error')
    }
  } catch (e) {
    console.error('Failed to update agent definition:', e)
    context.showToast('Failed to update agent definition', 'error')
  }
}

export async function duplicateAgentDefinition(
  item: AgentDefInfo,
  fetchDefinitions: (includeDeleted?: boolean) => void | Promise<void>,
  showToast: (text: string, type: ToastType) => void,
): Promise<void> {
  const newName = window.prompt('New agent name:', `${item.definition.name}-copy`)
  if (!newName) return
  const body = buildDuplicateAgentBody(item, newName)
  try {
    const res = await fetch(`${getBaseUrl()}/api/agents/definitions`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    })
    if (res.ok) {
      fetchDefinitions(true)
      showToast(`Agent "${newName}" duplicated`, 'success')
    } else {
      showToast('Failed to duplicate agent definition', 'error')
    }
  } catch (e) {
    console.error('Failed to duplicate agent definition:', e)
    showToast('Failed to duplicate agent definition', 'error')
  }
}

export async function deleteAgentDefinition(
  dbId: string,
  confirm: (options: ConfirmOptions) => Promise<boolean>,
  fetchDefinitions: (includeDeleted?: boolean) => void | Promise<void>,
  showToast: (text: string, type: ToastType) => void,
): Promise<void> {
  if (!await confirm({
    title: 'Delete agent definition?',
    confirmLabel: 'Delete',
    destructive: true,
  })) return
  try {
    const res = await fetch(`${getBaseUrl()}/api/agents/definitions/${dbId}`, {
      method: 'DELETE',
    })
    if (res.ok) {
      fetchDefinitions(true)
      showToast('Agent definition deleted', 'success')
    } else {
      showToast('Failed to delete agent definition', 'error')
    }
  } catch (e) {
    console.error('Failed to delete agent definition:', e)
    showToast('Failed to delete agent definition', 'error')
  }
}

export async function restoreAgentDefinition(
  dbId: string,
  fetchDefinitions: (includeDeleted?: boolean) => void | Promise<void>,
  showToast: (text: string, type: ToastType) => void,
): Promise<void> {
  try {
    const res = await fetch(`${getBaseUrl()}/api/agents/definitions/${dbId}/restore`, {
      method: 'POST',
    })
    if (res.ok) {
      fetchDefinitions(true)
      showToast('Agent definition restored', 'success')
    } else {
      showToast('Failed to restore agent definition', 'error')
    }
  } catch (e) {
    console.error('Failed to restore agent definition:', e)
    showToast('Failed to restore agent definition', 'error')
  }
}

export async function downloadAgentDefinition(name: string): Promise<void> {
  try {
    const res = await fetch(`${getBaseUrl()}/api/agents/definitions/${name}/export`)
    if (res.ok) {
      const text = await res.text()
      const blob = new Blob([text], { type: 'application/x-yaml' })
      const url = URL.createObjectURL(blob)
      const link = document.createElement('a')
      link.href = url
      link.download = `${name}.yaml`
      link.click()
      URL.revokeObjectURL(url)
    }
  } catch (e) {
    console.error('Failed to download agent:', e)
  }
}

export async function saveYamlAgentDefinition(context: YamlSaveContext): Promise<void> {
  if (!context.yamlAgent) return
  let parsed: Record<string, unknown>
  try {
    parsed = parseYamlObject(context.yamlContent)
  } catch (e) {
    throw new Error(`Invalid YAML: ${e instanceof Error ? e.message : String(e)}`)
  }
  const isDb = context.yamlAgent.source.endsWith('-db')
  if (isDb && context.yamlAgent.db_id) {
    const body = buildYamlDefinitionBody(parsed, {
      name: context.yamlAgent.definition.name,
      surfaces: context.yamlAgent.definition.surfaces,
      provider: context.yamlAgent.definition.provider,
      mode: context.yamlAgent.definition.mode,
      baseBranch: context.yamlAgent.definition.base_branch,
      timeout: context.yamlAgent.definition.timeout,
      maxTurns: context.yamlAgent.definition.max_turns,
    })
    const res = await fetch(`${getBaseUrl()}/api/agents/definitions/${context.yamlAgent.db_id}`, {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    })
    if (!res.ok) throw new Error('Failed to save agent definition')
  } else {
    const res = await fetch(
      `${getBaseUrl()}/api/agents/definitions/import/${context.yamlAgent.definition.name}`,
      { method: 'POST' },
    )
    if (!res.ok) throw new Error('Failed to import agent definition to DB')
  }
  context.fetchDefinitions(true)
}

export async function saveSidebarYamlAgentDefinition(
  context: SidebarYamlSaveContext,
): Promise<void> {
  if (!context.editingId) return
  let parsed: Record<string, unknown>
  try {
    parsed = parseYamlObject(context.sidebarYamlContent)
  } catch (e) {
    window.alert(`Invalid YAML: ${e instanceof Error ? e.message : String(e)}`)
    return
  }

  try {
    const body = buildYamlDefinitionBody(parsed, {
      name: context.form.name,
      surfaces: context.form.surfaces,
      provider: context.form.provider,
      mode: context.form.mode,
      baseBranch: context.form.base_branch,
      timeout: context.form.timeout,
      maxTurns: context.form.max_turns,
    })
    const res = await fetch(`${getBaseUrl()}/api/agents/definitions/${context.editingId}`, {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    })
    if (res.ok) {
      context.onToggleCreateForm(false)
      context.setEditingId(null)
      context.setCreateForm({ ...DEFAULT_FORM })
      context.fetchDefinitions(true)
    } else {
      context.showToast('Failed to save agent from YAML', 'error')
    }
  } catch (e) {
    console.error('Failed to save agent from YAML:', e)
    context.showToast('Failed to save agent from YAML', 'error')
  }
}

export async function installAgentFromTemplate(
  name: string,
  fetchDefinitions: (includeDeleted?: boolean) => void | Promise<void>,
  showToast: (text: string, type: ToastType) => void,
): Promise<void> {
  try {
    const res = await fetch(`${getBaseUrl()}/api/agents/definitions/${encodeURIComponent(name)}/install`, {
      method: 'POST',
    })
    if (res.ok) {
      fetchDefinitions(true)
    } else {
      const data = await res.json().catch(() => ({}))
      showToast(data.detail || 'Failed to install from template', 'error')
    }
  } catch (e) {
    console.error('Failed to install agent from template:', e)
    showToast('Failed to install from template', 'error')
  }
}

export async function moveAgentToProject(
  item: AgentDefInfo,
  projectId: string | undefined,
  confirm: (options: ConfirmOptions) => Promise<boolean>,
  fetchDefinitions: (includeDeleted?: boolean) => void | Promise<void>,
): Promise<void> {
  if (!projectId || !item.db_id) return
  if (!await confirm({
    title: 'Move to project?',
    description: `Move "${item.definition.name}" to the current project? It will no longer apply globally.`,
    confirmLabel: 'Move',
  })) return
  try {
    const res = await fetch(`${getBaseUrl()}/api/workflows/${item.db_id}/move-to-project`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ project_id: projectId }),
    })
    if (res.ok) fetchDefinitions(true)
  } catch (e) {
    console.error('Failed to move agent to project:', e)
  }
}

export async function moveAgentToGlobal(
  item: AgentDefInfo,
  confirm: (options: ConfirmOptions) => Promise<boolean>,
  fetchDefinitions: (includeDeleted?: boolean) => void | Promise<void>,
): Promise<void> {
  if (!item.db_id) return
  if (!await confirm({
    title: 'Move to global?',
    description: `Move "${item.definition.name}" to global scope? It will apply to all projects.`,
    confirmLabel: 'Move',
  })) return
  try {
    const res = await fetch(`${getBaseUrl()}/api/workflows/${item.db_id}/move-to-global`, {
      method: 'POST',
    })
    if (res.ok) fetchDefinitions(true)
  } catch (e) {
    console.error('Failed to move agent to global:', e)
  }
}

export async function restoreAgentFromTemplate(
  item: AgentDefInfo,
  confirm: (options: ConfirmOptions) => Promise<boolean>,
  fetchDefinitions: (includeDeleted?: boolean) => void | Promise<void>,
): Promise<void> {
  if (!item.db_id) return
  if (!await confirm({
    title: 'Restore from template?',
    description: `Reset "${item.definition.name}" to the bundled template version? Your customizations will be lost.`,
    confirmLabel: 'Restore',
  })) return
  try {
    const res = await fetch(`${getBaseUrl()}/api/workflows/${item.db_id}/restore-from-template`, {
      method: 'POST',
    })
    if (res.ok) fetchDefinitions(true)
  } catch (e) {
    console.error('Failed to restore agent from template:', e)
  }
}

export async function importAgentDefinition(
  name: string,
  fetchDefinitions: (includeDeleted?: boolean) => void | Promise<void>,
): Promise<boolean> {
  try {
    const res = await fetch(`${getBaseUrl()}/api/agents/definitions/import/${name}`, {
      method: 'POST',
    })
    if (res.ok) fetchDefinitions(true)
    return res.ok
  } catch (e) {
    console.error('Failed to import agent definition:', e)
    return false
  }
}
