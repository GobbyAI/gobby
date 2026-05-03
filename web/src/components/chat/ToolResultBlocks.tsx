import { useMemo } from 'react'
import { Prism as SyntaxHighlighter } from 'react-syntax-highlighter'
import { oneDark } from 'react-syntax-highlighter/dist/esm/styles/prism'

import { cn } from '../../lib/utils'
import {
  type GsqzMetadata,
  parseGsqzWrapper,
  parseReadOutput,
} from './ToolCallCard.helpers'

const TOOL_RESULT_WRAP_CLASS = 'tool-result-wrap'

const lineNumberStyle = {
  minWidth: '2.5em',
  paddingRight: '1em',
  textAlign: 'right' as const,
  userSelect: 'none' as const,
  color: 'var(--text-muted)',
}

const highlighterTheme = {
  ...oneDark,
  'pre[class*="language-"]': {
    ...oneDark['pre[class*="language-"]'],
    background: 'var(--code-bg)',
    margin: '0',
    padding: '0.75rem',
    fontSize: '0.75rem',
  },
  'code[class*="language-"]': {
    ...oneDark['code[class*="language-"]'],
    background: 'transparent',
    fontFamily: "'SF Mono', 'Fira Code', 'JetBrains Mono', monospace",
  },
}

const wrappedHighlighterStyle = {
  margin: 0,
  borderRadius: '0.25rem',
  maxHeight: '24rem',
  overflowY: 'auto' as const,
  overflowX: 'hidden' as const,
  whiteSpace: 'pre-wrap' as const,
  overflowWrap: 'anywhere' as const,
}

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
        'flex flex-wrap gap-x-3 gap-y-0.5 px-2 py-1 text-[10px] font-mono',
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
        'px-2 py-1 text-[10px] font-mono border-b border-border/40 bg-muted/30',
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

const JSON_BLOCK_BASE =
  'rounded p-2 max-h-96 overflow-y-auto overflow-x-hidden font-mono text-xs whitespace-pre-wrap break-words'

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
  const palette =
    variant === 'error'
      ? 'bg-destructive/30 text-destructive-foreground'
      : 'bg-muted text-foreground'
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
    <SyntaxHighlighter
      style={highlighterTheme}
      language={language}
      PreTag="div"
      showLineNumbers
      startingLineNumber={startingLineNumber}
      wrapLongLines
      lineNumberStyle={lineNumberStyle}
      customStyle={wrappedHighlighterStyle}
    >
      {content}
    </SyntaxHighlighter>
  )
}

function PlainBody({ body, language }: { body: string; language?: string }) {
  return (
    <SyntaxHighlighter
      style={highlighterTheme}
      language={language ?? 'text'}
      PreTag="div"
      wrapLongLines
      customStyle={wrappedHighlighterStyle}
      codeTagProps={{ className: TOOL_RESULT_WRAP_CLASS }}
    >
      {body}
    </SyntaxHighlighter>
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
    <div className="overflow-hidden rounded border border-border/40 bg-muted/30">
      <GsqzMetadataStrip metadata={metadata} />
      {nested ? (
        <GsqzResultBlock metadata={nested.metadata} body={nested.body} language={language} />
      ) : (
        <ToolResultBody body={body} language={language} />
      )}
    </div>
  )
}
