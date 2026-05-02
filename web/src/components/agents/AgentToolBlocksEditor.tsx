import { useState } from 'react'

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
    <div className="step-chip-input">
      <div className="step-chips">
        {values.map(v => (
          <span key={v} className="step-chip">
            {v}
            <button type="button" className="step-chip-remove" onClick={() => onChange(values.filter(x => x !== v))}>&times;</button>
          </span>
        ))}
      </div>
      <div className="step-chip-add-row">
        <input
          className="agent-edit-input step-chip-field"
          value={input}
          onChange={e => setInput(e.target.value)}
          onKeyDown={e => { if (e.key === 'Enter') { e.preventDefault(); handleAdd() } }}
          placeholder={placeholder}
        />
        <button type="button" className="agent-defs-btn step-chip-add-btn" onClick={handleAdd} disabled={!input.trim()}>+</button>
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
    <div className="agent-tool-blocks-editor">
      {onBlockedToolsChange && (
        <div className="agent-edit-field">
          <span className="agent-edit-label">Blocked Native Tools</span>
          <ChipInput
            values={blockedTools}
            onChange={onBlockedToolsChange}
            placeholder="e.g. Edit, Write, Bash"
          />
        </div>
      )}
      {onBlockedMcpToolsChange && (
        <div className="agent-edit-field">
          <span className="agent-edit-label">Blocked MCP Tools</span>
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
