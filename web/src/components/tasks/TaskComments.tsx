import { useState, useEffect, useCallback, useMemo, useRef } from 'react'
import { relativeTime } from '../../utils/formatTime'

// =============================================================================
// Types
// =============================================================================

interface Comment {
  id: string
  task_id: string
  parent_comment_id: string | null
  author: string
  author_type: string
  body: string
  created_at: string
  updated_at: string
}

interface ThreadedComment extends Comment {
  replies: ThreadedComment[]
}

// =============================================================================
// Helpers
// =============================================================================

function getBaseUrl(): string {
  return ''
}

function authorIcon(type: string): string {
  if (type === 'agent') return '\u2699'
  if (type === 'human') return '\u{1F464}'
  return '\u{1F4BB}'
}

function shortAuthor(author: string): string {
  if (author.startsWith('#')) return author
  return author.length > 16 ? author.slice(0, 12) + '...' : author
}

/** Build threaded tree from flat comment list */
function buildThreads(comments: Comment[]): ThreadedComment[] {
  const map = new Map<string, ThreadedComment>()
  const roots: ThreadedComment[] = []

  for (const c of comments) {
    map.set(c.id, { ...c, replies: [] })
  }

  for (const c of comments) {
    const node = map.get(c.id)!
    if (c.parent_comment_id && map.has(c.parent_comment_id)) {
      map.get(c.parent_comment_id)!.replies.push(node)
    } else {
      roots.push(node)
    }
  }

  return roots
}

/** Parse @mentions from text */
function renderWithMentions(text: string): (string | JSX.Element)[] {
  const parts: (string | JSX.Element)[] = []
  const regex = /@(\w[\w.-]*)/g
  let lastIndex = 0
  let match: RegExpExecArray | null

  while ((match = regex.exec(text)) !== null) {
    if (match.index > lastIndex) {
      parts.push(text.slice(lastIndex, match.index))
    }
    parts.push(
      <span key={match.index} className="task-comment-mention">@{match[1]}</span>
    )
    lastIndex = match.index + match[0].length
  }

  if (lastIndex < text.length) {
    parts.push(text.slice(lastIndex))
  }

  return parts
}

// =============================================================================
// MentionInput - textarea with @mention autocomplete
// =============================================================================

interface KnownAuthor {
  id: string
  label: string
}

function MentionInput({
  value,
  onChange,
  onSubmit,
  placeholder,
  authors,
}: {
  value: string
  onChange: (v: string) => void
  onSubmit: () => void
  placeholder: string
  authors: KnownAuthor[]
}) {
  const [showSuggestions, setShowSuggestions] = useState(false)
  const [query, setQuery] = useState('')
  const [cursorPos, setCursorPos] = useState(0)
  const textareaRef = useRef<HTMLTextAreaElement>(null)

  const filtered = useMemo(() => {
    if (!query) return authors.slice(0, 5)
    const q = query.toLowerCase()
    return authors.filter(a => a.label.toLowerCase().includes(q) || a.id.toLowerCase().includes(q)).slice(0, 5)
  }, [authors, query])

  const handleChange = (e: React.ChangeEvent<HTMLTextAreaElement>) => {
    const v = e.target.value
    const pos = e.target.selectionStart || 0
    onChange(v)
    setCursorPos(pos)

    // Detect @mention trigger
    const before = v.slice(0, pos)
    const mentionMatch = before.match(/@(\w*)$/)
    if (mentionMatch) {
      setQuery(mentionMatch[1])
      setShowSuggestions(true)
    } else {
      setShowSuggestions(false)
    }
  }

  const insertMention = (author: KnownAuthor) => {
    const before = value.slice(0, cursorPos)
    const after = value.slice(cursorPos)
    const mentionMatch = before.match(/@(\w*)$/)
    if (mentionMatch) {
      const start = cursorPos - mentionMatch[0].length
      const newValue = value.slice(0, start) + `@${author.label} ` + after
      onChange(newValue)
    }
    setShowSuggestions(false)
    textareaRef.current?.focus()
  }

  const handleKeyDown = (e: React.KeyboardEvent) => {
    if (e.key === 'Enter' && (e.metaKey || e.ctrlKey)) {
      e.preventDefault()
      onSubmit()
    }
  }

  return (
    <div className="task-comment-input-wrapper">
      <textarea
        ref={textareaRef}
        className="task-comment-textarea"
        value={value}
        onChange={handleChange}
        onKeyDown={handleKeyDown}
        placeholder={placeholder}
        rows={2}
      />
      {showSuggestions && filtered.length > 0 && (
        <div className="task-comment-suggestions">
          {filtered.map(a => (
            <button
              key={a.id}
              className="task-comment-suggestion"
              onMouseDown={e => { e.preventDefault(); insertMention(a) }}
            >
              <span className="task-comment-suggestion-label">{a.label}</span>
              <span className="task-comment-suggestion-id">{shortAuthor(a.id)}</span>
            </button>
          ))}
        </div>
      )}
    </div>
  )
}

// =============================================================================
// CommentNode (recursive)
// =============================================================================

