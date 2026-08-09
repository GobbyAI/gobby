import { useId, useState } from 'react'
import { cn } from '../../../lib/utils'
import { Button } from '../../ui/Button'
import { FormField } from '../../ui/FormField'
import { Input } from '../../ui/Input'
import { Textarea } from '../../ui/Textarea'
import { coarseHitAreaCls } from '../../ui/controlStyles'
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
    <FormField label="Command" className="mb-2.5 [&>label:first-child]:text-xs">
      {({ id, describedBy, invalid }) => (
        <Textarea
          id={id}
          aria-describedby={describedBy}
          error={invalid}
          className="min-h-[50px] resize-y font-mono text-sm"
          value={(step.exec as string) ?? ''}
          onChange={(e) => onChange({ exec: e.target.value })}
          placeholder="shell command"
          rows={3}
        />
      )}
    </FormField>
  )
}

export function PromptFields({ step, onChange }: StepFieldsProps) {
  return (
    <FormField label="Prompt" className="mb-2.5 [&>label:first-child]:text-xs">
      {({ id, describedBy, invalid }) => (
        <Textarea
          id={id}
          aria-describedby={describedBy}
          error={invalid}
          className="min-h-[50px] resize-y font-[inherit] text-md"
          value={(step.prompt as string) ?? ''}
          onChange={(e) => onChange({ prompt: e.target.value })}
          placeholder="LLM prompt text"
          rows={4}
        />
      )}
    </FormField>
  )
}

export function McpFields({ step, onChange }: StepFieldsProps) {
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
      <FormField label="Server" className="mb-2.5 [&>label:first-child]:text-xs">
        {({ id, describedBy, invalid }) => (
          <Input
            id={id}
            aria-describedby={describedBy}
            error={invalid}
            type="text"
            value={(mcp.server as string) ?? ''}
            onChange={(e) => setMcpField('server', e.target.value)}
          />
        )}
      </FormField>
      <FormField label="Tool" className="mb-2.5 [&>label:first-child]:text-xs">
        {({ id, describedBy, invalid }) => (
          <Input
            id={id}
            aria-describedby={describedBy}
            error={invalid}
            type="text"
            value={(mcp.tool as string) ?? ''}
            onChange={(e) => setMcpField('tool', e.target.value)}
          />
        )}
      </FormField>
      <FormField label="Arguments" group className="mb-2.5 [&>span:first-child]:text-xs">
        {() => <KeyValueEditor sectionName="Arguments" pairs={argPairs} onChange={setArgs} />}
      </FormField>
    </>
  )
}

export function InvokePipelineFields({ step, onChange }: StepFieldsProps) {
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
      <FormField label="Pipeline Name" className="mb-2.5 [&>label:first-child]:text-xs">
        {({ id, describedBy, invalid }) => (
          <Input
            id={id}
            aria-describedby={describedBy}
            error={invalid}
            type="text"
            value={name}
            onChange={(e) => setName(e.target.value)}
            placeholder="pipeline-name"
          />
        )}
      </FormField>
      <FormField label="Arguments" group className="mb-2.5 [&>span:first-child]:text-xs">
        {() => <KeyValueEditor sectionName="Arguments" pairs={argPairs} onChange={setArgs} />}
      </FormField>
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
  const approvalId = useId()
  const conditionValue = stripTemplateWrapper((step.condition as string) ?? '')
  const toolsValue = Array.isArray(step.tools) ? (step.tools as string[]).join(', ') : ''

  return (
    <div className="mt-2 border-t border-border pt-2">
      <FormField label="Condition" className="mb-2.5 [&>label:first-child]:text-xs">
        {({ id, describedBy, invalid }) => (
          <DraftTextInput
            key={conditionValue}
            id={id}
            describedBy={describedBy}
            invalid={invalid}
            value={conditionValue}
            onCommit={(draft) => {
              const value = draft.trim()
              onChange({ condition: value ? wrapTemplateExpr(value) : undefined })
              return value
            }}
            placeholder="e.g. inputs.mode == 'deploy'"
          />
        )}
      </FormField>

      <FormField label="Input" className="mb-2.5 [&>label:first-child]:text-xs">
        {({ id, describedBy, invalid }) => (
          <Input
            id={id}
            aria-describedby={describedBy}
            error={invalid}
            type="text"
            value={(step.input as string) ?? ''}
            onChange={(e) => onChange({ input: e.target.value || undefined })}
            placeholder="e.g. $prev_step.output"
          />
        )}
      </FormField>

      {type === 'prompt' && (
        <FormField label="Tools" className="mb-2.5 [&>label:first-child]:text-xs">
          {({ id, describedBy, invalid }) => (
            <DraftTextInput
              key={toolsValue}
              id={id}
              describedBy={describedBy}
              invalid={invalid}
              value={toolsValue}
              onCommit={(draft) => {
                const tools = draft
                  .split(',')
                  .map((item) => item.trim())
                  .filter(Boolean)
                onChange({ tools: tools.length > 0 ? tools : undefined })
                return tools.join(', ')
              }}
              placeholder="Comma-separated tool list"
            />
          )}
        </FormField>
      )}

      <div className="mb-2.5 flex items-center gap-1.5 text-sm">
        <Input
          id={approvalId}
          type="checkbox"
          wrapperClassName="w-auto"
          className="size-4 h-4 rounded p-0"
          checked={!!approval?.required}
          onChange={(e) => {
            if (e.target.checked) {
              onChange({ approval: { required: true, message: '', timeout: 0 } })
            } else {
              onChange({ approval: undefined })
            }
          }}
        />
        <label htmlFor={approvalId} className="cursor-pointer">
          Requires approval
        </label>
      </div>

      {!!approval?.required && (
        <>
          <FormField label="Approval Message" className="mb-2.5 [&>label:first-child]:text-xs">
            {({ id, describedBy, invalid }) => (
              <Input
                id={id}
                aria-describedby={describedBy}
                error={invalid}
                type="text"
                value={(approval.message as string) ?? ''}
                onChange={(e) =>
                  onChange({ approval: { ...approval, message: e.target.value } })
                }
                placeholder="Approval prompt message"
              />
            )}
          </FormField>
          <FormField label="Timeout (seconds)" className="mb-2.5 [&>label:first-child]:text-xs">
            {({ id, describedBy, invalid }) => (
              <Input
                id={id}
                aria-describedby={describedBy}
                error={invalid}
                type="number"
                value={(approval.timeout as number) ?? 0}
                onChange={(e) =>
                  onChange({ approval: { ...approval, timeout: Number(e.target.value) || 0 } })
                }
                min={0}
              />
            )}
          </FormField>
        </>
      )}
    </div>
  )
}

