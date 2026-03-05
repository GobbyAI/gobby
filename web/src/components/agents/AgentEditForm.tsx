import { useState } from 'react'
import { SidebarPanel } from '../shared/SidebarPanel'
import { CodeMirrorEditor } from '../shared/CodeMirrorEditor'
import { AgentRulesEditor } from './AgentRulesEditor'
import { AgentVariablesEditor } from './AgentVariablesEditor'
import { AgentSkillsEditor } from './AgentSkillsEditor'
import { AgentStepsEditor } from './AgentStepsEditor'
import type { WorkflowStep } from './AgentStepsEditor'

export interface AgentFormData {
  name: string
  description: string
  role: string
  goal: string
  personality: string
  instructions: string
  provider: string
  model: string
  mode: string
  isolation: string
  base_branch: string
  timeout: number
  max_turns: number
  pipeline: string
}

export interface AgentItemForPanel {
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
      rule_selectors?: { include: string[]; exclude: string[] }
      variables?: Record<string, unknown>
      [key: string]: unknown
    } | null
    lifecycle_variables: Record<string, unknown>
    default_variables: Record<string, unknown>
    steps?: WorkflowStep[] | null
    step_variables?: Record<string, unknown> | null
    exit_condition?: string | null
  }
  source: string
  source_path: string | null
  db_id: string | null
}

interface RuleSelectors {
  include: string[]
  exclude: string[]
}

interface AgentEditFormProps {
  isOpen: boolean
  readOnly?: boolean
  agentItem?: AgentItemForPanel | null
  form: AgentFormData
  onChange: (form: AgentFormData) => void
  onSave: () => void
  onCancel: () => void
  isEditing: boolean
  providerModels: Record<string, { value: string; label: string }[]>
  saveDisabled?: boolean
  editingId?: string | null
  branches?: string[]
  isGitProject?: boolean
  projectId?: string
  rules?: string[]
  onRulesChange?: (rules: string[]) => void
  ruleSelectors?: RuleSelectors | null
  onRuleSelectorsChange?: (selectors: RuleSelectors) => void
  variables?: Record<string, unknown>
  onVariablesChange?: (variables: Record<string, unknown>) => void
  sidebarView?: 'form' | 'yaml'
  onViewChange?: (view: 'form' | 'yaml') => void
  yamlContent?: string
  onYamlChange?: (content: string) => void
  onYamlSave?: () => void
  pipelines?: { id: string; name: string }[]
  editSkills?: string[]
  onSkillsChange?: (skills: string[]) => void
  steps?: WorkflowStep[]
  onStepsChange?: (steps: WorkflowStep[]) => void
}

function FormInput({ label, value, onChange, placeholder, required }: {
  label: string; value: string; onChange: (v: string) => void; placeholder?: string; required?: boolean
}) {
  return (
    <label className="agent-edit-field">
      <span className="agent-edit-label">{label}{required ? ' *' : ''}</span>
      <input
        className="agent-edit-input"
        value={value}
        onChange={e => onChange(e.target.value)}
        placeholder={placeholder}
      />
    </label>
  )
}

function FormTextarea({ label, value, onChange, placeholder, rows = 3 }: {
  label: string; value: string; onChange: (v: string) => void; placeholder?: string; rows?: number
}) {
  return (
    <label className="agent-edit-field">
      <span className="agent-edit-label">{label}</span>
      <textarea
        className="agent-edit-input agent-edit-textarea"
        value={value}
        onChange={e => onChange(e.target.value)}
        placeholder={placeholder}
        rows={rows}
        style={{ resize: 'vertical' }}
      />
    </label>
  )
}

function MetaRow({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className="agent-edit-meta-row">
      <span className="agent-edit-meta-label">{label}</span>
      <div className="agent-edit-meta-value">{children}</div>
    </div>
  )
}

const MODE_COLORS: Record<string, string> = {
  terminal: '#f59e0b',
  headless: '#6b7280',
  embedded: '#06b6d4',
  self: '#ec4899',
}

