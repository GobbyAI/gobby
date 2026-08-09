import { useCallback, useState } from 'react'
import { Heading } from '../shared/Heading'
import { Button } from '../ui/Button'
import { Card } from '../ui/Card'
import { Chip } from '../ui/Chip'
import { FormField } from '../ui/FormField'
import { Input } from '../ui/Input'
import { NativeSelect } from '../ui/NativeSelect'
import { Textarea } from '../ui/Textarea'
import { coarseHitAreaCls } from '../ui/controlStyles'

export interface WorkflowTransition {
  to: string
  when: string
  on_transition?: Record<string, unknown>[]
}

export interface WorkflowStep {
  name: string
  description?: string | null
  status_message?: string | null
  allowed_tools?: string[] | 'all'
  blocked_tools?: string[]
  allowed_mcp_tools?: string[] | 'all'
  blocked_mcp_tools?: string[]
  transitions?: WorkflowTransition[]
  exit_when?: string | null
  on_enter?: Record<string, unknown>[]
  on_exit?: Record<string, unknown>[]
  on_mcp_success?: Record<string, unknown>[]
  on_mcp_error?: Record<string, unknown>[]
}

interface AgentStepsEditorProps {
  steps: WorkflowStep[]
  onChange: (steps: WorkflowStep[]) => void
}

function createDefaultStep(existing: WorkflowStep[]): WorkflowStep {
  const names = new Set(existing.map((step) => step.name))
  let number = existing.length + 1
  while (names.has(`step-${number}`)) number += 1
  return {
    name: `step-${number}`,
    allowed_tools: 'all',
    blocked_tools: [],
    allowed_mcp_tools: 'all',
    blocked_mcp_tools: [],
    transitions: [],
  }
}

function getStepPreview(step: WorkflowStep): string {
  const parts: string[] = []
  if (step.description) {
    const description =
      step.description.length > 50 ? `${step.description.slice(0, 47)}...` : step.description
    parts.push(description)
  }
  if (step.transitions && step.transitions.length > 0) {
    parts.push(
      `${step.transitions.length} transition${step.transitions.length > 1 ? 's' : ''}`,
    )
  }
  return parts.join(' \u2014 ')
}

function ChipInput({
  values,
  onChange,
  placeholder,
}: {
  values: string[]
  onChange: (values: string[]) => void
  placeholder?: string
}) {
  const [input, setInput] = useState('')

  const handleAdd = () => {
    const value = input.trim()
    if (value && !values.includes(value)) onChange([...values, value])
    setInput('')
  }

  return (
    <div className="flex flex-col gap-1">
      <div className="flex flex-wrap gap-1">
        {values.map((value) => (
          <Chip key={value} className="gap-1 border border-border pl-2 pr-1.5 text-xs">
            {value}
            <Button
              type="button"
              variant="ghost"
              size="icon"
              dense
              className={`${coarseHitAreaCls} min-h-0 w-auto px-px text-sm leading-none hover:text-[var(--color-error)]`}
              onClick={() => onChange(values.filter((item) => item !== value))}
              aria-label={`Remove ${value}`}
            >
              &times;
            </Button>
          </Chip>
        ))}
      </div>
      <div className="flex items-center gap-1">
        <Input
          wrapperClassName="min-w-0 flex-1"
          className="px-2 text-sm"
          value={input}
          onChange={(event) => setInput(event.target.value)}
          onKeyDown={(event) => {
            if (event.key === 'Enter') {
              event.preventDefault()
              handleAdd()
            }
          }}
          placeholder={placeholder}
        />
        <Button
          type="button"
          size="sm"
          dense
          className={coarseHitAreaCls}
          onClick={handleAdd}
          disabled={!input.trim()}
          aria-label="Add value"
        >
          +
        </Button>
      </div>
    </div>
  )
}

