import {
  useCallback,
  useEffect,
  useRef,
  useState,
  type KeyboardEvent,
} from "react";

/**
 * D4 — inline field editors for PATCH-family task fields.
 *
 * Presentational primitives only: they own a local draft, commit the
 * changed value via `onCommit`, and never call endpoints. The host (D5)
 * wires `onCommit` to useTaskInlineEdit.commitField. State / assignee /
 * stage are deliberately NOT free-text here — those render as dedicated
 * action controls (claim/release, stage move, close/reopen) in the host.
 *
 * Shared interaction contract: Esc cancels (reverts to the committed
 * value), blur and Enter commit. The committed value is reconciled from
 * the prop during render (React's documented "adjust state when a prop
 * changes" pattern) so a server/WS update replaces the draft without an
 * effect. Visual treatment is owned by the D5 detail-pane redesign; these
 * carry semantic class hooks only.
 */

interface CommonEditorProps {
  disabled?: boolean;
  ariaLabel: string;
}

interface TaskTextFieldProps extends CommonEditorProps {
  value: string;
  onCommit: (next: string) => void;
  placeholder?: string;
}

export function TaskTextField({
  value,
  onCommit,
  disabled,
  ariaLabel,
  placeholder,
}: TaskTextFieldProps) {
  const [committed, setCommitted] = useState(value);
  const [draft, setDraft] = useState(value);
  const skipNextBlurCommitRef = useRef(false);
  if (committed !== value) {
    setCommitted(value);
    setDraft(value);
  }

  const commit = useCallback(() => {
    const next = draft.trim();
    if (next === committed) return;
    setCommitted(next);
    onCommit(next);
  }, [committed, draft, onCommit]);

  const onKeyDown = useCallback((event: KeyboardEvent<HTMLInputElement>) => {
    if (event.key === "Enter") {
      event.preventDefault();
      event.currentTarget.blur();
    } else if (event.key === "Escape") {
      event.preventDefault();
      skipNextBlurCommitRef.current = true;
      setDraft(committed);
      event.currentTarget.blur();
    }
  }, [committed]);

  return (
    <input
      type="text"
      className="task-inline-edit task-inline-edit--text"
      aria-label={ariaLabel}
      placeholder={placeholder}
      value={draft}
      disabled={disabled}
      onChange={(event) => setDraft(event.target.value)}
      onKeyDown={onKeyDown}
      onBlur={() => {
        if (skipNextBlurCommitRef.current) {
          skipNextBlurCommitRef.current = false;
          return;
        }
        commit();
      }}
    />
  );
}

interface TaskTextAreaFieldProps extends CommonEditorProps {
  value: string;
  onCommit: (next: string) => void;
  rows?: number;
  debounceMs?: number;
  placeholder?: string;
}

export function TaskTextAreaField({
  value,
  onCommit,
  disabled,
  ariaLabel,
  rows = 4,
  debounceMs = 600,
  placeholder,
}: TaskTextAreaFieldProps) {
  const [committed, setCommitted] = useState(value);
  const [draft, setDraft] = useState(value);
  const timerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const skipNextBlurCommitRef = useRef(false);

  const clearTimer = useCallback(() => {
    if (timerRef.current !== null) {
      clearTimeout(timerRef.current);
      timerRef.current = null;
    }
  }, []);

  if (committed !== value) {
    setCommitted(value);
    setDraft(value);
  }

  useEffect(() => clearTimer, [clearTimer]);

  const commit = useCallback(
    (next: string) => {
      if (next === committed) return;
      setCommitted(next);
      onCommit(next);
    },
    [committed, onCommit],
  );

  const onChange = useCallback(
    (next: string) => {
      setDraft(next);
      clearTimer();
      timerRef.current = setTimeout(() => {
        timerRef.current = null;
        commit(next);
      }, debounceMs);
    },
    [clearTimer, commit, debounceMs],
  );

  const onKeyDown = useCallback(
    (event: KeyboardEvent<HTMLTextAreaElement>) => {
      if (event.key === "Escape") {
        event.preventDefault();
        clearTimer();
        skipNextBlurCommitRef.current = true;
        setDraft(committed);
        event.currentTarget.blur();
      }
    },
    [clearTimer, committed],
  );

  return (
    <textarea
      className="task-inline-edit task-inline-edit--textarea"
      aria-label={ariaLabel}
      placeholder={placeholder}
      rows={rows}
      value={draft}
      disabled={disabled}
      onChange={(event) => onChange(event.target.value)}
      onKeyDown={onKeyDown}
      onBlur={() => {
        clearTimer();
        if (skipNextBlurCommitRef.current) {
          skipNextBlurCommitRef.current = false;
          return;
        }
        commit(draft);
      }}
    />
  );
}

