import { useEffect, useRef, useCallback } from 'react'
import { Terminal as XTerm } from '@xterm/xterm'
import { FitAddon } from '@xterm/addon-fit'
import '@xterm/xterm/css/xterm.css'

interface TerminalProps {
  runId: string | null
  readOnly?: boolean
  onInput: (runId: string, data: string) => void
  onOutput: (callback: (runId: string, data: string) => void) => void
}

export function Terminal({ runId, readOnly, onInput, onOutput }: TerminalProps) {
  const containerRef = useRef<HTMLDivElement>(null)
  const terminalRef = useRef<XTerm | null>(null)
  const fitAddonRef = useRef<FitAddon | null>(null)
  const currentRunIdRef = useRef<string | null>(null)

  // Initialize terminal
  useEffect(() => {
    if (!containerRef.current) return

    const terminal = new XTerm({
      fontFamily: "'SF Mono', 'Fira Code', 'JetBrains Mono', monospace",
      fontSize: 14,
      theme: {
        background: '#0a0a0a',
        foreground: '#e5e5e5',
        cursor: '#3b82f6',
        cursorAccent: '#0a0a0a',
        selectionBackground: '#3b82f680',
        black: '#e5e5e5',
        red: '#f87171',
        green: '#4ade80',
        yellow: '#facc15',
        blue: '#3b82f6',
        magenta: '#c084fc',
        cyan: '#22d3ee',
        white: '#e5e5e5',
        brightBlack: '#737373',
        brightRed: '#fca5a5',
        brightGreen: '#86efac',
        brightYellow: '#fde047',
        brightBlue: '#60a5fa',
        brightMagenta: '#d8b4fe',
        brightCyan: '#67e8f9',
        brightWhite: '#ffffff',
      },
      cursorBlink: true,
      scrollback: 10000,
      convertEol: true,
      minimumContrastRatio: 4.5,
    })

    const fitAddon = new FitAddon()
    terminal.loadAddon(fitAddon)

    terminal.open(containerRef.current)

    // Suppress iOS QuickType/autofill bar and Android autofill above keyboard
    if (terminal.textarea) {
      terminal.textarea.setAttribute('autocomplete', 'off')
      terminal.textarea.setAttribute('autocorrect', 'off')
      terminal.textarea.setAttribute('autocapitalize', 'off')
      terminal.textarea.setAttribute('spellcheck', 'false')
      terminal.textarea.setAttribute('data-form-type', 'other')
    }

    fitAddon.fit()

    terminalRef.current = terminal
    fitAddonRef.current = fitAddon

    // Handle resize
    const resizeObserver = new ResizeObserver(() => {
      fitAddon.fit()
    })
    resizeObserver.observe(containerRef.current)

    // Handle window resize
    const handleResize = () => fitAddon.fit()
    window.addEventListener('resize', handleResize)

    return () => {
      resizeObserver.disconnect()
      window.removeEventListener('resize', handleResize)
      terminal.dispose()
    }
  }, [])

  // Handle input from terminal (disabled in read-only mode)
  useEffect(() => {
    if (!terminalRef.current || readOnly) return

    const terminal = terminalRef.current
    const disposable = terminal.onData((data) => {
      if (currentRunIdRef.current) {
        onInput(currentRunIdRef.current, data)
      }
    })

    return () => disposable.dispose()
  }, [onInput, readOnly])

  // Handle output callback
  const handleOutput = useCallback((outputRunId: string, data: string) => {
    // Only write output for the currently selected agent
    if (outputRunId === currentRunIdRef.current && terminalRef.current) {
      terminalRef.current.write(data)
    }
  }, [])

  // Register output handler
  useEffect(() => {
    onOutput(handleOutput)
  }, [onOutput, handleOutput])

  // Update current run ID and clear terminal on agent change
  useEffect(() => {
    currentRunIdRef.current = runId
    if (terminalRef.current) {
      terminalRef.current.clear()
      if (runId) {
        terminalRef.current.writeln(`\x1b[90m--- Connected to agent ${runId} ---\x1b[0m`)
      } else {
        terminalRef.current.writeln('\x1b[90m--- No agent connected ---\x1b[0m')
        terminalRef.current.writeln('\x1b[90mSpawn an embedded agent to see output here.\x1b[0m')
      }
    }
  }, [runId])

  return (
    <div className="terminal-container">
      <div ref={containerRef} className="terminal-content" />
    </div>
  )
}

