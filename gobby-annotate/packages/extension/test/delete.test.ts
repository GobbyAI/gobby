import "fake-indexeddb/auto";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { Drafts, isExported } from "../src/drafts";
import { deleteBatches } from "../src/export";
import { fixture, png } from "../../core/test/fixtures";

const shot = "screenshots/example.png";
let appGroup: Set<string>;
let native: ReturnType<typeof vi.fn>;
beforeEach(() => {
  // Emulates ExportStore's delete: the App Group holds staged ZIPs by export ID.
  appGroup = new Set();
  native = vi.fn(
    async (_host: string, message: { action: string; transferId: string }) => {
      if (message.action !== "delete") return { ok: false };
      appGroup.delete(`gobby-annotate-${message.transferId}.zip`);
      return { ok: true };
    },
  );
  vi.stubGlobal("chrome", { runtime: { sendNativeMessage: native } });
});
afterEach(() => {
  vi.unstubAllGlobals();
  vi.useRealTimers();
});

async function withNote(drafts: Drafts, title: string) {
  const batch = await drafts.create(title);
  return drafts.change(batch.id, {
    annotation: {
      ...fixture().manifest.annotations[0]!,
      id: crypto.randomUUID(),
    },
    image: new Blob([new Uint8Array(png)], { type: "image/png" }),
  });
}
async function stage(drafts: Drafts, batchId: string, at: string) {
  const id = crypto.randomUUID();
  appGroup.add(`gobby-annotate-${id}.zip`);
  await drafts.recordExport(batchId, { id, at, staged: true });
  return `gobby-annotate-${id}.zip`;
}

it("reads a batch as exported until an edit lands after its last export", async () => {
  vi.useFakeTimers({ toFake: ["Date"] });
  vi.setSystemTime(new Date("2026-10-15T10:00:00.000Z"));
  const drafts = new Drafts(crypto.randomUUID()),
    batch = await withNote(drafts, "Review");
  expect(isExported(batch)).toBe(false);
  await drafts.recordExport(batch.id, {
    id: crypto.randomUUID(),
    at: batch.updatedAt,
    staged: false,
  });
  const exported = (await drafts.get(batch.id))!;
  expect(exported.updatedAt).toBe(batch.updatedAt);
  expect(isExported(exported)).toBe(true);
  vi.setSystemTime(new Date("2026-10-15T10:00:01.000Z"));
  const edited = await drafts.change(batch.id, { title: "Review, edited" });
  expect(edited.exports).toHaveLength(1);
  expect(isExported(edited)).toBe(false);
});

it("removes one batch and every screenshot under its key prefix", async () => {
  const drafts = new Drafts(crypto.randomUUID()),
    removed = await withNote(drafts, "Remove me"),
    kept = await withNote(drafts, "Keep me");
  await drafts.remove(removed.id);
  expect(await drafts.get(removed.id)).toBeUndefined();
  expect(await drafts.image(removed.id, shot)).toBeUndefined();
  expect((await drafts.list()).map((b) => b.id)).toEqual([kept.id]);
  expect((await drafts.snapshot(kept.id)).assets.get(shot)).toEqual(png);
});

it("deletes a batch from IndexedDB and every staged ZIP it delivered", async () => {
  const drafts = new Drafts(crypto.randomUUID()),
    batch = await withNote(drafts, "Exported twice"),
    other = await withNote(drafts, "Other");
  const first = await stage(drafts, batch.id, batch.updatedAt),
    second = await stage(drafts, batch.id, batch.updatedAt),
    otherZip = await stage(drafts, other.id, other.updatedAt);
  await drafts.recordExport(batch.id, {
    id: crypto.randomUUID(),
    at: batch.updatedAt,
    staged: false,
  });
  expect(await deleteBatches(drafts, [batch.id])).toBe(1);
  expect(native).toHaveBeenCalledTimes(2);
  expect(appGroup.has(first)).toBe(false);
  expect(appGroup.has(second)).toBe(false);
  expect([...appGroup]).toEqual([otherZip]);
  expect(await drafts.get(batch.id)).toBeUndefined();
  expect(await drafts.image(batch.id, shot)).toBeUndefined();
  expect((await drafts.list()).map((b) => b.id)).toEqual([other.id]);
});

it("keeps the batch for a retry when the app cannot delete its ZIP", async () => {
  const drafts = new Drafts(crypto.randomUUID()),
    batch = await withNote(drafts, "Staged");
  const zip = await stage(drafts, batch.id, batch.updatedAt);
  native.mockResolvedValueOnce({ ok: false });
  await expect(deleteBatches(drafts, [batch.id])).rejects.toThrow(
    /could not delete the staged export/,
  );
  expect(appGroup.has(zip)).toBe(true);
  expect((await drafts.snapshot(batch.id)).assets.get(shot)).toEqual(png);
});

it("clears exported batches but keeps drafts edited after their export", async () => {
  vi.useFakeTimers({ toFake: ["Date"] });
  vi.setSystemTime(new Date("2026-10-15T10:00:00.000Z"));
  const drafts = new Drafts(crypto.randomUUID()),
    exported = await withNote(drafts, "Exported"),
    edited = await withNote(drafts, "Edited after export"),
    draft = await withNote(drafts, "Never exported");
  const exportedZip = await stage(drafts, exported.id, exported.updatedAt),
    editedZip = await stage(drafts, edited.id, edited.updatedAt);
  vi.setSystemTime(new Date("2026-10-15T10:00:01.000Z"));
  await drafts.change(edited.id, { title: "Edited after export, again" });
  const ids = (await drafts.list()).map((b) => b.id);
  expect(await deleteBatches(drafts, ids, { when: isExported })).toBe(1);
  expect(appGroup.has(exportedZip)).toBe(false);
  expect([...appGroup]).toEqual([editedZip]);
  expect((await drafts.list()).map((b) => b.id).sort()).toEqual(
    [edited.id, draft.id].sort(),
  );
});
