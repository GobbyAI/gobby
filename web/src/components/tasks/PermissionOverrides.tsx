import { useState, useCallback, useEffect } from 'react'
import { cn } from '../../lib/utils'

interface PermissionRule {
  id: string
  label: string
  description: string
  icon: string
  category: 'tools' | 'access' | 'network'
  default: boolean
}

interface PermissionOverridesState {
  rules: Record<string, boolean>
  fileScope: string
}

const PERMISSION_RULES: PermissionRule[] = [
  { id: 'file_edit', label: 'File Editing', description: 'Allow Edit, Write, and NotebookEdit tools', icon: '✎', category: 'tools', default: true },
  { id: 'shell', label: 'Shell Access', description: 'Allow Bash command execution', icon: '▶', category: 'tools', default: true },
  { id: 'git_write', label: 'Git Write', description: 'Allow git push, branch, commit operations', icon: '⭡', category: 'tools', default: true },
  { id: 'mcp_tools', label: 'MCP Tools', description: 'Allow calling tools on MCP servers', icon: '⚙', category: 'tools', default: true },
  { id: 'network', label: 'Network Access', description: 'Allow web fetch and external API calls', icon: '⇅', category: 'network', default: true },
  { id: 'spawn_agents', label: 'Spawn Agents', description: 'Allow spawning sub-agents and terminals', icon: '∴', category: 'access', default: true },
]

const STORAGE_KEY = 'gobby-perm-overrides-'

const CATEGORIES: { key: string; label: string }[] = [
  { key: 'tools', label: 'Tool Access' },
  { key: 'network', label: 'Network' },
  { key: 'access', label: 'Orchestration' },
]

const ROOT_CLS = 'overflow-hidden rounded-md border border-[var(--border)]'
const HEADER_CLS =
  'flex w-full cursor-pointer items-center gap-1.5 border-0 bg-[var(--bg-secondary)] px-2.5 py-2 text-left text-[length:calc(var(--font-size-base)*0.75)] text-[var(--text-primary)] hover:bg-[var(--bg-tertiary)] pointer-coarse:min-h-11'
const TOGGLE_ICON_CLS = 'w-2.5 text-[length:calc(var(--font-size-base)*0.6)] text-[var(--text-muted)]'
const HEADER_LABEL_CLS = 'flex-1 font-medium'
const BADGE_CLS =
  'rounded-lg bg-[color-mix(in_srgb,var(--text-muted)_15%,transparent)] px-1.5 py-px text-[length:calc(var(--font-size-base)*0.6)] text-[var(--text-muted)]'
const BADGE_ACTIVE_CLS =
  'bg-[color-mix(in_srgb,var(--color-warning-foreground)_15%,transparent)] text-[var(--color-warning-foreground)]'

const BODY_CLS = 'flex flex-col gap-2.5 px-2.5 pb-2.5 pt-2'
const CATEGORY_CLS = 'flex flex-col gap-1'
const CATEGORY_LABEL_CLS =
  'pb-0.5 text-[length:calc(var(--font-size-base)*0.6)] font-semibold uppercase tracking-[0.05em] text-[var(--text-muted)]'

const RULE_CLS =
  'flex items-center justify-between gap-2 rounded px-1.5 py-[5px] transition-colors duration-150 hover:bg-[var(--bg-secondary)]'
const RULE_OVERRIDDEN_CLS =
  'bg-[color-mix(in_srgb,var(--color-warning-foreground)_6%,transparent)] hover:bg-[color-mix(in_srgb,var(--color-warning-foreground)_10%,transparent)]'
const RULE_INFO_CLS = 'flex min-w-0 flex-1 items-center gap-2'
const RULE_ICON_CLS = 'w-[18px] shrink-0 text-center text-[length:calc(var(--font-size-base)*0.8)]'
const RULE_TEXT_CLS = 'flex min-w-0 flex-col gap-px'
const RULE_LABEL_CLS = 'text-[length:calc(var(--font-size-base)*0.72)] font-medium text-[var(--text-primary)]'
const RULE_DESC_CLS =
  'overflow-hidden text-ellipsis whitespace-nowrap text-[length:calc(var(--font-size-base)*0.6)] text-[var(--text-muted)]'

const TOGGLE_CLS = 'relative h-[18px] w-8 shrink-0 cursor-pointer border-0 bg-transparent p-0'
const TOGGLE_TRACK_CLS = 'block h-full w-full rounded-[9px] transition-colors duration-200'
const TOGGLE_ON_TRACK_CLS = 'bg-[var(--accent)]'
const TOGGLE_OFF_TRACK_CLS = 'bg-[color-mix(in_srgb,var(--text-muted)_35%,transparent)]'
const TOGGLE_THUMB_CLS =
  'absolute left-0.5 top-0.5 h-3.5 w-3.5 rounded-full bg-[var(--text-primary)] shadow-[var(--shadow-sm)] transition-transform duration-200'
const TOGGLE_THUMB_ON_CLS = 'translate-x-3.5'

const FILE_SCOPE_CLS = 'flex flex-col gap-[3px] px-1.5'
const FILE_INPUT_CLS =
  'box-border w-full rounded border border-[var(--border)] bg-[var(--bg-primary)] px-2 py-[5px] font-[inherit] text-[length:calc(var(--font-size-base)*0.7)] text-[var(--text-primary)] outline-none placeholder:text-[var(--text-muted)] focus:border-[var(--accent)] pointer-coarse:min-h-11'
const FILE_HINT_CLS = 'text-[length:calc(var(--font-size-base)*0.55)] text-[var(--text-muted)]'

