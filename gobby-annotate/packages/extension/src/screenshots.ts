import { viewport } from "./frame-agent";
import type { Bounds, Viewport } from "@gobby/annotate-core";

export function regionCrop(
  bounds: Bounds,
  view: Viewport,
  width: number,
  height: number,
) {
  const scaleX = width / view.visual.width;
  const scaleY = height / view.visual.height;
  const x = Math.max(0, Math.floor(bounds.x * scaleX));
  const y = Math.max(0, Math.floor(bounds.y * scaleY));
  const right = Math.min(width, Math.ceil((bounds.x + bounds.width) * scaleX));
  const bottom = Math.min(
    height,
    Math.ceil((bounds.y + bounds.height) * scaleY),
  );
  if (right <= x || bottom <= y)
    throw new Error(
      "Selected region is outside the screenshot. Select the region again.",
    );
  return {
    x,
    y,
    width: right - x,
    height: bottom - y,
    sourceBounds: {
      x: x / scaleX,
      y: y / scaleY,
      width: (right - x) / scaleX,
      height: (bottom - y) / scaleY,
    },
  };
}

export async function cropRegion(
  capture: { image: string; width: number; height: number },
  bounds: Bounds,
  view: Viewport,
) {
  const crop = regionCrop(bounds, view, capture.width, capture.height);
  const image = new Image();
  image.src = capture.image;
  await image.decode();
  if (
    image.naturalWidth !== capture.width ||
    image.naturalHeight !== capture.height
  )
    throw new Error("Screenshot dimensions changed. Retry capture.");
  const canvas = document.createElement("canvas");
  canvas.width = crop.width;
  canvas.height = crop.height;
  const context = canvas.getContext("2d");
  if (!context) throw new Error("Could not crop screenshot. Retry capture.");
  context.drawImage(
    image,
    crop.x,
    crop.y,
    crop.width,
    crop.height,
    0,
    0,
    crop.width,
    crop.height,
  );
  const encoded = canvas.toDataURL("image/png");
  if (!encoded.startsWith("data:image/png;base64,"))
    throw new Error("Could not encode region screenshot. Retry capture.");
  return {
    image: encoded,
    width: crop.width,
    height: crop.height,
    sourceBounds: crop.sourceBounds,
  };
}

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
