import { useState, useEffect, useCallback, useRef, useMemo } from 'react'
import { TabBar } from '../shared/TabBar'
import { RulesTab } from './RulesTab'
import { AgentsTab } from './AgentsTab'
import { PipelinesTab } from './PipelinesTab'
import { CodeMirrorEditor } from '../shared/CodeMirrorEditor'
import { useConfirmDialog } from '../../hooks/useConfirmDialog'
import './WorkflowsPage.css'

type ActiveTab = 'pipelines' | 'agents' | 'rules'
type SourceFilter = 'installed' | 'project' | 'templates' | 'deleted'

const TABS = [
  { id: 'pipelines', label: 'Pipelines' },
  { id: 'agents', label: 'Agents' },
  { id: 'rules', label: 'Rules' },
]

const SOURCE_OPTIONS: { value: SourceFilter; label: string }[] = [
  { value: 'installed', label: 'Installed' },
  { value: 'project', label: 'Project' },
  { value: 'templates', label: 'Templates' },
  { value: 'deleted', label: 'Deleted' },
]

export function WorkflowsPage({ projectId }: { projectId?: string }) {
  const [activeTab, setActiveTab] = useState<ActiveTab>('pipelines')
  const [searchText, setSearchText] = useState('')
  const [sourceFilter, setSourceFilter] = useState<SourceFilter>('installed')
  const [devMode, setDevMode] = useState(false)
  const [showRuleCreateModal, setShowRuleCreateModal] = useState(false)
  const [showAgentCreateForm, setShowAgentCreateForm] = useState(false)
  const [showPipelineCreate, setShowPipelineCreate] = useState(false)
  const [refreshKey, setRefreshKey] = useState(0)
  const [refreshing, setRefreshing] = useState(false)
  const [hideGobby, setHideGobby] = useState(false)
  const [hideInstalled, setHideInstalled] = useState(false)

  // Lifted tab-specific filter state
  const [pipelineEnabledFilter, setPipelineEnabledFilter] = useState<boolean | null>(null)
  const [agentProviderFilter, setAgentProviderFilter] = useState<string>('all')
  const [ruleEventFilter, setRuleEventFilter] = useState<string | null>(null)

  // Dynamic options reported by tabs
  const [agentProviders, setAgentProviders] = useState<string[]>([])
  const [ruleEventTypes, setRuleEventTypes] = useState<string[]>([])
  const [availableTags, setAvailableTags] = useState<string[]>([])

  // Cross-tab filters
  const [tagFilter, setTagFilter] = useState<string | null>(null)
  const [priorityFilter, setPriorityFilter] = useState<number | null>(null)

  // Rules bulk toggle state
  const [rulesAllEnabled, setRulesAllEnabled] = useState(false)

  // Popover state
  const [showFilterPopover, setShowFilterPopover] = useState(false)
  const filterRef = useRef<HTMLDivElement>(null)

  // Fetch dev_mode from admin status
  useEffect(() => {
    fetch('/api/admin/status')
      .then(res => res.ok ? res.json() : null)
      .then(data => {
        if (data?.dev_mode) setDevMode(true)
      })
      .catch(() => {})
  }, [])

  const handleRefresh = useCallback(async () => {
    setRefreshing(true)
    setRefreshKey(k => k + 1)
    setTimeout(() => setRefreshing(false), 600)
  }, [])

  // Click-outside to close popover
  useEffect(() => {
    if (!showFilterPopover) return
    const handleMouseDown = (e: MouseEvent) => {
      if (filterRef.current && !filterRef.current.contains(e.target as Node)) {
        setShowFilterPopover(false)
      }
    }
    document.addEventListener('mousedown', handleMouseDown)
    return () => document.removeEventListener('mousedown', handleMouseDown)
  }, [showFilterPopover])


  // Badge count
  const activeFilterCount = useMemo(() => {
    let count = 0
    if (sourceFilter !== 'installed') count++
    if (hideGobby) count++
    if (sourceFilter === 'templates' && hideInstalled) count++
    if (activeTab === 'pipelines' && pipelineEnabledFilter !== null) count++
    if (activeTab === 'agents' && agentProviderFilter !== 'all') count++
    if (activeTab === 'rules' && ruleEventFilter !== null) count++
    if (tagFilter !== null) count++
    if (priorityFilter !== null) count++
    return count
  }, [sourceFilter, hideGobby, hideInstalled, activeTab, pipelineEnabledFilter, agentProviderFilter, ruleEventFilter, tagFilter, priorityFilter])

  // Bulk actions
  const handleInstallAll = useCallback(async () => {
    const typeMap: Record<ActiveTab, string> = { pipelines: 'pipeline', agents: 'agent', rules: 'rule' }
    try {
      const res = await fetch(`/api/workflows/install-all-templates?workflow_type=${typeMap[activeTab]}`, {
        method: 'POST',
      })
      if (res.ok) {
        setSourceFilter('installed')
        setRefreshKey(k => k + 1)
      }
    } catch (e) {
      console.error('Failed to install all templates:', e)
    }
  }, [activeTab])

  const handleBulkToggleRules = useCallback(async () => {
    try {
      const res = await fetch('/api/rules/bulk-toggle', {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ source: sourceFilter, enabled: !rulesAllEnabled }),
      })
      if (res.ok) setRefreshKey(k => k + 1)
    } catch (e) {
      console.error('Failed to bulk toggle rules:', e)
    }
  }, [sourceFilter, rulesAllEnabled])

  return (
    <main className="workflows-page">
      {/* Title row */}
      <div className="workflows-toolbar">
        <div className="workflows-toolbar-left">
          <h2 className="workflows-toolbar-title">Workflows</h2>
        </div>
      </div>

      {/* Tab bar + search/filter/actions */}
      <div className="workflows-tab-row">
        <TabBar
          tabs={TABS}
          activeTab={activeTab}
          onTabChange={(id) => setActiveTab(id as ActiveTab)}
        />
        <div className="workflows-tab-row-right">
          <input
            className="workflows-search"
            type="text"
            placeholder={`Search ${activeTab}...`}
            value={searchText}
            onChange={e => setSearchText(e.target.value)}
          />
          <div className="workflows-filter-wrapper" ref={filterRef}>
            <button
              type="button"
              className={`workflows-filter-icon-btn ${activeFilterCount > 0 ? 'workflows-filter-icon-btn--active' : ''}`}
              onClick={() => setShowFilterPopover(v => !v)}
              title="Filter"
            >
              <svg width="16" height="16" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round">
                <line x1="2" y1="4" x2="14" y2="4" />
                <line x1="4" y1="8" x2="12" y2="8" />
                <line x1="6" y1="12" x2="10" y2="12" />
              </svg>
            </button>
            {showFilterPopover && (
              <FilterPopover
                sourceFilter={sourceFilter}
                onSourceFilterChange={setSourceFilter}
                hideGobby={hideGobby}
                onHideGobbyChange={setHideGobby}
                hideInstalled={hideInstalled}
                onHideInstalledChange={setHideInstalled}
                activeTab={activeTab}
                pipelineEnabledFilter={pipelineEnabledFilter}
                onPipelineEnabledFilterChange={setPipelineEnabledFilter}
                agentProviderFilter={agentProviderFilter}
                onAgentProviderFilterChange={setAgentProviderFilter}
                agentProviders={agentProviders}
                ruleEventFilter={ruleEventFilter}
                onRuleEventFilterChange={setRuleEventFilter}
                ruleEventTypes={ruleEventTypes}
                tagFilter={tagFilter}
                onTagFilterChange={setTagFilter}
                availableTags={availableTags}
                priorityFilter={priorityFilter}
                onPriorityFilterChange={setPriorityFilter}
              />
            )}
          </div>
          <button
            type="button"
            className={`workflows-toolbar-btn ${refreshing ? 'workflows-toolbar-btn--spinning' : ''}`}
            onClick={handleRefresh}
            title="Refresh"
          >
            &#x21bb;
          </button>
          {sourceFilter === 'templates' && (
            <button
              type="button"
              className="workflows-toolbar-btn"
              onClick={handleInstallAll}
            >
              Install All
            </button>
          )}
          {activeTab === 'rules' && (sourceFilter === 'installed' || sourceFilter === 'project') && (
            <div className="rules-enforcement-toggle" onClick={handleBulkToggleRules}>
              <div className={`workflows-toggle-track ${rulesAllEnabled ? 'workflows-toggle-track--on' : ''}`}>
                <div className="workflows-toggle-knob" />
              </div>
              <span>Enable All</span>
            </div>
          )}
          {activeTab === 'pipelines' && (
            <button
              type="button"
              className="workflows-new-btn"
              onClick={() => setShowPipelineCreate(true)}
            >
              + Pipeline
            </button>
          )}
          {activeTab === 'agents' && (
            <button
              type="button"
              className="workflows-new-btn"
              onClick={() => setShowAgentCreateForm(prev => !prev)}
            >
              + Agent
            </button>
          )}
          {activeTab === 'rules' && (
            <button
              type="button"
              className="workflows-new-btn"
              onClick={() => setShowRuleCreateModal(true)}
            >
              + Rule
            </button>
          )}
        </div>
      </div>

      {/* Tab content */}
      {activeTab === 'pipelines' && (
        <PipelinesTab
          searchText={searchText}
          sourceFilter={sourceFilter}
          devMode={devMode}
          showCreate={showPipelineCreate}
          onCreateHandled={() => setShowPipelineCreate(false)}
          refreshKey={refreshKey}
          projectId={projectId}
          hideGobby={hideGobby}
          hideInstalled={sourceFilter === 'templates' && hideInstalled}
          enabledFilter={pipelineEnabledFilter}
          tagFilter={tagFilter}
          priorityFilter={priorityFilter}
          onTagsChange={setAvailableTags}
        />
      )}

      {activeTab === 'agents' && (
        <AgentsTab
          searchText={searchText}
          sourceFilter={sourceFilter}
          devMode={devMode}
          showCreateForm={showAgentCreateForm}
          onToggleCreateForm={setShowAgentCreateForm}
          refreshKey={refreshKey}
          projectId={projectId}
          hideGobby={hideGobby}
          hideInstalled={sourceFilter === 'templates' && hideInstalled}
          filterProvider={agentProviderFilter}
          onProvidersChange={setAgentProviders}
          tagFilter={tagFilter}
          onTagsChange={setAvailableTags}
        />
      )}

      {activeTab === 'rules' && (
        <RulesTab
          searchText={searchText}
          sourceFilter={sourceFilter}
          devMode={devMode}
          showCreateModal={showRuleCreateModal}
          onCloseCreateModal={() => setShowRuleCreateModal(false)}
          refreshKey={refreshKey}
          projectId={projectId}
          hideGobby={hideGobby}
          hideInstalled={sourceFilter === 'templates' && hideInstalled}
          eventFilter={ruleEventFilter}
          onEventTypesChange={setRuleEventTypes}
          onAllEnabledChange={setRulesAllEnabled}
          tagFilter={tagFilter}
          priorityFilter={priorityFilter}
          onTagsChange={setAvailableTags}
        />
      )}
    </main>
  )
}

