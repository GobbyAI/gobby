import { memo, useMemo } from 'react'
import ReactMarkdown, { defaultUrlTransform, type Components } from 'react-markdown'
import remarkGfm from 'remark-gfm'
import { marked } from 'marked'
import type { PluggableList } from 'unified'

import { codeBlockComponents } from '../chat/CodeBlock'

const defaultRemarkPlugins: PluggableList = [remarkGfm]

// eslint-disable-next-line react-refresh/only-export-components -- #19930 keeps styling authority with MarkdownBody.
export const markdownBodyClassName = [
  'max-w-[70ch]',
  '[&_p]:mb-3',
  '[&_p:last-child]:mb-0',
  '[&_code]:rounded-sm',
  '[&_code]:bg-[var(--code-bg)]',
  '[&_code]:px-1.5',
  '[&_code]:py-0.5',
  '[&_code]:font-mono',
  '[&_code]:text-[length:var(--text-base)]',
  '[&_pre]:my-3',
  '[&_pre]:overflow-x-auto',
  '[&_pre]:rounded-lg',
  '[&_pre]:bg-[var(--code-bg)]',
  '[&_pre]:p-4',
  '[&_pre_code]:bg-transparent',
  '[&_pre_code]:p-0',
  '[&_h1]:mt-6',
  '[&_h1]:mb-2',
  '[&_h1]:text-[length:var(--text-3xl)]',
  '[&_h1]:font-semibold',
  '[&_h2]:mt-6',
  '[&_h2]:mb-2',
  '[&_h2]:text-[length:var(--text-2xl)]',
  '[&_h2]:font-semibold',
  '[&_h3]:mt-5',
  '[&_h3]:mb-2',
  '[&_h3]:text-[length:var(--text-xl)]',
  '[&_h3]:font-semibold',
  '[&_h4]:mt-4',
  '[&_h4]:mb-2',
  '[&_h4]:text-[length:var(--text-lg)]',
  '[&_h4]:font-semibold',
  '[&_h5]:mt-4',
  '[&_h5]:mb-2',
  '[&_h5]:text-[length:var(--text-base)]',
  '[&_h5]:font-semibold',
  '[&_h6]:mt-4',
  '[&_h6]:mb-2',
  '[&_h6]:text-[length:var(--text-base)]',
  '[&_h6]:font-semibold',
  '[&_blockquote]:my-3',
  '[&_blockquote]:ml-4',
  '[&_blockquote]:pl-4',
  '[&_blockquote]:text-[var(--text-muted)]',
  '[&_blockquote]:italic',
  '[&_table]:w-full',
  '[&_table]:border-collapse',
  '[&_table]:text-[length:var(--text-base)]',
  '[&_thead]:bg-[var(--bg-tertiary)]',
  '[&_th]:whitespace-nowrap',
  '[&_th]:border-b-2',
  '[&_th]:border-[var(--border)]',
  '[&_th]:px-3',
  '[&_th]:py-2',
  '[&_th]:text-left',
  '[&_th]:font-semibold',
  '[&_td]:border-b',
  '[&_td]:border-[var(--border)]',
  '[&_td]:px-3',
  '[&_td]:py-2',
  '[&_tbody_tr:hover]:bg-[var(--surface-tint-subtle)]',
  '[&_hr]:my-6',
  '[&_hr]:border-0',
  '[&_hr]:border-t',
  '[&_hr]:border-[var(--border)]',
  '[&_input[type=checkbox]]:mr-2',
  '[&_li:has(>input[type=checkbox])]:-ml-6',
  '[&_li:has(>input[type=checkbox])]:list-none',
  '[&_ul]:my-2',
  '[&_ul]:pl-6',
  '[&_ol]:my-2',
  '[&_ol]:pl-6',
  '[&_li]:mb-1.5',
  '[&_li]:leading-[1.6]',
  '[&_li>p]:mb-1',
  '[&_ul_ul]:my-1',
  '[&_ol_ol]:my-1',
  '[&_ul_ol]:my-1',
  '[&_ol_ul]:my-1',
  '[&_strong]:font-semibold',
  '[&_strong]:text-[var(--text-primary)]',
  '[&_del]:text-[var(--text-muted)]',
  '[&_del]:line-through',
  '[&_img]:my-3',
  '[&_img]:max-w-full',
  '[&_img]:rounded-lg',
  '[&>*:first-child]:mt-0',
  '[&>*:last-child]:mb-0',
  '[&_a]:text-[var(--accent)]',
  '[&_a]:no-underline',
  '[&_a:hover]:underline',
  '[&:has(>.cursor)>*:last-of-type:not(.cursor)]:inline',
].join(' ')

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
