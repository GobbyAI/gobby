import { useState, useEffect, useCallback, useMemo, useRef } from 'react'
import '../agents/agents.css'
import * as yaml from 'js-yaml'
import { useAgentRuns } from '../../hooks/useAgentRuns'
import type { RunningAgent, AgentRun } from '../../hooks/useAgentRuns'
import { useConfirmDialog } from '../../hooks/useConfirmDialog'
import { YamlEditorModal } from './WorkflowsPage'
import { AgentEditForm } from '../agents/AgentEditForm'
import type { AgentFormData } from '../agents/AgentEditForm'

// =============================================================================
// Types
// =============================================================================

interface AgentDefInfo {
  definition: {
    name: string
    description: string | null
    role: string | null
    goal: string | null
    personality: string | null
    instructions: string | null
    provider: string
    model: string | null
    mode: string
    isolation: string | null
    base_branch: string
    timeout: number
    max_turns: number
    default_workflow: string | null
    sandbox: Record<string, unknown> | null
    skill_profile: Record<string, unknown> | null
    workflows: {
      pipeline?: string
      rules?: string[]
      variables?: Record<string, unknown>
      [key: string]: unknown
    } | null
    extends: string | null
    lifecycle_variables: Record<string, unknown>
    default_variables: Record<string, unknown>
  }
  source: string
  source_path: string | null
  db_id: string | null
  enabled: boolean
  overridden_by: string | null
  deleted_at: string | null
  tags: string[] | null
}

// =============================================================================
// Constants
// =============================================================================

const SOURCE_LABELS: Record<string, string> = {
  'template': 'Template',
  'installed': 'Installed',
  'project': 'Project',
}

const PROVIDER_COLORS: Record<string, string> = {
  inherit: '#9ca3af',
  claude: '#6366f1',
  gemini: '#a855f7',
  codex: '#22c55e',
  cursor: '#3b82f6',
  windsurf: '#14b8a6',
  copilot: '#f97316',
}

const MODE_COLORS: Record<string, string> = {
  terminal: '#f59e0b',
  headless: '#6b7280',
  embedded: '#06b6d4',
  self: '#ec4899',
}

const STATUS_COLORS: Record<string, string> = {
  running: '#3b82f6',
  pending: '#f59e0b',
  success: '#10b981',
  error: '#ef4444',
  timeout: '#f97316',
  cancelled: '#6b7280',
}

const ISOLATION_COLORS: Record<string, string> = {
  clone: '#ef4444',
  worktree: '#eab308',
  none: '#6b7280',
}

const DEFAULT_FORM: AgentFormData = {
  name: '', extends: '', description: '', role: '', goal: '', personality: '', instructions: '',
  provider: 'inherit', model: '', mode: 'inherit', isolation: 'inherit',
  base_branch: 'inherit', timeout: 0, max_turns: 0, pipeline: '',
}

const PROVIDER_MODELS: Record<string, { value: string; label: string }[]> = {
  inherit: [{ value: '', label: '(default)' }],
  claude: [
    { value: '', label: '(default)' },
    { value: 'opus', label: 'Opus' },
    { value: 'sonnet', label: 'Sonnet' },
    { value: 'haiku', label: 'Haiku' },
  ],
  gemini: [{ value: '', label: '(default)' }],
  codex: [{ value: '', label: '(default)' }],
  cursor: [{ value: '', label: '(default)' }],
  windsurf: [{ value: '', label: '(default)' }],
  copilot: [{ value: '', label: '(default)' }],
}

function agentDefToYaml(d: AgentDefInfo['definition']): string {
  const obj: Record<string, unknown> = { name: d.name }
  if (d.extends) obj.extends = d.extends
  if (d.description) obj.description = d.description
  if (d.role) obj.role = d.role
  if (d.goal) obj.goal = d.goal
  if (d.personality) obj.personality = d.personality
  if (d.instructions) obj.instructions = d.instructions
  obj.provider = d.provider
  if (d.model) obj.model = d.model
  obj.mode = d.mode
  if (d.isolation) obj.isolation = d.isolation
  obj.base_branch = d.base_branch
  obj.timeout = d.timeout
  obj.max_turns = d.max_turns
  if (d.workflows) obj.workflows = d.workflows
  if (d.sandbox) obj.sandbox = d.sandbox
  return yaml.dump(obj, { lineWidth: 120, noRefs: true })
}

function getBaseUrl(): string {
  return ''
}

// =============================================================================
// Component
// =============================================================================

interface AgentsTabProps {
  searchText: string
  sourceFilter: 'installed' | 'project' | 'templates' | 'deleted'
  devMode: boolean
  showCreateForm: boolean
  onToggleCreateForm: (show: boolean) => void
  refreshKey?: number
  projectId?: string
  hideGobby?: boolean
  hideInstalled?: boolean
  filterProvider: string
  onProvidersChange: (providers: string[]) => void
  tagFilter?: string | null
  onTagsChange?: (tags: string[]) => void
}