export interface TaskSelectOption {
  value: string;
  label: string;
}

interface TaskSelectFieldProps extends CommonEditorProps {
  value: string;
  options: TaskSelectOption[];
  onCommit: (next: string) => void;
}

export function TaskSelectField({
  value,
  options,
  onCommit,
  disabled,
  ariaLabel,
}: TaskSelectFieldProps) {
  return (
    <select
      className="task-inline-edit task-inline-edit--select"
      aria-label={ariaLabel}
      value={value}
      disabled={disabled}
      onChange={(event) => {
        if (event.target.value !== value) onCommit(event.target.value);
      }}
    >
      {options.map((option) => (
        <option key={option.value} value={option.value}>
          {option.label}
        </option>
      ))}
    </select>
  );
}

interface TaskTagsFieldProps extends CommonEditorProps {
  value: string[];
  onCommit: (next: string[]) => void;
}

function sameTags(a: string[], b: string[]): boolean {
  return a.length === b.length && a.every((tag, index) => tag === b[index]);
}

export function TaskTagsField({
  value,
  onCommit,
  disabled,
  ariaLabel,
}: TaskTagsFieldProps) {
  const [committed, setCommitted] = useState<string[]>(value);
  const [tags, setTags] = useState<string[]>(value);
  const [entry, setEntry] = useState("");
  const skipNextBlurCommitRef = useRef(false);
  if (!sameTags(committed, value)) {
    setCommitted(value);
    setTags(value);
  }

  const commit = useCallback(
    (next: string[]) => {
      if (sameTags(next, committed)) return;
      setCommitted(next);
      onCommit(next);
    },
    [committed, onCommit],
  );

  const addTag = useCallback(() => {
    const tag = entry.trim();
    if (!tag || tags.includes(tag)) {
      setEntry("");
      return;
    }
    setTags((prev) => [...prev, tag]);
    setEntry("");
  }, [entry, tags]);

  const removeTag = useCallback((tag: string) => {
    setTags((prev) => prev.filter((existing) => existing !== tag));
  }, []);

  const onKeyDown = useCallback(
    (event: KeyboardEvent<HTMLInputElement>) => {
      if (event.key === "Enter" || event.key === ",") {
        event.preventDefault();
        addTag();
      } else if (event.key === "Backspace" && entry === "" && tags.length > 0) {
        event.preventDefault();
        setTags((prev) => prev.slice(0, -1));
      } else if (event.key === "Escape") {
        event.preventDefault();
        skipNextBlurCommitRef.current = true;
        setTags(committed);
        setEntry("");
        event.currentTarget.blur();
      }
    },
    [addTag, committed, entry, tags.length],
  );

  return (
    <div
      className="task-inline-edit task-inline-edit--tags"
      role="group"
      aria-label={ariaLabel}
    >
      {tags.map((tag) => (
        <span key={tag} className="task-inline-edit__tag">
          {tag}
          <button
            type="button"
            className="task-inline-edit__tag-remove"
            aria-label={`Remove label ${tag}`}
            disabled={disabled}
            onClick={() => removeTag(tag)}
          >
            ×
          </button>
        </span>
      ))}
      <input
        type="text"
        className="task-inline-edit__tag-input"
        aria-label="Add label"
        value={entry}
        disabled={disabled}
        onChange={(event) => setEntry(event.target.value)}
        onKeyDown={onKeyDown}
        onBlur={() => {
          if (skipNextBlurCommitRef.current) {
            skipNextBlurCommitRef.current = false;
            return;
          }
          const trimmed = entry.trim();
          const nextTags =
            trimmed && !tags.includes(trimmed) ? [...tags, trimmed] : tags;
          setEntry("");
          commit(nextTags);
        }}
      />
    </div>
  );
}
