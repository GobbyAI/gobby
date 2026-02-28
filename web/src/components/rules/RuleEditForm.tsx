import { useState } from 'react'
import { SidebarPanel } from '../shared/SidebarPanel'
import { CodeMirrorEditor } from '../shared/CodeMirrorEditor'
import './RuleEditForm.css'

const RULE_EVENTS = [
  'before_tool', 'after_tool', 'on_error',
  'session_start', 'session_end', 'on_commit', 'user_prompt_submit',
]

const EFFECT_TYPES = ['block', 'set_variable', 'inject_context', 'mcp_call', 'observe']

export interface RuleFormData {
  name: string
  event: string
  description: string
  priority: number
  enabled: boolean
  group: string
  tags: string[]
  when: string
  effect: { type: string; [key: string]: unknown }
}

export const DEFAULT_RULE_FORM: RuleFormData = {
  name: '',
  event: 'before_tool',
  description: '',
  priority: 100,
  enabled: true,
  group: '',
  tags: [],
  when: '',
  effect: { type: 'block', reason: '' },
}

interface RuleEditFormProps {
  isOpen: boolean
  readOnly?: boolean
  form: RuleFormData
  onChange: (form: RuleFormData) => void
  onSave: () => void
  onCancel: () => void
  isEditing: boolean
  saveDisabled?: boolean
  sidebarView: 'form' | 'yaml'
  onViewChange: (view: 'form' | 'yaml') => void
  yamlContent: string
  onYamlChange: (content: string) => void
  onYamlSave: () => void
}

function MetaRow({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className="rule-edit-meta-row">
      <span className="rule-edit-meta-label">{label}</span>
      <div className="rule-edit-meta-value">{children}</div>
    </div>
  )
}

export function RuleEditForm({
  isOpen, readOnly, form, onChange, onSave, onCancel,
  isEditing, saveDisabled,
  sidebarView, onViewChange, yamlContent, onYamlChange, onYamlSave,
}: RuleEditFormProps) {
  const [tagInput, setTagInput] = useState('')

  const set = <K extends keyof RuleFormData>(key: K, value: RuleFormData[K]) =>
    onChange({ ...form, [key]: value })

  const setEffect = (updates: Record<string, unknown>) =>
    onChange({ ...form, effect: { ...form.effect, ...updates } })

  const changeEffectType = (type: string) => {
    const defaults: Record<string, Record<string, unknown>> = {
      block: { type: 'block', reason: '' },
      set_variable: { type: 'set_variable', variable: '', value: '' },
      inject_context: { type: 'inject_context', template: '' },
      mcp_call: { type: 'mcp_call', server: '', tool: '', arguments: {}, background: false },
      observe: { type: 'observe', category: '', message: '' },
    }
    onChange({ ...form, effect: (defaults[type] || { type }) as RuleFormData['effect'] })
  }

  const addTag = () => {
    const tag = tagInput.trim()
    if (tag && !form.tags.includes(tag)) {
      set('tags', [...form.tags, tag])
    }
    setTagInput('')
  }

  const title = readOnly ? form.name || 'Rule' : (isEditing ? 'Edit Rule' : 'Create Rule')

  const headerContent = (
    <>
      <div className="sidebar-tab-bar">
        <button
          type="button"
          className={`sidebar-tab ${sidebarView !== 'yaml' ? 'sidebar-tab--active' : ''}`}
          onClick={() => onViewChange('form')}
        >
          Form
        </button>
        <button
          type="button"
          className={`sidebar-tab ${sidebarView === 'yaml' ? 'sidebar-tab--active' : ''}`}
          onClick={() => onViewChange('yaml')}
        >
          YAML
        </button>
      </div>
    </>
  )

  const footer = !readOnly ? (
    <>
      <button className="rule-edit-btn" onClick={onCancel} type="button">Cancel</button>
      <button
        className="rule-edit-btn rule-edit-btn--primary"
        onClick={sidebarView === 'yaml' ? onYamlSave : onSave}
        disabled={saveDisabled}
        type="button"
      >
        {isEditing ? 'Save' : 'Create'}
      </button>
    </>
  ) : undefined

  return (
    <SidebarPanel isOpen={isOpen} onClose={onCancel} title={title} headerContent={headerContent} footer={footer}>
      {sidebarView === 'yaml' ? (
        <div className="rule-edit-yaml-view">
          <CodeMirrorEditor
            content={yamlContent}
            language="yaml"
            readOnly={readOnly}
            onChange={onYamlChange}
            onSave={!readOnly ? onYamlSave : undefined}
          />
        </div>
      ) : readOnly ? (
        <ReadOnlyView form={form} />
      ) : (
        <>
          {/* Name */}
          <div className="rule-edit-section">
            <label className="rule-edit-field">
              <span className="rule-edit-label">Name *</span>
              <input
                className="rule-edit-input"
                value={form.name}
                onChange={e => set('name', e.target.value)}
                placeholder="my-rule"
              />
            </label>
          </div>

          {/* Meta */}
          <div className="rule-edit-meta">
            <MetaRow label="Event">
              <select className="rule-edit-input" value={form.event} onChange={e => set('event', e.target.value)}>
                {RULE_EVENTS.map(ev => <option key={ev} value={ev}>{ev}</option>)}
              </select>
            </MetaRow>
            <MetaRow label="Priority">
              <input className="rule-edit-input" type="number" value={form.priority} onChange={e => set('priority', Number(e.target.value))} min={0} />
            </MetaRow>
            <MetaRow label="Group">
              <input className="rule-edit-input" value={form.group} onChange={e => set('group', e.target.value)} placeholder="(none)" />
            </MetaRow>
          </div>

          {/* Condition */}
          <div className="rule-edit-section">
            <h4 className="rule-edit-section-title">Condition</h4>
            <label className="rule-edit-field">
              <span className="rule-edit-label">When (expression)</span>
              <input
                className="rule-edit-input rule-edit-mono"
                value={form.when}
                onChange={e => set('when', e.target.value)}
                placeholder='e.g. tool_name == "Edit"'
              />
            </label>
          </div>

          {/* Effect */}
          <div className="rule-edit-section">
            <h4 className="rule-edit-section-title">Effect</h4>
            <label className="rule-edit-field">
              <span className="rule-edit-label">Type</span>
              <select className="rule-edit-input" value={form.effect.type} onChange={e => changeEffectType(e.target.value)}>
                {EFFECT_TYPES.map(t => <option key={t} value={t}>{t}</option>)}
              </select>
            </label>
            <EffectFields effect={form.effect} onChange={setEffect} />
          </div>

          {/* Tags */}
          <div className="rule-edit-section">
            <h4 className="rule-edit-section-title">Tags</h4>
            <div className="rule-edit-chips">
              {form.tags.map(tag => (
                <span key={tag} className="rule-edit-chip">
                  {tag}
                  <button type="button" className="rule-edit-chip-remove" onClick={() => set('tags', form.tags.filter(t => t !== tag))}>&times;</button>
                </span>
              ))}
              <div className="rule-edit-chip-add">
                <input
                  className="rule-edit-input"
                  value={tagInput}
                  onChange={e => setTagInput(e.target.value)}
                  onKeyDown={e => { if (e.key === 'Enter') { e.preventDefault(); addTag() } }}
                  placeholder="Add tag..."
                />
              </div>
            </div>
          </div>

          {/* Description */}
          <div className="rule-edit-section">
            <h4 className="rule-edit-section-title">Description</h4>
            <textarea
              className="rule-edit-textarea"
              value={form.description}
              onChange={e => set('description', e.target.value)}
              placeholder="What this rule does..."
              rows={3}
            />
          </div>
        </>
      )}
    </SidebarPanel>
  )
}