function ToolGatingSection({
  step,
  onChange,
}: {
  step: WorkflowStep
  onChange: (step: Partial<WorkflowStep>) => void
}) {
  const isAllowedAll = step.allowed_tools === 'all'
  const isMcpAllowedAll = step.allowed_mcp_tools === 'all'

  return (
    <div className="flex flex-col gap-1.5 border-t border-border pt-1.5">
      <Heading
        level={5}
        className="m-0 text-xs font-semibold uppercase tracking-wider text-[var(--text-muted)]"
      >
        Tool Gating
      </Heading>
      <FormField label="Allowed Tools">
        {({ id, describedBy, invalid }) => (
          <div className="flex flex-col gap-1">
            <NativeSelect
              id={id}
              wrapperClassName="w-auto max-w-32"
              className="px-1 text-xs"
              aria-describedby={describedBy}
              error={invalid}
              value={isAllowedAll ? 'all' : 'list'}
              onChange={(event) =>
                onChange({ allowed_tools: event.target.value === 'all' ? 'all' : [] })
              }
            >
              <option value="all">All</option>
              <option value="list">Specific list</option>
            </NativeSelect>
            {!isAllowedAll && (
              <ChipInput
                values={step.allowed_tools as string[]}
                onChange={(value) => onChange({ allowed_tools: value })}
                placeholder="Tool name..."
              />
            )}
          </div>
        )}
      </FormField>
      <FormField label="Blocked Tools" group>
        {() => (
          <ChipInput
            values={step.blocked_tools || []}
            onChange={(value) => onChange({ blocked_tools: value })}
            placeholder="Tool to block..."
          />
        )}
      </FormField>
      <FormField label="Allowed MCP Tools">
        {({ id, describedBy, invalid }) => (
          <div className="flex flex-col gap-1">
            <NativeSelect
              id={id}
              wrapperClassName="w-auto max-w-32"
              className="px-1 text-xs"
              aria-describedby={describedBy}
              error={invalid}
              value={isMcpAllowedAll ? 'all' : 'list'}
              onChange={(event) =>
                onChange({ allowed_mcp_tools: event.target.value === 'all' ? 'all' : [] })
              }
            >
              <option value="all">All</option>
              <option value="list">Specific list</option>
            </NativeSelect>
            {!isMcpAllowedAll && (
              <ChipInput
                values={step.allowed_mcp_tools as string[]}
                onChange={(value) => onChange({ allowed_mcp_tools: value })}
                placeholder="server:tool..."
              />
            )}
          </div>
        )}
      </FormField>
      <FormField label="Blocked MCP Tools" group>
        {() => (
          <ChipInput
            values={step.blocked_mcp_tools || []}
            onChange={(value) => onChange({ blocked_mcp_tools: value })}
            placeholder="server:tool..."
          />
        )}
      </FormField>
    </div>
  )
}

function TransitionsSection({
  step,
  onChange,
  allStepNames,
}: {
  step: WorkflowStep
  onChange: (step: Partial<WorkflowStep>) => void
  allStepNames: string[]
}) {
  const transitions = step.transitions || []

  const updateTransition = (index: number, updates: Partial<WorkflowTransition>) => {
    onChange({
      transitions: transitions.map((transition, itemIndex) =>
        itemIndex === index ? { ...transition, ...updates } : transition,
      ),
    })
  }

  const addTransition = () => {
    const otherNames = allStepNames.filter((name) => name !== step.name)
    onChange({ transitions: [...transitions, { to: otherNames[0] || '', when: '' }] })
  }

  return (
    <div className="flex flex-col gap-1.5 border-t border-border pt-1.5">
      <Heading
        level={5}
        className="m-0 text-xs font-semibold uppercase tracking-wider text-[var(--text-muted)]"
      >
        Transitions
      </Heading>
      {transitions.map((transition, index) => (
        <div key={index} className="flex items-center gap-1.5">
          <NativeSelect
            wrapperClassName="w-30 shrink-0"
            className="px-1.5 text-sm"
            aria-label={`Transition ${index + 1} target`}
            value={transition.to}
            onChange={(event) => updateTransition(index, { to: event.target.value })}
          >
            <option value="">(select step)</option>
            {allStepNames
              .filter((name) => name !== step.name)
              .map((name) => (
                <option key={name} value={name}>
                  {name}
                </option>
              ))}
          </NativeSelect>
          <Input
            wrapperClassName="min-w-0 flex-1"
            className="px-1.5 text-sm"
            aria-label={`Transition ${index + 1} condition`}
            value={transition.when}
            onChange={(event) => updateTransition(index, { when: event.target.value })}
            placeholder="when expression..."
          />
          <Button
            type="button"
            variant="ghost"
            size="icon"
            dense
            className={`${coarseHitAreaCls} min-h-0 w-auto px-px text-sm leading-none hover:text-[var(--color-error)]`}
            onClick={() =>
              onChange({ transitions: transitions.filter((_, itemIndex) => itemIndex !== index) })
            }
            aria-label={`Remove transition ${index + 1}`}
          >
            &times;
          </Button>
        </div>
      ))}
      <Button
        type="button"
        size="sm"
        dense
        className={`${coarseHitAreaCls} self-start`}
        onClick={addTransition}
      >
        + Add Transition
      </Button>
    </div>
  )
}

