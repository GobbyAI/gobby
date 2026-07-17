import type { ContentBlock } from '../../types/chat'
import { extractImageSrc } from '../../lib/imageSources'
import { CodeBlock } from '../shared/CodeBlock'
import { DiffBlock } from '../shared/DiffBlock'
import { computeSyntheticDiffLines } from '../shared/DiffBlock.helpers'
import { MarkdownBody } from '../shared/MarkdownBody'
import { TOOL_RESULT_CUSTOM_STYLE } from './ToolCallCard.styles'
import { UnknownBlockCard } from './UnknownBlockCard'

interface RichContentBlocksProps {
  blocks: ContentBlock[]
  idPrefix: string
}

const IGNORED_PROTOCOL_BLOCK_TYPES = new Set([
  'file_history_snapshot',
  'retry_state',
  'turn_completed',
  'ui_telemetry',
])

export function RichContentBlocks({ blocks, idPrefix }: RichContentBlocksProps) {
  return (
    <div className="space-y-2">
      {blocks.map((block, index) => (
        <RichContentBlock key={`${idPrefix}-${index}`} block={block} id={`${idPrefix}-${index}`} />
      ))}
    </div>
  )
}

function RichContentBlock({ block, id }: { block: ContentBlock; id: string }) {
  if (block.type === 'text') {
    return <MarkdownBody id={id} content={block.content} />
  }

  if (block.type === 'resource_link') {
    const label = block.name || block.uri
    const href = safeResourceHref(block.uri)
    return (
      <div className="rounded border border-border bg-muted/30 px-3 py-2 text-xs">
        <div className="font-medium text-foreground truncate">{label}</div>
        {block.description && (
          <div className="mt-1 text-muted-foreground">{block.description}</div>
        )}
        <div className="mt-1 font-mono text-muted-foreground truncate">
          {href ? (
            <a href={href} target="_blank" rel="noreferrer noopener" className="underline">
              {block.uri}
            </a>
          ) : (
            block.uri
          )}
        </div>
      </div>
    )
  }

  if (block.type === 'resource') {
    const resource = block.resource
    const title = stringValue(resource.name) || stringValue(resource.uri) || 'Resource'
    const text = stringValue(resource.text) || stringValue(resource.content)
    return (
      <div className="rounded border border-border bg-muted/30 px-3 py-2 text-xs">
        <div className="font-medium text-foreground truncate">{title}</div>
        {text && (
          <CodeBlock
            language="text"
            className="tool-code-surface mt-2"
            customStyle={TOOL_RESULT_CUSTOM_STYLE}
          >
            {text}
          </CodeBlock>
        )}
      </div>
    )
  }

  if (block.type === 'image') {
    const src = extractImageSrc(block)
    if (!src) return null
    return (
      <div>
        <img
          src={src}
          alt="Image content"
          loading="lazy"
          decoding="async"
          className="max-w-full rounded border border-border"
        />
      </div>
    )
  }

  if (block.type === 'audio') {
    const src = audioSource(block)
    if (!src) return <div className="text-xs text-muted-foreground">Audio content</div>
    return <audio src={src} controls aria-label="Audio content" className="w-full" />
  }

  if (block.type === 'diff') {
    const oldText = block.old_text || ''
    const newText = block.new_text || ''
    return (
      <div>
        {block.path && (
          <div className="mb-1 font-mono text-xs text-muted-foreground">{block.path}</div>
        )}
        <DiffBlock
          lines={computeSyntheticDiffLines(oldText, newText)}
          language={languageFromPath(block.path)}
          className="tool-code-surface"
        />
      </div>
    )
  }

  if (block.type === 'terminal') {
    return (
      <div className="rounded border border-border bg-muted/30 px-3 py-2 font-mono text-xs text-muted-foreground">
        Terminal {block.terminal_id || 'session'}
      </div>
    )
  }

  if (block.type === 'unknown') {
    if (IGNORED_PROTOCOL_BLOCK_TYPES.has(block.block_type)) return null
    return <UnknownBlockCard blockType={block.block_type} raw={block.raw} />
  }

  return null
}

function safeResourceHref(uri: string): string | null {
  if (uri.startsWith('/')) return uri
  try {
    const parsed = new URL(uri)
    return ['http:', 'https:', 'file:'].includes(parsed.protocol) ? uri : null
  } catch {
    return null
  }
}

function audioSource(block: Extract<ContentBlock, { type: 'audio' }>): string | null {
  if (block.url) return block.url
  if (block.data) {
    const mimeType = block.mime_type || 'audio/mpeg'
    return `data:${mimeType};base64,${block.data}`
  }
  return null
}

function stringValue(value: unknown): string | null {
  return typeof value === 'string' && value ? value : null
}

function languageFromPath(path: string | undefined): string {
  if (!path) return 'text'
  const ext = path.split('.').pop()?.toLowerCase()
  return ext || 'text'
}
