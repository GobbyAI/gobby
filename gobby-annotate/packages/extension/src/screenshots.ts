import { viewport } from "./frame-agent";

export type Fingerprint = {
  document: string;
  url: string;
  viewport: ReturnType<typeof viewport>;
};
export function fingerprint(documentId: string): Fingerprint {
  return { document: documentId, url: location.href, viewport: viewport() };
}
export function sameCapture(a: Fingerprint, b: Fingerprint): boolean {
  // Messaging may reorder keys; compare state rather than its serialization.
  const av = a.viewport,
    bv = b.viewport;
  return (
    a.document === b.document &&
    a.url === b.url &&
    av.layout.width === bv.layout.width &&
    av.layout.height === bv.layout.height &&
    av.visual.width === bv.visual.width &&
    av.visual.height === bv.visual.height &&
    av.visual.offsetLeft === bv.visual.offsetLeft &&
    av.visual.offsetTop === bv.visual.offsetTop &&
    av.visual.scale === bv.visual.scale &&
    av.devicePixelRatio === bv.devicePixelRatio &&
    av.scrollX === bv.scrollX &&
    av.scrollY === bv.scrollY
  );
}
export async function captureHidden<T>(
  host: HTMLElement,
  capture: () => Promise<T>,
): Promise<T> {
  const previous = host.style.visibility;
  host.style.visibility = "hidden";
  try {
    await new Promise<void>((resolve) =>
      requestAnimationFrame(() => requestAnimationFrame(() => resolve())),
    );
    return await capture();
  } finally {
    host.style.visibility = previous;
  }
}
export function base64(bytes: Uint8Array): string {
  let str = "";
  for (let i = 0; i < bytes.length; i += 8192)
    str += String.fromCharCode(...bytes.subarray(i, i + 8192));
  return btoa(str);
}
