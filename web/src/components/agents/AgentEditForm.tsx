import { useId, useState } from 'react'
import { SidebarPanel } from '../shared/SidebarPanel'
import { CodeMirrorEditor } from '../shared/CodeMirrorEditor'
import { Heading } from '../shared/Heading'
import { Button } from '../ui/Button'
import { Card } from '../ui/Card'
import { Chip } from '../ui/Chip'
import { FormField } from '../ui/FormField'
import { Input } from '../ui/Input'
import { NativeSelect } from '../ui/NativeSelect'
import { TabBar } from '../ui/TabBar'
import { Textarea } from '../ui/Textarea'
import { coarseHitAreaCls } from '../ui/controlStyles'
import { AgentRulesEditor } from './AgentRulesEditor'
import { AgentVariablesEditor } from './AgentVariablesEditor'
import { AgentSkillsEditor } from './AgentSkillsEditor'
import { AgentStepsEditor } from './AgentStepsEditor'
import { AgentToolBlocksEditor } from './AgentToolBlocksEditor'
import type { WorkflowStep } from './AgentStepsEditor'
import {
  AUTO_REASONING_EFFORT,
  getModelsForProvider,
  getOrderedProviders,
  getReasoningOptionsForModel,
  type ProviderModelEntry,
} from '../../lib/providerModels'

export interface AgentFormData {
  name: string
  description: string
  surfaces: string[]
  role: string
  goal: string
  personality: string
  instructions: string
  provider: string
  model: string
  reasoning_effort: string
  reasoning_required: boolean
  mode: string
  isolation: string
  base_branch: string
  timeout: number
  pipeline: string
  fallback_agent: string
}

