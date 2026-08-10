import { useState, useEffect, useCallback, useMemo } from 'react'
import { Button } from '../ui/Button'
import { Chip } from '../ui/Chip'
import { Input } from '../ui/Input'
import { NativeSelect } from '../ui/NativeSelect'
import { coarseHitAreaCls } from '../ui/controlStyles'
import { getAgentEditorCaughtError, getAgentEditorResponseError } from './agent-editor-errors'

interface RuleInfo {
  name: string
  description?: string
  source?: string
  project_id?: string | null
}

interface RuleSelectors {
  include: string[]
  exclude: string[]
}

interface AgentRulesEditorProps {
  definitionId?: string | null
  rules: string[]
  onRulesChange: (rules: string[]) => void
  projectId?: string
  ruleSelectors?: RuleSelectors | null
  onRuleSelectorsChange?: (selectors: RuleSelectors) => void
}

const SELECTOR_PREFIXES = ['tag:', 'group:', 'name:'] as const

export function AgentRulesEditor({
  definitionId, rules, onRulesChange, projectId,
  ruleSelectors, onRuleSelectorsChange,
}: AgentRulesEditorProps) {
  const [availableRules, setAvailableRules] = useState<RuleInfo[]>([])
  const [adding, setAdding] = useState(false)
  const [actionError, setActionError] = useState<string | null>(null)

  // Autocomplete data for selectors
  const [knownTags, setKnownTags] = useState<string[]>([])
  const [knownGroups, setKnownGroups] = useState<string[]>([])
  const [addingSelectorType, setAddingSelectorType] = useState<'include' | 'exclude' | null>(null)
  const [selectorPrefix, setSelectorPrefix] = useState<string>('tag:')
  const [selectorValue, setSelectorValue] = useState('')

  useEffect(() => {
    const params = projectId ? `?project_id=${encodeURIComponent(projectId)}` : ''
    fetch(`/api/rules${params}`)
      .then(r => r.json())
      .then(data => {
        const items = (data.rules || []).map((r: RuleInfo) => ({
          name: r.name,
          description: r.description,
          source: r.source,
          project_id: r.project_id,
        }))
        setAvailableRules(items)
      })
      .catch(() => setAvailableRules([]))
  }, [projectId])

  // Fetch tags and groups for selector autocomplete
  useEffect(() => {
    fetch('/api/rules/tags')
      .then(r => r.json())
      .then(data => setKnownTags(data.tags || []))
      .catch(() => setKnownTags([]))
    fetch('/api/rules/groups')
      .then(r => r.json())
      .then(data => setKnownGroups(data.groups || []))
      .catch(() => setKnownGroups([]))
  }, [])

  const addableRules = availableRules.filter(r => !rules.includes(r.name))
  const projectRules = addableRules.filter(r => r.project_id)
  const globalRules = addableRules.filter(r => !r.project_id)

  const handleAdd = useCallback(async (ruleName: string) => {
    setActionError(null)
    if (!definitionId) {
      onRulesChange([...rules, ruleName])
      setAdding(false)
      return
    }
    try {
      const res = await fetch(`/api/agents/definitions/${definitionId}/rules`, {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ add: [ruleName] }),
      })
      if (!res.ok) throw new Error(await getAgentEditorResponseError(res, 'Failed to add rule'))
      const data = await res.json()
      onRulesChange(data.rules || [...rules, ruleName])
    } catch (e) {
      setActionError(getAgentEditorCaughtError(e, 'Failed to add rule'))
    }
    setAdding(false)
  }, [definitionId, rules, onRulesChange])

  const handleRemove = useCallback(async (ruleName: string) => {
    setActionError(null)
    if (!definitionId) {
      onRulesChange(rules.filter(r => r !== ruleName))
      return
    }
    try {
      const res = await fetch(`/api/agents/definitions/${definitionId}/rules`, {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ remove: [ruleName] }),
      })
      if (!res.ok) throw new Error(await getAgentEditorResponseError(res, 'Failed to remove rule'))
      const data = await res.json()
      onRulesChange(data.rules || rules.filter(r => r !== ruleName))
    } catch (e) {
      setActionError(getAgentEditorCaughtError(e, 'Failed to remove rule'))
    }
  }, [definitionId, rules, onRulesChange])

  // --- Selector handlers ---
  const selectors = useMemo<RuleSelectors>(
    () => ruleSelectors || { include: [], exclude: [] },
    [ruleSelectors],
  )

  const handleAddSelector = useCallback(async (type: 'include' | 'exclude', selector: string) => {
    setActionError(null)
    const updated: RuleSelectors = {
      include: [...selectors.include],
      exclude: [...selectors.exclude],
    }
    if (type === 'include' && !updated.include.includes(selector)) {
      updated.include.push(selector)
    } else if (type === 'exclude' && !updated.exclude.includes(selector)) {
      updated.exclude.push(selector)
    }

    if (!definitionId) {
      onRuleSelectorsChange?.(updated)
      return
    }

    try {
      const body = type === 'include'
        ? { add_include: [selector] }
        : { add_exclude: [selector] }
      const res = await fetch(`/api/agents/definitions/${definitionId}/rule-selectors`, {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
      })
      if (!res.ok) throw new Error(await getAgentEditorResponseError(res, 'Failed to add selector'))
      const data = await res.json()
      onRuleSelectorsChange?.(data.rule_selectors)
    } catch (e) {
      setActionError(getAgentEditorCaughtError(e, 'Failed to add selector'))
    }
  }, [definitionId, selectors, onRuleSelectorsChange])

  const handleRemoveSelector = useCallback(async (type: 'include' | 'exclude', selector: string) => {
    setActionError(null)
    const updated: RuleSelectors = {
      include: selectors.include.filter(s => s !== selector),
      exclude: selectors.exclude.filter(s => s !== selector),
    }

    if (!definitionId) {
      onRuleSelectorsChange?.(updated)
      return
    }

    try {
      const body = type === 'include'
        ? { remove_include: [selector] }
        : { remove_exclude: [selector] }
      const res = await fetch(`/api/agents/definitions/${definitionId}/rule-selectors`, {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
      })
      if (!res.ok) throw new Error(await getAgentEditorResponseError(res, 'Failed to remove selector'))
      const data = await res.json()
      onRuleSelectorsChange?.(data.rule_selectors)
    } catch (e) {
      setActionError(getAgentEditorCaughtError(e, 'Failed to remove selector'))
    }
  }, [definitionId, selectors, onRuleSelectorsChange])

  const commitSelector = () => {
    if (!addingSelectorType || !selectorValue.trim()) {
      setAddingSelectorType(null)
      return
    }
    const full = selectorPrefix === 'name:' ? selectorValue.trim() : `${selectorPrefix}${selectorValue.trim()}`
    handleAddSelector(addingSelectorType, full)
    setAddingSelectorType(null)
    setSelectorValue('')
    setSelectorPrefix('tag:')
  }

  // Build autocomplete suggestions based on selected prefix
  const suggestions = selectorPrefix === 'tag:' ? knownTags
    : selectorPrefix === 'group:' ? knownGroups
    : []
  const filteredSuggestions = suggestions.filter(s =>
    s.toLowerCase().includes(selectorValue.toLowerCase()) &&
    !selectors.include.includes(`${selectorPrefix}${s}`) &&
    !selectors.exclude.includes(`${selectorPrefix}${s}`)
  )

  const removeButton = (type: 'include' | 'exclude' | 'rule', value: string) => (
    <Button
      type="button"
      variant="ghost"
      size="icon"
      dense
      className={`${coarseHitAreaCls} min-h-0 w-auto px-0.5 text-base leading-none hover:text-[var(--color-error)]`}
      onClick={() => {
        if (type === 'rule') void handleRemove(value)
        else void handleRemoveSelector(type, value)
      }}
      title={`Remove ${value}`}
    >
      &times;
    </Button>
  )

  const selectorInput = (type: 'include' | 'exclude') => (
    <div className="flex items-center gap-1">
      <NativeSelect
        wrapperClassName="w-20 shrink-0"
        className="px-1 text-xs"
        aria-label={`${type} selector prefix`}
        value={selectorPrefix}
        onChange={(event) => {
          setSelectorPrefix(event.target.value)
          setSelectorValue('')
        }}
      >
        {SELECTOR_PREFIXES.map((prefix) => (
          <option key={prefix} value={prefix}>
            {prefix}
          </option>
        ))}
      </NativeSelect>
      <div className="min-w-0 flex-1">
        <Input
          autoFocus
          value={selectorValue}
          onChange={(event) => setSelectorValue(event.target.value)}
          onKeyDown={(event) => {
            if (event.key === 'Enter') commitSelector()
            if (event.key === 'Escape') setAddingSelectorType(null)
          }}
          placeholder="value"
          aria-label={`${type} selector value`}
          list={`selector-suggestions-${type}`}
        />
        {filteredSuggestions.length > 0 && (
          <datalist id={`selector-suggestions-${type}`}>
            {filteredSuggestions.map((suggestion) => (
              <option key={suggestion} value={suggestion}>
                {suggestion}
              </option>
            ))}
          </datalist>
        )}
      </div>
      <Button
        type="button"
        size="sm"
        dense
        className={coarseHitAreaCls}
        onClick={commitSelector}
      >
        Add
      </Button>
      <Button
        type="button"
        size="sm"
        dense
        className={coarseHitAreaCls}
        onClick={() => setAddingSelectorType(null)}
      >
        Cancel
      </Button>
    </div>
  )

  return (
    <div className="flex flex-col gap-2">
      {actionError && (
        <Button
          type="button"
          variant="destructive"
          size="sm"
          dense
          className={`${coarseHitAreaCls} justify-start border-destructive/40 bg-destructive/10 px-3 py-2 text-xs text-destructive-foreground`}
          onClick={() => setActionError(null)}
          aria-label={`Dismiss error: ${actionError}`}
        >
          {actionError}
        </Button>
      )}

      <div className="flex flex-wrap items-center gap-1.5">
        {rules.map((name) => (
          <Chip key={name} className="gap-1 border border-border pl-2.5 pr-2 text-sm">
            {name}
            {removeButton('rule', name)}
          </Chip>
        ))}
        {rules.length === 0 && !adding && (
          <span className="text-sm italic text-[var(--text-muted)]">No rules assigned</span>
        )}
      </div>
      {adding ? (
        <NativeSelect
          wrapperClassName="max-w-50"
          className="text-sm"
          aria-label="Select rule"
          autoFocus
          value=""
          onChange={(event) => {
            if (event.target.value) void handleAdd(event.target.value)
          }}
          onBlur={() => setAdding(false)}
        >
          <option value="">Select rule...</option>
          {projectRules.length > 0 && (
            <optgroup label="Project">
              {projectRules.map((rule) => (
                <option key={rule.name} value={rule.name}>
                  {rule.name}
                </option>
              ))}
            </optgroup>
          )}
          {globalRules.length > 0 && (
            <optgroup label="Global">
              {globalRules.map((rule) => (
                <option key={rule.name} value={rule.name}>
                  {rule.name}
                </option>
              ))}
            </optgroup>
          )}
          {projectRules.length === 0 && globalRules.length === 0 && (
            <option disabled>No rules available</option>
          )}
        </NativeSelect>
      ) : (
        <Button
          type="button"
          size="sm"
          dense
          className={`${coarseHitAreaCls} self-start`}
          onClick={() => setAdding(true)}
          disabled={addableRules.length === 0}
        >
          + Add Rule
        </Button>
      )}

      {onRuleSelectorsChange && (
        <div className="mt-2.5 flex flex-col gap-2 border-t border-border pt-2.5">
          <div className="text-sm font-semibold uppercase tracking-wider text-[var(--text-muted)]">
            Rule Selectors
          </div>
          {(['include', 'exclude'] as const).map((type) => {
            const values = selectors[type]
            return (
              <div key={type} className="flex flex-col gap-1">
                <span className="text-xs uppercase tracking-wider text-[var(--text-muted)]">
                  {type}
                </span>
                <div className="flex flex-wrap items-center gap-1.5">
                  {values.map((selector) => (
                    <Chip
                      key={selector}
                      tone={type === 'include' ? 'info' : 'error'}
                      className={`gap-1 border border-dashed pl-2.5 pr-2 text-sm ${
                        type === 'include'
                          ? 'border-[var(--color-info)]'
                          : 'border-[var(--color-error)]'
                      }`}
                    >
                      {selector}
                      {removeButton(type, selector)}
                    </Chip>
                  ))}
                  {values.length === 0 && addingSelectorType !== type && (
                    <span className="text-sm italic text-[var(--text-muted)]">None</span>
                  )}
                </div>
                {addingSelectorType === type ? (
                  selectorInput(type)
                ) : (
                  <Button
                    type="button"
                    size="sm"
                    dense
                    className={`${coarseHitAreaCls} self-start`}
                    onClick={() => {
                      setAddingSelectorType(type)
                      setSelectorPrefix('tag:')
                      setSelectorValue('')
                    }}
                  >
                    + Add {type === 'include' ? 'Include' : 'Exclude'}
                  </Button>
                )}
              </div>
            )
          })}
        </div>
      )}
    </div>
  )
}
