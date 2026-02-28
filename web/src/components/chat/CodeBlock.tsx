import React, { useCallback, useEffect, useRef, useState } from 'react'
import { Prism as SyntaxHighlighter } from 'react-syntax-highlighter'
import { oneDark } from 'react-syntax-highlighter/dist/esm/styles/prism'
import type { Components } from 'react-markdown'
import { cn } from '../../lib/utils'
import { useArtifactContext } from './artifacts/ArtifactContext'

const customTheme = {
  ...oneDark,
  'pre[class*="language-"]': {
    ...oneDark['pre[class*="language-"]'],
    background: '#0d0d0d',
    margin: '0',
    padding: '1rem',
    fontSize: '0.9em',
  },
  'code[class*="language-"]': {
    ...oneDark['code[class*="language-"]'],
    background: 'transparent',
    fontFamily: "'SF Mono', 'Fira Code', 'JetBrains Mono', monospace",
  },
}

const MIN_ARTIFACT_LINES = 15

interface CodeProps {
  children?: React.ReactNode
  className?: string
  node?: unknown
}

function CodeBlockInner({ children, className }: CodeProps) {
  const [copied, setCopied] = useState(false)
  const { openCodeAsArtifact } = useArtifactContext()
  const copyTimeoutRef = useRef<number | null>(null)

  const match = /language-([\w+#\-./]+)/.exec(className || '')
  const language = match ? match[1] : ''
  const codeString = String(children).replace(/\n$/, '')
  const isInline = !match && !String(children).includes('\n')

  const lineCount = codeString.split('\n').length
  const canOpenAsArtifact = lineCount >= MIN_ARTIFACT_LINES

  useEffect(() => {
    return () => {
      if (copyTimeoutRef.current) clearTimeout(copyTimeoutRef.current)
    }
  }, [])

  const handleCopy = useCallback(async () => {
    try {
      await navigator.clipboard.writeText(codeString)
      setCopied(true)
      if (copyTimeoutRef.current) clearTimeout(copyTimeoutRef.current)
      copyTimeoutRef.current = window.setTimeout(() => setCopied(false), 2000)
    } catch {
      console.error('Failed to copy to clipboard')
    }
  }, [codeString])

  const handleOpenArtifact = useCallback(() => {
    openCodeAsArtifact(language || 'text', codeString, language ? `${language} snippet` : 'Code snippet')
  }, [openCodeAsArtifact, language, codeString])

  if (isInline) {
    return (
      <code className={cn('rounded bg-muted px-1.5 py-0.5 text-sm font-mono text-foreground', className)}>
        {children}
      </code>
    )
  }

  return (
    <div className="my-3 rounded-lg border border-border overflow-hidden">
      <div className="flex items-center justify-between bg-muted/50 px-3 py-1.5 text-xs">
        <span className="text-muted-foreground font-mono">{language || 'text'}</span>
        <div className="flex items-center gap-1">
          {canOpenAsArtifact && (
            <button
              className="flex items-center gap-1 rounded px-1.5 py-0.5 text-muted-foreground hover:text-foreground hover:bg-muted transition-colors"
              onClick={handleOpenArtifact}
              title="Open in panel"
            >
              <PanelIcon />
            </button>
          )}
          <button
            className="flex items-center gap-1 rounded px-1.5 py-0.5 text-muted-foreground hover:text-foreground hover:bg-muted transition-colors"
            onClick={handleCopy}
            title="Copy code"
          >
            {copied ? <CheckIcon /> : <CopyIcon />}
          </button>
        </div>
      </div>
      <SyntaxHighlighter
        style={customTheme}
        language={language || 'text'}
        PreTag="div"
        showLineNumbers
        lineNumberStyle={{
          minWidth: '2.5em',
          paddingRight: '1em',
          textAlign: 'right',
          userSelect: 'none',
          color: '#555',
        }}
        customStyle={{ margin: 0, borderRadius: 0 }}
      >
        {codeString}
      </SyntaxHighlighter>
    </div>
  )
}

function TableWrapper({ children }: { children?: React.ReactNode }) {
  return (
    <div className="overflow-x-auto my-3">
      <table className="min-w-full border-collapse text-sm">
        {children}
      </table>
    </div>
  )
}

function ImageBlock({ src, alt, ...props }: React.ImgHTMLAttributes<HTMLImageElement>) {
  // Handle base64 data URIs and regular URLs — prevents raw text rendering (#9277)
  return (
    <img
      src={src}
      alt={alt || 'Image'}
      className="max-w-full rounded-lg border border-border my-2"
      loading="lazy"
      {...props}
    />
  )
}

function Anchor({ href, children, ...props }: React.AnchorHTMLAttributes<HTMLAnchorElement>) {
  const isExternal = href && (href.startsWith('http://') || href.startsWith('https://'))
  return (
    <a
      href={href}
      className="text-accent hover:underline"
      {...(isExternal ? { target: '_blank', rel: 'noopener noreferrer' } : {})}
      {...props}
    >
      {children}
    </a>
  )
}

export const codeBlockComponents: Partial<Components> = {
  code: CodeBlockInner as Components['code'],
  table: TableWrapper as Components['table'],
  a: Anchor as Components['a'],
  img: ImageBlock as Components['img'],
}

function CopyIcon() {
  return (
    <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
      <rect x="9" y="9" width="13" height="13" rx="2" ry="2" />
      <path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1" />
    </svg>
  )
}

function CheckIcon() {
  return (
    <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
      <polyline points="20 6 9 17 4 12" />
    </svg>
  )
}

function PanelIcon() {
  return (
    <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
      <rect x="3" y="3" width="18" height="18" rx="2" ry="2" />
      <line x1="12" y1="3" x2="12" y2="21" />
    </svg>
  )
}