export function AgentsTab({ searchText, sourceFilter, devMode, showCreateForm, onToggleCreateForm, refreshKey = 0, projectId, hideGobby, hideInstalled, filterProvider, onProvidersChange, tagFilter, onTagsChange }: AgentsTabProps) {
  const { confirm, ConfirmDialogElement } = useConfirmDialog()
  const { running, recentRuns, cancelAgent } = useAgentRuns()
  const [definitions, setDefinitions] = useState<AgentDefInfo[]>([])
  const [loading, setLoading] = useState(true)
  const [showRecentRuns, setShowRecentRuns] = useState(false)
  const [selectedAgent, setSelectedAgent] = useState<AgentDefInfo | null>(null)
  const [editingId, setEditingId] = useState<string | null>(null)
  const [importingName, setImportingName] = useState<string | null>(null)
  const [importResult, setImportResult] = useState<{ name: string; ok: boolean } | null>(null)
  const [toastMessage, setToastMessage] = useState<{ text: string; type: 'success' | 'error' } | null>(null)

  // YAML editor state
  const [yamlAgent, setYamlAgent] = useState<AgentDefInfo | null>(null)
  const [yamlContent, setYamlContent] = useState('')
  const [yamlLoading] = useState(false)

  const showToast = useCallback((text: string, type: 'success' | 'error') => {
    setToastMessage({ text, type })
    setTimeout(() => setToastMessage(null), 4000)
  }, [])

  const [createForm, setCreateForm] = useState<AgentFormData>({ ...DEFAULT_FORM })

  // Branch / git state for the edit form
  const [branches, setBranches] = useState<string[]>([])
  const [isGitProject, setIsGitProject] = useState(true)
  const [editRules, setEditRules] = useState<string[]>([])
  const [editRuleSelectors, setEditRuleSelectors] = useState<{ include: string[]; exclude: string[] } | null>(null)
  const [editVariables, setEditVariables] = useState<Record<string, unknown>>({})
  const [editSkills, setEditSkills] = useState<string[]>([])

  // Sidebar view state (form vs YAML)
  const [sidebarView, setSidebarView] = useState<'form' | 'yaml'>('form')
  const [sidebarYamlContent, setSidebarYamlContent] = useState('')

  // Pipeline list for selector
  const [pipelineList, setPipelineList] = useState<{ id: string; name: string }[]>([])

  const fetchDefinitions = useCallback(async (includeDeleted = false) => {
    setLoading(true)
    try {
      const params = includeDeleted ? '?include_deleted=true' : ''
      const res = await fetch(`${getBaseUrl()}/api/agents/definitions${params}`)
      const data = await res.json()
      if (data.status === 'success') {
        setDefinitions(data.definitions)
      }
    } catch (e) {
      console.error('Failed to fetch agent definitions:', e)
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => { fetchDefinitions(true) }, [fetchDefinitions])

  // Re-fetch when refreshKey changes (skip initial render)
  const initialRef = useRef(true)
  useEffect(() => {
    if (initialRef.current) {
      initialRef.current = false
      return
    }
    fetchDefinitions(true)
  }, [refreshKey, fetchDefinitions])

  // Clear editing state when form closes; reset form when opening for create
  useEffect(() => {
    if (!showCreateForm) {
      setEditingId(null)
      setSelectedAgent(null)
      setSidebarView('form')
    } else if (!editingId) {
      setCreateForm({ ...DEFAULT_FORM })
      setEditRules([])
      setEditRuleSelectors(null)
      setEditVariables({})
      setEditSkills([])
      setSelectedAgent(null)
      setSidebarView('form')
      setSidebarYamlContent(yaml.dump({
        name: '', provider: 'inherit', mode: 'inherit',
        base_branch: 'inherit', timeout: 0, max_turns: 0,
      }, { lineWidth: 120, noRefs: true }))
    }
  }, [showCreateForm, editingId])

  // Fetch git branches and project status
  useEffect(() => {
    const params = new URLSearchParams()
    if (projectId) params.set('project_id', projectId)
    Promise.allSettled([
      fetch(`${getBaseUrl()}/api/source-control/branches?${params}`),
      fetch(`${getBaseUrl()}/api/source-control/status?${params}`),
    ]).then(([brRes, statusRes]) => {
      if (brRes.status === 'fulfilled' && brRes.value.ok) {
        brRes.value.json().then((data: { branches?: { name: string; is_remote: boolean }[] }) => {
          setBranches((data.branches || []).filter(b => !b.is_remote).map(b => b.name))
        })
      }
      if (statusRes.status === 'fulfilled' && statusRes.value.ok) {
        statusRes.value.json().then((data: { repo_path?: string }) => {
          setIsGitProject(!!data.repo_path)
        })
      }
    })
  }, [projectId])

  const [providerModels, setProviderModels] = useState(PROVIDER_MODELS)

  useEffect(() => {
    fetch(`${getBaseUrl()}/admin/models`)
      .then(res => res.ok ? res.json() : null)
      .then(data => {
        if (data?.models) setProviderModels(prev => ({ ...prev, ...data.models }))
      })
      .catch(e => console.error('Failed to fetch model list:', e))
  }, [])

  // Fetch pipeline list for selector
  useEffect(() => {
    fetch(`${getBaseUrl()}/api/workflows?workflow_type=pipeline`)
      .then(r => r.ok ? r.json() : null)
      .then(data => {
        if (data?.workflows) {
          setPipelineList(data.workflows
            .filter((w: { deleted_at?: string | null }) => !w.deleted_at)
            .map((w: { id: string; name: string }) => ({ id: w.id, name: w.name }))
          )
        }
      })
      .catch(() => setPipelineList([]))
  }, [])

  const installedNames = useMemo(() => {
    const names = new Set<string>()
    for (const d of definitions) {
      if (d.source === 'installed' && !d.deleted_at) {
        names.add(d.definition.name)
      }
    }
    return names
  }, [definitions])

  const filtered = useMemo(() => definitions.filter(d => {
    // Hide gobby-tagged items
    if (hideGobby && d.tags && d.tags.includes('gobby')) return false
    // Source filter (exclusive)
    if (sourceFilter === 'installed') {
      if (d.source === 'template' || d.source === 'project' || d.deleted_at) return false
    } else if (sourceFilter === 'project') {
      if (d.source !== 'project' || d.deleted_at) return false
    } else if (sourceFilter === 'templates') {
      if (d.source !== 'template' || d.deleted_at) return false
    } else if (sourceFilter === 'deleted') {
      if (!d.deleted_at) return false
    }

    if (hideInstalled && installedNames.has(d.definition.name)) return false
    if (filterProvider !== 'all' && d.definition.provider !== filterProvider) return false
    if (tagFilter && !(d.tags && d.tags.includes(tagFilter))) return false
    if (searchText.trim()) {
      const q = searchText.toLowerCase()
      if (
        !d.definition.name.toLowerCase().includes(q) &&
        !(d.definition.description && d.definition.description.toLowerCase().includes(q)) &&
        !(d.definition.role && d.definition.role.toLowerCase().includes(q)) &&
        !d.definition.provider.toLowerCase().includes(q)
      ) return false
    }
    return true
  }), [definitions, installedNames, sourceFilter, filterProvider, searchText, hideGobby, hideInstalled, tagFilter])

  const providers = useMemo(
    () => [...new Set(definitions.map(d => d.definition.provider))].sort(),
    [definitions]
  )

  useEffect(() => {
    onProvidersChange(providers)
  }, [providers, onProvidersChange])

  const allTags = useMemo(() => {
    const tags = new Set<string>()
    for (const d of definitions) {
      if (d.tags) for (const t of d.tags) tags.add(t)
    }
    return [...tags].sort()
  }, [definitions])

  useEffect(() => {
    onTagsChange?.(allTags)
  }, [allTags, onTagsChange])

  const handleCreate = async () => {
    try {
      const body: Record<string, unknown> = {
        name: createForm.name,
        provider: createForm.provider,
        mode: createForm.mode,
        isolation: createForm.isolation,
        base_branch: createForm.base_branch,
        timeout: createForm.timeout,
        max_turns: createForm.max_turns,
      }
      if (createForm.description) body.description = createForm.description
      if (createForm.role) body.role = createForm.role
      if (createForm.goal) body.goal = createForm.goal
      if (createForm.personality) body.personality = createForm.personality
      if (createForm.instructions) body.instructions = createForm.instructions
      if (createForm.model) body.model = createForm.model
      if (createForm.extends) body.extends = createForm.extends
      // Nest rules, rule_selectors, variables, pipeline under workflows
      const workflows: Record<string, unknown> = {}
      if (editRules.length > 0) workflows.rules = editRules
      if (editRuleSelectors) workflows.rule_selectors = editRuleSelectors
      if (Object.keys(editVariables).length > 0) workflows.variables = editVariables
      if (createForm.pipeline) workflows.pipeline = createForm.pipeline
      if (editSkills.length > 0) {
        workflows.skill_selectors = { include: editSkills }
      }
      if (Object.keys(workflows).length > 0) body.workflows = workflows

      const res = await fetch(`${getBaseUrl()}/api/agents/definitions`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
      })
      if (res.ok) {
        onToggleCreateForm(false)
        setCreateForm({ ...DEFAULT_FORM })
        fetchDefinitions(true)
        showToast(`Agent "${createForm.name}" created`, 'success')
      } else {
        showToast('Failed to create agent definition', 'error')
      }
    } catch (e) {
      console.error('Failed to create agent definition:', e)
      showToast('Failed to create agent definition', 'error')
    }
  }

  const handleEdit = (item: AgentDefInfo) => {
    const d = item.definition
    setCreateForm({
      name: d.name,
      extends: d.extends || '',
      description: d.description || '',
      role: d.role || '',
      goal: d.goal || '',
      personality: d.personality || '',
      instructions: d.instructions || '',
      provider: d.provider,
      model: d.model || '',
      mode: d.mode,
      isolation: d.isolation || 'inherit',
      base_branch: d.base_branch,
      timeout: d.timeout,
      max_turns: d.max_turns,
      pipeline: (d.workflows?.pipeline as string) || '',
    })
    setEditingId(item.db_id)
    setEditRules((d.workflows?.rules as string[]) || [])
    const rs = d.workflows?.rule_selectors as { include: string[]; exclude: string[] } | undefined
    setEditRuleSelectors(rs || null)
    setEditVariables((d.workflows?.variables as Record<string, unknown>) || {})
    // Load from skill_selectors.include (preferred) or legacy skill_profile
    const skillSelectors = d.workflows?.skill_selectors as { include?: string[]; exclude?: string[] } | undefined
    if (skillSelectors?.include && skillSelectors.include.length > 0) {
      setEditSkills(skillSelectors.include.filter(s => s !== '*'))
    } else if (d.skill_profile) {
      setEditSkills(Object.keys(d.skill_profile))
    } else {
      setEditSkills([])
    }
    setSelectedAgent(null)
    setSidebarView('form')
    setSidebarYamlContent(agentDefToYaml(d))
    onToggleCreateForm(true)
  }

  const handleUpdate = async () => {
    if (!editingId) return
    try {
      const body: Record<string, unknown> = {
        name: createForm.name,
        extends: createForm.extends || null,
        description: createForm.description || null,
        role: createForm.role || null,
        goal: createForm.goal || null,
        personality: createForm.personality || null,
        instructions: createForm.instructions || null,
        provider: createForm.provider,
        model: createForm.model || null,
        mode: createForm.mode,
        isolation: createForm.isolation,
        base_branch: createForm.base_branch,
        timeout: createForm.timeout,
        max_turns: createForm.max_turns,
      }
      // Include full workflows state
      const workflows: Record<string, unknown> = {}
      if (createForm.pipeline) workflows.pipeline = createForm.pipeline
      if (editRules.length > 0) workflows.rules = editRules
      if (editRuleSelectors) workflows.rule_selectors = editRuleSelectors
      if (Object.keys(editVariables).length > 0) workflows.variables = editVariables
      if (editSkills.length > 0) {
        workflows.skill_selectors = { include: editSkills }
      }
      if (Object.keys(workflows).length > 0) body.workflows = workflows

      const res = await fetch(`${getBaseUrl()}/api/agents/definitions/${editingId}`, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
      })
      if (res.ok) {
        onToggleCreateForm(false)
        setEditingId(null)
        setCreateForm({ ...DEFAULT_FORM })
        fetchDefinitions(true)
        // silent success — panel closes, grid refreshes
      } else {
        showToast('Failed to update agent definition', 'error')
      }
    } catch (e) {
      console.error('Failed to update agent definition:', e)
      showToast('Failed to update agent definition', 'error')
    }
  }

  const handleEditRulesChange = useCallback((newRules: string[]) => {
    setEditRules(newRules)
    if (editingId) {
      setDefinitions(prev => prev.map(def =>
        def.db_id === editingId
          ? { ...def, definition: { ...def.definition, workflows: { ...def.definition.workflows, rules: newRules } } }
          : def
      ))
    }
  }, [editingId])

  const handleEditRuleSelectorsChange = useCallback((newSelectors: { include: string[]; exclude: string[] }) => {
    setEditRuleSelectors(newSelectors)
    if (editingId) {
      setDefinitions(prev => prev.map(def =>
        def.db_id === editingId
          ? { ...def, definition: { ...def.definition, workflows: { ...def.definition.workflows, rule_selectors: newSelectors } } }
          : def
      ))
    }
  }, [editingId])

  const handleEditVariablesChange = useCallback((newVars: Record<string, unknown>) => {
    setEditVariables(newVars)
    if (editingId) {
      setDefinitions(prev => prev.map(def =>
        def.db_id === editingId
          ? { ...def, definition: { ...def.definition, workflows: { ...def.definition.workflows, variables: newVars } } }
          : def
      ))
    }
  }, [editingId])

  const handleDuplicate = useCallback(async (item: AgentDefInfo) => {
    const newName = window.prompt('New agent name:', `${item.definition.name}-copy`)
    if (!newName) return
    const d = item.definition
    const body: Record<string, unknown> = {
      name: newName,
      provider: d.provider,
      mode: d.mode,
      base_branch: d.base_branch,
      timeout: d.timeout,
      max_turns: d.max_turns,
    }
    if (d.description) body.description = d.description
    if (d.role) body.role = d.role
    if (d.goal) body.goal = d.goal
    if (d.personality) body.personality = d.personality
    if (d.instructions) body.instructions = d.instructions
    if (d.model) body.model = d.model
    if (d.isolation) body.isolation = d.isolation
    if (d.workflows) body.workflows = d.workflows
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
  }, [fetchDefinitions, showToast])

  const handleDelete = async (dbId: string) => {
    if (!await confirm({ title: 'Delete agent definition?', confirmLabel: 'Delete', destructive: true })) return
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

  const handleRestore = async (dbId: string) => {
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

  const handleDownload = useCallback(async (name: string) => {
    try {
      const res = await fetch(`${getBaseUrl()}/api/agents/definitions/${name}/export`)
      if (res.ok) {
        const text = await res.text()
        const blob = new Blob([text], { type: 'application/x-yaml' })
        const url = URL.createObjectURL(blob)
        const a = document.createElement('a')
        a.href = url
        a.download = `${name}.yaml`
        a.click()
        URL.revokeObjectURL(url)
      }
    } catch (e) {
      console.error('Failed to download agent:', e)
    }
  }, [])

  const handleYamlSave = useCallback(async () => {
    if (!yamlAgent) return
    let parsed: Record<string, unknown>
    try {
      parsed = yaml.load(yamlContent, { schema: yaml.JSON_SCHEMA }) as Record<string, unknown>
    } catch (e) {
      throw new Error(`Invalid YAML: ${e instanceof Error ? e.message : String(e)}`)
    }
    if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) {
      throw new Error('Invalid YAML: expected an object')
    }
    const isDb = yamlAgent.source.endsWith('-db')
    if (isDb && yamlAgent.db_id) {
      const body: Record<string, unknown> = {
        name: (parsed.name as string) || yamlAgent.definition.name,
        description: parsed.description ?? null,
        extends: parsed.extends ?? null,
        sources: parsed.sources ?? null,
        role: parsed.role ?? null,
        goal: parsed.goal ?? null,
        personality: parsed.personality ?? null,
        instructions: parsed.instructions ?? null,
        provider: parsed.provider || yamlAgent.definition.provider,
        model: parsed.model ?? null,
        mode: parsed.mode || yamlAgent.definition.mode,
        isolation: parsed.isolation ?? null,
        base_branch: (parsed.base_branch as string) || yamlAgent.definition.base_branch,
        timeout: parsed.timeout ?? yamlAgent.definition.timeout,
        max_turns: parsed.max_turns ?? yamlAgent.definition.max_turns,
      }
      if (parsed.workflows) body.workflows = parsed.workflows
      const res = await fetch(`${getBaseUrl()}/api/agents/definitions/${yamlAgent.db_id}`, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
      })
      if (!res.ok) throw new Error('Failed to save agent definition')
    } else {
      // For file-based agents, import to DB
      const res = await fetch(`${getBaseUrl()}/api/agents/definitions/import/${yamlAgent.definition.name}`, {
        method: 'POST',
      })
      if (!res.ok) throw new Error('Failed to import agent definition to DB')
    }
    setYamlAgent(null)
    fetchDefinitions(true)
  }, [yamlAgent, yamlContent, fetchDefinitions])

  // YAML save from sidebar
  const handleSidebarYamlSave = useCallback(async () => {
    if (!editingId) return
    let parsed: Record<string, unknown>
    try {
      parsed = yaml.load(sidebarYamlContent, { schema: yaml.JSON_SCHEMA }) as Record<string, unknown>
    } catch (e) {
      window.alert(`Invalid YAML: ${e instanceof Error ? e.message : String(e)}`)
      return
    }
    if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) {
      window.alert('Invalid YAML: expected an object')
      return
    }
    try {
      const body: Record<string, unknown> = {
        name: (parsed.name as string) || createForm.name,
        description: parsed.description ?? null,
        extends: parsed.extends ?? null,
        sources: parsed.sources ?? null,
        role: parsed.role ?? null,
        goal: parsed.goal ?? null,
        personality: parsed.personality ?? null,
        instructions: parsed.instructions ?? null,
        provider: parsed.provider || createForm.provider,
        model: parsed.model ?? null,
        mode: parsed.mode || createForm.mode,
        isolation: parsed.isolation ?? null,
        base_branch: (parsed.base_branch as string) || createForm.base_branch,
        timeout: parsed.timeout ?? createForm.timeout,
        max_turns: parsed.max_turns ?? createForm.max_turns,
      }
      if (parsed.workflows) body.workflows = parsed.workflows
      const res = await fetch(`${getBaseUrl()}/api/agents/definitions/${editingId}`, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
      })
      if (res.ok) {
        onToggleCreateForm(false)
        setEditingId(null)
        setCreateForm({ ...DEFAULT_FORM })
        fetchDefinitions(true)
      } else {
        showToast('Failed to save agent from YAML', 'error')
      }
    } catch (e) {
      console.error('Failed to save agent from YAML:', e)
      showToast('Failed to save agent from YAML', 'error')
    }
  }, [editingId, sidebarYamlContent, createForm, fetchDefinitions, onToggleCreateForm, showToast])

  const handleInstallFromTemplate = async (name: string) => {
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

  const handleMoveToProject = useCallback(async (item: AgentDefInfo) => {
    if (!projectId || !item.db_id) return
    if (!await confirm({ title: 'Move to project?', description: `Move "${item.definition.name}" to the current project? It will no longer apply globally.`, confirmLabel: 'Move' })) return
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
  }, [projectId, fetchDefinitions])

  const handleMoveToGlobal = useCallback(async (item: AgentDefInfo) => {
    if (!item.db_id) return
    if (!await confirm({ title: 'Move to global?', description: `Move "${item.definition.name}" to global scope? It will apply to all projects.`, confirmLabel: 'Move' })) return
    try {
      const res = await fetch(`${getBaseUrl()}/api/workflows/${item.db_id}/move-to-global`, {
        method: 'POST',
      })
      if (res.ok) fetchDefinitions(true)
    } catch (e) {
      console.error('Failed to move agent to global:', e)
    }
  }, [fetchDefinitions])

  const handleImport = async (name: string) => {
    setImportingName(name)
    setImportResult(null)
    try {
      const res = await fetch(`${getBaseUrl()}/api/agents/definitions/import/${name}`, {
        method: 'POST',
      })
      setImportResult({ name, ok: res.ok })
      if (res.ok) fetchDefinitions(true)
    } catch (e) {
      console.error('Failed to import agent definition:', e)
      setImportResult({ name, ok: false })
    } finally {
      setImportingName(null)
      setTimeout(() => setImportResult(null), 3000)
    }
  }

  return (
    <div className="agent-defs-tab">
      {ConfirmDialogElement}
      {toastMessage && (
        <div
          className={`agent-defs-toast ${toastMessage.type === 'success' ? 'agent-defs-toast--success' : ''}`}
          onClick={() => setToastMessage(null)}
        >
          {toastMessage.text}
        </div>
      )}


      {/* Running agents */}
      {(running.length > 0 || recentRuns.length > 0) && (
        <div className="agent-runs-section">
          <div className="agent-runs-header">
            <h3 className="agent-runs-title">
              Running Agents
              {running.length > 0 && (
                <span className="agent-runs-count agent-runs-count--active">{running.length}</span>
              )}
            </h3>
            {recentRuns.length > 0 && (
              <button
                className="agent-defs-btn"
                onClick={() => setShowRecentRuns(!showRecentRuns)}
              >
                {showRecentRuns ? 'Hide history' : `History (${recentRuns.length})`}
              </button>
            )}
          </div>

          {running.length > 0 ? (
            <div className="agent-runs-list">
              {running.map(agent => (
                <RunningAgentCard key={agent.run_id} agent={agent} onCancel={cancelAgent} />
              ))}
            </div>
          ) : (
            <div className="agent-runs-empty">No agents currently running</div>
          )}

          {showRecentRuns && recentRuns.length > 0 && (
            <div className="agent-runs-history">
              <table className="agent-runs-table">
                <thead>
                  <tr>
                    <th>Status</th>
                    <th>Provider</th>
                    <th>Prompt</th>
                    <th>Turns</th>
                    <th>Duration</th>
                    <th>Started</th>
                  </tr>
                </thead>
                <tbody>
                  {recentRuns.map(run => (
                    <AgentRunRow key={run.id} run={run} />
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </div>
      )}

      {/* Card grid */}
      <div className="workflows-content">
        {loading ? (
          <div className="workflows-loading">Loading agent definitions...</div>
        ) : filtered.length === 0 ? (
          <div className="workflows-empty">No agent definitions found</div>
        ) : (
          <div className="workflows-grid">
            {filtered.map(item => {
              const d = item.definition
              const isDb = !!item.db_id
              const isTemplate = item.source === 'template'
              const wfMeta = ['rules', 'variables', 'pipeline']
              const workflowCount = d.workflows
                ? Object.entries(d.workflows).filter(([k]) => !wfMeta.includes(k) && typeof d.workflows![k] === 'object' && d.workflows![k] !== null && !Array.isArray(d.workflows![k])).length
                : 0

              return (
                <div
                  key={d.name}
                  className={`agent-def-card${item.deleted_at ? ' agent-def-card--deleted' : ''}${isTemplate ? ' workflows-card--template' : ''}`}
                >
                  {/* Card header */}
                  <button
                    className="agent-def-header"
                    onClick={() => {
                      if (item.deleted_at) return
                      if (isDb) {
                        handleEdit(item)
                      } else {
                        setSelectedAgent(item)
                        setSidebarYamlContent(agentDefToYaml(item.definition))
                      }
                    }}
                  >
                    <div className="agent-def-header-top">
                      <span className={`agent-def-name${item.deleted_at ? ' agent-def-name--deleted' : ''}`}>{d.name}</span>
                      <span className="workflows-card-type workflows-card-type--agent">agent</span>
                    </div>
                    {d.description && (
                      <div className="agent-def-desc">
                        {d.description.split('\n')[0].slice(0, 100)}
                      </div>
                    )}
                    <div className="agent-def-badges">
                      <span className="workflows-card-badge workflows-card-badge--source">
                        {SOURCE_LABELS[item.source] || item.source}
                      </span>
                      <span
                        className="agent-def-badge agent-def-badge--filled"
                        style={{ background: PROVIDER_COLORS[d.provider] || '#666' }}
                      >
                        {d.provider}
                      </span>
                      <span
                        className="agent-def-badge agent-def-badge--filled"
                        style={{ background: MODE_COLORS[d.mode] || '#666' }}
                      >
                        {d.mode}
                      </span>
                      {d.isolation && (
                        <span
                          className="agent-def-badge agent-def-badge--filled"
                          style={{ background: ISOLATION_COLORS[d.isolation] || '#666' }}
                        >
                          {d.isolation}
                        </span>
                      )}
                      {workflowCount > 0 && (
                        <span className="agent-def-badge agent-def-badge--dim">
                          {workflowCount} workflow{workflowCount !== 1 ? 's' : ''}
                        </span>
                      )}
                      <span className="agent-def-badge agent-def-badge--dim">
                        {d.timeout}s
                      </span>
                    </div>
                  </button>

                  {/* Card footer - always visible */}
                  <div className="workflows-card-footer">
                    {item.deleted_at ? (
                      <div className="workflows-card-actions">
                        {item.db_id && (
                          <button
                            type="button"
                            className="workflows-action-btn workflows-action-btn--restore"
                            onClick={() => handleRestore(item.db_id!)}
                            title="Restore this agent"
                          >
                            Restore
                          </button>
                        )}
                      </div>
                    ) : isTemplate ? (
                      <>
                        <div />
                        <div className="workflows-card-actions">
                          {devMode ? (
                            <>
                              {installedNames.has(d.name)
                                ? <button type="button" className="workflows-action-btn" disabled title="Already installed">Installed</button>
                                : <button type="button" className="workflows-action-btn" onClick={() => handleInstallFromTemplate(d.name)} title="Create an installed copy">Install</button>}
                              <button type="button" className="workflows-action-icon" onClick={() => handleDuplicate(item)} title="Duplicate" aria-label="Duplicate agent">
                                <svg width="14" height="14" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round"><rect x="5.5" y="5.5" width="9" height="9" rx="1.5" /><path d="M10.5 5.5V2.5a1 1 0 0 0-1-1h-7a1 1 0 0 0-1 1v7a1 1 0 0 0 1 1h3" /></svg>
                              </button>
                              <button type="button" className="workflows-action-icon" onClick={() => handleDownload(d.name)} title="Download YAML" aria-label="Download agent as YAML">
                                <svg width="14" height="14" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round"><path d="M8 2v9m0 0L5 8m3 3 3-3M2.5 12.5v1a1 1 0 0 0 1 1h9a1 1 0 0 0 1-1v-1" /></svg>
                              </button>
                              {item.db_id && (
                                <button type="button" className="workflows-action-icon workflows-action-icon--danger" onClick={() => handleDelete(item.db_id!)} title="Delete" aria-label="Delete agent">
                                  <svg width="14" height="14" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round"><path d="M2.5 4.5h11M5.5 4.5V3a1 1 0 0 1 1-1h3a1 1 0 0 1 1 1v1.5M6.5 7v4.5M9.5 7v4.5" /><path d="M3.5 4.5 4 13a1 1 0 0 0 1 1h6a1 1 0 0 0 1-1l.5-8.5" /></svg>
                                </button>
                              )}
                            </>
                          ) : (
                            <>
                              {installedNames.has(d.name)
                                ? <button type="button" className="workflows-action-btn" disabled title="Already installed">Installed</button>
                                : <button type="button" className="workflows-action-btn" onClick={() => handleInstallFromTemplate(d.name)} title="Create an installed copy">Install</button>}
                              <button type="button" className="workflows-action-icon" onClick={() => handleDownload(d.name)} title="Download YAML" aria-label="Download agent as YAML">
                                <svg width="14" height="14" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round"><path d="M8 2v9m0 0L5 8m3 3 3-3M2.5 12.5v1a1 1 0 0 0 1 1h9a1 1 0 0 0 1-1v-1" /></svg>
                              </button>
                            </>
                          )}
                        </div>
                      </>
                    ) : (
                      <>
                        <div />
                        <div className="workflows-card-actions">
                          {item.source === 'installed' && projectId && item.db_id && d.name !== 'default' && (
                            <button type="button" className="workflows-action-btn" onClick={() => handleMoveToProject(item)} title="Move to current project">To Project</button>
                          )}
                          {item.source === 'project' && item.db_id && (
                            <button type="button" className="workflows-action-btn" onClick={() => handleMoveToGlobal(item)} title="Move to global scope">To Global</button>
                          )}
                          <button type="button" className="workflows-action-icon" onClick={() => handleDuplicate(item)} title="Duplicate" aria-label="Duplicate agent">
                            <svg width="14" height="14" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round"><rect x="5.5" y="5.5" width="9" height="9" rx="1.5" /><path d="M10.5 5.5V2.5a1 1 0 0 0-1-1h-7a1 1 0 0 0-1 1v7a1 1 0 0 0 1 1h3" /></svg>
                          </button>
                          <button type="button" className="workflows-action-icon" onClick={() => handleDownload(d.name)} title="Download YAML" aria-label="Download agent as YAML">
                            <svg width="14" height="14" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round"><path d="M8 2v9m0 0L5 8m3 3 3-3M2.5 12.5v1a1 1 0 0 0 1 1h9a1 1 0 0 0 1-1v-1" /></svg>
                          </button>
                          {item.db_id ? (
                            <button type="button" className="workflows-action-icon workflows-action-icon--danger" onClick={() => handleDelete(item.db_id!)} title="Delete" aria-label="Delete agent">
                              <svg width="14" height="14" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round"><path d="M2.5 4.5h11M5.5 4.5V3a1 1 0 0 1 1-1h3a1 1 0 0 1 1 1v1.5M6.5 7v4.5M9.5 7v4.5" /><path d="M3.5 4.5 4 13a1 1 0 0 0 1 1h6a1 1 0 0 0 1-1l.5-8.5" /></svg>
                            </button>
                          ) : (
                            <button type="button" className="workflows-action-btn" onClick={() => handleImport(d.name)} disabled={importingName === d.name} title="Import to DB for customization">
                              {importingName === d.name ? '...' : 'Import'}
                            </button>
                          )}
                          {importResult?.name === d.name && (
                            <span className={`agent-def-import-result ${importResult.ok ? 'agent-def-import-result--ok' : 'agent-def-import-result--err'}`}>
                              {importResult.ok ? 'OK' : 'Fail'}
                            </span>
                          )}
                        </div>
                      </>
                    )}
                  </div>

                </div>
              )
            })}
          </div>
        )}
      </div>

      {/* YAML editor modal (kept for non-sidebar YAML edits) */}
      {yamlAgent && (
        <YamlEditorModal
          workflowName={yamlAgent.definition.name}
          yamlContent={yamlContent}
          loading={yamlLoading}
          onChange={setYamlContent}
          onSave={handleYamlSave}
          onClose={() => setYamlAgent(null)}
        />
      )}

      {/* Agent detail/edit panel */}
      <AgentEditForm
        isOpen={showCreateForm || selectedAgent !== null}
        readOnly={!showCreateForm && selectedAgent !== null && !selectedAgent.db_id}
        agentItem={selectedAgent}
        form={createForm}
        onChange={setCreateForm}
        onSave={editingId ? handleUpdate : handleCreate}
        onCancel={() => { onToggleCreateForm(false); setEditingId(null); setSelectedAgent(null) }}
        isEditing={!!editingId}
        providerModels={providerModels}
        saveDisabled={!createForm.name.trim()}
        editingId={editingId}
        branches={branches}
        isGitProject={isGitProject}
        projectId={projectId}
        rules={editRules}
        onRulesChange={handleEditRulesChange}
        ruleSelectors={editRuleSelectors}
        onRuleSelectorsChange={handleEditRuleSelectorsChange}
        variables={editVariables}
        onVariablesChange={handleEditVariablesChange}
        sidebarView={sidebarView}
        onViewChange={setSidebarView}
        yamlContent={sidebarYamlContent}
        onYamlChange={setSidebarYamlContent}
        onYamlSave={handleSidebarYamlSave}
        pipelines={pipelineList}
        editSkills={editSkills}
        onSkillsChange={setEditSkills}
        agentNames={definitions.filter(d => !d.deleted_at && d.source !== 'template' && d.enabled !== false).map(d => d.definition.name)}
      />
    </div>
  )
}

// =============================================================================
// Sub-components
// =============================================================================

function formatDuration(startIso: string, endIso?: string | null): string {
  const start = new Date(startIso).getTime()
  const end = endIso ? new Date(endIso).getTime() : Date.now()
  if (isNaN(start) || isNaN(end)) return '\u2014'
  const seconds = Math.floor((end - start) / 1000)
  if (seconds < 60) return `${seconds}s`
  const minutes = Math.floor(seconds / 60)
  const secs = seconds % 60
  if (minutes < 60) return `${minutes}m ${secs}s`
  const hours = Math.floor(minutes / 60)
  return `${hours}h ${minutes % 60}m`
}

function formatTimeAgo(iso: string): string {
  const diff = Date.now() - new Date(iso).getTime()
  if (isNaN(diff)) return '\u2014'
  const minutes = Math.floor(diff / 60000)
  if (minutes < 1) return 'just now'
  if (minutes < 60) return `${minutes}m ago`
  const hours = Math.floor(minutes / 60)
  if (hours < 24) return `${hours}h ago`
  return `${Math.floor(hours / 24)}d ago`
}

function RunningAgentCard({ agent, onCancel }: { agent: RunningAgent; onCancel: (id: string) => void }) {
  return (
    <div className="agent-run-card agent-run-card--running">
      <div className="agent-run-card-top">
        <span className="agent-run-pulse" />
        <span className="agent-run-id">{agent.run_id}</span>
        <span
          className="agent-def-badge agent-def-badge--filled"
          style={{ background: PROVIDER_COLORS[agent.provider] || '#666' }}
        >
          {agent.provider}
        </span>
        <span
          className="agent-def-badge agent-def-badge--filled"
          style={{ background: MODE_COLORS[agent.mode] || '#666' }}
        >
          {agent.mode}
        </span>
        {agent.workflow_name && (
          <span className="agent-def-badge agent-def-badge--dim">{agent.workflow_name}</span>
        )}
      </div>
      <div className="agent-run-card-bottom">
        <span className="agent-run-duration">{formatDuration(agent.started_at)}</span>
        {agent.pid && <span className="agent-run-meta">PID {agent.pid}</span>}
        <span className="agent-run-session">{agent.session_id.slice(0, 8)}</span>
        <button
          className="agent-defs-btn agent-defs-btn--danger agent-run-cancel"
          onClick={() => onCancel(agent.run_id)}
        >
          Cancel
        </button>
      </div>
    </div>
  )
}

function AgentRunRow({ run }: { run: AgentRun }) {
  const duration = run.started_at
    ? formatDuration(run.started_at, run.completed_at)
    : '—'

  return (
    <tr className="agent-run-row">
      <td>
        <span
          className="agent-run-status"
          style={{ color: STATUS_COLORS[run.status] || '#888' }}
        >
          {run.status}
        </span>
      </td>
      <td>
        <span
          className="agent-def-badge agent-def-badge--filled"
          style={{ background: PROVIDER_COLORS[run.provider] || '#666' }}
        >
          {run.provider}
        </span>
      </td>
      <td className="agent-run-prompt-cell">
        <span className="agent-run-prompt" title={run.prompt}>
          {run.prompt.slice(0, 80)}{run.prompt.length > 80 ? '...' : ''}
        </span>
      </td>
      <td>{run.turns_used}</td>
      <td>{duration}</td>
      <td>{run.started_at ? formatTimeAgo(run.started_at) : '—'}</td>
    </tr>
  )
}
