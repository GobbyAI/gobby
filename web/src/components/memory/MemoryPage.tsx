import { useState, useCallback, useMemo, useEffect, useRef, lazy, Suspense, Component, type ReactNode } from 'react'
import { useMemory, useNeo4jStatus } from '../../hooks/useMemory'
import { useNow } from '../../hooks/useNow'
import type { GobbyMemory } from '../../hooks/useMemory'
import { MemoryFilters } from './MemoryFilters'
import { MemoryTable } from './MemoryTable'
import { MemoryForm } from './MemoryForm'
import type { MemoryFormData } from './MemoryForm'
import { MemoryDetail } from './MemoryDetail'
import { IS_MOBILE, IS_IOS, WEBGL_CAP } from '../../utils/platform'
import { cn } from '../../lib/utils'

const DEFAULT_KNOWLEDGE_GRAPH_LIMIT = IS_IOS ? 150 : IS_MOBILE ? 250 : 500
const GRAPH_LIMIT_MIN = 50
const KNOWLEDGE_LIMIT_MAX = IS_IOS ? 300 : IS_MOBILE ? 500 : 5000
const GRAPH_LIMIT_STEP = 50

const KnowledgeGraph = lazy(() => import('./KnowledgeGraph').then(m => ({ default: m.KnowledgeGraph })))

const FALLBACK_BUTTON_CLS =
  'cursor-pointer rounded border border-[var(--border)] bg-[var(--bg-secondary)] px-3 py-1.5 text-[length:var(--text-sm)] text-[var(--text-primary)] hover:bg-[var(--bg-tertiary)]'
const FALLBACK_PRIMARY_BUTTON_CLS =
  'cursor-pointer rounded border-0 bg-[var(--accent)] px-3 py-1.5 text-[length:var(--text-sm)] text-[var(--accent-foreground)] hover:opacity-90'

class KnowledgeGraphErrorBoundary extends Component<
  { children: ReactNode; onFallback?: () => void },
  { hasError: boolean }
> {
  constructor(props: { children: ReactNode; onFallback?: () => void }) {
    super(props)
    this.state = { hasError: false }
  }
  static getDerivedStateFromError() {
    return { hasError: true }
  }
  componentDidCatch(error: Error, info: React.ErrorInfo) {
    console.error('[KnowledgeGraphErrorBoundary]', error, info)
  }
  render() {
    if (this.state.hasError) {
      return (
        <div className="p-8 text-center text-[var(--text-secondary)]">
          <div>3D knowledge graph failed to load.</div>
          <div className="mt-3 flex justify-center gap-2">
            <button
              onClick={() => this.setState({ hasError: false })}
              className={FALLBACK_BUTTON_CLS}
            >
              Try Again
            </button>
            {this.props.onFallback && (
              <button
                onClick={this.props.onFallback}
                className={FALLBACK_PRIMARY_BUTTON_CLS}
              >
                Switch to List
              </button>
            )}
          </div>
        </div>
      )
    }
    return this.props.children
  }
}

function ListIcon() {
  return (
    <svg width="14" height="14" viewBox="0 0 14 14" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round">
      <line x1="3" y1="3" x2="11" y2="3" />
      <line x1="3" y1="7" x2="11" y2="7" />
      <line x1="3" y1="11" x2="11" y2="11" />
    </svg>
  )
}

function KnowledgeIcon() {
  return (
    <svg width="14" height="14" viewBox="0 0 14 14" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round">
      <rect x="1" y="2" width="4" height="3" rx="1" />
      <rect x="9" y="2" width="4" height="3" rx="1" />
      <rect x="5" y="9" width="4" height="3" rx="1" />
      <line x1="5" y1="3.5" x2="9" y2="3.5" />
      <line x1="3" y1="5" x2="7" y2="9" />
      <line x1="11" y1="5" x2="7" y2="9" />
    </svg>
  )
}

type ViewMode = 'list' | 'knowledge'
interface MemoryPageProps {
  projectId?: string | null
}

