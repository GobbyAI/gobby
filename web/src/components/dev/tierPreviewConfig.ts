export type TierId = "portrait" | "landscape" | "desktop";

export interface TierSpec {
  label: string;
  width: number | null;
  height: number | null;
}

/* Fixed dimensions mirror the responsive tier contract: portrait/landscape
   phone viewports render at exact tier size; desktop fills the stage. */
export const TIERS: Record<TierId, TierSpec> = {
  portrait: { label: "Portrait", width: 440, height: 956 },
  landscape: { label: "Landscape", width: 932, height: 430 },
  desktop: { label: "Desktop", width: null, height: null },
};

/** Query-param switch read by main.tsx before mounting the app. */
export function isTierPreviewRequested(search: string): boolean {
  return new URLSearchParams(search).has("tier-preview");
}
