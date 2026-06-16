import { useEffect, useMemo, useState, type ReactNode } from "react";

import type { GobbyMemory } from "../../../hooks/useMemory";
import { cn } from "../../../lib/utils";
import {
  DetailPaneHeader,
  SelectField,
  TagsField,
  TextAreaField,
  useDetailDraft,
} from "../fields";
import type { MemoryDraft } from "./MemoryTabActions";
import {
  dreamFlagLabel,
  isHiddenMemory,
  MEMORY_TYPE_OPTIONS,
  memoryDreamFlag,
  normalizeMemoryTags,
  purgeCountdownLabel,
} from "./MemoryTabData";

interface MemoryDetailPanelProps {
  memory: GobbyMemory | null;
  onSave: (draft: MemoryDraft) => Promise<boolean>;
  onConfirmLeaveChange: (handler: (next: () => void) => void) => void;
  onRestore?: (memory: GobbyMemory) => Promise<void> | void;
  actions?: ReactNode;
}

function formatTimestamp(value: string | null): string {
  if (!value) return "—";
  const parsed = new Date(value);
  return Number.isFinite(parsed.getTime()) ? parsed.toLocaleString() : "—";
}

function draftFromMemory(memory: GobbyMemory | null): MemoryDraft | null {
  if (!memory) return null;
  return {
    id: memory.id,
    content: memory.content,
    memory_type: memory.memory_type,
    tags: normalizeMemoryTags(memory.tags),
  };
}

export function MemoryDetailPanel({
  memory,
  onSave,
  onConfirmLeaveChange,
  onRestore,
  actions,
}: MemoryDetailPanelProps) {
  const sourceDraft = useMemo(() => draftFromMemory(memory), [memory]);
  const detailDraft = useDetailDraft<MemoryDraft>({
    source: sourceDraft,
    onSave,
  });
  const { draft, dirty, saving, serverChanged, save, discard, confirmIfDirty } = detailDraft;
  const [restoring, setRestoring] = useState(false);

  useEffect(() => {
    onConfirmLeaveChange(confirmIfDirty);
  }, [confirmIfDirty, onConfirmLeaveChange]);

  if (!draft || !memory) {
    return (
      <div className="flex h-full items-center justify-center px-4 text-sm text-muted-foreground">
        Select a memory to inspect and edit its saved context.
      </div>
    );
  }

  const hidden = isHiddenMemory(memory);
  const isDeleteFlag = memoryDreamFlag(memory) === "delete";
  const flagLabel = dreamFlagLabel(memory);
  const purgeLabel = purgeCountdownLabel(memory);

  return (
    <div className="flex h-full flex-col overflow-hidden">
      <DetailPaneHeader
        title="Memory detail"
        dirty={dirty}
        saving={saving}
        serverChanged={serverChanged}
        actions={actions}
        onSave={() => void save()}
        onDiscard={discard}
      />
      {hidden && (
        <div
          className={cn(
            "flex items-center justify-between gap-3 border-b border-border px-3 py-2",
            isDeleteFlag ? "bg-destructive/10" : "bg-warning/10",
          )}
        >
          <span
            className={cn(
              "text-xs font-medium",
              isDeleteFlag ? "text-destructive-foreground" : "text-warning-foreground",
            )}
          >
            {flagLabel}
            {purgeLabel ? ` · ${purgeLabel}` : ""}
          </span>
          {onRestore && (
            <button
              type="button"
              className="btn btn-ghost btn-sm"
              aria-label="Restore memory"
              disabled={restoring}
              onClick={() => {
                setRestoring(true);
                void Promise.resolve(onRestore(memory)).finally(() => setRestoring(false));
              }}
            >
              {restoring ? "Restoring…" : "Restore"}
            </button>
          )}
        </div>
      )}
      <div className="flex min-h-0 flex-1 flex-col gap-3 overflow-y-auto p-3">
        <TextAreaField
          label="Content"
          ariaLabel="Memory content"
          value={draft.content}
          rows={7}
          onChange={(value) => detailDraft.setField("content", value)}
        />
        <SelectField
          label="Type"
          ariaLabel="Memory type"
          value={draft.memory_type}
          options={MEMORY_TYPE_OPTIONS}
          onChange={(value) => detailDraft.setField("memory_type", value)}
        />
        <TagsField
          label="Tags"
          ariaLabel="Tags"
          value={draft.tags}
          placeholder="Add tag"
          onChange={(value) => detailDraft.setField("tags", value)}
        />
        <dl className="grid grid-cols-[auto_1fr] gap-x-3 gap-y-1 rounded-md border border-border bg-[var(--bg-secondary)] p-3 text-xs">
          <dt className="text-muted-foreground">Created</dt>
          <dd className="text-foreground">{new Date(memory.created_at).toLocaleString()}</dd>
          <dt className="text-muted-foreground">Updated</dt>
          <dd className="text-foreground">{new Date(memory.updated_at).toLocaleString()}</dd>
          <dt className="text-muted-foreground">Accesses</dt>
          <dd className="text-foreground">{memory.access_count}</dd>
          {hidden && (
            <>
              <dt className="text-muted-foreground">Flagged</dt>
              <dd className="text-foreground">{flagLabel}</dd>
              <dt className="text-muted-foreground">Last reviewed</dt>
              <dd className="text-foreground">{formatTimestamp(memory.last_dreamed_at)}</dd>
              <dt className="text-muted-foreground">Purge</dt>
              <dd className="text-foreground">{purgeLabel ?? "—"}</dd>
            </>
          )}
        </dl>
      </div>
    </div>
  );
}
