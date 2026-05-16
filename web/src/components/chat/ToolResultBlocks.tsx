import { useMemo } from 'react'

import { cn } from '../../lib/utils'
import { CodeBlock } from '../shared/CodeBlock'
import { TOOL_CARD_SPACING } from '../shared/spacing'
import {
  type GsqzMetadata,
  parseGsqzWrapper,
  parseReadOutput,
} from './ToolCallCard.helpers'
import { TOOL_RESULT_CUSTOM_STYLE } from './ToolCallCard.styles'

const TOOL_RESULT_WRAP_CLASS = 'tool-result-wrap'

export interface MetadataStripProps {
  meta: Record<string, unknown>
  className?: string
}

function formatMetaValue(value: unknown): string {
  if (typeof value === 'string') {
    return value.length > 64 ? `${value.slice(0, 63)}…` : value
  }
  if (typeof value === 'number' || typeof value === 'boolean') return String(value)
  if (value == null) return 'null'
  try {
    const s = JSON.stringify(value)
    return s.length > 64 ? `${s.slice(0, 63)}…` : s
  } catch {
    return String(value)
  }
}

export function MetadataStrip({ meta, className }: MetadataStripProps) {
  const entries = Object.entries(meta).filter(([, v]) => v !== undefined)
  if (entries.length === 0) return null
  return (
    <div
      className={cn(
        'flex flex-wrap gap-x-3 gap-y-0.5 text-[10px] font-mono',
        TOOL_CARD_SPACING.metaStrip,
        'border-b border-border/40 text-muted-foreground bg-muted/30',
        className,
      )}
    >
      {entries.map(([key, value]) => (
        <span key={key}>
          <span className="opacity-60">{key}:</span>{' '}
          <span>{formatMetaValue(value)}</span>
        </span>
      ))}
    </div>
  )
}

interface GsqzMetadataStripProps {
  metadata: GsqzMetadata
}

function GsqzMetadataStrip({ metadata }: GsqzMetadataStripProps) {
  const parts: string[] = []
  if (metadata.chunkId) parts.push(`chunk ${metadata.chunkId}`)
  if (metadata.wallTimeSeconds != null) parts.push(`${metadata.wallTimeSeconds.toFixed(3)}s`)
  if (metadata.exitCode != null) parts.push(`exit ${metadata.exitCode}`)
  if (metadata.tokenCount != null) parts.push(`${metadata.tokenCount} tokens`)
  if (metadata.strategy) parts.push(`gsqz:${metadata.strategy}`)
  if (metadata.reduction) parts.push(metadata.reduction)
  if (parts.length === 0) return null

  const isError = metadata.exitCode != null && metadata.exitCode !== 0
  return (
    <div
      className={cn(
        TOOL_CARD_SPACING.metaStrip,
        'text-[10px] font-mono border-b border-border/40 bg-muted/30',
        isError ? 'text-destructive-foreground/80' : 'text-muted-foreground',
      )}
    >
      {parts.join(' · ')}
    </div>
  )
}

export type JsonResultVariant = 'normal' | 'error'

export interface JsonResultBlockProps {
  value: unknown
  variant?: JsonResultVariant
  className?: string
}

const JSON_BLOCK_BASE = cn(
  'rounded max-h-96 overflow-y-auto overflow-x-hidden font-mono text-xs whitespace-pre-wrap break-words',
  TOOL_CARD_SPACING.resultPad,
)

function formatJsonForDisplay(value: unknown): string {
  let serialized: string
  try {
    if (typeof value === 'string') {
      serialized = JSON.stringify(JSON.parse(value), null, 2)
    } else {
      serialized = JSON.stringify(value, null, 2) ?? String(value)
    }
  } catch {
    return typeof value === 'string' ? value : String(value)
  }
  return serialized.replace(/\\n/g, '\n').replace(/\\t/g, '\t')
}

export function JsonResultBlock({ value, variant = 'normal', className }: JsonResultBlockProps) {
  const display = useMemo(() => formatJsonForDisplay(value), [value])
  // Normal results sit transparently on the bordered tool card (no second
  // off-shade slab). The error variant keeps a destructive wash: that is a
  // grayscale-legible state signal, not chrome (.impeccable.md state rule).
  const palette =
    variant === 'error'
      ? 'bg-destructive/30 text-destructive-foreground'
      : 'text-foreground'
  return (
    <pre className={cn(JSON_BLOCK_BASE, TOOL_RESULT_WRAP_CLASS, palette, className)}>
      <code>{display}</code>
    </pre>
  )
}

export interface RenderResultBodyOptions {
  language?: string
  variant?: JsonResultVariant
}

function looksLikeJsonString(body: string): boolean {
  const t = body.trimStart()
  return t.startsWith('{') || t.startsWith('[')
}

function LineNumberedBody({
  content,
  language,
  startingLineNumber,
}: {
  content: string
  language: string
  startingLineNumber: number
}) {
  return (
    <CodeBlock
      language={language}
      startingLineNumber={startingLineNumber}
      wrapLongLines
      className="tool-code-surface"
      customStyle={TOOL_RESULT_CUSTOM_STYLE}
    >
      {content}
    </CodeBlock>
  )
}

function PlainBody({ body, language }: { body: string; language?: string }) {
  return (
    <CodeBlock
      language={language ?? 'text'}
      showLineNumbers={false}
      wrapLongLines
      className="tool-code-surface"
      customStyle={TOOL_RESULT_CUSTOM_STYLE}
      codeTagProps={{ className: TOOL_RESULT_WRAP_CLASS }}
    >
      {body}
    </CodeBlock>
  )
}

export interface ToolResultBodyProps extends RenderResultBodyOptions {
  body: string
}

export function ToolResultBody({ body, language, variant }: ToolResultBodyProps) {
  const parsedRead = parseReadOutput(body)
  if (parsedRead) {
    return (
      <LineNumberedBody
        content={parsedRead.content}
        language={language ?? 'text'}
        startingLineNumber={parsedRead.startLine}
      />
    )
  }

  if (looksLikeJsonString(body)) {
    return <JsonResultBlock value={body} variant={variant} />
  }

  return <PlainBody body={body} language={language} />
}

export interface GsqzResultBlockProps {
  metadata: GsqzMetadata
  body: string
  language?: string
}

export function GsqzResultBlock({ metadata, body, language }: GsqzResultBlockProps) {
  const nested = useMemo(() => parseGsqzWrapper(body), [body])
  return (
    <div className="overflow-hidden rounded border border-border/40">
      <GsqzMetadataStrip metadata={metadata} />
      {nested ? (
        <GsqzResultBlock metadata={nested.metadata} body={nested.body} language={language} />
      ) : (
        <ToolResultBody body={body} language={language} />
      )}
    </div>
  )
}
