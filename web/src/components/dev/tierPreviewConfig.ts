export type TierId = "portrait" | "landscape" | "desktop";

export interface TierSpec {
  label: string;
  width: number | null;
  height: number | null;
  /** Phone tiers emulate a touch pointer inside the preview iframe. */
  pointer: "coarse" | null;
}

/* Fixed dimensions mirror the responsive tier contract: portrait/landscape
   phone viewports render at exact tier size; desktop fills the stage. */
export const TIERS: Record<TierId, TierSpec> = {
  portrait: { label: "Portrait", width: 440, height: 956, pointer: "coarse" },
  landscape: { label: "Landscape", width: 932, height: 430, pointer: "coarse" },
  desktop: { label: "Desktop", width: null, height: null, pointer: null },
};

const POINTER_PARAM = "pointer";

/** Query-param switch read by main.tsx before mounting the app. */
export function isTierPreviewRequested(search: string): boolean {
  return new URLSearchParams(search).has("tier-preview");
}

/** The preview iframe's src for a tier: phone tiers carry the pointer emulation. */
export function tierFrameSrc(tier: TierSpec): string {
  return tier.pointer ? `/?${POINTER_PARAM}=${tier.pointer}` : "/";
}

/**
 * Stamp the requested pointer emulation on the document root. The CSS
 * `pointer-coarse` variant (tailwind-theme.css), tokens.css and
 * useCoarsePointer all honour `data-pointer="coarse"` alongside the real
 * media query, which a desktop iframe can never match.
 */
export function applyPointerOverride(search: string, root: HTMLElement): void {
  if (new URLSearchParams(search).get(POINTER_PARAM) === "coarse") {
    root.dataset.pointer = "coarse";
  }
}