type AdvancedFieldKey = 'on_enter' | 'on_exit' | 'on_mcp_success' | 'on_mcp_error'

function AdvancedJsonField({
  label,
  value,
  onCommit,
}: {
  label: string
  value: Record<string, unknown>[] | undefined
  onCommit: (value: Record<string, unknown>[]) => void
}) {
  const [draft, setDraft] = useState(value?.length ? JSON.stringify(value, null, 2) : '')
  const [error, setError] = useState<string | null>(null)

  const commitDraft = () => {
    const text = draft.trim()
    if (!text) {
      setError(null)
      onCommit([])
      return
    }
    try {
      const parsed: unknown = JSON.parse(text)
      if (!Array.isArray(parsed)) {
        setError('Value must be a JSON array')
        return
      }
      setError(null)
      onCommit(parsed as Record<string, unknown>[])
    } catch {
      setError('Invalid JSON')
    }
  }

  return (
    <div className="flex flex-col gap-1">
      <FormField label={label}>
        {({ id, describedBy }) => (
          <Textarea
            id={id}
            className="min-h-15 font-[inherit] text-xs"
            value={draft}
            onChange={(event) => setDraft(event.target.value)}
            onBlur={commitDraft}
            aria-describedby={describedBy}
            error={Boolean(error)}
            rows={3}
            placeholder="[]"
          />
        )}
      </FormField>
      {error && (
        <span
          className="rounded-md border border-destructive/40 bg-destructive/10 px-3 py-2 text-xs text-destructive-foreground"
          role="alert"
        >
          {error}
        </span>
      )}
    </div>
  )
}

function AdvancedSection({
  step,
  onChange,
}: {
  step: WorkflowStep
  onChange: (step: Partial<WorkflowStep>) => void
}) {
  const [expanded, setExpanded] = useState(false)
  const fields: { key: AdvancedFieldKey; label: string }[] = [
    { key: 'on_enter', label: 'on_enter' },
    { key: 'on_exit', label: 'on_exit' },
    { key: 'on_mcp_success', label: 'on_mcp_success' },
    { key: 'on_mcp_error', label: 'on_mcp_error' },
  ]

  return (
    <div className="flex flex-col gap-1.5 border-t border-border pt-1.5">
      <Button
        type="button"
        variant="ghost"
        size="sm"
        dense
        className={`${coarseHitAreaCls} self-start p-0 uppercase tracking-wider`}
        aria-expanded={expanded}
        onClick={() => setExpanded(!expanded)}
      >
        <span className="shrink-0 text-xs text-[var(--text-muted)]">
          {expanded ? '\u25BE' : '\u25B8'}
        </span>
        Advanced
      </Button>
      {expanded && (
        <div className="mt-1.5 flex flex-col gap-2">
          {fields.map(({ key, label }) => (
            <AdvancedJsonField
              key={key}
              label={label}
              value={step[key]}
              onCommit={(value) => onChange({ [key]: value })}
            />
          ))}
        </div>
      )}
    </div>
  )
}

