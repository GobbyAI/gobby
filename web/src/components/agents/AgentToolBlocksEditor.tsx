import { useState } from 'react'
import { Button } from '../ui/Button'
import { Chip } from '../ui/Chip'
import { FormField } from '../ui/FormField'
import { Input } from '../ui/Input'
import { coarseHitAreaCls } from '../ui/controlStyles'

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
          aria-label="Add blocked tool"
        >
          +
        </Button>
      </div>
    </div>
  )
}

interface AgentToolBlocksEditorProps {
  blockedTools: string[]
  onBlockedToolsChange?: (tools: string[]) => void
  blockedMcpTools: string[]
  onBlockedMcpToolsChange?: (tools: string[]) => void
}

export function AgentToolBlocksEditor({
  blockedTools,
  onBlockedToolsChange,
  blockedMcpTools,
  onBlockedMcpToolsChange,
}: AgentToolBlocksEditorProps) {
  return (
    <div className="flex flex-col gap-3">
      {onBlockedToolsChange && (
        <FormField label="Blocked Native Tools" group>
          {() => (
            <ChipInput
              values={blockedTools}
              onChange={onBlockedToolsChange}
              placeholder="e.g. Edit, Write, Bash"
            />
          )}
        </FormField>
      )}
      {onBlockedMcpToolsChange && (
        <FormField label="Blocked MCP Tools" group>
          {() => (
            <ChipInput
              values={blockedMcpTools}
              onChange={onBlockedMcpToolsChange}
              placeholder="e.g. gobby-tasks-ops:submit_for_review"
            />
          )}
        </FormField>
      )}
    </div>
  )
}
