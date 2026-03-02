import { useState, useCallback, useMemo, useEffect, useRef } from 'react'
import * as yaml from 'js-yaml'
import { useRules } from '../../hooks/useRules'
import type { RuleSummary, RuleDetail } from '../../hooks/useRules'
import { useConfirmDialog } from '../../hooks/useConfirmDialog'
import { RuleEditForm, DEFAULT_RULE_FORM } from '../rules/RuleEditForm'
import type { RuleFormData } from '../rules/RuleEditForm'

const NEW_RULE_TEMPLATE = yaml.dump({
  name: 'my-rule',
  event: 'before_tool',
  enabled: true,
  effect: { type: 'block', message: 'Blocked by rule' },
}, { lineWidth: 120, noRefs: true })

function detailToForm(detail: RuleDetail): RuleFormData {
  return {
    name: detail.name,
    event: detail.event || 'before_tool',
    description: detail.description || '',
    priority: detail.priority,
    enabled: detail.enabled,
    group: detail.group || '',
    tags: detail.tags || [],
    when: detail.when || '',
    effect: (detail.effect as RuleFormData['effect']) || { type: 'block', reason: '' },
  }
}

function formToDefinition(form: RuleFormData): Record<string, unknown> {
  const def: Record<string, unknown> = {
    event: form.event,
    priority: form.priority,
    enabled: form.enabled,
  }
  if (form.description) def.description = form.description
  if (form.group) def.group = form.group
  if (form.when) def.when = form.when
  if (form.tags.length > 0) def.tags = form.tags
  def.effect = form.effect
  return def
}

interface RulesTabProps {
  searchText: string
  sourceFilter: 'installed' | 'project' | 'templates' | 'deleted'
  devMode: boolean
  showCreateModal: boolean
  onCloseCreateModal: () => void
  refreshKey?: number
  projectId?: string
  hideGobby?: boolean
  hideInstalled?: boolean
  eventFilter: string | null
  onEventTypesChange: (types: string[]) => void
  onAllEnabledChange: (allEnabled: boolean) => void
  tagFilter?: string | null
  priorityFilter?: number | null
  onTagsChange?: (tags: string[]) => void
}

