import { useState, useEffect, useCallback, useMemo, useRef } from 'react'
import { AGENT_DEFS_TAB_CLS } from '../agents/agents-styles'
import { useConfirmDialog } from '../../hooks/useConfirmDialog'
import { YamlEditorModal } from './WorkflowsPage'
import { AgentEditForm } from '../agents/AgentEditForm'
import type { AgentFormData } from '../agents/AgentEditForm'
import type { WorkflowStep } from '../agents/AgentStepsEditor'
import type { ProviderModelEntry } from '../../lib/providerModels'
import { AgentDefinitionsGrid } from './AgentsTab.cards'
import {
  createAgentDefinition,
  deleteAgentDefinition,
  downloadAgentDefinition,
  duplicateAgentDefinition,
  importAgentDefinition,
  installAgentFromTemplate,
  moveAgentToGlobal,
  moveAgentToProject,
  restoreAgentDefinition,
  restoreAgentFromTemplate,
  saveSidebarYamlAgentDefinition,
  saveYamlAgentDefinition,
  updateAgentDefinition,
} from './AgentsTab.actions'
import {
  filterAgentDefinitions,
  getAgentNames,
  getAllTags,
  getInstalledNames,
  getProviders,
  loadAgentDefinitions,
  loadBranchStatus,
  loadPipelineList,
  loadProviderCatalog,
} from './AgentsTab.data'
import {
  DEFAULT_FORM,
  type AgentDefInfo,
  type AgentsTabProps,
  type RuleSelectors,
} from './AgentsTab.types'
import {
  agentDefToYaml,
  agentToFormData,
  defaultSidebarYaml,
  extractAgentEditWorkflowState,
} from './AgentsTab.payloads'

