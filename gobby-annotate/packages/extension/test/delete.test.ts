import "fake-indexeddb/auto";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { Drafts, isExported } from "../src/drafts";
import { applyAppDeletions, deleteBatches } from "../src/export";
import { fixture, png } from "../../core/test/fixtures";

const shot = "screenshots/example.png";
let appGroup: Set<string>;
let appDeleted: Set<string>;
let native: ReturnType<typeof vi.fn>;
// Emulates ExportStore: the App Group holds staged ZIPs by export ID. The app's own
// deletions wait in appDeleted until the extension acknowledges them; unstaging a ZIP
// leaves them in place.
async function hostReply(
  _host: string,
  message: { action: string; transferId?: string },
) {
  if (message.action === "deleted")
    return { ok: true, deleted: [...appDeleted] };
  if (message.action === "acknowledge") {
    appDeleted.delete(message.transferId ?? "");
    return { ok: true };
  }
  if (message.action !== "delete") return { ok: false };
  appGroup.delete(`gobby-annotate-${message.transferId}.zip`);
  return { ok: true };
}
beforeEach(() => {
  appGroup = new Set();
  appDeleted = new Set();
  native = vi.fn(hostReply);
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

/** Emulates ExportStore.delete: records the app's deletion, then removes the ZIP. */
function appDelete(id: string) {
  appDeleted.add(id);
  appGroup.delete(`gobby-annotate-${id}.zip`);
}

it("removes an exported batch the app deleted, along with its other staged ZIPs", async () => {
  const drafts = new Drafts(crypto.randomUUID()),
    batch = await withNote(drafts, "Exported twice"),
    other = await withNote(drafts, "Other");
  await stage(drafts, batch.id, batch.updatedAt);
  await stage(drafts, batch.id, batch.updatedAt);
  const otherZip = await stage(drafts, other.id, other.updatedAt);
  const [deleted, sibling] = (await drafts.get(batch.id))!.exports!;
  appDelete(deleted!.id);
  await applyAppDeletions(drafts);
  expect(await drafts.get(batch.id)).toBeUndefined();
  expect(appGroup.has(`gobby-annotate-${sibling!.id}.zip`)).toBe(false);
  expect([...appGroup]).toEqual([otherZip]);
  expect((await drafts.list()).map((b) => b.id)).toEqual([other.id]);
  expect([...appDeleted]).toEqual([]);
});

it("keeps an app-deleted batch and its record until its removal commits", async () => {
  const name = crypto.randomUUID();
  const drafts = new Drafts(name),
    batch = await withNote(drafts, "Exported twice"),
    other = await withNote(drafts, "Other"),
    draft = await withNote(drafts, "Draft");
  await stage(drafts, batch.id, batch.updatedAt);
  await stage(drafts, batch.id, batch.updatedAt);
  const otherZip = await stage(drafts, other.id, other.updatedAt);
  const [deleted, sibling] = (await drafts.get(batch.id))!.exports!;
  appDelete(deleted!.id);
  const siblingZip = `gobby-annotate-${sibling!.id}.zip`;
  vi.spyOn(drafts, "remove").mockRejectedValueOnce(
    new Error("IndexedDB write failed"),
  );
  await expect(applyAppDeletions(drafts)).rejects.toThrow(
    "IndexedDB write failed",
  );
  expect(appGroup.has(siblingZip)).toBe(false);
  expect(appDeleted.has(deleted!.id)).toBe(true);
  expect((await drafts.snapshot(batch.id)).assets.get(shot)).toEqual(png);
  // A reopened Drafts on the same database stands in for a relaunch.
  const reopened = new Drafts(name);
  await applyAppDeletions(reopened);
  expect(await reopened.get(batch.id)).toBeUndefined();
  expect(await reopened.image(batch.id, shot)).toBeUndefined();
  expect([...appGroup]).toEqual([otherZip]);
  expect([...appDeleted]).toEqual([]);
  expect((await reopened.list()).map((b) => b.id).sort()).toEqual(
    [other.id, draft.id].sort(),
  );
});

it("keeps an app-deleted batch's record when a sibling ZIP cannot be unstaged", async () => {
  const name = crypto.randomUUID();
  const drafts = new Drafts(name),
    batch = await withNote(drafts, "Exported twice");
  await stage(drafts, batch.id, batch.updatedAt);
  await stage(drafts, batch.id, batch.updatedAt);
  const [deleted, sibling] = (await drafts.get(batch.id))!.exports!;
  appDelete(deleted!.id);
  const siblingZip = `gobby-annotate-${sibling!.id}.zip`;
  native.mockImplementation(
    async (host: string, message: { action: string; transferId?: string }) =>
      message.action === "delete" && message.transferId === sibling!.id
        ? { ok: false }
        : hostReply(host, message),
  );
  await expect(applyAppDeletions(drafts)).rejects.toThrow(
    /could not delete the staged export/,
  );
  expect(appGroup.has(siblingZip)).toBe(true);
  expect(appDeleted.has(deleted!.id)).toBe(true);
  expect(await drafts.get(batch.id)).toBeDefined();
  native.mockImplementation(hostReply);
  await applyAppDeletions(new Drafts(name));
  expect(appGroup.has(siblingZip)).toBe(false);
  expect(await new Drafts(name).get(batch.id)).toBeUndefined();
  expect([...appDeleted]).toEqual([]);
});

it("keeps a draft edited after its export and still retires the app's deletion", async () => {
  vi.useFakeTimers({ toFake: ["Date"] });
  vi.setSystemTime(new Date("2026-10-15T10:00:00.000Z"));
  const drafts = new Drafts(crypto.randomUUID()),
    batch = await withNote(drafts, "Edited after export");
  await stage(drafts, batch.id, batch.updatedAt);
  vi.setSystemTime(new Date("2026-10-15T10:00:01.000Z"));
  await drafts.change(batch.id, { title: "Edited after export, again" });
  appDelete((await drafts.get(batch.id))!.exports![0]!.id);
  await applyAppDeletions(drafts);
  expect((await drafts.get(batch.id))?.title).toBe(
    "Edited after export, again",
  );
  expect([...appDeleted]).toEqual([]);
});

it("unstages an app-deleted edited draft's ZIP after a rejected cleanup and keeps the draft", async () => {
  vi.useFakeTimers({ toFake: ["Date"] });
  vi.setSystemTime(new Date("2026-10-15T10:00:00.000Z"));
  const name = crypto.randomUUID();
  const drafts = new Drafts(name),
    batch = await withNote(drafts, "Edited after export");
  const zip = await stage(drafts, batch.id, batch.updatedAt);
  vi.setSystemTime(new Date("2026-10-15T10:00:01.000Z"));
  await drafts.change(batch.id, { title: "Edited after export, again" });
  const id = (await drafts.get(batch.id))!.exports![0]!.id;
  // The app recorded its deletion, then the interruption left its ZIP in the App Group.
  appDeleted.add(id);
  native.mockImplementation(
    async (host: string, message: { action: string; transferId?: string }) =>
      message.action === "delete" && message.transferId === id
        ? { ok: false }
        : hostReply(host, message),
  );
  await expect(applyAppDeletions(drafts)).rejects.toThrow(
    /could not delete the staged export/,
  );
  expect(appGroup.has(zip)).toBe(true);
  expect(appDeleted.has(id)).toBe(true);
  expect((await drafts.snapshot(batch.id)).assets.get(shot)).toEqual(png);
  native.mockImplementation(hostReply);
  // A reopened Drafts on the same database stands in for a relaunch.
  const reopened = new Drafts(name);
  await applyAppDeletions(reopened);
  expect(appGroup.has(zip)).toBe(false);
  expect(appDeleted.has(id)).toBe(false);
  expect((await reopened.get(batch.id))?.title).toBe(
    "Edited after export, again",
  );
  expect((await reopened.snapshot(batch.id)).assets.get(shot)).toEqual(png);
});

it("unstages an app deletion that no batch holds and retires its record", async () => {
  const drafts = new Drafts(crypto.randomUUID()),
    kept = await withNote(drafts, "Kept");
  const id = crypto.randomUUID();
  appGroup.add(`gobby-annotate-${id}.zip`);
  appDeleted.add(id);
  await applyAppDeletions(drafts);
  expect(appGroup.has(`gobby-annotate-${id}.zip`)).toBe(false);
  expect([...appDeleted]).toEqual([]);
  expect((await drafts.list()).map((b) => b.id)).toEqual([kept.id]);
  expect((await drafts.snapshot(kept.id)).assets.get(shot)).toEqual(png);
});

it("retires an app deletion that no batch holds", async () => {
  const drafts = new Drafts(crypto.randomUUID());
  appDeleted.add(crypto.randomUUID());
  await applyAppDeletions(drafts);
  expect([...appDeleted]).toEqual([]);
  expect(await drafts.list()).toEqual([]);
});
