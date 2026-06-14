import { useEffect, useMemo, type ReactNode } from "react";

import type { GobbyMemory } from "../../../hooks/useMemory";
import {
  DetailPaneHeader,
  SelectField,
  TagsField,
  TextAreaField,
  useDetailDraft,
} from "../fields";
import type { MemoryDraft } from "./MemoryTabActions";
import { MEMORY_TYPE_OPTIONS, normalizeMemoryTags } from "./MemoryTabData";

interface MemoryDetailPanelProps {
  memory: GobbyMemory | null;
  onSave: (draft: MemoryDraft) => Promise<boolean>;
  onConfirmLeaveChange: (handler: (next: () => void) => void) => void;
  actions?: ReactNode;
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
  actions,
}: MemoryDetailPanelProps) {
  const sourceDraft = useMemo(() => draftFromMemory(memory), [memory]);
  const detailDraft = useDetailDraft<MemoryDraft>({
    source: sourceDraft,
    onSave,
  });
  const { draft, dirty, saving, serverChanged, save, discard, confirmIfDirty } = detailDraft;

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
        </dl>
      </div>
    </div>
  );
}