function DraftTextInput({
  id,
  describedBy,
  invalid,
  value,
  onCommit,
  placeholder,
}: {
  id: string
  describedBy?: string
  invalid: boolean
  value: string
  onCommit: (draft: string) => string
  placeholder: string
}) {
  const [draft, setDraft] = useState(value)

  return (
    <Input
      id={id}
      aria-describedby={describedBy}
      error={invalid}
      type="text"
      value={draft}
      onChange={(event) => setDraft(event.target.value)}
      onBlur={() => setDraft(onCommit(draft))}
      placeholder={placeholder}
    />
  )
}

function KeyValueEditor(props: {
  sectionName: string
  pairs: KVPair[]
  onChange: (pairs: KVPair[]) => void
}) {
  return <KeyValueDraftEditor key={JSON.stringify(props.pairs)} {...props} />
}

function KeyValueDraftEditor({
  sectionName,
  pairs,
  onChange,
}: {
  sectionName: string
  pairs: KVPair[]
  onChange: (pairs: KVPair[]) => void
}) {
  const [draftPairs, setDraftPairs] = useState(pairs)

  return (
    <div className="flex flex-col gap-1">
      {draftPairs.map((pair, index) => (
        <div key={index} className="flex items-center gap-1">
          <Input
            type="text"
            wrapperClassName="min-w-0 flex-1"
            className="h-8 rounded px-2 py-1 text-sm"
            value={pair.key}
            aria-label={`${sectionName} key ${index + 1}`}
            onChange={(e) => {
              const next = [...draftPairs]
              next[index] = { ...next[index], key: e.target.value }
              setDraftPairs(next)
            }}
            onBlur={() => onChange(draftPairs)}
            placeholder="key"
          />
          <Input
            type="text"
            wrapperClassName="min-w-0 flex-1"
            className="h-8 rounded px-2 py-1 text-sm"
            value={pair.value}
            aria-label={`${sectionName} value ${index + 1}`}
            onChange={(e) => {
              const next = [...draftPairs]
              next[index] = { ...next[index], value: e.target.value }
              setDraftPairs(next)
            }}
            onBlur={() => onChange(draftPairs)}
            placeholder="value"
          />
          <Button
            type="button"
            variant="destructive"
            size="icon"
            className={cn(coarseHitAreaCls, 'size-7 min-h-7 shrink-0 border-border')}
            onClick={() => {
              const next = draftPairs.filter((_, pairIndex) => pairIndex !== index)
              setDraftPairs(next)
              onChange(next)
            }}
            aria-label={`Remove ${sectionName} row ${index + 1}`}
          >
            &times;
          </Button>
        </div>
      ))}
      <Button
        type="button"
        variant="ghost"
        size="sm"
        className={cn(coarseHitAreaCls, 'justify-start border-dashed border-border')}
        onClick={() => setDraftPairs([...draftPairs, { key: '', value: '' }])}
        aria-label={`Add ${sectionName} row`}
      >
        + Add
      </Button>
    </div>
  )
}
