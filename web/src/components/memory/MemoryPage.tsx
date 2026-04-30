import { useState, useCallback, useMemo, useEffect, useRef, lazy, Suspense, Component, type ReactNode } from 'react'
import './MemoryPage.css'
import './MemoryPagePanels.css'
import { useMemory, useNeo4jStatus } from '../../hooks/useMemory'
import { useNow } from '../../hooks/useNow'
import type { GobbyMemory } from '../../hooks/useMemory'
import { MemoryFilters } from './MemoryFilters'
import { MemoryTable } from './MemoryTable'
import { MemoryForm } from './MemoryForm'
import type { MemoryFormData } from './MemoryForm'
import { MemoryDetail } from './MemoryDetail'
import { IS_MOBILE, IS_IOS, WEBGL_CAP } from '../../utils/platform'

const DEFAULT_KNOWLEDGE_GRAPH_LIMIT = IS_IOS ? 150 : IS_MOBILE ? 250 : 500
const GRAPH_LIMIT_MIN = 50
const KNOWLEDGE_LIMIT_MAX = IS_IOS ? 300 : IS_MOBILE ? 500 : 5000
const GRAPH_LIMIT_STEP = 50

const KnowledgeGraph = lazy(() => import('./KnowledgeGraph').then(m => ({ default: m.KnowledgeGraph })))

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
        <div style={{ padding: '2rem', color: 'var(--text-secondary)', textAlign: 'center' }}>
          <div>3D knowledge graph failed to load.</div>
          <div style={{ display: 'flex', gap: '0.5rem', justifyContent: 'center', marginTop: '0.75rem' }}>
            <button
              onClick={() => this.setState({ hasError: false })}
              style={{ padding: '0.35rem 0.75rem', borderRadius: 4, border: '1px solid var(--border)', background: 'var(--bg-secondary)', color: 'var(--text-primary)', cursor: 'pointer', fontSize: '0.8rem' }}
            >
              Try Again
            </button>
            {this.props.onFallback && (
              <button
                onClick={this.props.onFallback}
                style={{ padding: '0.35rem 0.75rem', borderRadius: 4, border: 'none', background: 'var(--accent)', color: 'var(--accent-foreground)', cursor: 'pointer', fontSize: '0.8rem' }}
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

  // Configurable graph limits (fetched from backend config, overridable per-session)
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

  // Default to knowledge view when Neo4j is configured and no saved preference
  // Skip if 3D previously failed (user can manually re-select knowledge view to retry)
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

  // Persist view mode
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

  // Apply search and recent filters to memories
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
    <main className="memory-page">
      {errorMessage && (
        <div className="memory-error-toast" onClick={() => setErrorMessage(null)}>
          {errorMessage}
        </div>
      )}
      {/* Toolbar */}
      <div className="memory-toolbar">
        <div className="memory-toolbar-left">
          <h2 className="memory-toolbar-title">Memory</h2>
        </div>
        <div className="memory-toolbar-right">
          <div className="memory-view-toggle">
            {viewModes.map(([mode, Icon, title]) => (
              <button
                key={mode}
                className={`memory-view-btn ${viewMode === mode ? 'active' : ''}`}
                onClick={() => setViewMode(mode)}
                title={title}
              >
                <Icon />
              </button>
            ))}
          </div>
          <input
            className="memory-search"
            type="text"
            placeholder="Search..."
            value={searchText}
            onChange={e => setSearchText(e.target.value)}
          />
          <button
            className="memory-toolbar-btn"
            onClick={refreshMemories}
            title="Refresh"
            disabled={isLoading}
          >
            &#x21bb;
          </button>
          <button className="memory-new-btn" onClick={handleCreate}>
            + New
          </button>
        </div>
      </div>

      {/* Filter bar */}
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

      {/* Content area */}
      <div className="memory-content">
        {viewMode === 'knowledge' ? (
          !WEBGL_CAP.supported ? (
            <div style={{ padding: '2rem', color: 'var(--text-secondary)', textAlign: 'center' }}>
              <div>WebGL is not available on this device.</div>
              <button
                onClick={() => setViewMode('list')}
                style={{ marginTop: '0.75rem', padding: '0.35rem 0.75rem', borderRadius: 4, border: 'none', background: 'var(--accent)', color: 'var(--accent-foreground)', cursor: 'pointer', fontSize: '0.8rem' }}
              >
                Switch to List
              </button>
            </div>
          ) : (
          <KnowledgeGraphErrorBoundary onFallback={handleKnowledgeGraphError}>
            <Suspense fallback={<div style={{ padding: '2rem', color: 'var(--text-secondary)' }}>Loading 3D graph...</div>}>
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

      {/* Detail slide-out panel — always rendered */}
      <MemoryDetail
        memory={selectedMemory}
        onEdit={handleDetailEdit}
        onDelete={handleDetailDelete}
        onClose={() => setSelectedMemory(null)}
      />

      {/* Create/Edit form modal */}
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
