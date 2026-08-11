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
} from '../../../api/runtimeConfigSegments'

export function asString(value: unknown): string {
  return typeof value === 'string' ? value : ''
}

export function asNumber(value: unknown): number | null {
  return typeof value === 'number' ? value : null
}

export function asStringList(value: unknown): string[] {
  return Array.isArray(value)
    ? value.filter((item): item is string => typeof item === 'string')
    : []
}

export function asTypedList<T>(value: unknown): T[] {
  return Array.isArray(value) ? (value as T[]) : []
}

export function asMap<V>(value: unknown): Record<string, V> {
  return value && typeof value === 'object' && !Array.isArray(value)
    ? (value as Record<string, V>)
    : {}
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
export function decodeDynamicMapKeys<V>(map: Record<string, V>): Record<string, V> {
  return Object.fromEntries(
    Object.entries(map).map(([key, value]) => [
      key === '' ? key : decodeDynamicSegmentLenient(key),
      value,
    ]),
  )
}

export function encodeDynamicMapKeys<V>(map: Record<string, V>): Record<string, V> {
  return Object.fromEntries(
    Object.entries(map).map(([key, value]) => [
      key === '' ? key : encodeDynamicSegment(key),
      value,
    ]),
  )
}
