import { useState, useCallback, useMemo, useEffect, useRef } from 'react'
import * as yaml from 'js-yaml'
import { useWorkflows } from '../../hooks/useWorkflows'
import type { WorkflowDetail } from '../../hooks/useWorkflows'
import { PipelineEditor } from './PipelineEditor'
import type { PipelineEditorHandle } from './PipelineEditor'
import { CodeMirrorEditor } from '../shared/CodeMirrorEditor'
import { SidebarPanel } from '../shared/SidebarPanel'
import { YamlEditorModal } from './WorkflowsPage'

const SCAFFOLD_PIPELINE_YAML = `name: new-pipeline
type: pipeline
description: ""
steps:
  - id: step-1
    exec: echo hello
`

interface PipelinesTabProps {
  searchText: string
  sourceFilter: 'installed' | 'project' | 'templates' | 'deleted'
  devMode: boolean
  createMode: 'builder' | 'yaml' | null
  onCreateModeHandled: () => void
  refreshKey?: number
  projectId?: string
  hideGobby?: boolean
  hideInstalled?: boolean
  enabledFilter: boolean | null
}

export function PipelinesTab({ searchText, sourceFilter, devMode, createMode, onCreateModeHandled, refreshKey = 0, projectId, hideGobby, hideInstalled, enabledFilter }: PipelinesTabProps) {
  const {
    workflows,
    isLoading,
    fetchWorkflows,
    createWorkflow,
    updateWorkflow,
    deleteWorkflow,
    duplicateWorkflow,
    toggleEnabled,
    importYaml,
    exportYaml,
    restoreWorkflow,
    installFromTemplate,
  } = useWorkflows()

  const [showImportModal, setShowImportModal] = useState(false)
  const [editingWorkflow, setEditingWorkflow] = useState<WorkflowDetail | null>(null)
  const [yamlEditorWf, setYamlEditorWf] = useState<WorkflowDetail | null>(null)
  const [yamlContent, setYamlContent] = useState('')
  const [yamlLoading, setYamlLoading] = useState(false)
  const editorRef = useRef<PipelineEditorHandle>(null)

  // Always fetch with include_deleted so the filter can work
  useEffect(() => {
    fetchWorkflows({ include_deleted: true })
  }, [fetchWorkflows])

  // Re-fetch when refreshKey changes (skip initial render)
  const initialRef = useRef(true)
  useEffect(() => {
    if (initialRef.current) {
      initialRef.current = false
      return
    }
    fetchWorkflows({ include_deleted: true })
  }, [refreshKey, fetchWorkflows])

  // Handle create mode from parent dropdown
  useEffect(() => {
    if (createMode === 'builder') {
      onCreateModeHandled()
      const scaffoldDef = { name: 'new-pipeline', type: 'pipeline', description: '', steps: [{ id: 'step-1', exec: 'echo hello' }] }
      createWorkflow({
        name: 'new-pipeline',
        definition_json: JSON.stringify(scaffoldDef),
        workflow_type: 'pipeline',
      }).then(result => {
        if (result) setEditingWorkflow(result)
      })
    } else if (createMode === 'yaml') {
      onCreateModeHandled()
      setShowImportModal(true)
    }
  }, [createMode, onCreateModeHandled, createWorkflow])

  const installedNames = useMemo(() => {
    const names = new Set<string>()
    for (const w of workflows) {
      if (w.workflow_type === 'pipeline' && w.source === 'installed' && !w.deleted_at) {
        names.add(w.name)
      }
    }
    return names
  }, [workflows])

  // Filtering logic
  const filteredWorkflows = useMemo(() => {
    let result = workflows.filter(w => w.workflow_type === 'pipeline')

    if (sourceFilter === 'installed') {
      result = result.filter(w => w.source === 'installed' && !w.deleted_at)
    } else if (sourceFilter === 'project') {
      result = result.filter(w => w.source === 'project' && !w.deleted_at)
    } else if (sourceFilter === 'templates') {
      result = result.filter(w => w.source === 'template' && !w.deleted_at)
    } else if (sourceFilter === 'deleted') {
      result = result.filter(w => !!w.deleted_at)
    }

    if (hideGobby) {
      result = result.filter(w => !(w.tags && w.tags.includes('gobby')))
    }
    if (hideInstalled) {
      result = result.filter(w => !installedNames.has(w.name))
    }
    if (enabledFilter !== null) {
      result = result.filter(w => w.enabled === enabledFilter)
    }

    if (searchText.trim()) {
      const q = searchText.toLowerCase()
      result = result.filter(w =>
        w.name.toLowerCase().includes(q) ||
        (w.description && w.description.toLowerCase().includes(q)) ||
        (w.tags && w.tags.some(t => t.toLowerCase().includes(q)))
      )
    }

    return result
  }, [workflows, installedNames, enabledFilter, searchText, sourceFilter, hideGobby, hideInstalled])

  const handleDelete = useCallback(async (wf: WorkflowDetail) => {
    if (!window.confirm(`Delete "${wf.name}"?`)) return
    try {
      await deleteWorkflow(wf.id)
    } finally {
      fetchWorkflows({ include_deleted: true })
    }
  }, [deleteWorkflow, fetchWorkflows])

  const handleRestore = useCallback(async (wf: WorkflowDetail) => {
    try {
      await restoreWorkflow(wf.id)
    } finally {
      fetchWorkflows({ include_deleted: true })
    }
  }, [restoreWorkflow, fetchWorkflows])

  const handleDuplicate = useCallback(async (wf: WorkflowDetail) => {
    const newName = window.prompt('New name:', `${wf.name}-copy`)
    if (!newName) return
    await duplicateWorkflow(wf.id, newName)
  }, [duplicateWorkflow])

  const handleExport = useCallback(async (wf: WorkflowDetail) => {
    const yamlStr = await exportYaml(wf.id)
    if (yamlStr) {
      const blob = new Blob([yamlStr], { type: 'application/x-yaml' })
      const url = URL.createObjectURL(blob)
      const a = document.createElement('a')
      a.href = url
      a.download = `${wf.name}.yaml`
      a.click()
      URL.revokeObjectURL(url)
    }
  }, [exportYaml])

  const handleMoveToProject = useCallback(async (wf: WorkflowDetail) => {
    if (!projectId) return
    if (!window.confirm(`Move "${wf.name}" to the current project? It will no longer apply globally.`)) return
    try {
      const res = await fetch(`/api/workflows/${wf.id}/move-to-project`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ project_id: projectId }),
      })
      if (res.ok) fetchWorkflows({ include_deleted: true })
    } catch (e) {
      console.error('Failed to move workflow to project:', e)
    }
  }, [projectId, fetchWorkflows])

  const handleMoveToGlobal = useCallback(async (wf: WorkflowDetail) => {
    if (!window.confirm(`Move "${wf.name}" to global scope? It will apply to all projects.`)) return
    try {
      const res = await fetch(`/api/workflows/${wf.id}/move-to-global`, {
        method: 'POST',
      })
      if (res.ok) fetchWorkflows({ include_deleted: true })
    } catch (e) {
      console.error('Failed to move workflow to global:', e)
    }
  }, [fetchWorkflows])

  const handleYamlEdit = useCallback(async (wf: WorkflowDetail) => {
    setYamlLoading(true)
    setYamlEditorWf(wf)
    try {
      const yamlStr = await exportYaml(wf.id)
      setYamlContent(yamlStr || '')
    } catch (e) {
      console.error('Failed to export YAML:', e)
      setYamlContent('')
      window.alert(`Failed to export workflow YAML: ${e instanceof Error ? e.message : String(e)}`)
      setYamlEditorWf(null)
      return
    } finally {
      setYamlLoading(false)
    }
  }, [exportYaml])

  const handleYamlSave = useCallback(async () => {
    if (!yamlEditorWf) return
    let parsed: Record<string, unknown>
    try {
      parsed = yaml.load(yamlContent, { schema: yaml.JSON_SCHEMA }) as Record<string, unknown>
    } catch (e) {
      throw new Error(`Invalid YAML: ${e instanceof Error ? e.message : String(e)}`)
    }
    if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) throw new Error('Invalid YAML: expected an object')
    if (parsed.name !== undefined && typeof parsed.name !== 'string') throw new Error('Invalid YAML: "name" must be a string')
    if (typeof parsed.name === 'string' && !parsed.name.trim()) throw new Error('Invalid YAML: "name" must not be empty')
    if (parsed.description !== undefined && typeof parsed.description !== 'string') throw new Error('Invalid YAML: "description" must be a string')
    if (parsed.steps !== undefined && !Array.isArray(parsed.steps)) throw new Error('Invalid YAML: "steps" must be an array')
    await updateWorkflow(yamlEditorWf.id, {
      name: (parsed.name as string) || yamlEditorWf.name,
      description: (parsed.description as string) || undefined,
      definition_json: JSON.stringify(parsed),
    })
    setYamlEditorWf(null)
  }, [yamlEditorWf, yamlContent, updateWorkflow])

  const stepCount = useCallback((wf: WorkflowDetail) => {
    try {
      const data = JSON.parse(wf.definition_json)
      return (data.steps || []).length
    } catch {
      return 0
    }
  }, [])

  const handleEditorClose = useCallback(() => {
    if (editorRef.current?.isDirty && !window.confirm('You have unsaved changes. Discard them?')) return
    setEditingWorkflow(null)
    fetchWorkflows({ include_deleted: true })
  }, [fetchWorkflows])

  return (
    <>
      {/* Card grid */}
      <div className="workflows-content">
        {isLoading ? (
          <div className="workflows-loading">Loading...</div>
        ) : filteredWorkflows.length === 0 ? (
          <div className="workflows-empty">No pipelines match the current filters.</div>
        ) : (
          <div className="workflows-grid">
            {filteredWorkflows.map(wf => {
              const isTemplate = wf.source === 'template'
              const cardClass = [
                'workflows-card',
                wf.deleted_at ? 'workflows-card--deleted' : '',
                isTemplate ? 'workflows-card--template' : '',
              ].filter(Boolean).join(' ')

              return (
                <div className={cardClass} key={wf.id}>
                  <div className="workflows-card-header">
                    <span className={`workflows-card-name${wf.deleted_at ? ' workflows-card-name--deleted' : ''}`}>{wf.name}</span>
                    <div style={{ display: 'flex', gap: 6, alignItems: 'center' }}>
                      <span className={`workflows-card-type workflows-card-type--${wf.workflow_type}`}>
                        {wf.workflow_type}
                      </span>
                    </div>
                  </div>

                  {wf.description && (
                    <div className="workflows-card-desc">{wf.description}</div>
                  )}

                  <div className="workflows-card-badges">
                    <span className="workflows-card-badge workflows-card-badge--source">
                      {wf.source}
                    </span>
                    <span className="workflows-card-badge workflows-card-badge--priority">
                      P{wf.priority}
                    </span>
                    <span className="workflows-card-badge">
                      {stepCount(wf)} step{stepCount(wf) !== 1 ? 's' : ''}
                    </span>
                    <span className="workflows-card-badge">v{wf.version}</span>
                    {wf.tags && wf.tags.map(tag => (
                      <span className="workflows-card-badge" key={tag}>{tag}</span>
                    ))}
                  </div>

                  <div className="workflows-card-footer">
                    {wf.deleted_at ? (
                      <div className="workflows-card-actions">
                        <button
                          type="button"
                          className="workflows-action-btn workflows-action-btn--restore"
                          onClick={() => handleRestore(wf)}
                          title="Restore this workflow"
                        >
                          Restore
                        </button>
                      </div>
                    ) : isTemplate ? (
                      <>
                        <div />
                        <div className="workflows-card-actions">
                          {devMode ? (
                            <>
                              {installedNames.has(wf.name)
                                ? <button type="button" className="workflows-action-btn" disabled title="Already installed">Installed</button>
                                : <button type="button" className="workflows-action-btn" onClick={() => installFromTemplate(wf.id)} title="Create an installed copy">Install</button>}
                              <button type="button" className="workflows-action-btn" onClick={() => handleYamlEdit(wf)} title="Edit as YAML">YAML</button>
                              <button type="button" className="workflows-action-btn" onClick={() => setEditingWorkflow(wf)} title="Edit pipeline steps">Edit</button>
                              <button type="button" className="workflows-action-icon" onClick={() => handleDuplicate(wf)} title="Duplicate" aria-label="Duplicate workflow">
                                <svg width="14" height="14" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round"><rect x="5.5" y="5.5" width="9" height="9" rx="1.5" /><path d="M10.5 5.5V2.5a1 1 0 0 0-1-1h-7a1 1 0 0 0-1 1v7a1 1 0 0 0 1 1h3" /></svg>
                              </button>
                              <button type="button" className="workflows-action-icon" onClick={() => handleExport(wf)} title="Download YAML" aria-label="Download workflow as YAML">
                                <svg width="14" height="14" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round"><path d="M8 2v9m0 0L5 8m3 3 3-3M2.5 12.5v1a1 1 0 0 0 1 1h9a1 1 0 0 0 1-1v-1" /></svg>
                              </button>
                              <button type="button" className="workflows-action-icon workflows-action-icon--danger" onClick={() => handleDelete(wf)} title="Delete" aria-label="Delete workflow">
                                <svg width="14" height="14" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round"><path d="M2.5 4.5h11M5.5 4.5V3a1 1 0 0 1 1-1h3a1 1 0 0 1 1 1v1.5M6.5 7v4.5M9.5 7v4.5" /><path d="M3.5 4.5 4 13a1 1 0 0 0 1 1h6a1 1 0 0 0 1-1l.5-8.5" /></svg>
                              </button>
                            </>
                          ) : (
                            <>
                              {installedNames.has(wf.name)
                                ? <button type="button" className="workflows-action-btn" disabled title="Already installed">Installed</button>
                                : <button type="button" className="workflows-action-btn" onClick={() => installFromTemplate(wf.id)} title="Create an installed copy">Install</button>}
                              <button type="button" className="workflows-action-icon" onClick={() => handleExport(wf)} title="Download YAML" aria-label="Download workflow as YAML">
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
                          onClick={() => toggleEnabled(wf.id)}
                        >
                          <div className={`workflows-toggle-track ${wf.enabled ? 'workflows-toggle-track--on' : ''}`}>
                            <div className="workflows-toggle-knob" />
                          </div>
                          <span>{wf.enabled ? 'On' : 'Off'}</span>
                        </div>

                        <div className="workflows-card-actions">
                          {wf.source === 'installed' && projectId && (
                            <button type="button" className="workflows-action-btn" onClick={() => handleMoveToProject(wf)} title="Move to current project">To Project</button>
                          )}
                          {wf.source === 'project' && (
                            <button type="button" className="workflows-action-btn" onClick={() => handleMoveToGlobal(wf)} title="Move to global scope">To Global</button>
                          )}
                          <button type="button" className="workflows-action-btn" onClick={() => handleYamlEdit(wf)} title="Edit as YAML">YAML</button>
                          {wf.workflow_type === 'pipeline' && (
                            <button type="button" className="workflows-action-btn" onClick={() => setEditingWorkflow(wf)} title="Edit pipeline steps">Edit</button>
                          )}
                          <button type="button" className="workflows-action-icon" onClick={() => handleDuplicate(wf)} title="Duplicate" aria-label="Duplicate workflow">
                            <svg width="14" height="14" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round"><rect x="5.5" y="5.5" width="9" height="9" rx="1.5" /><path d="M10.5 5.5V2.5a1 1 0 0 0-1-1h-7a1 1 0 0 0-1 1v7a1 1 0 0 0 1 1h3" /></svg>
                          </button>
                          <button type="button" className="workflows-action-icon" onClick={() => handleExport(wf)} title="Download YAML" aria-label="Download workflow as YAML">
                            <svg width="14" height="14" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round"><path d="M8 2v9m0 0L5 8m3 3 3-3M2.5 12.5v1a1 1 0 0 0 1 1h9a1 1 0 0 0 1-1v-1" /></svg>
                          </button>
                          <button type="button" className="workflows-action-icon workflows-action-icon--danger" onClick={() => handleDelete(wf)} title="Delete" aria-label="Delete workflow">
                            <svg width="14" height="14" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round"><path d="M2.5 4.5h11M5.5 4.5V3a1 1 0 0 1 1-1h3a1 1 0 0 1 1 1v1.5M6.5 7v4.5M9.5 7v4.5" /><path d="M3.5 4.5 4 13a1 1 0 0 0 1 1h6a1 1 0 0 0 1-1l.5-8.5" /></svg>
                          </button>
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

      {/* New pipeline from YAML modal */}
      {showImportModal && (
        <NewPipelineYamlModal
          onClose={() => setShowImportModal(false)}
          onImport={importYaml}
        />
      )}

      {/* YAML editor modal */}
      {yamlEditorWf && (
        <YamlEditorModal
          workflowName={yamlEditorWf.name}
          yamlContent={yamlContent}
          loading={yamlLoading}
          onChange={setYamlContent}
          onSave={handleYamlSave}
          onClose={() => setYamlEditorWf(null)}
        />
      )}

      {/* Pipeline editor sidebar */}
      <SidebarPanel
        isOpen={!!editingWorkflow}
        onClose={handleEditorClose}
        title={editingWorkflow?.name || 'Pipeline'}
        width={560}
        footer={
          <>
            <button className="pipeline-editor-btn" onClick={() => editingWorkflow && handleExport(editingWorkflow)} type="button">Export YAML</button>
            <button className="pipeline-editor-btn pipeline-editor-btn--primary" onClick={() => editorRef.current?.save()} type="button">Save</button>
          </>
        }
      >
        {editingWorkflow && (
          <PipelineEditor
            ref={editorRef}
            pipeline={editingWorkflow}
            onBack={handleEditorClose}
            updateWorkflow={updateWorkflow}
            onExport={() => handleExport(editingWorkflow)}
            inSidebar
          />
        )}
      </SidebarPanel>
    </>
  )
}

function NewPipelineYamlModal({ onClose, onImport }: {
  onClose: () => void
  onImport: (yaml: string) => Promise<WorkflowDetail | null>
}) {
  const [content, setContent] = useState(SCAFFOLD_PIPELINE_YAML)
  const [submitting, setSubmitting] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [isDirty, setIsDirty] = useState(false)

  const wrappedOnChange = useCallback((c: string) => { setIsDirty(true); setContent(c) }, [])

  const handleSubmit = async () => {
    if (!content.trim()) return
    setError(null)
    setSubmitting(true)
    try {
      const result = await onImport(content)
      if (result) onClose()
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Failed to create pipeline')
    } finally {
      setSubmitting(false)
    }
  }

  const handleClose = () => {
    if (isDirty && !window.confirm('You have unsaved changes. Discard them?')) return
    onClose()
  }

  return (
    <div className="workflows-modal-overlay" onClick={handleClose}>
      <div className="workflows-yaml-modal" onClick={e => e.stopPropagation()}>
        <div className="workflows-yaml-header">
          <h3>New Pipeline — YAML</h3>
          <div className="workflows-yaml-header-actions">
            {error && <span className="workflows-yaml-error">{error}</span>}
            <button type="button" className="workflows-modal-cancel" onClick={handleClose}>Cancel</button>
            <button
              type="button"
              className="workflows-modal-submit"
              onClick={handleSubmit}
              disabled={!content.trim() || submitting}
            >
              {submitting ? 'Creating...' : 'Create'}
            </button>
          </div>
        </div>
        <div className="workflows-yaml-editor">
          <CodeMirrorEditor
            content={content}
            language="yaml"
            onChange={wrappedOnChange}
            onSave={handleSubmit}
          />
        </div>
      </div>
    </div>
  )
}