export function RulesTab({ searchText, sourceFilter, devMode, showCreateModal, onCloseCreateModal, refreshKey = 0, projectId, hideGobby, hideInstalled, eventFilter, onEventTypesChange, onAllEnabledChange, tagFilter, priorityFilter, onTagsChange }: RulesTabProps) {
  const { confirm, ConfirmDialogElement } = useConfirmDialog()
  const {
    rules,
    isLoading,
    eventTypes,
    toggleRule,
    fetchRuleDetail,
    createRule,
    updateRule,
    deleteRule,
    installFromTemplate,
    fetchRules,
  } = useRules()

  // Re-fetch when refreshKey changes (skip initial render)
  const initialRef = useRef(true)
  useEffect(() => {
    if (initialRef.current) {
      initialRef.current = false
      return
    }
    fetchRules()
  }, [refreshKey, fetchRules])

  // Sidebar state
  const [sidebarRule, setSidebarRule] = useState<RuleSummary | null>(null)
  const [sidebarView, setSidebarView] = useState<'form' | 'yaml'>('form')
  const [ruleForm, setRuleForm] = useState<RuleFormData>({ ...DEFAULT_RULE_FORM })
  const [sidebarYaml, setSidebarYaml] = useState('')
  const [sidebarLoading, setSidebarLoading] = useState(false)

  // Open sidebar in create mode when "+ Rule" is clicked
  useEffect(() => {
    if (showCreateModal) {
      setRuleForm({ ...DEFAULT_RULE_FORM })
      setSidebarYaml(NEW_RULE_TEMPLATE)
      setSidebarView('form')
      setSidebarRule({ id: '__new__', name: '', event: '', source: 'installed', enabled: true, priority: 100, effect: null, tags: [], description: null, group: null, when: null })
      onCloseCreateModal()
    }
  }, [showCreateModal, onCloseCreateModal])

  const installedNames = useMemo(() => {
    const names = new Set<string>()
    for (const r of rules) {
      if (r.source === 'installed' && !(r as RuleSummary & { deleted_at?: string | null }).deleted_at) {
        names.add(r.name)
      }
    }
    return names
  }, [rules])

  // Filter rules
  const filteredRules = useMemo(() => {
    let result = rules

    // Source filter (exclusive)
    if (sourceFilter === 'installed') {
      result = result.filter(r => r.source === 'installed' && !(r as RuleSummary & { deleted_at?: string | null }).deleted_at)
    } else if (sourceFilter === 'project') {
      result = result.filter(r => r.source === 'project' && !(r as RuleSummary & { deleted_at?: string | null }).deleted_at)
    } else if (sourceFilter === 'templates') {
      result = result.filter(r => r.source === 'template' && !(r as RuleSummary & { deleted_at?: string | null }).deleted_at)
    } else if (sourceFilter === 'deleted') {
      result = result.filter(r => !!(r as RuleSummary & { deleted_at?: string | null }).deleted_at)
    }

    if (eventFilter) {
      result = result.filter(r => r.event === eventFilter)
    }
    if (hideGobby) {
      result = result.filter(r => !(r.tags && r.tags.includes('gobby')))
    }
    if (hideInstalled) {
      result = result.filter(r => !installedNames.has(r.name))
    }
    if (tagFilter) {
      result = result.filter(r => r.tags && r.tags.includes(tagFilter))
    }
    if (priorityFilter !== null && priorityFilter !== undefined) {
      result = result.filter(r => r.priority === priorityFilter)
    }
    if (searchText.trim()) {
      const q = searchText.toLowerCase()
      result = result.filter(r =>
        r.name.toLowerCase().includes(q) ||
        (r.description && r.description.toLowerCase().includes(q)) ||
        (r.event && r.event.toLowerCase().includes(q)) ||
        (r.tags && r.tags.some(t => t.toLowerCase().includes(q)))
      )
    }

    return result
  }, [rules, installedNames, eventFilter, searchText, sourceFilter, hideGobby, hideInstalled, tagFilter, priorityFilter])

  const handleToggle = useCallback(async (rule: RuleSummary) => {
    await toggleRule(rule.name, !rule.enabled)
  }, [toggleRule])

  const handleDelete = useCallback(async (rule: RuleSummary) => {
    const isTemplate = rule.source === 'template'
    const title = isTemplate ? `Force-delete "${rule.name}"?` : `Delete "${rule.name}"?`
    const description = isTemplate ? 'This is a template rule. It will be re-created on next sync.' : undefined
    if (!await confirm({ title, description, confirmLabel: 'Delete', destructive: true })) return
    await deleteRule(rule.name, isTemplate)
  }, [deleteRule, confirm])

  const openSidebar = useCallback(async (rule: RuleSummary, view: 'form' | 'yaml') => {
    setSidebarLoading(true)
    setSidebarRule(rule)
    setSidebarView(view)
    try {
      const detail = await fetchRuleDetail(rule.name)
      if (detail) {
        setRuleForm(detailToForm(detail))
        const obj: Record<string, unknown> = { name: detail.name, event: detail.event, priority: detail.priority }
        if (detail.description) obj.description = detail.description
        if (detail.when) obj.when = detail.when
        if (detail.effect) obj.effect = detail.effect
        if (detail.tags && detail.tags.length > 0) obj.tags = detail.tags
        if (detail.group) obj.group = detail.group
        obj.enabled = detail.enabled
        setSidebarYaml(yaml.dump(obj, { lineWidth: 120, noRefs: true }))
      } else {
        window.alert('Failed to load rule details')
        setSidebarRule(null)
      }
    } catch (e) {
      console.error('Failed to load rule:', e)
      setSidebarRule(null)
    } finally {
      setSidebarLoading(false)
    }
  }, [fetchRuleDetail])

  const handleCardClick = useCallback((rule: RuleSummary) => openSidebar(rule, 'form'), [openSidebar])

  const handleFormSave = useCallback(async () => {
    if (!sidebarRule) return
    const isCreating = sidebarRule.id === '__new__'
    const def = formToDefinition(ruleForm)
    try {
      if (isCreating) {
        if (!ruleForm.name.trim()) { window.alert('Rule name is required'); return }
        await createRule(ruleForm.name.trim(), def)
      } else {
        await updateRule(sidebarRule.name, { ...def, name: ruleForm.name })
      }
      setSidebarRule(null)
    } catch (e) {
      window.alert(`Save failed: ${e instanceof Error ? e.message : String(e)}`)
    }
  }, [sidebarRule, ruleForm, createRule, updateRule])

  const handleSidebarYamlSave = useCallback(async () => {
    if (!sidebarRule) return
    const isCreating = sidebarRule.id === '__new__'
    let parsed: Record<string, unknown>
    try {
      parsed = yaml.load(sidebarYaml, { schema: yaml.JSON_SCHEMA }) as Record<string, unknown>
    } catch (e) {
      window.alert(`Invalid YAML: ${e instanceof Error ? e.message : String(e)}`)
      return
    }
    if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) {
      window.alert('Invalid YAML: expected an object')
      return
    }
    try {
      if (isCreating) {
        const name = typeof parsed.name === 'string' && parsed.name.trim() ? parsed.name.trim() : ''
        if (!name) { window.alert('Rule must have a "name" field'); return }
        const { name: _name, ...definition } = parsed
        await createRule(name, definition)
      } else {
        await updateRule(sidebarRule.name, parsed)
      }
      setSidebarRule(null)
    } catch (e) {
      window.alert(`Save failed: ${e instanceof Error ? e.message : String(e)}`)
    }
  }, [sidebarRule, sidebarYaml, createRule, updateRule])

  const closeSidebar = useCallback(() => setSidebarRule(null), [])

  const handleDuplicate = useCallback(async (rule: RuleSummary) => {
    const newName = window.prompt('New rule name:', `${rule.name}-copy`)
    if (!newName) return
    const detail = await fetchRuleDetail(rule.name)
    if (!detail) return
    const definition: Record<string, unknown> = {}
    if (detail.event) definition.event = detail.event
    if (detail.description) definition.description = detail.description
    if (detail.when) definition.when = detail.when
    if (detail.effect) definition.effect = detail.effect
    if (detail.tags && detail.tags.length > 0) definition.tags = detail.tags
    if (detail.group) definition.group = detail.group
    definition.priority = detail.priority
    definition.enabled = detail.enabled
    await createRule(newName, definition)
  }, [fetchRuleDetail, createRule])

  const handleDownload = useCallback(async (rule: RuleSummary) => {
    const detail = await fetchRuleDetail(rule.name)
    if (!detail) return
    const obj: Record<string, unknown> = {
      name: detail.name,
      event: detail.event,
      priority: detail.priority,
    }
    if (detail.description) obj.description = detail.description
    if (detail.when) obj.when = detail.when
    if (detail.effect) obj.effect = detail.effect
    if (detail.tags && detail.tags.length > 0) obj.tags = detail.tags
    if (detail.group) obj.group = detail.group
    obj.enabled = detail.enabled
    const yamlStr = yaml.dump(obj, { lineWidth: 120, noRefs: true })
    const blob = new Blob([yamlStr], { type: 'application/x-yaml' })
    const url = URL.createObjectURL(blob)
    const a = document.createElement('a')
    a.href = url
    a.download = `${rule.name}.yaml`
    a.click()
    URL.revokeObjectURL(url)
  }, [fetchRuleDetail])

  const handleMoveToProject = useCallback(async (rule: RuleSummary) => {
    if (!projectId) return
    if (!await confirm({ title: 'Move to project?', description: `Move "${rule.name}" to the current project? It will no longer apply globally.`, confirmLabel: 'Move' })) return
    try {
      const res = await fetch(`/api/workflows/${rule.id}/move-to-project`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ project_id: projectId }),
      })
      if (res.ok) await fetchRules()
    } catch (e) {
      console.error('Failed to move rule to project:', e)
    }
  }, [projectId, fetchRules])

  const handleMoveToGlobal = useCallback(async (rule: RuleSummary) => {
    if (!await confirm({ title: 'Move to global?', description: `Move "${rule.name}" to global scope? It will apply to all projects.`, confirmLabel: 'Move' })) return
    try {
      const res = await fetch(`/api/workflows/${rule.id}/move-to-global`, {
        method: 'POST',
      })
      if (res.ok) await fetchRules()
    } catch (e) {
      console.error('Failed to move rule to global:', e)
    }
  }, [fetchRules])

  useEffect(() => {
    onEventTypesChange(eventTypes)
  }, [eventTypes, onEventTypesChange])

  const allTags = useMemo(() => {
    const tags = new Set<string>()
    for (const r of rules) {
      if (r.tags) for (const t of r.tags) tags.add(t)
    }
    return [...tags].sort()
  }, [rules])

  useEffect(() => {
    onTagsChange?.(allTags)
  }, [allTags, onTagsChange])

  useEffect(() => {
    const allEnabled = filteredRules.length > 0 && filteredRules.every(r => r.enabled)
    onAllEnabledChange(allEnabled)
  }, [filteredRules, onAllEnabledChange])

  return (
    <div className="rules-tab">
      {ConfirmDialogElement}
      {/* Card grid */}
      <div className="workflows-content">
        {isLoading ? (
          <div className="workflows-loading">Loading rules...</div>
        ) : filteredRules.length === 0 ? (
          <div className="workflows-empty">No rules match the current filters.</div>
        ) : (
          <div className="workflows-grid">
            {filteredRules.map(rule => (
              <RuleCard
                key={rule.id}
                rule={rule}
                devMode={devMode}
                projectId={projectId}
                onCardClick={() => handleCardClick(rule)}
                onToggle={() => handleToggle(rule)}
                onDelete={() => handleDelete(rule)}
                onDuplicate={() => handleDuplicate(rule)}
                onDownload={() => handleDownload(rule)}
                isInstalled={installedNames.has(rule.name)}
                onInstall={() => installFromTemplate(rule.id)}
                onMoveToProject={() => handleMoveToProject(rule)}
                onMoveToGlobal={() => handleMoveToGlobal(rule)}
              />
            ))}
          </div>
        )}
      </div>

      {/* Rule sidebar editor */}
      <RuleEditForm
        isOpen={!!sidebarRule}
        readOnly={sidebarRule?.source === 'template'}
        form={ruleForm}
        onChange={setRuleForm}
        onSave={handleFormSave}
        onCancel={closeSidebar}
        isEditing={!!sidebarRule && sidebarRule.id !== '__new__'}
        saveDisabled={sidebarLoading}
        sidebarView={sidebarView}
        onViewChange={setSidebarView}
        yamlContent={sidebarYaml}
        onYamlChange={setSidebarYaml}
        onYamlSave={handleSidebarYamlSave}
      />
    </div>
  )
}

