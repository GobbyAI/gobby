import { useState } from 'react'
import {
  AGENT_BTN_CLS,
  AGENT_EDIT_FIELD_CLS,
  AGENT_EDIT_INPUT_CLS,
  AGENT_EDIT_LABEL_CLS,
  STEP_CHIP_ADD_BTN_CLS,
  STEP_CHIP_ADD_ROW_CLS,
  STEP_CHIP_CLS,
  STEP_CHIP_FIELD_CLS,
  STEP_CHIP_INPUT_CLS,
  STEP_CHIP_REMOVE_CLS,
  STEP_CHIPS_CLS,
} from './agents-styles'

function ChipInput({ values, onChange, placeholder }: {
  values: string[]
  onChange: (values: string[]) => void
  placeholder?: string
}) {
  const [input, setInput] = useState('')

  const handleAdd = () => {
    const v = input.trim()
    if (v && !values.includes(v)) {
      onChange([...values, v])
    }
    setInput('')
  }

  return (
    <div className={STEP_CHIP_INPUT_CLS}>
      <div className={STEP_CHIPS_CLS}>
        {values.map(v => (
          <span key={v} className={STEP_CHIP_CLS}>
            {v}
            <button type="button" className={STEP_CHIP_REMOVE_CLS} onClick={() => onChange(values.filter(x => x !== v))}>&times;</button>
          </span>
        ))}
      </div>
      <div className={STEP_CHIP_ADD_ROW_CLS}>
        <input
          className={`${AGENT_EDIT_INPUT_CLS} ${STEP_CHIP_FIELD_CLS}`}
          value={input}
          onChange={e => setInput(e.target.value)}
          onKeyDown={e => { if (e.key === 'Enter') { e.preventDefault(); handleAdd() } }}
          placeholder={placeholder}
        />
        <button type="button" className={`${AGENT_BTN_CLS} ${STEP_CHIP_ADD_BTN_CLS}`} onClick={handleAdd} disabled={!input.trim()}>+</button>
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
    <div>
      {onBlockedToolsChange && (
        <div className={AGENT_EDIT_FIELD_CLS}>
          <span className={AGENT_EDIT_LABEL_CLS}>Blocked Native Tools</span>
          <ChipInput
            values={blockedTools}
            onChange={onBlockedToolsChange}
            placeholder="e.g. Edit, Write, Bash"
          />
        </div>
      )}
      {onBlockedMcpToolsChange && (
        <div className={AGENT_EDIT_FIELD_CLS}>
          <span className={AGENT_EDIT_LABEL_CLS}>Blocked MCP Tools</span>
          <ChipInput
            values={blockedMcpTools}
            onChange={onBlockedMcpToolsChange}
            placeholder="e.g. gobby-tasks-ops:submit_for_review"
          />
        </div>
      )}
    </div>
  )
}
