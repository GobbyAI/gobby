import type { GobbyMemory, MemoryFilters, MemoryStats } from "../../../hooks/useMemory";

export interface MemoryTypeOption {
  value: string;
  label: string;
}

export const MEMORY_TYPE_OPTIONS: MemoryTypeOption[] = [
  { value: "fact", label: "Fact" },
  { value: "preference", label: "Preference" },
  { value: "pattern", label: "Pattern" },
  { value: "context", label: "Context" },
];

export interface MemoryTabFilters {
  search: string;
  memoryType: string | null;
  recentOnly: boolean;
}

export function filtersFromMemoryHook(filters: MemoryFilters): MemoryTabFilters {
  return {
    search: filters.search,
    memoryType: filters.memoryType,
    recentOnly: filters.recentOnly,
  };
}

export function memoryTypeLabel(type: string): string {
  return MEMORY_TYPE_OPTIONS.find((option) => option.value === type)?.label ?? type;
}

export function memoryTypeCount(stats: MemoryStats | null, type: string): number {
  return stats?.by_type?.[type] ?? 0;
}

export function isRecentMemory(memory: GobbyMemory, now = Date.now()): boolean {
  const createdAt = new Date(memory.created_at).getTime();
  if (!Number.isFinite(createdAt)) return false;
  return createdAt > now - 24 * 60 * 60 * 1000;
}

export function filterMemories(
  memories: GobbyMemory[],
  filters: MemoryTabFilters,
  now = Date.now(),
): GobbyMemory[] {
  const query = filters.search.trim().toLowerCase();
  return memories.filter((memory) => {
    if (filters.memoryType && memory.memory_type !== filters.memoryType) return false;
    if (filters.recentOnly && !isRecentMemory(memory, now)) return false;
    if (!query) return true;
    return (
      memory.content.toLowerCase().includes(query) ||
      memory.memory_type.toLowerCase().includes(query) ||
      (memory.tags ?? []).some((tag) => tag.toLowerCase().includes(query))
    );
  });
}

export function normalizeMemoryTags(tags: string[] | null): string[] {
  return Array.isArray(tags) ? tags : [];
}