export function MemoryPage({ projectId }: MemoryPageProps = {}) {
  const {
    memories,
    stats,
    isLoading,
    filters,
    setFilters,
    createMemory,
    updateMemory,
    deleteMemory,
    refreshMemories,
    fetchKnowledgeGraph,
    fetchEntityNeighbors,
  } = useMemory(projectId)
  const neo4jStatus = useNeo4jStatus()

  const [knowledgeGraphLimit, setKnowledgeGraphLimit] = useState(DEFAULT_KNOWLEDGE_GRAPH_LIMIT)

  useEffect(() => {
    const controller = new AbortController()
    fetch('/api/config/values', { signal: controller.signal })
      .then(res => res.ok ? res.json() : null)
      .then(data => {
        if (!data) return
        const values = data.values ?? data
        const kgLimit = values?.['ui.knowledge_graph_limit']
        if (typeof kgLimit === 'number' && kgLimit >= 50) setKnowledgeGraphLimit(kgLimit)
      })
      .catch((e) => { if (e.name !== 'AbortError') console.debug('Config fetch failed:', e) })
    return () => controller.abort()
  }, [])

  const [viewMode, setViewMode] = useState<ViewMode>(() => {
    try {
      const saved = localStorage.getItem('gobby-memory-view')
      if (saved === 'knowledge' || saved === 'list') return saved
    } catch { /* noop */ }
    return 'list'
  })
  const [showForm, setShowForm] = useState(false)

  const autoSwitchedRef = useRef(false)
  useEffect(() => {
    if (neo4jStatus?.configured && viewMode === 'list' && !autoSwitchedRef.current) {
      try {
        if (!localStorage.getItem('gobby-memory-view') && !localStorage.getItem('gobby-kg-failed')) {
          setViewMode('knowledge')
        }
      } catch {
        setViewMode('knowledge')
      }
      autoSwitchedRef.current = true
    }
  }, [neo4jStatus?.configured, viewMode])

  useEffect(() => {
    try { localStorage.setItem('gobby-memory-view', viewMode) } catch { /* noop */ }
  }, [viewMode])
  const [editMemory, setEditMemory] = useState<GobbyMemory | null>(null)
  const [selectedMemory, setSelectedMemory] = useState<GobbyMemory | null>(null)
  const [errorMessage, setErrorMessage] = useState<string | null>(null)

  const showError = useCallback((msg: string) => {
    setErrorMessage(msg)
    setTimeout(() => setErrorMessage(null), 4000)
  }, [])
  const handleKnowledgeGraphError = useCallback(() => {
    setViewMode('list')
    showError('3D knowledge graph unavailable — switched to list view')
    try { localStorage.setItem('gobby-kg-failed', 'true') } catch { /* noop */ }
  }, [showError])

  const [searchText, setSearchText] = useState('')
  const now = useNow()

  const filteredMemories = useMemo(() => {
    let result = memories

    if (filters.recentOnly) {
      const cutoff = now - 24 * 60 * 60 * 1000
      result = result.filter(m => new Date(m.created_at).getTime() > cutoff)
    }

    if (searchText.trim()) {
      const q = searchText.toLowerCase()
      result = result.filter(m =>
        m.content.toLowerCase().includes(q) ||
        m.memory_type.toLowerCase().includes(q) ||
        (m.tags && m.tags.some(t => t.toLowerCase().includes(q)))
      )
    }

    return result
  }, [memories, filters.recentOnly, searchText, now])

  const handleCreate = useCallback(() => {
    setEditMemory(null)
    setShowForm(true)
  }, [])

  const handleEdit = useCallback((memory: GobbyMemory) => {
    setSelectedMemory(null)
    setEditMemory(memory)
    setShowForm(true)
  }, [])

  const handleSave = useCallback(
    async (data: MemoryFormData) => {
      try {
        if (editMemory) {
          await updateMemory(editMemory.id, {
            content: data.content,
            importance: data.importance,
            tags: data.tags,
          })
        } else {
          await createMemory({
            content: data.content,
            memory_type: data.memory_type,
            importance: data.importance,
            tags: data.tags,
          })
        }
        setShowForm(false)
        setEditMemory(null)
      } catch (e) {
        console.error('Failed to save memory:', e)
        showError('Failed to save memory')
      }
    },
    [editMemory, createMemory, updateMemory, showError]
  )

  const handleDelete = useCallback(
    async (memoryId: string) => {
      try {
        await deleteMemory(memoryId)
        if (selectedMemory?.id === memoryId) {
          setSelectedMemory(null)
        }
      } catch (e) {
        console.error('Failed to delete memory:', e)
        showError('Failed to delete memory')
      }
    },
    [deleteMemory, selectedMemory, showError]
  )

  const handleSelect = useCallback((memory: GobbyMemory) => {
    setSelectedMemory(memory)
  }, [])

  const handleDetailEdit = useCallback(() => {
    if (selectedMemory) {
      handleEdit(selectedMemory)
    }
  }, [selectedMemory, handleEdit])

  const handleDetailDelete = useCallback(() => {
    if (selectedMemory) {
      handleDelete(selectedMemory.id)
    }
  }, [selectedMemory, handleDelete])

  const viewModes: [ViewMode, React.ComponentType, string][] = [
    ...(neo4jStatus?.configured ? [['knowledge' as ViewMode, KnowledgeIcon, 'Knowledge graph'] as [ViewMode, React.ComponentType, string]] : []),
    ['list', ListIcon, 'List view'],
  ]

  return (
    <main className="flex flex-1 flex-col overflow-hidden px-6 py-4 max-sm:px-4">
      {errorMessage && (
        <div
          className="fixed right-5 top-15 z-[1000] cursor-pointer rounded-md bg-[var(--color-error)] px-4 py-2 text-[length:var(--text-base)] text-[var(--accent-foreground)]"
          onClick={() => setErrorMessage(null)}
        >
          {errorMessage}
        </div>
      )}
      <div className="mb-2 flex flex-wrap items-center justify-between gap-4 border-b border-[var(--border)] pb-3">
        <div className="flex flex-wrap items-center gap-2">
          <h1 className="m-0 text-[length:var(--text-xl)] font-semibold text-[var(--text-primary)]">Memory</h1>
        </div>
        <div className="flex min-w-0 flex-1 flex-wrap items-center justify-end gap-1.5">
          <div className="flex items-center gap-0.5 rounded-md border border-[var(--border)] bg-[var(--bg-secondary)] p-0.5">
            {viewModes.map(([mode, Icon, title]) => {
              const isActive = viewMode === mode
              return (
                <button
                  key={mode}
                  type="button"
                  className={cn(
                    'flex h-7 w-7 cursor-pointer items-center justify-center rounded border-0 bg-transparent p-0 text-[var(--text-muted)] transition-[background-color,color] duration-150 hover:bg-[var(--bg-tertiary)] hover:text-[var(--text-primary)] pointer-coarse:h-11 pointer-coarse:w-11',
                    isActive && 'bg-[var(--accent)] text-[var(--bg-primary)] hover:bg-[var(--accent)] hover:text-[var(--bg-primary)]',
                  )}
                  onClick={() => setViewMode(mode)}
                  title={title}
                >
                  <Icon />
                </button>
              )
            })}
          </div>
          <input
            className="box-border min-w-0 max-w-[180px] flex-[1_1_140px] rounded-md border border-[var(--border)] bg-[var(--bg-secondary)] px-2 py-1.5 text-[length:var(--text-base)] text-[var(--text-primary)] placeholder:text-[var(--text-muted)] focus:border-[var(--accent)] focus:outline-none pointer-coarse:min-h-11"
            type="text"
            placeholder="Search"
            value={searchText}
            onChange={e => setSearchText(e.target.value)}
          />
          <button
            type="button"
            className="flex h-7 w-7 cursor-pointer items-center justify-center rounded-md border border-[var(--border)] bg-[var(--bg-secondary)] p-0 text-[length:var(--text-base)] text-[var(--text-secondary)] transition-[background-color,color,opacity] duration-150 hover:bg-[var(--bg-tertiary)] hover:text-[var(--text-primary)] disabled:cursor-not-allowed disabled:opacity-60 pointer-coarse:h-11 pointer-coarse:w-11"
            onClick={refreshMemories}
            title="Refresh"
            disabled={isLoading}
          >
            &#x21bb;
          </button>
          <button
            type="button"
            className="cursor-pointer whitespace-nowrap rounded-md border-0 bg-[var(--accent)] px-3 py-1.5 text-[length:var(--text-base)] font-medium text-[var(--bg-primary)] transition-opacity duration-150 hover:opacity-85 pointer-coarse:min-h-11 pointer-coarse:px-4 pointer-coarse:py-2"
            onClick={handleCreate}
          >
            + New
          </button>
        </div>
      </div>

      <MemoryFilters
        filters={filters}
        stats={stats}
        recentCount={stats?.recent_count ?? 0}
        onFiltersChange={setFilters}
        viewMode={viewMode}
        knowledgeGraphLimit={knowledgeGraphLimit}
        onKnowledgeGraphLimitChange={setKnowledgeGraphLimit}
        limitMin={GRAPH_LIMIT_MIN}
        limitMax={KNOWLEDGE_LIMIT_MAX}
        limitStep={GRAPH_LIMIT_STEP}
      />

      <div className="flex min-h-0 flex-1 flex-col overflow-y-auto">
        {viewMode === 'knowledge' ? (
          !WEBGL_CAP.supported ? (
            <div className="p-8 text-center text-[var(--text-secondary)]">
              <div>WebGL is not available on this device.</div>
              <button
                onClick={() => setViewMode('list')}
                className={cn('mt-3', FALLBACK_PRIMARY_BUTTON_CLS)}
              >
                Switch to List
              </button>
            </div>
          ) : (
          <KnowledgeGraphErrorBoundary onFallback={handleKnowledgeGraphError}>
            <Suspense fallback={<div className="p-8 text-[var(--text-secondary)]">Loading 3D graph...</div>}>
              <KnowledgeGraph
                fetchKnowledgeGraph={fetchKnowledgeGraph}
                fetchEntityNeighbors={fetchEntityNeighbors}
                limit={knowledgeGraphLimit}
                onError={handleKnowledgeGraphError}
              />
            </Suspense>
          </KnowledgeGraphErrorBoundary>
          )
        ) : (
          <MemoryTable
            memories={filteredMemories}
            onSelect={handleSelect}
            onDelete={handleDelete}
            onUpdate={updateMemory}
            onEdit={handleEdit}
            isLoading={isLoading}
          />
        )}
      </div>

      <MemoryDetail
        memory={selectedMemory}
        onEdit={handleDetailEdit}
        onDelete={handleDetailDelete}
        onClose={() => setSelectedMemory(null)}
      />

      {showForm && (
        <MemoryForm
          memory={editMemory}
          onSave={handleSave}
          onCancel={() => {
            setShowForm(false)
            setEditMemory(null)
          }}
        />
      )}
    </main>
  )
}
