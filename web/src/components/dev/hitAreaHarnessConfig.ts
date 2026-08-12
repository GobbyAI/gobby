/** Matches TierPreview's query-param gate (`?hit-area-harness`). */
export function isHitAreaHarnessRequested(search: string): boolean {
  return new URLSearchParams(search).has("hit-area-harness");
}
