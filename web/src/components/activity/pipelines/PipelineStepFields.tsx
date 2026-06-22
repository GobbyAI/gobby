import { useId } from 'react'
import {
  CHECKBOX_LABEL_CLS,
  COMMON_CLS,
  FIELD_CLS,
  FIELD_INPUT_CLS,
  FIELD_LABEL_CLS,
  FIELD_TEXTAREA_CLS,
  FIELD_TEXTAREA_MONO_CLS,
  KV_ADD_CLS,
  KV_CLS,
  KV_INPUT_CLS,
  KV_REMOVE_CLS,
  KV_ROW_CLS,
} from './PipelineEditor.styles'
import type {
  KVPair,
  PipelineStep,
  StepChangeHandler,
  StepType,
} from './PipelineEditor.types'
import { stripTemplateWrapper, wrapTemplateExpr } from './pipelineStepModel'

type StepFieldsProps = {
  step: PipelineStep
  onChange: StepChangeHandler
}

export function ExecFields({ step, onChange }: StepFieldsProps) {
  return (
    <label className={FIELD_CLS}>
      <span className={FIELD_LABEL_CLS}>Command</span>
      <textarea
        className={FIELD_TEXTAREA_MONO_CLS}
        value={(step.exec as string) ?? ''}
        onChange={(e) => onChange({ exec: e.target.value })}
        placeholder="shell command"
        rows={3}
      />
    </label>
  )
}

export function PromptFields({ step, onChange }: StepFieldsProps) {
  return (
    <label className={FIELD_CLS}>
      <span className={FIELD_LABEL_CLS}>Prompt</span>
      <textarea
        className={FIELD_TEXTAREA_CLS}
        value={(step.prompt as string) ?? ''}
        onChange={(e) => onChange({ prompt: e.target.value })}
        placeholder="LLM prompt text"
        rows={4}
      />
    </label>
  )
}

export function McpFields({ step, onChange }: StepFieldsProps) {
  const argumentsLabelId = useId()
  const mcp = (step.mcp as Record<string, unknown>) ?? {}
  const args = (mcp.arguments as Record<string, string>) ?? {}

  const setMcpField = (key: string, value: unknown) => {
    onChange({ mcp: { ...mcp, [key]: value } })
  }

  const argPairs: KVPair[] = Object.entries(args).map(([key, value]) => ({
    key,
    value: String(value),
  }))

  const setArgs = (pairs: KVPair[]) => {
    const obj: Record<string, string> = {}
    for (const pair of pairs) if (pair.key.trim()) obj[pair.key] = pair.value
    setMcpField('arguments', obj)
  }

  return (
    <>
      <label className={FIELD_CLS}>
        <span className={FIELD_LABEL_CLS}>Server</span>
        <input
          type="text"
          className={FIELD_INPUT_CLS}
          value={(mcp.server as string) ?? ''}
          onChange={(e) => setMcpField('server', e.target.value)}
        />
      </label>
      <label className={FIELD_CLS}>
        <span className={FIELD_LABEL_CLS}>Tool</span>
        <input
          type="text"
          className={FIELD_INPUT_CLS}
          value={(mcp.tool as string) ?? ''}
          onChange={(e) => setMcpField('tool', e.target.value)}
        />
      </label>
      <div className={FIELD_CLS} role="group" aria-labelledby={argumentsLabelId}>
        <span id={argumentsLabelId} className={FIELD_LABEL_CLS}>
          Arguments
        </span>
        <KeyValueEditor sectionName="Arguments" pairs={argPairs} onChange={setArgs} />
      </div>
    </>
  )
}