function FilterPopover({
  sourceFilter,
  onSourceFilterChange,
  hideGobby,
  onHideGobbyChange,
  hideInstalled,
  onHideInstalledChange,
  activeTab,
  pipelineEnabledFilter,
  onPipelineEnabledFilterChange,
  agentProviderFilter,
  onAgentProviderFilterChange,
  agentProviders,
  ruleEventFilter,
  onRuleEventFilterChange,
  ruleEventTypes,
  tagFilter,
  onTagFilterChange,
  availableTags,
  priorityFilter,
  onPriorityFilterChange,
}: {
  sourceFilter: SourceFilter
  onSourceFilterChange: (v: SourceFilter) => void
  hideGobby: boolean
  onHideGobbyChange: (v: boolean) => void
  hideInstalled: boolean
  onHideInstalledChange: (v: boolean) => void
  activeTab: ActiveTab
  pipelineEnabledFilter: boolean | null
  onPipelineEnabledFilterChange: (v: boolean | null) => void
  agentProviderFilter: string
  onAgentProviderFilterChange: (v: string) => void
  agentProviders: string[]
  ruleEventFilter: string | null
  onRuleEventFilterChange: (v: string | null) => void
  ruleEventTypes: string[]
  tagFilter: string | null
  onTagFilterChange: (v: string | null) => void
  availableTags: string[]
  priorityFilter: number | null
  onPriorityFilterChange: (v: number | null) => void
}) {
  return (
    <div className="workflows-filter-popover">
      {/* Source section */}
      <div className="workflows-filter-popover-section">
        <div className="workflows-filter-popover-label">Source</div>
        <div className="workflows-filter-popover-chips">
          {SOURCE_OPTIONS.map(opt => (
            <button
              key={opt.value}
              type="button"
              className={`workflows-filter-chip ${sourceFilter === opt.value ? 'workflows-filter-chip--active' : ''}`}
              onClick={() => onSourceFilterChange(opt.value)}
            >
              {opt.label}
            </button>
          ))}
        </div>
      </div>

      {/* Tab-specific section */}
      {activeTab === 'pipelines' && (
        <div className="workflows-filter-popover-section">
          <div className="workflows-filter-popover-label">Status</div>
          <div className="workflows-filter-popover-chips">
            <button
              type="button"
              className={`workflows-filter-chip ${pipelineEnabledFilter === true ? 'workflows-filter-chip--active' : ''}`}
              onClick={() => onPipelineEnabledFilterChange(pipelineEnabledFilter === true ? null : true)}
            >
              Enabled
            </button>
            <button
              type="button"
              className={`workflows-filter-chip ${pipelineEnabledFilter === false ? 'workflows-filter-chip--active' : ''}`}
              onClick={() => onPipelineEnabledFilterChange(pipelineEnabledFilter === false ? null : false)}
            >
              Disabled
            </button>
          </div>
        </div>
      )}

      {activeTab === 'agents' && agentProviders.length > 0 && (
        <div className="workflows-filter-popover-section">
          <div className="workflows-filter-popover-label">Provider</div>
          <div className="workflows-filter-popover-chips">
            {agentProviders.map(p => (
              <button
                key={p}
                type="button"
                className={`workflows-filter-chip ${agentProviderFilter === p ? 'workflows-filter-chip--active' : ''}`}
                onClick={() => onAgentProviderFilterChange(agentProviderFilter === p ? 'all' : p)}
              >
                {p}
              </button>
            ))}
          </div>
        </div>
      )}

      {activeTab === 'rules' && ruleEventTypes.length > 0 && (
        <div className="workflows-filter-popover-section">
          <div className="workflows-filter-popover-label">Event</div>
          <div className="workflows-filter-popover-chips">
            {ruleEventTypes.map(ev => (
              <button
                key={ev}
                type="button"
                className={`workflows-filter-chip ${ruleEventFilter === ev ? 'workflows-filter-chip--active' : ''}`}
                onClick={() => onRuleEventFilterChange(ruleEventFilter === ev ? null : ev)}
              >
                {ev}
              </button>
            ))}
          </div>
        </div>
      )}

      {/* Priority */}
      <div className="workflows-filter-popover-section">
        <div className="workflows-filter-popover-label">Priority</div>
        <div className="workflows-filter-popover-chips">
          {[1, 2, 3].map(p => (
            <button
              key={p}
              type="button"
              className={`workflows-filter-chip ${priorityFilter === p ? 'workflows-filter-chip--active' : ''}`}
              onClick={() => onPriorityFilterChange(priorityFilter === p ? null : p)}
            >
              P{p}
            </button>
          ))}
        </div>
      </div>

      {/* Tags */}
      {availableTags.length > 0 && (
        <div className="workflows-filter-popover-section">
          <div className="workflows-filter-popover-label">Tag</div>
          <div className="workflows-filter-popover-chips">
            {availableTags.map(tag => (
              <button
                key={tag}
                type="button"
                className={`workflows-filter-chip ${tagFilter === tag ? 'workflows-filter-chip--active' : ''}`}
                onClick={() => onTagFilterChange(tagFilter === tag ? null : tag)}
              >
                {tag}
              </button>
            ))}
          </div>
        </div>
      )}

      {/* Hide Built-in */}
      <div className="workflows-filter-popover-section workflows-filter-popover-section--bottom">
        <label className="workflows-filter-popover-checkbox" onClick={() => onHideGobbyChange(!hideGobby)}>
          <div className={`workflows-toggle-track ${hideGobby ? 'workflows-toggle-track--on' : ''}`}>
            <div className="workflows-toggle-knob" />
          </div>
          <span>Hide Built-in</span>
        </label>
        {sourceFilter === 'templates' && (
          <label className="workflows-filter-popover-checkbox" onClick={() => onHideInstalledChange(!hideInstalled)}>
            <div className={`workflows-toggle-track ${hideInstalled ? 'workflows-toggle-track--on' : ''}`}>
              <div className="workflows-toggle-knob" />
            </div>
            <span>Hide Installed</span>
          </label>
        )}
      </div>
    </div>
  )
}

