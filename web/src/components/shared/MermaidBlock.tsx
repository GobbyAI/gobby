import { useEffect, useState } from 'react'

import { CodeBlockInner, type CodeProps } from '../chat/CodeBlockRenderers'
import { useResolvedTheme } from '../../hooks/useResolvedTheme'
import { cn, resolveCssVar } from '../../lib/utils'
import { Button } from '../ui/Button'

// The only value import of mermaid lives inside loadMermaid() — the library
// must stay a lazy chunk and never enter the main bundle. Type-only
// references are erased at compile time.
type MermaidApi = typeof import('mermaid').default

let mermaidLoader: Promise<MermaidApi> | null = null

function loadMermaid(): Promise<MermaidApi> {
  mermaidLoader ??= import('mermaid').then((mod) => mod.default)
  return mermaidLoader
}

// mermaid.initialize is global, so track the last-applied theme and re-apply
// only when the resolved theme actually changes.
let initializedTheme: 'light' | 'dark' | null = null

function ensureInitialized(api: MermaidApi, theme: 'light' | 'dark'): void {
  if (initializedTheme === theme) return
  initializedTheme = theme
  api.initialize({
    startOnLoad: false,
    securityLevel: 'strict',
    theme: 'base',
    themeVariables: {
      darkMode: theme === 'dark',
      background: resolveCssVar('--bg-secondary'),
      primaryColor: resolveCssVar('--bg-secondary'),
      primaryTextColor: resolveCssVar('--text-primary'),
      textColor: resolveCssVar('--text-primary'),
      primaryBorderColor: resolveCssVar('--accent'),
      lineColor: resolveCssVar('--border'),
    },
  })
}

// mermaid.render ids must be unique per call — it briefly mounts a DOM
// element under the id while rendering.
let renderSeq = 0

type RenderState =
  | { status: 'loading' }
  | { status: 'ready'; svg: string }
  | { status: 'error' }

function MermaidDiagram({ code, fallback }: { code: string; fallback: CodeProps }) {
  const theme = useResolvedTheme()
  // Results are keyed by the (theme, code) pair that produced them; a key
  // mismatch during render means the current request is still in flight, so
  // "loading" is derived instead of set synchronously in the effect.
  const requestKey = `${theme}:${code}`
  const [result, setResult] = useState<{
    key: string
    state: Exclude<RenderState, { status: 'loading' }>
  } | null>(null)
  const [expanded, setExpanded] = useState(false)

  useEffect(() => {
    let cancelled = false
    loadMermaid()
      .then((api) => {
        ensureInitialized(api, theme)
        return api.render(`mermaid-block-${++renderSeq}`, code)
      })
      .then(({ svg }) => {
        if (!cancelled) setResult({ key: requestKey, state: { status: 'ready', svg } })
      })
      .catch(() => {
        if (!cancelled) setResult({ key: requestKey, state: { status: 'error' } })
      })
    return () => {
      cancelled = true
    }
  }, [code, theme, requestKey])

  const state: RenderState =
    result && result.key === requestKey ? result.state : { status: 'loading' }

  if (state.status === 'loading') {
    return (
      <div
        role="status"
        aria-label="Rendering diagram"
        className="my-3 h-24 animate-pulse rounded-lg border border-border bg-muted/30"
      />
    )
  }

  if (state.status === 'error') {
    return (
      <div className="my-3">
        <p className="mb-1 text-xs text-muted-foreground">
          Diagram failed to render — showing source.
        </p>
        <CodeBlockInner {...fallback} />
      </div>
    )
  }

  return (
    <div className="my-3 rounded-lg border border-border overflow-hidden">
      <div
        role="img"
        aria-label="Mermaid diagram"
        className={cn('overflow-auto p-3', !expanded && 'max-h-96')}
        dangerouslySetInnerHTML={{ __html: state.svg }}
      />
      <div className="flex justify-end border-t border-border bg-muted/50 px-3 py-1">
        <Button
          type="button"
          variant="ghost"
          size="sm"
          dense
          className="rounded px-1.5 py-0.5 text-xs text-muted-foreground hover:text-foreground hover:bg-muted transition-colors"
          onClick={() => setExpanded((value) => !value)}
        >
          {expanded ? 'Collapse' : 'Expand'}
        </Button>
      </div>
    </div>
  )
}

/**
 * `code` component override for MarkdownBody (plan wiki-obsidian-panel §2.4):
 * renders `language-mermaid` fences as themed SVG diagrams and delegates
 * everything else to the default CodeBlockInner.
 */
export function MermaidBlock(props: CodeProps) {
  const { children, className } = props
  if (!/\blanguage-mermaid\b/.test(className ?? '')) {
    return <CodeBlockInner {...props} />
  }
  return <MermaidDiagram code={String(children).replace(/\n$/, '')} fallback={props} />
}
