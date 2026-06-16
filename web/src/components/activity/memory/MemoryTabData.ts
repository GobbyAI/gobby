import type {
  GobbyMemory,
  MemoryFilters,
  MemoryStats,
  MemoryVisibility,
} from "../../../hooks/useMemory";

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

export interface MemoryVisibilityOption {
  value: MemoryVisibility;
  label: string;
}

export const MEMORY_VISIBILITY_OPTIONS: MemoryVisibilityOption[] = [
  { value: "active", label: "Active" },
  { value: "hidden", label: "Hidden" },
  { value: "all", label: "All" },
];

export interface MemoryTabFilters {
  search: string;
  memoryType: string | null;
  recentOnly: boolean;
  visibility: MemoryVisibility;
}

export function filtersFromMemoryHook(filters: MemoryFilters): MemoryTabFilters {
  return {
    search: filters.search,
    memoryType: filters.memoryType,
    recentOnly: filters.recentOnly,
    visibility: filters.visibility,
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

// A memory is "hidden" once the nightly dream sweep soft-deletes it (Dream GC,
// #17165). `deleted_at` is the authoritative flag; `dream_action` records why.
export function isHiddenMemory(memory: GobbyMemory): boolean {
  return Boolean(memory.deleted_at);
}

function matchesVisibility(memory: GobbyMemory, visibility: MemoryVisibility): boolean {
  if (visibility === "all") return true;
  return visibility === "hidden" ? isHiddenMemory(memory) : !isHiddenMemory(memory);
}

export function filterMemories(
  memories: GobbyMemory[],
  filters: MemoryTabFilters,
  now = Date.now(),
): GobbyMemory[] {
  const query = filters.search.trim().toLowerCase();
  return memories.filter((memory) => {
    if (!matchesVisibility(memory, filters.visibility)) return false;
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

// Grace windows (days) before a soft-hidden memory is hard-purged. Mirrors the
// MemoryDreamConfig defaults (purge_review_after_days / purge_delete_after_days);
// the backend measures the grace window from `deleted_at`.
export const DREAM_PURGE_GRACE_DAYS: Record<"review" | "delete", number> = {
  review: 90,
  delete: 30,
};

export type DreamFlag = "review" | "delete";

// Normalized dream action for a hidden memory, or null if the memory is active
// (or carries an unrecognized action).
export function memoryDreamFlag(memory: GobbyMemory): DreamFlag | null {
  if (!isHiddenMemory(memory)) return null;
  return memory.dream_action === "review" || memory.dream_action === "delete"
    ? memory.dream_action
    : null;
}

export function dreamFlagLabel(memory: GobbyMemory): string | null {
  const flag = memoryDreamFlag(memory);
  if (flag === "review") return "Flagged for review";
  if (flag === "delete") return "Flagged for deletion";
  return isHiddenMemory(memory) ? "Flagged" : null;
}

const MS_PER_DAY = 24 * 60 * 60 * 1000;

// Human-readable countdown to hard purge for a hidden memory, or null when the
// memory is active or its `deleted_at` timestamp is unparseable.
export function purgeCountdownLabel(memory: GobbyMemory, now = Date.now()): string | null {
  const flag = memoryDreamFlag(memory);
  if (!flag || !memory.deleted_at) return null;
  const hiddenAt = new Date(memory.deleted_at).getTime();
  if (!Number.isFinite(hiddenAt)) return null;
  const elapsedDays = (now - hiddenAt) / MS_PER_DAY;
  const remaining = Math.ceil(DREAM_PURGE_GRACE_DAYS[flag] - elapsedDays);
  if (remaining <= 0) return "Purges on next sweep";
  if (remaining === 1) return "Purges in 1 day";
  return `Purges in ${remaining} days`;
}