const RESET_CLS =
  'self-start cursor-pointer rounded border border-[var(--border)] bg-transparent px-2.5 py-[3px] text-[length:calc(var(--font-size-base)*0.6)] text-[var(--text-muted)] transition-colors duration-150 hover:border-[var(--text-muted)] hover:text-[var(--text-primary)] pointer-coarse:min-h-11'

function getStoredOverrides(taskId: string): PermissionOverridesState {
  try {
    const raw = localStorage.getItem(`${STORAGE_KEY}${taskId}`)
    if (raw) {
      const parsed = JSON.parse(raw)
      if (parsed && typeof parsed === 'object' && typeof parsed.rules === 'object') {
        return {
          rules: parsed.rules ?? {},
          fileScope: typeof parsed.fileScope === 'string' ? parsed.fileScope : '',
        }
      }
    }
  } catch { /* noop */ }
  return { rules: {}, fileScope: '' }
}

function storeOverrides(taskId: string, state: PermissionOverridesState) {
  try {
    localStorage.setItem(`${STORAGE_KEY}${taskId}`, JSON.stringify(state))
  } catch { /* noop */ }
}

function hasOverrides(state: PermissionOverridesState): boolean {
  const hasRuleOverrides = Object.entries(state.rules).some(([id, val]) => {
    const rule = PERMISSION_RULES.find(r => r.id === id)
    return rule && val !== rule.default
  })
  return hasRuleOverrides || state.fileScope.trim().length > 0
}

interface PermissionOverridesProps {
  taskId: string
}

export function PermissionOverrides({ taskId }: PermissionOverridesProps) {
  const [state, setState] = useState<PermissionOverridesState>(() => getStoredOverrides(taskId))
  const [expanded, setExpanded] = useState(false)

  useEffect(() => {
    setState(getStoredOverrides(taskId))
  }, [taskId])

  useEffect(() => {
    const timer = setTimeout(() => storeOverrides(taskId, state), 300)
    return () => clearTimeout(timer)
  }, [taskId, state])

  const toggleRule = useCallback((ruleId: string) => {
    setState(prev => {
      const rule = PERMISSION_RULES.find(r => r.id === ruleId)
      if (!rule) return prev
      const current = prev.rules[ruleId] ?? rule.default
      return { ...prev, rules: { ...prev.rules, [ruleId]: !current } }
    })
  }, [])

  const setFileScope = useCallback((value: string) => {
    setState(prev => ({ ...prev, fileScope: value }))
  }, [])

  const resetAll = useCallback(() => {
    setState({ rules: {}, fileScope: '' })
  }, [])

  const overrideCount = Object.entries(state.rules).filter(([id, val]) => {
    const rule = PERMISSION_RULES.find(r => r.id === id)
    return rule && val !== rule.default
  }).length + (state.fileScope.trim() ? 1 : 0)

  const active = hasOverrides(state)

  return (
    <div className={ROOT_CLS}>
      <button
        className={HEADER_CLS}
        onClick={() => setExpanded(!expanded)}
      >
        <span className={TOGGLE_ICON_CLS}>{expanded ? '▾' : '▸'}</span>
        <span className={HEADER_LABEL_CLS}>Permission Overrides</span>
        {active ? (
          <span className={cn(BADGE_CLS, BADGE_ACTIVE_CLS)}>
            {overrideCount} override{overrideCount !== 1 ? 's' : ''}
          </span>
        ) : (
          <span className={BADGE_CLS}>defaults</span>
        )}
      </button>

      {expanded && (
        <div className={BODY_CLS}>
          {CATEGORIES.map(cat => {
            const rules = PERMISSION_RULES.filter(r => r.category === cat.key)
            if (rules.length === 0) return null
            return (
              <div key={cat.key} className={CATEGORY_CLS}>
                <div className={CATEGORY_LABEL_CLS}>{cat.label}</div>
                {rules.map(rule => {
                  const enabled = state.rules[rule.id] ?? rule.default
                  const isOverridden = enabled !== rule.default
                  return (
                    <div
                      key={rule.id}
                      className={cn(RULE_CLS, isOverridden && RULE_OVERRIDDEN_CLS)}
                    >
                      <div className={RULE_INFO_CLS}>
                        <span className={RULE_ICON_CLS}>{rule.icon}</span>
                        <div className={RULE_TEXT_CLS}>
                          <span className={RULE_LABEL_CLS}>{rule.label}</span>
                          <span className={RULE_DESC_CLS}>{rule.description}</span>
                        </div>
                      </div>
                      <button
                        className={TOGGLE_CLS}
                        onClick={() => toggleRule(rule.id)}
                        title={enabled ? 'Disable' : 'Enable'}
                        role="switch"
                        aria-checked={enabled}
                      >
                        <span className={cn(TOGGLE_TRACK_CLS, enabled ? TOGGLE_ON_TRACK_CLS : TOGGLE_OFF_TRACK_CLS)}>
                          <span className={cn(TOGGLE_THUMB_CLS, enabled && TOGGLE_THUMB_ON_CLS)} />
                        </span>
                      </button>
                    </div>
                  )
                })}
              </div>
            )
          })}

          <div className={CATEGORY_CLS}>
            <label htmlFor="perm-file-scope-input" className={CATEGORY_LABEL_CLS}>File Scope</label>
            <div className={FILE_SCOPE_CLS}>
              <input
                id="perm-file-scope-input"
                type="text"
                className={FILE_INPUT_CLS}
                value={state.fileScope}
                onChange={e => setFileScope(e.target.value)}
                placeholder="e.g. src/components/**, tests/**"
              />
              <span className={FILE_HINT_CLS}>
                Restrict file access to matching glob patterns (comma-separated)
              </span>
            </div>
          </div>

          {active && (
            <button className={RESET_CLS} onClick={resetAll}>
              Reset to defaults
            </button>
          )}
        </div>
      )}
    </div>
  )
}