function getEffectType(effect: Record<string, unknown> | null): string | null {
  if (!effect) return null
  const type = effect.type
  if (typeof type === 'string') return type
  // Infer from keys
  if ('block' in effect || effect.type === 'block') return 'block'
  if ('set_variable' in effect || 'variable' in effect) return 'set_variable'
  if ('inject_context' in effect || 'context' in effect) return 'inject_context'
  if ('mcp_call' in effect || 'tool' in effect) return 'mcp_call'
  return null
}

function RuleCard({ rule, devMode, projectId, isInstalled, onCardClick, onToggle, onDelete, onDuplicate, onDownload, onInstall, onMoveToProject, onMoveToGlobal }: {
  rule: RuleSummary
  devMode: boolean
  projectId?: string
  isInstalled: boolean
  onCardClick: () => void
  onToggle: () => void
  onDelete: () => void
  onDuplicate: () => void
  onDownload: () => void
  onInstall: () => void
  onMoveToProject: () => void
  onMoveToGlobal: () => void
}) {
  const effectType = getEffectType(rule.effect)
  const isTemplate = rule.source === 'template'
  const isDeleted = !!(rule as RuleSummary & { deleted_at?: string | null }).deleted_at

  return (
    <div className={`rules-card${isTemplate ? ' workflows-card--template' : ''}${isDeleted ? ' rules-card--deleted' : ''}`}>
      <div className="rules-card-main" onClick={onCardClick} style={{ cursor: 'pointer' }}>
        <div className="rules-card-header">
          <span className="rules-card-name">{rule.name}</span>
          <span className="workflows-card-type workflows-card-type--rule">rule</span>
        </div>

        {rule.description && (
          <div className="rules-card-desc">{rule.description}</div>
        )}

        <div className="workflows-card-badges" style={{ marginTop: 6 }}>
          {rule.tags && rule.tags.length > 0 && rule.tags.map(tag => (
            <span className="rules-card-tag" key={tag}>{tag}</span>
          ))}
          {rule.event && (
            <span className="rules-card-event">{rule.event}</span>
          )}
          {effectType && (
            <span className={`rules-card-effect rules-card-effect--${effectType}`}>{effectType}</span>
          )}
          <span className="rules-card-priority">P{rule.priority}</span>
        </div>
      </div>

      <div className="workflows-card-footer">
        {(isTemplate || isDeleted) ? (
          <>
            <div />
            <div className="workflows-card-actions">
              {devMode ? (
                <>
                  {isTemplate && (
                    isInstalled
                      ? <button type="button" className="workflows-action-btn" disabled title="Already installed">Installed</button>
                      : <button type="button" className="workflows-action-btn" onClick={e => { e.stopPropagation(); onInstall() }} title="Create an installed copy">Install</button>
                  )}
                  <button type="button" className="workflows-action-icon" onClick={e => { e.stopPropagation(); onDuplicate() }} title="Duplicate" aria-label="Duplicate rule">
                    <svg width="14" height="14" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round"><rect x="5.5" y="5.5" width="9" height="9" rx="1.5" /><path d="M10.5 5.5V2.5a1 1 0 0 0-1-1h-7a1 1 0 0 0-1 1v7a1 1 0 0 0 1 1h3" /></svg>
                  </button>
                  <button type="button" className="workflows-action-icon" onClick={e => { e.stopPropagation(); onDownload() }} title="Download YAML" aria-label="Download rule as YAML">
                    <svg width="14" height="14" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round"><path d="M8 2v9m0 0L5 8m3 3 3-3M2.5 12.5v1a1 1 0 0 0 1 1h9a1 1 0 0 0 1-1v-1" /></svg>
                  </button>
                  <button type="button" className="workflows-action-icon workflows-action-icon--danger" onClick={e => { e.stopPropagation(); onDelete() }} title="Delete rule" aria-label="Delete rule">
                    <svg width="14" height="14" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round"><path d="M2.5 4.5h11M5.5 4.5V3a1 1 0 0 1 1-1h3a1 1 0 0 1 1 1v1.5M6.5 7v4.5M9.5 7v4.5" /><path d="M3.5 4.5 4 13a1 1 0 0 0 1 1h6a1 1 0 0 0 1-1l.5-8.5" /></svg>
                  </button>
                </>
              ) : (
                <>
                  {isTemplate && (
                    isInstalled
                      ? <button type="button" className="workflows-action-btn" disabled title="Already installed">Installed</button>
                      : <button type="button" className="workflows-action-btn" onClick={e => { e.stopPropagation(); onInstall() }} title="Create an installed copy">Install</button>
                  )}
                  <button type="button" className="workflows-action-icon" onClick={e => { e.stopPropagation(); onDownload() }} title="Download YAML" aria-label="Download rule as YAML">
                    <svg width="14" height="14" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round"><path d="M8 2v9m0 0L5 8m3 3 3-3M2.5 12.5v1a1 1 0 0 0 1 1h9a1 1 0 0 0 1-1v-1" /></svg>
                  </button>
                </>
              )}
            </div>
          </>
        ) : (
          <>
            <div
              className="workflows-toggle"
              onClick={e => { e.stopPropagation(); onToggle() }}
            >
              <div className={`workflows-toggle-track ${rule.enabled ? 'workflows-toggle-track--on' : ''}`}>
                <div className="workflows-toggle-knob" />
              </div>
              <span>{rule.enabled ? 'On' : 'Off'}</span>
            </div>

            <div className="workflows-card-actions">
              {rule.source === 'installed' && projectId && (
                <button type="button" className="workflows-action-btn" onClick={e => { e.stopPropagation(); onMoveToProject() }} title="Move to current project">To Project</button>
              )}
              {rule.source === 'project' && (
                <button type="button" className="workflows-action-btn" onClick={e => { e.stopPropagation(); onMoveToGlobal() }} title="Move to global scope">To Global</button>
              )}
              <button type="button" className="workflows-action-icon" onClick={e => { e.stopPropagation(); onDuplicate() }} title="Duplicate" aria-label="Duplicate rule">
                <svg width="14" height="14" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round"><rect x="5.5" y="5.5" width="9" height="9" rx="1.5" /><path d="M10.5 5.5V2.5a1 1 0 0 0-1-1h-7a1 1 0 0 0-1 1v7a1 1 0 0 0 1 1h3" /></svg>
              </button>
              <button type="button" className="workflows-action-icon" onClick={e => { e.stopPropagation(); onDownload() }} title="Download YAML" aria-label="Download rule as YAML">
                <svg width="14" height="14" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round"><path d="M8 2v9m0 0L5 8m3 3 3-3M2.5 12.5v1a1 1 0 0 0 1 1h9a1 1 0 0 0 1-1v-1" /></svg>
              </button>
              <button type="button" className="workflows-action-icon workflows-action-icon--danger" onClick={e => { e.stopPropagation(); onDelete() }} title="Delete rule" aria-label="Delete rule">
                <svg width="14" height="14" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round"><path d="M2.5 4.5h11M5.5 4.5V3a1 1 0 0 1 1-1h3a1 1 0 0 1 1 1v1.5M6.5 7v4.5M9.5 7v4.5" /><path d="M3.5 4.5 4 13a1 1 0 0 0 1 1h6a1 1 0 0 0 1-1l.5-8.5" /></svg>
              </button>
            </div>
          </>
        )}
      </div>

    </div>
  )
}

