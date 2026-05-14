import { useState } from 'react'
import type { GobbyMemory } from '../../hooks/useMemory'
import { inputFocusCls } from '../shared/focusStyles'

interface MemoryFormProps {
  memory: GobbyMemory | null
  onSave: (data: MemoryFormData) => void
  onCancel: () => void
}

export interface MemoryFormData {
  content: string
  memory_type: string
  importance: number
  tags: string[]
}

const MEMORY_TYPES = ['fact', 'preference', 'pattern', 'context'] as const

const FIELD_INPUT_CLS =
  `rounded-md border border-[var(--border)] bg-[var(--bg-tertiary)] px-2 py-1.5 font-[inherit] text-[length:var(--text-base)] text-[var(--text-primary)] ${inputFocusCls} pointer-coarse:min-h-11`
const FIELD_LABEL_CLS = 'text-[length:var(--text-sm)] font-medium text-[var(--text-muted)]'
const FIELD_WRAP_CLS = 'flex flex-col gap-1'

export function MemoryForm({ memory, onSave, onCancel }: MemoryFormProps) {
  const [content, setContent] = useState(memory?.content ?? '')
  const [memoryType, setMemoryType] = useState(memory?.memory_type ?? 'fact')
  const [importance, setImportance] = useState(memory?.importance ?? 0.5)
  const [tags, setTags] = useState<string[]>(memory?.tags ?? [])
  const [tagInput, setTagInput] = useState('')
  const [error, setError] = useState<string | null>(null)

  const isEdit = memory !== null

  function handleSubmit(e: React.FormEvent) {
    e.preventDefault()
    if (!content.trim()) {
      setError('Content is required')
      return
    }
    setError(null)
    onSave({
      content: content.trim(),
      memory_type: memoryType,
      importance,
      tags,
    })
  }

  function handleAddTag() {
    const tag = tagInput.trim()
    if (tag && !tags.includes(tag)) {
      setTags([...tags, tag])
    }
    setTagInput('')
  }

  function handleRemoveTag(tag: string) {
    setTags(tags.filter((t) => t !== tag))
  }

  function handleTagKeyDown(e: React.KeyboardEvent) {
    if (e.key === 'Enter') {
      e.preventDefault()
      handleAddTag()
    }
  }

  return (
    <div
      className="fixed inset-0 z-[110] flex items-center justify-center bg-[var(--surface-scrim)]"
      onClick={onCancel}
    >
      <form
        className="flex max-h-[80vh] w-[min(480px,90vw)] flex-col gap-3 overflow-y-auto rounded-xl border border-[var(--border)] bg-[var(--bg-secondary)] p-5"
        role="dialog"
        aria-modal="true"
        aria-labelledby="memory-form-title"
        onSubmit={handleSubmit}
        onClick={(e) => e.stopPropagation()}
      >
        <div className="flex items-center justify-between">
          <h2 id="memory-form-title" className="m-0 text-[length:var(--text-xl)] text-[var(--text-primary)]">
            {isEdit ? 'Edit Memory' : 'Create Memory'}
          </h2>
          <button
            type="button"
            className="flex h-8 w-8 cursor-pointer items-center justify-center border-0 bg-transparent p-1 text-[length:var(--text-2xl)] leading-none text-[var(--text-muted)] hover:text-[var(--text-primary)] pointer-coarse:h-11 pointer-coarse:w-11"
            onClick={onCancel}
            aria-label="Close form"
          >
            &times;
          </button>
        </div>

        {error && (
          <div className="rounded-md border border-[var(--color-error)] bg-[color-mix(in_srgb,var(--color-error)_10%,transparent)] px-2 py-1.5 text-[length:var(--text-md)] text-[var(--color-error)]">
            {error}
          </div>
        )}

        <label className={FIELD_WRAP_CLS}>
          <span className={FIELD_LABEL_CLS}>Content</span>
          <textarea
            className={`${FIELD_INPUT_CLS} box-border w-full resize-y`}
            value={content}
            onChange={(e) => setContent(e.target.value)}
            placeholder="What should be remembered?"
            rows={4}
            autoFocus
          />
        </label>

        <div className="flex gap-3">
          <label className={`${FIELD_WRAP_CLS} flex-1`}>
            <span className={FIELD_LABEL_CLS}>Type</span>
            <select
              className={FIELD_INPUT_CLS}
              value={memoryType}
              onChange={(e) => setMemoryType(e.target.value)}
            >
              {MEMORY_TYPES.map((t) => (
                <option key={t} value={t}>
                  {t.charAt(0).toUpperCase() + t.slice(1)}
                </option>
              ))}
            </select>
          </label>

          <div className={`${FIELD_WRAP_CLS} flex-1`}>
            <label className={FIELD_LABEL_CLS} htmlFor="memory-importance-slider">
              Importance: {(importance * 100).toFixed(0)}%
            </label>
            <input
              id="memory-importance-slider"
              type="range"
              className="w-full accent-[var(--accent)]"
              min="0"
              max="1"
              step="0.05"
              value={importance}
              onChange={(e) => setImportance(Number(e.target.value))}
            />
          </div>
        </div>

        <div className={FIELD_WRAP_CLS} role="group" aria-label="Tags">
          <span className={FIELD_LABEL_CLS}>Tags</span>
          <div className="flex min-h-[2rem] flex-wrap items-center gap-1 rounded-md border border-[var(--border)] bg-[var(--bg-tertiary)] p-1.5">
            {tags.map((tag) => (
              <span
                key={tag}
                className="flex items-center gap-0.5 rounded border border-[var(--border)] bg-[var(--bg-primary)] px-1.5 py-px text-[length:var(--text-sm)] text-[var(--text-secondary)]"
              >
                {tag}
                <button
                  type="button"
                  className="cursor-pointer border-0 bg-transparent p-0 text-[length:var(--text-sm)] leading-none text-[var(--text-muted)] hover:text-[var(--color-error)]"
                  onClick={() => handleRemoveTag(tag)}
                  aria-label={`Remove tag ${tag}`}
                >
                  &times;
                </button>
              </span>
            ))}
            <input
              type="text"
              value={tagInput}
              onChange={(e) => setTagInput(e.target.value)}
              onKeyDown={handleTagKeyDown}
              placeholder="Add tag..."
              className="min-w-[80px] flex-1 border-0 bg-transparent font-[inherit] text-[length:var(--text-md)] text-[var(--text-primary)] outline-none placeholder:text-[var(--text-muted)]"
            />
          </div>
        </div>

        <div className="flex justify-end gap-2 pt-1.5">
          <button
            type="button"
            className="cursor-pointer rounded-md border border-[var(--border)] bg-transparent px-3 py-1.5 text-[length:var(--text-base)] text-[var(--text-secondary)] hover:bg-[var(--bg-tertiary)] pointer-coarse:min-h-11"
            onClick={onCancel}
          >
            Cancel
          </button>
          <button
            type="submit"
            className="cursor-pointer rounded-md border border-[var(--accent)] bg-[var(--accent)] px-3 py-1.5 text-[length:var(--text-base)] font-medium text-[var(--bg-primary)] hover:opacity-90 pointer-coarse:min-h-11"
          >
            {isEdit ? 'Save Changes' : 'Create Memory'}
          </button>
        </div>
      </form>
    </div>
  )
}
