import { memo, useMemo } from 'react'
import ReactMarkdown, { defaultUrlTransform, type Components } from 'react-markdown'
import remarkGfm from 'remark-gfm'
import { marked } from 'marked'
import type { PluggableList } from 'unified'

import { codeBlockComponents } from '../chat/CodeBlock'

const defaultRemarkPlugins: PluggableList = [remarkGfm]

// The default sanitizer only keeps http/https/mailto/tel and relative URLs;
// wikilink: URLs from the remarkWikilink plugin must survive it.
function urlTransform(url: string): string {
  return url.startsWith('wikilink:') ? url : defaultUrlTransform(url)
}

const MemoizedBlock = memo(
  ({
    content,
    plugins,
    components,
  }: {
    content: string
    plugins: PluggableList
    components: Partial<Components>
  }) => (
    <ReactMarkdown remarkPlugins={plugins} components={components} urlTransform={urlTransform}>
      {content}
    </ReactMarkdown>
  ),
  (prev, next) =>
    prev.content === next.content &&
    prev.plugins === next.plugins &&
    prev.components === next.components,
)
MemoizedBlock.displayName = 'MarkdownBody.MemoizedBlock'

// Stable per-reference ids so the block key changes when a caller swaps the
// extension props — content-equal cached blocks must re-render under a new
// plugin or component set.
let nextExtensionId = 1
const extensionIds = new WeakMap<object, number>()
function extensionIdentity(value: object | undefined): number {
  if (!value) return 0
  let id = extensionIds.get(value)
  if (id === undefined) {
    id = nextExtensionId++
    extensionIds.set(value, id)
  }
  return id
}

function stableHash(s: string): string {
  let h = 0
  for (let i = 0; i < s.length; i++) {
    h = ((h << 5) - h + s.charCodeAt(i)) | 0
  }
  return (h >>> 0).toString(36)
}

const BLOCK_PROTOCOL_TAGS = [
  'system-reminder',
  'task-notification',
  'local-command-caveat',
  'local-command-stdout',
  'command-name',
  'command-args',
  'command-message',
  'hook_context',
  'hook-context',
  'antml_thinking',
  'antml_function_calls',
  'antml_invoke',
  'environment_context',
  'skill',
] as const

const UNCLOSED_BLOCK_PROTOCOL_TAGS = [
  'system-reminder',
  'task-notification',
  'local-command-caveat',
  'hook_context',
  'environment_context',
  'skill',
] as const

const WRAPPER_ONLY_PROTOCOL_TAGS = [
  'proposed_plan',
  'proposed_implementation',
  'search_quality_reflection',
  'permissions instructions',
  'permission instructions',
  'collaboration_mode',
  'turn_aborted',
  'instructions',
  'skills_instructions',
] as const

function escapeRegex(text: string): string {
  return text.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')
}

function buildTagPattern(tags: readonly string[]): string {
  return tags.map(escapeRegex).join('|')
}

const blockProtocolTagPattern = buildTagPattern(BLOCK_PROTOCOL_TAGS)
const unclosedBlockProtocolTagPattern = buildTagPattern(UNCLOSED_BLOCK_PROTOCOL_TAGS)
const wrapperOnlyProtocolTagPattern = buildTagPattern(WRAPPER_ONLY_PROTOCOL_TAGS)

const blockProtocolTagRe = new RegExp(
  `<(${blockProtocolTagPattern})(?=[\\s>])[^>]*>[\\s\\S]*?</\\1\\s*>`,
  'gi',
)

const unclosedBlockProtocolTagRe = new RegExp(
  `<(?:${unclosedBlockProtocolTagPattern})(?=[\\s>])[^>]*>[\\s\\S]*$`,
  'gi',
)

const wrapperOnlyProtocolTagRe = new RegExp(
  `</?(?:${wrapperOnlyProtocolTagPattern})(?=[\\s>])[^>]*>`,
  'gi',
)

function stripProtocolTags(text: string): string {
  if (!text.includes('<')) {
    return text
  }
  return text
    .replace(blockProtocolTagRe, '')
    .replace(unclosedBlockProtocolTagRe, '')
    .replace(wrapperOnlyProtocolTagRe, '')
    .replace(/\n{3,}/g, '\n\n')
}

/**
 * Canonical markdown renderer used everywhere in the app: chat bubbles,
 * tool result bodies, session summaries, skill previews, file previews.
 *
 * Splits content into block-level tokens via marked's lexer so completed
 * blocks memoize during streaming. Strips internal protocol tags
 * (system-reminder, hook_context, etc.) that should never reach the user.
 * Routes code/table/anchor/image rendering through `codeBlockComponents`,
 * which composes with the shared `CodeBlock` primitive (#13794).
 */
export function MarkdownBody({
  content,
  id,
  remarkPlugins,
  components,
}: {
  content: string
  id: string
  /** Extra remark plugins appended after the default remarkGfm. */
  remarkPlugins?: PluggableList
  /** Component overrides merged over the default codeBlockComponents. */
  components?: Partial<Components>
}) {
  const blocks = useMemo(() => {
    const cleaned = stripProtocolTags(content)
    const tokens = marked.lexer(cleaned)
    return tokens.map((token) => token.raw)
  }, [content])

  const mergedPlugins = useMemo<PluggableList>(
    () =>
      remarkPlugins?.length ? [...defaultRemarkPlugins, ...remarkPlugins] : defaultRemarkPlugins,
    [remarkPlugins],
  )
  const mergedComponents = useMemo<Partial<Components>>(
    () => (components ? { ...codeBlockComponents, ...components } : codeBlockComponents),
    [components],
  )
  const extensionKey = `${extensionIdentity(remarkPlugins)}-${extensionIdentity(components)}`

  return (
    <>
      {blocks.map((block, i) => (
        <MemoizedBlock
          key={`${id}-${i}-${stableHash(block)}-${extensionKey}`}
          content={block}
          plugins={mergedPlugins}
          components={mergedComponents}
        />
      ))}
    </>
  )
}