export function AgentStepsEditor({ steps, onChange }: AgentStepsEditorProps) {
  const [expandedName, setExpandedName] = useState<string | null>(null)
  const allStepNames = steps.map((step) => step.name)

  const updateStep = useCallback(
    (index: number, updates: Partial<WorkflowStep>) => {
      onChange(steps.map((step, itemIndex) => (itemIndex === index ? { ...step, ...updates } : step)))
    },
    [steps, onChange],
  )

  const renameStep = useCallback(
    (index: number, newName: string) => {
      const oldName = steps[index].name
      onChange(
        steps.map((step, itemIndex) => ({
          ...step,
          ...(itemIndex === index ? { name: newName } : {}),
          transitions: step.transitions?.map((transition) =>
            transition.to === oldName ? { ...transition, to: newName } : transition,
          ),
        })),
      )
    },
    [steps, onChange],
  )

  const deleteStep = useCallback(
    (index: number) => {
      const name = steps[index].name
      onChange(steps.filter((_, itemIndex) => itemIndex !== index))
      if (expandedName === name) setExpandedName(null)
    },
    [steps, onChange, expandedName],
  )

  const moveStep = useCallback(
    (index: number, direction: -1 | 1) => {
      const target = index + direction
      if (target < 0 || target >= steps.length) return
      const next = [...steps]
      ;[next[index], next[target]] = [next[target], next[index]]
      onChange(next)
    },
    [steps, onChange],
  )

  const addStep = useCallback(() => {
    const step = createDefaultStep(steps)
    onChange([...steps, step])
    setExpandedName(step.name)
  }, [steps, onChange])

  return (
    <div className="flex flex-col gap-1.5">
      {steps.length === 0 && (
        <span className="text-sm italic text-[var(--text-muted)]">No steps defined</span>
      )}
      {steps.map((step, index) => {
        const isExpanded = expandedName === step.name
        return (
          <Card
            key={`${step.name}-${index}`}
            className={`overflow-hidden bg-[var(--bg-primary)] ${
              isExpanded ? 'border-[var(--accent)]' : ''
            }`}
          >
            <Button
              type="button"
              variant="ghost"
              dense
              className={`${coarseHitAreaCls} min-h-0 w-full justify-start rounded-none border-0 px-3 py-2 text-left font-[inherit] text-[inherit] hover:bg-[var(--bg-tertiary)]`}
              aria-expanded={isExpanded}
              onClick={() => setExpandedName(isExpanded ? null : step.name)}
            >
              <Chip tone="accent" className="text-sm">
                {step.name}
              </Chip>
              <span className="flex-1 overflow-hidden text-ellipsis whitespace-nowrap text-sm text-[var(--text-muted)]">
                {getStepPreview(step)}
              </span>
              <span className="shrink-0 text-xs text-[var(--text-muted)]">
                {isExpanded ? '\u25BE' : '\u25B8'}
              </span>
            </Button>
            {isExpanded && (
              <div className="flex flex-col gap-2.5 border-t border-border px-3 pt-2 pb-3">
                <div className="flex items-center gap-1.5">
                  <Button
                    type="button"
                    size="sm"
                    dense
                    className={coarseHitAreaCls}
                    onClick={() => moveStep(index, -1)}
                    disabled={index === 0}
                    title="Move up"
                  >
                    &uarr;
                  </Button>
                  <Button
                    type="button"
                    size="sm"
                    dense
                    className={coarseHitAreaCls}
                    onClick={() => moveStep(index, 1)}
                    disabled={index === steps.length - 1}
                    title="Move down"
                  >
                    &darr;
                  </Button>
                  <Button
                    type="button"
                    variant="destructive"
                    size="sm"
                    dense
                    className={coarseHitAreaCls}
                    onClick={() => deleteStep(index)}
                  >
                    Delete
                  </Button>
                </div>
                <FormField label="Name">
                  {({ id, describedBy, invalid }) => (
                    <Input
                      id={id}
                      aria-describedby={describedBy}
                      error={invalid}
                      value={step.name}
                      onChange={(event) => {
                        const newName = event.target.value
                        renameStep(index, newName)
                        setExpandedName(newName)
                      }}
                    />
                  )}
                </FormField>
                <FormField label="Description">
                  {({ id, describedBy, invalid }) => (
                    <Textarea
                      id={id}
                      aria-describedby={describedBy}
                      error={invalid}
                      value={step.description || ''}
                      onChange={(event) =>
                        updateStep(index, { description: event.target.value || null })
                      }
                      placeholder="What this step does..."
                      rows={2}
                    />
                  )}
                </FormField>
                <FormField label="Status Message">
                  {({ id, describedBy, invalid }) => (
                    <Textarea
                      id={id}
                      aria-describedby={describedBy}
                      error={invalid}
                      value={step.status_message || ''}
                      onChange={(event) =>
                        updateStep(index, { status_message: event.target.value || null })
                      }
                      placeholder="Shown while step is active..."
                      rows={2}
                    />
                  )}
                </FormField>
                <ToolGatingSection
                  step={step}
                  onChange={(updates) => updateStep(index, updates)}
                />
                <TransitionsSection
                  step={step}
                  onChange={(updates) => updateStep(index, updates)}
                  allStepNames={allStepNames}
                />
                <FormField label="Exit When">
                  {({ id, describedBy, invalid }) => (
                    <Input
                      id={id}
                      aria-describedby={describedBy}
                      error={invalid}
                      value={step.exit_when || ''}
                      onChange={(event) =>
                        updateStep(index, { exit_when: event.target.value || null })
                      }
                      placeholder="Expression to auto-exit this step..."
                    />
                  )}
                </FormField>
                <AdvancedSection
                  step={step}
                  onChange={(updates) => updateStep(index, updates)}
                />
              </div>
            )}
          </Card>
        )
      })}
      <Button
        type="button"
        size="sm"
        dense
        className={`${coarseHitAreaCls} self-start`}
        onClick={addStep}
      >
        + Add Step
      </Button>
    </div>
  )
}
