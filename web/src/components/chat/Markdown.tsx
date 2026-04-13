import { memo, useMemo } from 'react'
import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
import { marked } from 'marked'
import { codeBlockComponents } from './CodeBlock'

const MemoizedBlock = memo(
  ({ content }: { content: string }) => (
    <ReactMarkdown remarkPlugins={[remarkGfm]} components={codeBlockComponents}>
      {content}
    </ReactMarkdown>
  ),
  (prev, next) => prev.content === next.content
)
MemoizedBlock.displayName = 'MemoizedBlock'

function stableHash(s: string): string {
  let h = 0
  for (let i = 0; i < s.length; i++) {
    h = ((h << 5) - h + s.charCodeAt(i)) | 0
  }
  return (h >>> 0).toString(36)
}

/** Strip protocol tags not meant for display */
function stripProtocolTags(text: string): string {
  return text.replace(
    /<\/?(?:command-name|command-message|proposed_plan|proposed_implementation|search_quality_reflection)>/g,
    ''
  )
}

export function Markdown({ content, id }: { content: string; id: string }) {
  const blocks = useMemo(() => {
    const cleaned = stripProtocolTags(content)
    const tokens = marked.lexer(cleaned)
    return tokens.map((token) => token.raw)
  }, [content])

  return (
    <>
      {blocks.map((block, i) => (
        <MemoizedBlock key={`${id}-${i}-${stableHash(block)}`} content={block} />
      ))}
    </>
  )
}
