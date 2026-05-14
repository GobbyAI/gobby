import { useState } from 'react'
import { SidebarPanel } from '../shared/SidebarPanel'
import type { SpanRecord } from '../../hooks/useTraces'
import { parseLLMAttributes, formatTokenCount } from './llm-utils'
import { cn } from '../../lib/utils'
import { Heading } from '../shared/Heading'

interface TraceDetailProps {
  isOpen: boolean
  onClose: () => void
  span?: SpanRecord
}

const SECTION_HEADING_CLS =
  'mb-3 flex items-center justify-between gap-2 border-b border-[var(--border)] pb-2 text-[length:var(--text-base)] text-[var(--text-primary)]'

const TOGGLE_BUTTON_CLS =
  'shrink-0 cursor-pointer rounded-sm border border-[var(--border)] bg-transparent px-1.5 py-0.5 text-[length:var(--text-xs)] font-normal text-[var(--text-secondary)] hover:text-[var(--text-primary)] pointer-coarse:min-h-11 pointer-coarse:px-3'

const TABLE_CLS = 'w-full border-collapse text-[length:var(--text-md)]'
const TH_CLS =
  'w-[30%] border-b border-[var(--border)] px-2 py-1.5 text-left align-top font-medium text-[var(--text-secondary)] max-[640px]:block max-[640px]:w-full max-[640px]:border-b-0 max-[640px]:py-0.5 max-[640px]:px-0 max-[640px]:text-[length:var(--text-xs)] max-[640px]:uppercase max-[640px]:tracking-[0.5px] max-[640px]:break-words'
const TD_CLS =
  'border-b border-[var(--border)] px-2 py-1.5 text-left align-top font-mono break-all max-[640px]:block max-[640px]:w-full max-[640px]:border-b-0 max-[640px]:py-0.5 max-[640px]:px-0'
const TR_CLS = 'max-[640px]:block max-[640px]:w-full max-[640px]:border-b max-[640px]:border-[var(--border)] max-[640px]:py-1.5 last:max-[640px]:border-b-0'

function formatNsToMs(ns: number): string {
  return (ns / 1_000_000).toFixed(2) + 'ms'
}

