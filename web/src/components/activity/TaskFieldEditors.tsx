import {
  useCallback,
  useEffect,
  useRef,
  useState,
  type KeyboardEvent,
} from "react";
import { cn } from "../../lib/utils";
import { Button } from "../ui/Button";
import { coarseHitAreaCls } from "../ui/controlStyles";
import { Input } from "../ui/Input";
import { NativeSelect } from "../ui/NativeSelect";
import { Textarea } from "../ui/Textarea";

/**
 * D4 — inline field editors for PATCH-family task fields.
 *
 * Presentational primitives only: they own a local draft, commit the
 * changed value via `onCommit`, and never call endpoints. The host (D5)
 * wires `onCommit` to useTaskInlineEdit.commitField. State/stage and
 * terminal actions are deliberately NOT free-text here — those render as
 * dedicated action controls in the host.
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
  className?: string;
}

function useSkipNextBlurCommit() {
  const skipNextBlurCommitRef = useRef(false);
  const skipNextBlurCommit = useCallback(() => {
    skipNextBlurCommitRef.current = true;
  }, []);
  const shouldSkipBlurCommit = useCallback(() => {
    if (!skipNextBlurCommitRef.current) return false;
    skipNextBlurCommitRef.current = false;
    return true;
  }, []);
  return { shouldSkipBlurCommit, skipNextBlurCommit };
}

export function TaskTextField({
  value,
  onCommit,
  disabled,
  ariaLabel,
  placeholder,
  className,
}: TaskTextFieldProps) {
  const [editorState, setEditorState] = useState(() => ({
    sourceValue: value,
    committed: value,
    draft: value,
  }));
  const { committed, draft } =
    editorState.sourceValue === value
      ? editorState
      : { committed: value, draft: value };
  const { shouldSkipBlurCommit, skipNextBlurCommit } = useSkipNextBlurCommit();

  const commit = useCallback(() => {
    const next = draft.trim();
    setEditorState({ sourceValue: value, committed: next, draft: next });
    if (next === committed) return;
    onCommit(next);
  }, [committed, draft, onCommit, value]);

  const onKeyDown = useCallback((event: KeyboardEvent<HTMLInputElement>) => {
    if (event.key === "Enter") {
      event.preventDefault();
      event.currentTarget.blur();
    } else if (event.key === "Escape") {
      event.preventDefault();
      skipNextBlurCommit();
      setEditorState({ sourceValue: value, committed, draft: committed });
      event.currentTarget.blur();
    }
  }, [committed, skipNextBlurCommit, value]);

  return (
    <Input
      type="text"
      className={cn(
        "w-full rounded-[0.35rem] border-border bg-[var(--bg-secondary)] px-[0.55rem] py-[0.4rem] text-[length:var(--text-sm)] text-[var(--text-primary)] focus-visible:border-accent focus-visible:outline-2 focus-visible:outline-offset-1 focus-visible:outline-accent disabled:cursor-not-allowed disabled:opacity-60",
        className,
      )}
      aria-label={ariaLabel}
      placeholder={placeholder}
      value={draft}
      disabled={disabled}
      onChange={(event) =>
        setEditorState({ sourceValue: value, committed, draft: event.target.value })
      }
      onKeyDown={onKeyDown}
      onBlur={() => {
        if (shouldSkipBlurCommit()) return;
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
  const [editorState, setEditorState] = useState(() => ({
    sourceValue: value,
    committed: value,
    draft: value,
  }));
  const { committed, draft } =
    editorState.sourceValue === value
      ? editorState
      : { committed: value, draft: value };
  const timerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const { shouldSkipBlurCommit, skipNextBlurCommit } = useSkipNextBlurCommit();

  const clearTimer = useCallback(() => {
    if (timerRef.current !== null) {
      clearTimeout(timerRef.current);
      timerRef.current = null;
    }
  }, []);

  useEffect(() => {
    clearTimer();
  }, [clearTimer, value]);

  useEffect(() => clearTimer, [clearTimer]);

  const commit = useCallback(
    (next: string) => {
      const committedNext = next.trim();
      setEditorState({
        sourceValue: value,
        committed: committedNext,
        draft: committedNext,
      });
      if (committedNext === committed) return;
      onCommit(committedNext);
    },
    [committed, onCommit, value],
  );

  const onChange = useCallback(
    (next: string) => {
      setEditorState({ sourceValue: value, committed, draft: next });
      clearTimer();
      timerRef.current = setTimeout(() => {
        timerRef.current = null;
        commit(next);
      }, debounceMs);
    },
    [clearTimer, commit, committed, debounceMs, value],
  );

  const onKeyDown = useCallback(
    (event: KeyboardEvent<HTMLTextAreaElement>) => {
      if (event.key === "Escape") {
        event.preventDefault();
        clearTimer();
        skipNextBlurCommit();
        setEditorState({ sourceValue: value, committed, draft: committed });
        event.currentTarget.blur();
      }
    },
    [clearTimer, committed, skipNextBlurCommit, value],
  );

  return (
    <Textarea
      className="min-h-[4.5rem] w-full resize-y rounded-[0.35rem] border-border bg-[var(--bg-secondary)] px-[0.55rem] py-[0.4rem] text-[length:var(--text-sm)] leading-normal text-[var(--text-primary)] focus-visible:border-accent focus-visible:outline-2 focus-visible:outline-offset-1 focus-visible:outline-accent disabled:cursor-not-allowed disabled:opacity-60"
      aria-label={ariaLabel}
      placeholder={placeholder}
      rows={rows}
      value={draft}
      disabled={disabled}
      onChange={(event) => onChange(event.target.value)}
      onKeyDown={onKeyDown}
      onBlur={() => {
        // Blur commits exactly once after canceling pending debounce.
        clearTimer();
        if (shouldSkipBlurCommit()) return;
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
    <NativeSelect
      className="w-full rounded-[0.35rem] border-border bg-[var(--bg-secondary)] px-[0.55rem] py-[0.4rem] text-[length:var(--text-sm)] text-[var(--text-primary)] focus-visible:border-accent focus-visible:outline-2 focus-visible:outline-offset-1 focus-visible:outline-accent disabled:cursor-not-allowed disabled:opacity-60"
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
    </NativeSelect>
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
  const [editorState, setEditorState] = useState(() => ({
    sourceValue: value,
    committed: value,
    tags: value,
  }));
  const { committed, tags } = sameTags(editorState.sourceValue, value)
    ? editorState
    : { committed: value, tags: value };
  const [entry, setEntry] = useState("");
  const { shouldSkipBlurCommit, skipNextBlurCommit } = useSkipNextBlurCommit();

  const commit = useCallback(
    (next: string[]) => {
      setEditorState({ sourceValue: value, committed: next, tags: next });
      if (sameTags(next, committed)) return;
      onCommit(next);
    },
    [committed, onCommit, value],
  );

  const addTag = useCallback(() => {
    const tag = entry.trim();
    if (!tag || tags.includes(tag)) {
      setEntry("");
      return;
    }
    const next = [...tags, tag];
    setEntry("");
    commit(next);
  }, [commit, entry, tags]);

  const removeTag = useCallback((tag: string) => {
    const next = tags.filter((existing) => existing !== tag);
    commit(next);
  }, [commit, tags]);

  const onKeyDown = useCallback(
    (event: KeyboardEvent<HTMLInputElement>) => {
      if (event.key === "Enter" || event.key === ",") {
        event.preventDefault();
        addTag();
      } else if (event.key === "Backspace" && entry === "" && tags.length > 0) {
        event.preventDefault();
        const next = tags.slice(0, -1);
        commit(next);
      } else if (event.key === "Escape") {
        event.preventDefault();
        skipNextBlurCommit();
        setEditorState({ sourceValue: value, committed, tags: committed });
        setEntry("");
        event.currentTarget.blur();
      }
    },
    [addTag, commit, committed, entry, skipNextBlurCommit, tags, value],
  );

  return (
    <div
      className="flex flex-wrap items-center gap-[0.3rem] rounded-[0.35rem] border border-border bg-[var(--bg-secondary)] px-[0.4rem] py-[0.3rem]"
      role="group"
      aria-label={ariaLabel}
    >
      {tags.map((tag) => (
        <span
          key={tag}
          className="inline-flex h-5 items-center gap-1 rounded-full bg-[var(--accent-tint)] px-2 font-sans text-[length:var(--text-2xs)] font-[var(--font-weight-semibold)] text-accent"
        >
          {tag}
          <Button
            type="button"
            variant="ghost"
            size="icon"
            dense
            className={cn(
              coarseHitAreaCls,
              "h-auto min-h-0 w-auto cursor-pointer p-0 text-[length:var(--text-sm)] leading-none text-[var(--text-muted)] hover:text-[var(--text-primary)]",
            )}
            aria-label={`Remove label ${tag}`}
            disabled={disabled}
            onClick={() => removeTag(tag)}
          >
            ×
          </Button>
        </span>
      ))}
      <Input
        type="text"
        className="min-w-16 flex-[1_1_4rem] border-0 bg-transparent px-[0.2rem] py-[0.15rem] text-[length:var(--text-sm)] text-[var(--text-primary)] focus-visible:border-accent focus-visible:outline-2 focus-visible:outline-offset-1 focus-visible:outline-accent"
        wrapperClassName="w-auto min-w-16 flex-1"
        aria-label="Add label"
        value={entry}
        disabled={disabled}
        onChange={(event) => setEntry(event.target.value)}
        onKeyDown={onKeyDown}
        onBlur={() => {
          if (shouldSkipBlurCommit()) return;
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