interface AgentSelectorProps {
  agents: Array<{
    run_id: string
    provider: string
    pid?: number
    mode?: string
  }>
  selectedAgent: string | null
  onSelect: (runId: string | null) => void
}

export function AgentSelector({ agents, selectedAgent, onSelect }: AgentSelectorProps) {
  if (agents.length === 0) {
    return (
      <div className="agent-selector">
        <span className="no-agents">No agents running</span>
      </div>
    )
  }

  return (
    <div className="agent-selector">
      <select
        value={selectedAgent || ''}
        onChange={(e) => onSelect(e.target.value || null)}
        className="agent-select"
      >
        <option value="">Select agent...</option>
        {agents.map((agent) => {
          const modeLabel = agent.mode === 'tmux' ? ' [tmux]' : ''
          return (
            <option key={agent.run_id} value={agent.run_id}>
              {agent.provider}{modeLabel} (PID: {agent.pid || 'unknown'}) - {agent.run_id.slice(0, 8)}
            </option>
          )
        })}
      </select>
    </div>
  )
}

interface TerminalPanelProps {
  isOpen: boolean
  onToggle: () => void
  agents: Array<{
    run_id: string
    provider: string
    pid?: number
    mode?: string
  }>
  selectedAgent: string | null
  onSelectAgent: (runId: string | null) => void
  onInput: (runId: string, data: string) => void
  onOutput: (callback: (runId: string, data: string) => void) => void
}

export function TerminalPanel({
  isOpen,
  onToggle,
  agents,
  selectedAgent,
  onSelectAgent,
  onInput,
  onOutput,
}: TerminalPanelProps) {
  // Determine if selected agent is read-only (tmux agents are read-only in chat panel)
  const selectedAgentInfo = agents.find(a => a.run_id === selectedAgent)
  const isReadOnly = selectedAgentInfo?.mode === 'tmux'

  const hasAgents = agents.length > 0
  const canOpen = isOpen && hasAgents

  return (
    <div className={`terminal-panel ${canOpen ? 'open' : 'collapsed'} ${!hasAgents ? 'disabled' : ''}`}>
      <div className="terminal-header" onClick={hasAgents ? onToggle : undefined}>
        <span className="terminal-title">
          <TerminalIcon />
          Active Agents
          <span className="agent-count">{agents.length}</span>
          {isOpen && isReadOnly && (
            <span className="mode-badge mode-view">VIEW</span>
          )}
        </span>
        <div className="terminal-actions" onClick={(e) => e.stopPropagation()}>
          {isOpen && (
            <AgentSelector
              agents={agents}
              selectedAgent={selectedAgent}
              onSelect={onSelectAgent}
            />
          )}
          <button className="terminal-toggle" onClick={onToggle}>
            {isOpen ? '\u25BC' : '\u25B2'}
          </button>
        </div>
      </div>
      {canOpen && (
        <Terminal
          runId={selectedAgent}
          readOnly={isReadOnly}
          onInput={onInput}
          onOutput={onOutput}
        />
      )}
    </div>
  )
}

function TerminalIcon() {
  return (
    <svg
      width="16"
      height="16"
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="2"
      strokeLinecap="round"
      strokeLinejoin="round"
    >
      <polyline points="4 17 10 11 4 5" />
      <line x1="12" y1="19" x2="20" y2="19" />
    </svg>
  )
}
