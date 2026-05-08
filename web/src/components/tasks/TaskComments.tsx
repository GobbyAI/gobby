import { useState, useEffect, useCallback, useMemo, useRef } from 'react'
import { relativeTime } from '../../utils/formatTime'
import { cn } from '../../lib/utils'

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

const ROOT_CLS = 'flex flex-col gap-2'
const STATE_TEXT_CLS = 'py-1 text-[length:var(--text-sm)] text-[var(--text-muted)]'
const COUNT_CLS = 'pt-1 text-[length:var(--text-xs)] text-[var(--text-muted)]'
const LIST_CLS = 'flex flex-col gap-0.5'
const EMPTY_CLS = 'py-1 text-[length:var(--text-sm)] text-[var(--text-muted)]'

const NODE_CLS = 'border-b border-[var(--border)] py-1.5 last:border-b-0'
const NODE_NESTED_CLS = 'ml-4 border-b-0 pl-2'

const HEADER_CLS = 'mb-[3px] flex items-center gap-1.5'
const AUTHOR_ICON_CLS = 'text-[length:var(--text-sm)]'
const AUTHOR_CLS = 'text-[length:var(--text-sm)] font-semibold text-[var(--text-primary)]'
const TIME_CLS = 'ml-auto text-[length:var(--text-2xs)] text-[var(--text-muted)]'
const BODY_CLS = 'whitespace-pre-wrap break-words text-[length:var(--text-sm)] leading-[1.5] text-[var(--text-secondary)]'
const MENTION_CLS =
  'rounded-sm bg-[color-mix(in_srgb,var(--color-info)_10%,transparent)] px-0.5 font-semibold text-[var(--color-info)]'
const ACTIONS_CLS = 'mt-[3px]'
const REPLY_BTN_CLS =
  'cursor-pointer border-0 bg-transparent p-0 text-[length:var(--text-xs)] text-[var(--text-muted)] hover:text-[var(--text-primary)]'
const REPLIES_CLS = 'mt-1'

const COMPOSE_CLS = 'mt-1 flex items-end gap-1.5'
const INPUT_WRAPPER_CLS = 'relative flex-1'
const TEXTAREA_CLS =
  'block min-h-9 w-full resize-y rounded border border-[var(--border)] bg-[var(--bg-secondary)] px-2 py-1.5 font-[inherit] text-[length:var(--text-sm)] text-[var(--text-primary)] focus:border-[var(--accent)] focus:outline-none'
const SEND_BTN_CLS =
  'cursor-pointer whitespace-nowrap rounded border border-[color-mix(in_srgb,var(--color-info)_30%,transparent)] bg-[var(--color-info-soft)] px-3 py-[5px] font-[inherit] text-[length:var(--text-xs)] text-[var(--color-info)] enabled:hover:bg-[color-mix(in_srgb,var(--color-info)_22%,transparent)] disabled:cursor-default disabled:opacity-40 pointer-coarse:min-h-11'

const SUGGESTIONS_CLS =
  'absolute bottom-full left-0 z-50 mb-1 min-w-[180px] rounded border border-[var(--border)] bg-[var(--bg-primary)] p-[3px] shadow-[var(--shadow-md)]'
const SUGGESTION_CLS =
  'flex w-full cursor-pointer items-center gap-1.5 rounded-sm border-0 bg-transparent px-2 py-1 text-left font-[inherit] text-[length:var(--text-sm)] text-[var(--text-secondary)] hover:bg-[var(--bg-secondary)] hover:text-[var(--text-primary)] pointer-coarse:min-h-11'
const SUGGESTION_LABEL_CLS = 'flex-1'
const SUGGESTION_ID_CLS = 'font-[inherit] text-[length:var(--text-2xs)] text-[var(--text-muted)]'

function getBaseUrl(): string {
  return ''
}

function authorIcon(type: string): string {
  if (type === 'agent') return '⚙'
  if (type === 'human') return '\u{1F464}'
  return '\u{1F4BB}'
}

function shortAuthor(author: string): string {
  if (author.startsWith('#')) return author
  return author.length > 16 ? author.slice(0, 12) + '...' : author
}

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
      <span key={match.index} className={MENTION_CLS}>@{match[1]}</span>
    )
    lastIndex = match.index + match[0].length
  }

  if (lastIndex < text.length) {
    parts.push(text.slice(lastIndex))
  }

  return parts
}

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
    <div className={INPUT_WRAPPER_CLS}>
      <textarea
        ref={textareaRef}
        className={TEXTAREA_CLS}
        value={value}
        onChange={handleChange}
        onKeyDown={handleKeyDown}
        placeholder={placeholder}
        rows={2}
      />
      {showSuggestions && filtered.length > 0 && (
        <div className={SUGGESTIONS_CLS}>
          {filtered.map(a => (
            <button
              key={a.id}
              className={SUGGESTION_CLS}
              onMouseDown={e => { e.preventDefault(); insertMention(a) }}
            >
              <span className={SUGGESTION_LABEL_CLS}>{a.label}</span>
              <span className={SUGGESTION_ID_CLS}>{shortAuthor(a.id)}</span>
            </button>
          ))}
        </div>
      )}
    </div>
  )
}

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
    <div className={cn(NODE_CLS, depth > 0 && NODE_NESTED_CLS)}>
      <div className={HEADER_CLS}>
        <span className={AUTHOR_ICON_CLS}>{authorIcon(comment.author_type)}</span>
        <span className={AUTHOR_CLS}>{shortAuthor(comment.author)}</span>
        <span className={TIME_CLS}>{relativeTime(comment.created_at)}</span>
      </div>
      <div className={BODY_CLS}>
        {renderWithMentions(comment.body)}
      </div>
      <div className={ACTIONS_CLS}>
        <button
          className={REPLY_BTN_CLS}
          onClick={() => setShowReply(!showReply)}
        >
          {showReply ? 'Cancel' : 'Reply'}
        </button>
      </div>

      {showReply && (
        <div className={COMPOSE_CLS}>
          <MentionInput
            value={replyText}
            onChange={setReplyText}
            onSubmit={handleSubmitReply}
            placeholder="Reply... (Cmd+Enter to send)"
            authors={authors}
          />
          <button
            className={SEND_BTN_CLS}
            onClick={handleSubmitReply}
            disabled={!replyText.trim()}
          >
            Send
          </button>
        </div>
      )}

      {comment.replies.length > 0 && (
        <div className={REPLIES_CLS}>
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
    return <div className={STATE_TEXT_CLS}>Loading comments...</div>
  }

  if (error && comments.length === 0) {
    return <div className={STATE_TEXT_CLS}>{error}</div>
  }

  return (
    <div className={ROOT_CLS}>
      {threads.length > 0 ? (
        <div className={LIST_CLS}>
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
        <div className={EMPTY_CLS}>No comments yet</div>
      )}

      <div className={COMPOSE_CLS}>
        <MentionInput
          value={newComment}
          onChange={setNewComment}
          onSubmit={handleNewComment}
          placeholder="Add a comment... (Cmd+Enter to send)"
          authors={authors}
        />
        <button
          className={SEND_BTN_CLS}
          onClick={handleNewComment}
          disabled={!newComment.trim()}
        >
          Comment
        </button>
      </div>

      <span className={COUNT_CLS}>{comments.length} comment{comments.length !== 1 ? 's' : ''}</span>
    </div>
  )
}