export interface AgentItemForPanel {
  definition: {
    name: string
    description: string | null
    surfaces?: string[] | null
    role: string | null
    goal: string | null
    personality: string | null
    instructions: string | null
    provider: string
    model: string | null
    reasoning_effort?: string | null
    reasoning_required?: boolean | null
    fallback_agent: string | null
    mode: string
    isolation: string | null
    base_branch: string
    timeout: number
    default_workflow: string | null
    sandbox: Record<string, unknown> | null
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
    blocked_tools?: string[] | null
    blocked_mcp_tools?: string[] | null
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
  providerCatalog: ProviderModelEntry[]
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
  blockedTools?: string[]
  onBlockedToolsChange?: (tools: string[]) => void
  blockedMcpTools?: string[]
  onBlockedMcpToolsChange?: (tools: string[]) => void
  agentNames?: string[]
}

const FALLBACK_PROVIDER_OPTIONS = ['claude', 'codex', 'qwen', 'droid']

function FormInput({ label, value, onChange, placeholder, required }: {
  label: string; value: string; onChange: (v: string) => void; placeholder?: string; required?: boolean
}) {
  return (
    <FormField label={`${label}${required ? ' *' : ''}`}>
      {({ id, describedBy, invalid }) => (
        <Input
          id={id}
          aria-describedby={describedBy}
          error={invalid}
          value={value}
          onChange={(event) => onChange(event.target.value)}
          placeholder={placeholder}
          required={required}
        />
      )}
    </FormField>
  )
}

function FormTextarea({ label, value, onChange, placeholder, rows = 3 }: {
  label: string; value: string; onChange: (v: string) => void; placeholder?: string; rows?: number
}) {
  return (
    <FormField label={label}>
      {({ id, describedBy, invalid }) => (
        <Textarea
          id={id}
          className="resize-y font-[inherit] leading-[1.4]"
          aria-describedby={describedBy}
          error={invalid}
          value={value}
          onChange={(event) => onChange(event.target.value)}
          placeholder={placeholder}
          rows={rows}
        />
      )}
    </FormField>
  )
}

function MetaRow({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className="flex items-center justify-between py-1 text-sm">
      <span className="mr-3 shrink-0 text-[var(--text-muted)]">{label}</span>
      <div className="max-w-55 flex-1 text-right [&_input[type=number]]:w-full [&_select]:w-full">
        {children}
      </div>
    </div>
  )
}


export function AgentEditForm({
  isOpen, readOnly, agentItem,
  form, onChange, onSave, onCancel, isEditing, providerCatalog, saveDisabled,
  editingId, branches = [], isGitProject = true, projectId,
  rules, onRulesChange, ruleSelectors, onRuleSelectorsChange,
  variables, onVariablesChange,
  sidebarView: sidebarViewProp, onViewChange,
  yamlContent, onYamlChange, onYamlSave,
  pipelines,
  editSkills, onSkillsChange,
  steps, onStepsChange,
  blockedTools, onBlockedToolsChange,
  blockedMcpTools, onBlockedMcpToolsChange,
  agentNames = [],
}: AgentEditFormProps) {
  const checkboxIdPrefix = useId()
  const [customModelInput, setCustomModelInput] = useState(false)
  const [customBranchInput, setCustomBranchInput] = useState(false)

  const view = sidebarViewProp ?? 'form'

  const isInheritProvider = form.provider === 'inherit'
  const providerOptions = (() => {
    const discovered = getOrderedProviders(providerCatalog.map(entry => entry.provider))
    return discovered.length > 0 ? discovered : FALLBACK_PROVIDER_OPTIONS
  })()
  const models = isInheritProvider
    ? [{ value: '', label: '(default)' }]
    : [{ value: '', label: '(default)' }, ...getModelsForProvider(providerCatalog, form.provider)]
  const isKnown = models?.some(m => m.value === form.model)
  const showCustomModel = !isInheritProvider && (customModelInput || !models || (!isKnown && form.model !== ''))
  const reasoningOptions = isInheritProvider
    ? [{ value: AUTO_REASONING_EFFORT, label: 'Auto', disabled: true }]
    : getReasoningOptionsForModel(providerCatalog, form.provider, form.model)
  const reasoningDisabled = reasoningOptions.length === 1 && Boolean(reasoningOptions[0]?.disabled)
  const resolveReasoningEffort = (provider: string, model: string, currentReasoning: string): string => {
    if (provider === 'inherit') {
      return AUTO_REASONING_EFFORT
    }
    const options = getReasoningOptionsForModel(providerCatalog, provider, model)
    return options.some(option => option.value === currentReasoning)
      ? currentReasoning
      : AUTO_REASONING_EFFORT
  }

  const branchKnown = form.base_branch === 'inherit' || branches.includes(form.base_branch)
  const showCustomBranch = isGitProject && (customBranchInput || (!branchKnown && form.base_branch !== ''))

  const set = <K extends keyof AgentFormData>(key: K, value: AgentFormData[K]) =>
    onChange({ ...form, [key]: value })

  const toggleSurface = (surface: string) => {
    const next = form.surfaces.includes(surface)
      ? form.surfaces.filter(item => item !== surface)
      : [...form.surfaces, surface]
    onChange({ ...form, surfaces: next.length > 0 ? next : ['spawn'] })
  }

  const rd = agentItem?.definition
  const wfMeta = ['rules', 'variables', 'pipeline', 'rule_selectors']
  const workflowEntries = rd?.workflows
    ? Object.entries(rd.workflows).filter(([k]) => !wfMeta.includes(k) && typeof rd.workflows![k] === 'object' && rd.workflows![k] !== null && !Array.isArray(rd.workflows![k]))
    : []

  const title = readOnly ? (rd?.name || 'Agent') : (isEditing ? 'Edit Agent' : 'Create Agent')

  const headerContent = (
    <>
      {onViewChange && (
        <TabBar
          tabs={[
            { id: 'form', label: 'Form' },
            { id: 'yaml', label: 'YAML' },
          ]}
          activeTab={view}
          onTabChange={(tabId) => onViewChange(tabId === 'yaml' ? 'yaml' : 'form')}
          ariaLabel="Agent editor view"
          className="mb-0"
        />
      )}
    </>
  )

  const footer = !readOnly ? (
    <>
      <Button
        className={coarseHitAreaCls}
        onClick={onCancel}
        type="button"
      >
        Cancel
      </Button>
      <Button
        variant="primary"
        className={coarseHitAreaCls}
        onClick={view === 'yaml' && onYamlSave ? onYamlSave : onSave}
        disabled={saveDisabled}
        type="button"
      >
        {isEditing ? 'Save' : 'Create'}
      </Button>
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
        <div className="h-full [&_.codemirror-container]:h-full">
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
          <div className="border-b border-border px-5 py-3">
            <MetaRow label="Provider"><span>{rd.provider}</span></MetaRow>
            <MetaRow label="Model"><span>{rd.model || '(default)'}</span></MetaRow>
            <MetaRow label="Reasoning"><span>{rd.reasoning_effort || 'Auto'}</span></MetaRow>
            <MetaRow label="Require reasoning"><span>{rd.reasoning_required ? 'Yes' : 'No'}</span></MetaRow>
            {rd.fallback_agent && (
              <MetaRow label="Fallback"><span>{rd.fallback_agent}</span></MetaRow>
            )}
            <MetaRow label="Mode"><span>{rd.mode}</span></MetaRow>
            <MetaRow label="Isolation"><span>{rd.isolation || 'none'}</span></MetaRow>
            <MetaRow label="Base branch"><span>{rd.base_branch}</span></MetaRow>
            <MetaRow label="Timeout"><span>{rd.timeout}s</span></MetaRow>
            {rd.default_workflow && (
              <MetaRow label="Default workflow"><span>{rd.default_workflow}</span></MetaRow>
            )}
            {rd.workflows?.pipeline && (
              <MetaRow label="Pipeline"><span>{rd.workflows.pipeline}</span></MetaRow>
            )}
          </div>

          {rd.description && (
            <div className="flex flex-col gap-1.5 border-b border-border px-5 py-3">
              <Heading level={4} className="mt-0 mb-1 text-sm font-semibold uppercase tracking-wider text-[var(--text-muted)]">Description</Heading>
              <pre className="m-0 whitespace-pre-wrap font-sans text-sm leading-relaxed text-[var(--text-secondary)]">{rd.description}</pre>
            </div>
          )}
          {rd.role && (
            <div className="flex flex-col gap-1.5 border-b border-border px-5 py-3">
              <Heading level={4} className="mt-0 mb-1 text-sm font-semibold uppercase tracking-wider text-[var(--text-muted)]">Role</Heading>
              <pre className="m-0 whitespace-pre-wrap font-sans text-sm leading-relaxed text-[var(--text-secondary)]">{rd.role}</pre>
            </div>
          )}
          {rd.goal && (
            <div className="flex flex-col gap-1.5 border-b border-border px-5 py-3">
              <Heading level={4} className="mt-0 mb-1 text-sm font-semibold uppercase tracking-wider text-[var(--text-muted)]">Goal</Heading>
              <pre className="m-0 whitespace-pre-wrap font-sans text-sm leading-relaxed text-[var(--text-secondary)]">{rd.goal}</pre>
            </div>
          )}
          {rd.personality && (
            <div className="flex flex-col gap-1.5 border-b border-border px-5 py-3">
              <Heading level={4} className="mt-0 mb-1 text-sm font-semibold uppercase tracking-wider text-[var(--text-muted)]">Personality</Heading>
              <pre className="m-0 whitespace-pre-wrap font-sans text-sm leading-relaxed text-[var(--text-secondary)]">{rd.personality}</pre>
            </div>
          )}
          {rd.instructions && (
            <div className="flex flex-col gap-1.5 border-b border-border px-5 py-3">
              <Heading level={4} className="mt-0 mb-1 text-sm font-semibold uppercase tracking-wider text-[var(--text-muted)]">Instructions</Heading>
              <pre className="m-0 whitespace-pre-wrap font-sans text-sm leading-relaxed text-[var(--text-secondary)]">{rd.instructions}</pre>
            </div>
          )}

          {workflowEntries.length > 0 && (
            <div className="flex flex-col gap-1.5 border-b border-border px-5 py-3">
              <Heading level={4} className="mt-0 mb-1 text-sm font-semibold uppercase tracking-wider text-[var(--text-muted)]">Workflows</Heading>
              <div className="flex flex-col gap-1.5">
                {workflowEntries.map(([wfName, wfRaw]) => {
                  const wf = wfRaw as { type?: string; file?: string; mode?: string; internal?: boolean; step_count?: number; description?: string }
                  return (
                    <div key={wfName} className="flex flex-wrap items-center gap-1.5 text-sm">
                      <span className="font-[inherit] font-semibold text-[var(--text-primary)]">{wfName}</span>
                      {wf.type && <Chip>{wf.type}</Chip>}
                      {wf.file && <Chip>{wf.file}</Chip>}
                      {wf.internal && <Chip>internal</Chip>}
                      {wf.step_count != null && <Chip>{wf.step_count} steps</Chip>}
                      {wf.description && <span className="basis-full text-xs text-[var(--text-muted)]">{wf.description}</span>}
                    </div>
                  )
                })}
              </div>
            </div>
          )}

          {rd.workflows?.rules && rd.workflows.rules.length > 0 && (
            <div className="flex flex-col gap-1.5 border-b border-border px-5 py-3">
              <Heading level={4} className="mt-0 mb-1 text-sm font-semibold uppercase tracking-wider text-[var(--text-muted)]">Rules</Heading>
              <div className="flex flex-wrap items-center gap-1.5">
                {(rd.workflows.rules as string[]).map(name => (
                  <Chip key={name} className="border border-border text-sm">{name}</Chip>
                ))}
              </div>
            </div>
          )}

          {rd.workflows?.rule_selectors && (
            <div className="flex flex-col gap-1.5 border-b border-border px-5 py-3">
              <Heading level={4} className="mt-0 mb-1 text-sm font-semibold uppercase tracking-wider text-[var(--text-muted)]">Rule Selectors</Heading>
              {(() => {
                const rs = rd.workflows!.rule_selectors as { include?: string[]; exclude?: string[] }
                return (
                  <>
                    {rs.include && rs.include.length > 0 && (
                      <div>
                        <span className="text-xs uppercase tracking-[0.3px] text-[var(--text-muted)]">Include</span>
                        <div className="mt-1 flex flex-wrap items-center gap-1.5">
                          {rs.include.map(s => (
                            <Chip key={s} tone="info" className="border border-dashed border-[var(--color-info)] text-sm">{s}</Chip>
                          ))}
                        </div>
                      </div>
                    )}
                    {rs.exclude && rs.exclude.length > 0 && (
                      <div className="mt-1.5">
                        <span className="text-xs uppercase tracking-[0.3px] text-[var(--text-muted)]">Exclude</span>
                        <div className="mt-1 flex flex-wrap items-center gap-1.5">
                          {rs.exclude.map(s => (
                            <Chip key={s} tone="error" className="border border-dashed border-[var(--color-error)] text-sm">{s}</Chip>
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
            <div className="flex flex-col gap-1.5 border-b border-border px-5 py-3">
              <Heading level={4} className="mt-0 mb-1 text-sm font-semibold uppercase tracking-wider text-[var(--text-muted)]">Variables</Heading>
              <div className="flex flex-col gap-1">
                {Object.entries(rd.workflows!.variables as Record<string, unknown>).map(([key, val]) => (
                  <div key={key} className="flex items-center gap-2 text-sm">
                    <code className="font-semibold text-[var(--text-primary)] min-w-[80px]">{key}</code>
                    <span className="text-[var(--text-muted)] flex-1 overflow-hidden text-ellipsis whitespace-nowrap">{typeof val === 'string' ? val : JSON.stringify(val)}</span>
                  </div>
                ))}
              </div>
            </div>
          )}

          {((rd.blocked_tools && rd.blocked_tools.length > 0) || (rd.blocked_mcp_tools && rd.blocked_mcp_tools.length > 0)) && (
            <div className="flex flex-col gap-1.5 border-b border-border px-5 py-3">
              <Heading level={4} className="mt-0 mb-1 text-sm font-semibold uppercase tracking-wider text-[var(--text-muted)]">Tool Restrictions</Heading>
              {rd.blocked_tools && rd.blocked_tools.length > 0 && (
                <div className="flex flex-col gap-1">
                  <span className="text-xs uppercase tracking-[0.3px] text-[var(--text-muted)]">Blocked Tools</span>
                  <div className="flex flex-wrap gap-1">
                    {rd.blocked_tools.map(t => <Chip key={t} className="border border-border text-xs">{t}</Chip>)}
                  </div>
                </div>
              )}
              {rd.blocked_mcp_tools && rd.blocked_mcp_tools.length > 0 && (
                <div className="flex flex-col gap-1">
                  <span className="text-xs uppercase tracking-[0.3px] text-[var(--text-muted)]">Blocked MCP Tools</span>
                  <div className="flex flex-wrap gap-1">
                    {rd.blocked_mcp_tools.map(t => <Chip key={t} className="border border-border text-xs">{t}</Chip>)}
                  </div>
                </div>
              )}
            </div>
          )}

          {rd.steps && rd.steps.length > 0 && (
            <div className="flex flex-col gap-1.5 border-b border-border px-5 py-3">
              <Heading level={4} className="mt-0 mb-1 text-sm font-semibold uppercase tracking-wider text-[var(--text-muted)]">Steps ({rd.steps.length})</Heading>
              <div className="flex flex-col gap-1">
                {rd.steps.map((s, i) => (
                  <Card key={i} padding="sm" className="flex items-center gap-2 text-sm">
                    <Chip tone="accent">{s.name}</Chip>
                    <span className="text-xs text-[var(--text-muted)]">
                      {s.description || ''}
                      {s.transitions && s.transitions.length > 0 ? ` \u2192 ${s.transitions.map(t => t.to).join(', ')}` : ''}
                    </span>
                  </Card>
                ))}
              </div>
            </div>
          )}

          {rd.sandbox && (
            <div className="flex flex-col gap-1.5 border-b border-border px-5 py-3">
              <Heading level={4} className="mt-0 mb-1 text-sm font-semibold uppercase tracking-wider text-[var(--text-muted)]">Sandbox</Heading>
              <pre className="m-0 overflow-x-auto rounded border border-border bg-[var(--bg-primary)] p-2 font-[inherit] text-xs text-[var(--text-secondary)]">{JSON.stringify(rd.sandbox, null, 2)}</pre>
            </div>
          )}
          <div className="flex flex-col gap-1.5 border-b border-border px-5 py-3">
            <Heading level={4} className="mt-0 mb-1 text-sm font-semibold uppercase tracking-wider text-[var(--text-muted)]">Source</Heading>
            <div className="text-sm text-[var(--text-secondary)] [&_code]:break-all [&_code]:font-[inherit] [&_code]:text-xs [&_code]:text-[var(--text-muted)]">
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
          <div className="flex flex-col gap-1.5 border-b border-border px-5 py-3">
            <FormInput label="Name" value={form.name} onChange={v => set('name', v)} placeholder="my-agent" required />
          </div>

          {/* Editable meta */}
          <div className="border-b border-border px-5 py-3">
            <MetaRow label="Provider">
              <NativeSelect aria-label="Provider" value={form.provider} onChange={e => {
                const v = e.target.value
                if (v === 'inherit') {
                  setCustomModelInput(false)
                  onChange({
                    ...form,
                    provider: v,
                    model: '',
                    reasoning_effort: AUTO_REASONING_EFFORT,
                    reasoning_required: false,
                  })
                } else {
                  const newModels = getModelsForProvider(providerCatalog, v)
                  const valid = newModels?.some(m => m.value === form.model)
                  setCustomModelInput(false)
                  const nextModel = valid ? form.model : ''
                  const nextReasoning = resolveReasoningEffort(v, nextModel, form.reasoning_effort)
                  onChange({
                    ...form,
                    provider: v,
                    model: nextModel,
                    reasoning_effort: nextReasoning,
                    reasoning_required:
                      nextReasoning === AUTO_REASONING_EFFORT ? false : form.reasoning_required,
                  })
                }
              }}>
                <option value="inherit">(default)</option>
                {providerOptions.map(provider => (
                  <option key={provider} value={provider}>
                    {provider}
                  </option>
                ))}
              </NativeSelect>
            </MetaRow>

            <MetaRow label="Model">
              {showCustomModel ? (
                <div className="flex items-center gap-1">
                  <Input
                    value={form.model}
                    onChange={e => set('model', e.target.value)}
                    placeholder="e.g. claude-sonnet-4-5-20250929"
                    aria-label="Custom model"
                    autoFocus={customModelInput}
                  />
                  {models && (
                    <Button
                      type="button"
                      variant="ghost"
                      size="icon"
                      dense
                      className={`${coarseHitAreaCls} shrink-0`}
                      onClick={() => { setCustomModelInput(false); set('model', '') }}
                      aria-label="Use discovered models"
                    >
                      &times;
                    </Button>
                  )}
                </div>
              ) : (
                <NativeSelect aria-label="Model" value={form.model} onChange={e => {
                  if (e.target.value === '__custom__') { setCustomModelInput(true); set('model', '') }
                  else {
                    const nextModel = e.target.value
                    const nextReasoning = resolveReasoningEffort(form.provider, nextModel, form.reasoning_effort)
                    onChange({
                      ...form,
                      model: nextModel,
                      reasoning_effort: nextReasoning,
                      reasoning_required:
                        nextReasoning === AUTO_REASONING_EFFORT ? false : form.reasoning_required,
                    })
                  }
                }}>
                  {models?.map(m => <option key={m.value} value={m.value}>{m.label}</option>)}
                  {!isInheritProvider && <option value="__custom__">Custom...</option>}
                </NativeSelect>
              )}
            </MetaRow>

            <MetaRow label="Fallback">
              {form.fallback_agent ? (
                <div className="flex items-center gap-1">
                  <NativeSelect aria-label="Fallback agent" value={form.fallback_agent} onChange={e => {
                    onChange({ ...form, fallback_agent: e.target.value || '' })
                  }}>
                    {agentNames.filter(n => n !== form.name).includes(form.fallback_agent) ? null : (
                      <option key={form.fallback_agent} value={form.fallback_agent}>{form.fallback_agent} (missing)</option>
                    )}
                    {agentNames.filter(n => n !== form.name).map(n => (
                      <option key={n} value={n}>{n}</option>
                    ))}
                  </NativeSelect>
                  <Button
                    type="button"
                    variant="ghost"
                    size="icon"
                    dense
                    className={`${coarseHitAreaCls} shrink-0`}
                    onClick={() => onChange({ ...form, fallback_agent: '' })}
                    aria-label="Remove fallback agent"
                  >
                    &times;
                  </Button>
                </div>
              ) : (
                <Button
                  type="button"
                  variant="ghost"
                  size="sm"
                  dense
                  className={`${coarseHitAreaCls} h-auto min-h-0 p-0 text-[var(--accent)] underline hover:text-[var(--accent-hover)]`}
                  disabled={!agentNames.some(n => n !== form.name)}
                  onClick={() => {
                    const first = agentNames.find(n => n !== form.name)
                    if (first) onChange({ ...form, fallback_agent: first })
                  }}
                >
                  + Add fallback agent
                </Button>
              )}
            </MetaRow>

            <MetaRow label="Reasoning">
              <NativeSelect
                aria-label="Reasoning"
                value={form.reasoning_effort}
                onChange={e => {
                  const nextReasoning = e.target.value
                  onChange({
                    ...form,
                    reasoning_effort: nextReasoning,
                    reasoning_required:
                      nextReasoning === AUTO_REASONING_EFFORT ? false : form.reasoning_required,
                  })
                }}
                disabled={reasoningDisabled}
              >
                {reasoningOptions.map(option => (
                  <option key={option.value} value={option.value} disabled={option.disabled}>
                    {option.label}
                  </option>
                ))}
              </NativeSelect>
            </MetaRow>

            <MetaRow label="Require support">
              <label
                htmlFor={`${checkboxIdPrefix}-reasoning-required`}
                className="flex select-none items-center justify-end gap-1.5 text-sm text-[var(--text-primary)]"
              >
                <Input
                  id={`${checkboxIdPrefix}-reasoning-required`}
                  type="checkbox"
                  wrapperClassName="w-auto"
                  className="size-4 h-4 shrink-0 p-0"
                  aria-label="Require reasoning support"
                  checked={form.reasoning_required}
                  disabled={form.reasoning_effort === AUTO_REASONING_EFFORT}
                  onChange={e => set('reasoning_required', e.target.checked)}
                />
                <span>{form.reasoning_effort === AUTO_REASONING_EFFORT ? 'Disabled on Auto' : 'Fail if unsupported'}</span>
              </label>
            </MetaRow>

            <MetaRow label="Mode">
              <NativeSelect aria-label="Mode" value={form.mode} onChange={e => set('mode', e.target.value)}>
                <option value="inherit">(default)</option>
                <option value="interactive">Interactive</option>
                <option value="embedded">Embedded</option>
                <option value="headless">Headless</option>
              </NativeSelect>
            </MetaRow>

            <MetaRow label="Surfaces">
              <div className="flex flex-col gap-1.5">
                <label
                  htmlFor={`${checkboxIdPrefix}-surface-spawn`}
                  className="flex select-none items-center justify-end gap-1.5 text-sm text-[var(--text-primary)]"
                >
                  <Input
                    id={`${checkboxIdPrefix}-surface-spawn`}
                    type="checkbox"
                    wrapperClassName="w-auto"
                    className="size-4 h-4 shrink-0 p-0"
                    aria-label="Spawn surface"
                    checked={form.surfaces.includes('spawn')}
                    onChange={() => toggleSurface('spawn')}
                  />
                  <span>Spawn</span>
                </label>
                <label
                  htmlFor={`${checkboxIdPrefix}-surface-persona`}
                  className="flex select-none items-center justify-end gap-1.5 text-sm text-[var(--text-primary)]"
                >
                  <Input
                    id={`${checkboxIdPrefix}-surface-persona`}
                    type="checkbox"
                    wrapperClassName="w-auto"
                    className="size-4 h-4 shrink-0 p-0"
                    aria-label="Persona surface"
                    checked={form.surfaces.includes('persona')}
                    onChange={() => toggleSurface('persona')}
                  />
                  <span>Persona</span>
                </label>
              </div>
            </MetaRow>

            <MetaRow label="Isolation">
              <NativeSelect
                aria-label="Isolation"
                value={isGitProject ? form.isolation : 'inherit'}
                onChange={e => set('isolation', e.target.value)}
                disabled={!isGitProject}
              >
                <option value="inherit">(default)</option>
                <option value="none">None</option>
                <option value="worktree">Worktree</option>
                <option value="clone">Clone</option>
              </NativeSelect>
            </MetaRow>

            <MetaRow label="Base branch">
              {!isGitProject ? (
                <NativeSelect aria-label="Base branch" disabled value="inherit">
                  <option value="inherit">(default)</option>
                </NativeSelect>
              ) : showCustomBranch ? (
                <div className="flex items-center gap-1">
                  <Input
                    value={form.base_branch}
                    onChange={e => set('base_branch', e.target.value)}
                    placeholder="branch name"
                    aria-label="Custom base branch"
                    autoFocus={customBranchInput}
                  />
                  <Button
                    type="button"
                    variant="ghost"
                    size="icon"
                    dense
                    className={`${coarseHitAreaCls} shrink-0`}
                    onClick={() => { setCustomBranchInput(false); set('base_branch', 'inherit') }}
                    aria-label="Use known branches"
                  >
                    &times;
                  </Button>
                </div>
              ) : (
                <NativeSelect aria-label="Base branch" value={form.base_branch} onChange={e => {
                  if (e.target.value === '__custom__') { setCustomBranchInput(true); set('base_branch', '') }
                  else set('base_branch', e.target.value)
                }}>
                  <option value="inherit">(default)</option>
                  {branches.map(b => <option key={b} value={b}>{b}</option>)}
                  <option value="__custom__">Custom...</option>
                </NativeSelect>
              )}
            </MetaRow>

            {pipelines && (
              <MetaRow label="Pipeline">
                <NativeSelect aria-label="Pipeline" value={form.pipeline} onChange={e => set('pipeline', e.target.value)}>
                  <option value="">(none)</option>
                  {pipelines.map(p => <option key={p.id} value={p.name}>{p.name}</option>)}
                </NativeSelect>
              </MetaRow>
            )}

            <MetaRow label="Timeout (s)">
              <Input
                type="number"
                min={0}
                aria-label="Timeout in seconds"
                value={form.timeout}
                onChange={e => set('timeout', Number(e.target.value))}
              />
            </MetaRow>
          </div>

          {/* Identity */}
          <div className="flex flex-col gap-1.5 border-b border-border px-5 py-3">
            <Heading level={4} className="mt-0 mb-1 text-sm font-semibold uppercase tracking-wider text-[var(--text-muted)]">Identity</Heading>
            <FormTextarea label="Description" value={form.description} onChange={v => set('description', v)} placeholder="What this agent does..." />
            <FormTextarea label="Role" value={form.role} onChange={v => set('role', v)} placeholder="e.g. Senior security engineer" />
            <FormTextarea label="Goal" value={form.goal} onChange={v => set('goal', v)} placeholder="What success looks like..." />
            <FormTextarea label="Personality" value={form.personality} onChange={v => set('personality', v)} placeholder="Communication style, tone..." />
          </div>

          {/* Instructions */}
          <div className="flex flex-col gap-1.5 border-b border-border px-5 py-3">
            <Heading level={4} className="mt-0 mb-1 text-sm font-semibold uppercase tracking-wider text-[var(--text-muted)]">Instructions</Heading>
            <div className="max-h-100 min-h-50 overflow-hidden rounded-md border border-border [&_.codemirror-container]:h-50">
              <CodeMirrorEditor
                content={form.instructions}
                language="markdown"
                onChange={v => set('instructions', v)}
              />
            </div>
          </div>

          {/* Rules */}
          {onRulesChange && rules !== undefined && (
            <div className="flex flex-col gap-1.5 border-b border-border px-5 py-3">
              <Heading level={4} className="mt-0 mb-1 text-sm font-semibold uppercase tracking-wider text-[var(--text-muted)]">Rules</Heading>
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
            <div className="flex flex-col gap-1.5 border-b border-border px-5 py-3">
              <Heading level={4} className="mt-0 mb-1 text-sm font-semibold uppercase tracking-wider text-[var(--text-muted)]">Skills</Heading>
              <AgentSkillsEditor
                skills={editSkills}
                onSkillsChange={onSkillsChange}
                projectId={projectId}
              />
            </div>
          )}

          {/* Variables */}
          {onVariablesChange && variables !== undefined && (
            <div className="flex flex-col gap-1.5 border-b border-border px-5 py-3">
              <Heading level={4} className="mt-0 mb-1 text-sm font-semibold uppercase tracking-wider text-[var(--text-muted)]">Variables</Heading>
              <AgentVariablesEditor
                definitionId={editingId}
                variables={variables}
                onVariablesChange={onVariablesChange}
              />
            </div>
          )}

          {/* Tool Restrictions */}
          {(onBlockedToolsChange || onBlockedMcpToolsChange) && (
            <div className="flex flex-col gap-1.5 border-b border-border px-5 py-3">
              <Heading level={4} className="mt-0 mb-1 text-sm font-semibold uppercase tracking-wider text-[var(--text-muted)]">Tool Restrictions</Heading>
              <AgentToolBlocksEditor
                blockedTools={blockedTools || []}
                onBlockedToolsChange={onBlockedToolsChange}
                blockedMcpTools={blockedMcpTools || []}
                onBlockedMcpToolsChange={onBlockedMcpToolsChange}
              />
            </div>
          )}

          {/* Steps */}
          {onStepsChange && steps !== undefined && (
            <div className="flex flex-col gap-1.5 border-b border-border px-5 py-3">
              <Heading level={4} className="mt-0 mb-1 text-sm font-semibold uppercase tracking-wider text-[var(--text-muted)]">Steps</Heading>
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