export function YamlEditorModal({ workflowName, yamlContent, loading, onChange, onSave, onClose }: {
  workflowName: string
  yamlContent: string
  loading: boolean
  onChange: (content: string) => void
  onSave: () => Promise<void>
  onClose: () => void
}) {
  const { confirm, ConfirmDialogElement } = useConfirmDialog()
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [isDirty, setIsDirty] = useState(false)
  const wrappedOnChange = useCallback((content: string) => { setIsDirty(true); onChange(content) }, [onChange])

  const handleSave = async () => {
    setError(null)
    setSaving(true)
    try {
      await onSave()
      setIsDirty(false)
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Invalid YAML')
    } finally {
      setSaving(false)
    }
  }

  const handleClose = async () => {
    if (isDirty && !await confirm({ title: 'Unsaved changes', description: 'You have unsaved changes. Discard them?', confirmLabel: 'Discard', destructive: true })) return
    onClose()
  }

  return (
    <div className="workflows-modal-overlay" onClick={handleClose}>
      {ConfirmDialogElement}
      <div className="workflows-yaml-modal" onClick={e => e.stopPropagation()}>
        <div className="workflows-yaml-header">
          <h3>Edit YAML — {workflowName}</h3>
          <div className="workflows-yaml-header-actions">
            {error && <span className="workflows-yaml-error">{error}</span>}
            <button type="button" className="workflows-modal-cancel" onClick={handleClose}>Cancel</button>
            <button
              type="button"
              className="workflows-modal-submit"
              onClick={handleSave}
              disabled={saving || loading}
            >
              {saving ? 'Saving...' : 'Save'}
            </button>
          </div>
        </div>
        <div className="workflows-yaml-editor">
          {loading ? (
            <div className="workflows-loading">Loading YAML...</div>
          ) : (
            <CodeMirrorEditor
              content={yamlContent}
              language="yaml"
              onChange={wrappedOnChange}
              onSave={handleSave}
            />
          )}
        </div>
      </div>
    </div>
  )
}