export function InvokePipelineFields({ step, onChange }: StepFieldsProps) {
  const argumentsLabelId = useId()
  const raw = step.invoke_pipeline
  const isObject = typeof raw === 'object' && raw !== null
  const rawObject = isObject ? (raw as Record<string, unknown>) : {}
  const name = isObject ? ((rawObject.name as string) ?? '') : ((raw as string) ?? '')
  const args = isObject ? ((rawObject.arguments as Record<string, string>) ?? {}) : {}
  const argPairs: KVPair[] = Object.entries(args).map(([key, value]) => ({
    key,
    value: String(value),
  }))

  const setName = (nextName: string) => {
    if (isObject || argPairs.length > 0) {
      onChange({ invoke_pipeline: { ...rawObject, name: nextName } })
    } else {
      onChange({ invoke_pipeline: nextName })
    }
  }

  const setArgs = (pairs: KVPair[]) => {
    const obj: Record<string, string> = {}
    for (const pair of pairs) if (pair.key.trim()) obj[pair.key] = pair.value
    if (Object.keys(obj).length === 0 && !isObject) return
    onChange({ invoke_pipeline: { name, arguments: obj } })
  }

  return (
    <>
      <label className={FIELD_CLS}>
        <span className={FIELD_LABEL_CLS}>Pipeline Name</span>
        <input
          type="text"
          className={FIELD_INPUT_CLS}
          value={name}
          onChange={(e) => setName(e.target.value)}
          placeholder="pipeline-name"
        />
      </label>
      <div className={FIELD_CLS} role="group" aria-labelledby={argumentsLabelId}>
        <span id={argumentsLabelId} className={FIELD_LABEL_CLS}>
          Arguments
        </span>
        <KeyValueEditor sectionName="Arguments" pairs={argPairs} onChange={setArgs} />
      </div>
    </>
  )
}

export function ActivateWorkflowFields({ step, onChange }: StepFieldsProps) {
  const activateWorkflow = (step.activate_workflow as Record<string, unknown>) ?? {}
  const vars = (activateWorkflow.variables as Record<string, string>) ?? {}

  const setActivateWorkflowField = (key: string, value: unknown) => {
    onChange({ activate_workflow: { ...activateWorkflow, [key]: value } })
  }

  const varPairs: KVPair[] = Object.entries(vars).map(([key, value]) => ({
    key,
    value: String(value),
  }))

  const setVars = (pairs: KVPair[]) => {
    const obj: Record<string, string> = {}
    for (const pair of pairs) if (pair.key.trim()) obj[pair.key] = pair.value
    setActivateWorkflowField('variables', obj)
  }

  return (
    <>
      <label className={FIELD_CLS}>
        <span className={FIELD_LABEL_CLS}>Workflow Name</span>
        <input
          type="text"
          className={FIELD_INPUT_CLS}
          value={(activateWorkflow.name as string) ?? ''}
          onChange={(e) => setActivateWorkflowField('name', e.target.value)}
        />
      </label>
      <label className={FIELD_CLS}>
        <span className={FIELD_LABEL_CLS}>Session ID</span>
        <input
          type="text"
          className={FIELD_INPUT_CLS}
          value={(activateWorkflow.session_id as string) ?? ''}
          onChange={(e) => setActivateWorkflowField('session_id', e.target.value)}
          placeholder="Optional"
        />
      </label>
      <div className={FIELD_CLS} role="group" aria-label="Variables">
        <span className={FIELD_LABEL_CLS}>Variables</span>
        <KeyValueEditor sectionName="Variables" pairs={varPairs} onChange={setVars} />
      </div>
    </>
  )
}