function ReadOnlyView({ form }: { form: RuleFormData }) {
  return (
    <>
      <div className="rule-edit-meta">
        <MetaRow label="Event"><span>{form.event}</span></MetaRow>
        <MetaRow label="Priority"><span>{form.priority}</span></MetaRow>
        <MetaRow label="Enabled"><span>{form.enabled ? 'Yes' : 'No'}</span></MetaRow>
        {form.group && <MetaRow label="Group"><span>{form.group}</span></MetaRow>}
      </div>
      {form.description && (
        <div className="rule-edit-section">
          <h4 className="rule-edit-section-title">Description</h4>
          <span className="rule-edit-readonly-value">{form.description}</span>
        </div>
      )}
      {form.when && (
        <div className="rule-edit-section">
          <h4 className="rule-edit-section-title">Condition</h4>
          <code className="rule-edit-readonly-value rule-edit-mono">{form.when}</code>
        </div>
      )}
      <div className="rule-edit-section">
        <h4 className="rule-edit-section-title">Effect</h4>
        <pre className="rule-edit-readonly-pre">{JSON.stringify(form.effect, null, 2)}</pre>
      </div>
      {form.tags.length > 0 && (
        <div className="rule-edit-section">
          <h4 className="rule-edit-section-title">Tags</h4>
          <div className="rule-edit-chips">
            {form.tags.map(tag => <span key={tag} className="rule-edit-chip">{tag}</span>)}
          </div>
        </div>
      )}
    </>
  )
}

