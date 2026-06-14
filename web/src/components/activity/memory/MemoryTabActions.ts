import type { GobbyMemory } from "../../../hooks/useMemory";

export interface MemoryDraft {
  id: string;
  content: string;
  memory_type: string;
  tags: string[];
}

export interface SaveMemoryDraftOptions {
  draft: MemoryDraft;
  updateMemory: (
    memoryId: string,
    params: { content?: string; memory_type?: string; tags?: string[] },
  ) => Promise<GobbyMemory | null>;
}

export async function saveMemoryDraft({
  draft,
  updateMemory,
}: SaveMemoryDraftOptions): Promise<boolean> {
  const saved = await updateMemory(draft.id, {
    content: draft.content,
    memory_type: draft.memory_type,
    tags: draft.tags,
  });
  return Boolean(saved);
}

export async function copyMemoryContent(memory: GobbyMemory): Promise<void> {
  await navigator.clipboard?.writeText(memory.content);
}

export async function deleteMemoryWithRefresh({
  memory,
  deleteMemory,
}: {
  memory: GobbyMemory;
  deleteMemory: (memoryId: string) => Promise<boolean>;
}): Promise<boolean> {
  return deleteMemory(memory.id);
}
