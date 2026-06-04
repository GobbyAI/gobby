import { useCallback, useEffect, useMemo, useState } from 'react'
import type { ChatMessage } from '../types/chat'
import { classifyTool } from '../types/chat'

export interface ChangedFile {
  path: string
  status: string // E = edited, W = written (new), D = deleted
}

function getBaseUrl(): string {
  return import.meta.env.VITE_API_BASE_URL || ''
}

/** Tools that create or modify files */
const EDIT_TOOL_TYPES = new Set(['edit'])

/** Extract file path from tool call arguments */
function extractFilePath(args: Record<string, unknown> | undefined): string | null {
  if (!args) return null
  // Claude Code uses file_path; other CLIs may use path
  const raw = args.file_path ?? args.path
  if (typeof raw === 'string' && raw.length > 0) return raw
  return null
}

const STATUS_ORDER = (s: string) => (s === 'W' ? 0 : s === 'E' ? 1 : s === 'D' ? 2 : 3)

function sortChanges(files: ChangedFile[]): ChangedFile[] {
  return [...files].sort((a, b) => {
    const diff = STATUS_ORDER(a.status) - STATUS_ORDER(b.status)
    return diff !== 0 ? diff : a.path.localeCompare(b.path)
  })
}

function isChangedFile(value: unknown): value is ChangedFile {
  return (
    typeof value === 'object' &&
    value !== null &&
    typeof (value as { path?: unknown }).path === 'string' &&
    typeof (value as { status?: unknown }).status === 'string'
  )
}

function parseChangedFiles(data: unknown): ChangedFile[] {
  if (
    typeof data !== 'object' ||
    data === null ||
    !Array.isArray((data as { files?: unknown }).files)
  ) {
    console.warn('Invalid session changes response shape:', data)
    return []
  }
  const files = (data as { files: unknown[] }).files
  if (!files.every(isChangedFile)) {
    console.warn('Invalid session changes files response shape:', data)
    return []
  }
  return files
}

/**
 * Scan the live chat transcript for completed edit/write tool calls. Used as a
 * fast optimistic overlay for the active chat only — the authoritative list
 * comes from the session-scoped backend endpoint.
 */
function scanMessages(messages: ChatMessage[]): ChangedFile[] {
  const fileMap = new Map<string, string>() // path → status
  for (const msg of messages) {
    if (msg.role !== 'assistant' || !msg.toolCalls) continue
    for (const tc of msg.toolCalls) {
      if (tc.status !== 'completed') continue
      const toolType = tc.tool_type || classifyTool(tc.tool_name)
      if (!EDIT_TOOL_TYPES.has(toolType)) continue

      const filePath = extractFilePath(tc.arguments)
      if (!filePath) continue
      if (filePath.includes('.gobby/')) continue
      if (filePath.includes('.claude/plans/')) continue

      const toolName = tc.tool_name?.toLowerCase() || ''
      if (toolName === 'write' && !fileMap.has(filePath)) {
        fileMap.set(filePath, 'W')
      } else {
        fileMap.set(filePath, 'E')
      }
    }
  }
  return Array.from(fileMap, ([path, status]) => ({ path, status }))
}

export interface UseFileChangesResult {
  changedFiles: ChangedFile[]
  fetchDiff: (path: string) => Promise<string>
  loading: boolean
  error: string | null
  refresh: () => void
}

/**
 * Session-scoped file changes for the Changes panel.
 *
 * The authoritative changed-file list and per-file diffs come from the viewed
 * session's working tree (`GET /api/sessions/{id}/changes`), so worktree/clone
 * and resumed sessions are correct and switching sessions switches contents.
 * The live message-scan is layered on top as a fast optimistic overlay, but
 * only for the active chat (`isLiveSession`), where the transcript belongs to
 * the viewed session.
 */
export function useFileChanges(
  sessionId: string | null,
  messages: ChatMessage[],
  isLiveSession: boolean,
): UseFileChangesResult {
  const [sessionFiles, setSessionFiles] = useState<ChangedFile[]>([])
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const [reloadKey, setReloadKey] = useState(0)

  const liveFiles = useMemo(() => scanMessages(messages), [messages])

  const refresh = useCallback(() => setReloadKey((key) => key + 1), [])

  // Fetch the viewed session's changes; re-runs on session switch or refresh.
  useEffect(() => {
    let cancelled = false
    const load = async () => {
      if (!sessionId) {
        setSessionFiles([])
        setError(null)
        setLoading(false)
        return
      }
      // Clear stale contents so a session switch never shows the prior session.
      setSessionFiles([])
      setLoading(true)
      setError(null)
      try {
        const res = await fetch(
          `${getBaseUrl()}/api/sessions/${encodeURIComponent(sessionId)}/changes`,
        )
        if (!res.ok) throw new Error(`HTTP ${res.status}`)
        const data = await res.json()
        if (!cancelled) {
          setSessionFiles(parseChangedFiles(data))
        }
      } catch (err) {
        if (cancelled) return
        if (import.meta.env.DEV) console.error('session changes fetch failed:', err)
        setSessionFiles([])
        setError('Could not load changes for this session.')
      } finally {
        if (!cancelled) setLoading(false)
      }
    }
    void load()
    return () => {
      cancelled = true
    }
  }, [sessionId, reloadKey])

  const changedFiles = useMemo(() => {
    if (!isLiveSession || liveFiles.length === 0) return sessionFiles
    // Merge the optimistic overlay on top, preferring the authoritative backend
    // entry when both name the same path.
    const merged = new Map<string, ChangedFile>()
    for (const f of liveFiles) merged.set(f.path, f)
    for (const f of sessionFiles) merged.set(f.path, f)
    return sortChanges(Array.from(merged.values()))
  }, [sessionFiles, liveFiles, isLiveSession])

  const fetchDiff = useCallback(
    async (path: string): Promise<string> => {
      if (!sessionId) return ''
      try {
        const res = await fetch(
          `${getBaseUrl()}/api/sessions/${encodeURIComponent(sessionId)}/changes/diff?path=${encodeURIComponent(path)}`,
        )
        if (!res.ok) return ''
        const data = await res.json()
        return data.diff || ''
      } catch (err) {
        if (import.meta.env.DEV) console.error('fetchDiff failed:', err)
        return ''
      }
    },
    [sessionId],
  )

  return { changedFiles, fetchDiff, loading, error, refresh }
}