function LLMSummary({ span }: { span: SpanRecord }) {
  const [showRaw, setShowRaw] = useState(false)
  const [showPrompt, setShowPrompt] = useState(false)
  const [showCompletion, setShowCompletion] = useState(false)

  const llm = parseLLMAttributes(span.attributes_json)
  if (!llm) return null

  const durationNs = span.end_time_ns - span.start_time_ns
  const durationSec = durationNs / 1_000_000_000
  const tokensPerSec = durationSec > 0 ? (llm.completionTokens / durationSec).toFixed(1) : '-'
  const totalTokens = llm.promptTokens + llm.completionTokens
  const promptRatio = totalTokens > 0 ? (llm.promptTokens / totalTokens) * 100 : 0

  let attributes: Record<string, any> = {}
  try {
    if (span.attributes_json) attributes = JSON.parse(span.attributes_json)
  } catch { /* ignore */ }

  if (showRaw) {
    return (
      <div>
        <Heading level={3} className={SECTION_HEADING_CLS}>
          <span>Raw Attributes</span>
          <button type="button" className={TOGGLE_BUTTON_CLS} onClick={() => setShowRaw(false)}>
            Show LLM view
          </button>
        </Heading>
        <table className={TABLE_CLS}>
          <tbody>
            {Object.entries(attributes).map(([key, value]) => (
              <tr key={key} className={TR_CLS}>
                <th className={TH_CLS}>{key}</th>
                <td className={TD_CLS}>{typeof value === 'object' ? JSON.stringify(value) : String(value)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    )
  }

  return (
    <>
      <div>
        <Heading level={3} className={SECTION_HEADING_CLS}>
          <span>LLM Call</span>
          <button type="button" className={TOGGLE_BUTTON_CLS} onClick={() => setShowRaw(true)}>
            Show raw
          </button>
        </Heading>
        <div className="grid grid-cols-2 gap-3">
          <div className="flex flex-col gap-1">
            <span className="text-[length:var(--text-sm)] text-[var(--text-secondary)]">Provider</span>
            <span className="font-mono text-[length:var(--text-md)] text-[var(--text-primary)]">{llm.system}</span>
          </div>
          <div className="flex flex-col gap-1">
            <span className="text-[length:var(--text-sm)] text-[var(--text-secondary)]">Model</span>
            <span className="font-mono text-[length:var(--text-md)] text-[var(--text-primary)]">{llm.model}</span>
          </div>
          <div className="flex flex-col gap-1">
            <span className="text-[length:var(--text-sm)] text-[var(--text-secondary)]">Latency</span>
            <span className="font-mono text-[length:var(--text-md)] text-[var(--text-primary)]">{formatNsToMs(durationNs)}</span>
          </div>
          <div className="flex flex-col gap-1">
            <span className="text-[length:var(--text-sm)] text-[var(--text-secondary)]">Tokens/sec</span>
            <span className="font-mono text-[length:var(--text-md)] text-[var(--text-primary)]">{tokensPerSec}</span>
          </div>
          <div className="col-span-full flex flex-col gap-1">
            <span className="text-[length:var(--text-sm)] text-[var(--text-secondary)]">
              Tokens: {formatTokenCount(llm.promptTokens)} in / {formatTokenCount(llm.completionTokens)} out / {formatTokenCount(totalTokens)} total
            </span>
            <div className="mt-1 h-1.5 rounded-sm bg-[var(--bg-tertiary)]">
              <div
                className="h-full rounded-sm bg-[var(--color-warning-foreground)]"
                style={{ width: `${promptRatio}%` }}
              />
            </div>
          </div>
        </div>
      </div>

      {llm.prompt && (
        <div>
          <Heading level={3} className={SECTION_HEADING_CLS}>
            <span>Prompt</span>
            <button type="button" className={TOGGLE_BUTTON_CLS} onClick={() => setShowPrompt(!showPrompt)}>
              {showPrompt ? 'Collapse' : 'Expand'}
            </button>
          </Heading>
          {showPrompt && (
            <div className="max-h-[300px] overflow-y-auto whitespace-pre-wrap rounded border border-[color-mix(in_srgb,var(--color-info)_30%,var(--border))] bg-[color-mix(in_srgb,var(--color-info)_8%,var(--bg-tertiary))] p-3 font-mono text-[length:var(--text-sm)]">
              {llm.prompt}
            </div>
          )}
        </div>
      )}

      {llm.completion && (
        <div>
          <Heading level={3} className={SECTION_HEADING_CLS}>
            <span>Completion</span>
            <button type="button" className={TOGGLE_BUTTON_CLS} onClick={() => setShowCompletion(!showCompletion)}>
              {showCompletion ? 'Collapse' : 'Expand'}
            </button>
          </Heading>
          {showCompletion && (
            <div className="max-h-[300px] overflow-y-auto whitespace-pre-wrap rounded border border-[color-mix(in_srgb,var(--color-warning-foreground)_30%,var(--border))] bg-[color-mix(in_srgb,var(--color-warning-foreground)_8%,var(--bg-tertiary))] p-3 font-mono text-[length:var(--text-sm)]">
              {llm.completion}
            </div>
          )}
        </div>
      )}
    </>
  )
}

export function TraceDetail({ isOpen, onClose, span }: TraceDetailProps) {
  if (!span) {
    return (
      <SidebarPanel isOpen={isOpen} onClose={onClose} title="Span Detail">
        <div className="flex flex-col gap-6 p-4">No span selected</div>
      </SidebarPanel>
    )
  }

  const durationMs = formatNsToMs(span.end_time_ns - span.start_time_ns)
  const llmAttrs = parseLLMAttributes(span.attributes_json)

  let attributes: Record<string, any> = {}
  try {
    if (span.attributes_json) {
      attributes = JSON.parse(span.attributes_json)
    }
  } catch (e) {
    console.error('Failed to parse span attributes', e)
  }

  let events: any[] = []
  try {
    if (span.events_json) {
      events = JSON.parse(span.events_json)
    }
  } catch (e) {
    console.error('Failed to parse span events', e)
  }

  return (
    <SidebarPanel isOpen={isOpen} onClose={onClose} title={`Span: ${span.name}`}>
      <div className="flex flex-col gap-6 p-4">
        <div>
          <Heading level={3} className={cn(SECTION_HEADING_CLS, "justify-start")}>Overview</Heading>
          <table className={TABLE_CLS}>
            <tbody>
              <tr className={TR_CLS}><th className={TH_CLS}>Name</th><td className={TD_CLS}>{span.name}</td></tr>
              <tr className={TR_CLS}><th className={TH_CLS}>Status</th><td className={TD_CLS}>{span.status}</td></tr>
              <tr className={TR_CLS}><th className={TH_CLS}>Kind</th><td className={TD_CLS}>{span.kind}</td></tr>
              <tr className={TR_CLS}><th className={TH_CLS}>Duration</th><td className={TD_CLS}>{durationMs}</td></tr>
              <tr className={TR_CLS}><th className={TH_CLS}>Span ID</th><td className={TD_CLS}>{span.span_id}</td></tr>
              <tr className={TR_CLS}><th className={TH_CLS}>Trace ID</th><td className={TD_CLS}>{span.trace_id}</td></tr>
            </tbody>
          </table>
        </div>

        {llmAttrs ? (
          <LLMSummary span={span} />
        ) : (
          Object.keys(attributes).length > 0 && (
            <div>
              <Heading level={3} className={cn(SECTION_HEADING_CLS, "justify-start")}>Attributes</Heading>
              <table className={TABLE_CLS}>
                <tbody>
                  {Object.entries(attributes).map(([key, value]) => (
                    <tr key={key} className={TR_CLS}>
                      <th className={TH_CLS}>{key}</th>
                      <td className={TD_CLS}>{typeof value === 'object' ? JSON.stringify(value) : String(value)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )
        )}

        {events.length > 0 && (
          <div>
            <Heading level={3} className={cn(SECTION_HEADING_CLS, "justify-start")}>Events</Heading>
            <div className="flex flex-col gap-3">
              {events.map((event, index) => {
                const eventAttrs = event.attributes || {}
                return (
                  <div key={`${event.name}-${index}`} className="rounded border border-[var(--border)] bg-[var(--bg-secondary)] p-2">
                    <div className="mb-2 flex justify-between text-[length:var(--text-md)]">
                      <span className="font-semibold">{event.name}</span>
                      {event.timestamp && (
                        <span className="text-[var(--text-secondary)]">
                          {new Date(event.timestamp / 1_000_000).toISOString()}
                        </span>
                      )}
                    </div>
                    {Object.keys(eventAttrs).length > 0 && (
                      <table className={TABLE_CLS}>
                        <tbody>
                          {Object.entries(eventAttrs).map(([key, value]) => (
                            <tr key={key} className={TR_CLS}>
                              <th className={TH_CLS}>{key}</th>
                              <td className={TD_CLS}>{typeof value === 'object' ? JSON.stringify(value) : String(value)}</td>
                            </tr>
                          ))}
                        </tbody>
                      </table>
                    )}
                  </div>
                )
              })}
            </div>
          </div>
        )}
      </div>
    </SidebarPanel>
  )
}
