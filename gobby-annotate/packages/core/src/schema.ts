import { z } from "zod";

export const MAX_BYTES = 128 * 1024 * 1024;
export const MAX_ANNOTATIONS = 100;
export const id = z.string().uuid();
const finite = z.number().finite();
const positive = finite.positive();
export const boundsSchema = z
  .object({
    x: finite,
    y: finite,
    width: finite.nonnegative(),
    height: finite.nonnegative(),
  })
  .strict();
export const viewportSchema = z
  .object({
    layout: z.object({ width: positive, height: positive }).strict(),
    visual: z
      .object({
        width: positive,
        height: positive,
        offsetLeft: finite,
        offsetTop: finite,
        scale: positive,
      })
      .strict(),
    devicePixelRatio: positive,
    scrollX: finite,
    scrollY: finite,
  })
  .strict();
export const screenshotSchema = z.discriminatedUnion("status", [
  z
    .object({
      status: z.literal("available"),
      path: z.string().regex(/^screenshots\/[a-zA-Z0-9_-]+\.png$/),
      width: z.number().int().positive(),
      height: z.number().int().positive(),
      sourceBounds: boundsSchema
        .extend({ width: positive, height: positive })
        .optional(),
    })
    .strict(),
  z
    .object({
      status: z.literal("unavailable"),
      reason: z.string().trim().min(1).max(2000),
    })
    .strict(),
]);
export const annotationSchema = z
  .object({
    id,
    revision: z.number().int().positive(),
    createdAt: z.string().datetime(),
    updatedAt: z.string().datetime(),
    comment: z.string().max(20000),
    page: z
      .object({ url: z.string().url().max(8192), title: z.string().max(2000) })
      .strict(),
    frame: z
      .object({
        url: z.string().max(8192),
        path: z.array(z.string().max(2000)).max(32),
        access: z.enum(["document", "host-only", "visible-region"]),
      })
      .strict(),
    target: z
      .object({
        kind: z.enum(["element", "rectangle"]),
        locator: z.array(z.string().max(2000)).max(64),
        tag: z.string().max(100),
        role: z.string().max(100),
        text: z.string().max(500),
        bounds: boundsSchema,
        screenshotBounds: boundsSchema,
      })
      .strict(),
    viewport: viewportSchema,
    screenshot: screenshotSchema,
  })
  .strict();
export const manifestSchema = z
  .object({
    version: z.literal(1),
    batchId: id,
    exportId: id,
    title: z.string().trim().min(1).max(200),
    exportedAt: z.string().datetime(),
    annotations: z.array(annotationSchema).min(1).max(MAX_ANNOTATIONS),
  })
  .strict()
  .superRefine((value, ctx) => {
    const ids = new Set<string>();
    for (const a of value.annotations) {
      if (ids.has(a.id))
        ctx.addIssue({
          code: "custom",
          message: `Duplicate annotation ID: ${a.id}`,
        });
      if (!a.comment.trim())
        ctx.addIssue({
          code: "custom",
          message: `Annotation ${a.id} needs a comment`,
        });
      if (Date.parse(a.updatedAt) < Date.parse(a.createdAt))
        ctx.addIssue({
          code: "custom",
          message: `Annotation ${a.id} has reversed timestamps`,
        });
      ids.add(a.id);
    }
  });
export type Bounds = z.infer<typeof boundsSchema>;
export type Viewport = z.infer<typeof viewportSchema>;
export type Annotation = z.infer<typeof annotationSchema>;
export type Manifest = z.infer<typeof manifestSchema>;
export type Bundle = { manifest: Manifest; assets: Map<string, Uint8Array> };

export function safePath(path: string): boolean {
  return (
    path === "capture.json" || /^screenshots\/[a-zA-Z0-9_-]+\.png$/.test(path)
  );
}