export function CommonFields({
  step,
  type,
  onChange,
}: {
  step: PipelineStep
  type: StepType
  onChange: StepChangeHandler
}) {
  const approval = step.approval as Record<string, unknown> | undefined

  return (
    <div className={COMMON_CLS}>
      <label className={FIELD_CLS}>
        <span className={FIELD_LABEL_CLS}>Condition</span>
        <input
          type="text"
          className={FIELD_INPUT_CLS}
          value={stripTemplateWrapper((step.condition as string) ?? '')}
          onChange={(e) => {
            const val = e.target.value.trim()
            onChange({ condition: val ? wrapTemplateExpr(val) : undefined })
          }}
          placeholder="e.g. inputs.mode == 'deploy'"
        />
      </label>

      <label className={FIELD_CLS}>
        <span className={FIELD_LABEL_CLS}>Input</span>
        <input
          type="text"
          className={FIELD_INPUT_CLS}
          value={(step.input as string) ?? ''}
          onChange={(e) => onChange({ input: e.target.value || undefined })}
          placeholder="e.g. $prev_step.output"
        />
      </label>

      {type === 'prompt' && (
        <label className={FIELD_CLS}>
          <span className={FIELD_LABEL_CLS}>Tools</span>
          <input
            type="text"
            className={FIELD_INPUT_CLS}
            value={Array.isArray(step.tools) ? (step.tools as string[]).join(', ') : ''}
            onChange={(e) => {
              const val = e.target.value.trim()
              onChange({
                tools: val
                  ? val
                    .split(',')
                    .map((item) => item.trim())
                    .filter(Boolean)
                  : undefined,
              })
            }}
            placeholder="Comma-separated tool list"
          />
        </label>
      )}

      <div className={FIELD_CLS}>
        <label className={CHECKBOX_LABEL_CLS}>
          <input
            type="checkbox"
            checked={!!approval?.required}
            onChange={(e) => {
              if (e.target.checked) {
                onChange({ approval: { required: true, message: '', timeout: 0 } })
              } else {
                onChange({ approval: undefined })
              }
            }}
          />
          Requires approval
        </label>
      </div>

      {!!approval?.required && (
        <>
          <label className={FIELD_CLS}>
            <span className={FIELD_LABEL_CLS}>Approval Message</span>
            <input
              type="text"
              className={FIELD_INPUT_CLS}
              value={(approval.message as string) ?? ''}
              onChange={(e) =>
                onChange({ approval: { ...approval, message: e.target.value } })
              }
              placeholder="Approval prompt message"
            />
          </label>
          <label className={FIELD_CLS}>
            <span className={FIELD_LABEL_CLS}>Timeout (seconds)</span>
            <input
              type="number"
              className={FIELD_INPUT_CLS}
              value={(approval.timeout as number) ?? 0}
              onChange={(e) =>
                onChange({ approval: { ...approval, timeout: Number(e.target.value) || 0 } })
              }
              min={0}
            />
          </label>
        </>
      )}
    </div>
  )
}

function KeyValueEditor({
  sectionName,
  pairs,
  onChange,
}: {
  sectionName: string
  pairs: KVPair[]
  onChange: (pairs: KVPair[]) => void
}) {
  return (
    <div className={KV_CLS}>
      {pairs.map((pair, index) => (
        <div key={index} className={KV_ROW_CLS}>
          <input
            type="text"
            className={KV_INPUT_CLS}
            value={pair.key}
            aria-label={`${sectionName} key ${index + 1}`}
            onChange={(e) => {
              const next = [...pairs]
              next[index] = { ...next[index], key: e.target.value }
              onChange(next)
            }}
            placeholder="key"
          />
          <input
            type="text"
            className={KV_INPUT_CLS}
            value={pair.value}
            aria-label={`${sectionName} value ${index + 1}`}
            onChange={(e) => {
              const next = [...pairs]
              next[index] = { ...next[index], value: e.target.value }
              onChange(next)
            }}
            placeholder="value"
          />
          <button
            type="button"
            className={KV_REMOVE_CLS}
            onClick={() => onChange(pairs.filter((_, pairIndex) => pairIndex !== index))}
            aria-label={`Remove ${sectionName} row ${index + 1}`}
          >
            &times;
          </button>
        </div>
      ))}
      <button
        type="button"
        className={KV_ADD_CLS}
        onClick={() => onChange([...pairs, { key: '', value: '' }])}
        aria-label={`Add ${sectionName} row`}
      >
        + Add
      </button>
    </div>
  )
}
