import React from 'react'

import { CodeBlock } from './CodeBlock'

interface CodeProps {
  children?: React.ReactNode
  className?: string
  node?: unknown
}

export function MarkdownCodeBlock({ children, className, ...props }: CodeProps) {
  const match = /language-(\w+)/.exec(className || '')
  const language = match ? match[1] : ''
  const codeString = String(children).replace(/\n$/, '')
  const isInline = !match && !String(children).includes('\n')

  if (isInline) {
    return (
      <code className={className} {...props}>
        {children}
      </code>
    )
  }

  return (
    <div className="code-block-wrapper">
      {language && (
        <div className="code-block-header">
          <span className="code-block-language">{language}</span>
          <button
            className="code-block-copy"
            onClick={() => navigator.clipboard.writeText(codeString)}
            title="Copy code"
          >
            <CopyIcon />
          </button>
        </div>
      )}
      <CodeBlock
        language={language || 'text'}
        customStyle={{
          margin: language ? '0' : '0.75rem 0',
          borderRadius: language ? '0 0 0.5rem 0.5rem' : '0.5rem',
        }}
      >
        {codeString}
      </CodeBlock>
    </div>
  )
}

export function MarkdownTableWrapper({
  children,
}: {
  children?: React.ReactNode
}) {
  return (
    <div className="table-wrapper">
      <table>{children}</table>
    </div>
  )
}

export function MarkdownAnchor({
  href,
  children,
  ...props
}: React.AnchorHTMLAttributes<HTMLAnchorElement>) {
  const isExternal = href && (href.startsWith('http://') || href.startsWith('https://'))
  return (
    <a
      href={href}
      {...(isExternal ? { target: '_blank', rel: 'noopener noreferrer' } : {})}
      {...props}
    >
      {children}
    </a>
  )
}

function CopyIcon() {
  return (
    <svg
      width="14"
      height="14"
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="2"
      strokeLinecap="round"
      strokeLinejoin="round"
    >
      <rect x="9" y="9" width="13" height="13" rx="2" ry="2" />
      <path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1" />
    </svg>
  )
}
