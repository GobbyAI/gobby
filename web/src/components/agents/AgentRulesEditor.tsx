import { useState, useEffect, useCallback, useMemo } from 'react'
import {
  AGENT_BTN_CLS,
  AGENT_EDIT_INPUT_CLS,
  AGENT_RULE_SELECTOR_GROUP_CLS,
  AGENT_RULE_SELECTOR_HEADING_CLS,
  AGENT_RULE_SELECTOR_INPUT_ROW_CLS,
  AGENT_RULE_SELECTOR_PREFIX_CLS,
  AGENT_RULE_SELECTOR_VALUE_WRAP_CLS,
  AGENT_RULE_SELECTORS_CLS,
  AGENT_RULE_SELECTORS_LABEL_CLS,
  AGENT_RULES_ADD_BTN_CLS,
  AGENT_RULES_ADD_SELECT_CLS,
  AGENT_RULES_CHIP_CLS,
  AGENT_RULES_CHIP_EXCLUDE_CLS,
  AGENT_RULES_CHIP_INCLUDE_CLS,
  AGENT_RULES_CHIP_REMOVE_CLS,
  AGENT_RULES_CHIP_SELECTOR_CLS,
  AGENT_RULES_CHIPS_CLS,
  AGENT_RULES_EDITOR_CLS,
  AGENT_RULES_EMPTY_CLS,
} from './agents-styles'

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

  // Autocomplete data for selectors
  const [knownTags, setKnownTags] = useState<string[]>([])
  const [knownGroups, setKnownGroups] = useState<string[]>([])
  const [addingSelectorType, setAddingSelectorType] = useState<'include' | 'exclude' | null>(null)
  const [selectorPrefix, setSelectorPrefix] = useState<string>('tag:')
  const [selectorValue, setSelectorValue] = useState('')

  useEffect(() => {
    const params = projectId ? `?project_id=${projectId}` : ''
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
      if (res.ok) {
        const data = await res.json()
        onRulesChange(data.rules || [...rules, ruleName])
      }
    } catch (e) {
      console.error('Failed to add rule:', e)
    }
    setAdding(false)
  }, [definitionId, rules, onRulesChange])

  const handleRemove = useCallback(async (ruleName: string) => {
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
      if (res.ok) {
        const data = await res.json()
        onRulesChange(data.rules || rules.filter(r => r !== ruleName))
      }
    } catch (e) {
      console.error('Failed to remove rule:', e)
    }
  }, [definitionId, rules, onRulesChange])

  // --- Selector handlers ---
  const selectors = useMemo<RuleSelectors>(
    () => ruleSelectors || { include: [], exclude: [] },
    [ruleSelectors],
  )

  const handleAddSelector = useCallback(async (type: 'include' | 'exclude', selector: string) => {
    const updated: RuleSelectors = {
      include: [...selectors.include],
      exclude: [...selectors.exclude],
    }
    if (type === 'include' && !updated.include.includes(selector)) {
      updated.include.push(selector)
    } else if (type === 'exclude' && !updated.exclude.includes(selector)) {
      updated.exclude.push(selector)
    }

    if (definitionId) {
      try {
        const body = type === 'include'
          ? { add_include: [selector] }
          : { add_exclude: [selector] }
        const res = await fetch(`/api/agents/definitions/${definitionId}/rule-selectors`, {
          method: 'PATCH',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(body),
        })
        if (res.ok) {
          const data = await res.json()
          onRuleSelectorsChange?.(data.rule_selectors)
          return
        }
      } catch (e) {
        console.error('Failed to add selector:', e)
      }
    }
    onRuleSelectorsChange?.(updated)
  }, [definitionId, selectors, onRuleSelectorsChange])

  const handleRemoveSelector = useCallback(async (type: 'include' | 'exclude', selector: string) => {
    const updated: RuleSelectors = {
      include: selectors.include.filter(s => s !== selector),
      exclude: selectors.exclude.filter(s => s !== selector),
    }

    if (definitionId) {
      try {
        const body = type === 'include'
          ? { remove_include: [selector] }
          : { remove_exclude: [selector] }
        const res = await fetch(`/api/agents/definitions/${definitionId}/rule-selectors`, {
          method: 'PATCH',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(body),
        })
        if (res.ok) {
          const data = await res.json()
          onRuleSelectorsChange?.(data.rule_selectors)
          return
        }
      } catch (e) {
        console.error('Failed to remove selector:', e)
      }
    }
    onRuleSelectorsChange?.(updated)
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

  return (
    <div className={AGENT_RULES_EDITOR_CLS}>
      {/* Explicit rules */}
      <div className={AGENT_RULES_CHIPS_CLS}>
        {rules.map(name => (
          <span key={name} className={AGENT_RULES_CHIP_CLS}>
            {name}
            <button
              type="button"
              className={AGENT_RULES_CHIP_REMOVE_CLS}
              onClick={() => handleRemove(name)}
              title={`Remove ${name}`}
            >
              &times;
            </button>
          </span>
        ))}
        {rules.length === 0 && !adding && (
          <span className={AGENT_RULES_EMPTY_CLS}>No rules assigned</span>
        )}
      </div>
      {adding ? (
        <select
          className={`${AGENT_EDIT_INPUT_CLS} ${AGENT_RULES_ADD_SELECT_CLS}`}
          autoFocus
          value=""
          onChange={e => { if (e.target.value) handleAdd(e.target.value) }}
          onBlur={() => setAdding(false)}
        >
          <option value="">Select rule...</option>
          {projectRules.length > 0 && (
            <optgroup label="Project">
              {projectRules.map(r => (
                <option key={r.name} value={r.name}>{r.name}</option>
              ))}
            </optgroup>
          )}
          {globalRules.length > 0 && (
            <optgroup label="Global">
              {globalRules.map(r => (
                <option key={r.name} value={r.name}>{r.name}</option>
              ))}
            </optgroup>
          )}
          {projectRules.length === 0 && globalRules.length === 0 && (
            <option disabled>No rules available</option>
          )}
        </select>
      ) : (
        <button
          type="button"
          className={`${AGENT_BTN_CLS} ${AGENT_RULES_ADD_BTN_CLS}`}
          onClick={() => setAdding(true)}
          disabled={addableRules.length === 0}
        >
          + Add Rule
        </button>
      )}

      {/* Rule Selectors */}
      {onRuleSelectorsChange && (
        <div className={AGENT_RULE_SELECTORS_CLS}>
          <div className={AGENT_RULE_SELECTORS_LABEL_CLS}>Rule Selectors</div>

          {/* Include */}
          <div className={AGENT_RULE_SELECTOR_GROUP_CLS}>
            <span className={AGENT_RULE_SELECTOR_HEADING_CLS}>Include</span>
            <div className={AGENT_RULES_CHIPS_CLS}>
              {selectors.include.map(s => (
                <span key={s} className={`${AGENT_RULES_CHIP_CLS} ${AGENT_RULES_CHIP_SELECTOR_CLS} ${AGENT_RULES_CHIP_INCLUDE_CLS}`}>
                  {s}
                  <button type="button" className={AGENT_RULES_CHIP_REMOVE_CLS} onClick={() => handleRemoveSelector('include', s)} title={`Remove ${s}`}>&times;</button>
                </span>
              ))}
              {selectors.include.length === 0 && addingSelectorType !== 'include' && (
                <span className={AGENT_RULES_EMPTY_CLS}>None</span>
              )}
            </div>
            {addingSelectorType === 'include' ? (
              <div className={AGENT_RULE_SELECTOR_INPUT_ROW_CLS}>
                <select
                  className={`${AGENT_EDIT_INPUT_CLS} ${AGENT_RULE_SELECTOR_PREFIX_CLS}`}
                  value={selectorPrefix}
                  onChange={e => { setSelectorPrefix(e.target.value); setSelectorValue('') }}
                >
                  {SELECTOR_PREFIXES.map(p => <option key={p} value={p}>{p}</option>)}
                </select>
                <div className={AGENT_RULE_SELECTOR_VALUE_WRAP_CLS}>
                  <input
                    className={AGENT_EDIT_INPUT_CLS}
                    autoFocus
                    value={selectorValue}
                    onChange={e => setSelectorValue(e.target.value)}
                    onKeyDown={e => { if (e.key === 'Enter') commitSelector(); if (e.key === 'Escape') setAddingSelectorType(null) }}
                    placeholder="value"
                    list="selector-suggestions-include"
                  />
                  {filteredSuggestions.length > 0 && (
                    <datalist id="selector-suggestions-include">
                      {filteredSuggestions.map(s => <option key={s} value={s} aria-label={s} />)}
                    </datalist>
                  )}
                </div>
                <button type="button" className={AGENT_BTN_CLS} onClick={commitSelector}>Add</button>
                <button type="button" className={AGENT_BTN_CLS} onClick={() => setAddingSelectorType(null)}>Cancel</button>
              </div>
            ) : (
              <button
                type="button"
                className={`${AGENT_BTN_CLS} ${AGENT_RULES_ADD_BTN_CLS}`}
                onClick={() => { setAddingSelectorType('include'); setSelectorPrefix('tag:'); setSelectorValue('') }}
              >
                + Add Include
              </button>
            )}
          </div>

          {/* Exclude */}
          <div className={AGENT_RULE_SELECTOR_GROUP_CLS}>
            <span className={AGENT_RULE_SELECTOR_HEADING_CLS}>Exclude</span>
            <div className={AGENT_RULES_CHIPS_CLS}>
              {selectors.exclude.map(s => (
                <span key={s} className={`${AGENT_RULES_CHIP_CLS} ${AGENT_RULES_CHIP_SELECTOR_CLS} ${AGENT_RULES_CHIP_EXCLUDE_CLS}`}>
                  {s}
                  <button type="button" className={AGENT_RULES_CHIP_REMOVE_CLS} onClick={() => handleRemoveSelector('exclude', s)} title={`Remove ${s}`}>&times;</button>
                </span>
              ))}
              {selectors.exclude.length === 0 && addingSelectorType !== 'exclude' && (
                <span className={AGENT_RULES_EMPTY_CLS}>None</span>
              )}
            </div>
            {addingSelectorType === 'exclude' ? (
              <div className={AGENT_RULE_SELECTOR_INPUT_ROW_CLS}>
                <select
                  className={`${AGENT_EDIT_INPUT_CLS} ${AGENT_RULE_SELECTOR_PREFIX_CLS}`}
                  value={selectorPrefix}
                  onChange={e => { setSelectorPrefix(e.target.value); setSelectorValue('') }}
                >
                  {SELECTOR_PREFIXES.map(p => <option key={p} value={p}>{p}</option>)}
                </select>
                <div className={AGENT_RULE_SELECTOR_VALUE_WRAP_CLS}>
                  <input
                    className={AGENT_EDIT_INPUT_CLS}
                    autoFocus
                    value={selectorValue}
                    onChange={e => setSelectorValue(e.target.value)}
                    onKeyDown={e => { if (e.key === 'Enter') commitSelector(); if (e.key === 'Escape') setAddingSelectorType(null) }}
                    placeholder="value"
                    list="selector-suggestions-exclude"
                  />
                  {filteredSuggestions.length > 0 && (
                    <datalist id="selector-suggestions-exclude">
                      {filteredSuggestions.map(s => <option key={s} value={s} aria-label={s} />)}
                    </datalist>
                  )}
                </div>
                <button type="button" className={AGENT_BTN_CLS} onClick={commitSelector}>Add</button>
                <button type="button" className={AGENT_BTN_CLS} onClick={() => setAddingSelectorType(null)}>Cancel</button>
              </div>
            ) : (
              <button
                type="button"
                className={`${AGENT_BTN_CLS} ${AGENT_RULES_ADD_BTN_CLS}`}
                onClick={() => { setAddingSelectorType('exclude'); setSelectorPrefix('tag:'); setSelectorValue('') }}
              >
                + Add Exclude
              </button>
            )}
          </div>
        </div>
      )}
    </div>
  )
}