function EffectFields({ effect, onChange }: { effect: Record<string, unknown>; onChange: (u: Record<string, unknown>) => void }) {
  const type = effect.type as string

  if (type === 'block') {
    return (
      <>
        <label className="rule-edit-field">
          <span className="rule-edit-label">Reason</span>
          <textarea className="rule-edit-textarea" value={(effect.reason as string) ?? ''} onChange={e => onChange({ reason: e.target.value })} placeholder="Why this is blocked..." rows={2} />
        </label>
        <label className="rule-edit-field">
          <span className="rule-edit-label">Tools (comma-separated)</span>
          <input className="rule-edit-input" value={Array.isArray(effect.tools) ? (effect.tools as string[]).join(', ') : ''} onChange={e => onChange({ tools: e.target.value.split(',').map(s => s.trim()).filter(Boolean) })} placeholder="Edit, Write" />
        </label>
        <label className="rule-edit-field">
          <span className="rule-edit-label">MCP Tools (comma-separated)</span>
          <input className="rule-edit-input" value={Array.isArray(effect.mcp_tools) ? (effect.mcp_tools as string[]).join(', ') : ''} onChange={e => onChange({ mcp_tools: e.target.value.split(',').map(s => s.trim()).filter(Boolean) })} placeholder="gobby-tasks.create_task" />
        </label>
        <label className="rule-edit-field">
          <span className="rule-edit-label">Command pattern</span>
          <input className="rule-edit-input rule-edit-mono" value={(effect.command_pattern as string) ?? ''} onChange={e => onChange({ command_pattern: e.target.value || undefined })} placeholder="regex pattern" />
        </label>
        <label className="rule-edit-field">
          <span className="rule-edit-label">Command NOT pattern</span>
          <input className="rule-edit-input rule-edit-mono" value={(effect.command_not_pattern as string) ?? ''} onChange={e => onChange({ command_not_pattern: e.target.value || undefined })} placeholder="regex exclusion" />
        </label>
      </>
    )
  }

  if (type === 'set_variable') {
    return (
      <>
        <label className="rule-edit-field">
          <span className="rule-edit-label">Variable name</span>
          <input className="rule-edit-input rule-edit-mono" value={(effect.variable as string) ?? ''} onChange={e => onChange({ variable: e.target.value })} placeholder="my_var" />
        </label>
        <label className="rule-edit-field">
          <span className="rule-edit-label">Value</span>
          <textarea className="rule-edit-textarea rule-edit-mono" value={(effect.value as string) ?? ''} onChange={e => onChange({ value: e.target.value })} placeholder="value or expression" rows={2} />
        </label>
      </>
    )
  }

  if (type === 'inject_context') {
    return (
      <div className="rule-edit-field">
        <span className="rule-edit-label">Template</span>
        <div className="rule-edit-codemirror">
          <CodeMirrorEditor
            content={(effect.template as string) ?? ''}
            language="markdown"
            onChange={v => onChange({ template: v })}
          />
        </div>
      </div>
    )
  }

  if (type === 'mcp_call') {
    const args = (effect.arguments as Record<string, string>) ?? {}
    const argPairs = Object.entries(args).map(([key, value]) => ({ key, value: String(value) }))
    return (
      <>
        <label className="rule-edit-field">
          <span className="rule-edit-label">Server</span>
          <input className="rule-edit-input" value={(effect.server as string) ?? ''} onChange={e => onChange({ server: e.target.value })} placeholder="gobby-tasks" />
        </label>
        <label className="rule-edit-field">
          <span className="rule-edit-label">Tool</span>
          <input className="rule-edit-input" value={(effect.tool as string) ?? ''} onChange={e => onChange({ tool: e.target.value })} placeholder="create_task" />
        </label>
        <div className="rule-edit-field">
          <span className="rule-edit-label">Arguments</span>
          <MatchEditor
            pairs={argPairs}
            onChange={pairs => {
              const obj: Record<string, string> = {}
              for (const p of pairs) if (p.key.trim()) obj[p.key] = p.value
              onChange({ arguments: obj })
            }}
          />
        </div>
        <label className="rule-edit-field" style={{ flexDirection: 'row', alignItems: 'center', gap: 8 }}>
          <input type="checkbox" checked={!!effect.background} onChange={e => onChange({ background: e.target.checked })} />
          <span className="rule-edit-label" style={{ textTransform: 'none' }}>Run in background</span>
        </label>
      </>
    )
  }

  if (type === 'observe') {
    return (
      <>
        <label className="rule-edit-field">
          <span className="rule-edit-label">Category</span>
          <input className="rule-edit-input" value={(effect.category as string) ?? ''} onChange={e => onChange({ category: e.target.value })} placeholder="audit" />
        </label>
        <label className="rule-edit-field">
          <span className="rule-edit-label">Message</span>
          <textarea className="rule-edit-textarea" value={(effect.message as string) ?? ''} onChange={e => onChange({ message: e.target.value })} placeholder="Log message..." rows={2} />
        </label>
      </>
    )
  }

  return null
}

function MatchEditor({ pairs, onChange }: { pairs: { key: string; value: string }[]; onChange: (pairs: { key: string; value: string }[]) => void }) {
  return (
    <div className="rule-edit-kv">
      {pairs.map((p, i) => (
        <div key={i} className="rule-edit-kv-row">
          <input
            className="rule-edit-input"
            value={p.key}
            onChange={e => { const next = [...pairs]; next[i] = { ...next[i], key: e.target.value }; onChange(next) }}
            placeholder="key"
          />
          <input
            className="rule-edit-input"
            value={p.value}
            onChange={e => { const next = [...pairs]; next[i] = { ...next[i], value: e.target.value }; onChange(next) }}
            placeholder="value"
          />
          <button type="button" className="rule-edit-kv-remove" onClick={() => onChange(pairs.filter((_, j) => j !== i))}>&times;</button>
        </div>
      ))}
      <button type="button" className="rule-edit-kv-add" onClick={() => onChange([...pairs, { key: '', value: '' }])}>+ Add</button>
    </div>
  )
}