export function AgentsTab({
  searchText,
  sourceFilter,
  devMode,
  showCreateForm,
  onToggleCreateForm,
  refreshKey = 0,
  projectId,
  hideGobby,
  hideInstalled,
  filterProvider,
  onProvidersChange,
  tagFilter,
  onTagsChange,
}: AgentsTabProps) {
  const { confirm, ConfirmDialogElement } = useConfirmDialog()
  const [definitions, setDefinitions] = useState<AgentDefInfo[]>([])
  const [loading, setLoading] = useState(true)
  const [selectedAgent, setSelectedAgent] = useState<AgentDefInfo | null>(null)
  const [editingId, setEditingId] = useState<string | null>(null)
  const [importingName, setImportingName] = useState<string | null>(null)
  const [importResult, setImportResult] = useState<{ name: string; ok: boolean } | null>(null)
  const [toastMessage, setToastMessage] = useState<{ text: string; type: 'success' | 'error' } | null>(null)
  const [yamlAgent, setYamlAgent] = useState<AgentDefInfo | null>(null)
  const [yamlContent, setYamlContent] = useState('')
  const [yamlLoading] = useState(false)
  const [createForm, setCreateForm] = useState<AgentFormData>({ ...DEFAULT_FORM })
  const [branches, setBranches] = useState<string[]>([])
  const [isGitProject, setIsGitProject] = useState(true)
  const [editRules, setEditRules] = useState<string[]>([])
  const [editRuleSelectors, setEditRuleSelectors] = useState<RuleSelectors | null>(null)
  const [editVariables, setEditVariables] = useState<Record<string, unknown>>({})
  const [editSkills, setEditSkills] = useState<string[]>([])
  const [editSteps, setEditSteps] = useState<WorkflowStep[]>([])
  const [editBlockedTools, setEditBlockedTools] = useState<string[]>([])
  const [editBlockedMcpTools, setEditBlockedMcpTools] = useState<string[]>([])
  const [sidebarView, setSidebarView] = useState<'form' | 'yaml'>('form')
  const [sidebarYamlContent, setSidebarYamlContent] = useState('')
  const [pipelineList, setPipelineList] = useState<{ id: string; name: string }[]>([])
  const [providerCatalog, setProviderCatalog] = useState<ProviderModelEntry[]>([])

  const showToast = useCallback((text: string, type: 'success' | 'error') => {
    setToastMessage({ text, type })
    setTimeout(() => setToastMessage(null), 4000)
  }, [])

  const fetchDefinitions = useCallback(async (includeDeleted = false) => {
    setLoading(true)
    try {
      const nextDefinitions = await loadAgentDefinitions(includeDeleted)
      if (nextDefinitions) setDefinitions(nextDefinitions)
    } catch (e) {
      console.error('Failed to fetch agent definitions:', e)
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    fetchDefinitions(true)
  }, [fetchDefinitions])

  const initialRef = useRef(true)
  useEffect(() => {
    if (initialRef.current) {
      initialRef.current = false
      return
    }
    fetchDefinitions(true)
  }, [refreshKey, fetchDefinitions])

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
      setEditSteps([])
      setEditBlockedTools([])
      setEditBlockedMcpTools([])
      setSelectedAgent(null)
      setSidebarView('form')
      setSidebarYamlContent(defaultSidebarYaml())
    }
  }, [showCreateForm, editingId])

  useEffect(() => {
    loadBranchStatus(projectId).then((status) => {
      if (status.branches) setBranches(status.branches)
      if (status.isGitProject !== undefined) setIsGitProject(status.isGitProject)
    })
  }, [projectId])

  useEffect(() => {
    loadProviderCatalog()
      .then(setProviderCatalog)
      .catch((e) => console.error('Failed to fetch provider catalog:', e))
  }, [])

  useEffect(() => {
    loadPipelineList()
      .then(setPipelineList)
      .catch(() => setPipelineList([]))
  }, [])

  const agentNames = useMemo(
    () => getAgentNames(definitions, createForm.name),
    [definitions, createForm.name],
  )
  const installedNames = useMemo(() => getInstalledNames(definitions), [definitions])
  const filtered = useMemo(
    () => filterAgentDefinitions(definitions, installedNames, {
      sourceFilter,
      filterProvider,
      searchText,
      hideGobby,
      hideInstalled,
      tagFilter,
    }),
    [
      definitions,
      installedNames,
      sourceFilter,
      filterProvider,
      searchText,
      hideGobby,
      hideInstalled,
      tagFilter,
    ],
  )
  const providers = useMemo(() => getProviders(definitions), [definitions])
  const allTags = useMemo(() => getAllTags(definitions), [definitions])

  useEffect(() => {
    onProvidersChange(providers)
  }, [providers, onProvidersChange])

  useEffect(() => {
    onTagsChange?.(allTags)
  }, [allTags, onTagsChange])

  const mutationContext = useMemo(() => ({
    form: createForm,
    rules: editRules,
    ruleSelectors: editRuleSelectors,
    variables: editVariables,
    skills: editSkills,
    steps: editSteps,
    blockedTools: editBlockedTools,
    blockedMcpTools: editBlockedMcpTools,
    fetchDefinitions,
    showToast,
    onToggleCreateForm,
    setCreateForm,
  }), [
    createForm,
    editRules,
    editRuleSelectors,
    editVariables,
    editSkills,
    editSteps,
    editBlockedTools,
    editBlockedMcpTools,
    fetchDefinitions,
    showToast,
    onToggleCreateForm,
  ])

  const handleCreate = useCallback(
    () => createAgentDefinition(mutationContext),
    [mutationContext],
  )

  const handleEdit = useCallback((item: AgentDefInfo) => {
    setCreateForm(agentToFormData(item))
    setEditingId(item.db_id)
    const workflowState = extractAgentEditWorkflowState(item)
    setEditSteps(workflowState.steps)
    setEditBlockedTools(workflowState.blockedTools)
    setEditBlockedMcpTools(workflowState.blockedMcpTools)
    setEditRules(workflowState.rules)
    setEditRuleSelectors(workflowState.ruleSelectors)
    setEditVariables(workflowState.variables)
    setEditSkills(workflowState.skills)
    setSelectedAgent(null)
    setSidebarView('form')
    setSidebarYamlContent(agentDefToYaml(item.definition))
    onToggleCreateForm(true)
  }, [onToggleCreateForm])

  const handleUpdate = useCallback(
    () => updateAgentDefinition({ ...mutationContext, editingId, setEditingId }),
    [mutationContext, editingId],
  )

  const handleEditRulesChange = useCallback((newRules: string[]) => {
    setEditRules(newRules)
    if (editingId) {
      setDefinitions((prev) => prev.map((definition) =>
        definition.db_id === editingId
          ? {
            ...definition,
            definition: {
              ...definition.definition,
              workflows: { ...definition.definition.workflows, rules: newRules },
            },
          }
          : definition,
      ))
    }
  }, [editingId])

  const handleEditRuleSelectorsChange = useCallback((newSelectors: RuleSelectors) => {
    setEditRuleSelectors(newSelectors)
    if (editingId) {
      setDefinitions((prev) => prev.map((definition) =>
        definition.db_id === editingId
          ? {
            ...definition,
            definition: {
              ...definition.definition,
              workflows: {
                ...definition.definition.workflows,
                rule_selectors: newSelectors,
              },
            },
          }
          : definition,
      ))
    }
  }, [editingId])

  const handleEditVariablesChange = useCallback((newVars: Record<string, unknown>) => {
    setEditVariables(newVars)
    if (editingId) {
      setDefinitions((prev) => prev.map((definition) =>
        definition.db_id === editingId
          ? {
            ...definition,
            definition: {
              ...definition.definition,
              workflows: { ...definition.definition.workflows, variables: newVars },
            },
          }
          : definition,
      ))
    }
  }, [editingId])

  const handleDuplicate = useCallback(
    (item: AgentDefInfo) => duplicateAgentDefinition(item, fetchDefinitions, showToast),
    [fetchDefinitions, showToast],
  )

  const handleDelete = useCallback(
    (dbId: string) => deleteAgentDefinition(dbId, confirm, fetchDefinitions, showToast),
    [confirm, fetchDefinitions, showToast],
  )

  const handleRestore = useCallback(
    (dbId: string) => restoreAgentDefinition(dbId, fetchDefinitions, showToast),
    [fetchDefinitions, showToast],
  )

  const handleDownload = useCallback(
    (name: string) => downloadAgentDefinition(name),
    [],
  )

  const handleYamlSave = useCallback(async () => {
    await saveYamlAgentDefinition({ yamlAgent, yamlContent, fetchDefinitions })
    setYamlAgent(null)
  }, [yamlAgent, yamlContent, fetchDefinitions])

  const handleSidebarYamlSave = useCallback(
    () => saveSidebarYamlAgentDefinition({
      editingId,
      sidebarYamlContent,
      form: createForm,
      fetchDefinitions,
      onToggleCreateForm,
      setEditingId,
      setCreateForm,
      showToast,
    }),
    [
      editingId,
      sidebarYamlContent,
      createForm,
      fetchDefinitions,
      onToggleCreateForm,
      showToast,
    ],
  )

  const handleInstallFromTemplate = useCallback(
    (name: string) => installAgentFromTemplate(name, fetchDefinitions, showToast),
    [fetchDefinitions, showToast],
  )

  const handleMoveToProject = useCallback(
    (item: AgentDefInfo) => moveAgentToProject(item, projectId, confirm, fetchDefinitions),
    [confirm, projectId, fetchDefinitions],
  )

  const handleMoveToGlobal = useCallback(
    (item: AgentDefInfo) => moveAgentToGlobal(item, confirm, fetchDefinitions),
    [confirm, fetchDefinitions],
  )

  const handleRestoreFromTemplate = useCallback(
    (item: AgentDefInfo) => restoreAgentFromTemplate(item, confirm, fetchDefinitions),
    [confirm, fetchDefinitions],
  )

  const handleImport = useCallback(async (name: string) => {
    setImportingName(name)
    setImportResult(null)
    try {
      const ok = await importAgentDefinition(name, fetchDefinitions)
      setImportResult({ name, ok })
    } finally {
      setImportingName(null)
      setTimeout(() => setImportResult(null), 3000)
    }
  }, [fetchDefinitions])

  const handleOpenAgent = useCallback((item: AgentDefInfo) => {
    if (item.db_id) {
      handleEdit(item)
    } else {
      setSelectedAgent(item)
      setSidebarYamlContent(agentDefToYaml(item.definition))
    }
  }, [handleEdit])

  return (
    <div className={AGENT_DEFS_TAB_CLS}>
      {ConfirmDialogElement}
      {toastMessage && (
        <div
          className={`agent-defs-toast fixed right-5 top-[60px] z-[1000] cursor-pointer rounded-md px-4 py-2 text-[length:var(--text-base)] text-[var(--accent-foreground)] [animation:fadeIn_0.2s_ease] ${toastMessage.type === 'success' ? 'agent-defs-toast--success bg-[var(--color-success-foreground)]' : 'bg-[var(--color-error)]'}`}
          onClick={() => setToastMessage(null)}
        >
          {toastMessage.text}
        </div>
      )}

      <AgentDefinitionsGrid
        loading={loading}
        definitions={filtered}
        devMode={devMode}
        installedNames={installedNames}
        importingName={importingName}
        importResult={importResult}
        projectId={projectId}
        onOpenAgent={handleOpenAgent}
        onDuplicate={handleDuplicate}
        onDelete={handleDelete}
        onRestore={handleRestore}
        onDownload={handleDownload}
        onInstallFromTemplate={handleInstallFromTemplate}
        onMoveToProject={handleMoveToProject}
        onMoveToGlobal={handleMoveToGlobal}
        onRestoreFromTemplate={handleRestoreFromTemplate}
        onImport={handleImport}
      />

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

      <AgentEditForm
        isOpen={showCreateForm || selectedAgent !== null}
        readOnly={!showCreateForm && selectedAgent !== null && !selectedAgent.db_id}
        agentItem={selectedAgent}
        form={createForm}
        onChange={setCreateForm}
        onSave={editingId ? handleUpdate : handleCreate}
        onCancel={() => { onToggleCreateForm(false); setEditingId(null); setSelectedAgent(null) }}
        isEditing={!!editingId}
        providerCatalog={providerCatalog}
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
        steps={editSteps}
        onStepsChange={setEditSteps}
        blockedTools={editBlockedTools}
        onBlockedToolsChange={setEditBlockedTools}
        blockedMcpTools={editBlockedMcpTools}
        onBlockedMcpToolsChange={setEditBlockedMcpTools}
        agentNames={agentNames}
      />
    </div>
  )
}