function CommentNode({
  comment,
  depth,
  authors,
  onReply,
}: {
  comment: ThreadedComment
  depth: number
  authors: KnownAuthor[]
  onReply: (parentId: string, body: string) => void
}) {
  const [showReply, setShowReply] = useState(false)
  const [replyText, setReplyText] = useState('')

  const handleSubmitReply = () => {
    if (!replyText.trim()) return
    onReply(comment.id, replyText.trim())
    setReplyText('')
    setShowReply(false)
  }

  return (
    <div className={`task-comment-node ${depth > 0 ? 'task-comment-node--nested' : ''}`}>
      <div className="task-comment-header">
        <span className="task-comment-author-icon">{authorIcon(comment.author_type)}</span>
        <span className="task-comment-author">{shortAuthor(comment.author)}</span>
        <span className="task-comment-time">{relativeTime(comment.created_at)}</span>
      </div>
      <div className="task-comment-body">
        {renderWithMentions(comment.body)}
      </div>
      <div className="task-comment-actions">
        <button
          className="task-comment-reply-btn"
          onClick={() => setShowReply(!showReply)}
        >
          {showReply ? 'Cancel' : 'Reply'}
        </button>
      </div>

      {showReply && (
        <div className="task-comment-reply-form">
          <MentionInput
            value={replyText}
            onChange={setReplyText}
            onSubmit={handleSubmitReply}
            placeholder="Reply... (Cmd+Enter to send)"
            authors={authors}
          />
          <button
            className="task-comment-send-btn"
            onClick={handleSubmitReply}
            disabled={!replyText.trim()}
          >
            Send
          </button>
        </div>
      )}

      {comment.replies.length > 0 && (
        <div className="task-comment-replies">
          {comment.replies.map(reply => (
            <CommentNode
              key={reply.id}
              comment={reply}
              depth={depth + 1}
              authors={authors}
              onReply={onReply}
            />
          ))}
        </div>
      )}
    </div>
  )
}

// =============================================================================
// TaskComments
// =============================================================================

interface TaskCommentsProps {
  taskId: string
}

export function TaskComments({ taskId }: TaskCommentsProps) {
  const [comments, setComments] = useState<Comment[]>([])
  const [isLoading, setIsLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [newComment, setNewComment] = useState('')
  const [authors, setAuthors] = useState<KnownAuthor[]>([])

  const fetchComments = useCallback(async () => {
    setIsLoading(true)
    setError(null)
    try {
      const baseUrl = getBaseUrl()
      const response = await fetch(`${baseUrl}/api/tasks/${encodeURIComponent(taskId)}/comments`)
      if (response.ok) {
        const data = await response.json()
        setComments(data.comments || [])
      } else {
        throw new Error(`Failed to fetch comments: ${response.statusText}`)
      }
    } catch (e) {
      console.error('Failed to fetch comments:', e)
      setError('Failed to load comments')
    } finally {
      setIsLoading(false)
    }
  }, [taskId])

  const fetchAuthors = useCallback(async () => {
    try {
      const baseUrl = getBaseUrl()
      const response = await fetch(`${baseUrl}/api/sessions?limit=30`)
      if (response.ok) {
        const data = await response.json()
        const sessions: Array<{ id: string; agent_name?: string; cli_type?: string }> = data.sessions || []
        const seen = new Set<string>()
        const results: KnownAuthor[] = []
        for (const s of sessions) {
          const name = s.agent_name || s.cli_type || null
          const key = name || s.id
          if (seen.has(key)) continue
          seen.add(key)
          results.push({ id: s.id, label: name || shortAuthor(s.id) })
        }
        setAuthors(results)
      }
    } catch (e) {
      console.error('Failed to fetch authors:', e)
    }
  }, [])

  useEffect(() => {
    fetchComments()
    fetchAuthors()
  }, [fetchComments, fetchAuthors])

  const handlePost = useCallback(async (body: string, parentId?: string) => {
    try {
      const baseUrl = getBaseUrl()
      const response = await fetch(`${baseUrl}/api/tasks/${encodeURIComponent(taskId)}/comments`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          body,
          author: 'web-user',
          author_type: 'human',
          parent_comment_id: parentId || null,
        }),
      })
      if (response.ok) {
        fetchComments()
      } else {
        console.error('Failed to post comment:', response.status)
      }
    } catch (e) {
      console.error('Failed to post comment:', e)
    }
  }, [taskId, fetchComments])

  const handleNewComment = () => {
    if (!newComment.trim()) return
    handlePost(newComment.trim())
    setNewComment('')
  }

  const handleReply = (parentId: string, body: string) => {
    handlePost(body, parentId)
  }

  const threads = useMemo(() => buildThreads(comments), [comments])

  if (isLoading && comments.length === 0) {
    return <div className="task-comments-loading">Loading comments...</div>
  }

  if (error && comments.length === 0) {
    return <div className="task-comments-error">{error}</div>
  }

  return (
    <div className="task-comments">
      {/* Thread list */}
      {threads.length > 0 ? (
        <div className="task-comments-list">
          {threads.map(thread => (
            <CommentNode
              key={thread.id}
              comment={thread}
              depth={0}
              authors={authors}
              onReply={handleReply}
            />
          ))}
        </div>
      ) : (
        <div className="task-comments-empty">No comments yet</div>
      )}

      {/* New comment input */}
      <div className="task-comment-compose">
        <MentionInput
          value={newComment}
          onChange={setNewComment}
          onSubmit={handleNewComment}
          placeholder="Add a comment... (Cmd+Enter to send)"
          authors={authors}
        />
        <button
          className="task-comment-send-btn"
          onClick={handleNewComment}
          disabled={!newComment.trim()}
        >
          Comment
        </button>
      </div>

      <span className="task-comments-count">{comments.length} comment{comments.length !== 1 ? 's' : ''}</span>
    </div>
  )
}
