/**
 * Draft-value accessors shared by config-backed settings sections. A section's
 * draft stores whatever the daemon returned (any shape), so reads coerce to the
 * concrete type the field expects, defaulting safely when the stored value is
 * absent or the wrong shape. Kept in a `.ts` module (no components) so the
 * component wrappers in `configFields.tsx` stay Fast-Refresh friendly.
 */

import {
  decodeDynamicSegmentLenient,
  encodeDynamicSegment,
} from "../../../api/runtimeConfigSegments";

export function asString(value: unknown): string {
  return typeof value === "string" ? value : "";
}

export function asNumber(value: unknown): number | null {
  return typeof value === "number" ? value : null;
}

export function asStringList(value: unknown): string[] {
  return Array.isArray(value)
    ? value.filter((item): item is string => typeof item === "string")
    : [];
}

export function asTypedList<T>(value: unknown): T[] {
  return Array.isArray(value) ? (value as T[]) : [];
}

export function asMap<V>(value: unknown): Record<string, V> {
  return value && typeof value === "object" && !Array.isArray(value)
    ? (value as Record<string, V>)
    : {};
}

/**
 * Map-key halves of the dynamic-segment codec for editors backed by
 * `pattern`-registered config maps (`skills.hubs.{hub}`,
 * `context_window_overrides.{model_match}`, …). The store holds keys in their
 * canonical encoded form so a key containing `.` never reads as a dotted-path
 * separator; editors display the decoded key and re-encode on write. The
 * empty string is the transient just-added row and passes through untouched —
 * the codec rejects empty segments by contract.
 */
export interface DynamicMapRow<V> {
  storedKey: string;
  displayKey: string;
  value: V;
}

export function hasBlankDynamicMapKey<V>(
  rows: readonly DynamicMapRow<V>[],
): boolean {
  return rows.some(({ displayKey }) => displayKey.trim() === "");
}

export function decodeDynamicMapRows<V>(
  map: Record<string, V>,
): DynamicMapRow<V>[] {
  return Object.entries(map).map(([storedKey, value]) => ({
    storedKey,
    displayKey:
      storedKey === "" ? storedKey : decodeDynamicSegmentLenient(storedKey),
    value,
  }));
}

export function encodeDynamicMapRows<V>(
  rows: DynamicMapRow<V>[],
): Record<string, V> {
  const entries: Array<[string, V]> = [];
  const storedKeys = new Set<string>();
  for (const { storedKey, displayKey, value } of rows) {
    const originalDisplayKey =
      storedKey === "" ? storedKey : decodeDynamicSegmentLenient(storedKey);
    const nextStoredKey =
      displayKey === originalDisplayKey
        ? storedKey
        : displayKey === ""
          ? displayKey
          : encodeDynamicSegment(displayKey);
    if (storedKeys.has(nextStoredKey)) {
      throw new Error(
        `Dynamic map rows collide at stored key ${nextStoredKey}`,
      );
    }
    storedKeys.add(nextStoredKey);
    entries.push([nextStoredKey, value]);
  }
  return Object.fromEntries(entries);
}
