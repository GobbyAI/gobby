import type {
  GobbyMemory,
  MemoryFilters,
  MemoryStats,
  MemoryVisibility,
} from "../../../hooks/useMemory";
import {
  DEFAULT_GRAPH_LIMITS,
  sanitizeGraphLimit,
  type GraphLimits,
} from "./KnowledgeGraphModel";

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

export function memoryScopeLabel(memory: Pick<GobbyMemory, "is_global">): "Global" | "Project" {
  return memory.is_global ? "Global" : "Project";
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

export type DreamFlag = "review" | "delete";

export type DreamPurgeGraceDays = Partial<Record<DreamFlag, number>>;

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
export function purgeCountdownLabel(
  memory: GobbyMemory,
  graceDays: DreamPurgeGraceDays | null,
  now = Date.now(),
): string | null {
  const flag = memoryDreamFlag(memory);
  if (!flag || !memory.deleted_at) return null;
  const grace = graceDays?.[flag];
  if (!Number.isFinite(grace)) return null;
  const hiddenAt = new Date(memory.deleted_at).getTime();
  if (!Number.isFinite(hiddenAt)) return null;
  const elapsedDays = (now - hiddenAt) / MS_PER_DAY;
  const remaining = Math.ceil(Number(grace) - elapsedDays);
  if (remaining <= 0) return "Purges on next sweep";
  if (remaining === 1) return "Purges in 1 day";
  return `Purges in ${remaining} days`;
}

function positiveNumber(value: unknown): number | null {
  if (typeof value !== "number" || !Number.isFinite(value) || value <= 0) return null;
  return value;
}

export function extractDreamPurgeGraceDays(values: unknown): DreamPurgeGraceDays | null {
  if (!values || typeof values !== "object" || Array.isArray(values)) return null;
  const memory = (values as { memory?: unknown }).memory;
  if (!memory || typeof memory !== "object" || Array.isArray(memory)) return null;
  const dream = (memory as { dream?: unknown }).dream;
  if (!dream || typeof dream !== "object" || Array.isArray(dream)) return null;

  const review = positiveNumber(
    (dream as { purge_review_after_days?: unknown }).purge_review_after_days,
  );
  const deleteGrace = positiveNumber(
    (dream as { purge_delete_after_days?: unknown }).purge_delete_after_days,
  );
  if (review === null && deleteGrace === null) return null;
  return {
    ...(review !== null ? { review } : {}),
    ...(deleteGrace !== null ? { delete: deleteGrace } : {}),
  };
}

/**
 * Pull the persisted knowledge-graph fetch limits out of an
 * `/api/config/values` payload (`ui.knowledge_graph_limit` /
 * `ui.knowledge_graph_relationship_limit`). Missing or malformed values fall
 * back to the defaults so the graph always has usable limits (#19157).
 */
export function extractGraphLimits(values: unknown): GraphLimits {
  if (!values || typeof values !== "object" || Array.isArray(values)) return DEFAULT_GRAPH_LIMITS;
  const ui = (values as { ui?: unknown }).ui;
  if (!ui || typeof ui !== "object" || Array.isArray(ui)) return DEFAULT_GRAPH_LIMITS;
  const rawEntities = (ui as { knowledge_graph_limit?: unknown }).knowledge_graph_limit;
  const rawRelationships = (ui as { knowledge_graph_relationship_limit?: unknown })
    .knowledge_graph_relationship_limit;
  const numericLimit = (value: unknown) =>
    typeof value === "number" || (typeof value === "string" && value.trim() !== "")
      ? Number(value)
      : Number.NaN;
  return {
    entities: sanitizeGraphLimit(numericLimit(rawEntities), DEFAULT_GRAPH_LIMITS.entities),
    relationships: sanitizeGraphLimit(
      numericLimit(rawRelationships),
      DEFAULT_GRAPH_LIMITS.relationships,
    ),
  };
}
