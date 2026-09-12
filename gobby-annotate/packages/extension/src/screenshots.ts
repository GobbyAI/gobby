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
  return JSON.stringify(a) === JSON.stringify(b);
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