export function AgentEditForm({
  isOpen, readOnly, agentItem,
  form, onChange, onSave, onCancel, isEditing, providerModels, saveDisabled,
  editingId, branches = [], isGitProject = true, projectId,
  rules, onRulesChange, ruleSelectors, onRuleSelectorsChange,
  variables, onVariablesChange,
  sidebarView: sidebarViewProp, onViewChange,
  yamlContent, onYamlChange, onYamlSave,
  pipelines,
  editSkills, onSkillsChange,
  steps, onStepsChange,
}: AgentEditFormProps) {
  const [customModelInput, setCustomModelInput] = useState(false)
  const [customBranchInput, setCustomBranchInput] = useState(false)

  const view = sidebarViewProp ?? 'form'

  const isInheritProvider = form.provider === 'inherit'
  const models = isInheritProvider ? [{ value: '', label: '(default)' }] : providerModels[form.provider]
  const isKnown = models?.some(m => m.value === form.model)
  const showCustomModel = !isInheritProvider && (customModelInput || !models || (!isKnown && form.model !== ''))

  const branchKnown = form.base_branch === 'inherit' || branches.includes(form.base_branch)
  const showCustomBranch = isGitProject && (customBranchInput || (!branchKnown && form.base_branch !== ''))

  const set = <K extends keyof AgentFormData>(key: K, value: AgentFormData[K]) =>
    onChange({ ...form, [key]: value })

  const rd = agentItem?.definition
  const wfMeta = ['rules', 'variables', 'pipeline', 'rule_selectors']
  const workflowEntries = rd?.workflows
    ? Object.entries(rd.workflows).filter(([k]) => !wfMeta.includes(k) && typeof rd.workflows![k] === 'object' && rd.workflows![k] !== null && !Array.isArray(rd.workflows![k]))
    : []

  const title = readOnly ? (rd?.name || 'Agent') : (isEditing ? 'Edit Agent' : 'Create Agent')

  const headerContent = (
    <>
      {onViewChange && (
        <div className="sidebar-tab-bar">
          <button
            type="button"
            className={`sidebar-tab ${view !== 'yaml' ? 'sidebar-tab--active' : ''}`}
            onClick={() => onViewChange('form')}
          >
            Form
          </button>
          <button
            type="button"
            className={`sidebar-tab ${view === 'yaml' ? 'sidebar-tab--active' : ''}`}
            onClick={() => onViewChange('yaml')}
          >
            YAML
          </button>
        </div>
      )}
    </>
  )

  const footer = !readOnly ? (
    <>
      <button className="agent-defs-btn" onClick={onCancel} type="button">Cancel</button>
      <button
        className="agent-defs-btn agent-defs-btn--primary"
        onClick={view === 'yaml' && onYamlSave ? onYamlSave : onSave}
        disabled={saveDisabled}
        type="button"
      >
        {isEditing ? 'Save' : 'Create'}
      </button>
    </>
  ) : undefined

  return (
    <SidebarPanel
      isOpen={isOpen}
      onClose={onCancel}
      title={title}
      headerContent={headerContent}
      footer={footer}
    >
      {view === 'yaml' ? (
        <div className="agent-edit-yaml-view">
          <CodeMirrorEditor
            content={yamlContent || ''}
            language="yaml"
            readOnly={readOnly}
            onChange={onYamlChange}
            onSave={!readOnly ? onYamlSave : undefined}
          />
        </div>
      ) : readOnly && rd ? (
        <>
          <div className="agent-edit-meta">
            <MetaRow label="Provider"><span>{rd.provider}</span></MetaRow>
            <MetaRow label="Model"><span>{rd.model || '(default)'}</span></MetaRow>
            <MetaRow label="Mode"><span>{rd.mode}</span></MetaRow>
            <MetaRow label="Isolation"><span>{rd.isolation || 'none'}</span></MetaRow>
            <MetaRow label="Base branch"><span>{rd.base_branch}</span></MetaRow>
            <MetaRow label="Timeout"><span>{rd.timeout}s</span></MetaRow>
            <MetaRow label="Max turns"><span>{String(rd.max_turns)}</span></MetaRow>
            {rd.default_workflow && (
              <MetaRow label="Default workflow"><span>{rd.default_workflow}</span></MetaRow>
            )}
            {rd.workflows?.pipeline && (
              <MetaRow label="Pipeline"><span>{rd.workflows.pipeline}</span></MetaRow>
            )}
          </div>

          {rd.description && (
            <div className="agent-edit-section">
              <h4 className="agent-edit-section-title">Description</h4>
              <pre className="agent-def-description-full">{rd.description}</pre>
            </div>
          )}
          {rd.role && (
            <div className="agent-edit-section">
              <h4 className="agent-edit-section-title">Role</h4>
              <pre className="agent-def-description-full">{rd.role}</pre>
            </div>
          )}
          {rd.goal && (
            <div className="agent-edit-section">
              <h4 className="agent-edit-section-title">Goal</h4>
              <pre className="agent-def-description-full">{rd.goal}</pre>
            </div>
          )}
          {rd.personality && (
            <div className="agent-edit-section">
              <h4 className="agent-edit-section-title">Personality</h4>
              <pre className="agent-def-description-full">{rd.personality}</pre>
            </div>
          )}
          {rd.instructions && (
            <div className="agent-edit-section">
              <h4 className="agent-edit-section-title">Instructions</h4>
              <pre className="agent-def-description-full">{rd.instructions}</pre>
            </div>
          )}

          {workflowEntries.length > 0 && (
            <div className="agent-edit-section">
              <h4 className="agent-edit-section-title">Workflows</h4>
              <div className="agent-def-workflow-list">
                {workflowEntries.map(([wfName, wfRaw]) => {
                  const wf = wfRaw as { type?: string; file?: string; mode?: string; internal?: boolean; step_count?: number; description?: string }
                  return (
                    <div key={wfName} className="agent-def-workflow-item">
                      <span className="agent-def-workflow-name">{wfName}</span>
                      {wf.type && <span className="agent-def-badge agent-def-badge--dim">{wf.type}</span>}
                      {wf.file && <span className="agent-def-badge agent-def-badge--dim">{wf.file}</span>}
                      {wf.mode && <span className="agent-def-badge agent-def-badge--filled" style={{ background: MODE_COLORS[wf.mode] || '#666' }}>{wf.mode}</span>}
                      {wf.internal && <span className="agent-def-badge agent-def-badge--dim">internal</span>}
                      {wf.step_count != null && <span className="agent-def-badge agent-def-badge--dim">{wf.step_count} steps</span>}
                      {wf.description && <span className="agent-def-workflow-desc">{wf.description}</span>}
                    </div>
                  )
                })}
              </div>
            </div>
          )}

          {rd.workflows?.rules && rd.workflows.rules.length > 0 && (
            <div className="agent-edit-section">
              <h4 className="agent-edit-section-title">Rules</h4>
              <div className="agent-rules-chips">
                {(rd.workflows.rules as string[]).map(name => (
                  <span key={name} className="agent-rules-chip">{name}</span>
                ))}
              </div>
            </div>
          )}

          {rd.workflows?.rule_selectors && (
            <div className="agent-edit-section">
              <h4 className="agent-edit-section-title">Rule Selectors</h4>
              {(() => {
                const rs = rd.workflows!.rule_selectors as { include?: string[]; exclude?: string[] }
                return (
                  <>
                    {rs.include && rs.include.length > 0 && (
                      <div>
                        <span className="agent-edit-label">Include</span>
                        <div className="agent-rules-chips" style={{ marginTop: 4 }}>
                          {rs.include.map(s => (
                            <span key={s} className="agent-rules-chip agent-rules-chip--selector agent-rules-chip--include">{s}</span>
                          ))}
                        </div>
                      </div>
                    )}
                    {rs.exclude && rs.exclude.length > 0 && (
                      <div style={{ marginTop: 6 }}>
                        <span className="agent-edit-label">Exclude</span>
                        <div className="agent-rules-chips" style={{ marginTop: 4 }}>
                          {rs.exclude.map(s => (
                            <span key={s} className="agent-rules-chip agent-rules-chip--selector agent-rules-chip--exclude">{s}</span>
                          ))}
                        </div>
                      </div>
                    )}
                  </>
                )
              })()}
            </div>
          )}

          {rd.workflows?.variables && Object.keys(rd.workflows.variables as Record<string, unknown>).length > 0 && (
            <div className="agent-edit-section">
              <h4 className="agent-edit-section-title">Variables</h4>
              <div className="agent-vars-list">
                {Object.entries(rd.workflows!.variables as Record<string, unknown>).map(([key, val]) => (
                  <div key={key} className="agent-vars-row">
                    <code className="agent-vars-key">{key}</code>
                    <span className="agent-vars-value">{typeof val === 'string' ? val : JSON.stringify(val)}</span>
                  </div>
                ))}
              </div>
            </div>
          )}

          {rd.steps && rd.steps.length > 0 && (
            <div className="agent-edit-section">
              <h4 className="agent-edit-section-title">Steps ({rd.steps.length})</h4>
              <div className="step-readonly-list">
                {rd.steps.map((s, i) => (
                  <div key={i} className="step-readonly-item">
                    <span className="step-name-badge">{s.name}</span>
                    <span className="step-readonly-summary">
                      {s.description || ''}
                      {s.transitions && s.transitions.length > 0 ? ` \u2192 ${s.transitions.map(t => t.to).join(', ')}` : ''}
                    </span>
                  </div>
                ))}
              </div>
            </div>
          )}

          {rd.sandbox && (
            <div className="agent-edit-section">
              <h4 className="agent-edit-section-title">Sandbox</h4>
              <pre className="agent-def-json">{JSON.stringify(rd.sandbox, null, 2)}</pre>
            </div>
          )}
          {rd.skill_profile && (
            <div className="agent-edit-section">
              <h4 className="agent-edit-section-title">Skill Profile</h4>
              <pre className="agent-def-json">{JSON.stringify(rd.skill_profile, null, 2)}</pre>
            </div>
          )}

          <div className="agent-edit-section">
            <h4 className="agent-edit-section-title">Source</h4>
            <div className="agent-def-source-info">
              {agentItem.source_path ? (
                <code>{agentItem.source_path}</code>
              ) : (
                <span>Database ({agentItem.source}){agentItem.db_id ? ` \u2014 ${agentItem.db_id.slice(0, 8)}` : ''}</span>
              )}
            </div>
          </div>
        </>
      ) : (
        <>
          {/* Name */}
          <div className="agent-edit-section">
            <FormInput label="Name" value={form.name} onChange={v => set('name', v)} placeholder="my-agent" required />
          </div>

          {/* Editable meta */}
          <div className="agent-edit-meta">
            <MetaRow label="Provider">
              <select className="agent-edit-input" value={form.provider} onChange={e => {
                const v = e.target.value
                if (v === 'inherit') {
                  setCustomModelInput(false)
                  onChange({ ...form, provider: v, model: '' })
                } else {
                  const newModels = providerModels[v]
                  const valid = newModels?.some(m => m.value === form.model)
                  setCustomModelInput(false)
                  onChange({ ...form, provider: v, model: valid ? form.model : '' })
                }
              }}>
                <option value="inherit">(default)</option>
                <option value="claude">Claude</option>
                <option value="gemini">Gemini</option>
                <option value="codex">Codex</option>
                <option value="cursor">Cursor</option>
              </select>
            </MetaRow>

            <MetaRow label="Model">
              {showCustomModel ? (
                <div className="agent-edit-model-field">
                  <input
                    className="agent-edit-input"
                    value={form.model}
                    onChange={e => set('model', e.target.value)}
                    placeholder="e.g. claude-sonnet-4-5-20250929"
                    autoFocus={customModelInput}
                  />
                  {models && (
                    <button type="button" className="agent-edit-model-toggle" onClick={() => { setCustomModelInput(false); set('model', '') }}>&times;</button>
                  )}
                </div>
              ) : (
                <select className="agent-edit-input" value={form.model} onChange={e => {
                  if (e.target.value === '__custom__') { setCustomModelInput(true); set('model', '') }
                  else set('model', e.target.value)
                }}>
                  {models?.map(m => <option key={m.value} value={m.value}>{m.label}</option>)}
                  {!isInheritProvider && <option value="__custom__">Custom...</option>}
                </select>
              )}
            </MetaRow>

            <MetaRow label="Mode">
              <select className="agent-edit-input" value={form.mode} onChange={e => set('mode', e.target.value)}>
                <option value="inherit">(default)</option>
                <option value="self">Self</option>
                <option value="terminal">Terminal</option>
                <option value="embedded">Embedded</option>
                <option value="headless">Headless</option>
              </select>
            </MetaRow>

            <MetaRow label="Isolation">
              <select
                className="agent-edit-input"
                value={isGitProject ? form.isolation : 'inherit'}
                onChange={e => set('isolation', e.target.value)}
                disabled={!isGitProject}
              >
                <option value="inherit">(default)</option>
                <option value="none">None</option>
                <option value="worktree">Worktree</option>
                <option value="clone">Clone</option>
              </select>
            </MetaRow>

            <MetaRow label="Base branch">
              {!isGitProject ? (
                <select className="agent-edit-input" disabled value="inherit">
                  <option value="inherit">(default)</option>
                </select>
              ) : showCustomBranch ? (
                <div className="agent-edit-model-field">
                  <input
                    className="agent-edit-input"
                    value={form.base_branch}
                    onChange={e => set('base_branch', e.target.value)}
                    placeholder="branch name"
                    autoFocus={customBranchInput}
                  />
                  <button type="button" className="agent-edit-model-toggle" onClick={() => { setCustomBranchInput(false); set('base_branch', 'inherit') }}>&times;</button>
                </div>
              ) : (
                <select className="agent-edit-input" value={form.base_branch} onChange={e => {
                  if (e.target.value === '__custom__') { setCustomBranchInput(true); set('base_branch', '') }
                  else set('base_branch', e.target.value)
                }}>
                  <option value="inherit">(default)</option>
                  {branches.map(b => <option key={b} value={b}>{b}</option>)}
                  <option value="__custom__">Custom...</option>
                </select>
              )}
            </MetaRow>

            {pipelines && (
              <MetaRow label="Pipeline">
                <select className="agent-edit-input" value={form.pipeline} onChange={e => set('pipeline', e.target.value)}>
                  <option value="">(none)</option>
                  {pipelines.map(p => <option key={p.id} value={p.name}>{p.name}</option>)}
                </select>
              </MetaRow>
            )}

            <MetaRow label="Timeout (s)">
              <input
                className="agent-edit-input"
                type="number"
                min={0}
                value={form.timeout}
                onChange={e => set('timeout', Number(e.target.value))}
              />
            </MetaRow>

            <MetaRow label="Max turns">
              <input
                className="agent-edit-input"
                type="number"
                min={0}
                value={form.max_turns}
                onChange={e => set('max_turns', Number(e.target.value))}
              />
            </MetaRow>
          </div>

          {/* Identity */}
          <div className="agent-edit-section">
            <h4 className="agent-edit-section-title">Identity</h4>
            <FormTextarea label="Description" value={form.description} onChange={v => set('description', v)} placeholder="What this agent does..." />
            <FormTextarea label="Role" value={form.role} onChange={v => set('role', v)} placeholder="e.g. Senior security engineer" />
            <FormTextarea label="Goal" value={form.goal} onChange={v => set('goal', v)} placeholder="What success looks like..." />
            <FormTextarea label="Personality" value={form.personality} onChange={v => set('personality', v)} placeholder="Communication style, tone..." />
          </div>

          {/* Instructions */}
          <div className="agent-edit-section">
            <h4 className="agent-edit-section-title">Instructions</h4>
            <div className="agent-edit-codemirror">
              <CodeMirrorEditor
                content={form.instructions}
                language="markdown"
                onChange={v => set('instructions', v)}
              />
            </div>
          </div>

          {/* Rules */}
          {onRulesChange && rules !== undefined && (
            <div className="agent-edit-section">
              <h4 className="agent-edit-section-title">Rules</h4>
              <AgentRulesEditor
                definitionId={editingId}
                rules={rules}
                onRulesChange={onRulesChange}
                projectId={projectId}
                ruleSelectors={ruleSelectors}
                onRuleSelectorsChange={onRuleSelectorsChange}
              />
            </div>
          )}

          {/* Skills */}
          {onSkillsChange && editSkills !== undefined && (
            <div className="agent-edit-section">
              <h4 className="agent-edit-section-title">Skills</h4>
              <AgentSkillsEditor
                skills={editSkills}
                onSkillsChange={onSkillsChange}
                projectId={projectId}
              />
            </div>
          )}

          {/* Variables */}
          {onVariablesChange && variables !== undefined && (
            <div className="agent-edit-section">
              <h4 className="agent-edit-section-title">Variables</h4>
              <AgentVariablesEditor
                definitionId={editingId}
                variables={variables}
                onVariablesChange={onVariablesChange}
              />
            </div>
          )}

          {/* Steps */}
          {onStepsChange && steps !== undefined && (
            <div className="agent-edit-section">
              <h4 className="agent-edit-section-title">Steps</h4>
              <AgentStepsEditor
                steps={steps}
                onChange={onStepsChange}
              />
            </div>
          )}
        </>
      )}
    </SidebarPanel>
  )
}
